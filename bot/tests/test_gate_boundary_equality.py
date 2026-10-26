"""The entry gate on its EXACT boundaries, and the C++ ABI on hostile arguments.

Found by mutation testing, not by reading: flipping `>` to `>=` (or `<=` to `<`)
at any of the three gate comparisons, in Python or in `cpp/carrycore.cpp`, left
every existing test green, because the fuzz and the "a hair inside / a hair
outside" probes never land ON the boundary. Where the boundary is decided is a
contract: the basis may EQUAL the carry budget (the gate refuses when it
EXCEEDS it), and a net edge of exactly zero is not an edge.

Every figure below is exact in binary floating point (powers of two and small
integers), so "equal" really is equal in both implementations. Nothing here
moves a threshold or says anything about profitability.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import carry_backtest as cb  # noqa: E402
import carry_core as core  # noqa: E402
import carry_costs as cc  # noqa: E402

H8 = 8 * 3600 * 1000


@pytest.fixture(scope="module")
def native():
    built = core.build()
    if not built and not core.available():
        if os.environ.get("REQUIRE_NATIVE_CORE") == "1":
            pytest.fail("REQUIRE_NATIVE_CORE=1 and libcarrycore.so is not built")
        pytest.skip("no compiler and no libcarrycore.so")
    return core


# One-day hold, 10 bps round trip, no borrow, funding 1670 bps/print:
#   carry 5010 - round trip 10 = net edge 5000, budget 5000 * 1 day = 5000.
# perp 1.5 against spot 1.0 is a basis of exactly +5000 bps; 0.5 is exactly -5000.
PRINTS = [1670.0, 1670.0, 1670.0]
KW = dict(borrow_apr=0.0, round_trip_bps=10.0)
HOLD = 1.0


def both(prints, perp, spot, *, hold=HOLD, borrow=0.0, rt=10.0):
    py = cc.entry_gate(funding_prints_bps=prints, perp=perp, spot=spot,
                       borrow_apr=borrow, round_trip_bps=rt, hold_days=hold)
    p = core.params(borrow_apr=borrow, round_trip_bps=rt, hold_days=hold,
                    gated=True, overlay=True)
    cpp = core.entry_gate(prints, perp, spot, p)
    return (py.allowed, py.reason), (cpp["allowed"], cpp["reason"])


def test_the_constructed_boundary_is_really_exact():
    v = cc.evaluate_entry(funding_prints_bps=PRINTS, perp=1.5, spot=1.0,
                          hold_days=HOLD, **KW)
    assert v.basis_budget_bps == 5000.0 and v.entry_basis_bps == 5000.0


def test_a_premium_exactly_equal_to_the_budget_is_allowed_in_both(native):
    py, cpp = both(PRINTS, 1.5, 1.0)
    assert py == cpp == (True, "CARRY_CLEARS_COSTS_AND_BASIS")


def test_a_discount_exactly_equal_to_the_budget_is_allowed_in_both(native):
    py, cpp = both(PRINTS, 0.5, 1.0)
    assert py == cpp == (True, "CARRY_CLEARS_COSTS_AND_BASIS")


def test_just_past_the_premium_budget_is_refused_in_both(native):
    py, cpp = both(PRINTS, 1.5 + 2.0 ** -30, 1.0)
    assert py == cpp == (False, "ENTRY_BASIS_EXCEEDS_CARRY_BUDGET")


def test_just_past_the_discount_budget_is_refused_in_both(native):
    py, cpp = both(PRINTS, 0.5 - 2.0 ** -30, 1.0)
    assert py == cpp == (False, "ENTRY_BASIS_DISCOUNT_EXCEEDS_CARRY_BUDGET")


def test_a_net_edge_of_exactly_zero_is_refused_in_both(native):
    """Funding 10 bps/print = 30/day against a 30 bps round trip over one day:
    net edge 0.0 exactly. With a zero budget and a zero basis nothing else
    would stop it, and the measured-hold check (3 settlements needed, 17
    allowed) would wave it through: only `net_edge <= 0` refuses it."""
    verdict = cc.evaluate_entry(funding_prints_bps=[10.0] * 3, perp=1.0, spot=1.0,
                                borrow_apr=0.0, round_trip_bps=30.0, hold_days=1.0)
    assert verdict.net_edge_bps_per_day == 0.0
    assert (verdict.allowed, verdict.reason) == (False, "CARRY_BELOW_COST_OF_CAPITAL")
    py, cpp = both([10.0] * 3, 1.0, 1.0, rt=30.0)
    assert py == cpp == (False, "CARRY_BELOW_COST_OF_CAPITAL")


def test_the_ungated_entry_threshold_is_inclusive_in_both_simulators(native):
    """`may_open = rate_bps >= entry_bps` in the Python simulator and in C++."""
    rate = 2e-5
    entry = rate * 1e4
    rows = [cb.Settlement(ms=i * H8, perp=100_000.0, spot=100_000.0, rate=rate,
                          perp_high=100_000.0, perp_low=100_000.0)
            for i in range(14)]
    py = cb.simulate_settlements_series(
        rows, notional=100_000.0, entry_bps=entry, borrow_apr=0.0, gated=False,
        impact_bps=0.0, mode=cb.OVERLAY)
    p = core.params(notional=100_000.0, entry_bps=entry, borrow_apr=0.0,
                    round_trip_bps=cb.round_trip_bps_for(cb.OVERLAY), gated=False,
                    overlay=True, taker_spot_bps=cb.TAKER_BPS_SPOT,
                    taker_perp_bps=cb.TAKER_BPS_PERP)
    cpp = core.simulate(rows, p)
    assert py["trades"] == cpp["trades"] == 1
    assert py["net_usd"] == pytest.approx(cpp["net"], abs=0.01)


# -- the ABI: hostile arguments must refuse, never crash ----------------------

def _call(snippet):
    """Run `snippet` in a child so a segfault is a return code, not a dead
    test run. The snippet may use `lib`, `ctypes`, `core` and `P` (Params)."""
    code = textwrap.dedent(f"""
        import ctypes, sys
        sys.path.insert(0, {BOT!r}); sys.path.insert(0, {os.path.join(BOT, 'tools')!r})
        import carry_core as core
        lib = core.load()
        assert lib is not None
        P = core.params(gated=True, overlay=True, round_trip_bps=11.0)
        D = ctypes.POINTER(ctypes.c_double)
        {snippet}
    """)
    return subprocess.run([sys.executable, "-I", "-c", code], capture_output=True,
                          text=True, timeout=60)


@pytest.mark.parametrize("snippet", [
    # prints pointer NULL while n > 0
    "r = ctypes.c_int32(-1); print(lib.carry_entry_gate(None, 5, 100.0, 100.0, "
    "ctypes.byref(P), ctypes.byref(r)))",
    # params NULL
    "r = ctypes.c_int32(-1); a = (ctypes.c_double * 3)(1, 1, 1); "
    "print(lib.carry_entry_gate(a, 3, 100.0, 100.0, None, ctypes.byref(r)))",
    # reason NULL
    "a = (ctypes.c_double * 3)(1, 1, 1); "
    "print(lib.carry_entry_gate(a, 3, 100.0, 100.0, ctypes.byref(P), None))",
], ids=["null-prints", "null-params", "null-reason"])
def test_entry_gate_refuses_null_arguments_without_crashing(native, snippet):
    out = _call(snippet)
    assert out.returncode == 0, f"crashed: rc={out.returncode} {out.stderr[-300:]}"
    assert out.stdout.strip() == "0", "a NULL argument must be a refusal"


@pytest.mark.parametrize("n", [0, -1, -2**31])
def test_entry_gate_with_no_prints_refuses_as_insufficient_history(native, n):
    out = _call(
        "r = ctypes.c_int32(-1); a = (ctypes.c_double * 1)(5.0); "
        f"print(lib.carry_entry_gate(a, {n}, 100.0, 100.0, ctypes.byref(P), "
        "ctypes.byref(r)), r.value)")
    assert out.returncode == 0, out.stderr[-300:]
    assert out.stdout.split() == ["0", "2"]          # 2 = INSUFFICIENT_FUNDING_HISTORY


@pytest.mark.parametrize("rows_arg,n,expect", [
    ("None", 5, "1"),              # NULL rows
    ("rows", 0, "1"),              # an empty window is refused, not simulated
    ("rows", -1, "1"),
])
def test_simulate_refuses_empty_or_null_input_without_crashing(
        native, rows_arg, n, expect):
    out = _call(
        "rows = (core.Settlement * 2)(); res = core.Result(); "
        f"print(lib.carry_simulate({rows_arg}, {n}, ctypes.byref(P), "
        "ctypes.byref(res)))")
    assert out.returncode == 0, f"crashed: rc={out.returncode} {out.stderr[-300:]}"
    assert out.stdout.strip() == expect


def test_simulate_refuses_null_params_and_null_result(native):
    out = _call(
        "rows = (core.Settlement * 2)(); res = core.Result(); "
        "print(lib.carry_simulate(rows, 2, None, ctypes.byref(res)), "
        "lib.carry_simulate(rows, 2, ctypes.byref(P), None))")
    assert out.returncode == 0, f"crashed: rc={out.returncode} {out.stderr[-300:]}"
    assert out.stdout.split() == ["1", "1"]
