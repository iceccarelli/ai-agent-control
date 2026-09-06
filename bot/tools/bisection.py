#!/usr/bin/env python3
"""Does the analyser's SELECTION carry the residual, or did the old map?

THE FACT BEING RE-MEASURED
==========================
Slice 13 ran a bisection whose result has anchored every slice since:

    information-free flags of identical shape  ->  median 15.4  (BELOW its null)
    the real analyser's flags                  ->  median 72.7  (ABOVE its null)

on surrogate series with **no temporal structure**, where nothing can be timed.
That asymmetry is the single strongest clue about where the skill test's
residual bias lives, because no story about the *scoring* can explain it: the
scoring is byte-for-byte identical on both sides. It has to be about what the
analyser's rule selects.

But it was measured under the multi-entry-per-run `simulate_schedule` that
slice 16 showed was defective and slice 17 replaced. Under that map, of 54
observed entries only 26 were run starts; the other 28 were interior bars of
long runs, reached because a lock-up expired there. The bisection's two sides
were therefore compared under a map the project has since rejected, and the
clue every subsequent hypothesis has been built on has never been re-measured.

THE TWO SIDES
-------------
Both run the identical current pipeline — one trade per contiguous run, the
block-resample null, the same barrier arithmetic, the same costs, the same
sampling-floor tolerance:

    Side A   the real analyser's flags on the surrogate
    Side B   flags with the SAME multiset of run lengths and the SAME multiset
             of gaps, interleaved at random, carrying no information about the
             bars at all

Side B is built by :func:`skill_test.shape_matched_flags`, which takes no bars,
no prices and no strata — only two multisets of integers and a generator. Its
independence from the price path is structural, not argued.

WHAT EACH OUTCOME WOULD MEAN
----------------------------
If the asymmetry survives — A materially above 50, B at or below it — the
residual is a property of what the analyser selects, and the old map was
incidental. If it collapses — both sides at the same level — then the old
multi-entry map was a material part of the artefact and the residual has to be
re-characterised from scratch under the current instrument.

Neither outcome is assumed. The decision rule below was fixed before any number
was produced.

Usage
-----
    python3 tools/bisection.py --surrogates 12 --runs 1500
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import backtest as bt
import market_data as md
import skill_test as sk

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass(frozen=True)
class BisectionRow:
    """One surrogate, both sides. Every field is measured, none inferred."""

    seed: int
    n_runs: int
    percentile_a: Optional[float]
    percentile_b: Optional[float]
    trades_a: int
    trades_b: int
    acceptance_a: float
    acceptance_b: float

    @property
    def contrast(self) -> Optional[float]:
        if self.percentile_a is None or self.percentile_b is None:
            return None
        return self.percentile_a - self.percentile_b


def run_side(
    bars: Sequence,
    cfg: "bt.BacktestConfig",
    *,
    flags: Optional[np.ndarray],
    runs: int,
    seed: int,
    lockup: int,
    horizon: int,
    atr_period: int,
    strata_bins: int,
    block_tries: int,
    block_nodes: int,
    block_target: int,
    null_kind: str = "block_resample",
) -> Optional[sk.SkillVerdict]:
    """One side of the bisection, through the untouched current pipeline.

    ``flags=None`` is Side A and takes the ordinary path: ``run_permutation``
    calls the real analyser itself. Anything else is Side B. Every other
    argument is identical between the two calls, which is the entire point —
    the only difference the comparison is allowed to contain is what the flags
    are.

    ``null_kind`` is threaded through so slice 21 can drive BOTH sides through
    the plain ``rotation`` null — the one with no geometry constraint, no
    placement search and therefore no acceptance filter. It is a passthrough
    and nothing else: the caller supplies one value which both sides receive,
    so the two calls can never differ in which null they were ranked against.
    """
    return sk.run_permutation(
        bars, cfg, runs=runs, seed=seed, round_trip_bps=25.0,
        take_profit_atr=4.0, stop_atr=2.0, horizon=horizon,
        atr_period=atr_period, warmup=cfg.warmup_bars, lockup=lockup,
        strata_bins=strata_bins, null_kind=null_kind,
        block_tries=block_tries, block_nodes=block_nodes,
        block_target=block_target, flags_override=flags,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=os.path.join(REPO, "data", "real_1d"))
    parser.add_argument("--interval", default="D")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--surrogates", type=int, default=12)
    parser.add_argument("--runs", type=int, default=1500)
    parser.add_argument("--seed", type=int, default=20250727)
    parser.add_argument("--horizon", type=int, default=24)
    parser.add_argument("--atr-period", type=int, default=14)
    parser.add_argument("--strata-bins", type=int, default=5)
    # Lock-up 1 is the configuration in which entries ARE run starts and the
    # null's entry geometry matches the observed one to TV 0.0000 exactly. It
    # is the only configuration in which the block-resample null is a perfect
    # match on all three constraints, so it is the one the bisection uses.
    parser.add_argument("--lockup", type=int, default=1)
    parser.add_argument("--block-tries", type=int, default=20_000)
    parser.add_argument("--block-nodes", type=int, default=2_000)
    parser.add_argument("--block-target", type=int, default=400)
    # Which null BOTH sides are ranked against. Default unchanged for backward
    # compatibility with the slice-19 numbers in EDGE.md 4l; `rotation` is the
    # slice-21 discriminator because it has almost no construction machinery.
    parser.add_argument("--null", dest="null_kind", default="block_resample",
                        choices=("rotation", "matched", "stratified_rotation",
                                 "block_resample"))
    parser.add_argument("--min-confidence", type=float, default=0.12)
    parser.add_argument("--min-agreement", type=float, default=0.40)
    args = parser.parse_args(argv)

    loaded, _books, notes = md.load_corpus(args.data_dir)
    symbol = args.symbol or sorted(loaded)[0]
    bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
            for b in loaded[symbol]]

    print("=" * 78)
    print("BISECTION — re-run under the CURRENT instrument (slice 19)")
    print("=" * 78)
    for note in notes:
        print(note)
    print(f"symbol            : {symbol}")
    print(f"bars              : {len(bars):,}")
    print()
    print("Side A : the real analyser's flags on the surrogate")
    print("Side B : the SAME multiset of run lengths and the SAME multiset of")
    print("         gaps, interleaved at random, carrying NO information about")
    print("         the bars (shape_matched_flags takes no prices at all)")
    print()
    print(f"map    : one trade per contiguous run, lock-up {args.lockup}")
    print(f"null   : {args.null_kind}, {args.runs:,} replicates per side")
    print("Both sides run byte-identical machinery. The ONLY difference is the")
    print("flags. Slice 13 measured 72.7 / 15.4 for these two sides under the")
    print("OLD multi-entry map; this asks whether that survives the new one.")
    print()

    cfg = bt.BacktestConfig(
        starting_cash=10_000.0, warmup_bars=200, interval=args.interval,
        min_confidence=args.min_confidence,
        min_component_agreement=args.min_agreement,
    )

    rows: List[BisectionRow] = []
    for offset in range(args.surrogates):
        seed = args.seed + 1000 + offset
        surrogate = sk.surrogate_series(bars, seed=seed)

        # ---- STEP 1: the analyser's block structure on THIS surrogate -----
        real_flags = sk.signal_flags(surrogate, cfg, warmup=cfg.warmup_bars)
        if not real_flags.any():
            print(f"  surrogate {offset + 1:2d}: analyser never fired — skipped")
            continue
        runs_observed, gaps_observed = sk.extract_blocks(
            real_flags, warmup=cfg.warmup_bars
        )
        run_lengths = np.array([length for _, length in runs_observed],
                               dtype=np.int64)
        gaps_array = np.array(gaps_observed, dtype=np.int64)

        # ---- STEP 2: information-free flags of identical shape ------------
        synthetic = sk.shape_matched_flags(
            run_lengths, gaps_array, real_flags.size,
            warmup=cfg.warmup_bars, rng=np.random.default_rng(seed ^ 0xB15EC7),
        )
        # Assert the shape identity here as well as in the tests: a silent
        # mismatch would make the whole comparison meaningless, and it costs
        # nothing to check it on the data actually used.
        synthetic_runs, synthetic_gaps = sk.extract_blocks(
            synthetic, warmup=cfg.warmup_bars
        )
        assert sorted(length for _, length in synthetic_runs) == \
            sorted(length for _, length in runs_observed), "run multiset drift"
        assert sorted(synthetic_gaps) == sorted(gaps_observed), "gap multiset drift"
        assert int(synthetic.sum()) == int(real_flags.sum()), "flag count drift"

        # ---- STEP 3: score both sides through the identical pipeline ------
        common = dict(
            runs=args.runs, seed=seed, lockup=args.lockup, horizon=args.horizon,
            atr_period=args.atr_period, strata_bins=args.strata_bins,
            block_tries=args.block_tries, block_nodes=args.block_nodes,
            block_target=args.block_target, null_kind=args.null_kind,
        )
        side_a = run_side(surrogate, cfg, flags=None, **common)
        side_b = run_side(surrogate, cfg, flags=synthetic, **common)

        def readable(verdict):
            if verdict is None or verdict.runs == 0:
                return None, 0, 0.0
            return verdict.percentile, verdict.n_trades, verdict.acceptance_rate

        pa, ta, aa = readable(side_a)
        pb, tb, ab = readable(side_b)
        row = BisectionRow(
            seed=seed, n_runs=len(runs_observed),
            percentile_a=pa, percentile_b=pb,
            trades_a=ta, trades_b=tb, acceptance_a=aa, acceptance_b=ab,
        )
        rows.append(row)
        print(f"  surrogate {offset + 1:2d}: "
              f"A {('%5.1f' % pa) if pa is not None else '  n/a'} "
              f"({ta:2d} trades, accept {aa:5.1%})  |  "
              f"B {('%5.1f' % pb) if pb is not None else '  n/a'} "
              f"({tb:2d} trades, accept {ab:5.1%})  |  "
              f"runs {row.n_runs}"
              + (f"  contrast {row.contrast:+.1f}" if row.contrast is not None
                 else "  contrast n/a"))

    usable = [r for r in rows if r.contrast is not None]
    if len(usable) < 3:
        print()
        print("C — INCONCLUSIVE: fewer than 3 surrogates produced both sides.")
        return 2

    a_values = np.array([r.percentile_a for r in usable], dtype=float)
    b_values = np.array([r.percentile_b for r in usable], dtype=float)
    contrasts = np.array([r.contrast for r in usable], dtype=float)

    print()
    print("=" * 78)
    print("PER-SURROGATE")
    print("=" * 78)
    print(f"{'seed':>10} {'runs':>5} {'A pct':>7} {'B pct':>7} {'A-B':>8} "
          f"{'A trades':>9} {'B trades':>9}")
    for r in usable:
        print(f"{r.seed:>10} {r.n_runs:>5} {r.percentile_a:>7.1f} "
              f"{r.percentile_b:>7.1f} {r.contrast:>+8.1f} "
              f"{r.trades_a:>9} {r.trades_b:>9}")

    print()
    print("=" * 78)
    print("POOLED")
    print("=" * 78)
    print(f"Side A (real analyser flags)   : median {np.median(a_values):5.1f}  "
          f"mean {a_values.mean():5.1f}  sd {a_values.std(ddof=1):5.1f}")
    print(f"Side B (info-free, same shape) : median {np.median(b_values):5.1f}  "
          f"mean {b_values.mean():5.1f}  sd {b_values.std(ddof=1):5.1f}")
    print(f"contrast A-B                   : median {np.median(contrasts):+5.1f}  "
          f"mean {contrasts.mean():+5.1f}")
    print(f"surrogates with A > B          : {int((contrasts > 0).sum())} "
          f"of {len(usable)}")

    # A percentile is uniform on [0,100] under its own null, so sd is 28.87 and
    # the mean of n draws has that over sqrt(n). Reported for BOTH sides,
    # because the question is where each one sits, not only where A sits.
    standard_error = 28.87 / np.sqrt(len(usable))
    z_a = (a_values.mean() - 50.0) / standard_error
    z_b = (b_values.mean() - 50.0) / standard_error
    print(f"uniformity z vs 50             : A {z_a:+.2f}   B {z_b:+.2f}   "
          f"(n = {len(usable)})")

    # Paired sign test on the contrast: no distributional assumption, and the
    # pairing is real (both sides ran on the SAME surrogate).
    from math import comb
    positives = int((contrasts > 0).sum())
    n_pairs = int((contrasts != 0).sum())
    if n_pairs:
        tail = sum(comb(n_pairs, k)
                   for k in range(min(positives, n_pairs - positives) + 1))
        sign_p = min(1.0, 2.0 * tail / (2.0 ** n_pairs))
    else:
        sign_p = 1.0
    print(f"paired sign test on A-B        : p = {sign_p:.6f}")

    # ------------------------------------------------------------------
    # The decision rule, fixed before the numbers were seen.
    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print("DECISION")
    print("=" * 78)
    a_high = np.median(a_values) > 50.0 and z_a > 1.96
    b_low = np.median(b_values) <= 50.0
    majority = positives > len(usable) / 2.0
    if a_high and b_low and majority and sign_p < 0.05:
        print("A — ASYMMETRY SURVIVES.")
        print("Real analyser flags sit materially above 50 and shape-matched")
        print("information-free flags sit at or below it, under the current")
        print("one-trade-per-run map and the block-resample null. The residual")
        print("is a property of what the analyser SELECTS, not of the old")
        print("multi-entry schedule.")
        return 0
    if abs(np.median(contrasts)) < 15.0 and sign_p >= 0.05:
        print("B — ASYMMETRY COLLAPSES.")
        print("Under the current instrument the two sides are statistically")
        print("indistinguishable. The old multi-entry-per-run map was a")
        print("material part of the slice-13 artefact, and the residual must be")
        print("re-characterised from scratch under the current instrument only.")
        return 1
    print("C — INCONCLUSIVE.")
    print("The two sides differ but not in the shape the decision rule names:")
    print("either Side A is not materially above 50, or Side B is not at or")
    print("below it, or the contrast is not consistent across surrogates.")
    print("Increase the surrogate count or the replicate count using existing")
    print("machinery. Do not change the decision rule.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
