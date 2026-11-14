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
    "test_close_plan_absence_proof",
    "test_close_policy",
    "test_close_store",
    "test_close_execution_faults",
    "test_close_engine_integration",
    "test_close_broker_hooks",
    "test_close_real_stack",
    "test_btc_hold_benchmark",
    "test_carry_funding_truth", "test_carry_fee_truth", "test_carry_report",
    "test_carry_broker", "test_carry_preflight", "test_fee_payload_probe",
    "test_carry_live_gates", "test_carry_funding_history_gap",
    # a blanked ledger row must refuse to start, not read as a first run
    "test_carry_cold_start", "test_carry_entry_allowance_survives_restart",
    # the money path of the directional book: exits, funding, lifecycles
    "test_provisional_exit_reconciliation", "test_directional_funding",
    "test_position_lifecycles", "test_observe_exits",
    "test_unpriced_exit_visibility", "test_funding_sign_lock",
    "test_carry_hold_session", "test_drill",
    "test_simulator_fill_contract", "test_funding_window", "test_carry_latency", "test_carry_cost_inputs", "test_carry_shadow_forward", "test_registry_matches_production_rule", "test_carry_cost_reality", "test_gate_arithmetic_overflow", "test_entry_gate_parity", "test_mainnet_public", "test_mainnet_collect", "test_mainnet_protocol", "test_monitor_self_heal", "test_mainnet_stall_alert", "test_watch_workflow", "test_mainnet_independent_prints", "test_snapshot_nonfinite_fail_closed", "test_broker_unstated_fields_fail_closed", "test_liquidation_view_absent_fields", "test_mainnet_book", "test_registry_search_width", "test_runner_alerts", "test_runner_alerts_reliability", "test_records_consistent_alias", "test_carry_engine_boundaries", "test_carry_risk_boundaries", "test_carry_unwind_partial", "test_monitor_cycle_fail_closed", "test_margin_nan_fail_closed", "test_liquidation_gate_fail_closed", "test_monitor_fail_closed", "test_sweep_abi", "test_gate_boundary_equality", "test_carry_backtest", "test_carry_walkforward", "test_multi_asset_carry", "test_fly_runtime", "test_venue_isolation", "test_drill_evidence", "test_evidence_reconcile", "test_evidence_reconcile_trades", "test_evidence_reconcile_carry", "test_drill_final_state", "test_drill_final_state_tool", "test_artefact_log_dedupe", "test_exit_proof_retry", "test_close_testnet_short", "test_close_testnet_experiment", "test_real_stack_adapter", "test_next_settlement", "test_recon_summary", "test_carry_broker_position_rows", "test_unattended_workflow", "test_unattended_drill", "test_runner_monitor", "test_monitor_summary", "test_drill_image", "test_fly_machines", "test_exit_execution_ids", "test_private_ws_consumer", "test_conformance_evidence_end_to_end", "test_venue_evidence", "test_linear_simulator", "test_monitor_mid_history_unreadable", "test_promotion_gate_cited_evidence_exists", "test_sync_linear_stop_gate_evidence",
    # live arming and the order/exit path
    "test_config", "test_live_promotion_check", "test_micro_live_preflight", "test_bybit_connection", "test_kill_switch_drill", "test_orchestrator", "test_naked_position_handling", "test_directional_close_unconfirmed",
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
    # the gate says plainly it is not the full suite, and the job that runs the
    # other files exists; the old 61-failure baseline is recorded as NOT
    # reproduced rather than silently dropped
    assert "WHAT THIS IS NOT: the repository's full suite" in text
    assert "rest-of-suite" in text and "NOT reproduced" in text


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


# ---------------------------------------------------------------------------
# the rest-of-suite job: every test file the correctness gate does not name
# ---------------------------------------------------------------------------

def _rest_job(text):
    m = re.search(r"^  rest-of-suite:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
                  text, re.M | re.S)
    assert m, "the workflow has no rest-of-suite job"
    return m.group(1)


def test_the_rest_of_the_suite_is_derived_not_listed(text):
    job = _rest_job(text)
    # the file list is a set difference against what the gate names, so a new
    # test file is covered by one job or the other and cannot fall between them
    assert "comm -23" in job and "tests/test_*.py" in job
    assert "carry-verify.yml" in job
    # no hand-maintained list of test files inside this job
    assert not re.search(r"^\s+tests/test_[A-Za-z0-9_]+\.py\s*$", job, re.M)


def test_the_rest_of_the_suite_is_offline_and_holds_no_secret(text):
    job = _rest_job(text)
    assert "secrets." not in job and "FLY_" not in job and "BYBIT" not in job
    assert "REQUIRE_NATIVE_CORE" in job and "make -C ../cpp" in job
    assert re.search(r"timeout-minutes: \d+", job)
    # a full history: some tests read git history (repo shape, provenance)
    assert "fetch-depth: 0" in job


def test_the_rest_of_the_suite_excludes_only_the_documented_freshness_marker(text):
    job = _rest_job(text)
    assert '-m "not data_freshness"' in job
    for hidden in ("continue-on-error", "|| true", "--deselect", "--ignore",
                   "-k "):
        assert hidden not in job, hidden


# ---------------------------------------------------------------------------
# red-team round 2: the derived "rest of the suite" list must come from the pytest ARGUMENT BLOCKS only, never from
# any text that happens to mention a test file (a comment, an echo, a doc line).
# ---------------------------------------------------------------------------
import shutil
import subprocess

REPO = os.path.dirname(BOT)
AWK = os.path.join(REPO, ".github", "scripts", "named_tests.awk")
TEST_TOKEN = re.compile(r"tests/(test_[A-Za-z0-9_]+\.py)")


def _blocks_in_python(text):
    """An independent (indentation based) parse: the `tests/test_*.py` tokens of every `python -m pytest` command,
    i.e. the command line and its continuation lines up to the first blank line, comment, or new YAML key/item."""
    named, inside = [], False
    for line in text.splitlines():
        start = re.match(r"\s*(?:run:\s*[>|]?\s*)?python -m pytest(?:\s|$)", line)
        if not inside and start:
            inside = True
        elif inside and (not line.strip() or line.lstrip().startswith("#") or re.match(r"\s*-\s", line)
                         or (re.match(r"\s*[A-Za-z_-]+:(\s|$)", line) and not line.lstrip().startswith("run:"))):
            inside = False
        if inside:
            named += TEST_TOKEN.findall(line)
    return sorted(set(named))


def _awk(text, tmp_path):
    assert shutil.which("awk"), "awk is needed (it is on every ubuntu runner)"
    f = tmp_path / "wf.yml"
    f.write_text(text, encoding="utf-8")
    out = subprocess.run(["awk", "-f", AWK, str(f)], capture_output=True, text=True, check=True).stdout
    return sorted({t[len("tests/"):] for t in out.split()})


def _tests_in_blocks_named(text):
    return sorted({m for m in _blocks_in_python(text)})


def test_the_derived_rest_list_is_driven_by_the_pytest_blocks_not_by_the_whole_file(text, tmp_path):
    assert os.path.exists(AWK), "the extractor script is missing"
    from_awk = _awk(text, tmp_path)
    assert from_awk == _blocks_in_python(text)
    assert len(from_awk) > 300                              # the real gate list, not an empty match
    # the rest job calls THAT extractor, not a grep over the file
    job = _rest_job(text)
    assert "named_tests.awk" in job
    assert not re.search(r"grep -oE 'tests/test_", job), "the whole-file grep is back"
    # it fails loudly if the workflow names a file that does not exist
    assert "comm -13" in job


def test_a_comment_or_echo_mentioning_a_test_file_does_not_exclude_it_from_the_rest_job(text, tmp_path):
    victim = "test_carry_latency.py"
    listed = f"          tests/{victim}\n"
    assert listed in text
    stripped = text.replace(listed, "", 1)
    assert victim not in _awk(stripped, tmp_path)           # not listed: not named (positive control for the next lines)
    decorated = (stripped.replace("      - name: Scope of this gate\n",
                                  f"      # tests/{victim} is mentioned here in prose only\n"
                                  f"      - name: Scope of this gate\n", 1)
                 .replace('echo "### carry-verify', f'echo "see tests/{victim} ### carry-verify', 1)
                 .replace("# WHAT THIS IS NOT", f"# tests/{victim}\n# WHAT THIS IS NOT", 1))
    assert f"tests/{victim}" in decorated and decorated != stripped
    assert victim not in _awk(decorated, tmp_path), "a mention in a comment/echo excluded the file from CI"
    assert victim in _awk(text, tmp_path)                   # listed in the block: named
    # a single-line `run: python -m pytest tests/x.py` is also a block
    one = "      - run: x\n        run: python -m pytest -q tests/test_alpha.py tests/test_beta.py\n\n"
    assert _awk(one, tmp_path) == ["test_alpha.py", "test_beta.py"]
    two = "        run: |\n          echo python -m pytest tests/test_nope.py\n          grep tests/test_nope2.py x\n"
    assert _awk(two, tmp_path) == []
    # a block ends at a comment, a blank line, a new list item or a new key: text after it is not an argument
    head = "        run: >\n          python -m pytest -q\n          tests/test_a.py\n"
    for tail in ("          # tests/test_b.py\n", "\n          tests/test_b.py\n", "      - name: x\n        run: echo tests/test_b.py\n",
                 "        env:\n          X: tests/test_b.py\n"):
        assert _awk(head + tail, tmp_path) == ["test_a.py"], tail


def test_each_test_file_is_listed_in_exactly_one_gate_line_and_the_old_comment_mention_is_gone(text):
    lines = [l.strip() for l in text.splitlines() if re.fullmatch(r"\s+tests/test_[A-Za-z0-9_]+\.py", l)]
    dupes = sorted({l for l in lines if lines.count(l) > 1})
    assert dupes == [], dupes
    assert "tests/test_entry_gate_parity.py SKIPS" not in text
    assert text.count("test_entry_gate_parity") == 1


def test_the_gate_list_equals_the_files_the_blocks_name_and_all_exist(text):
    blocks = _blocks_in_python(text)
    for name in blocks:
        assert os.path.exists(os.path.join(HERE, name)), name
    for needed in REQUIRED:
        assert f"{needed}.py" in blocks, needed


# ---- the executed-Postgres job -------------------------------------------------------------------------------

def _pg_job(text):
    m = re.search(r"^  supabase-migrations:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", text, re.M | re.S)
    assert m, "the workflow has no supabase-migrations job: the RLS/append-only tests would only ever SKIP in CI"
    return m.group(1)


def test_a_dedicated_job_executes_the_supabase_tests_and_cannot_skip_them(text):
    job = _pg_job(text)
    assert 'REQUIRE_PG: "1"' in job
    assert {"test_supabase_migrations.py", "test_supabase_sync.py"} <= set(_blocks_in_python(job))
    # exactly one job runs them: the gate list must not also name them (a skipping copy is worse than none)
    assert "test_supabase_migrations.py" not in _blocks_in_python(text.split("\n  supabase-migrations:")[0])
    assert re.search(r"-c constraints\.txt[^\n]*\bpgserver\b", job), "pgserver must be installed under the constraints pin"
    for hidden in ("continue-on-error", "|| true", "--deselect", "--ignore", "-k ", "secrets.", "if:"):
        assert hidden not in job, hidden
    assert re.search(r"timeout-minutes: \d+", job)
    for py in ('"3.11"', '"3.12"'):
        assert py in job
    with open(os.path.join(BOT, "constraints.txt"), encoding="utf-8") as fh:
        pins = fh.read()
    assert re.search(r"^pgserver==\d+\.\d+\.\d+\s*$", pins, re.M)
    assert re.search(r"^pytest==\d+\.\d+\.\d+\s*$", pins, re.M)


def test_the_pg_requirement_is_enforced_by_the_supabase_test_itself():
    with open(os.path.join(HERE, "test_supabase_migrations.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert "REQUIRE_PG" in src and 'importorskip("pgserver"' not in src


def _overclaims(text):
    """Phrases the workflow must not use: they claim more than the jobs deliver."""
    bad = [p for p in ("together they cover the whole suite", "(cannot skip)", "cannot skip)") if p in text]
    if "run in NO job" not in text:
        bad.append("missing: data_freshness tests run in NO job")
    return bad


def test_rt4_6_the_overclaim_detector_can_fail(text):
    assert _overclaims(text) == []
    assert _overclaims(text + "\n# together they cover the whole suite") and _overclaims(text.replace("(a missing or broken", "(cannot skip) (a missing or broken"))
    assert _overclaims(text.replace("run in NO job", "run in some job"))


def test_rt4_6_the_workflow_does_not_overclaim_coverage_or_skip_proofs(text):
    # tests marked data_freshness are excluded by the rest job and named by no other: they run in NO job, and the comments must say so
    assert _overclaims(text) == []
    assert text.count('-m "not data_freshness"') == 1 and "-m data_freshness" in text.replace("`pytest -m data_freshness`", "-m data_freshness")
    # the statement is true only while such tests exist: if none do, the comment is stale and this test says so
    marked = 0
    for f in os.listdir(os.path.join(BOT, "tests")):
        if f.startswith("test_") and f.endswith(".py"):
            marked += open(os.path.join(BOT, "tests", f), encoding="utf-8").read().count("pytest.mark.data_freshness")
    assert marked > 0, "no test carries data_freshness any more: delete the 'run in NO job' caveat and the deselection"
    job = _pg_job(text)
    assert "-k " not in job and "--deselect" not in job and " -m \"" not in job


def test_rt4_6_both_pg_test_files_route_their_fixture_through_require_pg():
    for name in ("test_supabase_migrations.py", "test_supabase_sync.py"):
        src = open(os.path.join(HERE, name), encoding="utf-8").read()
        assert "def _unavailable(" in src and "REQUIRE_PG" in src and "importorskip(" + '"pgserver"' not in src, name
        assert len(re.findall(r"pytest\.skip\(", src)) == 1, name


def test_every_action_is_pinned_by_sha_and_every_workflow_is_read_only():
    wf_dir = os.path.join(REPO, ".github", "workflows")
    for name in sorted(os.listdir(wf_dir)):
        body = open(os.path.join(wf_dir, name), encoding="utf-8").read()
        for ref in re.findall(r"^\s*-?\s*uses:\s*(\S+)", body, re.M):
            assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref), (name, ref)
        assert re.search(r"^permissions:\n  contents: read\n", body, re.M), name
    for name in ("carry-verify.yml", "security.yml"):
        assert "secrets." not in open(os.path.join(wf_dir, name), encoding="utf-8").read().lower(), name


def test_the_python_compat_workflow_covers_the_newest_interpreters_and_the_help_string_proof():
    """Round 21: Python 3.14 refuses a literal `%` in an argparse help string when the parser is BUILT, which broke the owner's `ms_testnet_autopilot.py status` while every 3.11/3.12 job was green."""
    body = open(os.path.join(REPO, ".github", "workflows", "python-compat.yml"), encoding="utf-8").read()
    assert 'python: ["3.13", "3.14"]' in body
    assert "tests/test_cli_help_strings.py" in body and "tests/test_docs_topology_guard.py" in body
    assert "secrets." not in body.lower() and "BYBIT" not in body and "LIVE_TRADING_ACK" not in body
    assert re.search(r"^permissions:\n  contents: read\n", body, re.M)
