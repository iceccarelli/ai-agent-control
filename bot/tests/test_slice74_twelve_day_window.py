"""Slice 74 — twelve bars, ceiling 7, a second setup, and still no entry.

WHAT THIS SLICE MEASURES
========================
`ceiling 7, setups 2 of 12, flagged 1, entries 0, closed trades 0`.

`2026-08-21` close-joins at exactly `FUND_ABS` and stands at the bar's close —
the **second** forward setup. It is the last bar, so it is not a flag: the same
structure `2026-08-19` was in one slice ago.

`2026-08-19` is still flagged and still **not scoreable**. §56d fixed the
boundary at `last − 7`; the last bar is `2026-08-21`, so the highest scoreable
bar is `2026-08-14`. It needs the corpus to reach `2026-08-26` — four days,
by subtraction rather than forecast.

WHAT THIS SLICE FOUND
=====================
**The threshold sits on the venue's base rate.** `0.0001` is not a cap and not
an extreme: 1,174 of 4,422 prints sit exactly there (26.6%, the mode), 282
exceed it, and the maximum is `0.00088148`. `FUND_ABS` with an inclusive `>=`
therefore selects **base-or-above** funding; the rule's selectivity comes from
the close-time join and `one_entry_per_contiguous_run`, not from the threshold
being rare.

That corrects the programme's **language**, moves no measured number, and is
**not** a reason to touch `FUND_ABS` — a threshold changed after seeing which
prints qualify is fitted to data whether the change is dressed as an
optimisation or as a correction. EDGE.md §57c.

STEP 1 LANDED THIS TIME
=======================
§56h recorded a design note that never reached the repository. §57a was written,
grepped and committed before a single tool was derived, and the artefacts record
the EDGE sections present when they ran.
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

FRESHNESS = "artifacts/slice74_data_freshness.json"
FORWARD = "artifacts/slice74_forward_shadow.json"
GATE = "artifacts/slice74_promotion_gate.json"

WINDOW = ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13",
          "2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17",
          "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21"]

PRIOR = {n: (f"artifacts/slice{n}_forward_shadow.json", ceiling)
         for n, ceiling in ((62, 0), (63, 0), (64, 0), (65, 0), (66, 0),
                            (67, 0), (68, 1), (69, 2), (70, 3), (71, 4),
                            (72, 5), (73, 6))}


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
# 1 — the window reached twelve
# ===========================================================================


class TestTheWindowReachedTwelve:

    def test_twelve_closed_bars_lie_after_t1(self):
        result = cp.check(cp.LINEAR_BTC)
        assert result.appended_rows >= 12
        assert [s[:10] for s in result.appended_timestamps][:12] == WINDOW
        assert load(FRESHNESS)["after_t1_linear"] == 12
        assert load(FRESHNESS)["after_t1_dates"] == WINDOW

    def test_the_growth_is_measured_against_the_previous_artefact(self):
        delta = load(FRESHNESS)["delta_since_slice73"]
        assert delta["previous_after_t1_linear"] == 11
        assert delta["new_linear_bars_since_slice73"] == 1
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
        delta = load(FRESHNESS)["delta_since_slice73"]
        assert delta["new_funding_prints_since_slice73"] == 3
        assert load(FRESHNESS)["latest_funding_timestamp"] == \
            "2026-08-22T16:00:00+00:00"
        digests = load(FRESHNESS)["digests"]
        assert digests["funding_genuinely_changed_this_slice"] is True
        assert digests["funding_identical_to_slice73_uncompressed"] is False

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
        assert len(payload["human_note_claims_checked"]) >= 15

    def test_the_note_path_names_this_slice_and_slice_is_derived(self):
        payload = load(FRESHNESS)
        assert payload["human_note_path"] == \
            "docs/human/HUMAN_DATA_NOTE_SLICE74.md"
        assert payload["human_note_present"] is True
        assert payload["slice"] == 74
        with open(os.path.join(REPO, "tools", "slice74_freshness.py"),
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
        assert slice_const.value.value == 74

    def test_the_slice_number_is_derived_in_every_tool(self):
        for name in ("slice74_freshness.py", "slice74_forward_shadow.py",
                     "slice74_promotion_gate.py"):
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
            assert load(artefact)["slice"] == 74, artefact

    def test_the_counts_came_from_this_slices_files(self):
        """AMENDED BY SLICE 75; on slice 74's EXPIRES_WITH_DATA list."""
        assert cp.check(cp.LINEAR_BTC).rows_on_disk >= \
            load(FRESHNESS)["linear_rows"]
        assert cp.check(cp.FUNDING_BTC).rows_on_disk >= \
            load(FRESHNESS)["funding_rows"]
        assert load(FRESHNESS)["linear_rows"] == 1473
        assert load(FRESHNESS)["funding_rows"] == 4422

    def test_the_finding_says_twelve_and_not_eleven(self):
        for text in (prose(load(FRESHNESS)["delta_since_slice73"]["finding"]),
                     prose(load(FORWARD)["window_grew_since_slice73"]["note"])):
            assert "ELEVEN BARS" not in text.upper()
            assert "TWELVE BARS" in text.upper()
        finding = prose(load(FRESHNESS)["delta_since_slice73"]["finding"])
        assert "ceiling 7, setups 2 of 12, fills 0" in finding


# ===========================================================================
# 2 — a second setup, and the ladder
# ===========================================================================


class TestTheSecondSetup:

    def test_the_state_ladder_reads_2_1_0_0_0_0(self):
        ladder = load(FORWARD)["state_ladder"]
        assert ladder["1_setups"] == 2
        assert ladder["2_flagged"] == 1
        assert ladder["3_eligible_and_flagged"] == 0
        assert ladder["4_candidates_after_schedule"] == 0
        assert ladder["5_entries_taken"] == 0
        assert ladder["6_closed_trades"] == 0

    def test_the_setups_are_08_19_and_08_21(self):
        regime = load(FRESHNESS)["forward_funding_regime"]
        assert regime["forward_bars"] == 12
        assert regime["funding_setups_in_window"] == 2
        assert regime["setup_dates"] == ["2026-08-19", "2026-08-21"]
        assert set(regime["setup_directions"].values()) == {fb.SHORT_SETUP}

    def test_08_21_is_reproduced_from_the_corpus_not_the_artefact(self):
        bars, funding, folds = _corpus()
        window = list(fb.forward_window_indices(bars, folds))
        rates = fb.funding_at_decision(bars, funding)
        setups = fb.funding_setups(bars, funding, fund_abs=fb.FUND_ABS)
        index = next(i for i in window if _day(bars, i) == "2026-08-21")
        assert rates[index] == fb.FUND_ABS
        assert index in setups
        assert setups[index] == fb.SHORT_SETUP

    def test_the_qualifying_print_stands_at_the_close(self):
        """23:59:59.999Z is strictly before the next day's 00:00 print."""
        bars, _funding, _folds = _corpus()
        close = fb.close_time_ms(bars[-1])
        midnight = int(bars[-1].start_ms) + 86_400_000
        assert close == midnight - 1
        prints = [r for r in cp.read_rows(cp.FUNDING_BTC)
                  if r["funding_time"][:10] == "2026-08-21"]
        assert [p["funding_time"][11:16] for p in prints] == \
            ["00:00", "08:00", "16:00"]
        assert float(prints[-1]["funding_rate"]) == fb.FUND_ABS

    def test_08_21_is_not_a_flag_because_it_is_the_last_bar(self):
        """AMENDED BY SLICE 75; on slice 74's list. NAME KEPT — slice 74's
        verdict cites it, and slice 74's rule is amend the body, never the
        name.

        The claim was true of slice 74's corpus and is now false of the live
        one: 2026-08-22 closed, so 08-21 is no longer last and DOES flag —
        the transition 08-19 made a slice earlier. What the test now asserts
        is the mechanism, which is durable, against slice 74's FROZEN record.
        EDGE.md §58b.
        """
        frozen = load(FRESHNESS)
        assert frozen["after_t1_dates"][-1] == "2026-08-21"
        assert frozen["forward_funding_regime"][
            "setup_bars_that_are_the_last_bar_of_the_corpus"] == \
            ["2026-08-21"]
        assert [f[:10] for f in load(FORWARD)["forward_decisions"][
            "flagged_bars_in_window"]] == ["2026-08-19"]
        # The mechanism, live: whatever the last bar is, it cannot flag.
        bars, funding, _folds = _corpus()
        flags, _directions = fb.flags_and_directions(bars, funding, warmup=200)
        assert bool(flags[len(bars) - 1]) is False

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
# 3 — 08-19 is still not scoreable, and that was a subtraction
# ===========================================================================


class TestTheFlagIsStillNotScoreable:

    def test_eligibility_still_reaches_back_seven_bars(self):
        block = load(FORWARD)["barrier_eligibility"]
        assert block["reaches_back_from_the_end_by_bars"] == 7
        assert block["highest_scoreable_bar_utc"][:10] == "2026-08-14"

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

    def test_08_19_has_no_barrier_outcome(self):
        bars, _funding, _folds = _corpus()
        idx, _net, _used = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=fb.TAKE_PROFIT_ATR, stop_atr=fb.STOP_ATR,
            horizon=fb.HORIZON, atr_period=fb.ATR_PERIOD,
            round_trip_bps=fb.ROUND_TRIP_BPS, side="short",
            entry_on=fb.ENTRY_ON)
        index = next(i for i in range(len(bars))
                     if _day(bars, i) == "2026-08-19")
        assert index not in {int(i) for i in idx}
        assert "2026-08-19" in load(FORWARD)["barrier_eligibility"][
            "forward_bars_that_are_not_scoreable"]

    def test_it_needs_the_corpus_to_reach_08_26(self):
        """Arithmetic, not forecast — and the arithmetic was done wrong.

        §57b said "four days short" and the mission said the same. The corpus
        says FIVE: 08-19 sits at index `last - 2`, needs `last >= flag + 7`,
        and 08-22 through 08-26 is five bars. The claim that a number came
        from subtraction rather than forecast is worth nothing if the
        subtraction is not checked against the file. EDGE.md §57e.
        """
        bars, _funding, _folds = _corpus()
        flag = next(i for i in range(len(bars))
                    if _day(bars, i) == "2026-08-19")
        needed_index = flag + 7
        last = len(bars) - 1
        assert last < needed_index
        # AMENDED BY SLICE 75; on slice 74's list. The DESTINATION is
        # permanent, the DISTANCE is not — it shrinks by one per closed bar,
        # and each slice must recompute it rather than reuse a number. §58b.
        target = dt.datetime.strptime(
            _day(bars, last), "%Y-%m-%d").replace(tzinfo=dt.timezone.utc) + \
            dt.timedelta(days=needed_index - last)
        assert target.strftime("%Y-%m-%d") == "2026-08-26"
        # What slice 74 recorded, kept against its frozen artefact.
        assert load(FRESHNESS)["after_t1_dates"][-1] == "2026-08-21"

    def test_five_forward_bars_are_scoreable_now(self):
        block = load(FORWARD)["barrier_eligibility"]
        assert block["forward_bars_that_are_scoreable"] == WINDOW[:5]
        assert load(FORWARD)["ceiling"][
            "forward_bars_with_a_barrier_outcome"] == 5

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
# 4 — the threshold sits on the venue's base rate
# ===========================================================================


class TestTheThresholdSitsOnTheBaseRate:

    def test_the_census_is_measured_not_asserted(self):
        """AMENDED BY SLICE 75; on slice 74's list.

        The census was recomputed against the live corpus, which grows. The
        durable claims are the SHAPE — the base rate is modal, is not a cap,
        and the corpus only ever gains prints — and those are asserted live.
        The exact counts stay pinned to slice 74's frozen artefact.
        """
        census = load(FRESHNESS)["forward_funding_regime"]["threshold_census"]
        rows = cp.read_rows(cp.FUNDING_BTC)
        values = [float(r["funding_rate"]) for r in rows]
        assert census["prints"] == 4422
        assert census["exactly_at_fund_abs"] == 1174
        assert census["strictly_above_fund_abs"] == 282
        # Direction of travel, and the shape that does not expire.
        assert len(values) >= census["prints"]
        assert sum(1 for v in values if v == fb.FUND_ABS) >= \
            census["exactly_at_fund_abs"]
        assert sum(1 for v in values if v > fb.FUND_ABS) >= \
            census["strictly_above_fund_abs"]
        assert max(values) >= census["max_print"] > fb.FUND_ABS

    def test_it_is_not_a_cap(self):
        census = load(FRESHNESS)["forward_funding_regime"]["threshold_census"]
        assert census["fund_abs_is_a_cap"] is False
        assert census["strictly_above_fund_abs"] > 0
        assert census["max_print"] > fb.FUND_ABS

    def test_it_is_the_modal_value_by_a_wide_margin(self):
        census = load(FRESHNESS)["forward_funding_regime"]["threshold_census"]
        assert census["fund_abs_is_the_modal_value"] is True
        assert census["exactly_at_fund_abs"] > \
            100 * census["runner_up_value_count"]
        assert census["share_exactly_at_fund_abs"] > 0.2

    def test_the_pattern_predates_t1(self):
        """Not a forward-window phenomenon: the same share before and after."""
        rows = cp.read_rows(cp.FUNDING_BTC)
        pre = [float(r["funding_rate"]) for r in rows
               if r["funding_time"][:10] <= "2026-08-09"]
        assert sum(1 for v in pre if v == fb.FUND_ABS) / len(pre) > 0.2

    def test_the_forward_setups_are_ordinary_qualifying_prints(self):
        """Both are exactly at the base rate — not outliers."""
        bars, funding, folds = _corpus()
        window = list(fb.forward_window_indices(bars, folds))
        rates = fb.funding_at_decision(bars, funding)
        setups = fb.funding_setups(bars, funding, fund_abs=fb.FUND_ABS)
        for index in window:
            if index in setups:
                assert rates[index] == fb.FUND_ABS

    def test_this_corrects_language_and_moves_no_number(self):
        census = load(FRESHNESS)["forward_funding_regime"]["threshold_census"]
        finding = prose(census["finding"])
        assert "BASE-OR-ABOVE" in finding.upper()
        assert "moves no measured number" in finding
        assert "both sides" in finding
        assert census["this_is_not_a_reason_to_move_fund_abs"] is True

    def test_fund_abs_did_not_move(self):
        assert fb.FUND_ABS == 0.0001
        assert load(FORWARD)["fund_abs_moved_this_slice"] is False
        assert load(FORWARD)["join_changed_this_slice"] is False
        assert load(FRESHNESS)["forward_funding_regime"][
            "parameters_moved_because_of_this"] is False
        why = prose(load(FRESHNESS)["forward_funding_regime"]["why_not"])
        assert "dressed as an optimisation or as a correction" in why

    def test_the_correction_is_recorded_in_edge(self):
        with open(os.path.join(REPO, "EDGE.md"), encoding="utf-8") as handle:
            edge = handle.read()
        flat = prose(edge)
        assert "\n## 57c." in edge
        assert "base funding rate" in flat
        assert "does not invalidate the clear" in flat


# ===========================================================================
# 5 — the ceiling
# ===========================================================================


class TestTheCeiling:

    def test_the_ceiling_is_seven(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["closed_forward_bars"] == 12
        assert ceiling["max_possible_forward_closed_trades"] == 7
        assert ceiling["observed_forward_closed_trades"] == 0
        assert ceiling["within_ceiling"] is True
        assert "§57a" in ceiling["declared_in"]

    def test_it_is_not_six_and_not_eight(self):
        ceiling = load(FORWARD)["ceiling"]["max_possible_forward_closed_trades"]
        assert ceiling != 6, "6 was slice 73's ceiling, at N=11"
        assert ceiling < 8, "8 would need N=13"

    def test_the_unconditional_bound_is_commentary_only(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["unconditional_upper_bound"] == 11
        assert ceiling["max_possible_forward_closed_trades"] == 7
        assert ceiling["observed_forward_closed_trades"] <= \
            ceiling["forward_bars_with_a_barrier_outcome"]

    def test_every_prior_slice_ceiling_is_recorded_and_lower(self):
        observed = [load(f)["ceiling"]["max_possible_forward_closed_trades"]
                    for _n, (f, _c) in sorted(PRIOR.items())]
        assert observed == [expected for _n, (_f, expected)
                            in sorted(PRIOR.items())]
        assert observed == [0, 0, 0, 0, 0, 0, 1, 2, 3, 4, 5, 6]
        assert load(FORWARD)["ceiling"][
            "max_possible_forward_closed_trades"] == 7

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
# 6 — STEP 1 landed, and the guard that checks it
# ===========================================================================


class TestTheDesignNoteLandedThisTime:

    def test_edge_carries_57a_through_57d(self):
        with open(os.path.join(REPO, "EDGE.md"), encoding="utf-8") as handle:
            edge = handle.read()
        for letter in "abcd":
            assert f"\n## 57{letter}." in edge, letter

    def test_the_artefacts_recorded_57_as_present_when_they_ran(self):
        for path in (FRESHNESS, FORWARD):
            recorded = load(path)["edge_state_when_this_ran"]["sections_present"]
            assert 57 in recorded, path

    def test_the_ceilings_citation_resolves(self):
        declared = load(FORWARD)["ceiling"]["declared_in"]
        assert "§57a" in declared
        assert 57 in load(FORWARD)["edge_state_when_this_ran"][
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
                    if os.path.basename(p).startswith("slice74_")}
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
        path = os.path.join(REPO, "STAGE1_VERDICT_SLICE74.md")
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

        Guard four fired when a slice-73 test was renamed while slice 73's
        SHIPPED verdict cited it. A shipped verdict is a record; renaming a
        test it names would make the record false, exactly as slice 70
        refused to edit slice 67's. Amend the body, never the name.
        """
        from test_slice70_eight_day_window import (  # noqa: PLC0415
            TestAVerdictMayNotCiteATestThatDoesNotExist as Guard)
        with open(os.path.join(REPO, "STAGE1_VERDICT_SLICE73.md"),
                  encoding="utf-8") as handle:
            cited = set(Guard.NAME.findall(handle.read()))
        # `_test_names_in_the_tree` includes module stems as well as function
        # names, which is why a verdict may name a test MODULE too.
        assert sorted(cited - Guard()._test_names_in_the_tree()) == []


class TestTheScoringWasNotReTuned:

    SUBSTITUTIONS = {
        "forward_shadow_observation/12": "forward_shadow_observation/13",
        "slice73_forward_shadow.json": "slice74_forward_shadow.json",
        "slice73_forward_shadow.log": "slice74_forward_shadow.log",
        "window_grew_since_slice72": "window_grew_since_slice73",
        "slice72_forward_shadow.json": "slice73_forward_shadow.json",
        "SLICE 73 — FORWARD SHADOW SEGMENT":
            "SLICE 74 — FORWARD SHADOW SEGMENT",
        "EDGE.md §56a, before this tool ran":
            "EDGE.md §57a, before this tool ran",
    }

    DECLARED_CHANGES = {
        "ceiling": "advances 6 -> 7; the milestone note describes the second "
                   "setup and 08-19 still four days short of scoreable",
        "why_neither_moved": "adds §57c — the threshold sits on the venue's "
                             "base rate, which is a correction to language "
                             "and not a licence to re-derive it",
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

    def test_the_scoring_was_not_re_tuned_since_slice73(self):
        old_body, old_payload = self._split(
            "slice73_forward_shadow.py", self.SUBSTITUTIONS)
        new_body, new_payload = self._split(
            "slice74_forward_shadow.py", self.SUBSTITUTIONS)
        assert old_body == new_body, "the SCORING changed between slices"
        assert set(old_payload) - set(new_payload) == set(), "a key was DROPPED"
        drifted = {k for k, v in old_payload.items()
                   if new_payload.get(k) != v}
        assert drifted == set(self.DECLARED_CHANGES), sorted(drifted)
        added = set(new_payload) - set(old_payload)
        assert added == set(self.DECLARED_ADDITIONS), sorted(added)

    def test_the_guard_is_where_the_next_slice_will_look_for_it(self):
        with open(os.path.join(REPO, "tests",
                               "test_slice74_twelve_day_window.py"),
                  encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)}
        assert "test_the_scoring_was_not_re_tuned_since_slice73" in names


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
        "test_twelve_closed_bars_lie_after_t1",
        "test_history_is_untouched_and_growth_is_an_append",
        "test_the_open_bar_is_absent",
        "test_only_btc_grew",
        "test_the_counts_came_from_this_slices_files",
        "test_08_21_is_reproduced_from_the_corpus_not_the_artefact",
        "test_the_qualifying_print_stands_at_the_close",
        "test_08_21_is_not_a_flag_because_it_is_the_last_bar",
        "test_08_20_is_still_not_a_setup",
        "test_that_offset_is_measured_from_the_frozen_function",
        "test_08_19_has_no_barrier_outcome",
        "test_it_needs_the_corpus_to_reach_08_26",
        "test_the_census_is_measured_not_asserted",
        "test_the_pattern_predates_t1",
        "test_the_forward_setups_are_ordinary_qualifying_prints",
    }

    LIVE_READS = ("check", "check_all", "read_rows", "_corpus",
                  "load_corpus", "load_funding", "barrier_r_for_all_bars",
                  "funding_at_decision", "funding_setups",
                  "forward_window_indices", "close_time_ms",
                  "flags_and_directions", "directed_signal_bars", "summary")

    MODULE = "test_slice74_twelve_day_window.py"

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

    def test_slice73s_list_named_the_three_corpus_failures(self):
        from test_slice73_eleven_day_window import (  # noqa: PLC0415
            TestExpiringAssertionsAreDeclared as Prior)
        for name in ("test_the_counts_came_from_this_slices_files",
                     "test_the_flag_appeared_because_08_19_stopped_being_"
                     "the_last_bar",
                     "test_that_is_measured_from_the_frozen_function_not_"
                     "asserted"):
            assert name in Prior.EXPIRES_WITH_DATA, name

    def test_the_two_it_missed_read_a_document_not_the_corpus(self):
        """Why the sweep missed them, asserted rather than excused."""
        from test_slice73_eleven_day_window import (  # noqa: PLC0415
            TestExpiringAssertionsAreDeclared as Prior)
        for name in ("test_the_recorded_digest_matches_the_edge_that_ships_"
                     "beside_it",
                     "test_from_git_when_a_repository_is_present"):
            assert name not in Prior.EXPIRES_WITH_DATA, name
        assert "EDGE.md" not in " ".join(Prior.LIVE_READS)

    def test_the_verdict_hands_the_list_forward(self):
        path = os.path.join(REPO, "STAGE1_VERDICT_SLICE74.md")
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
            REPO, "artifacts", "slice74_restored_from_slice73.json"))
        assert load(FRESHNESS)["pack_regression"][
            "occurred_this_slice"] is False

    def test_the_pack_base_claim_is_true_this_time(self):
        claim = load(FRESHNESS)["pack_base_claim"]
        assert claim["true"] is True
        assert claim["was_false_when_first_made_in_slice67"] is True

    def test_edge_carries_every_section_from_45_to_57(self):
        with open(os.path.join(REPO, "EDGE.md"), encoding="utf-8") as handle:
            edge = handle.read()
        for section in range(45, 58):
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
            if not name.startswith("slice74_") or not name.endswith(".json"):
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
        assert load(GATE)["items_complete_unchanged_from_slice_73"] is True

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
