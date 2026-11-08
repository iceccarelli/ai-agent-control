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
            assert e["provenance"] in ("assumed", "derived", "measured", "venue_stated") and e["value"] > 0, (name, comp, k, e)
    assert len(s["rejection_conditions"]) >= 4
    assert "who pays" not in s["counterparty"] and len(s["counterparty"]) > 15 and "Counterparty" in s["economic_mechanism"]
    assert "SPECIFICATION ONLY" in s["status_note"]


def test_no_template_claims_to_have_ruled_out_a_bias_its_own_required_data_says_it_cannot():
    for name in MECHANISMS:
        s = load(name)
        assert declared_without_data(s) == [], name
        assert missing_declarations(s), f"{name}: needs data the repo lacks, so at least one declaration must be False"
    # the point-in-time universe is only declared where the universe data exists: none of the shipped candidates has it
    assert load("cross_sectional_funding")["bias_declarations"]["point_in_time_universe"] is False
    assert load("market_neutral_relative_value")["bias_declarations"]["point_in_time_universe"] is False
    assert load("cash_and_carry")["bias_declarations"]["fills_modelled_against_real_depth"] is False       # its own L2 depth item says repo_has False


@pytest.mark.parametrize("name", MECHANISMS)
def test_hedge_declaration_true_only_if_a_hedge_data_item_exists_and_is_whole(name):
    """Round 3: cash_and_carry declared hedge completeness while its own data row says the basis is partial; three others declared it with no hedge row at all."""
    s = load(name)
    if s["bias_declarations"]["hedge_completeness_modelled"] is True:
        rows = [d for d in s["required_data"] if "hedge_completeness_modelled" in d["declaration_keys"]]
        assert rows and all(d["repo_has"] is True and "partial" not in d["gap"].lower() for d in rows), name
    else:
        assert any("hedge_completeness_modelled" in d["declaration_keys"] and d["repo_has"] is False for d in s["required_data"]), name


@pytest.mark.parametrize("name", MECHANISMS)
def test_every_missing_data_row_has_a_real_gap_description(name):
    for d in load(name)["required_data"]:
        if d["repo_has"] is False:
            assert d["gap"].strip().lower() not in ("", "none", "assumed", "n/a") and len(d["gap"]) >= 10, (name, d)


def test_in_sample_mean_is_not_labelled_measured_as_a_forecast():
    f = load("cash_and_carry")["funding_model"]["expected_bps_per_settlement"]
    assert f["provenance"] == "derived" and "forecast" in f["note"].lower()
    for name in MECHANISMS:
        for e in load(name)["funding_model"].values():
            assert not (e["provenance"] == "measured" and "note" not in e), (name, e)


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
def test_prospective_protocol_is_machine_readable_and_claims_no_start(name):
    pp = load(name)["prospective_protocol"]
    assert isinstance(pp, dict) and pp["description"] and pp["start_ms"] is None                 # a template cannot have started observing
    assert isinstance(pp["min_duration_ms"], int) and pp["min_duration_ms"] >= 14 * 86_400_000
    assert pp["min_settlements"] >= SV.MIN_PROSPECTIVE_SETTLEMENTS and pp["min_coverage"] >= SV.MIN_PROSPECTIVE_COVERAGE
    assert SV._prospective_floors({"prospective_period": pp})["min_duration_ms"] == pp["min_duration_ms"]       # the verdict layer reads it


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


def test_rt4_cross_venue_funding_history_is_not_claimed_as_in_the_repo():
    s = load("cross_venue_funding")
    row = next(d for d in s["required_data"] if d["name"].startswith("funding history for the same symbol on two venues"))
    assert row["repo_has"] is False and "interval" in row["gap"] and "transfer" in row["gap"]       # its own gap names what is missing
    assert s["bias_declarations"]["no_future_funding_knowledge"] is False                           # declaration consistent with the missing data
    assert "no_future_funding_knowledge" in missing_declarations(s)


# ---- provenance labels follow the published fee schedule; funding-interval heterogeneity is declared (the interval facts are OWNER-OBSERVED)
def test_venue_stated_is_used_only_for_numbers_the_repos_published_schedule_states():
    import carry_costs as CC
    saw = 0
    for name in MECHANISMS:
        s = load(name)
        for comp in ("fee_model", "slippage_model", "funding_model"):
            for k, e in s[comp].items():
                if e["provenance"] == "venue_stated":
                    saw += 1
                    assert (comp, k) == ("fee_model", "taker_bps") and e["value"] == CC.TAKER_BPS_PERP and "BASE-tier" in e["note"], (name, comp, k, e)
    assert saw >= 6
    assert load("spot_perp_basis")["fee_model"]["taker_bps"] == {"provenance": "assumed", "value": 6.0}       # 6.0 is in no published schedule here: stays assumed
    for name in MECHANISMS:                                                                                    # nothing was upgraded to measured/modelled without a data basis
        for comp in ("fee_model", "slippage_model"):
            assert all(e["provenance"] in ("assumed", "venue_stated") for e in load(name)[comp].values()), name


def test_cross_sectional_says_plainly_that_an_8h_only_filter_drops_most_high_funding_perps():
    s = load("cross_sectional_funding")
    txt = json.dumps(s)
    assert "8h funding interval only" not in txt and "OWNER-OBSERVED" in s["economic_mechanism"] and "8h-only filter would exclude the majority" in s["economic_mechanism"]
    h = s["funding_interval_handling"]
    assert "1440 / fundingInterval_min" in h["method"] and "per-interval cohorts" in h["method"] and "deliveryTime" in h["method"] and "cannot pass" in h["unknown_interval"]
    assert any("fundingInterval" in r and "never on raw per-print" in r for r in s["eligibility_rules"])
    for name in ("market_neutral_relative_value", "cross_venue_funding", "cash_and_carry", "spot_perp_basis"):
        assert "funding_interval_handling" in load(name), name
