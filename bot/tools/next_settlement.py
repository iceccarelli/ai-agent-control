#!/usr/bin/env python3
"""Is a funding settlement close enough to start a carry drill now?

Bybit's perp funding settles at 00:00, 08:00 and 16:00 UTC. A drill that must
cross one needs to open shortly BEFORE it (a long hold is a long exposure) and
not so late that the settlement has passed. This answers one question for the
unattended workflow: is the next settlement between `--min-lead-minutes` and
`--max-lead-minutes` away? Exit 0 yes, 10 no. Pure: the clock is `--now-ms`
(default: the wall clock) and nothing is sent anywhere.

    python tools/next_settlement.py --min-lead-minutes 4 --max-lead-minutes 20
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Optional, Sequence, Tuple

SETTLEMENT_HOURS_UTC = (0, 8, 16)
DAY_MS = 86_400_000
HOUR_MS = 3_600_000
EXIT_INSIDE, EXIT_OUTSIDE = 0, 10


def next_settlement_ms(now_ms: int) -> int:
    """The first settlement strictly after `now_ms` (epoch ms, UTC)."""
    day_start = (int(now_ms) // DAY_MS) * DAY_MS
    for day in (day_start, day_start + DAY_MS):
        for hour in SETTLEMENT_HOURS_UTC:
            t = day + hour * HOUR_MS
            if t > now_ms:
                return t
    raise AssertionError("unreachable: a settlement exists in the next 48 h")


def window_status(now_ms: int, *, min_lead_s: float, max_lead_s: float
                  ) -> Tuple[bool, int, int]:
    """`(inside, seconds_to_next_settlement, settlement_ms)`."""
    if not (0 <= min_lead_s <= max_lead_s):
        raise ValueError("need 0 <= min lead <= max lead")
    settle = next_settlement_ms(now_ms)
    lead = (settle - int(now_ms)) / 1000.0
    return (min_lead_s <= lead <= max_lead_s, int(lead), settle)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--min-lead-minutes", type=float, default=4.0)
    ap.add_argument("--max-lead-minutes", type=float, default=20.0)
    ap.add_argument("--now-ms", type=int, default=0,
                    help="reference time in epoch ms (default: now)")
    args = ap.parse_args(argv)
    now = args.now_ms or int(time.time() * 1000)
    try:
        inside, lead, settle = window_status(
            now, min_lead_s=args.min_lead_minutes * 60.0,
            max_lead_s=args.max_lead_minutes * 60.0)
    except ValueError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 3
    print(f"next_settlement_ms={settle}")
    print(f"seconds_to_settlement={lead}")
    print(f"inside_window={'true' if inside else 'false'}")
    return EXIT_INSIDE if inside else EXIT_OUTSIDE


if __name__ == "__main__":
    raise SystemExit(main())
