"""The durable registration invariant, in one place.

WHY THIS MODULE EXISTS
======================
Until slice 57 the repository asserted, in 38 places across 8 test files, that
`cleared_edge_signal_from_artifacts` returns `None`. That was true, and it was
the right thing to assert while nothing had cleared.

Slice 57 registered `funding_carry_fade_btc_v1` under a pre-declared
out-of-sample gate, and all 38 went red at once.

**That is the single most dangerous moment in this programme's history**, and
it deserves to be named rather than quietly fixed: a result went the agent's
way, and 38 guards objected. The temptation is to relax each one until the
suite is green again, which would leave the repository asserting nothing much
in exchange for a number nobody could check.

So the guards are not relaxed one by one. They are all pointed at the invariant
that was underneath them the whole time, which is **stronger than "None" in the
ways that matter and weaker only in the one way the programme has legitimately
earned**:

    1. no FROZEN name may ever be the cleared edge — that is what almost every
       one of those 38 assertions was actually protecting;
    2. if anything is cleared, it must be a name with a pre-declared gate
       registered in `project_status`, not an artefact that merely looks good;
    3. and it must be the name the repository's own record says cleared.

Point 1 is the load-bearing one. Eleven families are frozen and the deny-list
is the reason none of them can be promoted by dropping a file into
`artifacts/`. Nothing in slice 57 touches that, and these helpers keep asserting
it everywhere the old `is None` used to.

WHAT THIS DELIBERATELY DOES NOT DO
==================================
It does not accept "something cleared" as sufficient. A name with no registered
gate fails. A frozen name fails. A name other than the one on record fails.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import project_status as ps  # noqa: E402

#: The one name the repository's record says cleared, and the artefact that did
#: it. Written here rather than derived, so that a second signal clearing — or
#: this one clearing from a different artefact — is a test failure and a human
#: decision, not a silent widening.
PERMITTED_CLEARED_SIGNAL = "funding_carry_fade_btc_v1"
PERMITTED_CLEARED_ARTEFACT = "slice57_oos_edge_BTCUSDT_summary.json"


def assert_registration_is_sound(artifact_dir: Optional[str] = None) -> None:
    """The replacement for `assert cleared_edge_signal_from_artifacts(...) is None`.

    Passes when nothing cleared, and when exactly the one gated name did.
    Fails on anything else — including, and especially, a frozen name.
    """
    directory = artifact_dir or os.path.join(REPO, "artifacts")
    cleared = ps.cleared_edge_signal_from_artifacts(directory)

    if cleared is None:
        return

    assert cleared not in ps.ABSENT_SIGNALS, (
        f"{cleared!r} is on the deny-list and must never be a cleared edge. "
        f"Evidence that closed it: {ps.FROZEN_ABSENT.get(cleared, '')[:200]}")
    assert cleared in ps.OOS_GATED_REGISTRATION, (
        f"{cleared!r} cleared without a pre-declared gate registered in "
        "project_status. A name that can register on a good-looking artefact "
        "alone is the slice-55 defect returning.")
    assert cleared == PERMITTED_CLEARED_SIGNAL, (
        f"{cleared!r} cleared, but the repository's record says only "
        f"{PERMITTED_CLEARED_SIGNAL!r} did. A second cleared edge is a human "
        "decision, not a test update.")


def assert_this_artefact_registers_nothing(artifact_dir: str,
                                           signal: str) -> None:
    """`signal` is not what cleared, whatever is in `artifact_dir`.

    For the per-family tests whose point is "MY family registered nothing".
    Stronger than the old `is None` for that purpose: it stays meaningful even
    once some other family has legitimately cleared.
    """
    cleared = ps.cleared_edge_signal_from_artifacts(artifact_dir)
    assert cleared != signal, (
        f"{signal!r} registered a cleared edge from {artifact_dir}")
    assert_registration_is_sound(artifact_dir)


def stock_cleared_edge_signal() -> Optional[str]:
    """What a stock snapshot carries today, for tests that pin the snapshot.

    Derived from the hook rather than hardcoded, so a test using it cannot
    drift away from what the code actually returns — while
    `assert_registration_is_sound` above keeps the value honest.
    """
    return ps.cleared_edge_signal_from_artifacts(
        os.path.join(REPO, "artifacts"))
