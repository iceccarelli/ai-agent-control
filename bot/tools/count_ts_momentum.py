#!/usr/bin/env python3
"""How often does `ts_momentum_v1` actually SCHEDULE a trade? Counts only.

WHY THIS RUNS FIRST, AND ALONE
==============================
The intake states a design expectation of **>= 100 scored trades per symbol**
and a **hard gate of >= 50** for any POSITIVE claim, with an instruction for
what to do if the implementation misses it:

    "If after implementation a symbol has < 50 -> non-measurable; cannot
     support POSITIVE. Do NOT lower LOOKBACK/SKIP to manufacture counts."

Slice 43 learned the cost of finding this out late, and slice 49 learned it
again. So the count comes first, from its own tool, with no percentile anywhere
in the file.

THE THING THIS SIGNAL MAKES VISIBLE
===================================
Every prior family here fires on an EVENT: a shock, a range extreme, a gap, a
channel break. Their flags are sparse, so they form many short runs.

`ts_momentum_v1` is a **STATE**. The sign of a 10-bar return is defined on
essentially every bar past warmup, so `flags` is nearly all-True and forms very
few contiguous runs. `simulate_schedule` yields **at most one entry per
contiguous run**, on the run's first bar (slice 17 — the rule that makes the
rotation null valid at all).

The consequence, predicted in EDGE.md §31d and pinned in
`tests/test_ts_momentum_v1.py` before this tool ran: **the schedule is governed
by how often the sign FLIPS, not by how many bars are flagged.** A symbol whose
10-bar return sign persists for months contributes one trade per persistence
episode, not one per bar.

This tool therefore reports both numbers — flagged bars and scheduled entries —
because quoting only the first would make the sample look an order of magnitude
larger than the instrument will actually score.

WHAT IT DOES NOT DO
===================
It does not touch LOOKBACK or SKIP. Both are read from the signal module, and
neither is exposed as an argument, so a count produced under a different window
is impossible to generate here. There is no `--seed`, no rotation and no
percentile: a count tool that could be talked into producing a number would
eventually be asked to.
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
from signals import ts_momentum_v1 as og

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
DESIGN_EXPECTATION = 100

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
    ret = og.momentum_returns(bars)
    finite = ret[np.isfinite(ret)]
    sign = np.sign(finite)
    flips = int((sign[1:] != sign[:-1]).sum()) if finite.size > 1 else 0

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
        "sign_flips": flips,
        "defined_bars": int(finite.size),
        "exact_zero_returns": int((finite == 0.0).sum()),
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
    print("COUNT FINDING — ts_momentum_v1   (no scores, no percentiles)")
    print("=" * 78)
    print(f"  rule             : sign of close[t-{og.SKIP}] / "
          f"close[t-{og.SKIP + og.LOOKBACK}] - 1   "
          f"(LOOKBACK {og.LOOKBACK}, SKIP {og.SKIP}, frozen)")
    print(f"  direction        : ret > 0 -> LONG, ret < 0 -> SHORT "
          f"(continuation); ret == 0 -> no setup")
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
    print("WHY — a STATE signal against an EVENT-shaped scheduler")
    print("-" * 78)
    for symbol, _d in UNIVERSE:
        s = results[symbol]
        print(f"  {symbol:8s} {s['setups']:,} flagged bars of "
              f"{s['defined_bars']:,} with a defined window "
              f"({s['exact_zero_returns']} exactly-zero returns)")
        print(f"           {s['flag_runs']} contiguous flag runs, longest "
              f"{s['longest_run']:,} bars; {s['sign_flips']} sign flips")
        print(f"           -> {s['entries']} scheduled entries "
              f"(one per run, at its first bar)")

    print()
    print("Every prior family here fires on an EVENT and its flags are sparse.")
    print("This one is a STATE: the sign of a 10-bar return is defined on")
    print("nearly every bar, so the flags form few long runs. simulate_schedule")
    print("takes AT MOST ONE entry per contiguous run (slice 17 — the rule that")
    print("makes the rotation null valid), so what governs the sample size is")
    print("how often the sign FLIPS, not how many bars are flagged.")
    print()

    below = [s for s, _d in UNIVERSE
             if results[s]["entries"] < DESIGN_TARGET]
    print("=" * 78)
    if below:
        print(f"COUNT FINDING: BELOW THE HARD GATE on {', '.join(below)}")
        print()
        print("Per the intake, LOOKBACK and SKIP are NOT changed and no grid")
        print("is run. A symbol below the gate cannot support a POSITIVE claim")
        print("for this family, and a window moved to reach a count would be a")
        print("parameter fitted to an observed shortfall.")
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
