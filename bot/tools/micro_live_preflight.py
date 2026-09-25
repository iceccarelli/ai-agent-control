#!/usr/bin/env python3
"""Read-only micro-live preflight check. Prints READY or REFUSE and exits.

This tool writes NOTHING: no gate file, no config, no kill switch, no
`live_authorized`, no orders. It exists so a human can ask "would the gate
allow live today?" without hand-reading `promotion_gate.py` output, and get
an answer that is exactly as fail-closed as the gate itself.

`--dry-run` is accepted for compatibility with operator tooling that always
passes it; this tool has no other mode. There is no write mode to graduate
to — that would defeat the point of a preflight check.
"""
from __future__ import annotations

import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import promotion_gate as pg  # noqa: E402


def run(*, path: str = pg.GATE_PATH) -> int:
    verdict = pg.evaluate_promotion_gate(path=path)

    lines = ["=" * 66, "MICRO-LIVE PREFLIGHT — read-only, writes nothing", "=" * 66, ""]
    for item in verdict.items:
        state = "complete" if item.complete else "incomplete"
        lines.append(f"  [{state:10s}] ({item.owner:8s}) {item.key}")
        lines.append(f"               {item.evidence}")
    lines.append("")
    if verdict.reasons:
        lines.append("  reasons blocking live:")
        for reason in verdict.reasons:
            lines.append(f"    - {reason}")
        lines.append("")

    verdict_line = "READY" if verdict.allows_live else "REFUSE"
    lines.append(f"VERDICT: {verdict_line}")
    lines.append(
        "" if verdict.allows_live else
        "(REFUSE is the only correct answer until a human completes every "
        "gate item AND config.is_live_authorized independently returns "
        "True. This tool cannot make either true.)")
    print("\n".join(lines))
    return 0 if verdict.allows_live else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="accepted for compatibility; this tool has no other mode")
    parser.add_argument("--gate-path", default=pg.GATE_PATH,
                        help="promotion gate JSON to read (default: %(default)s)")
    args = parser.parse_args(argv)
    return run(path=args.gate_path)


if __name__ == "__main__":
    raise SystemExit(main())
