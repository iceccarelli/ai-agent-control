#!/usr/bin/env python3
"""Is the SAME-ASSET directed instrument honest for `sign_flip_momentum_v1`?

WHAT THIS RUNS
==============
The slice-43 / 46 / 49 / 50 same-asset directed control: whole bars shuffled and
re-based, each surrogate finding **its own** flip events, scored by
`edge_measurement.score_schedule` and ranked against `rotation_replicates`. The
three-clause rule (slice 37, unchanged) decides VALID or INVALID.

THIS IS THE FIRST RUN IN THREE SLICES THAT CAN ACTUALLY FAIL ON ITS MERITS
=========================================================================
The last two controls never got to say anything about the market:

* slice 49 (`open_gap_fade_v1`): `surrogate_series` re-bases with
  `scale = price / bar.open`, so every surrogate bar opens exactly at the prior
  close. The null **deleted** the trigger's quantity. 0 scoreable entries per
  surrogate.
* slice 50 (`ts_momentum_v1`): the null preserved the trigger, but a state true
  on nearly every bar forms one contiguous flag run and `simulate_schedule`
  takes one entry per run. 1 scoreable entry per surrogate.

Both were construction mismatches. Neither was evidence about bias.

**Predictions for this run, recorded in EDGE.md §33d at bee6946 and in the
count finding at aaeaa68, before this file existed:**

1. surrogates will find flips — a shuffled series still has a windowed return
   whose sign changes, and `surrogate_series` preserves close-to-close returns;
2. surrogates will schedule **more** flips than the real series, not fewer — a
   shuffled series has more sign alternation, not less;
3. so the incompletes clause should pass, and the control should return a real
   verdict about the instrument rather than about the construction.

If (1) or (2) is wrong, this family is INCONCLUSIVE for the same reason as the
last two and the report will say so in those words.

ON THE STANDING BIAS HYPOTHESIS
===============================
`docs/RESEARCH_HOLD.md` records that the same-asset control read cleanly (49.05)
on a return-based trigger and badly (+3.63) on a high/low-based one, with the
tentative explanation that the bias tracks how directly the trigger reads the
geometry the barrier prices its stops from. This trigger reads **closes only** —
the same shape as the clean case. If it comes back biased anyway, that
hypothesis is wrong, and saying so before the run is what makes the check worth
anything.

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
from signals import sign_flip_momentum_v1 as fade

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
    print("SAME-ASSET DIRECTED CONTROL — sign_flip_momentum_v1")
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
    print(f"  trigger          : CHANGE in the sign of close[t-{fade.SKIP}] / "
          f"close[t-{fade.SKIP + fade.LOOKBACK}] - 1; "
          f"flip to + -> LONG, flip to - -> SHORT (continuation)")
    print()
    print("Whole bars are shuffled and re-based, so each surrogate finds its")
    print("own flips. PREDICTED BEFORE THIS RUN (EDGE.md 33d at bee6946, the")
    print("count finding at aaeaa68): surrogates will find flips AND schedule")
    print("enough of them -- a shuffled series has MORE sign alternation than")
    print("a real one, not less -- so the incompletes clause should pass and")
    print("this control should return a real verdict about the instrument")
    print("rather than about the construction. That would be the first time in")
    print("three slices. If instead surrogates come back short, this family is")
    print("INCONCLUSIVE for the slice-50 reason and the report says so.")
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
        print("The SAME-ASSET directed construction (sign CHANGE of the own")
        print("10-bar windowed return, traded as CONTINUATION of the new sign,")
        print("next-open fill) is unbiased on series where nothing can be")
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
