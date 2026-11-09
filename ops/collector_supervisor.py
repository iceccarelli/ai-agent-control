#!/usr/bin/env python3
"""Keeps the PUBLIC-data market collector running across crashes and Codespace restarts. It has no order path and no credential.

It only runs ``tools/market_collector.py run-continuous`` in bounded segments (default 24 h) and restarts it after an unexpected exit with an exponential
back-off. It STOPS, writes ``ALERT`` and does not restart when:
  * the owner touched the stop file (a clean stop), or
  * the collector's own ``health.json`` says it REFUSED (low disk, cycle log needs repair, root not private ...): those need a human, not a retry loop, or
  * the same failure repeats ``--max-restarts`` times in a row.
Nothing here writes inside the git work tree: the data root, state dir, seals and backups must live outside it.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Callable, Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
COLLECTOR = os.path.join(REPO, "bot", "tools", "market_collector.py")
NEEDS_HUMAN = ("REFUSED",)            # health.json statuses that a restart can never fix


def inside_repo(path: str) -> bool:
    p = os.path.realpath(path)
    return p == REPO or p.startswith(REPO + os.sep)


def read_health(root: str) -> Dict[str, object]:
    try:
        with open(os.path.join(root, "health.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def build_command(a: argparse.Namespace, duration_s: int) -> List[str]:
    cmd = [sys.executable, COLLECTOR, "run-continuous", "--out-root", a.root, "--interval-ms", str(a.interval_ms), "--duration-s", str(duration_s),
           "--stop-file", a.stop_file, "--max-symbols", str(a.max_symbols), "--seal-manifest", a.seal_manifest, "--seal-every", str(a.seal_every),
           "--backup-to", a.backup_to, "--receipts", a.receipts, "--backup-every", str(a.backup_every), "--restore-check-every", str(a.restore_check_every)]
    return cmd


def decide(exit_code: int, health: Dict[str, object], consecutive_bad: int, max_restarts: int, stop_file_exists: bool) -> Dict[str, object]:
    """Pure policy: {'action': 'stop'|'next_segment'|'backoff', 'reason': str, 'delay_s': float}."""
    if stop_file_exists:
        return {"action": "stop", "reason": "STOP_FILE", "delay_s": 0}
    st = str(health.get("status", ""))
    if st in NEEDS_HUMAN:
        return {"action": "stop", "reason": f"COLLECTOR_{st}:{health.get('refusal') or health.get('stop_reason') or '?'}", "delay_s": 0}
    if exit_code == 0 and st != "CRASHED":
        return {"action": "next_segment", "reason": "SEGMENT_DONE", "delay_s": 5}
    if consecutive_bad + 1 >= max_restarts:
        return {"action": "stop", "reason": f"TOO_MANY_FAILURES:{consecutive_bad + 1}", "delay_s": 0}
    return {"action": "backoff", "reason": f"EXIT_{exit_code}:{st or 'NO_HEALTH'}", "delay_s": min(600.0, 15.0 * (2 ** consecutive_bad))}


def run(a: argparse.Namespace, runner: Callable[[Sequence[str]], int] = None, sleeper: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = None) -> int:
    log = log or (lambda m: print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " " + m, flush=True))
    runner = runner or (lambda cmd: subprocess.call(list(cmd)))
    for label, path in (("data root", a.root), ("state dir", a.state_dir), ("seal manifest", a.seal_manifest), ("receipts", a.receipts), ("backup dir", a.backup_to),
                        ("stop file", a.stop_file)):
        if inside_repo(path):
            log(f"REFUSED: {label} {path} is inside the git work tree")
            return 2
    os.makedirs(a.state_dir, mode=0o700, exist_ok=True)
    bad = 0
    segs = 0
    while True:
        if os.path.exists(a.stop_file):
            log("stop file present: not starting")
            return 0
        segs += 1
        log(f"segment {segs}: starting the PUBLIC collector (no order path, no credential)")
        code = runner(build_command(a, a.segment_s))
        health = read_health(a.root)
        d = decide(code, health, bad, a.max_restarts, os.path.exists(a.stop_file))
        log(f"segment {segs}: exit={code} health={health.get('status')} -> {d['action']} ({d['reason']})")
        bad = 0 if d["action"] == "next_segment" else bad + 1
        if d["action"] == "stop":
            if d["reason"] != "STOP_FILE":
                with open(os.path.join(a.state_dir, "ALERT"), "w", encoding="utf-8") as fh:
                    fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} collector supervisor stopped: {d['reason']}\n"
                             "A human must look at <root>/health.json and RUNBOOKS 'Codespaces operations' before restarting.\n")
                return 1
            return 0
        if a.max_segments and segs >= a.max_segments:
            return 0
        sleeper(float(d["delay_s"]))


def parser() -> argparse.ArgumentParser:
    h = os.path.expanduser("~")
    p = argparse.ArgumentParser(description="restart-on-failure supervisor for the PUBLIC market collector (no order path)")
    p.add_argument("--root", default=os.path.join(h, "datasets", "md-collector"))
    p.add_argument("--state-dir", default=os.path.join(h, ".ai-agent-control", "ops"))
    p.add_argument("--stop-file", default=os.path.join(h, "datasets", "STOP"))
    p.add_argument("--seal-manifest", default=os.path.join(h, "seals", "md-seal.jsonl"))
    p.add_argument("--receipts", default=os.path.join(h, "seals", "md-receipts.jsonl"))
    p.add_argument("--backup-to", default=os.path.join(h, "backups", "md"))
    p.add_argument("--interval-ms", type=int, default=60000)
    p.add_argument("--max-symbols", type=int, default=20)
    p.add_argument("--segment-s", type=int, default=86400)
    p.add_argument("--seal-every", type=int, default=60)
    p.add_argument("--backup-every", type=int, default=60)
    p.add_argument("--restore-check-every", type=int, default=360)
    p.add_argument("--max-restarts", type=int, default=6, help="consecutive failed segments before the supervisor stops and raises ALERT")
    p.add_argument("--max-segments", type=int, default=0, help="0 = run until stopped")
    return p


if __name__ == "__main__":
    sys.exit(run(parser().parse_args()))
