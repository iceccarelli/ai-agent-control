"""Multiple-testing correction: the layer this programme never had.

Eleven families frozen ABSENT + one cleared = twelve trials against one
price path, one venue, one four-year window. Nothing in this tree counted
them, and nothing corrected for them.

project_status.py:282 applies exactly this reasoning at width THREE
("best-of-three at a 95th percentile occurs about 14% of the time under a
global null") and never applies it at the width the programme actually
searched. These tests pin the arithmetic, the estimator, and the fact that
the seed is derived from the authoritative family list rather than the
signals/ directory - which undercounts by two.
"""
from __future__ import annotations

import math
import os
import random
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import deflated_sharpe as ds  # noqa: E402
import hypothesis_registry as hr  # noqa: E402


class TestFamilyWiseRate:
    def test_reproduces_the_trees_own_width_three_figure(self):
        """project_status.py:282 says ~14% for best-of-three. Confirm."""
        assert ds.family_wise_false_positive_rate(3, 0.05) == \
            pytest.approx(0.1426, abs=1e-4)

    def test_width_twelve_is_forty_six_percent(self):
        assert ds.family_wise_false_positive_rate(12, 0.05) == \
            pytest.approx(0.4596, abs=1e-4)

    def test_single_trial_is_exactly_alpha(self):
        assert ds.family_wise_false_positive_rate(1, 0.05) == pytest.approx(0.05)

    def test_rate_is_monotonic_in_search_width(self):
        rates = [ds.family_wise_false_positive_rate(n) for n in range(1, 30)]
        assert rates == sorted(rates)

    def test_the_tree_states_the_width_three_number_in_prose(self):
        """If this narration is ever removed, the correction loses its anchor."""
        src = open(os.path.join(ROOT, "project_status.py"), encoding="utf-8").read()
        assert "best-of-three" in src and "14%" in src


class TestNormPpf:
    def test_matches_known_quantiles(self):
        for p, want in [(0.975, 1.959964), (0.95, 1.644854), (0.5, 0.0),
                        (0.025, -1.959964)]:
            assert ds.norm_ppf(p) == pytest.approx(want, abs=1e-5)

    def test_refuses_out_of_range(self):
        for bad in (0.0, 1.0, -0.1, 2.0):
            with pytest.raises(ValueError):
                ds.norm_ppf(bad)

    def test_cdf_and_ppf_are_inverses(self):
        for p in (0.01, 0.2, 0.5, 0.8, 0.99):
            assert ds.norm_cdf(ds.norm_ppf(p)) == pytest.approx(p, abs=1e-6)


class TestExpectedMaxSharpe:
    def test_one_trial_has_no_selection_bias(self):
        assert ds.expected_max_sharpe_under_null(1) == 0.0

    def test_grows_with_search_width(self):
        vals = [ds.expected_max_sharpe_under_null(n) for n in (2, 5, 12, 50)]
        assert vals == sorted(vals)

    def test_matches_monte_carlo_within_five_percent(self):
        """The formula is only worth having if it predicts simulation."""
        random.seed(7)
        n_trials, obs = 12, 60
        maxes = []
        for _ in range(1500):
            best = -9.0
            for _ in range(n_trials):
                r = [random.gauss(0.0, 1.0) for _ in range(obs)]
                best = max(best, ds.sharpe_from_returns(r) * math.sqrt(obs - 1))
            maxes.append(best)
        empirical = sum(maxes) / len(maxes)
        assert empirical == pytest.approx(
            ds.expected_max_sharpe_under_null(n_trials), rel=0.05)


class TestDeflatedSharpe:
    def _returns(self, mean, sd, n, seed=1):
        random.seed(seed)
        return [random.gauss(mean, sd) for _ in range(n)]

    def test_pure_noise_at_high_search_width_does_not_survive(self):
        out = ds.deflated_sharpe_ratio(self._returns(0.0, 1.0, 60), n_trials=12)
        assert out["dsr"] < 0.95

    def test_a_strong_signal_at_width_one_survives(self):
        out = ds.deflated_sharpe_ratio(self._returns(0.5, 1.0, 250), n_trials=1)
        assert out["dsr"] > 0.95

    def test_units_are_consistent_between_sharpe_and_the_null_bar(self):
        """Regression: sr is per-observation, the order statistic is z-scale.

        Subtracting them directly (sharpe_variance=1.0) makes the null bar
        ~10x too large and reports dsr ~ 0 for EVERY input, which reads as
        rigour and is a broken instrument. The default must scale the bar
        to per-observation units.
        """
        returns = self._returns(0.3, 1.0, 200)
        out = ds.deflated_sharpe_ratio(returns, n_trials=12)
        assert 0.0 < out["expected_max_sharpe_under_null"] < 1.0
        assert out["expected_max_sharpe_under_null"] == pytest.approx(
            ds.expected_max_sharpe_under_null(12) / math.sqrt(len(returns) - 1),
            rel=1e-9)
        assert 0.0 < out["dsr"] < 1.0

    def test_the_same_track_record_degrades_as_search_width_grows(self):
        r = self._returns(0.25, 1.0, 200)
        wide = ds.deflated_sharpe_ratio(r, n_trials=100)["dsr"]
        narrow = ds.deflated_sharpe_ratio(r, n_trials=1)["dsr"]
        assert wide < narrow

    def test_negative_skew_is_penalised(self):
        """A stop-and-target strategy is structurally negatively skewed."""
        random.seed(3)
        base = [random.gauss(0.3, 1.0) for _ in range(200)]
        skewed = list(base)
        skewed[0] = -12.0  # one large loss: negative skew, fat tail
        assert (ds.deflated_sharpe_ratio(skewed, 12)["dsr"]
                < ds.deflated_sharpe_ratio(base, 12)["dsr"])

    def test_zero_variance_is_refused_not_infinite(self):
        with pytest.raises(ValueError):
            ds.deflated_sharpe_ratio([0.1] * 30, n_trials=5)


class TestMinimumTrackRecord:
    def test_weaker_sharpe_needs_a_longer_record(self):
        assert (ds.minimum_track_record_length(0.2, 0.0, 3.0)
                > ds.minimum_track_record_length(1.0, 0.0, 3.0))

    def test_non_positive_sharpe_can_never_be_significant(self):
        assert ds.minimum_track_record_length(0.0, 0.0, 3.0) == float("inf")


class TestEffectiveTrials:
    def test_independent_trials_are_unchanged(self):
        assert ds.effective_n_trials(12, 0.0) == pytest.approx(12.0)

    def test_correlated_trials_count_for_less(self):
        assert ds.effective_n_trials(12, 0.5) < 12.0

    def test_raw_count_is_the_conservative_choice(self):
        """Erring toward over-correction is the safe direction."""
        assert ds.effective_n_trials(12, 0.3) < 12


class TestRegistry:
    def test_seed_uses_the_authoritative_list_not_the_signals_directory(self):
        """signals/ has 10 .py files; FROZEN_ABSENT records 11 families.

        Seeding from the directory would undercount the search width by two
        (technical_analysis and closed_analyser were measured and frozen
        without a surviving module) and produce a WEAKER correction than the
        evidence supports.
        """
        import project_status as ps
        seeded = {t["trial_id"] for t in hr._seed_trials()}
        assert set(ps.FROZEN_ABSENT) <= seeded
        assert "funding_carry_fade_btc_v1" in seeded
        assert len(seeded) == len(ps.FROZEN_ABSENT) + 1
        sig_dir = os.path.join(ROOT, "signals")
        n_modules = len([f for f in os.listdir(sig_dir)
                         if f.endswith(".py") and f != "__init__.py"])
        assert len(seeded) > n_modules

    def test_registry_is_append_only(self, tmp_path):
        reg = {"schema": "hypothesis_registry/1", "trials": []}
        hr.record(reg, "x", status="tested")
        with pytest.raises(ValueError):
            hr.record(reg, "x", status="tested")

    def test_seed_is_idempotent(self, tmp_path):
        reg = {"schema": "hypothesis_registry/1", "trials": []}
        first = hr.seed(reg)
        assert hr.seed(reg) == 0 and first > 0

    def test_summary_calls_a_single_clear_insufficient_at_this_width(self, tmp_path):
        reg = {"schema": "hypothesis_registry/1", "trials": []}
        hr.seed(reg)
        s = hr.summary(reg)
        assert s["n_trials_recorded"] == 12
        assert s["n_cleared"] == 1
        assert s["family_wise_false_positive_rate"] > 0.05
        assert "not evidence of edge" in s["reading"]

    def test_the_seed_is_documented_as_a_floor(self, tmp_path):
        reg = {"schema": "hypothesis_registry/1", "trials": []}
        hr.seed(reg)
        assert "floor" in " ".join(reg.keys()).lower() or \
            "floor" in reg.get("seed_is_a_floor_not_a_census", "").lower()
