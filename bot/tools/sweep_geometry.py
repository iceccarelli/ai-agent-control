#!/usr/bin/env python3
"""Sweep the exit geometry and report expectancy in R, after costs.

WHY THIS IS THE FIRST THING SLICE 9 DOES
========================================
Slice 8 measured, on seven years of real BTC data:

    take-profit (+2 ATR) : 30.7%      break-even at 2:1 : 33.3%
    stop        (-1 ATR) : 65.2%      realised win rate : 32.1%

The strategy's realised hit rate was almost exactly the *unconditional* rate,
which says the entry signal was contributing close to nothing and the payoff
geometry was losing money on its own. One fold traded 192 times at a **54.2%**
win rate and was still negative.

So before another feature, another model, or another threshold: does **any**
(take-profit, stop, horizon) combination have positive expectancy on this data,
after realistic costs? That is a question about arithmetic and the historical
price path, and it is answerable in one sweep. It costs an afternoon and it
governs everything downstream, because a model trained under a losing geometry
is learning to forecast the outcome of a losing game more precisely.

WHAT IS MEASURED
----------------
For every bar, with barriers at ``+k*ATR`` / ``-m*ATR`` and a horizon of ``H``
bars, the realised **R-multiple** — profit and loss in units of the risk
actually taken:

    R unit    = m * ATR                       (the distance to the stop)
    take-profit hit -> +k/m R
    stop hit        -> -1 R
    horizon expiry  -> (exit - entry) * direction / (m * ATR)   R

A timeout is **not** zero. Resolving it as the realised move is the whole
reason drift can show up in this table at all; scoring it zero would hide the
one effect a long-only strategy on an appreciating asset actually has.

Costs are converted into R, which is where the mechanical part of slice 8's
result lives:

    cost_R = round_trip_cost / (m * ATR / entry)

A tighter stop is a *smaller* R unit, so the same 25 bps round trip eats a
larger share of it. At m=0.5 on hourly BTC the round trip can be a third of the
risk unit; at m=3 it is a twentieth. That is not a subtlety, it is most of the
answer.

WHAT THIS TOOL CANNOT DO
------------------------
It cannot tell you a geometry is profitable. It measures the *unconditional*
distribution — every bar, no signal — plus a small number of simple filters. A
positive number here is a necessary condition for a strategy on this geometry,
not a sufficient one, and a strategy still has to beat the same costs *after*
its own selection.

It is also the honest place to state the theory the numbers will confirm: on a
driftless random walk, P(hit +k before -m) is m/(k+m), so expectancy in R is
(m/(k+m))*(k/m) - (k/(k+m))*1 = 0 for **every** k and m. Geometry alone cannot
manufacture edge. What it can do is change how much of the edge costs consume,
and whether drift is captured — and both of those are worth measuring.

Usage
-----
    python3 tools/sweep_geometry.py                      # the default grid
    python3 tools/sweep_geometry.py --quick              # a coarse grid, fast
    python3 tools/sweep_geometry.py --filter trend_up
    python3 tools/sweep_geometry.py --direction -1       # shorts
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import features as _features
import market_data as md

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

#: Bybit spot taker is 10 bps a side; 5 bps of slippage is the shipped default.
#: Expressed here rather than read from config so a sweep is reproducible from
#: its command line alone.
DEFAULT_ROUND_TRIP_BPS = 25.0


# ---------------------------------------------------------------------------
# the vectorised barrier scan
# ---------------------------------------------------------------------------


def wilder_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray,
               period: int) -> np.ndarray:
    """Wilder ATR, returned aligned to ``close`` with NaN before it is defined.

    Reimplemented here in vectorised form because the sweep evaluates ~10^7
    bar-geometry pairs and calling the scalar path that many times would take
    hours. ``--verify`` cross-checks it against ``features.triple_barrier_label``
    on a random sample, which is the only reason a second implementation is
    acceptable at all: two implementations that are never compared are just two
    chances to be wrong.
    """
    n = close.size
    atr = np.full(n, np.nan)
    if n < period + 1:
        return atr
    previous = close[:-1]
    true_range = np.maximum(
        high[1:] - low[1:],
        np.maximum(np.abs(high[1:] - previous), np.abs(low[1:] - previous)),
    )
    seed = true_range[: period].mean()
    atr[period] = seed
    alpha = 1.0 / period
    running = seed
    for i in range(period + 1, n):
        running = running + alpha * (true_range[i - 1] - running)
        atr[i] = running
    return atr


@dataclass(frozen=True)
class SweepResult:
    """One (k, m, H) cell. Every rate is a fraction; every R is an R."""

    take_profit_atr: float
    stop_atr: float
    horizon: int
    direction: int
    n: int
    hit_rate: float
    stop_rate: float
    timeout_rate: float
    expectancy_r_gross: float
    expectancy_r_net: float
    cost_r: float
    median_bars: float
    break_even_hit_rate: float
    r_std: float

    @property
    def t_stat(self) -> Optional[float]:
        """Expectancy over its own standard error.

        Reported because an expectancy of +0.01R over 60,000 overlapping
        samples is not the same claim as +0.01R over 60 independent ones, and
        the sign alone invites reading it as the former. Overlapping windows
        make even this optimistic — see the caveat printed with the table.
        """
        if self.n < 2 or self.r_std <= 0.0:
            return None
        return self.expectancy_r_net / (self.r_std / np.sqrt(self.n))


def scan(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, atr: np.ndarray,
    *, take_profit_atr: float, stop_atr: float, horizon: int, direction: int,
    eligible: np.ndarray, round_trip_bps: float,
) -> Optional[SweepResult]:
    """Resolve every eligible bar's barriers and summarise the R distribution.

    THE PESSIMISTIC RULE, PRESERVED
    -------------------------------
    Within a bar the stop is checked before the take-profit. A candle reports
    its high and its low, not the order they happened in; when the range spans
    both barriers either answer fits the data and only one of them is safe.
    ``features.triple_barrier_label`` and ``backtest`` make the same choice, so
    a geometry that looks good here will not evaporate in the simulator.
    """
    n = close.size
    entry_ok = eligible & np.isfinite(atr) & (atr > 0.0)
    # A bar cannot be labelled if the series ends before its horizon does.
    entry_ok[max(0, n - horizon - 1):] = False
    indices = np.nonzero(entry_ok)[0]
    if indices.size == 0:
        return None

    entry = close[indices]
    risk = stop_atr * atr[indices]
    reward = take_profit_atr * atr[indices]
    if direction > 0:
        tp_level = entry + reward
        sl_level = entry - risk
    else:
        tp_level = entry - reward
        sl_level = entry + risk

    realised = np.full(indices.size, np.nan)
    bars_used = np.zeros(indices.size, dtype=np.int32)
    outcome = np.zeros(indices.size, dtype=np.int8)   # 1 tp, -1 stop, 0 timeout
    open_mask = np.ones(indices.size, dtype=bool)

    payoff = take_profit_atr / stop_atr
    for step in range(1, horizon + 1):
        forward = indices + step
        bar_high = high[forward]
        bar_low = low[forward]
        if direction > 0:
            hit_stop = bar_low <= sl_level
            hit_tp = bar_high >= tp_level
        else:
            hit_stop = bar_high >= sl_level
            hit_tp = bar_low <= tp_level

        # Stop first, always.
        stopped = open_mask & hit_stop
        realised[stopped] = -1.0
        outcome[stopped] = -1
        bars_used[stopped] = step
        open_mask &= ~stopped

        took_profit = open_mask & hit_tp
        realised[took_profit] = payoff
        outcome[took_profit] = 1
        bars_used[took_profit] = step
        open_mask &= ~took_profit

        if not open_mask.any():
            break

    if open_mask.any():
        # Timeout: the realised move, not zero. This is where drift enters.
        exit_index = indices[open_mask] + horizon
        move = (close[exit_index] - entry[open_mask]) * direction
        realised[open_mask] = move / risk[open_mask]
        bars_used[open_mask] = horizon
        outcome[open_mask] = 0

    cost_r = float(np.mean((round_trip_bps / 10_000.0) * entry / risk))
    net = realised - (round_trip_bps / 10_000.0) * entry / risk

    total = float(indices.size)
    return SweepResult(
        take_profit_atr=take_profit_atr,
        stop_atr=stop_atr,
        horizon=horizon,
        direction=direction,
        n=int(total),
        hit_rate=float((outcome == 1).sum() / total),
        stop_rate=float((outcome == -1).sum() / total),
        timeout_rate=float((outcome == 0).sum() / total),
        expectancy_r_gross=float(np.mean(realised)),
        expectancy_r_net=float(np.mean(net)),
        cost_r=cost_r,
        median_bars=float(np.median(bars_used)),
        break_even_hit_rate=1.0 / (1.0 + payoff),
        r_std=float(np.std(net, ddof=1)) if total > 1 else 0.0,
    )


# ---------------------------------------------------------------------------
# entry filters
# ---------------------------------------------------------------------------


def build_filters(close: np.ndarray, high: np.ndarray, low: np.ndarray,
                  atr: np.ndarray) -> Dict[str, np.ndarray]:
    """Simple, honest conditioning masks.

    Deliberately crude. The point of the sweep is the geometry, and a
    sophisticated filter here would confound the two questions — a positive
    cell would leave you unable to say whether the geometry or the filter did
    it. These are the cheapest possible conditions, each one a single
    well-known statement about the bar.
    """
    n = close.size
    everything = np.ones(n, dtype=bool)

    def sma(period: int) -> np.ndarray:
        out = np.full(n, np.nan)
        if n >= period:
            cumulative = np.cumsum(np.insert(close, 0, 0.0))
            out[period - 1:] = (cumulative[period:] - cumulative[:-period]) / period
        return out

    sma50, sma200 = sma(50), sma(200)
    with np.errstate(invalid="ignore"):
        trend_up = np.nan_to_num(close > sma200, nan=False).astype(bool)
        trend_dn = np.nan_to_num(close < sma200, nan=False).astype(bool)
        golden = np.nan_to_num(sma50 > sma200, nan=False).astype(bool)
        vol = atr / np.where(close > 0, close, np.nan)
    finite_vol = vol[np.isfinite(vol)]
    quiet = np.zeros(n, dtype=bool)
    loud = np.zeros(n, dtype=bool)
    if finite_vol.size:
        low_cut, high_cut = np.percentile(finite_vol, [33.0, 67.0])
        quiet = np.nan_to_num(vol <= low_cut, nan=False).astype(bool)
        loud = np.nan_to_num(vol >= high_cut, nan=False).astype(bool)

    return {
        "none": everything,
        "trend_up": trend_up,
        "trend_down": trend_dn,
        "golden_cross": golden,
        "low_vol": quiet,
        "high_vol": loud,
        "trend_up_low_vol": trend_up & quiet,
    }


# ---------------------------------------------------------------------------
# verification against the canonical labeller
# ---------------------------------------------------------------------------


def verify_against_features(bars, high, low, close, atr, *, samples: int = 200,
                            seed: int = 11) -> Tuple[int, int, List[str]]:
    """Cross-check the vectorised scan against ``features.triple_barrier_label``.

    Two implementations that are never compared are two chances to be wrong.
    This is the comparison. Any mismatch is reported and the sweep refuses to
    print a table, because a fast wrong answer is worse than a slow right one.
    """
    spec = _features.BarrierSpec(take_profit_atr=2.0, stop_atr=1.0,
                                 max_horizon=24, direction=1)
    rng = np.random.default_rng(seed)
    n = close.size
    candidates = np.nonzero(np.isfinite(atr) & (atr > 0.0))[0]
    candidates = candidates[(candidates > 300) & (candidates < n - 30)]
    if candidates.size == 0:
        return 0, 0, ["no eligible bars to verify"]
    picks = rng.choice(candidates, size=min(samples, candidates.size), replace=False)

    problems: List[str] = []
    checked = agreed = 0
    for index in sorted(int(i) for i in picks):
        label = _features.triple_barrier_label(bars, index, spec)
        if not label.resolved or label.atr_at_entry is None:
            continue
        cell = scan(
            high, low, close, atr,
            take_profit_atr=2.0, stop_atr=1.0, horizon=24, direction=1,
            eligible=_only(index, n), round_trip_bps=0.0,
        )
        if cell is None:
            continue
        checked += 1
        mine = 1 if cell.hit_rate > 0.5 else (-1 if cell.stop_rate > 0.5 else 0)
        # Compare on `barrier`, which is an unambiguous string, rather than on
        # `outcome`, which is an integer whose encoding this module would have
        # to duplicate. Duplicating an encoding to check an implementation is
        # how a verification ends up testing the copy of the assumption it was
        # supposed to catch — which is exactly what happened on the first run
        # of this function, and it reported four false mismatches.
        theirs = {"take_profit": 1, "stop": -1, "timeout": 0}.get(label.barrier)
        if theirs is None:
            continue
        if mine == theirs:
            agreed += 1
        else:
            problems.append(
                f"bar {index}: vectorised={mine} canonical={theirs} "
                f"(barrier={label.barrier})"
            )
    return checked, agreed, problems[:10]


def _only(index: int, n: int) -> np.ndarray:
    mask = np.zeros(n, dtype=bool)
    mask[index] = True
    return mask


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=os.path.join(REPO, "data", "real"))
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--direction", type=int, default=1, choices=(1, -1))
    parser.add_argument("--round-trip-bps", type=float,
                        default=DEFAULT_ROUND_TRIP_BPS)
    parser.add_argument("--atr-period", type=int, default=14)
    parser.add_argument("--filter", default=None,
                        help="evaluate only this entry filter")
    parser.add_argument("--quick", action="store_true", help="a coarse grid")
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--no-verify", action="store_true")
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args(argv)

    bars_by_symbol, _books, notes = md.load_corpus(args.data_dir)
    symbol = args.symbol or sorted(bars_by_symbol)[0]
    bars = bars_by_symbol[symbol]

    high = np.array([b.high for b in bars], dtype=float)
    low = np.array([b.low for b in bars], dtype=float)
    close = np.array([b.close for b in bars], dtype=float)
    atr = wilder_atr(high, low, close, args.atr_period)

    print("=" * 78)
    print("DATA PROVENANCE")
    print("=" * 78)
    for note in notes:
        print(note)
    print(f"symbol            : {symbol}")
    print(f"bars              : {len(bars):,}")
    print(f"direction         : {'LONG' if args.direction > 0 else 'SHORT'}")
    print(f"round trip        : {args.round_trip_bps:.1f} bps")

    if not args.no_verify:
        print()
        print("verifying the vectorised scan against features.triple_barrier_label …")
        checked, agreed, problems = verify_against_features(
            bars, high, low, close, atr
        )
        print(f"  {agreed}/{checked} sampled bars agree")
        if problems:
            print("  MISMATCHES — refusing to print a table built on a scan "
                  "that disagrees with the canonical labeller:")
            for problem in problems:
                print(f"    {problem}")
            return 2

    filters = build_filters(close, high, low, atr)
    if args.filter:
        if args.filter not in filters:
            raise SystemExit(f"unknown filter {args.filter!r}; "
                             f"choose from {sorted(filters)}")
        filters = {args.filter: filters[args.filter]}

    if args.quick:
        tps = [1.0, 2.0, 3.0]
        stops = [1.0, 2.0]
        horizons = [24, 96]
    else:
        tps = [0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0]
        stops = [0.5, 1.0, 1.5, 2.0, 3.0, 4.0]
        horizons = [6, 12, 24, 48, 96, 168, 336]

    results: List[Tuple[str, SweepResult]] = []
    for name, mask in filters.items():
        for k in tps:
            for m in stops:
                for h in horizons:
                    cell = scan(
                        high, low, close, atr,
                        take_profit_atr=k, stop_atr=m, horizon=h,
                        direction=args.direction, eligible=mask,
                        round_trip_bps=args.round_trip_bps,
                    )
                    if cell is not None and cell.n >= 200:
                        results.append((name, cell))

    print()
    print("=" * 78)
    print(f"GEOMETRY SWEEP — {len(results):,} cells")
    print("=" * 78)
    print("Expectancy is in R, where 1R is the distance to the stop. 'net' is")
    print("after a round trip. A cell needs >= 200 samples to be shown.")
    print()
    print("NOTE ON SIGNIFICANCE: samples overlap heavily — consecutive bars share")
    print("almost all of their outcome window — so the effective sample size is a")
    print("small fraction of n and the t-statistic is optimistic by roughly the")
    print("square root of the overlap. Treat it as a screen, never as a p-value.")
    print()

    results.sort(key=lambda pair: pair[1].expectancy_r_net, reverse=True)
    header = (f"{'filter':<17}{'TP':>5}{'SL':>5}{'H':>5}{'n':>9}{'hit%':>7}"
              f"{'brk%':>7}{'costR':>7}{'grossR':>9}{'netR':>9}{'t':>7}")
    print(header)
    print("-" * len(header))
    for name, cell in results[: args.top]:
        t = cell.t_stat
        print(f"{name:<17}{cell.take_profit_atr:>5.2f}{cell.stop_atr:>5.2f}"
              f"{cell.horizon:>5d}{cell.n:>9,}{cell.hit_rate * 100:>7.1f}"
              f"{cell.break_even_hit_rate * 100:>7.1f}{cell.cost_r:>7.3f}"
              f"{cell.expectancy_r_gross:>+9.4f}{cell.expectancy_r_net:>+9.4f}"
              f"{(f'{t:.1f}' if t is not None else 'n/a'):>7}")

    print()
    print("WORST CELLS (the same screen, inverted)")
    print("-" * len(header))
    for name, cell in results[-5:]:
        print(f"{name:<17}{cell.take_profit_atr:>5.2f}{cell.stop_atr:>5.2f}"
              f"{cell.horizon:>5d}{cell.n:>9,}{cell.hit_rate * 100:>7.1f}"
              f"{cell.break_even_hit_rate * 100:>7.1f}{cell.cost_r:>7.3f}"
              f"{cell.expectancy_r_gross:>+9.4f}{cell.expectancy_r_net:>+9.4f}")

    positive = [c for _, c in results if c.expectancy_r_net > 0]
    print()
    print("=" * 78)
    print("READING")
    print("=" * 78)
    print(f"cells with positive NET expectancy : {len(positive):,} / {len(results):,}")
    if results:
        gross_positive = [c for _, c in results if c.expectancy_r_gross > 0]
        print(f"cells with positive GROSS expectancy: {len(gross_positive):,}")
        print()
        print("On a driftless random walk every cell would be 0.0000 gross, because")
        print("P(hit +k before -m) = m/(k+m) makes the payoff exactly fair. Gross")
        print("expectancy away from zero is drift or serial dependence; the gap")
        print("between gross and net is what the round trip costs, and it shrinks")
        print("as the stop widens because a wider stop is a bigger R.")

    if args.json_out:
        payload = [
            {"filter": name, **{f: getattr(cell, f) for f in (
                "take_profit_atr", "stop_atr", "horizon", "direction", "n",
                "hit_rate", "stop_rate", "timeout_rate", "expectancy_r_gross",
                "expectancy_r_net", "cost_r", "median_bars",
                "break_even_hit_rate")}}
            for name, cell in results
        ]
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1)
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
