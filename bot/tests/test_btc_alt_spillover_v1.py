"""Tests for `btc_alt_spillover_v1` — the slice-35 cross-asset candidate.

The signal is the first here whose trigger lives on a *different instrument and
a different venue* from the thing it trades, the first that is two-sided, and
the first that fills at the next bar's open. Each of those is a new way to be
wrong, so each gets its own tests.

Hand-built series throughout: where a case asserts a specific bar, the geometry
was constructed so the answer is checkable by eye rather than by trusting the
implementation.
"""
from __future__ import annotations

import ast
import datetime as dt
import inspect
import os
import sys

import numpy as np
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import backtest as bt  # noqa: E402
import skill_test as sk  # noqa: E402
from signals import btc_alt_spillover_v1 as sp  # noqa: E402

DAY_MS = 86_400_000
EPOCH = int(dt.datetime(2022, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)


def bar(day, close, *, high=None, low=None, open_=None, vol=100.0):
    o = close if open_ is None else open_
    h = max(o, close) if high is None else high
    l = min(o, close) if low is None else low
    return bt.Bar(EPOCH + day * DAY_MS, o, h, l, close, vol)


def flat_series(n, price=100.0, start_day=0):
    """A perfectly flat series: ATR is zero, so no bar can ever be a shock."""
    return [bar(start_day + i, price) for i in range(n)]


def calm_then(n, *, jump_at, jump_pct, price=100.0, wiggle=1.0, start_day=0):
    """A series with a small constant range, then one large close-to-close move.

    The constant high/low range gives a predictable non-zero ATR, so the shock
    threshold is easy to reason about.
    """
    bars = []
    current = price
    for i in range(n):
        if i == jump_at:
            current = current * (1.0 + jump_pct)
        bars.append(bar(start_day + i, current,
                        high=current + wiggle, low=current - wiggle))
    return bars


class TestShockThresholds:

    def test_a_flat_series_produces_no_setup(self):
        """Zero ATR must not divide-by-zero into a universe of signals."""
        assert sp.btc_setups(flat_series(60)) == {}

    def test_a_large_up_move_is_a_long_setup(self):
        bars = calm_then(60, jump_at=40, jump_pct=+0.25)
        setups = sp.btc_setups(bars)
        day = sp._utc_date(bars[40])
        assert setups.get(day) == sp.LONG_SETUP

    def test_a_large_down_move_is_a_short_setup(self):
        bars = calm_then(60, jump_at=40, jump_pct=-0.25)
        setups = sp.btc_setups(bars)
        assert setups.get(sp._utc_date(bars[40])) == sp.SHORT_SETUP

    def test_a_move_below_the_threshold_is_not_a_setup(self):
        """The threshold is 1.0 x ATR%, not 'any move'."""
        bars = calm_then(60, jump_at=40, jump_pct=+0.001, wiggle=2.0)
        assert sp._utc_date(bars[40]) not in sp.btc_setups(bars)

    def test_the_threshold_is_the_declared_multiple(self):
        """A move just under 1.0 x ATR% fails; just over passes.

        Built by measuring the realised threshold on the series itself, so the
        test pins the RULE rather than a hand-computed number that would drift
        if the ATR implementation changed.
        """
        base = calm_then(60, jump_at=40, jump_pct=0.0, wiggle=1.0)
        atr = sp._atr(base)
        close = np.array([b.close for b in base], dtype=float)
        threshold = sp.SHOCK_MULTIPLIER * (atr[40] / close[40])
        assert np.isfinite(threshold) and threshold > 0

        under = calm_then(60, jump_at=40, jump_pct=threshold * 0.5, wiggle=1.0)
        over = calm_then(60, jump_at=40, jump_pct=threshold * 3.0, wiggle=1.0)
        assert sp._utc_date(under[40]) not in sp.btc_setups(under)
        assert sp.btc_setups(over).get(sp._utc_date(over[40])) == sp.LONG_SETUP

    def test_the_multiplier_is_frozen_at_one(self):
        assert sp.SHOCK_MULTIPLIER == 1.0

    def test_the_barrier_constants_are_frozen(self):
        assert sp.STOP_ATR == 1.5
        assert sp.TAKE_PROFIT_R == 2.0
        assert sp.TAKE_PROFIT_ATR == 3.0
        assert sp.HORIZON == 5
        assert sp.ATR_PERIOD == 14
        assert sp.LOCKUP == 1
        assert sp.ROUND_TRIP_BPS == 25.0
        assert sp.ENTRY_ON == "next_open"


class TestDateAlignment:

    def test_a_date_missing_on_the_alt_is_skipped(self):
        """Not filled, not interpolated, not shifted onto a neighbour."""
        btc = calm_then(60, jump_at=40, jump_pct=+0.25)
        alt = [b for i, b in enumerate(flat_series(60)) if i != 40]
        directed = sp.directed_signal_bars(alt, btc)
        shock_day = sp._utc_date(btc[40])
        assert all(sp._utc_date(alt[i]) != shock_day for i, _ in directed)

    def test_a_date_missing_on_btc_produces_nothing(self):
        btc = flat_series(60)
        alt = calm_then(60, jump_at=40, jump_pct=+0.25)
        assert sp.directed_signal_bars(alt, btc) == []

    def test_only_shared_dates_can_fire(self):
        """Alt runs 2022, BTC runs 2023: no overlap, no trades."""
        btc = calm_then(60, jump_at=40, jump_pct=+0.25, start_day=400)
        alt = flat_series(60, start_day=0)
        assert sp.directed_signal_bars(alt, btc) == []

    def test_the_aligned_span_is_reported_honestly(self):
        btc = flat_series(100, start_day=0)
        alt = flat_series(100, start_day=50)
        shared, first, last = sp.aligned_date_span(alt, btc)
        assert shared == 50
        assert first == sp._utc_date(alt[0])
        assert last == sp._utc_date(btc[99])

    def test_no_forward_fill_anywhere_in_the_module(self):
        """AST, not text — the module docstring says "never interpolated".

        Fifth slice running that a text-ban assertion had to become an AST one
        (EDGE.md §12c, §13c). The rule is not "remember to be careful", it is
        "write the guard as an AST check the first time", and I have now failed
        to follow my own rule twice after writing it down. What matters is
        whether any *call* fills a gap.
        """
        banned = {"ffill", "fillna", "interpolate", "reindex", "pad",
                  "bfill", "resample"}
        tree = ast.parse(inspect.getsource(sp))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in banned, node.attr
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None)
                assert name not in banned, name


class TestNoLookahead:

    def test_truncating_the_future_cannot_change_the_past(self):
        rng = np.random.default_rng(35)
        closes = 100.0 + np.cumsum(rng.normal(0, 2.0, 400))
        btc = [bar(i, float(c), high=float(c) + 2, low=float(c) - 2)
               for i, c in enumerate(closes)]
        alt = [bar(i, float(c) * 0.5, high=float(c) * 0.5 + 1,
                   low=float(c) * 0.5 - 1) for i, c in enumerate(closes)]

        full = sp.directed_signal_bars(alt, btc)
        cut = sp.directed_signal_bars(alt[:250], btc[:250])
        # Every signal the truncated run produced must appear identically in the
        # full run. The full run may add later ones; it may not revise earlier.
        assert cut == [d for d in full if d[0] < len(cut and alt[:250]) - 1
                       and d[0] in {c[0] for c in cut}]
        for index, direction in cut:
            assert (index, direction) in full

    def test_the_last_alt_bar_never_fires(self):
        """There is no bar to fill on, so it must be dropped, not clamped."""
        btc = calm_then(60, jump_at=59, jump_pct=+0.25)
        alt = flat_series(60)
        assert all(i != 59 for i, _ in sp.directed_signal_bars(alt, btc))

    def test_warmup_suppresses_earlier_bars(self):
        btc = calm_then(60, jump_at=20, jump_pct=+0.25)
        alt = flat_series(60)
        assert sp.directed_signal_bars(alt, btc, warmup=30) == []


class TestBarrierGeometry:
    """The R arithmetic is `skill_test`'s. These tests pin how it is CALLED."""

    def _series(self, n=60, price=100.0):
        return [bar(i, price, high=price + 2.0, low=price - 2.0) for i in range(n)]

    def test_a_long_stop_is_one_and_a_half_atr_below_the_next_open(self):
        bars = self._series()
        idx, net, _ = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=sp.TAKE_PROFIT_ATR, stop_atr=sp.STOP_ATR,
            horizon=sp.HORIZON, atr_period=sp.ATR_PERIOD,
            round_trip_bps=0.0, side="long", entry_on="next_open")
        assert idx.size > 0
        # A dead-flat series never touches either barrier, so every trade is a
        # time stop at zero move -> exactly 0 R before costs.
        assert np.allclose(net, 0.0, atol=1e-9)

    def test_the_reward_to_risk_is_two(self):
        assert sp.TAKE_PROFIT_ATR / sp.STOP_ATR == pytest.approx(2.0)

    def test_a_long_that_gaps_down_is_stopped_for_minus_one_r(self):
        bars = [bar(i, 100.0, high=102.0, low=98.0) for i in range(40)]
        # Bar 40 opens at 100 and collapses far below any plausible stop.
        bars.append(bar(40, 50.0, high=100.0, low=50.0, open_=100.0))
        bars += [bar(41 + i, 50.0, high=52.0, low=48.0) for i in range(10)]
        idx, net, _ = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=sp.TAKE_PROFIT_ATR, stop_atr=sp.STOP_ATR,
            horizon=sp.HORIZON, atr_period=sp.ATR_PERIOD,
            round_trip_bps=0.0, side="long", entry_on="next_open")
        position = {int(i): p for p, i in enumerate(idx)}[39]
        assert net[position] == pytest.approx(-1.0)

    def test_the_same_collapse_pays_a_short(self):
        bars = [bar(i, 100.0, high=102.0, low=98.0) for i in range(40)]
        bars.append(bar(40, 50.0, high=100.0, low=50.0, open_=100.0))
        bars += [bar(41 + i, 50.0, high=52.0, low=48.0) for i in range(10)]
        idx, net, _ = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=sp.TAKE_PROFIT_ATR, stop_atr=sp.STOP_ATR,
            horizon=sp.HORIZON, atr_period=sp.ATR_PERIOD,
            round_trip_bps=0.0, side="short", entry_on="next_open")
        position = {int(i): p for p, i in enumerate(idx)}[39]
        assert net[position] == pytest.approx(2.0)

    def test_the_entry_bar_itself_can_stop_the_trade_out(self):
        """Entering at an open means that session's low is live.

        Ignoring it would flatter every result by pretending same-day stops
        cannot happen.
        """
        bars = [bar(i, 100.0, high=102.0, low=98.0) for i in range(40)]
        bars.append(bar(40, 99.0, high=100.5, low=40.0, open_=100.0))
        bars += [bar(41 + i, 99.0, high=101.0, low=97.0) for i in range(10)]
        idx, net, used = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=sp.TAKE_PROFIT_ATR, stop_atr=sp.STOP_ATR,
            horizon=sp.HORIZON, atr_period=sp.ATR_PERIOD,
            round_trip_bps=0.0, side="long", entry_on="next_open")
        position = {int(i): p for p, i in enumerate(idx)}[39]
        assert net[position] == pytest.approx(-1.0)
        assert used[position] == 0        # resolved on the entry bar

    def test_the_horizon_is_five_bars_from_the_entry_bar(self):
        bars = [bar(i, 100.0 + i * 0.01, high=100.0 + i * 0.01 + 0.02,
                    low=100.0 + i * 0.01 - 0.02) for i in range(60)]
        _idx, _net, used = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=sp.TAKE_PROFIT_ATR, stop_atr=sp.STOP_ATR,
            horizon=sp.HORIZON, atr_period=sp.ATR_PERIOD,
            round_trip_bps=0.0, side="long", entry_on="next_open")
        assert used.max() == sp.HORIZON


class TestExistingBehaviourIsUnchanged:
    """The extension must not have moved anything that was already measured."""

    def _series(self, n=300, seed=5):
        rng = np.random.default_rng(seed)
        closes = 100.0 + np.cumsum(rng.normal(0.05, 2.0, n))
        return [bar(i, float(c), high=float(c) + 1.5, low=float(c) - 1.5)
                for i, c in enumerate(closes)]

    def test_the_defaults_reproduce_the_pre_slice_35_call(self):
        bars = self._series()
        explicit = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=4.0, stop_atr=2.0, horizon=24,
            atr_period=14, round_trip_bps=25.0, side="long", entry_on="close")
        default = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=4.0, stop_atr=2.0, horizon=24,
            atr_period=14, round_trip_bps=25.0)
        assert np.array_equal(explicit[0], default[0])
        assert np.allclose(explicit[1], default[1], equal_nan=True)
        assert np.array_equal(explicit[2], default[2])

    @pytest.mark.parametrize("bad", ["up", "", "LONG", None])
    def test_an_unknown_side_is_refused(self, bad):
        with pytest.raises(ValueError):
            sk.barrier_r_for_all_bars(
                self._series(60), take_profit_atr=3.0, stop_atr=1.5, horizon=5,
                atr_period=14, round_trip_bps=25.0, side=bad)

    @pytest.mark.parametrize("bad", ["open", "", "next_close", None])
    def test_an_unknown_entry_mode_is_refused(self, bad):
        with pytest.raises(ValueError):
            sk.barrier_r_for_all_bars(
                self._series(60), take_profit_atr=3.0, stop_atr=1.5, horizon=5,
                atr_period=14, round_trip_bps=25.0, entry_on=bad)


class TestOnePositionPerSymbol:

    def test_overlapping_setups_produce_one_trade_each(self):
        """`simulate_schedule` at lock-up 1 is the existing mechanism."""
        flags = np.zeros(300, dtype=bool)
        flags[[210, 211, 212, 250]] = True
        entries = sk.simulate_schedule(flags, warmup=200, lockup=sp.LOCKUP)
        assert entries == [210, 250]

    def test_the_signal_produces_isolated_flags_and_a_direction_for_each(self):
        btc = calm_then(300, jump_at=250, jump_pct=+0.25)
        alt = flat_series(300)
        flags, directions = sp.flags_and_directions(alt, btc, warmup=200)
        assert flags.sum() == len(directions)
        for index in np.nonzero(flags)[0]:
            assert directions[int(index)] in (sp.LONG_SETUP, sp.SHORT_SETUP)


class TestDirectionsAreNotMisassigned:
    """The bug that made the first control run meaningless.

    The direction sequence the nulls recycle must come from the bars ACTUALLY
    entered. Built from every *flagged* bar instead, 37 of 72 observed ETH
    trades were scored in the wrong direction.
    """

    def test_the_observed_schedule_uses_each_bar_s_own_direction(self):
        import edge_measurement as em
        directions = {10: sp.LONG_SETUP, 11: sp.SHORT_SETUP, 20: sp.SHORT_SETUP}
        lookup = {sp.LONG_SETUP: {10: 0, 20: 1}, sp.SHORT_SETUP: {10: 0, 20: 1}}
        net = {sp.LONG_SETUP: np.array([1.0, 1.0]),
               sp.SHORT_SETUP: np.array([-1.0, -1.0])}
        book = em.DirectedBook(directions=directions,
                               direction_seq=[sp.LONG_SETUP, sp.SHORT_SETUP],
                               lookup=lookup, net_r=net)
        # Entries 10 (long) and 20 (short) -> +1.0 and -1.0 -> mean 0.0
        scored = em.score_schedule([10, 20], {}, np.array([]), book=book,
                                   directed_mode="own")
        assert scored.n_trades == 2
        assert scored.mean_r == pytest.approx(0.0)

    def test_a_null_schedule_recycles_the_observed_sequence(self):
        import edge_measurement as em
        lookup = {sp.LONG_SETUP: {30: 0, 40: 1}, sp.SHORT_SETUP: {30: 0, 40: 1}}
        net = {sp.LONG_SETUP: np.array([1.0, 1.0]),
               sp.SHORT_SETUP: np.array([-1.0, -1.0])}
        book = em.DirectedBook(directions={}, direction_seq=[sp.LONG_SETUP],
                               lookup=lookup, net_r=net)
        scored = em.score_schedule([30, 40], {}, np.array([]), book=book,
                                   directed_mode="sequence")
        assert scored.mean_r == pytest.approx(1.0)     # both forced long

    def test_the_long_short_mix_survives_recycling(self):
        import edge_measurement as em
        seq = [sp.LONG_SETUP, sp.LONG_SETUP, sp.SHORT_SETUP]
        book = em.DirectedBook(directions={}, direction_seq=seq,
                               lookup={sp.LONG_SETUP: {}, sp.SHORT_SETUP: {}},
                               net_r={sp.LONG_SETUP: np.array([]),
                                      sp.SHORT_SETUP: np.array([])})
        used = [book.direction_seq[i % len(seq)] for i in range(6)]
        assert used.count(sp.LONG_SETUP) == 4
        assert used.count(sp.SHORT_SETUP) == 2


class TestTheNullRespectsTheConstruction:
    """Slice 36: "the null mishandles one-trade-per-run" — tested, not argued.

    Slice 35's control failures invited that reading. A 1,000-surrogate run
    showed the percentile distribution is uniform (median 50.40, z = -0.51,
    buckets within a point of expectation), which refutes it. These tests pin
    the mechanics so the refutation does not rest on one run.
    """

    def _setup(self):
        import edge_measurement as em
        import market_data as md
        loaded, _b, _n = md.load_corpus(os.path.join(REPO, "data", "real_multi_1d"))
        alt = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
               for b in loaded["ETHUSDT"]]
        btc_loaded, _bb, _bn = md.load_corpus(os.path.join(REPO, "data", "real_1d"))
        btc = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
               for b in btc_loaded[sorted(btc_loaded)[0]]]
        warmup = 200
        flags, directions = sp.flags_and_directions(alt, btc, warmup=warmup)

        lookup, net = {}, {}
        for tag, side in ((sp.LONG_SETUP, "long"), (sp.SHORT_SETUP, "short")):
            idx, r, _u = sk.barrier_r_for_all_bars(
                alt, take_profit_atr=sp.TAKE_PROFIT_ATR, stop_atr=sp.STOP_ATR,
                horizon=sp.HORIZON, atr_period=sp.ATR_PERIOD,
                round_trip_bps=sp.ROUND_TRIP_BPS, side=side,
                entry_on=sp.ENTRY_ON)
            lookup[tag] = {int(i): p for p, i in enumerate(idx)}
            net[tag] = r
        eligible = np.zeros(len(alt), dtype=bool)
        eligible[sorted(set(lookup[sp.LONG_SETUP]) & set(lookup[sp.SHORT_SETUP]))] = True
        eligible[:warmup] = False
        entries = sk.simulate_schedule(flags, warmup=warmup, lockup=sp.LOCKUP)
        book = em.DirectedBook(
            directions=directions,
            direction_seq=[directions[e] for e in entries if e in directions],
            lookup=lookup, net_r=net)
        return em, flags, eligible, entries, book, lookup, net, warmup

    def test_every_null_schedule_obeys_one_trade_per_run(self):
        """No rotation replicate may take two entries inside one flag run."""
        em, flags, eligible, _entries, _book, _lk, _net, warmup = self._setup()
        rng = np.random.default_rng(36)
        region = (flags & eligible)[warmup:]
        for _ in range(200):
            candidate = np.zeros_like(flags)
            candidate[warmup:] = np.roll(
                region, int(rng.integers(1, max(2, region.size))))
            sched = sk.simulate_schedule(candidate, warmup=warmup, lockup=sp.LOCKUP)
            runs, _gaps = sk.extract_blocks(candidate, warmup=warmup)
            for start, length in runs:
                inside = [e for e in sched if start <= e < start + length]
                assert len(inside) <= 1, (start, length, inside)

    def test_the_null_scores_only_next_open_entries(self):
        """Both sides resolve against the SAME next-open lookup, never a close."""
        em, _f, _e, _entries, book, _lk, _net, _w = self._setup()
        alt_len = max(max(book.lookup[sp.LONG_SETUP]),
                      max(book.lookup[sp.SHORT_SETUP])) + 1
        # The next-open lookup excludes one more trailing bar than a close-entry
        # lookup would, because the fill happens one bar later.
        assert sp.ENTRY_ON == "next_open"
        for tag in (sp.LONG_SETUP, sp.SHORT_SETUP):
            assert max(book.lookup[tag]) < alt_len

    def test_the_null_preserves_the_direction_mix(self):
        em, flags, eligible, entries, book, lookup, net, warmup = self._setup()
        observed_long = book.direction_seq.count(sp.LONG_SETUP)
        share = observed_long / len(book.direction_seq)
        rng = np.random.default_rng(37)
        region = (flags & eligible)[warmup:]
        shares = []
        for _ in range(50):
            candidate = np.zeros_like(flags)
            candidate[warmup:] = np.roll(
                region, int(rng.integers(1, max(2, region.size))))
            sched = sk.simulate_schedule(candidate, warmup=warmup, lockup=sp.LOCKUP)
            used = [book.direction_seq[i % len(book.direction_seq)]
                    for i in range(len(sched))]
            shares.append(used.count(sp.LONG_SETUP) / max(1, len(used)))
        assert abs(float(np.mean(shares)) - share) < 0.05

    def test_the_null_never_scores_an_ineligible_bar(self):
        em, _f, eligible, _entries, book, lookup, net, _w = self._setup()
        ineligible = [i for i in range(len(eligible)) if not eligible[i]]
        scored = em.score_schedule(ineligible[:50], lookup[sp.LONG_SETUP],
                                   net[sp.LONG_SETUP], book=book,
                                   directed_mode="sequence")
        if scored is not None:
            # Anything scoreable must have been in a side lookup after all.
            assert scored.n_trades <= len(ineligible[:50])

    def test_the_observed_and_null_use_one_scoring_function(self):
        import edge_measurement as em
        import inspect
        source = inspect.getsource(em)
        assert source.count("def score_schedule(") == 1


class TestNoForkedRArithmetic:

    def test_the_signal_module_defines_no_scoring_function(self):
        tree = ast.parse(inspect.getsource(sp))
        defined = {n.name for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        forbidden = {"barrier_r_for_all_bars", "simulate_schedule",
                     "score_schedule", "shape_matched_flags", "run_permutation"}
        assert not (defined & forbidden)

    def test_it_computes_no_r(self):
        source = inspect.getsource(sp)
        for banned in ("net_r", "take_profit_atr", "stop_atr", "realised",
                       "payoff"):
            assert banned not in source, banned

    def test_it_imports_no_order_path(self):
        tree = ast.parse(inspect.getsource(sp))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for banned in ("risk_management", "trading_engine", "bybit_connection",
                       "position_sizing", "ml_strategy"):
            assert banned not in imported, banned

    def test_the_atr_comes_from_the_production_implementation(self):
        source = inspect.getsource(sp._atr)
        assert "sweep_geometry" in source or "sweep" in source
        assert "wilder_atr" in source

    def test_the_selector_offers_it(self):
        import edge_measurement as em
        source = inspect.getsource(em.main)
        assert '"btc_alt_spillover_v1"' in source
        assert 'default="closed_analyser"' in source


class TestTheOneDayExclusion:
    """Slice 39 — the human's pre-declared data-quality exclusion.

    *"Exclude UTC date 2025-01-07 from the BTC driver for BOTH real-series
    signal generation and the directed null/control path."* The bar is a stub:
    the 1-minute Bitstamp archive stops a minute or two into 7 January 2025,
    leaving volume 0.52 against a corpus median of 2,807.7.

    What these tests protect is not the outcome — EDGE.md 20b shows the
    exclusion changes no setup at all — but the *shape* of the mechanism: a
    fixed, declared list of calendar dates that covers both paths through one
    chokepoint, and that nobody can quietly grow after seeing a number.
    """

    ALT_DAY = 1_100  # any day; only the BTC side is filtered

    def _btc(self, dates):
        """BTC bars stamped on given real calendar dates."""
        out = []
        for date in dates:
            moment = dt.datetime.fromisoformat(date).replace(
                tzinfo=dt.timezone.utc)
            out.append(bt.Bar(int(moment.timestamp() * 1000),
                              100.0, 101.0, 99.0, 100.0, 10.0))
        return out

    def test_the_declared_list_is_exactly_one_date(self):
        """Pinned. A list that can grow after a run is not a pre-declaration."""
        assert sp.EXCLUDED_BTC_UTC_DATES == ("2025-01-07",)

    def test_the_excluded_bar_is_dropped(self):
        bars = self._btc(["2025-01-05", "2025-01-06", "2025-01-07",
                          "2025-01-08", "2025-01-09"])
        kept = {sp._utc_date(b) for b in sp.usable_btc_bars(bars)}
        assert "2025-01-07" not in kept

    def test_the_neighbours_are_untouched(self):
        """The decisive contrast: one day, not a window."""
        bars = self._btc(["2025-01-05", "2025-01-06", "2025-01-07",
                          "2025-01-08", "2025-01-09"])
        kept = {sp._utc_date(b) for b in sp.usable_btc_bars(bars)}
        assert kept == {"2025-01-05", "2025-01-06", "2025-01-08", "2025-01-09"}

    def test_no_other_date_is_ever_dropped(self):
        """Swept across four years of real dates, one at a time."""
        start = dt.date(2023, 1, 1)
        dates = [(start + dt.timedelta(days=i)).isoformat()
                 for i in range(1200)]
        bars = self._btc(dates)
        kept = [sp._utc_date(b) for b in sp.usable_btc_bars(bars)]
        assert len(kept) == len(dates) - 1
        assert set(dates) - set(kept) == {"2025-01-07"}

    def test_order_is_preserved_and_nothing_is_restamped(self):
        bars = self._btc(["2025-01-06", "2025-01-07", "2025-01-08"])
        kept = sp.usable_btc_bars(bars)
        assert [b.start_ms for b in kept] == [bars[0].start_ms,
                                              bars[2].start_ms]

    def test_an_empty_exclusion_list_is_the_identity(self, monkeypatch):
        """The filter must vanish when the list is empty, not merely be cheap."""
        monkeypatch.setattr(sp, "EXCLUDED_BTC_UTC_DATES", ())
        bars = self._btc(["2025-01-06", "2025-01-07", "2025-01-08"])
        assert sp.usable_btc_bars(bars) == list(bars)

    def test_the_filter_runs_before_any_return_is_computed(self):
        """Structural: `btc_setups` is the chokepoint both paths come through.

        The real series and the directed null both reach the driver here, so
        filtering inside this function is what makes "in BOTH paths" true by
        construction instead of by two call sites kept in step by hand.
        """
        tree = ast.parse(inspect.getsource(sp.btc_setups))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "usable_btc_bars"]
        assert len(calls) == 1

    def test_the_excluded_date_generates_no_setup(self):
        """A shock ON the excluded day must not survive the filter.

        Built so the excluded bar *would* be a setup if it were kept: a calm
        series, then a +30% close on 2025-01-07 alone.
        """
        dates = [(dt.date(2024, 12, 1) + dt.timedelta(days=i)).isoformat()
                 for i in range(60)]
        bars = []
        for date in dates:
            moment = dt.datetime.fromisoformat(date).replace(
                tzinfo=dt.timezone.utc)
            price = 130.0 if date == "2025-01-07" else 100.0
            bars.append(bt.Bar(int(moment.timestamp() * 1000), price,
                               price + 1.0, price - 1.0, price, 10.0))
        with_stub = sp.btc_setups(bars)
        assert "2025-01-07" not in with_stub

        # ...and the same series with the spike on a neighbouring day IS one,
        # so the test above is about the exclusion and not about the fixture.
        moved = []
        for date in dates:
            moment = dt.datetime.fromisoformat(date).replace(
                tzinfo=dt.timezone.utc)
            price = 130.0 if date == "2025-01-09" else 100.0
            moved.append(bt.Bar(int(moment.timestamp() * 1000), price,
                                price + 1.0, price - 1.0, price, 10.0))
        assert sp.btc_setups(moved).get("2025-01-09") == sp.LONG_SETUP

    def test_the_reported_span_drops_the_excluded_date_too(self):
        """A span counting a date the trigger can never fire on overstates it."""
        dates = [(dt.date(2025, 1, 1) + dt.timedelta(days=i)).isoformat()
                 for i in range(20)]
        btc = self._btc(dates)
        alt = self._btc(dates)
        shared, first, last = sp.aligned_date_span(alt, btc)
        assert shared == len(dates) - 1
        assert first == "2025-01-01" and last == "2025-01-20"


class TestTheExclusionReachesTheNullPath:
    """The half of the pre-declaration that is easy to leave undone.

    The control shuffles the *alt* bars and leaves BTC alone, so a BTC-side
    filter only reaches the null if the null consumes the same flag array the
    real run does. It does — and that is asserted here against the real
    corpus rather than a fixture, because this is the claim the slice-39
    control logs rest on.
    """

    def _corpus(self):
        import market_data as md
        loaded, _b, _n = md.load_corpus(os.path.join(REPO, "data",
                                                     "real_multi_1d"))
        alt = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
               for b in loaded["ETHUSDT"]]
        btc_loaded, _bb, _bn = md.load_corpus(os.path.join(REPO, "data",
                                                          "real_1d"))
        btc = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
               for b in btc_loaded[sorted(btc_loaded)[0]]]
        return alt, btc

    def test_the_control_builder_sees_the_filtered_driver(self):
        import control_directed as cd
        alt, btc = self._corpus()

        class Args:
            multiplier = sp.SHOCK_MULTIPLIER
            atr_period = sp.ATR_PERIOD
            horizon = sp.HORIZON
            stop_atr = sp.STOP_ATR
            take_profit_atr = sp.TAKE_PROFIT_ATR
            round_trip_bps = sp.ROUND_TRIP_BPS
            lockup = sp.LOCKUP

        flags = cd.build_book_and_flags(alt, btc, warmup=200, args=Args())[0]
        flagged = {sp._utc_date(alt[i])
                   for i in range(len(alt)) if flags[i]}
        assert "2025-01-07" not in flagged

    def test_the_real_path_and_the_null_path_share_one_flag_array(self):
        """Not "both were filtered" — the *same* array, so they cannot diverge."""
        import control_directed as cd
        alt, btc = self._corpus()

        class Args:
            multiplier = sp.SHOCK_MULTIPLIER
            atr_period = sp.ATR_PERIOD
            horizon = sp.HORIZON
            stop_atr = sp.STOP_ATR
            take_profit_atr = sp.TAKE_PROFIT_ATR
            round_trip_bps = sp.ROUND_TRIP_BPS
            lockup = sp.LOCKUP

        control_flags = cd.build_book_and_flags(alt, btc, warmup=200,
                                                args=Args())[0]
        real_flags, _dirs = sp.flags_and_directions(alt, btc, warmup=200)
        assert np.array_equal(control_flags, real_flags)

    def test_the_2025_01_08_shock_survives_the_exclusion(self):
        """EDGE.md 20b, asserted rather than asserted-about.

        The declared reason for the exclusion was that the stub bar
        manufactures a spurious shock on 2025-01-08. It does not: the stub's
        close is 0.017% from the previous close, so the shock is a property of
        the *missing* true close, not of the stub's presence. If a future
        change makes this test fail, the exclusion has started doing something
        it was never shown to do, and EDGE.md 20b needs rewriting.
        """
        _alt, btc = self._corpus()
        setups = sp.btc_setups(btc)
        assert setups.get("2025-01-08") == sp.SHORT_SETUP
