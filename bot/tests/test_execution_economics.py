"""tools/execution_economics.py: taker vs maker-first, as a labelled sensitivity.

Pins the arithmetic (closed-form break-evens agree with the model they invert),
the labelling (never a measurement, never a production change) and the input
refusals. No venue, no fill data: there is none in the repository.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import replace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import execution_economics as ee  # noqa: E402

BASE = ee.LegAssumptions(taker_bps=5.5, maker_bps=2.0)


def test_a_certain_maker_fill_saves_exactly_the_fee_difference():
    a = replace(BASE, fill_probability=1.0)
    assert ee.taker_leg_bps(a) == 5.5
    assert ee.maker_first_leg_bps(a) == pytest.approx(2.0)


def test_a_fill_that_never_comes_costs_the_cross_plus_the_wait():
    a = replace(BASE, fill_probability=0.0, price_drift_bps=1.0,
                missed_fill_cost_bps=0.5, fill_delay_prints=1.0,
                carry_cost_per_print_bps=2.0)
    assert ee.maker_first_leg_bps(a) == pytest.approx(5.5 + 1.0 + 0.5 + 2.0)


def test_the_hand_computed_mixture():
    a = ee.LegAssumptions(taker_bps=5.5, maker_bps=2.0, taker_impact_bps=0.5,
                          fill_probability=0.6, partial_fill_share=0.75,
                          adverse_selection_bps=1.0, price_drift_bps=0.8,
                          missed_fill_cost_bps=0.4, fill_delay_prints=0.0,
                          final_hedge_mismatch_share=0.1)
    cross = 6.0
    filled = 0.75 * 3.0 + 0.25 * (cross + 0.8)
    missed = cross + 0.8 + 0.4
    want = 0.6 * filled + 0.4 * missed + 0.1 * cross
    assert ee.maker_first_leg_bps(a) == pytest.approx(want)


def test_with_no_cost_of_missing_resting_weakly_dominates():
    """With zero drift, zero missed-fill cost and no wait, a miss just crosses
    late at the taker price, so maker-first can never be worse and the
    break-even is p* = 0. That is what the ASSUMPTIONS say, and the note says
    so: the inputs that decide the question are the unmeasured ones."""
    for adv in (0.0, 1.5):
        be = ee.breakeven_fill_probability(replace(BASE,
                                                   adverse_selection_bps=adv))
        assert be["value"] == 0.0
        assert be["regime"] == "maker_first_always_cheaper"
        assert "not a finding" in be["note"]


@pytest.mark.parametrize("kw", [
    dict(price_drift_bps=2.0), dict(price_drift_bps=1.0,
                                    adverse_selection_bps=1.5),
    dict(partial_fill_share=0.5, missed_fill_cost_bps=1.0),
    dict(final_hedge_mismatch_share=0.2, taker_impact_bps=1.0,
         missed_fill_cost_bps=0.5),
    dict(fill_delay_prints=0.5, carry_cost_per_print_bps=1.0),
])
def test_the_breakeven_fill_probability_is_where_the_costs_cross(kw):
    a = replace(BASE, **kw)
    be = ee.breakeven_fill_probability(a)
    assert be["value"] is not None and 0 <= be["value"] <= 1, be
    at = replace(a, fill_probability=be["value"])
    assert ee.maker_first_leg_bps(at) == pytest.approx(ee.taker_leg_bps(at))
    above = replace(a, fill_probability=min(1.0, be["value"] + 0.05))
    assert ee.maker_first_leg_bps(above) < ee.taker_leg_bps(above)
    assert be["regime"] == "maker_first_cheaper_above"


def test_adverse_selection_large_enough_means_maker_never_wins():
    # a maker fill that costs 2 + 4 bps is worse than crossing at 5.5: filling
    # more only makes it worse, and the break-even names that regime
    a = replace(BASE, adverse_selection_bps=4.0)
    be = ee.breakeven_fill_probability(a)
    assert be["value"] is None
    assert be["regime"] == "maker_first_never_cheaper"
    for p in (0.0, 0.3, 1.0):
        x = replace(a, fill_probability=p)
        assert ee.maker_first_leg_bps(x) >= ee.taker_leg_bps(x)


def test_waiting_costs_can_make_maker_first_lose_at_any_fill_rate():
    a = replace(BASE, fill_delay_prints=1.0, carry_cost_per_print_bps=4.0)
    assert ee.breakeven_fill_probability(a)["regime"] == \
        "maker_first_never_cheaper"


@pytest.mark.parametrize("p,f", [(0.5, 1.0), (0.9, 0.6), (0.2, 0.3)])
def test_the_breakeven_adverse_selection_is_where_the_costs_cross(p, f):
    a = replace(BASE, fill_probability=p, partial_fill_share=f,
                price_drift_bps=0.5)
    be = ee.breakeven_adverse_selection_bps(a)["value"]
    at = replace(a, adverse_selection_bps=be)
    assert ee.maker_first_leg_bps(at) == pytest.approx(ee.taker_leg_bps(at))
    worse = replace(a, adverse_selection_bps=be + 0.1)
    assert ee.maker_first_leg_bps(worse) > ee.taker_leg_bps(worse)


def test_no_maker_fill_means_no_adverse_selection_breakeven():
    assert ee.breakeven_adverse_selection_bps(
        replace(BASE, fill_probability=0.0))["value"] is None


def test_the_report_is_labelled_a_sensitivity_and_changes_nothing():
    r = ee.compare(replace(BASE, fill_probability=0.7),
                   replace(BASE, fill_probability=0.7))
    assert r["measured_fill_data_used"] is False
    assert r["production_execution_style_changed"] is False
    assert "NOT A MEASUREMENT" in r["label"]
    rt = r["round_trip"]
    assert rt["taker_bps"] == pytest.approx(11.0)
    assert rt["maker_first_saving_bps"] == pytest.approx(
        rt["taker_bps"] - rt["maker_first_bps"])
    assert rt["saving_usd_per_round_trip"] == pytest.approx(
        rt["maker_first_saving_bps"] / 1e4 * 100_000.0)


def test_the_engine_default_style_is_still_taker():
    import inspect
    from carry_engine import TAKER, CarryEngine
    assert inspect.signature(CarryEngine.__init__).parameters[
        "execution_style"].default == TAKER


@pytest.mark.parametrize("kw", [
    dict(fill_probability=1.5), dict(fill_probability=-0.1),
    dict(partial_fill_share=2.0), dict(taker_bps=-1.0),
    dict(fill_delay_prints=float("nan")), dict(maker_bps=float("inf")),
    dict(final_hedge_mismatch_share=-0.2), dict(adverse_selection_bps=None),
])
def test_unusable_assumptions_are_refused(kw):
    a = replace(BASE, **kw)
    with pytest.raises(ValueError):
        ee.maker_first_leg_bps(a)


def test_the_cli_prints_the_label_and_writes_json(tmp_path, capsys):
    out = tmp_path / "r.json"
    assert ee.main(["--fill-probability", "0.6", "--adverse-selection-bps",
                    "1", "--exit-funding-bps", "-0.5", "--fill-delay-prints",
                    "0.1", "--json", str(out)]) == 0
    text = capsys.readouterr().out
    assert "NOT A MEASUREMENT" in text and "measured_fill_data_used: false" in text
    data = json.loads(out.read_text())
    assert data["measured_fill_data_used"] is False
    # exit waiting cost is the inverted funding paid while resting
    assert data["legs"]["exit"]["assumptions"]["carry_cost_per_print_bps"] == 0.5
    assert data["legs"]["entry"]["assumptions"]["carry_cost_per_print_bps"] == 0.0
    assert len(data["grid"]) == 30
    assert ee.main(["--fill-probability", "2"]) == 2
