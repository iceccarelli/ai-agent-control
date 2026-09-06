"""Tests for `funding_carry_fade_v1` — the slice-55 funding fade.

This is the first family in the programme whose trigger lives in a **different
series from the bars**, and that is where every plausible-but-wrong version of
it hides:

* read the funding print just *after* the decision bar's close and the rule has
  a full day of hindsight, while producing output that looks entirely normal;
* forward-fill across a missing print and the rule trades on a payment that may
  not have happened;
* get the millisecond scale wrong and the join silently finds nothing — the
  first version of the loader did exactly this and reported **zero setups on
  all three symbols**, which reads as a market finding rather than a unit bug;
* drop the funding cost and the fade collects the carry for free, flattering
  the strategy in the exact direction of its own thesis.

Each gets a decisive test. The lookahead boundary is attacked from both sides:
a print one millisecond after the close must not be used, one exactly at the
close must be.
"""
from __future__ import annotations

import ast
import csv
import datetime as dt
import gzip
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
from signals import funding_carry_fade_v1 as fc  # noqa: E402

DAY_MS = 86_400_000
EPOCH = int(dt.datetime(2022, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")


def bar(day, close=100.0, *, high=None, low=None, open_=None, vol=100.0):
    o = close if open_ is None else open_
    h = max(o, close) * 1.01 if high is None else high
    l = min(o, close) * 0.99 if low is None else low
    return bt.Bar(EPOCH + day * DAY_MS, o, h, l, close, vol)


def bars(n, *, close=100.0):
    return [bar(i, close) for i in range(n)]


def funding(pairs):
    """`[(epoch_ms, rate), ...]` -> FundingSeries."""
    return fc.FundingSeries([t for t, _r in pairs], [r for _t, r in pairs],
                            symbol="TEST")


def eight_hourly(n, rate, *, start_day=0):
    """`n` prints at 00:00, 08:00, 16:00 from `start_day`, all the same rate."""
    base = EPOCH + start_day * DAY_MS
    return [(base + i * 8 * 3_600_000, rate) for i in range(n)]


# ---------------------------------------------------------------------------
# the constants
# ---------------------------------------------------------------------------


class TestTheConstantsAreTheIntakes:
    """Frozen in NEW_SIGNAL_INTAKE.md and EDGE.md §36b before any number."""

    def test_every_declared_constant(self):
        assert fc.FUND_ABS == 0.0001
        assert fc.STOP_ATR == 1.5
        assert fc.TAKE_PROFIT_R == 1.0
        assert fc.HORIZON == 5
        assert fc.ATR_PERIOD == 14
        assert fc.LOCKUP == 1
        assert fc.ROUND_TRIP_BPS == 25.0
        assert fc.ENTRY_ON == "next_open"
        assert fc.NAME == "funding_carry_fade_v1"

    def test_one_r_of_take_profit_is_one_stop_distance(self):
        """The barrier's payoff is take_profit_atr / stop_atr.

        The intake fixes the STOP at 1.5 ATR and the target at 1.0 R, so the
        take-profit is 1.5 ATR. Writing 1.0 would leave the payoff at 0.67 and
        quietly change the risk unit — the trap slice 43 walked into.
        """
        assert fc.TAKE_PROFIT_ATR == 1.5
        assert fc.TAKE_PROFIT_ATR / fc.STOP_ATR == pytest.approx(1.0)

    def test_the_scheduler_constant_is_the_instruments(self):
        from signals import ts_momentum_v1 as ts
        assert fc.LOCKUP == ts.LOCKUP == 1


# ---------------------------------------------------------------------------
# the join — the dangerous part
# ---------------------------------------------------------------------------


class TestTheJoinNeverReadsTheFuture:
    """`f[t]` is the LAST print at or before `close_time(t)`. Both sides pinned."""

    def test_a_print_one_millisecond_after_the_close_is_not_used(self):
        """Decisive. This is the off-by-one that would buy a day of hindsight."""
        series = bars(3)
        close = fc.close_time_ms(series[0])
        f = funding([(close - 1000, 0.0),          # just before: usable
                     (close + 1, 0.9)])           # just after: must be ignored
        assert f.at_or_before(close) == 0.0
        assert fc.funding_at_decision(series, f)[0] == 0.0

    def test_a_print_exactly_at_the_close_is_used(self):
        """The other side of the boundary — `<=`, not `<`."""
        series = bars(3)
        close = fc.close_time_ms(series[0])
        f = funding([(close - 1000, 0.0), (close, 0.5)])
        assert f.at_or_before(close) == 0.5

    def test_the_last_print_wins_not_the_nearest(self):
        """A 'nearest' join would reach forward whenever the next print is closer."""
        series = bars(2)
        close = fc.close_time_ms(series[0])
        f = funding([(close - 10 * 3_600_000, 0.001),   # 10h before
                     (close + 60_000, 0.9)])           # 1 min after
        assert f.at_or_before(close) == 0.001

    def test_a_bar_with_no_prior_print_has_no_rate_and_no_setup(self):
        """No forward-fill, and no reaching forward either."""
        series = bars(4)
        later = fc.close_time_ms(series[2]) + 1
        f = funding([(later, 0.01)])
        rates = fc.funding_at_decision(series, f)
        assert np.isnan(rates[0]) and np.isnan(rates[1]) and np.isnan(rates[2])
        assert np.isfinite(rates[3])
        assert 0 not in fc.funding_setups(series, f)
        assert 1 not in fc.funding_setups(series, f)

    def test_truncating_the_funding_series_does_not_change_earlier_decisions(self):
        """The measurement-scale version of the same guarantee.

        Stated carefully, because the naive version is wrong: truncating the
        funding series is NOT like truncating a bar series. A bar whose close
        falls after the last surviving print legitimately changes — it now
        joins an older rate. What must never change is a decision at a bar
        whose close is at or before the last surviving print, because that
        decision could only ever have used prints that are still there.
        """
        series = bars(40)
        rng = np.random.default_rng(3)
        prints = [(t, float(rng.normal(0, 0.0004)))
                  for t, _r in eight_hourly(120, 0.0)]
        full = fc.funding_setups(series, funding(prints))
        for cut in (30, 60, 90):
            last_kept = prints[cut - 1][0]
            partial = fc.funding_setups(series, funding(prints[:cut]))
            decided = [t for t, b in enumerate(series)
                       if fc.close_time_ms(b) <= last_kept]
            assert decided, cut
            for index in decided:
                assert partial.get(index) == full.get(index), (cut, index)

    #: The functions that decide whether a bar is a setup and in which
    #: direction. Everything a trade's ENTRY depends on is computed here, so a
    #: forward index in any of them is lookahead.
    DECISION_PATH = ("funding_at_decision", "funding_setups",
                     "directed_signal_bars", "flags_and_directions")

    #: The functions that price a trade that has already been decided. A
    #: forward index here is the next-open fill and the exit bar — both of them
    #: pre-declared in EDGE.md §36b, neither of them available to the decision.
    COST_PATH = ("funding_paid_over_hold", "funding_adjusted_net_r")

    def _forward_subscripts(self, function_name):
        tree = ast.parse(inspect.getsource(fc))
        found = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef)
                    and node.name == function_name):
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Subscript):
                    continue
                if isinstance(inner.slice, ast.Slice):
                    continue
                if isinstance(inner.slice, ast.BinOp) and \
                        isinstance(inner.slice.op, ast.Add):
                    found.append(ast.dump(inner)[:160])
        return found

    def test_every_named_function_exists(self):
        """Otherwise a rename turns the two guards below into no-ops."""
        for name in self.DECISION_PATH + self.COST_PATH:
            assert hasattr(fc, name), name

    def test_the_decision_never_reads_a_forward_bar(self):
        """AST: no `x[i + k]` anywhere in the trigger path.

        NARROWED IN SLICE 55, AND WHY
        -----------------------------
        This was a blanket ban over the whole module, written at STEP 3 when the
        module held nothing but a trigger. STEP 4 added the funding cost that
        EDGE.md §36d **requires** — and that cost has to read `open[t+1]`, the
        next-open entry price the intake pre-declared, and the bar the position
        exits on. The blanket ban forbade the very arithmetic the design
        mandates. Same shape as §12c: a rule stated over a whole file
        eventually forbids the legitimate case.

        So the ban is scoped to the DECISION functions rather than dropped, and
        `test_the_cost_path_forward_reads_are_only_the_entry_and_the_exit`
        below pins what the cost path is allowed to do instead. Net effect is
        stricter, not looser: before, "the module" was one undifferentiated
        blob; now each function is held to the rule that actually applies to it.
        """
        for name in self.DECISION_PATH:
            found = self._forward_subscripts(name)
            assert not found, (name, found)

    def test_the_cost_path_forward_reads_are_only_the_entry_and_the_exit(self):
        """The cost path may look forward — but only at two pre-declared bars.

        `signal_index + 1` is the next-open fill (ENTRY_ON == "next_open") and
        `entry_index + bars_held` is the exit the barrier already resolved to.
        Nothing else, and no forward read at all outside these two functions.
        """
        for name in self.COST_PATH:
            for dumped in self._forward_subscripts(name):
                assert "signal_index" in dumped or "entry_index" in dumped, \
                    (name, dumped)

    def test_no_function_outside_those_two_lists_reads_forward(self):
        """The lists cannot be dodged by adding a third function."""
        tree = ast.parse(inspect.getsource(fc))
        known = set(self.DECISION_PATH) | set(self.COST_PATH)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name in known:
                continue
            assert not self._forward_subscripts(node.name), node.name


class TestTheTimeScaleIsRight:
    """The bug that produced zero setups on every symbol and looked like data."""

    def test_the_two_time_sources_agree_to_the_second(self):
        """The venue's ms column is authoritative; the ISO string is truncated.

        They differ by up to 30 ms on roughly 39% of rows, and no print in
        these corpora lands within two seconds of a daily close boundary, so
        neither choice could change a join. Preferring the source's own field
        means there is no discrepancy left to explain away.
        """
        import market_data as md
        path = os.path.join(
            REPO, "data/real_funding/funding",
            "BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz")
        with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))[:500]
        for row in rows:
            declared = int(row["funding_time_ms"])
            parsed = int(md.parse_timestamp(row["funding_time"])) // 1000
            assert abs(declared - parsed) <= 1000, row["funding_time"]

    def test_the_loader_uses_the_venues_millisecond_column(self):
        path = os.path.join(
            REPO, "data/real_funding/funding",
            "BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz")
        series = fc.load_funding(path)
        with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
            declared = [int(r["funding_time_ms"]) for r in csv.DictReader(handle)]
        assert series.times_ms == declared

    def test_real_bars_all_find_a_funding_print(self):
        """The regression this whole class exists for.

        The funding corpus starts one day before the bars, so every bar has a
        prior print. If this ever returns zeros again, the join is broken —
        not the market.
        """
        import market_data as md
        loaded, _b, _n = md.load_corpus(
            os.path.join(REPO, "data/real_linear_1d"), verify=False)
        for symbol in SYMBOLS:
            series = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close,
                             b.volume) for b in loaded[symbol]]
            f = fc.load_funding(os.path.join(
                REPO, "data/real_funding/funding",
                f"BINANCE_LINEAR_{symbol[:3]}_USDT_FUNDING.csv.gz"))
            rates = fc.funding_at_decision(series, f)
            assert np.isfinite(rates).all(), symbol

    def test_a_non_monotonic_funding_series_is_refused_at_construction(self):
        """The join is a binary search; unsorted input would return nonsense."""
        with pytest.raises(ValueError):
            fc.FundingSeries([3, 1, 2], [0.0, 0.0, 0.0])
        with pytest.raises(ValueError):
            fc.FundingSeries([1, 1], [0.0, 0.0])


# ---------------------------------------------------------------------------
# direction and threshold
# ---------------------------------------------------------------------------


class TestTheDirectionFadesTheCrowd:

    def test_rich_positive_funding_is_a_short(self):
        series = bars(3)
        f = funding([(fc.close_time_ms(series[0]) - 1000, +0.001)])
        assert fc.funding_setups(series, f)[0] == fc.SHORT_SETUP

    def test_rich_negative_funding_is_a_long(self):
        series = bars(3)
        f = funding([(fc.close_time_ms(series[0]) - 1000, -0.001)])
        assert fc.funding_setups(series, f)[0] == fc.LONG_SETUP

    def test_the_threshold_is_inclusive_on_both_sides(self):
        """`>=` and `<=`, exactly as the intake writes it.

        Asserted on bar 0 specifically rather than on the whole dict: with one
        print and three bars, that print is also the last one at or before bars
        1 and 2, so all three legitimately carry the same setup. That is the
        join working, not a leak.
        """
        series = bars(3)
        close = fc.close_time_ms(series[0])
        assert fc.funding_setups(series,
                                 funding([(close - 1, +fc.FUND_ABS)]))[0] \
            == fc.SHORT_SETUP
        assert fc.funding_setups(series,
                                 funding([(close - 1, -fc.FUND_ABS)]))[0] \
            == fc.LONG_SETUP

    def test_just_inside_the_threshold_does_not_fire(self):
        series = bars(3)
        close = fc.close_time_ms(series[0])
        for rate in (fc.FUND_ABS * 0.99, -fc.FUND_ABS * 0.99, 0.0):
            assert fc.funding_setups(series, funding([(close - 1, rate)])) == {}

    def test_the_side_symmetry_is_exact(self):
        """Negating every rate must swap every direction and nothing else."""
        series = bars(30)
        rng = np.random.default_rng(9)
        prints = [(t, float(rng.normal(0, 0.0005)))
                  for t, _r in eight_hourly(100, 0.0)]
        positive = fc.funding_setups(series, funding(prints))
        negated = fc.funding_setups(
            series, funding([(t, -r) for t, r in prints]))
        assert set(positive) == set(negated)
        swap = {fc.LONG_SETUP: fc.SHORT_SETUP, fc.SHORT_SETUP: fc.LONG_SETUP}
        for index, direction in positive.items():
            assert negated[index] == swap[direction], index


# ---------------------------------------------------------------------------
# funding as a cost
# ---------------------------------------------------------------------------


class TestFundingIsChargedNotIgnored:
    """The rule shorts when funding is rich. Ignoring it would pay the strategy."""

    def test_a_long_pays_positive_funding(self):
        series = bars(12)
        f = funding(eight_hourly(30, +0.001))
        cost = fc.funding_paid_over_hold(f, series, 0, 5, fc.LONG_SETUP)
        assert cost > 0.0

    def test_a_short_receives_positive_funding(self):
        series = bars(12)
        f = funding(eight_hourly(30, +0.001))
        cost = fc.funding_paid_over_hold(f, series, 0, 5, fc.SHORT_SETUP)
        assert cost < 0.0

    def test_the_two_sides_are_exact_negatives(self):
        series = bars(12)
        f = funding(eight_hourly(30, +0.0007))
        long_cost = fc.funding_paid_over_hold(f, series, 0, 5, fc.LONG_SETUP)
        short_cost = fc.funding_paid_over_hold(f, series, 0, 5, fc.SHORT_SETUP)
        assert long_cost == -short_cost

    def test_the_window_is_half_open_at_entry_and_closed_at_exit(self):
        """A print at the entry instant belongs to the previous holder.

        Hand-checked: entry is `open[1]`, so the exposure window starts at
        `close_time(0)`. A print exactly there is excluded; one exactly at the
        exit close is included.
        """
        series = bars(12)
        entry_moment = fc.close_time_ms(series[0])
        exit_moment = fc.close_time_ms(series[1 + 5])
        f = funding([(entry_moment, 0.5), (exit_moment, 0.25)])
        cost = fc.funding_paid_over_hold(f, series, 0, 5, fc.LONG_SETUP)
        assert cost == pytest.approx(0.25)

    def test_a_longer_hold_pays_more(self):
        series = bars(20)
        f = funding(eight_hourly(60, +0.0004))
        short_hold = fc.funding_paid_over_hold(f, series, 0, 1, fc.LONG_SETUP)
        long_hold = fc.funding_paid_over_hold(f, series, 0, 5, fc.LONG_SETUP)
        assert long_hold > short_hold > 0.0

    def test_no_prints_in_the_window_costs_nothing(self):
        series = bars(12)
        f = funding([(EPOCH - DAY_MS, 0.5)])
        assert fc.funding_paid_over_hold(f, series, 0, 5, fc.LONG_SETUP) == 0.0

    def test_an_unknown_direction_raises_rather_than_guessing(self):
        series = bars(12)
        f = funding(eight_hourly(30, 0.001))
        with pytest.raises(ValueError):
            fc.funding_paid_over_hold(f, series, 0, 5, "SIDEWAYS")


# ---------------------------------------------------------------------------
# material difference — the trigger contains no price at all
# ---------------------------------------------------------------------------


class TestTheTriggerContainsNoPrice:
    """The strongest material-difference claim this programme has been able to make."""

    def test_mangling_every_price_changes_no_setup(self):
        """Decisive. All ten frozen families would change; this does not."""
        series = bars(40)
        rng = np.random.default_rng(5)
        prints = [(t, float(rng.normal(0, 0.0005)))
                  for t, _r in eight_hourly(140, 0.0)]
        f = funding(prints)
        plain = fc.funding_setups(series, f)
        mangled_bars = [bt.Bar(b.start_ms, b.open * 7.0, b.high * 9.0,
                               b.low * 0.3, b.close * 5.0, b.volume)
                        for b in series]
        assert fc.funding_setups(mangled_bars, f) == plain
        assert plain, "the fixture must produce setups"

    def test_the_module_reads_no_price_field_in_its_trigger(self):
        """AST: the trigger path touches no OHLC attribute.

        `close_time_ms` reads `start_ms` and `end_us` — timing, not price — and
        that is the only bar attribute the decision path may touch.
        """
        for name in ("funding_at_decision", "funding_setups",
                     "directed_signal_bars", "flags_and_directions"):
            source = inspect.getsource(getattr(fc, name))
            for field in (".open", ".high", ".low", ".close"):
                assert field not in source, (name, field)

    def test_it_reads_no_other_signal_module(self):
        tree = ast.parse(inspect.getsource(fc))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        assert not any(name.startswith("signals") for name in imported), imported

    def test_it_uses_no_indicator_no_channel_and_no_return(self):
        tree = ast.parse(inspect.getsource(fc))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for forbidden in ("rsi", "macd", "adx", "bollinger", "atr_percentiles",
                          "channel", "donchian", "momentum_returns",
                          "range_location", "gap_values"):
            assert forbidden not in names, forbidden


# ---------------------------------------------------------------------------
# no forked R arithmetic
# ---------------------------------------------------------------------------


class TestTheRArithmeticIsNotForked:

    def test_the_module_computes_no_barrier_of_its_own(self):
        """No take-profit, no stop level, no realised payoff computed here.

        NARROWED IN SLICE 55: `net_r` left this list. It was banned as a NAME
        when the module had no cost function; STEP 4 added
        `funding_adjusted_net_r`, whose whole job is to take the barrier's own
        `net_r` array as a PARAMETER and add one charge to it. Banning the word
        forbade the design §36d mandates rather than the behaviour it warns
        against.

        The behaviour is pinned by the three tests below instead — that the
        module never calls the barrier, that its adjustment only ever modifies
        the array it was handed, and that the barrier itself is untouched.
        """
        tree = ast.parse(inspect.getsource(fc))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for forbidden in ("tp_level", "sl_level", "realised", "payoff",
                          "barrier_r", "stop_level", "take_profit_level"):
            assert forbidden not in names, forbidden

    def test_the_module_never_calls_the_shared_barrier(self):
        """It consumes the barrier's output; it must never invoke it.

        AST, not a text search. The first version of this test grepped the
        source for `barrier_r_for_all_bars` and went red on the module's own
        docstring — which EXPLAINS why it does not call it. That is EDGE.md
        §12c for the fifth time: a guard written as a text ban forces the code
        to stop explaining what it forbids. Ban the call, not the word.
        """
        tree = ast.parse(inspect.getsource(fc))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = (func.attr if isinstance(func, ast.Attribute)
                      else func.id if isinstance(func, ast.Name) else "")
            assert called != "barrier_r_for_all_bars", ast.dump(node)[:120]

    def test_that_guard_would_catch_a_real_call(self):
        """The control: a text ban and an AST ban differ, and this proves how."""
        tree = ast.parse("import skill_test as sk\n"
                         "x = sk.barrier_r_for_all_bars(bars)\n")
        hits = [n for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and getattr(n.func, "attr", None) == "barrier_r_for_all_bars"]
        assert hits

    def test_the_adjustment_starts_from_the_array_it_was_given(self):
        """A copy of `net_r`, one charge subtracted — never a fresh array.

        If this ever built its own values, the funding "adjustment" would be a
        second barrier wearing a cost's name, and the comparison against every
        prior EDGE.md number would be meaningless.
        """
        source = inspect.getsource(fc.funding_adjusted_net_r)
        assert "np.array(net_r, dtype=float, copy=True)" in source
        assert "adjusted[position] -=" in source
        assert "np.zeros" not in source

    def test_the_adjustment_changes_nothing_when_funding_is_zero(self):
        """The strongest form of "this is a cost, not a model"."""
        series = bars(60)
        prints = eight_hourly(200, 0.0)
        book = funding(prints)
        indices = np.arange(10, 40)
        net = np.linspace(-1.0, 1.0, indices.size)
        used = np.full(indices.size, 3)
        for tag in (fc.LONG_SETUP, fc.SHORT_SETUP):
            out = fc.funding_adjusted_net_r(series, book, indices, net, used,
                                            tag)
            assert np.allclose(out, net)

    def test_no_basis_point_cost_arithmetic_happens_here(self):
        """The 25 bps is the barrier's. Only FUNDING is computed in this module."""
        tree = ast.parse(inspect.getsource(fc))
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp):
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                assert "ROUND_TRIP_BPS" not in names

    #: `funding_adjusted_net_r` needs the risk distance to express a funding
    #: charge in R at all: `cost_R = fraction * entry / (stop_atr * atr)`. It
    #: reads STOP_ATR and ATR_PERIOD as keyword DEFAULTS, which the measurement
    #: path overrides with the same values it passes the barrier.
    GEOMETRY_EXEMPT = {"funding_adjusted_net_r": ("STOP_ATR", "ATR_PERIOD")}

    def test_the_geometry_constants_are_declared_and_never_applied_here(self):
        """No function invents the barrier's geometry.

        NARROWED IN SLICE 55, for the third time this class has met the same
        lesson: the ban was written at STEP 3 over a module that was a trigger
        and nothing else. Converting a funding rate into R REQUIRES the risk
        distance — there is no other way to put a cash flow on the barrier's
        scale — so the exemption is named, scoped to one function and two
        constants, and TAKE_PROFIT_ATR and LOCKUP stay banned everywhere.
        """
        tree = ast.parse(inspect.getsource(fc))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            allowed = self.GEOMETRY_EXEMPT.get(node.name, ())
            for constant in ("STOP_ATR", "TAKE_PROFIT_ATR", "ATR_PERIOD",
                             "LOCKUP"):
                if constant in allowed:
                    continue
                assert constant not in names, (node.name, constant)

    def test_the_exemption_is_not_a_blank_cheque(self):
        """Every exempt function must exist, and the ban must still bite.

        TAKE_PROFIT_ATR and LOCKUP are exempted NOWHERE — the take-profit and
        the scheduler are the barrier's and the instrument's, and this module
        must not reimplement either.
        """
        for name, allowed in self.GEOMETRY_EXEMPT.items():
            assert hasattr(fc, name), name
            assert set(allowed) <= {"STOP_ATR", "ATR_PERIOD"}, name
        exempted = {c for allowed in self.GEOMETRY_EXEMPT.values()
                    for c in allowed}
        assert "TAKE_PROFIT_ATR" not in exempted
        assert "LOCKUP" not in exempted

    def test_the_shared_barrier_was_not_modified(self):
        """Every number in EDGE.md depends on this function being byte-stable."""
        source = inspect.getsource(sk.barrier_r_for_all_bars)
        assert "funding" not in source.lower()


# ---------------------------------------------------------------------------
# scheduling and the real corpora
# ---------------------------------------------------------------------------


class TestSchedulingAndTheRealCorpora:

    def _real(self, symbol):
        import market_data as md
        loaded, _b, _n = md.load_corpus(
            os.path.join(REPO, "data/real_linear_1d"), verify=False)
        series = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
                  for b in loaded[symbol]]
        f = fc.load_funding(os.path.join(
            REPO, "data/real_funding/funding",
            f"BINANCE_LINEAR_{symbol[:3]}_USDT_FUNDING.csv.gz"))
        return series, f

    def test_the_last_bar_never_carries_a_setup(self):
        series = bars(5)
        f = funding(eight_hourly(20, 0.001))
        directed = fc.directed_signal_bars(series, f)
        assert all(i < len(series) - 1 for i, _d in directed)

    def test_warmup_bars_are_excluded(self):
        series = bars(30)
        f = funding(eight_hourly(100, 0.001))
        assert 5 in dict(fc.directed_signal_bars(series, f, warmup=0))
        assert 5 not in dict(fc.directed_signal_bars(series, f, warmup=10))

    def test_flags_and_directions_agree(self):
        series = bars(30)
        f = funding(eight_hourly(100, 0.001))
        flags, directions = fc.flags_and_directions(series, f)
        assert set(np.nonzero(flags)[0].tolist()) == set(directions)

    @pytest.mark.parametrize("symbol,setups,longs", [("BTCUSDT", 421, 6),
                                                     ("ETHUSDT", 415, 10),
                                                     ("SOLUSDT", 534, 98)])
    def test_the_real_setup_counts_are_stable(self, symbol, setups, longs):
        """Pinned so a join change fails here with a number, not in a run.

        The heavy skew toward SHORT is a fact about the sample, not a bug:
        perpetual funding is positive most of the time because leveraged longs
        dominate. Recording it stops a future reader treating the imbalance as
        evidence of a broken direction rule.

        BTCUSDT MOVED 417 -> 418 IN SLICE 73, 418 -> 419 IN SLICE 75 and
        419 -> 421 IN SLICE 76.
        The first movement was the first since the pin was written in slice
        55; it is now a RECURRING amendment, because every forward bar that
        stops being the corpus's last bar becomes a directed signal bar and
        adds one. That is the pin doing its job, not drifting: it moves by the
        number of bars APPENDED, for nameable bars, or something is wrong.
        Slices 73 and 75 each appended one and it moved by one; slice 76
        caught up TWO closed days (`2026-08-23` and `2026-08-24`), so
        `2026-08-22` and `2026-08-23` both stopped being last and it moved by
        TWO. The step size is the number of appended bars, not the constant
        one — slice 75's wording said "by exactly one" and that was true of
        every slice it had seen, which is how a true sentence becomes a wrong
        rule. EDGE.md §59f. The cause is recorded
        in `test_the_btc_movement_is_the_08_19_short` below rather than being
        absorbed by editing the number: `2026-08-19` close-joined at exactly
        FUND_ABS, and when `2026-08-20` closed it stopped being the corpus's
        last bar and became a directed signal bar. `long_setups` did not move,
        so the new one is a SHORT, which is what the forward artefacts say.
        EDGE.md §56e.
        """
        series, f = self._real(symbol)
        summary = fc.summary(series, f, warmup=200)
        assert summary["setups"] == setups
        assert summary["long_setups"] == longs
        assert summary["bars_without_funding"] == 0

    def test_the_btc_movement_is_the_08_19_short(self):
        """Why the pin moved, asserted not assumed. AMENDED BY SLICE 75.

        The NAME is kept: `STAGE1_VERDICT_SLICE73.md` cites it and a shipped
        verdict must not be made false by a rename (slice 74's rule). The
        claim it was written for is kept too and is permanent — 2026-08-19 is
        the bar that took the count to 418 — and the newest directed bar is
        now 2026-08-21, which took it to 419.

        This reads the CORPUS and no slice artefact, so it stays an
        independent witness. EDGE.md §56b, §56e, §58b.
        """
        series, f = self._real("BTCUSDT")
        directed = fc.directed_signal_bars(series, f, warmup=200)

        def stamp(index):
            return dt.datetime.fromtimestamp(
                series[index].start_ms / 1000.0,
                tz=dt.timezone.utc).strftime("%Y-%m-%d")

        by_date = {stamp(i): d for i, d in directed}
        # The bar this test is named for: still a directed SHORT, and the
        # 418th, which no later data can change.
        assert by_date["2026-08-19"] == fc.SHORT_SETUP
        assert [stamp(i) for i, _d in directed].index("2026-08-19") == 417
        # The newest, which is what moved the pin this slice.
        index, direction = directed[-1]
        assert stamp(index) == "2026-08-23"
        assert direction == fc.SHORT_SETUP
        assert index < len(series) - 1, (
            "a directed signal bar can never be the last bar: the fill is at "
            "the next bar's open")
        assert sum(1 for _i, d in directed if d == fc.LONG_SETUP) == 6
