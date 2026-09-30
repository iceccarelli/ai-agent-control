"""Suite-wide fixtures.

The one thing here is the data-read ledger kill switch.

Signal-quality note (see bot/pytest.ini for the marker registration):
a full-suite red is not automatically a code regression. Two categories are
expected to go red for reasons that have nothing to do with whether the code
is correct:

* scikit-learn missing from this environment. Files that need it (e.g.
  test_policy.py, test_training.py) report SKIPPED with that reason, not
  ERROR or FAILED — install scikit-learn (see requirements.txt) to get real
  coverage back.
* `@pytest.mark.data_freshness` — a test asserting against the real wall
  clock or the live corpus's own row counts (e.g. test_corpus_health.py,
  test_daily_forward_refresh.py, parts of test_settlement_corpus_is_multi_
  asset.py, test_borrow_curve.py, test_slice62_forward_extension.py). These
  are SUPPOSED to go red as real time passes without a fresh corpus accrual
  or a re-pin; that is the finding, not a bug.

Run `pytest -m "not data_freshness"` for the code-correctness signal alone.
A red result outside both of the above categories is a real failure and
should be investigated as one.
"""
from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture(autouse=True, scope="session")
def _disable_data_read_ledger():
    """A test run must not fabricate reads.

    `market_data.load_corpus` records every corpus read into
    artifacts/data_read_ledger.json so that a future holdout can be certified
    untouched. The suite loads corpora hundreds of times, and none of those
    loads informed a hypothesis — they are assertions about code, not looks at
    data. Left unsuppressed they would bury the six reads that actually matter
    under noise, and a ledger nobody can read is a ledger nobody uses.

    Set for the session and restored afterwards, so an interactive run in the
    same shell is still recorded.
    """
    previous = os.environ.get("LEDGER_DISABLED")
    os.environ["LEDGER_DISABLED"] = "1"
    yield
    if previous is None:
        os.environ.pop("LEDGER_DISABLED", None)
    else:
        os.environ["LEDGER_DISABLED"] = previous


@pytest.fixture(autouse=True, scope="session")
def _tracked_read_ledger_is_restored():
    """The suite must not be able to edit the contamination ledger.

    `_disable_data_read_ledger` above sets LEDGER_DISABLED, which is why this
    has held so far. But it is a kill switch, and a test that lifts it — there
    is exactly one, and it is the test that proves recording stays non-fatal —
    gets a live writer pointed at `artifacts/data_read_ledger.json`. That is a
    TRACKED file, and it is the record of which windows this programme has
    already looked at: the thing every holdout claim is checked against. An
    entry the suite invented is a claim about the data nobody made.

    It has already happened once. `reserved_holdout.load` took its path as a
    default argument, bound at import, so the test's `monkeypatch.setattr(rh,
    "DEFAULT_PATH", ...)` did not reach it and a read of dataset "X" landed in
    the real ledger. `git status` caught it; nothing in the suite did.

    So: snapshot at session start, restore and FAIL at session end. Restoring
    keeps the working tree clean for the verify receipt; failing is what makes
    the next occurrence visible instead of merely reverted.
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(
        __file__))), "artifacts", "data_read_ledger.json")
    before = None
    if os.path.isfile(path):
        with open(path, "rb") as handle:
            before = handle.read()

    yield

    if before is None:
        return
    with open(path, "rb") as handle:
        after = handle.read()
    if after != before:
        with open(path, "wb") as handle:
            handle.write(before)
        raise AssertionError(
            f"the suite modified the tracked read ledger ({path}). It has been "
            f"restored, but a test is writing where it must not: pass an "
            f"explicit path= to reserved_holdout, and do not rely on "
            f"monkeypatching DEFAULT_PATH alone.")


# --- MODULE SCOPE, deliberately -------------------------------------------
# persistence.py binds DEFAULT_DB_PATH = os.environ.get("STATE_DB_PATH", ...)
# at IMPORT time, and config.py defaults the same key to the TRACKED fixture
# state/trading_state.db. A session-scoped fixture runs too late: pytest has
# already imported the test modules, which imported persistence, which already
# baked the tracked path in.
#
# So this runs when conftest is imported, which is before any test module.
#
# Why it matters: sqlite writes journal pages even on a read-only open, so the
# tracked file came back dirty after every suite run and `git checkout` refused
# to switch branches with "local changes would be overwritten". A test suite
# that dirties the repository it is testing is a bug in the suite.
#
# A test that deliberately wants the shipped fixture copies it explicitly --
# see test_operator_refuses_when_the_shipped_kill_switch_is_engaged.
_TEST_STATE_DIR = tempfile.mkdtemp(prefix="pytest-state-")
os.environ["STATE_DB_PATH"] = os.path.join(_TEST_STATE_DIR, "trading_state.db")


# --- data_freshness marker for a digest-locked file ------------------------
# test_slice62_forward_extension.py is one of the "apparatus" files whose
# sha256 is pinned in artifacts/slice6{3,4,5,6,7}_restored_from_slice*.json
# and re-verified by each of those files' TestTheRestoration tests. Editing
# its source (even to add a marker + docstring) changes that hash and trips
# every one of those restoration checks — a provenance ledger, not a place
# for this cycle's marker edits to land.
#
# test_a_truncated_corpus_is_caught belongs in data_freshness for the same
# reason as the tests marked directly in other files (see the module
# docstring above): `never_shrank` compares the live corpus's row count
# against slice55's pinned baseline, the daily accrual keeps growing that
# live corpus, and once the gap between "now" and the pin exceeds the fixed
# 50 rows this test truncates, the truncated copy still lands above the
# stale pin and `never_shrank` reads True instead of False. That is the pin
# drifting out of date with real time, not a code regression. So the marker
# is applied here, from collection, leaving the file itself untouched.
def pytest_collection_modifyitems(items):
    target = (
        "test_slice62_forward_extension.py::"
        "TestTheNewInvariantActuallyCatchesThings::"
        "test_a_truncated_corpus_is_caught"
    )
    for item in items:
        if item.nodeid.endswith(target):
            item.add_marker(pytest.mark.data_freshness)
