"""risk_policy: fail-closed evaluation, explicit reason codes, and a breach latch that survives a restart."""
from __future__ import annotations

import dataclasses
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)

import risk_policy as RP  # noqa: E402

POLICY = RP.Policy(per_trade_loss_usd=2.0, daily_loss_usd=3.0, strategy_drawdown_usd=5.0, portfolio_drawdown_usd=8.0, max_hold_hours=360.0, max_exposure_usd=100.0,
                   max_leverage=3.0, min_liquidity_usd=1000.0, max_unhedged_usd=5.0, min_margin_multiple=2.0, min_liquidation_distance_pct=20.0, max_data_age_s=90.0,
                   max_correlated_exposure_usd=100.0, min_recent_funding_bps=0.2, approved_by_owner=True, approval_note="test fixture, not an owner approval")
GOOD = RP.State(trade_loss_usd=0.1, loss_today_usd=0.1, strategy_drawdown_usd=0.5, portfolio_drawdown_usd=0.5, hold_hours=10.0, exposure_usd=50.0, leverage=1.0,
                liquidity_usd=50_000.0, unhedged_usd=0.0, margin_multiple=5.0, liquidation_distance_pct=60.0, data_age_s=5.0, correlated_exposure_usd=50.0,
                recent_mean_funding_bps=0.5, operational_fault=False)
STATE_FIELDS = [f.name for f in dataclasses.fields(RP.State)]


def test_positive_control_a_fully_measured_in_limit_state_is_allowed():
    assert RP.evaluate(POLICY, GOOD) == RP.Decision(RP.ALLOW, ())


def test_an_unapproved_or_absent_policy_refuses_everything():
    for pol in (None, dataclasses.replace(POLICY, approved_by_owner=False)):
        d = RP.evaluate(pol, GOOD)
        assert d.status == RP.REFUSE and d.reasons == ("NO_APPROVED_POLICY",)


@pytest.mark.parametrize("name", STATE_FIELDS)
def test_every_missing_input_refuses_with_its_name(name):
    d = RP.evaluate(POLICY, dataclasses.replace(GOOD, **{name: None}))
    assert d.status in (RP.REFUSE, RP.SUSPEND) and f"MISSING_INPUT:{name}" in d.reasons


@pytest.mark.parametrize("name", [f for f in STATE_FIELDS if f != "operational_fault"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "5"])
def test_a_non_numeric_input_is_missing_not_zero(name, bad):
    d = RP.evaluate(POLICY, dataclasses.replace(GOOD, **{name: bad}))
    assert d.status != RP.ALLOW and f"MISSING_INPUT:{name}" in d.reasons


@pytest.mark.parametrize("lim,field,breaching,latches", [
    ("per_trade_loss_usd", "trade_loss_usd", 2.01, True), ("daily_loss_usd", "loss_today_usd", 3.01, True),
    ("strategy_drawdown_usd", "strategy_drawdown_usd", 5.01, True), ("portfolio_drawdown_usd", "portfolio_drawdown_usd", 8.01, True),
    ("max_hold_hours", "hold_hours", 360.1, False), ("max_exposure_usd", "exposure_usd", 100.1, False), ("max_leverage", "leverage", 3.1, False),
    ("min_liquidity_usd", "liquidity_usd", 999.0, False), ("max_unhedged_usd", "unhedged_usd", 5.01, True), ("min_margin_multiple", "margin_multiple", 1.9, False),
    ("min_liquidation_distance_pct", "liquidation_distance_pct", 19.9, False), ("max_data_age_s", "data_age_s", 90.1, False),
    ("max_correlated_exposure_usd", "correlated_exposure_usd", 100.1, False), ("min_recent_funding_bps", "recent_mean_funding_bps", 0.19, False)])
def test_each_limit_breaches_with_its_code_and_loss_or_hedge_breaches_latch(lim, field, breaching, latches):
    d = RP.evaluate(POLICY, dataclasses.replace(GOOD, **{field: breaching}))
    assert f"BREACH:{lim}" in d.reasons and (d.status == RP.SUSPEND) == latches and bool(d.latch) == latches
    edge = RP.evaluate(POLICY, dataclasses.replace(GOOD, **{field: getattr(POLICY, lim)}))
    assert edge.status == RP.ALLOW                                   # exactly AT the limit is inside it (positive control for the comparison)


def test_an_operational_fault_suspends_and_a_nan_limit_is_an_invalid_policy_not_a_pass():
    d = RP.evaluate(POLICY, dataclasses.replace(GOOD, operational_fault=True))
    assert d.status == RP.SUSPEND and "OPERATIONAL_FAULT" in d.reasons
    d2 = RP.evaluate(dataclasses.replace(POLICY, max_leverage=float("nan")), GOOD)
    assert d2.status == RP.REFUSE and "POLICY_INVALID:max_leverage" in d2.reasons


def test_a_latched_suspension_survives_a_restart_and_a_clean_check_does_not_lift_it(tmp_path):
    path = str(tmp_path / "latch.json")
    latch = RP.RiskLatch(path)
    bad = RP.evaluate(POLICY, dataclasses.replace(GOOD, trade_loss_usd=9.0))
    assert latch.apply("btc_carry", bad, at_ms=1).status == RP.SUSPEND
    restarted = RP.RiskLatch(path)                                   # a new process
    assert restarted.is_suspended("btc_carry")[0] is True and restarted.is_suspended("other")[0] is False
    in_force = restarted.apply("btc_carry", RP.evaluate(POLICY, GOOD), at_ms=2)
    assert in_force.status == RP.SUSPEND and "LATCHED" in in_force.reasons


def test_only_an_explicit_clear_with_a_reason_lifts_it_and_the_clearing_is_recorded(tmp_path):
    latch = RP.RiskLatch(str(tmp_path / "latch.json"))
    latch.apply("s", RP.evaluate(POLICY, dataclasses.replace(GOOD, loss_today_usd=99.0)), at_ms=1)
    with pytest.raises(ValueError, match="reason"):
        latch.clear("s", reason=" ", at_ms=2)
    assert latch.is_suspended("s")[0]
    latch.clear("s", reason="owner reviewed the loss", at_ms=3)
    assert latch.is_suspended("s")[0] is False and latch.apply("s", RP.evaluate(POLICY, GOOD), at_ms=4).status == RP.ALLOW
    import json
    assert json.load(open(latch.path))["__history__"][0]["reason"] == "owner reviewed the loss"


def test_an_unreadable_latch_file_is_a_suspension_not_a_clear_bill(tmp_path):
    p = tmp_path / "latch.json"; p.write_text("{not json")
    latch = RP.RiskLatch(str(p))
    assert latch.is_suspended("anything") == (True, ("LATCH_FILE_UNREADABLE",))
    assert latch.apply("anything", RP.evaluate(POLICY, GOOD), at_ms=1).status == RP.SUSPEND
    with pytest.raises(ValueError, match="unreadable"):
        latch.clear("anything", reason="r", at_ms=2)


def test_a_non_latching_breach_refuses_but_does_not_latch(tmp_path):
    latch = RP.RiskLatch(str(tmp_path / "latch.json"))
    d = RP.evaluate(POLICY, dataclasses.replace(GOOD, data_age_s=500.0))
    assert d.status == RP.REFUSE and latch.apply("s", d, at_ms=1).status == RP.REFUSE and not latch.is_suspended("s")[0]
