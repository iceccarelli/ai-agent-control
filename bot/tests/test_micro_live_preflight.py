"""micro_live_preflight.py: a read-only echo of promotion_gate, nothing more.

It must never be the thing that decides live is okay - `promotion_gate`
already is that. This file checks the preflight tool agrees with the gate,
writes nothing, and refuses whenever the gate refuses.
"""
from __future__ import annotations

import ast
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
TOOL_PATH = os.path.join(BOT, "tools", "micro_live_preflight.py")
GATE = os.path.join(BOT, "artifacts", "slice59_promotion_gate.json")

sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, BOT)

import micro_live_preflight as mlp  # noqa: E402
import promotion_gate as pg  # noqa: E402


def gate_file() -> dict:
    with open(GATE, encoding="utf-8") as handle:
        return json.load(handle)


class TestAgreesWithTheGate:
    def test_exit_code_matches_the_gate_verdict(self):
        verdict = pg.evaluate_promotion_gate()
        assert mlp.run() == (0 if verdict.allows_live else 1)

    def test_refuses_on_the_current_tree(self):
        # This repo's gate is REFUSE today (documented in
        # docs/PROMOTION_GATE_MICRO_LIVE.md). If this ever flips to READY it
        # must be because a human completed every gate item, never because
        # this test was loosened to match a drifted default.
        assert mlp.run() == 1

    def test_missing_gate_file_refuses_rather_than_crashing(self, tmp_path):
        missing = str(tmp_path / "no_such_gate.json")
        assert mlp.run(path=missing) == 1

    def test_corrupt_gate_json_refuses_rather_than_crashing(self, tmp_path):
        corrupt = tmp_path / "gate.json"
        corrupt.write_text("{not valid json", encoding="utf-8")
        assert mlp.run(path=str(corrupt)) == 1

    def test_incomplete_checklist_refuses_and_names_the_open_human_item(
            self, tmp_path, capsys):
        # The real gate's own checklist, untouched: several human items are
        # still open (human_risk_memo_signed among them).
        path = tmp_path / "gate.json"
        path.write_text(json.dumps(gate_file()), encoding="utf-8")

        rc = mlp.run(path=str(path))
        out = capsys.readouterr().out
        assert rc == 1
        assert "VERDICT: REFUSE" in out
        assert "human_risk_memo_signed" in out

    def test_live_chain_refusal_survives_a_fully_ticked_checklist(
            self, monkeypatch, tmp_path, capsys):
        """Flipping every checklist bool to True is not enough — the gate
        also independently asks config.is_live_authorized, and this proves
        the preflight tool reports that refusal rather than papering over it.
        """
        import config as _config
        payload = gate_file()
        for entry in payload["checklist"].values():
            entry["complete"] = True
        path = tmp_path / "gate.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

        monkeypatch.setattr(
            _config, "is_live_authorized",
            lambda *a, **k: (False, "LIVE_TRADING_ACK absent"))

        rc = mlp.run(path=str(path))
        out = capsys.readouterr().out
        assert rc == 1
        assert "VERDICT: REFUSE" in out
        assert "live-arming chain" in out
        assert "LIVE_TRADING_ACK absent" in out

    def test_the_gate_file_on_disk_is_not_mutated_by_a_preflight_run(self):
        before = open(GATE, encoding="utf-8").read()
        mlp.run()
        after = open(GATE, encoding="utf-8").read()
        assert before == after


class TestNeverWrites:
    def test_the_module_calls_no_write_or_arm_api(self):
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
        assert not (called & banned), f"preflight calls a live-affecting API: {called & banned}"

    def test_run_assigns_no_live_authorized_anywhere_in_the_module(self):
        with open(TOOL_PATH, encoding="utf-8") as handle:
            source = handle.read()
        assert "live_authorized =" not in source
        assert "live_authorized=True" not in source


class TestOutputShape:
    def test_prints_a_verdict_line(self, capsys):
        mlp.run()
        out = capsys.readouterr().out
        assert "VERDICT: READY" in out or "VERDICT: REFUSE" in out

    def test_dry_run_flag_is_accepted_and_changes_nothing(self):
        argv_result = mlp.main(["--dry-run"])
        assert argv_result == mlp.run()
