"""The four invariants every session ends by quoting, checked by bytes.

The session tail asks: gate still false? FUND_ABS still 0.0001? cap still 100?
live_authorized unassigned? Answering from memory is how a number nobody
watched gets into a commit message. The tool answers from the tree.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import session_tail as st  # noqa: E402

REPO = os.path.join(os.path.dirname(__file__), "..")


class TestOnThisTree:
    def test_every_invariant_holds(self):
        report = st.check(REPO)
        assert report["gate_still_false"] is True
        assert report["fund_abs_still_0_0001"] is True
        assert report["cap_still_100"] is True
        assert report["live_authorized_unassigned"] is True
        assert report["ok"] is True

    def test_the_cli_exits_zero_and_prints_the_tail_labels(self, capsys):
        assert st.main(["--repo", REPO]) == 0
        out = capsys.readouterr().out
        for label in ("GATE STILL FALSE?", "FUND_ABS STILL 0.0001?",
                      "CAP STILL 100?", "LIVE_AUTHORIZED UNASSIGNED?"):
            assert label in out


class TestTheAstScanCatchesAnAssignment:
    def _tree(self, tmp_path, body):
        (tmp_path / "mod.py").write_text(body, encoding="utf-8")
        return str(tmp_path)

    def test_attribute_assignment_is_caught(self, tmp_path):
        root = self._tree(tmp_path, "cfg.live_authorized = True\n")
        assert st.live_authorized_assignments(root)

    def test_upper_case_attribute_is_caught(self, tmp_path):
        root = self._tree(tmp_path, "cfg.LIVE_AUTHORIZED = True\n")
        assert st.live_authorized_assignments(root)

    def test_setattr_is_caught(self, tmp_path):
        root = self._tree(tmp_path, "setattr(cfg, 'live_authorized', True)\n")
        assert st.live_authorized_assignments(root)

    def test_augmented_and_annotated_assignment_are_caught(self, tmp_path):
        root = self._tree(tmp_path, "cfg.live_authorized: bool = True\n"
                                    "cfg.live_authorized |= True\n")
        assert len(st.live_authorized_assignments(root)) == 2

    def test_reading_it_into_a_local_is_not_an_assignment(self, tmp_path):
        root = self._tree(
            tmp_path,
            "live_authorized = bool(getattr(cfg, 'LIVE_AUTHORIZED', False))\n")
        assert st.live_authorized_assignments(root) == []

    def test_tests_are_not_scanned(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "t.py").write_text("c.live_authorized = 1\n",
                                                 encoding="utf-8")
        assert st.live_authorized_assignments(str(tmp_path)) == []


class TestAViolationFailsTheExit:
    def test_a_moved_cap_is_reported_and_fails(self, monkeypatch):
        import shadow
        monkeypatch.setattr(shadow, "SHADOW_MAX_NOTIONAL_USD", 250.0)
        report = st.check(REPO)
        assert report["cap_still_100"] is False
        assert report["ok"] is False
        assert st.main(["--repo", REPO]) != 0

    def test_the_tool_arms_nothing(self):
        with open(os.path.join(REPO, "tools", "session_tail.py"),
                  encoding="utf-8") as fh:
            body = fh.read()
        assert "place_market" not in body
        assert "place_order" not in body
