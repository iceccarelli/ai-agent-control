"""Contract tests for ``tools/train_policy.py`` — the offline training loop.

Fully offline. No network, no state store, no config, and — deliberately — no
training on the real 61,513-bar corpus. A test suite that spends three minutes
building features is a test suite nobody runs before pushing, and the properties
being defended here are structural, not statistical: they hold on eight thousand
fabricated rows exactly as they hold on seven years of Bitstamp.

The failure modes this file exists to catch, each of which has ended a real
trading system:

* **a tool that writes a model regardless of quality.** A noise model must be
  refused, and refusing it must mean nothing appears at the live model path —
  not "appears with a warning", not "appears with ``promoted: false`` inside".
  This is the most important test here;
* **a holdout that is not held out.** Asserted on *indices*: no holdout row in
  any training fold, and — the subtle one — no training row whose triple-barrier
  label was resolved by a bar inside the holdout. The features can look
  perfectly clean while the answer has already leaked through the label;
* **an artefact nobody can date.** The manifest must record the corpus hash, the
  feature schema version *and* digest, the split dates, the sample counts, the
  label definition, every metric and the verdict;
* **a model served against redefined features.** An artefact whose schema
  version or digest does not match this module's must be refused at load, before
  the pickle is opened;
* **irreproducibility.** The same inputs must produce the same artefact bytes,
  or none of the above is checkable a week later.

Two fabricated datasets carry most of the file, built once at module scope: one
with a genuinely learnable logistic signal, one with none at all. Both go
through the *real* ``policy.Policy.train``; only the feature computation is
skipped, because ``tests/test_features.py`` already owns that.
"""
from __future__ import annotations

import ast
import gzip
import hashlib
import json
import os
import re
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import features as ft  # noqa: E402
import market_data as md  # noqa: E402
import policy as pol  # noqa: E402
import train_policy as tp  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL_PATH = os.path.join(REPO, "tools", "train_policy.py")

pytestmark = pytest.mark.skipif(
    not pol.sklearn_available(),
    reason=f"scikit-learn is unavailable: {pol.unavailability_reason()}",
)

_OHLCV_COLUMNS = (
    "time_period_start", "time_period_end", "time_open", "time_close",
    "price_open", "price_high", "price_low", "price_close",
    "volume_traded", "trades_count",
)

#: 2018-01-01T00:00:00Z in microseconds — the same epoch the real corpus starts
#: at, so a date printed by a test looks like a date printed by a real run.
_EPOCH_US = 1_514_764_800_000_000
_HOUR_US = 3_600_000_000


# ---------------------------------------------------------------------------
# fabricated inputs
# ---------------------------------------------------------------------------


def _dataset_report(rows: int, bars: int) -> ft.DatasetReport:
    """A plausible ``DatasetReport`` for a fabricated dataset.

    Constructed rather than obtained from ``build_dataset`` because these tests
    are about what the *trainer* does with a report, not about how the report is
    produced; ``tests/test_features.py`` owns the latter and asserts it row by
    row.
    """
    return ft.DatasetReport(
        bars_seen=bars,
        rows_kept=rows,
        dropped_cold=ft.WARMUP_BARS,
        dropped_missing_features=0,
        dropped_unresolved_label=max(0, bars - rows - ft.WARMUP_BARS),
        missing_by_feature={name: 0 for name in ft.FEATURE_NAMES},
        label_counts={
            ft.LABEL_TAKE_PROFIT: rows // 2,
            ft.LABEL_STOP: rows - rows // 2,
            ft.LABEL_TIMEOUT: 0,
        },
        always_missing=ft.BOOK_FEATURE_NAMES,
        rows_with_book=0,
        required=ft.PRICE_FEATURE_NAMES,
        schema_version=ft.FEATURE_SCHEMA_VERSION,
        schema_digest=ft.FEATURE_SCHEMA_DIGEST,
        barriers=ft.BarrierSpec(),
    )


def make_dataset(
    n: int = 8000,
    *,
    seed: int = 0,
    signal: float = 1.3,
    horizon: int = 24,
) -> ft.Dataset:
    """A dataset with the real schema, real ``nan`` book columns, and a known signal.

    ``signal=0`` is pure noise — the label is a coin flip independent of every
    column — and is the input a training tool must refuse. Anything above zero
    is a well-specified logistic model, which a calibrated classifier should
    both rank and calibrate well; that is what makes "this promotes" a
    meaningful assertion rather than a lucky seed.

    The three book columns are ``nan`` for every row, exactly as they are on an
    OHLCV-only corpus, so every test in this file exercises the column-selection
    path rather than a tidier hypothetical one.
    """
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, len(ft.FEATURE_NAMES)))
    for name in ft.BOOK_FEATURE_NAMES:
        X[:, ft.FEATURE_INDEX[name]] = np.nan
    score = signal * X[:, ft.FEATURE_INDEX["ret_24"]] + (
        0.9 * signal / 1.3
    ) * X[:, ft.FEATURE_INDEX["rsi_norm"]]
    probability = 1.0 / (1.0 + np.exp(-score))
    outcome = np.where(
        rng.random(n) < probability, ft.LABEL_TAKE_PROFIT, ft.LABEL_STOP
    ).astype(np.int8)
    timestamps = _EPOCH_US // 1000 + np.arange(n, dtype=np.int64) * 3_600_000
    return ft.Dataset(
        X=X,
        y=outcome,
        timestamps=timestamps,
        bar_index=np.arange(n, dtype=np.int64) + ft.WARMUP_BARS,
        bars_to_resolution=np.full(n, horizon, dtype=np.int32),
        feature_names=ft.FEATURE_NAMES,
        schema_version=ft.FEATURE_SCHEMA_VERSION,
        schema_digest=ft.FEATURE_SCHEMA_DIGEST,
        barriers=ft.BarrierSpec(),
        report=_dataset_report(n, n + ft.WARMUP_BARS + 24),
    )


def make_corpus_info(**overrides) -> tp.CorpusInfo:
    defaults = dict(
        root="/nowhere/data/fake",
        symbol="BTCUSD",
        n_bars=8000,
        first_ms=_EPOCH_US // 1000,
        last_ms=_EPOCH_US // 1000 + 8000 * 3_600_000,
        manifest_sha256="a" * 64,
        file_sha256={"ohlcv/TEST_SPOT_BTC_USD_1H.csv.gz": "b" * 64},
        synthetic=True,
        source={"exchange": "TEST", "instrument": "BTC/USD"},
        notes=("no order book in this corpus",),
        files_checked=1,
        recompressed=(),
    )
    defaults.update(overrides)
    return tp.CorpusInfo(**defaults)


def _bars_csv(rows, start_us: int = _EPOCH_US, step_us: int = _HOUR_US) -> str:
    lines = [",".join(_OHLCV_COLUMNS)]
    for i, (o, h, l, c, v) in enumerate(rows):
        start = start_us + i * step_us
        end = start + step_us
        lines.append(",".join([
            md.format_timestamp(start), md.format_timestamp(end),
            md.format_timestamp(start), md.format_timestamp(end - 60_000_000),
            f"{o:.2f}", f"{h:.2f}", f"{l:.2f}", f"{c:.2f}", f"{v:.4f}", "0",
        ]))
    return "\n".join(lines) + "\n"


def write_corpus(root: str, n_bars: int = 900, *, seed: int = 5,
                 symbol: str = "TEST_SPOT_BTC_USD_1H") -> str:
    """Write a minimal but genuinely valid corpus: gz OHLCV plus a real manifest.

    Real hashes, because half the point of these tests is that a corpus whose
    bytes do not match its manifest is refused before a single feature is
    computed. A fixture with a fake hash could not tell a working check from a
    check that always passes.
    """
    rng = np.random.default_rng(seed)
    price = 20_000.0
    rows = []
    for _ in range(n_bars):
        step = rng.normal(0.0, 0.006)
        opened = price
        price = max(1.0, price * (1.0 + step))
        high = max(opened, price) * (1.0 + abs(rng.normal(0, 0.002)))
        low = min(opened, price) * (1.0 - abs(rng.normal(0, 0.002)))
        rows.append((opened, high, low, price, 100.0 + abs(rng.normal(0, 10))))

    os.makedirs(os.path.join(root, "ohlcv"), exist_ok=True)
    relpath = f"ohlcv/{symbol}.csv.gz"
    raw = _bars_csv(rows).encode("utf-8")
    blob = gzip.compress(raw, mtime=0)
    with open(os.path.join(root, relpath), "wb") as handle:
        handle.write(blob)
    manifest = {
        "schema": "coinapi-flat-files/1",
        "synthetic": True,
        "bar_seconds": 3600,
        "source": {"exchange": "TEST", "instrument": "BTC/USD", "licence": "n/a"},
        "files": {
            relpath: {
                "kind": "ohlcv", "data_kind": "ohlcv", "interval_seconds": 3600,
                "rows": len(rows),
                "sha256": hashlib.sha256(blob).hexdigest(),
                "sha256_uncompressed": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(blob),
            }
        },
    }
    with open(os.path.join(root, "MANIFEST.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=1, sort_keys=True)
    return root


# ---------------------------------------------------------------------------
# module-scoped runs — the expensive fixtures, built once
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def learnable_run() -> tp.TrainingRun:
    """A run on a signal a calibrated model can actually find. Should promote."""
    return tp.train_policy(
        make_dataset(8000, seed=0, signal=1.3),
        make_corpus_info(),
        holdout_from=None, n_folds=4, embargo=30, seed=7,
    )


@pytest.fixture(scope="module")
def noise_run() -> tp.TrainingRun:
    """A run on pure noise. Must be refused; everything else here is secondary."""
    return tp.train_policy(
        make_dataset(6000, seed=1, signal=0.0),
        make_corpus_info(n_bars=6000),
        holdout_from=None, n_folds=4, embargo=30, seed=7,
    )


@pytest.fixture(scope="module")
def small_corpus(tmp_path_factory) -> str:
    """A tiny on-disk corpus for the end-to-end CLI paths."""
    return write_corpus(str(tmp_path_factory.mktemp("corpus")), n_bars=900)


# ---------------------------------------------------------------------------
# 1. the CLI surface
# ---------------------------------------------------------------------------


class TestCliSurface:
    """The flags, and — more importantly — the flags that must not exist."""

    def test_every_documented_flag_is_accepted(self):
        args = tp.build_parser().parse_args([
            "--data-dir", "d", "--holdout-from", "2024-01-01", "--folds", "3",
            "--embargo", "7", "--seed", "11", "--out", "o", "--dry-run",
        ])
        assert args.data_dir == "d"
        assert args.holdout_from == "2024-01-01"
        assert args.folds == 3
        assert args.embargo == 7
        assert args.seed == 11
        assert args.out == "o"
        assert args.dry_run is True

    def test_defaults_are_the_real_corpus_and_no_dry_run(self):
        args = tp.build_parser().parse_args([])
        assert args.data_dir.endswith(os.path.join("data", "real"))
        assert args.dry_run is False
        assert args.holdout_from is None

    def test_default_seed_and_folds_come_from_the_modules_that_own_them(self):
        args = tp.build_parser().parse_args([])
        assert args.folds == pol.DEFAULT_N_FOLDS
        assert args.seed == tp.DEFAULT_SEED

    def test_default_embargo_is_the_longest_feature_lookback(self):
        # Serial correlation in the features reaches exactly as far as the
        # longest window that produces them; a shorter default would be a number
        # chosen for convenience.
        assert tp.build_parser().parse_args([]).embargo == ft.MAX_LOOKBACK

    def test_no_flag_can_lower_a_promotion_threshold(self):
        """The single most dangerous flag this tool could grow."""
        forbidden = (
            "auc", "brier", "ece", "calibration-error", "threshold", "min-",
            "max-", "criteria", "promote", "force", "skip", "no-verify",
        )
        options = [
            option
            for action in tp.build_parser()._actions
            for option in action.option_strings
        ]
        for option in options:
            lowered = option.lstrip("-").lower()
            assert not any(bad in lowered for bad in forbidden), (
                f"{option} looks like it could weaken the promotion bar; "
                "thresholds belong in policy.PromotionCriteria where a change "
                "is a reviewable diff"
            )

    def test_promotion_criteria_are_policys_defaults_unmodified(self):
        assert tp.PROMOTION_CRITERIA == pol.PromotionCriteria()

    def test_the_tool_does_not_construct_a_loosened_criteria_object(self):
        """No ``PromotionCriteria(min_auc_mean=...)`` anywhere in the source."""
        tree = ast.parse(open(TOOL_PATH, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                target = node.func
                name = getattr(target, "attr", getattr(target, "id", ""))
                if name == "PromotionCriteria":
                    assert not node.args and not node.keywords, (
                        "PromotionCriteria is constructed with overrides; the "
                        "bar must be policy's, unaltered"
                    )

    def test_gitignore_lines_ignore_models_but_keep_manifests(self):
        assert tp.GITIGNORE_LINES[0] == "models/**"
        assert "!models/**/" in tp.GITIGNORE_LINES
        assert f"!models/**/{tp.MANIFEST_FILENAME}" in tp.GITIGNORE_LINES


# ---------------------------------------------------------------------------
# 2. the corpus must verify before anything is fitted
# ---------------------------------------------------------------------------


class TestCorpusVerification:
    def test_a_good_corpus_verifies(self, small_corpus):
        report = tp.verify_corpus(small_corpus)
        assert report.ok
        assert report.checked == 1

    def test_a_tampered_corpus_aborts(self, tmp_path):
        root = write_corpus(str(tmp_path / "bad"), n_bars=400)
        target = os.path.join(root, "ohlcv", "TEST_SPOT_BTC_USD_1H.csv.gz")
        with open(target, "ab") as handle:
            handle.write(b"garbage")
        with pytest.raises(tp.TrainingAborted, match="does not match its manifest"):
            tp.verify_corpus(root)

    def test_a_missing_manifest_aborts(self, tmp_path):
        root = write_corpus(str(tmp_path / "nomani"), n_bars=400)
        os.remove(os.path.join(root, "MANIFEST.json"))
        with pytest.raises(tp.TrainingAborted):
            tp.verify_corpus(root)

    def test_a_row_count_that_disagrees_with_the_manifest_aborts(self, tmp_path):
        """The case a hash alone cannot see: edited file, regenerated hash."""
        root = write_corpus(str(tmp_path / "rows"), n_bars=400)
        manifest_path = os.path.join(root, "MANIFEST.json")
        manifest = json.load(open(manifest_path, encoding="utf-8"))
        entry = next(iter(manifest["files"].values()))
        entry["rows"] = 999_999
        json.dump(manifest, open(manifest_path, "w", encoding="utf-8"))
        with pytest.raises(tp.TrainingAborted):
            tp.verify_corpus(root)

    def test_verification_happens_before_a_single_feature_is_built(
        self, tmp_path, monkeypatch, capsys
    ):
        """The property, not the implementation: unverified bytes are never fitted."""
        root = write_corpus(str(tmp_path / "bad2"), n_bars=400)
        with open(os.path.join(root, "ohlcv", "TEST_SPOT_BTC_USD_1H.csv.gz"), "ab") as fh:
            fh.write(b"x")

        def explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("build_dataset ran on an unverified corpus")

        monkeypatch.setattr(tp.ft, "build_dataset", explode)
        out_root = str(tmp_path / "models")
        assert tp.main(["--data-dir", root, "--out", out_root]) == 2
        assert not os.path.exists(out_root)
        assert "ABORTED BEFORE TRAINING" in capsys.readouterr().out

    def test_an_unknown_symbol_aborts_and_lists_what_is_there(self, small_corpus):
        with pytest.raises(tp.TrainingAborted, match="BTCUSD"):
            tp.load_corpus_bars(small_corpus, "DOGEUSDT")

    def test_a_single_symbol_corpus_needs_no_symbol_flag(self, small_corpus):
        bars, info = tp.load_corpus_bars(small_corpus, None)
        assert len(bars) == 900
        assert info.symbol == "BTCUSD"

    def test_corpus_info_carries_the_manifest_hash(self, small_corpus):
        _bars, info = tp.load_corpus_bars(small_corpus, None)
        expected = hashlib.sha256(
            open(os.path.join(small_corpus, "MANIFEST.json"), "rb").read()
        ).hexdigest()
        assert info.manifest_sha256 == expected
        assert info.file_sha256, "the per-file hashes must be recorded too"

    def test_a_synthetic_corpus_is_flagged_as_synthetic(self, small_corpus):
        _bars, info = tp.load_corpus_bars(small_corpus, None)
        assert info.synthetic is True


# ---------------------------------------------------------------------------
# 3. the split
# ---------------------------------------------------------------------------


class TestSplit:
    def test_parse_cut_date_accepts_a_plain_date_as_utc(self):
        assert tp.parse_cut_date("2024-01-01") == 1_704_067_200_000

    def test_parse_cut_date_accepts_a_trailing_z(self):
        assert tp.parse_cut_date("2024-01-01T00:00:00Z") == 1_704_067_200_000

    def test_parse_cut_date_refuses_garbage(self):
        with pytest.raises(tp.TrainingAborted, match="ISO-8601"):
            tp.parse_cut_date("last tuesday")

    def test_the_default_cut_leaves_the_configured_fraction_in_the_holdout(self):
        dataset = make_dataset(1000, signal=0.0)
        cut, source = tp.resolve_holdout_from(dataset, None)
        held = int(np.sum(dataset.timestamps >= cut))
        assert 0.15 * len(dataset) <= held <= 0.25 * len(dataset)
        assert "default" in source

    def test_an_explicit_cut_is_reported_as_explicit(self):
        dataset = make_dataset(500, signal=0.0)
        cut, source = tp.resolve_holdout_from(dataset, "2018-01-10")
        assert cut == tp.parse_cut_date("2018-01-10")
        assert "--holdout-from" in source

    def test_train_and_holdout_are_disjoint(self):
        dataset = make_dataset(1000, signal=0.0)
        split = tp.split_holdout(dataset, tp.resolve_holdout_from(dataset, None)[0], 10)
        assert not (set(split.train_pos.tolist()) & set(split.holdout_pos.tolist()))

    def test_no_training_row_is_timestamped_after_the_cut(self):
        dataset = make_dataset(1000, signal=0.0)
        cut = tp.resolve_holdout_from(dataset, None)[0]
        split = tp.split_holdout(dataset, cut, 10)
        assert np.all(dataset.timestamps[split.train_pos] < cut)
        assert np.all(dataset.timestamps[split.holdout_pos] >= cut)

    def test_no_training_label_was_resolved_by_a_holdout_bar(self):
        """The leak that the feature columns cannot show you."""
        dataset = make_dataset(1000, signal=0.0, horizon=24)
        cut = tp.resolve_holdout_from(dataset, None)[0]
        split = tp.split_holdout(dataset, cut, 10)
        label_end = dataset.outcome_end_index[split.train_pos]
        assert label_end.max() < split.first_holdout_bar - split.embargo_bars

    def test_the_boundary_purge_actually_removes_rows(self):
        dataset = make_dataset(1000, signal=0.0, horizon=24)
        cut = tp.resolve_holdout_from(dataset, None)[0]
        split = tp.split_holdout(dataset, cut, 10)
        assert split.n_boundary_purged >= 24
        assert (split.n_train + split.n_holdout + split.n_boundary_purged
                == len(dataset))

    def test_a_larger_embargo_purges_strictly_more(self):
        dataset = make_dataset(1000, signal=0.0, horizon=24)
        cut = tp.resolve_holdout_from(dataset, None)[0]
        small = tp.split_holdout(dataset, cut, 0)
        large = tp.split_holdout(dataset, cut, 100)
        assert large.n_boundary_purged == small.n_boundary_purged + 100
        assert large.n_train == small.n_train - 100

    def test_a_cut_after_the_data_leaves_an_empty_holdout(self):
        dataset = make_dataset(300, signal=0.0)
        split = tp.split_holdout(dataset, tp.parse_cut_date("2099-01-01"), 5)
        assert split.n_holdout == 0
        assert split.n_train == len(dataset)

    def test_a_negative_embargo_is_refused(self):
        dataset = make_dataset(300, signal=0.0)
        with pytest.raises(tp.TrainingAborted):
            tp.split_holdout(dataset, tp.resolve_holdout_from(dataset, None)[0], -1)

    def test_a_cut_before_the_data_aborts_rather_than_training_on_nothing(self):
        with pytest.raises(tp.TrainingAborted, match="no training rows"):
            tp.train_policy(
                make_dataset(300, signal=0.0), make_corpus_info(),
                holdout_from="2000-01-01", n_folds=3, embargo=5, seed=7,
            )


# ---------------------------------------------------------------------------
# 4. the folds
# ---------------------------------------------------------------------------


class TestFolds:
    def test_no_holdout_row_appears_in_any_training_fold(self, learnable_run):
        held = set(learnable_run.split.holdout_pos.tolist())
        folds = tp.fold_row_positions(
            learnable_run.dataset, learnable_run.split,
            n_folds=learnable_run.n_folds, embargo=learnable_run.embargo,
        )
        assert folds, "the run must have produced folds to assert on"
        for train_pos, _val_pos in folds:
            assert not (set(train_pos.tolist()) & held)

    def test_no_holdout_row_appears_in_any_validation_fold_either(self, learnable_run):
        held = set(learnable_run.split.holdout_pos.tolist())
        for _train_pos, val_pos in tp.fold_row_positions(
            learnable_run.dataset, learnable_run.split,
            n_folds=learnable_run.n_folds, embargo=learnable_run.embargo,
        ):
            assert not (set(val_pos.tolist()) & held)

    def test_every_fold_row_comes_from_the_training_block(self, learnable_run):
        allowed = set(learnable_run.split.train_pos.tolist())
        for train_pos, val_pos in tp.fold_row_positions(
            learnable_run.dataset, learnable_run.split,
            n_folds=learnable_run.n_folds, embargo=learnable_run.embargo,
        ):
            assert set(train_pos.tolist()) <= allowed
            assert set(val_pos.tolist()) <= allowed

    def test_within_a_fold_training_strictly_precedes_validation(self, learnable_run):
        for train_pos, val_pos in tp.fold_row_positions(
            learnable_run.dataset, learnable_run.split,
            n_folds=learnable_run.n_folds, embargo=learnable_run.embargo,
        ):
            assert train_pos.max() < val_pos.min()

    def test_the_number_of_folds_matches_the_request(self, learnable_run):
        assert len(learnable_run.report.folds) == learnable_run.n_folds

    def test_fold_bounds_are_reported_in_chronological_order(self, learnable_run):
        firsts = [first for _f, first, _l in learnable_run.fold_bounds]
        assert firsts == sorted(firsts)


# ---------------------------------------------------------------------------
# 5. labels and feature columns
# ---------------------------------------------------------------------------


class TestLabelsAndColumns:
    def test_only_the_take_profit_outcome_is_a_one(self):
        y = np.array([ft.LABEL_TAKE_PROFIT, ft.LABEL_STOP, ft.LABEL_TIMEOUT])
        assert tp.binarise(y).tolist() == [1.0, 0.0, 0.0]

    def test_the_timeout_is_a_loss_and_the_definition_says_so(self):
        text = tp.label_definition(ft.BarrierSpec())
        assert "timeout" in text
        assert "y=0" in text

    def test_the_label_definition_names_the_barrier_widths(self):
        text = tp.label_definition(ft.BarrierSpec(take_profit_atr=3.0, stop_atr=1.5))
        assert "+3x" in text and "-1.5x" in text

    def test_the_nan_book_columns_are_dropped_by_name(self):
        kept, dropped = tp.usable_feature_columns(make_dataset(200, signal=0.0))
        assert set(dropped) == set(ft.BOOK_FEATURE_NAMES)
        assert set(kept) == set(ft.PRICE_FEATURE_NAMES)

    def test_kept_columns_stay_in_schema_order(self):
        kept, _ = tp.usable_feature_columns(make_dataset(200, signal=0.0))
        assert list(kept) == [n for n in ft.FEATURE_NAMES if n in set(kept)]

    def test_the_model_is_built_on_the_kept_columns_only(self, learnable_run):
        assert learnable_run.policy.feature_names == learnable_run.feature_names
        assert set(ft.BOOK_FEATURE_NAMES).isdisjoint(learnable_run.feature_names)

    def test_a_dataset_with_no_usable_column_aborts(self):
        dataset = make_dataset(300, signal=0.0)
        dataset.X[:, :] = np.nan
        with pytest.raises(tp.TrainingAborted, match="nan"):
            tp.train_policy(dataset, make_corpus_info(), holdout_from=None,
                            n_folds=3, embargo=5, seed=7)

    def test_an_empty_dataset_aborts(self):
        empty = make_dataset(10, signal=0.0)
        empty = ft.Dataset(
            X=empty.X[:0], y=empty.y[:0], timestamps=empty.timestamps[:0],
            bar_index=empty.bar_index[:0],
            bars_to_resolution=empty.bars_to_resolution[:0],
            feature_names=ft.FEATURE_NAMES,
            schema_version=ft.FEATURE_SCHEMA_VERSION,
            schema_digest=ft.FEATURE_SCHEMA_DIGEST,
            barriers=ft.BarrierSpec(), report=_dataset_report(0, 0),
        )
        with pytest.raises(tp.TrainingAborted, match="empty"):
            tp.train_policy(empty, make_corpus_info(), holdout_from=None,
                            n_folds=3, embargo=5, seed=7)


# ---------------------------------------------------------------------------
# 6. promotion
# ---------------------------------------------------------------------------


class TestPromotion:
    def test_a_noise_model_is_refused(self, noise_run):
        """If this ever passes, the tool will promote anything."""
        assert noise_run.promoted is False
        assert noise_run.all_reasons

    def test_a_learnable_signal_is_promoted(self, learnable_run):
        assert learnable_run.promoted is True, learnable_run.all_reasons
        assert learnable_run.all_reasons == ()

    def test_promotion_needs_both_halves(self, learnable_run):
        assert learnable_run.wf_ok and learnable_run.holdout_ok

    def test_a_failing_holdout_alone_refuses(self, learnable_run):
        """Constructed directly: good folds, bad holdout, no promotion."""
        run = tp.TrainingRun(**{**learnable_run.__dict__,
                                "holdout_ok": False,
                                "holdout_reasons": ("HOLDOUT_AUC_TOO_LOW: x",)})
        assert run.promoted is False
        assert "HOLDOUT_AUC_TOO_LOW: x" in run.all_reasons

    def test_a_failing_fold_alone_refuses(self, learnable_run):
        run = tp.TrainingRun(**{**learnable_run.__dict__,
                                "wf_ok": False, "wf_reasons": ("NO_AUC",)})
        assert run.promoted is False

    def test_an_absent_holdout_refuses(self):
        ok, reasons = tp.holdout_verdict(tp.HoldoutMetrics(), pol.PromotionCriteria())
        assert ok is False
        assert reasons[0].startswith("HOLDOUT_ABSENT")

    def test_a_tiny_holdout_refuses(self):
        metrics = tp.HoldoutMetrics(
            n_samples=10, base_rate=0.5, train_base_rate=0.5,
            brier=0.1, baseline_brier=0.25, auc=0.9,
            calibration=pol.CalibrationReport(expected_calibration_error=0.01),
        )
        ok, reasons = tp.holdout_verdict(metrics, pol.PromotionCriteria())
        assert ok is False
        assert any("HOLDOUT_TOO_SMALL" in r for r in reasons)

    def test_a_holdout_that_ranks_badly_refuses(self):
        metrics = tp.HoldoutMetrics(
            n_samples=5000, base_rate=0.5, train_base_rate=0.5,
            brier=0.24, baseline_brier=0.25, auc=0.51,
            calibration=pol.CalibrationReport(expected_calibration_error=0.01),
        )
        ok, reasons = tp.holdout_verdict(metrics, pol.PromotionCriteria())
        assert ok is False
        assert any("HOLDOUT_AUC_TOO_LOW" in r for r in reasons)

    def test_a_holdout_no_better_than_the_base_rate_refuses(self):
        metrics = tp.HoldoutMetrics(
            n_samples=5000, base_rate=0.5, train_base_rate=0.5,
            brier=0.25, baseline_brier=0.25, auc=0.9,
            calibration=pol.CalibrationReport(expected_calibration_error=0.01),
        )
        ok, reasons = tp.holdout_verdict(metrics, pol.PromotionCriteria())
        assert ok is False
        assert any("BRIER_NO_BETTER" in r for r in reasons)

    def test_a_miscalibrated_holdout_refuses_even_with_a_great_auc(self):
        metrics = tp.HoldoutMetrics(
            n_samples=5000, base_rate=0.5, train_base_rate=0.5,
            brier=0.1, baseline_brier=0.25, auc=0.99,
            calibration=pol.CalibrationReport(expected_calibration_error=0.4),
        )
        ok, reasons = tp.holdout_verdict(metrics, pol.PromotionCriteria())
        assert ok is False
        assert any("HOLDOUT_MISCALIBRATED" in r for r in reasons)

    def test_a_degenerate_holdout_label_balance_refuses(self):
        metrics = tp.HoldoutMetrics(
            n_samples=5000, base_rate=0.001, train_base_rate=0.5,
            brier=0.001, baseline_brier=0.25, auc=0.9,
            calibration=pol.CalibrationReport(expected_calibration_error=0.01),
        )
        ok, reasons = tp.holdout_verdict(metrics, pol.PromotionCriteria())
        assert ok is False
        assert any("DEGENERATE_LABEL_BALANCE" in r for r in reasons)

    def test_the_holdout_uses_the_same_thresholds_as_the_folds(self):
        """No second, softer bar invented for the holdout."""
        criteria = pol.PromotionCriteria()
        just_under = tp.HoldoutMetrics(
            n_samples=5000, base_rate=0.5, train_base_rate=0.5,
            brier=0.20, baseline_brier=0.25,
            auc=criteria.min_auc_mean - 0.0001,
            calibration=pol.CalibrationReport(expected_calibration_error=0.01),
        )
        assert tp.holdout_verdict(just_under, criteria)[0] is False

    def test_the_holdout_baseline_is_the_training_base_rate(self, learnable_run):
        """A baseline computed from the holdout would have seen the future."""
        y_train = tp.binarise(
            learnable_run.dataset.y[learnable_run.split.train_pos]
        )
        assert learnable_run.holdout.train_base_rate == pytest.approx(
            float(np.mean(y_train))
        )

    def test_evaluating_an_untrained_policy_aborts_rather_than_scoring_nothing(self):
        policy = pol.Policy(feature_names=("a", "b"), schema_version="x")
        with pytest.raises(tp.TrainingAborted):
            tp.evaluate_holdout(
                policy, np.zeros((5, 2)), np.array([0, 1, 0, 1, 0]),
                train_base_rate=0.5,
            )


# ---------------------------------------------------------------------------
# 7. writing — where a verdict becomes a file
# ---------------------------------------------------------------------------


class TestWriting:
    def test_a_promoted_run_writes_the_versioned_artefact_and_the_live_path(
        self, learnable_run, tmp_path
    ):
        out = str(tmp_path / "models")
        result = tp.write_artefacts(learnable_run, out, dry_run=False)
        assert result.artefact_path == os.path.join(out, learnable_run.run_id)
        assert result.live_path == os.path.join(out, tp.LIVE_DIRNAME)
        for path in (result.artefact_path, result.live_path):
            assert os.path.exists(os.path.join(path, pol.ESTIMATOR_FILENAME))
            assert os.path.exists(os.path.join(path, pol.METADATA_FILENAME))
            assert os.path.exists(os.path.join(path, tp.MANIFEST_FILENAME))

    def test_a_refused_run_writes_nothing_to_the_live_path(self, noise_run, tmp_path):
        """The test that matters. A refused model must not be servable."""
        out = str(tmp_path / "models")
        result = tp.write_artefacts(noise_run, out, dry_run=False)
        assert result.live_path is None
        assert not os.path.exists(os.path.join(out, tp.LIVE_DIRNAME))

    def test_a_refused_run_is_written_under_rejected_for_inspection(
        self, noise_run, tmp_path
    ):
        out = str(tmp_path / "models")
        result = tp.write_artefacts(noise_run, out, dry_run=False)
        assert tp.REJECTED_DIRNAME in result.artefact_path
        assert os.path.exists(os.path.join(result.artefact_path, tp.MANIFEST_FILENAME))
        assert os.path.exists(
            os.path.join(result.artefact_path, pol.ESTIMATOR_FILENAME)
        )

    def test_a_refused_run_does_not_delete_a_previously_promoted_model(
        self, learnable_run, noise_run, tmp_path
    ):
        out = str(tmp_path / "models")
        tp.write_artefacts(learnable_run, out, dry_run=False)
        live = os.path.join(out, tp.LIVE_DIRNAME)
        before = open(os.path.join(live, pol.METADATA_FILENAME), "rb").read()
        result = tp.write_artefacts(noise_run, out, dry_run=False)
        assert open(os.path.join(live, pol.METADATA_FILENAME), "rb").read() == before
        assert result.stale_live_warning is not None
        assert "PREVIOUSLY promoted" in result.stale_live_warning

    def test_dry_run_writes_absolutely_nothing(self, learnable_run, tmp_path):
        out = str(tmp_path / "models")
        result = tp.write_artefacts(learnable_run, out, dry_run=True)
        assert result.dry_run is True
        assert result.artefact_path is None and result.live_path is None
        assert not os.path.exists(out)

    def test_dry_run_quotes_no_fingerprint_it_did_not_compute(
        self, tmp_path, capsys, small_corpus
    ):
        out = str(tmp_path / "models")
        tp.main(["--data-dir", small_corpus, "--out", out, "--dry-run",
                 "--folds", "3", "--embargo", "5"])
        printed = capsys.readouterr().out
        assert "NOTHING WAS WRITTEN" in printed
        assert "fingerprint" not in printed
        assert not os.path.exists(out)

    def test_the_run_id_names_the_directory(self, learnable_run, tmp_path):
        out = str(tmp_path / "models")
        result = tp.write_artefacts(learnable_run, out, dry_run=False)
        assert os.path.basename(result.artefact_path) == learnable_run.run_id


# ---------------------------------------------------------------------------
# 8. the manifest
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def written(learnable_run, tmp_path_factory):
    """A promoted artefact on disk, with its manifest parsed."""
    out = str(tmp_path_factory.mktemp("written"))
    result = tp.write_artefacts(learnable_run, out, dry_run=False)
    manifest = json.load(
        open(os.path.join(result.artefact_path, tp.MANIFEST_FILENAME), encoding="utf-8")
    )
    return result, manifest


class TestManifest:
    def test_it_records_the_corpus_manifest_hash_it_trained_against(
        self, written, learnable_run
    ):
        _result, manifest = written
        assert manifest["corpus"]["manifest_sha256"] == (
            learnable_run.corpus.manifest_sha256
        )
        assert manifest["corpus"]["file_sha256"]

    def test_it_records_the_feature_schema_version_and_digest(self, written):
        _result, manifest = written
        assert manifest["features"]["schema_version"] == ft.FEATURE_SCHEMA_VERSION
        assert manifest["features"]["schema_digest"] == ft.FEATURE_SCHEMA_DIGEST

    def test_it_records_which_columns_were_dropped_and_which_were_used(self, written):
        _result, manifest = written
        assert manifest["features"]["dropped_unmeasurable"] == list(
            ft.BOOK_FEATURE_NAMES
        )
        assert manifest["features"]["used"] == list(ft.PRICE_FEATURE_NAMES)

    def test_it_records_the_date_ranges_of_train_and_holdout(self, written):
        _result, manifest = written
        split = manifest["split"]
        assert split["train_validate"]["first_bar"] < split["train_validate"]["last_bar"]
        assert split["holdout"]["first_bar"] > split["train_validate"]["last_bar"]
        assert split["holdout_from"].endswith("Z")

    def test_it_records_every_fold_validation_range(self, written, learnable_run):
        _result, manifest = written
        ranges = manifest["split"]["fold_validation_ranges"]
        assert len(ranges) == learnable_run.n_folds
        assert all(r["first_bar"] and r["last_bar"] for r in ranges)

    def test_it_records_the_sample_counts(self, written, learnable_run):
        _result, manifest = written
        split = manifest["split"]
        assert split["train_validate"]["n_samples"] == learnable_run.split.n_train
        assert split["holdout"]["n_samples"] == learnable_run.split.n_holdout
        assert split["boundary_purged"]["n_samples"] == (
            learnable_run.split.n_boundary_purged
        )

    def test_it_records_the_label_definition(self, written):
        _result, manifest = written
        assert "triple-barrier" in manifest["label"]["definition"]
        assert manifest["label"]["barriers"]["max_horizon"] == 24
        assert manifest["label"]["binary_base_rate"] is not None

    def test_it_records_every_walk_forward_metric(self, written, learnable_run):
        _result, manifest = written
        walk = manifest["metrics"]["walk_forward"]
        assert len(walk["folds"]) == learnable_run.n_folds
        assert walk["oos"]["auc_mean"] is not None
        assert walk["oos"]["brier_mean"] is not None
        assert walk["calibration"]["buckets"]

    def test_it_records_the_holdout_metrics_separately(self, written):
        _result, manifest = written
        holdout = manifest["metrics"]["holdout"]
        assert holdout["auc"] is not None
        assert holdout["calibration"]["expected_calibration_error"] is not None

    def test_it_records_the_promotion_verdict_and_the_criteria(self, written):
        _result, manifest = written
        promotion = manifest["promotion"]
        assert promotion["promoted"] is True
        assert promotion["criteria"] == pol.PromotionCriteria().to_dict()
        assert promotion["walk_forward_reasons"] == []
        assert promotion["holdout_reasons"] == []

    def test_a_refused_manifest_records_why(self, noise_run, tmp_path):
        out = str(tmp_path / "models")
        result = tp.write_artefacts(noise_run, out, dry_run=False)
        manifest = json.load(open(
            os.path.join(result.artefact_path, tp.MANIFEST_FILENAME), encoding="utf-8"
        ))
        assert manifest["promotion"]["promoted"] is False
        reasons = (manifest["promotion"]["walk_forward_reasons"]
                   + manifest["promotion"]["holdout_reasons"])
        assert reasons, "a refusal with no stated reason is not a refusal"

    def test_it_records_the_seed_and_hyperparameters(self, written, learnable_run):
        _result, manifest = written
        assert manifest["determinism"]["seed"] == learnable_run.seed
        assert manifest["determinism"]["hyperparameters"]["random_state"] == (
            learnable_run.seed
        )

    def test_it_records_the_feature_importance_and_where_it_was_measured(self, written):
        _result, manifest = written
        importance = manifest["feature_importance"]
        assert len(importance["ranked"]) == len(ft.PRICE_FEATURE_NAMES)
        assert "holdout" in importance["measured_on"]
        assert importance["base_auc"] is not None

    def test_it_warns_that_the_holdout_is_single_use(self, written):
        _result, manifest = written
        assert any("single-use" in w or "spent it" in w
                   for w in manifest["warnings"])

    def test_it_states_that_no_profit_figure_was_produced(self, written):
        _result, manifest = written
        assert any("not profit" in w for w in manifest["warnings"])

    def test_the_manifest_is_written_beside_the_live_model_too(self, written):
        result, _manifest = written
        assert os.path.exists(os.path.join(result.live_path, tp.MANIFEST_FILENAME))

    def test_the_manifest_is_plain_json_with_no_numpy_leftovers(self, written):
        _result, manifest = written
        text = json.dumps(manifest)
        assert "numpy" not in text and "ndarray" not in text


# ---------------------------------------------------------------------------
# 9. determinism
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def twin_runs(tmp_path_factory):
    """The same inputs, twice, written to two different directories."""
    results = []
    for name in ("first", "second"):
        run = tp.train_policy(
            make_dataset(2500, seed=4, signal=1.3),
            make_corpus_info(n_bars=2500),
            holdout_from=None, n_folds=3, embargo=10, seed=7,
        )
        out = str(tmp_path_factory.mktemp(name))
        tp.write_artefacts(run, out, dry_run=False)
        results.append(run)
    return results


class TestDeterminism:
    def test_two_identical_runs_produce_the_same_artefact_hash(self, twin_runs):
        first, second = twin_runs
        assert first.artefact_fingerprint == second.artefact_fingerprint
        assert first.artefact_fingerprint is not None

    def test_two_identical_runs_produce_byte_identical_estimators(self, twin_runs):
        first, second = twin_runs
        assert first.estimator_sha256 == second.estimator_sha256

    def test_two_identical_runs_land_in_the_same_versioned_directory(self, twin_runs):
        first, second = twin_runs
        assert first.run_id == second.run_id

    def test_two_identical_runs_report_the_same_metrics(self, twin_runs):
        first, second = twin_runs
        assert first.report.auc_mean == second.report.auc_mean
        assert first.holdout.brier == second.holdout.brier

    def test_the_fingerprint_excludes_the_wall_clock(self, twin_runs):
        """``policy``'s own content hash cannot be the reproducible one."""
        first, second = twin_runs
        assert first.policy.artefact_hash is not None
        assert "trained_at" not in tp._canonical(
            {"folds": [f.to_dict() for f in first.report.folds]}
        )
        assert first.artefact_fingerprint == second.artefact_fingerprint

    def test_a_different_seed_is_a_different_run(self):
        corpus = make_corpus_info()
        common = dict(seed=7, n_folds=4, embargo=30, holdout_from_ms=0,
                      hyperparameters={"random_state": 7})
        base = tp.compute_run_id(corpus, **common)
        assert base != tp.compute_run_id(corpus, **{**common, "seed": 8})

    def test_a_different_corpus_is_a_different_run(self):
        common = dict(seed=7, n_folds=4, embargo=30, holdout_from_ms=0,
                      hyperparameters={})
        one = tp.compute_run_id(make_corpus_info(), **common)
        other = tp.compute_run_id(make_corpus_info(manifest_sha256="c" * 64), **common)
        assert one != other

    def test_a_different_split_or_fold_count_is_a_different_run(self):
        corpus = make_corpus_info()
        common = dict(seed=7, n_folds=4, embargo=30, holdout_from_ms=0,
                      hyperparameters={})
        base = tp.compute_run_id(corpus, **common)
        assert base != tp.compute_run_id(corpus, **{**common, "n_folds": 5})
        assert base != tp.compute_run_id(corpus, **{**common, "embargo": 31})
        assert base != tp.compute_run_id(corpus, **{**common, "holdout_from_ms": 1})

    def test_the_run_id_is_short_enough_to_be_a_directory_name(self, learnable_run):
        assert re.fullmatch(r"[0-9a-f]{12}", learnable_run.run_id)


# ---------------------------------------------------------------------------
# 10. loading — the schema gate
# ---------------------------------------------------------------------------


class TestArtefactLoading:
    def test_a_matching_artefact_loads(self, written):
        result, _manifest = written
        policy = tp.load_artefact(result.artefact_path)
        assert policy.is_trained
        assert policy.feature_names == ft.PRICE_FEATURE_NAMES
        assert policy.schema_version == ft.FEATURE_SCHEMA_VERSION

    def test_a_loaded_artefact_still_knows_it_was_promoted(self, written):
        result, _manifest = written
        assert tp.load_artefact(result.artefact_path).promotion[0] is True

    def _copy_with_manifest(self, source: str, target: str, mutate) -> str:
        os.makedirs(target, exist_ok=True)
        for name in (pol.ESTIMATOR_FILENAME, pol.METADATA_FILENAME):
            with open(os.path.join(source, name), "rb") as src:
                with open(os.path.join(target, name), "wb") as dst:
                    dst.write(src.read())
        manifest = json.load(
            open(os.path.join(source, tp.MANIFEST_FILENAME), encoding="utf-8")
        )
        mutate(manifest)
        json.dump(manifest,
                  open(os.path.join(target, tp.MANIFEST_FILENAME), "w",
                       encoding="utf-8"))
        return target

    def test_a_stale_schema_version_is_refused_at_load(self, written, tmp_path):
        result, _manifest = written

        def bump(manifest):
            manifest["features"]["schema_version"] = "99.0.0"

        path = self._copy_with_manifest(
            result.artefact_path, str(tmp_path / "stale"), bump
        )
        with pytest.raises(ft.SchemaMismatch, match="version mismatch"):
            tp.load_artefact(path)

    def test_a_changed_digest_is_refused_even_at_the_same_version(
        self, written, tmp_path
    ):
        """The reordered-schema case nobody thought worth a version bump."""
        result, _manifest = written

        def scramble(manifest):
            manifest["features"]["schema_digest"] = "0" * 16

        path = self._copy_with_manifest(
            result.artefact_path, str(tmp_path / "digest"), scramble
        )
        with pytest.raises(ft.SchemaMismatch, match="digest mismatch"):
            tp.load_artefact(path)

    def test_an_artefact_with_no_training_manifest_is_refused(self, written, tmp_path):
        result, _manifest = written
        target = str(tmp_path / "nomanifest")
        os.makedirs(target)
        for name in (pol.ESTIMATOR_FILENAME, pol.METADATA_FILENAME):
            with open(os.path.join(result.artefact_path, name), "rb") as src:
                open(os.path.join(target, name), "wb").write(src.read())
        with pytest.raises(pol.ArtefactError, match="MANIFEST"):
            tp.load_artefact(target)

    def test_a_manifest_that_lists_no_features_is_refused(self, written, tmp_path):
        result, _manifest = written

        def strip(manifest):
            manifest["features"]["used"] = []

        path = self._copy_with_manifest(
            result.artefact_path, str(tmp_path / "nofeat"), strip
        )
        with pytest.raises(pol.ArtefactError):
            tp.load_artefact(path)

    def test_a_reordered_feature_list_is_refused(self, written, tmp_path):
        result, _manifest = written

        def swap(manifest):
            used = list(manifest["features"]["used"])
            used[0], used[1] = used[1], used[0]
            manifest["features"]["used"] = used

        path = self._copy_with_manifest(
            result.artefact_path, str(tmp_path / "reordered"), swap
        )
        with pytest.raises(pol.SchemaMismatch, match="ORDER"):
            tp.load_artefact(path)

    def test_a_corrupt_estimator_is_refused(self, written, tmp_path):
        result, _manifest = written
        target = self._copy_with_manifest(
            result.artefact_path, str(tmp_path / "corrupt"), lambda m: None
        )
        with open(os.path.join(target, pol.ESTIMATOR_FILENAME), "ab") as handle:
            handle.write(b"tampered")
        with pytest.raises(pol.ArtefactError, match="hash mismatch"):
            tp.load_artefact(target)

    def test_reading_a_manifest_from_nowhere_raises(self, tmp_path):
        with pytest.raises(pol.ArtefactError):
            tp.read_training_manifest(str(tmp_path / "does-not-exist"))


# ---------------------------------------------------------------------------
# 11. the report
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def promoted_report(learnable_run, tmp_path_factory) -> str:
    out = str(tmp_path_factory.mktemp("report"))
    write = tp.write_artefacts(learnable_run, out, dry_run=False)
    return tp.render_report(learnable_run, write)


@pytest.fixture(scope="module")
def refused_report(noise_run, tmp_path_factory) -> str:
    out = str(tmp_path_factory.mktemp("refused"))
    write = tp.write_artefacts(noise_run, out, dry_run=False)
    return tp.render_report(noise_run, write)


class TestReport:
    def test_provenance_comes_first(self, promoted_report):
        assert promoted_report.splitlines()[1] == "DATA PROVENANCE"

    def test_the_verdict_comes_last(self, promoted_report):
        assert (promoted_report.index("PROMOTION VERDICT")
                > promoted_report.index("HOLDOUT —"))
        assert (promoted_report.index("PROMOTION VERDICT")
                > promoted_report.index("WALK-FORWARD"))

    def test_every_section_is_present(self, promoted_report):
        for heading in ("DATA PROVENANCE", "FEATURES AND LABELS", "SPLIT",
                        "WALK-FORWARD", "CALIBRATION", "FEATURE IMPORTANCE",
                        "HOLDOUT", "PROMOTION VERDICT"):
            assert heading in promoted_report

    def test_the_calibration_table_is_printed(self, promoted_report):
        assert "predicted    realised        gap" in promoted_report
        assert "reliability  n=" in promoted_report

    def test_the_feature_importance_table_lists_every_used_column(
        self, promoted_report
    ):
        for name in ft.PRICE_FEATURE_NAMES:
            assert name in promoted_report

    def test_the_holdout_burn_warning_is_not_optional(self, promoted_report):
        assert "SINGLE USE" in promoted_report
        assert "burned it" in promoted_report

    def test_a_synthetic_corpus_is_called_out_as_not_a_market(self, promoted_report):
        assert "NOT A MARKET" in promoted_report

    def test_a_refusal_says_refused_and_lists_every_reason(
        self, refused_report, noise_run
    ):
        assert "REFUSED" in refused_report
        for reason in noise_run.all_reasons:
            assert reason in refused_report

    def test_a_refusal_says_the_live_path_was_not_written(self, refused_report):
        assert "LIVE MODEL PATH   : NOT WRITTEN" in refused_report

    def test_the_thresholds_are_printed_with_the_verdict(self, promoted_report):
        for key in pol.PromotionCriteria().to_dict():
            assert key in promoted_report

    def test_folds_at_or_below_a_coin_flip_are_named(self, refused_report):
        assert "FOLDS AT OR BELOW A COIN FLIP" in refused_report
        assert "FOLDS WORSE THAN THE BASE RATE" in refused_report

    def test_no_profit_is_ever_projected(self, promoted_report, refused_report):
        for report in (promoted_report, refused_report):
            lowered = report.lower()
            for word in ("sharpe", "expectancy", "pnl", "equity curve",
                         "annualised", "annualized"):
                assert word not in lowered
            assert "produced no profit estimate" in lowered

    def test_the_report_says_which_block_importance_was_measured_on(
        self, promoted_report
    ):
        assert "measured on" in promoted_report
        assert "out of sample" in promoted_report

    def test_the_dropped_columns_are_named_in_the_report(self, promoted_report):
        assert "DROPPED (nan)" in promoted_report
        for name in ft.BOOK_FEATURE_NAMES:
            assert name in promoted_report


# ---------------------------------------------------------------------------
# 12. feature importance
# ---------------------------------------------------------------------------


class TestFeatureImportance:
    def test_it_ranks_the_true_signal_above_the_noise(self, learnable_run):
        top = [imp.name for imp in learnable_run.importance[:4]]
        assert "ret_24" in top, learnable_run.importance[:6]

    def test_it_covers_every_used_column_exactly_once(self, learnable_run):
        names = [imp.name for imp in learnable_run.importance]
        assert sorted(names) == sorted(learnable_run.feature_names)

    def test_it_is_sorted_by_importance(self, learnable_run):
        drops = [imp.mean_auc_drop for imp in learnable_run.importance]
        assert drops == sorted(drops, reverse=True)

    def test_it_reports_the_spread_across_repeats(self, learnable_run):
        assert all(imp.repeats == tp.IMPORTANCE_REPEATS
                   for imp in learnable_run.importance)
        assert any(imp.std_auc_drop is not None for imp in learnable_run.importance)

    def test_it_returns_nothing_rather_than_zero_on_a_single_class_block(self):
        policy = pol.Policy(feature_names=("a", "b"), schema_version="x")
        base, ranked = tp.permutation_importance(
            policy, np.zeros((10, 2)), np.zeros(10), ("a", "b"), seed=1,
        )
        assert base is None
        assert all(imp.mean_auc_drop is None for imp in ranked)


# ---------------------------------------------------------------------------
# 13. end to end through main()
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_a_noise_corpus_runs_refuses_and_exits_nonzero(
        self, small_corpus, tmp_path, capsys
    ):
        """A random-walk corpus has no edge, and the tool must say so."""
        out = str(tmp_path / "models")
        code = tp.main(["--data-dir", small_corpus, "--out", out,
                        "--folds", "3", "--embargo", "5"])
        printed = capsys.readouterr().out
        assert code == 1, "a refused model must not exit 0"
        assert "REFUSED" in printed
        assert not os.path.exists(os.path.join(out, tp.LIVE_DIRNAME))
        assert os.path.isdir(os.path.join(out, tp.REJECTED_DIRNAME))

    def test_the_gitignore_advice_is_printed(self, small_corpus, tmp_path, capsys):
        tp.main(["--data-dir", small_corpus, "--out", str(tmp_path / "m"),
                 "--folds", "3", "--embargo", "5", "--dry-run"])
        printed = capsys.readouterr().out
        for line in tp.GITIGNORE_LINES:
            assert line in printed

    def test_a_missing_corpus_aborts_with_a_named_reason(self, tmp_path, capsys):
        code = tp.main(["--data-dir", str(tmp_path / "nothing"),
                        "--out", str(tmp_path / "m")])
        assert code == 2
        assert "ABORTED" in capsys.readouterr().out

    def test_an_impossible_cut_date_aborts_without_writing(self, small_corpus, tmp_path):
        out = str(tmp_path / "models")
        assert tp.main(["--data-dir", small_corpus, "--out", out,
                        "--holdout-from", "not-a-date"]) == 2
        assert not os.path.exists(out)


# ---------------------------------------------------------------------------
# 14. structural honesty guards
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tree() -> ast.Module:
    """The tool parsed as a syntax tree, for the structural guards below."""
    return ast.parse(open(TOOL_PATH, encoding="utf-8").read())


class TestSourceGuards:
    """Properties of the file itself, asserted so they cannot rot silently."""

    def test_there_is_no_bare_except(self, tree):
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                assert node.type is not None, "a bare except hides the reason"

    def test_no_handler_silently_passes(self, tree):
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                assert not all(isinstance(s, ast.Pass) for s in node.body), (
                    "an exception swallowed without a word is a failure nobody "
                    "will ever see"
                )

    def test_the_tool_does_not_import_config_or_the_live_stack(self, tree):
        forbidden = {"config", "trading_engine", "bybit_connection",
                     "risk_management", "persistence", "main"}
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not (imported & forbidden), (
            f"a training tool that can reach {imported & forbidden} can reach "
            "live state"
        )

    def test_every_public_function_has_a_docstring(self, tree):
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                if node.name.startswith("_"):
                    continue
                assert ast.get_docstring(node), f"{node.name} has no docstring"

    def test_json_ready_refuses_types_it_does_not_understand(self):
        with pytest.raises(TypeError):
            tp._json_ready(object())

    def test_json_ready_converts_numpy_scalars(self):
        assert tp._json_ready(np.int64(3)) == 3
        assert tp._json_ready(np.float64(1.5)) == 1.5
        assert tp._json_ready(np.bool_(True)) is True

    def test_a_non_finite_number_becomes_null_not_a_plausible_zero(self):
        assert tp._json_ready(np.float64("nan")) is None
        assert tp._json_ready(np.float64("inf")) is None

    def test_missing_measurements_print_as_a_dash_not_as_zero(self):
        assert tp._fmt(None).strip() == "-"
        assert tp._fmt(0.5) == "0.5000"

    def test_format_ms_of_nothing_is_nothing(self):
        assert tp.format_ms(None) is None
        assert tp.format_ms(1_704_067_200_000) == "2024-01-01T00:00:00Z"

    def test_the_git_commit_is_none_rather_than_a_guess(self, monkeypatch):
        def fail(*args, **kwargs):
            raise OSError("no git here")

        monkeypatch.setattr(tp.subprocess, "run", fail)
        assert tp.git_commit() is None
