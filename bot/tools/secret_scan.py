#!/usr/bin/env python3
"""Scan tracked files for credential-shaped content. Offline, standard library only.

    python3 tools/secret_scan.py            # scan `git ls-files`
    python3 tools/secret_scan.py PATH ...   # scan these files

It prints ``path:line: rule`` and NEVER the matched text, so the output of a scan
cannot itself leak what it found (CI logs are public). Exit 1 if anything matched,
2 if a file list could not be obtained.

This is a net for the obvious shapes, not a proof of absence. A generic high-entropy
detector is deliberately not attempted: it would flag the sha256 digests this
repository stores everywhere and teach people to ignore the tool.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Iterable, List, Optional, Sequence, Tuple

#: (rule name, pattern). Patterns match credential SHAPES. Placeholder values
#: (``...``, ``<key>``, ``xxxx``, ``example``) are filtered out below.
RULES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("private-key-block", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("github-fine-grained-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{20,}\b")),
    ("fly-token", re.compile(r"\bfo1_[A-Za-z0-9_-]{20,}\b|\bFlyV1 fm[12]_[A-Za-z0-9+/=_-]{20,}")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9]{40,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    # an assignment of a long literal to a name that says it is a credential. The
    # value is group 1; quotes are optional (env / YAML style); a name that contains
    # "test" or "testnet" is still a credential name.
    ("credential-assignment", re.compile(
        r"""(?ix)\b(?:[a-z0-9_]*api[_-]?key|[a-z0-9_]*api[_-]?secret|[a-z0-9_]*secret[_-]?key|
            access[_-]?token|auth[_-]?token|password|passwd)\b["']?\s*[:=]\s*["']?([A-Za-z0-9/+_=-]{16,})["']?""")),
)

_PLACEHOLDER = re.compile(r"(?i)(x{4,}|\.{3}|<[^>]+>|example|placeholder|changeme|dummy|fake|test|your[_-]|"
                          r"sample|redacted|0{8,}|1234567890|abcdef)")

#: Findings reviewed by a human and known to be fixtures: phrase-shaped dummy values
#: handed to test doubles (``"super_secret_value_1"``), not credentials. Pinned by
#: file, rule AND count, so a NEW credential-shaped literal in the same file is
#: still reported. Never extend this to a real value; rotate the credential instead.
REVIEWED_FIXTURES = {
    ("bot/tests/test_close_testnet_experiment.py", "credential-assignment"): 2,
    ("bot/tests/test_memory.py", "credential-assignment"): 2,
    ("bot/tests/test_paper_agent_certification.py", "credential-assignment"): 5,
    ("bot/tests/test_owner_tools.py", "credential-assignment"): 1,
    ("bot/tests/test_project_status.py", "credential-assignment"): 1,
    # Reviewed in the economic-engine integration: the manifest names environment-variable NAMES (a mapping of "api_key" -> a variable name, never a value), and its test feeds
    # an obviously fake literal to prove that credential VALUES are never echoed. Count-pinned like the rest.
    ("bot/tools/testnet_action_manifest.py", "credential-assignment"): 1,
    ("bot/tests/test_testnet_action_manifest.py", "credential-assignment"): 1,
}

#: Files that are binary or generated and carry no reviewable text.
SKIP_SUFFIXES = (".gz", ".so", ".png", ".jpg", ".pdf", ".zip", ".db", ".pyc")
MAX_BYTES = 2_000_000


def tracked_files(root: str) -> Optional[List[str]]:
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-z"], check=True,
                             capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return [os.path.join(root, p) for p in out.decode("utf-8", "replace").split("\0") if p]


def scan_text(text: str) -> List[Tuple[int, str]]:
    """`[(line_number, rule)]`. The matched text is never returned."""
    hits: List[Tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if len(line) > 2000:
            line = line[:2000]
        for rule, pattern in RULES:
            m = pattern.search(line)
            if not m:
                continue
            if rule == "credential-assignment" and _PLACEHOLDER.search(m.group(1)):
                continue
            hits.append((number, rule))
    return hits


#: Files the last `scan_paths` could not read (too large / not UTF-8), so a clean
#: result can say how much it did NOT look at.
SKIPPED: List[str] = []


def scan_paths(paths: Iterable[str]) -> List[Tuple[str, int, str]]:
    found: List[Tuple[str, int, str]] = []
    SKIPPED.clear()
    for path in paths:
        if path.endswith(SKIP_SUFFIXES) or not os.path.isfile(path):
            continue
        try:
            if os.path.getsize(path) > MAX_BYTES:
                SKIPPED.append(path)
                continue
            with open(path, "r", encoding="utf-8", errors="strict") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError):
            SKIPPED.append(path)
            continue
        found.extend((path, n, rule) for n, rule in scan_text(text))
    return found


def apply_reviewed(found: List[Tuple[str, int, str]], root: str) -> List[Tuple[str, int, str]]:
    """Drop findings that exactly match a reviewed fixture entry (file, rule, count)."""
    grouped: dict = {}
    for item in found:
        grouped.setdefault((os.path.relpath(item[0], root), item[2]), []).append(item)
    kept: List[Tuple[str, int, str]] = []
    for key, items in grouped.items():
        if REVIEWED_FIXTURES.get(key) == len(items):
            continue
        kept.extend(items)
    return kept


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    paths = args or tracked_files(root)
    if paths is None:
        print("could not list tracked files (is this a git checkout?)", file=sys.stderr)
        return 2
    found = apply_reviewed(scan_paths(paths), root)
    for path, line, rule in found:
        print(f"{os.path.relpath(path, root)}:{line}: {rule}")
    print(f"secret scan: {len(found)} finding(s) in {len(list(paths))} file(s); "
          f"{len(SKIPPED)} not scanned (binary, non-UTF-8 or over {MAX_BYTES} bytes)")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
