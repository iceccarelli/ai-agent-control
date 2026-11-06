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


# -- independent data-integrity audit (reproduced): money readers that accepted the wrong row, the wrong unit or the wrong interval ----------------

FEE = {"symbol": SYM, "makerFeeRate": "0.0002", "takerFeeRate": "0.00055"}


def test_a_good_fee_row_reads_in_basis_points_and_a_row_that_names_no_symbol_is_still_accepted():
    r = broker(**{"/v5/account/fee-rate": [FEE]}).get_fee_rates(SYM, "linear")
    assert r["taker_bps"] == pytest.approx(5.5) and r["maker_bps"] == pytest.approx(2.0)
    assert broker(**{"/v5/account/fee-rate": [without(FEE, "symbol")]}).get_fee_rates(SYM, "linear")["taker_bps"] == pytest.approx(5.5)


def test_a_fee_row_for_another_symbol_is_refused_and_not_read_as_free():
    # Reproduced: [{symbol: ETHUSDT, maker 0, taker 0}] for BTCUSDT returned 0 bps and was cached for the life of the process, so the cost gate
    # priced the round trip at nothing.
    wrong = dict(FEE, symbol="ETHUSDT", makerFeeRate="0", takerFeeRate="0")
    with pytest.raises(PairIncident, match="ETHUSDT"):
        broker(**{"/v5/account/fee-rate": [wrong]}).get_fee_rates(SYM, "linear")


@pytest.mark.parametrize("field,value", [("takerFeeRate", "0.055"), ("makerFeeRate", "0.055"), ("takerFeeRate", "1"), ("takerFeeRate", "0.0101")])
def test_a_fee_in_percent_instead_of_a_fraction_is_refused_not_read_as_hundreds_of_bps(field, value):
    # Reproduced: takerFeeRate "0.055" (percent) read as 550 bps without complaint.
    with pytest.raises(PairIncident, match="implausible"):
        broker(**{"/v5/account/fee-rate": [dict(FEE, **{field: value})]}).get_fee_rates(SYM, "linear")


T_SETTLE = 1_700_000_000_000


def settlement(**over):
    row = {"symbol": SYM, "type": "SETTLEMENT", "category": "linear", "id": "s1", "transactionTime": str(T_SETTLE), "funding": "-1.2",
           "change": "-1.2", "size": "-0.1", "currency": "USDT"}
    row.update(over)
    return row


def test_a_settlement_row_without_a_funding_field_makes_the_evidence_incomplete_instead_of_vanishing():
    # Reproduced: {type SETTLEMENT, change "-1.2", no funding} was skipped with no error and no rejection, so complete=True and payments=[]: a charged
    # settlement became "no settlement".
    good = broker(**{"/v5/account/transaction-log": [settlement()]}).get_funding_evidence(SYM, T_SETTLE - 1000, T_SETTLE + 1000)
    assert good.complete and len(good.payments) == 1 and good.payments[0]["usd"] == pytest.approx(-1.2)
    for missing in (without(settlement(), "funding"), settlement(funding=""), settlement(funding=None)):
        ev = broker(**{"/v5/account/transaction-log": [missing]}).get_funding_evidence(SYM, T_SETTLE - 1000, T_SETTLE + 1000)
        assert not ev.complete and any("funding" in e for e in ev.errors), ev.errors
        assert ev.payments == []
    zero = broker(**{"/v5/account/transaction-log": [settlement(funding="0", change="0")]}).get_funding_evidence(SYM, T_SETTLE - 1000, T_SETTLE + 1000)
    assert zero.complete and zero.payments[0]["usd"] == 0.0                    # a stated zero is a legitimate zero


def test_a_funding_reader_reads_the_interval_the_ticker_states_and_refuses_anything_but_eight_hours():
    # Reproduced: fundingIntervalHour was ignored, so a 4h/1h instrument was priced as 8h (income understated 2x-8x, staleness check loosened).
    row = dict(TICKER, fundingRate="0.0001")
    assert broker(**{"/v5/market/tickers": [row]}).get_funding_bps(SYM) == pytest.approx(1.0)
    assert broker(**{"/v5/market/tickers": [dict(row, fundingIntervalHour="8")]}).get_funding_bps(SYM) == pytest.approx(1.0)
    for interval in ("4", "1", "2", "0", "", "abc"):
        with pytest.raises(PairIncident, match="interval"):
            broker(**{"/v5/market/tickers": [dict(row, fundingIntervalHour=interval)]}).get_funding_bps(SYM)


@pytest.mark.parametrize("raw", ["0.4", "-0.4", "1", "0.0501", "", "abc"])
def test_an_implausible_or_unparseable_funding_rate_is_a_pair_incident_not_thousands_of_bps_or_a_bare_valueerror(raw):
    # Reproduced: "1" returned 10000 bps, "-0.4" returned -4000 bps, "" raised a bare ValueError.
    with pytest.raises(PairIncident):
        broker(**{"/v5/market/tickers": [dict(TICKER, fundingRate=raw)]}).get_funding_bps(SYM)


def test_a_funding_rate_beyond_the_venues_own_cap_in_the_same_row_is_refused():
    row = dict(TICKER, fundingCap="0.00333")
    assert broker(**{"/v5/market/tickers": [dict(row, fundingRate="0.00333")]}).get_funding_bps(SYM) == pytest.approx(33.3)
    with pytest.raises(PairIncident, match="cap"):
        broker(**{"/v5/market/tickers": [dict(row, fundingRate="0.01")]}).get_funding_bps(SYM)
    with pytest.raises(PairIncident):
        broker(**{"/v5/market/tickers": [dict(row, fundingRate="0.0001", fundingCap="abc")]}).get_funding_bps(SYM)


@pytest.mark.parametrize("bid,ask", [("100", "99"), ("100", "100")])
def test_a_crossed_or_locked_touch_is_refused_not_returned_with_a_negative_spread(bid, ask):
    with pytest.raises(PairIncident, match="crossed|locked"):
        broker(**{"/v5/market/tickers": [dict(TICKER, bid1Price=bid, ask1Price=ask)]}).get_book_top(SYM, "linear")
    assert broker(**{"/v5/market/tickers": [dict(TICKER, bid1Price="99", ask1Price="100")]}).get_book_top(SYM, "linear") == {"bid": 99.0, "ask": 100.0}
