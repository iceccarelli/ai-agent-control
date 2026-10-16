"""A number the venue did not state is unreadable - never zero, never safe.

Four reads in `CarryBroker` turned a missing or unusable field into the
permissive answer:

* `get_liquidation_view`: a NON-EMPTY `liqPrice` that is nan / 0 / negative, or
  a position whose mark is missing, came back as `distance_pct=None`, which the
  engine reads as "no reachable liquidation price - the safest state there is"
  and skips the gate. Only `""` is Bybit's spelling of that.
* `_order_state`: a terminal order row with no `cumExecQty` read as "filled
  nothing", i.e. a REJECTION, so the engine crossed on top of an order that
  may have filled.
* `get_spot_inventory`: a wallet row with no `walletBalance` read as 0 BTC, and
  an overlay with a correct hedge unwinds it as "inventory withdrawn".
* `_holding` (pair reconciliation): same shape for both legs.

Real `CarryBroker` over Bybit-shaped payloads, real `CarryEngine`; offline.
"""
from __future__ import annotations

import itertools
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from carry_broker import CarryBroker, OrderOutcomeUnknown, PairIncident  # noqa: E402
from carry_engine import BookState  # noqa: E402
from test_carry_broker import FakeClient, envelope  # noqa: E402
from test_carry_venue_direction_integration import (  # noqa: E402
    broker_with_rows, held_engine, tick)

POS = {"side": "Sell", "size": "0.001", "positionIdx": 0,
       "markPrice": "100000", "positionIM": "20", "positionMM": "5"}


def without(row, key):
    return {k: v for k, v in row.items() if k != key}


# -- 1. the liquidation view ----------------------------------------------------

UNUSABLE = [
    dict(POS, liqPrice="nan"),
    dict(POS, liqPrice="inf"),
    dict(POS, liqPrice="0"),
    dict(POS, liqPrice="-5"),
    dict(POS, liqPrice="abc"),
    dict(POS, liqPrice="101000", markPrice="0"),
    dict(without(POS, "markPrice"), liqPrice="101000"),
]
IDS = ["nan", "inf", "zero", "negative", "text", "mark-zero", "mark-missing"]


@pytest.mark.parametrize("row", UNUSABLE, ids=IDS)
def test_an_unusable_liquidation_price_is_not_a_safe_one(row):
    with pytest.raises(PairIncident):
        broker_with_rows(row).get_liquidation_view("BTCUSDT")


@pytest.mark.parametrize("row", UNUSABLE, ids=IDS)
def test_the_engine_halts_instead_of_skipping_the_gate(row):
    engine = held_engine(broker_with_rows(row))
    engine.require_liquidation_check = True       # as main.build_bot sets it
    engine.snapshot = None
    d = engine._check_liquidation_distance()
    assert d is not None and d.state is BookState.HALTED
    assert d.reason == "LIQUIDATION_PRICE_UNREADABLE"


def test_the_venues_own_unreachable_spelling_still_passes():
    v = broker_with_rows(dict(POS, liqPrice="")).get_liquidation_view("BTCUSDT")
    assert v["reason"] == "BEYOND_VENUE_PRICE_BOUNDS" and v["distance_pct"] is None
    engine = held_engine(broker_with_rows(dict(POS, liqPrice="")))
    engine.require_liquidation_check = True
    assert engine._check_liquidation_distance() is None


def test_a_real_liquidation_price_still_measures_distance():
    v = broker_with_rows(dict(POS, liqPrice="101000")).get_liquidation_view(
        "BTCUSDT")
    assert v["distance_pct"] == pytest.approx(1.0)


def test_an_engine_double_that_reports_an_unusable_price_halts_too():
    """Defence in depth: any broker answering with the unusable reason gets
    the same refusal, whatever produced it."""
    class Unusable:
        def get_liquidation_view(self, symbol):
            return {"distance_pct": None, "account_mm_rate": None,
                    "reason": "LIQ_PRICE_UNUSABLE"}
    engine = held_engine(broker_with_rows(dict(POS, liqPrice="")))
    engine.broker = Unusable()
    engine.require_liquidation_check = True
    d = engine._check_liquidation_distance()
    assert d.state is BookState.HALTED
    assert d.reason == "LIQUIDATION_DISTANCE_UNUSABLE"


# -- 2. the order read-back -----------------------------------------------------

class OrderRow(FakeClient):
    def __init__(self, row):
        super().__init__()
        self.row = row

    def _request(self, method, endpoint, *, params=None, body=None,
                 signed=False, retries=3):
        if endpoint == "/v5/order/create":
            self.created.append(body)
            return envelope({"orderId": "1", "orderLinkId": body["orderLinkId"]})
        if endpoint in ("/v5/order/realtime", "/v5/order/history"):
            return envelope({"list": [dict(self.row,
                                           orderLinkId=params["orderLinkId"])]})
        return super()._request(method, endpoint, params=params, body=body,
                                signed=signed, retries=retries)


def order_broker(row):
    seq = itertools.count(1)
    return CarryBroker(client=OrderRow(row), order_gate=lambda: (True, "T"),
                       sequence_source=lambda *a: next(seq))


NO_FILL_FIELD = [
    {"orderStatus": "Filled", "avgPrice": "100000"},
    {"orderStatus": "Filled", "cumExecQty": "", "avgPrice": "100000"},
    {"orderStatus": "Filled", "cumExecQty": None, "avgPrice": "100000"},
    {"orderStatus": "Cancelled", "cumExecQty": "nan"},
]


@pytest.mark.parametrize("row", NO_FILL_FIELD,
                         ids=["absent", "blank", "null", "nan"])
def test_a_terminal_order_that_does_not_state_its_fill_is_not_a_rejection(row):
    b = order_broker(row)
    with pytest.raises(OrderOutcomeUnknown):
        b.place_market(symbol="BTCUSDT", side="Sell", qty=0.001,
                       product="linear")
    with pytest.raises(PairIncident):
        order_broker(row).place_post_only(
            symbol="BTCUSDT", side="Sell", qty=0.001, price=100000.0,
            product="linear")
    with pytest.raises(PairIncident):
        order_broker(row).cancel_order(symbol="BTCUSDT",
                                       order_link_id="x", product="linear")


def test_an_order_that_states_zero_is_still_a_definite_zero():
    r = order_broker({"orderStatus": "Cancelled", "cumExecQty": "0",
                      "avgPrice": "0"}).place_market(
        symbol="BTCUSDT", side="Sell", qty=0.001, product="linear")
    assert r is None


# -- 3. wallet and pair-reconciliation reads ----------------------------------------

class Wallet(FakeClient):
    def __init__(self, coin_rows):
        super().__init__()
        self.coin_rows = coin_rows

    def _request(self, method, endpoint, *, params=None, body=None,
                 signed=False, retries=3):
        if endpoint == "/v5/account/wallet-balance":
            return envelope({"list": [{"coin": self.coin_rows}]})
        return super()._request(method, endpoint, params=params, body=body,
                                signed=signed, retries=retries)


def wallet_broker(coin_rows):
    return CarryBroker(client=Wallet(coin_rows), order_gate=lambda: (True, "T"),
                       sequence_source=lambda *a: 1)


@pytest.mark.parametrize("entry", [
    {"coin": "BTC"},
    {"coin": "BTC", "walletBalance": ""},
    {"coin": "BTC", "walletBalance": None},
], ids=["absent", "blank", "null"])
def test_a_wallet_row_without_a_stated_balance_is_not_zero_btc(entry):
    with pytest.raises(PairIncident):
        wallet_broker([entry]).get_spot_inventory("BTCUSDT")
    with pytest.raises(PairIncident):
        wallet_broker([entry])._holding("BTCUSDT", "spot")


def test_a_stated_zero_balance_is_still_zero():
    b = wallet_broker([{"coin": "BTC", "walletBalance": "0", "locked": "0"}])
    assert b.get_spot_inventory("BTCUSDT") == 0.0
