"""A pack that describes data it does not contain, and the HOLD that follows.

WHAT THIS FILE IS GUARDING
==========================
The slice-61 human pack names two extended data files and contains neither. The
risk is not that anyone lied — a large member failing to attach is a routine
accident. The risk is that a *description* of an extension gets treated as an
extension, which is the third costume of one recurring substitution (EDGE.md
§44d):

    slice 55  a rule in prose        instead of a rule in code
    slice 59  history relabelled     instead of forward experience
    slice 61  a note asserting data  instead of the data

So the tests here check the files, the hashes and the counts — never the note.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
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
FRESH = os.path.join(ARTIFACTS, "slice61_data_freshness.json")
HOLD = os.path.join(ARTIFACTS, "slice61_forward_shadow.json")
GATE61 = os.path.join(ARTIFACTS, "slice61_promotion_gate.json")
GATE60 = os.path.join(ARTIFACTS, "slice60_promotion_gate.json")

LINEAR = "data/real_linear_1d/ohlcv/BINANCE_LINEAR_BTC_USDT_1D.csv.gz"
FUNDING = "data/real_funding/funding/BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz"


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def gz_sha256(path):
    with gzip.open(os.path.join(REPO, path), "rt", encoding="utf-8",
                   newline="") as handle:
        return hashlib.sha256(handle.read().encode("utf-8")).hexdigest()


def rows(path):
    with gzip.open(os.path.join(REPO, path), "rt", encoding="utf-8",
                   newline="") as handle:
        return list(csv.DictReader(io.StringIO(handle.read())))


# ===========================================================================
# 1 — the files, not the note
# ===========================================================================


class TestTheCorpusIsCheckedByHashNotByDescription:

    def test_both_files_still_hash_to_their_slice57_pins_over_the_prefix(self):
        """The single most informative assertion in this file — now split.

        As written in slice 61 this asserted a whole-file digest, and said so
        proudly: identical hashes prove BOTH that no history was rewritten
        (the note's more important promise) AND that no extension exists
        (identical files cannot contain new rows).

        **In slice 62 those two claims came apart.** A human supplied a real
        extension, so the whole-file digests necessarily differ — and the first
        claim, the one that matters, still holds exactly. Only the SECOND half
        was ever a dated observation, and slice 61's own artefact continues to
        record it (see the tests immediately below, which read that frozen
        record and still pass unchanged).

        What survives here is the invariant: the measured history is
        byte-for-byte the history that was measured. EDGE.md §45c.
        """
        import tools.corpus_prefix as cp  # noqa: PLC0415
        folds = fb.load_folds()
        linear = cp.check(cp.LINEAR_BTC)
        funding = cp.check(cp.FUNDING_BTC)
        assert linear.prefix_sha256 == \
            folds["sources"]["linear_bars"]["sha256_uncompressed"]
        assert funding.prefix_sha256 == \
            folds["sources"]["funding"]["sha256_uncompressed"]
        assert linear.append_only, linear.why_not()
        assert funding.append_only, funding.why_not()

    def test_the_artefact_records_both_hash_comparisons(self):
        check = load(FRESH)["hash_check"]
        assert check["linear_unchanged_since_t1"] is True
        assert check["funding_unchanged_since_t1"] is True
        assert check["linear_sha256"] == check["linear_pinned_in_folds"]
        assert check["funding_sha256"] == check["funding_pinned_in_folds"]

    def test_it_says_what_the_hashes_prove(self):
        proof = load(FRESH)["hash_check"]["what_this_proves"]
        assert "NO HISTORY WAS REWRITTEN" in proof
        assert "NO EXTENSION EXISTS" in proof

    def test_no_history_was_rewritten(self):
        """Stated separately because it is the promise that was KEPT."""
        assert load(HOLD)["history_rewritten"] is False

    def test_the_artefact_still_records_that_the_last_bar_was_t1(self):
        """A dated record of a moment, asserted as one.

        Slice 61 checked this against the disk, because in slice 61 the disk
        and the record said the same thing. They no longer do, and the record
        is the one that is frozen: `slice61_data_freshness.json` will say the
        corpus ended at `t1` for as long as it exists, because that is what
        was true when it was written. What is on the disk TODAY is asserted in
        `tests/test_slice62_forward_extension.py`, against today's rule.

        Conflating the two is how a dated artefact quietly becomes a living
        document — slices 54 and 57 both had to unpick that, and this is the
        third time.
        """
        folds = fb.load_folds()
        recorded = load(FRESH)["latest_linear_timestamp"]
        assert recorded.startswith(folds["t1"][:10])
        assert load(FRESH)["new_linear_bars_count"] == 0

    def test_the_disk_has_since_moved_past_that_record(self):
        """And the record was not edited to keep up. That is the point.

        If this ever fails while the tests above pass, someone has rewritten a
        dated artefact to agree with a later observation.
        """
        folds = fb.load_folds()
        last = rows(LINEAR)[-1]["time_period_start"]
        assert last > folds["t1"][:10]
        assert load(FRESH)["latest_linear_timestamp"] < last

    def test_zero_bars_after_t1(self):
        assert load(FRESH)["new_linear_bars_count"] == 0

    def test_extension_present_is_false(self):
        assert load(FRESH)["extension_present"] is False
        assert load(FRESH)["new_bars_available"] is False


class TestThePackIsDescribedByItsManifest:

    def test_the_note_is_in_the_tree(self):
        assert os.path.isfile(os.path.join(
            REPO, "docs", "human", "HUMAN_DATA_NOTE_SLICE61.md"))

    def test_the_note_claims_two_extended_files(self):
        claimed = load(FRESH)["human_note_claims_extended_files"]
        assert len(claimed) == 2
        assert any("real_linear_1d" in c for c in claimed)
        assert any("real_funding" in c for c in claimed)

    def test_the_pack_contained_no_data_files(self):
        pack = load(FRESH)["human_pack"]
        assert pack["data_member_count"] == 0
        assert pack["member_count"] == 2
        assert all(m["name"].endswith(".md") for m in pack["members"])

    def test_the_pack_is_pinned_by_hash(self):
        pack = load(FRESH)["human_pack"]
        assert len(pack["sha256"]) == 64
        assert pack["bytes"] > 0

    def test_the_discrepancy_is_recorded_without_accusation(self):
        """The finding must be stated as a fact about artefacts, not a claim
        about intent. A large member failing to attach is routine."""
        text = load(FRESH)["discrepancy"]
        assert "without accusation" in text
        assert "fail to attach" in text
        assert "not settled" in text

    def test_the_freshness_tool_reads_files_not_the_note(self):
        """AST-free but structural: the tool hashes the corpus and never opens
        the note to decide anything."""
        import inspect

        import slice61_freshness as tool
        source = inspect.getsource(tool)
        assert "sha256" in source
        # The note's path appears only as a recorded FACT, never parsed.
        assert "HUMAN_DATA_NOTE" in source
        assert "read_note" not in source
        assert "parse_note" not in source

    def test_no_fetch_was_attempted_and_the_reason_is_given(self):
        fresh = load(FRESH)
        assert fresh["fetch_attempted"] is False
        assert "ProxyError" in fresh["why_no_fetch"]
        assert "activity, not evidence" in fresh["why_no_fetch"]


# ===========================================================================
# 2 — the HOLD
# ===========================================================================


class TestTheHoldIsHonest:

    def test_it_says_HOLD_with_zero_forward(self):
        hold = load(HOLD)
        assert hold["result"] == "HOLD"
        assert hold["extension_present"] is False
        assert hold["is_forward_observation"] is False
        assert hold["forward_observations_to_date"] == 0
        assert hold["forward_n_trades"] == 0
        assert hold["forward_mean_net_r"] is None

    def test_is_forward_observation_tracks_the_count(self):
        """`true` only if the count is above zero — never because bars or
        entries exist, only because trades CLOSED."""
        hold = load(HOLD)
        assert hold["is_forward_observation"] == \
            (hold["forward_observations_to_date"] > 0)

    def test_nothing_was_fabricated(self):
        assert load(HOLD)["bars_fabricated"] == 0
        assert load(FRESH)["bars_fabricated"] == 0
        assert load(FRESH)["corpus_appended_to"] is False

    def test_the_monitors_were_not_re_run(self):
        hold = load(HOLD)
        assert hold["monitors_re_run"] is False
        assert hold["monitor_readings"] is None
        assert hold["monitor_status"] is None
        assert "manufacture" in hold["why_monitors_were_not_re_run"]

    def test_the_readings_that_stand_match_slice59_exactly(self):
        stands = load(HOLD)["monitor_readings_that_stand"]
        prior = load(os.path.join(ARTIFACTS, "slice59_forward_shadow.json"))
        m4 = next(r for r in prior["monitor_readings"]
                  if r["name"] == "M4_halves")
        assert stands["monitor_status"] == prior["monitor_status"]
        assert stands["m4_state"] == m4["state"] == "WARN"
        assert stands["m4_detail"] == m4["detail"]

    def test_m4_was_not_silenced(self):
        assert load(HOLD)["monitor_readings_that_stand"]["m4_state"] == "WARN"
        assert "not silenced" in \
            load(HOLD)["monitor_readings_that_stand"]["note"]

    def test_the_single_funding_print_still_produces_nothing(self):
        hold = load(HOLD)
        assert hold["new_funding_prints_count"] >= 1
        assert "produces nothing" in hold["one_new_funding_print_note"]
        assert hold["forward_observations_to_date"] == 0

    def test_it_cannot_register_anything(self):
        hold = load(HOLD)
        assert hold["is_stage1_evidence"] is False
        assert hold["registration_eligible"] is False

    def test_even_renamed_as_a_summary_it_registers_nothing(self, tmp_path):
        (tmp_path / "x_summary.json").write_text(json.dumps(load(HOLD)),
                                                 encoding="utf-8")
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_the_clear_was_not_re_scored(self):
        assert load(HOLD)["cleared_edge_re_scored_this_slice"] is False
        offending = [n for n in os.listdir(ARTIFACTS)
                     if n.startswith("slice61_")
                     and ("edge_" in n or "control_" in n or "_summary" in n)]
        assert not offending, offending

    def test_it_claims_no_progress(self):
        hold = load(HOLD)
        assert hold["closer_to_autonomous_profit_agent"] is False
        assert "Nothing advanced this slice" in hold["hold_is_not_progress"]


# ===========================================================================
# 3 — nothing moved
# ===========================================================================


class TestTheFrozenPolicyIsUntouched:

    def test_the_fingerprint_is_the_declared_one(self):
        assert shadow.constants_fingerprint()[:32] == \
            "662de0115880871352d5d623b1020eaa"
        assert load(HOLD)["constants_fingerprint_matches_frozen"] is True

    def test_the_constants_are_the_frozen_ones(self):
        assert load(HOLD)["constants"] == dict(fb.CONSTANTS) == {
            "FUND_ABS": 0.0001, "STOP_ATR": 1.5, "TAKE_PROFIT_R": 1.0,
            "TAKE_PROFIT_ATR": 1.5, "HORIZON": 5, "ATR_PERIOD": 14,
            "LOCKUP": 1, "ROUND_TRIP_BPS": 25.0, "ENTRY_ON": "next_open"}

    def test_the_caps_are_untouched(self):
        assert shadow.SHADOW_MAX_NOTIONAL_USD == 100.00
        assert shadow.SHADOW_MAX_CONCURRENT_POSITIONS == 1
        assert shadow.SHADOW_MAX_ENTRIES_PER_DAY == 1
        assert load(HOLD)["caps"]["max_notional_usd"] == 100.00

    def test_the_schedule_is_still_one_per_run(self):
        assert load(HOLD)["schedule_mode"] == "one_entry_per_contiguous_run"

    def test_the_thresholds_are_untouched(self):
        assert load(HOLD)["monitor_thresholds"] == shadow.MONITOR_THRESHOLDS
        assert shadow.MONITOR_THRESHOLDS["M1_rolling_trades"]["alert_below"] \
            == -0.25
        assert shadow.MONITOR_THRESHOLDS["M3_concentration"]["warn_above"] \
            == 0.60
        assert load(HOLD)["thresholds_moved_this_slice"] is False

    def test_the_folds_were_not_re_cut(self):
        assert fb.folds_are_unmodified()
        assert fb.load_folds()["t1"] == "2026-08-09T00:00:00Z"

    def test_the_clear_still_comes_from_slice_57(self):
        assert ps.cleared_edge_signal_from_artifacts(ARTIFACTS) == \
            "funding_carry_fade_btc_v1"
        oos = load(os.path.join(
            ARTIFACTS, "slice57_oos_edge_BTCUSDT_summary.json"))
        assert oos["observed"]["n_trades"] == 41
        assert round(oos["m1"]["percentile"], 2) == 95.13
        assert oos["m2"]["percentile"] == 96.0
        assert oos["control_validated"] is True

    def test_the_eleven_freezes_are_intact(self):
        assert len(ps.ABSENT_SIGNALS) == 11
        assert "funding_carry_fade_v1" in ps.ABSENT_SIGNALS
        assert ps.FROZEN_STATUS["funding_carry_fade_v1"] == "ABSENT"

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


class TestTheGateStillRefuses:

    def test_it_refuses_from_every_gate_file(self):
        for path in (GATE60, GATE61):
            assert pg.promotion_gate_allows_live(path=path) is False

    def test_the_completed_count_did_not_change(self):
        assert load(GATE61)["items_complete"] == load(GATE60)["items_complete"]
        assert load(GATE61)["items_complete_unchanged_from_slice_60"] is True

    def test_six_items_remain_incomplete(self):
        verdict = pg.evaluate_promotion_gate(path=GATE61)
        assert len(verdict.incomplete) == 6

    def test_the_forward_item_records_the_slice61_recheck(self):
        evidence = load(GATE61)["checklist"]["forward_shadow_clean"]["evidence"]
        assert "contained none" in evidence
        assert "HOLD" in evidence
        assert "hash identically" in evidence

    def test_the_gate_records_the_pack_it_never_saw_data_from(self):
        pack = load(GATE61)["human_pack_slice61"]
        assert pack["data_files"] == 0
        assert "not data" in pack["note"]

    def test_the_human_items_are_still_incomplete(self):
        gate = load(GATE61)
        for key in ("human_risk_memo_signed", "forward_shadow_clean",
                    "m4_recent_half_accepted_or_recovered",
                    "kill_switch_drill_recorded",
                    "linear_protective_stop_verified",
                    "live_trading_ack_present"):
            assert gate["checklist"][key]["complete"] is False, key

    def test_live_is_still_dark(self):
        status = ps.current()
        assert status.live_authorized is False
        assert status.policy_mode == "off"
        assert status.execution_mode == "paper"
        assert status.models_current_present is False
        assert not os.path.exists(os.path.join(REPO, "models", "current"))
