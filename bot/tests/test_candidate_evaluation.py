"""Round 22: every candidate family is evaluated through ONE interface with ONE closed status vocabulary, and a positive backtest is never QUALIFIED."""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(HERE), os.path.join(os.path.dirname(HERE), "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import candidate_competition as CC_  # noqa: E402
import candidate_evaluation as CE  # noqa: E402
import eligibility_gate as EG  # noqa: E402

SPECS = os.path.join(os.path.dirname(HERE), "research", "candidates")


@pytest.fixture(scope="module")
def real_view(tmp_path_factory):
    d = tmp_path_factory.mktemp("ledger")
    rep = CC_.run_competition(ledger_dir=str(d))
    return CE.view(rep, CE.load_specs(SPECS)), rep


def test_the_status_vocabulary_is_the_directives_six_and_closed():
    assert CE.STATUSES == ("UNTESTED", "DATA_INVALID", "INSUFFICIENT", "NEGATIVE", "PROMISING", "QUALIFIED")
    assert len(CE.FAMILIES) == 11 and len(set(CE.FAMILIES)) == 11
    assert sorted(CE.FAMILIES) == sorted(f[:-5] for f in os.listdir(SPECS) if f.endswith(".json")), "the opportunity set is exactly the registered specifications"


def test_the_real_repository_state_evaluates_all_eleven_candidates_and_says_no_winner(real_view):
    v, rep = real_view
    assert [c["family"] for c in v["candidates"]] == list(CE.FAMILIES)
    assert all(c["status"] in CE.STATUSES for c in v["candidates"])
    assert v["qualified"] == [] and v["winner"] is None and v["statement"].startswith("NO WINNER")
    by = {c["family"]: c for c in v["candidates"]}
    assert by["cash_and_carry"]["status"] == "NEGATIVE"                        # the committed corpus rejects it in sample
    assert by["cross_venue_funding"]["status"] == "UNTESTED"
    assert sum(v["counts"].values()) == 11


def test_every_candidate_carries_the_directives_fields_and_unknown_stays_unknown(real_view):
    v, _ = real_view
    need = {"candidate_id", "hypothesis", "data_requirements", "execution_requirements", "cost_model", "capital_model", "sample_size", "gross_edge", "net_edge", "confidence", "drawdown", "failure_modes", "status"}
    for c in v["candidates"]:
        if c["family"] in ("cash_and_carry", "cross_interval_funding_dispersion", "cross_sectional_funding", "spot_perp_basis"):
            assert need <= set(c), (c["family"], need - set(c))
        assert c["net_edge"]["value_bps"] is not None
    c = {x["family"]: x for x in v["candidates"]}["cross_interval_funding_dispersion"]
    assert c["gross_edge"]["value_bps"] == CE.UNKNOWN and c["net_edge"]["value_bps"] == CE.UNKNOWN and c["drawdown"]["measured"] == CE.UNKNOWN
    assert c["hypothesis"] != CE.UNKNOWN and "funding" in c["hypothesis"].lower()
    assert c["capital_model"]["structure"] == "PERP_ONLY" and c["capital_model"]["capital_per_usd_notional_at_leverage_1"] == "1.02"
    carry = {x["family"]: x for x in v["candidates"]}["cash_and_carry"]["capital_model"]
    assert carry["structure"] == "ACQUIRED_SPOT_PERP" and carry["capital_per_usd_notional_at_leverage_1"] == "2.04"
    assert {x["family"]: x for x in v["candidates"]}["liquidity_imbalance"]["capital_model"]["structure"] == CE.UNKNOWN


def row(**kw):
    base = {"name": "x", "candidate": "x@spec-1", "verdict": "UNTESTED", "readiness_level": None, "net_edge": {"value_bps": None, "significant": None}, "rejection_reasons": [], "evaluator": {"wired": False, "code": "NO_EVALUATOR_WIRED:x"}}
    base.update(kw)
    return base


def full(**kw):
    """A row that states EVERYTHING a QUALIFIED claim needs (dataset state, multiple-testing count, independent observations)."""
    base = dict(dataset_state={"state": "DATA_SUFFICIENT_ELIGIBLE_FOR_FURTHER_REVIEW", "reasons": []}, multiple_testing={"family": "f", "n_variants_counted": 3, "persisted_variants": 3},
                families={"F": {"n_rows": 120}})
    base.update(kw)
    return row(**base)


@pytest.mark.parametrize("r,expect", [
    (row(), "UNTESTED"),
    (row(verdict="INSUFFICIENT_DATA"), "INSUFFICIENT"),
    (row(verdict="REJECTED"), "NEGATIVE"),
    (row(verdict="INSUFFICIENT_DATA", readiness_level="PROMISING", net_edge={"value_bps": 4.0, "significant": True}), "PROMISING"),
    (row(verdict="INSUFFICIENT_DATA", readiness_level="PROSPECTIVE", net_edge={"value_bps": 4.0, "significant": True}), "PROMISING"),
    (full(verdict="INSUFFICIENT_DATA", readiness_level="EXECUTION_READY", net_edge={"value_bps": 4.0, "significant": True}), "QUALIFIED"),
    (row(verdict="INSUFFICIENT_DATA", readiness_level="EXECUTION_READY", net_edge={"value_bps": 4.0, "significant": False}), "PROMISING"),
    (row(verdict="INSUFFICIENT_DATA", readiness_level="EXECUTION_READY", net_edge={"value_bps": 4.0, "significant": None}), "PROMISING"),
    (row(verdict="INSUFFICIENT_DATA", readiness_level="EXECUTION_READY", net_edge={"value_bps": -1.0, "significant": True}), "PROMISING"),
    (row(verdict="INSUFFICIENT_DATA", readiness_level="EXECUTION_READY", net_edge={"value_bps": None, "significant": True}), "PROMISING"),
    (row(verdict="REJECTED", readiness_level="EXECUTION_READY", net_edge={"value_bps": 4.0, "significant": True}), "NEGATIVE"),
    (row(dataset_state={"state": "DATA_INVALID", "reasons": ["bad page"]}, verdict="INSUFFICIENT_DATA", readiness_level="PROMISING"), "DATA_INVALID"),
    (row(dataset_state={"state": "DATA_INCOMPLETE", "reasons": []}), "DATA_INVALID"),
    (row(dataset_state={"state": "DATA_UNAVAILABLE", "reasons": []}), "DATA_INVALID"),
    (row(dataset_state={"state": "DATA_VALID_BUT_INSUFFICIENT", "reasons": ["too few rows"]}, verdict="INSUFFICIENT_DATA"), "INSUFFICIENT"),
])
def test_the_status_mapping_never_calls_a_positive_backtest_qualified(r, expect):
    assert CE.status_of(r)[0] == expect


def test_a_positive_in_sample_figure_alone_is_at_most_insufficient():
    s, why = CE.status_of(row(verdict="INSUFFICIENT_DATA", net_edge={"value_bps": 40.0, "significant": True}))
    assert s == "INSUFFICIENT"                                                    # no ladder level: a backtest is not a promise


def test_missing_families_are_listed_untested_and_the_set_is_always_complete():
    v = CE.view({"results": [row(name="cash_and_carry", verdict="REJECTED")]})
    assert [c["family"] for c in v["candidates"]] == list(CE.FAMILIES)
    assert {c["family"]: c["status"] for c in v["candidates"]}["spot_perp_basis"] == "UNTESTED"
    assert v["counts"]["NEGATIVE"] == 1 and v["counts"]["UNTESTED"] == 10
    assert CE.view({"results": [row(name="made_up_family")]})["not_in_the_set"] == ["made_up_family"]
    assert CE.view({})["winner"] is None


def test_a_qualified_candidate_is_named_but_the_statement_keeps_the_owner_and_bounded_session_in_the_loop():
    v = CE.view({"results": [full(name="cash_and_carry", verdict="INSUFFICIENT_DATA", readiness_level="EXECUTION_READY", net_edge={"value_bps": 3.0, "significant": True})]})
    assert v["qualified"] == ["x@spec-1"] and "owner's attestation" in v["statement"]


def test_the_evidence_input_of_the_eligibility_gate_comes_from_this_view(real_view):
    v, _ = real_view
    ev = CE.evidence_for(v, "cash_and_carry@spec-1")
    assert ev["status"] == "NEGATIVE" and ev["candidate_id"].startswith("cash_and_carry")
    assert CE.evidence_for(v, None)["status"] == "UNTESTED" and CE.evidence_for(v, "no_such_family@spec-9")["status"] == "UNTESTED"
    gate = EG.evaluate({"evidence": ev})
    t = next(x for x in gate["terms"] if x["term"] == "STATISTICAL_EVIDENCE_SUFFICIENT")
    assert t["verdict"] == EG.FAIL and gate["verdict"] == EG.NO_TRADE


def test_the_command_line_prints_the_set_and_reads_only_the_given_file(tmp_path, capsys, real_view):
    _, rep = real_view
    f = tmp_path / "competition.json"
    f.write_text(json.dumps(rep, default=str))
    assert CE.main([str(f)]) == 0
    out = capsys.readouterr().out
    assert "NO WINNER" in out and out.count("\n") >= 12
    assert CE.main([str(tmp_path / "missing.json")]) == 2
    assert CE.main([str(f), "--json"]) == 0 and json.loads(capsys.readouterr().out)["schema"] == CE.SCHEMA


def test_the_module_reads_no_ledger_clock_network_or_environment():
    src = open(os.path.join(os.path.dirname(HERE), "tools", "candidate_evaluation.py"), encoding="utf-8").read()
    for banned in ("history_holdout", "candidate_ledger", "capital_anchor", "os.environ", "getenv", "time.time", "urllib", "socket", "requests", "subprocess", "unreserved_exploratory"):
        assert banned not in src, banned


@pytest.mark.parametrize("drop", ["dataset_state", "multiple_testing", "families"])
def test_a_row_missing_what_a_qualification_needs_is_at_most_promising(drop):
    """REPRODUCED (review): {ladder EXECUTION_READY, net +5, significant} with no dataset_state / multiple-testing / sample was QUALIFIED."""
    r = full(verdict="INSUFFICIENT_DATA", readiness_level="EXECUTION_READY", net_edge={"value_bps": 5.0, "significant": True})
    r.pop(drop)
    s, why = CE.status_of(r)
    assert s == "PROMISING" and "NOT_QUALIFIED" in " ".join(why)
    assert CE.status_of(row(verdict="INSUFFICIENT_DATA", readiness_level="EXECUTION_READY", net_edge={"value_bps": 5.0, "significant": True}))[0] == "PROMISING"
    thin = full(verdict="INSUFFICIENT_DATA", readiness_level="EXECUTION_READY", net_edge={"value_bps": 5.0, "significant": True}, families={"F": {"n_rows": 49}})
    assert CE.status_of(thin)[0] == "PROMISING"
    bad_ds = full(verdict="INSUFFICIENT_DATA", readiness_level="EXECUTION_READY", net_edge={"value_bps": 5.0, "significant": True}, dataset_state={"state": "DATA_VALID_BUT_INSUFFICIENT", "reasons": []})
    assert CE.status_of(bad_ds)[0] == "PROMISING"


def test_a_duplicate_family_row_is_judged_by_its_worst_status_and_evidence_matches_the_exact_spec():
    rejected = row(name="cash_and_carry", verdict="REJECTED")
    qualified = full(name="cash_and_carry", verdict="INSUFFICIENT_DATA", readiness_level="EXECUTION_READY", net_edge={"value_bps": 5.0, "significant": True})
    for rows in ([rejected, qualified], [qualified, rejected]):
        v = CE.view({"results": rows})
        assert v["winner"] is None and {c["family"]: c["status"] for c in v["candidates"]}["cash_and_carry"] == "NEGATIVE"
    v = CE.view({"results": [qualified]})
    assert CE.evidence_for(v, "no_such_family@spec-9")["status"] == "UNTESTED"          # no such candidate
    q2 = dict(qualified, candidate="cash_and_carry@spec-1")
    v2 = CE.view({"results": [q2]})
    assert CE.evidence_for(v2, "cash_and_carry@spec-1")["status"] == "QUALIFIED"
    assert CE.evidence_for(v2, "cash_and_carry@other-digest")["status"] == "UNTESTED", "a different specification revision is not this candidate"
    assert CE.evidence_for(v2, "cash_and_carry")["status"] == "QUALIFIED"
