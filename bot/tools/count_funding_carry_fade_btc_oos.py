#!/usr/bin/env python3
"""How many trades does `funding_carry_fade_btc_v1` schedule in the OOS window?

WHY THIS RUNS BEFORE THE CONTROL AND BEFORE ANY PERCENTILE
==========================================================
The intake's first clause is `n_OOS >= 40`, and it is the clause most exposed
to a quiet fix. The full-sample run schedules 85 trades on BTCUSDT; roughly
half the bars are late; so the OOS count will land somewhere near 42 — a
handful above the floor. A cut moved by a few bars, or a floor moved by a few
trades, would clear it and would be almost invisible in a summary.

So the count is taken first, on its own, and printed with the arithmetic that
produced it. If it comes in below 40 the verdict is INCONCLUSIVE, `t_mid` is
not moved, the floor is not lowered, and no control is run — because a control
on a sample the intake has already declared too small is a number nobody is
allowed to use.

WHAT IT REPORTS
===============
The same four places this family can lose its sample (the join, the threshold,
the one-trade-per-run scheduler, the barrier's horizon tail), plus the fifth
that is new here: the cut. Each is printed rather than summarised, so a reader
can see which one the sample went to.

WHAT IT DOES NOT DO
===================
No score, no percentile, no rotation, no seed. It reads the LOCKED calendar and
refuses to run if that calendar's hash is not the one in the lock log.
"""
from __future__ import annotations

import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import numpy as np  # noqa: E402

import backtest as bt  # noqa: E402
import market_data as md  # noqa: E402
import skill_test as sk  # noqa: E402
from signals import funding_carry_fade_btc_v1 as fb  # noqa: E402

import edge_measurement as em  # noqa: E402

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



#: The intake's clause 1. Not an argument, deliberately — a floor that can be
#: passed on the command line is a floor that can be lowered from a shell.
HARD_FLOOR = 40


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir",
                        default=os.path.join(REPO, "data", "real_linear_1d"))
    parser.add_argument("--funding-data", default=os.path.join(
        REPO, "data", "real_funding", "funding",
        "BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz"))
    parser.add_argument("--folds", default=fb.FOLDS_PATH)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--warmup", type=int, default=200)
    args = parser.parse_args(argv)

    fb.require_supported_symbol(args.symbol)
    folds = fb.load_folds(args.folds)
    if not fb.folds_are_unmodified(args.folds):
        print("REFUSING: the fold calendar's sha256 is not the one recorded "
              "in the lock log. A cut that changed after it was locked cannot "
              "produce a count that means anything.")
        return 2

    loaded, _books, _notes = md.load_corpus(args.data_dir, verify=False)
    bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
            for b in loaded[args.symbol]]
    funding = fb.load_funding(args.funding_data)

    flags, directions = fb.flags_and_directions(bars, funding,
                                                warmup=args.warmup)
    full_flags = np.array(flags, copy=True)
    late_flags = fb.restrict_flags_to_late(flags, bars, folds)

    side_lookup = {}
    for tag, side in ((fb.LONG_SETUP, "long"), (fb.SHORT_SETUP, "short")):
        idx, _net, _used = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=fb.TAKE_PROFIT_ATR, stop_atr=fb.STOP_ATR,
            horizon=fb.HORIZON, atr_period=fb.ATR_PERIOD,
            round_trip_bps=fb.ROUND_TRIP_BPS, side=side, entry_on=fb.ENTRY_ON)
        side_lookup[tag] = {int(i) for i in idx}

    eligible = np.zeros(len(bars), dtype=bool)
    eligible[sorted(side_lookup[fb.LONG_SETUP] &
                    side_lookup[fb.SHORT_SETUP])] = True
    eligible[:args.warmup] = False

    late_mask = np.zeros(len(bars), dtype=bool)
    late_mask[list(fb.late_window_indices(bars, folds))] = True
    eligible_oos = eligible & late_mask

    full_entries = sk.simulate_schedule(
        em.tradable_flags(full_flags, eligible),
        warmup=args.warmup, lockup=fb.LOCKUP)
    oos_entries = sk.simulate_schedule(
        em.tradable_flags(late_flags, eligible_oos),
        warmup=args.warmup, lockup=fb.LOCKUP)

    full_scored = [e for e in full_entries if e in directions]
    oos_scored = [e for e in oos_entries if e in directions]
    oos_runs, _gaps = sk.extract_blocks(
        em.tradable_flags(late_flags, eligible_oos), warmup=args.warmup)

    n_oos = len(oos_scored)
    long_oos = sum(1 for e in oos_scored if directions[e] == fb.LONG_SETUP)
    rates = fb.funding_at_decision(bars, funding)

    print("=" * 78)
    print("SLICE 57 — OOS COUNT FINDING   (no scores, no percentiles)")
    print("=" * 78)
    print(f"  signal           : {fb.NAME}   ({args.symbol} only)")
    print(f"  derived from     : {fb.DERIVED_FROM}  (FROZEN ABSENT — same "
          f"constants, different universe, different gate)")
    print(f"  rule             : |f| >= {fb.FUND_ABS:g} at the last print on "
          f"or before the bar close   (frozen, not lowered)")
    print(f"  barrier          : +{fb.TAKE_PROFIT_ATR:g}/-{fb.STOP_ATR:g} ATR, "
          f"horizon {fb.HORIZON}, {fb.ROUND_TRIP_BPS:g} bps + funding over "
          f"the hold, entry {fb.ENTRY_ON}")
    print()
    print(f"  fold calendar    : {os.path.relpath(args.folds, REPO)}")
    print(f"  folds sha256     : {fb.folds_sha256(args.folds)}")
    print(f"                     matches the lock log -> the cut is the one "
          f"frozen before any score existed")
    print(f"  t_mid            : {folds['t_mid']}")
    print(f"  early [t0,t_mid) : {folds['n_bars_early']:,} bars   burn-in, may "
          f"NOT register")
    print(f"  late  [t_mid,t1] : {folds['n_bars_late']:,} bars   OOS, the ONLY "
          f"registering window")
    print()
    print("-" * 78)
    print("WHERE THE SAMPLE GOES — five places, printed rather than summarised")
    print("-" * 78)
    print(f"  1. the join      : {int(np.isfinite(rates).sum()):,} of "
          f"{len(bars):,} bars joined a funding rate (no forward-fill)")
    print(f"  2. the threshold : {len(directions):,} setups over the whole "
          f"series")
    print(f"  3. the cut       : {int(full_flags.sum()):,} flagged bars -> "
          f"{int(late_flags.sum()):,} in the late window "
          f"({int(full_flags.sum()) - int(late_flags.sum()):,} withheld as "
          f"burn-in)")
    print(f"  4. the scheduler : {int(late_flags.sum()):,} late flags -> "
          f"{len(oos_runs)} contiguous runs (one trade each, slice 17)")
    print(f"  5. the horizon   : {int(eligible_oos.sum()):,} late bars can "
          f"host a barrier that resolves")
    print()
    print(f"  FULL SAMPLE      : {len(full_scored)} scored trades   "
          f"(diagnostic only — may not register)")
    print(f"  OOS              : {n_oos} scored trades   "
          f"({long_oos} long / {n_oos - long_oos} short)")
    print()
    share = (100.0 * n_oos / len(full_scored)) if full_scored else 0.0
    print(f"  the OOS window holds {share:.1f}% of the full-sample trades "
          f"against {100.0 * folds['n_bars_late'] / folds['n_bars_total']:.1f}%"
          f" of the bars")
    print()
    print("=" * 78)
    if n_oos >= HARD_FLOOR:
        print(f"COUNT FINDING: n_OOS = {n_oos} >= {HARD_FLOOR}. Clause 1 of "
              f"the intake's gate is MET.")
        print()
        print("The remaining four clauses are untouched by this: a control")
        print("must be VALID on this window before any percentile is read,")
        print("and M1, M2 and the sign of the mean net R decide the rest.")
        result = 0
    else:
        print(f"COUNT FINDING: n_OOS = {n_oos} < {HARD_FLOOR}. Clause 1 FAILS.")
        print()
        print("Per the intake, the verdict is INCONCLUSIVE. t_mid is NOT")
        print("moved and the floor is NOT lowered — either would be a")
        print("parameter fitted to an observed shortfall, and on a count this")
        print("close to its floor it would be nearly invisible afterwards.")
        print("No control is run: a control on a sample already declared too")
        print("small produces a number nobody is permitted to use.")
        result = 1
    print("=" * 78)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
