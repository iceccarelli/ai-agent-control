"""A create response's PARTIAL fill is a claim; the lifecycle log must not
treat it as final. And a retry of a sent id that cannot be read is UNKNOWN.

`_place_market_core` says a create response's fill "is a claim to check, not a
fact". A claim of a fill SMALLER than the order passes through as the leg (an
existing test pins that), but `_OrderTrail.finish` then wrote CANCEL_CONFIRM
for it - filing a possibly still-live order as cancelled, which removes it from
the unresolved set that blocks the next cycle and that a restart reads against
the venue. Bybit's real create response carries only an order id, so this is
the defensive half of the rule.

The retry path (`_OrderTrail._resume`, an id already recorded as sent) let any
error from the venue read escape as a bare exception. The engine only halts on
`outcome_unknown`; a bare error is "the leg did not fill", and the leg is sent
again.
"""
from __future__ import annotations

import itertools
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import order_lifecycle as ol                                  # noqa: E402
from bybit_connection import TransientAPIError                # noqa: E402
from carry_broker import CarryBroker, OrderOutcomeUnknown, PairIncident  # noqa: E402
from persistence import StateStore                            # noqa: E402
from test_carry_broker import FakeClient, envelope            # noqa: E402


class ClaimingVenue(FakeClient):
    """create answers with a fill claim; the order row is what it really is."""

    def __init__(self, *, claim, row_status, row_filled):
        super().__init__(perp_qty=0.0, spot_qty=5.0)
        self.claim, self.row_status, self.row_filled = claim, row_status, row_filled
        self.orders = {}
        self.reads = 0

    def _request(self, method, endpoint, *, params=None, body=None,
                 signed=False, retries=3):
        if endpoint == "/v5/order/create":
            self.created.append(body)
            link = body["orderLinkId"]
            self.orders[link] = {
                "orderLinkId": link, "orderStatus": self.row_status,
                "cumExecQty": str(self.row_filled), "avgPrice": "100000",
                "cumFeeDetail": {"USDT": "0.011"}}
            result = {"orderId": "1", "orderLinkId": link}
            if self.claim is not None:
                result.update(cumExecQty=str(self.claim), avgPrice="100000")
            return envelope(result)
        if endpoint in ("/v5/order/realtime", "/v5/order/history"):
            self.reads += 1
            row = self.orders.get(params["orderLinkId"])
            return envelope({"list": [row] if row else []})
        return super()._request(method, endpoint, params=params, body=body,
                                signed=signed, retries=retries)


def broker(venue, **kw):
    seq = itertools.count(1)
    return CarryBroker(client=venue, order_gate=lambda: (True, "T"),
                       sequence_source=lambda *a: next(seq), **kw)


def sell(b):
    return b.place_market(symbol="BTCUSDT", side="Sell", qty=1.0,
                          product="linear")


def test_a_partial_claim_is_still_the_leg_the_engine_gets():
    """Pinned elsewhere (test_carry_malformed_fill): a partial fill in the create
    response is sane and is not read back. Unchanged."""
    venue = ClaimingVenue(claim=0.4, row_status="PartiallyFilled",
                          row_filled=0.4)
    got = sell(broker(venue))
    assert got["filled_qty"] == pytest.approx(0.4)
    assert venue.reads == 0 and len(venue.created) == 1


def test_but_a_claimed_partial_is_not_written_down_as_a_cancelled_order(tmp_path):
    """Nothing established that the rest of the order will not fill, so the
    durable lifecycle must keep it unresolved (blocks the next cycle, read
    against the venue at restart) instead of calling it cancelled."""
    store = StateStore(str(tmp_path / "s.db"))
    venue = ClaimingVenue(claim=0.4, row_status="PartiallyFilled",
                          row_filled=0.4)
    sell(broker(venue, order_log=store))
    rows = store.unresolved_carry_orders()
    assert len(rows) == 1, "the order was filed as final on a claim"
    assert rows[0]["status"] == ol.PARTIAL
    assert rows[0]["filled_qty"] == pytest.approx(0.4)
    store.close()


def test_a_partial_the_venue_confirms_final_is_still_filed_cancelled(
        tmp_path, monkeypatch):
    monkeypatch.setattr(CarryBroker, "ORDER_READBACK_SLEEP_S", 0.0)
    store = StateStore(str(tmp_path / "s.db"))
    venue = ClaimingVenue(claim=None, row_status="PartiallyFilledCanceled",
                          row_filled=0.6)
    got = sell(broker(venue, order_log=store))
    assert got["filled_qty"] == pytest.approx(0.6)
    assert store.unresolved_carry_orders() == []
    assert store.orders_for_symbol("BTCUSDT")[0]["status"] == ol.CANCELLED
    store.close()


def test_a_claim_of_the_whole_order_is_filled_and_needs_no_second_read(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    venue = ClaimingVenue(claim=1.0, row_status="Filled", row_filled=1.0)
    got = sell(broker(venue, order_log=store))
    assert got["filled_qty"] == pytest.approx(1.0)
    assert venue.reads == 0
    assert store.unresolved_carry_orders() == []
    store.close()


class UnreadableOnRetry(FakeClient):
    """create times out once (so the order is recorded as sent); afterwards
    the order row cannot be parsed."""

    def __init__(self):
        super().__init__(perp_qty=0.0, spot_qty=5.0)
        self.armed = False

    def _request(self, method, endpoint, *, params=None, body=None,
                 signed=False, retries=3):
        if endpoint == "/v5/order/create":
            self.created.append(body)
            raise TransientAPIError("transport error: timed out")
        if endpoint in ("/v5/order/realtime", "/v5/order/history"):
            if not self.armed:
                raise TransientAPIError("transport error: timed out")
            return envelope({"list": [{"orderLinkId": params["orderLinkId"],
                                       "orderStatus": "Filled"}]})   # no cumExecQty
        return super()._request(method, endpoint, params=params, body=body,
                                signed=signed, retries=retries)


@pytest.mark.parametrize("read", ["raises", "unstated"])
def test_a_retry_of_a_sent_order_that_cannot_be_read_is_an_unknown_outcome(
        tmp_path, read):
    """The retry path asks the venue about an id already sent. Whatever goes
    wrong with that read, the engine must be told the outcome is UNKNOWN (it
    halts on that); a bare error is read as 'did not fill' and the leg is
    sent again."""
    store = StateStore(str(tmp_path / "s.db"))
    venue = UnreadableOnRetry()
    b = CarryBroker(client=venue, order_gate=lambda: (True, "T"),
                    sequence_source=lambda *a: 7, order_log=store)
    with pytest.raises(OrderOutcomeUnknown):
        sell(b)                                    # first attempt: unanswered
    sent = len(venue.created)
    venue.armed = read == "unstated"
    with pytest.raises(Exception) as caught:
        sell(b)                                    # the retry of the same id
    assert getattr(caught.value, "outcome_unknown", False) is True, (
        f"{type(caught.value).__name__} would be read as 'did not fill'")
    assert len(venue.created) == sent, "a sent order is never sent again"
    store.close()
