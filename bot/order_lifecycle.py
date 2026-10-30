"""The life of one order, as a state machine the store enforces.

An order's fate used to live in a running process: a variable, a log line, an
exception. If the process died after one leg filled, nothing durable said so.
This module defines the states and the legal moves between them; `persistence`
applies them atomically and keeps an append-only event log. It is pure: no I/O,
no clock, no venue.

STATES (stored in `orders.status`; the first five predate this module)

  pending                  intent recorded, nothing sent. A restart that finds
                           this knows the request never left.
  submitted                the request is about to leave / has left; no answer.
  acknowledged             the venue accepted it and gave an order id.
  partial                  some quantity filled; the rest may still fill.
  cancel_requested         a cancel was sent. NOT a cancellation: the order may
                           still fill, and a fill wins the race.
  timed_out                the request got no answer in time. NOT a rejection.
  unknown                  the outcome could not be established.
  reconciliation_required  the order's state could not be read or established
                           (UNCERTAIN). The venue's word resolves it.
  contradicted             the evidence CONTRADICTS the record: an overfill, a
                           fill on a finished or never-sent order, a reject with
                           fills. Never cleared by an ordinary event; only an
                           explicit RESOLVE_CONTRADICTION carrying the verified
                           cumulative quantity.
  filled / cancelled / rejected / never_sent / abandoned   terminal.

RULES THE MACHINE ENFORCES
  * A timeout is never a rejection; only a venue answer or proof of absence
    ends an unanswered order.
  * A sent order is never sent again: SUBMIT_ATTEMPT is legal only from pending.
  * Filled quantity only moves forward; a lower cumulative report is stale.
  * A terminal order does not change. Evidence that contradicts it (more fill
    than recorded) moves it to `contradicted` instead of being absorbed or
    ignored, and a contradiction is not an uncertainty: no ack, cancel
    confirmation, rejection or repeated fill report clears it.
  * Exposure cannot be abandoned: ABANDON is refused once anything has filled.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Optional, Tuple

PENDING = "pending"
SUBMITTED = "submitted"
ACKNOWLEDGED = "acknowledged"
PARTIAL = "partial"
FILLED = "filled"
CANCEL_REQUESTED = "cancel_requested"
CANCELLED = "cancelled"
REJECTED = "rejected"
TIMED_OUT = "timed_out"
UNKNOWN = "unknown"
RECONCILIATION_REQUIRED = "reconciliation_required"
CONTRADICTED = "contradicted"
NEVER_SENT = "never_sent"
ABANDONED = "abandoned"

ALL_STATES: Tuple[str, ...] = (
    PENDING, SUBMITTED, ACKNOWLEDGED, PARTIAL, CANCEL_REQUESTED, TIMED_OUT,
    UNKNOWN, RECONCILIATION_REQUIRED, CONTRADICTED, FILLED, CANCELLED, REJECTED,
    NEVER_SENT, ABANDONED)
TERMINAL: FrozenSet[str] = frozenset(
    {FILLED, CANCELLED, REJECTED, NEVER_SENT, ABANDONED})
UNRESOLVED: FrozenSet[str] = frozenset(ALL_STATES) - TERMINAL

# Event kinds
SUBMIT_ATTEMPT = "submit_attempt"
ACK = "ack"
FILL_REPORT = "fill_report"            # carries the CUMULATIVE filled quantity
REJECT = "reject"
TIMEOUT = "timeout"
UNKNOWN_OUTCOME = "unknown_outcome"
CANCEL_REQUEST = "cancel_request"
CANCEL_CONFIRM = "cancel_confirm"      # may carry the cumulative filled quantity
RECONCILE_REQUIRED = "reconcile_required"
NEVER_SENT_EVENT = NEVER_SENT          # the event and the state share the word
ABANDON = "abandon"
RESOLVE_CONTRADICTION = "resolve_contradiction"  # explicit, with the verified quantity
EVENT_KINDS: FrozenSet[str] = frozenset({
    SUBMIT_ATTEMPT, ACK, FILL_REPORT, REJECT, TIMEOUT, UNKNOWN_OUTCOME,
    CANCEL_REQUEST, CANCEL_CONFIRM, RECONCILE_REQUIRED, NEVER_SENT_EVENT,
    ABANDON, RESOLVE_CONTRADICTION})

#: Cumulative quantity is compared with this slack (venue rounding), relative
#: to the requested quantity, plus an absolute floor.
QTY_REL_TOL = 1e-9
QTY_ABS_TOL = 1e-12

_SENT = frozenset({SUBMITTED, ACKNOWLEDGED, PARTIAL, CANCEL_REQUESTED, TIMED_OUT,
                   UNKNOWN, RECONCILIATION_REQUIRED})
_MAYBE_LIVE = _SENT


@dataclass(frozen=True)
class Transition:
    """The verdict on one event against one state."""
    new_status: Optional[str]      # None = no change
    applied: bool                  # True when the record changes
    reason: str = ""
    new_filled_qty: Optional[float] = None


def _tol(requested: float) -> float:
    return max(QTY_ABS_TOL, QTY_REL_TOL * abs(requested))


def _no(reason: str) -> Transition:
    return Transition(None, False, reason)


def decide(state: str, kind: str, *, recorded_qty: float, requested_qty: float,
           filled_qty: Optional[float] = None) -> Transition:
    """What `kind` does to an order in `state`. Pure.

    `recorded_qty` is the cumulative fill already stored; `filled_qty` the
    cumulative quantity the event reports (for FILL_REPORT / CANCEL_CONFIRM).
    """
    if state not in ALL_STATES:
        return _no(f"ILLEGAL_TRANSITION: unknown state {state!r}")
    if kind not in EVENT_KINDS:
        return _no(f"ILLEGAL_TRANSITION: unknown event {kind!r}")
    tol = _tol(requested_qty)

    # -- cumulative quantities arrive on fill reports and cancel confirmations --
    cum: Optional[float] = None
    if kind in (FILL_REPORT, CANCEL_CONFIRM, RESOLVE_CONTRADICTION) \
            and filled_qty is not None:
        cum = float(filled_qty)
        if cum != cum or cum < 0 or cum == float("inf"):
            return _no("ILLEGAL_TRANSITION: unusable fill quantity")

    # -- a contradiction is cleared only by an explicit resolution ----------------
    if kind == RESOLVE_CONTRADICTION:
        if state != CONTRADICTED:
            return _no(f"ILLEGAL_TRANSITION: {state} is not contradicted")
        if cum is None:
            return _no("ILLEGAL_TRANSITION: a resolution needs the verified "
                       "cumulative filled quantity")
        # A correction may go DOWN (an overfill reported by mistake): it is the
        # one event allowed to, and `persistence` refuses it without written
        # evidence and logs it in full.
        final = FILLED if cum >= requested_qty - tol else CANCELLED
        return Transition(final, True, "RESOLVED: verified quantity supplied",
                          new_filled_qty=cum)
    if state == CONTRADICTED:
        if kind == FILL_REPORT:
            if (cum or 0.0) > recorded_qty + tol:
                return Transition(CONTRADICTED, True,
                                  "CONTRADICTION: more fill than the record",
                                  new_filled_qty=cum)
            return _no("STALE_FILL_REPORT" if (cum or 0.0) < recorded_qty - tol
                       else "NO_CHANGE")
        return _no("NEEDS_RESOLUTION: only resolve_contradiction clears a "
                   "contradiction")

    # -- fills -----------------------------------------------------------------------
    if state in TERMINAL and kind in (FILL_REPORT, CANCEL_CONFIRM) \
            and cum is not None and cum > recorded_qty + tol:
        # More fill than the record holds, on an order the record called
        # finished: never absorbed, never ignored.
        return Transition(CONTRADICTED, True,
                          "CONTRADICTION: fill beyond the record",
                          new_filled_qty=cum)
    if kind == FILL_REPORT:
        cum = float(cum if cum is not None else 0.0)
        if state in TERMINAL:
            return _no("STALE_FILL_REPORT" if cum < recorded_qty - tol
                       else "NO_CHANGE")
        if state == PENDING:
            # A fill on an order that was never sent cannot be real, and it
            # cannot be ignored either.
            return Transition(CONTRADICTED, True,
                              "CONTRADICTION: fill on an order never sent",
                              new_filled_qty=cum)
        if cum < recorded_qty - tol:
            return _no("STALE_FILL_REPORT")
        if cum > requested_qty + tol:
            return Transition(CONTRADICTED, True, "CONTRADICTION: overfill",
                              new_filled_qty=cum)
        if cum <= tol:
            return _no("NO_CHANGE")
        if cum >= requested_qty - tol:
            return Transition(FILLED, True, new_filled_qty=cum)
        if abs(cum - recorded_qty) <= tol and state == PARTIAL:
            return _no("NO_CHANGE")
        # A cancel in flight stays a cancel in flight until the venue confirms.
        return Transition(CANCEL_REQUESTED if state == CANCEL_REQUESTED else PARTIAL,
                          True, new_filled_qty=cum)

    # -- everything else on a finished order is inert -----------------------------
    if state in TERMINAL:
        return _no("TERMINAL")

    if kind == SUBMIT_ATTEMPT:
        if state == PENDING:
            return Transition(SUBMITTED, True)
        return _no(f"ILLEGAL_TRANSITION: {state} -> submit_attempt "
                   "(a sent order is never sent again)")

    if kind == ACK:
        if state in (SUBMITTED, TIMED_OUT, UNKNOWN, RECONCILIATION_REQUIRED):
            return Transition(ACKNOWLEDGED, True)
        if state in (ACKNOWLEDGED, PARTIAL, CANCEL_REQUESTED):
            return _no("NO_CHANGE")
        return _no(f"ILLEGAL_TRANSITION: {state} -> ack")

    if kind == REJECT:
        if recorded_qty > tol:
            return Transition(CONTRADICTED, True,
                              "CONTRADICTION: rejected with fills")
        if state in (PENDING, SUBMITTED, TIMED_OUT, UNKNOWN,
                     RECONCILIATION_REQUIRED, ACKNOWLEDGED):
            return Transition(REJECTED, True)
        return _no(f"ILLEGAL_TRANSITION: {state} -> reject")

    if kind == TIMEOUT:
        if state == SUBMITTED:
            return Transition(TIMED_OUT, True)
        return _no(f"ILLEGAL_TRANSITION: {state} -> timeout")

    if kind == UNKNOWN_OUTCOME:
        if state in (SUBMITTED, TIMED_OUT):
            return Transition(UNKNOWN, True)
        if state in (ACKNOWLEDGED, PARTIAL, CANCEL_REQUESTED):
            return Transition(RECONCILIATION_REQUIRED, True,
                              "the order exists but its state cannot be read")
        return _no(f"ILLEGAL_TRANSITION: {state} -> unknown_outcome")

    if kind == RECONCILE_REQUIRED:
        if state == RECONCILIATION_REQUIRED:
            return _no("NO_CHANGE")
        return Transition(RECONCILIATION_REQUIRED, True)

    if kind == CANCEL_REQUEST:
        if state in (ACKNOWLEDGED, PARTIAL, TIMED_OUT, UNKNOWN,
                     RECONCILIATION_REQUIRED, SUBMITTED):
            return Transition(CANCEL_REQUESTED, True)
        if state == CANCEL_REQUESTED:
            return _no("NO_CHANGE")
        return _no(f"ILLEGAL_TRANSITION: {state} -> cancel_request "
                   "(nothing was sent to cancel)")

    if kind == CANCEL_CONFIRM:
        cum = recorded_qty if cum is None else cum
        if cum < recorded_qty - tol:
            return _no("STALE_FILL_REPORT")
        if state in _MAYBE_LIVE:
            if cum >= requested_qty - tol:
                return Transition(FILLED, True, "the cancel lost to the fill",
                                  new_filled_qty=cum)
            return Transition(CANCELLED, True, new_filled_qty=cum)
        return _no(f"ILLEGAL_TRANSITION: {state} -> cancel_confirm")

    if kind == NEVER_SENT_EVENT:
        if recorded_qty > tol:
            return _no("ILLEGAL_TRANSITION: an order with fills was sent")
        return Transition(NEVER_SENT, True)

    if kind == ABANDON:
        if recorded_qty > tol:
            return _no("ILLEGAL_TRANSITION: exposure cannot be abandoned")
        return Transition(ABANDONED, True)

    return _no(f"ILLEGAL_TRANSITION: {state} -> {kind}")
