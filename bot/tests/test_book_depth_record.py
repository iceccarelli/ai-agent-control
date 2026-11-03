"""The public order-book DEPTH read and what an execution record keeps of it.

* `CarryBroker.get_book_depth`: public, asked once, validated whole.
* `execution_record.walk_book` / `parse_ladder`: the executable VWAP of the
  order quantity, never extrapolated past the book shown.
* `assess_fill`: depth is OPTIONAL. A record without it is still a valid
  top-of-book sample; depth never moves the reference price or the slippage.
* `LedgerBroker.begin_decision`: prefers depth, falls back to the ticker touch,
  is bounded in wall-clock time when asked, and never builds a book.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import execution_record as xr                                # noqa: E402
import ledger as L                                           # noqa: E402
from carry_broker import CarryBroker, PairIncident           # noqa: E402
from test_carry_broker import FakeClient, envelope           # noqa: E402

T0 = 1_700_000_000_000
BIDS = [("99990", "0.5"), ("99980", "1.0"), ("99950", "5.0")]
ASKS = [("100010", "0.5"), ("100020", "1.0"), ("100050", "5.0")]


def book(bids=BIDS, asks=ASKS, **extra):
    r = {"s": "BTCUSDT", "b": [list(x) for x in bids],
         "a": [list(x) for x in asks], "ts": T0, "u": 1}
    r.update(extra)
    return envelope(r)


class BookClient(FakeClient):
    def __init__(self, payload=None, raises=None, **kw):
        super().__init__(**kw)
        self.payload, self.raises = payload, raises

    def _request(self, method, endpoint, *, params=None, body=None,
                 signed=False, retries=3):
        if endpoint == "/v5/market/orderbook":
            self.calls.append((method, endpoint, params, body, signed, retries))
            if self.raises:
                raise self.raises
            return self.payload
        return super()._request(method, endpoint, params=params, body=body,
                                signed=signed, retries=retries)


def broker(client):
    return CarryBroker(client=client, sequence_source=lambda *a: 7,
                       order_gate=lambda: (True, "TEST"))


# -- the broker read ------------------------------------------------------------------

def test_the_depth_read_is_public_get_asked_once():
    c = BookClient(book())
    out = broker(c).get_book_depth("BTCUSDT", "linear")
    (method, endpoint, params, _b, signed, retries), = [
        x for x in c.calls if x[1] == "/v5/market/orderbook"]
    assert (method, signed, retries) == ("GET", False, 0)
    assert params == {"category": "linear", "symbol": "BTCUSDT", "limit": 50}
    assert out["bid"] == 99990.0 and out["ask"] == 100010.0
    assert out["bids"][0] == (99990.0, 0.5) and out["asks"][2] == (100050.0, 5.0)
    assert out["venue_ts_ms"] == T0


def test_spot_depth_uses_the_spot_category():
    c = BookClient(book())
    broker(c).get_book_depth("BTCUSDT", "spot")
    assert c.calls[-1][2]["category"] == "spot"


def test_an_unknown_product_is_refused_before_any_request():
    c = BookClient(book())
    with pytest.raises(ValueError):
        broker(c).get_book_depth("BTCUSDT", "option")
    assert not [x for x in c.calls if x[1] == "/v5/market/orderbook"]


@pytest.mark.parametrize("payload", [
    book(bids=[]),                                        # no bids
    book(asks=[]),                                        # no asks
    book(bids=[("99990", "0.5"), ("99995", "1")]),        # bids rising
    book(asks=[("100010", "0.5"), ("100005", "1")]),      # asks falling
    book(bids=[("99990", "0")]),                          # a zero size
    book(bids=[("nan", "1")]),                            # not a price
    book(bids=[("-5", "1")]),                             # negative
    book(bids=[("100020", "1")]),                         # crossed
    book(s="ETHUSDT"),                                    # another symbol
    envelope({}),                                         # empty result
], ids=["nobids", "noasks", "bids_rising", "asks_falling", "zero_size",
        "nan_price", "negative", "crossed", "wrong_symbol", "empty"])
def test_a_broken_book_is_refused_whole(payload):
    with pytest.raises(PairIncident):
        broker(BookClient(payload)).get_book_depth("BTCUSDT", "linear")


def test_a_missing_or_bad_venue_timestamp_is_none_not_the_local_clock():
    for ts in (None, "x", "nan", "inf", -5, 0):
        p = book()
        p["result"]["ts"] = ts
        assert broker(BookClient(p)).get_book_depth(
            "BTCUSDT", "linear")["venue_ts_ms"] is None


def test_a_transport_failure_propagates_so_the_caller_can_carry_on():
    with pytest.raises(RuntimeError):
        broker(BookClient(raises=RuntimeError("down"))).get_book_depth(
            "BTCUSDT", "linear")


# -- the pure walk ----------------------------------------------------------------------

def ladders():
    return (xr.parse_ladder(BIDS, descending=True),
            xr.parse_ladder(ASKS, descending=False))


def test_a_buy_walks_the_asks_and_a_sell_walks_the_bids():
    bids, asks = ladders()
    w = xr.walk_book("Buy", 1.0, bids, asks)
    # 0.5 @ 100010 + 0.5 @ 100020
    assert w["supported"] and w["vwap_price"] == pytest.approx(100015.0)
    assert w["levels_used"] == 2 and w["levels_seen"] == 3
    w = xr.walk_book("Sell", 1.0, bids, asks)
    assert w["vwap_price"] == pytest.approx(99985.0) and w["levels_used"] == 2


def test_a_quantity_inside_the_touch_costs_the_touch():
    bids, asks = ladders()
    w = xr.walk_book("Buy", 0.2, bids, asks)
    assert w["vwap_price"] == 100010.0 and w["levels_used"] == 1


def test_depth_that_cannot_support_the_quantity_is_not_extrapolated():
    bids, asks = ladders()
    w = xr.walk_book("Buy", 10.0, bids, asks)
    assert w["supported"] is False and w["vwap_price"] is None
    assert w["available_qty"] == pytest.approx(6.5) and w["levels_used"] == 3


@pytest.mark.parametrize("qty", [0, -1, None, float("nan"), "x"])
def test_no_walk_for_a_bad_quantity(qty):
    bids, asks = ladders()
    assert xr.walk_book("Buy", qty, bids, asks) is None


def test_no_walk_without_depth_or_for_a_bad_side():
    bids, asks = ladders()
    assert xr.walk_book("Buy", 1.0, bids, None) is None
    assert xr.walk_book("Hold", 1.0, bids, asks) is None


# -- the record -------------------------------------------------------------------------

def ctx(**kw):
    base = dict(venue="bybit", symbol="BTCUSDT", product="linear", side="Buy",
                decision_ms=T0 + 1000, qty=1.0, mark=100_000.0,
                bid=99990.0, ask=100010.0, observed_ms=T0 + 900,
                book_source="orderbook", book_venue_ts_ms=T0 + 850,
                bids=xr.parse_ladder(BIDS, descending=True),
                asks=xr.parse_ladder(ASKS, descending=False))
    base.update(kw)
    return xr.DecisionContext(**base)


def fill(c, **kw):
    args = dict(side="Buy", symbol="BTCUSDT", product="linear", venue="bybit",
                order_type="market", requested_qty=1.0, filled_qty=1.0,
                avg_price=100_016.0, now_ms=T0 + 2000, fee_usd=0.5,
                fee_confirmed=True, order_link_id="L1", submit_ms=T0 + 1050,
                fill_ms=T0 + 1150, environment="testnet")
    args.update(kw)
    return xr.assess_fill(c, **args)


def test_a_record_with_depth_states_vwap_mid_spread_and_levels():
    r = fill(ctx())
    assert r["status"] == xr.MEASURED and r["reference_basis"] == "executable_side"
    assert r["mid_price"] == 100_000.0
    assert r["spread_abs"] == pytest.approx(20.0)
    assert r["spread_bps"] == pytest.approx(2.0)
    d = r["depth"]
    assert d["status"] == "MEASURED" and d["basis"] == "requested_qty"
    assert d["vwap_price"] == pytest.approx(100015.0)
    assert (d["levels_used"], d["levels_seen"]) == (2, 3)
    assert d["supported"] is True and d["requested_qty"] == 1.0
    # provenance
    assert r["book_source"] == "orderbook" and r["environment"] == "testnet"
    assert r["decision_ms"] == T0 + 1000 and r["observed_ms"] == T0 + 900
    assert r["book_age_ms"] == 100 and r["book_venue_ts_ms"] == T0 + 850
    assert r["order_link_id"] == "L1"
    assert r["notional_usd"] == pytest.approx(1.0 * 100_016.0)


def test_depth_never_moves_the_reference_or_the_slippage():
    with_depth = fill(ctx())
    without = fill(ctx(bids=None, asks=None, book_source="ticker_touch"))
    for k in ("reference_price", "reference_basis", "slippage_usd",
              "slippage_bps", "spread_cost_usd", "shortfall_vs_mid_usd",
              "actual_cost_usd", "status"):
        assert with_depth[k] == without[k], k
    # the reference is the TOUCH (ask), not the depth-walked VWAP
    assert with_depth["reference_price"] == 100010.0


def test_a_record_without_depth_is_still_a_valid_top_of_book_sample():
    r = fill(ctx(bids=None, asks=None))
    assert r["status"] == xr.MEASURED and r["reference_basis"] == "executable_side"
    assert r["depth"] is None
    assert r["mid_price"] == 100_000.0 and r["spread_bps"] == pytest.approx(2.0)


def test_insufficient_depth_is_stated_not_extrapolated():
    r = fill(ctx(qty=10.0), requested_qty=10.0, filled_qty=10.0)
    d = r["depth"]
    assert d["status"] == "DEPTH_INSUFFICIENT" and d["vwap_price"] is None
    assert d["supported"] is False
    assert r["status"] == xr.MEASURED       # the sample itself is unaffected


def test_depth_disagreeing_with_the_touch_is_dropped_not_blended():
    r = fill(ctx(bid=99991.0))              # touch 99991, ladder says 99990
    assert r["depth"] == {"status": "DEPTH_TOUCH_MISMATCH"}
    assert r["reference_price"] == 100010.0


def test_a_broken_ladder_in_the_context_is_invalid_depth_only():
    r = fill(ctx(bids=((99990.0, 1.0), (99995.0, 1.0))))
    assert r["depth"] == {"status": "DEPTH_INVALID"}
    assert r["status"] == xr.MEASURED


def test_a_stale_book_is_rejected_whatever_depth_it_carries():
    r = fill(ctx(observed_ms=T0 + 1000 - xr.MAX_OBSERVATION_AGE_MS - 1))
    assert r["status"] == xr.UNAVAILABLE
    assert "OBSERVATION_STALE" in r["reasons"]
    assert r["depth"] is None and r["reference_price"] is None


def test_a_book_observed_after_the_decision_is_rejected():
    r = fill(ctx(observed_ms=T0 + 1000 + xr.FUTURE_SKEW_MS + 1))
    assert r["status"] == xr.UNAVAILABLE
    assert "OBSERVATION_AFTER_DECISION" in r["reasons"]


def test_a_partial_fill_walks_the_requested_quantity_and_measures_the_filled():
    r = fill(ctx(), requested_qty=1.0, filled_qty=0.4, avg_price=100_012.0)
    assert r["filled_qty"] == 0.4 and r["unfilled_qty"] == pytest.approx(0.6)
    assert r["depth"]["requested_qty"] == 1.0
    assert r["notional_usd"] == pytest.approx(0.4 * 100_012.0)


def test_failed_submission_carries_context_but_no_price_and_no_fill():
    r = xr.failed_submission(ctx(), side="Buy", symbol="BTCUSDT",
                             product="linear", venue="bybit",
                             order_type="market", requested_qty=1.0,
                             why="SUBMISSION_FAILED: Boom",
                             environment="testnet", recorded_ms=T0 + 1500)
    assert r["status"] == xr.NO_FILL and r["filled_qty"] == 0.0
    assert r["environment"] == "testnet" and r["recorded_ms"] == T0 + 1500
    assert r["book_source"] == "orderbook" and r["book_age_ms"] == 100
    assert r["notional_usd"] is None
    assert "reference_price" not in r and "avg_price" not in r


# -- LedgerBroker.begin_decision --------------------------------------------------------

class Inner:
    VENUE = "bybit"

    def __init__(self, depth=None, top=None):
        self.depth, self.top = depth, top
        self.calls = []

    def get_book_depth(self, symbol, product):
        self.calls.append("depth")
        if isinstance(self.depth, Exception):
            raise self.depth
        return self.depth

    def get_book_top(self, symbol, product):
        self.calls.append("top")
        if isinstance(self.top, Exception):
            raise self.top
        return self.top


def decide(inner, **kw):
    lb = L.LedgerBroker(inner, L.Journal(), ms=lambda: T0)
    lb.begin_decision(symbol="BTCUSDT", product="linear", side="Buy", qty=1.0,
                      mark=100_000.0, **kw)
    return lb._decisions[("BTCUSDT", "linear", "Buy")]


DEPTH = {"bid": 99990.0, "ask": 100010.0, "venue_ts_ms": T0 - 20,
         "bids": xr.parse_ladder(BIDS, descending=True),
         "asks": xr.parse_ladder(ASKS, descending=False)}
TOP = {"bid": 99991.0, "ask": 100009.0}


def test_the_depth_read_is_preferred_and_the_ticker_is_not_touched():
    inner = Inner(DEPTH, TOP)
    c = decide(inner)
    assert inner.calls == ["depth"]
    assert (c.bid, c.ask, c.book_source) == (99990.0, 100010.0, "orderbook")
    assert c.observed_ms == T0 and c.book_venue_ts_ms == T0 - 20
    assert c.bids and c.asks


def test_a_failed_depth_read_falls_back_to_the_ticker_touch():
    inner = Inner(RuntimeError("down"), TOP)
    c = decide(inner)
    assert inner.calls == ["depth", "top"]
    assert (c.bid, c.ask, c.book_source) == (99991.0, 100009.0, "ticker_touch")
    assert c.bids is None and c.asks is None


def test_no_readable_book_means_no_book_and_the_mark_alone():
    inner = Inner(RuntimeError("down"), RuntimeError("down"))
    c = decide(inner)
    assert c.bid is None and c.ask is None and c.book_source == ""
    assert c.mark == 100_000.0


@pytest.mark.parametrize("bad", [
    {"bid": float("nan"), "ask": 1.0}, {"bid": 0.0, "ask": 1.0},
    {"bid": 101.0, "ask": 100.0}, {"bid": "x", "ask": 1.0}, {}, None])
def test_an_unusable_touch_is_no_book(bad):
    c = decide(Inner(bad, bad))
    assert c.bid is None and c.ask is None


def test_a_supplied_book_is_used_and_nothing_is_read():
    inner = Inner(DEPTH, TOP)
    c = decide(inner, bid=99.0, ask=101.0, observed_ms=T0 - 10,
               book_source="tick_snapshot")
    assert inner.calls == []
    assert c.book_source == "tick_snapshot" and (c.bid, c.ask) == (99.0, 101.0)


def test_capture_book_false_reads_nothing():
    inner = Inner(DEPTH, TOP)
    c = decide(inner, capture_book=False)
    assert inner.calls == [] and c.bid is None


def test_a_bounded_read_hands_each_source_what_is_left_of_one_deadline():
    seen = []

    class Timed(Inner):
        def get_book_depth(self, symbol, product, timeout_s=None):
            seen.append(("depth", timeout_s))
            time.sleep(0.05)
            raise RuntimeError("slow then failed")

        def get_book_top(self, symbol, product, timeout_s=None):
            seen.append(("top", timeout_s))
            return TOP

    c = decide(Timed(), book_timeout_s=1.0)
    (_n1, t1), (_n2, t2) = seen
    assert 0.9 < t1 <= 1.0 and 0 < t2 < t1 - 0.04       # one shared deadline
    assert c.book_source == "ticker_touch"


def test_a_spent_budget_reads_nothing_more_and_invents_no_book():
    seen = []

    class Slow(Inner):
        def get_book_depth(self, symbol, product, timeout_s=None):
            seen.append("depth")
            time.sleep(0.12)
            raise TimeoutError("hung")

        def get_book_top(self, symbol, product, timeout_s=None):
            seen.append("top")
            return TOP

    started = time.monotonic()
    c = decide(Slow(), book_timeout_s=0.1)
    assert seen == ["depth"]                      # no second read past the bound
    assert time.monotonic() - started < 0.5
    assert c.bid is None and c.ask is None and c.mark == 100_000.0


def test_an_unbounded_read_passes_no_timeout():
    seen = []

    class Plain(Inner):
        def get_book_depth(self, symbol, product, timeout_s=None):
            seen.append(timeout_s)
            return DEPTH

    decide(Plain())
    assert seen == [None]


class Bounded(Inner):
    """Getters shaped like the real adapter's: they CAN take a timeout."""
    def get_book_depth(self, symbol, product, timeout_s=None):
        return super().get_book_depth(symbol, product)

    def get_book_top(self, symbol, product, timeout_s=None):
        return super().get_book_top(symbol, product)


def test_a_bounded_read_that_answers_in_time_is_used():
    c = decide(Bounded(DEPTH, TOP), book_timeout_s=1.0)
    assert c.book_source == "orderbook" and c.bid == 99990.0


def test_a_bounded_read_never_calls_a_getter_that_cannot_take_a_bound():
    inner = Inner(DEPTH, TOP)                      # timeout-less getters
    c = decide(inner, book_timeout_s=1.0)
    assert inner.calls == [] and c.bid is None     # unmeasured, not unbounded


def test_a_bounded_read_that_raises_is_no_book():
    c = decide(Inner(RuntimeError("x"), RuntimeError("y")), book_timeout_s=1.0)
    assert c.bid is None


def test_environment_is_provenance_taken_from_the_client_when_it_states_one():
    class Client:
        venue = "testnet"

    inner = Inner(DEPTH, TOP)
    inner.client = Client()
    assert L.LedgerBroker(inner, L.Journal())._environment() == "testnet"
    assert L.LedgerBroker(Inner(), L.Journal())._environment() == ""

    class Bare:
        def __getattr__(self, name):
            raise RuntimeError("no client here")
    assert L.LedgerBroker(Bare(), L.Journal())._environment() == ""


# -- the bound is the transport's, not a thread's ------------------------------------

class _Transport:
    """One scripted answer per call; records the timeout it was given."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.timeouts = []

    def request(self, method, url, *, headers, params=None, body=None,
                timeout=10.0):
        self.timeouts.append(timeout)
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return a


def _client(tmp_path, transport):
    import bybit_connection as bc
    from persistence import StateStore
    from test_bybit_connection import Cfg
    store = StateStore(str(tmp_path / "s.db"))
    return bc.BybitClient(config=Cfg(), store=store, transport=transport), store


def test_a_per_call_timeout_reaches_the_transport_and_the_default_is_unchanged(
        tmp_path, monkeypatch):
    import json
    import bybit_connection as bc
    ok = (200, json.dumps({"retCode": 0, "retMsg": "OK", "result": {"list": []}}))
    t = _Transport(ok)
    client, store = _client(tmp_path, t)
    monkeypatch.setattr(bc.time, "sleep", lambda s: pytest.fail("slept"))
    client._request("GET", "/v5/market/tickers", params={"symbol": "BTCUSDT"})
    client._request("GET", "/v5/market/tickers", params={"symbol": "BTCUSDT"},
                    timeout_s=0.7, retries=0)
    assert t.timeouts == [client.timeout_s, 0.7]
    store.close()


@pytest.mark.parametrize("answer", [
    RuntimeError("connection reset"),
    (503, "busy"),
    (429, "slow down"),
    (200, '{"retCode": 10006, "retMsg": "rate limit", "result": {}}'),
    (200, '{"retCode": 10016, "retMsg": "server error", "result": {}}'),
])
def test_a_single_attempt_read_fails_at_once_without_sleeping(
        tmp_path, monkeypatch, answer):
    import bybit_connection as bc
    t = _Transport(answer)
    client, store = _client(tmp_path, t)
    slept = []
    monkeypatch.setattr(bc.time, "sleep", lambda s: slept.append(s))
    with pytest.raises(bc.BybitAPIError):
        client._request("GET", "/v5/market/orderbook",
                        params={"symbol": "BTCUSDT"}, retries=0, timeout_s=0.5)
    assert len(t.timeouts) == 1 and slept == []
    store.close()


def test_the_default_retry_path_still_backs_off(tmp_path, monkeypatch):
    import bybit_connection as bc
    t = _Transport(RuntimeError("reset"))
    client, store = _client(tmp_path, t)
    slept = []
    monkeypatch.setattr(bc.time, "sleep", lambda s: slept.append(s))
    with pytest.raises(bc.BybitAPIError):
        client._request("GET", "/v5/market/tickers", params={"symbol": "BTCUSDT"})
    assert len(t.timeouts) == 4 and slept            # unchanged: 1 + 3 retries
    store.close()


def test_the_carry_broker_hands_its_timeout_to_both_book_reads():
    c = BookClient(book())
    seen = []
    real = c._request

    def spy(method, endpoint, **kw):
        seen.append((endpoint, kw.pop("timeout_s", None), kw.get("retries")))
        return real(method, endpoint, **kw)
    c._request = spy
    b = broker(c)
    b.get_book_depth("BTCUSDT", "linear", timeout_s=0.4)
    try:
        b.get_book_top("BTCUSDT", "linear", timeout_s=0.4)
    except PairIncident:
        pass                                  # this fake's ticker has no touch
    assert seen == [("/v5/market/orderbook", 0.4, 0),
                    ("/v5/market/tickers", 0.4, 0)]
    # without a timeout the ticker read is exactly what it was (default retries)
    seen.clear()
    try:
        b.get_book_top("BTCUSDT", "linear")
    except PairIncident:
        pass
    assert seen == [("/v5/market/tickers", None, None)]   # client default: 3
