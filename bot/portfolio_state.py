"""One read-only portfolio state, built from the records that are authoritative.

Every number carries WHERE it came from, WHETHER it can be trusted and WHEN it
was true. Nothing here writes, sends or estimates: a figure no source states is
UNAVAILABLE with a reason, never 0.

STATUS
======
  CONFIRMED     the authority stated it and nothing contradicts it
  UNAVAILABLE   the authority did not state it, could not be read, or no
                authority for it exists in this codebase today (reason says
                which). ``value`` is None.
  CONTRADICTED  two authorities disagree beyond an explicit tolerance. The
                value shown is the AUTHORITY's (per the table below); the
                other side is in ``detail``.

AUTHORITY TABLE (``AUTHORITY``)
===============================
  account_equity        venue totalEquity (BybitClient.get_equity), as persisted
                        to equity_state by TradingBot.tick (StateStore)
  available_capital     venue totalAvailableBalance (CarryBroker.get_available_margin_usd)
  quote_free            venue walletBalance-locked of the quote coin
                        (CarryBroker.get_quote_free_usd)
  reserved_capital      capital_reservation table (CapitalReservations)
  deployed_capital      venue: perp positionIM (CarryBroker.get_liquidation_view)
                        + spot inventory x spot mark (get_spot_inventory, get_spot_mark)
  spot_assets           venue (CarryBroker.get_spot_inventory)
  linear_positions      venue (CarryBroker.get_perp_position)
  open_orders           venue (CarryBroker.get_open_carry_orders)
  unresolved_orders     order log (StateStore.unresolved_carry_orders)
  initial_margin        venue positionIM (CarryBroker.get_liquidation_view)
  maintenance_margin    venue accountMMRate (a RATE; get_liquidation_view)
  fees_paid             ledger (Journal.statement fees_usd + fee provenance)
  fees_due              no authority today -> UNAVAILABLE
  funding               ledger (Journal.statement funding_usd + provenance)
  funding_accrued       no authority today -> UNAVAILABLE
  unrealised_pnl        no authority today -> UNAVAILABLE
  realised_pnl          ledger (Journal.statement net_usd / net_basis)
  liquidation_exposure  venue (CarryBroker.get_liquidation_view)
  per_strategy_risk     capital_reservation table (capital held per strategy)
  total_risk            capital_reservation table (sum of the above)
  daily_loss            StateStore daily_anchor + account_equity
  drawdown              StateStore equity_state
  kill_switch           StateStore.is_kill_switch_engaged
  engine_book           StateStore.load_carry_position (CarryEngine.to_state)

CONTRADICTION CHECKS (tolerances in ``Tolerances``)
===================================================
  PERP_ENGINE_VS_VENUE   engine perp leg filled_qty vs venue perp size
  PERP_LEDGER_VS_VENUE   |ledger open_perp_qty| vs venue perp size
  PERP_ENGINE_VS_LEDGER  engine perp leg vs |ledger open_perp_qty|
  ORDER_LOG_VS_VENUE     an order OPEN at the venue that the order log does
                         not hold as unresolved
  QUOTE_EXCEEDS_AVAILABLE the quote coin's free balance above the account's
                         total availability (same tolerance as
                         CarryEngine._capital_refusal)

``entry_blockers()`` turns all of this into machine-readable reasons.
``reduction_allowed()`` is True ALWAYS: no blocker here may stop a protective
exit or an emergency reduction of risk.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import ledger as _ledger
from persistence import utc_day, utc_now_epoch

CONFIRMED = "CONFIRMED"
UNAVAILABLE = "UNAVAILABLE"
CONTRADICTED = "CONTRADICTED"
STATUSES = (CONFIRMED, UNAVAILABLE, CONTRADICTED)

AUTHORITY: Dict[str, str] = {
    "account_equity": "venue:totalEquity via StateStore.equity_state",
    "available_capital": "venue:CarryBroker.get_available_margin_usd",
    "quote_free": "venue:CarryBroker.get_quote_free_usd",
    "reserved_capital": "store:capital_reservation",
    "deployed_capital": "venue:get_liquidation_view.position_im_usd+get_spot_inventory*get_spot_mark",
    "spot_assets": "venue:CarryBroker.get_spot_inventory",
    "linear_positions": "venue:CarryBroker.get_perp_position",
    "open_orders": "venue:CarryBroker.get_open_carry_orders",
    "unresolved_orders": "order_log:StateStore.unresolved_carry_orders",
    "initial_margin": "venue:get_liquidation_view.position_im_usd",
    "maintenance_margin": "venue:get_liquidation_view.account_mm_rate",
    "fees_paid": "ledger:Journal.statement.fees_usd",
    "fees_due": "none",
    "funding": "ledger:Journal.statement.funding_usd",
    "funding_accrued": "none",
    "unrealised_pnl": "none",
    "realised_pnl": "ledger:Journal.statement.net_usd",
    "liquidation_exposure": "venue:CarryBroker.get_liquidation_view",
    "per_strategy_risk": "store:capital_reservation(live, by strategy)",
    "total_risk": "store:capital_reservation(live, total)",
    "daily_loss": "store:daily_anchor+account_equity",
    "drawdown": "store:equity_state",
    "kill_switch": "store:StateStore.is_kill_switch_engaged",
    "engine_book": "store:StateStore.load_carry_position",
}

#: Fields that must be CONFIRMED and fresh before NEW exposure may be taken.
REQUIRED_FOR_ENTRY = (
    "kill_switch", "unresolved_orders", "account_equity", "available_capital",
    "linear_positions", "open_orders", "daily_loss", "drawdown")
#: Plus: the ledger must load and produce a statement (REQUIRED_UNAVAILABLE:ledger).
#: A PROVISIONAL realised P&L (an open lifecycle not yet funding-reconciled) is
#: reported as UNAVAILABLE but does not by itself block; an unknown fee does.

#: Fields that come from a point-in-time READING and so can go stale. Store
#: and ledger records are the record itself; their as-of is when it was read.
TIME_SENSITIVE = ("account_equity", "available_capital", "quote_free",
                  "spot_assets", "linear_positions", "open_orders",
                  "initial_margin", "maintenance_margin", "liquidation_exposure",
                  "deployed_capital", "daily_loss", "drawdown")

# Blocker codes (prefix before ':' is the stable machine code).
KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
KILL_SWITCH_UNREADABLE = "KILL_SWITCH_UNREADABLE"
UNRESOLVED_ORDERS = "UNRESOLVED_ORDERS"
STALE_STATE = "STALE_STATE"
REQUIRED_UNAVAILABLE = "REQUIRED_UNAVAILABLE"
FEE_UNKNOWN = "FEE_UNKNOWN"
RECONCILIATION_FAILED = "RECONCILIATION_FAILED"
CONTRADICTION = "CONTRADICTION"
LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
ENGINE_HALTED = "ENGINE_HALTED"

#: Lifecycle states from ledger.lifecycle_reconciliation that are FAILURES (as
#: opposed to "not yet reconciled", which is reported but does not block).
FAILED_RECONCILIATION_STATES = ("CONTRADICTED_AFTER_RECONCILIATION",
                                "INCONSISTENT_RECORD")

#: Purposes a reduction may carry. Informational: reduction_allowed() is True
#: whatever the purpose.
REDUCTION_PURPOSES = ("exit", "protective_exit", "emergency_reduce", "unwind")


@dataclass(frozen=True)
class Field:
    name: str
    value: Any
    status: str
    source: str
    as_of_epoch: Optional[float]
    reason: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def confirmed(self) -> bool:
        return self.status == CONFIRMED


def unavailable(name: str, reason: str, source: Optional[str] = None,
                as_of: Optional[float] = None, **detail: Any) -> Field:
    return Field(name, None, UNAVAILABLE, source or AUTHORITY.get(name, "none"),
                 as_of, reason, dict(detail))


def confirmed(name: str, value: Any, as_of: Optional[float],
              source: Optional[str] = None, **detail: Any) -> Field:
    return Field(name, value, CONFIRMED, source or AUTHORITY[name], as_of, "",
                 dict(detail))


@dataclass(frozen=True)
class Tolerances:
    """Explicit comparison slack. Defaults are the constants the codebase
    already uses for the same comparisons (no new numbers):
    perp size -> ledger.SIZE_REL_TOL / SIZE_ABS_TOL (lifecycle size match);
    balance consistency -> CarryEngine._capital_refusal's 1e-9 rel + 1e-6 abs."""
    size_rel: float = _ledger.SIZE_REL_TOL
    size_abs: float = _ledger.SIZE_ABS_TOL
    balance_rel: float = 1e-9
    balance_abs: float = 1e-6

    def sizes_agree(self, a: float, b: float) -> bool:
        return abs(a - b) <= max(self.size_abs, self.size_rel * max(abs(a), abs(b)))


@dataclass(frozen=True)
class PortfolioLimits:
    """Every limit is REQUIRED (no defaults). ``max_total_capital_usd`` may be
    passed as None to say explicitly "not enforced here"; the state then records
    that it was not checked."""
    max_daily_loss_fraction: float
    max_drawdown_fraction: float
    max_state_age_s: float
    max_total_capital_usd: Optional[float]


@dataclass(frozen=True)
class Contradiction:
    code: str
    detail: Dict[str, Any]


@dataclass(frozen=True)
class VenueSnapshot:
    """Venue reads, each already a Field. Build with ``read_venue``."""
    available_capital: Field
    quote_free: Field
    spot_assets: Field
    spot_mark: Field
    linear_positions: Field
    open_orders: Field
    liquidation_view: Field


def read_venue(broker: Any, *, spot_symbol: str, perp_symbol: str,
               quote_coin: str = "USDT",
               clock: Callable[[], float] = utc_now_epoch) -> VenueSnapshot:
    """Call the CarryBroker READ methods (nothing is sent) once each. A read
    that raises, is missing or returns a non-finite / negative number becomes
    UNAVAILABLE naming the exception; it is never replaced by 0."""

    def read(name: str, source: str, fn_name: str, *args: Any,
             numeric: bool = True) -> Field:
        fn = getattr(broker, fn_name, None)
        if not callable(fn):
            return unavailable(name, f"broker has no {fn_name}", source)
        try:
            value = fn(*args)
        except Exception as exc:  # noqa: BLE001 - unreadable is unavailable
            return unavailable(name, f"{fn_name} raised {type(exc).__name__}: {exc}",
                               source, clock())
        at = clock()
        if numeric:
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(float(value)) or float(value) < 0):
                return unavailable(name, f"{fn_name} returned unusable {value!r}",
                                   source, at)
            value = float(value)
        return confirmed(name, value, at, source)

    liq = read("liquidation_view", "venue:CarryBroker.get_liquidation_view",
               "get_liquidation_view", perp_symbol, numeric=False)
    if liq.confirmed and not isinstance(liq.value, Mapping):
        liq = unavailable("liquidation_view", "not a mapping", liq.source,
                          liq.as_of_epoch)
    orders = read("open_orders", AUTHORITY["open_orders"], "get_open_carry_orders",
                  spot_symbol, perp_symbol, numeric=False)
    if orders.confirmed and not isinstance(orders.value, list):
        orders = unavailable("open_orders", "not a list", orders.source,
                             orders.as_of_epoch)
    return VenueSnapshot(
        available_capital=read("available_capital", AUTHORITY["available_capital"],
                               "get_available_margin_usd"),
        quote_free=read("quote_free", AUTHORITY["quote_free"],
                        "get_quote_free_usd", quote_coin),
        spot_assets=read("spot_assets", AUTHORITY["spot_assets"],
                         "get_spot_inventory", spot_symbol),
        spot_mark=read("spot_mark", "venue:CarryBroker.get_spot_mark",
                       "get_spot_mark", spot_symbol),
        linear_positions=read("linear_positions", AUTHORITY["linear_positions"],
                              "get_perp_position", perp_symbol),
        open_orders=orders,
        liquidation_view=liq,
    )


@dataclass(frozen=True)
class PortfolioState:
    as_of_epoch: float
    fields: Dict[str, Field]
    contradictions: Tuple[Contradiction, ...]
    limits: PortfolioLimits
    _blockers: Tuple[str, ...]

    def __getattr__(self, name: str) -> Field:
        fields = self.__dict__.get("fields") or {}
        if name in fields:
            return fields[name]
        raise AttributeError(name)

    def entry_blockers(self) -> List[str]:
        """Machine-readable reasons NEW exposure is refused. Empty = none.
        Each item is ``CODE`` or ``CODE:qualifier``."""
        return list(self._blockers)

    def entries_permitted(self) -> Tuple[bool, str]:
        """Same shape as main.carry_entries_permitted_from's gate."""
        b = self._blockers
        return (not b, "portfolio clear" if not b else "; ".join(b))

    def reduction_allowed(self, purpose: str = "exit") -> bool:
        """Protective exits and emergency risk reduction are ALWAYS allowed.

        Deliberately unconditional: a stale price, an unknown fee, a tripped
        kill switch, a contradiction or a breached limit are each a reason to
        stop ADDING exposure and never a reason to keep it. The order gates
        that guard a reduction (reduce-only, the order lifecycle) live with
        the order, not here."""
        return True

    def as_dict(self) -> Dict[str, Any]:
        return {
            "as_of_epoch": self.as_of_epoch,
            "fields": {k: {"value": f.value, "status": f.status,
                           "source": f.source, "as_of_epoch": f.as_of_epoch,
                           "reason": f.reason, "detail": f.detail}
                       for k, f in self.fields.items()},
            "contradictions": [{"code": c.code, "detail": c.detail}
                               for c in self.contradictions],
            "entry_blockers": list(self._blockers),
        }


def _engine_perp_qty(book: Optional[Mapping[str, Any]]) -> Optional[float]:
    if book is None:
        return None
    pos = book.get("position")
    if pos is None:
        return 0.0
    try:
        q = float((pos.get("perp") or {}).get("filled_qty"))
    except (TypeError, ValueError, AttributeError):
        return None
    return abs(q) if math.isfinite(q) else None


def build_portfolio_state(store: Any, *, limits: PortfolioLimits,
                          venue: Optional[VenueSnapshot],
                          reservations: Any = None,
                          account_scope: Optional[str] = None,
                          tolerances: Tolerances = Tolerances(),
                          fee_window_from_ms: Optional[int] = None,
                          now_epoch: Optional[float] = None) -> PortfolioState:
    """Assemble the state. READ-ONLY: only StateStore reads are used
    (``get_daily_anchor``, never ``ensure_daily_anchor``, which writes).

    ``venue`` None means no venue read was made: every venue field is
    UNAVAILABLE and entries are blocked. ``fee_window_from_ms``: the journal
    window over which an unknown fee blocks entries (None = the whole journal;
    the journal is append-only, so an old unknown fee then blocks until a
    human scopes the window)."""
    now = utc_now_epoch() if now_epoch is None else float(now_epoch)
    f: Dict[str, Field] = {}
    contra: List[Contradiction] = []

    # -- kill switch ------------------------------------------------------
    try:
        engaged, why = store.is_kill_switch_engaged()
        f["kill_switch"] = confirmed("kill_switch", bool(engaged), now, why=why)
    except Exception as exc:  # noqa: BLE001
        f["kill_switch"] = unavailable("kill_switch",
                                       f"unreadable: {type(exc).__name__}: {exc}")

    # -- order log --------------------------------------------------------
    try:
        pending = store.unresolved_carry_orders()
        if not isinstance(pending, list):
            raise TypeError(type(pending).__name__)
        f["unresolved_orders"] = confirmed(
            "unresolved_orders",
            [{k: o.get(k) for k in ("order_link_id", "symbol", "status", "leg",
                                    "cycle_id", "filled_qty", "qty")}
             for o in pending], now)
    except Exception as exc:  # noqa: BLE001
        f["unresolved_orders"] = unavailable(
            "unresolved_orders", f"order log unreadable: {type(exc).__name__}: {exc}")

    # -- engine book ------------------------------------------------------
    try:
        book = store.load_carry_position()
        f["engine_book"] = (
            unavailable("engine_book", "no carry book has run against this store")
            if book is None else
            confirmed("engine_book", {"book_state": book.get("book_state"),
                                      "perp_qty": _engine_perp_qty(book)},
                      float(book.get("updated_epoch") or now)))
    except Exception as exc:  # noqa: BLE001
        book = None
        f["engine_book"] = unavailable("engine_book",
                                       f"unreadable: {type(exc).__name__}: {exc}")

    # -- equity, daily loss, drawdown -----------------------------------
    try:
        eq = store.get_equity_state()
        if eq.updated_epoch > 0 and math.isfinite(eq.last_equity) and eq.last_equity >= 0:
            f["account_equity"] = confirmed("account_equity", eq.last_equity,
                                            eq.updated_epoch)
        else:
            f["account_equity"] = unavailable("account_equity",
                                              "no equity observation recorded")
        if eq.peak_equity > 0 and f["account_equity"].confirmed:
            f["drawdown"] = confirmed("drawdown", eq.drawdown_fraction(),
                                      eq.updated_epoch, peak_equity=eq.peak_equity)
        else:
            f["drawdown"] = unavailable("drawdown", "no positive peak equity recorded")
    except Exception as exc:  # noqa: BLE001
        f["account_equity"] = unavailable("account_equity",
                                          f"unreadable: {type(exc).__name__}")
        f["drawdown"] = unavailable("drawdown", f"unreadable: {type(exc).__name__}")
    try:
        anchor = store.get_daily_anchor(utc_day(now))
        eqf = f["account_equity"]
        if anchor is None or anchor.start_equity <= 0:
            f["daily_loss"] = unavailable("daily_loss",
                                          "no daily anchor for today's UTC day")
        elif not eqf.confirmed:
            f["daily_loss"] = unavailable("daily_loss", "account equity unavailable")
        else:
            loss = max(0.0, (anchor.start_equity - eqf.value) / anchor.start_equity)
            f["daily_loss"] = confirmed("daily_loss", loss, eqf.as_of_epoch,
                                        start_equity=anchor.start_equity,
                                        day=anchor.day)
    except Exception as exc:  # noqa: BLE001
        f["daily_loss"] = unavailable("daily_loss", f"unreadable: {type(exc).__name__}")

    # -- ledger -----------------------------------------------------------
    stmt = None
    fee_stmt = None
    try:
        rows = store.load_ledger()
        if rows is None:
            raise LookupError("no journal has been written against this store")
        journal = _ledger.Journal.from_rows(rows)
        stmt = journal.statement(now_ms=int(now * 1000))
        fee_stmt = (stmt if fee_window_from_ms is None
                    else journal.statement(from_ms=int(fee_window_from_ms),
                                           now_ms=int(now * 1000)))
    except Exception as exc:  # noqa: BLE001
        why = f"ledger unreadable: {type(exc).__name__}: {exc}"
        for name in ("realised_pnl", "fees_paid", "funding"):
            f[name] = unavailable(name, why)
    if stmt is not None:
        f["realised_pnl"] = (
            confirmed("realised_pnl", stmt["net_usd"], now,
                      net_basis=stmt["net_basis"])
            if stmt["net_basis"] in ("confirmed", "no_fills") else
            unavailable("realised_pnl", f"provisional: {stmt['net_basis']}",
                        as_of=now, provisional_net_usd=stmt["net_usd"],
                        net_basis=stmt["net_basis"]))
        f["fees_paid"] = (
            confirmed("fees_paid", stmt["fees_usd"], now)
            if stmt["fees_confirmed"] else
            unavailable("fees_paid", "fills with unknown/unresolved/unlabelled fee",
                        as_of=now, lower_bound_usd=stmt["fees_usd"],
                        fees_unknown=stmt["fees_unknown"],
                        fees_unresolved=stmt["fees_unresolved"],
                        fees_unlabelled=stmt["fees_unlabelled"]))
        f["funding"] = (
            confirmed("funding", stmt["funding_usd"], now)
            if stmt["funding_confirmed"] else
            unavailable("funding", "funding not venue-confirmed and reconciled",
                        as_of=now, provisional_usd=stmt["funding_usd"],
                        venue_usd=stmt["funding_venue_usd"],
                        estimated_usd=stmt["funding_estimated_usd"]))

    # Never-sourced today: named, not zeroed.
    f["fees_due"] = unavailable(
        "fees_due", "no authority states fees owed on open exposure; the engine's "
        "fee_reserve_usd is a pre-trade reserve, not a liability record")
    f["funding_accrued"] = unavailable(
        "funding_accrued", "no CarryBroker read states the next settlement's "
        "accrual; booked funding is in 'funding'")
    f["unrealised_pnl"] = unavailable(
        "unrealised_pnl", "no CarryBroker read states unrealisedPnl; the ledger "
        "deliberately does not value open positions")

    # -- venue ------------------------------------------------------------
    if venue is None:
        for name in ("available_capital", "quote_free", "spot_assets",
                     "linear_positions", "open_orders", "initial_margin",
                     "maintenance_margin", "liquidation_exposure",
                     "deployed_capital"):
            f[name] = unavailable(name, "no venue read was supplied")
    else:
        f["available_capital"] = venue.available_capital
        f["quote_free"] = venue.quote_free
        f["spot_assets"] = venue.spot_assets
        f["linear_positions"] = venue.linear_positions
        f["open_orders"] = venue.open_orders
        lv = venue.liquidation_view
        if lv.confirmed:
            v = lv.value
            im = v.get("position_im_usd")
            size = float(v.get("size") or 0.0)
            if size == 0.0:
                f["initial_margin"] = confirmed("initial_margin", 0.0, lv.as_of_epoch,
                                                note="venue states no position")
            elif im is None:
                f["initial_margin"] = unavailable("initial_margin",
                                                  "venue did not state positionIM",
                                                  as_of=lv.as_of_epoch)
            else:
                f["initial_margin"] = confirmed("initial_margin", float(im),
                                                lv.as_of_epoch)
            mm = v.get("account_mm_rate")
            f["maintenance_margin"] = (
                confirmed("maintenance_margin", float(mm), lv.as_of_epoch,
                          unit="rate", margin_mode=v.get("margin_mode"))
                if mm is not None else
                unavailable("maintenance_margin", "venue did not state accountMMRate "
                            f"(margin mode {v.get('margin_mode')!r}); no USD "
                            "maintenance-margin read exists", as_of=lv.as_of_epoch))
            f["liquidation_exposure"] = confirmed(
                "liquidation_exposure",
                {k: v.get(k) for k in ("size", "side", "mark", "liq_price",
                                       "distance_pct", "reason")},
                lv.as_of_epoch)
        else:
            for name in ("initial_margin", "maintenance_margin",
                         "liquidation_exposure"):
                f[name] = unavailable(name, lv.reason, as_of=lv.as_of_epoch)
        im_f, spot_f, mark_f = f["initial_margin"], venue.spot_assets, venue.spot_mark
        if im_f.confirmed and spot_f.confirmed and mark_f.confirmed:
            f["deployed_capital"] = confirmed(
                "deployed_capital", im_f.value + spot_f.value * mark_f.value,
                min(im_f.as_of_epoch, spot_f.as_of_epoch, mark_f.as_of_epoch),
                perp_im_usd=im_f.value, spot_qty=spot_f.value,
                spot_mark=mark_f.value)
        else:
            missing = [x.name for x in (im_f, spot_f, mark_f) if not x.confirmed]
            f["deployed_capital"] = unavailable(
                "deployed_capital", f"inputs unavailable: {missing}")

    # -- reservations -------------------------------------------------------
    if reservations is None:
        for name in ("reserved_capital", "per_strategy_risk", "total_risk"):
            f[name] = unavailable(name, "no CapitalReservations supplied")
    else:
        try:
            by = reservations.live_by_strategy(account_scope)
            total = sum(v["reserved_usd"] + v["committed_usd"] for v in by.values())
            f["reserved_capital"] = confirmed("reserved_capital", total, now,
                                              account_scope=account_scope)
            f["per_strategy_risk"] = confirmed(
                "per_strategy_risk", by, now,
                measure="capital held (reserved + committed) per strategy; the "
                        "only per-strategy risk record that exists")
            f["total_risk"] = confirmed("total_risk", total, now,
                                        measure="capital held, all strategies")
        except Exception as exc:  # noqa: BLE001
            for name in ("reserved_capital", "per_strategy_risk", "total_risk"):
                f[name] = unavailable(name, f"unreadable: {type(exc).__name__}")

    # -- contradictions ---------------------------------------------------
    tol = tolerances
    venue_perp = f["linear_positions"]
    engine_qty = _engine_perp_qty(book)
    ledger_qty = abs(float(stmt["open_perp_qty"])) if stmt is not None else None
    if venue_perp.confirmed and engine_qty is not None and \
            not tol.sizes_agree(engine_qty, venue_perp.value):
        contra.append(Contradiction("PERP_ENGINE_VS_VENUE", {
            "engine": engine_qty, "venue": venue_perp.value}))
    if venue_perp.confirmed and ledger_qty is not None and \
            not tol.sizes_agree(ledger_qty, venue_perp.value):
        contra.append(Contradiction("PERP_LEDGER_VS_VENUE", {
            "ledger": ledger_qty, "venue": venue_perp.value}))
    if engine_qty is not None and ledger_qty is not None and \
            not tol.sizes_agree(engine_qty, ledger_qty):
        contra.append(Contradiction("PERP_ENGINE_VS_LEDGER", {
            "engine": engine_qty, "ledger": ledger_qty}))
    if f["open_orders"].confirmed and f["unresolved_orders"].confirmed:
        known = {o["order_link_id"] for o in f["unresolved_orders"].value}
        stray = [o.get("orderLinkId") for o in f["open_orders"].value
                 if o.get("orderLinkId") not in known]
        if stray:
            contra.append(Contradiction("ORDER_LOG_VS_VENUE", {
                "open_at_venue_not_unresolved_in_log": stray}))
    av, qf = f["available_capital"], f["quote_free"]
    if av.confirmed and qf.confirmed and \
            qf.value > av.value * (1 + tol.balance_rel) + tol.balance_abs:
        contra.append(Contradiction("QUOTE_EXCEEDS_AVAILABLE", {
            "quote_free_usd": qf.value, "available_usd": av.value}))

    def mark_contradicted(name: str, code: str, detail: Dict[str, Any]) -> None:
        old = f[name]
        f[name] = Field(name, old.value, CONTRADICTED, old.source, old.as_of_epoch,
                        code, {**old.detail, **detail})
    for c in contra:
        if c.code.startswith("PERP_") and f["linear_positions"].status != UNAVAILABLE:
            mark_contradicted("linear_positions", c.code, c.detail)
        elif c.code == "ORDER_LOG_VS_VENUE":
            mark_contradicted("open_orders", c.code, c.detail)
        elif c.code == "QUOTE_EXCEEDS_AVAILABLE":
            mark_contradicted("available_capital", c.code, c.detail)

    # -- blockers -----------------------------------------------------------
    blockers: List[str] = []
    ks = f["kill_switch"]
    if not ks.confirmed:
        blockers.append(KILL_SWITCH_UNREADABLE)
    elif ks.value:
        blockers.append(KILL_SWITCH_ENGAGED)
    uo = f["unresolved_orders"]
    if uo.confirmed and uo.value:
        blockers.append(f"{UNRESOLVED_ORDERS}:{len(uo.value)}")
    eb = f["engine_book"]
    if eb.confirmed and str(eb.value.get("book_state")) == "HALTED":
        blockers.append(ENGINE_HALTED)
    for name in REQUIRED_FOR_ENTRY:
        fld = f[name]
        if fld.status == UNAVAILABLE and name != "kill_switch":
            blockers.append(f"{REQUIRED_UNAVAILABLE}:{name}")
    if not (math.isfinite(limits.max_state_age_s) and limits.max_state_age_s > 0):
        blockers.append(f"{LIMIT_EXCEEDED}:STATE_AGE_LIMIT_INVALID")
    else:
        for name in TIME_SENSITIVE:
            fld = f[name]
            if fld.status != UNAVAILABLE and fld.as_of_epoch is not None and \
                    now - fld.as_of_epoch > limits.max_state_age_s:
                blockers.append(f"{STALE_STATE}:{name}")
    if stmt is None:
        blockers.append(f"{REQUIRED_UNAVAILABLE}:ledger")
    if fee_stmt is not None and (fee_stmt["fees_unknown"] or fee_stmt["fees_unresolved"]
                                 or fee_stmt["fees_unlabelled"]):
        blockers.append(FEE_UNKNOWN)
    if stmt is not None:
        failed = [l["lifecycle_id"] for l in stmt["funding_lifecycles"]
                  if l["state"] in FAILED_RECONCILIATION_STATES]
        if failed:
            blockers.append(f"{RECONCILIATION_FAILED}:{','.join(failed)}")
    for c in contra:
        blockers.append(f"{CONTRADICTION}:{c.code}")
    dl, dd = f["daily_loss"], f["drawdown"]
    if dl.confirmed and not (dl.value < limits.max_daily_loss_fraction):
        blockers.append(f"{LIMIT_EXCEEDED}:DAILY_LOSS")
    if dd.confirmed and not (dd.value < limits.max_drawdown_fraction):
        blockers.append(f"{LIMIT_EXCEEDED}:DRAWDOWN")
    if limits.max_total_capital_usd is not None:
        rc, dc = f["reserved_capital"], f["deployed_capital"]
        if rc.confirmed and dc.confirmed:
            if not (rc.value + dc.value <= limits.max_total_capital_usd):
                blockers.append(f"{LIMIT_EXCEEDED}:TOTAL_CAPITAL")
        else:
            blockers.append(f"{REQUIRED_UNAVAILABLE}:total_capital_inputs")

    return PortfolioState(now, f, tuple(contra), limits, tuple(blockers))
