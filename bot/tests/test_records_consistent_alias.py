"""`records_consistent` is the name; `pnl_claim_allowed` is a deprecated alias.

The old name read like permission to claim profit. What the field means is that
the captured records agree with each other and with the venue; it says nothing
about profit. The new name says that. The old name stays, with the identical
value, so no consumer breaks, and the schema stays evidence_reconciliation/1 on
purpose: three readers match that string exactly.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import evidence_reconcile as ER  # noqa: E402
import recon_summary as S  # noqa: E402
import test_evidence_reconcile_carry as C  # noqa: E402


def scenarios():
    """A resolved report and several that are not (a tampered capture, an empty
    one, and the report with the claim fields removed)."""
    yield C.run()
    yield C.run(C.build(tweak=C._alter("exec_open", execQty="0.002")))
    yield C.run([])


def test_the_new_name_exists_and_is_a_bool():
    for report in scenarios():
        assert isinstance(report["records_consistent"], bool)


def test_the_deprecated_alias_has_the_identical_value_on_every_report():
    seen = set()
    for report in scenarios():
        assert "pnl_claim_allowed" in report
        assert report["pnl_claim_allowed"] is report["records_consistent"]
        seen.add(report["records_consistent"])
    assert seen == {True, False}, "the scenarios must exercise both values"


def test_the_schema_did_not_change():
    # three readers (run_outcomes, recon_summary, the monitor fixtures) match it
    assert C.run()["schema"] == "evidence_reconciliation/1"


def test_the_note_says_consistency_not_profit():
    note = C.run()["pnl_note"]
    assert "records" in note and "not profitability" in note
    assert "records_consistent" in note


def test_the_summary_labels_it_records_consistent_not_profit():
    out = "\n".join(S.summarise(C.run()))
    assert "records_consistent: True" in out
    assert "not profit" in out
    # the old label is gone from the human text; the alias lives in the JSON only
    assert "pnl_claim_allowed" not in out


def test_the_summary_of_a_tampered_capture_says_false():
    out = "\n".join(S.summarise(C.run(C.build(
        tweak=C._alter("exec_open", execQty="0.002")))))
    assert "records_consistent: False" in out


def test_the_exit_code_follows_the_new_field(tmp_path):
    """0 when consistent, 2 when not: read from records_consistent, so removing
    the deprecated alias later cannot change the exit code."""
    src = open(ER.__file__, encoding="utf-8").read()
    assert 'report["records_consistent"] else 2' in src
    assert 'report["pnl_claim_allowed"] else 2' not in src


def test_a_report_written_before_the_rename_still_summarises():
    old = dict(C.run())
    del old["records_consistent"]
    out = "\n".join(S.summarise(old))
    assert "records_consistent: True" in out
