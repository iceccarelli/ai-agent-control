"""tools/promote_forward_shadow.py: fail-closed, human-only promotion.

No network, no real artifacts/forward_shadow_current.json is ever touched —
everything runs against temp-file fixtures.
"""
from __future__ import annotations

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import promote_forward_shadow as pfs                # noqa: E402

OLD_SHADOW = {
    "tool": "slice76_forward_shadow (scored against the APPEND-ONLY full corpora)",
    "observed_at_utc": "2026-09-01T00:00:00Z",
    "forward_n_trades": 3,
    "closed_forward_bars": 41,
    "state_ladder": {"6_closed_trades": 3},
    "gate_requires": {"closed_forward_trades": 20, "forward_days": 180},
}

NEW_SCRATCH = {
    "tool": "slice76_forward_shadow (scored against the APPEND-ONLY full corpora)",
    "observed_at_utc": "2026-09-21T00:00:00Z",
    "forward_n_trades": 5,
    "closed_forward_bars": 48,
    "state_ladder": {"6_closed_trades": 5},
    "gate_requires": {"closed_forward_trades": 20, "forward_days": 180},
}


def _write(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


class TestFailsClosed:
    def test_no_flags_refuses_and_does_not_write(self, tmp_path):
        scratch_path = tmp_path / "forward.json"
        shadow_path = tmp_path / "forward_shadow_current.json"
        _write(scratch_path, NEW_SCRATCH)
        _write(shadow_path, OLD_SHADOW)
        before = shadow_path.read_text(encoding="utf-8")

        rc = pfs.main([
            "--from", str(scratch_path),
            "--shadow-path", str(shadow_path),
        ])

        assert rc != 0
        assert shadow_path.read_text(encoding="utf-8") == before

    def test_write_without_i_am_human_refuses_and_does_not_write(self, tmp_path):
        scratch_path = tmp_path / "forward.json"
        shadow_path = tmp_path / "forward_shadow_current.json"
        _write(scratch_path, NEW_SCRATCH)
        _write(shadow_path, OLD_SHADOW)
        before = shadow_path.read_text(encoding="utf-8")

        rc = pfs.main([
            "--from", str(scratch_path),
            "--shadow-path", str(shadow_path),
            "--write",
        ])

        assert rc != 0
        assert shadow_path.read_text(encoding="utf-8") == before

    def test_i_am_human_without_write_refuses_and_does_not_write(self, tmp_path):
        scratch_path = tmp_path / "forward.json"
        shadow_path = tmp_path / "forward_shadow_current.json"
        _write(scratch_path, NEW_SCRATCH)
        _write(shadow_path, OLD_SHADOW)
        before = shadow_path.read_text(encoding="utf-8")

        rc = pfs.main([
            "--from", str(scratch_path),
            "--shadow-path", str(shadow_path),
            "--i-am-human",
        ])

        assert rc != 0
        assert shadow_path.read_text(encoding="utf-8") == before


class TestPromotesOnlyWithBothFlags:
    def test_write_and_i_am_human_updates_temp_shadow_copy(self, tmp_path):
        scratch_path = tmp_path / "forward.json"
        shadow_path = tmp_path / "forward_shadow_current.json"
        _write(scratch_path, NEW_SCRATCH)
        _write(shadow_path, OLD_SHADOW)

        rc = pfs.main([
            "--from", str(scratch_path),
            "--shadow-path", str(shadow_path),
            "--i-am-human",
            "--write",
        ])

        assert rc == 0
        written = json.loads(shadow_path.read_text(encoding="utf-8"))
        assert written["forward_n_trades"] == 5
        assert written["closed_forward_bars"] == 48
        assert written == NEW_SCRATCH

class TestBarsResolveThroughNestedSchema:
    """The real scorer nests the closed-bar count under `ceiling` /
    `forward_window`, never at the top level — see stage_b_bars.py. The
    before/after summary must not silently report None or 0 for a shadow
    file or scratch score that only carries the nested form."""

    def test_summary_resolves_nested_ceiling_bars_not_top_level(
            self, tmp_path, capsys):
        old_flat = dict(OLD_SHADOW)
        new_nested = {
            "tool": "slice76_forward_shadow",
            "observed_at_utc": "2026-09-21T00:00:00Z",
            "forward_n_trades": 3,
            "ceiling": {"closed_forward_bars": 43},
            "forward_window": {"of_which_closed": 43},
            "gate_requires": {"closed_forward_trades": 20, "forward_days": 180},
        }
        scratch_path = tmp_path / "forward.json"
        shadow_path = tmp_path / "forward_shadow_current.json"
        _write(scratch_path, new_nested)
        _write(shadow_path, old_flat)

        rc = pfs.main([
            "--from", str(scratch_path),
            "--shadow-path", str(shadow_path),
        ])
        assert rc != 0

        out = capsys.readouterr().out
        assert "closed_forward_bars: 41 -> 43  (CHANGED)" in out

    def test_promoting_a_nested_only_scratch_still_summarizes_the_real_count(
            self, tmp_path, capsys):
        """After promotion the tracked shadow file has no top-level
        `closed_forward_bars` key at all — the next promote's `old` side must
        still resolve the nested value rather than treating it as 0/None."""
        nested = {
            "tool": "slice76_forward_shadow",
            "observed_at_utc": "2026-09-21T00:00:00Z",
            "forward_n_trades": 3,
            "ceiling": {"closed_forward_bars": 43},
            "forward_window": {"of_which_closed": 43},
        }
        scratch_path = tmp_path / "forward.json"
        shadow_path = tmp_path / "forward_shadow_current.json"
        _write(scratch_path, nested)
        _write(shadow_path, nested)

        rc = pfs.main([
            "--from", str(scratch_path),
            "--shadow-path", str(shadow_path),
        ])
        assert rc != 0

        out = capsys.readouterr().out
        assert "closed_forward_bars: 43 -> 43" in out
        assert "(CHANGED)" not in out


class TestPromoteToolIsNeverAutomated:
    def test_it_is_never_invoked_from_the_accrual_script(self):
        """The tool name may appear in a comment or a printed hint pointing a
        human at it, but the script must never actually execute it."""
        script = os.path.join(REPO, "scripts", "stage_b_forward_accrual.sh")
        with open(script, encoding="utf-8") as handle:
            text = handle.read()
        assert '"$PY" tools/promote_forward_shadow.py' not in text
        assert "$PY tools/promote_forward_shadow.py" not in text
