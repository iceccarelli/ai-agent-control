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
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

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
            except Exception:  # noqa: BLE001
                conditions["book"] = None
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

        filled_qty = float(fill.get("cumExecQty") or sizing.qty)
        avg_price = float(fill.get("avgPrice") or intent.entry_price)
        if filled_qty <= 0:
            return self._report(stage="fill", reason="ZERO_FILL", symbol=symbol,
                                entry_order_link_id=entry.order_link_id)

        self.store.upsert_position(
            symbol, side, filled_qty, avg_price, stop_price=0.0,
            order_link_id=entry.order_link_id,
        )

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
            meta={"stop_order_link_id": protected},
        )

        # -- 10. take-profit legs (best effort; the stop is what matters) --
        tp_ids: List[str] = []
        # Leg quantities must sum EXACTLY to the filled quantity. Sizing each
        # leg independently and letting the exchange snap it down leaves a
        # remainder that is too small to sell or protect — dust that the
        # position book then has to reason about. Giving the final leg the
        # residual removes the problem at source.
        leg_quantities: List[float] = []
        allocated = 0.0
        for index, (_, fraction) in enumerate(plan.legs):
            if index == len(plan.legs) - 1:
                leg_quantities.append(max(0.0, filled_qty - allocated))
            else:
                leg_qty = filled_qty * fraction
                leg_quantities.append(leg_qty)
                allocated += leg_qty

        for (price, _fraction), leg_qty in zip(plan.legs, leg_quantities):
            leg = self.client.place_take_profit(
                symbol=symbol, side=intent.exit_side or "Sell",
                qty=leg_qty, limit_price=price, filters=filters,
            )
            if leg.ok:
                tp_ids.append(leg.order_link_id)
            else:
                # A missing TP leg costs upside, not capital. Logged loudly and
                # carried on; a missing STOP is what triggers the emergency path.
                logger.warning("take-profit leg rejected for %s at %s: %s",
                               symbol, price, leg.reason)

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
            logger.info("maker entry rejected for %s (%s); using taker",
                        symbol, maker.reason)
            return self.client.place_order(
                symbol=symbol, side=side, qty=qty,
                order_type="Market", purpose="entry", filters=filters,
            )

        filled = self._await_fill(symbol, maker.order_link_id,
                                  timeout_s=self.maker_timeout_s)
        if filled is not None:
            return maker

        # Not filled in time. Cancel FIRST, then decide what is left to do.
        self.client.cancel_order(symbol=symbol, order_link_id=maker.order_link_id)
        residual = self.client.get_order(
            symbol=symbol, order_link_id=maker.order_link_id
        )
        already = float((residual or {}).get("cumExecQty") or 0.0)
        remaining = max(0.0, qty - already)
        if remaining <= 0.0:
            return maker
        if already > 0.0:
            logger.info("maker entry partially filled %s of %s on %s; "
                        "completing with a taker order", already, qty, symbol)
        return self.client.place_order(
            symbol=symbol, side=side, qty=remaining,
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
                    self.store.update_order_status(order_link_id, "filled")
                    return order
                if status in {"Rejected", "Cancelled", "Deactivated"}:
                    self.store.update_order_status(order_link_id, status.lower())
                    return None
            time.sleep(delay)
            delay = min(1.0, delay * 1.5)
        return None

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
        direction = 1.0 if normalize_side(side) == "Buy" else -1.0
        planned_entry = plan.stop_price + direction * abs(plan.stop_distance)
        offset = fill - planned_entry
        if abs(offset) < 1e-12:
            return plan

        legs = tuple((price + offset, fraction) for price, fraction in plan.legs)
        moved = ExitPlan(stop_price=plan.stop_price + offset, legs=legs)
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
        closed = self.client.place_order(
            symbol=symbol, side=exit_side, qty=qty, order_type="Market",
            purpose="emergency_close", filters=filters, reduce_only=True,
        )
        self.risk.trip_kill_switch(f"{reason} on {symbol}")
        self.store.journal(symbol, "EMERGENCY", reason, {
            "close_ok": closed.ok, "close_reason": closed.reason,
        })
        if closed.ok:
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
        qty = float(row["qty"])
        entry_price = float(row["entry_price"])

        result = self.client.place_order(
            symbol=symbol, side=exit_side, qty=qty, order_type="Market",
            purpose="close", reduce_only=True,
        )
        if not result.ok:
            logger.error("close failed for %s: %s", symbol, result.reason)
            return self._report(stage="close", reason=result.reason, symbol=symbol)

        fill = self._await_fill(symbol, result.order_link_id)
        realised_exit = float(
            (fill or {}).get("avgPrice") or exit_price or entry_price
        )

        self.client.cancel_all(symbol)

        direction = 1.0 if normalize_side(side) == "Buy" else -1.0
        gross = (realised_exit - entry_price) * qty * direction
        self.store.record_trade(TradeRecord(
            symbol=symbol, side=side, qty=qty, entry_price=entry_price,
            exit_price=realised_exit, gross_pnl=gross,
            entry_fee=entry_fee, exit_fee=exit_fee,
            opened_epoch=float(row.get("opened_epoch") or utc_now_epoch()),
            closed_epoch=utc_now_epoch(),
            order_link_id=result.order_link_id,
            meta={"reason": reason},
        ))
        self.store.remove_position(symbol)
        self.store.journal(symbol, "CLOSED", reason, {
            "gross_pnl": gross, "fees": entry_fee + exit_fee,
            "net_pnl": gross - entry_fee - exit_fee,
        })
        return self._report(ok=True, stage="close", reason="CLOSED", symbol=symbol,
                            qty=qty, detail={"gross_pnl": gross})

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
            return summary
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
            try:
                pnl_rows = self.client.get_closed_pnl(symbol, since_ms=since_ms)
                qty_sum = sum(float(r.get("qty", 0) or 0) for r in pnl_rows)
                if qty_sum > 0:
                    price = sum(float(r.get("avgExitPrice", 0) or 0)
                                * float(r.get("qty", 0) or 0)
                                for r in pnl_rows) / qty_sum
            except BybitAPIError as exc:
                logger.error("observe_exits: closed-pnl unreadable for %s: %s",
                             symbol, exc)
            if price is None or price <= 0:
                try:
                    price = float(self.client.get_last_price(symbol))
                except BybitAPIError as exc:
                    logger.critical("observe_exits: %s exited at the venue but no "
                                    "price can be read (%s); will retry next cycle",
                                    symbol, exc)
                    summary["unread"] += 1
                    continue
                reason = "VENUE_EXIT_PRICE_UNREAD"
                summary["price_approximated"] += 1
            fully = self.record_exit_fill(symbol=symbol, qty=exited, price=price,
                                          fee=0.0, reason=reason)
            summary["closed" if fully else "reduced"] += 1
            logger.warning("observe_exits: %s exited %.8g @ %.8g (%s) fully=%s",
                           symbol, exited, price, reason, fully)
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
    ) -> bool:
        """Account for a partial or full exit that an exchange-side order filled.

        This closes a **data-flow gap** rather than a logic bug, and it is the
        kind that silently corrupts state: when a take-profit leg fills, the
        exchange holding shrinks but nothing told the position book. The row
        kept its original quantity, so the next full close asked to sell more
        than was held and was rejected — leaving a position the bot believed it
        had closed.

        There is exactly one place that decrements a position, and this is it.

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
        gross = (float(price) - entry_price) * closed_qty * direction
        # Entry fees are apportioned across the parts of the position that
        # close, so a partially-exited trade is not credited a full entry fee
        # twice, nor charged none at all.
        entry_fee_share = (
            entry_price * closed_qty * float(getattr(self, "taker_fee_estimate", 0.001))
        )

        self.store.record_trade(TradeRecord(
            symbol=symbol, side=side, qty=closed_qty,
            entry_price=entry_price, exit_price=float(price),
            gross_pnl=gross, entry_fee=entry_fee_share, exit_fee=float(fee),
            opened_epoch=float(row.get("opened_epoch") or utc_now_epoch()),
            closed_epoch=utc_now_epoch(),
            order_link_id=order_link_id, meta={"reason": reason, "partial": True},
        ))

        remaining = held - closed_qty
        if remaining <= 1e-12:
            self.store.remove_position(symbol)
            # OCO: the position is gone, so every sibling protective order must
            # go with it. Without this, a filled stop leaves the take-profit legs
            # resting, and they later fill against a position that no longer
            # exists — selling inventory the bot does not hold, or re-entering
            # the market unintentionally.
            self._cancel_protective_orders(symbol)
            self.store.journal(symbol, "CLOSED", reason,
                               {"qty": closed_qty, "gross_pnl": gross})
            return True

        # A remainder below the exchange minimum is DUST: it cannot be
        # protected by a stop and cannot be sold, because both orders would be
        # rejected for being under the minimum quantity/notional. Tracking it as
        # an open position would mean carrying a position the system can neither
        # exit nor protect, and reporting it as protected would be a lie. It is
        # recorded as closed, with the dust logged explicitly.
        try:
            filters = self.client.get_instrument_filters(symbol)
            min_qty = float(filters.min_qty)
            min_notional = float(filters.min_notional)
        except Exception:  # noqa: BLE001
            min_qty = min_notional = 0.0
        if min_qty and (
            remaining < min_qty or remaining * float(price) < min_notional
        ):
            self.store.remove_position(symbol)
            self._cancel_protective_orders(symbol)
            # BOOK the dust rather than forgetting it. The position is closed
            # because the remainder cannot be sold or protected; the inventory
            # is still on the exchange, and a book that does not say so cannot
            # honour "the ledger explains the entire equity change".
            total_dust = remaining
            try:
                total_dust = self.store.add_dust(symbol, remaining)
            except Exception:  # noqa: BLE001
                logger.warning("could not record dust for %s", symbol, exc_info=True)
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

        stop_price = float(row.get("stop_price") or 0.0)
        meta = {}
        try:
            meta = json.loads(str(row.get("meta") or "{}"))
        except Exception:  # noqa: BLE001
            meta = {}
        old_stop_id = str(meta.get("stop_order_link_id") or "")

        # Re-size the protective stop to the REMAINING quantity.
        #
        # This is not housekeeping. A stop left sized for the original position
        # is rejected by the exchange the moment it triggers ("insufficient
        # balance"), because a take-profit already sold part of the inventory.
        # The position then has no working protection at all, while both the
        # bot and the exchange still show a resting stop order. A backtest
        # reconciliation run surfaced this: positions were entered and never
        # exited, because every stop trigger bounced.
        new_stop_id = old_stop_id
        if stop_price > 0:
            exit_side = "Sell" if normalize_side(side) == "Buy" else "Buy"
            replacement = self.move_stop(
                symbol=symbol, exit_side=exit_side, qty=remaining,
                new_stop=stop_price, old_stop_link_id=old_stop_id,
            )
            if replacement:
                new_stop_id = replacement
            else:
                logger.critical(
                    "could not resize the stop for %s after a partial exit; the "
                    "remaining %.8f is protected only by an over-sized stop that "
                    "the exchange will reject", symbol, remaining,
                )

        self.store.upsert_position(
            symbol, side, remaining, entry_price,
            stop_price=stop_price,
            order_link_id=str(row.get("order_link_id") or ""),
            meta={"stop_order_link_id": new_stop_id},
        )
        self.store.journal(symbol, "PARTIAL_EXIT", reason,
                           {"closed": closed_qty, "remaining": remaining,
                            "stop_resized_to": remaining})
        return False

    def _cancel_protective_orders(self, symbol: str) -> None:
        """Cancel every resting stop/TP for a symbol (the OCO half of an exit)."""
        try:
            self.client.cancel_all(symbol)
        except Exception:  # noqa: BLE001
            logger.exception("could not cancel protective orders for %s", symbol)
        for row in self.store.pending_orders():
            if str(row.get("symbol")) == symbol and str(row.get("purpose")) in ("stop", "tp"):
                self.store.update_order_status(str(row["order_link_id"]), "cancelled")

    # -- startup -----------------------------------------------------------

    def reconcile(self) -> Dict[str, Any]:
        """Resolve state against the exchange, then protect anything naked.

        Called before the first trade of a run. A position found without a
        verified stop is not simply logged: it is protected or closed.
        """
        summary = self.client.reconcile_on_startup(
            symbols=tuple(getattr(self.cfg, "TRADING_SYMBOLS", ()) or ()))
        for symbol in list(summary.get("naked_positions") or []):
            row = {p["symbol"]: p for p in self.store.open_positions()}.get(symbol)
            if row is None:
                continue
            side = normalize_side(str(row["side"])) or "Buy"
            exit_side = "Sell" if side == "Buy" else "Buy"
            entry = float(row["entry_price"])
            stop_fraction = float(getattr(self.cfg, "STOP_LOSS_PCT", 0.02))
            stop = entry * (1 - stop_fraction) if side == "Buy" else entry * (1 + stop_fraction)
            try:
                filters = self.client.get_instrument_filters(symbol)
            except BybitAPIError:
                continue
            placed = self._protect(
                symbol=symbol, exit_side=exit_side, qty=float(row["qty"]),
                stop_price=stop, filters=filters,
            )
            if placed:
                self.store.set_position_stop(symbol, stop)
                logger.warning("reconcile: protected naked position %s", symbol)
            else:
                self._emergency_close(
                    symbol=symbol, exit_side=exit_side, qty=float(row["qty"]),
                    filters=filters, reason="NAKED_ON_STARTUP",
                )
        return summary

    def shutdown(self, cancel_protective: bool = False) -> None:
        """Graceful stop.

        Cancels working entry and take-profit orders but **keeps protective
        stops in place** unless explicitly told otherwise: a stop outliving the
        process is a feature, not a leak. The legacy ``stop()`` cancelled
        nothing and closed nothing.
        """
        for row in self.store.pending_orders():
            purpose = str(row.get("purpose", ""))
            if purpose == "stop" and not cancel_protective:
                continue
            self.client.cancel_order(
                symbol=str(row["symbol"]),
                order_link_id=str(row["order_link_id"]),
            )
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
