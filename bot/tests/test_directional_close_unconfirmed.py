"""A linear close that did not execute must not erase the position row.

The venue's position list is what observe_exits books exits from. Deleting the
row for a close whose fill never resolved (or only partly executed) hid a
position that was still open, so a new entry could stack on it.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from fake_bybit import FakeBybit  # noqa: E402
from test_observe_exits import LinearCfg, _engine, _open  # noqa: E402


def _linear(tmp_path):
    fake = FakeBybit()
    engine, store = _engine(tmp_path, LinearCfg(), fake)
    _open(store, side="Buy", qty=0.01, entry=60_000.0)
    return engine, store


def test_a_linear_close_with_no_resolved_fill_keeps_the_row(tmp_path):
    engine, store = _linear(tmp_path)
    engine._await_fill = lambda symbol, link, timeout_s=10.0: None
    report = engine.close_position(symbol="BTCUSDT", reason="manual")
    assert report.ok is False and report.reason == "CLOSE_UNCONFIRMED"
    assert store.open_position_count() == 1
    assert store._query("SELECT * FROM trades") == []


def test_a_linear_close_that_only_partly_executed_keeps_the_row(tmp_path):
    engine, store = _linear(tmp_path)
    engine._await_fill = lambda symbol, link, timeout_s=10.0: {
        "orderStatus": "PartiallyFilledCanceled", "cumExecQty": "0.004",
        "avgPrice": "60100"}
    report = engine.close_position(symbol="BTCUSDT", reason="manual")
    assert report.ok is False and report.reason == "CLOSE_UNCONFIRMED"
    assert store.open_position_count() == 1


def test_a_linear_close_that_fully_executed_is_booked(tmp_path):
    engine, store = _linear(tmp_path)
    engine._await_fill = lambda symbol, link, timeout_s=10.0: {
        "orderStatus": "Filled", "cumExecQty": "0.01", "avgPrice": "60100"}
    report = engine.close_position(symbol="BTCUSDT", reason="manual")
    assert report.ok is True
    assert store.open_position_count() == 0


def test_a_cancelled_market_order_that_part_filled_is_a_fill_not_unknown(tmp_path):
    engine, store = _linear(tmp_path)

    class Client:
        def get_order(self, **kw):
            return {"orderStatus": "PartiallyFilledCanceled",
                    "cumExecQty": "0.003", "avgPrice": "60000"}
    engine.client = Client()
    fill = engine._await_fill("BTCUSDT", "x", timeout_s=1.0)
    assert fill is not None and float(fill["cumExecQty"]) == 0.003


def test_a_cancelled_order_with_nothing_executed_is_still_unfilled(tmp_path):
    engine, store = _linear(tmp_path)

    class Client:
        def get_order(self, **kw):
            return {"orderStatus": "PartiallyFilledCanceled", "cumExecQty": "0"}
    engine.client = Client()
    assert engine._await_fill("BTCUSDT", "x", timeout_s=1.0) is None


def test_a_final_exit_after_a_take_profit_is_priced_on_its_own_rows(tmp_path):
    """Long 0.01: TP1 took 0.005 @ 62000 (already booked); the stop then took
    the other 0.005 @ 58000. The venue lists both closes; the stop-out must be
    booked at 58000, not at the 60000 blend (which would hide the loss)."""
    import time
    import pytest
    fake = FakeBybit()
    engine, store = _engine(tmp_path, LinearCfg(), fake)
    _open(store, side="Buy", qty=0.005, entry=60_000.0)   # TP1 already booked
    now = int(time.time() * 1000)
    fake.closed_pnl.append({
        "symbol": "BTCUSDT", "side": "Sell", "qty": "0.005", "orderId": "tp1",
        "avgExitPrice": "62000", "closedPnl": "10", "updatedTime": str(now + 1000)})
    fake.closed_pnl.append({
        "symbol": "BTCUSDT", "side": "Sell", "qty": "0.005", "orderId": "stop",
        "avgExitPrice": "58000", "closedPnl": "-10", "updatedTime": str(now + 2000)})
    s = engine.observe_exits()
    assert s["closed"] == 1
    trades = store._query("SELECT * FROM trades")
    assert len(trades) == 1
    assert float(trades[0]["exit_price"]) == pytest.approx(58_000.0)
    assert float(trades[0]["net_pnl"]) < 0


def test_two_unpriced_partial_exits_of_equal_size_are_both_booked(tmp_path):
    """With closed-pnl lagging (no rows), TP1 then the stop each take 0.005 of
    a 0.01 position. Their dedupe keys must differ or the second (losing) exit
    is dropped from the trades table and the loss streak."""
    fake = FakeBybit()
    engine, store = _engine(tmp_path, LinearCfg(), fake)
    _open(store, side="Buy", qty=0.01, entry=60_000.0)
    fake.positions["BTCUSDT"] = {"symbol": "BTCUSDT", "side": "Buy",
                                 "size": "0.005", "avgPrice": "60000",
                                 "stopLoss": "58000"}
    engine.client.get_last_price = lambda symbol: 62_000.0
    assert engine.observe_exits()["reduced"] == 1
    del fake.positions["BTCUSDT"]
    engine.client.get_last_price = lambda symbol: 58_000.0
    assert engine.observe_exits()["closed"] == 1
    assert len(store._query("SELECT * FROM trades")) == 2


def test_an_unreadable_live_book_reaches_the_gate_as_unavailable(tmp_path):
    fake = FakeBybit()
    engine, store = _engine(tmp_path, LinearCfg(), fake)
    engine.client.get_book_features = lambda symbol: None
    engine.client.book_feed_unavailable = lambda symbol: True
    assert engine._market_conditions("BTCUSDT")["book"] == {"unavailable": True}


def test_no_feed_still_reaches_the_gate_as_none(tmp_path):
    fake = FakeBybit()
    engine, store = _engine(tmp_path, LinearCfg(), fake)
    engine.client.get_book_features = lambda symbol: None
    engine.client.book_feed_unavailable = lambda symbol: False
    assert engine._market_conditions("BTCUSDT")["book"] is None


def test_a_raising_book_source_is_unavailable_not_absent(tmp_path):
    fake = FakeBybit()
    engine, store = _engine(tmp_path, LinearCfg(), fake)

    def boom(symbol):
        raise RuntimeError("feed down")
    engine.client.get_book_features = boom
    assert engine._market_conditions("BTCUSDT")["book"] == {"unavailable": True}


def test_reanchor_leaves_a_plan_with_no_stop_distance_alone(tmp_path):
    import trading_engine as te
    engine, store = _engine(tmp_path, LinearCfg(), FakeBybit())
    plan = te.ExitPlan(stop_price=98.0, legs=((104.0, 1.0),), stop_distance=0.0)
    assert engine._reanchor(plan, side="Buy", fill_price=100.0) is plan



def test_a_derivatives_order_cancelled_after_a_partial_fill_counts_as_a_fill(tmp_path):
    """Only spot reports PartiallyFilledCanceled; linear says Cancelled with an
    executed quantity. That executed quantity is a held position."""
    engine, store = _linear(tmp_path)

    class Client:
        def get_order(self, **kw):
            return {"orderStatus": "Cancelled", "cumExecQty": "0.003",
                    "avgPrice": "60000"}
    engine.client = Client()
    fill = engine._await_fill("BTCUSDT", "x", timeout_s=1.0)
    assert fill is not None and float(fill["cumExecQty"]) == 0.003


def test_a_cancelled_order_with_nothing_executed_is_still_unfilled_on_linear(tmp_path):
    engine, store = _linear(tmp_path)

    class Client:
        def get_order(self, **kw):
            return {"orderStatus": "Cancelled", "cumExecQty": "0"}
    engine.client = Client()
    assert engine._await_fill("BTCUSDT", "x", timeout_s=1.0) is None


def test_an_emergency_close_that_only_partly_executed_keeps_the_remainder(tmp_path):
    import trading_engine as te
    engine, store = _linear(tmp_path)
    store.upsert_position("BTCUSDT", "Buy", 0.01, 60_000.0, stop_price=58_000.0)
    engine._await_fill = lambda symbol, link, timeout_s=10.0: {
        "orderStatus": "PartiallyFilledCanceled", "cumExecQty": "0.004",
        "avgPrice": "59900"}
    import position_sizing as ps
    filters = engine.client.get_instrument_filters("BTCUSDT")
    engine._emergency_close(symbol="BTCUSDT", exit_side="Sell", qty=0.01,
                            filters=filters, reason="TEST")
    rows = store.open_positions()
    assert len(rows) == 1 and float(rows[0]["qty"]) == pytest.approx(0.006)
