"""0040 — join the book instead of crossing it, and fall back when it does not fill.

WHAT THIS IS WORTH, AND WHAT IT IS NOT
======================================
0032/0036 measured the overlay's round trip at 11 bps taker (two perp legs at
5.5). This account's maker rate is 2.0, so the same two legs are 4 bps. On the
settlement clock that is the difference between a median trade that cannot pay
for its own exit and one that can.

The spread is NOT the argument: Bybit's BTCUSDT touch is 0.1 USDT wide, about
0.013 bps. Crossing it is free. The 7 bps is entirely the FEE TIER, which is
why joining the queue is worth a wait and worth the code below.

What this cannot know offline is FILL PROBABILITY — whether a post-only order
at the touch is hit before the price walks away. That is why every path here
ends in a taker fallback, why the cost gate keeps pricing the TAKER round trip
(maker is upside, never an assumption), and why the realized fee of every fill
is recorded rather than modelled.

ONE RULE ABOVE THE FEE
======================
Getting OUT is never made slower to save a fee. Unwinds, emergency unwinds and
margin-driven exits are taker, immediately, always.
"""
from __future__ import annotations

import os
import sys
import time
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from carry_broker import CarryBroker, PairIncident  # noqa: E402
from carry_engine import (MAKER_FIRST, TAKER, BookState,  # noqa: E402
                          CarryEngine, OVERLAY)
from carry_risk import CarryRisk  # noqa: E402
from market_snapshot import MarketSnapshot  # noqa: E402

MARK = 100_000.0
H8 = 8 * 3600 * 1000
BID, ASK = 99_999.9, 100_000.1


class Venue:
    """A venue whose resting orders fill, or do not, on command."""

    def __init__(self, *, maker_fills=True, maker_partial=0.0,
                 post_only_rejected=False, book=None):
        self.maker_fills = maker_fills
        self.maker_partial = maker_partial
        self.post_only_rejected = post_only_rejected
        self.book = book or {"bid": BID, "ask": ASK}
        self.calls = []          # ("maker"|"taker"|"cancel", side, qty, price)

    # -- execution ---------------------------------------------------------
    def place_post_only(self, *, symbol, side, qty, price, product):
        self.calls.append(("maker", side, qty, price))
        if self.post_only_rejected:
            return None
        filled = qty if self.maker_fills else qty * self.maker_partial
        return {"filled_qty": filled, "avg_price": price,
                "order_link_id": f"m{len(self.calls)}",
                "fee": filled * price * 2.0 / 1e4, "maker": True,
                "resting": filled < qty}

    def place_market(self, *, symbol, side, qty, product):
        self.calls.append(("taker", side, qty, None))
        return {"filled_qty": qty, "avg_price": MARK,
                "order_link_id": f"t{len(self.calls)}",
                "fee": qty * MARK * 5.5 / 1e4, "maker": False}

    def cancel_order(self, *, symbol, order_link_id, product):
        self.calls.append(("cancel", None, None, None))
        # The venue reports the order's TOTAL fill, which is what it had
        # already reported at placement: nothing more filled during the wait.
        maker = [c for c in self.calls if c[0] == "maker"][-1]
        return {"filled_qty": maker[2] * self.maker_partial,
                "avg_price": maker[3], "fee": 0.0}

    def get_book_top(self, symbol, product):
        return dict(self.book)

    # -- the rest of the adapter ------------------------------------------
    def get_margin_multiple(self, symbol):
        return 5.0

    def get_mark(self, symbol):
        return MARK

    def get_lot_rules(self, symbol, product):
        return {"qty_step": 1e-6, "min_qty": 1e-6, "min_notional": 0.0}

    def get_fee_rates(self, symbol, product):
        return {"maker_bps": 2.0,
                "taker_bps": 10.0 if product == "spot" else 5.5}

    def get_spot_inventory(self, symbol):
        return 5.0


def engine(venue, *, style=MAKER_FIRST, mode=OVERLAY, wait_s=0.05, **kw):
    e = CarryEngine(broker=venue, max_notional_usd=100.0, borrow_apr=0.0,
                    execution_mode=mode, execution_style=style,
                    maker_wait_s=wait_s, persist=lambda s: None,
                    **kw)
    e.pair_risk = CarryRisk(max_notional_usd=100.0)
    e.snapshot = MarketSnapshot(perp_mark=MARK, spot_mark=MARK,
                                funding_bps=3.0, margin_multiple=5.0,
                                observed_at_s=time.time())
    for k in (1, 2):
        e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                    funding_print_ms=k * H8)
    return e


def opened(venue, **kw):
    e = engine(venue, **kw)
    d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                    funding_print_ms=3 * H8, timestamp_ms=3 * H8)
    return e, d


class TestItJoinsTheBookInsteadOfCrossingIt:
    def test_a_short_rests_at_the_ask(self):
        v = Venue()
        e, d = opened(v)
        assert d.action == "opened", d.reason
        assert v.calls[0][:2] == ("maker", "Sell")
        assert v.calls[0][3] == ASK          # never crosses

    def test_a_buy_rests_at_the_bid(self):
        """A Buy joins the bid. It never lifts the offer, which would be
        paying the spread for the privilege of being a maker."""
        v = Venue()
        e = engine(v)
        leg = e._rest("BTCUSDT", "Buy", 0.001, "linear")
        assert v.calls[0][:2] == ("maker", "Buy")
        assert v.calls[0][3] == BID
        assert leg.maker_qty == pytest.approx(0.001)

    def test_acquire_never_rests_an_entry_leg(self):
        """Maker-first is an OVERLAY lever. In ACQUIRE the trade is a PAIR,
        and every second an entry leg rests is a second one side is naked —
        which is the rule this engine exists to enforce, sold for 7 bps."""
        v = Venue()
        e, d = opened(v, mode="acquire")
        assert d.action == "opened", d.reason
        assert [c[0] for c in v.calls] == ["taker", "taker"]

    def test_no_taker_order_when_the_maker_fills(self):
        v = Venue()
        e, d = opened(v)
        assert [c[0] for c in v.calls] == ["maker"]
        assert e.position.perp.maker_qty == pytest.approx(0.001)
        assert e.position.perp.taker_qty == 0.0

    def test_the_realised_fee_is_the_maker_fee(self):
        v = Venue()
        e, _d = opened(v)
        leg = e.position.perp
        assert leg.fee == pytest.approx(leg.filled_qty * ASK * 2.0 / 1e4)

    def test_taker_style_never_rests(self):
        v = Venue()
        e, d = opened(v, style=TAKER)
        assert [c[0] for c in v.calls] == ["taker"]
        assert e.position.perp.maker_qty == 0.0


class TestWhenTheBookDoesNotComeToUs:
    def test_an_unfilled_maker_is_cancelled_then_taken(self):
        v = Venue(maker_fills=False)
        e, d = opened(v)
        assert [c[0] for c in v.calls] == ["maker", "cancel", "taker"]
        assert d.action == "opened"
        assert e.position.perp.taker_qty == pytest.approx(0.001)

    def test_a_partial_maker_fill_takes_only_the_remainder(self):
        v = Venue(maker_fills=False, maker_partial=0.4)
        e, d = opened(v)
        taker = [c for c in v.calls if c[0] == "taker"][0]
        assert taker[2] == pytest.approx(0.0006)
        assert e.position.perp.maker_qty == pytest.approx(0.0004)
        assert e.position.perp.taker_qty == pytest.approx(0.0006)
        assert e.position.perp.filled_qty == pytest.approx(0.001)

    def test_a_rejected_post_only_falls_back_immediately(self):
        v = Venue(post_only_rejected=True)
        e, d = opened(v)
        assert [c[0] for c in v.calls] == ["maker", "taker"]
        assert d.action == "opened"

    def test_an_unreadable_book_falls_back_rather_than_guessing_a_price(self):
        v = Venue()
        v.get_book_top = lambda s, p: (_ for _ in ()).throw(
            PairIncident("no ticker"))
        e, d = opened(v)
        assert [c[0] for c in v.calls] == ["taker"]
        assert d.action == "opened"

    def test_an_order_whose_fate_is_unknown_halts_rather_than_doubling(self):
        """place_post_only raising means the order MIGHT be resting. Crossing
        on top of it would open a second leg, so the book stops."""
        def blind(**kw):
            raise PairIncident("cannot tell whether the order rests")
        v = Venue()
        v.place_post_only = blind
        e, d = opened(v)
        assert d.action == "halted", d.reason
        assert e.state is BookState.HALTED
        assert not [c for c in v.calls if c[0] == "taker"]

    def test_a_crossed_book_is_refused_rather_than_quoted_into(self):
        v = Venue(book={"bid": 100_001.0, "ask": 99_999.0})
        e, d = opened(v)
        assert [c[0] for c in v.calls] == ["taker"]


class TestGettingOutIsNeverSlowedForAFee:
    def test_an_unwind_is_taker(self):
        v = Venue()
        e, _d = opened(v)
        v.calls.clear()
        for k in (4, 5, 6):
            d = e.on_candle(mark=MARK, funding_bps=-0.5, spot=MARK,
                            funding_print_ms=k * H8)
        assert d.action == "unwound"
        assert [c[0] for c in v.calls] == ["taker"]

    def test_a_margin_exit_is_taker(self):
        v = Venue()
        e, _d = opened(v)
        v.get_margin_multiple = lambda s: 1.1
        v.calls.clear()
        d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                        funding_print_ms=7 * H8)
        assert d.action == "unwound"
        assert all(c[0] == "taker" for c in v.calls)

    def test_the_emergency_spot_unwind_is_taker(self):
        class NoPerp(Venue):
            def place_post_only(self, **kw):
                self.calls.append(("maker", kw["side"], kw["qty"], kw["price"]))
                return None

            def place_market(self, *, symbol, side, qty, product):
                self.calls.append(("taker", side, qty, None))
                if product == "linear":
                    return None
                return {"filled_qty": qty, "avg_price": MARK,
                        "order_link_id": "x", "fee": 0.1}
        v = NoPerp()
        e, d = opened(v, mode="acquire")
        assert "PERP_LEG_DID_NOT_FILL" in d.reason
        sells = [c for c in v.calls if c[1] == "Sell" and c[0] == "taker"]
        assert sells, "the naked spot was not sold with a taker order"

    def test_only_the_two_entry_paths_may_be_patient(self):
        """The invariant, read off the source rather than trusted.

        `patient=True` is what lets an order rest. If it ever appears in a
        method that REMOVES an exposure, some exit became cancellable for the
        sake of 7 bps. This walks the engine and names the methods it is
        allowed to appear in.
        """
        import ast
        import inspect
        import carry_engine

        tree = ast.parse(inspect.getsource(carry_engine))
        patient_in = set()
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                for kw in node.keywords:
                    if kw.arg == "patient" and \
                            getattr(kw.value, "value", False) is True:
                        patient_in.add(func.name)
        assert patient_in == {"_open_overlay"}, patient_in


class TestTheGateStillPricesTheTaker:
    def test_the_round_trip_stays_conservative(self):
        v = Venue()
        e = engine(v)
        assert e.round_trip_bps() == pytest.approx(11.0)   # 2 x taker 5.5

    def test_maker_is_upside_not_an_assumption(self):
        """If the gate priced 4 bps and the fill came back taker, the gate
        would have admitted trades that cannot pay for themselves."""
        import inspect
        assert "taker" in inspect.getsource(CarryEngine.round_trip_bps).lower()


class TestTheBrokerSpeaksTheVenuesLanguage:
    class Client:
        def __init__(self, ticker=None, create=None, raise_on_create=None):
            self.ticker = ticker or {"list": [{"bid1Price": "99999.9",
                                               "ask1Price": "100000.1",
                                               "markPrice": "100000.0",
                                               "lastPrice": "100000.0"}]}
            self.create = create or {"orderLinkId": "x"}
            self.raise_on_create = raise_on_create
            self.bodies = []

        def _request(self, method, endpoint, *, params=None, body=None,
                     signed=False, retries=3):
            if endpoint == "/v5/market/tickers":
                return self.ticker
            if endpoint == "/v5/order/create":
                self.bodies.append(body)
                if self.raise_on_create:
                    raise self.raise_on_create
                return self.create
            if endpoint == "/v5/order/realtime":
                return {"list": [{"cumExecQty": "0.001", "avgPrice": "100000.1",
                                  "cumExecFee": "0.02", "orderStatus": "Filled",
                                  "orderLinkId": "x"}]}
            if endpoint == "/v5/order/cancel":
                return {"orderLinkId": "x"}
            return {}

    def broker(self, client):
        return CarryBroker(client=client, sequence_source=lambda *a: 1,
                           order_gate=lambda: (True, "TEST"))

    def test_the_touch_comes_from_the_ticker(self):
        c = self.Client()
        assert self.broker(c).get_book_top("BTCUSDT", "linear") == {
            "bid": 99999.9, "ask": 100000.1}

    def test_a_missing_touch_raises(self):
        c = self.Client(ticker={"list": [{"markPrice": "1"}]})
        with pytest.raises(PairIncident):
            self.broker(c).get_book_top("BTCUSDT", "linear")

    def test_a_post_only_order_says_so(self):
        c = self.Client()
        self.broker(c).place_post_only(symbol="BTCUSDT", side="Sell",
                                       qty=0.001, price=100000.1,
                                       product="linear")
        body = c.bodies[0]
        assert body["orderType"] == "Limit"
        assert body["timeInForce"] == "PostOnly"
        assert body["price"] == "100000.1"
        assert body["qty"] == "0.001"

    def test_a_rejected_post_only_is_not_an_incident(self):
        """Post-only rejection means the price moved, not that the book is
        broken: the caller takes the spread instead.

        No error CODE is interpreted here. The venue is asked the only
        question that matters — does this order exist? — and a rejected order
        does not.
        """
        from bybit_connection import BybitAPIError

        class Rejected(self.Client):
            def _request(self, method, endpoint, **kw):
                if endpoint == "/v5/order/realtime":
                    return {"list": []}
                return super()._request(method, endpoint, **kw)
        c = Rejected(raise_on_create=BybitAPIError("post only", 30208))
        assert self.broker(c).place_post_only(
            symbol="BTCUSDT", side="Sell", qty=0.001, price=1.0,
            product="linear") is None

    def test_a_lost_response_that_left_an_order_resting_is_reported(self):
        """The create timed out and the order IS there. Returning None would
        make the caller cross on top of a live resting leg."""

        class Resting(self.Client):
            def _request(self, method, endpoint, **kw):
                if endpoint == "/v5/order/realtime":
                    return {"list": [{"cumExecQty": "0", "avgPrice": "0",
                                      "orderStatus": "New",
                                      "orderLinkId": "x"}]}
                return super()._request(method, endpoint, **kw)
        c = Resting(raise_on_create=RuntimeError("read timeout"))
        state = self.broker(c).place_post_only(
            symbol="BTCUSDT", side="Sell", qty=0.001, price=100000.1,
            product="linear")
        assert state["resting"] is True
        assert state["filled_qty"] == 0.0

    def test_an_order_that_cannot_be_read_back_is_an_incident(self):
        class Blind(self.Client):
            def _request(self, method, endpoint, **kw):
                if endpoint == "/v5/order/realtime":
                    raise RuntimeError("venue unreachable")
                return super()._request(method, endpoint, **kw)
        with pytest.raises(Exception):
            self.broker(Blind()).place_post_only(
                symbol="BTCUSDT", side="Sell", qty=0.001, price=100000.1,
                product="linear")

    def test_a_cancel_that_arrives_late_reports_the_fill(self):
        class Late(self.Client):
            def _request(self, method, endpoint, **kw):
                if endpoint == "/v5/order/cancel":
                    raise RuntimeError("too late to cancel")
                return super()._request(method, endpoint, **kw)
        c = Late()
        result = self.broker(c).cancel_order(symbol="BTCUSDT",
                                             order_link_id="x",
                                             product="linear")
        assert result["filled_qty"] == pytest.approx(0.001)

    def test_the_fee_comes_back_with_the_fill(self):
        c = self.Client()
        fill = self.broker(c).place_post_only(symbol="BTCUSDT", side="Sell",
                                              qty=0.001, price=100000.1,
                                              product="linear")
        assert fill["fee"] == pytest.approx(0.02)
        assert fill["maker"] is True


class TestTheStyleIsConfigured:
    def test_the_default_is_the_conservative_one(self):
        import config as _config
        cfg = _config.load({"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
                            "CARRY_EXECUTION_MODE": "overlay"})
        assert cfg.CARRY_EXECUTION_STYLE == TAKER

    def test_maker_first_reaches_the_engine(self):
        import config as _config
        import main as _main
        cfg = _config.load({"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
                            "CARRY_EXECUTION_MODE": "overlay",
                            "CARRY_EXECUTION_STYLE": "maker_first"})
        bot = _main.build_bot(config=cfg, store=_Store(), client=mock.Mock(),
                              risk_manager=mock.Mock(), engine=mock.Mock())
        assert bot.carry.execution_style == MAKER_FIRST

    def test_an_unknown_style_is_refused_at_load(self):
        import config as _config
        with pytest.raises(Exception):
            _config.load({"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
                          "CARRY_EXECUTION_MODE": "overlay",
                          "CARRY_EXECUTION_STYLE": "iceberg"})

    def test_the_wait_is_bounded(self):
        import config as _config
        for bad in ("0", "601", "-5"):
            with pytest.raises(Exception):
                _config.load({"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
                              "CARRY_EXECUTION_MODE": "overlay",
                              "CARRY_MAKER_WAIT_S": bad})


class _Store:
    def __init__(self):
        self.seq = 0

    def next_order_seq(self):
        self.seq += 1
        return self.seq

    def trip_kill_switch(self, reason):
        pass

    def is_kill_switch_engaged(self):
        return (False, "")

    def save_carry_position(self, state):
        pass

    def load_carry_position(self):
        return None

    def open_positions(self):
        return []
