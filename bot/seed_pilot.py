"""Seed pilot: the decision rule for the FIRST cash cycles. Offline. Inert.

WHY THIS EXISTS
===============
Ordinary capital allocation needs ACTUAL_RECONCILED cash cycles
(`strategy_evidence`, `carry_evidence`); a book that has none can never get
them through the ordinary path. This module is the separate, narrow, human-
approved door for the first few: a strictly bounded pilot whose ONLY product is
evidence.

WHAT THIS MODULE IS
===================
A PURE decision function. Given an owner-approved `SeedPilotCharter`, the state
of the pilot and ONE requested cycle, `decide` returns ALLOW or REFUSE with every
named reason. It reads no file, no clock, no environment variable and no venue,
writes nothing, imports no broker, engine, store or config module, and has no
way to place an order. ALLOW is a statement about a request, not an action.

WHAT IT CAN NEVER DO (and the tests pin each)
=============================================
* It is not wired into any entry path, is not activated by anything, and
  nothing imports it. Using it needs a human-approved change that wires it.
* A successful pilot yields EVIDENCE ONLY. The decision has no field that
  allocates capital, promotes, satisfies a promotion-gate item or arms live
  trading (`grants_allocation`, `grants_promotion` and `grants_live` are always
  False and cannot be set); a charter that carries any such key is invalid.
  `config.is_live_authorized` stays the only authority on live arming and
  `LIVE_TRADING_ACK` is neither read nor written here.
* The charter's limits are the OWNER's. No notional, loss, duration, count,
  discovery window or drawdown figure is defined here: the only constants are
  structural CEILINGS that force a charter to expire and stay bounded.

FAIL CLOSED
===========
ALLOW needs EVERY condition below. Anything missing, unreadable, unparseable,
of the wrong type, non-finite or unknown is a REFUSE with a named reason, and
`decide` never raises: an unexpected fault is itself the reason INTERNAL_ERROR.

  charter     well-formed (exact key set), mainnet only, bounded, unexpired
  approval    the charter digest is recorded by the owner in an approvals file
              outside the repository that the loader found private (see
              `seed_pilot_io`), and the charter in hand hashes to exactly that
              digest: any edit after approval voids it
  termination no manual termination flag (an unreadable flag is a flag), no
              recorded termination (sticky: removing the flag does not revive it)
  loss        cumulative realized loss (losses only; wins never extend the
              budget) plus the open cycle's booked loss is below the charter's
              explicit limit; reaching it terminates the pilot for good
  one cycle   no other cycle open in the pilot's own log or in the book, and no
              open cycle the log does not know about
  count       fewer cycles than the charter allows (aborted attempts count)
  size        notional and planned duration within the charter's bounds
  protective  a protective-exit plan that the venue read-back verified, fresh,
              covering the notional, whose worst-case loss fits the REMAINING
              loss budget
  window      the cycle starts after the owner's discovery window (a cycle
              inside it can never be evidence and would BLOCK the evidence set);
              an unknown window refuses
  intent      the cycle's intent is already DURABLY recorded, byte-identical to
              this request. The first call for a new cycle therefore REFUSEs with
              INTENT_NOT_DURABLY_RECORDED and the record to write; the caller
              writes it (seed_pilot_io.IntentLog) and asks again. No order may
              precede that record.

The drawdown limit of the evidence policy is a QUALIFICATION threshold, not a
loss allowance; nothing here reads it as one. A charter whose loss limit exceeds
it is only WARNED about (the evidence set could be BLOCKed by a loss the pilot
itself permitted).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

ALLOW = "ALLOW"
REFUSE = "REFUSE"

SCHEMA = "seed_pilot_charter/1"
EVENT_SCHEMA = "seed_pilot_event/1"
APPROVAL_KEY_PREFIX = "seed_pilot_charter:"

# -- structural ceilings: they bound what an owner may write, they are not policy
DAY_S = 86400.0
#: A charter must expire, and not far away.
MAX_CHARTER_SPAN_S = 90 * DAY_S
#: One cycle may not be planned to outlast this (the charter's own bound is
#: usually far lower).
MAX_CYCLE_DURATION_CEILING_S = 30 * DAY_S
#: A pilot is a handful of cycles, not a programme.
MAX_CYCLES_CEILING = 100
#: A protective-exit verification older than this is not "now".
MAX_PLAN_AGE_CEILING_S = 600.0
#: A request stamped further from the caller's clock than this is not current.
MAX_REQUEST_SKEW_S = 60.0

# -- reasons -----------------------------------------------------------------------
CHARTER_MISSING = "CHARTER_MISSING"
CHARTER_INVALID = "CHARTER_INVALID"
CHARTER_NOT_YET_VALID = "CHARTER_NOT_YET_VALID"
CHARTER_EXPIRED = "CHARTER_EXPIRED"
APPROVAL_MISSING = "APPROVAL_MISSING"
APPROVAL_UNSAFE = "APPROVAL_UNSAFE"
CHARTER_NOT_APPROVED_AS_WRITTEN = "CHARTER_NOT_APPROVED_AS_WRITTEN"
CLOCK_INVALID = "CLOCK_INVALID"
REQUEST_INVALID = "REQUEST_INVALID"
REQUEST_TIME_INVALID = "REQUEST_TIME_INVALID"
REQUEST_NOT_FOR_THIS_CHARTER = "REQUEST_NOT_FOR_THIS_CHARTER"
NOT_MAINNET = "NOT_MAINNET"
NOTIONAL_ABOVE_BOUND = "NOTIONAL_ABOVE_BOUND"
DURATION_ABOVE_BOUND = "DURATION_ABOVE_BOUND"
TERMINATION_FLAG_PRESENT = "TERMINATION_FLAG_PRESENT"
TERMINATION_FLAG_UNKNOWN = "TERMINATION_FLAG_UNKNOWN"
PILOT_TERMINATED = "PILOT_TERMINATED"
INTENT_LOG_UNAVAILABLE = "INTENT_LOG_UNAVAILABLE"
INTENT_LOG_INVALID = "INTENT_LOG_INVALID"
LOG_CHARTER_DIGEST_MISMATCH = "LOG_CHARTER_DIGEST_MISMATCH"
CUMULATIVE_LOSS_LIMIT_REACHED = "CUMULATIVE_LOSS_LIMIT_REACHED"
OPEN_CYCLE_LOSS_UNKNOWN = "OPEN_CYCLE_LOSS_UNKNOWN"
BOOK_STATE_UNKNOWN = "BOOK_STATE_UNKNOWN"
SECOND_CONCURRENT_CYCLE = "SECOND_CONCURRENT_CYCLE"
UNRECORDED_OPEN_CYCLE = "UNRECORDED_OPEN_CYCLE"
MAX_CYCLES_REACHED = "MAX_CYCLES_REACHED"
INTENT_ALREADY_CONSUMED = "INTENT_ALREADY_CONSUMED"
INTENT_MISMATCH = "INTENT_MISMATCH"
INTENT_NOT_DURABLY_RECORDED = "INTENT_NOT_DURABLY_RECORDED"
CYCLE_DURATION_EXCEEDED = "CYCLE_DURATION_EXCEEDED"
PROTECTIVE_PLAN_MISSING = "PROTECTIVE_PLAN_MISSING"
PROTECTIVE_PLAN_NOT_VERIFIED = "PROTECTIVE_PLAN_NOT_VERIFIED"
PROTECTIVE_PLAN_LOSS_EXCEEDS_BUDGET = "PROTECTIVE_PLAN_LOSS_EXCEEDS_BUDGET"
DISCOVERY_WINDOW_UNKNOWN = "DISCOVERY_WINDOW_UNKNOWN"
CYCLE_WITHIN_DISCOVERY_WINDOW = "CYCLE_WITHIN_DISCOVERY_WINDOW"
INTERNAL_ERROR = "INTERNAL_ERROR"

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}\Z")


def _num(x: Any) -> Optional[float]:
    """A finite float from a real number only (bool and text are not numbers)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    v = float(x)
    return v if math.isfinite(v) else None


def _pos(x: Any) -> Optional[float]:
    v = _num(x)
    return v if v is not None and v > 0 else None


def _nonneg(x: Any) -> Optional[float]:
    v = _num(x)
    return v if v is not None and v >= 0 else None


def _text(x: Any) -> bool:
    return isinstance(x, str) and bool(x.strip())


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _sha(obj: Any) -> str:
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


# -- the charter ---------------------------------------------------------------------

@dataclass(frozen=True)
class SeedPilotCharter:
    """What the owner signs. Every field is required; there are no defaults, so
    nothing here is a value this software chose."""
    charter_id: str
    owner: str                          # who signs (informational, not identity)
    strategy_id: str
    environment: str                    # must be exactly "mainnet"
    symbol: str
    max_notional_usd: float
    max_cycle_duration_s: float
    max_cycles: int
    cumulative_loss_limit_usd: float
    not_before_s: float
    expires_at_s: float
    max_plan_age_s: float
    termination_flag_path: str          # absolute; its existence terminates
    intent_log_path: str                # absolute; the durable intent record
    protective_exit_required: bool      # must be True; there is no other value


CHARTER_FIELDS = tuple(SeedPilotCharter.__dataclass_fields__)


def charter_to_dict(c: SeedPilotCharter) -> Dict[str, Any]:
    d = {k: getattr(c, k) for k in CHARTER_FIELDS}
    d["schema"] = SCHEMA
    return d


def charter_problems(c: Any) -> List[str]:
    """Why `c` is not a usable charter (empty = well formed). Never raises."""
    if not isinstance(c, SeedPilotCharter):
        return [f"{CHARTER_INVALID}:not_a_SeedPilotCharter"]
    p: List[str] = []

    def bad(name: str) -> None:
        p.append(f"{CHARTER_INVALID}:{name}")

    for name in ("charter_id", "strategy_id"):
        if not (isinstance(getattr(c, name), str) and _ID.match(getattr(c, name))):
            bad(name)
    if not _text(c.owner):
        bad("owner")
    if c.environment != "mainnet":
        p.append(f"{NOT_MAINNET}:{c.environment!r}")
    if not (_text(c.symbol) and c.symbol == c.symbol.strip().upper()):
        bad("symbol")
    notional = _pos(c.max_notional_usd)
    if notional is None:
        bad("max_notional_usd")
    dur = _pos(c.max_cycle_duration_s)
    if dur is None or dur > MAX_CYCLE_DURATION_CEILING_S:
        bad("max_cycle_duration_s")
    if (isinstance(c.max_cycles, bool) or not isinstance(c.max_cycles, int)
            or not 1 <= c.max_cycles <= MAX_CYCLES_CEILING):
        bad("max_cycles")
    loss = _pos(c.cumulative_loss_limit_usd)
    if loss is None or (notional is not None and loss > notional):
        bad("cumulative_loss_limit_usd")
    nb, ex = _pos(c.not_before_s), _pos(c.expires_at_s)
    if nb is None:
        bad("not_before_s")
    if ex is None or (nb is not None and not ex > nb):
        bad("expires_at_s")
    elif nb is not None and ex - nb > MAX_CHARTER_SPAN_S:
        bad("charter_span_exceeds_ceiling")
    age = _pos(c.max_plan_age_s)
    if age is None or age > MAX_PLAN_AGE_CEILING_S:
        bad("max_plan_age_s")
    for name in ("termination_flag_path", "intent_log_path"):
        v = getattr(c, name)
        if not (_text(v) and v == v.strip() and v.startswith("/")
                and "\x00" not in v and ".." not in v.split("/")):
            bad(name)
    if c.termination_flag_path == c.intent_log_path:
        bad("termination_flag_path_equals_intent_log_path")
    if c.protective_exit_required is not True:
        bad("protective_exit_required")
    return p


def charter_from_dict(d: Any) -> Tuple[Optional[SeedPilotCharter], List[str]]:
    """(charter, problems). The key set is EXACT: an unknown key (for example
    ``allows_live``, ``promote``, ``allocation``) is an invalid charter, never
    ignored."""
    if not isinstance(d, Mapping):
        return None, [f"{CHARTER_INVALID}:not_an_object"]
    keys = set(d)
    want = set(CHARTER_FIELDS) | {"schema"}
    if d.get("schema") != SCHEMA:
        return None, [f"{CHARTER_INVALID}:schema"]
    extra, missing = sorted(map(str, keys - want)), sorted(want - keys)
    if extra or missing:
        out = [f"{CHARTER_INVALID}:unknown_key={k}" for k in extra]
        out += [f"{CHARTER_INVALID}:missing_key={k}" for k in missing]
        return None, out
    try:
        c = SeedPilotCharter(**{k: d[k] for k in CHARTER_FIELDS})
    except TypeError:
        return None, [f"{CHARTER_INVALID}:construction"]
    probs = charter_problems(c)
    return (None, probs) if probs else (c, [])


def approval_key(charter_id: str) -> str:
    return f"{APPROVAL_KEY_PREFIX}{charter_id}"


def charter_digest(c: SeedPilotCharter) -> str:
    """SHA-256 of the charter exactly as written, in the SAME form as
    `promotion_gate.approval_digest(approval_key(id), charter_dict)` so the
    repo's approvals-file convention is the owner's one act for both (a test
    pins the two together)."""
    return _sha({"key": approval_key(c.charter_id), "entry": charter_to_dict(c)})


# -- approval ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ApprovalStatus:
    """What `seed_pilot_io` found about the owner's approval of THIS charter."""
    approved_digest: Optional[str] = None     # the digest the owner recorded
    problems: Tuple[str, ...] = ()            # why the approvals file was unusable


# -- the request and the state ------------------------------------------------------------

@dataclass(frozen=True)
class ProtectivePlan:
    """The exit that protects the cycle if everything else fails. `verified` is
    NOT a claim this module can check: it must come from a venue read-back made
    by whoever wires the pilot, and it is recorded in the intent."""
    venue_readback_verified: bool
    verification_ref: str          # identifies the read-back (order id, digest, ...)
    verified_at_s: float
    stop_price: float
    covered_notional_usd: float
    max_loss_if_triggered_usd: float


@dataclass(frozen=True)
class CycleRequest:
    intent_id: str
    charter_id: str
    strategy_id: str
    environment: str
    symbol: str
    requested_notional_usd: float
    planned_duration_s: float
    requested_at_s: float
    protective_plan: Optional[ProtectivePlan] = None


@dataclass(frozen=True)
class PilotEvent:
    """One durable fact. kind INTENT: a cycle is about to be attempted.
    kind RESULT: it ended (outcome CLOSED or ABORTED, with its net USD, losses
    negative). kind TERMINATED: the pilot ended for good."""
    kind: str
    charter_id: str
    charter_digest: str
    at_s: float
    intent_id: str = ""
    request_digest: str = ""
    notional_usd: Optional[float] = None
    outcome: str = ""
    net_usd: Optional[float] = None
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d["schema"] = EVENT_SCHEMA
        return d


EVENT_KINDS = ("INTENT", "RESULT", "TERMINATED")
OUTCOMES = ("CLOSED", "ABORTED")


@dataclass(frozen=True)
class PilotState:
    """Everything the decision reads that is not the charter or the request. A
    field left as None is UNKNOWN, and unknown refuses."""
    #: the manual termination flag exists (True), does not (False), unreadable (None)
    termination_flag_present: Optional[bool] = None
    #: the pilot's durable events, in order; None = the log was unavailable/invalid
    events: Optional[Tuple[PilotEvent, ...]] = None
    #: ids of cycles open in the live book right now; None = could not be read
    book_open_cycle_ids: Optional[Tuple[str, ...]] = None
    #: booked loss so far on the open pilot cycle (>= 0); required when one is open
    open_cycle_loss_usd: Optional[float] = None
    #: the owner's discovery window (start_s, end_s); None = unset/unknown
    discovery_window_s: Optional[Tuple[float, float]] = None
    #: the evidence policy's drawdown limit, if set (ADVISORY: a warning only)
    evidence_drawdown_limit_usd: Optional[float] = None


@dataclass(frozen=True)
class PilotDecision:
    verdict: str
    reasons: Tuple[str, ...]
    details: Mapping[str, Any]
    #: The INTENT to write durably before asking again; set only when the sole
    #: thing missing is that record.
    intent_to_record: Optional[Mapping[str, Any]] = None
    #: True when the pilot must be recorded TERMINATED (loss limit reached).
    must_record_termination: bool = False
    #: Always False, and not constructor arguments: a pilot decision is
    #: necessary for a pilot cycle, never an allocation, promotion or arming.
    grants_allocation: bool = field(default=False, init=False)
    grants_promotion: bool = field(default=False, init=False)
    grants_live: bool = field(default=False, init=False)

    @property
    def allowed(self) -> bool:
        return self.verdict == ALLOW

    def as_dict(self) -> Dict[str, Any]:
        return {"verdict": self.verdict, "reasons": list(self.reasons),
                "details": dict(self.details),
                "intent_to_record": (dict(self.intent_to_record)
                                     if self.intent_to_record else None),
                "must_record_termination": self.must_record_termination,
                "grants_allocation": False, "grants_promotion": False,
                "grants_live": False,
                "note": ("evidence only: this decision allocates nothing, "
                         "promotes nothing and arms nothing")}


def request_digest(r: CycleRequest) -> str:
    """The identity of a request, EXCLUDING times (the same cycle is asked about
    again after its intent is recorded, at a later moment)."""
    plan = r.protective_plan
    return _sha({
        "intent_id": r.intent_id, "charter_id": r.charter_id,
        "strategy_id": r.strategy_id, "environment": r.environment,
        "symbol": r.symbol, "notional": r.requested_notional_usd,
        "duration": r.planned_duration_s,
        "plan": None if not isinstance(plan, ProtectivePlan) else {
            "ref": plan.verification_ref, "stop": plan.stop_price,
            "covered": plan.covered_notional_usd,
            "max_loss": plan.max_loss_if_triggered_usd}})


# -- folding the durable events ------------------------------------------------------------

@dataclass(frozen=True)
class _Fold:
    intents: Mapping[str, PilotEvent]
    results: Mapping[str, PilotEvent]
    terminated: bool
    realized_loss_usd: float


def _fold(events: Sequence[Any], charter: SeedPilotCharter, digest: str
          ) -> Tuple[Optional[_Fold], List[str]]:
    """Derive the pilot's history, or say why the log cannot be believed."""
    intents: Dict[str, PilotEvent] = {}
    results: Dict[str, PilotEvent] = {}
    terminated = False
    loss = 0.0
    why: List[str] = []
    last_t = None
    for e in events:
        if not isinstance(e, PilotEvent) or e.kind not in EVENT_KINDS:
            return None, [f"{INTENT_LOG_INVALID}:event_shape"]
        if e.charter_id != charter.charter_id:
            return None, [f"{INTENT_LOG_INVALID}:foreign_charter_id"]
        if e.charter_digest != digest:
            return None, [f"{LOG_CHARTER_DIGEST_MISMATCH}"]
        t = _num(e.at_s)
        if t is None or (last_t is not None and t < last_t):
            return None, [f"{INTENT_LOG_INVALID}:time_not_monotonic"]
        last_t = t
        if e.kind == "INTENT":
            if not (isinstance(e.intent_id, str) and _ID.match(e.intent_id)) \
                    or e.intent_id in intents or _pos(e.notional_usd) is None \
                    or not _text(e.request_digest):
                return None, [f"{INTENT_LOG_INVALID}:intent"]
            intents[e.intent_id] = e
        elif e.kind == "RESULT":
            net = _num(e.net_usd)
            if (e.intent_id not in intents or e.intent_id in results
                    or e.outcome not in OUTCOMES or net is None):
                return None, [f"{INTENT_LOG_INVALID}:result"]
            results[e.intent_id] = e
            loss += max(0.0, -net)             # losses only; a win buys nothing
        else:
            terminated = True
    return _Fold(intents, results, terminated, loss), why


# -- the decision ------------------------------------------------------------------------------

def decide(*, charter: Any, approval: Any, state: Any, request: Any,
           now_s: Any) -> PilotDecision:
    """ALLOW or REFUSE for ONE requested pilot cycle, with every reason.
    Pure; never raises."""
    try:
        return _decide(charter, approval, state, request, now_s)
    except Exception as exc:  # noqa: BLE001 - a fault is a refusal
        return PilotDecision(REFUSE, (f"{INTERNAL_ERROR}:{type(exc).__name__}",), {})


def _decide(charter: Any, approval: Any, state: Any, request: Any, now_s: Any
            ) -> PilotDecision:
    r: List[str] = []
    det: Dict[str, Any] = {}
    if charter is None:
        return PilotDecision(REFUSE, (CHARTER_MISSING,), det)
    cprob = charter_problems(charter)
    if cprob:
        return PilotDecision(REFUSE, tuple(cprob), det)
    c: SeedPilotCharter = charter
    digest = charter_digest(c)
    det["charter_id"], det["charter_digest"] = c.charter_id, digest

    # approval ---------------------------------------------------------------
    if not isinstance(approval, ApprovalStatus):
        r.append(APPROVAL_MISSING)
    else:
        r += [f"{APPROVAL_UNSAFE}:{p}" for p in approval.problems]
        if not _text(approval.approved_digest):
            if not approval.problems:
                r.append(APPROVAL_MISSING)
        elif approval.approved_digest != digest:
            r.append(CHARTER_NOT_APPROVED_AS_WRITTEN)

    # clock and validity -----------------------------------------------------------
    now = _pos(now_s)
    if now is None:
        r.append(CLOCK_INVALID)
    else:
        if now < c.not_before_s:
            r.append(CHARTER_NOT_YET_VALID)
        if now >= c.expires_at_s:
            r.append(CHARTER_EXPIRED)

    # the request ------------------------------------------------------------------------
    if not isinstance(request, CycleRequest):
        return PilotDecision(REFUSE, tuple(r + [f"{REQUEST_INVALID}:not_a_CycleRequest"]),
                             det)
    q: CycleRequest = request
    if not (isinstance(q.intent_id, str) and _ID.match(q.intent_id)):
        r.append(f"{REQUEST_INVALID}:intent_id")
    if (q.charter_id, q.strategy_id, q.symbol) != (c.charter_id, c.strategy_id, c.symbol):
        r.append(REQUEST_NOT_FOR_THIS_CHARTER)
    if q.environment != "mainnet" or q.environment != c.environment:
        r.append(f"{NOT_MAINNET}:{q.environment!r}")
    notional = _pos(q.requested_notional_usd)
    if notional is None:
        r.append(f"{REQUEST_INVALID}:requested_notional_usd")
    elif notional > c.max_notional_usd:
        r.append(f"{NOTIONAL_ABOVE_BOUND}:{notional}>{c.max_notional_usd}")
    dur = _pos(q.planned_duration_s)
    if dur is None:
        r.append(f"{REQUEST_INVALID}:planned_duration_s")
    elif dur > c.max_cycle_duration_s:
        r.append(f"{DURATION_ABOVE_BOUND}:{dur}>{c.max_cycle_duration_s}")
    req_t = _pos(q.requested_at_s)
    if req_t is None or (now is not None and abs(now - req_t) > MAX_REQUEST_SKEW_S):
        r.append(REQUEST_TIME_INVALID)

    # state ---------------------------------------------------------------------------------
    if not isinstance(state, PilotState):
        return PilotDecision(REFUSE, tuple(r + [f"{INTENT_LOG_UNAVAILABLE}:no_state"]),
                             det)
    s: PilotState = state
    if s.termination_flag_present is True:
        r.append(TERMINATION_FLAG_PRESENT)
    elif s.termination_flag_present is not False:
        r.append(TERMINATION_FLAG_UNKNOWN)

    fold: Optional[_Fold] = None
    if s.events is None or not isinstance(s.events, (tuple, list)):
        r.append(INTENT_LOG_UNAVAILABLE)
    else:
        fold, why = _fold(s.events, c, digest)
        if fold is None:
            r += why
    try:
        my_digest = request_digest(q)
    except (TypeError, ValueError):
        my_digest = ""
        r.append(f"{REQUEST_INVALID}:unhashable")
    own: Optional[PilotEvent] = None
    remaining = None
    must_terminate = False
    open_ids: List[str] = []
    if fold is not None:
        if fold.terminated:
            r.append(PILOT_TERMINATED)
        open_ids = [i for i in fold.intents if i not in fold.results]
        loss = fold.realized_loss_usd
        if open_ids:
            ol = _nonneg(s.open_cycle_loss_usd)
            if ol is None:
                r.append(OPEN_CYCLE_LOSS_UNKNOWN)
            else:
                loss += ol
        det["cumulative_loss_usd"] = loss
        det["cumulative_loss_limit_usd"] = c.cumulative_loss_limit_usd
        remaining = c.cumulative_loss_limit_usd - loss
        if loss >= c.cumulative_loss_limit_usd:
            r.append(CUMULATIVE_LOSS_LIMIT_REACHED)
            must_terminate = True
        others = [i for i in open_ids if i != q.intent_id]
        if others:
            r.append(SECOND_CONCURRENT_CYCLE)
        if s.book_open_cycle_ids is None or not isinstance(
                s.book_open_cycle_ids, (tuple, list)):
            r.append(BOOK_STATE_UNKNOWN)
        else:
            book = [str(i) for i in s.book_open_cycle_ids]
            if [i for i in book if i != q.intent_id]:
                r.append(SECOND_CONCURRENT_CYCLE)
            if [i for i in book if i not in fold.intents]:
                r.append(UNRECORDED_OPEN_CYCLE)
        if len([i for i in fold.intents if i != q.intent_id]) >= c.max_cycles:
            r.append(MAX_CYCLES_REACHED)
        own = fold.intents.get(q.intent_id)
        if q.intent_id in fold.results:
            r.append(INTENT_ALREADY_CONSUMED)
        if own is not None:
            if own.request_digest != my_digest:
                r.append(INTENT_MISMATCH)
            started = _num(own.at_s)
            if (now is not None and started is not None
                    and now - started > c.max_cycle_duration_s):
                r.append(CYCLE_DURATION_EXCEEDED)

    # discovery window (the owner's; never defined here) --------------------------------------------
    w = s.discovery_window_s
    if not (isinstance(w, (tuple, list)) and len(w) == 2 and _num(w[0]) is not None
            and _num(w[1]) is not None and w[0] < w[1]):
        r.append(DISCOVERY_WINDOW_UNKNOWN)
    elif req_t is not None and req_t <= w[1]:
        r.append(CYCLE_WITHIN_DISCOVERY_WINDOW)

    # protective-exit plan ---------------------------------------------------------------------------
    r += _plan_reasons(c, q, now, remaining, notional)

    # advisory (never a refusal) --------------------------------------------------------------------------
    warn: List[str] = []
    dd = _pos(s.evidence_drawdown_limit_usd)
    if dd is not None and c.cumulative_loss_limit_usd > dd:
        warn.append("charter loss limit exceeds the evidence drawdown limit: a loss "
                    "the pilot permits could BLOCK the evidence it exists to produce")
    det["warnings"] = warn

    # durable intent ----------------------------------------------------------------------------------------
    if fold is not None and own is None:
        r.append(INTENT_NOT_DURABLY_RECORDED)
    reasons = tuple(dict.fromkeys(r))          # ordered, de-duplicated
    only_missing_intent = reasons == (INTENT_NOT_DURABLY_RECORDED,)
    intent = None
    if only_missing_intent:
        intent = PilotEvent(
            kind="INTENT", charter_id=c.charter_id, charter_digest=digest,
            at_s=float(now), intent_id=q.intent_id, request_digest=my_digest,
            notional_usd=float(notional)).to_dict()
    verdict = ALLOW if not reasons else REFUSE
    return PilotDecision(verdict, reasons or ("ALL_CHECKS_PASSED",), det, intent,
                         must_terminate)


def _plan_reasons(c: SeedPilotCharter, q: CycleRequest, now: Optional[float],
                  remaining: Optional[float], notional: Optional[float]
                  ) -> List[str]:
    p = q.protective_plan
    if not isinstance(p, ProtectivePlan):
        return [PROTECTIVE_PLAN_MISSING]
    out: List[str] = []

    def bad(name: str) -> None:
        out.append(f"{PROTECTIVE_PLAN_NOT_VERIFIED}:{name}")

    if p.venue_readback_verified is not True:
        bad("venue_readback_verified")
    if not _text(p.verification_ref):
        bad("verification_ref")
    v_t = _pos(p.verified_at_s)
    if v_t is None:
        bad("verified_at_s")
    elif now is not None:
        age = now - v_t
        if age < -MAX_REQUEST_SKEW_S:
            bad("verified_in_the_future")
        elif age > c.max_plan_age_s:
            bad("verification_stale")
    if _pos(p.stop_price) is None:
        bad("stop_price")
    covered = _pos(p.covered_notional_usd)
    if covered is None or (notional is not None and covered < notional):
        bad("covered_notional_usd")
    worst = _nonneg(p.max_loss_if_triggered_usd)
    if worst is None:
        bad("max_loss_if_triggered_usd")
    elif remaining is not None and worst > remaining:
        out.append(f"{PROTECTIVE_PLAN_LOSS_EXCEEDS_BUDGET}:{worst}>{remaining}")
    return out


# -- monitoring an open cycle (exits are never refused) ---------------------------------------------------------

CONTINUE = "CONTINUE"
EXIT_AND_TERMINATE = "EXIT_AND_TERMINATE"


@dataclass(frozen=True)
class MonitorResult:
    action: str
    reasons: Tuple[str, ...]
    must_record_termination: bool


def monitor(*, charter: Any, state: Any, now_s: Any, intent_id: str
            ) -> MonitorResult:
    """Should the open pilot cycle `intent_id` be wound down NOW? This only
    ADVISES; it does not exit anything and exits are never gated by the pilot.
    Anything unknown says EXIT: a cycle nobody can watch should not be held."""
    try:
        if charter_problems(charter) or not isinstance(state, PilotState):
            return MonitorResult(EXIT_AND_TERMINATE, (CHARTER_INVALID,), False)
        c: SeedPilotCharter = charter
        digest = charter_digest(c)
        r: List[str] = []
        now = _pos(now_s)
        if now is None:
            r.append(CLOCK_INVALID)
        elif now >= c.expires_at_s:
            r.append(CHARTER_EXPIRED)
        if state.termination_flag_present is True:
            r.append(TERMINATION_FLAG_PRESENT)
        elif state.termination_flag_present is not False:
            r.append(TERMINATION_FLAG_UNKNOWN)
        term = False
        if state.events is None:
            r.append(INTENT_LOG_UNAVAILABLE)
        else:
            fold, why = _fold(state.events, c, digest)
            if fold is None:
                r += why
            else:
                if fold.terminated:
                    r.append(PILOT_TERMINATED)
                loss = fold.realized_loss_usd
                if intent_id in fold.intents and intent_id not in fold.results:
                    ol = _nonneg(state.open_cycle_loss_usd)
                    if ol is None:
                        r.append(OPEN_CYCLE_LOSS_UNKNOWN)
                    else:
                        loss += ol
                    t0 = _num(fold.intents[intent_id].at_s)
                    if now is not None and t0 is not None \
                            and now - t0 > c.max_cycle_duration_s:
                        r.append(CYCLE_DURATION_EXCEEDED)
                else:
                    r.append(f"{INTENT_LOG_INVALID}:cycle_not_open")
                if loss >= c.cumulative_loss_limit_usd:
                    r.append(CUMULATIVE_LOSS_LIMIT_REACHED)
                    term = True
        return MonitorResult(EXIT_AND_TERMINATE if r else CONTINUE,
                             tuple(dict.fromkeys(r)) or ("WITHIN_CHARTER",), term)
    except Exception as exc:  # noqa: BLE001
        return MonitorResult(EXIT_AND_TERMINATE,
                             (f"{INTERNAL_ERROR}:{type(exc).__name__}",), False)
