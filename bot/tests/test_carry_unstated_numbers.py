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
            return {"distance_pct": None, "account_mm_rate": 0.05,
                    "margin_mode": "REGULAR_MARGIN",
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


# -- 3. the account margin rate, by margin mode ---------------------------------
#
# `get_liquidation_view` used to catch every wallet error and answer
# `account_mm_rate=None`, which the engine read as "skip the account check".
# Under account-level margin modes the rate is mandatory; isolated margin is
# per position and keeps the mandatory liquidation-distance gate instead.

ACCOUNT_LEVEL = ["REGULAR_MARGIN", "PORTFOLIO_MARGIN"]
BAD_RATES = [None, "", "nan", "inf", "-inf", "-0.1", "abc", True]
BAD_RATE_IDS = ["null", "blank", "nan", "inf", "-inf", "negative", "text",
                "bool"]
SAFE = dict(POS, liqPrice="130000")


def moded(row, mode="REGULAR_MARGIN", rate="0.05"):
    b = broker_with_rows(row)
    b._client.margin_mode = mode
    b._client.account_mm_rate = rate
    return b


@pytest.mark.parametrize("mode", ACCOUNT_LEVEL)
@pytest.mark.parametrize("rate", BAD_RATES, ids=BAD_RATE_IDS)
def test_an_unusable_account_rate_is_not_a_skipped_check(mode, rate):
    with pytest.raises(PairIncident):
        moded(SAFE, mode, rate).get_liquidation_view("BTCUSDT")


@pytest.mark.parametrize("mode", ACCOUNT_LEVEL)
def test_an_unreadable_wallet_is_not_a_skipped_check(mode):
    b = moded(SAFE, mode)
    inner = b._client._request

    def request(method, endpoint, **kw):
        if endpoint == "/v5/account/wallet-balance":
            raise RuntimeError("wallet-balance timed out")
        return inner(method, endpoint, **kw)

    b._client._request = request
    with pytest.raises(RuntimeError):
        b.get_liquidation_view("BTCUSDT")


@pytest.mark.parametrize("mode", ACCOUNT_LEVEL)
def test_a_wallet_with_no_rows_is_not_a_skipped_check(mode):
    b = moded(SAFE, mode)
    inner = b._client._request

    def request(method, endpoint, **kw):
        if endpoint == "/v5/account/wallet-balance":
            return envelope({"list": []})
        return inner(method, endpoint, **kw)

    b._client._request = request
    with pytest.raises(PairIncident):
        b.get_liquidation_view("BTCUSDT")


@pytest.mark.parametrize("mode", [None, "", "CROSS", "isolated_margin", 1])
def test_a_margin_mode_the_venue_did_not_state_is_refused(mode):
    with pytest.raises(PairIncident):
        moded(SAFE, mode).get_liquidation_view("BTCUSDT")


@pytest.mark.parametrize("mode", ACCOUNT_LEVEL)
def test_account_level_modes_carry_their_stated_mode_and_rate(mode):
    v = moded(SAFE, mode, "0.12").get_liquidation_view("BTCUSDT")
    assert v["margin_mode"] == mode and v["account_mm_rate"] == 0.12


def test_isolated_margin_does_not_borrow_an_account_level_rate():
    b = moded(SAFE, "ISOLATED_MARGIN", rate=None)
    v = b.get_liquidation_view("BTCUSDT")
    assert v["margin_mode"] == "ISOLATED_MARGIN"
    assert v["account_mm_rate"] is None
    assert v["distance_pct"] == pytest.approx(30.0)
    assert not [c for c in b._client.calls
                if c[1] == "/v5/account/wallet-balance"]


def _engine(row, mode="REGULAR_MARGIN", rate="0.05"):
    engine = held_engine(moded(row, mode, rate))
    engine.require_liquidation_check = True
    engine.kill_switch = (tripped := []).append
    return engine, tripped


@pytest.mark.parametrize("mode", ACCOUNT_LEVEL)
@pytest.mark.parametrize("rate", BAD_RATES, ids=BAD_RATE_IDS)
def test_the_held_book_halts_on_an_unusable_account_rate(mode, rate):
    engine, tripped = _engine(SAFE, mode, rate)
    d = tick(engine)
    assert d.action == "halted" and d.reason == "LIQUIDATION_PRICE_UNREADABLE"
    assert engine.state is BookState.HALTED and tripped
    # and it stays frozen: no later tick manages or opens anything
    assert tick(engine).reason == "KILL_SWITCH_ENGAGED"


@pytest.mark.parametrize("mode", ACCOUNT_LEVEL)
def test_a_valid_low_account_rate_still_holds(mode):
    engine, tripped = _engine(SAFE, mode, "0.05")
    assert engine._check_liquidation_distance() is None and not tripped


@pytest.mark.parametrize("mode", ACCOUNT_LEVEL)
def test_a_high_account_rate_still_unwinds(mode):
    engine, _ = _engine(SAFE, mode, "0.85")
    d = engine._check_liquidation_distance()
    assert d.action == "unwound" and "ACCOUNT_MM_RATE" in d.reason


def test_isolated_margin_keeps_the_per_position_distance_gate():
    engine, _ = _engine(dict(POS, liqPrice="101000"), "ISOLATED_MARGIN", None)
    d = engine._check_liquidation_distance()
    assert d.action == "unwound" and "LIQUIDATION_1.0PCT" in d.reason
    engine, tripped = _engine(SAFE, "ISOLATED_MARGIN", None)
    assert engine._check_liquidation_distance() is None and not tripped


def test_isolated_margin_without_a_liquidation_price_halts():
    engine, tripped = _engine(dict(POS, liqPrice=""), "ISOLATED_MARGIN", None)
    d = engine._check_liquidation_distance()
    assert d.state is BookState.HALTED and tripped
    assert d.reason == "LIQUIDATION_DISTANCE_UNUSABLE"


@pytest.mark.parametrize("view", [
    None, [], "x",
    {"distance_pct": 30.0, "account_mm_rate": 0.05},
    {"distance_pct": 30.0, "account_mm_rate": 0.05, "margin_mode": "CROSS"},
    {"distance_pct": 30.0, "account_mm_rate": 0.05, "margin_mode": None},
], ids=["none", "list", "str", "no-mode", "unknown-mode", "null-mode"])
def test_an_engine_double_with_no_stated_mode_halts(view):
    class Double:
        def get_liquidation_view(self, symbol):
            return view
    engine = held_engine(broker_with_rows(SAFE))
    engine.broker = Double()
    engine.require_liquidation_check = True
    d = engine._check_liquidation_distance()
    assert d.state is BookState.HALTED


@pytest.mark.parametrize("rate", [None, float("nan"), float("inf"),
                                  float("-inf"), -0.1, "abc", True])
@pytest.mark.parametrize("mode", ACCOUNT_LEVEL)
def test_an_engine_double_with_an_unusable_rate_halts(mode, rate):
    class Double:
        def get_liquidation_view(self, symbol):
            return {"distance_pct": 30.0, "account_mm_rate": rate,
                    "margin_mode": mode, "reason": ""}
    engine = held_engine(broker_with_rows(SAFE))
    engine.broker = Double()
    engine.require_liquidation_check = True
    d = engine._check_liquidation_distance()
    assert d.state is BookState.HALTED
    assert d.reason == "ACCOUNT_MM_RATE_UNUSABLE"
