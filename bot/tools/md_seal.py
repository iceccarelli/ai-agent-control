"""Seal, back up and restore-check the recorder's market-data log (``md-YYYYMMDD.db``), so a lost tail or a lost day is DETECTED.

Why. The log is hash-chained inside each segment, but nothing outside the segment remembers how far it got: a probe deleted ids 7-10 of 10 and
``MarketStore.verify()`` returned no problem and a reopened writer resumed at id 7. A year of evidence that can lose its newest day silently, on an
ephemeral volume with no copy, is not an asset. This tool adds the missing outside memory and a tested copy. It reads segments READ-ONLY and never
writes into the recorder's directory (the manifest and receipts live elsewhere, ideally on a different volume).

    python3 tools/md_seal.py seal    --dir MD_DIR --manifest M.jsonl                       # record how far each segment got (chained; append-only)
    python3 tools/md_seal.py check   --dir MD_DIR --manifest M.jsonl                       # is anything shorter, missing or altered since?
    python3 tools/md_seal.py backup  --dir MD_DIR --to OTHER_VOLUME_DIR --receipts R.jsonl # copy SEALED days (before today UTC), verify each copy
    python3 tools/md_seal.py restore-check --backup DIR --receipts R.jsonl                  # open every copy, recompute its chain, compare the receipt

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


#: A day is treated as closed only an hour after it ended: the recorder closes yesterday's segment on its first append of the new day, so right after
#: midnight the writer may still hold it (a backup of an open WAL segment lost records at exactly this point).
CLOSE_MARGIN_MS = 3_600_000


def seal(directory: str, manifest: str, *, now_ms: Optional[int] = None, accept_problems: bool = False) -> Dict[str, Any]:
    """Append one manifest row describing every segment now. Segments whose day ended over an hour ago are 'closed' and hash-pinned.
    REFUSES when ``check`` already finds a problem: sealing over a truncation would launder it into the new baseline (reproduced). ``accept_problems`` is
    the owner's explicit decision to re-baseline after investigating."""
    now = int(time.time() * 1000) if now_ms is None else min(int(now_ms), int(time.time() * 1000))
    if os.path.abspath(os.path.dirname(os.path.abspath(manifest))) == os.path.abspath(directory):
        raise SealError("the manifest must not live in the recorder's own directory: it is the outside memory")
    today = _today(now - CLOSE_MARGIN_MS)
    segs = [describe(directory, n, closed=n[3:11] < today) for n in segment_names(directory)]
    if not segs:
        raise SealError("no segments to seal")
    if os.path.isfile(manifest) and not accept_problems:
        prior = check(directory, manifest)
        if not prior["ok"]:
            raise SealError(f"refusing to seal over existing problems (that would make them the new baseline): {prior['problems'][:3]}")
    n, head = EC.append(manifest, {"at_ms": now, "segments": segs})
    return {"manifest_rows": n, "head": head, "segments": len(segs)}


def check(directory: str, manifest: str, *, deep: bool = False) -> Dict[str, Any]:
    """Compare the directory with every sealed row. ``deep`` also recomputes every record's chain from its payload (catches an edit INSIDE an open segment, which the
    cheap checks cannot; slow on a large day). Returns {'ok', 'problems', 'rows_checked'}."""
    problems: List[str] = []
    if not os.path.isfile(manifest):
        return {"ok": False, "problems": ["no manifest: nothing outside the recorder remembers how far it got"], "rows_checked": 0}
    ver = EC.verify(manifest)
    if not ver.get("ok", False):
        problems.append(f"the manifest's own chain does not verify: {ver.get('problems') or ver.get('state') or ver}")
    lines, _q = EC.read_lines(manifest)
    rows = EC.rows_of(lines)
    present = set(segment_names(directory))
    by_segment: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        for seg in row.get("segments", []):
            by_segment.setdefault(seg["segment"], []).append(seg)       # EVERY description of the segment, oldest first
    for name, descs in sorted(by_segment.items()):
        if name not in present:
            problems.append(f"{name}: MISSING (it was sealed with {max(d['records'] for d in descs)} records)")
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
            problems += [f"{name}: {p}" for p in recompute_chain(os.path.join(directory, name))]
    return {"ok": not problems, "problems": problems, "rows_checked": len(rows)}


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


def backup(directory: str, dest: str, receipts: str, *, now_ms: Optional[int] = None,
           copy_fn: Callable[[str, str], None] = online_copy) -> Dict[str, Any]:
    """Copy every CLOSED day to ``dest`` (never overwriting), verify the copy, then write a chained receipt. The injected ``copy_fn`` lets a test (or an
    object-store uploader) replace the transfer; verification always re-reads the copy."""
    now = int(time.time() * 1000) if now_ms is None else min(int(now_ms), int(time.time() * 1000))
    if os.path.realpath(dest) == os.path.realpath(directory) or os.path.realpath(dest).startswith(os.path.realpath(directory) + os.sep):
        raise SealError("the backup destination is the recorder's own directory: a copy beside the original is lost with it")
    os.makedirs(dest, exist_ok=True)
    today = _today(now - CLOSE_MARGIN_MS)
    done: List[Dict[str, Any]] = []
    skipped: List[str] = []
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
            bad += [f"copy: {p}" for p in recompute_chain(dst)]
        except sqlite3.DatabaseError as exc:                 # a copy SQLite cannot even open is the clearest failure of all
            bad.append(f"copy is not a readable database ({exc})")
        if bad:
            os.rename(dst, dst + ".REJECTED")
            raise SealError(f"{name}: the copy failed verification and was set aside: {bad[:3]}")
        EC.append(receipts, {"at_ms": now, "segment": name, "sha256": d_dst["sha256"], "records": d_dst["records"], "last_id": d_dst["last_id"],
                             "last_chain": d_dst["last_chain"]})
        done.append({"segment": name, "records": d_dst["records"]})
    return {"backed_up": done, "skipped": skipped}


def restore_check(backup_dir: str, receipts: str) -> Dict[str, Any]:
    """Prove the backup restores: every receipted copy exists, hashes as receipted and recomputes its own chain from the payloads."""
    problems: List[str] = []
    if not os.path.isfile(receipts):
        return {"ok": False, "problems": ["no receipts"], "checked": 0}
    ver = EC.verify(receipts)
    if not ver.get("ok", False):
        problems.append("the receipts' own chain does not verify")
    n = 0
    for r in EC.rows_of(EC.read_lines(receipts)[0]):
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
    c = sub.add_parser("check"); c.add_argument("--dir", required=True); c.add_argument("--manifest", required=True)
    c.add_argument("--deep", action="store_true", help="also recompute every record's chain (slow on a large day)")
    b = sub.add_parser("backup"); b.add_argument("--dir", required=True); b.add_argument("--to", required=True); b.add_argument("--receipts", required=True)
    r = sub.add_parser("restore-check"); r.add_argument("--backup", required=True); r.add_argument("--receipts", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "seal":
            res = seal(a.dir, a.manifest, accept_problems=a.accept_problems)
        elif a.cmd == "check":
            res = check(a.dir, a.manifest, deep=a.deep)
        elif a.cmd == "backup":
            res = backup(a.dir, a.to, a.receipts)
        else:
            res = restore_check(a.backup, a.receipts)
    except (SealError, OSError, sqlite3.DatabaseError, EC.ChainDesync) as exc:
        print(f"md_seal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(res, indent=2, sort_keys=True, default=str))
    return 0 if res.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
