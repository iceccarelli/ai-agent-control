"""A closed order is read from /v5/order/history; realtime lists open ones.

`CarryBroker._order_state` read only `/v5/order/realtime`. Real Bybit lists
OPEN orders there (New, PartiallyFilled, Untriggered); an order that filled,
was cancelled or was rejected is in `/v5/order/history`. The repository's own
client learned this live (`BybitClient.get_order`: "a filled market order was
always a 404 ... re-placed the same order up to three times") and reads both;
the canonical fake (`tests/fake_bybit.py`) serves closed orders from history
only. The carry broker did not, so on the real venue a filled market order
could not be confirmed by its own read-back, and a cleanly cancelled maker
order's final state could not be read at all.

These run the real `CarryBroker` against that canonical fake through a thin
client shim. Offline; nothing here can reach a venue.
"""
from __future__ import annotations

import itertools
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from bybit_connection import BybitAPIError  # noqa: E402
from carry_broker import CarryBroker, OrderOutcomeUnknown  # noqa: E402
from fake_bybit import FakeBybit  # noqa: E402

BASE = "https://api-testnet.bybit.com"


class Shim:
    """`client._request` as CarryBroker uses it, answered by FakeBybit."""

    def __init__(self, fake):
        self.fake = fake
        fake.verify_signature = False
        self.calls = []

    def _request(self, method, endpoint, *, params=None, body=None,
                 signed=False, retries=3):
        self.calls.append((method, endpoint))
        _status, text = self.fake.request(
            method, BASE + endpoint, headers={}, params=params or {},
            body=json.dumps(body) if body is not None else None)
        payload = json.loads(text)
        if int(payload.get("retCode", -1)) != 0:
            raise BybitAPIError(payload.get("retMsg", ""), payload["retCode"])
        return payload


def broker():
    fake = FakeBybit(balances={"USDT": 100_000.0, "BTC": 5.0})
    shim = Shim(fake)
    seq = itertools.count(1)
    b = CarryBroker(client=shim, order_gate=lambda: (True, "T"),
                    sequence_source=lambda *a: next(seq))
    return b, shim, fake


def test_a_filled_market_order_is_confirmed_from_history():
    b, shim, fake = broker()
    r = b.place_market(symbol="BTCUSDT", side="Buy", qty=0.001, product="spot")
    assert r is not None and r["filled_qty"] == pytest.approx(0.001)
    reads = [e for m, e in shim.calls if e.startswith("/v5/order/")]
    assert reads == ["/v5/order/create", "/v5/order/realtime",
                     "/v5/order/history"]
    assert list(fake.orders.values())[0]["orderStatus"] == "Filled"


def test_a_resting_order_is_found_in_realtime_without_touching_history():
    b, shim, fake = broker()
    state = b.place_post_only(symbol="BTCUSDT", side="Sell", qty=0.001,
                              price=100_000.0, product="linear")
    assert state is not None and state["resting"] is True
    assert "/v5/order/history" not in [e for _m, e in shim.calls]


def test_a_cleanly_cancelled_maker_order_reads_back_as_cancelled():
    b, shim, fake = broker()
    state = b.place_post_only(symbol="BTCUSDT", side="Sell", qty=0.001,
                              price=100_000.0, product="linear")
    link = state["order_link_id"]
    final = b.cancel_order(symbol="BTCUSDT", order_link_id=link,
                           product="linear")
    assert final["resting"] is False
    assert final["status"] == "Cancelled"
    assert final["filled_qty"] == 0.0


def test_an_order_in_neither_list_is_no_row():
    b, shim, fake = broker()
    assert b._order_state("BTCUSDT", "never-sent", "linear") is None


def test_an_accepted_order_in_neither_list_is_still_unknown():
    """The history fallback must not turn 'nowhere to be found' into a fill
    or a rejection: an accepted order the venue cannot show stays unknown."""
    b, shim, fake = broker()
    real = fake.request

    def forget(method, url, **kw):
        status, text = real(method, url, **kw)
        if "/v5/order/create" in url:
            fake.orders.clear()          # accepted, then vanished
        return status, text
    fake.request = forget
    with pytest.raises(OrderOutcomeUnknown):
        b.place_market(symbol="BTCUSDT", side="Buy", qty=0.001, product="spot")
