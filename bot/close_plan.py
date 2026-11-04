"""A close that survives partial fills, restarts, duplicates and ambiguity.

STATUS: a pure planner. It reads observations and a durable attempt log and says
what the NEXT safe step is. It sends nothing and imports no client. It is driven
by `close_executor.CloseExecutor`, which `CarryEngine` uses for an OVERLAY close
ONLY when the owner has approved every close-policy value (`close_policy`); until
then the engine still closes with one reduce-only Buy and, on a partial fill,
records the remainder and halts without retrying (`CarryEngine._partial_close`).

NOTHING HERE IS A VENUE FACT. Whether a repeated orderLinkId is rejected, and
under which conditions, has not been measured by this repository. The default is
therefore to NEVER re-send an id: an attempt the venue does not show is waited on
until the caller proves it absent (`Observation.absence_proven`), then it is
resolved NEVER_SENT and a NEW attempt under a NEW link id may follow.
Re-sending the same id is available only when the policy says so explicitly
(`Policy.resend_same_link_id`), i.e. after an operator has run the testnet
experiment and decided it is safe.

THE FIVE FAILURES IT IS BUILT AGAINST
  partial fill      the remainder is sized from the VENUE'S CURRENT short, never
                    from a remembered number
  restart           the planner is a pure function of (durable attempt log, fresh
                    venue reads); there is no in-memory state to lose
  duplicate events  fills are merged by execution id; the plan never sums fills
  ambiguity         a response that did not arrive leaves the attempt SENT; the
                    only things allowed next are WAIT until the venue shows the
                    order or the caller PROVES it never landed (then a new
                    attempt, new link id). Re-sending THAT attempt under the SAME
                    link id happens only if the policy opts in
  reversal          the order is always reduce-only, never larger than the short
                    the venue reports, and a net-LONG read halts: a Buy is never
                    proposed against a position that is not short

WHAT THE DESIGN ASSUMES ABOUT THE VENUE (read before wiring it in)
  1. (ONLY when `Policy.resend_same_link_id` is True) A second order under an
     orderLinkId that already exists is REJECTED. Bybit's docs say the id must be
     "always unique" but do not say how long, nor what the rejection looks like;
     the repo's own broker treats retCode 170141 as "the first attempt landed".
     If a venue re-accepted an id once its first order was final, a re-send could
     add a second order: the exposure is then bounded by `max_sends` per attempt
     AND every proposal is clamped to the short observed, so with reduce-only the
     position still cannot reverse. With the default (False) this assumption is
     not used at all.
  2. A reduce-only order larger than the position is refused or trimmed, never
     reversing. The docs do not say which. Every proposal is sized from the short
     just observed, so an oversize order can arise only from a stale read, which
     the lag guard waits out (bounded).
  3. An order can be looked up by link id. A lookup that lags looks like "not
     known": that is the ambiguous case (see `Observation.absence_proven`).
  4. Order statuses: only Bybit's documented ones are understood (live: New,
     PartiallyFilled, Created, Untriggered, Triggered, Active; terminal: Filled,
     Cancelled, PartiallyFilledCanceled, Rejected, Deactivated). Anything else
     WAITS and is never treated as terminal.
  The caller must PERSIST the attempt log (before sending) and should persist the
  wait counters; losing the counters on a restart only delays the halt.

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
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

# -- attempt states (durable) ---------------------------------------------------
INTENT = "INTENT"              # recorded before sending; the send may or may not have happened
SENT = "SENT"                  # sent; outcome not yet confirmed at the venue
FILLED = "FILLED"              # resolved: the venue says fully filled
PARTIAL = "PARTIAL"            # resolved: ended with less than requested filled
CANCELLED = "CANCELLED"        # resolved: ended with nothing filled
REJECTED = "REJECTED"          # resolved: the venue refused it (e.g. reduce-only)
NEVER_SENT = "NEVER_SENT"      # resolved: PROVEN absent at the venue past the acceptance window
RESOLVED = (FILLED, PARTIAL, CANCELLED, REJECTED, "MOOT", NEVER_SENT)
UNRESOLVED = (INTENT, SENT)

#: Bybit's documented order statuses. Only a DOCUMENTED terminal status resolves an
#: attempt; a documented live one blocks everything; anything else is not
#: understood and waits (bounded) rather than being guessed at.
LIVE_STATUSES = ("New", "PartiallyFilled", "Created", "Untriggered", "Triggered", "Active")
TERMINAL_STATUSES = ("Filled", "Cancelled", "PartiallyFilledCanceled", "Rejected", "Deactivated")
MOOT = "MOOT"                  # resolved: nothing was left to close; the fills are NOT known

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
R_UNPROVEN = "ATTEMPT_ABSENCE_NOT_YET_PROVEN"
R_ORDER_UNREADABLE = "ORDER_STATUS_UNREADABLE"
R_UNKNOWN_STATUS = "ORDER_STATUS_NOT_UNDERSTOOD"
R_WAIT_EXHAUSTED = "WAITED_TOO_LONG"
R_RESEND_EXHAUSTED = "RESENDS_EXHAUSTED"
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
    #: how many times THIS attempt has been sent (1 for the first send)
    sends: int = 1
    #: the venue's short when this attempt was planned (the baseline for
    #: recognising a position read that has not caught up with its fills)
    short_before: float = 0.0


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
    #: Link ids of attempts the caller has PROVEN never landed: the venue shows no
    #: such order in the realtime AND history lists, and longer has passed since
    #: the request could have left than a signed request can still be accepted
    #: (`order_recovery.absence_window_s`). Absence seen sooner than that is not
    #: proof, so the caller must not list the id.
    absence_proven: Tuple[str, ...] = ()
    #: Consecutive WAIT plans of any reason, and of the lagging-read reason, held
    #: by the caller. Losing them (a restart) only means waiting a little longer,
    #: never sending sooner.
    waits: int = 0
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
    #: Total consecutive WAITs of any reason before a human is called. There is
    #: no cancel action here: a close order that stays live, or a venue that will
    #: not answer, is a person's problem after this long.
    max_waits: int = 12
    #: Sends of ONE attempt (the first plus re-sends) before a human is called.
    max_sends: int = 3
    #: May an attempt the venue does not show be re-sent under the SAME link id?
    #: False (the default) never does it: the attempt is waited on until its
    #: absence is proven, then replaced by a NEW attempt. True relies on the
    #: venue rejecting a repeated id, which this repository has not verified.
    resend_same_link_id: bool = False


@dataclass(frozen=True)
class Plan:
    action: str
    reason: str
    qty: float = 0.0
    link_id: str = ""
    attempt: int = 0
    reduce_only: bool = True
    #: the venue's short at the time of a SEND (stored on the new attempt)
    short_seen: float = 0.0
    #: resolutions the caller must persist before acting: ((n, state, filled_qty, exec_ids), ...)
    resolve: Tuple[Tuple[int, str, float, Tuple[str, ...]], ...] = ()


LINK_ID_MAX = 36
_LINK_OK = re.compile(r"^[A-Za-z0-9_-]{1,36}$")


def valid_link_id(link_id: str) -> bool:
    """Bybit's orderLinkId rule: 1-36 characters of letters, digits, dashes and
    underscores. (Colons and dots are not allowed.)"""
    return bool(_LINK_OK.match(str(link_id)))


def link_id_for(cycle_id: str, n: int) -> str:
    """Deterministic, venue-legal and injective in practice: a lossy sanitising of
    the cycle id alone would let 'a:b' and 'a.b' name the same order, so a short
    digest of the RAW cycle id is always part of it."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", str(cycle_id))[:12]
    digest = hashlib.sha256(str(cycle_id).encode("utf-8", "surrogatepass")).hexdigest()[:8]
    return f"CL-{safe}-{digest}-{int(n)}"


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
    if status == "Deactivated":
        return CANCELLED
    if status == "Filled" or filled >= requested - dust:
        return FILLED
    return PARTIAL if filled > dust else CANCELLED


def plan_close(cycle_id: str, attempts: Sequence[Attempt], obs: Observation,
               policy: Policy = Policy(), *, book_short_qty: Optional[float] = None) -> Plan:
    """The next safe step for closing a short. Pure: same inputs, same answer.

    `attempts` is the durable log (any order). `obs` is read fresh for this call.
    `book_short_qty` is accepted for callers that have it and is NOT used: an
    order is sized from the venue's current short and nothing remembered.
    A WAIT that has gone on for `policy.max_waits` consecutive plans becomes a
    HALT: a person is called, since this module can neither cancel nor force."""
    p = _plan(cycle_id, attempts, obs, policy)
    if p.action == WAIT and obs.waits >= policy.max_waits:
        return Plan(HALT, R_WAIT_EXHAUSTED, resolve=p.resolve)
    return p


def _plan(cycle_id: str, attempts: Sequence[Attempt], obs: Observation,
          policy: Policy) -> Plan:
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
    ambiguous: List[Attempt] = []
    not_understood: Optional[Attempt] = None
    resolved: Dict[int, Tuple[float, float]] = {}      # n -> (short_before, filled)
    for a in log:
        if a.state in RESOLVED:
            resolved[a.n] = (a.short_before, a.filled_qty)
            continue
        if a.link_id in obs.order_read_failed:
            return Plan(WAIT, R_ORDER_UNREADABLE, attempt=a.n, link_id=a.link_id)
        o = obs.orders.get(a.link_id)
        if o is None:
            ambiguous.append(a)
            continue
        if o.status in LIVE_STATUSES:
            live_attempt = a
            continue
        if o.status not in TERMINAL_STATUSES:
            not_understood = a                          # never guessed at, never resolved
            continue
        filled = max(a.filled_qty, float(o.cum_filled))
        ids = tuple(sorted(set(a.exec_ids) | set(o.exec_ids)))
        resolve.append((a.n, _terminal(o.status, a.qty, filled, policy.dust), filled, ids))
        resolved[a.n] = (a.short_before, filled)
    resolve_t = tuple(resolve)

    # 3. A close order is live, or its status is not one we understand: never
    #    place another beside it.
    if live_attempt is not None:
        return Plan(WAIT, R_LIVE, attempt=live_attempt.n,
                    link_id=live_attempt.link_id, resolve=resolve_t)
    if not_understood is not None:
        return Plan(WAIT, R_UNKNOWN_STATUS, attempt=not_understood.n,
                    link_id=not_understood.link_id, resolve=resolve_t)

    # 4. An attempt the venue does not show. It may never have landed, or the
    #    lookup may lag. By default NOTHING is sent for it: it is waited on until
    #    the caller PROVES it absent, and only then is it resolved NEVER_SENT and
    #    replaced by a new attempt (new link id). Re-sending the same id happens
    #    only under `policy.resend_same_link_id`.
    if ambiguous:
        if short <= policy.dust:
            # Nothing left to close; the attempts are moot (their fills are not known).
            return Plan(NOOP_FLAT, R_FLAT, resolve=resolve_t + tuple(
                (a.n, MOOT, a.filled_qty, a.exec_ids) for a in ambiguous))
        if policy.resend_same_link_id:
            a = ambiguous[-1]
            if a.sends >= policy.max_sends:
                return Plan(HALT, R_RESEND_EXHAUSTED, attempt=a.n,
                            link_id=a.link_id, resolve=resolve_t)
            qty = floor_to_step(min(a.qty, short), policy.qty_step)
            if qty < policy.min_qty:
                return Plan(HALT, R_DUST, resolve=resolve_t)
            return Plan(RESEND, R_UNKNOWN_ORDER, qty=qty, link_id=a.link_id,
                        attempt=a.n, short_seen=short, resolve=resolve_t)
        unproven = [a for a in ambiguous
                    if a.link_id not in obs.absence_proven
                    or a.filled_qty > policy.dust or a.exec_ids]
        if unproven:
            # Includes an attempt that already recorded a fill or an execution
            # id: an order that left a trace cannot be "absent", whatever the
            # lookup says now. It waits, and the wait bound ends in a HALT.
            a = unproven[0]
            return Plan(WAIT, R_UNPROVEN, attempt=a.n, link_id=a.link_id,
                        resolve=resolve_t)
        for a in ambiguous:
            resolve.append((a.n, NEVER_SENT, 0.0, ()))
            resolved[a.n] = (a.short_before, 0.0)
        resolve_t = tuple(resolve)
        # fall through: every earlier attempt is now resolved

    # 5. Everything earlier is resolved. What is left to close is what the VENUE
    #    holds now.
    if short <= policy.dust:
        return Plan(NOOP_FLAT, R_FLAT, resolve=resolve_t)
    # A position read that shows MORE than the last attempt's own baseline says
    # should remain has not caught up with its fills (or something added to the
    # short): wait within a bound, then propose the reduce-only order anyway.
    if resolved and obs.lag_waits < policy.max_lag_waits:
        last = max(resolved)
        before, filled = resolved[last]
        if before > policy.dust and short > (before - filled) + max(policy.dust, policy.min_qty / 2):
            return Plan(WAIT, R_LAG, resolve=resolve_t)
    if len(log) >= policy.max_attempts:
        return Plan(HALT, R_EXHAUSTED, resolve=resolve_t)
    qty = floor_to_step(short, policy.qty_step)
    if qty < policy.min_qty:
        return Plan(HALT, R_DUST, resolve=resolve_t)
    n = (log[-1].n + 1) if log else 1
    return Plan(SEND, R_SHORT_REMAINS, qty=qty, link_id=link_id_for(cycle_id, n),
                attempt=n, short_seen=short, resolve=resolve_t)


def apply_plan(attempts: Sequence[Attempt], plan: Plan) -> Tuple[Attempt, ...]:
    """The durable log after the caller persists `plan.resolve` and, for a SEND,
    the INTENT record (written BEFORE the order is sent); for a RESEND the send
    count of that attempt goes up. Pure."""
    by_n = {a.n: a for a in attempts}
    for n, state, filled, ids in plan.resolve:
        if n in by_n:
            by_n[n] = replace(by_n[n], state=state, filled_qty=filled, exec_ids=ids)
    if plan.action == SEND:
        by_n[plan.attempt] = Attempt(plan.attempt, plan.link_id, plan.qty, INTENT,
                                     sends=1, short_before=plan.short_seen)
    elif plan.action == RESEND and plan.attempt in by_n:
        a = by_n[plan.attempt]
        by_n[plan.attempt] = replace(a, sends=a.sends + 1, state=INTENT)
    return tuple(sorted(by_n.values(), key=lambda a: a.n))
