"""Drive a reduce-only close of a perp short through the durable close log.

`close_plan` decides; this module acts, and nothing else does. One `step()`:

  1. reads the venue (signed net position; each unresolved attempt BY LINK ID,
     cross-checked against its execution rows; the proof of absence),
  2. asks `close_plan.plan_close` what is safe,
  3. commits the plan to the close log in ONE transaction (resolutions, the
     new INTENT, the wait counters, a halt reason, the closed flag),
  4. only then, and only if that commit succeeded, sends - once.

What this guarantees, each pinned by a test (tests/test_close_execution_faults):

  * an order is never sent unless its intent is already on disk; a log that
    cannot be written means nothing is sent;
  * an id is never sent twice by default (`Policy.resend_same_link_id` is off);
  * an attempt whose fate is not known is waited on, never replaced, until it is
    PROVEN absent (the venue shows nothing AND the acceptance window has passed
    since its intent was written);
  * an INTENT left by a previous process is never sent from here: a send happens
    only in the step that wrote the intent, within INTENT_TO_SEND_MAX_S;
  * an acknowledgement is not a fill: fills come from the venue's order row,
    confirmed by its execution rows;
  * "flat" is the venue's own signed position reading zero, with every earlier
    attempt resolved; and flat that no fill of ours explains is reported as such;
  * attempt count, wait counters and history live in the log, so a restart
    neither resets nor forgets them.

The kill switch is deliberately NOT consulted: a reduce-only Buy cannot add
exposure, and refusing it would leave the short open. Whatever the broker itself
refuses is honoured (and halts the close). After a HALT nothing is sent again;
a human clears it.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import close_plan as CP

logger = logging.getLogger(__name__)

#: The longest between writing an intent and putting the request on the wire.
#: The absence proof is measured from the intent time plus this margin plus the
#: transport's worst case, so a send later than this would outrun the proof; it
#: is therefore never made.
INTENT_TO_SEND_MAX_S = 5.0

FLAT, IN_PROGRESS, HALT = "FLAT", "IN_PROGRESS", "HALT"

#: Reasons raised by the executor itself (the planner has its own).
R_LOG_UNWRITABLE = "CLOSE_LOG_UNWRITABLE"
R_LOG_UNREADABLE = "CLOSE_LOG_UNREADABLE"
R_REFUSED = "CLOSE_ORDER_REFUSED"
R_OUTCOME_UNKNOWN = "CLOSE_SEND_OUTCOME_UNKNOWN"
R_STALE_INTENT = "CLOSE_SEND_ABORTED_STALE_INTENT"
R_EXEC_MISMATCH = "CLOSE_EXECUTIONS_DISAGREE_WITH_ORDER"
R_HALTED_BEFORE = "CLOSE_HALTED_EARLIER"
R_FLAT_UNEXPLAINED = "CLOSE_FLAT_WITHOUT_OUR_FILLS"
R_REVERSED = "CLOSE_REVERSED_THE_POSITION"
R_POLICY_UNAVAILABLE = "CLOSE_POLICY_UNAVAILABLE"


@dataclass(frozen=True)
class StepResult:
    status: str                       # FLAT | IN_PROGRESS | HALT
    reason: str
    action: str = ""                  # the planner's action, or "" when none ran
    filled: float = 0.0               # filled across every attempt on file
    #: link ids whose fills reached the venue but not this process's ledger
    #: booking (found by reading the venue, not returned by a send). The book
    #: needs a human reconciliation for them; the executor never books them.
    unbooked: Tuple[str, ...] = ()
    #: True when flat is explained by fills of our own orders
    explained: bool = False
    attempts: int = 0
    sent: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)
    #: the short the venue showed at the last readable look (None if unread)
    venue_short: Optional[float] = None


def _attempt(row: Dict[str, Any]) -> CP.Attempt:
    return CP.Attempt(
        n=int(row["n"]), link_id=str(row["link_id"]), qty=float(row["qty"]),
        state=str(row["state"]), filled_qty=float(row["filled_qty"] or 0.0),
        exec_ids=tuple(row["exec_ids"]), sends=int(row["sends"] or 1),
        short_before=float(row["short_before"] or 0.0))


class CloseExecutor:
    """See the module docstring. `broker` needs: get_perp_net_qty, read_order,
    get_order_executions, place_market(order_link_id=...)."""

    def __init__(self, *, broker: Any, store: Any, symbol: str,
                 policy: Optional[CP.Policy] = None, absence_window_s: float,
                 clock: Callable[[], float] = time.time,
                 refusal_types: Tuple[type, ...] = (),
                 reader: Any = None,
                 policy_factory: Optional[Callable[[], CP.Policy]] = None) -> None:
        for name, v in (("absence_window_s", absence_window_s),):
            if (isinstance(v, bool) or not isinstance(v, (int, float))
                    or not math.isfinite(v) or v <= 0):
                raise ValueError(f"{name} must be a positive finite number, "
                                 f"got {v!r}")
        if (policy is None) == (policy_factory is None):
            raise ValueError("give exactly one of policy and policy_factory")
        self.broker = broker
        #: Where reads go. The ledger wrapper hides the adapter's order reads
        #: on purpose, so a deployment passes the bare adapter here and the
        #: wrapper (which books fills) as `broker`, which only sends.
        self.reader = reader if reader is not None else broker
        self.store = store
        self.symbol = symbol
        self._policy: Optional[CP.Policy] = policy
        self._policy_factory = policy_factory
        self.absence_window_s = float(absence_window_s)
        self.clock = clock
        #: exception types the broker raises when it REFUSED before anything
        #: left (kill switch, unwritable order log): the attempt provably
        #: never went out.
        if not refusal_types:
            from carry_broker import CarryOrderRefused
            refusal_types = (CarryOrderRefused,)
        self.refusal_types = tuple(refusal_types)
        #: links whose send RETURNED a result in this process (the ledger
        #: wrapper booked it). Anything else that fills was found by reading.
        self._booked: Dict[str, float] = {}
        self._last_short: Optional[float] = None

    @property
    def policy(self) -> CP.Policy:
        if self._policy is None:
            assert self._policy_factory is not None
            self._policy = self._policy_factory()
        return self._policy

    # -- state ---------------------------------------------------------------
    def in_progress(self, cycle_id: str) -> bool:
        try:
            return str(cycle_id) in self.store.open_close_cycles()
        except Exception:  # noqa: BLE001 - unreadable log: assume in progress
            return True

    def halted_reason(self, cycle_id: str) -> str:
        return str(self.store.close_state(cycle_id).get("halted_reason") or "")

    # -- observation ----------------------------------------------------------
    def _observe(self, cycle_id: str, attempts: List[CP.Attempt], rows: Dict[int, dict],
                 state: Dict[str, Any]) -> Tuple[CP.Observation, Dict[str, str]]:
        notes: Dict[str, str] = {}
        try:
            net: Optional[float] = float(self.reader.get_perp_net_qty(self.symbol))
            if not math.isfinite(net):
                net = None
        except Exception as exc:  # noqa: BLE001
            net = None
            notes["position"] = f"unreadable: {type(exc).__name__}"
        orders: Dict[str, CP.OrderObs] = {}
        failed: List[str] = []
        proven: List[str] = []
        now = self.clock()
        for a in attempts:
            if a.state in CP.RESOLVED:
                continue
            row = rows[a.n]
            try:
                st = self.reader.read_order(self.symbol, a.link_id, "linear")
            except Exception as exc:  # noqa: BLE001
                failed.append(a.link_id)
                notes[a.link_id] = f"order unreadable: {type(exc).__name__}"
                continue
            if st is None:
                waited = now - float(row["intent_epoch"])
                if waited >= INTENT_TO_SEND_MAX_S + self.absence_window_s:
                    proven.append(a.link_id)
                else:
                    notes[a.link_id] = f"absent for {waited:.1f}s, not yet proof"
                continue
            try:
                cum = float(st["filled_qty"])
                status = str(st.get("status") or "")
                if not math.isfinite(cum) or cum < 0:
                    raise ValueError("cumulative fill is not a usable number")
                ids: Tuple[str, ...] = ()
                if cum > self.policy.dust:
                    rows_x = self.reader.get_order_executions(
                        self.symbol, order_link_id=a.link_id, product="linear")
                    events = [(str(r.get("execId") or ""), float(r.get("execQty") or 0.0))
                              for r in rows_x]
                    total, ids = CP.merge_fills((), events)
                    # The order row and its executions are two reads of one fact.
                    # If they disagree the fill is not established.
                    if abs(total - cum) > max(self.policy.dust, 1e-9):
                        failed.append(a.link_id)
                        notes[a.link_id] = (f"executions sum {total:.10g} but the "
                                            f"order says {cum:.10g}")
                        continue
                orders[a.link_id] = CP.OrderObs(status=status, cum_filled=cum,
                                                exec_ids=ids)
            except Exception as exc:  # noqa: BLE001
                failed.append(a.link_id)
                notes[a.link_id] = f"unusable order read: {type(exc).__name__}: {exc}"
        return CP.Observation(
            net_qty=net, orders=orders, order_read_failed=tuple(failed),
            absence_proven=tuple(proven), waits=int(state["waits"]),
            lag_waits=int(state["lag_waits"])), notes

    # -- one step ---------------------------------------------------------------
    def step(self, cycle_id: str) -> StepResult:
        """One observe -> plan -> persist -> (send once) cycle. Never raises
        for a venue or log failure: those become WAIT or HALT results."""
        cid = str(cycle_id)
        last = self._once(cid)
        # A send is followed by ONE more look so an ordinary fill finishes in
        # the same tick; it never sends a second time in the same step.
        if last.sent and last.status == IN_PROGRESS and last.reason != R_OUTCOME_UNKNOWN:
            follow = self._once(cid, may_send=False)
            return follow if follow.status != IN_PROGRESS or follow.action else last
        return last

    def _once(self, cid: str, *, may_send: bool = True) -> StepResult:
        try:
            state = self.store.close_state(cid)
            rows_l = self.store.close_attempts(cid)
        except Exception as exc:  # noqa: BLE001
            return StepResult(HALT, R_LOG_UNREADABLE,
                              detail={"error": f"{type(exc).__name__}: {exc}"})
        try:
            self.policy                       # resolved once; lot rules are read here
        except Exception as exc:  # noqa: BLE001
            return StepResult(HALT, R_POLICY_UNAVAILABLE, detail={
                "error": f"{type(exc).__name__}: {exc}"})
        halted = str(state.get("halted_reason") or "")
        if int(state.get("closed") or 0):
            return self._result(FLAT, "ALREADY_CLOSED", rows_l)
        if halted:
            return self._result(HALT, R_HALTED_BEFORE, rows_l,
                                detail={"halted_reason": halted})
        attempts = [_attempt(r) for r in rows_l]
        rows = {int(r["n"]): r for r in rows_l}
        obs, notes = self._observe(cid, attempts, rows, state)
        if obs.net_qty is not None:
            self._last_short = max(0.0, -float(obs.net_qty))
        plan = CP.plan_close(cid, attempts, obs, self.policy)

        if not may_send and plan.action in (CP.SEND, CP.RESEND):
            # The follow-up look after a send never starts another order.
            return self._result(IN_PROGRESS, "SEND_DEFERRED_TO_NEXT_STEP", rows_l,
                                action=plan.action)
        waits, lag = int(state["waits"]), int(state["lag_waits"])
        if plan.action == CP.WAIT:
            waits += 1
            lag = lag + 1 if plan.reason == CP.R_LAG else lag
        else:
            waits, lag = 0, 0
        halted_reason = plan.reason if plan.action == CP.HALT else ""
        closed = plan.action == CP.NOOP_FLAT
        # Net long after our own reduce-only buys is a reversal, named as such.
        if plan.action == CP.HALT and plan.reason == CP.R_NET_LONG and any(
                a.state not in (CP.NEVER_SENT,) for a in attempts):
            halted_reason = R_REVERSED

        new_attempt = None
        resend_n = None
        if plan.action == CP.SEND:
            new_attempt = {"n": plan.attempt, "link_id": plan.link_id,
                           "qty": plan.qty, "short_before": plan.short_seen}
        elif plan.action == CP.RESEND:
            resend_n = plan.attempt

        # Flat is only declared closed when the venue read is fresh and no
        # attempt is unresolved after this plan (the planner makes them MOOT).
        try:
            self.store.apply_close_step(
                cid, resolutions=[(n, s, f, list(i)) for n, s, f, i in plan.resolve],
                new_attempt=new_attempt, resend_n=resend_n, waits=waits,
                lag_waits=lag, halted_reason=halted_reason, closed=closed,
                now=self.clock())
        except Exception as exc:  # noqa: BLE001 - nothing may be sent
            logger.critical("close %s: the log could not be written (%s: %s); "
                            "nothing is sent", cid, type(exc).__name__, exc)
            return StepResult(HALT, R_LOG_UNWRITABLE, action=plan.action,
                              detail={"error": f"{type(exc).__name__}: {exc}"})

        try:
            after = self.store.close_attempts(cid)
        except Exception as exc:  # noqa: BLE001
            return StepResult(HALT, R_LOG_UNREADABLE, action=plan.action,
                              detail={"error": f"{type(exc).__name__}: {exc}"})

        if plan.action == CP.NOOP_FLAT:
            return self._result(FLAT, plan.reason, after, action=plan.action,
                                detail={"notes": notes})
        if plan.action == CP.HALT:
            return self._result(HALT, halted_reason, after, action=plan.action,
                                detail={"notes": notes, "planner_reason": plan.reason})
        if plan.action == CP.WAIT:
            return self._result(IN_PROGRESS, plan.reason, after, action=plan.action,
                                detail={"notes": notes, "waits": waits})
        return self._send(cid, plan, after)

    def _send(self, cid: str, plan: CP.Plan, after: List[dict]) -> StepResult:
        row = next((r for r in after if r["link_id"] == plan.link_id), None)
        if row is None or str(row["state"]) != "INTENT":
            return self._result(HALT, R_LOG_UNREADABLE, after, action=plan.action,
                                detail={"why": "the intent just written is not on file"})
        age = self.clock() - float(row["intent_epoch"])
        if age > INTENT_TO_SEND_MAX_S or age < -1.0:
            # Left unsent. Its absence is proven by the clock like any other.
            return self._result(IN_PROGRESS, R_STALE_INTENT, after, action=plan.action,
                                detail={"intent_age_s": age})
        try:
            result = self.broker.place_market(
                symbol=self.symbol, side="Buy", qty=plan.qty, product="linear",
                order_link_id=plan.link_id)
        except self.refusal_types as exc:
            # The broker refused before anything left: the attempt provably
            # never existed. It still counts against the attempt budget.
            return self._refused(cid, plan, exc)
        except ValueError as exc:
            return self._refused(cid, plan, exc)
        except Exception as exc:  # noqa: BLE001 - the outcome is in doubt
            self._mark_sent(cid, plan.attempt)
            logger.error("close %s attempt %d: send outcome unknown (%s: %s)",
                         cid, plan.attempt, type(exc).__name__, exc)
            return self._result(IN_PROGRESS, R_OUTCOME_UNKNOWN,
                                self._safe_attempts(cid, after), action=plan.action,
                                sent=plan.link_id,
                                detail={"error": f"{type(exc).__name__}: {exc}"})
        self._mark_sent(cid, plan.attempt)
        if isinstance(result, dict) and result.get("filled_qty") is not None:
            # The ledger wrapper booked this fill when it returned. That is the
            # only way a close fill is ever booked.
            self._booked[plan.link_id] = float(result.get("filled_qty") or 0.0)
        return self._result(IN_PROGRESS, "SENT", self._safe_attempts(cid, after),
                            action=plan.action, sent=plan.link_id)

    def _refused(self, cid: str, plan: CP.Plan, exc: BaseException) -> StepResult:
        reason = R_REFUSED
        try:
            self.store.apply_close_step(
                cid, resolutions=[(plan.attempt, CP.NEVER_SENT, 0.0, [])],
                waits=0, lag_waits=0, halted_reason=reason, now=self.clock())
        except Exception as write_exc:  # noqa: BLE001
            logger.critical("close %s: refusal could not be recorded (%s)", cid, write_exc)
        return self._result(HALT, reason, self._safe_attempts(cid, []),
                            action=plan.action,
                            detail={"error": f"{type(exc).__name__}: {exc}"})

    def _mark_sent(self, cid: str, n: int) -> None:
        try:
            self.store.mark_close_sent(cid, n, now=self.clock())
        except Exception:  # noqa: BLE001 - INTENT is also treated as ambiguous
            logger.error("close %s attempt %d: SENT stamp not written", cid, n)

    def _safe_attempts(self, cid: str, fallback: List[dict]) -> List[dict]:
        try:
            return self.store.close_attempts(cid)
        except Exception:  # noqa: BLE001
            return fallback

    def _result(self, status: str, reason: str, rows: List[dict], *, action: str = "",
                sent: Optional[str] = None,
                detail: Optional[Dict[str, Any]] = None) -> StepResult:
        filled = sum(float(r["filled_qty"] or 0.0) for r in rows)
        unknown = [r for r in rows if r["state"] == CP.MOOT]
        unbooked = tuple(
            r["link_id"] for r in rows
            if r["state"] in CP.RESOLVED and r["state"] != CP.NEVER_SENT
            and (float(r["filled_qty"] or 0.0)
                 > self._booked.get(r["link_id"], 0.0) + self.policy.dust
                 or r["state"] == CP.MOOT))
        explained = filled > self.policy.dust and not unknown
        if status == FLAT and not explained:
            # Flat that none of our fills explain (or fills we cannot size) is
            # not a close we can claim.
            detail = dict(detail or {})
            detail["flat_unexplained"] = True
        return StepResult(status, reason, action=action, filled=filled,
                          unbooked=unbooked, explained=explained,
                          attempts=len(rows), sent=sent, detail=detail or {},
                          venue_short=self._last_short)
