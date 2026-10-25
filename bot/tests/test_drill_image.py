"""The drill runner image ships everything the unattended orchestrator can
import, cannot be aimed at mainnet, and does not carry credentials.

Static checks (no Docker daemon in CI), like test_dockerfile.py.
"""
from __future__ import annotations

import ast
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
ENTRY = ["runner_monitor", "run_outcomes", "carry_preflight", "unattended_drill",
         "next_settlement", "recon_summary", "drill", "close_testnet_short",
         "drill_final_state", "evidence_reconcile"]


def _read(name):
    return open(os.path.join(BOT, name), encoding="utf-8").read()


def _copied():
    text = re.sub(r"\\\n", " ", _read("Dockerfile.drill"))
    out = set()
    for line in text.splitlines():
        if line.startswith("COPY ") and "--from" not in line:
            parts = line.split()[1:-1]
            out.update(parts)
    return out


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
    tools = {f[:-3] for f in os.listdir(os.path.join(BOT, "tools"))
             if f.endswith(".py")}
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
    missing = sorted(p for p in _closure() if p not in copied
                     and os.path.basename(p) not in copied)
    assert not missing, f"Dockerfile.drill does not COPY: {missing}"


def test_no_bare_directory_copy_but_signals():
    for src in _copied():
        assert src not in ("tools/", ".", "./", "tools"), src


def test_entrypoint_drops_privileges_and_execs():
    sh = _read("deploy/drill-entrypoint.sh")
    assert "exec setpriv" in sh and "--reuid=bot" in sh


def test_image_is_testnet_and_credential_free():
    text = "\n".join(l for l in _read("Dockerfile.drill").splitlines()
                     if not l.lstrip().startswith("#"))
    assert "USE_TESTNET=1" in text and "BYBIT_VENUE=testnet" in text
    assert "LIVE_TRADING_ACK" not in text
    assert "BYBIT_API" not in text
    assert "--loop" in text


def _cmd(text):
    line = [l for l in text.splitlines() if l.startswith("CMD ")]
    assert len(line) == 1
    return json.loads(line[0][4:])


def test_the_default_command_is_the_read_only_monitor():
    text = _read("Dockerfile.drill")
    cmd = _cmd(text)
    assert cmd[:2] == ["python3", "tools/runner_monitor.py"]
    joined = " ".join(cmd)
    for forbidden in ("unattended_drill", "--conformance", "--arm", "drill.py",
                      "close_testnet_short"):
        assert forbidden not in joined, forbidden
    assert "--loop" in cmd


def test_the_conformance_runner_ships_but_is_never_the_entrypoint_or_default():
    text = _read("Dockerfile.drill")
    assert "tools/unattended_drill.py" in text        # available, explicitly
    entry = [l for l in text.splitlines() if l.startswith("ENTRYPOINT")]
    assert entry == ['ENTRYPOINT ["/usr/local/bin/drill-entrypoint"]']
    toml = _read("fly.drill.toml")
    assert "--conformance" not in toml and "unattended_drill" not in "\n".join(
        l for l in toml.splitlines() if not l.lstrip().startswith("#"))


def test_fly_config_properties():
    t = _read("fly.drill.toml")
    code = "\n".join(l for l in t.splitlines() if not l.lstrip().startswith("#"))
    assert 'app = "carry-drill"' in code
    assert code.count("[[mounts]]") == 1
    assert "[http_service]" not in code and "[services]" not in code
    assert 'USE_TESTNET = "1"' in code and 'BYBIT_VENUE = "testnet"' in code
    assert "LIVE_TRADING_ACK" not in code
    assert not re.search(r"(?i)(key|secret|token|password)\s*=", code)
    ms = int(re.search(r'kill_timeout = "(\d+)s"', code).group(1))
    assert ms >= 240
    assert 'dockerfile = "Dockerfile.drill"' in code
