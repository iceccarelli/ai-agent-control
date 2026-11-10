"""The Codespaces operations layer (Makefile, ops/collector_supervisor.py, .devcontainer): it must stay free of any order path and any live switch, its supervisor policy must
restart what a restart can fix and stop (with an ALERT) on what only a human can, and every tool path the Makefile names must exist. Offline; nothing here starts a process
that touches the network."""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import types

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(BOT)
MAKEFILE = os.path.join(REPO, "Makefile")
SUP = os.path.join(REPO, "ops", "collector_supervisor.py")


def _load():
    spec = importlib.util.spec_from_file_location("collector_supervisor", SUP)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


S = _load()
FORBIDDEN = ("execute", "--unwind", "LIVE_TRADING_ACK", "USE_TESTNET=0", "PAPER_TRADING=0", "BYBIT_API_KEY", "BYBIT_API_SECRET", "mainnet", "api.bybit.com", "ms_testnet_capability",
             "confirm-plan-digest")


def _text(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------- no order path, no live switch


@pytest.mark.parametrize("path", [MAKEFILE, SUP, os.path.join(REPO, ".devcontainer", "devcontainer.json")])
def test_the_ops_layer_has_no_order_path_and_no_live_switch(path):
    low = _text(path)
    for token in FORBIDDEN:
        # the Makefile and devcontainer MAY mention the word in a comment explaining its absence; never in an active line
        active = [ln for ln in low.splitlines() if token.lower() in ln.lower() and not ln.lstrip().startswith("#")]
        assert active == [], (path, token, active[:2])


def test_the_makefile_documents_that_the_execute_step_is_not_a_target():
    t = _text(MAKEFILE)
    targets = set(re.findall(r"^([a-z][a-z0-9-]*):", t, flags=re.M))
    assert not [x for x in targets if "exec" in x or "order" in x or "live" in x or "unwind" in x], targets
    assert {"collect-start", "collect-stop", "testnet-preflight", "scope", "diagnose", "backfill", "audit"} <= targets


def test_every_tool_the_makefile_runs_exists():
    for rel in sorted(set(re.findall(r"tools/([a-z_]+\.py)", _text(MAKEFILE)))):
        assert os.path.isfile(os.path.join(BOT, "tools", rel)), rel
    assert os.path.isfile(SUP)


@pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")
def test_every_target_parses_with_a_dry_run(tmp_path):
    t = _text(MAKEFILE)
    for target in sorted(set(re.findall(r"^([a-z][a-z0-9-]*):", t, flags=re.M))):
        r = subprocess.run(["make", "-n", target, f"HOME={tmp_path}", f"DATA={tmp_path}/d", "UNI=/x/u", "HIST=/x/h", "START=1", "END=2", "SPOT=/x/s"], cwd=REPO, capture_output=True, text=True)
        assert r.returncode == 0, (target, r.stderr[:300])


# ---------------------------------------------------------------- devcontainer


def test_the_devcontainer_is_valid_private_and_carries_no_secret_value():
    d = json.loads(_text(os.path.join(REPO, ".devcontainer", "devcontainer.json")))
    assert d["image"].endswith("3.12-bookworm") or "3.11" in d["image"]            # the tested interpreters
    assert d["portsAttributes"]["8787"]["visibility"] == "private"
    assert set(d["secrets"]) == {"BYBIT_TESTNET_API_KEY", "BYBIT_TESTNET_API_SECRET"}
    assert all(set(v) == {"description"} for v in d["secrets"].values())              # a description only, never a value
    assert d["remoteEnv"] == {"PAPER_TRADING": "1", "USE_TESTNET": "1"}               # the SAFE values only
    assert d["postStartCommand"] == "make ops-resume"                                # resumes only what the owner enabled; never starts anything new
    assert "execute" not in json.dumps(d).lower()


# ---------------------------------------------------------------- supervisor policy


def test_decide_table():
    d = S.decide
    assert d(0, {"status": "STOPPED"}, 0, 6, False)["action"] == "next_segment"
    assert d(0, {}, 0, 6, True) == {"action": "stop", "reason": "STOP_FILE", "delay_s": 0}
    r = d(1, {"status": "REFUSED", "refusal": "LOW_DISK"}, 0, 6, False)
    assert r["action"] == "stop" and "LOW_DISK" in r["reason"]                          # a retry loop cannot fix a refusal
    assert d(0, {"status": "CRASHED"}, 0, 6, False)["action"] == "backoff"              # exit 0 with a crashed health is not success
    delays = [d(1, {}, n, 99, False)["delay_s"] for n in range(8)]
    assert delays == sorted(delays) and delays[0] == 15 and max(delays) == 600
    assert d(1, {}, 5, 6, False)["action"] == "stop"


def _args(tmp_path, **kw):
    a = S.parser().parse_args(["--root", str(tmp_path / "root"), "--state-dir", str(tmp_path / "st"), "--stop-file", str(tmp_path / "STOP"), "--seal-manifest", str(tmp_path / "seal.jsonl"),
                               "--receipts", str(tmp_path / "rec.jsonl"), "--backup-to", str(tmp_path / "bk")])
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def test_a_crash_is_restarted_with_backoff_then_the_next_segment_runs(tmp_path):
    codes = iter([1, 1, 0, 0])
    sleeps, cmds = [], []
    a = _args(tmp_path, max_segments=4)
    rc = S.run(a, runner=lambda c: (cmds.append(list(c)), next(codes))[1], sleeper=sleeps.append, log=lambda m: None)
    assert rc == 0 and len(cmds) == 4 and sleeps[:2] == [15.0, 30.0] and sleeps[2] == 5
    assert all("run-continuous" in c and "--stop-file" in c for c in cmds)
    assert not os.path.exists(tmp_path / "st" / "ALERT")


def test_repeated_failure_stops_and_raises_an_alert(tmp_path):
    a = _args(tmp_path, max_restarts=3)
    rc = S.run(a, runner=lambda c: 1, sleeper=lambda s: None, log=lambda m: None)
    assert rc == 1 and "TOO_MANY_FAILURES" in (tmp_path / "st" / "ALERT").read_text()


def test_a_collector_refusal_stops_the_supervisor_instead_of_looping(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "health.json").write_text(json.dumps({"status": "REFUSED", "refusal": "FREE_SPACE_BELOW_RESERVE"}))
    calls = []
    rc = S.run(_args(tmp_path), runner=lambda c: (calls.append(c), 2)[1], sleeper=lambda s: None, log=lambda m: None)
    assert rc == 1 and len(calls) == 1 and "FREE_SPACE_BELOW_RESERVE" in (tmp_path / "st" / "ALERT").read_text()


def test_the_stop_file_is_a_clean_stop_and_nothing_starts_while_it_exists(tmp_path):
    (tmp_path / "STOP").write_text("")
    calls = []
    assert S.run(_args(tmp_path), runner=lambda c: calls.append(c), sleeper=lambda s: None, log=lambda m: None) == 0 and calls == []


def test_the_supervisor_refuses_any_path_inside_the_git_work_tree(tmp_path):
    for field, val in (("root", os.path.join(REPO, "data")), ("state_dir", os.path.join(REPO, "st")), ("stop_file", os.path.join(REPO, "STOP")),
                       ("seal_manifest", os.path.join(REPO, "seal.jsonl")), ("backup_to", os.path.join(REPO, "bk"))):
        a = _args(tmp_path)
        setattr(a, field, val)
        calls = []
        assert S.run(a, runner=lambda c: calls.append(c), sleeper=lambda s: None, log=lambda m: None) == 2 and calls == [], field


def test_the_command_is_the_documented_public_collector_with_its_hooks(tmp_path):
    cmd = S.build_command(_args(tmp_path), 3600)
    assert cmd[1].endswith(os.path.join("bot", "tools", "market_collector.py")) and cmd[2] == "run-continuous"
    for flag in ("--seal-manifest", "--backup-to", "--receipts", "--restore-check-every", "--stop-file", "--max-symbols"):
        assert flag in cmd


# ---------------------------------------------------------------- one documented layout, a control centre that sees what the pipeline wrote, and the resume / status recipes
RUNBOOKS = os.path.join(BOT, "RUNBOOKS.md")
needs_make = pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")


def _make(args, tmp_path, **env):
    # run as a TOP-LEVEL make: when the suite itself runs under `make test-quick`, an inherited MAKELEVEL/MAKEFLAGS makes this child print
    # "make[1]: Leaving directory", which landed after the `ln -sfn` line and broke the "last command" assertion (the assertion is unchanged).
    base = {k: v for k, v in os.environ.items() if k not in ("MAKELEVEL", "MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES")}
    return subprocess.run(["make", "--no-print-directory", *args, f"HOME={tmp_path}"], cwd=REPO, capture_output=True, text=True, env={**base, **env})


def _layout(tmp_path):
    d = tmp_path / "datasets"
    for rel in ("universe-v3-20261001T000000Z", "universe-v3-20261109T000000Z", "md-20260901", "md-20261101", "full-history/data", "spot-snap/spot-instruments/snapshot-1",
                "spot-snap/spot-instruments/snapshot-2"):
        (d / rel).mkdir(parents=True)
    return d


@needs_make
def test_the_default_globs_find_the_documented_layout(tmp_path):
    d = _layout(tmp_path)
    for target in ("scope", "diagnose", "plan"):
        out = _make(["-n", target], tmp_path).stdout
        assert f"{d}/universe-v3-20261109T000000Z" in out, (target, out[:300])
    plan = _make(["-n", "plan"], tmp_path).stdout
    assert f"--history-root {d}/md-20261101 " in plan                      # `plan` subtracts what the newest md-2* history already holds
    for target in ("regime-eval", "pipeline"):
        out = _make(["-n", target, "START=1", "END=2"], tmp_path).stdout
        assert f"--history-root {d}/full-history/data " in out, (target, out[:400])   # the full backfill is the history to evaluate once it exists
        assert f"{d}/spot-snap/spot-instruments/snapshot-2" in out
    # an explicit HIST= always wins
    assert "--history-root /x/h " in _make(["-n", "regime-eval", "HIST=/x/h"], tmp_path).stdout


@needs_make
def test_regime_eval_without_a_spot_snapshot_stops_with_a_message_instead_of_passing_an_empty_spot_dir(tmp_path):
    d = tmp_path / "datasets"
    (d / "md-20261101").mkdir(parents=True)
    (d / "universe-v3-1").mkdir()
    r = _make(["regime-eval"], tmp_path)
    assert r.returncode == 2 and "SPOT" in (r.stdout + r.stderr) and "--spot-dir" not in r.stdout


@needs_make
def test_pipeline_links_latest_and_the_control_centre_reads_latest_and_the_session_state_dir(tmp_path):
    out = _make(["-n", "pipeline", "START=1", "END=7", "HIST=/x/h", "UNI=/x/u", "SPOT=/x/s"], tmp_path).stdout
    assert "ln -sfn run-7 " in out and out.rstrip().splitlines()[-1].startswith("ln -sfn run-7 ")        # only after the run succeeded
    cc = _make(["-n", "control-centre-start"], tmp_path).stdout
    assert f"--artifacts-dir {tmp_path}/datasets/pipeline-out/latest" in cc
    assert f"--testnet-dir {tmp_path}/.ai-agent-control/ms_testnet" in cc


@needs_make
def test_ops_resume_restarts_each_service_the_owner_enabled_independently(tmp_path):
    ops = tmp_path / ".ai-agent-control" / "ops"
    ops.mkdir(parents=True)
    (ops / "control-centre.enabled").write_text("")
    r = _make(["ops-resume", "PY=true"], tmp_path)                           # PY=true: the recipe runs, nothing real is started
    assert r.returncode == 0, r.stderr
    assert (ops / "cc.pid").exists() and not (ops / "supervisor.pid").exists(), "control centre enabled alone must resume alone"
    (ops / "cc.pid").unlink()
    (ops / "control-centre.enabled").unlink()
    (ops / "ENABLED").write_text("")
    assert _make(["ops-resume", "PY=true"], tmp_path).returncode == 0
    assert (ops / "supervisor.pid").exists() and not (ops / "cc.pid").exists()


@needs_make
def test_collect_status_prints_the_disk_figure(tmp_path):
    root = tmp_path / "datasets" / "md-collector"
    root.mkdir(parents=True)
    (root / "health.json").write_text(json.dumps({"status": "RUNNING", "disk": {"free_bytes": 123456789, "reserve_bytes": 1}}))
    r = _make(["collect-status", f"PY={sys.executable}"], tmp_path)
    assert "'disk'" in r.stdout and "123456789" in r.stdout, r.stdout


def test_the_manual_collector_and_the_supervisor_cannot_collide_on_seal_receipts_backup_or_stop_file():
    sup_defaults = {a.dest: a.default for a in S.parser()._actions if a.dest in ("seal_manifest", "receipts", "backup_to", "stop_file", "root")}
    names = {os.path.basename(v.rstrip("/")) for v in sup_defaults.values()}
    manual = [ln for ln in _text(RUNBOOKS).splitlines() if "market_collector.py" in ln and ("run-continuous" in ln or " seal " in ln or " backup " in ln or "restore-check" in ln)]
    assert manual, "the runbook's manual collector commands moved"
    for ln in manual:
        for n in names:
            assert n not in re.findall(r"[\w.-]+", ln), (n, ln[:200])
    mk = _text(MAKEFILE)
    assert "STOP-md-collector" in mk and sup_defaults["stop_file"].endswith("STOP-md-collector")
