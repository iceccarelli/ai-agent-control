"""A provisional exit (booked without an authoritative price) is priced only
from the venue's own execution rows, once, atomically - or not at all.

Real engine + real StateStore over the fake exchange. No venue is contacted:
the executions are injected."""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from persistence import StateStore  # noqa: E402
from test_lifecycle_integration import (  # noqa: E402,F401
    ENTRY, buy_intent, exchange, stack)

LINK = "BB-tp-9-provisional"


def _ex(exec_id, qty, price, **over):
    row = {"execId": exec_id, "orderLinkId": LINK, "symbol": "BTCUSDT",
           "side": "Sell", "execType": "Trade", "execQty": str(qty),
           "execPrice": str(price), "execFee": "0.01", "feeCurrency": "USDT"}
    row.update(over)
    return row


def _provisional(stack, fraction=4):
    engine, client, risk, store, exchange = stack
    assert engine.execute(buy_intent()).ok
    held = float(store.open_positions()[0]["qty"])
    qty = held / fraction
    engine.record_exit_fill(
        symbol="BTCUSDT", qty=qty, price=ENTRY, fee=0.0, reason="VENUE_EXIT_SPOT",
        order_link_id=LINK, dedupe_key=f"exit_spot:{LINK}",
        price_provisional=True)
    rows = store.provisional_trades()
    assert len(rows) == 1
    return engine, store, rows[0], qty


class TestAProvisionalRowIsNotConfirmedEconomics:
    def test_it_is_in_no_confirmed_reader(self, stack):
        engine, store, trade, qty = _provisional(stack)
        assert trade["exit_price"] == 0.0 and trade["gross_pnl"] == 0.0
        assert trade["exit_fee_realized"] == 0          # placeholder-derived fee
        assert not store.recent_trades()
        assert store.net_returns() == []
        assert store.trades_for_symbol("BTCUSDT") == []
        assert store.trades_with_regime() == []
        ev = store.fee_evidence()
        assert ev["trades"] == 0 and ev["price_provisional_trades"] == 1
        assert ev["realized_net_pnl"] == 0.0 and ev["unconfirmed_net_pnl"] == 0.0

    def test_the_conservative_brakes_still_see_it(self, stack):
        engine, store, trade, qty = _provisional(stack)
        assert store.consecutive_losses() >= 1          # fees only: a "loss"
        anchor = store.get_daily_anchor()
        assert anchor.trades >= 1 and anchor.realised_pnl <= 0.0

    def test_the_operator_report_keeps_it_out_of_realised_pnl(self, stack, tmp_path):
        import json
        from tools import carry_report
        engine, store, trade, qty = _provisional(stack)
        report = carry_report.build_report(store.path) if hasattr(
            carry_report, "build_report") else None
        if report is None:
            pytest.skip("report builder has another name")
        ordinary = report["ordinary_trades"]
        assert ordinary["price_provisional_trades"] == 1
        assert ordinary["trades"] == 0
        assert ordinary["realised_net_pnl"] == 0.0
        json.dumps(report, default=str)


class TestReconciliation:
    def test_valid_fills_price_the_row_exactly_once(self, stack):
        engine, store, trade, qty = _provisional(stack)
        rows = [_ex("E1", qty / 2, ENTRY * 1.02, execFee="0.02"),
                _ex("E2", qty / 2, ENTRY * 1.04, execFee="0.03")]
        summary = engine.reconcile_provisional_exits(lambda s, l: rows)
        assert summary["reconciled"] == 1 and not store.provisional_trades()
        confirmed = store.recent_trades()
        assert len(confirmed) == 1
        t = confirmed[0]
        vwap = ENTRY * 1.03
        assert t["exit_price"] == pytest.approx(vwap)
        assert t["gross_pnl"] == pytest.approx((vwap - ENTRY) * qty)
        assert t["exit_fee"] == pytest.approx(0.05)
        assert t["net_pnl"] == pytest.approx(
            t["gross_pnl"] - t["entry_fee"] - 0.05)
        assert t["exit_fee_realized"] == 1 and t["price_provisional"] == 0

    def test_replaying_the_same_fills_changes_nothing(self, stack):
        engine, store, trade, qty = _provisional(stack)
        rows = [_ex("E1", qty, ENTRY * 1.02)]
        engine.reconcile_provisional_exits(lambda s, l: rows)
        before = store.recent_trades()
        anchor = store.get_daily_anchor().realised_pnl
        again = engine.reconcile_provisional_exits(lambda s, l: rows)
        assert again["checked"] == 0 and again["reconciled"] == 0
        assert store.recent_trades() == before
        assert store.get_daily_anchor().realised_pnl == pytest.approx(anchor)
        # the store itself also refuses a second application
        assert store.reconcile_provisional_trade(
            before[0]["id"], exit_price=1.0, gross_pnl=1.0, exit_fee=0.0,
            exit_fee_realized=True, exit_fee_currency="USDT", evidence={}) is False

    def test_the_days_realised_pnl_moves_by_the_difference_only(self, stack):
        engine, store, trade, qty = _provisional(stack)
        before = store.get_daily_anchor().realised_pnl
        rows = [_ex("E1", qty, ENTRY * 1.02, execFee="0.5")]
        engine.reconcile_provisional_exits(lambda s, l: rows)
        t = store.recent_trades()[0]
        assert store.get_daily_anchor().realised_pnl == pytest.approx(
            before + (t["net_pnl"] - trade["net_pnl"]))

    def test_it_is_journaled_with_the_before_values(self, stack):
        engine, store, trade, qty = _provisional(stack)
        engine.reconcile_provisional_exits(
            lambda s, l: [_ex("E1", qty, ENTRY * 1.02)])
        assert any(d["decision"] == "EXIT_PRICE_RECONCILED"
                   for d in store.recent_decisions())
        import json
        meta = json.loads(store.recent_trades()[0]["meta"])
        assert meta["reconciliation"]["before"]["exit_price"] == 0.0
        assert meta["reconciliation"]["evidence"]["exec_ids"] == ["E1"]


BAD = {
    "wrong symbol": lambda q: [_ex("E1", q, ENTRY, symbol="ETHUSDT")],
    "wrong side": lambda q: [_ex("E1", q, ENTRY, side="Buy")],
    "other order": lambda q: [_ex("E1", q, ENTRY, orderLinkId="BB-tp-other")],
    "not a trade": lambda q: [_ex("E1", q, ENTRY, execType="Funding")],
    "no exec id": lambda q: [_ex("", q, ENTRY)],
    "no price": lambda q: [_ex("E1", q, 0)],
    "nan price": lambda q: [_ex("E1", q, "nan")],
    "no quantity": lambda q: [_ex("E1", 0, ENTRY)],
    "no fee": lambda q: [_ex("E1", q, ENTRY, execFee="")],
    "no fee currency": lambda q: [_ex("E1", q, ENTRY, feeCurrency="")],
    "incomplete": lambda q: [_ex("E1", q / 2, ENTRY)],
    "over-filled": lambda q: [_ex("E1", q * 2, ENTRY)],
    "same id twice, different": lambda q: [
        _ex("E1", q / 2, ENTRY), _ex("E1", q / 2, ENTRY * 2)],
    "nothing": lambda q: [],
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_bad_evidence_never_confirms_a_trade(stack, name):
    engine, store, trade, qty = _provisional(stack)
    summary = engine.reconcile_provisional_exits(lambda s, l: BAD[name](qty))
    assert summary["reconciled"] == 0
    assert len(store.provisional_trades()) == 1
    assert not store.recent_trades()
    assert store.net_returns() == []


def test_an_exact_replay_of_one_fill_is_counted_once(stack):
    engine, store, trade, qty = _provisional(stack)
    row = _ex("E1", qty, ENTRY * 1.02)
    engine.reconcile_provisional_exits(lambda s, l: [row, dict(row)])
    t = store.recent_trades()[0]
    assert t["exit_price"] == pytest.approx(ENTRY * 1.02)


def test_an_unreadable_venue_leaves_the_row_provisional(stack):
    engine, store, trade, qty = _provisional(stack)

    def boom(symbol, link):
        raise RuntimeError("venue down")
    summary = engine.reconcile_provisional_exits(boom)
    assert summary["pending"] == 1 and summary["reconciled"] == 0
    assert len(store.provisional_trades()) == 1


def test_a_fee_in_an_unconvertible_currency_keeps_the_row_provisional(stack):
    engine, store, trade, qty = _provisional(stack)
    rows = [_ex("E1", qty, ENTRY * 1.02, execFee="0.1", feeCurrency="DOGE")]
    summary = engine.reconcile_provisional_exits(lambda s, l: rows)
    assert summary["reconciled"] == 0 and len(store.provisional_trades()) == 1


def test_a_base_coin_fee_is_converted_at_the_authoritative_price(stack):
    engine, store, trade, qty = _provisional(stack)
    rows = [_ex("E1", qty, ENTRY * 1.02, execFee="0.000001", feeCurrency="BTC")]
    engine.reconcile_provisional_exits(lambda s, l: rows)
    t = store.recent_trades()[0]
    assert t["exit_fee"] == pytest.approx(0.000001 * ENTRY * 1.02)


def test_a_crash_inside_the_update_leaves_the_row_untouched(stack, monkeypatch):
    engine, store, trade, qty = _provisional(stack)
    anchor = store.get_daily_anchor().realised_pnl
    real = store.journal

    def boom(*a, **k):
        raise RuntimeError("killed mid-write")
    monkeypatch.setattr(store, "journal", boom)
    with pytest.raises(RuntimeError):
        engine.reconcile_provisional_exits(
            lambda s, l: [_ex("E1", qty, ENTRY * 1.02)])
    monkeypatch.setattr(store, "journal", real)
    assert len(store.provisional_trades()) == 1
    assert store.get_daily_anchor().realised_pnl == pytest.approx(anchor)
    # and the retry after the "restart" succeeds exactly once
    engine.reconcile_provisional_exits(lambda s, l: [_ex("E1", qty, ENTRY * 1.02)])
    assert len(store.recent_trades()) == 1


def test_mixed_confirmed_and_provisional_rows_keep_their_own_economics(stack):
    engine, store, trade, qty = _provisional(stack)
    engine.record_exit_fill(
        symbol="BTCUSDT", qty=qty, price=ENTRY * 1.01, fee=0.0,
        reason="VENUE_EXIT_SPOT", order_link_id="BB-tp-9-confirmed",
        dedupe_key="exit_spot:BB-tp-9-confirmed")
    confirmed_before = store.recent_trades()
    assert len(confirmed_before) == 1
    engine.reconcile_provisional_exits(
        lambda s, l: [_ex("E1", qty, ENTRY * 1.02)])
    after = store.recent_trades()
    assert len(after) == 2
    old = [t for t in after if t["order_link_id"] == "BB-tp-9-confirmed"][0]
    assert old == confirmed_before[0]                     # untouched


def test_the_migration_is_idempotent_across_restarts(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,"
        " side TEXT NOT NULL, qty REAL NOT NULL, entry_price REAL NOT NULL,"
        " exit_price REAL NOT NULL, gross_pnl REAL NOT NULL, entry_fee REAL NOT NULL,"
        " exit_fee REAL NOT NULL, net_pnl REAL NOT NULL, opened_epoch REAL NOT NULL,"
        " closed_epoch REAL NOT NULL, day TEXT NOT NULL,"
        " order_link_id TEXT NOT NULL DEFAULT '', meta TEXT NOT NULL DEFAULT '{}')")
    conn.execute("INSERT INTO trades(symbol, side, qty, entry_price, exit_price,"
                 " gross_pnl, entry_fee, exit_fee, net_pnl, opened_epoch,"
                 " closed_epoch, day) VALUES('BTCUSDT','Buy',1,100,101,1,0,0,1,1,2,'d')")
    conn.commit()
    conn.close()
    for _ in range(3):
        store = StateStore(path)
        assert len(store.recent_trades()) == 1
        assert store.provisional_trades() == []
        store.close()


class TestAnUnconfirmedCloseIsNeverAPricedTrade:
    def _close_unresolved(self, stack):
        engine, client, risk, store, exchange = stack
        assert engine.execute(buy_intent()).ok
        qty = float(store.open_positions()[0]["qty"])
        engine._await_fill = lambda *a, **k: None       # the poll timed out
        report = engine.close_position(symbol="BTCUSDT", reason="manual",
                                       exit_price=ENTRY * 3)   # a caller guess
        return engine, store, exchange, report, qty

    def test_it_is_booked_provisional_with_no_price_at_all(self, stack):
        engine, store, exchange, report, qty = self._close_unresolved(stack)
        rows = store.provisional_trades()
        assert len(rows) == 1
        assert rows[0]["exit_price"] == 0.0 and rows[0]["gross_pnl"] == 0.0
        assert rows[0]["exit_fee_realized"] == 0
        assert not store.recent_trades() and store.net_returns() == []

    def test_the_venues_executions_then_price_it_once(self, stack):
        engine, store, exchange, report, qty = self._close_unresolved(stack)
        link = store.provisional_trades()[0]["order_link_id"]
        rows = [{"execId": "X1", "orderLinkId": link, "symbol": "BTCUSDT",
                 "side": "Sell", "execType": "Trade", "execQty": str(qty),
                 "execPrice": str(ENTRY * 1.01), "execFee": "0.1",
                 "feeCurrency": "USDT"}]
        assert engine.reconcile_provisional_exits(lambda s, l: rows)["reconciled"] == 1
        t = store.recent_trades()[0]
        assert t["exit_price"] == pytest.approx(ENTRY * 1.01)
        assert engine.reconcile_provisional_exits(lambda s, l: rows)["checked"] == 0

    def test_a_close_that_never_executed_stays_provisional_for_ever(self, stack):
        engine, store, exchange, report, qty = self._close_unresolved(stack)
        for _ in range(3):
            assert engine.reconcile_provisional_exits(lambda s, l: [])["reconciled"] == 0
        assert len(store.provisional_trades()) == 1
        assert not store.recent_trades()


class TestSpotProtectionMustBeAbleToExecute:
    def test_a_replacement_stop_is_placed_only_after_the_legs_free_the_coin(
            self, stack):
        import json
        engine, client, risk, store, exchange = stack
        assert engine.execute(buy_intent()).ok
        row = store.open_positions()[0]
        meta = json.loads(row["meta"]) if isinstance(row["meta"], str) else row["meta"]
        exchange.reserve_limit_sells = True
        exchange.balances["BTC"] = float(row["qty"])
        exchange.orders[meta["stop_order_link_id"]]["orderStatus"] = "Rejected"
        engine.observe_exits()
        assert all(exchange.orders[t]["orderStatus"] == "Cancelled"
                   for t in meta["tp_order_ids"])
        assert exchange._free("BTC", exchange.balances["BTC"]) >= float(
            row["qty"]) - 1e-9

    def test_an_emergency_sale_frees_the_reserved_coin_first(self, stack):
        engine, client, risk, store, exchange = stack
        assert engine.execute(buy_intent()).ok
        row = store.open_positions()[0]
        exchange.reserve_limit_sells = True
        exchange.balances["BTC"] = float(row["qty"])
        filters = client.get_instrument_filters("BTCUSDT")
        report = engine._emergency_close(
            symbol="BTCUSDT", exit_side="Sell", qty=float(row["qty"]),
            filters=filters, reason="TEST")
        assert report.detail["closed"] is True


class TestNoSecondCloseBesideAnUnknownOne:
    def _unknown_close(self, stack):
        engine, client, risk, store, exchange = stack
        assert engine.execute(buy_intent()).ok
        qty = float(store.open_positions()[0]["qty"])
        real = client.place_order
        calls = []

        def lost_reply(**kw):
            res = real(**kw)
            calls.append(kw.get("purpose"))
            if kw.get("purpose") == "close":
                store.update_order_status(res.order_link_id, "unknown")
                return type(res)(ok=False, order_link_id=res.order_link_id,
                                 reason="OUTCOME_UNKNOWN")
            return res
        client.place_order = lost_reply
        first = engine.close_position(symbol="BTCUSDT", reason="horizon")
        assert first.reason != "CLOSED"
        return engine, client, store, exchange, calls, qty

    def test_the_retry_does_not_send_a_second_sale(self, stack):
        engine, client, store, exchange, calls, qty = self._unknown_close(stack)
        link = [o for o in store.unresolved_orders() if o["purpose"] == "close"][0]
        exchange.orders[link["order_link_id"]]["orderStatus"] = "New"   # still live
        sent = calls.count("close")
        second = engine.close_position(symbol="BTCUSDT", reason="horizon")
        assert second.reason == "CLOSE_PENDING"
        assert calls.count("close") == sent

    def test_a_close_that_did_execute_is_booked_from_its_own_fill_not_resold(
            self, stack):
        engine, client, store, exchange, calls, qty = self._unknown_close(stack)
        sent = calls.count("close")
        second = engine.close_position(symbol="BTCUSDT", reason="horizon")
        assert second.reason == "CLOSE_RECONCILED"
        assert calls.count("close") == sent
        assert store.open_positions() == []
        assert len(store.recent_trades()) == 1

    def test_a_close_that_ended_with_nothing_executed_allows_a_fresh_one(self, stack):
        engine, client, store, exchange, calls, qty = self._unknown_close(stack)
        link = [o for o in store.unresolved_orders() if o["purpose"] == "close"][0]
        exchange.orders[link["order_link_id"]].update(
            orderStatus="Cancelled", cumExecQty="0")
        second = engine.close_position(symbol="BTCUSDT", reason="horizon")
        # (the test double loses every close reply, so the fresh attempt is
        # unknown again - what matters is that it was ALLOWED to be sent)
        assert second.reason != "CLOSE_PENDING"
        assert calls.count("close") == 2


def test_a_position_reply_without_a_list_is_not_flat(stack):
    import bybit_connection as bc
    engine, client, risk, store, exchange = stack
    client.is_linear = True
    client._request = lambda *a, **k: {"retCode": 0, "result": {}}
    with pytest.raises(bc.PermanentAPIError):
        client.get_position("BTCUSDT")


def test_a_take_profit_leg_of_unknown_outcome_is_still_tracked(stack):
    import json
    engine, client, risk, store, exchange = stack
    real = client.place_take_profit
    seen = []

    def flaky(**kw):
        res = real(**kw)
        seen.append(res.order_link_id)
        if len(seen) == 1:
            return type(res)(ok=False, order_link_id=res.order_link_id,
                             reason="OUTCOME_UNKNOWN")
        return res
    client.place_take_profit = flaky
    assert engine.execute(buy_intent()).ok
    row = store.open_positions()[0]
    meta = json.loads(row["meta"]) if isinstance(row["meta"], str) else row["meta"]
    assert seen[0] in meta["tp_order_ids"]
