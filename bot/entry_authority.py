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
    different canonical body (symbol/side/qty/category/orderType/price/reduceOnly/timeInForce) is refused as a conflict, and the number of distinct unresolved entries is bounded.
  * the instrument: only the carry's own legs, ``{perp symbol: (linear, Sell), spot symbol: (spot, Buy)}``; any other (category, symbol, side) is ``UNKNOWN_INSTRUMENT`` /
    ``WRONG_DIRECTION`` (a DOGEUSDT limit, an option, a perp long, a spot sell of the hedge are all refused);
  * the order TYPE: only exact ``Market`` / ``Limit``; a Market order carries no price (a price on a Market order was sized as notional = price*qty: reproduced), and is sized
    at the mark (max with a price if one is ever stated) on BOTH sides;
  * the orderLinkId: ``[A-Za-z0-9_-]{1,36}`` fullmatch, no whitespace (``' A1'`` collided with ``'A1'`` in the ledger but not on the wire).
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
import re
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
#: An orderLinkId is exactly this (Bybit: 1-36 of A-Za-z0-9_-). ``fullmatch`` (not ``$``, which accepts a trailing newline); never stripped or normalised.
LINK_ID = re.compile(r"[A-Za-z0-9_-]{1,36}")
ORDER_TYPES = ("Market", "Limit")
#: A spot SELL is an exit only if the spot left after it still covers the short perp to within this fraction (the spot leg is net of the buy fee in base coin, and a
#: proportional partial close floors to a lot). Beyond it the sell strips the hedge off a short perp and is refused.
HEDGE_SLACK = 0.02


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
    order_type: str = ""
    time_in_force: Optional[str] = None


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


def is_spot_sell(body: Mapping[str, Any]) -> bool:
    try:
        return str(body.get("category", "")).lower() == "spot" and str(body.get("side", "")).strip().lower() == "sell"
    except Exception:                                              # noqa: BLE001
        return False


def intent_from_body(body: Mapping[str, Any]) -> OrderIntent:
    """Strict parse of the fields the authority reasons about. A body that does not state them is refused, never defaulted, normalised or stripped: what is judged is the exact
    string that will be sent."""
    missing = [k for k in ("category", "symbol", "side", "qty", "orderType") if not str(body.get(k, "")).strip()]
    if missing:
        raise EntryRefused([f"ORDER_FIELD_MISSING:{k}" for k in missing])
    raw_link = body.get("orderLinkId")
    if raw_link is None or (isinstance(raw_link, str) and not raw_link.strip()):
        raise EntryRefused(["ORDER_FIELD_MISSING:orderLinkId"])          # an order without a deterministic id cannot be reconciled
    if not isinstance(raw_link, str) or not LINK_ID.fullmatch(raw_link):
        raise EntryRefused(["ORDER_LINK_ID_INVALID"])
    if not all(isinstance(body[k], str) for k in ("category", "symbol", "side", "orderType")):
        raise EntryRefused(["ORDER_FIELD_NOT_TEXT"])
    order_type = body["orderType"]
    if order_type not in ORDER_TYPES:
        raise EntryRefused([f"ORDER_TYPE_UNKNOWN:{order_type[:20]}"])
    price = None if body.get("price") in (None, "") else str(body["price"])
    if order_type == "Market" and price is not None:
        raise EntryRefused(["MARKET_ORDER_WITH_PRICE"])                 # a Market order has no limit price; one stated here was being used to size the notional
    tif = body.get("timeInForce")
    return OrderIntent(category=body["category"], symbol=body["symbol"], side=body["side"], qty=str(body["qty"]),
                       reduce_only=_truthy(body.get("reduceOnly")), order_link_id=raw_link, price=price, order_type=order_type,
                       time_in_force=None if tif in (None, "") else str(tif))


# ---------------------------------------------------------------- checks: (intent, venue) -> refusal code or None

Check = Callable[[OrderIntent, str], Optional[str]]


def venue_check(allowed: Sequence[str]) -> Check:
    allowed_t = tuple(allowed)

    def check(_i: OrderIntent, venue: str) -> Optional[str]:
        return None if venue in allowed_t else f"VENUE_NOT_ALLOWED:{venue}"
    return check


def instrument_check(legs: Mapping[Tuple[str, str], str]) -> Check:
    """Only the carry's own legs. ``legs`` maps (category, symbol) -> the ONE side an entry may take there. Exact string comparison: no case folding, no stripping."""
    table = dict(legs)

    def check(i: OrderIntent, _v: str) -> Optional[str]:
        want = table.get((i.category, i.symbol))
        if want is None:
            return f"UNKNOWN_INSTRUMENT:{i.category}:{i.symbol}"
        return None if i.side == want else f"WRONG_DIRECTION:{i.category}:{i.symbol}:{i.side}"
    return check


def kill_switch_check(reader: Callable[[], Tuple[bool, str]]) -> Check:
    def check(_i: OrderIntent, _v: str) -> Optional[str]:
        try:
            engaged, _why = reader()
        except Exception:                                          # noqa: BLE001 - an unreadable switch is an engaged switch
            return "KILL_SWITCH_UNREADABLE"
        return "KILL_SWITCH_ENGAGED" if engaged else None
    return check


def order_notional(i: OrderIntent, mark_provider: Optional[Callable[[str], Optional[float]]] = None) -> Tuple[Optional[Decimal], Optional[str]]:
    """(worst-case notional in USD, refusal code). The worst case: a MARKET order (either side) at max(mark, any stated price): it has no limit semantics, so a price field on it
    must never LOWER the notional (reproduced: Market 1 BTC + price '1' read as $1); a limit SELL at max(limit, mark) (a limit of '1' for 100 BTC must not read as $100); a limit
    BUY at its own limit (it cannot fill above it). No usable price = a refusal, never a guess."""
    try:
        qty = Decimal(i.qty)
        px = Decimal(i.price) if i.price else None
    except (InvalidOperation, ValueError):
        return None, "ORDER_QTY_OR_PRICE_UNPARSABLE"
    if not qty.is_finite() or qty <= 0:
        return None, "ORDER_QTY_INVALID"
    mark: Optional[Decimal] = None
    if mark_provider is not None:
        try:
            m = mark_provider(i.symbol)
        except Exception:                                          # noqa: BLE001
            m = None
        if isinstance(m, (int, float)) and not isinstance(m, bool) and math.isfinite(m) and m > 0:
            mark = Decimal(str(m))
    side = i.side.strip().lower()
    if i.order_type != "Limit":                                    # Market, or anything not provably a limit: sized at the mark, never below it
        if mark is None:
            return None, "NO_PRICE_FOR_NOTIONAL"
        px = mark if px is None else max(px, mark)
    elif px is None:
        px = mark
    elif side == "sell":
        if mark is None:
            return None, "NO_PRICE_FOR_NOTIONAL"
        px = max(px, mark)
    if px is None or not px.is_finite() or px <= 0:
        return None, "NO_PRICE_FOR_NOTIONAL"
    return qty * px, None


def notional_cap_check(max_usd: float, mark_provider: Optional[Callable[[str], Optional[float]]] = None) -> Check:
    """The order's notional must be finite and within the cap (see ``order_notional``)."""
    def check(i: OrderIntent, _v: str) -> Optional[str]:
        notional, why = order_notional(i, mark_provider)
        if notional is None:
            return why
        return None if float(notional) <= float(max_usd) else "NOTIONAL_ABOVE_CAP"
    return check


def risk_policy_check(provider: Callable[..., Tuple[Optional[RP.Policy], RP.State]], latch: Optional[RP.RiskLatch], strategy: str,
                      clock: Callable[[], float] = time.time,
                      pending_notional: Optional[Callable[[OrderIntent], Optional[float]]] = None) -> Check:
    """The owner-approved policy with measured inputs; a latched suspension refuses. ``provider`` failing refuses (no policy = NO_APPROVED_POLICY). The policy is evaluated on
    POST-trade exposure: the provider is handed this order's own worst-case notional (``pending_notional``); a notional that cannot be established is passed as NaN so the
    provider reports exposure UNKNOWN rather than ignoring the order."""
    def check(i: OrderIntent, _v: str) -> Optional[str]:
        try:
            if pending_notional is not None:
                pend = pending_notional(i)
                policy, state = provider(float("nan") if pend is None else pend)
            else:
                policy, state = provider()
        except Exception:                                          # noqa: BLE001
            return "RISK_STATE_UNAVAILABLE"
        decision = RP.evaluate(policy, state)
        if latch is not None:
            decision = latch.apply(strategy, decision, at_ms=int(clock() * 1000))
        return None if decision.status == RP.ALLOW else "RISK_POLICY:" + "|".join(decision.reasons)
    return check


# ---------------------------------------------------------------- the durable reservation

_CANON_FIELDS = ("category", "symbol", "side", "qty", "orderType", "price", "reduceOnly", "timeInForce")


def _canonical_intent(i: OrderIntent) -> Dict[str, Any]:
    """The FULL body the order is identified by. A resend is 'identical' only if every one of these is equal."""
    return {"category": i.category, "symbol": i.symbol, "side": i.side, "qty": i.qty, "orderType": i.order_type, "price": i.price, "reduceOnly": bool(i.reduce_only),
            "timeInForce": i.time_in_force}


def _canonical(row: Mapping[str, Any]) -> Dict[str, Any]:
    """The same projection of a ledger row. A row written before these fields existed lacks them and therefore never equals a new intent (a conflict, never a guess)."""
    return {k: row.get(k, "<absent>") for k in _CANON_FIELDS}


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
                if _canonical(prior) == _canonical_intent(i):
                    return None                                        # an identical resend: how a lost acknowledgement is resolved
                return "RESERVATION_CONFLICT_SAME_ID_DIFFERENT_ORDER"
            unresolved = [r for r in rows if r["link"] not in resolved and not r.get("resolved") and isinstance(r.get("at_ms"), int) and now - r["at_ms"] <= self.window_ms]
            if len(unresolved) >= max_unresolved:
                return "TOO_MANY_UNRESOLVED_ENTRIES"
            os.write(fd, (json.dumps(dict(_canonical_intent(i), link=i.order_link_id, at_ms=now, resolved=False), sort_keys=True) + "\n").encode("utf-8"))
            os.fsync(fd)
            return None
        finally:
            os.close(fd)

    def resolve(self, order_link_id: str) -> None:
        """NOTHING IN PRODUCTION CALLS THIS (grep: only tests). Reservations are therefore bounded by ``RESERVATION_WINDOW_MS``, not by resolution: see RUNBOOKS 'Entry reservation ledger'.
        Mark a reservation resolved by APPENDING a resolution row under the same lock (never rewrites or replaces the file, so a reserve() waiting on the lock cannot append to an orphaned inode)."""
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

    def __init__(self, checks: Sequence[Check], *, name: str = "entry_authority", perp_symbol: Optional[str] = None, spot_symbol: Optional[str] = None,
                 hedge_reader: Optional[Callable[[], Tuple[float, float]]] = None) -> None:
        if not checks:
            raise ValueError("an authority with no checks would approve everything; refusing to build one")
        self.checks = tuple(checks)
        self.name = name
        self.perp_symbol = perp_symbol
        self.spot_symbol = spot_symbol
        #: () -> (perp short size, spot inventory), both from the VENUE's own reads (unsigned, base units). None = a spot sell can never be shown to keep the hedge.
        self.hedge_reader = hedge_reader
        self.refusals: List[Tuple[str, Tuple[str, ...]]] = []

    def spot_sell_keeps_hedge(self, body: Mapping[str, Any]) -> bool:
        """True only if this spot SELL provably leaves the short perp covered, from the venue's own position and wallet reads. Anything unknowable (no reader, a failed read, a
        non-finite number, a quote-unit or unparsable qty, another symbol) is False: the sell is then treated as exposure and refused as ``WRONG_DIRECTION``. Conservative on
        purpose: refusing a legitimate spot exit leaves a long (price risk) that a human can close; stripping the hedge leaves a naked short."""
        try:
            if self.hedge_reader is None or body.get("symbol") != self.spot_symbol or body.get("category") != "spot":
                return False
            if body.get("marketUnit") not in (None, "baseCoin"):
                return False
            qty = Decimal(str(body["qty"]))
            perp, spot = self.hedge_reader()
            perp, spot = float(perp), float(spot)
            if not (qty.is_finite() and qty > 0 and math.isfinite(perp) and math.isfinite(spot) and perp >= 0 and spot >= 0):
                return False
            return (spot - float(qty)) >= perp * (1.0 - HEDGE_SLACK)
        except Exception:                                          # noqa: BLE001
            return False

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
                   require_risk_policy: bool = True, strategy: str = "btcusdt_funding_carry_overlay", perp_symbol: Optional[str] = None,
                   spot_symbol: Optional[str] = None, hedge_reader: Optional[Callable[[], Tuple[float, float]]] = None) -> EntryAuthority:
    """The authority a production book installs on its client. ``require_risk_policy`` is not a flag the environment can turn off: callers pass True for every venue that can
    hold real or testnet exposure. A book without a state directory keeps reservations in a temp-free in-process ledger path and says so by raising: persistence is mandatory."""
    if not state_dir:
        raise ValueError("an entry authority needs a state directory for its durable reservations and risk latch")
    for nm, sym in (("perp_symbol", perp_symbol), ("spot_symbol", spot_symbol)):
        if not isinstance(sym, str) or not re.fullmatch(r"[A-Z0-9]{3,20}", sym):
            raise ValueError(f"an entry authority is bound to the carry's legs: {nm} must be a plain symbol, got {sym!r}")
    os.makedirs(state_dir, exist_ok=True)
    latch = RP.RiskLatch(os.path.join(state_dir, "risk_latch.json"))
    checks: List[Check] = [venue_check([venue])]
    reader = getattr(store, "is_kill_switch_engaged", None)
    checks.append(kill_switch_check(reader) if callable(reader) else (lambda _i, _v: "KILL_SWITCH_UNREADABLE"))
    checks.append(instrument_check({("linear", perp_symbol): "Sell", ("spot", spot_symbol): "Buy"}))
    if require_risk_policy:
        sp = state_provider or (lambda pending_notional_usd=None: RP.State())

        def provider(pending: Optional[float] = None) -> Tuple[Optional[RP.Policy], RP.State]:
            try:
                state = sp(pending_notional_usd=pending)
            except TypeError:                                      # a legacy zero-argument provider: it cannot see the pending order (and says so by not using it)
                state = sp()
            return load_policy(policy_path), state

        def pend(i: OrderIntent) -> Optional[float]:
            n, _why = order_notional(i, mark_provider)
            return None if n is None else float(n)
        checks.append(risk_policy_check(provider, latch, strategy, pending_notional=pend))
    checks.append(notional_cap_check(max_notional_usd, mark_provider))
    checks.append(reservation_check(ReservationStore(os.path.join(state_dir, "entry_reservations.jsonl"))))
    return EntryAuthority(checks, name=f"book:{venue}", perp_symbol=perp_symbol, spot_symbol=spot_symbol, hedge_reader=hedge_reader)
