"""Tests for `open_gap_fade_v1` — the slice-49 open-gap fade.

The signal's whole content is one ratio taken across an index boundary, which
gives it two ways to be subtly wrong while still producing plausible numbers:

* let bar `t`'s own high or low reach `ATR_prev[t]` and the denominator knows
  about the move it is scaling — a large gap day would shrink its own gap;
* emit the setup at `t` instead of `t-1` and the shared barrier fills at
  `open[t+1]`, a full session after the gap the thesis is about, which would
  measure a different hypothesis under this signal's name.

Both get decisive tests. So do the two nearest frozen neighbours: a fixture
where a close-to-close shock fires and this does not, and one where this fires
and `range_location_fade_v1` does not — in both directions, because an argument
that two triggers differ is not evidence that they do.

The fixtures are built so the arithmetic is checkable by eye: a corridor of
identical bars has a constant true range, so Wilder ATR is exactly that constant
and every gap in ATR units can be written down before the code runs.
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
from signals import open_gap_fade_v1 as og  # noqa: E402

DAY_MS = 86_400_000
EPOCH = int(dt.datetime(2022, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)

#: The corridor's constant true range. Every bar spans [99, 101] and closes at
#: 100, so TR = max(2, |101-100|, |99-100|) = 2 on every bar and Wilder ATR is
#: exactly 2.0 from bar 14 onward. One ATR is therefore 2.0 price units and a
#: 0.75-ATR gap is exactly 1.5.
FLAT_ATR = 2.0
GAP_AT_K = og.GAP_K * FLAT_ATR          # 1.5 — exactly on the threshold


def bar(day, close, *, high=None, low=None, open_=None, vol=100.0):
    o = close if open_ is None else open_
    h = max(o, close) if high is None else high
    l = min(o, close) if low is None else low
    return bt.Bar(EPOCH + day * DAY_MS, o, h, l, close, vol)


def corridor(n, *, start=0, close=100.0, half=1.0):
    """`n` identical bars: open == close, high/low symmetric about it.

    Constant true range `2 * half`, so Wilder ATR is exactly `2 * half` once
    defined, and every assertion about a gap in ATR units is arithmetic rather
    than a guess about the implementation.
    """
    return [bar(start + i, close, high=close + half, low=close - half,
                open_=close) for i in range(n)]


def gap_bar(day, open_price, *, close=None, pad=1.0):
    """One bar that opens away from 100 and (by default) closes back at it."""
    c = 100.0 if close is None else close
    return bar(day, c, open_=open_price,
               high=max(open_price, c) + pad, low=min(open_price, c) - pad)


# ---------------------------------------------------------------------------
# the constants
# ---------------------------------------------------------------------------


class TestTheConstantsAreTheIntakes:
    """Frozen in NEW_SIGNAL_INTAKE.md and EDGE.md §30b before any number."""

    def test_every_declared_constant(self):
        assert og.GAP_K == 0.75
        assert og.ATR_PERIOD == 14
        assert og.STOP_ATR == 1.5
        assert og.TAKE_PROFIT_R == 1.0
        assert og.HORIZON == 4
        assert og.LOCKUP == 1
        assert og.ROUND_TRIP_BPS == 25.0
        assert og.ENTRY_ON == "next_open"
        assert og.NAME == "open_gap_fade_v1"

    def test_one_r_of_take_profit_is_one_stop_distance(self):
        """The barrier's payoff is take_profit_atr / stop_atr.

        The intake fixes the STOP at 1.5 ATR and the target at 1.0 R, so the
        take-profit is 1.5 ATR. Writing 1.0 would leave the payoff at 0.67 and
        quietly change the risk unit — the trap slice 43 walked into.
        """
        assert og.TAKE_PROFIT_ATR == 1.5
        assert og.TAKE_PROFIT_ATR / og.STOP_ATR == pytest.approx(1.0)

    def test_the_horizon_is_four_and_not_a_neighbours(self):
        """A constant copied from a neighbouring signal would be silent."""
        from signals import post_shock_fade_v1 as shock
        from signals import range_location_fade_v1 as rl
        assert og.HORIZON == 4
        assert og.HORIZON != shock.HORIZON        # 3
        assert og.HORIZON != rl.HORIZON           # 5

    def test_the_threshold_is_not_a_neighbours_either(self):
        from signals import post_shock_fade_v1 as shock
        assert og.GAP_K == 0.75
        assert og.GAP_K != shock.SHOCK_K          # 2.0


# ---------------------------------------------------------------------------
# the gap arithmetic — ATR reads bars <= t-1 only
# ---------------------------------------------------------------------------


class TestTheDenominatorCannotSeeBarT:
    """The defect that would let a big gap day shrink its own gap.

    If `ATR_prev[t]` included bar t, a violent opening would widen the very
    denominator that scales it, and the largest gaps would systematically read
    smaller than they are — biasing the sample toward mild events under a rule
    that claims to select extreme ones.
    """

    def test_the_corridor_atr_is_exactly_two(self):
        """The premise every other fixture in this file rests on."""
        bars = corridor(40)
        atr = og._wilder_atr(bars)
        assert np.isnan(atr[13])
        assert atr[14] == pytest.approx(FLAT_ATR)
        assert atr[39] == pytest.approx(FLAT_ATR)

    def test_a_violent_bar_does_not_scale_its_own_gap(self):
        """Decisive. Bar 20 opens 1.5 above and swings 20 wide.

        `ATR_prev[20]` is the corridor's 2.0, so the gap is exactly 0.75. Were
        bar 20's own 20-point range in the denominator, the ATR would jump to
        ~3.3 and the gap would read ~0.45 — below the threshold, and the setup
        would vanish. So this test fails loudly under that defect rather than
        producing a slightly different number.
        """
        bars = corridor(20) + [gap_bar(20, 100.0 + GAP_AT_K, pad=10.0)] + \
            corridor(5, start=21)
        gap = og.gap_values(bars)
        assert gap[20] == pytest.approx(og.GAP_K)
        assert 20 - 1 in og.gap_setups(bars)

    def test_the_gap_is_open_minus_prior_close_over_prior_atr(self):
        """Arithmetic, written out, on a value chosen not to be the threshold."""
        bars = corridor(20) + [gap_bar(20, 103.0)] + corridor(3, start=21)
        gap = og.gap_values(bars)
        assert gap[20] == pytest.approx((103.0 - 100.0) / FLAT_ATR)   # 1.5

    def test_the_first_bar_and_the_warmup_carry_no_gap(self):
        bars = corridor(20)
        gap = og.gap_values(bars)
        assert np.isnan(gap[0])
        for t in range(1, og.ATR_PERIOD + 1):
            assert np.isnan(gap[t]), t
        assert np.isfinite(gap[og.ATR_PERIOD + 1])

    def test_a_zero_atr_window_produces_no_setup_rather_than_infinity(self):
        """A flat window makes any gap infinitely many ATRs.

        That is a division by zero wearing the costume of a very strong signal,
        so those bars are dropped rather than being the strongest setups in the
        sample.
        """
        bars = [bar(i, 100.0, high=100.0, low=100.0, open_=100.0)
                for i in range(20)] + [gap_bar(20, 130.0)]
        gap = og.gap_values(bars)
        assert np.isnan(gap[20])
        assert og.gap_setups(bars) == {}


# ---------------------------------------------------------------------------
# the threshold is inclusive, exactly at +/- 0.75
# ---------------------------------------------------------------------------


class TestTheThresholdIsInclusive:
    """The intake writes `>=` and `<=`. A `>` would silently drop the boundary.

    The corridor makes the boundary exactly representable: ATR is 2.0, so a
    0.75-ATR gap is an open of 101.5 — and 101.5, 100.0, 1.5 and 0.75 are all
    exact in binary floating point. This is a real equality test, not an
    approximation dressed as one.
    """

    def _gap_at(self, open_price):
        bars = corridor(20) + [gap_bar(20, open_price)] + corridor(3, start=21)
        return og.gap_values(bars)[20], og.gap_setups(bars)

    def test_exactly_plus_k_fires_short(self):
        value, setups = self._gap_at(100.0 + GAP_AT_K)
        assert value == 0.75                       # exact, not approx
        assert setups[19] == og.SHORT_SETUP

    def test_exactly_minus_k_fires_long(self):
        value, setups = self._gap_at(100.0 - GAP_AT_K)
        assert value == -0.75
        assert setups[19] == og.LONG_SETUP

    def test_just_inside_the_threshold_does_not_fire(self):
        value, setups = self._gap_at(100.0 + GAP_AT_K - 0.02)
        assert value < og.GAP_K
        assert setups == {}

    def test_just_inside_the_negative_threshold_does_not_fire(self):
        value, setups = self._gap_at(100.0 - GAP_AT_K + 0.02)
        assert value > -og.GAP_K
        assert setups == {}

    def test_a_flat_open_never_fires(self):
        bars = corridor(30)
        assert og.gap_setups(bars) == {}


# ---------------------------------------------------------------------------
# direction — the fade, not the follow
# ---------------------------------------------------------------------------


class TestTheDirectionIsTheFade:
    """Invert this and the signal becomes an opening-range breakout.

    That is not a smaller version of this hypothesis — it is the opposite one,
    and it is exactly the flip `docs/RESEARCH_HOLD.md` forbids being made after
    a loss. Pinned in both directions so the flip cannot arrive as a tidy-up.
    """

    def test_an_up_gap_is_a_short(self):
        bars = corridor(20) + [gap_bar(20, 105.0)] + corridor(3, start=21)
        assert og.gap_setups(bars)[19] == og.SHORT_SETUP

    def test_a_down_gap_is_a_long(self):
        bars = corridor(20) + [gap_bar(20, 95.0)] + corridor(3, start=21)
        assert og.gap_setups(bars)[19] == og.LONG_SETUP

    def test_the_directions_are_the_only_two_values(self):
        rng = np.random.default_rng(4)
        price, bars = 100.0, []
        for i in range(400):
            price *= float(np.exp(rng.normal(0, 0.03)))
            opened = price * float(np.exp(rng.normal(0, 0.03)))
            bars.append(bar(i, price, open_=opened,
                            high=max(price, opened) * 1.01,
                            low=min(price, opened) * 0.99))
        directions = set(og.gap_setups(bars).values())
        assert directions <= {og.LONG_SETUP, og.SHORT_SETUP}
        assert len(directions) == 2, "fixture should produce both sides"


# ---------------------------------------------------------------------------
# the index convention — signal at t-1, fill at open[t]
# ---------------------------------------------------------------------------


class TestTheSignalIndexIsOneBeforeTheGapBar:
    """The defect that would measure a different hypothesis under this name.

    The shared barrier fills a signal at index `s` on `open[s+1]`. The intake
    fills on the open of the gap bar itself, so the signal index must be one
    less than the bar the gap is measured on. Emitting at `t` would fill at
    `open[t+1]` — a full session late, after the reclaim the thesis is about
    has had a day to happen.
    """

    def test_the_setup_key_is_one_less_than_the_gap_bar(self):
        bars = corridor(20) + [gap_bar(20, 105.0)] + corridor(3, start=21)
        gap = og.gap_values(bars)
        assert gap[20] > og.GAP_K
        setups = og.gap_setups(bars)
        assert set(setups) == {19}

    def test_the_barrier_fills_that_signal_on_the_gap_bars_open(self):
        """Decisive, and checked against the shared barrier, not by assertion.

        `barrier_r_for_all_bars` is asked for the entry price it would use for
        a signal at 19 under this module's `ENTRY_ON`. It must be the gap bar's
        open, 105.0 — the price the trigger measured.
        """
        bars = corridor(20) + [gap_bar(20, 105.0)] + corridor(10, start=21)
        assert og.ENTRY_ON == "next_open"
        opens = [b.open for b in bars]
        assert opens[19 + 1] == 105.0

        indices, _net, _used = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=og.TAKE_PROFIT_ATR, stop_atr=og.STOP_ATR,
            horizon=og.HORIZON, atr_period=og.ATR_PERIOD,
            round_trip_bps=og.ROUND_TRIP_BPS, side="short",
            entry_on=og.ENTRY_ON)
        assert 19 in indices.tolist()

    def test_the_stop_is_sized_on_the_same_atr_the_gap_used(self):
        """`ATR_prev[t]` and the barrier's risk unit must be one number.

        The barrier sizes risk as `stop_atr * atr[signal_index]`. With the
        signal at `t-1` that is `atr[t-1]`, which is precisely the denominator
        of the gap. If the two ever disagreed, a 0.75-ATR gap would be stopped
        at something other than 1.5 of the ATR it was measured in.
        """
        bars = corridor(20) + [gap_bar(20, 105.0)] + corridor(10, start=21)
        atr = og._wilder_atr(bars)
        assert atr[19] == pytest.approx(FLAT_ATR)
        gap = og.gap_values(bars)
        assert gap[20] == pytest.approx((105.0 - 100.0) / atr[19])

    def test_the_last_bar_never_carries_a_setup(self):
        bars = corridor(20) + [gap_bar(20, 105.0)]
        directed = og.directed_signal_bars(bars)
        assert all(index < len(bars) - 1 for index, _d in directed)

    def test_warmup_bars_are_excluded(self):
        bars = corridor(60) + [gap_bar(60, 105.0)] + corridor(5, start=61)
        assert 59 in dict(og.directed_signal_bars(bars, warmup=0))
        assert 59 not in dict(og.directed_signal_bars(bars, warmup=100))


# ---------------------------------------------------------------------------
# material difference — fixtures where the neighbours disagree
# ---------------------------------------------------------------------------


class TestItIsNotPostShockFade:
    """Same asset, same fade, same barrier. Different measurement window.

    `post_shock_fade_v1` reads the CLOSE-TO-CLOSE return over ATR%. This reads
    the OVERNIGHT move. Both directions of disagreement are built, because
    either one alone would be consistent with the two rules being nested.
    """

    def test_a_close_to_close_shock_with_a_flat_open_fires_only_the_shock(self):
        """Bar 20 opens exactly at the prior close and then falls hard.

        `post_shock_fade_v1` sees a large negative return on the close. This
        signal sees an opening gap of exactly zero and stands down.
        """
        from signals import post_shock_fade_v1 as shock
        # The recovery bars continue from 80, not back at 100: a corridor that
        # jumped home would itself be a gap, and the fixture would fire this
        # signal for a reason that has nothing to do with bar 20.
        bars = corridor(20) + [bar(20, 80.0, open_=100.0, high=100.5,
                                   low=79.0)] + corridor(5, start=21,
                                                         close=80.0)
        assert og.gap_values(bars)[20] == pytest.approx(0.0)
        assert og.gap_setups(bars) == {}
        assert shock.shock_setups(bars), "fixture must fire the neighbour"

    def test_an_open_gap_that_closes_flat_fires_only_the_gap(self):
        """Bar 20 opens 3 ATR away and closes back at the prior close.

        This signal sees a large gap. `post_shock_fade_v1` sees a
        close-to-close return of zero and stands down. This is the direction
        that matters most: it shows the new rule selects events the frozen one
        cannot see, rather than a subset of them.
        """
        from signals import post_shock_fade_v1 as shock
        bars = corridor(20) + [bar(20, 100.0, open_=106.0, high=106.5,
                                   low=99.5)] + corridor(5, start=21)
        assert og.gap_values(bars)[20] == pytest.approx(3.0)
        assert og.gap_setups(bars)[19] == og.SHORT_SETUP
        assert 20 not in shock.shock_setups(bars)

    def test_the_two_modules_are_not_the_same_rule_on_random_data(self):
        """Over a long random series the two setup sets must differ materially.

        Nested rules would show one set contained in the other. Identical rules
        would show equality. Neither is acceptable for a materially different
        family, and 'they look different' is not a test.
        """
        from signals import post_shock_fade_v1 as shock
        rng = np.random.default_rng(17)
        price, bars = 100.0, []
        for i in range(1500):
            opened = price * float(np.exp(rng.normal(0, 0.02)))
            price = opened * float(np.exp(rng.normal(0, 0.02)))
            bars.append(bar(i, price, open_=opened,
                            high=max(price, opened) * 1.01,
                            low=min(price, opened) * 0.99))
        mine = set(og.gap_setups(bars))
        theirs = {t - 1 for t in shock.shock_setups(bars)}
        assert mine and theirs
        assert not mine <= theirs
        assert not theirs <= mine


class TestItIsNotRangeLocationFade:
    """No range, no window, no location — and fixtures rather than an argument."""

    def test_a_close_at_a_range_extreme_after_a_flat_open_fires_only_theirs(self):
        """A slow grind to the top of a 20-bar range, opening flat every day.

        `range_location_fade_v1` fires on the location. This signal sees a
        sequence of zero gaps.
        """
        from signals import range_location_fade_v1 as rl
        bars = corridor(25)
        price = 100.0
        for i in range(25, 45):
            price += 0.5
            # open exactly at the prior close: a grind, never a gap
            bars.append(bar(i, price, open_=price - 0.5,
                            high=price + 0.2, low=price - 0.7))
        assert og.gap_setups(bars) == {}
        assert rl.location_setups(bars), "fixture must fire the neighbour"

    def test_a_gap_landing_mid_range_fires_only_mine(self):
        """Bar 25 gaps 2 ATR up but closes back inside the corridor.

        The close sits at the middle of its trailing range, so
        `range_location_fade_v1` stands down; the gap is unmistakable, so this
        one fires.
        """
        from signals import range_location_fade_v1 as rl
        bars = corridor(25) + [bar(25, 100.0, open_=104.0, high=104.5,
                                   low=99.0)] + corridor(3, start=26)
        assert og.gap_setups(bars)[24] == og.SHORT_SETUP
        loc = rl.range_location(bars)
        assert 0.10 < loc[25] < 0.90
        assert 25 not in rl.location_setups(bars)

    def test_the_two_modules_are_not_the_same_rule_on_random_data(self):
        from signals import range_location_fade_v1 as rl
        rng = np.random.default_rng(23)
        price, bars = 100.0, []
        for i in range(1500):
            opened = price * float(np.exp(rng.normal(0, 0.02)))
            price = opened * float(np.exp(rng.normal(0, 0.02)))
            bars.append(bar(i, price, open_=opened,
                            high=max(price, opened) * 1.01,
                            low=min(price, opened) * 0.99))
        mine = set(og.gap_setups(bars))
        theirs = set(rl.location_setups(bars))
        assert mine and theirs
        assert not mine <= theirs
        assert not theirs <= mine


class TestItIsNotAnyOfTheOtherFrozenFamilies:

    def test_it_reads_no_other_instrument(self):
        """Structural, not textual: no second price series can reach it.

        Written as an AST and signature check on the first attempt. A raw-text
        ban would fail against the module's own docstring, which *explains*
        why this is not the cross-asset family — the seventh time this
        repository would have learned that a text ban forces the code to stop
        explaining what it forbids (EDGE.md §12c).
        """
        for name in ("gap_values", "gap_setups", "directed_signal_bars",
                     "flags_and_directions", "summary"):
            signature = inspect.signature(getattr(og, name))
            positional = [p for p in signature.parameters.values()
                          if p.kind is not p.KEYWORD_ONLY]
            assert [p.name for p in positional] == ["bars"], (name, positional)

        tree = ast.parse(inspect.getsource(og))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        assert not any(name.startswith("signals") for name in imported), imported

    def test_it_uses_no_oscillator_and_no_channel(self):
        tree = ast.parse(inspect.getsource(og))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for forbidden in ("rsi", "macd", "adx", "bollinger", "supertrend",
                          "channel", "donchian", "range_high", "range_low"):
            assert forbidden not in names, forbidden


# ---------------------------------------------------------------------------
# no lookahead
# ---------------------------------------------------------------------------


class TestNoLookahead:

    def test_truncating_the_future_does_not_change_the_past(self):
        rng = np.random.default_rng(11)
        price, bars = 100.0, []
        for i in range(300):
            opened = price * float(np.exp(rng.normal(0, 0.02)))
            price = opened * float(np.exp(rng.normal(0, 0.02)))
            bars.append(bar(i, price, open_=opened,
                            high=max(price, opened) * 1.01,
                            low=min(price, opened) * 0.99))
        full = og.gap_setups(bars)
        for cut in (120, 200, 280):
            prefix = og.gap_setups(bars[:cut])
            for index, direction in prefix.items():
                assert full.get(index) == direction, (cut, index)

    def test_the_gap_values_themselves_are_bit_stable_under_truncation(self):
        """Stronger than the setup check: the ratio itself must not move.

        A setup-level check would pass even if the denominator drifted, as long
        as the drift never crossed the threshold.
        """
        rng = np.random.default_rng(12)
        price, bars = 100.0, []
        for i in range(300):
            opened = price * float(np.exp(rng.normal(0, 0.02)))
            price = opened * float(np.exp(rng.normal(0, 0.02)))
            bars.append(bar(i, price, open_=opened,
                            high=max(price, opened) * 1.01,
                            low=min(price, opened) * 0.99))
        full = og.gap_values(bars)
        for cut in (150, 250):
            prefix = og.gap_values(bars[:cut])
            for t in range(prefix.size):
                if np.isnan(prefix[t]):
                    assert np.isnan(full[t]), t
                else:
                    assert prefix[t] == full[t], t     # identical bits

    def test_the_trigger_never_indexes_a_forward_bar(self):
        """AST: no `x[i + k]`. The fill is the barrier's job, not this module's."""
        tree = ast.parse(inspect.getsource(og))
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
    """Every R in this project comes from one function. This adds no second one.

    A forked barrier that drifted by a line would make this measurement
    incomparable with every number already in EDGE.md — and the drift would be
    invisible, because both sides would still produce plausible R values.
    """

    def test_the_module_computes_no_barrier_of_its_own(self):
        tree = ast.parse(inspect.getsource(og))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for forbidden in ("tp_level", "sl_level", "realised", "payoff",
                          "barrier_r", "stop_level", "take_profit_level",
                          "net_r"):
            assert forbidden not in names, forbidden

    def test_no_cost_arithmetic_happens_here(self):
        tree = ast.parse(inspect.getsource(og))
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp):
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                assert "ROUND_TRIP_BPS" not in names

    def test_the_atr_helper_defers_to_the_production_implementation(self):
        source = inspect.getsource(og._wilder_atr)
        assert "wilder_atr" in source
        tree = ast.parse(inspect.getsource(og))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert "true_range" not in names

    def test_the_stop_and_target_constants_are_only_declared_here(self):
        """They are passed to the shared barrier; they are never applied here."""
        tree = ast.parse(inspect.getsource(og))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                assert "STOP_ATR" not in names, node.name
                assert "TAKE_PROFIT_ATR" not in names, node.name


# ---------------------------------------------------------------------------
# scheduling
# ---------------------------------------------------------------------------


class TestOneTradePerRun:

    def test_consecutive_gaps_yield_one_entry(self):
        bars = corridor(20)
        for i in range(20, 24):
            bars.append(gap_bar(i, 105.0))
        bars += corridor(3, start=24)
        flags, _d = og.flags_and_directions(bars)
        entries = sk.simulate_schedule(flags, warmup=0, lockup=og.LOCKUP)
        assert len([e for e in entries if 19 <= e <= 23]) <= 1

    def test_separated_gaps_yield_two_entries(self):
        bars = corridor(20) + [gap_bar(20, 105.0)] + corridor(20, start=21)
        bars.append(gap_bar(41, 105.0))
        bars += corridor(3, start=42)
        flags, _d = og.flags_and_directions(bars)
        entries = sk.simulate_schedule(flags, warmup=0, lockup=og.LOCKUP)
        assert len([e for e in entries if e in (19, 40)]) == 2

    def test_flags_and_directions_agree(self):
        bars = corridor(20) + [gap_bar(20, 105.0)] + corridor(3, start=21)
        flags, directions = og.flags_and_directions(bars)
        assert set(np.nonzero(flags)[0].tolist()) == set(directions)

    def test_the_summary_adds_up(self):
        bars = corridor(20) + [gap_bar(20, 105.0)] + corridor(20, start=21) + \
            [gap_bar(41, 95.0)] + corridor(3, start=42)
        summary = og.summary(bars)
        assert summary["setups"] == summary["long_setups"] + \
            summary["short_setups"]
        assert summary["long_setups"] == 1
        assert summary["short_setups"] == 1


# ---------------------------------------------------------------------------
# the real corpora — counts, not scores
# ---------------------------------------------------------------------------


class TestTheRealCorpora:
    """Counts only. No R, no percentile — those belong to the measurement path.

    THE FINDING THIS CLASS PINS
    ===========================
    The rule fires **1 / 0 / 0** times on BTCUSD / ETHUSDT / SOLUSDT against a
    design target of >= 50 per symbol. That is not a threshold that happens to
    be a little high: it is a structural property of the data.

    **Spot crypto trades 24/7, so there is no overnight session.** The daily
    "open" is the first print after 00:00 UTC and the prior daily "close" is the
    last print before it — seconds apart. The gap this thesis is about is the
    tick-level move across midnight, not an auction discontinuity. Measured
    below: on BTCUSD 17% of bars have `open[t]` exactly equal to `close[t-1]`
    and the median gap is 0.0135% of price; on the two Binance series roughly
    half of all bars are exactly equal and the median gap is 0.0002%.

    These numbers are pinned rather than described so that a corpus change, or
    an ATR-path change that made the denominator wrong, fails here with a
    figure a reader can compare — instead of quietly turning a structural
    absence into a plausible-looking sample.
    """

    def _bars(self, directory, symbol=None):
        import market_data as md
        loaded, _b, _n = md.load_corpus(os.path.join(REPO, directory))
        key = symbol or sorted(loaded)[0]
        if key not in loaded:
            pytest.skip(f"{key} not in {directory}")
        return [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
                for b in loaded[key]]

    def test_btc_daily_setups_are_stable(self):
        bars = self._bars("data/real_1d")
        summary = og.summary(bars, warmup=200)
        assert summary["bars"] == 3135
        assert summary["setups"] == 1
        assert summary["long_setups"] + summary["short_setups"] == 1

    @pytest.mark.parametrize("symbol", ["ETHUSDT", "SOLUSDT"])
    def test_the_alts_produce_no_setup_at_all(self, symbol):
        bars = self._bars("data/real_multi_1d", symbol)
        assert og.summary(bars, warmup=200)["setups"] == 0

    def test_no_symbol_reaches_the_intakes_fifty_trade_design_target(self):
        """The count finding, as an assertion rather than a paragraph.

        The intake's instruction when this happens is explicit: do not lower
        GAP_K. This test is what makes lowering it visible — a smaller
        threshold would turn these into non-zero counts and this assertion
        would have to be edited by hand, with a human on the commit.
        """
        counts = [og.summary(self._bars("data/real_1d"), warmup=200)["setups"]]
        for symbol in ("ETHUSDT", "SOLUSDT"):
            counts.append(og.summary(self._bars("data/real_multi_1d", symbol),
                                     warmup=200)["setups"])
        assert counts == [1, 0, 0]
        assert all(count < 50 for count in counts)

    @pytest.mark.parametrize("directory,symbol,exact,median_pct", [
        ("data/real_1d", "BTCUSD", 540, 0.0135),
        ("data/real_multi_1d", "ETHUSDT", 702, 0.0002),
        ("data/real_multi_1d", "SOLUSDT", 740, 0.0000),
    ])
    def test_the_open_is_essentially_the_prior_close(self, directory, symbol,
                                                     exact, median_pct):
        """Why the count is what it is: 24/7 markets do not gap.

        `exact` is the number of bars where `open[t] == close[t-1]` to the last
        bit. `median_pct` is the median absolute gap as a percentage of price.
        Both are properties of the corpus, not of this signal, and pinning them
        distinguishes "the rule found nothing" from "the rule is broken".
        """
        bars = self._bars(directory, symbol)
        opens = np.array([b.open for b in bars])
        closes = np.array([b.close for b in bars])
        raw = opens[1:] - closes[:-1]
        assert int((raw == 0.0).sum()) == exact
        relative = np.abs(raw) / closes[:-1] * 100.0
        assert float(np.median(relative)) == pytest.approx(median_pct,
                                                           abs=5e-5)

    def test_the_denominator_is_a_plausible_daily_atr(self):
        """Guards the alternative explanation: a wrongly huge ATR.

        If `ATR_prev` were inflated, real gaps would read as tiny and the count
        finding would be an artefact. BTC's daily Wilder ATR(14) must sit in a
        believable band as a fraction of price — a few percent, not tens.
        """
        bars = self._bars("data/real_1d")
        atr = og._wilder_atr(bars)
        closes = np.array([b.close for b in bars])
        ratio = atr[np.isfinite(atr)] / closes[np.isfinite(atr)]
        assert 0.01 < float(np.median(ratio)) < 0.10


class TestTheSurrogateCannotExerciseThisTrigger:
    """The instrument finding, pinned in code because it decides the verdict.

    `skill_test.surrogate_series` re-bases every shuffled bar onto the running
    price with `scale = price / bar.open`, where `price` is the previous
    surrogate bar's close. The re-based open is therefore **exactly** the prior
    close, on every bar, by construction.

    For a trigger that reads `open[t] - close[t-1]` that is not a destroyed
    relationship — it is a deleted quantity. A null must randomise *when* an
    event happens, not remove the possibility of the event. So this control
    cannot be exercised for this family at all, and its INVALID verdict says
    nothing about market structure.

    This is asserted rather than argued because it is the difference between
    "the control failed" (an instrument-bias finding) and "the control could
    not be run" (a construction mismatch), and those two lead to different
    honest verdicts.
    """

    def test_every_surrogate_bar_opens_exactly_at_the_prior_close(self):
        rng = np.random.default_rng(5)
        price, bars = 100.0, []
        for i in range(300):
            opened = price * float(np.exp(rng.normal(0, 0.02)))
            price = opened * float(np.exp(rng.normal(0, 0.02)))
            bars.append(bar(i, price, open_=opened,
                            high=max(price, opened) * 1.01,
                            low=min(price, opened) * 0.99))
        shuffled = sk.surrogate_series(bars, seed=99)
        opens = np.array([b.open for b in shuffled])
        closes = np.array([b.close for b in shuffled])
        assert np.allclose(opens[1:], closes[:-1], rtol=0, atol=1e-9)

    def test_a_surrogate_of_a_gap_rich_series_has_no_gaps_left(self):
        """Decisive: a series built to fire constantly stops firing entirely."""
        bars = corridor(20)
        for i in range(20, 120):
            bars.append(gap_bar(i, 100.0 + (6.0 if i % 2 else -6.0)))
        assert len(og.gap_setups(bars)) > 50, "fixture must fire a lot"
        shuffled = sk.surrogate_series(bars, seed=7)
        assert og.gap_setups(shuffled) == {}


# ---------------------------------------------------------------------------
# the slice-49 measurement record
# ---------------------------------------------------------------------------


class TestTheSlice49Artefact:
    """The record must agree with the repository, not with its own summary.

    The specific failure this guards against is the one that matters most for
    an INCONCLUSIVE family: an artefact that carries a percentile. There is no
    M1 and no M2 for `open_gap_fade_v1`, so any number in those fields would be
    invented — and a later reader would have no way to tell.
    """

    PATH = os.path.join(REPO, "artifacts", "slice49_stage1_open_gap_fade_v1.json")

    def _payload(self):
        with open(self.PATH, encoding="utf-8") as handle:
            return json.load(handle)

    def test_it_exists_and_is_valid_json(self):
        assert os.path.exists(self.PATH)
        assert self._payload()["schema"] == "stage1_measurement/1"

    def test_the_constants_are_the_modules(self):
        declared = self._payload()["constants"]
        assert declared["GAP_K"] == og.GAP_K == 0.75
        assert declared["ATR_PERIOD"] == og.ATR_PERIOD
        assert declared["STOP_ATR"] == og.STOP_ATR
        assert declared["TAKE_PROFIT_R"] == og.TAKE_PROFIT_R
        assert declared["TAKE_PROFIT_ATR"] == og.TAKE_PROFIT_ATR
        assert declared["HORIZON"] == og.HORIZON
        assert declared["LOCKUP"] == og.LOCKUP
        assert declared["ROUND_TRIP_BPS"] == og.ROUND_TRIP_BPS
        assert declared["ENTRY_ON"] == og.ENTRY_ON

    def test_no_m1_or_m2_is_quoted_anywhere(self):
        """The decisive one. INCONCLUSIVE means no reading exists."""
        payload = self._payload()
        edge = payload["edge"]
        assert edge["runs_executed"] == 0
        assert edge["m1"] is None and edge["m2"] is None
        assert edge["n_trades"] is None and edge["mean_r"] is None
        assert edge["control_validated"] is False
        assert payload["edge_verdict"] == "INCONCLUSIVE"

    def test_the_counts_match_the_signal_module_on_the_real_corpora(self):
        """Not a transcription: recomputed from the corpora it claims."""
        import market_data as md
        claimed = self._payload()["count_finding"]["entries"]
        for symbol, directory in (("BTCUSD", "data/real_1d"),
                                  ("ETHUSDT", "data/real_multi_1d"),
                                  ("SOLUSDT", "data/real_multi_1d")):
            loaded, _b, _n = md.load_corpus(os.path.join(REPO, directory))
            bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
                    for b in loaded[symbol]]
            assert og.summary(bars, warmup=200)["setups"] == claimed[symbol]

    def test_no_symbol_is_claimed_to_meet_the_target(self):
        finding = self._payload()["count_finding"]
        assert finding["design_target_per_symbol"] == 50
        assert finding["symbols_meeting_target"] == []
        assert all(n < 50 for n in finding["entries"].values())

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

    def test_the_control_failure_is_named_as_a_construction_mismatch(self):
        """Mislabelling this as bias would corrupt the standing hypothesis.

        Slice 46's z = +3.6 was an instrument that ran and came back skewed.
        This one never ran. Filing them together would make the bias evidence
        look stronger than it is.
        """
        why = self._payload()["control"]["why_invalid"]
        assert "CONSTRUCTION MISMATCH" in why
        assert "NOT INSTRUMENT BIAS" in why
        assert "surrogate_series" in why

    def test_the_positive_rule_is_the_intakes_and_is_not_met(self):
        payload = self._payload()
        assert payload["positive_rule_met"] is False
        assert payload["cleared_edge_signal"] is None
        rule = payload["positive_rule"].lower()
        assert "at least two" in rule
        assert "50 scored trades" in rule

    def test_the_null_defect_is_recorded_and_not_repaired(self):
        """A null fixed after seeing its verdict is fitted to that verdict."""
        note = self._payload()["instrument_defect_recorded_not_repaired"]
        assert "NOT built here" in note
        assert "human pre-declaration" in note
        source = inspect.getsource(sk.surrogate_series)
        assert "scale = price / bar.open" in source, (
            "the defect this artefact describes must still be the code's "
            "actual behaviour, or the note is stale")

    def test_it_claims_no_progress_toward_a_profit_agent(self):
        payload = self._payload()
        assert payload["closer_to_autonomous_profit_agent"] is False
        assert payload["live_authorized"] is False
        assert payload["models_current_present"] is False
        assert not os.path.exists(os.path.join(REPO, "models", "current"))

    def test_the_prior_freezes_are_untouched(self):
        """The six that were frozen when this ran are still frozen.

        The artefact records `frozen_absent_count: 6` because that was true at
        slice 49, when this family's freeze was still only PROPOSED. Slice 50
        performed it, so the count is now 7 and the artefact is left alone —
        rewriting a dated record to match today's code destroys the record.
        """
        import project_status as ps
        payload = self._payload()
        assert payload["prior_six_still_frozen"] is True
        assert payload["frozen_absent_count"] == 6
        assert len(ps.ABSENT_SIGNALS) >= 6
        assert payload["bars"]["m1"] == ps.M1_BAR == 95.0
        assert payload["bars"]["m2"] == ps.M2_BAR == 95.0

    def test_this_family_is_now_frozen_inconclusive(self):
        """Slice 49 proposed the freeze; slice 50 carried it out."""
        import project_status as ps
        assert "open_gap_fade_v1" in ps.ABSENT_SIGNALS
        assert ps.FROZEN_STATUS["open_gap_fade_v1"] == "INCONCLUSIVE"
        why = ps.FROZEN_ABSENT["open_gap_fade_v1"]
        assert "NO M1 and NO M2" in why
        assert "1 / 0 / 0" in why
        assert "CONSTRUCTION MISMATCH" in why
        assert "slice49_count_finding_open_gap_fade.log" in why

    def test_no_edge_summary_artefact_exists_for_this_family(self):
        """The strongest available check that STEP 5 did not run."""
        artifacts = os.path.join(REPO, "artifacts")
        hits = [f for f in os.listdir(artifacts)
                if "open_gap_fade" in f and "edge" in f]
        assert hits == [], hits
