#!/usr/bin/env python3
"""Is the null's own acceptance filter manufacturing the elevation?

THE HYPOTHESIS, STATED BEFORE ANY NUMBER IS PRODUCED
====================================================
Slice 19 isolated the residual to what the analyser selects, and measured
something it was not looking for while doing so:

    geometry acceptance rate : Side A median 2.35%   Side B median 9.35%
                               A < B on 20 of 24 surrogates

The block-resample null is built in two stages. A constraint search produces
**legal re-tilings** — arrangements that consume the observed multiset of run
lengths and the observed multiset of gaps exactly, placing every run on a bar
from its own geometry cell. Then a second stage keeps only those whose
*entry-cell distribution* falls within the sampling-floor tolerance of the
observed one. That second stage is the **acceptance filter**.

    If the filter selects on a quantity correlated with R — and it selects on
    the distribution of entries over cells that are themselves ATR quantiles,
    while R is inversely proportional to the risk unit — then the accepted
    pool's mean R is depressed relative to the unselected legal pool. The
    harder the filter bites, the more depressed, and the higher the observed
    side's percentile rises, with no timing skill involved anywhere.

Side A is filtered harder than Side B and scores about 25 percentile points
higher. That is the coincidence this tool exists to test.

WHAT IS MEASURED
----------------
For one observed flag sequence, ONE search produces a set of legal re-tilings.
Each is scored by the identical barrier arithmetic. The observed sequence is
then ranked twice against that single set:

    percentile_unfiltered  rank of the observed mean R among ALL legal
                           re-tilings, with no tolerance applied
    percentile_accepted    rank among the subset whose entry-cell distribution
                           is within the pre-declared tolerance
                           (this is what the shipped instrument reports)

**One search, two rankings.** The search is not re-run, so the two pools cannot
differ by anything except the filter — which is the whole design requirement.

WHY THE SEARCH STOPS ON LEGAL COUNT, NOT ACCEPTED COUNT
--------------------------------------------------------
``run_permutation`` stops once it has ``block_target`` **accepted**
arrangements. Reproducing that here would make the size of the legal pool a
function of the acceptance rate — the very quantity under test — so a side that
is filtered harder would search longer and collect a larger, differently-shaped
legal pool. This tool stops on the **legal** count instead. Both pools then come
from one identical set of searches, and the only difference between them is the
filter.

A DISTINCTION SLICE 19 CONFLATED, AND THIS TOOL SEPARATES
----------------------------------------------------------
The rate slice 19 reported was ``accepted / searches``, which is the product of
two very different things:

    solve rate           legal re-tilings / searches attempted
    tolerance acceptance accepted / legal re-tilings

Only the second is the filter. The first is how hard the constraint-satisfaction
problem is, which differs between the sides for an unrelated reason: the
analyser's run starts sit in unusual cells, so its tiling problem is simply
harder. Both are reported separately here, because a hypothesis about filtering
cannot be tested against a number that is mostly about search difficulty.

Usage
-----
    python3 tools/filter_diagnostic.py --surrogates 12
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import backtest as bt
import market_data as md
import skill_test as sk

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

#: A side with fewer legal re-tilings than this on a surrogate is reported as
#: INCOMPLETE for that side rather than given an invented percentile. The count
#: of incomplete sides is reported; nothing is dropped silently.
MIN_LEGAL = 30


def rank_pools(
    strategy_r: float,
    means: Sequence[float],
    distances: Sequence[float],
    tolerance: float,
) -> Tuple[Optional[float], Optional[float], int, int]:
    """Rank one observed value against a pool, twice — with and without filter.

    Pure, so the arithmetic that decides this slice can be unit-tested against
    hand-built pools rather than only against whatever the search happened to
    produce.

    Uses the identical percentile convention as
    ``skill_test.run_permutation`` — ``(means < observed).mean() * 100`` — so
    the accepted-pool number is directly comparable with everything already in
    EDGE.md.

    Returns ``(percentile_unfiltered, percentile_accepted, n_legal,
    n_accepted)``; a percentile is None when its pool is empty.
    """
    means_array = np.asarray(means, dtype=float)
    distance_array = np.asarray(distances, dtype=float)
    n_legal = int(means_array.size)
    if n_legal == 0:
        return None, None, 0, 0

    unfiltered = float((means_array < strategy_r).mean() * 100.0)
    keep = distance_array <= tolerance
    n_accepted = int(keep.sum())
    accepted = (float((means_array[keep] < strategy_r).mean() * 100.0)
                if n_accepted else None)
    return unfiltered, accepted, n_legal, n_accepted


@dataclass(frozen=True)
class SidePools:
    """One side on one surrogate. Every field measured, none inferred."""

    strategy_r: float
    percentile_unfiltered: Optional[float]
    percentile_accepted: Optional[float]
    n_searched: int
    n_legal: int
    n_accepted: int
    mean_r_legal: float
    sd_r_legal: float
    mean_r_accepted: float
    tolerance: float
    n_trades: int

    @property
    def solve_rate(self) -> float:
        return self.n_legal / self.n_searched if self.n_searched else 0.0

    @property
    def tolerance_acceptance(self) -> float:
        return self.n_accepted / self.n_legal if self.n_legal else 0.0

    @property
    def delta(self) -> Optional[float]:
        """accepted − unfiltered. Positive means the filter LIFTS the rank."""
        if self.percentile_accepted is None or self.percentile_unfiltered is None:
            return None
        return self.percentile_accepted - self.percentile_unfiltered

    @property
    def complete(self) -> bool:
        return (self.n_legal >= MIN_LEGAL
                and self.percentile_accepted is not None
                and self.percentile_unfiltered is not None)


def collect_pools(
    bars: Sequence,
    cfg: "bt.BacktestConfig",
    *,
    flags_override: Optional[np.ndarray],
    seed: int,
    lockup: int,
    horizon: int = 24,
    atr_period: int = 14,
    round_trip_bps: float = 25.0,
    take_profit_atr: float = 4.0,
    stop_atr: float = 2.0,
    strata_bins: int = 5,
    legal_target: int = 400,
    block_tries: int = 40_000,
    block_nodes: int = 2_000,
) -> Optional[SidePools]:
    """Reproduce ``run_permutation``'s block-resample setup, keep BOTH pools.

    Every step below mirrors ``skill_test.run_permutation`` and calls the same
    functions — the barrier scoring, the strata, the tolerance rule, the block
    extraction, the placement search and the schedule. Nothing is
    reimplemented. The single behavioural difference is that the tolerance is
    used to *label* each legal re-tiling rather than to discard it.
    """
    indices, net_r, _bars_used = sk.barrier_r_for_all_bars(
        bars, take_profit_atr=take_profit_atr, stop_atr=stop_atr,
        horizon=horizon, atr_period=atr_period, round_trip_bps=round_trip_bps,
    )
    if indices.size == 0:
        return None
    lookup = {int(idx): pos for pos, idx in enumerate(indices)}

    flags = (sk.signal_flags(bars, cfg, warmup=cfg.warmup_bars)
             if flags_override is None
             else np.asarray(flags_override, dtype=bool))
    if not flags.any():
        return None

    warmup = cfg.warmup_bars
    observed_entries = sk.simulate_schedule(flags, warmup=warmup, lockup=lockup)
    chosen = [lookup[i] for i in observed_entries if i in lookup]
    if len(chosen) < 30:
        return None
    strategy_r = float(np.mean(net_r[chosen]))

    rng = np.random.default_rng(seed)
    eligible_mask = np.zeros(flags.size, dtype=bool)
    eligible_mask[indices] = True
    eligible_mask[:warmup] = False
    atr_over_price, range_position = sk.stratification_features(
        bars, atr_period=atr_period, range_lookback=atr_period,
    )
    strata = sk.assign_strata(
        atr_over_price, range_position, eligible_mask, bins=strata_bins
    )
    n_cells = strata_bins * strata_bins
    observed_geometry = sk.geometry_distribution(
        np.array(observed_entries, dtype=int), strata, n_cells
    )

    # The tolerance is taken from the SAME rule with the SAME seed derivation
    # the instrument uses. It is not recomputed differently and it is not
    # changed — it is only applied later.
    tolerance, _floor_p10 = sk.sampling_floor_tolerance(
        observed_geometry, len(observed_entries),
        resamples=2000, rng=np.random.default_rng(seed ^ 0x5EED),
    )

    eligible_flags = flags & eligible_mask
    runs_observed, gaps_observed = sk.extract_blocks(eligible_flags, warmup=warmup)
    run_lengths = np.array([length for _, length in runs_observed], dtype=np.int64)
    run_cells = np.array([int(strata[start]) for start, _ in runs_observed],
                         dtype=np.int64)
    gaps_array = np.array(gaps_observed, dtype=np.int64)
    pools = sk.candidate_sets(strata, eligible_mask, warmup=warmup)
    if run_lengths.size == 0 or any(int(c) not in pools for c in run_cells):
        return None

    means: List[float] = []
    distances: List[float] = []
    searched = 0
    for _attempt in range(block_tries):
        searched += 1
        built = sk.block_resample_flags(
            run_lengths, run_cells, gaps_array, strata, flags.size,
            warmup=warmup, node_budget=block_nodes, rng=rng,
        )
        if built is None:
            continue
        candidate, _nodes = built
        entries = sk.simulate_schedule(candidate, warmup=warmup, lockup=lockup)
        if not entries:
            continue
        pick = [lookup[i] for i in entries if i in lookup]
        if len(pick) < 20:
            continue
        means.append(float(np.mean(net_r[pick])))
        distances.append(sk.total_variation(
            sk.geometry_distribution(np.array(entries, dtype=int), strata, n_cells),
            observed_geometry,
        ))
        if len(means) >= legal_target:
            break

    unfiltered, accepted_pct, n_legal, n_accepted = rank_pools(
        strategy_r, means, distances, tolerance
    )
    means_array = np.asarray(means, dtype=float)
    keep = np.asarray(distances, dtype=float) <= tolerance
    return SidePools(
        strategy_r=strategy_r,
        percentile_unfiltered=unfiltered,
        percentile_accepted=accepted_pct,
        n_searched=searched,
        n_legal=n_legal,
        n_accepted=n_accepted,
        mean_r_legal=float(means_array.mean()) if n_legal else float("nan"),
        sd_r_legal=(float(means_array.std(ddof=1)) if n_legal > 1
                    else float("nan")),
        mean_r_accepted=(float(means_array[keep].mean()) if n_accepted
                         else float("nan")),
        tolerance=tolerance,
        n_trades=len(chosen),
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=os.path.join(REPO, "data", "real_1d"))
    parser.add_argument("--interval", default="D")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--surrogates", type=int, default=12)
    parser.add_argument("--seed", type=int, default=20250727)
    parser.add_argument("--lockup", type=int, default=1)
    parser.add_argument("--legal-target", type=int, default=400)
    parser.add_argument("--block-tries", type=int, default=40_000)
    parser.add_argument("--block-nodes", type=int, default=2_000)
    parser.add_argument("--strata-bins", type=int, default=5)
    parser.add_argument("--min-confidence", type=float, default=0.12)
    parser.add_argument("--min-agreement", type=float, default=0.40)
    args = parser.parse_args(argv)

    loaded, _books, notes = md.load_corpus(args.data_dir)
    symbol = args.symbol or sorted(loaded)[0]
    bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
            for b in loaded[symbol]]

    print("=" * 78)
    print("NULL-POOL FILTER DIAGNOSTIC (slice 20)")
    print("=" * 78)
    for note in notes:
        print(note)
    print(f"symbol            : {symbol}")
    print(f"bars              : {len(bars):,}")
    print()
    print("ONE search per side per surrogate produces the legal re-tilings.")
    print("The observed sequence is ranked against that ONE set twice:")
    print("  unfiltered : all legal re-tilings, no tolerance applied")
    print("  accepted   : only those within the pre-declared tolerance")
    print("The tolerance is NOT changed. The search is NOT changed. The only")
    print("difference between the two pools is whether the filter is applied.")
    print()
    print("Side A = real analyser flags.  Side B = shape_matched_flags with the")
    print("same run-length and gap multisets, carrying no information at all.")
    print()

    cfg = bt.BacktestConfig(
        starting_cash=10_000.0, warmup_bars=200, interval=args.interval,
        min_confidence=args.min_confidence,
        min_component_agreement=args.min_agreement,
    )

    rows: List[Tuple[int, Optional[SidePools], Optional[SidePools]]] = []
    incomplete = 0
    for offset in range(args.surrogates):
        seed = args.seed + 1000 + offset
        surrogate = sk.surrogate_series(bars, seed=seed)

        real_flags = sk.signal_flags(surrogate, cfg, warmup=cfg.warmup_bars)
        if not real_flags.any():
            print(f"  surrogate {offset + 1:2d}: analyser never fired — skipped")
            incomplete += 2
            continue
        runs_observed, gaps_observed = sk.extract_blocks(
            real_flags, warmup=cfg.warmup_bars)
        run_lengths = np.array([l for _, l in runs_observed], dtype=np.int64)
        synthetic = sk.shape_matched_flags(
            run_lengths, np.array(gaps_observed, dtype=np.int64),
            real_flags.size, warmup=cfg.warmup_bars,
            rng=np.random.default_rng(seed ^ 0xB15EC7),
        )

        common = dict(
            seed=seed, lockup=args.lockup, strata_bins=args.strata_bins,
            legal_target=args.legal_target, block_tries=args.block_tries,
            block_nodes=args.block_nodes,
        )
        side_a = collect_pools(surrogate, cfg, flags_override=None, **common)
        side_b = collect_pools(surrogate, cfg, flags_override=synthetic, **common)
        rows.append((seed, side_a, side_b))

        def show(side, label):
            if side is None or not side.complete:
                return f"{label} INCOMPLETE"
            return (f"{label} unf {side.percentile_unfiltered:5.1f} "
                    f"acc {side.percentile_accepted:5.1f} "
                    f"D {side.delta:+6.1f} "
                    f"[{side.n_accepted:3d}/{side.n_legal:3d}]")
        for side in (side_a, side_b):
            if side is None or not side.complete:
                incomplete += 1
        print(f"  surrogate {offset + 1:2d}: {show(side_a, 'A')}  |  "
              f"{show(side_b, 'B')}")

    complete = [(s, a, b) for s, a, b in rows
                if a is not None and b is not None and a.complete and b.complete]
    if len(complete) < 3:
        print()
        print("C — INCONCLUSIVE: fewer than 3 surrogates complete on both sides.")
        return 2

    print()
    print("=" * 78)
    print("PER-SURROGATE")
    print("=" * 78)
    print(f"{'seed':>10} | {'A unf':>6} {'A acc':>6} {'dA':>6} {'A n_acc/n_leg':>14} "
          f"{'A solve':>8} {'A tolacc':>9} | {'B unf':>6} {'B acc':>6} {'dB':>6} "
          f"{'B n_acc/n_leg':>14} {'B solve':>8} {'B tolacc':>9}")
    for seed, a, b in complete:
        print(f"{seed:>10} | {a.percentile_unfiltered:>6.1f} "
              f"{a.percentile_accepted:>6.1f} {a.delta:>+6.1f} "
              f"{a.n_accepted:>6d}/{a.n_legal:<7d} {a.solve_rate:>7.1%} "
              f"{a.tolerance_acceptance:>8.1%} | "
              f"{b.percentile_unfiltered:>6.1f} {b.percentile_accepted:>6.1f} "
              f"{b.delta:>+6.1f} {b.n_accepted:>6d}/{b.n_legal:<7d} "
              f"{b.solve_rate:>7.1%} {b.tolerance_acceptance:>8.1%}")

    print()
    print("=" * 78)
    print("MEAN R OF EACH POOL (does the filter select lower-R arrangements?)")
    print("=" * 78)
    print(f"{'seed':>10} | {'A obs R':>9} {'A legal R':>10} {'A acc R':>9} "
          f"{'A shift':>9} | {'B obs R':>9} {'B legal R':>10} {'B acc R':>9} "
          f"{'B shift':>9}")
    for seed, a, b in complete:
        print(f"{seed:>10} | {a.strategy_r:>+9.4f} {a.mean_r_legal:>+10.4f} "
              f"{a.mean_r_accepted:>+9.4f} "
              f"{a.mean_r_accepted - a.mean_r_legal:>+9.4f} | "
              f"{b.strategy_r:>+9.4f} {b.mean_r_legal:>+10.4f} "
              f"{b.mean_r_accepted:>+9.4f} "
              f"{b.mean_r_accepted - b.mean_r_legal:>+9.4f}")

    delta_a = np.array([a.delta for _s, a, _b in complete], dtype=float)
    delta_b = np.array([b.delta for _s, _a, b in complete], dtype=float)
    unf_a = np.array([a.percentile_unfiltered for _s, a, _b in complete])
    unf_b = np.array([b.percentile_unfiltered for _s, _a, b in complete])
    acc_a = np.array([a.percentile_accepted for _s, a, _b in complete])
    acc_b = np.array([b.percentile_accepted for _s, _a, b in complete])
    shift_a = np.array([a.mean_r_accepted - a.mean_r_legal
                        for _s, a, _b in complete])
    shift_b = np.array([b.mean_r_accepted - b.mean_r_legal
                        for _s, _a, b in complete])

    print()
    print("=" * 78)
    print("POOLED")
    print("=" * 78)
    print(f"complete surrogates : {len(complete)} of {args.surrogates}   "
          f"incomplete side-runs: {incomplete}")
    print(f"Side A  unfiltered median {np.median(unf_a):5.1f}   "
          f"accepted median {np.median(acc_a):5.1f}   "
          f"dA median {np.median(delta_a):+5.1f}  mean {delta_a.mean():+5.1f}")
    print(f"Side B  unfiltered median {np.median(unf_b):5.1f}   "
          f"accepted median {np.median(acc_b):5.1f}   "
          f"dB median {np.median(delta_b):+5.1f}  mean {delta_b.mean():+5.1f}")
    standard_error = 28.87 / np.sqrt(len(complete))
    print(f"uniformity z vs 50  : A unfiltered {(unf_a.mean() - 50) / standard_error:+.2f}"
          f"   A accepted {(acc_a.mean() - 50) / standard_error:+.2f}"
          f"   B unfiltered {(unf_b.mean() - 50) / standard_error:+.2f}"
          f"   B accepted {(acc_b.mean() - 50) / standard_error:+.2f}")
    print(f"mean-R shift accepted-legal : A median {np.median(shift_a):+.5f}   "
          f"B median {np.median(shift_b):+.5f}")
    print(f"solve rate median   : A {np.median([a.solve_rate for _s,a,_b in complete]):.1%}"
          f"   B {np.median([b.solve_rate for _s,_a,b in complete]):.1%}")
    print(f"tolerance acceptance: A "
          f"{np.median([a.tolerance_acceptance for _s,a,_b in complete]):.1%}"
          f"   B {np.median([b.tolerance_acceptance for _s,_a,b in complete]):.1%}")

    # ------------------------------------------------------------------
    # The decision rule, fixed before the numbers were seen.
    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print("DECISION")
    print("=" * 78)
    median_delta_a = float(np.median(delta_a))
    median_delta_b = float(np.median(delta_b))
    a_falls = median_delta_a >= 10.0
    b_flat = abs(median_delta_b) < max(5.0, abs(median_delta_a) / 2.0)
    if a_falls and b_flat:
        print("A — THE ACCEPTANCE FILTER IS THE MECHANISM.")
        print("Removing the filter drops Side A's percentile materially while")
        print("Side B barely moves. The elevation is an artefact of ranking")
        print("against a filtered, lower-R null subfamily, not of timing.")
        return 0
    if abs(median_delta_a) < 10.0 and abs(median_delta_b) < 10.0:
        print("B — THE ACCEPTANCE FILTER IS NOT THE MECHANISM.")
        print("Both sides rank essentially the same with and without the")
        print("filter. Side A's elevation survives its removal, so the residual")
        print("remains in what the analyser selects.")
        return 1
    print("C — INCONCLUSIVE.")
    print("The deltas do not fall into either named shape. Increase the search")
    print("budget or the surrogate count using existing machinery; do not")
    print("change the decision rule and do not touch the tolerance.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
