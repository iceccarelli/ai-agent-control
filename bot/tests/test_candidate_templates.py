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


def test_exactly_the_eight_mechanisms_exist():
    have = sorted(os.path.basename(p)[:-5] for p in glob.glob(os.path.join(CAND, "*.json")))
    assert have == sorted(MECHANISMS), have


@pytest.mark.parametrize("name", MECHANISMS)
def test_template_is_valid_and_untested(name):
    s = load(name)
    assert SV.spec_problems(s) == []
    v = SV.evaluate(s, [])
    assert v.verdict == SV.UNTESTED and v.next_stage == "historical_test", (name, v.as_dict())
    assert s["candidate_id"].startswith(name + "@")
    for comp in ("fee_model", "slippage_model", "funding_model"):
        for k, e in s[comp].items():
            assert e["provenance"] in ("assumed", "measured") and e["value"] > 0, (name, comp, k, e)
    assert all(s["bias_declarations"][k] is True for k in SV.BIAS_DECLARATIONS)
    assert len(s["rejection_conditions"]) >= 4
    assert "who pays" not in s["counterparty"] and len(s["counterparty"]) > 15 and "Counterparty" in s["economic_mechanism"]
    assert "SPECIFICATION ONLY" in s["status_note"]


@pytest.mark.parametrize("name", MECHANISMS)
def test_template_states_what_data_the_repo_lacks(name):
    s = load(name)
    for d in s["required_data"]:
        assert d["point_in_time"] is True and isinstance(d["repo_has"], bool) and d["gap"], (name, d)
    if name != "cash_and_carry":
        assert any(d["repo_has"] is False for d in s["required_data"]), f"{name}: claims the repo has everything"


@pytest.mark.parametrize("name", MECHANISMS)
def test_template_is_registrable(name):
    import hypothesis_registry as hr
    s = load(name)
    reg: dict = {}
    SV.register_candidate(reg, s, code_sha="0" * 40, input_data_sha256={"none_yet": "0" * 64})
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
