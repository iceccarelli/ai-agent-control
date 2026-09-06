"""Delta-neutral carry book: two legs, one position, executed on candle receipt.

WHAT THIS IS
============
The trading system in this tree holds ONE leg at a time and bets on direction.
That trade is arithmetically dead: 25 bps round trip x ~36 entries a year is
900 bps of friction against a ~15 bps edge, and eleven signal families died
proving it.

This module holds TWO legs and bets on nothing.

    LONG spot BTC  +  SHORT perp BTC  =  zero price exposure

Price goes up, the spot leg gains what the perp leg loses. Price goes down, the
reverse. You are flat. What you keep is the funding the leveraged longs pay to
be long, every eight hours, forever.

On this repo's own 4,431-print corpus:

    funding positive          85.4% of prints
    mean rate                 +0.625 bps per 8h
    collected over 4.05 yrs   27.70% of notional  =  6.84%/yr gross
    entry cost                31 bps ONE TIME, not per period

You are not predicting. You are supplying balance sheet to people who want
leverage, and charging them for it. That is a business with an economic reason
to exist, which is exactly what direction-guessing on daily bars never had.

THE THING THAT KILLS THIS TRADE
===============================
One leg landing without the other. A spot buy that fills while the perp short
is rejected is not a hedged book — it is a naked long at whatever size you
sized the CARRY at, which is the size you would never take directionally. The
same trade run carelessly is how funds die: not from the carry going negative,
but from waking up delta-long into a crash because a leg failed at 3am.

So the invariants here are not caution. They are the product:

  1. LEGS LAND TOGETHER OR NOT AT ALL. A partial pair is unwound immediately.
     If the unwind fails, the kill switch trips. There is no third option.
  2. DELTA IS MEASURED, NOT ASSUMED. The hedge drifts as price moves and as
     legs fill at different prices. It is recomputed on every candle.
  3. LEVERAGE IS ON THE SPREAD, NOT ON A VIEW. 10x on a delta-neutral pair is
     10x on a carry spread. 10x on a directional bet is a margin call with a
     countdown. This module physically cannot express the second one: it has
     no direction parameter.
  4. THE SHORT LEG CAN BE LIQUIDATED. Spot cannot. Margin headroom on the perp
     is checked before every action and is the first thing to trip.

WHAT EXECUTES
=============
`on_candle()` is the whole loop. It runs on every closed bar and it either acts
or states why it did not. In order:

    liquidation headroom -> delta drift -> funding decision -> open / rebalance
                                                            / harvest / unwind

No LLM sits anywhere on that path. A forecaster may later size the book by
predicting the next funding rate — a tractable problem where being wrong means
collecting less, not losing — but it will hand this module a NOTIONAL, never a
direction. This module has no direction to give it.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: Rebalance when |delta| exceeds this fraction of book notional. Below it,
#: churning the legs costs more in fees than the residual exposure costs in
#: risk. 2% of notional on a $100k book is $2k of drift — a rounding error
#: against the carry, and far cheaper than two taker fees to correct.
DELTA_BAND = 0.02

#: Refuse to open, and start unwinding, below this maintenance-margin multiple
#: on the short perp leg. 2.0x means the perp can move 50% against the short
#: before liquidation. The spot leg is the collateral of last resort and can be
#: sold to post margin, but selling spot in a squeeze is the worst execution in
#: the world, so the trigger sits well before it is needed.
MIN_MARGIN_MULTIPLE = 2.0

#: Do not open new carry below this forecast 8h rate. At 0.2 bps per 8h the
#: annualised gross is ~2.2%, which does not clear the 31 bps entry cost inside
#: a reasonable hold. Below it, hold cash.
MIN_ENTRY_FUNDING_BPS = 0.2

#: Unwind when funding has been negative this many consecutive prints. Negative
#: funding means the trade has inverted: you are now PAYING to hold the hedge.
#: Three prints is one day — long enough not to react to a single squeeze,
#: short enough to leave before a regime.
NEGATIVE_FUNDING_EXIT_PRINTS = 3


class BookState(Enum):
    FLAT = "FLAT"
    OPENING = "OPENING"
    HEDGED = "HEDGED"
    DRIFTED = "DRIFTED"
    UNWINDING = "UNWINDING"
    HALTED = "HALTED"


@dataclass
class Leg:
    """One side of the pair. Filled quantity is what the VENUE says, not what
    was requested — the difference between those two numbers is the naked
    exposure this module exists to eliminate."""

    symbol: str
    side: str                    # "Buy" (spot long) or "Sell" (perp short)
    product: str                 # "spot" or "linear"
    requested_qty: float = 0.0
    filled_qty: float = 0.0
    avg_price: float = 0.0
    order_link_id: str = ""

    @property
    def notional(self) -> float:
        return self.filled_qty * self.avg_price

    @property
    def is_complete(self) -> bool:
        if self.requested_qty <= 0:
            return False
        return abs(self.filled_qty - self.requested_qty) <= 1e-9


@dataclass
class CarryPosition:
    """A hedged pair. Delta is DERIVED from fills, never carried as a belief."""

    spot: Leg
    perp: Leg
    opened_ms: int = 0
    funding_collected: float = 0.0
    negative_funding_streak: int = 0

    @property
    def delta_qty(self) -> float:
        """Net BTC exposure. Zero is the whole point of the trade."""
        return self.spot.filled_qty - self.perp.filled_qty

    def delta_fraction(self, mark: float) -> float:
        """|delta| as a fraction of book notional. The number that trips a
        rebalance."""
        book = self.spot.notional + self.perp.notional
        if book <= 0:
            return 0.0
        return abs(self.delta_qty * mark) / book

    @property
    def is_paired(self) -> bool:
        return self.spot.is_complete and self.perp.is_complete


@dataclass
class CarryDecision:
    """What the engine did, and why. `acted` is False unless an order went out."""

    action: str
    acted: bool = False
    reason: str = ""
    state: BookState = BookState.FLAT
    detail: Dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.acted


class CarryEngine:
    """Holds the pair. Acts on candles. Refuses on anything it cannot verify.

    Constructed with a broker adapter exposing:
        place_market(symbol, side, qty, product) -> {filled_qty, avg_price, order_link_id}
        get_margin_multiple(symbol) -> float
        get_mark(symbol) -> float
    and a kill switch callable. Everything else is arithmetic done here.
    """

    def __init__(self, *, broker: Any, kill_switch: Any = None,
                 spot_symbol: str = "BTCUSDT", perp_symbol: str = "BTCUSDT",
                 max_notional_usd: float = 100.0,
                 delta_band: float = DELTA_BAND,
                 min_margin_multiple: float = MIN_MARGIN_MULTIPLE,
                 min_entry_funding_bps: float = MIN_ENTRY_FUNDING_BPS) -> None:
        self.broker = broker
        self.kill_switch = kill_switch
        self.spot_symbol = spot_symbol
        self.perp_symbol = perp_symbol
        self.max_notional_usd = float(max_notional_usd)
        self.delta_band = float(delta_band)
        self.min_margin_multiple = float(min_margin_multiple)
        self.min_entry_funding_bps = float(min_entry_funding_bps)
        self.position: Optional[CarryPosition] = None
        self.state = BookState.FLAT

    # -- the loop ---------------------------------------------------------

    def on_candle(self, *, mark: float, funding_bps: float,
                  timestamp_ms: int = 0) -> CarryDecision:
        """One closed bar. Act or state why not. This is the entire loop.

        Order is not cosmetic. Liquidation headroom is checked before anything
        else because a margin call does not wait for the funding decision, and
        delta is checked before opening more because adding size to a drifted
        book compounds the exposure you already failed to hedge.
        """
        if self.state is BookState.HALTED:
            return CarryDecision("halted", reason="KILL_SWITCH_ENGAGED",
                                 state=self.state)

        if not self._finite(mark) or mark <= 0:
            return self._halt("MARK_UNREADABLE",
                              "a book that cannot be marked cannot be hedged")

        if self.position is not None:
            headroom = self._check_margin()
            if headroom is not None:
                return headroom

            drift = self.position.delta_fraction(mark)
            if drift > self.delta_band:
                return self._rebalance(mark, drift)

            if funding_bps < 0:
                self.position.negative_funding_streak += 1
                if (self.position.negative_funding_streak
                        >= NEGATIVE_FUNDING_EXIT_PRINTS):
                    return self._unwind(mark, "FUNDING_INVERTED")
            else:
                self.position.negative_funding_streak = 0
                self.position.funding_collected += (
                    funding_bps / 1e4) * self.position.perp.notional

            return CarryDecision("hold", reason="HEDGED_AND_COLLECTING",
                                 state=BookState.HEDGED,
                                 detail={"delta_fraction": drift,
                                         "funding_bps": funding_bps,
                                         "collected": self.position.funding_collected})

        if funding_bps < self.min_entry_funding_bps:
            return CarryDecision(
                "stand_aside", reason="FUNDING_TOO_THIN_TO_CLEAR_ENTRY_COST",
                state=BookState.FLAT,
                detail={"funding_bps": funding_bps,
                        "required_bps": self.min_entry_funding_bps})

        return self._open(mark, funding_bps, timestamp_ms)

    # -- actions ----------------------------------------------------------

    def _open(self, mark: float, funding_bps: float,
              timestamp_ms: int) -> CarryDecision:
        """Both legs, or neither. There is no partially-open carry position."""
        qty = self.max_notional_usd / mark
        if qty <= 0 or not self._finite(qty):
            return self._halt("SIZE_INVALID", "refusing an unsizable book")

        margin = self._margin_multiple()
        if margin is not None and margin < self.min_margin_multiple:
            return CarryDecision("stand_aside", reason="MARGIN_HEADROOM_TOO_THIN",
                                 state=BookState.FLAT,
                                 detail={"margin_multiple": margin})

        self.state = BookState.OPENING
        spot = self._fire(self.spot_symbol, "Buy", qty, "spot")
        if spot is None or spot.filled_qty <= 0:
            self.state = BookState.FLAT
            return CarryDecision("refused", reason="SPOT_LEG_DID_NOT_FILL",
                                 state=BookState.FLAT)

        # Hedge exactly what the spot leg ACTUALLY filled. Hedging the requested
        # size instead is how a rounding difference becomes a permanent short.
        perp = self._fire(self.perp_symbol, "Sell", spot.filled_qty, "linear")
        if perp is None or perp.filled_qty <= 0:
            return self._emergency_unwind_spot(
                spot, "PERP_LEG_DID_NOT_FILL — spot is naked, unwinding now")

        position = CarryPosition(spot=spot, perp=perp, opened_ms=timestamp_ms)
        residual = position.delta_fraction(mark)
        if residual > self.delta_band:
            # Both legs landed but the sizes disagree by more than the band.
            # That is not a hedge, it is a directional position wearing one.
            self.position = position
            return self._unwind(mark, "LEGS_LANDED_UNPAIRED")

        self.position = position
        self.state = BookState.HEDGED
        return CarryDecision(
            "opened", acted=True, reason="PAIR_LANDED", state=BookState.HEDGED,
            detail={"qty": spot.filled_qty, "spot_price": spot.avg_price,
                    "perp_price": perp.avg_price, "delta_fraction": residual,
                    "funding_bps": funding_bps})

    def _rebalance(self, mark: float, drift: float) -> CarryDecision:
        """Bring delta back inside the band by trading the PERP leg only.

        The perp is the cheap leg (5.5 bps vs 10 bps taker) and the one that can
        be shorted freely. Correcting on the spot side would mean selling
        collateral, which is the last thing to touch.
        """
        position = self.position
        assert position is not None
        delta = position.delta_qty
        side = "Sell" if delta > 0 else "Buy"
        fill = self._fire(self.perp_symbol, side, abs(delta), "linear")
        if fill is None or fill.filled_qty <= 0:
            return self._halt(
                "REBALANCE_FAILED",
                f"delta is {drift:.2%} of book and the correction did not fill")

        if side == "Sell":
            position.perp.filled_qty += fill.filled_qty
        else:
            position.perp.filled_qty -= fill.filled_qty
        self.state = BookState.HEDGED
        return CarryDecision(
            "rebalanced", acted=True, reason="DELTA_RETURNED_TO_BAND",
            state=BookState.HEDGED,
            detail={"drift_before": drift, "corrected_qty": fill.filled_qty,
                    "drift_after": position.delta_fraction(mark)})

    def _unwind(self, mark: float, reason: str) -> CarryDecision:
        """Close both legs. Perp first: it is the leg that can be liquidated."""
        position = self.position
        assert position is not None
        self.state = BookState.UNWINDING
        perp = self._fire(self.perp_symbol, "Buy", position.perp.filled_qty,
                          "linear")
        spot = self._fire(self.spot_symbol, "Sell", position.spot.filled_qty,
                          "spot")
        if perp is None or spot is None:
            return self._halt(
                "UNWIND_INCOMPLETE",
                f"{reason} — a leg did not close and the book is now naked")
        self.position = None
        self.state = BookState.FLAT
        return CarryDecision("unwound", acted=True, reason=reason,
                             state=BookState.FLAT,
                             detail={"collected": position.funding_collected})

    def _emergency_unwind_spot(self, spot: Leg, reason: str) -> CarryDecision:
        """The perp never landed. Sell the spot immediately; it is naked long."""
        self.state = BookState.UNWINDING
        closed = self._fire(self.spot_symbol, "Sell", spot.filled_qty, "spot")
        if closed is None or closed.filled_qty <= 0:
            return self._halt("NAKED_SPOT_UNWIND_FAILED", reason)
        self.position = None
        self.state = BookState.FLAT
        return CarryDecision("unwound", acted=True, reason=reason,
                             state=BookState.FLAT)

    # -- guards -----------------------------------------------------------

    def _check_margin(self) -> Optional[CarryDecision]:
        margin = self._margin_multiple()
        if margin is None:
            return self._halt("MARGIN_UNREADABLE",
                              "an unreadable margin is a margin call you cannot see")
        if margin < self.min_margin_multiple:
            return self._unwind(self.broker.get_mark(self.perp_symbol),
                                f"MARGIN_HEADROOM_{margin:.2f}x_BELOW_FLOOR")
        return None

    def _margin_multiple(self) -> Optional[float]:
        try:
            value = float(self.broker.get_margin_multiple(self.perp_symbol))
        except Exception:  # noqa: BLE001
            return None
        return value if self._finite(value) else None

    def _fire(self, symbol: str, side: str, qty: float,
              product: str) -> Optional[Leg]:
        if qty <= 0 or not self._finite(qty):
            return None
        try:
            result = self.broker.place_market(symbol=symbol, side=side,
                                              qty=qty, product=product)
        except Exception as exc:  # noqa: BLE001
            logger.error("leg %s %s %s failed: %s", side, symbol, product, exc)
            return None
        if not result:
            return None
        return Leg(symbol=symbol, side=side, product=product,
                   requested_qty=qty,
                   filled_qty=float(result.get("filled_qty", 0.0) or 0.0),
                   avg_price=float(result.get("avg_price", 0.0) or 0.0),
                   order_link_id=str(result.get("order_link_id", "")))

    def _halt(self, reason: str, detail: str) -> CarryDecision:
        self.state = BookState.HALTED
        if self.kill_switch is not None:
            try:
                self.kill_switch(reason)
            except Exception:  # noqa: BLE001
                logger.critical("kill switch itself failed on %s", reason)
        logger.critical("carry book HALTED: %s — %s", reason, detail)
        return CarryDecision("halted", reason=reason, state=BookState.HALTED,
                             detail={"detail": detail})

    @staticmethod
    def _finite(value: Any) -> bool:
        try:
            return math.isfinite(float(value))
        except (TypeError, ValueError):
            return False
