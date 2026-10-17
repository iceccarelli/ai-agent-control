"""A missing or unrecognised orderStatus is not proof that an order is dead.

`CarryBroker._order_state` classified an order as resting only when its
status was one of a fixed list; ANY other value - including a missing field
(a truncated payload) or a status this code has never seen - read as
terminal. With nothing filled that meant "rejected, cross instead", so the
engine sent a market order for the FULL quantity on top of a post-only order
of unknown state, without ever cancelling it; with a partial fill it crossed
the remainder the same way. The market-order read-back treated the same row
as "did not fill", so the engine could re-send next tick.

Now only a status the venue documents as terminal
(`bybit_connection.TERMINAL_ORDER_STATUS`) is terminal; everything else is an
order that may still fill. Real `CarryBroker` over Bybit-shaped fakes, real
`CarryEngine`. Offline; nothing here can reach a venue.
"""
from __future__ import annotations

import itertools
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bybit_connection  # noqa: E402
from carry_broker import CarryBroker, OrderOutcomeUnknown  # noqa: E402
from carry_engine import BookState  # noqa: E402
from test_carry_broker import FakeClient, envelope  # noqa: E402
from test_carry_maker_cancel_still_resting import (  # noqa: E402
    StickyVenue, drive, market_orders)


class PostOnlyRow(StickyVenue):
    """The post-only order's read-back is `row`, before and after any cancel."""

    def __init__(self, row):
        super().__init__()
        self.row = row

    def _request(self, method, endpoint, *, params=None, body=None,
                 signed=False, retries=3):
        if endpoint == "/v5/order/realtime":
            link = params["orderLinkId"]
            order = next(b for b in self.created if b["orderLinkId"] == link)
            if order.get("timeInForce") == "PostOnly":
                self.calls.append((method, endpoint, params, body))
                return envelope({"list": [dict(self.row, orderLinkId=link)]})
        return super()._request(method, endpoint, params=params, body=body,
                                signed=signed, retries=retries)


@pytest.mark.parametrize("row", [
    {"cumExecQty": "0", "avgPrice": "0"},                       # no status at all
    {"orderStatus": "", "cumExecQty": "0", "avgPrice": "0"},
    {"orderStatus": "Weird", "cumExecQty": "0", "avgPrice": "0"},
    {"orderStatus": "Weird", "cumExecQty": "0.0004", "avgPrice": "100000.5"},
], ids=["missing", "empty", "unknown-0", "unknown-partial"])
def test_an_unknown_status_is_never_crossed_on_top_of(row):
    v = PostOnlyRow(row)
    e, d, kills = drive(v)
    assert not market_orders(v), "crossed on top of an order of unknown state"
    assert v.cancelled, "an order of unknown state was never even cancelled"
    assert e.state is BookState.HALTED
    assert any("MAKER_ORDER_STILL_RESTING" in k for k in kills), kills


@pytest.mark.parametrize("status", sorted(bybit_connection.TERMINAL_ORDER_STATUS))
def test_a_documented_terminal_status_with_nothing_filled_is_a_rejection(status):
    """Legitimate post-only handling is unchanged: a confirmed terminal order
    that filled nothing never rested, so the engine crosses once and opens."""
    v = PostOnlyRow({"orderStatus": status, "cumExecQty": "0", "avgPrice": "0"})
    e, d, kills = drive(v)
    assert d.action == "opened", d.reason
    assert not v.cancelled
    takers = market_orders(v)
    assert len(takers) == 1 and float(takers[0]["qty"]) == pytest.approx(0.001)
    assert not kills


class MarketStatusless(FakeClient):
    """A market order the venue accepted (orderId only); its read-back row has
    no orderStatus and nothing filled."""

    def _request(self, method, endpoint, *, params=None, body=None,
                 signed=False, retries=3):
        if endpoint == "/v5/order/create":
            self.created.append(body)
            return envelope({"orderId": "1", "orderLinkId": body["orderLinkId"]})
        if endpoint == "/v5/order/realtime":
            return envelope({"list": [{"cumExecQty": "0", "avgPrice": "0",
                                       "orderLinkId": params["orderLinkId"]}]})
        return super()._request(method, endpoint, params=params, body=body,
                                signed=signed, retries=retries)


def test_a_market_order_with_an_unreadable_status_is_an_unknown_outcome():
    seq = itertools.count(1)
    b = CarryBroker(client=MarketStatusless(), order_gate=lambda: (True, "T"),
                    sequence_source=lambda *a: next(seq))
    with pytest.raises(OrderOutcomeUnknown):
        b.place_market(symbol="BTCUSDT", side="Sell", qty=0.001,
                       product="linear")


@pytest.mark.parametrize("status,resting", [
    *[(s, False) for s in sorted(bybit_connection.TERMINAL_ORDER_STATUS)],
    *[(s, True) for s in sorted(bybit_connection.OPEN_ORDER_STATUS)],
    ("Created", True), ("", True), ("Weird", True), (None, True),
])
def test_the_classifier_treats_only_documented_terminal_statuses_as_final(
        status, resting):
    class One(FakeClient):
        def _request(self, method, endpoint, *, params=None, body=None,
                     signed=False, retries=3):
            row = {"cumExecQty": "0.5", "avgPrice": "1", "orderLinkId": "x"}
            if status is not None:
                row["orderStatus"] = status
            return envelope({"list": [row]})
    state = CarryBroker(client=One(), order_gate=lambda: (True, "T"),
                        sequence_source=lambda *a: 1)._order_state(
        "BTCUSDT", "x", "linear")
    assert state["resting"] is resting


# -- the duplicate-orderLinkId path --------------------------------------------
#
# A duplicate-id rejection means the FIRST attempt exists at the venue. It was
# resolved by a reader that took cumExecQty and ignored the status: a first attempt still live with nothing filled came back as "did not
# fill", and the engine was free to send a new one on the next tick.

class Duplicate(FakeClient):
    """create raises a duplicate-id retCode; the read-back row is `row`."""

    def __init__(self, row):
        super().__init__(raise_on_create=110072)
        self.row = row

    def _request(self, method, endpoint, *, params=None, body=None,
                 signed=False, retries=3):
        if endpoint == "/v5/order/realtime":
            self.calls.append((method, endpoint, params, body))
            return envelope({"list": [dict(self.row,
                                           orderLinkId=params["orderLinkId"])]})
        return super()._request(method, endpoint, params=params, body=body,
                                signed=signed, retries=retries)


def _dup(row):
    return CarryBroker(client=Duplicate(row), order_gate=lambda: (True, "T"),
                       sequence_source=lambda *a: 1)


@pytest.mark.parametrize("row", [
    {"orderStatus": "New", "cumExecQty": "0", "avgPrice": "0"},
    {"orderStatus": "PartiallyFilled", "cumExecQty": "0.2", "avgPrice": "1"},
    {"cumExecQty": "0", "avgPrice": "0"},                        # no status
], ids=["live-unfilled", "live-partial", "statusless"])
def test_a_duplicate_whose_first_attempt_is_still_live_is_unknown_not_dead(row):
    with pytest.raises(OrderOutcomeUnknown):
        _dup(row).place_market(symbol="BTCUSDT", side="Buy", qty=0.5,
                               product="spot")


def test_a_duplicate_whose_first_attempt_filled_is_that_fill():
    r = _dup({"orderStatus": "Filled", "cumExecQty": "0.5",
              "avgPrice": "100000"}).place_market(
        symbol="BTCUSDT", side="Buy", qty=0.5, product="spot")
    assert r is not None and r["filled_qty"] == pytest.approx(0.5)


def test_a_duplicate_whose_first_attempt_died_unfilled_is_no_fill():
    r = _dup({"orderStatus": "Cancelled", "cumExecQty": "0",
              "avgPrice": "0"}).place_market(
        symbol="BTCUSDT", side="Buy", qty=0.5, product="spot")
    assert r is None


# -- an accepted market order whose read-back has no row ----------------------
#
# The create response carried an orderId, so the order EXISTS. A read-back
# that then finds no row is not "did not fill": the engine would refuse the
# leg and could send another next tick, while the first may be live.

class AcceptedThenInvisible(FakeClient):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def _request(self, method, endpoint, *, params=None, body=None,
                 signed=False, retries=3):
        if endpoint == "/v5/order/create":
            self.created.append(body)
            return envelope({"orderId": "1", "orderLinkId": body["orderLinkId"]})
        if endpoint == "/v5/order/realtime":
            return envelope({"list": [dict(r, orderLinkId=params["orderLinkId"])
                                      for r in self.rows]})
        return super()._request(method, endpoint, params=params, body=body,
                                signed=signed, retries=retries)


def _accepted(rows):
    seq = itertools.count(1)
    return CarryBroker(client=AcceptedThenInvisible(rows),
                       order_gate=lambda: (True, "T"),
                       sequence_source=lambda *a: next(seq))


def test_an_accepted_market_order_with_no_row_is_an_unknown_outcome():
    with pytest.raises(OrderOutcomeUnknown):
        _accepted([]).place_market(symbol="BTCUSDT", side="Sell", qty=0.001,
                                   product="linear")


def test_an_accepted_market_order_that_died_unfilled_is_no_fill():
    r = _accepted([{"orderStatus": "Cancelled", "cumExecQty": "0",
                    "avgPrice": "0"}]).place_market(
        symbol="BTCUSDT", side="Sell", qty=0.001, product="linear")
    assert r is None


def test_an_accepted_market_order_that_filled_is_that_fill():
    r = _accepted([{"orderStatus": "Filled", "cumExecQty": "0.001",
                    "avgPrice": "100000"}]).place_market(
        symbol="BTCUSDT", side="Sell", qty=0.001, product="linear")
    assert r["filled_qty"] == pytest.approx(0.001)


# -- an accepted market order still live with part of it filled ---------------

class LivePartialReadback(FakeClient):
    """Create succeeds with no fill in the response; the read-back says `row`."""

    def __init__(self, row):
        super().__init__()
        self.row = row

    def _request(self, method, endpoint, *, params=None, body=None,
                 signed=False, retries=3):
        if endpoint == "/v5/order/create":
            return envelope({"orderId": "1", "orderLinkId": body["orderLinkId"]})
        if endpoint == "/v5/order/realtime":
            return envelope({"list": [dict(self.row,
                                           orderLinkId=params["orderLinkId"])]})
        return super()._request(method, endpoint, params=params, body=body,
                                signed=signed, retries=retries)


def _live_partial_broker(row):
    return CarryBroker(client=LivePartialReadback(row),
                       order_gate=lambda: (True, "T"),
                       sequence_source=lambda *a: 1)


def test_an_accepted_market_order_still_live_with_a_partial_fill_is_unknown():
    with pytest.raises(OrderOutcomeUnknown):
        _live_partial_broker({"orderStatus": "PartiallyFilled", "cumExecQty": "0.2",
                   "avgPrice": "100000"}).place_market(
            symbol="BTCUSDT", side="Sell", qty=1.0, product="linear")


def test_an_accepted_market_order_that_ended_partial_is_that_partial_fill():
    r = _live_partial_broker({"orderStatus": "PartiallyFilledCanceled",
                   "cumExecQty": "0.2", "avgPrice": "100000"}).place_market(
        symbol="BTCUSDT", side="Sell", qty=1.0, product="linear")
    assert r is not None and r["filled_qty"] == pytest.approx(0.2)


# -- a venue timeout is not a refusal -----------------------------------------

class TimeoutRefusal(FakeClient):
    def __init__(self, exc_type, code):
        super().__init__()
        self.exc_type, self.code = exc_type, code

    def _request(self, method, endpoint, *, params=None, body=None,
                 signed=False, retries=3):
        if endpoint == "/v5/order/create":
            raise self.exc_type("venue said no", self.code)
        if endpoint in ("/v5/order/realtime", "/v5/order/history"):
            return envelope({"list": []})      # the venue shows no row yet
        return super()._request(method, endpoint, params=params, body=body,
                                signed=signed, retries=retries)


@pytest.mark.parametrize("code", [10000, 170007])
def test_a_venue_timeout_retcode_is_an_unknown_outcome_not_a_rejection(code):
    b = CarryBroker(client=TimeoutRefusal(bybit_connection.PermanentAPIError, code),
                    order_gate=lambda: (True, "T"), sequence_source=lambda *a: 1)
    with pytest.raises(OrderOutcomeUnknown):
        b.place_market(symbol="BTCUSDT", side="Buy", qty=0.5, product="spot")


def test_an_ordinary_venue_refusal_is_still_a_rejection():
    b = CarryBroker(client=TimeoutRefusal(bybit_connection.PermanentAPIError, 110007),
                    order_gate=lambda: (True, "T"), sequence_source=lambda *a: 1)
    assert b.place_market(symbol="BTCUSDT", side="Buy", qty=0.5,
                          product="spot") is None


# -- a 200 with no retCode is not a refusal ----------------------------------

@pytest.mark.parametrize("body", ['{"message": "upstream"}', '[]', '{"retCode": null}',
                                  '{"retCode": "x"}'])
def test_a_200_without_a_usable_retcode_is_read_back_not_recorded_as_a_refusal(
        body, tmp_path, monkeypatch):
    """The order executed and the venue shows it filled. A body with no
    retCode used to become retCode -1, a definite rejection: place_market
    returned None without ever reading the order back, and the engine could
    send it again."""
    import json
    import time

    from fake_bybit import API_KEY, API_SECRET
    from persistence import StateStore
    monkeypatch.setattr(time, "sleep", lambda s: None)

    class Cfg:
        USE_TESTNET = True
        BYBIT_API_KEY = API_KEY
        BYBIT_API_SECRET = API_SECRET
        BYBIT_RECV_WINDOW_MS = 5000
        REQUEST_TIMEOUT_SECONDS = 5.0
        USE_LEVERAGE = False
        SYMBOL_FILTERS_TTL = 3600
        ORDERLINK_PREFIX = "BB"

    class Transport(bybit_connection.Transport):
        def __init__(self):
            self.creates = 0

        def request(self, method, url, **kw):
            if "order/create" in url:
                self.creates += 1
                return 200, body
            return 200, json.dumps({"retCode": 0, "result": {"list": [{
                "orderStatus": "Filled", "cumExecQty": "0.01",
                "avgPrice": "100000", "orderLinkId": "x"}]}})

    store = StateStore(str(tmp_path / "s.db"))
    store.claim_writer()
    transport = Transport()
    client = bybit_connection.BybitClient(config=Cfg(), store=store,
                                          transport=transport)
    broker = CarryBroker(client=client,
                         sequence_source=lambda *a: store.next_order_seq(),
                         order_gate=lambda: (True, ""))
    result = broker.place_market(symbol="BTCUSDT", side="Sell", qty=0.01,
                                 product="linear")
    assert result is not None and result["filled_qty"] == pytest.approx(0.01)
    assert transport.creates == 1, "the order was re-sent"
