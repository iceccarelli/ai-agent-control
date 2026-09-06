"""Tests for donchian_breakout_v1 — the slice-28 candidate signal.

The previous signal layer was closed by measurement, not by opinion. This one
gets the same instrument, which means the flags it produces have to be exactly
what the pre-declaration in EDGE.md §9b says they are — no look-ahead, no drift
in the constants, no quiet re-interpretation of "breakout event".

Everything here is offline, synthetic and hand-checkable. Where a case asserts a
specific bar, the geometry was constructed so the answer is obvious by
inspection rather than by trusting the implementation.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

from signals import donchian_breakout_v1 as db  # noqa: E402


def _flat_then_breakout(n=db.CHANNEL_N, level=100.0, spike_at=None, spike=110.0):
    """A flat channel at ``level``, with one bar closing above it."""
    size = n + 20
    high = np.full(size, level)
    low = np.full(size, level - 5.0)
    close = np.full(size, level - 1.0)
    if spike_at is not None:
        close[spike_at] = spike
        high[spike_at] = spike
    return high, low, close


# ---------------------------------------------------------------------------
# the constants are the pre-declaration, not preferences
# ---------------------------------------------------------------------------

class TestConstantsAreAsPreDeclared:
    """EDGE.md §9b fixed these before any number existed.

    Grid-searching N after seeing a percentile is the bar-shopping
    RESEARCH_STATUS.md forbids, so the value lives in code as a constant and is
    asserted here rather than passed in from a command line.
    """

    def test_channel_length_is_fifty_five(self):
        assert db.CHANNEL_N == 55

    def test_the_barrier_matches_the_validated_instrument(self):
        """Identical to slice 23's, so the only difference is WHICH BARS."""
        assert db.STOP_ATR == 2.0
        assert db.TAKE_PROFIT_ATR == 4.0
        assert db.HORIZON == 24
        assert db.ATR_PERIOD == 14
        assert db.ROUND_TRIP_BPS == 25.0
        assert db.LOCKUP == 1


# ---------------------------------------------------------------------------
# the channel itself
# ---------------------------------------------------------------------------

class TestChannels:

    def test_the_upper_channel_is_the_max_of_the_previous_n_bars(self):
        high = np.arange(10.0, 10.0 + 30.0)      # strictly increasing
        low = high - 5.0
        upper, lower = db.channels(high, low, n=5)
        # bar 10's channel is bars 5..9 -> highs 15..19 -> max 19
        assert upper[10] == pytest.approx(19.0)
        assert lower[10] == pytest.approx(10.0)  # lows 10..14 -> min 10

    def test_the_channel_excludes_the_bar_it_judges(self):
        """The entire no-lookahead argument, asserted on one bar.

        Bar 10's own high is enormous. If it leaked into its own channel, the
        breakout inequality could never be satisfied and the signal would be
        silently dead.
        """
        high = np.full(30, 100.0)
        low = np.full(30, 95.0)
        high[10] = 1_000.0
        upper, _lower = db.channels(high, low, n=5)
        assert upper[10] == pytest.approx(100.0)     # not 1000
        assert upper[11] == pytest.approx(1_000.0)   # it does count for bar 11

    def test_the_warmup_is_nan_not_zero(self):
        upper, lower = db.channels(np.full(30, 100.0), np.full(30, 95.0), n=5)
        assert np.isnan(upper[:5]).all()
        assert np.isnan(lower[:5]).all()

    def test_truncating_the_future_cannot_change_the_past(self):
        """Structural no-lookahead: recompute on a prefix, compare."""
        rng = np.random.default_rng(28)
        close = 100.0 + np.cumsum(rng.normal(0, 1.0, 200))
        high, low = close + 1.0, close - 1.0
        full, _ = db.channels(high, low, n=db.CHANNEL_N)
        cut, _ = db.channels(high[:120], low[:120], n=db.CHANNEL_N)
        assert np.allclose(full[db.CHANNEL_N:120], cut[db.CHANNEL_N:120],
                           equal_nan=True)


# ---------------------------------------------------------------------------
# the flags
# ---------------------------------------------------------------------------

class TestBreakoutFlags:

    def test_a_single_close_above_the_channel_flags_that_bar(self):
        n = 5
        high, low, close = _flat_then_breakout(n=n, spike_at=12)
        flags = db.breakout_flags(high, low, close, n=n)
        assert flags[12]
        assert flags.sum() == 1

    def test_a_sustained_run_above_the_channel_flags_only_the_crossing(self):
        """The EVENT clause. Without it, one idea becomes many correlated entries.

        Ten consecutive closes above the channel must produce exactly ONE flag,
        on the bar that crossed — otherwise the trade count would measure how
        long trends last rather than how often breakouts happen.
        """
        n = 5
        size = n + 30
        high = np.full(size, 100.0)
        low = np.full(size, 95.0)
        close = np.full(size, 99.0)
        close[12:22] = 110.0
        high[12:22] = 110.0
        flags = db.breakout_flags(high, low, close, n=n)
        assert flags[12]
        assert not flags[13:22].any()
        assert flags.sum() == 1

    def test_a_close_equal_to_the_channel_does_not_flag(self):
        """Strictly greater-than. A touch is not a breakout."""
        n = 5
        size = n + 20
        high = np.full(size, 100.0)
        low = np.full(size, 95.0)
        close = np.full(size, 99.0)
        close[12] = 100.0                     # equal, not above
        flags = db.breakout_flags(high, low, close, n=n)
        assert not flags.any()

    def test_two_separate_breakouts_flag_twice(self):
        """Distinct events flag independently once the first has aged out.

        Bars 12 and 25 are 13 apart, so with ``n=5`` the first spike is no
        longer inside the channel bar 25 is judged against — the channel there
        is back to 100. Both are therefore genuine crossings.
        """
        n = 5
        size = n + 40
        high = np.full(size, 100.0)
        low = np.full(size, 95.0)
        close = np.full(size, 99.0)
        for bar in (12, 25):
            close[bar] = 110.0
            high[bar] = 110.0
        flags = db.breakout_flags(high, low, close, n=n)
        assert flags[12]
        assert flags[25]
        assert flags.sum() == 2

    def test_a_second_breakout_inside_the_channel_window_does_not_flag(self):
        """The complement: a spike still inside the window raises the bar.

        Bar 14 is only two bars after the bar-12 spike, so with ``n=5`` that
        spike is still in its channel. Closing at the same 110.0 is not
        strictly greater, so it is not a new breakout.
        """
        n = 5
        size = n + 40
        high = np.full(size, 100.0)
        low = np.full(size, 95.0)
        close = np.full(size, 99.0)
        for bar in (12, 14):
            close[bar] = 110.0
            high[bar] = 110.0
        flags = db.breakout_flags(high, low, close, n=n)
        assert flags[12]
        assert not flags[14]
        assert flags.sum() == 1

    def test_nothing_flags_during_the_warmup(self):
        rng = np.random.default_rng(1)
        close = 100.0 + np.cumsum(rng.normal(0, 5.0, 300))
        flags = db.breakout_flags(close + 1.0, close - 1.0, close)
        assert not flags[:db.CHANNEL_N + 1].any()

    def test_a_series_shorter_than_the_channel_produces_nothing(self):
        flags = db.breakout_flags(np.full(10, 1.0), np.full(10, 1.0),
                                  np.full(10, 1.0), n=55)
        assert flags.shape == (10,)
        assert not flags.any()

    def test_it_is_deterministic(self):
        rng = np.random.default_rng(7)
        close = 100.0 + np.cumsum(rng.normal(0, 2.0, 400))
        high, low = close + 1.0, close - 1.0
        first = db.breakout_flags(high, low, close)
        second = db.breakout_flags(high, low, close)
        assert np.array_equal(first, second)

    def test_it_never_flags_on_a_nan_channel(self):
        """Fail closed: an undefined channel is not a breakout."""
        high = np.full(80, 100.0)
        low = np.full(80, 95.0)
        close = np.full(80, 200.0)            # far above everything
        flags = db.breakout_flags(high, low, close, n=db.CHANNEL_N)
        assert not flags[:db.CHANNEL_N + 1].any()


# ---------------------------------------------------------------------------
# the adapter, and compatibility with the validated instrument
# ---------------------------------------------------------------------------

class TestInstrumentCompatibility:

    class _Bar:
        def __init__(self, h, l, c):
            self.high, self.low, self.close = h, l, c

    def _bars(self, n=300, seed=3):
        rng = np.random.default_rng(seed)
        close = 100.0 + np.cumsum(rng.normal(0.05, 2.0, n))
        return [self._Bar(c + 1.0, c - 1.0, c) for c in close]

    def test_flags_from_bars_matches_the_array_path(self):
        bars = self._bars()
        high = np.array([b.high for b in bars])
        low = np.array([b.low for b in bars])
        close = np.array([b.close for b in bars])
        assert np.array_equal(db.flags_from_bars(bars),
                              db.breakout_flags(high, low, close))

    def test_the_caller_warmup_is_applied_on_top_of_the_channel_warmup(self):
        bars = self._bars()
        flags = db.flags_from_bars(bars, warmup=200)
        assert not flags[:200].any()

    def test_one_trade_per_run_gives_one_entry_per_flag(self):
        """Every flag is an isolated event, so entries == flags at lock-up 1.

        This is what makes the signal compatible with the validated schedule:
        `simulate_schedule` takes at most one entry per contiguous run, and
        this signal's runs are all length 1 by construction.
        """
        import skill_test as sk
        bars = self._bars(n=600, seed=11)
        flags = db.flags_from_bars(bars, warmup=db.CHANNEL_N + 1)
        runs, _gaps = sk.extract_blocks(flags, warmup=db.CHANNEL_N + 1)
        assert all(length == 1 for _start, length in runs)
        entries = sk.simulate_schedule(flags, warmup=db.CHANNEL_N + 1,
                                       lockup=db.LOCKUP)
        assert entries == [start for start, _ in runs]

    def test_the_signal_module_computes_no_R_arithmetic(self):
        """Flags only. Scoring belongs to the validated instrument.

        A forked copy of the barrier maths that drifted by one line would make
        this measurement incomparable with everything already in EDGE.md.
        """
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(db))
        defined = {node.name for node in ast.walk(tree)
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        forbidden = {"barrier_r_for_all_bars", "simulate_schedule",
                     "shape_matched_flags", "wilder_atr", "score_schedule"}
        assert not (defined & forbidden)
        source = inspect.getsource(db)
        assert "net_r" not in source and "round_trip" not in source.lower().replace(
            "round_trip_bps", "")


class TestEdgeMeasurementSelector:
    """`--signal` must reach the flags, and must default to the CLOSED path."""

    def test_the_default_is_the_closed_analyser(self):
        import inspect
        import edge_measurement as em
        source = inspect.getsource(em.main)
        assert 'default="closed_analyser"' in source

    def test_both_choices_are_offered(self):
        """Slice 35 added a third choice; this one still must be offered.

        Asserted per-name rather than on the whole tuple literal, so adding a
        future signal does not require editing an unrelated test — while still
        failing if `donchian_breakout_v1` is ever quietly dropped.
        """
        import inspect
        import edge_measurement as em
        source = inspect.getsource(em.main)
        assert '"closed_analyser"' in source
        assert '"donchian_breakout_v1"' in source
        assert 'choices=(' in source

    def test_the_closed_path_still_calls_the_original_analyser(self):
        import inspect
        import edge_measurement as em
        source = inspect.getsource(em.main)
        assert "sk.signal_flags(bars, cfg, warmup=warmup)" in source

    def test_the_new_path_calls_the_new_signal(self):
        import inspect
        import edge_measurement as em
        source = inspect.getsource(em.main)
        assert "donchian.flags_from_bars(bars, warmup=warmup)" in source

    def test_the_bars_are_unchanged_by_adding_a_signal_selector(self):
        import edge_measurement as em
        assert em.M1_PERCENTILE_BAR == 95.0
        assert em.M2_PERCENTILE_BAR == 95.0
