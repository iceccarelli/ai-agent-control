"""The drill records where its evidence chain ENDS (head hash and record count)
so a later reconcile can tell a cut tail from the real end. Temp files only; no
broker, no venue."""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import drill  # noqa: E402
import evidence_fixtures as fx  # noqa: E402
import evidence_reconcile as er  # noqa: E402


def _write(path, records):
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records))


def test_the_report_gets_the_head_hash_and_count(tmp_path):
    records = fx.good_chain()
    ev, out = tmp_path / "ev.jsonl", tmp_path / "out.json"
    _write(ev, records)
    report = {"evidence_capture": {"requested": True, "complete": True}}
    drill.stamp_capture_tail(report, str(ev), str(out))
    cap = report["evidence_capture"]
    assert cap["head_hash"] == records[-1]["record_hash"]
    assert cap["records"] == len(records)
    assert json.loads(out.read_text())["evidence_capture"]["head_hash"] == cap["head_hash"]


def test_the_recorded_tail_catches_a_later_truncation(tmp_path):
    records = fx.good_chain()
    ev = tmp_path / "ev.jsonl"
    _write(ev, records)
    report = {"evidence_capture": {"requested": True}}
    drill.stamp_capture_tail(report, str(ev), "")
    _write(ev, records[:-1])
    cap = er.load_capture(str(ev),
                          expect_head_hash=report["evidence_capture"]["head_hash"],
                          expect_records=report["evidence_capture"]["records"])
    assert cap.intact is False


def test_nothing_is_stamped_without_a_capture_or_an_unreadable_file(tmp_path):
    report = {"verdict": "FAILED"}
    drill.stamp_capture_tail(report, str(tmp_path / "missing.jsonl"), "")
    assert "evidence_capture" not in report
    report = {"evidence_capture": {"requested": True}}
    drill.stamp_capture_tail(report, str(tmp_path / "missing.jsonl"), "")
    assert "head_hash" not in report["evidence_capture"]
