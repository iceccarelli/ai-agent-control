"""trading_engine.py — one order lifecycle, end to end.

WHAT REPLACED WHAT
==================
The as-received module (11,195 lines, retained at
``_dead/trading_engine_legacy.py``) contained eight engine classes, five mutually
inconsistent exit regimes, and a 213-line partial-take-profit implementation
accidentally indented **inside the body of a sleep helper**. None of the five
exit regimes could run:

* three were bound onto ``IntegratedTradingEngine`` by a patch block whose
  explicit method list omitted every stop-attachment method;
* one posted to ``/v5/position/trading-stop``, which does not support spot;
* one was the nested closure inside ``nb_sleep``, recreated and discarded on
  every call.

The only exit that actually ran was a software price-poll in ``main.py`` that
died with the process. **Every live entry was a naked market order.**

THE INVARIANT THIS MODULE EXISTS TO ENFORCE
-------------------------------------------
    A confirmed position always has a verified protective stop.

Concretely: entry fills -> stop is placed -> the stop is *read back* from the
exchange. If the stop cannot be placed and verified, the position is closed at
market immediately and the kill switch trips. There is no path that leaves a
filled position without a stop, and no path that reports success without
exchange confirmation.

WHAT WAS DELETED AND WHY
------------------------
* **The fallback market order.** Legacy ``execute_trade`` caught an exception
  from the TP/SL wrapper and placed a *second* full-size market order (legacy
  5016-5020). If the wrapper had failed after the entry filled, that doubled the
  position and left it unprotected.
* **``place_and_confirm_order``.** It re-placed the same order up to three times
  with no idempotency key, and could never confirm because it read the wrong
  response shape and queried only *open* orders (a filled market order is not
  one). It had zero callers, which is the only reason it never fired.
* **The second ``STRONG_BUY`` inversion.** Legacy line 5092 still read
  ``side = 'Buy' if signal_type == 'BUY' else 'Sell'``, so ``STRONG_BUY``
  executed as a **Sell**. One inversion had been patched; this one had not.
* **The ``min()`` threshold ratchets.** Legacy ``min(self.min_confidence,
  override)`` meant the stricter of two thresholds could never win.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import config as _config
from bybit_connection import (
    BybitAPIError,
    BybitClient,
    OrderResult,
    PermanentAPIError,
)
from persistence import StateStore, TradeRecord, utc_now_epoch
from position_sizing import (
    BillionairePositionSizing,
    InstrumentFilters,
    SizingRequest,
    SizingResult,
    snap_price,
    snap_qty_down,
)
from memory import TradingMemory
from risk_management import BillionaireRiskManager, normalize_side
import venue_fees
from venue_fees import FeeSet

logger = logging.getLogger("engine")

__all__ = [
    "TradingEngine",
    "BillionaireTradingEngine",
    "TradeIntent",
    "ExecutionReport",
    "ExitPlan",
    "get_position_tracker",
    "should_close_position",
]


# ---------------------------------------------------------------------------
# value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TradeIntent:
    """What the strategy wants to do, before any gating or sizing.

    ``side`` is derived once, here, from an explicit signal type — not
    re-derived at each stage with a different ``if/else`` (which is how the same
    codebase ended up with two contradictory ``STRONG_BUY`` mappings).
    """

    symbol: str
    signal_type: str
    entry_price: float
    stop_price: float
    take_profits: Tuple[Tuple[float, float], ...] = ()   # (price, size_fraction)
    confidence: Optional[float] = None
    #: A CALIBRATED probability that this trade reaches its target before its
    #: stop. Distinct from ``confidence``, which is a signal-strength score.
    #: ``None`` means "not estimated", and the edge gate then falls back to
    #: judging the reward leg alone against cost — which is stricter than any
    #: assumption it could make instead. Only a promoted model, or a
    #: calibration measured over realised outcomes, may fill this in.
    win_probability: Optional[float] = None
    reason: str = ""
    #: A hard ceiling on the order's notional (USD), set by a strategy whose
    #: contract caps its size (the shadow book's frozen 100 USD). Sizing can
    #: only be reduced to it, never raised, and a size that cannot meet the
    #: exchange minimum under it is skipped rather than rounded up.
    max_notional_usd: Optional[float] = None

    @property
    def side(self) -> Optional[str]:
        """Entry side. ``None`` for anything that is not an explicit buy or sell.

        HOLD is a first-class outcome and yields ``None`` — it does not fall
        through to a default direction.
        """
        token = str(self.signal_type).strip().upper()
        if token in {"BUY", "STRONG_BUY", "LONG"}:
            return "Buy"
        if token in {"SELL", "STRONG_SELL", "SHORT"}:
            return "Sell"
        return None

    @property
    def expected_take_profit(self) -> Optional[float]:
        """Size-weighted average of the take-profit ladder.

        This — not the first leg — is the trade's expected reward, and it is
        what the risk/reward and edge gates must measure. Judging a partial-TP
        ladder by its nearest leg understates the trade; judging it by its
        farthest leg overstates it. The weighted average is the only figure that
        corresponds to what the position is actually expected to realise.
        """
        if not self.take_profits:
            return None
        total_fraction = sum(f for _, f in self.take_profits)
        if total_fraction <= 0:
            return None
        return sum(price * f for price, f in self.take_profits) / total_fraction

    @property
    def exit_side(self) -> Optional[str]:
        side = self.side
        if side is None:
            return None
        return "Sell" if side == "Buy" else "Buy"


@dataclass(frozen=True)
class ExecutionReport:
    """Outcome of one attempt. ``ok`` defaults to False."""

    ok: bool = False
    stage: str = "not_started"
    reason: str = "NOT_ATTEMPTED"
    symbol: str = ""
    qty: float = 0.0
    entry_order_link_id: str = ""
    stop_order_link_id: str = ""
    take_profit_ids: Tuple[str, ...] = ()
    detail: Dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True)
class ExitPlan:
    """Where the protective stop and the take-profit legs sit."""

    stop_price: float
    legs: Tuple[Tuple[float, float], ...] = ()   # (price, fraction of qty)
    #: Distance from the planned entry to the stop, in price units. Carried
    #: explicitly because it is the RISK UNIT the position was sized against,
    #: and re-anchoring the bracket to an actual fill must preserve it rather
    #: than preserving the stop price. Zero means "not recorded", and
    #: ``_reanchor`` then leaves the plan alone rather than guessing.
    stop_distance: float = 0.0

    def validate(self, *, side: str, entry_price: float) -> Optional[str]:
        """Return a problem description, or None if the plan is coherent."""
        if self.stop_price <= 0:
            return "NO_STOP"
        if side == "Buy" and self.stop_price >= entry_price:
            return "STOP_ABOVE_ENTRY_FOR_LONG"
        if side == "Sell" and self.stop_price <= entry_price:
            return "STOP_BELOW_ENTRY_FOR_SHORT"
        total = sum(f for _, f in self.legs)
        if total > 1.0 + 1e-9:
            return f"TP_FRACTIONS_EXCEED_POSITION:{total}"
        for price, _ in self.legs:
            if side == "Buy" and price <= entry_price:
                return "TP_BELOW_ENTRY_FOR_LONG"
            if side == "Sell" and price >= entry_price:
                return "TP_ABOVE_ENTRY_FOR_SHORT"
        return None


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------


class TradingEngine:
    """Signal -> gate -> size -> entry -> verified bracket -> managed exit.

    One class, one path. Every dependency is injected so the whole lifecycle is
    testable offline.
    """

    def __init__(
        self,
        client: BybitClient,
        risk_manager: BillionaireRiskManager,
        position_sizer: Optional[BillionairePositionSizing] = None,
        store: Optional[StateStore] = None,
        config: Any = None,
        memory: Optional[TradingMemory] = None,
        **_ignored: Any,
    ) -> None:
        self.client = client
        self.risk = risk_manager
        self.store = store if store is not None else risk_manager.store
        self.sizer = position_sizer or BillionairePositionSizing(
            risk_manager=risk_manager
        )
        self.cfg = config if config is not None else _config.get_config_object()

        self.paper = bool(getattr(self.cfg, "PAPER_TRADING", True))
        self.min_confidence = float(getattr(self.cfg, "MIN_CONFIDENCE", 0.6))
        self.breakeven_after_first_tp = bool(
            getattr(self.cfg, "BREAKEVEN_AFTER_FIRST_TP", True)
        )
        #: Used only to apportion entry fees across partial exits. The exchange
        #: is the source of truth for fees actually charged; this is the
        #: estimate applied to the entry leg when a position closes in pieces.
        self.taker_fee_estimate = float(getattr(self.cfg, "TAKER_FEE", 0.001))
        self.prefer_maker = bool(getattr(self.cfg, "PREFER_MAKER", False))
        self.maker_timeout_s = float(getattr(self.cfg, "MAKER_TIMEOUT_SECONDS", 20.0))

        #: The shared reference layer. Injected so a backtest can pass one and a
        #: unit test can pass None. It is read for sizing and written after
        #: every fill; it can never relax a gate — see memory.size_multiplier.
        self.memory = memory
        if self.memory is None and bool(getattr(self.cfg, "MEMORY_ENABLED", False)):
            try:
                self.memory = TradingMemory(self.store, self.cfg)
            except Exception:  # noqa: BLE001
                # Losing the reference layer costs learning, not safety. It is
                # never on the path that decides whether a trade is allowed.
                logger.warning("memory layer unavailable; continuing without it")
                self.memory = None

    # -- market-condition inputs the gates need ---------------------------

    def _market_conditions(self, symbol: str) -> Dict[str, Any]:
        """Gather the per-symbol inputs the cost and liquidity gates consume.

        Kept in one method so both gate calls in :meth:`execute` see *the same*
        conditions. Fetching them twice would let a trade be sized against one
        spread and gated against another — a small window, and exactly the kind
        of small window that produces an unreproducible incident.

        Funding is fetched only on perpetuals. ``None`` is passed through rather
        than defaulted, so the gate blocks on an unknown carry instead of
        assuming a free one.
        """
        conditions: Dict[str, Any] = {"funding_rate": None, "book": None}
        try:
            conditions["funding_rate"] = self.client.get_funding_rate(symbol)
        except Exception:  # noqa: BLE001
            conditions["funding_rate"] = None
        book_source = getattr(self.client, "get_book_features", None)
        if callable(book_source):
            try:
                conditions["book"] = book_source(symbol)
                unavailable = getattr(self.client, "book_feed_unavailable", None)
                if (conditions["book"] is None and callable(unavailable)
                        and unavailable(symbol) is True):
                    conditions["book"] = {"unavailable": True}
            except Exception:  # noqa: BLE001
                conditions["book"] = {"unavailable": True}
        return conditions

    # -- the reference layer, applied in the only direction it may act -----

    def _apply_memory_throttle(
        self, symbol: str, sizing: SizingResult, filters: InstrumentFilters
    ) -> SizingResult:
        """Shrink a size according to what this symbol has actually done.

        The whole contract in one sentence: **this can only ever reduce.**
        ``memory.size_multiplier`` is clamped to ``[MEMORY_MIN_THROTTLE, 1.0]``
        at its source, and the multiplication here is the only operator applied
        to it — never a division, which would silently turn the same value into
        an enlargement.

        This is deliberately not a "confidence boost". A bot that grows its own
        size after a winning streak is a bot that reaches its largest position
        immediately before the streak ends. Experience is allowed to make it
        more careful and is not allowed to make it braver.
        """
        if self.memory is None:
            return sizing
        try:
            multiplier = float(self.memory.size_multiplier(symbol))
        except Exception:  # noqa: BLE001
            logger.warning("memory throttle unavailable for %s; sizing unchanged", symbol)
            return sizing
        if not (math.isfinite(multiplier)) or multiplier >= 1.0:
            # >= 1.0 is not trusted as an instruction to grow. It means
            # "no reduction warranted", and the size is left exactly as sized.
            return sizing
        if multiplier <= 0.0:
            # qty == 0 IS "do not trade" — SizingResult.should_trade is derived
            # from the quantity, deliberately, so there is no way to express a
            # result that says "trade" and carries no size.
            return replace(
                sizing, qty=0.0, reason="MEMORY_THROTTLE_TO_ZERO",
                detail={**dict(sizing.detail), "multiplier": multiplier},
            )

        throttled = float(snap_qty_down(sizing.qty * multiplier, filters.qty_step))
        if throttled <= 0.0 or throttled < float(filters.min_qty):
            return replace(
                sizing, qty=0.0,
                reason="MEMORY_THROTTLE_BELOW_MIN_QTY",
                detail={**dict(sizing.detail), "multiplier": round(multiplier, 4),
                        "throttled_qty": throttled},
            )
        logger.info("memory throttle %s: %.4f x %.8f -> %.8f",
                    symbol, multiplier, sizing.qty, throttled)
        return replace(
            sizing, qty=throttled,
            detail={**dict(sizing.detail), "memory_multiplier": round(multiplier, 4),
                    "qty_before_memory": sizing.qty},
        )

    @staticmethod
    def _allocate_legs(filled_qty: float, fractions: Sequence[float],
                       step: Any) -> List[float]:
        """Take-profit leg quantities: each snapped to the lot step HERE, the
        last taking the real residual when the legs are meant to cover the whole
        position. Leaving the snap to the client floors every leg and strands the
        floor losses (and, with float noise, a whole step)."""
        covers_all = abs(sum(fractions) - 1.0) <= 1e-9
        out: List[float] = []
        allocated = 0.0
        for index, fraction in enumerate(fractions):
            if index == len(fractions) - 1 and covers_all:
                qty = float(snap_qty_down(max(0.0, filled_qty - allocated), step))
            else:
                qty = float(snap_qty_down(filled_qty * fraction, step))
            out.append(qty)
            allocated += qty
        return out

    def _spot_sellable_qty(self, symbol: str, filled_qty: float,
                           entry_fees: Any, filters: InstrumentFilters) -> float:
        """What a spot buy left in the account (see ``venue_fees.spot_buy_net_qty``),
        snapped down to the lot step."""
        net = venue_fees.spot_buy_net_qty(symbol, filled_qty, entry_fees)
        if net <= 0.0:
            return 0.0
        return float(snap_qty_down(net, filters.qty_step))

    def _apply_notional_cap(
        self, intent: "TradeIntent", sizing: SizingResult,
        filters: InstrumentFilters,
    ) -> SizingResult:
        """Clamp the size to the strategy's own notional ceiling. Only reduces."""
        cap = intent.max_notional_usd
        if cap is None:
            return sizing
        price = float(intent.entry_price)
        try:
            cap = float(cap)
        except (TypeError, ValueError):
            cap = float("nan")
        if not (math.isfinite(cap) and cap > 0.0 and price > 0.0):
            return replace(sizing, qty=0.0, reason="NOTIONAL_CAP_INVALID",
                           detail={**dict(sizing.detail), "cap": intent.max_notional_usd})
        if sizing.qty * price <= cap:
            return sizing
        capped = float(snap_qty_down(cap / price, filters.qty_step))
        if (capped <= 0.0 or capped < float(filters.min_qty)
                or capped * price < float(filters.min_notional)):
            return replace(sizing, qty=0.0, reason="NOTIONAL_CAP_BELOW_MIN",
                           detail={**dict(sizing.detail), "cap_usd": cap,
                                   "capped_qty": capped})
        return replace(sizing, qty=capped,
                       detail={**dict(sizing.detail), "qty_before_cap": sizing.qty,
                               "notional_cap_usd": cap})

    def _remember_execution(self, **kwargs: Any) -> None:
        """Record what an order actually cost. Never raises into the trade path.

        Recording is bookkeeping: a failure here must not abort a trade that has
        already filled, and must not be mistaken for a risk event. It is logged
        and swallowed — the one category of exception this codebase is allowed
        to swallow, precisely because nothing downstream depends on it.
        """
        if self.memory is None:
            return
        try:
            self.memory.record_execution(**kwargs)
        except Exception:  # noqa: BLE001
            logger.warning("could not record execution quality for %s",
                           kwargs.get("symbol"), exc_info=True)

    # -- the one entry point ----------------------------------------------

    def _fee_leg(
        self, *, symbol: str, fees: FeeSet, price: float, qty: float,
        caller_amount: float = 0.0, ref: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """One fee leg, in the symbol's quote coin, with its provenance.

        ``realized`` is True only when the venue stated the fee AND every
        currency in it converted with a recorded basis (identity, or base coin x
        this leg's own price). Otherwise the amount is NOT realised: it is the
        caller's figure if one was given, else a static taker-rate estimate, and
        ``raw`` says which. The raw venue amounts, currencies, source field and
        the order identity they came from are always kept.
        """
        base, unit = venue_fees.split_symbol_units(symbol)
        conv = venue_fees.convert_fee_set(
            fees, leg_price=price, qty=qty, accounting_unit=unit, base_coin=base)
        raw = conv.as_dict()
        raw["unit"] = unit
        raw["ref"] = dict(ref or {})
        if conv.confirmed:
            return {"amount": float(conv.account_unit_amount), "realized": True,
                    "currency": unit, "raw": raw}
        if caller_amount:
            amount, raw["substitute"] = float(caller_amount), "caller_supplied"
        else:
            amount = venue_fees.estimate_leg_fee(
                price=price, qty=qty, taker_fee=self.taker_fee_estimate)
            raw["substitute"] = "static_estimate"
        return {"amount": amount, "realized": False, "currency": None, "raw": raw}

    def execute(self, intent: TradeIntent) -> ExecutionReport:
        """Run one trade attempt through every stage, in order.

        Each stage can only stop the trade. There is no stage that rescues a
        failed earlier stage by retrying it differently — that is what produced
        the legacy double-submit.
        """
        symbol = intent.symbol
        side = intent.side
        if side is None:
            # HOLD, or an unrecognised signal type. Not an error, not a trade.
            return self._report(
                stage="signal", reason=f"NO_TRADEABLE_SIDE:{intent.signal_type!r}",
                symbol=symbol,
            )

        # -- 1. equity ----------------------------------------------------
        try:
            equity = self.client.get_equity()
        except BybitAPIError as exc:
            return self._report(stage="equity", reason=f"EQUITY_UNREADABLE: {exc}",
                                symbol=symbol)
        if equity <= 0:
            return self._report(stage="equity", reason="EQUITY_NON_POSITIVE",
                                symbol=symbol)
        self.risk.update_equity(equity)

        # -- 2. filters ---------------------------------------------------
        try:
            filters = self.client.get_instrument_filters(symbol)
        except BybitAPIError as exc:
            return self._report(stage="filters", reason=f"NO_FILTERS: {exc}",
                                symbol=symbol)

        # -- 3. live quote: spread and staleness are gate inputs -----------
        try:
            quote = self.client.get_quote(symbol)
        except BybitAPIError as exc:
            # No quote means the spread and freshness gates cannot be evaluated.
            # That is a block, not a licence to skip them.
            return self._report(stage="quote", reason=f"NO_QUOTE: {exc}",
                                symbol=symbol)

        # -- 4. pre-trade risk gate (size-independent) --------------------
        # Read ONCE and reused by both gate calls, so the trade is sized and
        # gated against identical market conditions.
        conditions = self._market_conditions(symbol)
        pre = self.risk.gate_order(
            symbol=symbol, side=side, entry_price=intent.entry_price,
            stop_loss=intent.stop_price, quantity=0.0, account_equity=equity,
            confidence=intent.confidence,
            take_profit=intent.expected_take_profit,
            win_probability=intent.win_probability,
            quote=quote, data_age_seconds=quote.get("age_seconds"),
            **conditions,
        )
        if not pre.ok:
            return self._report(stage="pre_gate", reason=pre.reason, symbol=symbol,
                                detail=dict(pre.detail))

        # -- 4. size ------------------------------------------------------
        stop_fraction = abs(intent.entry_price - intent.stop_price) / intent.entry_price
        sizing = self.sizer.calculate_position_size(SizingRequest(
            symbol=symbol, side=side, current_price=intent.entry_price,
            current_portfolio_value=equity, stop_loss_distance=stop_fraction,
            filters=filters, signal_confidence=intent.confidence or 0.0,
        ))
        if not sizing.should_trade:
            return self._report(stage="sizing", reason=sizing.reason, symbol=symbol,
                                detail=dict(sizing.detail))

        # -- 4b. what this symbol has actually cost us before ---------------
        # Recorded experience can only make the position SMALLER. It is applied
        # after sizing and before the final gate, so the gate sees the quantity
        # that will actually be sent. If the throttle pushes the size below the
        # exchange minimum the trade is skipped rather than rounded back up —
        # rounding up is how a "reduce risk" mechanism ends up increasing it.
        sizing = self._apply_memory_throttle(symbol, sizing, filters)
        if not sizing.should_trade:
            return self._report(stage="sizing", reason=sizing.reason, symbol=symbol,
                                detail=dict(sizing.detail))
        sizing = self._apply_notional_cap(intent, sizing, filters)
        if not sizing.should_trade:
            return self._report(stage="sizing", reason=sizing.reason, symbol=symbol,
                                detail=dict(sizing.detail))

        # -- 5. final risk gate, now with the actual size -----------------
        final = self.risk.gate_order(
            symbol=symbol, side=side, entry_price=intent.entry_price,
            stop_loss=intent.stop_price, quantity=sizing.qty,
            account_equity=equity, confidence=intent.confidence,
            take_profit=intent.expected_take_profit,
            win_probability=intent.win_probability,
            quote=quote, data_age_seconds=quote.get("age_seconds"),
            **conditions,
        )
        if not final.ok:
            return self._report(stage="final_gate", reason=final.reason,
                                symbol=symbol, detail=dict(final.detail))

        # -- 6. validate the exit plan BEFORE entering --------------------
        plan = ExitPlan(
            stop_price=intent.stop_price, legs=intent.take_profits,
            stop_distance=abs(intent.entry_price - intent.stop_price),
        )
        problem = plan.validate(side=side, entry_price=intent.entry_price)
        if problem:
            # Refusing here is the cheap version of the invariant: never open a
            # position whose exit plan is already known to be incoherent.
            return self._report(stage="exit_plan", reason=problem, symbol=symbol)

        if self.paper:
            self.store.journal(symbol, "PAPER", "would_enter", {
                "side": side, "qty": sizing.qty, "entry": intent.entry_price,
                "stop": intent.stop_price,
            })
            return self._report(ok=True, stage="paper", reason="PAPER_OK",
                                symbol=symbol, qty=sizing.qty)

        # -- 7. entry -----------------------------------------------------
        entry = self._enter(
            symbol=symbol, side=side, qty=sizing.qty,
            filters=filters, quote=quote,
        )
        if not entry.ok:
            if entry.reason not in ("OUTCOME_UNKNOWN", "DUPLICATE_INTENT"):
                # A refused entry starts the short cooldown the risk manager
                # already defines for it; otherwise the same refused order is
                # re-sent (and re-recorded) every tick.
                self.risk.note_submit_result(accepted=False, symbol=symbol)
            return self._report(stage="entry", reason=entry.reason, symbol=symbol,
                                detail={"ret_code": entry.ret_code},
                                entry_order_link_id=entry.order_link_id)

        # -- 8. confirm the fill ------------------------------------------
        fill = self._await_fill(symbol, entry.order_link_id)
        if fill is None:
            # We do not know whether we are in the market. Do not enter again;
            # let reconciliation resolve it and stop trading this symbol.
            self.risk.set_cooldown(symbol)
            return self._report(stage="fill", reason="FILL_UNCONFIRMED",
                                symbol=symbol, entry_order_link_id=entry.order_link_id)

        # A missing/zero cumExecQty on a nominally terminal "Filled" status is
        # a venue-response anomaly, not evidence of a full fill: it used to
        # default to the REQUESTED qty (sizing.qty), which made the ZERO_FILL
        # check below unreachable and risked opening a position sized from
        # intent rather than from what the exchange actually reports filled.
        # The exit path (below) already defaults to 0.0 for the same field;
        # this makes entry consistent with it.
        filled_qty = float(fill.get("cumExecQty") or 0.0)
        avg_price = float(fill.get("avgPrice") or intent.entry_price)
        # The venue's own fee for this fill, amount AND currency, kept raw in
        # the position row so the eventual exit(s) can book a realised entry
        # fee. ``None`` means the venue did not say -- not that it was zero.
        entry_fees = venue_fees.extract_order_fees(fill)
        entry_meta = {"entry_fees": entry_fees.to_dict(),
                      "entry_fee_ref": {
                          "order_link_id": entry.order_link_id,
                          "order_id": str(fill.get("orderId") or "")}}
        if filled_qty <= 0:
            return self._report(stage="fill", reason="ZERO_FILL", symbol=symbol,
                                entry_order_link_id=entry.order_link_id)

        # On spot the fee of a BUY is taken in the BASE coin (the venue's own
        # testnet record: cumExecQty 0.002344 BTC, fee 0.000002344 BTC), so the
        # account holds less than cumExecQty. A stop or take-profit sized to the
        # gross fill asks to sell coin that is not there. Everything below
        # (the position row, the stop, the legs) is sized to what is held.
        if (not getattr(self.client, "is_linear", False)
                and normalize_side(side) == "Buy"):
            filled_qty = self._spot_sellable_qty(symbol, filled_qty, entry_fees,
                                                 filters)
            if filled_qty < float(filters.min_qty):
                # Held, but too little to sell or to protect: dust, booked as
                # such rather than forgotten (the same rule a partial exit's
                # remainder follows).
                try:
                    self.store.add_dust(symbol, float(fill.get("cumExecQty") or 0.0))
                except Exception:  # noqa: BLE001
                    logger.warning("could not record dust for %s", symbol,
                                   exc_info=True)
                self.store.journal(symbol, "ENTRY_DUST", "net of fee below minimum", {
                    "gross_filled": float(fill.get("cumExecQty") or 0.0)})
                filled_qty = 0.0
                self.store.update_order_status(entry.order_link_id, "filled")
            if filled_qty <= 0:
                return self._report(stage="fill", reason="ZERO_SELLABLE",
                                    symbol=symbol,
                                    entry_order_link_id=entry.order_link_id)

        self.store.upsert_position(
            symbol, side, filled_qty, avg_price, stop_price=0.0,
            order_link_id=entry.order_link_id, meta=entry_meta,
        )
        self.store.update_order_status(entry.order_link_id, "filled")

        # What the entry ACTUALLY cost against what the strategy assumed. This
        # is the measurement that tells an operator whether SLIPPAGE_BPS is a
        # number or a wish. It is recorded, never acted on automatically.
        self._remember_execution(
            symbol=symbol, side=side, intended_price=intent.entry_price,
            fill_price=avg_price, qty=filled_qty,
            order_link_id=entry.order_link_id, purpose="entry",
            is_maker=bool(entry.raw.get("maker_fill")),
        )

        # -- 9. re-anchor the bracket to the ACTUAL fill ------------------
        # The plan was built from the signal bar's close. The fill happens on
        # the next bar and can be anywhere. When the market gaps through the
        # planned stop, placing that stop unmodified means placing a stop the
        # position is already beyond: it fires immediately, and the trade is a
        # guaranteed -1R that no signal quality can rescue.
        #
        # Found by the slice-9 R-multiple instrumentation, which reported a
        # long filled at 10,501 with its protective stop at 10,593 — 92 dollars
        # the wrong side of its own entry. Neither the strategy nor the gate
        # could see it, because both had already run against a price that was
        # correct when they ran.
        plan = self._reanchor(plan, side=side, fill_price=avg_price)
        problem = plan.validate(side=side, entry_price=avg_price)
        if problem:
            # The fill has invalidated the exit plan and it cannot be repaired
            # within the risk budget. Closing at market here costs the spread;
            # the alternative is holding a position whose stop is a fiction.
            return self._emergency_close(
                symbol=symbol, exit_side=intent.exit_side or "Sell",
                qty=filled_qty, filters=filters,
                reason=f"FILL_INVALIDATED_PLAN:{problem}",
                entry_id=entry.order_link_id,
            )

        # -- protective stop, then VERIFY it ------------------------------
        protected = self._protect(
            symbol=symbol, exit_side=intent.exit_side or "Sell",
            qty=filled_qty, stop_price=plan.stop_price, filters=filters,
        )
        if protected is None:
            # The invariant is broken. Close at market, trip the kill switch,
            # and stop. A human clears it.
            return self._emergency_close(
                symbol=symbol, exit_side=intent.exit_side or "Sell",
                qty=filled_qty, filters=filters,
                reason="BRACKET_FAILED", entry_id=entry.order_link_id,
            )

        self.store.set_position_stop(symbol, plan.stop_price)
        # Remember which order is the protective stop, so a partial exit can
        # resize it. Without this the stop stays sized for the ORIGINAL
        # quantity and the exchange rejects it when it triggers.
        self.store.upsert_position(
            symbol, side, filled_qty, avg_price, stop_price=plan.stop_price,
            order_link_id=entry.order_link_id,
            meta={"stop_order_link_id": protected, **entry_meta},
        )

        # -- 10. take-profit legs (best effort; the stop is what matters) --
        tp_ids: List[str] = []
        # Leg quantities must sum EXACTLY to the filled quantity. Sizing each
        # leg independently and letting the exchange snap it down leaves a
        # remainder that is too small to sell or protect — dust that the
        # position book then has to reason about. Giving the final leg the
        # residual removes the problem at source.
        leg_quantities = self._allocate_legs(
            filled_qty, [f for _, f in plan.legs], filters.qty_step)

        for (price, _fraction), leg_qty in zip(plan.legs, leg_quantities):
            leg = self.client.place_take_profit(
                symbol=symbol, side=intent.exit_side or "Sell",
                qty=leg_qty, limit_price=price, filters=filters,
            )
            if leg.ok:
                tp_ids.append(leg.order_link_id)
            elif leg.reason == "OUTCOME_UNKNOWN" and leg.order_link_id:
                # It may be resting at the venue: track it, so a fill of it is
                # observed and booked and a close/emergency cancels it.
                tp_ids.append(leg.order_link_id)
                logger.warning("take-profit leg %s for %s has an unknown "
                               "outcome; tracked", leg.order_link_id, symbol)
            else:
                # A missing TP leg costs upside, not capital. Logged loudly and
                # carried on; a missing STOP is what triggers the emergency path.
                logger.warning("take-profit leg rejected for %s at %s: %s",
                               symbol, price, leg.reason)

        if tp_ids:
            # Spot exits are observed from these orders (there is no venue
            # position to read), so the row has to name them.
            self.store.upsert_position(
                symbol, side, filled_qty, avg_price, stop_price=plan.stop_price,
                order_link_id=entry.order_link_id,
                meta={"stop_order_link_id": protected, **entry_meta,
                      "tp_order_ids": list(tp_ids)},
            )

        self.store.journal(symbol, "ENTERED", "bracketed", {
            "qty": filled_qty, "avg_price": avg_price,
            "stop": plan.stop_price, "tp_legs": len(tp_ids),
        })
        return self._report(
            ok=True, stage="complete", reason="ENTERED_AND_PROTECTED",
            symbol=symbol, qty=filled_qty,
            entry_order_link_id=entry.order_link_id,
            stop_order_link_id=protected,
            take_profit_ids=tuple(tp_ids),
            detail={"avg_price": avg_price},
        )

    # -- entry execution ---------------------------------------------------

    def _enter(
        self, *, symbol: str, side: str, qty: float,
        filters: InstrumentFilters, quote: Mapping[str, Any],
    ) -> OrderResult:
        """Submit the entry, maker-first when configured.

        **Why bother.** On Bybit spot a taker pays ~10 bps and a maker pays ~10
        bps too — but a maker order rests at the *passive* side of the book, so
        it also avoids crossing the spread. On a 4 bps spread that is ~2 bps
        saved per side; on a strategy taking hundreds of round trips it is the
        difference between a thin edge and no edge. The cost gate already
        refuses trades that cannot clear the round trip, so every basis point
        recovered here widens the set of trades worth taking.

        **Why it is off by default.** A resting order can go unfilled while the
        market leaves, which turns a small guaranteed cost into an occasional
        large opportunity cost. Whether that trade-off is worth it depends on
        the strategy's holding period, and that is a measurement, not an
        assumption — so ``PREFER_MAKER`` defaults to off and the backtester can
        settle it.

        Falls back to a taker order if the maker order does not fill within
        ``MAKER_TIMEOUT_SECONDS``. The fallback **cancels the resting order
        first and re-reads the filled quantity**, so the two can never both be
        live — that is the double-execution failure the legacy engine had.
        """
        if not self.prefer_maker:
            return self.client.place_order(
                symbol=symbol, side=side, qty=qty,
                order_type="Market", purpose="entry", filters=filters,
            )

        # Rest at the passive side: a Buy joins the bid, a Sell joins the ask.
        passive_price = float(quote["bid"] if side == "Buy" else quote["ask"])
        maker = self.client.place_order(
            symbol=symbol, side=side, qty=qty, order_type="Limit",
            price=passive_price, time_in_force="PostOnly",
            purpose="entry", filters=filters,
        )
        if not maker.ok:
            if maker.reason in {"OUTCOME_UNKNOWN", "DUPLICATE_INTENT"}:
                # The maker order may be resting at the venue. A taker beside
                # it would be a second entry; report the unknown and let the
                # unresolved-entry gate hold the symbol until it is resolved.
                logger.error("maker entry for %s has an unknown outcome (%s); "
                             "no taker is sent", symbol, maker.reason)
                return maker
            logger.info("maker entry rejected for %s (%s); using taker",
                        symbol, maker.reason)
            return self.client.place_order(
                symbol=symbol, side=side, qty=qty,
                order_type="Market", purpose="entry", filters=filters,
            )

        filled = self._await_fill(symbol, maker.order_link_id,
                                  timeout_s=self.maker_timeout_s)
        if filled is not None:
            return replace(maker, raw={**maker.raw, "maker_fill": True})

        # Not filled in time. Cancel FIRST, then decide what is left to do.
        self.client.cancel_order(symbol=symbol, order_link_id=maker.order_link_id)
        residual = self.client.get_order(
            symbol=symbol, order_link_id=maker.order_link_id
        )
        status = str((residual or {}).get("orderStatus", ""))
        if status not in {"Filled", "Cancelled", "Rejected", "Deactivated",
                          "PartiallyFilledCanceled"}:
            # The cancel did not take, or the order cannot be read: it may
            # still be resting or filling. Do not send a taker beside it.
            logger.error("maker entry %s for %s is not confirmed cancelled "
                         "(%r); no taker is sent", maker.order_link_id, symbol,
                         status or "unread")
            return OrderResult(ok=False, order_link_id=maker.order_link_id,
                               reason="MAKER_ENTRY_UNRESOLVED")
        already = float((residual or {}).get("cumExecQty") or 0.0)
        if already <= 0.0 and status in ("Cancelled", "Rejected", "Deactivated"):
            # Verified at the venue: nothing executed. (cancel_order leaves an
            # entry row for the venue's record to settle.)
            self.store.update_order_status(maker.order_link_id, status.lower())
        if already > 0.0:
            # Part of the maker order executed. That lot is what the book
            # must carry (the entry is confirmed from this order); a taker for
            # the rest would leave the maker lot unrecorded and unprotected.
            logger.info("maker entry partially filled %s of %s on %s; "
                        "keeping the partial fill", already, qty, symbol)
            return replace(maker, raw={**maker.raw, "maker_fill": True})
        return self.client.place_order(
            symbol=symbol, side=side, qty=qty,
            order_type="Market", purpose="entry", filters=filters,
        )

    # -- stages ------------------------------------------------------------

    def _await_fill(
        self, symbol: str, order_link_id: str, timeout_s: float = 10.0
    ) -> Optional[Dict[str, Any]]:
        """Poll until the entry reaches a terminal state. None if unresolved.

        Uses ``perf_counter`` for elapsed time — which is what it is for — and
        actually sleeps between polls. The legacy confirm loop used a sleep
        helper that returned immediately, so it hot-spun for its whole window.
        """
        deadline = time.perf_counter() + timeout_s
        delay = 0.2
        while time.perf_counter() < deadline:
            try:
                order = self.client.get_order(
                    symbol=symbol, order_link_id=order_link_id
                )
            except BybitAPIError as exc:
                logger.warning("fill poll failed for %s: %s", order_link_id, exc)
                order = None
            if order:
                status = str(order.get("orderStatus", ""))
                if status == "Filled":
                    self._record_fill_status(order_link_id)
                    return order
                if (status in ("PartiallyFilledCanceled", "Cancelled", "Deactivated")
                        and float(order.get("cumExecQty") or 0.0) > 0.0):
                    # Terminal, and part of it executed: the venue holds that
                    # quantity, so it is a fill of the executed size -- not an
                    # "unconfirmed" order that leaves exposure nobody tracks.
                    self._record_fill_status(order_link_id)
                    return order
                if status in {"Rejected", "Cancelled", "Deactivated"}:
                    self.store.update_order_status(order_link_id, status.lower())
                    return None
            time.sleep(delay)
            delay = min(1.0, delay * 1.5)
        return None

    def _record_fill_status(self, order_link_id: str) -> None:
        """Mark a fill-polled order filled -- except an ENTRY, which is marked
        only once its position row exists (``execute`` does that), so a failure
        between the two cannot leave a terminal order and no position."""
        row = self.store.get_order(order_link_id)
        if row is not None and str(row.get("purpose")) == "entry":
            return
        self.store.update_order_status(order_link_id, "filled")

    def _reanchor(self, plan: ExitPlan, *, side: str, fill_price: float) -> ExitPlan:
        """Shift the bracket so it is measured from the price we actually paid.

        The stop distance is preserved, not the stop price. That is the whole
        decision, and it is the conservative one:

        * Preserving the *distance* keeps the risk unit the trade was sized
          against, so the position's R stays what the sizer intended. The stop
          moves with the fill.
        * Preserving the *price* would mean a favourable gap silently widens
          the real risk and an adverse gap puts the stop on the wrong side of
          entry — the defect this method exists to remove.

        Take-profit legs are shifted by the same offset. Rescaling them instead
        would change the trade's reward-to-risk after the gate approved it,
        which is the sort of quiet re-specification that makes a backtest and a
        live account disagree.

        A fill that is not usable leaves the plan untouched; ``validate`` then
        rejects it at the call site rather than this method inventing a repair.
        """
        try:
            fill = float(fill_price)
        except (TypeError, ValueError):
            return plan
        if not math.isfinite(fill) or fill <= 0.0 or plan.stop_price <= 0.0:
            return plan

        # The distance is taken from the plan, whose entry was the signal bar's
        # close. Recovering that close from the plan itself is not possible, so
        # the offset is expressed against the stop directly.
        if not plan.stop_distance > 0.0:
            return plan          # no stated risk unit: nothing to re-anchor to
        direction = 1.0 if normalize_side(side) == "Buy" else -1.0
        planned_entry = plan.stop_price + direction * abs(plan.stop_distance)
        offset = fill - planned_entry
        if abs(offset) < 1e-12:
            return plan

        legs = tuple((price + offset, fraction) for price, fraction in plan.legs)
        moved = ExitPlan(stop_price=plan.stop_price + offset, legs=legs,
                         stop_distance=plan.stop_distance)
        logger.info(
            "bracket re-anchored: fill %.8f vs planned %.8f (offset %+.8f); "
            "stop %.8f -> %.8f, distance preserved",
            fill, planned_entry, offset, plan.stop_price, moved.stop_price,
        )
        return moved

    def _protect(
        self,
        *,
        symbol: str,
        exit_side: str,
        qty: float,
        stop_price: float,
        filters: InstrumentFilters,
    ) -> Optional[str]:
        """Place the protective stop and read it back. Returns its id, or None.

        Reading it back is the point. The legacy code posted stops into
        ``except: pass`` blocks against an endpoint that does not exist for
        spot, so "stop placed" was never once verified against the exchange.
        """
        stop = self.client.place_stop_order(
            symbol=symbol, side=exit_side, qty=qty,
            trigger_price=stop_price, filters=filters,
        )
        if not stop.ok:
            logger.critical("stop placement REJECTED for %s: %s", symbol, stop.reason)
            return None

        # The read-back dispatches by category inside the client: a spot stop is
        # an order, a linear stop is a field on the position. The engine asks the
        # one question it cares about — "is the stop live?" — and does not need
        # to know which venue it is on to ask it.
        live, detail = self.client.verify_stop(
            symbol=symbol, order_link_id=stop.order_link_id
        )
        if not live:
            logger.critical("stop for %s is not live: %s", symbol, detail)
            return None
        logger.info("stop verified for %s (%s)", symbol, detail)
        return stop.order_link_id

    def _emergency_close(
        self,
        *,
        symbol: str,
        exit_side: str,
        qty: float,
        filters: InstrumentFilters,
        reason: str,
        entry_id: str = "",
    ) -> ExecutionReport:
        """Close at market and trip the kill switch.

        The bot may trip the kill switch; only a human clears it (see
        ``StateStore.clear_kill_switch_by_human``). There is deliberately no
        automatic recovery from here.
        """
        logger.critical("EMERGENCY CLOSE %s (%s)", symbol, reason)
        if not getattr(self.client, "is_linear", False):
            # A resting spot limit sell reserves the coin it offers, so the
            # emergency market sell has nothing free to sell while a
            # take-profit leg rests (close_position frees it first for the
            # same reason). The stop stays; a missing take-profit costs upside.
            self._cancel_take_profit_legs(symbol)
        closed = self.client.place_order(
            symbol=symbol, side=exit_side, qty=qty, order_type="Market",
            purpose="emergency_close", filters=filters, reduce_only=True,
        )
        self.risk.trip_kill_switch(f"{reason} on {symbol}")
        self.store.journal(symbol, "EMERGENCY", reason, {
            "close_ok": closed.ok, "close_reason": closed.reason,
        })
        if closed.ok:
            # The position is gone from the venue, so it must leave the book AS
            # A TRADE: before this, an emergency exit removed the row and booked
            # nothing, so the loss (and both fees) never reached the trades
            # table, the daily anchor or the loss streak.
            #
            # Booking is best effort and NEVER allowed to derail the flatten:
            # the venue position is already closed, so whatever happens here the
            # row must leave the book. A failure is loud and journaled so the
            # unbooked exit is a known gap, not a silent one.
            keep_row = False
            row = {p["symbol"]: p for p in self.store.open_positions()}.get(symbol)
            if row is not None:
                try:
                    try:
                        fill = self._await_fill(symbol, closed.order_link_id)
                    except Exception:  # noqa: BLE001
                        fill = None    # booked below as an UNCONFIRMED exit
                    executed = float((fill or {}).get("cumExecQty") or 0.0)
                    held = float(row["qty"])
                    tol = max(1e-9, held * 1e-9)
                    if fill is None or executed <= tol:
                        # The order was ACCEPTED; nothing says it sold. A
                        # rejected or unresolved reduce-only order leaves the
                        # position open: keep the row AND its protective
                        # orders, and let the venue-truth observers book the
                        # exit if it did execute. (The kill switch is already
                        # tripped, so nothing new can open meanwhile.)
                        logger.critical(
                            "emergency close of %s is UNCONFIRMED (no executed "
                            "quantity for %s): the position row and protective "
                            "orders are kept", symbol, closed.order_link_id)
                        self.store.journal(
                            symbol, "EMERGENCY_UNCONFIRMED", reason,
                            {"exit_order_link_id": closed.order_link_id,
                             "exit_order_id": closed.order_id})
                        keep_row = True
                    elif executed < held - tol:
                        # Only part of it sold: book what sold and keep the rest on
                        # the book, rather than booking the whole row as gone.
                        avg = self._fill_price(fill)
                        self.record_exit_fill(
                            symbol=symbol, qty=executed,
                            price=avg or float(row["entry_price"]),
                            price_provisional=avg <= 0.0,
                            fee=0.0, reason=f"EMERGENCY:{reason}:PARTIAL",
                            order_link_id=closed.order_link_id,
                            dedupe_key=f"exit:{closed.order_link_id}",
                            fees=venue_fees.extract_order_fees(fill))
                        logger.critical(
                            "emergency close of %s only partly executed (%s of "
                            "%s); the remainder is still held", symbol, executed, held)
                        keep_row = True
                    else:
                        self._book_full_exit(
                            symbol=symbol, row=row, exit_order=closed, fill=fill,
                            reason=f"EMERGENCY:{reason}")
                        self._cancel_protective_orders(symbol)
                except Exception as exc:  # noqa: BLE001
                    logger.critical(
                        "emergency exit of %s could not be booked as a trade "
                        "(%s); the position is closed at the venue and is being "
                        "removed from the book UNBOOKED", symbol, exc)
                    self.store.journal(symbol, "EMERGENCY_UNBOOKED", reason,
                                       {"error": f"{type(exc).__name__}: {exc}"})
            if not keep_row:
                self.store.remove_position(symbol)
        else:
            logger.critical(
                "EMERGENCY CLOSE FAILED for %s (%s) — position may be naked and "
                "unprotected; human intervention required", symbol, closed.reason
            )
        return self._report(
            stage="emergency", reason=reason, symbol=symbol, qty=qty,
            entry_order_link_id=entry_id,
            detail={"closed": closed.ok, "close_reason": closed.reason,
                    "kill_switch": True},
        )

    def _settle_pending_closes(self, symbol: str,
                               row: Mapping[str, Any]) -> Optional[ExecutionReport]:
        """Never send a second close beside one whose outcome is unknown.

        A close whose reply was lost (OUTCOME_UNKNOWN) leaves its order row
        non-terminal; the next cycle used to mint a NEW link id and sell again,
        and on spot (no reduce-only, other base coin may be held) the second
        sale could take coin that is not the position's. Each such order is
        asked of the venue first: an unreadable or still-live one blocks the
        new close; one that executed is booked from its own fill (provisional
        if the venue states no price); one that ended with nothing executed is
        settled and the close may proceed. Returns a report to stop with, or
        None to go on.
        """
        try:
            pending = [o for o in self.store.unresolved_orders()
                       if str(o.get("symbol")) == symbol
                       and str(o.get("purpose")) in ("close", "emergency_close")]
        except Exception:  # noqa: BLE001
            logger.exception("could not list pending closes for %s", symbol)
            return self._report(stage="close", reason="CLOSE_PENDING_UNREADABLE",
                                symbol=symbol)
        for order_row in pending:
            link = str(order_row.get("order_link_id") or "")
            if float(order_row.get("created_epoch") or 0.0) < float(
                    row.get("opened_epoch") or 0.0):
                # Sent before THIS position was opened: it belongs to an
                # earlier one (already booked or gone). Replaying its fill
                # against this position would book a sale it never made.
                self.store.update_order_status(link, "stale")
                self.store.journal(symbol, "STALE_CLOSE_ORDER_SETTLED", link, {})
                continue
            try:
                order = self.client.get_order(symbol=symbol, order_link_id=link)
            except BybitAPIError as exc:
                logger.error("pending close %s unreadable: %s", link, exc)
                return self._report(stage="close", reason="CLOSE_PENDING",
                                    symbol=symbol, detail={"exit_order_link_id": link})
            status = str((order or {}).get("orderStatus", ""))
            executed = float((order or {}).get("cumExecQty") or 0.0)
            if order and status in ("Filled", "PartiallyFilledCanceled",
                                    "Cancelled", "Deactivated") and executed > 0.0:
                price = float(order.get("avgPrice") or 0.0)
                qty = min(executed, float(row["qty"]))
                self.record_exit_fill(
                    symbol=symbol, qty=qty, price=price, fee=0.0,
                    reason="CLOSE_RECONCILED", order_link_id=link,
                    dedupe_key=f"exit:{link}",
                    fees=venue_fees.extract_order_fees(order),
                    fee_ref={"order_link_id": link,
                             "order_id": str(order.get("orderId") or "")},
                    price_provisional=price <= 0.0)
                self.store.update_order_status(link, "filled")
                return self._report(
                    stage="close", reason="CLOSE_RECONCILED", symbol=symbol,
                    detail={"exit_order_link_id": link, "executed_exit_qty": qty})
            if order and status in ("Cancelled", "Rejected", "Deactivated"):
                self.store.update_order_status(link, status.lower())
                continue
            # no row at the venue, or still live: whether it will sell is unknown
            return self._report(stage="close", reason="CLOSE_PENDING",
                                symbol=symbol, detail={"exit_order_link_id": link,
                                                       "status": status or "unread"})
        return None

    def _cooldown_after_loss(self, symbol: str) -> None:
        """Start the symbol cooldown when the position that just closed lost.

        The most recent position in the trades table is the one just booked, so
        a streak of at least one means it closed at or below zero net. Uses the
        existing TRADE_COOLDOWN_SECONDS; never raises into the exit path.
        """
        try:
            if self.store.consecutive_losses() >= 1:
                self.risk.set_cooldown(symbol)
        except Exception:  # noqa: BLE001
            logger.debug("loss cooldown not set for %s", symbol, exc_info=True)

    # -- stop management ---------------------------------------------------

    def move_stop(
        self,
        *,
        symbol: str,
        exit_side: str,
        qty: float,
        new_stop: float,
        old_stop_link_id: str,
        filters: Optional[InstrumentFilters] = None,
    ) -> Optional[str]:
        """Replace a stop: **place the new one first, then cancel the old.**

        Order matters. Cancel-then-place leaves a window in which the position
        has no protection at all, and if the placement then fails the position
        stays naked. Place-then-cancel can briefly double the protective size,
        which is the strictly safer failure.
        """
        filters = filters or self.client.get_instrument_filters(symbol)
        placed = self.client.place_stop_order(
            symbol=symbol, side=exit_side, qty=qty,
            trigger_price=new_stop, filters=filters,
        )
        if not placed.ok:
            logger.warning("stop move failed for %s (%s); keeping the existing stop",
                           symbol, placed.reason)
            return None

        if old_stop_link_id:
            if not self.client.cancel_order(
                symbol=symbol, order_link_id=old_stop_link_id
            ):
                # Two live stops is survivable; the first to trigger exits the
                # position and the other is cancelled at reconciliation.
                logger.error("could not cancel the superseded stop %s on %s — two "
                             "protective stops are now live", old_stop_link_id, symbol)
        self.store.set_position_stop(symbol, new_stop)
        return placed.order_link_id

    def move_stop_to_breakeven(
        self, *, symbol: str, exit_side: str, qty: float,
        entry_price: float, old_stop_link_id: str,
    ) -> Optional[str]:
        """Ratchet the stop to entry after a partial take-profit fills."""
        if not self.breakeven_after_first_tp:
            return None
        return self.move_stop(
            symbol=symbol, exit_side=exit_side, qty=qty,
            new_stop=entry_price, old_stop_link_id=old_stop_link_id,
        )

    # -- exits -------------------------------------------------------------

    @staticmethod
    def _row_meta(row: Mapping[str, Any]) -> Dict[str, Any]:
        try:
            meta = json.loads(str(row.get("meta") or "{}"))
        except Exception:  # noqa: BLE001
            return {}
        return meta if isinstance(meta, dict) else {}

    def close_position(
        self,
        *,
        symbol: str,
        reason: str,
        exit_price: Optional[float] = None,
        entry_fee: float = 0.0,
        exit_fee: float = 0.0,
    ) -> ExecutionReport:
        """Close a position at market and record it fee-inclusive.

        Cancels the resting protective orders **after** the close is confirmed,
        so a failed close never leaves the position unprotected.
        """
        positions = {p["symbol"]: p for p in self.store.open_positions()}
        row = positions.get(symbol)
        if row is None:
            return self._report(stage="close", reason="NO_SUCH_POSITION",
                                symbol=symbol)

        side = str(row["side"])
        exit_side = "Sell" if normalize_side(side) == "Buy" else "Buy"
        # Float subtraction leaves noise (0.9470719999999999 for 0.947072), and
        # snapping that DOWN to the lot step sells one step too little: a
        # permanent dust remainder on every close. Twelve places is far below any
        # lot step and removes the noise.
        qty = round(float(row["qty"]), 12)
        entry_price = float(row["entry_price"])
        row_meta = self._row_meta(row)

        pending = self._settle_pending_closes(symbol, row)
        if pending is not None:
            return pending
        row = {p["symbol"]: p for p in self.store.open_positions()}.get(symbol)
        if row is None:
            return self._report(stage="close", reason="CLOSED_BY_EARLIER_ORDER",
                                symbol=symbol)
        qty = round(float(row["qty"]), 12)

        if not getattr(self.client, "is_linear", False):
            # A resting spot limit sell reserves the coin it offers, so the legs
            # of a take-profit ladder that covers the position leave nothing free
            # for the close. They are cancelled FIRST (the stop stays: a missing
            # take-profit costs upside, never capital), and a close that then
            # fails still has its stop.
            self._cancel_take_profit_legs(symbol)

        result = self.client.place_order(
            symbol=symbol, side=exit_side, qty=qty, order_type="Market",
            purpose="close", reduce_only=True,
        )
        if not result.ok:
            logger.error("close failed for %s: %s", symbol, result.reason)
            return self._report(
                stage="close", reason=result.reason, symbol=symbol,
                detail={
                    "exit_order_link_id": result.order_link_id,
                    "exit_order_id": result.order_id,
                    "exit_side": exit_side,
                    "requested_exit_qty": qty,
                    "exit_status": "REJECTED",
                })

        fill = self._await_fill(symbol, result.order_link_id)
        # The ACTUAL venue-confirmed executed quantity, not the requested
        # one — POST /v5/order/create's acknowledgement is not execution
        # (Bybit's own docs: order creation is async; confirm status via
        # WS/order query). 0.0 (never the requested qty) if the fill
        # never resolved — an unconfirmed exit is not evidence of one.
        executed_exit_qty = float((fill or {}).get("cumExecQty") or 0.0)
        exit_status = str((fill or {}).get("orderStatus", "") or "")

        # On linear the venue's position list is the truth, and observe_exits
        # books the real exit from it. A close that did not execute in full
        # (no resolved fill, or a partial one) must therefore leave the row and
        # the stops in place: booking it as closed would hide a position that
        # is still open, and a new entry could stack on it.
        tol = max(1e-9, qty * 1e-9)
        if ((getattr(self.client, "is_linear", False)
                and executed_exit_qty < qty - tol)
                or (not getattr(self.client, "is_linear", False)
                    and executed_exit_qty <= tol)):
            # (spot: a close that sold NOTHING -- rejected, cancelled, or whose
            # fill never resolved -- is not a close: the coin is still held, so
            # the row and its stop stay and `_settle_pending_closes` books the
            # order later if it did execute.)
            logger.critical(
                "close of %s did not execute in full (%.12g of %.12g, %s): the "
                "position row and protective orders are kept", symbol,
                executed_exit_qty, qty, exit_status or "unresolved")
            self.store.journal(symbol, "CLOSE_UNCONFIRMED_STOPS_KEPT", reason, {
                "exit_order_link_id": result.order_link_id,
                "exit_order_id": result.order_id,
                "executed_exit_qty": executed_exit_qty})
            return self._report(
                stage="close", reason="CLOSE_UNCONFIRMED", symbol=symbol,
                qty=qty, detail={
                    "exit_order_link_id": result.order_link_id,
                    "exit_order_id": result.order_id,
                    "exit_side": exit_side,
                    "requested_exit_qty": qty,
                    "executed_exit_qty": executed_exit_qty,
                    "exit_status": exit_status or "UNCONFIRMED"})

        if (not getattr(self.client, "is_linear", False)
                and 0.0 < executed_exit_qty < qty - max(1e-9, qty * 1e-9)):
            # Spot: only part of the position sold and the rest was cancelled.
            # Booking the whole row as closed (and cancelling the stop) would
            # leave the unsold remainder held with no stop and no row. Book what
            # sold; record_exit_fill reduces the row and resizes the stop.
            avg = self._fill_price(fill)
            self.record_exit_fill(
                symbol=symbol, qty=executed_exit_qty,
                price=avg or entry_price, fee=0.0, price_provisional=avg <= 0.0,
                reason=f"{reason}:PARTIAL_CLOSE", order_link_id=result.order_link_id,
                dedupe_key=f"exit:{result.order_link_id}",
                fees=venue_fees.extract_order_fees(fill or {}),
                fee_ref={"order_link_id": result.order_link_id,
                         "order_id": str((fill or {}).get("orderId") or "")})
            return self._report(
                stage="close", reason="CLOSE_PARTIAL", symbol=symbol, qty=qty,
                detail={"exit_order_link_id": result.order_link_id,
                        "requested_exit_qty": qty,
                        "executed_exit_qty": executed_exit_qty,
                        "exit_status": exit_status or "PARTIAL"})

        # The protective orders come off only once the close is CONFIRMED. A
        # close that was accepted but whose fill never resolved (a poll that
        # timed out, or an order that ended Rejected / Cancelled) may not have
        # reduced the position at all; cancelling its stops then would book the
        # position as closed while it is still open and unprotected. Left in
        # place, a resting stop on a position that really did close is the lesser
        # evil: the journal names it so it is cleaned up, never silently lost.
        if fill is not None:
            self._cancel_protective_orders(symbol)
        else:
            logger.critical(
                "close of %s is UNCONFIRMED (no resolved fill for %s): the "
                "protective orders are left in place", symbol,
                result.order_link_id)
            self.store.journal(symbol, "CLOSE_UNCONFIRMED_STOPS_KEPT", reason, {
                "exit_order_link_id": result.order_link_id,
                "exit_order_id": result.order_id})

        booked = self._book_full_exit(
            symbol=symbol, row=row, exit_order=result, fill=fill, reason=reason,
            exit_price=exit_price, caller_entry_fee=entry_fee,
            caller_exit_fee=exit_fee)
        entry_leg, exit_leg = booked["entry_leg"], booked["exit_leg"]
        # Exit identity/quantity evidence (mission "REAL FINDING #3"):
        # extends the existing ExecutionReport.detail path -- never a
        # parallel evidence model -- so a caller (e.g.
        # tools/testnet_conformance_run.py) can require real exit WS
        # order/execution evidence keyed on exit_order_link_id/
        # exit_order_id, exactly like it already does for entry.
        return self._report(
            ok=True, stage="close", reason="CLOSED", symbol=symbol,
            qty=qty, detail={
                "gross_pnl": booked["gross"],
                "exit_order_link_id": result.order_link_id,
                "exit_order_id": result.order_id,
                "exit_side": exit_side,
                "requested_exit_qty": qty,
                "executed_exit_qty": executed_exit_qty,
                "exit_avg_price": booked["exit_price"],
                "exit_status": exit_status,
                "entry_fee": entry_leg["amount"],
                "entry_fee_realized": entry_leg["realized"],
                "exit_fee": exit_leg["amount"],
                "exit_fee_realized": exit_leg["realized"],
            })

    def _book_full_exit(
        self, *, symbol: str, row: Mapping[str, Any], exit_order: Any,
        fill: Optional[Mapping[str, Any]], reason: str,
        exit_price: Optional[float] = None, caller_entry_fee: float = 0.0,
        caller_exit_fee: float = 0.0,
    ) -> Dict[str, Any]:
        """Book the whole of an open position as closed by one market order.

        The ONE place a normal close and an emergency close reach the trades
        table, so neither can drop the economics the other keeps. Both fee legs
        come from the venue's own rows where it stated them: the entry fee was
        captured at fill time (position meta), the exit fee is read from the
        exit order's fill. Anything the venue did not state is a labelled
        non-realised figure -- never a silent 0.0 and never flagged realised.
        An exit whose fill never resolved is booked too (the position is gone
        from the book), but marked ``exit_fill_confirmed=False`` so its price is
        not mistaken for a venue fact.
        """
        side = str(row["side"])
        qty = float(row["qty"])
        entry_price = float(row["entry_price"])
        meta = self._row_meta(row)
        # No resolved fill, or a fill that states no price: the exit price is
        # UNKNOWN. The row is booked PROVISIONAL (quantity and fees only,
        # exit_price 0 / gross 0, kept out of every confirmed-economics reader)
        # and priced later from the venue's executions by
        # reconcile_provisional_exits. Neither the caller's figure, the entry
        # price nor the last price stands in.
        realised_exit = self._fill_price(fill)
        confirmed = fill is not None and realised_exit > 0.0
        provisional = not confirmed
        direction = 1.0 if normalize_side(side) == "Buy" else -1.0
        gross = (0.0 if provisional
                 else (realised_exit - entry_price) * qty * direction)
        entry_leg = self._fee_leg(
            symbol=symbol, fees=FeeSet.from_dict(meta.get("entry_fees")),
            price=entry_price, qty=qty, caller_amount=caller_entry_fee,
            ref=meta.get("entry_fee_ref"))
        exit_leg = self._fee_leg(
            symbol=symbol, fees=venue_fees.extract_order_fees(fill),
            price=realised_exit if confirmed else entry_price,   # unit only
            qty=qty, caller_amount=caller_exit_fee,
            ref={"order_link_id": exit_order.order_link_id,
                 "order_id": str((fill or {}).get("orderId")
                                 or getattr(exit_order, "order_id", "") or "")})
        if provisional:
            exit_leg = dict(exit_leg, realized=False, currency=None)
        link = str(exit_order.order_link_id or "")
        # dedupe_key ties this trade to the exit order's own durable id, the
        # same identity `find_live_spot_stop`/`orders(order_link_id)` already
        # rely on -- a crash between this `record_trade` and the
        # `remove_position` below leaves the position row in place, so a
        # retried/replayed close must not book it twice.
        # See `StateStore.record_trade`'s docstring for the mechanism.
        # One transaction for the trade and the removal of the position it
        # closes: a kill between them left the position beside a booked exit.
        with self.store.transaction():
            self.store.record_trade(TradeRecord(
                symbol=symbol, side=side, qty=qty, entry_price=entry_price,
                exit_price=0.0 if provisional else realised_exit,
                price_provisional=provisional, gross_pnl=gross,
                entry_fee=entry_leg["amount"], exit_fee=exit_leg["amount"],
                entry_fee_realized=entry_leg["realized"],
                exit_fee_realized=exit_leg["realized"],
                entry_fee_currency=entry_leg["currency"],
                exit_fee_currency=exit_leg["currency"],
                opened_epoch=float(row.get("opened_epoch") or utc_now_epoch()),
                closed_epoch=utc_now_epoch(),
                order_link_id=link,
                meta={"reason": reason, "exit_fill_confirmed": confirmed,
                      "funding_booked": not getattr(self.client, "is_linear", False),
                      "fees": {"entry": entry_leg["raw"], "exit": exit_leg["raw"]}},
                dedupe_key=f"exit:{link}" if link else "",
            ))
            self.store.remove_position(symbol)
        self._cooldown_after_loss(symbol)
        self.store.journal(symbol, "CLOSED", reason, {
            "gross_pnl": gross,
            "fees": entry_leg["amount"] + exit_leg["amount"],
            "net_pnl": gross - entry_leg["amount"] - exit_leg["amount"],
            "entry_fee_realized": entry_leg["realized"],
            "exit_fee_realized": exit_leg["realized"],
            "exit_fill_confirmed": confirmed,
        })
        return {"gross": gross, "exit_price": realised_exit,
                "entry_leg": entry_leg, "exit_leg": exit_leg}

    #: ``get_closed_pnl`` is one page of this many rows with no cursor. A full
    #: page may be truncated, so its fee association is not trusted.
    _CLOSED_PNL_PAGE = 50

    def _observed_exit_fees(
        self, symbol: str, pnl_rows: Sequence[Mapping[str, Any]], exited: float,
    ) -> Tuple[FeeSet, Dict[str, Any], Dict[str, Any]]:
        """The venue fee for an exchange-side exit, or an UNKNOWN set and why.

        Closed-PnL rows name the closing order(s); each order's own
        ``cumFeeDetail`` is the documented fee source (``openFee``/``closeFee``
        have no documented currency, so they are kept as evidence only). A fee
        is attributed to this exit only if the evidence closes exactly: the
        closed-PnL window, the named orders' executed quantity and the quantity
        that left the position must agree, otherwise an earlier partial exit's
        order (still inside the window) or a later fill on the same order would
        be counted twice.
        """
        evidence: Dict[str, Any] = {"closed_pnl": [
            {"orderId": r.get("orderId"), "qty": r.get("qty"),
             "openFee": r.get("openFee"), "closeFee": r.get("closeFee")}
            for r in pnl_rows]}

        def unknown(note: str):
            return (venue_fees.UNKNOWN_FEES, {},
                    {**evidence, "fee_unattributed": note})

        if not pnl_rows:
            return unknown("no closed-pnl rows")
        if len(pnl_rows) >= self._CLOSED_PNL_PAGE:
            return unknown("closed-pnl page full; possibly truncated")
        tol = max(1e-9, abs(exited) * 1e-6)
        if abs(sum(float(r.get("qty", 0) or 0) for r in pnl_rows) - exited) > tol:
            return unknown("closed-pnl quantity != quantity that left the position")
        order_ids = sorted({str(r.get("orderId") or "") for r in pnl_rows
                            if r.get("orderId")})
        if not order_ids or any(not str(r.get("orderId") or "") for r in pnl_rows):
            return unknown("a closed-pnl row carries no orderId")
        total, executed = venue_fees.FeeSet((), True, "cumFeeDetail"), 0.0
        for oid in order_ids:
            try:
                order = self.client.get_order(symbol=symbol, order_id=oid)
            except BybitAPIError as exc:
                return unknown(f"order {oid} unreadable: {exc}")
            if not order:
                return unknown(f"order {oid} not found")
            fees = venue_fees.extract_order_fees(order)
            if not fees.known:
                return unknown(f"order {oid}: {fees.note}")
            total = total.plus(fees)
            executed += float(order.get("cumExecQty") or 0.0)
        if abs(executed - exited) > tol:
            return unknown("named orders executed a different quantity")
        return total, {"order_ids": order_ids}, evidence

    #: An order the venue cannot find is only called never-sent once it is this
    #: old: a create whose reply was lost can take a moment to become visible.
    UNRESOLVED_ENTRY_NEVER_SENT_AFTER_S = 120.0

    def resolve_unresolved_entries(self) -> Dict[str, Any]:
        """Settle entries whose outcome the engine never saw, while running.

        An entry left ``submitted``/``unknown`` (a fill poll that timed out, a
        reply that never came) blocks its symbol and takes a slot against the
        position and exposure caps. Only a restart used to clear it, so a venue
        blip parked a symbol until then. Each is looked up at the venue: a
        terminal order is recorded; a spot buy that executed is adopted into the
        book and protected (or flattened) like any naked position, and is only
        marked filled once that has actually happened. A linear fill, or an
        adoption that could not be completed, is left unresolved -- loudly,
        once, and not re-queried every tick -- so the gate keeps holding the
        symbol for a human. Never raises into the loop.
        """
        summary: Dict[str, Any] = {"checked": 0, "resolved": 0, "adopted": [],
                                   "unread": 0}
        try:
            rows = [r for r in self.store.unresolved_orders()
                    if str(r.get("purpose")) == "entry"]
        except Exception:  # noqa: BLE001
            logger.exception("could not list unresolved entries")
            return summary
        now = utc_now_epoch()
        stuck = self.__dict__.setdefault("_stuck_entries", {})
        live_ids = {str(r["order_link_id"]) for r in rows}
        for gone in [k for k in stuck if k not in live_ids]:
            del stuck[gone]
        for row in rows:
            oid, symbol = str(row["order_link_id"]), str(row["symbol"])
            if stuck.get(oid, 0.0) > now:
                continue                      # flagged for a human; not re-queried
            summary["checked"] += 1
            try:
                self._resolve_one_entry(row, oid, symbol, now, summary, stuck)
            except Exception:  # noqa: BLE001
                logger.exception("could not settle unresolved entry %s", oid)
                stuck[oid] = now + self.STUCK_ENTRY_RECHECK_S   # logged once, not per tick
                summary["unread"] += 1
        return summary

    #: How long an entry that cannot be settled automatically is left alone
    #: (and not re-queried) before it is looked at again.
    STUCK_ENTRY_RECHECK_S = 600.0

    def _resolve_one_entry(self, row: Mapping[str, Any], oid: str, symbol: str,
                           now: float, summary: Dict[str, Any],
                           stuck: Dict[str, float]) -> None:
        try:
            remote = self.client.get_order(symbol=symbol, order_link_id=oid,
                                           strict=True)
        except BybitAPIError as exc:
            logger.warning("unresolved entry %s unreadable: %s", oid, exc)
            summary["unread"] += 1
            return
        if remote is None:
            age = now - float(row.get("created_epoch") or now)
            if age >= self.UNRESOLVED_ENTRY_NEVER_SENT_AFTER_S:
                self.store.update_order_status(oid, "never_sent")
                summary["resolved"] += 1
            return
        status = str(remote.get("orderStatus", ""))
        if status not in ("Filled", "Cancelled", "Rejected", "Deactivated",
                          "PartiallyFilledCanceled"):
            return                            # still live: nothing to settle yet
        executed = float(remote.get("cumExecQty") or 0.0) > 0.0
        if status == "Filled" and not executed:
            # `execute` treats a Filled with no executed quantity as an anomaly
            # (ZERO_FILL), not as proof that nothing was bought.
            logger.critical("unresolved entry %s is Filled with cumExecQty=%r; "
                            "left for a human", oid, remote.get("cumExecQty"))
            stuck[oid] = now + self.STUCK_ENTRY_RECHECK_S
            summary["unread"] += 1
            return
        if not executed:
            self.store.update_order_status(
                oid, status.lower(), exchange_id=str(remote.get("orderId", "")))
            summary["resolved"] += 1
            return
        if getattr(self.client, "is_linear", False):
            logger.critical(
                "unresolved entry %s on %s EXECUTED at the venue (linear): left "
                "unresolved so the symbol stays blocked until a human reconciles "
                "it", oid, symbol)
            stuck[oid] = now + self.STUCK_ENTRY_RECHECK_S
            summary["unread"] += 1
            return
        adopt = getattr(self.client, "_adopt_filled_spot_entry", None)
        adopted = adopt(row, remote, {}) if callable(adopt) else False
        if adopted is not True:
            logger.critical(
                "unresolved spot entry %s on %s executed but could not be "
                "recorded; left unresolved for a human", oid, symbol)
            stuck[oid] = now + self.STUCK_ENTRY_RECHECK_S
            summary["unread"] += 1
            return
        self.store.update_order_status(
            oid, "filled", exchange_id=str(remote.get("orderId", "")))
        summary["resolved"] += 1
        held = {p["symbol"]: p for p in self.store.open_positions()}.get(symbol)
        if (held is not None and str(held.get("order_link_id")) == oid
                and float(held.get("stop_price") or 0.0) <= 0.0):
            self._protect_or_close_naked(symbol, held, reason="ENTRY_RESOLVED_LATE")
            summary["adopted"].append(symbol)

    def observe_exits(self) -> Dict[str, Any]:
        """Notice exchange-side exits (stop / take-profit fills) and book them.

        Before this method, nothing in the runtime loop looked at a position
        after entry: exits were delegated to venue-side orders and never
        observed, so ``positions`` rows lived forever, ``open_position_count``
        never decremented, ``trades`` was never written by a live run and the
        loss-streak brake was inert. After MAX_OPEN_POSITIONS such exits the
        shell locked itself out permanently.

        Linear only (spot holdings are balances, not positions). For each
        position the ledger holds: read the venue position; if the venue holds
        less than the ledger, the difference has exited. The exit price comes
        from /v5/position/closed-pnl records newer than the position's open
        time. If those cannot be read the exit is STILL booked - at the last
        traded price, with reason ``VENUE_EXIT_PRICE_UNREAD`` - because a
        ledger that disagrees with the venue about whether a position exists is
        worse than a ledger with an approximate exit price. Never places an
        order. Never raises into the loop.
        """
        summary: Dict[str, Any] = {"checked": 0, "closed": 0, "reduced": 0,
                                   "unread": 0, "price_approximated": 0}
        if not getattr(self.client, "is_linear", False):
            return self._observe_spot_exits(summary)
        for row in list(self.store.open_positions()):
            symbol = str(row["symbol"])
            summary["checked"] += 1
            try:
                remote = self.client.get_position(symbol)
            except BybitAPIError as exc:
                logger.error("observe_exits: cannot read %s at the venue: %s",
                             symbol, exc)
                summary["unread"] += 1
                continue
            held = float(row["qty"])
            remote_qty = float((remote or {}).get("size", 0) or 0)
            exited = held - remote_qty
            if exited <= 0:
                continue
            since_ms = int(float(row.get("opened_epoch", 0) or 0) * 1000)
            price: Optional[float] = None
            reason = "VENUE_EXIT"
            order_ids: List[str] = []
            pnl_rows: List[Dict[str, Any]] = []
            try:
                all_rows = self.client.get_closed_pnl(symbol, since_ms=since_ms)
                # A price is accepted ONLY from rows PROVEN to be this exit:
                # exactly the quantity that left, not a full (possibly
                # truncated) page, none already named by an exit booked
                # before. Anything else -- a lagging read that still shows
                # only an earlier take-profit, a sum that overshoots -- would
                # price this exit at another fill's price, with full
                # confidence. That is a provisional exit, not a guess.
                pnl_rows = self._proven_exit_rows(
                    symbol, all_rows, exited,
                    float(row.get("opened_epoch", 0) or 0.0))
                qty_sum = sum(self._row_qty(r) for r in pnl_rows)
                if qty_sum > 0:
                    price = sum(float(r.get("avgExitPrice", 0) or 0)
                                * self._row_qty(r) for r in pnl_rows) / qty_sum
                order_ids = sorted({
                    str(r.get("orderId") or "") for r in pnl_rows
                    if r.get("orderId")
                })
            except BybitAPIError as exc:
                logger.error("observe_exits: closed-pnl unreadable for %s: %s",
                             symbol, exc)
            provisional = False
            if price is None or price <= 0:
                # The venue says the quantity LEFT; it has not said at what
                # price. The last traded price is not that price. The exit is
                # booked PROVISIONAL (quantity and what fees are known, no
                # price, no gross), the position and its stop are resized once,
                # and the price is reconciled later from the venue's own
                # executions by order id. The row never enters confirmed P&L.
                provisional = True
                price = float(row["entry_price"])        # unit placeholder only
                reason = "VENUE_EXIT_PRICE_UNREAD"
                summary["exits_provisional"] = summary.get(
                    "exits_provisional", 0) + 1
            # Crash-boundary invariant: a process kill between
            # `record_exit_fill`'s `record_trade` and its `remove_position`
            # (or `upsert_position` for a partial) leaves this exact position
            # row in place, so the next cycle recomputes the SAME exit and
            # would otherwise book it twice. The key is the POSITION's own
            # state: which position (symbol + open time) and the quantity it
            # held before and lost now. An identical replay reproduces it; the
            # next partial exit of the same position cannot, because the
            # quantity held only ever falls. The venue's order id is NOT part
            # of it: one order can fill in pieces across cycles, and a key
            # built from it silently dropped every piece after the first.
            dedupe_key = (f"exit_pos:{symbol}:"
                          f"{float(row.get('opened_epoch', 0) or 0):.6f}:"
                          f"{held:.10f}:{exited:.10f}")
            exit_fees, fee_ref, evidence = self._observed_exit_fees(
                symbol, pnl_rows, exited)
            fully = self.record_exit_fill(symbol=symbol, qty=exited, price=price,
                                          fee=0.0, reason=reason,
                                          dedupe_key=dedupe_key, fees=exit_fees,
                                          fee_ref=fee_ref, fee_evidence=evidence,
                                          price_provisional=provisional,
                                          exit_order_ids=order_ids,
                                          exit_row_keys=[self._row_key(r)
                                                         for r in pnl_rows])
            if provisional:
                self.store.journal(symbol, "EXIT_UNPRICED_PROVISIONAL",
                                   reason, {"qty": exited,
                                            "exit_order_ids": order_ids})
            summary["closed" if fully else "reduced"] += 1
            logger.warning("observe_exits: %s exited %.8g (%s) fully=%s%s",
                           symbol, exited, reason, fully,
                           " PROVISIONAL" if provisional else
                           f" @ {price:.8g}")
        return summary

    def _observe_spot_exits(self, summary: Dict[str, Any]) -> Dict[str, Any]:
        """Book spot stop / take-profit fills from the orders themselves.

        Spot has no venue position to compare with, so the protective orders
        the row names (its stop and its take-profit legs) are read. One that
        has reached a terminal state with executed quantity is a real exit:
        it is booked through ``record_exit_fill`` (which also resizes the
        stop and cancels the siblings), once -- an order already booked on
        the row is skipped. Without this a spot stop-out was invisible: the
        row stayed open, the loss never reached the streak brake, and the
        position count never came back down.
        """
        open_rows = list(self.store.open_positions())
        live_stops = {(str(r["symbol"]),
                       str(self._row_meta(r).get("stop_order_link_id") or ""))
                      for r in open_rows}
        seen_phantoms = self.__dict__.setdefault("_phantom_journaled", set())
        seen_phantoms.intersection_update(live_stops)      # healed/closed ones
        deferrals = self.__dict__.setdefault("_dead_stop_deferrals", {})
        for sym in [s for s in deferrals if s not in {k for k, _ in live_stops}]:
            deferrals.pop(sym, None)
        # Take-profit legs seen terminal with NOTHING filled (cancelled by us or
        # the venue) are not read again; in memory only, so a restart re-reads
        # each once. Dropped with the row.
        dead_tps = self.__dict__.setdefault("_dead_tp_legs", set())
        dead_reads = self.__dict__.setdefault("_dead_tp_reads", {})
        open_symbols = {str(r["symbol"]) for r in open_rows}
        dead_tps.intersection_update({k for k in dead_tps if k[0] in open_symbols})
        for k in [k for k in dead_reads if k[0] not in open_symbols]:
            dead_reads.pop(k, None)
        for row in open_rows:
            symbol = str(row["symbol"])
            try:
                has_close = any(
                    str(o.get("symbol")) == symbol
                    and str(o.get("purpose")) in ("close", "emergency_close")
                    for o in self.store.unresolved_orders())
            except Exception:  # noqa: BLE001
                has_close = False
            if has_close:
                # a close that executed while the process was down (or whose
                # reply was lost) is booked from its own fill, once
                self._settle_pending_closes(symbol, row)
                if not any(str(p["symbol"]) == symbol
                           for p in self.store.open_positions()):
                    summary["closed"] += 1
                    continue
                row = next(p for p in self.store.open_positions()
                           if str(p["symbol"]) == symbol)
            meta = self._row_meta(row)
            booked = set(meta.get("booked_exit_orders") or [])
            # Take-profit legs first: a leg that sold part of the position is
            # what makes the original-size stop fail, and it must be booked
            # (resizing the stop) before the stop is judged dead.
            ids = [str(i) for i in (meta.get("tp_order_ids") or [])] + [
                str(meta.get("stop_order_link_id") or "")]
            summary["checked"] += 1
            dead_stop = ""
            tp_in_flight = False
            for link in ids:
                if not link or link in booked or (symbol, link) in dead_tps:
                    continue
                try:
                    order = self.client.get_order(symbol=symbol, order_link_id=link)
                except BybitAPIError as exc:
                    logger.error("observe_exits: cannot read %s order %s: %s",
                                 symbol, link, exc)
                    summary["unread"] += 1
                    continue
                if not order:
                    continue
                status = str(order.get("orderStatus", ""))
                if (link != str(meta.get("stop_order_link_id") or "")
                        and status in ("Cancelled", "Rejected", "Deactivated")
                        and not float(order.get("cumExecQty") or 0.0) > 0.0):
                    # Two consecutive dead reads before it is skipped for good:
                    # one stale snapshot must not hide a fill still settling.
                    seen_dead = dead_reads[(symbol, link)] = dead_reads.get(
                        (symbol, link), 0) + 1
                    if seen_dead >= 2:
                        dead_tps.add((symbol, link))
                    continue
                dead_reads.pop((symbol, link), None)   # not dead on this read
                if (link != str(meta.get("stop_order_link_id") or "")
                        and status == "PartiallyFilled"):
                    tp_in_flight = True     # coin is leaving; book it first
                if (link == str(meta.get("stop_order_link_id") or "")
                        and status in ("Rejected", "Cancelled", "Deactivated")
                        and not float(order.get("cumExecQty") or 0.0) > 0.0):
                    # The protective stop is DEAD with nothing sold: a
                    # triggered stop the venue refused, or one cancelled behind
                    # our back. The ledger still calls this position
                    # protected; spot has no mid-cycle naked check, so this is
                    # where it is seen. Re-protect, or flatten.
                    dead_stop = link
                    continue
                cancelled_with_fill = (
                    status in ("Cancelled", "Deactivated")
                    and link != str(meta.get("stop_order_link_id") or "")
                    and float(order.get("cumExecQty") or 0.0) > 0.0)
                if status not in ("Filled", "PartiallyFilledCanceled") \
                        and not cancelled_with_fill:
                    continue
                try:
                    qty = float(order.get("cumExecQty") or 0.0)
                    price = float(order.get("avgPrice") or 0.0)
                    if price <= 0 and qty > 0:
                        # a cancelled order may omit avgPrice: derive it, else
                        # fall back to the leg's own limit price
                        value = float(order.get("cumExecValue") or 0.0)
                        price = (value / qty if value > 0
                                 else float(order.get("price") or 0.0))
                except (TypeError, ValueError):
                    continue
                current = {p["symbol"]: p for p in self.store.open_positions()}.get(symbol)
                if current is None:
                    break
                qty = min(qty, float(current["qty"]))
                provisional = False
                if qty > 0 and price <= 0:
                    unpriced = self.__dict__.setdefault("_unpriced_cycles", {})
                    n_unpriced = unpriced[(symbol, link)] = unpriced.get(
                        (symbol, link), 0) + 1
                    if n_unpriced > self.DEAD_STOP_MAX_DEFERRALS:
                        # Waiting for a price the venue will not give is how a
                        # position stays naked for ever. Book the QUANTITY (it
                        # resizes the position and the stop) as a PROVISIONAL
                        # exit: no exit price is invented. The row carries
                        # exit_price 0 / gross 0, is flagged price_provisional,
                        # and is excluded from confirmed realized P&L,
                        # expectancy, sizing inputs and promotion evidence until
                        # reconciled. The entry price below is only a
                        # placeholder for unit conversions, never stored as a
                        # fill price. (It still counts in the loss-streak and
                        # daily-loss brakes: fees are real and the brake errs
                        # conservative.)
                        provisional = True
                        price = float(current["entry_price"] or 0.0)
                        logger.critical(
                            "observe_exits: %s order %s filled %.8g with no "
                            "price from the venue after %d cycles; booking a "
                            "PROVISIONAL, UNPRICED exit (excluded from "
                            "confirmed P&L until reconciled)", symbol, link,
                            qty, n_unpriced)
                        self.store.journal(symbol, "EXIT_UNPRICED_PROVISIONAL",
                                           link, {"qty": qty,
                                                  "status": status})
                        summary["exits_provisional"] = summary.get(
                            "exits_provisional", 0) + 1
                if qty <= 0 or price <= 0:
                    logger.error("observe_exits: %s order %s is %s but reports "
                                 "qty=%s price=%s; not booked", symbol, link,
                                 status, order.get("cumExecQty"), price)
                    summary["unread"] += 1
                    # A fill we cannot price is coin that left: the recorded
                    # size is stale, so do not judge the stop against it.
                    tp_in_flight = True
                    continue
                fees = venue_fees.extract_order_fees(order)
                fully = self.record_exit_fill(
                    symbol=symbol, qty=qty, price=price, fee=0.0,
                    reason="VENUE_EXIT_SPOT", order_link_id=link,
                    dedupe_key=f"exit_spot:{link}", fees=fees,
                    fee_ref={"order_link_id": link,
                             "order_id": str(order.get("orderId") or "")},
                    booked_order_id=link, price_provisional=provisional)
                summary["closed" if fully else "reduced"] += 1
                logger.warning("observe_exits: spot %s exited %.8g @ %.8g via %s "
                               "fully=%s", symbol, qty, price, link, fully)
                # One exit per symbol per cycle: a partial exit replaces the
                # stop, so the row is re-read next cycle.
                break
            else:
                if dead_stop and tp_in_flight:
                    # Let the part-filled leg be booked first - but not for
                    # ever: a thin book can leave it part-filled for hours.
                    n = deferrals[symbol] = deferrals.get(symbol, 0) + 1
                    if n > 2 * self.DEAD_STOP_MAX_DEFERRALS:
                        # The leg will not cancel or book. Do NOT fall through
                        # to the emergency path: the leg still reserves coin and
                        # the recorded size is stale, so a stop (or an emergency
                        # sale) would be refused and only trip the kill switch.
                        # Say so once, loudly, and keep retrying the cancel.
                        seen = self.__dict__.setdefault("_leg_stuck_journaled", set())
                        seen.intersection_update(live_stops)
                        key = (symbol, dead_stop)
                        if key not in seen:
                            seen.add(key)
                            logger.critical(
                                "observe_exits: %s stop %s has stayed dead for "
                                "%d cycles and a take-profit leg will not "
                                "cancel or book; a human must look", symbol,
                                dead_stop, n)
                            self.store.journal(symbol, "STOP_DEAD_LEG_STUCK",
                                               dead_stop, {"cycles": n})
                        self._handle_dead_spot_stop(symbol, row, dead_stop,
                                                    summary, relaxed=True)
                    elif n > self.DEAD_STOP_MAX_DEFERRALS:
                        logger.critical(
                            "observe_exits: %s stop %s is dead and a "
                            "take-profit leg has stayed part-filled for %d "
                            "cycles; re-protecting what is held", symbol,
                            dead_stop, n)
                        self._handle_dead_spot_stop(
                            symbol, row, dead_stop, summary, relaxed=True)
                elif dead_stop:
                    deferrals.pop(symbol, None)
                    self._handle_dead_spot_stop(symbol, row, dead_stop, summary)
                if not (dead_stop and tp_in_flight):
                    deferrals.pop(symbol, None)     # the grace starts afresh
        return summary

    DEAD_STOP_MAX_DEFERRALS = 3

    def _handle_dead_spot_stop(self, symbol: str, row: Mapping[str, Any],
                               link: str, summary: Dict[str, Any],
                               relaxed: bool = False) -> None:
        """A spot stop is Rejected/Cancelled/Deactivated with nothing sold."""
        if relaxed:
            # A take-profit leg has stayed part-filled while the stop is dead.
            # Free the coin it reserves and let the NEXT cycle do the rest: the
            # cancelled leg reports PartiallyFilledCanceled, is booked through
            # record_exit_fill (resizing the position), and a stop that is
            # still dead is then judged against the true size by the normal
            # path. Re-protecting here, in the same breath, would size the new
            # stop before that booking and could end in an emergency sale that
            # loses the leg's P&L. A missing take-profit costs upside, never
            # capital.
            ids = [str(t) for t in
                   (self._row_meta(row).get("tp_order_ids") or [])]
            direct_ok = True
            booked_ids = set(self._row_meta(row).get("booked_exit_orders") or [])
            for tp in ids:          # the engine's own list, not only the table
                if tp in booked_ids:
                    continue        # already filled and booked: nothing to cancel
                try:
                    if self.client.cancel_order(symbol=symbol,
                                                order_link_id=tp) is False:
                        direct_ok = False
                except Exception:  # noqa: BLE001
                    direct_ok = False
            if self._cancel_take_profit_legs(symbol) and direct_ok:
                logger.critical(
                    "observe_exits: %s stop %s is dead and a take-profit leg "
                    "stayed part-filled; cancelled the leg(s), the partial "
                    "fill is booked and the stop re-judged next cycle",
                    symbol, link)
            else:
                logger.critical(
                    "observe_exits: %s stop %s is dead and the part-filled "
                    "take-profit leg could not be cancelled; retrying next "
                    "cycle", symbol, link)
            summary.setdefault("stop_dead", []).append(symbol)
            return
        try:
            held = float(self.client.get_coin_balance(
                self.client._base_asset(symbol), total=True))
        except Exception as exc:  # noqa: BLE001
            logger.error("observe_exits: cannot read the %s balance to judge "
                         "the dead stop %s: %s", symbol, link, exc)
            summary["unread"] += 1
            return
        if held < float(row["qty"]) * 0.98:
            # The coin is not (all) there: the position was sold or moved
            # behind the row's back. Re-protecting or "emergency selling" coin
            # that is not held would only trip the kill switch; leave it for
            # reconcile and say so.
            seen = self.__dict__.setdefault("_phantom_journaled", set())
            first = (symbol, link) not in seen
            (logger.critical if first else logger.debug)(
                "observe_exits: the %s stop %s is dead and only %.8g of the "
                "%.8g recorded is held; not re-protecting phantom coin",
                symbol, link, held, float(row["qty"]))
            if first:                           # one alarm/row, not one per cycle
                seen.add((symbol, link))
                self.store.journal(symbol, "STOP_DEAD_COIN_MISSING", link,
                                   {"held": held, "recorded": float(row["qty"])})
            summary.setdefault("stop_dead_unverified", []).append(symbol)
            return
        logger.critical(
            "observe_exits: the %s stop %s is dead with nothing filled; the "
            "position is NAKED", symbol, link)
        # A replacement stop is a conditional MARKET sell: it can only execute
        # on free coin, and resting take-profit legs reserve the coin they
        # offer. Free it first, or the new stop would verify as "live" and
        # still be unable to fill.
        self._cancel_take_profit_legs(symbol)
        summary.setdefault("stop_dead", []).append(symbol)
        outcome = self._protect_or_close_naked(
            symbol, row, reason="SPOT_STOP_DEAD", prefer_recorded_stop=True)
        summary.setdefault("stop_dead_outcome", {})[symbol] = outcome

    @staticmethod
    def _fill_price(fill: Optional[Mapping[str, Any]]) -> float:
        """The price an order actually executed at, from the order's own
        fields (``avgPrice``, else executed value / executed quantity), or 0.0.
        Never the entry price, a caller figure or a market price."""
        if not fill:
            return 0.0
        for getter in (
                lambda f: float(f.get("avgPrice") or 0.0),
                lambda f: (float(f.get("cumExecValue") or 0.0)
                           / float(f.get("cumExecQty") or 0.0))):
            try:
                v = getter(fill)
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            if math.isfinite(v) and v > 0.0:
                return v
        return 0.0

    @staticmethod
    def _row_qty(r: Mapping[str, Any]) -> float:
        """Quantity a closed-pnl row CLOSED: ``closedSize`` when the venue
        states it (``qty`` is the order's size, which overshoots for a
        partially filled or oversized order), else ``qty``."""
        for key in ("closedSize", "qty"):
            try:
                v = float(r.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if math.isfinite(v) and v > 0:
                return v
        return 0.0

    @staticmethod
    def _row_key(r: Mapping[str, Any]) -> str:
        """A closed-pnl row's identity: one row prices one exit."""
        return "|".join(str(r.get(k) or "") for k in (
            "orderId", "updatedTime", "closedSize", "qty", "avgExitPrice"))

    def _proven_exit_rows(self, symbol: str, rows: List[Dict[str, Any]],
                          exited: float,
                          opened_epoch: float = 0.0) -> List[Dict[str, Any]]:
        """The closed-pnl rows that are provably this exit, or [] (no proof)."""
        if not rows or len(rows) >= self._CLOSED_PNL_PAGE:
            return []
        if opened_epoch:
            # An exit of THIS position is unpriced AND unproven (it claimed no
            # closed-pnl row): which row belongs to which exit is no longer
            # something the rows can prove -- its own row looks free and could
            # price the next exit at the wrong price. Until the earlier
            # exit is priced by the venue's executions, later ones are
            # provisional too.
            try:
                if any(int(t["price_provisional"])
                       and not self._row_meta(t).get("exit_row_keys")
                       for t in self.store.lifecycle_trades(symbol, opened_epoch)):
                    return []
            except Exception:  # noqa: BLE001
                return []
        try:
            claimed = self.store.claimed_exit_row_keys(symbol)
        except Exception:  # noqa: BLE001
            return []
        fresh = [r for r in rows if self._row_key(r) not in claimed]

        def stamp(r: Mapping[str, Any]) -> float:
            try:
                return float(r.get("updatedTime", 0) or 0)
            except (TypeError, ValueError):
                return 0.0
        tolerance = max(1e-9, abs(exited) * 1e-9)
        taken: List[Dict[str, Any]] = []
        total = 0.0
        for r in sorted(fresh, key=stamp, reverse=True):
            q = self._row_qty(r)
            if q <= 0:
                continue
            taken.append(r)
            total += q
            if abs(total - exited) <= tolerance:
                return taken
            if total > exited:
                return []
        return []

    @staticmethod
    def _rows_of_this_exit(rows: List[Dict[str, Any]],
                           exited: float) -> List[Dict[str, Any]]:
        """The newest closed-pnl rows that add up to ``exited``.

        The venue lists every close since the position opened, including the
        partial exits already booked. Averaging all of them prices a later
        exit at a blend of earlier ones (a stop-out after a take-profit would
        be booked as a win). When the newest rows sum exactly to the quantity
        that has just left, those are the exit; otherwise the full list is
        returned unchanged and the caller's existing handling applies.
        """
        def stamp(r: Dict[str, Any]) -> float:
            try:
                return float(r.get("updatedTime", 0) or 0)
            except (TypeError, ValueError):
                return 0.0

        if len(rows) >= TradingEngine._CLOSED_PNL_PAGE:
            return rows          # a full page may be truncated: say nothing new
        tolerance = max(1e-9, abs(exited) * 1e-9)
        taken: List[Dict[str, Any]] = []
        total = 0.0
        for r in sorted(rows, key=stamp, reverse=True):
            try:
                qty = float(r.get("qty", 0) or 0)
            except (TypeError, ValueError):
                return rows
            if qty <= 0:
                continue
            taken.append(r)
            total += qty
            if abs(total - exited) <= tolerance:
                return taken
            if total > exited:
                return rows
        return rows

    # -- provisional exits: reconciliation ---------------------------------

    @staticmethod
    def _finite_pos(value: Any) -> Optional[float]:
        try:
            x = float(value)
        except (TypeError, ValueError):
            return None
        return x if math.isfinite(x) and x > 0.0 else None

    def validate_exit_executions(
        self, trade: Mapping[str, Any], executions: Sequence[Mapping[str, Any]],
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """Prove (or refuse) that `executions` are the fills of THIS exit.

        Returns ``(evidence, "")`` or ``(None, reason)``. Evidence is accepted
        only if every row is a Trade execution of the trade's own exit order
        and symbol, on the closing side, with a unique id, a positive finite
        price and quantity and a stated fee, and the quantities add up to the
        booked quantity exactly. Anything else - missing, duplicated with
        different content, extra, mismatched, incomplete - is refused, and the
        row stays provisional. A price is never inferred.
        """
        link = str(trade.get("order_link_id") or "")
        symbol = str(trade.get("symbol") or "")
        order_ids = {str(i) for i in (self._row_meta(trade).get("exit_order_ids")
                                      or []) if i}
        if not (link or order_ids) or not symbol:
            return None, "the provisional trade has no exit order identity"
        side = normalize_side(str(trade.get("side") or ""))
        if side not in ("Buy", "Sell"):
            return None, "the provisional trade has no side"
        exit_side = "Sell" if side == "Buy" else "Buy"
        seen: Dict[str, Mapping[str, Any]] = {}
        for ex in executions or ():
            if not isinstance(ex, Mapping):
                return None, "an execution row is not an object"
            exec_id = str(ex.get("execId") or "")
            if not exec_id:
                return None, "an execution has no execId"
            if str(ex.get("execType") or "Trade") != "Trade":
                return None, f"execution {exec_id} is not a Trade"
            ours = ((link and str(ex.get("orderLinkId") or "") == link)
                    or str(ex.get("orderId") or "") in order_ids)
            if not ours:
                return None, f"execution {exec_id} belongs to another order"
            if str(ex.get("symbol") or "") != symbol:
                return None, f"execution {exec_id} is for another symbol"
            if normalize_side(str(ex.get("side") or "")) != exit_side:
                return None, f"execution {exec_id} is on the wrong side"
            if exec_id in seen:
                if dict(seen[exec_id]) != dict(ex):
                    return None, f"execution {exec_id} appears twice differently"
                continue                    # an exact replay of one fill
            seen[exec_id] = ex
        if not seen:
            return None, "no executions"
        qty = 0.0
        notional = 0.0
        fee_by_ccy: Dict[Optional[str], float] = {}
        for exec_id, ex in seen.items():
            q = self._finite_pos(ex.get("execQty"))
            p = self._finite_pos(ex.get("execPrice"))
            if q is None or p is None:
                return None, f"execution {exec_id} has no usable price/quantity"
            raw_fee = ex.get("execFee")
            try:
                fee = float(raw_fee)
            except (TypeError, ValueError):
                return None, f"execution {exec_id} states no fee"
            if not math.isfinite(fee):
                return None, f"execution {exec_id} states a non-finite fee"
            ccy = venue_fees._clean_currency(ex.get("feeCurrency"))
            if ccy is None:
                return None, f"execution {exec_id} states no fee currency"
            qty += q
            notional += q * p
            fee_by_ccy[ccy] = fee_by_ccy.get(ccy, 0.0) + fee
        booked = float(trade.get("qty") or 0.0)
        if abs(qty - booked) > max(1e-9, abs(booked) * 1e-9):
            return None, (f"executions total {qty:.12g}, the booked exit is "
                          f"{booked:.12g}")
        fees = FeeSet(tuple(venue_fees.VenueFee(a, c)
                            for c, a in fee_by_ccy.items()),
                      True, "execution")
        return {"vwap": notional / qty, "qty": qty,
                "exec_ids": sorted(seen), "fees": fees,
                "fee_by_currency": dict(fee_by_ccy)}, ""

    def _recover_exit_order_ids(self, trade: Mapping[str, Any]) -> List[str]:
        """The venue order ids of a linear exit booked while closed-pnl was
        unreadable, from closed-pnl itself - accepted only if the rows that
        match close EXACTLY to the booked quantity and each names its order."""
        reader = getattr(self.client, "get_closed_pnl", None)
        if reader is None or not getattr(self.client, "is_linear", False):
            return []
        qty = float(trade.get("qty") or 0.0)
        try:
            rows = reader(str(trade["symbol"]), since_ms=int(
                float(trade.get("opened_epoch") or 0.0) * 1000))
        except Exception:  # noqa: BLE001
            return []
        # Only rows that could be THIS exit: not later than it, and not an
        # order another booked exit already names. The rows left must add up
        # to the booked quantity exactly; a larger pool (two unidentified exits
        # of equal size) is ambiguous, and a wrong identity would price this
        # exit from someone else's fills, so it is refused.
        closed_ms = float(trade.get("closed_epoch") or 0.0) * 1000
        try:
            claimed = self.store.claimed_exit_order_ids(
                str(trade["symbol"]), exclude_trade_id=int(trade["id"]))
        except Exception:  # noqa: BLE001
            return []

        def stamp(r: Mapping[str, Any]) -> float:
            try:
                return float(r.get("updatedTime", 0) or 0)
            except (TypeError, ValueError):
                return 0.0
        chosen = [r for r in rows
                  if str(r.get("orderId") or "") not in claimed
                  and stamp(r) <= closed_ms + 120_000]
        total = sum(float(r.get("qty", 0) or 0) for r in chosen)
        if (not chosen or abs(total - qty) > max(1e-9, abs(qty) * 1e-9)
                or any(not r.get("orderId") for r in chosen)):
            return []
        return sorted({str(r["orderId"]) for r in chosen})

    def _allocate_order_executions(
            self, trade: Mapping[str, Any],
            rows: Sequence[Mapping[str, Any]]) -> Sequence[Mapping[str, Any]]:
        """When one venue order's fills were booked as several exits (it filled
        over several cycles), give this exit ITS fills: the order's executions,
        oldest first, are dealt out to the exits that name the order, oldest
        first, and only if every boundary falls between whole executions.
        Anything that does not divide exactly is returned unchanged (and then
        fails validation): an execution is never split to make the sums work."""
        ids = {str(i) for i in (self._row_meta(trade).get("exit_order_ids") or [])}
        if not ids or not rows:
            return rows
        total = 0.0
        seen_ids = set()
        for ex in rows:
            eid = str(ex.get("execId") or "")
            if eid in seen_ids:
                continue
            seen_ids.add(eid)
            q = self._finite_pos(ex.get("execQty"))
            if q is None:
                return rows
            total += q
        booked = float(trade.get("qty") or 0.0)
        tol = max(1e-9, abs(booked) * 1e-9)
        if abs(total - booked) <= tol:
            return rows
        try:
            siblings = [t for t in self.store.lifecycle_trades(
                str(trade["symbol"]), trade["opened_epoch"])
                if ids & {str(i) for i in
                          (self._row_meta(t).get("exit_order_ids") or [])}]
        except Exception:  # noqa: BLE001
            return rows
        if len(siblings) < 2 or abs(
                sum(float(t["qty"]) for t in siblings) - total) > tol:
            return rows
        unique = {}
        for ex in rows:
            unique.setdefault(str(ex.get("execId") or ""), ex)
        ordered = sorted(unique.values(), key=lambda e: (
            str(e.get("execTime") or ""), str(e.get("execId") or "")))
        cursor = 0
        for sib in sorted(siblings, key=lambda t: (float(t["closed_epoch"]),
                                                   int(t["id"]))):
            want = float(sib["qty"])
            chunk: List[Mapping[str, Any]] = []
            got = 0.0
            while cursor < len(ordered) and got < want - tol:
                chunk.append(ordered[cursor])
                got += float(self._finite_pos(ordered[cursor].get("execQty")) or 0.0)
                cursor += 1
            if abs(got - want) > tol:
                return rows                 # a boundary inside one execution
            if int(sib["id"]) == int(trade["id"]):
                return chunk
        return rows

    def reconcile_provisional_exits(
        self, fetch: Optional[Callable[[str, str], Sequence[Mapping[str, Any]]]]
        = None,
        fetch_by_order_id: Optional[
            Callable[[str, str, Mapping[str, Any]], Sequence[Mapping[str, Any]]]]
        = None,
    ) -> Dict[str, Any]:
        """Price the exits that were booked provisional, from the venue's fills.

        `fetch(symbol, order_link_id)` / `fetch_by_order_id(symbol, order_id,
        trade)` return execution rows (defaults: the client's own paged read).
        An exit that carries venue ORDER IDS (a stop/TP the venue triggered) is
        read by those ids and the union of their fills must equal the booked
        quantity; one with only a link id is read by the link. A row is
        promoted only on evidence that `validate_exit_executions` accepts and
        whose fees convert into the accounting unit; otherwise it stays
        provisional and the reason is counted. Safe to call every cycle:
        applying the same fills twice is a no-op, and a failed fetch changes
        nothing.
        """
        summary: Dict[str, Any] = {"checked": 0, "reconciled": 0, "pending": 0,
                                   "refused": {}}
        client_read = getattr(self.client, "get_executions", None)
        fetch = fetch or client_read
        if fetch_by_order_id is None and client_read is not None:
            def fetch_by_order_id(symbol, order_id, trade):  # noqa: E306
                # The fills lie between the position's open and the moment the
                # exit was booked. Anchor on those (an exit booked after an
                # outage of more than a day is still found), inside the venue's
                # 7-day limit on one query.
                day = 86_400_000
                closed_ms = int(float(trade.get("closed_epoch") or 0.0) * 1000)
                opened_ms = int(float(trade.get("opened_epoch") or 0.0) * 1000)
                start = max(0, opened_ms - 60_000, closed_ms - 6 * day)
                return client_read(symbol, order_id=order_id, start_ms=start,
                                   end_ms=start + int(6.9 * day))
        if fetch is None and fetch_by_order_id is None:
            summary["unavailable"] = True
            return summary
        for trade in self.store.provisional_trades():
            summary["checked"] += 1
            ident = str(trade["order_link_id"] or trade["id"])
            try:
                ids = [str(i) for i in
                       (self._row_meta(trade).get("exit_order_ids") or []) if i]
                if not ids and not trade["order_link_id"]:
                    ids = self._recover_exit_order_ids(trade)
                    if ids:
                        self.store.add_trade_exit_order_ids(int(trade["id"]), ids)
                        trade = dict(trade, meta=json.dumps(
                            {**self._row_meta(trade), "exit_order_ids": ids}))
                if ids and fetch_by_order_id is not None:
                    rows: List[Mapping[str, Any]] = []
                    for oid in ids:
                        rows.extend(fetch_by_order_id(
                            str(trade["symbol"]), oid, trade))
                elif trade["order_link_id"] and fetch is not None:
                    rows = list(fetch(str(trade["symbol"]),
                                      str(trade["order_link_id"])))
                else:
                    summary["pending"] += 1
                    summary["refused"][ident] = "no exit order identity"
                    continue
            except Exception as exc:  # noqa: BLE001
                logger.error("reconcile_provisional_exits: cannot read the "
                             "fills of %s: %s", ident, exc)
                summary["pending"] += 1
                continue
            rows = list(self._allocate_order_executions(trade, rows))
            evidence, reason = self.validate_exit_executions(trade, rows)
            if evidence is None:
                summary["pending"] += 1
                summary["refused"][ident] = reason
                logger.warning("reconcile_provisional_exits: %s stays "
                               "provisional: %s", ident, reason)
                continue
            vwap, qty = evidence["vwap"], evidence["qty"]
            direction = 1.0 if normalize_side(str(trade["side"])) == "Buy" else -1.0
            gross = (vwap - float(trade["entry_price"])) * qty * direction
            leg = self._fee_leg(
                symbol=str(trade["symbol"]), fees=evidence["fees"], price=vwap,
                qty=qty, ref={"order_link_id": str(trade["order_link_id"]),
                              "exec_ids": evidence["exec_ids"]})
            if not leg["realized"]:
                # The fee could not be converted into the accounting unit with
                # a recorded basis: the economics are not confirmed, so the
                # row stays provisional rather than carry a guessed fee.
                summary["pending"] += 1
                summary["refused"][ident] = (
                    "the execution fee could not be converted into the "
                    "accounting unit")
                continue
            ok = self.store.reconcile_provisional_trade(
                int(trade["id"]), exit_price=vwap, gross_pnl=gross,
                exit_fee=float(leg["amount"]), exit_fee_realized=True,
                exit_fee_currency=leg["currency"],
                evidence={"exec_ids": evidence["exec_ids"],
                          "qty": qty, "vwap": vwap,
                          "fee_by_currency": evidence["fee_by_currency"],
                          "fee_raw": leg["raw"]})
            if ok:
                summary["reconciled"] += 1
        return summary

    def record_exit_fill(
        self,
        *,
        symbol: str,
        qty: float,
        price: float,
        fee: float,
        reason: str,
        order_link_id: str = "",
        dedupe_key: str = "",
        fees: Optional[FeeSet] = None,
        fee_ref: Optional[Mapping[str, Any]] = None,
        fee_evidence: Optional[Mapping[str, Any]] = None,
        booked_order_id: str = "",
        price_provisional: bool = False,
        exit_order_ids: Sequence[str] = (),
        exit_row_keys: Sequence[str] = (),
    ) -> bool:
        """Account for a partial or full exit that an exchange-side order filled.

        This closes a **data-flow gap** rather than a logic bug, and it is the
        kind that silently corrupts state: when a take-profit leg fills, the
        exchange holding shrinks but nothing told the position book. The row
        kept its original quantity, so the next full close asked to sell more
        than was held and was rejected — leaving a position the bot believed it
        had closed.

        There is exactly one place that decrements a position, and this is it.

        ``dedupe_key`` (typically derived from the venue's own closed-pnl
        orderId(s), see ``observe_exits``) makes the ``record_trade`` below
        a no-op on replay: a crash between that call and the
        ``remove_position``/``upsert_position`` further down leaves this
        exit re-discoverable next cycle, and it must be booked once, not
        twice. See ``StateStore.record_trade``'s docstring.

        ``fee`` is a caller figure and is NEVER treated as realised; only a
        venue-reported ``fees`` set is. ``fee_ref`` names the venue order the
        fees came from and ``fee_evidence`` carries raw non-authoritative
        evidence (closed-PnL ``openFee``/``closeFee``), stored on the trade.

        Returns True if the position is now fully closed.
        """
        positions = {p["symbol"]: p for p in self.store.open_positions()}
        row = positions.get(symbol)
        if row is None:
            logger.warning("exit fill for %s with no open position on record", symbol)
            return False

        held = float(row["qty"])
        closed_qty = min(abs(float(qty)), held)
        if closed_qty <= 0:
            return False

        entry_price = float(row["entry_price"])
        side = str(row["side"])
        direction = 1.0 if normalize_side(side) == "Buy" else -1.0
        # An exit price the venue never stated is UNKNOWN, not the entry
        # price: a provisional row carries exit_price 0 and gross 0 and is kept
        # out of every confirmed-economics reader.
        gross = (0.0 if price_provisional
                 else (float(price) - entry_price) * closed_qty * direction)
        # Entry fees are apportioned across the parts of the position that
        # close, so a partially-exited trade is not credited a full entry fee
        # twice, nor charged none at all.
        prior_meta = self._row_meta(row)
        entry_fees = FeeSet.from_dict(prior_meta.get("entry_fees"))
        fraction = closed_qty / held
        # The venue's entry fee is apportioned in its own raw currencies, and
        # the unapportioned remainder is carried forward on the position row
        # below, so the legs of a partially exited trade sum to exactly one
        # entry fee.
        entry_share = entry_fees.scaled(fraction)
        entry_remaining = entry_fees.minus(entry_share)
        entry_leg = self._fee_leg(
            symbol=symbol, fees=entry_share, price=entry_price, qty=closed_qty,
            ref=prior_meta.get("entry_fee_ref"))
        exit_leg = self._fee_leg(
            symbol=symbol, fees=fees or venue_fees.UNKNOWN_FEES,
            price=float(price), qty=closed_qty, caller_amount=float(fee or 0.0),
            ref=fee_ref)
        if price_provisional:
            # The fee was converted with a PLACEHOLDER price: whatever the
            # venue stated stays in `raw`, but the stored amount is not
            # realised economics until reconciliation re-derives it from the
            # authoritative executions.
            exit_leg = dict(exit_leg, realized=False, currency=None)

        # `record_trade` is idempotent on `dedupe_key` (see its docstring):
        # a crash between it and the position mutations below leaves this
        # exact exit re-discoverable next cycle, and it must be booked once,
        # not twice, without double-counting `daily_anchor`. The position
        # mutations that follow (`remove_position`/`upsert_position`,
        # `_cancel_protective_orders`, `journal`) are safe to repeat on that
        # replay regardless of whether the trade row was just inserted or
        # was a no-op: they are idempotent (DELETE / INSERT-ON-CONFLICT-
        # UPDATE to the SAME target values, a cancel of already-cancelled
        # orders, an append-only journal entry), and on the one call path
        # that reaches this method in production (`observe_exits`, linear
        # only) so is `move_stop`'s underlying venue call — linear's stop is
        # a field on the position (`/v5/position/trading-stop`), not a
        # separate order, so replaying it re-sets the same value rather than
        # minting a duplicate live order the way a SPOT conditional stop
        # would (see `move_stop`'s docstring and `_protect_or_close_naked`'s
        # spot-specific guard for that case, which is why the fix there
        # needed `find_live_spot_stop` and this one does not).
        trade_record = TradeRecord(
            symbol=symbol, side=side, qty=closed_qty,
            entry_price=entry_price,
            exit_price=0.0 if price_provisional else float(price),
            price_provisional=bool(price_provisional),
            gross_pnl=gross, entry_fee=entry_leg["amount"],
            exit_fee=exit_leg["amount"],
            entry_fee_realized=entry_leg["realized"],
            exit_fee_realized=exit_leg["realized"],
            entry_fee_currency=entry_leg["currency"],
            exit_fee_currency=exit_leg["currency"],
            opened_epoch=float(row.get("opened_epoch") or utc_now_epoch()),
            closed_epoch=utc_now_epoch(),
            order_link_id=order_link_id,
            meta={"reason": reason, "partial": True,
                  "exit_order_ids": [str(i) for i in exit_order_ids],
                  "exit_row_keys": [str(k) for k in exit_row_keys],
                  "funding_booked": not getattr(self.client, "is_linear", False),
                  "fees": {"entry": entry_leg["raw"], "exit": exit_leg["raw"]},
                  "fee_evidence": dict(fee_evidence or {})},
            dedupe_key=dedupe_key,
        )

        remaining = round(held - closed_qty, 12)
        full = remaining <= 1e-12
        stop_price = float(row.get("stop_price") or 0.0)
        old_stop_id = str(prior_meta.get("stop_order_link_id") or "")
        # A remainder below the exchange minimum is DUST: it cannot be
        # protected by a stop and cannot be sold, because both orders would be
        # rejected for being under the minimum quantity/notional. Tracking it as
        # an open position would mean carrying a position the system can neither
        # exit nor protect, and reporting it as protected would be a lie. It is
        # recorded as closed, with the dust logged explicitly.
        dust = False
        if not full:
            try:
                filters = self.client.get_instrument_filters(symbol)
                min_qty = float(filters.min_qty)
                min_notional = float(filters.min_notional)
            except Exception:  # noqa: BLE001
                min_qty = min_notional = 0.0
            # Linear only: the remainder is real venue size and its stop is a
            # field of the position (no minimum applies), so it stays on the
            # book. Dust is a SPOT notion -- coin that can be neither sold nor
            # protected. An unpriced exit (price 0) is no evidence the
            # remainder is small: value it at the entry price, never at zero.
            unit = float(price) if float(price) > 0.0 else entry_price
            dust = bool(
                (not getattr(self.client, "is_linear", False)) and min_qty and (
                    remaining < min_qty or remaining * unit < min_notional))

        def position_meta(stop_id: str) -> Dict[str, Any]:
            return {"stop_order_link_id": stop_id,
                    "entry_fees": entry_remaining.to_dict(),
                    "entry_fee_ref": prior_meta.get("entry_fee_ref") or {},
                    "tp_order_ids": list(prior_meta.get("tp_order_ids") or []),
                    "booked_exit_orders": sorted(
                        set(prior_meta.get("booked_exit_orders") or [])
                        | ({booked_order_id} if booked_order_id else set()))}

        # ONE transaction for everything the ledger and the position book say
        # about this exit: a kill between the trade and the position change
        # used to leave the old quantity on the book beside a booked exit, and
        # the next cycle -- seeing a different venue size -- booked a second,
        # different exit on top (the dedupe key is the position's state).
        # Venue calls (cancel, resize the stop) cannot be in a transaction and
        # follow it; each is replay-safe.
        total_dust = remaining
        with self.store.transaction():
            self.store.record_trade(trade_record)
            if full or dust:
                self.store.remove_position(symbol)
                if dust:
                    # BOOK the dust rather than forgetting it: the inventory is
                    # still on the exchange, and a book that does not say so
                    # cannot honour "the ledger explains the entire equity change".
                    total_dust = self.store.add_dust(symbol, remaining)
            else:
                self.store.upsert_position(
                    symbol, side, remaining, entry_price,
                    stop_price=stop_price,
                    order_link_id=str(row.get("order_link_id") or ""),
                    # upsert_position REPLACES meta, so the unapportioned entry
                    # fee must be carried explicitly or it vanishes on the first
                    # partial exit.
                    meta=position_meta(old_stop_id))

        if full:
            self._cooldown_after_loss(symbol)
            # OCO: the position is gone, so every sibling protective order must
            # go with it. Without this, a filled stop leaves the take-profit legs
            # resting, and they later fill against a position that no longer
            # exists -- selling inventory the bot does not hold, or re-entering
            # the market unintentionally.
            self._cancel_protective_orders(symbol)
            self.store.journal(symbol, "CLOSED", reason,
                               {"qty": closed_qty, "gross_pnl": gross})
            return True
        if dust:
            self._cancel_protective_orders(symbol)
            self.store.journal(symbol, "CLOSED", f"{reason}+dust", {
                "dust_qty": remaining,
                "dust_total": total_dust,
                "note": "remainder below exchange minimum; unsellable and "
                        "unprotectable, so the position is closed on the books "
                        "and the remainder is booked to the dust ledger",
            })
            logger.warning(
                "%s: %.10f left after a partial exit is below the exchange "
                "minimum; treating the position as closed and leaving the dust",
                symbol, remaining,
            )
            return True

        # Re-size the protective stop to the REMAINING quantity.
        #
        # This is not housekeeping. A stop left sized for the original position
        # is rejected by the exchange the moment it triggers ("insufficient
        # balance"), because a take-profit already sold part of the inventory.
        # The position then has no working protection at all, while both the
        # bot and the exchange still show a resting stop order.
        if stop_price > 0:
            exit_side = "Sell" if normalize_side(side) == "Buy" else "Buy"
            replacement = self.move_stop(
                symbol=symbol, exit_side=exit_side, qty=remaining,
                new_stop=stop_price, old_stop_link_id=old_stop_id,
            )
            if replacement and replacement != old_stop_id:
                self.store.upsert_position(
                    symbol, side, remaining, entry_price,
                    stop_price=stop_price,
                    order_link_id=str(row.get("order_link_id") or ""),
                    meta=position_meta(replacement))
            elif not replacement:
                logger.critical(
                    "could not resize the stop for %s after a partial exit; the "
                    "remaining %.8f is protected only by an over-sized stop that "
                    "the exchange will reject", symbol, remaining,
                )
        self.store.journal(symbol, "PARTIAL_EXIT", reason,
                           {"closed": closed_qty, "remaining": remaining,
                            "stop_resized_to": remaining})
        return False

    def _cancel_take_profit_legs(self, symbol: str) -> bool:
        """Cancel the resting take-profit legs for a symbol, one by one, leaving
        the stop alone. Best effort: a leg that cannot be cancelled stays open.
        Returns True only if every leg was cancelled (or there were none)."""
        try:
            legs = [r for r in self.store.unresolved_orders()
                    if str(r.get("symbol")) == symbol
                    and str(r.get("purpose")) == "tp"]
        except Exception:  # noqa: BLE001
            logger.exception("could not list take-profit legs for %s", symbol)
            return False
        all_ok = True
        for leg in legs:
            try:
                cancelled = self.client.cancel_order(
                    symbol=symbol, order_link_id=str(leg["order_link_id"]))
                # cancel_order reports a venue refusal by returning False
                if cancelled is False:
                    all_ok = False
            except Exception:  # noqa: BLE001
                all_ok = False
                logger.warning("could not cancel take-profit leg %s",
                               leg["order_link_id"], exc_info=True)
        return all_ok

    def _cancel_protective_orders(self, symbol: str) -> None:
        """Cancel every resting stop/TP for a symbol (the OCO half of an exit).

        The local rows are marked cancelled only if the venue cancel succeeded:
        a row marked cancelled over a leg that is still live would never be
        looked at again, and that leg could later sell coin the book does not
        hold. On failure they stay open for startup reconcile to chase."""
        try:
            ok = bool(self.client.cancel_all(symbol))
        except Exception:  # noqa: BLE001
            logger.exception("could not cancel protective orders for %s", symbol)
            ok = False
        rows = [r for r in self.store.unresolved_orders()
                if str(r.get("symbol")) == symbol
                and str(r.get("purpose")) in ("stop", "tp")]
        if not ok:
            if rows:
                self.store.journal(symbol, "PROTECTIVE_CANCEL_FAILED",
                                   "venue cancel-all failed; legs left open locally",
                                   {"legs": [str(r["order_link_id"]) for r in rows]})
            return
        for row in rows:
            self.store.update_order_status(str(row["order_link_id"]), "cancelled")

    # -- startup -----------------------------------------------------------

    def _record_stop_id(self, symbol: str, row: Mapping[str, Any],
                        stop_id: str) -> None:
        """Name the live protective stop (and any resting take-profit legs) on
        the row. Spot exits are observed, and partial exits resize the stop,
        from these ids; a re-protected row that lacks them is never booked on
        a stop-out and leaves the old stop live beside the resized one."""
        meta = self._row_meta(row)
        if stop_id:
            meta["stop_order_link_id"] = stop_id
        if not meta.get("tp_order_ids"):
            try:
                meta["tp_order_ids"] = [
                    str(o["order_link_id"]) for o in self.store.pending_orders()
                    if str(o.get("symbol")) == symbol
                    and str(o.get("purpose")) == "tp"]
            except Exception:  # noqa: BLE001
                logger.exception("could not recover tp ids for %s", symbol)
        current = {p["symbol"]: p for p in self.store.open_positions()}.get(symbol)
        if current is None:
            return
        self.store.upsert_position(
            symbol, str(current["side"]), float(current["qty"]),
            float(current["entry_price"]),
            stop_price=float(current.get("stop_price") or 0.0),
            order_link_id=str(current.get("order_link_id") or ""), meta=meta)

    def _protect_or_close_naked(
        self, symbol: str, row: Dict[str, Any], *, reason: str,
        prefer_recorded_stop: bool = False
    ) -> str:
        """The one reaction to "naked": re-protect, or flatten if that fails.

        Shared by every caller that can observe a naked position —
        ``reconcile()`` at startup and ``check_naked_positions()`` mid-cycle —
        so there is exactly one place this decision is made, not two dialects
        that could quietly drift apart. Returns ``"REPROTECTED"``,
        ``"FLATTENED"``, ``"FLATTEN_FAILED"`` (the close was refused; the
        position is still held and unprotected), or ``"SKIPPED_FILTERS_UNREADABLE"`` (the position is
        left naked this cycle; the caller sees it again next cycle).
        """
        side = normalize_side(str(row["side"])) or "Buy"
        exit_side = "Sell" if side == "Buy" else "Buy"
        entry = float(row["entry_price"])
        stop_fraction = float(getattr(self.cfg, "STOP_LOSS_PCT", 0.02))
        stop = entry * (1 - stop_fraction) if side == "Buy" else entry * (1 + stop_fraction)
        if prefer_recorded_stop:
            # A ratcheted/tightened stop must not be loosened to the default.
            try:
                recorded = float(row.get("stop_price") or 0.0)
            except (TypeError, ValueError):
                recorded = 0.0
            # (a recorded stop on the wrong side of the entry is not a ratchet)
            if recorded > 0.0 and (recorded < entry if side == "Buy"
                                   else recorded > entry):
                stop = recorded

        # Crash-boundary invariant: do not mint a SECOND live stop order for
        # a position that already has one resting at the venue. This is the
        # gap `place_stop_order` alone cannot close on spot — its
        # `orderLinkId` is a fresh sequence number every call, so it is
        # never deduplicated as a retry the way an entry order is. A crash
        # between the stop being accepted at the venue and this process
        # durably recording `stop_price` locally (or the earlier "position
        # found naked" read racing a stop this same process placed moments
        # before) must re-discover that stop, not double it. See
        # `BybitClient.find_live_spot_stop`'s docstring and
        # tests/test_naked_position_handling.py::
        # TestSpotNakedRecoveryIsIdempotent. Linear is guarded out here
        # (rather than relying solely on find_live_spot_stop's own
        # is_linear check) because it is never naturally duplicated on
        # linear and some test doubles for TradingEngine.client model
        # only the linear surface and do not implement this spot-only
        # method at all.
        existing = None
        if not getattr(self.client, "is_linear", False):
            existing = self.client.find_live_spot_stop(symbol)
        if existing is not None:
            live_trigger = float(existing.get("triggerPrice") or 0.0) or stop
            self.store.set_position_stop(symbol, live_trigger)
            self._record_stop_id(symbol, row, str(existing.get("orderLinkId") or ""))
            self.store.journal(symbol, "REPROTECTED", reason, {
                "mechanism": "already_live_at_venue",
                "order_link_id": existing.get("orderLinkId", ""),
                "stop_price": live_trigger,
            })
            return "REPROTECTED"

        try:
            filters = self.client.get_instrument_filters(symbol)
        except BybitAPIError:
            return "SKIPPED_FILTERS_UNREADABLE"
        placed = self._protect(
            symbol=symbol, exit_side=exit_side, qty=float(row["qty"]),
            stop_price=stop, filters=filters,
        )
        if placed:
            self.store.set_position_stop(symbol, stop)
            self._record_stop_id(symbol, row, str(placed))
            return "REPROTECTED"
        report = self._emergency_close(
            symbol=symbol, exit_side=exit_side, qty=float(row["qty"]),
            filters=filters, reason=reason,
        )
        if not (report.detail or {}).get("closed", True):
            return "FLATTEN_FAILED"
        return "FLATTENED"

    def reconcile(self) -> Dict[str, Any]:
        """Resolve state against the exchange, then protect anything naked.

        Called before the first trade of a run. A position found without a
        verified stop is not simply logged: it is protected or closed.

        This checks the LOCAL LEDGER's ``stop_price`` column
        (``StateStore.positions_without_stops()``), not a live venue read —
        it catches a stop this process itself never recorded (a lost fill, a
        crash mid-attach), but not a stop that existed and was later cleared
        AT THE VENUE while the ledger still believes it is live. See
        ``check_naked_positions()`` for that case, checked every cycle.
        """
        summary = self.client.reconcile_on_startup(
            symbols=tuple(getattr(self.cfg, "TRADING_SYMBOLS", ()) or ()))
        for symbol in list(summary.get("naked_positions") or []):
            row = {p["symbol"]: p for p in self.store.open_positions()}.get(symbol)
            if row is None:
                continue
            if getattr(self.client, "is_linear", False):
                # The venue may already be flat (closed or liquidated while we
                # were down). There is nothing to protect then, and an
                # emergency close of a flat symbol would trip the kill switch
                # for no reason; observe_exits books the real exit.
                try:
                    remote = self.client.get_position(symbol)
                    flat = float((remote or {}).get("size", 0) or 0) <= 0
                except Exception:  # noqa: BLE001
                    flat = False
                if flat:
                    logger.warning("reconcile: %s is naked in the ledger but flat "
                                   "at the venue; left for observe_exits", symbol)
                    continue
            outcome = self._protect_or_close_naked(
                symbol, row, reason="NAKED_ON_STARTUP")
            if outcome == "REPROTECTED":
                logger.warning("reconcile: protected naked position %s", symbol)
            elif outcome != "FLATTENED":
                logger.critical(
                    "reconcile: naked position %s was NOT protected or "
                    "flattened (%s); it is still unprotected", symbol, outcome)
        return summary

    def check_naked_positions(self) -> Dict[str, Any]:
        """Item 3's mid-cycle naked check — the live-venue counterpart to
        ``reconcile()``'s local-ledger-only check, run every tick.

        ``reconcile()`` trusts the ledger's ``stop_price`` column, which this
        process itself wrote; it cannot see a stop that was live and was then
        cleared AT THE VENUE while the position stayed open — the exact
        incident ``linear_protective_stop_verified`` Item 3 exists to catch.
        This reads ``verify_stop`` straight from the exchange for every open
        linear position, every cycle, so that incident is caught inside the
        cycle it happens in rather than only at the next restart.

        Linear only (spot has no position-attached stop to verify this way).
        Never places an order for a position already flat — that is
        ``observe_exits()``'s job, called first each tick; this method only
        ever sees positions the ledger still believes are open.
        """
        summary: Dict[str, Any] = {
            "checked": 0, "reprotected": [], "flattened": [], "skipped": [],
            "unread": 0,
        }
        if not getattr(self.client, "is_linear", False):
            return summary
        for row in list(self.store.open_positions()):
            symbol = str(row["symbol"])
            qty = float(row["qty"])
            if qty <= 0:
                continue
            summary["checked"] += 1
            try:
                remote = self.client.get_position(symbol)
            except BybitAPIError as exc:
                logger.error("check_naked_positions: cannot read %s at the "
                             "venue: %s", symbol, exc)
                summary["unread"] += 1
                continue
            remote_qty = abs(float((remote or {}).get("size", 0) or 0))
            if remote_qty <= 0:
                # Exited at the venue since the ledger last saw it. Booking
                # the exit is observe_exits()'s job, not this method's.
                continue
            live, detail = self.client.verify_stop(symbol=symbol, order_link_id="")
            if live:
                continue
            if detail == "NO_POSITION":
                # The venue's own stop fired between the two reads. Flat is not
                # naked: booking the exit is observe_exits()'s job, and an
                # "emergency close" of nothing would trip the kill switch.
                continue
            logger.critical("NAKED position detected mid-cycle: %s (%s)",
                            symbol, detail)
            outcome = self._protect_or_close_naked(
                symbol, row, reason="NAKED_MID_CYCLE")
            if outcome == "REPROTECTED":
                summary["reprotected"].append(symbol)
                logger.warning("check_naked_positions: re-protected %s", symbol)
            elif outcome == "FLATTENED":
                summary["flattened"].append(symbol)
            elif outcome == "FLATTEN_FAILED":
                summary.setdefault("flatten_failed", []).append(symbol)
            else:
                summary["skipped"].append(symbol)
        return summary

    def shutdown(self, cancel_protective: bool = False) -> None:
        """Graceful stop.

        Cancels working entry and take-profit orders but **keeps protective
        stops in place** unless explicitly told otherwise: a stop outliving the
        process is a feature, not a leak. The legacy ``stop()`` cancelled
        nothing and closed nothing.
        """
        # First settle what the venue already did with unresolved entries (an
        # executed spot buy is adopted and protected), so the cancels below never
        # act on an entry that in fact filled -- and the process does not exit
        # leaving a fill unprotected.
        try:
            self.resolve_unresolved_entries()
        except Exception:  # noqa: BLE001
            logger.exception("could not settle unresolved entries at shutdown")
        for row in self.store.pending_orders():
            purpose = str(row.get("purpose", ""))
            if purpose == "stop" and not cancel_protective:
                continue
            try:
                self.client.cancel_order(
                    symbol=str(row["symbol"]),
                    order_link_id=str(row["order_link_id"]),
                )
            except Exception:  # noqa: BLE001
                # One order that cannot be cancelled must not leave the rest live.
                logger.exception("shutdown could not cancel %s", row.get("order_link_id"))
        logger.info("shutdown complete; protective stops left in place=%s",
                    not cancel_protective)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _report(**kw: Any) -> ExecutionReport:
        report = ExecutionReport(**kw)
        if not report.ok:
            logger.info("trade not taken (%s/%s): %s",
                        report.symbol, report.stage, report.reason)
        return report


# The name main.py constructs.
BillionaireTradingEngine = TradingEngine


# ---------------------------------------------------------------------------
# small helpers other modules import
# ---------------------------------------------------------------------------


def should_close_position(
    *,
    side: str,
    entry_price: float,
    current_price: float,
    stop_price: float,
    take_profit_price: Optional[float] = None,
) -> Tuple[bool, str]:
    """Local exit check, used only as a backstop to exchange-side orders.

    Thresholds come from the caller, not from hardcoded defaults. The legacy
    version of this function carried its own 5%/10% defaults, which disagreed
    with the two other exit regimes in the same file (0.5% and 2%).
    """
    direction = normalize_side(side)
    if direction is None:
        return False, "UNKNOWN_SIDE"
    if direction == "Buy":
        if current_price <= stop_price:
            return True, "STOP_HIT"
        if take_profit_price and current_price >= take_profit_price:
            return True, "TAKE_PROFIT_HIT"
    else:
        if current_price >= stop_price:
            return True, "STOP_HIT"
        if take_profit_price and current_price <= take_profit_price:
            return True, "TAKE_PROFIT_HIT"
    return False, "HOLD"


def get_position_tracker(store: StateStore) -> StateStore:
    """The position book is the persisted store. There is no second one.

    The legacy ``PositionTracker`` kept an in-memory book with ``fees_paid``
    hardcoded to 0.0 and no writer, so every PnL it produced was fee-blind.
    """
    return store
