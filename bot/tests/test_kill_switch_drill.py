"""The kill-switch drill: the live path, and the forgery it must not commit.

WHY THIS FILE EXISTS
====================
`tools/kill_switch_drill.py` performs the six steps of
`docs/promotion/KILL_SWITCH_DRILL_SCRIPT.md` and records what the system
actually returned at each one. It exists because the script had never been run
and its evidence would otherwise be a terminal scrollback nobody kept.

THREE PROPERTIES, AND ALL THREE WERE BROKEN AT SOME POINT
=========================================================
1. **It must hit the LIVE path.** An earlier version of this drill assembled
   its own health dict and asserted against that. It passed, and it proved
   nothing about the endpoint an orchestrator polls. The drill now calls the
   real `TradingBot.health()` and fetches the real `HTTPServer` the bot starts
   itself, so the `200 if healthy else 503` mapping is never recomputed.
2. **It must not complete the gate item.** `kill_switch_drill_recorded` is one
   of six items a HUMAN owns, and the template says it plainly: *"A drill you
   did not run is worth nothing, and marking this item complete without running
   one is a forgery."*
3. **It must not leave the switch engaged**, and must not touch the production
   database.

WHAT THIS FILE DOES NOT DO
==========================
It does not re-prove what is already covered elsewhere. `KILL_SWITCH_ENGAGED`
is asserted in eight other test files, and
`test_orchestrator.py::test_health_endpoint_returns_503_when_unhealthy` already
proves the endpoint's status mapping. This file asserts that the DRILL exercises
those paths and records what they returned — the transcript, not the mechanism.

Nothing here reaches the network or opens the configured state database.
"""
from __future__ import annotations

import hashlib
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


def step(report, name):
    return next(s for s in report["steps"] if s["step"] == name)


@pytest.fixture(scope="module")
def report():
    """One drill run, shared. It writes nothing outside a temporary directory."""
    return ksd.run()


# ---------------------------------------------------------------------------
# 1 — it hits the live path
# ---------------------------------------------------------------------------


class TestItExercisesTheLivePath:
    def test_the_gate_chain_is_the_one_the_engine_calls(self, report):
        """`gate_order` is what `trading_engine` calls before every entry."""
        blocked = step(report, "4_entries_blocked")["observed"]
        assert blocked["gate_reason"] == "KILL_SWITCH_ENGAGED"
        assert blocked["gate_ok"] is False
        assert blocked["should_halt_trading"] is True
        assert blocked["can_trade"] is False

    def test_the_503_comes_from_a_real_server(self, report):
        """Not from recomputing `200 if healthy else 503` in the drill."""
        after = step(report, "5_health_unhealthy_503")["observed"]
        assert after["http_status"] == 503
        assert after["healthy"] is False
        assert after["kill_switch"] is True

    def test_health_flips_and_the_switch_is_the_attributable_cause(self, report):
        """`health()` is also unhealthy on a naked position or before
        reconciliation, so a single unhealthy reading proves nothing. The drill
        records BOTH sides with the other two causes pinned."""
        before = step(report, "2_health_before_trip")["observed"]
        after = step(report, "5_health_unhealthy_503")["observed"]
        assert before["healthy"] is True and before["http_status"] == 200
        assert before["naked_positions"] == []
        assert before["reconciled"] is True
        assert after["healthy"] is False and after["http_status"] == 503

    def test_clearing_restores_health_over_the_same_endpoint(self, report):
        cleared = step(report, "7b_exact_token_clears")["observed"]
        assert cleared["engaged_after_clear"] is False
        assert cleared["healthy"] is True
        assert cleared["http_status"] == 200

    def test_it_does_not_reimplement_the_status_mapping(self):
        """Checked against CODE, not prose.

        The first version of this test asserted the phrase
        `200 if healthy else 503` was absent from the file — and the docstring
        explaining that the mapping is never recomputed contains that phrase,
        so the test failed on its own explanation. A substring scan cannot tell
        an implementation from a description of one. This walks the AST for the
        ternary itself, which can only exist in executable code.
        """
        import ast
        tree = ast.parse(source())

        # (a) the drill never CONSTRUCTS a status from a condition.
        for node in ast.walk(tree):
            if not isinstance(node, ast.IfExp):
                continue
            branches = {getattr(node.body, "value", None),
                        getattr(node.orelse, "value", None)}
            assert not ({200, 503} & branches), (
                f"the drill computes an HTTP status itself: {ast.dump(node)}")

        # A THIRD CHECK WAS TRIED HERE AND WAS WRONG, which is worth recording
        # because it is the same mistake as the substring scan above wearing a
        # different hat. It forbade the integer 503 anywhere in executable
        # code — and the drill's own step-5 predicate is
        # `after_http["status"] == 503`, which is the drill CHECKING the status
        # it read. Banning the literal would have stopped the drill verifying
        # its own observation. Comparing a value you read against the value you
        # expect is not reimplementing the mapping; only deriving it is.
        #
        # (b) it READS the status off the response and the HTTPError, which is
        #     the only way it can legitimately obtain one.
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert "code" in attrs, "never reads HTTPError.code"
        assert "status" in attrs, "never reads response.status"

    def test_the_apis_it_claims_are_the_ones_it_calls(self, report):
        text = source()
        for api in report["apis_exercised"]:
            leaf = api.split(" -> ")[0].split(".")[-1]
            assert leaf in text, api


# ---------------------------------------------------------------------------
# 2 — it cannot complete the gate item
# ---------------------------------------------------------------------------


class TestItCannotCompleteTheGateItem:
    def test_the_report_says_so_explicitly(self, report):
        assert report["gate_item"] == "kill_switch_drill_recorded"
        assert report["gate_item_completed"] is False

    def test_the_signature_block_is_blank(self, report):
        assert report["signature"] == {
            "drill_performed_by": "", "witnessed_by": "", "date": ""}

    def test_it_never_writes_the_gate_file(self):
        assert os.path.basename(pg.GATE_PATH) not in source()

    def test_running_it_does_not_move_the_gate(self, report):
        """The load-bearing assertion of this file."""
        verdict = pg.evaluate_promotion_gate()
        assert verdict.allows_live is False
        complete = {i.key for i in verdict.items if i.complete}
        assert "kill_switch_drill_recorded" not in complete
        assert len(complete) == 2

    def test_it_says_why_a_program_cannot_answer_the_question(self, report):
        why = report["why_this_does_not_complete_the_item"]
        assert "HUMAN" in why and "under time pressure" in why


# ---------------------------------------------------------------------------
# 3 — it cannot leave the switch engaged, or touch production
# ---------------------------------------------------------------------------


class TestItCannotLeaveTheSwitchEngaged:
    def test_the_switch_is_disengaged_at_exit(self, report):
        assert report["switch_restored"] is True
        assert report["switch_engaged_at_exit"] is False

    def test_the_switch_is_clear_in_the_database_afterwards(self, tmp_path):
        path = str(tmp_path / "s.db")
        ksd.run(state_path=path)
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

        out = ksd.run(state_path=path)
        assert out["switch_engaged_before"] is True

        store = persistence.StateStore(path)
        engaged, reason = store.is_kill_switch_engaged()
        store.close()
        assert engaged is True
        assert "real incident" in reason

    def test_a_failed_restore_is_its_own_exit_code(self):
        text = source()
        assert "return 2" in text
        assert "COULD NOT BE RESTORED" in text

    def test_the_restore_is_conditional_on_the_state_before(self):
        """The condition is the property; a count of call sites is not."""
        text = source()
        assert "Restore whatever was true before the drill" in text
        assert "was_engaged" in text


class TestItDoesNotTouchProduction:
    def test_the_default_database_is_disposable(self, report):
        assert report["disposable_database"] is True
        assert "throwaway" in report["database"]

    def test_the_production_database_is_byte_identical(self, report):
        assert report["production_db_untouched"] is True
        observed = step(report, "8_disengaged_and_production_untouched")[
            "observed"]
        assert observed["production_sha256_before"] == \
            observed["production_sha256_after"]

    def test_a_run_leaves_the_real_production_database_byte_identical(self):
        """The invariant, asserted directly. NO SKIP.

        The first version of this test skipped with "no production database in
        this checkout" while the file plainly existed. That predicate was
        wrong twice over: it checked the path the DRILL resolved (which
        `conftest.py` deliberately redirects into the test tree) rather than
        the real one, and it treated a deliberately hidden path as a missing
        file. A skip on this surface hides exactly the property the drill
        exists to establish.

        So this ignores the environment entirely, hashes the REAL production
        path, runs the drill, and hashes it again. A checkout with no
        production database yields None on both sides and still asserts
        equality — there is nothing to skip for.
        """
        real = os.path.join(REPO, "state", "trading_state.db")

        def digest():
            if not os.path.exists(real):
                return None
            with open(real, "rb") as handle:
                return hashlib.sha256(handle.read()).hexdigest()

        before = digest()
        ksd.run()
        assert digest() == before, (
            "a drill run changed the production database")

    def test_the_suite_is_structurally_pointed_away_from_production(self):
        """Why the drill under pytest never resolves the production path.

        `tests/conftest.py` sets `STATE_DB_PATH` into the test tree before
        anything imports `persistence`. That is a safety property worth
        asserting rather than a reason to skip: it is what makes running the
        drill inside the suite incapable of touching production state.
        """
        redirected = os.environ.get("STATE_DB_PATH", "")
        assert redirected, "conftest did not set STATE_DB_PATH"
        assert os.path.abspath(redirected) != os.path.abspath(
            os.path.join(REPO, "state", "trading_state.db")), (
                "the suite is pointed AT the production database")

    def test_the_reported_hash_matches_whatever_path_it_resolved(self, report):
        """A self-reported 'untouched' is worth nothing if the hash is fiction.

        Whatever path the drill resolved — scratch under pytest, production
        from a shell — the digest it published must be that file's real digest,
        or None when the file is absent. No skip: both branches assert.
        """
        observed = step(report, "8_disengaged_and_production_untouched")[
            "observed"]
        path = os.path.join(REPO, observed["production_db"])
        if os.path.exists(path):
            with open(path, "rb") as handle:
                actual = hashlib.sha256(handle.read()).hexdigest()
        else:
            actual = None
        assert actual == observed["production_sha256_after"]
        assert observed["production_sha256_before"] == \
            observed["production_sha256_after"]


# ---------------------------------------------------------------------------
# 4 — the transcript is observations, not checkmarks
# ---------------------------------------------------------------------------


class TestTheStepsAreObservations:
    def test_every_step_records_what_was_observed(self, report):
        assert report["steps"]
        for entry in report["steps"]:
            assert set(entry) == {"step", "expected", "observed", "as_scripted"}
            assert isinstance(entry["observed"], dict) and entry["observed"]

    def test_the_scripted_steps_are_all_present(self, report):
        names = [s["step"] for s in report["steps"]]
        for expected in ("1_baseline", "2_health_before_trip", "3_trip",
                         "4_entries_blocked", "5_health_unhealthy_503",
                         "6_survives_restart", "7a_wrong_token_refused",
                         "7b_exact_token_clears",
                         "8_disengaged_and_production_untouched"):
            assert expected in names, expected

    def test_a_wrong_token_raises_and_leaves_it_engaged(self, report):
        observed = step(report, "7a_wrong_token_refused")["observed"]
        assert observed["still_engaged"] is True
        assert "Error" in observed["raised"]

    def test_it_survives_a_restart(self, report):
        assert step(report, "6_survives_restart")["observed"]["engaged"] is True

    def test_all_steps_behaved_as_scripted(self, report):
        failed = [s["step"] for s in report["steps"] if not s["as_scripted"]]
        assert failed == [], failed


class TestTheTokenIsNotRestated:
    def test_the_exact_token_is_the_one_persistence_demands(self):
        import inspect
        assert ksd.HUMAN_TOKEN in inspect.getsource(
            persistence.StateStore.clear_kill_switch_by_human)
