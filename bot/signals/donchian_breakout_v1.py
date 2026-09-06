"""donchian_breakout_v1 — a channel breakout, and nothing else.

WHAT THIS IS, AND WHY IT EXISTS
===============================
The classical `technical_analysis` analyser was measured against a validated
instrument on three timeframes and failed every time (EDGE.md §5c, §6d;
`STAGE1_VERDICT.md`). That signal layer is CLOSED. This module is a **new
signal**, specified by a human in the slice-28 mission and implemented exactly
as written — not improved, not extended, not tuned.

The thesis, as given: liquid crypto trend followers and stop-driven
discretionary traders create continuation after range breakouts. Once price
closes beyond a multi-week high, inventory and stop cascades can push further
before mean reversion. The other side is range traders and early fades, who are
wrong when a new regime leg starts.

MATERIAL DIFFERENCE FROM THE CLOSED ANALYSER
--------------------------------------------
This is the check that decides whether a candidate is even eligible for Stage 1
re-entry, and "same indicators, new weights" fails it. Here the information set
is **disjoint**:

    closed analyser   RSI, MACD, Bollinger, Supertrend, ADX, plus a weighted
                      confidence score and a component-agreement vote
    this signal       the rolling high of the previous N bars. That is all.

No oscillator, no volatility band, no trend filter, no committee. The decision
is one inequality. And the sampling differs too: this fires on the **breakout
event** — the bar that crosses — not on every bar that happens to sit above the
channel while a trend runs.

NO LOOK-AHEAD, STRUCTURALLY
---------------------------
`upper_channel(i)` is built from `high[i-N : i]` — a half-open slice ending at
`i-1`. The bar being judged is **never** part of the channel it is judged
against. That is not a convention to remember; it is the only way the arrays are
ever built here, and `tests/test_donchian_breakout_v1.py` asserts it by
truncating the future and checking the prefix is unchanged.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
------------------------------------------
It produces **flags** and nothing else — no scoring, no R arithmetic, no
schedule. Entries, barriers and every R come from `skill_test`, which is the
machinery slice 23 validated. A forked copy that drifted by one line would make
the measurement incomparable with everything already in EDGE.md, so there is no
copy.

It also never shorts. Spot is long-only; `lower_channel` is computed and
exported because the tests check it and because a reader will look for it, but
nothing in the signal path consumes it.
"""
from __future__ import annotations

from typing import Sequence, Tuple

import numpy as np

#: Fixed by the slice-28 pre-declaration. **Not** a tunable.
#: Grid-searching N after seeing a percentile is exactly the bar-shopping
#: RESEARCH_STATUS.md forbids; a different N would be a different signal
#: needing its own human pre-declaration.
CHANNEL_N = 55

#: The barrier the pre-declaration fixed — identical to the instrument slice 23
#: validated, so that the only difference between this measurement and slice
#: 24's is which bars were chosen.
STOP_ATR = 2.0
TAKE_PROFIT_ATR = 4.0
HORIZON = 24
ATR_PERIOD = 14
ROUND_TRIP_BPS = 25.0
LOCKUP = 1

NAME = "donchian_breakout_v1"


def channels(
    high: np.ndarray, low: np.ndarray, *, n: int = CHANNEL_N,
) -> Tuple[np.ndarray, np.ndarray]:
    """Rolling extremes of the ``n`` bars **strictly before** each bar.

    ``upper[i] = max(high[i-n : i])`` and ``lower[i] = min(low[i-n : i])``.
    Both are NaN for ``i < n``, where the window is incomplete.

    The half-open slice is the whole no-lookahead argument: it ends at ``i-1``,
    so bar ``i``'s own high can never raise the channel it must clear.
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    size = high.size
    upper = np.full(size, np.nan)
    lower = np.full(size, np.nan)
    if n < 1:
        return upper, lower
    for i in range(n, size):
        upper[i] = high[i - n:i].max()
        lower[i] = low[i - n:i].min()
    return upper, lower


def breakout_flags(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, *, n: int = CHANNEL_N,
) -> np.ndarray:
    """True on each bar that CLOSES above a channel it did not close above before.

    ```
    flags[i] = close[i] > upper[i]  AND  close[i-1] <= upper[i-1]
    ```

    Both clauses matter. The first is the breakout. The second makes it an
    **event**: without it, every bar of a long trend above the channel would
    flag, which would turn one idea into a hundred correlated entries and make
    the trade count a function of how long trends last rather than of how often
    breakouts happen.

    False for ``i <= n`` — the warm-up, where either the channel or its
    predecessor is undefined. Never true on a NaN comparison, because NaN
    comparisons are False in both directions and that is the fail-closed answer
    when the channel does not exist yet.
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    upper, _lower = channels(high, low, n=n)

    flags = np.zeros(close.size, dtype=bool)
    if close.size <= n:
        return flags
    # Index from n+1: bar n has a channel but bar n-1 does not, so the
    # "was not above it yesterday" clause is not yet answerable.
    for i in range(n + 1, close.size):
        if np.isnan(upper[i]) or np.isnan(upper[i - 1]):
            continue
        if close[i] > upper[i] and close[i - 1] <= upper[i - 1]:
            flags[i] = True
    return flags


def flags_from_bars(bars: Sequence, *, warmup: int = 0, n: int = CHANNEL_N) -> np.ndarray:
    """Flags for a sequence of bar objects with ``.high``/``.low``/``.close``.

    The adapter the measurement tools call. ``warmup`` suppresses flags before
    a caller-supplied index so this signal obeys the same warm-up the rest of
    the instrument uses; it is applied **on top of** the channel warm-up, never
    instead of it.
    """
    high = np.array([b.high for b in bars], dtype=float)
    low = np.array([b.low for b in bars], dtype=float)
    close = np.array([b.close for b in bars], dtype=float)
    flags = breakout_flags(high, low, close, n=n)
    if warmup > 0:
        flags[:int(warmup)] = False
    return flags
