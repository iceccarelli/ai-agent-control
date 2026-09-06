"""`funding_carry_fade_btc_v1` — the BTCUSDT-only funding fade, OOS-gated.

WHAT THIS MODULE IS
===================
A **product definition**, not a new trigger. The entry rule, the join, the
barrier geometry and the cost model are byte-identical to the frozen
`funding_carry_fade_v1`, and this module re-exports that family's
implementation rather than restating it.

That is deliberate and it is the whole argument for the name being honest. Two
things could have made this a retune wearing an out-of-sample costume:

* changing a constant. None is changed. `CONSTANTS` below is asserted equal to
  the frozen module's values by a test, so a future edit to either side goes
  red;
* changing the *rule*. The functions here are the frozen module's, reached
  through this module's namespace, so there is no second implementation to
  drift.

**What is genuinely new is the universe and the gate**, and both live here:

* `UNIVERSE = ("BTCUSDT",)` — a one-symbol universe, enforced by
  :func:`require_supported_symbol`, which raises on anything else. ETHUSDT and
  SOLUSDT are not measurable through this module even by accident;
* :func:`load_folds` and :func:`in_late_window` — the out-of-sample cut, read
  from the calendar locked in `artifacts/funding_carry_fade_btc_v1_folds.json`
  BEFORE this file existed. This module cannot compute a cut; it can only read
  the one that was frozen, and it verifies the file's hash against the lock log
  when asked to.

WHY A SEPARATE NAME AT ALL
==========================
`funding_carry_fade_v1` is FROZEN ABSENT (slice 56): its pre-declared rule
needed two of three symbols and one delivered. Re-running the survivor under
the old name would be reopening a freeze; re-running it under a new name with
the same full-sample gate would be the post-hoc universe narrowing that freeze
forbids.

The difference that makes this a product rather than a rescue is that **the
registering evidence is the late half of the history only**. Slice 55's BTC
97.0 / 97.5 was measured across all 1,461 bars; roughly 730 of those are now
burn-in and may not register anything. See EDGE.md §39.

WHAT THIS MODULE DOES NOT DO
============================
It computes no barrier — `skill_test.barrier_r_for_all_bars` is the one shared
implementation every number in EDGE.md depends on and it is not touched. It
does not read the funding series for any bar later than the decision bar's
close. It exposes no way to widen the universe, move the cut, or score the
early window as though it were the late one.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Dict, Sequence, Tuple

from signals import funding_carry_fade_v1 as _base

# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------

NAME = "funding_carry_fade_btc_v1"

#: The frozen multi-symbol family this product is derived from. Named in code
#: rather than only in prose so that a reader of `signals/` can see the
#: relationship without going to EDGE.md, and so a test can assert the
#: constants still agree.
DERIVED_FROM = _base.NAME

#: One symbol. The intake's universe, and the reason this module exists.
UNIVERSE: Tuple[str, ...] = ("BTCUSDT",)

# --------------------------------------------------------------------------
# constants — the intake's, identical to the frozen family's
# --------------------------------------------------------------------------

FUND_ABS = _base.FUND_ABS                  # 0.0001
STOP_ATR = _base.STOP_ATR                  # 1.5
TAKE_PROFIT_R = _base.TAKE_PROFIT_R        # 1.0
TAKE_PROFIT_ATR = _base.TAKE_PROFIT_ATR    # 1.5 == STOP_ATR * TAKE_PROFIT_R
HORIZON = _base.HORIZON                    # 5
ATR_PERIOD = _base.ATR_PERIOD              # 14
LOCKUP = _base.LOCKUP                      # 1
ROUND_TRIP_BPS = _base.ROUND_TRIP_BPS      # 25.0
ENTRY_ON = _base.ENTRY_ON                  # "next_open"

LONG_SETUP = _base.LONG_SETUP
SHORT_SETUP = _base.SHORT_SETUP

#: The single source of truth for what this product is, in one place, so that a
#: test can compare it against the frozen module and against the intake table
#: without scraping module attributes.
CONSTANTS: Dict[str, object] = {
    "FUND_ABS": FUND_ABS,
    "STOP_ATR": STOP_ATR,
    "TAKE_PROFIT_R": TAKE_PROFIT_R,
    "TAKE_PROFIT_ATR": TAKE_PROFIT_ATR,
    "HORIZON": HORIZON,
    "ATR_PERIOD": ATR_PERIOD,
    "LOCKUP": LOCKUP,
    "ROUND_TRIP_BPS": ROUND_TRIP_BPS,
    "ENTRY_ON": ENTRY_ON,
}

# --------------------------------------------------------------------------
# the rule — re-exported, not reimplemented
# --------------------------------------------------------------------------

FundingSeries = _base.FundingSeries
load_funding = _base.load_funding
close_time_ms = _base.close_time_ms
funding_at_decision = _base.funding_at_decision
funding_setups = _base.funding_setups
funding_paid_over_hold = _base.funding_paid_over_hold
funding_adjusted_net_r = _base.funding_adjusted_net_r
directed_signal_bars = _base.directed_signal_bars
flags_and_directions = _base.flags_and_directions
summary = _base.summary

# --------------------------------------------------------------------------
# the universe gate
# --------------------------------------------------------------------------


class UnsupportedSymbol(ValueError):
    """Raised when this product is pointed at anything but BTCUSDT.

    A distinct exception type rather than a bare `ValueError` so that a caller
    cannot swallow it by accident alongside a parsing error, and so a test can
    assert the specific refusal.
    """


def require_supported_symbol(symbol: str) -> str:
    """Return `symbol` if this product may be measured on it, else raise.

    The intake is explicit that ETHUSDT and SOLUSDT are "not measured, not
    registered, not optional diagnostics". Prose cannot enforce that; this can.
    Every entry point in this module and in the tools that drive it passes
    through here, so a measurement on the wrong symbol is not something a
    careless `--symbol` flag can produce.
    """
    if symbol not in UNIVERSE:
        raise UnsupportedSymbol(
            f"{NAME} is a {'/'.join(UNIVERSE)}-only product and was asked for "
            f"{symbol!r}. The intake places other symbols explicitly out of "
            f"scope — they are not measured, not registered and not "
            f"diagnostics. The multi-symbol family is "
            f"{DERIVED_FROM}, which is FROZEN ABSENT and must not be reopened.")
    return symbol


# --------------------------------------------------------------------------
# the locked out-of-sample cut
# --------------------------------------------------------------------------

FOLDS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "artifacts", "funding_carry_fade_btc_v1_folds.json")

LOCK_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "artifacts", "slice57_folds_lock.log")


def load_folds(path: str = FOLDS_PATH) -> dict:
    """Read the frozen calendar. This module cannot compute one.

    There is deliberately no `compute_folds` here. The cut was produced by
    `tools/lock_btc_folds.py` and committed before this file existed; a module
    that could re-derive it is a module that could re-derive it *after* seeing
    a count.
    """
    with open(path, encoding="utf-8") as handle:
        folds = json.load(handle)
    if folds.get("method") != "index_midpoint_50pct":
        raise ValueError(
            f"fold calendar at {path} does not use the pre-declared method; "
            f"found {folds.get('method')!r}")
    if folds.get("signal") != NAME:
        raise ValueError(
            f"fold calendar at {path} belongs to {folds.get('signal')!r}")
    return folds


def folds_sha256(path: str = FOLDS_PATH) -> str:
    """The hash of the calendar file, as the lock log recorded it."""
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def folds_are_unmodified(folds_path: str = FOLDS_PATH,
                         lock_path: str = LOCK_PATH) -> bool:
    """Does the calendar still hash to what was locked before any score existed?

    The single check that makes "the cut was frozen first" verifiable rather
    than asserted. Any edit to `t_mid` — however well-intentioned, however
    small — changes this hash and this returns False.
    """
    with open(lock_path, encoding="utf-8") as handle:
        lock = handle.read()
    return folds_sha256(folds_path) in lock


def in_late_window(bar, folds: dict) -> bool:
    """Is this decision bar inside the registering window `[t_mid, t1]`?

    Half-open at the bottom by the intake's own bracket notation: `t_mid`
    itself is LATE. The early window is `[t0, t_mid)`.
    """
    return int(bar.start_ms) >= int(folds["t_mid_ms"])


def late_window_indices(bars: Sequence, folds: dict) -> Tuple[int, ...]:
    """Indices of the bars in the registering window, in series order."""
    return tuple(i for i, bar in enumerate(bars) if in_late_window(bar, folds))


def restrict_flags_to_late(flags, bars, folds):
    """Zero every flag whose DECISION BAR falls before the cut.

    Restricting the flags rather than slicing the bar array is deliberate, and
    it is the difference between an honest OOS run and a subtly different
    strategy:

    * the ATR, the barrier and the funding join keep reading the bars that
      precede `t_mid`, exactly as they did in the full-sample run. A trade
      entered on the first late bar sees the same 14-bar ATR it would have
      seen;
    * only the *decision* is withheld. That is what "out of sample" means here
      — the rule may not be credited for trades it took before the cut, not
      that it must pretend the earlier prices never happened.

    Slicing the array would silently re-warm every indicator at `t_mid` and
    change the first several trades' geometry, which would make the OOS number
    incomparable with the full-sample one it is meant to be tested against.
    """
    import numpy as np

    restricted = np.array(flags, dtype=bool, copy=True)
    cut = int(folds["t_mid_ms"])
    for index, bar in enumerate(bars):
        if int(bar.start_ms) < cut:
            restricted[index] = False
    return restricted


# ---------------------------------------------------------------------------
# The FORWARD window. Added in slice 62; see EDGE.md §45e.
#
# Purely additive. Nothing that produced a measured number calls either of the
# two functions below, so no percentile, no control and no registered artefact
# can move because they exist. The tests assert that: the constants tuple and
# its fingerprint are unchanged, and `restrict_flags_to_late` is byte-identical
# in behaviour to what it was.
# ---------------------------------------------------------------------------
def in_forward_window(bar, folds: dict) -> bool:
    """Is this decision bar STRICTLY after the end of the measured window?

    Strict, unlike `in_late_window`, and deliberately so. `t_mid` is the OPEN
    of the registering window and belongs to it; `t1` is its CLOSE and has
    already been measured. A bar stamped exactly `t1` is the last bar of the
    cleared run, not the first bar of forward experience, and counting it as
    forward would be the slice-59 substitution — history relabelled as
    experience — in its smallest and most plausible form.
    """
    return int(bar.start_ms) > int(folds["t1_ms"])


def forward_window_indices(bars: Sequence, folds: dict) -> Tuple[int, ...]:
    """Indices of the bars strictly after `t1`, in series order."""
    return tuple(i for i, bar in enumerate(bars) if in_forward_window(bar, folds))


def restrict_flags_to_forward(flags, bars, folds):
    """Zero every flag whose DECISION BAR is at or before `t1`.

    Built the same way as `restrict_flags_to_late`, and for the same reason:
    it restricts the FLAGS, never the bar array.

    Slicing the array to the forward window would be far more damaging here
    than it was at `t_mid`, because the forward window is one bar long. A
    14-period ATR computed over one bar is not a warm indicator — it is a
    number invented out of nothing, and the geometry of every barrier derived
    from it would be fiction. Warm-up state legitimately flows from history.
    **Decisions do not.**
    """
    import numpy as np

    restricted = np.array(flags, dtype=bool, copy=True)
    cut = int(folds["t1_ms"])
    for index, bar in enumerate(bars):
        if int(bar.start_ms) <= cut:
            restricted[index] = False
    return restricted
