#!/usr/bin/env python3
"""How often does `open_gap_fade_v1` actually fire? Counts only — no scores.

WHY THIS RUNS FIRST, AND ALONE
==============================
The intake states a design target of **>= 50 scored trades per symbol** and an
instruction for what to do if the implementation misses it:

    "If implementation-time count finding shows any symbol < 50 scoreable
     setups, do NOT lower GAP_K. Record counts, stop, and report INCONCLUSIVE
     for that symbol / family per intake rules — do not grid GAP_K."

That instruction is only worth anything if the count is known before anything
else is. Slice 43 learned this the expensive way: `post_shock_fade_v1` was
implemented, controlled and measured before anyone noticed its rule fired 45 /
10 / 7 times, which left two of three symbols unable to validate a control at
all. So the count comes first here, from its own tool, with no percentile
anywhere in the file.

WHAT IT COMPUTES, AND WITH WHOSE ARITHMETIC
===========================================
The pipeline is the one `edge_measurement` uses, imported rather than copied:

    flags_and_directions  ->  barrier eligibility (both sides)
    ->  em.tradable_flags  (S1 horizon embargo)
    ->  sk.simulate_schedule (one-trade-per-run lockup)

and it stops there. What it prints is the number of entries that WOULD be
scored, not what they would score. There is deliberately no `--seed`, no
rotation, no percentile and no way to ask this tool for one: a count tool that
could be talked into producing a number would eventually be asked to.

WHAT IT DOES NOT DO
===================
It does not touch GAP_K. The threshold is a keyword-only argument nowhere in
this file, and the constant is read from the signal module so that a count
produced under a different threshold is impossible to generate here.
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
from signals import open_gap_fade_v1 as og

import edge_measurement as em

# DATA-READ LEDGER (slice 78). Installed BEFORE the first load_corpus call so
# that every corpus this tool reads is recorded, and a future holdout can be
# certified untouched by tools/reserved_holdout.py. Reading is reading: a
# negative result steers the next hypothesis exactly as a positive one does,
# which is how slice 57 ended up "out of sample" on bytes slice 55 had read.
try:
    import reserved_holdout as _read_ledger
    _read_ledger.install()
except Exception as _ledger_exc:  # noqa: BLE001 - never blocks a measurement
    # NOT a silent pass: the reason is bound and the flag is legible. An
    # unrecorded read is a real loss (a future holdout cannot be certified),
    # but it must not take the measurement down with it.
    _read_ledger = None
    _READ_LEDGER_UNAVAILABLE = repr(_ledger_exc)



REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The intake's design target, per symbol. Not a bar this tool enforces — it
#: reports against it, and a human decides what a shortfall means.
DESIGN_TARGET = 50

UNIVERSE: List[Tuple[str, str]] = [
    ("BTCUSD", os.path.join(REPO, "data", "real_1d")),
    ("ETHUSDT", os.path.join(REPO, "data", "real_multi_1d")),
    ("SOLUSDT", os.path.join(REPO, "data", "real_multi_1d")),
]


def load(symbol: str, directory: str):
    loaded, _b, _n = md.load_corpus(directory)
    if symbol not in loaded:
        raise SystemExit(f"{symbol} not present in {directory}")
    return [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
            for b in loaded[symbol]]


def count_for(bars, *, warmup: int, args) -> dict:
    """Setups, eligible setups and post-lockup entries. No scores."""
    flags, directions = og.flags_and_directions(bars, warmup=warmup)

    side_lookup = {}
    for tag, side in ((og.LONG_SETUP, "long"), (og.SHORT_SETUP, "short")):
        idx, _net, _u = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=args.take_profit_atr, stop_atr=args.stop_atr,
            horizon=args.horizon, atr_period=args.atr_period,
            round_trip_bps=args.round_trip_bps, side=side,
            entry_on=og.ENTRY_ON,
        )
        side_lookup[tag] = {int(i) for i in idx}

    eligible = np.zeros(len(bars), dtype=bool)
    eligible[sorted(side_lookup[og.LONG_SETUP] &
                    side_lookup[og.SHORT_SETUP])] = True
    eligible[:warmup] = False

    scoreable = [i for i in sorted(directions) if eligible[i] and flags[i]]
    entries = sk.simulate_schedule(em.tradable_flags(flags, eligible),
                                   warmup=warmup, lockup=args.lockup)
    scored_entries = [e for e in entries if e in directions]

    gap = og.gap_values(bars)
    finite = gap[np.isfinite(gap)]
    opens = np.array([b.open for b in bars], dtype=float)
    closes = np.array([b.close for b in bars], dtype=float)
    raw = opens[1:] - closes[:-1]

    return {
        "bars": len(bars),
        "setups": len(directions),
        "scoreable": len(scoreable),
        "entries": len(scored_entries),
        "long": sum(1 for i in scored_entries
                    if directions[i] == og.LONG_SETUP),
        "exact_zero_gaps": int((raw == 0.0).sum()),
        "gap_rows": raw.size,
        "median_abs_gap_atr": float(np.median(np.abs(finite))) if finite.size
        else float("nan"),
        "p99_abs_gap_atr": float(np.percentile(np.abs(finite), 99))
        if finite.size else float("nan"),
        "max_abs_gap_atr": float(np.abs(finite).max()) if finite.size
        else float("nan"),
        "median_abs_gap_pct": float(np.median(np.abs(raw) / closes[:-1])) * 100.0,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warmup", type=int, default=200)
    parser.add_argument("--lockup", type=int, default=og.LOCKUP)
    parser.add_argument("--horizon", type=int, default=og.HORIZON)
    parser.add_argument("--atr-period", type=int, default=og.ATR_PERIOD)
    parser.add_argument("--stop-atr", type=float, default=og.STOP_ATR)
    parser.add_argument("--take-profit-atr", type=float,
                        default=og.TAKE_PROFIT_ATR)
    parser.add_argument("--round-trip-bps", type=float,
                        default=og.ROUND_TRIP_BPS)
    args = parser.parse_args(argv)

    print("=" * 78)
    print("COUNT FINDING — open_gap_fade_v1   (no scores, no percentiles)")
    print("=" * 78)
    print(f"  rule             : |open[t] - close[t-1]| / ATR_prev[t] >= "
          f"{og.GAP_K:g}   (frozen)")
    print(f"  direction        : up-gap -> SHORT, down-gap -> LONG (fade)")
    print(f"  barrier          : +{args.take_profit_atr:g}/-{args.stop_atr:g} "
          f"ATR, horizon {args.horizon}, {args.round_trip_bps:g} bps, "
          f"entry {og.ENTRY_ON} (== the gap bar's own open)")
    print(f"  design target    : >= {DESIGN_TARGET} scored trades per symbol")
    print()

    results = {}
    for symbol, directory in UNIVERSE:
        bars = load(symbol, directory)
        stats = count_for(bars, warmup=args.warmup, args=args)
        results[symbol] = stats
        print(f"  {symbol:8s} {stats['bars']:>5,} bars   "
              f"setups {stats['setups']:>4}   "
              f"scoreable {stats['scoreable']:>4}   "
              f"entries {stats['entries']:>4}   "
              f"-> {'MEETS' if stats['entries'] >= DESIGN_TARGET else 'BELOW'} "
              f"the {DESIGN_TARGET}-trade target")

    print()
    print("-" * 78)
    print("WHY — the gap distribution in the corpora themselves")
    print("-" * 78)
    for symbol, _d in UNIVERSE:
        s = results[symbol]
        print(f"  {symbol:8s} open[t] == close[t-1] exactly on "
              f"{s['exact_zero_gaps']:,} of {s['gap_rows']:,} bars "
              f"({100.0 * s['exact_zero_gaps'] / s['gap_rows']:.1f}%)")
        print(f"           |gap| in ATR: median {s['median_abs_gap_atr']:.4f}, "
              f"p99 {s['p99_abs_gap_atr']:.3f}, max {s['max_abs_gap_atr']:.3f}"
              f"   (threshold {og.GAP_K:g})")
        print(f"           |gap| as % of price: median "
              f"{s['median_abs_gap_pct']:.4f}%")

    print()
    print("These corpora are 24/7 spot markets. There is no overnight session:")
    print("the daily open is the first print after 00:00 UTC and the prior")
    print("daily close is the last print before it. The 'gap' this thesis is")
    print("about is a tick-level move across midnight, not an auction")
    print("discontinuity — so a 0.75-ATR gap is a once-in-a-corpus event.")
    print()

    below = [s for s, _d in UNIVERSE
             if results[s]["entries"] < DESIGN_TARGET]
    print("=" * 78)
    if below:
        print(f"COUNT FINDING: BELOW TARGET on {', '.join(below)}")
        print()
        print("Per the intake, GAP_K is NOT lowered and no grid is run. A")
        print("symbol below the target cannot support a POSITIVE claim for")
        print("this family, and a threshold moved to reach a count would be a")
        print("parameter fitted to an observed shortfall.")
    else:
        print("COUNT FINDING: every symbol meets the design target.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
