#!/usr/bin/env python3
"""The basis your P&L actually pays, versus the one the book decides on.

THE GAP
=======
`CarryEngine` decides on DAILY CLOSES. Funding settles at 00:00, 08:00 and
16:00 UTC. `carry_backtest` reports a mean entry basis of +6 bps and a mean exit
of +29 bps, both measured at daily closes — a proxy for the basis at the moment
the transfer happens and the moment a real book has to be hedged.

Nobody has measured the difference. It matters because basis is the term that
took -$4,772 out of $42,843 of gross carry: it is the single line item that
turns a positive-carry trade negative, and the book has only ever seen it
through a once-a-day lens.

WHAT THIS ANSWERS
=================
  1. How far does the settlement basis sit from the daily-close basis?
  2. Is the difference a constant offset (harmless, absorbed by the gate's
     budget) or noise (dangerous, because the gate is then judging one number
     and the book is paying another)?
  3. Which settlement window is worst? If 00:00 systematically differs from
     16:00, an entry timed to a window is worth more than a wider basis gate.

WHAT IT DOES NOT DO
===================
Change any threshold. It reports a discrepancy. Whether `carry_costs` should
gate on the settlement basis instead of the daily one is a decision that follows
this measurement, not one this tool may make — and it would need its own
out-of-sample window, because choosing a gate after seeing which basis looked
better is exactly the selection path `reserved_holdout.py` exists to detect.

    python3 tools/fetch_settlement_klines.py --write
    python3 tools/basis_at_settlement.py --repo .
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import json
import os
import statistics
import sys
from typing import Any, Dict, List, Optional

SETTLEMENT_DIR = "data/real_settlement_8h"
DAILY_PERP = "data/real_linear_1d/ohlcv/BINANCE_LINEAR_BTC_USDT_1D.csv.gz"
DAILY_SPOT = "data/real_spot_btc/ohlcv/BINANCE_SPOT_BTC_USDT_1D.csv.gz"
SETTLEMENT_HOURS = (0, 8, 16)


def _open(path: str):
    return gzip.open(path, "rt") if path.endswith(".gz") else open(path, "rt")


def load_8h(path: str) -> Dict[dt.datetime, float]:
    if not os.path.exists(path):
        return {}
    with _open(path) as handle:
        return {dt.datetime.fromtimestamp(int(r["open_time_ms"]) / 1000.0,
                                          dt.timezone.utc): float(r["price_close"])
                for r in csv.DictReader(handle)}


def load_daily(path: str) -> Dict[dt.date, float]:
    # Absent is a legitimate state. The settlement basis stands on its own; the
    # daily series is only needed for the COMPARISON, and a missing comparison
    # is reported as zero paired windows rather than as a crash.
    if not os.path.exists(path):
        return {}
    with _open(path) as handle:
        return {dt.datetime.fromisoformat(
            r["time_period_start"].replace("Z", "+00:00")).date():
            float(r["price_close"]) for r in csv.DictReader(handle)}


def analyse(repo: str) -> Dict[str, Any]:
    perp8 = load_8h(os.path.join(repo, SETTLEMENT_DIR,
                                 "BINANCE_PERP_BTCUSDT_8H.csv.gz"))
    spot8 = load_8h(os.path.join(repo, SETTLEMENT_DIR,
                                 "BINANCE_SPOT_BTCUSDT_8H.csv.gz"))
    if not perp8 or not spot8:
        return {"available": False,
                "reason": ("no 8h settlement series. Fetch it first:\n"
                           "  python3 tools/fetch_settlement_klines.py --write"),
                "is_a_measurement": False}

    daily_perp = load_daily(os.path.join(repo, DAILY_PERP))
    daily_spot = load_daily(os.path.join(repo, DAILY_SPOT))

    windows = sorted(set(perp8) & set(spot8))
    per_hour: Dict[int, List[float]] = {h: [] for h in SETTLEMENT_HOURS}
    paired: List[Dict[str, Any]] = []

    for when in windows:
        if when.hour not in SETTLEMENT_HOURS:
            continue
        settle_bps = (perp8[when] / spot8[when] - 1.0) * 1e4
        per_hour[when.hour].append(settle_bps)
        day = when.date()
        if day in daily_perp and day in daily_spot:
            close_bps = (daily_perp[day] / daily_spot[day] - 1.0) * 1e4
            paired.append({"utc": when.strftime("%Y-%m-%dT%H:%MZ"),
                           "settlement_bps": settle_bps,
                           "daily_close_bps": close_bps,
                           "difference_bps": settle_bps - close_bps})

    settlement_only = [(perp8[w] / spot8[w] - 1.0) * 1e4 for w in windows
                       if w.hour in SETTLEMENT_HOURS]
    diffs = [p["difference_bps"] for p in paired]
    settles = [p["settlement_bps"] for p in paired] or settlement_only
    closes = [p["daily_close_bps"] for p in paired]

    def stats(values: List[float]) -> Dict[str, float]:
        if not values:
            return {}
        srt = sorted(values)
        return {"mean": statistics.mean(values),
                "median": statistics.median(values),
                "stdev": statistics.pstdev(values) if len(values) > 1 else 0.0,
                "p05": srt[int(len(srt) * 0.05)],
                "p95": srt[int(len(srt) * 0.95)]}

    diff_stats = stats(diffs)
    # A constant offset is absorbed by the gate's budget. NOISE is not: it means
    # the gate judges one number and the book pays another, and the size of that
    # gap is the size of the mistake.
    offset_like = bool(diff_stats and abs(diff_stats["mean"]) >
                       2.0 * max(diff_stats["stdev"], 1e-9))

    return {
        "available": True,
        "windows_compared": len(paired),
        "settlement_windows": len(settlement_only),
        "daily_series_present": bool(daily_perp and daily_spot),
        "first": paired[0]["utc"] if paired else None,
        "last": paired[-1]["utc"] if paired else None,
        "settlement_basis_bps": stats(settles),
        "daily_close_basis_bps": stats(closes),
        "difference_bps": diff_stats,
        "by_settlement_hour": {str(h): stats(v) for h, v in per_hour.items()},
        "difference_is_offset_not_noise": offset_like,
        "reading": (
            "a CONSTANT offset is absorbed by the gate's basis budget; NOISE "
            "means the gate judges one number and the book pays another, and "
            "the spread of the difference is the size of that mistake"),
        "is_a_measurement": True,
        "changes_no_threshold": True,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    report = analyse(args.repo)

    print("=" * 74)
    print("BASIS AT SETTLEMENT vs BASIS AT THE DAILY CLOSE")
    print("=" * 74)
    if not report["available"]:
        print(f"\n  {report['reason']}")
        return 1

    print(f"  {report['settlement_windows']} settlement windows, "
          f"{report['windows_compared']} paired with a daily close  "
          f"{report['first']} .. {report['last']}\n")
    if not report["daily_series_present"]:
        print("  ! daily series absent — settlement basis only, no comparison\n")
    for label, key in (("at settlement (00/08/16 UTC)", "settlement_basis_bps"),
                       ("at the daily close        ", "daily_close_basis_bps"),
                       ("DIFFERENCE               ", "difference_bps")):
        s = report[key]
        if not s:
            continue
        print(f"  {label}  mean {s['mean']:+7.2f}  median {s['median']:+7.2f}  "
              f"sd {s['stdev']:6.2f}  p05 {s['p05']:+7.2f}  p95 {s['p95']:+7.2f}")
    print("\n  by settlement hour (bps):")
    for hour, s in report["by_settlement_hour"].items():
        if s:
            print(f"    {int(hour):02d}:00 UTC  mean {s['mean']:+7.2f}  "
                  f"sd {s['stdev']:6.2f}")
    print(f"\n  difference is an OFFSET rather than noise: "
          f"{report['difference_is_offset_not_noise']}")
    print(f"  {report['reading']}")
    print("\n  This tool changes no threshold. Gating on the settlement basis "
          "would\n  be a NEW hypothesis and needs its own out-of-sample window.")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
