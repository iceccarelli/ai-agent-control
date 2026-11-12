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

# ---------------------------------------------------------------------------
# OFFLINE BY DEFAULT: no inherited credentials, no network.
#
# A developer shell or CI runner may carry real exchange credentials and a live
# or testnet venue selection. A test that builds a client without an injected
# transport would then SIGN AND SEND a real request. So, before any test module
# is imported (config reads the environment at import), the venue/credential
# variables are removed from this process, and the HTTP layer refuses to leave
# the machine. The caller's environment is restored when the session ends.
# Opt out for a deliberate, separately authorised integration run only:
#   BOT_ALLOW_NETWORK_TESTS=1 (and it still needs its own explicit arming).
# ---------------------------------------------------------------------------
_SCRUBBED = ("BYBIT_API_KEY", "BYBIT_API_SECRET", "BYBIT_VENUE", "USE_TESTNET",
             "PAPER_TRADING", "LIVE_TRADING_ACK")
_ALLOW_NETWORK = os.environ.get("BOT_ALLOW_NETWORK_TESTS") == "1"
_SAVED_ENV = {} if _ALLOW_NETWORK else {
    k: os.environ.pop(k) for k in _SCRUBBED if k in os.environ}


# ---------------------------------------------------------------------------
# OWNER STATE IS NEVER A TEST'S TO TOUCH: the holdout registry, the candidate ledgers and the capital lineage.
#
# These live in the owner's home (``~/.ai-agent-control``). The function-scoped fixtures below give every TEST its own copies, but a MODULE-scoped fixture (and any subprocess it starts) is set up BEFORE
# them, so it saw the real files: ``test_ms_pipeline_cli.py::ran`` runs ``ms_pipeline.py run --unreserved-exploratory`` as a subprocess, which RECORDED A READ of the fixture window (to 2027-01-03) in the real
# per-user registry. The owner is told to run ``make test-quick`` (RUNBOOKS); doing so burned ``mainnet|linear`` through 2027-01-03 and every later ``--reserve`` was refused ALREADY_READ. The variables are
# therefore set for the WHOLE SESSION here, at import (before any module is collected and before any subprocess is started), and a session-end check fails the run if the real files changed anyway.
# ---------------------------------------------------------------------------
_REAL_OWNER_HOME = os.path.join(os.path.expanduser("~"), ".ai-agent-control")
_REAL_CANDIDATE_LEDGERS = os.path.join(os.path.expanduser("~"), ".cache", "ai-agent-control", "candidate_ledgers")        # where candidate_readiness / candidate_competition write when CANDIDATE_LEDGER_DIR is unset
_OWNER_STATE_VARS = ("HISTORY_HOLDOUT_LEDGER", "CANDIDATE_LEDGER_DIR", "CAPITAL_ANCHOR_DIR")
_OWNER_STATE_ORIG = {k: os.environ.get(k) for k in _OWNER_STATE_VARS}
_SESSION_OWNER_STATE = tempfile.mkdtemp(prefix="bot_tests_owner_state_")
os.environ["HISTORY_HOLDOUT_LEDGER"] = os.path.join(_SESSION_OWNER_STATE, "history_holdout.jsonl")
os.environ["CANDIDATE_LEDGER_DIR"] = os.path.join(_SESSION_OWNER_STATE, "candidate_ledgers")
os.environ["CAPITAL_ANCHOR_DIR"] = os.path.join(_SESSION_OWNER_STATE, "capital_anchor")


def _real_owner_state_fingerprint():
    """(name, size, mtime_ns) of what a test run must never change under the real ``~/.ai-agent-control``: the holdout registry and its sidecars, the capital anchor directory, the autopilot's engine root book."""
    out = []
    for rel in ("history_holdout.jsonl", "history_holdout.jsonl.chain", "history_holdout.jsonl.head", "ms_autopilot/capital.json", "ms_testnet_capability.json", "ms_autopilot_grant.json"):
        p = os.path.join(_REAL_OWNER_HOME, rel)
        try:
            st = os.stat(p)
            out.append((rel, st.st_size, st.st_mtime_ns))
        except OSError:
            out.append((rel, None, None))
    anchor = os.path.join(_REAL_OWNER_HOME, "capital_anchor")
    try:
        out.append(("capital_anchor/", tuple(sorted((n, os.stat(os.path.join(anchor, n)).st_size) for n in os.listdir(anchor)))))
    except OSError:
        out.append(("capital_anchor/", None))
    ledgers = _REAL_CANDIDATE_LEDGERS
    try:
        out.append(("candidate_ledgers/", tuple(sorted((n, os.stat(os.path.join(ledgers, n)).st_size) for n in os.listdir(ledgers)))))
    except OSError:
        out.append(("candidate_ledgers/", None))
    return tuple(out)


_OWNER_STATE_BEFORE = _real_owner_state_fingerprint()


class NetworkBlocked(RuntimeError):
    """A test tried to make a real HTTP request."""


def _blocked(self, method, url, *args, **kwargs):
    raise NetworkBlocked(
        f"offline test run: {str(method).upper()} to "
        f"{str(url).split('?')[0]} was blocked (set BOT_ALLOW_NETWORK_TESTS=1 "
        "only for a deliberately authorised integration run)")


if not _ALLOW_NETWORK:
    try:
        import requests as _requests
        _requests.Session.request = _blocked
    except ImportError:                                  # pragma: no cover
        pass


@pytest.fixture(autouse=True)
def _isolated_capital_anchor(tmp_path_factory, monkeypatch):
    """The capital lineage lives OUTSIDE --root (ms_capital_anchor), at $CAPITAL_ANCHOR_DIR or ~/.ai-agent-control/capital_anchor. A test must never read or write the developer's real one, and two tests must
    never share an account's lineage: every test gets its own directory."""
    monkeypatch.setenv("CAPITAL_ANCHOR_DIR", str(tmp_path_factory.mktemp("capital_anchor")))
    # the SESSION-wide directories set at import are the safety net for module-scoped fixtures and subprocesses; a TEST gets its own candidate-ledger directory so that one test's research ledgers can never make
    # another test's reservation look like it follows an earlier evaluation (PRIOR_EVALUATION_DETECTED)
    monkeypatch.setenv("CANDIDATE_LEDGER_DIR", str(tmp_path_factory.mktemp("candidate_ledgers")))
    monkeypatch.delenv("CAPITAL_WITNESS", raising=False)
    monkeypatch.delenv("CAPITAL_WITNESS_REQUIRED", raising=False)
    yield


@pytest.fixture(autouse=True, scope="session")
def _restore_caller_environment():
    yield
    os.environ.update(_SAVED_ENV)
    for k, v in _OWNER_STATE_ORIG.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    after = _real_owner_state_fingerprint()
    if after != _OWNER_STATE_BEFORE:                                                    # a test (or a subprocess it started) wrote the owner's REAL state: fail the run loudly, never quietly
        changed = [a for a, b in zip(after, _OWNER_STATE_BEFORE) if a != b]
        pytest.fail("this test run changed the owner's REAL state under ~/.ai-agent-control (a test must use the session-isolated $HISTORY_HOLDOUT_LEDGER / $CANDIDATE_LEDGER_DIR / "
                    f"$CAPITAL_ANCHOR_DIR and an engine root under tmp_path): {changed}", pytrace=False)


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


@pytest.fixture(autouse=True, scope="session")
def _no_test_opens_the_tracked_state_database():
    """No test may open the database file that is tracked in the repository.

    `bot/state/trading_state.db` is a real file in a checkout (and in an
    operator's, where it may be a live ledger). `STATE_DB_PATH` defaults to a path
    that resolves to it, so a test or tool that forgets to pass a temp path would
    read, migrate or WRITE it. The suite refuses that at the one place every SQLite
    open passes through. A test that needs a database uses `tmp_path`.
    """
    import sqlite3

    tracked = os.path.realpath(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state"))
    real_connect = sqlite3.connect

    def guarded(database, *args, **kwargs):
        try:
            target = os.path.realpath(os.fspath(database))
        except TypeError:
            target = ""
        if target == tracked or target.startswith(tracked + os.sep):
            raise AssertionError(
                f"a test tried to open {target}: the tracked state directory is "
                "never a test database. Use tmp_path.")
        return real_connect(database, *args, **kwargs)

    sqlite3.connect = guarded
    try:
        yield
    finally:
        sqlite3.connect = real_connect


@pytest.fixture(autouse=True)
def _private_executor_lock_per_test(tmp_path, monkeypatch):
    """The legacy armed tools take a LOCAL account lock (``ms_executor_lock``) at a path ``MS_EXECUTOR_LOCK_PATH`` names, else under the owner's real ``~/.ai-agent-control``. No test may touch the
    real one (nor contend with another test, or with a running autopilot on a developer's machine): every test gets its own."""
    monkeypatch.setenv("MS_EXECUTOR_LOCK_PATH", str(tmp_path / "executor_lock" / "ms_executor.lock"))
    yield


@pytest.fixture(autouse=True)
def _drill_single_writer_per_test(request, monkeypatch):
    """An armed drill takes the single-writer authority (the local account lock AND the shared Supabase lease) before it may send anything. Tests of the drill's OWN behaviour arm it without a lease
    backend, so the guard is satisfied by default; a test OF the guard marks itself ``real_single_writer`` and sees the real function."""
    if request.node.get_closest_marker("real_single_writer"):
        yield
        return
    import sys
    bot = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for extra in (bot, os.path.join(bot, "tools")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    try:
        import drill
    except Exception:                                               # noqa: BLE001 - a test session that cannot import the drill has nothing to patch
        yield
        return
    monkeypatch.setattr(drill, "single_writer_guard", lambda env, max_runtime_s=None: ((lambda: None), ""))
    yield


@pytest.fixture(autouse=True)
def _drill_entry_ledger_per_test(request, tmp_path, monkeypatch):
    """The drill claims one armed entry per UTC day in a ledger file it is told to use, and refuses when none is named. Tests that arm a drill do not
    name one, so each gets a fresh temporary ledger (no test can consume another's day). A test of the real refusal marks itself
    ``real_entry_ledger`` and sees the original function."""
    if request.node.get_closest_marker("real_entry_ledger"):
        yield
        return
    import sys
    bot = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for extra in (bot, os.path.join(bot, "tools")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    try:
        import drill
    except Exception:                                               # noqa: BLE001 - a test session that cannot import the drill has nothing to patch
        yield
        return
    original = drill.claim_entry_slot
    ledger = str(tmp_path / "drill_entries_per_test.jsonl")

    def with_ledger(path, now_ms, max_per_day=None):
        return original(path or ledger, now_ms, max_per_day)

    with_ledger.__wrapped__ = original
    monkeypatch.setattr(drill, "claim_entry_slot", with_ledger)
    yield


@pytest.fixture(autouse=True)
def _entry_authority_for_tests(request, monkeypatch):
    """Opening orders now need an entry authority (a bare client refuses). Tests that are not ABOUT the authority get a permissive stand-in, defined HERE only: no permissive
    authority exists in production code. A test of the real thing marks itself ``real_entry_authority`` and sees the production objects."""
    if request.node.get_closest_marker("real_entry_authority"):
        yield
        return
    import sys
    bot = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if bot not in sys.path:
        sys.path.insert(0, bot)
    try:
        import bybit_connection
        import entry_authority
    except Exception:                                              # noqa: BLE001
        yield
        return

    class _PermissiveForTests:
        def authorize(self, body, *, venue):
            return None

    monkeypatch.setattr(bybit_connection, "DEFAULT_ENTRY_AUTHORITY", _PermissiveForTests())
    monkeypatch.setattr(entry_authority, "build_for_book", lambda **kw: _PermissiveForTests())
    yield


@pytest.fixture(autouse=True)
def _isolated_history_holdout_registry(tmp_path, monkeypatch):
    """No test may read or write the REAL per-user holdout registry (``~/.ai-agent-control/history_holdout.jsonl``).

    ``tools/history_holdout.py`` keeps ONE registry per user so that no ledger directory, data root or mark dataset can give an unreserved read a clean slate. The price of that is a global
    file, and a test that touched it would burn (or reserve) real calendar time on the owner's machine. Every test gets its own empty registry through ``$HISTORY_HOLDOUT_LEDGER``; a test
    that wants a specific path sets the variable itself (its monkeypatch runs after this one).
    """
    monkeypatch.setenv("HISTORY_HOLDOUT_LEDGER", str(tmp_path / "isolated_holdout_registry" / "history_holdout.jsonl"))
