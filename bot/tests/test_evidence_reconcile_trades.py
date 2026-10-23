"""Reconciling the bot's OWN rows (orders and closed trades) against venue
captures. The local database keeps prices and fees per leg but no execId, so each
leg is rebuilt from the captured executions of its order. Synthetic data only."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import evidence_reconcile as er  # noqa: E402
import evidence_fixtures as fx  # noqa: E402

E_LINK, E_ORD, X_ORD = "ENT-1", "ORD-E", "ORD-X"


def entry_rows(side="Buy"):
    return [fx.exec_row(1, order=E_ORD, link=E_LINK, side=side, qty="0.001",
                        price="60000", fee="0.00006"),
            fx.exec_row(2, order=E_ORD, link=E_LINK, side=side, qty="0.001",
                        price="60010", fee="0.00006")]


def exit_rows(side="Sell", qty="0.002", price="60100", fee="0.00012"):
    return [fx.exec_row(3, order=X_ORD, link="EXT-1", side=side, qty=qty,
                        price=price, fee=fee, t=fx.T0 + 600_000)]


def specs(entry=None, exit_=None, orders=True, side="Buy"):
    out = [dict(path="/v5/execution/list",
                rows=(entry if entry is not None else entry_rows(side))
                + (exit_ if exit_ is not None else exit_rows(
                    "Sell" if side == "Buy" else "Buy")))]
    if orders:
        out.append(dict(path="/v5/order/history", rows=[
            fx.order_row(E_ORD, cum="0.002", side=side, link=E_LINK),
            fx.order_row(X_ORD, cum="0.002", link="EXT-1",
                         side="Sell" if side == "Buy" else "Buy")],
            at=fx.T0 + 700_000))
    return out


def trade(**over):
    t = {"claim_id": "T1", "source": "testnet_claim", "environment": "testnet",
         "symbol": "BTCUSDT", "side": "Buy", "qty": 0.002,
         "entry_price": 60005.0, "exit_price": 60100.0,
         "gross_pnl": (60100.0 - 60005.0) * 0.002, "entry_fee": 0.00012,
         "exit_fee": 0.00012, "entry_fee_realized": True,
         "exit_fee_realized": True, "price_provisional": False,
         "opened_ms": fx.T0, "closed_ms": fx.T0 + 600_000,
         "entry_order_link_id": E_LINK, "exit_order_ids": [X_ORD]}
    t.update(over)
    return t


def go(trades, spec=None, **kw):
    cap = er.capture_from_records(fx.chain(spec if spec is not None else specs()))
    return er.reconcile(trade_claims=trades, captures=[cap], now_ms=fx.NOW, **kw)


def verdict(r, i=0):
    return r["trades"][i]


class TestTradesAgainstVenueExecutions:
    def test_a_matching_corroborated_trade_is_reconciled(self):
        r = go([trade()])
        assert verdict(r)["provenance"] == er.VENUE_RECONCILED
        assert verdict(r)["resolved"] and verdict(r)["issues"] == []
        assert r["pnl_claim_allowed"] is True

    def test_a_short_trade_reconciles_with_opposite_sides(self):
        t = trade(side="Sell", entry_price=60005.0, exit_price=59900.0,
                  gross_pnl=(60005.0 - 59900.0) * 0.002)
        s = specs(side="Sell", exit_=exit_rows("Buy", price="59900"))
        assert verdict(go([t], s))["resolved"]

    def test_the_default_store_source_is_only_a_local_record(self):
        v = verdict(go([trade(source="local", environment="unknown")]))
        assert v["provenance"] == er.LOCAL_RECORD and not v["resolved"]

    @pytest.mark.parametrize("over,code", [
        ({"entry_price": 60006.0, "gross_pnl": (60100 - 60006.0) * 0.002},
         "VENUE_DISAGREES:entry_price"),
        ({"exit_price": 60101.0, "gross_pnl": (60101 - 60005.0) * 0.002},
         "VENUE_DISAGREES:exit_price"),
        ({"entry_fee": 0.5}, "VENUE_DISAGREES:entry_fee"),
        ({"exit_fee": 0.5}, "VENUE_DISAGREES:exit_fee"),
        ({"qty": 0.003, "gross_pnl": (60100 - 60005.0) * 0.003},
         "VENUE_DISAGREES:leg_qty"),
        ({"side": "Sell", "gross_pnl": (60005.0 - 60100.0) * 0.002},
         "VENUE_DISAGREES:side"),
        ({"symbol": "ETHUSDT"}, "VENUE_DISAGREES:leg_identity"),
        ({"closed_ms": fx.T0 + 1_800_000}, "VENUE_DISAGREES:closed_time")])
    def test_any_disagreement_with_the_venue_blocks(self, over, code):
        v = verdict(go([trade(**over)]))
        assert code in v["issues"] and not v["resolved"]

    def test_a_gross_that_does_not_follow_the_prices_blocks(self):
        v = verdict(go([trade(gross_pnl=99.0)]))
        assert "LOCAL_GROSS_ARITHMETIC" in v["issues"] and not v["resolved"]

    def test_a_provisional_exit_price_is_never_final(self):
        v = verdict(go([trade(price_provisional=True, exit_price=0.0)]))
        assert "EXIT_PRICE_PROVISIONAL" in v["issues"] and not v["resolved"]

    @pytest.mark.parametrize("leg", ["entry", "exit"])
    def test_a_fee_not_marked_realised_is_unresolved(self, leg):
        v = verdict(go([trade(**{f"{leg}_fee_realized": False})]))
        assert f"{leg.upper()}_FEE_NOT_REALIZED" in v["issues"]
        assert not v["resolved"]

    def test_a_partial_exit_cannot_prove_its_own_price(self):
        # the exit order filled 0.002 but this trade row closed only 0.001
        t = trade(qty=0.001, gross_pnl=(60100.0 - 60005.0) * 0.001,
                  entry_fee=0.00006)
        v = verdict(go([t]))
        assert "EXIT_ALLOCATION_UNPROVEN" in v["issues"] and not v["resolved"]

    def test_the_entry_fee_is_the_pro_rata_share(self):
        t = trade(qty=0.001, gross_pnl=(60100.0 - 60005.0) * 0.001,
                  entry_fee=0.00006)
        assert "VENUE_DISAGREES:entry_fee" not in verdict(go([t]))["issues"]

    def test_missing_exit_order_ids_leave_a_local_record(self):
        v = verdict(go([trade(exit_order_ids=[])]))
        assert v["provenance"] == er.LOCAL_RECORD
        assert "NO_VENUE_IDENTIFIERS" in v["issues"]

    def test_legs_absent_from_the_capture_stay_a_claim(self):
        v = verdict(go([trade()], spec=[]))
        assert v["provenance"] == er.CLAIMED_TESTNET_FILL
        assert "NOT_IN_CAPTURE" in v["issues"]

    def test_no_order_row_means_no_corroboration(self):
        v = verdict(go([trade()], specs(orders=False)))
        assert "NO_CORROBORATION" in v["issues"] and not v["resolved"]

    def test_two_different_bodies_for_one_exec_id_contradict(self):
        s = specs() + [dict(path="/v5/execution/list",
                            rows=[dict(exit_rows()[0], execPrice="60000")])]
        assert "VENUE_ROWS_CONTRADICT" in verdict(go([trade()], s))["issues"]

    def test_a_non_trade_execution_in_a_leg_blocks(self):
        rows = exit_rows()
        rows[0]["execType"] = "Funding"
        assert "VENUE_DISAGREES:leg_identity" in verdict(
            go([trade()], specs(exit_=rows)))["issues"]

    @pytest.mark.parametrize("over,code", [
        ({"qty": float("nan")}, "BAD_QTY"), ({"side": "flat"}, "BAD_SIDE"),
        ({"entry_price": 0}, "BAD_PRICE"), ({"closed_ms": 1}, "BAD_TIMESTAMP"),
        ({"closed_ms": fx.NOW + 10 ** 9}, "BAD_TIMESTAMP"),
        ({"source": "venue"}, "BAD_SOURCE")])
    def test_malformed_fields_are_named(self, over, code):
        assert code in verdict(go([trade(**over)]))["issues"]

    @pytest.mark.parametrize("bad", [None, "x", 3])
    def test_a_non_claim_is_reported_not_raised(self, bad):
        assert not verdict(go([bad]))["resolved"]

    def test_a_broken_chain_blocks_the_trade(self):
        recs = fx.chain(specs())
        recs[0]["response"]["result"]["list"][0]["execPrice"] = "1"
        r = er.reconcile(trade_claims=[trade()],
                         captures=[er.capture_from_records(recs)], now_ms=fx.NOW)
        assert "CAPTURE_CHAIN_BROKEN" in verdict(r)["issues"]


class TestLocalOrders:
    def order(self, **over):
        o = {"claim_id": "O1", "source": "testnet_claim", "environment": "testnet",
             "order_link_id": E_LINK, "exchange_id": E_ORD, "symbol": "BTCUSDT",
             "side": "Buy", "qty": 0.002, "status": "filled"}
        o.update(over)
        return o

    def go(self, orders, spec=None):
        cap = er.capture_from_records(fx.chain(spec if spec is not None else specs()))
        return er.reconcile(order_claims=orders, captures=[cap], now_ms=fx.NOW)

    def test_a_filled_order_that_the_venue_confirms_is_reconciled(self):
        r = self.go([self.order()])
        assert r["order_claims"][0]["resolved"] is True

    @pytest.mark.parametrize("over,code", [
        ({"status": "cancelled"}, "VENUE_DISAGREES:order_status"),
        ({"qty": 0.003}, "VENUE_DISAGREES:filled_qty"),
        ({"side": "Sell"}, "VENUE_DISAGREES:order_identity"),
        ({"order_link_id": "OTHER"}, "VENUE_DISAGREES:order_identity")])
    def test_a_disagreement_is_named(self, over, code):
        v = self.go([self.order(**over)])["order_claims"][0]
        assert not v["resolved"]
        if code:
            assert code in v["issues"]

    @pytest.mark.parametrize("status", ["pending", "new", "unknown",
                                        "partially_filled", ""])
    def test_an_unsettled_local_status_is_never_final(self, status):
        v = self.go([self.order(status=status)])["order_claims"][0]
        assert not v["resolved"]
        assert any(i.startswith(("ORDER_OUTCOME_UNSETTLED", "MISSING_FIELD"))
                   for i in v["issues"])

    def test_a_filled_order_with_no_venue_id_is_only_a_local_record(self):
        v = self.go([self.order(exchange_id="")])["order_claims"][0]
        assert v["provenance"] == er.LOCAL_RECORD
        assert "NO_VENUE_IDENTIFIERS" in v["issues"]

    def test_a_cancelled_order_the_venue_cancelled_agrees(self):
        spec = [dict(path="/v5/order/history", rows=[
            dict(fx.order_row(E_ORD, cum="0", status="Cancelled", link=E_LINK))])]
        v = self.go([self.order(status="cancelled")], spec)["order_claims"][0]
        assert v["resolved"]

    def test_a_filled_order_without_any_execution_row_is_uncorroborated(self):
        spec = [dict(path="/v5/order/history",
                     rows=[fx.order_row(E_ORD, cum="0.002", link=E_LINK)])]
        v = self.go([self.order()], spec)["order_claims"][0]
        assert "NO_CORROBORATION" in v["issues"] and not v["resolved"]


class TestTheStoreAdapter:
    def make_store(self, tmp_path):
        from persistence import StateStore
        path = str(tmp_path / "state.db")
        s = StateStore(path)
        s.close()
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO orders (order_link_id,symbol,side,order_type,qty,price,"
            "purpose,status,exchange_id,created_epoch,updated_epoch,meta) VALUES "
            "('ENT-1','BTCUSDT','Buy','Market',0.002,0,'entry','filled','ORD-E',"
            "1,1,'{}')")
        conn.execute(
            "INSERT INTO trades (symbol,side,qty,entry_price,exit_price,gross_pnl,"
            "entry_fee,exit_fee,net_pnl,opened_epoch,closed_epoch,day,"
            "order_link_id,meta,dedupe_key,entry_fee_realized,exit_fee_realized,"
            "price_provisional) VALUES ('BTCUSDT','Buy',0.002,60005,60100,0.19,"
            "0.00012,0.00012,0.1,?,?,'2025-10-09','ENT-1',?,'k',1,1,0)",
            (fx.T0 / 1000, (fx.T0 + 600_000) / 1000,
             json.dumps({"exit_order_ids": [X_ORD]})))
        conn.commit()
        conn.close()
        return path

    def test_it_builds_claims_and_leaves_the_file_untouched(self, tmp_path):
        path = self.make_store(tmp_path)
        before = hashlib.sha256(open(path, "rb").read()).hexdigest()
        c = er.claims_from_store(path, environment="testnet",
                                 source="testnet_claim")
        assert hashlib.sha256(open(path, "rb").read()).hexdigest() == before
        assert c["trade_claims"][0]["exit_order_ids"] == [X_ORD]
        assert c["order_claims"][0]["exchange_id"] == "ORD-E"
        cap = er.capture_from_records(fx.chain(specs()))
        r = er.reconcile(now_ms=fx.NOW, captures=[cap], **c)
        assert r["trades"][0]["resolved"] and r["order_claims"][0]["resolved"]
        assert r["pnl_claim_allowed"] is True

    def test_the_default_assertion_keeps_everything_local(self, tmp_path):
        c = er.claims_from_store(self.make_store(tmp_path))
        r = er.reconcile(now_ms=fx.NOW, **c)
        assert {v["provenance"] for v in r["trades"] + r["order_claims"]} \
            == {er.LOCAL_RECORD}
        assert r["pnl_claim_allowed"] is False

    def test_the_connection_is_read_only(self, tmp_path):
        path = self.make_store(tmp_path)
        # a read-only URI cannot create a missing database either
        with pytest.raises(er.StoreUnreadable):
            er.claims_from_store(str(tmp_path / "missing.db"))
        assert not (tmp_path / "missing.db").exists()

    def test_the_cli_accepts_a_store(self, tmp_path):
        path = self.make_store(tmp_path)
        out = tmp_path / "rep" / "r.json"
        code = er.main(["--store", path, "--store-environment", "testnet",
                        "--now-ms", str(fx.NOW), "--json", str(out)])
        rep = json.load(open(out))
        assert code == 2 and rep["counts_by_provenance"][er.LOCAL_RECORD] == 2
        assert rep["trades"][0]["provenance"] == er.LOCAL_RECORD  # source=local


def spec_with(entry=None, exit_=None, **kw):
    return specs(entry=entry, exit_=exit_, **kw)


class TestFindingsFromTheAdversarialReview:
    """Reproduced against the first version of the trade reconciler."""

    def test_the_same_trade_claimed_twice_resolves_neither(self):
        r = go([trade(claim_id="A"), trade(claim_id="B")])
        for v in r["trades"]:
            assert not v["resolved"]
            assert "ENTRY_OVERCLAIMED" in v["issues"]
            assert "EXIT_OVERCLAIMED" in v["issues"]
        assert r["pnl_claim_allowed"] is False

    def test_two_entries_cannot_share_one_exit_order(self):
        extra = [fx.exec_row(9, order="ORD-E2", link="ENT-2", qty="0.002",
                             price="60005", fee="0.00012")]
        s = specs()
        s[0]["rows"] = s[0]["rows"] + extra
        s[1]["rows"] = s[1]["rows"] + [fx.order_row("ORD-E2", link="ENT-2")]
        r = go([trade(claim_id="A"),
                trade(claim_id="B", entry_order_link_id="ENT-2")], s)
        assert all("EXIT_OVERCLAIMED" in v["issues"] and not v["resolved"]
                   for v in r["trades"])

    def test_the_entry_order_needs_its_own_order_record(self):
        s = specs()
        s[1]["rows"] = [r for r in s[1]["rows"] if r["orderId"] != E_ORD]
        v = verdict(go([trade()], s))
        assert "NO_CORROBORATION" in v["issues"] and not v["resolved"]

    @pytest.mark.parametrize("row", [
        dict(symbol="ETHUSDT"), dict(side="Buy"), dict(cumExecQty="0.000001"),
        dict(orderStatus="Rejected")])
    def test_an_order_record_must_match_the_leg(self, row):
        s = specs()
        for r in s[1]["rows"]:
            if r["orderId"] == X_ORD:
                r.update(row)
        assert not verdict(go([trade()], s))["resolved"]

    @pytest.mark.parametrize("over", [
        {"exit_price": "abc"}, {"gross_pnl": None}, {"closed_ms": "abc"},
        {"opened_ms": 1.5, "closed_ms": 2.5}, {"opened_ms": int(1e300)}])
    def test_unvalidated_fields_no_longer_pass(self, over):
        c = trade(**over)
        if over.get("gross_pnl", 1) is None:
            del c["gross_pnl"]
        assert not verdict(go([c]))["resolved"]

    def test_opened_and_closed_must_agree_with_the_executions(self):
        assert "VENUE_DISAGREES:opened_time" in verdict(
            go([trade(opened_ms=fx.T0 - 3_000_000)]))["issues"]
        assert not verdict(go([trade(opened_ms=fx.T0 + 700_000,
                                     closed_ms=fx.T0 + 800_000)]))["resolved"]

    def test_an_exit_before_the_entry_is_refused(self):
        s = specs(exit_=exit_rows())
        s[0]["rows"][-1] = dict(s[0]["rows"][-1], execTime=str(fx.T0 - 5_000_000))
        assert not verdict(go([trade(closed_ms=fx.T0 - 5_000_000)], s))["resolved"]

    def test_a_blank_exit_time_blocks_instead_of_skipping_the_check(self):
        s = specs()
        s[0]["rows"][-1] = dict(s[0]["rows"][-1], execTime="")
        assert "VENUE_DISAGREES:exec_time_unusable" in verdict(go([trade()], s))["issues"]

    def test_mixed_fee_currencies_in_a_leg_are_refused(self):
        s = specs()
        s[0]["rows"][1] = dict(s[0]["rows"][1], feeCurrency="BTC")
        assert "VENUE_DISAGREES:fee_currency" in verdict(go([trade()], s))["issues"]

    def test_a_stated_fee_currency_must_match(self):
        assert "VENUE_DISAGREES:fee_currency" in verdict(
            go([trade(entry_fee_currency="BTC")]))["issues"]
        assert verdict(go([trade(entry_fee_currency="USDT",
                                 exit_fee_currency="USDT")]))["resolved"]

    @pytest.mark.parametrize("ids", [{"ORD-X": 1}, "ORD-X", 5, [5]])
    def test_exit_order_ids_must_be_a_list_of_strings(self, ids):
        assert not verdict(go([trade(exit_order_ids=ids)]))["resolved"]

    def test_duplicate_exit_ids_are_one_order(self):
        assert verdict(go([trade(exit_order_ids=[X_ORD, X_ORD])]))["resolved"]

    @pytest.mark.parametrize("flag", ["0", "false", 1, "yes"])
    def test_realised_means_true_not_truthy(self, flag):
        v = verdict(go([trade(entry_fee_realized=flag)]))
        assert "ENTRY_FEE_NOT_REALIZED" in v["issues"] and not v["resolved"]

    def test_a_large_genuine_quantity_is_not_falsely_refused(self):
        q = 999999.9
        part = "333333.3"
        entry = [fx.exec_row(i, order=E_ORD, link=E_LINK, qty=part, price="1",
                             fee="0") for i in (1, 2, 3)]
        exit_ = [fx.exec_row(4, order=X_ORD, link="EXT-1", side="Sell",
                             qty=str(q), price="2", fee="0", t=fx.T0 + 600_000)]
        s = specs(entry=entry, exit_=exit_)
        for r in s[1]["rows"]:
            r["cumExecQty"] = str(q)
        t = trade(qty=q, entry_price=1.0, exit_price=2.0, gross_pnl=q,
                  entry_fee=0.0, exit_fee=0.0)
        assert verdict(go([t], s))["resolved"]


class TestOrderFindings:
    def order(self, **over):
        return TestLocalOrders().order(**over)

    def go(self, orders, spec):
        cap = er.capture_from_records(fx.chain(spec))
        return er.reconcile(order_claims=orders, captures=[cap], now_ms=fx.NOW)

    def test_filled_needs_fills_that_add_up_to_the_order(self):
        s = specs()
        s[0]["rows"] = [dict(entry_rows()[0], execQty="0.0000001")]
        v = self.go([self.order()], s)["order_claims"][0]
        assert "NO_CORROBORATION" in v["issues"] and not v["resolved"]

    def test_fills_of_another_symbol_or_side_do_not_corroborate(self):
        for over in (dict(symbol="ETHUSDT"), dict(side="Sell")):
            s = specs()
            s[0]["rows"] = [dict(r, **over) for r in entry_rows()]
            assert not self.go([self.order()], s)["order_claims"][0]["resolved"]

    def test_conflicting_final_statuses_are_not_resolved(self):
        s = specs()
        s[1]["rows"] = [dict(fx.order_row(E_ORD, cum="0.002", link=E_LINK)),
                        dict(fx.order_row(E_ORD, cum="0.002", link=E_LINK,
                                          status="Cancelled"))]
        for status in ("filled", "cancelled"):
            v = self.go([self.order(status=status)], s)["order_claims"][0]
            assert "ORDER_STATUS_CONFLICT" in v["issues"] and not v["resolved"]

    def test_a_cancelled_order_must_account_for_its_executions(self):
        spec = [dict(path="/v5/order/history", rows=[
            fx.order_row(E_ORD, cum="0.5", status="Cancelled", link=E_LINK)])]
        v = self.go([self.order(status="cancelled")], spec)["order_claims"][0]
        assert "VENUE_DISAGREES:cancelled_fills" in v["issues"]

    def test_two_local_orders_cannot_share_one_exchange_id(self):
        r = self.go([self.order(claim_id="A"),
                     self.order(claim_id="B", order_link_id="OTHER")], specs())
        assert all("DUPLICATE_EXCHANGE_ID" in v["issues"] and not v["resolved"]
                   for v in r["order_claims"])


class TestMalformedClaimsNeverRaise:
    @pytest.mark.parametrize("over", [
        {"exit_order_ids": 5}, {"side": ["Buy"]}, {"side": {"a": 1}},
        {"qty": 10 ** 5000}, {"entry_price": 10 ** 5000},
        {"claim_id": 10 ** 5000}, {"entry_order_link_id": ["x"]}])
    def test_trades(self, over):
        assert not verdict(go([trade(**over)]))["resolved"]

    @pytest.mark.parametrize("over", [
        {"qty": 10 ** 5000}, {"side": ["Buy"]}, {"status": ["filled"]},
        {"exchange_id": 5}, {"order_link_id": None}])
    def test_orders(self, over):
        cap = er.capture_from_records(fx.chain(specs()))
        r = er.reconcile(order_claims=[TestLocalOrders().order(**over)],
                         captures=[cap], now_ms=fx.NOW)
        assert not r["order_claims"][0]["resolved"]

    def test_a_fill_claim_with_a_giant_integer_is_refused_not_raised(self):
        cap = er.capture_from_records(fx.chain(fx.good_chain() and [
            dict(path="/v5/execution/list", rows=[fx.exec_row(1)])]))
        r = er.reconcile(fill_claims=[fx.claim(qty=10 ** 5000)], captures=[cap],
                         now_ms=fx.NOW)
        assert not r["fills"][0]["resolved"]


class TestTheStoreAdapterIsRobust:
    def build(self, tmp_path, wal=False):
        return TestTheStoreAdapter().make_store(tmp_path)

    def test_a_bad_row_becomes_a_malformed_claim_not_an_exception(self, tmp_path):
        path = self.build(tmp_path)
        conn = sqlite3.connect(path)
        conn.execute("UPDATE trades SET closed_epoch = 1e999")
        conn.commit()
        conn.close()
        c = er.claims_from_store(path, environment="testnet",
                                 source="testnet_claim")
        assert c["trade_claims"] == [{"_malformed": "unreadable trade row"}]
        assert len(c["order_claims"]) == 1                 # the rest still read
        r = er.reconcile(now_ms=fx.NOW, **c)
        assert not r["trades"][0]["resolved"]

    def test_text_in_a_real_column_is_a_malformed_row(self, tmp_path):
        path = self.build(tmp_path)
        conn = sqlite3.connect(path)
        conn.execute("UPDATE trades SET opened_epoch = 'soon'")
        conn.commit()
        conn.close()
        assert er.claims_from_store(path)["trade_claims"] == [
            {"_malformed": "unreadable trade row"}]

    def test_a_non_database_or_missing_tables_are_a_clear_refusal(self, tmp_path):
        junk = tmp_path / "junk.db"
        junk.write_bytes(b"not sqlite" * 100)
        with pytest.raises(er.StoreUnreadable):
            er.claims_from_store(str(junk))
        empty = tmp_path / "empty.db"
        sqlite3.connect(str(empty)).close()
        with pytest.raises(er.StoreUnreadable):
            er.claims_from_store(str(empty))

    def test_reading_leaves_no_sidecar_files_and_an_unmerged_wal_is_refused(self, tmp_path):
        path = self.build(tmp_path)
        before = sorted(os.listdir(tmp_path))
        er.claims_from_store(path)
        assert sorted(os.listdir(tmp_path)) == before
        with open(path + "-wal", "wb") as fh:
            fh.write(b"x" * 64)
        with pytest.raises(er.StoreUnreadable, match="unmerged"):
            er.claims_from_store(path)

    def test_the_cli_refuses_an_unreadable_store_without_a_traceback(self, tmp_path, capsys):
        assert er.main(["--store", str(tmp_path / "nope.db"),
                        "--now-ms", str(fx.NOW)]) == 3
        assert "refusing" in capsys.readouterr().err


class TestProvenExitAllocation:
    """A reconciled provisional exit stores the execIds that priced it, so a
    partial exit split across trade rows CAN be proven, exactly."""

    def two_exit_execs(self):
        return [fx.exec_row(3, order=X_ORD, link="EXT-1", side="Sell", qty="0.001",
                            price="60100", fee="0.00006", t=fx.T0 + 600_000),
                fx.exec_row(4, order=X_ORD, link="EXT-1", side="Sell", qty="0.001",
                            price="60200", fee="0.00006", t=fx.T0 + 610_000)]

    def half(self, n, price, **over):
        t = trade(claim_id=f"T{n}", qty=0.001, exit_price=price,
                  gross_pnl=(price - 60005.0) * 0.001, entry_fee=0.00006,
                  exit_fee=0.00006, exit_exec_ids=[f"EXEC-{n + 2}"])
        t.update(over)
        return t

    def test_each_half_reconciles_when_the_executions_are_named(self):
        s = specs(exit_=self.two_exit_execs())
        r = go([self.half(1, 60100.0), self.half(2, 60200.0)], s)
        assert [v["resolved"] for v in r["trades"]] == [True, True]
        assert r["pnl_claim_allowed"] is True

    def test_without_the_execution_ids_the_split_is_unproven(self):
        s = specs(exit_=self.two_exit_execs())
        a = self.half(1, 60100.0)
        del a["exit_exec_ids"]
        assert "EXIT_ALLOCATION_UNPROVEN" in verdict(go([a], s))["issues"]

    def test_one_execution_cannot_price_two_trades(self):
        s = specs(exit_=self.two_exit_execs())
        r = go([self.half(1, 60100.0), self.half(2, 60100.0,
                                                 exit_exec_ids=["EXEC-3"])], s)
        assert all("EXIT_OVERCLAIMED" in v["issues"] and not v["resolved"]
                   for v in r["trades"])

    def test_a_named_execution_the_capture_lacks_blocks(self):
        s = specs(exit_=self.two_exit_execs())
        v = verdict(go([self.half(1, 60100.0, exit_exec_ids=["EXEC-99"])], s))
        assert "VENUE_DISAGREES:exit_exec_missing" in v["issues"]
        assert not v["resolved"]

    def test_the_price_must_be_that_executions_price(self):
        s = specs(exit_=self.two_exit_execs())
        v = verdict(go([self.half(1, 60200.0)], s))
        assert "VENUE_DISAGREES:exit_price" in v["issues"]

    def test_named_executions_that_do_not_add_up_to_the_trade_are_refused(self):
        s = specs(exit_=self.two_exit_execs())
        v = verdict(go([self.half(1, 60100.0, qty=0.002,
                                  gross_pnl=(60100.0 - 60005.0) * 0.002,
                                  entry_fee=0.00012)], s))
        assert not v["resolved"]

    def test_the_adapter_carries_the_reconciled_execution_ids(self, tmp_path):
        path = TestTheStoreAdapter().make_store(tmp_path)
        conn = sqlite3.connect(path)
        conn.execute("UPDATE trades SET meta = ?", (json.dumps({
            "exit_order_ids": [X_ORD],
            "reconciliation": {"evidence": {"exec_ids": ["EXEC-3", "EXEC-4"]}}}),))
        conn.commit()
        conn.close()
        c = er.claims_from_store(path)
        assert c["trade_claims"][0]["exit_exec_ids"] == ["EXEC-3", "EXEC-4"]
