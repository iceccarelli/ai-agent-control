"""Slice 72 — ten bars, ceiling 5, and the first forward setup in the pilot.

WHAT THIS SLICE MEASURES
========================
`ceiling 5, setups 1 of 10, fills 0`.

`2026-08-19` close-joined at exactly `FUND_ABS` and **stood at the bar's
close** — the first forward decision ever to qualify. It is not the same event
as `2026-08-12`, whose equal print was superseded before its bar closed; §51c
has kept those two claims apart for four slices and this is the first time the
second one is true.

**It is not a trade and cannot become one on this tree.** The rule enters at
`next_open` and `2026-08-19` is the last bar, so there is no bar to fill on.
No flag, no candidate, no entry. The numerator is still 0.

WHAT ELSE THIS SLICE FOUND
==========================
1.  **The declared ceiling does not bound what every artefact since slice 62
    said it bounds.** `max(0, N − HORIZON)` assumes a full-horizon hold; the
    short-side barrier resolves early 52.8% of the time, so a later decision
    can also close inside the window. The declared number stands — re-deriving
    a pre-declared bound in the slice a setup first fired is exactly the move
    this programme refuses — but the `if_exceeded` sentence was wrong and is
    corrected, and the unconditional bound (`N − 1`) is reported beside it.

2.  **The funding corpus genuinely grew** (+6 prints, uncompressed digest
    moved), clearing slice 71's watch item. The same check that answered
    "recompressed, not changed" last slice answers "changed" this slice. A test
    that has only ever returned one answer is not evidence; this one has now
    returned both.

3.  **Prose that could only describe one outcome.** The finding text carried
    the literal words "ZERO SETUPS FIRED" in two artefacts. This is the slice
    on which that became false. Both are now derived from the setup count.

EXPIRING ASSERTIONS
===================
Per §55f this module declares `EXPIRES_WITH_DATA`: the tests here that read
live corpus state and will need amendment when the next bar lands. An
assertion that expires is not a defect; one that expires with nobody having
written down that it would is.
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

FRESHNESS = "artifacts/slice72_data_freshness.json"
FORWARD = "artifacts/slice72_forward_shadow.json"
GATE = "artifacts/slice72_promotion_gate.json"

WINDOW = ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13",
          "2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17",
          "2026-08-18", "2026-08-19"]

PRIOR = {n: (f"artifacts/slice{n}_forward_shadow.json", ceiling)
         for n, ceiling in ((62, 0), (63, 0), (64, 0), (65, 0), (66, 0),
                            (67, 0), (68, 1), (69, 2), (70, 3), (71, 4))}


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


# ===========================================================================
# 1 — the window reached ten, and the funding is not stale
# ===========================================================================


class TestTheWindowReachedTen:

    def test_ten_closed_bars_lie_after_t1(self):
        result = cp.check(cp.LINEAR_BTC)
        assert result.appended_rows >= 10
        assert [s[:10] for s in result.appended_timestamps][:10] == WINDOW
        assert load(FRESHNESS)["after_t1_linear"] == 10
        assert load(FRESHNESS)["after_t1_dates"] == WINDOW

    def test_the_growth_is_measured_against_the_previous_artefact(self):
        delta = load(FRESHNESS)["delta_since_slice71"]
        assert delta["previous_after_t1_linear"] == 9
        assert delta["new_linear_bars_since_slice71"] == 1
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

    def test_the_funding_is_not_stale(self):
        """Slice 71's watch item, cleared. The mission makes it a FAIL
        condition: linear growth with funding still ending 2026-08-18T16:00."""
        delta = load(FRESHNESS)["delta_since_slice71"]
        assert delta["new_funding_prints_since_slice71"] == 6
        assert load(FRESHNESS)["latest_funding_timestamp"] == \
            "2026-08-20T16:00:00+00:00"
        digests = load(FRESHNESS)["digests"]
        assert digests["funding_genuinely_changed_this_slice"] is True
        assert digests["funding_identical_to_slice71_uncompressed"] is False

    def test_the_funding_covers_the_newest_decision(self):
        """A field slice 71 shipped INVERTED relative to its name.

        As written it asked whether the last print falls at or before the
        newest decision instant — true exactly when the corpus STOPS SHORT,
        which is the stale condition the name rules out. It returned True in
        slice 71 because the funding ended at 08-18T16:00 and the decision
        instant was 08-18T23:59:59.999, and it went False here while coverage
        was strictly better. Nothing consumed it, so no measurement was
        affected. Corrected in slice 72. EDGE.md §55h.
        """
        bars, _f, _folds = _corpus()
        funding = fb.load_funding(os.path.join(
            REPO, "data", "real_funding", "funding",
            "BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz"))
        rate = fb.funding_at_decision(bars, funding)[-1]
        assert rate == rate, "the newest decision joins to no rate at all"
        regime = load(FRESHNESS)["forward_funding_regime"]
        assert regime["funding_covers_the_newest_decision"] is True
        assert regime["funding_extends_past_the_newest_decision"] is True

    def test_the_old_field_would_have_answered_the_wrong_question(self):
        """The inversion, demonstrated rather than described."""
        bars, _f, _folds = _corpus()
        last_print = cp.read_rows(cp.FUNDING_BTC)[-1]["funding_time"]
        newest = dt.datetime.fromtimestamp(
            fb.close_time_ms(bars[-1]) / 1000.0,
            tz=dt.timezone.utc).isoformat()
        as_slice71_wrote_it = last_print <= newest
        assert as_slice71_wrote_it is False
        # ...while the question it was named for answers True.
        assert load(FRESHNESS)["forward_funding_regime"][
            "funding_covers_the_newest_decision"] is True
        # And slice 71's frozen artefact keeps what it said.
        assert load("artifacts/slice71_data_freshness.json")[
            "forward_funding_regime"][
                "funding_covers_the_newest_decision"] is True

    def test_the_uncompressed_check_now_answers_both_ways(self):
        """Slice 71 said 'recompressed, not changed'. Slice 72 says 'changed'.

        Same instrument, opposite answers, one slice apart — which is what
        makes it evidence rather than a formality. EDGE.md §54d, §55a.
        """
        assert load("artifacts/slice71_data_freshness.json")["digests"][
            "funding_identical_to_slice70_uncompressed"] is True
        assert load(FRESHNESS)["digests"][
            "funding_identical_to_slice71_uncompressed"] is False

    def test_only_btc_grew(self):
        grew = {p for p, r in cp.check_all().items() if r.extended}
        assert grew == {cp.LINEAR_BTC, cp.FUNDING_BTC}
        for path, result in cp.check_all().items():
            if "BTC" not in path:
                assert result.appended_rows == 0, path

    def test_the_open_bar_is_absent(self):
        """Clock-derived. 2026-08-20 is open; it is also present in FUNDING,
        which is correct and unrelated — funding prints are not bars."""
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        rows = cp.read_rows(cp.LINEAR_BTC)
        assert not any(r["time_period_start"][:10] >= today for r in rows)
        assert load(FRESHNESS)["open_bar_absent_as_expected"] is True

    def test_a_funding_print_dated_after_the_last_bar_is_not_a_bar(self):
        """AMENDED BY SLICE 73, and named in advance on slice 72's list.

        The CLAIM — that funding prints run past the newest closed bar, and
        that such a print is not a bar — is general and is asserted from the
        clock. The specific date it was written against (08-20) became a
        closed bar in slice 73, which is the thing that expired, not the
        claim. EDGE.md §55f, §56f.
        """
        rows = cp.read_rows(cp.LINEAR_BTC)
        last_bar = rows[-1]["time_period_start"][:10]
        prints = cp.read_rows(cp.FUNDING_BTC)
        assert prints[-1]["funding_time"][:10] > last_bar
        assert not any(r["time_period_start"][:10] > last_bar for r in rows)
        # What slice 72 recorded, kept against its frozen artefact.
        assert load(FRESHNESS)["after_t1_dates"][-1] == "2026-08-19"

    def test_every_note_claim_is_true(self):
        payload = load(FRESHNESS)
        assert payload["note_claims_all_true"] is True
        assert payload["false_note_claims"] == []
        assert payload["human_note_is_evidence"] is False
        assert payload["claim_vs_files_discrepancy"] is False
        assert len(payload["human_note_claims_checked"]) >= 13

    def test_the_note_quoted_the_measure_the_repository_pins(self):
        """First note to quote an UNCOMPRESSED funding digest. Scored."""
        digests = load(FRESHNESS)["digests"]
        assert digests["note_funding_digest_matches_the_uncompressed_file"] \
            is True
        assert load(FRESHNESS)["human_note_claims_checked"][
            "funding sha256 (uncompressed)"] is True

    def test_the_note_path_names_this_slice_and_slice_is_derived(self):
        payload = load(FRESHNESS)
        assert payload["human_note_path"] == \
            "docs/human/HUMAN_DATA_NOTE_SLICE72.md"
        assert payload["human_note_present"] is True
        assert payload["slice"] == 72
        source = os.path.join(REPO, "tools", "slice72_freshness.py")
        with open(source, encoding="utf-8") as handle:
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
        assert slice_const.value.value == 72, (
            "SLICE froze at its predecessor's value for the FOURTH slice "
            "running. EDGE.md §52a, §54f.")

    def test_the_slice_number_is_derived_in_every_tool(self):
        for name in ("slice72_freshness.py", "slice72_forward_shadow.py",
                     "slice72_promotion_gate.py"):
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
            assert load(artefact)["slice"] == 72, artefact

    def test_the_counts_came_from_this_slices_files(self):
        """AMENDED BY SLICE 73. Named on slice 72's EXPIRES_WITH_DATA list
        and in its verdict, so this amendment was a work item, not a
        surprise. EDGE.md §55f, §56f."""
        assert cp.check(cp.LINEAR_BTC).rows_on_disk >= \
            load(FRESHNESS)["linear_rows"]
        assert cp.check(cp.FUNDING_BTC).rows_on_disk >= \
            load(FRESHNESS)["funding_rows"]
        assert load(FRESHNESS)["linear_rows"] == 1471
        assert load(FRESHNESS)["funding_rows"] == 4416


# ===========================================================================
# 2 — the first forward setup, and what it is not
# ===========================================================================


class TestTheFirstForwardSetup:

    def test_exactly_one_forward_decision_reached_fund_abs(self):
        regime = load(FRESHNESS)["forward_funding_regime"]
        assert regime["forward_bars"] == 10
        assert regime["funding_setups_in_window"] == 1
        assert regime["setup_dates"] == ["2026-08-19"]
        assert regime["setup_directions"] == {"2026-08-19": fb.SHORT_SETUP}

    def test_the_rate_is_reproduced_from_the_corpus_not_the_artefact(self):
        bars, funding, folds = _corpus()
        window = list(fb.forward_window_indices(bars, folds))
        rates = fb.funding_at_decision(bars, funding)
        setups = fb.funding_setups(bars, funding, fund_abs=fb.FUND_ABS)
        target = next(i for i in window
                      if dt.datetime.fromtimestamp(
                          bars[i].start_ms / 1000.0,
                          tz=dt.timezone.utc).strftime("%Y-%m-%d")
                      == "2026-08-19")
        assert rates[target] == fb.FUND_ABS
        assert target in setups
        assert setups[target] == fb.SHORT_SETUP

    def test_the_qualifying_print_stood_at_the_close(self):
        """The distinction §51c has maintained since slice 68.

        08-19's prints rise through the day and the 16:00 one is last before
        the close, so the qualifying rate IS the decision rate.
        """
        prints = [r for r in cp.read_rows(cp.FUNDING_BTC)
                  if r["funding_time"][:10] == "2026-08-19"]
        assert [p["funding_time"][11:16] for p in prints] == \
            ["00:00", "08:00", "16:00"]
        assert float(prints[-1]["funding_rate"]) == fb.FUND_ABS
        assert max(float(p["funding_rate"]) for p in prints) == \
            float(prints[-1]["funding_rate"])

    def test_it_is_not_the_same_event_as_08_12(self):
        """08-12's equal print was superseded before its bar closed."""
        bars, funding, folds = _corpus()
        window = list(fb.forward_window_indices(bars, folds))
        rates = fb.funding_at_decision(bars, funding)
        setups = fb.funding_setups(bars, funding, fund_abs=fb.FUND_ABS)

        def on(date):
            return next(i for i in window
                        if dt.datetime.fromtimestamp(
                            bars[i].start_ms / 1000.0,
                            tz=dt.timezone.utc).strftime("%Y-%m-%d") == date)

        assert rates[on("2026-08-12")] == 0.00006601
        assert on("2026-08-12") not in setups
        prints = load(FRESHNESS)["forward_funding_regime"][
            "prints_at_or_above_fund_abs"]
        stamps = [p["funding_time"] for p in prints]
        assert "2026-08-12T00:00:00+00:00" in stamps
        assert "2026-08-19T16:00:00+00:00" in stamps
        assert all(p["equals_fund_abs_exactly"] for p in prints)

    def test_the_setup_produced_no_flag_no_candidate_no_entry_no_trade(self):
        forward = load(FORWARD)
        assert forward["forward_decisions"]["flagged_bars_in_window"] == []
        assert forward["forward_decisions"]["candidates_after_schedule"] == []
        assert forward["forward_decisions"]["entries_taken"] == 0
        assert forward["forward_n_trades"] == 0
        assert forward["is_forward_observation"] is False
        assert forward["trades"] == []
        assert forward["forward_mean_net_r"] is None

    def test_the_reason_is_the_rule_not_a_shortfall(self):
        """It is excluded for having no next bar, NOT for missing the line."""
        aside = load(FORWARD)["forward_decisions"]["why_the_rule_stood_aside"]
        last = aside[-1]
        assert last["bar_utc"][:10] == "2026-08-19"
        assert last["funding_setup_present"] is True
        assert last["is_last_bar_of_corpus"] is True
        assert "no next bar to fill on" in prose(last["excluded_as_last_bar"])
        assert load(FRESHNESS)["forward_funding_regime"][
            "setup_bars_that_are_the_last_bar_of_the_corpus"] == \
            ["2026-08-19"]

    def test_no_parameter_moved_to_produce_it(self):
        assert fb.FUND_ABS == 0.0001
        assert fb.HORIZON == 5
        assert shadow.constants_fingerprint()[:32] == \
            "662de0115880871352d5d623b1020eaa"
        assert load(FORWARD)["fund_abs_moved_this_slice"] is False
        assert load(FORWARD)["join_changed_this_slice"] is False
        assert load(FORWARD)["caps_changed_this_slice"] is False
        assert load(FORWARD)["thresholds_moved_this_slice"] is False

    def test_the_setup_is_not_counted_as_a_trade_anywhere(self):
        """The slice-59 substitution, in the only form still available."""
        assert load(GATE)["checklist"]["forward_shadow_clean"][
            "forward_trades_to_date"] == 0
        assert load(GATE)["items_complete"] == 2
        assert load(FORWARD)["is_stage1_evidence"] is False
        assert load(FORWARD)["registration_eligible"] is False
        assert load(FORWARD)["closer_to_autonomous_profit_agent"] is False
        why = prose(load(FRESHNESS)["forward_funding_regime"][
            "why_a_setup_is_not_a_trade"])
        assert "activity relabelled as experience" in why

    def test_the_finding_prose_is_derived_not_a_constant(self):
        """"ZERO SETUPS FIRED" was a literal until the slice it went false."""
        for text in (prose(load(FRESHNESS)["delta_since_slice71"]["finding"]),
                     prose(load(FORWARD)["window_grew_since_slice71"]["note"])):
            assert "ZERO SETUPS FIRED" not in text.upper()
            assert "1 of 10" in text
            assert "SETUP IS NOT A TRADE" in text.upper()
        finding = prose(load(FRESHNESS)["delta_since_slice71"]["finding"])
        assert "TEN BARS" in finding.upper()
        assert "NINE BARS" not in finding.upper()
        assert "ceiling 5, setups 1 of 10, fills 0" in finding


# ===========================================================================
# 3 — the ceiling, and the bound it does not actually provide
# ===========================================================================


class TestTheCeilingAndItsAssumption:

    def test_the_ceiling_is_five(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["closed_forward_bars"] == 10
        assert ceiling["max_possible_forward_closed_trades"] == 5
        assert ceiling["observed_forward_closed_trades"] == 0
        assert ceiling["within_ceiling"] is True
        assert "§55a" in ceiling["declared_in"]

    def test_it_is_not_four_and_not_six(self):
        ceiling = load(FORWARD)["ceiling"]["max_possible_forward_closed_trades"]
        assert ceiling != 4, "4 was slice 71's ceiling, at N=9"
        assert ceiling < 6, "6 would need N=11"

    def test_the_declared_formula_was_not_re_derived(self):
        """The correction is to the INTERPRETATION, not to the number."""
        payload = load(FRESHNESS)
        assert max(0, payload["after_t1_linear"] - fb.HORIZON) == 5
        assert load(FORWARD)["ceiling"][
            "max_possible_forward_closed_trades"] == 5
        assert fb.HORIZON == 5

    def test_barriers_resolve_early_more_than_half_the_time(self):
        """The empirical fact the correction rests on, measured not asserted."""
        bars, _funding, _folds = _corpus()
        _idx, _net, used = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=fb.TAKE_PROFIT_ATR, stop_atr=fb.STOP_ATR,
            horizon=fb.HORIZON, atr_period=fb.ATR_PERIOD,
            round_trip_bps=fb.ROUND_TRIP_BPS, side="short",
            entry_on=fb.ENTRY_ON)
        early = sum(1 for u in used if int(u) < fb.HORIZON)
        assert early > 0
        assert early / len(used) > 0.5, (
            "if this drops below a half the wording of §55c should be "
            "revisited, but the argument does not depend on the exact rate")

    def test_the_unconditional_bound_is_reported(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["unconditional_upper_bound"] == 9
        assert ceiling["unconditional_upper_bound"] == \
            load(FRESHNESS)["after_t1_linear"] - 1
        assert ceiling["observed_forward_closed_trades"] <= \
            ceiling["unconditional_upper_bound"]

    def test_the_if_exceeded_claim_was_corrected(self):
        text = prose(load(FORWARD)["ceiling"]["if_exceeded"])
        assert "CORRECTED IN SLICE 72" in text
        assert "§55c" in text
        assert "unconditional_upper_bound" in text
        assumption = prose(load(FORWARD)["ceiling"][
            "assumption_the_declared_ceiling_rests_on"])
        assert "full HORIZON" in assumption

    def test_the_old_claim_is_visible_in_the_frozen_artefacts(self):
        """Not erased from history: prior artefacts keep what they said."""
        for slice_number in (68, 70, 71):
            old = load(f"artifacts/slice{slice_number}_forward_shadow.json")
            assert "DATA DEFECT" in old["ceiling"]["if_exceeded"], slice_number

    def test_every_prior_slice_ceiling_is_recorded_and_lower(self):
        observed = [load(f)["ceiling"]["max_possible_forward_closed_trades"]
                    for _n, (f, _c) in sorted(PRIOR.items())]
        assert observed == [expected for _n, (_f, expected)
                            in sorted(PRIOR.items())]
        assert observed == [0, 0, 0, 0, 0, 0, 1, 2, 3, 4]
        assert load(FORWARD)["ceiling"][
            "max_possible_forward_closed_trades"] == 5

    def test_every_prior_slice_observed_zero_trades(self):
        for _n, (path, _c) in sorted(PRIOR.items()):
            assert load(path)["forward_n_trades"] == 0, path
        assert load(FORWARD)["forward_n_trades"] == 0

    def test_the_per_bar_table_covers_every_date(self):
        table = load(FORWARD)["funding_setup_table"]
        assert [row["date"] for row in table] == WINDOW
        assert [row["date"] for row in table
                if row["funding_setup_present"]] == ["2026-08-19"]
        assert table[-1]["shortfall_vs_fund_abs"] == 0.0
        assert all(row["shortfall_vs_fund_abs"] > 0 for row in table[:-1])

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
# 4 — three misses, still unrescued
# ===========================================================================


class TestTheMissesWereStillNotRescued:

    def test_the_three_misses_reproduce_from_the_corpus(self):
        bars, funding, folds = _corpus()
        window = list(fb.forward_window_indices(bars, folds))
        rates = fb.funding_at_decision(bars, funding)

        def on(date):
            return next(i for i in window
                        if dt.datetime.fromtimestamp(
                            bars[i].start_ms / 1000.0,
                            tz=dt.timezone.utc).strftime("%Y-%m-%d") == date)

        assert rates[on("2026-08-12")] == 0.00006601
        assert rates[on("2026-08-17")] == 0.00009202
        assert rates[on("2026-08-18")] == 0.00003650

    def test_the_reason_records_that_nothing_was_moved(self):
        why = prose(load(FORWARD)["why_neither_moved"])
        for stamp in ("2026-08-12T00:00Z", "2026-08-17T16:00Z",
                      "2026-08-18T16:00Z", "2026-08-19"):
            assert stamp in why, stamp
        assert "reached by a market is evidence" in why
        assert "void the clear" in why

    def test_the_restraint_is_what_makes_the_setup_evidence(self):
        why = prose(load(FRESHNESS)["forward_funding_regime"]["why_not"])
        assert "lowered is not" in why
        assert load(FRESHNESS)["forward_funding_regime"][
            "parameters_moved_because_of_this"] is False


# ===========================================================================
# 5 — the guard family, carried by import
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
                    if os.path.basename(p).startswith("slice72_")}
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
        path = os.path.join(REPO, "STAGE1_VERDICT_SLICE72.md")
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


class TestTheScoringWasNotReTuned:

    SUBSTITUTIONS = {
        "forward_shadow_observation/10": "forward_shadow_observation/11",
        "slice71_forward_shadow.json": "slice72_forward_shadow.json",
        "slice71_forward_shadow.log": "slice72_forward_shadow.log",
        "window_grew_since_slice70": "window_grew_since_slice71",
        "slice70_forward_shadow.json": "slice71_forward_shadow.json",
        "SLICE 71 — FORWARD SHADOW SEGMENT":
            "SLICE 72 — FORWARD SHADOW SEGMENT",
        "EDGE.md §54a, before this tool ran":
            "EDGE.md §55a, before this tool ran",
    }

    DECLARED_CHANGES = {
        "ceiling": "advances 4 -> 5; gains the assumption it rests on and the "
                   "unconditional bound; if_exceeded corrected per §55c",
        "why_neither_moved": "records that a threshold was reached without "
                             "anything being moved",
        "window_grew_since_slice71": "the note's setup count is now DERIVED "
                                     "rather than the literal 'ZERO SETUPS "
                                     "FIRED', which went false this slice",
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

    def test_the_scoring_was_not_re_tuned_since_slice71(self):
        old_body, old_payload = self._split(
            "slice71_forward_shadow.py", self.SUBSTITUTIONS)
        new_body, new_payload = self._split(
            "slice72_forward_shadow.py", self.SUBSTITUTIONS)
        assert old_body == new_body, "the SCORING changed between slices"
        assert set(old_payload) - set(new_payload) == set(), "a key was DROPPED"
        drifted = {k for k, v in old_payload.items()
                   if new_payload.get(k) != v}
        assert drifted == set(self.DECLARED_CHANGES), sorted(drifted)
        added = set(new_payload) - set(old_payload)
        assert added == set(self.DECLARED_ADDITIONS), sorted(added)

    def test_the_guard_is_where_the_next_slice_will_look_for_it(self):
        with open(os.path.join(REPO, "tests",
                               "test_slice72_ten_day_window.py"),
                  encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)}
        assert "test_the_scoring_was_not_re_tuned_since_slice71" in names


# ===========================================================================
# 6 — assertions that will expire, declared instead of discovered
# ===========================================================================


class TestExpiringAssertionsAreDeclared:
    """EDGE.md §55f. A list for slice 73 instead of a pytest surprise.

    Slice 71 shipped three assertions that expired the moment a bar arrived,
    in three different spellings. That is not a defect a guard can catch —
    each was correct when written. What IS controllable is whether the next
    slice inherits a list or a failure.

    So this module names its own live-reading tests, and an AST sweep checks
    the list against the module. A name missing from the list fails; a name
    on the list that reads nothing live fails too.
    """

    # Every test in this module that reads LIVE corpus state.
    EXPIRES_WITH_DATA = {
        "test_ten_closed_bars_lie_after_t1",
        "test_history_is_untouched_and_growth_is_an_append",
        "test_the_funding_covers_the_newest_decision",
        "test_the_old_field_would_have_answered_the_wrong_question",
        "test_only_btc_grew",
        "test_the_open_bar_is_absent",
        "test_a_funding_print_dated_after_the_last_bar_is_not_a_bar",
        "test_the_counts_came_from_this_slices_files",
        "test_the_rate_is_reproduced_from_the_corpus_not_the_artefact",
        "test_the_qualifying_print_stood_at_the_close",
        "test_it_is_not_the_same_event_as_08_12",
        "test_barriers_resolve_early_more_than_half_the_time",
        "test_the_three_misses_reproduce_from_the_corpus",
    }

    LIVE_READS = ("check", "check_all", "read_rows", "_corpus",
                  "load_corpus", "load_funding", "barrier_r_for_all_bars",
                  "funding_at_decision", "funding_setups",
                  "forward_window_indices", "close_time_ms")

    MODULE = "test_slice72_ten_day_window.py"

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
        assert missing == [], (
            f"these read live corpus state and are not declared: {missing}")
        assert stale == [], (
            f"these are declared but read nothing live: {stale}")

    def test_the_list_is_not_empty_and_not_everything(self):
        """A sweep that matched all or nothing would prove nothing."""
        total = {n.name for n in self._functions()}
        assert 0 < len(self.EXPIRES_WITH_DATA) < len(total)

    def test_the_verdict_hands_the_list_forward(self):
        path = os.path.join(REPO, "STAGE1_VERDICT_SLICE72.md")
        if not os.path.isfile(path):
            return
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        assert "EXPIRES_WITH_DATA" in text


# ===========================================================================
# 7 — the pack, and everything frozen
# ===========================================================================


class TestThePackAndTheFreezes:

    def test_no_restoration_was_needed_again(self):
        assert not os.path.exists(os.path.join(
            REPO, "artifacts", "slice72_restored_from_slice71.json"))
        assert load(FRESHNESS)["pack_regression"][
            "occurred_this_slice"] is False

    def test_the_pack_base_claim_is_true_this_time(self):
        claim = load(FRESHNESS)["pack_base_claim"]
        assert claim["true"] is True
        assert claim["was_false_when_first_made_in_slice67"] is True

    def test_edge_carries_every_section_from_45_to_55(self):
        with open(os.path.join(REPO, "EDGE.md"), encoding="utf-8") as handle:
            edge = handle.read()
        for section in range(45, 56):
            assert f"\n## {section}a." in edge, section

    def test_the_frozen_pack_is_intact(self):
        assert shadow.SHADOW_MAX_CONCURRENT_POSITIONS == 1
        assert shadow.SHADOW_MAX_ENTRIES_PER_DAY == 1
        assert shadow.SHADOW_MAX_NOTIONAL_USD == 100.00
        assert load(FORWARD)["constants_fingerprint_matches_frozen"] is True
        assert load(FORWARD)["schedule_mode"] == "one_entry_per_contiguous_run"
        assert load(FORWARD)["caps"]["symbol"] == "BTCUSDT"

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
        assert fb.folds_sha256() == \
            "ff5cc8a2bb92362058b376659ff12f314c10f71a101d1f581f69da31f025013c"
        assert fb.folds_are_unmodified() is True
        assert load(FRESHNESS)["folds_unmodified"] is True

    def test_no_edge_measurement_ran_this_slice(self):
        for name in sorted(os.listdir(os.path.join(REPO, "artifacts"))):
            if not name.startswith("slice72_") or not name.endswith(".json"):
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
        assert load(GATE)["items_complete_unchanged_from_slice_71"] is True

    def test_the_gate_refuses_to_credit_five_possible_trades(self):
        evidence = prose(load(GATE)["checklist"]["forward_shadow_clean"][
            "evidence"])
        assert "5 POSSIBLE TRADES DO NOT ADVANCE THIS GATE" in evidence
        assert "SETUP IS NOT A TRADE" in evidence.upper()
        assert "never the numerator of what has happened" in evidence

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
