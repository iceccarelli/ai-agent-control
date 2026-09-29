"""tools/promote_forward_shadow.py: fail-closed, human-only promotion.

No network, no real artifacts/forward_shadow_current.json is ever touched —
everything runs against temp-file fixtures.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import promote_forward_shadow as pfs                # noqa: E402
import shadow as _shadow                             # noqa: E402
from signals import funding_carry_fade_btc_v1 as _signal  # noqa: E402

#: A syntactically real (40-hex) sha, independent of whatever this checkout's
#: actual HEAD is. Tests that want a "valid" candidate monkeypatch
#: `pfs.prov.git_commit` to return exactly this, so the independent git check
#: in `validate_provenance` passes without depending on the real repo state.
VALID_GIT_COMMIT = "a" * 40


def _valid_provenance():
    """Fields that pass `validate_provenance` against the real frozen signal
    module and shadow scope, paired with `_patched_git_identity` below."""
    return {
        "signal": _shadow.SHADOW_SIGNAL,
        "symbol": _shadow.SHADOW_SYMBOL,
        "constants_fingerprint": _shadow.constants_fingerprint()[:32],
        "constants": dict(_signal.CONSTANTS),
        "fund_abs": _signal.FUND_ABS,
        "caps": {
            "max_concurrent_positions": _shadow.SHADOW_MAX_CONCURRENT_POSITIONS,
            "max_entries_per_day": _shadow.SHADOW_MAX_ENTRIES_PER_DAY,
            "max_notional_usd": _shadow.SHADOW_MAX_NOTIONAL_USD,
            "symbol": _shadow.SHADOW_SYMBOL,
        },
        "git_commit": VALID_GIT_COMMIT,
    }


@pytest.fixture(autouse=True)
def _patched_git_identity(monkeypatch):
    """Every test in this file promotes against a fake but internally
    consistent git identity, never the real checkout's actual HEAD — the
    real HEAD changes over time and would make these tests depend on the
    state of the tree they happen to run in."""
    monkeypatch.setattr(pfs.prov, "git_commit", lambda *a, **kw: VALID_GIT_COMMIT)


OLD_SHADOW = {
    "tool": "slice76_forward_shadow (scored against the APPEND-ONLY full corpora)",
    "observed_at_utc": "2026-09-01T00:00:00Z",
    "forward_n_trades": 3,
    "closed_forward_bars": 41,
    "state_ladder": {"6_closed_trades": 3},
    "gate_requires": {"closed_forward_trades": 20, "forward_days": 180},
    **_valid_provenance(),
}

NEW_SCRATCH = {
    "tool": "slice76_forward_shadow (scored against the APPEND-ONLY full corpora)",
    "observed_at_utc": "2026-09-21T00:00:00Z",
    "forward_n_trades": 5,
    "closed_forward_bars": 48,
    "state_ladder": {"6_closed_trades": 5},
    "gate_requires": {"closed_forward_trades": 20, "forward_days": 180},
    **_valid_provenance(),
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

    def test_tmp_file_is_gone_after_a_successful_promotion(self, tmp_path):
        """Promotion writes through a temp file and atomically replaces the
        target, never leaving a stray .tmp file behind on success."""
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
        assert not os.path.exists(f"{shadow_path}.tmp")


class TestPromotionNoop:
    """Repeatedly promoting identical canonical content must be a true
    no-op: zero write, reported explicitly, not a silent re-write of
    identical bytes with a fresh mtime."""

    def test_identical_content_is_a_noop_and_does_not_touch_the_file(
            self, tmp_path, capsys):
        scratch_path = tmp_path / "forward.json"
        shadow_path = tmp_path / "forward_shadow_current.json"
        _write(scratch_path, OLD_SHADOW)
        _write(shadow_path, OLD_SHADOW)
        mtime_before = os.stat(shadow_path).st_mtime_ns

        rc = pfs.main([
            "--from", str(scratch_path),
            "--shadow-path", str(shadow_path),
            "--i-am-human",
            "--write",
        ])

        assert rc == 0
        assert os.stat(shadow_path).st_mtime_ns == mtime_before
        out = capsys.readouterr().out
        assert "PROMOTION_NOOP" in out

    def test_reordered_but_identical_keys_are_still_a_noop(
            self, tmp_path, capsys):
        """Content hashing is key-order independent, so a shadow file whose
        keys happen to be serialized in a different order than the scratch
        file is still recognized as identical content."""
        scratch_path = tmp_path / "forward.json"
        shadow_path = tmp_path / "forward_shadow_current.json"
        reordered = dict(reversed(list(OLD_SHADOW.items())))
        _write(scratch_path, OLD_SHADOW)
        _write(shadow_path, reordered)

        rc = pfs.main([
            "--from", str(scratch_path),
            "--shadow-path", str(shadow_path),
            "--i-am-human",
            "--write",
        ])

        assert rc == 0
        out = capsys.readouterr().out
        assert "PROMOTION_NOOP" in out

    def test_changed_content_is_accepted_not_a_noop(self, tmp_path, capsys):
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
        out = capsys.readouterr().out
        assert "PROMOTION_ACCEPTED" in out
        assert "PROMOTION_NOOP" not in out

    def test_first_ever_promotion_is_not_a_noop(self, tmp_path, capsys):
        """No prior shadow file means nothing to compare against, so the
        first promotion is always PROMOTION_ACCEPTED even though there is
        technically no 'change' to point at."""
        scratch_path = tmp_path / "forward.json"
        shadow_path = tmp_path / "forward_shadow_current.json"
        _write(scratch_path, NEW_SCRATCH)

        rc = pfs.main([
            "--from", str(scratch_path),
            "--shadow-path", str(shadow_path),
            "--i-am-human",
            "--write",
        ])

        assert rc == 0
        out = capsys.readouterr().out
        assert "PROMOTION_ACCEPTED" in out


class TestValidatesProvenanceIndependently:
    """A candidate cannot certify its own provenance. Every field checked
    here is independently re-derived from the live frozen signal module,
    the live shadow scope, or the live git tree — none of these mismatches
    may be waved through even with both human flags."""

    def _promote(self, tmp_path, candidate):
        scratch_path = tmp_path / "forward.json"
        shadow_path = tmp_path / "forward_shadow_current.json"
        _write(scratch_path, candidate)
        _write(shadow_path, OLD_SHADOW)
        return pfs.main([
            "--from", str(scratch_path),
            "--shadow-path", str(shadow_path),
            "--i-am-human",
            "--write",
        ])

    def test_wrong_signal_is_refused(self, tmp_path):
        bad = dict(NEW_SCRATCH)
        bad["signal"] = "some_other_signal_v2"
        assert self._promote(tmp_path, bad) != 0

    def test_wrong_symbol_is_refused(self, tmp_path):
        bad = dict(NEW_SCRATCH)
        bad["symbol"] = "ETHUSDT"
        assert self._promote(tmp_path, bad) != 0

    def test_missing_signal_is_refused(self, tmp_path):
        bad = dict(NEW_SCRATCH)
        del bad["signal"]
        assert self._promote(tmp_path, bad) != 0

    def test_forged_constants_fingerprint_is_refused(self, tmp_path):
        bad = dict(NEW_SCRATCH)
        bad["constants_fingerprint"] = "0" * 32
        assert self._promote(tmp_path, bad) != 0

    def test_tampered_constants_with_stale_fingerprint_is_refused(
            self, tmp_path):
        """The declared fingerprint alone isn't enough: if a 'constants'
        payload is present it must itself hash to the live fingerprint, so
        a candidate can't carry a retuned constants dict alongside an
        untouched (now-stale) fingerprint string."""
        bad = dict(NEW_SCRATCH)
        tampered = dict(bad["constants"])
        tampered["FUND_ABS"] = 0.0002  # retuned, fingerprint left as-is
        bad["constants"] = tampered
        assert self._promote(tmp_path, bad) != 0

    def test_wrong_fund_abs_is_refused(self, tmp_path):
        bad = dict(NEW_SCRATCH)
        bad["fund_abs"] = 0.0002
        assert self._promote(tmp_path, bad) != 0

    def test_wrong_caps_is_refused(self, tmp_path):
        bad = dict(NEW_SCRATCH)
        bad["caps"] = dict(bad["caps"], max_notional_usd=10_000.00)
        assert self._promote(tmp_path, bad) != 0

    def test_missing_git_commit_is_refused(self, tmp_path):
        bad = dict(NEW_SCRATCH)
        del bad["git_commit"]
        assert self._promote(tmp_path, bad) != 0

    def test_malformed_git_commit_is_refused(self, tmp_path):
        bad = dict(NEW_SCRATCH)
        bad["git_commit"] = "not-a-real-sha"
        assert self._promote(tmp_path, bad) != 0

    def test_wrong_git_commit_is_refused(self, tmp_path):
        """The candidate's git_commit must match THIS tree's actual HEAD
        (patched to VALID_GIT_COMMIT for this file) — a candidate claiming a
        different, syntactically valid commit is still refused."""
        bad = dict(NEW_SCRATCH)
        bad["git_commit"] = "b" * 40
        assert self._promote(tmp_path, bad) != 0

    def test_dirty_suffix_mismatch_is_refused(self, tmp_path, monkeypatch):
        """A candidate claiming a clean tree when the actual tree is dirty
        (or vice versa) is a provenance mismatch, not a cosmetic detail."""
        monkeypatch.setattr(pfs.prov, "git_commit",
                            lambda *a, **kw: VALID_GIT_COMMIT + "-dirty")
        bad = dict(NEW_SCRATCH)
        bad["git_commit"] = VALID_GIT_COMMIT  # claims clean
        assert self._promote(tmp_path, bad) != 0

    def test_valid_provenance_is_accepted(self, tmp_path):
        """Sanity check: NEW_SCRATCH's provenance, as constructed, actually
        passes — so the refusals above are testing the specific tampered
        field, not some unrelated fixture mistake."""
        assert self._promote(tmp_path, dict(NEW_SCRATCH)) == 0

    def test_refusal_does_not_write(self, tmp_path):
        scratch_path = tmp_path / "forward.json"
        shadow_path = tmp_path / "forward_shadow_current.json"
        bad = dict(NEW_SCRATCH)
        bad["symbol"] = "ETHUSDT"
        _write(scratch_path, bad)
        _write(shadow_path, OLD_SHADOW)
        before = shadow_path.read_text(encoding="utf-8")

        rc = pfs.main([
            "--from", str(scratch_path),
            "--shadow-path", str(shadow_path),
            "--i-am-human",
            "--write",
        ])

        assert rc != 0
        assert shadow_path.read_text(encoding="utf-8") == before


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


class TestRejectsRegression:
    """A promotion that would move a monotonic counter or the observation
    timestamp backward is evidence corruption, not genuine forward accrual,
    and must be refused even with both human flags."""

    def test_forward_n_trades_regression_refuses_even_with_both_flags(
            self, tmp_path):
        scratch_path = tmp_path / "forward.json"
        shadow_path = tmp_path / "forward_shadow_current.json"
        regressed = dict(NEW_SCRATCH)
        regressed["forward_n_trades"] = 2  # below OLD_SHADOW's 3
        _write(scratch_path, regressed)
        _write(shadow_path, OLD_SHADOW)
        before = shadow_path.read_text(encoding="utf-8")

        rc = pfs.main([
            "--from", str(scratch_path),
            "--shadow-path", str(shadow_path),
            "--i-am-human",
            "--write",
        ])

        assert rc != 0
        assert shadow_path.read_text(encoding="utf-8") == before

    def test_closed_forward_bars_regression_refuses(self, tmp_path):
        scratch_path = tmp_path / "forward.json"
        shadow_path = tmp_path / "forward_shadow_current.json"
        regressed = dict(NEW_SCRATCH)
        regressed["closed_forward_bars"] = 10  # below OLD_SHADOW's 41
        _write(scratch_path, regressed)
        _write(shadow_path, OLD_SHADOW)
        before = shadow_path.read_text(encoding="utf-8")

        rc = pfs.main([
            "--from", str(scratch_path),
            "--shadow-path", str(shadow_path),
            "--i-am-human",
            "--write",
        ])

        assert rc != 0
        assert shadow_path.read_text(encoding="utf-8") == before

    def test_observed_at_utc_regression_refuses(self, tmp_path):
        scratch_path = tmp_path / "forward.json"
        shadow_path = tmp_path / "forward_shadow_current.json"
        regressed = dict(NEW_SCRATCH)
        regressed["observed_at_utc"] = "2026-08-15T00:00:00Z"  # before OLD's
        _write(scratch_path, regressed)
        _write(shadow_path, OLD_SHADOW)
        before = shadow_path.read_text(encoding="utf-8")

        rc = pfs.main([
            "--from", str(scratch_path),
            "--shadow-path", str(shadow_path),
            "--i-am-human",
            "--write",
        ])

        assert rc != 0
        assert shadow_path.read_text(encoding="utf-8") == before

    def test_equal_counters_are_not_a_regression(self, tmp_path):
        """Re-promoting identical evidence (idempotent re-run) must not be
        rejected as a regression."""
        scratch_path = tmp_path / "forward.json"
        shadow_path = tmp_path / "forward_shadow_current.json"
        _write(scratch_path, OLD_SHADOW)
        _write(shadow_path, OLD_SHADOW)

        rc = pfs.main([
            "--from", str(scratch_path),
            "--shadow-path", str(shadow_path),
            "--i-am-human",
            "--write",
        ])

        assert rc == 0

    def test_no_prior_shadow_file_is_not_a_regression(self, tmp_path):
        """First-ever promotion: current is {} (no file yet), so there is
        nothing to regress against."""
        scratch_path = tmp_path / "forward.json"
        shadow_path = tmp_path / "forward_shadow_current.json"
        _write(scratch_path, NEW_SCRATCH)

        rc = pfs.main([
            "--from", str(scratch_path),
            "--shadow-path", str(shadow_path),
            "--i-am-human",
            "--write",
        ])

        assert rc == 0


class TestPromoteToolIsNeverAutomated:
    def test_it_is_never_invoked_from_the_accrual_script(self):
        """The tool name may appear in a comment or a printed hint pointing a
        human at it, but the script must never actually execute it."""
        script = os.path.join(REPO, "scripts", "stage_b_forward_accrual.sh")
        with open(script, encoding="utf-8") as handle:
            text = handle.read()
        assert '"$PY" tools/promote_forward_shadow.py' not in text
        assert "$PY tools/promote_forward_shadow.py" not in text
