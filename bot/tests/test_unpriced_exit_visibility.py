"""An exit that never prices must stay visible and must stop new risk."""
from __future__ import annotations

import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import risk_management as rm  # noqa: E402
from performance_analytics import PerformanceAnalytics  # noqa: E402
from persistence import PersistenceError, StateStore, TradeRecord, utc_now_epoch  # noqa: E402
from test_risk_management import good_order  # noqa: E402

H = 3600.0


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "s.db"))
    s.claim_writer()
    s.update_equity(10_000.0)
    yield s
    s.close()


def _exit(store, *, age_h, provisional=True, opened=None, qty=0.01):
    closed = utc_now_epoch() - age_h * H
    return store.record_trade(TradeRecord(
        symbol="BTCUSDT", side="Sell", qty=qty, entry_price=60_000.0,
        exit_price=0.0 if provisional else 59_000.0,
        price_provisional=provisional,
        gross_pnl=0.0 if provisional else 10.0, entry_fee=0.1, exit_fee=0.1,
        opened_epoch=opened if opened is not None else closed - 3 * H,
        closed_epoch=closed))


def test_a_fresh_unpriced_exit_is_visible_but_not_yet_stuck(store):
    _exit(store, age_h=0.5)
    st = store.unpriced_exit_status()
    assert st["count"] == 1 and st["stuck"] == [] and st["lifecycles"] == 1
    assert 0.4 * H < st["oldest_age_s"] < 0.6 * H
    assert rm.BillionaireRiskManager(store=store)._gate_unpriced_exit().ok


def test_a_stuck_one_blocks_new_entries_and_is_named(store):
    tid = _exit(store, age_h=7)
    st = store.unpriced_exit_status()
    assert st["stuck"] == [tid]
    manager = rm.BillionaireRiskManager(store=store)
    gate = manager._gate_unpriced_exit()
    assert not gate.ok and gate.reason == "UNPRICED_EXIT_STUCK"
    assert manager.gate_order(**good_order()).reason == "UNPRICED_EXIT_STUCK"


def test_an_unreadable_state_fails_closed(store, monkeypatch):
    manager = rm.BillionaireRiskManager(store=store)
    monkeypatch.setattr(store, "unpriced_exit_status",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert manager._gate_unpriced_exit().reason == "UNPRICED_EXIT_STATE_UNREADABLE"


def test_pricing_it_lifts_the_block_and_it_enters_the_statistics(store):
    tid = _exit(store, age_h=7)
    assert store.reconcile_provisional_trade(
        tid, exit_price=59_000.0, gross_pnl=10.0, exit_fee=0.1,
        exit_fee_realized=True, exit_fee_currency="USDT", evidence={})
    assert store.unpriced_exit_status()["count"] == 0
    assert rm.BillionaireRiskManager(store=store)._gate_unpriced_exit().ok
    assert len(store.net_returns()) == 1


def test_a_human_acknowledgement_unblocks_but_never_invents_a_price(store):
    tid = _exit(store, age_h=7)
    with pytest.raises(PersistenceError):
        store.acknowledge_unpriced_exit(tid, "yes")
    assert store.acknowledge_unpriced_exit(
        tid, StateStore.UNPRICED_ACK, note="venue history purged")
    st = store.unpriced_exit_status()
    assert st["stuck"] == [] and st["reviewed"] == [tid] and st["count"] == 1
    assert rm.BillionaireRiskManager(store=store)._gate_unpriced_exit().ok
    assert len(store.provisional_trades()) == 1          # still unpriced
    assert store.net_returns() == []                     # still no statistic


def test_acknowledging_a_priced_or_unknown_row_does_nothing(store):
    tid = _exit(store, age_h=7, provisional=False)
    assert store.acknowledge_unpriced_exit(tid, StateStore.UNPRICED_ACK) is False
    assert store.acknowledge_unpriced_exit(9999, StateStore.UNPRICED_ACK) is False


def test_the_stuck_alarm_journals_once_per_row(store):
    tid = _exit(store, age_h=7)
    assert store.mark_unpriced_alarm(tid) is True
    assert store.mark_unpriced_alarm(tid) is False
    rows = store._query("SELECT * FROM decisions WHERE decision = "
                        "'UNPRICED_EXIT_STUCK'")
    assert len(rows) == 1


def test_the_analytics_say_what_they_leave_out(store):
    _exit(store, age_h=20, provisional=False)
    _exit(store, age_h=7, opened=utc_now_epoch() - 30 * H)
    rep = PerformanceAnalytics(store).report()
    assert rep.unpriced_exits == 1 and rep.unpriced_stuck == 1
    assert "UNPRICED" in rep.summary() and "STUCK" in rep.summary()
    assert any("no authoritative price" in n for n in rep.notes)


def test_the_analytics_with_only_an_unpriced_exit_still_say_so(store):
    _exit(store, age_h=7)
    rep = PerformanceAnalytics(store).report()
    assert rep.trades == 0 and rep.unpriced_exits == 1
    assert any("no authoritative price" in n for n in rep.notes)


def test_the_health_endpoint_is_unhealthy_and_names_it(store):
    import main as app
    tid = _exit(store, age_h=7)
    bot = types.SimpleNamespace(
        store=store, _reconciled=True, started_epoch=0, cfg=types.SimpleNamespace(),
        observation_status="ok", loop_interval_s=60.0, _tick_failures=0,
        _last_tick_ok_epoch=0.0, HEALTH_MAX_TICK_FAILURES=5, HEALTH_STALL_INTERVALS=3,
        carry_status=lambda: {}, _loop_stalled=lambda: False,
        _risk_halted=lambda: False, _memory_health=lambda: {},
        _policy_health=lambda: {})
    payload = app.TradingBot.health(bot)
    assert payload["healthy"] is False
    assert payload["unpriced_exits"]["stuck"] == [tid]
    store.acknowledge_unpriced_exit(tid, StateStore.UNPRICED_ACK)
    again = app.TradingBot.health(bot)
    assert again["healthy"] is True and again["unpriced_exits"]["reviewed"] == [tid]


def test_the_loop_alarm_marks_each_stuck_row_and_ignores_fresh_ones(store):
    import main as app
    stuck = _exit(store, age_h=7)
    fresh = _exit(store, age_h=0.2, opened=utc_now_epoch() - 50 * H)
    bot = types.SimpleNamespace(
        store=store, UNPRICED_ALARM_EVERY_S=600.0, _last_unpriced_alarm=0.0)
    app.TradingBot._alarm_unpriced_exits(bot)
    metas = {r["id"]: r["meta"] for r in store.provisional_trades()}
    assert "stuck_alarm_epoch" in metas[stuck]
    assert "stuck_alarm_epoch" not in metas[fresh]


def test_the_operator_report_carries_the_unpriced_exits(store):
    from tools.carry_report import build_report
    _exit(store, age_h=7)
    rep = build_report(store.path)["ordinary_trades"]
    assert rep["price_provisional_trades"] == 1
    assert rep["price_provisional_oldest_age_s"] > 6 * H
    assert rep["price_provisional_reviewed"] == 0


def test_unbooked_funding_is_named_in_the_analytics_and_the_health_payload(store):
    import json as _json
    store.record_trade(TradeRecord(
        symbol="BTCUSDT", side="Sell", qty=0.01, entry_price=60_000.0,
        exit_price=59_000.0, gross_pnl=10.0, entry_fee=0.1, exit_fee=0.1,
        opened_epoch=1000.0, closed_epoch=2000.0,
        meta={"funding_booked": False,
              "funding_status": "FUNDING_SIGN_UNVERIFIED"}))
    assert store.funding_unbooked_summary() == {
        "count": 1, "statuses": {"FUNDING_SIGN_UNVERIFIED": 1}}
    rep = PerformanceAnalytics(store).report()
    assert any("NO funding booked" in n for n in rep.notes)
    import main as app
    bot = types.SimpleNamespace(
        store=store, _reconciled=True, started_epoch=0, cfg=types.SimpleNamespace(),
        observation_status="ok", loop_interval_s=60.0, _tick_failures=0,
        _last_tick_ok_epoch=0.0, HEALTH_MAX_TICK_FAILURES=5, HEALTH_STALL_INTERVALS=3,
        carry_status=lambda: {}, _loop_stalled=lambda: False,
        _risk_halted=lambda: False, _memory_health=lambda: {},
        _policy_health=lambda: {})
    payload = app.TradingBot.health(bot)
    assert payload["funding_unbooked"]["count"] == 1
    assert "partially_open_after_exit" in payload
    _json.dumps(payload)                      # servable as the health body


def test_the_operator_report_names_unbooked_funding_and_half_closed_positions(store):
    from tools.carry_report import build_report
    store.record_trade(TradeRecord(
        symbol="BTCUSDT", side="Sell", qty=0.004, entry_price=60_000.0,
        exit_price=59_000.0, gross_pnl=4.0, entry_fee=0.0, exit_fee=0.0,
        opened_epoch=1000.0, closed_epoch=2000.0,
        meta={"funding_booked": False, "funding_status": "EVIDENCE_INCOMPLETE"}))
    store._exec("INSERT INTO positions(symbol, side, qty, entry_price, stop_price,"
                " opened_epoch) VALUES('BTCUSDT','Sell',0.006,60000,61000,1000.0)")
    rep = build_report(store.path)["ordinary_trades"]
    assert rep["funding_unbooked_by_status"] == {"EVIDENCE_INCOMPLETE": 1}
    assert rep["positions_open_after_an_exit"] == 1
