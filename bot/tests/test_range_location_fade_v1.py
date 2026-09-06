"""Tests for `range_location_fade_v1` — the slice-46 range-location fade.

The signal's whole content is one ratio, which makes it unusually easy to get
subtly wrong in ways that still produce plausible numbers:

* include bar t in its own trailing range and `loc` is pinned near 1.0 or 0.0
  by arithmetic rather than by the market;
* invert the direction and it becomes a small, badly-parameterised Donchian;
* clip `loc` to [0, 1] and a close that gapped *through* the range becomes
  indistinguishable from one that merely touched its edge.

Each of those gets its own test, and the range arithmetic is checked against
hand-built series where the answer is computable by eye.
"""
from __future__ import annotations

import ast
import datetime as dt
import inspect
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
from signals import range_location_fade_v1 as rl  # noqa: E402

DAY_MS = 86_400_000
EPOCH = int(dt.datetime(2022, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)


def bar(day, close, *, high=None, low=None, open_=None, vol=100.0):
    o = close if open_ is None else open_
    h = max(o, close) if high is None else high
    l = min(o, close) if low is None else low
    return bt.Bar(EPOCH + day * DAY_MS, o, h, l, close, vol)


def box(n, *, high=110.0, low=90.0, close=100.0, start=0):
    """A flat corridor: every bar has the same high, low and close.

    The trailing range is exactly [low, high] and `loc` is exactly
    `(close - low) / (high - low)` — so any assertion about it is arithmetic,
    not a guess about the implementation.
    """
    return [bar(start + i, close, high=high, low=low) for i in range(n)]


# ---------------------------------------------------------------------------
# the constants
# ---------------------------------------------------------------------------


class TestTheConstantsAreTheIntakes:
    """Frozen in NEW_SIGNAL_INTAKE.md and EDGE.md §27a before any number."""

    def test_every_declared_constant(self):
        assert rl.RANGE_BARS == 20
        assert rl.UPPER == 0.90
        assert rl.LOWER == 0.10
        assert rl.ATR_PERIOD == 14
        assert rl.STOP_ATR == 1.5
        assert rl.TAKE_PROFIT_R == 1.0
        assert rl.HORIZON == 5
        assert rl.LOCKUP == 1
        assert rl.ROUND_TRIP_BPS == 25.0
        assert rl.ENTRY_ON == "next_open"
        assert rl.NAME == "range_location_fade_v1"

    def test_one_r_of_take_profit_is_one_stop_distance(self):
        """The barrier's payoff is take_profit_atr / stop_atr.

        The intake fixes the STOP at 1.5 ATR and the target at 1.0 R, so the
        take-profit is 1.5 ATR. Writing 1.0 would leave the payoff at 0.67 and
        quietly change the risk unit — the same trap as slice 43.
        """
        assert rl.TAKE_PROFIT_ATR == 1.5
        assert rl.TAKE_PROFIT_ATR / rl.STOP_ATR == pytest.approx(1.0)

    def test_the_thresholds_are_symmetric_about_a_half(self):
        assert rl.UPPER + rl.LOWER == pytest.approx(1.0)

    def test_the_horizon_is_five_and_the_window_is_twenty(self):
        """Copied constants from a neighbouring signal would be silent."""
        from signals import post_shock_fade_v1 as shock
        assert rl.HORIZON == 5
        assert rl.HORIZON != shock.HORIZON      # 3
        assert rl.RANGE_BARS == 20


# ---------------------------------------------------------------------------
# the range arithmetic — bar t is not in its own range
# ---------------------------------------------------------------------------


class TestBarTIsExcludedFromItsOwnRange:
    """The defect that would make this signal an arithmetic identity.

    If bar t contributed its own high to `range_high`, then a bar closing at a
    new high would have `loc == 1.0` *by construction*, and the signal would be
    firing on a tautology rather than on a market state.
    """

    def test_a_new_high_does_not_define_its_own_range(self):
        """Decisive. Bar 25 makes a new high far above the corridor.

        With bar t excluded, the range stays [90, 110] and `loc` is
        (150 - 90) / 20 = 3.0 — far outside [0, 1], which is exactly what a
        close that gapped through its range should read. Were bar t included,
        the range would become [90, 150] and `loc` would be exactly 1.0.
        """
        bars = box(25) + [bar(25, 150.0, high=150.0, low=95.0)]
        loc = rl.range_location(bars)
        assert loc[25] == pytest.approx(3.0)
        assert loc[25] != pytest.approx(1.0)

    def test_a_new_low_does_not_define_its_own_range_either(self):
        bars = box(25) + [bar(25, 30.0, high=95.0, low=30.0)]
        loc = rl.range_location(bars)
        assert loc[25] == pytest.approx((30.0 - 90.0) / 20.0)
        assert loc[25] < 0.0

    def test_the_window_is_exactly_the_twenty_prior_bars(self):
        """A 21st bar back must not reach the range.

        Bars 0-4 sit in a wide corridor; bars 5-24 in a narrow one. At t = 25
        the window is 5..24, so the wide bars are out of scope and the range is
        the narrow one.
        """
        bars = box(5, high=200.0, low=10.0) + box(20, high=110.0, low=90.0,
                                                  start=5)
        bars.append(bar(25, 100.0, high=101.0, low=99.0))
        loc = rl.range_location(bars)
        assert loc[25] == pytest.approx((100.0 - 90.0) / 20.0)

    def test_loc_is_undefined_before_the_window_is_full(self):
        loc = rl.range_location(box(30))
        assert np.all(np.isnan(loc[:rl.RANGE_BARS]))
        assert np.isfinite(loc[rl.RANGE_BARS])

    def test_a_zero_width_range_yields_no_location(self):
        """A range with no interior has no edge to be at."""
        bars = [bar(i, 100.0, high=100.0, low=100.0) for i in range(25)]
        loc = rl.range_location(bars)
        assert np.all(np.isnan(loc[20:]))
        assert rl.location_setups(bars) == {}

    def test_loc_outside_the_unit_interval_is_kept_not_clipped(self):
        """Clipping would erase "through the range" versus "at its edge"."""
        bars = box(25) + [bar(25, 150.0, high=150.0, low=95.0)]
        assert rl.range_location(bars)[25] > 1.0

    def test_the_arithmetic_matches_a_hand_computation(self):
        for close, expected in ((90.0, 0.0), (95.0, 0.25), (100.0, 0.5),
                                (108.0, 0.9), (110.0, 1.0)):
            bars = box(22) + [bar(22, close, high=110.0, low=90.0)]
            assert rl.range_location(bars)[22] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# the direction — the whole hypothesis
# ---------------------------------------------------------------------------


class TestTheDirectionIsAFade:
    """Top of range is a SHORT. Bottom is a LONG. Backwards is Donchian."""

    def test_a_close_at_the_top_is_a_short_setup(self):
        bars = box(22) + [bar(22, 109.0, high=110.0, low=100.0)]
        assert rl.location_setups(bars).get(22) == rl.SHORT_SETUP

    def test_a_close_at_the_bottom_is_a_long_setup(self):
        bars = box(22) + [bar(22, 91.0, high=100.0, low=90.0)]
        assert rl.location_setups(bars).get(22) == rl.LONG_SETUP

    def test_a_close_in_the_middle_is_nothing(self):
        bars = box(22) + [bar(22, 100.0, high=101.0, low=99.0)]
        assert 22 not in rl.location_setups(bars)

    @pytest.mark.parametrize("close,fires", [
        (107.9, False),   # loc 0.895
        (108.0, True),    # loc 0.900 — the boundary is inclusive
        (108.1, True),
        (92.1, False),    # loc 0.105
        (92.0, True),     # loc 0.100 — inclusive on this side too
        (91.9, True),
    ])
    def test_the_thresholds_are_inclusive_exactly_where_declared(self, close,
                                                                 fires):
        """`loc >= 0.90` and `loc <= 0.10`, checked at the boundary itself.

        On a [90, 110] range a close of 108.0 is loc 0.900 exactly. The intake
        writes `>=` and `<=`, so it fires; 107.9 is 0.895 and must not.
        """
        bars = box(22) + [bar(22, close, high=110.0, low=90.0)]
        assert (22 in rl.location_setups(bars)) is fires

    def test_the_mapping_is_symmetric(self):
        """A mirrored series must produce mirrored directions."""
        top = box(22) + [bar(22, 109.0, high=110.0, low=100.0)]
        bottom = box(22) + [bar(22, 91.0, high=100.0, low=90.0)]
        assert rl.location_setups(top).get(22) == rl.SHORT_SETUP
        assert rl.location_setups(bottom).get(22) == rl.LONG_SETUP

    def test_the_pathological_both_case_is_declared_and_honoured(self):
        """`upper <= lower` must yield NOTHING, not both directions.

        Unreachable with the frozen constants, but the intake writes the case
        out and an edit that inverted the thresholds would otherwise emit a
        setup on every bar.
        """
        bars = box(22) + [bar(22, 100.0, high=101.0, low=99.0)]
        assert rl.location_setups(bars, upper=0.1, lower=0.9) == {}


# ---------------------------------------------------------------------------
# material difference, asserted rather than asserted-about
# ---------------------------------------------------------------------------


class TestItIsNotOneOfTheFrozenFamilies:

    def test_it_fires_without_any_range_break(self):
        """The distinction from `donchian_breakout_v1`, made concrete.

        The close is inside the trailing range — no new high is made — and the
        signal still fires, in the opposite direction to a breakout.
        """
        bars = box(22) + [bar(22, 109.0, high=109.5, low=105.0)]
        loc = rl.range_location(bars)[22]
        assert 0.90 <= loc <= 1.0            # inside the range, at its top
        assert rl.location_setups(bars).get(22) == rl.SHORT_SETUP

    def test_a_quiet_grind_fires_this_but_not_the_shock_fade(self):
        """The distinction from `post_shock_fade_v1`, made concrete.

        A series drifting up ~0.6% a day ends at the top of its 20-day range
        with no ATR shock anywhere. Location fires; magnitude does not.
        """
        from signals import post_shock_fade_v1 as shock
        bars, price = [], 100.0
        for i in range(60):
            price *= 1.006
            bars.append(bar(i, price, high=price * 1.001, low=price * 0.999))
        located = rl.location_setups(bars)
        assert located, "the grind must reach a range extreme"
        assert all(d == rl.SHORT_SETUP for d in located.values())
        assert shock.shock_setups(bars) == {}

    def test_it_reads_no_other_instrument(self):
        """Structural, not textual: no second price series can reach it.

        Written as an AST check on the first attempt's second try. A raw-text
        ban on "btc" failed immediately — against the module's own docstring,
        which *explains* why this is not the cross-asset family. That is the
        sixth time this repository has learned that a text ban forces the code
        to stop explaining what it forbids (EDGE.md §12c). The invariant that
        actually matters is the signature: every public entry point takes one
        `bars` sequence and nothing else, and the module imports no other
        signal.
        """
        for name in ("range_location", "location_setups",
                     "directed_signal_bars", "flags_and_directions",
                     "summary"):
            signature = inspect.signature(getattr(rl, name))
            positional = [p for p in signature.parameters.values()
                          if p.kind is not p.KEYWORD_ONLY]
            assert [p.name for p in positional] == ["bars"], (name, positional)

        tree = ast.parse(inspect.getsource(rl))
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
        rng = np.random.default_rng(11)
        price, bars = 100.0, []
        for i in range(200):
            price *= float(np.exp(rng.normal(0, 0.02)))
            bars.append(bar(i, price, high=price * 1.01, low=price * 0.99))
        full = rl.location_setups(bars)
        for cut in (120, 160, 190):
            prefix = rl.location_setups(bars[:cut])
            for index, direction in prefix.items():
                assert full.get(index) == direction, (cut, index)

    def test_the_last_bar_never_carries_a_setup(self):
        bars = box(30) + [bar(30, 109.0, high=110.0, low=100.0)]
        directed = rl.directed_signal_bars(bars)
        assert all(index < len(bars) - 1 for index, _d in directed)

    def test_warmup_bars_are_excluded(self):
        bars = box(60) + [bar(60, 109.0, high=110.0, low=100.0)] + box(
            5, start=61)
        assert 60 in dict(rl.directed_signal_bars(bars, warmup=0))
        assert 60 not in dict(rl.directed_signal_bars(bars, warmup=100))

    def test_the_trigger_never_reads_a_forward_bar(self):
        """AST: no `bars[t + k]`. The fill is the barrier's job."""
        tree = ast.parse(inspect.getsource(rl))
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
        tree = ast.parse(inspect.getsource(rl))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for forbidden in ("tp_level", "sl_level", "realised", "payoff",
                          "barrier_r", "stop_level", "take_profit_level"):
            assert forbidden not in names, forbidden

    def test_no_cost_arithmetic_happens_here(self):
        tree = ast.parse(inspect.getsource(rl))
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp):
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                assert "ROUND_TRIP_BPS" not in names

    def test_the_atr_helper_defers_to_the_production_implementation(self):
        source = inspect.getsource(rl._atr)
        assert "wilder_atr" in source
        tree = ast.parse(inspect.getsource(rl))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert "true_range" not in names


# ---------------------------------------------------------------------------
# scheduling
# ---------------------------------------------------------------------------


class TestOneTradePerRun:

    def test_consecutive_extremes_yield_one_entry(self):
        bars = box(22) + [bar(22 + i, 109.0, high=110.0, low=100.0)
                          for i in range(4)] + box(3, start=26)
        flags, _d = rl.flags_and_directions(bars)
        entries = sk.simulate_schedule(flags, warmup=0, lockup=rl.LOCKUP)
        assert len([e for e in entries if 22 <= e <= 25]) <= 1

    def test_separated_extremes_yield_two_entries(self):
        bars = box(22)
        bars.append(bar(22, 109.0, high=110.0, low=100.0))
        bars += box(20, start=23)
        bars.append(bar(43, 109.0, high=110.0, low=100.0))
        bars += box(3, start=44)
        flags, _d = rl.flags_and_directions(bars)
        entries = sk.simulate_schedule(flags, warmup=0, lockup=rl.LOCKUP)
        assert len([e for e in entries if e in (22, 43)]) == 2

    def test_flags_and_directions_agree(self):
        bars = box(22) + [bar(22, 109.0, high=110.0, low=100.0)] + box(2,
                                                                       start=23)
        flags, directions = rl.flags_and_directions(bars)
        assert set(np.nonzero(flags)[0].tolist()) == set(directions)

    def test_the_summary_adds_up(self):
        bars = box(22) + [bar(22, 91.0, high=100.0, low=90.0)] + box(2,
                                                                     start=23)
        summary = rl.summary(bars)
        assert summary["setups"] == summary["long_setups"] + \
            summary["short_setups"]


# ---------------------------------------------------------------------------
# the real corpora — counts, not scores
# ---------------------------------------------------------------------------


class TestTheRealCorpora:
    """Pinned so a change to the rule or the data fails here, not in a run.

    No percentile is computed in this class. These are properties of the frozen
    rule and the registered corpora.
    """

    PATHS = {
        "BTCUSD": "data/real_1d/ohlcv/BITSTAMP_SPOT_BTC_USD_1D.csv.gz",
        "ETHUSDT": "data/real_multi_1d/ohlcv/BINANCE_SPOT_ETH_USDT_1D.csv.gz",
        "SOLUSDT": "data/real_multi_1d/ohlcv/BINANCE_SPOT_SOL_USDT_1D.csv.gz",
    }
    SETUPS = {"BTCUSD": 678, "ETHUSDT": 242, "SOLUSDT": 261}
    ENTRIES = {"BTCUSD": 281, "ETHUSDT": 112, "SOLUSDT": 117}

    def _bars(self, symbol):
        import market_data as md
        return md.load_ohlcv(os.path.join(REPO, self.PATHS[symbol])).bars

    @pytest.mark.parametrize("symbol", ["BTCUSD", "ETHUSDT", "SOLUSDT"])
    def test_the_setup_count_is_what_slice_46_recorded(self, symbol):
        assert rl.summary(self._bars(symbol),
                          warmup=200)["setups"] == self.SETUPS[symbol]

    @pytest.mark.parametrize("symbol", ["BTCUSD", "ETHUSDT", "SOLUSDT"])
    def test_every_symbol_clears_the_fifty_trade_design_gate(self, symbol):
        """The gate the intake set BEFORE implementation, checked after.

        Slice 43's design failed here — 45 / 10 / 7 setups meant two symbols
        could not even validate a control. This one clears the floor on all
        three with room, which is what makes the measurement legitimate rather
        than merely attempted.
        """
        bars = self._bars(symbol)
        flags, _d = rl.flags_and_directions(bars, warmup=200)
        entries = sk.simulate_schedule(flags, warmup=200, lockup=rl.LOCKUP)
        assert len(entries) == self.ENTRIES[symbol]
        assert len(entries) >= 50, symbol

    def test_at_least_two_symbols_are_measurable(self):
        """The intake's precondition for a POSITIVE being possible at all."""
        measurable = 0
        for symbol in self.PATHS:
            flags, _d = rl.flags_and_directions(self._bars(symbol), warmup=200)
            if len(sk.simulate_schedule(flags, warmup=200,
                                        lockup=rl.LOCKUP)) >= 50:
                measurable += 1
        assert measurable >= 2


# ---------------------------------------------------------------------------
# the control this construction needed, and what it said
# ---------------------------------------------------------------------------


class TestTheSameAssetControl:

    def test_the_surrogate_finds_its_own_range_extremes(self):
        """Why the cross-asset control does not apply.

        Shuffling moves the ranges and their extremes with the series, so a
        surrogate's observed schedule is its own flags — not a fixed date set
        held in place by a separate driver.
        """
        rng = np.random.default_rng(5)
        price, bars = 100.0, []
        for i in range(400):
            price *= float(np.exp(rng.normal(0, 0.02)))
            bars.append(bar(i, price, high=price * 1.02, low=price * 0.98))
        shuffled = sk.surrogate_series(bars, seed=9)
        assert set(rl.location_setups(bars)) != set(rl.location_setups(shuffled))

    def test_the_control_tool_uses_the_three_clause_rule_unchanged(self):
        import control_range_location_fade as crlf
        import control_directed as cd
        assert crlf.cd.evaluate_control is cd.evaluate_control
        assert cd.Z_ABS_MAX == 1.96
        assert cd.KS_MIN_P == 0.05
        assert cd.MAX_INCOMPLETE_SHARE == 0.05

    def test_both_same_asset_controls_score_through_the_same_functions(self):
        """The duplication is the runner; the arithmetic is shared.

        `control_range_location_fade.py` is a second file rather than a flag on
        slice 43's tool, so the part that could drift is pinned: both call the
        same scoring functions out of `edge_measurement`.
        """
        import control_post_shock_fade as cpsf
        import control_range_location_fade as crlf

        def called(module):
            tree = ast.parse(inspect.getsource(module))
            return {getattr(n.func, "attr", None)
                    for n in ast.walk(tree) if isinstance(n, ast.Call)}

        shared = {"score_schedule", "rotation_replicates", "tradable_flags",
                  "simulate_schedule", "barrier_r_for_all_bars",
                  "surrogate_series", "evaluate_control"}
        assert shared <= called(cpsf)
        assert shared <= called(crlf)

    def test_it_builds_flags_from_the_bars_it_is_given(self):
        import control_range_location_fade as crlf
        tree = ast.parse(inspect.getsource(crlf.build_book_and_flags))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", None) == "flags_and_directions"]
        assert len(calls) == 1
        assert any(getattr(a, "id", None) == "bars" for a in calls[0].args)


class TestTheSlice46Artefacts:
    """All three controls failed. The logs must stay unusable as attestations."""

    ART = os.path.join(REPO, "artifacts")
    SYMBOLS = ("BTCUSD", "ETHUSDT", "SOLUSDT")

    def _log(self, symbol):
        path = os.path.join(
            self.ART,
            f"slice46_control_range_location_fade_{symbol}_n1000.log")
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    @pytest.mark.parametrize("symbol", SYMBOLS)
    def test_the_control_is_invalid_and_cannot_attest(self, symbol):
        text = self._log(symbol)
        assert "CONTROL: **INVALID" in text
        assert "CONTROL: **VALID**" not in text

    @pytest.mark.parametrize("symbol", SYMBOLS)
    def test_the_failure_is_bias_not_lack_of_power(self, symbol):
        """0% incomplete: the instrument WAS exercised, a thousand times.

        Slice 43's controls failed because they could not be run. These ran
        fully and came back biased, which is a different and much stronger
        finding.
        """
        text = self._log(symbol)
        assert "0 of 1000 (0.0%)" in text
        assert "surrogates used      : 1000 of 1000" in text

    @pytest.mark.parametrize("symbol", SYMBOLS)
    def test_the_log_forbids_reporting_a_real_series_percentile(self, symbol):
        assert "No real-series percentile" in self._log(symbol)

    def test_no_slice_46_edge_artefact_was_produced(self):
        """The control ran first precisely so this stays empty."""
        import glob
        assert glob.glob(os.path.join(self.ART, "slice46_edge_*")) == []

    def test_nothing_was_registered(self):
        import project_status as project_status
        registration_invariant.assert_registration_is_sound(self.ART)
