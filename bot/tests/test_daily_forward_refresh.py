"""The daily forward report, and the two rules it exists to obey.

WHY THIS FILE EXISTS
====================
`tools/daily_forward_refresh.py` watches a gap nobody was watching: the forward
corpus fell three weeks behind the venue and the pilot's trade count read `0 of
20` for sixteen slices because of it, not because of anything the rule did.

The tool reports that gap daily. It has two hard rules, and both were learned
by breaking them during 0060:

1. **It does not write.** Appending moves `carry_backtest.simulate` — which
   reads the corpora whole, with no date bound — and PHASE1_DECISION.md quotes
   that result as +9.66%/yr n=15. A refresh that serves the pilot silently
   restates a shipped decision figure. Appending is `append_closed_corpus.py`'s
   job, which already exists and is tested; this tool must not become a second
   one.
2. **It never writes into `artifacts/`.** The forward scorer defaults `--out`
   to a committed slice artefact. Three of those were clobbered by exploratory
   runs during 0060 and had to be restored from git. On a nightly timer that
   would happen every night, inside output nobody reads.

Nothing here reaches the network.
"""
from __future__ import annotations

import ast
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import corpus_health as ch                          # noqa: E402
import daily_forward_refresh as dfr                 # noqa: E402

TOOL = os.path.join(REPO, "tools", "daily_forward_refresh.py")


def source():
    with open(TOOL, encoding="utf-8") as handle:
        return handle.read()


class TestItDoesNotWrite:
    def test_run_has_no_write_parameter(self):
        """Not defaulted to False — ABSENT, so no caller can pass one."""
        import inspect
        assert "write" not in inspect.signature(dfr.run).parameters

    def test_the_append_tool_is_only_ever_called_with_write_false(self):
        tree = ast.parse(source())
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", None) == "run"
                 and getattr(getattr(n.func, "value", None), "id", None) == "acc"]
        assert calls, "the append tool is never consulted"
        for call in calls:
            kw = {k.arg: k.value for k in call.keywords}
            assert "write" in kw, "acc.run called without an explicit write"
            assert kw["write"].value is False

    def test_it_does_not_reimplement_appending(self):
        """The duplicate this replaced was 414 lines against an existing 225."""
        text = source()
        for forbidden in ("def append", "gzip.open(path, \"wb\")", "os.replace"):
            assert forbidden not in text, forbidden

    def test_it_names_the_tool_that_does_append(self):
        assert "append_closed_corpus" in source()


class TestItNeverWritesIntoArtifacts:
    def test_the_scorer_is_always_given_an_explicit_out(self):
        tree = ast.parse(source())
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # The argv is a LIST argument, so the flags are nested inside the
            # call. Walking only `node.args` finds nothing and the assertion
            # passes vacuously — which is what the first version of this test
            # did, reporting success while checking not one flag.
            literals = [n.value for n in ast.walk(node)
                        if isinstance(n, ast.Constant)
                        and isinstance(n.value, str)]
            flat = " ".join(str(v) for v in literals)
            if "--observed-at-utc" in flat:
                assert "--out" in flat and "--log" in flat
                found = True
        assert found, "no invocation of the forward scorer was found"

    def test_the_default_scratch_is_a_temporary_directory(self):
        text = source()
        assert "TemporaryDirectory" in text
        assert "default=None" in text


class TestItReportsTheBlockerRatherThanRoutingAroundIt:
    def test_it_checks_the_frozen_baseline(self):
        """The check that would have caught 0060's contamination on day one."""
        verdict = dfr.frozen_baseline_intact()
        assert verdict["expected_pct"] == 9.66
        assert verdict["expected_trades"] == 15
        assert verdict["intact"] is True, (
            f"the frozen baseline reads {verdict['net_annualised_pct']} / "
            f"{verdict['trades']}; something appended to a corpus "
            f"PHASE1_DECISION is pinned to")

    def test_a_broken_baseline_is_its_own_exit_code(self):
        text = source()
        assert "return 3" in text
        assert "NO LONGER REPRODUCES PHASE1_DECISION" in text

    def test_the_history_is_stated_in_the_report_not_just_the_docstring(self):
        """The blocker is RESOLVED, and the report still has to say what it was.

        These two assertions used to check `blocked_because` and "no date
        bound". Both still pass, but only by accident: the first is now a
        substring of `previously_blocked_because` and the second survives in
        prose describing the old defect. An assertion that passes by luck is
        not protecting anything, so it is stated explicitly here.
        """
        text = source()
        assert "previously_blocked_because" in text
        assert "RESOLVED" in text
        assert "FROZEN_SNAPSHOT_CUT" in text

    def test_it_says_what_unblocked_it_and_who_appends(self):
        """`Bound the frozen baseline by` is gone because it was DONE.

        The tool used to name that as the fix somebody else would have to make.
        `carry_backtest` now cuts the frozen read at the snapshot boundary and
        the pilot is scored against the append-only full corpora, so the
        sentence describing the pending fix would be describing the past.

        What must remain is that this tool still does not append — that is
        `append_closed_corpus`'s job, and a reporter that quietly grew a write
        path is the duplication this file exists to prevent.
        """
        text = source()
        assert "FORWARD_DATA_DIR" in text and "full" in text
        assert "append_closed_corpus" in text
        import inspect
        assert "write" not in inspect.signature(dfr.run).parameters


class TestTheFullCorpusHealthIsSurfaced:
    """`corpus_health.py` reads three legs of `_full` (perp/spot/funding),
    but the forward pilot only ever reads FORWARD_DATA_DIR / FORWARD_FUNDING
    — perp and funding. It never reads spot (that feeds the basis leg
    elsewhere, in carry_backtest). append_closed_corpus.run_all keeps
    perp+funding's `_full` trees moving (W1b); append_spot_corpus.run_all
    keeps spot's, run separately.

    So a stale spot_1d is real book-health hygiene, not evidence Stage B is
    stuck — printing a "Stage B blocked" line for a series the pilot never
    reads would be exactly the false alarm this field exists to avoid.
    `stage_b_relevant_problems` is the corpus_health finding list with
    spot_1d's entries filtered out; only THAT list gates the BLOCKER text.
    """

    def test_the_real_full_corpus_is_unhealthy_today(self):
        """Ground truth, not a fixture: spot_1d is still stale on disk after
        the perp/funding catch-up. If this ever goes green on its own,
        something now keeps spot in sync and this premise should be
        revisited, not deleted."""
        report = ch.check(REPO, corpus="full", asset="BTC")
        assert report["healthy"] is False
        assert any("spot_1d" in p and "STALE" in p for p in report["problems"]), \
            report["problems"]

    def test_the_real_stage_b_relevant_problems_are_empty_the_day_after_catchup(self):
        """Ground truth pinned to the corpus's OWN last bar, not the wall
        clock: `corpus_health` marks a series stale the instant a calendar
        day passes with no refresh, so asserting this against real `today`
        would rot on schedule — the exact bug fixed earlier in
        tests/test_reviewer_verdict.py, reintroduced here if pinned wrong.
        Evaluated the morning after perp/funding's own last closed bar
        (2026-09-19 -> today=2026-09-20), the ONLY unhealthy leg is spot,
        which the pilot does not read."""
        import datetime as dt
        health = ch.check(REPO, corpus="full", asset="BTC",
                          today=dt.date(2026, 9, 20))
        relevant = [p for p in health["problems"] if not p.startswith("spot_1d:")]
        assert relevant == [], relevant

    def _fake_behind(self):
        return {"new_closed_bars": [], "new_funding_prints": 0,
                "linear_last_before": "2026-09-15",
                "linear_last_after": "2026-09-15"}

    def test_run_carries_both_fields_with_no_network(self, monkeypatch):
        """Proves the field split is wired into dfr.run()'s output — not a
        claim about today's real corpus state (that's the ground-truth test
        above, pinned to a fixed `today`), so the health check itself is
        mocked here rather than left to the wall clock."""
        class _FakeCH:
            def check(self, *a, **kw):
                return {"corpus": "full", "healthy": False,
                       "problems": ["spot_1d: STALE:5d"]}
        monkeypatch.setattr(dfr.acc, "run", lambda **kw: self._fake_behind())
        monkeypatch.setattr(dfr, "score_forward", lambda *a, **kw: None)
        monkeypatch.setattr(dfr, "ch", _FakeCH())
        report = dfr.run()
        assert report["full_corpus_health"]["corpus"] == "full"
        assert report["full_corpus_health"]["healthy"] is False
        assert report["stage_b_relevant_problems"] == []

    def _run_main(self, monkeypatch, capsys, health):
        """Rebinds `dfr.ch` itself, not `corpus_health.check` — that function
        is shared with `carry_backtest`'s own `import corpus_health` inside
        `frozen_baseline_intact()`, and mutating the real module's attribute
        would break that unrelated call with a fixture shaped for this one."""
        class _FakeCH:
            def check(self, *a, **kw):
                return health
        monkeypatch.setattr(dfr.acc, "run", lambda **kw: self._fake_behind())
        monkeypatch.setattr(dfr, "score_forward", lambda *a, **kw: None)
        monkeypatch.setattr(dfr, "ch", _FakeCH())
        dfr.main([])
        return capsys.readouterr().err

    def test_a_stage_b_relevant_problem_prints_a_blocker(self, monkeypatch, capsys):
        err = self._run_main(monkeypatch, capsys, {
            "healthy": False, "corpus": "full",
            "problems": ["perp_1d: STALE:3d"]})
        assert "BLOCKER" in err and "forward pilot" in err
        assert "NOTE" not in err

    def test_a_spot_only_problem_prints_a_note_not_a_blocker(self, monkeypatch, capsys):
        err = self._run_main(monkeypatch, capsys, {
            "healthy": False, "corpus": "full",
            "problems": ["spot_1d: STALE:5d"]})
        assert "BLOCKER" not in err
        assert "NOTE" in err and "not a Stage B blocker" in err

    def test_a_healthy_full_corpus_prints_neither(self, monkeypatch, capsys):
        err = self._run_main(monkeypatch, capsys, {
            "healthy": True, "corpus": "full", "problems": []})
        assert "BLOCKER" not in err and "NOTE" not in err


class TestTheCronLineIsDocumented:
    def test_the_docstring_carries_a_schedulable_line(self):
        assert "* * *" in dfr.__doc__
        assert "daily_forward_refresh.py" in dfr.__doc__

    def test_it_does_not_schedule_itself_on_the_minute(self):
        """Every job that asks for 'daily' lands on :00. This one does not."""
        line = next(l for l in dfr.__doc__.splitlines() if "* * *" in l)
        assert not line.strip().startswith("0 "), line
