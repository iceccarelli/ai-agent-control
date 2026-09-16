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

    def test_the_blocker_is_stated_in_the_report_not_just_the_docstring(self):
        assert "blocked_because" in source()
        assert "no date bound" in source()

    def test_it_says_what_would_unblock_it(self):
        assert "Bound the frozen baseline by" in source()


class TestTheCronLineIsDocumented:
    def test_the_docstring_carries_a_schedulable_line(self):
        assert "* * *" in dfr.__doc__
        assert "daily_forward_refresh.py" in dfr.__doc__

    def test_it_does_not_schedule_itself_on_the_minute(self):
        """Every job that asks for 'daily' lands on :00. This one does not."""
        line = next(l for l in dfr.__doc__.splitlines() if "* * *" in l)
        assert not line.strip().startswith("0 "), line
