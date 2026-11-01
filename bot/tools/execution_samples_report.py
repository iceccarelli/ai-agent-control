#!/usr/bin/env python3
"""Read-only: how many real execution samples this book holds, and how many more
the entry net-benefit gate needs before it can price a round trip.

    python3 tools/execution_samples_report.py [--db PATH] \\
        [--records PATH --environment testnet|mainnet] \\
        --mode overlay|acquire --notional-usd N

`--records` reads the JSONL a drill appends (`tools/drill.py
--execution-records`); only lines labelled with `--environment` count, so
testnet fills are never mixed into a mainnet tally. `--db` and `--records` may
be combined; the same fill is counted once.

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


class _Fill:
    """The four fields the estimator reads, from one JSONL record."""
    __slots__ = ("kind", "ms", "ref", "fee_raw")

    def __init__(self, kind, ms, ref, fee_raw):
        self.kind, self.ms, self.ref, self.fee_raw = kind, ms, ref, fee_raw


def load_records(paths, environment):
    """(fills, skipped) from drill JSONL files. A line that is unreadable, of
    another schema or labelled with another environment is not a sample; it is
    counted as skipped, never guessed at."""
    fills, skipped = [], 0
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                    ok = (d.get("schema") == "execution_records/1"
                          and d.get("environment") == environment
                          and isinstance(d.get("fee_raw"), dict))
                    if not ok:
                        skipped += 1
                        continue
                    fills.append(_Fill(d["kind"], int(d["ms"]), str(d["ref"]),
                                       d["fee_raw"]))
                except (ValueError, KeyError, TypeError, AttributeError):
                    skipped += 1
    return fills, skipped


def build(db_path: str, *, mode: str, notional_usd: float, perp: str, spot: str,
          now_ms: int = 0, records=(), environment: str = "") -> dict:
    entries, skipped = [], 0
    if db_path:
        import carry_performance
        entries.extend(carry_performance.load_journal(db_path).entries)
    if records:
        extra, skipped = load_records(records, environment)
        entries.extend(extra)
    now = now_ms or int(time.time() * 1000)
    out = est.estimate_round_trip(
        entries, as_of_ms=now, execution_mode=mode, perp_symbol=perp,
        spot_symbol=spot, planned_notional_usd=notional_usd)
    out["records_skipped"] = skipped
    need = est.DEFAULT_POLICY.min_samples
    out["shortfall"] = {l["leg"]: max(0, need - int(l["n_in_size_band"]))
                        for l in out["legs"]}
    return out


def main(argv=None, env=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default="")
    ap.add_argument("--records", action="append", default=[],
                    help="drill JSONL (repeatable); needs --environment")
    ap.add_argument("--environment", default="",
                    help="count only records labelled with this environment")
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
    if not args.db and not args.records:
        ap.error("give --db and/or --records")
    if args.records and not args.environment:
        ap.error("--records needs --environment (testnet fills must never be "
                 "counted as mainnet)")
    for missing in ([args.db] if args.db else []) + args.records:
        if not os.path.exists(missing):
            print(json.dumps({"error": f"no such file: {missing}"}))
            return 2
    try:
        report = build(args.db, mode=args.mode, notional_usd=args.notional_usd,
                       perp=args.perp, spot=args.spot,
                       records=args.records, environment=args.environment)
    except Exception as exc:  # noqa: BLE001 - any failure is exit 2, never a traceback
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0 if report["status"] == est.MEASURED else 3


if __name__ == "__main__":
    sys.exit(main())
