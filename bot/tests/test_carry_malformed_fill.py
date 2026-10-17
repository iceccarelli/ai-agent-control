"""A create response whose fill cannot be trusted is never "the leg did not fill".

The create call returned, so the order exists. Its `cumExecQty` / `avgPrice`
are claims: text, NaN/inf (every comparison against which is False), a negative
quantity or more than was asked for must send the broker to the order's own
record, and to `OrderOutcomeUnknown` (a HALT) when that record cannot settle it.

Offline: the venue is the scripted `FeeVenue` of test_carry_fee_truth; no
network, no credential.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from carry_broker import OrderOutcomeUnknown  # noqa: E402
from carry_engine import BookState  # noqa: E402
from test_carry_fee_truth import (  # noqa: E402
    MARK, build, kinds, modern, open_the_hedge)

BAD = [("NaN", str(MARK)), ("inf", str(MARK)), ("-inf", str(MARK)),
       ("abc", str(MARK)), ("-0.5", str(MARK)),
       ("0.5", "NaN"), ("0.5", "abc"), ("0.5", "0"), ("0.5", "-1"),
       ("999", str(MARK))]                    # more than was asked for


def corrupt_create(venue, *, category, side, qty_text, price_text,
                   readback="venue"):
    """The matching create executes, but its RESPONSE carries bad fill text.

    readback: "venue" = the order's own record is honest; "gone" = the venue
    shows no row; "bad" = the record is corrupt too.
    """
    real = venue._request

    def request(method, endpoint, **kw):
        body = kw.get("body") or {}
        hit = (endpoint == "/v5/order/create" and body.get("category") == category
               and body.get("side") == side)
        if hit:
            out = real(method, endpoint, **kw)
            out["result"] = dict(out["result"], cumExecQty=qty_text,
                                 avgPrice=price_text)
            request.sent = True
            return out
        if request.sent and endpoint in ("/v5/order/realtime",
                                         "/v5/order/history"):
            if readback == "gone":
                return {"retCode": 0, "retMsg": "OK", "result": {"list": []}}
            if readback == "bad":
                out = real(method, "/v5/order/realtime", **kw)
                rows = out["result"]["list"]
                if rows:
                    rows[0]["cumExecQty"] = "NaN"
                return out
        return real(method, endpoint, **kw)

    request.sent = False
    venue._request = request


def place(engine, product="linear", side="Sell", qty=0.001):
    return engine.broker.place_market(symbol="BTCUSDT", side=side, qty=qty,
                                      product=product)


@pytest.mark.parametrize("qty_text,price_text", BAD)
class TestTheBrokerDoesNotTrustAMalformedCreateFill:
    def test_unreadable_record_is_an_unknown_outcome_not_a_zero_fill(
            self, qty_text, price_text):
        venue, _j, engine = build(modern)
        corrupt_create(venue, category="linear", side="Sell",
                       qty_text=qty_text, price_text=price_text,
                       readback="gone")
        with pytest.raises(OrderOutcomeUnknown):
            place(engine)

    def test_a_corrupt_record_is_an_unknown_outcome_too(self, qty_text,
                                                        price_text):
        venue, _j, engine = build(modern)
        corrupt_create(venue, category="linear", side="Sell",
                       qty_text=qty_text, price_text=price_text,
                       readback="bad")
        with pytest.raises(OrderOutcomeUnknown):
            place(engine)

    def test_an_honest_record_is_the_authority(self, qty_text, price_text):
        venue, _j, engine = build(modern)
        corrupt_create(venue, category="linear", side="Sell",
                       qty_text=qty_text, price_text=price_text,
                       readback="venue")
        got = place(engine)
        assert got["filled_qty"] == pytest.approx(0.001)
        assert got["avg_price"] == pytest.approx(MARK)
        assert len(venue.created) == 1, "re-sent on top of an accepted order"


def test_a_partial_fill_in_the_create_response_is_still_accepted():
    venue, _j, engine = build(modern)
    corrupt_create(venue, category="linear", side="Sell", qty_text="0.0004",
                   price_text=str(MARK), readback="gone")
    got = place(engine)                       # no read-back needed or made
    assert got["filled_qty"] == pytest.approx(0.0004)


def test_a_negative_fill_on_the_record_is_not_a_clean_terminal_row():
    venue, _j, engine = build(modern)
    real = venue._request

    def request(method, endpoint, **kw):
        out = real(method, endpoint, **kw)
        if endpoint == "/v5/order/create":
            out["result"] = {"orderLinkId": out["result"]["orderLinkId"]}
        if endpoint == "/v5/order/realtime":
            for row in out["result"]["list"]:
                row["cumExecQty"] = "-1"
        return out

    venue._request = request
    with pytest.raises(OrderOutcomeUnknown):
        place(engine)


class TestTheBookStopsOnAnUnaccountableLeg:
    @pytest.mark.parametrize("qty_text,price_text", BAD)
    def test_a_malformed_perp_leg_after_a_valid_spot_fill_halts_the_book(
            self, qty_text, price_text):
        venue, journal, engine = build(modern)
        corrupt_create(venue, category="linear", side="Sell",
                       qty_text=qty_text, price_text=price_text,
                       readback="gone")
        decision = open_the_hedge(engine)
        assert decision.state is BookState.HALTED
        assert decision.reason == "PERP_LEG_OUTCOME_UNKNOWN"
        assert engine.state is BookState.HALTED
        assert [b["side"] for b in venue.created] == ["Buy", "Sell"], \
            "an order was sent on top of an unknown perp leg"
        assert "perp_open" not in kinds(journal)       # never booked as hedged
        assert engine.position is not None
        assert engine.position.perp.filled_qty == 0    # no phantom hedge

    def test_the_engine_itself_rejects_a_nan_fill_a_stub_broker_returns(self):
        from carry_engine import CarryEngine

        class Stub:
            def place_market(self, **kw):
                return {"filled_qty": float("nan"), "avg_price": MARK,
                        "order_link_id": "x"}

        e = CarryEngine(broker=Stub(), max_notional_usd=100.0, borrow_apr=0.0,
                        execution_mode="acquire", persist=lambda s: None)
        assert e._take("BTCUSDT", "Sell", 0.001, "linear") is None
        assert e.state is BookState.HALTED


class TestAMakerOrderIsHeldToTheSameStandard:
    @staticmethod
    def _engine(placed, final=None):
        from carry_engine import CarryEngine

        class Stub:
            orders = []

            def get_book_top(self, symbol, product):
                return {"bid": MARK - 1, "ask": MARK + 1}

            def place_post_only(self, **kw):
                return placed

            def cancel_order(self, **kw):
                return final

            def place_market(self, **kw):
                Stub.orders.append(kw)
                return {"filled_qty": kw["qty"], "avg_price": MARK,
                        "order_link_id": "m"}

        Stub.orders = []
        e = CarryEngine(broker=Stub(), max_notional_usd=100.0, borrow_apr=0.0,
                        execution_mode="acquire", persist=lambda s: None,
                        maker_wait_s=0.001)
        return e, Stub

    @pytest.mark.parametrize("placed", [
        {"filled_qty": float("nan"), "avg_price": MARK, "order_link_id": "p"},
        {"filled_qty": 5.0, "avg_price": MARK, "order_link_id": "p"},   # > asked
        {"filled_qty": -1.0, "avg_price": MARK, "order_link_id": "p"},
        {"filled_qty": 0.001, "avg_price": 0.0, "order_link_id": "p"},  # unpriced
    ])
    def test_an_unaccountable_maker_fill_halts_and_crosses_nothing(self, placed):
        e, stub = self._engine(dict(placed, resting=False))
        assert e._rest_then_take("BTCUSDT", "Sell", 0.001, "linear", 1e-6) is None
        assert e.state is BookState.HALTED
        assert stub.orders == [], "crossed on top of an unknown maker order"

    def test_an_unaccountable_cancel_total_halts(self):
        e, stub = self._engine(
            {"filled_qty": 0.0, "avg_price": 0.0, "order_link_id": "p",
             "resting": True},
            final={"filled_qty": float("nan"), "resting": False})
        assert e._rest("BTCUSDT", "Sell", 0.001, "linear") is None
        assert e.state is BookState.HALTED
