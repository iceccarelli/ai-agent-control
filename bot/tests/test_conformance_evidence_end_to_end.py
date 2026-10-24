"""Closing the evidence loop, offline: a bounded session on the fake venue,
captured through the evidence transport, then validated.

The first real testnet conformance run produced evidence that the validator
(correctly) could not reconcile: orders were captured only as they looked
mid-run (a stop still `Untriggered`, a take-profit never re-read) and no
execution row was ever read. `capture_final_venue_state` adds the missing
read-only GETs; `close_position` records the entry link, exit order and exit
execution ids on the trade. This file proves the whole chain and that, without
the final-state read, the same session stays unreconciled."""
from __future__ import annotations

import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import bybit_connection as bc  # noqa: E402
import evidence_reconcile as er  # noqa: E402
import fake_bybit  # noqa: E402
import testnet_conformance_run as tcr  # noqa: E402
import trading_engine as te  # noqa: E402
import venue_evidence as ve  # noqa: E402
from fake_bybit import FakeBybit  # noqa: E402
from persistence import StateStore  # noqa: E402
from position_sizing import BillionairePositionSizing  # noqa: E402
from risk_management import BillionaireRiskManager  # noqa: E402
from test_orchestrator import Cfg  # noqa: E402

SYMBOL = "BTCUSDT"


class SessionCfg(Cfg):
    PAPER_TRADING = False
    BYBIT_VENUE = "testnet"


class FilledOrdersCarryFees(FakeBybit):
    """A fake whose filled orders state a fee and whose executions exist from
    the moment an order fills, as on the venue."""

    def _create_order(self, body):
        result = super()._create_order(body)
        order = self.orders.get(body.get("orderLinkId"))
        if order and order["orderStatus"] == "Filled":
            order["cumFeeDetail"] = {"USDT": "0.1"}
            self.executions.append({
                "execId": f"EX-{order['orderId']}", "orderId": order["orderId"],
                "orderLinkId": order["orderLinkId"], "symbol": order["symbol"],
                "side": order["side"], "execType": "Trade",
                "execQty": order["cumExecQty"], "execPrice": order["avgPrice"],
                "execFee": "0.1", "feeCurrency": "USDT",
                "execTime": str(int(time.time() * 1000))})
        return result


@pytest.fixture()
def session(tmp_path):
    fake = FilledOrdersCarryFees()
    evidence = str(tmp_path / "evidence" / "chain.jsonl")
    transport = ve.EvidenceCapturingTransport(
        fake, evidence_path=evidence, venue="bybit", environment="testnet")
    db = str(tmp_path / "state.db")
    store = StateStore(db)
    store.claim_writer()
    store.update_equity(10_000.0)
    cfg = SessionCfg()
    client = bc.BybitClient(config=cfg, store=store, transport=transport)
    risk = BillionaireRiskManager(config=cfg, store=store)
    engine = te.TradingEngine(
        client=client, risk_manager=risk,
        position_sizer=BillionairePositionSizing(config=cfg, risk_manager=risk),
        store=store, config=cfg)
    last = fake_bybit.LAST_PRICE[SYMBOL]
    entered = engine.execute(te.TradeIntent(
        symbol=SYMBOL, signal_type="BUY", entry_price=last,
        stop_price=last * 0.98, take_profits=((last * 1.05, 1.0),),
        confidence=0.8))
    assert entered.ok
    closed = engine.close_position(symbol=SYMBOL, reason="session_flatten")
    assert closed.ok
    return {"fake": fake, "store": store, "client": client, "db": db,
            "evidence": evidence}


def validate(session, *, final_state):
    if final_state:
        result = tcr.capture_final_venue_state(
            client=session["client"], store=session["store"], symbol=SYMBOL)
        assert result["ok"], result
    session["store"].close()
    claims = er.claims_from_store(session["db"], environment="testnet",
                                  source="testnet_claim")
    capture = er.load_capture(session["evidence"])
    return er.reconcile(now_ms=int(time.time() * 1000) + 5_000,
                        captures=[capture], **claims), claims


class TestTheSessionValidatesEndToEnd:
    def test_every_order_and_the_trade_reconcile(self, session):
        report, _ = validate(session, final_state=True)
        for verdict in report["order_claims"] + report["trades"]:
            assert verdict["provenance"] == er.VENUE_RECONCILED, verdict
            assert verdict["issues"] == []
        assert report["counts_by_provenance"][er.VENUE_RECONCILED] == 5
        assert report["pnl_claim_allowed"] is True

    def test_the_trade_names_its_entry_exit_order_and_execution(self, session):
        _report, claims = validate(session, final_state=True)
        trade = claims["trade_claims"][0]
        entry = [o for o in claims["order_claims"]
                 if o["claim_id"].startswith("order:BB-entr")][0]
        close = [o for o in claims["order_claims"]
                 if o["claim_id"].startswith("order:BB-clos")][0]
        assert trade["entry_order_link_id"] == entry["order_link_id"]
        assert trade["exit_order_ids"] == [close["exchange_id"]]
        assert trade["exit_exec_ids"] == [f"EX-{close['exchange_id']}"]

    def test_without_the_final_state_read_the_same_session_is_unreconciled(self, session):
        """This is the first real run's outcome, reproduced offline."""
        report, _ = validate(session, final_state=False)
        issues = {i for v in report["order_claims"] for i in v["issues"]}
        assert "NO_FINAL_VENUE_STATE" in issues      # the stop, read only mid-run
        assert "NOT_IN_CAPTURE" in issues            # the take-profit, never read
        assert "NO_CORROBORATION" in issues          # fills, never read
        assert report["pnl_claim_allowed"] is False

    def test_a_cancelled_conditional_order_reported_deactivated_agrees(self, session):
        session["fake"].orders[
            [o for o in session["fake"].orders if "-stop-" in o][0]
        ]["orderStatus"] = "Deactivated"
        report, _ = validate(session, final_state=True)
        stop = [v for v in report["order_claims"] if "-stop-" in v["claim_id"]][0]
        assert stop["resolved"] is True


class TestFinalStateCaptureIsReadOnly:
    def test_it_places_amends_and_cancels_nothing(self, session):
        fake = session["fake"]
        before = (len(fake.order_create_bodies()),
                  sorted((k, v["orderStatus"]) for k, v in fake.orders.items()))
        tcr.capture_final_venue_state(
            client=session["client"], store=session["store"], symbol=SYMBOL)
        after = (len(fake.order_create_bodies()),
                 sorted((k, v["orderStatus"]) for k, v in fake.orders.items()))
        assert before == after

    def test_a_failed_read_is_reported_not_hidden(self, session, monkeypatch):
        def boom(*a, **kw):
            raise bc.PermanentAPIError("nope", ret_code=10001)
        monkeypatch.setattr(session["client"], "get_executions", boom)
        result = tcr.capture_final_venue_state(
            client=session["client"], store=session["store"], symbol=SYMBOL)
        assert result["ok"] is False and result["failures"]

    def test_an_unreadable_store_is_reported(self):
        class Broken:
            def orders_for_symbol(self, symbol):
                raise RuntimeError("db gone")
        result = tcr.capture_final_venue_state(
            client=object(), store=Broken(), symbol=SYMBOL)
        assert result["ok"] is False and result["orders"] == 0

    def test_a_settled_order_with_no_exchange_id_is_a_failure(self):
        class Store:
            def orders_for_symbol(self, symbol):
                return [{"order_link_id": "X", "exchange_id": "",
                         "status": "filled", "created_epoch": 10.0}]
        result = tcr.capture_final_venue_state(
            client=object(), store=Store(), symbol=SYMBOL)
        assert result["ok"] is False and "no exchange id" in result["failures"][0]

    def test_a_pending_order_with_no_exchange_id_is_just_skipped(self):
        class Store:
            def orders_for_symbol(self, symbol):
                return [{"order_link_id": "X", "exchange_id": "",
                         "status": "pending", "created_epoch": 10.0}]
        result = tcr.capture_final_venue_state(
            client=object(), store=Store(), symbol=SYMBOL)
        assert result == {"ok": True, "orders": 1, "reads": 0, "failures": []}

    def test_a_read_that_returns_nothing_is_not_a_capture(self):
        class Store:
            def orders_for_symbol(self, symbol):
                return [{"order_link_id": "X", "exchange_id": "O1",
                         "status": "filled", "created_epoch": 10.0}]

        class Client:
            def get_order(self, **kw):
                return None
        result = tcr.capture_final_venue_state(
            client=Client(), store=Store(), symbol=SYMBOL)
        assert result["ok"] is False and result["reads"] == 0
        assert "no order" in result["failures"][0]

    def test_only_this_runs_orders_are_read_and_each_once(self):
        reads = []

        class Store:
            def orders_for_symbol(self, symbol):
                return [
                    {"exchange_id": "OLD", "status": "filled", "created_epoch": 1.0},
                    {"exchange_id": "NEW", "status": "filled", "created_epoch": 100.0},
                    {"exchange_id": "NEW", "status": "filled", "created_epoch": 101.0}]

        class Client:
            def get_order(self, **kw):
                reads.append(("order", kw["order_id"]))
                return {"orderId": kw["order_id"]}

            def get_executions(self, symbol, **kw):
                reads.append(("exec", kw["order_id"]))
                return []
        result = tcr.capture_final_venue_state(
            client=Client(), store=Store(), symbol=SYMBOL, since_epoch=50.0)
        assert result["ok"] is True
        assert reads == [("order", "NEW"), ("exec", "NEW")]


class TestTheCloseLinkage:
    def test_a_provisional_close_keeps_its_old_lookup_by_link(self, tmp_path):
        """Naming order ids on a provisional trade would switch its price
        lookup from the link id to order-id matching."""
        fake = FilledOrdersCarryFees()
        store = StateStore(str(tmp_path / "s.db"))
        store.claim_writer()
        store.update_equity(10_000.0)
        cfg = SessionCfg()
        client = bc.BybitClient(config=cfg, store=store, transport=fake)
        risk = BillionaireRiskManager(config=cfg, store=store)
        engine = te.TradingEngine(
            client=client, risk_manager=risk,
            position_sizer=BillionairePositionSizing(config=cfg, risk_manager=risk),
            store=store, config=cfg)
        last = fake_bybit.LAST_PRICE[SYMBOL]
        assert engine.execute(te.TradeIntent(
            symbol=SYMBOL, signal_type="BUY", entry_price=last,
            stop_price=last * 0.98, take_profits=((last * 1.05, 1.0),),
            confidence=0.8)).ok
        engine._fill_price = lambda fill: 0.0            # the exit price is unread
        assert engine.close_position(symbol=SYMBOL, reason="t").ok
        import json
        trade = store._query("SELECT * FROM trades")[0]
        meta = json.loads(trade["meta"])
        assert trade["price_provisional"] == 1
        assert meta["exit_order_ids"] == [] and meta["exit_exec_ids"] == []
        assert meta["entry_order_link_id"]

    def test_an_emergency_close_makes_no_execution_read(self, tmp_path):
        fake = FilledOrdersCarryFees()
        reads = []
        real = fake.request

        def spy(method, url, **kw):
            if "execution/list" in url:
                reads.append(url)
            return real(method, url, **kw)
        fake.request = spy
        store = StateStore(str(tmp_path / "s.db"))
        store.claim_writer()
        store.update_equity(10_000.0)
        cfg = SessionCfg()
        client = bc.BybitClient(config=cfg, store=store, transport=fake)
        risk = BillionaireRiskManager(config=cfg, store=store)
        engine = te.TradingEngine(
            client=client, risk_manager=risk,
            position_sizer=BillionairePositionSizing(config=cfg, risk_manager=risk),
            store=store, config=cfg)
        last = fake_bybit.LAST_PRICE[SYMBOL]
        assert engine.execute(te.TradeIntent(
            symbol=SYMBOL, signal_type="BUY", entry_price=last,
            stop_price=last * 0.98, take_profits=((last * 1.05, 1.0),),
            confidence=0.8)).ok
        row = {p["symbol"]: p for p in store.open_positions()}[SYMBOL]
        order = client.place_order(symbol=SYMBOL, side="Sell",
                                   qty=float(row["qty"]), order_type="Market",
                                   purpose="close", reduce_only=True)
        fill = engine._await_fill(SYMBOL, order.order_link_id)
        engine._book_full_exit(symbol=SYMBOL, row=row, exit_order=order,
                               fill=fill, reason="EMERGENCY:test",
                               prove_executions=False)
        assert reads == []
        import json
        meta = json.loads(store._query("SELECT * FROM trades")[0]["meta"])
        assert meta["exit_exec_ids"] == []


class TestTheValidatorHandlesNonFinalStates:
    def test_a_stop_only_ever_seen_untriggered_is_stale_not_contradicted(self):
        import evidence_fixtures as fx
        specs = [dict(path="/v5/order/realtime", rows=[
            fx.order_row("OS", cum="0", status="Untriggered", link="STOP-1")])]
        cap = er.capture_from_records(fx.chain(specs))
        claim = {"claim_id": "o", "source": "testnet_claim",
                 "environment": "testnet", "order_link_id": "STOP-1",
                 "exchange_id": "OS", "symbol": "BTCUSDT", "side": "Buy",
                 "qty": 0.002, "status": "cancelled"}
        report = er.reconcile(order_claims=[claim], captures=[cap], now_ms=fx.NOW)
        issues = report["order_claims"][0]["issues"]
        assert "NO_FINAL_VENUE_STATE" in issues
        assert not any(i.startswith("VENUE_DISAGREES") for i in issues)
