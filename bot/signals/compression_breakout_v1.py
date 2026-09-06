"""compression_breakout_v1 — a range break that happens while volatility is low.

THE THESIS, AS THE HUMAN WROTE IT
=================================
A break of a prior range that occurs **while volatility is compressed** may
reflect a discrete shift in participation rather than noise inside an already
expanded range. The trigger is a **conjunction**, and neither half is a signal
on its own:

    COMPRESSED[t]  = percentile rank of ATR[t] within ATR[t-49..t]  <=  25
    HH[t]          = max(high[t-20] .. high[t-1])        bar t EXCLUDED
    LL[t]          = min(low[t-20]  .. low[t-1])

    LONG_SETUP   iff COMPRESSED[t] and close[t] > HH[t]
    SHORT_SETUP  iff COMPRESSED[t] and close[t] < LL[t]

The full specification is `NEW_SIGNAL_INTAKE.md`, human-filled and
authoritative. This module implements it and nothing else: no volume filter, no
trend condition, no second indicator, no exception for any date. The design note
fixed before this file existed is EDGE.md §34.

WHAT IS ACTUALLY NEW HERE, STATED PLAINLY
=========================================
The break half is a **20-bar Donchian**. `donchian_breakout_v1` is frozen ABSENT
at 91.2 / 91.5 on a 55-bar channel, and a reader is entitled to ask whether this
is that family with a shorter window.

It is not, and the difference is the **precondition**: this refuses a break that
happens while ATR sits above its 25th percentile. Whether that refusal does real
work is an empirical question, not a rhetorical one, so the count finding
reports the filter's survival rate — how many otherwise-qualifying breaks it
removes — and `tests/test_compression_breakout_v1.py` builds series where the
two families disagree in both directions.

THE TWO WINDOW ASYMMETRIES ARE DELIBERATE AND OPPOSITE
======================================================
They look inconsistent side by side, and both are the intake's:

* the **percentile window INCLUDES bar t**. The question is where *today's*
  volatility sits in its own recent distribution; ranking bar t against a window
  it is not a member of would answer a different question;
* the **channel EXCLUDES bar t**. A bar cannot break a level it helped set.
  Including `high[t]` in `HH[t]` would make `close[t] > HH[t]` almost
  unreachable — the rule would silently nearly never fire, and it would look
  like a market fact rather than an arithmetic one.

Getting either backwards produces a plausible signal that measures something
else, so both are pinned from both sides.

NO LOOKAHEAD, STRUCTURALLY
==========================
`ATR[t]` is Wilder ATR over bars up to and including `t`, which is knowable at
`t`'s close. The channel reads bars strictly before `t`. The percentile reads a
trailing window. Nothing reads past `t`, and a test truncates the series and
asserts every surviving decision is unchanged.

The fill is at `open[t+1]`, handled by the shared barrier via
`entry_on="next_open"`; this module never computes a price.

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

#: Every constant here was frozen in `NEW_SIGNAL_INTAKE.md` and EDGE.md §34b
#: BEFORE any number existed. None is a tunable, and none is to be
#: grid-searched — not before a run, not after seeing one.
COMP_MAX = 25.0            # ATR percentile at or below which a bar is compressed
PCTILE_WINDOW = 50         # bars of ATR history the rank is taken over, INCLUDING t
BREAK_N = 20               # channel length, t-BREAK_N .. t-1, EXCLUDING t
ATR_PERIOD = 14            # Wilder
STOP_ATR = 1.5             # R_dist = 1.5 * ATR at the signal bar
TAKE_PROFIT_R = 2.0        # breakout continuation target
#: The shared barrier's payoff is ``take_profit_atr / stop_atr``, so a 2.0 R
#: target on a 1.5 ATR stop is 3.0 ATR of take-profit. Writing 2.0 here would
#: give a payoff of 1.33 and quietly change the intake's target.
TAKE_PROFIT_ATR = STOP_ATR * TAKE_PROFIT_R   # == 3.0
HORIZON = 5                # daily bars; time stop at the close of entry_bar + 5
#: The instrument's, unchanged. Not this signal's to alter.
LOCKUP = 1
ROUND_TRIP_BPS = 25.0
ENTRY_ON = "next_open"

NAME = "compression_breakout_v1"

LONG_SETUP = "LONG_SETUP"    # compressed, and closed above the prior high
SHORT_SETUP = "SHORT_SETUP"  # compressed, and closed below the prior low


def _wilder_atr(bars: Sequence, period: int = ATR_PERIOD) -> np.ndarray:
    """Wilder ATR via the production implementation. Never re-derived here.

    ``atr[i]`` uses bars up to and including ``i``, which is what the intake
    means by "ATR available at t", and is the same value the shared barrier
    uses to size the stop at the signal bar.
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


def atr_percentiles(bars: Sequence, *, atr_period: int = ATR_PERIOD,
                    window: int = PCTILE_WINDOW) -> np.ndarray:
    """Percentile rank of ``ATR[t]`` within ``ATR[t-window+1] .. ATR[t]``.

    In [0, 100]. ``nan`` until the window is full of finite ATR values — a rank
    taken over a partial window would be computed against a different reference
    set than every later bar, which is a silent inconsistency rather than an
    error.

    The convention is ``(count of window values strictly below ATR[t]) /
    (window size) * 100``, matching `edge_measurement.percentile_of`. Ties count
    as *not below*, so a flat stretch of identical ATRs ranks 0 — correctly, since
    such a bar is not above anything in its own window.
    """
    atr = _wilder_atr(bars, atr_period)
    n = atr.size
    out = np.full(n, np.nan)
    for t in range(n):
        start = t - window + 1
        if start < 0:
            continue
        block = atr[start:t + 1]
        if not np.isfinite(block).all():
            continue
        out[t] = float((block < atr[t]).sum()) / float(block.size) * 100.0
    return out


def channel_bounds(bars: Sequence, *, n: int = BREAK_N
                   ) -> Tuple[np.ndarray, np.ndarray]:
    """``(HH, LL)`` over the ``n`` bars **strictly before** each bar.

    ``nan`` where there are not ``n`` prior bars. Bar ``t`` is excluded from its
    own channel: a bar cannot break a level it helped set, and including it
    would make the break condition an arithmetic near-impossibility rather than
    a market event.
    """
    high = np.array([b.high for b in bars], dtype=float)
    low = np.array([b.low for b in bars], dtype=float)
    size = high.size
    hh = np.full(size, np.nan)
    ll = np.full(size, np.nan)
    for t in range(n, size):
        hh[t] = float(high[t - n:t].max())
        ll[t] = float(low[t - n:t].min())
    return hh, ll


def compression_setups(bars: Sequence, *, comp_max: float = COMP_MAX,
                       window: int = PCTILE_WINDOW, n: int = BREAK_N,
                       atr_period: int = ATR_PERIOD) -> Dict[int, str]:
    """``{bar_index: LONG_SETUP | SHORT_SETUP}`` for every compressed break.

    Both halves must hold on the same bar. The threshold is **inclusive** —
    ``ATR_PCTILE <= 25`` — exactly as the intake writes it, and the break is
    **strict**: ``close > HH`` and ``close < LL``. Touching a level is not
    breaking it.

    The two break conditions cannot both hold, since ``HH >= LL`` on any real
    series; the case is written out anyway so a future edit that inverted the
    channel produces *no* setups rather than silently producing both.
    """
    pct = atr_percentiles(bars, atr_period=atr_period, window=window)
    hh, ll = channel_bounds(bars, n=n)
    close = np.array([b.close for b in bars], dtype=float)

    setups: Dict[int, str] = {}
    for t in range(close.size):
        if not np.isfinite(pct[t]) or pct[t] > comp_max:
            continue
        if not np.isfinite(hh[t]) or not np.isfinite(ll[t]):
            continue
        up = close[t] > hh[t]
        down = close[t] < ll[t]
        if up and down:
            continue                      # impossible; declared as no setup
        if up:
            setups[t] = LONG_SETUP
        elif down:
            setups[t] = SHORT_SETUP
    return setups


def breaks_without_compression(bars: Sequence, *, n: int = BREAK_N,
                               window: int = PCTILE_WINDOW,
                               atr_period: int = ATR_PERIOD,
                               comp_max: float = COMP_MAX) -> Dict[int, str]:
    """Every channel break, compressed or not. **Diagnostic only.**

    Exists so the count finding can report what the compression filter actually
    removes, which is the whole material-difference claim of this family
    (EDGE.md §34c). It is not used to generate setups and no measurement path
    calls it — `compression_setups` never consults it.
    """
    hh, ll = channel_bounds(bars, n=n)
    close = np.array([b.close for b in bars], dtype=float)
    out: Dict[int, str] = {}
    for t in range(close.size):
        if not np.isfinite(hh[t]) or not np.isfinite(ll[t]):
            continue
        if close[t] > hh[t]:
            out[t] = LONG_SETUP
        elif close[t] < ll[t]:
            out[t] = SHORT_SETUP
    return out


def directed_signal_bars(
    bars: Sequence, *, warmup: int = 0, comp_max: float = COMP_MAX,
    window: int = PCTILE_WINDOW, n: int = BREAK_N,
    atr_period: int = ATR_PERIOD,
) -> List[Tuple[int, str]]:
    """``[(signal_index, direction), ...]``, ordered.

    The index is the **signal** bar; the fill happens one bar later, which the
    shared barrier handles via ``entry_on="next_open"``. The last bar carries no
    setup — there is no next bar to fill on — and bars before ``warmup`` are
    excluded, matching every prior directed measurement here.
    """
    setups = compression_setups(bars, comp_max=comp_max, window=window, n=n,
                                atr_period=atr_period)
    if not setups:
        return []
    last = len(bars) - 1
    return [(index, setups[index]) for index in sorted(setups)
            if warmup <= index < last]


def flags_and_directions(
    bars: Sequence, *, warmup: int = 0, comp_max: float = COMP_MAX,
    window: int = PCTILE_WINDOW, n: int = BREAK_N,
    atr_period: int = ATR_PERIOD,
) -> Tuple[np.ndarray, Dict[int, str]]:
    """The same information in the shapes the instrument already speaks."""
    directed = directed_signal_bars(bars, warmup=warmup, comp_max=comp_max,
                                    window=window, n=n, atr_period=atr_period)
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
    every_break = breaks_without_compression(bars)
    last = len(bars) - 1
    unfiltered = sum(1 for t in every_break if warmup <= t < last)
    return {
        "bars": len(bars),
        "setups": len(directions),
        "long_setups": longs,
        "short_setups": len(directions) - longs,
        "breaks_any_volatility": unfiltered,
    }
