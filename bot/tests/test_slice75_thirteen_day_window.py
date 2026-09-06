"""Slice 75 — thirteen bars, ceiling 8, a second flag, a third setup, no entry.

WHAT THIS SLICE MEASURES
========================
`ceiling 8, setups 3 of 13, flagged 2, entries 0, closed trades 0`.

`2026-08-22` closed, so `2026-08-21` is no longer the corpus's last bar and it
**flags** — the transition `2026-08-19` made in slice 73. `2026-08-22` is itself
a **third setup**, close-joining at exactly `FUND_ABS`, and being the last bar it
does not flag.

**Nothing is scoreable.** Eligibility reaches back seven bars, so with
`2026-08-22` last the highest scoreable bar is `2026-08-15`. The gaps are
recomputed from the corpus every slice and never carried forward — §57b reused a
distance and got it wrong:

    08-19   4 closed days away   (2026-08-26)
    08-21   6                    (2026-08-28)
    08-22   7                    (2026-08-29)

WHAT THIS SLICE ADDS
====================
**Three setups are at most two entries.** `one_entry_per_contiguous_run` admits
one entry per contiguous run of flags. `08-19` is isolated; `08-21` and `08-22`
are adjacent and form one run. So the schedule's output is bounded at two even
once every other gate opens — the first slice where the distance between a setup
count and a trade count is more than one step. EDGE.md §58c.

**The base rate is what is accumulating.** All three setups sit at exactly
`FUND_ABS`, which §57c established is the venue's base rate, not an extreme. The
current run of base-rate prints is six (48 hours) — the 89th percentile of 294
such runs, mean 4.0, longest 70. Unremarkable by the corpus's own standard, and
precisely the moment at which moving `FUND_ABS` would feel most reasonable and
be most wrong. Nothing moved. EDGE.md §58d.
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

FRESHNESS = "artifacts/slice75_data_freshness.json"
FORWARD = "artifacts/slice75_forward_shadow.json"
GATE = "artifacts/slice75_promotion_gate.json"

WINDOW = ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13",
          "2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17",
          "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21",
          "2026-08-22"]

PRIOR = {n: (f"artifacts/slice{n}_forward_shadow.json", ceiling)
         for n, ceiling in ((62, 0), (63, 0), (64, 0), (65, 0), (66, 0),
                            (67, 0), (68, 1), (69, 2), (70, 3), (71, 4),
                            (72, 5), (73, 6), (74, 7))}


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
# 1 — the window reached thirteen
# ===========================================================================


class TestTheWindowReachedThirteen:

    def test_thirteen_closed_bars_lie_after_t1(self):
        result = cp.check(cp.LINEAR_BTC)
        assert result.appended_rows >= 13
        assert [s[:10] for s in result.appended_timestamps][:13] == WINDOW
        assert load(FRESHNESS)["after_t1_linear"] == 13
        assert load(FRESHNESS)["after_t1_dates"] == WINDOW

    def test_the_growth_is_measured_against_the_previous_artefact(self):
        delta = load(FRESHNESS)["delta_since_slice74"]
        assert delta["previous_after_t1_linear"] == 12
        assert delta["new_linear_bars_since_slice74"] == 1
        assert delta["the_window_grew"] is True

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
        delta = load(FRESHNESS)["delta_since_slice74"]
        assert delta["new_funding_prints_since_slice74"] == 2
        assert load(FRESHNESS)["latest_funding_timestamp"] == \
            "2026-08-23T08:00:00+00:00"
        digests = load(FRESHNESS)["digests"]
        assert digests["funding_genuinely_changed_this_slice"] is True
        assert digests["funding_identical_to_slice74_uncompressed"] is False

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
            "docs/human/HUMAN_DATA_NOTE_SLICE75.md"
        assert payload["human_note_present"] is True
        assert payload["slice"] == 75
        with open(os.path.join(REPO, "tools", "slice75_freshness.py"),
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
        assert slice_const.value.value == 75

    def test_the_slice_number_is_derived_in_every_tool(self):
        for name in ("slice75_freshness.py", "slice75_forward_shadow.py",
                     "slice75_promotion_gate.py"):
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
            assert load(artefact)["slice"] == 75, artefact

    def test_the_counts_came_from_this_slices_files(self):
        """AMENDED BY SLICE 75; on slice 74's EXPIRES_WITH_DATA list."""
        assert cp.check(cp.LINEAR_BTC).rows_on_disk >= \
            load(FRESHNESS)["linear_rows"]
        assert cp.check(cp.FUNDING_BTC).rows_on_disk >= \
            load(FRESHNESS)["funding_rows"]
        assert load(FRESHNESS)["linear_rows"] == 1474
        assert load(FRESHNESS)["funding_rows"] == 4424

    def test_the_finding_says_thirteen_and_not_twelve(self):
        for text in (prose(load(FRESHNESS)["delta_since_slice74"]["finding"]),
                     prose(load(FORWARD)["window_grew_since_slice74"]["note"])):
            assert "TWELVE BARS" not in text.upper()
            assert "THIRTEEN BARS" in text.upper()
        finding = prose(load(FRESHNESS)["delta_since_slice74"]["finding"])
        assert "ceiling 8, setups 3 of 13, fills 0" in finding

    def test_the_spelled_number_table_no_longer_runs_out(self):
        """It stopped at TWELVE and the window reached thirteen. EDGE.md §58f.

        It fell back to the integer rather than lying, which is why a derived
        field is chosen — but a lookup with a bounded domain is a constant
        waiting to run out. Extended, with the fallback kept so the next
        overflow is loud.
        """
        for name in ("slice75_freshness.py", "slice75_forward_shadow.py"):
            with open(os.path.join(REPO, "tools", name),
                      encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            table = next(
                n.value for n in tree.body
                if isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "_SPELLED"
                        for t in n.targets))
            keys = {k.value for k in table.keys}
            assert 13 in keys and max(keys) >= 20, name


# ===========================================================================
# 2 — the second flag, the third setup, and the ladder
# ===========================================================================


class TestTheSecondFlagAndThirdSetup:

    def test_the_state_ladder_reads_3_2_0_0_0_0(self):
        ladder = load(FORWARD)["state_ladder"]
        assert ladder["1_setups"] == 3
        assert ladder["2_flagged"] == 2
        assert ladder["3_eligible_and_flagged"] == 0
        assert ladder["4_candidates_after_schedule"] == 0
        assert ladder["5_entries_taken"] == 0
        assert ladder["6_closed_trades"] == 0

    def test_the_setups_are_08_19_08_21_and_08_22(self):
        regime = load(FRESHNESS)["forward_funding_regime"]
        assert regime["forward_bars"] == 13
        assert regime["funding_setups_in_window"] == 3
        assert regime["setup_dates"] == \
            ["2026-08-19", "2026-08-21", "2026-08-22"]
        assert set(regime["setup_directions"].values()) == {fb.SHORT_SETUP}

    def test_08_21_is_now_a_flag(self):
        """The transition 08-19 made in slice 73, made by 08-21 here.

        Slice 74 recorded it as a last-bar setup that could not flag. The
        corpus moved, not the rule.
        """
        bars, funding, _folds = _corpus()
        flags, directions = fb.flags_and_directions(bars, funding, warmup=200)
        index = next(i for i in range(len(bars))
                     if _day(bars, i) == "2026-08-21")
        assert index < len(bars) - 1
        assert bool(flags[index]) is True
        assert directions[index] == fb.SHORT_SETUP
        assert [f[:10] for f in load(FORWARD)["forward_decisions"][
            "flagged_bars_in_window"]] == ["2026-08-19", "2026-08-21"]
        # Slice 74's frozen record of what it was then.
        assert load("artifacts/slice74_forward_shadow.json")[
            "forward_decisions"]["flagged_bars_in_window"] == \
            ["2026-08-19T00:00:00Z"]

    def test_08_22_is_a_setup_reproduced_from_the_corpus(self):
        bars, funding, folds = _corpus()
        window = list(fb.forward_window_indices(bars, folds))
        rates = fb.funding_at_decision(bars, funding)
        setups = fb.funding_setups(bars, funding, fund_abs=fb.FUND_ABS)
        index = next(i for i in window if _day(bars, i) == "2026-08-22")
        assert rates[index] == fb.FUND_ABS
        assert index in setups
        assert setups[index] == fb.SHORT_SETUP

    def test_08_22_stands_at_the_close_and_does_not_flag(self):
        bars, funding, _folds = _corpus()
        close = fb.close_time_ms(bars[-1])
        assert close == int(bars[-1].start_ms) + 86_400_000 - 1
        prints = [r for r in cp.read_rows(cp.FUNDING_BTC)
                  if r["funding_time"][:10] == "2026-08-22"]
        assert [p["funding_time"][11:16] for p in prints] == \
            ["00:00", "08:00", "16:00"]
        assert float(prints[-1]["funding_rate"]) == fb.FUND_ABS
        flags, _directions = fb.flags_and_directions(bars, funding, warmup=200)
        assert bool(flags[len(bars) - 1]) is False
        assert load(FRESHNESS)["forward_funding_regime"][
            "setup_bars_that_are_the_last_bar_of_the_corpus"] == \
            ["2026-08-22"]

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
# 3 — three setups are at most two entries
# ===========================================================================


class TestThreeSetupsAreAtMostTwoEntries:
    """EDGE.md §58c. `one_entry_per_contiguous_run` bounds the schedule."""

    def test_the_runs_are_08_19_alone_and_08_21_with_08_22(self):
        runs = load(FORWARD)["ceiling"]["contiguous_setup_runs"]
        assert runs == [["2026-08-19"], ["2026-08-21", "2026-08-22"]]

    def test_the_schedule_admits_at_most_two_not_three(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["max_entries_the_schedule_would_admit"] == 2
        assert load(FORWARD)["state_ladder"]["1_setups"] == 3
        assert load(FORWARD)["schedule_mode"] == \
            "one_entry_per_contiguous_run"

    def test_the_gap_at_08_20_is_what_separates_them(self):
        """If 08-20 had been a setup all three would be one run, and the
        schedule would admit ONE.

        AMENDED BY SLICE 76; on slice 75's EXPIRES_WITH_DATA list. NAME KEPT
        — `STAGE1_VERDICT_SLICE75.md` cites it, and the rule is amend the
        body, never the name.

        Slice 75 saw three setups. Slice 76 caught up TWO closed days and the
        live window holds FIVE: `2026-08-23` and `2026-08-24` close-joined at
        the base rate as well. What the test is NAMED for does not expire —
        `2026-08-20` is still not a setup, so `2026-08-19` stays isolated and
        everything from `2026-08-21` onward is ONE contiguous run, and the
        schedule's bound is still TWO however long that second run gets.
        Slice 75's three are asserted against its FROZEN artefact; the
        separation is asserted live. EDGE.md §59f.
        """
        bars, funding, folds = _corpus()
        window = list(fb.forward_window_indices(bars, folds))
        setups = fb.funding_setups(bars, funding, fund_abs=fb.FUND_ABS)
        dates = [_day(bars, i) for i in window if i in setups]
        # Slice 75's record, frozen, derived from its own artefact.
        assert [e["bar_utc"][:10]
                for e in load(FORWARD)["forward_decisions"][
                    "why_the_rule_stood_aside"]
                if e["funding_setup_present"]] == \
            ["2026-08-19", "2026-08-21", "2026-08-22"]
        # Live: the corpus only appends, so slice 75's three are still the
        # first three, in order.
        assert dates[:3] == ["2026-08-19", "2026-08-21", "2026-08-22"]
        gap = next(i for i in window if _day(bars, i) == "2026-08-20")
        assert gap not in setups
        # The separation itself: exactly two runs, starting where they did.
        runs, previous = [], None
        for index in [i for i in window if i in setups]:
            if previous is None or index != previous + 1:
                runs.append([])
            runs[-1].append(_day(bars, index))
            previous = index
        assert [run[0] for run in runs] == ["2026-08-19", "2026-08-21"]
        assert len(runs) == 2

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
        assert block["highest_scoreable_bar_utc"][:10] == "2026-08-15"

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
        for date in ("2026-08-19", "2026-08-21", "2026-08-22"):
            index = next(i for i in range(len(bars))
                         if _day(bars, i) == date)
            assert index not in scoreable, date
            assert date in load(FORWARD)["barrier_eligibility"][
                "forward_bars_that_are_not_scoreable"]

    def test_the_gaps_are_recomputed_from_the_corpus(self):
        """§57b reused a distance from the slice before and got it wrong.

        Every gap is `(i + HORIZON + 2) - last`, read from indices this
        slice. 08-19 is FOUR closed days away now; it was five when the last
        bar was 08-21, and §57e owns the earlier error.

        AMENDED BY SLICE 76; on slice 75's EXPIRES_WITH_DATA list. The
        formula is what is permanent; every number in it is relative to a
        LAST BAR, and slice 76 moved the last bar by two. Slice 75's numbers
        are recomputed against slice 75's OWN last bar, which is read from
        its frozen artefact rather than assumed; the live gaps are recomputed
        against the live one. Reusing either set as the other is exactly the
        §57b error. EDGE.md §59f.
        """
        bars, _funding, _folds = _corpus()
        last = len(bars) - 1
        index_of = {}
        for i in range(len(bars)):
            index_of[_day(bars, i)] = i
        frozen_last = index_of[load(FRESHNESS)["after_t1_dates"][-1]]
        gaps = load(FORWARD)["barrier_eligibility"][
            "closed_days_until_scoreable"]
        for date, remaining in gaps.items():
            assert remaining == (index_of[date] + fb.HORIZON + 2) - \
                frozen_last, date
        assert gaps["2026-08-19"] == 4
        assert gaps["2026-08-21"] == 6
        assert gaps["2026-08-22"] == 7
        # Live, recomputed this slice: two bars closed, so every distance
        # shrank by two, and two more setups joined the queue behind them.
        assert last - frozen_last == 2
        live = {date: (index_of[date] + fb.HORIZON + 2) - last
                for date in ("2026-08-19", "2026-08-21", "2026-08-22",
                             "2026-08-23", "2026-08-24")}
        assert live == {"2026-08-19": 2, "2026-08-21": 4, "2026-08-22": 5,
                        "2026-08-23": 6, "2026-08-24": 7}

    def test_the_destinations_are_the_dates_the_note_names(self):
        """AMENDED BY SLICE 76; on slice 75's EXPIRES_WITH_DATA list.

        The DESTINATION is permanent and the DISTANCE is not — slice 74's
        §58b lesson, now applied to slice 75's own numbers. Slice 75's gaps
        are added to slice 75's last day; the live gaps are added to the live
        last day; both land on the same calendar dates, which is the whole
        point of the claim. EDGE.md §59f.
        """
        bars, _funding, _folds = _corpus()

        def as_date(text):
            return dt.datetime.strptime(text, "%Y-%m-%d").replace(
                tzinfo=dt.timezone.utc)

        live_last = _day(bars, len(bars) - 1)
        frozen_last = load(FRESHNESS)["after_t1_dates"][-1]
        gaps = load(FORWARD)["barrier_eligibility"][
            "closed_days_until_scoreable"]
        for date, expected in (("2026-08-19", "2026-08-26"),
                               ("2026-08-21", "2026-08-28"),
                               ("2026-08-22", "2026-08-29")):
            target = as_date(frozen_last) + dt.timedelta(days=gaps[date])
            assert target.strftime("%Y-%m-%d") == expected, date
            index = next(i for i in range(len(bars))
                         if _day(bars, i) == date)
            live_gap = (index + fb.HORIZON + 2) - (len(bars) - 1)
            live_target = as_date(live_last) + dt.timedelta(days=live_gap)
            assert live_target.strftime("%Y-%m-%d") == expected, date

    def test_six_forward_bars_are_scoreable_now(self):
        block = load(FORWARD)["barrier_eligibility"]
        assert block["forward_bars_that_are_scoreable"] == WINDOW[:6]
        assert load(FORWARD)["ceiling"][
            "forward_bars_with_a_barrier_outcome"] == 6

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
        """AMENDED BY SLICE 76; on slice 75's EXPIRES_WITH_DATA list.

        The census was recomputed against the live funding series, which
        grows — slice 76 appended prints and both the run count and the
        TRAILING run changed. Slice 75's counts stay pinned to its frozen
        artefact; what is asserted live is the SHAPE that does not expire:
        the corpus only gains prints, run counts only rise, and the longest
        run cannot shrink. The trailing run is the one number here that can
        fall, and it did — which is why it is measured, not carried.
        EDGE.md §59f.
        """
        import itertools
        runs_block = load(FRESHNESS)["forward_funding_regime"][
            "base_rate_runs"]
        values = [float(r["funding_rate"])
                  for r in cp.read_rows(cp.FUNDING_BTC)]
        runs = [len(list(g)) for at, g in itertools.groupby(
            v == fb.FUND_ABS for v in values) if at]
        # Slice 75's frozen record.
        assert runs_block["runs_at_exactly_base"] == 294
        assert runs_block["longest_run_prints"] == 70
        assert runs_block["current_trailing_run_prints"] == 6
        # Live, measured this slice: monotone in the directions that are
        # monotone, and free in the one that is not.
        assert len(runs) >= runs_block["runs_at_exactly_base"]
        assert max(runs) >= runs_block["longest_run_prints"]
        trailing = 0
        for value in reversed(values):
            if value != fb.FUND_ABS:
                break
            trailing += 1
        assert trailing >= 1
        assert trailing <= max(runs)

    def test_the_current_run_is_unremarkable(self):
        runs_block = load(FRESHNESS)["forward_funding_regime"][
            "base_rate_runs"]
        assert runs_block["current_trailing_run_prints"] == 6
        assert runs_block["current_trailing_run_hours"] == 48
        assert runs_block["longest_run_prints"] > \
            10 * runs_block["current_trailing_run_prints"]
        assert runs_block["percentile_of_current_run"] < 95.0

    def test_all_three_setups_sit_at_exactly_the_base_rate(self):
        """AMENDED BY SLICE 76; on slice 75's EXPIRES_WITH_DATA list. NAME
        KEPT — `STAGE1_VERDICT_SLICE75.md` cites it.

        "Three" was true of slice 75's window and is now FIVE. The claim the
        test exists to make is not the count but the RATE: every qualifying
        bar sits at exactly `FUND_ABS`, none above it. That is still true of
        all five, which is the §57c finding continuing to hold rather than a
        reason to move the threshold. Slice 75's three are pinned to its
        frozen artefact. EDGE.md §59f.
        """
        bars, funding, folds = _corpus()
        window = list(fb.forward_window_indices(bars, folds))
        rates = fb.funding_at_decision(bars, funding)
        setups = fb.funding_setups(bars, funding, fund_abs=fb.FUND_ABS)
        qualifying = [rates[i] for i in window if i in setups]
        assert len([e for e in load(FORWARD)["forward_decisions"][
            "why_the_rule_stood_aside"] if e["funding_setup_present"]]) == 3
        assert len(qualifying) == 5
        assert all(rate == fb.FUND_ABS for rate in qualifying)
        assert not any(rate > fb.FUND_ABS for rate in qualifying)

    def test_the_threshold_is_still_the_modal_value_and_not_a_cap(self):
        census = load(FRESHNESS)["forward_funding_regime"]["threshold_census"]
        assert census["fund_abs_is_a_cap"] is False
        assert census["fund_abs_is_the_modal_value"] is True
        assert census["strictly_above_fund_abs"] > 0

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

    def test_the_ceiling_is_eight(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["closed_forward_bars"] == 13
        assert ceiling["max_possible_forward_closed_trades"] == 8
        assert ceiling["observed_forward_closed_trades"] == 0
        assert ceiling["within_ceiling"] is True
        assert "§58a" in ceiling["declared_in"]

    def test_it_is_not_seven_and_not_nine(self):
        ceiling = load(FORWARD)["ceiling"]["max_possible_forward_closed_trades"]
        assert ceiling != 7, "7 was slice 74's ceiling, at N=12"
        assert ceiling < 9, "9 would need N=14"

    def test_the_unconditional_bound_is_commentary_only(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["unconditional_upper_bound"] == 12
        assert ceiling["max_possible_forward_closed_trades"] == 8
        assert ceiling["observed_forward_closed_trades"] <= \
            ceiling["forward_bars_with_a_barrier_outcome"]

    def test_every_prior_slice_ceiling_is_recorded_and_lower(self):
        observed = [load(f)["ceiling"]["max_possible_forward_closed_trades"]
                    for _n, (f, _c) in sorted(PRIOR.items())]
        assert observed == [expected for _n, (_f, expected)
                            in sorted(PRIOR.items())]
        assert observed == [0, 0, 0, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7]
        assert load(FORWARD)["ceiling"][
            "max_possible_forward_closed_trades"] == 8

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
    """417 -> 418 (slice 73) -> 419 (slice 75), each by exactly one."""

    def test_the_pin_is_now_419(self):
        """AMENDED BY SLICE 76; on slice 75's EXPIRES_WITH_DATA list. NAME
        KEPT — it carries a number that is now history, and renaming it would
        make `STAGE1_VERDICT_SLICE75.md` cite a test that does not exist.

        419 -> 421. Slice 76 caught up TWO closed days, so `2026-08-22` and
        `2026-08-23` both stopped being the corpus's last bar and both became
        directed signal bars. `long_setups` did not move, so both are SHORT.
        EDGE.md §59f.
        """
        from signals import funding_carry_fade_v1 as fc  # noqa: PLC0415
        bars, funding, _folds = _corpus()
        summary = fc.summary(bars, funding, warmup=200)
        assert summary["setups"] == 421
        assert summary["long_setups"] == 6
        assert summary["bars_without_funding"] == 0

    def test_the_mover_is_08_21_and_it_moved_by_exactly_one(self):
        """AMENDED BY SLICE 76; on slice 75's EXPIRES_WITH_DATA list. NAME
        KEPT, AND THE NAME IS NOW WRONG IN ITS SECOND HALF.

        It did not move by one. Slice 75 appended one bar and the pin moved
        by one; slice 76 caught up TWO closed days and it moved by TWO, with
        `2026-08-23` — not `2026-08-21` — as the newest directed bar. "By
        exactly one" was true of every slice that had run when it was
        written, which is precisely how a true observation hardens into a
        wrong rule. The durable statement is that the pin moves by the number
        of bars APPENDED, each one nameable. The name is left alone because
        `STAGE1_VERDICT_SLICE75.md` cites it and a shipped verdict must not
        be made false by a rename; the correction lives in the body and in
        EDGE.md §59f, where it is on the record rather than tidied away.
        """
        from signals import funding_carry_fade_v1 as fc  # noqa: PLC0415
        bars, funding, _folds = _corpus()
        directed = fc.directed_signal_bars(bars, funding, warmup=200)
        by_date = {_day(bars, i): d for i, d in directed}
        # Slice 75's mover: still directed, still SHORT, permanently so.
        assert by_date["2026-08-21"] == fc.SHORT_SETUP
        # This slice's movers, both of them, and the step size.
        index, direction = directed[-1]
        assert _day(bars, index) == "2026-08-23"
        assert direction == fc.SHORT_SETUP
        assert by_date["2026-08-22"] == fc.SHORT_SETUP
        assert len(directed) == 421
        appended = load(FRESHNESS)["after_t1_linear"]
        assert len(list(fb.forward_window_indices(
            bars, _folds))) - appended == 2

    def test_this_is_now_a_recurring_amendment_not_a_surprise(self):
        """Every forward bar that stops being last adds exactly one.

        The pin is still worth keeping as an absolute — a join change would
        fail here with a number — but its movement is expected, nameable and
        must be by one.
        """
        with open(os.path.join(REPO, "tests",
                               "test_funding_carry_fade_v1.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        assert "418 -> 419 IN SLICE 75" in source
        assert "RECURRING amendment" in source
# ===========================================================================
# 6 — STEP 1 landed, and the guard that checks it
# ===========================================================================


class TestTheDesignNoteLandedThisTime:

    def test_edge_carries_58a_through_58e(self):
        with open(os.path.join(REPO, "EDGE.md"), encoding="utf-8") as handle:
            edge = handle.read()
        for letter in "abcde":
            assert f"\n## 58{letter}." in edge, letter

    def test_the_artefacts_recorded_58_as_present_when_they_ran(self):
        for path in (FRESHNESS, FORWARD):
            recorded = load(path)["edge_state_when_this_ran"]["sections_present"]
            assert 58 in recorded, path

    def test_the_ceilings_citation_resolves(self):
        declared = load(FORWARD)["ceiling"]["declared_in"]
        assert "§58a" in declared
        assert 58 in load(FORWARD)["edge_state_when_this_ran"][
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
                    if os.path.basename(p).startswith("slice75_")}
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
        path = os.path.join(REPO, "STAGE1_VERDICT_SLICE75.md")
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
        "forward_shadow_observation/13": "forward_shadow_observation/14",
        "slice74_forward_shadow.json": "slice75_forward_shadow.json",
        "slice74_forward_shadow.log": "slice75_forward_shadow.log",
        "window_grew_since_slice73": "window_grew_since_slice74",
        "slice73_forward_shadow.json": "slice74_forward_shadow.json",
        "SLICE 74 — FORWARD SHADOW SEGMENT":
            "SLICE 75 — FORWARD SHADOW SEGMENT",
        "EDGE.md §57a, before this tool ran":
            "EDGE.md §58a, before this tool ran",
    }

    DECLARED_CHANGES = {
        "ceiling": "advances 7 -> 8; gains contiguous_setup_runs and "
                   "max_entries_the_schedule_would_admit (§58c); the "
                   "milestone note describes the second flag and third setup",
        "why_neither_moved": "adds §58d — the base-rate run is at the 89th "
                             "percentile and unremarkable, which is when "
                             "moving FUND_ABS would feel most reasonable",
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

    def test_the_scoring_was_not_re_tuned_since_slice74(self):
        old_body, old_payload = self._split(
            "slice74_forward_shadow.py", self.SUBSTITUTIONS)
        new_body, new_payload = self._split(
            "slice75_forward_shadow.py", self.SUBSTITUTIONS)
        assert old_body == new_body, "the SCORING changed between slices"
        assert set(old_payload) - set(new_payload) == set(), "a key was DROPPED"
        drifted = {k for k, v in old_payload.items()
                   if new_payload.get(k) != v}
        assert drifted == set(self.DECLARED_CHANGES), sorted(drifted)
        added = set(new_payload) - set(old_payload)
        assert added == set(self.DECLARED_ADDITIONS), sorted(added)

    def test_the_guard_is_where_the_next_slice_will_look_for_it(self):
        with open(os.path.join(REPO, "tests",
                               "test_slice75_thirteen_day_window.py"),
                  encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)}
        assert "test_the_scoring_was_not_re_tuned_since_slice74" in names


# ===========================================================================
# 8 — expiring assertions, declared
# ===========================================================================


class TestExpiringAssertionsAreDeclared:
    """EDGE.md §55f, §56f, §57e.

    Slice 73 declared twelve and three of them expired here, all on the list.
    Two guard-five tests ALSO expired and were NOT on it, because the sweep
    looks for corpus reads and those read a document. The sweep is widened.
    """

    EXPIRES_WITH_DATA = {
        "test_thirteen_closed_bars_lie_after_t1",
        "test_history_is_untouched_and_growth_is_an_append",
        "test_the_open_bar_is_absent",
        "test_only_btc_grew",
        "test_the_counts_came_from_this_slices_files",
        "test_08_21_is_now_a_flag",
        "test_08_22_is_a_setup_reproduced_from_the_corpus",
        "test_08_22_stands_at_the_close_and_does_not_flag",
        "test_08_20_is_still_not_a_setup",
        "test_the_gap_at_08_20_is_what_separates_them",
        "test_that_offset_is_measured_from_the_frozen_function",
        "test_neither_flag_has_a_barrier_outcome",
        "test_the_gaps_are_recomputed_from_the_corpus",
        "test_the_destinations_are_the_dates_the_note_names",
        "test_the_run_census_is_measured_not_asserted",
        "test_all_three_setups_sit_at_exactly_the_base_rate",
        "test_the_pin_is_now_419",
        "test_the_mover_is_08_21_and_it_moved_by_exactly_one",
    }

    LIVE_READS = ("check", "check_all", "read_rows", "_corpus",
                  "load_corpus", "load_funding", "barrier_r_for_all_bars",
                  "funding_at_decision", "funding_setups",
                  "forward_window_indices", "close_time_ms",
                  "flags_and_directions", "directed_signal_bars", "summary")

    MODULE = "test_slice75_thirteen_day_window.py"

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
        path = os.path.join(REPO, "STAGE1_VERDICT_SLICE75.md")
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
        assert load(GATE)["items_complete_unchanged_from_slice_74"] is True

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
