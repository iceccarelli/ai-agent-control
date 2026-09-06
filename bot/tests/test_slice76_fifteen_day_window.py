"""Slice 76 — fifteen bars, ceiling 10, four flags, five setups, no entry.

WHAT THIS SLICE MEASURES
========================
`ceiling 10, setups 5 of 15, flagged 4, entries 0, closed trades 0`.

TWO closed UTC days arrived at once — `2026-08-23` and `2026-08-24` — because
the human was late, not because anything about the product changed. A catch-up
is **two rows appended to one file**. EDGE.md §59b.

Two closed bars promote two flags. `2026-08-22` and `2026-08-23` each stopped
being the corpus's last bar and each became a directed SHORT, taking the flag
count from two to four. That is arithmetic about which bar is last; reading the
doubling as acceleration is a reader supplying a thesis the data does not carry.

`2026-08-24` is a FIFTH setup, close-joining at exactly `FUND_ABS`, and being
the last bar it does not flag.

**Nothing is scoreable.** Eligibility reaches back seven bars, so with
`2026-08-24` last the highest scoreable bar is `2026-08-17` and all four flags
sit in the tail. The gaps are recomputed from the corpus every slice and never
carried — §57b reused a distance and got it wrong:

    08-19   2 closed days away   (2026-08-26)
    08-21   4                    (2026-08-28)
    08-22   5                    (2026-08-29)
    08-23   6                    (2026-08-30)
    08-24   7                    (2026-08-31)

The catch-up moved the WINDOW forward by two and the TAIL forward by two. What
shrank is the distance from a FIXED bar to a moving boundary. EDGE.md §59c.

WHAT THIS SLICE ADDS
====================
**Five setups are still at most TWO entries.** `one_entry_per_contiguous_run`
admits one entry per contiguous run. `08-19` is isolated; `08-21` through
`08-24` are one run. The setup count rose by two and the bound did not move at
all — the clearest demonstration yet that counting setups was never counting
trades. EDGE.md §58c.

**The predecessor digest, fixed at the instrument.** Slice 75 wrote
`slice74_linear_sha256_uncompressed` equal to its OWN current digest, shipping
`identical = true` beside `differs = true`. This slice pins from slice 75's
CURRENT values, VERIFIES them against that artefact's own current fields before
computing anything, and FAILS CLOSED on a mismatch. `identical` and `differs`
are derived from one comparison so they cannot contradict, and a test forbids
the pair. A full audit of every prior-digest constant on the tree finds FOUR
defects — slice 66 stale by two, slices 73/74/75 self-comparing — and correctly
clears slices 63 and 71, whose equalities are real. EDGE.md §59d.

**`why_not_closer` is derived.** Every gate artefact from slice 62 to slice 75
opened that field with "One post-t1 day", fourteen slices after there was one.
The earlier artefacts are not edited; the field is now interpolated from this
slice's own files. EDGE.md §59d.

**A rule that was true until it wasn't.** Slice 75 wrote that the whole-corpus
pin "moves by exactly one, for a nameable bar, or something is wrong". True of
every slice that had run; wrong as a rule the first time a catch-up appended
two. It moves by the number of bars APPENDED. EDGE.md §59f.
"""
from __future__ import annotations

import ast
import datetime as dt
import hashlib
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import backtest as bt  # noqa: E402
import corpus_prefix as cp  # noqa: E402
import market_data as md  # noqa: E402
import project_status as ps  # noqa: E402
import promotion_gate as pg  # noqa: E402
import shadow  # noqa: E402
import skill_test as sk  # noqa: E402
from signals import funding_carry_fade_btc_v1 as fb  # noqa: E402

FRESHNESS = "artifacts/slice76_data_freshness.json"
FORWARD = "artifacts/slice76_forward_shadow.json"
GATE = "artifacts/slice76_promotion_gate.json"
SLICE_NUMBER = 76

WINDOW = ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13",
          "2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17",
          "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21",
          "2026-08-22", "2026-08-23", "2026-08-24"]

PRIOR = {n: (f"artifacts/slice{n}_forward_shadow.json", ceiling)
         for n, ceiling in ((62, 0), (63, 0), (64, 0), (65, 0), (66, 0),
                            (67, 0), (68, 1), (69, 2), (70, 3), (71, 4),
                            (72, 5), (73, 6), (74, 7), (75, 8))}


def load(path: str) -> dict:
    with open(os.path.join(REPO, path), encoding="utf-8") as handle:
        return json.load(handle)


def sha256(path: str) -> str:
    with open(os.path.join(REPO, path), "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def prose(text: str) -> str:
    return " ".join(str(text).split())


def _corpus():
    loaded, _b, _n = md.load_corpus(
        os.path.join(REPO, "data", "real_linear_1d"), verify=False)
    bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
            for b in loaded["BTCUSDT"]]
    funding = fb.load_funding(os.path.join(
        REPO, "data", "real_funding", "funding",
        "BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz"))
    return bars, funding, fb.load_folds()


def _day(bars, index):
    return dt.datetime.fromtimestamp(
        bars[index].start_ms / 1000.0, tz=dt.timezone.utc).strftime("%Y-%m-%d")


# ===========================================================================
# 1 — the window reached fifteen, two bars at once
# ===========================================================================


class TestTheWindowReachedFifteen:

    def test_fifteen_closed_bars_lie_after_t1(self):
        result = cp.check(cp.LINEAR_BTC)
        assert result.appended_rows >= 15
        assert [s[:10] for s in result.appended_timestamps][:15] == WINDOW
        assert load(FRESHNESS)["after_t1_linear"] == 15
        assert load(FRESHNESS)["after_t1_dates"] == WINDOW

    def test_the_growth_is_measured_against_the_previous_artefact(self):
        delta = load(FRESHNESS)["delta_since_slice75"]
        assert delta["previous_after_t1_linear"] == 13
        assert delta["new_linear_bars_since_slice75"] == 2
        assert delta["the_window_grew"] is True

    def test_it_was_a_catch_up_and_the_artefact_says_so(self):
        """Two closed days at once, named as such. EDGE.md §59b.

        A catch-up is two rows appended to one file because the human was
        late. The artefact must not report it as one bar twice or as
        acceleration, and a reader must not have to infer it from a delta.
        """
        delta = load(FRESHNESS)["delta_since_slice75"]
        assert delta["this_was_a_catch_up"] is True
        assert delta["new_linear_bars_since_slice75"] == 2
        assert load(FRESHNESS)["after_t1_dates"][-2:] == \
            ["2026-08-23", "2026-08-24"]
        assert "two rows appended to one file" in prose(
            delta["what_a_catch_up_is"])

    def test_history_is_untouched_and_growth_is_an_append(self):
        folds = fb.load_folds()
        for path, result in cp.check_all().items():
            assert result.append_only, f"{path}: {result.why_not()}"
            assert result.appended_all_strictly_after_t1
        assert cp.check(cp.LINEAR_BTC).prefix_sha256 == \
            folds["sources"]["linear_bars"]["sha256_uncompressed"]
        assert cp.check(cp.FUNDING_BTC).prefix_sha256 == \
            folds["sources"]["funding"]["sha256_uncompressed"]
        assert load(FRESHNESS)["prefix_invariant"]["history_rewritten"] is False

    def test_the_open_bar_is_absent(self):
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        rows = cp.read_rows(cp.LINEAR_BTC)
        assert not any(r["time_period_start"][:10] >= today for r in rows)
        assert load(FRESHNESS)["open_bar_absent_as_expected"] is True

    def test_the_funding_is_not_stale(self):
        delta = load(FRESHNESS)["delta_since_slice75"]
        assert delta["new_funding_prints_since_slice75"] == 7
        assert load(FRESHNESS)["latest_funding_timestamp"] == \
            "2026-08-25T16:00:00+00:00"
        digests = load(FRESHNESS)["digests"]
        assert digests["funding_genuinely_changed_this_slice"] is True
        assert digests["funding_identical_to_slice75_uncompressed"] is False

    def test_the_funding_covers_both_new_bars_close_joins(self):
        """Linear growth with stale funding would make the join stale.

        Both new decision bars need a print standing at their close. The
        funding runs to 2026-08-25T16:00Z, past the newest decision instant
        of 2026-08-24T23:59:59.999Z, so neither bar is joined against a rate
        that arrived after it.
        """
        regime = load(FRESHNESS)["forward_funding_regime"]
        for date in ("2026-08-23", "2026-08-24"):
            assert regime["rate_at_each_forward_decision"][date] == fb.FUND_ABS
        assert regime["funding_covers_the_newest_decision"] is True
        assert regime["funding_extends_past_the_newest_decision"] is True

    def test_only_btc_grew(self):
        grew = {p for p, r in cp.check_all().items() if r.extended}
        assert grew == {cp.LINEAR_BTC, cp.FUNDING_BTC}
        for path, result in cp.check_all().items():
            if "BTC" not in path:
                assert result.appended_rows == 0, path

    def test_every_note_claim_is_true(self):
        payload = load(FRESHNESS)
        assert payload["note_claims_all_true"] is True
        assert payload["false_note_claims"] == []
        assert payload["human_note_is_evidence"] is False
        assert payload["claim_vs_files_discrepancy"] is False
        assert len(payload["human_note_claims_checked"]) >= 16

    def test_the_note_path_names_this_slice_and_slice_is_derived(self):
        payload = load(FRESHNESS)
        assert payload["human_note_path"] == \
            "docs/human/HUMAN_DATA_NOTE_SLICE76.md"
        assert payload["human_note_present"] is True
        assert payload["slice"] == 76
        with open(os.path.join(REPO, "tools", "slice76_freshness.py"),
                  encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        note_path = next(n for n in tree.body
                         if isinstance(n, ast.Assign)
                         and any(getattr(t, "id", None) == "NOTE_PATH"
                                 for t in n.targets))
        assert isinstance(note_path.value, ast.JoinedStr)
        slice_const = next(n for n in tree.body
                           if isinstance(n, ast.Assign)
                           and any(getattr(t, "id", None) == "SLICE"
                                   for t in n.targets))
        assert slice_const.value.value == 76

    def test_the_slice_number_is_derived_in_every_tool(self):
        for name in ("slice76_freshness.py", "slice76_forward_shadow.py",
                     "slice76_promotion_gate.py"):
            with open(os.path.join(REPO, "tools", name),
                      encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            literals = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Dict)
                for key, value in zip(node.keys, node.values)
                if isinstance(key, ast.Constant) and key.value == "slice"
                and isinstance(value, ast.Constant)]
            assert literals == [], f"{name}: slice number is a literal again"
        for artefact in (FRESHNESS, FORWARD, GATE):
            assert load(artefact)["slice"] == 76, artefact

    def test_the_counts_came_from_this_slices_files(self):
        """On slice 76's own EXPIRES_WITH_DATA list from the day it is written."""
        assert cp.check(cp.LINEAR_BTC).rows_on_disk >= \
            load(FRESHNESS)["linear_rows"]
        assert cp.check(cp.FUNDING_BTC).rows_on_disk >= \
            load(FRESHNESS)["funding_rows"]
        assert load(FRESHNESS)["linear_rows"] == 1476
        assert load(FRESHNESS)["funding_rows"] == 4431

    def test_the_finding_says_fifteen_and_not_thirteen(self):
        for text in (prose(load(FRESHNESS)["delta_since_slice75"]["finding"]),
                     prose(load(FORWARD)["window_grew_since_slice75"]["note"])):
            assert "THIRTEEN BARS" not in text.upper()
            assert "FOURTEEN BARS" not in text.upper()
            assert "FIFTEEN BARS" in text.upper()
        finding = prose(load(FRESHNESS)["delta_since_slice75"]["finding"])
        assert "ceiling 10, setups 5 of 15, fills 0" in finding
        assert "2026-08-23, 2026-08-24" in finding

    def test_the_spelled_number_table_no_longer_runs_out(self):
        """It stopped at TWELVE and the window reached thirteen. EDGE.md §58f.

        It fell back to the integer rather than lying, which is why a derived
        field is chosen — but a lookup with a bounded domain is a constant
        waiting to run out. Extended, with the fallback kept so the next
        overflow is loud.
        """
        for name in ("slice76_freshness.py", "slice76_forward_shadow.py"):
            with open(os.path.join(REPO, "tools", name),
                      encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            table = next(
                n.value for n in tree.body
                if isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "_SPELLED"
                        for t in n.targets))
            keys = {k.value for k in table.keys}
            assert 15 in keys and max(keys) >= 20, name


# ===========================================================================
# 2 — two closed bars promote two flags, and the ladder
# ===========================================================================


class TestTwoClosedBarsPromoteTwoFlags:

    def test_the_state_ladder_reads_5_4_0_0_0_0(self):
        ladder = load(FORWARD)["state_ladder"]
        assert ladder["1_setups"] == 5
        assert ladder["2_flagged"] == 4
        assert ladder["3_eligible_and_flagged"] == 0
        assert ladder["4_candidates_after_schedule"] == 0
        assert ladder["5_entries_taken"] == 0
        assert ladder["6_closed_trades"] == 0

    def test_the_flag_count_moved_by_the_number_of_bars_appended(self):
        """Two closed bars promote two flags. Arithmetic, not momentum.

        Each closed bar makes the previous last bar no longer last, and a
        setup that is not the last bar flags. So the flag count rises by the
        number of bars APPENDED — one in slices 73 and 75, two here — and any
        reading of the doubling as acceleration is a reader supplying a
        thesis the data does not carry. EDGE.md §59b.
        """
        before = load("artifacts/slice75_forward_shadow.json")
        assert before["state_ladder"]["2_flagged"] == 2
        assert load(FORWARD)["state_ladder"]["2_flagged"] == 4
        appended = load(FRESHNESS)["delta_since_slice75"][
            "new_linear_bars_since_slice75"]
        assert appended == 2
        assert (load(FORWARD)["state_ladder"]["2_flagged"]
                - before["state_ladder"]["2_flagged"]) == appended

    def test_the_setups_are_the_five_the_corpus_produces(self):
        regime = load(FRESHNESS)["forward_funding_regime"]
        assert regime["forward_bars"] == 15
        assert regime["funding_setups_in_window"] == 5
        assert regime["setup_dates"] == \
            ["2026-08-19", "2026-08-21", "2026-08-22", "2026-08-23",
             "2026-08-24"]
        assert set(regime["setup_directions"].values()) == {fb.SHORT_SETUP}

    def test_08_22_and_08_23_are_now_flags(self):
        """The transition 08-19 made in slice 73 and 08-21 in slice 75.

        Slice 75 recorded 08-22 as a last-bar setup that could not flag. Both
        it and 08-23 made the transition inside this one catch-up. The corpus
        moved, not the rule.
        """
        bars, funding, _folds = _corpus()
        flags, directions = fb.flags_and_directions(bars, funding, warmup=200)
        for date in ("2026-08-22", "2026-08-23"):
            index = next(i for i in range(len(bars))
                         if _day(bars, i) == date)
            assert index < len(bars) - 1, date
            assert bool(flags[index]) is True, date
            assert directions[index] == fb.SHORT_SETUP, date
        assert [f[:10] for f in load(FORWARD)["forward_decisions"][
            "flagged_bars_in_window"]] == \
            ["2026-08-19", "2026-08-21", "2026-08-22", "2026-08-23"]
        # Slice 75's frozen record of what it was then.
        assert load("artifacts/slice75_forward_shadow.json")[
            "forward_decisions"]["flagged_bars_in_window"] == \
            ["2026-08-19T00:00:00Z", "2026-08-21T00:00:00Z"]

    def test_08_24_is_a_setup_reproduced_from_the_corpus(self):
        bars, funding, folds = _corpus()
        window = list(fb.forward_window_indices(bars, folds))
        rates = fb.funding_at_decision(bars, funding)
        setups = fb.funding_setups(bars, funding, fund_abs=fb.FUND_ABS)
        index = next(i for i in window if _day(bars, i) == "2026-08-24")
        assert rates[index] == fb.FUND_ABS
        assert index in setups
        assert setups[index] == fb.SHORT_SETUP

    def test_08_24_stands_at_the_close_and_does_not_flag(self):
        bars, funding, _folds = _corpus()
        close = fb.close_time_ms(bars[-1])
        assert close == int(bars[-1].start_ms) + 86_400_000 - 1
        prints = [r for r in cp.read_rows(cp.FUNDING_BTC)
                  if r["funding_time"][:10] == "2026-08-24"]
        assert [p["funding_time"][11:16] for p in prints] == \
            ["00:00", "08:00", "16:00"]
        assert float(prints[-1]["funding_rate"]) == fb.FUND_ABS
        flags, _directions = fb.flags_and_directions(bars, funding, warmup=200)
        assert bool(flags[len(bars) - 1]) is False
        assert load(FRESHNESS)["forward_funding_regime"][
            "setup_bars_that_are_the_last_bar_of_the_corpus"] == \
            ["2026-08-24"]

    def test_08_20_is_still_not_a_setup(self):
        bars, funding, folds = _corpus()
        window = list(fb.forward_window_indices(bars, folds))
        rates = fb.funding_at_decision(bars, funding)
        setups = fb.funding_setups(bars, funding, fund_abs=fb.FUND_ABS)
        index = next(i for i in window if _day(bars, i) == "2026-08-20")
        assert rates[index] == 0.00009422
        assert index not in setups

    def test_no_entry_and_no_closed_trade(self):
        forward = load(FORWARD)
        assert forward["forward_decisions"]["candidates_after_schedule"] == []
        assert forward["forward_decisions"]["entries_taken"] == 0
        assert forward["forward_decisions"][
            "decisions_with_no_entry_bar_yet"] == []
        assert forward["forward_decisions"][
            "decisions_whose_exit_bar_does_not_exist_yet"] == []
        assert forward["forward_n_trades"] == 0
        assert forward["is_forward_observation"] is False
        assert forward["trades"] == []
        assert forward["forward_mean_net_r"] is None

    def test_a_lower_rung_is_never_reported_as_a_higher_one(self):
        reading = prose(load(FORWARD)["state_ladder"]["reading"])
        assert "slice-59 substitution" in reading
        evidence = prose(load(GATE)["checklist"]["forward_shadow_clean"][
            "evidence"])
        assert "A SETUP IS NOT A FLAG" in evidence.upper()
        assert "AN ENTRY IS NOT A CLOSED TRADE" in evidence.upper()
        assert load(GATE)["checklist"]["forward_shadow_clean"][
            "forward_trades_to_date"] == 0


# ===========================================================================
# 3 — five setups are STILL at most two entries
# ===========================================================================


class TestFiveSetupsAreStillAtMostTwoEntries:
    """EDGE.md §58c, §59b. `one_entry_per_contiguous_run` bounds the schedule.

    Slice 75 had three setups in two runs and a bound of two. This slice has
    FIVE setups — and the bound is still TWO, because the two new setups
    extend the existing run rather than starting a third. The gap between a
    setup count and a trade count widened by two this slice without the bound
    moving at all, which is the clearest available demonstration that
    counting setups was never counting trades.
    """

    def test_the_runs_are_08_19_alone_and_08_21_through_08_24(self):
        runs = load(FORWARD)["ceiling"]["contiguous_setup_runs"]
        assert runs == [["2026-08-19"],
                        ["2026-08-21", "2026-08-22", "2026-08-23",
                         "2026-08-24"]]

    def test_the_schedule_admits_at_most_two_not_five(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["max_entries_the_schedule_would_admit"] == 2
        assert load(FORWARD)["state_ladder"]["1_setups"] == 5
        assert load(FORWARD)["schedule_mode"] == \
            "one_entry_per_contiguous_run"

    def test_two_more_setups_did_not_raise_the_bound(self):
        """The bound did not move while the setup count rose by two.

        Slice 75: 3 setups, 2 runs, bound 2. Slice 76: 5 setups, 2 runs,
        bound 2. `one_entry_per_contiguous_run` admits one entry per run and
        the new setups landed inside a run that already existed.
        """
        before = load("artifacts/slice75_forward_shadow.json")["ceiling"]
        now = load(FORWARD)["ceiling"]
        assert before["max_entries_the_schedule_would_admit"] == 2
        assert now["max_entries_the_schedule_would_admit"] == 2
        assert len(before["contiguous_setup_runs"]) == 2
        assert len(now["contiguous_setup_runs"]) == 2
        assert (load(FORWARD)["state_ladder"]["1_setups"]
                - load("artifacts/slice75_forward_shadow.json")[
                    "state_ladder"]["1_setups"]) == 2

    def test_the_gap_at_08_20_is_what_separates_them(self):
        """If 08-20 had been a setup, all five would be ONE run and the
        schedule would admit ONE entry instead of two.

        `2026-08-20` close-joined at 0.00009422 and missed by 5.8e-6. That
        single non-setup day is the whole difference between a bound of one
        and a bound of two, and it is the kind of margin that makes moving
        FUND_ABS feel like a rounding decision. Nothing moved. EDGE.md §59b.
        """
        bars, funding, folds = _corpus()
        window = list(fb.forward_window_indices(bars, folds))
        setups = fb.funding_setups(bars, funding, fund_abs=fb.FUND_ABS)
        dates = [_day(bars, i) for i in window if i in setups]
        assert dates == ["2026-08-19", "2026-08-21", "2026-08-22",
                         "2026-08-23", "2026-08-24"]
        gap = next(i for i in window if _day(bars, i) == "2026-08-20")
        assert gap not in setups
        # Exactly two runs, and the second one absorbed both new setups.
        runs, previous = [], None
        for index in [i for i in window if i in setups]:
            if previous is None or index != previous + 1:
                runs.append([])
            runs[-1].append(_day(bars, index))
            previous = index
        assert [run[0] for run in runs] == ["2026-08-19", "2026-08-21"]
        assert len(runs) == 2
        assert len(runs[1]) == 4
        assert fb.FUND_ABS == 0.0001

    def test_it_is_a_bound_on_the_schedule_not_a_prediction(self):
        with open(os.path.join(REPO, "EDGE.md"), encoding="utf-8") as handle:
            edge = prose(handle.read())
        assert "It does not predict that either entry happens" in edge
        assert "bound on the schedule's output" in edge


# ===========================================================================
# 4 — nothing is scoreable, and every gap is recomputed
# ===========================================================================


class TestNothingIsScoreable:

    def test_eligibility_still_reaches_back_seven_bars(self):
        block = load(FORWARD)["barrier_eligibility"]
        assert block["reaches_back_from_the_end_by_bars"] == 7
        assert block["highest_scoreable_bar_utc"][:10] == "2026-08-17"

    def test_the_tail_advanced_with_the_window_not_against_it(self):
        """Two closed bars moved the window forward AND the tail forward.

        A reader could expect a catch-up to make an older flag scoreable. It
        does not, by itself: the seven-bar tail is measured from the corpus's
        END, so appending two bars moves the boundary by two as well. What
        appending does is shrink the DISTANCE from a FIXED bar to that moving
        boundary — 2026-08-19 went from four closed days away to two — and
        only that shrinking eventually makes it scoreable. EDGE.md §59c.
        """
        before = load("artifacts/slice75_forward_shadow.json")[
            "barrier_eligibility"]
        now = load(FORWARD)["barrier_eligibility"]
        assert before["highest_scoreable_bar_utc"][:10] == "2026-08-15"
        assert now["highest_scoreable_bar_utc"][:10] == "2026-08-17"
        assert now["reaches_back_from_the_end_by_bars"] == \
            before["reaches_back_from_the_end_by_bars"] == 7
        assert before["closed_days_until_scoreable"]["2026-08-19"] == 4
        assert now["closed_days_until_scoreable"]["2026-08-19"] == 2

    def test_that_offset_is_measured_from_the_frozen_function(self):
        bars, _funding, _folds = _corpus()
        for side in ("short", "long"):
            idx, _net, _used = sk.barrier_r_for_all_bars(
                bars, take_profit_atr=fb.TAKE_PROFIT_ATR,
                stop_atr=fb.STOP_ATR, horizon=fb.HORIZON,
                atr_period=fb.ATR_PERIOD,
                round_trip_bps=fb.ROUND_TRIP_BPS, side=side,
                entry_on=fb.ENTRY_ON)
            assert (len(bars) - 1) - int(max(idx)) == 7, side

    def test_neither_flag_has_a_barrier_outcome(self):
        bars, _funding, _folds = _corpus()
        idx, _net, _used = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=fb.TAKE_PROFIT_ATR, stop_atr=fb.STOP_ATR,
            horizon=fb.HORIZON, atr_period=fb.ATR_PERIOD,
            round_trip_bps=fb.ROUND_TRIP_BPS, side="short",
            entry_on=fb.ENTRY_ON)
        scoreable = {int(i) for i in idx}
        for date in ("2026-08-19", "2026-08-21", "2026-08-22", "2026-08-23"):
            index = next(i for i in range(len(bars))
                         if _day(bars, i) == date)
            assert index not in scoreable, date
            assert date in load(FORWARD)["barrier_eligibility"][
                "forward_bars_that_are_not_scoreable"]
        assert load(FORWARD)["state_ladder"]["3_eligible_and_flagged"] == 0

    def test_the_gaps_are_recomputed_from_the_corpus(self):
        """§57b reused a distance from the slice before and got it wrong.

        Every gap is `(i + HORIZON + 2) - last`, read from indices this
        slice. 08-19 is FOUR closed days away now; it was five when the last
        bar was 08-21, and §57e owns the earlier error.

        This slice says TWO for 08-19. Slice 75 said four, slice 74 said
        five, and not one of those numbers is carried forward: the formula
        is, and the indices are read fresh. EDGE.md §59c.
        """
        bars, _funding, _folds = _corpus()
        last = len(bars) - 1
        index_of = {}
        for i in range(len(bars)):
            index_of[_day(bars, i)] = i
        gaps = load(FORWARD)["barrier_eligibility"][
            "closed_days_until_scoreable"]
        for date, remaining in gaps.items():
            assert remaining == (index_of[date] + fb.HORIZON + 2) - last, date
        assert gaps["2026-08-19"] == 2
        assert gaps["2026-08-21"] == 4
        assert gaps["2026-08-22"] == 5
        assert gaps["2026-08-23"] == 6
        assert gaps["2026-08-24"] == 7
        # Every distance shrank by exactly the number of bars appended, and
        # slice 75's numbers are not reused as this slice's.
        before = load("artifacts/slice75_forward_shadow.json")[
            "barrier_eligibility"]["closed_days_until_scoreable"]
        for date in ("2026-08-19", "2026-08-21", "2026-08-22"):
            assert before[date] - gaps[date] == 2, date

    def test_the_destinations_are_the_dates_the_note_names(self):
        """The DESTINATION is permanent; the DISTANCE is not. EDGE.md §58b.

        Slice 74 said 08-19 was five closed days away, slice 75 said four,
        this slice says two — and all three point at the SAME calendar date,
        2026-08-26, which is what makes the destination the durable claim and
        the distance the expiring one. Checked here against slice 75's frozen
        gaps as well as this slice's live ones, so the two must agree on
        where they land.
        """
        bars, _funding, _folds = _corpus()

        def as_date(text):
            return dt.datetime.strptime(text, "%Y-%m-%d").replace(
                tzinfo=dt.timezone.utc)

        live_last = _day(bars, len(bars) - 1)
        assert live_last == "2026-08-24"
        frozen_last = load("artifacts/slice75_data_freshness.json")[
            "after_t1_dates"][-1]
        frozen_gaps = load("artifacts/slice75_forward_shadow.json")[
            "barrier_eligibility"]["closed_days_until_scoreable"]
        gaps = load(FORWARD)["barrier_eligibility"][
            "closed_days_until_scoreable"]
        for date, expected in (("2026-08-19", "2026-08-26"),
                               ("2026-08-21", "2026-08-28"),
                               ("2026-08-22", "2026-08-29"),
                               ("2026-08-23", "2026-08-30"),
                               ("2026-08-24", "2026-08-31")):
            target = as_date(live_last) + dt.timedelta(days=gaps[date])
            assert target.strftime("%Y-%m-%d") == expected, date
            if date in frozen_gaps:
                older = as_date(frozen_last) + dt.timedelta(
                    days=frozen_gaps[date])
                assert older.strftime("%Y-%m-%d") == expected, date

    def test_eight_forward_bars_are_scoreable_now(self):
        block = load(FORWARD)["barrier_eligibility"]
        assert block["forward_bars_that_are_scoreable"] == WINDOW[:8]
        assert load(FORWARD)["ceiling"][
            "forward_bars_with_a_barrier_outcome"] == 8

    def test_the_frozen_function_was_not_modified(self):
        assert load(FORWARD)["barrier_eligibility"][
            "not_modified_by_this_slice"] is True
        with open(os.path.join(REPO, "tools", "skill_test.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        assert "eligible[max(0, n - horizon - entry_offset - 1):] = False" \
            in source
        assert fb.HORIZON == 5
        assert fb.ENTRY_ON == "next_open"


# ===========================================================================
# 5 — the base rate is what is accumulating
# ===========================================================================


class TestTheBaseRateIsWhatIsAccumulating:

    def test_the_run_census_is_measured_not_asserted(self):
        """Recomputed from the funding file this slice, never carried."""
        import itertools
        runs_block = load(FRESHNESS)["forward_funding_regime"][
            "base_rate_runs"]
        values = [float(r["funding_rate"])
                  for r in cp.read_rows(cp.FUNDING_BTC)]
        runs = [len(list(g)) for at, g in itertools.groupby(
            v == fb.FUND_ABS for v in values) if at]
        assert runs_block["runs_at_exactly_base"] == len(runs) == 295
        assert runs_block["longest_run_prints"] == max(runs) == 70
        trailing = 0
        for value in reversed(values):
            if value != fb.FUND_ABS:
                break
            trailing += 1
        assert runs_block["current_trailing_run_prints"] == trailing == 2

    def test_the_current_run_got_SHORTER_and_that_is_reported(self):
        """The same measurement pointing the other way, kept.

        Slice 75 reported a trailing base-rate run of SIX prints at the 89th
        percentile and used it to argue the regime was unremarkable. This
        slice's run is TWO, at the 71st, and the setups kept arriving anyway.
        A measurement that is quoted while it flatters an argument and
        dropped when it stops is not a measurement, so it is reported here
        with the direction of travel named. Neither reading is a reason to
        touch FUND_ABS. EDGE.md §58d.
        """
        runs_block = load(FRESHNESS)["forward_funding_regime"][
            "base_rate_runs"]
        before = load("artifacts/slice75_data_freshness.json")[
            "forward_funding_regime"]["base_rate_runs"]
        assert before["current_trailing_run_prints"] == 6
        assert runs_block["current_trailing_run_prints"] == 2
        assert runs_block["current_trailing_run_prints"] < \
            before["current_trailing_run_prints"]
        assert runs_block["current_trailing_run_hours"] == 16
        assert runs_block["percentile_of_current_run"] < \
            before["percentile_of_current_run"]
        assert runs_block["percentile_of_current_run"] < 95.0
        assert runs_block["longest_run_prints"] > \
            10 * runs_block["current_trailing_run_prints"]
        assert runs_block["fund_abs_moved_because_of_this"] is False

    def test_all_five_setups_sit_at_exactly_the_base_rate(self):
        """Five now, and not one of them is above the threshold.

        The claim is not the count but the RATE: every qualifying bar sits at
        exactly `FUND_ABS`. That is the §57c finding continuing to hold — the
        rule is fading BASE-rate funding, which is what it has always done —
        and emphatically not a reason to move the threshold.
        """
        bars, funding, folds = _corpus()
        window = list(fb.forward_window_indices(bars, folds))
        rates = fb.funding_at_decision(bars, funding)
        setups = fb.funding_setups(bars, funding, fund_abs=fb.FUND_ABS)
        qualifying = [rates[i] for i in window if i in setups]
        assert len(qualifying) == 5
        assert all(rate == fb.FUND_ABS for rate in qualifying)
        assert not any(rate > fb.FUND_ABS for rate in qualifying)

    def test_the_threshold_is_still_the_modal_value_and_not_a_cap(self):
        census = load(FRESHNESS)["forward_funding_regime"]["threshold_census"]
        assert census["fund_abs_is_a_cap"] is False
        assert census["fund_abs_is_the_modal_value"] is True
        assert census["strictly_above_fund_abs"] > 0

    def test_the_census_is_this_slices_file_and_the_prose_agrees(self):
        """The census numbers and the sentence quoting them are one source.

        Slice 75 computed the census in a block and TYPED "1,174 of 4,422"
        into the finding beside it. Both were true then and the pair would
        have drifted the moment a print arrived — which is this slice. The
        finding now interpolates the block. EDGE.md §53d, §57c.
        """
        regime = load(FRESHNESS)["forward_funding_regime"]
        census = regime["threshold_census"]
        values = [float(r["funding_rate"])
                  for r in cp.read_rows(cp.FUNDING_BTC)]
        assert census["prints"] == len(values) == 4431
        assert census["exactly_at_fund_abs"] == \
            sum(1 for v in values if v == fb.FUND_ABS) == 1182
        assert census["strictly_above_fund_abs"] == \
            sum(1 for v in values if v > fb.FUND_ABS) == 282
        assert census["max_print"] == max(values) == 0.00088148
        finding = prose(regime["finding"])
        assert f"{census['exactly_at_fund_abs']:,} of " \
               f"{census['prints']:,} prints" in finding
        assert "1,174 of 4,422" not in finding

    def test_fund_abs_did_not_move(self):
        assert fb.FUND_ABS == 0.0001
        assert load(FORWARD)["fund_abs_moved_this_slice"] is False
        assert load(FORWARD)["join_changed_this_slice"] is False
        assert load(FRESHNESS)["forward_funding_regime"][
            "parameters_moved_because_of_this"] is False
        assert load(FRESHNESS)["forward_funding_regime"]["base_rate_runs"][
            "fund_abs_moved_because_of_this"] is False

    def test_the_temptation_is_named_in_edge(self):
        with open(os.path.join(REPO, "EDGE.md"), encoding="utf-8") as handle:
            edge = prose(handle.read())
        assert "§58d." in edge or "58d." in edge
        assert "most reasonable and be most wrong" in edge


# ===========================================================================
# 5b — the ceiling
# ===========================================================================


class TestTheCeiling:

    def test_the_ceiling_is_ten(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["closed_forward_bars"] == 15
        assert ceiling["max_possible_forward_closed_trades"] == 10
        assert ceiling["observed_forward_closed_trades"] == 0
        assert ceiling["within_ceiling"] is True
        assert "§59a" in ceiling["declared_in"]

    def test_it_is_not_nine_and_not_eleven(self):
        ceiling = load(FORWARD)["ceiling"]["max_possible_forward_closed_trades"]
        assert ceiling != 8, "8 was slice 75's ceiling, at N=13"
        assert ceiling != 9, "9 would be N=14 — the catch-up appended TWO"
        assert ceiling < 11, "11 would need N=16"
        assert ceiling == max(0, 15 - fb.HORIZON)

    def test_the_ceiling_moved_by_the_number_of_bars_appended(self):
        """Two closed bars, two ceiling steps. It counts bars, not outcomes.

        The ceiling is `max(0, N - HORIZON)` and N rose by two, so it rose by
        two. A larger ceiling is a larger DENOMINATOR of what is possible; it
        moves the numerator of what has happened not at all, and observed is
        still 0. EDGE.md §59a.
        """
        before = load("artifacts/slice75_forward_shadow.json")["ceiling"]
        now = load(FORWARD)["ceiling"]
        assert before["max_possible_forward_closed_trades"] == 8
        assert now["max_possible_forward_closed_trades"] == 10
        assert (now["closed_forward_bars"]
                - before["closed_forward_bars"]) == 2
        assert (now["max_possible_forward_closed_trades"]
                - before["max_possible_forward_closed_trades"]) == 2
        assert now["observed_forward_closed_trades"] == \
            before["observed_forward_closed_trades"] == 0

    def test_the_unconditional_bound_is_commentary_only(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["unconditional_upper_bound"] == 14
        assert ceiling["max_possible_forward_closed_trades"] == 10
        assert ceiling["observed_forward_closed_trades"] <= \
            ceiling["forward_bars_with_a_barrier_outcome"]

    def test_every_prior_slice_ceiling_is_recorded_and_lower(self):
        observed = [load(f)["ceiling"]["max_possible_forward_closed_trades"]
                    for _n, (f, _c) in sorted(PRIOR.items())]
        assert observed == [expected for _n, (_f, expected)
                            in sorted(PRIOR.items())]
        assert observed == [0, 0, 0, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8]
        assert load(FORWARD)["ceiling"][
            "max_possible_forward_closed_trades"] == 10

    def test_every_prior_slice_observed_zero_closed_trades(self):
        for _n, (path, _c) in sorted(PRIOR.items()):
            assert load(path)["forward_n_trades"] == 0, path
        assert load(FORWARD)["forward_n_trades"] == 0

    def test_no_clamping_no_slicing_no_invention(self):
        forward = load(FORWARD)
        assert forward["forward_decisions"]["exit_clamping_to_corpus_end"] \
            is False
        assert forward["forward_window"]["bar_array_sliced"] is False
        assert forward["forward_window"]["invented_future_bars"] == 0
        assert forward["bars_fabricated"] == 0
        assert load(FRESHNESS)["bars_fabricated"] == 0
        assert load(FRESHNESS)["corpus_appended_to_by_this_tool"] is False

    def test_the_forward_monitors_report_insufficient_data(self):
        states = {r["name"]: r["state"]
                  for r in load(FORWARD)["forward_monitor_readings"]}
        assert set(states.values()) == {"INSUFFICIENT_DATA"}
        assert load(FORWARD)["forward_monitor_status"] == "INSUFFICIENT_DATA"

    def test_the_historical_m4_warn_was_not_rebranded(self):
        historical = load("artifacts/slice59_forward_shadow.json")
        m4 = next(r for r in historical["monitor_readings"]
                  if r["name"] == "M4_halves")
        assert m4["state"] == "WARN"
        assert "deliberately NOT recomputed" in \
            prose(load(FORWARD)["monitor_scope_note"])


# ===========================================================================
# 5c — the whole-corpus pin moved again, and it is now expected to
# ===========================================================================


class TestTheSetupPinMovesWithEachNewFlag:
    """417 -> 418 (slice 73) -> 419 (slice 75) -> 421 (slice 76).

    The step size is the number of bars APPENDED, not the constant one.
    Slice 75 wrote "it moves by exactly one, for a nameable bar, or something
    is wrong" — true of every slice that had run, and wrong as a rule the
    first time a catch-up appended two. EDGE.md §59f.
    """

    def test_the_pin_is_now_421(self):
        from signals import funding_carry_fade_v1 as fc  # noqa: PLC0415
        bars, funding, _folds = _corpus()
        summary = fc.summary(bars, funding, warmup=200)
        assert summary["setups"] == 421
        assert summary["long_setups"] == 6
        assert summary["bars_without_funding"] == 0

    def test_the_movers_are_08_22_and_08_23_and_it_moved_by_two(self):
        """Both movers named, and the step size checked against the append.

        `2026-08-22` and `2026-08-23` each stopped being the corpus's last
        bar when the catch-up landed, so each became a directed signal bar.
        `long_setups` did not move, so both are SHORT — which is what the
        forward artefact says independently.
        """
        from signals import funding_carry_fade_v1 as fc  # noqa: PLC0415
        bars, funding, _folds = _corpus()
        directed = fc.directed_signal_bars(bars, funding, warmup=200)
        by_date = {_day(bars, i): d for i, d in directed}
        # Slice 75's mover: still directed, still SHORT, permanently so.
        assert by_date["2026-08-21"] == fc.SHORT_SETUP
        # This slice's two movers.
        assert by_date["2026-08-22"] == fc.SHORT_SETUP
        assert by_date["2026-08-23"] == fc.SHORT_SETUP
        index, direction = directed[-1]
        assert _day(bars, index) == "2026-08-23"
        assert direction == fc.SHORT_SETUP
        assert len(directed) == 421
        # The step size IS the number of bars appended.
        appended = load(FRESHNESS)["delta_since_slice75"][
            "new_linear_bars_since_slice75"]
        assert appended == 2
        assert len(directed) - 419 == appended

    def test_the_step_size_rule_was_corrected_not_quietly_fixed(self):
        """A sentence true of every slice so far is not a rule. §59f.

        Slice 75's module still carries a test named
        `test_the_mover_is_08_21_and_it_moved_by_exactly_one`. The name is
        kept because `STAGE1_VERDICT_SLICE75.md` cites it and a shipped
        verdict must not be made false by a rename; its BODY was amended and
        says so. This test exists so the correction is asserted somewhere,
        not merely written in a docstring nothing reads.
        """
        with open(os.path.join(REPO, "tests",
                               "test_funding_carry_fade_v1.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        assert "418 -> 419 IN SLICE 75" in source
        assert "419 -> 421 IN SLICE 76" in source
        assert "RECURRING amendment" in source
        assert "number of bars APPENDED" in source
        with open(os.path.join(REPO, "tests",
                               "test_slice75_thirteen_day_window.py"),
                  encoding="utf-8") as handle:
            older = ast.parse(handle.read())
        names = {n.name for n in ast.walk(older)
                 if isinstance(n, ast.FunctionDef)}
        assert "test_the_mover_is_08_21_and_it_moved_by_exactly_one" in names
        with open(os.path.join(REPO, "EDGE.md"), encoding="utf-8") as handle:
            edge = prose(handle.read())
        assert "59f." in edge
        assert "the number of bars appended" in edge
        assert "is not thereby a rule" in edge
# ===========================================================================
# 5d — the predecessor digest, and the pair that cannot both be true
# ===========================================================================


class TestThePredecessorDigestIsThePredecessors:
    """EDGE.md §59d.

    A predecessor constant has one job: name the digest the PREVIOUS slice
    currently reported. It failed that job four times in twelve slices, and
    the failure is silent by construction — a constant equal to the
    artefact's own current digest produces `identical = true` beside
    `differs = true`, and nothing in the artefact objects.
    """

    LINEAR75 = ("6e64847c54991ef38c0aaaad3095446ea955c822e07"
                "f14938ff6943c8c93b0a7")
    FUNDING75 = ("fec0ee8c33b528cb0baeda4de0c3d5b0c3bb15a144bc"
                 "5537cd379e3bda4219b2")

    def test_identical_and_differs_cannot_both_be_true(self):
        """The pair slice 75 shipped, forbidden by construction here.

        Both fields are derived from ONE comparison read with opposite
        senses, so no setting of the constant can make them agree. The
        artefact says so about itself, and this test says so about the
        artefact.
        """
        digests = load(FRESHNESS)["digests"]
        identical = digests["linear_identical_to_slice75_uncompressed"]
        differs = digests["linear_differs_because_a_bar_arrived"]
        assert isinstance(identical, bool) and isinstance(differs, bool)
        assert identical is not differs
        assert not (identical and differs)
        assert digests["identical_and_differs_cannot_both_be_true"] is True
        # And the same for funding.
        f_identical = digests["funding_identical_to_slice75_uncompressed"]
        f_differs = digests["funding_differs_because_prints_arrived"]
        assert f_identical is not f_differs
        assert digests[
            "funding_identical_and_differs_cannot_both_be_true"] is True

    def test_slice75_shipped_exactly_that_contradiction(self):
        """The defect, still visible in the frozen artefact. Not edited."""
        older = load("artifacts/slice75_data_freshness.json")["digests"]
        assert older["linear_identical_to_slice74_uncompressed"] is True
        assert older["linear_differs_from_slice74_because_a_bar_arrived"] \
            is True
        # And it was a self-compare: the "predecessor" was its own digest.
        assert older["slice74_linear_sha256_uncompressed"] == \
            older["linear_sha256_uncompressed"]

    def test_the_prior_digests_come_from_slice75s_CURRENT_fields(self):
        older = load("artifacts/slice75_data_freshness.json")["digests"]
        digests = load(FRESHNESS)["digests"]
        assert digests["slice75_linear_sha256_uncompressed"] == \
            older["linear_sha256_uncompressed"] == self.LINEAR75
        assert digests["slice75_funding_sha256_uncompressed"] == \
            older["funding_sha256_uncompressed"] == self.FUNDING75
        assert digests[
            "prior_linear_digest_equals_the_previous_artefacts_own"] is True
        assert digests[
            "prior_funding_digest_equals_the_previous_artefacts_own"] is True
        # NOT from the broken field.
        assert digests["slice75_linear_sha256_uncompressed"] != \
            older.get("slice74_funding_sha256_uncompressed")
        assert "slice74_linear_sha256_uncompressed" in \
            digests["where_the_prior_digest_came_from"]

    def test_this_slices_constants_are_not_its_own_digests(self):
        digests = load(FRESHNESS)["digests"]
        assert digests["prior_linear_digest_equals_this_slices_own"] is False
        assert digests["prior_funding_digest_equals_this_slices_own"] is False
        assert digests["linear_sha256_uncompressed"] != self.LINEAR75
        assert digests["funding_sha256_uncompressed"] != self.FUNDING75

    def test_the_constants_are_verified_against_the_previous_artefact(self):
        """The tool refuses to run on a mismatched constant. Fail closed."""
        with open(os.path.join(REPO, "tools", "slice76_freshness.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        assert "PRIOR_{kind.upper()}_UNCOMPRESSED does not match" in source
        assert "raise SystemExit(" in source
        claims = load(FRESHNESS)["human_note_claims_checked"]
        assert claims[
            "predecessor linear uncompressed = slice 75 CURRENT"] is True
        assert claims[
            "predecessor funding uncompressed = slice 75 CURRENT"] is True

    def test_the_audit_finds_the_four_and_clears_the_two(self):
        """Every prior-digest constant on this tree, audited not recounted.

        Four defects: slice 66 (stale by two — it matched neither its own
        digest nor its predecessor's) and slices 73, 74 and 75 (self-compare).
        Slices 63 and 71 are equalities that are CORRECT, because the corpus
        genuinely did not move — 71's funding file was RECOMPRESSED, not
        changed, which is the §54d case. Reporting those two as defects would
        inflate the count; reporting the four as legitimate would hide them.
        """
        audit = load(FRESHNESS)["digests"]["prior_digest_audit"]
        assert audit["constants_audited"] >= 20
        assert audit["defect_count"] == 4
        assert {row["slice"] for row in audit["defects"]} == {66, 73, 74, 75}
        assert audit["self_compare_defect_count"] == 3
        assert {row["slice"] for row in audit["self_compare_defects"]} == \
            {73, 74, 75}
        assert {(row["slice"], row["kind"])
                for row in audit["legitimate_equalities"]} == \
            {(63, "linear"), (71, "funding")}
        assert audit["indeterminate"] == []
        assert audit["this_slice_is_clean"] is True
        assert SLICE_NUMBER not in {row["slice"] for row in audit["defects"]}


# ===========================================================================
# 5e — the gate field that was wrong for fourteen slices
# ===========================================================================


class TestWhyNotCloserIsDerived:
    """EDGE.md §59d. A hand-typed sentence in a field no instrument reads."""

    def test_the_gate_no_longer_says_one_post_t1_day(self):
        why = prose(load(GATE)["why_not_closer"])
        assert "One post-t1 day" not in why
        assert why.startswith("15 post-t1 days")
        assert load(GATE)["why_not_closer_is_derived_not_typed"] is True

    def test_every_number_in_it_comes_from_this_slices_artefacts(self):
        why = prose(load(GATE)["why_not_closer"])
        assert str(load(FRESHNESS)["after_t1_linear"]) in why
        assert f"{load(FORWARD)['state_ladder']['1_setups']} setups" in why
        assert f"{load(FORWARD)['state_ladder']['2_flagged']} flags" in why
        assert str(load(FORWARD)["ceiling"][
            "max_possible_forward_closed_trades"]) in why
        assert "6 of the 8 items" in why

    def test_the_residue_is_named_not_back_dated(self):
        """Slices 62-75 are NOT edited. The record stands; the field moves."""
        for number in range(62, 76):
            path = f"artifacts/slice{number}_promotion_gate.json"
            if not os.path.isfile(os.path.join(REPO, path)):
                continue
            assert prose(load(path)["why_not_closer"]).startswith(
                "One post-t1 day"), path
        assert "slice 62 to slice 75" in prose(
            load(GATE)["the_field_this_replaces"])


# ===========================================================================
# 6 — STEP 1 landed, and the guard that checks it
# ===========================================================================


class TestTheDesignNoteLandedThisTime:

    def test_edge_carries_59a_through_59f(self):
        with open(os.path.join(REPO, "EDGE.md"), encoding="utf-8") as handle:
            edge = handle.read()
        for letter in "abcdef":
            assert f"\n## 59{letter}." in edge, letter

    def test_the_artefacts_recorded_59_as_present_when_they_ran(self):
        for path in (FRESHNESS, FORWARD):
            recorded = load(path)["edge_state_when_this_ran"]["sections_present"]
            assert 59 in recorded, path

    def test_the_ceilings_citation_resolves(self):
        declared = load(FORWARD)["ceiling"]["declared_in"]
        assert "§59a" in declared
        assert 59 in load(FORWARD)["edge_state_when_this_ran"][
            "sections_present"]

    def test_slice73s_artefacts_did_not_record_56(self):
        """The defect §56h owns, still visible in the frozen record.

        Slice 73's artefacts were re-run after §56 was added, so they DO
        record it — what is gone forever is the commit-order evidence, and
        §56h says so rather than the artefacts pretending otherwise.
        """
        recorded = load("artifacts/slice73_forward_shadow.json")[
            "edge_state_when_this_ran"]["sections_present"]
        assert 56 in recorded
        with open(os.path.join(REPO, "EDGE.md"), encoding="utf-8") as handle:
            edge = handle.read()
        assert "\n## 56h." in edge
        assert "did not exist when the tools ran" in edge


# ===========================================================================
# 7 — guards, carried by import
# ===========================================================================


class TestTheGuardFamilyIsCarried:

    def test_member_one_live_absolutes(self):
        from test_slice66_four_day_window import (  # noqa: PLC0415
            TestNoTestPinsALiveAbsolute as Guard)
        guard = Guard()
        offences = {os.path.basename(p): guard._offences(p)
                    for p in guard._test_files()}
        assert {k: v for k, v in offences.items() if v} == {}, json.dumps(
            offences, indent=2)

    def test_member_two_foreign_slice_constants(self):
        from test_slice69_seven_day_window import (  # noqa: PLC0415
            TestNoToolCarriesAnotherSlicesConstant as Guard)
        guard = Guard()
        offences = {os.path.basename(p): guard._offences(p)
                    for p in guard._tools()
                    if os.path.basename(p).startswith("slice76_")}
        assert {k: v for k, v in offences.items() if v} == {}, json.dumps(
            offences, indent=2)

    def test_member_three_live_counts_vs_dated_artefacts(self):
        from test_slice70_eight_day_window import (  # noqa: PLC0415
            TestNoTestEqualsALiveCountAgainstADatedArtefact as Guard)
        guard = Guard()
        offences = {os.path.basename(p): guard._offences(p)
                    for p in guard._test_files()}
        assert {k: v for k, v in offences.items() if v} == {}, json.dumps(
            offences, indent=2)

    def test_member_four_verdicts_cite_real_tests(self):
        from test_slice70_eight_day_window import (  # noqa: PLC0415
            TestAVerdictMayNotCiteATestThatDoesNotExist as Guard)
        guard = Guard()
        path = os.path.join(REPO, "STAGE1_VERDICT_SLICE76.md")
        if not os.path.isfile(path):
            return
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        cited, exhibited = set(), set()
        for line in lines:
            names = set(Guard.NAME.findall(line))
            (exhibited if Guard.ANNOTATION in line else cited).update(names)
        present = guard._test_names_in_the_tree()
        assert sorted(cited - exhibited - present) == [], sorted(
            cited - exhibited - present)
        assert sorted(exhibited & present) == [], sorted(exhibited & present)

    def test_member_five_cited_edge_sections(self):
        from test_slice73_eleven_day_window import (  # noqa: PLC0415
            TestACitedEdgeSectionMustExistWhenTheArtefactWasWritten as Guard)
        guard = Guard()
        for path in (FRESHNESS, FORWARD):
            payload = load(path)
            recorded = set(payload["edge_state_when_this_ran"]
                           ["sections_present"])
            missing = [n for n in guard._cited(payload) if n not in recorded]
            assert missing == [], (path, missing)

    def test_all_five_guards_still_exist_where_expected(self):
        expected = {
            "test_slice66_four_day_window": ("TestNoTestPinsALiveAbsolute",),
            "test_slice69_seven_day_window": (
                "TestNoToolCarriesAnotherSlicesConstant",),
            "test_slice70_eight_day_window": (
                "TestNoTestEqualsALiveCountAgainstADatedArtefact",
                "TestAVerdictMayNotCiteATestThatDoesNotExist"),
            "test_slice73_eleven_day_window": (
                "TestACitedEdgeSectionMustExistWhenTheArtefactWasWritten",),
        }
        for module, classes in expected.items():
            with open(os.path.join(REPO, "tests", f"{module}.py"),
                      encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            defined = {n.name for n in ast.walk(tree)
                       if isinstance(n, ast.ClassDef)}
            for name in classes:
                assert name in defined, (module, name)

    def test_a_cited_test_name_is_never_renamed(self):
        """Slice 74's rule. EDGE.md §57e.

        Guard four fired in slice 74 when a slice-73 test was renamed while
        slice 73's SHIPPED verdict cited it. Slice 75 checks BOTH shipped
        verdicts, because eight tests were amended here and every cited name
        was kept. A shipped verdict is a record; renaming a
        test it names would make the record false, exactly as slice 70
        refused to edit slice 67's. Amend the body, never the name.
        """
        from test_slice70_eight_day_window import (  # noqa: PLC0415
            TestAVerdictMayNotCiteATestThatDoesNotExist as Guard)
        cited = set()
        for number in (73, 74):
            with open(os.path.join(REPO,
                                   f"STAGE1_VERDICT_SLICE{number}.md"),
                      encoding="utf-8") as handle:
                cited |= set(Guard.NAME.findall(handle.read()))
        # `_test_names_in_the_tree` includes module stems as well as function
        # names, which is why a verdict may name a test MODULE too.
        assert sorted(cited - Guard()._test_names_in_the_tree()) == []


class TestTheScoringWasNotReTuned:

    SUBSTITUTIONS = {
        "forward_shadow_observation/14": "forward_shadow_observation/15",
        "slice75_forward_shadow.json": "slice76_forward_shadow.json",
        "slice75_forward_shadow.log": "slice76_forward_shadow.log",
        "window_grew_since_slice74": "window_grew_since_slice75",
        "slice74_forward_shadow.json": "slice75_forward_shadow.json",
        "SLICE 75 — FORWARD SHADOW SEGMENT":
            "SLICE 76 — FORWARD SHADOW SEGMENT",
        "EDGE.md §58a, before this tool ran":
            "EDGE.md §59a, before this tool ran",
    }

    DECLARED_CHANGES = {
        "ceiling": "advances 8 -> 10 because a CATCH-UP appended two closed "
                   "days; the milestone note describes both promotions "
                   "(08-22 and 08-23), the fifth setup standing on the last "
                   "bar, and the tail moving forward with the window "
                   "(§59a, §59b, §59c)",
        "why_neither_moved": "adds §59b — the flag count doubled because two "
                             "bars closed, which is arithmetic about which "
                             "bar is last and not acceleration; and records "
                             "that the base-rate run FELL from six prints to "
                             "two, the same measurement pointing the other "
                             "way",
    }
    DECLARED_ADDITIONS = {}

    @classmethod
    def _split(cls, name, substitutions):
        class Normalise(ast.NodeTransformer):
            def visit_Constant(self, node):          # noqa: N802
                if isinstance(node.value, str):
                    value = node.value
                    for before, after in substitutions.items():
                        value = value.replace(before, after)
                    return ast.copy_location(ast.Constant(value), node)
                return node

        with open(os.path.join(REPO, "tools", name), encoding="utf-8") as h:
            tree = Normalise().visit(ast.parse(h.read()))
        build = next(n for n in tree.body
                     if isinstance(n, ast.FunctionDef) and n.name == "build")
        *before, final = build.body
        assert isinstance(final, ast.Return)
        before = [s for s in before
                  if not (isinstance(s, ast.Expr)
                          and isinstance(s.value, ast.Constant)
                          and isinstance(s.value.value, str))]
        payload = {k.value: ast.dump(v)
                   for k, v in zip(final.value.keys, final.value.values)}
        return [ast.dump(s) for s in before], payload

    def test_the_scoring_was_not_re_tuned_since_slice75(self):
        old_body, old_payload = self._split(
            "slice75_forward_shadow.py", self.SUBSTITUTIONS)
        new_body, new_payload = self._split(
            "slice76_forward_shadow.py", self.SUBSTITUTIONS)
        assert old_body == new_body, "the SCORING changed between slices"
        assert set(old_payload) - set(new_payload) == set(), "a key was DROPPED"
        drifted = {k for k, v in old_payload.items()
                   if new_payload.get(k) != v}
        assert drifted == set(self.DECLARED_CHANGES), sorted(drifted)
        added = set(new_payload) - set(old_payload)
        assert added == set(self.DECLARED_ADDITIONS), sorted(added)

    def test_the_guard_is_where_the_next_slice_will_look_for_it(self):
        with open(os.path.join(REPO, "tests",
                               "test_slice76_fifteen_day_window.py"),
                  encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)}
        assert "test_the_scoring_was_not_re_tuned_since_slice75" in names


# ===========================================================================
# 8 — expiring assertions, declared
# ===========================================================================


class TestExpiringAssertionsAreDeclared:
    """EDGE.md §55f, §56f, §57e, §59f.

    Slice 75 declared eighteen and EIGHT of them expired here — the largest
    single expiry the mechanism has handled, because a catch-up expires two
    slices' worth of dated pins at once. Every one was on the list, so the
    next slice inherited a list instead of a pytest surprise, and each was
    amended in the body with its NAME KEPT.

    The list below is this slice's, declared the day it is written rather
    than discovered by the slice that follows. It is checked against the
    module by an AST sweep for live corpus reads, so a test that reads the
    corpus and is not declared fails HERE rather than in slice 77.
    """

    EXPIRES_WITH_DATA = {
        "test_fifteen_closed_bars_lie_after_t1",
        "test_history_is_untouched_and_growth_is_an_append",
        "test_the_open_bar_is_absent",
        "test_only_btc_grew",
        "test_the_counts_came_from_this_slices_files",
        "test_08_22_and_08_23_are_now_flags",
        "test_08_24_is_a_setup_reproduced_from_the_corpus",
        "test_08_24_stands_at_the_close_and_does_not_flag",
        "test_08_20_is_still_not_a_setup",
        "test_the_gap_at_08_20_is_what_separates_them",
        "test_that_offset_is_measured_from_the_frozen_function",
        "test_neither_flag_has_a_barrier_outcome",
        "test_the_gaps_are_recomputed_from_the_corpus",
        "test_the_destinations_are_the_dates_the_note_names",
        "test_the_run_census_is_measured_not_asserted",
        "test_all_five_setups_sit_at_exactly_the_base_rate",
        "test_the_census_is_this_slices_file_and_the_prose_agrees",
        "test_the_pin_is_now_421",
        "test_the_movers_are_08_22_and_08_23_and_it_moved_by_two",
    }

    LIVE_READS = ("check", "check_all", "read_rows", "_corpus",
                  "load_corpus", "load_funding", "barrier_r_for_all_bars",
                  "funding_at_decision", "funding_setups",
                  "forward_window_indices", "close_time_ms",
                  "flags_and_directions", "directed_signal_bars", "summary")

    MODULE = "test_slice76_fifteen_day_window.py"

    @classmethod
    def _reads_live(cls, node):
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            name = getattr(inner.func, "attr", None) or getattr(
                inner.func, "id", None)
            if name in cls.LIVE_READS:
                return True
        return False

    def _functions(self):
        with open(os.path.join(REPO, "tests", self.MODULE),
                  encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        return [n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef)
                and n.name.startswith("test_")]

    def test_the_declared_list_matches_the_module(self):
        actual = {n.name for n in self._functions() if self._reads_live(n)}
        missing = sorted(actual - self.EXPIRES_WITH_DATA)
        stale = sorted(self.EXPIRES_WITH_DATA - actual)
        assert missing == [], f"undeclared live readers: {missing}"
        assert stale == [], f"declared but read nothing live: {stale}"

    def test_the_list_is_not_empty_and_not_everything(self):
        total = {n.name for n in self._functions()}
        assert 0 < len(self.EXPIRES_WITH_DATA) < len(total)

    def test_slice74s_list_named_all_four_of_its_failures(self):
        """Zero unlisted surprises from the slice-74 module this time."""
        from test_slice74_twelve_day_window import (  # noqa: PLC0415
            TestExpiringAssertionsAreDeclared as Prior)
        for name in ("test_the_counts_came_from_this_slices_files",
                     "test_08_21_is_not_a_flag_because_it_is_the_last_bar",
                     "test_it_needs_the_corpus_to_reach_08_26",
                     "test_the_census_is_measured_not_asserted"):
            assert name in Prior.EXPIRES_WITH_DATA, name

    def test_the_pre_forward_module_has_no_such_list(self):
        """Where this slice's remaining surprises came from.

        Four of the eight baseline failures were in the slice-74 module and
        all four were on its list; two were in slice 73's. The last two are
        in `test_funding_carry_fade_v1.py`, which predates the mechanism and
        pins a whole-corpus aggregate. Its movement is documented in place
        rather than listed here, because that module is not a slice module
        and is never re-derived.
        """
        with open(os.path.join(REPO, "tests",
                               "test_funding_carry_fade_v1.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        assert "EXPIRES_WITH_DATA" not in source
        assert "RECURRING amendment" in source

    def test_the_verdict_hands_the_list_forward(self):
        path = os.path.join(REPO, "STAGE1_VERDICT_SLICE76.md")
        if not os.path.isfile(path):
            return
        with open(path, encoding="utf-8") as handle:
            assert "EXPIRES_WITH_DATA" in handle.read()


# ===========================================================================
# 9 — the pack, and everything frozen
# ===========================================================================


class TestThePackAndTheFreezes:

    def test_no_restoration_was_needed_again(self):
        assert not os.path.exists(os.path.join(
            REPO, "artifacts", "slice75_restored_from_slice74.json"))
        assert load(FRESHNESS)["pack_regression"][
            "occurred_this_slice"] is False

    def test_the_pack_base_claim_is_true_this_time(self):
        claim = load(FRESHNESS)["pack_base_claim"]
        assert claim["true"] is True
        assert claim["was_false_when_first_made_in_slice67"] is True

    def test_edge_carries_every_section_from_45_to_58(self):
        with open(os.path.join(REPO, "EDGE.md"), encoding="utf-8") as handle:
            edge = handle.read()
        for section in range(45, 59):
            assert f"\n## {section}a." in edge, section

    def test_the_frozen_pack_is_intact(self):
        assert shadow.SHADOW_MAX_CONCURRENT_POSITIONS == 1
        assert shadow.SHADOW_MAX_ENTRIES_PER_DAY == 1
        assert shadow.SHADOW_MAX_NOTIONAL_USD == 100.00
        assert load(FORWARD)["constants_fingerprint_matches_frozen"] is True
        assert load(FORWARD)["schedule_mode"] == "one_entry_per_contiguous_run"
        assert load(FORWARD)["caps"]["symbol"] == "BTCUSDT"
        assert shadow.constants_fingerprint()[:32] == \
            "662de0115880871352d5d623b1020eaa"

    def test_the_monitor_thresholds_are_unchanged(self):
        thresholds = shadow.MONITOR_THRESHOLDS
        assert thresholds["M1_rolling_trades"]["warn_below"] == 0.0
        assert thresholds["M1_rolling_trades"]["alert_below"] == -0.25
        assert thresholds["M2_rolling_days"]["window_days"] == 90
        assert thresholds["M3_concentration"]["warn_above"] == 0.60
        assert thresholds["M4_halves"]["min_trades"] == 20

    def test_eleven_families_stay_frozen_and_the_universe_is_btc_only(self):
        assert len(ps.ABSENT_SIGNALS) == 11
        assert load(FORWARD)["frozen_absent_count"] == 11
        assert fb.UNIVERSE == ("BTCUSDT",)
        assert "funding_carry_fade_v1" in ps.ABSENT_SIGNALS

    def test_the_oos_clear_was_not_re_scored(self):
        assert sha256("artifacts/slice57_oos_edge_BTCUSDT_summary.json") == \
            "28b7dfe0f22c4867661bc73f6434c4acce54a0483c4469a6540155b1b3c18c51"
        assert load(FORWARD)["cleared_edge_re_scored_this_slice"] is False
        assert load(FORWARD)["is_stage1_evidence"] is False
        assert load(FORWARD)["registration_eligible"] is False
        assert fb.folds_sha256() == \
            "ff5cc8a2bb92362058b376659ff12f314c10f71a101d1f581f69da31f025013c"
        assert fb.folds_are_unmodified() is True

    def test_no_edge_measurement_ran_this_slice(self):
        for name in sorted(os.listdir(os.path.join(REPO, "artifacts"))):
            if not name.startswith("slice75_") or not name.endswith(".json"):
                continue
            text = json.dumps(load(f"artifacts/{name}"))
            for banned in ("m1_percentile", "replicates",
                           "control_attestation", "EDGE_EVIDENCE_POSITIVE"):
                assert banned not in text, (name, banned)

    def test_the_gate_refuses(self):
        assert pg.promotion_gate_allows_live() is False
        assert load(GATE)["promotion_gate_allows_live"] is False
        assert load(GATE)["items_complete"] == 2
        assert load(GATE)["items_total"] == 8
        assert pg.MIN_FORWARD_TRADES == 20
        assert pg.MIN_FORWARD_DAYS == 180
        assert load(GATE)["minimums_moved_this_slice"] is False
        assert load(GATE)["items_complete_unchanged_from_the_previous_slice"] \
            is True
        assert load(GATE)["supersedes"] == \
            "artifacts/slice75_promotion_gate.json"

    def test_no_human_item_was_completed_in_code(self):
        assert load(GATE)["human_items_completed_in_code"] == 0
        assert load(GATE)["signatures_forged"] is False
        human = [item for item in load(GATE)["evaluated"]["items"]
                 if item["owner"].startswith("human")]
        assert human and not any(item["complete"] for item in human)

    def test_live_is_dark(self):
        status = ps.current()
        assert bool(status.live_authorized) is False
        assert bool(status.models_current_present) is False
        assert status.policy_mode == "off"
        assert not os.path.exists(os.path.join(REPO, "models", "current"))

    def test_not_closer_to_autonomy(self):
        for path in (FORWARD, GATE):
            assert load(path)["closer_to_autonomous_profit_agent"] is False

    def test_the_manifest_residue_is_unchanged(self):
        audit = load(FRESHNESS)["manifests"]
        assert audit["broadcast_occurred_this_slice"] is False
        assert audit["measured_product_entries_are_accurate"] is True
        assert audit["count_disagreeing"] == 4
        assert audit["not_edited_by_this_slice"] is True

    def test_the_funding_seam_is_still_unfilled(self):
        assert load(FRESHNESS)["funding_seam"]["missing_prints"] == \
            ["2026-08-09T16:00:00+00:00"]

    def test_no_fetch_was_attempted(self):
        assert load(FRESHNESS)["fetch_attempted"] is False
