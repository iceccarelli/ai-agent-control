"""Static contract for bot/scripts/stage_b_forward_accrual.sh.

This script requires real venue egress (append_closed_corpus.py --write,
append_spot_corpus.py --write) and is never executed here — see
docs/human/NO_GLUE_OPS.md #9. Everything below is a parse/grep-level check
of the script text, not a run of it.
"""
from __future__ import annotations

import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_PATH = os.path.join(REPO, "scripts", "stage_b_forward_accrual.sh")
REPO_ROOT = os.path.dirname(REPO)
WORKFLOW_PATH = os.path.join(
    REPO_ROOT, ".github", "workflows", "stage-b-forward-accrual.yml")


def _lines():
    with open(SCRIPT_PATH, encoding="utf-8") as handle:
        return handle.readlines()


def _text():
    return "".join(_lines())


def _first_line_containing(needle: str) -> int:
    for i, line in enumerate(_lines()):
        if needle in line and not line.lstrip().startswith("#"):
            return i
    raise AssertionError(f"{needle!r} not found as live (non-comment) code")


class TestCallOrder:
    """append_closed -> append_spot -> refresh, in that order, uncommented."""

    def test_append_closed_corpus_precedes_append_spot_corpus(self):
        closed = _first_line_containing("tools/append_closed_corpus.py --write")
        spot = _first_line_containing("tools/append_spot_corpus.py --write")
        assert closed < spot

    def test_append_spot_corpus_precedes_daily_forward_refresh(self):
        spot = _first_line_containing("tools/append_spot_corpus.py --write")
        refresh = _first_line_containing("tools/daily_forward_refresh.py")
        assert spot < refresh

    def test_set_euo_pipefail_precedes_all_three_calls(self):
        lines = _lines()
        set_line = next(i for i, l in enumerate(lines)
                        if l.strip() == "set -euo pipefail")
        closed = _first_line_containing("tools/append_closed_corpus.py --write")
        assert set_line < closed


class TestForbiddenPatterns:
    def test_never_invokes_the_promote_tool(self):
        """The tool name may appear in a comment or a printed hint pointing
        a human at it, but no live (non-comment, non-print-string) line may
        actually invoke it."""
        for line in _lines():
            if "promote_forward_shadow" not in line:
                continue
            stripped = line.strip()
            assert stripped.startswith("#") or '"' in stripped, (
                f"looks like a live invocation, not a comment/string: {line!r}")
        assert '"$PY" tools/promote_forward_shadow.py' not in _text()
        assert "$PY tools/promote_forward_shadow.py" not in _text()

    def test_never_opens_the_shadow_file_for_writing(self):
        text = _text()
        # A read-only open (mode "r") is fine and expected; a write mode is not.
        assert re.search(r'open\(\s*shadow_path\s*,\s*["\']w', text) is None
        assert "> artifacts/forward_shadow_current.json" not in text
        assert ">>artifacts/forward_shadow_current.json" not in text

    def test_never_commits_or_pushes(self):
        text = _text()
        for line in text.splitlines():
            body = line.strip().lstrip('"\'(')
            assert not body.startswith(("git commit", "git push")), line

    def test_never_places_an_order_or_sets_allows_live(self):
        text = _text()
        for banned in ("place_order", "allows_live = True", "allows_live=True",
                       "--allows-live"):
            assert banned not in text

    def test_commented_out_settlement_and_borrow_fetchers_stay_commented(self):
        """NO_GLUE_OPS.md #7: these are not part of the Stage B daily path.
        If ever uncommented, the script's own header requires `|| true` so a
        fetch failure there can never abort the money path above it."""
        for line in _lines():
            stripped = line.strip()
            if "fetch_settlement_klines.py" in stripped or \
               "fetch_borrow_rates.py" in stripped:
                assert stripped.startswith("#"), (
                    f"uncommented fetch call must be wrapped in `|| true`: "
                    f"{stripped!r}")


class TestNoSecondDualTreeHandCall:
    """append_closed_corpus.py / append_spot_corpus.py already sync PRIMARY +
    _full atomically in one call — the script must not hand-call either tool
    a second time for the other tree."""

    def test_append_closed_corpus_is_called_exactly_once(self):
        text = _text()
        assert text.count("tools/append_closed_corpus.py") == 1

    def test_append_spot_corpus_is_called_exactly_once(self):
        text = _text()
        assert text.count("tools/append_spot_corpus.py") == 1


class TestGithubActionsIsNotACompetingScheduledWriter:
    """docs/human/NO_GLUE_OPS.md item 10 names the Factory Mac cron as the
    one canonical SCHEDULED corpus writer. .github/workflows/
    stage-b-forward-accrual.yml runs this identical script and used to also
    carry a `schedule:` trigger (~1 hour from the Factory Mac's own cron)
    plus its own commit+push step — two unsupervised writers racing to
    append the same dual-tree corpus with no ownership check between them.

    It never actually collided (this workflow's run history commits nothing
    from github-actions[bot] anywhere in this repo), but "never collided
    yet" on a live daily schedule is not the same as "cannot collide". The
    fix was to remove the schedule, not to build a lease: this workflow is
    now workflow_dispatch-only, an explicit human-triggered failover, never
    a second automatic writer. These tests keep that invariant from quietly
    regressing.
    """

    @staticmethod
    def _workflow_text() -> str:
        with open(WORKFLOW_PATH, encoding="utf-8") as handle:
            return handle.read()

    def test_workflow_file_exists(self):
        assert os.path.isfile(WORKFLOW_PATH), WORKFLOW_PATH

    def test_no_schedule_trigger(self):
        """A `schedule:` trigger key (even disabled-looking) must not
        reappear under `on:` — that key is exactly what made this an
        unsupervised second writer, regardless of what the cron expression
        inside it says."""
        text = self._workflow_text()
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert not re.match(r"^schedule\s*:", stripped), (
                f"a live 'schedule:' trigger reappeared: {line!r} — this "
                f"workflow must stay manual-dispatch-only unless a real "
                f"ownership/lease mechanism with the Factory Mac cron is "
                f"built first (see NO_GLUE_OPS.md item 10)")

    def test_workflow_dispatch_trigger_is_present(self):
        """The workflow must still be usable as an explicit human failover,
        not disabled outright."""
        text = self._workflow_text()
        assert re.search(r"^\s*workflow_dispatch\s*:", text, re.MULTILINE)

    def test_commit_and_push_still_gated_on_a_real_diff(self):
        """The commit step must remain conditional on an actual corpus
        change, not an unconditional commit+push on every manual run."""
        text = self._workflow_text()
        assert "git diff --cached --quiet" in text
        assert "No corpus tip changes." in text
