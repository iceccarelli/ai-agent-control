"""Offline evidence validation: four provenance classes, and nothing becomes
VENUE_RECONCILED without matching, corroborated, hash-verified venue rows.

All fixtures are synthetic (tests/evidence_fixtures.py). No network, no
credentials, no real trading record."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import evidence_reconcile as er  # noqa: E402
import evidence_fixtures as fx  # noqa: E402
import venue_evidence as ve  # noqa: E402


def run(claims, specs=None, **kw):
    cap = er.capture_from_records(fx.chain(specs) if specs is not None
                                  else fx.good_chain())
    return er.reconcile(fill_claims=claims, captures=[cap], now_ms=fx.NOW, **kw)


def fill(report, i=0):
    return report["fills"][i]


class TestTheFourClasses:
    def test_a_matching_corroborated_fill_is_reconciled(self):
        r = run([fx.claim()])
        assert fill(r)["provenance"] == er.VENUE_RECONCILED
        assert fill(r)["resolved"] is True and fill(r)["issues"] == []
        assert r["pnl_claim_allowed"] is True

    def test_a_simulated_fill_is_never_venue_evidence(self):
        r = run([fx.claim(source="simulation", environment="simulation")])
        assert fill(r)["provenance"] == er.SIMULATED
        assert not fill(r)["resolved"] and r["pnl_claim_allowed"] is False

    def test_a_simulation_that_names_a_venue_environment_is_flagged(self):
        r = run([fx.claim(source="simulation", environment="testnet")])
        assert fill(r)["provenance"] == er.SIMULATED
        assert "SIMULATION_CLAIMS_A_VENUE" in fill(r)["issues"]

    def test_a_local_record_is_unresolved(self):
        r = run([fx.claim(source="local", environment="unknown")])
        assert fill(r)["provenance"] == er.LOCAL_RECORD
        assert not fill(r)["resolved"]

    def test_a_testnet_claim_with_no_capture_stays_a_claim(self):
        r = run([fx.claim()], specs=[])
        assert fill(r)["provenance"] == er.CLAIMED_TESTNET_FILL
        assert "NOT_IN_CAPTURE" in fill(r)["issues"] and not fill(r)["resolved"]

    @pytest.mark.parametrize("drop", ["exec_id", "order_id"])
    def test_a_testnet_claim_without_venue_ids_is_only_local(self, drop):
        r = run([fx.claim(**{drop: ""})])
        assert fill(r)["provenance"] == er.LOCAL_RECORD
        assert "NO_VENUE_IDENTIFIERS" in fill(r)["issues"]

    def test_a_mainnet_claim_is_not_accepted_as_evidence(self):
        r = run([fx.claim(environment="mainnet")])
        assert "MAINNET_EVIDENCE_NOT_ACCEPTED" in fill(r)["issues"]
        assert not fill(r)["resolved"]


class TestMatchingIsFieldForField:
    @pytest.mark.parametrize("field,value,code", [
        ("qty", 0.003, "qty"), ("price", 60001.0, "price"),
        ("fee", 0.5, "fee"), ("exec_time_ms", fx.T0 + 1001, "exec_time_ms"),
        ("side", "Sell", "side"), ("symbol", "ETHUSDT", "symbol"),
        ("order_id", "ORD-9", "order_id"), ("order_link_id", "OTHER", "order_link_id")])
    def test_any_disagreement_with_the_venue_row_blocks(self, field, value, code):
        r = run([fx.claim(**{field: value})])
        assert f"VENUE_DISAGREES:{code}" in fill(r)["issues"]
        assert fill(r)["provenance"] == er.CLAIMED_TESTNET_FILL
        assert not fill(r)["resolved"] and r["pnl_claim_allowed"] is False

    def test_string_and_float_forms_of_one_number_match(self):
        r = run([fx.claim(qty="0.0020", price="60000.00", fee="0.000120")])
        assert fill(r)["resolved"]

    def test_a_rebate_with_a_negative_fee_matches_when_the_venue_says_so(self):
        specs = [dict(path="/v5/execution/list", rows=[fx.exec_row(1, fee="-0.0001")]),
                 dict(path="/v5/order/history", rows=[fx.order_row()])]
        r = run([fx.claim(fee=-0.0001)], specs)
        assert fill(r)["resolved"]


class TestCorroboration:
    def test_an_execution_row_alone_is_not_enough(self):
        r = run([fx.claim()], [dict(path="/v5/execution/list",
                                    rows=[fx.exec_row(1)])])
        assert "NO_CORROBORATION" in fill(r)["issues"] and not fill(r)["resolved"]

    def test_an_order_row_for_a_different_side_does_not_corroborate(self):
        specs = [dict(path="/v5/execution/list", rows=[fx.exec_row(1)]),
                 dict(path="/v5/order/history", rows=[fx.order_row(side="Sell")])]
        assert "NO_CORROBORATION" in fill(run([fx.claim()], specs))["issues"]

    def test_an_order_whose_cum_qty_is_below_the_fill_does_not_corroborate(self):
        specs = [dict(path="/v5/execution/list", rows=[fx.exec_row(1)]),
                 dict(path="/v5/order/history", rows=[fx.order_row(cum="0.001")])]
        assert "NO_CORROBORATION" in fill(run([fx.claim()], specs))["issues"]

    def test_a_ws_execution_event_corroborates(self):
        # build by hand (build_ws_event_record stamps the live git commit)
        base = fx.record("/v5/execution/list", [fx.exec_row(1)],
                         prev=ve.GENESIS_PREV_HASH)
        wsrec = ve.EvidenceRecord(
            venue="bybit", environment="testnet",
            captured_at_utc=fx.iso(fx.T0 + 61_000), method="WS_EVENT",
            request_url="wss://stream-testnet.bybit.com/v5/private",
            request={"topic": "execution"},
            response={"data": [fx.exec_row(1)]}, order_link_id="",
            git_commit=fx.GIT, prev_hash=base["record_hash"], transport="ws",
            venue_event_id="e1").to_dict()
        cap = er.capture_from_records([base, wsrec])
        r = er.reconcile(fill_claims=[fx.claim()], captures=[cap], now_ms=fx.NOW)
        assert fill(r)["resolved"]


class TestTheCaptureItselfMustBeTrusted:
    def test_an_edited_record_breaks_the_chain_and_every_claim(self):
        recs = copy.deepcopy(fx.good_chain())
        recs[0]["response"]["result"]["list"][0]["execQty"] = "0.002"
        recs[0]["response"]["result"]["list"][0]["execPrice"] = "1"
        cap = er.capture_from_records(recs)
        assert not cap.intact
        r = er.reconcile(fill_claims=[fx.claim()], captures=[cap], now_ms=fx.NOW)
        assert "CAPTURE_CHAIN_BROKEN" in fill(r)["issues"] and not fill(r)["resolved"]

    def test_a_deleted_or_reordered_record_breaks_the_chain(self):
        recs = fx.good_chain()
        assert not er.capture_from_records(recs[1:]).intact
        assert not er.capture_from_records(list(reversed(recs))).intact

    def test_a_lookalike_host_is_not_a_testnet_capture(self):
        specs = [dict(path="/v5/execution/list", rows=[fx.exec_row(1)],
                      host="https://api-testnet.bybit.com.evil.io"),
                 dict(path="/v5/order/history", rows=[fx.order_row()],
                      host="https://api-testnet.bybit.com.evil.io")]
        r = run([fx.claim()], specs)
        assert "NOT_IN_CAPTURE" in fill(r)["issues"] and not fill(r)["resolved"]

    def test_plain_http_is_not_a_venue_capture(self):
        specs = [dict(path="/v5/execution/list", rows=[fx.exec_row(1)],
                      host="http://api-testnet.bybit.com")]
        assert "NOT_IN_CAPTURE" in fill(run([fx.claim()], specs))["issues"]

    def test_a_capture_from_another_venue_cannot_confirm_a_testnet_claim(self):
        specs = [dict(path="/v5/execution/list", rows=[fx.exec_row(1)],
                      env="demo", host="https://api-demo.bybit.com"),
                 dict(path="/v5/order/history", rows=[fx.order_row()],
                      env="demo", host="https://api-demo.bybit.com")]
        r = run([fx.claim()], specs)
        assert "ENV_MISMATCH" in fill(r)["issues"] and not fill(r)["resolved"]

    def test_a_failed_response_contributes_nothing(self):
        specs = [dict(path="/v5/execution/list", rows=[fx.exec_row(1)], ret=10001),
                 dict(path="/v5/order/history", rows=[fx.order_row()])]
        assert "NOT_IN_CAPTURE" in fill(run([fx.claim()], specs))["issues"]

    def test_a_capture_stamped_before_the_execution_is_impossible(self):
        specs = [dict(path="/v5/execution/list", rows=[fx.exec_row(1)],
                      at=fx.T0 - 3_600_000),
                 dict(path="/v5/order/history", rows=[fx.order_row()])]
        assert "CAPTURED_BEFORE_EXECUTION" in fill(run([fx.claim()], specs))["issues"]

    def test_two_different_rows_for_one_exec_id_contradict(self):
        specs = [dict(path="/v5/execution/list", rows=[fx.exec_row(1)]),
                 dict(path="/v5/execution/list",
                      rows=[fx.exec_row(1, price="59000")]),
                 dict(path="/v5/order/history", rows=[fx.order_row()])]
        assert "VENUE_ROWS_CONTRADICT" in fill(run([fx.claim()], specs))["issues"]

    def test_an_unreadable_or_garbled_file_is_a_broken_chain_not_a_crash(self, tmp_path):
        assert not er.load_capture(str(tmp_path / "missing.jsonl")).intact
        bad = tmp_path / "bad.jsonl"
        bad.write_text("not json\n[1,2]\n")
        cap = er.load_capture(str(bad))
        assert not cap.intact and len(cap.chain_issues) == 2


class TestIdentityAndDuplicates:
    def test_an_identical_replay_counts_once_and_is_flagged(self):
        r = run([fx.claim(), fx.claim()])
        assert fill(r, 0)["resolved"] and fill(r, 0)["counted"]
        assert fill(r, 1)["counted"] is False
        assert "DUPLICATE_EXEC_ID" in fill(r, 1)["issues"]
        assert r["pnl_claim_allowed"] is False

    def test_conflicting_copies_of_one_exec_id_are_both_unresolved(self):
        r = run([fx.claim(), fx.claim(price=61000.0)])
        for i in (0, 1):
            assert "CONFLICTING_DUPLICATE" in fill(r, i)["issues"]
            assert not fill(r, i)["resolved"]

    def test_one_link_id_on_two_orders_is_ambiguous(self):
        r = run([fx.claim(), fx.claim(2, order_id="ORD-2")])
        assert "AMBIGUOUS_ORDER_LINK_ID" in fill(r, 0)["issues"]
        assert not fill(r, 0)["resolved"]


class TestMalformedClaims:
    @pytest.mark.parametrize("over,code", [
        ({"qty": float("nan")}, "BAD_QTY"), ({"qty": 0}, "BAD_QTY"),
        ({"qty": -1}, "BAD_QTY"), ({"price": float("inf")}, "BAD_PRICE"),
        ({"price": 0}, "BAD_PRICE"), ({"fee": float("nan")}, "BAD_FEE"),
        ({"fee": "n/a"}, "BAD_FEE"), ({"side": "buy"}, "BAD_SIDE"),
        ({"exec_time_ms": 0}, "BAD_TIMESTAMP"),
        ({"exec_time_ms": fx.NOW + 10 * 3_600_000}, "BAD_TIMESTAMP"),
        ({"exec_time_ms": 1.5}, "BAD_TIMESTAMP"),
        ({"source": "venue"}, "BAD_SOURCE"),
        ({"environment": "prod"}, "BAD_ENVIRONMENT"),
        ({"source": "testnet_claim", "environment": "unknown"},
         "TESTNET_CLAIM_ENVIRONMENT_MISMATCH")])
    def test_a_malformed_field_is_named_and_unresolved(self, over, code):
        r = run([fx.claim(**over)])
        assert code in fill(r)["issues"] and not fill(r)["resolved"]

    @pytest.mark.parametrize("name", ["claim_id", "symbol", "side", "qty",
                                      "price", "fee", "exec_time_ms"])
    def test_a_missing_field_is_named(self, name):
        c = fx.claim()
        del c[name]
        assert f"MISSING_FIELD:{name}" in fill(run([c]))["issues"]

    def test_a_blank_fee_is_missing_not_zero(self):
        assert "MISSING_FIELD:fee" in fill(run([fx.claim(fee="")]))["issues"]

    def test_a_boolean_is_not_a_quantity(self):
        assert "BAD_QTY" in fill(run([fx.claim(qty=True)]))["issues"]


class TestPartialFillsAndOrderCompleteness:
    def two_fill_specs(self, cum="0.004"):
        return [dict(path="/v5/execution/list",
                     rows=[fx.exec_row(1, qty="0.002"), fx.exec_row(2, qty="0.002")]),
                dict(path="/v5/order/history", rows=[fx.order_row(cum=cum)])]

    def test_all_fills_of_the_order_make_it_complete(self):
        r = run([fx.claim(1), fx.claim(2)], self.two_fill_specs())
        assert all(f["resolved"] for f in r["fills"])
        assert r["orders"][0]["complete"] is True and r["pnl_claim_allowed"]

    def test_a_missing_fill_leaves_the_order_incomplete(self):
        r = run([fx.claim(1)], self.two_fill_specs())
        assert fill(r)["resolved"]                       # the fill itself is real
        assert r["orders"][0]["complete"] is False
        assert r["pnl_claim_allowed"] is False           # but the order is not whole

    def test_an_over_claimed_order_is_not_complete(self):
        r = run([fx.claim(1), fx.claim(2)], self.two_fill_specs(cum="0.003"))
        assert r["orders"][0]["complete"] is False and not r["pnl_claim_allowed"]


class TestFunding:
    T = fx.T0 + 8 * 3_600_000

    def f_claim(self, **over):
        c = {"claim_id": "F1", "source": "testnet_claim", "environment": "testnet",
             "symbol": "BTCUSDT", "time_ms": self.T, "amount": -0.05}
        c.update(over)
        return c

    def go(self, funding, row=None):
        specs = [dict(path="/v5/execution/list", rows=[fx.exec_row(1)]),
                 dict(path="/v5/order/history", rows=[fx.order_row()]),
                 dict(path="/v5/account/transaction-log",
                      rows=[row or fx.settlement_row(t=self.T)],
                      at=self.T + 60_000)]
        cap = er.capture_from_records(fx.chain(specs))
        return er.reconcile(fill_claims=[fx.claim()], funding_claims=funding,
                            captures=[cap], now_ms=fx.NOW + 9 * 3_600_000)

    def test_a_confirmed_settlement_reconciles(self):
        r = self.go([self.f_claim()])
        assert r["funding"][0]["provenance"] == er.VENUE_RECONCILED
        assert r["pnl_claim_allowed"] is True

    def test_the_wrong_amount_is_refused(self):
        r = self.go([self.f_claim(amount=-0.5)])
        assert "VENUE_DISAGREES:amount" in r["funding"][0]["issues"]
        assert r["pnl_claim_allowed"] is False

    def test_a_row_whose_change_contradicts_its_funding_is_not_trusted(self):
        r = self.go([self.f_claim()],
                    fx.settlement_row(t=self.T, funding="-0.05", change="0.05"))
        assert "VENUE_SIGN_MISMATCH" in r["funding"][0]["issues"]

    def test_a_row_with_no_change_field_is_unverified(self):
        row = fx.settlement_row(t=self.T)
        del row["change"]
        assert "VENUE_SIGN_UNVERIFIED" in self.go([self.f_claim()], row)[
            "funding"][0]["issues"]

    def test_an_unobserved_settlement_stays_a_claim(self):
        r = self.go([self.f_claim(time_ms=self.T - 8 * 3_600_000)])
        assert "NOT_IN_CAPTURE" in r["funding"][0]["issues"]

    def test_a_duplicate_settlement_counts_once(self):
        r = self.go([self.f_claim(), self.f_claim(claim_id="F2")])
        assert r["funding"][1]["counted"] is False
        assert "DUPLICATE_SETTLEMENT" in r["funding"][1]["issues"]

    def test_a_non_settlement_row_is_never_matched(self):
        row = fx.settlement_row(t=self.T)
        row["type"] = "TRADE"
        assert "NOT_IN_CAPTURE" in self.go([self.f_claim()], row)["funding"][0]["issues"]


class TestLocalLedgerSummaries:
    def ledger(self, **over):
        d = {"schema": "cash_ledger/2", "allows_live": False, "fills": 1,
             "fees_usd": 0.00012, "fees_confirmed": True,
             "funding_status": "CONFIRMED_FROM_SETTLEMENTS"}
        d.update(over)
        return d

    def summary(self, ledger, claims=None):
        r = run(claims if claims is not None else [fx.claim()], ledgers=[ledger])
        return r, r["ledger_summaries"][0]

    def test_a_ledger_is_always_a_local_record_without_identifiers(self):
        r, s = self.summary(self.ledger())
        assert s["provenance"] == er.LOCAL_RECORD and s["resolved"] is False
        assert "LEDGER_SUMMARY_HAS_NO_FILL_IDENTIFIERS" in s["issues"]

    def test_a_ledger_that_agrees_does_not_block_reconciled_fills(self):
        r, _ = self.summary(self.ledger())
        assert r["pnl_claim_allowed"] is True

    @pytest.mark.parametrize("over,code", [
        ({"fills": 2}, "LEDGER_FILL_COUNT_DISAGREES"),
        ({"fills": None}, "LEDGER_FILL_COUNT_MISSING"),
        ({"fees_usd": 9.0}, "LEDGER_FEES_DISAGREE_WITH_FILLS"),
        ({"fees_usd": None}, "LEDGER_FEES_MISSING"),
        ({"fees_confirmed": False}, "LEDGER_FEES_UNCONFIRMED"),
        ({"funding_status": "UNCONFIRMED"}, "LEDGER_FUNDING_UNCONFIRMED"),
        ({"funding_status": "NO_SETTLEMENT_OBSERVED"},
         "LEDGER_FUNDING_NO_SETTLEMENT_OBSERVED"),
        ({"allows_live": True}, "LEDGER_DOES_NOT_STATE_ALLOWS_LIVE_FALSE"),
        ({"schema": "cash_ledger/9"}, "UNKNOWN_LEDGER_SCHEMA")])
    def test_a_disagreeing_ledger_blocks_the_pnl_claim(self, over, code):
        r, s = self.summary(self.ledger(**over))
        assert code in s["issues"] and r["pnl_claim_allowed"] is False

    def test_the_committed_cash_ledger_is_a_local_record_with_no_venue_ids(self):
        path = os.path.join(os.path.dirname(HERE), "artifacts", "cash_ledger.json")
        payload = json.load(open(path))
        r = er.reconcile(ledgers=[payload], now_ms=fx.NOW)
        s = r["ledger_summaries"][0]
        assert s["provenance"] == er.LOCAL_RECORD and not s["resolved"]
        assert r["pnl_claim_allowed"] is False


class TestNothingIsInferred:
    def test_no_input_allows_no_claim(self):
        assert er.reconcile(now_ms=fx.NOW)["pnl_claim_allowed"] is False

    def test_a_ledger_alone_never_allows_a_claim(self):
        r = er.reconcile(ledgers=[{"schema": "cash_ledger/2", "allows_live": False,
                                   "fills": 3, "fees_usd": 1.0,
                                   "fees_confirmed": True,
                                   "funding_status": "CONFIRMED_FROM_SETTLEMENTS"}],
                         now_ms=fx.NOW)
        assert r["pnl_claim_allowed"] is False

    def test_the_inputs_are_not_mutated(self):
        claims = [fx.claim()]
        before = copy.deepcopy(claims)
        recs = fx.good_chain()
        snap = copy.deepcopy(recs)
        er.reconcile(fill_claims=claims, captures=[er.capture_from_records(recs)],
                     now_ms=fx.NOW)
        assert claims == before and recs == snap

    def test_the_report_has_no_pnl_number(self):
        text = json.dumps(run([fx.claim()]))
        assert "net_pnl" not in text and "final_pnl" not in text


class TestTheCli:
    def write(self, tmp_path, recs):
        p = tmp_path / "evidence" / "chain.jsonl"
        p.parent.mkdir()
        p.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in recs))
        return p

    def test_it_reads_evidence_read_only_and_exits_by_the_verdict(self, tmp_path, capsys):
        ev = self.write(tmp_path, fx.good_chain())
        before = hashlib.sha256(ev.read_bytes()).hexdigest()
        claims = tmp_path / "claims.json"
        claims.write_text(json.dumps({"fill_claims": [fx.claim()]}))
        out = tmp_path / "out" / "report.json"
        code = er.main(["--claims", str(claims), "--evidence", str(ev),
                        "--now-ms", str(fx.NOW), "--json", str(out)])
        assert code == 0 and json.load(open(out))["pnl_claim_allowed"] is True
        assert hashlib.sha256(ev.read_bytes()).hexdigest() == before

    def test_an_unresolved_claim_exits_nonzero(self, tmp_path):
        ev = self.write(tmp_path, fx.good_chain())
        claims = tmp_path / "claims.json"
        claims.write_text(json.dumps({"fill_claims": [fx.claim(price=1.0)]}))
        assert er.main(["--claims", str(claims), "--evidence", str(ev),
                        "--now-ms", str(fx.NOW)]) == 2

    def test_it_will_not_write_a_report_beside_the_evidence(self, tmp_path):
        ev = self.write(tmp_path, fx.good_chain())
        assert er.main(["--evidence", str(ev), "--now-ms", str(fx.NOW),
                        "--json", str(ev.parent / "report.json")]) == 3
        assert not (ev.parent / "report.json").exists()

    def test_the_reference_time_is_required(self):
        with pytest.raises(SystemExit):
            er.main([])


class TestTheHostCheckInTheCaptureLayerToo:
    @pytest.mark.parametrize("url,expected", [
        ("https://api.bybit.com/v5/x", "mainnet"),
        ("https://api-testnet.bybit.com/v5/x", "testnet"),
        ("https://api-demo.bybit.com/v5/x", "demo"),
        ("wss://stream-testnet.bybit.com/v5/private", "testnet"),
        ("https://api.bybit.com.evil.io/v5/x", "unknown"),
        ("https://evil.io/api.bybit.com", "unknown"),
        ("http://api.bybit.com/v5/x", "unknown"),
        ("https://api-testnet.bybit.com@evil.io/", "unknown"),
        ("", "unknown")])
    def test_the_environment_is_read_from_the_exact_host(self, url, expected):
        assert ve.infer_environment_from_url(url) == expected

    def test_a_lookalike_cannot_be_filed_as_testnet_evidence(self):
        with pytest.raises(ve.EvidenceRefused):
            ve.validate_environment_claim(
                declared_environment="testnet",
                request_url="https://api-testnet.bybit.com.evil.io/v5/x")


class TestFindingsFromTheAdversarialReview:
    """Each of these was reproduced against the first version of the validator."""

    def test_two_environments_do_not_pool_one_order_id(self):
        specs = [
            dict(path="/v5/execution/list", rows=[fx.exec_row(1, qty="0.001"),
                                                  fx.exec_row(2, qty="0.001")]),
            dict(path="/v5/order/history", rows=[fx.order_row(cum="0.002")]),
            dict(path="/v5/execution/list", rows=[fx.exec_row(1, qty="0.001"),
                                                  fx.exec_row(2, qty="0.001")],
                 env="demo", host="https://api-demo.bybit.com"),
            dict(path="/v5/order/history", rows=[fx.order_row(cum="0.002")],
                 env="demo", host="https://api-demo.bybit.com")]
        claims = [fx.claim(1, qty=0.001), fx.claim(2, qty=0.001, environment="demo")]
        # claim 2 uses EXEC-2 on demo; each environment's order is half covered
        r = run(claims, specs)
        assert [o["complete"] for o in r["orders"]] == [False, False]
        assert r["pnl_claim_allowed"] is False

    def test_an_unparseable_capture_time_blocks_instead_of_skipping_the_check(self):
        recs = fx.good_chain()
        recs[0]["captured_at_utc"] = "garbage"
        body = {k: v for k, v in recs[0].items() if k != "record_hash"}
        recs[0]["record_hash"] = ve.content_hash(body)
        recs[1]["prev_hash"] = recs[0]["record_hash"]
        body1 = {k: v for k, v in recs[1].items() if k != "record_hash"}
        recs[1]["record_hash"] = ve.content_hash(body1)
        r = er.reconcile(fill_claims=[fx.claim()],
                         captures=[er.capture_from_records(recs)], now_ms=fx.NOW)
        assert "CAPTURE_TIME_INVALID" in fill(r)["issues"] and not fill(r)["resolved"]

    @pytest.mark.parametrize("kind", ["Funding", "BustTrade", "AdlTrade", ""])
    def test_only_an_ordinary_trade_row_is_a_fill(self, kind):
        row = fx.exec_row(1)
        row["execType"] = kind
        specs = [dict(path="/v5/execution/list", rows=[row]),
                 dict(path="/v5/order/history", rows=[fx.order_row()])]
        r = run([fx.claim()], specs)
        assert "VENUE_DISAGREES:exec_type" in fill(r)["issues"]

    def test_a_stated_fee_currency_must_match(self):
        r = run([fx.claim(fee_currency="BTC")])
        assert "VENUE_DISAGREES:fee_currency" in fill(r)["issues"]
        assert fill(run([fx.claim(fee_currency="USDT")]))["resolved"]

    def test_a_ws_event_must_restate_the_same_fill(self):
        base = fx.record("/v5/execution/list", [fx.exec_row(1)],
                         prev=ve.GENESIS_PREV_HASH)
        lie = dict(fx.exec_row(1), orderId="OTHER", execQty="999")
        wsrec = ve.EvidenceRecord(
            venue="bybit", environment="testnet",
            captured_at_utc=fx.iso(fx.T0 + 61_000), method="WS_EVENT",
            request_url="wss://stream-testnet.bybit.com/v5/private",
            request={"topic": "execution"}, response={"data": [lie]},
            order_link_id="", git_commit=fx.GIT, prev_hash=base["record_hash"],
            transport="ws", venue_event_id="e1").to_dict()
        r = er.reconcile(fill_claims=[fx.claim()],
                         captures=[er.capture_from_records([base, wsrec])],
                         now_ms=fx.NOW)
        assert "NO_CORROBORATION" in fill(r)["issues"] and not fill(r)["resolved"]

    @pytest.mark.parametrize("host", [
        "https://api-testnet.bybit.com:9999", "https://user@api-testnet.bybit.com",
        "https://api-testnet.bybit.com:99999"])
    def test_a_port_or_userinfo_is_not_the_exact_host(self, host):
        specs = [dict(path="/v5/execution/list", rows=[fx.exec_row(1)], host=host),
                 dict(path="/v5/order/history", rows=[fx.order_row()], host=host)]
        assert "NOT_IN_CAPTURE" in fill(run([fx.claim()], specs))["issues"]

    def test_the_default_https_port_is_still_the_exact_host(self):
        host = "https://api-testnet.bybit.com:443"
        specs = [dict(path="/v5/execution/list", rows=[fx.exec_row(1)], host=host),
                 dict(path="/v5/order/history", rows=[fx.order_row()], host=host)]
        assert fill(run([fx.claim()], specs))["resolved"]

    def test_the_latest_order_snapshot_is_used_for_completeness(self):
        specs = [dict(path="/v5/execution/list",
                      rows=[fx.exec_row(1, qty="0.001"), fx.exec_row(2, qty="0.001")]),
                 dict(path="/v5/order/realtime", rows=[fx.order_row(cum="0.001",
                                                                    status="PartiallyFilled")]),
                 dict(path="/v5/order/history", rows=[fx.order_row(cum="0.002")])]
        r = run([fx.claim(1, qty=0.001), fx.claim(2, qty=0.001)], specs)
        assert r["orders"][0]["complete"] is True and r["pnl_claim_allowed"]

    def test_one_row_present_in_two_environments_resolves_in_the_claimed_one(self):
        specs = [dict(path="/v5/execution/list", rows=[fx.exec_row(1)]),
                 dict(path="/v5/execution/list", rows=[fx.exec_row(1)], env="demo",
                      host="https://api-demo.bybit.com"),
                 dict(path="/v5/order/history", rows=[fx.order_row()], env="demo",
                      host="https://api-demo.bybit.com")]
        r = run([fx.claim(environment="demo")], specs)
        assert fill(r)["resolved"]

    def test_a_funding_capture_older_than_the_settlement_is_impossible(self):
        T = fx.T0 + 8 * 3_600_000
        specs = [dict(path="/v5/account/transaction-log",
                      rows=[fx.settlement_row(t=T)], at=T - 3_600_000)]
        cap = er.capture_from_records(fx.chain(specs))
        r = er.reconcile(funding_claims=[{
            "claim_id": "F", "source": "testnet_claim", "environment": "testnet",
            "symbol": "BTCUSDT", "time_ms": T, "amount": -0.05}],
            captures=[cap], now_ms=T + 9 * 3_600_000)
        assert "CAPTURED_BEFORE_EXECUTION" in r["funding"][0]["issues"]


class TestMalformedInputNeverRaises:
    @pytest.mark.parametrize("bad", [None, "x", 7, ["a"], {"claim_id": "c"}])
    def test_a_non_claim_is_reported_not_raised(self, bad):
        r = run([bad])
        assert not fill(r)["resolved"] and r["pnl_claim_allowed"] is False

    @pytest.mark.parametrize("over", [
        {"side": ["Buy"]}, {"side": {"a": 1}}, {"qty": 10 ** 400},
        {"exec_time_ms": 10 ** 400}, {"price": "１２"}, {"qty": "1e999"}])
    def test_hostile_field_values_are_refused_not_raised(self, over):
        assert not fill(run([fx.claim(**over)]))["resolved"]

    def test_none_inputs_are_empty(self):
        r = er.reconcile(fill_claims=None, funding_claims=None, now_ms=fx.NOW)
        assert r["pnl_claim_allowed"] is False

    def test_a_non_mapping_ledger_is_a_malformed_local_record(self):
        r = er.reconcile(ledgers=["x"], now_ms=fx.NOW)
        assert "MALFORMED_LEDGER" in r["ledger_summaries"][0]["issues"]

    def test_hash_valid_but_malformed_capture_records_are_skipped(self):
        def seal(rec, prev):
            rec["prev_hash"] = prev
            rec["record_hash"] = ve.content_hash(
                {k: v for k, v in rec.items() if k != "record_hash"})
            return rec
        good = fx.good_chain()
        weird = [
            {"environment": "testnet", "request_url": "https://[x/", "response": {},
             "method": "GET"},
            {"environment": "testnet", "transport": "ws", "request": "x",
             "request_url": "wss://stream-testnet.bybit.com/v5/private",
             "response": {"data": "nope"}},
            {"environment": "testnet", "request_url": fx.TESTNET_REST +
             "/v5/execution/list", "method": "GET", "response": "text"}]
        chain, prev = [], ve.GENESIS_PREV_HASH
        for rec in weird + good:
            chain.append(seal(dict(rec), prev))
            prev = chain[-1]["record_hash"]
        cap = er.capture_from_records(chain + [None])
        r = er.reconcile(fill_claims=[fx.claim()], captures=[cap], now_ms=fx.NOW)
        assert fill(r)["issues"] == ["CAPTURE_CHAIN_BROKEN"] or \
            "CAPTURE_CHAIN_BROKEN" in fill(r)["issues"]      # the None breaks it
        cap2 = er.capture_from_records(chain)
        r2 = er.reconcile(fill_claims=[fx.claim()], captures=[cap2], now_ms=fx.NOW)
        assert fill(r2)["resolved"]


class TestTheCliRefusesUnreadableInputsCleanly:
    """A session with zero fills writes no ledger; pointing the validator at the
    missing file used to end in a traceback."""

    def test_a_missing_ledger_is_a_refusal_not_a_traceback(self, tmp_path, capsys):
        code = er.main(["--ledger", str(tmp_path / "absent.json"),
                        "--now-ms", str(fx.NOW)])
        assert code == 3
        assert "not readable" in capsys.readouterr().err

    def test_a_missing_claims_file(self, tmp_path, capsys):
        assert er.main(["--claims", str(tmp_path / "absent.json"),
                        "--now-ms", str(fx.NOW)]) == 3

    @pytest.mark.parametrize("text", ["not json", "[1, 2]", "", "null"])
    def test_malformed_inputs(self, tmp_path, capsys, text):
        bad = tmp_path / "bad.json"
        bad.write_text(text)
        for flag in ("--ledger", "--claims"):
            assert er.main([flag, str(bad), "--now-ms", str(fx.NOW)]) == 3
        assert "refusing" in capsys.readouterr().err

    def test_a_missing_evidence_file_is_a_broken_chain_in_the_report(self, tmp_path, capsys):
        out = tmp_path / "r" / "r.json"
        code = er.main(["--evidence", str(tmp_path / "absent.jsonl"),
                        "--now-ms", str(fx.NOW), "--json", str(out)])
        report = json.load(open(out))
        assert code == 2 and report["captures"][0]["intact"] is False
