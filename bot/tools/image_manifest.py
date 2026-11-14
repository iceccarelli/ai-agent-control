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
import ast
import hashlib
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)

#: Paths are relative to the application directory (``bot/`` in the repository, ``/app`` in the image). The RECORDER FILES are the
#: static import closure of the three recorder-side entry points, computed from the repository at ``expected`` time, so a parser, book or
#: store module the recorder imports is compared too (an earlier version compared a hand-picked 19 files and could say "contains the
#: recorder" while the book code differed). The REPORT TOOLS are listed after a marker and only the owner's analysis uses them.
ENTRY_POINTS = ("tools/market_recorder.py", "tools/runner_monitor.py", "tools/runner_supervisor.py", "tools/carry_preflight.py")     # carry_preflight is spawned by path (not imported) by runner_monitor every cycle
REPORT_TOOLS = ("tools/profitability_report.py", "tools/anchor_archive.py", "tools/capacity_summary.py")
MARKER = "# report-tools (not part of the recorder)"

_LINE = re.compile(r"^([0-9a-f]{64})\s+\*?(\S+)\s*$")
_MISSING = re.compile(r"sha256sum:\s+(\S+?):\s+No such file or directory")


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _imports(path: str) -> List[str]:
    """Every dotted module name the file imports, with each parent package and each ``from pkg import name`` target as a candidate
    (``from signals import x`` -> ``signals`` and ``signals.x``, which may be a module or only an attribute: the caller keeps what
    is a file)."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    out: List[str] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                parts = a.name.split(".")
                out += [".".join(parts[:i]) for i in range(1, len(parts) + 1)]
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            parts = n.module.split(".")
            out += [".".join(parts[:i]) for i in range(1, len(parts) + 1)]
            out += [f"{n.module}.{a.name}" for a in n.names if a.name != "*"]
    return out


def _candidates(mod: str) -> List[str]:
    rel = mod.replace(".", "/")
    return [f"{rel}.py", f"{rel}/__init__.py", f"tools/{rel}.py"]


def closure(root: str = BOT, entries: Sequence[str] = ENTRY_POINTS) -> List[str]:
    """The files of ``root`` the entry points import, transitively (a module is a file of ``root`` or ``root/tools``, a package is its
    ``__init__.py`` and the submodules imported from it (and their siblings in the same package, no further); anything else is the standard library or a third party and is not ours to
    compare). LAZY imports count: a function-level ``import x`` is a file the process may load, so a change to it is a real difference
    between the image and the repository even when the common path never runs it."""
    seen: Dict[str, None] = {}
    todo = list(entries)
    while todo:
        rel = todo.pop()
        if rel in seen:
            continue
        p = os.path.join(root, rel)
        if not os.path.isfile(p):
            raise SystemExit(f"the repository has no {rel}: run this from a checkout that contains the recorder")
        seen[rel] = None
        # a file INSIDE a package (signals/...) is compared, and so are its siblings it imports; what it reaches outside the package is
        # research tooling the drill image does not ship and the recorder never loads, so it is not followed
        pkg = rel.split("/")[0] + "/" if ("/" in rel and not rel.startswith("tools/")) else None
        for mod in _imports(p):
            for cand in _candidates(mod):
                if pkg and not cand.startswith(pkg):
                    continue
                if os.path.isfile(os.path.join(root, cand)) and cand not in seen:
                    todo.append(cand)
    return sorted(seen)


def file_sets(root: str = BOT) -> Tuple[List[str], List[str]]:
    rec = closure(root)
    return rec, [f for f in REPORT_TOOLS if f not in rec]


def expected_listing(root: str = BOT) -> str:
    rec, tools = file_sets(root)
    lines = [f"{_sha(os.path.join(root, f))}  {f}" for f in rec] + [MARKER]
    lines += [f"{_sha(os.path.join(root, f))}  {f}" for f in tools if os.path.isfile(os.path.join(root, f))]
    return "\n".join(lines) + "\n"


def machine_command(app: str = "carry-drill", root: str = BOT) -> str:
    rec, tools = file_sets(root)
    return f"fly ssh console -a {app} -C \"sh -c 'cd /app && sha256sum {' '.join(rec + tools)} 2>&1'\""


def parse(text: str) -> Tuple[Dict[str, str], List[str]]:
    """``({path: sha256}, [paths reported missing])``. Any other line (a banner, a warning, the marker) is ignored."""
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


def conflicts(text: str) -> List[str]:
    """Paths the text states TWICE in a way that disagrees: two different hashes, or a hash and a 'No such file'. ``parse`` keeps the last line, so concatenated output of two machines or two runs
    (a wrong hash first, the right one later) read as a match; a conflicting path is never a match."""
    seen: Dict[str, set] = {}
    for ln in text.splitlines():
        m = _LINE.match(ln.strip())
        if m:
            seen.setdefault(m.group(2), set()).add(m.group(1))
            continue
        m = _MISSING.search(ln)
        if m:
            seen.setdefault(m.group(1), set()).add("MISSING")
    return sorted(k for k, v in seen.items() if len(v) > 1)


def _split_expected(text: str) -> Tuple[List[str], List[str]]:
    rec: List[str] = []
    tools: List[str] = []
    cur = rec
    for ln in text.splitlines():
        if ln.strip() == MARKER:
            cur = tools
            continue
        m = _LINE.match(ln.strip())
        if m:
            cur.append(m.group(2))
    return rec, tools


def compare(expected_text: str, actual_text: str, root: Optional[str] = BOT) -> Dict[str, Any]:
    """Compare the expected listing with what the machine reported. Fail closed: a path stated inconsistently on either side, an expected listing with no recorder files, a recorder file filed under the
    report-tools marker, or an expected listing that does not cover THIS checkout's recorder closure is never IDENTICAL. ``root=None`` skips the checkout cross-check (the listing alone is judged)."""
    want, _ = parse(expected_text)
    rec_files, tool_files = _split_expected(expected_text)
    got, missing = parse(actual_text)
    bad = set(conflicts(actual_text)) | set(conflicts(expected_text))
    closure_files: List[str] = []
    if root is not None:
        try:
            closure_files = file_sets(root)[0]
        except (SystemExit, OSError, SyntaxError):
            closure_files = []
    misfiled = sorted(f for f in tool_files if f in closure_files)             # a recorder file listed after the marker would be judged as a report tool: judge it as the recorder file it is
    if misfiled:
        rec_files = rec_files + misfiled
        tool_files = [f for f in tool_files if f not in misfiled]
    uncovered = sorted(f for f in closure_files if f not in rec_files)          # this checkout's recorder closure has files the expected listing does not name
    rows: List[Dict[str, str]] = []
    for rel in rec_files + tool_files:
        part = "recorder" if rel in rec_files else "report_tool"
        if rel in bad:
            st = "CONFLICTING_REPORT"
        elif rel in got:
            st = "MATCH" if got[rel] == want[rel] else "DIFFERENT"
        elif rel in missing:
            st = "MISSING_ON_MACHINE"
        else:
            st = "NOT_REPORTED_BY_MACHINE"
        rows.append({"file": rel, "part": part, "status": st})
    recorder_ok = bool(rec_files) and not uncovered and all(r["status"] == "MATCH" for r in rows if r["part"] == "recorder")
    all_ok = recorder_ok and all(r["status"] == "MATCH" for r in rows)
    if not rec_files:
        verdict = "EXPECTED_LISTING_HAS_NO_RECORDER_FILES"
    elif uncovered:
        verdict = "EXPECTED_LISTING_DOES_NOT_COVER_THIS_CHECKOUT"
    elif all_ok:
        verdict = "IDENTICAL_TO_EXPECTED"
    elif recorder_ok:
        verdict = "RECORDER_FILES_IDENTICAL_REPORT_TOOLS_DIFFER"
    elif not got and not missing:
        verdict = "NOTHING_REPORTED_BY_MACHINE"
    else:
        verdict = "DOES_NOT_CONTAIN_THIS_RECORDER_IMPLEMENTATION"
    return {"verdict": verdict, "rows": rows, "contains_this_recorder_implementation": recorder_ok,
            "recorder_files_compared": len(rec_files), "conflicting_paths": sorted(bad), "expected_listing_misses": uncovered,
            "differing": [r["file"] for r in rows if r["status"] != "MATCH"],
            "note": "byte-for-byte comparison of the recorder's whole import closure; it does not identify the commit or the image digest. Run `expected`, `command` and `compare` from the SAME checkout"}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("expected"); e.add_argument("--root", default=BOT)
    c = sub.add_parser("command"); c.add_argument("--app", default="carry-drill"); c.add_argument("--root", default=BOT)
    cp = sub.add_parser("compare"); cp.add_argument("--expected", required=True); cp.add_argument("--actual", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "expected":
        sys.stdout.write(expected_listing(a.root))
        return 0
    if a.cmd == "command":
        print(machine_command(a.app, a.root))
        return 0
    res = compare(open(a.expected, encoding="utf-8").read(), open(a.actual, encoding="utf-8").read())
    print(json.dumps(res, indent=2, sort_keys=True))
    return 0 if res["verdict"] in ("IDENTICAL_TO_EXPECTED", "RECORDER_FILES_IDENTICAL_REPORT_TOOLS_DIFFER") else 1


if __name__ == "__main__":
    sys.exit(main())
