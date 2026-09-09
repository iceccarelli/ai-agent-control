"""The size gate the carry pair did not have.

RiskManager.gate_order guards every directional order in this tree. The carry
pair passed through NONE of it, leaving max_notional_usd as the only size
control on the book. At $100 that is harmless. At $1m it is the entire risk
framework.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import shadow  # noqa: E402
from carry_risk import CarryRisk  # noqa: E402
from market_snapshot import MarketSnapshot  # noqa: E402


def snap(**kw):
    base = dict(perp_mark=100_000.0, spot_mark=100_000.0, funding_bps=1.0,
                margin_multiple=5.0, observed_at_s=time.time())
    base.update(kw)
    return MarketSnapshot(**base)


class Store:
    def __init__(self, engaged=False, boom=False):
        self.engaged, self.boom = engaged, boom

    def is_kill_switch_engaged(self):
        if self.boom:
            raise RuntimeError("state db unreadable")
        return (self.engaged, "test")


def risk(**kw):
    kw.setdefault("store", Store())
    kw.setdefault("max_notional_usd", 100.0)
    return CarryRisk(**kw)


class TestTheCapIsTheGatesCap:
    def test_the_default_comes_from_shadow(self):
        assert CarryRisk(store=None).max_notional_usd == pytest.approx(
            shadow.SHADOW_MAX_NOTIONAL_USD)

    def test_moving_the_constant_moves_the_gate(self):
        from unittest import mock
        with mock.patch.object(shadow, "SHADOW_MAX_NOTIONAL_USD", 250.0):
            assert CarryRisk(store=None).max_notional_usd == pytest.approx(250.0)

    def test_a_notional_above_the_cap_is_refused(self):
        d = risk().gate_open(notional_usd=100.01, snapshot=snap(),
                             has_open_pair=False)
        assert not d and d.reason == "NOTIONAL_ABOVE_CAP"

    def test_the_gate_never_returns_a_size(self):
        """A gate that can enlarge an order is not a gate."""
        d = risk().gate_open(notional_usd=50.0, snapshot=snap(),
                             has_open_pair=False)
        assert d.detail["notional_usd"] == pytest.approx(50.0)


class TestOrderedByDamage:
    def test_the_kill_switch_outranks_everything(self):
        d = CarryRisk(store=Store(engaged=True), max_notional_usd=100.0
                      ).gate_open(notional_usd=1e9,
                                  snapshot=snap(observed_at_s=0.0),
                                  has_open_pair=True)
        assert d.reason == "KILL_SWITCH_ENGAGED"

    def test_an_unreadable_switch_is_an_engaged_switch(self):
        d = CarryRisk(store=Store(boom=True), max_notional_usd=100.0
                      ).gate_open(notional_usd=100.0, snapshot=snap(),
                                  has_open_pair=False)
        assert d.reason == "KILL_SWITCH_UNREADABLE"

    def test_an_open_pair_outranks_a_stale_view(self):
        d = risk().gate_open(notional_usd=100.0,
                             snapshot=snap(observed_at_s=0.0),
                             has_open_pair=True)
        assert d.reason == "PAIR_ALREADY_OPEN"


class TestOnlyOnePair:
    def test_a_second_pair_is_refused(self):
        d = risk().gate_open(notional_usd=100.0, snapshot=snap(),
                             has_open_pair=True)
        assert d.reason == "PAIR_ALREADY_OPEN"

    def test_the_reason_names_the_shared_liquidation_price(self):
        d = risk().gate_open(notional_usd=100.0, snapshot=snap(),
                             has_open_pair=True)
        assert "liquidation price" in d.detail["note"]


class TestFreshnessIsDelegated:
    def test_a_stale_snapshot_is_refused(self):
        d = risk().gate_open(notional_usd=100.0,
                             snapshot=snap(observed_at_s=time.time() - 600),
                             has_open_pair=False)
        assert d.reason == "MARKET_VIEW_STALE"

    def test_thin_margin_is_refused(self):
        d = risk().gate_open(notional_usd=100.0, snapshot=snap(margin_multiple=1.1),
                             has_open_pair=False)
        assert d.reason == "MARGIN_HEADROOM_TOO_THIN"

    def test_the_gate_is_not_looser_than_the_engine(self):
        from carry_engine import MIN_MARGIN_MULTIPLE as engine_floor
        import carry_risk
        assert carry_risk.MIN_MARGIN_MULTIPLE >= engine_floor


class TestEntriesPerDay:
    def test_a_second_entry_the_same_day_is_refused(self):
        r = risk()
        assert r.gate_open(notional_usd=100.0, snapshot=snap(),
                           has_open_pair=False)
        r.record_entry()
        d = r.gate_open(notional_usd=100.0, snapshot=snap(),
                        has_open_pair=False)
        assert d.reason == "ENTRY_LIMIT_REACHED_TODAY"

    def test_tomorrow_is_allowed_again(self):
        r = risk()
        yesterday = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
        r.record_entry(now=yesterday)
        assert r.gate_open(notional_usd=100.0, snapshot=snap(),
                           has_open_pair=False)

    def test_a_refused_pair_does_not_consume_the_day(self):
        """record_entry is called only after BOTH legs land."""
        r = risk()
        r.gate_open(notional_usd=1e9, snapshot=snap(), has_open_pair=False)
        assert r.gate_open(notional_usd=100.0, snapshot=snap(),
                           has_open_pair=False)


class TestBadInput:
    @pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf"),
                                     None, "100"])
    def test_an_unusable_notional_is_refused(self, bad):
        d = risk().gate_open(notional_usd=bad, snapshot=snap(),
                             has_open_pair=False)
        assert not d
        assert d.reason in ("NOTIONAL_INVALID", "NOTIONAL_ABOVE_CAP")


class TestItDecidesNothingElse:
    def test_the_module_places_no_orders(self):
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "carry_risk.py"), encoding="utf-8") as fh:
            body = fh.read()
        for banned in ("place_market", "live_authorized", "openai",
                       "anthropic"):
            assert banned not in body
