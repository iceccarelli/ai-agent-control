"""A close that survives partial fills, restarts, duplicates and ambiguity.

STATUS: a pure planner. It reads observations and a durable attempt log and says
what the NEXT safe step is. It sends nothing, imports no client and is not wired
into the live engine. The live book still closes with one reduce-only Buy and,
on a partial fill, records the remainder and halts without retrying
(`CarryEngine._partial_close`): changing that to multi-step resume is new order
behaviour and is an owner decision. This module is what that decision would be
made against, and the deterministic fault-injection tests are its evidence.

THE FIVE FAILURES IT IS BUILT AGAINST
  partial fill      the remainder is sized from the VENUE'S CURRENT short, never
                    from a remembered number
  restart           the planner is a pure function of (durable attempt log, fresh
                    venue reads); there is no in-memory state to lose
  duplicate events  fills are merged by execution id; the plan never sums fills
  ambiguity         a response that did not arrive leaves the attempt SENT; the
                    only things allowed next are WAIT, or re-sending THAT attempt
                    under the SAME order link id (the venue accepts one order per
                    link id, so a second send cannot become a second order)
  reversal          the order is always reduce-only, never larger than the short
                    the venue reports, and a net-LONG read halts: a Buy is never
                    proposed against a position that is not short

INVARIANTS (each is a test)
  * at most one close order is ever live or proposed at a time
  * a new attempt starts only after the previous one is resolved at the venue
  * the proposed quantity never exceeds the venue's current short
  * an unreadable position proposes nothing
  * attempts are bounded; exhaustion halts for a human
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

# -- attempt states (durable) ---------------------------------------------------
INTENT = "INTENT"              # recorded before sending; the send may or may not have happened
SENT = "SENT"                  # sent; outcome not yet confirmed at the venue
FILLED = "FILLED"              # resolved: the venue says fully filled
PARTIAL = "PARTIAL"            # resolved: ended with less than requested filled
CANCELLED = "CANCELLED"        # resolved: ended with nothing filled
REJECTED = "REJECTED"          # resolved: the venue refused it (e.g. reduce-only)
RESOLVED = (FILLED, PARTIAL, CANCELLED, REJECTED)
UNRESOLVED = (INTENT, SENT)

# -- what the planner can propose -------------------------------------------------
NOOP_FLAT = "NOOP_FLAT"                    # the venue holds no short: closed
SEND = "SEND_REDUCE_ONLY"                  # a NEW attempt: qty, link_id
RESEND = "RESEND_SAME_ATTEMPT"             # the same attempt, the same link id
WAIT = "WAIT"                              # a close is live, or the read cannot be trusted yet
HALT = "HALT"                              # a human must act; nothing is sent

# -- why ---------------------------------------------------------------------------
R_FLAT = "VENUE_HOLDS_NO_SHORT"
R_SHORT_REMAINS = "SHORT_REMAINS"
R_LIVE = "CLOSE_ORDER_LIVE_AT_VENUE"
R_UNREADABLE = "POSITION_UNREADABLE"
R_NET_LONG = "VENUE_IS_NET_LONG"
R_EXHAUSTED = "ATTEMPTS_EXHAUSTED"
R_DUST = "REMAINDER_BELOW_MIN_ORDER"
R_LAG = "POSITION_READ_LAGS_FILLS"
R_UNKNOWN_ORDER = "ATTEMPT_NOT_VISIBLE_AT_VENUE"
R_ORDER_UNREADABLE = "ORDER_STATUS_UNREADABLE"
R_BAD_INPUT = "INVALID_INPUT"


@dataclass(frozen=True)
class Attempt:
    """One durable record per close attempt."""
    n: int
    link_id: str
    qty: float
    state: str
    filled_qty: float = 0.0
    exec_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class OrderObs:
    """What the venue says about one close order, looked up BY LINK ID.
    `status` is New | PartiallyFilled | Filled | Cancelled | Rejected."""
    status: str
    cum_filled: float = 0.0
    exec_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Observation:
    """Fresh venue reads. `net_qty` is signed (short is negative); None means
    the position could not be read. `orders` maps an attempt's link id to its
    OrderObs; a link id the venue does not know is simply absent, and a lookup
    that FAILED is listed in `order_read_failed` (those are different facts)."""
    net_qty: Optional[float]
    orders: Mapping[str, OrderObs] = field(default_factory=dict)
    order_read_failed: Tuple[str, ...] = ()
    #: How many consecutive times the caller has already been told to wait for a
    #: lagging position read. Held by the caller; losing it (a restart) only
    #: means waiting a little longer, never sending sooner.
    lag_waits: int = 0


@dataclass(frozen=True)
class Policy:
    max_attempts: int = 3
    qty_step: float = 0.001
    min_qty: float = 0.001
    dust: float = 1e-9
    #: A position read that still shows the pre-fill size is waited out at most
    #: this many times; after that the reduce-only order is proposed anyway (the
    #: venue refuses an oversized one, and attempts are bounded).
    max_lag_waits: int = 5


@dataclass(frozen=True)
class Plan:
    action: str
    reason: str
    qty: float = 0.0
    link_id: str = ""
    attempt: int = 0
    reduce_only: bool = True
    #: resolutions the caller must persist before acting: ((n, state, filled_qty, exec_ids), ...)
    resolve: Tuple[Tuple[int, str, float, Tuple[str, ...]], ...] = ()


LINK_ID_MAX = 36
_LINK_OK = re.compile(r"^[A-Za-z0-9_-]{1,36}$")


def valid_link_id(link_id: str) -> bool:
    """Bybit's orderLinkId rule: 1-36 characters of letters, digits, dashes and
    underscores. (Colons and dots are not allowed.)"""
    return bool(_LINK_OK.match(str(link_id)))


def link_id_for(cycle_id: str, n: int) -> str:
    """Deterministic and venue-legal: the same cycle and attempt number always
    name the same order, so a re-send after a restart cannot create a second
    one. A cycle id that would not fit is replaced by a fixed-length digest of
    itself (still a pure function of the cycle id)."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", str(cycle_id))
    link = f"CL-{safe}-{int(n)}"
    if len(link) > LINK_ID_MAX:
        digest = hashlib.sha256(str(cycle_id).encode()).hexdigest()[:16]
        link = f"CL-{digest}-{int(n)}"
    return link


def floor_to_step(qty: float, step: float) -> float:
    if not (isinstance(qty, (int, float)) and math.isfinite(qty)) or qty <= 0 or step <= 0:
        return 0.0
    lots = math.floor(round(qty / step, 9))
    return round(lots * step, 12)


def merge_fills(known: Sequence[str], events: Sequence[Tuple[str, float]],
                prior_filled: float = 0.0) -> Tuple[float, Tuple[str, ...]]:
    """Fold execution events into an attempt's fill total, ONCE per execution id.

    `events` are (exec_id, qty) pairs as delivered, possibly repeated and out of
    order. `prior_filled` is what the attempt already recorded for `known` ids.
    A repeated id adds nothing; an event with no id is refused rather than
    guessed at (an id-less fill cannot be told from its own duplicate)."""
    seen = set(known)
    total = float(prior_filled)
    for exec_id, qty in events:
        if not exec_id or not isinstance(qty, (int, float)) or not math.isfinite(qty) or qty < 0:
            raise ValueError(f"unusable execution event {exec_id!r}/{qty!r}")
        if exec_id in seen:
            continue
        seen.add(exec_id)
        total += float(qty)
    return total, tuple(sorted(seen))


def _terminal(status: str, requested: float, filled: float, dust: float) -> str:
    if status == "Rejected":
        return REJECTED
    if status == "Filled" or filled >= requested - dust:
        return FILLED
    return PARTIAL if filled > dust else CANCELLED


def plan_close(cycle_id: str, attempts: Sequence[Attempt], obs: Observation,
               policy: Policy = Policy(), *, book_short_qty: Optional[float] = None) -> Plan:
    """The next safe step for closing a short. Pure: same inputs, same answer.

    `attempts` is the durable log (any order). `obs` is read fresh for this call.
    `book_short_qty` is what the book recorded as short; it is used only to
    recognise a position read that has not caught up with fills, never to size
    an order."""
    if not cycle_id or any(a.n < 1 for a in attempts):
        return Plan(HALT, R_BAD_INPUT)
    log = sorted(attempts, key=lambda a: a.n)
    if len({a.n for a in log}) != len(log) or len({a.link_id for a in log}) != len(log):
        return Plan(HALT, R_BAD_INPUT)

    # 1. The position. Unreadable proposes nothing; net long is never closed with a Buy.
    net = obs.net_qty
    if net is None or not isinstance(net, (int, float)) or not math.isfinite(net):
        return Plan(WAIT, R_UNREADABLE)
    if net > policy.dust:
        return Plan(HALT, R_NET_LONG)
    short = max(0.0, -float(net))

    # 2. Resolve what the venue has already decided about earlier attempts.
    resolve = []
    live_attempt: Optional[Attempt] = None
    ambiguous: Optional[Attempt] = None
    cum_filled = 0.0
    for a in log:
        if a.state in RESOLVED:
            cum_filled += a.filled_qty
            continue
        o = obs.orders.get(a.link_id)
        if a.link_id in obs.order_read_failed:
            return Plan(WAIT, R_ORDER_UNREADABLE, attempt=a.n, link_id=a.link_id)
        if o is None:
            ambiguous = a
            continue
        filled = max(a.filled_qty, float(o.cum_filled))
        ids = tuple(sorted(set(a.exec_ids) | set(o.exec_ids)))
        if o.status in ("New", "PartiallyFilled"):
            live_attempt = a
            continue
        state = _terminal(o.status, a.qty, filled, policy.dust)
        resolve.append((a.n, state, filled, ids))
        cum_filled += filled
    resolve_t = tuple(resolve)

    # 3. A close order is live: never place another beside it.
    if live_attempt is not None:
        return Plan(WAIT, R_LIVE, attempt=live_attempt.n,
                    link_id=live_attempt.link_id, resolve=resolve_t)

    # 4. An attempt the venue does not know. It may never have landed, or the
    #    lookup may lag. The only thing proposed is the SAME order under the SAME
    #    link id: the venue accepts one order per link id, so this cannot add a
    #    second order, and the quantity is clamped to the short actually held.
    if ambiguous is not None:
        if short <= policy.dust:
            # Nothing left to close; the attempt is moot.
            return Plan(NOOP_FLAT, R_FLAT, resolve=resolve_t
                        + ((ambiguous.n, CANCELLED, ambiguous.filled_qty, ambiguous.exec_ids),))
        qty = floor_to_step(min(ambiguous.qty, short), policy.qty_step)
        if qty < policy.min_qty:
            return Plan(HALT, R_DUST, resolve=resolve_t)
        return Plan(RESEND, R_UNKNOWN_ORDER, qty=qty, link_id=ambiguous.link_id,
                    attempt=ambiguous.n, resolve=resolve_t)

    # 5. Everything earlier is resolved. What is left to close is what the VENUE
    #    holds now.
    if short <= policy.dust:
        return Plan(NOOP_FLAT, R_FLAT, resolve=resolve_t)
    # A position read that still shows the pre-fill size right after the fills
    # were confirmed is a lagging read, not a remainder: wait rather than send a
    # reduce-only order the venue will refuse. (A short LARGER than the book's is
    # a real excess and is closed, as the live unwind does.)
    if (book_short_qty is not None and log and cum_filled >= book_short_qty - policy.dust
            and short <= book_short_qty + policy.dust
            and obs.lag_waits < policy.max_lag_waits):
        return Plan(WAIT, R_LAG, resolve=resolve_t)
    if len(log) >= policy.max_attempts:
        return Plan(HALT, R_EXHAUSTED, resolve=resolve_t)
    qty = floor_to_step(short, policy.qty_step)
    if qty < policy.min_qty:
        return Plan(HALT, R_DUST, resolve=resolve_t)
    n = (log[-1].n + 1) if log else 1
    return Plan(SEND, R_SHORT_REMAINS, qty=qty, link_id=link_id_for(cycle_id, n),
                attempt=n, resolve=resolve_t)


def apply_plan(attempts: Sequence[Attempt], plan: Plan) -> Tuple[Attempt, ...]:
    """The durable log after the caller persists `plan.resolve` and, for a SEND,
    the INTENT record (written BEFORE the order is sent). Pure."""
    by_n = {a.n: a for a in attempts}
    for n, state, filled, ids in plan.resolve:
        if n in by_n:
            by_n[n] = replace(by_n[n], state=state, filled_qty=filled, exec_ids=ids)
    if plan.action == SEND:
        by_n[plan.attempt] = Attempt(plan.attempt, plan.link_id, plan.qty, INTENT)
    return tuple(sorted(by_n.values(), key=lambda a: a.n))
