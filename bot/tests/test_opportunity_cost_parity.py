"""Opportunity cost of capital: one input, four implementations, one decision.

`opportunity_cost_apr` is the forgone return on the hedged capital, a per-year
cost on the hedged notional charged exactly like `borrow_apr`: beside borrow in
the entry gate (both halves: the amortised cost check and the demonstrated-hold
break-even) and per held print on qty * spot in the simulators. Its default is
0.0 and the repository holds no rate, so nothing here supplies one in
production; these tests pin that

  * the default reproduces every earlier decision and every earlier number,
  * the Python gate, the runtime engine, the Python settlement simulator, the
    replay and the C++ core take the SAME decision on the same inputs, at the
    default and away from it,
  * a value that exceeds the funding edge makes the gate refuse,
  * a value the gate cannot price is refused, never treated as zero,
  * it only ever refuses MORE.
"""
from __future__ import annotations

import math
import os
import random
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import carry_backtest as cb  # noqa: E402
import carry_core as core  # noqa: E402
import carry_costs as cc  # noqa: E402
from carry_engine import OVERLAY, CarryEngine  # noqa: E402

SPOT = 100_000.0
H8 = 8 * 3600 * 1000
CENT = 0.01


@pytest.fixture(scope="module")
def native():
    built = core.build()
    if not built and not core.available():
        if os.environ.get("REQUIRE_NATIVE_CORE") == "1":
            pytest.fail("REQUIRE_NATIVE_CORE=1 and libcarrycore.so is not built")
        pytest.skip("no compiler and no libcarrycore.so")
    return core


def px(basis_bps):
    return SPOT * (1.0 + basis_bps / 1e4)


class FeeVenue:
    def __init__(self, taker):
        self.taker = taker

    def get_fee_rates(self, symbol, product):
        return {"maker_bps": 2.0, "taker_bps": self.taker}

    def place_market(self, **kw):
        raise AssertionError("entry_economics places nothing")


def python_gate(prints, perp, borrow, rt, opp, **kw):
    v = cc.entry_gate(funding_prints_bps=prints, perp=perp, spot=SPOT,
                      borrow_apr=borrow, round_trip_bps=rt,
                      opportunity_cost_apr=opp, **kw)
    return v.allowed, v.reason


def cpp_gate(prints, perp, borrow, rt, opp, **kw):
    p = core.params(borrow_apr=borrow, round_trip_bps=rt, gated=True,
                    overlay=True, opportunity_cost_apr=opp, **kw)
    out = core.entry_gate(prints, perp, SPOT, p)
    return out["allowed"], out["reason"]


def runtime_gate(prints, perp, borrow, rt, opp):
    e = CarryEngine(broker=FeeVenue(rt / 2.0), max_notional_usd=100.0,
                    borrow_apr=borrow, execution_mode=OVERLAY,
                    persist=lambda s: None, opportunity_cost_apr=opp)
    e._funding_history = list(prints)
    econ = e.entry_economics(mark=perp, spot=SPOT, funding_bps=0.0)
    if econ.refusal is None:
        return True, econ.verdict.reason
    return False, econ.refusal.reason


# -- the default changes nothing ----------------------------------------------

def test_the_default_is_zero_and_no_rate_was_invented():
    assert cc.DEFAULT_OPPORTUNITY_COST_APR == 0.0
    import inspect
    for fn in (cc.evaluate_entry, cc.hold_clears_round_trip, cc.entry_gate,
               cb.simulate_settlements_series):
        assert inspect.signature(fn).parameters[
            "opportunity_cost_apr"].default == 0.0, fn.__name__
    assert inspect.signature(CarryEngine.__init__).parameters[
        "opportunity_cost_apr"].default == 0.0
    assert core.params().opportunity_cost_apr == 0.0


def test_zero_reproduces_the_earlier_gate_bit_for_bit():
    rng = random.Random(11)
    for _ in range(3000):
        prints = [rng.uniform(-2, 6) for _ in range(rng.randint(0, 8))]
        kw = dict(perp=px(rng.uniform(-60, 60)), spot=SPOT,
                  borrow_apr=rng.choice([0.0, 0.02, 0.1]),
                  round_trip_bps=rng.choice([11.0, 31.0, 7.0]))
        for fn in (cc.evaluate_entry, cc.entry_gate):
            a = fn(funding_prints_bps=prints, **kw)
            b = fn(funding_prints_bps=prints, opportunity_cost_apr=0.0, **kw)
            assert (a.allowed, a.reason, a.net_edge_bps_per_day,
                    a.basis_budget_bps, a.smoothed_funding_bps) == \
                   (b.allowed, b.reason, b.net_edge_bps_per_day,
                    b.basis_budget_bps, b.smoothed_funding_bps)
            assert b.opportunity_cost_bps_per_day == 0.0


# -- the gate refuses an opportunity cost the funding does not clear -----------

def test_an_opportunity_cost_above_the_funding_edge_refuses(native):
    # 2 bps per 8h is 6 bps/day of carry; 11 bps over 30 days is 0.367/day.
    # Clears at zero cost; an operator stating a 25%/yr alternative return
    # (6.85 bps/day) puts the net edge below zero.
    prints = [2.0] * 5
    assert python_gate(prints, SPOT, 0.0, 11.0, 0.0) == \
        (True, "CARRY_CLEARS_COSTS_AND_BASIS")
    want = (False, "CARRY_BELOW_COST_OF_CAPITAL")
    assert python_gate(prints, SPOT, 0.0, 11.0, 0.25) == want
    assert cpp_gate(prints, SPOT, 0.0, 11.0, 0.25) == want
    assert runtime_gate(prints, SPOT, 0.0, 11.0, 0.25) == want
    v = cc.entry_gate(funding_prints_bps=prints, perp=SPOT, spot=SPOT,
                      borrow_apr=0.0, round_trip_bps=11.0,
                      opportunity_cost_apr=0.25)
    assert v.opportunity_cost_bps_per_day == pytest.approx(0.25 / 365 * 1e4)
    assert v.net_edge_bps_per_day < 0


def test_it_binds_on_the_demonstrated_hold_as_well(native):
    # 1 bps/8h clears the 11 bps round trip in 11 settlements (<= 17) at zero
    # cost. A 5% opportunity cost is 0.457 bps per settlement: net 0.543 needs
    # 21 > 17 settlements, while the 30-day amortised check still passes.
    prints = [1.0] * 5
    assert python_gate(prints, SPOT, 0.0, 11.0, 0.0)[0] is True
    assert cc.evaluate_entry(funding_prints_bps=prints, perp=SPOT, spot=SPOT,
                             borrow_apr=0.0, round_trip_bps=11.0,
                             opportunity_cost_apr=0.05).allowed
    want = (False, "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD")
    assert python_gate(prints, SPOT, 0.0, 11.0, 0.05) == want
    assert cpp_gate(prints, SPOT, 0.0, 11.0, 0.05) == want
    assert runtime_gate(prints, SPOT, 0.0, 11.0, 0.05) == want


def test_opportunity_cost_and_borrow_are_the_same_charge(native):
    """Same units, same treatment: a book charged b of borrow and o of
    opportunity decides exactly as one charged b + o of either."""
    rng = random.Random(5)
    for _ in range(400):
        prints = [rng.uniform(0.0, 4.0)] * 5
        b, o = rng.choice([0.0, 0.01, 0.03]), rng.choice([0.0, 0.02, 0.06])
        basis = rng.choice([-20.0, 0.0, 10.0])
        split = python_gate(prints, px(basis), b, 11.0, o)
        # (continuous random rates never land on the float boundary where
        # b/365 + o/365 and (b+o)/365 could round apart)
        assert split == python_gate(prints, px(basis), b + o, 11.0, 0.0)
        assert split == python_gate(prints, px(basis), 0.0, 11.0, b + o)


# -- four implementations, one answer, away from the default --------------------

GRID = [(rate, basis, borrow, rt, opp)
        for rate in (0.2, 0.5, 0.7, 1.0, 1.5, 2.0, 5.0)
        for basis in (-80.0, 0.0, 25.0)
        for borrow in (0.0, 0.05)
        for rt in (11.0, 31.0)
        for opp in (0.0, 0.01, 0.04, 0.10, 0.5)]


def test_python_runtime_and_cpp_agree_with_an_opportunity_cost(native):
    reasons = set()
    for rate, basis, borrow, rt, opp in GRID:
        prints = [rate] * 5
        py = python_gate(prints, px(basis), borrow, rt, opp)
        rt_ = runtime_gate(prints, px(basis), borrow, rt, opp)
        cp = cpp_gate(prints, px(basis), borrow, rt, opp)
        assert py == rt_ == cp, (rate, basis, borrow, rt, opp, py, rt_, cp)
        reasons.add(py[1])
    assert {"CARRY_CLEARS_COSTS_AND_BASIS", "CARRY_BELOW_COST_OF_CAPITAL",
            "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"} <= reasons


def test_the_break_even_boundary_with_an_opportunity_cost(native):
    # n settlements to clear 11 bps at net f - (opp/365*1e4)/3, walked across
    # the 17-settlement budget; the three implementations draw the same line.
    for opp in (0.01, 0.05, 0.123):
        per_settlement = opp / 365.0 * 1e4 / 3.0
        for n in range(10, 25):
            for nudge in (-1e-7, 1e-9, 1e-6):
                f = 11.0 / n + per_settlement + nudge
                prints = [f] * 5
                a = python_gate(prints, SPOT, 0.0, 11.0, opp)
                b = cpp_gate(prints, SPOT, 0.0, 11.0, opp)
                c = runtime_gate(prints, SPOT, 0.0, 11.0, opp)
                assert a == b == c, (opp, n, nudge, a, b, c)


@pytest.mark.parametrize("opp", [-0.01, float("nan"), float("inf"),
                                 -float("inf")])
def test_an_unpriceable_opportunity_cost_is_refused_everywhere(native, opp):
    py = python_gate([50.0] * 5, SPOT, 0.0, 11.0, opp)
    cp = cpp_gate([50.0] * 5, SPOT, 0.0, 11.0, opp)
    assert py == cp == (False, "COST_INPUT_INVALID")
    v = cc.evaluate_entry(funding_prints_bps=[50.0] * 5, perp=SPOT, spot=SPOT,
                          borrow_apr=0.0, round_trip_bps=11.0,
                          opportunity_cost_apr=opp)
    assert v.detail["fields"] == ["opportunity_cost_apr"]
    h = cc.hold_clears_round_trip(funding_prints_bps=[50.0] * 5,
                                  round_trip_bps=11.0, borrow_apr=0.0,
                                  opportunity_cost_apr=opp)
    assert (h.allowed, h.reason) == (False, "COST_INPUT_INVALID")
    with pytest.raises(ValueError):
        CarryEngine(broker=FeeVenue(5.5), borrow_apr=0.0,
                    execution_mode=OVERLAY, persist=lambda s: None,
                    opportunity_cost_apr=opp)


@pytest.mark.parametrize("opp", [None, "5%", True])
def test_the_engine_refuses_a_non_number(opp):
    with pytest.raises(ValueError):
        CarryEngine(broker=FeeVenue(5.5), borrow_apr=0.0,
                    execution_mode=OVERLAY, persist=lambda s: None,
                    opportunity_cost_apr=opp)


def test_an_overflowing_opportunity_cost_refuses_identically(native):
    for opp in (1e306, 1.7976931348623157e308):
        py = python_gate([5.0] * 5, SPOT, 0.0, 11.0, opp)
        cp = cpp_gate([5.0] * 5, SPOT, 0.0, 11.0, opp)
        assert py == cp and py[0] is False, (opp, py, cp)


def test_it_only_ever_refuses_more():
    rng = random.Random(3)
    tightened = 0
    for _ in range(3000):
        prints = [rng.uniform(-1, 5) for _ in range(rng.randint(3, 8))]
        kw = dict(perp=px(rng.uniform(-40, 40)), spot=SPOT,
                  borrow_apr=rng.choice([0.0, 0.03]),
                  round_trip_bps=rng.choice([11.0, 31.0]))
        base = cc.entry_gate(funding_prints_bps=prints, **kw)
        charged = cc.entry_gate(funding_prints_bps=prints,
                                opportunity_cost_apr=rng.choice([0.01, 0.05,
                                                                 0.2]), **kw)
        assert not (charged.allowed and not base.allowed)
        tightened += base.allowed and not charged.allowed
    assert tightened > 20


# -- the simulators, on the real corpus, to the cent -----------------------------

@pytest.fixture(scope="module")
def rows():
    return cb.load_bybit_settlements(BOT)[0]


def both(rows, *, opp, borrow=0.0, **knobs):
    python = cb.simulate_settlements_series(
        rows, borrow_apr=borrow, gated=True, impact_bps=1.093, mode=cb.OVERLAY,
        opportunity_cost_apr=opp, **knobs)
    params = core.params(
        borrow_apr=borrow, impact_bps=1.093, gated=True, overlay=True,
        round_trip_bps=cb.round_trip_bps_for(cb.OVERLAY),
        taker_spot_bps=cb.TAKER_BPS_SPOT, taker_perp_bps=cb.TAKER_BPS_PERP,
        opportunity_cost_apr=opp, **knobs)
    return python, core.simulate(rows, params)


def test_the_simulator_default_is_unchanged(rows):
    implicit = cb.simulate_settlements_series(rows, borrow_apr=0.0, gated=True,
                                              impact_bps=1.093, mode=cb.OVERLAY)
    explicit = cb.simulate_settlements_series(rows, borrow_apr=0.0, gated=True,
                                              impact_bps=1.093, mode=cb.OVERLAY,
                                              opportunity_cost_apr=0.0)
    assert implicit["attribution"] == explicit["attribution"]
    assert implicit["refusals"] == explicit["refusals"]
    assert implicit["opportunity_cost_usd"] == 0.0
    assert implicit["net_after_opportunity_cost_usd"] == implicit["net_usd"]


@pytest.mark.parametrize("opp,knobs", [
    (0.0, {}), (0.02, {}), (0.05, {}), (0.12, {}),
    (0.05, dict(demonstrated_hold_days=8.0, hold_days=20.0)),
    (0.03, dict(funding_history=5, ewma_alpha=0.3)),
])
def test_python_and_cpp_simulators_agree_to_the_cent(native, rows, opp, knobs):
    for borrow in (0.0, 0.05):
        python, cpp = both(rows, opp=opp, borrow=borrow, **knobs)
        a = python["attribution"]
        label = (opp, borrow, knobs)
        assert cpp["trades"] == python["trades"], label
        assert cpp["closed_trades"] == python["closed_trades"], label
        assert cpp["net"] == pytest.approx(a["net_usd"], abs=CENT), label
        assert cpp["borrow"] == pytest.approx(-a["borrow_usd"], abs=CENT), label
        assert cpp["opportunity"] == pytest.approx(
            -python["opportunity_cost_usd"], abs=CENT), label
        by = dict(zip(cc.GATE_REASONS, cpp["refusals_by_reason"]))
        assert {k: v for k, v in by.items() if v} == python["refusals"], label
        if opp == 0.0:
            assert cpp["opportunity"] == 0.0


def test_a_stated_opportunity_cost_moves_the_simulators(native, rows):
    zero, _ = both(rows, opp=0.0)
    high, high_cpp = both(rows, opp=0.12)
    assert high["trades"] < zero["trades"]
    assert high["refusals"].get("CARRY_BELOW_COST_OF_CAPITAL", 0) + \
        high["refusals"].get("BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD", 0) > \
        zero["refusals"].get("CARRY_BELOW_COST_OF_CAPITAL", 0) + \
        zero["refusals"].get("BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD", 0)
    assert high["opportunity_cost_usd"] < 0
    assert high["net_after_opportunity_cost_usd"] == pytest.approx(
        high["net_usd"] + high["opportunity_cost_usd"])


def test_the_python_simulator_refuses_a_nonsense_value(rows):
    for bad in (-0.01, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            cb.simulate_settlements_series(rows[:20], gated=True,
                                           mode=cb.OVERLAY,
                                           opportunity_cost_apr=bad)


# -- the replay hands it to the real engine ---------------------------------------

def _constant_rows(rate_bps, n=12):
    return [cb.Settlement(ms=(i + 1) * H8, perp=SPOT, spot=SPOT,
                          rate=rate_bps / 1e4, perp_high=SPOT, perp_low=SPOT)
            for i in range(n)]


def test_the_replay_engine_decides_with_the_same_gate():
    import carry_replay as cr
    rows = _constant_rows(2.0)
    zero = cr.replay(rows, notional=1_000.0, borrow_apr=0.0)
    implicit_zero = cr.replay(rows, notional=1_000.0, borrow_apr=0.0,
                              opportunity_cost_apr=0.0)
    assert zero["trades"] == implicit_zero["trades"] >= 1
    assert zero["net_usd"] == implicit_zero["net_usd"]
    assert zero["opportunity_cost_usd"] == 0.0
    # The engine inside the replay refuses exactly what the gate refuses.
    want = python_gate([2.0] * 5, SPOT, 0.0, 11.0, 0.25)
    assert want == (False, "CARRY_BELOW_COST_OF_CAPITAL")
    high = cr.replay(rows, notional=1_000.0, borrow_apr=0.0,
                     opportunity_cost_apr=0.25)
    assert high["trades"] == 0
    assert high["reasons"].get("CARRY_BELOW_COST_OF_CAPITAL", 0) > 0
    # and a cost that still clears charges per held print, outside net_usd
    mid = cr.replay(rows, notional=1_000.0, borrow_apr=0.0,
                    opportunity_cost_apr=0.01)
    assert mid["trades"] == zero["trades"]
    assert mid["net_usd"] == pytest.approx(zero["net_usd"])
    assert mid["opportunity_cost_usd"] < 0
    assert mid["net_after_opportunity_cost_usd"] == pytest.approx(
        mid["net_usd"] + mid["opportunity_cost_usd"])
    assert mid["ledger_agrees"] is True


def test_the_thirty_day_assumption_cannot_approve_in_any_implementation(native):
    """The headline requirement, on every path that decides an entry: 0.3 bps
    per 8h clears the round trip amortised over ASSUMED_HOLD_DAYS (30) but
    needs 37 settlements against the 17 a DEMONSTRATED_HOLD_DAYS hold has.
    The Python gate, the runtime engine, the replay (the engine driven by
    main.tick's sequence), the Python simulator and the C++ core all refuse,
    for the same reason."""
    import carry_replay as cr
    reason = "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"
    assert cc.evaluate_entry(funding_prints_bps=[0.3] * 5, perp=SPOT,
                             spot=SPOT, borrow_apr=0.0,
                             round_trip_bps=11.0).allowed
    assert python_gate([0.3] * 5, SPOT, 0.0, 11.0, 0.0) == (False, reason)
    assert runtime_gate([0.3] * 5, SPOT, 0.0, 11.0, 0.0) == (False, reason)
    assert cpp_gate([0.3] * 5, SPOT, 0.0, 11.0, 0.0) == (False, reason)
    rows = _constant_rows(0.3, n=14)
    replayed = cr.replay(rows, notional=1_000.0, borrow_apr=0.0)
    assert replayed["trades"] == 0
    assert replayed["reasons"].get(reason, 0) > 0
    sim = cb.simulate_settlements_series(rows, borrow_apr=0.0, gated=True,
                                         mode=cb.OVERLAY)
    assert sim["trades"] == 0 and sim["refusals"].get(reason, 0) > 0
    p = core.params(borrow_apr=0.0, gated=True, overlay=True,
                    round_trip_bps=11.0)
    cpp = core.simulate(rows, p)
    assert cpp["trades"] == 0
    assert cpp["refusals_by_reason"][cc.GATE_REASONS.index(reason)] == \
        sim["refusals"][reason]
