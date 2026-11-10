#!/usr/bin/env python3
"""READ-ONLY inventory of research data directories: what exists, how big it is, what period it covers, what is missing or suspicious. Standard library only.

It never writes inside the directories it inspects, never follows symlinks out of them, never opens a database (``*.db``, ``*.sqlite*``, ``-wal``, ``-shm``) or anything that looks like a credential
(``.env*``, ``*.key``, ``*.pem``), and never prints file contents: it reports names, sizes, modification times and (with ``--hash``) sha256 of ordinary data files only. Output is JSON on stdout
(or ``--out FILE``, which must NOT be inside an inspected root).

    python3 tools/dataset_inventory.py --root $HOME/datasets --root $HOME/backups --root $HOME/seals [--hash] [--out /tmp/inventory.json]

COVERAGE is inferred from the names of data files (an ISO ``YYYY-MM-DD`` / ``YYYYMMDD`` token) and reported separately from modification times, because a file's mtime is when it was
written, not what period it describes. A coverage claim therefore always names its source. Gaps are days with no file between the first and last named day for a given top-level dataset.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence

SKIP_OPEN = re.compile(r"(?i)(\.db$|\.sqlite3?$|\.db-(wal|shm)$|-wal$|-shm$|(^|/)\.env|\.key$|\.pem$|\.p12$|id_rsa|secret|token)")
DAY_RE = re.compile(r"(?<!\d)(20\d{2})[-_]?(0[1-9]|1[0-2])[-_]?(0[1-9]|[12]\d|3[01])(?!\d)")
MAX_HASH_BYTES = 2 * 1024 ** 3


def _days(name: str) -> List[dt.date]:
    out = []
    for m in DAY_RE.finditer(name):
        try:
            out.append(dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    return out


def _sha256(path: str) -> Optional[str]:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def inventory(root: str, *, do_hash: bool = False) -> Dict[str, Any]:
    root = os.path.abspath(root)
    rep: Dict[str, Any] = {"root": root, "exists": os.path.isdir(root), "files": 0, "bytes": 0, "datasets": {}, "databases_present_not_opened": [], "unreadable": 0, "symlinks_skipped": 0}
    if not rep["exists"]:
        return rep
    ds: Dict[str, Dict[str, Any]] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for d in list(dirnames):
            if os.path.islink(os.path.join(dirpath, d)):
                rep["symlinks_skipped"] += 1
                dirnames.remove(d)
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root)
            if os.path.islink(p):
                rep["symlinks_skipped"] += 1
                continue
            try:
                st = os.stat(p, follow_symlinks=False)
            except OSError:
                rep["unreadable"] += 1
                continue
            top = rel.split(os.sep, 1)[0] if os.sep in rel else "(root)"
            d = ds.setdefault(top, {"files": 0, "bytes": 0, "ext": {}, "mtime_min": None, "mtime_max": None, "named_days": set(), "hashes": {}, "unhashed_sensitive": 0})
            d["files"] += 1
            d["bytes"] += st.st_size
            ext = os.path.splitext(fn)[1].lower() or "(none)"
            d["ext"][ext] = d["ext"].get(ext, 0) + 1
            d["mtime_min"] = st.st_mtime if d["mtime_min"] is None else min(d["mtime_min"], st.st_mtime)
            d["mtime_max"] = st.st_mtime if d["mtime_max"] is None else max(d["mtime_max"], st.st_mtime)
            d["named_days"].update(_days(rel))
            rep["files"] += 1
            rep["bytes"] += st.st_size
            if SKIP_OPEN.search(rel):
                d["unhashed_sensitive"] += 1
                if re.search(r"(?i)(\.db$|\.sqlite3?$|-wal$|-shm$)", rel):
                    rep["databases_present_not_opened"].append({"path": rel, "bytes": st.st_size})
                continue
            if do_hash and st.st_size <= MAX_HASH_BYTES:
                d["hashes"][rel] = _sha256(p)
    iso = lambda t: None if t is None else dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")      # noqa: E731
    for name, d in sorted(ds.items()):
        days = sorted(d.pop("named_days"))
        gaps: List[str] = []
        if len(days) >= 2:
            have, cur = set(days), days[0]
            while cur < days[-1]:
                cur += dt.timedelta(days=1)
                if cur not in have:
                    gaps.append(cur.isoformat())
        rep["datasets"][name] = {"files": d["files"], "bytes": d["bytes"], "extensions": dict(sorted(d["ext"].items())), "written_first": iso(d["mtime_min"]), "written_last": iso(d["mtime_max"]),
                                 "named_coverage": {"source": "day tokens in file names", "first": days[0].isoformat() if days else None, "last": days[-1].isoformat() if days else None,
                                                    "distinct_days": len(days), "missing_days_between": len(gaps), "missing_days_sample": gaps[:10]},
                                 "sha256_files": len(d["hashes"]), "not_opened_sensitive_or_db": d["unhashed_sensitive"], **({"sha256": d["hashes"]} if do_hash else {})}
    return rep


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="read-only inventory of research data directories")
    ap.add_argument("--root", action="append", required=True)
    ap.add_argument("--hash", action="store_true", help="sha256 of ordinary data files (never databases or credential-like names); slow on large trees")
    ap.add_argument("--out", help="write the JSON here (must not be inside an inspected root)")
    a = ap.parse_args(list(argv) if argv is not None else None)
    roots = [os.path.abspath(r) for r in a.root]
    if a.out:
        o = os.path.abspath(a.out)
        if any(o == r or o.startswith(r + os.sep) for r in roots):
            print("refused: --out must not be inside an inspected root", file=sys.stderr)
            return 2
    rep = {"schema": "dataset_inventory.v1", "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "roots": [inventory(r, do_hash=a.hash) for r in roots],
           "note": "read-only; no database was opened; coverage comes from file names, not from file contents"}
    text = json.dumps(rep, indent=1, sort_keys=True)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        os.chmod(a.out, 0o600)
        print(f"inventory written to {a.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
