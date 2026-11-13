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
    "tiny_1e-11": lambda u: Q(D("1E-11"), "modelled", u),                       # above the old 1e-12 bound: the independent re-review passed it for impact, basis, borrow and opportunity cost
    "tiny_5e-10": lambda u: Q(D("5E-10"), "derived", u),
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
    for q in (Q(D(0), "measured", unit), Q(D(0), "venue_stated", unit), Q(D("0.0365") if unit == "fraction" else D("0.5"), "modelled", unit), Q(D("1E-8"), "modelled", unit)):
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
    assert abs(res.components["hedge_error"].value) < D("1E-9"), "control: the zero really did erase the hedge error, which is why it must not pass bare"


def test_a_zero_adverse_move_that_is_measured_or_irrelevant_or_nonzero_is_not_flagged():
    base = dict(hedge_ratio=Q(D("0.6"), "measured", "ratio"))
    for q in (Q(D(0), "measured", "bps"), Q(D(0), "venue_stated", "bps"), Q(D("100"), "modelled", "bps")):
        assert "adverse_move" not in EM.evaluate_chain(overlay(**base, adverse_move_bps=q)).assumed_inputs, q
    whole = EM.evaluate_chain(overlay(hedge_ratio=Q(D(1), "measured", "ratio"), adverse_move_bps=Q(D(0), "modelled", "bps")))
    assert "adverse_move" not in whole.assumed_inputs, "with a whole, measured hedge there is no residual to move against"


@pytest.mark.parametrize("prov", ["derived", "modelled", "assumed"])
def test_a_whole_hedge_that_is_not_an_observation_is_an_assumed_input_unless_its_reason_is_declared_and_true_of_the_legs(prov):
    from test_economic_model import book, perp, spot, tier
    whole = Q(D(1), prov, "ratio")
    reason = {"hedge_ratio": "ZERO_SPOT_TAKER_FEE_SO_NO_BASE_COIN_SHORTFALL"}

    def legs(*, side="buy", taker="0", maker="0", in_base=True, role="taker"):
        return (EM.Leg("spot", spot(), side, book(), tier(taker=taker, maker=maker), role, fee_in_base=in_base), EM.Leg("perp", perp(), "sell", book(), tier()))
    assert "hedge_ratio" in EM.evaluate_chain(overlay(hedge_ratio=whole)).assumed_inputs                                   # bare: a claim
    assert "hedge_ratio" in EM.evaluate_chain(overlay(hedge_ratio=whole, zero_reasons=reason)).assumed_inputs, "declared on a scenario with NO spot leg: the reason's fact does not hold (re-review Z1)"
    ok = EM.evaluate_chain(overlay(legs=legs(), hedge_ratio=whole, zero_reasons=reason))
    assert ("hedge_ratio" in ok.assumed_inputs) == (prov == "assumed"), "an 'assumed' ratio stays assumed whatever reason is declared (the existing rule); the others are justified by the bound reason AND the fact"
    for why, kw in (("a spot taker fee that is not zero", dict(taker="0.001")), ("a spot SELL leg", dict(side="sell")), ("a fee not taken in the base coin", dict(in_base=False))):
        assert "hedge_ratio" in EM.evaluate_chain(overlay(legs=legs(**kw), hedge_ratio=whole, zero_reasons=reason)).assumed_inputs, why
    assert "hedge_ratio" in EM.evaluate_chain(overlay(legs=legs(role="maker", maker="0.0002", taker="0"), hedge_ratio=whole, zero_reasons=reason)).assumed_inputs, "a maker leg is judged by its MAKER rate"
    assert "hedge_ratio" not in EM.evaluate_chain(overlay(legs=legs(role="maker", maker="0", taker="0.001"), hedge_ratio=whole, zero_reasons=reason, maker_fill_probability=Q(D(1), "measured", "probability"))).assumed_inputs \
        or prov == "assumed"
    wrong = EM.evaluate_chain(overlay(legs=legs(), hedge_ratio=whole, zero_reasons={"hedge_ratio": "OWNER_STATED_ZERO_COST_OF_CAPITAL"}))
    assert "hedge_ratio" in wrong.assumed_inputs, "a reason bound to another input justifies nothing"


@pytest.mark.parametrize("ratio", ["0.99999999999", "0.9999999995", "1.0000000001"])
def test_a_hedge_ratio_within_a_billionth_of_whole_is_a_whole_hedge_claim(ratio):
    for prov in ("derived", "modelled"):
        assert "hedge_ratio" in EM.evaluate_chain(overlay(hedge_ratio=Q(D(ratio), prov, "ratio"))).assumed_inputs, (ratio, prov)
    assert "hedge_ratio" not in EM.evaluate_chain(overlay(hedge_ratio=Q(D("0.99999"), "derived", "ratio"))).assumed_inputs, "0.00001 short of whole is a real (if small) hedge error, an estimate with provenance"


def test_a_hedge_that_is_measured_or_not_whole_is_not_flagged():
    for q in (Q(D(1), "measured", "ratio"), Q(D(1), "venue_stated", "ratio"), Q(D("0.999"), "derived", "ratio"), Q(D("0.6"), "modelled", "ratio")):
        assert "hedge_ratio" not in EM.evaluate_chain(overlay(hedge_ratio=q)).assumed_inputs, q


# ------------------------------------------------------------------ Round 19: ONE structure-keyed capital definition (capital specialist F1/F6, quant specialist F1)
@pytest.mark.parametrize("lev", [1, 2, 3, 5])
def test_the_structure_registry_is_the_one_definition_the_two_wrappers_read(lev):
    assert EM.structure_capital_usd(EM.ACQUIRED_SPOT_PERP, 1, lev).value == EM.hedged_pair_capital_usd(1, lev).value
    assert EM.structure_capital_usd(EM.PERP_ONLY, 1, lev).value == EM.perp_only_capital_usd(1, lev).value
    # coin the owner already holds is capital that could be SOLD (it earns its alternative either way), so with no collateral credit claimed it commits what the bought pair does
    assert EM.structure_capital_usd(EM.OWNED_INVENTORY_OVERLAY, 1, lev).value == EM.hedged_pair_capital_usd(1, lev).value
    assert set(EM.CAPITAL_STRUCTURES) == {EM.ACQUIRED_SPOT_PERP, EM.OWNED_INVENTORY_OVERLAY, EM.PERP_ONLY}


def test_an_unstated_collateral_ratio_is_an_upper_bound_labelled_assumed_and_a_stated_one_is_derived():
    unstated = EM.structure_capital_usd(EM.OWNED_INVENTORY_OVERLAY, 100_000, 1)
    assert unstated.value == D("204000") and unstated.provenance == "assumed", "no credit is claimed for held coin until the owner states the venue's collateral ratio: the margin is paid separately"
    stated = EM.structure_capital_usd(EM.OWNED_INVENTORY_OVERLAY, 100_000, 1, inventory_collateral_ratio=D("0.9"))
    assert stated.value == D("112200") and stated.provenance == "derived", "0.9 of the held coin backs the margin: only 10% of it is paid in cash, plus the 2% buffer on both"
    full = EM.structure_capital_usd(EM.OWNED_INVENTORY_OVERLAY, 100_000, 5, inventory_collateral_ratio=D("1"))
    assert full.value == D("102000"), "the credit can never exceed the margin it backs: capital is the inventory plus its buffer, never less"
    for bad in (D("-0.1"), D("1.01"), float("nan"), D("inf")):
        with pytest.raises(EM.EconomicModelError):
            EM.structure_capital_usd(EM.OWNED_INVENTORY_OVERLAY, 1, 1, inventory_collateral_ratio=bad)
    assert EM.structure_capital_usd(EM.ACQUIRED_SPOT_PERP, 1, 1).provenance == "derived" and EM.structure_capital_usd(EM.PERP_ONLY, 1, 1).provenance == "derived"
    with pytest.raises(EM.EconomicModelError):
        EM.structure_capital_usd("SOMETHING_ELSE", 1, 1)
    with pytest.raises(EM.EconomicModelError):
        EM.structure_capital_usd(EM.PERP_ONLY, 1, 1, inventory_collateral_ratio=D("0.9"))           # a ratio that nothing reads is a mistake, not a default


_CAPITAL_MULTIPLE_LITERALS = {1.16, 2.04, 2.003, 2.02, 1.122, 1.224}
_ALLOWED_CAPITAL_LITERALS = {("opportunity_map.py", 1.16): "the research overlay: BTC already held + an ASSUMED ~15% perp margin + 1% buffer (pinned to 1 + 0.15 + 0.01 and below the executor's 2.04)"}


def _capital_literals(sources):
    """``(basename, value, line)`` for every float literal in ``sources`` ((name, python source) pairs) that is a capital multiple."""
    import ast
    return [(os.path.basename(name), node.value, node.lineno) for name, src in sources for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Constant) and isinstance(node.value, float) and node.value in _CAPITAL_MULTIPLE_LITERALS]


def test_the_capital_literal_tripwire_sees_a_planted_multiple_in_every_spelling_it_polices():
    planted = {"x = 2.04": 2.04, "def f():\n    return 1.16": 1.16, "CAP = {'a': 2.003}": 2.003, "y = n * 2.02": 2.02, "z = 1.122": 1.122, "w = (1.224)": 1.224}
    for src, value in planted.items():
        assert _capital_literals([("tools/new_module.py", src)]) == [("new_module.py", value, src.count("\n") + 1)], src
    assert _capital_literals([("tools/ok.py", "x = 1.02  # a tolerance\ny = 2.5\nz = 'a string 2.04'")]) == []


def test_every_capital_multiple_in_production_code_comes_from_the_registry_or_is_a_named_assumption():
    """The tripwire (quant F1, capital F1): the same hedge was priced at 1.0 / 1.10 / 1.16 / 1.2 / 2.003 / 2.02 / 2.04 by different modules. A numeric literal that IS a capital multiple may exist
    in exactly one place, the research overlay's labelled ASSUMPTION; every other multiple is computed by ``economic_model.structure_capital_usd`` (or its two wrappers)."""
    import glob
    root = os.path.dirname(HERE)
    files = sorted(glob.glob(os.path.join(root, "*.py")) + glob.glob(os.path.join(root, "tools", "*.py")))
    assert len(files) > 100, "the walker found no shipped code: the tripwire would pass vacuously"
    hits = [(name, base, value, line) for name in files for (base, value, line) in _capital_literals([(name, open(name, encoding="utf-8").read())])]
    offenders = [f"{os.path.relpath(name, root)}:{line} = {value}" for name, base, value, line in hits if (base, value) not in _ALLOWED_CAPITAL_LITERALS]
    assert not offenders, f"a numeric capital multiple outside the registry: {offenders}; compute it with economic_model.structure_capital_usd"
    assert set(_ALLOWED_CAPITAL_LITERALS) <= {(base, value) for _n, base, value, _l in hits}, "the allow-list names a literal that no longer exists (delete the entry)"
