"""A test run must never read or write the owner's REAL state (the holdout registry, the candidate ledgers, the capital lineage) -- including through a MODULE-scoped fixture or a subprocess.

REPRODUCED (round-10 research specialist): ``test_ms_pipeline_cli.py::ran`` is module-scoped and runs ``ms_pipeline.py run --unreserved-exploratory`` as a subprocess. The per-test isolation fixtures are
function-scoped, so they were not yet active when it ran: the child recorded a read of the fixture window (to 2027-01-03) in the real ``~/.ai-agent-control/history_holdout.jsonl``. The owner runs
``make test-quick`` (RUNBOOKS), so doing that burned ``mainnet|linear`` through 2027-01-03 and every later ``--reserve`` was refused ALREADY_READ. The variables are now set for the whole session in conftest
(at import, before collection) and the session-end check fails the run if the real files changed.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
VARS = ("HISTORY_HOLDOUT_LEDGER", "CANDIDATE_LEDGER_DIR", "CAPITAL_ANCHOR_DIR")


@pytest.fixture(scope="module")
def seen_by_a_module_fixture():
    """what a MODULE-scoped fixture (set up before any function-scoped autouse fixture) and a subprocess it starts see: the situation of ``ran``"""
    here = {k: os.environ.get(k) for k in VARS}
    code = "import os; print('|'.join(os.environ.get(k, '') for k in %r))" % (VARS,)
    child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60).stdout.strip().split("|")
    return here, dict(zip(VARS, child))


def test_a_module_scoped_fixture_and_its_subprocess_never_see_the_real_owner_state(seen_by_a_module_fixture):
    here, child = seen_by_a_module_fixture
    real_home = os.path.join(os.path.expanduser("~"), ".ai-agent-control")
    for k in VARS:
        assert here[k] and "bot_tests_owner_state_" in here[k] and not here[k].startswith(real_home), (k, here[k])
        assert child[k] == here[k], "a subprocess inherits the isolation: that is the path that burned the registry"


def test_the_registry_a_child_resolves_is_the_isolated_one(seen_by_a_module_fixture):
    code = "import sys; sys.path.insert(0, %r); import history_holdout as H; print(H.default_registry_path())" % (os.path.join(BOT, "tools"),)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60).stdout.strip()
    assert out and not out.startswith(os.path.join(os.path.expanduser("~"), ".ai-agent-control")), out      # per-test isolated (function-scoped) or the session directory: never the real home


def test_the_session_end_check_sees_a_change_to_the_real_owner_state(monkeypatch, tmp_path):
    import conftest as CF
    base = CF._real_owner_state_fingerprint()
    assert isinstance(base, tuple) and base == CF._OWNER_STATE_BEFORE, "nothing in this run has touched the real owner state so far"
    fake_home = tmp_path / ".ai-agent-control"
    fake_home.mkdir()
    monkeypatch.setattr(CF, "_REAL_OWNER_HOME", str(fake_home))
    before = CF._real_owner_state_fingerprint()
    (fake_home / "history_holdout.jsonl").write_text("{}\n")
    assert CF._real_owner_state_fingerprint() != before
