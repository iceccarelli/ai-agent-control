"""Measurement tools must survive a tree that is not a git repository.

Finding (reproduced 2026-09-05 on a fresh unzip of the distributed
archive): `tools/slice76_forward_shadow.py` computed every number, then
died at the last line of `build()` with
`CalledProcessError: 'git rev-parse HEAD' returned non-zero exit status
128`. 51 tools carried the same unguarded idiom. The practical effect is
that no outside reviewer could reproduce a single figure in EDGE.md from
the artifact as distributed - the measurement ran and the result was
thrown away.

Fix: tools/provenance.py returns a sentinel instead of raising.
`UNKNOWN_NOT_A_GIT_REPO` is strictly more informative than a traceback:
it records that provenance was unavailable rather than pretending it was.
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import provenance  # noqa: E402


def test_git_commit_in_a_real_repo_is_a_sha_or_an_honest_sentinel():
    value = provenance.git_commit(ROOT)
    assert value == provenance.UNKNOWN or provenance.is_real_commit(value) \
        or value == provenance.UNAVAILABLE


def test_git_commit_on_a_non_repo_returns_the_sentinel_and_does_not_raise(tmp_path):
    assert provenance.git_commit(str(tmp_path)) == provenance.UNKNOWN


def test_dirty_tree_is_marked(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    run = lambda *a: subprocess.run(a, cwd=str(repo), capture_output=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (repo / "f.txt").write_text("one")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "c")
    clean = provenance.git_commit(str(repo))
    if not provenance.is_real_commit(clean):
        pytest.skip("git unavailable in this sandbox")
    assert not clean.endswith(provenance.DIRTY_SUFFIX)
    (repo / "f.txt").write_text("two")
    assert provenance.git_commit(str(repo)).endswith(provenance.DIRTY_SUFFIX)


def test_sentinels_are_not_mistaken_for_commits():
    assert not provenance.is_real_commit(provenance.UNKNOWN)
    assert not provenance.is_real_commit(provenance.UNAVAILABLE)
    assert not provenance.is_real_commit("")
    assert provenance.is_real_commit("a" * 40)
    assert provenance.is_real_commit("a" * 40 + provenance.DIRTY_SUFFIX)


def _digest_pinned_tools():
    """Historical apparatus whose sha256 is recorded and must not change.

    artifacts/slice67_restored_from_slice66.json pins the bytes of the
    slice-62..66 tools as a DATED RECORD of what was run at the time.
    tests/test_slice67_five_day_window.py enforces those digests. Editing
    them - even to fix a real crash - would rewrite history, which is the
    one thing this programme is built to prevent. They are exempt here and
    stay broken ON PURPOSE: they are not run to produce new numbers, and
    the current-generation tools that ARE run have been fixed.
    """
    manifest = os.path.join(ROOT, "artifacts", "slice67_restored_from_slice66.json")
    if not os.path.isfile(manifest):
        return set()
    import json
    data = json.load(open(manifest, encoding="utf-8"))
    pinned = set()
    for entries in data.get("apparatus", {}).values():
        if isinstance(entries, dict):
            pinned.update(entries)
    return pinned


def test_current_generation_tools_no_longer_crash_on_a_non_git_tree():
    """The whole class of defect, excluding immutable historical records."""
    pinned = _digest_pinned_tools()
    offenders = []
    for path in sorted(glob.glob(os.path.join(ROOT, "tools", "*.py"))):
        rel = os.path.relpath(path, ROOT)
        if rel in pinned:
            continue
        src = open(path, encoding="utf-8").read()
        if "rev-parse" not in src:
            continue
        # check_output raises on non-zero exit; subprocess.run(check=False)
        # and the provenance helper do not.
        if re.search(r"check_output\([^)]*rev-parse", src, re.S):
            offenders.append(rel)
    assert not offenders, (
        f"tools still crash on a non-git tree: {sorted(set(offenders))}")


def test_the_pinned_exemption_is_real_and_bounded():
    """If the pin manifest disappears, the exemption must not silently widen."""
    pinned = _digest_pinned_tools()
    assert pinned, "digest pin manifest missing; exemption would swallow everything"
    still_broken = [p for p in pinned if p.startswith("tools/")
                    and os.path.isfile(os.path.join(ROOT, p))
                    and "rev-parse" in open(os.path.join(ROOT, p),
                                            encoding="utf-8").read()]
    # These are known, dated, and deliberately left alone.
    assert all(re.search(r"slice6[2-6]", p) for p in still_broken), still_broken
