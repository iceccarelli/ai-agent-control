"""Acquire-mode economics against an INDEPENDENT closed-form oracle, and the exact, derived deviation of the two simulators from it.

Why this exists. The native C++ core (``cpp/carrycore.cpp``) and ``carry_backtest`` model a position of ONE gross quantity on both legs. The real engine
(driven through history by ``carry_replay``) buys ``Q`` BTC spot, pays the taker fee IN BTC, so it holds ``Q * (1 - f)``, and shorts THAT quantity (snapped
to the venue's lot). Comparing the engine with a simulator that shares the flawed assumption would prove nothing, so the expected values below are written
from the mechanics (what you hold, what you owe, what you receive) and share no formula with any of the three implementations.

Hand example used throughout the comments (Q = 1 BTC, f = 10 bps, spot 100k -> 120k, perp 100k -> 118k, 3 bps funding):
  hold 0.999 BTC; spot leg 0.999 * 20000 = 19980; perp leg -0.999 * 18000 = -17982; basis 1998 (a gross-quantity model says 2000);
  fees 100 (entry spot fee, taken in BTC) + 54.945 + 119.88 + 64.8351 = 339.6601 (a gross-quantity model says 339.9).

What is pinned:
  1. the engine equals the oracle to the cent (funding, basis, fees, net) for several price paths and sizes;
  2. each simulator differs from the oracle by EXACTLY the term the gross-quantity assumption introduces (f * Q * (dS - dP) on basis, f on funding, f times the
     three non-entry-spot fee legs on fees), so any drift of either side is caught and the size of the approximation is a stated number, not a guess;
  3. a quantity the venue's lot cannot hedge is a REAL unhedged P&L that the engine books and the simulators cannot see.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import carry_backtest as cb  # noqa: E402
import carry_core as core  # noqa: E402
import carry_replay as rp  # noqa: E402

H8 = 8 * 3600 * 1000
F_SPOT = 10.0 / 1e4                 # the spot taker fee, written out here (checked against the code's constant below)
T_PERP = 5.5 / 1e4                  # the perp taker fee


def test_the_fee_constants_the_oracle_assumes_are_the_ones_the_code_uses():
    assert cb.TAKER_BPS_SPOT == 10.0 and cb.TAKER_BPS_PERP == 5.5


def rows(spot_perp, rates_bps):
    return [cb.Settlement(ms=i * H8, perp=p, spot=s, rate=r / 1e4, perp_high=p, perp_low=p)
            for i, ((s, p), r) in enumerate(zip(spot_perp, rates_bps))]


def path(q, s0, s1, p0, p1, pre=12, up=8, down=4, rate=3.0, neg=-1.0):
    """``pre`` prints at the entry prices, ``up`` + ``down`` prints at the exit prices; funding ``rate`` bps then ``neg`` bps for the last ``down``."""
    sp = [(s0, p0)] * pre + [(s1, p1)] * (up + down)
    rt = [rate] * (pre + up) + [neg] * down
    return q, rows(sp, rt)


def three_ways(q, rs, s0=None):
    n = q * rs[0].perp                                           # every implementation sizes the position as notional / PERP price
    eng = rp.replay(rs, notional=n, borrow_apr=0.0, impact_bps=0.0, execution_mode="acquire")
    py = cb.simulate_settlements_series(rs, notional=n, borrow_apr=0.0, gated=True, impact_bps=0.0, mode=cb.ACQUIRE, latency_rows=0)
    prm = core.params(notional=n, borrow_apr=0.0, impact_bps=0.0, round_trip_bps=cb.round_trip_bps_for(cb.ACQUIRE), gated=True, overlay=False,
                      taker_spot_bps=cb.TAKER_BPS_SPOT, taker_perp_bps=cb.TAKER_BPS_PERP, latency_rows=0)
    return eng, py, core.simulate(rs, prm)


def oracle(q, rs, trade, s0, s1, p0, p1, lot=0.001):
    """First principles. ``trade`` supplies only WHEN the position was open (the decision logic, whose parity is tested elsewhere); every number is arithmetic
    on what was bought, held, sold and paid."""
    held = q * (1.0 - F_SPOT)                                    # the spot buy fee is taken in BTC: that is all you hold
    short = round(int(held / lot + 1e-9) * lot, 12)             # the perp is sold in whole lots of what you hold
    i_open, i_close = trade["opened_ms"] // H8, trade["closed_ms"] // H8
    funding = sum(short * rs[i].perp * rs[i].rate for i in range(i_open + 1, i_close + 1))     # the short earns the print x mark x rate on each print held
    spot_leg = held * (s1 - s0)
    perp_leg = -short * (p1 - p0)
    fees = (q * s0 * F_SPOT                                      # entry: the BTC fee, valued at the entry price
            + short * p0 * T_PERP                                # entry: perp taker
            + held * s1 * F_SPOT                                 # exit: spot sell of what is held
            + short * p1 * T_PERP)                               # exit: perp buy back
    return {"funding": funding, "basis": spot_leg + perp_leg, "fees": fees, "held": held, "short": short}


SCENARIOS = {
    "the hand example (spot +20k, perp +18k)":        path(1.0, 100_000.0, 120_000.0, 100_000.0, 118_000.0),
    "flat basis, both +20k":                          path(1.0, 100_000.0, 120_000.0, 100_000.0, 120_000.0),
    "basis widening against the short":               path(2.0, 100_000.0, 100_000.0, 100_000.0, 103_000.0),
    "prices fall, spot -15k, perp -13k":              path(3.0, 100_000.0, 85_000.0, 100_000.0, 87_000.0),
    "a larger position, small moves":                 path(10.0, 50_000.0, 50_500.0, 50_020.0, 50_480.0),
}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_the_engine_equals_the_first_principles_oracle_to_the_cent(name):
    q, rs = SCENARIOS[name]
    s0, p0 = rs[0].spot, rs[0].perp
    s1, p1 = rs[-1].spot, rs[-1].perp
    eng, _py, _cpp = three_ways(q, rs, s0)
    assert eng["trades"] == 1
    tr = eng["trade_rows"][0]
    want = oracle(q, rs, tr, s0, s1, p0, p1)
    assert tr["spot_qty"] == pytest.approx(want["held"], abs=1e-9)
    assert tr["qty"] == pytest.approx(want["short"], abs=1e-9)
    assert eng["funding_usd"] == pytest.approx(want["funding"], abs=0.005)
    assert eng["basis_usd"] == pytest.approx(want["basis"], abs=0.005)
    assert eng["fees_usd"] == pytest.approx(-want["fees"], abs=0.005)
    assert eng["net_usd"] == pytest.approx(want["funding"] + want["basis"] - want["fees"], abs=0.01)


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_each_simulator_differs_from_the_oracle_by_exactly_the_gross_quantity_term_and_no_more(name):
    q, rs = SCENARIOS[name]
    s0, p0 = rs[0].spot, rs[0].perp
    s1, p1 = rs[-1].spot, rs[-1].perp
    eng, py, cpp = three_ways(q, rs, s0)
    want = oracle(q, rs, eng["trade_rows"][0], s0, s1, p0, p1)
    # The gross-quantity model holds q (not q * (1 - f)) on both legs. Derived, not read from code:
    gross = q                                                    # lot-aligned in every scenario, so no snapping term here
    funding_g = want["funding"] / (1.0 - F_SPOT)                # the same prints on q instead of q * (1 - f)
    basis_g = gross * ((s1 - s0) - (p1 - p0))
    fees_g = q * s0 * F_SPOT + gross * p0 * T_PERP + gross * s1 * F_SPOT + gross * p1 * T_PERP
    assert cpp["funding"] == pytest.approx(funding_g, abs=0.05)
    assert cpp["basis"] == pytest.approx(basis_g, abs=0.05)
    assert cpp["fees"] == pytest.approx(fees_g, abs=0.05)
    a = py["attribution"]
    assert a["funding_usd"] == pytest.approx(funding_g, abs=0.05)
    assert a["basis_usd"] == pytest.approx(basis_g, abs=0.05)
    assert -a["fees_usd"] == pytest.approx(fees_g, abs=0.05)             # the Python simulator books fees as a negative number
    # and the size of the approximation is a stated number: f * q * (dS - dP) on basis, f * funding on funding
    assert cpp["basis"] - want["basis"] == pytest.approx(F_SPOT * q * ((s1 - s0) - (p1 - p0)), abs=0.05)
    assert cpp["funding"] - want["funding"] == pytest.approx(F_SPOT * funding_g, abs=0.05)


def test_the_hand_example_in_the_module_docstring_is_what_the_engine_and_the_core_produce():
    q, rs = SCENARIOS["the hand example (spot +20k, perp +18k)"]
    eng, _py, cpp = three_ways(q, rs, 100_000.0)
    assert eng["basis_usd"] == pytest.approx(1998.0, abs=1e-6) and cpp["basis"] == pytest.approx(2000.0, abs=1e-6)
    assert eng["fees_usd"] == pytest.approx(-339.6601, abs=1e-6) and cpp["fees"] == pytest.approx(339.9, abs=1e-6)
    assert eng["funding_usd"] == pytest.approx(517.2822, abs=1e-6)             # 0.999 * (270 + 283.2 - 35.4): 9 prints at 100000 and 8 at 118000 earn +3 bps, the last 3 at 118000 pay -1 bp


def test_flat_basis_is_the_one_case_where_the_gross_basis_error_vanishes_and_the_funding_error_does_not():
    q, rs = SCENARIOS["flat basis, both +20k"]
    eng, _py, cpp = three_ways(q, rs, 100_000.0)
    assert eng["basis_usd"] == pytest.approx(0.0, abs=1e-6) and cpp["basis"] == pytest.approx(0.0, abs=1e-6)
    assert cpp["funding"] > eng["funding_usd"]                                    # still 0.1 % high


def test_a_quantity_the_lot_cannot_hedge_is_real_unhedged_pnl_the_engine_books_and_the_simulators_cannot_see():
    # Notional 123456 at 100000: spot buy 1.23456 -> snapped to the spot lot by the engine; the perp is sold in whole 0.001 lots of what is HELD.
    # Hand: the engine's own trade row gives the quantities; the residual (held - short) rides the spot price alone.
    s0 = p0 = 100_000.0
    sp = [(s0, p0)] * 12 + [(120_000.0, 120_000.0)] * 12
    rt = [3.0] * 20 + [-1.0] * 4
    rs = rows(sp, rt)
    n = 123_456.0
    eng = rp.replay(rs, notional=n, borrow_apr=0.0, impact_bps=0.0, execution_mode="acquire")
    tr = eng["trade_rows"][0]
    residual = tr["spot_qty"] - tr["qty"]
    assert residual > 0.0                                                          # more BTC held than hedged
    assert eng["basis_usd"] == pytest.approx(tr["spot_qty"] * 20_000.0 - tr["qty"] * 20_000.0, abs=0.005)
    assert eng["basis_usd"] == pytest.approx(residual * 20_000.0, abs=0.005) and eng["basis_usd"] > 1.0
    prm = core.params(notional=n, borrow_apr=0.0, impact_bps=0.0, round_trip_bps=cb.round_trip_bps_for(cb.ACQUIRE), gated=True, overlay=False,
                      taker_spot_bps=cb.TAKER_BPS_SPOT, taker_perp_bps=cb.TAKER_BPS_PERP, latency_rows=0)
    assert core.simulate(rs, prm)["basis"] == pytest.approx(0.0, abs=1e-6)         # a hedge ratio of exactly 1: the residual does not exist for it
