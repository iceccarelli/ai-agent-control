"""policy.py — the trainable policy: a *calibrated* probability, and nothing more.

WHY THIS MODULE EXISTS, AND WHY IT IS DELIBERATELY SMALL
========================================================
``risk_management._gate_expected_edge`` computes, for every candidate trade::

    edge_bps = p * win_bps - (1 - p) * loss_bps - round_trip_cost_bps

and refuses the trade unless ``edge_bps >= MIN_EDGE_BPS``.  That single ``p`` is
the only place a model can enter this system, and the arithmetic around it is
brutally unforgiving: the gate multiplies ``p`` by a reward and subtracts a
cost, so an over-confident score does not produce a slightly worse decision, it
produces a *real loss*, repeatedly, in a direction nobody is monitoring.

A classifier that reports ``0.80`` and is right 55% of the time is not "80%
accurate with some noise".  On a 40 bps reward / 30 bps risk / 25 bps cost
trade it claims ``0.8*40 - 0.2*30 - 25 = +1 bps`` when the truth is
``0.55*40 - 0.45*30 - 25 = -16.5 bps``.  It converts a losing strategy into one
that looks marginally profitable, and it does so on every single trade.  That
is why this module is built around calibration rather than accuracy, why the
Brier score is reported before the AUC everywhere, and why
:meth:`Policy.reliability` exists as a printable table: the table is the
*evidence* that the number the gate consumes means what it says.

WHAT THIS MODULE MAY DO
-----------------------
It may **propose**.  That is the whole authority.  It sits behind every existing
risk gate and has no path to any of them:

* it cannot raise a risk limit — it never returns a size, a fraction or a
  notional, and imports neither ``position_sizing`` nor ``risk_management``;
* it cannot clear the kill switch — it imports neither ``persistence`` nor
  ``trading_engine``, and has no writer role;
* it cannot bypass ``MIN_EDGE_BPS`` — it does not know what ``MIN_EDGE_BPS`` is,
  because it does not import ``config``.  :meth:`PolicyDecision.expected_edge_bps`
  computes the same arithmetic as the gate purely so a caller can *log* what the
  model thought; the gate recomputes it from its own configured cost and its
  answer is the one that counts;
* it cannot make a position larger.  A ``PolicyDecision`` carries a probability
  and a direction.  Turning that into risk is somebody else's job, and that
  somebody already refuses shorts on spot, already clamps by memory, and already
  fails closed.

A ``Policy`` that is unavailable, untrained, unpromoted, or fed a bad feature
vector returns a decision with ``usable=False`` and ``probability=None``.  Never
``0.5``.  ``0.5`` is a number the edge gate will happily multiply; ``None`` is
not, and that difference is the whole fail-closed design.

OPTIONAL DEPENDENCY
-------------------
scikit-learn is an **optional** import.  If it is missing the module still
imports, the live bot still starts, and every entry point returns a clear,
permanent "no model available" state — which means the classical strategy runs
exactly as it does today.  A missing research dependency must never take down a
trading loop, and it must never be papered over with a guessed probability.
The unavailability is permanent for the life of the process by design: there is
no retry, no lazy re-import, no half-initialised state to reason about.  Install
the dependency and restart.

WHAT IS NOT IMPORTED, AND WHY
-----------------------------
``config``, ``persistence``, ``risk_management``, ``trading_engine`` and
``market_data`` are all absent.  This module is stdlib + numpy + optionally
sklearn.  That is what makes it honest to test: there is no environment to read,
no database to write, and no way for a training run to touch live state.  The
input contract is deliberately minimal — a 2-D feature matrix, an ordered list
of feature names, and a schema version string — so the module that *builds*
features and the module that *consumes* probabilities can both change without
either one reaching into this one.

THE FOUR THINGS THAT GO WRONG WITH ML ON MARKET DATA
----------------------------------------------------
1. **Leakage through overlapping labels.**  If the label at bar ``i`` resolves
   over the next 50 bars, then bars ``i`` and ``i+1`` share 49/50 of their
   answer.  Standard k-fold puts one in train and the other in validation and
   reports a score that has no out-of-sample content whatsoever.  Fixed here by
   :func:`purged_walk_forward_folds`, and *demonstrated* in the test suite by
   showing that the un-purged variant scores strictly better — the leak is
   measurable, which is exactly why it is dangerous.
2. **Un-calibrated scores.**  Fixed by ``CalibratedClassifierCV`` and measured
   by Brier score and expected calibration error, both reported out-of-sample.
3. **Silently reordered features.**  A model trained on ``[rsi, atr, spread]``
   and served ``[atr, rsi, spread]`` does not crash.  It produces plausible,
   wrong predictions forever.  Fixed by refusing to load or predict unless the
   exact ordered feature-name list and schema version match, and by accepting a
   ``{name: value}`` mapping so a caller never has to get the order right.
4. **Promoting a model because it exists.**  Fixed by
   :meth:`Policy.meets_promotion_criteria`, whose default is to REFUSE.

ARTEFACT TRUST
--------------
``save``/``load`` use :mod:`pickle` for the estimator, because that is the only
format sklearn guarantees round-trips exactly.  Unpickling executes code, so an
artefact directory is as trusted as the code in this repository.  The recorded
SHA-256 detects corruption and accidental substitution; it is **not** a defence
against an attacker who can write to your model directory.  If that is your
threat model, sign the directory out of band.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import math
import os
import pickle
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np

logger = logging.getLogger("policy")

# ---------------------------------------------------------------------------
# the optional dependency
# ---------------------------------------------------------------------------
#
# Imported once, at import time, into module globals.  Every consumer reads the
# global at call time (never captures it at def time) so that a test can
# simulate absence with a single monkeypatch, and so that there is exactly one
# answer to "is there a model engine" for the life of the process.

try:  # pragma: no cover - exercised by the no-sklearn test via a fresh import
    from sklearn.calibration import CalibratedClassifierCV as _CalibratedClassifierCV
    from sklearn.ensemble import (
        HistGradientBoostingClassifier as _HistGradientBoostingClassifier,
    )

    _SKLEARN_IMPORT_ERROR: Optional[str] = None
except Exception as _exc:  # noqa: BLE001 - any import failure means "no model"
    _CalibratedClassifierCV = None
    _HistGradientBoostingClassifier = None
    _SKLEARN_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"

#: True when a model engine is present.  Read at call time, never cached by
#: callers, so it can be monkeypatched in tests.
SKLEARN_AVAILABLE: bool = _SKLEARN_IMPORT_ERROR is None

#: The exact import failure, for the log line and for
#: :func:`unavailability_reason`.  ``None`` when sklearn imported cleanly.
SKLEARN_IMPORT_ERROR: Optional[str] = _SKLEARN_IMPORT_ERROR

if not SKLEARN_AVAILABLE:  # pragma: no cover - see the no-sklearn test
    logger.warning(
        "POLICY_UNAVAILABLE scikit-learn could not be imported (%s); the "
        "trainable policy is permanently disabled for this process and the "
        "classical strategy will run unchanged. No probability will be "
        "produced by this module until the dependency is installed and the "
        "process restarted.",
        SKLEARN_IMPORT_ERROR,
    )

__all__ = [
    # availability
    "SKLEARN_AVAILABLE",
    "SKLEARN_IMPORT_ERROR",
    "sklearn_available",
    "unavailability_reason",
    # errors
    "PolicyError",
    "PolicyUnavailable",
    "SchemaMismatch",
    "ArtefactError",
    # value types
    "Direction",
    "PolicyDecision",
    "CalibrationBucket",
    "CalibrationReport",
    "FoldMetrics",
    "TrainingReport",
    "PromotionCriteria",
    "WalkForwardFold",
    # the artefact
    "Policy",
    "try_load",
    "read_metadata",
    # metrics (pure numpy, usable without sklearn)
    "roc_auc",
    "brier_score",
    "log_loss_score",
    "accuracy_at_threshold",
    "calibration_report",
    "expected_edge_bps",
    # cross-validation
    "purged_walk_forward_folds",
    # constants
    "ARTEFACT_SCHEMA",
    "METADATA_FILENAME",
    "ESTIMATOR_FILENAME",
    "DEFAULT_HYPERPARAMETERS",
    "DEFAULT_N_FOLDS",
    "DEFAULT_EMBARGO_BARS",
    "INNER_CALIBRATION_FOLDS",
    "MIN_INNER_TRAIN_FRACTION",
    "DEFAULT_CALIBRATION_METHOD",
    "ISOTONIC_MIN_SAMPLES",
    "IMPLAUSIBLE_CERTAINTY",
    "REASON_UNEVALUATED",
]

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

#: Version tag of the *artefact layout* (not of the features).  Bump when the
#: on-disk shape changes, so an old directory is refused instead of
#: half-understood.
ARTEFACT_SCHEMA = "policy-artefact/1"

METADATA_FILENAME = "metadata.json"
ESTIMATOR_FILENAME = "estimator.pkl"

#: Hyper-parameters for the boosted-tree base estimator.
#:
#: Every one of these is set to *lose* information deliberately.  With a few
#: thousand bars and a signal-to-noise ratio that is generously described as
#: poor, an unregularised gradient booster will fit the noise perfectly and
#: report a beautiful in-sample number.
#:
#: * ``max_depth=3`` / ``max_leaf_nodes=8`` — shallow.  Deep interactions
#:   between technical features are almost always fitted noise; three levels is
#:   enough to express "trend and volatility and spread together", which is the
#:   most any of this data supports.
#: * ``min_samples_leaf=200`` — the number that matters most.  A leaf is a
#:   *statistical statement*: at 200 samples a 55% win rate has a standard error
#:   of ~3.5 percentage points, which is at least arguable.  At the sklearn
#:   default of 20 it is ~11 points, i.e. the leaf is a memory of a handful of
#:   bars wearing a probability costume.
#: * ``l2_regularization=1.0`` — strong shrinkage of leaf values towards the
#:   prior, so extreme probabilities have to be earned.
#: * ``learning_rate=0.05`` with ``max_iter=200`` — many small steps beat few
#:   large ones when the target is mostly noise.
#: * ``max_bins=64`` — coarse binning is itself regularisation on continuous
#:   market features whose exact values are not meaningful.
#: * ``early_stopping=False`` — sklearn's internal early stopping carves out a
#:   *random* validation slice, which on overlapping-label time-series data is
#:   precisely the leak this module exists to prevent.  Model selection happens
#:   in the purged walk-forward loop or not at all.
DEFAULT_HYPERPARAMETERS: Dict[str, Any] = {
    "max_depth": 3,
    "max_leaf_nodes": 8,
    "min_samples_leaf": 200,
    "l2_regularization": 1.0,
    "learning_rate": 0.05,
    "max_iter": 200,
    "max_bins": 64,
    "early_stopping": False,
    "random_state": 7,
}

#: Walk-forward folds by default.  Fewer than three and "consistent across
#: folds" is not a statement anyone can make.
DEFAULT_N_FOLDS = 4

#: Inner purged walk-forward splits used to fit the probability calibrator.
#: Five blocks of validation over an expanding window, of which only those whose
#: training side clears :data:`MIN_INNER_TRAIN_FRACTION` are kept.
INNER_CALIBRATION_FOLDS = 5

#: An inner split whose training side is smaller than this fraction of the
#: training block is discarded.
#:
#: This is not a tuning knob, it is a correctness condition. The calibrator maps
#: "what this model says" to "what actually happens". A sub-model trained on a
#: quarter of the data is a *different, weaker* model whose scores are shrunk
#: towards the base rate, so calibrating against it teaches the mapping for a
#: model nobody is going to ship — and the measured effect is large: on a
#: synthetic learnable signal, allowing quarter-data splits raised the pooled
#: out-of-sample calibration error from 0.021 to 0.065, enough on its own to
#: fail promotion.
MIN_INNER_TRAIN_FRACTION = 0.5

#: Bars of embargo between the end of a training block and the start of the
#: validation block, *on top of* label-window purging.  Serial correlation in
#: features does not stop at the label horizon; the embargo covers the residual.
DEFAULT_EMBARGO_BARS = 5

#: Default probability-calibration method.
#:
#: **sigmoid (Platt scaling), not isotonic.**  Isotonic regression is
#: non-parametric and strictly better *asymptotically* — it can correct any
#: monotone distortion.  It also has one parameter per distinct predicted value
#: and therefore overfits hard on small calibration sets, and its failure mode
#: is the worst possible one here: it produces a step function that maps whole
#: ranges of scores to 0.0 or 1.0, i.e. maximal over-confidence at exactly the
#: extremes where ``_gate_expected_edge`` is most sensitive.  Platt scaling has
#: two parameters, degrades gracefully, and cannot manufacture certainty it did
#: not see.  On the sample sizes this system actually has (a few thousand bars
#: per walk-forward fold, of which the inner calibration split sees a fraction),
#: two parameters is the right budget.  Isotonic is available explicitly and is
#: *refused* below :data:`ISOTONIC_MIN_SAMPLES` rather than silently downgraded.
DEFAULT_CALIBRATION_METHOD = "sigmoid"

#: Minimum training samples before ``method="isotonic"`` is permitted at all.
#: The usual rule of thumb is ~1,000 calibration points; below that isotonic is
#: a memorisation of the calibration set.
ISOTONIC_MIN_SAMPLES = 1000

#: A calibrated probability this close to 0 or 1 is not a belief, it is a bug —
#: no honest model of a market bar is 99.9999% sure.  Predictions beyond this
#: are refused rather than served, because they are the single most dangerous
#: input the edge gate can receive.
IMPLAUSIBLE_CERTAINTY = 1e-6

REASON_UNEVALUATED = "UNEVALUATED"


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class PolicyError(Exception):
    """Base class. Anything raised by this module is one of these."""


class PolicyUnavailable(PolicyError):
    """No model engine. Raised only by offline entry points (train/load).

    The *live* path never sees this: :meth:`Policy.predict_edge` returns an
    unusable decision instead, because a trading loop must not have to wrap a
    prediction in a try/except to stay alive.
    """


class SchemaMismatch(PolicyError):
    """The caller's feature contract is not the one the model was trained on.

    Raised loudly and never downgraded to a warning. A model served features in
    a different order does not fail — it succeeds, wrongly, forever.
    """


class ArtefactError(PolicyError):
    """The artefact on disk is missing, malformed, or does not match its hash."""


# ---------------------------------------------------------------------------
# small numeric helpers
# ---------------------------------------------------------------------------


def _finite(value: Any) -> bool:
    """True only for a real, finite scalar. ``None``, NaN and inf are all False."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f)


def _as_float_array(values: Any, name: str) -> np.ndarray:
    try:
        arr = np.asarray(values, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} is not numeric: {exc}") from exc
    return arr


def _canonical_json(obj: Any) -> str:
    """Deterministic JSON. Key order is fixed so the hash is stable."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_json_default)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, Enum):
        return obj.value
    raise TypeError(f"not JSON-serialisable: {type(obj).__name__}")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _iso_from_epoch(epoch_seconds: Optional[float]) -> Optional[str]:
    """Epoch seconds -> ISO-8601 UTC, or ``None``. Never a fabricated date."""
    if not _finite(epoch_seconds):
        return None
    try:
        return (
            _dt.datetime.fromtimestamp(float(epoch_seconds), tz=_dt.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# availability
# ---------------------------------------------------------------------------


def sklearn_available() -> bool:
    """Is a model engine present in this process?

    Reads the module global at call time — deliberately, so that a test can
    simulate absence and so that there is exactly one answer.
    """
    return bool(SKLEARN_AVAILABLE)


def unavailability_reason() -> Optional[str]:
    """Why there is no model engine, or ``None`` when there is one.

    The string is stable for the life of the process: unavailability here is
    permanent, not transient, and pretending otherwise would invite a retry loop
    around a missing dependency.
    """
    if sklearn_available():
        return None
    return (
        "scikit-learn is not importable in this process "
        f"({SKLEARN_IMPORT_ERROR or 'unknown import failure'}); the trainable "
        "policy is permanently disabled. Install the dependency and restart."
    )


# ---------------------------------------------------------------------------
# metrics — pure numpy, so they work with or without sklearn
# ---------------------------------------------------------------------------
#
# These are re-implemented rather than imported from sklearn.metrics for three
# reasons: they must work in the no-sklearn build so that a caller can still
# score stored predictions; they must not drift with the library version, since
# the promotion thresholds are calibrated against *these* definitions; and each
# one returns ``None`` rather than a number when the input cannot support it.


def _clean_pair(
    y_true: Any, y_score: Any
) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    """Validate a (labels, scores) pair. ``None`` if it cannot be scored."""
    yt = _as_float_array(y_true, "y_true").ravel()
    ys = _as_float_array(y_score, "y_score").ravel()
    if yt.size == 0 or yt.shape != ys.shape:
        return None
    if not np.all(np.isfinite(yt)) or not np.all(np.isfinite(ys)):
        return None
    if not np.all((yt == 0.0) | (yt == 1.0)):
        raise ValueError("y_true must be binary 0/1")
    return yt, ys


def roc_auc(y_true: Any, y_score: Any) -> Optional[float]:
    """Area under the ROC curve, with proper mid-rank handling of ties.

    Returns ``None`` — never ``0.5`` — when the labels are single-class, because
    "undefined" and "no better than chance" are different facts and the
    promotion check must be able to tell them apart.
    """
    pair = _clean_pair(y_true, y_score)
    if pair is None:
        return None
    yt, ys = pair
    n_pos = float(np.sum(yt == 1.0))
    n_neg = float(np.sum(yt == 0.0))
    if n_pos == 0.0 or n_neg == 0.0:
        return None
    order = np.argsort(ys, kind="mergesort")
    sorted_scores = ys[order]
    ranks = np.empty(ys.size, dtype=float)
    i = 0
    while i < sorted_scores.size:
        j = i
        while j + 1 < sorted_scores.size and sorted_scores[j + 1] == sorted_scores[i]:
            j += 1
        # 1-based mid-rank shared by the whole tie group
        ranks[order[i : j + 1]] = 0.5 * ((i + 1) + (j + 1))
        i = j + 1
    rank_sum_pos = float(np.sum(ranks[yt == 1.0]))
    auc = (rank_sum_pos - n_pos * (n_pos + 1.0) / 2.0) / (n_pos * n_neg)
    return float(auc)


def brier_score(y_true: Any, y_prob: Any) -> Optional[float]:
    """Mean squared error of the probability. Lower is better; 0 is perfect.

    This is the headline metric of this module. AUC only measures *ranking* —
    a model that scores every winner above every loser but reports 0.99 for all
    of them has a perfect AUC and is a catastrophe once ``_gate_expected_edge``
    multiplies those numbers. Brier penalises exactly that.
    """
    pair = _clean_pair(y_true, y_prob)
    if pair is None:
        return None
    yt, yp = pair
    return float(np.mean((yp - yt) ** 2))


def log_loss_score(y_true: Any, y_prob: Any, *, eps: float = 1e-12) -> Optional[float]:
    """Negative log-likelihood per sample. Clipped so a confident miss is finite.

    Reported alongside Brier because it punishes over-confidence far more
    harshly; a model whose log loss is bad while its Brier looks acceptable is
    making a small number of very confident mistakes, which is the failure mode
    that empties an account fastest.
    """
    pair = _clean_pair(y_true, y_prob)
    if pair is None:
        return None
    yt, yp = pair
    yp = np.clip(yp, eps, 1.0 - eps)
    return float(-np.mean(yt * np.log(yp) + (1.0 - yt) * np.log(1.0 - yp)))


def accuracy_at_threshold(
    y_true: Any, y_prob: Any, threshold: float = 0.5
) -> Optional[float]:
    """Accuracy at the operating threshold the policy will actually use.

    Reported last and treated as decoration: on a 51/49 label balance a model
    that always says "up" scores 51% accuracy and has no information at all.
    """
    pair = _clean_pair(y_true, y_prob)
    if pair is None:
        return None
    if not _finite(threshold):
        return None
    yt, yp = pair
    return float(np.mean((yp >= float(threshold)).astype(float) == yt))


def expected_edge_bps(
    probability: Optional[float],
    win_bps: float,
    loss_bps: float,
    cost_bps: float,
) -> Optional[float]:
    """``p*win - (1-p)*loss - cost``, in bps, or ``None`` if it cannot be computed.

    This mirrors ``risk_management._gate_expected_edge`` **for reporting only**.
    The gate recomputes it from its own configured round-trip cost, and the
    gate's answer is the one that decides. Duplicating the formula here lets an
    operator see what the model believed next to what the gate decided, without
    this module ever learning what ``MIN_EDGE_BPS`` is.
    """
    if probability is None or not _finite(probability):
        return None
    p = float(probability)
    if p < 0.0 or p > 1.0:
        return None
    if not (_finite(win_bps) and _finite(loss_bps) and _finite(cost_bps)):
        return None
    return float(p * float(win_bps) - (1.0 - p) * float(loss_bps) - float(cost_bps))


# ---------------------------------------------------------------------------
# calibration report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationBucket:
    """One row of the reliability table.

    An empty bucket reports ``None`` for both means — not ``0.0``. A bucket
    nothing landed in has no observed frequency, and writing ``0.0`` there would
    make the calibration error look better than it is.
    """

    lower: float
    upper: float
    count: int
    mean_predicted: Optional[float] = None
    observed_frequency: Optional[float] = None

    @property
    def gap(self) -> Optional[float]:
        """Predicted minus realised. Positive means over-confident."""
        if self.mean_predicted is None or self.observed_frequency is None:
            return None
        return float(self.mean_predicted - self.observed_frequency)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lower": self.lower,
            "upper": self.upper,
            "count": int(self.count),
            "mean_predicted": self.mean_predicted,
            "observed_frequency": self.observed_frequency,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CalibrationBucket":
        return cls(
            lower=float(raw["lower"]),
            upper=float(raw["upper"]),
            count=int(raw["count"]),
            mean_predicted=raw.get("mean_predicted"),
            observed_frequency=raw.get("observed_frequency"),
        )


@dataclass(frozen=True)
class CalibrationReport:
    """Predicted probability versus realised frequency, by bucket.

    This is the artefact's evidence. ``print(report)`` prints the table, because
    a calibration report nobody reads is worth nothing, and the barrier to
    reading it should be one function call.
    """

    buckets: Tuple[CalibrationBucket, ...] = ()
    n_samples: int = 0
    base_rate: Optional[float] = None
    brier: Optional[float] = None
    #: Sample-weighted mean absolute gap (ECE). ``None`` when there is no data.
    expected_calibration_error: Optional[float] = None
    #: Worst populated bucket's absolute gap.
    max_calibration_error: Optional[float] = None

    def populated(self, min_count: int = 1) -> Tuple[CalibrationBucket, ...]:
        return tuple(b for b in self.buckets if b.count >= max(1, int(min_count)))

    def is_monotone(self, *, min_count: int = 10, tolerance: float = 0.0) -> bool:
        """Do realised frequencies increase with predicted probability?

        Buckets below ``min_count`` are ignored — three samples in a bucket is
        not a violation of monotonicity, it is an absence of evidence. With
        fewer than two populated buckets this returns ``False``: monotonicity of
        a single point is not a property anyone should be reassured by.
        """
        pts = [
            b.observed_frequency
            for b in self.populated(min_count)
            if b.observed_frequency is not None
        ]
        if len(pts) < 2:
            return False
        return all(pts[i + 1] >= pts[i] - float(tolerance) for i in range(len(pts) - 1))

    def format_table(self) -> str:
        """The reliability table as text, ready to print or log."""
        head = (
            f"reliability  n={self.n_samples}"
            f"  base_rate={_fmt(self.base_rate)}"
            f"  brier={_fmt(self.brier)}"
            f"  ece={_fmt(self.expected_calibration_error)}"
            f"  max_gap={_fmt(self.max_calibration_error)}"
        )
        lines = [
            head,
            "  bucket           n   predicted    realised        gap",
            "  ------------------------------------------------------",
        ]
        for b in self.buckets:
            lines.append(
                f"  [{b.lower:.2f},{b.upper:.2f})"
                f"{b.count:8d}"
                f"{_fmt(b.mean_predicted):>12}"
                f"{_fmt(b.observed_frequency):>12}"
                f"{_fmt(b.gap):>11}"
            )
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.format_table()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "buckets": [b.to_dict() for b in self.buckets],
            "n_samples": int(self.n_samples),
            "base_rate": self.base_rate,
            "brier": self.brier,
            "expected_calibration_error": self.expected_calibration_error,
            "max_calibration_error": self.max_calibration_error,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CalibrationReport":
        return cls(
            buckets=tuple(
                CalibrationBucket.from_dict(b) for b in raw.get("buckets", ())
            ),
            n_samples=int(raw.get("n_samples", 0)),
            base_rate=raw.get("base_rate"),
            brier=raw.get("brier"),
            expected_calibration_error=raw.get("expected_calibration_error"),
            max_calibration_error=raw.get("max_calibration_error"),
        )


def _fmt(value: Optional[float]) -> str:
    return "     -" if value is None else f"{value:.4f}"


def calibration_report(
    y_true: Any, y_prob: Any, *, n_buckets: int = 10
) -> CalibrationReport:
    """Build a reliability table from labels and predicted probabilities.

    Buckets are equal-width over [0, 1] rather than equal-count quantiles. Equal
    width is the right choice for this consumer: the edge gate cares what
    happens *at* p=0.7, not at "the 70th percentile of whatever the model
    happened to output", and a quantile binning hides the case where a model
    only ever predicts 0.49–0.51.
    """
    n_buckets = max(1, int(n_buckets))
    edges = np.linspace(0.0, 1.0, n_buckets + 1)
    pair = _clean_pair(y_true, y_prob)
    if pair is None:
        return CalibrationReport(
            buckets=tuple(
                CalibrationBucket(float(edges[i]), float(edges[i + 1]), 0)
                for i in range(n_buckets)
            ),
            n_samples=0,
        )
    yt, yp = pair
    buckets: List[CalibrationBucket] = []
    weighted_gap = 0.0
    max_gap: Optional[float] = None
    total = float(yt.size)
    for i in range(n_buckets):
        lo, hi = float(edges[i]), float(edges[i + 1])
        if i == n_buckets - 1:
            mask = (yp >= lo) & (yp <= hi)  # last bucket closes on the right
        else:
            mask = (yp >= lo) & (yp < hi)
        count = int(np.sum(mask))
        if count == 0:
            buckets.append(CalibrationBucket(lo, hi, 0))
            continue
        mean_p = float(np.mean(yp[mask]))
        obs = float(np.mean(yt[mask]))
        buckets.append(CalibrationBucket(lo, hi, count, mean_p, obs))
        gap = abs(mean_p - obs)
        weighted_gap += gap * count / total
        max_gap = gap if max_gap is None else max(max_gap, gap)
    return CalibrationReport(
        buckets=tuple(buckets),
        n_samples=int(yt.size),
        base_rate=float(np.mean(yt)),
        brier=brier_score(yt, yp),
        expected_calibration_error=float(weighted_gap),
        max_calibration_error=max_gap,
    )


# ---------------------------------------------------------------------------
# purged, embargoed walk-forward cross-validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WalkForwardFold:
    """One fold: which rows train, which validate, and what was removed.

    ``purged_index`` and ``embargoed_index`` are reported separately and are
    disjoint, so a test (and an operator) can assert *which rows* were dropped
    and *why* — not merely that the score changed. A leak defence that can only
    be verified by looking at a metric is not verifiable at all, because the
    metric moves for a dozen other reasons.
    """

    fold: int
    train_index: np.ndarray
    val_index: np.ndarray
    purged_index: np.ndarray
    embargoed_index: np.ndarray
    val_start: int
    val_stop: int

    @property
    def n_train(self) -> int:
        return int(self.train_index.size)

    @property
    def n_val(self) -> int:
        return int(self.val_index.size)

    @property
    def n_purged(self) -> int:
        return int(self.purged_index.size)

    @property
    def n_embargoed(self) -> int:
        return int(self.embargoed_index.size)


def _resolution_array(bars_to_resolution: Any, n_samples: int) -> np.ndarray:
    """Coerce the per-sample label horizon. Refuses to guess it.

    A scalar broadcasts (a constant horizon is the common case). ``None`` is
    rejected by the caller, not defaulted to zero: a horizon of zero says "the
    label resolves on the same bar", which disables purging entirely and is
    exactly the silent mistake this module exists to prevent.
    """
    arr = _as_float_array(bars_to_resolution, "bars_to_resolution")
    if arr.ndim == 0:
        arr = np.full(n_samples, float(arr))
    arr = arr.ravel()
    if arr.size != n_samples:
        raise ValueError(
            f"bars_to_resolution has {arr.size} entries for {n_samples} samples"
        )
    if not np.all(np.isfinite(arr)):
        raise ValueError("bars_to_resolution contains non-finite values")
    if np.any(arr < 0):
        raise ValueError("bars_to_resolution contains negative values")
    return np.floor(arr).astype(np.int64)


def purged_walk_forward_folds(
    n_samples: int,
    bars_to_resolution: Any,
    *,
    n_folds: int = DEFAULT_N_FOLDS,
    embargo_bars: int = DEFAULT_EMBARGO_BARS,
    purge: bool = True,
) -> Tuple[WalkForwardFold, ...]:
    """Expanding-window walk-forward splits with label purging and an embargo.

    The data is cut into ``n_folds + 1`` contiguous blocks in time order. Fold
    *k* validates on block *k+1* and trains on everything before it. Expanding
    rather than rolling, because this system's data is scarce and discarding the
    oldest block to keep the window fixed throws away the only thing that makes
    the deepest folds trustworthy.

    **Purging.** Sample *i*'s label is not known until bar ``i + h_i``. If that
    window reaches into the validation block, then training on *i* is training
    on information that overlaps the answer being tested. Such samples are
    removed. With an expanding window the training rows all precede the
    validation block, so the rule reduces to::

        drop i  iff  i + h_i >= val_start - embargo

    but it is implemented as a general interval intersection so that it stays
    correct if the fold layout ever changes.

    **Embargo.** Purging handles the label overlap. It does not handle the fact
    that features are themselves serially correlated — a rolling 20-bar mean at
    the last training bar and at the first validation bar share 19 observations.
    ``embargo_bars`` drops a further gap immediately before the validation
    block. It is a blunt instrument, and it is set by the caller because only
    the caller knows the longest look-back in its feature set.

    Skipping this is the reason most published crypto ML results do not survive
    contact with a live account: standard k-fold on overlapping labels reports
    an out-of-sample score that is partially an in-sample score, and the
    difference is large enough to turn noise into a "strategy".
    """
    n_samples = int(n_samples)
    if n_samples <= 0:
        raise ValueError("n_samples must be positive")
    n_folds = int(n_folds)
    if n_folds < 1:
        raise ValueError("n_folds must be >= 1")
    if n_samples < n_folds + 1:
        raise ValueError(
            f"{n_samples} samples cannot be split into {n_folds + 1} blocks"
        )
    embargo = int(embargo_bars)
    if embargo < 0:
        raise ValueError("embargo_bars must be >= 0")

    horizon = _resolution_array(bars_to_resolution, n_samples)
    edges = np.linspace(0, n_samples, n_folds + 2).astype(int)
    all_index = np.arange(n_samples, dtype=np.int64)
    label_end = all_index + horizon

    folds: List[WalkForwardFold] = []
    for k in range(n_folds):
        val_start, val_stop = int(edges[k + 1]), int(edges[k + 2])
        val_index = all_index[val_start:val_stop]
        candidates = all_index[:val_start]
        if val_index.size == 0 or candidates.size == 0:
            continue

        # The validation block's own labels extend past its last row.
        val_label_end = int(np.max(label_end[val_index])) if val_index.size else val_stop

        def _excluded(gap: int) -> np.ndarray:
            forbidden_lo = val_start - gap
            forbidden_hi = val_label_end + gap
            cand_end = label_end[candidates] if purge else candidates
            return (cand_end >= forbidden_lo) & (candidates <= forbidden_hi)

        purged_mask = _excluded(0)
        total_mask = _excluded(embargo)
        embargo_only_mask = total_mask & ~purged_mask

        folds.append(
            WalkForwardFold(
                fold=k,
                train_index=candidates[~total_mask],
                val_index=val_index,
                purged_index=candidates[purged_mask],
                embargoed_index=candidates[embargo_only_mask],
                val_start=val_start,
                val_stop=val_stop,
            )
        )
    return tuple(folds)


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FoldMetrics:
    """Out-of-sample metrics for one walk-forward fold.

    Every metric is ``Optional``. A fold whose validation block is single-class
    has no AUC — reporting ``0.5`` there would let a degenerate split average
    itself into a promotion.
    """

    fold: int
    n_train: int
    n_val: int
    n_purged: int
    n_embargoed: int
    val_start: int
    val_stop: int
    #: Brier first, deliberately: it is the metric this system consumes.
    brier: Optional[float] = None
    baseline_brier: Optional[float] = None
    auc: Optional[float] = None
    log_loss: Optional[float] = None
    accuracy: Optional[float] = None
    base_rate: Optional[float] = None
    skipped_reason: Optional[str] = None

    @property
    def usable(self) -> bool:
        """Did this fold actually produce a measurement?"""
        return self.skipped_reason is None and self.auc is not None

    @property
    def brier_skill(self) -> Optional[float]:
        """``1 - brier/baseline_brier``. Positive means better than the base rate.

        The baseline is "always predict the training base rate", which is the
        honest thing to beat. A model that cannot beat a constant is not adding
        information, however good its AUC looks.
        """
        if self.brier is None or self.baseline_brier in (None, 0.0):
            return None
        return float(1.0 - self.brier / self.baseline_brier)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "fold": int(self.fold),
            "n_train": int(self.n_train),
            "n_val": int(self.n_val),
            "n_purged": int(self.n_purged),
            "n_embargoed": int(self.n_embargoed),
            "val_start": int(self.val_start),
            "val_stop": int(self.val_stop),
            "brier": self.brier,
            "baseline_brier": self.baseline_brier,
            "auc": self.auc,
            "log_loss": self.log_loss,
            "accuracy": self.accuracy,
            "base_rate": self.base_rate,
            "skipped_reason": self.skipped_reason,
        }
        return d

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FoldMetrics":
        return cls(**{k: raw.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


@dataclass(frozen=True)
class TrainingReport:
    """Everything measured at training time. Serialised verbatim into the artefact.

    An artefact that cannot say what it was trained on, over what dates, against
    what label, and how well it did out of sample is not a model — it is a
    pickle of unknown provenance that somebody will eventually point at real
    money.
    """

    schema_version: str = ""
    feature_names: Tuple[str, ...] = ()
    label_definition: str = ""
    n_samples: int = 0
    n_features: int = 0
    base_rate: Optional[float] = None
    folds: Tuple[FoldMetrics, ...] = ()
    calibration: CalibrationReport = field(default_factory=CalibrationReport)
    hyperparameters: Dict[str, Any] = field(default_factory=dict)
    calibration_method: str = DEFAULT_CALIBRATION_METHOD
    calibration_cv: str = ""
    threshold: float = 0.5
    n_folds: int = 0
    embargo_bars: int = 0
    purge_enabled: bool = True
    trained_at: Optional[str] = None
    train_start: Optional[str] = None
    train_end: Optional[str] = None

    # -- aggregates, all computed from the usable folds only ---------------

    @property
    def usable_folds(self) -> Tuple[FoldMetrics, ...]:
        return tuple(f for f in self.folds if f.usable)

    def _agg(self, attr: str) -> List[float]:
        return [
            float(getattr(f, attr))
            for f in self.usable_folds
            if getattr(f, attr) is not None
        ]

    @property
    def auc_mean(self) -> Optional[float]:
        vals = self._agg("auc")
        return float(np.mean(vals)) if vals else None

    @property
    def auc_min(self) -> Optional[float]:
        vals = self._agg("auc")
        return float(np.min(vals)) if vals else None

    @property
    def auc_std(self) -> Optional[float]:
        vals = self._agg("auc")
        return float(np.std(vals)) if len(vals) > 1 else None

    @property
    def brier_mean(self) -> Optional[float]:
        vals = self._agg("brier")
        return float(np.mean(vals)) if vals else None

    @property
    def baseline_brier_mean(self) -> Optional[float]:
        vals = self._agg("baseline_brier")
        return float(np.mean(vals)) if vals else None

    @property
    def brier_skill(self) -> Optional[float]:
        """Pooled Brier skill score against the base-rate baseline."""
        b, base = self.brier_mean, self.baseline_brier_mean
        if b is None or base in (None, 0.0):
            return None
        return float(1.0 - b / base)

    @property
    def log_loss_mean(self) -> Optional[float]:
        vals = self._agg("log_loss")
        return float(np.mean(vals)) if vals else None

    @property
    def accuracy_mean(self) -> Optional[float]:
        vals = self._agg("accuracy")
        return float(np.mean(vals)) if vals else None

    def summary(self) -> str:
        """One paragraph an operator can paste into a review, Brier first."""
        return (
            f"policy schema={self.schema_version} n={self.n_samples} "
            f"features={self.n_features} folds={len(self.usable_folds)}/"
            f"{len(self.folds)} label={self.label_definition!r}\n"
            f"  out-of-sample: brier={_fmt(self.brier_mean)} "
            f"(baseline {_fmt(self.baseline_brier_mean)}, "
            f"skill {_fmt(self.brier_skill)})  "
            f"auc mean={_fmt(self.auc_mean)} min={_fmt(self.auc_min)}  "
            f"logloss={_fmt(self.log_loss_mean)}  "
            f"acc@{self.threshold:.2f}={_fmt(self.accuracy_mean)}\n"
            + self.calibration.format_table()
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "feature_names": list(self.feature_names),
            "label_definition": self.label_definition,
            "n_samples": int(self.n_samples),
            "n_features": int(self.n_features),
            "base_rate": self.base_rate,
            "folds": [f.to_dict() for f in self.folds],
            "calibration": self.calibration.to_dict(),
            "hyperparameters": dict(self.hyperparameters),
            "calibration_method": self.calibration_method,
            "calibration_cv": self.calibration_cv,
            "threshold": float(self.threshold),
            "n_folds": int(self.n_folds),
            "embargo_bars": int(self.embargo_bars),
            "purge_enabled": bool(self.purge_enabled),
            "trained_at": self.trained_at,
            "train_start": self.train_start,
            "train_end": self.train_end,
            # Denormalised aggregates so the JSON is readable without this class.
            "oos": {
                "brier_mean": self.brier_mean,
                "baseline_brier_mean": self.baseline_brier_mean,
                "brier_skill": self.brier_skill,
                "auc_mean": self.auc_mean,
                "auc_min": self.auc_min,
                "auc_std": self.auc_std,
                "log_loss_mean": self.log_loss_mean,
                "accuracy_mean": self.accuracy_mean,
            },
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TrainingReport":
        return cls(
            schema_version=str(raw.get("schema_version", "")),
            feature_names=tuple(raw.get("feature_names", ())),
            label_definition=str(raw.get("label_definition", "")),
            n_samples=int(raw.get("n_samples", 0)),
            n_features=int(raw.get("n_features", 0)),
            base_rate=raw.get("base_rate"),
            folds=tuple(FoldMetrics.from_dict(f) for f in raw.get("folds", ())),
            calibration=CalibrationReport.from_dict(raw.get("calibration", {})),
            hyperparameters=dict(raw.get("hyperparameters", {})),
            calibration_method=str(
                raw.get("calibration_method", DEFAULT_CALIBRATION_METHOD)
            ),
            calibration_cv=str(raw.get("calibration_cv", "")),
            threshold=float(raw.get("threshold", 0.5)),
            n_folds=int(raw.get("n_folds", 0)),
            embargo_bars=int(raw.get("embargo_bars", 0)),
            purge_enabled=bool(raw.get("purge_enabled", True)),
            trained_at=raw.get("trained_at"),
            train_start=raw.get("train_start"),
            train_end=raw.get("train_end"),
        )


# ---------------------------------------------------------------------------
# promotion
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromotionCriteria:
    """What a model must prove before it is allowed to influence a trade.

    Getting a model is easy. Getting one worth risking money on is the hard
    part, and this is where that distinction lives. Every threshold below is a
    deliberate floor, not a target, and the defaults are set so that the answer
    to "may this model trade?" is **no** unless the evidence says otherwise.
    """

    #: Folds that actually produced a measurement. Two folds cannot distinguish
    #: "consistent" from "lucky twice".
    min_usable_folds: int = 3

    #: Total training samples. Below this the confidence interval on every
    #: number in the report is wider than the effect being claimed.
    min_samples: int = 1000

    #: Mean out-of-sample AUC. 0.5 is a coin flip; 0.55 is roughly the smallest
    #: ranking edge that can survive a 25 bps round trip at realistic payoffs.
    min_auc_mean: float = 0.55

    #: **Every** fold must clear this. A model that is excellent in one fold and
    #: useless in three is not a model, it is one lucky regime — and averaging
    #: hides it, which is why this is a per-fold floor rather than a std cap.
    min_auc_per_fold: float = 0.52

    #: Brier skill against "always predict the base rate". Must be positive: a
    #: model that cannot beat a constant is adding noise to the edge gate, no
    #: matter how well it ranks.
    min_brier_skill: float = 0.01

    #: Expected calibration error on pooled out-of-sample predictions. 5 points
    #: of miscalibration on a 0.6 probability is a 10% error in the reward term
    #: of the edge equation, which is the same order as the entire cost budget.
    max_calibration_error: float = 0.05

    #: Label balance. Outside this range the metrics stop meaning anything: a
    #: 2% base rate makes Brier trivially small and accuracy meaningless.
    min_base_rate: float = 0.05
    max_base_rate: float = 0.95

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PromotionCriteria":
        return cls(**{k: raw[k] for k in cls.__dataclass_fields__ if k in raw})


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------


class Direction(str, Enum):
    """What the policy proposes. ``FLAT`` is a first-class, honest answer.

    ``SHORT`` is a *proposal* only. On spot it will be refused downstream by
    ``risk_management._gate_short_capability``, by design and by name — this
    module does not know what category it is running in and must not pretend to.
    """

    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


@dataclass(frozen=True)
class PolicyDecision:
    """One proposal. Fails closed by construction.

    The class enforces its own central invariant in ``__post_init__``:

    * ``usable=False``  =>  ``probability is None``
    * ``usable=True``   =>  ``probability`` is a finite float in [0, 1]

    There is no way to build an unusable decision that carries ``0.5``, and no
    way to build a usable one that carries ``None``. That is structural rather
    than conventional because the failure it prevents — a fallback probability
    reaching ``_gate_expected_edge`` — is invisible in every log and every
    metric until the account is smaller.
    """

    usable: bool = False
    reason: str = REASON_UNEVALUATED
    probability: Optional[float] = None
    direction: Direction = Direction.FLAT
    schema_version: Optional[str] = None
    model_hash: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.usable:
            if self.probability is None or not _finite(self.probability):
                raise ValueError(
                    "a usable PolicyDecision must carry a finite probability"
                )
            if not (0.0 <= float(self.probability) <= 1.0):
                raise ValueError(
                    f"probability {self.probability!r} is outside [0, 1]"
                )
        elif self.probability is not None:
            raise ValueError(
                "an unusable PolicyDecision must carry probability None, not a "
                f"default ({self.probability!r})"
            )

    def __bool__(self) -> bool:
        """Truthiness is *usability*, not tradeability.

        ``if decision:`` guarantees ``decision.probability`` is a real number.
        Whether to act on it is :attr:`proposes_trade`, kept separate so the two
        questions cannot be confused at a call site.
        """
        return bool(self.usable)

    @property
    def proposes_trade(self) -> bool:
        return bool(self.usable) and self.direction in (Direction.LONG, Direction.SHORT)

    @property
    def probability_in_direction(self) -> Optional[float]:
        """Probability that the *proposed* side wins.

        For a SHORT proposal that is ``1 - probability``, because the model's
        label is defined on the long side. Getting this backwards is a silent,
        symmetric error, so it is computed here once instead of at each caller.
        """
        if not self.usable or self.probability is None:
            return None
        if self.direction is Direction.SHORT:
            return float(1.0 - self.probability)
        if self.direction is Direction.LONG:
            return float(self.probability)
        return None

    def expected_edge_bps(
        self, win_bps: float, loss_bps: float, cost_bps: float
    ) -> Optional[float]:
        """The model's own edge estimate, in bps — advisory, never authoritative.

        ``None`` unless this decision proposes a side. The risk layer recomputes
        this from its configured round-trip cost and compares it to
        ``MIN_EDGE_BPS``; nothing here can change that outcome.
        """
        return expected_edge_bps(
            self.probability_in_direction, win_bps, loss_bps, cost_bps
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "usable": bool(self.usable),
            "reason": self.reason,
            "probability": self.probability,
            "direction": self.direction.value,
            "schema_version": self.schema_version,
            "model_hash": self.model_hash,
            "detail": dict(self.detail),
        }

    @classmethod
    def unusable(cls, reason: str, **detail: Any) -> "PolicyDecision":
        """The only way this module ever answers when it cannot answer."""
        return cls(usable=False, reason=str(reason), probability=None, detail=detail)


# ---------------------------------------------------------------------------
# the policy
# ---------------------------------------------------------------------------


class Policy:
    """A trained, calibrated, versioned, self-describing binary policy.

    Input contract, deliberately minimal so that this module is independent of
    whatever builds the features:

    * ``X`` — a 2-D numpy array, one row per sample, columns in the order given
      by ``feature_names``;
    * ``feature_names`` — the exact ordered names of those columns;
    * ``schema_version`` — an opaque string identifying the feature definition.
      Change it whenever the *meaning* of any column changes, even if the name
      does not. It is the only thing standing between a redefined feature and a
      model that keeps predicting confidently from it.

    Label convention: ``y = 1`` means the forward outcome favoured the **long**
    side, and ``predict_proba`` returns P(y = 1). What "favoured" means is your
    business and must be stated in ``label_definition``, which is required at
    training time and recorded in the artefact.
    """

    def __init__(
        self,
        *,
        feature_names: Sequence[str],
        schema_version: str,
        hyperparameters: Optional[Mapping[str, Any]] = None,
        calibration_method: str = DEFAULT_CALIBRATION_METHOD,
        threshold: float = 0.5,
        promotion_criteria: Optional[PromotionCriteria] = None,
        require_promotion: bool = True,
    ) -> None:
        names = tuple(str(n) for n in feature_names)
        if not names:
            raise ValueError("feature_names must not be empty")
        if len(set(names)) != len(names):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"feature_names contains duplicates: {dupes}")
        if not str(schema_version).strip():
            raise ValueError("schema_version must be a non-empty string")
        if calibration_method not in ("sigmoid", "isotonic"):
            raise ValueError(
                f"calibration_method must be 'sigmoid' or 'isotonic', got "
                f"{calibration_method!r}"
            )
        if not _finite(threshold) or not (0.0 < float(threshold) < 1.0):
            raise ValueError("threshold must be a fraction strictly inside (0, 1)")

        self.feature_names: Tuple[str, ...] = names
        self.schema_version: str = str(schema_version)
        self.hyperparameters: Dict[str, Any] = dict(
            DEFAULT_HYPERPARAMETERS if hyperparameters is None else hyperparameters
        )
        self.calibration_method: str = calibration_method
        self.threshold: float = float(threshold)
        self.promotion_criteria: PromotionCriteria = (
            promotion_criteria or PromotionCriteria()
        )
        #: Research escape hatch. ``True`` (the default) means an unpromoted
        #: model refuses to predict at all, so the only way to reach a live
        #: decision is through :meth:`meets_promotion_criteria`.
        self.require_promotion: bool = bool(require_promotion)

        self._model: Any = None
        self._report: Optional[TrainingReport] = None
        self._artefact_hash: Optional[str] = None
        self._promotion: Tuple[bool, Tuple[str, ...]] = (False, ("NOT_TRAINED",))

    # -- introspection ----------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        state = "trained" if self.is_trained else "untrained"
        return (
            f"<Policy {state} schema={self.schema_version!r} "
            f"features={len(self.feature_names)} hash={self.artefact_hash}>"
        )

    @property
    def is_trained(self) -> bool:
        return self._model is not None

    @property
    def training_report(self) -> Optional[TrainingReport]:
        return self._report

    @property
    def artefact_hash(self) -> Optional[str]:
        """Content hash of the last saved/loaded artefact, or ``None``.

        ``None`` for a model that has been trained but never written to disk:
        an in-memory model has no content hash to quote, and inventing one would
        make an unsaved model indistinguishable from a persisted one in the logs.
        """
        return self._artefact_hash

    def metadata(self) -> Dict[str, Any]:
        """The self-describing record. This is exactly what ``save`` writes."""
        return {
            "artefact_schema": ARTEFACT_SCHEMA,
            "schema_version": self.schema_version,
            "feature_names": list(self.feature_names),
            "n_features": len(self.feature_names),
            "calibration_method": self.calibration_method,
            "threshold": self.threshold,
            "hyperparameters": dict(self.hyperparameters),
            "promotion_criteria": self.promotion_criteria.to_dict(),
            "training": self._report.to_dict() if self._report else None,
            "promotion": {
                "ok": self._promotion[0],
                "reasons": list(self._promotion[1]),
            },
        }

    # -- training ---------------------------------------------------------

    def train(
        self,
        X: Any,
        y: Any,
        timestamps: Any,
        bars_to_resolution: Any,
        *,
        label_definition: str,
        n_folds: int = DEFAULT_N_FOLDS,
        embargo_bars: int = DEFAULT_EMBARGO_BARS,
        purge: bool = True,
        n_calibration_buckets: int = 10,
    ) -> TrainingReport:
        """Fit with purged, embargoed walk-forward cross-validation.

        ``bars_to_resolution`` is **required** — a scalar for a constant horizon
        or one entry per sample. There is no default, because the only possible
        default is zero and zero silently disables purging.

        ``label_definition`` is **required** and is free text: "close[i+12] >
        close[i] net of 2x taker fee", say. An artefact that cannot state what
        its ``1`` means is unusable six months later, and six months later is
        exactly when somebody points it at real money.

        Sequence of work, in order:

        1. validate every input, refusing rather than repairing;
        2. build purged/embargoed expanding-window folds;
        3. for each fold, fit a *fresh* calibrated model on the surviving
           training rows and score it on the untouched validation block;
        4. pool the out-of-sample predictions and build the reliability table
           from them — the calibration evidence is strictly out-of-sample,
           because an in-sample calibration curve is always beautiful;
        5. refit one final model on all the data, which is the artefact. The
           folds measure; the artefact learns from everything. Note the
           implication and accept it consciously: the shipped model is not the
           one that was measured, it is one trained the same way on more data.

        Returns the :class:`TrainingReport` and stores it. It does **not**
        promote anything — see :meth:`meets_promotion_criteria`.
        """
        if not sklearn_available():
            raise PolicyUnavailable(unavailability_reason() or "no model engine")
        if not str(label_definition or "").strip():
            raise ValueError(
                "label_definition is required: state what y == 1 means, in words"
            )

        Xa, ya, ts, horizon = self._validate_training_inputs(
            X, y, timestamps, bars_to_resolution
        )
        n_samples, n_features = Xa.shape

        if self.calibration_method == "isotonic" and n_samples < ISOTONIC_MIN_SAMPLES:
            raise ValueError(
                f"isotonic calibration refused: {n_samples} samples is below "
                f"ISOTONIC_MIN_SAMPLES={ISOTONIC_MIN_SAMPLES}; isotonic would "
                "memorise the calibration set and report certainty it never saw. "
                "Use calibration_method='sigmoid'."
            )

        folds = purged_walk_forward_folds(
            n_samples,
            horizon,
            n_folds=n_folds,
            embargo_bars=embargo_bars,
            purge=purge,
        )
        if not folds:
            raise ValueError("no usable walk-forward folds could be constructed")

        fold_metrics: List[FoldMetrics] = []
        pooled_true: List[np.ndarray] = []
        pooled_prob: List[np.ndarray] = []
        calibration_cv_used = ""

        for fold in folds:
            metrics, oos_true, oos_prob, cv_used = self._run_fold(
                Xa, ya, horizon, fold
            )
            fold_metrics.append(metrics)
            if cv_used:
                calibration_cv_used = cv_used
            if oos_true is not None and oos_prob is not None:
                pooled_true.append(oos_true)
                pooled_prob.append(oos_prob)

        if not any(m.usable for m in fold_metrics):
            reasons = sorted({m.skipped_reason or "NO_METRICS" for m in fold_metrics})
            raise ValueError(
                "every walk-forward fold was unusable "
                f"({', '.join(reasons)}); nothing was measured, so nothing is "
                "reported. Check the label balance, the sample count, and the "
                "purge horizon."
            )

        calibration = (
            calibration_report(
                np.concatenate(pooled_true),
                np.concatenate(pooled_prob),
                n_buckets=n_calibration_buckets,
            )
            if pooled_true
            else CalibrationReport()
        )

        # The shipped artefact: same recipe, all of the data.
        final_model, final_cv = self._fit_one(Xa, ya, horizon, embargo_bars)
        self._model = final_model
        self._artefact_hash = None  # not persisted yet; do not quote a stale hash

        self._report = TrainingReport(
            schema_version=self.schema_version,
            feature_names=self.feature_names,
            label_definition=str(label_definition),
            n_samples=int(n_samples),
            n_features=int(n_features),
            base_rate=float(np.mean(ya)),
            folds=tuple(fold_metrics),
            calibration=calibration,
            hyperparameters=dict(self.hyperparameters),
            calibration_method=self.calibration_method,
            calibration_cv=final_cv or calibration_cv_used,
            threshold=self.threshold,
            n_folds=int(n_folds),
            embargo_bars=int(embargo_bars),
            purge_enabled=bool(purge),
            trained_at=_iso_from_epoch(_dt.datetime.now(tz=_dt.timezone.utc).timestamp()),
            train_start=_iso_from_epoch(float(ts[0])) if ts.size else None,
            train_end=_iso_from_epoch(float(ts[-1])) if ts.size else None,
        )
        self._refresh_promotion()
        logger.info("POLICY_TRAINED %s", self._report.summary().splitlines()[1].strip())
        return self._report

    def _validate_training_inputs(
        self, X: Any, y: Any, timestamps: Any, bars_to_resolution: Any
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Refuse anything that is not exactly what was promised.

        No imputation, no coercion of stray labels, no re-sorting of unordered
        timestamps. Silently sorting a caller's rows would rewrite which sample
        precedes which, which is the one thing the purge depends on.
        """
        Xa = _as_float_array(X, "X")
        if Xa.ndim != 2:
            raise ValueError(f"X must be 2-D, got shape {Xa.shape}")
        if Xa.shape[1] != len(self.feature_names):
            raise SchemaMismatch(
                f"X has {Xa.shape[1]} columns but the schema declares "
                f"{len(self.feature_names)} features: {list(self.feature_names)}"
            )
        if not np.all(np.isfinite(Xa)):
            bad = int(np.sum(~np.isfinite(Xa)))
            raise ValueError(
                f"X contains {bad} non-finite value(s). This module refuses to "
                "impute: a NaN feature means the feature pipeline broke, and a "
                "model that quietly fills it produces a confident number from "
                "missing data."
            )

        ya = _as_float_array(y, "y").ravel()
        if ya.size != Xa.shape[0]:
            raise ValueError(f"y has {ya.size} entries for {Xa.shape[0]} rows")
        if not np.all(np.isfinite(ya)) or not np.all((ya == 0.0) | (ya == 1.0)):
            raise ValueError("y must be binary 0/1 with no missing values")

        ts = np.asarray(timestamps)
        if ts.dtype.kind == "M":
            ts = ts.astype("datetime64[s]").astype(np.int64)
        ts = _as_float_array(ts, "timestamps").ravel()
        if ts.size != Xa.shape[0]:
            raise ValueError(f"timestamps has {ts.size} entries for {Xa.shape[0]} rows")
        if not np.all(np.isfinite(ts)):
            raise ValueError("timestamps contains non-finite values")
        if np.any(np.diff(ts) < 0):
            raise ValueError(
                "timestamps must be non-decreasing; this module will not sort "
                "your rows for you, because sorting silently changes which "
                "sample the purge believes precedes which"
            )

        horizon = _resolution_array(bars_to_resolution, Xa.shape[0])
        return Xa, ya, ts, horizon

    def _make_estimator(self) -> Any:
        return _HistGradientBoostingClassifier(**self.hyperparameters)

    def _inner_splits(
        self, horizon: np.ndarray, n: int, embargo_bars: int
    ) -> Optional[List[Tuple[np.ndarray, np.ndarray]]]:
        """Purged inner splits for the calibrator, or ``None`` to fall back.

        ``CalibratedClassifierCV`` defaults to *stratified random* k-fold, which
        on overlapping-label data leaks the answer into the calibration set —
        the same mistake as the outer loop, one level down, where nobody looks.
        So the inner splits are purged walk-forward too, and splits whose
        training side is below :data:`MIN_INNER_TRAIN_FRACTION` are discarded so
        that the calibrator is fitted against a model of roughly the strength of
        the one being shipped. If fewer than two survive, the caller falls back
        to stratified k-fold and the fact is recorded in the artefact rather
        than hidden.
        """
        try:
            inner = purged_walk_forward_folds(
                n,
                horizon,
                n_folds=INNER_CALIBRATION_FOLDS,
                embargo_bars=embargo_bars,
                purge=True,
            )
        except ValueError:
            return None
        floor = MIN_INNER_TRAIN_FRACTION * n
        splits: List[Tuple[np.ndarray, np.ndarray]] = []
        for f in inner:
            if f.train_index.size < 2 or f.val_index.size < 2:
                continue
            if f.train_index.size < floor:
                continue
            splits.append((f.train_index, f.val_index))
        return splits if len(splits) >= 2 else None

    def _fit_one(
        self, X: np.ndarray, y: np.ndarray, horizon: np.ndarray, embargo_bars: int
    ) -> Tuple[Any, str]:
        """Fit one calibrated model. Returns ``(model, which_inner_cv_was_used)``."""
        splits = self._inner_splits(horizon, X.shape[0], embargo_bars)
        usable_splits = None
        if splits is not None:
            usable_splits = [
                (tr, va)
                for tr, va in splits
                if np.unique(y[tr]).size == 2 and np.unique(y[va]).size == 2
            ]
            if len(usable_splits) < 2:
                usable_splits = None

        if usable_splits is not None:
            cv: Any = usable_splits
            cv_name = f"purged_walk_forward({len(usable_splits)})"
        else:
            cv = 3
            cv_name = "stratified_kfold(3)_fallback"
            logger.warning(
                "POLICY_CALIBRATION_CV_FALLBACK purged inner splits were "
                "degenerate; falling back to stratified 3-fold for calibration. "
                "This is recorded in the artefact."
            )

        model = _CalibratedClassifierCV(
            estimator=self._make_estimator(),
            method=self.calibration_method,
            cv=cv,
            ensemble=True,
        )
        model.fit(X, y)
        return model, cv_name

    def _run_fold(
        self,
        X: np.ndarray,
        y: np.ndarray,
        horizon: np.ndarray,
        fold: WalkForwardFold,
    ) -> Tuple[FoldMetrics, Optional[np.ndarray], Optional[np.ndarray], str]:
        """Train on the surviving rows, score on the untouched validation block."""
        base = dict(
            fold=fold.fold,
            n_train=fold.n_train,
            n_val=fold.n_val,
            n_purged=fold.n_purged,
            n_embargoed=fold.n_embargoed,
            val_start=fold.val_start,
            val_stop=fold.val_stop,
        )
        tr, va = fold.train_index, fold.val_index
        if tr.size == 0:
            return FoldMetrics(**base, skipped_reason="EMPTY_TRAIN_AFTER_PURGE"), None, None, ""
        if np.unique(y[tr]).size < 2:
            return FoldMetrics(**base, skipped_reason="SINGLE_CLASS_TRAIN"), None, None, ""
        if va.size == 0:
            return FoldMetrics(**base, skipped_reason="EMPTY_VALIDATION"), None, None, ""

        try:
            model, cv_name = self._fit_one(
                X[tr], y[tr], horizon[tr] if horizon.size else horizon, 0
            )
            prob = np.asarray(model.predict_proba(X[va]), dtype=float)[:, 1]
        except Exception as exc:  # noqa: BLE001 - a failed fold is a fact, not a crash
            logger.warning("POLICY_FOLD_FAILED fold=%s error=%s", fold.fold, exc)
            return (
                FoldMetrics(**base, skipped_reason=f"FIT_FAILED: {type(exc).__name__}"),
                None,
                None,
                "",
            )

        y_val = y[va]
        # The honest baseline: predict the *training* base rate, always. It is
        # what a system with no model would do, and it is what the model must
        # beat to justify its existence.
        baseline_p = float(np.mean(y[tr]))
        metrics = FoldMetrics(
            **base,
            brier=brier_score(y_val, prob),
            baseline_brier=brier_score(y_val, np.full(y_val.shape, baseline_p)),
            auc=roc_auc(y_val, prob),
            log_loss=log_loss_score(y_val, prob),
            accuracy=accuracy_at_threshold(y_val, prob, self.threshold),
            base_rate=float(np.mean(y_val)) if y_val.size else None,
            skipped_reason=None if np.unique(y_val).size == 2 else "SINGLE_CLASS_VAL",
        )
        return metrics, y_val, prob, cv_name

    # -- prediction -------------------------------------------------------

    def predict_proba(self, X: Any) -> Optional[np.ndarray]:
        """Batch P(y = 1). ``None`` when there is no model — never an array of 0.5.

        For research and evaluation. The live path uses :meth:`predict_edge`,
        which returns a decision object with a reason attached. Bad *shapes*
        raise here rather than returning ``None``, because in an offline script
        a wrong-shaped matrix is a bug to fix, not a condition to survive.
        """
        if not sklearn_available() or self._model is None:
            return None
        Xa = _as_float_array(X, "X")
        if Xa.ndim == 1:
            Xa = Xa.reshape(1, -1)
        if Xa.ndim != 2 or Xa.shape[1] != len(self.feature_names):
            raise SchemaMismatch(
                f"expected {len(self.feature_names)} columns, got shape {Xa.shape}"
            )
        if not np.all(np.isfinite(Xa)):
            raise ValueError("X contains non-finite values; this module will not impute")
        return np.asarray(self._model.predict_proba(Xa), dtype=float)[:, 1]

    def predict_edge(
        self,
        features: Union[Mapping[str, Any], Sequence[float], np.ndarray],
        *,
        feature_names: Optional[Sequence[str]] = None,
        schema_version: Optional[str] = None,
    ) -> PolicyDecision:
        """One feature vector in, one :class:`PolicyDecision` out. Never raises.

        ``features`` may be:

        * a **mapping** ``{name: value}`` — the safe form. The vector is built
          in the model's own order, so a caller cannot get the order wrong. Use
          this one.
        * a **sequence / 1-D array** — assumed to already be in the model's
          feature order. Length is checked; order cannot be. Pass
          ``feature_names`` alongside it and the order *is* checked.

        Returns ``usable=False`` with ``probability=None`` — never a guess —
        when: sklearn is missing, no model is loaded, the model has not been
        promoted, the schema or feature names do not match, a feature is missing
        or non-finite, the estimator raises, or the resulting probability is
        outside a sane range.

        This method deliberately does not raise. It is called from the trading
        loop, and a model problem must degrade to "no proposal", never to an
        exception that a caller might catch too broadly.
        """
        ctx = dict(schema_version=self.schema_version, model_hash=self._artefact_hash)
        try:
            if not sklearn_available():
                return self._refuse(
                    "SKLEARN_UNAVAILABLE", ctx, explanation=unavailability_reason()
                )
            if self._model is None:
                return self._refuse("NO_MODEL_LOADED", ctx)
            if self.require_promotion and not self._promotion[0]:
                return self._refuse(
                    "NOT_PROMOTED", ctx, reasons=list(self._promotion[1])
                )

            if schema_version is not None and str(schema_version) != self.schema_version:
                return self._refuse(
                    "SCHEMA_VERSION_MISMATCH",
                    ctx,
                    presented=str(schema_version),
                    expected=self.schema_version,
                )
            if feature_names is not None:
                presented = tuple(str(n) for n in feature_names)
                if presented != self.feature_names:
                    return self._refuse(
                        "FEATURE_NAMES_MISMATCH",
                        ctx,
                        reordered=sorted(presented) == sorted(self.feature_names),
                        presented=list(presented),
                        expected=list(self.feature_names),
                    )

            vector, refusal = self._vector_from(features)
            if refusal is not None:
                return self._refuse(refusal[0], ctx, **refusal[1])

            raw = self._model.predict_proba(vector.reshape(1, -1))
            p = float(np.asarray(raw, dtype=float)[0, 1])
        except Exception as exc:  # noqa: BLE001 - fail closed, loudly, in the log
            logger.warning("POLICY_PREDICT_FAILED %s: %s", type(exc).__name__, exc)
            return PolicyDecision(
                usable=False,
                reason="PREDICT_FAILED",
                probability=None,
                schema_version=self.schema_version,
                model_hash=self._artefact_hash,
                detail={"error": f"{type(exc).__name__}: {exc}"},
            )

        if not _finite(p) or p < 0.0 or p > 1.0:
            return self._refuse("PROBABILITY_OUT_OF_RANGE", ctx, value=p)
        if p <= IMPLAUSIBLE_CERTAINTY or p >= 1.0 - IMPLAUSIBLE_CERTAINTY:
            # Not a belief; a bug. Serving it would hand the edge gate the most
            # dangerous input it can receive.
            return self._refuse("IMPLAUSIBLE_CERTAINTY", ctx, value=p)

        if p > self.threshold:
            direction = Direction.LONG
        elif p < 1.0 - self.threshold:
            direction = Direction.SHORT
        else:
            direction = Direction.FLAT

        return PolicyDecision(
            usable=True,
            reason="OK" if direction is not Direction.FLAT else "NO_DIRECTIONAL_EDGE",
            probability=p,
            direction=direction,
            schema_version=self.schema_version,
            model_hash=self._artefact_hash,
            detail={"threshold": self.threshold},
        )

    def _refuse(
        self, reason: str, ctx: Mapping[str, Any], **detail: Any
    ) -> PolicyDecision:
        return PolicyDecision(
            usable=False,
            reason=reason,
            probability=None,
            direction=Direction.FLAT,
            schema_version=ctx.get("schema_version"),
            model_hash=ctx.get("model_hash"),
            detail={k: v for k, v in detail.items() if v is not None},
        )

    def _vector_from(
        self, features: Any
    ) -> Tuple[np.ndarray, Optional[Tuple[str, Dict[str, Any]]]]:
        """Build the ordered feature vector, or say why it cannot be built."""
        empty = np.zeros(0)
        if isinstance(features, Mapping):
            missing = [n for n in self.feature_names if n not in features]
            if missing:
                return empty, ("FEATURE_MISSING", {"missing": missing})
            values = [features[n] for n in self.feature_names]
            unknown = [k for k in features if k not in self.feature_names]
            if unknown:
                # Not fatal — extra keys are ignored — but say so, because an
                # unknown key is usually a renamed feature nobody wired up.
                logger.debug("POLICY_EXTRA_FEATURES ignored=%s", sorted(unknown))
        else:
            arr = np.asarray(features)
            if arr.ndim == 2 and arr.shape[0] == 1:
                arr = arr.reshape(-1)
            if arr.ndim != 1:
                return empty, ("FEATURE_SHAPE_INVALID", {"shape": list(arr.shape)})
            if arr.size != len(self.feature_names):
                return empty, (
                    "FEATURE_COUNT_MISMATCH",
                    {"got": int(arr.size), "expected": len(self.feature_names)},
                )
            values = list(arr)

        bad = [
            self.feature_names[i] for i, v in enumerate(values) if not _finite(v)
        ]
        if bad:
            # NaN is the single most common live failure: an indicator with too
            # little history, a gap in the book, a division by a zero spread.
            # sklearn's HistGradientBoosting would happily accept it and route
            # it down a learned default branch. We refuse: a probability derived
            # from a missing input is a fabricated number.
            return empty, ("FEATURE_NOT_FINITE", {"features": bad})
        return np.asarray([float(v) for v in values], dtype=float), None

    # -- calibration evidence ---------------------------------------------

    def reliability(
        self,
        y_true: Any = None,
        y_prob: Any = None,
        *,
        n_buckets: int = 10,
    ) -> Optional[CalibrationReport]:
        """The reliability table: predicted probability versus realised frequency.

        With no arguments, returns the pooled **out-of-sample** table measured
        during training (``None`` if this policy has never been trained — not an
        empty table, because an empty table looks like a measurement).

        With ``(y_true, y_prob)``, scores whatever you pass — for checking a
        deployed model against realised outcomes, which is the only way to find
        out that the market changed shape.

        ``print(policy.reliability())`` prints the table.
        """
        if y_true is not None and y_prob is not None:
            return calibration_report(y_true, y_prob, n_buckets=n_buckets)
        if self._report is None:
            return None
        return self._report.calibration

    # -- promotion --------------------------------------------------------

    def meets_promotion_criteria(
        self, criteria: Optional[PromotionCriteria] = None
    ) -> Tuple[bool, Tuple[str, ...]]:
        """May this model influence a trade? Returns ``(ok, reasons)``.

        **The default is to refuse.** An untrained policy, a policy whose folds
        were unusable, a policy with a good average and one bad fold, a policy
        that ranks well but is miscalibrated — all of them fail. ``reasons`` is
        empty only on success and otherwise lists every distinct failure, not
        just the first, so one review round fixes everything rather than
        peeling the objections off one at a time.
        """
        crit = criteria or self.promotion_criteria
        reasons: List[str] = []

        if not sklearn_available():
            return False, ("SKLEARN_UNAVAILABLE",)
        if self._model is None or self._report is None:
            return False, ("NOT_TRAINED",)

        rep = self._report
        usable = rep.usable_folds

        if rep.n_samples < crit.min_samples:
            reasons.append(
                f"INSUFFICIENT_SAMPLES: {rep.n_samples} < {crit.min_samples}"
            )
        if len(usable) < crit.min_usable_folds:
            reasons.append(
                f"INSUFFICIENT_FOLDS: {len(usable)} usable of {len(rep.folds)} "
                f"< {crit.min_usable_folds}"
            )

        base = rep.base_rate
        if base is None:
            reasons.append("NO_BASE_RATE")
        elif not (crit.min_base_rate <= base <= crit.max_base_rate):
            reasons.append(
                f"DEGENERATE_LABEL_BALANCE: base_rate={base:.4f} outside "
                f"[{crit.min_base_rate}, {crit.max_base_rate}]"
            )

        auc_mean = rep.auc_mean
        if auc_mean is None:
            reasons.append("NO_AUC")
        elif auc_mean < crit.min_auc_mean:
            reasons.append(
                f"AUC_MEAN_TOO_LOW: {auc_mean:.4f} < {crit.min_auc_mean}"
            )

        weak = [
            f"fold {f.fold}={f.auc:.4f}"
            for f in usable
            if f.auc is not None and f.auc < crit.min_auc_per_fold
        ]
        if weak:
            reasons.append(
                "AUC_INCONSISTENT_ACROSS_FOLDS: "
                + ", ".join(weak)
                + f" < {crit.min_auc_per_fold}"
            )

        skill = rep.brier_skill
        if skill is None:
            reasons.append("NO_BRIER_SKILL")
        elif skill < crit.min_brier_skill:
            reasons.append(
                f"BRIER_NO_BETTER_THAN_BASE_RATE: skill={skill:.4f} < "
                f"{crit.min_brier_skill} (brier={_fmt(rep.brier_mean)} vs "
                f"baseline {_fmt(rep.baseline_brier_mean)})"
            )

        ece = rep.calibration.expected_calibration_error
        if ece is None:
            reasons.append("NO_CALIBRATION_MEASUREMENT")
        elif ece > crit.max_calibration_error:
            reasons.append(
                f"MISCALIBRATED: ece={ece:.4f} > {crit.max_calibration_error}"
            )

        return (not reasons), tuple(reasons)

    def _refresh_promotion(self) -> None:
        ok, reasons = self.meets_promotion_criteria()
        self._promotion = (ok, tuple(reasons))
        if ok:
            logger.info("POLICY_PROMOTABLE schema=%s", self.schema_version)
        else:
            logger.warning(
                "POLICY_NOT_PROMOTED schema=%s reasons=%s",
                self.schema_version,
                list(reasons),
            )

    @property
    def promotion(self) -> Tuple[bool, Tuple[str, ...]]:
        """The cached promotion verdict, refreshed on train and on load."""
        return self._promotion

    def set_promotion_criteria(self, criteria: PromotionCriteria) -> Tuple[bool, Tuple[str, ...]]:
        """Replace the criteria and re-evaluate. Returns the new verdict."""
        if not isinstance(criteria, PromotionCriteria):
            raise TypeError("criteria must be a PromotionCriteria")
        self.promotion_criteria = criteria
        self._refresh_promotion()
        return self._promotion

    # -- persistence ------------------------------------------------------

    def save(self, path: str) -> str:
        """Write a versioned, self-describing artefact directory. Returns its hash.

        Layout::

            <path>/estimator.pkl   the fitted, calibrated estimator
            <path>/metadata.json   everything needed to decide whether to trust it

        The metadata records the artefact schema, the feature schema version,
        the exact ordered feature names, the training date range, the sample
        count, the label definition, the hyper-parameters, the full calibration
        report, the per-fold out-of-sample metrics, the promotion criteria and
        the promotion verdict. ``content_hash`` covers both files, so tampering
        with either one is detected at load.

        Refuses to save an untrained policy: an artefact directory that looks
        real and contains no model is a trap for whoever finds it next.
        """
        if self._model is None or self._report is None:
            raise PolicyError("refusing to save an untrained policy")
        os.makedirs(path, exist_ok=True)
        blob = pickle.dumps(self._model, protocol=pickle.HIGHEST_PROTOCOL)
        estimator_hash = _sha256_bytes(blob)

        meta = self.metadata()
        meta["estimator_sha256"] = estimator_hash
        meta["content_hash"] = _sha256_bytes(_canonical_json(meta).encode("utf-8"))

        with open(os.path.join(path, ESTIMATOR_FILENAME), "wb") as fh:
            fh.write(blob)
        with open(os.path.join(path, METADATA_FILENAME), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2, sort_keys=True, default=_json_default)

        self._artefact_hash = meta["content_hash"]
        logger.info(
            "POLICY_SAVED path=%s hash=%s promoted=%s",
            path,
            self._artefact_hash,
            self._promotion[0],
        )
        return self._artefact_hash

    @classmethod
    def load(
        cls,
        path: str,
        *,
        feature_names: Sequence[str],
        schema_version: str,
        verify_hash: bool = True,
        require_promotion: bool = True,
    ) -> "Policy":
        """Load an artefact, refusing loudly unless the caller's schema matches.

        ``feature_names`` and ``schema_version`` are **required**. You cannot
        load a model without declaring the feature contract you intend to feed
        it, because the failure mode of not declaring it is not a crash — it is
        a model that keeps returning confident, plausible, wrong numbers from
        columns that mean something else now. The comparison is on the exact
        ordered list: a permutation is a mismatch and says so explicitly.

        Raises :class:`SchemaMismatch`, :class:`ArtefactError` or
        :class:`PolicyUnavailable`. Callers on the live path should use
        :func:`try_load`, which converts all of these into "no model".
        """
        meta = read_metadata(path)

        got_schema = str(meta.get("artefact_schema", ""))
        if got_schema != ARTEFACT_SCHEMA:
            raise ArtefactError(
                f"artefact schema mismatch at {path}: found {got_schema!r}, "
                f"this code writes {ARTEFACT_SCHEMA!r}"
            )

        expected_names = tuple(str(n) for n in meta.get("feature_names", ()))
        presented_names = tuple(str(n) for n in feature_names)
        meta_schema = str(meta.get("schema_version", ""))

        if meta_schema != str(schema_version):
            raise SchemaMismatch(
                f"feature schema version mismatch at {path}: the artefact was "
                f"trained on {meta_schema!r}, the caller presents "
                f"{str(schema_version)!r}. Refusing to serve predictions from a "
                "model whose features may have been redefined."
            )
        if presented_names != expected_names:
            if sorted(presented_names) == sorted(expected_names):
                raise SchemaMismatch(
                    f"feature ORDER mismatch at {path}: the same {len(expected_names)} "
                    "names in a different order. This is the defect that produces "
                    "plausible, wrong predictions forever, so it is refused rather "
                    f"than reordered.\n  artefact: {list(expected_names)}\n"
                    f"  caller:   {list(presented_names)}"
                )
            raise SchemaMismatch(
                f"feature name mismatch at {path}.\n"
                f"  missing from caller: {sorted(set(expected_names) - set(presented_names))}\n"
                f"  unknown to artefact: {sorted(set(presented_names) - set(expected_names))}"
            )

        estimator_path = os.path.join(path, ESTIMATOR_FILENAME)
        try:
            with open(estimator_path, "rb") as fh:
                blob = fh.read()
        except OSError as exc:
            raise ArtefactError(f"cannot read {estimator_path}: {exc}") from exc

        if verify_hash:
            recorded = str(meta.get("estimator_sha256", ""))
            actual = _sha256_bytes(blob)
            if recorded != actual:
                raise ArtefactError(
                    f"estimator hash mismatch at {path}: recorded {recorded}, "
                    f"found {actual}. The artefact has been modified or corrupted."
                )
            declared = str(meta.get("content_hash", ""))
            recomputed = _sha256_bytes(
                _canonical_json(
                    {k: v for k, v in meta.items() if k != "content_hash"}
                ).encode("utf-8")
            )
            if declared != recomputed:
                raise ArtefactError(
                    f"metadata hash mismatch at {path}: declared {declared}, "
                    f"recomputed {recomputed}. The metadata has been edited."
                )

        if not sklearn_available():
            raise PolicyUnavailable(unavailability_reason() or "no model engine")

        try:
            model = pickle.loads(blob)
        except Exception as exc:  # noqa: BLE001
            raise ArtefactError(f"cannot unpickle {estimator_path}: {exc}") from exc

        training = meta.get("training") or {}
        policy = cls(
            feature_names=expected_names,
            schema_version=meta_schema,
            hyperparameters=meta.get("hyperparameters") or None,
            calibration_method=str(
                meta.get("calibration_method", DEFAULT_CALIBRATION_METHOD)
            ),
            threshold=float(meta.get("threshold", 0.5)),
            promotion_criteria=PromotionCriteria.from_dict(
                meta.get("promotion_criteria", {})
            ),
            require_promotion=require_promotion,
        )
        policy._model = model
        policy._report = TrainingReport.from_dict(training) if training else None
        policy._artefact_hash = str(meta.get("content_hash", "")) or None
        policy._refresh_promotion()
        logger.info(
            "POLICY_LOADED path=%s schema=%s hash=%s promoted=%s",
            path,
            policy.schema_version,
            policy._artefact_hash,
            policy._promotion[0],
        )
        return policy


def read_metadata(path: str) -> Dict[str, Any]:
    """Read an artefact's metadata without loading the model.

    For tooling and review: you should be able to see what a model claims about
    itself, and whether it was promotable, without unpickling anything and
    without having sklearn installed.
    """
    meta_path = os.path.join(path, METADATA_FILENAME)
    try:
        with open(meta_path, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
    except FileNotFoundError as exc:
        raise ArtefactError(f"no policy artefact at {meta_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtefactError(f"unreadable policy metadata at {meta_path}: {exc}") from exc
    if not isinstance(meta, dict):
        raise ArtefactError(f"policy metadata at {meta_path} is not an object")
    return meta


def try_load(
    path: str,
    *,
    feature_names: Sequence[str],
    schema_version: str,
    require_promotion: bool = True,
) -> Tuple[Optional["Policy"], Optional[str]]:
    """The live path's loader: ``(policy, None)`` or ``(None, reason)``. Never raises.

    A missing artefact, a stale schema, a corrupt file or a missing dependency
    must all degrade to "the classical strategy runs unchanged". The reason is
    returned *and* logged so the operator sees a named cause instead of silence,
    but the trading loop never has to handle an exception to stay alive.
    """
    try:
        policy = Policy.load(
            path,
            feature_names=feature_names,
            schema_version=schema_version,
            require_promotion=require_promotion,
        )
    except PolicyError as exc:
        logger.warning("POLICY_LOAD_REFUSED path=%s %s: %s", path, type(exc).__name__, exc)
        return None, f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - nothing here may reach the loop
        logger.warning("POLICY_LOAD_FAILED path=%s %s: %s", path, type(exc).__name__, exc)
        return None, f"{type(exc).__name__}: {exc}"
    return policy, None
