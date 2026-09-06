"""open_gap_fade_v1 — fade the gap between the prior close and today's open.

THE THESIS, AS THE HUMAN WROTE IT
=================================
A daily bar that **opens** far from the prior close creates an overnight
inventory and attention discontinuity. Market makers and discretionary flow
often partially reclaim that gap over the following sessions.

The candidate inefficiency is a **fade of the open gap** — not a close-to-close
shock, not a close's location inside a trailing range, not a Donchian break, not
a BTC lag, not an oscillator committee. The trigger's entire information set is

    { open[t],  close[t-1],  ATR_prev[t] }

The full specification is `NEW_SIGNAL_INTAKE.md`, human-filled and
authoritative. This module implements it and nothing else: no volume filter, no
regime condition, no second indicator, no exception for any date. The design
note fixed before this file existed is EDGE.md §30.

MATERIAL DIFFERENCE — WHY THIS IS ELIGIBLE AT ALL
=================================================
Six names are frozen. Checked against each:

    technical_analysis      RSI/MACD/Bollinger/Supertrend/ADX committee
    closed_analyser         the same committee under its other name
    donchian_breakout_v1    a BREAK of the N-day high/low, traded as continuation
    btc_alt_spillover_v1    BTC's return, traded on ETH/SOL
    post_shock_fade_v1      this symbol's own CLOSE-TO-CLOSE return / ATR
    range_location_fade_v1  WHERE the close sits inside a trailing 20-bar range
    this signal             the OVERNIGHT move: open[t] against close[t-1]

The nearest neighbour is `post_shock_fade_v1` — same asset, same fade direction,
same barrier family. The distinction is the window over which the move is
measured: **between** sessions rather than **across** one. A day that closes 3
ATR from its prior close but opens flat fires there and not here; a day that
opens 1 ATR away and closes back where it started fires here and not there.
`tests/test_open_gap_fade_v1.py` builds both fixtures and asserts the
disagreement in both directions, because an argument that two triggers differ is
not evidence that they do.

THE INDEX CONVENTION, AND WHY IT IS NOT A TRICK
===============================================
The intake fills at `open[t]` — the open of the gap bar itself, not the next
bar's. That is expressed in the existing instrument with no new arithmetic by
naming the **signal bar** ``s = t - 1``:

    signal at index s   ->  shared barrier fills at open[s+1] == open[t]
    risk sized on atr[s] ->  Wilder ATR over bars <= s == ATR_prev[t]
    horizon 4 from entry ->  time stop at close[t+4]

So `directed_signal_bars` returns ``s``, one less than the bar the gap belongs
to, and `ENTRY_ON = "next_open"` does the rest. `gap_values` is indexed by
``t`` — the bar the gap is measured on — and `gap_setups` converts to ``s``
exactly once, in one place, so the two conventions cannot be confused at a call
site. Tests assert the relationship from both ends.

NO LOOKAHEAD — AND THE ONE PLACE THIS IS SUBTLE
===============================================
`ATR_prev[t]` reads bars strictly before `t`; a test truncates the series and
asserts every surviving gap value is unchanged to the last bit.

The trigger does read `open[t]`, which is *after* the signal bar `s = t-1`. That
is not lookahead: the open of bar t is known at the instant of bar t's open,
which is the instant the position is opened. Decision and fill are simultaneous.

It is, however, **optimistic**: you cannot observe a print and trade at that same
print. The human declared this fill explicitly, so it is implemented as written
and the caveat is recorded in EDGE.md §30c rather than discovered later as an
excuse. It applies identically to the null, which fills the same way on rotated
dates, so M1 and M2 stay internally comparable — what is flattered is the
absolute mean R, not the ranking.

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

#: Every constant here was frozen in `NEW_SIGNAL_INTAKE.md` and EDGE.md §30b
#: BEFORE any number existed. None is a tunable, and none is to be
#: grid-searched — not before a run, not after seeing one.
GAP_K = 0.75               # |gap| >= GAP_K fires; INCLUSIVE on both sides
ATR_PERIOD = 14            # Wilder, over bars up to t-1 only
STOP_ATR = 1.5             # R_dist = 1.5 * ATR_prev[t]
TAKE_PROFIT_R = 1.0        # fixed 1:1 — a mean-reversion target
#: The shared barrier's payoff is ``take_profit_atr / stop_atr``, so a 1.0 R
#: target on a 1.5 ATR stop is 1.5 ATR of take-profit. Writing 1.0 here would
#: quietly change the STOP distance, which the intake fixes at 1.5 ATR.
TAKE_PROFIT_ATR = STOP_ATR * TAKE_PROFIT_R   # == 1.5
HORIZON = 4                # daily bars; time stop at the close of entry_bar + 4
LOCKUP = 1                 # one trade per run
ROUND_TRIP_BPS = 25.0
#: Read this with the index convention above: the signal is emitted at ``t-1``
#: so that "next open" is the open of the gap bar itself.
ENTRY_ON = "next_open"

NAME = "open_gap_fade_v1"

LONG_SETUP = "LONG_SETUP"    # fade a DOWN-gap: buy the discount
SHORT_SETUP = "SHORT_SETUP"  # fade an UP-gap: sell the premium


def _wilder_atr(bars: Sequence, period: int = ATR_PERIOD) -> np.ndarray:
    """Wilder ATR via the production implementation. Never re-derived here.

    ``atr[i]`` uses bars up to and including ``i``. The gap at bar ``t``
    therefore divides by ``atr[t-1]``, which is what the intake calls
    ``ATR_prev[t]`` — and it is the same value the shared barrier uses to size
    the stop, because the barrier reads the ATR at the signal index and the
    signal index is ``t-1``.
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


def gap_values(bars: Sequence, *, atr_period: int = ATR_PERIOD) -> np.ndarray:
    """``gap[t]`` for every bar, ``nan`` where it is undefined.

    ``gap[t] = (open[t] - close[t-1]) / ATR_prev[t]``, indexed by **t**: the
    bar whose open is being measured. Bar 0 has no prior close and is always
    ``nan``.

    ``nan`` also where ``ATR_prev[t]`` is not finite or is not strictly
    positive. A zero ATR is a flat window, in which any gap at all is infinitely
    many ATRs — the division would be a division by zero dressed up as a very
    strong signal, so those bars carry no setup at all.
    """
    open_ = np.array([b.open for b in bars], dtype=float)
    close = np.array([b.close for b in bars], dtype=float)
    atr = _wilder_atr(bars, atr_period)

    n = close.size
    gap = np.full(n, np.nan)
    if n < 2:
        return gap
    prev_atr = atr[:-1]
    valid = np.isfinite(prev_atr) & (prev_atr > 0.0)
    raw = open_[1:] - close[:-1]
    gap[1:] = np.where(valid, raw / np.where(valid, prev_atr, 1.0), np.nan)
    return gap


def gap_setups(bars: Sequence, *, gap_k: float = GAP_K,
               atr_period: int = ATR_PERIOD) -> Dict[int, str]:
    """``{signal_index: LONG_SETUP | SHORT_SETUP}``, keyed by ``s = t - 1``.

    The threshold is **inclusive** on both sides, exactly as the intake writes
    it: ``gap >= +GAP_K`` and ``gap <= -GAP_K``. A gap landing exactly on
    ±0.75 fires. That is a decision, not an accident of a comparison operator,
    and a test pins both boundaries.

    An **up**-gap is a SHORT and a **down**-gap is a LONG: the direction is the
    opposite of the gap, which is the entire hypothesis.

    The returned keys are signal indices ``s = t - 1`` so that the shared
    barrier's ``entry_on="next_open"`` fills at ``open[t]``. This is the only
    place the two conventions meet.
    """
    gap = gap_values(bars, atr_period=atr_period)
    setups: Dict[int, str] = {}
    for t in range(gap.size):
        value = gap[t]
        if not np.isfinite(value):
            continue
        up = value >= gap_k
        down = value <= -gap_k
        # With gap_k > 0 the two cannot both hold. The case is written out
        # anyway so that a future edit passing a negative threshold produces
        # NO setups rather than silently producing both.
        if up and down:
            continue
        if up:
            setups[t - 1] = SHORT_SETUP      # fade the up-gap
        elif down:
            setups[t - 1] = LONG_SETUP       # fade the down-gap
    return setups


def directed_signal_bars(
    bars: Sequence, *, warmup: int = 0, gap_k: float = GAP_K,
    atr_period: int = ATR_PERIOD,
) -> List[Tuple[int, str]]:
    """``[(signal_index, direction), ...]``, ordered, signal index ``s = t-1``.

    The fill happens at ``open[s+1]``, which the shared barrier handles via
    ``entry_on="next_open"``; this module never computes a price.

    The last bar carries no setup — there is no next bar to fill on — and bars
    before ``warmup`` are excluded, matching every prior directed measurement
    here.
    """
    setups = gap_setups(bars, gap_k=gap_k, atr_period=atr_period)
    if not setups:
        return []
    last = len(bars) - 1
    return [(index, setups[index]) for index in sorted(setups)
            if warmup <= index < last]


def flags_and_directions(
    bars: Sequence, *, warmup: int = 0, gap_k: float = GAP_K,
    atr_period: int = ATR_PERIOD,
) -> Tuple[np.ndarray, Dict[int, str]]:
    """The same information in the shapes the instrument already speaks.

    A boolean flag array — what ``simulate_schedule``, ``extract_blocks`` and
    the rotation null consume unchanged — and an index -> direction map carried
    alongside. The map never decides *whether* a bar is a candidate, only which
    way a trade taken there points.
    """
    directed = directed_signal_bars(bars, warmup=warmup, gap_k=gap_k,
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
