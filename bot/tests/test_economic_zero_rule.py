"""The RUNTIME rule behind the source scan: a zero-valued, non-observed cost input must not be able to pass the model, however the zero was spelled.

A source scan (tests/test_zero_cost_label_pins.py) is a tripwire for the spellings it knows; THIS is the enforcement. ``ChainResult.assumed_inputs`` (the property ``cost_model_evidence`` and
``ms_edge_gate`` already honour) lists a cost input whose value is economically zero while its provenance is derived / modelled / assumed, unless the caller names a STRUCTURAL reason for the zero
from the closed set ``STRUCTURAL_ZERO_REASONS`` in ``Scenario.zero_reasons``. It is value-based, so keyword arguments, aliases, '0E-8', arithmetic, ``replace()`` and ``**dict`` are all covered.
(Specialist review: the source scan alone caught 3 of 38 planted spellings.)
"""
from __future__ import annotations

import dataclasses
import os
import sys
from decimal import Decimal as D

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.dirname(HERE), os.path.join(os.path.dirname(HERE), "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import economic_model as EM  # noqa: E402
from test_economic_model import overlay  # noqa: E402   (the existing hand-computable fixture: every input known and non-assumed)

Q = EM.Quantity

SPELLINGS = {
    "positional_modelled": lambda u: Q(D(0), "modelled", u),
    "positional_derived": lambda u: Q(D(0), "derived", u),
    "keyword_args": lambda u: Q(value=D(0), provenance="modelled", unit=u),
    "dict_splat": lambda u: Q(**{"value": D(0), "provenance": "modelled", "unit": u}),
    "exponent_zero_0E-8": lambda u: Q(D("0E-8"), "modelled", u),
    "negative_zero": lambda u: Q(-0, "modelled", u),
    "quantized_zero": lambda u: Q(D(0).quantize(D("0.01")), "modelled", u),
    "string_zero": lambda u: Q("0", "modelled", u),
    "tiny_1e-30": lambda u: Q(D("1E-30"), "modelled", u),
    "arithmetic_D1_minus_D1": lambda u: Q(D(1) - D(1), "modelled", u),
    "replace_of_a_measured_one": lambda u: dataclasses.replace(Q(D(1), "measured", u), value=D(0), provenance="modelled"),
    "replace_of_unknown": lambda u: dataclasses.replace(EM.unknown(u), value=D(0), provenance="derived"),
    "empty_qsum": lambda u: EM.qsum([], u),
    "times_literal_zero": lambda u: Q(D(3), "modelled", u) * 0,
    "subclass_GrossEdge": lambda u: EM.GrossEdge(D(0), "modelled", u, D(1), D(1)),
}
# (scenario field, ChainResult component name, unit)
INPUTS = [("impact_bps", "impact", "bps"), ("basis_cost_bps", "basis", "bps"), ("borrow_apr", "borrow", "fraction"), ("opportunity_cost_apr", "opportunity_cost", "fraction")]


@pytest.mark.parametrize("field,comp,unit", INPUTS)
@pytest.mark.parametrize("name", sorted(SPELLINGS))
def test_a_bare_zero_cost_input_is_an_assumed_input_whatever_its_spelling(name, field, comp, unit):
    res = EM.evaluate_chain(overlay(**{field: SPELLINGS[name](unit)}))
    assert comp in res.assumed_inputs, f"{name} for {field}: the model accepted a labelled zero as a cost (assumed_inputs={res.assumed_inputs})"


@pytest.mark.parametrize("field,unit", [("opportunity_cost_apr", "fraction"), ("impact_bps", "bps")])
def test_a_zero_that_is_a_measurement_or_a_nonzero_estimate_is_not_flagged(field, unit):
    """Controls (pass now and after): a MEASURED / venue_stated zero is an observation; a non-zero modelled number is an estimate with visible provenance."""
    comp = dict((f, c) for f, c, _u in INPUTS)[field]
    for q in (Q(D(0), "measured", unit), Q(D(0), "venue_stated", unit), Q(D("0.0365") if unit == "fraction" else D("0.5"), "modelled", unit)):
        res = EM.evaluate_chain(overlay(**{field: q}))
        assert comp not in res.assumed_inputs, (field, q)


def test_a_structural_zero_with_an_accepted_reason_is_allowed_and_a_made_up_reason_is_not():
    ok = EM.evaluate_chain(overlay(borrow_apr=EM.structural_zero("fraction", "SPOT_LEG_PAID_FROM_USDT_CASH_NO_BORROW"), zero_reasons={"borrow_apr": "SPOT_LEG_PAID_FROM_USDT_CASH_NO_BORROW"}))
    assert "borrow" not in ok.assumed_inputs
    bad = EM.evaluate_chain(overlay(borrow_apr=Q(D(0), "derived", "fraction"), zero_reasons={"borrow_apr": "BECAUSE_I_SAID_SO"}))
    assert "borrow" in bad.assumed_inputs
    with pytest.raises(EM.EconomicModelError):
        EM.structural_zero("fraction", "BECAUSE_I_SAID_SO")


def test_an_unjustified_zero_can_never_be_a_pass_in_the_evidence_builder():
    import hashlib
    import json
    from test_economic_model import SPEC
    import strategy_verdict as SV
    sha = hashlib.sha256(b"b5 input").hexdigest()
    clean = EM.cost_model_evidence(EM.evaluate_chain(overlay()), spec=SPEC, data_sha256=sha)
    assert clean.result == SV.PASS                                                  # control: the fixture itself passes
    lazy = EM.cost_model_evidence(EM.evaluate_chain(overlay(opportunity_cost_apr=SPELLINGS["exponent_zero_0E-8"]("fraction"))), spec=SPEC, data_sha256=sha)
    assert lazy.result != SV.PASS and "opportunity_cost" in lazy.metrics["assumed_names"], (lazy.result, lazy.metrics["assumed_names"])


# ---------------------------------------------------------------- the closed set, the bound and the capital a pair commits
def test_the_structural_reasons_are_a_closed_set_and_the_owner_zero_is_one_of_them():
    # the closed list, each reason BOUND to the one input it speaks about. The third reason is the hedge-ratio claim added with the binding: a whole hedge used to be accepted bare, so listing it TIGHTENS the rule.
    assert EM.STRUCTURAL_ZERO_REASONS == frozenset({"SPOT_LEG_PAID_FROM_USDT_CASH_NO_BORROW", "OWNER_STATED_ZERO_COST_OF_CAPITAL", "ZERO_SPOT_TAKER_FEE_SO_NO_BASE_COIN_SHORTFALL"})
    assert {k: set(v) for k, v in EM.STRUCTURAL_ZERO_INPUTS.items()} == {
        "SPOT_LEG_PAID_FROM_USDT_CASH_NO_BORROW": {"borrow_apr"}, "OWNER_STATED_ZERO_COST_OF_CAPITAL": {"opportunity_cost_apr"}, "ZERO_SPOT_TAKER_FEE_SO_NO_BASE_COIN_SHORTFALL": {"hedge_ratio"}}
    fields = {f.name for f in dataclasses.fields(EM.Scenario)}
    assert all(inputs and inputs <= fields for inputs in EM.STRUCTURAL_ZERO_INPUTS.values()), "a reason must name real scenario inputs"
    q = EM.structural_zero("fraction", "OWNER_STATED_ZERO_COST_OF_CAPITAL")
    assert q.value == 0 and q.provenance == "derived" and q.unit == "fraction"
    ok = EM.evaluate_chain(overlay(opportunity_cost_apr=q, zero_reasons={"opportunity_cost_apr": "OWNER_STATED_ZERO_COST_OF_CAPITAL"}))
    assert "opportunity_cost" not in ok.assumed_inputs
    wrong_input = EM.evaluate_chain(overlay(impact_bps=EM.structural_zero("bps", "OWNER_STATED_ZERO_COST_OF_CAPITAL")))       # a reason must be DECLARED for the input; the constructor alone justifies nothing
    assert "impact" in wrong_input.assumed_inputs


def test_a_zero_reason_declared_for_a_nonzero_input_changes_nothing():
    res = EM.evaluate_chain(overlay(opportunity_cost_apr=Q(D("0.0365"), "modelled", "fraction"), zero_reasons={"opportunity_cost_apr": "OWNER_STATED_ZERO_COST_OF_CAPITAL"}))
    assert "opportunity_cost" not in res.assumed_inputs and res.components["opportunity_cost"].value == D("10")


def test_a_known_zero_adverse_selection_is_unjustified_only_where_a_maker_leg_uses_it():
    """The existing test below never sets a KNOWN adverse-selection input, so a mutant that dropped the maker-leg guard survived. Here the input is a known, modelled zero in both worlds."""
    from test_economic_model import book, perp, q as qq, tier
    zero = Q(D(0), "modelled", "bps")
    maker = (EM.Leg("perp", perp(), "sell", book(), tier(maker="-0.0001"), role="maker"),)
    on_maker = EM.evaluate_chain(overlay(legs=maker, adverse_selection_bps=zero, maker_fill_probability=qq("0.8")))
    assert "adverse_selection" in on_maker.assumed_inputs, "a maker leg charges adverse selection: a modelled zero there is an unjustified zero"
    on_taker = EM.evaluate_chain(overlay(adverse_selection_bps=zero))
    assert "adverse_selection" not in on_taker.assumed_inputs and on_taker.unjustified_zero_inputs == (), "a taker-only chain never uses the input: a zero there is not a claim"
    measured = EM.evaluate_chain(overlay(legs=maker, adverse_selection_bps=Q(D(0), "measured", "bps"), maker_fill_probability=qq("0.8")))
    assert "adverse_selection" not in measured.assumed_inputs


def test_a_zero_adverse_selection_is_only_checked_when_a_maker_leg_uses_it():
    """Taker-only scenarios have no adverse selection: the model's own structural zero there is not an unjustified one."""
    res = EM.evaluate_chain(overlay())
    assert res.components["adverse_selection"].value == 0 and "adverse_selection" not in res.assumed_inputs and res.unjustified_zero_inputs == ()


def test_the_upper_bound_treats_only_a_named_unknown_nonnegative_cost_as_free_and_never_reports_a_net():
    res = EM.evaluate_chain(overlay(opportunity_cost_apr=EM.unknown("fraction")))
    assert not res.net_edge_bps.known and "opportunity_cost" in res.unknown_inputs
    bound = EM.net_edge_upper_bound_bps(res, nonneg_unknown=("opportunity_cost",))
    known = EM.evaluate_chain(overlay(opportunity_cost_apr=Q(D(0), "measured", "fraction")))
    assert bound == known.net_edge_bps.value                                              # the bound IS the net with that one cost at zero
    assert EM.net_edge_upper_bound_bps(res) is None                                       # an unnamed unknown makes no bound
    two = EM.evaluate_chain(overlay(opportunity_cost_apr=EM.unknown("fraction"), borrow_apr=EM.unknown("fraction")))
    assert EM.net_edge_upper_bound_bps(two, nonneg_unknown=("opportunity_cost",)) is None  # a second, unnamed unknown: no bound
    assert EM.net_edge_upper_bound_bps(two, nonneg_unknown=("opportunity_cost", "borrow")) is not None
    gross_unknown = EM.evaluate_chain(overlay(gross_edge_bps=EM.unknown("bps"), opportunity_cost_apr=EM.unknown("fraction")))
    assert EM.net_edge_upper_bound_bps(gross_unknown, nonneg_unknown=("opportunity_cost",)) is None    # no gross edge, no bound


def test_the_bound_can_only_reject_never_pass():
    """Property: with the unknown cost floored at zero the bound is >= every net the true (non-negative) cost could give."""
    base = EM.net_edge_upper_bound_bps(EM.evaluate_chain(overlay(opportunity_cost_apr=EM.unknown("fraction"))), nonneg_unknown=("opportunity_cost",))
    for apr in ("0", "0.01", "0.0365", "0.2", "1"):
        true_net = EM.evaluate_chain(overlay(opportunity_cost_apr=Q(D(apr), "measured", "fraction"))).net_edge_bps.value
        assert true_net <= base


@pytest.mark.parametrize("notional,leverage,expect", [("375", 1, "765"), ("200", 1, "408"), ("90", 3, "122.4"), ("1000", 5, "1224"), ("500", 2, "765")])
def test_the_capital_a_hedged_pair_commits_is_spot_plus_perp_margin_plus_the_plan_buffer(notional, leverage, expect):
    q = EM.hedged_pair_capital_usd(notional, leverage)
    assert q.value == D(expect) and q.provenance == "derived" and q.unit == "usd"
    assert EM.PLAN_BUFFER_FRACTION == D("0.02")


@pytest.mark.parametrize("bad", [("0", 1), ("-5", 1), ("100", 0), ("100", D("0.5"))])
def test_a_capital_basis_with_nonsense_inputs_is_refused(bad):
    with pytest.raises(EM.EconomicModelError):
        EM.hedged_pair_capital_usd(*bad)


def test_the_plan_demands_exactly_the_capital_the_canonical_function_gives():
    """One number, two places: build_plan's cash need must equal hedged_pair_capital_usd for the same notional and leverage (they disagreed 2.04x vs 1.16x before)."""
    import ms_capital as CC
    import ms_testnet_session as S
    import inspect
    assert "hedged_pair_capital_usd" in inspect.getsource(S.build_plan)
    assert CC.MARGIN_PER_USD == EM.hedged_pair_capital_usd(1, 1).value == D("2.04")


# ---------------------------------------------------------------- a reason is bound to the input it names (review R2-4: an owner-stated zero COST OF CAPITAL excused a zero market impact)
WRONG_BINDINGS = [
    # (declared against, the zero input, its component name, unit, the reason on the closed list that is NOT bound to that input)
    ("impact_bps", "impact", "bps", "OWNER_STATED_ZERO_COST_OF_CAPITAL"),
    ("impact_bps", "impact", "bps", "SPOT_LEG_PAID_FROM_USDT_CASH_NO_BORROW"),
    ("basis_cost_bps", "basis", "bps", "OWNER_STATED_ZERO_COST_OF_CAPITAL"),
    ("basis_cost_bps", "basis", "bps", "SPOT_LEG_PAID_FROM_USDT_CASH_NO_BORROW"),
    ("borrow_apr", "borrow", "fraction", "OWNER_STATED_ZERO_COST_OF_CAPITAL"),
    ("opportunity_cost_apr", "opportunity_cost", "fraction", "SPOT_LEG_PAID_FROM_USDT_CASH_NO_BORROW"),
    ("opportunity_cost_apr", "opportunity_cost", "fraction", "ZERO_SPOT_TAKER_FEE_SO_NO_BASE_COIN_SHORTFALL"),
    ("adverse_move_bps", "adverse_move", "bps", "OWNER_STATED_ZERO_COST_OF_CAPITAL"),
]


@pytest.mark.parametrize("field,comp,unit,reason", WRONG_BINDINGS)
def test_a_reason_declared_against_an_input_it_is_not_bound_to_justifies_nothing(field, comp, unit, reason):
    zero = EM.structural_zero(unit, reason)                                           # the constructor accepts any listed reason: the BINDING is what the runtime rule enforces
    over = dict(**{field: zero}, zero_reasons={field: reason})
    if field == "adverse_move_bps":
        over["hedge_ratio"] = Q(D("0.6"), "measured", "ratio")                         # a hedge that is not whole, so the move matters
    res = EM.evaluate_chain(overlay(**over))
    assert comp in res.assumed_inputs, f"{reason} excused a zero {field}: assumed_inputs={res.assumed_inputs}"


@pytest.mark.parametrize("field,comp,unit,reason", [("borrow_apr", "borrow", "fraction", "SPOT_LEG_PAID_FROM_USDT_CASH_NO_BORROW"), ("opportunity_cost_apr", "opportunity_cost", "fraction", "OWNER_STATED_ZERO_COST_OF_CAPITAL")])
def test_a_reason_bound_to_its_own_input_still_justifies_it(field, comp, unit, reason):
    res = EM.evaluate_chain(overlay(**{field: EM.structural_zero(unit, reason)}, zero_reasons={field: reason}))
    assert comp not in res.assumed_inputs and res.unjustified_zero_inputs == ()


@pytest.mark.parametrize("bad", [["OWNER_STATED_ZERO_COST_OF_CAPITAL"], {"x": 1}, 7, None, ("OWNER_STATED_ZERO_COST_OF_CAPITAL",)])
def test_a_malformed_reason_is_unjustified_and_never_raises(bad):
    res = EM.evaluate_chain(overlay(opportunity_cost_apr=Q(D(0), "derived", "fraction"), zero_reasons={"opportunity_cost_apr": bad}))
    assert "opportunity_cost" in res.assumed_inputs


# ---------------------------------------------------------------- a zero adverse move and a whole hedge are claims too
@pytest.mark.parametrize("name", sorted(SPELLINGS))
def test_a_bare_zero_adverse_move_against_a_hedge_that_is_not_whole_is_an_assumed_input(name):
    over = dict(hedge_ratio=Q(D("0.6"), "measured", "ratio"), adverse_move_bps=SPELLINGS[name]("bps"))
    if name == "tiny_1e-30":
        with pytest.raises(EM.EconomicModelError, match="magnitude out of bounds"):        # 0.4 x 1E-30 is below the model's magnitude bound: refused outright, which cannot pass either
            EM.evaluate_chain(overlay(**over))
        return
    res = EM.evaluate_chain(overlay(**over))
    assert "adverse_move" in res.assumed_inputs, f"{name}: a zero move behind a 40% hedge error was accepted (assumed_inputs={res.assumed_inputs})"
    assert res.components["hedge_error"].value == 0, "control: the zero really did erase the hedge error, which is why it must not pass bare"


def test_a_zero_adverse_move_that_is_measured_or_irrelevant_or_nonzero_is_not_flagged():
    base = dict(hedge_ratio=Q(D("0.6"), "measured", "ratio"))
    for q in (Q(D(0), "measured", "bps"), Q(D(0), "venue_stated", "bps"), Q(D("100"), "modelled", "bps")):
        assert "adverse_move" not in EM.evaluate_chain(overlay(**base, adverse_move_bps=q)).assumed_inputs, q
    whole = EM.evaluate_chain(overlay(hedge_ratio=Q(D(1), "measured", "ratio"), adverse_move_bps=Q(D(0), "modelled", "bps")))
    assert "adverse_move" not in whole.assumed_inputs, "with a whole, measured hedge there is no residual to move against"


@pytest.mark.parametrize("prov", ["derived", "modelled", "assumed"])
def test_a_whole_hedge_that_is_not_an_observation_is_an_assumed_input_unless_its_reason_is_declared(prov):
    res = EM.evaluate_chain(overlay(hedge_ratio=Q(D(1), prov, "ratio")))
    assert "hedge_ratio" in res.assumed_inputs
    ok = EM.evaluate_chain(overlay(hedge_ratio=Q(D(1), prov, "ratio"), zero_reasons={"hedge_ratio": "ZERO_SPOT_TAKER_FEE_SO_NO_BASE_COIN_SHORTFALL"}))
    assert ("hedge_ratio" in ok.assumed_inputs) == (prov == "assumed"), "an 'assumed' ratio stays assumed whatever reason is declared (the existing rule); the others are justified by the bound reason"
    wrong = EM.evaluate_chain(overlay(hedge_ratio=Q(D(1), prov, "ratio"), zero_reasons={"hedge_ratio": "OWNER_STATED_ZERO_COST_OF_CAPITAL"}))
    assert "hedge_ratio" in wrong.assumed_inputs


def test_a_hedge_that_is_measured_or_not_whole_is_not_flagged():
    for q in (Q(D(1), "measured", "ratio"), Q(D(1), "venue_stated", "ratio"), Q(D("0.999"), "derived", "ratio"), Q(D("0.6"), "modelled", "ratio")):
        assert "hedge_ratio" not in EM.evaluate_chain(overlay(hedge_ratio=q)).assumed_inputs, q
