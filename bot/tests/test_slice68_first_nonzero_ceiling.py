"""Slice 68 — the ceiling opens, one print lands on the line, nothing moves.

WHAT THIS SLICE IS THE FIRST TO SAY
===================================
**The ceiling is 1.** `max(0, 6 - HORIZON)`. §49b predicted it from a four-bar
window and §50c confirmed the first half at five; this is the second half, and
`TestThePredictionCompleted` checks it against the *frozen artefacts that
recorded the prediction* rather than against a fresh computation.

**A forward print landed exactly on FUND_ABS and did not become a setup.**
`2026-08-12T00:00:00Z` printed `0.00010000`, and `funding_setups` compares
`f >= fund_abs` — inclusive. It was not the rate at the decision, because the
join reads the rate standing at the bar's CLOSE and two later prints superseded
it. `TestTheThresholdWasTouchedAndNotCrossed` pins the whole chain: the
inclusive comparison, the close-time join, the superseding prints, and the fact
that no parameter moved because of any of it.

**The pack regression ended.** The pack is the slice-67 deliverable. There is no
restoration artefact this slice because there was nothing to restore — the first
time since slice 62.

WHAT IS DELIBERATELY NOT CLAIMED
================================
That six quiet days say anything about the edge. No position was opened, so only
the rule's *selectivity* was exercised, not its skill. The cleared run scheduled
41 trades in two years — about one per eighteen days — so six quiet days is
unremarkable under the null and the alternative alike.
"""
from __future__ import annotations

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
from signals import funding_carry_fade_btc_v1 as fb  # noqa: E402

FRESHNESS = "artifacts/slice68_data_freshness.json"
FORWARD = "artifacts/slice68_forward_shadow.json"
GATE = "artifacts/slice68_promotion_gate.json"

PRIOR = {
    62: ("artifacts/slice62_data_freshness.json",
         "artifacts/slice62_forward_shadow.json", 1),
    63: ("artifacts/slice63_data_freshness.json",
         "artifacts/slice63_forward_shadow.json", 1),
    64: ("artifacts/slice64_data_freshness.json",
         "artifacts/slice64_forward_shadow.json", 2),
    65: ("artifacts/slice65_data_freshness.json",
         "artifacts/slice65_forward_shadow.json", 3),
    66: ("artifacts/slice66_data_freshness.json",
         "artifacts/slice66_forward_shadow.json", 4),
    67: ("artifacts/slice67_data_freshness.json",
         "artifacts/slice67_forward_shadow.json", 5),
}


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
# 1 — the window reached six, from files
# ===========================================================================


class TestTheWindowReachedSix:

    def test_six_closed_bars_lie_after_t1(self):
        result = cp.check(cp.LINEAR_BTC)
        assert result.appended_rows >= 6
        assert [s[:10] for s in result.appended_timestamps][:6] == [
            "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13",
            "2026-08-14", "2026-08-15"]
        assert load(FRESHNESS)["after_t1_linear"] == 6
        assert load(FRESHNESS)["after_t1_dates"] == [
            "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13",
            "2026-08-14", "2026-08-15"]

    def test_the_growth_is_measured_against_the_previous_artefact(self):
        delta = load(FRESHNESS)["delta_since_slice67"]
        assert delta["previous_after_t1_linear"] == 5
        assert delta["new_linear_bars_since_slice67"] == 1
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

    def test_only_btc_grew(self):
        grew = {p for p, r in cp.check_all().items() if r.extended}
        assert grew == {cp.LINEAR_BTC, cp.FUNDING_BTC}
        for path, result in cp.check_all().items():
            if "BTC" not in path:
                assert result.appended_rows == 0, path

    def test_no_unclosed_bar_is_on_disk(self):
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        rows = cp.read_rows(cp.LINEAR_BTC)
        assert not any(r["time_period_start"][:10] >= today for r in rows)
        assert load(FRESHNESS)["open_bar_absent_as_expected"] is True

    def test_every_note_claim_is_true_this_time(self):
        """Including the pack-base claim that was FALSE in slice 67."""
        payload = load(FRESHNESS)
        assert payload["note_claims_all_true"] is True
        assert payload["false_note_claims"] == []
        assert payload["claim_vs_files_discrepancy"] is False
        assert any("pack" in k for k in payload["human_note_claims_checked"])
        assert payload["human_note_is_evidence"] is False

    def test_slice68s_counts_came_from_files_not_the_manifest(self):
        """Amended in slice 69 under the standing rule: a live check belongs to
        whichever slice is current. Slice 68's counts are asserted against its
        OWN frozen prefix figures."""
        payload = load(FRESHNESS)
        linear = payload["prefix_invariant"]["checks"][cp.LINEAR_BTC]
        funding = payload["prefix_invariant"]["checks"][cp.FUNDING_BTC]
        assert payload["linear_rows"] == linear["rows_on_disk"] == 1467
        assert payload["funding_rows"] == funding["rows_on_disk"] == 4403


# ===========================================================================
# 2 — the ceiling is 1, and the prediction completed
# ===========================================================================


class TestThePredictionCompleted:

    def test_the_ceiling_is_one(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["closed_forward_bars"] == 6
        assert ceiling["max_possible_forward_closed_trades"] == 1
        assert ceiling["observed_forward_closed_trades"] == 0
        assert ceiling["within_ceiling"] is True
        assert "§51b" in ceiling["declared_in"]

    def test_the_ceiling_is_arithmetic_not_a_stored_one(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["max_possible_forward_closed_trades"] == \
            max(0, ceiling["closed_forward_bars"] - fb.HORIZON)
        assert fb.HORIZON == 5

    def test_slice66_predicted_this_two_slices_ago(self):
        """Read from the FROZEN artefact — the prediction as recorded."""
        note = prose(load("artifacts/slice66_forward_shadow.json")[
            "window_grew_since_slice65"]["note"])
        assert "|W| = 6 yields 1" in note

    def test_slice67_confirmed_the_first_half(self):
        prior = load("artifacts/slice67_forward_shadow.json")["ceiling"]
        assert prior["closed_forward_bars"] == 5
        assert prior["max_possible_forward_closed_trades"] == 0

    def test_the_ceiling_counts_decision_bars_not_outcomes(self):
        note = prose(load(FORWARD)["ceiling"]["note_on_this_milestone"])
        assert "DECISION BARS" in note
        assert "not a forecast" in note

    def test_a_ceiling_of_zero_would_now_be_wrong(self):
        """Mission rule: `after_t1_linear = 6` with ceiling 0 is a FAIL."""
        assert load(FRESHNESS)["after_t1_linear"] == 6
        assert load(FORWARD)["ceiling"][
            "max_possible_forward_closed_trades"] != 0

    def test_a_ceiling_of_two_would_also_be_wrong(self):
        assert load(FORWARD)["ceiling"][
            "max_possible_forward_closed_trades"] < 2


# ===========================================================================
# 3 — the threshold was touched and not crossed
# ===========================================================================


class TestTheThresholdWasTouchedAndNotCrossed:

    def test_the_comparison_is_inclusive(self):
        """Asserted against the code's behaviour, not its docstring."""
        bars, funding, _folds = _corpus()
        assert fb.FUND_ABS == 0.0001
        rates = fb.funding_at_decision(bars, funding)
        setups = fb.funding_setups(bars, funding, fund_abs=fb.FUND_ABS)
        exactly = [t for t in range(rates.size)
                   if rates[t] == fb.FUND_ABS]
        assert exactly, "no bar in the whole corpus sits exactly on FUND_ABS"
        for index in exactly:
            assert index in setups, (
                "a rate equal to FUND_ABS did not produce a setup — the "
                "comparison is no longer inclusive")

    def test_exactly_one_forward_print_reached_the_threshold(self):
        regime = load(FRESHNESS)["forward_funding_regime"]
        assert len(regime["prints_at_or_above_fund_abs"]) == 1
        entry = regime["prints_at_or_above_fund_abs"][0]
        assert entry["funding_time"].startswith("2026-08-12T00:00")
        assert entry["equals_fund_abs_exactly"] is True
        assert entry["funding_rate"] == fb.FUND_ABS

    def test_but_it_was_not_the_rate_at_any_decision(self):
        regime = load(FRESHNESS)["forward_funding_regime"]
        rates = regime["rate_at_each_forward_decision"]
        assert len(rates) == 6
        assert all(value < fb.FUND_ABS for value in rates.values())
        assert regime["funding_setups_in_window"] == 0

    def test_the_join_reads_the_close_not_the_open(self):
        """The mechanism, asserted rather than described.

        The 2026-08-12 bar's decision rate must be the LAST print at or before
        its close, which is the 16:00 print — not the 00:00 print that touched
        the threshold.
        """
        bars, funding, folds = _corpus()
        window = list(fb.forward_window_indices(bars, folds))
        rates = fb.funding_at_decision(bars, funding)
        target = next(i for i in window
                      if dt.datetime.fromtimestamp(
                          bars[i].start_ms / 1000.0,
                          tz=dt.timezone.utc).strftime("%Y-%m-%d")
                      == "2026-08-12")
        assert rates[target] == 0.00006601
        assert rates[target] < fb.FUND_ABS
        prints = {row["funding_time"]: float(row["funding_rate"])
                  for row in cp.read_rows(cp.FUNDING_BTC)}
        assert prints["2026-08-12T00:00:00+00:00"] == fb.FUND_ABS
        assert prints["2026-08-12T16:00:00+00:00"] == rates[target]

    def test_no_parameter_moved_because_of_the_near_miss(self):
        regime = load(FRESHNESS)["forward_funding_regime"]
        assert regime["parameters_moved_because_of_this"] is False
        assert "fitted to the data" in prose(regime["why_not"])
        assert fb.FUND_ABS == 0.0001
        assert fb.HORIZON == 5
        assert shadow.constants_fingerprint()[:32] == \
            "662de0115880871352d5d623b1020eaa"

    def test_it_is_not_recorded_as_a_missed_trade(self):
        finding = prose(load(FRESHNESS)["forward_funding_regime"]["finding"])
        assert "not a missed trade and not a defect" in finding


# ===========================================================================
# 4 — what the zero means, stated narrowly
# ===========================================================================


class TestWhatThisZeroMeans:

    def test_every_prior_slice_ran_at_a_zero_ceiling(self):
        for slice_number, (_fresh, forward, bars) in sorted(PRIOR.items()):
            payload = load(forward)
            assert payload["forward_n_trades"] == 0, slice_number
            assert payload["ceiling"]["closed_forward_bars"] == bars
            assert payload["ceiling"][
                "max_possible_forward_closed_trades"] == 0, slice_number

    def test_this_slice_is_the_first_with_a_non_zero_ceiling(self):
        prior_ceilings = [load(f)["ceiling"][
            "max_possible_forward_closed_trades"]
            for _s, (_x, f, _b) in sorted(PRIOR.items())]
        assert set(prior_ceilings) == {0}
        assert load(FORWARD)["ceiling"][
            "max_possible_forward_closed_trades"] == 1

    def test_the_artefact_says_what_the_zero_does_not_mean(self):
        note = prose(load(FORWARD)["window_grew_since_slice67"]["note"])
        assert "says NOTHING about whether the rule makes money" in note
        assert "only its selectivity was exercised, not its skill" in note

    def test_the_artefact_gives_the_base_rate_that_makes_six_days_unremarkable(
            self):
        note = prose(load(FORWARD)["window_grew_since_slice67"]["note"])
        assert "41 trades in two years" in note
        assert "one per eighteen days" in note

    def test_the_rule_stood_aside_on_all_six_bars(self):
        aside = load(FORWARD)["forward_decisions"]["why_the_rule_stood_aside"]
        assert len(aside) == 6
        assert all(entry["funding_setup_present"] is False for entry in aside)
        last = [e for e in aside if e["is_last_bar_of_corpus"]]
        assert len(last) == 1 and last[0]["bar_utc"].startswith("2026-08-15")

    def test_growth_and_observation_remain_independent(self):
        assert load(FRESHNESS)["delta_since_slice67"]["the_window_grew"] is True
        assert load(FORWARD)["is_forward_observation"] is False
        assert load(FORWARD)["forward_n_trades"] == 0
        assert load(FORWARD)["forward_observations_to_date"] == 0
        assert load(FORWARD)["trades"] == []

    def test_no_clamping_no_slicing_no_invention(self):
        assert load(FORWARD)["forward_decisions"][
            "exit_clamping_to_corpus_end"] is False
        assert load(FORWARD)["forward_window"]["bar_array_sliced"] is False
        assert load(FORWARD)["forward_window"]["invented_future_bars"] == 0
        assert load(FORWARD)["bars_fabricated"] == 0
        assert load(FRESHNESS)["bars_fabricated"] == 0

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
# 5 — the pack arrived intact
# ===========================================================================


class TestThePackArrivedIntact:

    def test_no_restoration_was_needed(self):
        """The first slice since 62 with nothing to restore."""
        assert not os.path.exists(os.path.join(
            REPO, "artifacts", "slice68_restored_from_slice67.json"))

    def test_the_apparatus_from_slices_62_to_67_is_all_present(self):
        required = [
            "tools/corpus_prefix.py",
            "tests/test_slice62_forward_extension.py",
            "tests/test_slice63_window_did_not_grow.py",
            "tests/test_slice64_window_grew.py",
            "tests/test_slice65_three_day_window.py",
            "tests/test_slice66_four_day_window.py",
            "tests/test_slice67_five_day_window.py",
        ] + [f"tools/slice{n}_{k}.py" for n in range(62, 68)
             for k in ("freshness", "forward_shadow", "promotion_gate")]
        missing = [p for p in required
                   if not os.path.exists(os.path.join(REPO, p))]
        assert missing == [], missing

    def test_edge_carries_every_section_from_45_to_51(self):
        with open(os.path.join(REPO, "EDGE.md"), encoding="utf-8") as handle:
            edge = handle.read()
        for section in range(45, 52):
            assert f"\n## {section}a." in edge, section

    def test_the_meta_guard_is_still_enforced(self):
        from test_slice66_four_day_window import (  # noqa: PLC0415
            TestNoTestPinsALiveAbsolute as Guard)
        guard = Guard()
        offences = {os.path.basename(p): guard._offences(p)
                    for p in guard._test_files()}
        assert {k: v for k, v in offences.items() if v} == {}


class TestTheManifestResidueIsUnchanged:

    def test_the_broadcast_is_still_stopped(self):
        for directory in ("data/real_linear_1d", "data/real_funding"):
            with open(os.path.join(REPO, directory, "MANIFEST.json"),
                      encoding="utf-8") as handle:
                declared = json.load(handle)["date_range_utc"]
            assert declared["BTCUSDT"]["rows"] != declared["ETHUSDT"]["rows"]
            assert declared["ETHUSDT"]["rows"] == declared["SOLUSDT"]["rows"]

    def test_the_btc_entries_are_accurate(self):
        audit = load(FRESHNESS)["manifests"]
        assert audit["measured_product_entries_are_accurate"] is True
        assert audit["count_disagreeing"] == 4
        assert audit["not_edited_by_this_slice"] is True


# ===========================================================================
# 6 — nothing frozen moved, and the gate still refuses
# ===========================================================================


class TestNothingMovedAndTheGateRefuses:

    def test_the_frozen_pack_is_intact(self):
        assert shadow.constants_fingerprint()[:32] == \
            "662de0115880871352d5d623b1020eaa"
        assert shadow.SHADOW_MAX_CONCURRENT_POSITIONS == 1
        assert shadow.SHADOW_MAX_ENTRIES_PER_DAY == 1
        assert shadow.SHADOW_MAX_NOTIONAL_USD == 100.00
        assert load(FORWARD)["constants_fingerprint_matches_frozen"] is True
        assert load(FORWARD)["schedule_mode"] == "one_entry_per_contiguous_run"
        assert load(FORWARD)["caps_changed_this_slice"] is False
        assert load(FORWARD)["thresholds_moved_this_slice"] is False

    def test_the_monitor_thresholds_are_unchanged(self):
        thresholds = shadow.MONITOR_THRESHOLDS
        assert thresholds["M1_rolling_trades"]["warn_below"] == 0.0
        assert thresholds["M1_rolling_trades"]["alert_below"] == -0.25
        assert thresholds["M2_rolling_days"]["window_days"] == 90
        assert thresholds["M3_concentration"]["warn_above"] == 0.60
        assert thresholds["M4_halves"]["min_trades"] == 20

    def test_eleven_families_stay_frozen(self):
        assert len(ps.ABSENT_SIGNALS) == 11
        assert "funding_carry_fade_v1" in ps.ABSENT_SIGNALS
        assert load(FORWARD)["frozen_absent_count"] == 11

    def test_eth_and_sol_were_not_measured_under_the_btc_name(self):
        assert fb.UNIVERSE == ("BTCUSDT",)
        assert load(FORWARD)["symbol"] == "BTCUSDT"

    def test_the_oos_clear_was_not_re_scored(self):
        assert sha256("artifacts/slice57_oos_edge_BTCUSDT_summary.json") == \
            "28b7dfe0f22c4867661bc73f6434c4acce54a0483c4469a6540155b1b3c18c51"
        assert load(FORWARD)["cleared_edge_re_scored_this_slice"] is False
        assert load(FORWARD)["is_stage1_evidence"] is False
        assert load(FORWARD)["registration_eligible"] is False
        assert load(FORWARD)["forward_mean_net_r"] is None
        assert fb.folds_sha256() == \
            "ff5cc8a2bb92362058b376659ff12f314c10f71a101d1f581f69da31f025013c"
        assert fb.folds_are_unmodified() is True

    def test_no_edge_measurement_ran_this_slice(self):
        for name in sorted(os.listdir(os.path.join(REPO, "artifacts"))):
            if not name.startswith("slice68_") or not name.endswith(".json"):
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
        assert load(FORWARD)["promotion_gate_allows_live"] == \
            pg.promotion_gate_allows_live()

    def test_the_minimums_did_not_move(self):
        assert pg.MIN_FORWARD_TRADES == 20
        assert pg.MIN_FORWARD_DAYS == 180
        assert load(GATE)["minimums_moved_this_slice"] is False

    def test_the_gate_refuses_to_credit_a_possible_trade(self):
        evidence = prose(load(GATE)["checklist"]["forward_shadow_clean"][
            "evidence"])
        assert "ONE POSSIBLE TRADE DOES NOT ADVANCE THIS GATE" in evidence
        assert "denominator of what is possible, not the numerator" in evidence

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

    def test_the_funding_seam_is_still_unfilled(self):
        assert load(FRESHNESS)["funding_seam"]["missing_prints"] == \
            ["2026-08-09T16:00:00+00:00"]

    def test_no_fetch_was_attempted(self):
        assert load(FRESHNESS)["fetch_attempted"] is False
