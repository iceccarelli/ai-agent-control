#!/usr/bin/env python3
"""Is the SAME-ASSET directed instrument honest? The control slice 46 owes.

WHY THIS EXISTS AND WHY THE OLD CONTROL DOES NOT COVER IT
=========================================================
`btc_alt_spillover_v1`'s control shuffles the *traded* series and leaves the BTC
driver alone, so the signal fires on the same calendar dates in every surrogate.
Only the price path being traded changes.

`range_location_fade_v1` triggers on where the symbol's **own** close sits in
its **own** trailing range. Shuffling the series moves the ranges and the
extremes with it: a surrogate's observed schedule is the surrogate's *own*
flags, computed from the shuffled bars. That is the same shape of construction
`tools/control_post_shock_fade.py` was built for in slice 43 — and a different
one from the cross-asset control, which is not reused.

WHY THIS IS A SECOND FILE RATHER THAN A FLAG
============================================
It duplicates the *runner* of `control_post_shock_fade.py` and shares all of
the *arithmetic*: `evaluate_control` (the three-clause rule),
`edge_measurement.score_schedule`, `rotation_replicates` and `tradable_flags`
are imported, not copied. The duplication is therefore ~80 lines of argument
parsing and printing, and the alternative — parameterising the slice-43 tool by
signal — would have meant editing a module whose AST is asserted by the tests
of a now-frozen family, for no measurement benefit. `tests/` asserts that both
controls call the same scoring functions, so the part that matters cannot
drift.

THE TEST
========
`surrogate_series` shuffles whole bars and re-bases them onto a running price,
preserving the return distribution and total drift and destroying every
relationship *between* bars. On such a series a close sitting at the top of its
trailing range says nothing about the next five days, because the next five
days were drawn from a hat. A correct instrument must therefore report
percentiles uniform on [0, 100].

Note what is being asked: not "does the fade work" but "when nothing can be
timed, does the ruler say so". A ruler that reports high percentiles here is
measuring itself.

THE CRITERION (human pre-declaration, slice 37, unchanged)
==========================================================
CONTROL VALID if and only if ALL THREE hold:

    (a) |z| < 1.96   two-sided, mean of surrogate percentiles vs 50,
                     SE = sample sd / sqrt(n_surrogates)
    (b) Kolmogorov-Smirnov vs Uniform(0, 100) NOT rejected at p >= 0.05
    (c) incompletes <= 5% of surrogates

`median <= 50` is retired and informational. There is no fourth clause, and one
is not to be added after seeing these numbers.

THE INHERITED DEFECT
====================
The CROSS-ASSET directed machinery carries ~1.5 percentile points of
unexplained UPWARD bias (EDGE.md 22d). Slice 43 narrowed it: the same-asset
control read 49.05, below 50, so the bias is not generic to directed
constructions. This construction is same-asset, so the evidence says it is
probably clean -- but "probably clean" is not "validated", which is why this
runs first and per symbol. Nothing here fixes the cross-asset bias: fixing a
measurement instrument requires a human pre-declaration made before the run it
enables, and none exists.
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
from signals import range_location_fade_v1 as fade

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
    print("SAME-ASSET DIRECTED CONTROL — range_location_fade_v1")
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
    print(f"  trigger          : loc >= {fade.UPPER:g} -> SHORT, "
          f"loc <= {fade.LOWER:g} -> LONG; range {fade.RANGE_BARS} bars "
          f"(t-{fade.RANGE_BARS}..t-1, bar t excluded)")
    print()
    print("Whole bars are shuffled and re-based, so the ranges and their")
    print("extremes move WITH the series -- each surrogate finds its own. On a")
    print("series with no temporal structure a close at the top of its range")
    print("says nothing about the next five days, so a correct instrument must")
    print("report percentiles uniform on [0, 100].")
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
