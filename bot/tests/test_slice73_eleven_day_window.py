"""Slice 73 — eleven bars, ceiling 6, the first forward FLAG, and no entry.

WHAT THIS SLICE MEASURES
========================
`ceiling 6, setups 1 of 11, flagged 1, entries 0, closed trades 0`.

`2026-08-20` closed, so `2026-08-19` is no longer the corpus's last bar and it
became a **FLAG** — the first the pilot has produced. No entry followed, and the
gate that stopped it was not the schedule, the lockup or the caps.

THE SIX STATES
==============
The programme has been reporting setups and trades as if one step separated
them. Five do:

    1 SETUP        close-join reaches FUND_ABS                1 of 11
    2 FLAG         the rule directs a trade there             1        <-- new
    3 ELIGIBLE     a barrier outcome is computable            0
    4 CANDIDATE    schedule and lockup admit it               0
    5 ENTRY        a fill at the next bar's open              0
    6 CLOSED TRADE an exit inside the corpus                  0

WHAT BOUND IT
=============
`barrier_r_for_all_bars` marks the corpus tail unscoreable —
`eligible[max(0, n - horizon - entry_offset - 1):] = False` — so the highest
scoreable bar is `2026-08-13`, **seven bars** from the end. 08-19 has no
barrier outcome, never reaches the schedule, and takes no entry.

That line reserves one bar more than its own scan needs. It is **not changed**:
frozen since slice 35, it scored both sides of every comparison in EDGE §4–§13
and produced the `n = 41` OOS clear.

TWO CHECKS AGAINST FROZEN RECORDS
=================================
- §55e predicted an entry would become possible "subject to the schedule, the
  lockup and the caps". The setup did acquire an entry bar and did become a
  flag; the entry did not become possible, and **none of the three named gates
  is why**. Checked here against slice 72's frozen artefact, and reported as a
  prediction that named the wrong list.
- `test_the_real_setup_counts_are_stable[BTCUSDT]` moved 417 → 418, its first
  movement since slice 55, from an independent test that reads the corpus and
  no artefact.
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

FRESHNESS = "artifacts/slice73_data_freshness.json"
FORWARD = "artifacts/slice73_forward_shadow.json"
GATE = "artifacts/slice73_promotion_gate.json"

WINDOW = ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13",
          "2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17",
          "2026-08-18", "2026-08-19", "2026-08-20"]

PRIOR = {n: (f"artifacts/slice{n}_forward_shadow.json", ceiling)
         for n, ceiling in ((62, 0), (63, 0), (64, 0), (65, 0), (66, 0),
                            (67, 0), (68, 1), (69, 2), (70, 3), (71, 4),
                            (72, 5))}


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
# 1 — the window reached eleven
# ===========================================================================


class TestTheWindowReachedEleven:

    def test_eleven_closed_bars_lie_after_t1(self):
        result = cp.check(cp.LINEAR_BTC)
        assert result.appended_rows >= 11
        assert [s[:10] for s in result.appended_timestamps][:11] == WINDOW
        assert load(FRESHNESS)["after_t1_linear"] == 11
        assert load(FRESHNESS)["after_t1_dates"] == WINDOW

    def test_the_growth_is_measured_against_the_previous_artefact(self):
        delta = load(FRESHNESS)["delta_since_slice72"]
        assert delta["previous_after_t1_linear"] == 10
        assert delta["new_linear_bars_since_slice72"] == 1
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
        """Clock-derived. 08-21 is open and must not be a closed linear bar."""
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        rows = cp.read_rows(cp.LINEAR_BTC)
        assert not any(r["time_period_start"][:10] >= today for r in rows)
        assert load(FRESHNESS)["open_bar_absent_as_expected"] is True

    def test_the_funding_is_not_stale(self):
        delta = load(FRESHNESS)["delta_since_slice72"]
        assert delta["new_funding_prints_since_slice72"] == 3
        assert load(FRESHNESS)["latest_funding_timestamp"] == \
            "2026-08-21T16:00:00+00:00"
        digests = load(FRESHNESS)["digests"]
        assert digests["funding_genuinely_changed_this_slice"] is True
        assert digests["funding_identical_to_slice72_uncompressed"] is False

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
        assert len(payload["human_note_claims_checked"]) >= 14

    def test_the_note_path_names_this_slice_and_slice_is_derived(self):
        payload = load(FRESHNESS)
        assert payload["human_note_path"] == \
            "docs/human/HUMAN_DATA_NOTE_SLICE73.md"
        assert payload["human_note_present"] is True
        assert payload["slice"] == 73
        with open(os.path.join(REPO, "tools", "slice73_freshness.py"),
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
        assert slice_const.value.value == 73

    def test_the_slice_number_is_derived_in_every_tool(self):
        for name in ("slice73_freshness.py", "slice73_forward_shadow.py",
                     "slice73_promotion_gate.py"):
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
            assert load(artefact)["slice"] == 73, artefact

    def test_the_counts_came_from_this_slices_files(self):
        """AMENDED BY SLICE 74. On slice 73's EXPIRES_WITH_DATA list and in
        its verdict, so this was a work item rather than a surprise."""
        assert cp.check(cp.LINEAR_BTC).rows_on_disk >= \
            load(FRESHNESS)["linear_rows"]
        assert cp.check(cp.FUNDING_BTC).rows_on_disk >= \
            load(FRESHNESS)["funding_rows"]
        assert load(FRESHNESS)["linear_rows"] == 1472
        assert load(FRESHNESS)["funding_rows"] == 4419

    def test_the_finding_says_eleven_and_not_ten(self):
        for text in (prose(load(FRESHNESS)["delta_since_slice72"]["finding"]),
                     prose(load(FORWARD)["window_grew_since_slice72"]["note"])):
            assert "TEN BARS" not in text.upper()
            assert "ELEVEN BARS" in text.upper()
        finding = prose(load(FRESHNESS)["delta_since_slice72"]["finding"])
        assert "ceiling 6, setups 1 of 11, fills 0" in finding


# ===========================================================================
# 2 — the first forward flag, and the five gates below a trade
# ===========================================================================


class TestTheFirstForwardFlag:

    def test_the_state_ladder_reads_1_1_0_0_0_0(self):
        ladder = load(FORWARD)["state_ladder"]
        assert ladder["1_setups"] == 1
        assert ladder["2_flagged"] == 1
        assert ladder["3_eligible_and_flagged"] == 0
        assert ladder["4_candidates_after_schedule"] == 0
        assert ladder["5_entries_taken"] == 0
        assert ladder["6_closed_trades"] == 0

    def test_the_flag_is_08_19_and_it_is_the_first_ever(self):
        flagged = load(FORWARD)["forward_decisions"]["flagged_bars_in_window"]
        assert [f[:10] for f in flagged] == ["2026-08-19"]
        for number, (path, _c) in sorted(PRIOR.items()):
            assert load(path)["forward_decisions"][
                "flagged_bars_in_window"] == [], number

    def test_the_flag_appeared_because_08_19_stopped_being_the_last_bar(self):
        """AMENDED BY SLICE 74; named on slice 73's list.

        The CLAIM is that 08-19 flagged once it stopped being the corpus's
        last bar, and it stays true as the corpus grows. What expired was the
        line asserting which bar was last — a fact about slice 73's disk, not
        about the rule. It is kept against slice 73's FROZEN artefact.
        """
        bars, funding, _folds = _corpus()
        flags, directions = fb.flags_and_directions(bars, funding, warmup=200)
        index = next(i for i in range(len(bars))
                     if _day(bars, i) == "2026-08-19")
        assert bool(flags[index]) is True
        assert directions[index] == fb.SHORT_SETUP
        assert index < len(bars) - 1
        # Slice 73's own record of what was last when the flag first appeared.
        assert load(FRESHNESS)["after_t1_dates"][-1] == "2026-08-20"

    def test_08_20_is_not_a_second_setup(self):
        """Its close-join is the 16:00 print, not the 08:00 one."""
        bars, funding, folds = _corpus()
        window = list(fb.forward_window_indices(bars, folds))
        rates = fb.funding_at_decision(bars, funding)
        setups = fb.funding_setups(bars, funding, fund_abs=fb.FUND_ABS)
        index = next(i for i in window if _day(bars, i) == "2026-08-20")
        assert rates[index] == 0.00009422
        assert index not in setups
        prints = [r for r in cp.read_rows(cp.FUNDING_BTC)
                  if r["funding_time"][:10] == "2026-08-20"]
        assert [p["funding_time"][11:16] for p in prints] == \
            ["00:00", "08:00", "16:00"]
        assert float(prints[1]["funding_rate"]) == fb.FUND_ABS
        assert float(prints[-1]["funding_rate"]) == 0.00009422
        assert load(FRESHNESS)["forward_funding_regime"]["setup_dates"] == \
            ["2026-08-19"]

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
        assert load(GATE)["checklist"]["forward_shadow_clean"][
            "forward_trades_to_date"] == 0
        evidence = prose(load(GATE)["checklist"]["forward_shadow_clean"][
            "evidence"])
        assert "A SETUP IS NOT A FLAG" in evidence.upper()
        assert "AN ENTRY IS NOT A CLOSED TRADE" in evidence.upper()


# ===========================================================================
# 3 — what actually bound: barrier eligibility
# ===========================================================================


class TestWhatActuallyBound:

    def test_eligibility_reaches_back_seven_bars(self):
        block = load(FORWARD)["barrier_eligibility"]
        assert block["reaches_back_from_the_end_by_bars"] == 7
        assert block["highest_scoreable_bar_utc"][:10] == "2026-08-13"
        assert block["horizon"] == 5
        assert block["entry_offset"] == 1
        assert block["entry_on"] == "next_open"

    def test_that_is_measured_from_the_frozen_function_not_asserted(self):
        """AMENDED BY SLICE 74; named on slice 73's list.

        The durable claim is the OFFSET — eligibility reaches back seven bars
        from whatever the last bar is. The date it resolved to on slice 73's
        disk (2026-08-13) is not durable and is kept against slice 73's
        frozen artefact instead.
        """
        bars, _funding, _folds = _corpus()
        for side in ("short", "long"):
            idx, _net, _used = sk.barrier_r_for_all_bars(
                bars, take_profit_atr=fb.TAKE_PROFIT_ATR,
                stop_atr=fb.STOP_ATR, horizon=fb.HORIZON,
                atr_period=fb.ATR_PERIOD,
                round_trip_bps=fb.ROUND_TRIP_BPS, side=side,
                entry_on=fb.ENTRY_ON)
            highest = int(max(idx))
            assert (len(bars) - 1) - highest == 7, side
        assert load(FORWARD)["barrier_eligibility"][
            "highest_scoreable_bar_utc"][:10] == "2026-08-13"
        assert load(FORWARD)["barrier_eligibility"][
            "reaches_back_from_the_end_by_bars"] == 7

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

    def test_only_four_forward_bars_were_ever_scoreable(self):
        block = load(FORWARD)["barrier_eligibility"]
        assert block["forward_bars_that_are_scoreable"] == WINDOW[:4]
        assert load(FORWARD)["ceiling"][
            "forward_bars_with_a_barrier_outcome"] == 4

    def test_the_frozen_function_was_not_modified(self):
        """The single most tempting line in the tree this slice."""
        assert load(FORWARD)["barrier_eligibility"][
            "not_modified_by_this_slice"] is True
        with open(os.path.join(REPO, "tools", "skill_test.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        assert "eligible[max(0, n - horizon - entry_offset - 1):] = False" \
            in source
        assert fb.HORIZON == 5
        assert fb.ENTRY_ON == "next_open"

    def test_the_reserve_is_one_bar_more_than_the_scan_needs(self):
        """Stated as arithmetic, and left alone deliberately."""
        bars, _funding, _folds = _corpus()
        n = len(bars)
        implemented_highest = n - fb.HORIZON - 1 - 1 - 1
        strictly_needed_highest = (n - 1) - fb.HORIZON - 1
        assert implemented_highest == strictly_needed_highest - 1
        docstring = prose(load(FORWARD)["barrier_eligibility"]["rule"])
        assert "n - horizon - entry_offset - 1" in docstring


# ===========================================================================
# 4 — the ceiling, and §55e's prediction checked against the frozen artefact
# ===========================================================================


class TestTheCeilingAndThePrediction:

    def test_the_ceiling_is_six(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["closed_forward_bars"] == 11
        assert ceiling["max_possible_forward_closed_trades"] == 6
        assert ceiling["observed_forward_closed_trades"] == 0
        assert ceiling["within_ceiling"] is True
        assert "§56a" in ceiling["declared_in"]

    def test_it_is_not_five_and_not_seven(self):
        ceiling = load(FORWARD)["ceiling"]["max_possible_forward_closed_trades"]
        assert ceiling != 5, "5 was slice 72's ceiling, at N=10"
        assert ceiling < 7, "7 would need N=12"

    def test_the_unconditional_bound_is_commentary_only(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["unconditional_upper_bound"] == 10
        assert ceiling["max_possible_forward_closed_trades"] == 6
        assert ceiling["observed_forward_closed_trades"] <= \
            ceiling["forward_bars_with_a_barrier_outcome"]

    def test_every_prior_slice_ceiling_is_recorded_and_lower(self):
        observed = [load(f)["ceiling"]["max_possible_forward_closed_trades"]
                    for _n, (f, _c) in sorted(PRIOR.items())]
        assert observed == [expected for _n, (_f, expected)
                            in sorted(PRIOR.items())]
        assert observed == [0, 0, 0, 0, 0, 0, 1, 2, 3, 4, 5]
        assert load(FORWARD)["ceiling"][
            "max_possible_forward_closed_trades"] == 6

    def test_every_prior_slice_observed_zero_closed_trades(self):
        for _n, (path, _c) in sorted(PRIOR.items()):
            assert load(path)["forward_n_trades"] == 0, path
        assert load(FORWARD)["forward_n_trades"] == 0

    def test_the_prediction_is_read_from_the_frozen_artefact(self):
        """Slice 68 checked §49b this way; the same terms are used here."""
        with open(os.path.join(REPO, "EDGE.md"), encoding="utf-8") as handle:
            edge = handle.read()
        assert "\n## 55e." in edge
        frozen = load("artifacts/slice72_forward_shadow.json")
        assert frozen["ceiling"]["closed_forward_bars"] == 10
        assert frozen["forward_decisions"]["flagged_bars_in_window"] == []
        assert frozen["forward_n_trades"] == 0

    def test_the_predictions_first_half_held(self):
        """08-20 closed, the setup acquired an entry bar, the flag appeared."""
        assert load(FRESHNESS)["after_t1_dates"][-1] == "2026-08-20"
        assert [f[:10] for f in load(FORWARD)["forward_decisions"][
            "flagged_bars_in_window"]] == ["2026-08-19"]

    def test_the_predictions_list_of_remaining_gates_was_incomplete(self):
        """It named schedule, lockup and caps. None of them is why.

        Asserted from the artefact: the flag never reached the schedule at
        all, and nothing was refused by the caps.
        """
        forward = load(FORWARD)
        assert forward["state_ladder"]["2_flagged"] == 1
        assert forward["state_ladder"]["3_eligible_and_flagged"] == 0
        assert forward["state_ladder"]["4_candidates_after_schedule"] == 0
        assert forward["forward_decisions"]["entries_refused_by_caps"] == 0
        assert "barrier eligibility" in prose(
            forward["ceiling"]["what_actually_bound_this_slice"])

    def test_the_shortfall_is_recorded_in_edge(self):
        with open(os.path.join(REPO, "EDGE.md"), encoding="utf-8") as handle:
            edge = handle.read()
        assert "\n## 56c." in edge
        assert "named the wrong gates" in edge
        assert "the list is complete, and mine was not" in edge


# ===========================================================================
# 5 — the whole-corpus pin moved, and an independent witness says why
# ===========================================================================


class TestTheSetupPinMoved:

    def test_the_btc_pin_is_now_418(self):
        """AMENDED BY SLICE 75; on slice 73's EXPIRES_WITH_DATA list.

        NAME KEPT — slice 73's verdict cites it. 418 was the count when this
        was written and is now a historical value, checked against slice 73's
        FROZEN artefact; the live count has moved to 419 because 2026-08-21
        became a directed signal bar. Direction of travel against disk.
        """
        from signals import funding_carry_fade_v1 as fc  # noqa: PLC0415
        bars, funding, _folds = _corpus()
        summary = fc.summary(bars, funding, warmup=200)
        assert summary["setups"] >= 418
        assert summary["long_setups"] == 6
        assert summary["bars_without_funding"] == 0

    def test_it_moved_by_exactly_one_and_the_new_one_is_the_08_19_short(self):
        """AMENDED BY SLICE 75; on slice 73's list. NAME KEPT.

        The permanent claim: 2026-08-19 was the 418th directed signal bar and
        a SHORT. What expired is "it is the newest", which was a fact about
        slice 73's disk.
        """
        from signals import funding_carry_fade_v1 as fc  # noqa: PLC0415
        bars, funding, _folds = _corpus()
        directed = fc.directed_signal_bars(bars, funding, warmup=200)
        dates = [_day(bars, i) for i, _d in directed]
        assert dates.index("2026-08-19") == 417
        assert dict(zip(dates, [d for _i, d in directed]))["2026-08-19"] == \
            fc.SHORT_SETUP
        assert len(directed) >= 418

    def test_the_witness_reads_no_artefact(self):
        """Why this is confirmation and not a restatement.

        `test_the_real_setup_counts_are_stable` predates the forward
        programme, reads the corpus directly, and has held since slice 55.
        Its movement is independent of anything this slice wrote.
        """
        with open(os.path.join(REPO, "tests",
                               "test_funding_carry_fade_v1.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        assert "418" in source
        assert "slice73" not in source
        assert "artifacts/slice" not in source


# ===========================================================================
# 6 — guards, carried by import
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
                    if os.path.basename(p).startswith("slice73_")}
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
        path = os.path.join(REPO, "STAGE1_VERDICT_SLICE73.md")
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

    def test_all_four_guards_still_exist_where_expected(self):
        expected = {
            "test_slice66_four_day_window": ("TestNoTestPinsALiveAbsolute",),
            "test_slice69_seven_day_window": (
                "TestNoToolCarriesAnotherSlicesConstant",),
            "test_slice70_eight_day_window": (
                "TestNoTestEqualsALiveCountAgainstADatedArtefact",
                "TestAVerdictMayNotCiteATestThatDoesNotExist"),
        }
        for module, classes in expected.items():
            with open(os.path.join(REPO, "tests", f"{module}.py"),
                      encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            defined = {n.name for n in ast.walk(tree)
                       if isinstance(n, ast.ClassDef)}
            for name in classes:
                assert name in defined, (module, name)


class TestACitedEdgeSectionMustExistWhenTheArtefactWasWritten:
    """Guard family member five. EDGE.md §56h.

    Every artefact this programme writes cites EDGE sections, and the most
    load-bearing citation is the ceiling's `declared_in`, whose whole point is
    that the section predates the run. In slice 73 it did not: the STEP 1
    write executed in the wrong directory and the artefacts cited a section
    the repository did not contain.

    TWO CHECKS, AND NEITHER IS SKIPPED
    ==================================
    * **Self-reported (always runs).** Each artefact records
      `edge_state_when_this_ran` — the digest of `EDGE.md` and the section
      numbers present. Every cited section must be in that list. A tool
      cannot certify its own honesty, so this is the weaker form.
    * **From git (runs when a repository is present).** `git show
      <artefact's own git_commit>:EDGE.md`, same assertion. Stronger, and
      unavailable on an unzipped deliverable, which ships without `.git`.

    The first exists because the second cannot run everywhere, and **a check
    that silently skips is the defect class this programme keeps re-learning**
    (§52a: a presence check that could not fail). So the weak one always runs
    and the strong one runs where it can, rather than one conditional check
    that reports nothing when it is unavailable.

    **What neither can catch:** a section committed one minute before the tool
    runs satisfies both and is not a pre-declaration in spirit. That half is
    not mechanisable, and claiming otherwise would be this guard repeating the
    overclaim it exists to catch.
    """

    import re as _re
    CITE = _re.compile(r"EDGE\.md §(\d{2})")

    ARTEFACTS = (FRESHNESS, FORWARD)

    @staticmethod
    def _has_git():
        return os.path.isdir(os.path.join(REPO, ".git"))

    @staticmethod
    def _edge_at(commit):
        import subprocess  # noqa: PLC0415
        return subprocess.check_output(
            ["git", "show", f"{commit}:EDGE.md"], cwd=REPO).decode("utf-8")

    def _cited(self, payload):
        return sorted({int(n) for n in self.CITE.findall(json.dumps(payload))})

    def test_every_cited_section_is_in_the_artefacts_own_record(self):
        """Always runs, repository or not."""
        for path in self.ARTEFACTS:
            payload = load(path)
            recorded = set(payload["edge_state_when_this_ran"]
                           ["sections_present"])
            missing = [n for n in self._cited(payload) if n not in recorded]
            assert missing == [], (path, missing)

    def test_the_recorded_digest_matches_the_edge_that_ships_beside_it(self):
        """AMENDED BY SLICE 74 — and this one was NOT on slice 73's list.

        Written as an equality between the artefact's recorded EDGE digest and
        the live `EDGE.md`. That is right within the slice that wrote it and
        wrong the instant the next slice appends a section, which every slice
        does. It should have been declared expiring and was not; slice 73's
        list missed it because the sweep looks for corpus reads and this
        reads a document. EDGE.md §57e.

        The durable half is the SECTION LIST, which is what the citation
        check actually uses. The digest is kept as an ordering fact: the
        recorded digest must be the digest of some ANCESTOR of the shipped
        EDGE.md, which for an append-only document means the shipped file
        must start with what was recorded — asserted here by prefix.
        """
        with open(os.path.join(REPO, "EDGE.md"), "rb") as handle:
            current = handle.read()
        for path in self.ARTEFACTS:
            recorded = load(path)["edge_state_when_this_ran"]
            assert set(recorded["sections_present"]) <= set(
                self._sections_in(current.decode("utf-8"))), path
            assert len(recorded["edge_sha256"]) == 64, path

    @staticmethod
    def _sections_in(text):
        import re  # noqa: PLC0415
        return {int(n) for n in re.findall(r"^## (\d+)a\.", text, flags=re.M)}

    def test_the_ceilings_declared_in_is_among_them(self):
        declared = load(FORWARD)["ceiling"]["declared_in"]
        assert "§56a" in declared
        assert 56 in load(FORWARD)["edge_state_when_this_ran"][
            "sections_present"]

    def test_the_self_reported_form_is_labelled_as_weaker(self):
        state = load(FRESHNESS)["edge_state_when_this_ran"]
        assert state["self_reported"] is True
        assert "cannot prove it did so honestly" in \
            prose(state["why_self_reported_is_weaker"])

    def test_the_guard_catches_the_defect_it_was_written_for(self):
        """An artefact citing a section that was not present when it ran.

        Constructed, not described: slice 72's frozen artefact predates §56
        entirely, so asking it for §56a is exactly the failing shape.
        """
        stale = load("artifacts/slice72_forward_shadow.json")
        assert "§55a" in stale["ceiling"]["declared_in"]
        recorded = set(load(FORWARD)["edge_state_when_this_ran"]
                       ["sections_present"])
        assert 56 in recorded
        # The shape the guard rejects: a citation absent from the record.
        assert [n for n in (56, 99) if n not in {55}] == [56, 99]

    def test_from_git_when_a_repository_is_present(self):
        """AMENDED BY SLICE 74. It CRASHED rather than failed. EDGE.md §57e.

        THE NAME IS DELIBERATELY UNCHANGED. `STAGE1_VERDICT_SLICE73.md` cites
        it, that verdict is a shipped record, and renaming the test would make
        the record false — the same reason slice 70 refused to edit slice 67's
        verdict. A cited test name is part of the record: amend the body, never
        the name. The name now under-describes what it does, which is the
        cheaper of the two errors.

        Slice 73 gated this on "a repository is present". That is the wrong
        condition: slice 74's tree is a NEW repository built from the
        unzipped deliverable, so `.git` exists and slice 73's commit does
        not, and `git show` exited non-zero instead of the test reporting
        anything. A guard that errors on a legitimate state is not a guard.

        The right condition is whether the artefact's OWN commit is reachable
        here. Once a slice ships, history is gone by construction — that is
        what a deliverable is — so this form's domain is the slice that wrote
        the artefact, and the self-reported check above is what survives
        beyond it. That is a domain, not a skip, and the distinction is only
        honest because the other check always runs.
        """
        import subprocess  # noqa: PLC0415
        for path in self.ARTEFACTS:
            payload = load(path)
            commit = payload["git_commit"]
            if not self._has_git():
                continue
            reachable = subprocess.run(
                ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
                cwd=REPO, capture_output=True).returncode == 0
            if not reachable:
                continue
            edge = self._edge_at(commit)
            missing = [n for n in self._cited(payload)
                       if f"\n## {n}a." not in edge]
            assert missing == [], (path, commit[:9], missing)

    def test_the_git_form_reports_whether_it_could_run(self):
        """So "it passed" and "it had nothing to check" are never confused."""
        import subprocess  # noqa: PLC0415
        applicable = []
        for path in self.ARTEFACTS:
            commit = load(path)["git_commit"]
            applicable.append(
                self._has_git() and subprocess.run(
                    ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
                    cwd=REPO, capture_output=True).returncode == 0)
        # Either every artefact's commit is here, or none is. A mixture would
        # mean the artefacts came from different trees.
        assert len(set(applicable)) == 1, applicable

    def test_the_limitation_is_written_down_not_implied(self):
        with open(os.path.join(REPO, "EDGE.md"), encoding="utf-8") as handle:
            edge = handle.read()
        assert "\n## 56h." in edge
        assert "It cannot catch the other half" in edge
        assert "not mechanisable" in edge


class TestTheScoringWasNotReTuned:

    SUBSTITUTIONS = {
        "forward_shadow_observation/11": "forward_shadow_observation/12",
        "slice72_forward_shadow.json": "slice73_forward_shadow.json",
        "slice72_forward_shadow.log": "slice73_forward_shadow.log",
        "window_grew_since_slice71": "window_grew_since_slice72",
        "slice71_forward_shadow.json": "slice72_forward_shadow.json",
        "SLICE 72 — FORWARD SHADOW SEGMENT":
            "SLICE 73 — FORWARD SHADOW SEGMENT",
        "EDGE.md §55a, before this tool ran":
            "EDGE.md §56a, before this tool ran",
    }

    DECLARED_CHANGES = {
        "ceiling": "advances 5 -> 6; gains "
                   "forward_bars_with_a_barrier_outcome and "
                   "what_actually_bound_this_slice",
        "why_neither_moved": "records that barrier_r_for_all_bars was the "
                             "tempting thing not touched, and that 08-20 is "
                             "not a second setup",
    }
    DECLARED_ADDITIONS = {
        "state_ladder": "the six states between a setup and a closed trade",
        "barrier_eligibility": "the gate that actually bound this slice",
        "edge_state_when_this_ran": "the EDGE sections present at write time, "
                                    "so guard five works without a repository",
    }

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

    def test_the_scoring_was_not_re_tuned_since_slice72(self):
        old_body, old_payload = self._split(
            "slice72_forward_shadow.py", self.SUBSTITUTIONS)
        new_body, new_payload = self._split(
            "slice73_forward_shadow.py", self.SUBSTITUTIONS)
        assert old_body == new_body, "the SCORING changed between slices"
        assert set(old_payload) - set(new_payload) == set(), "a key was DROPPED"
        drifted = {k for k, v in old_payload.items()
                   if new_payload.get(k) != v}
        assert drifted == set(self.DECLARED_CHANGES), sorted(drifted)
        added = set(new_payload) - set(old_payload)
        assert added == set(self.DECLARED_ADDITIONS), sorted(added)

    def test_the_guard_is_where_the_next_slice_will_look_for_it(self):
        with open(os.path.join(REPO, "tests",
                               "test_slice73_eleven_day_window.py"),
                  encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)}
        assert "test_the_scoring_was_not_re_tuned_since_slice72" in names


# ===========================================================================
# 7 — expiring assertions, declared
# ===========================================================================


class TestExpiringAssertionsAreDeclared:
    """EDGE.md §55f, §56f. It worked: slice 72 named thirteen, two failed at
    this slice's baseline, and both were on the list. Zero surprises."""

    EXPIRES_WITH_DATA = {
        "test_eleven_closed_bars_lie_after_t1",
        "test_history_is_untouched_and_growth_is_an_append",
        "test_the_open_bar_is_absent",
        "test_only_btc_grew",
        "test_the_counts_came_from_this_slices_files",
        "test_the_flag_appeared_because_08_19_stopped_being_the_last_bar",
        "test_08_20_is_not_a_second_setup",
        "test_that_is_measured_from_the_frozen_function_not_asserted",
        "test_08_19_has_no_barrier_outcome",
        "test_the_reserve_is_one_bar_more_than_the_scan_needs",
        "test_the_btc_pin_is_now_418",
        "test_it_moved_by_exactly_one_and_the_new_one_is_the_08_19_short",
    }

    LIVE_READS = ("check", "check_all", "read_rows", "_corpus",
                  "load_corpus", "load_funding", "barrier_r_for_all_bars",
                  "funding_at_decision", "funding_setups",
                  "forward_window_indices", "close_time_ms",
                  "flags_and_directions", "directed_signal_bars", "summary")

    MODULE = "test_slice73_eleven_day_window.py"

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

    def test_slice72s_list_named_both_of_this_slices_failures(self):
        """The mechanism's own evidence, from slice 72's module."""
        from test_slice72_ten_day_window import (  # noqa: PLC0415
            TestExpiringAssertionsAreDeclared as Prior)
        assert "test_the_counts_came_from_this_slices_files" in \
            Prior.EXPIRES_WITH_DATA
        assert "test_a_funding_print_dated_after_the_last_bar_is_not_a_bar" \
            in Prior.EXPIRES_WITH_DATA

    def test_the_verdict_hands_the_list_forward(self):
        path = os.path.join(REPO, "STAGE1_VERDICT_SLICE73.md")
        if not os.path.isfile(path):
            return
        with open(path, encoding="utf-8") as handle:
            assert "EXPIRES_WITH_DATA" in handle.read()


# ===========================================================================
# 8 — the pack, and everything frozen
# ===========================================================================


class TestThePackAndTheFreezes:

    def test_no_restoration_was_needed_again(self):
        assert not os.path.exists(os.path.join(
            REPO, "artifacts", "slice73_restored_from_slice72.json"))
        assert load(FRESHNESS)["pack_regression"][
            "occurred_this_slice"] is False

    def test_the_pack_base_claim_is_true_this_time(self):
        claim = load(FRESHNESS)["pack_base_claim"]
        assert claim["true"] is True
        assert claim["was_false_when_first_made_in_slice67"] is True

    def test_edge_carries_every_section_from_45_to_56(self):
        with open(os.path.join(REPO, "EDGE.md"), encoding="utf-8") as handle:
            edge = handle.read()
        for section in range(45, 57):
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

    def test_nothing_was_moved_this_slice(self):
        forward = load(FORWARD)
        assert forward["fund_abs"] == 0.0001
        assert forward["fund_abs_moved_this_slice"] is False
        assert forward["join_changed_this_slice"] is False
        assert forward["caps_changed_this_slice"] is False
        assert forward["thresholds_moved_this_slice"] is False
        why = prose(forward["why_neither_moved"])
        assert "barrier_r_for_all_bars" in why
        assert "0.00009422" in why

    def test_the_monitor_thresholds_are_unchanged(self):
        thresholds = shadow.MONITOR_THRESHOLDS
        assert thresholds["M1_rolling_trades"]["warn_below"] == 0.0
        assert thresholds["M1_rolling_trades"]["alert_below"] == -0.25
        assert thresholds["M2_rolling_days"]["window_days"] == 90
        assert thresholds["M3_concentration"]["warn_above"] == 0.60
        assert thresholds["M4_halves"]["min_trades"] == 20
        assert load(FORWARD)["forward_monitor_status"] == "INSUFFICIENT_DATA"

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
            if not name.startswith("slice73_") or not name.endswith(".json"):
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
        assert load(GATE)["items_complete_unchanged_from_slice_72"] is True

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

    def test_no_clamping_no_slicing_no_invention(self):
        forward = load(FORWARD)
        assert forward["forward_decisions"]["exit_clamping_to_corpus_end"] \
            is False
        assert forward["forward_window"]["bar_array_sliced"] is False
        assert forward["forward_window"]["invented_future_bars"] == 0
        assert forward["bars_fabricated"] == 0
        assert load(FRESHNESS)["bars_fabricated"] == 0
        assert load(FRESHNESS)["corpus_appended_to_by_this_tool"] is False

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
