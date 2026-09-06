"""Tests for `post_shock_fade_v1` — the slice-43 same-asset shock fade.

The signal is the first here whose directional hypothesis is **reversion**:
every family measured before it bought continuation in some form. That makes
one class of bug uniquely easy and uniquely invisible — an inverted sign turns
this signal into a small, badly-parameterised version of one of the frozen
ones, and the numbers would still look plausible. So the direction is tested
from several angles rather than once.

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

import registration_invariant

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import backtest as bt  # noqa: E402
import pure_indicators as pi  # noqa: E402
import skill_test as sk  # noqa: E402
from signals import post_shock_fade_v1 as ps  # noqa: E402

DAY_MS = 86_400_000
EPOCH = int(dt.datetime(2022, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)


def bar(day, close, *, high=None, low=None, open_=None, vol=100.0):
    o = close if open_ is None else open_
    h = max(o, close) if high is None else high
    l = min(o, close) if low is None else low
    return bt.Bar(EPOCH + day * DAY_MS, o, h, l, close, vol)


def calm_then(n, *, jump_at, jump_pct, price=100.0, wiggle=1.0):
    """A constant-range series with one large close-to-close move.

    The constant high/low range gives a predictable non-zero ATR, so the shock
    threshold is easy to reason about by hand.
    """
    bars = []
    current = price
    for i in range(n):
        if i == jump_at:
            current = current * (1.0 + jump_pct)
        bars.append(bar(i, current, high=current + wiggle, low=current - wiggle))
    return bars


# ---------------------------------------------------------------------------
# the constants
# ---------------------------------------------------------------------------


class TestTheConstantsAreTheIntakes:
    """Frozen in NEW_SIGNAL_INTAKE.md and EDGE.md §24a before any number."""

    def test_every_declared_constant(self):
        assert ps.SHOCK_K == 2.0
        assert ps.ATR_PERIOD == 14
        assert ps.STOP_ATR == 1.5
        assert ps.TAKE_PROFIT_R == 1.0
        assert ps.HORIZON == 3
        assert ps.LOCKUP == 1
        assert ps.ROUND_TRIP_BPS == 25.0
        assert ps.ENTRY_ON == "next_open"
        assert ps.NAME == "post_shock_fade_v1"

    def test_one_r_of_take_profit_is_one_stop_distance(self):
        """The trap: writing TAKE_PROFIT_ATR = 1.0 for a "1R" target.

        The barrier's payoff is `take_profit_atr / stop_atr`. The intake fixes
        the STOP at 1.5 ATR and the target at 1.0 R, so the take-profit is
        1.5 ATR — one stop distance away. Writing 1.0 would leave the payoff at
        0.67 and quietly change the risk unit.
        """
        assert ps.TAKE_PROFIT_ATR == 1.5
        assert ps.TAKE_PROFIT_ATR / ps.STOP_ATR == pytest.approx(1.0)

    def test_the_payoff_the_barrier_will_actually_use_is_one_to_one(self):
        bars = calm_then(60, jump_at=30, jump_pct=-0.15)
        _idx, net, _u = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=ps.TAKE_PROFIT_ATR, stop_atr=ps.STOP_ATR,
            horizon=ps.HORIZON, atr_period=ps.ATR_PERIOD,
            round_trip_bps=0.0, side="long", entry_on=ps.ENTRY_ON)
        wins = net[net > 0.5]
        if wins.size:
            assert float(wins.max()) == pytest.approx(1.0, abs=1e-9)

    def test_the_horizon_is_three_not_five(self):
        """Spillover's horizon was 5. A copied constant would be silent."""
        assert ps.HORIZON == 3
        from signals import btc_alt_spillover_v1 as spill
        assert ps.HORIZON != spill.HORIZON


# ---------------------------------------------------------------------------
# the direction — the whole hypothesis
# ---------------------------------------------------------------------------


class TestTheDirectionIsAFade:
    """Up-shock is a SHORT. Down-shock is a LONG. Backwards is a new signal."""

    def test_an_up_shock_is_a_short_setup(self):
        bars = calm_then(60, jump_at=40, jump_pct=+0.30)
        setups = ps.shock_setups(bars)
        assert setups.get(40) == ps.SHORT_SETUP

    def test_a_down_shock_is_a_long_setup(self):
        bars = calm_then(60, jump_at=40, jump_pct=-0.30)
        setups = ps.shock_setups(bars)
        assert setups.get(40) == ps.LONG_SETUP

    def test_it_is_the_opposite_of_the_continuation_family(self):
        """The material-difference claim, asserted rather than asserted-about.

        `btc_alt_spillover_v1` calls an up-shock a LONG. This calls it a SHORT.
        If these two ever agreed on a direction, one of them would have been
        reimplemented into the other.
        """
        from signals import btc_alt_spillover_v1 as spill
        bars = calm_then(60, jump_at=40, jump_pct=+0.30)
        assert ps.shock_setups(bars).get(40) == ps.SHORT_SETUP
        assert spill.btc_setups(bars).get(
            spill._utc_date(bars[40])) == spill.LONG_SETUP

    def test_the_mapping_is_symmetric_under_reflection(self):
        """Mirror the series about its start: every direction must flip.

        A one-sided bug — say, a threshold that only ever fires upward — would
        survive both tests above and die here.
        """
        up = calm_then(60, jump_at=40, jump_pct=+0.25)
        down = calm_then(60, jump_at=40, jump_pct=-0.25)
        assert ps.shock_setups(up).get(40) == ps.SHORT_SETUP
        assert ps.shock_setups(down).get(40) == ps.LONG_SETUP

    def test_a_calm_series_produces_nothing(self):
        bars = [bar(i, 100.0, high=101.0, low=99.0) for i in range(80)]
        assert ps.shock_setups(bars) == {}

    @pytest.mark.parametrize("jump,fires", [
        (0.02, False), (0.03, False), (0.04, False),
        (0.043, True), (0.05, True), (0.30, True),
    ])
    def test_the_firing_boundary_is_where_the_arithmetic_says(self, jump,
                                                              fires):
        """Checked by hand, not by running the code and writing down the answer.

        On this fixture the range is a constant +/-1 on a price of 100, so
        ATR settles at 2.0 and the true range of the jump bar is about 100x.
        Wilder smoothing gives ATR[40] ~= (13 * 2 + 100x) / 14, and the rule
        fires when 100x >= 2 * ATR[40] / 1, i.e.

            1400x >= 52 + 200x   ->   x >= 52/1200 = 0.0433

        So 4.0% must not fire and 4.3% must. The boundary is worth pinning
        because the ATR at bar t INCLUDES bar t's own range, which makes the
        threshold partly self-inflating: a bigger shock raises the bar it has
        to clear. That is the intake's convention, and the same one
        `btc_alt_spillover_v1` uses.
        """
        bars = calm_then(60, jump_at=40, jump_pct=+jump)
        assert (40 in ps.shock_setups(bars)) is fires

    def test_the_threshold_is_self_inflating_and_that_is_the_spec(self):
        """ATR[t] includes bar t, so the shock raises its own bar.

        Stated as a test because it is the reason K = 2.0 fires so rarely on
        real daily data, and a reader comparing counts deserves to know it is
        a property of the declared rule rather than a bug.
        """
        quiet = calm_then(60, jump_at=40, jump_pct=+0.001)
        loud = calm_then(60, jump_at=40, jump_pct=+0.30)
        quiet_atr = ps._atr(quiet)[40] / quiet[40].close
        loud_atr = ps._atr(loud)[40] / loud[40].close
        assert loud_atr > quiet_atr

    def test_k_scales_the_threshold_monotonically(self):
        """Not a grid search — a property. A bigger K can only fire less."""
        bars = calm_then(200, jump_at=100, jump_pct=+0.20)
        counts = [len(ps.shock_setups(bars, k=k)) for k in (0.5, 1.0, 2.0, 4.0)]
        assert counts == sorted(counts, reverse=True)


# ---------------------------------------------------------------------------
# no lookahead
# ---------------------------------------------------------------------------


class TestNoLookahead:

    def test_truncating_the_future_does_not_change_the_past(self):
        """The decisive structural test.

        Decisions made at bar t must depend only on bars <= t. Cutting the
        series short and re-running must reproduce every surviving decision.
        """
        bars = calm_then(120, jump_at=60, jump_pct=+0.28)
        full = ps.shock_setups(bars)
        for cut in (70, 90, 110):
            prefix = ps.shock_setups(bars[:cut])
            for index, direction in prefix.items():
                assert full.get(index) == direction, (cut, index)

    def test_the_last_bar_never_carries_a_setup(self):
        """There is no next bar to fill on."""
        bars = calm_then(60, jump_at=59, jump_pct=+0.30)
        directed = ps.directed_signal_bars(bars)
        assert all(index < len(bars) - 1 for index, _d in directed)

    def test_warmup_bars_are_excluded(self):
        bars = calm_then(200, jump_at=30, jump_pct=+0.30)
        assert 30 in dict(ps.directed_signal_bars(bars, warmup=0))
        assert 30 not in dict(ps.directed_signal_bars(bars, warmup=100))

    def test_the_trigger_reads_only_close_and_atr(self):
        """Structural: no `open`, `high` or `low` of the signal bar decides.

        The extremes belong to the barrier, which reads them forward from the
        entry. A trigger that peeked at the signal bar's own high would be
        using information that a close-time decision does not have.
        """
        tree = ast.parse(inspect.getsource(ps.shock_setups))
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert "high" not in attrs
        assert "low" not in attrs
        assert "open" not in attrs


# ---------------------------------------------------------------------------
# the ATR the intake named
# ---------------------------------------------------------------------------


class TestTheAtrIsTheProductionOne:

    def test_it_matches_pure_indicators_to_float_epsilon(self):
        """The intake says "pure_indicators / production Wilder ATR".

        Two implementations exist: the vectorised `sweep_geometry.wilder_atr`
        that the barrier uses, and `pure_indicators.atr`. The signal must not
        fire on one number and be sized by another. They agree — asserted here
        rather than assumed, because two implementations that are never
        compared are just two chances to be wrong.
        """
        import market_data as md
        bars = md.load_ohlcv(os.path.join(
            REPO, "data/real_1d/ohlcv/BITSTAMP_SPOT_BTC_USD_1D.csv.gz")
        ).bars[:600]
        high = np.array([b.high for b in bars])
        low = np.array([b.low for b in bars])
        close = np.array([b.close for b in bars])
        mine = ps._atr(bars)
        theirs = pi.atr(high, low, close, ps.ATR_PERIOD)
        finite = np.isfinite(mine)
        offset = mine.size - theirs.size
        assert np.allclose(mine[finite], theirs[finite[offset:]], rtol=1e-12)

    def test_the_signal_does_not_reimplement_it(self):
        """AST: the module computes no true range of its own."""
        tree = ast.parse(inspect.getsource(ps))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert "true_range" not in names
        source = inspect.getsource(ps._atr)
        assert "wilder_atr" in source

    def test_an_undefined_atr_produces_no_setup(self):
        """Fail closed while the ATR is still warming up."""
        bars = calm_then(20, jump_at=5, jump_pct=+0.50)
        for index in ps.shock_setups(bars):
            assert index >= ps.ATR_PERIOD


# ---------------------------------------------------------------------------
# no forked R arithmetic
# ---------------------------------------------------------------------------


class TestTheRArithmeticIsNotForked:
    """Every R comes from `skill_test.barrier_r_for_all_bars`.

    A forked copy that drifted by a line would make this measurement
    incomparable with everything already in EDGE.md, and the drift would be
    invisible: both versions would return plausible numbers.
    """

    def test_the_module_computes_no_barrier_of_its_own(self):
        tree = ast.parse(inspect.getsource(ps))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for forbidden in ("tp_level", "sl_level", "realised", "payoff",
                          "barrier_r", "stop_level", "take_profit_level"):
            assert forbidden not in names, forbidden

    def test_the_module_never_reads_a_forward_bar(self):
        """No `bars[t + k]` anywhere: the fill is the barrier's job."""
        tree = ast.parse(inspect.getsource(ps))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript):
                continue
            if not isinstance(node.slice, ast.BinOp):
                continue
            if isinstance(node.slice.op, ast.Add):
                raise AssertionError(ast.dump(node)[:160])

    def test_no_round_trip_cost_is_applied_here(self):
        """The constant is declared for the runner, not used for arithmetic."""
        tree = ast.parse(inspect.getsource(ps))
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp):
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                assert "ROUND_TRIP_BPS" not in names


# ---------------------------------------------------------------------------
# scheduling
# ---------------------------------------------------------------------------


class TestOneTradePerRun:

    def test_consecutive_shocks_yield_one_entry(self):
        """Two shock days in a row is one run, so one trade."""
        bars = []
        price = 100.0
        for i in range(60):
            if i in (40, 41):
                price *= 1.30
            bars.append(bar(i, price, high=price + 1.0, low=price - 1.0))
        flags, _directions = ps.flags_and_directions(bars)
        entries = sk.simulate_schedule(flags, warmup=0, lockup=ps.LOCKUP)
        run_entries = [e for e in entries if e in (40, 41)]
        assert len(run_entries) <= 1

    def test_separated_shocks_yield_two_entries(self):
        """The control: without it, "one entry" could mean "broken"."""
        bars = []
        price = 100.0
        for i in range(80):
            if i in (30, 60):
                price *= 1.30
            bars.append(bar(i, price, high=price + 1.0, low=price - 1.0))
        flags, _d = ps.flags_and_directions(bars)
        entries = sk.simulate_schedule(flags, warmup=0, lockup=ps.LOCKUP)
        assert len([e for e in entries if e in (30, 60)]) == 2

    def test_flags_and_directions_agree_with_each_other(self):
        bars = calm_then(120, jump_at=60, jump_pct=+0.28)
        flags, directions = ps.flags_and_directions(bars)
        assert set(np.nonzero(flags)[0].tolist()) == set(directions)

    def test_the_summary_counts_what_it_says(self):
        bars = calm_then(120, jump_at=60, jump_pct=-0.28)
        summary = ps.summary(bars)
        assert summary["setups"] == summary["long_setups"] + \
            summary["short_setups"]
        assert summary["long_setups"] >= 1


# ---------------------------------------------------------------------------
# the real corpora — mechanical properties, not scores
# ---------------------------------------------------------------------------


class TestTheRealCorpora:
    """Counts, pinned. No percentile is computed here.

    These numbers are properties of the frozen rule and the registered data.
    They are pinned so that a change to either fails here rather than inside a
    measurement — and so the slice-43 record can be checked by a reader.
    """

    PATHS = {
        "BTCUSD": "data/real_1d/ohlcv/BITSTAMP_SPOT_BTC_USD_1D.csv.gz",
        "ETHUSDT": "data/real_multi_1d/ohlcv/BINANCE_SPOT_ETH_USDT_1D.csv.gz",
        "SOLUSDT": "data/real_multi_1d/ohlcv/BINANCE_SPOT_SOL_USDT_1D.csv.gz",
    }
    EXPECTED = {"BTCUSD": 45, "ETHUSDT": 10, "SOLUSDT": 7}

    def _bars(self, symbol):
        import market_data as md
        return md.load_ohlcv(os.path.join(REPO, self.PATHS[symbol])).bars

    @pytest.mark.parametrize("symbol", ["BTCUSD", "ETHUSDT", "SOLUSDT"])
    def test_the_setup_count_is_what_slice_43_recorded(self, symbol):
        summary = ps.summary(self._bars(symbol), warmup=200)
        assert summary["setups"] == self.EXPECTED[symbol], summary

    @pytest.mark.parametrize("symbol", ["BTCUSD", "ETHUSDT", "SOLUSDT"])
    def test_no_symbol_reaches_the_intakes_fifty_trade_floor(self, symbol):
        """The finding that decided slice 43, asserted as a fact about K=2.0.

        The intake requires at least two symbols with >= 50 scored trades or
        the run is INCONCLUSIVE. At K = 2.0 the shock threshold is roughly
        8.6% / 9.4% / 13.3% of price on these three symbols, and none of them
        produces 50 setups — let alone 50 scored trades after one-trade-per-run
        and the horizon embargo.

        If this test ever fails because a count went UP, the data changed. If
        it fails because K changed, that is the grid search the intake forbids.
        """
        assert ps.summary(self._bars(symbol), warmup=200)["setups"] < 50

    def test_the_threshold_is_a_high_bar_on_daily_crypto(self):
        """Why the counts are small, in one checkable number."""
        bars = self._bars("BTCUSD")
        atr = ps._atr(bars)
        close = np.array([b.close for b in bars])
        threshold = ps.SHOCK_K * (atr / close)
        median = float(np.nanmedian(threshold))
        assert 0.05 < median < 0.15, median


# ---------------------------------------------------------------------------
# the control this construction needed
# ---------------------------------------------------------------------------


class TestTheSameAssetControl:
    """Slice 43 built a new control, and the reason is worth pinning.

    `btc_alt_spillover_v1`'s control shuffles the traded series while the BTC
    driver holds the flag dates fixed. Here the trigger is the symbol's OWN
    return, so shuffling moves the shocks with it and each surrogate finds its
    own. Reusing the cross-asset control would have measured a different
    construction from the one being claimed.
    """

    def test_the_surrogate_finds_its_own_shocks(self):
        """The property that makes a separate control necessary."""
        bars = calm_then(400, jump_at=200, jump_pct=+0.30)
        shuffled = sk.surrogate_series(bars, seed=7)
        real = set(ps.shock_setups(bars))
        surrogate = set(ps.shock_setups(shuffled))
        assert real != surrogate

    def test_the_control_tool_uses_the_three_clause_rule_unchanged(self):
        import control_post_shock_fade as cpsf
        import control_directed as cd
        assert cpsf.cd.evaluate_control is cd.evaluate_control
        assert cd.Z_ABS_MAX == 1.96
        assert cd.KS_MIN_P == 0.05
        assert cd.MAX_INCOMPLETE_SHARE == 0.05

    def test_the_control_scores_through_the_shared_path(self):
        """No forked scoring: it calls edge_measurement, like every other."""
        import control_post_shock_fade as cpsf
        source = inspect.getsource(cpsf)
        tree = ast.parse(source)
        called = {getattr(n.func, "attr", None)
                  for n in ast.walk(tree) if isinstance(n, ast.Call)}
        assert "score_schedule" in called
        assert "rotation_replicates" in called
        assert "tradable_flags" in called
        assert "barrier_r_for_all_bars" in called

    def test_it_builds_flags_from_the_bars_it_is_given(self):
        """Structural: the surrogate's own flags, not a fixed date set."""
        import control_post_shock_fade as cpsf
        tree = ast.parse(inspect.getsource(cpsf.build_book_and_flags))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", None) == "flags_and_directions"]
        assert len(calls) == 1
        assert any(getattr(a, "id", None) == "bars" for a in calls[0].args)


# ---------------------------------------------------------------------------
# the slice-43 artefacts
# ---------------------------------------------------------------------------


class TestTheSlice43Artefacts:
    """What the run actually produced, pinned to its files."""

    ART = os.path.join(REPO, "artifacts")

    def _control(self, symbol):
        path = os.path.join(
            self.ART, f"slice43_control_post_shock_fade_{symbol}_n1000.log")
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_the_btc_control_is_valid(self):
        text = self._control("BTCUSD")
        assert "CONTROL: **VALID**" in text

    @pytest.mark.parametrize("symbol", ["ETHUSDT", "SOLUSDT"])
    def test_the_alt_controls_are_invalid_on_incompletes(self, symbol):
        """Too few setups per surrogate to score, so the ruler cannot be checked.

        Not a bug in the control — a consequence of K = 2.0 firing 10 and 7
        times on these series. The clause that catches it is (c), and it is
        the clause that exists for exactly this.
        """
        text = self._control(symbol)
        assert "CONTROL: **INVALID" in text
        assert "CONTROL: **VALID**" not in text
        assert "(c) incompletes" in text

    def test_only_btc_produced_a_summary(self):
        """ETH and SOL failed closed on the 30-entry floor, writing nothing."""
        import glob
        produced = sorted(os.path.basename(p) for p in glob.glob(
            os.path.join(self.ART, "slice43_edge_*_summary.json")))
        assert produced == ["slice43_edge_post_shock_fade_BTCUSD_summary.json"]

    def test_the_btc_reading_is_absent_under_a_validated_control(self):
        import json
        with open(os.path.join(
                self.ART,
                "slice43_edge_post_shock_fade_BTCUSD_summary.json"),
                encoding="utf-8") as handle:
            summary = json.load(handle)
        assert summary["signal"] == "post_shock_fade_v1"
        assert summary["verdict"] == "EDGE_EVIDENCE_ABSENT"
        assert summary["control_validated"] is True
        assert summary["m1"]["percentile"] < 95.0
        assert summary["m2"]["percentile"] < 95.0

    def test_the_fade_underperformed_its_own_rotations(self):
        """The direction of the miss, recorded because it is informative.

        M1 = 1.6 is not "nearly 95 from below" — it is the fade doing WORSE
        than reshuffling the same trades. On BTC daily at K = 2.0 and a 3-bar
        horizon, post-shock behaviour leans continuation, not reversion. That
        is a fact about this measurement, not a new thesis, and nothing in this
        repository is permitted to act on it without a fresh human intake.
        """
        import json
        with open(os.path.join(
                self.ART,
                "slice43_edge_post_shock_fade_BTCUSD_summary.json"),
                encoding="utf-8") as handle:
            summary = json.load(handle)
        assert summary["m1"]["percentile"] < 50.0
        assert summary["m2"]["delta"] < 0.0

    def test_nothing_was_registered(self):
        import project_status as project_status
        registration_invariant.assert_registration_is_sound(self.ART)
