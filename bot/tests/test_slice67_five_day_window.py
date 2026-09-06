"""Slice 67 — five bars, half a prediction confirmed, and the first false note.

THREE THINGS THIS SLICE IS THE FIRST TO SAY
===========================================
**A prediction made in advance came true.** §49b, written when `|W| = 4`,
declared `|W| = 5 → ceiling 0` and `|W| = 6 → ceiling 1`. The window is now 5
and the ceiling is 0. `TestThePredictionHeld` checks the observed value against
the *frozen artefact that recorded the prediction*, not against a fresh
computation — so it is a check on the claim, not a restatement of the formula.

**A human note made a claim that is false.** Every note claim across slices
62–66 was about DATA and every one verified. Slice 67's note adds one about
PROCESS — *"Pack base: post-restore slice66 tree (not slice-61)"* — and it is
wrong. It is scored in the same dict as the data claims, on the same terms, so
a false claim cannot be quarantined out of the tally.

**A guard fired on good news.** Slice 65's broadcast check asserted that all
three symbols declare one count, and said that if that stopped being true the
broadcast may have been repaired. It stopped. That check is retired and
replaced here with two that assert what is now true: the broadcast is over, and
the residue it left is not.

AND ONE THING THAT DID NOT HAPPEN
=================================
Slice 67 repaired **zero** live-absolute assertions in slice 66's tests — the
first slice in six that did not have to. Slice 66's AST meta-guard is why.
`test_the_meta_guard_is_still_enforced` keeps it that way.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import corpus_prefix as cp  # noqa: E402
import project_status as ps  # noqa: E402
import promotion_gate as pg  # noqa: E402
import shadow  # noqa: E402
from signals import funding_carry_fade_btc_v1 as fb  # noqa: E402

FRESHNESS = "artifacts/slice67_data_freshness.json"
FORWARD = "artifacts/slice67_forward_shadow.json"
GATE = "artifacts/slice67_promotion_gate.json"
RESTORED = "artifacts/slice67_restored_from_slice66.json"

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
}


def load(path: str) -> dict:
    with open(os.path.join(REPO, path), encoding="utf-8") as handle:
        return json.load(handle)


def sha256(path: str) -> str:
    with open(os.path.join(REPO, path), "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def prose(text: str) -> str:
    return " ".join(str(text).split())


# ===========================================================================
# 1 — the window grew to five, from files
# ===========================================================================


class TestTheWindowGrewToFive:

    def test_five_closed_bars_lie_after_t1(self):
        result = cp.check(cp.LINEAR_BTC)
        assert result.appended_rows >= 5
        assert [s[:10] for s in result.appended_timestamps][:5] == [
            "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13",
            "2026-08-14"]
        assert load(FRESHNESS)["after_t1_linear"] == 5
        assert load(FRESHNESS)["after_t1_dates"] == [
            "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13",
            "2026-08-14"]

    def test_the_growth_is_measured_against_the_previous_artefact(self):
        delta = load(FRESHNESS)["delta_since_slice66"]
        assert delta["previous_after_t1_linear"] == 4
        assert delta["new_linear_bars_since_slice66"] == 1
        assert delta["new_funding_prints_since_slice66"] == 2
        assert delta["the_window_grew"] is True

    def test_the_forward_artefact_agrees(self):
        grew = load(FORWARD)["window_grew_since_slice66"]
        assert grew["closed_forward_bars_previously"] == 4
        assert grew["closed_forward_bars_now"] == 5
        assert grew["delta"] == 1
        assert grew["grew"] is True

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

    def test_slice67s_counts_came_from_files_not_the_manifest(self):
        """Amended in slice 68 under the standing rule: a slice's live check
        belongs to whichever slice is current. Slice 67's counts are asserted
        against its OWN frozen prefix figures; the live equivalent lives in
        `tests/test_slice68_first_nonzero_ceiling.py`."""
        payload = load(FRESHNESS)
        linear = payload["prefix_invariant"]["checks"][cp.LINEAR_BTC]
        funding = payload["prefix_invariant"]["checks"][cp.FUNDING_BTC]
        assert payload["linear_rows"] == linear["rows_on_disk"] == 1466
        assert payload["funding_rows"] == funding["rows_on_disk"] == 4399


# ===========================================================================
# 2 — the first false claim a human note has made
# ===========================================================================


class TestTheNoteMadeOneFalseClaim:

    def test_the_growth_claim_is_true_and_reported_as_such(self):
        """Kept separate from the false one. A reader scanning for 'was the
        growth claim honest?' must not be answered by a different question."""
        payload = load(FRESHNESS)
        assert payload["growth_claim_verified"] is True
        assert payload["claim_vs_files_discrepancy"] is False

    def test_the_pack_base_claim_is_false(self):
        payload = load(FRESHNESS)
        assert payload["note_claims_all_true"] is False
        assert payload["false_note_claims"] == [
            "pack base: post-restore slice66 tree (not slice-61)"]
        assert payload["pack_base_claim"]["true"] is False
        assert payload["pack_base_claim"][
            "first_false_note_claim_in_the_programme"] is True

    def test_it_is_scored_in_the_same_dict_as_the_data_claims(self):
        """A false claim must not be quarantined into a footnote."""
        claims = load(FRESHNESS)["human_note_claims_checked"]
        assert len(claims) >= 8
        assert sum(1 for v in claims.values() if v is False) == 1
        assert any("pack base" in k for k in claims)

    def test_every_data_claim_the_note_made_is_true(self):
        """The finding is precise: DATA claims verified, the PROCESS claim did
        not. Blurring that would be as dishonest as ignoring the failure."""
        claims = load(FRESHNESS)["human_note_claims_checked"]
        data_claims = {k: v for k, v in claims.items() if "pack base" not in k}
        assert len(data_claims) >= 7
        assert all(data_claims.values()), data_claims

    def test_the_evidence_is_digests_not_an_impression(self):
        evidence = load(RESTORED)["evidence_the_pack_is_the_slice61_tree"]
        assert evidence["edge_sections_45_to_49_present_in_pack"] is False
        assert evidence["human_note_claimed_otherwise"] is True
        assert evidence["occurrences"] == 5
        assert evidence["EDGE.md"]["slice61_and_slice67_pack"] != \
            evidence["EDGE.md"]["slice66_deliverable"]

    def test_it_is_recorded_without_accusation(self):
        assert load(FRESHNESS)["pack_base_claim"][
            "recorded_without_accusation"] is True
        why = prose(load(FRESHNESS)["pack_base_claim"]["why_it_matters"])
        assert "cannot check which parent their build script used" in why


# ===========================================================================
# 3 — the prediction held
# ===========================================================================


class TestThePredictionHeld:

    def test_slice66_predicted_zero_at_five_bars(self):
        """Read from slice 66's FROZEN artefact — the prediction as recorded,
        not as remembered."""
        note = prose(load("artifacts/slice66_forward_shadow.json")[
            "window_grew_since_slice65"]["note"])
        assert "|W| = 5 still yields 0" in note
        assert "|W| = 6 yields 1" in note

    def test_and_the_observation_matches_it(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["closed_forward_bars"] == 5
        assert ceiling["max_possible_forward_closed_trades"] == 0
        assert ceiling["observed_forward_closed_trades"] == 0
        assert ceiling["within_ceiling"] is True
        assert "§50c" in ceiling["declared_in"]

    def test_the_ceiling_is_arithmetic_not_a_stored_zero(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["max_possible_forward_closed_trades"] == \
            max(0, ceiling["closed_forward_bars"] - fb.HORIZON)

    def test_the_remaining_half_of_the_prediction_is_one_day_away(self):
        def ceiling_at(bars):
            return max(0, bars - fb.HORIZON)

        assert ceiling_at(5) == 0
        assert ceiling_at(6) == 1
        assert fb.HORIZON == 5
        needed = (load(FORWARD)["ceiling"][
            "closed_forward_bars_needed_for_one_trade"]
            - load(FORWARD)["ceiling"]["closed_forward_bars"])
        assert needed == 1

    def test_no_ceiling_above_zero_is_claimed(self):
        """Forbidden explicitly by the mission while `after_t1_linear < 6`."""
        assert load(FORWARD)["ceiling"][
            "max_possible_forward_closed_trades"] == 0
        assert "NOT claimed this slice" in \
            load(FORWARD)["ceiling"]["note_on_the_next_milestone"]


class TestSixZerosAndWhatTheyMean:

    def test_every_prior_slice_also_ran_against_a_zero_ceiling(self):
        for slice_number, (_fresh, forward, bars) in sorted(PRIOR.items()):
            payload = load(forward)
            assert payload["forward_n_trades"] == 0, slice_number
            assert payload["ceiling"]["closed_forward_bars"] == bars
            assert payload["ceiling"][
                "max_possible_forward_closed_trades"] == 0, slice_number
            assert max(0, bars - fb.HORIZON) == 0, slice_number

    def test_the_window_sizes_form_the_expected_sequence(self):
        sizes = [load(f)["ceiling"]["closed_forward_bars"]
                 for _s, (_x, f, _b) in sorted(PRIOR.items())]
        assert sizes == [1, 1, 2, 3, 4]
        assert load(FORWARD)["ceiling"]["closed_forward_bars"] == 5

    def test_none_of_the_six_zeros_is_evidence_about_the_edge(self):
        note = prose(load(FORWARD)["window_grew_since_slice66"]["note"])
        assert "none of them is evidence for or against the edge" in note
        assert "the rule has never had an opportunity" in note

    def test_the_second_independent_reason_is_stated(self):
        """A longer window is necessary and not sufficient: the rule must also
        signal, and no forward funding rate has reached FUND_ABS."""
        note = prose(load(FORWARD)["window_grew_since_slice66"]["note"])
        assert "has not SIGNALLED" in note
        assert "not merely the window to lengthen" in note

    def test_the_rule_stood_aside_on_all_five_bars(self):
        aside = load(FORWARD)["forward_decisions"]["why_the_rule_stood_aside"]
        assert len(aside) == 5
        assert all(entry["funding_setup_present"] is False for entry in aside)
        last = [e for e in aside if e["is_last_bar_of_corpus"]]
        assert len(last) == 1 and last[0]["bar_utc"].startswith("2026-08-14")

    def test_four_of_five_have_only_the_funding_reason(self):
        aside = load(FORWARD)["forward_decisions"]["why_the_rule_stood_aside"]
        sole = [e for e in aside if not e["is_last_bar_of_corpus"]]
        assert len(sole) == 4
        assert all(e["fund_abs_threshold"] == fb.FUND_ABS for e in sole)

    def test_growth_and_observation_are_independent_and_disagree(self):
        assert load(FRESHNESS)["delta_since_slice66"]["the_window_grew"] is True
        assert load(FORWARD)["is_forward_observation"] is False
        assert load(FORWARD)["forward_n_trades"] == 0
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
# 4 — the guard that fired on good news, and the one that stopped a recurrence
# ===========================================================================


class TestTheManifestBroadcastWasRepaired:

    def test_the_broadcast_has_stopped(self):
        from test_slice55_data_eligibility import (  # noqa: PLC0415
            TestTheManifestsDescribeTheFilesBesideThem as Manifests)
        for directory in ("data/real_linear_1d", "data/real_funding"):
            with open(os.path.join(REPO, directory, "MANIFEST.json"),
                      encoding="utf-8") as handle:
                declared = json.load(handle)["date_range_utc"]
            assert declared["BTCUSDT"]["rows"] != declared["ETHUSDT"]["rows"]
            assert declared["ETHUSDT"]["rows"] == declared["SOLUSDT"]["rows"]
            _p, disk = Manifests._on_disk(directory, "BTCUSDT")
            assert declared["BTCUSDT"]["rows"] == disk

    def test_but_the_residue_remains(self):
        audit = load(FRESHNESS)["manifests"]
        assert audit["count_disagreeing"] == 4
        assert audit["measured_product_entries_are_accurate"] is True
        assert audit["not_edited_by_this_slice"] is True

    def test_sol_funding_stopped_moving_for_the_first_time_in_four_slices(self):
        from test_slice55_data_eligibility import (  # noqa: PLC0415
            TestTheManifestsDescribeTheFilesBesideThem as Manifests)
        with open(os.path.join(REPO, "data/real_funding/MANIFEST.json"),
                  encoding="utf-8") as handle:
            declared = json.load(handle)["date_range_utc"]["SOLUSDT"]["rows"]
        assert declared == Manifests.SOL_FUNDING_DECLARED_HISTORY[-1] == 4397
        _p, disk = Manifests._on_disk("data/real_funding", "SOLUSDT")
        assert disk == 4458
        assert declared < disk

    def test_the_manifests_were_not_edited_by_this_programme(self):
        assert load(FRESHNESS)["manifests_edited_by_this_slice"] is False
        assert "cannot attest" in prose(
            load(FRESHNESS)["why_manifests_not_edited"])


class TestTheMetaGuardEarnedItsKeep:

    def test_the_meta_guard_is_still_enforced(self):
        """Slice 66's AST sweep still runs and still passes on every test file,
        including the four this slice wrote or amended."""
        from test_slice66_four_day_window import (  # noqa: PLC0415
            TestNoTestPinsALiveAbsolute as Guard)
        guard = Guard()
        offences = {os.path.basename(p): guard._offences(p)
                    for p in guard._test_files()}
        assert {k: v for k, v in offences.items() if v} == {}

    def test_no_live_absolute_needed_repairing_this_slice(self):
        """The first slice in six. Recorded as evidence that making a mistake
        unshippable beats restating the rule."""
        record = load(RESTORED)
        assert "ZERO live-absolute" in record["the_meta_guard_worked"]
        amended = record["apparatus"].get("amended_in_slice67", {})
        for entry in amended.values():
            assert "live absolute" not in entry["why"].lower()


# ===========================================================================
# 5 — the restoration
# ===========================================================================


class TestTheRestoration:

    def test_every_restored_file_matches_its_recorded_digest(self):
        record = load(RESTORED)
        amended = record["apparatus"].get("amended_in_slice67", {})
        for group in ("apparatus", "dated_records"):
            files = record[group]["files"]
            assert files, group
            for relative, digest in files.items():
                assert os.path.exists(os.path.join(REPO, relative)), relative
                if relative in amended:
                    entry = amended[relative]
                    assert entry["sha256_as_restored_from_slice66"] == digest
                    assert sha256(relative) == \
                        entry["sha256_after_slice67_amendment"], relative
                    assert entry["is_this_a_dated_record"] is False
                    assert entry["why"] and entry["strength"]
                else:
                    assert sha256(relative) == digest, relative

    def test_no_dated_record_was_amended(self):
        record = load(RESTORED)
        amended = set(record["apparatus"].get("amended_in_slice67", {}))
        assert amended
        assert not (amended & set(record["dated_records"]["files"]))

    def test_the_dated_records_were_not_regenerated_on_this_tree(self):
        record = load(RESTORED)
        assert record["dated_records"]["regenerated_against_todays_corpus"] \
            is False
        checked = 0
        for relative in record["dated_records"]["files"]:
            if not relative.endswith(".json"):
                continue
            commit = load(relative).get("git_commit")
            if not commit:
                continue
            checked += 1
            probe = subprocess.run(
                ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
                cwd=REPO, capture_output=True)
            assert probe.returncode != 0, (
                f"{relative} carries commit {commit}, which EXISTS here — it "
                f"was regenerated, not restored")
        assert checked >= 15, checked

    def test_the_prior_artefacts_still_record_their_own_window_sizes(self):
        for slice_number, (fresh, _forward, bars) in sorted(PRIOR.items()):
            assert load(fresh)["after_t1_linear"] == bars, slice_number


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
        assert load(FORWARD)["caps"]["max_notional_usd"] == 100.00

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
            if not name.startswith("slice67_") or not name.endswith(".json"):
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

    def test_the_gate_evidence_refuses_the_six_zero_narrative(self):
        evidence = prose(load(GATE)["checklist"]["forward_shadow_clean"][
            "evidence"])
        assert "NOT SIX FAILED ATTEMPTS" in evidence
        assert "no quiet day here proves or disproves the clear" in evidence

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
