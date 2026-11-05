#!/usr/bin/env python3
"""Online backup, verification and DISPOSABLE restore of a SQLite state database.

    python3 tools/state_backup.py backup  --db  PATH --out-dir DIR
    python3 tools/state_backup.py verify  --backup FILE
    python3 tools/state_backup.py restore --backup FILE --into NEWDIR

* ``backup`` opens the source READ-ONLY and copies a consistent snapshot with SQLite's online
  backup API (safe while the writer runs). The source is never written, locked for writing,
  hashed or opened without ``mode=ro``. It refuses an out-dir that is the source's own
  directory (a backup beside the ledger on the same volume is not a backup). It checks the copy
  (``PRAGMA integrity_check``), records its SHA-256 and per-table row COUNTS (never contents) in
  a manifest.
* ``verify`` recomputes the hash and counts and re-runs the integrity check.
* ``restore`` writes ONLY into a new or empty directory, refuses anything that looks like a
  live state directory or that already holds a database, and re-verifies what it wrote. It is
  the rehearsal that proves a backup can be restored; it never replaces a live database.

ENCRYPTION is not done here (the standard library has no authenticated encryption): encrypt the
backup file before it leaves the machine, e.g. ``age -r <recipient> -o x.db.age x.db`` or
``openssl enc -aes-256-cbc -pbkdf2 -in x.db -out x.db.enc``, and keep the manifest beside it.
No path is defaulted: this tool does not know where any production database is.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from typing import Any, Dict, Optional, Sequence

TOOL_VERSION = 1
MANIFEST_SUFFIX = ".manifest.json"


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _inspect(path: str) -> Dict[str, Any]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = [r[0] for r in con.execute("PRAGMA integrity_check")]
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        con.close()
    return {"integrity": integrity, "integrity_ok": integrity == ["ok"], "tables": counts}


def backup(db: str, out_dir: str, *, now: Optional[float] = None) -> Dict[str, Any]:
    src_dir = os.path.dirname(os.path.realpath(db))
    out_real = os.path.realpath(out_dir)
    if out_real == src_dir or out_real.startswith(src_dir + os.sep):
        raise SystemExit("refusing: --out-dir is the source's own directory (or inside it); "
                         "a backup next to the ledger is lost with the ledger")
    if not os.path.isfile(db):
        raise SystemExit("refusing: --db is not a file")
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if now is None else now))
    dest = os.path.join(out_dir, f"state-{stamp}.db")
    if os.path.exists(dest):
        raise SystemExit(f"refusing: {dest} already exists")
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    info = _inspect(dest)
    manifest = {"tool_version": TOOL_VERSION, "created_utc": stamp, "backup_file": os.path.basename(dest),
                "source_basename": os.path.basename(db), "bytes": os.path.getsize(dest), "sha256": _sha256(dest),
                "integrity_ok": info["integrity_ok"], "tables": info["tables"],
                "note": "row counts only; no contents are recorded; encrypt the file before it leaves the machine"}
    with open(dest + MANIFEST_SUFFIX, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    if not info["integrity_ok"]:
        raise SystemExit(f"the backup copy FAILED its integrity check: {info['integrity'][:3]}")
    return manifest


def verify(backup_file: str) -> Dict[str, Any]:
    mpath = backup_file + MANIFEST_SUFFIX
    try:
        manifest = json.load(open(mpath, encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "problems": [f"manifest {os.path.basename(mpath)} missing or unreadable"]}
    problems = []
    if not os.path.isfile(backup_file):
        return {"ok": False, "problems": ["backup file missing"]}
    if _sha256(backup_file) != manifest.get("sha256"):
        problems.append("SHA-256 differs from the manifest: the file changed after it was written")
    try:
        info = _inspect(backup_file)
    except sqlite3.DatabaseError as exc:
        return {"ok": False, "problems": problems + [f"not a readable SQLite database: {type(exc).__name__}"]}
    if not info["integrity_ok"]:
        problems.append(f"integrity_check: {info['integrity'][:3]}")
    if info["tables"] != manifest.get("tables"):
        problems.append("table row counts differ from the manifest")
    return {"ok": not problems, "problems": problems, "tables": info["tables"], "sha256": manifest.get("sha256")}


def restore(backup_file: str, into: str) -> Dict[str, Any]:
    v = verify(backup_file)
    if not v["ok"]:
        raise SystemExit(f"refusing: the backup does not verify: {v['problems']}")
    target = os.path.realpath(into)
    if os.path.basename(target) == "state" or target.rstrip(os.sep).endswith(os.sep + "state") or target == "/app/state":
        raise SystemExit("refusing: --into looks like a live state directory; restore only into a disposable one")
    if os.path.exists(target) and (not os.path.isdir(target) or os.listdir(target)):
        raise SystemExit("refusing: --into exists and is not an empty directory")
    os.makedirs(target, exist_ok=True)
    out = os.path.join(target, "trading_state.db")
    shutil.copyfile(backup_file, out)
    got = _inspect(out)
    ok = got["integrity_ok"] and got["tables"] == v["tables"] and _sha256(out) == v["sha256"]
    return {"ok": ok, "restored_to": out, "tables": got["tables"],
            "note": "a rehearsal: this is a disposable copy; nothing on the live volume was touched"}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup"); b.add_argument("--db", required=True); b.add_argument("--out-dir", required=True)
    v = sub.add_parser("verify"); v.add_argument("--backup", required=True)
    r = sub.add_parser("restore"); r.add_argument("--backup", required=True); r.add_argument("--into", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "backup":
        res = backup(a.db, a.out_dir)
    elif a.cmd == "verify":
        res = verify(a.backup)
    else:
        res = restore(a.backup, a.into)
    print(json.dumps(res, indent=2, sort_keys=True))
    return 0 if res.get("ok", res.get("integrity_ok", True)) else 1


if __name__ == "__main__":
    sys.exit(main())
