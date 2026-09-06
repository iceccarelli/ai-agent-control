"""`funding_carry_fade_btc_v1` — the BTC-only product and its out-of-sample cut.

WHAT THIS FILE IS GUARDING
==========================
The frozen family's trigger, join and cost model already have 47 tests in
`test_funding_carry_fade_v1.py` and are not re-tested here. What is new in this
product, and therefore what is tested here, is three things:

1. **that nothing was retuned.** This name exists because slice 55's BTC reading
   looked good and its companions did not. The only thing standing between that
   and a post-hoc rescue is that every constant, the join and the rule are
   identical to the frozen family's. Several tests assert that identity from
   both directions, so an edit to either module goes red;
2. **that the universe is one symbol, enforced.** The intake says ETHUSDT and
   SOLUSDT are "not measured, not registered, not optional diagnostics". Prose
   cannot enforce that;
3. **that the cut was frozen before anything was scored, and cannot move.**
   This is the load-bearing claim of the whole slice. It is checked by hash, by
   git ordering, and by the arithmetic of the calendar itself.
"""
from __future__ import annotations

import ast
import datetime as dt
import hashlib
import inspect
import json
import os
import subprocess
import sys

import numpy as np
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import backtest as bt  # noqa: E402
import skill_test as sk  # noqa: E402
from signals import funding_carry_fade_btc_v1 as fb  # noqa: E402
from signals import funding_carry_fade_v1 as base  # noqa: E402

ARTIFACTS = os.path.join(REPO, "artifacts")
FOLDS = os.path.join(ARTIFACTS, "funding_carry_fade_btc_v1_folds.json")
LOCK = os.path.join(ARTIFACTS, "slice57_folds_lock.log")

DAY_MS = 86_400_000
EPOCH = int(dt.datetime(2022, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def bar(day, close=100.0, *, high=None, low=None, open_=None, vol=100.0):
    o = close if open_ is None else open_
    h = max(o, close) * 1.01 if high is None else high
    lo = min(o, close) * 0.99 if low is None else low
    return bt.Bar(EPOCH + day * DAY_MS, o, h, lo, close, vol)


def bars(n, *, close=100.0):
    return [bar(i, close) for i in range(n)]


def funding(pairs):
    return fb.FundingSeries([t for t, _r in pairs], [r for _t, r in pairs],
                            symbol="BTCUSDT")


def eight_hourly(n, rate, *, start_day=0):
    base_ms = EPOCH + start_day * DAY_MS
    return [(base_ms + i * 8 * 3_600_000, rate) for i in range(n)]


# ===========================================================================
# 1 — nothing was retuned
# ===========================================================================


class TestTheConstantsAreTheIntakesAndTheFrozenFamilys:
    """The single most important group in this file.

    If any of these goes red, `funding_carry_fade_btc_v1` has stopped being the
    same rule on a smaller universe and become a variant — at which point the
    out-of-sample framing is decoration on a parameter search.
    """

    INTAKE = {
        "FUND_ABS": 0.0001,
        "STOP_ATR": 1.5,
        "TAKE_PROFIT_R": 1.0,
        "TAKE_PROFIT_ATR": 1.5,
        "HORIZON": 5,
        "ATR_PERIOD": 14,
        "LOCKUP": 1,
        "ROUND_TRIP_BPS": 25.0,
        "ENTRY_ON": "next_open",
    }

    def test_every_constant_matches_the_human_intake_table(self):
        assert fb.CONSTANTS == self.INTAKE

    def test_every_constant_matches_the_frozen_family(self):
        """Both directions: the table AND the module attributes."""
        for key, value in self.INTAKE.items():
            assert getattr(fb, key) == value, key
            assert getattr(base, key) == value, key

    def test_the_take_profit_is_derived_not_chosen(self):
        """Seventh slice this has had to be written down.

        The shared barrier's payoff is take_profit_atr / stop_atr. Writing the
        intake's 1.0 R into TAKE_PROFIT_ATR would give 0.67 and silently change
        the target.
        """
        assert fb.TAKE_PROFIT_ATR == fb.STOP_ATR * fb.TAKE_PROFIT_R == 1.5

    def test_fund_abs_was_not_lowered(self):
        """The specific retune the intake names and forbids."""
        assert fb.FUND_ABS == 0.0001
        assert fb.FUND_ABS == base.FUND_ABS

    def test_the_module_declares_where_it_came_from(self):
        assert fb.DERIVED_FROM == base.NAME == "funding_carry_fade_v1"
        assert fb.NAME == "funding_carry_fade_btc_v1"
        assert fb.NAME != base.NAME

    def test_the_frozen_familys_constants_were_not_edited_by_this_slice(self):
        assert (base.FUND_ABS, base.STOP_ATR, base.TAKE_PROFIT_R,
                base.TAKE_PROFIT_ATR, base.HORIZON, base.ATR_PERIOD,
                base.LOCKUP, base.ROUND_TRIP_BPS, base.ENTRY_ON) == \
            (0.0001, 1.5, 1.0, 1.5, 5, 14, 1, 25.0, "next_open")


class TestTheRuleIsNotReimplemented:
    """A second implementation is a second thing to drift.

    These assert the functions are the SAME OBJECTS, not merely equivalent, so
    there is no copy that could quietly diverge from the frozen family under a
    later edit.
    """

    SHARED = ("load_funding", "close_time_ms", "funding_at_decision",
              "funding_setups", "funding_paid_over_hold",
              "funding_adjusted_net_r", "directed_signal_bars",
              "flags_and_directions", "summary")

    @pytest.mark.parametrize("name", SHARED)
    def test_it_is_the_frozen_familys_own_function(self, name):
        assert getattr(fb, name) is getattr(base, name), name

    def test_the_setup_tags_are_shared_too(self):
        assert fb.LONG_SETUP is base.LONG_SETUP
        assert fb.SHORT_SETUP is base.SHORT_SETUP
        assert fb.FundingSeries is base.FundingSeries

    def test_this_module_computes_no_barrier_of_its_own(self):
        tree = ast.parse(inspect.getsource(fb))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for forbidden in ("tp_level", "sl_level", "realised", "payoff",
                          "barrier_r", "stop_level", "take_profit_level"):
            assert forbidden not in names, forbidden

    def test_this_module_never_calls_the_shared_barrier(self):
        """AST, not a text search — the docstring EXPLAINS that it does not
        call it, and a grep would go red on the explanation. EDGE.md §12c."""
        tree = ast.parse(inspect.getsource(fb))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = (func.attr if isinstance(func, ast.Attribute)
                      else func.id if isinstance(func, ast.Name) else "")
            assert called != "barrier_r_for_all_bars", ast.dump(node)[:120]

    def test_the_shared_barrier_still_knows_nothing_about_funding(self):
        source = inspect.getsource(sk.barrier_r_for_all_bars)
        assert "funding" not in source.lower()


# ===========================================================================
# 2 — the universe is one symbol, enforced
# ===========================================================================


class TestTheUniverseIsBtcOnly:

    def test_the_universe_is_exactly_one_symbol(self):
        assert fb.UNIVERSE == ("BTCUSDT",)
        assert len(fb.UNIVERSE) == 1

    def test_btc_is_accepted(self):
        assert fb.require_supported_symbol("BTCUSDT") == "BTCUSDT"

    @pytest.mark.parametrize("symbol", ["ETHUSDT", "SOLUSDT", "BTCUSD",
                                        "btcusdt", "BTC_USDT", "",
                                        "DOGEUSDT", "BTCUSDT "])
    def test_everything_else_is_refused(self, symbol):
        with pytest.raises(fb.UnsupportedSymbol):
            fb.require_supported_symbol(symbol)

    def test_the_refusal_names_the_frozen_family_it_is_not(self):
        """An operator who hits this should learn why, not just that."""
        with pytest.raises(fb.UnsupportedSymbol) as excinfo:
            fb.require_supported_symbol("ETHUSDT")
        message = str(excinfo.value)
        assert "ETHUSDT" in message
        assert "funding_carry_fade_v1" in message
        assert "FROZEN" in message

    def test_it_is_a_distinct_exception_type(self):
        """So a caller cannot swallow it alongside a parsing error."""
        assert issubclass(fb.UnsupportedSymbol, ValueError)
        assert fb.UnsupportedSymbol is not ValueError

    def test_there_is_no_way_to_widen_the_universe(self):
        """No setter, no argument, no environment variable.

        The intake's out-of-scope clause is only as good as the absence of a
        convenient way around it.
        """
        source = inspect.getsource(fb)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                args = [a.arg for a in node.args.args + node.args.kwonlyargs]
                assert "universe" not in args, node.name
        assert "os.environ" not in source
        assert "getenv" not in source


# ===========================================================================
# 3 — the cut was locked first, and cannot move
# ===========================================================================


class TestTheFoldCalendarWasLockedBeforeAnythingWasScored:

    def _folds(self):
        with open(FOLDS, encoding="utf-8") as handle:
            return json.load(handle)

    def test_the_calendar_and_its_lock_log_both_exist(self):
        assert os.path.exists(FOLDS)
        assert os.path.exists(LOCK)

    def test_the_calendar_still_hashes_to_what_was_locked(self):
        """The one check that makes 'frozen first' verifiable.

        Any edit to t_mid — however small, however well-intentioned — changes
        this hash and this goes red.
        """
        assert fb.folds_are_unmodified()
        digest = hashlib.sha256(open(FOLDS, "rb").read()).hexdigest()
        with open(LOCK, encoding="utf-8") as handle:
            assert digest in handle.read()

    def test_the_method_has_no_free_parameters(self):
        folds = self._folds()
        assert folds["method"] == "index_midpoint_50pct"
        assert folds["schema"] == "fold_calendar/1"
        assert folds["signal"] == fb.NAME
        assert folds["symbol"] == "BTCUSDT"

    def test_the_midpoint_arithmetic_is_checkable_from_the_file(self):
        """A reader should not have to trust the tool that wrote it."""
        folds = self._folds()
        total = folds["n_bars_total"]
        assert folds["mid_index"] == total // 2
        assert folds["n_bars_early"] == folds["mid_index"]
        assert folds["n_bars_late"] == total - folds["mid_index"]
        assert folds["n_bars_early"] + folds["n_bars_late"] == total
        # Genuinely a half, to within the odd bar.
        assert abs(folds["n_bars_early"] - folds["n_bars_late"]) <= 1

    def test_the_cut_lies_strictly_inside_the_series(self):
        folds = self._folds()
        assert folds["t0_ms"] < folds["t_mid_ms"] < folds["t1_ms"]

    def test_the_iso_stamps_agree_with_the_millisecond_stamps(self):
        folds = self._folds()
        for iso_key, ms_key in (("t0", "t0_ms"), ("t_mid", "t_mid_ms"),
                                ("t1", "t1_ms")):
            expected = dt.datetime.fromtimestamp(
                folds[ms_key] / 1000.0,
                tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            assert folds[iso_key] == expected, iso_key

    def test_it_pins_both_source_files_by_hash(self):
        folds = self._folds()
        for key in ("linear_bars", "funding"):
            entry = folds["sources"][key]
            path = os.path.join(REPO, entry["path"])
            assert os.path.exists(path), entry["path"]
            assert len(entry["sha256_uncompressed"]) == 64

    def test_the_pinned_hashes_still_match_the_measured_prefix_on_disk(self):
        """If a corpus were swapped, the locked cut would no longer be a cut of
        the data being measured — and that must be loud, not silent.

        Asserted against the **prefix** of each file at its pinned row count,
        not against the whole file. Slice 62 is when that distinction started
        to matter: a human supplied a genuine extension and the whole-file form
        went red without a defect anywhere. A whole-file digest can only say
        "different"; it cannot tell an append from a rewrite, which is the one
        thing this test exists to tell. See `tools/corpus_prefix.py` and
        EDGE.md §45c.

        **The invariant is not weakened.** Any edit to a measured byte still
        fails here, because the prefix digest still fails. Two clauses are
        ADDED: the corpus may not shrink, and every appended row must be
        strictly after `t1` — which catches a back-fill into the measured
        window, a contamination the old whole-file form never looked for.
        """
        import tools.corpus_prefix as cp  # noqa: PLC0415
        folds = self._folds()
        for key in ("linear_bars", "funding"):
            entry = folds["sources"][key]
            result = cp.check(entry["path"])
            assert result.prefix_sha256 == entry["sha256_uncompressed"], (
                f"{entry['path']}: MEASURED HISTORY HAS BEEN REWRITTEN")
            assert result.append_only, result.why_not()

    def test_the_two_records_that_pin_these_files_still_agree(self):
        """The fold calendar and the slice-55 eligibility artefact were written
        three slices apart by separate code paths and pin the same two digests.

        If they ever disagree, one of them has been edited, and neither can be
        trusted until a human says which. Cross-checking them is cheap and it
        is the only thing that catches a pin being moved to match a file.
        """
        import tools.corpus_prefix as cp  # noqa: PLC0415
        agreement = cp.pinned_digests_agree()
        assert agreement, "no files were compared"
        assert all(agreement.values()), agreement

    def test_it_records_that_the_cut_may_not_move(self):
        folds = self._folds()
        assert "may not be moved" in folds["forbidden"]
        assert "INCONCLUSIVE" in folds["forbidden"]
        assert "40" in folds["forbidden"]

    def test_it_records_that_the_cut_used_no_scores(self):
        folds = self._folds()
        note = folds["method_note"]
        assert "TIMESTAMPS AND INDEX POSITION ONLY" in note
        assert "not from returns" in note

    def test_git_ordering_puts_the_lock_before_the_signal_module(self):
        """The claim 'locked before anything was scored', checked in history.

        Skipped rather than failed outside a git tree — the property is real
        but a packaged copy cannot demonstrate it.
        """
        def first_commit(path):
            out = subprocess.run(
                ["git", "log", "--diff-filter=A", "--format=%H", "--", path],
                cwd=REPO, capture_output=True, text=True)
            if out.returncode != 0 or not out.stdout.strip():
                return None
            return out.stdout.strip().splitlines()[-1]

        def order(commit):
            out = subprocess.run(
                ["git", "rev-list", "--count", commit],
                cwd=REPO, capture_output=True, text=True)
            return int(out.stdout.strip()) if out.returncode == 0 else None

        folds_commit = first_commit("artifacts/funding_carry_fade_btc_v1_folds.json")
        module_commit = first_commit("signals/funding_carry_fade_btc_v1.py")
        if not folds_commit or not module_commit:
            pytest.skip("not a git tree with this history")
        if folds_commit == module_commit:
            # SLICE 58: this went RED, and the defect was in the test.
            #
            # A packaged tree that is unzipped and `git init`-ed afresh has ONE
            # commit containing both paths. That is not evidence the ordering
            # was violated — it is the ABSENCE of evidence either way, because
            # the history that carried the proof was not shipped in the zip.
            # Asserting on it would fail an honest tree for the crime of having
            # been repackaged, and a test that cries wolf gets edited rather
            # than heeded.
            #
            # The ordering claim does not rest on this test alone. The folds
            # file is hashed into artifacts/slice57_folds_lock.log, that hash is
            # asserted on every run by
            # `test_the_calendar_still_hashes_to_what_was_locked`, and the
            # slice-57 commit ids are recorded in
            # artifacts/slice58_shadow_pack.json for anyone with the original
            # history.
            pytest.skip(
                "history squashed or re-initialised: both paths were added in "
                "one commit, so ordering is not observable here")
        assert order(folds_commit) < order(module_commit), (
            "the fold calendar must be committed BEFORE the module that "
            "consumes it")

    def test_the_module_cannot_compute_a_cut_only_read_one(self):
        """No `compute_folds` here, deliberately: a module that could
        re-derive the cut is one that could re-derive it after seeing a
        count."""
        assert not hasattr(fb, "compute_folds")
        assert not hasattr(fb, "make_folds")
        source = inspect.getsource(fb)
        assert "//2" not in source.replace(" ", "")

    def test_a_tampered_calendar_is_detected(self, tmp_path):
        folds = self._folds()
        folds["t_mid_ms"] = folds["t_mid_ms"] + DAY_MS
        path = tmp_path / "folds.json"
        path.write_text(json.dumps(folds), encoding="utf-8")
        assert not fb.folds_are_unmodified(str(path), LOCK)

    def test_a_calendar_for_another_signal_is_refused(self, tmp_path):
        folds = self._folds()
        folds["signal"] = "something_else"
        path = tmp_path / "folds.json"
        path.write_text(json.dumps(folds), encoding="utf-8")
        with pytest.raises(ValueError):
            fb.load_folds(str(path))

    def test_a_calendar_with_a_different_method_is_refused(self, tmp_path):
        folds = self._folds()
        folds["method"] = "whatever_gave_the_nicer_answer"
        path = tmp_path / "folds.json"
        path.write_text(json.dumps(folds), encoding="utf-8")
        with pytest.raises(ValueError):
            fb.load_folds(str(path))


class TestTheLateWindowSelection:

    def test_t_mid_itself_is_late(self):
        """The intake's brackets: early is [t0, t_mid), late is [t_mid, t1]."""
        folds = fb.load_folds()
        cut = folds["t_mid_ms"]
        assert fb.in_late_window(bt.Bar(cut, 1, 1, 1, 1, 0), folds)
        assert not fb.in_late_window(bt.Bar(cut - 1, 1, 1, 1, 1, 0), folds)

    def test_the_late_indices_are_contiguous_and_correctly_sized(self):
        folds = fb.load_folds()
        series = [bt.Bar(folds["t0_ms"] + i * DAY_MS, 1, 1, 1, 1, 0)
                  for i in range(folds["n_bars_total"])]
        late = fb.late_window_indices(series, folds)
        assert late == tuple(range(folds["mid_index"], folds["n_bars_total"]))
        assert len(late) == folds["n_bars_late"]

    def test_restricting_flags_zeroes_only_the_early_ones(self):
        folds = fb.load_folds()
        series = [bt.Bar(folds["t0_ms"] + i * DAY_MS, 1, 1, 1, 1, 0)
                  for i in range(folds["n_bars_total"])]
        flags = np.ones(len(series), dtype=bool)
        restricted = fb.restrict_flags_to_late(flags, series, folds)
        assert restricted[folds["mid_index"]:].all()
        assert not restricted[:folds["mid_index"]].any()
        assert int(restricted.sum()) == folds["n_bars_late"]

    def test_restricting_does_not_mutate_the_input(self):
        """The full-sample flags are reused by the diagnostic path."""
        folds = fb.load_folds()
        series = [bt.Bar(folds["t0_ms"] + i * DAY_MS, 1, 1, 1, 1, 0)
                  for i in range(folds["n_bars_total"])]
        flags = np.ones(len(series), dtype=bool)
        fb.restrict_flags_to_late(flags, series, folds)
        assert flags.all(), "restrict_flags_to_late mutated its argument"

    def test_it_restricts_the_decision_not_the_history(self):
        """The distinction that makes the OOS number comparable.

        Slicing the bar array would re-warm the ATR at t_mid and change the
        first several trades' geometry. Restricting the FLAGS leaves every
        indicator reading the same history it read in the full-sample run and
        withholds only the entry decision.
        """
        source = inspect.getsource(fb.restrict_flags_to_late)
        assert "bars[" not in source.replace(" ", "")
        folds = fb.load_folds()
        series = [bt.Bar(folds["t0_ms"] + i * DAY_MS, 1, 1, 1, 1, 0)
                  for i in range(folds["n_bars_total"])]
        restricted = fb.restrict_flags_to_late(
            np.ones(len(series), dtype=bool), series, folds)
        assert len(restricted) == len(series), \
            "the restricted flag array must still span the whole history"


# ===========================================================================
# 4 — the rule itself, on fixtures (join, thresholds, scheduler)
# ===========================================================================


class TestTheRuleThroughThisModulesNamespace:
    """The frozen family is tested in its own file; these run the same
    functions through this module, so a future decision to stop re-exporting
    and start copying would be caught here."""

    def test_the_thresholds_are_the_intakes(self):
        series = bars(6)
        rich = funding([(EPOCH + i * DAY_MS + 1, 0.0002) for i in range(6)])
        setups = fb.funding_setups(series, rich)
        assert set(setups.values()) == {fb.SHORT_SETUP}

        cheap = funding([(EPOCH + i * DAY_MS + 1, -0.0002) for i in range(6)])
        assert set(fb.funding_setups(series, cheap).values()) == {fb.LONG_SETUP}

    def test_a_rate_inside_the_band_is_no_setup(self):
        series = bars(6)
        flat = funding([(EPOCH + i * DAY_MS + 1, 0.00005) for i in range(6)])
        assert fb.funding_setups(series, flat) == {}

    def test_the_threshold_is_inclusive_at_exactly_fund_abs(self):
        series = bars(3)
        exact = funding([(EPOCH + i * DAY_MS + 1, fb.FUND_ABS)
                         for i in range(3)])
        assert set(fb.funding_setups(series, exact).values()) == \
            {fb.SHORT_SETUP}

    def test_missing_funding_means_no_setup(self):
        """No forward-fill: a rate carried forward is a guess about a payment
        that may not have happened."""
        series = bars(6)
        late_only = funding([(EPOCH + 5 * DAY_MS + 1, 0.0009)])
        setups = fb.funding_setups(series, late_only)
        assert all(index >= 5 for index in setups)

    def test_a_print_after_the_close_cannot_decide_that_bar(self):
        """The off-by-one that would be a full day of hindsight in a costume."""
        series = bars(4)
        close_ms = fb.close_time_ms(series[2])
        just_after = funding([(close_ms + 1000, 0.0050)])
        assert 2 not in fb.funding_setups(series, just_after)

    def test_truncating_funding_leaves_earlier_decisions_unchanged(self):
        series = bars(40)
        prints = eight_hourly(120, 0.0)
        prints = [(t, 0.0003 if i % 3 else -0.0003)
                  for i, (t, _r) in enumerate(prints)]
        full = fb.funding_setups(series, funding(prints))
        for cut in (30, 60, 90):
            last_kept = prints[cut - 1][0]
            partial = fb.funding_setups(series, funding(prints[:cut]))
            decided = [t for t, b in enumerate(series)
                       if fb.close_time_ms(b) <= last_kept]
            assert decided, cut
            for index in decided:
                assert partial.get(index) == full.get(index), (cut, index)

    def test_one_trade_per_contiguous_run(self):
        """Slice 17's rule, on a fixture, through this module.

        Rich funding persists for days, so this is where most of the sample
        goes and a reader should see it demonstrated rather than described.
        """
        flags = np.zeros(40, dtype=bool)
        flags[10:20] = True          # one run of ten
        flags[30:33] = True          # one run of three
        entries = sk.simulate_schedule(flags, warmup=5, lockup=fb.LOCKUP)
        assert entries == [10, 30], entries

    def test_the_funding_adjustment_is_zero_when_funding_is_zero(self):
        """The strongest form of 'this is a cost, not a model'."""
        series = bars(60)
        book = funding(eight_hourly(200, 0.0))
        indices = np.arange(10, 40)
        net = np.linspace(-1.0, 1.0, indices.size)
        used = np.full(indices.size, 3)
        for tag in (fb.LONG_SETUP, fb.SHORT_SETUP):
            out = fb.funding_adjusted_net_r(series, book, indices, net, used,
                                            tag)
            assert np.allclose(out, net)

    def test_a_short_receives_positive_funding(self):
        """Sign convention. Getting it backwards would flatter this rule
        exactly where it enters most often."""
        series = bars(60)
        book = funding(eight_hourly(200, 0.0005))
        # Start past the ATR(14) warm-up: before it, `risk` is not finite and
        # the adjustment is correctly skipped rather than applied to a
        # meaningless denominator.
        indices = np.arange(20, 50)
        net = np.zeros(indices.size)
        used = np.full(indices.size, 3)
        short = fb.funding_adjusted_net_r(series, book, indices, net, used,
                                          fb.SHORT_SETUP)
        long_ = fb.funding_adjusted_net_r(series, book, indices, net, used,
                                          fb.LONG_SETUP)
        assert (short > 0).all()
        assert (long_ < 0).all()
        assert np.allclose(short, -long_)


# ===========================================================================
# 5 — registration posture (this name is NOT frozen, and must not auto-clear)
# ===========================================================================


class TestRegistrationPosture:

    def test_this_name_is_not_on_the_deny_list(self):
        """It is new. If it were frozen this slice could not measure it."""
        import project_status as ps
        assert fb.NAME not in ps.ABSENT_SIGNALS

    def test_the_family_it_derives_from_is_still_frozen(self):
        import project_status as ps
        assert base.NAME in ps.ABSENT_SIGNALS
        assert ps.FROZEN_STATUS[base.NAME] == "ABSENT"

    def test_the_slice55_btc_artefact_is_untouched_and_still_positive(self):
        """It may not register anything, and it may not be edited either."""
        path = os.path.join(
            ARTIFACTS, "slice55_edge_funding_carry_fade_BTCUSDT_summary.json")
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        assert data["verdict"] == "EDGE_EVIDENCE_POSITIVE"
        assert round(data["m1"]["percentile"], 1) == 97.0
        assert round(data["m2"]["percentile"], 1) == 97.5
        assert data["observed"]["n_trades"] == 85
        assert data["signal"] == base.NAME != fb.NAME

    def test_a_slice55_artefact_cannot_register_this_new_name(self):
        """The specific forgery this slice's framing invites: relabel the old
        POSITIVE with the new product's name."""
        import tempfile

        import project_status as ps
        path = os.path.join(
            ARTIFACTS, "slice55_edge_funding_carry_fade_BTCUSDT_summary.json")
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        data["signal"] = fb.NAME          # the relabel
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "x_summary.json"), "w",
                      encoding="utf-8") as handle:
                json.dump(data, handle)
            # It WOULD register — which is precisely why the gate is the OOS
            # artefact and not any summary that happens to be on disk. The
            # protection is that this slice writes exactly one registering
            # artefact and it is the OOS one; this test pins the shape of the
            # hole so a later reader can see it was considered.
            result = ps.cleared_edge_signal_from_artifacts(tmp)
            assert result in (None, fb.NAME)
            if result == fb.NAME:
                # then the ONLY defence is that no such file exists in the real
                # artifacts directory. Assert that directly.
                real = [n for n in os.listdir(ARTIFACTS)
                        if n.endswith("_summary.json")]
                for name in real:
                    with open(os.path.join(ARTIFACTS, name),
                              encoding="utf-8") as handle:
                        on_disk = json.load(handle)
                    if on_disk.get("signal") == fb.NAME:
                        assert "oos" in name.lower(), (
                            f"{name} claims {fb.NAME} but is not the OOS "
                            "artefact; only the OOS run may register")


# ===========================================================================
# 6 — the OOS gate is the ONLY thing that registers (slice 57 STEP 6)
# ===========================================================================


def _oos_summary():
    with open(os.path.join(ARTIFACTS,
                           "slice57_oos_edge_BTCUSDT_summary.json"),
              encoding="utf-8") as handle:
        return json.load(handle)


class TestTheOosGateIsTheOnlyRegisteringSource:
    """The programme's first cleared edge, and the guard that makes it mean
    something.

    `artifacts/` now contains THREE artefacts for this rule that report
    EDGE_EVIDENCE_POSITIVE:

      * `slice55_edge_funding_carry_fade_BTCUSDT_summary.json` — 97.0 / 97.5,
        full sample, the FROZEN family's name;
      * `slice57_diagnostic_fullsample_BTCUSDT_summary.json` — the same
        numbers under the NEW name, full sample;
      * `slice57_oos_edge_BTCUSDT_summary.json` — 95.13 / 96.0 on the late
        half.

    Only the third may register, and the human intake says so in a table with
    two rows reading NO. Slice 55 proved that such a rule enforces nothing
    while it lives in prose. These tests are the enforcement.
    """

    def test_the_gate_is_declared_in_code(self):
        import project_status as ps
        folds_rel, floor = ps.OOS_GATED_REGISTRATION[fb.NAME]
        assert folds_rel == "artifacts/funding_carry_fade_btc_v1_folds.json"
        assert floor == 40

    def test_the_oos_artefact_declares_everything_the_gate_requires(self):
        d = _oos_summary()
        assert d["signal"] == fb.NAME
        assert d["window"] == "oos_late"
        assert d["registration_eligible"] is True
        assert d["oos_folds_sha256"] == fb.folds_sha256()
        assert d["control_validated"] is True

    def test_all_five_intake_clauses_are_recorded_and_met(self):
        d = _oos_summary()
        clauses = d["gate_clauses"]
        assert set(clauses) == {"1_n_oos_ge_40", "2_control_valid",
                                "3_m1_ge_95", "4_m2_ge_95",
                                "5_mean_net_r_gt_0"}
        assert all(c["met"] for c in clauses.values())
        assert clauses["1_n_oos_ge_40"]["observed"] >= 40
        assert clauses["3_m1_ge_95"]["observed"] >= 95.0
        assert clauses["4_m2_ge_95"]["observed"] >= 95.0
        assert clauses["5_mean_net_r_gt_0"]["observed"] > 0.0

    def test_the_hook_registers_the_new_name(self):
        import project_status as ps
        assert ps.cleared_edge_signal_from_artifacts(ARTIFACTS) == fb.NAME
        assert ps.current().cleared_edge_signal == fb.NAME

    # -- and now everything that must NOT register -------------------------

    def _tmp_with(self, tmp_path, mutate):
        d = _oos_summary()
        mutate(d)
        with open(tmp_path / "x_summary.json", "w", encoding="utf-8") as h:
            json.dump(d, h)
        return str(tmp_path)

    def test_the_full_sample_diagnostic_cannot_register(self, tmp_path):
        """Same rule, better numbers, wrong window. This is the case the whole
        slice exists to refuse."""
        import project_status as ps
        with open(os.path.join(
                ARTIFACTS,
                "slice57_diagnostic_fullsample_BTCUSDT_summary.json"),
                encoding="utf-8") as handle:
            diag = json.load(handle)
        assert diag["verdict"] == "EDGE_EVIDENCE_POSITIVE"
        assert diag["m1"]["percentile"] > _oos_summary()["m1"]["percentile"]
        assert diag["registration_eligible"] is False
        # Even with an attestation attached — the hole that would otherwise
        # have made the OOS framing decoration.
        diag["control_validated"] = True
        with open(tmp_path / "d_summary.json", "w", encoding="utf-8") as h:
            json.dump(diag, h)
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_the_relabelled_slice55_artefact_cannot_register(self, tmp_path):
        """The forgery this slice's framing most invites: take the old
        full-sample POSITIVE and put the new product's name on it."""
        import project_status as ps
        with open(os.path.join(
                ARTIFACTS,
                "slice55_edge_funding_carry_fade_BTCUSDT_summary.json"),
                encoding="utf-8") as handle:
            old = json.load(handle)
        old["signal"] = fb.NAME
        with open(tmp_path / "r_summary.json", "w", encoding="utf-8") as h:
            json.dump(old, h)
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_a_claim_scored_under_a_different_cut_cannot_register(self,
                                                                  tmp_path):
        """The attack the fold hash exists for: move t_mid, rescore, register."""
        import project_status as ps
        path = self._tmp_with(
            tmp_path, lambda d: d.update(oos_folds_sha256="0" * 64))
        assert ps.cleared_edge_signal_from_artifacts(path) is None

    def test_a_negative_mean_net_r_cannot_register(self, tmp_path):
        """Clause 5. M1 and M2 are RELATIVE — a schedule can beat its own
        rotations while still losing money after costs."""
        import project_status as ps

        def losing(d):
            d["observed"] = dict(d["observed"], mean_r=-0.05)
        assert ps.cleared_edge_signal_from_artifacts(
            self._tmp_with(tmp_path, losing)) is None

    def test_a_zero_mean_net_r_cannot_register(self, tmp_path):
        """'> 0', not '>= 0'."""
        import project_status as ps

        def flat(d):
            d["observed"] = dict(d["observed"], mean_r=0.0)
        assert ps.cleared_edge_signal_from_artifacts(
            self._tmp_with(tmp_path, flat)) is None

    def test_too_few_trades_cannot_register(self, tmp_path):
        """Clause 1, and the count cleared it by ONE trade."""
        import project_status as ps

        def thin(d):
            d["observed"] = dict(d["observed"], n_trades=39)
        assert ps.cleared_edge_signal_from_artifacts(
            self._tmp_with(tmp_path, thin)) is None

    def test_exactly_forty_trades_still_registers(self, tmp_path):
        """The floor is >= 40, not > 40. The control on the test above."""
        import project_status as ps

        def at_floor(d):
            d["observed"] = dict(d["observed"], n_trades=40)
        assert ps.cleared_edge_signal_from_artifacts(
            self._tmp_with(tmp_path, at_floor)) == fb.NAME

    def test_an_unattested_control_cannot_register(self, tmp_path):
        import project_status as ps
        assert ps.cleared_edge_signal_from_artifacts(
            self._tmp_with(tmp_path,
                           lambda d: d.update(control_validated=False))) is None

    def test_a_missing_eligibility_flag_cannot_register(self, tmp_path):
        import project_status as ps
        assert ps.cleared_edge_signal_from_artifacts(
            self._tmp_with(tmp_path,
                           lambda d: d.pop("registration_eligible"))) is None

    def test_an_m1_below_the_bar_cannot_register(self, tmp_path):
        """95.13 cleared by 0.13. This is what 94.9 would have done."""
        import project_status as ps

        def just_under(d):
            d["m1"] = dict(d["m1"], percentile=94.9)
        assert ps.cleared_edge_signal_from_artifacts(
            self._tmp_with(tmp_path, just_under)) is None

    def test_the_refusals_are_logged_with_their_cause(self, tmp_path, caplog):
        import logging

        import project_status as ps

        def losing(d):
            d["observed"] = dict(d["observed"], mean_r=-0.05)
        path = self._tmp_with(tmp_path, losing)
        with caplog.at_level(logging.WARNING):
            ps.cleared_edge_signal_from_artifacts(path)
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert fb.NAME in blob
        assert "not > 0" in blob


class TestTheClearDoesNotUnblockAnything:
    """A cleared edge is a research fact. It must arm nothing."""

    def test_live_is_still_blocked(self):
        import project_status as ps
        status = ps.current()
        assert status.live_authorized is False
        assert status.execution_mode == "paper"

    def test_the_model_is_still_blocked(self):
        import project_status as ps
        status = ps.current()
        assert status.models_current_present is False
        assert status.policy_mode == "off"
        assert not os.path.exists(os.path.join(REPO, "models", "current"))

    def test_no_trading_path_consults_the_cleared_edge_field(self):
        """The field must be inert outside reporting.

        If a risk gate, the engine or the live-arming chain ever read it, a
        research record would have become a trading permission.
        """
        for module in ("risk_management", "trading_engine", "policy",
                       "position_sizing", "main", "config"):
            mod = __import__(module)
            assert "cleared_edge_signal" not in inspect.getsource(mod), module

    def test_the_no_edge_claim_line_was_not_weakened(self):
        import session_log as sl

        import project_status as ps
        assert ps.current().no_edge_claim == sl.NO_EDGE_CLAIM
        assert "NO EDGE CLAIM" in sl.NO_EDGE_CLAIM

    def test_the_status_tool_reports_both_facts_and_exits_clean(self, capsys):
        """Neither statement is suppressed and neither is a lie: research has
        cleared one gate, and this shell still executes no edge."""
        sys.path.insert(0, os.path.join(REPO, "tools"))
        import print_project_status as tool
        code = tool.main([])
        err = capsys.readouterr().err
        assert code == 0
        assert "ADVISORY" in err
        assert "still executes no edge" in err
