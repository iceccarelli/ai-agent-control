"""post_shock_fade_v1 — fade an extreme single-day move in the same symbol.

THE THESIS, AS THE HUMAN WROTE IT
=================================
After an extreme single-day move in liquid crypto spot, short-horizon mean
reversion is offered by inventory-rebalancing market makers and by the
exhaustion of discretionary chase. A day that closes at least K x ATR from the
prior close leaves dealers off-side; the next few sessions often reclaim a
fraction of that move.

The candidate inefficiency is a **fade of the shock**, not a continuation.

The full specification is `NEW_SIGNAL_INTAKE.md`, human-filled and
authoritative. This module implements it and nothing else. No volume filter, no
regime condition, no second indicator, no exception for any date.

MATERIAL DIFFERENCE — WHY THIS IS ELIGIBLE AT ALL
=================================================
Four families are frozen ABSENT. This one is checked against each:

    technical_analysis      RSI/MACD/Bollinger/Supertrend/ADX committee
    donchian_breakout_v1    the symbol's own N-day high/low channel
    btc_alt_spillover_v1    BTC's return, traded on ETH/SOL
    this signal             the symbol's OWN return, scaled by its own Wilder
                            ATR(14), traded on ITSELF, in the OPPOSITE
                            direction to the move

No oscillator. No channel and no N to search. No cross-asset driver and no
calendar join. And the direction is the point: `btc_alt_spillover_v1` buys the
continuation of a shock, this sells it. The two would take opposite sides of
the same trade, which is what makes this a new question rather than a
reparameterisation of an answered one.

The honest caveat, from EDGE.md §24b: an ATR-scaled shock threshold with a
next-open fill is *mechanically* similar to spillover's trigger. What differs
is the information set and the direction.

WHAT THIS MODULE DOES NOT DO
============================
It computes **directed entry indices** and nothing else. Every R comes from
`skill_test.barrier_r_for_all_bars`, the one shared implementation the null
also uses. No barrier arithmetic is reproduced here — a forked copy that
drifted by a line would make this measurement incomparable with everything
already in EDGE.md, and `tests/test_post_shock_fade_v1.py` asserts at AST level
that none exists.

NO LOOKAHEAD, STRUCTURALLY
==========================
The trigger reads `close[t]`, `close[t-1]` and an ATR ending at `t`. The fill
is at `open[t+1]`. Nothing reads past `t` to decide, and nothing reads the entry
bar to size — `ATR_entry` is the ATR of the last *closed* bar at or before
entry, which is the signal bar. A test truncates the series and asserts the
surviving prefix of decisions is unchanged.

WHY THE SHOCK AND THE STOP SHARE ONE ATR IMPLEMENTATION
=======================================================
The threshold uses `ATR[t] / close[t]`; the barrier sizes `R_dist` from the ATR
at the same bar. If those two came from different implementations, the signal
could fire on one number and be sized by another, and nothing would ever say
so. Both come from `sweep_geometry.wilder_atr` — the vectorised production
implementation that `skill_test.barrier_r_for_all_bars` itself uses. It agrees
with `pure_indicators.atr` (the intake's other name for it) to float epsilon,
and `tests/test_post_shock_fade_v1.py` asserts that equivalence rather than
trusting it.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

#: Every constant here was frozen in `NEW_SIGNAL_INTAKE.md` and EDGE.md §24a
#: BEFORE any number existed. None of them is a tunable, and none is to be
#: grid-searched — not before a run, not after seeing one.
SHOCK_K = 2.0              # r vs SHOCK_K * (ATR / close)
ATR_PERIOD = 14            # Wilder
STOP_ATR = 1.5             # R_dist = 1.5 * ATR at the signal bar
TAKE_PROFIT_R = 1.0        # fixed 1:1 — a mean-reversion target, not a trend 2R
#: The shared barrier's payoff is ``take_profit_atr / stop_atr``, so a 1.0 R
#: target on a 1.5 ATR stop is 1.5 ATR of take-profit. Writing this as 1.0
#: would quietly change the STOP distance, which the intake fixes at 1.5 ATR.
TAKE_PROFIT_ATR = STOP_ATR * TAKE_PROFIT_R   # == 1.5
HORIZON = 3                # daily bars; time stop at the close of entry_bar + 3
LOCKUP = 1                 # one trade per run
ROUND_TRIP_BPS = 25.0
ENTRY_ON = "next_open"     # fill at the symbol's own open of the bar after

NAME = "post_shock_fade_v1"

LONG_SETUP = "LONG_SETUP"    # fade a DOWN shock
SHORT_SETUP = "SHORT_SETUP"  # fade an UP shock


def _atr(bars: Sequence, period: int = ATR_PERIOD) -> np.ndarray:
    """Wilder ATR via the production implementation. Never re-derived here."""
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


def shock_setups(bars: Sequence, *, k: float = SHOCK_K,
                 atr_period: int = ATR_PERIOD) -> Dict[int, str]:
    """``{bar_index: LONG_SETUP | SHORT_SETUP}`` for every shock bar.

    ``shock_up`` when ``r[t] >= +k * (ATR[t] / close[t])`` and ``shock_dn`` when
    ``r[t] <= -k * (...)``. The two are mutually exclusive for any positive
    threshold, but the specification writes "shock_up and not shock_dn"
    explicitly and that is honoured literally rather than optimised away — a
    threshold of zero would make them overlap, and a reader should be able to
    see the rule that was actually declared.

    **The direction is inverted relative to the move**, which is the entire
    hypothesis: an up-shock is a SHORT setup and a down-shock is a LONG one.
    """
    close = np.array([b.close for b in bars], dtype=float)
    atr = _atr(bars, atr_period)
    setups: Dict[int, str] = {}
    for t in range(1, close.size):
        if not np.isfinite(atr[t]) or atr[t] <= 0.0 or close[t] <= 0.0:
            continue
        if close[t - 1] <= 0.0:
            continue
        r = (close[t] - close[t - 1]) / close[t - 1]
        threshold = k * (atr[t] / close[t])
        shock_up = r >= +threshold
        shock_dn = r <= -threshold
        if shock_up and not shock_dn:
            setups[t] = SHORT_SETUP       # fade the up-move
        elif shock_dn and not shock_up:
            setups[t] = LONG_SETUP        # fade the down-move
    return setups


def directed_signal_bars(
    bars: Sequence, *, warmup: int = 0, k: float = SHOCK_K,
    atr_period: int = ATR_PERIOD,
) -> List[Tuple[int, str]]:
    """``[(signal_index, direction), ...]``, ordered.

    The returned index is the **signal** bar. The fill happens one bar later,
    which the shared barrier arithmetic handles via ``entry_on="next_open"``;
    this module never computes a price.

    The last bar carries no setup: there is no next bar to fill on. Bars before
    ``warmup`` are excluded so that the ATR is well established and so the
    measurement matches every prior directed run in this repository.
    """
    setups = shock_setups(bars, k=k, atr_period=atr_period)
    if not setups:
        return []
    last = len(bars) - 1
    return [(index, setups[index]) for index in sorted(setups)
            if warmup <= index < last]


def flags_and_directions(
    bars: Sequence, *, warmup: int = 0, k: float = SHOCK_K,
    atr_period: int = ATR_PERIOD,
) -> Tuple[np.ndarray, Dict[int, str]]:
    """The same information in the shapes the instrument already speaks.

    A boolean flag array — what ``simulate_schedule``, ``extract_blocks`` and
    the rotation null consume unchanged — and an index -> direction map carried
    alongside. The map never decides *whether* a bar is a candidate, only which
    way a trade taken there points.
    """
    directed = directed_signal_bars(bars, warmup=warmup, k=k,
                                    atr_period=atr_period)
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
