"""Seal, back up and restore-check the recorder's market-data log (``md-YYYYMMDD.db``), so a lost tail or a lost day is DETECTED.

Why. The log is hash-chained inside each segment, but nothing outside the segment remembers how far it got: a probe deleted ids 7-10 of 10 and
``MarketStore.verify()`` returned no problem and a reopened writer resumed at id 7. A year of evidence that can lose its newest day silently, on an
ephemeral volume with no copy, is not an asset. This tool adds the missing outside memory and a tested copy. It reads segments READ-ONLY and never
writes into the recorder's directory (the manifest and receipts live elsewhere, ideally on a different volume).

    python3 tools/md_seal.py seal    --dir MD_DIR --manifest M.jsonl                       # record how far each segment got (chained; append-only)
    python3 tools/md_seal.py check   --dir MD_DIR --manifest M.jsonl                       # is anything shorter, missing or altered since?
    python3 tools/md_seal.py backup  --dir MD_DIR --to OTHER_VOLUME_DIR --receipts R.jsonl --manifest M.jsonl # check against the seal, copy SEALED closed days, verify each copy
    python3 tools/md_seal.py restore-check --backup DIR --receipts R.jsonl --manifest M.jsonl   # every sealed closed day has a matching receipt; recompute every copy's chain

Guarantees: ``seal`` appends one chained row listing, for every segment, its record count, last id, last chain value and (for a closed day) the file's
SHA-256. ``check`` compares the directory with EVERY earlier row, not only the last, and reports TRUNCATED (fewer records / lost last id), MISSING (a
segment gone), ALTERED (a record's chain value or a closed day's hash changed) and a broken manifest chain. ``backup`` copies only closed days, re-hashes
and re-opens each copy before it writes a receipt, and never deletes or overwrites anything. ``restore-check`` recomputes every record's chain from its
payload in the copy: a backup is not proven until this passes.

Not claimed: that the manifest itself is safe (keep it off the recorder's volume and anchor its head somewhere the writer cannot reach, as with the trial
journal), that a segment which was never sealed can be recovered, or that a rolling day (still open) is hash-pinned; its progress is pinned by chain value.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import evidence_chain as EC  # noqa: E402
import md_store as MS  # noqa: E402


class SealError(ValueError):
    pass


def _ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _inside(path: str, directory: str) -> bool:
    """True when ``path`` is the directory or anything under it, after resolving symlinks (an alias or a link into the recorder's directory is still inside)."""
    rp, rd = os.path.realpath(path), os.path.realpath(directory)
    return rp == rd or rp.startswith(rd + os.sep)


def future_problems(names: Sequence[str], now_ms: int) -> List[str]:
    """Segments dated after today's UTC date (relative to ``now_ms``): a fabricated far-future day would otherwise become 'the newest segment'."""
    today = _today(now_ms)
    return [f"{n}: FUTURE segment (dated {n[3:11]}, today is {today}): fabricated or a clock fault; it is not part of the recorded history" for n in names if n[3:11] > today]


def _today(now_ms: int) -> str:
    return datetime.datetime.fromtimestamp(now_ms / 1000.0, tz=datetime.timezone.utc).strftime("%Y%m%d")


def segment_names(directory: str) -> List[str]:
    return sorted(f for f in os.listdir(directory) if f.startswith("md-") and f.endswith(".db") and len(f) == len("md-YYYYMMDD.db"))


def describe(directory: str, name: str, *, closed: bool) -> Dict[str, Any]:
    path = os.path.join(directory, name)
    con = _ro(path)
    try:
        n, first, last = con.execute("SELECT COUNT(*), MIN(id), MAX(id) FROM log").fetchone()
        chain = con.execute("SELECT chain FROM log ORDER BY id DESC LIMIT 1").fetchone()
        start = con.execute("SELECT value FROM meta WHERE key='chain_start'").fetchone()
    finally:
        con.close()
    out: Dict[str, Any] = {"segment": name, "records": int(n or 0), "first_id": first, "last_id": last,
                           "last_chain": bytes(chain[0]).hex() if chain else "", "chain_start": start[0] if start else "", "closed": bool(closed)}
    if closed:
        out["sha256"] = _sha256(path)                      # a closed day does not change, so its whole file is pinned
    return out


def _aside(path: str, suffix: str) -> str:
    """Rename ``path`` to ``path+suffix``, or ``suffix.2``, ``.3`` ... when that name is taken: an earlier set-aside is evidence and is never overwritten."""
    cand = path + suffix
    n = 1
    while os.path.lexists(cand):
        n += 1
        cand = f"{path}{suffix}.{n}"
    os.rename(path, cand)
    return cand


def _pruned_tail(directory: str) -> Optional[str]:
    """The last chain value the recorder recorded when it pruned its oldest days (``pruned.jsonl``), read-only; None when nothing was pruned."""
    try:
        with open(os.path.join(directory, "pruned.jsonl"), encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        return str(rows[-1]["last_chain"]) if rows else None
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def continuity_problems(directory: str, descs: Sequence[Dict[str, Any]], baseline: Optional[Dict[str, Dict[str, Any]]] = None, *, excused: bool = False) -> List[str]:
    """At least as strict as ``MarketStore.verify()``: every segment's ``chain_start`` must equal the previous segment's last chain and its first id must follow the
    previous last id (a backdated segment that carries its OWN chain_start is not part of the chain). The OLDEST segment must start from genesis, from the recorder's
    recorded prune tail, or from the chain_start it had when it was first sealed. ``descs`` is oldest first."""
    problems: List[str] = []
    prev_chain: Optional[str] = None
    prev_id: Optional[int] = None
    for idx, d in enumerate(descs):
        name, start = d["segment"], d.get("chain_start") or ""
        if idx == 0:
            tail = _pruned_tail(directory)
            sealed_start = (baseline or {}).get(name, {}).get("chain_start")
            ok_starts = {MS.GENESIS} | ({tail} if tail else set()) | ({sealed_start} if sealed_start else set())
            if excused:
                ok_starts.add(start)            # check() already proved this start equals the last receipted chain of the pruned days
            if start not in ok_starts:
                problems.append(f"{name}: CHAIN BREAK (the oldest segment starts from {start[:12]!r}: not genesis, not a recorded prune tail, not its sealed start)")
        elif prev_chain is not None and start != prev_chain:
            problems.append(f"{name}: CHAIN BREAK (chain_start does not continue the previous segment's last chain)")
        if d["first_id"] is not None:
            if prev_id is not None and d["first_id"] != prev_id + 1:
                problems.append(f"{name}: ID GAP (first id {d['first_id']}, the previous segment ended at {prev_id})")
            prev_id = d["last_id"]
        prev_chain = d["last_chain"] or start
    return problems


#: A day is treated as closed only an hour after it ended: the recorder closes yesterday's segment on its first append of the new day, so right after
#: midnight the writer may still hold it (a backup of an open WAL segment lost records at exactly this point).
CLOSE_MARGIN_MS = 3_600_000


def seal(directory: str, manifest: str, *, now_ms: Optional[int] = None, accept_problems: bool = False, receipts: Optional[str] = None,
         backup: Optional[str] = None) -> Dict[str, Any]:
    """Append one manifest row describing every segment now. Segments whose day ended over an hour ago are 'closed' and hash-pinned. Without an EXPLICIT ``now_ms``
    the NEWEST segment is never closed: a wall clock stepped forward would otherwise hash-pin a day the recorder is still writing (the next check would call the
    recorder's own growth ALTERED). The owner passes ``--now-ms`` to close the newest day after the recorder has stopped.
    REFUSES when ``check`` already finds a problem: sealing over a truncation would launder it into the new baseline (reproduced). ``accept_problems`` is
    the owner's explicit decision to re-baseline after investigating. ``receipts`` (the backup receipts) lets ``check`` excuse a day the recorder's retention pruned
    AFTER it was backed up; a pruned day with no verified receipt still refuses."""
    now = int(time.time() * 1000) if now_ms is None else min(int(now_ms), int(time.time() * 1000))
    if _inside(manifest, directory):
        raise SealError("the manifest must not live at or under the recorder's own directory: it is the outside memory")
    today = _today(now - CLOSE_MARGIN_MS)
    names = segment_names(directory)
    future = future_problems(names, int(time.time() * 1000))        # the REAL clock: an owner's earlier --now-ms must not turn existing days into 'future'
    if future and not accept_problems:
        raise SealError(f"refusing to seal: {future[:3]}")
    segs = [describe(directory, n, closed=n[3:11] < today and (now_ms is not None or n != names[-1])) for n in names]
    if not segs:
        raise SealError("no segments to seal")
    if os.path.isfile(manifest) and not accept_problems:
        prior = check(directory, manifest, receipts=receipts, backup=backup)
        if not prior["ok"]:
            raise SealError(f"refusing to seal over existing problems (that would make them the new baseline): {prior['problems'][:3]}")
    if not accept_problems:
        # the SAME receipt-based excuse check() applies: after retention pruned a backed-up day (and pruned.jsonl, which the recorder writes, is absent or not trusted)
        # the oldest remaining segment starts from the last receipted chain; without it seal refused what check() accepted (a deadlock)
        excused = False
        if os.path.isfile(manifest):
            names_now = [n for n in names if n[3:11] <= _today(int(time.time() * 1000))]
            excused = bool(_pruned_excused(directory, _by_segment(EC.rows_of(EC.read_lines(manifest)[0])), names_now, receipts, backup=backup))
        broken = continuity_problems(directory, segs, _baseline(manifest), excused=excused)
        if broken:
            raise SealError(f"refusing to seal a directory whose segments do not chain: {broken[:3]}")
    n, head = EC.append(manifest, {"at_ms": now, "segments": segs})
    return {"manifest_rows": n, "head": head, "segments": len(segs)}


def _pruned_excused(directory: str, by_segment: Dict[str, List[Dict[str, Any]]], names_now: Sequence[str], receipts: Optional[str],
                    backup: Optional[str] = None, why: Optional[List[str]] = None) -> Dict[str, str]:
    """Sealed segments that are absent because the recorder's retention pruned them, mapped to why they are excused. A MISSING sealed segment is excused ONLY when
    ALL hold: it is older than the oldest segment still present; the receipts verify and hold a receipt for it whose last_chain equals the sealed (closed) row's;
    and the chain of absent days runs unbroken from the sealed history into the oldest present segment (each absent day's sealed chain_start is the previous absent
    day's last chain, and the oldest present segment's chain_start equals the last absent day's receipted last_chain). ``pruned.jsonl`` is NOT consulted: the
    recorder writes it, so it cannot vouch for itself. Returns {} (nothing excused) otherwise."""
    if not receipts or not os.path.isfile(receipts) or not names_now:
        return {}
    try:
        if not EC.verify(receipts).get("ok", False):
            return {}
        got = {r["segment"]: r for r in EC.rows_of(EC.read_lines(receipts)[0]) if isinstance(r, dict) and "segment" in r}
    except (OSError, ValueError, KeyError, TypeError, EC.ChainDesync):
        return {}
    oldest = names_now[0]
    absent = sorted(n for n in by_segment if n not in set(names_now) and n < oldest)
    if not absent:
        return {}
    try:
        oldest_start = describe(directory, oldest, closed=False)["chain_start"]
    except sqlite3.DatabaseError:
        return {}
    prev_chain: Optional[str] = None
    for name in absent:
        descs = by_segment[name]
        closed = [d for d in descs if d.get("closed")]
        r = got.get(name)
        if not closed or r is None or r.get("last_chain") != closed[-1]["last_chain"] or r.get("records") != closed[-1]["records"]:
            return {}
        if prev_chain is not None and descs[0].get("chain_start") != prev_chain:
            return {}
        prev_chain = closed[-1]["last_chain"]
    if prev_chain != oldest_start:
        return {}
    if backup is not None:                      # the receipt is only a claim about a copy: when a backup directory is named, the copy itself must be there and hash as receipted
        for name in absent:
            path = os.path.join(backup, name)
            if not os.path.isfile(path) or _sha256(path) != got[name].get("sha256"):
                if why is not None:
                    why.append(f"{name}: the receipt says it was backed up but the copy in {backup!r} is missing or its sha256 differs from the receipt: NOT excused")
                return {}
    return {n: "pruned by retention after a verified backup receipt" for n in absent}


def check(directory: str, manifest: str, *, deep: bool = False, receipts: Optional[str] = None, now_ms: Optional[int] = None,
          backup: Optional[str] = None) -> Dict[str, Any]:
    """Compare the directory with every sealed row. ``deep`` also recomputes every record's chain from its payload (catches an edit INSIDE an open segment, which the
    cheap checks cannot; slow on a large day). Returns {'ok', 'problems', 'rows_checked'} and, when a day was excused, 'notes'.
    ``backup`` (the backup directory): an excused day's copy must exist there with the receipted sha256. Without it the excuse rests on the receipts ALONE (a receipt is a
    claim, not the copy) and the result says so: run ``restore-check`` alongside."""
    problems: List[str] = []
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if not os.path.isfile(manifest):
        return {"ok": False, "problems": ["no manifest: nothing outside the recorder remembers how far it got"], "rows_checked": 0}
    ver = EC.verify(manifest)
    if not ver.get("ok", False):
        problems.append(f"the manifest's own chain does not verify: {ver.get('problems') or ver.get('state') or ver}")
    lines, _q = EC.read_lines(manifest)
    rows = EC.rows_of(lines)
    names_now = segment_names(directory)
    present = set(names_now)
    by_segment = _by_segment(rows)                                       # EVERY description of each segment, oldest first
    future = future_problems(names_now, now)
    problems += future
    names_now = [n for n in names_now if n[3:11] <= _today(now)]         # a fabricated future day is reported, and never decides which segment is the newest
    present = set(names_now)
    backup_why: List[str] = []
    excused = _pruned_excused(directory, by_segment, names_now, receipts, backup=backup, why=backup_why)
    problems += backup_why
    for name, descs in sorted(by_segment.items()):
        if name not in present:
            if name in excused:
                continue
            why = ""
            if names_now and name < names_now[0]:
                why = (" older than the oldest segment present: it looks like retention pruning, but there is NO verified backup receipt that matches it and chains into "
                       "the oldest remaining day, so this evidence is NOT backed up and has vanished: back up BEFORE retention (RUNBOOKS), then investigate")
            problems.append(f"{name}: MISSING (it was sealed with {max(d['records'] for d in descs)} records){why}")
            continue
        try:
            con = _ro(os.path.join(directory, name))
            try:
                n = con.execute("SELECT COUNT(*) FROM log").fetchone()[0]
                for d in descs:                                          # each earlier seal must still hold, not only the latest
                    at = con.execute("SELECT chain FROM log WHERE id=?", (d["last_id"],)).fetchone() if d["last_id"] is not None else None
                    if d["last_id"] is not None and at is None:
                        problems.append(f"{name}: TRUNCATED (sealed id {d['last_id']} is gone; {n} records remain)")
                    elif at is not None and bytes(at[0]).hex() != d["last_chain"]:
                        problems.append(f"{name}: ALTERED (the chain value at sealed id {d['last_id']} changed)")
            finally:
                con.close()
        except sqlite3.DatabaseError as exc:
            problems.append(f"{name}: unreadable ({type(exc).__name__})")
            continue
        top = max(d["records"] for d in descs)
        if n < top:
            problems.append(f"{name}: TRUNCATED ({n} records, {top} were sealed)")
        closed = [d for d in descs if d.get("closed")]
        if closed:
            if n != closed[-1]["records"]:
                problems.append(f"{name}: ALTERED (a closed day now holds {n} records, was sealed with {closed[-1]['records']})")
            if _sha256(os.path.join(directory, name)) != closed[-1]["sha256"]:
                problems.append(f"{name}: ALTERED (a closed day's file hash changed)")
        if deep:
            try:
                problems += [f"{name}: {p}" for p in recompute_chain(os.path.join(directory, name))]
            except sqlite3.DatabaseError as exc:                    # a bit-flipped segment is a PROBLEM to report, not an exception to crash the caller (reproduced by the lifecycle worker)
                problems.append(f"{name}: unreadable on deep check ({type(exc).__name__})")
    # the segments must still form ONE chain, and nothing unsealed may sit before the sealed history (a backdated or fabricated day)
    if by_segment:
        newest_sealed = max(by_segment)
        for name in names_now:
            if name not in by_segment and name < newest_sealed:
                problems.append(f"{name}: UNSEALED segment older than the newest sealed one ({newest_sealed}): a backdated or fabricated day")
        descs: List[Dict[str, Any]] = []
        for name in names_now:
            try:
                descs.append(describe(directory, name, closed=False))
            except sqlite3.DatabaseError:
                continue                                                   # already reported as unreadable above when it was sealed
        problems += continuity_problems(directory, descs, {k: v[0] for k, v in by_segment.items()}, excused=bool(excused))
    out: Dict[str, Any] = {"ok": not problems, "problems": problems, "rows_checked": len(rows)}
    if excused:
        out["excused"] = sorted(excused)
        out["notes"] = ([f"{len(excused)} pruned day(s) excused: each backup copy was found in {backup!r} with the receipted sha256 (restore-check still recomputes the chains)"] if backup is not None else
                        [f"{len(excused)} pruned day(s) excused on the RECEIPTS ALONE: no backup directory was given, so no copy was looked at. Receipts are a claim, not the data: "
                         "run restore-check --backup DIR (or pass --backup to check) alongside this result"])
    return out


def _by_segment(rows: Sequence[Any]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        if isinstance(row, dict):
            for seg in row.get("segments", []):
                out.setdefault(seg["segment"], []).append(seg)
    return out


def _baseline(manifest: str) -> Dict[str, Dict[str, Any]]:
    """Each sealed segment's FIRST description (its original chain_start)."""
    if not os.path.isfile(manifest):
        return {}
    return {k: v[0] for k, v in _by_segment(EC.rows_of(EC.read_lines(manifest)[0])).items()}


def sealed_closed(manifest: str) -> Dict[str, Dict[str, Any]]:
    """The latest CLOSED description of every sealed segment."""
    out: Dict[str, Dict[str, Any]] = {}
    if os.path.isfile(manifest):
        for name, descs in _by_segment(EC.rows_of(EC.read_lines(manifest)[0])).items():
            closed = [d for d in descs if d.get("closed")]
            if closed:
                out[name] = closed[-1]
    return out


def recompute_chain(path: str) -> List[str]:
    """Problems found recomputing every record's chain from its payload (independent of any manifest)."""
    problems: List[str] = []
    con = _ro(path)
    try:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            problems.append("SQLite integrity_check failed")
        start = con.execute("SELECT value FROM meta WHERE key='chain_start'").fetchone()
        prev = start[0] if start else MS.GENESIS
        expect = None
        for row in con.execute("SELECT id, kind, wall_ms, mono_s, category, conn_id, topic, payload, chain FROM log ORDER BY id"):
            if expect is not None and row[0] != expect:
                problems.append(f"id gap: expected {expect}, found {row[0]}")
            expect = row[0] + 1
            try:
                text = MS._decompress(row[7]).decode("utf-8")
            except Exception:                               # noqa: BLE001
                problems.append(f"record {row[0]}: payload unreadable")
                continue
            want = MS._chain(prev, row[1], row[2], row[3], row[4], row[5], row[6], hashlib.sha256(text.encode("utf-8")).hexdigest())
            got = bytes(row[8]).hex()
            if want != got:
                problems.append(f"record {row[0]}: chain mismatch")
            prev = got
    finally:
        con.close()
    return problems


def online_copy(src: str, dst: str) -> None:
    """SQLite's online backup: includes records still in the WAL (a plain file copy of the main .db lost 10 of 20 records while the writer was open)."""
    a = _ro(src)
    try:
        b = sqlite3.connect(dst)
        try:
            a.backup(b)
        finally:
            b.close()
    finally:
        a.close()


def backup(directory: str, dest: str, receipts: str, *, manifest: str, now_ms: Optional[int] = None,
           copy_fn: Callable[[str, str], None] = online_copy) -> Dict[str, Any]:
    """Copy every SEALED, CLOSED day to ``dest`` (never overwriting), verify the copy, then write a chained receipt. The directory is first checked against the
    seal ``manifest`` and the whole backup is REFUSED if it fails (reproduced: a truncated sealed day was copied and receipted, and restore-check passed); a copy must
    also equal the sealed record count, last id and last chain. A closed day the manifest does not hold as closed is skipped and the result is not ok: seal first.
    The injected ``copy_fn`` lets a test (or an object-store uploader) replace the transfer; verification always re-reads the copy."""
    now = int(time.time() * 1000) if now_ms is None else min(int(now_ms), int(time.time() * 1000))
    if _inside(dest, directory):
        raise SealError("the backup destination is the recorder's own directory: a copy beside the original is lost with it")
    if _inside(receipts, directory) or _inside(manifest, directory):
        raise SealError("the receipts and the manifest must not live at or under the recorder's directory: they are the outside memory")
    chk = check(directory, manifest, receipts=receipts, now_ms=now)
    if not chk["ok"]:
        raise SealError(f"refusing to back up: the directory fails its seal manifest (a backup would launder the damage): {chk['problems'][:3]}")
    sealed = sealed_closed(manifest)
    os.makedirs(dest, exist_ok=True)
    today = _today(now - CLOSE_MARGIN_MS)
    done: List[Dict[str, Any]] = []
    skipped: List[str] = []
    unsealed = 0
    have = set()
    if os.path.isfile(receipts):
        for r in EC.rows_of(EC.read_lines(receipts)[0]):
            have.add(r.get("segment"))
    for name in segment_names(directory):
        if name[3:11] >= today:
            skipped.append(f"{name}: today's segment is still being written")
            continue
        if name in have:
            skipped.append(f"{name}: already backed up")
            continue
        src, dst = os.path.join(directory, name), os.path.join(dest, name)
        want = sealed.get(name)
        if want is None:
            unsealed += 1
            skipped.append(f"{name}: NOT SEALED as a closed day (run seal first); not backed up")
            continue
        if os.path.exists(dst):
            raise SealError(f"{dst} already exists and has no receipt: refusing to overwrite or trust it")
        copy_fn(src, dst)
        d_src = describe(directory, name, closed=False)
        bad = []
        d_dst: Dict[str, Any] = {}
        try:
            d_dst = describe(dest, name, closed=True)
            # The copy's BYTES legitimately differ from the source's (an online backup rewrites the file); what must agree is the CONTENT.
            for k in ("records", "first_id", "last_id", "last_chain"):
                if d_src[k] != d_dst[k]:
                    bad.append(f"the copy's {k} ({d_dst[k]!r}) differs from the source's ({d_src[k]!r}): records were lost in the copy")
            for k in ("records", "last_id", "last_chain"):
                if d_dst[k] != want[k]:
                    bad.append(f"the copy's {k} ({d_dst[k]!r}) differs from the sealed value ({want[k]!r})")
            bad += [f"copy: {p}" for p in recompute_chain(dst)]
        except sqlite3.DatabaseError as exc:                 # a copy SQLite cannot even open is the clearest failure of all
            bad.append(f"copy is not a readable database ({exc})")
        if bad:
            if os.path.lexists(dst):
                _aside(dst, ".REJECTED")
            raise SealError(f"{name}: the copy failed verification and was set aside: {bad[:3]}")
        EC.append(receipts, {"at_ms": now, "segment": name, "sha256": d_dst["sha256"], "records": d_dst["records"], "last_id": d_dst["last_id"],
                             "last_chain": d_dst["last_chain"]})
        done.append({"segment": name, "records": d_dst["records"]})
    return {"ok": unsealed == 0, "backed_up": done, "skipped": skipped}


def backed_up(directory: str, name: str, receipts: str, backup_dir: str) -> bool:
    """True only when ``name`` has a verified off-volume copy: the receipts chain verifies, holds a receipt for it, the copy exists with the receipted sha256, and the receipt's last_chain
    equals the local segment's. Anything unreadable is False (default-deny); used as the recorder's emergency-prune predicate."""
    try:
        if not os.path.isfile(receipts) or not EC.verify(receipts).get("ok", False):
            return False
        rows = [r for r in EC.rows_of(EC.read_lines(receipts)[0]) if isinstance(r, dict) and r.get("segment") == name]
        if not rows:
            return False
        r = rows[-1]
        copy = os.path.join(backup_dir, name)
        if not os.path.isfile(copy) or _sha256(copy) != r.get("sha256"):
            return False
        return describe(directory, name, closed=False)["last_chain"] == r.get("last_chain")
    except (OSError, ValueError, KeyError, TypeError, sqlite3.DatabaseError, EC.ChainDesync):
        return False


def restore_check(backup_dir: str, receipts: str, manifest: str) -> Dict[str, Any]:
    """Prove the backup restores: every receipted copy exists, hashes as receipted and recomputes its own chain from the payloads, AND every closed day the seal
    ``manifest`` holds has a receipt whose record count, last id and last chain equal the sealed ones (a receipt of a truncated or missing day proves nothing)."""
    problems: List[str] = []
    if not os.path.isfile(receipts):
        return {"ok": False, "problems": ["no receipts"], "checked": 0}
    ver = EC.verify(receipts)
    if not ver.get("ok", False):
        problems.append("the receipts' own chain does not verify")
    if not os.path.isfile(manifest):
        problems.append("no seal manifest: nothing says which days the backup must hold")
    else:
        mv = EC.verify(manifest)
        if not mv.get("ok", False):
            problems.append("the seal manifest's own chain does not verify")
    receipt_rows = [r for r in EC.rows_of(EC.read_lines(receipts)[0]) if isinstance(r, dict) and "segment" in r]
    latest = {r["segment"]: r for r in receipt_rows}
    for name, want in sorted(sealed_closed(manifest).items()):
        got = latest.get(name)
        if got is None:
            problems.append(f"{name}: sealed as a closed day but has NO receipt (never backed up)")
        else:
            for k in ("records", "last_id", "last_chain"):
                if got.get(k) != want.get(k):
                    problems.append(f"{name}: the receipt's {k} ({got.get(k)!r}) differs from the sealed value ({want.get(k)!r})")
    n = 0
    for r in receipt_rows:
        name = r["segment"]; path = os.path.join(backup_dir, name)
        n += 1
        if not os.path.isfile(path):
            problems.append(f"{name}: receipted but missing from the backup")
            continue
        if _sha256(path) != r["sha256"]:
            problems.append(f"{name}: hash differs from its receipt")
            continue
        problems += [f"{name}: {p}" for p in recompute_chain(path)]
    return {"ok": not problems, "problems": problems, "checked": n}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seal"); s.add_argument("--dir", required=True); s.add_argument("--manifest", required=True)
    s.add_argument("--accept-problems", action="store_true", help="re-baseline over problems you have investigated (otherwise sealing refuses)")
    s.add_argument("--receipts", default=None, help="backup receipts: excuse days the recorder's retention pruned AFTER they were backed up")
    s.add_argument("--backup", default=None, help="backup directory: an excused day's copy must exist there with the receipted sha256")
    c = sub.add_parser("check"); c.add_argument("--dir", required=True); c.add_argument("--manifest", required=True)
    c.add_argument("--receipts", default=None, help="backup receipts: excuse days the recorder's retention pruned AFTER they were backed up")
    c.add_argument("--backup", default=None, help="backup directory: an excused day's copy must exist there with the receipted sha256 (without it the excuse is receipt-based only)")
    c.add_argument("--deep", action="store_true", help="also recompute every record's chain (slow on a large day)")
    s.add_argument("--now-ms", type=int, default=None, help="explicit clock: lets seal close the NEWEST day too (use after the recorder has stopped)")
    b = sub.add_parser("backup"); b.add_argument("--dir", required=True); b.add_argument("--to", required=True); b.add_argument("--receipts", required=True)
    b.add_argument("--manifest", required=True, help="the seal manifest the directory is checked against first")
    r = sub.add_parser("restore-check"); r.add_argument("--backup", required=True); r.add_argument("--receipts", required=True)
    r.add_argument("--manifest", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "seal":
            res = seal(a.dir, a.manifest, now_ms=a.now_ms, accept_problems=a.accept_problems, receipts=a.receipts, backup=a.backup)
        elif a.cmd == "check":
            res = check(a.dir, a.manifest, deep=a.deep, receipts=a.receipts, backup=a.backup)
        elif a.cmd == "backup":
            res = backup(a.dir, a.to, a.receipts, manifest=a.manifest)
        else:
            res = restore_check(a.backup, a.receipts, a.manifest)
    except (SealError, OSError, sqlite3.DatabaseError, EC.ChainDesync) as exc:
        print(f"md_seal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(res, indent=2, sort_keys=True, default=str))
    return 0 if res.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
