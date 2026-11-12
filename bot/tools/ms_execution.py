"""ms_execution: the order-group LIFECYCLE state machine, durably journalled (pure; no venue client, no clock, no network).

States (ms_types.GroupState): PLANNED -> RISK_APPROVED -> SUBMITTED -> PARTIAL -> HEDGED | UNHEDGED | UNKNOWN_OUTCOME -> EXITING -> CLOSED | FAILED | HALTED.

Design rules (each has a test):
* WRITE-AHEAD JOURNAL. Every fact is appended to a hash-chained, append-only store BEFORE it takes effect; the in-memory state is a pure fold of the journal, so a restart
  rebuilds exactly the same groups. An order INTENT is journalled before ``send_order`` is called; the idempotency key sent as orderLinkId is ``group_id-leg_id``.
* NEVER BLIND RESEND. A missing/raised ack (or an intent with no ack found after a restart) makes the leg UNKNOWN and the group UNKNOWN_OUTCOME. Only ``reconcile`` (an injected
  ``venue_state()`` read) can resolve it: an order found under the link id is ADOPTED; only a COMPLETE read that shows no order, after ``resend_after_ms``, authorises ONE
  resend with the same link id.
* Fills are deduplicated by fill_id; duplicate acks are counted and ignored; a late fill re-opens a CLOSED/FAILED group as UNHEDGED so it is unwound, never ignored.
* FLAT means the venue's own position read says flat: CLOSED is only reached after ``venue_state()`` agrees; an unreadable position HALTS NEW EXPOSURE.
* No new exposure when: the engine is halted (risk breach, unknown venue position, position mismatch, unattributed fill), account/market data is stale or unknown, the symbol is
  halted or its funding interval changed. Risk-REDUCING (reduce-only unwind / exit) orders are still allowed.
* Recovery of an unhedged group is a reduce-only TAKER unwind of the excess filled leg (after cancelling the unfilled maker leg).
Order submission goes through the injected ``send_order(OrderRequest) -> Ack`` (and ``cancel_order``); in this repository state only the in-memory ``SimVenue`` below exists.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ms_types as T  # noqa: E402
from ms_types import GroupState as G  # noqa: E402

GENESIS = "00" * 32
EPS = Decimal("1e-9")
LIVE, SENT, UNKNOWN, FILLED, CANCELLED, REJECTED, NEW = "LIVE", "SENT", "UNKNOWN", "FILLED", "CANCELLED", "REJECTED", "NEW"
FEE_IN_BASE_MAX_FRACTION = Decimal("0.002")      # twice the highest spot taker fee on the published schedule (0.1%): a gross-vs-net remainder at most this large is a fee taken in the base coin, not a partial fill
_OPEN_LEG = (LIVE, SENT, UNKNOWN)


class IllegalTransition(Exception):
    pass


class SimulatedCrash(BaseException):
    """Raised by the simulated venue to model a process crash; BaseException so no ``except Exception`` in the engine can swallow it."""


# ------------------------------------------------------------------ journal

def _canon(o: Any) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


class MemoryStore:
    def __init__(self, lines: Optional[Sequence[str]] = None) -> None:
        self.lines: List[str] = list(lines or [])

    def append(self, line: str) -> None:
        self.lines.append(line)

    def read(self) -> List[str]:
        return list(self.lines)


class FileStore:
    """Append-only jsonl, flushed and fsynced per line."""

    def __init__(self, path: str) -> None:
        self.path = path

    def append(self, line: str) -> None:
        size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
        prefix = ""
        if size:                                                  # a complete last line that lost only its newline must not have the next entry glued onto it (that would read as ONE torn line)
            with open(self.path, "rb") as rb:
                rb.seek(-1, os.SEEK_END)
                prefix = "" if rb.read(1) == b"\n" else "\n"
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(prefix + line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            try:
                os.truncate(self.path, size)                      # a failed append (out of disk, I/O error) leaves NO partial line behind: the process may live on and append again
            except OSError:
                pass
            raise

    def read(self) -> List[str]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as fh:
            return [l.rstrip("\n") for l in fh if l.strip()]

    def drop_torn_tail(self, expected: Optional[str] = None) -> str:
        """Set the LAST line aside (a torn append: killed or out of disk mid-write) so the next append starts on a clean line. The text is kept in ``<journal>.torn`` BEFORE the journal is cut, and
        the cut is IN PLACE (a truncation at the start of that line): no second copy of the journal is written, because a torn append is very often a FULL DISK and a repair that needs space would
        leave the owner's emergency unwind unable to open the journal (independent re-review, finding 6). A crash between the two steps leaves the original journal and the kept text, never a
        mixture; keeping the text is best effort (when the disk refuses it, the text goes to stderr, the only copy, and the repair still proceeds).
        ``expected`` is the line the CALLER judged torn: the file is re-read here, and unless its last line is still exactly that line nothing is changed (ValueError). Under the single-writer lock that
        cannot differ; the emergency unwind's lock-broken branch runs WITHOUT it, where re-reading and dropping whatever is last could discard a good line another writer had just appended."""
        lines = self.read()
        if not lines or (expected is not None and lines[-1] != expected):
            raise ValueError("journal changed while its torn tail was being set aside: nothing was changed")
        dropped = lines[-1]
        with open(self.path, "rb") as rb:
            data = rb.read()
        tail = dropped.encode("utf-8")
        at = data.rfind(tail)
        if at < 0 or (at > 0 and data[at - 1:at] != b"\n") or data[at + len(tail):].strip(b"\r\n\t ") != b"":
            raise ValueError("journal changed while its torn tail was being set aside: the last line could not be located, nothing was changed")
        try:
            with open(self.path + ".torn", "a", encoding="utf-8") as fh:
                fh.write(dropped + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            print(f"JOURNAL_TORN_TEXT_NOT_KEPT: {type(exc).__name__}: the torn line could not be written to {os.path.basename(self.path)}.torn; it follows, and this is the only copy:", file=sys.stderr)
            print(dropped, file=sys.stderr)
        _truncate_file(self.path, at)
        try:
            fd = os.open(os.path.dirname(os.path.abspath(self.path)), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass
        return dropped


def _truncate_file(path: str, size: int) -> None:
    """Cut ``path`` to ``size`` bytes and make it durable (a seam the crash test replaces)."""
    with open(path, "r+b") as fh:
        fh.truncate(size)
        fh.flush()
        os.fsync(fh.fileno())


def verify_chain(lines: Sequence[str]) -> Tuple[bool, str]:
    prev = GENESIS
    for i, line in enumerate(lines):
        try:
            e = json.loads(line)
            h = e.pop("h")
        except (ValueError, KeyError):
            return False, f"UNPARSEABLE_AT_{i}"
        if e.get("seq") != i:
            return False, f"SEQ_GAP_AT_{i}"
        if e.get("prev") != prev:
            return False, f"CHAIN_BROKEN_AT_{i}"
        if hashlib.sha256((prev + _canon(e)).encode()).hexdigest() != h:
            return False, f"MUTATED_AT_{i}"
        prev = h
    return True, ""


class Journal:
    def __init__(self, store: Any) -> None:
        self.store = store
        lines = store.read()
        ok, why = verify_chain(lines)
        dropped: Optional[str] = None
        if not ok and lines and why == f"UNPARSEABLE_AT_{len(lines) - 1}" and verify_chain(lines[:-1])[0] and hasattr(store, "drop_torn_tail"):
            # ONLY an unparseable LAST line over a chain that verifies: a torn append. The journal is write-ahead (the line is durable, THEN the action is taken), so a line that never finished was never
            # acted on and setting it aside is safe; without this an owner's emergency `reconcile --unwind` could not even open the journal. A complete line with a wrong hash or sequence is tampering
            # and, like any damaged earlier line, stays refused.
            dropped = store.drop_torn_tail(expected=lines[-1])
            lines = lines[:-1]
            ok, why = verify_chain(lines)
        if not ok:
            raise ValueError(f"journal refused: {why}")
        self.entries: List[Dict[str, Any]] = [json.loads(l) for l in lines]
        self.head = self.entries[-1]["h"] if self.entries else GENESIS
        self.repaired_torn_tail: Optional[Dict[str, Any]] = None
        if dropped is not None:
            self._record_repair(dropped)

    def _record_repair(self, dropped: str) -> None:
        """A repair is never silent: the evidence file holds the text, and the repair itself is printed and journalled as an ANOMALY (a reviewer found the only trace was ``<journal>.torn``). This module has no
        clock, so the entry carries the last durable entry's time. Journalling is best effort: a torn append is often a full disk, and the owner's emergency unwind must still be able to open the journal."""
        kept = os.path.basename(str(getattr(self.store, "path", "journal"))) + ".torn"
        info = {"code": "TORN_TAIL_DROPPED", "dropped_chars": len(dropped), "dropped_sha16": hashlib.sha256(dropped.encode("utf-8")).hexdigest()[:16], "kept_in": kept}
        self.repaired_torn_tail = dict(info)
        print(f"JOURNAL_TORN_TAIL_DROPPED: a torn final journal line ({info['dropped_chars']} characters, sha256 {info['dropped_sha16']}) was set aside in {kept}; every earlier line verified", file=sys.stderr)
        if not self.entries:
            # the journal was ONLY the torn line: there is no durable entry to take a time from, and an entry stamped 0 would read to the lifecycle as a run that started in 1970 (a spurious
            # LIFECYCLE_EXCEEDED, re-review T1). The printed line and the kept text are the evidence.
            print(f"JOURNAL_TORN_TAIL_ANOMALY_NOT_JOURNALLED: the journal held nothing before the torn line; the line above and {kept} are the evidence", file=sys.stderr)
            return
        try:
            self.append("ANOMALY", "", int(self.entries[-1]["ts_ms"]), info)
        except OSError as exc:
            print(f"JOURNAL_TORN_TAIL_ANOMALY_NOT_JOURNALLED: {type(exc).__name__}: the line above and {kept} are the evidence", file=sys.stderr)

    def append(self, typ: str, group_id: str, ts_ms: int, body: Mapping[str, Any]) -> Dict[str, Any]:
        e = {"seq": len(self.entries), "prev": self.head, "type": typ, "group_id": group_id, "ts_ms": int(ts_ms), "body": dict(body)}
        h = hashlib.sha256((self.head + _canon(e)).encode()).hexdigest()
        line = _canon({**e, "h": h})
        self.store.append(line)                       # durable first; only then does the in-memory chain advance
        e["h"] = h
        self.entries.append(e)
        self.head = h
        return e


# ------------------------------------------------------------------ wire types

@dataclass(frozen=True)
class OrderRequest:
    """What ``send_order`` receives: the leg bound to its group, candidate and spec digest; ``link_id`` is the idempotency key (orderLinkId)."""
    group_id: str
    candidate_id: str
    spec_digest: str
    leg: T.LegPlan
    link_id: str


@dataclass(frozen=True)
class Ack:
    link_id: str
    status: str                  # ACCEPTED | REJECTED
    order_id: Optional[str] = None
    reason: str = ""


@dataclass(frozen=True)
class ExecPolicy:
    hedge_tol: float = 0.02
    max_account_age_ms: int = 60_000
    max_market_age_ms: int = 60_000
    resend_after_ms: int = 2_000
    max_resends: int = 1
    max_exit_attempts: int = 5
    exit_on_interval_change: bool = False
    recon_fee_bps: float = 5.5
    reconcile_every_step: bool = False


# ------------------------------------------------------------------ runtime (a fold of the journal)

@dataclass
class LegRuntime:
    plan: T.LegPlan
    kind: str                       # ENTRY | EXIT
    parent: Optional[str]
    link: str
    status: str = NEW
    order_id: Optional[str] = None
    filled: Decimal = Decimal(0)
    cost: Decimal = Decimal(0)      # sum qty*price
    fees: Decimal = Decimal(0)
    attempts: int = 0
    sent_ms: Optional[int] = None
    forced: Decimal = Decimal(0)    # qty removed from this entry leg by a forced close (liquidation)
    recon_qty: Decimal = Decimal(0)     # part of ``filled`` that was adopted from the order's cumulative quantity (no execution seen yet)
    recon_cost: Decimal = Decimal(0)
    recon_fees: Decimal = Decimal(0)
    reject_reason: str = ""

    @property
    def plan_qty(self) -> Decimal:
        return Decimal(self.plan.qty)

    @property
    def avg_px(self) -> Decimal:
        return self.cost / self.filled if self.filled > 0 else Decimal(0)


@dataclass
class GroupRuntime:
    plan: T.OrderGroupPlan
    state: G = G.PLANNED
    legs: Dict[str, LegRuntime] = field(default_factory=dict)
    exit_mode: Optional[str] = None
    halted_reason: Optional[str] = None
    exit_attempt_floor: int = 0          # rejected exit legs at the moment the owner's confirmed unwind lifted an exit halt: only rejections AFTER it count towards the next halt
    flat_verified: bool = False
    first_intent_ms: Optional[int] = None
    approved: bool = False
    interval_changed: bool = False
    ack_dups: int = 0
    fill_dups: int = 0
    reason: str = ""

    def entry_legs(self) -> List[LegRuntime]:
        return [l for l in self.legs.values() if l.kind == "ENTRY"]

    def exit_legs(self) -> List[LegRuntime]:
        return [l for l in self.legs.values() if l.kind == "EXIT"]


HELD_REASON = "SPOT_EXIT_HELD_PERP_NOT_CLOSED"      # the session held a spot exit behind its perp exit: not a venue rejection
#: Reasons of an exit the VENUE did not refuse: the session held it (above), or the executor could not READ something it needs before it can send (``FILTERS_UNAVAILABLE``: nothing reached the venue). Neither counts toward
#: ``max_exit_attempts`` and neither is re-sent inside the step that produced it; the session's exit window bounds how long it may last (round-10 specialist, A6: such refusals halted a group with the perp open).
UNCOUNTED_EXIT_REASONS = (HELD_REASON, "FILTERS_UNAVAILABLE")


def counts_as_rejection(l: Any) -> bool:
    return l.status == REJECTED and not str(l.reject_reason).startswith(UNCOUNTED_EXIT_REASONS)


def _poskey(category: str, symbol: str) -> str:
    return f"{category}:{symbol}"


def _sgn(side: str) -> int:
    return 1 if side == "Buy" else -1


def _D(x: Any) -> Decimal:
    return Decimal(str(x))


def _Dq(x: Any) -> Decimal:
    """A quantity/price from a float source with binary noise removed (10 dp), so 201.16 is not 201.15999999999997 and cannot round DOWN to a step below it."""
    return Decimal(str(round(float(x), 10)))


_ALLOWED = {
    G.PLANNED: {G.RISK_APPROVED, G.FAILED},
    G.RISK_APPROVED: {G.SUBMITTED, G.FAILED, G.HALTED, G.UNKNOWN_OUTCOME, G.PARTIAL, G.HEDGED, G.UNHEDGED},
    G.SUBMITTED: {G.PARTIAL, G.HEDGED, G.UNHEDGED, G.UNKNOWN_OUTCOME, G.FAILED, G.HALTED, G.EXITING},
    G.PARTIAL: {G.SUBMITTED, G.HEDGED, G.UNHEDGED, G.UNKNOWN_OUTCOME, G.FAILED, G.HALTED, G.EXITING},
    G.HEDGED: {G.EXITING, G.UNHEDGED, G.HALTED, G.UNKNOWN_OUTCOME, G.PARTIAL, G.CLOSED},
    G.UNHEDGED: {G.EXITING, G.HEDGED, G.HALTED, G.UNKNOWN_OUTCOME, G.CLOSED, G.PARTIAL, G.FAILED},
    G.UNKNOWN_OUTCOME: {G.SUBMITTED, G.PARTIAL, G.HEDGED, G.UNHEDGED, G.FAILED, G.HALTED, G.EXITING, G.CLOSED},
    G.EXITING: {G.CLOSED, G.UNHEDGED, G.HALTED, G.UNKNOWN_OUTCOME, G.HEDGED},
    G.HALTED: {G.UNHEDGED, G.EXITING, G.HEDGED, G.CLOSED, G.FAILED, G.UNKNOWN_OUTCOME, G.PARTIAL, G.SUBMITTED},
    G.CLOSED: {G.UNHEDGED, G.EXITING, G.HALTED, G.UNKNOWN_OUTCOME},          # a LATE fill only
    G.FAILED: {G.UNHEDGED, G.EXITING, G.HALTED, G.UNKNOWN_OUTCOME},
}


class ExecutionEngine:
    def __init__(self, store: Any, *, send_order: Callable[[OrderRequest], Any], cancel_order: Callable[[OrderRequest], Any], venue_state: Callable[[], Any],
                 policy: ExecPolicy = ExecPolicy(), qty_steps: Optional[Mapping[str, str]] = None) -> None:
        self.journal = Journal(store)
        self._send = send_order
        self._cancel = cancel_order
        self._venue = venue_state
        self.policy = policy
        self.qty_steps = dict(qty_steps or {})
        self.groups: Dict[str, GroupRuntime] = {}
        self.halts: Dict[str, int] = {}
        self.blocked_symbols: Dict[str, str] = {}
        self.halted_symbols: set = set()
        self.fill_ids: set = set()
        self.positions_exp: Dict[str, Decimal] = {}
        self.spot_base: Dict[str, Decimal] = {}      # spot key -> balance the client held that this engine did not buy (venue - expected), captured from a COMPLETE read at submit
        self.anomalies: List[Dict[str, Any]] = []
        for e in self.journal.entries:
            self._apply(e)
        last = self.journal.entries[-1]["ts_ms"] if self.journal.entries else 0
        for g in list(self.groups.values()):                          # restart: an intent with no ack is an UNKNOWN outcome, never a missing order
            for l in g.legs.values():
                if l.status == SENT:
                    self._emit("ACK_LOST", g.plan.group_id, last, {"leg_id": l.plan.leg_id, "why": "RESTART_NO_ACK"})
            if g.state not in (G.CLOSED, G.FAILED):
                self._settle(g, last, act=False)

    @classmethod
    def recover(cls, store: Any, now_ms: int, **kw: Any) -> "ExecutionEngine":
        eng = cls(store, **kw)
        eng.reconcile(now_ms)
        return eng

    # ---------------------------------------------------------------- reducer
    def _apply(self, e: Mapping[str, Any]) -> None:
        typ, gid, ts, b = e["type"], e["group_id"], e["ts_ms"], e["body"]
        if typ == "PLAN":
            p = b["plan"]
            plan = T.OrderGroupPlan(**{**p, "legs": tuple(T.LegPlan(**l) for l in p["legs"])})
            if gid not in self.groups:
                self.groups[gid] = GroupRuntime(plan)
            return
        if typ == "HALT_NEW":
            self.halts[b["reason"]] = ts
            return
        if typ == "RESUME_NEW":
            self.halts.pop(b["reason"], None)
            return
        if typ in ("SYMBOL_HALT", "SYMBOL_RESUME"):
            (self.halted_symbols.add if typ == "SYMBOL_HALT" else self.halted_symbols.discard)(b["symbol"])
            return
        if typ == "SYMBOL_BLOCK":
            self.blocked_symbols[b["symbol"]] = b["reason"]
            return
        if typ == "SYMBOL_UNBLOCK":
            self.blocked_symbols.pop(b["symbol"], None)
            return
        if typ == "BASELINE":
            self.spot_base[b["key"]] = _D(b["qty"])
            return
        if typ == "ANOMALY":
            self.anomalies.append({"group_id": gid, **b})
            return
        g = self.groups.get(gid)
        if g is None:
            return
        if typ == "RISK":
            g.approved = bool(b["approved"])
        elif typ == "STATE":
            g.state, g.reason = G(b["to"]), b.get("reason", "")
        elif typ == "INTENT":
            lid = b["leg_id"]
            if lid not in g.legs:
                lp = T.LegPlan(**b["leg"]) if b.get("leg") else next(x for x in g.plan.legs if x.leg_id == lid)
                g.legs[lid] = LegRuntime(lp, b["kind"], b.get("parent"), b["link"])
            l = g.legs[lid]
            l.status, l.attempts, l.sent_ms = SENT, l.attempts + 1, ts
            if g.first_intent_ms is None and l.kind == "ENTRY":
                g.first_intent_ms = ts
        elif typ == "ACK":
            l = g.legs[b["leg_id"]]
            if b["status"] == "ACCEPTED":
                l.order_id = b.get("order_id") or l.order_id
                if l.status in (SENT, UNKNOWN, NEW):
                    l.status = LIVE
            elif l.status in (SENT, UNKNOWN, NEW):
                l.status = REJECTED
                l.reject_reason = str(b.get("reason") or "")
        elif typ == "ACK_DUP":
            g.ack_dups += 1
        elif typ == "ACK_LOST":
            g.legs[b["leg_id"]].status = UNKNOWN
        elif typ == "FILL":
            f = b["fill"]
            if f["fill_id"] in self.fill_ids:
                return
            self.fill_ids.add(f["fill_id"])
            l = g.legs[f["leg_id"]]
            q, px = _Dq(f["qty"]), _Dq(f["price"])
            fee = _D(f["fee_usd"])
            if str(f["fill_id"]).startswith("recon:"):                 # adopted from the order's cumulative quantity: remembered so the real executions can replace it
                l.recon_qty += q
                l.recon_cost += q * px
                l.recon_fees += fee
            elif l.recon_qty > 0:                                      # a real execution for quantity a recon fill already booked: REPLACE that part, never add to it
                a = min(q, l.recon_qty)
                if l.plan.category == "spot" and _sgn(f["side"]) > 0 and l.recon_qty - a <= l.recon_qty * FEE_IN_BASE_MAX_FRACTION:
                    a = l.recon_qty                                    # the execution covers the adopted quantity but for a fee-sized remainder: a spot BUY's fee is taken from the coin received, so the order row's
                                                                       # cumulative quantity is GROSS and the execution NET; that remainder was never held, keeping it is phantom inventory the exit cannot sell
                share = a / l.recon_qty
                dc, df = l.recon_cost * share, l.recon_fees * share
                l.recon_qty -= a
                l.recon_cost -= dc
                l.recon_fees -= df
                l.filled -= a
                l.cost -= dc
                l.fees -= df
                k0 = _poskey(l.plan.category, l.plan.symbol)
                self.positions_exp[k0] = self.positions_exp.get(k0, Decimal(0)) - _sgn(f["side"]) * a
            l.filled += q
            l.cost += q * px
            l.fees += fee
            if l.status in (SENT, UNKNOWN, NEW):
                l.status = LIVE
            if l.filled >= l.plan_qty - EPS:
                l.status = FILLED
            k = _poskey(l.plan.category, l.plan.symbol)
            self.positions_exp[k] = self.positions_exp.get(k, Decimal(0)) + _sgn(f["side"]) * q
            g.flat_verified = False
        elif typ == "FILL_DUP":
            g.fill_dups += 1
        elif typ == "CANCEL_INTENT":
            pass
        elif typ == "CANCEL_DONE":
            l = g.legs[b["leg_id"]]
            if b["status"] == "CANCELLED" and l.status != FILLED:
                l.status = CANCELLED
            elif b["status"] in ("TOO_LATE", "LOST"):
                l.status = UNKNOWN
        elif typ == "LEG_GONE":
            l = g.legs[b["leg_id"]]
            if l.status != FILLED:
                l.status = CANCELLED
        elif typ == "EXIT_MODE":
            g.exit_mode = b["mode"]
        elif typ == "FORCED":
            l = g.legs[b["leg_id"]]
            q = _Dq(b["qty"])
            l.forced += q
            k = _poskey(l.plan.category, l.plan.symbol)
            self.positions_exp[k] = self.positions_exp.get(k, Decimal(0)) - _sgn(l.plan.side) * q
            g.flat_verified = False
        elif typ == "HALT_GROUP":
            g.halted_reason = b["reason"] or None
            if "exit_floor" in b:
                g.exit_attempt_floor = int(b["exit_floor"])
        elif typ == "FLAT_VERIFIED":
            g.flat_verified = True
        elif typ == "INTERVAL_FLAG":
            g.interval_changed = True

    def _emit(self, typ: str, gid: str, ts: int, body: Mapping[str, Any]) -> None:
        self._apply(self.journal.append(typ, gid, ts, body))

    # ---------------------------------------------------------------- public: lifecycle
    def plan_group(self, plan: T.OrderGroupPlan, now_ms: int) -> GroupRuntime:
        if plan.group_id not in self.groups:                           # idempotent: the same plan twice is one group
            self._emit("PLAN", plan.group_id, now_ms, {"plan": asdict(plan)})
        return self.groups[plan.group_id]

    def approve_group(self, group_id: str, decision: Any, now_ms: int) -> GroupState:
        g = self.groups[group_id]
        if g.state != G.PLANNED:
            return g.state
        ok = bool(getattr(decision, "approved", decision))
        self._emit("RISK", group_id, now_ms, {"approved": ok, "reasons": list(getattr(decision, "reasons", ()))})
        self._set_state(g, G.RISK_APPROVED if ok else G.FAILED, "RISK_APPROVED" if ok else "RISK_REJECTED:" + ",".join(map(str, getattr(decision, "reasons", ()))), now_ms)
        return g.state

    def new_exposure_block(self, now_ms: int, market_ts_ms: Optional[int], account_ts_ms: Optional[int]) -> Optional[str]:
        if self.halts:
            return "NEW_EXPOSURE_HALTED:" + ",".join(sorted(self.halts))
        for name, ts, age in (("ACCOUNT", account_ts_ms, self.policy.max_account_age_ms), ("MARKET", market_ts_ms, self.policy.max_market_age_ms)):
            if ts is None:
                return f"{name}_DATA_UNKNOWN"
            if now_ms - ts > age or now_ms - ts < 0:
                return f"{name}_DATA_STALE"
        return None

    def submit_group(self, group_id: str, now_ms: int, *, market_ts_ms: Optional[int], account_ts_ms: Optional[int]) -> GroupState:
        g = self.groups[group_id]
        if g.state != G.RISK_APPROVED:
            return g.state
        why = self.new_exposure_block(now_ms, market_ts_ms, account_ts_ms)
        syms = {l.symbol for l in g.plan.legs}
        if why is None and (syms & self.halted_symbols):
            why = "SYMBOL_HALTED"
        if why is None and (syms & set(self.blocked_symbols)):
            why = "SYMBOL_BLOCKED:" + ",".join(sorted(self.blocked_symbols[s] for s in syms & set(self.blocked_symbols)))
        if why:
            self._set_state(g, G.FAILED, why, now_ms)                  # never submitted: nothing to unwind
            return g.state
        self._capture_spot_baseline(g, now_ms)
        for lp in g.plan.legs:
            ok = self._send_leg(g, lp, "ENTRY", None, now_ms)
            if not ok:
                break
        self._settle(g, now_ms)
        return g.state

    def _capture_spot_baseline(self, g: GroupRuntime, ts: int) -> None:
        """A spot balance includes holdings the client already owned (the premise is BTC the client owns). Record, from a COMPLETE venue read taken before this group sends anything,
        how much of each spot balance is NOT the engine's, so that flat can later be verified on the DELTA. No complete read -> no baseline -> the group cannot close by delta."""
        keys = [_poskey(lp.category, lp.symbol) for lp in g.plan.legs if lp.category == "spot"]
        keys = [k for k in keys if self.positions_exp.get(k, Decimal(0)) == 0 or k not in self.spot_base]     # an engine-held key keeps its baseline; an idle one is re-read
        if not keys:
            return
        view = self._read_venue()
        pos = view.get("positions") if view else None
        if pos is None or not bool(view.get("complete", False)):
            return
        for k in keys:
            q = _D(pos.get(k, 0)) - self.positions_exp.get(k, Decimal(0))
            if self.spot_base.get(k) != q:
                self._emit("BASELINE", "", ts, {"key": k, "qty": str(q)})

    def on_ack(self, ack: Any, group_id: str, leg_id: str, now_ms: int) -> None:
        g = self.groups.get(group_id)
        l = g.legs.get(leg_id) if g else None
        if l is None:
            self._emit("ANOMALY", group_id, now_ms, {"code": "ACK_FOR_UNKNOWN_LEG", "leg_id": leg_id})
            return
        st, oid = getattr(ack, "status", None), getattr(ack, "order_id", None)
        if st == "ACCEPTED" and l.status in (LIVE, FILLED, CANCELLED) and (l.order_id in (None, oid)):
            self._emit("ACK_DUP", group_id, now_ms, {"leg_id": leg_id})
            return
        if st == "ACCEPTED" and l.order_id not in (None, oid):
            self._emit("ANOMALY", group_id, now_ms, {"code": "CONFLICTING_ORDER_ID", "leg_id": leg_id})
            self._emit("HALT_GROUP", group_id, now_ms, {"reason": "CONFLICTING_ORDER_ID"})
        else:
            self._emit("ACK", group_id, now_ms, {"leg_id": leg_id, "status": st, "order_id": oid, "reason": getattr(ack, "reason", "")})
        self._settle(g, now_ms)

    def on_fill(self, fill: T.Fill, now_ms: int) -> bool:
        """True when the fill was new. Duplicates (same fill_id) are counted and dropped; a fill for an unknown group/leg halts new exposure."""
        if fill.fill_id in self.fill_ids:
            if fill.group_id in self.groups:
                self._emit("FILL_DUP", fill.group_id, now_ms, {"fill_id": fill.fill_id})
            return False
        g = self.groups.get(fill.group_id)
        if g is None or fill.leg_id not in g.legs:
            self._emit("ANOMALY", fill.group_id, now_ms, {"code": "UNATTRIBUTED_FILL", "fill_id": fill.fill_id})
            self._emit("HALT_NEW", fill.group_id, now_ms, {"reason": "UNATTRIBUTED_FILL"})
            return False
        self._emit("FILL", fill.group_id, now_ms, {"fill": asdict(fill)})
        self._settle(g, now_ms)
        return True

    def on_forced_close(self, group_id: str, leg_id: str, qty: float, now_ms: int) -> None:
        """The venue closed (part of) an entry leg's position itself (liquidation). The group is re-evaluated: the other leg is now naked."""
        g = self.groups[group_id]
        self._emit("FORCED", group_id, now_ms, {"leg_id": leg_id, "qty": str(qty)})
        self._settle(g, now_ms)

    def on_order_done(self, group_id: str, leg_id: str, now_ms: int) -> None:
        """The venue says the order is finished without filling the rest (an IOC remainder, an expiry): the leg is terminal. Idempotent."""
        g = self.groups.get(group_id)
        l = g.legs.get(leg_id) if g else None
        if l is not None and l.status in (LIVE, UNKNOWN, SENT):
            self._emit("CANCEL_DONE", group_id, now_ms, {"leg_id": leg_id, "status": "CANCELLED"})
            self._settle(g, now_ms)

    def request_exit(self, group_id: str, now_ms: int, reason: str = "EXIT") -> GroupState:
        g = self.groups[group_id]
        if g.exit_mode is None and g.state not in (G.CLOSED, G.FAILED, G.PLANNED, G.RISK_APPROVED):
            self._cancel_live_entries(g, now_ms)
            self._emit("EXIT_MODE", group_id, now_ms, {"mode": "EXIT", "reason": reason})
        self._settle(g, now_ms)
        return g.state

    def clear_exit_halt(self, group_id: str, now_ms: int) -> Optional[str]:
        """The owner's CONFIRMED emergency unwind lifts the halt that repeated exit rejections raised (``EXIT_ORDERS_REJECTED``), which would otherwise stop every further exit order. The lift is
        journalled as a HALT_GROUP with an empty reason AND the number of rejected exit legs so far (``exit_floor``): only rejections after this point count towards a new halt, so the next
        step really sends exits instead of re-halting on the old count. No other halt is lifted (a conflicting order id, an instrument halt, ``NOT_FLAT_ON_VENUE`` where the engine believes
        flat while the venue is not: nothing the engine could send would be an exit, so lifting it would only look like progress). Returns the reason lifted, or None."""
        g = self.groups.get(group_id)
        if g is None or g.halted_reason != "EXIT_ORDERS_REJECTED":
            return None
        rejected = sum(1 for l in g.exit_legs() if counts_as_rejection(l))
        self._emit("HALT_GROUP", group_id, now_ms, {"reason": "", "exit_floor": rejected})
        self._settle(g, now_ms)
        return "EXIT_ORDERS_REJECTED"

    # ---------------------------------------------------------------- public: external events
    def halt_new_exposure(self, reason: str, now_ms: int) -> None:
        if reason not in self.halts:
            self._emit("HALT_NEW", "", now_ms, {"reason": reason})

    def resume_new_exposure(self, reason: str, now_ms: int) -> None:
        if reason in self.halts:
            self._emit("RESUME_NEW", "", now_ms, {"reason": reason})

    def on_risk_breach(self, reasons: Sequence[str], now_ms: int) -> None:
        """Stop adding exposure; cancel every not-yet-hedged group's live entry legs; the recovery policy then unwinds what filled."""
        self.halt_new_exposure("RISK_BREACH:" + ",".join(map(str, reasons)), now_ms)
        for g in list(self.groups.values()):
            if g.state in (G.PLANNED, G.RISK_APPROVED):
                self._set_state(g, G.FAILED, "RISK_BREACH_BEFORE_SUBMIT", now_ms)
            elif g.state not in (G.CLOSED, G.FAILED, G.HEDGED) and g.exit_mode is None:
                self._cancel_live_entries(g, now_ms)
                self._settle(g, now_ms)

    def on_instrument_halt(self, symbol: str, now_ms: int) -> None:
        self._emit("SYMBOL_HALT", "", now_ms, {"symbol": symbol})
        for g in self.groups.values():
            if g.state in (G.CLOSED, G.FAILED):
                continue
            if g.state in (G.PLANNED, G.RISK_APPROVED) and any(l.symbol == symbol for l in g.plan.legs):
                self._set_state(g, G.FAILED, f"INSTRUMENT_HALT:{symbol}", now_ms)
            elif any(l.plan.symbol == symbol for l in g.legs.values()):
                self._emit("HALT_GROUP", g.plan.group_id, now_ms, {"reason": f"INSTRUMENT_HALT:{symbol}"})
                self._settle(g, now_ms)

    def on_instrument_resume(self, symbol: str, now_ms: int) -> None:
        self._emit("SYMBOL_RESUME", "", now_ms, {"symbol": symbol})
        for g in self.groups.values():
            if g.halted_reason == f"INSTRUMENT_HALT:{symbol}":
                self._emit("HALT_GROUP", g.plan.group_id, now_ms, {"reason": ""})
                self._settle(g, now_ms)

    def on_funding_interval_change(self, symbol: str, old_min: Optional[int], new_min: Optional[int], now_ms: int) -> None:
        """Approved economics are stale: unsubmitted groups on the symbol FAIL (re-plan), the symbol takes no new entries, submitted groups are flagged (and exited if policy says)."""
        self._emit("SYMBOL_BLOCK", "", now_ms, {"symbol": symbol, "reason": f"FUNDING_INTERVAL_{old_min}_TO_{new_min}"})
        for g in list(self.groups.values()):
            if g.state in (G.CLOSED, G.FAILED) or not any(l.symbol == symbol for l in g.plan.legs):
                continue
            if g.state in (G.PLANNED, G.RISK_APPROVED):
                self._set_state(g, G.FAILED, "FUNDING_INTERVAL_CHANGED", now_ms)
                continue
            self._emit("INTERVAL_FLAG", g.plan.group_id, now_ms, {"symbol": symbol, "old": old_min, "new": new_min})
            if self.policy.exit_on_interval_change:
                self.request_exit(g.plan.group_id, now_ms, "FUNDING_INTERVAL_CHANGED")

    def clear_symbol_block(self, symbol: str, now_ms: int) -> None:
        if symbol in self.blocked_symbols:
            self._emit("SYMBOL_UNBLOCK", "", now_ms, {"symbol": symbol})

    # ---------------------------------------------------------------- the clock-driven step
    def step(self, now_ms: int, *, market_ts_ms: Optional[int] = None, account_ts_ms: Optional[int] = None) -> Dict[str, str]:
        if self.policy.reconcile_every_step or "UNKNOWN_VENUE_POSITION" in self.halts or any(l.status in (SENT, UNKNOWN) for g in self.groups.values() for l in g.legs.values()):
            self.reconcile(now_ms)
        for g in list(self.groups.values()):
            if g.state in (G.CLOSED, G.FAILED):
                continue
            if g.exit_mode is None and g.first_intent_ms is not None and now_ms - g.first_intent_ms >= g.plan.max_wait_ms:
                if any(l.status == LIVE for l in g.entry_legs()):
                    self._cancel_live_entries(g, now_ms)
            self._settle(g, now_ms)
        return {gid: g.state.value for gid, g in self.groups.items()}

    # ---------------------------------------------------------------- reconcile
    def _read_venue(self) -> Optional[Mapping[str, Any]]:
        try:
            v = self._venue()
        except Exception:
            return None
        return v if isinstance(v, Mapping) else None

    def reconcile(self, now_ms: int) -> None:
        view = self._read_venue()
        if view is None or view.get("positions") is None:
            self.halt_new_exposure("UNKNOWN_VENUE_POSITION", now_ms)
        elif "UNKNOWN_VENUE_POSITION" in self.halts and bool(view.get("complete", False)):
            self.resume_new_exposure("UNKNOWN_VENUE_POSITION", now_ms)       # a COMPLETE position read is the only thing that lifts it
        if view is None:
            return
        orders, execs = view.get("orders", {}), view.get("executions", [])
        complete = bool(view.get("complete", False))
        by_link: Dict[str, List[Mapping[str, Any]]] = {}
        for x in execs:
            by_link.setdefault(x.get("link_id", ""), []).append(x)
        for g in list(self.groups.values()):
            gid = g.plan.group_id
            for l in list(g.legs.values()):
                if l.status not in (SENT, UNKNOWN, LIVE) and not by_link.get(l.link):
                    continue
                o = orders.get(l.link)
                for x in by_link.get(l.link, []):                              # executions first: they are the fills
                    if x["fill_id"] in self.fill_ids:
                        continue
                    self.on_fill(T.Fill(x["fill_id"], gid, l.plan.leg_id, l.plan.symbol, x.get("side", l.plan.side), float(x["qty"]), float(x["price"]),
                                        float(x.get("fee_usd", 0.0)), x.get("fee_ccy", "USDT"), x.get("liquidity", "TAKER"), int(x.get("ts_ms", now_ms)), x.get("prov", "SIMULATED")), now_ms)
                if o is None:
                    if l.status in (SENT, UNKNOWN) and l.order_id is None and complete and l.sent_ms is not None and now_ms - l.sent_ms >= self.policy.resend_after_ms \
                            and l.attempts <= self.policy.max_resends and (l.kind == "EXIT" or self._entry_allowed(g)):
                        self._send_leg(g, l.plan, l.kind, l.parent, now_ms)         # the venue has NO order under this link id: one idempotent resend
                    elif l.status in (LIVE, UNKNOWN) and l.order_id is not None and complete and not by_link.get(l.link):
                        self._emit("LEG_GONE", gid, now_ms, {"leg_id": l.plan.leg_id})
                    continue
                if l.status in (SENT, UNKNOWN) and o.get("status") != "Rejected":
                    self._emit("ACK", gid, now_ms, {"leg_id": l.plan.leg_id, "status": "ACCEPTED", "order_id": o.get("order_id"), "reason": "ADOPTED_BY_RECONCILE"})
                cum = _D(o.get("filled_qty", 0))
                if cum > l.filled + EPS:                                          # the venue says more filled than we hold executions for
                    px = _D(o.get("avg_price", 0))
                    self.on_fill(T.Fill(f"recon:{l.link}:{cum}", gid, l.plan.leg_id, l.plan.symbol, l.plan.side, float(cum - l.filled), float(px),
                                        float((cum - l.filled) * px) * self.policy.recon_fee_bps / 1e4, "EST", "TAKER", now_ms, "SIMULATED"), now_ms)
                st = o.get("status")
                if st == "Rejected":
                    self._emit("ACK", gid, now_ms, {"leg_id": l.plan.leg_id, "status": "REJECTED", "order_id": None, "reason": "VENUE_REJECTED"})
                elif st in ("Cancelled", "PartiallyFilledCanceled", "Deactivated") and l.status in (LIVE, UNKNOWN, SENT):
                    self._emit("CANCEL_DONE", gid, now_ms, {"leg_id": l.plan.leg_id, "status": "CANCELLED"})
                elif st in ("New", "PartiallyFilled") and l.status == UNKNOWN:
                    self._emit("ACK", gid, now_ms, {"leg_id": l.plan.leg_id, "status": "ACCEPTED", "order_id": o.get("order_id"), "reason": "RECONCILED_LIVE"})
            self._settle(g, now_ms)
        pos = view.get("positions")
        if pos is not None:
            keys = {k for k in set(self.positions_exp) | set(pos) if not k.startswith("spot:")}
            for k in sorted(keys):
                exp, got = self.positions_exp.get(k, Decimal(0)), _D(pos.get(k, 0))
                if abs(got - exp) > EPS and f"POSITION_MISMATCH:{k}" not in self.halts:
                    self._emit("ANOMALY", "", now_ms, {"code": "POSITION_MISMATCH", "key": k, "venue": str(got), "expected": str(exp)})
                    self.halt_new_exposure(f"POSITION_MISMATCH:{k}", now_ms)

    # ---------------------------------------------------------------- internals
    def _entry_allowed(self, g: GroupRuntime) -> bool:
        return not self.halts and not (({l.symbol for l in g.plan.legs}) & (self.halted_symbols | set(self.blocked_symbols)))

    def _send_leg(self, g: GroupRuntime, lp: T.LegPlan, kind: str, parent: Optional[str], ts: int) -> bool:
        gid = g.plan.group_id
        link = f"{gid}-{lp.leg_id}"
        self._emit("INTENT", gid, ts, {"leg_id": lp.leg_id, "link": link, "kind": kind, "parent": parent, "leg": asdict(lp) if kind == "EXIT" else None})
        req = OrderRequest(gid, g.plan.candidate_id, g.plan.spec_digest, lp, link)
        try:
            ack = self._send(req)
        except Exception:
            ack = None
        if ack is None:
            self._emit("ACK_LOST", gid, ts, {"leg_id": lp.leg_id, "why": "NO_ACK"})
            return False
        self._emit("ACK", gid, ts, {"leg_id": lp.leg_id, "status": ack.status, "order_id": ack.order_id, "reason": ack.reason})
        return ack.status == "ACCEPTED"

    def _cancel_live_entries(self, g: GroupRuntime, ts: int) -> None:
        for l in g.entry_legs():
            if l.status == LIVE and l.plan.symbol not in self.halted_symbols:
                self._cancel_leg(g, l, ts)

    def _cancel_leg(self, g: GroupRuntime, l: LegRuntime, ts: int) -> None:
        gid = g.plan.group_id
        self._emit("CANCEL_INTENT", gid, ts, {"leg_id": l.plan.leg_id})
        try:
            r = self._cancel(OrderRequest(gid, g.plan.candidate_id, g.plan.spec_digest, l.plan, l.link))
        except Exception:
            r = None
        st = (r if isinstance(r, str) else getattr(r, "status", None)) if r is not None else "LOST"
        st = {"CANCELLED": "CANCELLED", "TOO_LATE": "TOO_LATE", "NOT_FOUND": "TOO_LATE"}.get(st, "LOST")
        self._emit("CANCEL_DONE", gid, ts, {"leg_id": l.plan.leg_id, "status": st})

    def _net(self, l: LegRuntime) -> Decimal:
        if l.kind != "ENTRY":
            return Decimal(0)
        red = sum((x.filled for x in self.groups_of(l).exit_legs() if x.parent == l.plan.leg_id), Decimal(0))
        return max(Decimal(0), l.filled - red - l.forced)

    def groups_of(self, l: LegRuntime) -> GroupRuntime:
        gid = l.link.split("-", 1)[0]
        return self.groups[gid]

    def _nets(self, g: GroupRuntime) -> List[Tuple[LegRuntime, Decimal, Decimal]]:
        out = []
        for l in g.entry_legs():
            n = self._net(l)
            px = l.avg_px if l.filled > 0 else Decimal(0)
            out.append((l, n, n * px))
        if g.first_intent_ms is not None:
            # A PLAN leg that was never SENT has no LegRuntime: the first create's ack was lost (or refused), ``submit_group`` stopped before it, and reconcile later filled the first leg alone. Without an
            # entry for the missing leg a group holding ONE filled leg reads as 'all legs filled' = HEDGED for the whole hold. It counts as an empty leg, so the pair reads UNHEDGED and is unwound.
            for lp in g.plan.legs:
                if lp.leg_id not in g.legs:
                    out.append((LegRuntime(lp, "ENTRY", None, f"{g.plan.group_id}-{lp.leg_id}", status=CANCELLED), Decimal(0), Decimal(0)))
        return out

    def _step_of(self, l: LegRuntime) -> Optional[Decimal]:
        s = self.qty_steps.get(_poskey(l.plan.category, l.plan.symbol)) or self.qty_steps.get(l.plan.symbol)
        return _D(s) if s else None

    def _excess(self, g: GroupRuntime) -> Dict[str, Decimal]:
        """Base qty each entry leg holds beyond what a hedge needs (EXIT mode: all of it; otherwise beyond the smallest leg's notional)."""
        nets = self._nets(g)
        if not nets:
            return {}
        if g.exit_mode == "EXIT":
            out_exit: Dict[str, Decimal] = {}
            for l, n, _ in nets:
                st = self._step_of(l)
                q = (n / st).to_integral_value(rounding="ROUND_DOWN") * st if st else n       # an off-step net (a fee taken in the base coin) would be rejected by the venue: the dust stays
                if q > 0 or not st:
                    out_exit[l.plan.leg_id] = q
            return out_exit
        matched = min(nv for _, _, nv in nets)
        out: Dict[str, Decimal] = {}
        for l, n, nv in nets:
            if n <= 0:
                continue
            px = nv / n
            ex = n if matched <= 0 else (nv - matched) / px
            ex = min(n, max(Decimal(0), ex))
            st = self._step_of(l)
            if st:
                ex = (ex / st).to_integral_value(rounding="ROUND_DOWN") * st
            if ex > EPS:
                out[l.plan.leg_id] = ex
        return out

    def _total(self, g: GroupRuntime) -> Decimal:
        """Net exposure still held, ignoring dust below one qty step (it cannot be traded and is not a position anyone can unwind)."""
        tot = Decimal(0)
        for l, n, _ in self._nets(g):
            st = self._step_of(l)
            if n > EPS and not (st and n < st):
                tot += n
        return tot

    def _balanced(self, g: GroupRuntime) -> bool:
        nets = self._nets(g)
        if len(nets) < 2:
            return True
        vals = [nv for _, _, nv in nets]
        hi, lo = max(vals), min(vals)
        if hi <= 0:
            return True
        return float((hi - lo) / hi) <= self.policy.hedge_tol or not self._excess(g)

    def _derive(self, g: GroupRuntime) -> Tuple[G, str]:
        if g.state in (G.PLANNED, G.RISK_APPROVED) and g.first_intent_ms is None and not any(l.status != NEW for l in g.legs.values()):
            return g.state, g.reason
        legs = list(g.legs.values())
        if any(l.status in (SENT, UNKNOWN) for l in legs):
            return G.UNKNOWN_OUTCOME, "ACK_OR_CANCEL_UNKNOWN_RECONCILE_BEFORE_RESEND"
        if g.halted_reason:
            return G.HALTED, g.halted_reason
        nets = self._nets(g)
        total = self._total(g)
        live_entry = any(l.status == LIVE for l in g.entry_legs())
        live_exit = any(l.status == LIVE for l in g.exit_legs())
        if g.exit_mode:
            if total <= EPS and not live_entry and not live_exit:
                return (G.CLOSED, "FLAT_VERIFIED_BY_VENUE") if g.flat_verified else (G.EXITING, "AWAIT_VENUE_FLAT_READ")
            if g.exit_mode == "UNWIND" and not live_exit and total > EPS and self._balanced(g):
                return G.HEDGED, "UNWIND_COMPLETE_REMAINDER_HEDGED"
            return G.EXITING, g.exit_mode
        fills = sum((l.filled for l in g.entry_legs()), Decimal(0))
        if live_entry:
            if all(l.filled >= l.plan_qty - EPS for l in g.entry_legs()):
                return G.HEDGED, "ALL_LEGS_FILLED"
            return (G.PARTIAL, "PARTIAL_FILLS") if fills > 0 else (G.SUBMITTED, "WORKING")
        if fills <= 0:
            return G.FAILED, "NO_FILL"
        if total <= EPS:
            return G.CLOSED if g.flat_verified else G.UNHEDGED, "NO_NET_EXPOSURE"
        if self._balanced(g):
            return G.HEDGED, "ALL_LEGS_FILLED" if all(l.filled >= l.plan_qty - EPS for l in g.entry_legs()) else "BALANCED_PARTIAL"
        return G.UNHEDGED, "LEG_IMBALANCE"

    def _set_state(self, g: GroupRuntime, new: G, reason: str, ts: int) -> None:
        if new == g.state:
            return
        if new not in _ALLOWED.get(g.state, set()):
            raise IllegalTransition(f"{g.state.value} -> {new.value} ({reason})")
        self._emit("STATE", g.plan.group_id, ts, {"from": g.state.value, "to": new.value, "reason": reason})

    def _settle(self, g: GroupRuntime, ts: int, act: bool = True) -> None:
        for _ in range(16):
            changed = False
            if act and g.exit_mode is None and g.first_intent_ms is not None and not any(l.status in (SENT, UNKNOWN) for l in g.legs.values()):
                for l in g.entry_legs():
                    if l.status == NEW:                                  # never sent (the sequence stopped at a failure): it will not be sent late
                        self._emit("LEG_GONE", g.plan.group_id, ts, {"leg_id": l.plan.leg_id})
                        changed = True
            # a failed (rejected/cancelled-unfilled) entry leg while siblings still work: stop the siblings
            if g.exit_mode is None and any(l.status == REJECTED or (l.status == CANCELLED and l.filled < l.plan_qty - EPS) for l in g.entry_legs()):
                if act and any(l.status == LIVE for l in g.entry_legs()):
                    self._cancel_live_entries(g, ts)
                    changed = True
            new, why = self._derive(g)
            if new != g.state:
                self._set_state(g, new, why, ts)
                changed = True
            if g.state == G.UNHEDGED and g.exit_mode is None:
                self._emit("EXIT_MODE", g.plan.group_id, ts, {"mode": "UNWIND", "reason": g.reason})
                changed = True
            if g.exit_mode == "UNWIND" and g.state == G.HEDGED:
                self._emit("EXIT_MODE", g.plan.group_id, ts, {"mode": None, "reason": "UNWIND_DONE"})
                changed = True
            if act and g.exit_mode and g.state in (G.EXITING, G.CLOSED, G.UNHEDGED, G.FAILED) and self._drive_exit(g, ts):
                changed = True
            if act and g.exit_mode and g.state == G.EXITING and not g.flat_verified and self._maybe_verify_flat(g, ts):
                changed = True
            if not changed:
                break

    def _drive_exit(self, g: GroupRuntime, ts: int) -> bool:
        if any(l.status in (SENT, UNKNOWN) for l in g.legs.values()) or g.halted_reason:
            return False
        if sum(1 for l in g.exit_legs() if counts_as_rejection(l)) - g.exit_attempt_floor >= self.policy.max_exit_attempts:
            self._emit("HALT_GROUP", g.plan.group_id, ts, {"reason": "EXIT_ORDERS_REJECTED"})
            return True
        sent = False
        excess = self._excess(g)
        for l in sorted(g.entry_legs(), key=lambda x: x.plan.category != "linear"):          # the perp (reduce-only) leg first: a spot sell is an exit only while the perp it hedges is already closed (ms_authority.spot_sell_keeps_hedge), and a naked short perp is the worse half-state
            ex = excess.get(l.plan.leg_id, Decimal(0))
            pending = sum((x.plan_qty - x.filled for x in g.exit_legs() if x.parent == l.plan.leg_id and x.status == LIVE), Decimal(0))
            need = ex - pending
            if any(x.parent == l.plan.leg_id and x.status == REJECTED and str(x.reject_reason).startswith(UNCOUNTED_EXIT_REASONS) and x.sent_ms == ts for x in g.exit_legs()):
                continue                                                  # held behind its perp exit (or not sendable this step because a read failed): the venue is re-read next step (a partial perp fill is then re-sent for the remainder)
            if need <= EPS or l.plan.symbol in self.halted_symbols or any(x.parent == l.plan.leg_id and x.status == LIVE for x in g.exit_legs()):
                continue
            k = sum(1 for x in g.exit_legs() if x.parent == l.plan.leg_id)
            lid = "x" + hashlib.sha256(f"{g.plan.group_id}|{l.plan.leg_id}|{k}".encode()).hexdigest()[:10]
            q = format(need.normalize(), "f")
            lp = T.LegPlan(lid, l.plan.symbol, l.plan.category, "Buy" if l.plan.side == "Sell" else "Sell", q, "Market", None, False, l.plan.category == "linear", "IOC")
            self._send_leg(g, lp, "EXIT", l.plan.leg_id, ts)
            sent = True
        return sent

    def _maybe_verify_flat(self, g: GroupRuntime, ts: int) -> bool:
        if self._total(g) > EPS or any(l.status in _OPEN_LEG for l in g.legs.values()):
            return False
        view = self._read_venue()
        pos = view.get("positions") if view else None
        if pos is None:
            self.halt_new_exposure("UNKNOWN_VENUE_POSITION", ts)
            return False
        if not bool(view.get("complete", False)):                                  # a read that may be missing rows cannot show a leg's key as flat: wait for a complete one
            return False
        for l in g.entry_legs():                                                   # EVERY leg key, spot included: flat is the venue's own read, never fill arithmetic
            k = _poskey(l.plan.category, l.plan.symbol)
            base = Decimal(0)
            if l.plan.category == "spot":                                          # a balance includes what the client already owned: verify the DELTA over the captured baseline
                if k not in self.spot_base:
                    if not any(a.get("group_id") == g.plan.group_id and a.get("code") == "SPOT_BASELINE_MISSING" for a in self.anomalies):
                        self._emit("ANOMALY", g.plan.group_id, ts, {"code": "SPOT_BASELINE_MISSING", "key": k})
                    return False                                                   # fail closed: stays EXITING, never CLOSED, never a guess
                base = self.spot_base[k]
            if abs(_D(pos.get(k, 0)) - base - self.positions_exp.get(k, Decimal(0))) > EPS:
                self._emit("ANOMALY", g.plan.group_id, ts, {"code": "NOT_FLAT_ON_VENUE", "key": k, "venue": str(pos.get(k, 0)), "expected": str(self.positions_exp.get(k, 0) + base)})
                self.halt_new_exposure(f"POSITION_MISMATCH:{k}", ts)
                self._emit("HALT_GROUP", g.plan.group_id, ts, {"reason": f"NOT_FLAT_ON_VENUE:{k}"})
                return True
        self._emit("FLAT_VERIFIED", g.plan.group_id, ts, {})
        return True

    # ---------------------------------------------------------------- reads
    def states(self) -> Dict[str, str]:
        return {gid: g.state.value for gid, g in sorted(self.groups.items())}

    def positions(self) -> Dict[str, str]:
        return {k: format(v.normalize(), "f") for k, v in sorted(self.positions_exp.items()) if v != 0}

    def open_groups(self) -> int:
        return sum(1 for g in self.groups.values() if g.state not in (G.CLOSED, G.FAILED))


# ------------------------------------------------------------------ the simulated venue (the ONLY order sink in this repository state)

class SimVenue:
    """In-memory exchange double with fault injection. It never touches a network; ``send_order`` / ``cancel_order`` / ``venue_state`` plug into ExecutionEngine."""

    def __init__(self) -> None:
        self.orders: Dict[str, Dict[str, Any]] = {}
        self.executions: List[Dict[str, Any]] = []
        self.send_calls: Dict[str, int] = {}
        self.seq = 0
        self.lose_request: set = set()           # the request never reaches the venue and no ack comes back
        self.drop_ack: set = set()               # accept the order but lose the ack (raises)
        self.crash_after_accept: set = set()     # accept, then the "process" dies before the engine journals the ack
        self.reject: Dict[str, str] = {}
        self.cancel_lost: set = set()
        self.positions_unknown = False
        self.incomplete_view = False
        self.fail_state_read = False
        self.hidden_executions: set = set()      # executions the read does not show (delayed)
        self.position_override: Dict[str, float] = {}
        self.book: Dict[str, float] = {}         # the venue's OWN position / balance book (category:symbol -> signed qty): updated by matching in execute / force_close, never re-derived from the executions list

    def _reducible(self, category: str, symbol: str, side: str) -> float:
        """The quantity an order on ``side`` can REDUCE right now: a Buy reduces a short perp, a Sell reduces a long perp or a spot balance (spot has no short)."""
        pos = self.book.get(f"{category}:{symbol}", 0.0)
        return max(0.0, -pos) if side == "Buy" else max(0.0, pos)

    def send_order(self, req: OrderRequest) -> Ack:
        link = req.link_id
        self.send_calls[link] = self.send_calls.get(link, 0) + 1
        if link in self.lose_request:
            raise TimeoutError("request lost")
        if link in self.reject:
            return Ack(link, "REJECTED", None, self.reject[link])
        if link not in self.orders and req.leg.reduce_only and self._reducible(req.leg.category, req.leg.symbol, req.leg.side) <= 1e-12:
            return Ack(link, "REJECTED", None, "REDUCE_ONLY_NO_POSITION_TO_REDUCE")          # a reduce-only order never opens or flips a position
        if link not in self.orders:                                     # idempotent on the link id
            self.seq += 1
            self.orders[link] = {"order_id": f"V{self.seq}", "link_id": link, "symbol": req.leg.symbol, "category": req.leg.category, "side": req.leg.side,
                                 "qty": float(req.leg.qty), "price": req.leg.price, "order_type": req.leg.order_type, "reduce_only": req.leg.reduce_only,
                                 "status": "New", "filled_qty": 0.0, "avg_price": 0.0, "group_id": req.group_id, "leg_id": req.leg.leg_id}
        if link in self.crash_after_accept:
            raise SimulatedCrash(link)
        if link in self.drop_ack:
            raise TimeoutError("ack lost")
        return Ack(link, "ACCEPTED", self.orders[link]["order_id"])

    def cancel_order(self, req: OrderRequest) -> str:
        if req.link_id in self.cancel_lost:
            raise TimeoutError("cancel lost")
        o = self.orders.get(req.link_id)
        if o is None:
            return "NOT_FOUND"
        if o["status"] in ("Filled",):
            return "TOO_LATE"
        o["status"] = "PartiallyFilledCanceled" if o["filled_qty"] > 0 else "Cancelled"
        return "CANCELLED"

    def execute(self, link: str, qty: float, price: float, ts_ms: int, *, liquidity: str = "MAKER", fee_usd: Optional[float] = None, fill_id: Optional[str] = None) -> Optional[T.Fill]:
        """Match ``qty`` of the order. A reduce-only order (and any spot sell) is CLAMPED to what the book holds on the reducing side, cumulatively across orders; with nothing left to
        reduce the order is cancelled and None is returned (no execution exists). The rest of a clamped order is cancelled too, as on a real venue."""
        o = self.orders[link]
        q = min(qty, o["qty"] - o["filled_qty"])
        clamped = False
        if o["reduce_only"] or (o["category"] == "spot" and o["side"] == "Sell"):
            cap = self._reducible(o["category"], o["symbol"], o["side"])
            if q > cap + 1e-12:
                q, clamped = cap, True
        q = round(q, 10)
        if q <= 1e-12:
            o["status"] = "PartiallyFilledCanceled" if o["filled_qty"] > 0 else "Cancelled"
            return None
        n = len([x for x in self.executions if x["link_id"] == link])
        fid = fill_id or f"{link}:{n}"
        fee = (fee_usd * (q / qty) if qty > 0 else fee_usd) if fee_usd is not None else q * price * (2.0 if liquidity == "MAKER" else 5.5) / 1e4
        prev = o["filled_qty"] * o["avg_price"]
        o["filled_qty"] += q
        o["avg_price"] = (prev + q * price) / o["filled_qty"] if o["filled_qty"] else 0.0
        if o["filled_qty"] >= o["qty"] - 1e-12:
            o["status"] = "Filled"
        elif o["status"] == "New":
            o["status"] = "PartiallyFilled"
        if clamped:
            o["status"] = "PartiallyFilledCanceled"
        k = f"{o['category']}:{o['symbol']}"
        self.book[k] = self.book.get(k, 0.0) + (q if o["side"] == "Buy" else -q)
        x = {"fill_id": fid, "link_id": link, "qty": q, "price": price, "fee_usd": fee, "liquidity": liquidity, "ts_ms": ts_ms, "symbol": o["symbol"], "category": o["category"], "side": o["side"]}
        self.executions.append(x)
        return T.Fill(fid, o["group_id"], o["leg_id"], o["symbol"], o["side"], q, price, fee, "USDT", liquidity, ts_ms, "SIMULATED")

    def force_close(self, symbol: str, category: str, side: str, qty: float, price: float, ts_ms: int) -> None:
        """The venue itself closes part of a position (liquidation): it shows in executions and positions, under no engine link id."""
        self.executions.append({"fill_id": f"FORCED:{len(self.executions)}", "link_id": "FORCED", "qty": qty, "price": price, "fee_usd": 0.0, "liquidity": "TAKER", "ts_ms": ts_ms,
                                "symbol": symbol, "category": category, "side": side})
        k = f"{category}:{symbol}"
        self.book[k] = self.book.get(k, 0.0) + (qty if side == "Buy" else -qty)

    def positions(self) -> Dict[str, float]:
        out: Dict[str, float] = dict(self.book)
        out.update(self.position_override)
        return out

    def venue_state(self) -> Dict[str, Any]:
        if self.fail_state_read:
            raise TimeoutError("venue read failed")
        return {"orders": {k: dict(v) for k, v in self.orders.items()},
                "executions": [dict(x) for x in self.executions if x["fill_id"] not in self.hidden_executions],
                "positions": None if self.positions_unknown else self.positions(), "complete": not self.incomplete_view}
