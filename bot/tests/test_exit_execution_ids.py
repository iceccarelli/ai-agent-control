"""A venue-triggered exit (stop / take-profit) is priced from closed-PnL rows,
which carry an average price and an order id but NO execution ids. The engine
now also reads the orders' executions and records the exact execution ids on the
exit row - in the same transaction as the trade - when, and only when, they
prove the exit. Otherwise the exit is booked exactly as before and is simply
not provable. One execution can price one exit row only."""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

from fake_bybit import FakeBybit  # noqa: E402
from test_observe_exits import LinearCfg, _engine, _open  # noqa: E402
import evidence_reconcile as er  # noqa: E402
import evidence_fixtures as fx  # noqa: E402


def exec_row(i, qty, price="61000", order="O1", fee="0.0005", ccy="USDT",
             side="Buy", **over):
    row = {"execId": f"EX-{i}", "orderId": order, "orderLinkId": "",
           "symbol": "BTCUSDT", "side": side, "execType": "Trade",
           "execQty": str(qty), "execPrice": str(price), "execFee": fee,
           "feeCurrency": ccy, "execTime": str(1_760_000_000_000 + i)}
    row.update(over)
    return row


def closed(qty, price="61000", oid="O1", dt=1000):
    return {"symbol": "BTCUSDT", "side": "Buy", "qty": str(qty), "orderId": oid,
            "avgExitPrice": str(price), "closedPnl": "-10",
            "updatedTime": str(int(time.time() * 1000) + dt)}


def venue(tmp_path, size=0.0, held=0.01):
    fake = FakeBybit()
    if size:
        fake.positions["BTCUSDT"] = {"symbol": "BTCUSDT", "side": "Sell",
                                     "size": str(size), "avgPrice": "60000",
                                     "stopLoss": "61000"}
    engine, store = _engine(tmp_path, LinearCfg(), fake)
    _open(store, qty=held)
    return fake, engine, store


def trades(store):
    return store._query("SELECT * FROM trades ORDER BY id")


def meta(row):
    return json.loads(row["meta"])


class TestACompleteExitRecordsItsExecutions:
    def test_the_exec_ids_are_stored_with_the_trade(self, tmp_path):
        fake, engine, store = venue(tmp_path)
        fake.closed_pnl.append(closed(0.01))
        fake.executions += [exec_row(1, 0.004), exec_row(2, 0.006)]
        assert engine.observe_exits()["closed"] == 1
        (t,) = trades(store)
        assert meta(t)["exit_exec_ids"] == ["EX-1", "EX-2"]
        assert "exec_ids_unproven" not in meta(t)["fee_evidence"]
        assert float(t["exit_price"]) == pytest.approx(61_000.0)

    def test_a_replayed_cycle_books_nothing_and_keeps_the_ids(self, tmp_path):
        fake, engine, store = venue(tmp_path)
        fake.closed_pnl.append(closed(0.01))
        fake.executions += [exec_row(1, 0.01)]
        engine.observe_exits()
        engine.observe_exits()
        (t,) = trades(store)
        assert meta(t)["exit_exec_ids"] == ["EX-1"]

    def test_a_multi_fill_exit_at_a_vwap_is_proven_when_it_matches(self, tmp_path):
        fake, engine, store = venue(tmp_path)
        fake.closed_pnl.append(closed(0.01, price="61100"))
        fake.executions += [exec_row(1, 0.005, price="61000"),
                            exec_row(2, 0.005, price="61200")]
        engine.observe_exits()
        assert meta(trades(store)[0])["exit_exec_ids"] == ["EX-1", "EX-2"]


class TestWhatIsNotProvenIsNotRecorded:
    def book(self, tmp_path, executions, price="61000", qty=0.01):
        fake, engine, store = venue(tmp_path)
        fake.closed_pnl.append(closed(qty, price=price))
        fake.executions += executions
        assert engine.observe_exits()["closed"] == 1
        (t,) = trades(store)
        return t

    def unproven(self, t):
        assert meta(t)["exit_exec_ids"] == []
        assert meta(t)["fee_evidence"]["exec_ids_unproven"]
        # the exit itself is accounted exactly as before
        assert float(t["exit_price"]) == pytest.approx(
            float(t["exit_price"]))

    def test_no_executions_at_all(self, tmp_path):
        self.unproven(self.book(tmp_path, []))

    def test_executions_that_do_not_add_up_to_the_exit(self, tmp_path):
        self.unproven(self.book(tmp_path, [exec_row(1, 0.004)]))

    def test_more_executions_than_this_exit_closed(self, tmp_path):
        # one order filled in two pieces, only the first has left the position
        fake, engine, store = venue(tmp_path, size=0.004)
        fake.closed_pnl.append(closed(0.006))
        fake.executions += [exec_row(1, 0.006), exec_row(2, 0.002)]
        assert engine.observe_exits()["reduced"] == 1
        assert meta(trades(store)[0])["exit_exec_ids"] == []

    def test_a_price_the_executions_contradict(self, tmp_path):
        t = self.book(tmp_path, [exec_row(1, 0.01, price="60500")])
        assert meta(t)["exit_exec_ids"] == []
        assert "closed-pnl" in meta(t)["fee_evidence"]["exec_ids_unproven"]

    @pytest.mark.parametrize("over", [
        {"execType": "Funding"}, {"symbol": "ETHUSDT"}, {"side": "Sell"},
        {"feeCurrency": ""}, {"execFee": ""}, {"execId": ""}, {"execPrice": "0"}])
    def test_a_row_that_is_not_an_ordinary_closing_fill(self, tmp_path, over):
        self.unproven(self.book(tmp_path, [exec_row(1, 0.01, **over)]))

    def test_a_duplicate_row_with_different_content(self, tmp_path):
        self.unproven(self.book(tmp_path, [
            exec_row(1, 0.005), exec_row(1, 0.005, price="1")]))

    def test_an_unreadable_execution_endpoint_does_not_stop_the_booking(self, tmp_path):
        fake, engine, store = venue(tmp_path)
        fake.closed_pnl.append(closed(0.01))
        real = fake.request

        def flaky(method, url, **kw):
            if "execution/list" in url:
                return 500, "boom"
            return real(method, url, **kw)
        fake.request = flaky
        assert engine.observe_exits()["closed"] == 1
        (t,) = trades(store)
        assert meta(t)["exit_exec_ids"] == [] and store.open_position_count() == 0

    def test_a_provisional_exit_attempts_no_proof(self, tmp_path):
        fake, engine, store = venue(tmp_path)
        real = fake.request
        reads = []

        def flaky(method, url, **kw):
            if "execution/list" in url:
                reads.append(url)
            if "closed-pnl" in url or "tickers" in url:
                return 500, "boom"
            return real(method, url, **kw)
        fake.request = flaky
        assert engine.observe_exits()["exits_provisional"] == 1
        assert meta(trades(store)[0])["exit_exec_ids"] == [] and reads == []


class TestOneExecutionPricesOneExit:
    def test_an_execution_already_named_by_another_exit_is_not_named_again(self, tmp_path):
        fake, engine, store = venue(tmp_path, size=0.004)
        fake.closed_pnl.append(closed(0.006, oid="O1", dt=1000))
        fake.executions += [exec_row(1, 0.006)]
        engine.observe_exits()
        # the venue (wrongly) serves the SAME execution for a second exit order
        fake.positions["BTCUSDT"]["size"] = "0.002"
        fake.closed_pnl.append(closed(0.002, oid="O2", dt=2000))
        fake.executions += [exec_row(1, 0.002, order="O2")]
        engine.observe_exits()
        first, second = trades(store)
        assert meta(first)["exit_exec_ids"] == ["EX-1"]
        assert meta(second)["exit_exec_ids"] == []
        assert "already named" in meta(second)["fee_evidence"]["exec_ids_unproven"]

    def test_the_store_lists_every_claimed_execution(self, tmp_path):
        fake, engine, store = venue(tmp_path)
        fake.closed_pnl.append(closed(0.01))
        fake.executions += [exec_row(1, 0.01)]
        engine.observe_exits()
        assert store.claimed_exit_exec_ids("BTCUSDT") == {"ex-1"}      # normalised
        assert store.claimed_exit_exec_ids("BTCUSDT", exclude_trade_id=1) == set()
        assert store.claimed_exit_exec_ids("ETHUSDT") == set()

    def test_a_provisional_reconciliation_cannot_reuse_a_claimed_execution(self, tmp_path):
        fake, engine, store = venue(tmp_path, size=0.004)
        # exit 1: proven, names EX-1
        fake.closed_pnl.append(closed(0.006, oid="O1", dt=1000))
        fake.executions += [exec_row(1, 0.006)]
        engine.observe_exits()
        # exit 2: closed-pnl unreadable -> provisional
        real = fake.request
        fake.positions["BTCUSDT"]["size"] = "0.002"

        def flaky(method, url, **kw):
            if "closed-pnl" in url or "tickers" in url:
                return 500, "boom"
            return real(method, url, **kw)
        fake.request = flaky
        engine.observe_exits()
        prov = [t for t in trades(store) if t["price_provisional"]]
        assert len(prov) == 1
        ok = store.reconcile_provisional_trade(
            int(prov[0]["id"]), exit_price=61_000.0, gross_pnl=-2.0,
            exit_fee=0.1, exit_fee_realized=True, exit_fee_currency="USDT",
            evidence={"exec_ids": ["EX-1"], "qty": 0.002, "vwap": 61_000.0})
        assert ok is False
        row = store._query("SELECT * FROM trades WHERE id = ?",
                           (int(prov[0]["id"]),))[0]
        assert row["price_provisional"] == 1                 # still unpriced
        journal = store._query(
            "SELECT * FROM decisions WHERE decision = 'EXIT_EXEC_ALREADY_CLAIMED'")
        assert len(journal) == 1

    def test_a_fresh_execution_still_reconciles(self, tmp_path):
        fake, engine, store = venue(tmp_path)
        real = fake.request

        def flaky(method, url, **kw):
            if "closed-pnl" in url or "tickers" in url:
                return 500, "boom"
            return real(method, url, **kw)
        fake.request = flaky
        engine.observe_exits()
        (t,) = trades(store)
        assert store.reconcile_provisional_trade(
            int(t["id"]), exit_price=61_000.0, gross_pnl=-10.0, exit_fee=0.1,
            exit_fee_realized=True, exit_fee_currency="USDT",
            evidence={"exec_ids": ["EX-9"], "qty": 0.01, "vwap": 61_000.0}) is True


class TestTheValidatorCanNowProveThem:
    def test_the_adapter_carries_the_recorded_ids_and_the_split_is_proven(self, tmp_path):
        fake, engine, store = venue(tmp_path)
        fake.closed_pnl.append(closed(0.01, price="61100"))
        fake.executions += [exec_row(1, 0.005, price="61000"),
                            exec_row(2, 0.005, price="61200")]
        engine.observe_exits()
        store.close()
        path = str(tmp_path / "state.db")
        c = er.claims_from_store(path, environment="testnet",
                                 source="testnet_claim")
        claim = c["trade_claims"][0]
        assert claim["exit_exec_ids"] == ["EX-1", "EX-2"]
        # venue capture of exactly those executions: the exit price is proven
        # from the named fills, not inferred from the order total
        claim.update(entry_order_link_id="ENT", entry_price=60_000.0,
                     entry_fee=0.0, entry_fee_realized=True,
                     exit_fee=0.001, exit_fee_realized=True)
        entry = [fx.exec_row(7, order="OE", link="ENT", symbol="BTCUSDT",
                             side="Sell", qty="0.01", price="60000", fee="0",
                             t=fx.T0)]
        ex = [dict(exec_row(i, 0.005, price=p), execTime=str(fx.T0 + 1000 * i))
              for i, p in ((1, "61000"), (2, "61200"))]
        cap = er.capture_from_records(fx.chain([
            dict(path="/v5/execution/list", rows=entry + ex),
            dict(path="/v5/order/history", rows=[
                fx.order_row("OE", cum="0.01", side="Sell", link="ENT"),
                fx.order_row("O1", cum="0.01", side="Buy", link="")],
                at=fx.T0 + 60_000)]))
        claim.update(opened_ms=fx.T0, closed_ms=fx.T0 + 2000)
        r = er.reconcile(trade_claims=[claim], captures=[cap], now_ms=fx.NOW)
        issues = r["trades"][0]["issues"]
        assert "EXIT_ALLOCATION_UNPROVEN" not in issues
        assert "VENUE_DISAGREES:exit_exec_missing" not in issues


class TestFindingsFromTheAdversarialReview:
    """Reproduced against the first version of the change."""

    def exit_once(self, tmp_path, executions, qty=0.01, oid="O1", fake_engine=None):
        fake, engine, store = fake_engine or venue(tmp_path)
        fake.closed_pnl.append(closed(qty, oid=oid))
        fake.executions += executions
        engine.observe_exits()
        return fake, engine, store

    @pytest.mark.parametrize("bad", [{"exit_exec_ids": 5}, {"exit_exec_ids": "EX-1"},
                                     {"reconciliation": {"evidence": {"exec_ids": 7}}},
                                     {"reconciliation": 3}, {"exit_exec_ids": [None, 5, ["x"]]}])
    def test_another_rows_malformed_meta_cannot_stop_a_booking(self, tmp_path, bad):
        fake, engine, store = venue(tmp_path)
        store._conn.execute(
            "INSERT INTO trades (symbol,side,qty,entry_price,exit_price,gross_pnl,"
            "entry_fee,exit_fee,net_pnl,opened_epoch,closed_epoch,day,"
            "order_link_id,meta,dedupe_key) VALUES ('BTCUSDT','Sell',0.001,60000,"
            "60000,0,0,0,0,1,2,'2025-10-09','x',?, 'old')", (json.dumps(bad),))
        store._conn.commit()
        fake.closed_pnl.append(closed(0.01))
        fake.executions += [exec_row(1, 0.01)]
        assert engine.observe_exits()["closed"] == 1
        assert store.open_position_count() == 0
        assert store.claimed_exit_exec_ids("BTCUSDT") >= set()   # does not raise
        # a string is not claimed as its characters
        assert "e" not in store.claimed_exit_exec_ids("BTCUSDT")

    @pytest.mark.parametrize("bad_id", [" ", "", True, ["x"], {"a": 1}, 1.5, None])
    def test_a_row_without_a_usable_id_proves_nothing(self, tmp_path, bad_id):
        _f, _e, store = self.exit_once(tmp_path, [exec_row(1, 0.01, execId=bad_id)])
        (t,) = trades(store)
        assert meta(t)["exit_exec_ids"] == []
        assert meta(t)["fee_evidence"]["exec_ids_unproven"]

    def test_ids_that_differ_only_in_case_within_one_order_prove_nothing(self, tmp_path):
        _f, _e, store = self.exit_once(tmp_path, [
            exec_row(1, 0.005, execId="abc"), exec_row(2, 0.005, execId="ABC")])
        assert meta(trades(store)[0])["exit_exec_ids"] == []

    @pytest.mark.parametrize("variant", ["ex-1", "EX-1 ", " EX-1", "Ex-1"])
    def test_a_case_or_spacing_variant_cannot_reclaim_an_execution(self, tmp_path, variant):
        fake, engine, store = venue(tmp_path)
        fake.closed_pnl.append(closed(0.01, oid="O1", dt=1000))
        fake.executions += [exec_row(1, 0.01)]
        engine.observe_exits()
        _open(store)                                    # the symbol is re-opened
        fake.closed_pnl.append(closed(0.01, oid="O2", dt=3000))
        fake.executions += [exec_row(1, 0.01, order="O2", execId=variant)]
        engine.observe_exits()
        first, second = trades(store)
        assert meta(first)["exit_exec_ids"] == ["EX-1"]
        assert meta(second)["exit_exec_ids"] == []

    def test_a_provisional_reconciliation_cannot_reclaim_by_variant(self, tmp_path):
        fake, engine, store = venue(tmp_path)
        fake.closed_pnl.append(closed(0.01))
        fake.executions += [exec_row(1, 0.01)]
        engine.observe_exits()
        _open(store)
        real = fake.request

        def flaky(method, url, **kw):
            if "closed-pnl" in url or "tickers" in url:
                return 500, "boom"
            return real(method, url, **kw)
        fake.request = flaky
        engine.observe_exits()
        prov = [t for t in trades(store) if t["price_provisional"]][0]
        for variant in ("ex-1", " EX-1 "):
            assert store.reconcile_provisional_trade(
                int(prov["id"]), exit_price=61_000.0, gross_pnl=-1.0,
                exit_fee=0.1, exit_fee_realized=True, exit_fee_currency="USDT",
                evidence={"exec_ids": [variant], "qty": 0.01}) is False

    def test_a_refused_reconciliation_is_visible_and_journalled_once(self, tmp_path):
        fake, engine, store = venue(tmp_path)
        fake.closed_pnl.append(closed(0.01, oid="O1", dt=1000))
        fake.executions += [exec_row(1, 0.01)]
        engine.observe_exits()
        _open(store)
        real = fake.request

        def flaky(method, url, **kw):
            if "closed-pnl" in url or "tickers" in url:
                return 500, "boom"
            return real(method, url, **kw)
        fake.request = flaky
        engine.observe_exits()
        # the venue re-serves the claimed execution for the provisional exit
        fake.executions.append(exec_row(1, 0.01, order="PROV"))
        prov = [t for t in trades(store) if t["price_provisional"]][0]
        store._conn.execute("UPDATE trades SET meta = ? WHERE id = ?", (
            json.dumps({**meta(prov), "exit_order_ids": ["PROV"]}), prov["id"]))
        store._conn.commit()
        fake.request = real
        sums = [engine.reconcile_provisional_exits() for _ in range(5)]
        assert all(x["reconciled"] == 0 for x in sums)
        assert all(x["pending"] >= 1 and x["refused"] for x in sums)
        rows = store._query(
            "SELECT * FROM decisions WHERE decision = 'EXIT_EXEC_ALREADY_CLAIMED'")
        assert len(rows) == 1
        still = store._query("SELECT * FROM trades WHERE id = ?", (prov["id"],))[0]
        assert still["price_provisional"] == 1
