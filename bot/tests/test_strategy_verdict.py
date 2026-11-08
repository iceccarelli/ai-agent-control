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
H2 = "fedcba9876543210" * 4
DAY = 86_400_000
REG_UTC = "2026-01-01T00:00:00Z"      # every registry here is registered at this fixed instant (patched), so observations can be placed deterministically after it
FUTURE_MS = 4_000_000_000_000        # an observation that starts after any registration made while these tests run
NOW_MS = FUTURE_MS + 90 * DAY         # the clock handed to the engine: after every observation window built below
KEY = "test-only-owner-key-0123456789"     # the owner's secret for THESE tests only; production reads OWNER_ATTESTATION_KEY, which this repository never holds
KEYP = lambda: KEY                          # injected key provider
_AUTO = object()


def lab(v, p="assumed"):
    return {"value": v, "provenance": p}


def spec(**over):
    s = {
        "candidate_id": "cand1", "family": "test_family", "hypothesis": "h", "economic_mechanism": "someone pays", "counterparty": "levered longs",
        "required_data": [{"name": "funding", "point_in_time": True}], "eligibility_rules": ["listed >= 30d"], "entry": "e", "exit": "x", "holding_period": "30d",
        "execution_model": "taker", "fee_model": {"maker_bps": lab(2.0), "taker_bps": lab(5.5)}, "funding_model": {"expected_bps_per_settlement": lab(0.3, "modelled")},
        "slippage_model": {"entry_bps": lab(1.0), "exit_bps": lab(1.0)}, "capacity_model": {"min_viable_capacity_usd": 1000.0}, "risk_model": "r", "drawdown_model": "d",
        "OOS_protocol": {"train_test_boundary": "2024-01-01", "data_window": "2022..2026"}, "prospective_protocol": {"description": "p", "min_duration_ms": 30 * DAY},
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
        m = {"unknown_cost_inputs": 0, "net_edge_bps_after_costs": 2.5, "chain_digest": SV.cost_chain_digest(spec_ if spec_ is not None else spec())}
    elif stage == "capacity_test":
        m = {"capacity_usd": 5000.0, "min_viable_capacity_usd": 1000.0, "chain_digest": SV.cost_chain_digest(spec_ if spec_ is not None else spec())}
    elif stage == "prospective_observation":
        m = {"complete": True, "observation_started_ms": FUTURE_MS, "observation_ended_ms": FUTURE_MS + 30 * DAY, "observed_settlements": 90, "coverage": 0.97}
        base["recorded_ms"] = FUTURE_MS + 30 * DAY                                  # recorded when the observation ended
    elif stage == "execution_validation":
        m = {"venue": "testnet", "fills_measured": True, "fees_measured": True, "observation_started_ms": FUTURE_MS, "fills_count": 25, "fee_observations_count": 25,
             "execution_log_sha256": H2, "candidate_id": "cand1", "spec_digest": SV.spec_digest(spec_ if spec_ is not None else spec())}
        base.update({"provenance": "testnet session 1", "data_sha256": None})
    elif stage == "reconciled_owner_result":
        if result == "PASS" and not kw and spec_ is None:
            return owner_item()                                                      # the ONLY way to build an owner result: from a sealed importer report
        m = {"net_usd_after_all_costs": 12.5, "observation_started_ms": FUTURE_MS, "import_report_sha256": H}
        base.update({"provenance": "owner_import report", "status": "RECONCILED", "data_sha256": None})
    metrics = dict(m, **kw.pop("metrics", {}))
    attest = kw.pop("attest", True)
    base.update(kw)
    item = SV.EvidenceItem(stage=stage, result=result, metrics=metrics, **base)
    return SV.attest_item(item, KEY) if attest and stage in SV.OBSERVED_STAGES else item          # observed stages are owner-attested by default


OFFLINE = ["historical_test", "oos_test", "cost_model", "capacity_test"]


def _registry(s_=None, **kw):
    import hypothesis_registry as hr
    from unittest import mock
    reg = {"trials": []}
    with mock.patch.object(hr, "_now", lambda: REG_UTC):
        SV.register_candidate(reg, s_ if s_ is not None else spec(), code_sha="abc123", input_data_sha256=kw.get("hashes", {"funding": H}))
        for other in kw.get("others", ()):
            SV.register_candidate(reg, other, code_sha="abc123", input_data_sha256={"funding": H})
    hr.seal(reg)
    return reg


_OWNER = {}


def import_report(spot_close=61_000.0, **kw):
    """A genuinely importer-sealed report from synthetic fixtures (BTC +1000 over the window by default: economic total positive)."""
    import tempfile, pathlib
    key = (spot_close, tuple(sorted(kw.items())))
    if key not in _OWNER:
        sys.path.insert(0, os.path.join(BOT, "tests"))
        import owner_import as OI, fixtures_owner_import as F
        import account_review as AR
        d = pathlib.Path(tempfile.mkdtemp())
        _OWNER[key] = OI.run_import(F.lay_out(d, F.Trip(), **kw), marks=F.marks(spot_close=spot_close))
    return copy.deepcopy(_OWNER[key])


def owner_item(report=None, s_=None, attest=True):
    it = SV.evidence_from_import_report(report if report is not None else import_report(), s_ if s_ is not None else spec())
    return SV.attest_item(it, KEY) if attest else it


REG = _registry()


def anchor_of(registry):
    import hypothesis_registry as hr
    return hr.anchor_digest(registry) if registry is not None and registry.get("trials") else None


def evaluate(s, items, registry=REG, anchor=_AUTO, key_provider=KEYP):
    """The owner-held anchor and owner key are supplied by default (the honest path); pass anchor=None / key_provider=lambda: None for the unanchored / keyless cases."""
    return SV.evaluate(s, items, registry=registry, now_ms=NOW_MS, anchor=anchor_of(registry) if anchor is _AUTO else anchor, key_provider=key_provider)


def counted(item, s, registry=None, key_provider=KEYP):
    return SV.counted(item, s, registry, now_ms=NOW_MS, key_provider=key_provider)


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
    ("prospective_observation", {"observation_ended_ms": FUTURE_MS + 29 * DAY}, "PROSPECTIVE_DURATION_SHORTER"),
    ("execution_validation", {"venue": "paper"}, "EXECUTION_NOT_MEASURED"),
    ("execution_validation", {"fees_measured": False}, "EXECUTION_NOT_MEASURED"),
])
def test_a_pass_that_does_not_carry_what_it_needs_does_not_count(stage, mutation, why):
    item = ev(stage, metrics=mutation)
    ok, reason = counted(item, spec(), REG)
    assert ok is False and why in reason
    assert counted(ev(stage), spec(), REG) == (True, None)                    # positive control


def test_provenance_hash_and_status_are_required_for_a_pass():
    c = lambda item: counted(item, spec(), REG)
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
    assert counted(other, spec(), REG)[1] == "EVIDENCE_NOT_ABOUT_THIS_CANDIDATE"
    stale = ev("historical_test", spec_digest=SV.spec_digest(spec(entry="a different entry rule")))
    assert counted(stale, spec(), REG)[1] == "EVIDENCE_GATHERED_AGAINST_A_DIFFERENT_SPEC"
    items = [ev(x) for x in OFFLINE]
    assert verdict(spec(), *items) == SV.PROMISING
    assert verdict(spec(capacity_model={"min_viable_capacity_usd": 1.0}), *items) == SV.INSUFFICIENT_DATA        # editing the spec AFTER the evidence invalidates it


def test_the_observed_stages_need_a_verifying_registry_that_predates_the_observation():
    base = [ev(x) for x in OFFLINE]
    obs = [ev("prospective_observation"), ev("execution_validation"), ev("reconciled_owner_result")]
    assert verdict(spec(), *base, *obs) == SV.OWNER_PROFIT_EVIDENCED                                              # the honest path
    assert evaluate(spec(), base + obs, registry=None).verdict == SV.INSUFFICIENT_DATA                            # no registry: self-declared 'registered' counts for nothing
    assert evaluate(spec(), base + obs, registry={"trials": []}).verdict == SV.INSUFFICIENT_DATA                 # candidate never registered
    import copy, hypothesis_registry as hr
    tampered = copy.deepcopy(REG); tampered["trials"][0]["costs"] = "cheaper"
    assert hr.verify(tampered)
    assert evaluate(spec(), base + obs, registry=tampered, anchor=None).verdict == SV.INSUFFICIENT_DATA                       # a registry that fails its own check does not back anything
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
    assert SV.evaluate_registered(reg, spec(), now_ms=NOW_MS).verdict == SV.REJECTED
    reg["trials"][0]["evidence"] = [e for e in reg["trials"][0]["evidence"] if not e["result"].endswith("FAIL")]   # someone deletes it
    with pytest.raises(SV.SpecError, match="integrity"):
        SV.evaluate_registered(reg, spec(), now_ms=NOW_MS)


def test_forged_owner_results_do_not_count():
    base = [ev(x) for x in OFFLINE] + [ev("prospective_observation"), ev("execution_validation")]
    assert verdict(spec(), *base, ev("reconciled_owner_result")) == SV.OWNER_PROFIT_EVIDENCED                    # positive control: built from a sealed importer report
    forged = SV.EvidenceItem(stage="reconciled_owner_result", result="PASS", provenance="owner_import report", status="RECONCILED", candidate_id="cand1",
                             spec_digest=SV.spec_digest(spec()), metrics={"net_usd_after_all_costs": 1e9, "observation_started_ms": FUTURE_MS, "import_report_sha256": H})
    assert counted(forged, spec(), REG) == (False, "OWNER_RESULT_HAS_NO_ATTACHED_IMPORT_REPORT")
    assert verdict(spec(), *base, forged) == SV.QUALIFIED                                                        # a free-form figure with a made-up hash is not an owner result


# ---------------------------------------------------------------- red-team 2: the ladder cannot be climbed on fiction

def _registered(**kw):
    return _registry(**kw)


def test_rt2_1a_offline_data_hashes_must_be_the_registered_input_hashes():
    other = [ev(x, data_sha256=H2) for x in OFFLINE]                                    # a perfectly hash-shaped, unregistered hash on every stage
    for item in other:
        assert counted(item, spec(), REG) == (False, "OFFLINE_DATA_HASH_NOT_IN_REGISTERED_INPUTS")
    assert evaluate(spec(), other).verdict == SV.INSUFFICIENT_DATA
    assert counted(other[0], spec(), None) == (True, None)                               # no registry: it can count toward PROMISING, and no further (below)
    assert all(counted(ev(x), spec(), REG) == (True, None) for x in OFFLINE)             # positive control: the registered hash
    reg2 = _registered(hashes={"funding": H, "costs": H2})
    assert counted(other[0], spec(), reg2) == (True, None)                               # registering the hash is what binds it


def test_rt2_1a_without_a_registry_the_ladder_stops_below_qualified():
    full = [ev(x) for x in OFFLINE] + [ev("prospective_observation"), ev("execution_validation"), ev("reconciled_owner_result")]
    assert evaluate(spec(), full, registry=None).verdict == SV.INSUFFICIENT_DATA
    r = evaluate(spec(), [ev(x) for x in OFFLINE], registry=None)
    assert r.verdict == SV.INSUFFICIENT_DATA and any("NO_VERIFYING_REGISTRY" in x for x in r.reasons)          # said out loud: an evidence-only run is never PROMISING


def test_rt2_1b_variant_count_is_cross_checked_against_the_registry_and_the_metrics():
    fam = spec(multiple_testing_family="fam")
    sib = spec(candidate_id="sibling", multiple_testing_family="fam")
    reg = _registered(s_=fam, others=[sib])
    one = ev("historical_test", spec_=fam, metrics={"n_variants_tested": 1})
    assert counted(one, fam, reg) == (False, "N_VARIANTS_BELOW_REGISTERED_COUNT")      # two registered trials share the family: 1 cannot be right
    assert counted(ev("historical_test", spec_=fam, metrics={"n_variants_tested": 2}), fam, reg) == (True, None)
    grid = spec(parameter_grid={"a": [1, 2, 3], "b": [1, 2]})
    greg = _registered(s_=grid)
    assert counted(ev("historical_test", spec_=grid, metrics={"n_variants_tested": 3}), grid, greg) == (False, "N_VARIANTS_BELOW_REGISTERED_COUNT")      # 3 x 2 grid cells
    assert counted(ev("historical_test", spec_=grid, metrics={"n_variants_tested": 6}), grid, greg) == (True, None)
    # what the evidence itself implies binds even with no registry at all
    implied = ev("oos_test", metrics={"n_variants_tested": 3, "variant_names": ["a", "b", "c", "d", "e"]})
    assert counted(implied, spec(), None) == (False, "N_VARIANTS_BELOW_WHAT_THE_EVIDENCE_IMPLIES")
    assert counted(ev("oos_test", metrics={"n_variants_tested": 5, "variant_names": ["a", "b", "c", "d", "e"]}), spec(), None) == (True, None)
    assert counted(ev("oos_test", metrics={"n_variants_tested": 3, "n_variants_attempted": 9}), spec(), None) == (False, "N_VARIANTS_BELOW_WHAT_THE_EVIDENCE_IMPLIES")


@pytest.mark.parametrize("mut,why", [
    ({"observation_ended_ms": FUTURE_MS + 1000}, "PROSPECTIVE_DURATION_SHORTER_THAN_REGISTERED_WINDOW"),         # complete:True with no real duration
    ({"observation_ended_ms": None}, "OBSERVATION_END_INVALID_OR_IN_THE_FUTURE"),
    ({"observation_ended_ms": NOW_MS + DAY}, "OBSERVATION_END_INVALID_OR_IN_THE_FUTURE"),                          # claims to end after now
    ({"observation_ended_ms": FUTURE_MS - 1}, "OBSERVATION_END_INVALID_OR_IN_THE_FUTURE"),                         # ends before it starts
    ({"observed_settlements": 3}, "TOO_FEW_OBSERVED_SETTLEMENTS"),
    ({"observed_settlements": None}, "TOO_FEW_OBSERVED_SETTLEMENTS"),
    ({"coverage": 0.5}, "COVERAGE_BELOW_FLOOR"),
    ({"coverage": 1.5}, "COVERAGE_BELOW_FLOOR"),
    ({"coverage": True}, "COVERAGE_BELOW_FLOOR"),
])
def test_rt2_1c_a_prospective_pass_needs_a_real_observed_duration_count_and_coverage(mut, why):
    ok, got = counted(ev("prospective_observation", metrics=mut), spec(), REG)
    assert ok is False and got.startswith(why), got
    assert counted(ev("prospective_observation"), spec(), REG) == (True, None)


def test_rt2_1c_the_registered_window_is_machine_readable_or_prospective_cannot_count():
    for proto in ("free text only", {"description": "no duration"}):
        s = spec(prospective_protocol=proto)
        reg = _registered(s_=s)
        assert counted(ev("prospective_observation", spec_=s), s, reg) == (False, "PROSPECTIVE_WINDOW_NOT_MACHINE_READABLE")
    s = spec(prospective_protocol=f"{FUTURE_MS}..{FUTURE_MS + 30 * DAY}")                 # the 'start..end' form carries its own duration
    assert counted(ev("prospective_observation", spec_=s), s, _registered(s_=s)) == (True, None)
    assert counted(ev("prospective_observation", spec_=s, metrics={"observation_ended_ms": FUTURE_MS + DAY}), s, _registered(s_=s))[0] is False
    floor = spec(prospective_protocol={"min_duration_ms": 30 * DAY, "min_settlements": 200, "min_coverage": 0.99})       # a protocol can only RAISE the floors
    assert counted(ev("prospective_observation", spec_=floor), floor, _registered(s_=floor))[1].startswith("TOO_FEW_OBSERVED_SETTLEMENTS")


@pytest.mark.parametrize("mut", [{"fills_count": 0}, {"fills_count": 3}, {"fills_count": True}, {"fee_observations_count": 0}, {"execution_log_sha256": None},
                                 {"execution_log_sha256": "0" * 64}, {"execution_log_sha256": "nothex"}, {"candidate_id": "someone_else"}, {"spec_digest": H}])
def test_rt2_1e_execution_validation_needs_measured_counts_and_a_log_hash_tied_to_the_candidate(mut):
    ok, why = counted(ev("execution_validation", metrics=mut), spec(), REG)
    assert ok is False and why.startswith("EXECUTION_NOT_MEASURED")
    flags_only = ev("execution_validation", metrics={k: None for k in ("fills_count", "fee_observations_count", "execution_log_sha256")})
    assert counted(flags_only, spec(), REG)[0] is False                                   # the three self-asserted flags alone are not enough
    assert counted(ev("execution_validation"), spec(), REG) == (True, None)


def test_rt2_1d_owner_result_is_built_only_from_a_sealed_reconciled_report_and_recomputes_the_hash():
    rep = import_report()
    item = owner_item(rep)
    assert item.result == SV.PASS and item.status == "RECONCILED" and item.metrics["import_report_sha256"] == rep["report_sha256"]
    assert item.metrics["net_usd_after_all_costs"] == rep["layers"]["economic_total"]["net_economic_pnl_usd"] > 0           # the figure comes from the report, not the caller
    assert item.metrics["net_usd_after_all_costs"] != rep["headline_realised_cash_usd"]                                    # and it is the ECONOMIC total, not the cash headline
    assert counted(item, spec(), REG) == (True, None)
    # tamper with the figure inside the attachment: the hash no longer recomputes
    t = copy.deepcopy(item.attachment); t["layers"]["economic_total"]["net_economic_pnl_usd"] = 1e9
    forged = SV.EvidenceItem(stage=item.stage, result="PASS", provenance=item.provenance, status="RECONCILED", candidate_id=item.candidate_id, spec_digest=item.spec_digest,
                             metrics=dict(item.metrics, net_usd_after_all_costs=1e9), attachment=t)
    ok, why = counted(forged, spec(), REG)
    assert ok is False and why.startswith("OWNER_REPORT_FAILS_VERIFICATION") and "does_not_recompute" in why
    # a hash that is not the attached report's
    wrong = SV.EvidenceItem(stage=item.stage, result="PASS", provenance=item.provenance, status="RECONCILED", candidate_id=item.candidate_id, spec_digest=item.spec_digest,
                            metrics=dict(item.metrics, import_report_sha256=H), attachment=item.attachment)
    assert counted(wrong, spec(), REG) == (False, "OWNER_HASH_NOT_RECOMPUTED_FROM_THE_ATTACHED_REPORT")
    # a figure that is not the attached report's economic total
    lie = SV.EvidenceItem(stage=item.stage, result="PASS", provenance=item.provenance, status="RECONCILED", candidate_id=item.candidate_id, spec_digest=item.spec_digest,
                          metrics=dict(item.metrics, net_usd_after_all_costs=item.metrics["net_usd_after_all_costs"] + 1.0), attachment=item.attachment)
    assert counted(lie, spec(), REG) == (False, "OWNER_FIGURE_NOT_THE_REPORTS_ECONOMIC_TOTAL")
    # a hand-made report that merely claims RECONCILED
    handmade = {"status": "RECONCILED", "report_sha256": H, "layers": {"economic_total": {"status": "ESTIMATED", "net_economic_pnl_usd": 1e9}}}
    with pytest.raises(SV.SpecError):
        SV.evidence_from_import_report(handmade, spec())


def test_rt2_2_the_owner_figure_is_the_economic_total_and_a_none_or_nonpositive_total_never_passes():
    neg = import_report(spot_close=59_000.0)                                                  # realised cash +476, owned BTC -1000: economic total negative
    assert neg["headline_realised_cash_usd"] > 0 > neg["layers"]["economic_total"]["net_economic_pnl_usd"]
    it = owner_item(neg)
    assert it.result == SV.FAIL and it.metrics["net_usd_after_all_costs"] < 0                 # the profitable-looking cash headline cannot carry it
    pa = [ev(x) for x in OFFLINE] + [ev("prospective_observation"), ev("execution_validation")]
    assert verdict(spec(), *pa, it) == SV.REJECTED
    # same report with the PASS forced: counted() refuses it on the economic figure
    forced = SV.EvidenceItem(stage=it.stage, result="PASS", provenance=it.provenance, status="RECONCILED", candidate_id=it.candidate_id, spec_digest=it.spec_digest,
                             metrics=it.metrics, attachment=it.attachment)
    ok, why = counted(forced, spec(), REG)
    assert ok is False and "OWNER_ECONOMIC_TOTAL_NOT_POSITIVE" in why
    # a withheld (None) / stale / unavailable total
    for bad_status, bad_net in (("STALE", None), ("UNAVAILABLE", None), ("ESTIMATED", None), ("STALE", 500.0)):
        rep = import_report()
        rep["layers"]["economic_total"].update(status=bad_status, net_economic_pnl_usd=bad_net)
        rep = _reseal(rep)
        item = SV.evidence_from_import_report(rep, spec())
        assert item.result == SV.INCONCLUSIVE, (bad_status, bad_net)
        f2 = SV.EvidenceItem(stage=item.stage, result="PASS", provenance=item.provenance, status="RECONCILED", candidate_id=item.candidate_id, spec_digest=item.spec_digest,
                             metrics=dict(item.metrics, net_usd_after_all_costs=500.0), attachment=item.attachment)
        assert counted(f2, spec(), REG)[0] is False, (bad_status, bad_net)


def _reseal(rep):
    import owner_import as OI
    lin = rep["lineage"]
    for path, e in lin["figures"].items():
        cur = rep
        for k in path.split("."):
            cur = cur[k]
        e["value"] = cur
        e["provenance_digest"] = OI._sha_text(OI._canon({k: e.get(k) for k in ("value", "formula", "sources", "derived_digests", "all_records_digest", "empty_set")}))
    fh = {p: [e["value"], e["provenance_digest"]] for p, e in sorted(lin["figures"].items())}
    lin["digest"] = OI._sha_text(OI._canon({"files": [[e["role"], e["sha256"]] for e in lin["files"]], "marks": lin.get("marks"), "stages": lin["stages"], "figures": fh}))
    rep["report_sha256"] = OI._sha_text(OI._canon({k: v for k, v in rep.items() if k != "report_sha256"}))
    return rep


def test_rt2_3_a_prospective_promotion_needs_the_same_binding_as_counted():
    s = spec()
    off = [ev(x) for x in OFFLINE]
    good = ev("prospective_observation", "INCONCLUSIVE", metrics={"complete": False})
    assert verdict(s, *off, good) == SV.PROSPECTIVE                                      # honest pending observation, registered before it started
    # the metric's own claim of 'registered_before_observation' is never trusted
    claim = lambda **o: ev("prospective_observation", "INCONCLUSIVE", metrics=dict({"complete": False, "registered_before_observation": True}, **o))
    assert verdict(s, *off, claim(observation_started_ms=1)) == SV.PROMISING              # started BEFORE registration
    assert verdict(s, *off, ev("prospective_observation", "INCONCLUSIVE", candidate_id="someone_else", metrics={"complete": False, "registered_before_observation": True})) == SV.PROMISING
    assert verdict(s, *off, ev("prospective_observation", "INCONCLUSIVE", spec_digest=H, metrics={"complete": False, "registered_before_observation": True})) == SV.PROMISING
    assert evaluate(s, off + [claim()], registry=None).verdict == SV.INSUFFICIENT_DATA    # no registry: nothing is 'registered'
    assert evaluate(s, off + [claim()], registry={"trials": []}).verdict == SV.INSUFFICIENT_DATA


def test_a_not_counted_pass_leaves_the_candidate_where_it_was():
    bad = ev("historical_test", metrics={"n_variants_tested": 0})
    r = evaluate(spec(), [bad])
    assert r.verdict == SV.INSUFFICIENT_DATA and r.stages["historical_test"].startswith("NOT_COUNTED")


# ---------------------------------------------------------------- the frozen BTC experiment is described, never promoted by a return

def test_the_frozen_btc_candidate_is_valid_untested_and_its_plan_is_untouched():
    import carry_prospective as CP
    c = SV.btc_carry_candidate()
    assert SV.spec_problems(c) == ["BIAS_NOT_RULED_OUT:fills_modelled_against_real_depth", "BIAS_NOT_RULED_OUT:hedge_completeness_modelled"]      # truthful: depth and basis/margin data are absent
    assert c["OOS_protocol"]["plan_digests"] == {f"{h:g}": CP.PINNED_DIGESTS[h] for h in CP.HURDLES_PCT}
    assert evaluate(c, []).verdict == SV.UNTESTED
    r = evaluate(c, [ev("historical_test", metrics={"net_edge_bps_after_costs": 50.0})])
    assert r.verdict == SV.UNTESTED and r.spec_problems                                                      # not testable until the depth and basis/margin data exist: no return promotes it
    with pytest.raises(SV.SpecError):
        SV.register_candidate({"trials": []}, c, code_sha="0" * 40, input_data_sha256={"none_yet": "0" * 64})


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
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == SV.INSUFFICIENT_DATA and any("NO_VERIFYING_REGISTRY" in r for r in out["reasons"])      # --evidence-only can never exceed INSUFFICIENT_DATA
    assert SV.main(["--candidate", str(tmp_path / "nope.json")]) == 2
    evf.write_text('{"stage": "nonsense", "result": "PASS"}\n')
    assert SV.main(["--candidate", str(cand), "--evidence", str(evf)]) == 2


def test_a_bias_declaration_cannot_out_claim_data_the_spec_says_is_missing():
    spec = json.loads(json.dumps(SV.btc_carry_candidate()))
    spec["required_data"][0]["repo_has"] = False
    spec["required_data"][0]["declaration_keys"] = ["point_in_time_universe"]
    assert not any(p.startswith("BIAS_DECLARED_WITHOUT_DATA") for p in SV.spec_problems(json.loads(json.dumps(SV.btc_carry_candidate()))))
    assert any(p.startswith("BIAS_DECLARED_WITHOUT_DATA:point_in_time_universe") for p in SV.spec_problems(spec))


# ---------------------------------------------------------------- red-team 3: the top rungs are not reachable from keyless, recomputable evidence

def _full(s_=None, **over):
    s_ = s_ if s_ is not None else spec()
    return [ev(x) for x in OFFLINE] + [ev("prospective_observation"), ev("execution_validation"), ev("reconciled_owner_result")]


def test_rt3_1_the_top_rungs_need_an_owner_attestation_under_a_key_the_repo_never_holds():
    full = _full()
    assert evaluate(spec(), full).verdict == SV.OWNER_PROFIT_EVIDENCED                                   # positive control: key present, evidence attested, registry anchored
    r = evaluate(spec(), full, key_provider=lambda: None)                                                 # key unset: nothing can verify
    assert r.verdict in (SV.PROMISING, SV.PROSPECTIVE) and any("OWNER_ATTESTATION_UNVERIFIABLE" in x for x in r.reasons)
    assert evaluate(spec(), full, key_provider=lambda: "a-different-owner-key-0123456789").verdict in (SV.PROMISING, SV.PROSPECTIVE)   # wrong key
    assert evaluate(spec(), full, key_provider=lambda: "short").verdict in (SV.PROMISING, SV.PROSPECTIVE)                                # a trivially short key attests nothing
    def boom():
        raise RuntimeError("secret store down")
    assert evaluate(spec(), full, key_provider=boom).verdict in (SV.PROMISING, SV.PROSPECTIVE)             # a failing provider is an absent key


def test_rt3_1_every_observed_stage_must_be_attested_and_the_attestation_binds_the_content():
    base = [ev(x) for x in OFFLINE]
    for stage in SV.OBSERVED_STAGES:
        plain = ev(stage, attest=False) if stage != "reconciled_owner_result" else owner_item(attest=False)
        ok, why = counted(plain, spec(), REG)
        assert ok is False and why.startswith("OWNER_ATTESTATION_UNVERIFIABLE"), stage
        assert counted(ev(stage) if stage != "reconciled_owner_result" else owner_item(), spec(), REG) == (True, None), stage       # positive control
    obs = ev("prospective_observation")
    edited = SV.EvidenceItem(**dict(SV.evidence_to_dict(obs), metrics=dict(obs.metrics, coverage=1.0)))                     # signed, then the content changed
    assert counted(edited, spec(), REG)[1].startswith("OWNER_ATTESTATION_UNVERIFIABLE")
    other = SV.attest_item(dataclasses_replace(obs, candidate_id="cand1"), KEY)
    other = dataclasses_replace(other, attestation=SV.owner_attest(SV.evidence_digest(obs), "someone_else", SV.spec_digest(spec()), KEY))
    assert counted(other, spec(), REG)[1].startswith("OWNER_ATTESTATION_UNVERIFIABLE")                                      # signed for another candidate
    assert evaluate(spec(), base + [obs, ev("execution_validation")]).verdict == SV.QUALIFIED


def dataclasses_replace(item, **kw):
    import dataclasses
    return dataclasses.replace(item, **kw)


def test_rt3_1_the_owner_report_needs_its_own_attestation_and_a_forged_report_is_not_enough():
    base = [ev(x) for x in OFFLINE] + [ev("prospective_observation"), ev("execution_validation")]
    it = owner_item()
    no_rep = dataclasses_replace(it, report_attestation=None)
    assert counted(no_rep, spec(), REG)[1].startswith("OWNER_ATTESTATION_UNVERIFIABLE")
    assert evaluate(spec(), base + [no_rep]).verdict == SV.QUALIFIED                                                       # capped one rung down, said out loud
    # poc1: forged economic total, correctly resealed (keyless digests all verify) and the item built honestly from it: still not attested
    f = import_report(); f["layers"]["economic_total"]["net_economic_pnl_usd"] = 1e9
    forged = owner_item(_reseal(f), attest=False)
    assert SV._verify_owner_report(_reseal(f)) == [] and counted(forged, spec(), REG)[1].startswith("OWNER_ATTESTATION_UNVERIFIABLE")
    assert evaluate(spec(), base + [forged], key_provider=lambda: None).verdict in (SV.PROMISING, SV.PROSPECTIVE)


def test_rt3_1_owner_attest_is_a_constant_time_hmac_and_the_key_is_never_stored():
    d = SV.evidence_digest(ev("execution_validation"))
    a = SV.owner_attest(d, "cand1", SV.spec_digest(spec()), KEY)
    import hashlib, hmac as _h
    msg = json.dumps(["owner-attest/1", d, "cand1", SV.spec_digest(spec())], separators=(",", ":")).encode()
    assert a == _h.new(KEY.encode(), msg, hashlib.sha256).hexdigest()
    assert a != SV.owner_attest(d, "cand2", SV.spec_digest(spec()), KEY) and a != SV.owner_attest(d, "cand1", H, KEY) and a != SV.owner_attest(H, "cand1", SV.spec_digest(spec()), KEY)
    with pytest.raises(SV.SpecError):
        SV.owner_attest(d, "cand1", H, "short")
    blob = json.dumps(SV.evidence_to_dict(ev("execution_validation")))
    assert KEY not in blob                                                                                                 # the item carries the HMAC output, never the key
    import inspect
    assert "compare_digest" in inspect.getsource(SV._check_attestation)
    reg = copy.deepcopy(REG)
    SV.record_evidence(reg, "cand1", ev("execution_validation"))
    assert KEY not in json.dumps(reg)


def test_rt3_1_the_key_is_read_from_the_environment_by_default(monkeypatch):
    full = _full()
    monkeypatch.delenv(SV.OWNER_ATTESTATION_KEY_ENV, raising=False)
    assert SV.evaluate(spec(), full, registry=REG, now_ms=NOW_MS, anchor=anchor_of(REG)).verdict in (SV.PROMISING, SV.PROSPECTIVE)       # unset => unverifiable
    monkeypatch.setenv(SV.OWNER_ATTESTATION_KEY_ENV, KEY)
    assert SV.evaluate(spec(), full, registry=REG, now_ms=NOW_MS, anchor=anchor_of(REG)).verdict == SV.OWNER_PROFIT_EVIDENCED


def test_rt3_1_the_attest_cli_signs_observed_evidence_from_the_environment_key(tmp_path, monkeypatch, capsys):
    cand = tmp_path / "c.json"; cand.write_text(json.dumps(spec()))
    evf = tmp_path / "e.jsonl"; out = tmp_path / "o.jsonl"
    plain = ev("prospective_observation", attest=False)
    evf.write_text(json.dumps(SV.evidence_to_dict(plain)) + "\n")
    monkeypatch.delenv(SV.OWNER_ATTESTATION_KEY_ENV, raising=False)
    assert SV.main(["attest", "--candidate", str(cand), "--evidence", str(evf), "--out", str(out)]) == 2          # no key: nothing signed
    monkeypatch.setenv(SV.OWNER_ATTESTATION_KEY_ENV, KEY)
    assert SV.main(["attest", "--candidate", str(cand), "--evidence", str(evf), "--out", str(out)]) == 0
    signed = SV.evidence_from_dict(json.loads(out.read_text()))
    assert counted(signed, spec(), REG) == (True, None) and KEY not in out.read_text() + capsys.readouterr().out
    evf.write_text(json.dumps(dict(SV.evidence_to_dict(plain), candidate_id="someone_else")) + "\n")
    assert SV.main(["attest", "--candidate", str(cand), "--evidence", str(evf)]) == 2                             # refuses to sign evidence about another candidate


def test_rt3_2_the_registry_must_be_anchored_for_qualified_and_the_anchor_detects_delete_and_reseal():
    import hypothesis_registry as hr
    full = _full()
    r = evaluate(spec(), full, anchor=None)
    assert r.verdict == SV.PROSPECTIVE and any("REGISTRY_NOT_ANCHORED" in x for x in r.reasons)
    assert evaluate(spec(), full).verdict == SV.OWNER_PROFIT_EVIDENCED                                    # anchored: the honest path
    # poc8: delete a recorded FAIL, reseal (keyless), and the registry verifies again
    reg = copy.deepcopy(REG)
    for x in OFFLINE:
        SV.record_evidence(reg, "cand1", ev(x))
    SV.record_evidence(reg, "cand1", ev("oos_test", "FAIL", metrics={}))
    hr.seal(reg)
    anchor = hr.anchor_digest(reg)
    assert hr.verify(reg, anchor=anchor) == [] and SV.evaluate_registered(reg, spec(), now_ms=NOW_MS, anchor=anchor, key_provider=KEYP).verdict == SV.REJECTED
    reg["trials"][0]["evidence"] = [e for e in reg["trials"][0]["evidence"] if not e["result"].endswith("FAIL")]
    hr.seal(reg)
    assert hr.verify(reg) == []                                                                         # the keyless seal is fooled...
    assert any("anchor" in p for p in hr.verify(reg, anchor=anchor))                                    # ...the owner-held anchor is not
    with pytest.raises(SV.SpecError, match="anchor"):
        SV.evaluate_registered(reg, spec(), now_ms=NOW_MS, anchor=anchor, key_provider=KEYP)
    with pytest.raises(SV.SpecError, match="anchor"):
        SV.evaluate(spec(), [], registry=reg, now_ms=NOW_MS, anchor=anchor)
    with pytest.raises(SV.SpecError):
        SV.evaluate(spec(), [], registry=None, anchor=anchor)
    # without any anchor the resealed registry is evaluated but can never reach QUALIFIED
    full_reg = copy.deepcopy(REG)
    for it in _full():
        SV.record_evidence(full_reg, "cand1", it)
    hr.seal(full_reg)
    un = SV.evaluate_registered(full_reg, spec(), now_ms=NOW_MS, key_provider=KEYP)
    assert un.verdict in (SV.PROSPECTIVE, SV.PROMISING) and any("REGISTRY_NOT_ANCHORED" in x for x in un.reasons) or un.verdict != SV.QUALIFIED


def test_rt3_2_the_registry_anchor_is_recomputed_from_the_trials_not_read_from_the_seal():
    import hypothesis_registry as hr
    reg = copy.deepcopy(REG)
    a = hr.anchor_digest(reg)
    assert hr.anchor_digest(copy.deepcopy(reg)) == a and len(a) == 64
    SV.record_evidence(reg, "cand1", ev("oos_test"))
    assert hr.anchor_digest(reg) != a                                                                   # appending changes the head: the owner re-anchors deliberately
    reg2 = copy.deepcopy(REG); reg2["trials"][0]["costs"] = "cheaper"; hr.seal(reg2)
    assert hr.verify(reg2) == [] and hr.verify(reg2, anchor=a)                                          # alteration + reseal detected
    reg3 = copy.deepcopy(REG); reg3["trials"] = []; hr.seal(reg3)
    assert hr.verify(reg3, anchor=a)                                                                    # whole-trial deletion + reseal detected
    assert hr.verify(copy.deepcopy(REG), anchor="zz")                                                   # a malformed anchor never passes


@pytest.mark.parametrize("pp,why", [
    ({"description": "p", "min_duration_ms": 1}, None),
    ({"description": "p", "min_duration_ms": 1000}, None),
])
def test_rt3_3_a_registered_min_duration_cannot_lower_the_thirty_day_floor(pp, why):
    s_ = spec(prospective_protocol=pp)
    reg = _registry(s_)
    short = ev("prospective_observation", spec_=s_, metrics={"observation_ended_ms": FUTURE_MS + 400, "observed_settlements": 30}, recorded_ms=FUTURE_MS + 400)
    ok, reason = SV.counted(short, s_, reg, now_ms=NOW_MS, key_provider=KEYP)
    assert ok is False and "PROSPECTIVE_DURATION_SHORTER" in reason                                      # poc2: 30 settlements in 0.4 s
    assert SV._prospective_floors({"prospective_period": pp})["min_duration_ms"] == 30 * DAY
    assert SV.counted(ev("prospective_observation", spec_=s_), s_, reg, now_ms=NOW_MS, key_provider=KEYP) == (True, None)
    # a protocol may RAISE the floor
    assert SV._prospective_floors({"prospective_period": dict(pp, min_duration_ms=90 * DAY)})["min_duration_ms"] == 90 * DAY
    assert SV._prospective_floors({"prospective_period": f"{FUTURE_MS}..{FUTURE_MS + DAY}"})["min_duration_ms"] == 30 * DAY


def test_rt3_3_observed_settlements_must_fit_in_the_observation_window():
    for n, ok in ((90, True), (91, True), (100, False), (10_000, False)):
        item = ev("prospective_observation", metrics={"observed_settlements": n})
        got, why = counted(item, spec(), REG)
        assert got is ok, (n, why)
        assert ok or why == "OBSERVED_SETTLEMENTS_CANNOT_FIT_IN_THE_OBSERVATION_WINDOW"
    # an 8h interval is the default; a protocol that registers a 1h interval legitimately fits 720 in 30 days
    s_ = spec(prospective_protocol={"description": "p", "min_duration_ms": 30 * DAY, "funding_interval_ms": 3_600_000})
    reg = _registry(s_)
    assert SV.counted(ev("prospective_observation", spec_=s_, metrics={"observed_settlements": 700}), s_, reg, now_ms=NOW_MS, key_provider=KEYP) == (True, None)


def test_rt3_3_evidence_cannot_be_recorded_before_the_observation_it_claims_ended():
    early = ev("prospective_observation", recorded_ms=FUTURE_MS + 30 * DAY - 1)
    assert counted(early, spec(), REG)[1] == "EVIDENCE_RECORDED_BEFORE_THE_OBSERVATION_IT_CLAIMS_ENDED"
    assert counted(ev("prospective_observation", recorded_ms=0), spec(), REG)[1] == "EVIDENCE_RECORDED_BEFORE_THE_OBSERVATION_IT_CLAIMS_ENDED"
    assert counted(ev("prospective_observation"), spec(), REG) == (True, None)
    # through the registry, recorded_ms is the registry's own timestamp: poc2 (recorded 'now', observation 'ending' in 2096) cannot count
    import hypothesis_registry as hr
    reg = copy.deepcopy(REG)
    SV.record_evidence(reg, "cand1", ev("prospective_observation"))
    hr.seal(reg)
    items = SV.evidence_from_registry(reg, "cand1")
    assert 0 < items[0].recorded_ms < FUTURE_MS
    assert SV.counted(items[0], spec(), reg, now_ms=NOW_MS, key_provider=KEYP)[1] == "EVIDENCE_RECORDED_BEFORE_THE_OBSERVATION_IT_CLAIMS_ENDED"


def test_rt3_3_an_observation_cannot_start_in_the_future():
    for stage in ("execution_validation", "prospective_observation"):
        item = ev(stage, metrics={"observation_started_ms": NOW_MS + DAY, "observation_ended_ms": NOW_MS + 31 * DAY})
        ok, why = counted(item, spec(), REG)
        assert ok is False and why in ("OBSERVATION_STARTS_IN_THE_FUTURE", "OBSERVATION_END_INVALID_OR_IN_THE_FUTURE"), stage


def test_rt3_4_cost_pass_must_carry_the_chain_digest_recomputed_from_the_spec():
    good = ev("cost_model")
    assert counted(good, spec(), REG) == (True, None)
    for bad in (dict(good.metrics, chain_digest=H), {k: v for k, v in good.metrics.items() if k != "chain_digest"}, dict(good.metrics, chain_digest=None)):
        item = SV.EvidenceItem(**dict(SV.evidence_to_dict(good), metrics=bad))
        assert counted(item, spec(), REG)[1] == "COST_CHAIN_DIGEST_NOT_RECOMPUTED_FROM_THE_SPEC"
    # the digest is of THIS spec's cost labels: cost evidence for different fees does not carry over
    other = spec(fee_model={"maker_bps": lab(1.0), "taker_bps": lab(2.0)})
    assert counted(SV.EvidenceItem(**dict(SV.evidence_to_dict(good), spec_digest=SV.spec_digest(other))), other, REG)[1] == "COST_CHAIN_DIGEST_NOT_RECOMPUTED_FROM_THE_SPEC"
    import economic_model as EM
    assert SV.cost_chain_digest(spec()) == EM.cost_chain_digest(spec())
    cap = ev("capacity_test")
    assert counted(SV.EvidenceItem(**dict(SV.evidence_to_dict(cap), metrics=dict(cap.metrics, chain_digest=H))), spec(), REG)[1] == "COST_CHAIN_DIGEST_NOT_RECOMPUTED_FROM_THE_SPEC"


def test_rt3_4_assumed_cost_inputs_never_pass_whatever_the_metric_says():
    allass = spec(funding_model={"expected_bps_per_settlement": lab(0.3, "assumed")})
    item = ev("cost_model", spec_=allass)
    assert counted(item, allass, REG)[1] == "ALL_COST_INPUTS_ASSUMED"
    assert counted(ev("cost_model"), spec(), REG) == (True, None)                                          # positive control: funding is modelled, not assumed
    named = ev("cost_model", metrics={"assumed_names": ["fees.taker"]})
    assert counted(named, spec(), REG)[1] == "ASSUMED_COST_INPUTS_CANNOT_PASS"
    assert evaluate(allass, [ev(x, spec_=allass) for x in OFFLINE], registry=_registry(allass)).verdict == SV.INSUFFICIENT_DATA


def test_rt3_4_capacity_pass_is_tied_to_the_specs_minimum():
    assert counted(ev("capacity_test"), spec(), REG) == (True, None)
    for bad in ({"min_viable_capacity_usd": 1.0}, {"min_viable_capacity_usd": None}):
        assert counted(ev("capacity_test", metrics=bad), spec(), REG)[1] == "CAPACITY_EVIDENCE_NOT_TIED_TO_THE_SPECS_MINIMUM"


def test_rt3_5_promising_needs_a_verifying_registry_that_holds_the_candidate():
    off = [ev(x) for x in OFFLINE]
    assert evaluate(spec(), off).verdict == SV.PROMISING                                                    # positive control
    for reg in (None, {"trials": []}, _registry(spec(candidate_id="someone_else"))):
        r = evaluate(spec(), off, registry=reg, anchor=None)
        assert r.verdict == SV.INSUFFICIENT_DATA and any("NO_VERIFYING_REGISTRY" in x for x in r.reasons), reg
    import hypothesis_registry as hr
    tampered = copy.deepcopy(REG); tampered["trials"][0]["costs"] = "cheaper"
    assert hr.verify(tampered) and evaluate(spec(), off, registry=tampered, anchor=None).verdict == SV.INSUFFICIENT_DATA


def test_rt3_7_a_pending_prospective_needs_a_started_attested_running_marker():
    off = [ev(x) for x in OFFLINE]
    pend = lambda **kw: ev("prospective_observation", "INCONCLUSIVE", **dict(kw, metrics=dict({"complete": False}, **kw.get("metrics", {}))))
    assert evaluate(spec(), off + [pend()]).verdict == SV.PROSPECTIVE                                       # positive control
    assert evaluate(spec(), off + [pend(attest=False)]).verdict == SV.PROMISING                             # unattested marker: not a verified running observation
    assert evaluate(spec(), off + [pend()], key_provider=lambda: None).verdict == SV.PROMISING              # no key: cannot verify
    future = pend(metrics={"observation_started_ms": NOW_MS + DAY})
    assert evaluate(spec(), off + [future]).verdict == SV.PROMISING                                         # has not started yet
