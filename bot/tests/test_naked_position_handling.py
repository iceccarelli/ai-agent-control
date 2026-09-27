"""TradingEngine.check_naked_positions() - the mid-cycle counterpart to
reconcile()'s startup-only, local-ledger-only naked check.

reconcile()'s naked_positions comes from StateStore.positions_without_stops(),
which trusts the ledger's own stop_price column - it cannot see a stop that
was live and was THEN CLEARED AT THE VENUE while the position stayed open.
check_naked_positions() reads verify_stop() straight from the exchange for
every open linear position, every cycle, so that incident is caught inside
the cycle it happens in. Same style as test_observe_exits.py: a real
BybitClient against the offline FakeBybit transport, not a hand-rolled fake.
"""
from __future__ import annotations

import os
import sys

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

SYMBOL = "BTCUSDT"


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


def _open_ledger_and_venue(store, fake, *, side="Buy", qty=0.001,
                           entry=60_000.0, stop=57_000.0):
    store.upsert_position(SYMBOL, side, qty, entry, stop_price=stop)
    fake.positions[SYMBOL] = {"symbol": SYMBOL, "side": side, "size": str(qty),
                              "avgPrice": str(entry), "stopLoss": str(stop)}


def test_spot_client_never_checks(tmp_path):
    fake = FakeBybit()
    engine, store = _engine(tmp_path, Cfg(), fake)
    store.upsert_position(SYMBOL, "Buy", 0.001, 60_000.0, stop_price=57_000.0)
    result = engine.check_naked_positions()
    assert result["checked"] == 0
    assert not any(r["url"].endswith("/v5/position/trading-stop")
                  for r in fake.requests)


def test_a_protected_position_is_untouched(tmp_path):
    fake = FakeBybit()
    engine, store = _engine(tmp_path, LinearCfg(), fake)
    _open_ledger_and_venue(store, fake)
    result = engine.check_naked_positions()
    assert result["checked"] == 1
    assert result["reprotected"] == [] and result["flattened"] == []
    # No order and no trading-stop write - only reads.
    assert not any(r["url"].endswith("/v5/order/create") for r in fake.requests)
    assert not any(r["url"].endswith("/v5/position/trading-stop")
                  for r in fake.requests)


def test_a_stop_cleared_at_the_venue_is_reprotected(tmp_path):
    """The exact incident: the ledger believes stop_price=57000 (it wrote
    it), but the venue's stopLoss field is empty - a stop lost venue-side,
    invisible to reconcile()'s ledger-only check."""
    fake = FakeBybit()
    engine, store = _engine(tmp_path, LinearCfg(), fake)
    _open_ledger_and_venue(store, fake)
    fake.positions[SYMBOL]["stopLoss"] = ""  # cleared AT THE VENUE ONLY

    result = engine.check_naked_positions()

    assert result["checked"] == 1
    assert result["reprotected"] == [SYMBOL]
    assert fake.positions[SYMBOL]["stopLoss"] not in ("", "0", "0.0")
    row = store.open_positions()[0]
    assert float(row["stop_price"]) > 0
    assert store.open_position_count() == 1


def test_reprotect_failure_flattens_and_trips_the_kill_switch(tmp_path):
    fake = FakeBybit()
    engine, store = _engine(tmp_path, LinearCfg(), fake)
    _open_ledger_and_venue(store, fake)
    fake.positions[SYMBOL]["stopLoss"] = ""
    fake.trading_stop_fails_with = 10001  # the re-attach itself is refused

    result = engine.check_naked_positions()

    assert result["checked"] == 1
    assert result["flattened"] == [SYMBOL]
    assert store.open_position_count() == 0
    engaged, reason = store.is_kill_switch_engaged()
    assert engaged is True
    assert "NAKED_MID_CYCLE" in reason


def test_an_exited_position_is_left_to_observe_exits(tmp_path):
    """A position the venue already shows flat is not this method's job -
    observe_exits() books the exit. check_naked_positions must not touch
    it (no order, no naked action)."""
    fake = FakeBybit()
    engine, store = _engine(tmp_path, LinearCfg(), fake)
    store.upsert_position(SYMBOL, "Buy", 0.001, 60_000.0, stop_price=57_000.0)
    # fake.positions has no BTCUSDT - the venue is flat.
    result = engine.check_naked_positions()
    assert result["checked"] == 1
    assert result["reprotected"] == [] and result["flattened"] == []
    assert not any(r["url"].endswith("/v5/order/create") for r in fake.requests)


def test_an_unreadable_venue_is_counted_not_acted_on(tmp_path):
    fake = FakeBybit()
    engine, store = _engine(tmp_path, LinearCfg(), fake)
    _open_ledger_and_venue(store, fake)
    real = fake.request

    def flaky(method, url, **kw):
        if "/v5/position/list" in url:
            return 500, "boom"
        return real(method, url, **kw)
    fake.request = flaky

    result = engine.check_naked_positions()
    assert result["unread"] == 1
    assert result["reprotected"] == [] and result["flattened"] == []
    assert store.open_position_count() == 1


def test_reconcile_still_catches_a_stop_this_process_never_recorded(tmp_path):
    """reconcile()'s own ledger-only path (positions_without_stops) still
    works for its own case: a position whose LEDGER stop_price is 0 - a
    lost fill or a crash mid-attach, not a venue-side clear."""
    fake = FakeBybit()
    engine, store = _engine(tmp_path, LinearCfg(), fake)
    store.upsert_position(SYMBOL, "Buy", 0.001, 60_000.0, stop_price=0.0)
    fake.positions[SYMBOL] = {"symbol": SYMBOL, "side": "Buy", "size": "0.001",
                              "avgPrice": "60000", "stopLoss": ""}
    summary = engine.reconcile()
    assert summary["naked_positions"] == [SYMBOL]
    assert fake.positions[SYMBOL]["stopLoss"] not in ("", "0", "0.0")
    row = store.open_positions()[0]
    assert float(row["stop_price"]) > 0


class TestClearPositionStop:
    def test_spot_refuses(self, tmp_path):
        fake = FakeBybit()
        store = StateStore(str(tmp_path / "state.db"))
        client = bc.BybitClient(config=Cfg(), store=store, transport=fake)
        result = client.clear_position_stop(symbol=SYMBOL)
        assert not result.ok
        assert result.reason == "SPOT_HAS_NO_POSITION_STOP_TO_CLEAR"

    def test_linear_clears_a_live_stop(self, tmp_path):
        fake = FakeBybit()
        store = StateStore(str(tmp_path / "state.db"))
        client = bc.BybitClient(config=LinearCfg(), store=store, transport=fake)
        fake.positions[SYMBOL] = {"symbol": SYMBOL, "side": "Buy", "size": "0.001",
                                  "avgPrice": "60000", "stopLoss": "57000"}
        result = client.clear_position_stop(symbol=SYMBOL)
        assert result.ok, result.reason
        live, detail = client.verify_stop(symbol=SYMBOL, order_link_id="")
        assert live is False
        assert detail == "POSITION_HAS_NO_STOP_LOSS"

    def test_a_rejected_clear_is_reported_not_silenced(self, tmp_path):
        fake = FakeBybit()
        store = StateStore(str(tmp_path / "state.db"))
        client = bc.BybitClient(config=LinearCfg(), store=store, transport=fake)
        fake.positions[SYMBOL] = {"symbol": SYMBOL, "side": "Buy", "size": "0.001",
                                  "avgPrice": "60000", "stopLoss": "57000"}
        fake.trading_stop_fails_with = 10001
        result = client.clear_position_stop(symbol=SYMBOL)
        assert not result.ok
        assert "REJECTED" in result.reason
        # Nothing changed at the venue - the rejected write never landed.
        assert fake.positions[SYMBOL]["stopLoss"] == "57000"
