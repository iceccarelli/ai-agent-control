#!/usr/bin/env python3
"""Classify WHY the external watcher could not read the recorder's monitor history: ``python3 tools/fly_read_failure.py fly.err`` prints ONE word from ``runner_alerts.READ_FAILURES``.

The watcher (``.github/workflows/carry-watch.yml``) reads ``/data/monitor/monitor.jsonl`` with ``flyctl ssh``. A failed read is not "no data": it is reported as what it is, so a watcher with a dead credential
is told apart from a machine that is down. The classification used to be a shell ``grep`` chain inside the workflow, and an independent review ran it on canned stderr: the bare words ``token`` and
``401|403`` matched inside unrelated text ("app carry-drill has no started VMs" read as ``unknown``, "machine ... is stopped" as ``auth_failed``, an IPv6 address containing ``5401`` as ``auth_failed``), so a
dead machine could read as a token problem. It lives here now, as code a test can execute.

ONLY THE CLASS LEAVES THIS TOOL: the stderr text can carry a token fragment, goes to the job log by the caller, and is never printed here. Offline, pure, no network, no clock.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Optional, Sequence

#: Order matters. A credential problem is decided first, on STRICT evidence (an expired token also says "failed to connect"); a machine that is stopped or has no started VMs says so in words
#: that contain no credential vocabulary and is decided next; then a timeout; then a refused or failed connection.
_AUTH = re.compile(
    r"unauthori[sz]ed|forbidden|permission denied|authentication (failed|required)|"
    r"\b(invalid|expired|revoked|bad|missing)[ _-]+(access[ _-]+)?token\b|\btoken\b[^\n]{0,24}\b(invalid|expired|revoked|rejected|denied|not valid)\b|"
    r"\b(http|status|code|error|response)\b[^\n]{0,24}\b(401|403)\b|\b(401|403)\b[^\n]{0,12}\b(unauthori[sz]ed|forbidden)\b", re.I)
_DEAD = re.compile(r"no started (vms|machines)|has no (started |running )?(vms|machines)|\bis stopped\b|\bhas stopped\b|\bnot running\b|\bno machines\b|\bno running machines\b|\bdestroyed\b", re.I)
_TIMEOUT = re.compile(r"timed? ?out|deadline exceeded", re.I)
_CONNECT = re.compile(r"connection refused|no such host|(network|host) is unreachable|\bunreachable\b|could not connect|failed to connect|\bdial\b", re.I)

CLASSES = ("auth_failed", "machine_unreachable", "timeout", "unknown")


def classify(stderr_text: Optional[str]) -> str:
    """One of CLASSES for ``flyctl ssh`` stderr. Anything this cannot recognise is ``unknown`` (a read failure the watcher could not classify: never ``no_data``, never silent)."""
    text = stderr_text or ""
    if _AUTH.search(text):
        return "auth_failed"
    if _DEAD.search(text):
        return "machine_unreachable"
    if _TIMEOUT.search(text):
        return "timeout"
    if _CONNECT.search(text):
        return "machine_unreachable"
    return "unknown"


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: fly_read_failure.py STDERR_FILE", file=sys.stderr)
        return 2
    try:
        with open(args[0], encoding="utf-8", errors="replace") as fh:
            text = fh.read(200_000)
    except OSError:
        text = ""
    print(classify(text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
