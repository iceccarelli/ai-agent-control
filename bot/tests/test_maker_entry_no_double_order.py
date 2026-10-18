"""A maker entry that may still be live must never be followed by a taker."""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from bybit_connection import OrderResult  # noqa: E402
from fake_bybit import FakeBybit  # noqa: E402
from test_observe_exits import LinearCfg, _engine  # noqa: E402


class StubClient:
    def __init__(self, maker, residual):
        self.maker, self.residual = maker, residual
        self.orders = []

    def place_order(self, **kw):
        self.orders.append(kw)
        if kw["order_type"] == "Limit":
            return self.maker
        return OrderResult(ok=True, order_link_id="TAKER", reason="SUBMITTED")

    def cancel_order(self, **kw):
        return None

    def get_order(self, **kw):
        return self.residual


def _enter(tmp_path, maker, residual):
    engine, store = _engine(tmp_path, LinearCfg(), FakeBybit())
    engine.prefer_maker = True
    engine.maker_timeout_s = 0.0
    engine._await_fill = lambda symbol, link, timeout_s=10.0: None
    engine.client = StubClient(maker, residual)
    result = engine._enter(symbol="BTCUSDT", side="Buy", qty=1.0, filters=None,
                           quote={"bid": 100.0, "ask": 100.1})
    return result, engine.client


def test_an_unknown_maker_outcome_sends_no_taker(tmp_path):
    result, client = _enter(
        tmp_path, OrderResult(ok=False, order_link_id="M", reason="OUTCOME_UNKNOWN"),
        None)
    assert result.ok is False
    assert [o["order_type"] for o in client.orders] == ["Limit"]


def test_a_maker_order_not_confirmed_cancelled_sends_no_taker(tmp_path):
    result, client = _enter(
        tmp_path, OrderResult(ok=True, order_link_id="M", reason="SUBMITTED"),
        {"orderStatus": "New", "cumExecQty": "0"})
    assert result.ok is False and result.reason == "MAKER_ENTRY_UNRESOLVED"
    assert [o["order_type"] for o in client.orders] == ["Limit"]


def test_an_unreadable_maker_order_sends_no_taker(tmp_path):
    result, client = _enter(
        tmp_path, OrderResult(ok=True, order_link_id="M", reason="SUBMITTED"), None)
    assert result.reason == "MAKER_ENTRY_UNRESOLVED"
    assert [o["order_type"] for o in client.orders] == ["Limit"]


def test_a_part_filled_maker_keeps_its_own_lot_and_sends_no_taker(tmp_path):
    maker = OrderResult(ok=True, order_link_id="M", reason="SUBMITTED")
    result, client = _enter(
        tmp_path, maker,
        {"orderStatus": "PartiallyFilledCanceled", "cumExecQty": "0.4"})
    assert result is maker
    assert [o["order_type"] for o in client.orders] == ["Limit"]


def test_a_cleanly_cancelled_unfilled_maker_falls_back_to_one_taker(tmp_path):
    result, client = _enter(
        tmp_path, OrderResult(ok=True, order_link_id="M", reason="SUBMITTED"),
        {"orderStatus": "Cancelled", "cumExecQty": "0"})
    assert result.order_link_id == "TAKER"
    assert [o["order_type"] for o in client.orders] == ["Limit", "Market"]
    assert client.orders[1]["qty"] == 1.0
