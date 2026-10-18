"""A linear close that did not execute must not erase the position row.

The venue's position list is what observe_exits books exits from. Deleting the
row for a close whose fill never resolved (or only partly executed) hid a
position that was still open, so a new entry could stack on it.
"""
from __future__ import annotations

import os
import sys

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
