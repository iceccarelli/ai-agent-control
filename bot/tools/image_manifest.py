#!/usr/bin/env python3
"""Does the image that is RUNNING contain this repository's recorder implementation? A read-only, owner-side check.

Merging a pull request changes the repository. It does not change what a Fly machine is running: that changes only when
an image is built and deployed, and this change does not do that. This tool answers the question from the files, not from a
tag or a commit message, so it works on an image built before this tool existed.

    # 1. on your machine, in a checkout of the commit you want to compare (reads only the repository):
    python3 bot/tools/image_manifest.py expected > expected.txt
    # 2. print the one read-only command to run against the machine, and run it yourself:
    python3 bot/tools/image_manifest.py command
    #       (it is `fly ssh console -a carry-drill -C "sh -c 'cd /app && sha256sum <files>'"` - it reads, it changes nothing)
    #       save its output as actual.txt
    # 3. compare:
    python3 bot/tools/image_manifest.py compare --expected expected.txt --actual actual.txt

``expected`` and the machine's ``sha256sum`` print the SAME format, so the machine needs no tool of ours. A file the machine
does not have is reported MISSING_ON_MACHINE (an image from before the recorder existed lacks several).

This tool never calls Fly, never reads a credential, a state database, a ``runs/`` file or any evidence file, and it cannot
tell you which commit an image was built from (the drill image carries no commit label); it tells you whether the FILES that
matter are byte-identical. The image digest the platform reports (``fly image show -a carry-drill``) is a separate fact:
record it beside this result.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)

#: Paths relative to the application directory (``bot/`` in the repository, ``/app`` in the image). Every one is COPYed by
#: Dockerfile.drill (a test pins that), so a mismatch is about content, never about a file the image was never meant to hold.
#: The CRITICAL files are the ones whose bytes ARE the recorder implementation.
CRITICAL = ("tools/market_recorder.py", "tools/runner_monitor.py", "tools/runner_supervisor.py", "md_feed.py", "md_capacity.py",
            "evidence_chain.py", "capacity_schema.py")
SUPPORTING = ("md_engine.py", "md_state.py", "md_store.py", "md_economics.py", "evidence_stats.py", "funding_eval.py",
              "recorder_reliability.py", "trade_economics.py", "opportunity_engine.py", "tools/capacity_summary.py",
              "tools/profitability_report.py", "tools/anchor_archive.py")
FILES = CRITICAL + SUPPORTING

_LINE = re.compile(r"^([0-9a-f]{64})\s+\*?(\S+)\s*$")
_MISSING = re.compile(r"sha256sum:\s+(\S+?):\s+No such file or directory")


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def expected_listing(root: str = BOT) -> str:
    lines = []
    for rel in FILES:
        p = os.path.join(root, rel)
        if not os.path.isfile(p):
            raise SystemExit(f"the repository has no {rel}: run this from a checkout that contains the recorder")
        lines.append(f"{_sha(p)}  {rel}")
    return "\n".join(lines) + "\n"


def machine_command(app: str = "carry-drill") -> str:
    files = " ".join(FILES)
    return f"fly ssh console -a {app} -C \"sh -c 'cd /app && sha256sum {files} 2>&1'\""


def parse(text: str) -> Tuple[Dict[str, str], List[str]]:
    """``({path: sha256}, [paths reported missing])``. Any other line (a banner, a warning) is ignored."""
    have: Dict[str, str] = {}
    missing: List[str] = []
    for ln in text.splitlines():
        m = _LINE.match(ln.strip())
        if m:
            have[m.group(2)] = m.group(1)
            continue
        m = _MISSING.search(ln)
        if m:
            missing.append(m.group(1))
    return have, missing


def compare(expected_text: str, actual_text: str) -> Dict[str, Any]:
    want, _ = parse(expected_text)
    got, missing = parse(actual_text)
    rows: List[Dict[str, str]] = []
    for rel in FILES:
        if rel not in want:
            rows.append({"file": rel, "status": "NOT_IN_EXPECTED_LISTING"})
        elif rel in got:
            rows.append({"file": rel, "status": "MATCH" if got[rel] == want[rel] else "DIFFERENT"})
        elif rel in missing:
            rows.append({"file": rel, "status": "MISSING_ON_MACHINE"})
        else:
            rows.append({"file": rel, "status": "NOT_REPORTED_BY_MACHINE"})
    status = {r["file"]: r["status"] for r in rows}
    critical_ok = all(status.get(f) == "MATCH" for f in CRITICAL)
    all_ok = all(r["status"] == "MATCH" for r in rows)
    if all_ok:
        verdict = "IDENTICAL_TO_EXPECTED"
    elif critical_ok:
        verdict = "RECORDER_FILES_IDENTICAL_SUPPORTING_FILES_DIFFER"
    elif not got and not missing:
        verdict = "NOTHING_REPORTED_BY_MACHINE"
    else:
        verdict = "DOES_NOT_CONTAIN_THIS_RECORDER_IMPLEMENTATION"
    return {"verdict": verdict, "rows": rows,
            "contains_this_recorder_implementation": critical_ok,
            "differing": [r["file"] for r in rows if r["status"] != "MATCH"],
            "note": "byte-for-byte file comparison; it does not identify the commit or the image digest"}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("expected"); e.add_argument("--root", default=BOT)
    c = sub.add_parser("command"); c.add_argument("--app", default="carry-drill")
    cp = sub.add_parser("compare"); cp.add_argument("--expected", required=True); cp.add_argument("--actual", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "expected":
        sys.stdout.write(expected_listing(a.root))
        return 0
    if a.cmd == "command":
        print(machine_command(a.app))
        return 0
    res = compare(open(a.expected, encoding="utf-8").read(), open(a.actual, encoding="utf-8").read())
    print(json.dumps(res, indent=2, sort_keys=True))
    return 0 if res["verdict"] in ("IDENTICAL_TO_EXPECTED", "RECORDER_FILES_IDENTICAL_SUPPORTING_FILES_DIFFER") else 1


if __name__ == "__main__":
    sys.exit(main())
