"""Round 22: the offline deployability check agrees with the committed files, FAILS when they disagree, and never says anything stronger than "offline checks pass, owner steps pending"."""
from __future__ import annotations

import json
import os
import re
import shutil
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
REPO = os.path.dirname(BOT)
sys.path[:0] = [BOT, os.path.join(BOT, "tools")]

import deploy_check as DC  # noqa: E402


@pytest.fixture(scope="module")
def checkout(tmp_path_factory):
    """A trimmed copy of the checkout (the files the checks read) that a test may break without touching the repository."""
    dst = tmp_path_factory.mktemp("co")
    shutil.copytree(BOT, dst / "bot", ignore=shutil.ignore_patterns("tests", "data", "artifacts", "__pycache__", "*.pyc", ".pytest_cache", "state", "runs"))
    shutil.copytree(os.path.join(REPO, "supabase"), dst / "supabase")
    return dst


@pytest.fixture
def co(checkout, tmp_path):
    dst = tmp_path / "co"
    shutil.copytree(checkout, dst)
    return dst


def rows(res, app):
    return {c["id"]: c for a in res["apps"] if a["app"] == app for c in a["checks"]}


def test_the_committed_executor_files_agree_and_the_verdict_is_never_stronger_than_pending_owner_steps():
    res = DC.run(BOT, ["carry-autopilot"])
    assert res["overall"] == DC.PASS_OWNER_PENDING, [c for a in res["apps"] for c in a["checks"] if not c["ok"]]
    assert set(rows(res, "carry-autopilot")) == {"T1_TOML", "T2_DOCKERFILE", "T3_PATHS", "T4_SECRET_NAMES", "T5_RPC", "T6_DEPLOY_COMMAND"}
    assert "NOT a deployment" in res["claim"] and "NOT a readiness statement" in res["claim"]
    assert len(res["owner_steps"]) >= 7 and any("fly secrets list" in s for s in res["owner_steps"]) and any("migration" in s for s in res["owner_steps"])
    blob = json.dumps(res).lower()
    assert "ready to deploy" not in blob and '"go"' not in blob and "deployed:" not in blob


def test_the_recorder_files_agree_too(checkout):
    res = DC.run(str(checkout / "bot"), ["carry-drill"])
    assert res["overall"] == DC.PASS_OWNER_PENDING, [c for a in res["apps"] for c in a["checks"] if not c["ok"]]


def edit(path, old, new, count=1):
    t = open(path, encoding="utf-8").read()
    assert t.count(old) >= 1, old
    open(path, "w", encoding="utf-8").write(t.replace(old, new, count))


@pytest.mark.parametrize("name,rel,old,new,check", [
    ("wrong_app_name", "fly.autopilot.toml", 'app = "carry-autopilot"', 'app = "carry-book"', "T1_TOML"),
    ("second_volume", "fly.autopilot.toml", '[[mounts]]\nsource = "autopilot_data"\ndestination = "/data"', '[[mounts]]\nsource = "autopilot_data"\ndestination = "/data"\n\n[[mounts]]\nsource = "x"\ndestination = "/y"', "T1_TOML"),
    ("public_service", "fly.autopilot.toml", "[build]", "[http_service]\ninternal_port = 8080\n\n[build]", "T1_TOML"),
    ("mainnet_env", "fly.autopilot.toml", 'USE_TESTNET = "1"', 'USE_TESTNET = "0"', "T1_TOML"),
    ("arming_name_in_toml", "fly.autopilot.toml", 'HOME = "/data"', 'HOME = "/data"\nLIVE_SOMETHING = "1"', "T1_TOML"),
    ("secret_in_toml", "fly.autopilot.toml", 'HOME = "/data"', 'HOME = "/data"\nBYBIT_TESTNET_API_SECRET = "x"', "T1_TOML"),
    ("restart_never", "fly.autopilot.toml", 'policy = "always"', 'policy = "no"', "T1_TOML"),
    ("build_commit_default", "Dockerfile.autopilot", "ARG BUILD_COMMIT\n", "ARG BUILD_COMMIT=unknown\n", "T2_DOCKERFILE"),
    ("no_manifest_build", "Dockerfile.autopilot", "runtime_provenance.py build", "runtime_provenance.py noop", "T2_DOCKERFILE"),
    ("entrypoint_does_not_verify", "deploy/autopilot-entrypoint.sh", "runtime_provenance.py verify", "runtime_provenance.py status", "T2_DOCKERFILE"),
    ("entrypoint_wrong_class", "deploy/autopilot-entrypoint.sh", "--expect TESTNET_EXECUTOR", "--expect READ_ONLY_RECORDER", "T2_DOCKERFILE"),
    ("entrypoint_does_not_refuse", "deploy/autopilot-entrypoint.sh", "exit 78", "exit 0", "T2_DOCKERFILE"),
    ("missing_copy_source", "Dockerfile.autopilot", "tools/eligibility_gate.py", "tools/does_not_exist.py", "T3_PATHS"),
    ("secret_name_undocumented", "RUNBOOKS.md", "SUPABASE_OWNER_ID", "SUPABASE_OWNER_X", "T4_SECRET_NAMES"),
    ("arming_name_in_dockerfile", "Dockerfile.autopilot", "ENV USE_TESTNET=1", "ENV LIVE_X=1 USE_TESTNET=1", "T4_SECRET_NAMES"),
    ("rpc_not_migrated", "tools/exec_evidence.py", '"exec_lease_renew"', '"exec_lease_renew_v2"', "T5_RPC"),
    ("deploy_without_build_commit", "deploy/safe-deploy.sh", '--build-arg BUILD_COMMIT="$(git rev-parse HEAD)"', "", "T6_DEPLOY_COMMAND"),
])
def test_each_disagreement_between_the_committed_pieces_is_a_no_go(co, name, rel, old, new, check):
    root = str(co / "bot")
    if rel == "RUNBOOKS.md":
        t = open(os.path.join(root, rel), encoding="utf-8").read()
        open(os.path.join(root, rel), "w", encoding="utf-8").write(t.replace(old, new))               # every mention
    else:
        edit(os.path.join(root, rel), old, new)
    res = DC.run(root, ["carry-autopilot"])
    assert res["overall"] == DC.NO_GO and not rows(res, "carry-autopilot")[check]["ok"], (name, rows(res, "carry-autopilot")[check])


def test_a_missing_migration_directory_or_toml_is_a_no_go_not_a_pass(co):
    shutil.rmtree(co / "supabase" / "migrations")
    assert not rows(DC.run(str(co / "bot"), ["carry-autopilot"]), "carry-autopilot")["T5_RPC"]["ok"]
    os.remove(co / "bot" / "fly.autopilot.toml")
    res = DC.run(str(co / "bot"), ["carry-autopilot"])
    assert res["overall"] == DC.NO_GO and not rows(res, "carry-autopilot")["T1_TOML"]["ok"]


def test_the_drill_deploy_command_must_carry_the_build_commit_and_name_its_own_toml(co):
    root = str(co / "bot")
    res = DC.run(root, ["carry-drill"])
    assert rows(res, "carry-drill")["T6_DEPLOY_COMMAND"]["ok"]
    t = open(os.path.join(root, "RUNBOOKS.md"), encoding="utf-8").read()
    open(os.path.join(root, "RUNBOOKS.md"), "w", encoding="utf-8").write(re.sub(r"--build-arg BUILD_COMMIT=\S+", "", t))
    assert not rows(DC.run(root, ["carry-drill"]), "carry-drill")["T6_DEPLOY_COMMAND"]["ok"]


def test_the_command_line_exit_codes_and_that_it_writes_nothing(co, capsys):
    root = str(co / "bot")
    before = sorted((os.path.relpath(os.path.join(r, f), root), os.path.getmtime(os.path.join(r, f))) for r, _d, fs in os.walk(root) for f in fs)
    assert DC.main(["--root", root, "--app", "carry-autopilot"]) == 0
    assert "OFFLINE_CHECKS_PASS_OWNER_STEPS_PENDING" in capsys.readouterr().out
    assert DC.main(["--root", root, "--app", "carry-autopilot", "--json"]) == 0 and json.loads(capsys.readouterr().out)["schema"] == DC.SCHEMA
    assert DC.main(["--root", str(co / "nope")]) == 2
    after = sorted((os.path.relpath(os.path.join(r, f), root), os.path.getmtime(os.path.join(r, f))) for r, _d, fs in os.walk(root) for f in fs)
    assert before == after


def test_the_module_has_no_network_environment_or_subprocess():
    src = open(os.path.join(BOT, "tools", "deploy_check.py"), encoding="utf-8").read()
    for banned in ("urllib", "socket", "requests", "os.environ", "getenv", "subprocess", "http.client", "os.system", ".write(", "open(path, \"w\""):
        assert banned not in src, banned
