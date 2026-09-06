#!/usr/bin/env python3
"""Is the SAME-ASSET directed instrument honest for `compression_breakout_v1`?

WHAT THIS RUNS
==============
The slice-43 / 46 / 49 / 50 / 52 same-asset directed control: whole bars
shuffled and re-based, each surrogate finding **its own** compressed breaks,
scored by `edge_measurement.score_schedule` and ranked against
`rotation_replicates`. The three-clause rule (slice 37, unchanged) decides VALID
or INVALID.

THE PREDICTION, AND WHY THIS RUN IS THE INTERESTING ONE
=======================================================
`docs/RESEARCH_HOLD.md` records a standing hypothesis: the directed instrument's
upward bias tracks how directly the trigger reads the same bar geometry the
barrier prices its stops from.

    slice 43   return-based trigger        49.05        clean
    slice 46   high/low-based trigger      z +3.632     badly biased
    slice 52   close-only trigger          |z| <= 0.94  clean, three symbols
    slice 53   high/low AND ATR-based      ?

**This is the first high/low trigger since slice 46, and it also reads the ATR
directly** — the very quantity the barrier uses to size its stop. On the
hypothesis, that is the worst combination yet, so EDGE.md §34d (committed at
0a07162, before this file existed) recorded the prediction: **this control is at
elevated risk of failing clause (a) in the positive direction.**

Either outcome is informative and neither is a licence to change anything:

* biased -> no percentile may be read for that symbol, and the null is **not**
  repaired mid-slice. That is a construction question for a human
  pre-declaration, not a fix to apply after seeing which way it went;
* clean -> the hypothesis is weaker than four data points suggested, and this
  is the case that could most easily have confirmed it.

A SECOND THING THIS RUN CANNOT DECIDE
=====================================
The count finding (`artifacts/slice53_count_finding_compression_breakout.log`,
committed at 0c5baa1) already put POSITIVE out of reach: the intake needs two
symbols at >= 50 scored trades and only BTCUSD reaches it, at 76. ETHUSDT (23)
and SOLUSDT (35) are non-measurable for a claim whatever their controls say.
This control still runs on all three, because a control is a statement about a
construction on a series and is worth having on the record either way.

THE CRITERION (human pre-declaration, slice 37, unchanged)
==========================================================
CONTROL VALID if and only if ALL THREE hold:

    (a) |z| < 1.96   two-sided, mean of surrogate percentiles vs 50,
                     SE = sample sd / sqrt(n_surrogates)
    (b) Kolmogorov-Smirnov vs Uniform(0, 100) NOT rejected at p >= 0.05
    (c) incompletes <= 5% of surrogates

`median <= 50` is retired and informational. There is no fourth clause, and one
is not to be added after seeing these numbers.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import backtest as bt
import market_data as md
import skill_test as sk
from signals import compression_breakout_v1 as fade

import control_directed as cd
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


def build_book_and_flags(bars, *, warmup, args):
    """Exactly what `edge_measurement` builds for this signal, via the same calls.

    The flags come from the bars handed in — which for a surrogate means the
    surrogate's own shocks. That is the whole difference from the cross-asset
    control, and it is why this function takes one series rather than two.
    """
    flags, directions = fade.flags_and_directions(bars, warmup=warmup)

    side_lookup = {}
    side_net = {}
    for tag, side in ((fade.LONG_SETUP, "long"), (fade.SHORT_SETUP, "short")):
        idx, net, _u = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=args.take_profit_atr, stop_atr=args.stop_atr,
            horizon=args.horizon, atr_period=args.atr_period,
            round_trip_bps=args.round_trip_bps, side=side,
            entry_on=fade.ENTRY_ON,
        )
        side_lookup[tag] = {int(i): p for p, i in enumerate(idx)}
        side_net[tag] = net

    eligible = np.zeros(len(bars), dtype=bool)
    common = set(side_lookup[fade.LONG_SETUP]) & set(side_lookup[fade.SHORT_SETUP])
    eligible[sorted(common)] = True
    eligible[:warmup] = False

    return (flags, eligible, directions, side_lookup, side_net,
            side_lookup[fade.LONG_SETUP], side_net[fade.LONG_SETUP])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir",
                        default=os.path.join(REPO, "data", "real_1d"))
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--interval", default="D")
    parser.add_argument("--control-runs", type=int, default=200)
    parser.add_argument("--runs", type=int, default=1500)
    parser.add_argument("--lockup", type=int, default=fade.LOCKUP)
    parser.add_argument("--horizon", type=int, default=fade.HORIZON)
    parser.add_argument("--atr-period", type=int, default=fade.ATR_PERIOD)
    parser.add_argument("--stop-atr", type=float, default=fade.STOP_ATR)
    parser.add_argument("--take-profit-atr", type=float,
                        default=fade.TAKE_PROFIT_ATR)
    parser.add_argument("--round-trip-bps", type=float,
                        default=fade.ROUND_TRIP_BPS)
    parser.add_argument("--warmup", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20250730)
    parser.add_argument("--min-trades", type=int, default=20,
                        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    loaded, _b, _n = md.load_corpus(args.data_dir)
    symbol = args.symbol or sorted(loaded)[0]
    bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
            for b in loaded[symbol]]

    counts = fade.summary(bars, warmup=args.warmup)

    print("=" * 78)
    print("SAME-ASSET DIRECTED CONTROL — compression_breakout_v1")
    print("=" * 78)
    print(f"  symbol           : {symbol} ({len(bars):,} bars) "
          f"from {args.data_dir}")
    print(f"  real-series setups: {counts['setups']}  "
          f"({counts['long_setups']} long / {counts['short_setups']} short)")
    print(f"  surrogates       : {args.control_runs}")
    print(f"  rotations each   : {args.runs}")
    print(f"  barrier          : +{args.take_profit_atr:g}/-{args.stop_atr:g} "
          f"ATR, horizon {args.horizon}, {args.round_trip_bps:g} bps, "
          f"entry {fade.ENTRY_ON}")
    print(f"  trigger          : ATR pctile <= {fade.COMP_MAX:g} over "
          f"{fade.PCTILE_WINDOW} bars AND close beyond the {fade.BREAK_N}-bar "
          f"channel; up -> LONG, down -> SHORT (continuation)")
    print()
    print("Whole bars are shuffled and re-based, so each surrogate finds its")
    print("own compressed breaks. PREDICTED BEFORE THIS RUN (EDGE.md 34d at")
    print("0a07162): this is the first high/low trigger since slice 46 AND it")
    print("reads the ATR the barrier uses to size its stop, so on the standing")
    print("bias hypothesis it is at ELEVATED RISK of failing clause (a)")
    print("positively. If it does, no percentile may be read and the null is")
    print("NOT repaired in this slice. If it comes back clean, the hypothesis")
    print("is weaker than four data points suggested.")
    print()

    percentiles: List[float] = []
    incomplete = 0
    for run in range(1, args.control_runs + 1):
        shuffled = sk.surrogate_series(bars, seed=args.seed + 1000 + run)
        (flags, eligible, directions, side_lookup, side_net,
         lookup, net_r) = build_book_and_flags(
            shuffled, warmup=args.warmup, args=args)
        entries = sk.simulate_schedule(
            em.tradable_flags(flags, eligible),
            warmup=args.warmup, lockup=args.lockup)
        book = em.DirectedBook(
            directions=directions,
            direction_seq=[directions[e] for e in entries if e in directions],
            lookup=side_lookup, net_r=side_net)
        observed = em.score_schedule(entries, lookup, net_r, book=book)
        if observed is None or observed.n_trades < args.min_trades:
            incomplete += 1
            print(f"  surrogate {run}: INCOMPLETE "
                  f"({0 if observed is None else observed.n_trades} scoreable "
                  f"entries, need {args.min_trades})", flush=True)
            continue
        rng = np.random.default_rng(args.seed + 7_000_000 + run)
        replicates = em.rotation_replicates(
            flags, eligible, lookup, net_r, warmup=args.warmup,
            lockup=args.lockup, runs=args.runs, rng=rng, book=book)
        if len(replicates) < args.runs // 10:
            incomplete += 1
            print(f"  surrogate {run}: INCOMPLETE "
                  f"({len(replicates)} replicates)", flush=True)
            continue
        pct = em.percentile_of(observed.mean_r, [s.mean_r for s in replicates])
        percentiles.append(pct)
        print(f"  surrogate {run}: percentile {pct:5.1f}  "
              f"({observed.n_trades} trades)", flush=True)

    print()
    print("=" * 78)
    print("VERDICT")
    print("=" * 78)
    verdict = cd.evaluate_control(percentiles, incomplete=incomplete,
                                  total=args.control_runs)
    print(f"surrogates used      : {verdict.n} of {verdict.total}")
    if verdict.n:
        print(f"  (a) mean vs 50     : mean {verdict.mean:.2f}, "
              f"sd {verdict.sd:.2f}, z = {verdict.z:+.3f}   "
              f"|z| < {cd.Z_ABS_MAX} -> "
              f"{'PASS' if verdict.z_ok else 'FAIL'}")
        print(f"  (b) KS vs U(0,100) : D = {verdict.ks_stat:.4f}, "
              f"p = {verdict.ks_p:.4f}   p >= {cd.KS_MIN_P} -> "
              f"{'PASS' if verdict.ks_ok else 'FAIL'}")
    else:
        print("  (a) mean vs 50     : no usable surrogates")
        print("  (b) KS vs U(0,100) : no usable surrogates")
    print(f"  (c) incompletes    : {verdict.incomplete} of {verdict.total} "
          f"({100.0 * verdict.incomplete_share:.1f}%)   "
          f"<= {int(100 * cd.MAX_INCOMPLETE_SHARE)}% -> "
          f"{'PASS' if verdict.complete_ok else 'FAIL'}")
    if verdict.n:
        print()
        print(f"  median {verdict.median:.2f}   INFORMATIONAL ONLY — retired "
              "as a gate in slice 37;")
        print("  it is a coin flip on a correct instrument (EDGE.md 11a, 17c) "
              "and decides nothing.")
    print()
    if verdict.valid:
        print("CONTROL: **VALID** — all three pre-declared clauses hold.")
        print()
        print("The SAME-ASSET directed construction (compressed-ATR break of")
        print("the own 20-bar channel, traded as CONTINUATION, next-open fill)")
        print("is unbiased on series where nothing can be timed.")
        print("THIS SAYS NOTHING ABOUT EDGE.")
        return 0
    print("CONTROL: **INVALID — the same-asset directed instrument cannot be "
          "trusted.**")
    print("  failed: " + "; ".join(verdict.failures))
    print()
    print("No real-series percentile from this construction may be reported.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
