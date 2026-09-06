"""funding_carry_fade_v1 — fade extreme funding on USDT-M linear perpetuals.

THE THESIS, AS THE HUMAN WROTE IT
=================================
Funding is the periodic payment between longs and shorts on a perpetual.
Extreme funding is crowded carry: one side is paying a lot to hold its
position. The candidate inefficiency is a fade of that crowding over a short
daily horizon.

    f[t]  = funding_rate of the LAST print with funding_time <= close_time(t)

    f[t] >= +FUND_ABS  ->  SHORT_SETUP   (fade rich long carry)
    f[t] <= -FUND_ABS  ->  LONG_SETUP    (fade rich short carry)
    otherwise          ->  no setup
    missing f[t]       ->  no setup

The full specification is `NEW_SIGNAL_INTAKE.md`, human-filled and
authoritative. The design note fixed before this file existed is EDGE.md §36.

WHY THIS FAMILY IS DIFFERENT FROM THE OTHER TEN
===============================================
All ten frozen families read price and nothing else — closes, opens, highs,
lows, ATR. This one triggers on a **cash flow between position holders**,
published by the venue, which cannot be derived from any price series. The
barrier still scores in price, because a trade has to be scored somehow, but the
entry decision contains no price at all. A test asserts that: mangle every OHLC
value in the bar series and not one setup changes.

THE JOIN IS THE DANGEROUS PART
==============================
Funding ticks every 8 hours (faster on SOLUSDT for part of the sample — the
venue changed cadence, which `artifacts/slice55_data_eligibility.json` records).
The decision clock is the daily bar. Three rules:

1. `f[t]` is the **last** print at or before `close_time(t)`. Not the next, not
   the nearest, not the day's mean;
2. **no forward-fill** across a bar with no prior print — that bar has no setup.
   A rate carried forward is a guess about a payment that may not have happened;
3. **nothing after `close_time(t)`.** The failure mode this guards is an
   off-by-one letting the 00:00 print of day `t+1` decide day `t` — a full day
   of hindsight that would look entirely plausible in the output.

`funding_at_or_before` is written as a strict binary search on `<=`, and the
tests attack it from both sides: a print one second after the close must not be
used, and one exactly at the close must be.

FUNDING IS ALSO A COST, AND THAT IS NOT OPTIONAL
================================================
This rule goes short exactly when funding is rich and positive. A short receives
positive funding, so a measurement that ignored funding would let the fade
collect the carry for free and would flatter the strategy in the direction of
its own thesis. `funding_paid_over_hold` computes the signed sum of the prints
inside a hold, and the measurement path applies it identically to the observed
schedule and to every null replicate.

It is deliberately **not** folded into `skill_test.barrier_r_for_all_bars`. That
function is the one shared implementation every measurement in EDGE.md depends
on, and changing it would make every prior number non-reproducible.

WHAT THIS MODULE DOES NOT DO
============================
It computes **directed signal indices** and a **funding cost**, and nothing
else. Every R comes from `skill_test.barrier_r_for_all_bars`. No barrier
arithmetic is reproduced here, and the tests assert at AST level that none
exists.
"""
from __future__ import annotations

import bisect
import csv
import gzip
import io
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

#: Every constant here was frozen in `NEW_SIGNAL_INTAKE.md` and EDGE.md §36b
#: BEFORE any number existed. None is a tunable, and none is to be
#: grid-searched — not before a run, not after seeing one.
FUND_ABS = 0.0001          # |f| at or beyond which a bar is a setup
STOP_ATR = 1.5             # R_dist = 1.5 * ATR at the signal bar
TAKE_PROFIT_R = 1.0        # a fade target
#: The shared barrier's payoff is ``take_profit_atr / stop_atr``, so a 1.0 R
#: target on a 1.5 ATR stop is 1.5 ATR of take-profit. Writing 1.0 here would
#: give a payoff of 0.67 and quietly change the intake's target.
TAKE_PROFIT_ATR = STOP_ATR * TAKE_PROFIT_R   # == 1.5
HORIZON = 5                # daily bars; time stop at the close of entry_bar + 5
ATR_PERIOD = 14            # Wilder, on the linear daily series
#: The instrument's, unchanged. Not this signal's to alter.
LOCKUP = 1
ROUND_TRIP_BPS = 25.0
ENTRY_ON = "next_open"

NAME = "funding_carry_fade_v1"

LONG_SETUP = "LONG_SETUP"    # fade rich SHORT carry (funding deeply negative)
SHORT_SETUP = "SHORT_SETUP"  # fade rich LONG carry (funding richly positive)


# ---------------------------------------------------------------------------
# the funding series
# ---------------------------------------------------------------------------


class FundingSeries:
    """A symbol's funding prints, and the only two questions asked of them.

    Holds strictly increasing epoch-millisecond stamps and their rates. Built
    once per symbol and passed in, so nothing here reads the filesystem during
    a measurement and a test can construct one by hand.
    """

    __slots__ = ("times_ms", "rates", "symbol")

    def __init__(self, times_ms: Sequence[int], rates: Sequence[float],
                 *, symbol: str = "") -> None:
        times = [int(t) for t in times_ms]
        values = [float(r) for r in rates]
        if len(times) != len(values):
            raise ValueError("funding times and rates must be the same length")
        for earlier, later in zip(times, times[1:]):
            if later <= earlier:
                raise ValueError(
                    "funding times must be strictly increasing; the join is a "
                    "binary search and an unsorted series would silently "
                    "return the wrong print")
        self.times_ms = times
        self.rates = values
        self.symbol = symbol

    def __len__(self) -> int:
        return len(self.times_ms)

    def at_or_before(self, moment_ms: int) -> Optional[float]:
        """The rate of the last print at or before ``moment_ms``, else ``None``.

        ``None`` — never a forward-fill, never the next print — is what makes a
        bar with no prior funding produce no setup rather than a guess.
        """
        index = bisect.bisect_right(self.times_ms, int(moment_ms)) - 1
        if index < 0:
            return None
        return self.rates[index]

    def sum_between(self, start_ms: int, end_ms: int) -> float:
        """Signed sum of the rates of prints in ``(start_ms, end_ms]``.

        Half-open at the start: a print exactly at the entry moment has already
        been paid by whoever held the position before, and the trade being
        scored opens at that moment. Closed at the end: a print exactly at the
        exit is paid by the position still being held.
        """
        low = bisect.bisect_right(self.times_ms, int(start_ms))
        high = bisect.bisect_right(self.times_ms, int(end_ms))
        if high <= low:
            return 0.0
        return float(sum(self.rates[low:high]))


def _open_text(path: str) -> io.TextIOBase:
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return open(path, "rt", encoding="utf-8", newline="")


def load_funding(path: str) -> FundingSeries:
    """Read one funding CSV into a :class:`FundingSeries`.

    Uses `market_data.parse_timestamp`, the same parser the bar loader uses, so
    a timestamp this repository would reject in a bar file is rejected here for
    the same reason. Rows are not repaired, reordered or de-duplicated: the
    corpus contract has already established that the file is clean, and a
    loader that quietly fixed a bad file would hide the thing the contract is
    for.
    """
    import sys
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo not in sys.path:
        sys.path.insert(0, repo)
    import market_data as md

    times: List[int] = []
    rates: List[float] = []
    symbol = ""
    with _open_text(path) as handle:
        for row in csv.DictReader(handle):
            symbol = symbol or str(row.get("symbol", "")).strip()
            # The venue supplies `funding_time_ms` and it is AUTHORITATIVE:
            # the ISO string is truncated to the second, so the two disagree by
            # up to 30 ms on ~39% of rows. Neither could change a join against
            # a 23:59:59.999 boundary — no print in these corpora lands within
            # two seconds of one — but preferring the source's own field means
            # there is no discrepancy to explain away later.
            #
            # The ISO fallback runs `market_data.parse_timestamp`, which
            # returns MICROSECONDS. The first version of this loader multiplied
            # by 1000 instead of dividing, putting every print a million times
            # too far in the future; the join then found nothing and reported
            # ZERO setups on all three symbols. That would have read as a
            # market finding rather than a unit bug, which is why
            # `test_the_two_time_sources_agree_to_the_second` exists.
            declared = str(row.get("funding_time_ms", "")).strip()
            if declared:
                times.append(int(declared))
            else:
                micros = md.parse_timestamp(str(row["funding_time"]).strip())
                times.append(int(micros) // 1000)
            rates.append(float(row["funding_rate"]))
    return FundingSeries(times, rates, symbol=symbol)


# ---------------------------------------------------------------------------
# the join
# ---------------------------------------------------------------------------


def close_time_ms(bar: Any) -> int:
    """The instant a daily bar closes, in epoch milliseconds.

    Prefers the bar's own recorded end when the loader supplies one, and
    otherwise takes the start of the next day. Both are the same instant for
    these corpora; the fallback exists so a hand-built test bar without an end
    still joins correctly.
    """
    end_us = getattr(bar, "end_us", None)
    if end_us:
        return int(end_us // 1000)
    return int(bar.start_ms) + 86_400_000 - 1


def funding_at_decision(bars: Sequence, funding: FundingSeries) -> np.ndarray:
    """``f[t]`` for every bar, ``nan`` where no print exists at or before it.

    This is the whole join, and it reads **only** backwards in time.
    """
    out = np.full(len(bars), np.nan)
    for t, bar in enumerate(bars):
        rate = funding.at_or_before(close_time_ms(bar))
        if rate is not None:
            out[t] = rate
    return out


def funding_setups(bars: Sequence, funding: FundingSeries, *,
                   fund_abs: float = FUND_ABS) -> Dict[int, str]:
    """``{bar_index: LONG_SETUP | SHORT_SETUP}`` for every extreme-funding bar.

    The threshold is **inclusive** on both sides, exactly as the intake writes
    it: ``f >= +FUND_ABS`` and ``f <= -FUND_ABS``.

    Rich positive funding is a **SHORT** and rich negative funding is a
    **LONG**: the direction opposes the crowd that is paying, which is the
    entire hypothesis. A missing rate is not a setup.
    """
    rates = funding_at_decision(bars, funding)
    setups: Dict[int, str] = {}
    for t in range(rates.size):
        value = rates[t]
        if not np.isfinite(value):
            continue
        rich_long = value >= fund_abs
        rich_short = value <= -fund_abs
        # With fund_abs > 0 the two cannot both hold. Written out so that a
        # future edit passing a negative threshold produces NO setups rather
        # than silently producing both.
        if rich_long and rich_short:
            continue
        if rich_long:
            setups[t] = SHORT_SETUP
        elif rich_short:
            setups[t] = LONG_SETUP
    return setups


# ---------------------------------------------------------------------------
# funding as a cost
# ---------------------------------------------------------------------------


def funding_paid_over_hold(funding: FundingSeries, bars: Sequence,
                           signal_index: int, bars_held: int,
                           direction: str) -> float:
    """Funding paid (positive) or received (negative) over one hold, in rate units.

    The position opens at ``open[signal_index + 1]`` and closes ``bars_held``
    bars later, so the exposure window is
    ``(close_time(signal_index), close_time(signal_index + 1 + bars_held)]``.

    A **long** pays positive funding, so its cost is ``+sum``. A **short**
    receives it, so its cost is ``-sum``. Since this rule shorts exactly when
    funding is rich and positive, the sign convention is the difference between
    a measurement and a flattering one.

    Returned in **rate units** — the same units as the prints. The caller
    converts to R by dividing by the trade's risk fraction, which is the only
    place the two scales meet.
    """
    entry_index = signal_index + 1
    exit_index = min(entry_index + bars_held, len(bars) - 1)
    start = close_time_ms(bars[signal_index])
    end = close_time_ms(bars[exit_index])
    total = funding.sum_between(start, end)
    if direction == LONG_SETUP:
        return total
    if direction == SHORT_SETUP:
        return -total
    raise ValueError(f"unknown direction {direction!r}")


def funding_adjusted_net_r(bars: Sequence, funding: FundingSeries,
                          indices: np.ndarray, net_r: np.ndarray,
                          bars_used: np.ndarray, direction: str, *,
                          stop_atr: float = STOP_ATR,
                          atr_period: int = ATR_PERIOD) -> np.ndarray:
    """`net_r` with the funding paid over each hold subtracted, in R units.

    The shared barrier already charges the 25 bps round trip as
    ``(bps / 10_000) * entry / risk`` — a fraction of notional divided by the
    risk distance. Funding is the same kind of quantity, so it converts the
    same way:

        cost_R  =  signed_funding_fraction * entry / risk
        risk    =  stop_atr * atr[signal_index]
        entry   =  open[signal_index + 1]        (ENTRY_ON == "next_open")

    WHY THIS IS A SEPARATE ARRAY AND NOT A CHANGE TO THE BARRIER
    -----------------------------------------------------------
    `skill_test.barrier_r_for_all_bars` is the one implementation every
    measurement in EDGE.md depends on. Folding funding into it would make every
    prior number non-reproducible for a cost that applies to exactly one family.

    WHY IT MUST BE APPLIED HERE RATHER THAN TO THE OBSERVED TRADES
    -------------------------------------------------------------
    The returned array is indexed exactly like the barrier's own `net_r`, so
    the measurement path substitutes it once and **every** consumer — the
    observed schedule and all 1,500 rotation replicates and 200 shape-matched
    schedules — is charged identically. Charging only the observed side would
    be the most flattering possible bug and the hardest to see in a summary.
    """
    import sys
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tools = os.path.join(repo, "tools")
    for candidate in (repo, tools):
        if candidate not in sys.path:
            sys.path.insert(0, candidate)
    import sweep_geometry as sweep

    high = np.array([b.high for b in bars], dtype=float)
    low = np.array([b.low for b in bars], dtype=float)
    close = np.array([b.close for b in bars], dtype=float)
    open_ = np.array([b.open for b in bars], dtype=float)
    atr = sweep.wilder_atr(high, low, close, atr_period)

    adjusted = np.array(net_r, dtype=float, copy=True)
    for position, signal_index in enumerate(np.asarray(indices, dtype=int)):
        risk = stop_atr * atr[signal_index]
        if not np.isfinite(risk) or risk <= 0.0:
            continue
        entry = open_[signal_index + 1]
        paid = funding_paid_over_hold(funding, bars, int(signal_index),
                                      int(bars_used[position]), direction)
        adjusted[position] -= paid * entry / risk
    return adjusted


# ---------------------------------------------------------------------------
# the shapes the instrument speaks
# ---------------------------------------------------------------------------


def directed_signal_bars(bars: Sequence, funding: FundingSeries, *,
                         warmup: int = 0,
                         fund_abs: float = FUND_ABS) -> List[Tuple[int, str]]:
    """``[(signal_index, direction), ...]``, ordered.

    The index is the **signal** bar; the fill happens one bar later, which the
    shared barrier handles via ``entry_on="next_open"``. The last bar carries no
    setup — there is no next bar to fill on — and bars before ``warmup`` are
    excluded, matching every prior directed measurement here.
    """
    setups = funding_setups(bars, funding, fund_abs=fund_abs)
    if not setups:
        return []
    last = len(bars) - 1
    return [(index, setups[index]) for index in sorted(setups)
            if warmup <= index < last]


def flags_and_directions(bars: Sequence, funding: FundingSeries, *,
                         warmup: int = 0, fund_abs: float = FUND_ABS
                         ) -> Tuple[np.ndarray, Dict[int, str]]:
    """The same information in the shapes the instrument already speaks."""
    directed = directed_signal_bars(bars, funding, warmup=warmup,
                                    fund_abs=fund_abs)
    flags = np.zeros(len(bars), dtype=bool)
    directions: Dict[int, str] = {}
    for index, direction in directed:
        flags[index] = True
        directions[index] = direction
    return flags, directions


def summary(bars: Sequence, funding: FundingSeries, *,
            warmup: int = 0) -> Dict[str, Any]:
    """Counts a reader can check against the log. Never used to filter."""
    _flags, directions = flags_and_directions(bars, funding, warmup=warmup)
    longs = sum(1 for d in directions.values() if d == LONG_SETUP)
    rates = funding_at_decision(bars, funding)
    return {
        "bars": len(bars),
        "funding_prints": len(funding),
        "bars_with_funding": int(np.isfinite(rates).sum()),
        "bars_without_funding": int((~np.isfinite(rates)).sum()),
        "setups": len(directions),
        "long_setups": longs,
        "short_setups": len(directions) - longs,
    }
