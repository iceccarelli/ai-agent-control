"""An unwind that closes ONE leg and not the other is a halt, never "flat".

Mutation testing of `CarryEngine._unwind` (ACQUIRE mode) found that replacing
`perp_left > QTY_DUST or spot_left > QTY_DUST` with `and` survived every test in
the tree. With `and`, a close in which the perp short is fully bought back but
the spot sale fills short (a partial fill, a lot the venue will not split) is
read as "nothing left": the book goes FLAT with a naked long still held, and the
next candle may open a second hedge against it. The same is true of a perp left
open behind a spot that sold in full. `_partial_close` is the answer the code
already has (record what the venue still holds, halt, call a human); these tests
pin that it is the answer whichever single leg is left.

The unwind sends only reduce-only closes in proportion to what actually closed,
so nothing here can enlarge an exposure.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_carry_engine import SPOT, FakeBroker, engine  # noqa: E402

from carry_engine import BookState  # noqa: E402


class LegFills(FakeBroker):
    """Market orders fill `perp_sell`/`perp_buy`/`spot_sell` of the quantity
    asked (1.0 = in full, None = the order is rejected)."""

    def __init__(self, *, spot_sell=1.0, perp_buy=1.0, **kw):
        super().__init__(**kw)
        self.spot_sell, self.perp_buy = spot_sell, perp_buy
        self.armed = False

    def place_market(self, *, symbol, side, qty, product):
        ratio = None
        if self.armed and product == "spot" and side == "Sell":
            ratio = self.spot_sell
        elif self.armed and product == "linear" and side == "Buy":
            ratio = self.perp_buy
        if ratio is None and not (self.armed and (
                (product == "spot" and side == "Sell")
                or (product == "linear" and side == "Buy"))):
            return super().place_market(symbol=symbol, side=side, qty=qty,
                                        product=product)
        self.orders.append((symbol, side, qty, product))
        if ratio is None:
            return None
        return {"filled_qty": qty * ratio, "avg_price": self.mark,
                "order_link_id": f"{product}-{len(self.orders)}",
                "fee_detail": {"known": True, "source": "cumFeeDetail",
                               "note": "scripted", "components": [
                                   {"amount": 0.0, "currency": "USDT"}]}}


def opened(**fills):
    broker = LegFills(**fills)
    eng = engine(broker)
    d = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=6.0)
    assert d.action == "opened", (d.action, d.reason)
    return eng, broker


def trigger_unwind(eng, broker):
    broker.armed = True
    broker.margin = 1.1                       # MARGIN_HEADROOM below the floor
    return eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=6.0)


def test_a_clean_unwind_still_goes_flat():
    eng, broker = opened()
    d = trigger_unwind(eng, broker)
    assert d.action == "unwound" and eng.position is None
    assert eng.state is BookState.FLAT


def test_the_perp_closes_but_the_spot_sale_fills_short_halts_with_the_spot_kept():
    eng, broker = opened(spot_sell=0.5)
    held = eng.position.spot.filled_qty
    d = trigger_unwind(eng, broker)
    assert (d.action, d.reason) == ("halted", "UNWIND_PARTIAL"), (d.action, d.reason)
    assert eng.state is BookState.HALTED and eng.position is not None
    assert eng.position.perp.filled_qty == pytest.approx(0.0, abs=1e-9)
    assert eng.position.spot.filled_qty == pytest.approx(held * 0.5, rel=1e-6)


def test_the_perp_closes_but_the_spot_sale_is_rejected_halts_with_the_spot_kept():
    eng, broker = opened(spot_sell=None)
    held = eng.position.spot.filled_qty
    d = trigger_unwind(eng, broker)
    assert (d.action, d.reason) == ("halted", "UNWIND_PARTIAL"), (d.action, d.reason)
    assert eng.position is not None
    assert eng.position.spot.filled_qty == pytest.approx(held, rel=1e-6)


def test_the_perp_buy_back_fills_short_halts_and_keeps_the_remainder():
    """Half the short is still open, so HALF the spot must stay with it: selling
    the spot in full against a half-open short would leave that half naked."""
    eng, broker = opened(perp_buy=0.5)
    short = eng.position.perp.filled_qty
    held = eng.position.spot.filled_qty
    d = trigger_unwind(eng, broker)
    assert (d.action, d.reason) == ("halted", "UNWIND_PARTIAL"), (d.action, d.reason)
    assert eng.position is not None
    assert eng.position.perp.filled_qty == pytest.approx(short * 0.5, rel=1e-6)
    assert eng.position.spot.filled_qty == pytest.approx(held * 0.5, rel=1e-3)
    sold = sum(o[2] for o in broker.orders if o[3] == "spot" and o[1] == "Sell")
    assert sold == pytest.approx(held * 0.5, rel=1e-3)


def test_the_perp_not_closing_at_all_never_sells_the_spot():
    eng, broker = opened(perp_buy=None)
    d = trigger_unwind(eng, broker)
    assert (d.action, d.reason) == ("halted", "UNWIND_INCOMPLETE")
    assert not any(o[3] == "spot" and o[1] == "Sell" for o in broker.orders), \
        "selling the spot against a short that is still open would leave it naked"


@pytest.mark.parametrize("perp_buy", [None, 0.0], ids=["rejected", "filled-zero"])
def test_a_perp_that_closed_nothing_is_incomplete_and_never_sells_the_spot(perp_buy):
    eng, broker = opened(perp_buy=perp_buy)
    d = trigger_unwind(eng, broker)
    assert (d.action, d.reason) == ("halted", "UNWIND_INCOMPLETE"), (d.action, d.reason)
    assert eng.position is not None
    assert not any(o[3] == "spot" and o[1] == "Sell" for o in broker.orders), \
        "selling the spot against a short that is still open would leave it naked"


@pytest.mark.parametrize("perp_buy", [None, 0.0], ids=["rejected", "filled-zero"])
def test_overlay_a_hedge_that_did_not_close_is_incomplete_and_sells_nothing(perp_buy):
    from carry_engine import CarryEngine
    from carry_risk import CarryRisk

    class Overlay(LegFills):
        def get_spot_inventory(self, symbol):
            return 5.0

    broker = Overlay(perp_buy=perp_buy)
    eng = engine(broker, execution_mode="overlay", borrow_apr=0.0)
    assert isinstance(eng, CarryEngine) and isinstance(eng.pair_risk, CarryRisk)
    d = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=6.0)
    assert d.action == "opened", (d.action, d.reason)
    d = trigger_unwind(eng, broker)
    assert (d.action, d.reason) == ("halted", "UNWIND_INCOMPLETE"), (d.action, d.reason)
    assert eng.position is not None
    assert not any(o[3] == "spot" for o in broker.orders), \
        "an overlay never trades the client's spot"


# -- the perp never landed: the naked spot ------------------------------------

class PerpRejected(LegFills):
    """The spot BUY lands, the perp SELL is rejected, and the emergency spot
    sale fills `spot_sell` of what is asked."""

    def place_market(self, *, symbol, side, qty, product):
        if product == "linear":
            self.orders.append((symbol, side, qty, product))
            return None
        if product == "spot" and side == "Sell":
            self.orders.append((symbol, side, qty, product))
            if self.spot_sell is None:
                return None
            return {"filled_qty": qty * self.spot_sell, "avg_price": self.mark,
                    "order_link_id": f"spot-{len(self.orders)}",
                    "fee_detail": {"known": True, "source": "cumFeeDetail",
                                   "note": "scripted", "components": [
                                       {"amount": 0.0, "currency": "USDT"}]}}
        return FakeBroker.place_market(self, symbol=symbol, side=side, qty=qty,
                                       product=product)


def entry_with_a_rejected_perp(spot_sell):
    broker = PerpRejected(spot_sell=spot_sell)
    eng = engine(broker)
    d = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=6.0)
    return eng, broker, d


def test_a_naked_spot_sold_in_full_is_flat_and_spends_the_days_entry():
    eng, broker, d = entry_with_a_rejected_perp(spot_sell=1.0)
    assert d.action == "unwound" and eng.position is None
    assert len(eng.pair_risk.export_days()["broken"]) == 1, \
        "a spot round trip was paid for nothing; it spends today's allowance"


def test_a_naked_spot_sold_in_part_halts_keeps_the_rest_and_spends_the_days_entry():
    eng, broker, d = entry_with_a_rejected_perp(spot_sell=0.5)
    assert (d.action, d.reason) == ("halted", "NAKED_SPOT_UNWIND_PARTIAL")
    assert eng.position is not None and eng.position.perp.filled_qty == 0.0
    assert eng.position.spot.filled_qty > 0.0
    assert len(eng.pair_risk.export_days()["broken"]) == 1, \
        "otherwise a venue that keeps rejecting the perp makes the book buy " \
        "spot again on the next tick"


def test_a_naked_spot_that_cannot_be_sold_at_all_halts_and_keeps_it():
    eng, broker, d = entry_with_a_rejected_perp(spot_sell=None)
    assert (d.action, d.reason) == ("halted", "NAKED_SPOT_UNWIND_FAILED")
    assert eng.position is not None and eng.position.spot.filled_qty > 0.0


# -- an order quantity that is not a usable number is never sent ---------------

@pytest.mark.parametrize("qty", [float("nan"), float("inf"), 0.0, -1.0])
def test_fire_sends_nothing_for_a_quantity_that_is_not_a_positive_finite_number(qty):
    broker = FakeBroker()
    eng = engine(broker, primed=False)
    assert eng._fire("BTCUSDT", "Sell", qty, "spot") is None
    assert eng._fire("BTCUSDT", "Buy", qty, "linear") is None
    assert broker.orders == []
