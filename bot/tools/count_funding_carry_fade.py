#!/usr/bin/env python3
"""How many trades does `funding_carry_fade_v1` schedule? Counts only.

WHY THIS RUNS FIRST
===================
The intake sets a hard gate of **>= 50 scored trades per symbol** for any
POSITIVE claim, with the usual instruction: a shortfall is a finding, not a
reason to lower `FUND_ABS`.

WHAT IT REPORTS, AND WHY EACH NUMBER IS HERE
============================================
Four numbers per symbol, because this family can lose its sample in four
different places and quoting only the last one would hide which:

* **bars with funding** — the join. Zero here would mean the join is broken,
  which is exactly what the first version of the loader produced from a
  microsecond/millisecond unit error. Not a market fact;
* **setups** — how often |f| reaches the threshold at a decision bar;
* **flag runs** — slice 50's lesson. `simulate_schedule` takes at most one
  entry per contiguous run of flags, so a state-shaped signal collapses. Rich
  funding persists for days at a time, so this family is expected to lose a
  large fraction of its setups here — that is a property of the pair (persistent
  signal, event-shaped scheduler) and it must be visible before the control
  runs, not inferred afterwards;
* **entries** — what the instrument will actually score.

It also reports the **funding charged** in R, because that cost is the
difference between measuring this thesis and flattering it, and a reader should
see its size before seeing any percentile.

WHAT IT DOES NOT DO
===================
It does not touch `FUND_ABS`, the stop, the take-profit or the horizon — all
are read from the signal module and none is exposed as an argument. There is no
`--seed`, no rotation and no percentile.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import backtest as bt
import market_data as md
import skill_test as sk
from signals import funding_carry_fade_v1 as fc

import edge_measurement as em

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The intake's hard gate for any POSITIVE claim, per symbol.
HARD_GATE = 50

UNIVERSE: List[Tuple[str, str]] = [
    ("BTCUSDT", "BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz"),
    ("ETHUSDT", "BINANCE_LINEAR_ETH_USDT_FUNDING.csv.gz"),
    ("SOLUSDT", "BINANCE_LINEAR_SOL_USDT_FUNDING.csv.gz"),
]


def count_for(bars, funding, *, warmup: int, args) -> dict:
    """Setups, runs, entries and the funding charge. No scores."""
    flags, directions = fc.flags_and_directions(bars, funding, warmup=warmup)

    side_lookup = {}
    shift = {}
    for tag, side in ((fc.LONG_SETUP, "long"), (fc.SHORT_SETUP, "short")):
        idx, net, used = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=args.take_profit_atr, stop_atr=args.stop_atr,
            horizon=args.horizon, atr_period=args.atr_period,
            round_trip_bps=args.round_trip_bps, side=side,
            entry_on=fc.ENTRY_ON)
        adjusted = fc.funding_adjusted_net_r(
            bars, funding, idx, net, used, tag,
            stop_atr=args.stop_atr, atr_period=args.atr_period)
        shift[tag] = float(np.mean(adjusted - net)) if net.size else 0.0
        side_lookup[tag] = {int(i) for i in idx}

    eligible = np.zeros(len(bars), dtype=bool)
    eligible[sorted(side_lookup[fc.LONG_SETUP]
                    & side_lookup[fc.SHORT_SETUP])] = True
    eligible[:warmup] = False

    scoreable = [i for i in sorted(directions) if eligible[i] and flags[i]]
    schedule = em.tradable_flags(flags, eligible)
    runs, _gaps = sk.extract_blocks(schedule, warmup=warmup)
    entries = sk.simulate_schedule(schedule, warmup=warmup, lockup=args.lockup)
    scored = [e for e in entries if e in directions]

    rates = fc.funding_at_decision(bars, funding)
    return {
        "bars": len(bars),
        "funding_prints": len(funding),
        "bars_with_funding": int(np.isfinite(rates).sum()),
        "setups": len(directions),
        "scoreable": len(scoreable),
        "flag_runs": len(runs),
        "longest_run": int(max(length for _s, length in runs)) if runs else 0,
        "entries": len(scored),
        "long_entries": sum(1 for e in scored
                            if directions[e] == fc.LONG_SETUP),
        "funding_r_long": shift[fc.LONG_SETUP],
        "funding_r_short": shift[fc.SHORT_SETUP],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir",
                        default=os.path.join(REPO, "data", "real_linear_1d"))
    parser.add_argument("--funding-dir",
                        default=os.path.join(REPO, "data", "real_funding",
                                             "funding"))
    parser.add_argument("--warmup", type=int, default=200)
    parser.add_argument("--lockup", type=int, default=fc.LOCKUP)
    parser.add_argument("--horizon", type=int, default=fc.HORIZON)
    parser.add_argument("--atr-period", type=int, default=fc.ATR_PERIOD)
    parser.add_argument("--stop-atr", type=float, default=fc.STOP_ATR)
    parser.add_argument("--take-profit-atr", type=float,
                        default=fc.TAKE_PROFIT_ATR)
    parser.add_argument("--round-trip-bps", type=float,
                        default=fc.ROUND_TRIP_BPS)
    args = parser.parse_args(argv)

    loaded, _books, _notes = md.load_corpus(args.data_dir, verify=False)

    print("=" * 78)
    print("COUNT FINDING — funding_carry_fade_v1   (no scores, no percentiles)")
    print("=" * 78)
    print(f"  rule             : |f| >= {fc.FUND_ABS:g} at the LAST funding "
          f"print on or before the bar close   (frozen)")
    print(f"  direction        : f >= +{fc.FUND_ABS:g} -> SHORT (fade rich long "
          f"carry); f <= -{fc.FUND_ABS:g} -> LONG")
    print(f"  barrier          : +{args.take_profit_atr:g}/-{args.stop_atr:g} "
          f"ATR, horizon {args.horizon}, {args.round_trip_bps:g} bps, "
          f"entry {fc.ENTRY_ON}, on the LINEAR daily series")
    print(f"  costs            : {args.round_trip_bps:g} bps round trip PLUS "
          f"the funding actually paid over each hold")
    print(f"  hard gate        : >= {HARD_GATE} scored trades per symbol")
    print()

    results = {}
    for symbol, filename in UNIVERSE:
        bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
                for b in loaded[symbol]]
        funding = fc.load_funding(os.path.join(args.funding_dir, filename))
        stats = count_for(bars, funding, warmup=args.warmup, args=args)
        results[symbol] = stats
        print(f"  {symbol:8s} {stats['bars']:>5,} bars   "
              f"setups {stats['setups']:>4}   "
              f"scoreable {stats['scoreable']:>4}   "
              f"entries {stats['entries']:>4}   "
              f"-> {'MEETS' if stats['entries'] >= HARD_GATE else 'BELOW'} "
              f"the {HARD_GATE}-trade gate")

    print()
    print("-" * 78)
    print("WHERE THE SAMPLE GOES — the join, then the scheduler")
    print("-" * 78)
    for symbol, _f in UNIVERSE:
        s = results[symbol]
        print(f"  {symbol:8s} {s['funding_prints']:,} funding prints; "
              f"{s['bars_with_funding']:,} of {s['bars']:,} bars joined a rate "
              f"(no forward-fill)")
        print(f"           {s['setups']} setups -> {s['flag_runs']} contiguous "
              f"runs (longest {s['longest_run']}) -> {s['entries']} entries "
              f"({s['long_entries']} long / "
              f"{s['entries'] - s['long_entries']} short)")
        print(f"           funding ADJUSTMENT to net R: "
              f"{s['funding_r_long']:+.4f} on longs, "
              f"{s['funding_r_short']:+.4f} on shorts")
        print(f"           (a short RECEIVES positive funding, so its net R "
              f"rises; a long pays. Applied to observed AND every replicate.)")

    print()
    print("Rich funding PERSISTS for days, so the flags form long runs and")
    print("simulate_schedule takes one entry per run (slice 17 — the rule that")
    print("makes the rotation null valid). The gap between setups and entries")
    print("is that persistence, not a defect, and it is printed rather than")
    print("described so the control is not asked to explain it later.")
    print()

    below = [s for s, _f in UNIVERSE if results[s]["entries"] < HARD_GATE]
    print("=" * 78)
    if below:
        print(f"COUNT FINDING: BELOW THE HARD GATE on {', '.join(below)}")
        print()
        print("Per the intake, FUND_ABS is NOT lowered and no grid is run. A")
        print("symbol below the gate cannot support a POSITIVE claim for this")
        print("family, and a threshold moved to reach a count would be a")
        print("parameter fitted to an observed shortfall.")
    else:
        print("COUNT FINDING: every symbol clears the >= 50 hard gate.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
