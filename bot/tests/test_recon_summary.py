"""The job summary says what the reconciliation says, and what it does not."""
from __future__ import annotations

import json
import os
import sys

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
        assert "pnl_claim_allowed: True" in out
        assert "says nothing about edge" in out

    def test_a_tampered_capture_shows_the_issue_and_no_net(self):
        records = C.build(tweak=C._alter("exec_open", execQty="0.002"))
        out = text(S.summarise(C.run(records)))
        assert "VENUE_DISAGREES:qty" in out and "NET " not in out
        assert "pnl_claim_allowed: False" in out

    def test_missing_or_unreadable_input_claims_nothing(self):
        for junk in (None, [], "x", {}, {"schema": "other"}):
            assert "nothing is claimed" in text(S.summarise(junk))

    def test_the_command_line_reads_a_file(self, tmp_path, capsys):
        path = tmp_path / "r.json"
        path.write_text(json.dumps(C.run()))
        assert S.main([str(path)]) == 0
        assert "pnl_claim_allowed" in capsys.readouterr().out

    def test_a_missing_file_does_not_raise(self, tmp_path, capsys):
        assert S.main([str(tmp_path / "nope.json")]) == 0
        assert "nothing is claimed" in capsys.readouterr().out
        assert S.main([]) == 3
