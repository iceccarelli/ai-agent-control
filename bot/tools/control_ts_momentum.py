#!/usr/bin/env python3
"""Is the SAME-ASSET directed instrument honest for `ts_momentum_v1`?

WHAT THIS RUNS
==============
The slice-43 / 46 / 49 same-asset directed control: whole bars shuffled and
re-based, each surrogate finding **its own** setups, scored by
`edge_measurement.score_schedule` and ranked against `rotation_replicates`. The
three-clause rule (slice 37, unchanged) decides VALID or INVALID.

TWO PREDICTIONS, RECORDED BEFORE THIS RUN
=========================================
Both were committed before this file existed — EDGE.md §31c at 0ac7674 and the
count finding at 773a35f.

**1. The slice-49 failure mode does NOT apply here.** `surrogate_series`
re-bases each shuffled bar with `scale = price / bar.open`, which sets every
surrogate's open to the prior close and therefore deleted
`open_gap_fade_v1`'s trigger outright. It does **not** flatten close-to-close
returns: re-basing preserves each bar's internal shape, so its open-to-close
return travels with it and the shuffled sequence still has a return
distribution. A trigger reading only closes will find setups on a surrogate.

**2. It will still be INVALID, for the reason the count finding already
recorded.** This signal is a STATE, true on nearly every bar, so a surrogate's
flags form one contiguous run and `simulate_schedule` takes one entry from it.
One entry is far below the 20-trade floor a surrogate needs to be usable, so
essentially every surrogate will be counted INCOMPLETE and clause (c) will
fail.

If prediction 1 is wrong — if surrogates produce no setups at all rather than
too few schedulable ones — that is a finding about `surrogate_series` and it
will be reported as one.

WHAT THIS INVALID DOES AND DOES NOT EVIDENCE
============================================
It is a **construction mismatch**, like slice 49's and unlike slice 46's:

* slice 46 (`range_location_fade_v1`): 1,000 usable surrogates per symbol at 0%
  incomplete, mean percentile skewed to z = +3.6. The instrument ran and came
  back biased. **That** is instrument-bias evidence.
* slice 49 (`open_gap_fade_v1`): the null deleted the trigger's quantity.
* here: the null preserves the trigger, but the scheduler collapses a dense
  signal to one trade per surrogate.

None of the three licenses a percentile, and only the first says anything about
bias. Filing them together would make the standing bias hypothesis look better
supported than it is.

WHAT IS NOT DONE ABOUT IT
=========================
The obvious repair — breaking flag runs at direction changes, or scheduling by
"one position at a time" rather than one per run — is **not made here**. There
are 441 / 209 / 201 sign flips in the three corpora, so such a change would
turn a 1-trade sample into a several-hundred-trade one, and making it after
seeing the count is a construction chosen to produce a sample.
`docs/RESEARCH_HOLD.md` requires a human pre-declaration written before the run
it enables. The defect is recorded for whoever writes that declaration.

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
from signals import ts_momentum_v1 as fade

import control_directed as cd
import edge_measurement as em

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
    print("SAME-ASSET DIRECTED CONTROL — ts_momentum_v1")
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
    print(f"  trigger          : sign of close[t-{fade.SKIP}] / "
          f"close[t-{fade.SKIP + fade.LOOKBACK}] - 1; "
          f"> 0 -> LONG, < 0 -> SHORT (continuation)")
    print()
    print("Whole bars are shuffled and re-based, so each surrogate finds its")
    print("own returns. PREDICTED BEFORE THIS RUN (EDGE.md 31c at 0ac7674, the")
    print("count finding at 773a35f): surrogates WILL find setups -- re-basing")
    print("does not flatten close-to-close returns the way it flattened the")
    print("open-vs-prior-close gap in slice 49 -- but they will schedule about")
    print("ONE trade each, because a state signal's flags form one contiguous")
    print("run and simulate_schedule takes one entry per run. One trade is far")
    print("below the 20-trade floor, so surrogates will be INCOMPLETE and")
    print("clause (c) will fail. That is a construction mismatch, NOT evidence")
    print("about instrument bias.")
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
        print("The SAME-ASSET directed construction (own range location,")
        print("faded, next-open fill) is unbiased on series where nothing can be")
        print("timed. THIS SAYS NOTHING ABOUT EDGE.")
        return 0
    print("CONTROL: **INVALID — the same-asset directed instrument cannot be "
          "trusted.**")
    print("  failed: " + "; ".join(verdict.failures))
    print()
    print("No real-series percentile from this construction may be reported.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
