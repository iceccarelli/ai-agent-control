#!/usr/bin/env python3
"""Run the TESTNET carry drill unattended, on any host that can reach Bybit.

WHY: a drill run from a laptop-bound Codespace dies when the Codespace stops,
and GitHub-hosted runners are blocked by Bybit (HTTP 403 from CloudFront). This
is the same procedure as `.github/workflows/carry-drill-testnet.yml`, as one
tested program, to run where the venue is reachable (a Fly.io machine in
Frankfurt, a small server, or - while it stays awake - the Codespace).

ONE RUN, in order (`--once`):
  1. is the testnet reachable from here? (public endpoint, no credentials;
     anything but HTTP 200 stops with exit 4 and NOTHING is sent);
  2. is a settlement 4-20 minutes away? (else exit 10, nothing sent);
  3. unarmed preflight (`tools/drill.py`; the venue must be flat);
  4. the armed drill across the settlement (capped; a timeout asks it to stop
     with SIGTERM, which makes the drill unwind, and only then kills it);
  5. ALWAYS: close any short left at the venue (`close_testnet_short.py
     --no-prompt --max-qty 0.0015`, reduce-only, testnet only);
  6. reconcile the capture against the drill's report; write verdict.json.

`--loop` repeats forever: sleep until the next window opens, run once, repeat.
A second loop on the same run root is refused (lock file); the loop stops when
the STOP file exists (`touch <run-root>/STOP`) and between runs only.

SAFETY (the children inherit a FIXED environment): BYBIT_VENUE=testnet,
USE_TESTNET=1, PAPER_TRADING=0, BOOK_MODE=carry, overlay, no borrow, a state
database path that does not exist, and LIVE_TRADING_ACK removed. The credentials
must already be in the environment (BYBIT_API_KEY / BYBIT_API_SECRET) and are
never printed. The drill itself refuses any non-testnet configuration or host.

EXIT CODES: 0 the run finished cleanly (drill exit 0 or 5, venue verified flat,
reconciliation produced a report); 1 it did not (see verdict.json); 3 refused
(missing credentials, another loop running, bad arguments); 4 the testnet is
not reachable from this host; 10 no settlement window now (nothing sent).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import next_settlement as NS  # noqa: E402

PROBE_URL = "https://api-testnet.bybit.com/v5/market/time"
MIN_LEAD_MIN, MAX_LEAD_MIN = 4.0, 20.0
DRILL_TIMEOUT_S = 34 * 60
TERM_GRACE_S = 120
MAX_CLOSE_QTY = "0.0015"
EXIT_OK, EXIT_FAILED, EXIT_REFUSED, EXIT_UNREACHABLE, EXIT_OUTSIDE = 0, 1, 3, 4, 10

#: What every child sees, whatever the parent's environment says.
FIXED_ENV = {
    "BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
    "CARRY_EXECUTION_MODE": "overlay", "BYBIT_VENUE": "testnet",
    "USE_TESTNET": "1", "PAPER_TRADING": "0",
}
CREDENTIALS = ("BYBIT_API_KEY", "BYBIT_API_SECRET")


def child_env(base: Mapping[str, str], run_dir: str) -> Dict[str, str]:
    env = dict(base)
    env.pop("LIVE_TRADING_ACK", None)
    env.update(FIXED_ENV)
    env["STATE_DB_PATH"] = os.path.join(run_dir, "no_running_book.db")
    return env


def probe(opener: Callable[..., Any] = urllib.request.urlopen) -> int:
    """HTTP status of the public testnet time endpoint, or 0 when unreachable."""
    try:
        with opener(PROBE_URL, timeout=15) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:                                          # noqa: BLE001
        return 0


def run_child(argv: Sequence[str], env: Mapping[str, str], *, log_path: str,
              timeout_s: Optional[float] = None) -> int:
    """Run one tool, appending its output to `log_path`. On timeout: SIGTERM
    (the drill unwinds on it), a grace period, then SIGKILL. Returns the exit
    code, or 124 for a timeout."""
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(list(argv), env=dict(env), stdout=log,
                                stderr=subprocess.STDOUT, cwd=BOT)
        try:
            return proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=TERM_GRACE_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            return 124


def verdict(*, preflight: Optional[int], drill: Optional[int],
            flat: Optional[int], recon: Optional[int]) -> Dict[str, Any]:
    """The run's verdict from the four exit codes (None = did not run)."""
    problems: List[str] = []
    if flat != 0:
        problems.append("the venue could not be verified flat: look at the "
                        "testnet account")
    if preflight != 0:
        problems.append("preflight failed (not flat, or the venue could not "
                        "be read): no drill was run")
    elif drill not in (0, 5):
        problems.append(f"drill exit code {drill} is not a clean finish")
    elif recon not in (0, 2):
        problems.append(f"reconciliation did not produce a usable report "
                        f"(exit {recon}): the evidence path is NOT working")
    return {"ok": not problems, "problems": problems, "preflight": preflight,
            "drill": drill, "ensure_flat": flat, "reconcile": recon}


def _guard(fn: Callable[[], int]) -> int:
    """A child that could not even be started is a failed child (125), never an
    exception that skips the steps after it."""
    try:
        return int(fn())
    except Exception:                                          # noqa: BLE001
        return 125


def run_once(run_dir: str, *, env: Mapping[str, str],
             now_ms: Optional[int] = None,
             prober: Callable[[], int] = probe,
             runner: Callable[..., int] = run_child) -> int:
    os.makedirs(run_dir, exist_ok=True)
    status = prober()
    if status != 200:
        _say(run_dir, f"testnet NOT reachable from this host (HTTP {status or 'no answer'}); "
                      "nothing sent")
        return EXIT_UNREACHABLE
    now = int(time.time() * 1000) if now_ms is None else now_ms
    inside, lead, _settle = NS.window_status(
        now, min_lead_s=MIN_LEAD_MIN * 60, max_lead_s=MAX_LEAD_MIN * 60)
    if not inside:
        _say(run_dir, f"no settlement {MIN_LEAD_MIN:.0f}-{MAX_LEAD_MIN:.0f} minutes "
                      f"away ({lead} s to the next); nothing sent")
        return EXIT_OUTSIDE
    cenv = child_env(env, run_dir)
    py = sys.executable
    tools = os.path.join(BOT, "tools")
    log = os.path.join(run_dir, "run.log")
    ev = os.path.join(run_dir, "drill_evidence.jsonl")
    out = os.path.join(run_dir, "drill_out.json")
    led = os.path.join(run_dir, "drill_ledger.json")
    rep = os.path.join(run_dir, "reconciliation.json")
    _say(run_dir, f"settlement in {lead} s: running")

    preflight: Optional[int] = None
    drill: Optional[int] = None
    flat: Optional[int] = None
    try:
        preflight = _guard(lambda: runner(
            [py, os.path.join(tools, "drill.py")], cenv, log_path=log))
        if preflight == 0:
            drill = _guard(lambda: runner(
                [py, os.path.join(tools, "drill.py"), "--arm",
                 "--hold-for-settlement", "--max-hold-hours", "0.5",
                 "--poll-seconds", "30", "--settle-wait-hours", "0",
                 "--min-settlements", "1", "--evidence-path", ev, "--out", out,
                 "--cash-ledger", led],
                cenv, log_path=log, timeout_s=DRILL_TIMEOUT_S))
    finally:
        # ALWAYS, even if this process is itself interrupted or a child could not
        # be started: leave the venue flat
        flat = _guard(lambda: runner(
            [py, os.path.join(tools, "close_testnet_short.py"), "--arm",
             "--no-prompt", "--max-qty", MAX_CLOSE_QTY, "--evidence-path",
             os.path.join(run_dir, "ensure_flat_evidence.jsonl")],
            cenv, log_path=log, timeout_s=300))
    recon = None
    if drill is not None:
        args = [py, os.path.join(tools, "evidence_reconcile.py"), "--evidence", ev,
                "--drill-report", out, "--now-ms", str(int(time.time() * 1000)),
                "--json", rep]
        if os.path.isfile(led):
            args += ["--ledger", led]
        recon = runner(args, cenv, log_path=log, timeout_s=300)
        if not (os.path.isfile(rep) and os.path.getsize(rep) > 0):
            recon = 99
        runner([py, os.path.join(tools, "recon_summary.py"), rep], cenv,
               log_path=os.path.join(run_dir, "summary.txt"), timeout_s=60)
    result = verdict(preflight=preflight, drill=drill, flat=flat, recon=recon)
    with open(os.path.join(run_dir, "verdict.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, sort_keys=True)
    _say(run_dir, "VERDICT " + json.dumps(result, sort_keys=True))
    return EXIT_OK if result["ok"] else EXIT_FAILED


def _say(run_dir: str, text: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {text}"
    print(line, flush=True)
    try:
        with open(os.path.join(run_dir, "orchestrator.log"), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def next_start_ms(now_ms: int) -> int:
    """When the next window opens: the next settlement minus MAX_LEAD_MIN, or
    now if that moment has passed but the window is still open."""
    settle = NS.next_settlement_ms(now_ms)
    start = settle - int(MAX_LEAD_MIN * 60_000)
    if start > now_ms:
        return start
    inside, _lead, _ = NS.window_status(
        now_ms, min_lead_s=MIN_LEAD_MIN * 60, max_lead_s=MAX_LEAD_MIN * 60)
    return now_ms if inside else NS.next_settlement_ms(settle) - int(MAX_LEAD_MIN * 60_000)


def acquire_lock(root: str):
    """An exclusive lock on <root>/loop.lock, or None if another loop holds it."""
    import fcntl
    os.makedirs(root, exist_ok=True)
    handle = open(os.path.join(root, "loop.lock"), "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def loop(root: str, *, env: Mapping[str, str], max_runs: int = 0,
         clock: Callable[[], float] = time.time,
         sleep: Callable[[float], None] = time.sleep,
         once: Callable[..., int] = run_once) -> int:
    runs = 0
    while True:
        if os.path.exists(os.path.join(root, "STOP")):
            _say(root, "STOP file present: loop ends")
            return EXIT_OK
        now_ms = int(clock() * 1000)
        wake = next_start_ms(now_ms)
        if wake > now_ms:
            _say(root, f"sleeping {(wake - now_ms) / 1000:.0f} s until the next window")
            # sleep in short slices so STOP and a changed clock are noticed
            while int(clock() * 1000) < wake:
                if os.path.exists(os.path.join(root, "STOP")):
                    break
                sleep(min(30.0, max(0.0, wake / 1000 - clock())))
            continue
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(clock()))
        code = once(os.path.join(root, f"run-{stamp}"), env=env)
        runs += 1
        _say(root, f"run {stamp} ended with exit {code}")
        if max_runs and runs >= max_runs:
            return code
        # step past this window so one window is never run twice
        while NS.window_status(int(clock() * 1000), min_lead_s=MIN_LEAD_MIN * 60,
                               max_lead_s=MAX_LEAD_MIN * 60)[0]:
            sleep(30.0)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run-dir", required=True,
                    help="one run: files go here. With --loop: the run ROOT "
                         "(a run-<time> folder is made per run)")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--max-runs", type=int, default=0)
    args = ap.parse_args(argv)
    missing = [k for k in CREDENTIALS if not os.environ.get(k)]
    if missing:
        print(f"refusing: {', '.join(missing)} not set in the environment "
              "(testnet credentials; never printed)", file=sys.stderr)
        return EXIT_REFUSED
    if args.max_runs < 0:
        print("refusing: --max-runs must be >= 0", file=sys.stderr)
        return EXIT_REFUSED
    if not args.loop:
        return run_once(args.run_dir, env=os.environ)
    lock = acquire_lock(args.run_dir)
    if lock is None:
        print("refusing: another loop is running on this run root",
              file=sys.stderr)
        return EXIT_REFUSED
    try:
        return loop(args.run_dir, env=os.environ, max_runs=args.max_runs)
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
