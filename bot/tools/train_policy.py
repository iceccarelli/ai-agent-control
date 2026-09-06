#!/usr/bin/env python3
"""tools/train_policy.py — fit the policy on real bars, and refuse to ship it by default.

WHAT THIS TOOL IS
=================
The offline loop that joins the two halves already in the repository:
``features.build_dataset`` turns bars into a supervised matrix, ``policy.Policy``
fits a *calibrated* probability with purged walk-forward cross-validation. This
file adds only the things neither module may own: reading the corpus, choosing
the split, printing the evidence, and deciding whether the result is allowed
near the live model path.

Nothing here re-implements a feature, a label, a fold, a metric or a promotion
rule. If a number appears in this report it was computed by ``features``,
``policy`` or ``market_data``, with two named exceptions that are stated where
they occur: the permutation feature importance (:func:`permutation_importance`)
and the holdout evaluation (:func:`evaluate_holdout`), which is
``PromotionCriteria`` applied verbatim to a block of data the model never saw.

THE HELD-OUT PERIOD, AND WHAT BURNS IT
--------------------------------------
Walk-forward cross-validation is the *measurement*; it is also, unavoidably, the
thing every choice in this file was made against. Fold count, embargo length,
barrier widths, which columns are usable — each of those is a decision, and a
decision made while watching a score is a decision fitted to that score. So the
most recent slice of the corpus is removed before any of it happens, is never in
a training fold, is never in a calibration split, and is scored exactly once, at
the end, and reported last.

That protection is one-shot. The moment somebody reads the holdout number and
changes a hyper-parameter, the holdout is training data wearing a holdout's
name, and every subsequent run of this tool reports a number that is no longer
out of sample. The report says so in full, every time, because the person who
needs the warning is the person who has already decided to ignore it.

There is deliberately **no flag that lowers a promotion threshold.** Loosening
the bar is a code change to ``policy.PromotionCriteria``, where it shows up in a
diff and gets reviewed. A ``--min-auc`` flag is how a noise model reaches
production at 2am with nobody in the loop.

WHAT THIS TOOL DOES NOT PRODUCE
-------------------------------
A profit estimate. Not one, anywhere. It measures whether a probability means
what it says — that is the input ``risk_management._gate_expected_edge``
multiplies by a reward and compares against a cost. Turning that into money
requires the backtester, real fees and a position sizer, and quoting a return
from a classifier's AUC is exactly the arithmetic this repository exists to
avoid.

USAGE
-----
::

    python3 tools/train_policy.py --data-dir data/real
    python3 tools/train_policy.py --data-dir data/real --dry-run
    python3 tools/train_policy.py --data-dir data/real --holdout-from 2024-01-01

Artefact layout under ``--out`` (default ``models/``)::

    models/<run_id>/            promoted: the versioned artefact
    models/current/             promoted: the live model path
    models/rejected/<run_id>/   refused: written for inspection, never served
    models/**/MANIFEST.json     the provenance record, in git

``models/`` is otherwise ignored by git; see the note printed at the end of a
run for the exact ``.gitignore`` lines.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

import features as ft  # noqa: E402
import market_data as md  # noqa: E402
import policy as pol  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Bumped when the *manifest layout* changes, so a reader can tell whether the
#: keys it wants exist rather than discovering their absence as a ``KeyError``.
TOOL_VERSION = 1

MANIFEST_FILENAME = "MANIFEST.json"

#: The live model path, relative to ``--out``. Only a promoted run writes here.
LIVE_DIRNAME = "current"

#: Where a refused run is written instead. It exists so a failure can be
#: inspected — a model nobody can look at is a model nobody can improve — and it
#: is a *different directory* so that no amount of path confusion can serve it.
REJECTED_DIRNAME = "rejected"

DEFAULT_MODELS_ROOT = os.path.join(REPO, "models")

#: Fraction of the sample timeline held out when ``--holdout-from`` is not given.
#:
#: A fraction rather than a hard-coded date so the default survives a corpus
#: that ends somewhere else; the resolved cut date is printed, so the run is
#: still reproducible by quoting it back. One fifth of seven years is about
#: seventeen months, which is long enough to contain a regime the training data
#: does not.
DEFAULT_HOLDOUT_FRACTION = 0.2

#: Below this many holdout rows the holdout is not evidence and promotion is
#: refused. Not a tunable: the holdout's whole job is to be a second opinion,
#: and a second opinion from fifty samples is a coin flip with a ceremony.
MIN_HOLDOUT_SAMPLES = 200

DEFAULT_SEED = 7

#: Permutation repeats for the importance table. Five is enough to see whether a
#: feature's contribution is larger than the shuffling noise, and the spread is
#: printed next to the mean so the reader can judge that for themselves.
IMPORTANCE_REPEATS = 5

#: The promotion bar, in one place, in code, where changing it is a reviewable
#: diff. There is no CLI path to any of these numbers by design.
PROMOTION_CRITERIA = pol.PromotionCriteria()

#: The label. ``BarrierSpec``'s own defaults, named here so the manifest records
#: the question the model was asked rather than leaving it to be reconstructed.
BARRIERS = ft.BarrierSpec()

_MS_PER_HOUR = 3_600_000


class TrainingAborted(RuntimeError):
    """Raised before any fitting happens, when an input cannot be trusted.

    Distinct from every sklearn or numpy error so that ``main`` can report it as
    a refusal — a clean "this did not run, here is why" — rather than as a crash
    that leaves the reader wondering whether a model was written.
    """


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _json_ready(obj: Any) -> Any:
    """Make numpy scalars and tuples JSON-serialisable, refusing to guess.

    ``json.dump`` turns an unknown type into a ``TypeError`` at the worst
    moment — after training, when the manifest is being written. Converting the
    handful of types this tool actually produces, and letting anything else
    raise, keeps the failure loud instead of writing ``"<numpy.float64 ...>"``
    into a provenance record.
    """
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        value = float(obj)
        return value if np.isfinite(value) else None
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return [_json_ready(v) for v in obj.tolist()]
    if isinstance(obj, tuple):
        return [_json_ready(v) for v in obj]
    raise TypeError(f"{type(obj).__name__} is not JSON-serialisable")


def _canonical(obj: Any) -> str:
    """Stable JSON text for hashing. Sorted keys, no incidental whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_json_ready)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def _fmt(value: Optional[float], places: int = 4) -> str:
    """``None`` prints as a dash, never as 0. A missing measurement is not zero."""
    if value is None:
        return "     -"
    return f"{value:.{places}f}"


def parse_cut_date(text: str) -> int:
    """``YYYY-MM-DD`` or full ISO-8601 UTC -> epoch milliseconds.

    Refuses a naive local-time interpretation: the corpus is timestamped in UTC
    and a cut date that silently shifted by the operator's timezone would move
    the holdout boundary by hours without telling anybody.
    """
    raw = str(text).strip()
    if not raw:
        raise TrainingAborted("--holdout-from was empty")
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = _dt.datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise TrainingAborted(
            f"--holdout-from {raw!r} is not an ISO-8601 date "
            f"(YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS): {exc}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return int(parsed.timestamp() * 1000)


def format_ms(ms: Optional[float]) -> Optional[str]:
    """Epoch milliseconds -> ISO-8601 UTC, or ``None`` for ``None``."""
    if ms is None:
        return None
    return (
        _dt.datetime.fromtimestamp(float(ms) / 1000.0, tz=_dt.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def git_commit() -> Optional[str]:
    """The commit this ran from, or ``None``.

    ``None`` rather than ``"unknown"``: a manifest that claims a commit it does
    not have is worse than one that admits it does not know. Deliberately left
    out of the artefact fingerprint — the model does not change because a
    README did.
    """
    try:
        out = subprocess.run(
            ["git", "-C", REPO, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = out.stdout.strip()
    return commit or None


# ---------------------------------------------------------------------------
# the corpus
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CorpusInfo:
    """Everything about the input data that belongs in the provenance record."""

    root: str
    symbol: str
    n_bars: int
    first_ms: Optional[int]
    last_ms: Optional[int]
    manifest_sha256: Optional[str]
    file_sha256: Mapping[str, str]
    synthetic: bool
    source: Mapping[str, Any]
    notes: Tuple[str, ...]
    files_checked: int
    recompressed: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "root": self.root,
            "symbol": self.symbol,
            "bars": int(self.n_bars),
            "first_bar": format_ms(self.first_ms),
            "last_bar": format_ms(self.last_ms),
            "manifest_sha256": self.manifest_sha256,
            "file_sha256": dict(self.file_sha256),
            "synthetic": bool(self.synthetic),
            "source": dict(self.source),
            "notes": list(self.notes),
            "files_checked": int(self.files_checked),
            "recompressed": list(self.recompressed),
        }


def verify_corpus(root: str) -> md.ManifestReport:
    """Check the corpus against its manifest, and abort if it does not match.

    Called **before** anything is loaded, not as a side effect of loading. The
    failure it guards against is training on a corpus somebody edited by hand:
    the run would succeed, the report would look reproducible, and every number
    in it would describe data that no longer exists anywhere else. Three minutes
    of feature building on unverified bytes is three minutes spent manufacturing
    a provenance record that is a lie.

    ``deep=True`` because a re-counted row is cheap here and catches the one
    case a hash cannot: a file whose hash was regenerated after the edit.
    """
    report = md.verify_manifest(root, deep=True)
    if report.problems:
        raise TrainingAborted(
            f"corpus at {root} does not match its manifest — refusing to train "
            "on unverified data:\n  " + "\n  ".join(report.problems)
        )
    return report


def load_corpus_bars(root: str, symbol: Optional[str] = None) -> Tuple[List[Any], CorpusInfo]:
    """Verify, load, and pick exactly one symbol. Never silently merges symbols.

    A multi-symbol corpus trained as one pooled matrix is a different (and
    defensible) experiment, but it is not this one, and concatenating series
    would break the single monotone timeline that every purge in ``policy``
    depends on. So a corpus with more than one symbol and no ``--symbol`` is an
    error with the choices listed, not a guess.
    """
    report = verify_corpus(root)
    bars_by_symbol, _books, notes = md.load_corpus(root, verify=False)

    available = sorted(bars_by_symbol)
    if symbol:
        chosen = symbol.upper()
        if chosen not in bars_by_symbol:
            raise TrainingAborted(
                f"symbol {chosen!r} is not in {root}; available: {available}"
            )
    elif len(available) == 1:
        chosen = available[0]
    else:
        raise TrainingAborted(
            f"{root} contains {len(available)} symbols {available}; pass "
            "--symbol to choose one. Pooling them would concatenate two "
            "timelines and break every purge downstream."
        )

    bars = bars_by_symbol[chosen]

    manifest_path = os.path.join(root, "MANIFEST.json")
    raw_manifest: Dict[str, Any] = {}
    try:
        with open(manifest_path, encoding="utf-8") as handle:
            raw_manifest = json.load(handle)
    except (OSError, json.JSONDecodeError):
        raw_manifest = {}

    files = raw_manifest.get("files") or {}
    file_hashes = {
        name: str(entry.get("sha256", ""))
        for name, entry in files.items()
        if isinstance(entry, dict)
    }

    info = CorpusInfo(
        root=os.path.abspath(root),
        symbol=chosen,
        n_bars=len(bars),
        first_ms=bars[0].start_ms if bars else None,
        last_ms=bars[-1].start_ms if bars else None,
        manifest_sha256=_sha256_file(manifest_path),
        file_sha256=file_hashes,
        synthetic=bool(raw_manifest.get("synthetic", True)),
        source=dict(raw_manifest.get("source") or {}),
        notes=tuple(notes),
        files_checked=report.checked,
        recompressed=tuple(report.recompressed),
    )
    return list(bars), info


# ---------------------------------------------------------------------------
# the label, in words
# ---------------------------------------------------------------------------


def label_definition(barriers: ft.BarrierSpec) -> str:
    """What ``y == 1`` means, in a sentence, for the artefact to carry forever.

    ``policy.Policy.train`` requires this and records it verbatim. Six months
    from now the question "was 1 the take-profit, or was it any positive
    return?" has exactly one honest answer and it has to be written down at the
    moment the labels are made.
    """
    side = "long" if barriers.direction == 1 else "short"
    return (
        f"triple-barrier {side}: y=1 iff the +{barriers.take_profit_atr:g}x"
        f"ATR({barriers.atr_period}) take-profit was touched before the "
        f"-{barriers.stop_atr:g}xATR stop within {barriers.max_horizon} bars "
        f"from this bar's close; the stop and the timeout are BOTH y=0, so "
        f"P(y=1) is the probability of a completed winning trade, not of a "
        f"positive return"
    )


def binarise(y: np.ndarray) -> np.ndarray:
    """``features``' three-way outcome -> the binary target ``policy`` requires.

    ``LABEL_TAKE_PROFIT`` is 1; ``LABEL_STOP`` and ``LABEL_TIMEOUT`` are both 0.
    Collapsing the timeout into the loss class is a *choice*, and it is the
    conservative one: a trade that never reached its target is not a win, and
    calling it one would inflate ``p`` in exactly the term
    ``_gate_expected_edge`` multiplies by the reward. Dropping timeouts instead
    would train the model on a sub-population it will not face live.
    """
    arr = np.asarray(y)
    return (arr == ft.LABEL_TAKE_PROFIT).astype(np.float64)


# ---------------------------------------------------------------------------
# the split
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Split:
    """Which rows train, which are held out, and which were sacrificed between.

    ``train_pos`` and ``holdout_pos`` are positions into the dataset's arrays,
    kept as explicit index arrays rather than as a boolean cut so that a test
    can assert the two sets are disjoint *and* that no training row's label
    window reaches into the holdout. A split that can only be checked by
    comparing dates is a split whose off-by-one nobody will ever find.
    """

    holdout_from_ms: int
    holdout_from_source: str
    train_pos: np.ndarray
    holdout_pos: np.ndarray
    boundary_purged_pos: np.ndarray
    first_holdout_bar: Optional[int]
    embargo_bars: int

    @property
    def n_train(self) -> int:
        return int(self.train_pos.size)

    @property
    def n_holdout(self) -> int:
        return int(self.holdout_pos.size)

    @property
    def n_boundary_purged(self) -> int:
        return int(self.boundary_purged_pos.size)


def resolve_holdout_from(
    dataset: ft.Dataset, requested: Optional[str]
) -> Tuple[int, str]:
    """Return ``(epoch_ms, how_it_was_chosen)`` for the holdout boundary.

    With no ``--holdout-from`` the cut falls at
    :data:`DEFAULT_HOLDOUT_FRACTION` of the *time span* covered by the samples,
    not of the row count. Rows are not uniformly distributed in time — a period
    the feature pipeline could not measure contributes no rows at all — and
    "the last 20% of the rows" would silently be a different date on every
    corpus while claiming to be a fixed rule.
    """
    if requested:
        return parse_cut_date(requested), f"--holdout-from {requested}"
    if len(dataset) == 0:
        raise TrainingAborted("the dataset is empty; there is nothing to split")
    first = int(dataset.timestamps[0])
    last = int(dataset.timestamps[-1])
    span = last - first
    cut = first + int(round(span * (1.0 - DEFAULT_HOLDOUT_FRACTION)))
    return cut, (
        f"default: the last {DEFAULT_HOLDOUT_FRACTION:.0%} of the "
        f"{span / (_MS_PER_HOUR * 24):.0f}-day sample span"
    )


def split_holdout(
    dataset: ft.Dataset, holdout_from_ms: int, embargo_bars: int
) -> Split:
    """Cut the dataset in time, purging the training side at the boundary.

    Three groups, not two:

    * **holdout** — every row whose feature timestamp is at or after the cut;
    * **boundary-purged** — rows before the cut whose *label* resolves on or
      after the first holdout bar, plus ``embargo_bars`` more. These are the
      dangerous ones. A row 12 bars before the cut, labelled over a 24-bar
      horizon, had its answer decided by bars that are in the holdout: training
      on it means the "never seen" period has already been seen, through the
      label, which is the leak nobody notices because the *features* look clean;
    * **train** — what is left.

    The embargo is applied on top of the label window because features are
    serially correlated well past the label horizon: a 264-bar moving average at
    the last training row shares 263 observations with the first holdout row.
    """
    if embargo_bars < 0:
        raise TrainingAborted("--embargo must be >= 0")
    stamps = np.asarray(dataset.timestamps, dtype=np.int64)
    is_holdout = stamps >= int(holdout_from_ms)
    holdout_pos = np.flatnonzero(is_holdout).astype(np.int64)
    before_pos = np.flatnonzero(~is_holdout).astype(np.int64)

    if holdout_pos.size == 0:
        return Split(
            holdout_from_ms=int(holdout_from_ms),
            holdout_from_source="",
            train_pos=before_pos,
            holdout_pos=holdout_pos,
            boundary_purged_pos=np.empty(0, dtype=np.int64),
            first_holdout_bar=None,
            embargo_bars=int(embargo_bars),
        )

    bar_index = np.asarray(dataset.bar_index, dtype=np.int64)
    label_end = np.asarray(dataset.outcome_end_index, dtype=np.int64)
    first_holdout_bar = int(np.min(bar_index[holdout_pos]))
    forbidden_from = first_holdout_bar - int(embargo_bars)

    overlaps = label_end[before_pos] >= forbidden_from
    return Split(
        holdout_from_ms=int(holdout_from_ms),
        holdout_from_source="",
        train_pos=before_pos[~overlaps],
        holdout_pos=holdout_pos,
        boundary_purged_pos=before_pos[overlaps],
        first_holdout_bar=first_holdout_bar,
        embargo_bars=int(embargo_bars),
    )


def usable_feature_columns(dataset: ft.Dataset) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """``(kept, dropped)`` feature names, by measurability, not by preference.

    ``policy.Policy.train`` refuses a matrix containing ``nan`` and is right to:
    imputing a missing feature manufactures a number the market never produced.
    On an OHLCV-only corpus the three book columns are ``nan`` for every row, so
    they are dropped *here*, by name, and the drop is printed and recorded in the
    manifest. The model that results is a different model from one trained with
    a book, and the artefact must be able to say so.
    """
    kept = dataset.usable_columns()
    dropped = tuple(n for n in dataset.feature_names if n not in set(kept))
    return kept, dropped


# ---------------------------------------------------------------------------
# the holdout evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HoldoutMetrics:
    """Scores on the block that was never trained on, calibrated or selected on.

    Every field is ``Optional`` for the same reason ``policy.FoldMetrics``'s are:
    a single-class holdout has no AUC, and reporting 0.5 there would let a
    degenerate split look like an honest coin flip rather than like an absence
    of evidence.
    """

    n_samples: int = 0
    base_rate: Optional[float] = None
    train_base_rate: Optional[float] = None
    brier: Optional[float] = None
    baseline_brier: Optional[float] = None
    auc: Optional[float] = None
    log_loss: Optional[float] = None
    accuracy: Optional[float] = None
    first_ms: Optional[int] = None
    last_ms: Optional[int] = None
    calibration: pol.CalibrationReport = field(default_factory=pol.CalibrationReport)

    @property
    def brier_skill(self) -> Optional[float]:
        if self.brier is None or self.baseline_brier in (None, 0.0):
            return None
        return float(1.0 - self.brier / self.baseline_brier)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n_samples": int(self.n_samples),
            "base_rate": self.base_rate,
            "train_base_rate": self.train_base_rate,
            "brier": self.brier,
            "baseline_brier": self.baseline_brier,
            "brier_skill": self.brier_skill,
            "auc": self.auc,
            "log_loss": self.log_loss,
            "accuracy": self.accuracy,
            "first_bar": format_ms(self.first_ms),
            "last_bar": format_ms(self.last_ms),
            "calibration": self.calibration.to_dict(),
        }


def evaluate_holdout(
    policy: pol.Policy,
    X: np.ndarray,
    y: np.ndarray,
    *,
    train_base_rate: Optional[float],
    timestamps: Optional[np.ndarray] = None,
    n_buckets: int = 10,
) -> HoldoutMetrics:
    """Score the final model once, on data it has never seen.

    The baseline is "always predict the *training* base rate" — the same
    baseline ``policy._run_fold`` uses, so the skill scores in the two halves of
    the report are comparable. Using the holdout's own base rate would be a
    baseline that had itself seen the future.

    Every metric here comes from ``policy``'s own functions. This is the one
    place in this file where a number is computed outside a walk-forward fold,
    and it is deliberately the *same* arithmetic on a different block of rows.
    """
    if X.shape[0] == 0:
        return HoldoutMetrics(n_samples=0, train_base_rate=train_base_rate)
    prob = policy.predict_proba(X)
    if prob is None:
        raise TrainingAborted(
            "the trained policy returned no probabilities for the holdout; "
            "there is no model to evaluate"
        )
    baseline = (
        None
        if train_base_rate is None
        else pol.brier_score(y, np.full(y.shape, float(train_base_rate)))
    )
    return HoldoutMetrics(
        n_samples=int(y.size),
        base_rate=float(np.mean(y)) if y.size else None,
        train_base_rate=train_base_rate,
        brier=pol.brier_score(y, prob),
        baseline_brier=baseline,
        auc=pol.roc_auc(y, prob),
        log_loss=pol.log_loss_score(y, prob),
        accuracy=pol.accuracy_at_threshold(y, prob, policy.threshold),
        first_ms=int(timestamps[0]) if timestamps is not None and timestamps.size else None,
        last_ms=int(timestamps[-1]) if timestamps is not None and timestamps.size else None,
        calibration=pol.calibration_report(y, prob, n_buckets=n_buckets),
    )


def holdout_verdict(
    metrics: HoldoutMetrics, criteria: pol.PromotionCriteria
) -> Tuple[bool, Tuple[str, ...]]:
    """Apply ``PromotionCriteria`` to the holdout. Same numbers, different rows.

    No new thresholds are invented here. The holdout is treated as one more fold
    that must clear the *same* bar as the walk-forward mean, because a model
    that ranks and calibrates well across four folds of 2018-2023 and falls
    apart on the most recent year has told you something about next year, and
    "the average was fine" is not a rebuttal.

    Fails closed on an absent or tiny holdout: a promotion granted because there
    was nothing left to check is the failure this whole file is arranged around.
    """
    reasons: List[str] = []
    if metrics.n_samples == 0:
        return False, ("HOLDOUT_ABSENT: no rows fell after the cut date, so the "
                       "model has never been tested out of sample",)
    if metrics.n_samples < MIN_HOLDOUT_SAMPLES:
        reasons.append(
            f"HOLDOUT_TOO_SMALL: {metrics.n_samples} < {MIN_HOLDOUT_SAMPLES} rows"
        )

    base = metrics.base_rate
    if base is None:
        reasons.append("HOLDOUT_NO_BASE_RATE")
    elif not (criteria.min_base_rate <= base <= criteria.max_base_rate):
        reasons.append(
            f"HOLDOUT_DEGENERATE_LABEL_BALANCE: base_rate={base:.4f} outside "
            f"[{criteria.min_base_rate}, {criteria.max_base_rate}]"
        )

    if metrics.auc is None:
        reasons.append("HOLDOUT_NO_AUC")
    elif metrics.auc < criteria.min_auc_mean:
        reasons.append(
            f"HOLDOUT_AUC_TOO_LOW: {metrics.auc:.4f} < {criteria.min_auc_mean}"
        )

    skill = metrics.brier_skill
    if skill is None:
        reasons.append("HOLDOUT_NO_BRIER_SKILL")
    elif skill < criteria.min_brier_skill:
        reasons.append(
            f"HOLDOUT_BRIER_NO_BETTER_THAN_BASE_RATE: skill={skill:.4f} < "
            f"{criteria.min_brier_skill} (brier={_fmt(metrics.brier)} vs "
            f"baseline {_fmt(metrics.baseline_brier)})"
        )

    ece = metrics.calibration.expected_calibration_error
    if ece is None:
        reasons.append("HOLDOUT_NO_CALIBRATION_MEASUREMENT")
    elif ece > criteria.max_calibration_error:
        reasons.append(
            f"HOLDOUT_MISCALIBRATED: ece={ece:.4f} > {criteria.max_calibration_error}"
        )
    return (not reasons), tuple(reasons)


# ---------------------------------------------------------------------------
# feature importance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Importance:
    """One feature's permutation importance: mean AUC lost, and the noise floor."""

    name: str
    mean_auc_drop: Optional[float]
    std_auc_drop: Optional[float]
    repeats: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature": self.name,
            "mean_auc_drop": self.mean_auc_drop,
            "std_auc_drop": self.std_auc_drop,
            "repeats": int(self.repeats),
        }


def permutation_importance(
    policy: pol.Policy,
    X: np.ndarray,
    y: np.ndarray,
    feature_names: Sequence[str],
    *,
    seed: int,
    repeats: int = IMPORTANCE_REPEATS,
) -> Tuple[Optional[float], Tuple[Importance, ...]]:
    """Shuffle each column, measure the AUC it takes with it.

    WHY PERMUTATION AND NOT ``feature_importances_``
    ------------------------------------------------
    There is no such attribute to read. The artefact is a
    ``CalibratedClassifierCV`` wrapping an ensemble of
    ``HistGradientBoostingClassifier``s, and split-count importances from a
    boosted tree are biased towards high-cardinality continuous columns anyway —
    which is every column here. Permutation asks the only question an operator
    cares about: if this feature carried no information, how much worse would
    the model be?

    WHAT IT IS FOR
    --------------
    Sanity, not selection. If ``hour_sin`` is the top feature, the model has
    found the clock rather than the market and somebody should say so out loud
    before it is promoted. Using this table to *drop* features and retrain is a
    model-selection decision, and if it is made against the holdout it has spent
    the holdout — which is why the report says which block this was measured on.

    Correlated features share credit and both look weak; that is a known
    limitation of permutation importance, not a defect in the model, and it is
    the reason the standard deviation across repeats is printed beside the mean.
    """
    if X.shape[0] == 0 or np.unique(y).size < 2:
        return None, tuple(
            Importance(name, None, None, 0) for name in feature_names
        )
    prob = policy.predict_proba(X)
    if prob is None:
        return None, tuple(Importance(n, None, None, 0) for n in feature_names)
    base_auc = pol.roc_auc(y, prob)
    if base_auc is None:
        return None, tuple(Importance(n, None, None, 0) for n in feature_names)

    rng = np.random.default_rng(int(seed))
    results: List[Importance] = []
    for column, name in enumerate(feature_names):
        drops: List[float] = []
        for _ in range(max(1, int(repeats))):
            shuffled = X.copy()
            shuffled[:, column] = rng.permutation(shuffled[:, column])
            permuted = policy.predict_proba(shuffled)
            auc = None if permuted is None else pol.roc_auc(y, permuted)
            if auc is not None:
                drops.append(float(base_auc - auc))
        if drops:
            results.append(
                Importance(
                    name,
                    float(np.mean(drops)),
                    float(np.std(drops)) if len(drops) > 1 else None,
                    len(drops),
                )
            )
        else:
            results.append(Importance(name, None, None, 0))
    ordered = tuple(
        sorted(
            results,
            key=lambda imp: (imp.mean_auc_drop is None, -(imp.mean_auc_drop or 0.0)),
        )
    )
    return float(base_auc), ordered


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


@dataclass
class TrainingRun:
    """One complete attempt: the inputs, the measurements, and the verdict.

    Held as a value so the report, the manifest and the write decision are all
    computed from the same object. A tool where "what was printed" and "what was
    written" are assembled separately is a tool that will one day print a
    refusal and write a promotion.
    """

    corpus: CorpusInfo
    dataset: ft.Dataset
    split: Split
    policy: pol.Policy
    report: pol.TrainingReport
    holdout: HoldoutMetrics
    holdout_ok: bool
    holdout_reasons: Tuple[str, ...]
    wf_ok: bool
    wf_reasons: Tuple[str, ...]
    importance: Tuple[Importance, ...]
    importance_base_auc: Optional[float]
    importance_measured_on: str
    feature_names: Tuple[str, ...]
    dropped_features: Tuple[str, ...]
    seed: int
    n_folds: int
    embargo: int
    run_id: str
    label: str
    fold_bounds: Tuple[Tuple[int, Optional[int], Optional[int]], ...]
    started_at: str
    git_commit: Optional[str]
    estimator_sha256: Optional[str] = None
    artefact_fingerprint: Optional[str] = None

    @property
    def promoted(self) -> bool:
        """The only promotion answer in this file. Both halves must agree."""
        return bool(self.wf_ok and self.holdout_ok)

    @property
    def all_reasons(self) -> Tuple[str, ...]:
        return tuple(self.wf_reasons) + tuple(self.holdout_reasons)


def compute_run_id(
    corpus: CorpusInfo,
    *,
    seed: int,
    n_folds: int,
    embargo: int,
    holdout_from_ms: int,
    hyperparameters: Mapping[str, Any],
) -> str:
    """A short, deterministic name for this run, computable before it starts.

    Derived from the *inputs* — corpus hash, schema, barriers, seed, split and
    hyper-parameters — rather than from the fitted model, so the output
    directory can be named before anything is fitted and two identical runs land
    in the same place instead of littering ``models/`` with timestamps. It is
    not the artefact hash; see :func:`artefact_fingerprint` for that.
    """
    payload = {
        "tool_version": TOOL_VERSION,
        "corpus_manifest_sha256": corpus.manifest_sha256,
        "corpus_files": dict(corpus.file_sha256),
        "symbol": corpus.symbol,
        "feature_schema_version": ft.FEATURE_SCHEMA_VERSION,
        "feature_schema_digest": ft.FEATURE_SCHEMA_DIGEST,
        "barriers": {
            "take_profit_atr": BARRIERS.take_profit_atr,
            "stop_atr": BARRIERS.stop_atr,
            "max_horizon": BARRIERS.max_horizon,
            "atr_period": BARRIERS.atr_period,
            "direction": BARRIERS.direction,
        },
        "seed": int(seed),
        "n_folds": int(n_folds),
        "embargo": int(embargo),
        "holdout_from_ms": int(holdout_from_ms),
        "hyperparameters": dict(hyperparameters),
        "promotion_criteria": PROMOTION_CRITERIA.to_dict(),
    }
    return _sha256_text(_canonical(payload))[:12]


def artefact_fingerprint(run: TrainingRun, estimator_sha256: str) -> str:
    """The reproducible content hash. Deliberately **not** ``content_hash``.

    ``policy.Policy.save`` writes a ``content_hash`` over the whole metadata
    document, and that document contains ``trained_at`` — a wall clock. It is
    the right hash for detecting tampering with a specific artefact directory
    and the wrong one for answering "did these inputs produce this model
    again?", because it changes every second whether the model changed or not.

    This fingerprint covers the fitted estimator's bytes plus everything about
    the run that a rerun should reproduce, and excludes every clock and every
    path. Two runs of this tool on the same corpus with the same flags produce
    the same fingerprint; that is asserted in ``tests/test_training.py``.
    """
    core = {
        "tool_version": TOOL_VERSION,
        "run_id": run.run_id,
        "estimator_sha256": estimator_sha256,
        "feature_schema_version": ft.FEATURE_SCHEMA_VERSION,
        "feature_schema_digest": ft.FEATURE_SCHEMA_DIGEST,
        "feature_names": list(run.feature_names),
        "label_definition": run.label,
        "seed": run.seed,
        "n_folds": run.n_folds,
        "embargo": run.embargo,
        "n_train": run.split.n_train,
        "n_holdout": run.split.n_holdout,
        "holdout_from_ms": run.split.holdout_from_ms,
        "folds": [f.to_dict() for f in run.report.folds],
        "calibration": run.report.calibration.to_dict(),
        "holdout": run.holdout.to_dict(),
        "promotion": {
            "promoted": run.promoted,
            "reasons": list(run.all_reasons),
        },
    }
    return _sha256_text(_canonical(core))


def fold_row_positions(
    dataset: ft.Dataset, split: Split, *, n_folds: int, embargo: int
) -> Tuple[Tuple[np.ndarray, np.ndarray], ...]:
    """Per-fold ``(train, validate)`` positions in the **dataset's** index space.

    ``policy.Policy.train`` builds these internally from the training subset and
    never exposes them. Rebuilding them here — through the same
    ``policy.purged_walk_forward_folds`` with the same arguments, not through a
    second implementation — is what lets a test assert that no holdout row is in
    any training fold, on indices rather than on a score.
    """
    train_pos = np.asarray(split.train_pos, dtype=np.int64)
    if train_pos.size == 0:
        return ()
    horizon = np.asarray(dataset.bars_to_resolution, dtype=np.int64)[train_pos]
    folds = pol.purged_walk_forward_folds(
        int(train_pos.size), horizon, n_folds=int(n_folds),
        embargo_bars=int(embargo), purge=True,
    )
    return tuple(
        (train_pos[f.train_index], train_pos[f.val_index]) for f in folds
    )


def train_policy(
    dataset: ft.Dataset,
    corpus: CorpusInfo,
    *,
    holdout_from: Optional[str],
    n_folds: int,
    embargo: int,
    seed: int,
) -> TrainingRun:
    """Split, fit, measure, and decide. Writes nothing.

    Ordered so that every decision is made before the number that would tempt
    someone to change it is visible: the holdout is cut first, the folds are run
    second, promotion is evaluated third, and the holdout is scored last.
    """
    if not pol.sklearn_available():
        raise TrainingAborted(
            "scikit-learn is not importable, so there is no model engine: "
            f"{pol.unavailability_reason()}. Install the dependency; this tool "
            "will not emit a placeholder artefact."
        )
    if len(dataset) == 0:
        raise TrainingAborted(
            "the dataset is empty — every bar was dropped. See the DATASET "
            "section for which reason dominated."
        )

    started_at = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cut_ms, cut_source = resolve_holdout_from(dataset, holdout_from)
    split = split_holdout(dataset, cut_ms, embargo)
    split = Split(
        holdout_from_ms=split.holdout_from_ms,
        holdout_from_source=cut_source,
        train_pos=split.train_pos,
        holdout_pos=split.holdout_pos,
        boundary_purged_pos=split.boundary_purged_pos,
        first_holdout_bar=split.first_holdout_bar,
        embargo_bars=split.embargo_bars,
    )
    if split.n_train == 0:
        raise TrainingAborted(
            f"no training rows survive the cut at {format_ms(cut_ms)}; the "
            "holdout boundary is before the start of the data"
        )

    kept, dropped = usable_feature_columns(dataset)
    if not kept:
        raise TrainingAborted(
            "no feature column is free of nan across the dataset; there is "
            "nothing a model can consume without imputing"
        )
    columns = [ft.FEATURE_INDEX[name] for name in kept]

    X_all = dataset.X[:, columns]
    y_all = binarise(dataset.y)
    X_train = X_all[split.train_pos]
    y_train = y_all[split.train_pos]
    ts_train = np.asarray(dataset.timestamps, dtype=np.int64)[split.train_pos]
    horizon_train = np.asarray(dataset.bars_to_resolution)[split.train_pos]

    hyperparameters = dict(pol.DEFAULT_HYPERPARAMETERS)
    hyperparameters["random_state"] = int(seed)

    run_id = compute_run_id(
        corpus, seed=seed, n_folds=n_folds, embargo=embargo,
        holdout_from_ms=cut_ms, hyperparameters=hyperparameters,
    )

    policy = pol.Policy(
        feature_names=kept,
        schema_version=ft.FEATURE_SCHEMA_VERSION,
        hyperparameters=hyperparameters,
        calibration_method=pol.DEFAULT_CALIBRATION_METHOD,
        promotion_criteria=PROMOTION_CRITERIA,
        require_promotion=True,
    )
    label = label_definition(dataset.barriers)
    report = policy.train(
        X_train, y_train, ts_train, horizon_train,
        label_definition=label,
        n_folds=int(n_folds),
        embargo_bars=int(embargo),
        purge=True,
    )
    wf_ok, wf_reasons = policy.meets_promotion_criteria()

    holdout = evaluate_holdout(
        policy,
        X_all[split.holdout_pos],
        y_all[split.holdout_pos],
        train_base_rate=float(np.mean(y_train)) if y_train.size else None,
        timestamps=np.asarray(dataset.timestamps, dtype=np.int64)[split.holdout_pos],
    )
    holdout_ok, holdout_reasons = holdout_verdict(holdout, PROMOTION_CRITERIA)

    if split.n_holdout > 0:
        base_auc, importance = permutation_importance(
            policy, X_all[split.holdout_pos], y_all[split.holdout_pos], kept,
            seed=seed,
        )
        measured_on = (
            f"the holdout block ({split.n_holdout} rows) — genuinely out of "
            "sample for this model"
        )
    else:
        base_auc, importance = permutation_importance(
            policy, X_train, y_train, kept, seed=seed
        )
        measured_on = (
            f"the training block ({split.n_train} rows) — IN SAMPLE for the "
            "final model, so these numbers are optimistic; there was no holdout"
        )

    fold_bounds: List[Tuple[int, Optional[int], Optional[int]]] = []
    for fold in report.folds:
        lo, hi = int(fold.val_start), int(fold.val_stop)
        first = int(ts_train[lo]) if 0 <= lo < ts_train.size else None
        last = int(ts_train[hi - 1]) if 0 < hi <= ts_train.size else None
        fold_bounds.append((int(fold.fold), first, last))

    return TrainingRun(
        corpus=corpus,
        dataset=dataset,
        split=split,
        policy=policy,
        report=report,
        holdout=holdout,
        holdout_ok=holdout_ok,
        holdout_reasons=holdout_reasons,
        wf_ok=wf_ok,
        wf_reasons=wf_reasons,
        importance=importance,
        importance_base_auc=base_auc,
        importance_measured_on=measured_on,
        feature_names=tuple(kept),
        dropped_features=tuple(dropped),
        seed=int(seed),
        n_folds=int(n_folds),
        embargo=int(embargo),
        run_id=run_id,
        label=label,
        fold_bounds=tuple(fold_bounds),
        started_at=started_at,
        git_commit=git_commit(),
    )


# ---------------------------------------------------------------------------
# the manifest
# ---------------------------------------------------------------------------


def build_manifest(run: TrainingRun, *, artefact_path: Optional[str]) -> Dict[str, Any]:
    """The provenance record written beside every artefact, promoted or not.

    It answers, without unpickling anything and without sklearn installed: what
    data, verified how; what feature schema, at what digest; what question the
    labels asked; which rows trained, which validated, which were held out, and
    over what dates; every metric; and whether it was promoted and why not.

    A model directory that cannot answer those is a pickle of unknown
    provenance, and the person who finds it in six months will assume the best.
    """
    dataset_report = run.dataset.report
    return {
        "manifest_schema": "policy-training/1",
        "tool": "tools/train_policy.py",
        "tool_version": TOOL_VERSION,
        "run_id": run.run_id,
        "started_at": run.started_at,
        "git_commit": run.git_commit,
        "artefact_path": artefact_path,
        "artefact_fingerprint": run.artefact_fingerprint,
        "estimator_sha256": run.estimator_sha256,
        "policy_content_hash": run.policy.artefact_hash,
        "artefact_schema": pol.ARTEFACT_SCHEMA,
        "determinism": {
            "seed": run.seed,
            "hyperparameters": dict(run.policy.hyperparameters),
            "note": (
                "artefact_fingerprint is reproducible across runs; "
                "policy_content_hash is not, because policy.save records the "
                "wall-clock training time inside the hashed document"
            ),
        },
        "corpus": run.corpus.to_dict(),
        "features": {
            "schema_version": ft.FEATURE_SCHEMA_VERSION,
            "schema_digest": ft.FEATURE_SCHEMA_DIGEST,
            "declared": list(ft.FEATURE_NAMES),
            "used": list(run.feature_names),
            "dropped_unmeasurable": list(run.dropped_features),
            "warmup_bars": ft.WARMUP_BARS,
        },
        "label": {
            "definition": run.label,
            "barriers": {
                "take_profit_atr": run.dataset.barriers.take_profit_atr,
                "stop_atr": run.dataset.barriers.stop_atr,
                "max_horizon": run.dataset.barriers.max_horizon,
                "atr_period": run.dataset.barriers.atr_period,
                "direction": run.dataset.barriers.direction,
                "reward_to_risk": run.dataset.barriers.reward_to_risk,
            },
            "counts_three_way": {
                ft.LABEL_NAMES[k]: int(v)
                for k, v in dataset_report.label_counts.items()
            },
            "binary_base_rate": run.report.base_rate,
        },
        "dataset": {
            "bars_seen": dataset_report.bars_seen,
            "rows_kept": dataset_report.rows_kept,
            "dropped_cold": dataset_report.dropped_cold,
            "dropped_missing_features": dataset_report.dropped_missing_features,
            "dropped_unresolved_label": dataset_report.dropped_unresolved_label,
            "always_missing": list(dataset_report.always_missing),
            "rows_with_book": dataset_report.rows_with_book,
        },
        "split": {
            "holdout_from": format_ms(run.split.holdout_from_ms),
            "holdout_from_source": run.split.holdout_from_source,
            "embargo_bars": run.embargo,
            "n_folds": run.n_folds,
            "train_validate": {
                "n_samples": run.split.n_train,
                "first_bar": format_ms(
                    int(run.dataset.timestamps[run.split.train_pos[0]])
                    if run.split.n_train else None
                ),
                "last_bar": format_ms(
                    int(run.dataset.timestamps[run.split.train_pos[-1]])
                    if run.split.n_train else None
                ),
            },
            "boundary_purged": {
                "n_samples": run.split.n_boundary_purged,
                "why": (
                    "rows before the cut whose label window reached into the "
                    "holdout (plus the embargo); training on them would mean "
                    "the holdout had already been seen through the label"
                ),
            },
            "holdout": {
                "n_samples": run.split.n_holdout,
                "first_bar": format_ms(run.holdout.first_ms),
                "last_bar": format_ms(run.holdout.last_ms),
            },
            "fold_validation_ranges": [
                {"fold": f, "first_bar": format_ms(a), "last_bar": format_ms(b)}
                for f, a, b in run.fold_bounds
            ],
        },
        "metrics": {
            "walk_forward": run.report.to_dict(),
            "holdout": run.holdout.to_dict(),
        },
        "feature_importance": {
            "method": "permutation, AUC drop",
            "measured_on": run.importance_measured_on,
            "base_auc": run.importance_base_auc,
            "repeats": IMPORTANCE_REPEATS,
            "ranked": [imp.to_dict() for imp in run.importance],
        },
        "promotion": {
            "promoted": run.promoted,
            "criteria": PROMOTION_CRITERIA.to_dict(),
            "min_holdout_samples": MIN_HOLDOUT_SAMPLES,
            "walk_forward_ok": run.wf_ok,
            "walk_forward_reasons": list(run.wf_reasons),
            "holdout_ok": run.holdout_ok,
            "holdout_reasons": list(run.holdout_reasons),
        },
        "warnings": [
            "This tool reports probability quality, not profit. No return, "
            "expectancy or Sharpe figure is produced anywhere in it.",
            "The holdout is single-use. Any hyper-parameter chosen after "
            "looking at the holdout metrics has spent it, and this manifest "
            "cannot detect that happening.",
        ],
    }


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteResult:
    """Where the artefact went, and where it deliberately did not."""

    artefact_path: Optional[str]
    live_path: Optional[str]
    dry_run: bool
    stale_live_warning: Optional[str] = None


def write_artefacts(
    run: TrainingRun, out_root: str, *, dry_run: bool
) -> WriteResult:
    """Write the artefact where its verdict says it belongs. Never anywhere else.

    Promoted: ``<out>/<run_id>/`` **and** ``<out>/current/``, the live path.
    Refused: ``<out>/rejected/<run_id>/`` only. The live path is not touched, not
    even to clear it — silently deleting a previously promoted model because
    today's run was worse is a surprise nobody needs during an incident, so a
    stale ``current`` is *reported* instead.

    ``dry_run`` writes nothing at all, including the manifest.
    """
    if dry_run:
        return WriteResult(artefact_path=None, live_path=None, dry_run=True)

    if run.promoted:
        artefact_path = os.path.join(out_root, run.run_id)
    else:
        artefact_path = os.path.join(out_root, REJECTED_DIRNAME, run.run_id)

    os.makedirs(artefact_path, exist_ok=True)
    run.policy.save(artefact_path)
    run.estimator_sha256 = _sha256_file(
        os.path.join(artefact_path, pol.ESTIMATOR_FILENAME)
    )
    run.artefact_fingerprint = artefact_fingerprint(run, run.estimator_sha256 or "")
    _write_manifest(artefact_path, build_manifest(run, artefact_path=artefact_path))

    live_path = os.path.join(out_root, LIVE_DIRNAME)
    if not run.promoted:
        stale = None
        if os.path.exists(os.path.join(live_path, pol.METADATA_FILENAME)):
            stale = (
                f"{live_path} still holds a PREVIOUSLY promoted model. This run "
                "did not replace it and did not remove it; it is still what a "
                "loader would serve."
            )
        return WriteResult(artefact_path, None, False, stale)

    os.makedirs(live_path, exist_ok=True)
    run.policy.save(live_path)
    _write_manifest(live_path, build_manifest(run, artefact_path=live_path))
    return WriteResult(artefact_path, live_path, False)


def _write_manifest(path: str, manifest: Mapping[str, Any]) -> str:
    target = os.path.join(path, MANIFEST_FILENAME)
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True, default=_json_ready)
        handle.write("\n")
    return target


def read_training_manifest(path: str) -> Dict[str, Any]:
    """Read an artefact's training manifest, or raise. No default document."""
    target = os.path.join(path, MANIFEST_FILENAME)
    try:
        with open(target, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except FileNotFoundError as exc:
        raise pol.ArtefactError(
            f"no {MANIFEST_FILENAME} at {target}: this artefact cannot state "
            "which feature schema it was built against, so it cannot be trusted"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise pol.ArtefactError(f"unreadable manifest at {target}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise pol.ArtefactError(f"manifest at {target} is not an object")
    return manifest


def load_artefact(path: str, *, require_promotion: bool = True) -> pol.Policy:
    """Load a trained artefact, refusing anything built against another schema.

    Two checks, in this order, and both must pass before the pickle is opened:

    1. ``features.require_schema_version(version, digest)`` — the version catches
       an intentional redefinition, the digest catches an unintentional one (a
       reordered ``FEATURE_SCHEMA`` that nobody thought worth a version bump).
       An artefact with no training manifest is refused outright, because a
       model that cannot state its digest cannot be checked against this one.
    2. ``policy.Policy.load`` — the exact ordered feature names and the artefact
       hashes.

    The failure this prevents is not a crash. It is a model that keeps returning
    confident, plausible, wrong probabilities from columns that mean something
    else now, straight into the term the edge gate multiplies by a reward.
    """
    manifest = read_training_manifest(path)
    features_block = manifest.get("features") or {}
    version = str(features_block.get("schema_version", ""))
    digest = features_block.get("schema_digest")
    ft.require_schema_version(version, str(digest) if digest else None)

    names = tuple(str(n) for n in features_block.get("used", ()))
    if not names:
        raise pol.ArtefactError(
            f"the manifest at {path} does not list the features the model used"
        )
    return pol.Policy.load(
        path,
        feature_names=names,
        schema_version=ft.FEATURE_SCHEMA_VERSION,
        require_promotion=require_promotion,
    )


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------

_RULE = "=" * 78


def render_report(run: TrainingRun, write: WriteResult) -> str:
    """Provenance first, verdict last, nothing optional.

    Same shape as ``tools/run_evaluation.py`` on purpose: an operator reading
    both should not have to learn two layouts to find the number that matters.
    """
    out: List[str] = []
    add = out.append

    # -- 1. provenance ----------------------------------------------------
    add(_RULE)
    add("DATA PROVENANCE")
    add(_RULE)
    corpus = run.corpus
    add(f"corpus            : {corpus.root}")
    add(f"symbol            : {corpus.symbol}")
    add(f"manifest sha256   : {corpus.manifest_sha256}")
    add(f"manifest verified : {corpus.files_checked} file(s) checked, "
        f"deep row count, 0 problems")
    if corpus.recompressed:
        add(f"  re-compressed but content-identical: {list(corpus.recompressed)}")
    add(f"synthetic         : {corpus.synthetic}")
    if corpus.synthetic:
        add("  *** THIS IS NOT A MARKET. Any model fitted here has learned a "
            "generator. ***")
    if corpus.source:
        add(f"source            : {corpus.source.get('exchange', '?')} "
            f"{corpus.source.get('instrument', '?')} "
            f"({corpus.source.get('native_resolution', '?')} native, licence "
            f"{corpus.source.get('licence', '?')})")
        add(f"source sha256     : {corpus.source.get('sha256')}")
    add(f"bars              : {corpus.n_bars} "
        f"[{format_ms(corpus.first_ms)} .. {format_ms(corpus.last_ms)}]")
    for note in corpus.notes:
        add(f"  note: {note}")
    add(f"code              : git {run.git_commit or 'unknown'}  "
        f"tool v{TOOL_VERSION}  started {run.started_at}")

    # -- 2. features and labels -------------------------------------------
    rep = run.dataset.report
    add("")
    add(_RULE)
    add("FEATURES AND LABELS")
    add(_RULE)
    add(f"schema            : {ft.FEATURE_SCHEMA_VERSION} "
        f"digest {ft.FEATURE_SCHEMA_DIGEST}")
    add(f"declared / used   : {len(ft.FEATURE_NAMES)} / {len(run.feature_names)}")
    if run.dropped_features:
        add(f"DROPPED (nan)     : {list(run.dropped_features)}")
        add("  dropped, not imputed: a fabricated value is a number the market "
            "never produced")
    add(f"label             : {run.label}")
    add(f"rows kept         : {rep.rows_kept} of {rep.bars_seen} bars "
        f"({(rep.kept_fraction or 0.0):.1%})")
    add(f"  dropped cold    : {rep.dropped_cold} (fewer than "
        f"{ft.WARMUP_BARS} bars of history)")
    add(f"  dropped missing : {rep.dropped_missing_features} (a required "
        "feature could not be measured)")
    add(f"  dropped unlabel : {rep.dropped_unresolved_label} (the barrier did "
        "not resolve inside the data)")
    counts = {ft.LABEL_NAMES[k]: v for k, v in rep.label_counts.items()}
    add(f"three-way outcome : {counts}")
    add(f"binary base rate  : {_fmt(run.report.base_rate)}  "
        f"(take_profit vs stop+timeout)")

    # -- 3. the split -----------------------------------------------------
    split = run.split
    add("")
    add(_RULE)
    add("SPLIT — THE HOLDOUT IS REMOVED BEFORE ANYTHING IS FITTED")
    add(_RULE)
    add(f"cut at            : {format_ms(split.holdout_from_ms)}  "
        f"({split.holdout_from_source})")
    if split.n_train:
        add(f"train + validate  : {split.n_train} rows "
            f"[{format_ms(int(run.dataset.timestamps[split.train_pos[0]]))} .. "
            f"{format_ms(int(run.dataset.timestamps[split.train_pos[-1]]))}]")
    add(f"boundary purged   : {split.n_boundary_purged} rows whose label window "
        f"reached into the holdout (+{split.embargo_bars} bar embargo)")
    add(f"holdout           : {split.n_holdout} rows "
        f"[{format_ms(run.holdout.first_ms)} .. {format_ms(run.holdout.last_ms)}]")
    add("")
    add("The holdout is not in any training fold, not in any calibration split,")
    add("and was scored exactly once, after promotion had already been decided")
    add("on the walk-forward folds. IT IS SINGLE USE: any hyper-parameter, any")
    add("feature choice, any barrier width picked after reading the HOLDOUT")
    add("section below has burned it, and every later run of this tool will")
    add("report an in-sample number under an out-of-sample heading.")

    # -- 4. walk-forward --------------------------------------------------
    add("")
    add(_RULE)
    add("WALK-FORWARD — OUT-OF-SAMPLE ONLY (holdout excluded)")
    add(_RULE)
    add(f"folds={run.n_folds}  embargo={run.embargo} bars  purge=on  "
        f"calibration={run.report.calibration_method} "
        f"via {run.report.calibration_cv}")
    add(f"{'fold':>4}{'train':>8}{'purged':>8}{'embargo':>8}{'val':>7}"
        f"{'brier':>9}{'base':>9}{'skill':>9}{'auc':>9}{'logloss':>9}{'acc':>8}")
    for fold in run.report.folds:
        add(f"{fold.fold:>4}{fold.n_train:>8}{fold.n_purged:>8}"
            f"{fold.n_embargoed:>8}{fold.n_val:>7}"
            f"{_fmt(fold.brier):>9}{_fmt(fold.baseline_brier):>9}"
            f"{_fmt(fold.brier_skill):>9}{_fmt(fold.auc):>9}"
            f"{_fmt(fold.log_loss):>9}{_fmt(fold.accuracy):>8}")
    for fold_no, first, last in run.fold_bounds:
        add(f"  fold {fold_no} validated on "
            f"[{format_ms(first)} .. {format_ms(last)}]")
    usable = run.report.usable_folds
    add("")
    add(f"usable folds      : {len(usable)}/{len(run.report.folds)}")
    skipped = [
        f"fold {f.fold}: {f.skipped_reason}"
        for f in run.report.folds
        if f.skipped_reason
    ]
    if skipped:
        add(f"UNUSABLE FOLDS    : {skipped}")
    add(f"auc mean/min/std  : {_fmt(run.report.auc_mean)} / "
        f"{_fmt(run.report.auc_min)} / {_fmt(run.report.auc_std)}")
    add(f"brier / baseline  : {_fmt(run.report.brier_mean)} / "
        f"{_fmt(run.report.baseline_brier_mean)}  "
        f"skill {_fmt(run.report.brier_skill)}")
    coin_flip = [
        f"fold {f.fold}={f.auc:.4f}"
        for f in usable
        if f.auc is not None and f.auc <= 0.5
    ]
    add(f"FOLDS AT OR BELOW A COIN FLIP : {coin_flip or 'none'}")
    negative_skill = [
        f"fold {f.fold}={f.brier_skill:.4f}"
        for f in usable
        if f.brier_skill is not None and f.brier_skill < 0.0
    ]
    add(f"FOLDS WORSE THAN THE BASE RATE: {negative_skill or 'none'}")

    # -- 5. calibration ---------------------------------------------------
    add("")
    add(_RULE)
    add("CALIBRATION — POOLED OUT-OF-SAMPLE (the evidence the gate needs)")
    add(_RULE)
    add("risk_management._gate_expected_edge computes "
        "p*win_bps - (1-p)*loss_bps - cost.")
    add("It multiplies p by a reward, so a p of 0.70 that wins 55% of the time "
        "does not")
    add("degrade the decision gracefully — it manufactures an edge that is not "
        "there.")
    add("This table is the only evidence that p means what it says.")
    add("")
    add(run.report.calibration.format_table())
    monotone = run.report.calibration.is_monotone()
    add("")
    add(f"realised frequency rises with predicted probability : {monotone}")

    # -- 6. importance ----------------------------------------------------
    add("")
    add(_RULE)
    add("FEATURE IMPORTANCE — PERMUTATION (AUC lost when the column is shuffled)")
    add(_RULE)
    add(f"measured on       : {run.importance_measured_on}")
    add(f"base auc          : {_fmt(run.importance_base_auc)}  "
        f"repeats={IMPORTANCE_REPEATS}  seed={run.seed}")
    add("A top feature that is a clock (hour_*, dow_*) rather than a market "
        "measurement is")
    add("a reason to stop and look, not a result. Correlated features split the "
        "credit and")
    add("both look weak, so read the whole list, not the first row.")
    add("")
    add(f"  {'feature':<24}{'auc drop':>11}{'std':>10}")
    add("  " + "-" * 45)
    for imp in run.importance:
        add(f"  {imp.name:<24}{_fmt(imp.mean_auc_drop):>11}"
            f"{_fmt(imp.std_auc_drop):>10}")

    # -- 7. holdout -------------------------------------------------------
    hold = run.holdout
    add("")
    add(_RULE)
    add("HOLDOUT — NEVER TRAINED ON, NEVER SELECTED ON, SCORED ONCE")
    add(_RULE)
    if hold.n_samples == 0:
        add("NO HOLDOUT ROWS. The model has not been tested out of sample at "
            "all, and promotion is refused on that ground alone.")
    else:
        add(f"period            : {format_ms(hold.first_ms)} .. "
            f"{format_ms(hold.last_ms)}   ({hold.n_samples} rows)")
        add(f"base rate         : {_fmt(hold.base_rate)}  "
            f"(training base rate {_fmt(hold.train_base_rate)})")
        add(f"brier / baseline  : {_fmt(hold.brier)} / "
            f"{_fmt(hold.baseline_brier)}  skill {_fmt(hold.brier_skill)}")
        add(f"auc               : {_fmt(hold.auc)}")
        add(f"logloss           : {_fmt(hold.log_loss)}")
        add(f"accuracy@{run.policy.threshold:.2f}     : {_fmt(hold.accuracy)}")
        add("")
        add(hold.calibration.format_table())

    # -- 8. verdict -------------------------------------------------------
    add("")
    add(_RULE)
    add("PROMOTION VERDICT")
    add(_RULE)
    if run.promoted:
        add("PROMOTED — the artefact cleared every criterion on the "
            "walk-forward folds")
        add("and on the untouched holdout. This is not permission to trade "
            "live: it is")
        add("permission for the artefact to exist at the live model path. "
            "Paper and")
        add("testnet stages are unchanged, and every risk gate still runs in "
            "front of it.")
    else:
        add("REFUSED — this model may not influence a trade.")
    add("")
    add(f"walk-forward criteria : {'PASS' if run.wf_ok else 'FAIL'}")
    for reason in run.wf_reasons:
        add(f"  - {reason}")
    if run.wf_ok:
        add("  (no objections)")
    add(f"holdout criteria      : {'PASS' if run.holdout_ok else 'FAIL'}")
    for reason in run.holdout_reasons:
        add(f"  - {reason}")
    if run.holdout_ok:
        add("  (no objections)")
    add("")
    add("thresholds (policy.PromotionCriteria — change them in code, in a "
        "reviewable diff):")
    for key, value in sorted(PROMOTION_CRITERIA.to_dict().items()):
        add(f"  {key:<24} {value}")
    add(f"  {'min_holdout_samples':<24} {MIN_HOLDOUT_SAMPLES}"
        "   (tools/train_policy.py)")

    add("")
    if write.dry_run:
        add("--dry-run: NOTHING WAS WRITTEN. No artefact, no manifest, no "
            "change to the live model path.")
    elif run.promoted:
        add(f"artefact          : {write.artefact_path}")
        add(f"LIVE MODEL PATH   : {write.live_path}")
    else:
        add(f"artefact          : {write.artefact_path}")
        add("LIVE MODEL PATH   : NOT WRITTEN. A refused model is written under "
            f"{REJECTED_DIRNAME}/ so it can be")
        add("                    inspected, and nowhere else. A tool that "
            "writes a model regardless")
        add("                    of quality is a tool that will eventually "
            "promote a noise model.")
        if write.stale_live_warning:
            add(f"WARNING           : {write.stale_live_warning}")
    if run.artefact_fingerprint:
        add(f"fingerprint       : {run.artefact_fingerprint}")
        add("                    (reproducible: same corpus, same flags, same "
            "hash)")
    add("")
    add("This tool produced no profit estimate, and cannot. It measures whether "
        "a")
    add("probability is honest. Whether an honest probability is a profitable "
        "one is")
    add("a question for backtest.py, with fees, slippage and a position sizer.")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# the CLI
# ---------------------------------------------------------------------------


GITIGNORE_LINES = (
    "models/**",
    "!models/**/",
    f"!models/**/{MANIFEST_FILENAME}",
)


def build_parser() -> argparse.ArgumentParser:
    """The flags. Note what is absent: anything that lowers the bar.

    Every promotion threshold lives in ``policy.PromotionCriteria`` and in the
    two module constants named in the report. Exposing them here would let a
    failing run be turned into a passing one from a shell history entry that
    nobody reviews.
    """
    parser = argparse.ArgumentParser(
        description="Train the policy on a verified corpus and refuse to "
                    "promote it unless the evidence says otherwise.",
    )
    parser.add_argument(
        "--data-dir", default=os.path.join(REPO, "data", "real"),
        help="corpus root containing MANIFEST.json and ohlcv/ (default: data/real)",
    )
    parser.add_argument(
        "--symbol", default=None,
        help="which symbol to train on; required if the corpus has more than one",
    )
    parser.add_argument(
        "--holdout-from", default=None,
        help="ISO date (UTC). Everything from here is held out entirely. "
             f"Default: the last {DEFAULT_HOLDOUT_FRACTION:.0%} of the sample span.",
    )
    parser.add_argument(
        "--folds", type=int, default=pol.DEFAULT_N_FOLDS,
        help=f"purged walk-forward folds (default {pol.DEFAULT_N_FOLDS})",
    )
    parser.add_argument(
        "--embargo", type=int, default=ft.MAX_LOOKBACK,
        help="bars embargoed between a training block and the block it is "
             f"scored on. Default {ft.MAX_LOOKBACK} = features.MAX_LOOKBACK, "
             "because the longest feature look-back is how far serial "
             "correlation reaches.",
    )
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED,
        help=f"random_state for the estimator and the permutation importance "
             f"(default {DEFAULT_SEED})",
    )
    parser.add_argument(
        "--out", default=DEFAULT_MODELS_ROOT,
        help="artefact root (default: models/)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="train and report, write nothing at all",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Load, build, train, report, and write only what the verdict allows.

    Returns 0 when a model was promoted and 1 when it was not. A refusal is a
    successful *run* and an unsuccessful *model*, and the exit code reports the
    model, so a CI job that trains nightly fails loudly rather than accumulating
    green ticks over a corpus that has stopped supporting an edge.
    """
    args = build_parser().parse_args(argv)

    try:
        bars, corpus = load_corpus_bars(args.data_dir, args.symbol)
        dataset = ft.build_dataset(bars, barriers=BARRIERS)
        run = train_policy(
            dataset, corpus,
            holdout_from=args.holdout_from,
            n_folds=args.folds,
            embargo=args.embargo,
            seed=args.seed,
        )
    except TrainingAborted as exc:
        print(_RULE)
        print("ABORTED BEFORE TRAINING")
        print(_RULE)
        print(str(exc))
        print("\nNothing was fitted and nothing was written.")
        return 2
    except (md.MarketDataError, ft.SchemaMismatch, pol.PolicyError, ValueError) as exc:
        print(_RULE)
        print("ABORTED")
        print(_RULE)
        print(f"{type(exc).__name__}: {exc}")
        print("\nNo artefact was written.")
        return 2

    # A dry run never touches the disk, so there are no estimator bytes to hash
    # and ``run.artefact_fingerprint`` stays ``None``. Fingerprinting the pickle
    # in memory instead would print a number that looks like the one a real run
    # quotes and is computed from different bytes, so the dry run quotes none.
    write = write_artefacts(run, args.out, dry_run=args.dry_run)

    print(render_report(run, write))
    print("")
    print("`.gitignore` needs these lines so models/ is ignored except its "
          "manifests:")
    for line in GITIGNORE_LINES:
        print(f"    {line}")
    return 0 if run.promoted else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
