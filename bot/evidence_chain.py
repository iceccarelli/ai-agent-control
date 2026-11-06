"""A tamper-evident sidecar for a JSONL evidence file.

For ``capacity-YYYYMMDD.jsonl`` the recorder also writes

  ``<file>.chain``  one line per evidence row: ``{"n": i, "r": sha256(row line), "h": chain}``
                    where ``chain_i = sha256(chain_{i-1} || r_i)`` from an all-zero genesis, and for an entry that adopted an
                    unchained row after a crash (``"resealed": true``) ``chain_i = sha256(chain_{i-1} || r_i || "RESEALED")``;
  ``<file>.head``   the latest ``{"n": count, "head": chain_n}``, replaced atomically.

and publishes the head OUTSIDE the evidence directory (``recorder_status.json`` and, through
the monitor, every ``monitor.jsonl`` line the external watcher reads). `verify` then detects:

  MUTATED           a row changed in place
  MISSING_INTERIOR  a row removed from the middle
  TRUNCATED         rows removed from the end (against the head file or an external anchor)
  DUPLICATED        a row repeated or inserted
  REORDERED         the same rows in a different order
  UNCHAINED_TAIL    rows written whose chain entry was not (a crash between the two writes);
                    visible, and sealed by `reseal_tail` with an explicit ``resealed`` mark
  HEAD_MISSING / HEAD_MISMATCH / ANCHOR_MISMATCH   the head cannot be verified

Honest limits:
  * A row adopted by ``reseal_tail`` was written without a chain entry; it is chained afterwards and marked, and ``admitted_rows``
    leaves it OUT of every statistic (a crash costs one row of evidence). The mark is part of the entry's hash, so adding it to
    an ordinary entry (to make a row vanish) or removing it from an adopted one (to make an adopted row count) breaks the
    sidecar. That is all it proves: a writer who can append to the file and its sidecar can still chain a forged NEW row, and
    only an external anchor taken before the forgery shows it.
  * A writer with access to every file AND to the anchors can forge a consistent history. The
    anchors copied into ``monitor.jsonl`` are a SECOND COPY ON THE SAME VOLUME: they are external to
    the evidence directory, not to the machine. They become genuinely external only when the owner
    archives ``monitor.jsonl`` (or the printed head) off the machine. Without any anchor a consistent
    truncation of the tail cannot be detected, and the report says "UNANCHORED".
  * Each day's chain starts from the all-zero genesis; there is no link between days. Removing a whole day (file and sidecars)
    is NOT a chain failure. It is caught only by an anchor for that file (the report then lists it as missing but anchored) or,
    if no anchor exists, by the reliability report's gap/completeness measures, which a small removal can stay under.
Pure standard library.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

GENESIS = "00" * 32
#: `append` writes the row and then its chain entry, so one crash can leave AT MOST ONE row unchained.
MAX_UNCHAINED = 1
#: Rows adopted by `reseal_tail` over a file's life. A few crashes are normal; many means rows were injected.
MAX_RESEALED_PER_FILE = 3
#: An external anchor this many rows (or fewer) behind the chain's head is LAGGING (the monitor copies the head hourly,
#: the recorder appends every five minutes); further behind it is STALE and the unwitnessed tail is long.
MAX_ANCHOR_LAG_ROWS = 48

#: verify() anchor_status values, weakest to strongest. Only ANCHORED_CURRENT means the newest anchor is the chain's head.
UNANCHORED = "UNANCHORED"
STALE_ANCHOR = "STALE_ANCHOR"
ANCHORED_LAGGING = "ANCHORED_LAGGING"
ANCHORED_CURRENT = "ANCHORED_CURRENT"
ANCHOR_CONFLICT = "ANCHOR_CONFLICT"


def canonical(row: Mapping[str, Any]) -> bytes:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def row_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


RESEAL_MARK = b"RESEALED"


def link(prev_hex: str, r_hex: str, resealed: bool = False) -> str:
    """Next chain value. A resealed entry (a row adopted after a crash) hashes a fixed mark in as well, so the mark cannot be
    added to or removed from an entry without the sidecar failing to recompute."""
    return hashlib.sha256(bytes.fromhex(prev_hex) + bytes.fromhex(r_hex) + (RESEAL_MARK if resealed else b"")).hexdigest()


def _paths(path: str) -> Tuple[str, str]:
    return path + ".chain", path + ".head"


def _atomic(path: str, text: str) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_chain_ex(path: str) -> Tuple[List[Dict[str, Any]], bool, bool]:
    """``(entries, torn_last_line, corrupt)``. Reading stops at the first line that is not a
    well-formed entry. A bad FINAL line with no newline is a torn write (benign, healed by the next
    writer); a bad line anywhere else, or a well-formed JSON line of the wrong shape, is corruption."""
    cpath, _ = _paths(path)
    out: List[Dict[str, Any]] = []
    torn = corrupt = False
    try:
        with open(cpath, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return out, False, False
    parts = data.split(b"\n")
    last = parts.pop()                       # b"" when the file ends with a newline
    for raw in parts:
        if not raw.strip():
            continue
        try:
            e = json.loads(raw)
        except (ValueError, RecursionError):
            corrupt = True
            return out, torn, corrupt
        if not (isinstance(e, dict) and isinstance(e.get("n"), int) and isinstance(e.get("r"), str)
                and isinstance(e.get("h"), str)):
            corrupt = True
            return out, torn, corrupt
        out.append(e)
    if last.strip():
        try:
            e = json.loads(last)
            if isinstance(e, dict) and isinstance(e.get("n"), int) and isinstance(e.get("r"), str) \
                    and isinstance(e.get("h"), str):
                out.append(e)                # complete entry whose newline was lost
            else:
                corrupt = True
        except (ValueError, RecursionError):
            torn = True
    return out, torn, corrupt


def _read_chain(path: str) -> List[Dict[str, Any]]:
    return _read_chain_ex(path)[0]


class ChainDesync(RuntimeError):
    """The evidence file and its sidecar disagree about how many rows exist (or the file ends in a
    torn line). Appending now would weld the disagreement into the record permanently."""


def _file_ends_clean(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                return True
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) == b"\n"
    except FileNotFoundError:
        return True


def append(path: str, row: Mapping[str, Any]) -> Tuple[int, str]:
    """Append `row` to the evidence file and its chain; returns ``(n, head)``.
    Order: row line, chain line, head. A crash between them leaves an UNCHAINED_TAIL (visible)
    or a head one behind the chain (still verifiable), never a silent gap."""
    raw = canonical(row)
    chain, side_torn, side_corrupt = _read_chain_ex(path)
    if side_corrupt or side_torn:
        raise ChainDesync("the chain sidecar is torn or corrupt; run repair() before appending")
    if not _file_ends_clean(path) or not _file_ends_clean(_paths(path)[0]):
        raise ChainDesync("the evidence file or its sidecar ends in a torn line; run repair() before appending")
    lines_on_disk, _q = read_lines(path)
    if _q:
        raise ChainDesync("the evidence file ends in a line that is not valid JSON; run repair() before appending")
    if len(lines_on_disk) != len(chain):
        raise ChainDesync(f"{len(lines_on_disk)} rows on disk but {len(chain)} chained; run repair() before appending")
    prev = chain[-1]["h"] if chain else GENESIS
    r = row_hash(raw)
    h = link(prev, r)
    n = len(chain) + 1
    cpath, hpath = _paths(path)
    with open(path, "ab") as fh:
        fh.write(raw + b"\n")
        fh.flush()
        os.fsync(fh.fileno())
    with open(cpath, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"n": n, "r": r, "h": h}, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    _atomic(hpath, json.dumps({"n": n, "head": h}, sort_keys=True))
    return n, h


def read_lines(path: str) -> Tuple[List[bytes], List[Dict[str, Any]]]:
    """The raw row lines, and the QUARANTINED ones: a final line with no newline or that is not
    JSON is set aside (with its line number), never parsed into a row, never hidden."""
    lines: List[bytes] = []
    quarantined: List[Dict[str, Any]] = []
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return lines, quarantined
    parts = data.split(b"\n")
    tail = parts.pop()                      # b"" when the file ends with a newline
    for i, ln in enumerate(parts, 1):
        if not ln.strip():
            continue
        lines.append(ln)
    if tail.strip():
        quarantined.append({"line": len(parts) + 1, "reason": "final line has no newline (torn write)",
                            "bytes": len(tail)})
    # a complete-but-unparseable FINAL line is also quarantined
    if lines:
        try:
            json.loads(lines[-1])
        except (ValueError, RecursionError):
            quarantined.append({"line": len(lines), "reason": "final line is not valid JSON", "bytes": len(lines[-1])})
            lines.pop()
    return lines, quarantined


def rows_of(lines: Sequence[bytes]) -> List[Any]:
    out: List[Any] = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except (ValueError, RecursionError):
            out.append(None)                # an interior corrupt line stays visible as None
    return out


def head_of(path: str) -> Optional[Dict[str, Any]]:
    _, hpath = _paths(path)
    try:
        with open(hpath, encoding="utf-8") as fh:
            h = json.load(fh)
        ok = (isinstance(h, dict) and isinstance(h.get("n"), int) and not isinstance(h.get("n"), bool)
              and isinstance(h.get("head"), str))
        return h if ok else None
    except (OSError, ValueError, RecursionError):
        return None


def verify(path: str, anchors: Optional[Sequence[Mapping[str, Any]]] = None) -> Dict[str, Any]:
    """Every check in this module's docstring. There is deliberately NO leniency for a resealed entry whose hash lacks the mark: such an
    entry is byte-for-byte what an ORDINARY entry with the flag flipped onto it looks like, and an external anchor cannot tell them apart
    (the anchor is the chain head, which does not contain the flag), so accepting it would reopen the hole the mark in the hash closes.
    No such file exists in production: ``evidence_chain`` first appears in this change, so nothing deployed has ever written one; a file
    from an earlier revision of this branch would verify as SIDECAR_INCONSISTENT and its day be excluded, visibly."""
    lines, quarantined = read_lines(path)
    chain, side_torn, side_corrupt = _read_chain_ex(path)
    head = head_of(path)
    issues: List[Dict[str, Any]] = []
    valid_anchors = [a for a in (anchors or ()) if isinstance(a, Mapping) and isinstance(a.get("n"), int)
                     and not isinstance(a.get("n"), bool) and a["n"] >= 1 and isinstance(a.get("head"), str)]

    def add(code: str, detail: str, index: Optional[int] = None) -> None:
        issues.append({"code": code, "detail": detail, "index": index})

    # 1. the sidecar must be internally consistent (its own chain recomputes)
    prev = GENESIS
    side_ok = True
    if side_corrupt:
        add("SIDECAR_CORRUPT", "the chain sidecar holds a line that is not a well-formed entry")
        side_ok = False
    for i, e in enumerate(chain):
        try:
            marked = "resealed" in e             # absent, or exactly True: any other value is not something the writer produces
            well_formed = (e.get("n") == i + 1 and (not marked or e["resealed"] is True) and len(e["r"]) == 64 and len(e["h"]) == 64)
            recomputes = well_formed and link(prev, e["r"], marked) == e["h"]
        except (ValueError, TypeError):          # r / h are not hex
            recomputes = False
        if not recomputes:
            add("SIDECAR_INCONSISTENT", f"chain entry {i + 1} does not recompute", i)
            side_ok = False
            break
        prev = e["h"]
    file_hashes = [row_hash(ln) for ln in lines]
    side_hashes = [e.get("r") for e in chain]
    unchained = 0
    if file_hashes != side_hashes:
        common = 0
        for a, b in zip(file_hashes, side_hashes):
            if a != b:
                break
            common += 1
        if file_hashes[:len(side_hashes)] == side_hashes and len(file_hashes) > len(side_hashes):
            unchained = len(file_hashes) - len(side_hashes)
            add("UNCHAINED_TAIL", f"{unchained} row(s) written after the last chain entry", len(side_hashes))
        elif side_hashes[:len(file_hashes)] == file_hashes and len(file_hashes) < len(side_hashes):
            add("TRUNCATED", f"{len(side_hashes) - len(file_hashes)} row(s) missing from the end", len(file_hashes))
        else:
            fs, ss = sorted(file_hashes), sorted(side_hashes)
            if fs == ss:
                add("REORDERED", "same rows, different order", common)
            elif len(file_hashes) == len(side_hashes):
                add("MUTATED", f"row {common + 1} differs from what was chained", common)
            elif len(file_hashes) < len(side_hashes):
                add("MISSING_INTERIOR", f"{len(side_hashes) - len(file_hashes)} row(s) removed (first difference at row {common + 1})", common)
            else:
                dup = len(file_hashes) - len(set(file_hashes))
                add("DUPLICATED" if dup else "INSERTED", f"{len(file_hashes) - len(side_hashes)} extra row(s) (first difference at row {common + 1})", common)
    elif len(set(file_hashes)) != len(file_hashes):
        add("DUPLICATED", "an identical row appears twice (and was chained twice)", None)
    # 2. the head
    if head is None:
        if chain or lines:                    # a brand-new empty file has nothing to anchor yet
            add("HEAD_MISSING", "no readable .head: the chain's end is unverifiable")
    elif side_ok and chain:
        if head.get("n") != chain[-1]["n"] or head.get("head") != chain[-1]["h"]:
            if head.get("n", 0) > len(chain):
                add("TRUNCATED", "the head records more rows than the chain holds")
            elif not any(i["code"] in ("TRUNCATED",) for i in issues):
                add("HEAD_MISMATCH", "the head file does not match the chain's last entry")
    elif head is not None and not chain and head.get("n"):
        add("TRUNCATED", "the chain is empty but the head records rows")
    # 3. external anchors: EVERY valid anchor must match the chain at its row; the LATEST decides the status
    anchor_conflict = False
    for a in valid_anchors:
        n, hd = a["n"], a["head"]
        if n > len(chain):
            add("TRUNCATED", f"an external anchor records {n} rows; only {len(chain)} are chained")
            anchor_conflict = True
        elif chain[n - 1]["h"] != hd:
            add("ANCHOR_MISMATCH", f"chain head at row {n} differs from the external anchor")
            anchor_conflict = True
    resealed = sum(1 for e in chain if e.get("resealed"))
    if unchained > MAX_UNCHAINED:
        add("UNCHAINED_TAIL_EXCESS", f"{unchained} unchained rows; a crash can leave at most {MAX_UNCHAINED}", len(chain))
    if resealed > MAX_RESEALED_PER_FILE:
        add("RESEALED_EXCESS", f"{resealed} rows were adopted by reseal (more than {MAX_RESEALED_PER_FILE} crashes' worth)")
    serious = [i for i in issues if i["code"] != "UNCHAINED_TAIL"]
    internally_consistent = not serious
    latest = max((a["n"] for a in valid_anchors), default=None)
    lag = None if latest is None or latest > len(chain) else len(chain) - latest
    if anchor_conflict:
        anchor_status = ANCHOR_CONFLICT
    elif latest is None:
        anchor_status = UNANCHORED
    elif not internally_consistent:
        anchor_status = "UNVERIFIABLE_CHAIN_BROKEN"
    elif lag == 0:
        anchor_status = ANCHORED_CURRENT
    elif lag <= MAX_ANCHOR_LAG_ROWS:
        anchor_status = ANCHORED_LAGGING
    else:
        anchor_status = STALE_ANCHOR
    return {"path": os.path.basename(path), "ok": not issues,
            # three DIFFERENT statements, never one Boolean:
            "internally_consistent": internally_consistent,           # the chained prefix verifies (no mutation/removal/reorder/...)
            "fully_chained": internally_consistent and unchained == 0,   # AND every row in the file has a chain entry
            "economic_evidence_eligible": not issues,                 # AND no issue of any kind: the only state a calculation may rest on
            "verified_prefix_rows": len(chain) if internally_consistent else 0,   # the rows a reader may ADMIT; unchained rows are never among them
            "tamper_evident_ok": internally_consistent,               # deprecated alias of internally_consistent (it tolerates an unchained tail)
            "anchor_status": anchor_status, "chain_rows": len(chain), "latest_anchor_rows": latest, "anchor_lag_rows": lag,
            "anchored": anchor_status in (ANCHORED_CURRENT, ANCHORED_LAGGING), "resealed_rows": resealed,
            "resealed_indices": [i for i, e in enumerate(chain) if e.get("resealed")], "sidecar_torn_tail": side_torn,
            "torn_aside_bytes": (os.path.getsize(path + ".torn") if os.path.exists(path + ".torn") else 0),
            "n_rows": len(lines), "n_chained": len(chain), "unchained_tail": unchained,
            "head": None if not chain else {"n": chain[-1]["n"], "head": chain[-1]["h"]},
            "quarantined_lines": quarantined, "issues": issues}


def admitted_rows(path: str, anchors: Optional[Sequence[Mapping[str, Any]]] = None) -> Tuple[List[Any], Dict[str, Any]]:
    """The rows a statistic may use from this file, with the verification that decided it: only the chained prefix of a
    file whose chain is internally consistent. A row without a chain entry is NEVER admitted, and a file that fails
    verification admits nothing. Read-only: this never repairs."""
    v = verify(path, anchors)
    lines, _ = read_lines(path)
    skip = set(v["resealed_indices"])           # a row ADOPTED by a repair was never chained when written: visible, counted, not evidence
    return [r for i, r in enumerate(rows_of(lines[:v["verified_prefix_rows"]])) if i not in skip], v


def reseal_tail(path: str) -> int:
    """Chain rows that were written but never chained (a crash between the two writes), each
    marked ``resealed`` so the repair is visible. Refuses unless everything before the tail
    verifies; returns the number sealed."""
    v = verify(path)
    if any(i["code"] not in ("UNCHAINED_TAIL", "HEAD_MISMATCH", "HEAD_MISSING") for i in v["issues"]):
        return 0
    if v["unchained_tail"] > MAX_UNCHAINED or v["resealed_rows"] + v["unchained_tail"] > MAX_RESEALED_PER_FILE:
        return 0                               # more than a crash can explain: do not legitimise it
    cpath, hpath = _paths(path)
    _truncate_to_last_newline(cpath)          # a torn / newline-less chain tail is regenerated below
    lines, _ = read_lines(path)
    chain = _read_chain(path)
    prev = chain[-1]["h"] if chain else GENESIS
    n = len(chain)
    sealed = 0
    with open(cpath, "a", encoding="utf-8") as fh:
        for ln in lines[len(chain):]:
            r = row_hash(ln)
            prev = link(prev, r, resealed=True)
            n += 1
            fh.write(json.dumps({"n": n, "r": r, "h": prev, "resealed": True}, sort_keys=True) + "\n")
            sealed += 1
        fh.flush()
        os.fsync(fh.fileno())
    if n:
        _atomic(hpath, json.dumps({"n": n, "head": prev}, sort_keys=True))
    return sealed


def _truncate_to_last_newline(path: str, keep_aside: Optional[str] = None) -> int:
    """Drop bytes after the last newline (a torn write). The dropped bytes are appended to
    ``keep_aside`` when given, so nothing that was on disk is destroyed. Returns bytes dropped."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return 0
    cut = data.rfind(b"\n") + 1
    torn = data[cut:]
    if not torn:
        return 0
    if keep_aside:
        with open(keep_aside, "ab") as fh:
            fh.write(torn + b"\n")
            fh.flush()
            os.fsync(fh.fileno())
    with open(path, "r+b") as fh:
        fh.truncate(cut)
        fh.flush()
        os.fsync(fh.fileno())
    return len(torn)


def _set_aside_bad_tail(path: str) -> int:
    """A newline-terminated final line that is not JSON (read_lines quarantines it) is moved to
    ``<file>.torn`` and cut from the file, so the file and its chain agree again. Returns bytes moved."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return 0
    if not data.endswith(b"\n"):
        return 0
    body = data[:-1]
    cut = body.rfind(b"\n") + 1
    last = body[cut:]
    if not last.strip():
        return 0
    try:
        json.loads(last)
        return 0
    except (ValueError, RecursionError):
        pass
    with open(path + ".torn", "ab") as fh:
        fh.write(last + b"\n")
        fh.flush()
        os.fsync(fh.fileno())
    with open(path, "r+b") as fh:
        fh.truncate(cut)
        fh.flush()
        os.fsync(fh.fileno())
    return len(last)


def needs_repair(path: str) -> bool:
    """True when the file or its sidecar is in a state `append` would refuse, or when its head lags."""
    chain, side_torn, side_corrupt = _read_chain_ex(path)
    if side_torn or side_corrupt or not _file_ends_clean(path) or not _file_ends_clean(_paths(path)[0]):
        return True
    lines, quarantined = read_lines(path)
    if quarantined or len(lines) != len(chain):
        return True
    return any(i["code"] in ("HEAD_MISMATCH", "HEAD_MISSING", "UNCHAINED_TAIL") for i in verify(path)["issues"])


def repair(path: str) -> Dict[str, Any]:
    """Make an evidence file appendable again after a crash, visibly: a torn final row is moved
    to ``<file>.torn`` (kept, never deleted), rows written but not chained are sealed with a
    ``resealed`` mark. Refuses (changes nothing further) if anything BEFORE the tail fails to verify."""
    moved = _truncate_to_last_newline(path, keep_aside=path + ".torn")
    moved += _set_aside_bad_tail(path)
    sealed = reseal_tail(path)
    v = verify(path)
    return {"torn_bytes_set_aside": moved, "rows_resealed": sealed, "tamper_evident_ok": v["tamper_evident_ok"],
            "appendable": v["tamper_evident_ok"] and v["unchained_tail"] == 0 and _file_ends_clean(path)}


def bulk_write(path: str, rows: Sequence[Mapping[str, Any]]) -> Tuple[int, str]:
    """Write many rows and their chain in one pass (offline tooling and tests). Produces the same
    bytes `append` would have produced row by row. Refuses to touch an existing file."""
    cpath, hpath = _paths(path)
    if os.path.exists(path) or os.path.exists(cpath):
        raise FileExistsError(path)
    prev, n = GENESIS, 0
    with open(path, "wb") as rf, open(cpath, "w", encoding="utf-8") as cf:
        for row in rows:
            raw = canonical(row)
            r = row_hash(raw)
            prev = link(prev, r)
            n += 1
            rf.write(raw + b"\n")
            cf.write(json.dumps({"n": n, "r": r, "h": prev}, sort_keys=True) + "\n")
    if n:
        _atomic(hpath, json.dumps({"n": n, "head": prev}, sort_keys=True))
    return n, prev
