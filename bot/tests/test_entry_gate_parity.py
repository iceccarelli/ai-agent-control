"""One entry decision, three implementations, identical answers.

`carry_costs.entry_gate` (evaluate_entry, then the measured-hold check) is what
`CarryEngine.entry_economics` asks, what both `carry_backtest` simulators ask,
and what `cpp/carrycore.cpp` reimplements. These tests feed IDENTICAL inputs to

  * the Python function (`carry_costs.entry_gate`),
  * the runtime engine (`CarryEngine.entry_economics`, the method `on_candle`
    returns verbatim on a refusal),
  * the Python settlement simulator (`carry_backtest.simulate_settlements_series`),
  * the C++ core (`carry_entry_gate` and `carry_simulate`),

and require the same accept/refuse and the same reason code. The hold check only
ever REFUSES more than `evaluate_entry` does; nothing here may be loosened to
make a replay look better, and the constants are asserted frozen.
"""
from __future__ import annotations

import math
import os
import random
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import carry_backtest as cb  # noqa: E402
import carry_core as core  # noqa: E402
import carry_costs as cc  # noqa: E402
from carry_engine import OVERLAY, CarryEngine  # noqa: E402

H8 = 8 * 3600 * 1000
SPOT = 100_000.0


@pytest.fixture(scope="module")
def native():
    built = core.build()
    if not built and not core.available():
        if os.environ.get("REQUIRE_NATIVE_CORE") == "1":
            pytest.fail("REQUIRE_NATIVE_CORE=1 and libcarrycore.so is not built: "
                        "the C++ half of the entry-gate parity cannot be skipped")
        pytest.skip("no compiler and no libcarrycore.so")
    return core


def px(basis_bps):
    return SPOT * (1.0 + basis_bps / 1e4)


class Venue:
    """Only the fee table is read by entry_economics. It can place nothing."""

    def __init__(self, taker=5.5):
        self.taker = taker
        self.orders = []

    def get_fee_rates(self, symbol, product):
        return {"maker_bps": 2.0, "taker_bps": self.taker}

    def place_market(self, **kw):
        self.orders.append(kw)
        raise AssertionError("a refused entry must not reach the venue")


def runtime(prints, perp, spot, borrow, rt):
    e = CarryEngine(broker=Venue(taker=rt / 2.0), max_notional_usd=100.0,
                    borrow_apr=borrow, execution_mode=OVERLAY,
                    persist=lambda s: None)
    e._funding_history = list(prints)
    econ = e.entry_economics(mark=perp, spot=spot, funding_bps=0.0)
    if econ.refusal is None:
        return True, econ.verdict.reason
    return False, econ.refusal.reason


def python_gate(prints, perp, spot, borrow, rt, hold=cc.ASSUMED_HOLD_DAYS,
                demo=cc.DEMONSTRATED_HOLD_DAYS):
    v = cc.entry_gate(funding_prints_bps=prints, perp=perp, spot=spot,
                      borrow_apr=borrow, round_trip_bps=rt, hold_days=hold,
                      demonstrated_hold_days=demo)
    return v.allowed, v.reason


def cpp_gate(prints, perp, spot, borrow, rt, hold=cc.ASSUMED_HOLD_DAYS,
             demo=cc.DEMONSTRATED_HOLD_DAYS):
    p = core.params(borrow_apr=borrow, round_trip_bps=rt, hold_days=hold,
                    gated=True, overlay=True, demonstrated_hold_days=demo)
    out = core.entry_gate(prints, perp, spot, p)
    return out["allowed"], out["reason"]


def sim_rows(rate_bps, basis_bps, n=14):
    return [cb.Settlement(ms=i * H8, perp=px(basis_bps), spot=SPOT,
                          rate=rate_bps / 1e4, perp_high=px(basis_bps),
                          perp_low=px(basis_bps)) for i in range(n)]


def simulators(rate_bps, basis_bps, borrow, mode, n=14):
    """(python result, c++ result) on a constant series: the first row where
    the EWMA has three prints is the first real decision, and every later
    decision on the same constant inputs is the same one."""
    rows = sim_rows(rate_bps, basis_bps, n)
    py = cb.simulate_settlements_series(
        rows, notional=100_000.0, borrow_apr=borrow, gated=True,
        impact_bps=0.0, mode=mode)
    p = core.params(notional=100_000.0, borrow_apr=borrow, impact_bps=0.0,
                    round_trip_bps=cb.round_trip_bps_for(mode), gated=True,
                    overlay=(mode == cb.OVERLAY),
                    taker_spot_bps=cb.TAKER_BPS_SPOT,
                    taker_perp_bps=cb.TAKER_BPS_PERP)
    return py, core.simulate(rows, p)


# -- the constants this change must not touch -------------------------------

def test_no_economic_parameter_moved():
    assert cc.ASSUMED_HOLD_DAYS == 30.0
    assert cc.DEMONSTRATED_HOLD_DAYS == 5.8
    assert (cc.TAKER_BPS_SPOT, cc.TAKER_BPS_PERP) == (10.0, 5.5)
    assert cc.FUNDING_PERIODS_PER_DAY == 3
    assert (cc.EWMA_ALPHA, cc.EWMA_MIN_PRINTS) == (0.5, 3)
    assert cb.round_trip_bps_for(cb.OVERLAY) == 11.0
    assert cb.round_trip_bps_for(cb.ACQUIRE) == 31.0
    assert CarryEngine.FUNDING_HISTORY == 8


def test_reason_table_matches_the_native_library(native):
    # the code numbers are the C++ enum (carrycore.h); a reorder breaks both
    assert cc.GATE_REASONS == (
        "CARRY_CLEARS_COSTS_AND_BASIS", "COST_INPUT_INVALID",
        "INSUFFICIENT_FUNDING_HISTORY", "FUNDING_NOT_POSITIVE",
        "BASIS_UNREADABLE", "CARRY_BELOW_COST_OF_CAPITAL",
        "ENTRY_BASIS_EXCEEDS_CARRY_BUDGET",
        "ENTRY_BASIS_DISCOUNT_EXCEEDS_CARRY_BUDGET",
        "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD")
    assert core.GATE_REASONS == len(cc.GATE_REASONS) == 9
    assert core.version().startswith("carrycore/5")
    # and each name is what the C++ returns for an input that produces it
    probes = {
        "COST_INPUT_INVALID": dict(prints=[5.0] * 5, rt=0.0),
        "INSUFFICIENT_FUNDING_HISTORY": dict(prints=[5.0]),
        "FUNDING_NOT_POSITIVE": dict(prints=[-1.0] * 5),
        "BASIS_UNREADABLE": dict(prints=[5.0] * 5, perp=0.0),
        "CARRY_BELOW_COST_OF_CAPITAL": dict(prints=[0.1] * 5),
        "ENTRY_BASIS_EXCEEDS_CARRY_BUDGET": dict(prints=[1.0] * 5, basis=900.0),
        "ENTRY_BASIS_DISCOUNT_EXCEEDS_CARRY_BUDGET":
            dict(prints=[1.0] * 5, basis=-900.0),
        "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD": dict(prints=[0.3] * 5),
        "CARRY_CLEARS_COSTS_AND_BASIS": dict(prints=[2.0] * 5),
    }
    for want, kw in probes.items():
        perp = kw.get("perp", px(kw.get("basis", 0.0)))
        got = cpp_gate(kw["prints"], perp, SPOT, 0.0, kw.get("rt", 11.0))
        assert got[1] == want, (want, got)
        assert python_gate(kw["prints"], perp, SPOT, 0.0, kw.get("rt", 11.0)) == got


def test_a_demonstrated_hold_with_no_finite_settlement_count_is_refused(native):
    for demo in (1e308, 6e307, 1.7976931348623157e308):
        py = python_gate([1.0] * 5, SPOT, SPOT, 0.0, 31.0, demo=demo)
        cp = cpp_gate([1.0] * 5, SPOT, SPOT, 0.0, 31.0, demo=demo)
        assert py == cp == (False, "COST_INPUT_INVALID"), (demo, py, cp)


def test_the_wrapper_refuses_a_different_smoother(native):
    p = core.params(gated=True, round_trip_bps=11.0, ewma_min_prints=1)
    with pytest.raises(ValueError):
        core.entry_gate([5.0], SPOT, SPOT, p)


def test_the_abi_check_tells_a_stale_library_from_the_current_one(native, tmp_path,
                                                                  monkeypatch):
    """make compares mtimes only, so an old-ABI .so touched after checkout is
    left alone; build() therefore asks the library itself and clean-rebuilds.
    This pins the question it asks: exact name, so carrycore/50, /4 or /3 fail."""
    import shutil
    import subprocess
    if shutil.which("g++") is None:
        pytest.skip("no compiler")
    assert core._abi_current() is True
    for name in ("carrycore/4 (stale)", "carrycore/3 (stale)",
                 "carrycore/50 (future)"):
        src = tmp_path / "v.cpp"
        src.write_text('extern "C" const char *carrycore_version(void)'
                       '{return "%s";}\n' % name)
        lib = tmp_path / ("lib%d.so" % len(name))
        subprocess.run(["g++", "-shared", "-fPIC", "-o", str(lib), str(src)],
                       check=True)
        monkeypatch.setattr(core, "LIB_PATH", str(lib))
        assert core._abi_current() is False, name


# -- the headline case and the boundary ---------------------------------------

def test_the_gate_only_refuses_more_it_never_admits_what_evaluate_entry_refused():
    rng = random.Random(7)
    extra = 0
    for _ in range(3000):
        prints = [rng.uniform(-2, 6) for _ in range(rng.randint(0, 8))]
        kw = dict(perp=px(rng.uniform(-60, 60)), spot=SPOT,
                  borrow_apr=rng.choice([0.0, 0.02, 0.1]),
                  round_trip_bps=rng.choice([11.0, 31.0, 7.0]))
        base = cc.evaluate_entry(funding_prints_bps=prints, **kw)
        gate = cc.entry_gate(funding_prints_bps=prints, **kw)
        assert not (gate.allowed and not base.allowed)
        if base.allowed != gate.allowed:
            extra += 1
            assert gate.reason == "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"
        else:
            assert gate.reason == base.reason
    assert extra > 50                        # the refinement actually binds


def test_clears_the_thirty_day_cost_but_not_the_measured_hold():
    prints = [0.3] * 5
    assert cc.evaluate_entry(funding_prints_bps=prints, perp=SPOT, spot=SPOT,
                             borrow_apr=0.0, round_trip_bps=11.0).allowed
    want = (False, "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD")
    assert python_gate(prints, SPOT, SPOT, 0.0, 11.0) == want
    assert runtime(prints, SPOT, SPOT, 0.0, 11.0) == want


def test_sufficient_under_both_gates():
    prints = [1.0] * 5                       # 11 settlements <= 17
    want = (True, "CARRY_CLEARS_COSTS_AND_BASIS")
    assert python_gate(prints, SPOT, SPOT, 0.0, 11.0) == want
    assert runtime(prints, SPOT, SPOT, 0.0, 11.0) == want


# -- three-way agreement on a grid ---------------------------------------------

GRID = [(rate, basis, borrow, rt)
        for rate in (-1.0, 0.0, 0.2, 0.3, 0.5, 0.6, 0.65, 0.7, 1.0, 1.9, 2.0, 5.0)
        for basis in (-80.0, -10.0, 0.0, 3.0, 25.0, 150.0)
        for borrow in (0.0, 0.02, 0.10)
        for rt in (11.0, 31.0, 7.5)]


def test_python_runtime_and_cpp_agree_on_every_grid_point(native):
    seen = set()
    for rate, basis, borrow, rt in GRID:
        prints = [rate] * 5
        py = python_gate(prints, px(basis), SPOT, borrow, rt)
        rt_ = runtime(prints, px(basis), SPOT, borrow, rt)
        cp = cpp_gate(prints, px(basis), SPOT, borrow, rt)
        assert py == rt_ == cp, (rate, basis, borrow, rt, py, rt_, cp)
        seen.add(py)
    reasons = {r for _a, r in seen}
    # the grid reaches every refusal the gate has, and the allow
    assert reasons == set(cc.GATE_REASONS) - {"COST_INPUT_INVALID",
                                              "INSUFFICIENT_FUNDING_HISTORY",
                                              "BASIS_UNREADABLE"}


@pytest.mark.parametrize("mode", [cb.OVERLAY, cb.ACQUIRE])
def test_both_simulators_make_the_same_decision_as_the_gate(native, mode):
    rt = cb.round_trip_bps_for(mode)
    for rate, basis, borrow, _ in GRID[::7]:
        want_ok, want_reason = python_gate([rate] * 5, px(basis), SPOT, borrow, rt)
        py, cpp = simulators(rate, basis, borrow, mode)
        assert (py["trades"] > 0) == want_ok == (cpp["trades"] > 0), (rate, basis)
        if want_ok:
            assert py["refusals"] == {"INSUFFICIENT_FUNDING_HISTORY": 2}
            by = dict(zip(cc.GATE_REASONS, cpp["refusals_by_reason"]))
            assert {k: v for k, v in by.items() if v} == py["refusals"]
        else:
            assert py["refusals"] == {"INSUFFICIENT_FUNDING_HISTORY": 2,
                                      want_reason: py["refusals"][want_reason]}
            by = dict(zip(cc.GATE_REASONS, cpp["refusals_by_reason"]))
            nonzero = {k: v for k, v in by.items() if v}
            assert nonzero == py["refusals"], (rate, basis, borrow, mode)


def test_the_simulators_count_the_hold_refusal_under_its_own_reason(native):
    py, cpp = simulators(0.3, 0.0, 0.0, cb.OVERLAY)
    assert py["trades"] == 0 and cpp["trades"] == 0
    assert py["refusals"]["BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"] == 12
    assert cpp["refusals_by_reason"][8] == 12


# -- the settlement-budget boundary ---------------------------------------------

def test_funding_exactly_on_the_settlement_budget_boundary(native):
    """17 settlements = floor(5.8 * 3). Funding of 11/17 bps clears the round
    trip in exactly 17 settlements. `min_settlements_to_clear` rounds the
    quotient to 9 places so float noise (11 / 1.0000000000000002) cannot cost a
    settlement, so one ulp below the edge is still 17; a real shortfall of 1e-9
    (relative) needs 18 and is refused. The three implementations must draw the
    line in the same place, ties and noise included."""
    edge = 11.0 / 17.0
    for f, ok in ((edge, True), (math.nextafter(edge, 0.0), True),
                  (math.nextafter(edge, 1.0), True), (edge * (1 - 1e-9), False),
                  (edge * (1 + 1e-9), True), (edge * (1 - 1e-7), False),
                  (edge * 0.5, False)):
        prints = [f] * 5
        got = {python_gate(prints, SPOT, SPOT, 0.0, 11.0)[0],
               runtime(prints, SPOT, SPOT, 0.0, 11.0)[0],
               cpp_gate(prints, SPOT, SPOT, 0.0, 11.0)[0]}
        assert got == {ok}, (f, got)


def test_boundary_with_borrow_and_a_fractional_hold(native):
    # a 5.9 day demonstrated hold has a budget of 17 settlements, 6.0 has 18
    for demo, budget in ((5.8, 17), (5.9, 17), (6.0, 18), (0.9, 2), (0.3, 0)):
        for borrow in (0.0, 0.05):
            net_per_settlement = lambda f: f - borrow / 365.0 * 1e4 / 3
            for n in range(1, 25):
                f = 11.0 / n + borrow / 365.0 * 1e4 / 3 + 1e-9
                assert net_per_settlement(f) > 0
                prints = [f] * 5
                a = python_gate(prints, SPOT, SPOT, borrow, 11.0, demo=demo)
                b = cpp_gate(prints, SPOT, SPOT, borrow, 11.0, demo=demo)
                assert a == b, (demo, borrow, n, a, b)
                # n settlements to clear: allowed exactly when n <= budget (the
                # entry is otherwise unremarkable, so no other reason may appear)
                assert a[1] == ("CARRY_CLEARS_COSTS_AND_BASIS" if n <= budget
                                else "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"), \
                    (demo, borrow, n, a)


def _ulps(x, k):
    for _ in range(abs(k)):
        x = math.nextafter(x, math.inf if k > 0 else -math.inf)
    return x


def test_rounding_ties_in_the_break_even_arithmetic_agree(native):
    """`min_settlements_to_clear` rounds the net funding to 12 places and the
    settlement quotient to 9, ties to even on the exact binary value. A C++
    `floor(x * 10^d + 0.5) / 10^d` differs from that on exactly these values,
    so the points are built ON the ties and walked a few ulps either side."""
    checked = refused = allowed = 0
    # quotient ties: rt / net = n + 5e-10
    for n in range(1, 31):
        x0 = n + 5e-10
        for j in range(-60, 61):
            f = 11.0 / _ulps(x0, j)
            a = python_gate([f] * 5, SPOT, SPOT, 0.0, 11.0)
            b = cpp_gate([f] * 5, SPOT, SPOT, 0.0, 11.0)
            assert a == b, (n, j, f, a, b)
            checked += 1
            allowed += a[0]
            refused += not a[0]
    # net ties: net = (m + 0.5) * 1e-12
    for m in (int(11.0 / 17.0 * 1e12), int(11.0 / 11.0 * 1e12),
              int(11.0 / 30.0 * 1e12), int(2.5 * 1e12)):
        f0 = (m + 0.5) * 1e-12
        for j in range(-60, 61):
            f = _ulps(f0, j)
            a = python_gate([f] * 5, SPOT, SPOT, 0.0, 11.0)
            b = cpp_gate([f] * 5, SPOT, SPOT, 0.0, 11.0)
            assert a == b, (m, j, f, a, b)
            checked += 1
    # the budget: hold * 3 near an integer
    for k in range(1, 40):
        for j in range(-30, 31):
            demo = _ulps(k / 3.0, j)
            f = 11.0 / k + 1e-9
            a = python_gate([f] * 5, SPOT, SPOT, 0.0, 11.0, demo=demo)
            b = cpp_gate([f] * 5, SPOT, SPOT, 0.0, 11.0, demo=demo)
            assert a == b, (k, j, demo, a, b)
            checked += 1
    assert checked > 2000 and allowed > 100 and refused > 100


# -- unusable inputs fail closed, identically ------------------------------------

NAN, INF = float("nan"), float("inf")


@pytest.mark.parametrize("kw", [
    dict(rt=0.0), dict(rt=-11.0), dict(rt=NAN), dict(rt=INF),
    dict(borrow=-0.01), dict(borrow=NAN), dict(borrow=INF),
    dict(hold=0.0), dict(hold=-3.0), dict(hold=NAN),
    dict(demo=0.0), dict(demo=-5.8), dict(demo=NAN), dict(demo=INF),
])
def test_unusable_cost_inputs_are_refused_everywhere(native, kw):
    args = dict(prints=[50.0] * 5, perp=SPOT, spot=SPOT, borrow=0.0, rt=11.0)
    args.update({k: v for k, v in kw.items() if k in args})
    extra = {k: v for k, v in kw.items() if k in ("hold", "demo")}
    py = python_gate(args["prints"], args["perp"], args["spot"], args["borrow"],
                     args["rt"], **extra)
    cp = cpp_gate(args["prints"], args["perp"], args["spot"], args["borrow"],
                  args["rt"], **extra)
    assert py == cp
    assert py[0] is False and py[1] == "COST_INPUT_INVALID", (kw, py)
    if not extra:
        # the engine reads the round trip from the fee table: 2 x perp taker
        assert runtime(args["prints"], args["perp"], args["spot"],
                       args["borrow"], args["rt"]) == py


@pytest.mark.parametrize("prints,want", [
    ([], "INSUFFICIENT_FUNDING_HISTORY"),
    ([50.0, 50.0], "INSUFFICIENT_FUNDING_HISTORY"),
    ([NAN, NAN, NAN, 50.0], "INSUFFICIENT_FUNDING_HISTORY"),
    ([INF, -INF, NAN], "INSUFFICIENT_FUNDING_HISTORY"),
    ([0.0] * 5, "FUNDING_NOT_POSITIVE"),
    ([-1.0] * 5, "FUNDING_NOT_POSITIVE"),
    ([NAN, 50.0, 50.0, 50.0], "CARRY_CLEARS_COSTS_AND_BASIS"),
])
def test_missing_or_nonfinite_funding(native, prints, want):
    py = python_gate(prints, SPOT, SPOT, 0.0, 11.0)
    assert py == cpp_gate(prints, SPOT, SPOT, 0.0, 11.0)
    assert py[1] == want
    finite = [p for p in prints if math.isfinite(p)]
    assert runtime(finite, SPOT, SPOT, 0.0, 11.0)[1] == want


@pytest.mark.parametrize("perp,spot", [
    (NAN, SPOT), (SPOT, NAN), (INF, SPOT), (SPOT, INF), (0.0, SPOT),
    (-SPOT, SPOT), (SPOT, 0.0), (SPOT, -SPOT)])
def test_an_unreadable_price_is_basis_unreadable_everywhere(native, perp, spot):
    # funding large enough that a zero or negative perp WOULD have budgeted
    prints = [1000.0] * 5
    assert python_gate(prints, perp, spot, 0.0, 11.0) == (False, "BASIS_UNREADABLE")
    assert cpp_gate(prints, perp, spot, 0.0, 11.0) == (False, "BASIS_UNREADABLE")


def test_fuzz_python_and_cpp_never_disagree(native):
    rng = random.Random(20261025)
    specials = [NAN, INF, -INF, 0.0, -0.0, 1e-12, -1e-12, 1e12]
    n_allowed = n_hold = 0
    for _ in range(40000):
        prints = [rng.choice(specials) if rng.random() < 0.06
                  else rng.uniform(-1.0, 8.0) * rng.choice([0.1, 1, 1])
                  for _ in range(rng.choice([0, 1, 2, 3, 3, 4, 5, 8]))]

        def pick(lo, hi, bad_rate=0.03):
            return rng.choice(specials) if rng.random() < bad_rate else rng.uniform(lo, hi)
        perp = px(rng.uniform(-300, 300)) if rng.random() > 0.04 else pick(-5, 5)
        spot = SPOT if rng.random() > 0.04 else pick(-5, 5)
        borrow = pick(0.0, 0.4)
        rt = pick(1.0, 45.0)
        hold = pick(1.0, 60.0)
        demo = pick(0.1, 12.0)
        a = python_gate(prints, perp, spot, borrow, rt, hold, demo)
        b = cpp_gate(prints, perp, spot, borrow, rt, hold, demo)
        assert a == b, (prints, perp, spot, borrow, rt, hold, demo, a, b)
        n_allowed += a[0]
        n_hold += a[1] == "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"
    assert n_allowed > 500 and n_hold > 500       # both branches were exercised


# -- a series that binds the gate: latency and settlements at the boundaries -----

def churn_rows(n=240, seed=3):
    """Funding that wanders around the gate's boundaries, a basis that moves,
    and a negative run now and then so entries, exits and re-entries all occur."""
    rng = random.Random(seed)
    rows, rate, basis = [], 0.8, 0.0
    for i in range(n):
        rate = max(-1.5, min(3.0, rate + rng.uniform(-0.45, 0.45)
                             + (0.25 if rate < 0.3 else 0.0)))
        basis = max(-40.0, min(60.0, basis + rng.uniform(-6, 6)))
        perp = px(basis + 3.0 * math.sin(i / 7.0))
        rows.append(cb.Settlement(ms=i * H8, perp=perp, spot=SPOT,
                                  rate=rate / 1e4, perp_high=perp * 1.001,
                                  perp_low=perp * 0.999))
    return rows


@pytest.mark.parametrize("latency", [0, 1, 2, 3])
@pytest.mark.parametrize("mode", [cb.OVERLAY, cb.ACQUIRE])
@pytest.mark.parametrize("borrow", [0.0, 0.05])
def test_simulators_agree_to_the_cent_with_the_gate_binding(native, latency,
                                                           mode, borrow):
    rows = churn_rows()
    py = cb.simulate_settlements_series(
        rows, notional=100_000.0, borrow_apr=borrow, gated=True,
        impact_bps=1.093, mode=mode, latency_rows=latency)
    p = core.params(notional=100_000.0, borrow_apr=borrow, impact_bps=1.093,
                    round_trip_bps=cb.round_trip_bps_for(mode), gated=True,
                    overlay=(mode == cb.OVERLAY),
                    taker_spot_bps=cb.TAKER_BPS_SPOT,
                    taker_perp_bps=cb.TAKER_BPS_PERP, latency_rows=latency)
    cpp = core.simulate(rows, p)
    a = py["attribution"]
    assert cpp["trades"] == py["trades"]
    assert cpp["net"] == pytest.approx(a["net_usd"], abs=0.01)
    assert cpp["funding"] == pytest.approx(a["funding_usd"], abs=0.01)
    assert cpp["fees"] == pytest.approx(-a["fees_usd"], abs=0.01)
    assert cpp["refusals"] == sum(py["refusals"].values())
    by = {k: v for k, v in zip(cc.GATE_REASONS, cpp["refusals_by_reason"]) if v}
    assert by == py["refusals"]
    assert py["refusals"].get("BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD", 0) > 0
    assert py["trades"] > 0


def cycle_rows(cycles=8):
    """Funding that cycles: a stretch too thin to clear the measured hold, a
    stretch that clears it, a negative run that ends the position. Exercises
    refusal, entry, hold, exit and re-entry through the real engine."""
    rows, i = [], 0
    plan = ([0.35] * 12 + [1.4] * 22 + [-0.8] * 4 + [0.6] * 3)
    for _ in range(cycles):
        for rate in plan:
            perp = px(4.0 * math.sin(i / 5.0))
            rows.append(cb.Settlement(ms=i * H8, perp=perp, spot=SPOT,
                                      rate=rate / 1e4, perp_high=perp * 1.001,
                                      perp_low=perp * 0.999))
            i += 1
    return rows


def test_the_real_engine_replay_agrees_with_both_simulators(native):
    """The engine is driven bar by bar through `carry_replay` (the same code
    path as production's tick), at the decision-row fill the replay uses
    (latency 0), and must make the same trades and the same refusals as the
    Python settlement simulator and the C++ core."""
    import carry_replay as rp
    rows = cycle_rows()
    eng = rp.replay(rows, notional=100_000.0, borrow_apr=0.0, impact_bps=0.0)
    sim = cb.simulate_settlements_series(
        rows, notional=100_000.0, borrow_apr=0.0, gated=True, impact_bps=0.0,
        mode=cb.OVERLAY, latency_rows=0)
    p = core.params(notional=100_000.0, borrow_apr=0.0, impact_bps=0.0,
                    round_trip_bps=11.0, gated=True, overlay=True,
                    latency_rows=0)
    cpp = core.simulate(rows, p)
    assert eng["trades"] == sim["trades"] == cpp["trades"]
    assert eng["trades"] >= 6
    hold = "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"
    assert eng["reasons"].get(hold, 0) == sim["refusals"][hold] \
        == cpp["refusals_by_reason"][8]
    assert sim["refusals"][hold] >= 10
    assert eng["reasons"].get("INSUFFICIENT_FUNDING_HISTORY", 0) \
        == sim["refusals"].get("INSUFFICIENT_FUNDING_HISTORY", 0) \
        == cpp["refusals_by_reason"][2]
    # the engine snaps lots to the venue's step; the residual is that, not a
    # different decision
    assert eng["net_usd"] == pytest.approx(sim["net_usd"], rel=0.02)
    assert cpp["net"] == pytest.approx(sim["net_usd"], abs=0.01)


# -- the runtime: refusal reaches no venue, survives a restart -------------------

H = 8 * 3600 * 1000


class OrderVenue(Venue):
    """Fee table plus the pieces on_candle touches before it would order."""

    inventory = 1.0

    def place_market(self, *, symbol, side, qty, product):
        self.orders.append((side, product, qty))
        return {"filled_qty": qty, "avg_price": SPOT, "order_link_id": "x",
                "fee": 0.0001}

    def get_margin_multiple(self, symbol):
        return 5.0

    def get_mark(self, symbol):
        return SPOT

    def get_lot_rules(self, symbol, product):
        return {"qty_step": 0.001, "min_qty": 0.001, "min_notional": 5.0}

    def get_spot_inventory(self, symbol):
        return self.inventory


def live_engine(venue, rate):
    from carry_risk import CarryRisk
    from market_snapshot import MarketSnapshot
    e = CarryEngine(broker=venue, max_notional_usd=100.0, borrow_apr=0.0,
                    execution_mode=OVERLAY, persist=lambda s: None)
    e.pair_risk = CarryRisk(max_notional_usd=100.0)
    e.snapshot = MarketSnapshot(perp_mark=SPOT, spot_mark=SPOT, funding_bps=rate,
                                margin_multiple=5.0, observed_at_s=time.time())
    return e


def tick(e, k, rate):
    return e.on_candle(mark=SPOT, funding_bps=rate, spot=SPOT,
                       funding_print_ms=k * H)


def test_a_refused_entry_sends_nothing_and_keeps_refusing():
    venue = OrderVenue()
    e = live_engine(venue, 0.3)
    decisions = [tick(e, k, 0.3) for k in range(1, 8)]
    reasons = [d.reason for d in decisions]
    assert reasons[:2] == ["INSUFFICIENT_FUNDING_HISTORY"] * 2
    assert set(reasons[2:]) == {"BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"}
    assert all(d.action == "stand_aside" and not d.acted for d in decisions)
    assert venue.orders == [] and e.position is None


def test_a_refusal_does_not_spend_the_days_entry_allowance():
    venue = OrderVenue()
    e = live_engine(venue, 0.3)
    for k in range(1, 8):
        tick(e, k, 0.3)
    allowance_after_refusals = e.pair_risk.export_days()
    fresh = live_engine(OrderVenue(), 0.3).pair_risk.export_days()
    assert allowance_after_refusals == fresh


def test_the_refusal_is_the_same_after_a_restart():
    venue = OrderVenue()
    e = live_engine(venue, 0.3)
    for k in range(1, 6):
        tick(e, k, 0.3)
    state = e.to_state()
    assert state["book_state"] == "FLAT" and state["position"] is None

    venue2 = OrderVenue()
    e2 = live_engine(venue2, 0.3)
    e2.restore(state)
    d = tick(e2, 6, 0.3)
    assert d.reason == "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"
    assert venue2.orders == [] and e2.position is None


def test_the_same_series_with_funding_that_clears_does_open():
    venue = OrderVenue()
    e = live_engine(venue, 1.5)
    decisions = [tick(e, k, 1.5) for k in range(1, 6)]
    assert any(d.acted for d in decisions)
    assert venue.orders and e.position is not None


def test_stale_or_unusable_inputs_still_refuse_before_the_gate_is_asked():
    # no spot mark: the basis is unknown, whatever the funding
    e = live_engine(OrderVenue(), 50.0)
    for k in range(1, 6):
        d = e.on_candle(mark=SPOT, funding_bps=50.0, spot=None,
                        funding_print_ms=k * H)
    assert d.reason == "SPOT_UNAVAILABLE_BASIS_UNKNOWN" and not d.acted
    # prints older than the gap rule are discarded, not smoothed across
    venue = OrderVenue()
    e = live_engine(venue, 50.0)
    e._funding_history = [50.0] * 5
    e._last_print_ms = 5 * H
    d = e.on_candle(mark=SPOT, funding_bps=50.0, spot=SPOT,
                    funding_print_ms=40 * H)
    assert d.reason == "INSUFFICIENT_FUNDING_HISTORY" and not d.acted
    assert venue.orders == []
    # a fee table the venue will not give leaves the round trip unpriced
    class Mute(OrderVenue):
        def get_fee_rates(self, symbol, product):
            raise RuntimeError("no fee table")
    e = live_engine(Mute(), 50.0)
    e._funding_history = [50.0] * 5
    econ = e.entry_economics(mark=SPOT, spot=SPOT, funding_bps=50.0)
    assert econ.refusal.reason == "FEE_RATES_UNREADABLE"


def test_the_basis_budget_boundary_agrees_on_both_sides_of_both_signs(native):
    """The basis budget (net edge x hold) is where a RELATIVE slip in either
    implementation hides: the random fuzz almost never lands within 1e-4 of it.
    Put entries a hair inside and a hair outside the budget, on the premium and
    the discount side, and require Python, the runtime engine and the C++ core to
    give the same answer, with the two sides giving DIFFERENT answers."""
    for funding in (1.0, 1.5, 2.0, 3.0):
        for borrow in (0.0, 0.05):
            prints = [funding] * 8
            v = cc.entry_gate(funding_prints_bps=prints, perp=SPOT, spot=SPOT,
                              borrow_apr=borrow, round_trip_bps=11.0)
            if not v.allowed:
                continue                      # no budget to probe at this rate
            budget = v.basis_budget_bps
            for sign, refused in ((1.0, "ENTRY_BASIS_EXCEEDS_CARRY_BUDGET"),
                                  (-1.0, "ENTRY_BASIS_DISCOUNT_EXCEEDS_CARRY_BUDGET")):
                answers = {}
                for rel in (-1e-3, -1e-5, -1e-7, 1e-7, 1e-5, 1e-3):
                    perp = px(sign * budget * (1.0 + rel))
                    got = {python_gate(prints, perp, SPOT, borrow, 11.0),
                           runtime(prints, perp, SPOT, borrow, 11.0),
                           cpp_gate(prints, perp, SPOT, borrow, 11.0)}
                    assert len(got) == 1, (funding, borrow, sign, rel, got)
                    answers[rel] = got.pop()
                # the probe is sensitive: just inside admits, just outside refuses
                assert answers[-1e-5][0] is True or \
                    answers[-1e-5][1] == "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"
                assert answers[1e-5] == (False, refused), (funding, borrow, sign)
