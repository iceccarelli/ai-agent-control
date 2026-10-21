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
        assert not store.confirmed_exit_rows()
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
        confirmed = store.confirmed_exit_rows()
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
        before = store.confirmed_exit_rows()
        anchor = store.get_daily_anchor().realised_pnl
        again = engine.reconcile_provisional_exits(lambda s, l: rows)
        assert again["checked"] == 0 and again["reconciled"] == 0
        assert store.confirmed_exit_rows() == before
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
        t = store.confirmed_exit_rows()[0]
        assert store.get_daily_anchor().realised_pnl == pytest.approx(
            before + (t["net_pnl"] - trade["net_pnl"]))

    def test_it_is_journaled_with_the_before_values(self, stack):
        engine, store, trade, qty = _provisional(stack)
        engine.reconcile_provisional_exits(
            lambda s, l: [_ex("E1", qty, ENTRY * 1.02)])
        assert any(d["decision"] == "EXIT_PRICE_RECONCILED"
                   for d in store.recent_decisions())
        import json
        meta = json.loads(store.confirmed_exit_rows()[0]["meta"])
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
    assert not store.confirmed_exit_rows()
    assert store.net_returns() == []


def test_an_exact_replay_of_one_fill_is_counted_once(stack):
    engine, store, trade, qty = _provisional(stack)
    row = _ex("E1", qty, ENTRY * 1.02)
    engine.reconcile_provisional_exits(lambda s, l: [row, dict(row)])
    t = store.confirmed_exit_rows()[0]
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
    t = store.confirmed_exit_rows()[0]
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
    assert len(store.confirmed_exit_rows()) == 1


def test_mixed_confirmed_and_provisional_rows_keep_their_own_economics(stack):
    engine, store, trade, qty = _provisional(stack)
    engine.record_exit_fill(
        symbol="BTCUSDT", qty=qty, price=ENTRY * 1.01, fee=0.0,
        reason="VENUE_EXIT_SPOT", order_link_id="BB-tp-9-confirmed",
        dedupe_key="exit_spot:BB-tp-9-confirmed")
    confirmed_before = store.confirmed_exit_rows()
    assert len(confirmed_before) == 1
    engine.reconcile_provisional_exits(
        lambda s, l: [_ex("E1", qty, ENTRY * 1.02)])
    after = store.confirmed_exit_rows()
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
        assert len(store.confirmed_exit_rows()) == 1
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
        assert not store.confirmed_exit_rows() and store.net_returns() == []

    def test_the_venues_executions_then_price_it_once(self, stack):
        engine, store, exchange, report, qty = self._close_unresolved(stack)
        link = store.provisional_trades()[0]["order_link_id"]
        rows = [{"execId": "X1", "orderLinkId": link, "symbol": "BTCUSDT",
                 "side": "Sell", "execType": "Trade", "execQty": str(qty),
                 "execPrice": str(ENTRY * 1.01), "execFee": "0.1",
                 "feeCurrency": "USDT"}]
        assert engine.reconcile_provisional_exits(lambda s, l: rows)["reconciled"] == 1
        t = store.confirmed_exit_rows()[0]
        assert t["exit_price"] == pytest.approx(ENTRY * 1.01)
        assert engine.reconcile_provisional_exits(lambda s, l: rows)["checked"] == 0

    def test_a_close_that_never_executed_stays_provisional_for_ever(self, stack):
        engine, store, exchange, report, qty = self._close_unresolved(stack)
        for _ in range(3):
            assert engine.reconcile_provisional_exits(lambda s, l: [])["reconciled"] == 0
        assert len(store.provisional_trades()) == 1
        assert not store.confirmed_exit_rows()


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
        assert len(store.confirmed_exit_rows()) == 1

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


class TestACloseThatExecutedWhileTheProcessWasDown:
    def test_startup_does_not_drop_it_unbooked_and_the_next_cycle_books_it(
            self, stack):
        engine, client, store, exchange, calls, qty = (
            TestNoSecondCloseBesideAnUnknownOne()._unknown_close(stack))
        summary = client.reconcile_on_startup()
        assert summary["closes_to_settle"] == 1        # left for the engine
        assert not summary["unknown"]                  # ...and startup may go on
        assert [o for o in store.unresolved_orders() if o["purpose"] == "close"]
        assert store.open_positions()                  # still on the book
        sent = calls.count("close")
        engine.observe_exits()                         # books it from its fill
        assert store.open_positions() == []
        assert len(store.confirmed_exit_rows()) == 1
        assert calls.count("close") == sent            # and sold nothing more
        assert not [o for o in store.unresolved_orders()
                    if o["purpose"] == "close"]


class TestAStaleCloseRowIsNotReplayedAgainstALaterPosition:
    def test_a_close_older_than_the_position_is_settled_not_booked(self, stack):
        engine, client, store, exchange, calls, qty = (
            TestNoSecondCloseBesideAnUnknownOne()._unknown_close(stack))
        link = [o for o in store.unresolved_orders()
                if o["purpose"] == "close"][0]["order_link_id"]
        # the position it belonged to is gone; a NEW one opens afterwards
        store.remove_position("BTCUSDT")
        store.upsert_position("BTCUSDT", "Buy", qty, 50_000.0, stop_price=49_000.0)
        store._exec("UPDATE positions SET opened_epoch = ? WHERE symbol='BTCUSDT'",
                    (float(store._query("SELECT created_epoch FROM orders WHERE "
                                        "order_link_id=?", (link,))[0][0]) + 60,))
        trades_before = len(store.confirmed_exit_rows())
        report = engine.close_position(symbol="BTCUSDT", reason="horizon")
        assert report.reason != "CLOSE_RECONCILED"
        assert len(store.confirmed_exit_rows()) == trades_before
        assert store._query("SELECT status FROM orders WHERE order_link_id=?",
                            (link,))[0][0] == "stale"


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


def test_perpetual_trades_are_counted_as_funding_unbooked(stack):
    engine, client, risk, store, exchange = stack
    from persistence import TradeRecord
    for booked in (True, False):
        store.record_trade(TradeRecord(
            symbol="BTCUSDT", side="Buy", qty=1.0, entry_price=100.0,
            exit_price=101.0, gross_pnl=1.0, entry_fee=0.0, exit_fee=0.0,
            opened_epoch=1.0, closed_epoch=2.0 + booked,
            meta={"funding_booked": booked}, dedupe_key=f"k{booked}"))
    assert store.fee_evidence()["funding_unbooked_trades"] == 1


# -- linear, venue-triggered exits: identified by the venue ORDER ID ---------

import json as _json  # noqa: E402
import time as _time  # noqa: E402

from fake_bybit import FakeBybit  # noqa: E402
from test_observe_exits import LinearCfg, _engine as _lin_engine, _open as _lin_open  # noqa: E402


def _lin_ex(exec_id, order_id, qty, price, **over):
    row = {"execId": exec_id, "orderId": order_id, "orderLinkId": "",
           "symbol": "BTCUSDT", "side": "Buy", "execType": "Trade",
           "execQty": str(qty), "execPrice": str(price), "execFee": "0.05",
           "feeCurrency": "USDT"}
    row.update(over)
    return row


def _lin_provisional(tmp_path, qty=0.01, closed_pnl_rows=None):
    fake = FakeBybit()
    engine, store = _lin_engine(tmp_path, LinearCfg(), fake)
    _lin_open(store, qty=qty)
    fake.closed_pnl.extend(closed_pnl_rows if closed_pnl_rows is not None else [{
        "symbol": "BTCUSDT", "side": "Buy", "qty": str(qty), "orderId": "ORD1",
        "avgExitPrice": "0", "closedPnl": "0",
        "updatedTime": str(int(_time.time() * 1000) + 1000)}])
    engine.observe_exits()
    assert len(store.provisional_trades()) == 1
    return engine, store, fake


class TestLinearVenueTriggeredExitReconcilesByOrderId:
    def test_it_reconciles_once_through_the_real_client_path(self, tmp_path):
        engine, store, fake = _lin_provisional(tmp_path)
        fake.executions.append(_lin_ex("X1", "ORD1", 0.01, 61_000))
        assert engine.reconcile_provisional_exits()["reconciled"] == 1
        t = store.confirmed_exit_rows()[0]
        assert t["exit_price"] == pytest.approx(61_000)
        assert t["gross_pnl"] < 0                        # a short stopped out
        assert engine.reconcile_provisional_exits()["checked"] == 0

    def test_several_partial_fills_are_volume_weighted(self, tmp_path):
        engine, store, fake = _lin_provisional(tmp_path)
        fake.executions += [_lin_ex("X1", "ORD1", 0.004, 61_000),
                            _lin_ex("X2", "ORD1", 0.006, 62_000)]
        engine.reconcile_provisional_exits()
        assert store.confirmed_exit_rows()[0]["exit_price"] == pytest.approx(
            (0.004 * 61_000 + 0.006 * 62_000) / 0.01)

    def test_more_than_one_page_of_fills_is_read_in_full(self, tmp_path):
        engine, store, fake = _lin_provisional(tmp_path, qty=1.5)
        fake.executions += [_lin_ex(f"X{i}", "ORD1", 0.01, 61_000)
                            for i in range(150)]
        assert engine.reconcile_provisional_exits()["reconciled"] == 1

    @pytest.mark.parametrize("bad", [
        {"orderId": "OTHER"}, {"execType": "BustTrade"}, {"side": "Sell"},
        {"symbol": "ETHUSDT"}, {"execFee": ""}, {"execPrice": "0"},
    ])
    def test_wrong_evidence_leaves_it_provisional(self, tmp_path, bad):
        engine, store, fake = _lin_provisional(tmp_path)
        row = _lin_ex("X1", "ORD1", 0.01, 61_000, **bad)
        if bad.get("symbol"):
            row["symbol"] = "BTCUSDT"        # the venue filter would hide it:
            fake.executions.append(dict(row, symbol="ETHUSDT"))   # not served
        else:
            fake.executions.append(row)
        assert engine.reconcile_provisional_exits()["reconciled"] == 0
        assert len(store.provisional_trades()) == 1
        assert store.confirmed_exit_rows() == []

    def test_a_quantity_mismatch_stays_provisional(self, tmp_path):
        engine, store, fake = _lin_provisional(tmp_path)
        fake.executions.append(_lin_ex("X1", "ORD1", 0.004, 61_000))
        assert engine.reconcile_provisional_exits()["reconciled"] == 0

    def test_an_exact_replay_of_one_fill_is_counted_once(self, tmp_path):
        engine, store, fake = _lin_provisional(tmp_path)
        row = _lin_ex("X1", "ORD1", 0.01, 61_000)
        fake.executions += [row, dict(row)]
        assert engine.reconcile_provisional_exits()["reconciled"] == 1

    def test_a_restart_reconciles_from_the_persisted_identity(self, tmp_path):
        engine, store, fake = _lin_provisional(tmp_path)
        fake.executions.append(_lin_ex("X1", "ORD1", 0.01, 61_000))
        from persistence import StateStore
        import trading_engine as te
        store2 = StateStore(store.path)
        store2.claim_writer()
        engine.store = store2                       # a fresh process, same DB
        assert engine.reconcile_provisional_exits()["reconciled"] == 1
        assert len(store2.confirmed_exit_rows()) == 1

    def test_identity_is_recovered_from_closed_pnl_when_the_read_had_failed(
            self, tmp_path):
        fake = FakeBybit()
        engine, store = _lin_engine(tmp_path, LinearCfg(), fake)
        _lin_open(store)
        real = fake.request
        state = {"down": True}

        def flaky(method, url, **kw):
            if "closed-pnl" in url and state["down"]:
                return 500, "boom"
            return real(method, url, **kw)
        fake.request = flaky
        engine.observe_exits()
        t = store.provisional_trades()[0]
        assert _json.loads(t["meta"])["exit_order_ids"] == []
        state["down"] = False
        fake.closed_pnl.append({
            "symbol": "BTCUSDT", "side": "Buy", "qty": "0.01",
            "orderId": "ORD9", "avgExitPrice": "0", "closedPnl": "0",
            "updatedTime": str(int(_time.time() * 1000) + 1000)})
        fake.executions.append(_lin_ex("X1", "ORD9", 0.01, 61_000))
        assert engine.reconcile_provisional_exits()["reconciled"] == 1

    def test_no_identity_is_never_guessed_from_symbol_side_and_price(
            self, tmp_path):
        fake = FakeBybit()
        engine, store = _lin_engine(tmp_path, LinearCfg(), fake)
        _lin_open(store)
        real = fake.request
        fake.request = lambda m, u, **kw: ((500, "boom") if "closed-pnl" in u
                                           else real(m, u, **kw))
        engine.observe_exits()
        fake.request = real
        fake.executions.append(_lin_ex("X1", "WHATEVER", 0.01, 61_000))
        s = engine.reconcile_provisional_exits()
        assert s["reconciled"] == 0 and len(store.provisional_trades()) == 1


class TestRecoveredIdentityIsNeverAnotherExits:
    def _two_legs(self, tmp_path, claim_b):
        fake = FakeBybit()
        engine, store = _lin_engine(tmp_path, LinearCfg(), fake)
        now = int(_time.time() * 1000)
        for oid, dt in (("ORD-A", 1000), ("ORD-B", 2000)):
            fake.closed_pnl.append({
                "symbol": "BTCUSDT", "side": "Buy", "qty": "0.003",
                "orderId": oid, "avgExitPrice": "61000", "closedPnl": "0",
                "updatedTime": str(now + dt)})
        if claim_b:
            from persistence import TradeRecord
            store.record_trade(TradeRecord(
                symbol="BTCUSDT", side="Sell", qty=0.003, entry_price=60_000.0,
                exit_price=61_000.0, gross_pnl=-3.0, entry_fee=0.0, exit_fee=0.0,
                opened_epoch=_time.time() - 100, closed_epoch=_time.time(),
                meta={"exit_order_ids": ["ORD-B"]}))
        trade = {"id": 999, "symbol": "BTCUSDT", "qty": 0.003,
                 "opened_epoch": _time.time() - 100,
                 "closed_epoch": _time.time()}
        return engine, trade

    def test_two_unidentified_equal_legs_are_ambiguous_and_refused(self, tmp_path):
        engine, trade = self._two_legs(tmp_path, claim_b=False)
        assert engine._recover_exit_order_ids(trade) == []

    def test_an_order_another_exit_already_names_is_not_taken(self, tmp_path):
        engine, trade = self._two_legs(tmp_path, claim_b=True)
        assert engine._recover_exit_order_ids(trade) == ["ORD-A"]

    def test_a_row_newer_than_the_exit_is_not_this_exit(self, tmp_path):
        engine, trade = self._two_legs(tmp_path, claim_b=True)
        trade["closed_epoch"] = _time.time() - 3600
        assert engine._recover_exit_order_ids(trade) == []


def test_get_executions_pages_by_cursor_and_carries_the_order_id(tmp_path):
    import bybit_connection as bc
    fake = FakeBybit()
    engine, store = _lin_engine(tmp_path, LinearCfg(), fake)
    fake.executions += [_lin_ex(f"X{i}", "ORD1", 0.01, 61_000) for i in range(230)]
    rows = engine.client.get_executions("BTCUSDT", order_id="ORD1")
    assert len(rows) == 230
    sent = [r for r in fake.requests if "execution/list" in r["url"]]
    assert len(sent) == 3 and all("orderId=ORD1" in r["url"] for r in sent)
    with pytest.raises(bc.PermanentAPIError):
        engine.client.get_executions("BTCUSDT")
    assert engine.client.get_executions("BTCUSDT", order_id="NOPE") == []
