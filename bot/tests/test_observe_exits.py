"""Exchange-side exits must reach the ledger.

Invariant protected (audit S5): a stop or take-profit filling at the venue
was observed by nothing; positions rows lived forever, trades were never
written by a live run, and MAX_OPEN_POSITIONS eventually locked the shell
out. observe_exits() books what the venue says happened. It never places an
order and never invents a position.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import bybit_connection as bc  # noqa: E402
import trading_engine as te  # noqa: E402
from fake_bybit import FakeBybit  # noqa: E402
from persistence import StateStore  # noqa: E402
from position_sizing import BillionairePositionSizing  # noqa: E402
from risk_management import BillionaireRiskManager  # noqa: E402
from test_orchestrator import Cfg  # noqa: E402


class LinearCfg(Cfg):
    CATEGORY = "linear"
    ALLOW_SHORTS = True


def _engine(tmp_path, cfg, fake):
    store = StateStore(str(tmp_path / "state.db"))
    store.claim_writer()
    store.update_equity(10_000.0)
    client = bc.BybitClient(config=cfg, store=store, transport=fake)
    risk = BillionaireRiskManager(config=cfg, store=store)
    engine = te.TradingEngine(
        client=client, risk_manager=risk,
        position_sizer=BillionairePositionSizing(config=cfg, risk_manager=risk),
        store=store, config=cfg,
    )
    return engine, store


def _open(store, symbol="BTCUSDT", side="Sell", qty=0.01, entry=60_000.0):
    store.upsert_position(symbol, side, qty, entry, stop_price=61_000.0)


def test_spot_does_nothing(tmp_path):
    fake = FakeBybit()
    engine, store = _engine(tmp_path, Cfg(), fake)
    _open(store)
    assert engine.observe_exits()["checked"] == 0
    assert store.open_position_count() == 1


def test_position_still_open_at_venue_is_untouched(tmp_path):
    fake = FakeBybit()
    fake.positions["BTCUSDT"] = {"symbol": "BTCUSDT", "side": "Sell", "size": "0.01",
                                 "avgPrice": "60000", "stopLoss": "61000"}
    engine, store = _engine(tmp_path, LinearCfg(), fake)
    _open(store)
    s = engine.observe_exits()
    assert s["checked"] == 1 and s["closed"] == 0 and s["reduced"] == 0
    assert store.open_position_count() == 1


def test_stop_fill_at_venue_is_booked_at_closed_pnl_price(tmp_path):
    fake = FakeBybit()
    engine, store = _engine(tmp_path, LinearCfg(), fake)
    _open(store)  # venue flat: fake.positions has no BTCUSDT
    fake.closed_pnl.append({
        "symbol": "BTCUSDT", "side": "Buy", "qty": "0.01",
        "avgExitPrice": "61000", "closedPnl": "-10.6",
        "updatedTime": str(int(time.time() * 1000) + 1000),
    })
    s = engine.observe_exits()
    assert s["closed"] == 1 and s["price_approximated"] == 0
    assert store.open_position_count() == 0
    trades = store._query("SELECT * FROM trades")
    assert len(trades) == 1
    assert float(trades[0]["exit_price"]) == pytest.approx(61_000.0)
    # a short stopped out above entry is a loss
    assert float(trades[0]["net_pnl"]) < 0


def test_partial_take_profit_reduces_the_ledger(tmp_path):
    fake = FakeBybit()
    fake.positions["BTCUSDT"] = {"symbol": "BTCUSDT", "side": "Sell", "size": "0.004",
                                 "avgPrice": "60000", "stopLoss": "61000"}
    engine, store = _engine(tmp_path, LinearCfg(), fake)
    _open(store, qty=0.01)
    fake.closed_pnl.append({
        "symbol": "BTCUSDT", "side": "Buy", "qty": "0.006",
        "avgExitPrice": "59000", "closedPnl": "6",
        "updatedTime": str(int(time.time() * 1000) + 1000),
    })
    s = engine.observe_exits()
    assert s["reduced"] == 1 and s["closed"] == 0
    row = store.open_positions()[0]
    assert float(row["qty"]) == pytest.approx(0.004)


def test_closed_pnl_unreadable_still_books_the_exit_and_says_so(tmp_path):
    fake = FakeBybit()
    engine, store = _engine(tmp_path, LinearCfg(), fake)
    _open(store)
    calls = {"n": 0}
    real = fake.request

    def flaky(method, url, **kw):
        if "closed-pnl" in url:
            calls["n"] += 1
            return 500, "boom"
        return real(method, url, **kw)
    fake.request = flaky
    s = engine.observe_exits()
    assert s["closed"] == 1 and s["price_approximated"] == 1
    assert store.open_position_count() == 0
    trade = store._query("SELECT * FROM trades")[0]
    assert "PRICE_UNREAD" in str(trade["meta"])


def test_never_places_an_order(tmp_path):
    fake = FakeBybit()
    engine, store = _engine(tmp_path, LinearCfg(), fake)
    _open(store)
    engine.observe_exits()
    assert not any(r["url"].endswith("/v5/order/create") for r in fake.requests)
