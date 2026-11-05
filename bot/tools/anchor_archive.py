#!/usr/bin/env python3
"""An append-only, hash-chained archive of evidence-chain anchors, kept OUTSIDE the recorder machine.

    python3 tools/anchor_archive.py append --history monitor_tail.jsonl --archive anchors.jsonl
    python3 tools/anchor_archive.py verify --archive anchors.jsonl [--archive-head HEX]
    python3 tools/anchor_archive.py latest --archive anchors.jsonl

Why this exists. The recorder's evidence files are hash-chained, and the monitor copies the newest chain head into
``monitor.jsonl`` every cycle. Both live on the SAME Fly volume under the same process owner, so a writer with
access to the volume can rewrite the evidence AND the copies. The remedy is to move the anchors off the machine
as they are produced. This tool is the owner-side half:

  1. Read-only fetch, from the owner's machine (no secret, no state database, nothing is changed):
         fly ssh console -a carry-drill -C "tail -n 400 /data/monitor/monitor.jsonl" > monitor_tail.jsonl
  2. ``append``: extract every ``recorder_chain_file / _n / _head`` triple, check it against what the archive already
     holds, and append the new ones. The archive is hash-chained, so editing or deleting an archived line is visible.
  3. ``latest`` prints the archive head (one 64-hex value). Store THAT value somewhere the recorder machine cannot
     reach (a password manager note, an e-mail to yourself, a private repository). It is the only thing that has to
     be kept off-machine to make the whole archive tamper-evident.
  4. ``verify --archive-head HEX`` later proves the archive is exactly what it was when you stored the value (or that
     it has only grown since, if you pass the entry count with ``--archive-entries``).

``append`` REFUSES (and writes nothing) when a file's anchor goes BACKWARDS (the evidence file was truncated) or when
the same file and row count appear with a different head (the chain was rewritten). Those are the two events the
anchors exist to catch.

What this does NOT do: it cannot prove the copy you made is off the machine, or that the archive's head you stored is
safe. It cannot see the recorder machine. It reads no credential and no protected file and places no order.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

GENESIS = "00" * 32
SCHEMA = "anchor_archive/1"


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _entry_hash(entry: Dict[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(_canon(body)).hexdigest()


def extract_anchors(history_path: str) -> List[Dict[str, Any]]:
    """Every (file, n, head) triple in a monitor history, in order, with the line's own timestamp. Malformed lines
    are skipped (a torn last line must not hide the ones before it)."""
    out: List[Dict[str, Any]] = []
    with open(history_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            f, n, h = r.get("recorder_chain_file"), r.get("recorder_chain_n"), r.get("recorder_chain_head")
            if isinstance(f, str) and f and isinstance(n, int) and not isinstance(n, bool) and n >= 1 \
                    and isinstance(h, str) and len(h) == 64:
                out.append({"file": f, "n": n, "head": h, "observed": r.get("at")})
    return out


def read_archive(path: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """``(entries, problems)``. Problems are named; the entries returned are only those BEFORE the first problem."""
    entries: List[Dict[str, Any]] = []
    problems: List[str] = []
    if not os.path.exists(path):
        return entries, problems
    prev = GENESIS
    with open(path, "rb") as fh:
        data = fh.read()
    if data and not data.endswith(b"\n"):
        problems.append("TORN_FINAL_LINE: the archive does not end in a newline")
    for i, raw in enumerate(data.split(b"\n")[:-1], 1):
        try:
            e = json.loads(raw)
        except ValueError:
            problems.append(f"LINE_{i}_NOT_JSON")
            break
        ok = (isinstance(e, dict) and e.get("schema") == SCHEMA and e.get("seq") == i and e.get("prev") == prev
              and isinstance(e.get("hash"), str) and _entry_hash(e) == e["hash"]
              and isinstance(e.get("file"), str) and isinstance(e.get("n"), int) and isinstance(e.get("head"), str))
        if not ok:
            problems.append(f"LINE_{i}_BREAKS_THE_CHAIN: edited, reordered, removed or forged")
            break
        entries.append(e)
        prev = e["hash"]
    return entries, problems


def archive_head(entries: Sequence[Dict[str, Any]]) -> str:
    return entries[-1]["hash"] if entries else GENESIS


def append(history_path: str, archive_path: str) -> Dict[str, Any]:
    """Add the history's new anchors to the archive. Writes NOTHING if any refusal condition is met."""
    entries, problems = read_archive(archive_path)
    if problems:
        return {"ok": False, "added": 0, "entries": len(entries), "archive_head": archive_head(entries), "issues": problems}
    last: Dict[str, Tuple[int, str]] = {}
    seen: Dict[Tuple[str, int], str] = {}
    for e in entries:
        seen[(e["file"], e["n"])] = e["head"]
        if e["file"] not in last or e["n"] > last[e["file"]][0]:
            last[e["file"]] = (e["n"], e["head"])
    new: List[Dict[str, Any]] = []
    issues: List[str] = []
    for a in extract_anchors(history_path):
        key = (a["file"], a["n"])
        if key in seen:
            if seen[key] != a["head"]:
                issues.append(f"ANCHOR_CONFLICT: {a['file']} row {a['n']} was archived with a different head: the chain was rewritten")
            continue
        if a["file"] in last and a["n"] < last[a["file"]][0]:
            issues.append(f"ANCHOR_REGRESSION: {a['file']} was archived at {last[a['file']][0]} rows and now reports {a['n']}: the evidence file was truncated")
            continue
        seen[key] = a["head"]
        last[a["file"]] = (a["n"], a["head"])
        new.append(a)
    if issues:
        return {"ok": False, "added": 0, "entries": len(entries), "archive_head": archive_head(entries), "issues": sorted(set(issues))}
    prev = archive_head(entries)
    out_lines: List[bytes] = []
    for k, a in enumerate(new, len(entries) + 1):
        e = {"schema": SCHEMA, "seq": k, "file": a["file"], "n": a["n"], "head": a["head"], "observed": a["observed"], "prev": prev}
        e["hash"] = _entry_hash(e)
        prev = e["hash"]
        out_lines.append(_canon(e) + b"\n")
    if out_lines:
        with open(archive_path, "ab") as fh:
            fh.write(b"".join(out_lines))
            fh.flush()
            os.fsync(fh.fileno())
    return {"ok": True, "added": len(out_lines), "entries": len(entries) + len(out_lines), "archive_head": prev, "issues": []}


def latest_per_file(entries: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for e in entries:
        if e["file"] not in out or e["n"] >= out[e["file"]]["n"]:
            out[e["file"]] = {"n": e["n"], "head": e["head"], "observed": e.get("observed")}
    return out


def anchors_by_file(entries: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for e in entries:
        out.setdefault(e["file"], []).append({"n": e["n"], "head": e["head"]})
    return out


def verify(archive_path: str, *, owner_held_head: Optional[str] = None, owner_held_entries: Optional[int] = None) -> Dict[str, Any]:
    """Is the archive internally intact, and does it still contain what the owner stored a head for?

    ``owner_held_head`` is the archive head the owner wrote down somewhere off the machine; with
    ``owner_held_entries`` (the entry count at that time) a LONGER archive is accepted as long as its first
    ``owner_held_entries`` entries hash to that head (it has only grown). Without a count, the head must equal the
    archive's current head exactly."""
    entries, problems = read_archive(archive_path)
    res: Dict[str, Any] = {"entries": len(entries), "archive_head": archive_head(entries), "problems": problems,
                           "internally_intact": not problems and bool(entries), "owner_held_head": owner_held_head,
                           "owner_held_head_matches": None}
    if owner_held_head is not None:
        if owner_held_entries is None:
            match = owner_held_head == archive_head(entries)
        else:
            match = (1 <= owner_held_entries <= len(entries) and entries[owner_held_entries - 1]["hash"] == owner_held_head)
        res["owner_held_head_matches"] = bool(match) and not problems
    res["location"] = ("EXTERNAL_ARCHIVE_HEAD_MATCHES_OWNER_HELD_VALUE" if res["owner_held_head_matches"] else
                       "ARCHIVE_PRESENT_HEAD_NOT_CONFIRMED_AGAINST_AN_OWNER_HELD_VALUE" if entries else "NO_ARCHIVE")
    res["note"] = ("this tool cannot see where the owner keeps the archive or its head; a match proves the archive is unchanged "
                   "since the owner recorded that value, not that the value is stored safely")
    return res


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("append"); a.add_argument("--history", required=True); a.add_argument("--archive", required=True)
    v = sub.add_parser("verify"); v.add_argument("--archive", required=True)
    v.add_argument("--archive-head", default=None); v.add_argument("--archive-entries", type=int, default=None)
    l = sub.add_parser("latest"); l.add_argument("--archive", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "append":
        res = append(args.history, args.archive)
        print(json.dumps(res, indent=2, sort_keys=True))
        return 0 if res["ok"] else 2
    if args.cmd == "verify":
        res = verify(args.archive, owner_held_head=args.archive_head, owner_held_entries=args.archive_entries)
        print(json.dumps(res, indent=2, sort_keys=True))
        return 0 if res["internally_intact"] and res["owner_held_head_matches"] is not False else 2
    entries, problems = read_archive(args.archive)
    print(json.dumps({"archive_head": archive_head(entries), "entries": len(entries), "problems": problems,
                      "latest_per_file": latest_per_file(entries),
                      "store_this_head_somewhere_the_recorder_machine_cannot_reach": archive_head(entries)}, indent=2, sort_keys=True))
    return 0 if not problems else 2


if __name__ == "__main__":
    sys.exit(main())
