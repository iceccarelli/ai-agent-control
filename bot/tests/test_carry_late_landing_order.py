"""A create that failed WITHOUT a venue answer may still land.

A transport failure on /v5/order/create (timeout, reset, 5xx) means the
request may be queued at the venue and processed after our read-back. Both
order paths asked the venue "does this order exist?", and when the answer was
"no row" they treated the leg as not sent: `place_post_only` returned None
and the engine crossed; `place_market` returned None and the engine refused
the leg, free to send another next tick. If the first request then landed,
the venue held two legs where one was intended.

A venue retCode is a definitive answer and still means "rejected, nothing
exists". No answer plus no row is now an unknown outcome: the book stops.
Real `CarryBroker` and `CarryEngine` over Bybit-shaped fakes; offline.
"""
from __future__ import annotations

import itertools
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bybit_connection import BybitAPIError, TransientAPIError  # noqa: E402
from carry_broker import CarryBroker, OrderOutcomeUnknown, PairIncident  # noqa: E402
from carry_engine import BookState  # noqa: E402
from test_carry_broker import FakeClient, envelope  # noqa: E402
from test_carry_maker_cancel_still_resting import StickyVenue, drive, market_orders  # noqa: E402


class LateLanding(FakeClient):
    """create raises `exc`; the order appears at the venue only AFTER the
    first read-back (`lands` True), or never (`lands` False)."""

    def __init__(self, exc, *, lands=True):
        super().__init__(perp_qty=0.0, spot_qty=5.0)
        self.exc, self.lands = exc, lands
        self.pending = []
        self.live = {}
        self.reads = 0

    def _request(self, method, endpoint, *, params=None, body=None,
                 signed=False, retries=3):
        self.calls.append((method, endpoint, params, body))
        if endpoint == "/v5/order/create":
            self.created.append(body)
            if self.exc is not None:
                exc, self.exc = self.exc, None       # only the first create
                self.pending.append(body)
                raise exc
            self.live[body["orderLinkId"]] = dict(
                body, orderStatus="Filled", cumExecQty=body["qty"],
                avgPrice="100000")
            return envelope({"orderId": "1", "orderLinkId": body["orderLinkId"]})
        if endpoint in ("/v5/order/realtime", "/v5/order/history"):
            self.reads += 1
            link = params["orderLinkId"]
            row = self.live.get(link)
            out = envelope({"list": [row] if row else []})
            if self.reads >= 2 and self.lands:
                for b in self.pending:          # the queued request lands now
                    self.live[b["orderLinkId"]] = dict(
                        b, orderStatus="New", cumExecQty="0", avgPrice="0")
                self.pending = []
            return out
        if endpoint == "/v5/market/tickers":
            return envelope({"list": [{"markPrice": "100000", "lastPrice": "100000",
                                       "fundingRate": "0.0003",
                                       "bid1Price": "99999.5", "ask1Price": "100000.5"}]})
        if endpoint == "/v5/account/wallet-balance":
            return envelope({"list": [{"coin": [
                {"coin": "BTC", "walletBalance": "5", "locked": "0"}]}]})
        return super()._request(method, endpoint, params=params, body=body,
                                signed=signed, retries=retries)


def broker(client):
    seq = itertools.count(1)
    return CarryBroker(client=client, order_gate=lambda: (True, "T"),
                       sequence_source=lambda *a: next(seq))


TIMEOUT = TransientAPIError("transport error: timed out")
REJECTED = BybitAPIError("post only would cross", 30208)


def test_a_post_only_whose_create_got_no_answer_is_not_a_rejection():
    with pytest.raises(PairIncident):
        broker(LateLanding(TIMEOUT)).place_post_only(
            symbol="BTCUSDT", side="Sell", qty=0.001, price=100_000.5,
            product="linear")


def test_a_market_order_whose_create_got_no_answer_is_unknown():
    with pytest.raises(OrderOutcomeUnknown):
        broker(LateLanding(TIMEOUT)).place_market(
            symbol="BTCUSDT", side="Sell", qty=0.001, product="linear")


def test_a_venue_rejection_with_no_row_is_still_just_not_sent():
    """The venue ANSWERED. Nothing exists; the caller may cross / refuse."""
    assert broker(LateLanding(REJECTED, lands=False)).place_post_only(
        symbol="BTCUSDT", side="Sell", qty=0.001, price=100_000.5,
        product="linear") is None
    assert broker(LateLanding(REJECTED, lands=False)).place_market(
        symbol="BTCUSDT", side="Sell", qty=0.001, product="linear") is None


def test_the_engine_does_not_cross_on_top_of_a_request_that_may_still_land():
    class V(StickyVenue):
        def __init__(self):
            super().__init__()
            self.inner = LateLanding(TIMEOUT)

        def _request(self, method, endpoint, *, params=None, body=None,
                     signed=False, retries=3):
            if endpoint in ("/v5/order/create", "/v5/order/realtime",
                            "/v5/order/history"):
                out = self.inner._request(method, endpoint, params=params,
                                          body=body, signed=signed,
                                          retries=retries)
                self.created = self.inner.created
                return out
            return super()._request(method, endpoint, params=params,
                                    body=body, signed=signed, retries=retries)

    v = V()
    e, d, kills = drive(v)
    assert not market_orders(v), "crossed on top of a request that may land"
    assert len(v.created) == 1
    assert e.state is BookState.HALTED
