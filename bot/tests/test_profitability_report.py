import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import evidence_chain as EC  # noqa: E402
import profitability_report as PR  # noqa: E402
from capacity_helpers import base_row, series, START, H8  # noqa: E402

BASE = base_row()


def lay_down(tmp_path, rows, name="capacity-20261104.jsonl"):
    d = tmp_path / "market"
    d.mkdir(exist_ok=True)
    EC.bulk_write(str(d / name), rows)
    return str(d)


def report(d, rows, **kw):
    return PR.build(d, now_ms=rows[-1]["at_ms"] + 60_000, **kw)


def test_a_short_clean_recording_is_healthy_but_every_conclusion_is_insufficient(tmp_path):
    rows = series(BASE, 3, lambda T, t: 0.65, lambda T: 0.65)
    rep = report(lay_down(tmp_path, rows), rows)
    assert rep["validation"]["stream_ok"] and rep["integrity"]["chain_ok_for_v2_files"]
    assert rep["reliability"]["verdict"] == "HEALTHY" and rep["authorizes_trading"] is False
    assert rep["economics"] is None
    stat = {o["strategy"]: o["status"] for o in rep["opportunities"]}
    assert stat["carry_overlay_taker/v1"] == "INSUFFICIENT_EVIDENCE"
    assert all(s in ("INSUFFICIENT_EVIDENCE", None) for s in stat.values())
    assert "FROZEN_ABSENT" in PR.render(rep) and "no status authorises trading" in PR.render(rep)


def test_a_long_low_funding_recording_reaches_an_economic_verdict_with_the_screens_and_unknowns(tmp_path):
    rows = series(BASE, 100, lambda T, t: 0.65, lambda T: 0.65, cadence_ms=600_000)
    rep = report(lay_down(tmp_path, rows), rows)
    assert rep["funding_evaluation"]["status"] == "OK" and rep["economics"] is not None
    res = rep["economics"]["result"]
    assert "latency" in res["unknowns"] and "ILLUSTRATIVE" in rep["economics"]["inputs"]["basis"]
    assert rep["economics"]["inputs"]["venue_rules"]["status"] == "ASSUMED_NOT_VENUE_VERIFIED"
    assert not any(h["meets"] for h in res["hurdles"])
    stat = {o["strategy"]: o["status"] for o in rep["opportunities"]}
    assert stat["carry_overlay_taker/v1"] == "ECONOMICALLY_UNATTRACTIVE"
    assert "book-fillable snapshots" in PR.render(rep) and "fills" not in PR.render(rep).replace("book-fillable", "")


def test_a_tampered_file_fails_integrity_and_drops_the_conclusion_to_insufficient(tmp_path):
    rows = series(BASE, 100, lambda T, t: 4.0, lambda T: 4.0, cadence_ms=600_000)
    d = lay_down(tmp_path, rows)
    p = os.path.join(d, "capacity-20261104.jsonl")
    data = open(p, "rb").read()
    open(p, "wb").write(data.replace(b'"forecast_bps":4.0', b'"forecast_bps":9.0', 1))
    rep = report(d, rows)
    assert rep["integrity"]["chain_ok_for_v2_files"] is False
    assert any(i["code"] == "MUTATED" for f in rep["integrity"]["files"] for i in f["chain"]["issues"])
    assert {o["strategy"]: o["status"] for o in rep["opportunities"]}["carry_overlay_taker/v1"] == "INSUFFICIENT_EVIDENCE"


def test_external_anchors_from_the_monitor_history_catch_a_consistently_rewritten_file(tmp_path):
    rows = series(BASE, 4, lambda T, t: 0.65, lambda T: 0.65)
    d = lay_down(tmp_path, rows)
    p = os.path.join(d, "capacity-20261104.jsonl")
    head = EC.verify(p)["head"]
    hist = tmp_path / "monitor.jsonl"
    hist.write_text(json.dumps({"recorder_chain_file": "capacity-20261104.jsonl", "recorder_chain_n": head["n"],
                                "recorder_chain_head": head["head"]}) + "\n")
    # an attacker deletes the last day AND rewrites the sidecar and head to match
    os.remove(p + ".chain"); os.remove(p + ".head"); os.remove(p)
    EC.bulk_write(p, rows[: head["n"] // 2])
    rep = report(d, rows, history=str(hist))
    codes = [i["code"] for f in rep["integrity"]["files"] for i in f["chain"]["issues"]]
    assert "TRUNCATED" in codes and rep["integrity"]["chain_ok_for_v2_files"] is False


def test_a_stopped_recorder_is_stale_in_the_report_however_good_its_old_rows(tmp_path):
    rows = series(BASE, 3, lambda T, t: 0.65, lambda T: 0.65)
    d = lay_down(tmp_path, rows)
    rep = PR.build(d, now_ms=rows[-1]["at_ms"] + 6 * 3_600_000)
    assert rep["reliability"]["verdict"] == "STALE"
    assert {o["strategy"]: o["status"] for o in rep["opportunities"]}["carry_overlay_taker/v1"] == "INSUFFICIENT_EVIDENCE"


def test_legacy_rows_are_quarantined_counted_and_never_used(tmp_path):
    rows = series(BASE, 3, lambda T, t: 0.65, lambda T: 0.65)
    d = lay_down(tmp_path, [{"at_ms": 1, "curve": [], "environment": "mainnet"}] * 4 + rows)
    rep = report(d, rows)
    assert rep["validation"]["n_legacy_quarantined"] == 4 and rep["validation"]["n_valid_rows"] == len(rows)


def test_a_torn_final_line_is_quarantined_and_visible_in_the_report(tmp_path):
    rows = series(BASE, 3, lambda T, t: 0.65, lambda T: 0.65)
    d = lay_down(tmp_path, rows)
    open(os.path.join(d, "capacity-20261104.jsonl"), "ab").write(b'{"schema": "capacity/2", "row')
    rep = report(d, rows)
    f = rep["integrity"]["files"][0]
    assert f["quarantined_lines"] and "QUARANTINED" in PR.render(rep)


def test_an_empty_directory_is_no_data_not_healthy(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    rep = PR.build(str(d), now_ms=1_793_800_000_000)
    assert rep["reliability"]["verdict"] == "NO_DATA" and rep["economics"] is None
    assert PR.main(["--dir", str(d)]) == 0
