"""A hash chain proves nothing was changed in the middle; it cannot show that the
END was cut off. Deleting the last record(s) of a capture left every remaining
prev_hash intact, so a chain whose final position read was non-flat reconciled
once that read was removed. ``expect_head_hash`` / ``expect_records`` (recorded
by whoever produced the capture) close that: a capture whose head or length is
not the one recorded is a broken chain. Synthetic data only.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import evidence_reconcile as er  # noqa: E402
import evidence_fixtures as fx  # noqa: E402


@pytest.fixture()
def chain_file(tmp_path):
    records = fx.good_chain()
    assert len(records) >= 2
    path = tmp_path / "ev.jsonl"
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records))
    return path, records


def _truncate(path, keep):
    lines = path.read_text().splitlines(keepends=True)
    path.write_text("".join(lines[:keep]))


def test_without_an_expectation_a_truncated_tail_is_not_detected(chain_file):
    # the limitation this feature exists for: documented, not hidden
    path, records = chain_file
    _truncate(path, len(records) - 1)
    assert er.load_capture(str(path)).intact is True


def test_the_recorded_head_hash_detects_a_cut_tail(chain_file):
    path, records = chain_file
    head = records[-1]["record_hash"]
    _truncate(path, len(records) - 1)
    cap = er.load_capture(str(path), expect_head_hash=head)
    assert cap.intact is False
    assert any("head" in i for i in cap.chain_issues)


def test_the_recorded_record_count_detects_a_cut_tail(chain_file):
    path, records = chain_file
    _truncate(path, len(records) - 1)
    cap = er.load_capture(str(path), expect_records=len(records))
    assert cap.intact is False and any("records" in i for i in cap.chain_issues)


def test_an_untouched_capture_matches_its_recorded_head_and_count(chain_file):
    path, records = chain_file
    cap = er.load_capture(str(path), expect_head_hash=records[-1]["record_hash"],
                          expect_records=len(records))
    assert cap.intact is True


def test_a_cut_tail_blocks_the_reconciliation(chain_file):
    path, records = chain_file
    _truncate(path, len(records) - 1)
    cap = er.load_capture(str(path), expect_head_hash=records[-1]["record_hash"])
    report = er.reconcile(fill_claims=[fx.claim()], captures=[cap], now_ms=fx.NOW)
    assert report["pnl_claim_allowed"] is False
    assert any("CAPTURE_CHAIN_BROKEN" in v["issues"] for v in report["fills"])


def test_the_command_line_takes_the_expectation(chain_file, tmp_path, capsys):
    path, records = chain_file
    _truncate(path, len(records) - 1)
    out = tmp_path / "out" / "r.json"
    rc = er.main(["--evidence", str(path), "--now-ms", str(fx.NOW),
                  "--expect-head-hash", records[-1]["record_hash"],
                  "--json", str(out)])
    report = json.loads(out.read_text())
    assert rc != 0 and report["pnl_claim_allowed"] is False


def test_the_command_line_refuses_a_mismatched_number_of_expectations(
        chain_file, tmp_path, capsys):
    path, records = chain_file
    rc = er.main(["--evidence", str(path), "--now-ms", str(fx.NOW),
                  "--expect-head-hash", "a", "--expect-head-hash", "b"])
    assert rc == 3
