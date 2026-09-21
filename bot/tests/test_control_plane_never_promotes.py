"""control_plane_tick.py must never wire the Builder toward promoting the
forward shadow file, or toward an untracked factory script.

Promotion of artifacts/forward_shadow_current.json is human-only (see
docs/human/NO_GLUE_OPS.md #5-6 and #14, tools/promote_forward_shadow.py).
This pins that the tick's own source, and every mission body it can
generate, never names that tool or invents a second accrual script path.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import control_plane_tick as cpt                    # noqa: E402


def _tick_source() -> str:
    with open(cpt.__file__, encoding="utf-8") as handle:
        return handle.read()


class TestTheTickNeverNamesThePromoteTool:
    def test_the_tick_source_never_mentions_promote_forward_shadow(self):
        assert "promote_forward_shadow" not in _tick_source()

    def test_mission_body_never_tells_the_builder_to_promote(self):
        verdict = {"stage_b": {"forward_n_trades": 3, "of_20": 20,
                                "closed_forward_bars": 41},
                   "allows_progress": True,
                   "next_actions": ["accrue closed forward trades: 3 of 20"],
                   "blockers": []}
        body = cpt._mission_body(verdict, forward=None, allows_live=False)
        assert "promote_forward_shadow" not in body
        assert ("promote scratch forward scores over "
                "forward_shadow_current.json") in body

    def test_mission_body_never_names_an_untracked_accrual_script(self):
        verdict = {"stage_b": {}, "allows_progress": False,
                   "next_actions": [], "blockers": []}
        body = cpt._mission_body(verdict, forward=None, allows_live=False)
        assert "stage_b_forward_accrual" not in body
