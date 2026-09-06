"""sign_flip_momentum_v1 — trade the CHANGE in a fixed-window return's sign.

THE THESIS, AS THE HUMAN WROTE IT
=================================
A sign change in a fixed-window cumulative return marks a discrete regime
transition, and the new direction is a candidate for short-horizon continuation
after costs. Entries occur **only on the flip event**.

    ret[t]  = close[t - SKIP] / close[t - SKIP - LOOKBACK] - 1
    sign[t] = +1 / -1 / 0
    FLIP    = sign[t] differs from the latest earlier NON-ZERO sign

The full specification is `NEW_SIGNAL_INTAKE.md`, human-filled and
authoritative. This module implements it and nothing else: no magnitude
threshold, no volume filter, no regime condition, no second indicator, no
exception for any date. The design note fixed before this file existed is
EDGE.md §33.

ITS RELATIONSHIP TO `ts_momentum_v1`, STATED RATHER THAN GLOSSED
================================================================
This module reads the **same state variable and the same window** as the frozen
`ts_momentum_v1`. `ret[t]` is computed identically. What differs is the event
extraction: that family flagged every bar where `ret` had a sign; this one flags
only the bars where that sign **changed**. Its flags are therefore a strict
subset of that family's, with the same directions on the bars they share, and a
test asserts exactly that rather than leaving it to be discovered.

Two things follow, both answered in EDGE.md §33a and repeated here because a
reader of this file deserves them without a second lookup:

* **It is not a retune of a frozen name.** `ts_momentum_v1` is INCONCLUSIVE — it
  produced no M1 and no M2. There is no result to fit toward.
* **It is not the forbidden scheduler repair.** `simulate_schedule` is untouched
  and one-trade-per-run stands. The human reshaped the *signal* to match the
  instrument, which is the allowed direction; reshaping the instrument to match
  the signal, after seeing a count, is what slice 51's freeze forbids.

WHY THE FLIP DEFINITION IS WRITTEN THE WAY IT IS
================================================
`previous_signs` walks backwards past zeros. That matters: a `+`, then three
bars of exactly zero return, then a `−` is **one** flip, at the `−`. Treating a
zero as a state reset would turn every zero-return bar into a flip candidate on
its successor, roughly doubling the sample on a corpus that has exactly-equal
closes — which these corpora do.

The first non-zero sign in a series is **never** a flip: there is no earlier
sign to differ from. Coding this as "sign changed from the previous bar" would
open a trade at the start of every series, which is a boundary artefact and not
a regime transition.

NO LOOKAHEAD, STRUCTURALLY
==========================
Every close the trigger reads is strictly before bar `t`, and the flip test
looks only backwards. The fill is at `open[t+1]`, handled by the shared barrier
via `entry_on="next_open"`; this module never computes a price. A test truncates
the series and asserts every surviving decision, and every surviving `ret` value
bit for bit, is unchanged.

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

#: Every constant here was frozen in `NEW_SIGNAL_INTAKE.md` and EDGE.md §33b
#: BEFORE any number existed. None is a tunable, and none is to be
#: grid-searched — not before a run, not after seeing one.
LOOKBACK = 10              # bars of return in the window
SKIP = 1                   # most recent closed bar excluded from the window
ATR_PERIOD = 14            # Wilder, for the stop distance only
STOP_ATR = 1.5             # R_dist = 1.5 * ATR at the signal bar
TAKE_PROFIT_R = 2.0        # momentum target
#: The shared barrier's payoff is ``take_profit_atr / stop_atr``, so a 2.0 R
#: target on a 1.5 ATR stop is 3.0 ATR of take-profit. Writing 2.0 here would
#: give a payoff of 1.33 and quietly change the intake's target.
TAKE_PROFIT_ATR = STOP_ATR * TAKE_PROFIT_R   # == 3.0
HORIZON = 5                # daily bars; time stop at the close of entry_bar + 5
#: UNCHANGED from the instrument. The intake is explicit that this stays as it
#: is: "do not change scheduler after counts".
LOCKUP = 1
ROUND_TRIP_BPS = 25.0
ENTRY_ON = "next_open"

NAME = "sign_flip_momentum_v1"

LONG_SETUP = "LONG_SETUP"    # flip to a positive windowed return
SHORT_SETUP = "SHORT_SETUP"  # flip to a negative windowed return

#: The first bar at which the window is fully defined. Named rather than
#: inlined because an off-by-one here would silently shorten the window.
FIRST_DEFINED = SKIP + LOOKBACK


def window_bounds(t: int) -> Tuple[int, int]:
    """The two close indices `ret[t]` divides, as ``(start, end)``.

    ``end = t - SKIP`` and ``start = t - SKIP - LOOKBACK``. Exposed so the tests
    can check the intake's arithmetic against the implementation rather than
    against a copy of it.
    """
    return t - SKIP - LOOKBACK, t - SKIP


def momentum_returns(bars: Sequence, *, lookback: int = LOOKBACK,
                     skip: int = SKIP) -> np.ndarray:
    """``ret[t]`` for every bar, ``nan`` where the window is undefined.

    ``nan`` where there are not enough prior bars, and where the denominator is
    not finite or not strictly positive — a non-positive close is not a price
    and the sign of a ratio through it means nothing.
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


def return_signs(bars: Sequence, *, lookback: int = LOOKBACK,
                 skip: int = SKIP) -> np.ndarray:
    """``sign[t]`` as ``+1 / -1 / 0``, with ``0`` also where ``ret`` is ``nan``.

    Undefined and exactly-flat are both encoded as ``0`` because the flip rule
    treats them identically: neither is a sign, and neither resets the state.
    Keeping them distinct would invite a future edit to make one of them a flip.
    """
    ret = momentum_returns(bars, lookback=lookback, skip=skip)
    signs = np.zeros(ret.size, dtype=np.int8)
    signs[np.isfinite(ret) & (ret > 0.0)] = 1
    signs[np.isfinite(ret) & (ret < 0.0)] = -1
    return signs


def previous_signs(signs: np.ndarray) -> np.ndarray:
    """For each ``t``, the latest **non-zero** sign strictly before ``t``.

    ``0`` where no such bar exists — which is the whole of the warmup and any
    prefix of zero-return bars. This is what makes zeros transparent rather than
    state-resetting: a run of them carries the previous sign across untouched.
    """
    out = np.zeros(signs.size, dtype=np.int8)
    carried = np.int8(0)
    for t in range(signs.size):
        out[t] = carried
        if signs[t] != 0:
            carried = signs[t]
    return out


def flip_setups(bars: Sequence, *, lookback: int = LOOKBACK,
                skip: int = SKIP) -> Dict[int, str]:
    """``{bar_index: LONG_SETUP | SHORT_SETUP}`` for every flip event.

    A flip at ``t`` requires all three of the intake's conditions:

    * ``sign[t] != 0`` — the bar itself has a direction;
    * a previous non-zero sign exists at all — so the first signed bar in a
      series is never a flip;
    * that previous sign differs from ``sign[t]``.

    The direction **follows** the new sign: a flip to positive is a LONG. This
    is a continuation family, and inverting it would make it a reversion family
    under a momentum name.
    """
    signs = return_signs(bars, lookback=lookback, skip=skip)
    previous = previous_signs(signs)
    setups: Dict[int, str] = {}
    for t in range(signs.size):
        current = signs[t]
        if current == 0:
            continue
        earlier = previous[t]
        if earlier == 0:
            continue              # no previous sign: not a flip, by definition
        if earlier == current:
            continue              # the state persisted; not an event
        setups[t] = LONG_SETUP if current > 0 else SHORT_SETUP
    return setups


def directed_signal_bars(
    bars: Sequence, *, warmup: int = 0, lookback: int = LOOKBACK,
    skip: int = SKIP,
) -> List[Tuple[int, str]]:
    """``[(signal_index, direction), ...]``, ordered.

    The index is the **signal** bar; the fill happens one bar later, which the
    shared barrier handles via ``entry_on="next_open"``. The last bar carries no
    setup — there is no next bar to fill on — and bars before ``warmup`` are
    excluded, matching every prior directed measurement here.
    """
    setups = flip_setups(bars, lookback=lookback, skip=skip)
    if not setups:
        return []
    last = len(bars) - 1
    return [(index, setups[index]) for index in sorted(setups)
            if warmup <= index < last]


def flags_and_directions(
    bars: Sequence, *, warmup: int = 0, lookback: int = LOOKBACK,
    skip: int = SKIP,
) -> Tuple[np.ndarray, Dict[int, str]]:
    """The same information in the shapes the instrument already speaks."""
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
