"""Every broker read the book decides on refuses a field the venue did not state.

Companion to `test_carry_unstated_numbers` and `test_liquidation_view_absent_
fields`: the same defect class (`float(row.get(key, 0) or 0)` turning a truncated,
renamed or null field into 0) survived in the margin, mark, spot mark, lot-rule
and inventory reads. A 0 there is not neutral: a zero IM reads as a 0x margin
multiple, a zero minNotionalValue removes the venue's minimum order value, a zero
`locked` overstates the coin the book may hedge, and a mark row for another
symbol prices the wrong instrument. Each must be unreadable instead.

Real `CarryBroker` over Bybit-shaped envelopes served by a stub client; offline.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from carry_broker import CarryBroker, PairIncident  # noqa: E402

SYM = "BTCUSDT"


class Venue:
    """Serves one fixed envelope per endpoint; records nothing else."""

    def __init__(self, **by_endpoint):
        self.by_endpoint = by_endpoint

    def _request(self, method, endpoint, **kw):
        return {"retCode": 0, "retMsg": "OK",
                "result": {"list": self.by_endpoint[endpoint]}}


def broker(**by_endpoint) -> CarryBroker:
    return CarryBroker(client=Venue(**by_endpoint), sequence_source=lambda *a: 1,
                       order_gate=lambda: (False, "TEST"))


def without(row, key):
    return {k: v for k, v in row.items() if k != key}


POSITION = {"symbol": SYM, "side": "Sell", "size": "0.01", "positionIdx": 0,
            "positionIM": "20", "positionMM": "5", "markPrice": "100000"}
TICKER = {"symbol": SYM, "markPrice": "100000", "lastPrice": "100000"}
LOT_LINEAR = {"qtyStep": "0.001", "minOrderQty": "0.001",
              "minNotionalValue": "5"}
WALLET = {"coin": [{"coin": "BTC", "walletBalance": "1.0", "locked": "0.1"}]}

BAD = [None, "", "  ", "nan", "inf", "-inf", "abc"]


def test_the_good_rows_read():
    b = broker(**{"/v5/position/list": [POSITION],
                  "/v5/market/tickers": [TICKER],
                  "/v5/market/instruments-info": [{"lotSizeFilter": LOT_LINEAR}],
                  "/v5/account/wallet-balance": [WALLET]})
    assert b.get_margin_multiple(SYM) == pytest.approx(4.0)
    assert b.get_mark(SYM) == 100000.0
    assert b.get_spot_mark(SYM) == 100000.0
    assert b.get_lot_rules(SYM, "linear")["min_notional"] == 5.0
    assert b.get_spot_inventory(SYM) == pytest.approx(0.9)


@pytest.mark.parametrize("bad", BAD)
@pytest.mark.parametrize("key", ["positionIM", "positionMM"])
def test_margin_needs_both_numbers_stated(key, bad):
    row = dict(POSITION)
    row[key] = bad
    with pytest.raises(PairIncident):
        broker(**{"/v5/position/list": [row]}).get_margin_multiple(SYM)


@pytest.mark.parametrize("key", ["positionIM", "positionMM"])
def test_margin_key_absent_is_unreadable(key):
    with pytest.raises(PairIncident):
        broker(**{"/v5/position/list": [without(POSITION, key)]}
               ).get_margin_multiple(SYM)


def test_a_negative_initial_margin_is_unreadable():
    with pytest.raises(PairIncident):
        broker(**{"/v5/position/list": [dict(POSITION, positionIM="-1")]}
               ).get_margin_multiple(SYM)


@pytest.mark.parametrize("bad", BAD + ["0", "-5"])
def test_mark_and_spot_mark_need_a_stated_positive_price(bad):
    for read, key in (("get_mark", "markPrice"), ("get_spot_mark", "lastPrice")):
        row = dict(TICKER)
        row[key] = bad
        with pytest.raises(PairIncident):
            getattr(broker(**{"/v5/market/tickers": [row]}), read)(SYM)


@pytest.mark.parametrize("read,key", [("get_mark", "markPrice"),
                                      ("get_spot_mark", "lastPrice")])
def test_a_ticker_for_another_symbol_is_refused(read, key):
    row = dict(TICKER, symbol="ETHUSDT")
    with pytest.raises(PairIncident):
        getattr(broker(**{"/v5/market/tickers": [row]}), read)(SYM)


@pytest.mark.parametrize("bad", [None, "", "nan", "inf", "-1", "abc"])
def test_the_linear_minimum_order_value_must_be_stated(bad):
    lot = dict(LOT_LINEAR, minNotionalValue=bad)
    with pytest.raises(PairIncident):
        broker(**{"/v5/market/instruments-info": [{"lotSizeFilter": lot}]}
               ).get_lot_rules(SYM, "linear")


def test_the_linear_minimum_order_value_key_absent_is_unreadable():
    lot = without(LOT_LINEAR, "minNotionalValue")
    with pytest.raises(PairIncident):
        broker(**{"/v5/market/instruments-info": [{"lotSizeFilter": lot}]}
               ).get_lot_rules(SYM, "linear")


@pytest.mark.parametrize("bad", [None, "", "nan", "inf", "-0.1", "abc"])
def test_locked_coin_must_be_stated_and_not_negative(bad):
    wallet = {"coin": [{"coin": "BTC", "walletBalance": "1.0", "locked": bad}]}
    with pytest.raises(PairIncident):
        broker(**{"/v5/account/wallet-balance": [wallet]}
               ).get_spot_inventory(SYM)


def test_locked_key_absent_is_unreadable():
    wallet = {"coin": [{"coin": "BTC", "walletBalance": "1.0"}]}
    with pytest.raises(PairIncident):
        broker(**{"/v5/account/wallet-balance": [wallet]}
               ).get_spot_inventory(SYM)


# -- review follow-ups: the neighbouring readers share the same rules ---------

@pytest.mark.parametrize("read,args,endpoint,row", [
    ("get_funding_bps", (SYM,), "/v5/market/tickers",
     dict(TICKER, symbol="ETHUSDT", fundingRate="0.0001")),
    ("get_book_top", (SYM, "linear"), "/v5/market/tickers",
     dict(TICKER, symbol="ETHUSDT", bid1Price="1", ask1Price="2")),
])
def test_every_ticker_reader_refuses_another_symbols_row(read, args, endpoint,
                                                         row):
    with pytest.raises(PairIncident):
        getattr(broker(**{endpoint: [row]}), read)(*args)


def test_a_flat_position_row_with_a_garbage_mark_hands_on_no_nan():
    import math
    flat = dict(POSITION, size="0", markPrice="nan", positionMM="0")
    b = broker(**{"/v5/position/list": [flat]})
    original = b.client._request

    def request(method, endpoint, **kw):
        if endpoint == "/v5/account/info":
            return {"retCode": 0, "result": {"marginMode": "ISOLATED_MARGIN"}}
        return original(method, endpoint, **kw)
    b.client._request = request
    view = b.get_liquidation_view(SYM)
    assert view["reason"] == "NO_POSITION" and math.isfinite(view["mark"])


def test_a_spot_holding_is_read_from_the_named_coin_not_the_first_row():
    wallet = {"coin": [{"coin": "ETH", "walletBalance": "9", "locked": "0"},
                       {"coin": "BTC", "walletBalance": "1", "locked": "0"}]}
    b = broker(**{"/v5/account/wallet-balance": [wallet]})
    assert b._holding(SYM, "spot") == 1.0


@pytest.mark.parametrize("coins", [
    [{"coin": "ETH", "walletBalance": "9", "locked": "0"}],
    [{"coin": "BTC", "walletBalance": "1"}, {"coin": "BTC", "walletBalance": "2"}],
])
def test_a_spot_holding_with_no_or_ambiguous_coin_row_is_unreadable(coins):
    with pytest.raises(PairIncident):
        broker(**{"/v5/account/wallet-balance": [{"coin": coins}]}
               )._holding(SYM, "spot")
