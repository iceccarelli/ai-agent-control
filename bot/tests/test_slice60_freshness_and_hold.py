"""Data freshness, the HOLD, and templates that do not move the gate.

WHAT THIS FILE IS GUARDING
==========================
Slice 60's whole risk is **manufactured progress**. There is no new data, so
every artefact it produces is at risk of dressing "we checked and nothing had
happened" as motion. Three specific substitutions are available and each has
tests here:

1. calling the HOLD a forward observation;
2. re-running the monitors on unchanged data and reporting the readings under a
   new slice number, so the record *looks* refreshed;
3. letting a blank template complete a checklist item — a placeholder mistaken
   for the thing it stands in for.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import project_status as ps  # noqa: E402
import promotion_gate as pg  # noqa: E402
import shadow  # noqa: E402
from signals import funding_carry_fade_btc_v1 as fb  # noqa: E402

ARTIFACTS = os.path.join(REPO, "artifacts")
FRESH = os.path.join(ARTIFACTS, "slice60_data_freshness.json")
HOLD = os.path.join(ARTIFACTS, "slice60_forward_shadow.json")
GATE60 = os.path.join(ARTIFACTS, "slice60_promotion_gate.json")
GATE59 = os.path.join(ARTIFACTS, "slice59_promotion_gate.json")
PROMO_DOCS = os.path.join(REPO, "docs", "promotion")


def prose(path):
    """A markdown file with its line wrapping and blockquote markers removed.

    Substring assertions over markdown are brittle for a reason that has
    nothing to do with the document: a sentence wrapped across two lines, or
    continued after a `>` marker, does not contain itself as a substring. Three
    assertions in this file failed on exactly that and the documents were
    fine.

    So prose is compared as prose. This is NOT a loosening — the phrases
    asserted are unchanged and still have to be present, word for word, in
    reading order.
    """
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    text = text.replace("\n>", " ").replace(">", " ")
    return " ".join(text.split())


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


# ===========================================================================
# 1 — freshness was proven, not assumed
# ===========================================================================


class TestTheFreshnessCheckIsHonest:

    def test_the_artefact_exists_and_names_its_source_of_truth(self):
        fresh = load(FRESH)
        assert fresh["schema"] == "data_freshness/1"
        assert fresh["t1_source"] == \
            "artifacts/funding_carry_fade_btc_v1_folds.json"
        assert fresh["folds_sha256"] == fb.folds_sha256()

    def test_t1_is_the_locked_measure_end(self):
        assert load(FRESH)["t1_previous"] == fb.load_folds()["t1"]

    def test_the_fetch_was_actually_attempted(self):
        """§43b required the attempt even though the answer was derivable:
        'I reasoned it must be zero' is weaker than 'I looked'."""
        assert load(FRESH)["fetch"]["fetch_attempted"] is True

    def test_a_failed_fetch_is_recorded_verbatim_not_summarised(self):
        fetch = load(FRESH)["fetch"]
        if fetch.get("fetch_succeeded") is False:
            assert fetch["error_class"]
            assert fetch["error_message"]
            assert fetch["rows_returned"] == 0

    def test_nothing_was_fabricated_or_appended(self):
        fresh = load(FRESH)
        assert fresh["bars_fabricated"] == 0
        assert fresh["corpus_appended_to"] is False

    def test_the_closed_bar_ceiling_was_computed(self):
        """The arithmetic recorded in §43b before the fetch ran."""
        ceiling = load(FRESH)["closed_bar_ceiling"]
        assert ceiling["max_possible_new_closed_bars"] == 0
        assert "DATA DEFECT" in ceiling["note"]

    def test_the_count_never_exceeds_the_ceiling(self):
        """If it ever did, that is a defect to investigate — not progress."""
        fresh = load(FRESH)
        assert fresh["new_linear_bars_count"] <= \
            fresh["closed_bar_ceiling"]["max_possible_new_closed_bars"]

    def test_new_bars_available_is_false(self):
        assert load(FRESH)["new_bars_available"] is False
        assert load(FRESH)["new_linear_bars_count"] == 0

    def test_only_closed_bars_would_count(self):
        rule = load(FRESH)["what_would_count"]
        assert rule["closed_only"] is True
        assert rule["first_forward_observation_needs_closed_bars"] == \
            fb.HORIZON + 1

    def test_synthetic_is_required_false_on_both_corpora(self):
        synthetic = load(FRESH)["synthetic"]
        assert synthetic["linear_manifest_synthetic"] is False
        assert synthetic["funding_manifest_synthetic"] is False


# ===========================================================================
# 2 — the HOLD is a HOLD
# ===========================================================================


class TestTheHoldIsNotDressedAsProgress:

    def test_it_says_HOLD(self):
        hold = load(HOLD)
        assert hold["result"] == "HOLD"
        assert hold["is_forward_observation"] is False
        assert hold["forward_observations_to_date"] == 0
        assert hold["reason"] == "no bars after measure end"

    def test_the_monitors_were_not_re_run(self):
        """The subtle substitution: re-reporting unchanged readings under a new
        slice number manufactures the appearance of a refreshed observation."""
        hold = load(HOLD)
        assert hold["monitors_re_run"] is False
        assert hold["monitor_readings"] is None
        assert hold["monitor_status"] is None
        assert hold["entries_blocked_by_monitor"] is None
        assert "manufacture" in hold["why_monitors_were_not_re_run"]

    def test_it_points_at_slice59s_readings_rather_than_copying_them(self):
        stands = load(HOLD)["monitor_readings_that_stand"]
        assert stands["source"] == "artifacts/slice59_forward_shadow.json"
        assert stands["m4_state"] == "WARN"

    def test_the_pointed_at_readings_match_slice59_exactly(self):
        """If these ever disagree, one artefact is lying about the other."""
        stands = load(HOLD)["monitor_readings_that_stand"]
        prior = load(os.path.join(ARTIFACTS, "slice59_forward_shadow.json"))
        m4 = next(r for r in prior["monitor_readings"]
                  if r["name"] == "M4_halves")
        assert stands["monitor_status"] == prior["monitor_status"]
        assert stands["m4_state"] == m4["state"]
        assert stands["m4_detail"] == m4["detail"]

    def test_the_single_new_funding_print_is_explained_not_counted(self):
        """1 print exists after t1 and produces nothing. Reporting the count
        without that sentence would read as partial progress."""
        hold = load(HOLD)
        assert hold["new_funding_prints_count"] >= 1
        note = hold["one_new_funding_print_note"]
        assert "produces NOTHING" in note
        assert "decision clock is the daily bar" in note
        assert hold["forward_observations_to_date"] == 0

    def test_it_states_that_a_hold_is_not_progress(self):
        assert "Nothing advanced this slice" in load(HOLD)["hold_is_not_progress"]

    def test_nothing_was_fabricated(self):
        assert load(HOLD)["bars_fabricated"] == 0

    def test_it_cannot_register_anything(self):
        hold = load(HOLD)
        assert hold["is_stage1_evidence"] is False
        assert hold["registration_eligible"] is False

    def test_dropping_it_anywhere_registers_nothing(self, tmp_path):
        (tmp_path / "x_summary.json").write_text(json.dumps(load(HOLD)),
                                                 encoding="utf-8")
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_the_clear_was_not_re_scored(self):
        hold = load(HOLD)
        assert hold["cleared_edge_re_scored_this_slice"] is False
        offending = [n for n in os.listdir(ARTIFACTS)
                     if n.startswith("slice60_")
                     and ("edge_" in n or "control_" in n or "_summary" in n)]
        assert not offending, offending

    def test_the_frozen_policy_is_restated_unchanged(self):
        hold = load(HOLD)
        assert hold["constants"] == dict(fb.CONSTANTS)
        assert hold["constants_fingerprint"] == shadow.CONSTANTS_FINGERPRINT
        assert hold["schedule_mode"] == "one_entry_per_contiguous_run"
        assert hold["caps"]["max_notional_usd"] == 100.00
        assert hold["monitor_thresholds"] == shadow.MONITOR_THRESHOLDS
        assert hold["thresholds_moved_this_slice"] is False

    def test_it_claims_no_progress_toward_autonomy(self):
        hold = load(HOLD)
        assert hold["closer_to_autonomous_profit_agent"] is False
        assert hold["promotion_gate_allows_live"] is False
        assert hold["live_authorized"] is False
        assert hold["models_current_present"] is False


# ===========================================================================
# 3 — templates are not progress
# ===========================================================================


class TestTemplatesDoNotMoveTheGate:

    TEMPLATES = ("RISK_MEMO_MICRO_LIVE_TEMPLATE.md",
                 "KILL_SWITCH_DRILL_SCRIPT.md",
                 "LINEAR_STOP_VERIFICATION_CHECKLIST.md")

    @pytest.mark.parametrize("name", TEMPLATES)
    def test_the_template_exists(self, name):
        assert os.path.isfile(os.path.join(PROMO_DOCS, name))

    @pytest.mark.parametrize("name", TEMPLATES)
    def test_it_announces_that_it_is_blank(self, name):
        text = prose(os.path.join(PROMO_DOCS, name))
        assert "TEMPLATE" in text or "NOT YET" in text
        assert "completes no checklist item" in text

    def test_the_memo_signature_blocks_are_empty(self):
        with open(os.path.join(PROMO_DOCS, self.TEMPLATES[0]),
                  encoding="utf-8") as handle:
            text = handle.read()
        assert "INTENTIONALLY BLANK" in text
        # Every signature line is still an underscore run — nothing filled in.
        for line in text.splitlines():
            if line.strip().startswith(("Proposer", "Risk officer",
                                        "Second reviewer", "Verified by",
                                        "Drill performed by")):
                assert "___" in line, line

    def test_the_memo_states_the_thinness_rather_than_selling_it(self):
        text = prose(os.path.join(PROMO_DOCS, self.TEMPLATES[0]))
        assert "three rotation replicates" in text
        assert "NOT held-out data" in text
        assert "not that it is reassuring" in text

    def test_the_memo_forbids_moving_the_m4_threshold(self):
        text = prose(os.path.join(PROMO_DOCS, self.TEMPLATES[0]))
        assert "Moving the M-4 threshold is not one of the options" in text

    def test_the_stop_checklist_names_the_simulator_gap(self):
        text = prose(os.path.join(PROMO_DOCS, self.TEMPLATES[2]))
        assert "NotImplementedError" in text
        assert "never been exercised end to end" in text

    # -- and the gate did not move ----------------------------------------

    def test_templates_are_not_a_checklist_item(self):
        """The load-bearing assertion of this class."""
        keys = [key for key, _d, _o in pg.CHECKLIST_ITEMS]
        assert not any("template" in key for key in keys)

    def test_the_gate_never_reads_the_template_directory(self):
        import inspect
        source = inspect.getsource(pg)
        assert "docs/promotion" not in source
        assert "TEMPLATE" not in source

    def test_the_gate_file_records_templates_outside_the_checklist(self):
        gate = load(GATE60)
        assert gate["templates_present"] is True
        assert "templates_present" not in gate["checklist"]
        assert len(gate["templates"]) == 3

    def test_the_completed_count_did_not_change(self):
        assert load(GATE60)["items_complete"] == load(GATE59)["items_complete"]
        assert load(GATE60)["items_complete_unchanged_from_slice_59"] is True

    def test_no_signature_was_forged(self):
        gate = load(GATE60)
        assert gate["signatures_present"] is False
        assert gate["signatures_forged"] is False

    def test_the_gate_still_refuses_under_the_new_file(self):
        verdict = pg.evaluate_promotion_gate(path=GATE60)
        assert verdict.allows_live is False
        assert len(verdict.incomplete) == 6

    def test_the_human_items_are_still_incomplete(self):
        gate = load(GATE60)
        for key in ("human_risk_memo_signed", "kill_switch_drill_recorded",
                    "linear_protective_stop_verified",
                    "live_trading_ack_present",
                    "m4_recent_half_accepted_or_recovered",
                    "forward_shadow_clean"):
            assert gate["checklist"][key]["complete"] is False, key

    def test_the_evidence_says_a_template_is_not_a_memo(self):
        gate = load(GATE60)
        assert "A template is a form; a memo is a decision" in \
            gate["checklist"]["human_risk_memo_signed"]["evidence"]
        assert "did not run is worth nothing" in \
            gate["checklist"]["kill_switch_drill_recorded"]["evidence"]

    def test_the_forward_item_records_the_slice60_recheck(self):
        evidence = load(GATE60)["checklist"]["forward_shadow_clean"]["evidence"]
        assert "0 new closed bars" in evidence
        assert "HOLD" in evidence


# ===========================================================================
# 4 — the standing posture, unchanged
# ===========================================================================


class TestNothingMovedThisSlice:

    def test_the_gate_refuses_from_both_files(self):
        for path in (GATE59, GATE60):
            assert pg.promotion_gate_allows_live(path=path) is False

    def test_live_is_still_dark(self):
        status = ps.current()
        assert status.live_authorized is False
        assert status.policy_mode == "off"
        assert status.execution_mode == "paper"
        assert status.models_current_present is False
        assert not os.path.exists(os.path.join(REPO, "models", "current"))

    def test_the_clear_still_comes_from_the_slice57_artefact(self):
        assert ps.cleared_edge_signal_from_artifacts(ARTIFACTS) == \
            "funding_carry_fade_btc_v1"
        oos = load(os.path.join(
            ARTIFACTS, "slice57_oos_edge_BTCUSDT_summary.json"))
        assert oos["observed"]["n_trades"] == 41
        assert round(oos["m1"]["percentile"], 2) == 95.13
        assert oos["m2"]["percentile"] == 96.0

    def test_the_eleven_freezes_are_intact(self):
        assert len(ps.ABSENT_SIGNALS) == 11
        assert "funding_carry_fade_v1" in ps.ABSENT_SIGNALS

    @pytest.mark.parametrize("signal", list(ps.ABSENT_SIGNALS))
    def test_a_forged_positive_is_still_refused(self, signal, tmp_path):
        (tmp_path / "x_summary.json").write_text(json.dumps({
            "symbol": "BTCUSDT", "signal": signal,
            "observed": {"n_trades": 500, "mean_r": 1.0},
            "m1": {"percentile": 99.9, "bar": 95.0, "passed": True},
            "m2": {"percentile": 99.9, "bar": 95.0, "passed": True},
            "control_validated": True, "verdict": "EDGE_EVIDENCE_POSITIVE",
            "registration_eligible": True, "window": "oos_late",
        }), encoding="utf-8")
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_the_caps_and_thresholds_are_untouched(self):
        assert shadow.SHADOW_MAX_NOTIONAL_USD == 100.00
        assert shadow.SHADOW_MAX_CONCURRENT_POSITIONS == 1
        assert shadow.SHADOW_MAX_ENTRIES_PER_DAY == 1
        assert shadow.MONITOR_THRESHOLDS["M1_rolling_trades"]["alert_below"] \
            == -0.25
        assert shadow.MONITOR_THRESHOLDS["M3_concentration"]["warn_above"] \
            == 0.60

    def test_the_folds_were_not_re_cut(self):
        assert fb.folds_are_unmodified()
        assert fb.load_folds()["t1"] == "2026-08-09T00:00:00Z"
