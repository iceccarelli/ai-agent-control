"""Exchange-side exits must reach the ledger.

Invariant protected (audit S5): a stop or take-profit filling at the venue
was observed by nothing; positions rows lived forever, trades were never
written by a live run, and MAX_OPEN_POSITIONS eventually locked the shell
out. observe_exits() books what the venue says happened. It never places an
order and never invents a position.
"""
from __future__ import annotations

import json
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


def test_spot_with_no_filled_protective_order_does_nothing(tmp_path):
    fake = FakeBybit()
    engine, store = _engine(tmp_path, Cfg(), fake)
    _open(store)
    assert engine.observe_exits()["closed"] == 0
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


def test_closed_pnl_unreadable_books_the_exit_provisional_not_at_a_market_price(
        tmp_path):
    fake = FakeBybit()
    engine, store = _engine(tmp_path, LinearCfg(), fake)
    _open(store)
    real = fake.request

    def flaky(method, url, **kw):
        if "closed-pnl" in url:
            return 500, "boom"
        if "tickers" in url:
            return 500, "ticker down"          # the last price is not a fallback
        return real(method, url, **kw)
    fake.request = flaky
    s = engine.observe_exits()
    assert s["closed"] == 1 and s["exits_provisional"] == 1
    assert store.open_position_count() == 0          # the position is not lost
    trade = store._query("SELECT * FROM trades")[0]
    assert trade["price_provisional"] == 1
    assert trade["exit_price"] == 0.0 and trade["gross_pnl"] == 0.0
    assert trade["exit_fee_realized"] == 0
    assert store.recent_trades() == [] and store.net_returns() == []
    assert engine.observe_exits()["closed"] == 0        # replay books nothing
    assert len(store._query("SELECT * FROM trades")) == 1


def test_zero_avg_exit_price_is_provisional_and_keeps_the_order_id(tmp_path):
    import json
    fake = FakeBybit()
    engine, store = _engine(tmp_path, LinearCfg(), fake)
    _open(store)
    fake.closed_pnl.append({
        "symbol": "BTCUSDT", "side": "Buy", "qty": "0.01", "orderId": "ORD1",
        "avgExitPrice": "0", "closedPnl": "0",
        "updatedTime": str(int(time.time() * 1000) + 1000)})
    engine.observe_exits()
    trade = store._query("SELECT * FROM trades")[0]
    assert trade["price_provisional"] == 1
    assert json.loads(trade["meta"])["exit_order_ids"] == ["ORD1"]


def test_a_partial_exit_with_no_price_resizes_the_position_once(tmp_path):
    fake = FakeBybit()
    fake.positions["BTCUSDT"] = {"symbol": "BTCUSDT", "side": "Sell",
                                 "size": "0.004", "avgPrice": "60000",
                                 "stopLoss": "61000"}
    engine, store = _engine(tmp_path, LinearCfg(), fake)
    _open(store, qty=0.01)
    real = fake.request
    fake.request = lambda m, u, **kw: ((500, "boom") if "closed-pnl" in u
                                       else real(m, u, **kw))
    engine.observe_exits()
    engine.observe_exits()
    assert float(store.open_positions()[0]["qty"]) == pytest.approx(0.004)
    assert len(store._query("SELECT * FROM trades")) == 1


def test_never_places_an_order(tmp_path):
    fake = FakeBybit()
    engine, store = _engine(tmp_path, LinearCfg(), fake)
    _open(store)
    engine.observe_exits()
    assert not any(r["url"].endswith("/v5/order/create") for r in fake.requests)


# -- spot: exits are read from the protective orders themselves -----------


def _spot_row(store, qty=0.01, entry=60_000.0, stop_id="STOP1", tps=("TP1",)):
    store.upsert_position(
        "BTCUSDT", "Buy", qty, entry, stop_price=58_800.0,
        meta={"stop_order_link_id": stop_id, "tp_order_ids": list(tps)})


def _serve(engine, orders):
    real = engine.client.get_order

    def get_order(*, symbol, order_link_id="", order_id="", strict=False):
        if order_link_id in orders:
            return orders[order_link_id]
        return None
    engine.client.get_order = get_order
    return real


def test_spot_stop_fill_is_booked_as_a_loss_and_closes_the_row(tmp_path):
    fake = FakeBybit()
    engine, store = _engine(tmp_path, Cfg(), fake)
    _spot_row(store)
    _serve(engine, {"STOP1": {"orderStatus": "Filled", "cumExecQty": "0.01",
                              "avgPrice": "58800", "orderId": "V1"}})
    s = engine.observe_exits()
    assert s["closed"] == 1
    assert store.open_position_count() == 0
    trades = store._query("SELECT * FROM trades")
    assert len(trades) == 1
    assert float(trades[0]["exit_price"]) == pytest.approx(58_800.0)
    assert float(trades[0]["net_pnl"]) < 0
    assert engine.observe_exits()["closed"] == 0          # nothing re-booked


def test_spot_partial_take_profit_reduces_once_and_is_not_rebooked(tmp_path):
    fake = FakeBybit()
    engine, store = _engine(tmp_path, Cfg(), fake)
    _spot_row(store, qty=0.01, tps=("TP1", "TP2"))
    _serve(engine, {"TP1": {"orderStatus": "Filled", "cumExecQty": "0.004",
                            "avgPrice": "62000", "orderId": "V2"}})
    assert engine.observe_exits()["reduced"] == 1
    row = store.open_positions()[0]
    assert float(row["qty"]) == pytest.approx(0.006)
    for _ in range(3):                                     # later cycles
        engine.observe_exits()
    assert float(store.open_positions()[0]["qty"]) == pytest.approx(0.006)
    assert len(store._query("SELECT * FROM trades")) == 1
    meta = json.loads(store.open_positions()[0]["meta"])
    assert "TP1" in meta["booked_exit_orders"]
    assert meta["tp_order_ids"] == ["TP1", "TP2"]


def test_spot_untriggered_or_unreadable_orders_book_nothing(tmp_path):
    fake = FakeBybit()
    engine, store = _engine(tmp_path, Cfg(), fake)
    _spot_row(store)
    _serve(engine, {"STOP1": {"orderStatus": "Untriggered", "cumExecQty": "0"},
                    "TP1": {"orderStatus": "New", "cumExecQty": "0"}})
    s = engine.observe_exits()
    assert s["closed"] == 0 and s["reduced"] == 0
    assert store.open_position_count() == 1


def test_a_linear_trade_says_its_pnl_excludes_funding(tmp_path):
    """The directional book does not read the venue's funding settlements, so a
    linear trade's net pnl must say it is before funding rather than imply it
    is complete."""
    fake = FakeBybit()
    engine, store = _engine(tmp_path, LinearCfg(), fake)
    _open(store)
    fake.closed_pnl.append({
        "symbol": "BTCUSDT", "side": "Buy", "qty": "0.01", "orderId": "o1",
        "avgExitPrice": "59000", "closedPnl": "10",
        "updatedTime": str(int(time.time() * 1000) + 1000)})
    engine.observe_exits()
    meta = json.loads(store._query("SELECT * FROM trades")[0]["meta"])
    assert meta["funding_booked"] is False


def test_a_linear_remainder_below_the_dust_threshold_stays_on_the_book(tmp_path):
    """On linear the remainder is real venue size with a position-attached stop;
    booking it as dust and deleting the row would leave it unwatched."""
    fake = FakeBybit()
    fake.positions["BTCUSDT"] = {"symbol": "BTCUSDT", "side": "Sell",
                                 "size": "0.00005", "avgPrice": "60000",
                                 "stopLoss": "61000"}
    engine, store = _engine(tmp_path, LinearCfg(), fake)
    _open(store, qty=0.001)
    fake.closed_pnl.append({
        "symbol": "BTCUSDT", "side": "Buy", "qty": "0.00095", "orderId": "o9",
        "avgExitPrice": "59000", "closedPnl": "9",
        "updatedTime": str(int(time.time() * 1000) + 1000)})
    s = engine.observe_exits()
    assert s["reduced"] == 1 and s["closed"] == 0
    rows = store.open_positions()
    assert len(rows) == 1 and float(rows[0]["qty"]) == pytest.approx(0.00005)
