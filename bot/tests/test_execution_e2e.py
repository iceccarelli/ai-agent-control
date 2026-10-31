"""CarryBroker -> LedgerBroker -> Journal with a fake transport: the decision
price reaches the journal and the performance report MEASURES slippage."""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import execution_record as xr           # noqa: E402
import ledger as L                       # noqa: E402
import carry_performance as perf         # noqa: E402
from carry_broker import CarryBroker, OrderOutcomeUnknown  # noqa: E402

T0 = 1_700_000_000_000


def env(v):
    return {"retCode": 0, "retMsg": "OK", "result": v}


class Venue:
    """Fake transport. Time advances `latency_ms` on every order create."""

    def __init__(self, clock, *, bid, ask, fill_px, fill_frac=1.0,
                 latency_ms=120, fail_create=False, book_ok=True):
        self.clock, self.bid, self.ask = clock, bid, ask
        self.fill_px, self.fill_frac = fill_px, fill_frac
        self.latency_ms, self.fail_create, self.book_ok = (
            latency_ms, fail_create, book_ok)
        self.orders = {}

    def _request(self, method, endpoint, *, params=None, body=None,
                 signed=False, retries=3):
        if endpoint == "/v5/market/tickers":
            if not self.book_ok:
                raise RuntimeError("ticker down")
            return env({"list": [{"symbol": params["symbol"],
                                  "bid1Price": str(self.bid),
                                  "ask1Price": str(self.ask),
                                  "markPrice": "100000"}]})
        if endpoint == "/v5/order/create":
            if self.fail_create:
                e = RuntimeError("rejected")
                e.ret_code = 110007
                raise e
            self.clock[0] += self.latency_ms
            qty = float(body["qty"]) * self.fill_frac
            fee = {"USDT": f"{qty * self.fill_px * 0.00055:.8f}"}
            row = {"orderLinkId": body["orderLinkId"], "cumExecQty": str(qty),
                   "avgPrice": str(self.fill_px), "cumFeeDetail": fee}
            self.orders[body["orderLinkId"]] = dict(
                row, orderStatus="Filled" if self.fill_frac == 1 else
                "PartiallyFilledCanceled", updatedTime=str(self.clock[0]))
            return env(row)
        if endpoint in ("/v5/order/realtime", "/v5/order/history"):
            row = self.orders.get(params.get("orderLinkId"))
            return env({"list": [row] if row else []})
        return env({})


def build(venue, clock):
    cb = CarryBroker(client=venue, sequence_source=lambda *a: 7,
                     order_gate=lambda: (True, "TEST"),
                     clock_ms=lambda: clock[0])
    j = L.Journal()
    lb = L.LedgerBroker(cb, j, ms=lambda: clock[0])
    return lb, j


REPORT_ENV = {"actual_reconciled_eligible": True, "reasons": [],
              "booked_figures_class": "UNAVAILABLE"}


def test_decision_reaches_journal_and_slippage_is_measured():
    clock = [T0]
    v = Venue(clock, bid=99_990.0, ask=100_010.0, fill_px=99_985.0)
    lb, j = build(v, clock)
    lb.begin_decision(symbol="BTCUSDT", product="linear", side="Sell",
                      qty=0.01, mark=100_000.0,
                      expected_fee_usd=0.55, expected_cost_usd=0.80)
    clock[0] += 50                               # decision -> submit delay
    res = lb.place_market(symbol="BTCUSDT", side="Sell", qty=0.01,
                          product="linear")
    assert res["venue"] == "bybit" and res["submit_ms"] == T0 + 50

    e = j.entries[-1]
    assert e.kind == "perp_open"
    assert e.decision_mark == 99_990.0           # the bid the Sell decided on
    rec = e.fee_raw["execution"]
    assert rec["status"] == xr.MEASURED and rec["venue"] == "bybit"
    assert rec["order_link_id"] == e.ref
    assert rec["decision_to_submit_ms"] == 50
    assert rec["submit_to_fill_ms"] == 120
    assert rec["slippage_usd"] == pytest.approx(5.0 * 0.01)
    assert rec["spread_cost_usd"] == pytest.approx(10.0 * 0.01)
    assert rec["filled_qty"] == 0.01 and rec["unfilled_qty"] == 0.0

    # persisted without a second ledger: survives a rows round trip
    j2 = L.Journal.from_rows(j.to_rows())
    assert j2.entries[-1].decision_mark == 99_990.0
    assert j2.entries[-1].fee_raw["execution"]["slippage_usd"] == rec["slippage_usd"]

    s = perf.slippage_section(j2, REPORT_ENV)
    assert s["measured_fills"] == 1 and s["fills"] == 1
    assert s["slippage_cost_usd"] == pytest.approx(0.05)
    assert s["evidence_class"] != "UNAVAILABLE"
    assert xr.reconcile(lb.executions, lb.fills) == []


def test_without_begin_decision_slippage_stays_unavailable():
    clock = [T0]
    lb, j = build(Venue(clock, bid=1, ask=2, fill_px=99_985.0), clock)
    lb.place_market(symbol="BTCUSDT", side="Sell", qty=0.01, product="linear")
    e = j.entries[-1]
    assert e.decision_mark is None
    assert e.fee_raw["execution"]["status"] == xr.UNAVAILABLE
    assert "NO_DECISION_CONTEXT" in e.fee_raw["execution"]["reasons"]
    assert perf.slippage_section(j, REPORT_ENV)["evidence_class"] == "UNAVAILABLE"


def test_book_unreadable_falls_back_to_mark_and_says_so():
    clock = [T0]
    v = Venue(clock, bid=0, ask=0, fill_px=99_985.0, book_ok=False)
    lb, j = build(v, clock)
    lb.begin_decision(symbol="BTCUSDT", product="linear", side="Sell",
                      qty=0.01, mark=100_000.0)
    lb.place_market(symbol="BTCUSDT", side="Sell", qty=0.01, product="linear")
    rec = j.entries[-1].fee_raw["execution"]
    assert j.entries[-1].decision_mark == 100_000.0
    assert rec["reference_basis"] == "mark" and rec["spread_cost_usd"] is None


def test_stale_decision_is_not_journalled():
    clock = [T0]
    v = Venue(clock, bid=99_990.0, ask=100_010.0, fill_px=99_985.0)
    lb, j = build(v, clock)
    lb.begin_decision(symbol="BTCUSDT", product="linear", side="Sell",
                      qty=0.01, mark=100_000.0)
    clock[0] += 10 * 60_000
    lb.place_market(symbol="BTCUSDT", side="Sell", qty=0.01, product="linear")
    e = j.entries[-1]
    assert e.decision_mark is None
    assert "DECISION_STALE" in e.fee_raw["execution"]["reasons"]


def test_partial_fill_is_booked_and_measured_at_filled_qty_only():
    clock = [T0]
    v = Venue(clock, bid=99_990.0, ask=100_010.0, fill_px=99_980.0,
              fill_frac=0.4)
    lb, j = build(v, clock)
    lb.begin_decision(symbol="BTCUSDT", product="linear", side="Sell",
                      qty=0.01, mark=100_000.0, expected_cost_usd=1.0)
    lb.place_market(symbol="BTCUSDT", side="Sell", qty=0.01, product="linear")
    rec = j.entries[-1].fee_raw["execution"]
    assert rec["filled_qty"] == pytest.approx(0.004)
    assert rec["unfilled_qty"] == pytest.approx(0.006)
    assert rec["slippage_usd"] == pytest.approx(10.0 * 0.004)
    assert rec["expected_cost_usd"] == pytest.approx(0.4)
    assert lb.fills[-1]["qty"] == pytest.approx(0.004)
    assert xr.reconcile(lb.executions, lb.fills) == []


def test_failed_submission_books_nothing_and_records_no_fill():
    clock = [T0]
    v = Venue(clock, bid=99_990.0, ask=100_010.0, fill_px=1.0,
              fail_create=True)
    lb, j = build(v, clock)
    lb.begin_decision(symbol="BTCUSDT", product="linear", side="Sell",
                      qty=0.01, mark=100_000.0)
    assert lb.place_market(symbol="BTCUSDT", side="Sell", qty=0.01,
                           product="linear") is None
    assert j.entries == [] and lb.fills == []
    assert lb.executions[-1]["status"] == xr.NO_FILL
    assert lb.executions[-1]["filled_qty"] == 0.0


def test_exception_during_submission_is_recorded_then_reraised():
    clock = [T0]
    v = Venue(clock, bid=99_990.0, ask=100_010.0, fill_px=1.0)

    def boom(*a, **k):
        raise OrderOutcomeUnknown("lost")
    lb, j = build(v, clock)
    lb.inner.place_market = boom
    with pytest.raises(OrderOutcomeUnknown):
        lb.place_market(symbol="BTCUSDT", side="Sell", qty=0.01,
                        product="linear")
    assert lb.executions[-1]["status"] == xr.NO_FILL
    assert "SUBMISSION_FAILED" in lb.executions[-1]["reasons"][0]
    assert j.entries == []


def test_contradictory_fill_over_order_is_not_measured():
    """A result larger than asked is still booked as the venue says, but it
    never produces a decision mark."""
    clock = [T0]
    lb, j = build(Venue(clock, bid=99_990.0, ask=100_010.0, fill_px=99_985.0),
                  clock)
    lb.begin_decision(symbol="BTCUSDT", product="linear", side="Sell",
                      qty=0.01, mark=100_000.0)
    lb._book({"filled_qty": 0.02, "avg_price": 99_985.0,
              "order_link_id": "X1", "fee_detail": {
                  "components": [{"currency": "USDT", "amount": 0.1}],
                  "known": True, "source": "cumFeeDetail", "detail": ""}},
             side="Sell", product="linear", symbol="BTCUSDT",
             requested_qty=0.01, order_type="market")
    e = j.entries[-1]
    assert e.decision_mark is None
    assert "FILL_EXCEEDS_ORDER" in e.fee_raw["execution"]["reasons"]


def test_old_rows_without_execution_still_load():
    j = L.Journal()
    L.perp_open(j, ms=T0, ref="a", qty=1.0, price=100.0, fee_usd=0.0,
                fee_status="confirmed")
    j2 = L.Journal.from_rows(j.to_rows())
    assert j2.entries[0].decision_mark is None
    assert "execution" not in j2.entries[0].fee_raw


def test_the_engine_declares_the_tick_mark_as_the_decision_price():
    from carry_engine import CarryEngine

    seen = []

    class B:
        def begin_decision(self, **kw):
            seen.append(kw)

    e = CarryEngine.__new__(CarryEngine)
    e.broker = B()
    e._decision_marks = {"linear": 60000.0, "spot": 59990.0}
    e._declare_decision("BTCUSDT", "linear", "Sell", 0.01)
    e._declare_decision("BTCUSDT", "spot", "Buy", 0.01)
    assert [(k["product"], k["mark"]) for k in seen] == [("linear", 60000.0),
                                                         ("spot", 59990.0)]


def test_no_decision_price_means_nothing_is_declared():
    from carry_engine import CarryEngine

    class B:
        def begin_decision(self, **kw):
            raise AssertionError("must not be called")

    e = CarryEngine.__new__(CarryEngine)
    e.broker = B()
    e._decision_marks = {"linear": 60000.0, "spot": None}
    e._declare_decision("BTCUSDT", "spot", "Buy", 0.01)      # no spot mark


def test_an_engine_opened_pair_journals_the_decision_price(tmp_path):
    """Production path end to end: on_candle -> _fire -> LedgerBroker ->
    CarryBroker -> Journal. Every fill carries the tick's decision mark."""
    from test_paired_leg_recovery import (FeeVenue, MARK, modern, stack,
                                          warm_and_open)
    store, venue, broker, engine = stack(str(tmp_path / "s.db"),
                                         FeeVenue(modern, spot_qty=0.0))
    d = warm_and_open(engine)
    assert engine.position is not None, d
    fills = [e for e in engine.broker.journal.entries
             if e.kind in ("spot_buy", "perp_open")]
    assert len(fills) == 2
    for e in fills:
        rec = e.fee_raw["execution"]
        assert e.decision_mark is not None and e.decision_mark > 0
        assert rec["status"] == "MEASURED", rec
        # the reference is the decision-time executable side price, not a mark
        # reconstructed from the fill
        assert abs(e.decision_mark - MARK) / MARK < 0.01
    store.close()


def test_an_exit_takes_no_extra_book_read_and_a_missing_price_clears_the_context():
    from carry_engine import CarryEngine
    calls = []

    class B:
        def begin_decision(self, **kw):
            calls.append(("begin", kw))

        def end_decision(self, **kw):
            calls.append(("end", kw))

    e = CarryEngine.__new__(CarryEngine)
    e.broker = B()
    e._decision_marks = {"linear": 60000.0, "spot": None}
    e._declare_decision("BTCUSDT", "linear", "Buy", 0.01)     # an exit
    e._declare_decision("BTCUSDT", "linear", "Sell", 0.01)    # an entry
    e._declare_decision("BTCUSDT", "spot", "Buy", 0.01)       # no spot price
    assert calls[0][0] == "begin" and calls[0][1]["capture_book"] is False
    assert calls[1][0] == "begin" and "capture_book" not in calls[1][1]
    assert calls[2][0] == "end"
