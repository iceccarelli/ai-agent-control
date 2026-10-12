"""No operator surface may let one answer stand in for another.

Fixtures are synthetic test data and documented response shapes; none is an
observation of a real account.
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import ledger as L  # noqa: E402
import operator_verdicts as ov  # noqa: E402
from test_carry_preflight_economics import Market, preflight  # noqa: E402
from tools import carry_report  # noqa: E402
from persistence import StateStore  # noqa: E402

T0 = 1_800_000_000_000
H = 3_600_000


def hedged(qty=0.5):
    j = L.Journal()
    L.perp_open(j, ms=T0, ref="o1", qty=qty, price=100_000.0, fee_usd=1.0,
                fee_status="confirmed")
    return j


def by_key(qs):
    return {q["key"]: q for q in qs}


class TestPreflightOkIsOnlyReadiness:
    def test_readable_refused_economics_closed_gate_no_journal(self):
        report, _ = preflight(Market(rate_bps=-4.0), paper=True)
        q = by_key(report["questions"])
        assert report["verdict"] == "PREFLIGHT_OK"
        assert q["market_data"]["answer"] == "MARKET_DATA_READABLE"
        assert q["account_position"]["answer"] == "ACCOUNT_STATE_READABLE"
        assert q["entry_evaluator"]["answer"] == "ENTRY_ECONOMICS_REFUSED"
        assert q["entry_evaluator"]["evidence"]["reason"] == "FUNDING_NOT_POSITIVE"
        assert q["order_authorisation"]["answer"] == "GATE_REFUSES_ORDERS"
        # nothing about accounting or income is implied by a readable venue
        for k in ("book_position", "funding_attribution", "fees", "fills",
                  "net_pnl"):
            assert q[k]["answer"] in (
                "NO_JOURNAL", "FUNDING_UNKNOWN_NO_JOURNAL",
                "FEES_UNKNOWN_NO_JOURNAL", "FILLS_UNKNOWN_NO_JOURNAL",
                "NOT_AVAILABLE")
        assert "PREFLIGHT_OK answers questions 1-2 only" in " ".join(
            report["notes"])

    def test_an_economics_pass_is_not_an_open_gate(self):
        report, _ = preflight(Market(rate_bps=3.0), paper=True)
        q = by_key(report["questions"])
        assert q["entry_evaluator"]["answer"] == "ENTRY_ECONOMICS_PASS"
        assert q["order_authorisation"]["answer"] == "GATE_REFUSES_ORDERS"

    def test_an_economics_refusal_is_a_decision_not_an_operational_failure(self):
        report, _ = preflight(Market(rate_bps=-4.0), paper=True)
        assert report["verdict"] == "PREFLIGHT_OK"
        assert report["blockers"] == []
        assert report["entry_economics"]["state"] == "ENTRY_ECONOMICS_REFUSED"

    def test_api_connectivity_does_not_hide_an_accounting_failure(self):
        j = hedged()                               # never reconciled
        report, _ = _with_statement(j.statement())
        q = by_key(report["questions"])
        assert report["verdict"] == "PREFLIGHT_OK"
        assert q["market_data"]["answer"] == "MARKET_DATA_READABLE"
        assert q["funding_attribution"]["answer"] == "PROVISIONAL"
        assert "NO_RECONCILIATION_RECORD" in q["funding_attribution"][
            "evidence"]["reasons"]
        assert q["net_pnl"]["answer"] == "PROVISIONAL_FUNDING_UNVERIFIED"

    def test_the_summary_line_keeps_every_answer_separate(self):
        report, _ = preflight(Market(rate_bps=-4.0), paper=True)
        line = report["summary"]["questions"]
        for n in range(1, 10):
            assert f"{n}." in line
        assert line.count("|") == 8


def _with_statement(statement, market=None):
    import carry_broker
    from tools import carry_preflight as pf
    from test_carry_preflight_economics import cfg
    m = market or Market(rate_bps=3.0)
    broker = carry_broker.CarryBroker(
        client=m, sequence_source=lambda *a: 0,
        order_gate=lambda: (False, "preflight is read-only"))
    report = pf.run_preflight(
        broker=broker, client=SimpleNamespace(get_equity=lambda: 9800.0),
        cfg=cfg(True), spot_symbol="BTCUSDT", perp_symbol="BTCUSDT",
        execution_mode="overlay", known_perp_qty=0.0, execution_style="taker",
        borrow_apr=0.0, statement=statement)
    return report, broker


class TestAccountingQuestions:
    def test_no_fills_means_no_net_pnl_and_nothing_to_confirm(self):
        q = by_key(ov.nine_questions(statement=L.Journal().statement()))
        assert q["fills"]["answer"] == "NO_FILLS_BOOKED"
        assert q["fees"]["answer"] == "NO_FILLS_TO_CONFIRM"
        assert q["net_pnl"]["answer"] == "NONE_NO_FILLS"
        assert q["funding_attribution"]["answer"] == "NO_POSITION_LIFECYCLES"

    def test_missing_settlement_coverage_never_yields_confirmed_net_economics(self):
        j = hedged()
        q = by_key(ov.nine_questions(statement=j.statement()))
        assert q["fees"]["answer"] == "FEES_CONFIRMED"       # fees are fine ...
        assert q["net_pnl"]["answer"] != "VENUE_STATED_AND_ATTRIBUTED"  # ... net is not
        assert q["funding_attribution"]["answer"] == "PROVISIONAL"

    def test_only_a_fully_reconciled_book_with_confirmed_fees_is_attributed(self):
        from carry_broker import FundingHistory, SettlementEvidence
        import funding_reconcile as R
        j = hedged()
        now = T0 + 3 * H
        R.reconcile_funding(
            j, history=FundingHistory(start_ms=T0 - 2 * H, end_ms=now),
            evidence=SettlementEvidence(start_ms=T0 - 2 * H, end_ms=now,
                                        windows=1), now_ms=now)
        q = by_key(ov.nine_questions(statement=j.statement()))
        assert q["funding_attribution"]["answer"] == "RECONCILED_COMPLETE"
        assert q["net_pnl"]["answer"] == "VENUE_STATED_AND_ATTRIBUTED"

    def test_unconfirmed_fees_are_reported_even_if_funding_is_complete(self):
        j = L.Journal()
        L.perp_open(j, ms=T0, ref="o1", qty=0.5, price=100_000.0, fee_usd=0.0,
                    fee_status="unknown")
        q = by_key(ov.nine_questions(statement=j.statement()))
        assert q["fees"]["answer"] == "FEES_UNCONFIRMED"
        assert q["net_pnl"]["answer"].startswith("PROVISIONAL_")

    def test_journal_versus_venue_position_is_compared_not_assumed(self):
        st = hedged(0.5).statement()
        assert by_key(ov.nine_questions(statement=st, venue_perp_qty=0.5))[
            "book_position"]["answer"] == "JOURNAL_MATCHES_VENUE"
        assert by_key(ov.nine_questions(statement=st, venue_perp_qty=0.9))[
            "book_position"]["answer"] == "JOURNAL_DISAGREES_WITH_VENUE"
        assert by_key(ov.nine_questions(statement=st))[
            "book_position"]["answer"] == "JOURNAL_ONLY_NOT_COMPARED"

    def test_unreadable_reads_are_reported_unreadable(self):
        q = by_key(ov.nine_questions(reads={"mark_perp": False, "funding_bps": True,
                                            "perp_position": False}))
        assert q["market_data"]["answer"] == "MARKET_DATA_UNREADABLE"
        assert q["account_position"]["answer"] == "ACCOUNT_STATE_UNREADABLE"
        assert q["market_data"]["evidence"]["unreadable"] == ["mark_perp"]

    def test_answer_vocabularies_do_not_overlap_between_questions(self):
        seen = {}
        scenarios = [
            ov.nine_questions(),
            ov.nine_questions(statement=L.Journal().statement()),
            ov.nine_questions(statement=hedged().statement(), venue_perp_qty=0.5,
                              reads={"mark_perp": True, "perp_position": True},
                              economics={"state": "ENTRY_ECONOMICS_PASS"},
                              order_gate=True),
            ov.nine_questions(statement=hedged().statement(), venue_perp_qty=9.0,
                              reads={"mark_perp": False, "perp_position": False},
                              economics={"state": "ENTRY_ECONOMICS_REFUSED"},
                              order_gate=False),
        ]
        for qs in scenarios:
            for q in qs:
                seen.setdefault(q["answer"], set()).add(q["key"])
        shared = {a: k for a, k in seen.items() if len(k) > 1}
        # NOT_CHECKED is the one deliberately shared "I did not look" answer
        shared.pop("NOT_CHECKED", None)
        assert shared == {}


class TestReportConsumesPersistedTruth:
    def test_the_report_carries_lifecycles_questions_and_provenance(self, tmp_path):
        j = hedged()
        db = str(tmp_path / "r.db")
        s = StateStore(db)
        s.save_ledger(j.to_rows())
        s.close()
        rep = carry_report.build_report(db)["carry"]
        assert rep["status"] == "PROVISIONAL_FUNDING_UNVERIFIED"
        assert rep["funding_reconciled"] is False
        assert rep["funding_lifecycles"][0]["state"] == "NOT_RECONCILED"
        assert rep["net_pnl_provenance"] == "PROVISIONAL_FUNDING_UNVERIFIED"
        q = by_key(rep["questions"])
        assert q["market_data"]["answer"] == "NOT_CHECKED"
        assert q["order_authorisation"]["answer"] == "NOT_CHECKED"
        json.dumps(rep)

    def test_a_provisional_lifecycle_survives_the_database_round_trip(self, tmp_path):
        import funding_reconcile as R
        j = hedged()
        R.reconcile_funding(j, history=None, evidence=None, now_ms=T0 + 3 * H)
        db = str(tmp_path / "r.db")
        s = StateStore(db)
        s.save_ledger(j.to_rows())
        s.close()
        rep = carry_report.build_report(db)["carry"]
        assert rep["status"] == "PROVISIONAL_FUNDING_UNVERIFIED"
        assert "FUNDING_HISTORY_NOT_READ" in rep["funding_lifecycles"][0]["reasons"]

    def test_modelled_audit_output_cannot_be_read_as_journal_provenance(self):
        from tools import carry_oos_audit as oos
        from test_carry_oos_audit import trading_series
        rep = oos.run_oos(trading_series(8), counterfactuals=False)
        assert rep["evidence_class"] == "MODELLED_HISTORICAL_SIMULATION"
        assert rep["venue_observed_pnl"] is None
        assert "net_pnl_provenance" not in rep and "questions" not in rep
        vocabulary = {ov.net_pnl_provenance(b) for b in (
            "no_fills", "confirmed", "provisional_fees_incomplete",
            "provisional_funding_unverified")}
        assert rep["evidence_class"] not in vocabulary


class TestNewModulesCannotTradeOrArm:
    """The reconciliation, verdict and audit code is read-side only."""

    @pytest.mark.parametrize("name", ["funding_reconcile.py",
                                      "operator_verdicts.py",
                                      os.path.join("tools", "carry_oos_audit.py")])
    def test_no_order_call_and_no_arming_in_the_new_modules(self, name):
        src = open(os.path.join(os.path.dirname(HERE), name)).read()
        for banned in ("place_market", "place_post_only", "/v5/order/create",
                       "LIVE_TRADING_ACK", "PAPER_TRADING=0", "allows_live"):
            assert banned not in src, (name, banned)
