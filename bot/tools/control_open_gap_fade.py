#!/usr/bin/env python3
"""Is the SAME-ASSET directed instrument honest for `open_gap_fade_v1`?

WHAT THIS RUNS, AND WHY IT IS EXPECTED TO REFUSE TO RUN
======================================================
The construction is the slice-43 / slice-46 same-asset directed control: whole
bars shuffled and re-based, each surrogate finding **its own** setups, scored by
`edge_measurement.score_schedule` and ranked against `rotation_replicates`. The
three-clause rule (slice 37, unchanged) decides VALID or INVALID.

**This tool is expected to report INVALID for a reason that is not instrument
bias, and that prediction is in git before it was ever run** (this docstring,
`tests/test_open_gap_fade_v1.TestTheSurrogateCannotExerciseThisTrigger`, and
EDGE.md 30d, committed at 6447fe5).

`skill_test.surrogate_series` re-bases every shuffled bar onto the running price
with

    scale = price / bar.open        price = previous surrogate bar's close

so the re-based open equals `bar.open * price / bar.open == price`, which is
**exactly** the prior close. On every bar. By construction.

For a trigger that reads `open[t] - close[t-1]` that is not a destroyed
relationship — it is a deleted quantity. Every surrogate has a gap of identically
zero, fires nothing, and is counted INCOMPLETE. Incompletes reach 100% and clause
(c) fails.

THE DISTINCTION THAT DECIDES THE VERDICT
========================================
"The control failed" and "the control could not be run" are different findings
and they license different conclusions:

* slice 46's `range_location_fade_v1` control was **exercised** a thousand times
  per symbol at 0% incompletes and came back biased at z = +3 to +4. That is an
  instrument-bias finding.
* this one is never exercised at all. Its INVALID says nothing about bias, about
  the fade, or about the market. It says the null and the trigger disagree about
  what a bar is.

Neither licenses a percentile. Both mean the family is INCONCLUSIVE, and this one
is doubly so: the count finding
(`artifacts/slice49_count_finding_open_gap_fade.log`) already showed 1 / 0 / 0
scoreable entries against a design target of 50, so there was nothing to
validate an instrument for.

WHAT IS NOT DONE ABOUT IT
=========================
The obvious repair — a surrogate that preserves each bar's open-to-prior-close
offset — is **not made here**. Changing a null after seeing which way its
verdict went is a construction chosen to fit a result, and
`docs/RESEARCH_HOLD.md` forbids it without a human pre-declaration written
before the run it enables. The defect is recorded for whoever writes that
declaration; it is not repaired inside the slice that discovered it.

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
from signals import open_gap_fade_v1 as fade

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
    print("SAME-ASSET DIRECTED CONTROL — open_gap_fade_v1")
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
    print(f"  trigger          : (open[t] - close[t-1]) / ATR_prev[t]; "
          f">= +{fade.GAP_K:g} -> SHORT, <= -{fade.GAP_K:g} -> LONG "
          f"(signal emitted at t-1 so the fill is the gap bar's own open)")
    print()
    print("Whole bars are shuffled and re-based, so each surrogate finds its")
    print("own gaps. PREDICTED BEFORE THIS RUN (see the module docstring and")
    print("tests/test_open_gap_fade_v1.py, committed at 6447fe5): it will find")
    print("NONE. surrogate_series re-bases with scale = price / bar.open, so")
    print("every surrogate bar opens exactly at the prior close and the gap is")
    print("identically zero. The null does not destroy this trigger's")
    print("information -- it deletes it. An INVALID verdict here is a")
    print("construction mismatch, NOT evidence about instrument bias.")
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
