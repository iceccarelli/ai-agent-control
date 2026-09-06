"""Tests for `ts_momentum_v1` — the slice-50 time-series momentum signal.

The rule is one division and a sign, which leaves very little room to be wrong
and exactly three ways to be wrong invisibly:

* slide the window by one bar — `close[t]/close[t-10]` instead of
  `close[t-1]/close[t-11]` — and the decision bar's own close enters the
  trigger. The result would still be a plausible momentum signal, measured with
  a one-bar lookahead;
* drop the `SKIP`, and the same thing happens more subtly;
* invert the sign, and it becomes a mean-reversion family wearing a momentum
  name — the flip `docs/RESEARCH_HOLD.md` forbids being made after a loss.

Each gets a decisive test against hand-computed arithmetic. So does the nearest
frozen neighbour, `donchian_breakout_v1`: fixtures where each fires and the
other does not, in both directions, because an argument that two triggers
differ is not evidence that they do.
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

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import backtest as bt  # noqa: E402
import skill_test as sk  # noqa: E402
from signals import ts_momentum_v1 as ts  # noqa: E402

DAY_MS = 86_400_000
EPOCH = int(dt.datetime(2022, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)


def bar(day, close, *, high=None, low=None, open_=None, vol=100.0):
    o = close if open_ is None else open_
    h = max(o, close) * 1.001 if high is None else high
    l = min(o, close) * 0.999 if low is None else low
    return bt.Bar(EPOCH + day * DAY_MS, o, h, l, close, vol)


def series(closes, *, start=0):
    """Bars whose closes are exactly the numbers given.

    Opens equal closes, so nothing about the fixture depends on intrabar shape
    and every assertion about `ret` is arithmetic the reader can do by hand.
    """
    return [bar(start + i, c, open_=c) for i, c in enumerate(closes)]


def drift(n, *, per_bar, base=100.0, start=0):
    return series([base * (1.0 + per_bar) ** i for i in range(n)], start=start)


# ---------------------------------------------------------------------------
# the constants
# ---------------------------------------------------------------------------


class TestTheConstantsAreTheIntakes:
    """Frozen in NEW_SIGNAL_INTAKE.md and EDGE.md §31b before any number."""

    def test_every_declared_constant(self):
        assert ts.LOOKBACK == 10
        assert ts.SKIP == 1
        assert ts.ATR_PERIOD == 14
        assert ts.STOP_ATR == 1.5
        assert ts.TAKE_PROFIT_R == 2.0
        assert ts.HORIZON == 5
        assert ts.LOCKUP == 1
        assert ts.ROUND_TRIP_BPS == 25.0
        assert ts.ENTRY_ON == "next_open"
        assert ts.NAME == "ts_momentum_v1"

    def test_two_r_of_take_profit_is_two_stop_distances(self):
        """The barrier's payoff is take_profit_atr / stop_atr.

        The intake fixes the STOP at 1.5 ATR and the target at 2.0 R, so the
        take-profit is 3.0 ATR. Writing 2.0 would give a payoff of 1.33 and
        quietly change the target — the trap slice 43 walked into and the
        reason this is asserted in every signal's suite since.
        """
        assert ts.TAKE_PROFIT_ATR == 3.0
        assert ts.TAKE_PROFIT_ATR / ts.STOP_ATR == pytest.approx(2.0)

    def test_the_target_is_not_a_fades(self):
        """A 2 R target is what makes this a momentum geometry.

        All three frozen fades target 1.0 R. Copying one would leave the module
        looking correct and measuring a different trade.
        """
        from signals import post_shock_fade_v1 as shock
        from signals import range_location_fade_v1 as rl
        from signals import open_gap_fade_v1 as og
        assert ts.TAKE_PROFIT_R == 2.0
        for other in (shock, rl, og):
            assert other.TAKE_PROFIT_R == 1.0
            assert ts.TAKE_PROFIT_ATR != other.TAKE_PROFIT_ATR

    def test_the_horizon_is_five(self):
        from signals import post_shock_fade_v1 as shock
        from signals import open_gap_fade_v1 as og
        assert ts.HORIZON == 5
        assert ts.HORIZON != shock.HORIZON     # 3
        assert ts.HORIZON != og.HORIZON        # 4


# ---------------------------------------------------------------------------
# the window — exactly the intake's indices
# ---------------------------------------------------------------------------


class TestTheWindowIsTheIntakes:
    """`close[t-11] .. close[t-1]` for LOOKBACK=10, SKIP=1.

    The defect this guards against is a window slid by one bar. It would not
    raise, would not look wrong, and would put the decision bar's own close
    inside the trigger — a one-bar lookahead dressed as momentum.
    """

    def test_the_bounds_are_t_minus_eleven_and_t_minus_one(self):
        assert ts.window_bounds(11) == (0, 10)
        assert ts.window_bounds(100) == (89, 99)
        start, end = ts.window_bounds(50)
        assert end == 50 - ts.SKIP
        assert start == 50 - ts.SKIP - ts.LOOKBACK
        assert end - start == ts.LOOKBACK

    def test_the_ratio_uses_exactly_those_two_closes(self):
        """Decisive, against arithmetic done by hand.

        Every close is distinct, so a window off by one bar in either direction
        gives a different number rather than the same one by luck.
        """
        closes = [100.0 + i for i in range(30)]
        bars = series(closes)
        ret = ts.momentum_returns(bars)
        for t in (11, 15, 20, 29):
            start, end = ts.window_bounds(t)
            assert ret[t] == pytest.approx(closes[end] / closes[start] - 1.0), t

    def test_bar_t_s_own_close_is_not_in_the_window(self):
        """The lookahead test, stated as an experiment.

        Bar 20's close is moved to an extreme value. `ret[20]` must not change
        at all — it is computed from bars 9 and 19. `ret[21]` and `ret[30]`
        may change, and one of them must, or the fixture proves nothing.
        """
        closes = [100.0 + i for i in range(35)]
        before = ts.momentum_returns(series(closes))
        closes[20] = 10_000.0
        after = ts.momentum_returns(series(closes))
        assert after[20] == before[20]
        assert after[21] != before[21]

    def test_the_window_is_undefined_before_it_is_full(self):
        bars = series([100.0 + i for i in range(20)])
        ret = ts.momentum_returns(bars)
        for t in range(ts.FIRST_DEFINED):
            assert np.isnan(ret[t]), t
        assert np.isfinite(ret[ts.FIRST_DEFINED])
        assert ts.FIRST_DEFINED == ts.SKIP + ts.LOOKBACK == 11

    def test_a_series_shorter_than_the_window_yields_nothing(self):
        assert ts.momentum_setups(series([100.0] * 5)) == {}
        assert np.isnan(ts.momentum_returns(series([100.0] * 11))).all()

    def test_a_non_positive_denominator_is_refused(self):
        """A close of zero is not a price, and its sign means nothing."""
        closes = [100.0] * 30
        closes[5] = 0.0
        ret = ts.momentum_returns(series(closes))
        assert np.isnan(ret[5 + ts.FIRST_DEFINED])


# ---------------------------------------------------------------------------
# direction — continuation, and the zero branch
# ---------------------------------------------------------------------------


class TestTheDirectionFollowsTheReturn:
    """Invert this and it is a mean-reversion family with a momentum name.

    That flip is exactly what `docs/RESEARCH_HOLD.md` forbids being made after
    a loss, so it is pinned in both directions and cannot arrive as a tidy-up.
    """

    def test_a_rising_window_is_a_long(self):
        bars = drift(30, per_bar=0.01)
        setups = ts.momentum_setups(bars)
        assert setups[20] == ts.LONG_SETUP
        assert set(setups.values()) == {ts.LONG_SETUP}

    def test_a_falling_window_is_a_short(self):
        bars = drift(30, per_bar=-0.01)
        setups = ts.momentum_setups(bars)
        assert setups[20] == ts.SHORT_SETUP
        assert set(setups.values()) == {ts.SHORT_SETUP}

    def test_it_is_not_the_fade(self):
        """Stated as its own test because the sign is the whole hypothesis."""
        rising = ts.momentum_setups(drift(30, per_bar=0.01))[25]
        falling = ts.momentum_setups(drift(30, per_bar=-0.01))[25]
        assert rising == ts.LONG_SETUP
        assert falling == ts.SHORT_SETUP
        assert rising != falling

    def test_an_exactly_flat_window_produces_no_setup(self):
        """`ret == 0` is a real branch on these corpora, not a formality.

        Between 17% and 51% of bars in the eligible corpora have an open
        exactly equal to the prior close, so exact float equality between two
        closes is not the impossibility it would be on a venue that closes.
        """
        bars = series([100.0] * 30)
        ret = ts.momentum_returns(bars)
        assert ret[20] == 0.0
        assert ts.momentum_setups(bars) == {}

    def test_a_window_that_returns_to_its_start_produces_no_setup(self):
        """Up then exactly back down: the interior moved, the endpoints did not."""
        closes = [100.0] * 12 + [120.0, 140.0, 160.0, 140.0, 120.0, 100.0] + \
            [100.0] * 12
        bars = series(closes)
        ret = ts.momentum_returns(bars)
        t = 18 + ts.SKIP          # window ends at index 18, which closes at 100
        assert ts.window_bounds(t)[1] == 18
        assert ret[t] == 0.0
        assert t not in ts.momentum_setups(bars)

    def test_the_directions_are_the_only_two_values(self):
        rng = np.random.default_rng(3)
        price, closes = 100.0, []
        for _ in range(400):
            price *= float(np.exp(rng.normal(0, 0.03)))
            closes.append(price)
        directions = set(ts.momentum_setups(series(closes)).values())
        assert directions == {ts.LONG_SETUP, ts.SHORT_SETUP}


# ---------------------------------------------------------------------------
# the barrier geometry the intake declared
# ---------------------------------------------------------------------------


class TestTheGeometryIsWiredToTheSharedBarrier:

    def test_the_fill_is_the_next_bars_open(self):
        """Checked against the shared barrier, not asserted about it."""
        closes = [100.0 + i for i in range(40)]
        bars = series(closes)
        assert ts.ENTRY_ON == "next_open"
        indices, net, used = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=ts.TAKE_PROFIT_ATR, stop_atr=ts.STOP_ATR,
            horizon=ts.HORIZON, atr_period=ts.ATR_PERIOD,
            round_trip_bps=ts.ROUND_TRIP_BPS, side="long",
            entry_on=ts.ENTRY_ON)
        assert indices.size > 0
        assert net.size == indices.size
        # the time stop lands on entry_bar + horizon, never further
        assert int(used.max()) <= ts.HORIZON

    def test_the_horizon_embargo_is_five_bars_of_tail(self):
        """The last bars cannot resolve, so they are not scoreable."""
        bars = series([100.0 + i for i in range(60)])
        indices, _net, _used = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=ts.TAKE_PROFIT_ATR, stop_atr=ts.STOP_ATR,
            horizon=ts.HORIZON, atr_period=ts.ATR_PERIOD,
            round_trip_bps=ts.ROUND_TRIP_BPS, side="long",
            entry_on=ts.ENTRY_ON)
        assert int(indices.max()) < len(bars) - ts.HORIZON - 1

    def test_the_two_sides_are_scored_by_the_same_function(self):
        """Side symmetry is the intake's requirement, not an option."""
        bars = series([100.0 + i for i in range(60)])
        kwargs = dict(take_profit_atr=ts.TAKE_PROFIT_ATR,
                      stop_atr=ts.STOP_ATR, horizon=ts.HORIZON,
                      atr_period=ts.ATR_PERIOD,
                      round_trip_bps=ts.ROUND_TRIP_BPS, entry_on=ts.ENTRY_ON)
        long_idx, _l, _lu = sk.barrier_r_for_all_bars(bars, side="long",
                                                      **kwargs)
        short_idx, _s, _su = sk.barrier_r_for_all_bars(bars, side="short",
                                                       **kwargs)
        assert long_idx.tolist() == short_idx.tolist()


# ---------------------------------------------------------------------------
# material difference — fixtures where the neighbours disagree
# ---------------------------------------------------------------------------


class TestItIsNotDonchianBreakout:
    """The nearest neighbour: also continuation, also the symbol's own price.

    The distinction is that Donchian needs a **break** of an extreme and this
    needs only a **sign**. Both directions of disagreement are built, because
    either alone would be consistent with one rule nesting inside the other.
    """

    def test_a_steady_decline_fires_momentum_and_never_donchian(self):
        """A monotonic decline: momentum is SHORT throughout, Donchian silent.

        Airtight rather than merely likely — Donchian breaks a trailing N-day
        HIGH, and a series that never rises can never make one. Momentum has a
        negative windowed return on every defined bar.
        """
        from signals import donchian_breakout_v1 as don
        bars = drift(120, per_bar=-0.005)
        setups = ts.momentum_setups(bars)
        assert len(setups) > 100
        assert set(setups.values()) == {ts.SHORT_SETUP}
        assert not don.flags_from_bars(bars, warmup=0).any()

    def test_a_break_after_a_flat_window_fires_only_donchian(self):
        """Ten flat bars, then a spike to a new high.

        Donchian breaks. Momentum's window ends one bar before the decision
        bar and spans a period whose endpoints are identical, so `ret` is
        exactly zero and there is no setup.
        """
        from signals import donchian_breakout_v1 as don
        n = don.CHANNEL_N
        closes = [100.0] * (n + 15) + [180.0]
        bars = series(closes)
        spike = len(closes) - 1
        flags = don.flags_from_bars(bars, warmup=0)
        assert flags[spike], "fixture must break the channel"
        # at the spike bar the momentum window is [spike-11 .. spike-1],
        # entirely inside the flat region
        assert ts.momentum_returns(bars)[spike] == 0.0
        assert spike not in ts.momentum_setups(bars)

    def test_the_two_select_incomparable_samples_on_random_data(self):
        """Stated as what is actually true, not as a tidier claim.

        On continuous random data every windowed return is non-zero, so
        momentum has a setup on essentially every defined bar and Donchian's
        rare breaks fall inside that set. The flag sets therefore DO nest, and
        pretending otherwise would be a test written to pass.

        What makes the families materially different is what they select and
        which way they point: momentum flags two orders of magnitude more bars,
        and half of them SHORT — a direction Donchian cannot express at all.
        The genuine disagreements are the two constructed fixtures above, where
        an exactly flat window separates them in the other direction.
        """
        from signals import donchian_breakout_v1 as don
        rng = np.random.default_rng(19)
        price, closes = 100.0, []
        for _ in range(1500):
            price *= float(np.exp(rng.normal(0, 0.02)))
            closes.append(price)
        bars = series(closes)
        mine = ts.momentum_setups(bars)
        theirs = set(np.nonzero(don.flags_from_bars(bars, warmup=0))[0].tolist())
        assert mine and theirs
        assert not set(mine) <= theirs
        assert len(mine) > 20 * len(theirs)
        shorts = [t for t, d in mine.items() if d == ts.SHORT_SETUP]
        assert len(shorts) > len(mine) // 4
        assert don.NAME == "donchian_breakout_v1"


class TestItIsNotAnyOfTheThreeFades:

    def test_the_same_shock_points_the_opposite_way_from_post_shock(self):
        """Decisive on direction: one fades the move, the other rides it.

        A calm decline of 0.2% a bar — far below a 2-ATR shock — then a single
        8% down bar. `post_shock_fade_v1` fades it and goes LONG. Momentum
        reads the calm decline in its window and goes SHORT. Same bar, same
        asset, opposite sides.
        """
        from signals import post_shock_fade_v1 as shock
        closes = [100.0 * (0.998 ** i) for i in range(30)]
        closes.append(closes[-1] * 0.92)
        closes += [closes[-1] * (0.998 ** i) for i in range(1, 10)]
        bars = [bar(i, c, open_=c, high=c * 1.01, low=c * 0.99)
                for i, c in enumerate(closes)]
        shocked = 30
        theirs = shock.shock_setups(bars)
        assert shocked in theirs, f"fixture must fire the neighbour: {theirs}"
        mine = ts.momentum_setups(bars)
        assert shocked in mine
        assert theirs[shocked] == shock.LONG_SETUP    # fades the fall
        assert mine[shocked] == ts.SHORT_SETUP        # rides it

    def test_it_does_not_read_a_range_or_a_location(self):
        """Same honesty as the Donchian case: momentum's set nests theirs.

        The separation that matters is directional, and it is checkable: at the
        bars where both fire, `range_location_fade_v1` fades the extreme and
        momentum rides the return that produced it, so the two must disagree on
        direction far more often than they agree.
        """
        from signals import range_location_fade_v1 as rl
        rng = np.random.default_rng(29)
        price, closes = 100.0, []
        for _ in range(1200):
            price *= float(np.exp(rng.normal(0, 0.02)))
            closes.append(price)
        bars = series(closes)
        mine = ts.momentum_setups(bars)
        theirs = rl.location_setups(bars)
        assert mine and theirs
        assert not set(mine) <= set(theirs)
        shared = [t for t in theirs if t in mine]
        assert len(shared) > 100
        same = {ts.LONG_SETUP: rl.LONG_SETUP, ts.SHORT_SETUP: rl.SHORT_SETUP}
        agree = sum(1 for t in shared if same[mine[t]] == theirs[t])
        # 27 of 433 on this fixture. A fade and a continuation reading the
        # same move must point opposite ways almost always; the residual
        # agreement is bars where the trailing range and the 10-bar return
        # happen to disagree with each other.
        assert agree < len(shared) // 5, (agree, len(shared))

    def test_it_never_reads_an_open(self):
        """Structural: the slice-49 corpus fact cannot touch this trigger.

        `open_gap_fade_v1` died on a corpus where `open[t] == close[t-1]`. This
        module must not read an open at all — asserted by moving every open far
        away and checking that not one decision changes.
        """
        closes = [100.0 + i for i in range(60)]
        plain = ts.momentum_setups(series(closes))
        mangled = ts.momentum_setups(
            [bar(i, c, open_=c * 5.0, high=c * 6.0, low=c * 0.5)
             for i, c in enumerate(closes)])
        assert plain == mangled
        tree = ast.parse(inspect.getsource(ts))
        attributes = {n.attr for n in ast.walk(tree)
                      if isinstance(n, ast.Attribute)}
        assert "open" not in attributes
        assert "high" not in attributes and "low" not in attributes

    def test_it_reads_no_other_instrument(self):
        """Structural, not textual: no second price series can reach it."""
        for name in ("momentum_returns", "momentum_setups",
                     "directed_signal_bars", "flags_and_directions",
                     "summary"):
            signature = inspect.signature(getattr(ts, name))
            positional = [p for p in signature.parameters.values()
                          if p.kind is not p.KEYWORD_ONLY]
            assert [p.name for p in positional] == ["bars"], (name, positional)

        tree = ast.parse(inspect.getsource(ts))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        assert not any(name.startswith("signals") for name in imported), imported


# ---------------------------------------------------------------------------
# no lookahead
# ---------------------------------------------------------------------------


class TestNoLookahead:

    def test_truncating_the_future_does_not_change_the_past(self):
        rng = np.random.default_rng(13)
        price, closes = 100.0, []
        for _ in range(300):
            price *= float(np.exp(rng.normal(0, 0.02)))
            closes.append(price)
        bars = series(closes)
        full = ts.momentum_setups(bars)
        for cut in (120, 200, 280):
            prefix = ts.momentum_setups(bars[:cut])
            for index, direction in prefix.items():
                assert full.get(index) == direction, (cut, index)

    def test_the_returns_themselves_are_bit_stable_under_truncation(self):
        """Stronger than the setup check: the ratio must not move at all.

        A setup-level check passes even if the value drifts, as long as the
        drift never crosses zero.
        """
        rng = np.random.default_rng(14)
        price, closes = 100.0, []
        for _ in range(300):
            price *= float(np.exp(rng.normal(0, 0.02)))
            closes.append(price)
        bars = series(closes)
        full = ts.momentum_returns(bars)
        for cut in (150, 250):
            prefix = ts.momentum_returns(bars[:cut])
            for t in range(prefix.size):
                if np.isnan(prefix[t]):
                    assert np.isnan(full[t]), t
                else:
                    assert prefix[t] == full[t], t     # identical bits

    def test_the_last_bar_never_carries_a_setup(self):
        bars = drift(30, per_bar=0.01)
        directed = ts.directed_signal_bars(bars)
        assert all(index < len(bars) - 1 for index, _d in directed)

    def test_warmup_bars_are_excluded(self):
        bars = drift(80, per_bar=0.01)
        assert 30 in dict(ts.directed_signal_bars(bars, warmup=0))
        assert 30 not in dict(ts.directed_signal_bars(bars, warmup=50))

    def test_the_trigger_never_indexes_a_forward_bar(self):
        """AST: no `x[i + k]`. The fill is the barrier's job."""
        tree = ast.parse(inspect.getsource(ts))
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
    """Every R in this project comes from one function. This adds no second one."""

    def test_the_module_computes_no_barrier_of_its_own(self):
        tree = ast.parse(inspect.getsource(ts))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for forbidden in ("tp_level", "sl_level", "realised", "payoff",
                          "barrier_r", "stop_level", "take_profit_level",
                          "net_r", "atr"):
            assert forbidden not in names, forbidden

    def test_no_cost_arithmetic_happens_here(self):
        tree = ast.parse(inspect.getsource(ts))
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp):
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                assert "ROUND_TRIP_BPS" not in names

    def test_the_geometry_constants_are_declared_and_never_applied_here(self):
        tree = ast.parse(inspect.getsource(ts))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                for constant in ("STOP_ATR", "TAKE_PROFIT_ATR", "ATR_PERIOD",
                                 "HORIZON"):
                    assert constant not in names, (node.name, constant)

    def test_the_module_computes_no_atr_at_all(self):
        """Unlike the fades, this trigger has no use for volatility.

        The stop distance is the barrier's business. A local ATR here would be
        a second implementation with nothing to check it against.
        """
        source = inspect.getsource(ts)
        assert "wilder_atr" not in source
        assert "true_range" not in source


# ---------------------------------------------------------------------------
# scheduling
# ---------------------------------------------------------------------------


class TestOneTradePerRun:

    def test_one_unbroken_run_of_flags_yields_exactly_one_entry(self):
        """The instrument's rule, pinned before it decides this slice.

        `simulate_schedule` gives **at most one entry per contiguous run of
        flags**, on the run's first bar (slice 17, and the reason the null is
        valid at all). Every prior family here fires on rare events, so its
        flags form many short runs and the rule is invisible.

        This signal is different in kind: it is a STATE, true on essentially
        every bar, so its flags form ONE run and the schedule contains ONE
        entry no matter how long the series is. That is a property of the pair
        (dense signal, event-shaped scheduler), not of the market, and it is
        asserted here so that the count finding in STEP 4 is a confirmation
        rather than a surprise.
        """
        bars = drift(60, per_bar=0.01)
        flags, _d = ts.flags_and_directions(bars)
        assert flags[20:50].all(), "the fixture should flag continuously"
        entries = sk.simulate_schedule(flags, warmup=0, lockup=ts.LOCKUP)
        assert len(entries) == 1
        assert entries[0] == ts.FIRST_DEFINED

    def test_a_flag_run_broken_by_a_flat_window_yields_two_entries(self):
        """The same rule from the other side: a break in the run restarts it."""
        rising = [100.0 * (1.01 ** i) for i in range(25)]
        flat = [rising[-1]] * 14
        falling = [flat[-1] * (0.99 ** i) for i in range(1, 26)]
        bars = series(rising + flat + falling)
        flags, _d = ts.flags_and_directions(bars)
        assert not flags.all(), "the flat stretch must break the run"
        entries = sk.simulate_schedule(flags, warmup=0, lockup=ts.LOCKUP)
        assert len(entries) >= 2

    def test_flags_and_directions_agree(self):
        bars = drift(40, per_bar=0.01)
        flags, directions = ts.flags_and_directions(bars)
        assert set(np.nonzero(flags)[0].tolist()) == set(directions)

    def test_the_summary_adds_up(self):
        closes = [100.0 * (1.01 ** i) for i in range(30)] + \
            [100.0 * (1.01 ** 29) * (0.99 ** i) for i in range(1, 30)]
        summary = ts.summary(series(closes))
        assert summary["setups"] == summary["long_setups"] + \
            summary["short_setups"]
        assert summary["long_setups"] > 0
        assert summary["short_setups"] > 0


# ---------------------------------------------------------------------------
# the slice-50 measurement record
# ---------------------------------------------------------------------------


class TestTheSlice50Artefact:
    """The record must agree with the repository, not with its own summary.

    For an INCONCLUSIVE family the specific exposure is a fabricated
    percentile: there is no M1 and no M2 here, so any number in those fields
    could only have been invented, and a later reader would have no way to tell.
    """

    PATH = os.path.join(REPO, "artifacts", "slice50_stage1_ts_momentum_v1.json")

    def _payload(self):
        with open(self.PATH, encoding="utf-8") as handle:
            return json.load(handle)

    def test_it_exists_and_is_valid_json(self):
        assert os.path.exists(self.PATH)
        assert self._payload()["schema"] == "stage1_measurement/1"

    def test_the_constants_are_the_modules(self):
        declared = self._payload()["constants"]
        assert declared["LOOKBACK"] == ts.LOOKBACK == 10
        assert declared["SKIP"] == ts.SKIP == 1
        assert declared["ATR_PERIOD"] == ts.ATR_PERIOD
        assert declared["STOP_ATR"] == ts.STOP_ATR
        assert declared["TAKE_PROFIT_R"] == ts.TAKE_PROFIT_R == 2.0
        assert declared["TAKE_PROFIT_ATR"] == ts.TAKE_PROFIT_ATR
        assert declared["HORIZON"] == ts.HORIZON == 5
        assert declared["LOCKUP"] == ts.LOCKUP
        assert declared["ROUND_TRIP_BPS"] == ts.ROUND_TRIP_BPS
        assert declared["ENTRY_ON"] == ts.ENTRY_ON

    def test_no_m1_or_m2_is_quoted_anywhere(self):
        """The decisive one. INCONCLUSIVE means no reading exists."""
        payload = self._payload()
        edge = payload["edge"]
        assert edge["runs_executed"] == 0
        assert edge["m1"] is None and edge["m2"] is None
        assert edge["n_trades"] is None and edge["mean_r"] is None
        assert edge["control_validated"] is False
        assert payload["edge_verdict"] == "INCONCLUSIVE"
        assert payload["positive_rule_met"] is False
        assert payload["cleared_edge_signal"] is None

    def test_the_counts_match_the_signal_module_on_the_real_corpora(self):
        """Recomputed from the corpora, not transcribed from the log."""
        import market_data as md
        claimed = self._payload()["count_finding"]["flagged_bars"]
        for symbol, directory in (("BTCUSD", "data/real_1d"),
                                  ("ETHUSDT", "data/real_multi_1d"),
                                  ("SOLUSDT", "data/real_multi_1d")):
            loaded, _b, _n = md.load_corpus(os.path.join(REPO, directory))
            bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
                    for b in loaded[symbol]]
            assert ts.summary(bars, warmup=200)["setups"] == claimed[symbol]

    def test_the_scheduled_entries_are_reproducible(self):
        """The number that actually decides the slice, recomputed here.

        The gap between 2,934 flagged bars and 1 scheduled entry is the whole
        finding, so it is recomputed through the same two functions the
        measurement path uses rather than trusted to the log.
        """
        import edge_measurement as em
        import market_data as md
        claimed = self._payload()["count_finding"]["entries"]
        for symbol, directory in (("BTCUSD", "data/real_1d"),
                                  ("ETHUSDT", "data/real_multi_1d"),
                                  ("SOLUSDT", "data/real_multi_1d")):
            loaded, _b, _n = md.load_corpus(os.path.join(REPO, directory))
            bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
                    for b in loaded[symbol]]
            flags, _d = ts.flags_and_directions(bars, warmup=200)
            long_idx, _n1, _u1 = sk.barrier_r_for_all_bars(
                bars, take_profit_atr=ts.TAKE_PROFIT_ATR, stop_atr=ts.STOP_ATR,
                horizon=ts.HORIZON, atr_period=ts.ATR_PERIOD,
                round_trip_bps=ts.ROUND_TRIP_BPS, side="long",
                entry_on=ts.ENTRY_ON)
            eligible = np.zeros(len(bars), dtype=bool)
            eligible[long_idx] = True
            eligible[:200] = False
            entries = sk.simulate_schedule(em.tradable_flags(flags, eligible),
                                           warmup=200, lockup=ts.LOCKUP)
            assert len(entries) == claimed[symbol], symbol

    def test_no_symbol_is_claimed_to_meet_the_gate(self):
        finding = self._payload()["count_finding"]
        assert finding["hard_gate_per_symbol"] == 50
        assert finding["symbols_meeting_gate"] == []
        assert all(n < 50 for n in finding["entries"].values())

    def test_the_thesis_is_recorded_as_not_rare(self):
        """The distinction from slice 49, which must not be blurred.

        `open_gap_fade_v1` had no events to find. This one has hundreds of sign
        flips and is collapsed by the scheduler instead. Recording it as "the
        signal rarely fires" would be false and would send the next intake
        looking in the wrong place.
        """
        finding = self._payload()["count_finding"]
        assert all(n > 200 for n in finding["sign_flips"].values())
        assert all(n > 1000 for n in finding["flagged_bars"].values())
        assert "STATE signal" in finding["cause"]
        assert "NOT rare" in finding["cause"]

    def test_every_control_is_recorded_invalid_with_its_log(self):
        control = self._payload()["control"]
        for symbol in ("BTCUSD", "ETHUSDT", "SOLUSDT"):
            entry = control[symbol]
            assert entry["verdict"] == "INVALID"
            assert entry["incomplete_pct"] == 100.0
            path = os.path.join(REPO, entry["log"])
            assert os.path.exists(path), entry["log"]
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            assert "CONTROL: **INVALID" in text
            assert "0 of 200" in text

    def test_the_slice49_failure_mode_is_recorded_as_not_recurring(self):
        """Checked against the two logs, not asserted.

        Slice 49's surrogates reported ZERO scoreable entries — the null had
        deleted the trigger. Slice 50's report ONE — the null preserved it and
        the scheduler collapsed it. Different components, different findings,
        and the difference is one character in a log line.
        """
        control = self._payload()["control"]
        assert control["slice49_failure_mode_recurred"] is False
        assert control["BTCUSD"]["typical_surrogate_entries"] == 1

        def first_surrogate_line(path):
            with open(os.path.join(REPO, path), encoding="utf-8") as handle:
                for line in handle:
                    if "surrogate 1:" in line:
                        return line
            raise AssertionError(path)

        old = first_surrogate_line(
            "artifacts/slice49_control_open_gap_fade_BTCUSD_n1000.log")
        new = first_surrogate_line(
            "artifacts/slice50_control_ts_momentum_BTCUSD_n1000.log")
        assert "(0 scoreable entries" in old
        assert "(1 scoreable entries" in new

    def test_the_control_failure_is_named_as_a_construction_mismatch(self):
        why = self._payload()["control"]["why_invalid"]
        assert "CONSTRUCTION MISMATCH" in why
        assert "NOT INSTRUMENT BIAS" in why
        assert "slice-46" in why or "slice 46" in why

    def test_the_scheduler_defect_is_recorded_and_not_repaired(self):
        """A scheduler changed after seeing the count it produced is fitted."""
        payload = self._payload()
        assert payload["scheduler_modified_after_seeing_counts"] is False
        note = payload["instrument_defect_recorded_not_repaired"]
        assert "must not be done in the slice that discovered" in note
        assert "human" in note and "pre-declaration" in note
        source = inspect.getsource(sk.simulate_schedule)
        assert "AT MOST ONE entry" in source, (
            "the defect this artefact describes must still be the code's "
            "actual behaviour, or the note is stale")

    def test_the_open_gap_freeze_is_recorded(self):
        """The artefact recorded 7 because that was true at slice 50.

        Slice 51 froze this family as the eighth, so the artefact is left
        alone and the invariant checked here is that nothing it recorded was
        released.
        """
        import project_status as ps
        payload = self._payload()
        assert payload["open_gap_fade_v1_frozen"] is True
        assert "open_gap_fade_v1" in ps.ABSENT_SIGNALS
        assert payload["frozen_absent_count"] == 7
        assert len(ps.ABSENT_SIGNALS) >= 7

    def test_it_claims_no_progress_toward_a_profit_agent(self):
        payload = self._payload()
        assert payload["closer_to_autonomous_profit_agent"] is False
        assert payload["live_authorized"] is False
        assert payload["models_current_present"] is False
        assert not os.path.exists(os.path.join(REPO, "models", "current"))

    def test_no_edge_summary_artefact_exists_for_this_family(self):
        artifacts = os.path.join(REPO, "artifacts")
        hits = [f for f in os.listdir(artifacts)
                if "ts_momentum" in f and "edge" in f]
        assert hits == [], hits

    def test_the_freeze_slice_50_deferred_has_since_happened(self):
        """Slice 50 left the freeze to a human; slice 51 is that human's call.

        The slice-50 artefact still says NOT PERFORMED, which was true when it
        was written. What this asserts is the pair: the record is unchanged,
        and the deferred decision has since been taken and is visible in code.
        """
        import project_status as ps
        assert "NOT PERFORMED" in self._payload()["freeze_of_this_family"]
        assert "ts_momentum_v1" in ps.ABSENT_SIGNALS
        assert ps.FROZEN_STATUS["ts_momentum_v1"] == "INCONCLUSIVE"
