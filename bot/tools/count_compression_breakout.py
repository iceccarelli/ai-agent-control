#!/usr/bin/env python3
"""How many trades does `compression_breakout_v1` schedule, and what does the
compression filter actually remove? Counts only.

WHY THIS RUNS FIRST
===================
The intake states a design expectation of **>= 80 scored trades per symbol** and
a **hard gate of >= 50**, with an instruction for a shortfall:

    "If any symbol < 50 after implementation -> non-measurable for POSITIVE.
     Do not raise COMP_MAX or cut BREAK_N after seeing counts."

THE SECOND NUMBER IS THE ONE THAT DECIDES WHAT THIS FAMILY *IS*
===============================================================
The break half of this rule is a 20-bar Donchian, and `donchian_breakout_v1` is
frozen ABSENT. The entire novelty is the compression precondition, so this tool
reports the filter's **survival rate**: how many bars break the channel at all,
and how many of those are also compressed.

* if nearly every break survives, the precondition is decorative and the
  material-difference claim is hollow;
* if nearly none survives, the sample dies and the family is non-measurable.

**Predicted in EDGE.md §34c, committed at 0a07162 before this tool existed:**
the filter will remove most breaks, because compression and breakout are in
tension — a bar that breaks a channel usually expands range, and its own true
range enters the ATR whose percentile is being tested. Where the survival rate
lands is the finding.

WHAT IT DOES NOT DO
===================
It does not touch COMP_MAX, BREAK_N, the percentile window or the channel
length — all are read from the signal module and none is exposed as an
argument. There is no `--seed`, no rotation and no percentile: a count tool that
could be talked into producing a number would eventually be asked to.
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
from signals import compression_breakout_v1 as og

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

#: The intake's HARD GATE for any POSITIVE claim, per symbol. Not a bar this
#: tool enforces — it reports against it, and a human decides what a
#: shortfall means.
DESIGN_TARGET = 50

#: The intake's softer design EXPECTATION, reported alongside so a count
#: that clears the gate but misses the expectation is still visible.
DESIGN_EXPECTATION = 80

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

    schedule = em.tradable_flags(flags, eligible)
    runs, _gaps = sk.extract_blocks(schedule, warmup=warmup)
    every_break = og.breaks_without_compression(bars)
    last = len(bars) - 1
    unfiltered = [t for t in every_break if warmup <= t < last]
    pct = og.atr_percentiles(bars)
    finite = pct[np.isfinite(pct)]
    compressed_bars = int((finite <= og.COMP_MAX).sum())

    return {
        "bars": len(bars),
        "setups": len(directions),
        "scoreable": len(scoreable),
        "entries": len(scored_entries),
        "long": sum(1 for i in scored_entries
                    if directions[i] == og.LONG_SETUP),
        "flag_runs": len(runs),
        "longest_run": int(max(length for _s, length in runs))
        if runs else 0,
        "breaks_any_volatility": len(unfiltered),
        "defined_bars": int(finite.size),
        "compressed_bars": compressed_bars,
        "survival_pct": (100.0 * len(directions) / len(unfiltered))
        if unfiltered else float("nan"),
        "merged_into_runs": len(scoreable) - len(scored_entries),
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
    print("COUNT FINDING — compression_breakout_v1   (no scores, no percentiles)")
    print("=" * 78)
    print(f"  rule             : ATR pctile <= {og.COMP_MAX:g} over "
          f"{og.PCTILE_WINDOW} bars (INCLUDING t)  AND  close beyond the "
          f"{og.BREAK_N}-bar channel (EXCLUDING t)   (frozen)")
    print(f"  direction        : break up -> LONG, break down -> SHORT "
          f"(continuation)")
    print(f"  barrier          : +{args.take_profit_atr:g}/-{args.stop_atr:g} "
          f"ATR, horizon {args.horizon}, {args.round_trip_bps:g} bps, "
          f"entry {og.ENTRY_ON}")
    print(f"  hard gate        : >= {DESIGN_TARGET} scored trades per symbol")
    print(f"  design expectation: >= {DESIGN_EXPECTATION} scored trades per "
          f"symbol")
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
    print("WHAT THE COMPRESSION FILTER REMOVES — the material-difference claim")
    print("-" * 78)
    for symbol, _d in UNIVERSE:
        s = results[symbol]
        print(f"  {symbol:8s} {s['breaks_any_volatility']} channel breaks at "
              f"any volatility; {s['compressed_bars']:,} of "
              f"{s['defined_bars']:,} bars compressed")
        print(f"           -> {s['setups']} breaks ALSO compressed  "
              f"(survival {s['survival_pct']:.1f}% of breaks)")
        print(f"           {s['flag_runs']} contiguous flag runs, longest "
              f"{s['longest_run']} bars -> {s['merged_into_runs']} merged away")
        print(f"           -> {s['entries']} scheduled entries "
              f"(one per run, at its first bar)")

    print()
    print("The survival rate is what makes this family distinct from the")
    print("frozen donchian_breakout_v1, whose break half this shares. A rate")
    print("near 100% would mean the precondition is decorative; a rate near 0%")
    print("would mean the sample cannot support a measurement. Compression and")
    print("breakout are in tension by construction: a bar that breaks a channel")
    print("usually expands its range, and its own true range enters the ATR")
    print("whose percentile is being tested.")
    print()

    below = [s for s, _d in UNIVERSE
             if results[s]["entries"] < DESIGN_TARGET]
    print("=" * 78)
    if below:
        print(f"COUNT FINDING: BELOW THE HARD GATE on {', '.join(below)}")
        print()
        print("Per the intake, COMP_MAX is NOT raised and BREAK_N is NOT cut,")
        print("and no grid is run. A symbol below the gate cannot support a")
        print("POSITIVE claim for this family, and a threshold moved to reach")
        print("a count would be a parameter fitted to an observed shortfall.")
    else:
        short = [s for s, _d in UNIVERSE
                 if results[s]["entries"] < DESIGN_EXPECTATION]
        print("COUNT FINDING: every symbol clears the >= 50 hard gate.")
        if short:
            print(f"  Below the >= {DESIGN_EXPECTATION} design expectation: "
                  f"{', '.join(short)} — recorded, not acted on.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
