"""The job summary says what the reconciliation says, and what it does not."""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import recon_summary as S  # noqa: E402
import test_evidence_reconcile_carry as C  # noqa: E402


def text(lines):
    return "\n".join(lines)


class TestSummarise:
    def test_a_resolved_cycle_states_its_numbers_and_its_limits(self):
        out = text(S.summarise(C.run()))
        assert "cycle: VENUE_RECONCILED resolved=True" in out
        assert "NET " in out and "fees" in out and "funding" in out
        assert "records_consistent: True" in out
        assert "says nothing about edge" in out

    def test_a_tampered_capture_shows_the_issue_and_no_net(self):
        records = C.build(tweak=C._alter("exec_open", execQty="0.002"))
        out = text(S.summarise(C.run(records)))
        assert "VENUE_DISAGREES:qty" in out and "NET " not in out
        assert "records_consistent: False" in out

    def test_missing_or_unreadable_input_claims_nothing(self):
        for junk in (None, [], "x", {}, {"schema": "other"}):
            assert "nothing is claimed" in text(S.summarise(junk))

    def test_the_command_line_reads_a_file(self, tmp_path, capsys):
        path = tmp_path / "r.json"
        path.write_text(json.dumps(C.run()))
        assert S.main([str(path)]) == 0
        assert "records_consistent" in capsys.readouterr().out

    def test_a_missing_file_does_not_raise(self, tmp_path, capsys):
        assert S.main([str(tmp_path / "nope.json")]) == 0
        assert "nothing is claimed" in capsys.readouterr().out
        assert S.main([]) == 3


class TestMalformedReportsNeverRaise:
    import pytest as _pytest

    @_pytest.mark.parametrize("patch", [
        {"carry_legs": None}, {"carry_legs": ["x"]}, {"carry_legs": [None]},
        {"carry_cycle": [{"economics": {"net_usdt": 1}}]},
        {"carry_cycle": [{"economics": {"net_usdt": 1, "entry_price": None,
                                        "exit_price": 1, "qty": 1,
                                        "perp_gross_usdt": None, "fees_usdt": 1,
                                        "fee_bps_of_entry_notional": 1,
                                        "funding_usdt": 0, "settlements": 0,
                                        "hold_ms": 1}}]},
        {"carry_cycle": [{"issues": 5}]}, {"funding": [None]},
        {"funding": "x"}, {"carry_cycle": "x"}])
    def test_a_report_it_cannot_read_claims_nothing(self, patch):
        report = dict(C.run(), **patch)
        out = "\n".join(S.summarise(report))
        assert "nothing is claimed" in out and "NET " not in out


class TestAnInconsistentReportIsNeverSummarisedAsAgreement:
    """`records_consistent: False` used to print the same 'the captured records
    agree' lines as a consistent report, so a failed reconciliation read as a
    pass in a job summary."""

    def _report(self, consistent):
        return {"schema": "evidence_reconciliation/1",
                "records_consistent": consistent,
                "carry_legs": [], "funding": [],
                "carry_cycle": [{"provenance": "CLAIMED_TESTNET_FILL",
                                 "resolved": False,
                                 "issues": ["CYCLE_NO_ENTRY"]}]}

    def test_a_failed_reconciliation_does_not_claim_agreement(self):
        text = "\n".join(S.summarise(self._report(False)))
        assert "records_consistent: False" in text
        assert "do NOT" in text
        assert "rows agree with the drill's books" not in text
        assert "records agree" not in text

    def test_a_consistent_report_keeps_its_wording(self):
        text = "\n".join(S.summarise(self._report(True)))
        assert "records_consistent: True (the captured records agree" in text
        assert "rows agree with the drill's books" in text

    @pytest.mark.parametrize("value", [None, "true", 1, "yes"])
    def test_only_a_literal_true_counts_as_consistent(self, value):
        text = "\n".join(S.summarise(self._report(value)))
        assert "rows agree with the drill's books" not in text


class TestALedgerThatBlocksIsNamedInTheSummary:
    """records_consistent=False used to print with no reason when the blocker
    was a cash ledger, because the summary showed legs, cycle and funding but
    never the ledger's own issues."""

    def test_the_ledger_issues_are_listed(self):
        report = C.run(ledgers=[C.legacy_ledger(fees_usd=0.5)])
        out = text(S.summarise(report))
        assert "ledger cash_ledger/2:" in out
        assert "LEDGER_FEES_DISAGREE_WITH_FILLS" in out

    def test_the_always_present_no_identifiers_note_is_not_noise(self):
        out = text(S.summarise(C.run(ledgers=[C.legacy_ledger()])))
        assert "LEDGER_SUMMARY_HAS_NO_FILL_IDENTIFIERS" not in out

    def test_a_clean_ledger_adds_no_ledger_line(self):
        assert "ledger cash_ledger" not in text(S.summarise(C.run(ledgers=[C.legacy_ledger()])))

    def test_a_ledger_that_misstates_funding_is_named(self):
        report = C.run(ledgers=[C.legacy_ledger(funding_settlements=7, funding_venue_usd=999.0)])
        assert "LEDGER_FUNDING_TOTAL_DISAGREES" in text(S.summarise(report))
