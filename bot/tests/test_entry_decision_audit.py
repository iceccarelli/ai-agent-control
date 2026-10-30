"""The entry decision carries its own arithmetic, and the arithmetic decides nothing.

`carry_costs.entry_gate` attaches `detail["audit"]` (`decision_audit`) to every
verdict: funding volatility (population std of the recent prints, bps per 8h),
inversion frequency (share of recent prints <= 0), the assumed and the
demonstrated hold and which half of the gate used which, and the expected net
over each hold. The engine copies it into the refusal (and the approval) it
returns as `entry_audit`. These tests pin that the numbers are right and that
NO decision changed: no new gate was added.
"""
from __future__ import annotations

import math
import os
import random
import statistics
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, HERE)

import carry_costs as cc  # noqa: E402
from carry_engine import OVERLAY, CarryEngine  # noqa: E402

SPOT = 100_000.0


def px(basis_bps):
    return SPOT * (1.0 + basis_bps / 1e4)


def reference_decision(prints, **kw):
    """entry_gate as it was written before the audit: evaluate_entry, then the
    demonstrated-hold check, composed by hand from the two public functions."""
    base = cc.evaluate_entry(funding_prints_bps=prints, **kw)
    if not base.allowed:
        return base.allowed, base.reason
    kw2 = {k: v for k, v in kw.items() if k in ("round_trip_bps", "borrow_apr",
                                               "opportunity_cost_apr")}
    hold = cc.hold_clears_round_trip(funding_prints_bps=prints, **kw2)
    if hold.allowed:
        return True, base.reason
    return False, hold.reason


def test_no_decision_changed_on_random_inputs():
    rng = random.Random(17)
    seen = set()
    for _ in range(5000):
        prints = [rng.uniform(-3, 6) for _ in range(rng.randint(0, 8))]
        if rng.random() < 0.05:
            prints.append(float("nan"))
        kw = dict(perp=px(rng.uniform(-80, 80)), spot=SPOT,
                  borrow_apr=rng.choice([0.0, 0.02, 0.1, -0.01]),
                  round_trip_bps=rng.choice([11.0, 31.0, 7.0, 0.0]),
                  opportunity_cost_apr=rng.choice([0.0, 0.0, 0.03]))
        v = cc.entry_gate(funding_prints_bps=prints, **kw)
        assert (v.allowed, v.reason) == reference_decision(prints, **kw)
        assert v.detail["audit"]["observability_only"] is True
        seen.add(v.reason)
    assert len(seen) >= 7


def test_the_verdict_numbers_are_untouched_by_the_audit():
    prints = [1.0, 2.0, -0.5, 3.0, 2.5]
    kw = dict(perp=px(4.0), spot=SPOT, borrow_apr=0.01, round_trip_bps=11.0)
    v = cc.entry_gate(funding_prints_bps=prints, **kw)
    bare = cc._entry_gate_decision(
        funding_prints_bps=prints, hold_days=cc.ASSUMED_HOLD_DAYS,
        demonstrated_hold_days=cc.DEMONSTRATED_HOLD_DAYS,
        ewma_alpha=cc.EWMA_ALPHA, ewma_min_prints=cc.EWMA_MIN_PRINTS,
        opportunity_cost_apr=0.0, **kw)
    for name in ("allowed", "reason", "expected_carry_bps_per_day",
                 "borrow_bps_per_day", "amortised_round_trip_bps_per_day",
                 "net_edge_bps_per_day", "entry_basis_bps", "basis_budget_bps",
                 "smoothed_funding_bps"):
        assert getattr(v, name) == getattr(bare, name), name
    assert {k: x for k, x in v.detail.items() if k != "audit"} == bare.detail


def test_the_audit_arithmetic():
    prints = [1.0, -0.5, 2.0, 0.0, 3.0]
    borrow, opp, rt = 0.02, 0.03, 11.0
    v = cc.entry_gate(funding_prints_bps=prints, perp=px(5.0), spot=SPOT,
                      borrow_apr=borrow, round_trip_bps=rt,
                      opportunity_cost_apr=opp)
    a = v.detail["audit"]
    assert a["funding_prints_seen"] == 5
    assert a["funding_volatility_bps"] == pytest.approx(statistics.pstdev(prints))
    assert a["funding_inversion_share"] == pytest.approx(2 / 5)
    assert a["assumed_hold_days"] == cc.ASSUMED_HOLD_DAYS
    assert a["demonstrated_hold_days"] == cc.DEMONSTRATED_HOLD_DAYS
    assert a["hold_used"] == {
        "amortisation_and_basis_budget": "assumed_hold_days",
        "break_even_round_trip": "demonstrated_hold_days"}
    smoothed = cc.ewma_funding_bps(prints)
    assert a["smoothed_funding_bps"] == smoothed
    carry = smoothed * 3
    net = carry - borrow / 365 * 1e4 - opp / 365 * 1e4
    assert a["carry_bps_per_day"] == pytest.approx(carry)
    assert a["net_carry_bps_per_day"] == pytest.approx(net)
    assert a["expected_net_over_assumed_hold_bps"] == pytest.approx(net * 30.0 - rt)
    assert a["expected_net_over_demonstrated_hold_bps"] == pytest.approx(
        net * 5.8 - rt)
    assert a["entry_basis_bps"] == pytest.approx(5.0)
    assert a["opportunity_cost_bps_per_day"] == pytest.approx(opp / 365 * 1e4)


def test_the_audit_shows_why_a_thirty_day_assumption_is_not_enough():
    """The headline case: the 30-day amortisation approves, the demonstrated
    hold refuses, and the audit shows the expected net is positive over 30
    days and negative over the hold that has actually been measured."""
    v = cc.entry_gate(funding_prints_bps=[0.3] * 5, perp=SPOT, spot=SPOT,
                      borrow_apr=0.0, round_trip_bps=11.0)
    assert v.reason == "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"
    a = v.detail["audit"]
    assert a["expected_net_over_assumed_hold_bps"] > 0
    assert a["expected_net_over_demonstrated_hold_bps"] < 0
    assert a["funding_inversion_share"] == 0.0
    assert a["funding_volatility_bps"] == 0.0


@pytest.mark.parametrize("prints,perp,borrow,rt", [
    ([], SPOT, 0.0, 11.0),
    (None, SPOT, 0.0, 11.0),
    ([float("nan")] * 4, float("nan"), float("nan"), float("nan")),
    ([1.0, "x", None, float("inf")], 0.0, 0.0, 11.0),
    ([5.0] * 5, -1.0, "abc", None),
])
def test_the_audit_never_raises_and_never_invents(prints, perp, borrow, rt):
    a = cc.decision_audit(funding_prints_bps=prints, perp=perp, spot=SPOT,
                          borrow_apr=borrow, round_trip_bps=rt,
                          hold_days=30.0, demonstrated_hold_days=5.8)
    assert a["observability_only"] is True
    if not [p for p in (prints or []) if isinstance(p, float)
            and math.isfinite(p)]:
        assert a["funding_volatility_bps"] is None
        assert a["funding_inversion_share"] is None
        assert a["expected_net_over_assumed_hold_bps"] is None


# -- the engine surfaces it -------------------------------------------------------

class FeeVenue:
    def get_fee_rates(self, symbol, product):
        return {"maker_bps": 2.0, "taker_bps": 5.5}


def test_the_engine_refusal_carries_the_audit_and_the_same_reason():
    e = CarryEngine(broker=FeeVenue(), borrow_apr=0.0, execution_mode=OVERLAY,
                    persist=lambda s: None)
    e._funding_history = [0.3, -0.2, 0.3, 0.4, 0.3]
    econ = e.entry_economics(mark=SPOT, spot=SPOT, funding_bps=0.3)
    want = cc.entry_gate(funding_prints_bps=e._funding_history, perp=SPOT,
                         spot=SPOT, borrow_apr=0.0, round_trip_bps=11.0)
    assert econ.refusal is not None and econ.refusal.reason == want.reason
    audit = econ.refusal.detail["entry_audit"]
    assert audit == want.detail["audit"]
    assert audit["funding_inversion_share"] == pytest.approx(0.2)
    # the fields the refusal always carried are still there and unchanged
    for key in ("funding_bps", "smoothed_bps", "entry_basis_bps",
                "net_edge_bps_per_day"):
        assert key in econ.refusal.detail


def test_the_engine_approval_carries_the_audit():
    import test_carry_overlay as ov
    venue = ov.Venue()
    e = ov.engine(venue)
    decision = ov.open_pair(e)
    assert decision.action == "opened", decision
    audit = decision.detail["entry_audit"]
    assert audit["observability_only"] is True
    assert audit["expected_net_over_demonstrated_hold_bps"] > 0
    # the orders sent are exactly the overlay's one hedge order
    assert [o[:2] for o in venue.orders] == [("Sell", "linear")]
