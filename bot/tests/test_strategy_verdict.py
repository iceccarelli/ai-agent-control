"""The economic verdict engine is built to KILL: a backtest never promotes, one failed rung is terminal, evidence out of order does not count, and every PASS must earn its place."""
from __future__ import annotations

import copy
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import strategy_verdict as SV  # noqa: E402

H = "a" * 64


def lab(v, p="assumed"):
    return {"value": v, "provenance": p}


def spec(**over):
    s = {
        "candidate_id": "cand1", "family": "test_family", "hypothesis": "h", "economic_mechanism": "someone pays", "counterparty": "levered longs",
        "required_data": [{"name": "funding", "point_in_time": True}], "eligibility_rules": ["listed >= 30d"], "entry": "e", "exit": "x", "holding_period": "30d",
        "execution_model": "taker", "fee_model": {"maker_bps": lab(2.0), "taker_bps": lab(5.5)}, "funding_model": {"expected_bps_per_settlement": lab(0.3, "modelled")},
        "slippage_model": {"entry_bps": lab(1.0), "exit_bps": lab(1.0)}, "capacity_model": {"min_viable_capacity_usd": 1000.0}, "risk_model": "r", "drawdown_model": "d",
        "OOS_protocol": {"train_test_boundary": "2024-01-01", "data_window": "2022..2026"}, "prospective_protocol": "p",
        "rejection_conditions": ["net edge after costs <= 0 out of sample"], "bias_declarations": {k: True for k in SV.BIAS_DECLARATIONS},
    }
    s.update(over)
    return s


def ev(stage, result="PASS", **kw):
    base = {"provenance": "run#1", "data_sha256": H}
    m = {}
    if stage in ("historical_test", "oos_test"):
        m = {"independent_observations": 500, "n_variants_tested": 3, "multiple_testing_adjusted": True, "uses_future_information": False, "universe_point_in_time": True}
    elif stage == "cost_model":
        m = {"unknown_cost_inputs": 0, "net_edge_bps_after_costs": 2.5}
    elif stage == "capacity_test":
        m = {"capacity_usd": 5000.0}
    elif stage == "prospective_observation":
        m = {"registered_before_observation": True, "complete": True}
    elif stage == "execution_validation":
        m = {"venue": "testnet", "fills_measured": True, "fees_measured": True}
        base = {"provenance": "testnet session 1"}
    elif stage == "reconciled_owner_result":
        m = {"net_usd_after_all_costs": 12.5}
        base = {"provenance": "owner_import report", "status": "RECONCILED"}
    metrics = dict(m, **kw.pop("metrics", {}))
    base.update(kw)
    return SV.EvidenceItem(stage=stage, result=result, metrics=metrics, **base)


OFFLINE = ["historical_test", "oos_test", "cost_model", "capacity_test"]


def verdict(s, *items):
    return SV.evaluate(s, list(items)).verdict


# ---------------------------------------------------------------- the specification

def test_a_complete_specification_is_valid_and_untested():
    assert SV.spec_problems(spec()) == []
    r = SV.evaluate(spec(), [])
    assert r.verdict == SV.UNTESTED and r.next_stage == "historical_test"


@pytest.mark.parametrize("field", SV.REQUIRED_SPEC_FIELDS)
def test_every_required_field_is_required(field):
    s = spec(); s.pop(field)
    assert f"MISSING:{field}" in SV.spec_problems(s) and SV.evaluate(s, [ev("historical_test")]).verdict == SV.UNTESTED


@pytest.mark.parametrize("bias", SV.BIAS_DECLARATIONS)
def test_an_unruled_out_bias_is_a_problem(bias):
    s = spec(); s["bias_declarations"][bias] = False
    assert f"BIAS_NOT_RULED_OUT:{bias}" in SV.spec_problems(s)
    s2 = spec(); s2["bias_declarations"].pop(bias)
    assert f"BIAS_NOT_RULED_OUT:{bias}" in SV.spec_problems(s2)


def test_a_non_point_in_time_data_requirement_is_refused():
    s = spec(required_data=[{"name": "funding", "point_in_time": False}])
    assert any(p.startswith("REQUIRED_DATA[0]_NOT_DECLARED_POINT_IN_TIME") for p in SV.spec_problems(s))
    assert SV.spec_problems(spec(required_data=[{"name": "x"}]))


@pytest.mark.parametrize("model,comp", [("fee_model", "taker_bps"), ("fee_model", "maker_bps"), ("slippage_model", "entry_bps"), ("slippage_model", "exit_bps"),
                                        ("funding_model", "expected_bps_per_settlement")])
def test_zero_or_unlabelled_costs_are_refused_unless_a_real_stated_zero(model, comp):
    s = spec(); s[model][comp] = lab(0.0, "assumed")
    assert f"ZERO_COST_WITHOUT_MEASUREMENT:{model}.{comp}" in SV.spec_problems(s)
    s = spec(); s[model][comp] = lab(0.0, "venue_stated")                      # a stated zero (e.g. a real rebate/zero-fee tier) is allowed
    assert not [p for p in SV.spec_problems(s) if comp in p]
    s = spec(); s[model][comp] = 1.0                                           # unlabelled
    assert f"COST_INPUT_UNLABELLED:{model}.{comp}" in SV.spec_problems(s)
    s = spec(); s[model][comp] = lab(float("nan"), "measured")
    assert f"COST_INPUT_NOT_A_NUMBER:{model}.{comp}" in SV.spec_problems(s)
    s = spec(); s[model][comp] = lab(None, "unknown")
    assert f"COST_INPUT_NOT_A_NUMBER:{model}.{comp}" in SV.spec_problems(s)


def test_rejection_conditions_must_be_falsifiable_statements_and_capacity_needs_a_minimum():
    assert SV.spec_problems(spec(rejection_conditions=[]))
    assert SV.spec_problems(spec(rejection_conditions=[""]))
    assert "CAPACITY_MODEL_NEEDS_min_viable_capacity_usd" in SV.spec_problems(spec(capacity_model={"note": "x"}))
    assert SV.spec_problems(spec(OOS_protocol={"data_window": "x"}))


# ---------------------------------------------------------------- the ladder

def test_a_backtest_alone_never_promotes():
    r = SV.evaluate(spec(), [ev("historical_test")])
    assert r.verdict == SV.INSUFFICIENT_DATA and "backtest alone never promotes" in " ".join(r.reasons) and r.next_stage == "oos_test"
    for strong in (ev("historical_test", metrics={"net_edge_bps_after_costs": 9999.0}),):
        assert verdict(spec(), strong) == SV.INSUFFICIENT_DATA                 # no return figure, however large, changes that


def test_the_full_ladder_climbs_one_rung_at_a_time():
    s = spec()
    assert verdict(s, *[ev(x) for x in OFFLINE]) == SV.PROMISING
    pending = ev("prospective_observation", "INCONCLUSIVE", metrics={"registered_before_observation": True, "complete": False})
    assert verdict(s, *[ev(x) for x in OFFLINE], pending) == SV.PROSPECTIVE
    base = [ev(x) for x in OFFLINE] + [ev("prospective_observation")]
    assert verdict(s, *base) == SV.PROSPECTIVE                                # prospective passed, execution not yet validated
    assert verdict(s, *base, ev("execution_validation")) == SV.QUALIFIED
    assert verdict(s, *base, ev("execution_validation"), ev("reconciled_owner_result")) == SV.OWNER_PROFIT_EVIDENCED


def test_one_failed_rung_is_terminal_and_later_passes_cannot_rescue_it():
    s = spec()
    everything = [ev(x) for x in OFFLINE] + [ev("prospective_observation"), ev("execution_validation"), ev("reconciled_owner_result")]
    for i, stage in enumerate(OFFLINE + ["prospective_observation", "execution_validation", "reconciled_owner_result"]):
        items = list(everything)
        items[i] = ev(stage, "FAIL", metrics={})
        r = SV.evaluate(s, items)
        assert r.verdict == SV.REJECTED and r.next_stage is None and f"{stage} FAILED" in r.reasons


def test_a_declared_rejection_condition_that_fires_kills_the_candidate():
    items = [ev(x) for x in OFFLINE]
    items[2] = ev("cost_model", metrics={"rejection_conditions_fired": ["net edge after costs <= 0 out of sample"]})
    assert verdict(spec(), *items) == SV.REJECTED


def test_evidence_recorded_out_of_order_is_ignored_and_reported():
    r = SV.evaluate(spec(), [ev("prospective_observation"), ev("execution_validation"), ev("reconciled_owner_result")])
    assert r.verdict == SV.INSUFFICIENT_DATA or r.verdict == SV.UNTESTED
    assert len(r.ignored) == 3 and all(v.startswith("NOT_COUNTED:OUT_OF_ORDER") for k, v in r.stages.items() if k in ("prospective_observation", "execution_validation", "reconciled_owner_result"))
    r2 = SV.evaluate(spec(), [ev("oos_test"), ev("cost_model"), ev("capacity_test")])                    # no historical test: nothing after it counts
    assert r2.verdict in (SV.UNTESTED, SV.INSUFFICIENT_DATA) and r2.ignored


def test_the_verdict_is_deterministic_and_order_of_evidence_does_not_matter():
    items = [ev(x) for x in OFFLINE] + [ev("prospective_observation")]
    a = SV.evaluate(spec(), items).as_dict()
    b = SV.evaluate(spec(), list(reversed(items))).as_dict()
    assert a == b


# ---------------------------------------------------------------- what a PASS must carry to count

@pytest.mark.parametrize("stage,mutation,why", [
    ("historical_test", {"independent_observations": 10}, "TOO_FEW_INDEPENDENT"),
    ("historical_test", {"independent_observations": True}, "TOO_FEW_INDEPENDENT"),
    ("historical_test", {"n_variants_tested": 0}, "NO_MULTIPLE_TESTING"),
    ("historical_test", {"multiple_testing_adjusted": False}, "NO_MULTIPLE_TESTING"),
    ("historical_test", {"uses_future_information": True}, "LOOK_AHEAD"),
    ("historical_test", {"uses_future_information": None}, "LOOK_AHEAD"),
    ("historical_test", {"universe_point_in_time": False}, "UNIVERSE_NOT_POINT_IN_TIME"),
    ("oos_test", {"independent_observations": 49}, "TOO_FEW_INDEPENDENT"),
    ("cost_model", {"unknown_cost_inputs": 1}, "UNKNOWN_COST_INPUTS"),
    ("cost_model", {"unknown_cost_inputs": None}, "UNKNOWN_COST_INPUTS"),
    ("cost_model", {"net_edge_bps_after_costs": 0.0}, "NET_EDGE_NOT_POSITIVE"),
    ("cost_model", {"net_edge_bps_after_costs": float("nan")}, "NET_EDGE_NOT_POSITIVE"),
    ("capacity_test", {"capacity_usd": 999.0}, "CAPACITY_BELOW"),
    ("prospective_observation", {"registered_before_observation": False}, "PROSPECTIVE_NOT_REGISTERED"),
    ("prospective_observation", {"complete": False}, "PROSPECTIVE_NOT_REGISTERED"),
    ("execution_validation", {"venue": "paper"}, "EXECUTION_NOT_MEASURED"),
    ("execution_validation", {"fees_measured": False}, "EXECUTION_NOT_MEASURED"),
    ("reconciled_owner_result", {"net_usd_after_all_costs": -1.0}, "OWNER_RESULT_NOT_RECONCILED"),
    ("reconciled_owner_result", {"net_usd_after_all_costs": float("inf") * 0 }, "OWNER_RESULT_NOT_RECONCILED"),
])
def test_a_pass_that_does_not_carry_what_it_needs_does_not_count(stage, mutation, why):
    item = ev(stage, metrics=mutation)
    ok, reason = SV.counted(item, spec())
    assert ok is False and why in reason
    assert ev(stage) and SV.counted(ev(stage), spec()) == (True, None)           # positive control


def test_provenance_hash_and_status_are_required_for_a_pass():
    assert SV.counted(ev("historical_test", provenance=""), spec())[1] == "PASS_WITHOUT_PROVENANCE"
    assert SV.counted(ev("historical_test", data_sha256=None), spec())[1] == "OFFLINE_PASS_WITHOUT_DATA_HASH"
    assert SV.counted(ev("historical_test", data_sha256="xyz"), spec())[1] == "OFFLINE_PASS_WITHOUT_DATA_HASH"
    assert "OWNER_RESULT_NOT_RECONCILED" in SV.counted(ev("reconciled_owner_result", status="PROVISIONAL"), spec())[1]       # only the importer's RECONCILED counts
    assert SV.counted(ev("historical_test", "FAIL", provenance=""), spec()) == (True, None)                                    # a FAIL always counts: killing is easy


def test_a_not_counted_pass_leaves_the_candidate_where_it_was():
    bad = ev("historical_test", metrics={"n_variants_tested": 0})
    r = SV.evaluate(spec(), [bad])
    assert r.verdict == SV.INSUFFICIENT_DATA and r.stages["historical_test"].startswith("NOT_COUNTED")


# ---------------------------------------------------------------- the frozen BTC experiment is described, never promoted by a return

def test_the_frozen_btc_candidate_is_valid_untested_and_its_plan_is_untouched():
    import carry_prospective as CP
    c = SV.btc_carry_candidate()
    assert SV.spec_problems(c) == []
    assert c["OOS_protocol"]["plan_digests"] == {f"{h:g}": CP.PINNED_DIGESTS[h] for h in CP.HURDLES_PCT}
    assert SV.evaluate(c, []).verdict == SV.UNTESTED
    assert SV.evaluate(c, [ev("historical_test", metrics={"net_edge_bps_after_costs": 50.0})]).verdict == SV.INSUFFICIENT_DATA


# ---------------------------------------------------------------- the registry link

def test_a_candidate_registers_in_the_tamper_evident_registry_and_evidence_maps_to_levels():
    import hypothesis_registry as hr
    reg = {"trials": []}
    SV.register_candidate(reg, spec(), code_sha="abc123", input_data_sha256={"funding": H})
    hr.seal(reg)
    assert hr.verify(reg) == [] and reg["trials"][0]["trial_id"] == "cand1"
    SV.record_evidence(reg, "cand1", ev("oos_test"))
    SV.record_evidence(reg, "cand1", ev("execution_validation"))
    st = hr.evidence_status(reg["trials"][0])
    assert st["OUT_OF_SAMPLE"] == "oos_test:PASS" and st["TESTNET_MECHANICS"] == "execution_validation:PASS" and st["OWNER_ACCOUNT_VERIFIED"] is None
    assert hr.verify(reg) == []
    bad = spec(); bad.pop("counterparty")
    with pytest.raises(SV.SpecError, match="invalid candidate"):
        SV.register_candidate({"trials": []}, bad, code_sha="x", input_data_sha256={})


def test_the_cli_prints_the_verdict_and_refuses_unreadable_input(tmp_path, capsys):
    cand = tmp_path / "c.json"; cand.write_text(json.dumps(spec()))
    evf = tmp_path / "e.jsonl"
    evf.write_text("".join(json.dumps({"stage": s, "result": "PASS", "provenance": "r", "data_sha256": H, "metrics": ev(s).metrics}) + "\n" for s in OFFLINE))
    assert SV.main(["--candidate", str(cand), "--evidence", str(evf)]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == SV.PROMISING
    assert SV.main(["--candidate", str(tmp_path / "nope.json")]) == 2
    evf.write_text('{"stage": "nonsense", "result": "PASS"}\n')
    assert SV.main(["--candidate", str(cand), "--evidence", str(evf)]) == 2
