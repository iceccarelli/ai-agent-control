"""Tests for the skill-test instrument itself — Stage 1's only deliverable.

Slices 10 through 16 all failed for the same reason in different disguises: a
null that *claimed* to hold something constant and did not. So the properties
asserted here are the claims the block resampler makes about itself, and they
are asserted as exact identities rather than as tolerances, because every one
of them is exact by construction:

    the multiset of flag RUN LENGTHS is preserved     -> clustering
    the multiset of GAP LENGTHS is preserved          -> spacing
    every run STARTS on a bar of its own stratum      -> geometry
    the arrangement tiles the series exactly          -> no drift, no overflow

If any of these silently degraded, the refusal reported in EDGE.md §4g would be
a bug rather than a finding, and that distinction is the whole value of the
slice. Everything here is offline, synthetic and hand-checkable.
"""
from __future__ import annotations

import os
import sys
from collections import Counter

import numpy as np
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import skill_test as sk  # noqa: E402


WARMUP = 10
N_BARS = 240


def _flags_with_runs(spans):
    """A flag array whose runs are exactly ``spans`` = [(start, length), ...]."""
    flags = np.zeros(N_BARS, dtype=bool)
    for start, length in spans:
        flags[start:start + length] = True
    return flags


SPANS = [(15, 3), (25, 1), (40, 7), (60, 2), (95, 12), (140, 4), (180, 6)]


def _one_cell_strata():
    strata = np.full(N_BARS, -1, dtype=np.int32)
    strata[WARMUP:] = 0
    return strata


def _two_cell_strata():
    """Alternating blocks of two cells, coarse enough to leave real choice."""
    strata = np.full(N_BARS, -1, dtype=np.int32)
    for index in range(WARMUP, N_BARS):
        strata[index] = (index // 5) % 2
    return strata


def _resample(strata, seed=0, budget=200_000):
    flags = _flags_with_runs(SPANS)
    runs, gaps = sk.extract_blocks(flags, warmup=WARMUP)
    lengths = np.array([length for _, length in runs], dtype=np.int64)
    cells = np.array([int(strata[start]) for start, _ in runs], dtype=np.int64)
    built = sk.block_resample_flags(
        lengths, cells, np.array(gaps, dtype=np.int64), strata, N_BARS,
        warmup=WARMUP, node_budget=budget, rng=np.random.default_rng(seed),
    )
    return flags, runs, gaps, lengths, cells, built


# ---------------------------------------------------------------------------
# extract_blocks
# ---------------------------------------------------------------------------

class TestBlockExtraction:

    def test_the_runs_are_exactly_the_spans_that_were_written(self):
        flags = _flags_with_runs(SPANS)
        runs, _gaps = sk.extract_blocks(flags, warmup=WARMUP)
        assert runs == SPANS

    def test_lengths_and_gaps_tile_the_region_exactly(self):
        """The identity the whole resampler rests on.

        If this failed, a re-tiling could not land on the last bar by
        arithmetic and the sampler would need slack it does not have.
        """
        flags = _flags_with_runs(SPANS)
        runs, gaps = sk.extract_blocks(flags, warmup=WARMUP)
        total = sum(length for _, length in runs) + sum(gaps)
        assert total == N_BARS - WARMUP

    def test_there_is_one_more_gap_than_run(self):
        flags = _flags_with_runs(SPANS)
        runs, gaps = sk.extract_blocks(flags, warmup=WARMUP)
        assert len(gaps) == len(runs) + 1

    def test_nothing_before_warmup_is_ever_reported(self):
        flags = _flags_with_runs([(2, 4)] + SPANS)
        runs, _gaps = sk.extract_blocks(flags, warmup=WARMUP)
        assert all(start >= WARMUP for start, _ in runs)


# ---------------------------------------------------------------------------
# block_resample_flags — the four exact invariants
# ---------------------------------------------------------------------------

class TestBlockResampleIsExact:

    def test_a_legal_tiling_is_found_when_every_bar_matches(self):
        _f, _r, _g, _l, _c, built = _resample(_one_cell_strata())
        assert built is not None

    def test_the_multiset_of_run_lengths_is_preserved_exactly(self):
        """Clustering. Not approximated, not bounded — identical."""
        _f, _r, _g, lengths, _c, built = _resample(_one_cell_strata())
        assert built is not None
        new_runs, _new_gaps = sk.extract_blocks(built[0], warmup=WARMUP)
        assert (Counter(length for _, length in new_runs)
                == Counter(int(x) for x in lengths))

    def test_the_multiset_of_gap_lengths_is_preserved_exactly(self):
        """Spacing. This is what slice 16's free-space variant lost."""
        _f, _r, gaps, _l, _c, built = _resample(_one_cell_strata())
        assert built is not None
        _new_runs, new_gaps = sk.extract_blocks(built[0], warmup=WARMUP)
        assert Counter(new_gaps) == Counter(gaps)

    def test_the_total_number_of_flagged_bars_is_preserved_exactly(self):
        flags, _r, _g, _l, _c, built = _resample(_one_cell_strata())
        assert built is not None
        assert int(built[0].sum()) == int(flags.sum())

    def test_every_run_starts_on_a_bar_of_a_matching_stratum(self):
        """Geometry. The one thing the placement controls, asserted directly."""
        strata = _two_cell_strata()
        _f, _r, _g, _l, cells, built = _resample(strata, seed=3)
        assert built is not None
        new_runs, _gaps = sk.extract_blocks(built[0], warmup=WARMUP)
        placed = Counter(int(strata[start]) for start, _ in new_runs)
        assert placed == Counter(int(c) for c in cells)

    def test_nothing_is_placed_before_warmup(self):
        _f, _r, _g, _l, _c, built = _resample(_one_cell_strata())
        assert built is not None
        assert not built[0][:WARMUP].any()

    def test_different_seeds_give_different_arrangements(self):
        """A null whose replicates are all the same arrangement is one point."""
        first = _resample(_two_cell_strata(), seed=1)[5]
        second = _resample(_two_cell_strata(), seed=2)[5]
        assert first is not None and second is not None
        assert not np.array_equal(first[0], second[0])

    def test_an_unsatisfiable_geometry_returns_none_rather_than_a_fallback(self):
        """The refusal path. A block may not be placed outside its own cell.

        Every run is declared to sit in a cell that exists nowhere in the
        series. A sampler that quietly relaxed the constraint would return an
        arrangement anyway — which is precisely the silent weakening the brief
        forbids, and the reason this assertion exists.
        """
        strata = _one_cell_strata()
        flags = _flags_with_runs(SPANS)
        runs, gaps = sk.extract_blocks(flags, warmup=WARMUP)
        lengths = np.array([length for _, length in runs], dtype=np.int64)
        impossible = np.full(len(runs), 7, dtype=np.int64)   # no bar is cell 7
        built = sk.block_resample_flags(
            lengths, impossible, np.array(gaps, dtype=np.int64), strata,
            N_BARS, warmup=WARMUP, node_budget=5_000,
            rng=np.random.default_rng(0),
        )
        assert built is None

    def test_the_search_respects_its_node_budget(self):
        """A budget of zero cannot succeed, and must not hang."""
        strata = _one_cell_strata()
        flags = _flags_with_runs(SPANS)
        runs, gaps = sk.extract_blocks(flags, warmup=WARMUP)
        lengths = np.array([length for _, length in runs], dtype=np.int64)
        cells = np.array([int(strata[s]) for s, _ in runs], dtype=np.int64)
        built = sk.block_resample_flags(
            lengths, cells, np.array(gaps, dtype=np.int64), strata, N_BARS,
            warmup=WARMUP, node_budget=0, rng=np.random.default_rng(0),
        )
        assert built is None


# ---------------------------------------------------------------------------
# the distance metric and the tolerance rule
# ---------------------------------------------------------------------------

class TestToleranceIsFixedByRule:

    def test_total_variation_is_zero_for_identical_distributions(self):
        p = np.array([0.5, 0.25, 0.25])
        assert sk.total_variation(p, p) == pytest.approx(0.0)

    def test_total_variation_is_one_for_disjoint_support(self):
        p = np.array([1.0, 0.0])
        q = np.array([0.0, 1.0])
        assert sk.total_variation(p, q) == pytest.approx(1.0)

    def test_total_variation_is_symmetric(self):
        p = np.array([0.6, 0.3, 0.1])
        q = np.array([0.2, 0.2, 0.6])
        assert sk.total_variation(p, q) == pytest.approx(sk.total_variation(q, p))

    def test_the_sampling_floor_is_strictly_positive_for_sparse_cells(self):
        """The reason the tolerance is not zero.

        54 draws over 25 cells cannot reproduce their own parent distribution:
        the expected count per cell is about two and sampling noise alone puts
        a hard lower bound on any achievable distance. A tolerance of zero
        would reject a perfectly matched null.
        """
        observed = np.full(25, 1.0 / 25.0)
        tolerance, floor_p10 = sk.sampling_floor_tolerance(
            observed, 54, resamples=400, rng=np.random.default_rng(0),
        )
        assert tolerance > 0.05
        assert 0.0 < floor_p10 <= tolerance

    def test_the_floor_shrinks_as_the_sample_grows(self):
        observed = np.full(25, 1.0 / 25.0)
        small, _ = sk.sampling_floor_tolerance(
            observed, 54, resamples=400, rng=np.random.default_rng(1))
        large, _ = sk.sampling_floor_tolerance(
            observed, 5_400, resamples=400, rng=np.random.default_rng(1))
        assert large < small

    def test_the_tolerance_is_reproducible_from_its_seed(self):
        observed = np.full(25, 1.0 / 25.0)
        first, _ = sk.sampling_floor_tolerance(
            observed, 54, resamples=200, rng=np.random.default_rng(11))
        second, _ = sk.sampling_floor_tolerance(
            observed, 54, resamples=200, rng=np.random.default_rng(11))
        assert first == second


# ---------------------------------------------------------------------------
# the schedule mechanics both sides share
# ---------------------------------------------------------------------------

class TestScheduleMechanics:

    def test_a_long_run_trades_once_not_once_per_lockup(self):
        """The slice-17 change, stated as the single case that defines it.

        A 20-bar run under a 7-bar lock-up used to trade at 20, 27 and 34 —
        two of those three entries being interior bars no block placement can
        choose. It now trades once, on the run's first bar.
        """
        flags = np.zeros(100, dtype=bool)
        flags[20:40] = True
        assert sk.simulate_schedule(flags, warmup=10, lockup=7) == [20]

    def test_a_run_shorter_than_the_lockup_trades_once(self):
        flags = np.zeros(100, dtype=bool)
        flags[20:23] = True
        assert sk.simulate_schedule(flags, warmup=10, lockup=24) == [20]

    def test_a_run_starting_inside_a_lockup_contributes_nothing(self):
        """``AT MOST one`` is load-bearing: a swallowed run trades zero times.

        It is emphatically NOT entered at an interior bar once the lock-up
        expires — that was the old behaviour and it is what made 28 of 54
        entries unreachable by a placement.
        """
        flags = np.zeros(100, dtype=bool)
        flags[20:22] = True
        flags[25:27] = True
        assert sk.simulate_schedule(flags, warmup=10, lockup=24) == [20]

    def test_a_lockup_expiring_mid_run_does_not_enter_mid_run(self):
        flags = np.zeros(100, dtype=bool)
        flags[20:22] = True
        flags[30:50] = True          # lock-up from bar 20 expires at 44, inside
        assert sk.simulate_schedule(flags, warmup=10, lockup=24) == [20]

    def test_every_entry_is_a_run_start_for_any_flag_pattern(self):
        """STEP 3's invariant, asserted over many random flag sequences.

        This is the property the whole slice exists to obtain, so it is checked
        against arbitrary inputs rather than one hand-picked case.
        """
        rng = np.random.default_rng(17)
        for _trial in range(200):
            flags = rng.random(300) < rng.uniform(0.05, 0.6)
            flags[:10] = False
            runs, _gaps = sk.extract_blocks(flags, warmup=10)
            starts = {start for start, _ in runs}
            for lockup in (1, 7, 24):
                entries = sk.simulate_schedule(flags, warmup=10, lockup=lockup)
                assert all(e in starts for e in entries)
                assert len(entries) == len(set(entries))
                assert entries == sorted(entries)

    def test_a_lockup_of_one_trades_every_run_exactly_once(self):
        """The degenerate case, and the only one where entries == all starts.

        With no cross-run lock-up the entry set IS the run-start set, so entry
        geometry and start geometry are identical by construction. It is
        measured in EDGE.md 4h and it is not the shipped default, because
        consecutive trades' scoring windows then overlap.
        """
        rng = np.random.default_rng(4)
        for _trial in range(50):
            flags = rng.random(300) < 0.3
            flags[:10] = False
            runs, _gaps = sk.extract_blocks(flags, warmup=10)
            entries = sk.simulate_schedule(flags, warmup=10, lockup=1)
            assert entries == [start for start, _ in runs]

    def test_the_same_flags_always_give_the_same_schedule(self):
        flags = np.zeros(200, dtype=bool)
        flags[np.array([15, 16, 44, 90, 91, 92, 150])] = True
        first = sk.simulate_schedule(flags, warmup=10, lockup=13)
        second = sk.simulate_schedule(flags, warmup=10, lockup=13)
        assert first == second


# ---------------------------------------------------------------------------
# slice 18 — the residual diagnostic's two quantities
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.join(REPO, "tools"))
import residual_diagnostic as rd  # noqa: E402


class TestBackwardAtrOverPrice:
    """Wilder ATR(n)/close, checked against a hand calculation.

    Wilder's ATR seeds with the SIMPLE mean of the first ``n`` true ranges and
    then smooths with ``atr = (atr*(n-1) + tr) / n``. Both halves are checked,
    because an implementation that used an EMA throughout would agree with a
    simple-mean-only test on bar n and diverge afterwards.
    """

    def test_a_constant_range_series_gives_that_range_over_price(self):
        n = 40
        high = np.full(n, 102.0)
        low = np.full(n, 100.0)
        close = np.full(n, 101.0)
        out = rd.backward_atr_over_price(high, low, close, period=14)
        # Every true range is exactly 2.0, so ATR is 2.0 once seeded.
        assert out[20] == pytest.approx(2.0 / 101.0)

    def test_it_matches_a_hand_rolled_wilder_calculation(self):
        rng = np.random.default_rng(18)
        n = 60
        close = 100.0 + np.cumsum(rng.normal(0, 1.0, n))
        high = close + rng.uniform(0.5, 2.0, n)
        low = close - rng.uniform(0.5, 2.0, n)

        period = 14
        true_range = np.empty(n)
        true_range[0] = high[0] - low[0]
        for i in range(1, n):
            true_range[i] = max(high[i] - low[i],
                                abs(high[i] - close[i - 1]),
                                abs(low[i] - close[i - 1]))
        expected = np.full(n, np.nan)
        expected[period] = true_range[1:period + 1].mean()
        for i in range(period + 1, n):
            expected[i] = (expected[i - 1] * (period - 1) + true_range[i]) / period

        out = rd.backward_atr_over_price(high, low, close, period=period)
        for i in (period, period + 1, 30, 45, n - 1):
            assert out[i] == pytest.approx(expected[i] / close[i], rel=1e-12)

    def test_the_warmup_is_nan_and_never_zero(self):
        n = 30
        out = rd.backward_atr_over_price(
            np.full(n, 101.0), np.full(n, 99.0), np.full(n, 100.0), period=14)
        assert np.isnan(out[:14]).all()

    def test_it_uses_no_bar_after_the_one_it_reports(self):
        """Structural no-lookahead: truncating the future cannot change it."""
        rng = np.random.default_rng(3)
        n = 80
        close = 100.0 + np.cumsum(rng.normal(0, 1.0, n))
        high = close + 1.0
        low = close - 1.0
        full = rd.backward_atr_over_price(high, low, close, period=14)
        cut = rd.backward_atr_over_price(high[:50], low[:50], close[:50], period=14)
        assert np.allclose(full[14:50], cut[14:50], equal_nan=True)


class TestForwardRealisedVol:

    def test_it_is_the_range_of_the_next_h_bars_over_this_bars_close(self):
        n = 30
        high = np.full(n, 100.0)
        low = np.full(n, 100.0)
        close = np.full(n, 100.0)
        high[5] = 110.0      # inside the window that follows bar 3
        low[7] = 90.0
        out = rd.forward_realised_vol(high, low, close, horizon=5)
        # bars 4..8 -> max high 110, min low 90, close[3] = 100
        assert out[3] == pytest.approx((110.0 - 90.0) / 100.0)

    def test_bar_i_itself_is_excluded(self):
        """Disjointness from the backward window — asserted, not assumed."""
        n = 20
        high = np.full(n, 100.0)
        low = np.full(n, 100.0)
        close = np.full(n, 100.0)
        high[4] = 500.0                       # bar i's own high is enormous
        out = rd.forward_realised_vol(high, low, close, horizon=3)
        assert out[4] == pytest.approx(0.0)   # ... and contributes nothing

    def test_the_tail_is_nan_where_the_window_is_incomplete(self):
        n = 20
        out = rd.forward_realised_vol(
            np.full(n, 101.0), np.full(n, 99.0), np.full(n, 100.0), horizon=5)
        assert np.isnan(out[-5:]).all()
        assert np.isfinite(out[-6])

    def test_a_hand_computed_case_with_a_trend(self):
        close = np.array([10.0, 11.0, 12.0, 13.0, 14.0, 15.0], dtype=float)
        high = close + 0.5
        low = close - 0.5
        out = rd.forward_realised_vol(high, low, close, horizon=2)
        # from bar 0: bars 1..2 -> high 12.5, low 10.5, close[0] = 10
        assert out[0] == pytest.approx((12.5 - 10.5) / 10.0)
        # from bar 3: bars 4..5 -> high 15.5, low 13.5, close[3] = 13
        assert out[3] == pytest.approx((15.5 - 13.5) / 13.0)


class TestMannWhitney:
    """The test statistic, checked where the answer is known exactly."""

    def test_identical_samples_are_not_significant(self):
        values = np.arange(1.0, 41.0)
        _u, p = rd.mann_whitney_u(values, values)
        assert p > 0.9

    def test_completely_separated_samples_are_significant(self):
        _u, p = rd.mann_whitney_u(np.arange(0.0, 30.0), np.arange(100.0, 130.0))
        assert p < 1e-6

    def test_u_is_zero_when_every_a_is_below_every_b(self):
        u, _p = rd.mann_whitney_u(np.array([1.0, 2.0, 3.0]),
                                  np.array([4.0, 5.0, 6.0]))
        assert u == pytest.approx(0.0)

    def test_it_is_symmetric_in_significance(self):
        a = np.array([1.0, 2.0, 3.0, 4.0, 9.0, 10.0])
        b = np.array([5.0, 6.0, 7.0, 8.0, 11.0, 12.0])
        _u1, p1 = rd.mann_whitney_u(a, b)
        _u2, p2 = rd.mann_whitney_u(b, a)
        assert p1 == pytest.approx(p2)

    def test_an_empty_sample_returns_nan_rather_than_a_number(self):
        _u, p = rd.mann_whitney_u(np.array([]), np.array([1.0, 2.0]))
        assert np.isnan(p)


# ---------------------------------------------------------------------------
# slice 19 — shape-matched, information-free flags (the bisection's Side B)
# ---------------------------------------------------------------------------

class TestShapeMatchedFlags:
    """Side B must share the SHAPE of the analyser's flags and nothing else.

    If it shared less, the comparison would confound shape with information —
    which is the confound the whole bisection exists to remove. If it shared
    more, it would not be information-free. Both failure modes are silent, so
    each identity is asserted rather than argued.
    """

    @staticmethod
    def _shape_of(flags, warmup=WARMUP):
        runs, gaps = sk.extract_blocks(flags, warmup=warmup)
        return Counter(length for _, length in runs), Counter(gaps)

    def _build(self, seed=0, source=None):
        source = _flags_with_runs(SPANS) if source is None else source
        runs, gaps = sk.extract_blocks(source, warmup=WARMUP)
        lengths = np.array([length for _, length in runs], dtype=np.int64)
        synthetic = sk.shape_matched_flags(
            lengths, np.array(gaps, dtype=np.int64), source.size,
            warmup=WARMUP, rng=np.random.default_rng(seed),
        )
        return source, synthetic

    def test_the_run_length_multiset_matches_exactly(self):
        source, synthetic = self._build()
        assert self._shape_of(synthetic)[0] == self._shape_of(source)[0]

    def test_the_gap_length_multiset_matches_exactly(self):
        source, synthetic = self._build()
        assert self._shape_of(synthetic)[1] == self._shape_of(source)[1]

    def test_the_total_flagged_bars_match_exactly(self):
        source, synthetic = self._build()
        assert int(synthetic.sum()) == int(source.sum())

    def test_it_tiles_the_post_warmup_region_exactly(self):
        source, synthetic = self._build()
        runs, gaps = sk.extract_blocks(synthetic, warmup=WARMUP)
        assert sum(l for _, l in runs) + sum(gaps) == source.size - WARMUP
        assert not synthetic[:WARMUP].any()

    def test_a_different_seed_moves_the_blocks_but_not_the_multisets(self):
        """Placement is random; shape is not. Both halves asserted together."""
        source, first = self._build(seed=1)
        _source, second = self._build(seed=2)
        assert not np.array_equal(first, second)
        assert self._shape_of(first) == self._shape_of(second)
        assert self._shape_of(first) == self._shape_of(source)

    def test_the_same_seed_is_reproducible(self):
        _s, first = self._build(seed=7)
        _s, second = self._build(seed=7)
        assert np.array_equal(first, second)

    def test_it_cannot_see_the_price_path_at_all(self):
        """Independence is STRUCTURAL: there is no bar argument to depend on.

        Asserted against the signature rather than by sampling outputs, because
        a sampled check could pass on a function that reads a global.
        """
        import inspect
        parameters = set(inspect.signature(sk.shape_matched_flags).parameters)
        assert parameters == {"run_lengths", "gaps", "n_bars", "warmup", "rng"}

    def test_every_entry_is_still_a_run_start_under_the_current_map(self):
        _source, synthetic = self._build(seed=5)
        runs, _gaps = sk.extract_blocks(synthetic, warmup=WARMUP)
        starts = {start for start, _ in runs}
        entries = sk.simulate_schedule(synthetic, warmup=WARMUP, lockup=1)
        assert entries == sorted(starts)

    def test_an_empty_shape_gives_an_empty_sequence(self):
        out = sk.shape_matched_flags(
            np.array([], dtype=np.int64), np.array([230], dtype=np.int64),
            N_BARS, warmup=WARMUP, rng=np.random.default_rng(0),
        )
        assert not out.any()

    def test_a_zero_length_edge_gap_never_merges_two_runs(self):
        """The bug the property test caught, pinned as its own case.

        A run touching the last bar makes the trailing gap 0. Shuffled into an
        interior slot that zero would fuse two runs into one and silently
        change the multiset the whole bisection depends on.
        """
        source = np.zeros(N_BARS, dtype=bool)
        source[WARMUP:WARMUP + 5] = True         # leading gap 0
        source[100:106] = True
        source[N_BARS - 4:] = True               # trailing gap 0
        runs, gaps = sk.extract_blocks(source, warmup=WARMUP)
        assert gaps.count(0) == 2, "the fixture must actually contain zeros"
        lengths = np.array([l for _, l in runs], dtype=np.int64)
        for seed in range(40):
            synthetic = sk.shape_matched_flags(
                lengths, np.array(gaps, dtype=np.int64), N_BARS,
                warmup=WARMUP, rng=np.random.default_rng(seed),
            )
            assert self._shape_of(synthetic) == self._shape_of(source)

    def test_more_than_two_zero_gaps_is_refused_not_silently_absorbed(self):
        with pytest.raises(ValueError):
            sk.shape_matched_flags(
                np.array([2, 2, 2], dtype=np.int64),
                np.array([0, 0, 0, 0], dtype=np.int64),
                N_BARS, warmup=WARMUP, rng=np.random.default_rng(0),
            )

    def test_it_holds_for_many_random_shapes(self):
        rng = np.random.default_rng(19)
        for _trial in range(100):
            source = rng.random(N_BARS) < rng.uniform(0.1, 0.7)
            source[:WARMUP] = False
            runs, gaps = sk.extract_blocks(source, warmup=WARMUP)
            if not runs:
                continue
            lengths = np.array([l for _, l in runs], dtype=np.int64)
            synthetic = sk.shape_matched_flags(
                lengths, np.array(gaps, dtype=np.int64), N_BARS,
                warmup=WARMUP, rng=rng,
            )
            assert self._shape_of(synthetic) == self._shape_of(source)
            assert int(synthetic.sum()) == int(source.sum())


class TestFlagsOverrideIsInert:
    """The injection point must not change the default path.

    ``flags_override`` exists only so the bisection can feed Side B through the
    identical pipeline. If passing None differed in any way from not passing
    it, every number in EDGE.md §4h would silently be about a different tool.
    """

    def test_the_default_is_none(self):
        import inspect
        default = inspect.signature(sk.run_permutation).parameters[
            "flags_override"].default
        assert default is None

    def test_signal_flags_is_still_what_the_default_path_calls(self):
        """Read the source: the override may only bypass the analyser call."""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(sk.run_permutation))
        calls = {node.func.id for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        assert "signal_flags" in calls


# ---------------------------------------------------------------------------
# slice 20 — the null-pool filter diagnostic's ranking arithmetic
# ---------------------------------------------------------------------------

import filter_diagnostic as fd  # noqa: E402


class TestRankPools:
    """One pool, two rankings. The arithmetic that decides slice 20.

    Tested against hand-built pools rather than only against whatever the
    constraint search happened to produce, because the whole claim of the slice
    is a comparison between these two numbers.
    """

    def test_when_every_legal_retiling_is_accepted_the_two_are_identical(self):
        """The null case the brief asks for: no filtering, no difference."""
        means = [0.0, 0.1, 0.2, 0.3, 0.4]
        distances = [0.01] * 5                       # all well inside tolerance
        unf, acc, n_legal, n_acc = fd.rank_pools(0.25, means, distances, 0.5)
        assert n_legal == 5 and n_acc == 5
        assert unf == pytest.approx(acc)
        assert unf == pytest.approx(60.0)            # 3 of 5 below 0.25

    def test_sparse_acceptance_can_change_the_percentile(self):
        """The case that makes the diagnostic meaningful.

        The observed value beats every accepted arrangement but only half the
        legal ones, so the filter LIFTS the rank from 50 to 100 without any
        property of the observed side changing.
        """
        means = [0.0, 0.1, 0.9, 1.0]
        distances = [0.01, 0.01, 0.9, 0.9]           # only the low-R pair pass
        unf, acc, n_legal, n_acc = fd.rank_pools(0.5, means, distances, 0.5)
        assert n_acc == 2 and n_legal == 4
        assert n_acc <= n_legal
        assert unf == pytest.approx(50.0)
        assert acc == pytest.approx(100.0)

    def test_the_accepted_pool_is_never_larger_than_the_legal_pool(self):
        rng = np.random.default_rng(20)
        for _trial in range(200):
            size = int(rng.integers(1, 40))
            means = rng.normal(size=size)
            distances = rng.random(size)
            _unf, _acc, n_legal, n_acc = fd.rank_pools(
                float(rng.normal()), means, distances, float(rng.random()))
            assert n_acc <= n_legal == size

    def test_an_empty_legal_pool_returns_none_rather_than_a_number(self):
        unf, acc, n_legal, n_acc = fd.rank_pools(0.5, [], [], 0.2)
        assert unf is None and acc is None and n_legal == 0 and n_acc == 0

    def test_an_empty_accepted_pool_still_reports_the_unfiltered_rank(self):
        """A filter that rejects everything must not destroy the other number."""
        unf, acc, n_legal, n_acc = fd.rank_pools(
            0.5, [0.0, 1.0], [0.9, 0.9], 0.2)
        assert n_legal == 2 and n_acc == 0
        assert acc is None
        assert unf == pytest.approx(50.0)

    def test_it_uses_the_same_percentile_convention_as_the_instrument(self):
        """`(means < observed).mean() * 100` — strictly less-than, as shipped.

        If this drifted, the accepted-pool number would not be comparable with
        any figure already recorded in EDGE.md.
        """
        means = [0.5, 0.5, 0.5, 0.5]
        unf, _acc, _nl, _na = fd.rank_pools(0.5, means, [0.0] * 4, 1.0)
        assert unf == pytest.approx(0.0)             # ties count as NOT below


# ---------------------------------------------------------------------------
# slice 21 — the rotation passthrough in the bisection tool
# ---------------------------------------------------------------------------

import bisection as bis  # noqa: E402


class TestBisectionNullPassthrough:
    """``--null`` must reach BOTH sides, and must reach them identically.

    The whole validity of a bisection is that the two sides differ in exactly
    one respect. If the null kind could differ between them — by a default in
    one path and an argument in the other — the comparison would be measuring
    two things at once and nobody would see it.
    """

    def test_run_side_forwards_the_null_kind_to_run_permutation(self, monkeypatch):
        seen = []

        def spy(*_args, **kwargs):
            seen.append(kwargs.get("null_kind"))
            return None

        monkeypatch.setattr(bis.sk, "run_permutation", spy)
        common = dict(runs=10, seed=1, lockup=1, horizon=24, atr_period=14,
                      strata_bins=5, block_tries=10, block_nodes=10,
                      block_target=10)
        cfg = bis.bt.BacktestConfig(starting_cash=1.0, warmup_bars=2,
                                    interval="D")
        bis.run_side([], cfg, flags=None, null_kind="rotation", **common)
        bis.run_side([], cfg, flags=np.zeros(3, dtype=bool),
                     null_kind="rotation", **common)
        assert seen == ["rotation", "rotation"]

    def test_both_sides_receive_the_same_null_kind_from_one_cli_value(self):
        """Read the source: the CLI value is passed once, to both calls.

        ``common`` is built once and splatted into both ``run_side`` calls, so a
        divergence is impossible by construction rather than by convention.
        """
        import ast
        import inspect
        source = inspect.getsource(bis.main)
        tree = ast.parse(source.strip())
        # Exactly one place assigns null_kind from the parsed arguments.
        assignments = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.keyword) and node.arg == "null_kind"
        ]
        assert len(assignments) == 1
        assert "args.null_kind" in ast.unparse(assignments[0].value)

    def test_the_default_null_is_unchanged(self):
        """Backward compatibility with the slice-19 numbers in EDGE.md 4l."""
        import inspect
        default = inspect.signature(bis.run_side).parameters["null_kind"].default
        assert default == "block_resample"

    def test_rotation_is_an_accepted_choice_on_both_tools(self):
        for module in (sk, bis):
            source = __import__("inspect").getsource(module.main)
            assert "rotation" in source

    def test_a_rotation_null_needs_no_placement_search_at_all(self):
        """Why rotation is the discriminator, asserted rather than asserted-to.

        The rotation branch of ``run_permutation`` never calls
        ``block_resample_flags``, so it has no solve rate, no candidate cells
        and no tolerance acceptance — none of the machinery slices 15-20 spent
        their time on.
        """
        import ast
        import inspect
        source = inspect.getsource(sk.run_permutation)
        tree = ast.parse(source.strip())
        # The block_resample_flags call sits inside a branch guarded by a
        # comparison against the string "block_resample".
        guards = [ast.unparse(node.test) for node in ast.walk(tree)
                  if isinstance(node, ast.If)
                  and "block_resample_flags" in ast.unparse(node)]
        assert guards, "expected the placement search to be branch-guarded"
        assert any("block_resample" in guard for guard in guards)


# ---------------------------------------------------------------------------
# slice 23 — the control-log parser that persists the confirmation record
# ---------------------------------------------------------------------------

import parse_control_log as pcl  # noqa: E402


class TestControlLogParser:
    """The record of a PASS has to be reconstructible from the artefact.

    `parse_control_log.py` is a reader, not a measurement — but the slice-23
    decision rests on the CSV it writes, so it is tested like anything else the
    decision rests on.
    """

    LOG = (
        "some preamble\n"
        "  surrogate 1: percentile  48.0  (49 trades)\n"
        "  surrogate 2: percentile 100.0  (37 trades)\n"
        "  surrogate 3: too few trades to rank\n"
        "  surrogate 4: percentile   0.0  (62 trades)\n"
        "VERDICT\n"
    )

    def _run(self, tmp_path, text):
        source = tmp_path / "in.log"
        target = tmp_path / "out.csv"
        source.write_text(text)
        pcl.main([str(source), str(target)])
        return target.read_text().strip().splitlines()

    def test_every_ranked_surrogate_becomes_a_row(self, tmp_path):
        rows = self._run(tmp_path, self.LOG)
        assert rows[0] == "surrogate,percentile,trades"
        assert rows[1] == "1,48.0,49"
        assert rows[2] == "2,100.0,37"
        assert rows[4] == "4,0.0,62"

    def test_an_incomplete_surrogate_is_recorded_not_dropped(self, tmp_path):
        """Silently dropping these would select the easy surrogates."""
        rows = self._run(tmp_path, self.LOG)
        assert any(row.startswith("3,,") for row in rows)
        assert "too few trades to rank" in "\n".join(rows)

    def test_boundary_percentiles_survive_the_round_trip(self, tmp_path):
        """0.0 and 100.0 are real values, not sentinels."""
        rows = self._run(tmp_path, self.LOG)
        values = [r.split(",")[1] for r in rows[1:] if r.split(",")[1]]
        assert "0.0" in values and "100.0" in values

    def test_lines_that_are_not_surrogate_rows_are_ignored(self, tmp_path):
        rows = self._run(tmp_path, self.LOG)
        assert len(rows) == 5      # header + 3 ranked + 1 incomplete

    def test_it_refuses_wrong_argument_counts_rather_than_guessing(self):
        assert pcl.main([]) == 2
        assert pcl.main(["only-one"]) == 2


# ---------------------------------------------------------------------------
# slice 24 — the edge-measurement wrapper must not fork the arithmetic
# ---------------------------------------------------------------------------

import edge_measurement as em  # noqa: E402


class TestEdgeMeasurementReusesTheInstrument:
    """A forked copy of the R maths would invalidate the comparison silently.

    The whole warrant of slice 24's reading is that the real-series side and the
    null side are scored by the machinery slice 23 validated. If this wrapper
    reimplemented any of it and drifted by one line, every number it printed
    would be about a different instrument and nothing would say so.
    """

    def test_it_defines_no_barrier_or_signal_arithmetic_of_its_own(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(em))
        defined = {n.name for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        forbidden = {"barrier_r_for_all_bars", "signal_flags",
                     "simulate_schedule", "shape_matched_flags",
                     "extract_blocks", "wilder_atr"}
        assert not (defined & forbidden), f"wrapper redefines {defined & forbidden}"

    def test_it_calls_the_instrument_for_every_scored_quantity(self):
        import inspect
        source = inspect.getsource(em)
        for name in ("sk.barrier_r_for_all_bars", "sk.signal_flags",
                     "sk.simulate_schedule", "sk.shape_matched_flags",
                     "sk.extract_blocks"):
            assert name in source, f"{name} is not called"

    def test_the_percentile_convention_matches_the_instrument(self):
        """Strictly less-than, so ties are NOT counted as below.

        Identical to ``run_permutation``'s ``(means < strategy_r).mean()*100``.
        If it drifted, an M1 percentile would not be comparable with any
        control number in EDGE.md.
        """
        assert em.percentile_of(0.5, [0.5, 0.5, 0.5, 0.5]) == pytest.approx(0.0)
        assert em.percentile_of(1.0, [0.0, 0.5, 2.0, 3.0]) == pytest.approx(50.0)
        assert em.percentile_of(9.0, [0.0, 1.0]) == pytest.approx(100.0)
        assert np.isnan(em.percentile_of(1.0, []))

    def test_the_pass_bars_are_the_pre_declared_ones(self):
        """Constants, not arguments — they cannot be moved from a command line."""
        assert em.M1_PERCENTILE_BAR == 95.0
        assert em.M2_PERCENTILE_BAR == 95.0
        assert em.M3_SOFT_GATE_FOLDS == 3

    def test_score_schedule_averages_only_scoreable_entries(self):
        net = np.array([1.0, 2.0, 3.0])
        lookup = {10: 0, 20: 1, 30: 2}
        scored = em.score_schedule([10, 20, 30, 99], lookup, net)
        assert scored.n_trades == 3
        assert scored.mean_r == pytest.approx(2.0)
        assert em.score_schedule([99], lookup, net) is None

    def test_the_bootstrap_ci_brackets_the_point_estimate(self):
        rng = np.random.default_rng(0)
        distribution = rng.normal(0.10, 0.05, 400)
        delta, low, high = em.bootstrap_ci(0.30, distribution,
                                           resamples=2000, rng=rng)
        assert delta == pytest.approx(0.30 - distribution.mean())
        assert low < delta < high
        assert low > 0                       # a clearly positive contrast

    def test_a_null_contrast_gives_a_ci_that_contains_zero(self):
        rng = np.random.default_rng(1)
        distribution = rng.normal(0.10, 0.05, 400)
        _delta, low, high = em.bootstrap_ci(float(distribution.mean()),
                                            distribution, resamples=2000, rng=rng)
        assert low < 0 < high

    def test_folds_tile_the_series_after_warmup_without_overlap(self):
        bounds = em.fold_bounds(2564, 4, 200)
        assert bounds[0][0] == 200 and bounds[-1][1] == 2564
        for (_a, b), (c, _d) in zip(bounds, bounds[1:]):
            assert b == c


# ---------------------------------------------------------------------------
# slice 26 — the control exit code must mean what the humans declared
# ---------------------------------------------------------------------------

class TestControlExitCodeHygiene:
    """`skill_test`'s exit code answers 'is the instrument valid?'.

    Before slice 26 it exited 1 whenever the worst surrogate crossed a 75
    ceiling — a bar a *correct* instrument fails with near certainty
    (0.75**n; ~1e-25 at n=200). Slice 23's validated control exited 1 on it.
    These tests pin the declared rule so the ruler cannot go back to
    contradicting its own reading.
    """

    @staticmethod
    def _source():
        import inspect
        return inspect.getsource(sk.main)

    def test_exit_zero_is_reachable_only_through_the_certification_branch(self):
        """No silent pass: exactly one `return 0` after the control block."""
        source = self._source()
        control_section = source[source.index("controls: List[float] = []"):]
        assert control_section.count("return 0") == 1
        assert "control_valid = " in control_section

    def test_certification_requires_all_three_declared_conditions(self):
        source = self._source()
        assert "control_valid = median_ok and uniformity_ok and completeness_ok" in source

    def test_the_median_condition_is_the_declared_one(self):
        assert "median_ok = median_control <= 50.0" in self._source()

    def test_the_uniformity_test_is_the_one_already_in_the_tool(self):
        """28.87/sqrt(n) is the sd of U(0,100); 1.96 is two-sided 5%.

        No new significance rule was invented for slice 26 — this is the same
        z the tool has printed since slice 17.
        """
        source = self._source()
        assert "28.87 / np.sqrt(len(controls))" in source
        assert "1.96" in source

    def test_the_incomplete_budget_generalises_slice_23s_ten_of_two_hundred(self):
        source = self._source()
        assert "incomplete_budget = max(1, int(0.05 * int(args.control_runs)))" in source
        # 10 at n=200 exactly, and never zero at small n.
        for n, expected in ((200, 10), (100, 5), (24, 1), (12, 1), (6, 1)):
            assert max(1, int(0.05 * n)) == expected

    def test_the_worst_ceiling_is_reported_but_never_gates(self):
        """It may print a note. It may not return."""
        source = self._source()
        block = source[source.index("if worst_control >= args.control_ceiling:"):]
        note = block[:block.index("if not uniformity_testable:")]
        assert "return" not in note
        assert "NOT a gate" in note

    def test_worst_is_still_printed_on_every_run(self):
        source = self._source()
        assert 'worst {worst_control:.1f}' in source

    def test_an_unbuildable_null_still_exits_two(self):
        source = self._source()
        assert "INSTRUMENT UNUSABLE" in source
        unusable = source[source.index("INSTRUMENT UNUSABLE"):]
        assert unusable[:200].count("return 2") == 1

    def test_a_valid_control_does_not_claim_edge(self):
        """The exact misreading this project has fought for sixteen slices."""
        source = self._source()
        certified = source[source.index("CONTROL: **VALID**"):]
        assert "EDGE/SKILL: GREEN" not in certified
        assert "SAYS NOTHING ABOUT EDGE" in certified
        assert "NOT a skill claim" in certified

    def test_an_invalid_control_forbids_interpreting_the_real_series(self):
        source = self._source()
        invalid = source[source.index("CONTROL: **INVALID"):]
        assert "must not be interpreted" in invalid

    def test_the_edge_tool_still_fails_closed_on_its_own_bars(self):
        """Hygiene did not 'align' the edge tool to exit 0."""
        import inspect
        source = inspect.getsource(em.main)
        assert 'return 0 if verdict == "EDGE_EVIDENCE_POSITIVE" else 1' in source
        assert em.M1_PERCENTILE_BAR == 95.0 and em.M2_PERCENTILE_BAR == 95.0
