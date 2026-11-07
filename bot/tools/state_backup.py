#!/usr/bin/env python3
"""Online backup, verification and DISPOSABLE restore of a SQLite state database.

    python3 tools/state_backup.py backup  --db  PATH --out-dir DIR
    python3 tools/state_backup.py verify  --backup FILE
    python3 tools/state_backup.py restore --backup FILE --into NEWDIR
    python3 tools/state_backup.py promote --restored FILE --live-db PATH --confirm-owner      # machine STOPPED; keeps the old file

* ``backup`` opens the source READ-ONLY (``mode=ro``, percent-encoded URI) and copies a consistent
  snapshot with SQLite's online backup API (safe while the writer runs). The source database file
  is never written. One caveat that is SQLite's, not this tool's: opening a WAL-mode database
  read-only may create its ``-wal``/``-shm`` side files in the source directory when no writer has
  them open; the main database file's bytes are unchanged. It refuses an out-dir that is the source's own
  directory (a backup beside the ledger on the same volume is not a backup). It checks the copy
  (``PRAGMA integrity_check``), records its SHA-256 and per-table row COUNTS (never contents) in
  a manifest.
* ``verify`` recomputes the hash and counts and re-runs the integrity check.
* ``restore`` writes ONLY into a new or empty directory, refuses anything that looks like a
  live state directory or that already holds a database, and re-verifies what it wrote. It is
  the rehearsal that proves a backup can be restored; it never replaces a live database. ``promote`` is the separate, owner-confirmed, cold-only step that does.

ENCRYPTION is not done here (the standard library has no authenticated encryption): encrypt the
backup file before it leaves the machine, e.g. ``age -r <recipient> -o x.db.age x.db`` or
``openssl enc -aes-256-cbc -pbkdf2 -in x.db -out x.db.enc``, and keep the manifest beside it.
No path is defaulted: this tool does not know where any production database is.
"""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import json
import os
import shutil
import sqlite3
import sys
import time
from typing import Any, Dict, Optional, Sequence

TOOL_VERSION = 2
MANIFEST_SUFFIX = ".manifest.json"


def _ro_uri(path: str) -> str:
    """A read-only SQLite URI with the path percent-encoded: a '#', '?' or '%' in a directory or file
    name must not redirect the open to a different file (an unencoded URI would)."""
    return pathlib.Path(os.path.abspath(path)).as_uri() + "?mode=ro"


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _inspect(path: str) -> Dict[str, Any]:
    con = sqlite3.connect(_ro_uri(path), uri=True)
    try:
        integrity = [r[0] for r in con.execute("PRAGMA integrity_check")]
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
        max_rowid: Dict[str, Any] = {}
        for t in tables:
            try:
                max_rowid[t] = con.execute(f'SELECT MAX(rowid) FROM "{t}"').fetchone()[0]
            except sqlite3.DatabaseError:
                max_rowid[t] = None                      # a WITHOUT ROWID table has none
        user_version = con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()
    return {"integrity": integrity, "integrity_ok": integrity == ["ok"], "tables": counts, "max_rowid": max_rowid, "user_version": user_version}


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
    src = sqlite3.connect(_ro_uri(db), uri=True)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    info = _inspect(dest)
    if not info["tables"]:
        os.remove(dest)               # a backup of nothing must never look like a backup
        raise SystemExit("refusing: the copy holds no tables - the source is empty or the wrong file was opened")
    manifest = {"tool_version": TOOL_VERSION, "created_utc": stamp, "backup_file": os.path.basename(dest),
                "source_basename": os.path.basename(db), "bytes": os.path.getsize(dest), "sha256": _sha256(dest),
                "integrity_ok": info["integrity_ok"], "tables": info["tables"],
                "max_rowid": info["max_rowid"], "user_version": info["user_version"],
                "note": "row counts, newest row ids and the schema version only; no contents are recorded; encrypt the file before it leaves the machine"}
    with open(dest + MANIFEST_SUFFIX, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    if not info["integrity_ok"]:
        raise SystemExit(f"the backup copy FAILED its integrity check: {info['integrity'][:3]}")
    return manifest


def verify(backup_file: str) -> Dict[str, Any]:
    mpath = backup_file + MANIFEST_SUFFIX
    try:
        manifest = json.load(open(mpath, encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("not an object")
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
    warnings = []
    if "max_rowid" not in manifest or "user_version" not in manifest:
        warnings.append("the manifest predates newest-row and schema-version markers (tool_version < 2): only hash, counts and integrity were checked")
    else:
        if info["max_rowid"] != manifest["max_rowid"]:
            problems.append("newest row ids differ from the manifest")
        if info["user_version"] != manifest["user_version"]:
            problems.append("schema version (PRAGMA user_version) differs from the manifest")
    return {"ok": not problems, "problems": problems, "warnings": warnings, "tables": info["tables"], "sha256": manifest.get("sha256")}


def restore(backup_file: str, into: str) -> Dict[str, Any]:
    v = verify(backup_file)
    if not v["ok"]:
        raise SystemExit(f"refusing: the backup does not verify: {v['problems']}")
    target = os.path.realpath(into)
    parts = [x.lower() for x in target.split(os.sep) if x]
    if ("state" in parts or target in ("/", "/app", "/data") or target.startswith(("/app/", "/data/"))
            or os.path.ismount(target)):
        raise SystemExit("refusing: --into looks like a live state directory, a volume or its contents; "
                         "restore only into a disposable location")
    if os.path.exists(target):
        raise SystemExit("refusing: --into already exists; a restore creates its own new directory so it can never land in "
                         "a directory something else uses")
    os.makedirs(target)
    out = os.path.join(target, "restored-state.db")      # never the live file name
    shutil.copyfile(backup_file, out)
    shutil.copyfile(backup_file + MANIFEST_SUFFIX, out + MANIFEST_SUFFIX)       # so `promote` can verify exactly what it is asked to install
    got = _inspect(out)
    ok = got["integrity_ok"] and got["tables"] == v["tables"] and _sha256(out) == v["sha256"]
    return {"ok": ok, "restored_to": out, "tables": got["tables"],
            "note": "a rehearsal: this is a disposable copy; nothing on the live volume was touched"}


def promote(restored: str, live_db: str, *, confirm_owner: bool, now: Optional[float] = None) -> Dict[str, Any]:
    """Install a RESTORED database as the live one. Cold only; never deletes anything.

    Refuses unless: the owner confirmed in words (``confirm_owner``); the restored file verifies against the manifest beside it; the live path's directory
    is a real directory and the live file is a regular file (or absent); no ``-wal``/``-shm`` side file exists (a writer is, or was uncleanly, there);
    and an exclusive SQLite lock on the live file can be taken right now (nothing else has it open). Then the old file is RENAMED to
    ``<live>.replaced-<UTC stamp>`` (kept, never deleted), the restored copy is written beside the live file, fsynced and renamed into place, and the
    result is verified again at the live path. A machine must be STOPPED first; this tool does not stop, start or restart anything."""
    if not confirm_owner:
        raise SystemExit("refusing: promoting a restored database onto the live path needs --confirm-owner (the owner's explicit act)")
    v = verify(restored)
    if not v["ok"]:
        raise SystemExit(f"refusing: the restored file does not verify: {v['problems']}")
    live_dir = os.path.dirname(os.path.abspath(live_db))
    if os.path.islink(live_dir) or not os.path.isdir(live_dir):
        raise SystemExit("refusing: the live database's directory must be a real directory")
    if os.path.lexists(live_db) and (os.path.islink(live_db) or not os.path.isfile(live_db)):
        raise SystemExit("refusing: the live path exists and is not a regular file")
    for side in ("-wal", "-shm", "-journal"):
        if os.path.lexists(live_db + side):
            raise SystemExit(f"refusing: {os.path.basename(live_db)}{side} exists - stop the machine and let it close the database cleanly first")
    if os.path.exists(live_db):
        try:
            con = sqlite3.connect(live_db, timeout=0.0)
            try:
                con.execute("BEGIN EXCLUSIVE")
                con.execute("ROLLBACK")
            finally:
                con.close()
        except sqlite3.OperationalError as exc:
            raise SystemExit(f"refusing: the live database is in use or unreadable ({exc}); stop the machine first")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if now is None else now))
    kept = None
    staged = os.path.join(live_dir, f".promote-{stamp}-{os.getpid()}.tmp")
    shutil.copyfile(restored, staged)
    try:
        fd = os.open(staged, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        if os.path.exists(live_db):
            kept = f"{live_db}.replaced-{stamp}"
            if os.path.lexists(kept):
                raise SystemExit(f"refusing: {os.path.basename(kept)} already exists")
            os.rename(live_db, kept)
        try:
            os.replace(staged, live_db)
        except BaseException:
            # The old file was already moved aside: put it back, or the next start would find NO database and make an empty one (reproduced by the red team).
            if kept and not os.path.lexists(live_db):
                os.rename(kept, live_db)
            raise
    except BaseException:
        if os.path.lexists(staged):
            os.remove(staged)
        raise
    got = _inspect(live_db)
    ok = got["integrity_ok"] and got["tables"] == v["tables"] and _sha256(live_db) == v["sha256"]
    return {"ok": ok, "live": live_db, "previous_kept_as": kept, "tables": got["tables"], "warnings": v.get("warnings", []),
            "note": "the previous database was renamed, not deleted; start the machine only after you have checked this result"}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup"); b.add_argument("--db", required=True); b.add_argument("--out-dir", required=True)
    v = sub.add_parser("verify"); v.add_argument("--backup", required=True)
    r = sub.add_parser("restore"); r.add_argument("--backup", required=True); r.add_argument("--into", required=True)
    pr = sub.add_parser("promote"); pr.add_argument("--restored", required=True); pr.add_argument("--live-db", required=True)
    pr.add_argument("--confirm-owner", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "promote":
        res = promote(a.restored, a.live_db, confirm_owner=a.confirm_owner)
        print(json.dumps(res, indent=2, sort_keys=True))
        return 0 if res.get("ok") else 1
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
