"""Atomic, persisted capital reservations: no two intents hold the same dollars.

WHY THIS IS ITS OWN MODULE
==========================
`portfolio_state` READS (it never writes) and `capital_allocator` is PURE (no
store, no clock). Reservation is the one place that WRITES capital claims, so it
lives apart: the read-only and pure modules can then be imported anywhere
without dragging a writer along.

THE RULE
========
A reservation succeeds only when

    free = available_usd - sum(live reservations in the same account scope)
           - buffer_usd
    free >= amount_usd

where ``available_usd`` is a CONFIRMED, fresh, source-labelled reading of the
account's free balance (``portfolio_state.PortfolioState.available_capital``,
itself the venue's ``totalAvailableBalance`` read through
``CarryBroker.get_available_margin_usd``), and ``buffer_usd`` is a REQUIRED
constructor argument with no default (the lead wires it; see the report).

"Live" means RESERVED or COMMITTED. A COMMITTED reservation (an order was
placed against it) keeps holding its dollars even after the venue's available
balance has already fallen by the fill: that double count is deliberate and
conservative. The caller releases it only once the venue's balance has been
re-read after the order reached a terminal state.

ATOMICITY
=========
The check and the insert run in ONE ``StateStore.transaction()``. The first
statement of that transaction is a WRITE to ``capital_reservation_lock``
(one row per account scope), so:

* in one process, ``StateStore``'s re-entrant lock is held for the whole
  read-decide-write (``transaction()`` holds it);
* across processes / connections on the same file, SQLite's write lock is
  taken BEFORE the sums are read, so a second writer blocks until the first
  commits and then reads the first one's row.

Either way two strategies can never both reserve the same dollars.

EXPIRY
======
``expire_stale(now)`` (also run inside every ``reserve``) moves a RESERVED row
past its ``expires_epoch`` to EXPIRED, freeing its dollars, EXCEPT when the
order log holds an unresolved order linked to it (``order_link_id`` set on the
row, or an order whose ``order_link_id`` equals the intent id): then the row
stays RESERVED and an ``EXPIRY_DEFERRED`` event is written. COMMITTED rows never
expire on a timer: an order went out against them and only an explicit
``release`` (refused while the linked order is unresolved) frees them.

Nothing here contacts a venue.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

import order_lifecycle as ol
from persistence import utc_now_epoch

RESERVED = "RESERVED"
COMMITTED = "COMMITTED"
RELEASED = "RELEASED"
EXPIRED = "EXPIRED"
LIVE_STATES = (RESERVED, COMMITTED)
ALL_STATES = (RESERVED, COMMITTED, RELEASED, EXPIRED)

# Machine-readable refusal / outcome codes.
OK_RESERVED = "RESERVED"
OK_IDEMPOTENT = "IDEMPOTENT_REPLAY"
OK_COMMITTED = "COMMITTED"
OK_RELEASED = "RELEASED"
AMOUNT_INVALID = "AMOUNT_INVALID"
TTL_INVALID = "TTL_INVALID"
FIELD_INVALID = "FIELD_INVALID"
AVAILABLE_UNCONFIRMED = "AVAILABLE_UNCONFIRMED"
AVAILABLE_STALE = "AVAILABLE_STALE"
AVAILABLE_INVALID = "AVAILABLE_INVALID"
INSUFFICIENT_FREE_CAPITAL = "INSUFFICIENT_FREE_CAPITAL"
INTENT_ID_CONFLICT = "INTENT_ID_CONFLICT"
INTENT_ID_SPENT = "INTENT_ID_SPENT"
NO_SUCH_RESERVATION = "NO_SUCH_RESERVATION"
ILLEGAL_STATE = "ILLEGAL_STATE"
ORDER_LINK_CONFLICT = "ORDER_LINK_CONFLICT"
ORDER_UNRESOLVED = "ORDER_UNRESOLVED"
STORE_ERROR = "STORE_ERROR"

DDL = """
CREATE TABLE IF NOT EXISTS capital_reservation (
    intent_id      TEXT PRIMARY KEY,
    strategy_id    TEXT NOT NULL,
    account_scope  TEXT NOT NULL,
    purpose        TEXT NOT NULL,
    amount_usd     REAL NOT NULL CHECK (amount_usd > 0),
    state          TEXT NOT NULL CHECK (state IN
                       ('RESERVED','COMMITTED','RELEASED','EXPIRED')),
    order_link_id  TEXT NOT NULL DEFAULT '',
    created_epoch  REAL NOT NULL,
    updated_epoch  REAL NOT NULL,
    expires_epoch  REAL NOT NULL,
    reason         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_capres_scope_state
    ON capital_reservation(account_scope, state);
CREATE TABLE IF NOT EXISTS capital_reservation_event (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id     TEXT NOT NULL,
    kind          TEXT NOT NULL,
    state_before  TEXT NOT NULL DEFAULT '',
    state_after   TEXT NOT NULL DEFAULT '',
    amount_usd    REAL NOT NULL DEFAULT 0.0,
    epoch         REAL NOT NULL,
    detail        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_capres_event_intent
    ON capital_reservation_event(intent_id);
CREATE TABLE IF NOT EXISTS capital_reservation_lock (
    account_scope TEXT PRIMARY KEY,
    version       INTEGER NOT NULL DEFAULT 0
);
"""


def install_schema(store: Any) -> None:
    """Create the reservation tables on an open ``StateStore``. Idempotent
    (every statement is IF NOT EXISTS); uses only the store's own
    ``transaction()``/``_exec`` so persistence.py stays untouched."""
    with store.transaction():
        for stmt in [s.strip() for s in DDL.split(";") if s.strip()]:
            store._exec(stmt)


@dataclass(frozen=True)
class ReservationResult:
    ok: bool
    code: str
    intent_id: str
    state: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok


def _finite(x: Any) -> bool:
    return (isinstance(x, (int, float)) and not isinstance(x, bool)
            and math.isfinite(float(x)))


class CapitalReservations:
    """Persisted, transactional, idempotent-by-intent-id capital claims.

    ``buffer_usd``: dollars never handed out, on top of live reservations.
    REQUIRED, no default; must be finite and >= 0.
    ``max_available_age_s``: how old the available-balance reading may be when
    a reservation is decided. REQUIRED, no default; finite and > 0.
    """

    def __init__(self, store: Any, *, buffer_usd: float,
                 max_available_age_s: float) -> None:
        if not (_finite(buffer_usd) and buffer_usd >= 0):
            raise ValueError(f"buffer_usd must be finite and >= 0, got {buffer_usd!r}")
        if not (_finite(max_available_age_s) and max_available_age_s > 0):
            raise ValueError("max_available_age_s must be finite and > 0, got "
                             f"{max_available_age_s!r}")
        self.store = store
        self.buffer_usd = float(buffer_usd)
        self.max_available_age_s = float(max_available_age_s)
        install_schema(store)

    # -- reads ---------------------------------------------------------------

    def get(self, intent_id: str) -> Optional[Dict[str, Any]]:
        rows = self.store._query(
            "SELECT * FROM capital_reservation WHERE intent_id = ?", (str(intent_id),))
        return dict(rows[0]) if rows else None

    def live(self, account_scope: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = ("SELECT * FROM capital_reservation WHERE state IN "
               "('RESERVED','COMMITTED')")
        args: tuple = ()
        if account_scope is not None:
            sql += " AND account_scope = ?"
            args = (str(account_scope),)
        return [dict(r) for r in self.store._query(sql + " ORDER BY intent_id", args)]

    def live_total_usd(self, account_scope: Optional[str] = None) -> float:
        return float(sum(float(r["amount_usd"]) for r in self.live(account_scope)))

    def live_by_strategy(self, account_scope: Optional[str] = None
                         ) -> Dict[str, Dict[str, float]]:
        """strategy_id -> {"reserved_usd", "committed_usd"} over live rows."""
        out: Dict[str, Dict[str, float]] = {}
        for r in self.live(account_scope):
            slot = out.setdefault(str(r["strategy_id"]),
                                  {"reserved_usd": 0.0, "committed_usd": 0.0})
            key = "reserved_usd" if r["state"] == RESERVED else "committed_usd"
            slot[key] += float(r["amount_usd"])
        return out

    def events(self, intent_id: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.store._query(
            "SELECT * FROM capital_reservation_event WHERE intent_id = ? "
            "ORDER BY id", (str(intent_id),))]

    def free_capital_usd(self, available: Any, account_scope: str,
                         now: Optional[float] = None) -> ReservationResult:
        """What a new reservation could take right now, or a refusal saying
        why it cannot be known. Read-only: does NOT run expiry."""
        now = utc_now_epoch() if now is None else float(now)
        bad = self._available_refusal(available, now)
        if bad is not None:
            return ReservationResult(False, bad[0], "", detail=bad[1])
        held = self.live_total_usd(account_scope)
        free = float(available.value) - held - self.buffer_usd
        return ReservationResult(True, "FREE_CAPITAL", "", detail={
            "free_usd": free, "available_usd": float(available.value),
            "available_source": available.source, "live_reserved_usd": held,
            "buffer_usd": self.buffer_usd, "account_scope": account_scope})

    # -- writes --------------------------------------------------------------

    def _event(self, intent_id: str, kind: str, before: str, after: str,
               amount: float, now: float, detail: Mapping[str, Any]) -> None:
        self.store._exec(
            "INSERT INTO capital_reservation_event(intent_id, kind, "
            "state_before, state_after, amount_usd, epoch, detail) "
            "VALUES(?,?,?,?,?,?,?)",
            (intent_id, kind, before, after, float(amount), now,
             json.dumps(dict(detail), default=str, sort_keys=True)))

    def _lock_scope(self, account_scope: str) -> None:
        # A WRITE first: takes SQLite's write lock before anything is read.
        self.store._exec(
            "INSERT INTO capital_reservation_lock(account_scope, version) "
            "VALUES(?, 1) ON CONFLICT(account_scope) DO UPDATE SET "
            "version = version + 1", (account_scope,))

    def _available_refusal(self, available: Any, now: float):
        status = getattr(available, "status", None)
        value = getattr(available, "value", None)
        source = getattr(available, "source", None)
        as_of = getattr(available, "as_of_epoch", None)
        if status != "CONFIRMED":
            return AVAILABLE_UNCONFIRMED, {
                "status": status, "reason": getattr(available, "reason", ""),
                "note": "an available balance that is not CONFIRMED is not "
                        "a number capital can be reserved against"}
        if not (_finite(value) and value >= 0 and isinstance(source, str)
                and source.strip() and _finite(as_of)):
            return AVAILABLE_INVALID, {"value": repr(value), "source": source,
                                       "as_of_epoch": as_of}
        age = now - float(as_of)
        # A reading stamped after `now` is not stale; one stamped further in
        # the future than the age limit means two clocks disagree: refuse.
        if age > self.max_available_age_s or age < -self.max_available_age_s:
            return AVAILABLE_STALE, {"age_s": age,
                                     "max_age_s": self.max_available_age_s,
                                     "source": source}
        return None

    def _linked_order_unresolved(self, row: Mapping[str, Any]) -> Optional[str]:
        """The link id of an unresolved order tied to this reservation, if any.
        An unreadable order log counts as unresolved (fail closed)."""
        links = [str(row.get("order_link_id") or ""), str(row["intent_id"])]
        for link in [l for l in links if l]:
            try:
                order = self.store.get_order(link)
            except Exception:  # noqa: BLE001 - unreadable is unresolved
                return link
            if order is not None and str(order.get("status")) not in ol.TERMINAL:
                return link
        return None

    def _expire_locked(self, now: float, account_scope: Optional[str]) -> List[str]:
        sql = ("SELECT * FROM capital_reservation WHERE state = 'RESERVED' "
               "AND expires_epoch <= ?")
        args: tuple = (now,)
        if account_scope is not None:
            sql += " AND account_scope = ?"
            args = (now, account_scope)
        expired: List[str] = []
        for r in [dict(x) for x in self.store._query(sql + " ORDER BY intent_id", args)]:
            link = self._linked_order_unresolved(r)
            if link is not None:
                self._event(r["intent_id"], "EXPIRY_DEFERRED", RESERVED, RESERVED,
                            r["amount_usd"], now,
                            {"order_link_id": link,
                             "why": "an unresolved order may be using this capital"})
                continue
            self.store._exec(
                "UPDATE capital_reservation SET state = 'EXPIRED', "
                "updated_epoch = ?, reason = 'TTL_ELAPSED' WHERE intent_id = ? "
                "AND state = 'RESERVED'", (now, r["intent_id"]))
            self._event(r["intent_id"], "EXPIRE", RESERVED, EXPIRED,
                        r["amount_usd"], now, {"expires_epoch": r["expires_epoch"]})
            expired.append(r["intent_id"])
        return expired

    def expire_stale(self, now: Optional[float] = None,
                     account_scope: Optional[str] = None) -> List[str]:
        """Expire RESERVED rows past their TTL; returns the expired intent ids.
        Rows tied to an unresolved order stay RESERVED (see module doc)."""
        now = utc_now_epoch() if now is None else float(now)
        with self.store.transaction():
            if account_scope is not None:
                self._lock_scope(account_scope)
            return self._expire_locked(now, account_scope)

    def reserve(self, strategy_id: str, intent_id: str, amount_usd: float,
                account_scope: str, purpose: str, *, available: Any,
                ttl_s: float, now: Optional[float] = None) -> ReservationResult:
        """Claim ``amount_usd`` for ``intent_id`` or refuse with a reason.

        ``available``: a ``portfolio_state.Field`` (status/value/source/
        as_of_epoch) for the account's free balance. ``ttl_s``: explicit, > 0.
        Replaying the same intent id with identical parameters returns the
        existing reservation (``IDEMPOTENT_REPLAY``) and claims nothing new."""
        now = utc_now_epoch() if now is None else float(now)
        for name, val in (("strategy_id", strategy_id), ("intent_id", intent_id),
                          ("account_scope", account_scope), ("purpose", purpose)):
            if not isinstance(val, str) or not val.strip():
                return ReservationResult(False, FIELD_INVALID, str(intent_id),
                                         detail={"field": name})
        if not (_finite(amount_usd) and amount_usd > 0):
            return ReservationResult(False, AMOUNT_INVALID, intent_id,
                                     detail={"amount_usd": repr(amount_usd)})
        if not (_finite(ttl_s) and ttl_s > 0):
            return ReservationResult(False, TTL_INVALID, intent_id,
                                     detail={"ttl_s": repr(ttl_s)})
        amount = float(amount_usd)
        with self.store.transaction():
            self._lock_scope(account_scope)
            existing = self.get(intent_id)
            if existing is not None:
                same = (existing["strategy_id"] == strategy_id
                        and existing["account_scope"] == account_scope
                        and existing["purpose"] == purpose
                        and abs(float(existing["amount_usd"]) - amount) <= 1e-9)
                if not same:
                    return ReservationResult(False, INTENT_ID_CONFLICT, intent_id,
                                             existing["state"],
                                             {"existing": existing})
                if existing["state"] in LIVE_STATES:
                    return ReservationResult(True, OK_IDEMPOTENT, intent_id,
                                             existing["state"],
                                             {"amount_usd": amount})
                return ReservationResult(False, INTENT_ID_SPENT, intent_id,
                                         existing["state"],
                                         {"note": "an intent id that was released "
                                                  "or expired is never reused"})
            bad = self._available_refusal(available, now)
            if bad is not None:
                self._event(intent_id, "REFUSE", "", "", amount, now,
                            {"code": bad[0], **bad[1]})
                return ReservationResult(False, bad[0], intent_id, detail=bad[1])
            self._expire_locked(now, account_scope)
            held = sum(float(r["amount_usd"]) for r in self.store._query(
                "SELECT amount_usd FROM capital_reservation WHERE account_scope = ? "
                "AND state IN ('RESERVED','COMMITTED')", (account_scope,)))
            free = float(available.value) - held - self.buffer_usd
            detail = {"amount_usd": amount, "free_usd": free,
                      "available_usd": float(available.value),
                      "available_source": available.source,
                      "live_reserved_usd": held, "buffer_usd": self.buffer_usd,
                      "account_scope": account_scope}
            if free < amount:
                self._event(intent_id, "REFUSE", "", "", amount, now,
                            {"code": INSUFFICIENT_FREE_CAPITAL, **detail})
                return ReservationResult(False, INSUFFICIENT_FREE_CAPITAL,
                                         intent_id, detail=detail)
            self.store._exec(
                "INSERT INTO capital_reservation(intent_id, strategy_id, "
                "account_scope, purpose, amount_usd, state, created_epoch, "
                "updated_epoch, expires_epoch) VALUES(?,?,?,?,?,'RESERVED',?,?,?)",
                (intent_id, strategy_id, account_scope, purpose, amount, now, now,
                 now + float(ttl_s)))
            self._event(intent_id, "RESERVE", "", RESERVED, amount, now, detail)
            return ReservationResult(True, OK_RESERVED, intent_id, RESERVED, detail)

    def commit(self, intent_id: str, order_link_id: str, *,
               now: Optional[float] = None) -> ReservationResult:
        """An order was (about to be) sent against this reservation. Idempotent
        for the same link id; COMMITTED rows never expire on a timer."""
        now = utc_now_epoch() if now is None else float(now)
        if not isinstance(order_link_id, str) or not order_link_id.strip():
            return ReservationResult(False, FIELD_INVALID, intent_id,
                                     detail={"field": "order_link_id"})
        with self.store.transaction():
            row = self.get(intent_id)
            if row is None:
                return ReservationResult(False, NO_SUCH_RESERVATION, intent_id)
            self._lock_scope(row["account_scope"])
            row = self.get(intent_id) or row  # re-read under the write lock
            if row["state"] == COMMITTED:
                if row["order_link_id"] == order_link_id:
                    return ReservationResult(True, OK_IDEMPOTENT, intent_id, COMMITTED)
                return ReservationResult(False, ORDER_LINK_CONFLICT, intent_id,
                                         COMMITTED,
                                         {"existing_link": row["order_link_id"]})
            if row["state"] != RESERVED:
                return ReservationResult(False, ILLEGAL_STATE, intent_id,
                                         row["state"],
                                         {"note": "only a RESERVED claim can be "
                                                  "committed; re-reserve"})
            self.store._exec(
                "UPDATE capital_reservation SET state = 'COMMITTED', "
                "order_link_id = ?, updated_epoch = ? WHERE intent_id = ?",
                (order_link_id, now, intent_id))
            self._event(intent_id, "COMMIT", RESERVED, COMMITTED, row["amount_usd"],
                        now, {"order_link_id": order_link_id})
            return ReservationResult(True, OK_COMMITTED, intent_id, COMMITTED)

    def release(self, intent_id: str, reason: str, *,
                now: Optional[float] = None) -> ReservationResult:
        """Give the dollars back. Refused while a linked order is unresolved
        (its fill may still be using them). Idempotent once RELEASED."""
        now = utc_now_epoch() if now is None else float(now)
        with self.store.transaction():
            row = self.get(intent_id)
            if row is None:
                return ReservationResult(False, NO_SUCH_RESERVATION, intent_id)
            self._lock_scope(row["account_scope"])
            row = self.get(intent_id) or row  # re-read under the write lock
            if row["state"] == RELEASED:
                return ReservationResult(True, OK_IDEMPOTENT, intent_id, RELEASED)
            if row["state"] == EXPIRED:
                return ReservationResult(False, ILLEGAL_STATE, intent_id, EXPIRED)
            link = self._linked_order_unresolved(row)
            if link is not None:
                self._event(intent_id, "RELEASE_REFUSED", row["state"],
                            row["state"], row["amount_usd"], now,
                            {"order_link_id": link, "reason": reason})
                return ReservationResult(False, ORDER_UNRESOLVED, intent_id,
                                         row["state"], {"order_link_id": link})
            self.store._exec(
                "UPDATE capital_reservation SET state = 'RELEASED', "
                "updated_epoch = ?, reason = ? WHERE intent_id = ?",
                (now, str(reason), intent_id))
            self._event(intent_id, "RELEASE", row["state"], RELEASED,
                        row["amount_usd"], now, {"reason": reason})
            return ReservationResult(True, OK_RELEASED, intent_id, RELEASED)
