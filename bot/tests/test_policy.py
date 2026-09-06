"""Contract tests for the trainable policy.

Fully offline, no network, no state store, no config. The properties defended
here are the ones that turn a machine-learning module from an asset into a
liability, and every one of them has a named failure mode:

* a model trained on **pure noise** must be refused — a system that promotes a
  noise model will promote anything, and this is the single most important test
  in the file;
* purged, embargoed walk-forward is asserted **on indices**, not on a score. A
  leak defence that can only be verified through a metric is not verified at
  all, because the metric moves for a dozen other reasons;
* the leak is then *measured*: the same estimator on the same folds scores
  materially better without purging. That gap is the reason purging exists;
* a feature-schema or feature-order mismatch is refused loudly, because the
  alternative is confident, plausible, wrong predictions forever;
* every failure path produces ``usable=False`` with ``probability=None``. Never
  ``0.5``. ``0.5`` is a number ``risk_management._gate_expected_edge`` will
  happily multiply by a reward.

The expensive fixtures are module-scoped: the whole file trains about a dozen
small models and runs in a few seconds.
"""
from __future__ import annotations

import ast
import contextlib
import copy
import importlib.util
import json
import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import policy as pol  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY_PATH = os.path.join(REPO, "policy.py")

SCHEMA = "features/v1"
NAMES = ("f0", "f1", "f2", "f3", "f4")

#: Hyper-parameters used wherever the *defaults* are not what is under test.
#: Deliberately weaker regularisation so that a few hundred rows can express
#: something; the shipped defaults are asserted separately.
FAST_HP = {
    "max_depth": 4,
    "max_leaf_nodes": 15,
    "min_samples_leaf": 20,
    "l2_regularization": 0.0,
    "learning_rate": 0.1,
    "max_iter": 60,
    "max_bins": 64,
    "early_stopping": False,
    "random_state": 0,
}


# ---------------------------------------------------------------------------
# synthetic data
# ---------------------------------------------------------------------------


def learnable_data(n=4000, seed=0):
    """A genuinely learnable signal with a known Bayes probability.

    ``y ~ Bernoulli(sigmoid(x·w))`` — so a well-calibrated model *should* be
    able to recover a real probability, and a calibration failure here is the
    model's fault rather than the data's.
    """
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, 5))
    w = np.array([1.0, -0.8, 0.6, 0.0, 0.0])
    p = 1.0 / (1.0 + np.exp(-(X @ w)))
    y = (rng.random(n) < p).astype(float)
    ts = np.arange(n, dtype=float) * 60.0 + 1_700_000_000.0
    return X, y, ts


def noise_data(n=3000, seed=3):
    """Features and labels that are independent by construction.

    There is nothing to learn. Any model that appears to learn something here
    has learned the sampling noise, and the promotion check exists to say so.
    """
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, 5))
    y = (rng.random(n) < 0.5).astype(float)
    ts = np.arange(n, dtype=float) * 60.0 + 1_700_000_000.0
    return X, y, ts


def blocky_data(n=4800, block_len=400, offset=200, d=4, seed=0):
    """Overlapping label windows: the shape that makes naive CV lie.

    Samples are grouped into contiguous *events* of ``block_len`` bars. Every
    sample in an event shares one label — the event's outcome — and every sample
    in an event looks like that event (a shared random signature plus a little
    noise). This is a caricature of real bar data, where a 250-bar forward
    return means adjacent samples share almost all of their answer.

    The signatures are random and each event occurs once, so there is **nothing
    generalisable** to learn: an honest evaluation scores 0.5. But a training
    row from the event that straddles the train/validation boundary hands the
    model the answer for the first ``block_len`` validation rows. ``offset``
    exists precisely so an event straddles that boundary.

    ``bars_to_resolution[i]`` is the distance from ``i`` to the last bar of its
    event — which is exactly the information the purge needs.
    """
    rng = np.random.default_rng(seed)
    idx = np.arange(n)
    block = (idx + offset) // block_len
    n_blocks = int(block.max()) + 1
    signature = rng.normal(size=(n_blocks, d)) * 3.0
    label = rng.integers(0, 2, size=n_blocks).astype(float)
    X = signature[block] + rng.normal(size=(n, d)) * 0.05
    y = label[block]
    ends = np.array([idx[block == b].max() for b in range(n_blocks)])
    bars_to_resolution = (ends[block] - idx).astype(float)
    return X, y, bars_to_resolution


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def learnable():
    return learnable_data()


@pytest.fixture(scope="module")
def trained(learnable):
    """A policy trained on a real signal with the SHIPPED default hyper-parameters."""
    X, y, ts = learnable
    p = pol.Policy(feature_names=NAMES, schema_version=SCHEMA)
    p.train(X, y, ts, 12, label_definition="sigmoid(x.w) > u — synthetic")
    return p


@pytest.fixture(scope="module")
def noise_policies():
    """Three noise models, three seeds. One unlucky seed is not a result."""
    out = []
    for seed in (3, 11, 29):
        X, y, ts = noise_data(seed=seed)
        p = pol.Policy(feature_names=NAMES, schema_version=SCHEMA)
        p.train(X, y, ts, 12, label_definition=f"coin flip, seed {seed}")
        out.append(p)
    return out


@pytest.fixture(scope="module")
def saved(trained, tmp_path_factory):
    path = str(tmp_path_factory.mktemp("artefact") / "policy_v1")
    digest = trained.save(path)
    return path, digest


def stub_policy(prob, *, raises=None):
    """A Policy whose estimator is a stub returning a chosen probability.

    Used to exercise the sanity band around ``predict_proba`` without training a
    model that is deliberately broken — the point is the guard, not the estimator.
    """

    class _Stub:
        def predict_proba(self, X):
            if raises is not None:
                raise raises
            return np.array([[1.0 - prob, prob]])

    p = pol.Policy(feature_names=NAMES, schema_version=SCHEMA)
    p._model = _Stub()
    p._promotion = (True, ())
    return p


def vector(value=0.0):
    return [value] * len(NAMES)


# ===========================================================================
# 1. availability of the optional dependency
# ===========================================================================


class TestAvailability:
    """sklearn is optional. Its absence is a state, not a crash."""

    def test_sklearn_is_available_in_this_environment(self):
        assert pol.sklearn_available() is True

    def test_available_means_no_unavailability_reason(self):
        assert pol.unavailability_reason() is None

    def test_availability_flag_and_error_agree(self):
        assert pol.SKLEARN_AVAILABLE == (pol.SKLEARN_IMPORT_ERROR is None)

    def test_sklearn_available_reads_the_global_at_call_time(self, monkeypatch):
        """So that absence can be simulated, and so there is one answer."""
        monkeypatch.setattr(pol, "SKLEARN_AVAILABLE", False)
        assert pol.sklearn_available() is False

    def test_unavailability_reason_is_stable_and_says_restart(self, monkeypatch):
        monkeypatch.setattr(pol, "SKLEARN_AVAILABLE", False)
        first = pol.unavailability_reason()
        second = pol.unavailability_reason()
        assert first == second
        assert "restart" in first.lower()


# ===========================================================================
# 2. metrics
# ===========================================================================


class TestMetrics:
    def test_auc_of_a_perfect_ranking_is_one(self):
        assert pol.roc_auc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]) == pytest.approx(1.0)

    def test_auc_of_a_reversed_ranking_is_zero(self):
        assert pol.roc_auc([0, 0, 1, 1], [0.9, 0.8, 0.2, 0.1]) == pytest.approx(0.0)

    def test_auc_of_constant_scores_is_one_half(self):
        """All ties: mid-ranks make this exactly 0.5, not undefined."""
        assert pol.roc_auc([0, 1, 0, 1], [0.5] * 4) == pytest.approx(0.5)

    def test_auc_handles_partial_ties(self):
        assert pol.roc_auc([0, 1], [0.4, 0.4]) == pytest.approx(0.5)

    def test_auc_of_a_single_class_is_none_not_one_half(self):
        """`None` means undefined; 0.5 means chance. The promotion check needs
        to tell those apart, so they must not share a value."""
        assert pol.roc_auc([1, 1, 1], [0.2, 0.5, 0.9]) is None
        assert pol.roc_auc([0, 0, 0], [0.2, 0.5, 0.9]) is None

    def test_auc_matches_a_brute_force_definition(self):
        rng = np.random.default_rng(5)
        y = (rng.random(200) < 0.4).astype(float)
        s = rng.random(200)
        pos, neg = s[y == 1], s[y == 0]
        brute = np.mean(
            [(1.0 if a > b else 0.5 if a == b else 0.0) for a in pos for b in neg]
        )
        assert pol.roc_auc(y, s) == pytest.approx(brute, abs=1e-12)

    def test_auc_of_empty_input_is_none(self):
        assert pol.roc_auc([], []) is None

    def test_metrics_reject_non_binary_labels(self):
        with pytest.raises(ValueError):
            pol.roc_auc([0, 1, 2], [0.1, 0.2, 0.3])

    def test_metrics_return_none_on_nan(self):
        assert pol.brier_score([0, 1], [0.5, float("nan")]) is None
        assert pol.roc_auc([0, 1], [0.5, float("nan")]) is None

    def test_metrics_return_none_on_length_mismatch(self):
        assert pol.brier_score([0, 1, 1], [0.5, 0.5]) is None

    def test_brier_of_a_perfect_forecast_is_zero(self):
        assert pol.brier_score([0, 1], [0.0, 1.0]) == pytest.approx(0.0)

    def test_brier_of_a_coin_flip_forecast_is_a_quarter(self):
        assert pol.brier_score([0, 1, 0, 1], [0.5] * 4) == pytest.approx(0.25)

    def test_brier_punishes_confident_mistakes(self):
        confident_wrong = pol.brier_score([1, 1], [0.05, 0.05])
        hedged_wrong = pol.brier_score([1, 1], [0.45, 0.45])
        assert confident_wrong > hedged_wrong

    def test_brier_sees_miscalibration_that_auc_cannot(self):
        """The central claim of this module, in two numbers.

        Both forecasters rank perfectly (AUC 1.0). One says 0.99 when it is
        right 50% of the time on the losing side; Brier is what notices.
        """
        y = [0, 0, 1, 1]
        honest = [0.2, 0.3, 0.7, 0.8]
        overconfident = [0.001, 0.002, 0.998, 0.999]
        assert pol.roc_auc(y, honest) == pol.roc_auc(y, overconfident) == 1.0
        # here the over-confident one happens to be right, so Brier prefers it
        assert pol.brier_score(y, overconfident) < pol.brier_score(y, honest)
        # ...and when it is wrong, Brier is brutal about it
        y2 = [1, 1, 0, 0]
        assert pol.brier_score(y2, overconfident) > 1.7 * pol.brier_score(y2, honest)
        # ...and log loss more brutal still, which is why both are reported
        brier_ratio = pol.brier_score(y2, overconfident) / pol.brier_score(y2, honest)
        loss_ratio = pol.log_loss_score(y2, overconfident) / pol.log_loss_score(
            y2, honest
        )
        assert loss_ratio > 2 * brier_ratio

    def test_log_loss_is_finite_even_for_a_certain_miss(self):
        value = pol.log_loss_score([1], [0.0])
        assert value is not None and math.isfinite(value)

    def test_log_loss_of_a_coin_flip_is_ln_two(self):
        assert pol.log_loss_score([0, 1], [0.5, 0.5]) == pytest.approx(math.log(2))

    def test_log_loss_punishes_overconfidence_harder_than_brier(self):
        y = [1, 1, 0, 0]
        over = [0.999, 0.999, 0.999, 0.999]
        mild = [0.6, 0.6, 0.6, 0.6]
        assert (
            pol.log_loss_score(y, over) / pol.log_loss_score(y, mild)
            > pol.brier_score(y, over) / pol.brier_score(y, mild)
        )

    def test_accuracy_at_threshold_uses_the_threshold(self):
        y = [0, 1, 1]
        p = [0.4, 0.6, 0.55]
        assert pol.accuracy_at_threshold(y, p, 0.5) == pytest.approx(1.0)
        assert pol.accuracy_at_threshold(y, p, 0.58) == pytest.approx(2 / 3)

    def test_accuracy_is_decoration_on_an_unbalanced_label(self):
        """51% accuracy from always saying 'up' is not information."""
        y = [1] * 51 + [0] * 49
        always_up = [0.99] * 100
        assert pol.accuracy_at_threshold(y, always_up, 0.5) == pytest.approx(0.51)
        assert pol.roc_auc(y, always_up) == pytest.approx(0.5)


class TestExpectedEdge:
    """The advisory mirror of ``risk_management._gate_expected_edge``."""

    def test_edge_matches_the_gate_arithmetic(self):
        assert pol.expected_edge_bps(0.6, 40.0, 30.0, 25.0) == pytest.approx(
            0.6 * 40 - 0.4 * 30 - 25
        )

    def test_edge_is_none_for_a_none_probability(self):
        assert pol.expected_edge_bps(None, 40.0, 30.0, 25.0) is None

    def test_edge_is_none_for_a_probability_outside_zero_one(self):
        assert pol.expected_edge_bps(1.4, 40.0, 30.0, 25.0) is None
        assert pol.expected_edge_bps(-0.1, 40.0, 30.0, 25.0) is None

    def test_edge_is_none_for_nan_inputs(self):
        assert pol.expected_edge_bps(0.6, float("nan"), 30.0, 25.0) is None
        assert pol.expected_edge_bps(float("nan"), 30.0, 30.0, 25.0) is None

    def test_overconfidence_is_worth_real_basis_points(self):
        """The number that motivates the whole module."""
        claimed = pol.expected_edge_bps(0.80, 40.0, 30.0, 25.0)
        truth = pol.expected_edge_bps(0.55, 40.0, 30.0, 25.0)
        assert claimed > 0 > truth
        assert claimed - truth > 15.0


# ===========================================================================
# 3. the reliability table
# ===========================================================================


class TestCalibrationReport:
    def test_a_perfectly_calibrated_forecast_has_near_zero_error(self):
        rng = np.random.default_rng(1)
        p = rng.random(20_000)
        y = (rng.random(20_000) < p).astype(float)
        rep = pol.calibration_report(y, p)
        assert rep.expected_calibration_error < 0.02
        assert rep.max_calibration_error < 0.05

    def test_a_systematically_overconfident_forecast_is_caught(self):
        rng = np.random.default_rng(2)
        truth = rng.random(20_000) * 0.6 + 0.2
        y = (rng.random(20_000) < truth).astype(float)
        stretched = np.clip((truth - 0.5) * 2.5 + 0.5, 0.0, 1.0)
        rep = pol.calibration_report(y, stretched)
        assert rep.expected_calibration_error > 0.05

    def test_empty_buckets_report_none_not_zero(self):
        rep = pol.calibration_report([0, 1], [0.51, 0.52])
        empty = [b for b in rep.buckets if b.count == 0]
        assert empty, "expected some empty buckets"
        assert all(b.mean_predicted is None for b in empty)
        assert all(b.observed_frequency is None for b in empty)
        assert all(b.gap is None for b in empty)

    def test_buckets_partition_the_unit_interval(self):
        rep = pol.calibration_report([0, 1], [0.0, 1.0], n_buckets=10)
        assert len(rep.buckets) == 10
        assert rep.buckets[0].lower == 0.0
        assert rep.buckets[-1].upper == 1.0
        assert sum(b.count for b in rep.buckets) == 2

    def test_probability_of_exactly_one_lands_in_the_last_bucket(self):
        rep = pol.calibration_report([1], [1.0])
        assert rep.buckets[-1].count == 1

    def test_gap_sign_says_which_way_the_model_is_wrong(self):
        rep = pol.calibration_report([0] * 90 + [1] * 10, [0.5] * 100)
        bucket = [b for b in rep.buckets if b.count == 100][0]
        assert bucket.gap > 0, "predicted above realised means over-confident"

    def test_report_of_no_data_is_empty_not_zero(self):
        rep = pol.calibration_report([], [])
        assert rep.n_samples == 0
        assert rep.base_rate is None
        assert rep.expected_calibration_error is None
        assert rep.brier is None

    def test_format_table_is_printable_and_names_its_columns(self):
        rep = pol.calibration_report([0, 1, 1], [0.2, 0.7, 0.9])
        table = rep.format_table()
        assert "predicted" in table and "realised" in table and "gap" in table
        assert "brier=" in table and "ece=" in table
        assert str(rep) == table

    def test_format_table_prints_a_dash_for_empty_buckets(self):
        rep = pol.calibration_report([0, 1], [0.51, 0.52])
        assert "-" in rep.format_table()

    def test_monotone_detects_a_monotone_curve(self):
        rng = np.random.default_rng(4)
        p = rng.random(20_000)
        y = (rng.random(20_000) < p).astype(float)
        assert pol.calibration_report(y, p).is_monotone() is True

    def test_monotone_detects_an_inverted_curve(self):
        rng = np.random.default_rng(4)
        p = rng.random(20_000)
        y = (rng.random(20_000) < (1.0 - p)).astype(float)
        assert pol.calibration_report(y, p).is_monotone() is False

    def test_monotone_of_a_single_populated_bucket_is_false(self):
        """Monotonicity of one point is not a property to be reassured by."""
        rep = pol.calibration_report([0, 1] * 50, [0.5] * 100)
        assert rep.is_monotone() is False

    def test_monotone_ignores_thinly_populated_buckets(self):
        rng = np.random.default_rng(6)
        p = np.concatenate([rng.random(5000), [0.05]])
        y = np.concatenate([(rng.random(5000) < p[:5000]).astype(float), [1.0]])
        rep = pol.calibration_report(y, p)
        assert rep.is_monotone(min_count=50) is True

    def test_report_round_trips_through_a_dict(self):
        rep = pol.calibration_report([0, 1, 1, 0], [0.2, 0.7, 0.9, 0.4])
        back = pol.CalibrationReport.from_dict(rep.to_dict())
        assert back.to_dict() == rep.to_dict()


# ===========================================================================
# 4. purged, embargoed walk-forward — asserted on indices
# ===========================================================================


class TestPurgedWalkForward:
    """The leak defence, verified structurally.

    Every assertion here is about *which rows* are in which set. A defence that
    can only be checked through a score is not a defence anyone can maintain.
    """

    def test_folds_are_contiguous_and_walk_forward(self):
        folds = pol.purged_walk_forward_folds(1000, 0, n_folds=4, embargo_bars=0)
        assert len(folds) == 4
        for f in folds:
            assert f.val_index[0] == f.val_start
            assert f.val_index[-1] == f.val_stop - 1
            assert np.all(np.diff(f.val_index) == 1)

    def test_validation_blocks_are_disjoint_and_ordered(self):
        folds = pol.purged_walk_forward_folds(1000, 0, n_folds=4, embargo_bars=0)
        for a, b in zip(folds, folds[1:]):
            assert a.val_stop == b.val_start

    def test_training_is_always_strictly_before_validation(self):
        folds = pol.purged_walk_forward_folds(1000, 25, n_folds=4, embargo_bars=3)
        for f in folds:
            assert f.train_index.max() < f.val_start

    def test_the_window_expands(self):
        folds = pol.purged_walk_forward_folds(1000, 0, n_folds=4, embargo_bars=0)
        sizes = [f.n_train for f in folds]
        assert sizes == sorted(sizes) and sizes[0] < sizes[-1]

    def test_with_no_horizon_and_no_embargo_nothing_is_removed(self):
        folds = pol.purged_walk_forward_folds(1000, 0, n_folds=4, embargo_bars=0)
        for f in folds:
            assert f.n_purged == 0 and f.n_embargoed == 0
            np.testing.assert_array_equal(f.train_index, np.arange(f.val_start))

    def test_purging_removes_exactly_the_overlapping_indices(self):
        """The headline assertion: the leaked rows, by index.

        With a constant 50-bar horizon, sample ``i`` resolves at ``i+50``. Every
        training row with ``i + 50 >= val_start`` overlaps the validation block
        and must be gone; every row below that must survive.
        """
        n, horizon = 1000, 50
        folds = pol.purged_walk_forward_folds(
            n, horizon, n_folds=4, embargo_bars=0, purge=True
        )
        for f in folds:
            leaked = np.arange(max(0, f.val_start - horizon), f.val_start)
            np.testing.assert_array_equal(f.purged_index, leaked)
            assert not set(leaked) & set(f.train_index.tolist())
            np.testing.assert_array_equal(
                f.train_index, np.arange(max(0, f.val_start - horizon))
            )

    def test_the_purged_rows_really_do_resolve_inside_the_validation_block(self):
        n, horizon = 1000, 50
        folds = pol.purged_walk_forward_folds(n, horizon, n_folds=4, embargo_bars=0)
        for f in folds:
            for i in f.purged_index:
                assert i + horizon >= f.val_start
            for i in f.train_index:
                assert i + horizon < f.val_start

    def test_purging_honours_a_per_sample_horizon(self):
        """Rows resolve at different times; only the overlapping ones go."""
        n = 400
        horizon = np.zeros(n)
        horizon[195] = 10       # resolves at 205 -> inside the first val block
        horizon[150] = 5        # resolves at 155 -> safely before it
        folds = pol.purged_walk_forward_folds(n, horizon, n_folds=1, embargo_bars=0)
        f = folds[0]
        assert f.val_start == 200
        assert set(f.purged_index.tolist()) == {195}
        assert 150 in set(f.train_index.tolist())

    def test_a_long_horizon_can_purge_the_entire_training_set(self):
        folds = pol.purged_walk_forward_folds(400, 10_000, n_folds=1, embargo_bars=0)
        assert folds[0].n_train == 0
        assert folds[0].n_purged == 200

    def test_the_embargo_removes_a_further_gap(self):
        n, embargo = 1000, 7
        folds = pol.purged_walk_forward_folds(
            n, 0, n_folds=4, embargo_bars=embargo, purge=True
        )
        for f in folds:
            expected = np.arange(f.val_start - embargo, f.val_start)
            np.testing.assert_array_equal(f.embargoed_index, expected)
            assert f.train_index.max() == f.val_start - embargo - 1

    def test_purged_and_embargoed_sets_are_disjoint(self):
        folds = pol.purged_walk_forward_folds(1000, 30, n_folds=4, embargo_bars=10)
        for f in folds:
            assert not set(f.purged_index.tolist()) & set(f.embargoed_index.tolist())

    def test_every_candidate_is_either_trained_purged_or_embargoed(self):
        folds = pol.purged_walk_forward_folds(1000, 30, n_folds=4, embargo_bars=10)
        for f in folds:
            union = (
                set(f.train_index.tolist())
                | set(f.purged_index.tolist())
                | set(f.embargoed_index.tolist())
            )
            assert union == set(range(f.val_start))

    def test_disabling_the_purge_keeps_the_overlapping_rows(self):
        folds = pol.purged_walk_forward_folds(
            1000, 50, n_folds=4, embargo_bars=0, purge=False
        )
        for f in folds:
            assert f.n_purged == 0
            np.testing.assert_array_equal(f.train_index, np.arange(f.val_start))

    def test_the_embargo_still_applies_when_purging_is_off(self):
        folds = pol.purged_walk_forward_folds(
            1000, 50, n_folds=4, embargo_bars=5, purge=False
        )
        assert all(f.n_embargoed == 5 for f in folds)

    def test_a_scalar_horizon_broadcasts(self):
        a = pol.purged_walk_forward_folds(500, 20, n_folds=2)
        b = pol.purged_walk_forward_folds(500, np.full(500, 20), n_folds=2)
        for x, y in zip(a, b):
            np.testing.assert_array_equal(x.train_index, y.train_index)

    def test_a_fractional_horizon_floors_rather_than_rounds(self):
        """A label that resolves 'somewhere in bar 50' is not resolved at 51."""
        f = pol.purged_walk_forward_folds(400, 20.9, n_folds=1, embargo_bars=0)[0]
        assert f.n_purged == 20

    def test_a_negative_horizon_is_refused(self):
        with pytest.raises(ValueError, match="negative"):
            pol.purged_walk_forward_folds(400, -1, n_folds=1)

    def test_a_nan_horizon_is_refused(self):
        with pytest.raises(ValueError, match="non-finite"):
            pol.purged_walk_forward_folds(400, float("nan"), n_folds=1)

    def test_a_wrong_length_horizon_is_refused(self):
        with pytest.raises(ValueError, match="entries"):
            pol.purged_walk_forward_folds(400, np.zeros(399), n_folds=1)

    def test_too_few_samples_for_the_fold_count_is_refused(self):
        with pytest.raises(ValueError, match="cannot be split"):
            pol.purged_walk_forward_folds(3, 0, n_folds=5)

    def test_a_negative_embargo_is_refused(self):
        with pytest.raises(ValueError, match="embargo"):
            pol.purged_walk_forward_folds(400, 0, n_folds=2, embargo_bars=-1)

    def test_zero_folds_is_refused(self):
        with pytest.raises(ValueError, match="n_folds"):
            pol.purged_walk_forward_folds(400, 0, n_folds=0)


# ===========================================================================
# 5. the leak is measurable
# ===========================================================================


def _pooled_auc(seed, purge):
    """Pooled out-of-sample AUC of a bare estimator over the same folds.

    Deliberately *not* routed through ``Policy.train``: the property under test
    belongs to the fold construction, and stripping the calibration wrapper
    isolates it, keeps the test deterministic, and keeps it fast.
    """
    from sklearn.ensemble import HistGradientBoostingClassifier

    X, y, horizon = blocky_data(seed=seed)
    folds = pol.purged_walk_forward_folds(
        len(y), horizon, n_folds=3, embargo_bars=0, purge=purge
    )
    truths, scores = [], []
    for f in folds:
        if np.unique(y[f.train_index]).size < 2:
            continue
        model = HistGradientBoostingClassifier(**FAST_HP)
        model.fit(X[f.train_index], y[f.train_index])
        scores.append(model.predict_proba(X[f.val_index])[:, 1])
        truths.append(y[f.val_index])
    return pol.roc_auc(np.concatenate(truths), np.concatenate(scores))


@pytest.fixture(scope="module")
def leak_experiment():
    seeds = range(6)
    return (
        [_pooled_auc(s, purge=True) for s in seeds],
        [_pooled_auc(s, purge=False) for s in seeds],
    )


class TestTheLeakIsReal:
    """Evidence that the purge is doing something, not just costing rows.

    On data whose labels overlap, the un-purged evaluation scores materially
    better than the purged one — on data with **no learnable signal at all**.
    Every point of that difference is the model reading the answer. This is the
    mechanism behind published crypto ML results that do not survive contact
    with a live account.
    """

    def test_the_unpurged_evaluation_scores_better(self, leak_experiment):
        purged, leaky = leak_experiment
        assert np.mean(leaky) > np.mean(purged) + 0.05

    def test_the_advantage_is_not_one_lucky_seed(self, leak_experiment):
        purged, leaky = leak_experiment
        wins = sum(1 for a, b in zip(purged, leaky) if b > a)
        assert wins >= 5, f"leaky beat purged in only {wins}/6 seeds"

    def test_the_purged_evaluation_is_honest_about_having_no_signal(
        self, leak_experiment
    ):
        """There is nothing to learn here, and purged evaluation says so."""
        purged, _ = leak_experiment
        assert abs(np.mean(purged) - 0.5) < 0.05

    def test_the_leak_carries_the_indices_it_should(self):
        X, y, horizon = blocky_data(seed=0)
        purged = pol.purged_walk_forward_folds(
            len(y), horizon, n_folds=3, embargo_bars=0, purge=True
        )
        leaky = pol.purged_walk_forward_folds(
            len(y), horizon, n_folds=3, embargo_bars=0, purge=False
        )
        for p_fold, l_fold in zip(purged, leaky):
            extra = sorted(set(l_fold.train_index.tolist()) - set(p_fold.train_index.tolist()))
            assert extra, "the leaky split should keep rows the purge removes"
            # every extra row shares its label with a validation row
            for i in extra:
                assert i + horizon[i] >= p_fold.val_start


# ===========================================================================
# 6. THE test: a noise model must not be promoted
# ===========================================================================


class TestANoiseModelIsRefused:
    """The most important test in this file.

    A system that promotes a model trained on pure noise will promote anything,
    and every downstream safety property becomes decoration. The data here has
    labels independent of features by construction, so the *only* correct
    verdict is refusal — and the refusal must be earned by measurement, not by
    luck, which is why three seeds are checked.

    Note what actually fails. The noise model's *calibration* is typically fine:
    it learns to say "about 0.5" and it is right about 0.5 of the time. It fails
    on discrimination (AUC at chance) and on Brier skill (it cannot beat a
    constant). That is the reason promotion is a conjunction rather than a
    single number: a well-calibrated model with no information is still worth
    nothing, and would otherwise sail through a calibration-only check.
    """

    def test_a_noise_model_is_refused(self, noise_policies):
        for p in noise_policies:
            ok, reasons = p.meets_promotion_criteria()
            assert ok is False
            assert reasons

    def test_the_refusal_names_the_measurement_that_failed(self, noise_policies):
        for p in noise_policies:
            _, reasons = p.meets_promotion_criteria()
            joined = " ".join(reasons)
            assert "AUC" in joined or "BRIER" in joined

    def test_a_noise_model_has_no_out_of_sample_discrimination(self, noise_policies):
        for p in noise_policies:
            assert abs(p.training_report.auc_mean - 0.5) < 0.05

    def test_a_noise_model_cannot_beat_the_base_rate_forecast(self, noise_policies):
        """Brier skill <= 0: the model adds nothing a constant does not."""
        for p in noise_policies:
            assert p.training_report.brier_skill < 0.01

    def test_a_noise_model_refuses_to_predict_at_all(self, noise_policies):
        """Refusal is not advisory: an unpromoted model produces no probability."""
        for p in noise_policies:
            d = p.predict_edge(vector())
            assert d.usable is False
            assert d.probability is None
            assert d.reason == "NOT_PROMOTED"

    def test_a_noise_model_is_still_measured_not_crashed(self, noise_policies):
        """It trains, it reports, it is simply not allowed near a trade."""
        for p in noise_policies:
            rep = p.training_report
            assert rep.n_samples == 3000
            assert len(rep.usable_folds) == pol.DEFAULT_N_FOLDS
            assert rep.brier_mean is not None


# ===========================================================================
# 7. a real signal is learned, promoted, and well calibrated
# ===========================================================================


class TestALearnableSignalIsPromoted:
    def test_it_is_promoted_with_no_objections(self, trained):
        ok, reasons = trained.meets_promotion_criteria()
        assert ok is True, reasons
        assert reasons == ()

    def test_it_discriminates_in_every_fold(self, trained):
        rep = trained.training_report
        assert rep.auc_mean > 0.7
        assert all(f.auc > 0.7 for f in rep.usable_folds)

    def test_it_beats_the_base_rate_forecast_on_brier(self, trained):
        rep = trained.training_report
        assert rep.brier_mean < rep.baseline_brier_mean
        assert rep.brier_skill > 0.1

    def test_its_calibration_is_measurably_good(self, trained):
        rep = trained.reliability()
        assert rep.expected_calibration_error < 0.05
        assert rep.max_calibration_error < 0.10

    def test_its_calibration_buckets_are_monotone(self, trained):
        assert trained.reliability().is_monotone(min_count=20) is True

    def test_the_reliability_table_is_out_of_sample(self, trained):
        """It must be built from the walk-forward predictions, not from a refit.

        Sample count is the tell: only the validation blocks contribute, so the
        table covers fewer rows than the training set.
        """
        rep = trained.reliability()
        assert 0 < rep.n_samples < trained.training_report.n_samples

    def test_predicted_probabilities_track_realised_frequency(self, trained):
        for b in trained.reliability().populated(min_count=50):
            assert abs(b.gap) < 0.12

    def test_probabilities_span_a_useful_range(self, trained):
        """A model that only ever says 0.49-0.51 clears every calibration check
        and is useless; the promotion criteria lean on AUC to catch that, and
        here we confirm the model actually commits."""
        populated = [b for b in trained.reliability().buckets if b.count > 20]
        assert populated[0].lower < 0.3 and populated[-1].upper > 0.7

    def test_all_probabilities_are_inside_zero_one(self, trained, learnable):
        X, _, _ = learnable
        probs = trained.predict_proba(X[:500])
        assert np.all(probs >= 0.0) and np.all(probs <= 1.0)

    def test_the_report_summary_leads_with_brier(self, trained):
        summary = trained.training_report.summary()
        assert summary.index("brier=") < summary.index("auc")

    def test_the_defaults_are_what_produced_this(self, trained):
        assert trained.hyperparameters == pol.DEFAULT_HYPERPARAMETERS
        assert trained.calibration_method == pol.DEFAULT_CALIBRATION_METHOD


# ===========================================================================
# 8. promotion criteria
# ===========================================================================


class TestPromotionCriteria:
    """The default is to refuse. Each criterion is checked in isolation."""

    def test_an_untrained_policy_is_refused(self):
        p = pol.Policy(feature_names=NAMES, schema_version=SCHEMA)
        ok, reasons = p.meets_promotion_criteria()
        assert ok is False and reasons == ("NOT_TRAINED",)

    def test_a_fresh_policy_caches_a_refusal(self):
        p = pol.Policy(feature_names=NAMES, schema_version=SCHEMA)
        assert p.promotion == (False, ("NOT_TRAINED",))

    def test_without_sklearn_promotion_is_refused(self, trained, monkeypatch):
        monkeypatch.setattr(pol, "SKLEARN_AVAILABLE", False)
        ok, reasons = trained.meets_promotion_criteria()
        assert ok is False and reasons == ("SKLEARN_UNAVAILABLE",)

    def test_too_few_samples_is_refused(self, trained):
        crit = pol.PromotionCriteria(min_samples=1_000_000)
        ok, reasons = trained.meets_promotion_criteria(crit)
        assert ok is False
        assert any("INSUFFICIENT_SAMPLES" in r for r in reasons)

    def test_too_few_usable_folds_is_refused(self, trained):
        crit = pol.PromotionCriteria(min_usable_folds=99)
        ok, reasons = trained.meets_promotion_criteria(crit)
        assert ok is False
        assert any("INSUFFICIENT_FOLDS" in r for r in reasons)

    def test_a_high_auc_bar_is_refused(self, trained):
        ok, reasons = trained.meets_promotion_criteria(
            pol.PromotionCriteria(min_auc_mean=0.99)
        )
        assert ok is False
        assert any("AUC_MEAN_TOO_LOW" in r for r in reasons)

    def test_one_weak_fold_sinks_a_good_average(self, trained):
        """'Excellent in one fold, useless in three' is not a model.

        The per-fold floor is what distinguishes that from a real edge, so it is
        checked directly against a doctored report rather than through the mean.
        """
        rep = trained.training_report
        folds = list(rep.folds)
        folds[1] = pol.FoldMetrics(
            **{**folds[1].to_dict(), "auc": 0.50, "brier": folds[1].brier}
        )
        doctored = pol.TrainingReport.from_dict({**rep.to_dict(), "folds": [f.to_dict() for f in folds]})
        p = pol.Policy(feature_names=NAMES, schema_version=SCHEMA)
        p._model = object()
        p._report = doctored
        ok, reasons = p.meets_promotion_criteria()
        assert ok is False
        assert any("AUC_INCONSISTENT_ACROSS_FOLDS" in r for r in reasons)

    def test_a_high_brier_skill_bar_is_refused(self, trained):
        ok, reasons = trained.meets_promotion_criteria(
            pol.PromotionCriteria(min_brier_skill=0.99)
        )
        assert ok is False
        assert any("BRIER_NO_BETTER_THAN_BASE_RATE" in r for r in reasons)

    def test_a_tight_calibration_bar_is_refused(self, trained):
        ok, reasons = trained.meets_promotion_criteria(
            pol.PromotionCriteria(max_calibration_error=0.0001)
        )
        assert ok is False
        assert any("MISCALIBRATED" in r for r in reasons)

    def test_a_degenerate_label_balance_is_refused(self, trained):
        ok, reasons = trained.meets_promotion_criteria(
            pol.PromotionCriteria(min_base_rate=0.9)
        )
        assert ok is False
        assert any("DEGENERATE_LABEL_BALANCE" in r for r in reasons)

    def test_every_failure_is_reported_not_just_the_first(self, trained):
        crit = pol.PromotionCriteria(
            min_samples=10**9, min_auc_mean=0.99, min_brier_skill=0.99
        )
        _, reasons = trained.meets_promotion_criteria(crit)
        assert len(reasons) >= 3

    def test_success_returns_an_empty_reason_tuple(self, trained):
        assert trained.meets_promotion_criteria() == (True, ())

    def test_the_shipped_defaults_are_the_documented_ones(self):
        c = pol.PromotionCriteria()
        assert c.min_usable_folds == 3
        assert c.min_samples == 1000
        assert c.min_auc_mean == 0.55
        assert c.min_auc_per_fold == 0.52
        assert c.min_brier_skill == 0.01
        assert c.max_calibration_error == 0.05
        assert (c.min_base_rate, c.max_base_rate) == (0.05, 0.95)

    def test_the_per_fold_floor_is_below_the_mean_floor(self):
        """Otherwise the per-fold check would be the only one that ever binds."""
        c = pol.PromotionCriteria()
        assert c.min_auc_per_fold < c.min_auc_mean

    def test_criteria_round_trip_through_a_dict(self):
        c = pol.PromotionCriteria(min_samples=77, min_auc_mean=0.61)
        assert pol.PromotionCriteria.from_dict(c.to_dict()) == c

    def test_setting_criteria_re_evaluates_immediately(self, trained):
        p = copy.copy(trained)
        ok, _ = p.set_promotion_criteria(pol.PromotionCriteria(min_auc_mean=0.99))
        assert ok is False and p.promotion[0] is False
        ok, _ = p.set_promotion_criteria(pol.PromotionCriteria())
        assert ok is True

    def test_setting_a_non_criteria_object_is_refused(self, trained):
        with pytest.raises(TypeError):
            copy.copy(trained).set_promotion_criteria({"min_samples": 1})


# ===========================================================================
# 9. PolicyDecision invariants
# ===========================================================================


class TestPolicyDecisionInvariants:
    """``usable=False`` implies ``probability is None``, structurally."""

    def test_the_default_decision_is_unusable(self):
        d = pol.PolicyDecision()
        assert d.usable is False
        assert d.probability is None
        assert d.reason == pol.REASON_UNEVALUATED

    def test_an_unusable_decision_cannot_carry_a_probability(self):
        with pytest.raises(ValueError, match="probability None"):
            pol.PolicyDecision(usable=False, probability=0.5)

    def test_a_usable_decision_cannot_carry_none(self):
        with pytest.raises(ValueError, match="finite probability"):
            pol.PolicyDecision(usable=True, probability=None)

    def test_a_usable_decision_cannot_carry_nan(self):
        with pytest.raises(ValueError):
            pol.PolicyDecision(usable=True, probability=float("nan"))

    def test_a_usable_decision_cannot_carry_a_probability_outside_zero_one(self):
        with pytest.raises(ValueError, match=r"outside \[0, 1\]"):
            pol.PolicyDecision(usable=True, probability=1.2)

    def test_unusable_factory_never_produces_a_probability(self):
        d = pol.PolicyDecision.unusable("ANYTHING", extra=1)
        assert d.probability is None and d.usable is False
        assert d.detail["extra"] == 1

    def test_truthiness_is_usability(self):
        assert bool(pol.PolicyDecision(usable=True, probability=0.5)) is True
        assert bool(pol.PolicyDecision.unusable("NOPE")) is False

    def test_a_flat_decision_is_usable_but_proposes_nothing(self):
        d = pol.PolicyDecision(
            usable=True, probability=0.5, direction=pol.Direction.FLAT
        )
        assert bool(d) is True
        assert d.proposes_trade is False

    def test_probability_in_direction_flips_for_a_short(self):
        d = pol.PolicyDecision(
            usable=True, probability=0.3, direction=pol.Direction.SHORT
        )
        assert d.probability_in_direction == pytest.approx(0.7)

    def test_probability_in_direction_is_none_when_flat(self):
        d = pol.PolicyDecision(usable=True, probability=0.5)
        assert d.probability_in_direction is None

    def test_probability_in_direction_is_none_when_unusable(self):
        assert pol.PolicyDecision.unusable("X").probability_in_direction is None

    def test_edge_estimate_is_none_for_an_unusable_decision(self):
        assert pol.PolicyDecision.unusable("X").expected_edge_bps(40, 30, 25) is None

    def test_edge_estimate_uses_the_directional_probability(self):
        d = pol.PolicyDecision(
            usable=True, probability=0.2, direction=pol.Direction.SHORT
        )
        assert d.expected_edge_bps(40, 30, 25) == pytest.approx(0.8 * 40 - 0.2 * 30 - 25)

    def test_a_decision_serialises(self):
        d = pol.PolicyDecision(
            usable=True, probability=0.6, direction=pol.Direction.LONG
        )
        raw = d.to_dict()
        assert raw["direction"] == "long"
        assert json.loads(json.dumps(raw))["probability"] == pytest.approx(0.6)

    def test_direction_flat_is_a_first_class_value(self):
        assert pol.Direction.FLAT.value == "flat"
        assert set(pol.Direction) == {
            pol.Direction.LONG,
            pol.Direction.SHORT,
            pol.Direction.FLAT,
        }


# ===========================================================================
# 10. prediction fails closed
# ===========================================================================


class TestPredictionFailsClosed:
    def test_a_good_vector_produces_a_usable_probability(self, trained, learnable):
        X, _, _ = learnable
        d = trained.predict_edge(X[0])
        assert d.usable is True
        assert 0.0 <= d.probability <= 1.0
        assert d.reason in ("OK", "NO_DIRECTIONAL_EDGE")

    def test_a_mapping_and_an_ordered_vector_agree(self, trained, learnable):
        X, _, _ = learnable
        by_order = trained.predict_edge(X[7])
        by_name = trained.predict_edge(
            {name: X[7][i] for i, name in enumerate(NAMES)}
        )
        assert by_name.probability == pytest.approx(by_order.probability)

    def test_a_mapping_is_order_independent(self, trained, learnable):
        """The safe calling convention: the caller cannot get the order wrong."""
        X, _, _ = learnable
        forward = {name: X[7][i] for i, name in enumerate(NAMES)}
        backward = dict(reversed(list(forward.items())))
        assert trained.predict_edge(backward).probability == pytest.approx(
            trained.predict_edge(forward).probability
        )

    def test_a_nan_feature_gives_an_unusable_decision(self, trained, learnable):
        X, _, _ = learnable
        bad = X[0].copy()
        bad[2] = float("nan")
        d = trained.predict_edge(bad)
        assert d.usable is False
        assert d.probability is None
        assert d.reason == "FEATURE_NOT_FINITE"
        assert d.detail["features"] == ["f2"]

    def test_an_infinite_feature_gives_an_unusable_decision(self, trained, learnable):
        X, _, _ = learnable
        bad = X[0].copy()
        bad[0] = float("inf")
        assert trained.predict_edge(bad).reason == "FEATURE_NOT_FINITE"

    def test_a_nan_in_a_mapping_is_caught_too(self, trained):
        d = trained.predict_edge({n: (float("nan") if n == "f1" else 0.0) for n in NAMES})
        assert d.reason == "FEATURE_NOT_FINITE"
        assert d.probability is None

    def test_a_missing_feature_names_itself(self, trained):
        partial = {n: 0.0 for n in NAMES if n != "f3"}
        d = trained.predict_edge(partial)
        assert d.reason == "FEATURE_MISSING"
        assert d.detail["missing"] == ["f3"]
        assert d.probability is None

    def test_extra_mapping_keys_are_ignored_not_fatal(self, trained, learnable):
        X, _, _ = learnable
        base = {name: X[3][i] for i, name in enumerate(NAMES)}
        d = trained.predict_edge({**base, "f_unknown": 1.0})
        assert d.usable is True

    def test_a_short_vector_is_refused(self, trained):
        d = trained.predict_edge([0.0, 0.0])
        assert d.reason == "FEATURE_COUNT_MISMATCH"
        assert d.detail == {"got": 2, "expected": 5}

    def test_a_long_vector_is_refused(self, trained):
        assert trained.predict_edge([0.0] * 9).reason == "FEATURE_COUNT_MISMATCH"

    def test_a_matrix_is_refused(self, trained):
        d = trained.predict_edge(np.zeros((3, 5)))
        assert d.reason == "FEATURE_SHAPE_INVALID"

    def test_a_single_row_matrix_is_accepted(self, trained, learnable):
        X, _, _ = learnable
        assert trained.predict_edge(X[0:1]).usable is True

    def test_a_schema_version_mismatch_is_refused(self, trained):
        d = trained.predict_edge(vector(), schema_version="features/v2")
        assert d.usable is False
        assert d.reason == "SCHEMA_VERSION_MISMATCH"
        assert d.detail["expected"] == SCHEMA

    def test_reordered_feature_names_are_refused_at_predict_time(self, trained):
        d = trained.predict_edge(vector(), feature_names=tuple(reversed(NAMES)))
        assert d.usable is False
        assert d.reason == "FEATURE_NAMES_MISMATCH"
        assert d.detail["reordered"] is True

    def test_renamed_features_are_refused_and_not_called_a_reorder(self, trained):
        d = trained.predict_edge(vector(), feature_names=("a", "b", "c", "d", "e"))
        assert d.reason == "FEATURE_NAMES_MISMATCH"
        assert d.detail["reordered"] is False

    def test_matching_names_and_schema_are_accepted(self, trained, learnable):
        X, _, _ = learnable
        d = trained.predict_edge(X[1], feature_names=NAMES, schema_version=SCHEMA)
        assert d.usable is True

    def test_no_model_loaded_gives_no_probability(self):
        p = pol.Policy(feature_names=NAMES, schema_version=SCHEMA)
        d = p.predict_edge(vector())
        assert d.usable is False
        assert d.probability is None
        assert d.reason == "NO_MODEL_LOADED"

    def test_no_model_loaded_never_returns_a_default_of_one_half(self):
        p = pol.Policy(feature_names=NAMES, schema_version=SCHEMA)
        for features in (vector(), {n: 0.0 for n in NAMES}, np.zeros(5)):
            assert p.predict_edge(features).probability is None

    def test_an_unpromoted_model_refuses_and_says_why(self, noise_policies):
        d = noise_policies[0].predict_edge(vector())
        assert d.reason == "NOT_PROMOTED"
        assert d.detail["reasons"]

    def test_the_promotion_gate_can_be_opened_for_research_only(self, noise_policies):
        p = copy.copy(noise_policies[0])
        p.require_promotion = False
        d = p.predict_edge(vector())
        assert d.usable is True

    def test_a_probability_outside_zero_one_is_refused(self):
        d = stub_policy(1.4).predict_edge(vector())
        assert d.usable is False
        assert d.reason == "PROBABILITY_OUT_OF_RANGE"
        assert d.probability is None

    def test_a_nan_probability_is_refused(self):
        d = stub_policy(float("nan")).predict_edge(vector())
        assert d.reason == "PROBABILITY_OUT_OF_RANGE"

    def test_implausible_certainty_is_refused(self):
        """No honest model of a market bar is 99.9999% sure."""
        d = stub_policy(1.0).predict_edge(vector())
        assert d.usable is False
        assert d.reason == "IMPLAUSIBLE_CERTAINTY"
        assert d.probability is None

    def test_implausible_certainty_is_refused_on_the_low_side_too(self):
        assert stub_policy(0.0).predict_edge(vector()).reason == "IMPLAUSIBLE_CERTAINTY"

    def test_a_merely_confident_probability_is_allowed(self):
        d = stub_policy(0.97).predict_edge(vector())
        assert d.usable is True and d.probability == pytest.approx(0.97)

    def test_an_estimator_that_raises_becomes_an_unusable_decision(self):
        d = stub_policy(0.5, raises=RuntimeError("boom")).predict_edge(vector())
        assert d.usable is False
        assert d.reason == "PREDICT_FAILED"
        assert "boom" in d.detail["error"]

    def test_predict_edge_never_raises(self, trained):
        """A trading loop must not have to wrap the model in a try/except."""
        for bad in (None, "nonsense", [], {}, object(), np.zeros((2, 2, 2))):
            d = trained.predict_edge(bad)
            assert d.usable is False and d.probability is None

    def test_without_sklearn_prediction_is_unusable(self, trained, monkeypatch):
        monkeypatch.setattr(pol, "SKLEARN_AVAILABLE", False)
        d = trained.predict_edge(vector())
        assert d.usable is False
        assert d.reason == "SKLEARN_UNAVAILABLE"
        assert d.probability is None

    def test_direction_follows_the_threshold(self, trained):
        assert stub_policy(0.8).predict_edge(vector()).direction is pol.Direction.LONG
        assert stub_policy(0.2).predict_edge(vector()).direction is pol.Direction.SHORT
        assert stub_policy(0.5).predict_edge(vector()).direction is pol.Direction.FLAT

    def test_a_flat_decision_says_so_in_its_reason(self):
        d = stub_policy(0.5).predict_edge(vector())
        assert d.reason == "NO_DIRECTIONAL_EDGE"
        assert d.proposes_trade is False

    def test_a_dead_band_threshold_widens_the_flat_zone(self):
        p = stub_policy(0.6)
        p.threshold = 0.65
        assert p.predict_edge(vector()).direction is pol.Direction.FLAT

    def test_every_decision_carries_the_schema_it_was_made_under(self, trained):
        for features in (vector(), [1.0], {"f0": float("nan")}):
            assert trained.predict_edge(features).schema_version == SCHEMA


class TestBatchPrediction:
    def test_batch_probabilities_are_in_range(self, trained, learnable):
        X, _, _ = learnable
        p = trained.predict_proba(X[:200])
        assert p.shape == (200,)
        assert np.all((p >= 0.0) & (p <= 1.0))

    def test_batch_prediction_without_a_model_is_none_not_a_default(self):
        p = pol.Policy(feature_names=NAMES, schema_version=SCHEMA)
        assert p.predict_proba(np.zeros((3, 5))) is None

    def test_batch_prediction_without_sklearn_is_none(self, trained, monkeypatch):
        monkeypatch.setattr(pol, "SKLEARN_AVAILABLE", False)
        assert trained.predict_proba(np.zeros((3, 5))) is None

    def test_batch_prediction_refuses_the_wrong_column_count(self, trained):
        with pytest.raises(pol.SchemaMismatch):
            trained.predict_proba(np.zeros((3, 4)))

    def test_batch_prediction_refuses_to_impute(self, trained):
        X = np.zeros((3, 5))
        X[1, 1] = float("nan")
        with pytest.raises(ValueError, match="impute"):
            trained.predict_proba(X)

    def test_a_single_row_is_promoted_to_a_batch(self, trained):
        assert trained.predict_proba(np.zeros(5)).shape == (1,)


# ===========================================================================
# 11. training input validation
# ===========================================================================


class TestTrainingRefusesBadInput:
    @pytest.fixture()
    def fresh(self):
        return pol.Policy(feature_names=NAMES, schema_version=SCHEMA, hyperparameters=FAST_HP)

    def test_a_missing_label_definition_is_refused(self, fresh, learnable):
        X, y, ts = learnable
        with pytest.raises(ValueError, match="label_definition"):
            fresh.train(X[:600], y[:600], ts[:600], 5, label_definition="  ")

    def test_a_wrong_column_count_is_a_schema_mismatch(self, fresh, learnable):
        X, y, ts = learnable
        with pytest.raises(pol.SchemaMismatch):
            fresh.train(X[:600, :4], y[:600], ts[:600], 5, label_definition="x")

    def test_nan_in_the_training_matrix_is_refused_not_imputed(self, fresh, learnable):
        X, y, ts = learnable
        bad = X[:600].copy()
        bad[10, 2] = float("nan")
        with pytest.raises(ValueError, match="refuses to impute"):
            fresh.train(bad, y[:600], ts[:600], 5, label_definition="x")

    def test_non_binary_labels_are_refused(self, fresh, learnable):
        X, y, ts = learnable
        bad = y[:600].copy()
        bad[3] = 2.0
        with pytest.raises(ValueError, match="binary"):
            fresh.train(X[:600], bad, ts[:600], 5, label_definition="x")

    def test_a_label_length_mismatch_is_refused(self, fresh, learnable):
        X, y, ts = learnable
        with pytest.raises(ValueError, match="entries"):
            fresh.train(X[:600], y[:599], ts[:600], 5, label_definition="x")

    def test_unsorted_timestamps_are_refused_not_sorted(self, fresh, learnable):
        X, y, ts = learnable
        shuffled = ts[:600].copy()
        shuffled[100], shuffled[101] = shuffled[101], shuffled[100]
        with pytest.raises(ValueError, match="non-decreasing"):
            fresh.train(X[:600], y[:600], shuffled, 5, label_definition="x")

    def test_equal_timestamps_are_allowed(self, fresh, learnable):
        """Several samples can share a bar; only going backwards is a defect."""
        X, y, ts = learnable
        flat = ts[:600].copy()
        flat[100] = flat[99]
        fresh.train(X[:600], y[:600], flat, 5, label_definition="x", n_folds=2)

    def test_a_none_horizon_is_refused_rather_than_defaulted_to_zero(
        self, fresh, learnable
    ):
        X, y, ts = learnable
        with pytest.raises(ValueError):
            fresh.train(X[:600], y[:600], ts[:600], None, label_definition="x")

    def test_a_one_dimensional_matrix_is_refused(self, fresh, learnable):
        _, y, ts = learnable
        with pytest.raises(ValueError, match="2-D"):
            fresh.train(np.zeros(600), y[:600], ts[:600], 5, label_definition="x")

    def test_single_class_labels_produce_a_named_failure(self, fresh, learnable):
        X, _, ts = learnable
        with pytest.raises(ValueError, match="unusable"):
            fresh.train(
                X[:600], np.zeros(600), ts[:600], 5, label_definition="x", n_folds=2
            )

    def test_isotonic_is_refused_on_a_small_sample(self, learnable):
        X, y, ts = learnable
        p = pol.Policy(
            feature_names=NAMES,
            schema_version=SCHEMA,
            calibration_method="isotonic",
            hyperparameters=FAST_HP,
        )
        with pytest.raises(ValueError, match="ISOTONIC_MIN_SAMPLES"):
            p.train(X[:500], y[:500], ts[:500], 5, label_definition="x")

    def test_datetime64_timestamps_are_accepted(self, fresh, learnable):
        X, y, _ = learnable
        stamps = np.datetime64("2024-01-01T00:00") + np.arange(600).astype(
            "timedelta64[m]"
        )
        rep = fresh.train(X[:600], y[:600], stamps, 5, label_definition="x", n_folds=2)
        assert rep.train_start.startswith("2024-01-01")

    def test_training_without_sklearn_raises_a_named_error(
        self, fresh, learnable, monkeypatch
    ):
        X, y, ts = learnable
        monkeypatch.setattr(pol, "SKLEARN_AVAILABLE", False)
        with pytest.raises(pol.PolicyUnavailable):
            fresh.train(X[:600], y[:600], ts[:600], 5, label_definition="x")


class TestTrainingReportContents:
    def test_it_records_what_it_was_trained_on(self, trained):
        rep = trained.training_report
        assert rep.schema_version == SCHEMA
        assert rep.feature_names == NAMES
        assert rep.n_samples == 4000
        assert rep.n_features == 5
        assert "synthetic" in rep.label_definition

    def test_it_records_the_training_date_range(self, trained):
        rep = trained.training_report
        assert rep.train_start.endswith("Z") and rep.train_end.endswith("Z")
        assert rep.train_start < rep.train_end
        assert rep.trained_at is not None

    def test_it_records_the_cross_validation_settings(self, trained):
        rep = trained.training_report
        assert rep.n_folds == pol.DEFAULT_N_FOLDS
        assert rep.embargo_bars == pol.DEFAULT_EMBARGO_BARS
        assert rep.purge_enabled is True

    def test_it_records_the_hyper_parameters(self, trained):
        assert trained.training_report.hyperparameters == pol.DEFAULT_HYPERPARAMETERS

    def test_it_records_which_inner_calibration_split_was_used(self, trained):
        assert "purged" in trained.training_report.calibration_cv

    def test_every_fold_reports_what_it_removed(self, trained):
        for f in trained.training_report.folds:
            assert f.n_purged > 0, "a 12-bar horizon must purge something"
            assert f.n_embargoed == pol.DEFAULT_EMBARGO_BARS
            assert f.n_train + f.n_purged + f.n_embargoed == f.val_start

    def test_fold_brier_skill_is_computed_against_the_base_rate(self, trained):
        for f in trained.training_report.usable_folds:
            assert f.brier_skill == pytest.approx(1.0 - f.brier / f.baseline_brier)

    def test_a_skipped_fold_is_not_usable(self):
        f = pol.FoldMetrics(
            fold=0, n_train=0, n_val=0, n_purged=0, n_embargoed=0,
            val_start=0, val_stop=0, skipped_reason="EMPTY_TRAIN_AFTER_PURGE",
        )
        assert f.usable is False
        assert f.brier_skill is None

    def test_the_report_round_trips_through_a_dict(self, trained):
        rep = trained.training_report
        back = pol.TrainingReport.from_dict(rep.to_dict())
        assert back.to_dict() == rep.to_dict()

    def test_the_report_is_json_serialisable(self, trained):
        json.dumps(trained.training_report.to_dict())

    def test_the_denormalised_aggregates_match_the_properties(self, trained):
        raw = trained.training_report.to_dict()
        assert raw["oos"]["auc_mean"] == pytest.approx(trained.training_report.auc_mean)
        assert raw["oos"]["brier_skill"] == pytest.approx(
            trained.training_report.brier_skill
        )

    def test_reliability_of_an_untrained_policy_is_none_not_empty(self):
        p = pol.Policy(feature_names=NAMES, schema_version=SCHEMA)
        assert p.reliability() is None

    def test_reliability_can_score_supplied_outcomes(self, trained):
        rep = trained.reliability([0, 1, 1, 0], [0.2, 0.8, 0.7, 0.3])
        assert rep.n_samples == 4

    def test_training_leaves_no_artefact_hash_until_saved(self, learnable):
        """An unsaved model must not be able to quote a content hash.

        Trained fresh rather than reusing the module fixture, because ``save``
        is what sets the hash and this assertion must not depend on which other
        test ran first.
        """
        X, y, ts = learnable
        p = pol.Policy(
            feature_names=NAMES, schema_version=SCHEMA, hyperparameters=FAST_HP
        )
        p.train(X[:1200], y[:1200], ts[:1200], 5, label_definition="x", n_folds=2)
        assert p.is_trained is True
        assert p.artefact_hash is None
        assert p.predict_edge(X[0]).model_hash is None


# ===========================================================================
# 12. artefacts
# ===========================================================================


class TestSaveAndLoad:
    def test_save_writes_both_files(self, saved):
        path, _ = saved
        assert os.path.isfile(os.path.join(path, pol.METADATA_FILENAME))
        assert os.path.isfile(os.path.join(path, pol.ESTIMATOR_FILENAME))

    def test_save_returns_a_content_hash(self, saved):
        _, digest = saved
        assert isinstance(digest, str) and len(digest) == 64

    def test_the_metadata_is_self_describing(self, saved):
        path, digest = saved
        meta = pol.read_metadata(path)
        assert meta["artefact_schema"] == pol.ARTEFACT_SCHEMA
        assert meta["schema_version"] == SCHEMA
        assert meta["feature_names"] == list(NAMES)
        assert meta["content_hash"] == digest
        assert meta["hyperparameters"] == pol.DEFAULT_HYPERPARAMETERS
        assert meta["training"]["n_samples"] == 4000
        assert meta["training"]["label_definition"]
        assert meta["training"]["train_start"] and meta["training"]["train_end"]
        assert meta["training"]["calibration"]["buckets"]
        assert meta["training"]["oos"]["brier_mean"] is not None
        assert meta["promotion"]["ok"] is True
        assert meta["promotion_criteria"]["min_auc_mean"] == 0.55

    def test_the_metadata_records_the_ordered_feature_names(self, saved):
        path, _ = saved
        assert tuple(pol.read_metadata(path)["feature_names"]) == NAMES

    def test_load_round_trips(self, saved):
        path, digest = saved
        back = pol.Policy.load(path, feature_names=NAMES, schema_version=SCHEMA)
        assert back.schema_version == SCHEMA
        assert back.feature_names == NAMES
        assert back.artefact_hash == digest
        assert back.is_trained

    def test_load_produces_identical_predictions(self, saved, trained, learnable):
        path, _ = saved
        X, _, _ = learnable
        back = pol.Policy.load(path, feature_names=NAMES, schema_version=SCHEMA)
        np.testing.assert_array_equal(
            back.predict_proba(X[:300]), trained.predict_proba(X[:300])
        )

    def test_load_preserves_the_training_report(self, saved, trained):
        path, _ = saved
        back = pol.Policy.load(path, feature_names=NAMES, schema_version=SCHEMA)
        assert back.training_report.to_dict() == trained.training_report.to_dict()

    def test_load_preserves_the_promotion_verdict(self, saved):
        path, _ = saved
        back = pol.Policy.load(path, feature_names=NAMES, schema_version=SCHEMA)
        assert back.promotion == (True, ())

    def test_a_reloaded_policy_predicts(self, saved, learnable):
        path, _ = saved
        X, _, _ = learnable
        back = pol.Policy.load(path, feature_names=NAMES, schema_version=SCHEMA)
        d = back.predict_edge(X[0])
        assert d.usable is True
        assert d.model_hash == back.artefact_hash

    def test_saving_twice_gives_the_same_hash(self, trained, tmp_path):
        a = trained.save(str(tmp_path / "a"))
        b = trained.save(str(tmp_path / "b"))
        assert a == b

    def test_the_hash_changes_when_the_model_changes(self, trained, tmp_path, learnable):
        X, y, ts = learnable
        other = pol.Policy(
            feature_names=NAMES, schema_version=SCHEMA, hyperparameters=FAST_HP
        )
        other.train(X[:2000], y[:2000], ts[:2000], 12, label_definition="different")
        assert other.save(str(tmp_path / "other")) != trained.save(str(tmp_path / "base"))

    def test_the_hash_changes_when_only_the_metadata_changes(self, trained, tmp_path):
        base = trained.save(str(tmp_path / "base"))
        moved = copy.copy(trained)
        moved.threshold = 0.55
        assert moved.save(str(tmp_path / "moved")) != base

    def test_saving_an_untrained_policy_is_refused(self, tmp_path):
        p = pol.Policy(feature_names=NAMES, schema_version=SCHEMA)
        with pytest.raises(pol.PolicyError, match="untrained"):
            p.save(str(tmp_path / "empty"))
        assert not os.path.exists(str(tmp_path / "empty"))


class TestLoadRefusesLoudly:
    def test_a_schema_version_mismatch_is_refused(self, saved):
        path, _ = saved
        with pytest.raises(pol.SchemaMismatch, match="schema version mismatch"):
            pol.Policy.load(path, feature_names=NAMES, schema_version="features/v2")

    def test_the_refusal_names_both_versions(self, saved):
        path, _ = saved
        with pytest.raises(pol.SchemaMismatch) as exc:
            pol.Policy.load(path, feature_names=NAMES, schema_version="features/v9")
        assert SCHEMA in str(exc.value) and "features/v9" in str(exc.value)

    def test_reordered_feature_names_are_refused(self, saved):
        """The defect that produces plausible, wrong predictions forever."""
        path, _ = saved
        reordered = (NAMES[1], NAMES[0]) + NAMES[2:]
        with pytest.raises(pol.SchemaMismatch, match="ORDER"):
            pol.Policy.load(path, feature_names=reordered, schema_version=SCHEMA)

    def test_a_reorder_refusal_prints_both_orders(self, saved):
        path, _ = saved
        with pytest.raises(pol.SchemaMismatch) as exc:
            pol.Policy.load(
                path, feature_names=tuple(reversed(NAMES)), schema_version=SCHEMA
            )
        assert "artefact:" in str(exc.value) and "caller:" in str(exc.value)

    def test_a_renamed_feature_is_refused_and_diffed(self, saved):
        path, _ = saved
        with pytest.raises(pol.SchemaMismatch) as exc:
            pol.Policy.load(
                path, feature_names=("f0", "f1", "f2", "f3", "f9"), schema_version=SCHEMA
            )
        assert "f4" in str(exc.value) and "f9" in str(exc.value)

    def test_a_missing_feature_is_refused(self, saved):
        path, _ = saved
        with pytest.raises(pol.SchemaMismatch):
            pol.Policy.load(path, feature_names=NAMES[:4], schema_version=SCHEMA)

    def test_a_missing_artefact_is_refused(self, tmp_path):
        with pytest.raises(pol.ArtefactError, match="no policy artefact"):
            pol.Policy.load(
                str(tmp_path / "nope"), feature_names=NAMES, schema_version=SCHEMA
            )

    def test_unparseable_metadata_is_refused(self, tmp_path):
        os.makedirs(str(tmp_path / "broken"))
        with open(str(tmp_path / "broken" / pol.METADATA_FILENAME), "w") as fh:
            fh.write("{not json")
        with pytest.raises(pol.ArtefactError, match="unreadable"):
            pol.read_metadata(str(tmp_path / "broken"))

    def test_a_foreign_artefact_schema_is_refused(self, saved, tmp_path):
        path, _ = saved
        target = str(tmp_path / "foreign")
        os.makedirs(target)
        meta = pol.read_metadata(path)
        meta["artefact_schema"] = "policy-artefact/0"
        with open(os.path.join(target, pol.METADATA_FILENAME), "w") as fh:
            json.dump(meta, fh)
        with pytest.raises(pol.ArtefactError, match="artefact schema mismatch"):
            pol.Policy.load(target, feature_names=NAMES, schema_version=SCHEMA)

    def test_a_tampered_estimator_is_detected(self, trained, tmp_path):
        path = str(tmp_path / "tampered")
        trained.save(path)
        with open(os.path.join(path, pol.ESTIMATOR_FILENAME), "ab") as fh:
            fh.write(b"\x00")
        with pytest.raises(pol.ArtefactError, match="estimator hash mismatch"):
            pol.Policy.load(path, feature_names=NAMES, schema_version=SCHEMA)

    def test_edited_metadata_is_detected(self, trained, tmp_path):
        path = str(tmp_path / "edited")
        trained.save(path)
        meta = pol.read_metadata(path)
        meta["training"]["oos"]["auc_mean"] = 0.99
        with open(os.path.join(path, pol.METADATA_FILENAME), "w") as fh:
            json.dump(meta, fh)
        with pytest.raises(pol.ArtefactError, match="metadata hash mismatch"):
            pol.Policy.load(path, feature_names=NAMES, schema_version=SCHEMA)

    def test_a_missing_estimator_file_is_refused(self, trained, tmp_path):
        path = str(tmp_path / "half")
        trained.save(path)
        os.remove(os.path.join(path, pol.ESTIMATOR_FILENAME))
        with pytest.raises(pol.ArtefactError, match="cannot read"):
            pol.Policy.load(path, feature_names=NAMES, schema_version=SCHEMA)

    def test_a_corrupt_estimator_is_refused(self, trained, tmp_path):
        path = str(tmp_path / "corrupt")
        trained.save(path)
        meta = pol.read_metadata(path)
        with open(os.path.join(path, pol.ESTIMATOR_FILENAME), "wb") as fh:
            fh.write(b"not a pickle")
        meta["estimator_sha256"] = "x"
        with pytest.raises(pol.ArtefactError):
            pol.Policy.load(
                path, feature_names=NAMES, schema_version=SCHEMA, verify_hash=False
            )

    def test_loading_without_sklearn_raises_unavailable(self, saved, monkeypatch):
        path, _ = saved
        monkeypatch.setattr(pol, "SKLEARN_AVAILABLE", False)
        with pytest.raises(pol.PolicyUnavailable):
            pol.Policy.load(path, feature_names=NAMES, schema_version=SCHEMA)

    def test_read_metadata_needs_neither_sklearn_nor_unpickling(
        self, saved, monkeypatch
    ):
        path, _ = saved
        monkeypatch.setattr(pol, "SKLEARN_AVAILABLE", False)
        assert pol.read_metadata(path)["schema_version"] == SCHEMA


class TestTryLoad:
    """The live path's loader: it must never raise, whatever it finds."""

    def test_a_good_artefact_loads(self, saved):
        path, _ = saved
        policy, reason = pol.try_load(path, feature_names=NAMES, schema_version=SCHEMA)
        assert reason is None
        assert policy is not None and policy.is_trained

    def test_a_missing_artefact_becomes_no_model(self, tmp_path):
        policy, reason = pol.try_load(
            str(tmp_path / "absent"), feature_names=NAMES, schema_version=SCHEMA
        )
        assert policy is None
        assert "ArtefactError" in reason

    def test_a_schema_mismatch_becomes_no_model(self, saved):
        path, _ = saved
        policy, reason = pol.try_load(
            path, feature_names=NAMES, schema_version="features/v7"
        )
        assert policy is None and "SchemaMismatch" in reason

    def test_a_reorder_becomes_no_model(self, saved):
        path, _ = saved
        policy, reason = pol.try_load(
            path, feature_names=tuple(reversed(NAMES)), schema_version=SCHEMA
        )
        assert policy is None and "SchemaMismatch" in reason

    def test_a_missing_dependency_becomes_no_model(self, saved, monkeypatch):
        path, _ = saved
        monkeypatch.setattr(pol, "SKLEARN_AVAILABLE", False)
        policy, reason = pol.try_load(path, feature_names=NAMES, schema_version=SCHEMA)
        assert policy is None and "PolicyUnavailable" in reason

    def test_an_unexpected_error_still_becomes_no_model(self, monkeypatch, saved):
        path, _ = saved

        def boom(*a, **k):
            raise KeyboardInterrupt if False else RuntimeError("unexpected")

        monkeypatch.setattr(pol.Policy, "load", staticmethod(boom))
        policy, reason = pol.try_load(path, feature_names=NAMES, schema_version=SCHEMA)
        assert policy is None and "unexpected" in reason


# ===========================================================================
# 13. construction
# ===========================================================================


class TestConstruction:
    def test_empty_feature_names_are_refused(self):
        with pytest.raises(ValueError, match="must not be empty"):
            pol.Policy(feature_names=[], schema_version=SCHEMA)

    def test_duplicate_feature_names_are_refused(self):
        """A duplicate makes the mapping calling convention ambiguous."""
        with pytest.raises(ValueError, match="duplicates"):
            pol.Policy(feature_names=("a", "b", "a"), schema_version=SCHEMA)

    def test_an_empty_schema_version_is_refused(self):
        with pytest.raises(ValueError, match="schema_version"):
            pol.Policy(feature_names=NAMES, schema_version="   ")

    def test_an_unknown_calibration_method_is_refused(self):
        with pytest.raises(ValueError, match="sigmoid"):
            pol.Policy(
                feature_names=NAMES, schema_version=SCHEMA, calibration_method="magic"
            )

    def test_a_threshold_outside_zero_one_is_refused(self):
        for bad in (0.0, 1.0, -0.2, 1.5, float("nan")):
            with pytest.raises(ValueError, match="threshold"):
                pol.Policy(feature_names=NAMES, schema_version=SCHEMA, threshold=bad)

    def test_feature_names_are_stored_as_an_ordered_tuple(self):
        p = pol.Policy(feature_names=["b", "a"], schema_version=SCHEMA)
        assert p.feature_names == ("b", "a")

    def test_the_defaults_are_the_shipped_ones(self):
        p = pol.Policy(feature_names=NAMES, schema_version=SCHEMA)
        assert p.hyperparameters == pol.DEFAULT_HYPERPARAMETERS
        assert p.calibration_method == pol.DEFAULT_CALIBRATION_METHOD
        assert p.threshold == 0.5
        assert p.require_promotion is True

    def test_the_default_estimator_is_well_regularised(self):
        """Shallow, high leaf minimum, real L2 — a leaf must be a statement."""
        hp = pol.DEFAULT_HYPERPARAMETERS
        assert hp["max_depth"] <= 3
        assert hp["min_samples_leaf"] >= 200
        assert hp["l2_regularization"] >= 1.0
        assert hp["early_stopping"] is False

    def test_metadata_of_an_untrained_policy_says_so(self):
        meta = pol.Policy(feature_names=NAMES, schema_version=SCHEMA).metadata()
        assert meta["training"] is None
        assert meta["promotion"]["ok"] is False


# ===========================================================================
# 14. the module works without sklearn
# ===========================================================================


@contextlib.contextmanager
def sklearn_blocked():
    """Make ``import sklearn`` fail, exactly as it would if it were not installed."""
    saved = {
        k: v
        for k, v in sys.modules.items()
        if k == "sklearn" or k.startswith("sklearn.")
    }
    for k in list(sys.modules):
        if k == "sklearn" or k.startswith("sklearn."):
            sys.modules[k] = None
    try:
        yield
    finally:
        for k in list(sys.modules):
            if k == "sklearn" or k.startswith("sklearn."):
                del sys.modules[k]
        sys.modules.update(saved)


@pytest.fixture(scope="module")
def policy_without_sklearn():
    """A second, independent import of policy.py made with sklearn unavailable."""
    spec = importlib.util.spec_from_file_location("policy_no_sklearn", POLICY_PATH)
    module = importlib.util.module_from_spec(spec)
    # Registered under its own name before execution: the dataclass machinery
    # resolves annotations through sys.modules, and this must not disturb the
    # normally-imported `policy`.
    sys.modules[spec.name] = module
    with sklearn_blocked():
        spec.loader.exec_module(module)
    return module


class TestWithoutSklearn:
    """The live bot must start and run with no model engine at all."""

    def test_the_module_imports(self, policy_without_sklearn):
        assert policy_without_sklearn is not None

    def test_it_knows_it_is_unavailable(self, policy_without_sklearn):
        assert policy_without_sklearn.SKLEARN_AVAILABLE is False
        assert policy_without_sklearn.sklearn_available() is False

    def test_it_records_the_actual_import_error(self, policy_without_sklearn):
        assert "Error" in policy_without_sklearn.SKLEARN_IMPORT_ERROR

    def test_the_unavailability_is_named_and_permanent(self, policy_without_sklearn):
        reason = policy_without_sklearn.unavailability_reason()
        assert "scikit-learn" in reason
        assert "permanently disabled" in reason
        assert reason == policy_without_sklearn.unavailability_reason()

    def test_a_policy_can_still_be_constructed(self, policy_without_sklearn):
        p = policy_without_sklearn.Policy(
            feature_names=NAMES, schema_version=SCHEMA
        )
        assert p.is_trained is False

    def test_prediction_is_unusable_never_a_default(self, policy_without_sklearn):
        p = policy_without_sklearn.Policy(feature_names=NAMES, schema_version=SCHEMA)
        d = p.predict_edge(vector())
        assert d.usable is False
        assert d.probability is None
        assert d.reason == "SKLEARN_UNAVAILABLE"

    def test_prediction_repeats_the_same_answer(self, policy_without_sklearn):
        p = policy_without_sklearn.Policy(feature_names=NAMES, schema_version=SCHEMA)
        first = p.predict_edge(vector())
        second = p.predict_edge(vector())
        assert first.reason == second.reason == "SKLEARN_UNAVAILABLE"

    def test_training_raises_a_named_error(self, policy_without_sklearn, learnable):
        X, y, ts = learnable
        p = policy_without_sklearn.Policy(feature_names=NAMES, schema_version=SCHEMA)
        with pytest.raises(policy_without_sklearn.PolicyUnavailable):
            p.train(X[:600], y[:600], ts[:600], 5, label_definition="x")

    def test_loading_a_real_artefact_degrades_to_no_model(
        self, policy_without_sklearn, saved
    ):
        path, _ = saved
        policy, reason = policy_without_sklearn.try_load(
            path, feature_names=NAMES, schema_version=SCHEMA
        )
        assert policy is None
        assert "PolicyUnavailable" in reason

    def test_the_metrics_still_work(self, policy_without_sklearn):
        """Scoring stored predictions must not need the training dependency."""
        assert policy_without_sklearn.roc_auc([0, 1], [0.1, 0.9]) == 1.0
        assert policy_without_sklearn.brier_score([0, 1], [0.0, 1.0]) == 0.0
        rep = policy_without_sklearn.calibration_report([0, 1], [0.2, 0.8])
        assert rep.n_samples == 2

    def test_the_fold_machinery_still_works(self, policy_without_sklearn):
        folds = policy_without_sklearn.purged_walk_forward_folds(1000, 50, n_folds=3)
        assert len(folds) == 3

    def test_promotion_is_refused(self, policy_without_sklearn):
        p = policy_without_sklearn.Policy(feature_names=NAMES, schema_version=SCHEMA)
        assert p.meets_promotion_criteria() == (False, ("SKLEARN_UNAVAILABLE",))


# ===========================================================================
# 15. structural guards
# ===========================================================================


class TestStructure:
    """What this module is allowed to touch, asserted over the AST."""

    @staticmethod
    def _tree():
        with open(POLICY_PATH, encoding="utf-8") as fh:
            return ast.parse(fh.read())

    @staticmethod
    def _imported_roots(tree):
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                roots.add(node.module.split(".")[0])
        return roots

    def test_it_imports_nothing_from_the_trading_stack(self):
        forbidden = {
            "config",
            "persistence",
            "memory",
            "market_data",
            "risk_management",
            "position_sizing",
            "bybit_connection",
            "technical_analysis",
            "trading_engine",
            "performance_analytics",
            "backtest",
            "main",
            "features",
        }
        assert not (self._imported_roots(self._tree()) & forbidden)

    def test_it_imports_no_network_client(self):
        assert not (
            self._imported_roots(self._tree())
            & {"requests", "urllib", "http", "socket", "websocket", "aiohttp"}
        )

    def test_sklearn_is_imported_only_inside_a_try(self):
        """Optionality is structural, not a convention."""
        tree = self._tree()
        guarded = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    guarded.add(id(sub))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "sklearn"
            ):
                assert id(node) in guarded, "sklearn must be an optional import"

    def test_sklearn_is_imported_exactly_once(self):
        """One import site means one answer to 'is a model engine present'."""
        source = open(POLICY_PATH, encoding="utf-8").read()
        assert source.count("\nfrom sklearn") + source.count("\nimport sklearn") == 0
        assert source.count("    from sklearn") == 2  # both inside the one try block

    def test_it_does_not_read_the_environment(self):
        source = open(POLICY_PATH, encoding="utf-8").read()
        assert "os.getenv" not in source
        assert "os.environ" not in source

    def test_no_definition_is_trapped_in_an_except_handler(self):
        trapped = [
            sub.name
            for node in ast.walk(self._tree())
            if isinstance(node, ast.ExceptHandler)
            for sub in ast.walk(node)
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        assert trapped == []

    def test_no_top_level_name_is_defined_twice(self):
        import collections

        names = collections.Counter(
            n.name
            for n in self._tree().body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        )
        assert {k: v for k, v in names.items() if v > 1} == {}

    def test_everything_exported_exists(self):
        missing = [name for name in pol.__all__ if not hasattr(pol, name)]
        assert missing == []

    def test_the_module_cannot_size_a_position(self):
        """It proposes. Sizing, limits and the kill switch are elsewhere."""
        source = open(POLICY_PATH, encoding="utf-8").read()
        for forbidden in (
            "kill_switch",
            "position_size",
            "qty",
            "MIN_EDGE_BPS",
            "risk_per_trade",
        ):
            assert f"def {forbidden}" not in source
        assert not hasattr(pol.Policy, "size")

    def test_the_decision_carries_no_size_field(self):
        fields = set(pol.PolicyDecision.__dataclass_fields__)
        assert not (fields & {"qty", "size", "notional", "risk_fraction", "leverage"})
