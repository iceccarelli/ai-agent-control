"""Cost reality for the carry OVERLAY: what a hold must earn back.

Offline and arithmetic only. Every number is either a repo constant (perp taker
5.5 bps per leg, 3 settlements/day, DEMONSTRATED_HOLD_DAYS 5.8 measured by the
hold audit) or given in the task; no market data is invented.

THE HEADLINE, stated plainly: at typical BTC funding (~1 bps per 8h, 0.01%) the
overlay needs ~11 settlements (~3.7 days) just to earn back its own 11 bps round
trip, ignoring that funding is variable and can flip sign. With ONE settlement
held it needs more than 11 bps per 8h (0.11%), an order of magnitude above
typical funding. The carry does NOT clear its costs on a short hold at typical
funding; it can only pay on a hold that is long AND stays positive.

THE GAP these tests reproduced, now closed: `evaluate_entry` amortises the round
trip over ASSUMED_HOLD_DAYS = 30, but no code holds a position 30 days (the exit
is FUNDING_INVERTED only; the measured median hold was ~5.8 days). So funding
that needs 12 days to break even was admitted. `carry_costs.entry_gate` now runs
`hold_clears_round_trip` after `evaluate_entry` and `CarryEngine.entry_economics`
asks it, so the production entry refuses that funding. It only ever refuses
more; the simulators and the C++ core make the same decision
(tests/test_entry_gate_parity.py).
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import carry_costs as cc  # noqa: E402

RT = 2 * cc.TAKER_BPS_PERP            # overlay: two perp legs, no spot leg
PER_DAY = cc.FUNDING_PERIODS_PER_DAY


def test_overlay_round_trip_is_eleven_bps_from_the_perp_taker_fee():
    assert RT == 11.0
    assert cc.TAKER_BPS_PERP == 5.5


@pytest.mark.parametrize("n,expected", [
    (1, 11.0), (2, 5.5), (3, 11 / 3), (6, 11 / 6), (11, 1.0),
    (17, 11 / 17), (30, 11 / 30), (90, 11 / 90)])
def test_breakeven_table_for_the_overlay_round_trip(n, expected):
    assert cc.breakeven_funding_bps_per_settlement(
        n, round_trip_bps=RT) == pytest.approx(expected)


def test_one_settlement_held_needs_more_than_eleven_bps_per_8h():
    """11 bps round trip, one settlement: funding must reach 11 bps per 8h,
    i.e. 0.11%, about 11x the typical ~1 bps (0.01%) BTC funding. A single
    settlement can never pay for the carry at typical funding."""
    assert cc.breakeven_funding_bps_per_settlement(1, round_trip_bps=RT) == 11.0
    typical = 1.0
    assert typical < cc.breakeven_funding_bps_per_settlement(
        1, round_trip_bps=RT)
    assert cc.min_settlements_to_clear(typical, round_trip_bps=RT) != 1


def test_carry_cannot_clear_costs_at_typical_funding_inside_a_short_hold():
    """At ~1 bps per 8h the break-even hold is 11 settlements = 11/3 = ~3.7
    days (ignoring that funding is variable and can flip sign, which only
    lengthens it). Anything shorter LOSES money at typical funding: the carry
    cannot clear its costs on a one-day hold."""
    assert cc.min_settlements_to_clear(1.0, round_trip_bps=RT) == 11
    assert 11 / PER_DAY == pytest.approx(3.667, abs=1e-3)
    for held in (1, 3, 9, 10):            # up to ~3.3 days: still under water
        assert held * 1.0 < RT


def test_min_settlements_for_other_expected_funding():
    assert cc.min_settlements_to_clear(11.0, round_trip_bps=RT) == 1
    assert cc.min_settlements_to_clear(5.5, round_trip_bps=RT) == 2
    assert cc.min_settlements_to_clear(2.0, round_trip_bps=RT) == 6   # 5.5 -> 6
    assert cc.min_settlements_to_clear(0.5, round_trip_bps=RT) == 22
    assert cc.min_settlements_to_clear(10.99, round_trip_bps=RT) == 2


def test_breakeven_and_min_settlements_agree():
    for f in (0.3, 0.7, 1.0, 1.7, 3.0, 8.0):
        n = cc.min_settlements_to_clear(f, round_trip_bps=RT)
        assert f >= cc.breakeven_funding_bps_per_settlement(
            n, round_trip_bps=RT) - 1e-9
        if n > 1:
            assert f < cc.breakeven_funding_bps_per_settlement(
                n - 1, round_trip_bps=RT)


def test_borrow_raises_the_breakeven_and_zero_borrow_is_the_overlay_case():
    assert cc.breakeven_funding_bps_per_settlement(
        11, round_trip_bps=RT, borrow_bps_per_day=0.0) == pytest.approx(1.0)
    # 0.3 bps/day of borrow is 0.1 bps per settlement
    assert cc.breakeven_funding_bps_per_settlement(
        11, round_trip_bps=RT, borrow_bps_per_day=0.3) == pytest.approx(1.1)


def test_funding_at_or_below_borrow_never_clears():
    assert cc.min_settlements_to_clear(
        0.1, round_trip_bps=RT, borrow_bps_per_day=0.3) is None
    assert cc.min_settlements_to_clear(0.0, round_trip_bps=RT) is None
    assert cc.min_settlements_to_clear(-2.0, round_trip_bps=RT) is None
    assert cc.min_settlements_to_clear(float("nan"), round_trip_bps=RT) is None


@pytest.mark.parametrize("n,rt,borrow", [
    (0, 11.0, 0.0), (-1, 11.0, 0.0), (float("nan"), 11.0, 0.0),
    (1, 0.0, 0.0), (1, -11.0, 0.0), (1, float("nan"), 0.0),
    (1, None, 0.0), (1, 11.0, -0.1), (1, 11.0, float("inf"))])
def test_breakeven_refuses_unpriceable_inputs(n, rt, borrow):
    with pytest.raises(ValueError):
        cc.breakeven_funding_bps_per_settlement(
            n, round_trip_bps=rt, borrow_bps_per_day=borrow)


def test_min_settlements_refuses_unpriceable_costs():
    for rt in (0.0, -1.0, float("nan"), None):
        with pytest.raises(ValueError):
            cc.min_settlements_to_clear(1.0, round_trip_bps=rt)


def test_basis_is_a_hedge_loss_at_a_discount_not_edge():
    """Short perp + long spot earns entry_basis - exit_basis. Perp BELOW spot
    by 40 bps converging to 0 is a 40 bps LOSS on the hedge (the short pays as
    the perp rises to spot). It is not credited to the break-even above."""
    assert cc.hedge_basis_pnl_bps(-40.0, 0.0) == -40.0
    assert cc.hedge_basis_pnl_bps(30.0, 0.0) == 30.0     # rich perp sheds: gain
    # and the break-even function has no basis term at all
    assert cc.breakeven_funding_bps_per_settlement(
        11, round_trip_bps=RT) == pytest.approx(1.0)


# -- the entry gate ---------------------------------------------------------

def prints(rate, n=5):
    return [rate] * n


def test_gap_evaluate_entry_alone_admits_funding_that_needs_twelve_days():
    """THE GAP. 0.3 bps/8h needs 37 settlements (~12.3 days) to earn back 11
    bps, yet evaluate_entry (30 day assumed hold) allows it. Kept as a pin on
    the underlying gate so the need for the horizon check stays visible."""
    v = cc.evaluate_entry(funding_prints_bps=prints(0.3), perp=100_000.0,
                          spot=100_000.0, borrow_apr=0.0, round_trip_bps=RT)
    assert v.allowed
    assert cc.min_settlements_to_clear(0.3, round_trip_bps=RT) == 37


def test_horizon_gate_refuses_funding_that_cannot_clear_in_the_measured_hold():
    v = cc.hold_clears_round_trip(funding_prints_bps=prints(0.3),
                                  round_trip_bps=RT, borrow_apr=0.0)
    assert not v and v.reason == "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"
    assert v.detail["settlements_needed"] == 37
    assert v.detail["settlements_budget"] == 17        # floor(5.8 * 3)


def test_horizon_gate_admits_typical_one_bp_funding_only_because_11_fits_in_17():
    """1 bps/8h needs 11 settlements, inside the 17 measured. This is the
    honest boundary: the gate refuses what cannot pay inside the measured hold;
    it does not claim 1 bps is profitable on a hold shorter than ~3.7 days."""
    v = cc.hold_clears_round_trip(funding_prints_bps=prints(1.0),
                                  round_trip_bps=RT, borrow_apr=0.0)
    assert v.allowed and v.detail["settlements_needed"] == 11


def test_horizon_gate_boundary_is_exact():
    edge = RT / 17                                       # 0.647058... bps
    ok = cc.hold_clears_round_trip(funding_prints_bps=prints(edge + 1e-6),
                                   round_trip_bps=RT, borrow_apr=0.0)
    no = cc.hold_clears_round_trip(funding_prints_bps=prints(edge - 1e-3),
                                   round_trip_bps=RT, borrow_apr=0.0)
    assert ok.allowed and not no.allowed


def test_horizon_gate_scales_with_the_acquire_round_trip():
    v = cc.hold_clears_round_trip(funding_prints_bps=prints(1.0),
                                  round_trip_bps=cc.ROUND_TRIP_BPS,
                                  borrow_apr=0.0)
    assert not v                                         # 31 settlements > 17


def test_horizon_gate_borrow_can_make_it_never_clear():
    v = cc.hold_clears_round_trip(funding_prints_bps=prints(1.0),
                                  round_trip_bps=RT, borrow_apr=0.20)
    # 0.20 APR = 5.48 bps/day = 1.83 bps/settlement > 1.0
    assert not v and v.detail["settlements_needed"] is None


@pytest.mark.parametrize("kw", [
    dict(round_trip_bps=float("nan")), dict(round_trip_bps=0.0),
    dict(round_trip_bps=None), dict(borrow_apr=-0.01),
    dict(borrow_apr=float("nan")), dict(hold_days=0.0),
    dict(hold_days=float("nan")), dict(hold_days=-1.0)])
def test_horizon_gate_fails_closed_on_unusable_inputs(kw):
    args = dict(funding_prints_bps=prints(50.0), round_trip_bps=RT,
                borrow_apr=0.0)
    args.update(kw)
    v = cc.hold_clears_round_trip(**args)
    assert not v and v.reason == "COST_INPUT_INVALID"


def test_horizon_gate_fails_closed_on_thin_or_negative_history():
    assert cc.hold_clears_round_trip(
        funding_prints_bps=[50.0, 50.0], round_trip_bps=RT,
        borrow_apr=0.0).reason == "INSUFFICIENT_FUNDING_HISTORY"
    v = cc.hold_clears_round_trip(funding_prints_bps=prints(-1.0),
                                  round_trip_bps=RT, borrow_apr=0.0)
    assert not v


def test_horizon_gate_uses_the_smoothed_rate_not_the_last_print():
    v = cc.hold_clears_round_trip(funding_prints_bps=[0.2, 0.2, 0.2, 9.0],
                                  round_trip_bps=RT, borrow_apr=0.0)
    assert v.smoothed_funding_bps == pytest.approx(
        cc.ewma_funding_bps([0.2, 0.2, 0.2, 9.0]))


def test_the_horizon_is_the_measured_median_and_assumed_hold_is_unchanged():
    assert cc.DEMONSTRATED_HOLD_DAYS == 5.8
    assert cc.ASSUMED_HOLD_DAYS == 30.0        # nothing loosened or retuned


# -- the engine asks the same gate ---------------------------------------------

def _engine(history):
    from carry_engine import CarryEngine

    class V:
        """Only the fee table is read by entry_economics; it places nothing."""

        def get_fee_rates(self, symbol, product):
            return {"maker_bps": 2.0, "taker_bps": 5.5}

    e = CarryEngine(broker=V(), max_notional_usd=100.0, borrow_apr=0.0,
                    execution_mode="overlay", persist=lambda s: None)
    e._funding_history = list(history)
    return e


def test_the_overlay_engine_refuses_what_cannot_clear_in_the_measured_hold():
    """Was test_gap_... (the engine ADMITTED 0.3 bps/8h, break-even 37
    settlements = ~12.3 days, against a measured median hold of ~5.8 days) and
    a strict xfail on the desired behaviour. The semantics changed on purpose:
    the production entry now refuses it, with the horizon gate's own reason."""
    e = _engine(prints(0.3))
    econ = e.entry_economics(mark=100_000.0, spot=100_000.0, funding_bps=0.3)
    assert econ.round_trip_bps == 11.0
    # evaluate_entry alone still allows it: the refusal is the horizon gate's
    assert cc.evaluate_entry(funding_prints_bps=prints(0.3), perp=100_000.0,
                             spot=100_000.0, borrow_apr=0.0,
                             round_trip_bps=11.0).allowed
    assert econ.refusal is not None and not econ.verdict.allowed
    assert econ.refusal.reason == "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"
    assert econ.refusal.action == "stand_aside"


def test_the_overlay_engine_still_admits_funding_that_clears_both_gates():
    e = _engine(prints(1.0))
    econ = e.entry_economics(mark=100_000.0, spot=100_000.0, funding_bps=1.0)
    assert econ.refusal is None and econ.verdict.allowed


def test_a_break_even_too_large_to_count_is_never_not_an_overflow():
    """`hold_clears_round_trip` promises to fail closed on every unusable input.
    A round trip so large that round_trip / net funding overflows to infinity
    used to raise OverflowError out of math.ceil (carry_preflight calls it
    directly, unguarded), while the native core answers "refused" for the same
    arithmetic. It is a refusal in both."""
    huge = 1.7e308
    assert cc.min_settlements_to_clear(1e-3, round_trip_bps=huge) is None
    v = cc.hold_clears_round_trip(funding_prints_bps=[1e-3] * 5,
                                  round_trip_bps=huge, borrow_apr=0.0)
    assert v.allowed is False
    assert v.reason == "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"
    assert v.detail["settlements_needed"] is None
    # a large but countable break-even is unchanged: counted, then over budget
    assert cc.min_settlements_to_clear(1.0, round_trip_bps=1e300) == int(1e300)
    # and the combined entry gate never reaches it with an absurd round trip
    gate = cc.entry_gate(funding_prints_bps=[1.0] * 5, perp=100_000.0,
                         spot=100_000.0, borrow_apr=0.0, round_trip_bps=huge)
    assert gate.allowed is False
