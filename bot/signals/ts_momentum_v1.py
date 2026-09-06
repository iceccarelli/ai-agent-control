"""ts_momentum_v1 — trade the sign of a fixed-window cumulative return.

THE THESIS, AS THE HUMAN WROTE IT
=================================
Intermediate-horizon returns may be partially persistent. The sign of a
cumulative return over a frozen window — with a one-bar skip, so the decision
bar's own microstructure is not in the window — is a candidate predictor of the
next few sessions' direction after costs.

This is a **continuation** trade, not a fade. The information set is the
symbol's own closes only:

    ret[t] = close[t - SKIP] / close[t - SKIP - LOOKBACK] - 1

The full specification is `NEW_SIGNAL_INTAKE.md`, human-filled and
authoritative. This module implements it and nothing else: no volume filter, no
regime condition, no second indicator, no threshold on the magnitude, no
exception for any date. The design note fixed before this file existed is
EDGE.md §31.

MATERIAL DIFFERENCE — WHY THIS IS ELIGIBLE AT ALL
=================================================
Seven names are frozen. Checked against each:

    technical_analysis      RSI/MACD/Bollinger/Supertrend/ADX committee
    closed_analyser         the same committee under its other name
    donchian_breakout_v1    a BREAK of the N-day high/low, traded as continuation
    btc_alt_spillover_v1    BTC's return, traded on ETH/SOL
    post_shock_fade_v1      own return MAGNITUDE in ATR units, traded as a FADE
    range_location_fade_v1  WHERE the close sits inside a trailing range
    open_gap_fade_v1        the OVERNIGHT move, open[t] vs close[t-1]
    this signal             the SIGN of a 10-bar close-to-close return, skipping
                            the most recent bar, traded as CONTINUATION

The nearest neighbour is `donchian_breakout_v1`: also continuation, also the
symbol's own price. The distinction is exact — Donchian requires a **new
extreme** and buys the break of it. This never asks whether any level was
exceeded. A series drifting up 0.3% a day inside its channel fires here and
never fires there; a series that spikes to a new 20-day high on one bar after
ten flat ones fires there and (with a near-zero windowed return) may not fire
here at all. `tests/test_ts_momentum_v1.py` builds both and asserts the
disagreement in both directions.

The second-nearest is `post_shock_fade_v1`, and two independent differences
separate them: **sign, not magnitude**, and **continuation, not fade**. The same
3-ATR down day is a LONG there and a SHORT here.

THE WINDOW, WRITTEN OUT
=======================
With `LOOKBACK = 10` and `SKIP = 1` the window is `close[t-11] .. close[t-1]`:
eleven closes spanning ten returns, ending one bar before the decision bar. Bar
`t`'s own close is **not** in the window. The endpoints are what the ratio uses;
the interior bars are what makes it a ten-bar return rather than a one-bar one.

`ret[t] == 0` is a real branch and produces **no setup**. On a continuous-auction
venue exact float equality between two closes would be a curiosity; on these
corpora it is not, and it is tested rather than assumed away.

NO LOOKAHEAD, STRUCTURALLY
==========================
Every close the trigger reads is strictly before bar `t`. The fill is at
`open[t+1]`, handled by the shared barrier via `entry_on="next_open"`; this
module never computes a price. A test truncates the series and asserts every
surviving decision — and every surviving `ret` value, bit for bit — is unchanged.

WHAT THIS MODULE DOES NOT DO
============================
It computes **directed signal indices** and nothing else. Every R comes from
`skill_test.barrier_r_for_all_bars`, the one shared implementation the null also
uses. No barrier arithmetic is reproduced here — a forked copy that drifted by a
line would make this measurement incomparable with everything already in
EDGE.md, and the tests assert at AST level that none exists.
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

#: Every constant here was frozen in `NEW_SIGNAL_INTAKE.md` and EDGE.md §31b
#: BEFORE any number existed. None is a tunable, and none is to be
#: grid-searched — not before a run, not after seeing one.
LOOKBACK = 10              # bars of return in the window
SKIP = 1                   # most recent closed bar excluded from the window
ATR_PERIOD = 14            # Wilder, for the stop distance only
STOP_ATR = 1.5             # R_dist = 1.5 * ATR at the signal bar
TAKE_PROFIT_R = 2.0        # a momentum target, unlike the three fades' 1.0
#: The shared barrier's payoff is ``take_profit_atr / stop_atr``, so a 2.0 R
#: target on a 1.5 ATR stop is 3.0 ATR of take-profit. Writing 2.0 here would
#: give a payoff of 1.33 and quietly change the intake's target.
TAKE_PROFIT_ATR = STOP_ATR * TAKE_PROFIT_R   # == 3.0
HORIZON = 5                # daily bars; time stop at the close of entry_bar + 5
LOCKUP = 1                 # one trade per run
ROUND_TRIP_BPS = 25.0
ENTRY_ON = "next_open"     # fill at this symbol's open of the bar after

NAME = "ts_momentum_v1"

LONG_SETUP = "LONG_SETUP"    # positive windowed return -> stay long
SHORT_SETUP = "SHORT_SETUP"  # negative windowed return -> stay short

#: The first bar at which the window is fully defined. Named rather than
#: inlined because it appears in three places and an off-by-one here would
#: silently shorten the window rather than raise.
FIRST_DEFINED = SKIP + LOOKBACK


def window_bounds(t: int) -> Tuple[int, int]:
    """The two close indices `ret[t]` divides, as ``(start, end)``.

    ``end = t - SKIP`` and ``start = t - SKIP - LOOKBACK``. Exposed as a
    function so the tests can assert the intake's arithmetic against the
    implementation rather than against a copy of it, and so no call site has to
    re-derive it.
    """
    return t - SKIP - LOOKBACK, t - SKIP


def momentum_returns(bars: Sequence, *, lookback: int = LOOKBACK,
                     skip: int = SKIP) -> np.ndarray:
    """``ret[t]`` for every bar, ``nan`` where the window is undefined.

    ``ret[t] = close[t - skip] / close[t - skip - lookback] - 1``.

    ``nan`` where there are not enough prior bars, and where the denominator is
    not finite or not strictly positive. A non-positive close is not a price;
    dividing by it would produce a number with a sign that means nothing.
    """
    close = np.array([b.close for b in bars], dtype=float)
    n = close.size
    ret = np.full(n, np.nan)
    first = skip + lookback
    if n <= first:
        return ret
    start = close[:n - first]
    end = close[lookback:n - skip]
    valid = np.isfinite(start) & (start > 0.0) & np.isfinite(end)
    ratio = np.where(valid, end / np.where(valid, start, 1.0), np.nan)
    ret[first:] = ratio - 1.0
    return ret


def momentum_setups(bars: Sequence, *, lookback: int = LOOKBACK,
                    skip: int = SKIP) -> Dict[int, str]:
    """``{bar_index: LONG_SETUP | SHORT_SETUP}`` for every defined bar.

    The **sign** decides the direction and nothing else — there is no magnitude
    threshold, because the intake declares none and adding one would be a free
    parameter arriving after the fact.

    A positive windowed return is a LONG and a negative one is a SHORT: the
    direction follows the return rather than opposing it, which is what makes
    this a continuation family and not a fourth fade.

    ``ret == 0`` yields **no setup**. Written as an explicit branch rather than
    left to fall through, because on these corpora exact equality between two
    closes is not the impossibility it would be on a venue that closes.
    """
    ret = momentum_returns(bars, lookback=lookback, skip=skip)
    setups: Dict[int, str] = {}
    for t in range(ret.size):
        value = ret[t]
        if not np.isfinite(value) or value == 0.0:
            continue
        setups[t] = LONG_SETUP if value > 0.0 else SHORT_SETUP
    return setups


def directed_signal_bars(
    bars: Sequence, *, warmup: int = 0, lookback: int = LOOKBACK,
    skip: int = SKIP,
) -> List[Tuple[int, str]]:
    """``[(signal_index, direction), ...]``, ordered.

    The index is the **signal** bar. The fill happens one bar later, which the
    shared barrier handles via ``entry_on="next_open"``; this module never
    computes a price.

    The last bar carries no setup — there is no next bar to fill on. Bars before
    ``warmup`` are excluded, matching every prior directed measurement here.
    """
    setups = momentum_setups(bars, lookback=lookback, skip=skip)
    if not setups:
        return []
    last = len(bars) - 1
    return [(index, setups[index]) for index in sorted(setups)
            if warmup <= index < last]


def flags_and_directions(
    bars: Sequence, *, warmup: int = 0, lookback: int = LOOKBACK,
    skip: int = SKIP,
) -> Tuple[np.ndarray, Dict[int, str]]:
    """The same information in the shapes the instrument already speaks.

    A boolean flag array — what ``simulate_schedule``, ``extract_blocks`` and
    the rotation null consume unchanged — and an index -> direction map carried
    alongside. The map never decides *whether* a bar is a candidate, only which
    way a trade taken there points.
    """
    directed = directed_signal_bars(bars, warmup=warmup, lookback=lookback,
                                    skip=skip)
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
