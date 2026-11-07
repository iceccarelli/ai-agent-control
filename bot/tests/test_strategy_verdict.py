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

H = "0123456789abcdef" * 4
FUTURE_MS = 4_000_000_000_000        # an observation that starts after any registration made while these tests run


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


def ev(stage, result="PASS", spec_=None, **kw):
    base = {"provenance": "run#1 offline harness", "data_sha256": H, "candidate_id": "cand1", "spec_digest": SV.spec_digest(spec_ if spec_ is not None else spec())}
    m = {}
    if stage in ("historical_test", "oos_test"):
        m = {"independent_observations": 500, "n_variants_tested": 3, "multiple_testing_adjusted": True, "uses_future_information": False, "universe_point_in_time": True}
    elif stage == "cost_model":
        m = {"unknown_cost_inputs": 0, "net_edge_bps_after_costs": 2.5}
    elif stage == "capacity_test":
        m = {"capacity_usd": 5000.0}
    elif stage == "prospective_observation":
        m = {"complete": True, "observation_started_ms": FUTURE_MS}
    elif stage == "execution_validation":
        m = {"venue": "testnet", "fills_measured": True, "fees_measured": True, "observation_started_ms": FUTURE_MS}
        base.update({"provenance": "testnet session 1", "data_sha256": None})
    elif stage == "reconciled_owner_result":
        m = {"net_usd_after_all_costs": 12.5, "observation_started_ms": FUTURE_MS, "import_report_sha256": H}
        base.update({"provenance": "owner_import report", "status": "RECONCILED", "data_sha256": None})
    metrics = dict(m, **kw.pop("metrics", {}))
    base.update(kw)
    return SV.EvidenceItem(stage=stage, result=result, metrics=metrics, **base)


OFFLINE = ["historical_test", "oos_test", "cost_model", "capacity_test"]


def _registry():
    import hypothesis_registry as hr
    reg = {"trials": []}
    SV.register_candidate(reg, spec(), code_sha="abc123", input_data_sha256={"funding": H})
    hr.seal(reg)
    return reg


REG = _registry()


def evaluate(s, items, registry=REG):
    return SV.evaluate(s, items, registry=registry)


def verdict(s, *items):
    return evaluate(s, list(items)).verdict


# ---------------------------------------------------------------- the specification

def test_a_complete_specification_is_valid_and_untested():
    assert SV.spec_problems(spec()) == []
    r = evaluate(spec(), [])
    assert r.verdict == SV.UNTESTED and r.next_stage == "historical_test"


@pytest.mark.parametrize("field", SV.REQUIRED_SPEC_FIELDS)
def test_every_required_field_is_required(field):
    s = spec(); s.pop(field)
    assert f"MISSING:{field}" in SV.spec_problems(s) and evaluate(s, [ev("historical_test")]).verdict == SV.UNTESTED


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
    r = evaluate(spec(), [ev("historical_test")])
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
        r = evaluate(s, items)
        assert r.verdict == SV.REJECTED and r.next_stage is None and f"{stage} FAILED" in r.reasons


def test_a_declared_rejection_condition_that_fires_kills_the_candidate():
    items = [ev(x) for x in OFFLINE]
    items[2] = ev("cost_model", metrics={"rejection_conditions_fired": ["net edge after costs <= 0 out of sample"]})
    assert verdict(spec(), *items) == SV.REJECTED


def test_evidence_recorded_out_of_order_is_ignored_and_reported():
    r = evaluate(spec(), [ev("prospective_observation"), ev("execution_validation"), ev("reconciled_owner_result")])
    assert r.verdict == SV.INSUFFICIENT_DATA or r.verdict == SV.UNTESTED
    assert len(r.ignored) == 3 and all(v.startswith("NOT_COUNTED:OUT_OF_ORDER") for k, v in r.stages.items() if k in ("prospective_observation", "execution_validation", "reconciled_owner_result"))
    r2 = evaluate(spec(), [ev("oos_test"), ev("cost_model"), ev("capacity_test")])                    # no historical test: nothing after it counts
    assert r2.verdict in (SV.UNTESTED, SV.INSUFFICIENT_DATA) and r2.ignored


def test_the_verdict_is_deterministic_and_order_of_evidence_does_not_matter():
    items = [ev(x) for x in OFFLINE] + [ev("prospective_observation")]
    a = evaluate(spec(), items).as_dict()
    b = evaluate(spec(), list(reversed(items))).as_dict()
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
    ("cost_model", {"net_edge_bps_after_costs": 600.0}, "NET_EDGE_IMPLAUSIBLY_LARGE"),
    ("cost_model", {"unknown_cost_inputs": False}, "UNKNOWN_COST_INPUTS"),
    ("cost_model", {"unknown_cost_inputs": 0.0}, "UNKNOWN_COST_INPUTS"),
    ("capacity_test", {"capacity_usd": 0}, "CAPACITY_BELOW"),
    ("capacity_test", {"capacity_usd": 999.0}, "CAPACITY_BELOW"),
    ("prospective_observation", {"observation_started_ms": 1}, "OBSERVATION_DID_NOT_START_AFTER_REGISTRATION"),
    ("prospective_observation", {"observation_started_ms": True}, "OBSERVATION_DID_NOT_START_AFTER_REGISTRATION"),
    ("prospective_observation", {"complete": False}, "PROSPECTIVE_NOT_COMPLETE"),
    ("reconciled_owner_result", {"import_report_sha256": "nothex"}, "NOT_TIED_TO_AN_IMPORT_REPORT_HASH"),
    ("execution_validation", {"venue": "paper"}, "EXECUTION_NOT_MEASURED"),
    ("execution_validation", {"fees_measured": False}, "EXECUTION_NOT_MEASURED"),
    ("reconciled_owner_result", {"net_usd_after_all_costs": -1.0}, "OWNER_RESULT_NOT_RECONCILED"),
    ("reconciled_owner_result", {"net_usd_after_all_costs": 0.0}, "OWNER_RESULT_NOT_RECONCILED"),
])
def test_a_pass_that_does_not_carry_what_it_needs_does_not_count(stage, mutation, why):
    item = ev(stage, metrics=mutation)
    ok, reason = SV.counted(item, spec(), REG)
    assert ok is False and why in reason
    assert SV.counted(ev(stage), spec(), REG) == (True, None)                    # positive control


def test_provenance_hash_and_status_are_required_for_a_pass():
    c = lambda item: SV.counted(item, spec(), REG)
    assert c(ev("historical_test", provenance=""))[1] == "PASS_WITHOUT_REAL_PROVENANCE"
    for weak in ("unknown", "ASSUMED", "modelled", " estimated ", "x", "short"):
        assert c(ev("historical_test", provenance=weak))[1] == "PASS_WITHOUT_REAL_PROVENANCE", weak
    assert c(ev("historical_test", data_sha256=None))[1] == "OFFLINE_PASS_WITHOUT_REAL_DATA_HASH"
    assert c(ev("historical_test", data_sha256="xyz"))[1] == "OFFLINE_PASS_WITHOUT_REAL_DATA_HASH"
    assert c(ev("historical_test", data_sha256="0" * 64))[1] == "OFFLINE_PASS_WITHOUT_REAL_DATA_HASH"          # a trivial all-zero hash is not a hash of data
    assert "OWNER_RESULT_NOT_RECONCILED" in c(ev("reconciled_owner_result", status="PROVISIONAL"))[1]          # only the importer's RECONCILED counts
    assert c(ev("historical_test", "FAIL", provenance="")) == (True, None)                                      # a FAIL always counts: killing is easy


# ---------------------------------------------------------------- red-team forgeries (bb0604d): every one must fail to promote

def test_evidence_must_be_about_this_candidate_and_this_exact_spec():
    other = ev("historical_test", candidate_id="someone_else")
    assert SV.counted(other, spec(), REG)[1] == "EVIDENCE_NOT_ABOUT_THIS_CANDIDATE"
    stale = ev("historical_test", spec_digest=SV.spec_digest(spec(entry="a different entry rule")))
    assert SV.counted(stale, spec(), REG)[1] == "EVIDENCE_GATHERED_AGAINST_A_DIFFERENT_SPEC"
    items = [ev(x) for x in OFFLINE]
    assert verdict(spec(), *items) == SV.PROMISING
    assert verdict(spec(capacity_model={"min_viable_capacity_usd": 1.0}), *items) == SV.INSUFFICIENT_DATA        # editing the spec AFTER the evidence invalidates it


def test_the_observed_stages_need_a_verifying_registry_that_predates_the_observation():
    base = [ev(x) for x in OFFLINE]
    obs = [ev("prospective_observation"), ev("execution_validation"), ev("reconciled_owner_result")]
    assert verdict(spec(), *base, *obs) == SV.OWNER_PROFIT_EVIDENCED                                              # the honest path
    assert evaluate(spec(), base + obs, registry=None).verdict in (SV.PROMISING, SV.PROSPECTIVE)                  # no registry: self-declared 'registered' counts for nothing
    assert evaluate(spec(), base + obs, registry={"trials": []}).verdict in (SV.PROMISING, SV.PROSPECTIVE)       # candidate never registered
    import copy, hypothesis_registry as hr
    tampered = copy.deepcopy(REG); tampered["trials"][0]["costs"] = "cheaper"
    assert hr.verify(tampered)
    assert evaluate(spec(), base + obs, registry=tampered).verdict in (SV.PROMISING, SV.PROSPECTIVE)             # a registry that fails its own check does not back anything
    early = [ev("prospective_observation", metrics={"observation_started_ms": 1})]
    assert evaluate(spec(), base + early, registry=REG).verdict == SV.PROMISING                                   # observation began BEFORE registration: not prospective


def test_metrics_are_normalised_so_a_lying_mapping_or_a_nan_cannot_reach_the_verdict():
    class Liar(dict):
        def get(self, k, d=None):
            return None if k == "rejection_conditions_fired" else super().get(k, d)
    with pytest.raises(SV.SpecError):
        SV.EvidenceItem("cost_model", "PASS", "run#1 offline harness", metrics={"net_edge_bps_after_costs": float("nan")}, data_sha256=H)
    liar = SV.EvidenceItem("cost_model", "PASS", "run#1 offline harness", metrics=Liar(ev("cost_model").metrics, rejection_conditions_fired=["x"]), data_sha256=H,
                           candidate_id="cand1", spec_digest=SV.spec_digest(spec()))
    assert type(liar.metrics) is dict and liar.metrics["rejection_conditions_fired"] == ["x"]                    # the real value survives; the lying .get cannot hide it


@pytest.mark.parametrize("fired", [["x"], "x", 1, {"a": 1}])
def test_any_nonempty_rejection_claim_kills_not_only_a_list(fired):
    items = [ev(x) for x in OFFLINE]
    items[0] = ev("historical_test", metrics={"rejection_conditions_fired": fired})
    assert verdict(spec(), *items) == SV.REJECTED


def test_a_failed_hypothesis_is_terminal():
    assert verdict(spec(), ev("hypothesis", "FAIL", metrics={})) == SV.REJECTED


def test_a_dropped_fail_cannot_hide_when_the_verdict_comes_from_the_sealed_registry():
    import copy, hypothesis_registry as hr
    reg = copy.deepcopy(REG)
    for x in OFFLINE:
        SV.record_evidence(reg, "cand1", ev(x))
    SV.record_evidence(reg, "cand1", ev("oos_test", "FAIL", metrics={}))                                          # an inconvenient failure is on the record
    hr.seal(reg)
    assert SV.evaluate_registered(reg, spec()).verdict == SV.REJECTED
    reg["trials"][0]["evidence"] = [e for e in reg["trials"][0]["evidence"] if not e["result"].endswith("FAIL")]   # someone deletes it
    with pytest.raises(SV.SpecError, match="integrity"):
        SV.evaluate_registered(reg, spec())


def test_forged_owner_results_do_not_count():
    base = [ev(x) for x in OFFLINE] + [ev("prospective_observation"), ev("execution_validation")]
    forged = ev("reconciled_owner_result", metrics={"net_usd_after_all_costs": 1e9, "observation_started_ms": FUTURE_MS, "import_report_sha256": H})
    assert verdict(spec(), *base, forged) == SV.OWNER_PROFIT_EVIDENCED                                           # a huge net is not itself disqualifying: the RECONCILED status + report hash are the gate
    no_hash = ev("reconciled_owner_result", metrics={"import_report_sha256": None})                              # the default metrics are merged: override the hash with nothing
    assert verdict(spec(), *base, no_hash) == SV.QUALIFIED


def test_a_not_counted_pass_leaves_the_candidate_where_it_was():
    bad = ev("historical_test", metrics={"n_variants_tested": 0})
    r = evaluate(spec(), [bad])
    assert r.verdict == SV.INSUFFICIENT_DATA and r.stages["historical_test"].startswith("NOT_COUNTED")


# ---------------------------------------------------------------- the frozen BTC experiment is described, never promoted by a return

def test_the_frozen_btc_candidate_is_valid_untested_and_its_plan_is_untouched():
    import carry_prospective as CP
    c = SV.btc_carry_candidate()
    assert SV.spec_problems(c) == []
    assert c["OOS_protocol"]["plan_digests"] == {f"{h:g}": CP.PINNED_DIGESTS[h] for h in CP.HURDLES_PCT}
    assert evaluate(c, []).verdict == SV.UNTESTED
    assert evaluate(c, [ev("historical_test", metrics={"net_edge_bps_after_costs": 50.0})]).verdict == SV.INSUFFICIENT_DATA


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
    evf.write_text("".join(json.dumps({"stage": s, "result": "PASS", "provenance": "run#1 offline harness", "data_sha256": H, "metrics": ev(s).metrics, "candidate_id": "cand1",
                                                                 "spec_digest": SV.spec_digest(spec())}) + "\n" for s in OFFLINE))
    assert SV.main(["--candidate", str(cand), "--evidence", str(evf)]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == SV.PROMISING
    assert SV.main(["--candidate", str(tmp_path / "nope.json")]) == 2
    evf.write_text('{"stage": "nonsense", "result": "PASS"}\n')
    assert SV.main(["--candidate", str(cand), "--evidence", str(evf)]) == 2
