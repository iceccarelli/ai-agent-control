"""The ONE authority for creating new exposure, evaluated at the last responsible boundary: ``BybitClient._request`` for ``POST /v5/order/create``.

Why here. Every path that can reach a venue (the carry book's ``CarryBroker``, ``BybitClient.place_order``/``place_stop_order``, the drills, the unattended runner, the
linear-stop drill, ``close_testnet_experiment``) ends in ``BybitClient._request``. A caller that skips a helper still passes through it, so a check placed here cannot be
skipped by a new caller. Before this module an opening order was gated only by gates the CALLER chose to apply (the carry book's own ``order_gate``/``entry_gate``; the
linear-stop drill, which calls ``place_order`` directly, had none).

What counts as new exposure (``increases_exposure``): any order that is not provably reducing. A linear/inverse order without ``reduceOnly``; a spot BUY. A spot SELL and a
``reduceOnly`` order are exits (the venue itself refuses a reduce-only quantity beyond the position). An unknown category, a missing side or an unparsable body is treated as
EXPOSURE (fail closed), never as an exit.

What the authority checks (all must pass; the first refusal wins and every reason is reported):
  * the client has an authority at all (``NO_ENTRY_AUTHORITY``: a bare client refuses to open anything);
  * the venue is one this authority was built for;
  * the persistent kill switch, re-read NOW (an unreadable switch is an engaged switch);
  * the owner-approved risk policy with MEASURED, fresh inputs (``risk_policy``): no approved policy, a missing measurement or a breach refuses; a latched suspension survives a
    restart and refuses until a reasoned clear;
  * the order's own notional against a hard cap;
  * a durable, cross-process RESERVATION per ``orderLinkId``: a resend of the identical order is allowed (that is how a lost acknowledgement is resolved), the same id with
    different symbol/side/qty/category is refused as a conflict, and the number of distinct unresolved entries is bounded.
The authority never approves anything by itself: with no approved policy the correct outcome is refusal, and nothing here sets ``approved_by_owner``.

NOT claimed: that the checks' INPUTS are measured by the running book today (risk_policy lists limits nothing measures yet: those refuse with ``MISSING_INPUT``, which is the
honest present state), or that this is venue conformance. The reduce-only exit path is not authorised here and is not made weaker by this module.
"""
from __future__ import annotations

import datetime
import fcntl
import json
import math
import os
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import risk_policy as RP

#: The engine sizes qty from the mark at decision time; the order is checked against the mark at SEND time. A cap check looser than this would be meaningless; tighter would
#: refuse sizing-correct orders that moved a few bps. 2%.
NOTIONAL_TOLERANCE = 1.02
#: Distinct entry reservations counted within this window bound CONCURRENT entries (a pair's two legs, plus a retry). It is a window, not a lifetime count: nothing in the book
#: marks a reservation 'resolved', so counting unresolved-forever rows would refuse every entry after the first pair (found in review). The reservation ledger itself is never
#: pruned: an old id still conflicts with a different order that reuses it.
RESERVATION_WINDOW_MS = 15 * 60 * 1000
MAX_UNRESOLVED_ENTRIES = 2        # one carry pair is two legs; a third distinct unresolved entry is not a retry of either


class EntryRefused(Exception):
    """An exposure-increasing order was refused before it reached the transport. ``reasons`` are stable machine codes."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = tuple(reasons)
        super().__init__("ENTRY_REFUSED: " + ", ".join(self.reasons))


@dataclass(frozen=True)
class OrderIntent:
    category: str
    symbol: str
    side: str
    qty: str
    reduce_only: bool
    order_link_id: str
    price: Optional[str] = None


def _truthy(v: Any) -> bool:
    """Only the boolean True or the exact string "true" is reduce-only. 1, "TRUE ", "yes" are NOT: how the venue reads a non-boolean is unverified, so it is treated as an entry."""
    return v is True or v == "true"


def increases_exposure(body: Mapping[str, Any]) -> bool:
    """True unless the order is PROVABLY an exit. Unknowns are exposure."""
    try:
        category = str(body.get("category", "")).lower()
        side = str(body.get("side", "")).strip().lower()
    except Exception:                                              # noqa: BLE001
        return True
    if _truthy(body.get("reduceOnly")) and category in ("linear", "inverse"):
        return False
    if category == "spot" and side == "sell":
        lev = body.get("isLeverage")
        if lev not in (None, "", 0, "0", False):
            return True                                            # a spot sell with margin leverage can open a short: not an exit
        return False
    return True


def intent_from_body(body: Mapping[str, Any]) -> OrderIntent:
    """Strict parse of the fields the authority reasons about. A body that does not state them is refused, never defaulted."""
    missing = [k for k in ("category", "symbol", "side", "qty") if not str(body.get(k, "")).strip()]
    if missing:
        raise EntryRefused([f"ORDER_FIELD_MISSING:{k}" for k in missing])
    link = str(body.get("orderLinkId", "")).strip()
    if not link:
        raise EntryRefused(["ORDER_FIELD_MISSING:orderLinkId"])          # an order without a deterministic id cannot be reconciled
    return OrderIntent(category=str(body["category"]).lower(), symbol=str(body["symbol"]), side=str(body["side"]), qty=str(body["qty"]),
                       reduce_only=_truthy(body.get("reduceOnly")), order_link_id=link, price=(None if body.get("price") in (None, "") else str(body["price"])))


# ---------------------------------------------------------------- checks: (intent, venue) -> refusal code or None

Check = Callable[[OrderIntent, str], Optional[str]]


def venue_check(allowed: Sequence[str]) -> Check:
    allowed_t = tuple(allowed)

    def check(_i: OrderIntent, venue: str) -> Optional[str]:
        return None if venue in allowed_t else f"VENUE_NOT_ALLOWED:{venue}"
    return check


def kill_switch_check(reader: Callable[[], Tuple[bool, str]]) -> Check:
    def check(_i: OrderIntent, _v: str) -> Optional[str]:
        try:
            engaged, _why = reader()
        except Exception:                                          # noqa: BLE001 - an unreadable switch is an engaged switch
            return "KILL_SWITCH_UNREADABLE"
        return "KILL_SWITCH_ENGAGED" if engaged else None
    return check


def notional_cap_check(max_usd: float, mark_provider: Optional[Callable[[str], Optional[float]]] = None) -> Check:
    """The order's notional (qty x price) must be finite and within the cap. A market order has no price in the body: the mark is asked for; no price = refusal."""
    def check(i: OrderIntent, _v: str) -> Optional[str]:
        try:
            qty = Decimal(i.qty)
            px = Decimal(i.price) if i.price else None
        except (InvalidOperation, ValueError):
            return "ORDER_QTY_OR_PRICE_UNPARSABLE"
        if not qty.is_finite() or qty <= 0:
            return "ORDER_QTY_INVALID"
        mark: Optional[Decimal] = None
        if mark_provider is not None:
            try:
                m = mark_provider(i.symbol)
            except Exception:                                      # noqa: BLE001
                m = None
            if isinstance(m, (int, float)) and not isinstance(m, bool) and math.isfinite(m) and m > 0:
                mark = Decimal(str(m))
        side = i.side.strip().lower()
        if px is None:
            px = mark                                              # a market order: priced at the mark
        elif side == "sell":
            # A limit SELL can fill at the best bid however low its limit: declaring price "1" must not make 100 BTC look like $100. Cost it at the larger of limit and mark.
            if mark is None:
                return "NO_PRICE_FOR_NOTIONAL"
            px = max(px, mark)
        # a limit BUY cannot fill above its limit: its own price is the worst case
        if px is None or not px.is_finite() or px <= 0:
            return "NO_PRICE_FOR_NOTIONAL"
        return None if float(qty * px) <= float(max_usd) else "NOTIONAL_ABOVE_CAP"
    return check


def risk_policy_check(provider: Callable[[], Tuple[Optional[RP.Policy], RP.State]], latch: Optional[RP.RiskLatch], strategy: str,
                      clock: Callable[[], float] = time.time) -> Check:
    """The owner-approved policy with measured inputs; a latched suspension refuses. ``provider`` failing refuses (no policy = NO_APPROVED_POLICY)."""
    def check(_i: OrderIntent, _v: str) -> Optional[str]:
        try:
            policy, state = provider()
        except Exception:                                          # noqa: BLE001
            return "RISK_STATE_UNAVAILABLE"
        decision = RP.evaluate(policy, state)
        if latch is not None:
            decision = latch.apply(strategy, decision, at_ms=int(clock() * 1000))
        return None if decision.status == RP.ALLOW else "RISK_POLICY:" + "|".join(decision.reasons)
    return check


# ---------------------------------------------------------------- the durable reservation

class ReservationStore:
    """Append-only, flock-serialised ledger of entry reservations shared by every process that names the same path. The reservation is written BEFORE the order is sent."""

    def __init__(self, path: str, *, window_ms: int = RESERVATION_WINDOW_MS) -> None:
        self.path = path
        self.window_ms = int(window_ms)

    def reserve(self, i: OrderIntent, *, max_unresolved: int = MAX_UNRESOLVED_ENTRIES, now_ms: Optional[int] = None) -> Optional[str]:
        """None if reserved (or an identical resend); else a refusal code."""
        now = int(time.time() * 1000) if now_ms is None else int(now_ms)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with open(self.path, "rb") as fh:
                raw = fh.read()
            rows: List[Dict[str, Any]] = []
            for line in raw.decode("utf-8", "replace").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict) or "link" not in row:
                        raise ValueError("bad row")
                except ValueError:
                    return "RESERVATION_LEDGER_UNREADABLE"            # refuse rather than guess what was already reserved
                rows.append(row)
            resolved = {r["link"] for r in rows if r.get("event") == "resolve"}
            rows = [r for r in rows if r.get("event") != "resolve"]
            same = [r for r in rows if r["link"] == i.order_link_id]
            if same:
                prior = same[0]
                if (prior.get("symbol"), prior.get("side"), prior.get("qty"), prior.get("category")) == (i.symbol, i.side, i.qty, i.category):
                    return None                                        # an identical resend: how a lost acknowledgement is resolved
                return "RESERVATION_CONFLICT_SAME_ID_DIFFERENT_ORDER"
            unresolved = [r for r in rows if r["link"] not in resolved and not r.get("resolved") and isinstance(r.get("at_ms"), int) and now - r["at_ms"] <= self.window_ms]
            if len(unresolved) >= max_unresolved:
                return "TOO_MANY_UNRESOLVED_ENTRIES"
            os.write(fd, (json.dumps({"link": i.order_link_id, "symbol": i.symbol, "side": i.side, "qty": i.qty, "category": i.category,
                                      "at_ms": now, "resolved": False}, sort_keys=True) + "\n").encode("utf-8"))
            os.fsync(fd)
            return None
        finally:
            os.close(fd)

    def resolve(self, order_link_id: str) -> None:
        """Mark a reservation resolved by APPENDING a resolution row under the same lock (never rewrites or replaces the file, so a reserve() waiting on the lock cannot append to an orphaned inode)."""
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, (json.dumps({"link": order_link_id, "event": "resolve", "at_ms": int(time.time() * 1000)}, sort_keys=True) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)


def reservation_check(store: ReservationStore, *, max_unresolved: int = MAX_UNRESOLVED_ENTRIES) -> Check:
    def check(i: OrderIntent, _v: str) -> Optional[str]:
        try:
            return store.reserve(i, max_unresolved=max_unresolved)
        except OSError:
            return "RESERVATION_LEDGER_UNAVAILABLE"
    check.side_effect = True                                       # type: ignore[attr-defined]  # writes a reservation: runs only if every other check passed
    return check


# ---------------------------------------------------------------- the authority

class EntryAuthority:
    """Runs its checks in order (the reservation LAST, so a refused order reserves nothing) and raises ``EntryRefused`` with every reason found."""

    def __init__(self, checks: Sequence[Check], *, name: str = "entry_authority") -> None:
        if not checks:
            raise ValueError("an authority with no checks would approve everything; refusing to build one")
        self.checks = tuple(checks)
        self.name = name
        self.refusals: List[Tuple[str, Tuple[str, ...]]] = []

    def authorize(self, body: Mapping[str, Any], *, venue: str) -> OrderIntent:
        intent = intent_from_body(body)
        reasons: List[str] = []
        for chk in self.checks:
            if getattr(chk, "side_effect", False) and reasons:
                continue                                           # a refused order must reserve nothing (reproduced: it did, and exhausted the reservation limit)
            why = chk(intent, venue)
            if why:
                reasons.append(why)
                if why.startswith(("KILL_SWITCH", "VENUE_NOT_ALLOWED")):
                    break                                          # nothing after a kill switch or the wrong venue matters
        if reasons:
            self.refusals.append((intent.order_link_id, tuple(reasons)))
            raise EntryRefused(reasons)
        return intent


# ---------------------------------------------------------------- production construction

POLICY_FIELDS = tuple(f for f in RP.Policy.__dataclass_fields__)


def load_policy(path: Optional[str]) -> Optional[RP.Policy]:
    """The owner's approved policy from a JSON file, or None. STRICT: every field present, no unknown field, numbers finite, and ``approved_by_owner`` literally true.
    A missing/unreadable/malformed file is None (= refusal), never a default policy."""
    if not path or os.path.islink(path) or not os.path.isfile(path):
        return None                                                # a symlinked policy file is not an owner-written one
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or set(doc) != set(POLICY_FIELDS):
        return None
    try:
        return RP.Policy(**doc)
    except TypeError:
        return None


def build_for_book(*, venue: str, store: Any, state_dir: Optional[str], max_notional_usd: float, mark_provider: Optional[Callable[[str], Optional[float]]] = None,
                   policy_path: Optional[str] = None, state_provider: Optional[Callable[[], RP.State]] = None,
                   require_risk_policy: bool = True, strategy: str = "btcusdt_funding_carry_overlay") -> EntryAuthority:
    """The authority a production book installs on its client. ``require_risk_policy`` is not a flag the environment can turn off: callers pass True for every venue that can
    hold real or testnet exposure. A book without a state directory keeps reservations in a temp-free in-process ledger path and says so by raising: persistence is mandatory."""
    if not state_dir:
        raise ValueError("an entry authority needs a state directory for its durable reservations and risk latch")
    os.makedirs(state_dir, exist_ok=True)
    latch = RP.RiskLatch(os.path.join(state_dir, "risk_latch.json"))
    checks: List[Check] = [venue_check([venue])]
    reader = getattr(store, "is_kill_switch_engaged", None)
    checks.append(kill_switch_check(reader) if callable(reader) else (lambda _i, _v: "KILL_SWITCH_UNREADABLE"))
    if require_risk_policy:
        sp = state_provider or (lambda: RP.State())

        def provider() -> Tuple[Optional[RP.Policy], RP.State]:
            return load_policy(policy_path), sp()
        checks.append(risk_policy_check(provider, latch, strategy))
    checks.append(notional_cap_check(max_notional_usd, mark_provider))
    checks.append(reservation_check(ReservationStore(os.path.join(state_dir, "entry_reservations.jsonl"))))
    return EntryAuthority(checks, name=f"book:{venue}")
