"""Round 22: one economics sheet per decision. Every line carries its basis; an unknown stays UNKNOWN and is never zero; the capital is the ONE definition."""
from __future__ import annotations

import copy
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.dirname(HERE), os.path.join(os.path.dirname(HERE), "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import economic_model as EM  # noqa: E402
import economics_sheet as ES  # noqa: E402
import eligibility_gate as EGATE  # noqa: E402
import ms_testnet_autopilot as AP  # noqa: E402
from ms_testnet_world import World  # noqa: E402


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def real_edge(w, funding="0.0001"):
    """A REAL verdict from the read-only evaluation on the fake venue (no order, no authority)."""
    w.fake.fee_rates = {"spot": ("0.001", "0.001"), "linear": ("0.0002", "0.00055")}
    w.fake.funding_rate, w.fake.funding_in_ms = funding, 600_000
    res = AP.run_once(w.env, inner=w.fake, clock=w.clock.now, dry_run=True, notional_usd="200", engine_root=str(w.tmp / "eng"))
    assert res["result"] == "EVALUATED" and isinstance(res.get("edge"), dict), res
    return res["edge"], res


def test_a_real_verdict_composes_into_every_line_the_directive_names(w):
    edge, res = real_edge(w)
    s = ES.build(edge, notional_usd="200", hold_s=res.get("hold_s"))
    assert s["schema"] == ES.SCHEMA and len(s["digest"]) == 64
    for key in ("gross_edge", "fees", "spread", "slippage_and_impact", "funding", "financing_and_borrow", "capital", "holding_period", "operational_costs", "net", "break_even", "worst_case_loss", "uncertainty"):
        assert key in s, key
    assert s["fees"] and all(v["unit"] == "bps" for v in s["fees"].values())
    for group in ("fees", "spread", "slippage_and_impact", "financing_and_borrow", "other_cost_lines"):
        for name, line in s[group].items():
            assert line["provenance"] in EM.PROVENANCE, (group, name, line)
    assert s["net"]["net_edge_bps"]["provenance"] in ("derived", "unknown")


def test_the_capital_is_the_one_definition_and_matches_the_verdicts_own_figure(w):
    edge, _ = real_edge(w)
    s = ES.build(edge, notional_usd="200")
    cap = s["capital"]
    assert cap["structure"] == EM.ACQUIRED_SPOT_PERP and cap["committed_usd_by_the_one_definition"] == "408.00" and cap["multiple_of_notional"] == "2.04"
    from decimal import Decimal as D
    assert cap["provenance"] == "derived" and D(cap["committed_usd_in_the_verdict"]) == D(cap["committed_usd_by_the_one_definition"])      # the verdict and the definition agree (the verdict's own text drops trailing zeros)
    assert ES.build(edge, structure=EM.PERP_ONLY, notional_usd="200")["capital"]["committed_usd_by_the_one_definition"] == "204.00"
    # no notional: the capital is UNKNOWN, not zero
    assert ES.build(edge)["capital"]["committed_usd_by_the_one_definition"] == "UNKNOWN"


def test_unknown_stays_unknown_never_zero_and_never_a_positive_net():
    edge = {"session_class": "INCONCLUSIVE", "net_edge_bps": None, "expected_funding_bps": None, "expected_net_usd": None, "required_funding_bps": None,
            "costs_bps": {"fee_entry": "5.5", "fee_exit": None, "spread": "1.0", "slippage": None, "financing": None}, "assumed_inputs": ["fee_tier"],
            "provenance": {"components": {"fee_entry": "venue_stated", "spread": "modelled"}, "uncalibrated_modelled": ["hedge_error_bps"], "calibration": None}, "capital": {"state": "UNSTATED"}}
    s = ES.build(edge)
    assert s["fees"]["fee_exit"]["value"] == "UNKNOWN" and s["fees"]["fee_exit"]["provenance"] == "unknown"
    assert s["slippage_and_impact"]["slippage"]["value"] == "UNKNOWN" and s["financing_and_borrow"]["financing"]["value"] == "UNKNOWN"
    assert s["net"]["net_edge_bps"]["value"] == "UNKNOWN" and s["net"]["net_positive"] is None
    assert s["net"]["unknown_cost_lines"] == ["fee_exit", "financing", "slippage"]
    assert s["operational_costs"]["value"] == "UNKNOWN" and s["worst_case_loss"]["modelled_usd"]["value"] == "UNKNOWN"
    assert s["uncertainty"]["uncalibrated_modelled"] == ["hedge_error_bps"] and s["uncertainty"]["assumed_inputs"] == ["fee_tier"]
    unknowns = ES.flatten_unknowns(s)
    assert "fees.fee_exit" in unknowns and "net.net_edge_bps" in unknowns and "worst_case_loss.modelled_usd" in unknowns
    assert "0" not in {s["fees"]["fee_exit"]["value"], s["net"]["net_edge_bps"]["value"]}


def test_a_positive_net_with_an_unknown_cost_line_is_not_called_positive():
    edge = {"session_class": "PROFIT_CANDIDATE", "net_edge_bps": "12.0", "costs_bps": {"fee": "5.0", "slippage": None}, "capital": {}, "provenance": {}}
    assert ES.build(edge)["net"]["net_positive"] is None
    edge["costs_bps"]["slippage"] = "1.0"
    assert ES.build(edge)["net"]["net_positive"] is True
    edge["net_edge_bps"] = "-0.1"
    assert ES.build(edge)["net"]["net_positive"] is False


def test_the_policy_cap_is_labelled_a_trigger_not_a_modelled_worst_case():
    s = ES.build({"costs_bps": {}}, loss_cap_usd=5.55)
    assert s["worst_case_loss"]["policy_cap_usd"]["provenance"] == "policy" and "trigger" in s["worst_case_loss"]["policy_cap_usd"]["note"]
    assert s["worst_case_loss"]["modelled_usd"]["value"] == "UNKNOWN"
    s2 = ES.build({"costs_bps": {}}, worst_case_loss_usd=3.0)
    assert s2["worst_case_loss"]["modelled_usd"] == {"value": "3.0", "unit": "USD", "provenance": "modelled"}


def test_the_eligibility_verdict_is_carried_not_decided_here(w):
    edge, _ = real_edge(w)
    gate = EGATE.evaluate({"edge": edge}, profile=EGATE.MEASUREMENT)
    s = ES.build(edge, notional_usd="200", eligibility=gate)
    assert s["eligibility"]["verdict"] == "NO_TRADE" and s["eligibility"]["digest"] == gate["digest"]
    assert "AUTHORITY_VALID" in s["eligibility"]["unknown"] + s["eligibility"]["failed"]


def test_it_is_deterministic_pure_and_never_raises_on_garbage():
    a = ES.build({"costs_bps": {"fee": "1"}}, notional_usd="100")
    assert a == ES.build({"costs_bps": {"fee": "1"}}, notional_usd="100")
    for junk in (None, "x", 3, [], {"costs_bps": "nope", "capital": 5, "provenance": []}):
        s = ES.build(junk)                                                    # type: ignore[arg-type]
        assert s["net"]["net_positive"] is None and s["capital"]["committed_usd_by_the_one_definition"] == "UNKNOWN"
    src = open(os.path.join(os.path.dirname(HERE), "tools", "economics_sheet.py"), encoding="utf-8").read()
    for banned in ("os.environ", "getenv", "time.time", "urllib", "socket", "requests", "subprocess", "open("):
        assert banned not in src, banned
    json.dumps(copy.deepcopy(a))


def test_net_positive_needs_cost_lines_and_a_finite_net_and_a_supplied_operating_cost_is_not_called_measured():
    """Review P3-3: net_positive was True with no cost lines at all or with a net of 'inf'; any supplied operating cost was labelled measured."""
    assert ES.build({"net_edge_bps": "5.0", "costs_bps": {}})["net"]["net_positive"] is None
    assert ES.build({"net_edge_bps": "inf", "costs_bps": {"fee": "1"}})["net"]["net_positive"] is None
    assert ES.build({"net_edge_bps": "NaN", "costs_bps": {"fee": "1"}})["net"]["net_positive"] is None
    assert ES.build({"net_edge_bps": "5.0", "costs_bps": {"fee": "1"}})["net"]["net_positive"] is True
    op = ES.build({"costs_bps": {}}, operating_cost_usd_per_day=3.0)["operational_costs"]
    assert op["provenance"] == "stated" and "cannot verify" in op["note"]
