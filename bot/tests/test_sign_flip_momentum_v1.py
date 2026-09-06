"""Tests for `sign_flip_momentum_v1` — the slice-52 flip-event momentum signal.

This signal shares its state variable and its window with the frozen
`ts_momentum_v1`, so the tests that matter are the ones that pin the *only*
thing that differs: which bars become setups.

Four ways to get the flip rule wrong while still producing a plausible sample:

* treat a zero-return bar as a state reset — every zero would make its successor
  a flip candidate, roughly doubling the sample on corpora that have
  exactly-equal closes;
* treat "sign differs from the previous BAR's sign" as the rule — a zero-return
  bar between two `+` bars would then read as two flips;
* fire on the first signed bar of the series — a boundary artefact, one free
  trade per symbol, and it would look exactly like a regime transition;
* invert the direction — a reversion family under a momentum name, which is the
  flip `docs/RESEARCH_HOLD.md` forbids being made after a loss.

Each gets a decisive test against a fixture whose signs can be read off by eye.
The relationship to `ts_momentum_v1` is asserted directly rather than argued:
strict subset, same directions where they overlap, materially fewer bars.
"""
from __future__ import annotations

import ast
import datetime as dt
import inspect
import json
import os
import sys

import numpy as np
import pytest

import registration_invariant

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import backtest as bt  # noqa: E402
import skill_test as sk  # noqa: E402
from signals import sign_flip_momentum_v1 as sf  # noqa: E402
from signals import ts_momentum_v1 as ts  # noqa: E402

DAY_MS = 86_400_000
EPOCH = int(dt.datetime(2022, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)


def bar(day, close, *, high=None, low=None, open_=None, vol=100.0):
    o = close if open_ is None else open_
    h = max(o, close) * 1.001 if high is None else high
    l = min(o, close) * 0.999 if low is None else low
    return bt.Bar(EPOCH + day * DAY_MS, o, h, l, close, vol)


def series(closes, *, start=0):
    """Bars whose closes are exactly the numbers given, opens equal to them."""
    return [bar(start + i, c, open_=c) for i, c in enumerate(closes)]


def leg(n, *, per_bar, base):
    """`n` closes compounding at `per_bar` from `base` (exclusive of base)."""
    return [base * (1.0 + per_bar) ** i for i in range(1, n + 1)]


def two_legs(up=25, down=25, *, rate=0.01, base=100.0):
    """A rising leg then a falling leg — exactly one flip, position computable.

    The windowed return turns negative once the window has moved far enough
    into the falling leg, so there is one `+ -> -` transition and no other.
    """
    rising = [base] + leg(up, per_bar=rate, base=base)
    falling = leg(down, per_bar=-3 * rate, base=rising[-1])
    return series(rising + falling)


# ---------------------------------------------------------------------------
# the constants
# ---------------------------------------------------------------------------


class TestTheConstantsAreTheIntakes:
    """Frozen in NEW_SIGNAL_INTAKE.md and EDGE.md §33b before any number."""

    def test_every_declared_constant(self):
        assert sf.LOOKBACK == 10
        assert sf.SKIP == 1
        assert sf.ATR_PERIOD == 14
        assert sf.STOP_ATR == 1.5
        assert sf.TAKE_PROFIT_R == 2.0
        assert sf.HORIZON == 5
        assert sf.LOCKUP == 1
        assert sf.ROUND_TRIP_BPS == 25.0
        assert sf.ENTRY_ON == "next_open"
        assert sf.NAME == "sign_flip_momentum_v1"

    def test_two_r_of_take_profit_is_two_stop_distances(self):
        """The barrier's payoff is take_profit_atr / stop_atr.

        The intake fixes the STOP at 1.5 ATR and the target at 2.0 R, so the
        take-profit is 3.0 ATR. Writing 2.0 would give a payoff of 1.33 and
        quietly change the target — the trap slice 43 walked into.
        """
        assert sf.TAKE_PROFIT_ATR == 3.0
        assert sf.TAKE_PROFIT_ATR / sf.STOP_ATR == pytest.approx(2.0)

    def test_the_scheduler_constant_is_the_instruments(self):
        """`LOCKUP` is not this signal's to change, and the intake says so.

        Slice 51 froze `ts_momentum_v1` partly to forbid altering
        one-trade-per-run after seeing a count shortfall. This family exists
        because the human reshaped the SIGNAL instead. If `LOCKUP` ever differs from
        the frozen family's, that repair happened by the back door.
        """
        assert sf.LOCKUP == ts.LOCKUP == 1

    def test_the_geometry_matches_the_intake_and_its_neighbour(self):
        """Same window and geometry as `ts_momentum_v1`, by design.

        The intake reuses them deliberately: the only declared difference is
        the event extraction, so a divergence here would mean this measures
        something else entirely while claiming to isolate the flip.
        """
        assert sf.LOOKBACK == ts.LOOKBACK
        assert sf.SKIP == ts.SKIP
        assert sf.HORIZON == ts.HORIZON
        assert sf.STOP_ATR == ts.STOP_ATR
        assert sf.TAKE_PROFIT_ATR == ts.TAKE_PROFIT_ATR
        assert sf.ENTRY_ON == ts.ENTRY_ON


# ---------------------------------------------------------------------------
# the window — identical to the intake's, and to its neighbour's
# ---------------------------------------------------------------------------


class TestTheWindowIsTheIntakes:

    def test_the_bounds_are_t_minus_eleven_and_t_minus_one(self):
        assert sf.window_bounds(11) == (0, 10)
        start, end = sf.window_bounds(50)
        assert end == 50 - sf.SKIP
        assert start == 50 - sf.SKIP - sf.LOOKBACK
        assert end - start == sf.LOOKBACK

    def test_the_ratio_uses_exactly_those_two_closes(self):
        closes = [100.0 + i for i in range(30)]
        ret = sf.momentum_returns(series(closes))
        for t in (11, 15, 20, 29):
            start, end = sf.window_bounds(t)
            assert ret[t] == pytest.approx(closes[end] / closes[start] - 1.0), t

    def test_the_returns_are_identical_to_the_frozen_familys(self):
        """Same state variable, verified bit for bit rather than assumed.

        If these ever diverged, this family would be measuring a different
        quantity while its design note claims the only difference is event
        extraction.
        """
        rng = np.random.default_rng(7)
        price, closes = 100.0, []
        for _ in range(400):
            price *= float(np.exp(rng.normal(0, 0.02)))
            closes.append(price)
        bars = series(closes)
        mine = sf.momentum_returns(bars)
        theirs = ts.momentum_returns(bars)
        assert np.array_equal(mine, theirs, equal_nan=True)

    def test_bar_t_s_own_close_is_not_in_the_window(self):
        closes = [100.0 + i for i in range(35)]
        before = sf.momentum_returns(series(closes))
        closes[20] = 10_000.0
        after = sf.momentum_returns(series(closes))
        assert after[20] == before[20]
        assert after[21] != before[21]

    def test_the_window_is_undefined_before_it_is_full(self):
        ret = sf.momentum_returns(series([100.0 + i for i in range(20)]))
        for t in range(sf.FIRST_DEFINED):
            assert np.isnan(ret[t]), t
        assert np.isfinite(ret[sf.FIRST_DEFINED])
        assert sf.FIRST_DEFINED == sf.SKIP + sf.LOOKBACK == 11

    def test_a_non_positive_denominator_is_refused(self):
        closes = [100.0] * 30
        closes[5] = 0.0
        ret = sf.momentum_returns(series(closes))
        assert np.isnan(ret[5 + sf.FIRST_DEFINED])
        assert sf.return_signs(series(closes))[5 + sf.FIRST_DEFINED] == 0


# ---------------------------------------------------------------------------
# the flip rule
# ---------------------------------------------------------------------------


class TestASetupIsOnlyAFlip:
    """The single thing that distinguishes this family. Pinned hard."""

    def test_a_persistent_sign_produces_no_setup_at_all(self):
        """Decisive. A monotone series has a sign on every bar and no flip.

        `ts_momentum_v1` fires on every one of those bars. This fires on none.
        """
        bars = series([100.0 * (1.01 ** i) for i in range(60)])
        assert sf.flip_setups(bars) == {}
        assert len(ts.momentum_setups(bars)) > 40

    def test_one_reversal_produces_exactly_one_setup(self):
        bars = two_legs()
        setups = sf.flip_setups(bars)
        assert len(setups) == 1
        index, direction = next(iter(setups.items()))
        assert direction == sf.SHORT_SETUP
        signs = sf.return_signs(bars)
        assert signs[index] == -1
        assert signs[index - 1] == 1          # the bar before is still positive

    def test_the_flip_is_at_the_first_bar_of_the_new_sign(self):
        """Not the last bar of the old one, and not one bar late."""
        bars = two_legs()
        signs = sf.return_signs(bars)
        first_negative = int(np.nonzero(signs == -1)[0][0])
        assert set(sf.flip_setups(bars)) == {first_negative}

    def test_two_reversals_produce_two_setups_pointing_opposite_ways(self):
        base = 100.0
        closes = [base] + leg(20, per_bar=0.01, base=base)
        closes += leg(20, per_bar=-0.03, base=closes[-1])
        closes += leg(25, per_bar=0.03, base=closes[-1])
        setups = sf.flip_setups(series(closes))
        assert len(setups) == 2
        directions = [d for _t, d in sorted(setups.items())]
        assert directions == [sf.SHORT_SETUP, sf.LONG_SETUP]

    def test_the_first_signed_bar_of_a_series_is_never_a_flip(self):
        """A boundary artefact worth one free trade per symbol if missed."""
        bars = series([100.0 * (1.01 ** i) for i in range(40)])
        signs = sf.return_signs(bars)
        first_signed = int(np.nonzero(signs != 0)[0][0])
        assert first_signed == sf.FIRST_DEFINED
        assert first_signed not in sf.flip_setups(bars)

    def test_flips_are_a_strict_subset_of_the_frozen_familys_flags(self):
        """The relationship stated in the design note, asserted not argued."""
        rng = np.random.default_rng(11)
        price, closes = 100.0, []
        for _ in range(1500):
            price *= float(np.exp(rng.normal(0, 0.02)))
            closes.append(price)
        bars = series(closes)
        mine = sf.flip_setups(bars)
        theirs = ts.momentum_setups(bars)
        assert mine
        assert set(mine) < set(theirs)                    # strict subset
        assert len(mine) < len(theirs) // 3               # materially sparser
        for t, direction in mine.items():
            assert theirs[t] == direction                 # same side on shared bars


class TestZerosAreTransparentNotResets:
    """A zero-return bar carries the previous sign across untouched.

    The failure this prevents: treating a zero as "no previous sign" would make
    the next signed bar a flip regardless of direction, and these corpora
    contain bars whose closes are exactly equal, so it would inflate the sample
    with events that never happened.
    """

    def test_the_previous_sign_walks_back_past_zeros(self):
        signs = np.array([0, 1, 0, 0, 0, 1, -1], dtype=np.int8)
        previous = sf.previous_signs(signs)
        assert previous.tolist() == [0, 0, 1, 1, 1, 1, 1]

    def test_a_zero_run_between_two_equal_signs_is_not_a_flip(self):
        """Decisive: `+ 0 0 0 +` must produce no setup."""
        base = 100.0
        rising = [base] + leg(12, per_bar=0.01, base=base)
        flat = [rising[-1]] * 12          # windowed return becomes exactly 0
        more = leg(14, per_bar=0.01, base=flat[-1])
        bars = series(rising + flat + more)
        signs = sf.return_signs(bars)
        assert 0 in signs.tolist() and 1 in signs.tolist()
        assert -1 not in signs.tolist(), "fixture must never go negative"
        assert sf.flip_setups(bars) == {}

    def test_a_zero_run_between_opposite_signs_is_exactly_one_flip(self):
        """`+ 0 0 -` is one event, at the `-`, not at either zero."""
        base = 100.0
        rising = [base] + leg(12, per_bar=0.01, base=base)
        flat = [rising[-1]] * 12
        falling = leg(20, per_bar=-0.03, base=flat[-1])
        bars = series(rising + flat + falling)
        signs = sf.return_signs(bars)
        setups = sf.flip_setups(bars)
        assert len(setups) == 1
        index = next(iter(setups))
        assert signs[index] == -1
        assert signs[index - 1] == 0, "the flip must land after the zero run"

    def test_a_zero_bar_is_never_itself_a_setup(self):
        rng = np.random.default_rng(5)
        closes, price = [], 100.0
        for i in range(400):
            price = price if i % 3 else price * float(np.exp(rng.normal(0, 0.02)))
            closes.append(price)
        bars = series(closes)
        signs = sf.return_signs(bars)
        for t in sf.flip_setups(bars):
            assert signs[t] != 0, t


class TestTheDirectionFollowsTheNewSign:
    """Continuation, not reversion. Pinned in both directions."""

    def test_a_flip_to_positive_is_a_long(self):
        base = 100.0
        closes = [base] + leg(20, per_bar=-0.02, base=base)
        closes += leg(25, per_bar=0.03, base=closes[-1])
        setups = sf.flip_setups(series(closes))
        assert len(setups) == 1
        assert next(iter(setups.values())) == sf.LONG_SETUP

    def test_a_flip_to_negative_is_a_short(self):
        setups = sf.flip_setups(two_legs())
        assert next(iter(setups.values())) == sf.SHORT_SETUP

    def test_the_direction_always_matches_the_sign_at_the_flip_bar(self):
        rng = np.random.default_rng(21)
        price, closes = 100.0, []
        for _ in range(1200):
            price *= float(np.exp(rng.normal(0, 0.03)))
            closes.append(price)
        bars = series(closes)
        signs = sf.return_signs(bars)
        setups = sf.flip_setups(bars)
        assert setups
        for t, direction in setups.items():
            expected = sf.LONG_SETUP if signs[t] > 0 else sf.SHORT_SETUP
            assert direction == expected, t


# ---------------------------------------------------------------------------
# the barrier geometry
# ---------------------------------------------------------------------------


class TestTheGeometryIsWiredToTheSharedBarrier:

    def test_the_fill_is_the_next_bars_open(self):
        bars = series([100.0 + i for i in range(60)])
        assert sf.ENTRY_ON == "next_open"
        indices, net, used = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=sf.TAKE_PROFIT_ATR, stop_atr=sf.STOP_ATR,
            horizon=sf.HORIZON, atr_period=sf.ATR_PERIOD,
            round_trip_bps=sf.ROUND_TRIP_BPS, side="long",
            entry_on=sf.ENTRY_ON)
        assert indices.size > 0 and net.size == indices.size
        assert int(used.max()) <= sf.HORIZON

    def test_the_horizon_embargo_is_five_bars_of_tail(self):
        bars = series([100.0 + i for i in range(60)])
        indices, _n, _u = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=sf.TAKE_PROFIT_ATR, stop_atr=sf.STOP_ATR,
            horizon=sf.HORIZON, atr_period=sf.ATR_PERIOD,
            round_trip_bps=sf.ROUND_TRIP_BPS, side="long",
            entry_on=sf.ENTRY_ON)
        assert int(indices.max()) < len(bars) - sf.HORIZON - 1

    def test_both_sides_are_scored_by_the_same_function(self):
        bars = series([100.0 + i for i in range(60)])
        kwargs = dict(take_profit_atr=sf.TAKE_PROFIT_ATR, stop_atr=sf.STOP_ATR,
                      horizon=sf.HORIZON, atr_period=sf.ATR_PERIOD,
                      round_trip_bps=sf.ROUND_TRIP_BPS, entry_on=sf.ENTRY_ON)
        long_idx, _l, _lu = sk.barrier_r_for_all_bars(bars, side="long", **kwargs)
        short_idx, _s, _su = sk.barrier_r_for_all_bars(bars, side="short", **kwargs)
        assert long_idx.tolist() == short_idx.tolist()


# ---------------------------------------------------------------------------
# material difference against the rest of the deny-list
# ---------------------------------------------------------------------------


class TestItIsNotTheOtherFrozenFamilies:

    def test_a_flip_can_occur_with_no_new_extreme(self):
        """Donchian needs a break. This does not."""
        from signals import donchian_breakout_v1 as don
        bars = two_legs()
        assert sf.flip_setups(bars)
        flags = don.flags_from_bars(bars, warmup=0)
        flip_index = next(iter(sf.flip_setups(bars)))
        assert not flags[flip_index], "the flip bar must not be a channel break"

    def test_it_never_reads_an_open_a_high_or_a_low(self):
        """Structural: the slice-49 corpus fact cannot touch this trigger."""
        closes = [100.0 + i for i in range(60)]
        plain = sf.flip_setups(series(closes))
        mangled = sf.flip_setups(
            [bar(i, c, open_=c * 5.0, high=c * 6.0, low=c * 0.5)
             for i, c in enumerate(closes)])
        assert plain == mangled
        tree = ast.parse(inspect.getsource(sf))
        attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert "open" not in attributes
        assert "high" not in attributes and "low" not in attributes

    def test_it_reads_no_other_instrument(self):
        for name in ("momentum_returns", "return_signs", "flip_setups",
                     "directed_signal_bars", "flags_and_directions", "summary"):
            signature = inspect.signature(getattr(sf, name))
            positional = [p for p in signature.parameters.values()
                          if p.kind is not p.KEYWORD_ONLY]
            assert [p.name for p in positional] == ["bars"], (name, positional)
        tree = ast.parse(inspect.getsource(sf))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        assert not any(name.startswith("signals") for name in imported), imported

    def test_it_uses_no_oscillator_no_channel_and_no_atr(self):
        source = inspect.getsource(sf)
        assert "wilder_atr" not in source
        assert "true_range" not in source
        tree = ast.parse(source)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for forbidden in ("rsi", "macd", "adx", "bollinger", "supertrend",
                          "channel", "donchian", "range_high", "range_low",
                          "atr"):
            assert forbidden not in names, forbidden


# ---------------------------------------------------------------------------
# no lookahead
# ---------------------------------------------------------------------------


class TestNoLookahead:

    def test_truncating_the_future_does_not_change_the_past(self):
        rng = np.random.default_rng(13)
        price, closes = 100.0, []
        for _ in range(400):
            price *= float(np.exp(rng.normal(0, 0.02)))
            closes.append(price)
        bars = series(closes)
        full = sf.flip_setups(bars)
        for cut in (150, 250, 350):
            prefix = sf.flip_setups(bars[:cut])
            for index, direction in prefix.items():
                assert full.get(index) == direction, (cut, index)

    def test_the_returns_are_bit_stable_under_truncation(self):
        rng = np.random.default_rng(14)
        price, closes = 100.0, []
        for _ in range(300):
            price *= float(np.exp(rng.normal(0, 0.02)))
            closes.append(price)
        bars = series(closes)
        full = sf.momentum_returns(bars)
        for cut in (150, 250):
            prefix = sf.momentum_returns(bars[:cut])
            for t in range(prefix.size):
                if np.isnan(prefix[t]):
                    assert np.isnan(full[t]), t
                else:
                    assert prefix[t] == full[t], t

    def test_the_flip_test_only_ever_looks_backwards(self):
        """`previous_signs` is a strict prefix scan, asserted directly."""
        signs = np.array([1, -1, 0, 1, 1, -1], dtype=np.int8)
        previous = sf.previous_signs(signs)
        for t in range(signs.size):
            earlier = [s for s in signs[:t] if s != 0]
            assert previous[t] == (earlier[-1] if earlier else 0), t

    def test_the_last_bar_never_carries_a_setup(self):
        bars = two_legs()
        assert all(i < len(bars) - 1 for i, _d in sf.directed_signal_bars(bars))

    def test_warmup_bars_are_excluded(self):
        bars = two_legs(up=40, down=40)
        flip = next(iter(sf.flip_setups(bars)))
        assert flip in dict(sf.directed_signal_bars(bars, warmup=0))
        assert flip not in dict(sf.directed_signal_bars(bars, warmup=flip + 1))

    def test_the_trigger_never_indexes_a_forward_bar(self):
        tree = ast.parse(inspect.getsource(sf))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript):
                continue
            if isinstance(node.slice, ast.BinOp) and \
                    isinstance(node.slice.op, ast.Add):
                raise AssertionError(ast.dump(node)[:160])


# ---------------------------------------------------------------------------
# no forked R arithmetic
# ---------------------------------------------------------------------------


class TestTheRArithmeticIsNotForked:

    def test_the_module_computes_no_barrier_of_its_own(self):
        tree = ast.parse(inspect.getsource(sf))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for forbidden in ("tp_level", "sl_level", "realised", "payoff",
                          "barrier_r", "stop_level", "take_profit_level",
                          "net_r"):
            assert forbidden not in names, forbidden

    def test_no_cost_arithmetic_happens_here(self):
        tree = ast.parse(inspect.getsource(sf))
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp):
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                assert "ROUND_TRIP_BPS" not in names

    def test_the_geometry_constants_are_declared_and_never_applied_here(self):
        tree = ast.parse(inspect.getsource(sf))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                for constant in ("STOP_ATR", "TAKE_PROFIT_ATR", "ATR_PERIOD",
                                 "HORIZON", "LOCKUP"):
                    assert constant not in names, (node.name, constant)


# ---------------------------------------------------------------------------
# scheduling — the question slice 50 was killed by
# ---------------------------------------------------------------------------


class TestTheFlagsAreEventShaped:
    """The whole reason this family exists. Asserted before the count is taken.

    `ts_momentum_v1` died because a state true on nearly every bar forms one
    contiguous flag run, and `simulate_schedule` takes one entry per run. A
    flip-event signal must instead produce many short runs.
    """

    def test_isolated_flips_become_separate_entries(self):
        base = 100.0
        closes = [base] + leg(20, per_bar=0.01, base=base)
        closes += leg(20, per_bar=-0.03, base=closes[-1])
        closes += leg(20, per_bar=0.03, base=closes[-1])
        closes += leg(20, per_bar=-0.03, base=closes[-1])
        bars = series(closes)
        flags, _d = sf.flags_and_directions(bars)
        runs, _gaps = sk.extract_blocks(flags, warmup=0)
        assert int(flags.sum()) == 3
        assert len(runs) == 3, "isolated flips must not merge into one run"
        entries = sk.simulate_schedule(flags, warmup=0, lockup=sf.LOCKUP)
        assert len(entries) == 3

    def test_the_frozen_family_collapses_where_this_one_does_not(self):
        """Side by side, on one fixture, with the instrument's own scheduler."""
        base = 100.0
        closes = [base] + leg(20, per_bar=0.01, base=base)
        closes += leg(20, per_bar=-0.03, base=closes[-1])
        closes += leg(20, per_bar=0.03, base=closes[-1])
        bars = series(closes)

        their_flags, _td = ts.flags_and_directions(bars)
        my_flags, _md = sf.flags_and_directions(bars)
        their_entries = sk.simulate_schedule(their_flags, warmup=0,
                                             lockup=ts.LOCKUP)
        my_entries = sk.simulate_schedule(my_flags, warmup=0, lockup=sf.LOCKUP)

        assert int(their_flags.sum()) > int(my_flags.sum())
        assert len(their_entries) == 1, "the state signal collapses to one run"
        assert len(my_entries) == 2, "the event signal keeps its two events"

    def test_adjacent_flips_do_merge_and_that_is_recorded(self):
        """The honest converse: alternating signs DO collapse.

        A series whose windowed return oscillates sign on consecutive bars
        produces consecutive flags, one run, one entry. This is the residual of
        slice 50's problem and it is asserted rather than hoped away — the
        count finding reports `flag_runs` next to `entries` for exactly this
        reason.
        """
        base = 100.0
        closes = [base] + leg(12, per_bar=0.02, base=base)
        # a saw-tooth that pushes the 10-bar windowed return across zero
        # on consecutive bars
        for i in range(20):
            closes += leg(1, per_bar=(-0.08 if i % 2 == 0 else 0.08),
                          base=closes[-1])
        bars = series(closes)
        flags, _d = sf.flags_and_directions(bars)
        runs, _gaps = sk.extract_blocks(flags, warmup=0)
        assert int(flags.sum()) > len(runs), "fixture must produce a merged run"
        entries = sk.simulate_schedule(flags, warmup=0, lockup=sf.LOCKUP)
        assert len(entries) < int(flags.sum())

    def test_flags_and_directions_agree(self):
        bars = two_legs()
        flags, directions = sf.flags_and_directions(bars)
        assert set(np.nonzero(flags)[0].tolist()) == set(directions)

    def test_the_summary_adds_up(self):
        base = 100.0
        closes = [base] + leg(20, per_bar=0.01, base=base)
        closes += leg(20, per_bar=-0.03, base=closes[-1])
        closes += leg(20, per_bar=0.03, base=closes[-1])
        summary = sf.summary(series(closes))
        assert summary["setups"] == summary["long_setups"] + \
            summary["short_setups"]
        assert summary["long_setups"] == 1
        assert summary["short_setups"] == 1


# ---------------------------------------------------------------------------
# the slice-52 measurement record
# ---------------------------------------------------------------------------


class TestTheSlice52Artefact:
    """The first fully interpretable three-symbol reading this repo has made.

    That makes the honesty risk different from the last three slices. There is
    no missing number to fabricate here — there are real percentiles, and the
    temptation runs the other way: ETH's 81.4 / 83.5 is the second-highest
    reading in the programme's history and is still a FAIL. The tests below
    exist so that it cannot quietly become "nearly cleared".
    """

    PATH = os.path.join(REPO, "artifacts",
                        "slice52_stage1_sign_flip_momentum_v1.json")

    def _payload(self):
        with open(self.PATH, encoding="utf-8") as handle:
            return json.load(handle)

    def test_it_exists_and_is_valid_json(self):
        assert os.path.exists(self.PATH)
        assert self._payload()["schema"] == "stage1_measurement/1"

    def test_the_constants_are_the_modules(self):
        declared = self._payload()["constants"]
        assert declared["LOOKBACK"] == sf.LOOKBACK == 10
        assert declared["SKIP"] == sf.SKIP == 1
        assert declared["STOP_ATR"] == sf.STOP_ATR
        assert declared["TAKE_PROFIT_R"] == sf.TAKE_PROFIT_R == 2.0
        assert declared["TAKE_PROFIT_ATR"] == sf.TAKE_PROFIT_ATR
        assert declared["HORIZON"] == sf.HORIZON == 5
        assert declared["LOCKUP"] == sf.LOCKUP
        assert declared["ROUND_TRIP_BPS"] == sf.ROUND_TRIP_BPS
        assert declared["ENTRY_ON"] == sf.ENTRY_ON

    def test_one_run_per_symbol_and_no_shopping(self):
        payload = self._payload()
        assert payload["runs_per_symbol"] == 1
        assert payload["constants_changed_after_seeing_numbers"] is False
        assert payload["grid_or_sweep_run"] is False
        assert payload["scheduler_modified"] is False

    def test_the_counts_are_reproducible_from_the_module(self):
        """Recomputed through the measurement path, not transcribed."""
        import edge_measurement as em
        import market_data as md
        claimed = self._payload()["count_finding"]["entries"]
        for symbol, directory in (("BTCUSD", "data/real_1d"),
                                  ("ETHUSDT", "data/real_multi_1d"),
                                  ("SOLUSDT", "data/real_multi_1d")):
            loaded, _b, _n = md.load_corpus(os.path.join(REPO, directory))
            bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
                    for b in loaded[symbol]]
            flags, _d = sf.flags_and_directions(bars, warmup=200)
            long_idx, _n1, _u1 = sk.barrier_r_for_all_bars(
                bars, take_profit_atr=sf.TAKE_PROFIT_ATR, stop_atr=sf.STOP_ATR,
                horizon=sf.HORIZON, atr_period=sf.ATR_PERIOD,
                round_trip_bps=sf.ROUND_TRIP_BPS, side="long",
                entry_on=sf.ENTRY_ON)
            eligible = np.zeros(len(bars), dtype=bool)
            eligible[long_idx] = True
            eligible[:200] = False
            entries = sk.simulate_schedule(em.tradable_flags(flags, eligible),
                                           warmup=200, lockup=sf.LOCKUP)
            assert len(entries) == claimed[symbol], symbol

    def test_every_symbol_cleared_the_trade_gate(self):
        finding = self._payload()["count_finding"]
        assert finding["hard_gate_per_symbol"] == 50
        assert sorted(finding["symbols_meeting_gate"]) == \
            ["BTCUSD", "ETHUSDT", "SOLUSDT"]
        assert all(n >= 100 for n in finding["entries"].values())

    def test_the_event_shape_worked(self):
        """The whole point of this family, checked as a number.

        `ts_momentum_v1` turned 2,934 flagged bars into 1 run. This turns 419
        into 290. If that ratio ever collapsed, this family would have
        inherited the problem it was designed around.
        """
        finding = self._payload()["count_finding"]
        for symbol in ("BTCUSD", "ETHUSDT", "SOLUSDT"):
            flagged = finding["flagged_bars"][symbol]
            runs = finding["flag_runs"][symbol]
            assert runs > 0.6 * flagged, (symbol, flagged, runs)
            assert finding["longest_run"][symbol] <= 10, symbol

    def test_every_control_is_recorded_valid_with_its_log(self):
        control = self._payload()["control"]
        assert control["all_valid"] is True
        for symbol in ("BTCUSD", "ETHUSDT", "SOLUSDT"):
            entry = control[symbol]
            assert entry["verdict"] == "VALID"
            assert abs(entry["z"]) < 1.96, symbol
            assert entry["ks_p"] >= 0.05, symbol
            assert entry["incomplete_pct"] <= 5.0, symbol
            assert entry["usable_surrogates"] == 200
            path = os.path.join(
                REPO, "artifacts",
                f"slice52_control_sign_flip_momentum_{symbol}_n1000.log")
            assert os.path.exists(path), symbol
            with open(path, encoding="utf-8") as handle:
                assert "CONTROL: **VALID**" in handle.read()

    def test_the_percentiles_match_the_summaries_they_came_from(self):
        """No transcription. The artefact is checked against the run output."""
        per = self._payload()["edge"]["per_symbol"]
        for symbol, claimed in per.items():
            with open(os.path.join(REPO, claimed["summary"]),
                      encoding="utf-8") as handle:
                summary = json.load(handle)
            assert summary["signal"] == sf.NAME
            assert summary["symbol"] == symbol
            assert summary["verdict"] == claimed["verdict"]
            assert summary["m1"]["percentile"] == claimed["m1"]
            assert summary["m2"]["percentile"] == claimed["m2"]
            assert summary["control_validated"] is True

    def test_no_symbol_cleared_either_bar(self):
        """The decisive one. 81.4 is a FAIL, and so is 83.5."""
        payload = self._payload()
        assert payload["symbols_clearing_both_bars"] == []
        for symbol, entry in payload["edge"]["per_symbol"].items():
            assert entry["verdict"] == "EDGE_EVIDENCE_ABSENT", symbol
            assert entry["m1"] < 95.0, symbol
            assert entry["m2"] < 95.0, symbol

    def test_the_positive_rule_is_the_intakes_and_is_not_met(self):
        payload = self._payload()
        assert payload["positive_rule_met"] is False
        assert payload["cleared_edge_signal"] is None
        rule = payload["positive_rule"].lower()
        assert "at least two" in rule
        assert "50 scored trades" in rule

    def test_absent_is_recorded_as_a_successful_measurement(self):
        """It is the outcome the instrument was built to be able to produce."""
        payload = self._payload()
        assert payload["edge_verdict"] == "ABSENT"
        assert payload["verdict_is_a_successful_measurement"] is True
        assert payload["edge"]["control_validated"] is True

    def test_it_claims_no_progress_toward_a_profit_agent(self):
        payload = self._payload()
        assert payload["closer_to_autonomous_profit_agent"] is False
        assert payload["live_authorized"] is False
        assert payload["models_current_present"] is False
        assert not os.path.exists(os.path.join(REPO, "models", "current"))

    def test_the_prior_eight_freezes_are_untouched(self):
        """The artefact recorded 8 because 8 was true at slice 52.

        Slice 53 froze this family as the ninth. The record stays as written;
        what must hold forever is that none of its eight was released.
        """
        import project_status as ps
        payload = self._payload()
        assert payload["frozen_absent_count"] == 8
        assert len(ps.ABSENT_SIGNALS) >= 8
        assert payload["bars"]["m1"] == ps.M1_BAR == 95.0
        assert payload["bars"]["m2"] == ps.M2_BAR == 95.0

    def test_the_freeze_slice_52_deferred_has_since_happened(self):
        """Slice 52 left the freeze to a human; slice 53 is that decision.

        The pair is what is asserted: the dated record still says NOT
        PERFORMED, which was true when it was written, and the deferred
        decision has since been taken and is visible in code as ABSENT — not
        INCONCLUSIVE, because this family has numbers.
        """
        import project_status as ps
        payload = self._payload()
        registration_invariant.assert_registration_is_sound(
            os.path.join(REPO, "artifacts"))
        assert payload["this_family_frozen"] is False
        assert "NOT PERFORMED" in payload["freeze_of_this_family"]
        assert "sign_flip_momentum_v1" in ps.ABSENT_SIGNALS
        assert ps.FROZEN_STATUS["sign_flip_momentum_v1"] == "ABSENT"

    def test_the_bias_hypothesis_note_is_recorded_as_support_not_proof(self):
        note = self._payload()["control"]["supports_bias_hypothesis"]
        assert "close-only" in note
        assert "49.05" in note and "3.632" in note
        assert "could have falsified it and did not" in note
