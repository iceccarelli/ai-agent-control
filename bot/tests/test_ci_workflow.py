"""The CI gate stays real: it names tests that exist, hides nothing, needs no secret."""
from __future__ import annotations

import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
WORKFLOW = os.path.join(os.path.dirname(BOT), ".github", "workflows",
                        "carry-verify.yml")

REQUIRED = (
    "test_carry_funding_truth", "test_carry_fee_truth", "test_carry_report",
    "test_carry_broker", "test_carry_preflight", "test_fee_payload_probe",
    "test_carry_live_gates", "test_carry_funding_history_gap",
    # a blanked ledger row must refuse to start, not read as a first run
    "test_carry_cold_start", "test_carry_entry_allowance_survives_restart",
    # the money path of the directional book: exits, funding, lifecycles
    "test_provisional_exit_reconciliation", "test_directional_funding",
    "test_position_lifecycles", "test_observe_exits",
    "test_unpriced_exit_visibility",
    "test_lifecycle_integration", "test_process_restart_recovery",
    "test_risk_management")


@pytest.fixture(scope="module")
def text():
    with open(WORKFLOW, encoding="utf-8") as fh:
        return fh.read()


def test_it_runs_on_pull_requests_and_pushes_to_main(text):
    # Text check on purpose: CI installs only requirements.txt + pytest, so this
    # test must not need a YAML library.
    assert re.search(r'^on:\n  pull_request:\n  push:\n    branches: \[main, "fix/\*\*"\]\n',
                     text, re.M), "triggers must include pull_request, main pushes, and fix-branch pushes"


def test_every_test_file_it_runs_exists_and_the_required_ones_are_present(text):
    named = re.findall(r"tests/(test_[A-Za-z0-9_]+\.py)", text)
    assert named, "the workflow names no tests"
    for name in named:
        assert os.path.exists(os.path.join(HERE, name)), name
    for needed in REQUIRED:
        assert f"{needed}.py" in named, needed


def test_it_runs_the_fly_check_and_a_checkout_safe_diff_check(text):
    assert "python tools/fly_stack.py --check" in text
    assert "git diff --check HEAD^1 HEAD" in text         # the change, not the tree
    assert "fetch-depth: 2" in text


def test_it_uses_the_supported_pythons(text):
    assert '"3.11"' in text and '"3.12"' in text
    with open(os.path.join(BOT, "Dockerfile"), encoding="utf-8") as fh:
        assert "python:3.11" in fh.read()


def test_it_needs_no_secret_and_cannot_reach_a_venue_or_trade(text):
    lowered = text.lower()
    assert "secrets." not in lowered
    for forbidden in ("bybit_api_key", "bybit_api_secret",
                      "testnet_api", "api-testnet", "api.bybit"):
        assert forbidden not in lowered, forbidden
    assert re.search(r"^permissions:\n  contents: read\n", text, re.M), \
        "the workflow must grant read-only repository access and nothing else"
    assert len(re.findall(r"^\s*(?:id-token|pull-requests|issues|actions|"
                          r"packages|deployments):\s*write", text, re.M)) == 0


def test_it_hides_no_failure(text):
    assert "continue-on-error" not in text
    assert "|| true" not in text
    assert "--ignore" not in text and "-k " not in text and "deselect" not in text
    assert "xfail" not in text.lower()


def test_it_does_not_pretend_to_be_the_full_suite(text):
    assert "pytest -q -p no:cacheprovider tests\n" not in text
    assert "61 failures" in text and "NOT run or hidden" in text


# ---------------------------------------------------------------------------
# the production-image job: the REAL image, built here, imported offline
# ---------------------------------------------------------------------------

def _image_job(text):
    m = re.search(r"^  production-image:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
                  text, re.M | re.S)
    assert m, "the workflow has no production-image job"
    return m.group(1)


def _runs(job):
    """Each `docker run ...` command with line continuations joined."""
    joined = re.sub(r"\\\n\s*", " ", job)
    return re.findall(r"docker run [^\n]+", joined)


def test_the_image_job_is_an_ordinary_always_run_job(text):
    job = _image_job(text)
    assert "runs-on: ubuntu-latest" in job
    assert re.search(r"timeout-minutes: \d+", job)
    for hidden in ("continue-on-error", "|| true", "needs:", "matrix:"):
        assert hidden not in job, hidden
    # no job-level or step-level condition can skip it (the one `if: always()`
    # belongs to the summary step, which only writes text)
    conds = re.findall(r"^\s+if: (.+)$", job, re.M)
    assert conds == ["always()"], conds
    assert re.search(r"if: always\(\)\n\s+run: \|\n\s+echo \"### production-image\"",
                     job)


def test_the_matrix_job_and_its_checks_are_untouched_by_the_image_job(text):
    main = text.split("\n  production-image:")[0]
    for kept in ('python: ["3.11", "3.12"]', "tests/test_carry_hold_audit.py",
                 "python tools/fly_stack.py --check",
                 "git diff --check HEAD^1 HEAD", "fetch-depth: 2",
                 "tests/test_dockerfile.py", "tests/test_image_ships_the_tools.py"):
        assert kept in main, kept


def test_the_image_is_built_from_the_production_dockerfile_and_context(text):
    job = _image_job(text)
    builds = re.findall(r"docker build [^\n]+", job)
    assert len(builds) == 1, builds
    b = builds[0]
    assert b.endswith(" ./bot") and '--tag "$IMAGE"' in b and "--pull" in b
    for other in (" -f ", "--file", "--push", "--output", "--load", "--cache-from"):
        assert other not in b, other
    assert "docker push" not in job and "docker login" not in job
    assert "docker/build-push-action" not in job and "docker/login-action" not in job
    assert re.search(r"IMAGE: ai-agent-control-carry-smoke:\$\{\{ github\.sha \}\}", job)
    assert not os.path.exists(os.path.join(BOT, "Dockerfile.smoke"))


def test_every_smoke_runs_the_image_this_job_built_and_cannot_start_the_app(text):
    job = _image_job(text)
    runs = _runs(job)
    assert len(runs) == 2, runs
    for r in runs:
        assert r.endswith(("print(\"production imports OK\")'",
                           "shipped modules as uid\", os.getuid())'")), r
        # the same variable the build tagged, never a fetched or cached image
        assert ' "$IMAGE" ' in r
        assert "ai-agent-control-carry-smoke:" not in r
        # the entrypoint is replaced; the trading entrypoint cannot run
        assert "--entrypoint python3 " in r
        for flag in ("--network none", "--read-only", "--cap-drop=ALL",
                     "--security-opt=no-new-privileges", "--pids-limit 64",
                     "--tmpfs /app/state:rw,noexec,nosuid,size=16m"):
            assert flag in r, (flag, r)
        # nothing host-side, privileged or credential-bearing
        for banned in (" -v ", "--volume", "--mount", "--env-file", "--privileged",
                       " --user", " -u ", "--network host", "--cap-add",
                       "--pid=", "--pid ", "--device", "--add-host", "-p "):
            assert banned not in r, (banned, r)
        code = r.split("-c ", 1)[1]
        assert "__main__" not in code and "runpy" not in code
        assert "main.main" not in code and "TradingBot" not in code
        assert "except" not in code and "try" not in code      # no swallowed import
    assert "import main; import funding_reconcile;" in runs[0]
    assert "os.getuid() != 0" in runs[1] and "import_module" in runs[1]


def test_the_only_trading_environment_the_image_job_sets_is_the_safe_one(text):
    job = _image_job(text)
    for r in _runs(job):
        envs = re.findall(r"-e (\S+)", r)
        assert envs == ["USE_TESTNET=1", "PAPER_TRADING=1", "LIVE_TRADING_ACK="], envs
    assert "secrets." not in job and "BYBIT" not in job.upper().replace(
        "BYBIT_VENUE", "")
    # no occurrence of these names anywhere in the workflow except those flags
    for name in ("live_trading_ack", "paper_trading"):
        for m in re.finditer(name, text, re.I):
            line = text[text.rfind("\n", 0, m.start()) + 1:text.find("\n", m.end())]
            assert re.search(r"-e (USE_TESTNET=1|PAPER_TRADING=1|LIVE_TRADING_ACK=) ",
                             line + " "), line


def test_the_image_properties_the_smoke_relies_on_are_in_the_dockerfile():
    with open(os.path.join(BOT, "Dockerfile"), encoding="utf-8") as fh:
        d = fh.read()
    assert re.search(r"^USER bot$", d, re.M)                     # unprivileged
    assert re.search(r'^VOLUME \["/app/state"\]$', d, re.M)    # the smoke's tmpfs
    assert re.search(r'^ENTRYPOINT \["python3", "main.py"\]$', d, re.M)
    assert "funding_reconcile.py" in d
    assert not re.search(r"^COPY \.\s", d, re.M)
    assert "COPY tools/ " not in d
    assert re.search(r"^ENV USE_TESTNET=1 \\\n\s+PAPER_TRADING=1", d, re.M)
