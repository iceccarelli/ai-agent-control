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
