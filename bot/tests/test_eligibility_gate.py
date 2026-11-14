"""Round 22: the strategy must EARN the right to trade. The eligibility gate is a conjunction of eight terms; UNKNOWN is never PASS; a Testnet measurement session has its own named outcome and can never
be read as eligibility to trade for profit."""
from __future__ import annotations

import copy
import itertools
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(HERE), os.path.join(os.path.dirname(HERE), "tools")]

import eligibility_gate as EG  # noqa: E402


def good_edge():
    return {"session_class": "PROFIT_CANDIDATE", "net_edge_bps": "12.5", "expected_net_usd": "1.04", "reasons": [],
            "costs_bps": {"fee_entry": "5.5", "fee_exit": "5.5", "spread": "1.0", "slippage": "0.5", "financing": "0.2"}, "assumed_inputs": [],
            "provenance": {"uncalibrated_modelled": [], "calibration": {"sessions": 31}}, "capital": {"state": "STATED", "committed_usd": "170.0"}}


def good_inputs():
    return {"data": {"venue_identity": True, "books_fresh": True, "funding_observed": True, "scope": "TESTNET"},
            "edge": good_edge(),
            "execution": {"model_vs_measured_status": "ENOUGH", "sessions_measured": 31, "mean_cost_surprise_bps": "0.4", "max_acceptable_surprise_bps": "1.0"},
            "evidence": {"status": "QUALIFIED", "candidate_id": "cash_and_carry"},
            "risk": {"loss_cap_usd": 5.55, "worst_case_loss_usd": 3.0, "capital_committed_usd": 170.0, "capital_available_usd": 10_000.0, "book_stops": []},
            "authority": {"capability": "VALID", "grant": "VALID", "credentials_present": True, "lease": "HELD"}}


def verdicts(res):
    return {t["term"]: t["verdict"] for t in res["terms"]}


def test_the_eight_terms_are_the_directives_and_all_must_pass_for_eligible():
    assert EG.TERMS == ("DATA_VALID", "COSTS_KNOWN", "CAPITAL_KNOWN", "EXECUTION_MODEL_VALID", "EDGE_POSITIVE_AFTER_COSTS", "STATISTICAL_EVIDENCE_SUFFICIENT", "RISK_LIMITS_ACCEPTABLE", "AUTHORITY_VALID")
    res = EG.evaluate(good_inputs())
    assert res["verdict"] == EG.ELIGIBLE and set(verdicts(res).values()) == {EG.PASS} and res["failed"] == [] and res["unknown"] == [] and res["waived"] == []
    assert EG.no_trade_reasons(res) == []


def test_today_nothing_is_qualified_or_calibrated_so_the_honest_answer_is_no_trade():
    """The repository's real state: no candidate is QUALIFIED, the execution model has no measured calibration, the cost basis has uncalibrated typed constants. The gate must say NO_TRADE and say why."""
    inp = good_inputs()
    inp["evidence"] = {"status": "UNTESTED"}
    inp["execution"] = {"model_vs_measured_status": "INSUFFICIENT_SAMPLE", "sessions_measured": 0}
    inp["edge"]["provenance"] = {"uncalibrated_modelled": ["hedge_error_bps", "basis_risk_bps"], "calibration": None}
    res = EG.evaluate(inp)
    assert res["verdict"] == EG.NO_TRADE
    assert {"STATISTICAL_EVIDENCE_SUFFICIENT", "EXECUTION_MODEL_VALID", "COSTS_KNOWN"} <= set(res["unknown"])
    assert any(r.startswith("STATISTICAL_EVIDENCE_SUFFICIENT:CANDIDATE_UNTESTED") for r in EG.no_trade_reasons(res, 8))


@pytest.mark.parametrize("term", EG.TERMS)
def test_removing_any_one_input_makes_that_term_unknown_and_the_whole_no_trade(term):
    """UNKNOWN is never PASS: drop the section a term reads and the conjunction is false."""
    section = {"DATA_VALID": "data", "COSTS_KNOWN": "edge", "CAPITAL_KNOWN": "edge", "EXECUTION_MODEL_VALID": "execution", "EDGE_POSITIVE_AFTER_COSTS": "edge", "STATISTICAL_EVIDENCE_SUFFICIENT": "evidence",
               "RISK_LIMITS_ACCEPTABLE": "risk", "AUTHORITY_VALID": "authority"}[term]
    inp = good_inputs()
    del inp[section]
    res = EG.evaluate(inp)
    assert res["verdict"] == EG.NO_TRADE and verdicts(res)[term] in (EG.UNKNOWN, EG.FAIL), verdicts(res)
    assert verdicts(res)[term] != EG.PASS


@pytest.mark.parametrize("garbage", [None, "x", 7, [], {"edge": "nope", "risk": 3, "data": [], "authority": "VALID"}])
def test_malformed_inputs_never_raise_and_never_pass(garbage):
    res = EG.evaluate(garbage)                                                    # type: ignore[arg-type]
    assert res["verdict"] == EG.NO_TRADE and EG.PASS not in verdicts(res).values()


def test_an_unknown_cost_line_is_unknown_not_zero_and_blocks_trading():
    inp = good_inputs()
    inp["edge"]["costs_bps"]["slippage"] = None
    res = EG.evaluate(inp)
    t = next(x for x in res["terms"] if x["term"] == "COSTS_KNOWN")
    assert t["verdict"] == EG.UNKNOWN and "COST_UNKNOWN:slippage" in t["reasons"] and res["verdict"] == EG.NO_TRADE


def test_an_assumed_input_is_not_a_known_cost_for_a_profit_session():
    inp = good_inputs()
    inp["edge"]["assumed_inputs"] = ["fee_tier"]
    t = next(x for x in EG.evaluate(inp)["terms"] if x["term"] == "COSTS_KNOWN")
    assert t["verdict"] == EG.UNKNOWN and t["reasons"][0].startswith("COST_BASIS_NOT_MEASURED")


def test_the_cost_of_capital_must_be_stated_for_a_profit_session():
    for state in ("UNSTATED", "STALE", "INVALID", "FUTURE_DATED"):
        inp = good_inputs()
        inp["edge"]["capital"]["state"] = state
        t = next(x for x in EG.evaluate(inp)["terms"] if x["term"] == "CAPITAL_KNOWN")
        assert t["verdict"] == EG.UNKNOWN, state
    inp = good_inputs()
    inp["edge"]["capital"]["committed_usd"] = None
    assert next(x for x in EG.evaluate(inp)["terms"] if x["term"] == "CAPITAL_KNOWN")["verdict"] == EG.UNKNOWN


@pytest.mark.parametrize("edge_change,expect", [
    ({"net_edge_bps": "-3.1", "session_class": "REJECTED"}, EG.FAIL),
    ({"net_edge_bps": "0.0", "session_class": "PROFIT_CANDIDATE"}, EG.FAIL),
    ({"net_edge_bps": "4.0", "session_class": "NOT_PROFIT_CANDIDATE"}, EG.FAIL),
    ({"net_edge_bps": None}, EG.UNKNOWN),
])
def test_the_edge_must_be_positive_after_every_cost_not_merely_non_negative(edge_change, expect):
    inp = good_inputs()
    inp["edge"].update(edge_change)
    t = next(x for x in EG.evaluate(inp)["terms"] if x["term"] == "EDGE_POSITIVE_AFTER_COSTS")
    assert t["verdict"] == expect and EG.evaluate(inp)["verdict"] == EG.NO_TRADE


@pytest.mark.parametrize("status,expect", [("QUALIFIED", EG.PASS), ("PROMISING", EG.UNKNOWN), ("UNTESTED", EG.UNKNOWN), (None, EG.UNKNOWN), ("NEGATIVE", EG.FAIL), ("INSUFFICIENT", EG.FAIL), ("DATA_INVALID", EG.FAIL), ("MADE_UP", EG.UNKNOWN)])
def test_only_a_qualified_candidate_is_sufficient_statistical_evidence(status, expect):
    inp = good_inputs()
    inp["evidence"] = {"status": status}
    assert next(x for x in EG.evaluate(inp)["terms"] if x["term"] == "STATISTICAL_EVIDENCE_SUFFICIENT")["verdict"] == expect


def test_an_uncalibrated_or_under_charging_execution_model_is_not_valid():
    inp = good_inputs()
    inp["execution"]["sessions_measured"] = 29
    assert next(x for x in EG.evaluate(inp)["terms"] if x["term"] == "EXECUTION_MODEL_VALID")["verdict"] == EG.UNKNOWN
    inp = good_inputs()
    inp["execution"]["mean_cost_surprise_bps"] = "2.5"                           # measured cost exceeded the model by more than the bound
    assert next(x for x in EG.evaluate(inp)["terms"] if x["term"] == "EXECUTION_MODEL_VALID")["verdict"] == EG.FAIL
    inp = good_inputs()
    del inp["execution"]["max_acceptable_surprise_bps"]
    assert next(x for x in EG.evaluate(inp)["terms"] if x["term"] == "EXECUTION_MODEL_VALID")["verdict"] == EG.UNKNOWN


@pytest.mark.parametrize("change,expect", [
    ({"worst_case_loss_usd": 6.0}, EG.FAIL), ({"capital_committed_usd": 20_000.0}, EG.FAIL), ({"book_stops": ["CAPITAL_BOOK_HALTED"]}, EG.FAIL),
    ({"worst_case_loss_usd": None}, EG.UNKNOWN), ({"loss_cap_usd": None}, EG.UNKNOWN), ({"capital_available_usd": "abc"}, EG.UNKNOWN),
])
def test_risk_limits_are_checked_against_the_cap_the_capital_and_the_books_own_stops(change, expect):
    inp = good_inputs()
    inp["risk"].update(change)
    assert next(x for x in EG.evaluate(inp)["terms"] if x["term"] == "RISK_LIMITS_ACCEPTABLE")["verdict"] == expect


@pytest.mark.parametrize("change,expect", [
    ({"capability": "ABSENT"}, EG.FAIL), ({"grant": "ABSENT"}, EG.FAIL), ({"credentials_present": False}, EG.FAIL), ({"lease": "LOST"}, EG.FAIL),
    ({"lease": None}, EG.UNKNOWN), ({"lease": "MAYBE"}, EG.UNKNOWN), ({"grant": None}, EG.UNKNOWN), ({"lease": "NOT_REQUIRED"}, EG.PASS),
])
def test_authority_needs_the_capability_the_grant_the_credentials_and_the_lease(change, expect):
    inp = good_inputs()
    inp["authority"].update(change)
    assert next(x for x in EG.evaluate(inp)["terms"] if x["term"] == "AUTHORITY_VALID")["verdict"] == expect


def test_a_failed_data_flag_fails_data_valid_and_a_missing_one_is_unknown():
    inp = good_inputs()
    inp["data"]["books_fresh"] = False
    assert next(x for x in EG.evaluate(inp)["terms"] if x["term"] == "DATA_VALID")["verdict"] == EG.FAIL
    inp = good_inputs()
    del inp["data"]["funding_observed"]
    assert next(x for x in EG.evaluate(inp)["terms"] if x["term"] == "DATA_VALID")["verdict"] == EG.UNKNOWN


# ------------------------------------------------------------------------------------------------ the measurement profile is its own, narrower thing
def measurement_inputs():
    inp = good_inputs()
    inp["risk"] = {"loss_cap_usd": 5.55, "loss_limit_usd": 5.55, "capital_committed_usd": 170.0, "capital_available_usd": 10_000.0, "book_stops": []}      # no modelled worst case: the limit is a trigger
    inp["edge"].update(net_edge_bps="-31.4", session_class="NOT_PROFIT_CANDIDATE", assumed_inputs=["fee_tier"], provenance={"uncalibrated_modelled": ["hedge_error_bps"], "calibration": None})
    inp["edge"]["capital"]["state"] = "UNSTATED"
    inp["evidence"] = {"status": "UNTESTED"}
    inp["execution"] = {}
    return inp


def test_a_bounded_mechanics_session_is_eligible_for_measurement_only_never_for_profit():
    res = EG.evaluate(measurement_inputs(), profile=EG.MEASUREMENT)
    assert res["verdict"] == EG.ELIGIBLE_MEASUREMENT_ONLY and res["verdict"] != EG.ELIGIBLE
    assert res["waived"] == ["EDGE_POSITIVE_AFTER_COSTS", "STATISTICAL_EVIDENCE_SUFFICIENT"]
    assert "NOT eligibility to trade for profit" in res["claim"] and "fake money" in res["claim"]
    # the very same facts under the PROFIT profile are a NO_TRADE
    assert EG.evaluate(measurement_inputs(), profile=EG.PROFIT_SEEKING)["verdict"] == EG.NO_TRADE


@pytest.mark.parametrize("section", ["data", "risk", "authority"])
def test_measurement_still_needs_valid_data_bounded_risk_and_authority(section):
    inp = measurement_inputs()
    del inp[section]
    res = EG.evaluate(inp, profile=EG.MEASUREMENT)
    assert res["verdict"] == EG.NO_TRADE


def test_measurement_still_refuses_an_unknown_cost_unknown_capital_or_loss_over_the_cap():
    inp = measurement_inputs()
    inp["edge"]["costs_bps"]["spread"] = None
    assert EG.evaluate(inp, profile=EG.MEASUREMENT)["verdict"] == EG.NO_TRADE
    inp = measurement_inputs()
    inp["edge"]["capital"]["committed_usd"] = None
    assert EG.evaluate(inp, profile=EG.MEASUREMENT)["verdict"] == EG.NO_TRADE
    inp = measurement_inputs()
    inp["risk"]["loss_limit_usd"] = 9.0
    assert EG.evaluate(inp, profile=EG.MEASUREMENT)["verdict"] == EG.NO_TRADE


def test_the_waivers_exist_only_under_the_measurement_profile_and_only_for_the_two_named_terms():
    prof = EG.evaluate(good_inputs(), profile=EG.PROFIT_SEEKING)
    assert prof["waived"] == [] and EG.WAIVED not in verdicts(prof).values()
    assert set(EG.MEASUREMENT_WAIVERS) == {"EDGE_POSITIVE_AFTER_COSTS", "STATISTICAL_EVIDENCE_SUFFICIENT"}
    with pytest.raises(ValueError):
        EG.evaluate(good_inputs(), profile="ANYTHING_ELSE")


def test_the_result_is_deterministic_and_the_digest_changes_with_any_term():
    a, b = EG.evaluate(good_inputs()), EG.evaluate(good_inputs())
    assert a == b and len(a["digest"]) == 64
    inp = good_inputs()
    inp["risk"]["worst_case_loss_usd"] = 4.0
    assert EG.evaluate(inp)["digest"] != a["digest"]


def test_every_single_term_failure_in_every_pairing_blocks_trading():
    """Exhaustive over single and double breakages: no combination of passes can hide a failure."""
    breakers = {
        "DATA_VALID": lambda i: i["data"].update(books_fresh=False), "COSTS_KNOWN": lambda i: i["edge"]["costs_bps"].update(spread=None),
        "CAPITAL_KNOWN": lambda i: i["edge"]["capital"].update(committed_usd=None), "EXECUTION_MODEL_VALID": lambda i: i["execution"].update(sessions_measured=3),
        "EDGE_POSITIVE_AFTER_COSTS": lambda i: i["edge"].update(net_edge_bps="-1"), "STATISTICAL_EVIDENCE_SUFFICIENT": lambda i: i["evidence"].update(status="NEGATIVE"),
        "RISK_LIMITS_ACCEPTABLE": lambda i: i["risk"].update(worst_case_loss_usd=99), "AUTHORITY_VALID": lambda i: i["authority"].update(grant="ABSENT"),
    }
    for r in (1, 2):
        for combo in itertools.combinations(sorted(breakers), r):
            inp = copy.deepcopy(good_inputs())
            for name in combo:
                breakers[name](inp)
            res = EG.evaluate(inp)
            assert res["verdict"] == EG.NO_TRADE, combo
            for name in combo:
                assert verdicts(res)[name] != EG.PASS, (combo, name)


def test_the_module_is_pure():
    src = open(os.path.join(os.path.dirname(HERE), "tools", "eligibility_gate.py"), encoding="utf-8").read()
    for banned in ("import os", "import socket", "import time", "urllib", "requests", "open(", "os.environ", "getenv", "datetime", "subprocess"):
        assert banned not in src, banned


def test_a_measurement_session_is_held_to_its_enforced_limit_and_its_worst_case_stays_unknown_not_zero():
    res = EG.evaluate(measurement_inputs(), profile=EG.MEASUREMENT)
    t = next(x for x in res["terms"] if x["term"] == "RISK_LIMITS_ACCEPTABLE")
    assert t["verdict"] == EG.PASS and t["facts"]["worst_case_loss_usd"] == "UNKNOWN" and t["facts"]["loss_limit_usd"] == 5.55
    # the same risk block under the PROFIT profile has no modelled worst case: UNKNOWN, so no trade
    prof = good_inputs()
    prof["risk"] = measurement_inputs()["risk"]
    assert next(x for x in EG.evaluate(prof)["terms"] if x["term"] == "RISK_LIMITS_ACCEPTABLE")["verdict"] == EG.UNKNOWN


def test_book_stops_must_be_stated_and_a_non_numeric_cost_is_not_known():
    """Review P3-2: a missing book_stops was read as 'no stop' and a cost of 'NaN' or 'abc' counted as known."""
    inp = good_inputs()
    del inp["risk"]["book_stops"]
    t = next(x for x in EG.evaluate(inp)["terms"] if x["term"] == "RISK_LIMITS_ACCEPTABLE")
    assert t["verdict"] == EG.UNKNOWN and t["reasons"] == ["BOOK_STOPS_NOT_STATED"]
    inp = good_inputs()
    inp["risk"]["book_stops"] = None
    assert next(x for x in EG.evaluate(inp)["terms"] if x["term"] == "RISK_LIMITS_ACCEPTABLE")["verdict"] == EG.UNKNOWN
    for bad in ("NaN", "inf", "abc", "", [], {}):
        inp = measurement_inputs()
        inp["edge"]["costs_bps"]["spread"] = bad
        res = EG.evaluate(inp, profile=EG.MEASUREMENT)
        assert next(x for x in res["terms"] if x["term"] == "COSTS_KNOWN")["verdict"] == EG.UNKNOWN and res["verdict"] == EG.NO_TRADE, bad
