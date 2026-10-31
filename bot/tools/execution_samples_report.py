#!/usr/bin/env python3
"""Read-only: how many real execution samples this book holds, and how many more
the entry net-benefit gate needs before it can price a round trip.

    python3 tools/execution_samples_report.py --db PATH --mode overlay|acquire \\
        --notional-usd N

Opens the database read-only, sends nothing, contacts no venue. For each leg of
a round trip it prints the orders on record, those usable for the planned size,
the newest sample's age, and the shortfall against the policy. Exit 0 when every
leg is measurable, 3 when any is not. The numbers are this book's own fills (a
testnet book's are testnet fills, which are not evidence of any market).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import execution_cost_estimate as est  # noqa: E402


def build(db_path: str, *, mode: str, notional_usd: float, perp: str, spot: str,
          now_ms: int = 0) -> dict:
    import carry_performance
    journal = carry_performance.load_journal(db_path)
    now = now_ms or int(time.time() * 1000)
    out = est.estimate_round_trip(
        journal.entries, as_of_ms=now, execution_mode=mode, perp_symbol=perp,
        spot_symbol=spot, planned_notional_usd=notional_usd)
    need = est.DEFAULT_POLICY.min_samples
    out["shortfall"] = {l["leg"]: max(0, need - int(l["n_in_size_band"]))
                        for l in out["legs"]}
    return out


def main(argv=None, env=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--mode", choices=("overlay", "acquire"), default=None,
                    help="default: CARRY_EXECUTION_MODE from the environment")
    ap.add_argument("--notional-usd", type=float, required=True,
                    help="the planned position size in USD")
    ap.add_argument("--perp", default=None,
                    help="default: CARRY_PERP_SYMBOL from the environment")
    ap.add_argument("--spot", default=None,
                    help="default: CARRY_SPOT_SYMBOL from the environment")
    args = ap.parse_args(argv)
    if not (args.notional_usd == args.notional_usd and args.notional_usd > 0
            and args.notional_usd != float("inf")):
        ap.error("--notional-usd must be a positive, finite number")
    env = os.environ if env is None else env
    args.mode = args.mode or str(env.get("CARRY_EXECUTION_MODE", "")).strip().lower()
    if args.mode not in ("overlay", "acquire"):
        ap.error("--mode is required (or set CARRY_EXECUTION_MODE to overlay/acquire)")
    args.perp = args.perp or str(env.get("CARRY_PERP_SYMBOL", "BTCUSDT")).strip().upper()
    args.spot = args.spot or str(env.get("CARRY_SPOT_SYMBOL", "BTCUSDT")).strip().upper()
    if not os.path.exists(args.db):
        print(json.dumps({"error": f"no database at {args.db}"}))
        return 2
    try:
        report = build(args.db, mode=args.mode, notional_usd=args.notional_usd,
                       perp=args.perp, spot=args.spot)
    except Exception as exc:  # noqa: BLE001 - any failure is exit 2, never a traceback
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0 if report["status"] == est.MEASURED else 3


if __name__ == "__main__":
    sys.exit(main())
