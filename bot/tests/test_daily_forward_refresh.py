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
    """`append_closed_corpus.py` only ever writes `data/real_linear_1d` and
    `data/real_funding` — the primary, bounded trees. This tool scores the
    pilot against the `_full` trees instead, and nothing appends to those
    automatically, so a cron that faithfully runs `append_closed_corpus.py`
    every day can still leave `_full` frozen while Stage B's counter stops
    moving. `corpus_health.py` already detects exactly this kind of
    staleness; it just was never called from anything that runs on its own.
    This wires it in, read-only.
    """

    def test_the_real_full_corpus_is_unhealthy_today(self):
        """Ground truth, not a fixture: proves the split exists on disk. If
        this ever goes green on its own, something now keeps `_full` in sync
        and this test's premise should be revisited, not deleted."""
        report = ch.check(REPO, corpus="full", asset="BTC")
        assert report["healthy"] is False
        assert any("STALE" in p for p in report["problems"]), report["problems"]

    def _fake_behind(self):
        return {"new_closed_bars": [], "new_funding_prints": 0,
                "linear_last_before": "2026-09-15",
                "linear_last_after": "2026-09-15"}

    def test_run_carries_the_finding_with_no_network(self, monkeypatch):
        monkeypatch.setattr(dfr.acc, "run", lambda **kw: self._fake_behind())
        monkeypatch.setattr(dfr, "score_forward", lambda *a, **kw: None)
        report = dfr.run()
        assert report["full_corpus_health"]["corpus"] == "full"
        assert report["full_corpus_health"]["healthy"] is False

    def test_main_prints_it_as_a_blocker(self, monkeypatch, capsys):
        monkeypatch.setattr(dfr.acc, "run", lambda **kw: self._fake_behind())
        monkeypatch.setattr(dfr, "score_forward", lambda *a, **kw: None)
        rc = dfr.main([])
        assert rc == 0, "unhealthy _full is reported, not fatal on its own"
        err = capsys.readouterr().err
        assert "BLOCKER" in err and "_full corpus" in err

    def test_a_healthy_full_corpus_prints_no_blocker(self, monkeypatch, capsys):
        monkeypatch.setattr(dfr.acc, "run", lambda **kw: self._fake_behind())
        monkeypatch.setattr(dfr, "score_forward", lambda *a, **kw: None)
        monkeypatch.setattr(dfr.ch, "check",
                            lambda *a, **kw: {"healthy": True, "problems": [],
                                             "corpus": "full"})
        dfr.main([])
        assert "BLOCKER" not in capsys.readouterr().err


class TestTheCronLineIsDocumented:
    def test_the_docstring_carries_a_schedulable_line(self):
        assert "* * *" in dfr.__doc__
        assert "daily_forward_refresh.py" in dfr.__doc__

    def test_it_does_not_schedule_itself_on_the_minute(self):
        """Every job that asks for 'daily' lands on :00. This one does not."""
        line = next(l for l in dfr.__doc__.splitlines() if "* * *" in l)
        assert not line.strip().startswith("0 "), line
