#!/usr/bin/env python3
"""Is the directed instrument honest for `funding_carry_fade_v1`?

WHAT THIS RUNS, AND WHY ITS SHAPE IS NEW
========================================
Every prior same-asset control shuffled whole bars and re-based them, and each
surrogate then found **its own** setups, because the trigger was computed from
the bars being shuffled. This family's trigger is not in the bars at all: it is
a funding print from a separate corpus, joined to the daily decision clock.

`EDGE.md` §36f, committed at d18a3b8 before any of this code existed, wrote down
what the control therefore has to do:

    "The control must therefore shuffle the bars while keeping each surrogate's
    funding attached **to the same position in the sequence**, so that the
    relationship between funding and the subsequent price path is destroyed
    while the marginal distribution of both is preserved. If that is not
    achievable cleanly, the honest outcome is INCONCLUSIVE — not a percentile
    from a null whose construction I improvised after seeing that the obvious
    one did not fit."

It is achievable cleanly, and by an accident of an existing function rather than
by anything invented here. `skill_test.surrogate_series` writes each shuffled
bar onto `bars[position].start_ms` — the timestamp of the **destination**
position, not of the source bar. The surrogate therefore carries the original
date sequence with a scrambled price path laid over it, and joining the
untouched funding series to it attaches the same rate to the same position. No
new null construction, no new join.

That claim is load-bearing, so this tool checks it instead of asserting it in
prose: `surrogate_series` skips any bar with a non-positive open, and a single
skip would shift every later position by one and silently break the attachment.
If the length or the timestamps differ, this tool **refuses to run** rather than
reporting a percentile from a series whose funding is off by a day.

WHAT THIS NULL IS, STATED PLAINLY
=================================
Because the trigger reads only funding, and funding is untouched by the shuffle,
**the flag array is identical on every surrogate.** Each surrogate scores that
one schedule against 1,000 rotations of itself, on a price path with no temporal
structure. The between-surrogate variation is entirely the price path.

That is the right null for the question the instrument asks — *do these dates
beat other dates?* — and it is exactly the arrangement §36f specified. It is
also, deliberately, not a test of anything else.

THE PREDICTION, RECORDED BEFORE THIS RUN
========================================
**This control is at elevated risk of failing clause (a) in the POSITIVE
direction, for a reason that is not instrument bias.**

The mechanism is the cost model, and it is visible without running anything:

* the rule goes SHORT exactly when `f >= +FUND_ABS` and LONG exactly when
  `f <= -FUND_ABS`, and a short receives positive funding while a long receives
  negative funding. So **the observed schedule collects carry on every single
  trade, by construction** — the funding adjustment to net R is positive at
  every observed entry, and at least `FUND_ABS` in size;
* a null replicate takes its direction from the recycled observed direction
  sequence (`score_schedule(..., directed_mode="sequence")`), so a rotated entry
  lands on a bar whose funding sign has nothing to do with the direction it is
  handed. Its adjustment averages far nearer zero.

The observed side therefore carries a systematic positive offset that survives
on a surrogate where nothing whatever can be timed. `artifacts/slice55_count_
finding_funding_carry_fade.log` (committed at 0a21edf) sizes it: **+0.0153 R on
BTCUSDT**, against a rotation spread in mean R of order `1/sqrt(85) ~ 0.11 R`.
Roughly a seventh of a standard deviation per surrogate — small, but the **same
sign every time**, which is the one thing a z over 200 surrogates is built to
find.

This is not slice 46's finding restated. Slice 46 found the *instrument* biased:
a high/low trigger leaking into a barrier priced off the same geometry. Here the
instrument is fine and the offset is **the carry itself**, appearing inside a
null that was designed to test price timing. Both are reasons a percentile
cannot be read; they are not the same reason, and this file says so before the
numbers rather than after.

WHAT IS REFUSED IN ADVANCE, WHICHEVER WAY IT GOES
=================================================
* **Clause (a) fails positively** -> that symbol is INCONCLUSIVE and no
  real-series percentile is reported for it. The null is **not** repaired in
  this slice. In particular the obvious "fix" — drop the funding cost so both
  sides are charged nothing — is refused now, before it can be tempting: the
  intake and §36d both mandate the cost, and a cost removed after seeing a
  control fail is a parameter fitted to a result. Whether a different null is
  the right one for a carry signal is a construction question for a human
  pre-declaration in a later slice;
* **all three clauses hold** -> the offset is smaller than the estimate above,
  and the real-series percentiles may be read for that symbol. That is a real
  possibility and this file is not written to make the failure sound inevitable;
* **clause (c) fails** -> reported as it falls. On a shuffled path a barrier can
  resolve differently, but eligibility here is dominated by the horizon tail, so
  this is not expected to bind.

None of the three outcomes is evidence about funding. A control is a statement
about a construction on a series where the answer is known in advance to be
nothing.

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
from signals import funding_carry_fade_v1 as fund

import control_directed as cd
import edge_measurement as em

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

UNIVERSE = {
    "BTCUSDT": "BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz",
    "ETHUSDT": "BINANCE_LINEAR_ETH_USDT_FUNDING.csv.gz",
    "SOLUSDT": "BINANCE_LINEAR_SOL_USDT_FUNDING.csv.gz",
}


def positions_preserved(original, shuffled) -> bool:
    """Did the shuffle keep every bar on its own date?

    The funding join is by bar close time, so this is the whole basis of the
    "funding stays attached to the same position" claim. `surrogate_series`
    drops any bar with a non-positive open; one drop shifts every later position
    and would join each surrogate's funding to the wrong day — invisibly, and in
    a direction nobody could predict.
    """
    return (len(shuffled) == len(original)
            and all(a.start_ms == b.start_ms
                    for a, b in zip(original, shuffled)))


def build_book_and_flags(bars, funding, *, warmup, args):
    """Exactly what `edge_measurement` builds for this signal, via the same calls.

    Including the funding adjustment: it is folded into the per-side net R
    arrays before anything is scored, so the observed schedule and every
    replicate index into the same charged numbers (§36d). A control that scored
    an uncharged surrogate would be validating a different instrument from the
    one that produces the edge numbers.
    """
    flags, directions = fund.flags_and_directions(bars, funding, warmup=warmup)

    side_lookup = {}
    side_net = {}
    for tag, side in ((fund.LONG_SETUP, "long"), (fund.SHORT_SETUP, "short")):
        idx, net, used = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=args.take_profit_atr, stop_atr=args.stop_atr,
            horizon=args.horizon, atr_period=args.atr_period,
            round_trip_bps=args.round_trip_bps, side=side,
            entry_on=fund.ENTRY_ON,
        )
        adjusted = fund.funding_adjusted_net_r(
            bars, funding, idx, net, used, tag,
            stop_atr=args.stop_atr, atr_period=args.atr_period)
        side_lookup[tag] = {int(i): p for p, i in enumerate(idx)}
        side_net[tag] = adjusted

    eligible = np.zeros(len(bars), dtype=bool)
    common = set(side_lookup[fund.LONG_SETUP]) & set(side_lookup[fund.SHORT_SETUP])
    eligible[sorted(common)] = True
    eligible[:warmup] = False

    return (flags, eligible, directions, side_lookup, side_net,
            side_lookup[fund.LONG_SETUP], side_net[fund.LONG_SETUP])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir",
                        default=os.path.join(REPO, "data", "real_linear_1d"))
    parser.add_argument("--funding-dir",
                        default=os.path.join(REPO, "data", "real_funding",
                                             "funding"))
    parser.add_argument("--funding-data", default=None,
                        help="explicit funding CSV; defaults by symbol")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--control-runs", type=int, default=200)
    parser.add_argument("--runs", type=int, default=1000)
    parser.add_argument("--lockup", type=int, default=fund.LOCKUP)
    parser.add_argument("--horizon", type=int, default=fund.HORIZON)
    parser.add_argument("--atr-period", type=int, default=fund.ATR_PERIOD)
    parser.add_argument("--stop-atr", type=float, default=fund.STOP_ATR)
    parser.add_argument("--take-profit-atr", type=float,
                        default=fund.TAKE_PROFIT_ATR)
    parser.add_argument("--round-trip-bps", type=float,
                        default=fund.ROUND_TRIP_BPS)
    parser.add_argument("--warmup", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20250730)
    parser.add_argument("--min-trades", type=int, default=20,
                        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    # verify=False: this corpus's MANIFEST carries no per-file checksum block,
    # and `verify_manifest` was NOT weakened to admit it. The files are pinned
    # by sha256 in artifacts/slice55_data_eligibility.json instead, and asserted
    # by tests on every run. See tools/verify_slice55_corpora.py.
    loaded, _b, _n = md.load_corpus(args.data_dir, verify=False)
    symbol = args.symbol
    bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
            for b in loaded[symbol]]
    funding_path = args.funding_data or os.path.join(
        args.funding_dir, UNIVERSE[symbol])
    funding = fund.load_funding(funding_path)

    counts = fund.summary(bars, funding, warmup=args.warmup)

    print("=" * 78)
    print("SAME-ASSET DIRECTED CONTROL — funding_carry_fade_v1")
    print("=" * 78)
    print(f"  symbol           : {symbol} ({len(bars):,} bars) "
          f"from {os.path.relpath(args.data_dir, REPO)}")
    print(f"  funding          : {counts['funding_prints']:,} prints from "
          f"{os.path.basename(funding_path)}")
    print(f"  real-series setups: {counts['setups']}  "
          f"({counts['long_setups']} long / {counts['short_setups']} short)")
    print(f"  surrogates       : {args.control_runs}")
    print(f"  rotations each   : {args.runs}")
    print(f"  barrier          : +{args.take_profit_atr:g}/-{args.stop_atr:g} "
          f"ATR, horizon {args.horizon}, {args.round_trip_bps:g} bps, "
          f"entry {fund.ENTRY_ON}, PLUS funding over the hold")
    print(f"  trigger          : |f| >= {fund.FUND_ABS:g} at the last print on "
          f"or before the bar close; rich long carry -> SHORT")
    print()
    print("Whole bars are shuffled and re-based onto the ORIGINAL date")
    print("sequence, so the untouched funding series joins each surrogate at")
    print("the same position — the arrangement EDGE.md 36f required at d18a3b8.")
    print("The trigger reads no price, so every surrogate carries the SAME flag")
    print("array and the variation between them is purely the price path.")
    print()
    print("PREDICTED BEFORE THIS RUN (this file's docstring, committed before")
    print("it was executed): ELEVATED RISK of failing clause (a) POSITIVELY,")
    print("and NOT for instrument bias. The observed schedule collects carry on")
    print("every trade by construction (+0.0153 R on BTCUSDT per the count")
    print("finding), while a rotated replicate is handed a direction unrelated")
    print("to the funding at the bar it lands on. A small offset with the same")
    print("sign on every surrogate is what a z over 200 of them detects.")
    print("If it fails: INCONCLUSIVE for this symbol, and the funding cost is")
    print("NOT dropped to rescue the null. If it passes: the offset is smaller")
    print("than that estimate and the percentiles may be read.")
    print()

    percentiles: List[float] = []
    incomplete = 0
    for run in range(1, args.control_runs + 1):
        shuffled = sk.surrogate_series(bars, seed=args.seed + 1000 + run)
        if not positions_preserved(bars, shuffled):
            print(f"  surrogate {run}: REFUSED — the shuffle did not preserve "
                  f"the date sequence ({len(shuffled):,} of {len(bars):,} "
                  f"bars), so funding would join the wrong days.")
            print()
            print("ABORTED. No percentile is reported from a series whose "
                  "funding attachment is not the one 36f specified.")
            return 2
        (flags, eligible, directions, side_lookup, side_net,
         lookup, net_r) = build_book_and_flags(
            shuffled, funding, warmup=args.warmup, args=args)
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
        print("The directed construction (funding-triggered fade on the linear")
        print("daily series, funding charged over the hold, next-open fill) is")
        print("unbiased on series where nothing can be timed. The carry offset")
        print("predicted above is present but too small to move the mean.")
        print("THIS SAYS NOTHING ABOUT EDGE.")
        return 0
    print("CONTROL: **INVALID — the directed instrument cannot be trusted for "
          "this family.**")
    print("  failed: " + "; ".join(verdict.failures))
    print()
    print("No real-series percentile from this construction may be reported")
    print("for this symbol. The null is NOT repaired in this slice and the")
    print("funding cost is NOT dropped — see this file's docstring, written")
    print("before the run.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
