"""A maker order whose cancel did not take must not be crossed on top of.

`CarryEngine._rest` rests a post-only order, waits, cancels it and reads the
order back. It then used only the read-back's `filled_qty` and crossed the
remainder - even when the read-back said the order was STILL RESTING
(`New` / `PartiallyFilled`): a cancel the venue rejected, rate-limited or had
not applied yet. The resting order can fill after that, so the venue could end
up holding the maker fill AND the market fill: in overlay a short larger than
the inventory it hedges, in acquire a long nobody paired. The per-tick
venue-vs-book check would only notice afterwards, and it does not unwind.

Real `CarryBroker` over a Bybit-shaped fake client (create returns only
orderId; fills come from /v5/order/realtime), real `CarryEngine` in
maker-first. Offline; nothing here can reach a venue.
"""
from __future__ import annotations

import itertools
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from carry_broker import CarryBroker  # noqa: E402
from carry_engine import MAKER_FIRST, OVERLAY, BookState, CarryEngine  # noqa: E402
from carry_risk import CarryRisk  # noqa: E402
from market_snapshot import MarketSnapshot  # noqa: E402
from test_carry_broker import FakeClient, envelope  # noqa: E402

MARK = 100_000.0
H8 = 8 * 3600 * 1000
PARTIAL = 0.4


class StickyVenue(FakeClient):
    """The post-only order fills 40% and rests. What the cancel does is the
    parameter: it may raise, or answer OK; either way the read-back afterwards
    says what `after_cancel` says."""

    def __init__(self, *, cancel_raises=False, after_cancel="PartiallyFilled"):
        super().__init__(mark=MARK, perp_qty=0.0, spot_qty=5.0)
        self.cancel_raises = cancel_raises
        self.after_cancel = after_cancel
        self.cancelled = set()

    def _request(self, method, endpoint, *, params=None, body=None,
                 signed=False, retries=3):
        self.calls.append((method, endpoint, params, body))
        if endpoint == "/v5/market/tickers":
            return envelope({"list": [{
                "markPrice": str(MARK), "lastPrice": str(MARK),
                "fundingRate": "0.0003",
                "bid1Price": "99999.5", "ask1Price": "100000.5"}]})
        if endpoint == "/v5/order/create":
            self.created.append(body)
            # the real create response: an id, no fill fields
            return envelope({"orderId": str(len(self.created)),
                             "orderLinkId": body["orderLinkId"]})
        if endpoint == "/v5/account/wallet-balance":
            return envelope({"list": [{"coin": [
                {"coin": "BTC", "walletBalance": "5", "locked": "0"}]}]})
        if endpoint == "/v5/order/cancel":
            self.cancelled.add(body["orderLinkId"])
            if self.cancel_raises:
                raise RuntimeError("retCode 10006: too many visits")
            return envelope({"orderId": "1", "orderLinkId": body["orderLinkId"]})
        if endpoint == "/v5/order/realtime":
            link = params["orderLinkId"]
            order = next(b for b in self.created if b["orderLinkId"] == link)
            if order.get("timeInForce") == "PostOnly":
                status = (self.after_cancel if link in self.cancelled
                          else "PartiallyFilled")
                return envelope({"list": [{
                    "orderStatus": status, "orderLinkId": link,
                    "cumExecQty": f"{float(order['qty']) * PARTIAL:.8f}",
                    "avgPrice": order["price"], "cumExecFee": "0"}]})
            return envelope({"list": [{
                "orderStatus": "Filled", "orderLinkId": link,
                "cumExecQty": order["qty"], "avgPrice": str(MARK),
                "cumExecFee": "0"}]})
        return super()._request(method, endpoint, params=params, body=body,
                                signed=signed, retries=retries)


def drive(venue):
    seq = itertools.count(1)
    broker = CarryBroker(client=venue, order_gate=lambda: (True, "TEST"),
                         sequence_source=lambda *a: next(seq))
    kills = []
    e = CarryEngine(broker=broker, max_notional_usd=100.0, borrow_apr=0.0,
                    execution_mode=OVERLAY, execution_style=MAKER_FIRST,
                    maker_wait_s=0.001, persist=lambda s: None,
                    kill_switch=kills.append)
    e.pair_risk = CarryRisk(max_notional_usd=100.0)
    e.snapshot = MarketSnapshot(perp_mark=MARK, spot_mark=MARK,
                                funding_bps=3.0, margin_multiple=5.0,
                                observed_at_s=time.time())
    for k in (1, 2):
        e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                    funding_print_ms=k * H8)
    d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                    funding_print_ms=3 * H8, timestamp_ms=3 * H8)
    return e, d, kills


def market_orders(venue):
    return [b for b in venue.created if b["orderType"] == "Market"]


@pytest.mark.parametrize("cancel_raises", [False, True],
                         ids=["cancel-answered-ok", "cancel-raised"])
def test_a_still_resting_maker_order_is_never_crossed_on_top_of(cancel_raises):
    v = StickyVenue(cancel_raises=cancel_raises)
    e, d, kills = drive(v)
    assert v.cancelled, "the engine never tried to cancel"
    assert not market_orders(v), (
        "crossed the remainder while the maker order still rests at the "
        "venue: two legs where one was intended")
    assert e.state is BookState.HALTED
    assert any("MAKER_ORDER_STILL_RESTING" in k for k in kills), kills
    assert e.position is None


def test_a_cancel_that_took_still_crosses_the_remainder_once():
    """The control: the read-back says Cancelled, so the remainder is crossed
    exactly once and the pair opens. The fix must not refuse this."""
    v = StickyVenue(after_cancel="Cancelled")
    e, d, kills = drive(v)
    assert d.action == "opened", d.reason
    takers = market_orders(v)
    assert len(takers) == 1
    assert float(takers[0]["qty"]) == pytest.approx(0.001 * (1 - PARTIAL))
    assert e.position.perp.filled_qty == pytest.approx(0.001)
    assert not kills


def test_a_maker_order_that_filled_before_the_cancel_is_one_whole_leg():
    """The cancel arrived late: the read-back says Filled with the FULL
    cumulative quantity. That total replaces the placement's partial figure
    (it is not added to it) and nothing is crossed."""
    class FilledLate(StickyVenue):
        def _request(self, method, endpoint, *, params=None, body=None,
                     signed=False, retries=3):
            if endpoint == "/v5/order/realtime":
                link = params["orderLinkId"]
                order = next(b for b in self.created
                             if b["orderLinkId"] == link)
                if link in self.cancelled:
                    return envelope({"list": [{
                        "orderStatus": "Filled", "orderLinkId": link,
                        "cumExecQty": order["qty"],
                        "avgPrice": order["price"], "cumExecFee": "0"}]})
            return super()._request(method, endpoint, params=params,
                                    body=body, signed=signed, retries=retries)

    v = FilledLate()
    e, d, kills = drive(v)
    assert d.action == "opened", d.reason
    assert not market_orders(v)
    assert e.position.perp.filled_qty == pytest.approx(0.001)
    assert e.position.perp.maker_qty == pytest.approx(0.001)
    assert e.position.perp.taker_qty == 0.0
    assert not kills


def test_an_order_that_vanishes_on_the_post_cancel_read_back_halts():
    """Ambiguous: the cancel was sent and the venue then returns no row for
    the id. What it filled is unknown, so nothing may be crossed on top."""
    class Vanishes(StickyVenue):
        def _request(self, method, endpoint, *, params=None, body=None,
                     signed=False, retries=3):
            if endpoint == "/v5/order/realtime" and \
                    params["orderLinkId"] in self.cancelled:
                self.calls.append((method, endpoint, params, body))
                return envelope({"list": []})
            return super()._request(method, endpoint, params=params,
                                    body=body, signed=signed, retries=retries)

    v = Vanishes()
    e, d, kills = drive(v)
    assert v.cancelled
    assert not market_orders(v)
    assert e.state is BookState.HALTED
    assert any("MAKER_ORDER_UNCANCELLABLE" in k for k in kills), kills


def test_an_unfilled_maker_order_cancelled_cleanly_is_then_taken():
    """The ordinary maker-first outcome: the post-only rests, nothing fills,
    the cancel takes, the read-back says Cancelled with cumExecQty 0. That is
    a definite answer (filled nothing, no longer live), so the engine crosses
    the whole quantity once. It must not read as 'could not be read back'
    and halt the book."""
    class UnfilledThenCancelled(StickyVenue):
        def _request(self, method, endpoint, *, params=None, body=None,
                     signed=False, retries=3):
            if endpoint == "/v5/order/realtime":
                link = params["orderLinkId"]
                order = next(b for b in self.created
                             if b["orderLinkId"] == link)
                if order.get("timeInForce") == "PostOnly":
                    self.calls.append((method, endpoint, params, body))
                    status = "Cancelled" if link in self.cancelled else "New"
                    return envelope({"list": [{
                        "orderStatus": status, "orderLinkId": link,
                        "cumExecQty": "0", "avgPrice": "0",
                        "cumExecFee": "0"}]})
            return super()._request(method, endpoint, params=params,
                                    body=body, signed=signed, retries=retries)

    v = UnfilledThenCancelled()
    e, d, kills = drive(v)
    assert not kills, kills
    assert d.action == "opened", d.reason
    takers = market_orders(v)
    assert len(takers) == 1 and float(takers[0]["qty"]) == pytest.approx(0.001)
    assert e.position.perp.maker_qty == 0.0
    assert e.position.perp.taker_qty == pytest.approx(0.001)
