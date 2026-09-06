#!/usr/bin/env python3
"""Is the barrier's risk unit contaminated by the analyser's own look-back?

THE HYPOTHESIS, STATED BEFORE IT IS TESTED
==========================================
Eight null designs have now been measured (EDGE.md 4a-4h). The control sits at
a median of 71-73 on series with **no temporal structure**, where the strategy
cannot have timed anything. Slice 17 established what the residual is *not*:
matching the barrier's own (ATR/price, range-position) geometry exactly — entry
geometry TV 0.0000, run lengths, gaps and trade counts all exact — moved the
control by essentially nothing against slice 13's null, which matched that
geometry not at all.

So the coupling runs through something the 5x5 cells do not capture. One
candidate is mechanical rather than statistical:

    The barrier's risk unit is ATR(14) ending AT THE ENTRY BAR. That is the
    same trailing window the analyser reads to decide whether to fire. If the
    analyser systematically fires where backward ATR **overstates** the
    volatility that actually follows, then a nominal 2-ATR stop is further away
    in real terms on the strategy's bars than on the null's, even though both
    sides are scored by identical nominal arithmetic. A softer real stop means
    fewer stop-outs and more timeouts resolving into drift — an upward bias
    with no timing skill in it, on any series, including a structure-free one.

That is a specific, falsifiable, purely mechanical claim, and this tool tests
it and nothing else.

WHAT IS MEASURED
----------------
For every bar of every surrogate series:

    backward_atr_over_price[i] = Wilder ATR(14) ending at bar i / close[i]
    forward_realised_vol[i]    = (max(high) - min(low)) / close[i]
                                 over bars i+1 .. i+24

and their ratio. The comparison is between the bars the analyser turned into
ENTRIES and same-cell bars it did not flag. If the hypothesis holds, the
flagged bars have the larger ratio.

WHY THE COMPARISON IS RUN ONLY ON SURROGATES
--------------------------------------------
On the real series a difference between flagged and unflagged bars could be
skill, drift, volatility clustering or contamination, and nothing here could
separate them. On a structure-free surrogate there is nothing to time, so any
systematic difference is mechanical by construction. Running this on real data
would answer a different question badly, so the tool refuses to.

WHY forward_realised_vol IS A RANGE AND NOT A RETURN STANDARD DEVIATION
-----------------------------------------------------------------------
Both were available; the choice is frozen before any number was looked at. The
barrier is a **touch** rule — it resolves when a high or a low crosses a level,
not when a close does. The extreme of the path over the horizon is therefore
the quantity that decides the outcome, and the high-low range measures exactly
that. A close-to-close standard deviation would answer a related but different
question and would be systematically smaller for the same path. The range is
also on the same footing as ATR, which is itself built from true ranges.

Usage
-----
    python3 tools/residual_diagnostic.py --surrogates 12
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
import sweep_geometry as sweep

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: A cell with fewer than this many unflagged partners is DROPPED for that
#: surrogate rather than matched loosely. A loose match reintroduces exactly
#: the confound the stratification exists to remove, and the count of dropped
#: cells is reported rather than buried.
MIN_PARTNERS = 5


def backward_atr_over_price(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, *, period: int = 14,
) -> np.ndarray:
    """Wilder ATR(``period``) ending at each bar, divided by that bar's close.

    This is the barrier's own risk unit, expressed scale-free. It is computed
    by the SAME function the sweep and the skill test use
    (``sweep_geometry.wilder_atr``), not a reimplementation, so the diagnostic
    cannot disagree with the instrument it is diagnosing.

    Uses only bars up to and including ``i``. NaN during the warm-up.
    """
    atr = sweep.wilder_atr(high, low, close, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(close > 0, atr / close, np.nan)
    return out


def forward_realised_vol(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, *, horizon: int = 24,
) -> np.ndarray:
    """High-low range over bars ``i+1 .. i+horizon``, divided by ``close[i]``.

    The quantity a *touch* barrier actually resolves against: the extreme of
    the path ahead, not the dispersion of its closes.

    Strictly forward-looking and strictly exclusive of bar ``i`` itself, so it
    shares no bar with :func:`backward_atr_over_price`. That disjointness is
    the point — if the two overlapped, a ratio between them would be partly a
    statement about one bar counted twice.

    NaN where fewer than ``horizon`` bars follow. This function is used ONLY
    for diagnosis; nothing in the trading path or the skill test consumes it,
    so no lookahead can leak into a decision.
    """
    n = close.size
    out = np.full(n, np.nan)
    if horizon < 1:
        return out
    for i in range(n - horizon):
        window_high = high[i + 1: i + 1 + horizon].max()
        window_low = low[i + 1: i + 1 + horizon].min()
        if close[i] > 0:
            out[i] = (window_high - window_low) / close[i]
    return out


def mann_whitney_u(a: np.ndarray, b: np.ndarray) -> Tuple[float, float]:
    """Two-sided Mann-Whitney U with a normal approximation and tie correction.

    Written out rather than imported so the test has no scipy dependency and so
    the tie handling is visible: these are ratios of two continuous quantities
    but ties still occur at the resolution of float rounding, and an
    uncorrected variance would overstate significance.

    Returns ``(u_statistic, two_sided_p)``. NaN when either sample is empty.
    """
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]
    n1, n2 = a.size, b.size
    if n1 == 0 or n2 == 0:
        return float("nan"), float("nan")

    combined = np.concatenate([a, b])
    order = np.argsort(combined, kind="mergesort")
    ranks = np.empty(combined.size, dtype=float)
    sorted_values = combined[order]
    index = 0
    tie_term = 0.0
    while index < sorted_values.size:
        end = index
        while end + 1 < sorted_values.size and sorted_values[end + 1] == sorted_values[index]:
            end += 1
        average_rank = 0.5 * (index + end) + 1.0
        ranks[order[index:end + 1]] = average_rank
        length = end - index + 1
        if length > 1:
            tie_term += length ** 3 - length
        index = end + 1

    rank_sum_a = float(ranks[:n1].sum())
    u_a = rank_sum_a - n1 * (n1 + 1) / 2.0
    total = float(n1 + n2)
    mean_u = n1 * n2 / 2.0
    variance = (n1 * n2 / 12.0) * ((total + 1.0) - tie_term / (total * (total - 1.0)))
    if variance <= 0:
        return float(u_a), float("nan")
    z = (u_a - mean_u) / np.sqrt(variance)
    # Two-sided normal tail, via the error function.
    from math import erfc, sqrt
    p_value = erfc(abs(z) / sqrt(2.0))
    return float(u_a), float(p_value)


@dataclass(frozen=True)
class SurrogateResult:
    """One surrogate's worth of the comparison. Every field is measured."""

    seed: int
    n_flagged: int
    n_unflagged: int
    cells_used: int
    cells_dropped: int
    bars_dropped: int
    median_ratio_flagged: float
    median_ratio_unflagged: float
    fraction_flagged_over_one: float
    fraction_unflagged_over_one: float
    median_atr_flagged: float
    median_atr_unflagged: float
    median_forward_flagged: float
    median_forward_unflagged: float
    p_value: float
    #: Per-cell ratio of the flagged bars' median ATR/price to the SAME cell's
    #: unflagged median. Reported because the pooled ratio of the two medians
    #: is a composition effect as much as a within-cell one: flagged bars are
    #: not spread across the cells the way the partner pool is, and the cells
    #: are themselves ATR quantiles. A claim about the risk unit has to be made
    #: within cells or not at all.
    cell_atr_ratios: Tuple[float, ...] = ()

    @property
    def gap(self) -> float:
        return self.median_ratio_flagged - self.median_ratio_unflagged


def analyse_surrogate(
    bars: Sequence,
    cfg: "bt.BacktestConfig",
    *,
    seed: int,
    warmup: int,
    lockup: int,
    horizon: int,
    atr_period: int,
    strata_bins: int,
) -> Optional[Tuple[SurrogateResult, np.ndarray, np.ndarray]]:
    """Run the whole comparison on ONE surrogate series.

    Returns ``(result, ratios_flagged, ratios_unflagged)`` so the caller can
    pool the raw ratios across surrogates for a single test with real power,
    rather than only combining per-surrogate summaries.
    """
    surrogate = sk.surrogate_series(bars, seed=seed)
    high = np.array([b.high for b in surrogate], dtype=float)
    low = np.array([b.low for b in surrogate], dtype=float)
    close = np.array([b.close for b in surrogate], dtype=float)

    backward = backward_atr_over_price(high, low, close, period=atr_period)
    forward = forward_realised_vol(high, low, close, horizon=horizon)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(forward > 0, backward / forward, np.nan)

    # The strata, built exactly as the skill test builds them.
    indices, _net, _used = sk.barrier_r_for_all_bars(
        surrogate, take_profit_atr=4.0, stop_atr=2.0, horizon=horizon,
        atr_period=atr_period, round_trip_bps=25.0,
    )
    if indices.size == 0:
        return None
    eligible = np.zeros(close.size, dtype=bool)
    eligible[indices] = True
    eligible[:warmup] = False
    eligible &= np.isfinite(ratio)

    atr_over_price, range_position = sk.stratification_features(
        surrogate, atr_period=atr_period, range_lookback=atr_period,
    )
    strata = sk.assign_strata(
        atr_over_price, range_position, eligible, bins=strata_bins
    )

    flags = sk.signal_flags(surrogate, cfg, warmup=warmup)
    entries = sk.simulate_schedule(flags & eligible, warmup=warmup, lockup=lockup)
    entries = [i for i in entries if eligible[i]]
    if not entries:
        return None

    # Partner pools: same stratum, eligible, NOT flagged at all. "Not flagged"
    # rather than "not an entry" — a bar the analyser wanted but the lock-up
    # swallowed is still a bar the analyser wanted, and putting it in the
    # control group would dilute exactly the difference under test.
    flagged_mask = flags & eligible
    partners: Dict[int, List[int]] = {}
    for index in np.nonzero(eligible & (strata >= 0) & ~flagged_mask)[0]:
        partners.setdefault(int(strata[index]), []).append(int(index))

    keep_flagged: List[int] = []
    keep_unflagged: List[int] = []
    cells_used = set()
    cells_dropped = set()
    bars_dropped = 0
    cell_atr_ratios: List[float] = []
    for cell in sorted({int(strata[i]) for i in entries if strata[i] >= 0}):
        pool = partners.get(cell, [])
        members = [i for i in entries if int(strata[i]) == cell]
        if len(pool) < MIN_PARTNERS:
            cells_dropped.add(cell)
            bars_dropped += len(members)
            continue
        cells_used.add(cell)
        keep_flagged.extend(members)
        keep_unflagged.extend(pool)
        flagged_atr = np.nanmedian(backward[np.array(members, dtype=int)])
        partner_atr = np.nanmedian(backward[np.array(pool, dtype=int)])
        if np.isfinite(flagged_atr) and np.isfinite(partner_atr) and partner_atr > 0:
            cell_atr_ratios.append(float(flagged_atr / partner_atr))

    if not keep_flagged or not keep_unflagged:
        return None

    ratios_flagged = ratio[np.array(keep_flagged, dtype=int)]
    ratios_unflagged = ratio[np.array(keep_unflagged, dtype=int)]
    ratios_flagged = ratios_flagged[np.isfinite(ratios_flagged)]
    ratios_unflagged = ratios_unflagged[np.isfinite(ratios_unflagged)]
    if ratios_flagged.size == 0 or ratios_unflagged.size == 0:
        return None

    _u, p_value = mann_whitney_u(ratios_flagged, ratios_unflagged)
    flagged_index = np.array(keep_flagged, dtype=int)
    unflagged_index = np.array(keep_unflagged, dtype=int)

    result = SurrogateResult(
        seed=seed,
        n_flagged=int(ratios_flagged.size),
        n_unflagged=int(ratios_unflagged.size),
        cells_used=len(cells_used),
        cells_dropped=len(cells_dropped),
        bars_dropped=bars_dropped,
        median_ratio_flagged=float(np.median(ratios_flagged)),
        median_ratio_unflagged=float(np.median(ratios_unflagged)),
        fraction_flagged_over_one=float((ratios_flagged > 1.0).mean()),
        fraction_unflagged_over_one=float((ratios_unflagged > 1.0).mean()),
        median_atr_flagged=float(np.nanmedian(backward[flagged_index])),
        median_atr_unflagged=float(np.nanmedian(backward[unflagged_index])),
        median_forward_flagged=float(np.nanmedian(forward[flagged_index])),
        median_forward_unflagged=float(np.nanmedian(forward[unflagged_index])),
        p_value=p_value,
        cell_atr_ratios=tuple(cell_atr_ratios),
    )
    return result, ratios_flagged, ratios_unflagged


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=os.path.join(REPO, "data", "real_1d"))
    parser.add_argument("--interval", default="D")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--surrogates", type=int, default=12)
    parser.add_argument("--seed", type=int, default=20250727)
    parser.add_argument("--horizon", type=int, default=24)
    parser.add_argument("--atr-period", type=int, default=14)
    parser.add_argument("--strata-bins", type=int, default=5)
    parser.add_argument("--lockup", type=int, default=1,
                        help="1 reproduces the one-trade-per-run schedule whose "
                             "entry geometry matched the null exactly")
    parser.add_argument("--min-confidence", type=float, default=0.12)
    parser.add_argument("--min-agreement", type=float, default=0.40)
    args = parser.parse_args(argv)

    loaded, _books, notes = md.load_corpus(args.data_dir)
    symbol = args.symbol or sorted(loaded)[0]
    bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
            for b in loaded[symbol]]

    print("=" * 78)
    print("RESIDUAL DIAGNOSTIC — is the barrier's risk unit contaminated?")
    print("=" * 78)
    for note in notes:
        print(note)
    print(f"symbol            : {symbol}")
    print(f"bars              : {len(bars):,}")
    print()
    print("HYPOTHESIS: the analyser fires where backward ATR(14) OVERSTATES the")
    print("realised 24-bar range that follows. If so its nominal 2-ATR stop is")
    print("further away in real terms than the null's on the same nominal")
    print("geometry, which biases the instrument upward with no timing in it.")
    print()
    print(f"backward_atr_over_price = WilderATR({args.atr_period}) at bar i / close[i]")
    print(f"forward_realised_vol    = (max(high)-min(low)) over i+1..i+{args.horizon} "
          f"/ close[i]")
    print("ratio                   = backward / forward   (>1 => ATR overstates)")
    print()
    print("Measured on SURROGATES ONLY. On the real series a difference could be")
    print("skill, drift or clustering; on a structure-free series it can only be")
    print("mechanical, which is the whole point of asking it there.")
    print()

    cfg = bt.BacktestConfig(
        starting_cash=10_000.0, warmup_bars=200, interval=args.interval,
        min_confidence=args.min_confidence,
        min_component_agreement=args.min_agreement,
    )

    results: List[SurrogateResult] = []
    pooled_flagged: List[np.ndarray] = []
    pooled_unflagged: List[np.ndarray] = []
    for offset in range(args.surrogates):
        seed = args.seed + 1000 + offset
        outcome = analyse_surrogate(
            bars, cfg, seed=seed, warmup=cfg.warmup_bars, lockup=args.lockup,
            horizon=args.horizon, atr_period=args.atr_period,
            strata_bins=args.strata_bins,
        )
        if outcome is None:
            print(f"  surrogate {offset + 1}: no usable flagged/partner pair — skipped")
            continue
        result, flagged, unflagged = outcome
        results.append(result)
        pooled_flagged.append(flagged)
        pooled_unflagged.append(unflagged)
        print(f"  surrogate {offset + 1:2d}: "
              f"flagged n={result.n_flagged:3d} med={result.median_ratio_flagged:.4f}  |  "
              f"unflagged n={result.n_unflagged:4d} med={result.median_ratio_unflagged:.4f}  |  "
              f"gap {result.gap:+.4f}  p={result.p_value:.4f}  "
              f"cells {result.cells_used} used / {result.cells_dropped} dropped")

    if not results:
        print()
        print("INCONCLUSIVE (C): no surrogate produced a usable comparison.")
        return 2

    print()
    print("=" * 78)
    print("PER-SURROGATE SUMMARY")
    print("=" * 78)
    print(f"{'seed':>10} {'med flag':>9} {'med unfl':>9} {'gap':>9} "
          f"{'%>1 flag':>9} {'%>1 unfl':>9} {'p':>8}")
    for result in results:
        print(f"{result.seed:>10} {result.median_ratio_flagged:>9.4f} "
              f"{result.median_ratio_unflagged:>9.4f} {result.gap:>+9.4f} "
              f"{result.fraction_flagged_over_one:>9.1%} "
              f"{result.fraction_unflagged_over_one:>9.1%} {result.p_value:>8.4f}")

    print()
    print("=" * 78)
    print("RAW LEVELS (medians, not ratios)")
    print("=" * 78)
    atr_flag = float(np.median([r.median_atr_flagged for r in results]))
    atr_unfl = float(np.median([r.median_atr_unflagged for r in results]))
    fwd_flag = float(np.median([r.median_forward_flagged for r in results]))
    fwd_unfl = float(np.median([r.median_forward_unflagged for r in results]))
    print(f"backward ATR/price : flagged {atr_flag:.5f}   unflagged {atr_unfl:.5f}"
          f"   ratio {atr_flag / atr_unfl if atr_unfl else float('nan'):.4f}")
    print(f"forward range/price: flagged {fwd_flag:.5f}   unflagged {fwd_unfl:.5f}"
          f"   ratio {fwd_flag / fwd_unfl if fwd_unfl else float('nan'):.4f}")
    print()
    print("Those two lines are POOLED across cells, so they mix a within-cell")
    print("effect with a composition effect -- the flagged bars are not spread")
    print("over the cells the way the partner pool is, and the cells are")
    print("themselves ATR quantiles. The within-cell version is the honest one:")
    per_cell = np.array(
        [r for result in results for r in result.cell_atr_ratios], dtype=float)
    per_cell = per_cell[np.isfinite(per_cell)]
    if per_cell.size:
        below = int((per_cell < 1.0).sum())
        # Sign test against "the ratio is as likely above 1 as below".
        from math import comb
        n_cells = int(per_cell.size)
        tail = sum(comb(n_cells, k) for k in range(min(below, n_cells - below) + 1))
        sign_p = min(1.0, 2.0 * tail / (2.0 ** n_cells))
        print(f"  WITHIN-CELL ATR/price ratio (flagged median / same cell's "
              f"unflagged median)")
        print(f"    cells compared : {n_cells} across {len(results)} surrogates")
        print(f"    median ratio   : {np.median(per_cell):.4f}   "
              f"mean {per_cell.mean():.4f}")
        print(f"    cells below 1.0: {below} of {n_cells}  "
              f"(sign test two-sided p = {sign_p:.6f})")

    all_flagged = np.concatenate(pooled_flagged)
    all_unflagged = np.concatenate(pooled_unflagged)
    _u, pooled_p = mann_whitney_u(all_flagged, all_unflagged)
    pooled_gap = float(np.median(all_flagged) - np.median(all_unflagged))
    gaps = np.array([r.gap for r in results])
    majority = int((gaps > 0).sum())

    print()
    print("=" * 78)
    print("POOLED")
    print("=" * 78)
    print(f"flagged   : n={all_flagged.size:,}  median {np.median(all_flagged):.4f}  "
          f"mean {all_flagged.mean():.4f}")
    print(f"unflagged : n={all_unflagged.size:,}  median {np.median(all_unflagged):.4f}  "
          f"mean {all_unflagged.mean():.4f}")
    print(f"gap (median flagged - median unflagged) : {pooled_gap:+.4f}")
    print(f"Mann-Whitney two-sided p                : {pooled_p:.6f}")
    print(f"surrogates with a POSITIVE gap          : {majority} of {len(results)}")
    print(f"per-surrogate gap: median {np.median(gaps):+.4f}, "
          f"min {gaps.min():+.4f}, max {gaps.max():+.4f}")
    print(f"cells dropped (<{MIN_PARTNERS} partners)  : "
          f"{sum(r.cells_dropped for r in results)} across {len(results)} surrogates, "
          f"{sum(r.bars_dropped for r in results)} entry bars lost")

    # ------------------------------------------------------------------
    # The decision rule, fixed before the numbers were seen.
    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print("DECISION")
    print("=" * 78)
    confirmed = (majority > len(results) / 2.0) and (pooled_p < 0.05) and (pooled_gap > 0)
    if confirmed:
        print("A — CONFIRMED CONTAMINATION.")
        print("The analyser fires where backward ATR overstates the range that")
        print("follows. Its nominal 2-ATR stop is softer in real terms than the")
        print("null's on identical nominal geometry, which biases the instrument")
        print("upward with no timing skill involved.")
        return 0
    if np.isfinite(pooled_p) and pooled_p >= 0.05:
        print("B — CONTAMINATION RULED OUT.")
        print("The ratios are statistically indistinguishable between the bars the")
        print("analyser entered and same-cell bars it did not flag. The residual")
        print("bias in the skill test is NOT explained by a backward-ATR versus")
        print("forward-volatility mismatch.")
        return 1
    if pooled_gap < 0 and pooled_p < 0.05:
        print("B — CONTAMINATION RULED OUT (and the sign is against it).")
        print("The difference is significant but runs the WRONG way: the analyser")
        print("fires where backward ATR UNDERstates the range that follows, which")
        print("would bias the instrument downward, not upward. The hypothesis as")
        print("stated is refuted.")
        return 1
    print("C — INCONCLUSIVE.")
    print("The comparison did not produce a stable answer. Expand the surrogate")
    print("count or the partner pools using only quantities the barrier already")
    print("sees; do not change the decision rule.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
