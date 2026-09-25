"""micro_live_preflight.py: a read-only echo of promotion_gate, nothing more.

It must never be the thing that decides live is okay - `promotion_gate`
already is that. This file checks the preflight tool agrees with the gate,
writes nothing, and refuses whenever the gate refuses.
"""
from __future__ import annotations

import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
TOOL_PATH = os.path.join(BOT, "tools", "micro_live_preflight.py")

sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, BOT)

import micro_live_preflight as mlp  # noqa: E402
import promotion_gate as pg  # noqa: E402


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
