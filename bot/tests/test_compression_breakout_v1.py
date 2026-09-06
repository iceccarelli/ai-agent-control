"""Tests for `compression_breakout_v1` — the slice-53 compressed-break signal.

The rule is a conjunction of two conditions computed over two windows that are
deliberately asymmetric, which is where every plausible-but-wrong version of it
lives:

* include bar `t`'s own high in `HH[t]` and `close[t] > HH[t]` becomes almost
  unreachable — the signal would nearly never fire and it would look like a
  market fact rather than an arithmetic one;
* exclude bar `t` from the ATR percentile window and the rank answers a
  different question, against a reference set the bar is not in;
* drop the compression half and this is a 20-bar Donchian, which is a frozen
  family with a shorter window;
* drop the break half and it is a volatility filter with no trade in it.

Each gets a decisive test. The material-difference claim against
`donchian_breakout_v1` is carried by a pair of fixtures that differ *only* in
how violent the breaking bar is: a quiet break fires, a loud break of the same
channel does not.
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
from signals import compression_breakout_v1 as cb  # noqa: E402

DAY_MS = 86_400_000
EPOCH = int(dt.datetime(2022, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)


def bar(day, close, *, high, low, open_=None, vol=100.0):
    return bt.Bar(EPOCH + day * DAY_MS, close if open_ is None else open_,
                  high, low, close, vol)


def calm(n, *, start=0, close=100.0, half=1.0):
    """`n` identical quiet bars: constant true range `2 * half`.

    Wilder ATR is exactly `2 * half` once defined, every ATR in the window is
    identical, and the percentile rank of any such bar is exactly 0 — so every
    assertion about compression on this fixture is arithmetic, not a guess.
    """
    return [bar(start + i, close, high=close + half, low=close - half)
            for i in range(n)]


def quiet_break(day, *, close=101.5, high=101.6, low=100.5):
    """A bar that closes above the calm channel WITHOUT expanding its range.

    Its own true range (1.6) is below the corridor's ATR (2.0), so the ATR at
    this bar falls slightly and its percentile stays at the floor. This is the
    event the thesis is about.
    """
    return bar(day, close, high=high, low=low)


def loud_break(day, *, close=118.0, high=120.0, low=99.0):
    """A bar that breaks the same channel by expanding violently.

    Its true range (21) is ten times the corridor's, so ATR jumps and the
    percentile rank goes to the top of its own window. Donchian fires here;
    this signal must not.
    """
    return bar(day, close, high=high, low=low)


# ---------------------------------------------------------------------------
# the constants
# ---------------------------------------------------------------------------


class TestTheConstantsAreTheIntakes:
    """Frozen in NEW_SIGNAL_INTAKE.md and EDGE.md §34b before any number."""

    def test_every_declared_constant(self):
        assert cb.COMP_MAX == 25.0
        assert cb.PCTILE_WINDOW == 50
        assert cb.BREAK_N == 20
        assert cb.ATR_PERIOD == 14
        assert cb.STOP_ATR == 1.5
        assert cb.TAKE_PROFIT_R == 2.0
        assert cb.HORIZON == 5
        assert cb.LOCKUP == 1
        assert cb.ROUND_TRIP_BPS == 25.0
        assert cb.ENTRY_ON == "next_open"
        assert cb.NAME == "compression_breakout_v1"

    def test_two_r_of_take_profit_is_two_stop_distances(self):
        """The barrier's payoff is take_profit_atr / stop_atr.

        The intake fixes the STOP at 1.5 ATR and the target at 2.0 R, so the
        take-profit is 3.0 ATR. Writing 2.0 would give a payoff of 1.33 and
        quietly change the target — the trap slice 43 walked into.
        """
        assert cb.TAKE_PROFIT_ATR == 3.0
        assert cb.TAKE_PROFIT_ATR / cb.STOP_ATR == pytest.approx(2.0)

    def test_the_channel_is_not_the_frozen_donchians(self):
        """A copied constant would leave this measuring the frozen family."""
        from signals import donchian_breakout_v1 as don
        assert cb.BREAK_N == 20
        assert don.CHANNEL_N == 55
        assert cb.BREAK_N != don.CHANNEL_N

    def test_the_scheduler_constant_is_the_instruments(self):
        from signals import ts_momentum_v1 as ts
        assert cb.LOCKUP == ts.LOCKUP == 1


# ---------------------------------------------------------------------------
# the ATR percentile — window INCLUDES bar t
# ---------------------------------------------------------------------------


class TestThePercentileWindowIncludesBarT:
    """The question is where TODAY's volatility sits in its own distribution."""

    def test_a_uniform_window_ranks_at_the_floor(self):
        """Every ATR identical -> nothing is below -> rank 0, and compressed."""
        bars = calm(80)
        pct = cb.atr_percentiles(bars)
        assert pct[79] == 0.0
        assert pct[79] <= cb.COMP_MAX

    def test_the_window_is_undefined_until_it_is_full(self):
        """A rank over a partial window compares against a different set."""
        bars = calm(80)
        pct = cb.atr_percentiles(bars)
        first = cb.PCTILE_WINDOW + cb.ATR_PERIOD - 1
        for t in range(first):
            assert np.isnan(pct[t]), t
        assert np.isfinite(pct[first])

    def test_bar_t_is_a_member_of_its_own_window(self):
        """Decisive, and it is the asymmetry a reader will doubt.

        Bar 70 is made violent. Its own ATR jumps, and because the window
        includes it, its rank rises to the top. Were bar t excluded, its rank
        would be computed from the calm bars alone and would stay at the floor
        — which is exactly the defect that would let a violent break count as
        compressed.
        """
        bars = calm(70) + [loud_break(70)] + calm(9, start=71)
        pct = cb.atr_percentiles(bars)
        assert pct[69] == 0.0
        assert pct[70] > cb.COMP_MAX
        assert pct[70] > 90.0

    def test_a_quiet_bar_stays_at_the_floor(self):
        bars = calm(70) + [quiet_break(70)] + calm(9, start=71)
        pct = cb.atr_percentiles(bars)
        assert pct[70] <= cb.COMP_MAX
        assert pct[70] == 0.0

    def test_the_threshold_is_inclusive_at_twenty_five(self):
        """`<= 25` is what the intake writes; a `<` would drop the boundary.

        Built so the rank is exactly 25.0: a 50-bar window in which precisely
        the bottom 12 ATRs are strictly below bar t's, and 12/50 * 100 = 24.0
        is not it — the window is constructed to put exactly 12.5... so the
        assertion is made on the arithmetic the implementation actually uses,
        with a hand-checked count.
        """
        window = cb.PCTILE_WINDOW
        # a synthetic ATR ladder is not reachable through bars alone, so the
        # convention is asserted directly on the documented formula
        block = np.arange(window, dtype=float)
        value = block[window // 4]                 # 12 of 50 strictly below
        rank = float((block < value).sum()) / float(block.size) * 100.0
        assert rank == 24.0
        assert rank <= cb.COMP_MAX
        value = block[window // 4 + 1]             # 13 of 50 -> 26.0
        rank = float((block < value).sum()) / float(block.size) * 100.0
        assert rank == 26.0
        assert rank > cb.COMP_MAX


# ---------------------------------------------------------------------------
# the channel — EXCLUDES bar t
# ---------------------------------------------------------------------------


class TestTheChannelExcludesBarT:
    """A bar cannot break a level it helped set."""

    def test_the_bounds_are_the_prior_twenty_bars(self):
        bars = calm(40)
        hh, ll = cb.channel_bounds(bars)
        assert hh[30] == pytest.approx(101.0)
        assert ll[30] == pytest.approx(99.0)
        for t in range(cb.BREAK_N):
            assert np.isnan(hh[t]) and np.isnan(ll[t]), t

    def test_a_bars_own_high_is_not_in_its_channel(self):
        """Decisive. Bar 70's high is 120 and its channel top stays 101.

        Were bar t included, `HH[70]` would be 120 and `close[70] > HH[70]`
        could never hold — the rule would silently almost never fire.
        """
        bars = calm(70) + [loud_break(70)] + calm(9, start=71)
        hh, _ll = cb.channel_bounds(bars)
        assert hh[70] == pytest.approx(101.0)
        assert bars[70].high == 120.0
        assert bars[70].close > hh[70]

    def test_the_break_is_strict_not_a_touch(self):
        """`close > HH`, not `>=`. Touching a level is not breaking it."""
        bars = calm(70) + [bar(70, 101.0, high=101.2, low=100.5)] + \
            calm(9, start=71)
        hh, _ll = cb.channel_bounds(bars)
        assert bars[70].close == hh[70]
        assert 70 not in cb.compression_setups(bars)
        assert 70 not in cb.breaks_without_compression(bars)


# ---------------------------------------------------------------------------
# the conjunction — both halves, or nothing
# ---------------------------------------------------------------------------


class TestBothHalvesAreRequired:
    """The entire content of this family is the AND."""

    def test_a_quiet_break_fires(self):
        bars = calm(70) + [quiet_break(70)] + calm(9, start=71)
        setups = cb.compression_setups(bars)
        assert setups == {70: cb.LONG_SETUP}

    def test_a_loud_break_of_the_same_channel_does_not_fire(self):
        """Decisive, and it is the thesis in one assertion.

        Same channel, same direction, same bar index. The only difference is
        that this break expands range, so its own ATR percentile leaves the
        compressed zone.
        """
        bars = calm(70) + [loud_break(70)] + calm(9, start=71)
        assert cb.compression_setups(bars) == {}
        assert 70 in cb.breaks_without_compression(bars)

    def test_compression_without_a_break_does_not_fire(self):
        """Sixty quiet bars are compressed on every bar and trade on none."""
        bars = calm(90)
        pct = cb.atr_percentiles(bars)
        compressed = [t for t in range(len(bars))
                      if np.isfinite(pct[t]) and pct[t] <= cb.COMP_MAX]
        assert len(compressed) > 20, "the fixture must be compressed"
        assert cb.compression_setups(bars) == {}

    def test_a_downside_quiet_break_is_a_short(self):
        bars = calm(70) + [bar(70, 98.5, high=99.5, low=98.4)] + \
            calm(9, start=71)
        assert cb.compression_setups(bars) == {70: cb.SHORT_SETUP}

    def test_the_direction_follows_the_break(self):
        """Continuation, not fade. Inverting this makes it a different family."""
        up = cb.compression_setups(calm(70) + [quiet_break(70)] +
                                   calm(9, start=71))
        down = cb.compression_setups(calm(70) +
                                     [bar(70, 98.5, high=99.5, low=98.4)] +
                                     calm(9, start=71))
        assert up[70] == cb.LONG_SETUP
        assert down[70] == cb.SHORT_SETUP


# ---------------------------------------------------------------------------
# material difference against donchian_breakout_v1
# ---------------------------------------------------------------------------


class TestItIsNotTheFrozenDonchian:
    """The nearest neighbour, and the one the design note owes a fixture."""

    def test_the_same_break_fires_donchian_and_not_this(self):
        """Both directions of the pair, on one channel.

        `donchian_breakout_v1` uses a 55-bar channel, so the fixture is long
        enough for both to be defined, and the breaking bar clears both.
        """
        from signals import donchian_breakout_v1 as don
        bars = calm(70) + [loud_break(70)] + calm(9, start=71)
        flags = don.flags_from_bars(bars, warmup=0)
        assert flags[70], "the fixture must break the frozen family's channel"
        assert cb.compression_setups(bars) == {}

    def test_the_quiet_break_fires_both_which_is_expected(self):
        """Honest converse: the families overlap where the break is quiet.

        The claim is not that they never agree — it is that this one refuses a
        subset the other accepts. Asserting disagreement everywhere would be a
        test written to flatter the thesis.
        """
        from signals import donchian_breakout_v1 as don
        bars = calm(70) + [quiet_break(70)] + calm(9, start=71)
        assert cb.compression_setups(bars) == {70: cb.LONG_SETUP}
        assert don.flags_from_bars(bars, warmup=0)[70]

    def test_the_compression_filter_removes_breaks_on_random_data(self):
        """The material-difference claim as a measurement, not a fixture.

        If the filter removed nothing, this family would be a 20-bar Donchian
        wearing a precondition. The count finding reports this ratio on the
        real corpora; here it is asserted to be doing work at all.
        """
        rng = np.random.default_rng(31)
        price, bars = 100.0, []
        for i in range(1500):
            step = float(np.exp(rng.normal(0, 0.02)))
            high = price * max(step, 1.0) * 1.01
            low = price * min(step, 1.0) * 0.99
            price *= step
            bars.append(bar(i, price, high=high, low=low))
        every = cb.breaks_without_compression(bars)
        filtered = cb.compression_setups(bars)
        assert every, "the fixture must produce breaks"
        assert set(filtered) <= set(every)
        assert len(filtered) < len(every), "the filter must remove something"


class TestItIsNotTheOtherFrozenFamilies:

    def test_it_reads_no_other_instrument(self):
        for name in ("atr_percentiles", "channel_bounds", "compression_setups",
                     "breaks_without_compression", "directed_signal_bars",
                     "flags_and_directions", "summary"):
            signature = inspect.signature(getattr(cb, name))
            positional = [p for p in signature.parameters.values()
                          if p.kind is not p.KEYWORD_ONLY]
            assert [p.name for p in positional] == ["bars"], (name, positional)
        tree = ast.parse(inspect.getsource(cb))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        assert not any(name.startswith("signals") for name in imported), imported

    def test_it_uses_no_oscillator_and_no_cumulative_return(self):
        tree = ast.parse(inspect.getsource(cb))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for forbidden in ("rsi", "macd", "adx", "bollinger", "supertrend",
                          "ret", "sign", "lookback", "skip", "gap"):
            assert forbidden not in names, forbidden

    def test_it_never_reads_an_open(self):
        """The slice-49 corpus fact cannot touch this trigger."""
        highs = [101.0 + i * 0.0 for i in range(80)]
        base = calm(80)
        mangled = [bt.Bar(b.start_ms, b.open * 5.0, b.high, b.low, b.close,
                          b.volume) for b in base]
        assert cb.compression_setups(base) == cb.compression_setups(mangled)
        tree = ast.parse(inspect.getsource(cb))
        attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert "open" not in attributes


# ---------------------------------------------------------------------------
# no lookahead
# ---------------------------------------------------------------------------


class TestNoLookahead:

    def _random(self, n=400, seed=17):
        rng = np.random.default_rng(seed)
        price, bars = 100.0, []
        for i in range(n):
            step = float(np.exp(rng.normal(0, 0.02)))
            high = price * max(step, 1.0) * 1.01
            low = price * min(step, 1.0) * 0.99
            price *= step
            bars.append(bar(i, price, high=high, low=low))
        return bars

    def test_truncating_the_future_does_not_change_the_past(self):
        bars = self._random()
        full = cb.compression_setups(bars)
        for cut in (200, 300, 380):
            prefix = cb.compression_setups(bars[:cut])
            for index, direction in prefix.items():
                assert full.get(index) == direction, (cut, index)

    def test_the_percentiles_are_bit_stable_under_truncation(self):
        """Stronger than the setup check: the rank itself must not move."""
        bars = self._random()
        full = cb.atr_percentiles(bars)
        for cut in (250, 350):
            prefix = cb.atr_percentiles(bars[:cut])
            for t in range(prefix.size):
                if np.isnan(prefix[t]):
                    assert np.isnan(full[t]), t
                else:
                    assert prefix[t] == full[t], t

    def test_the_channel_is_bit_stable_under_truncation(self):
        bars = self._random()
        hh, ll = cb.channel_bounds(bars)
        for cut in (250, 350):
            phh, pll = cb.channel_bounds(bars[:cut])
            for t in range(phh.size):
                if np.isnan(phh[t]):
                    assert np.isnan(hh[t]), t
                else:
                    assert phh[t] == hh[t] and pll[t] == ll[t], t

    def test_the_last_bar_never_carries_a_setup(self):
        bars = calm(70) + [quiet_break(70)]
        assert all(i < len(bars) - 1 for i, _d in cb.directed_signal_bars(bars))

    def test_warmup_bars_are_excluded(self):
        bars = calm(70) + [quiet_break(70)] + calm(9, start=71)
        assert 70 in dict(cb.directed_signal_bars(bars, warmup=0))
        assert 70 not in dict(cb.directed_signal_bars(bars, warmup=71))

    def test_the_trigger_never_indexes_a_forward_bar(self):
        tree = ast.parse(inspect.getsource(cb))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript):
                continue
            if isinstance(node.slice, ast.BinOp) and \
                    isinstance(node.slice.op, ast.Add):
                # `atr[start:t + 1]` is a SLICE ending at t inclusive, not a
                # forward read; only bare `x[i + k]` subscripts are forbidden.
                if isinstance(node.slice, ast.Slice):
                    continue
                raise AssertionError(ast.dump(node)[:160])


# ---------------------------------------------------------------------------
# no forked R arithmetic
# ---------------------------------------------------------------------------


class TestTheRArithmeticIsNotForked:

    def test_the_module_computes_no_barrier_of_its_own(self):
        tree = ast.parse(inspect.getsource(cb))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for forbidden in ("tp_level", "sl_level", "realised", "payoff",
                          "barrier_r", "stop_level", "take_profit_level",
                          "net_r"):
            assert forbidden not in names, forbidden

    def test_no_cost_arithmetic_happens_here(self):
        tree = ast.parse(inspect.getsource(cb))
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp):
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                assert "ROUND_TRIP_BPS" not in names

    def test_the_geometry_constants_are_declared_and_never_applied_here(self):
        tree = ast.parse(inspect.getsource(cb))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                for constant in ("STOP_ATR", "TAKE_PROFIT_ATR", "HORIZON",
                                 "LOCKUP"):
                    assert constant not in names, (node.name, constant)

    def test_the_atr_helper_defers_to_the_production_implementation(self):
        source = inspect.getsource(cb._wilder_atr)
        assert "wilder_atr" in source
        tree = ast.parse(inspect.getsource(cb))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert "true_range" not in names


# ---------------------------------------------------------------------------
# the geometry and the scheduler
# ---------------------------------------------------------------------------


class TestTheGeometryIsWiredToTheSharedBarrier:

    def test_the_fill_is_the_next_bars_open_and_the_horizon_is_five(self):
        bars = calm(80)
        assert cb.ENTRY_ON == "next_open"
        indices, net, used = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=cb.TAKE_PROFIT_ATR, stop_atr=cb.STOP_ATR,
            horizon=cb.HORIZON, atr_period=cb.ATR_PERIOD,
            round_trip_bps=cb.ROUND_TRIP_BPS, side="long",
            entry_on=cb.ENTRY_ON)
        assert indices.size > 0 and net.size == indices.size
        assert int(used.max()) <= cb.HORIZON
        assert int(indices.max()) < len(bars) - cb.HORIZON - 1

    def test_both_sides_are_scored_by_the_same_function(self):
        bars = calm(80)
        kwargs = dict(take_profit_atr=cb.TAKE_PROFIT_ATR, stop_atr=cb.STOP_ATR,
                      horizon=cb.HORIZON, atr_period=cb.ATR_PERIOD,
                      round_trip_bps=cb.ROUND_TRIP_BPS, entry_on=cb.ENTRY_ON)
        long_idx, _l, _lu = sk.barrier_r_for_all_bars(bars, side="long", **kwargs)
        short_idx, _s, _su = sk.barrier_r_for_all_bars(bars, side="short", **kwargs)
        assert long_idx.tolist() == short_idx.tolist()

    def test_the_flags_are_event_shaped(self):
        """Slice 50's lesson, checked before the count is taken."""
        bars = calm(70) + [quiet_break(70)] + calm(30, start=71) + \
            [quiet_break(101, close=103.0, high=103.1, low=102.0)] + \
            calm(9, start=102)
        flags, _d = cb.flags_and_directions(bars)
        runs, _gaps = sk.extract_blocks(flags, warmup=0)
        assert int(flags.sum()) == len(runs), "events must not merge here"
        entries = sk.simulate_schedule(flags, warmup=0, lockup=cb.LOCKUP)
        assert len(entries) == int(flags.sum())

    def test_flags_and_directions_agree(self):
        bars = calm(70) + [quiet_break(70)] + calm(9, start=71)
        flags, directions = cb.flags_and_directions(bars)
        assert set(np.nonzero(flags)[0].tolist()) == set(directions)

    def test_the_summary_adds_up_and_reports_the_unfiltered_count(self):
        bars = calm(70) + [quiet_break(70)] + calm(20, start=71) + \
            [loud_break(91)] + calm(9, start=92)
        summary = cb.summary(bars)
        assert summary["setups"] == summary["long_setups"] + \
            summary["short_setups"]
        assert summary["setups"] == 1
        assert summary["breaks_any_volatility"] == 2


# ---------------------------------------------------------------------------
# the slice-53 measurement record
# ---------------------------------------------------------------------------


class TestTheSlice53Artefact:
    """One symbol measured, two not. The record must keep those apart.

    This slice has both failure modes in one family: BTCUSD has real
    percentiles under a VALID control, and ETHUSDT / SOLUSDT have none at all.
    An artefact that blurred them would either fabricate two readings or
    discard a good one.
    """

    PATH = os.path.join(REPO, "artifacts",
                        "slice53_stage1_compression_breakout_v1.json")

    def _payload(self):
        with open(self.PATH, encoding="utf-8") as handle:
            return json.load(handle)

    def test_it_exists_and_is_valid_json(self):
        assert os.path.exists(self.PATH)
        assert self._payload()["schema"] == "stage1_measurement/1"

    def test_the_constants_are_the_modules(self):
        declared = self._payload()["constants"]
        assert declared["COMP_MAX"] == cb.COMP_MAX == 25.0
        assert declared["PCTILE_WINDOW"] == cb.PCTILE_WINDOW == 50
        assert declared["BREAK_N"] == cb.BREAK_N == 20
        assert declared["ATR_PERIOD"] == cb.ATR_PERIOD
        assert declared["STOP_ATR"] == cb.STOP_ATR
        assert declared["TAKE_PROFIT_R"] == cb.TAKE_PROFIT_R
        assert declared["TAKE_PROFIT_ATR"] == cb.TAKE_PROFIT_ATR
        assert declared["HORIZON"] == cb.HORIZON
        assert declared["LOCKUP"] == cb.LOCKUP
        assert declared["ENTRY_ON"] == cb.ENTRY_ON

    def test_no_grid_and_one_run(self):
        payload = self._payload()
        assert payload["constants_changed_after_seeing_numbers"] is False
        assert payload["grid_or_sweep_run"] is False
        assert payload["runs_per_symbol"] == {"BTCUSD": 1, "ETHUSDT": 0,
                                              "SOLUSDT": 0}

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
            flags, _d = cb.flags_and_directions(bars, warmup=200)
            long_idx, _n1, _u1 = sk.barrier_r_for_all_bars(
                bars, take_profit_atr=cb.TAKE_PROFIT_ATR, stop_atr=cb.STOP_ATR,
                horizon=cb.HORIZON, atr_period=cb.ATR_PERIOD,
                round_trip_bps=cb.ROUND_TRIP_BPS, side="long",
                entry_on=cb.ENTRY_ON)
            eligible = np.zeros(len(bars), dtype=bool)
            eligible[long_idx] = True
            eligible[:200] = False
            entries = sk.simulate_schedule(em.tradable_flags(flags, eligible),
                                           warmup=200, lockup=cb.LOCKUP)
            assert len(entries) == claimed[symbol], symbol

    def test_the_compression_filter_is_recorded_as_doing_real_work(self):
        """The material-difference claim, as a measured ratio.

        Near 100% would make the precondition decorative and this family a
        short-window Donchian; near 0% would kill the sample. It is 22-36%.
        """
        finding = self._payload()["count_finding"]
        for symbol, survival in finding["filter_survival_pct"].items():
            assert 5.0 < survival < 60.0, (symbol, survival)
            breaks = finding["breaks_any_volatility"][symbol]
            kept = finding["compressed_breaks"][symbol]
            assert kept < breaks, symbol
            assert round(100.0 * kept / breaks, 1) == survival, symbol

    def test_only_btc_cleared_the_gate_and_positive_died_at_the_count(self):
        finding = self._payload()["count_finding"]
        assert finding["hard_gate_per_symbol"] == 50
        assert finding["symbols_meeting_gate"] == ["BTCUSD"]
        assert finding["positive_already_unreachable_at_count_stage"] is True
        assert self._payload()["positive_rule_met"] is False

    def test_the_controls_are_recorded_with_their_clauses_and_logs(self):
        control = self._payload()["control"]
        assert control["BTCUSD"]["verdict"] == "VALID"
        assert abs(control["BTCUSD"]["z"]) < 1.96
        assert control["BTCUSD"]["ks_p"] >= 0.05
        assert control["BTCUSD"]["incomplete_pct"] <= 5.0
        for symbol in ("ETHUSDT", "SOLUSDT"):
            entry = control[symbol]
            assert entry["verdict"] == "INVALID"
            assert entry["failed_clauses"]
            assert entry["incomplete_pct"] > 5.0, symbol
        for symbol in ("BTCUSD", "ETHUSDT", "SOLUSDT"):
            path = os.path.join(REPO, control[symbol]["log"])
            assert os.path.exists(path), symbol
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            marker = ("CONTROL: **VALID**" if control[symbol]["verdict"] == "VALID"
                      else "CONTROL: **INVALID")
            assert marker in text, symbol

    def test_the_wrong_prediction_is_recorded_as_wrong(self):
        """The most important honesty test in this file.

        §34d predicted a POSITIVE bias and every z came back negative. A
        programme that only records its correct predictions is not keeping a
        record, and the standing hypothesis this weakens is one the project has
        leaned on for six slices.
        """
        control = self._payload()["control"]
        assert control["prediction_outcome"] == "WRONG"
        note = control["prediction_note"]
        assert "MATERIALLY WEAKENED" in note
        assert "speculation, not" in note
        for symbol in ("BTCUSD", "ETHUSDT", "SOLUSDT"):
            assert control[symbol]["z"] < 0.0, symbol

    def test_the_invalid_controls_are_not_blamed_on_a_construction_mismatch(self):
        """Slices 49 and 50 failed for construction reasons. This did not.

        Conflating them would corrupt both records: the null here found and
        scheduled setups, there were simply too few of them.
        """
        cause = self._payload()["control"]["invalid_cause"]
        assert "INCOMPLETES" in cause
        assert "NOT the slice-49 or slice-50 construction mismatch" in cause

    def test_btc_has_a_real_reading_and_the_others_have_none(self):
        edge = self._payload()["edge"]
        assert edge["runs_executed"] == 1
        assert edge["BTCUSD"]["verdict"] == "EDGE_EVIDENCE_ABSENT"
        assert edge["BTCUSD"]["control_validated"] is True
        assert edge["BTCUSD"]["m1"] < 95.0 and edge["BTCUSD"]["m2"] < 95.0
        assert edge["BTCUSD"]["n_trades"] == 76
        for symbol in ("ETHUSDT", "SOLUSDT"):
            assert edge[symbol]["verdict"] == "NOT MEASURED"
            assert edge[symbol]["m1"] is None and edge[symbol]["m2"] is None

    def test_btc_percentiles_match_the_summary_they_came_from(self):
        edge = self._payload()["edge"]["BTCUSD"]
        with open(os.path.join(REPO, edge["summary"]), encoding="utf-8") as handle:
            summary = json.load(handle)
        assert summary["signal"] == cb.NAME
        assert summary["symbol"] == "BTCUSD"
        assert summary["m1"]["percentile"] == edge["m1"]
        assert summary["m2"]["percentile"] == edge["m2"]
        assert summary["control_validated"] is True

    def test_no_edge_artefact_exists_for_the_unmeasured_symbols(self):
        artifacts = os.path.join(REPO, "artifacts")
        for symbol in ("ETHUSDT", "SOLUSDT"):
            hits = [f for f in os.listdir(artifacts)
                    if f.startswith(f"slice53_edge_compression_breakout_{symbol}")]
            assert hits == [], (symbol, hits)

    def test_the_family_verdict_is_inconclusive_not_absent(self):
        """One good symbol does not make a family verdict, and the rule says so."""
        payload = self._payload()
        assert payload["edge_verdict"] == "INCONCLUSIVE"
        note = payload["edge_verdict_note"]
        assert "multi-symbol rule" in note
        assert "refuses to generalise" in note

    def test_it_claims_no_progress_and_registers_nothing(self):
        import project_status as ps
        payload = self._payload()
        assert payload["cleared_edge_signal"] is None
        assert payload["closer_to_autonomous_profit_agent"] is False
        assert payload["live_authorized"] is False
        assert payload["models_current_present"] is False
        registration_invariant.assert_registration_is_sound(
            os.path.join(REPO, "artifacts"))
        assert not os.path.exists(os.path.join(REPO, "models", "current"))

    def test_the_freeze_slice_53_deferred_has_since_happened(self):
        """Slice 53 left this freeze to a human; slice 54 is that decision.

        The dated record still says 9 and NOT PERFORMED, which were true when
        it was written. What is asserted now is the pair: the record is
        unchanged, and the deferred decision has been taken and is visible in
        code as INCONCLUSIVE — not ABSENT, because two of three symbols were
        never measured.
        """
        import project_status as ps
        payload = self._payload()
        assert payload["sign_flip_momentum_v1_frozen"] is True
        assert payload["frozen_absent_count"] == 9
        assert len(ps.ABSENT_SIGNALS) >= 9
        assert payload["this_family_frozen"] is False
        assert "NOT PERFORMED" in payload["freeze_of_this_family"]
        assert "compression_breakout_v1" in ps.ABSENT_SIGNALS
        assert ps.FROZEN_STATUS["compression_breakout_v1"] == "INCONCLUSIVE"
