"""The hold audit measures the real engine's holds and changes no threshold."""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import carry_costs  # noqa: E402
from tools import carry_hold_audit as ha  # noqa: E402


class TestTheMeasurements:
    def test_distribution_of_known_holds(self):
        d = ha.hold_distribution([1, 2, 3, 4, 100])
        assert (d["n"], d["min"], d["median"], d["max"]) == (5, 1, 3, 100)
        assert d["mean"] == pytest.approx(22.0)
        assert d["share_under_7d"] == pytest.approx(0.8)
        assert d["share_at_least_30d"] == pytest.approx(0.2)

    def test_quantiles_interpolate(self):
        assert ha.quantile([0, 10], .25) == pytest.approx(2.5)
        assert ha.quantile([5], .9) == 5

    def test_an_empty_history_is_stated_not_averaged(self):
        assert ha.hold_distribution([]) == {"n": 0}
        assert ha.hold_verdict(30.0, {"n": 0})["verdict"] == "NO_COMPLETED_HOLDS"

    @pytest.mark.parametrize("assumed,verdict", [
        (30.0, "GATE_OPTIMISTIC_ABOUT_HOLD"),        # above mean AND median
        (10.0, "GATE_ABOVE_MEDIAN_BELOW_MEAN"),
        (3.0, "GATE_NOT_OPTIMISTIC_ABOUT_HOLD")])
    def test_verdict_compares_the_assumption_with_both_mean_and_median(
            self, assumed, verdict):
        dist = {"n": 9, "mean": 17.0, "median": 6.0}
        assert ha.hold_verdict(assumed, dist)["verdict"] == verdict

    def test_per_trade_economics_by_hand(self):
        t = {"qty": 1.0, "entry_perp": 100.0, "exit_perp": 90.0,
             "entry_spot": 99.0, "exit_spot": 95.0, "funding": 7.0}
        o = ha._trade_net(t, taker_perp_bps=5.5, taker_spot_bps=10.0,
                          mode="overlay")
        assert o["basis"] == pytest.approx(6.0)          # short gained 10, coin lost 4
        assert o["fees"] == pytest.approx((100 + 90) * 5.5 / 1e4)
        assert o["net"] == pytest.approx(7 + 6 - (100 + 90) * 5.5 / 1e4)
        a = ha._trade_net(t, taker_perp_bps=5.5, taker_spot_bps=10.0,
                          mode="acquire")
        assert a["fees"] == pytest.approx(o["fees"] + (99 + 95) * 10.0 / 1e4)

    def test_a_position_still_open_has_paid_only_its_entry_fee(self):
        t = {"qty": 1.0, "entry_perp": 100.0, "exit_perp": 90.0,
             "entry_spot": 99.0, "exit_spot": 95.0, "funding": 0.0,
             "open_at_end": True}
        o = ha._trade_net(t, taker_perp_bps=5.5, taker_spot_bps=10.0,
                          mode="overlay")
        assert o["fees"] == pytest.approx(100 * 5.5 / 1e4)

    def test_the_assessment_keeps_both_halves_of_the_evidence_together(self):
        verdict = {"verdict": "GATE_OPTIMISTIC_ABOUT_HOLD",
                   "assumed_hold_days": 30.0, "measured_median_days": 5.8,
                   "measured_mean_days": 16.9}
        text = ha.assess(verdict, {"earned_over_expected": 2.96},
                         {"losing_share": 0.69})
        assert "optimistic" in text and "offset in aggregate" in text
        assert "NOT shown to make entries mispriced" in text
        assert "69%" in text and "not changed here" in text
        worse = ha.assess(verdict, {"earned_over_expected": 0.5},
                          {"losing_share": 0.2})
        assert "optimistic on funding as well" in worse


@pytest.fixture(scope="module")
def audit():
    return ha.run_audit()


class TestOnTheRealEngineAndCorpus:
    def test_it_reconciles_to_the_replays_own_totals(self, audit):
        assert audit["reconciles_with_replay"] is True
        t = audit["totals"]
        assert t["net_usd"] == pytest.approx(
            t["funding_usd"] + t["basis_usd"] + t["fees_usd"] + t["borrow_usd"],
            abs=1.0)

    def test_the_gate_reproduces_the_historical_entries_but_the_discount_bound(
            self, audit):
        # The engine decided on the quote; the trade rows carry the FILL, which
        # slips the short below the quote. Five fills landed past the new
        # discount bound (they passed it at decision time), so the gate
        # re-run on the recorded fill prices refuses exactly those.
        assert audit["trades"] == 77
        assert audit["gate_reproduced_entries"] == 72

    def test_counts_are_consistent(self, audit):
        assert audit["trades"] >= audit["completed_trades"] > 0
        assert audit["hold_days"]["n"] == audit["completed_trades"]
        assert sum(audit["exit_reasons"].values()) == audit["trades"]
        assert audit["hold_days"]["min"] <= audit["hold_days"]["median"] \
            <= audit["hold_days"]["max"]

    def test_the_frozen_horizon_is_what_the_audit_judges(self, audit):
        assert audit["verdict"]["assumed_hold_days"] == carry_costs.ASSUMED_HOLD_DAYS
        frozen = audit["sensitivity_to_assumed_hold_in_sample"][0]
        assert frozen["hold_days"] == carry_costs.ASSUMED_HOLD_DAYS
        assert frozen["entries_passed"] == audit["gate_reproduced_entries"]

    def test_the_early_exit_table_is_ordered_and_ends_at_the_full_hold(self, audit):
        caps = audit["sensitivity_to_early_exit"]
        assert [c["cap_days"] for c in caps] == [3, 7, 14, 30, None]
        assert caps[-1]["share_of_full_hold_net"] == pytest.approx(1.0)
        assert all(c["trades"] == audit["completed_trades"] for c in caps)

    def test_overlay_and_acquire_are_economically_distinct(self, audit):
        acquire = ha.run_audit(mode="acquire")
        assert audit["round_trip_bps"] == pytest.approx(11.0)
        assert acquire["round_trip_bps"] == pytest.approx(31.0)
        assert acquire["totals"]["fees_usd"] < audit["totals"]["fees_usd"]
        assert acquire["reconciles_with_replay"] is True

    def test_the_report_is_json_serialisable_and_carries_its_caveats(self, audit):
        json.dumps(audit, default=str)
        assert any("in-sample" in c for c in audit["caveats"])
        assert "not changed here" in audit["assessment"]


class TestNothingIsRetuned:
    def test_the_assumed_hold_is_frozen_until_a_human_changes_it_on_evidence(self):
        """If this fails, someone changed the horizon. Re-run
        tools/carry_hold_audit.py, validate OUT OF SAMPLE, and update this pin in
        the same reviewed change. Never tune it until an entry passes."""
        assert carry_costs.ASSUMED_HOLD_DAYS == 30.0

    def test_running_the_audit_does_not_move_it_and_the_default_is_used(self, audit):
        before = carry_costs.ASSUMED_HOLD_DAYS
        ha.run_audit()
        assert carry_costs.ASSUMED_HOLD_DAYS == before
        kw = dict(funding_prints_bps=[2.0] * 8, perp=82_000.0, spot=82_000.0,
                  borrow_apr=0.0, round_trip_bps=11.0)
        assert carry_costs.evaluate_entry(**kw).net_edge_bps_per_day == \
            carry_costs.evaluate_entry(**kw, hold_days=before).net_edge_bps_per_day
