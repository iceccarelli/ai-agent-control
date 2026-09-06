"""range_location_fade_v1 — fade a close sitting at the extreme of its own range.

THE THESIS, AS THE HUMAN WROTE IT
=================================
A close that finishes in the extreme of its own recent high-low range is often
an over-extension of short-horizon discretionary flow and inventory. Market
makers and mean-reversion liquidity tend to push price back toward the interior
of that range over the next few sessions.

The candidate inefficiency is a **fade of range-extremity location** — not a
breakout of the range, not an ATR-shock fade, not a cross-asset lag, not an
oscillator stack. The information set is only the symbol's own close location
inside a fixed trailing range.

The full specification is `NEW_SIGNAL_INTAKE.md`, human-filled and
authoritative. This module implements it and nothing else: no volume filter, no
regime condition, no second indicator, no exception for any date.

MATERIAL DIFFERENCE — WHY THIS IS ELIGIBLE AT ALL
=================================================
Five names are frozen ABSENT. Checked against each:

    technical_analysis      RSI/MACD/Bollinger/Supertrend/ADX committee
    donchian_breakout_v1    a BREAK of the N-day high/low
    btc_alt_spillover_v1    BTC's return, traded on ETH/SOL
    post_shock_fade_v1      the symbol's own RETURN scaled by its own ATR
    this signal             WHERE the close sits inside a trailing 20-bar range

The nearest neighbour is `donchian_breakout_v1`, and the distinction is exact:
Donchian needs the range to be **broken** and buys the continuation. This one
**never requires a break** — it fires on a close still inside the range, at an
extreme percentile of it, and sells the approach.

The second-nearest is `post_shock_fade_v1`: also a same-asset fade, also filling
at the next open. The state variable differs — *location* rather than
*magnitude*. The sharpest case, from EDGE.md §27b: a series grinding up 0.5% a
day for a week ends at the top of its 20-day range with no ATR shock at all.
This fires; that does not.

BAR t IS EXCLUDED FROM ITS OWN RANGE
====================================
`range_high` and `range_low` are taken over `t-20 .. t-1`. Including bar t would
let a bar set the level it is then measured against: a new high would define
`range_high`, `loc` would be pinned at or near 1.0 by construction, and the
signal would be firing on an arithmetic identity rather than on a market state.
The intake says "excluding bar t (no same-bar lookahead)" and it is honoured
literally. `tests/test_range_location_fade_v1.py` asserts it from both sides.

WHAT THIS MODULE DOES NOT DO
============================
It computes **directed entry indices** and nothing else. Every R comes from
`skill_test.barrier_r_for_all_bars`, the one shared implementation the null also
uses. No barrier arithmetic is reproduced here — a forked copy that drifted by a
line would make this measurement incomparable with everything already in
EDGE.md, and the tests assert at AST level that none exists.

NO LOOKAHEAD, STRUCTURALLY
==========================
The trigger reads `close[t]` and the highs and lows of the twenty bars strictly
before it. The fill is at `open[t+1]`, handled by the shared barrier via
`entry_on="next_open"`; this module never computes a price. A test truncates the
series and asserts every surviving decision is unchanged.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

#: Every constant here was frozen in `NEW_SIGNAL_INTAKE.md` and EDGE.md §27a
#: BEFORE any number existed. None is a tunable, and none is to be
#: grid-searched — not before a run, not after seeing one.
RANGE_BARS = 20            # trailing window, t-20 .. t-1, excluding bar t
UPPER = 0.90               # loc >= UPPER -> fade an elevated close (SHORT)
LOWER = 0.10               # loc <= LOWER -> fade a depressed close (LONG)
ATR_PERIOD = 14            # Wilder, for the stop distance only
STOP_ATR = 1.5             # R_dist = 1.5 * ATR at the signal bar
TAKE_PROFIT_R = 1.0        # fixed 1:1 — a mean-reversion target
#: The shared barrier's payoff is ``take_profit_atr / stop_atr``, so a 1.0 R
#: target on a 1.5 ATR stop is 1.5 ATR of take-profit. Writing 1.0 here would
#: quietly change the STOP distance, which the intake fixes at 1.5 ATR.
TAKE_PROFIT_ATR = STOP_ATR * TAKE_PROFIT_R   # == 1.5
HORIZON = 5                # daily bars; time stop at the close of entry_bar + 5
LOCKUP = 1                 # one trade per run
ROUND_TRIP_BPS = 25.0
ENTRY_ON = "next_open"     # fill at this symbol's open of the bar after

NAME = "range_location_fade_v1"

LONG_SETUP = "LONG_SETUP"    # fade a close at the BOTTOM of its range
SHORT_SETUP = "SHORT_SETUP"  # fade a close at the TOP of its range


def _atr(bars: Sequence, period: int = ATR_PERIOD) -> np.ndarray:
    """Wilder ATR via the production implementation. Never re-derived here.

    Used only to report alongside the setups; the stop distance itself is
    computed by ``skill_test.barrier_r_for_all_bars`` from the same function,
    so the signal and the barrier cannot disagree about volatility.
    """
    import os
    import sys
    _tools = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "tools")
    if _tools not in sys.path:
        sys.path.insert(0, _tools)
    import sweep_geometry as sweep
    high = np.array([b.high for b in bars], dtype=float)
    low = np.array([b.low for b in bars], dtype=float)
    close = np.array([b.close for b in bars], dtype=float)
    return sweep.wilder_atr(high, low, close, period)


def range_location(bars: Sequence, *, window: int = RANGE_BARS) -> np.ndarray:
    """``loc[t]`` for every bar, ``nan`` where it is undefined.

    ``loc[t] = (close[t] - range_low[t]) / width[t]`` where the range is taken
    over ``t-window .. t-1`` — **bar t excluded**. Values outside [0, 1] are
    legitimate and are kept: a close that gapped beyond the prior range is
    exactly the extreme this signal is about, and clipping it would erase the
    distinction between "at the edge" and "through it".

    ``nan`` where there are not ``window`` prior bars, and where
    ``width <= 0`` — a zero-width range has no interior to be at the edge of,
    and the division would be meaningless rather than merely large.
    """
    high = np.array([b.high for b in bars], dtype=float)
    low = np.array([b.low for b in bars], dtype=float)
    close = np.array([b.close for b in bars], dtype=float)

    n = close.size
    loc = np.full(n, np.nan)
    if n <= window:
        return loc
    for t in range(window, n):
        range_high = float(high[t - window:t].max())
        range_low = float(low[t - window:t].min())
        width = range_high - range_low
        if not np.isfinite(width) or width <= 0.0:
            continue
        loc[t] = (close[t] - range_low) / width
    return loc


def location_setups(bars: Sequence, *, window: int = RANGE_BARS,
                    upper: float = UPPER,
                    lower: float = LOWER) -> Dict[int, str]:
    """``{bar_index: LONG_SETUP | SHORT_SETUP}`` for every extreme close.

    ``loc >= upper`` is a **SHORT** and ``loc <= lower`` is a **LONG**: the
    direction is inverted relative to where the close sits, which is the entire
    hypothesis.

    The specification writes the pathological both-at-once case explicitly and
    it is honoured literally rather than optimised away. With ``upper > lower``
    the two cannot overlap, but a reader should be able to see the rule that
    was actually declared, and a future edit that inverted the thresholds would
    then produce *no* setups rather than silently producing both.
    """
    loc = range_location(bars, window=window)
    setups: Dict[int, str] = {}
    for t in range(loc.size):
        value = loc[t]
        if not np.isfinite(value):
            continue
        high_extreme = value >= upper
        low_extreme = value <= lower
        if high_extreme and low_extreme:
            continue                      # pathological; declared as no setup
        if high_extreme:
            setups[t] = SHORT_SETUP       # fade the elevated close
        elif low_extreme:
            setups[t] = LONG_SETUP        # fade the depressed close
    return setups


def directed_signal_bars(
    bars: Sequence, *, warmup: int = 0, window: int = RANGE_BARS,
    upper: float = UPPER, lower: float = LOWER,
) -> List[Tuple[int, str]]:
    """``[(signal_index, direction), ...]``, ordered.

    The index is the **signal** bar. The fill happens one bar later, which the
    shared barrier handles via ``entry_on="next_open"``; this module never
    computes a price.

    The last bar carries no setup — there is no next bar to fill on. Bars before
    ``warmup`` are excluded, matching every prior directed measurement here.
    """
    setups = location_setups(bars, window=window, upper=upper, lower=lower)
    if not setups:
        return []
    last = len(bars) - 1
    return [(index, setups[index]) for index in sorted(setups)
            if warmup <= index < last]


def flags_and_directions(
    bars: Sequence, *, warmup: int = 0, window: int = RANGE_BARS,
    upper: float = UPPER, lower: float = LOWER,
) -> Tuple[np.ndarray, Dict[int, str]]:
    """The same information in the shapes the instrument already speaks.

    A boolean flag array — what ``simulate_schedule``, ``extract_blocks`` and
    the rotation null consume unchanged — and an index -> direction map carried
    alongside. The map never decides *whether* a bar is a candidate, only which
    way a trade taken there points.
    """
    directed = directed_signal_bars(bars, warmup=warmup, window=window,
                                    upper=upper, lower=lower)
    flags = np.zeros(len(bars), dtype=bool)
    directions: Dict[int, str] = {}
    for index, direction in directed:
        flags[index] = True
        directions[index] = direction
    return flags, directions


def summary(bars: Sequence, *, warmup: int = 0) -> Dict[str, Any]:
    """Counts a reader can check against the log. Never used to filter."""
    _flags, directions = flags_and_directions(bars, warmup=warmup)
    longs = sum(1 for d in directions.values() if d == LONG_SETUP)
    return {
        "bars": len(bars),
        "setups": len(directions),
        "long_setups": longs,
        "short_setups": len(directions) - longs,
    }
