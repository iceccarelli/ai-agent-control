"""A margin ratio or size cap that is not a finite number must refuse, never pass.

Every comparison against NaN is False, so `margin < floor` and
`notional > cap` both fall through to "approved" when either side is NaN.
Two places had that shape:

  * CarryRisk.gate_open: a snapshot-like object whose margin_multiple is NaN,
    a NaN margin floor, or a NaN notional cap approved the pair. The real
    MarketSnapshot.assert_fresh refuses a NaN margin first, so this is
    defence in depth; but the gate's own docstring says it must not be more
    permissive than the engine it guards, and CarryEngine._margin_multiple
    already turns a non-finite margin into "unreadable" -> HALT.
  * tools.drill.margin_stage_evidence: `multiple < floor` is False for NaN, so
    a NaN margin read on a held short reported ok=True.

Gates here only refuse more. `None` margin while flat is unchanged (that is
the documented pre-open case).
"""
from __future__ import annotations

import datetime as dt
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from carry_risk import CarryRisk  # noqa: E402
from tools.drill import margin_stage_evidence  # noqa: E402

NOW = dt.datetime(2026, 1, 5, 12, 0, tzinfo=dt.timezone.utc)


class _Snap:
    """The minimum a real MarketSnapshot exposes to the gate. assert_fresh
    passes so the margin check itself is what is under test."""

    def __init__(self, margin):
        self.margin_multiple = margin
        self.basis_bps = 1.0

    def assert_fresh(self):
        return None


def _gate(margin=3.0, notional=50.0, **kw):
    risk = CarryRisk(max_notional_usd=kw.pop("max_notional_usd", 100.0), **kw)
    return risk.gate_open(notional_usd=notional, snapshot=_Snap(margin),
                          has_open_pair=False, now=NOW)


class TestGateOpenRefusesNonFiniteNumbers:
    def test_baseline_is_approved(self):
        assert _gate().ok

    def test_none_margin_while_flat_is_still_allowed(self):
        assert _gate(margin=None).ok

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"),
                                     float("-inf"), 0.0, -1.0])
    def test_a_margin_that_is_not_finite_and_positive_is_refused(self, bad):
        d = _gate(margin=bad)
        assert not d.ok
        assert d.reason in {"MARGIN_UNUSABLE", "MARGIN_HEADROOM_TOO_THIN"}

    def test_a_nan_margin_is_refused_with_the_unusable_reason(self):
        assert _gate(margin=float("nan")).reason == "MARGIN_UNUSABLE"

    def test_a_non_numeric_margin_is_refused_not_raised(self):
        d = _gate(margin="abc")
        assert not d.ok
        assert d.reason == "MARGIN_UNUSABLE"

    def test_a_nan_margin_floor_is_refused(self):
        d = _gate(min_margin_multiple=float("nan"))
        assert not d.ok
        assert d.reason == "GATE_CONFIG_INVALID"

    def test_a_nan_notional_cap_is_refused(self):
        d = _gate(notional=1e12, max_notional_usd=float("nan"))
        assert not d.ok
        assert d.reason == "GATE_CONFIG_INVALID"

    def test_an_infinite_notional_cap_is_refused(self):
        d = _gate(notional=1e12, max_notional_usd=float("inf"))
        assert not d.ok
        assert d.reason == "GATE_CONFIG_INVALID"

    def test_a_non_positive_notional_cap_is_refused(self):
        assert not _gate(max_notional_usd=0.0).ok
        assert not _gate(max_notional_usd=-5.0).ok


class _Broker:
    def __init__(self, value):
        self.value = value

    def get_margin_multiple(self, symbol):
        return self.value


class TestDrillMarginStage:
    def _stage(self, value, qty=0.001):
        return margin_stage_evidence(broker=_Broker(value), symbol="BTCUSDT",
                                     venue_perp_qty=qty,
                                     min_margin_multiple=2.0)

    def test_healthy_margin_passes(self):
        assert self._stage(3.0)[0] is True

    def test_thin_margin_fails(self):
        assert self._stage(0.5)[0] is False

    def test_flat_is_unchanged(self):
        ok, ev, _ = self._stage(float("nan"), qty=0.0)
        assert ok is True and ev["reason"] == "NO_POSITION"

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"),
                                     float("-inf")])
    def test_a_non_finite_margin_on_a_held_short_fails(self, bad):
        ok, evidence, why = self._stage(bad)
        assert ok is False
        assert not math.isfinite(evidence["margin_multiple"])
        assert why
