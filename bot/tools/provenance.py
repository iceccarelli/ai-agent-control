#!/usr/bin/env python3
"""Repository provenance for measurement artefacts. Never raises.

Every measurement tool in this tree stamps its artefact with a git commit.
The original idiom was::

    "git_commit": _provenance.git_commit(REPO)

which raises `CalledProcessError` on a tree that is not a git repository -
which is exactly what a reviewer has after unzipping the distributed
archive. The call sits at the LAST line of `build()`, after every number
has been computed, so the tool did the whole job and then threw the result
away. Verified 2026-09-05: a fresh unzip of
`tradingbot_slice76 (2) (1).zip` running
`tools/slice76_forward_shadow.py` dies with exit status 128 from
`git rev-parse HEAD`. 51 tools carry the same idiom.

The provenance field exists so a number can be tied to the code that
produced it. `UNKNOWN_NOT_A_GIT_REPO` says exactly that and is strictly
more informative than a traceback: it records that provenance was
unavailable rather than pretending it was or losing the measurement.
"""
from __future__ import annotations

import os
import subprocess
from typing import Optional
import provenance as _provenance  # noqa: E402

UNKNOWN = "UNKNOWN_NOT_A_GIT_REPO"
UNAVAILABLE = "UNKNOWN_GIT_UNAVAILABLE"
DIRTY_SUFFIX = "-dirty"


def git_commit(repo: Optional[str] = None, *, mark_dirty: bool = True) -> str:
    """Best-effort `HEAD` sha. Returns a sentinel instead of raising.

    `mark_dirty` appends `-dirty` when the working tree has uncommitted
    changes, so an artefact cannot silently claim to have come from a clean
    commit it does not match.
    """
    repo = repo or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True,
            capture_output=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return UNAVAILABLE
    if head.returncode != 0:
        return UNKNOWN
    sha = head.stdout.strip()
    if not sha:
        return UNKNOWN
    if not mark_dirty:
        return sha
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, text=True,
            capture_output=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return sha
    if status.returncode == 0 and status.stdout.strip():
        return sha + DIRTY_SUFFIX
    return sha


def workspace_dirty(repo: Optional[str] = None) -> Optional[bool]:
    """True iff `git status --porcelain` reports ANY change anywhere in the
    working tree — tracked or untracked. This is exactly the check
    `git_commit`'s `-dirty` suffix already makes, exposed as its own field
    so a caller that wants to reason about it does not have to parse a
    suffix off a commit string. Returns `None` when git itself could not
    be asked (no repo, git missing, timeout) — never a guessed boolean.
    """
    repo = repo or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, text=True,
            capture_output=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if status.returncode != 0:
        return None
    return bool(status.stdout.strip())


def source_tree_clean(repo: Optional[str] = None) -> Optional[bool]:
    """True iff no TRACKED `*.py` file differs from `HEAD`.

    Deliberately narrower than `workspace_dirty`: an untracked artefact or
    data file sitting in the working tree makes `git status --porcelain`
    (and `git_commit`'s `-dirty` suffix) non-empty without a single line of
    source having changed. Collapsing both facts into one "-dirty" string
    invites a reader to conclude "the code that ran does not match its
    claimed commit" from evidence that only shows "something, possibly
    irrelevant, is lying around uncommitted". This answers the question
    that actually matters for an execution artefact: did the SOURCE that
    executed match `code_commit`. Returns `None` when git itself could not
    be asked.
    """
    repo = repo or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", "*.py"], cwd=repo,
            text=True, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if status.returncode != 0:
        return None
    return not status.stdout.strip()


def is_real_commit(value: str) -> bool:
    """True only for an actual sha (dirty or not), not a sentinel."""
    if not value or value in (UNKNOWN, UNAVAILABLE):
        return False
    return len(value.replace(DIRTY_SUFFIX, "")) == 40


if __name__ == "__main__":
    print(git_commit())
