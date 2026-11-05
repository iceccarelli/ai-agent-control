import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import evidence_chain as EC  # noqa: E402
import profitability_report as PR  # noqa: E402
from capacity_helpers import base_row, clone, noisy, series, START, H8  # noqa: E402

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
    assert rep["validation"]["stream_ok"] and rep["integrity"]["summary"]["economic_evidence_eligible"]
    assert rep["reliability"]["verdict"] == "HEALTHY" and rep["authorizes_trading"] is False
    assert rep["economics"] is None
    stat = {o["strategy"]: o["status"] for o in rep["opportunities"]}
    assert stat["carry_overlay_taker/v1"] == "INSUFFICIENT_EVIDENCE"
    assert all(s in ("INSUFFICIENT_EVIDENCE", None) for s in stat.values())
    assert "FROZEN_ABSENT" in PR.render(rep) and "no status authorises trading" in PR.render(rep)


def test_a_long_low_funding_recording_reaches_an_economic_verdict_with_the_screens_and_unknowns(tmp_path):
    rows = series(BASE, 100, lambda T, t: 0.65, noisy(0.65, 0.12))
    rep = report(lay_down(tmp_path, rows), rows)
    assert rep["funding_evaluation"]["status"] == "OK" and rep["economics"] is not None
    res = rep["economics"]["result"]
    assert "latency" in res["unknowns"] and "ILLUSTRATIVE" in rep["economics"]["inputs"]["basis"]
    assert rep["economics"]["inputs"]["venue_rules"]["status"] == "ASSUMED_NOT_VENUE_VERIFIED"
    assert not any(h["meets"] for h in res["hurdles"])
    stat = {o["strategy"]: o["status"] for o in rep["opportunities"]}
    assert stat["carry_overlay_taker/v1"] == "ECONOMICALLY_UNATTRACTIVE"
    text = PR.render(rep)
    assert "book-fillable snapshots" in text and "fills" not in text.replace("book-fillable", "")
    assert "LOWER BOUND" in text and "best case" in text and "MODELLED net" in text and "UNPRICED" in text
    assert "environment mainnet, symbol BTCUSDT" in text and "venue bybit" in text
    assert res["components_usd"]["market_impact_usd"] == 0.0 and "market_impact" in res["unknowns"]     # unknown, not a known zero


def test_a_tampered_file_fails_integrity_and_drops_the_conclusion_to_insufficient(tmp_path):
    rows = series(BASE, 100, lambda T, t: 4.0, noisy(4.0, 0.5))
    d = lay_down(tmp_path, rows)
    p = os.path.join(d, "capacity-20261104.jsonl")
    data = open(p, "rb").read()
    open(p, "wb").write(data.replace(b'"mode":"overlay"', b'"mode":"overlaz"', 1))
    rep = report(d, rows)
    assert rep["integrity"]["summary"]["economic_evidence_eligible"] is False
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
    assert "TRUNCATED" in codes and rep["integrity"]["summary"]["economic_evidence_eligible"] is False


def test_a_stopped_recorder_is_stale_in_the_report_however_good_its_old_rows(tmp_path):
    rows = series(BASE, 3, lambda T, t: 0.65, lambda T: 0.65)
    d = lay_down(tmp_path, rows)
    rep = PR.build(d, now_ms=rows[-1]["at_ms"] + 6 * 3_600_000)
    assert rep["reliability"]["verdict"] == "STALE"
    assert {o["strategy"]: o["status"] for o in rep["opportunities"]}["carry_overlay_taker/v1"] == "INSUFFICIENT_EVIDENCE"


def test_legacy_rows_are_quarantined_counted_and_never_used(tmp_path):
    rows = series(BASE, 3, lambda T, t: 0.65, lambda T: 0.65)
    d = lay_down(tmp_path, [{"at_ms": k, "curve": [], "environment": "mainnet"} for k in range(1, 5)] + rows)
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


# ---- adversarial-review findings -------------------------------------------------------------------------------

def test_a_day_file_emptied_to_zero_bytes_still_fails_the_integrity_gate(tmp_path):
    rows = series(BASE, 6, lambda T, t: 0.65, noisy(0.65, 0.1))
    d = lay_down(tmp_path, rows[: len(rows) // 2], name="capacity-20261103.jsonl")
    d = lay_down(tmp_path, rows[len(rows) // 2:], name="capacity-20261104.jsonl")
    open(os.path.join(d, "capacity-20261103.jsonl"), "wb").write(b"")       # sidecar and head left intact
    rep = report(d, rows)
    assert rep["integrity"]["summary"]["economic_evidence_eligible"] is False
    assert any(i["code"] == "TRUNCATED" for f in rep["integrity"]["files"] for i in f["chain"]["issues"])
    assert rep["economics"] is None


def test_economics_are_withheld_not_printed_with_a_caveat_when_a_gate_is_not_cleared(tmp_path):
    """Nine settlements at 6 bps used to print '58.5 %/yr, 10/15/20 -> meets' next to an INSUFFICIENT status."""
    rows = series(BASE, 9, lambda T, t: 6.0, noisy(6.0, 0.4))
    rep = report(lay_down(tmp_path, rows), rows)
    text = PR.render(rep)
    assert rep["economics"] is None and "WITHHELD" in text
    assert "meets" not in text and "%/yr on economic capital" not in text


def test_a_mutated_file_does_not_print_a_return_next_to_the_integrity_failure(tmp_path):
    rows = series(BASE, 100, lambda T, t: 4.0, noisy(4.0, 0.5))
    d = lay_down(tmp_path, rows)
    p = os.path.join(d, "capacity-20261104.jsonl")
    open(p, "wb").write(open(p, "rb").read().replace(b'"mode":"overlay"', b'"mode":"overlaz"', 1))
    text = PR.render(report(d, rows))
    assert "economic-evidence eligible" in text and ": False" in text and "WITHHELD" in text and "meets" not in text


def test_testnet_evidence_cannot_reach_a_candidate_or_a_return_and_the_report_names_the_environment(tmp_path):
    rows = [clone(BASE, r["source"]["store_record_id"], r["at_ms"], r["funding"]["forecast_bps"], r["funding"]["next_settlement_ms"],
                  None if r["funding"]["last_print_bps"] is None else (r["funding"]["last_print_settled_ms"], r["funding"]["last_print_bps"]),
                  env="testnet") for r in series(BASE, 100, lambda T, t: 4.0, noisy(4.0, 0.5))]
    rep = report(lay_down(tmp_path, rows), rows)
    text = PR.render(rep)
    assert rep["economics"] is None and "not mainnet" in " ".join(rep["economics_withheld_because"])
    assert "environment testnet" in text
    assert {o["strategy"]: o["status"] for o in rep["opportunities"]}["carry_overlay_taker/v1"] == "INSUFFICIENT_EVIDENCE"


def test_a_long_constant_funding_recording_is_withheld_for_want_of_effective_sample_size(tmp_path):
    rows = series(BASE, 150, lambda T, t: 4.0, lambda T: 4.0)
    rep = report(lay_down(tmp_path, rows), rows)
    assert rep["economics"] is None and "effective sample size" in " ".join(rep["economics_withheld_because"])


def test_the_report_says_when_it_has_no_external_anchor(tmp_path):
    rows = series(BASE, 4, lambda T, t: 0.65, noisy(0.65, 0.1))
    d = lay_down(tmp_path, rows)
    assert "UNANCHORED" in PR.render(report(d, rows)) and report(d, rows)["integrity"]["summary"]["files_unanchored"] == 1


def test_a_third_of_the_rows_rejected_voids_the_stream_and_is_printed(tmp_path):
    rows = series(BASE, 6, lambda T, t: 0.65, noisy(0.65, 0.1))
    for r in rows[::3]:
        r["funding"]["forecast_bps"] = 1e6                      # a unit error, not NaN
    rep = report(lay_down(tmp_path, rows), rows)
    assert rep["validation"]["stream_ok"] is False and rep["validation"]["n_rejected"] > 0
    assert "REJECTED" in PR.render(rep)


def test_thirty_five_settlements_at_eight_bps_do_not_print_meets_while_the_engine_says_insufficient(tmp_path):
    """The review: 35 joined settlements printed '10/15/20% -> meets' next to INSUFFICIENT_EVIDENCE."""
    rows = series(BASE, 36, lambda T, t: 8.0, noisy(8.0, 0.6))
    rep = report(lay_down(tmp_path, rows), rows)
    text = PR.render(rep)
    assert rep["economics"] is None and "meets" not in text
    assert any("opportunity engine needs 90" in w for w in rep["economics_withheld_because"])


def test_the_depth_candidates_reasons_are_printed_and_it_claims_no_screen(tmp_path):
    rows = series(BASE, 100, lambda T, t: 4.0, noisy(4.0, 0.5))
    rep = report(lay_down(tmp_path, rows), rows)
    stat = {o["strategy"]: o for o in rep["opportunities"]}
    assert stat["carry_overlay_taker/v1"]["status"] == "RESEARCH_CANDIDATE"
    assert stat["candidate_carry_depth/v1"]["status"] == "INSUFFICIENT_EVIDENCE"
    text = PR.render(rep)
    assert "depth-benefit entry rule is not evaluated" in text and "unpriced inputs" in text


def test_a_set_aside_torn_row_appears_in_the_report(tmp_path):
    rows = series(BASE, 4, lambda T, t: 0.65, noisy(0.65, 0.1))
    d = lay_down(tmp_path, rows)
    p = os.path.join(d, "capacity-20261104.jsonl")
    open(p, "ab").write(b'{"torn')
    EC.repair(p)
    f = report(d, rows)["integrity"]["files"][0]
    assert f["chain"]["torn_aside_bytes"] > 0 and "SET ASIDE" in PR.render(report(d, rows))


@pytest.mark.parametrize("flag,value", [("--hold-days", "0"), ("--hold-days", "-3"), ("--hold-days", "nan"), ("--hold-days", "1e9"),
                                        ("--notional", "0"), ("--notional", "inf")])
def test_nonsense_cli_arguments_are_refused_before_any_allocation(tmp_path, flag, value):
    with pytest.raises(SystemExit):
        PR.main(["--dir", str(tmp_path), flag, value])
