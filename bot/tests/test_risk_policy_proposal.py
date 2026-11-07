"""risk_policy_proposal: hand-checked arithmetic on a tiny series, honest labelling, and nothing activated."""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import risk_policy_proposal as RPP  # noqa: E402


def test_window_drawdown_is_the_worst_peak_to_trough_of_cumulative_funding_by_hand():
    # +2,+2,-3,-3,+1 : cumulative 2,4,1,-2,-1 ; peak 4 ; trough -2 -> drawdown 6
    assert RPP.window_drawdowns_bps([2, 2, -3, -3, 1], 5) == [6.0]
    # window of 3 over [1,-1,-1,2]: starts [1,-1,-1] -> cum 1,0,-1 peak 1 -> 2 ; [-1,-1,2] -> cum -1,-2,0 peak 0 -> 2
    assert RPP.window_drawdowns_bps([1, -1, -1, 2], 3) == [2.0, 2.0]
    assert RPP.window_drawdowns_bps([1, 1, 1, 1], 4) == [0.0]                    # funding that only rises never draws down


def test_shortfall_share_counts_trailing_windows_below_the_requirement():
    series = [0.0] * 4 + [10.0] * 4                    # trailing 4-print mean: 0,0.. then rising
    share = RPP.rolling_mean_shortfall_share(series, 4, 5.0)
    # windows: [0,0,0,0]=0 <5 ; [0,0,0,10]=2.5 <5 ; [0,0,10,10]=5 (not below) ; [0,10,10,10]=7.5 ; [10,10,10,10]=10 -> 2 of 5
    assert share == pytest.approx(2 / 5)
    assert RPP.rolling_mean_shortfall_share([1.0] * 3, 4, 1.0) != RPP.rolling_mean_shortfall_share([1.0] * 3, 4, 1.0)   # NaN: too short to say


def test_the_proposal_on_the_committed_history_is_labelled_and_activates_nothing():
    rep = RPP.propose(RPP.load_bps(RPP.DEFAULT_FUNDING), notional_usd=100.0)
    opts = {o["setting"]: o for o in rep["strategy_drawdown_funding_only"]["options"]}
    assert opts["tight"]["strategy_drawdown_limit_bps_of_notional"] <= opts["balanced"]["strategy_drawdown_limit_bps_of_notional"] <= opts["conservative"]["strategy_drawdown_limit_bps_of_notional"]
    assert opts["conservative"]["historical_30d_windows_that_would_have_stopped"] == 0.0
    assert opts["tight"]["historical_30d_windows_that_would_have_stopped"] > opts["balanced"]["historical_30d_windows_that_would_have_stopped"]
    assert "UNMEASURED" in rep["strategy_drawdown_funding_only"]["scope"] and "per_trade_loss_usd" in rep["unmeasured_limits"]
    assert "OWNER DECISION REQUIRED" in rep["decision"] and "stays False" in rep["decision"]
    assert rep["inputs"]["round_trip_bps_overlay_ASSUMED"] == 11.0 and "assumed" in rep["inputs"]["label"]
    assert 0.0 <= rep["persistent_shortfall"]["share_of_history_below_required_trailing_90_prints"] <= 1.0


def test_too_little_history_is_refused_and_a_bad_file_exits_2(tmp_path, capsys):
    with pytest.raises(ValueError, match="not enough"):
        RPP.propose([0.1] * 50, notional_usd=100.0)
    assert RPP.main(["--funding", str(tmp_path / "nope.json.gz")]) == 2


def test_the_policy_stays_unapproved_by_default():
    import risk_policy as RP
    import dataclasses
    assert dataclasses.fields(RP.Policy)[-2].name == "approved_by_owner" and dataclasses.fields(RP.Policy)[-2].default is False
