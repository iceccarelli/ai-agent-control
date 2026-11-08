"""Every research candidate template is a valid SPECIFICATION and is UNTESTED; a spec with a hole is refused."""
from __future__ import annotations

import copy
import glob
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import research_harness as RH  # noqa: E402
import strategy_verdict as SV  # noqa: E402

CAND = os.path.join(BOT, "research", "candidates")
MECHANISMS = ("cross_sectional_funding", "spot_perp_basis", "cross_venue_funding", "cash_and_carry", "market_neutral_relative_value", "spread_microstructure",
              "passive_execution", "liquidity_imbalance")


def load(name):
    with open(os.path.join(CAND, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def declared_without_data(spec):
    """The consistency rule: a bias declaration may be True only if no required_data item that is needed to rule that bias out is missing from the repo.
    Each required_data item names the declarations it backs in ``declaration_keys``. (Proposed home: ``strategy_verdict.spec_problems``.)"""
    bias = spec.get("bias_declarations") or {}
    out = []
    for i, d in enumerate(spec.get("required_data") or []):
        if d.get("repo_has") is False:
            for k in d.get("declaration_keys") or []:
                if bias.get(k) is True:
                    out.append(f"BIAS_DECLARED_WITHOUT_DATA:{k}:REQUIRED_DATA[{i}]")
    return out


def missing_declarations(spec):
    return sorted({k for d in spec["required_data"] if d["repo_has"] is False for k in d["declaration_keys"]})


def test_exactly_the_eight_mechanisms_exist():
    have = sorted(os.path.basename(p)[:-5] for p in glob.glob(os.path.join(CAND, "*.json")))
    assert have == sorted(MECHANISMS), have


@pytest.mark.parametrize("name", MECHANISMS)
def test_template_is_a_specification_whose_declarations_are_truthful(name):
    """The repo lacks data the candidate needs to rule out some biases: those declarations are NOT True, so the spec is not yet testable (UNTESTED)."""
    s = load(name)
    missing = missing_declarations(s)
    assert sorted(SV.spec_problems(s)) == sorted(f"BIAS_NOT_RULED_OUT:{k}" for k in missing)
    for k in SV.BIAS_DECLARATIONS:
        assert s["bias_declarations"].get(k) is (k not in missing), (name, k)           # False exactly where the missing data is needed, True elsewhere
    assert declared_without_data(s) == []
    v = SV.evaluate(s, [])
    assert v.verdict == SV.UNTESTED, (name, v.as_dict())
    assert v.next_stage == ("hypothesis" if missing else "historical_test"), (name, v.as_dict())
    assert s["candidate_id"].startswith(name + "@")
    for comp in ("fee_model", "slippage_model", "funding_model"):
        for k, e in s[comp].items():
            assert e["provenance"] in ("assumed", "measured") and e["value"] > 0, (name, comp, k, e)
    assert len(s["rejection_conditions"]) >= 4
    assert "who pays" not in s["counterparty"] and len(s["counterparty"]) > 15 and "Counterparty" in s["economic_mechanism"]
    assert "SPECIFICATION ONLY" in s["status_note"]


def test_no_template_claims_to_have_ruled_out_a_bias_its_own_required_data_says_it_cannot():
    for name in MECHANISMS:
        s = load(name)
        assert declared_without_data(s) == [], name
        if name != "cash_and_carry":
            assert missing_declarations(s), f"{name}: needs data the repo lacks, so at least one declaration must be False"
    # the point-in-time universe is only declared where the universe data exists: none of the shipped candidates has it
    assert load("cross_sectional_funding")["bias_declarations"]["point_in_time_universe"] is False
    assert load("market_neutral_relative_value")["bias_declarations"]["point_in_time_universe"] is False
    assert load("cash_and_carry")["bias_declarations"]["fills_modelled_against_real_depth"] is False       # its own L2 depth item says repo_has False


@pytest.mark.parametrize("name", MECHANISMS)
def test_true_declaration_over_missing_data_is_rejected(name):
    s = load(name)
    for i, d in enumerate(s["required_data"]):
        for k in d["declaration_keys"]:
            if d["repo_has"] is False:
                bad = copy.deepcopy(s)
                bad["bias_declarations"][k] = True                                       # claim the bias is ruled out although the data is missing
                assert f"BIAS_DECLARED_WITHOUT_DATA:{k}:REQUIRED_DATA[{i}]" in declared_without_data(bad), (name, i, k)
            else:
                ok = copy.deepcopy(s)
                ok["bias_declarations"][k] = True                                        # control: data present -> a True declaration is consistent
                assert not any(x.endswith(f"REQUIRED_DATA[{i}]") for x in declared_without_data(ok)), (name, i, k)
    # control: once the data exists (repo_has True everywhere) all-True declarations are consistent AND valid
    healed = copy.deepcopy(s)
    for d in healed["required_data"]:
        d["repo_has"] = True
    healed["bias_declarations"] = {k: True for k in SV.BIAS_DECLARATIONS}
    assert declared_without_data(healed) == [] and SV.spec_problems(healed) == []


@pytest.mark.parametrize("name", MECHANISMS)
def test_template_states_what_data_the_repo_lacks(name):
    s = load(name)
    for d in s["required_data"]:
        assert d["point_in_time"] is True and isinstance(d["repo_has"], bool) and d["gap"], (name, d)
        assert isinstance(d["declaration_keys"], list) and set(d["declaration_keys"]) <= set(SV.BIAS_DECLARATIONS), (name, d)
    if name != "cash_and_carry":
        assert any(d["repo_has"] is False for d in s["required_data"]), f"{name}: claims the repo has everything"


@pytest.mark.parametrize("name", MECHANISMS)
def test_template_is_registrable_only_once_its_data_exists(name):
    import hypothesis_registry as hr
    s = load(name)
    if missing_declarations(s):
        with pytest.raises(SV.SpecError):
            SV.register_candidate({}, s, code_sha="0" * 40, input_data_sha256={"none_yet": "0" * 64})       # honest declarations: not registrable yet
    healed = copy.deepcopy(s)
    for d in healed["required_data"]:
        d["repo_has"] = True
    healed["bias_declarations"] = {k: True for k in SV.BIAS_DECLARATIONS}
    reg: dict = {}
    SV.register_candidate(reg, healed, code_sha="0" * 40, input_data_sha256={"none_yet": "0" * 64})
    hr.seal(reg)
    assert hr.verify(reg) == [] and s["candidate_id"] in json.dumps(reg)


@pytest.mark.parametrize("field", SV.REQUIRED_SPEC_FIELDS)
def test_a_missing_field_is_refused(field):
    s = copy.deepcopy(load("cross_sectional_funding"))
    del s[field]
    assert f"MISSING:{field}" in SV.spec_problems(s)
    assert SV.evaluate(s, []).verdict == SV.UNTESTED


def test_zero_fee_unlabelled_cost_and_non_pit_data_are_refused():
    base = load("spot_perp_basis")
    z = copy.deepcopy(base); z["fee_model"]["taker_bps"] = {"value": 0.0, "provenance": "assumed"}
    assert "ZERO_COST_WITHOUT_MEASUREMENT:fee_model.taker_bps" in SV.spec_problems(z)
    u = copy.deepcopy(base); u["slippage_model"]["entry_bps"] = {"value": 3.0}
    assert "COST_INPUT_UNLABELLED:slippage_model.entry_bps" in SV.spec_problems(u)
    n = copy.deepcopy(base); n["required_data"][0]["point_in_time"] = False
    assert "REQUIRED_DATA[0]_NOT_DECLARED_POINT_IN_TIME" in SV.spec_problems(n)
    b = copy.deepcopy(base); b["bias_declarations"]["no_look_ahead"] = False
    assert "BIAS_NOT_RULED_OUT:no_look_ahead" in SV.spec_problems(b)
    for bad in (z, u, n, b):
        assert SV.evaluate(bad, []).verdict == SV.UNTESTED
    # the harness refuses the same zero at the cost layer
    with pytest.raises(RH.CostError):
        RH.net_edge_bps(10.0, {"fee_bps": {"value": 0.0, "provenance": "assumed"}, "slippage_bps": {"value": 1.0, "provenance": "assumed"}})
