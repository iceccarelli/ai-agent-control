"""The kill-switch drill, and the forgery it must not be able to commit.

WHY THIS FILE EXISTS
====================
`tools/kill_switch_drill.py` performs the six steps of
`docs/promotion/KILL_SWITCH_DRILL_SCRIPT.md` and records what the system
actually returned at each one. It exists because the script has never been run
and its evidence would otherwise be a terminal scrollback nobody kept.

It is also the single easiest place in this repository to commit the forgery the
promotion gate exists to prevent. `kill_switch_drill_recorded` is one of six
items a HUMAN owns, and the template says it plainly: *"A drill you did not run
is worth nothing, and marking this item complete without running one is a
forgery."* A program that ran the drill for itself and then ticked the box would
be exactly that, dressed as diligence.

So the assertions below are in two groups:

1. **It cannot complete the gate item.** No gate path, no gate write, an
   explicit `gate_item_completed: false`, and a blank signature block.
2. **It cannot leave the switch engaged.** A drill that tripped the real switch
   and walked away would have caused the incident it was rehearsing for.

Nothing here reaches the network or touches the configured state database.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import kill_switch_drill as ksd                     # noqa: E402
import persistence                                  # noqa: E402
import promotion_gate as pg                         # noqa: E402

TOOL = os.path.join(REPO, "tools", "kill_switch_drill.py")


def source():
    with open(TOOL, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture()
def report():
    return ksd.run()


class TestItCannotCompleteTheGateItem:
    def test_the_report_says_so_explicitly(self, report):
        assert report["gate_item"] == "kill_switch_drill_recorded"
        assert report["gate_item_completed"] is False

    def test_the_signature_block_is_blank(self, report):
        assert report["signature"] == {
            "drill_performed_by": "", "witnessed_by": "", "date": ""}

    def test_it_never_writes_the_gate_file(self):
        text = source()
        assert os.path.basename(pg.GATE_PATH) not in text
        assert "promotion_gate" not in text.replace(
            "promotion-gate items", "").replace("promotion gate", "")

    def test_running_it_does_not_move_the_gate(self, report):
        """The load-bearing assertion of this file."""
        verdict = pg.evaluate_promotion_gate()
        assert verdict.allows_live is False
        complete = {i.key for i in verdict.items if i.complete}
        assert "kill_switch_drill_recorded" not in complete
        assert len(complete) == 2

    def test_it_says_why_a_program_cannot_answer_the_question(self, report):
        why = report["why_this_does_not_complete_the_item"]
        assert "HUMAN" in why
        assert "under time pressure" in why


class TestItCannotLeaveTheSwitchEngaged:
    def test_the_switch_is_clear_afterwards(self, tmp_path):
        path = str(tmp_path / "s.db")
        report = ksd.run(state_path=path)
        assert report["switch_restored"] is True
        store = persistence.StateStore(path)
        engaged, _reason = store.is_kill_switch_engaged()
        store.close()
        assert engaged is False

    def test_a_switch_engaged_BEFORE_the_drill_is_left_engaged(self, tmp_path):
        """Restoring means restoring, not clearing. A drill run during a real
        incident must not resume trading as a side effect."""
        path = str(tmp_path / "s.db")
        store = persistence.StateStore(path)
        store.trip_kill_switch("a real incident, not the drill")
        store.close()

        report = ksd.run(state_path=path)
        assert report["switch_engaged_before"] is True

        store = persistence.StateStore(path)
        engaged, reason = store.is_kill_switch_engaged()
        store.close()
        assert engaged is True
        assert "real incident" in reason

    def test_the_default_database_is_disposable(self, report):
        assert report["disposable_database"] is True
        assert "throwaway" in report["database"]

    def test_a_failed_restore_is_its_own_exit_code(self):
        text = source()
        assert "return 2" in text
        assert "COULD NOT BE RESTORED" in text


class TestTheStepsAreObservationsNotCheckmarks:
    def test_every_step_records_what_was_observed(self, report):
        assert report["steps"]
        for step in report["steps"]:
            assert set(step) == {"step", "expected", "observed", "as_scripted"}
            assert step["observed"] not in (None, "", True)

    def test_the_six_scripted_steps_are_present(self, report):
        names = [s["step"] for s in report["steps"]]
        for expected in ("1_baseline", "2_trip", "3_blocks", "3b_health",
                         "4_survives_restart", "5a_wrong_token_refused",
                         "5b_exact_token_clears"):
            assert expected in names, expected

    def test_the_gate_chain_blocks_with_the_named_reason(self, report):
        blocks = next(s for s in report["steps"] if s["step"] == "3_blocks")
        assert blocks["observed"]["gate_reason"] == "KILL_SWITCH_ENGAGED"
        assert blocks["observed"]["should_halt_trading"] is True
        assert blocks["observed"]["can_trade"] is False

    def test_a_wrong_token_raises_and_leaves_it_engaged(self, report):
        step = next(s for s in report["steps"]
                    if s["step"] == "5a_wrong_token_refused")
        assert step["observed"]["still_engaged"] is True
        assert "Error" in step["observed"]["raised"]

    def test_it_survives_a_restart(self, report):
        step = next(s for s in report["steps"]
                    if s["step"] == "4_survives_restart")
        assert step["observed"]["engaged"] is True

    def test_all_steps_behaved_as_scripted(self, report):
        failed = [s["step"] for s in report["steps"] if not s["as_scripted"]]
        assert failed == [], failed


class TestTheTokenIsNotRestated:
    def test_the_exact_token_is_the_one_persistence_demands(self):
        """A copy that drifted would make the drill pass against a door that no
        longer opens."""
        import inspect
        assert ksd.HUMAN_TOKEN in inspect.getsource(
            persistence.StateStore.clear_kill_switch_by_human)

    def test_the_only_automated_clear_is_the_drill_undoing_its_own_trip(self):
        """`clear_kill_switch_by_human` is never called from automated code in
        this codebase — except here, to UNDO the drill's own trip.

        The first version of this test asserted a COUNT of call sites (`<= 3`,
        against an actual 4) which I wrote without counting. A magic number
        there encodes nothing: three calls would be fine and one could be a
        disaster. What matters is the CONDITION guarding the restore — the
        switch is only cleared when the drill itself engaged it — and that is
        asserted behaviourally by
        `TestItCannotLeaveTheSwitchEngaged::test_a_switch_engaged_BEFORE_the_
        drill_is_left_engaged`, which trips the switch first and proves the
        drill leaves it engaged.

        What is left here is the thing a behavioural test cannot check: that
        the justification is written down where the call happens.
        """
        text = source()
        assert "Restore whatever was true before the drill" in text
        assert "was_engaged" in text, (
            "the restore must be conditional on the state BEFORE the drill, "
            "or a drill run during a real incident resumes trading")
