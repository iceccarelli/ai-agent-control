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
    assert "USE_TESTNET=1" in d and "PAPER_TRADING=0" in d and "BYBIT_VENUE=testnet" in d and "HOME=/data" in d
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
    for token in ("LIVE_TRADING_ACK", "USE_TESTNET = \"0\"", "api.bybit.com", "BYBIT_API", "BYBIT_TESTNET_API", "SECRET", "KEY"):
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
