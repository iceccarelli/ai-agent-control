"""Feature-pipeline tests.

The shape follows `tests/test_indicators.py` and `tests/test_market_data.py`:
assert the property that the bug would actually violate, on constructed data
where the right answer is obvious by inspection, and on the real corpus where
it is not.

Five claims carry the module, and each has a class named after it:

1. **The future is unreachable.** `TestNoLookahead` computes the vector at bar
   *i* on the full series and on the series truncated at *i* and requires them
   to be identical, for many random *i*, on real bars. This is the most
   important test in the file: everything else is a quality problem, this one
   is the difference between a backtest and a fiction.

2. **Offline is online.** `TestBatchEqualsSingleBar` requires row *i* of a
   built dataset to equal `compute_features(bars, i).to_array()` element for
   element. If that ever fails there are two feature pipelines again.

3. **Scale-free.** `TestScaleInvariance` multiplies every price by ten and
   requires an unchanged vector. A feature that fails it is a feature that
   encodes "BTC costs $8,000", which was true for four months of this corpus
   and has not been true since.

4. **Nothing is imputed.** Missing is `None`, is reported by name, and is never
   `0.0` — checked for the absent order book, for a cold start, and for the
   degenerate bars (zero range, zero volume) that produce 0/0.

5. **Labels are pessimistic and honest.** Hand-built price paths where the
   answer is obvious, including a bar that touches both barriers, where the
   label must be the stop — the same conservative rule
   `backtest.Backtester._resolve_bar` applies to a simulated fill.

Everything here is offline and fast. The real-corpus tests read
`data/real/ohlcv/*.csv.gz` through `market_data` and touch no network.
"""
from __future__ import annotations

import ast
import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import features as ft  # noqa: E402
import market_data as md  # noqa: E402
import pure_indicators as pi  # noqa: E402
from features import (  # noqa: E402
    BarrierSpec, Dataset, FeatureVector, SchemaMismatch, build_dataset,
    compute_features, triple_barrier_label, triple_barrier_labels,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_DIR = os.path.join(REPO, "data", "real")
REAL_OHLCV = os.path.join(REAL_DIR, "ohlcv", "BITSTAMP_SPOT_BTC_USD_1H.csv.gz")

HOUR_MS = 3_600_000
#: 2018-01-01T00:00:00Z, a Monday — the first bar of the real corpus.
T0 = 1_514_764_800_000

needs_real_corpus = pytest.mark.skipif(
    not os.path.exists(REAL_OHLCV),
    reason="real corpus not present; see data/real/README.md",
)


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def synthetic_bars(n=400, seed=7, vol=0.01, start_price=100.0, start_ms=T0):
    """A well-behaved OHLCV series: positive prices, non-degenerate ranges.

    Deliberately not a fixture: several tests need two series that differ in
    exactly one way, and a fixture would hide the difference.
    """
    rng = np.random.default_rng(seed)
    closes = start_price * np.cumprod(1.0 + rng.normal(0.0002, vol, n))
    bars = []
    for i, close in enumerate(closes):
        open_ = float(close * (1.0 + rng.normal(0.0, vol / 5.0)))
        high = float(max(open_, close) * (1.0 + abs(rng.normal(0.0, vol / 3.0))))
        low = float(min(open_, close) * (1.0 - abs(rng.normal(0.0, vol / 3.0))))
        bars.append(md.Bar(
            start_ms=start_ms + i * HOUR_MS,
            open=open_, high=high, low=low, close=float(close),
            volume=float(abs(rng.normal(100.0, 25.0)) + 1.0),
            start_us=(start_ms + i * HOUR_MS) * 1000,
            end_us=(start_ms + (i + 1) * HOUR_MS) * 1000,
        ))
    return bars


def flat_bars(n, price=100.0, start_ms=T0):
    """Zero-range, zero-volume bars — every ratio in the module is 0/0 here."""
    return [
        md.Bar(start_ms=start_ms + i * HOUR_MS, open=price, high=price,
               low=price, close=price, volume=0.0)
        for i in range(n)
    ]


def scaled(bars, factor):
    """The same series at a different price level. Volume is untouched: a ten
    times higher price is not ten times the coins traded."""
    return [
        md.Bar(start_ms=b.start_ms, open=b.open * factor, high=b.high * factor,
               low=b.low * factor, close=b.close * factor, volume=b.volume,
               start_us=b.start_us, end_us=b.end_us)
        for b in bars
    ]


def constant_range_bars(n, price=100.0, half_range=1.0, start_ms=T0):
    """Bars whose true range is exactly ``2 * half_range`` every bar, so
    ATR(14) is exactly ``2 * half_range`` and every barrier price in the
    label tests can be written down by hand."""
    return [
        md.Bar(start_ms=start_ms + i * HOUR_MS, open=price,
               high=price + half_range, low=price - half_range,
               close=price, volume=10.0)
        for i in range(n)
    ]


def bar_at(price, high=None, low=None, index=0, start_ms=T0, open_=None):
    return md.Bar(
        start_ms=start_ms + index * HOUR_MS,
        open=price if open_ is None else open_,
        high=price if high is None else high,
        low=price if low is None else low,
        close=price, volume=10.0,
    )


SAMPLE_BOOK = (
    [[99.0, 3.0], [98.5, 2.0], [98.0, 1.0]],
    [[101.0, 1.0], [101.5, 2.0], [102.0, 1.0]],
)


def scaled_book(book, factor):
    bids, asks = book
    return ([[p * factor, s] for p, s in bids], [[p * factor, s] for p, s in asks])


@pytest.fixture(scope="session")
def real_bars():
    load = md.load_ohlcv(REAL_OHLCV)
    assert load.bars, "the real corpus produced no bars"
    return load.bars


# ---------------------------------------------------------------------------
# 1. the schema is data
# ---------------------------------------------------------------------------


class TestSchema:
    def test_schema_is_a_non_empty_ordered_tuple(self):
        assert isinstance(ft.FEATURE_SCHEMA, tuple)
        assert len(ft.FEATURE_SCHEMA) > 0

    def test_names_are_unique(self):
        names = [s.name for s in ft.FEATURE_SCHEMA]
        assert len(names) == len(set(names))

    def test_feature_names_follow_schema_order(self):
        assert ft.FEATURE_NAMES == tuple(s.name for s in ft.FEATURE_SCHEMA)

    def test_feature_index_agrees_with_the_order(self):
        for i, name in enumerate(ft.FEATURE_NAMES):
            assert ft.FEATURE_INDEX[name] == i

    def test_every_feature_has_a_description(self):
        for spec in ft.FEATURE_SCHEMA:
            assert spec.description.strip(), f"{spec.name} has no description"

    def test_every_feature_declares_a_positive_minimum(self):
        for spec in ft.FEATURE_SCHEMA:
            assert spec.min_bars >= 1

    def test_max_lookback_is_the_largest_declared_minimum(self):
        assert ft.MAX_LOOKBACK == max(s.min_bars for s in ft.FEATURE_SCHEMA)
        assert ft.WARMUP_BARS == ft.MAX_LOOKBACK

    def test_price_and_book_names_partition_the_schema(self):
        assert set(ft.PRICE_FEATURE_NAMES) | set(ft.BOOK_FEATURE_NAMES) == set(
            ft.FEATURE_NAMES
        )
        assert not set(ft.PRICE_FEATURE_NAMES) & set(ft.BOOK_FEATURE_NAMES)

    def test_book_features_are_the_ones_flagged_needs_book(self):
        assert ft.BOOK_FEATURE_NAMES == tuple(
            s.name for s in ft.FEATURE_SCHEMA if s.needs_book
        )
        assert len(ft.BOOK_FEATURE_NAMES) >= 3

    def test_the_schema_cannot_be_mutated_through_the_lookup_maps(self):
        """A caller that could edit SPEC_BY_NAME could make training and
        serving disagree at runtime, with no version change to detect it."""
        with pytest.raises(TypeError):
            ft.SPEC_BY_NAME["rsi_norm"] = None
        with pytest.raises(TypeError):
            ft.FEATURE_INDEX["rsi_norm"] = 0

    def test_version_is_a_string(self):
        assert isinstance(ft.FEATURE_SCHEMA_VERSION, str)
        assert ft.FEATURE_SCHEMA_VERSION.strip()

    def test_digest_is_stable_across_calls(self):
        assert ft.schema_digest() == ft.schema_digest() == ft.FEATURE_SCHEMA_DIGEST

    def test_digest_changes_when_a_feature_is_renamed(self):
        mutated = list(ft.FEATURE_SCHEMA)
        first = mutated[0]
        mutated[0] = ft.FeatureSpec(
            first.name + "_x", first.description, first.min_bars, first.needs_book
        )
        assert ft.schema_digest(tuple(mutated)) != ft.FEATURE_SCHEMA_DIGEST

    def test_digest_changes_when_the_order_changes(self):
        reordered = tuple(reversed(ft.FEATURE_SCHEMA))
        assert ft.schema_digest(reordered) != ft.FEATURE_SCHEMA_DIGEST

    def test_digest_changes_when_a_feature_is_dropped(self):
        assert ft.schema_digest(ft.FEATURE_SCHEMA[:-1]) != ft.FEATURE_SCHEMA_DIGEST

    def test_digest_changes_when_a_warmup_changes(self):
        mutated = list(ft.FEATURE_SCHEMA)
        first = mutated[0]
        mutated[0] = ft.FeatureSpec(
            first.name, first.description, first.min_bars + 1, first.needs_book
        )
        assert ft.schema_digest(tuple(mutated)) != ft.FEATURE_SCHEMA_DIGEST

    def test_digest_ignores_a_reworded_description(self):
        """Rewording prose must not invalidate a trained model — it does not
        change a single number."""
        mutated = list(ft.FEATURE_SCHEMA)
        first = mutated[0]
        mutated[0] = ft.FeatureSpec(
            first.name, "completely different words", first.min_bars,
            first.needs_book,
        )
        assert ft.schema_digest(tuple(mutated)) == ft.FEATURE_SCHEMA_DIGEST

    def test_digest_changes_when_the_version_changes(self):
        assert ft.schema_digest(version="9.9.9") != ft.FEATURE_SCHEMA_DIGEST

    def test_schema_rejects_a_duplicate_name(self):
        spec = ft.FEATURE_SCHEMA[0]
        with pytest.raises(ValueError):
            ft._validate_schema([spec, spec])

    def test_schema_rejects_an_empty_name(self):
        with pytest.raises(ValueError):
            ft._validate_schema([ft.FeatureSpec("  ", "d", 1)])

    def test_schema_rejects_a_missing_description(self):
        with pytest.raises(ValueError):
            ft._validate_schema([ft.FeatureSpec("x", "", 1)])

    def test_schema_rejects_a_zero_minimum(self):
        with pytest.raises(ValueError):
            ft._validate_schema([ft.FeatureSpec("x", "d", 0)])

    def test_schema_rejects_being_empty(self):
        with pytest.raises(ValueError):
            ft._validate_schema([])


class TestSchemaVersionGate:
    def test_matching_version_and_digest_pass(self):
        ft.require_schema_version(
            ft.FEATURE_SCHEMA_VERSION, ft.FEATURE_SCHEMA_DIGEST
        )

    def test_version_may_be_checked_alone(self):
        ft.require_schema_version(ft.FEATURE_SCHEMA_VERSION)

    def test_a_stale_version_is_refused(self):
        with pytest.raises(SchemaMismatch):
            ft.require_schema_version("0.0.1")

    def test_a_stale_digest_is_refused_even_when_the_version_matches(self):
        """The version is a promise a human makes; the digest is derived."""
        with pytest.raises(SchemaMismatch):
            ft.require_schema_version(ft.FEATURE_SCHEMA_VERSION, "deadbeefdeadbeef")

    def test_the_vector_carries_the_version_it_was_built_with(self):
        vector = compute_features(synthetic_bars(300))
        assert vector.schema_version == ft.FEATURE_SCHEMA_VERSION
        assert vector.schema_digest == ft.FEATURE_SCHEMA_DIGEST


class TestSchemaMatchesWhatIsProduced:
    def test_every_declared_feature_is_produced(self):
        produced = set(compute_features(synthetic_bars(400), None, SAMPLE_BOOK).as_dict())
        assert set(ft.FEATURE_NAMES) - produced == set()

    def test_no_feature_is_produced_that_is_not_declared(self):
        produced = set(compute_features(synthetic_bars(400), None, SAMPLE_BOOK).as_dict())
        assert produced - set(ft.FEATURE_NAMES) == set()

    def test_the_vector_is_as_long_as_the_schema(self):
        vector = compute_features(synthetic_bars(400))
        assert len(vector.values) == len(ft.FEATURE_SCHEMA)
        assert len(vector.to_array()) == len(ft.FEATURE_SCHEMA)

    def test_names_property_is_the_schema_order(self):
        assert compute_features(synthetic_bars(300)).names == ft.FEATURE_NAMES

    def test_get_rejects_an_undeclared_name(self):
        """A typo that returned None would be a feature silently dropped."""
        vector = compute_features(synthetic_bars(300))
        with pytest.raises(KeyError):
            vector.get("rsi_normal")


# ---------------------------------------------------------------------------
# 2. scale invariance
# ---------------------------------------------------------------------------


class TestScaleInvariance:
    @staticmethod
    def _compare(bars, factor, index=None, book=None):
        base = compute_features(bars, index, book)
        other = compute_features(
            scaled(bars, factor), index,
            None if book is None else scaled_book(book, factor),
        )
        assert base.missing == other.missing
        for name, a, b in zip(ft.FEATURE_NAMES, base.values, other.values):
            if a is None:
                continue
            assert b == pytest.approx(a, rel=1e-9, abs=1e-12), (
                f"{name} is not scale-free: {a} at 1x, {b} at {factor}x"
            )

    def test_ten_times_the_price_gives_an_identical_vector(self):
        self._compare(synthetic_bars(400, seed=1), 10.0)

    def test_a_hundred_times_the_price_gives_an_identical_vector(self):
        self._compare(synthetic_bars(400, seed=2), 100.0)

    def test_a_tenth_of_the_price_gives_an_identical_vector(self):
        self._compare(synthetic_bars(400, seed=3), 0.1)

    def test_scale_invariance_holds_with_a_book(self):
        self._compare(synthetic_bars(400, seed=4), 10.0, book=SAMPLE_BOOK)

    @pytest.mark.parametrize("seed", [11, 12, 13, 14, 15])
    def test_scale_invariance_across_seeds(self, seed):
        self._compare(synthetic_bars(400, seed=seed), 10.0)

    @pytest.mark.parametrize("index", [300, 350, 399])
    def test_scale_invariance_at_several_bars(self, index):
        self._compare(synthetic_bars(400, seed=5), 10.0, index=index)

    def test_the_test_is_not_vacuous(self):
        """Scaling must actually change the input, or the assertion above
        proves nothing."""
        bars = synthetic_bars(50, seed=6)
        assert scaled(bars, 10.0)[-1].close != bars[-1].close

    def test_no_feature_tracks_the_price_level(self):
        """Directly the failure mode: a model trained at $8k serving at $100k.

        Two series with the same shape at wildly different levels must produce
        the same vector; if any feature were a price, or a difference of
        prices, this fails.
        """
        bars = synthetic_bars(400, seed=8, start_price=8_000.0)
        self._compare(bars, 12.5)

    @needs_real_corpus
    def test_scale_invariance_on_real_bars(self, real_bars):
        window = list(real_bars[2_000:2_400])
        self._compare(window, 10.0)

    @needs_real_corpus
    def test_real_corpus_spans_the_range_this_matters_over(self, real_bars):
        """The claim the scale-invariance tests are defending is not
        hypothetical on this corpus."""
        closes = np.array([b.close for b in real_bars])
        assert closes.min() < 5_000.0
        assert closes.max() > 60_000.0


# ---------------------------------------------------------------------------
# 3. no lookahead — the most important class in this file
# ---------------------------------------------------------------------------


class TestNoLookahead:
    @staticmethod
    def _assert_truncation_equal(bars, index):
        full = compute_features(bars, index)
        cut = compute_features(bars[:index + 1], index)
        assert full.values == cut.values, f"bar {index} sees the future"
        assert full.missing == cut.missing
        assert full.timestamp_ms == cut.timestamp_ms

    @pytest.mark.parametrize("index", [264, 300, 333, 399])
    def test_truncating_at_the_bar_changes_nothing_synthetic(self, index):
        self._assert_truncation_equal(synthetic_bars(400, seed=21), index)

    @needs_real_corpus
    def test_truncating_at_the_bar_changes_nothing_on_real_bars(self, real_bars):
        """Computed at bar *i* on 61,513 bars, and on the first *i* of them.

        If anything in the pipeline read one bar past `index` — a `[-1]` that
        should have been `[index]`, a rolling window anchored at the end of the
        array — these two numbers diverge.
        """
        rng = np.random.default_rng(4242)
        bars = list(real_bars)
        for index in rng.integers(ft.MAX_LOOKBACK, 20_000, size=40):
            self._assert_truncation_equal(bars, int(index))

    def test_appending_future_bars_changes_nothing(self):
        bars = synthetic_bars(400, seed=22)
        before = compute_features(bars, 320)
        extended = bars + synthetic_bars(80, seed=99, start_price=1.0)
        assert compute_features(extended, 320).values == before.values

    def test_replacing_every_later_bar_changes_nothing(self):
        """Not "the later bars happen to be ignored" — they are absent from the
        slice the arithmetic runs on."""
        bars = synthetic_bars(400, seed=23)
        before = compute_features(bars, 300)
        poisoned = list(bars)
        for i in range(301, len(poisoned)):
            poisoned[i] = md.Bar(
                start_ms=poisoned[i].start_ms, open=1e6, high=2e6, low=5e5,
                close=1.5e6, volume=1e9,
            )
        assert compute_features(poisoned, 300).values == before.values

    def test_deleting_every_later_bar_changes_nothing(self):
        bars = synthetic_bars(400, seed=24)
        assert (compute_features(bars, 290).values
                == compute_features(bars[:291], 290).values)

    def test_the_default_index_is_the_last_bar(self):
        bars = synthetic_bars(400, seed=25)
        assert compute_features(bars).values == compute_features(bars, 399).values
        assert compute_features(bars).index == 399

    def test_a_negative_index_follows_python_convention(self):
        bars = synthetic_bars(400, seed=26)
        assert compute_features(bars, -1).values == compute_features(bars, 399).values
        assert compute_features(bars, -1).index == 399

    def test_an_index_past_the_end_raises(self):
        with pytest.raises(IndexError):
            compute_features(synthetic_bars(50), 50)

    def test_an_index_before_the_start_raises(self):
        with pytest.raises(IndexError):
            compute_features(synthetic_bars(50), -51)

    def test_an_empty_series_raises(self):
        with pytest.raises(ValueError):
            compute_features([])

    def test_the_window_never_exceeds_max_lookback(self):
        """The bound is what makes the value independent of how much history a
        live process happens to be holding."""
        vector = compute_features(synthetic_bars(2_000, seed=27), 1_999)
        assert vector.window_bars == ft.MAX_LOOKBACK

    def test_labels_do_not_read_past_the_horizon(self):
        bars = constant_range_bars(60)
        spec = BarrierSpec(max_horizon=10)
        before = triple_barrier_label(bars, 20, spec)
        poisoned = list(bars)
        for i in range(31, len(poisoned)):
            poisoned[i] = bar_at(1e6, high=2e6, low=5e5, index=i)
        assert triple_barrier_label(poisoned, 20, spec) == before

    def test_labels_do_not_read_the_feature_bar_index_from_the_future(self):
        """The barrier width comes from ATR at or before the feature bar."""
        bars = constant_range_bars(60)
        poisoned = list(bars)
        for i in range(31, len(poisoned)):
            poisoned[i] = bar_at(100.0, high=500.0, low=1.0, index=i)
        assert (triple_barrier_label(poisoned, 20, BarrierSpec()).atr_at_entry
                == triple_barrier_label(bars, 20, BarrierSpec()).atr_at_entry)


class TestHistoryIndependence:
    """Offline has years of history; a live process has a buffer. The value at
    a bar must not depend on which one it is."""

    def test_a_longer_prefix_does_not_change_the_vector(self):
        bars = synthetic_bars(1_000, seed=31)
        index = 900
        short = bars[index - ft.MAX_LOOKBACK + 1:index + 1]
        assert (compute_features(bars, index).values
                == compute_features(short, len(short) - 1).values)

    def test_exactly_the_warmup_is_enough(self):
        bars = synthetic_bars(1_000, seed=32)
        index = 800
        short = bars[index - ft.WARMUP_BARS + 1:index + 1]
        assert compute_features(short).warm is True
        assert compute_features(short).values == compute_features(bars, index).values

    def test_one_bar_short_of_the_warmup_is_flagged_cold(self):
        bars = synthetic_bars(ft.WARMUP_BARS - 1, seed=33)
        vector = compute_features(bars)
        assert vector.warm is False
        assert vector.window_bars == ft.WARMUP_BARS - 1

    def test_a_cold_vector_is_not_silently_equal_to_a_warm_one(self):
        """Short history is a different number, not merely less information —
        which is why `warm` exists instead of a comment."""
        bars = synthetic_bars(1_000, seed=34)
        index = 400
        cold = compute_features(bars[index - 40:index + 1])
        warm = compute_features(bars, index)
        assert cold.warm is False and warm.warm is True
        assert cold.get("atr_pct") != warm.get("atr_pct")

    def test_build_dataset_drops_cold_rows_by_default(self):
        bars = synthetic_bars(ft.MAX_LOOKBACK + 60, seed=35)
        data = build_dataset(bars)
        assert data.report.dropped_cold == ft.MAX_LOOKBACK - 1
        assert (data.bar_index >= ft.MAX_LOOKBACK - 1).all()


# ---------------------------------------------------------------------------
# 4. declared warm-up is the real warm-up
# ---------------------------------------------------------------------------


class TestDeclaredMinimumBars:
    """For every feature: measured at exactly `min_bars`, absent one bar
    earlier. Generated from the schema, so a new feature is covered the moment
    it is declared."""

    @pytest.mark.parametrize("spec", ft.FEATURE_SCHEMA, ids=lambda s: s.name)
    def test_the_declared_minimum_is_exact(self, spec):
        at_minimum = compute_features(
            synthetic_bars(spec.min_bars, seed=41), None, SAMPLE_BOOK
        )
        assert at_minimum.get(spec.name) is not None, (
            f"{spec.name} declares min_bars={spec.min_bars} but is missing there"
        )
        if spec.min_bars > 1:
            below = compute_features(
                synthetic_bars(spec.min_bars - 1, seed=41), None, SAMPLE_BOOK
            )
            assert below.get(spec.name) is None, (
                f"{spec.name} produced a value with fewer than "
                f"{spec.min_bars} bars — the declared warm-up is a lie"
            )


# ---------------------------------------------------------------------------
# 5. the book, present and absent
# ---------------------------------------------------------------------------


class TestMissingBook:
    def test_no_book_means_none_on_every_book_feature(self):
        vector = compute_features(synthetic_bars(400, seed=51))
        for name in ft.BOOK_FEATURE_NAMES:
            assert vector.get(name) is None

    def test_no_book_is_reported_as_missing_by_name(self):
        vector = compute_features(synthetic_bars(400, seed=52))
        assert set(ft.BOOK_FEATURE_NAMES) <= set(vector.missing)

    def test_no_book_is_never_zero(self):
        """0.0 spread and 0.0 imbalance are *measurements*: a perfectly tight,
        perfectly balanced book. A model cannot tell them from 'no book'."""
        vector = compute_features(synthetic_bars(400, seed=53))
        for name in ft.BOOK_FEATURE_NAMES:
            assert vector.get(name) != 0.0
            assert vector.get(name) is None

    def test_book_available_flag_is_false_without_one(self):
        assert compute_features(synthetic_bars(300)).book_available is False

    def test_a_supplied_book_produces_the_book_features(self):
        vector = compute_features(synthetic_bars(400, seed=54), None, SAMPLE_BOOK)
        for name in ft.BOOK_FEATURE_NAMES:
            assert vector.get(name) is not None
        assert vector.book_available is True
        assert vector.is_complete

    def test_a_one_sided_book_measures_nothing(self):
        """market_data refuses to derive a mid from one side; so does this."""
        bids, _ = SAMPLE_BOOK
        vector = compute_features(synthetic_bars(300, seed=55), None, (bids, []))
        for name in ft.BOOK_FEATURE_NAMES:
            assert vector.get(name) is None

    def test_an_empty_book_measures_nothing(self):
        vector = compute_features(synthetic_bars(300, seed=56), None, ([], []))
        for name in ft.BOOK_FEATURE_NAMES:
            assert vector.get(name) is None

    def test_a_crossed_book_measures_nothing(self):
        crossed = ([[101.0, 1.0]], [[99.0, 1.0]])
        vector = compute_features(synthetic_bars(300, seed=57), None, crossed)
        for name in ft.BOOK_FEATURE_NAMES:
            assert vector.get(name) is None

    def test_a_crossed_book_never_yields_a_negative_spread(self):
        """A negative spread arrives at a cost model as a credit."""
        crossed = ([[101.0, 1.0]], [[99.0, 1.0]])
        assert compute_features(
            synthetic_bars(300, seed=58), None, crossed
        ).get("book_spread_bps") is None

    def test_ladder_state_and_features_give_the_same_numbers(self):
        """Four callers hand books around in three shapes; all must funnel into
        one measurement, or training and the live gate diverge."""
        bars = synthetic_bars(300, seed=59)
        bids, asks = SAMPLE_BOOK
        state = md.BookState.from_ladders(bids, asks)
        from_ladders = compute_features(bars, None, SAMPLE_BOOK)
        from_state = compute_features(bars, None, state)
        from_features = compute_features(bars, None, state.features(levels=ft.BOOK_LEVELS))
        assert from_ladders.values == from_state.values == from_features.values

    def test_spread_bps_is_the_market_data_measurement(self):
        bars = synthetic_bars(300, seed=60)
        bids, asks = SAMPLE_BOOK
        expected = md.spread_bps(bids[0][0], asks[0][0])
        assert compute_features(bars, None, SAMPLE_BOOK).get(
            "book_spread_bps") == pytest.approx(expected)

    def test_imbalance_is_bounded(self):
        lopsided = ([[99.0, 10_000.0]], [[101.0, 0.001]])
        value = compute_features(synthetic_bars(300), None, lopsided).get(
            "book_imbalance")
        assert -1.0 <= value <= 1.0

    def test_microprice_deviation_leans_toward_the_thin_side(self):
        """Big bid size, small ask size: the microprice sits above the mid."""
        book = ([[99.0, 10.0]], [[101.0, 1.0]])
        value = compute_features(synthetic_bars(300), None, book).get(
            "book_microprice_dev_bps")
        assert value > 0.0

    def test_an_unrecognised_book_shape_is_refused(self):
        """Guessing at an unknown shape is how a model gets trained against a
        book that was never actually parsed."""
        with pytest.raises(TypeError):
            compute_features(synthetic_bars(300), None, {"bids": [], "asks": []})

    def test_a_three_element_book_is_refused(self):
        with pytest.raises(TypeError):
            compute_features(synthetic_bars(300), None, ([], [], []))

    @needs_real_corpus
    def test_the_real_corpus_has_no_book_and_says_so(self, real_bars):
        data = build_dataset(list(real_bars[:600]))
        assert set(ft.BOOK_FEATURE_NAMES) <= set(data.report.always_missing)
        assert data.report.rows_with_book == 0
        for name in ft.BOOK_FEATURE_NAMES:
            assert np.isnan(data.column(name)).all()


# ---------------------------------------------------------------------------
# 6. nothing is imputed
# ---------------------------------------------------------------------------


class TestNoImputation:
    def test_insufficient_history_is_none_not_zero(self):
        vector = compute_features(synthetic_bars(30, seed=61))
        assert vector.get("vol_regime_pct") is None
        assert vector.get("macd_hist_z") is None
        assert vector.get("dist_sma100_atr") is None

    def test_missing_names_exactly_the_none_values(self):
        vector = compute_features(synthetic_bars(30, seed=62))
        by_name = vector.as_dict()
        assert set(vector.missing) == {k for k, v in by_name.items() if v is None}

    def test_a_single_bar_still_produces_the_shape_only_features(self):
        vector = compute_features(synthetic_bars(1, seed=63))
        assert vector.get("body_fraction") is not None
        assert vector.get("hour_sin") is not None
        assert vector.get("ret_1") is None

    def test_a_zero_range_bar_has_no_shape(self):
        """0/0 is undefined, not 0.5. A 0.5 would read as a perfectly balanced
        bar, which is a measurement nobody made."""
        vector = compute_features(flat_bars(300))
        for name in ("body_fraction", "upper_wick_fraction",
                     "lower_wick_fraction", "close_location"):
            assert vector.get(name) is None

    def test_a_zero_range_bar_still_has_a_defined_size_when_atr_exists(self):
        bars = constant_range_bars(60) + [bar_at(100.0, index=60)]
        assert compute_features(bars).get("range_atr") == pytest.approx(0.0)

    def test_a_flat_series_has_no_atr_and_therefore_no_atr_ratios(self):
        """A zero ATR is not a small ATR — everything downstream divides by it."""
        vector = compute_features(flat_bars(300))
        assert vector.get("atr_pct") is None
        assert vector.get("dist_sma20_atr") is None
        assert vector.get("range_atr") is None

    def test_a_zero_volume_window_gives_no_volume_features(self):
        vector = compute_features(flat_bars(300))
        assert vector.get("volume_z_50") is None
        assert vector.get("volume_trend") is None

    def test_a_flat_series_produces_no_infinities(self):
        for value in compute_features(flat_bars(300)).values:
            assert value is None or math.isfinite(value)

    def test_no_nan_or_inf_ever_appears_in_a_vector(self):
        for seed in range(20):
            bars = synthetic_bars(400, seed=seed, vol=0.05)
            for value in compute_features(bars, None, SAMPLE_BOOK).values:
                assert value is None or math.isfinite(value)

    @pytest.mark.parametrize("n", [1, 2, 5, 14, 15, 25, 50, 100, 133, 264, 400])
    def test_no_nan_or_inf_at_any_history_length(self, n):
        for value in compute_features(synthetic_bars(n, seed=64)).values:
            assert value is None or math.isfinite(value)

    def test_to_array_uses_nan_for_missing(self):
        vector = compute_features(synthetic_bars(400, seed=65))
        row = vector.to_array()
        for name in ft.BOOK_FEATURE_NAMES:
            assert np.isnan(row[ft.FEATURE_INDEX[name]])

    def test_to_array_is_dense_and_float64(self):
        row = compute_features(synthetic_bars(400, seed=66)).to_array()
        assert row.dtype == np.float64
        assert row.shape == (len(ft.FEATURE_NAMES),)

    def test_a_non_finite_input_bar_is_refused(self):
        bars = synthetic_bars(50, seed=67)
        bars[-1] = md.Bar(start_ms=bars[-1].start_ms, open=1.0,
                          high=float("nan"), low=1.0, close=1.0, volume=1.0)
        with pytest.raises(ValueError):
            compute_features(bars)

    def test_a_non_positive_price_is_refused(self):
        bars = synthetic_bars(50, seed=68)
        bars[-1] = md.Bar(start_ms=bars[-1].start_ms, open=1.0, high=1.0,
                          low=0.0, close=1.0, volume=1.0)
        with pytest.raises(ValueError):
            compute_features(bars)

    def test_a_negative_volume_is_refused(self):
        bars = synthetic_bars(50, seed=69)
        bars[-1] = md.Bar(start_ms=bars[-1].start_ms, open=1.0, high=1.0,
                          low=1.0, close=1.0, volume=-5.0)
        with pytest.raises(ValueError):
            compute_features(bars)

    def test_something_that_is_not_a_candle_is_refused(self):
        with pytest.raises(TypeError):
            compute_features([object(), object()])

    def test_price_features_complete_ignores_the_book(self):
        vector = compute_features(synthetic_bars(400, seed=70))
        assert vector.is_complete is False
        assert vector.price_features_complete is True


# ---------------------------------------------------------------------------
# 7. the features mean what they say
# ---------------------------------------------------------------------------


class TestFeatureMeanings:
    def test_ret_1_is_the_log_return(self):
        bars = constant_range_bars(30) + [bar_at(110.0, high=111.0, low=109.0, index=30)]
        assert compute_features(bars).get("ret_1") == pytest.approx(
            math.log(110.0 / 100.0)
        )

    def test_rsi_norm_is_centred_on_zero(self):
        bars = [bar_at(100.0 + 0.0 * i, high=101.0, low=99.0, index=i)
                for i in range(60)]
        value = compute_features(bars).get("rsi_norm")
        assert value == pytest.approx(0.0)

    def test_rsi_norm_saturates_upward_on_a_monotonic_rise(self):
        bars = [bar_at(100.0 * (1.01 ** i), high=100.0 * (1.01 ** i) * 1.001,
                       low=100.0 * (1.01 ** i) * 0.999, index=i)
                for i in range(60)]
        assert compute_features(bars).get("rsi_norm") == pytest.approx(1.0)

    def test_rsi_norm_is_bounded(self):
        for seed in range(5):
            value = compute_features(synthetic_bars(300, seed=seed)).get("rsi_norm")
            assert -1.0 <= value <= 1.0

    def test_adx_norm_is_high_in_a_strong_trend(self):
        bars = [bar_at(100.0 * (1.02 ** i), high=100.0 * (1.02 ** i) * 1.001,
                       low=100.0 * (1.02 ** i) * 0.999, index=i)
                for i in range(200)]
        assert compute_features(bars).get("adx_norm") > 0.25

    def test_di_spread_is_positive_in_an_uptrend(self):
        bars = [bar_at(100.0 * (1.01 ** i), high=100.0 * (1.01 ** i) * 1.001,
                       low=100.0 * (1.01 ** i) * 0.999, index=i)
                for i in range(200)]
        assert compute_features(bars).get("di_spread") > 0.0

    def test_di_spread_is_negative_in_a_downtrend(self):
        bars = [bar_at(100.0 * (0.99 ** i), high=100.0 * (0.99 ** i) * 1.001,
                       low=100.0 * (0.99 ** i) * 0.999, index=i)
                for i in range(200)]
        assert compute_features(bars).get("di_spread") < 0.0

    def test_bb_position_is_a_half_at_the_middle_band(self):
        """Closes chosen so the last 20 average exactly 100 and the final close
        is 100 — the bands are symmetric about it, so the position is 0.5."""
        closes = ([99.0, 101.0] * 10) + [100.0] + ([99.0, 101.0] * 9) + [100.0]
        bars = [bar_at(c, high=c + 0.5, low=c - 0.5, index=i)
                for i, c in enumerate(closes)]
        assert compute_features(bars).get("bb_position") == pytest.approx(0.5)

    def test_a_zero_width_band_is_not_a_position_of_a_half(self):
        """Flat prices give upper == lower; 0.5 there would claim the close sat
        neatly in the middle of a band that does not exist."""
        assert compute_features(flat_bars(60)).get("bb_position") is None

    def test_bb_position_exceeds_one_on_a_breakout_rather_than_clamping(self):
        """Clamping would erase exactly the event the feature exists to see."""
        bars = [bar_at(100.0, high=101.0, low=99.0, index=i) for i in range(40)]
        bars.append(bar_at(150.0, high=151.0, low=149.0, index=40))
        assert compute_features(bars).get("bb_position") > 1.0

    def test_candle_shape_fractions_sum_to_one(self):
        """|body| + upper wick + lower wick is the whole range, by definition."""
        bars = synthetic_bars(50, seed=71)
        vector = compute_features(bars)
        total = (abs(vector.get("body_fraction"))
                 + vector.get("upper_wick_fraction")
                 + vector.get("lower_wick_fraction"))
        assert total == pytest.approx(1.0)

    def test_a_full_bullish_marubozu_is_all_body(self):
        bars = constant_range_bars(30)
        bars.append(md.Bar(start_ms=T0 + 30 * HOUR_MS, open=99.0, high=101.0,
                           low=99.0, close=101.0, volume=10.0))
        vector = compute_features(bars)
        assert vector.get("body_fraction") == pytest.approx(1.0)
        assert vector.get("upper_wick_fraction") == pytest.approx(0.0)
        assert vector.get("lower_wick_fraction") == pytest.approx(0.0)
        assert vector.get("close_location") == pytest.approx(1.0)

    def test_a_full_bearish_marubozu_is_all_body_the_other_way(self):
        bars = constant_range_bars(30)
        bars.append(md.Bar(start_ms=T0 + 30 * HOUR_MS, open=101.0, high=101.0,
                           low=99.0, close=99.0, volume=10.0))
        vector = compute_features(bars)
        assert vector.get("body_fraction") == pytest.approx(-1.0)
        assert vector.get("close_location") == pytest.approx(-1.0)

    def test_atr_pct_is_atr_over_close(self):
        bars = constant_range_bars(60)
        assert compute_features(bars).get("atr_pct") == pytest.approx(2.0 / 100.0)

    def test_range_atr_is_one_when_every_bar_is_the_same_size(self):
        assert compute_features(constant_range_bars(60)).get(
            "range_atr") == pytest.approx(1.0)

    def test_volume_z_is_positive_on_a_volume_spike(self):
        bars = constant_range_bars(60)
        bars[-1] = md.Bar(start_ms=bars[-1].start_ms, open=100.0, high=101.0,
                          low=99.0, close=100.0, volume=1_000.0)
        assert compute_features(bars).get("volume_z_50") > 3.0

    def test_volume_trend_is_positive_when_participation_builds(self):
        bars = [md.Bar(start_ms=T0 + i * HOUR_MS, open=100.0, high=101.0,
                       low=99.0, close=100.0,
                       volume=10.0 if i < 54 else 100.0)
                for i in range(60)]
        assert compute_features(bars).get("volume_trend") > 0.0

    def test_vol_regime_pct_is_one_at_a_volatility_high(self):
        """The regime measure is a rank, so a fresh extreme reads exactly 1.

        Built deterministically: a quiet alternating series whose last 24
        returns are fifty times larger, so the final window is the unique
        most-volatile one in the trailing distribution.
        """
        amplitudes = [0.001] * 400 + [0.05] * ft.VOL_FAST
        closes = [100.0]
        for i, amplitude in enumerate(amplitudes):
            step = amplitude if i % 2 == 0 else -amplitude
            closes.append(closes[-1] * (1.0 + step))
        bars = [md.Bar(start_ms=T0 + i * HOUR_MS, open=c, high=c * 1.01,
                       low=c * 0.99, close=c, volume=10.0)
                for i, c in enumerate(closes)]
        assert compute_features(bars).get("vol_regime_pct") == pytest.approx(1.0)

    def test_vol_regime_pct_is_low_in_the_quietest_window(self):
        amplitudes = [0.05] * 400 + [0.001] * ft.VOL_FAST
        closes = [100.0]
        for i, amplitude in enumerate(amplitudes):
            step = amplitude if i % 2 == 0 else -amplitude
            closes.append(closes[-1] * (1.0 + step))
        bars = [md.Bar(start_ms=T0 + i * HOUR_MS, open=c, high=c * 1.01,
                       low=c * 0.99, close=c, volume=10.0)
                for i, c in enumerate(closes)]
        value = compute_features(bars).get("vol_regime_pct")
        assert value == pytest.approx(1.0 / ft.VOL_REGIME_LOOKBACK)

    def test_vol_regime_pct_is_bounded(self):
        for seed in range(5):
            value = compute_features(synthetic_bars(400, seed=seed)).get(
                "vol_regime_pct")
            assert 0.0 < value <= 1.0

    def test_vol_ratio_above_one_means_volatility_is_expanding(self):
        rng = np.random.default_rng(73)
        quiet = rng.normal(0, 0.001, 300)
        loud = rng.normal(0, 0.02, 30)
        closes = 100.0 * np.cumprod(1.0 + np.concatenate([quiet, loud]))
        bars = [md.Bar(start_ms=T0 + i * HOUR_MS, open=float(c), high=float(c) * 1.001,
                       low=float(c) * 0.999, close=float(c), volume=10.0)
                for i, c in enumerate(closes)]
        assert compute_features(bars).get("vol_ratio_24_72") > 1.0

    @pytest.mark.parametrize("spec", [s for s in ft.FEATURE_SCHEMA if s.bounds],
                             ids=lambda s: s.name)
    def test_declared_bounds_hold(self, spec):
        for seed in range(6):
            bars = synthetic_bars(400, seed=seed, vol=0.03)
            value = compute_features(bars, None, SAMPLE_BOOK).get(spec.name)
            if value is None:
                continue
            low, high = spec.bounds
            assert low - 1e-12 <= value <= high + 1e-12, (
                f"{spec.name}={value} is outside its declared {spec.bounds}"
            )


class TestCyclicalTime:
    def test_midnight_is_the_origin_of_the_daily_circle(self):
        bars = [bar_at(100.0, high=101.0, low=99.0, index=0, start_ms=0)]
        vector = compute_features(bars)
        assert vector.get("hour_sin") == pytest.approx(0.0)
        assert vector.get("hour_cos") == pytest.approx(1.0)

    def test_six_in_the_morning_is_a_quarter_turn(self):
        bars = [bar_at(100.0, high=101.0, low=99.0, index=0, start_ms=6 * HOUR_MS)]
        vector = compute_features(bars)
        assert vector.get("hour_sin") == pytest.approx(1.0)
        assert vector.get("hour_cos") == pytest.approx(0.0, abs=1e-12)

    def test_noon_is_a_half_turn(self):
        bars = [bar_at(100.0, high=101.0, low=99.0, index=0, start_ms=12 * HOUR_MS)]
        assert compute_features(bars).get("hour_cos") == pytest.approx(-1.0)

    def test_the_daily_encoding_is_a_unit_circle(self):
        for hour in range(24):
            bars = [bar_at(100.0, high=101.0, low=99.0, index=0,
                           start_ms=hour * HOUR_MS)]
            vector = compute_features(bars)
            assert (vector.get("hour_sin") ** 2
                    + vector.get("hour_cos") ** 2) == pytest.approx(1.0)

    def test_twenty_three_is_adjacent_to_midnight(self):
        """The whole reason for sin/cos: an integer hour tells a tree that 23
        and 0 are 23 apart when they are one hour apart."""
        def point(hour):
            bars = [bar_at(100.0, high=101.0, low=99.0, index=0,
                           start_ms=hour * HOUR_MS)]
            vector = compute_features(bars)
            return np.array([vector.get("hour_sin"), vector.get("hour_cos")])

        near = np.linalg.norm(point(23) - point(0))
        far = np.linalg.norm(point(12) - point(0))
        assert near < far

    def test_the_epoch_was_a_thursday(self):
        bars = [bar_at(100.0, high=101.0, low=99.0, index=0, start_ms=0)]
        vector = compute_features(bars)
        assert vector.get("dow_sin") == pytest.approx(math.sin(2 * math.pi * 3 / 7))
        assert vector.get("dow_cos") == pytest.approx(math.cos(2 * math.pi * 3 / 7))

    def test_the_corpus_starts_on_a_monday(self):
        bars = [bar_at(100.0, high=101.0, low=99.0, index=0, start_ms=T0)]
        vector = compute_features(bars)
        assert vector.get("dow_sin") == pytest.approx(0.0)
        assert vector.get("dow_cos") == pytest.approx(1.0)

    def test_sunday_is_adjacent_to_monday(self):
        def point(day):
            bars = [bar_at(100.0, high=101.0, low=99.0, index=0,
                           start_ms=T0 + day * 24 * HOUR_MS)]
            vector = compute_features(bars)
            return np.array([vector.get("dow_sin"), vector.get("dow_cos")])

        assert np.linalg.norm(point(6) - point(0)) < np.linalg.norm(
            point(3) - point(0))

    def test_the_weekly_encoding_repeats_every_seven_days(self):
        def point(day):
            bars = [bar_at(100.0, high=101.0, low=99.0, index=0,
                           start_ms=T0 + day * 24 * HOUR_MS)]
            vector = compute_features(bars)
            return (vector.get("dow_sin"), vector.get("dow_cos"))

        assert point(0) == pytest.approx(point(7))

    def test_time_features_are_integer_derived_and_do_not_drift(self):
        """Bit-identical between a training run and a live process is the
        requirement; float division of a 2024 epoch already costs precision."""
        assert ft._time_features(T0) == ft._time_features(T0)
        assert ft._time_features(0)[1] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 8. triple-barrier labels
# ---------------------------------------------------------------------------


class TestTripleBarrier:
    """Hand-built paths. ATR is exactly 2.0 on `constant_range_bars`, so with
    the default 2:1 spec the barriers are exactly 104 and 98."""

    @staticmethod
    def path(after, entry_index=30, spec_bars=None):
        bars = spec_bars or constant_range_bars(entry_index + 1)
        for offset, (high, low, close) in enumerate(after, start=1):
            bars.append(md.Bar(
                start_ms=T0 + (entry_index + offset) * HOUR_MS,
                open=close, high=high, low=low, close=close, volume=10.0,
            ))
        return bars

    def test_the_barriers_are_where_arithmetic_says_they_are(self):
        label = triple_barrier_label(constant_range_bars(60), 30)
        assert label.atr_at_entry == pytest.approx(2.0)
        assert label.entry_price == pytest.approx(100.0)
        assert label.upper_barrier == pytest.approx(104.0)
        assert label.lower_barrier == pytest.approx(98.0)

    def test_a_clean_touch_of_the_upper_barrier_is_a_take_profit(self):
        bars = self.path([(101.0, 100.0, 100.5), (105.0, 100.0, 104.5)])
        label = triple_barrier_label(bars, 30)
        assert label.outcome == ft.LABEL_TAKE_PROFIT
        assert label.barrier == "take_profit"
        assert label.resolved is True
        assert label.bars_to_resolution == 2

    def test_a_clean_touch_of_the_lower_barrier_is_a_stop(self):
        bars = self.path([(100.5, 99.5, 100.0), (100.0, 97.0, 97.5)])
        label = triple_barrier_label(bars, 30)
        assert label.outcome == ft.LABEL_STOP
        assert label.barrier == "stop"
        assert label.bars_to_resolution == 2

    def test_touching_neither_barrier_is_a_timeout(self):
        spec = BarrierSpec(max_horizon=5)
        bars = self.path([(101.0, 99.0, 100.0)] * 10)
        label = triple_barrier_label(bars, 30, spec)
        assert label.outcome == ft.LABEL_TIMEOUT
        assert label.barrier == "timeout"
        assert label.resolved is True
        assert label.bars_to_resolution == 5

    def test_a_bar_touching_both_barriers_resolves_to_the_stop(self):
        """The bar reports its range, not its path. `backtest` makes the same
        conservative choice for a simulated fill; a label that disagreed would
        train the model to expect fills the simulator refuses."""
        bars = self.path([(105.0, 97.0, 100.0)])
        label = triple_barrier_label(bars, 30)
        assert label.outcome == ft.LABEL_STOP
        assert label.bars_to_resolution == 1

    def test_touching_both_barriers_is_pessimistic_for_a_short_too(self):
        spec = BarrierSpec(direction=-1)
        bars = self.path([(105.0, 95.0, 100.0)])
        label = triple_barrier_label(bars, 30, spec)
        assert label.outcome == ft.LABEL_STOP

    def test_exactly_touching_the_barrier_counts(self):
        bars = self.path([(104.0, 100.0, 102.0)])
        assert triple_barrier_label(bars, 30).outcome == ft.LABEL_TAKE_PROFIT

    def test_stopping_one_tick_short_of_the_barrier_does_not_count(self):
        spec = BarrierSpec(max_horizon=1)
        bars = self.path([(103.999, 98.001, 100.0)])
        assert triple_barrier_label(bars, 30, spec).outcome == ft.LABEL_TIMEOUT

    def test_the_feature_bar_cannot_resolve_its_own_label(self):
        """At the moment the features are known the bar is closed and its range
        is already spent; using it would be a one-bar lookahead."""
        bars = constant_range_bars(30)
        bars.append(md.Bar(start_ms=T0 + 30 * HOUR_MS, open=100.0, high=200.0,
                           low=50.0, close=100.0, volume=10.0))
        bars += [bar_at(100.0, high=100.5, low=99.5, index=31 + i)
                 for i in range(10)]
        label = triple_barrier_label(bars, 30, BarrierSpec(max_horizon=5))
        assert label.outcome == ft.LABEL_TIMEOUT

    def test_the_first_barrier_touched_wins(self):
        bars = self.path([
            (101.0, 99.0, 100.0),
            (100.5, 97.0, 98.0),          # stop, bar 2
            (110.0, 100.0, 109.0),        # take profit later — must not count
        ])
        label = triple_barrier_label(bars, 30)
        assert label.outcome == ft.LABEL_STOP
        assert label.bars_to_resolution == 2

    def test_a_touch_after_the_horizon_does_not_count(self):
        spec = BarrierSpec(max_horizon=3)
        bars = self.path([(101.0, 99.0, 100.0)] * 3 + [(120.0, 100.0, 119.0)])
        assert triple_barrier_label(bars, 30, spec).outcome == ft.LABEL_TIMEOUT

    def test_a_touch_on_the_last_horizon_bar_does_count(self):
        spec = BarrierSpec(max_horizon=3)
        bars = self.path([(101.0, 99.0, 100.0)] * 2 + [(120.0, 100.0, 119.0)])
        label = triple_barrier_label(bars, 30, spec)
        assert label.outcome == ft.LABEL_TAKE_PROFIT
        assert label.bars_to_resolution == 3

    def test_a_short_takes_profit_downward(self):
        spec = BarrierSpec(direction=-1)
        bars = self.path([(100.0, 95.0, 96.0)])
        label = triple_barrier_label(bars, 30, spec)
        assert label.outcome == ft.LABEL_TAKE_PROFIT
        assert label.lower_barrier == pytest.approx(96.0)
        assert label.upper_barrier == pytest.approx(102.0)

    def test_a_short_is_stopped_upward(self):
        spec = BarrierSpec(direction=-1)
        bars = self.path([(103.0, 100.0, 102.5)])
        assert triple_barrier_label(bars, 30, spec).outcome == ft.LABEL_STOP

    def test_barriers_scale_with_volatility_not_with_price(self):
        """The point of quoting them in ATR: the same label question at any
        price level and any regime."""
        quiet = triple_barrier_label(constant_range_bars(60, half_range=0.5), 30)
        loud = triple_barrier_label(constant_range_bars(60, half_range=5.0), 30)
        assert quiet.atr_at_entry == pytest.approx(1.0)
        assert loud.atr_at_entry == pytest.approx(10.0)
        assert (loud.upper_barrier - loud.entry_price) == pytest.approx(
            10.0 * (quiet.upper_barrier - quiet.entry_price)
        )

    def test_reward_to_risk_is_reported(self):
        assert BarrierSpec(take_profit_atr=3.0, stop_atr=1.5).reward_to_risk == 2.0

    def test_outcome_end_index_is_where_the_outcome_was_decided(self):
        bars = self.path([(101.0, 99.0, 100.0), (105.0, 100.0, 104.5)])
        label = triple_barrier_label(bars, 30)
        assert label.outcome_end_index == 32

    def test_an_empty_series_cannot_be_labelled(self):
        with pytest.raises(ValueError):
            triple_barrier_label([], 0)

    def test_an_out_of_range_index_raises(self):
        with pytest.raises(IndexError):
            triple_barrier_label(constant_range_bars(10), 10)

    @pytest.mark.parametrize("kwargs", [
        {"take_profit_atr": 0.0},
        {"take_profit_atr": -1.0},
        {"stop_atr": 0.0},
        {"stop_atr": float("nan")},
        {"max_horizon": 0},
        {"atr_period": 1},
        {"direction": 0},
        {"direction": 2},
    ])
    def test_a_nonsensical_barrier_spec_is_refused(self, kwargs):
        with pytest.raises(ValueError):
            BarrierSpec(**kwargs)


class TestUnresolvedLabels:
    def test_the_tail_of_the_series_is_unresolved_not_a_loss(self):
        """Defaulting to "loss" attaches a systematic negative label to
        whatever regime happens to end the sample."""
        bars = constant_range_bars(60)
        spec = BarrierSpec(max_horizon=10)
        label = triple_barrier_label(bars, 55, spec)
        assert label.resolved is False
        assert label.outcome is None
        assert label.reason == "horizon_extends_past_the_data"
        assert label.bars_to_resolution is None
        assert label.outcome_end_index is None

    def test_the_very_last_bar_is_unresolved(self):
        bars = constant_range_bars(60)
        assert triple_barrier_label(bars, 59).resolved is False

    def test_a_tail_bar_that_touches_a_barrier_early_is_resolved(self):
        """Running out of data only matters when the answer was not already
        known — over-purging is its own quiet bias."""
        bars = constant_range_bars(58)
        bars.append(bar_at(105.0, high=105.0, low=100.0, index=58))
        bars.append(bar_at(105.0, high=105.0, low=100.0, index=59))
        label = triple_barrier_label(bars, 57, BarrierSpec(max_horizon=24))
        assert label.resolved is True
        assert label.outcome == ft.LABEL_TAKE_PROFIT

    def test_too_little_history_for_an_atr_is_unresolved(self):
        bars = constant_range_bars(60)
        label = triple_barrier_label(bars, 5)
        assert label.resolved is False
        assert label.reason == "insufficient_history_for_atr"
        assert label.upper_barrier is None

    def test_a_flat_series_has_no_atr_and_so_no_barriers(self):
        label = triple_barrier_label(flat_bars(60), 30)
        assert label.resolved is False
        assert label.atr_at_entry is None

    def test_labels_over_a_range_cover_every_bar(self):
        bars = constant_range_bars(60)
        labels = triple_barrier_labels(bars)
        assert len(labels) == 60
        assert [lab.index for lab in labels] == list(range(60))

    def test_unresolved_labels_are_returned_not_hidden(self):
        """The caller that drops them should be able to count what it dropped."""
        labels = triple_barrier_labels(constant_range_bars(60))
        assert any(not lab.resolved for lab in labels)

    def test_a_sub_range_is_respected(self):
        labels = triple_barrier_labels(constant_range_bars(60), start=20, end=30)
        assert [lab.index for lab in labels] == list(range(20, 30))

    def test_build_dataset_excludes_the_unresolved_tail(self):
        bars = synthetic_bars(ft.MAX_LOOKBACK + 100, seed=81)
        spec = BarrierSpec(max_horizon=24)
        data = build_dataset(bars, barriers=spec)
        assert data.report.dropped_unresolved_label > 0
        last_labelled = int(data.bar_index.max())
        assert last_labelled <= len(bars) - 1
        for index in data.bar_index:
            assert triple_barrier_label(bars, int(index), spec).resolved

    def test_no_kept_row_has_an_unresolved_label(self):
        bars = synthetic_bars(ft.MAX_LOOKBACK + 120, seed=82)
        data = build_dataset(bars)
        assert set(np.unique(data.y)) <= {
            ft.LABEL_STOP, ft.LABEL_TIMEOUT, ft.LABEL_TAKE_PROFIT
        }
        assert not np.isnan(data.y.astype(float)).any()


# ---------------------------------------------------------------------------
# 9. offline == online
# ---------------------------------------------------------------------------


class TestBatchEqualsSingleBar:
    def test_every_row_equals_the_single_bar_computation(self):
        bars = synthetic_bars(ft.MAX_LOOKBACK + 120, seed=91)
        data = build_dataset(bars)
        assert len(data) > 0
        for row, index in zip(data.X, data.bar_index):
            expected = compute_features(bars, int(index)).to_array()
            np.testing.assert_array_equal(row, expected)

    def test_rows_match_with_a_book_too(self):
        bars = synthetic_bars(ft.MAX_LOOKBACK + 60, seed=92)
        books = [SAMPLE_BOOK] * len(bars)
        data = build_dataset(bars, books=books, require=ft.FEATURE_NAMES)
        assert len(data) > 0
        for row, index in zip(data.X, data.bar_index):
            expected = compute_features(bars, int(index), SAMPLE_BOOK).to_array()
            np.testing.assert_array_equal(row, expected)

    def test_timestamps_are_the_feature_bars(self):
        bars = synthetic_bars(ft.MAX_LOOKBACK + 60, seed=93)
        data = build_dataset(bars)
        for stamp, index in zip(data.timestamps, data.bar_index):
            assert int(stamp) == bars[int(index)].start_ms

    def test_labels_match_the_single_bar_labeller(self):
        bars = synthetic_bars(ft.MAX_LOOKBACK + 80, seed=94)
        spec = BarrierSpec(max_horizon=12)
        data = build_dataset(bars, barriers=spec)
        for outcome, horizon, index in zip(
            data.y, data.bars_to_resolution, data.bar_index
        ):
            label = triple_barrier_label(bars, int(index), spec)
            assert int(outcome) == label.outcome
            assert int(horizon) == label.bars_to_resolution

    def test_column_order_is_the_schema_order(self):
        bars = synthetic_bars(ft.MAX_LOOKBACK + 40, seed=95)
        data = build_dataset(bars)
        assert data.feature_names == ft.FEATURE_NAMES
        assert data.X.shape[1] == len(ft.FEATURE_NAMES)

    def test_a_column_accessor_reads_the_right_column(self):
        bars = synthetic_bars(ft.MAX_LOOKBACK + 40, seed=96)
        data = build_dataset(bars)
        column = data.column("rsi_norm")
        for value, index in zip(column, data.bar_index):
            assert value == pytest.approx(
                compute_features(bars, int(index)).get("rsi_norm")
            )

    @needs_real_corpus
    def test_rows_match_the_single_bar_path_on_real_bars(self, real_bars):
        bars = list(real_bars[10_000:10_500])
        data = build_dataset(bars)
        assert len(data) > 0
        for row, index in zip(data.X, data.bar_index):
            np.testing.assert_array_equal(
                row, compute_features(bars, int(index)).to_array()
            )


# ---------------------------------------------------------------------------
# 10. the dataset
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def data():
    """One modest dataset, built once, for the accounting assertions below."""
    return build_dataset(synthetic_bars(ft.MAX_LOOKBACK + 200, seed=101))


class TestDataset:
    def test_shapes_agree(self, data):
        n = len(data)
        assert data.X.shape == (n, len(ft.FEATURE_NAMES))
        assert data.y.shape == (n,)
        assert data.timestamps.shape == (n,)
        assert data.bar_index.shape == (n,)
        assert data.bars_to_resolution.shape == (n,)

    def test_dtypes_are_declared(self, data):
        assert data.X.dtype == np.float64
        assert data.y.dtype == np.int8
        assert data.timestamps.dtype == np.int64

    def test_labels_are_the_three_declared_outcomes(self, data):
        assert set(np.unique(data.y)) <= {-1, 0, 1}

    def test_timestamps_are_strictly_increasing(self, data):
        assert (np.diff(data.timestamps) > 0).all()

    def test_bar_indices_are_strictly_increasing(self, data):
        assert (np.diff(data.bar_index) > 0).all()

    def test_required_columns_contain_no_nan(self, data):
        for name in data.report.required:
            assert not np.isnan(data.column(name)).any()

    def test_book_columns_are_nan_not_zero_without_a_book(self, data):
        """Zero would be a measurement. NaN is the only honest encoding in a
        float matrix."""
        for name in ft.BOOK_FEATURE_NAMES:
            column = data.column(name)
            assert np.isnan(column).all()
            assert not (column == 0.0).any()

    def test_usable_columns_exclude_the_absent_book(self, data):
        usable = set(data.usable_columns())
        assert not usable & set(ft.BOOK_FEATURE_NAMES)
        assert set(ft.PRICE_FEATURE_NAMES) <= usable

    def test_the_report_accounts_for_every_bar(self, data):
        report = data.report
        assert report.rows_kept + report.dropped_total == report.bars_seen

    def test_the_report_records_the_schema_it_was_built_with(self, data):
        assert data.schema_version == ft.FEATURE_SCHEMA_VERSION
        assert data.schema_digest == ft.FEATURE_SCHEMA_DIGEST
        assert data.report.schema_version == ft.FEATURE_SCHEMA_VERSION

    def test_the_report_records_the_barriers(self, data):
        assert data.report.barriers == data.barriers

    def test_label_counts_sum_to_the_rows_kept(self, data):
        assert sum(data.report.label_counts.values()) == data.report.rows_kept

    def test_label_fractions_sum_to_one(self, data):
        total = sum(v for v in data.report.label_fractions().values() if v)
        assert total == pytest.approx(1.0)

    def test_kept_and_dropped_fractions_are_complementary(self, data):
        assert (data.report.kept_fraction
                + data.report.dropped_fraction) == pytest.approx(1.0)

    def test_the_report_cannot_be_mutated(self, data):
        with pytest.raises(TypeError):
            data.report.label_counts[1] = 999

    def test_outcome_end_index_is_the_purge_boundary(self, data):
        """Purged CV drops training rows whose outcome window reaches into the
        test fold; that needs the end index, and here it is."""
        np.testing.assert_array_equal(
            data.outcome_end_index, data.bar_index + data.bars_to_resolution
        )
        assert (data.bars_to_resolution >= 1).all()

    def test_outcome_windows_overlap_which_is_why_purging_exists(self, data):
        """Stated as a test so nobody trains on this with a plain KFold and
        wonders why the validation score is beautiful."""
        overlaps = (data.outcome_end_index[:-1] >= data.bar_index[1:]).sum()
        assert overlaps > 0

    def test_a_typo_in_require_is_refused(self):
        """A required feature that is not in the schema would silently require
        nothing."""
        with pytest.raises(ValueError):
            build_dataset(synthetic_bars(300), require=["rsi_normal"])

    def test_a_misaligned_book_list_is_refused(self):
        bars = synthetic_bars(300, seed=102)
        with pytest.raises(ValueError):
            build_dataset(bars, books=[SAMPLE_BOOK] * 10)

    def test_requiring_a_book_feature_without_a_book_keeps_nothing(self):
        """Correct, not a bug: the alternative is training on a column of
        zeros. The report says which feature did it."""
        bars = synthetic_bars(ft.MAX_LOOKBACK + 60, seed=103)
        data = build_dataset(bars, require=ft.FEATURE_NAMES)
        assert len(data) == 0
        assert data.X.shape == (0, len(ft.FEATURE_NAMES))
        assert data.report.dropped_missing_features > 0
        assert "book_spread_bps" in data.report.always_missing

    def test_a_book_supplied_per_bar_is_used(self):
        bars = synthetic_bars(ft.MAX_LOOKBACK + 60, seed=104)
        books = [SAMPLE_BOOK] * len(bars)
        data = build_dataset(bars, books=books, require=ft.FEATURE_NAMES)
        assert len(data) > 0
        assert data.report.rows_with_book == len(bars)
        assert not np.isnan(data.column("book_spread_bps")).any()

    def test_a_partially_present_book_is_reported_per_row(self):
        bars = synthetic_bars(ft.MAX_LOOKBACK + 60, seed=105)
        books = [SAMPLE_BOOK if i % 2 == 0 else None for i in range(len(bars))]
        data = build_dataset(bars, books=books)
        assert 0 < data.report.rows_with_book < len(bars)
        column = data.column("book_imbalance")
        assert np.isnan(column).any() and not np.isnan(column).all()

    def test_an_empty_range_gives_an_empty_but_well_shaped_dataset(self):
        data = build_dataset(synthetic_bars(300, seed=106), start=10, end=10)
        assert len(data) == 0
        assert data.X.shape == (0, len(ft.FEATURE_NAMES))
        assert data.usable_columns() == ()

    def test_a_sub_range_is_honoured(self):
        bars = synthetic_bars(ft.MAX_LOOKBACK + 200, seed=107)
        data = build_dataset(bars, start=ft.MAX_LOOKBACK, end=ft.MAX_LOOKBACK + 50)
        assert data.report.bars_seen == 50
        assert (data.bar_index >= ft.MAX_LOOKBACK).all()
        assert (data.bar_index < ft.MAX_LOOKBACK + 50).all()

    def test_switching_off_warmup_only_moves_where_the_rows_are_dropped(self):
        """Switching it off does not fabricate history. The cold rows still
        lack `vol_regime_pct`, so they are dropped one line further down as
        missing features — the count moves, the data does not."""
        bars = synthetic_bars(ft.MAX_LOOKBACK + 60, seed=108)
        warm = build_dataset(bars)
        cold = build_dataset(bars, require_warmup=False)
        assert cold.report.dropped_cold == 0
        assert cold.report.dropped_missing_features > warm.report.dropped_missing_features
        assert len(cold) == len(warm)

    def test_switching_off_warmup_keeps_more_rows_only_for_shallow_features(self):
        bars = synthetic_bars(ft.MAX_LOOKBACK + 60, seed=109)
        shallow = build_dataset(
            bars, require=("ret_1", "hour_sin"), require_warmup=False
        )
        assert len(shallow) > len(build_dataset(bars))
        assert shallow.report.dropped_cold == 0

    def test_column_rejects_an_undeclared_name(self, data):
        with pytest.raises(KeyError):
            data.column("not_a_feature")

    def test_n_features_matches_the_schema(self, data):
        assert data.n_features == len(ft.FEATURE_SCHEMA)


# ---------------------------------------------------------------------------
# 11. determinism and purity
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_the_same_input_twice_gives_the_same_vector(self):
        bars = synthetic_bars(400, seed=111)
        first = compute_features(bars, 300, SAMPLE_BOOK)
        second = compute_features(bars, 300, SAMPLE_BOOK)
        assert first.values == second.values
        assert first.missing == second.missing
        np.testing.assert_array_equal(first.to_array(), second.to_array())

    def test_the_same_input_twice_gives_the_same_dataset(self):
        bars = synthetic_bars(ft.MAX_LOOKBACK + 80, seed=112)
        first, second = build_dataset(bars), build_dataset(bars)
        np.testing.assert_array_equal(first.X, second.X)
        np.testing.assert_array_equal(first.y, second.y)
        np.testing.assert_array_equal(first.timestamps, second.timestamps)

    def test_a_list_and_a_tuple_of_the_same_bars_agree(self):
        bars = synthetic_bars(400, seed=113)
        assert compute_features(bars, 300).values == compute_features(
            tuple(bars), 300).values

    def test_computing_does_not_mutate_the_input(self):
        bars = synthetic_bars(400, seed=114)
        before = [(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
                  for b in bars]
        compute_features(bars, 300, SAMPLE_BOOK)
        build_dataset(bars)
        after = [(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
                 for b in bars]
        assert before == after

    def test_labelling_does_not_mutate_the_input(self):
        bars = constant_range_bars(60)
        before = list(bars)
        triple_barrier_labels(bars)
        assert bars == before

    def test_the_vector_is_immutable(self):
        vector = compute_features(synthetic_bars(300, seed=115))
        with pytest.raises(Exception):
            vector.values = ()

    def test_a_label_is_immutable(self):
        label = triple_barrier_label(constant_range_bars(60), 30)
        with pytest.raises(Exception):
            label.outcome = 1

    def test_to_array_returns_a_fresh_array_each_time(self):
        """A shared buffer would let one caller's fill leak into another's row."""
        vector = compute_features(synthetic_bars(300, seed=116))
        first = vector.to_array()
        first[0] = 12345.0
        assert vector.to_array()[0] != 12345.0


def module_imports(filename):
    path = os.path.join(REPO, filename)
    tree = ast.parse(open(path, encoding="utf-8").read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


class TestPurity:
    """The dependency direction is the reason this module can be trusted to be
    the same offline and online. Checked by AST, as `test_indicators.py` does,
    so the docstrings that *discuss* config do not trip the test."""

    def test_imports_only_what_it_is_allowed_to(self):
        allowed = {
            "__future__", "hashlib", "math", "dataclasses", "types", "typing",
            "numpy", "market_data", "pure_indicators",
        }
        assert module_imports("features.py") <= allowed, (
            f"unexpected imports: {sorted(module_imports('features.py') - allowed)}"
        )

    def test_does_not_import_the_trading_stack(self):
        banned = {
            "config", "persistence", "memory", "risk_management",
            "position_sizing", "bybit_connection", "trading_engine",
            "technical_analysis", "backtest", "main", "sklearn", "pandas",
            "torch", "requests", "os", "threading", "asyncio",
        }
        assert not (module_imports("features.py") & banned)

    def test_does_not_read_the_environment(self):
        source = open(os.path.join(REPO, "features.py"), encoding="utf-8").read()
        assert "os.getenv" not in source
        assert "os.environ" not in source

    def test_no_duplicate_top_level_definitions(self):
        """Python's last-definition-wins is the legacy codebase's pathology."""
        import collections

        tree = ast.parse(
            open(os.path.join(REPO, "features.py"), encoding="utf-8").read()
        )
        counts = collections.Counter(
            n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        )
        assert {k: v for k, v in counts.items() if v > 1} == {}

    def test_no_definitions_hidden_in_except_handlers(self):
        tree = ast.parse(
            open(os.path.join(REPO, "features.py"), encoding="utf-8").read()
        )
        trapped = [
            sub.name
            for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)
            for sub in ast.walk(node)
            if isinstance(sub, (ast.FunctionDef, ast.ClassDef))
        ]
        assert trapped == []

    def test_everything_public_is_exported(self):
        for name in ("compute_features", "build_dataset", "FEATURE_SCHEMA",
                     "FEATURE_SCHEMA_VERSION", "triple_barrier_label"):
            assert name in ft.__all__

    def test_indicator_math_is_not_reimplemented(self):
        """One RSI in the repo, not two. `pure_indicators` is the Wilder-correct
        one and this module must call it rather than paste it."""
        source = open(os.path.join(REPO, "features.py"), encoding="utf-8").read()
        assert "pi.rsi(" in source and "pi.atr(" in source and "pi.adx(" in source


# ---------------------------------------------------------------------------
# 12. the real corpus
# ---------------------------------------------------------------------------


@needs_real_corpus
class TestRealCorpus:
    def test_a_dataset_builds_from_the_real_loader(self, real_bars):
        data = build_dataset(list(real_bars[:1_200]))
        assert len(data) > 0
        assert data.X.shape[1] == len(ft.FEATURE_NAMES)

    def test_no_nan_in_required_columns_on_real_bars(self, real_bars):
        data = build_dataset(list(real_bars[:1_200]))
        for name in data.report.required:
            assert not np.isnan(data.column(name)).any(), name

    def test_all_three_outcomes_occur_on_real_bars(self, real_bars):
        """A labelling scheme that produced only one class would be a constant,
        not a label."""
        data = build_dataset(list(real_bars[:3_000]))
        assert all(count > 0 for count in data.report.label_counts.values())

    def test_the_label_distribution_is_not_degenerate(self, real_bars):
        data = build_dataset(list(real_bars[:3_000]))
        fractions = data.report.label_fractions()
        assert max(fractions.values()) < 0.95

    def test_features_stay_finite_across_the_2020_crash(self, real_bars):
        """The most violent window in the corpus is where a division by a
        vanishing denominator would show up."""
        crash_start = 1_583_020_800_000        # 2020-03-01T00:00:00Z
        indices = [i for i, b in enumerate(real_bars)
                   if crash_start <= b.start_ms < crash_start + 30 * 24 * HOUR_MS]
        assert indices, "the 2020-03 window is not in this corpus"
        for index in indices[::24]:
            for value in compute_features(real_bars, index).values:
                assert value is None or math.isfinite(value)

    def test_the_bounded_features_stay_bounded_over_a_long_stretch(self, real_bars):
        bounded = [s for s in ft.FEATURE_SCHEMA if s.bounds and not s.needs_book]
        for index in range(1_000, 20_000, 977):
            vector = compute_features(real_bars, index)
            for spec in bounded:
                value = vector.get(spec.name)
                if value is None:
                    continue
                low, high = spec.bounds
                assert low - 1e-9 <= value <= high + 1e-9, spec.name

    def test_the_dataset_is_chronological(self, real_bars):
        data = build_dataset(list(real_bars[:2_000]))
        assert (np.diff(data.timestamps) > 0).all()
