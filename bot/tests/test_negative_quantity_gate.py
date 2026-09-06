"""gate_order must refuse a negative or non-finite quantity outright.

Finding: `if quantity and float(quantity) > 0.0:` guards the four size
gates (per-trade risk, position notional, aggregate exposure, correlation
bucket). Anything that is NOT strictly positive - including a negative
quantity - skips straight past all four and falls through to
`RiskDecision(ok=True, reason="APPROVED")`.

Severity note (do not overstate): trading_engine.py's only two call sites
pass `quantity=0.0` for the intentional size-independent pre-gate, or
`sizing.qty` for the final gate - and BillionairePositionSizing refuses
(should_trade=False) rather than emitting a negative size, so this branch
is not reachable through the current engine flow. It is defense in depth
against a future or external caller (a hand-built TradeIntent, a test
harness, a different sizer), not a fix to an actively exploited path.
"""
from __future__ import annotations

import math

import pytest

from test_risk_management import EQUITY, good_order, manager, store  # noqa: F401


class TestNegativeQuantityIsRefused:
    def test_negative_quantity_is_blocked_not_approved(self, manager):
        decision = manager.gate_order(**good_order(quantity=-0.5))
        assert decision.ok is False
        assert decision.reason == "NEGATIVE_OR_NON_FINITE_QUANTITY"

    def test_nan_quantity_is_blocked(self, manager):
        decision = manager.gate_order(**good_order(quantity=float("nan")))
        assert decision.ok is False

    def test_inf_quantity_is_blocked(self, manager):
        decision = manager.gate_order(**good_order(quantity=float("inf")))
        assert decision.ok is False

    def test_zero_quantity_still_passes_as_the_size_independent_pre_gate(self, manager):
        # This is the EXISTING, intentional behaviour (trading_engine.py's
        # pre-gate call) and must not regress: 0.0 skips size gates but is
        # not itself a refusal.
        decision = manager.gate_order(**good_order(quantity=0.0))
        assert decision.ok is True

    def test_positive_quantity_still_runs_every_size_gate(self, manager):
        # An oversized notional must still be caught - proves the guard
        # change did not accidentally widen the positive path.
        decision = manager.gate_order(**good_order(quantity=1000.0))
        assert decision.ok is False
        assert decision.reason != "NEGATIVE_OR_NON_FINITE_QUANTITY"

    def test_a_blocked_negative_quantity_is_journaled(self, manager, store):
        manager.gate_order(**good_order(quantity=-1_000_000.0))
        rows = store._query(
            "SELECT * FROM decisions WHERE reason = ?",
            ("NEGATIVE_OR_NON_FINITE_QUANTITY",),
        )
        assert len(rows) == 1
