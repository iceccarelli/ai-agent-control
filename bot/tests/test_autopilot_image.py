"""The autopilot image ships everything the autopilot can import, cannot be aimed at mainnet, carries no credential, and its Fly app is one machine on one volume."""
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
ENTRY = ["ms_testnet_autopilot"]


def _read(name):
    return open(os.path.join(BOT, name), encoding="utf-8").read()


def _copy_lines():
    text = re.sub(r"\\\n", " ", _read("Dockerfile.autopilot"))
    return [line.split()[1:-1] for line in text.splitlines() if line.startswith("COPY ") and "--from" not in line]


def _copied():
    return {s for parts in _copy_lines() for s in parts}


def _imports(path):
    out = set()
    for n in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
        if isinstance(n, ast.Import):
            out.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            out.add(n.module.split(".")[0])
    return out


def _closure():
    bot = {f[:-3] for f in os.listdir(BOT) if f.endswith(".py")}
    tools = {f[:-3] for f in os.listdir(os.path.join(BOT, "tools")) if f.endswith(".py")}
    need, todo = set(), [f"tools/{m}.py" for m in ENTRY]
    while todo:
        p = todo.pop()
        if p in need:
            continue
        need.add(p)
        for m in _imports(os.path.join(BOT, p)):
            if m in tools:
                todo.append(f"tools/{m}.py")
            elif m in bot:
                todo.append(f"{m}.py")
    return need


def test_every_imported_file_is_copied():
    copied = _copied()
    missing = sorted(p for p in _closure() if p not in copied)
    assert not missing, f"Dockerfile.autopilot does not COPY: {missing}"


def test_no_bare_directory_copy_but_signals():
    for src in _copied():
        assert src not in ("tools/", ".", "./", "tools"), src


def test_the_image_is_testnet_only_and_carries_no_credential_or_live_switch():
    d = _read("Dockerfile.autopilot")
    assert "USE_TESTNET=1" in d and "BYBIT_VENUE=testnet" in d and "HOME=/data" in d
    assert "PAPER_TRADING" not in "\n".join(ln for ln in d.splitlines() if not ln.lstrip().startswith("#"))          # CLAUDE.md: PAPER_TRADING=0 is never set
    for token in ("LIVE_TRADING_ACK", "USE_TESTNET=0", "BYBIT_API_KEY", "BYBIT_API_SECRET", "BYBIT_TESTNET_API_KEY=", "BYBIT_TESTNET_API_SECRET=", "api.bybit.com", ".env", "ms_testnet_capability.json",
                  "ms_autopilot_grant.json"):
        assert not [ln for ln in d.splitlines() if not ln.lstrip().startswith("#") and token in ln], token
    assert 'CMD ["python3", "tools/ms_testnet_autopilot.py", "loop"]' in d and "autopilot-entrypoint" in d


def test_entrypoint_drops_privileges_execs_and_fixes_the_owner_files_modes():
    sh = _read("deploy/autopilot-entrypoint.sh")
    assert "exec setpriv" in sh and "--reuid=bot" in sh and "chown -R bot:bot /data" in sh
    assert "chmod 600" in sh and "ms_testnet_capability.json" in sh and "ms_autopilot_grant.json" in sh
    assert os.access(os.path.join(BOT, "deploy", "autopilot-entrypoint.sh"), os.X_OK)


def test_the_fly_app_is_one_machine_one_volume_no_public_port_and_no_secret():
    t = _read("fly.autopilot.toml")
    body = "\n".join(ln for ln in t.splitlines() if not ln.lstrip().startswith("#"))
    assert 'app = "carry-autopilot"' in body and 'primary_region = "fra"' in body and 'kill_timeout = "300s"' in body and 'kill_signal = "SIGTERM"' in body
    assert re.search(r'\[\[restart\]\]\s*policy = "always"', body) and "retries" not in body       # always-on: the loop never exits on ALERT/PAUSED/STOP/DRAIN, so `always` cannot create a restart loop
    assert 'strategy = "immediate"' in body and "[http_service]" not in body and "[[services]]" not in body
    assert body.count("[[mounts]]") == 1 and 'destination = "/data"' in body and 'dockerfile = "Dockerfile.autopilot"' in body
    assert 'USE_TESTNET = "1"' in body and 'BYBIT_VENUE = "testnet"' in body and 'HOME = "/data"' in body
    for token in ("LIVE_TRADING_ACK", "PAPER_TRADING", "USE_TESTNET = \"0\"", "api.bybit.com", "BYBIT_API", "BYBIT_TESTNET_API", "SECRET", "KEY"):
        assert token not in body, token


def test_an_assembled_image_directory_imports_the_autopilot(tmp_path):
    """Build the directory the Dockerfile builds (only the COPYed files) and import the autopilot there: a module missing from the image would fail here, not on Fly."""
    app = tmp_path / "app"
    app.mkdir()
    for parts in _copy_lines():
        for src in parts:
            if src.endswith("/") or src.startswith("deploy/") or src in ("requirements.txt", "constraints.txt"):
                continue
            dst = app / src
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(os.path.join(BOT, src), dst)
    shutil.copytree(os.path.join(BOT, "signals"), app / "signals", ignore=shutil.ignore_patterns("__pycache__"))
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH",)}
    env.update(HOME=str(tmp_path / "home"), PYTHONPATH=os.pathsep.join(p for p in sys.path if p and "site-packages" in p or "dist-packages" in p))
    r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, 'tools'); import ms_testnet_autopilot as AP; print(AP.GRANT_SCHEMA)"], cwd=str(app), env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "ms_autopilot_grant/v1" in r.stdout, (r.stdout[-300:], r.stderr[-600:])


def test_the_owner_control_wrapper_runs_as_the_worker_user_and_loads_secrets_from_pid1():
    sh = _read("deploy/autopilot-ctl.sh")
    assert "exec setpriv --reuid=bot" in sh and "/proc/1/environ" in sh and "HOME=/data" in sh and "ms_testnet_autopilot.py" in sh
    assert "BYBIT_TESTNET_API_KEY" in sh and "LIVE_TRADING_ACK" not in sh and "BYBIT_API_KEY" not in sh
    assert "deploy/autopilot-ctl.sh" in _read("Dockerfile.autopilot") and os.access(os.path.join(BOT, "deploy", "autopilot-ctl.sh"), os.X_OK)


def test_the_smoke_script_covers_the_runtime_properties_the_review_demanded():
    sh = _read("deploy/autopilot-image-smoke.sh")
    for needle in ("setpriv", "docker stop", "SIGTERM", "stat -c", "bot 700", "bot 600", "docker history", "api-testnet.bybit.com", "LIVE_TRADING_ACK", "STOPPED", "ExitCode"):
        assert needle in sh, needle


def test_the_safe_deploy_script_drains_checks_and_only_then_deploys_and_refuses_otherwise():
    sh = _read("deploy/safe-deploy.sh")
    assert sh.index("drain --wait") < sh.index("deploy-check") < sh.index("fly deploy -c fly.autopilot.toml --ha=false")
    assert sh.count("exit 1") >= 2 and "set -eu" in sh and "fly launch" not in sh.replace("never fly launch", "")
    assert "resume --reason" in sh


def test_the_owner_control_wrapper_passes_the_cost_of_capital_variables_the_decision_reads():
    """REPRODUCED (Round 22 execution review): `autopilot-ctl evaluate` ran with a whitelisted copy of PID 1's environment that lacked the owner's STATED cash alternative, so the read-only economics
    the owner asks for reported an UNKNOWN cost of capital on a machine whose loop had it. Every name the economics reads must be on the whitelist."""
    import ms_edge_gate as EG
    sh = _read("deploy/autopilot-ctl.sh")
    line = next(ln for ln in sh.splitlines() if "BYBIT_TESTNET_API_KEY|" in ln)
    for name in (EG.ENV_CASH_APR, EG.ENV_CASH_SOURCE, EG.ENV_CASH_STATED_UTC, "EXECUTOR_MIN_FREE_BYTES"):
        assert name in line, name
    for forbidden in ("LIVE_", "BYBIT_API_KEY", "BYBIT_API_SECRET"):
        assert forbidden not in line, forbidden                    # the wrapper must never copy an arming switch or a generic credential
