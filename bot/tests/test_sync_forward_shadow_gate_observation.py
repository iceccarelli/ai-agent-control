"""sync_forward_shadow_gate_observation.py: the gate's forward_shadow_clean
counters track the installed shadow artefact, and nothing else moves.

Dry-run by default, --write required to persist, refuses rather than guesses
when the shadow or gate cannot be read, and never touches a human-owned
checklist item or anything live-arming.
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
TOOL_PATH = os.path.join(BOT, "tools", "sync_forward_shadow_gate_observation.py")

sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, BOT)

import sync_forward_shadow_gate_observation as sync_tool  # noqa: E402


def _write(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def _shadow(*, trades: int, bars: int) -> dict:
    return {
        "forward_n_trades": trades,
        "ceiling": {"closed_forward_bars": bars},
        "forward_window": {"of_which_closed": bars},
    }


def _gate(*, trades: int, days: int, complete: bool = False) -> dict:
    return {
        "checklist": {
            "forward_shadow_clean": {
                "complete": complete,
                "owner": "observation",
                "evidence": (
                    f"{trades} of 20 forward closed trades; {days} of 180 "
                    f"forward days, scored against the APPEND-ONLY full "
                    f"corpora (artifacts/forward_shadow_current.json). "
                    f"Ladder 8/8/8/4/4/3. "
                    f"NOT complete: {trades} < 20 and {days} < 180, and the "
                    f"monitors need 10 and 20 closed trades before they arm."
                ),
                "forward_trades_to_date": trades,
                "forward_days_to_date": days,
                "min_forward_trades": 20,
                "min_forward_days": 180,
            },
            "human_risk_memo_signed": {
                "complete": False, "owner": "human",
                "evidence": "no memo path recorded",
            },
            "kill_switch_drill_recorded": {
                "complete": True, "owner": "human",
                "evidence": "drill transcript ok",
                "signature": {"witnessed_by": "Vincenzo Ceccarelli"},
            },
        },
    }


class TestDesyncedFixture:
    """A) 3 trades / 46 bars shadow against a gate still saying 36 days."""

    def test_dry_run_prints_the_change_and_writes_nothing(self, tmp_path):
        shadow_path = str(tmp_path / "shadow.json")
        gate_path = str(tmp_path / "gate.json")
        _write(shadow_path, _shadow(trades=3, bars=46))
        _write(gate_path, _gate(trades=3, days=36))
        before = gate_path and open(gate_path, encoding="utf-8").read()

        result = sync_tool.sync(shadow_path=shadow_path, gate_path=gate_path,
                                write=False)

        assert result["ok"] is True
        assert result["changed"] is True
        assert result["written"] is False
        assert open(gate_path, encoding="utf-8").read() == before

    def test_write_updates_days_and_leaves_complete_false(self, tmp_path):
        shadow_path = str(tmp_path / "shadow.json")
        gate_path = str(tmp_path / "gate.json")
        _write(shadow_path, _shadow(trades=3, bars=46))
        _write(gate_path, _gate(trades=3, days=36))

        result = sync_tool.sync(shadow_path=shadow_path, gate_path=gate_path,
                                write=True)
        assert result["written"] is True

        gate = json.load(open(gate_path, encoding="utf-8"))
        item = gate["checklist"]["forward_shadow_clean"]
        assert item["forward_trades_to_date"] == 3
        assert item["forward_days_to_date"] == 46
        assert item["complete"] is False
        assert "46 of 180" in item["evidence"]

    def test_cli_exits_zero_on_desync(self, tmp_path, capsys):
        shadow_path = str(tmp_path / "shadow.json")
        gate_path = str(tmp_path / "gate.json")
        _write(shadow_path, _shadow(trades=3, bars=46))
        _write(gate_path, _gate(trades=3, days=36))

        rc = sync_tool.main(["--shadow", shadow_path, "--gate", gate_path])
        assert rc == 0
        assert "46" in capsys.readouterr().out


class TestThresholdCrossing:
    """B) 20 trades / 180 bars: complete flips true, and ONLY this item."""

    def test_write_sets_complete_true_for_forward_shadow_clean_only(self, tmp_path):
        shadow_path = str(tmp_path / "shadow.json")
        gate_path = str(tmp_path / "gate.json")
        _write(shadow_path, _shadow(trades=20, bars=180))
        original_gate = _gate(trades=3, days=46)
        _write(gate_path, original_gate)

        result = sync_tool.sync(shadow_path=shadow_path, gate_path=gate_path,
                                write=True)
        assert result["new"]["complete"] is True

        gate = json.load(open(gate_path, encoding="utf-8"))
        item = gate["checklist"]["forward_shadow_clean"]
        assert item["complete"] is True
        assert item["forward_trades_to_date"] == 20
        assert item["forward_days_to_date"] == 180
        assert "COMPLETE" in item["evidence"]

        # Every other checklist key is byte-identical to what was written.
        for key in ("human_risk_memo_signed", "kill_switch_drill_recorded"):
            assert gate["checklist"][key] == original_gate["checklist"][key]

    def test_below_either_threshold_alone_stays_incomplete(self, tmp_path):
        shadow_path = str(tmp_path / "shadow.json")
        gate_path = str(tmp_path / "gate.json")
        _write(shadow_path, _shadow(trades=20, bars=179))
        _write(gate_path, _gate(trades=3, days=46))

        result = sync_tool.sync(shadow_path=shadow_path, gate_path=gate_path,
                                write=False)
        assert result["new"]["complete"] is False


class TestRefusesRatherThanGuesses:
    """C) Missing/unreadable inputs refuse; nothing is written."""

    def test_missing_shadow_file_refuses(self, tmp_path):
        gate_path = str(tmp_path / "gate.json")
        _write(gate_path, _gate(trades=3, days=36))
        missing_shadow = str(tmp_path / "no_such_shadow.json")

        result = sync_tool.sync(shadow_path=missing_shadow, gate_path=gate_path,
                                write=True)
        assert result["ok"] is False
        assert json.load(open(gate_path, encoding="utf-8")) == _gate(trades=3, days=36)

    def test_missing_shadow_file_cli_exits_one(self, tmp_path):
        gate_path = str(tmp_path / "gate.json")
        _write(gate_path, _gate(trades=3, days=36))
        missing_shadow = str(tmp_path / "no_such_shadow.json")

        rc = sync_tool.main(["--shadow", missing_shadow, "--gate", gate_path,
                            "--write"])
        assert rc == 1

    def test_unreadable_shadow_json_refuses(self, tmp_path):
        shadow_path = str(tmp_path / "shadow.json")
        gate_path = str(tmp_path / "gate.json")
        with open(shadow_path, "w", encoding="utf-8") as handle:
            handle.write("{not valid json")
        _write(gate_path, _gate(trades=3, days=36))

        result = sync_tool.sync(shadow_path=shadow_path, gate_path=gate_path,
                                write=True)
        assert result["ok"] is False

    def test_shadow_missing_forward_n_trades_refuses(self, tmp_path):
        shadow_path = str(tmp_path / "shadow.json")
        gate_path = str(tmp_path / "gate.json")
        _write(shadow_path, {"ceiling": {"closed_forward_bars": 46}})
        _write(gate_path, _gate(trades=3, days=36))

        result = sync_tool.sync(shadow_path=shadow_path, gate_path=gate_path,
                                write=True)
        assert result["ok"] is False

    def test_missing_gate_file_refuses(self, tmp_path):
        shadow_path = str(tmp_path / "shadow.json")
        _write(shadow_path, _shadow(trades=3, bars=46))
        missing_gate = str(tmp_path / "no_such_gate.json")

        result = sync_tool.sync(shadow_path=shadow_path, gate_path=missing_gate,
                                write=True)
        assert result["ok"] is False

    def test_gate_without_the_checklist_item_refuses(self, tmp_path):
        shadow_path = str(tmp_path / "shadow.json")
        gate_path = str(tmp_path / "gate.json")
        _write(shadow_path, _shadow(trades=3, bars=46))
        _write(gate_path, {"checklist": {}})

        result = sync_tool.sync(shadow_path=shadow_path, gate_path=gate_path,
                                write=True)
        assert result["ok"] is False


class TestNeverTouchesHumanItemsOrLiveArming:
    """D) source guard: no live-affecting API appears in the tool at all."""

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

    def test_human_owned_checklist_items_are_byte_identical_after_write(self, tmp_path):
        shadow_path = str(tmp_path / "shadow.json")
        gate_path = str(tmp_path / "gate.json")
        _write(shadow_path, _shadow(trades=3, bars=46))
        original = _gate(trades=3, days=36)
        _write(gate_path, original)
        before = copy.deepcopy(original["checklist"]["human_risk_memo_signed"])
        before_drill = copy.deepcopy(
            original["checklist"]["kill_switch_drill_recorded"])

        sync_tool.sync(shadow_path=shadow_path, gate_path=gate_path, write=True)

        gate = json.load(open(gate_path, encoding="utf-8"))
        assert gate["checklist"]["human_risk_memo_signed"] == before
        assert gate["checklist"]["kill_switch_drill_recorded"] == before_drill
