"""A tamper-evident sidecar for a JSONL evidence file.

For ``capacity-YYYYMMDD.jsonl`` the recorder also writes

  ``<file>.chain``  one line per evidence row: ``{"n": i, "r": sha256(row line), "h": chain}``
                    where ``chain_i = sha256(chain_{i-1} || r_i)`` from an all-zero genesis;
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
  * A writer with access to every file AND to the anchors can forge a consistent history. The
    anchors copied into ``monitor.jsonl`` are a SECOND COPY ON THE SAME VOLUME: they are external to
    the evidence directory, not to the machine. They become genuinely external only when the owner
    archives ``monitor.jsonl`` (or the printed head) off the machine. Without any anchor a consistent
    truncation of the tail cannot be detected, and the report says "UNANCHORED".
  * Each day's chain starts from the all-zero genesis; there is no link between days. Removing a
    whole day (file and sidecars) shows as a gap in the reliability report, not as a chain failure.
Pure standard library.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

GENESIS = "00" * 32


def canonical(row: Mapping[str, Any]) -> bytes:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def row_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def link(prev_hex: str, r_hex: str) -> str:
    return hashlib.sha256(bytes.fromhex(prev_hex) + bytes.fromhex(r_hex)).hexdigest()


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
        except ValueError:
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
        except ValueError:
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
        except ValueError:
            quarantined.append({"line": len(lines), "reason": "final line is not valid JSON", "bytes": len(lines[-1])})
            lines.pop()
    return lines, quarantined


def rows_of(lines: Sequence[bytes]) -> List[Any]:
    out: List[Any] = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            out.append(None)                # an interior corrupt line stays visible as None
    return out


def head_of(path: str) -> Optional[Dict[str, Any]]:
    _, hpath = _paths(path)
    try:
        with open(hpath, encoding="utf-8") as fh:
            h = json.load(fh)
        return h if isinstance(h, dict) and "n" in h and "head" in h else None
    except (OSError, ValueError):
        return None


def verify(path: str, anchors: Optional[Sequence[Mapping[str, Any]]] = None) -> Dict[str, Any]:
    lines, quarantined = read_lines(path)
    chain, side_torn, side_corrupt = _read_chain_ex(path)
    head = head_of(path)
    issues: List[Dict[str, Any]] = []

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
            recomputes = e.get("n") == i + 1 and link(prev, e["r"]) == e["h"]
        except ValueError:                       # r / h are not hex
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
        add("HEAD_MISSING", "no readable .head: the chain's end is unverifiable")
    elif side_ok and chain:
        if head.get("n") != chain[-1]["n"] or head.get("head") != chain[-1]["h"]:
            if head.get("n", 0) > len(chain):
                add("TRUNCATED", "the head records more rows than the chain holds")
            elif not any(i["code"] in ("TRUNCATED",) for i in issues):
                add("HEAD_MISMATCH", "the head file does not match the chain's last entry")
    elif head is not None and not chain and head.get("n"):
        add("TRUNCATED", "the chain is empty but the head records rows")
    # 3. external anchors
    for a in anchors or ():
        n, hd = a.get("n"), a.get("head")
        if not isinstance(n, int) or n < 1:
            continue
        if n > len(chain):
            add("TRUNCATED", f"an external anchor records {n} rows; only {len(chain)} are chained")
        elif chain[n - 1]["h"] != hd:
            add("ANCHOR_MISMATCH", f"chain head at row {n} differs from the external anchor")
    serious = [i for i in issues if i["code"] != "UNCHAINED_TAIL"]
    resealed = sum(1 for e in chain if e.get("resealed"))
    return {"path": os.path.basename(path), "ok": not issues, "tamper_evident_ok": not serious,
            "anchored": bool(anchors), "resealed_rows": resealed, "sidecar_torn_tail": side_torn,
            "n_rows": len(lines), "n_chained": len(chain), "unchained_tail": unchained,
            "head": None if not chain else {"n": chain[-1]["n"], "head": chain[-1]["h"]},
            "quarantined_lines": quarantined, "issues": issues}


def reseal_tail(path: str) -> int:
    """Chain rows that were written but never chained (a crash between the two writes), each
    marked ``resealed`` so the repair is visible. Refuses unless everything before the tail
    verifies; returns the number sealed."""
    v = verify(path)
    if any(i["code"] not in ("UNCHAINED_TAIL", "HEAD_MISMATCH") for i in v["issues"]):
        return 0
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
            prev = link(prev, r)
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


def repair(path: str) -> Dict[str, Any]:
    """Make an evidence file appendable again after a crash, visibly: a torn final row is moved
    to ``<file>.torn`` (kept, never deleted), rows written but not chained are sealed with a
    ``resealed`` mark. Refuses (changes nothing further) if anything BEFORE the tail fails to verify."""
    moved = _truncate_to_last_newline(path, keep_aside=path + ".torn")
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
