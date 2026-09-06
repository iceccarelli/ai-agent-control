#!/usr/bin/env python3
"""Is the directed instrument honest for `funding_carry_fade_btc_v1` ON THE OOS WINDOW?

WHY A SEPARATE CONTROL RUN AT ALL
=================================
Slice 55 validated this construction on BTCUSDT over the full sample: z = +0.632,
KS p = 0.5386, 0% incompletes, mean surrogate percentile 51.27. It would be
convenient to cite that and move on. It would also be wrong.

A control is a statement about **a construction applied to a particular series**,
not a certificate that travels — slices 35 to 40 are the whole argument for that
sentence, and this programme paid four slices to learn it. The OOS window has 41
trades where the full sample had 85. Halving the trade count widens the rotation
null, changes the incomplete rate, and changes how much of the flag structure
survives a rotation confined to half the bars. None of that is knowable from the
full-sample control, so it is measured here.

THE CONSTRUCTION, WHICH IS SLICE 55'S UNCHANGED EXCEPT FOR THE WINDOW
====================================================================
Whole bars shuffled and re-based by `skill_test.surrogate_series`, which writes
each shuffled bar onto the **destination** position's timestamp. The surrogate
therefore keeps the original date sequence under a scrambled price path, and the
untouched funding series joins at the same positions — the arrangement EDGE.md
§36f required. The tool asserts length and every timestamp and ABORTS rather
than reporting a percentile from a mis-joined series.

Two things are new, and both are about the window:

* **flags are restricted to the late window**, so a surrogate's observed
  schedule is the same 41-trade schedule the real run uses;
* **`eligible` is intersected with the late window**, so rotations are confined
  to the OOS bars. Leaving it spanning the full history would let replicates
  land on early dates the real rule was forbidden to trade — which is not a null
  for this measurement, it is a comparison between two different strategies, and
  it would systematically favour whichever half of the history happened to be
  kinder.

THE PREDICTION, RECORDED BEFORE THIS RUN
========================================
EDGE.md §39d, committed before the module existed:

    "slice 55 measured this construction's behaviour on BTCUSDT at 85 trades
    and got z = +0.632, mean surrogate percentile 51.27 — clean. The OOS window
    has roughly half the trades, so the rotation null is wider and the small
    structural offset from charging the observed schedule the carry it collects
    (EDGE.md §37e) will be HARDER to detect, not easier. The prediction is
    therefore that the OOS control passes clause (a) comfortably, and that the
    risk to this control is clause (c) — incompletes — from the smaller sample,
    not bias."

Restated concretely so it can be scored rather than remembered: **z should come
in at or below slice 55's +0.632 in magnitude, and if this control fails it
should fail on incompletes.** If it instead fails clause (a) upward, the
prediction was wrong and the reasoning in §37e about the carry offset needs
revisiting by a human — not patching here.

WHAT HAPPENS IF IT IS INVALID
=============================
No M1 and no M2 are computed for the OOS window, the edge verdict is
INCONCLUSIVE, and the null is **not** repaired mid-slice. The cut is not moved.
Nothing is re-run with a different seed.

THE CRITERION (human pre-declaration, slice 37, never relaxed)
==============================================================
    (a) |z| < 1.96   two-sided, mean of surrogate percentiles vs 50,
                     SE = sample sd / sqrt(n_surrogates)
    (b) KS vs U(0,100) NOT rejected at p >= 0.05
    (c) incompletes <= 5% of surrogates

`median <= 50` is retired and informational. There is no fourth clause and one
is not added after seeing these numbers.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import List

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import numpy as np  # noqa: E402

import backtest as bt  # noqa: E402
import market_data as md  # noqa: E402
import skill_test as sk  # noqa: E402
from signals import funding_carry_fade_btc_v1 as fb  # noqa: E402

import control_directed as cd  # noqa: E402
import edge_measurement as em  # noqa: E402


def positions_preserved(original, shuffled) -> bool:
    """Did the shuffle keep every bar on its own date?

    The funding join is by bar close time and the fold cut is by bar time, so
    this underpins BOTH the "funding stays attached to the same position" claim
    and the "the late window is the same set of dates" one.
    `surrogate_series` drops any bar with a non-positive open; one drop shifts
    every later position, joining funding to the wrong day AND moving the cut.
    """
    return (len(shuffled) == len(original)
            and all(a.start_ms == b.start_ms
                    for a, b in zip(original, shuffled)))


def build(bars, funding, folds, *, warmup, args):
    """Exactly what `edge_measurement` builds for the OOS run, same calls."""
    flags, directions = fb.flags_and_directions(bars, funding, warmup=warmup)
    flags = fb.restrict_flags_to_late(flags, bars, folds)

    side_lookup = {}
    side_net = {}
    for tag, side in ((fb.LONG_SETUP, "long"), (fb.SHORT_SETUP, "short")):
        idx, net, used = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=args.take_profit_atr, stop_atr=args.stop_atr,
            horizon=args.horizon, atr_period=args.atr_period,
            round_trip_bps=args.round_trip_bps, side=side,
            entry_on=fb.ENTRY_ON)
        adjusted = fb.funding_adjusted_net_r(
            bars, funding, idx, net, used, tag,
            stop_atr=args.stop_atr, atr_period=args.atr_period)
        side_lookup[tag] = {int(i): p for p, i in enumerate(idx)}
        side_net[tag] = adjusted

    eligible = np.zeros(len(bars), dtype=bool)
    common = set(side_lookup[fb.LONG_SETUP]) & set(side_lookup[fb.SHORT_SETUP])
    eligible[sorted(common)] = True
    eligible[:warmup] = False

    late = np.zeros(len(bars), dtype=bool)
    late[list(fb.late_window_indices(bars, folds))] = True
    eligible = eligible & late

    return (flags, eligible, directions, side_lookup, side_net,
            side_lookup[fb.LONG_SETUP], side_net[fb.LONG_SETUP])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir",
                        default=os.path.join(REPO, "data", "real_linear_1d"))
    parser.add_argument("--funding-data", default=os.path.join(
        REPO, "data", "real_funding", "funding",
        "BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz"))
    parser.add_argument("--folds", default=fb.FOLDS_PATH)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--control-runs", type=int, default=200)
    parser.add_argument("--runs", type=int, default=1000)
    parser.add_argument("--lockup", type=int, default=fb.LOCKUP)
    parser.add_argument("--horizon", type=int, default=fb.HORIZON)
    parser.add_argument("--atr-period", type=int, default=fb.ATR_PERIOD)
    parser.add_argument("--stop-atr", type=float, default=fb.STOP_ATR)
    parser.add_argument("--take-profit-atr", type=float,
                        default=fb.TAKE_PROFIT_ATR)
    parser.add_argument("--round-trip-bps", type=float,
                        default=fb.ROUND_TRIP_BPS)
    parser.add_argument("--warmup", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20250730)
    parser.add_argument("--min-trades", type=int, default=20,
                        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    fb.require_supported_symbol(args.symbol)
    folds = fb.load_folds(args.folds)
    if not fb.folds_are_unmodified(args.folds):
        print("REFUSING: the fold calendar's sha256 is not the one in the "
              "lock log.")
        return 2

    loaded, _b, _n = md.load_corpus(args.data_dir, verify=False)
    bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
            for b in loaded[args.symbol]]
    funding = fb.load_funding(args.funding_data)

    print("=" * 78)
    print("OOS DIRECTED CONTROL — funding_carry_fade_btc_v1")
    print("=" * 78)
    print(f"  symbol           : {args.symbol} ({len(bars):,} bars)")
    print(f"  window           : OOS ONLY  [{folds['t_mid']} .. {folds['t1']}]"
          f"   {folds['n_bars_late']:,} bars")
    print(f"  folds sha256     : {fb.folds_sha256(args.folds)}  (matches the "
          f"lock log)")
    print(f"  surrogates       : {args.control_runs}")
    print(f"  rotations each   : {args.runs}")
    print(f"  barrier          : +{args.take_profit_atr:g}/-{args.stop_atr:g} "
          f"ATR, horizon {args.horizon}, {args.round_trip_bps:g} bps PLUS "
          f"funding over the hold, entry {fb.ENTRY_ON}")
    print()
    print("A control is a statement about a construction on a PARTICULAR")
    print("series, not a certificate that travels. Slice 55 validated this")
    print("construction on BTC over 85 full-sample trades (z +0.632, KS p")
    print("0.5386, 0% incomplete). The OOS window has 41. Halving the trade")
    print("count widens the rotation null and changes the incomplete rate, so")
    print("it is measured here rather than cited from there.")
    print()
    print("PREDICTED BEFORE THIS RUN (EDGE.md 39d, committed before the")
    print("module existed): the smaller sample makes the carry offset HARDER")
    print("to detect, so |z| should come in at or below slice 55's 0.632 and")
    print("the risk to this control is clause (c) INCOMPLETES, not bias. If")
    print("it fails clause (a) upward instead, the prediction was wrong and")
    print("that goes to a human, not into a patch in this file.")
    print()
    print("Rotations are confined to the OOS window. Letting replicates land")
    print("on early dates the real rule was forbidden to trade would not be a")
    print("null for this measurement — it would compare two strategies.")
    print()

    percentiles: List[float] = []
    incomplete = 0
    for run in range(1, args.control_runs + 1):
        shuffled = sk.surrogate_series(bars, seed=args.seed + 1000 + run)
        if not positions_preserved(bars, shuffled):
            print(f"  surrogate {run}: REFUSED — the shuffle did not preserve "
                  f"the date sequence, so both the funding join and the fold "
                  f"cut would land on the wrong bars.")
            print()
            print("ABORTED. No percentile is reported.")
            return 2
        (flags, eligible, directions, side_lookup, side_net,
         lookup, net_r) = build(shuffled, funding, folds,
                                warmup=args.warmup, args=args)
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
              "as a gate in slice 37 and decides nothing.")
    print()
    if verdict.valid:
        print("CONTROL: **VALID** — all three pre-declared clauses hold.")
        print()
        print("The OOS directed construction is unbiased on series where")
        print("nothing can be timed. THIS SAYS NOTHING ABOUT EDGE — it says")
        print("the ruler may now be read.")
        return 0
    print("CONTROL: **INVALID — the instrument cannot be trusted on this "
          "window.**")
    print("  failed: " + "; ".join(verdict.failures))
    print()
    print("No OOS percentile may be reported. There is NO M1 and NO M2, the")
    print("verdict is INCONCLUSIVE, the cut is NOT moved and the null is NOT")
    print("repaired in this slice.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
