#!/usr/bin/env python3
"""Is it skill, or is it drift? A matched-exposure permutation test.

THE QUESTION THIS EXISTS TO SETTLE
==================================
Slice 9 ended with a strategy that has genuinely positive expectancy —
+0.548R net, 95% CI [+0.321, +0.785], 163 trades — and no way to tell whether
that number represents anything the strategy *did*.

The suspicion, from slice 9's own evidence, is that it is drift: BTC rose
roughly 8x over the sample, the strategy is long-only, and a long-only barrier
strategy on an appreciating asset shows positive expectancy without any edge
at all. The short-side sweep returned 0 of 84 positive cells, which is exactly
the asymmetry drift produces.

"Beat buy-and-hold" is the wrong test — it compares 2% notional against 100%,
so it answers a question about position sizing. "Positive expectancy" is also
the wrong test, for the reason above.

THE RIGHT TEST
--------------
Hold everything constant except the thing under examination, which is **when
the strategy chose to enter**:

    same number of trades
    same holding period for each trade      (bar-for-bar)
    same direction mix
    same instrument, same barriers, same costs
    ONLY the entry bars are randomised

Every one of those random portfolios is exposed to the identical drift, the
identical volatility regime, and the identical fee schedule. What they do not
have is the strategy's timing. The distribution of their expectancies is
therefore the null hypothesis "this strategy has no timing skill", expressed in
the same units as the strategy's own result.

The strategy's percentile within that distribution is the answer. At the 50th
percentile the entry logic contributed nothing measurable. Below it, the entry
logic was actively harmful.

WHY A PERMUTATION TEST AND NOT A T-TEST
---------------------------------------
Trade outcomes here are neither independent nor normal: they overlap in time,
they are floored at -1R by construction, and they have a long right tail. A
parametric interval on that distribution is a number with the wrong shape. The
permutation approach makes no distributional assumption at all — it just asks
how the observed result ranks among results the same process could have
produced by accident.

WHAT A PASS WOULD AND WOULD NOT MEAN
------------------------------------
A high percentile would mean the entry timing added something on this sample,
on this asset, over this period. It would NOT mean the effect is stable, that
it survives on other assets, or that it is large enough to matter after the
position-size limits are applied. Those are separate questions and this tool
answers none of them.

Usage
-----
    python3 tools/skill_test.py --data-dir data/real_1d --interval D
    python3 tools/skill_test.py --runs 5000 --seed 7
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
import sweep_geometry as sweep

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


@dataclass(frozen=True)
class SkillVerdict:
    """What the permutation said. Every field is measured, none is inferred."""

    n_trades: int
    strategy_expectancy_r: float
    baseline_mean_r: float
    baseline_std_r: float
    percentile: float
    p_value: float
    runs: int
    median_hold_bars: float
    exposure_fraction: float
    asset_return: float
    strategy_return: float
    #: Fraction of flagged bars that found a same-stratum non-flagged partner.
    #: Reported, never assumed: an unmatched flag is DROPPED rather than matched
    #: loosely, because a loose match reintroduces the contamination the
    #: stratification exists to remove.
    match_rate: float = 1.0
    strata_used: int = 0
    #: Geometry-matching diagnostics for the stratified rotation. The tolerance
    #: is fixed by rule before any rotation is evaluated -- see
    #: sampling_floor_tolerance -- and reported alongside the sampling floor so
    #: a reader can see how tight it is rather than taking it on trust.
    tolerance: float = float("nan")
    floor_p10: float = float("nan")
    acceptance_rate: float = 1.0
    best_distance: float = float("nan")
    median_accepted_distance: float = float("nan")
    #: Block-resampling diagnostics. ``n_blocks`` is the number of flag runs
    #: whose lengths are preserved EXACTLY by every replicate; ``snap_median``
    #: is the median distance, in bars, between a block's ideal position under
    #: the permuted gap sequence and the geometry-matched bar it was actually
    #: placed on. A large snap distance means the gap structure is being bent
    #: to satisfy the geometry constraint, and it is reported rather than
    #: buried for exactly that reason.
    n_blocks: int = 0
    snap_median: float = float("nan")
    placement_failures: int = 0
    placements_searched: int = 0
    #: The decomposition that explains a block-resample refusal. A block's
    #: START is the only position the placement controls; these two numbers say
    #: how much of the entry schedule that actually determines.
    start_entry_fraction: float = float("nan")
    start_geometry_gap: float = float("nan")

    @property
    def verdict(self) -> str:
        """The gate, stated once.

        95th percentile is the threshold, one-sided: the question is whether
        the strategy beat its own null, not whether it differs from it. It is
        deliberately not 90th — this is the gate that decides whether to keep
        building on an entry signal, and a one-in-ten result is not worth
        several more slices of work.
        """
        if self.percentile >= 95.0:
            return "SKILL — the entry timing beat a matched-exposure null"
        if self.percentile >= 75.0:
            return "INCONCLUSIVE — better than the null, not decisively"
        if self.percentile >= 25.0:
            return "DRIFT — indistinguishable from random entries"
        return "WORSE THAN RANDOM — the entry timing is subtracting value"

    @property
    def green(self) -> bool:
        return self.percentile >= 95.0


def barrier_r_for_all_bars(
    bars: Sequence, *, take_profit_atr: float, stop_atr: float,
    horizon: int, atr_period: int, round_trip_bps: float,
    side: str = "long", entry_on: str = "close",
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Realised net R for a trade opened at EVERY bar, under one geometry.

    Returns ``(eligible_indices, net_r, bars_to_resolution)``. This is the
    null's raw material and
    also how the strategy's own entries are scored, which is the whole point:
    both sides of the comparison are measured by identical arithmetic, so the
    only thing that can differ between them is *which bars were chosen*.

    ``side`` and ``entry_on`` were added in slice 35 for
    ``btc_alt_spillover_v1``, which is the first two-sided signal this
    instrument has scored and the first to fill at the *next* bar's open rather
    than at the signal bar's close.

    **They default to the previous behaviour exactly** — ``side="long"`` and
    ``entry_on="close"`` reproduce the pre-slice-35 function line for line, so
    every measurement in EDGE.md §4–§13 still reproduces byte-identically. The
    extension is here, in the one shared implementation, rather than copied into
    a signal module, because the whole validity of the comparison rests on the
    observed side and the null side being scored by the *same* code.

    ``entry_on="next_open"``: the signal at bar ``i`` fills at ``open[i+1]``, and
    the entry bar itself **is** scanned for the barriers — a position opened at
    an open can be stopped out that same session, and pretending otherwise would
    flatter every result. The time stop is the close of ``entry_bar + horizon``.
    """
    if side not in ("long", "short"):
        raise ValueError(f"side must be 'long' or 'short', got {side!r}")
    if entry_on not in ("close", "next_open"):
        raise ValueError(
            f"entry_on must be 'close' or 'next_open', got {entry_on!r}")

    high = np.array([b.high for b in bars], dtype=float)
    low = np.array([b.low for b in bars], dtype=float)
    close = np.array([b.close for b in bars], dtype=float)
    open_ = np.array([b.open for b in bars], dtype=float)
    atr = sweep.wilder_atr(high, low, close, atr_period)

    n = close.size
    # Offset from the signal bar to the bar the position is opened on, and the
    # first forward step scanned for a barrier touch. Entering at a close means
    # the signal bar is already over, so scanning starts at the next bar;
    # entering at the next open means that bar is live and must be scanned.
    entry_offset = 0 if entry_on == "close" else 1
    first_step = 1 if entry_on == "close" else 0

    eligible = np.isfinite(atr) & (atr > 0.0)
    eligible[max(0, n - horizon - entry_offset - 1):] = False
    indices = np.nonzero(eligible)[0]
    if indices.size == 0:
        # Every real caller (70+ call sites across tools/ and tests/) unpacks
        # THREE values: `indices, net, bars_used = barrier_r_for_all_bars(...)`.
        # A too-short corpus, an all-NaN ATR window, or a horizon that eats the
        # whole series hits this branch and previously returned a 2-tuple,
        # which raises ValueError at every one of those call sites instead of
        # the empty-result the caller is prepared to handle. Found by
        # tools/corpus_unit_audit.py's author while checking the function on
        # a short slice; reproduced here directly (see
        # tests/test_barrier_empty_input.py).
        return (np.array([], dtype=int), np.array([], dtype=float),
                np.array([], dtype=np.int32))

    entry_index = indices + entry_offset
    entry = close[indices] if entry_on == "close" else open_[entry_index]
    # Risk is sized on the ATR of the last CLOSED bar at or before entry, which
    # is the signal bar in both modes — the entry bar's own ATR is not knowable
    # when the order is placed.
    risk = stop_atr * atr[indices]
    if side == "long":
        tp_level = entry + take_profit_atr * atr[indices]
        sl_level = entry - risk
    else:
        tp_level = entry - take_profit_atr * atr[indices]
        sl_level = entry + risk

    realised = np.full(indices.size, np.nan)
    bars_used = np.full(indices.size, horizon, dtype=np.int32)
    still_open = np.ones(indices.size, dtype=bool)
    payoff = take_profit_atr / stop_atr
    for step in range(first_step, horizon + 1):
        forward = entry_index + step
        # Stop first, always: a candle reports its high and its low, not the
        # order they happened in, and only one of the two readings is safe.
        if side == "long":
            stopped = still_open & (low[forward] <= sl_level)
        else:
            stopped = still_open & (high[forward] >= sl_level)
        realised[stopped] = -1.0
        bars_used[stopped] = step
        still_open &= ~stopped
        if side == "long":
            won = still_open & (high[forward] >= tp_level)
        else:
            won = still_open & (low[forward] <= tp_level)
        realised[won] = payoff
        bars_used[won] = step
        still_open &= ~won
        if not still_open.any():
            break
    if still_open.any():
        exit_index = entry_index[still_open] + horizon
        move = close[exit_index] - entry[still_open]
        if side == "short":
            move = -move
        realised[still_open] = move / risk[still_open]
    net = realised - (round_trip_bps / 10_000.0) * entry / risk
    return indices, net, bars_used


def signal_flags(
    bars: Sequence, cfg: "bt.BacktestConfig", *, warmup: int,
) -> np.ndarray:
    """For every bar: would the strategy have wanted to open a trade there?

    Computed **independently of position state** — this asks only "did the
    signal machinery produce an actionable entry on this bar", not "did the
    strategy actually trade it". The flat-only constraint is applied afterwards
    by :func:`simulate_schedule`, which is precisely the separation the
    endogenous null needs: the *decision sequence* and the *scheduling
    mechanics* have to be independently substitutable.

    The real analyser is used, with the real thresholds, on the real candle
    window ending at each bar. Nothing is approximated: a bar is flagged if and
    only if ``TechnicalAnalysis.analyse`` returned an actionable signal that
    cleared ``MIN_CONFIDENCE`` and ``MIN_COMPONENT_AGREEMENT``, and — because
    this codebase's shipped category is spot — the direction is a BUY.
    """
    from technical_analysis import Candles, TechnicalAnalysis

    view = bt._backtest_config_view(cfg)
    analyser = TechnicalAnalysis(config=view)
    allow_shorts = bool(getattr(view, "ALLOW_SHORTS", False))

    opens = [b.open for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    volumes = [b.volume for b in bars]

    flags = np.zeros(len(bars), dtype=bool)
    for index in range(warmup, len(bars)):
        window = slice(max(0, index - 400), index + 1)
        try:
            candles = Candles(
                tuple(opens[window]), tuple(highs[window]), tuple(lows[window]),
                tuple(closes[window]), tuple(volumes[window]),
                str(cfg.interval),
            )
            signal = analyser.analyse("SYNTH", candles)
        except Exception:  # noqa: BLE001
            continue
        if not signal.is_actionable:
            continue
        if signal.action == "SELL" and not allow_shorts:
            # Spot cannot short. The live strategy suppresses these, so the
            # flag sequence must too, or the null would be drawing from a
            # richer population than the strategy ever had.
            continue
        flags[index] = True
    return flags


def stratification_features(
    bars: Sequence, *, atr_period: int, range_lookback: int,
) -> Tuple[np.ndarray, np.ndarray]:
    """The barrier's OWN inputs, and nothing else.

    Two features, both deliberate, both read off the same trailing window the
    barrier itself reads. The slice-13 bisection localised the residual bias to
    "the analyser's flags sitting on the bars that generated them", on a series
    with no temporal structure — which can only be a *contemporaneous* relation
    between what makes the analyser fire and what the barrier scores. So the
    null must be matched on exactly the quantities the barrier uses to place
    its levels, and on nothing further.

    ``atr_over_price = ATR(atr_period) / close``
        The barrier levels are ``close +/- k*ATR``, so this single number fixes
        how far both barriers sit from entry *as a fraction of price*, and
        therefore the whole shape of the outcome distribution: how likely a
        touch is within the horizon, and how large a timeout's R can be. It is
        the denominator of every R this instrument computes.

    ``range_position = (close - min(low)) / (max(high) - min(low))``
        Over the same ``range_lookback`` window. Where the entry sits inside
        its own recent range. This is what most of the analyser's components
        actually key on — Bollinger position, RSI, distance from a moving
        average are all monotone functions of roughly this — and it is also
        what determines whether the recent path has already visited the price
        levels the barriers occupy.

    ``range_lookback`` defaults to ``atr_period`` so both features describe the
    SAME window. Choosing a different one would introduce a second, unjustified
    look-back into a matching scheme whose entire warrant is "match on what the
    barrier reads".

    No third feature is added. Every extra matching dimension thins the strata
    and eventually matches each flag to itself; the discipline here is the
    smallest set that can carry the mechanism the bisection identified.

    Returns arrays aligned to ``bars``, NaN where undefined.
    """
    high = np.array([b.high for b in bars], dtype=float)
    low = np.array([b.low for b in bars], dtype=float)
    close = np.array([b.close for b in bars], dtype=float)
    atr = sweep.wilder_atr(high, low, close, atr_period)

    with np.errstate(divide="ignore", invalid="ignore"):
        atr_over_price = np.where(close > 0, atr / close, np.nan)

    n = close.size
    window = max(2, int(range_lookback))
    range_position = np.full(n, np.nan)
    for i in range(window - 1, n):
        lo = float(low[i - window + 1: i + 1].min())
        hi = float(high[i - window + 1: i + 1].max())
        if hi > lo:
            range_position[i] = (close[i] - lo) / (hi - lo)
    return atr_over_price, range_position


def assign_strata(
    atr_over_price: np.ndarray,
    range_position: np.ndarray,
    eligible: np.ndarray,
    *,
    bins: int,
) -> np.ndarray:
    """Quantile-bin both features over the ELIGIBLE population.

    Quantile bins rather than equal-width: ATR/price is strongly right-skewed
    on crypto, so equal-width bins would put nine tenths of the sample in the
    first bin and match almost nothing. Quantiles give equally-populated strata
    by construction, which is what a matched sampler needs.

    Cut points are computed over the eligible bars only — including ineligible
    ones would shift the boundaries by a population the test never draws from.

    Returns a stratum id per bar, ``-1`` where the bar cannot be stratified.
    """
    strata = np.full(atr_over_price.size, -1, dtype=np.int32)
    usable = eligible & np.isfinite(atr_over_price) & np.isfinite(range_position)
    if not usable.any():
        return strata

    def bin_of(values: np.ndarray) -> np.ndarray:
        cuts = np.quantile(values[usable], np.linspace(0.0, 1.0, bins + 1)[1:-1])
        return np.digitize(values, cuts)

    a_bin = bin_of(atr_over_price)
    r_bin = bin_of(range_position)
    strata[usable] = (a_bin[usable] * bins + r_bin[usable]).astype(np.int32)
    return strata


def matched_flags(
    flags: np.ndarray,
    strata: np.ndarray,
    rng: np.random.Generator,
) -> Tuple[np.ndarray, int, int]:
    """Replace every flagged bar with a NON-flagged bar from the same stratum.

    This is the null. Not a rotation, not a permutation: a like-for-like
    substitution on the barrier's own geometry.

    Each side ends up with the same number of flags, each sitting on a bar
    whose ATR/price and range-position fall in the same quantile cell. The two
    sides therefore differ in exactly one respect — whether the analyser said
    yes on that geometry or said no on the same geometry — which is the
    hypothesis, isolated.

    Sampling is **without replacement within a stratum**, so a null replicate
    cannot double-count one unusually good non-flagged bar. Returns
    ``(null_flags, matched, unmatched)``; a flag whose stratum contains no
    spare non-flagged bar is dropped and counted rather than matched loosely,
    because a loose match is exactly the contamination this is removing.
    """
    null_flags = np.zeros_like(flags)
    matched = unmatched = 0

    pools: Dict[int, List[int]] = {}
    for index in np.nonzero((strata >= 0) & ~flags)[0]:
        pools.setdefault(int(strata[index]), []).append(int(index))

    for index in np.nonzero(flags)[0]:
        stratum = int(strata[index])
        pool = pools.get(stratum)
        if not pool:
            unmatched += 1
            continue
        choice = int(rng.integers(len(pool)))
        null_flags[pool.pop(choice)] = True
        matched += 1
    return null_flags, matched, unmatched


def geometry_distribution(
    flagged_indices: np.ndarray, strata: np.ndarray, n_cells: int,
) -> np.ndarray:
    """Normalised histogram of flagged bars over the geometry cells."""
    counts = np.zeros(n_cells, dtype=float)
    for index in flagged_indices:
        cell = int(strata[index])
        if 0 <= cell < n_cells:
            counts[cell] += 1.0
    total = counts.sum()
    return counts / total if total > 0 else counts


def total_variation(p: np.ndarray, q: np.ndarray) -> float:
    """Total-variation distance between two discrete distributions.

    Chosen over chi-squared because most of the 25 cells hold only a handful of
    the ~54 flags, and chi-squared is unstable — and enormous — when expected
    counts are that small. TV is bounded in [0, 1], reads directly as "the
    largest probability mass by which the two distributions can disagree", and
    degrades gracefully on sparse cells.
    """
    return 0.5 * float(np.abs(p - q).sum())


def sampling_floor_tolerance(
    observed: np.ndarray, n_draws: int, *, resamples: int, rng: np.random.Generator,
) -> Tuple[float, float]:
    """The tolerance, fixed by RULE before any rotation is evaluated.

    A fixed number would have been arbitrary and — worse — tunable after the
    fact, which is exactly the failure mode this project keeps refusing. So the
    rule is stated instead of the number:

        accept a rotation only if its geometry distribution is at least as
        close to the observed one as a MEDIAN independent resample of the same
        size drawn from the observed distribution itself

    That floor exists because a fresh multinomial draw of ``n_draws`` items
    from the observed distribution does **not** reproduce it exactly: with ~54
    flags over 25 cells the expected count per cell is about two, so sampling
    noise alone puts a hard lower bound on any achievable TV distance.
    Demanding better than that bound would reject everything, including a
    perfectly matched null, and demanding much worse would accept rotations
    whose geometry is visibly different.

    Returns ``(tolerance, floor_p10)`` — the median and the 10th percentile of
    that resampling distribution, both reported so the reader can see how tight
    the floor is rather than taking the tolerance on trust.
    """
    if n_draws <= 0 or observed.sum() <= 0:
        return 0.0, 0.0
    distances = np.empty(resamples, dtype=float)
    for i in range(resamples):
        drawn = rng.multinomial(n_draws, observed) / float(n_draws)
        distances[i] = total_variation(drawn, observed)
    return float(np.median(distances)), float(np.percentile(distances, 10))


def extract_blocks(
    flags: np.ndarray, *, warmup: int,
) -> Tuple[List[Tuple[int, int]], List[int]]:
    """The observed block structure: runs of flags, and the gaps between them.

    Returns ``(runs, gaps)`` where ``runs`` is ``[(start_bar, length), ...]`` in
    temporal order and ``gaps`` is the ``len(runs) + 1`` list of unflagged
    stretches — the leading gap from ``warmup`` to the first run, each internal
    gap, and the trailing gap to the end of the series.

    Lengths and gaps together tile the region exactly:
    ``sum(lengths) + sum(gaps) == flags.size - warmup``. That identity is what
    makes a permutation of the two multisets a legal re-tiling of the same
    series, which is the whole basis of the block resampler below.

    WHY BLOCKS AND NOT BARS
    -----------------------
    Every null before this one moved or substituted individual flags, and each
    failed on a different constraint. The run/gap decomposition is the object
    that actually carries the clustering: the analyser does not fire on isolated
    bars, it fires on stretches (here: 47 runs, lengths 1 to 77, over 973
    flagged bars). Preserving the multiset of run lengths preserves the burst
    structure *exactly* rather than approximating it, and it does so without
    fixing where the bursts sit — which is the variable under test.
    """
    region = flags[warmup:]
    runs: List[Tuple[int, int]] = []
    index = 0
    while index < region.size:
        if region[index]:
            end = index
            while end < region.size and region[end]:
                end += 1
            runs.append((index + warmup, end - index))
            index = end
        else:
            index += 1

    gaps: List[int] = []
    cursor = warmup
    for start, length in runs:
        gaps.append(start - cursor)
        cursor = start + length
    gaps.append(flags.size - cursor)
    return runs, gaps


def candidate_sets(
    strata: np.ndarray, eligible: np.ndarray, *, warmup: int,
) -> Dict[int, np.ndarray]:
    """Every eligible bar, indexed by its geometry cell.

    These are the legal starting positions for a block: a block whose observed
    start sat in cell ``c`` may be re-placed on any bar in cell ``c``, and on no
    other bar. The candidate set is NOT restricted to unflagged bars — unlike
    the slice-14 matched sampler, which needed that restriction because it was
    substituting flags one for one. Here the observed arrangement must remain in
    the null's support, as it must for any permutation test to have a valid
    p-value; excluding it would bias the null away from the observation by
    construction.
    """
    pools: Dict[int, List[int]] = {}
    for index in np.nonzero(eligible & (strata >= 0))[0]:
        if index < warmup:
            continue
        pools.setdefault(int(strata[index]), []).append(int(index))
    return {cell: np.array(sorted(items), dtype=int) for cell, items in pools.items()}


def block_resample_flags(
    run_lengths: np.ndarray,
    run_cells: np.ndarray,
    gaps: np.ndarray,
    strata: np.ndarray,
    n_bars: int,
    *,
    warmup: int,
    node_budget: int,
    rng: np.random.Generator,
) -> Optional[Tuple[np.ndarray, float]]:
    """Re-tile the series with the SAME blocks, placed on matching geometry.

    THE THREE CONSTRAINTS, HELD AT ONCE
    ===================================
    Slices 12-15 each satisfied two of the three and paid for it on the third:

        rotation             clustering + counts, geometry FREE  -> control 73.1
        matched              geometry, clustering DESTROYED      -> control 96.8
        stratified rotation  all three demanded, family too small -> 0% accepted

    The rotation family has only ~2,300 members because a rotation has one
    degree of freedom. This sampler has one degree of freedom *per block*: it
    permutes which block goes where, permutes the gaps between them, and then
    chooses each block's actual starting bar from the set of bars in that
    block's own geometry cell. With 47 blocks and 40-160 candidates per cell the
    family is combinatorially large, which is what makes a joint constraint
    satisfiable where a rotation provably could not.

    A REPLICATE IS AN EXACT RE-TILING, NOT AN APPROXIMATE ONE
    ---------------------------------------------------------
    The run lengths and the gaps tile the region exactly:
    ``sum(lengths) + sum(gaps) == n_bars - warmup``. So a replicate is built by
    consuming BOTH multisets, each element exactly once, in a new order:

        gap, block, gap, block, ..., block, gap

    Because every element is used exactly once, the arrangement lands on the
    last bar of the series by arithmetic — there is no drift to correct and no
    slack to run out of. The multiset of run lengths and the multiset of gaps
    are therefore preserved **exactly**, not approximated and not merely
    bounded, and the only free choice is the pairing: which gap precedes which
    block.

    That pairing is exactly where the geometry constraint bites. Having laid
    down some prefix, the cursor is fixed; choosing gap ``g`` puts the next
    block's start on bar ``cursor + g``, whose cell is then determined. The
    block placed there must be one whose OBSERVED start sat in that same cell.
    So the sampler searches for an interleaving in which every block start lands
    on matching geometry — a constraint-satisfaction problem, solved by
    depth-first search with randomised child order and a node budget.

    THE NODE BUDGET IS SMALL ON PURPOSE. Measured throughput of *complete*
    solutions: 6.2/s at 2,000 nodes, 2.6/s at 8,000, 1.5/s at 20,000, 0.5/s at
    50,000. The search tree is astronomically larger than any budget, so a deep
    search buys nothing that a random restart does not buy more cheaply.

    WHY A SNAP-TO-NEAREST SCHEME WAS TRIED FIRST AND ABANDONED
    -----------------------------------------------------------
    The obvious implementation lays the blocks down at their ideal positions and
    then snaps each to the nearest bar of the right cell. It placed **zero**
    legal arrangements in 8,000 attempts, and the trajectory says why: the
    geometry cells are strongly clustered in time, because ATR/price is a
    regime variable and regimes persist. Cells 19-24 have a temporal standard
    deviation of 266-482 bars against 683 for a uniform spread. A snap therefore
    routinely has to jump 100-380 bars to find its own cell, the exact tiling
    has zero slack to absorb that, and the arrangement marches off the end of
    the series around block 13 of 47. Searching for a valid interleaving instead
    of repairing an invalid one is the fix. Loosening ``min_gap``, or letting a
    block start outside its cell, would have been the silent weakening this
    brief forbids.

    Returns ``(flags, nodes_used)``, or ``None`` when the search exhausts its
    budget without finding a complete tiling — the caller counts that as a
    placement failure and redraws.
    """
    count = int(run_lengths.size)
    if count == 0:
        return None

    # Plain Python lists in the hot loop. The search touches these millions of
    # times and numpy scalar indexing is an order of magnitude slower than a
    # list lookup at this granularity.
    cell_at = strata.tolist()
    gap_of = gaps.tolist()
    length_of = run_lengths.tolist()

    by_cell: Dict[int, List[int]] = {}
    for block in range(count):
        by_cell.setdefault(int(run_cells[block]), []).append(block)
    gaps_left = list(range(len(gap_of)))
    placed: List[Tuple[int, int]] = []
    nodes = [0]

    def extend(cursor: int, depth: int) -> bool:
        if depth == count:
            return True
        if nodes[0] >= node_budget:
            return False
        options: List[Tuple[int, int, int]] = []
        for gap_index in gaps_left:
            position = cursor + gap_of[gap_index]
            if position >= n_bars:
                continue
            cell = cell_at[position]
            if by_cell.get(cell):
                options.append((gap_index, position, cell))
        if not options:
            return False
        # Uniform order over the legal (gap, block) options. Slice 16 measured
        # three orderings -- proportional to how many blocks a gap unlocks,
        # uniform, and inverse -- at 7, 8 and 9 solutions per 120 searches:
        # indistinguishable. Uniform is kept because it is the cheapest and the
        # least opinionated about which part of the solution set to visit.
        for choice in rng.permutation(len(options)):
            gap_index, position, cell = options[choice]
            available = by_cell[cell]
            for offset in rng.permutation(len(available)):
                block = available[offset]
                length = length_of[block]
                if position + length > n_bars:
                    continue
                nodes[0] += 1
                available.remove(block)
                gaps_left.remove(gap_index)
                placed.append((position, length))
                if extend(position + length, depth + 1):
                    return True
                placed.pop()
                gaps_left.append(gap_index)
                available.append(block)
                if nodes[0] >= node_budget:
                    return False
        return False

    if not extend(warmup, 0):
        return None
    out = np.zeros(n_bars, dtype=bool)
    for start, length in placed:
        out[start:start + length] = True
    return out, float(nodes[0])


def shape_matched_flags(
    run_lengths: np.ndarray,
    gaps: np.ndarray,
    n_bars: int,
    *,
    warmup: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """A flag sequence with the same SHAPE as another and none of its content.

    Slice 13's bisection is the sharpest fact in this whole investigation:
    information-free flags of identical shape scored a median of 15.4 against
    their own null while the analyser's real flags scored 72.7, on series with
    no temporal structure. That pair localises the residual — but it was
    measured under the multi-entry-per-run map slice 17 replaced, so it has to
    be measured again before anything is built on it.

    This is the "information-free" side. It consumes the same multiset of run
    lengths and the same multiset of gaps, each element exactly once, in a
    random order:

        gap, run, gap, run, ..., run, gap

    Because the two multisets tile the post-warm-up region exactly, the
    arrangement lands on the last bar by arithmetic — no drift, no truncation,
    no rejection.

    THE POINT IS WHAT THIS FUNCTION CANNOT SEE
    ------------------------------------------
    It takes no bars, no prices, no ATR, no strata — only two multisets of
    integers and a generator. Independence from the price path is therefore
    structural rather than argued: there is no channel through which bar
    content could reach the placement. That is the whole design requirement for
    Side B, and it is enforced by the signature rather than by discipline.

    ZERO-LENGTH GAPS BELONG AT THE EDGES AND NOWHERE ELSE
    -----------------------------------------------------
    ``extract_blocks`` can return a gap of length 0, but only in two places:
    the leading gap when a run starts exactly at ``warmup``, and the trailing
    gap when a run ends on the last bar. Every *internal* gap is at least 1,
    because otherwise the two runs it separates would be one run.

    A naive shuffle of the gap multiset can therefore place a zero between two
    runs and silently **merge** them — which changes the run-length multiset
    the whole comparison depends on. A property test over 100 random shapes
    caught exactly that. Zeros are consequently assigned to the leading and
    trailing slots (at random when there is only one), and only the strictly
    positive gaps are shuffled through the interior. The multisets are then
    preserved exactly, which is asserted both here and at the call site.
    """
    count = int(run_lengths.size)
    out = np.zeros(n_bars, dtype=bool)
    if count == 0:
        return out

    values = [int(g) for g in gaps.tolist()]
    zeros = [g for g in values if g == 0]
    positive = [g for g in values if g > 0]
    if len(zeros) > 2:                      # cannot happen; refuse rather than
        raise ValueError(                   # produce a merged arrangement
            "more than two zero-length gaps: the source sequence cannot have "
            "come from extract_blocks"
        )
    shuffled = [positive[i] for i in rng.permutation(len(positive))]

    lead_zero = trail_zero = False
    if len(zeros) == 2:
        lead_zero = trail_zero = True
    elif len(zeros) == 1:
        if bool(rng.integers(2)):
            lead_zero = True
        else:
            trail_zero = True

    ordered: List[int] = []
    ordered.append(0 if lead_zero else shuffled.pop())
    interior = count - 1
    for _ in range(interior):
        ordered.append(shuffled.pop())
    ordered.append(0 if trail_zero else shuffled.pop())

    order = rng.permutation(count)
    cursor = int(warmup) + ordered[0]
    for slot, block in enumerate(order):
        length = int(run_lengths[block])
        out[cursor:cursor + length] = True
        cursor += length + ordered[slot + 1]
    return out


def simulate_schedule(
    flags: np.ndarray,
    *,
    warmup: int,
    lockup: int,
) -> List[int]:
    """Walk the bars applying the flat-only rule, and return the entry bars.

    This is the *mechanics* half of the endogenous null, and it is deliberately
    the identical function for both sides. Given a sequence of "the signal
    wanted to enter here" flags it produces the schedule that would actually
    have been traded: enter on the first bar of a run, be locked up for
    ``lockup`` bars, then look for the next run.

    ONE TRADE PER CONTIGUOUS RUN (slice 17)
    =======================================
    Before slice 17 this function entered on *any* flagged bar it landed on::

        while index < n:
            if flags[index]: entries.append(index); index += lockup
            else:            index += 1

    Two consequences, both measured in slice 16 and both fatal to the null:

    * a run longer than the lock-up traded **again** at its interior bars —
      47 runs implied 68 entries by ``ceil(length / lockup)``;
    * a run whose start fell inside a previous lock-up was entered later, at
      whichever interior bar the lock-up happened to expire on.

    Net: of 54 observed entries only 26 were run starts. A block placement
    controls where a run *starts* and nothing else, so it could not control the
    geometry of the bars the instrument actually scored — measured as
    TV(start geometry, entry geometry) = 0.2234 against a 0.2037 tolerance,
    on the observed data, before any resampling.

    The rule now is::

        a contiguous run of flags contributes AT MOST ONE entry,
        and if it contributes one it is on the run's FIRST bar

    Concretely: enter on the run's first bar, ignore every further flag inside
    that run, then advance to ``max(end_of_run, entry + lockup)``. If that
    lands inside a later run, that run's start is already behind us — so it
    contributes nothing and is skipped whole, rather than being entered at an
    interior bar. Gaps are untouched.

    Every entry is therefore a run start, by construction and not by
    approximation, which is asserted directly in ``tests/test_skill_test.py``.
    It is the ONLY change to the map from flags to entries, and it is applied
    identically to the observed flags and to every null replicate.

    WHAT IT COSTS, STATED PLAINLY
    -----------------------------
    On the real series the observed schedule goes from **54 entries to 34**.
    That is a change to a *scoring convention*, applied equally to both sides —
    not a threshold, not a risk limit, not a tolerance, and not a filter on
    which signals count. The strategy's own expectancy is recomputed under it.

    It does NOT make every run start an entry. 13 of the 47 runs begin inside a
    previous trade's lock-up and so trade nothing at all; ``at most one`` is
    load-bearing. Setting ``lockup=1`` collapses the rule to exactly one entry
    per run — 47 entries, and entry geometry identical to start geometry — at
    the cost of letting consecutive trades' scoring windows overlap. Both are
    measured in EDGE.md 4h; the shipped default keeps the lock-up.

    THE FIXED LOCK-UP, AND WHY IT REPLACED BARRIER RESOLUTION
    ---------------------------------------------------------
    Until slice 13 this function advanced by the *realised barrier-resolution
    time* of the bar just entered. That was faithful to the strategy and it was
    also the last remaining artefact in the instrument, because it coupled the
    schedule to the very outcomes being scored:

        a flag landing where the barrier resolved fast freed the account
        sooner, which changed WHICH later bars got sampled

    Renewal sampling with outcome feedback. It is not symmetric between the
    real flag sequence and a rotated one — the real sequence's lock-ups were
    generated by the bars it actually sat on, a rotated sequence's are not —
    so the two sides were not running the same process after all.

    A constant lock-up removes the feedback entirely. The schedule is now a
    deterministic function of the flag positions alone, so rotating the flags
    permutes the schedule and nothing else. The outcomes still differ between
    replicates, but they no longer decide *when the next sample is taken*.

    ``lockup`` defaults to the scoring horizon rather than to the observed
    median hold. Both were available; the horizon is a **configured constant**
    while the median hold is a statistic of the real schedule, and seeding the
    null with a number derived from the thing under test is the class of
    dependence this whole exercise exists to remove. It also makes the lock-up
    and the scoring window the same length, so no trade's score can overlap the
    next trade's entry.
    """
    entries: List[int] = []
    index = int(warmup)
    limit = flags.size
    step = max(1, int(lockup))
    while index < limit:
        if not flags[index]:
            index += 1
            continue
        # `index` is the FIRST bar of a run: either the walk arrived here from
        # an unflagged bar, or the skip below has just carried it past a whole
        # run. Both guarantee it, so an entry is always a run start.
        entries.append(index)
        end = index
        while end < limit and flags[end]:
            end += 1
        index = max(end, index + step)
        # If the lock-up expired inside a later run, that run's start is behind
        # us. It contributes NOTHING rather than being entered at an interior
        # bar, which is the whole point of the rule.
        while index < limit and flags[index]:
            index += 1
    return entries


def run_permutation(
    bars: Sequence,
    cfg: "bt.BacktestConfig",
    *,
    runs: int,
    seed: int,
    round_trip_bps: float,
    take_profit_atr: float,
    stop_atr: float,
    horizon: int,
    atr_period: int,
    warmup: int,
    lockup: int,
    strata_bins: int = 5,
    null_kind: str = "rotation",
    block_tries: int = 20_000,
    block_nodes: int = 2_000,
    block_target: int = 400,
    flags_override: Optional[np.ndarray] = None,
) -> Optional[SkillVerdict]:
    """Rank the strategy against itself with its information destroyed.

    THE ENDOGENOUS NULL
    ===================
    Slice 11's null slid the strategy's entry *schedule* to a random offset. It
    preserved the gaps, which fixed a genuine 2.5x understatement of the null's
    variance and took the real reading from 100.0 to 68.8 — but the control
    still read 84.5. Preserving spacing copies a symptom. The schedule was
    *produced* by the strategy reacting to that particular series, and moving it
    wholesale breaks a correspondence the strategy's own score retains.

    So both sides now generate their schedules through the **same process**:

        signal_flags()        -> did the signal want to enter on this bar?
        simulate_schedule()   -> flat-only, hold to barrier resolution, repeat
        barrier scoring       -> identical for both

    The strategy uses its real flag sequence. Each null replicate uses a
    **permutation** of that same sequence: the identical number of would-be
    entries, the identical marginal rate, run through the identical flat-only
    mechanics — but with every flag detached from the bar that earned it.

    The only surviving difference is whether the flags carry information. That
    is the hypothesis, stated as an experiment.

    Note what this buys that no amount of care with the previous null could:
    the clustering, the trade count, and the hold distribution are no longer
    *imitated*, they are **generated** by the same rule on both sides. A null
    that reproduces a symptom can always be wrong in some direction nobody
    thought to check; a null that runs the cause cannot differ except in the
    variable under test.
    """
    indices, net_r, bars_used = barrier_r_for_all_bars(
        bars, take_profit_atr=take_profit_atr, stop_atr=stop_atr,
        horizon=horizon, atr_period=atr_period, round_trip_bps=round_trip_bps,
    )
    if indices.size == 0:
        return None
    lookup = {int(idx): pos for pos, idx in enumerate(indices)}
    # Retained ONLY for the reported hold statistics. It is deliberately no
    # longer an input to scheduling — see simulate_schedule's docstring.
    resolution = {int(idx): int(bars_used[pos]) for pos, idx in enumerate(indices)}

    # ``flags_override`` is the ONLY way anything other than the real analyser
    # can supply the observed side, and it exists for exactly one purpose: the
    # slice-19 bisection feeds in information-free flags of identical shape so
    # both sides run byte-identical machinery. It defaults to None, in which
    # case this is the untouched slice-17 path.
    flags = (signal_flags(bars, cfg, warmup=warmup) if flags_override is None
             else np.asarray(flags_override, dtype=bool))
    if not flags.any():
        return None

    observed_entries = simulate_schedule(flags, warmup=warmup, lockup=lockup)
    chosen = [lookup[i] for i in observed_entries if i in lookup]
    if len(chosen) < 30:
        return None
    strategy_r = float(np.mean(net_r[chosen]))

    # ------------------------------------------------------------------
    # The null: MATCHED STRATIFIED SUBSTITUTION.
    # ------------------------------------------------------------------
    # Slice 13's bisection localised the residual precisely: information-free
    # flags of identical shape scored a median of 15.4 against their own
    # rotations, while the analyser's real flags scored 72.7. The rotation
    # operation was therefore not the bias; the bias attached to the analyser's
    # flags sitting on the bars that generated them. On a structure-free
    # surrogate that cannot be prediction, so it is a contemporaneous relation
    # between the analyser's firing condition and the barrier's geometry at the
    # same bar.
    #
    # So the null no longer MOVES the flags. It SUBSTITUTES them, one for one,
    # with non-flagged bars drawn from the same quantile cell of the barrier's
    # own inputs (ATR/price, position of close within its recent range). Both
    # sides then run the identical fixed-lock-up flat-only schedule and the
    # identical barrier scoring.
    #
    # What remains different is the one thing under test: the analyser said yes
    # on this geometry, and no on the matched geometry.
    rng = np.random.default_rng(seed)
    eligible_mask = np.zeros(flags.size, dtype=bool)
    eligible_mask[indices] = True
    eligible_mask[:warmup] = False
    atr_over_price, range_position = stratification_features(
        bars, atr_period=atr_period, range_lookback=atr_period,
    )
    strata = assign_strata(
        atr_over_price, range_position, eligible_mask, bins=strata_bins
    )

    n_cells = strata_bins * strata_bins
    observed_geometry = geometry_distribution(
        np.array(observed_entries, dtype=int), strata, n_cells
    )

    # ------------------------------------------------------------------
    # The tolerance is fixed HERE, by rule, before a single rotation is seen.
    # ------------------------------------------------------------------
    tolerance, floor_p10 = sampling_floor_tolerance(
        observed_geometry, len(observed_entries),
        resamples=2000, rng=np.random.default_rng(seed ^ 0x5EED),
    )

    collected: List[float] = []
    counts: List[int] = []
    matched_total = unmatched_total = 0
    accepted = attempted = 0
    accepted_distances: List[float] = []
    all_distances: List[float] = []
    snap_distances: List[float] = []
    placement_failures = 0
    region = (flags & eligible_mask)[warmup:].copy()

    # ------------------------------------------------------------------
    # Block structure, extracted once from the observed flag sequence.
    # ------------------------------------------------------------------
    eligible_flags = flags & eligible_mask
    runs_observed, gaps_observed = extract_blocks(eligible_flags, warmup=warmup)
    run_lengths = np.array([length for _, length in runs_observed], dtype=np.int64)
    run_cells = np.array([int(strata[start]) for start, _ in runs_observed],
                         dtype=np.int64)
    gaps_array = np.array(gaps_observed, dtype=np.int64)
    pools = candidate_sets(strata, eligible_mask, warmup=warmup)
    if null_kind == "block_resample" and (
        run_lengths.size == 0
        or any(int(cell) not in pools for cell in run_cells)
    ):
        return None

    # ------------------------------------------------------------------
    # THE DECOMPOSITION THAT DECIDES WHETHER THIS NULL CAN WORK AT ALL.
    # ------------------------------------------------------------------
    # A block placement controls exactly one thing: the bar a run STARTS on.
    # It does not control which bars become trades. Under a fixed lock-up a run
    # longer than the lock-up enters again at its interior bars, and a run whose
    # start falls inside the previous trade's lock-up is skipped entirely. So
    # before spending any search effort, measure how much of the entry schedule
    # the block starts actually account for, and how far the block-start
    # geometry sits from the entry geometry the tolerance is defined against.
    # If that gap already exceeds the tolerance, no placement can close it and
    # the refusal is structural rather than budgetary.
    start_bars = np.array([start for start, _ in runs_observed], dtype=int)
    entry_set = set(int(i) for i in observed_entries)
    starts_that_traded = sum(1 for s in start_bars if int(s) in entry_set)
    start_entry_fraction = (
        starts_that_traded / float(len(observed_entries))
        if observed_entries else float("nan")
    )
    start_geometry_gap = total_variation(
        geometry_distribution(start_bars, strata, n_cells), observed_geometry
    )

    # Deterministic sweep over every distinct offset, evaluated once. The
    # rotation family is small (~2,300 offsets), so sampling from it with
    # replacement would waste most of the budget re-testing offsets already
    # known to fail. Enumerating and then drawing from the ACCEPTED set is the
    # same null, computed without throwing work away.
    offsets = np.arange(1, max(2, region.size))
    if null_kind == "stratified_rotation":
        rotation_pool: List[np.ndarray] = []
        for offset in offsets:
            attempted += 1
            candidate = np.zeros_like(flags)
            candidate[warmup:] = np.roll(region, int(offset))
            entries = simulate_schedule(candidate, warmup=warmup, lockup=lockup)
            if not entries:
                continue
            distance = total_variation(
                geometry_distribution(np.array(entries, dtype=int), strata, n_cells),
                observed_geometry,
            )
            all_distances.append(distance)
            if distance <= tolerance:
                accepted += 1
                accepted_distances.append(distance)
                rotation_pool.append(candidate)
        if not rotation_pool:
            # Report the failure rather than loosening the tolerance. The
            # caller prints the acceptance rate and the best achievable
            # distance; forcing a pass here would be the workaround this whole
            # exercise exists to refuse.
            return SkillVerdict(
                n_trades=len(chosen), strategy_expectancy_r=strategy_r,
                baseline_mean_r=float("nan"), baseline_std_r=float("nan"),
                percentile=float("nan"), p_value=float("nan"), runs=0,
                median_hold_bars=0.0, exposure_fraction=0.0,
                asset_return=0.0, strategy_return=0.0,
                match_rate=0.0, strata_used=int(np.unique(strata[strata >= 0]).size),
                tolerance=tolerance, floor_p10=floor_p10,
                acceptance_rate=0.0,
                best_distance=min(all_distances) if all_distances else float("nan"),
                median_accepted_distance=float("nan"),
            )

    block_pool: List[np.ndarray] = []
    if null_kind == "block_resample":
        # Build the accepted set ONCE, exactly as stratified_rotation does, and
        # then draw replicates from it. A per-replicate rejection loop would
        # re-run the same expensive constraint search thousands of times to
        # discover the same answer.
        for _attempt in range(block_tries):
            attempted += 1
            built = block_resample_flags(
                run_lengths, run_cells, gaps_array, strata, flags.size,
                warmup=warmup, node_budget=block_nodes, rng=rng,
            )
            if built is None:
                placement_failures += 1
                continue
            candidate, nodes_used = built
            trial = simulate_schedule(candidate, warmup=warmup, lockup=lockup)
            if not trial:
                continue
            distance = total_variation(
                geometry_distribution(np.array(trial, dtype=int), strata, n_cells),
                observed_geometry,
            )
            all_distances.append(distance)
            snap_distances.append(nodes_used)
            if distance <= tolerance:
                accepted += 1
                accepted_distances.append(distance)
                block_pool.append(candidate)
                if len(block_pool) >= block_target:
                    break

    for _ in range(runs):
        if null_kind == "block_resample":
            if not block_pool:
                break
            null_flags = block_pool[int(rng.integers(len(block_pool)))]
            matched_total += int(null_flags.sum())
        elif null_kind == "stratified_rotation":
            null_flags = rotation_pool[int(rng.integers(len(rotation_pool)))]
            matched_total += int(region.sum())
        elif null_kind == "matched":
            null_flags, matched, unmatched = matched_flags(
                flags & eligible_mask, strata, rng
            )
            matched_total += matched
            unmatched_total += unmatched
        else:
            null_flags = np.zeros_like(flags)
            null_flags[warmup:] = np.roll(
                region, int(rng.integers(1, max(2, region.size)))
            )
            matched_total += int(region.sum())
        entries = simulate_schedule(null_flags, warmup=warmup, lockup=lockup)
        pick = [lookup[i] for i in entries if i in lookup]
        if len(pick) < 20:
            continue
        collected.append(float(np.mean(net_r[pick])))
        counts.append(len(pick))

    if null_kind == "block_resample" and accepted == 0:
        # Same refusal shape as stratified_rotation: report the acceptance rate
        # and the best achievable distance, do not loosen the tolerance.
        return SkillVerdict(
            n_trades=len(chosen), strategy_expectancy_r=strategy_r,
            baseline_mean_r=float("nan"), baseline_std_r=float("nan"),
            percentile=float("nan"), p_value=float("nan"), runs=0,
            median_hold_bars=0.0, exposure_fraction=0.0,
            asset_return=0.0, strategy_return=0.0,
            match_rate=0.0, strata_used=int(np.unique(strata[strata >= 0]).size),
            tolerance=tolerance, floor_p10=floor_p10, acceptance_rate=0.0,
            best_distance=min(all_distances) if all_distances else float("nan"),
            median_accepted_distance=float("nan"),
            n_blocks=int(run_lengths.size), snap_median=float("nan"),
            placement_failures=placement_failures,
            placements_searched=attempted,
            start_entry_fraction=start_entry_fraction,
            start_geometry_gap=start_geometry_gap,
        )

    if len(collected) < max(50, runs // 10):
        return None
    means = np.array(collected, dtype=float)
    match_rate = (
        matched_total / float(matched_total + unmatched_total)
        if (matched_total + unmatched_total) else 1.0
    )

    percentile = float((means < strategy_r).mean() * 100.0)
    p_value = float((means >= strategy_r).mean())

    close = np.array([b.close for b in bars], dtype=float)
    holds = [resolution.get(i, horizon) for i in observed_entries]
    return SkillVerdict(
        n_trades=len(chosen),
        strategy_expectancy_r=strategy_r,
        baseline_mean_r=float(means.mean()),
        baseline_std_r=float(means.std(ddof=1)),
        percentile=percentile,
        p_value=p_value,
        runs=len(collected),
        median_hold_bars=float(np.median(holds)) if holds else 0.0,
        exposure_fraction=(sum(holds) / float(close.size)) if holds else 0.0,
        asset_return=float((close[-1] - close[0]) / close[0]),
        strategy_return=float(np.median(counts)) if counts else 0.0,
        match_rate=match_rate,
        strata_used=int(np.unique(strata[strata >= 0]).size),
        tolerance=tolerance, floor_p10=floor_p10,
        acceptance_rate=(accepted / attempted) if attempted else 1.0,
        best_distance=min(all_distances) if all_distances else float("nan"),
        median_accepted_distance=(
            float(np.median(accepted_distances)) if accepted_distances
            else float("nan")
        ),
        n_blocks=int(run_lengths.size),
        snap_median=(
            float(np.median(snap_distances)) if snap_distances else float("nan")
        ),
        placement_failures=placement_failures,
        placements_searched=attempted,
        start_entry_fraction=start_entry_fraction,
        start_geometry_gap=start_geometry_gap,
    )


def surrogate_series(bars: Sequence, *, seed: int) -> List:
    """The same bars with their time structure destroyed, drift preserved.

    THE FALSIFICATION TEST
    ----------------------
    A percentile of 100 is exactly the kind of result that should be
    distrusted before it is published. So: shuffle the daily log returns
    independently, rebuild a price path from them, and run the identical
    strategy and the identical permutation on it.

    The shuffled series has the same length, the same return distribution and
    therefore the same total drift — but **no temporal structure at all**.
    There is no trend to follow, no momentum, no mean reversion. A strategy
    whose entry timing measures something real must score near the 50th
    percentile here, because on this series there is nothing to time.

    If it still scores 100, the test is measuring an artefact of the
    instrument rather than a property of the market, and the whole result has
    to be thrown away. That is the point of running it.

    WHOLE BARS ARE SHUFFLED, NOT CLOSE-TO-CLOSE RETURNS
    ---------------------------------------------------
    Slice 11 shuffled only the close-to-close returns and left each date's
    high/low offsets attached to that date, so intrabar range structure — and
    therefore ATR structure — survived the shuffle. Here each bar moves as a
    unit and is re-based onto the running price: its internal shape travels
    with it, and every relationship *between* bars is destroyed. Measured on
    the fixed version, |return| autocorrelation falls from +0.228 on real data
    to -0.037.
    """
    rng = np.random.default_rng(seed)
    order = rng.permutation(len(bars))
    out: List = []
    price = bars[0].close
    for position, source in enumerate(order):
        bar = bars[source]
        if bar.open <= 0:
            continue
        # Re-base the whole bar onto the running price, preserving its internal
        # shape: its open-to-close return, its wick proportions and therefore
        # its true range all travel with it.
        scale = price / bar.open
        out.append(bt.Bar(
            bars[position].start_ms, bar.open * scale, bar.high * scale,
            bar.low * scale, bar.close * scale, bar.volume,
        ))
        price = out[-1].close
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=os.path.join(REPO, "data", "real_1d"))
    parser.add_argument("--interval", default="D")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--runs", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=20250726)
    parser.add_argument("--round-trip-bps", type=float, default=25.0)
    parser.add_argument("--min-confidence", type=float, default=0.12)
    parser.add_argument("--min-agreement", type=float, default=0.40)
    # The barrier both sides are scored by. Defaults match the shipped
    # strategy: a 2-ATR stop with a 2:1 reward is a 4-ATR target.
    parser.add_argument("--take-profit-atr", type=float, default=4.0)
    parser.add_argument("--stop-atr", type=float, default=2.0)
    parser.add_argument("--horizon", type=int, default=24)
    parser.add_argument("--atr-period", type=int, default=14)
    # The FIXED lock-up. Defaults to the scoring horizon: a configured constant
    # rather than a statistic of the schedule under test. 0 means "use the
    # horizon". See simulate_schedule's docstring for why this is not the
    # observed median hold.
    parser.add_argument("--lockup", type=int, default=0)
    # Quantile bins PER FEATURE, so bins**2 strata. 5 gives 25 cells over ~2,300
    # eligible bars -- about 90 bars per cell against ~54 flags, enough to draw
    # without replacement and still leave the cell populated.
    parser.add_argument("--strata-bins", type=int, default=5)
    # Which null. All FOUR are measured in EDGE.md 4d-4g and NONE is valid yet.
    # `rotation` is the default only because it measured least invalid (control
    # median 73.1 against 96.8; the other two cannot be constructed at all).
    # Selectable so the next attempt can compare against all of them without
    # re-deriving them.
    parser.add_argument("--null", dest="null_kind", default="rotation",
                        choices=("rotation", "matched", "stratified_rotation",
                                 "block_resample"))
    parser.add_argument("--block-tries", type=int, default=20_000,
                        help="cap on placement searches; each is an independent "
                             "attempt at an exact re-tiling of the series")
    parser.add_argument("--block-nodes", type=int, default=2_000,
                        help="search nodes per placement before that placement "
                             "is abandoned. Small on purpose -- a random "
                             "restart is cheaper than a deep search")
    parser.add_argument("--block-target", type=int, default=400,
                        help="stop searching once this many placements have "
                             "been ACCEPTED; the null is drawn from that pool")
    parser.add_argument("--control-runs", type=int, default=5,
                        help="how many shuffled-return surrogates to run")
    parser.add_argument("--control-ceiling", type=float, default=75.0,
                        help="a surrogate above this invalidates the test")
    parser.add_argument("--surrogate", action="store_true",
                        help="destroy the time structure and re-run: the test "
                             "must return ~50th percentile on this")
    args = parser.parse_args(argv)

    loaded, _books, notes = md.load_corpus(args.data_dir)
    symbol = args.symbol or sorted(loaded)[0]
    bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
            for b in loaded[symbol]]

    print("=" * 78)
    print("DATA PROVENANCE")
    print("=" * 78)
    for note in notes:
        print(note)
    print(f"symbol            : {symbol}")
    print(f"bars              : {len(bars):,}")
    if args.surrogate:
        bars = surrogate_series(bars, seed=args.seed)
        print()
        print("*** SURROGATE MODE ***")
        print("Daily log returns have been shuffled. Same length, same return")
        print("distribution, same total drift, NO temporal structure. There is")
        print("nothing here to time, so a sound test must report ~50th")
        print("percentile. Anything near 100 means the instrument is broken.")

    cfg = bt.BacktestConfig(
        starting_cash=10_000.0, warmup_bars=200, interval=args.interval,
        min_confidence=args.min_confidence,
        min_component_agreement=args.min_agreement,
    )
    print()
    print(f"lock-up            : {args.lockup or args.horizon} bars, FIXED "
          "(schedule cannot depend on when a barrier resolved)")
    print()
    print("computing the signal flags with the REAL analyser, bar by bar …")
    verdict = run_permutation(
        bars, cfg, runs=args.runs, seed=args.seed,
        round_trip_bps=args.round_trip_bps,
        take_profit_atr=args.take_profit_atr, stop_atr=args.stop_atr,
        horizon=args.horizon, atr_period=args.atr_period,
        warmup=cfg.warmup_bars, lockup=args.lockup or args.horizon,
        strata_bins=args.strata_bins, null_kind=args.null_kind,
        block_tries=args.block_tries, block_nodes=args.block_nodes,
        block_target=args.block_target,
    )

    print()
    print("=" * 78)
    print("SKILL TEST — matched-exposure permutation")
    print("=" * 78)
    if verdict is None:
        print("REFUSED: fewer than 30 usable trades. A percentile computed from")
        print("a dozen trades is noise wearing a statistic's clothes.")
        return 1

    if verdict.runs == 0:
        print("=" * 78)
        print("SKILL TEST — REFUSED")
        print("=" * 78)
        if args.null_kind == "block_resample":
            print("No block placement met the pre-declared geometry tolerance.")
            print(f"  blocks re-tiled per placement      : {verdict.n_blocks}")
            print(f"  placements searched                : "
                  f"{verdict.placements_searched}")
            print(f"  searches that found no legal tiling : "
                  f"{verdict.placement_failures}")
            print(f"  tolerance (median resample floor)  : {verdict.tolerance:.4f}")
            print(f"  best achievable TV distance        : "
                  f"{verdict.best_distance:.4f}")
            print("  acceptance rate                    : 0.0%")
            print()
            print("AND THE REASON IS STRUCTURAL, NOT BUDGETARY:")
            print(f"  observed entries that are block STARTS : "
                  f"{verdict.start_entry_fraction:.1%}")
            print(f"  TV(block-start geometry, entry geometry): "
                  f"{verdict.start_geometry_gap:.4f}")
            print()
            print("A placement controls where a run STARTS and nothing else. But")
            print("under a fixed lock-up most trades are not run starts: a run")
            print("longer than the lock-up trades again at its interior bars, and")
            print("a run starting inside the previous trade's lock-up never trades")
            print("at all. The geometry of the block starts is therefore already")
            print("further from the entry geometry than the tolerance allows —")
            print("measured on the OBSERVED data, before any resampling. Placing")
            print("blocks perfectly cannot close a gap that perfect placement")
            print("leaves open.")
        else:
            print("No rotation offset met the pre-declared geometry tolerance.")
            print(f"  tolerance (median resample floor) : {verdict.tolerance:.4f}")
            print(f"  best achievable TV distance       : "
                  f"{verdict.best_distance:.4f}")
            print(f"  acceptance rate                   : 0.0% of "
                  f"{verdict.strata_used} populated cells' worth of offsets")
        print()
        print("The tolerance was NOT loosened to force a pass. A null whose")
        print("geometry cannot be matched is a null that cannot answer the")
        print("question, and saying so is the only honest output available.")
        return 2

    print(f"replicates         : {verdict.runs:,} runs, null = {args.null_kind}")
    print("Both sides run the SAME machinery -- same signal rate, same gates,")
    print("same fixed lock-up, same barrier scoring.")
    if args.null_kind == "matched":
        print("MATCHED: each flagged bar is replaced by a NON-flagged bar from")
        print("the same quantile cell of the barrier's own inputs (ATR/price,")
        print("close-within-recent-range). Matches geometry; destroys clustering.")
        print(f"stratification     : {args.strata_bins}x{args.strata_bins} "
              f"quantile cells, {verdict.strata_used} populated; "
              f"match rate {verdict.match_rate:.1%}")
    elif args.null_kind == "block_resample":
        print("BLOCK RESAMPLE: the observed flag sequence is decomposed into "
              f"{verdict.n_blocks} runs")
        print("and their gaps. Every replicate re-tiles the series with the "
              "SAME multiset")
        print("of run lengths -- so clustering and trade counts are preserved "
              "exactly, not")
        print("imitated -- while each run is placed on a bar drawn from its own "
              "geometry")
        print("cell. All three constraints hold at once; none is traded for "
              "another.")
        print(f"stratification     : {args.strata_bins}x{args.strata_bins} "
              f"quantile cells, {verdict.strata_used} populated")
        print(f"tolerance          : TV <= {verdict.tolerance:.4f} "
              f"(median resample floor; p10 {verdict.floor_p10:.4f})")
        # Two DIFFERENT rates, reported separately because conflating them
        # hides which constraint is doing the rejecting. A search can fail
        # because no legal exact re-tiling was found inside the node budget,
        # or it can succeed and then be rejected on geometry.
        searched = max(1, verdict.placements_searched)
        solved = searched - verdict.placement_failures
        print(f"placements         : {verdict.placements_searched:,} searched, "
              f"{solved:,} legal re-tilings ({solved / searched:.1%} solve rate)")
        print(f"geometry acceptance: {verdict.acceptance_rate:.1%} of searches, "
              f"{(verdict.acceptance_rate * searched / max(1, solved)):.1%} of "
              f"legal re-tilings; best TV {verdict.best_distance:.4f}, "
              f"median accepted {verdict.median_accepted_distance:.4f}")
        print(f"entries that are run STARTS: "
              f"{verdict.start_entry_fraction:.1%}  <- must be 100%; "
              f"TV(all-starts, entries) {verdict.start_geometry_gap:.4f}")
    elif args.null_kind == "stratified_rotation":
        print("STRATIFIED ROTATION: rotations preserve every run and gap, so")
        print("clustering and trade counts stay matched; an offset is ACCEPTED")
        print("only when its flags' distribution over the 25 geometry cells is")
        print("within a pre-declared total-variation tolerance of the observed")
        print("one. All three constraints held at once, not traded off.")
        print(f"tolerance          : TV <= {verdict.tolerance:.4f} "
              f"(median resample floor; p10 {verdict.floor_p10:.4f})")
        print(f"acceptance         : {verdict.acceptance_rate:.1%} of offsets; "
              f"best TV {verdict.best_distance:.4f}, "
              f"median accepted {verdict.median_accepted_distance:.4f}")
    else:
        print("ROTATION: the flag sequence is rotated. Preserves every run and")
        print("gap, so clustering and trade counts stay matched; does NOT match")
        print("the barrier geometry. NEITHER null is valid yet -- see EDGE.md.")
    print("Both sides are scored by the SAME triple barrier on the SAME series")
    print(f"with the SAME costs (+{args.take_profit_atr:g} / -{args.stop_atr:g} ATR, "
          f"{args.horizon}-bar horizon, {args.round_trip_bps:g} bps).")
    print("The ONLY difference is which bars were chosen. These are idealised")
    print("single-barrier R values, not the backtest's P&L: the right instrument")
    print("for comparing two sets of entry bars, the wrong one for a forecast.")
    print()
    print(f"strategy expectancy: {verdict.strategy_expectancy_r:+.4f} R")
    print(f"random-entry mean  : {verdict.baseline_mean_r:+.4f} R  "
          f"(sd {verdict.baseline_std_r:.4f})")
    print(f"strategy percentile: {verdict.percentile:.1f}")
    print(f"one-sided p        : {verdict.p_value:.4f}")
    print()
    print(f"trades             : {verdict.n_trades}  "
          f"(null median {verdict.strategy_return:.0f})")
    print(f"median hold        : {verdict.median_hold_bars:.0f} bars")
    print(f"time in market     : {verdict.exposure_fraction:.1%} of the sample")
    print(f"asset over sample  : {verdict.asset_return:+.1%}")
    print()
    print(f"raw reading        : {verdict.verdict}")

    if args.surrogate:
        # We ARE the control. Nothing further to run.
        print()
        print("This was the control run. A sound instrument reports ~50 here.")
        return 0

    # ------------------------------------------------------------------
    # The control, run automatically, every time.
    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print("CONTROL — the same test on series with NO temporal structure")
    print("=" * 78)
    print("A percentile is a claim about timing. On a shuffled-return series")
    print("there is nothing to time, so the same instrument must report ~50.")
    print("If it does not, the reading above is an artefact of the instrument")
    print("and cannot be reported as skill. This control runs unconditionally,")
    print("because a control you have to remember to run is a control that")
    print("gets skipped exactly when the result is exciting.")
    print()

    controls: List[float] = []
    for offset in range(args.control_runs):
        shuffled = surrogate_series(bars, seed=args.seed + 1000 + offset)
        control_verdict = run_permutation(
            shuffled, cfg,
            runs=max(500, args.runs // 5), seed=args.seed + offset,
            round_trip_bps=args.round_trip_bps,
            take_profit_atr=args.take_profit_atr, stop_atr=args.stop_atr,
            horizon=args.horizon, atr_period=args.atr_period,
            warmup=cfg.warmup_bars, lockup=args.lockup or args.horizon,
            strata_bins=args.strata_bins, null_kind=args.null_kind,
            block_tries=args.block_tries, block_nodes=args.block_nodes,
            block_target=args.block_target,
        )
        if control_verdict is None:
            print(f"  surrogate {offset + 1}: too few trades to rank")
            continue
        if control_verdict.runs == 0:
            print(f"  surrogate {offset + 1}: no replicate met the geometry "
                  f"tolerance (best TV {control_verdict.best_distance:.4f} "
                  f"vs {control_verdict.tolerance:.4f})")
            continue
        controls.append(control_verdict.percentile)
        print(f"  surrogate {offset + 1}: percentile "
              f"{control_verdict.percentile:>5.1f}  "
              f"({control_verdict.n_trades} trades)")

    print()
    print("=" * 78)
    print("VERDICT")
    print("=" * 78)
    if not controls:
        print("INSTRUMENT UNUSABLE: no surrogate produced enough trades to rank.")
        return 2

    worst_control = max(controls)
    median_control = float(np.median(controls))
    mean_control = float(np.mean(controls))
    print(f"surrogate percentiles: median {median_control:.1f}, "
          f"mean {mean_control:.1f}, worst {worst_control:.1f}  (must be near 50)")

    # A VALID instrument does not merely score "near 50" on surrogates -- its
    # surrogate percentiles are UNIFORM on [0,100], because a percentile is by
    # construction uniform under its own null. Reporting the mean against that
    # expectation is a far more powerful check than a median, and it is the
    # honest way to read a small sample.
    #
    # Note also what a max-based criterion costs: for a PERFECT instrument,
    # P(worst <= 75) is 0.75^n -- 24% at n=5 and 3.2% at n=12. The "worst <= 75"
    # bar is therefore close to unpassable once the control is run properly,
    # which is a property of the criterion rather than of the instrument.
    if len(controls) >= 5:
        standard_error = 28.87 / np.sqrt(len(controls))   # sd of U(0,100)
        z = (mean_control - 50.0) / standard_error
        print(f"uniformity check     : mean {mean_control:.1f} vs 50 expected, "
              f"z = {z:+.2f} over {len(controls)} surrogates")
        print(f"                       (P(worst<=75) for a PERFECT instrument "
              f"at n={len(controls)} is {0.75 ** len(controls):.3f})")
    print(f"real-series percentile: {verdict.percentile:.1f}")
    print()

    # ------------------------------------------------------------------
    # The CONTROL verdict. Slice 26 hygiene: this exit code answers "is the
    # instrument valid?", NOT "is there skill?".
    # ------------------------------------------------------------------
    # Before slice 26 this function exited 1 whenever `worst_control` crossed a
    # 75 ceiling. That ceiling was never a human-declared gate, and a CORRECT
    # instrument fails it with near certainty: P(all n draws <= 75) = 0.75**n,
    # which is 0.032 at n=12 and about 1e-25 at n=200. Slice 23's validated
    # control -- median 48.0, z = -1.18, 0 of 200 incomplete, KS p = 0.435 --
    # exited 1 on it. A ruler whose exit code contradicts its own reading is a
    # ruler that lies, so the code now follows the criteria the humans actually
    # declared (EDGE.md 7b):
    #
    #     exit 0   control ran, incompletes within budget, median <= 50,
    #              uniformity does not reject
    #     exit 1   control ran and failed that rule
    #     exit 2   no null could be built
    #
    # `worst` is still printed on every run and is never a gate.
    #
    # The edge claim lives in tools/edge_measurement.py, which is unchanged and
    # still exits non-zero when M1 or M2 misses its 95.0 bar. Nothing here says
    # anything about edge, and the output below says so in as many words.
    incomplete = int(args.control_runs) - len(controls)
    incomplete_budget = max(1, int(0.05 * int(args.control_runs)))
    median_ok = median_control <= 50.0
    uniformity_testable = len(controls) >= 5
    uniformity_ok = uniformity_testable and abs(
        (mean_control - 50.0) / (28.87 / np.sqrt(len(controls)))) < 1.96
    completeness_ok = incomplete <= incomplete_budget

    print(f"incomplete surrogates: {incomplete} of {args.control_runs}   "
          f"budget {incomplete_budget}  "
          f"-> {'ok' if completeness_ok else 'OVER BUDGET'}")
    if worst_control >= args.control_ceiling:
        print(f"note: worst {worst_control:.1f} exceeds the legacy {args.control_ceiling:.0f} "
              f"ceiling. That ceiling is NOT a gate -- a PERFECT instrument")
        print(f"      clears it with probability {0.75 ** len(controls):.3g} at "
              f"n={len(controls)}. Reported, never gated. See EDGE.md 7b.")
    if not uniformity_testable:
        print(f"note: {len(controls)} rankable surrogates is too few to test "
              "uniformity (needs >= 5).")
        print("      The control is therefore NOT certified -- fail closed.")
    print()

    control_valid = median_ok and uniformity_ok and completeness_ok
    if not control_valid:
        print("CONTROL: **INVALID — the instrument cannot be trusted here.**")
        print()
        reasons = []
        if not median_ok:
            reasons.append(f"median {median_control:.1f} > 50")
        if not uniformity_ok:
            reasons.append("uniformity rejects" if uniformity_testable
                           else "uniformity not testable")
        if not completeness_ok:
            reasons.append(f"{incomplete} incomplete > budget {incomplete_budget}")
        print("  failed: " + "; ".join(reasons))
        print()
        print("On a series whose time structure has been destroyed the strategy")
        print("CANNOT have timed anything, so a control that does not look")
        print("uniform is measuring a property of the instrument, not of the")
        print(f"market. The real-series reading of {verdict.percentile:.1f} above is")
        print("therefore NOT evidence of skill and must not be interpreted.")
        return 1

    print(f"CONTROL: **VALID** — median {median_control:.1f} <= 50, uniformity "
          "does not reject, incompletes within budget.")
    print()
    print("THIS SAYS NOTHING ABOUT EDGE. It says the ruler does not lie: on")
    print("series where nothing can be timed, this instrument reports what a")
    print("correct instrument must. Whether the strategy has timing skill is a")
    print("different question, answered by tools/edge_measurement.py against")
    print("pre-declared M1/M2 bars of 95.0 -- and for this analyser the answer")
    print("was ABSENT on 1D, 4H and 1H (EDGE.md 5c and 6d, STAGE1_VERDICT.md).")
    print()
    print(f"The real-series percentile printed above ({verdict.percentile:.1f})")
    print("is NOT a skill claim and must not be used as one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
