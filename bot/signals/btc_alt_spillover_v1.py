"""btc_alt_spillover_v1 — a BTC shock, then the alt, one bar later.

THE THESIS, AS THE HUMAN WROTE IT
=================================
BTC is the primary risk asset and attention anchor. A large BTC daily move is
absorbed first in BTC's own books; higher-beta majors (ETH, SOL) often continue
the same-direction move a bar or several later, as capital, leverage and
discretionary flow rotate. The other side of the trade is market makers on the
alt hedging with lag, and slower discretionary traders reacting to BTC headlines
after the BTC bar has already closed.

The full specification is `NEW_SIGNAL_INTAKE.md`, human-filled and authoritative.
This module implements it and nothing else.

MATERIAL DIFFERENCE — WHY THIS IS ELIGIBLE AT ALL
=================================================
The information set is **disjoint from both closed families**:

    technical_analysis     RSI, MACD, Bollinger, Supertrend, ADX on the target
    donchian_breakout_v1   the target's own N-day high
    this signal            BTC's close-to-close return, scaled by BTC's own
                           Wilder ATR(14). The target's price is used ONLY for
                           execution geometry — stop, take-profit, time stop.

The trigger never looks at the alt. Whether to trade is decided entirely on a
different instrument, on a different venue.

WHAT THIS MODULE DOES NOT DO
============================
It computes **directed entry indices** and nothing else. Every R comes from
`skill_test.barrier_r_for_all_bars`, the one shared implementation the null also
uses. No barrier arithmetic is reproduced here — a forked copy that drifted by a
line would make this measurement incomparable with everything already in
EDGE.md, and `tests/test_btc_alt_spillover_v1.py` asserts at AST level that none
exists.

NO LOOKAHEAD, STRUCTURALLY
==========================
The trigger reads `close_btc[t]` and `close_btc[t-1]` and an ATR ending at `t`.
The fill is at the alt's open of the bar *after* the shared UTC date. Nothing
reads past `t` to decide, and nothing reads the entry bar to size. A test
truncates the series and asserts the surviving prefix is unchanged.

ALIGNMENT IS BY UTC DATE, AND MISSING DATES ARE SKIPPED
=======================================================
BTC is Bitstamp BTC/USD; the alts are Binance USDT spot. Two venues, two
calendars. A date present on one and absent on the other is **skipped** — never
forward-filled, never interpolated. The intake form calls the venue split part
of the hypothesis rather than a hidden degree of freedom, and skipping is the
only treatment that keeps that honest.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

#: Every constant here was frozen in `NEW_SIGNAL_INTAKE.md` and EDGE.md §16b
#: BEFORE any number existed. None of them is a tunable, and none is to be
#: grid-searched — not before a run, not after seeing one.
SHOCK_MULTIPLIER = 1.0     # r_btc vs SHOCK_MULTIPLIER * (ATR_btc / close_btc)
ATR_PERIOD = 14            # Wilder, on both BTC and the alt
STOP_ATR = 1.5             # R_dist = 1.5 * ATR_alt
TAKE_PROFIT_R = 2.0        # fixed 2R
TAKE_PROFIT_ATR = STOP_ATR * TAKE_PROFIT_R   # == 3.0 ATR_alt
HORIZON = 5                # daily bars; time stop at close of entry_bar + 5
LOCKUP = 1                 # one trade per run
ROUND_TRIP_BPS = 25.0
ENTRY_ON = "next_open"     # fill at the alt's open of the bar after the shock

NAME = "btc_alt_spillover_v1"

LONG_SETUP = "LONG_SETUP"
SHORT_SETUP = "SHORT_SETUP"

#: UTC dates dropped from the BTC driver before any shock is computed.
#:
#: Slice 39, human pre-declaration, quoted in EDGE.md 20a: *"exclude UTC date
#: 2025-01-07 from the BTC driver for BOTH real-series signal generation and
#: the directed null/control path"*. The bar is a stub — the 1-minute Bitstamp
#: archive stops a minute or two into 7 January 2025, leaving a bar with volume
#: 0.52 against a corpus median of 2,807.7 and a high-low range of $28.
#:
#: This is a DATA-QUALITY exclusion and never a tunable. It is a fixed list of
#: calendar dates, declared before it was applied, and dates are not to be
#: added to it after seeing a number — the whole value of the list is that it
#: cannot be grown to taste. `tests/test_btc_alt_spillover_v1.py` pins its
#: exact contents.
#:
#: It does not do what it was declared to do, and EDGE.md 20b says so with the
#: arithmetic: the stub's close is 0.017% from the previous close, so the
#: 2025-01-08 shock survives it. Kept because a partial bar has no business in
#: a daily corpus, not because it fixed anything.
EXCLUDED_BTC_UTC_DATES: Tuple[str, ...] = ("2025-01-07",)


def _utc_date(bar: Any) -> str:
    """The bar's UTC calendar date as ``YYYY-MM-DD``.

    Alignment is by date only, deliberately: the two venues stamp their daily
    bars at the same UTC midnight but the objects carry epoch milliseconds, and
    comparing dates is what the specification says.
    """
    import datetime as _dt
    ms = int(getattr(bar, "start_ms", 0))
    return _dt.datetime.fromtimestamp(ms / 1000.0, tz=_dt.timezone.utc).strftime(
        "%Y-%m-%d")


def usable_btc_bars(btc_bars: Sequence) -> List[Any]:
    """``btc_bars`` with every :data:`EXCLUDED_BTC_UTC_DATES` bar dropped.

    One chokepoint on purpose. Both paths that matter — the real series and the
    directed null — reach the driver through :func:`btc_setups`, so filtering
    here is what makes "in BOTH paths" true by construction rather than by two
    call sites that have to be kept in step. The control shuffles the *alt*
    bars and leaves BTC alone, which is exactly why the BTC-side filter covers
    it: the surrogate runs consume the same flag array the real run does.

    Order is preserved and nothing is re-based, re-stamped or interpolated. A
    dropped bar leaves a one-day hole, and the trigger already reads
    ``close[t]`` against ``close[t-1]`` positionally — so after the drop the
    return spanning the hole is a two-day move. That is a real consequence and
    EDGE.md 20b works it out for the one date this list contains.
    """
    if not EXCLUDED_BTC_UTC_DATES:
        return list(btc_bars)
    excluded = frozenset(EXCLUDED_BTC_UTC_DATES)
    return [bar for bar in btc_bars if _utc_date(bar) not in excluded]


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


def btc_setups(btc_bars: Sequence, *, multiplier: float = SHOCK_MULTIPLIER,
               atr_period: int = ATR_PERIOD) -> Dict[str, str]:
    """UTC date -> ``LONG_SETUP`` / ``SHORT_SETUP`` for every BTC shock bar.

    ``shock_up`` when ``r_btc[t] >= +m * (ATR_btc[t] / close_btc[t])``,
    ``shock_dn`` when ``r_btc[t] <= -m * (...)``. The two are mutually exclusive
    for any positive threshold, but the specification writes
    "shock_up and not shock_dn" explicitly and that is honoured literally rather
    than optimised away — a threshold of zero would make them overlap, and a
    reader should be able to see the rule that was actually declared.

    :data:`EXCLUDED_BTC_UTC_DATES` is applied here, before the first return is
    computed. Every caller — real series and directed null alike — arrives
    through this function, so there is one place to read and one place to get
    wrong.
    """
    btc_bars = usable_btc_bars(btc_bars)
    close = np.array([b.close for b in btc_bars], dtype=float)
    atr = _atr(btc_bars, atr_period)
    setups: Dict[str, str] = {}
    for t in range(1, close.size):
        if not np.isfinite(atr[t]) or atr[t] <= 0.0 or close[t] <= 0.0:
            continue
        if close[t - 1] <= 0.0:
            continue
        r = (close[t] - close[t - 1]) / close[t - 1]
        threshold = multiplier * (atr[t] / close[t])
        shock_up = r >= +threshold
        shock_dn = r <= -threshold
        if shock_up and not shock_dn:
            setups[_utc_date(btc_bars[t])] = LONG_SETUP
        elif shock_dn and not shock_up:
            setups[_utc_date(btc_bars[t])] = SHORT_SETUP
    return setups


def directed_signal_bars(
    alt_bars: Sequence, btc_bars: Sequence, *, warmup: int = 0,
    multiplier: float = SHOCK_MULTIPLIER, atr_period: int = ATR_PERIOD,
) -> List[Tuple[int, str]]:
    """``[(alt_signal_index, direction), ...]``, ordered, one per shared date.

    The returned index is the alt bar carrying the **shared UTC date** — the
    signal bar. The fill happens one bar later, which the shared barrier
    arithmetic handles via ``entry_on="next_open"``; this module never computes
    a price.

    A date present on BTC but not on the alt (or the reverse) is simply absent
    from the result. So is a date whose alt bar is the last one, since there is
    no next bar to fill on.
    """
    setups = btc_setups(btc_bars, multiplier=multiplier, atr_period=atr_period)
    if not setups:
        return []
    last = len(alt_bars) - 1
    out: List[Tuple[int, str]] = []
    for index in range(len(alt_bars)):
        if index < warmup or index >= last:
            continue
        direction = setups.get(_utc_date(alt_bars[index]))
        if direction is not None:
            out.append((index, direction))
    return out


def flags_and_directions(
    alt_bars: Sequence, btc_bars: Sequence, *, warmup: int = 0,
    multiplier: float = SHOCK_MULTIPLIER, atr_period: int = ATR_PERIOD,
) -> Tuple[np.ndarray, Dict[int, str]]:
    """The same information as :func:`directed_signal_bars`, in the shapes the
    instrument already speaks: a boolean flag array and an index -> direction map.

    The flag array is what ``simulate_schedule``, ``extract_blocks`` and the
    rotation null consume unchanged. The direction map is carried alongside;
    it is never used to decide *whether* a bar is a candidate, only *which way*
    a trade taken there points.
    """
    directed = directed_signal_bars(
        alt_bars, btc_bars, warmup=warmup, multiplier=multiplier,
        atr_period=atr_period)
    flags = np.zeros(len(alt_bars), dtype=bool)
    directions: Dict[int, str] = {}
    for index, direction in directed:
        flags[index] = True
        directions[index] = direction
    return flags, directions


def aligned_date_span(alt_bars: Sequence, btc_bars: Sequence
                      ) -> Tuple[int, Optional[str], Optional[str]]:
    """``(n_shared_dates, first, last)`` — reported, never used to filter.

    The BTC corpus ends well before the alts do, so a reader needs to know how
    much of the alt series was actually tradable. Printing it is the difference
    between a measurement and a measurement people can check.

    Excluded dates are dropped here too. A span that counted a date the trigger
    can never fire on would overstate the tradable window by exactly the number
    of bars the exclusion removed.
    """
    btc_bars = usable_btc_bars(btc_bars)
    btc_dates = {_utc_date(b) for b in btc_bars}
    alt_dates = {_utc_date(b) for b in alt_bars}
    shared = sorted(btc_dates & alt_dates)
    if not shared:
        return 0, None, None
    return len(shared), shared[0], shared[-1]
