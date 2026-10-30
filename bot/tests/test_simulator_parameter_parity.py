"""The Python simulator and the C++ core mean the same thing by every parameter.

The core takes the funding window, EWMA alpha and minimum prints, the
negative-print exit streak, the assumed hold and the demonstrated hold as
parameters. The Python `simulate_settlements_series` used to hard-code all six,
so a sweep with a non-default value was not Python-equivalent and the
equivalence tests only ever compared the defaults. Each knob is now a keyword
of the Python simulator with the production value as its default, and each is
compared here, away from the default, trade for trade and to the cent.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import carry_backtest as cb  # noqa: E402
import carry_core as core  # noqa: E402

REPO = os.path.join(os.path.dirname(__file__), "..")
CENT = 0.01


@pytest.fixture(scope="module")
def native():
    built = core.build()
    if not built and not core.available():
        pytest.skip("no compiler and no libcarrycore.so")
    return core


@pytest.fixture(scope="module")
def rows():
    return cb.load_bybit_settlements(REPO)[0]


def both(rows, *, gated=True, borrow_apr=0.0, **knobs):
    mode = cb.OVERLAY
    python = cb.simulate_settlements_series(
        rows, borrow_apr=borrow_apr, gated=gated, impact_bps=1.093, mode=mode,
        **knobs)
    params = core.params(
        borrow_apr=borrow_apr, impact_bps=1.093, gated=gated, overlay=True,
        round_trip_bps=cb.round_trip_bps_for(mode),
        taker_spot_bps=cb.TAKER_BPS_SPOT, taker_perp_bps=cb.TAKER_BPS_PERP,
        **knobs)
    return python, core.simulate(rows, params)


def assert_same(python, cpp, label):
    a = python["attribution"]
    assert cpp["trades"] == python["trades"], label
    assert cpp["closed_trades"] == python["closed_trades"], label
    assert cpp["net"] == pytest.approx(a["net_usd"], abs=CENT), label
    assert cpp["funding"] == pytest.approx(a["funding_usd"], abs=CENT), label
    assert cpp["fees"] == pytest.approx(-a["fees_usd"], abs=CENT), label


NON_DEFAULT = [
    ("funding_history", dict(funding_history=4)),
    ("funding_history", dict(funding_history=12)),
    ("ewma_alpha", dict(ewma_alpha=0.2)),
    ("ewma_alpha", dict(ewma_alpha=0.9)),
    ("ewma_min_prints", dict(ewma_min_prints=6)),
    ("negative_exit_prints", dict(negative_exit_prints=1)),
    ("negative_exit_prints", dict(negative_exit_prints=6)),
    ("hold_days", dict(hold_days=10.0)),
    ("hold_days", dict(hold_days=90.0)),
    ("demonstrated_hold_days", dict(demonstrated_hold_days=2.0)),
    ("demonstrated_hold_days", dict(demonstrated_hold_days=30.0)),
    ("all together", dict(funding_history=5, ewma_alpha=0.3, ewma_min_prints=4,
                          negative_exit_prints=2, hold_days=20.0,
                          demonstrated_hold_days=8.0)),
]


@pytest.mark.parametrize("name,knobs", NON_DEFAULT,
                         ids=[f"{n}={list(k.values())}" for n, k in NON_DEFAULT])
def test_a_non_default_parameter_means_the_same_in_both(native, rows, name, knobs):
    python, cpp = both(rows, **knobs)
    assert_same(python, cpp, f"{name} {knobs}")


@pytest.mark.parametrize("name,knobs", NON_DEFAULT[:11:2])
def test_with_borrow_charged_as_well(native, rows, name, knobs):
    python, cpp = both(rows, borrow_apr=0.05, **knobs)
    assert_same(python, cpp, f"{name} {knobs} @5% borrow")


def test_the_defaults_are_still_the_production_values(native, rows):
    import carry_costs
    explicit = both(rows, funding_history=8, ewma_alpha=carry_costs.EWMA_ALPHA,
                    ewma_min_prints=carry_costs.EWMA_MIN_PRINTS,
                    negative_exit_prints=cb.NEGATIVE_EXIT_PRINTS,
                    hold_days=carry_costs.ASSUMED_HOLD_DAYS,
                    demonstrated_hold_days=carry_costs.DEMONSTRATED_HOLD_DAYS)[0]
    implicit = both(rows)[0]
    assert explicit["attribution"] == implicit["attribution"]
    assert explicit["trades"] == implicit["trades"]


def test_a_knob_that_does_nothing_would_be_caught(native, rows):
    """The knobs have to MOVE the result, or the comparison above proves
    nothing: at least four of them change the trade count or the net."""
    base = both(rows)[0]
    moved = 0
    for _name, knobs in NON_DEFAULT:
        python = both(rows, **knobs)[0]
        if (python["trades"] != base["trades"]
                or python["attribution"]["net_usd"] != base["attribution"]["net_usd"]):
            moved += 1
    assert moved >= 8


@pytest.mark.parametrize("bad", [dict(funding_history=0), dict(ewma_min_prints=0),
                                 dict(negative_exit_prints=0),
                                 dict(ewma_alpha=float("nan")),
                                 dict(hold_days=0.0)])
def test_nonsense_values_are_refused_by_the_python_simulator(rows, bad):
    with pytest.raises(ValueError):
        cb.simulate_settlements_series(rows, gated=True, mode=cb.OVERLAY, **bad)
