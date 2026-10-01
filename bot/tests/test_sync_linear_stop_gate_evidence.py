"""sync_linear_stop_gate_evidence.py: the gate's
linear_protective_stop_verified item tracks the SIGNED checklist, and
nothing else moves.

Dry-run by default, --write required to persist, refuses rather than
transcribes when the checklist is not fully signed, and never touches a
different human-owned checklist item (human_risk_memo_signed,
m4_recent_half_accepted_or_recovered) or anything live-arming.
"""
from __future__ import annotations

import ast
import copy
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
TOOL_PATH = os.path.join(BOT, "tools", "sync_linear_stop_gate_evidence.py")

sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, BOT)

import sync_linear_stop_gate_evidence as sync_tool  # noqa: E402

SIGNED_CHECKLIST = """# LINEAR PROTECTIVE-STOP VERIFICATION

[x] 1. A protective stop is PLACED on the venue for a linear position.
       observed: tip 2c84e89. transcript: bot/artifacts/linear_protective_stop_venue.json

[x] 2. The stop SURVIVES a process restart.
       observed: tip 228fb87. bot/artifacts/linear_stop_hold.json

[x] 3. A position that somehow ends up NAKED is detected within one cycle.
       observed: tip b10cfc7. bot/artifacts/linear_stop_naked_induce.json

[x] 4. Margin and liquidation behaviour on linear is understood and documented.
       observed: tip b8dcd84. bot/artifacts/linear_stop_margin_doc.json

## Sign-off

Verified by         : Vincenzo Ceccarelli  date: 2026-09-27
Reviewed by         : ______________________  date: __________
"""

UNSIGNED_CHECKLIST = SIGNED_CHECKLIST.replace(
    "Verified by         : Vincenzo Ceccarelli  date: 2026-09-27",
    "Verified by         : ______________________  date: __________")

PARTIAL_CHECKLIST = SIGNED_CHECKLIST.replace("[x] 4.", "[ ] 4.")

MEMO_TEXT = "# LINEAR MARGIN & LIQUIDATION MEMO\n\nfilled with real numbers.\n"


def _write_text(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _write_json(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def _write_all_known_artifacts(artifacts_dir):
    """A citation is only good once the file it names actually exists. Tests
    that exercise the flip-to-complete path stand up a real (fixture)
    artifact per `KNOWN_ARTIFACTS` name so the existence check the tool
    performs has something genuine to find."""
    os.makedirs(artifacts_dir, exist_ok=True)
    for name in sync_tool.KNOWN_ARTIFACTS:
        _write_json(os.path.join(artifacts_dir, name), {"verdict": "PASSED"})


def _gate(*, complete: bool = False) -> dict:
    return {
        "checklist": {
            "linear_protective_stop_verified": {
                "complete": complete,
                "owner": "human",
                "evidence": "not documented. NOTE: a human must document ...",
            },
            "human_risk_memo_signed": {
                "complete": False, "owner": "human",
                "evidence": "no memo path recorded",
            },
            "m4_recent_half_accepted_or_recovered": {
                "complete": False, "owner": "human or observation",
                "evidence": "M4_halves currently reads WARN ...",
            },
            "kill_switch_drill_recorded": {
                "complete": True, "owner": "human",
                "evidence": "drill transcript ok",
                "signature": {"witnessed_by": "Vincenzo Ceccarelli"},
            },
            "notional_cap_within_policy": {"complete": True, "owner": "machine"},
            "models_current_absent_or_contained": {"complete": True, "owner": "machine"},
        },
        "items_total": 8,
        "items_complete": 3,
    }


class TestASignedChecklistFlipsExactlyOneItem:
    """These fixtures stand up their own artifact files under `tmp_path` so
    the tool's existence check has real, if fake, JSON to find — the tool
    must not care THAT the bytes are fabricated for a test, only that a file
    genuinely exists where the checklist says it does."""

    def test_dry_run_prints_the_change_and_writes_nothing(self, tmp_path):
        checklist_path = str(tmp_path / "checklist.md")
        memo_path = str(tmp_path / "memo.md")
        gate_path = str(tmp_path / "gate.json")
        artifacts_dir = str(tmp_path / "artifacts")
        _write_text(checklist_path, SIGNED_CHECKLIST)
        _write_text(memo_path, MEMO_TEXT)
        _write_json(gate_path, _gate())
        _write_all_known_artifacts(artifacts_dir)
        before = open(gate_path, encoding="utf-8").read()

        result = sync_tool.sync(checklist_path=checklist_path, memo_path=memo_path,
                                gate_path=gate_path, artifacts_dir=artifacts_dir,
                                write=False)

        assert result["ok"] is True
        assert result["changed"] is True
        assert result["written"] is False
        assert open(gate_path, encoding="utf-8").read() == before

    def test_write_flips_complete_true_and_records_signature(self, tmp_path):
        checklist_path = str(tmp_path / "checklist.md")
        memo_path = str(tmp_path / "memo.md")
        gate_path = str(tmp_path / "gate.json")
        artifacts_dir = str(tmp_path / "artifacts")
        _write_text(checklist_path, SIGNED_CHECKLIST)
        _write_text(memo_path, MEMO_TEXT)
        original = _gate()
        _write_json(gate_path, original)
        _write_all_known_artifacts(artifacts_dir)

        result = sync_tool.sync(checklist_path=checklist_path, memo_path=memo_path,
                                gate_path=gate_path, artifacts_dir=artifacts_dir,
                                write=True)
        assert result["written"] is True

        gate = json.load(open(gate_path, encoding="utf-8"))
        item = gate["checklist"]["linear_protective_stop_verified"]
        assert item["complete"] is True
        assert "Vincenzo Ceccarelli" in item["evidence"]
        assert "linear_protective_stop_venue.json" in item["evidence"]
        assert item["signature"]["verified_by"] == "Vincenzo Ceccarelli"
        assert item["signature"]["date"] == "2026-09-27"

        # Every other checklist key is byte-identical to what was written.
        for key in ("human_risk_memo_signed", "m4_recent_half_accepted_or_recovered",
                    "kill_switch_drill_recorded"):
            assert gate["checklist"][key] == original["checklist"][key]

    def test_items_complete_is_recounted_not_hardcoded(self, tmp_path):
        checklist_path = str(tmp_path / "checklist.md")
        memo_path = str(tmp_path / "memo.md")
        gate_path = str(tmp_path / "gate.json")
        artifacts_dir = str(tmp_path / "artifacts")
        _write_text(checklist_path, SIGNED_CHECKLIST)
        _write_text(memo_path, MEMO_TEXT)
        _write_json(gate_path, _gate())
        _write_all_known_artifacts(artifacts_dir)

        result = sync_tool.sync(checklist_path=checklist_path, memo_path=memo_path,
                                gate_path=gate_path, artifacts_dir=artifacts_dir,
                                write=True)
        assert result["items_complete_old"] == 3
        assert result["items_complete_new"] == 4

        gate = json.load(open(gate_path, encoding="utf-8"))
        assert gate["items_complete"] == 4

    def test_cli_exits_zero_and_reports_the_flip(self, tmp_path, capsys):
        checklist_path = str(tmp_path / "checklist.md")
        memo_path = str(tmp_path / "memo.md")
        gate_path = str(tmp_path / "gate.json")
        artifacts_dir = str(tmp_path / "artifacts")
        _write_text(checklist_path, SIGNED_CHECKLIST)
        _write_text(memo_path, MEMO_TEXT)
        _write_json(gate_path, _gate())
        _write_all_known_artifacts(artifacts_dir)

        rc = sync_tool.main(["--checklist", checklist_path, "--memo", memo_path,
                            "--gate", gate_path, "--artifacts-dir", artifacts_dir])
        assert rc == 0
        assert "False -> True" in capsys.readouterr().out

    def test_second_run_is_a_no_op(self, tmp_path):
        checklist_path = str(tmp_path / "checklist.md")
        memo_path = str(tmp_path / "memo.md")
        gate_path = str(tmp_path / "gate.json")
        artifacts_dir = str(tmp_path / "artifacts")
        _write_text(checklist_path, SIGNED_CHECKLIST)
        _write_text(memo_path, MEMO_TEXT)
        _write_json(gate_path, _gate())
        _write_all_known_artifacts(artifacts_dir)

        first = sync_tool.sync(checklist_path=checklist_path, memo_path=memo_path,
                               gate_path=gate_path, artifacts_dir=artifacts_dir,
                               write=True)
        assert first["written"] is True

        second = sync_tool.sync(checklist_path=checklist_path, memo_path=memo_path,
                                gate_path=gate_path, artifacts_dir=artifacts_dir,
                                write=True)
        assert second["changed"] is False
        assert second["written"] is False


class TestRefusesWhenCitedArtifactsDoNotExistOnDisk:
    """The exact defect this PR found in the real repo: a checklist can cite
    an artifact by name with nothing behind it. Reproduced here with a
    synthetic checklist/memo so the case is exercised even when the real
    repo's own checklist is blank (see `TestAppliedToTheRealRepoFiles`)."""

    def test_a_fully_signed_checklist_still_refuses_if_no_artifact_exists(self, tmp_path):
        checklist_path = str(tmp_path / "checklist.md")
        memo_path = str(tmp_path / "memo.md")
        gate_path = str(tmp_path / "gate.json")
        artifacts_dir = str(tmp_path / "artifacts")  # deliberately never created
        _write_text(checklist_path, SIGNED_CHECKLIST)
        _write_text(memo_path, MEMO_TEXT)
        _write_json(gate_path, _gate())

        result = sync_tool.sync(checklist_path=checklist_path, memo_path=memo_path,
                                gate_path=gate_path, artifacts_dir=artifacts_dir,
                                write=True)

        assert result["ok"] is False
        assert "linear_protective_stop_venue.json" in result["reason"]
        gate = json.load(open(gate_path, encoding="utf-8"))
        assert gate["checklist"]["linear_protective_stop_verified"]["complete"] is False

    def test_a_partially_missing_artifact_set_also_refuses(self, tmp_path):
        checklist_path = str(tmp_path / "checklist.md")
        memo_path = str(tmp_path / "memo.md")
        gate_path = str(tmp_path / "gate.json")
        artifacts_dir = str(tmp_path / "artifacts")
        _write_text(checklist_path, SIGNED_CHECKLIST)
        _write_text(memo_path, MEMO_TEXT)
        _write_json(gate_path, _gate())
        _write_all_known_artifacts(artifacts_dir)
        # Item 1's own artifact is real; one other cited file is not.
        os.remove(os.path.join(artifacts_dir, "linear_stop_hold.json"))

        result = sync_tool.sync(checklist_path=checklist_path, memo_path=memo_path,
                                gate_path=gate_path, artifacts_dir=artifacts_dir,
                                write=True)

        assert result["ok"] is False
        assert "linear_stop_hold.json" in result["reason"]

    def test_a_non_json_file_at_the_path_still_refuses(self, tmp_path):
        checklist_path = str(tmp_path / "checklist.md")
        memo_path = str(tmp_path / "memo.md")
        gate_path = str(tmp_path / "gate.json")
        artifacts_dir = str(tmp_path / "artifacts")
        _write_text(checklist_path, SIGNED_CHECKLIST)
        _write_text(memo_path, MEMO_TEXT)
        _write_json(gate_path, _gate())
        _write_all_known_artifacts(artifacts_dir)
        _write_text(os.path.join(artifacts_dir, sync_tool.ITEM_1_ARTIFACT),
                    "not json at all")

        result = sync_tool.sync(checklist_path=checklist_path, memo_path=memo_path,
                                gate_path=gate_path, artifacts_dir=artifacts_dir,
                                write=True)

        assert result["ok"] is False
        assert sync_tool.ITEM_1_ARTIFACT in result["reason"]


class TestRefusesRatherThanTranscribesAJudgmentCall:
    def test_any_item_still_unticked_refuses(self, tmp_path):
        checklist_path = str(tmp_path / "checklist.md")
        memo_path = str(tmp_path / "memo.md")
        gate_path = str(tmp_path / "gate.json")
        _write_text(checklist_path, PARTIAL_CHECKLIST)
        _write_text(memo_path, MEMO_TEXT)
        _write_json(gate_path, _gate())

        result = sync_tool.sync(checklist_path=checklist_path, memo_path=memo_path,
                                gate_path=gate_path, write=True)
        assert result["ok"] is False
        gate = json.load(open(gate_path, encoding="utf-8"))
        assert gate["checklist"]["linear_protective_stop_verified"]["complete"] is False

    def test_blank_signoff_refuses(self, tmp_path):
        checklist_path = str(tmp_path / "checklist.md")
        memo_path = str(tmp_path / "memo.md")
        gate_path = str(tmp_path / "gate.json")
        _write_text(checklist_path, UNSIGNED_CHECKLIST)
        _write_text(memo_path, MEMO_TEXT)
        _write_json(gate_path, _gate())

        result = sync_tool.sync(checklist_path=checklist_path, memo_path=memo_path,
                                gate_path=gate_path, write=True)
        assert result["ok"] is False
        assert "Sign-off" in result["reason"]

    def test_missing_checklist_refuses(self, tmp_path):
        memo_path = str(tmp_path / "memo.md")
        gate_path = str(tmp_path / "gate.json")
        _write_text(memo_path, MEMO_TEXT)
        _write_json(gate_path, _gate())
        missing = str(tmp_path / "no_such_checklist.md")

        result = sync_tool.sync(checklist_path=missing, memo_path=memo_path,
                                gate_path=gate_path, write=True)
        assert result["ok"] is False

    def test_missing_memo_refuses(self, tmp_path):
        checklist_path = str(tmp_path / "checklist.md")
        gate_path = str(tmp_path / "gate.json")
        _write_text(checklist_path, SIGNED_CHECKLIST)
        _write_json(gate_path, _gate())
        missing_memo = str(tmp_path / "no_such_memo.md")

        result = sync_tool.sync(checklist_path=checklist_path, memo_path=missing_memo,
                                gate_path=gate_path, write=True)
        assert result["ok"] is False

    def test_checklist_missing_the_item_1_artifact_reference_refuses(self, tmp_path):
        checklist_path = str(tmp_path / "checklist.md")
        memo_path = str(tmp_path / "memo.md")
        gate_path = str(tmp_path / "gate.json")
        no_artifact = SIGNED_CHECKLIST.replace(
            "bot/artifacts/linear_protective_stop_venue.json", "(see transcript)")
        _write_text(checklist_path, no_artifact)
        _write_text(memo_path, MEMO_TEXT)
        _write_json(gate_path, _gate())

        result = sync_tool.sync(checklist_path=checklist_path, memo_path=memo_path,
                                gate_path=gate_path, write=True)
        assert result["ok"] is False
        assert "linear_protective_stop_venue.json" in result["reason"]

    def test_missing_gate_file_refuses(self, tmp_path):
        checklist_path = str(tmp_path / "checklist.md")
        memo_path = str(tmp_path / "memo.md")
        artifacts_dir = str(tmp_path / "artifacts")
        _write_text(checklist_path, SIGNED_CHECKLIST)
        _write_text(memo_path, MEMO_TEXT)
        _write_all_known_artifacts(artifacts_dir)
        missing_gate = str(tmp_path / "no_such_gate.json")

        result = sync_tool.sync(checklist_path=checklist_path, memo_path=memo_path,
                                gate_path=missing_gate, artifacts_dir=artifacts_dir,
                                write=True)
        assert result["ok"] is False

    def test_gate_without_the_checklist_item_refuses(self, tmp_path):
        checklist_path = str(tmp_path / "checklist.md")
        memo_path = str(tmp_path / "memo.md")
        gate_path = str(tmp_path / "gate.json")
        artifacts_dir = str(tmp_path / "artifacts")
        _write_text(checklist_path, SIGNED_CHECKLIST)
        _write_text(memo_path, MEMO_TEXT)
        _write_json(gate_path, {"checklist": {}})
        _write_all_known_artifacts(artifacts_dir)

        result = sync_tool.sync(checklist_path=checklist_path, memo_path=memo_path,
                                gate_path=gate_path, artifacts_dir=artifacts_dir,
                                write=True)
        assert result["ok"] is False

    def test_cli_exits_one_when_refused(self, tmp_path):
        memo_path = str(tmp_path / "memo.md")
        gate_path = str(tmp_path / "gate.json")
        _write_text(memo_path, MEMO_TEXT)
        _write_json(gate_path, _gate())
        missing = str(tmp_path / "no_such_checklist.md")

        rc = sync_tool.main(["--checklist", missing, "--memo", memo_path,
                            "--gate", gate_path, "--write"])
        assert rc == 1


class TestNeverTouchesADifferentHumanItemOrLiveArming:
    def test_source_contains_no_live_arming_or_forgery_calls(self):
        with open(TOOL_PATH, encoding="utf-8") as handle:
            source = handle.read()
        for banned in ("live_authorized =", "live_authorized=True",
                       "clear_kill_switch", "trip_kill_switch",
                       "place_order", "place_market_order",
                       "place_limit_order", "revoke_cleared_edge"):
            assert banned not in source, f"tool source contains {banned!r}"

    def test_ast_calls_no_write_or_arm_api(self):
        with open(TOOL_PATH, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=TOOL_PATH)
        banned = {"clear_kill_switch_by_human", "trip_kill_switch",
                  "revoke_cleared_edge", "place_order", "place_market_order",
                  "place_limit_order"}
        called = {
            node.func.attr if isinstance(node.func, ast.Attribute)
            else getattr(node.func, "id", None)
            for node in ast.walk(tree) if isinstance(node, ast.Call)
        }
        assert not (called & banned), f"calls a live-affecting API: {called & banned}"

    def test_never_writes_human_risk_memo_signed_or_m4(self, tmp_path):
        checklist_path = str(tmp_path / "checklist.md")
        memo_path = str(tmp_path / "memo.md")
        gate_path = str(tmp_path / "gate.json")
        artifacts_dir = str(tmp_path / "artifacts")
        _write_text(checklist_path, SIGNED_CHECKLIST)
        _write_text(memo_path, MEMO_TEXT)
        original = _gate()
        _write_json(gate_path, original)
        _write_all_known_artifacts(artifacts_dir)
        before_memo = copy.deepcopy(original["checklist"]["human_risk_memo_signed"])
        before_m4 = copy.deepcopy(
            original["checklist"]["m4_recent_half_accepted_or_recovered"])

        result = sync_tool.sync(checklist_path=checklist_path, memo_path=memo_path,
                                gate_path=gate_path, artifacts_dir=artifacts_dir,
                                write=True)
        assert result["written"] is True

        gate = json.load(open(gate_path, encoding="utf-8"))
        assert gate["checklist"]["human_risk_memo_signed"] == before_memo
        assert gate["checklist"]["m4_recent_half_accepted_or_recovered"] == before_m4

    def test_gate_json_has_no_allows_live_key_before_or_after(self, tmp_path):
        checklist_path = str(tmp_path / "checklist.md")
        memo_path = str(tmp_path / "memo.md")
        gate_path = str(tmp_path / "gate.json")
        artifacts_dir = str(tmp_path / "artifacts")
        _write_text(checklist_path, SIGNED_CHECKLIST)
        _write_text(memo_path, MEMO_TEXT)
        _write_json(gate_path, _gate())
        _write_all_known_artifacts(artifacts_dir)

        result = sync_tool.sync(checklist_path=checklist_path, memo_path=memo_path,
                                gate_path=gate_path, artifacts_dir=artifacts_dir,
                                write=True)
        assert result["written"] is True

        gate = json.load(open(gate_path, encoding="utf-8"))
        assert "allows_live" not in gate
        assert "live_authorized" not in gate


class TestAppliedToTheRealRepoFiles:
    """The actual, committed checklist + memo, read straight - proves the
    regexes work against the real prose, not only a hand-built fixture.

    The checklist and the gate file were both found, on audit, to disagree
    with reality: the checklist ticked all four venue items and cited eight
    `bot/artifacts/linear_*.json` transcripts, and the gate JSON had been
    synced to `complete: true` from that signing — but none of the eight
    cited files were ever committed to this repository. There is no history
    for them at all (`git log --all` on each path is empty). The claim was
    typed, not backed. The checklist has been reverted to its blank template
    state and the gate item back to `complete: false` until real venue
    evidence exists; these tests now assert that truthful (refusing) state
    instead of the fabricated one."""

    def test_the_real_checklist_is_blank_and_evaluate_refuses(self):
        result = sync_tool.evaluate()
        assert result["ok"] is False
        assert "not all four venue items are [x]" in result["reason"]

    def test_the_real_gate_item_reads_incomplete(self):
        gate = json.load(open(sync_tool.DEFAULT_GATE, encoding="utf-8"))
        item = gate["checklist"]["linear_protective_stop_verified"]
        assert item["complete"] is False
        assert "signature" not in item

    def test_none_of_the_eight_cited_artifacts_exist_on_disk(self):
        """The audit finding, pinned as a regression test: if any of these
        ever reappear as real, committed venue transcripts, this test (and
        the checklist) should be updated together - not one without the
        other."""
        for name in sync_tool.KNOWN_ARTIFACTS:
            path = os.path.join(sync_tool.DEFAULT_ARTIFACTS_DIR, name)
            assert not os.path.isfile(path), (
                f"{name} now exists on disk - if this is genuine venue "
                "evidence, re-sign the checklist and re-run "
                "tools/sync_linear_stop_gate_evidence.py --write; do not "
                "just delete this assertion")

    def test_a_write_against_the_real_tree_refuses_and_changes_nothing(self):
        before = open(sync_tool.DEFAULT_GATE, encoding="utf-8").read()
        result = sync_tool.sync(write=True)
        assert result["ok"] is False
        assert open(sync_tool.DEFAULT_GATE, encoding="utf-8").read() == before
