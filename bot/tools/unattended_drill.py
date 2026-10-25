#!/usr/bin/env python3
"""Run the TESTNET carry drill unattended, on any host that can reach Bybit.

ORDER-CAPABLE: invoke it only with --conformance. The unattended DEFAULT is the
read-only tools/runner_monitor.py. A run here ends in one of ENTRY_NOT_ELIGIBLE
(the entry gate refused before anything was armed or at the open; exit 11, no
order sent), CONFORMANCE_PASSED (an order was placed, exited, captured and
reconciled, venue flat; exit 0) or CONFORMANCE_FAILED (exit 1).

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

SIGNALS: SIGTERM / SIGHUP / SIGINT stop the drill child (its whole process
group gets SIGTERM, so the drill unwinds; SIGKILL after a grace period), THEN
run the safety-net close (further signals during it are ignored), then exit 143.
Give the host a stop timeout of at least ~4 minutes (Fly: kill_timeout).

EXIT CODES: 0 the run finished cleanly (drill exit 0 or 5, venue verified flat,
reconciliation produced a report); 1 it did not (see verdict.json); 3 refused
(missing credentials, another loop running, bad arguments); 4 the testnet is
not reachable from this host; 10 no settlement window now (nothing sent).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
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
import run_outcomes as RO  # noqa: E402

PROBE_URL = "https://api-testnet.bybit.com/v5/market/time"
TOOLS = os.path.join(BOT, "tools")           # where the child tools live
PROBE_TRIES, PROBE_PAUSE_S = 3, 20.0
KEEP_RUNS = 60
EXIT_SIGNALLED = 143
MIN_LEAD_MIN, MAX_LEAD_MIN = 4.0, 20.0
DRILL_TIMEOUT_S = 34 * 60
PREFLIGHT_TIMEOUT_S = 300
TERM_GRACE_S = 120
MAX_CLOSE_QTY = "0.0015"
EXIT_OK, EXIT_FAILED, EXIT_REFUSED, EXIT_UNREACHABLE, EXIT_OUTSIDE = 0, 1, 3, 4, 10
#: A correct strategy refusal: the run was sound, no order was sent, and it is
#: NOT a conformance pass. Non-zero on purpose, so no caller reads it as one.
EXIT_NOT_ELIGIBLE = 11

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


class Shutdown(BaseException):
    """The orchestrator was told to stop (SIGTERM / SIGHUP / SIGINT). A
    BaseException so that no `except Exception` swallows it."""


_SIGNAL = {"count": 0, "protect": False, "pending": 0}


def _handler(signum: int, _frame: Any) -> None:
    """First signal: raise Shutdown (the drill is stopped and the safety net
    runs). Every later signal is ignored, and while the safety net itself runs
    the first one is only RECORDED: interrupting the reduce-only close would
    leave the short open."""
    _SIGNAL["count"] += 1
    if _SIGNAL["count"] > 1:
        return
    if _SIGNAL["protect"]:
        _SIGNAL["pending"] = signum
        return
    raise Shutdown(f"signal {signum}")


def install_signal_handlers() -> None:
    for name in ("SIGTERM", "SIGHUP", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, _handler)


def _stop_group(proc: "subprocess.Popen[bytes]") -> None:
    """SIGTERM the child's whole process group (the drill unwinds on it), wait
    a grace period, then SIGKILL the group."""
    for sig, wait_s in ((signal.SIGTERM, TERM_GRACE_S), (signal.SIGKILL, 30)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=wait_s)
            return
        except subprocess.TimeoutExpired:
            continue


def run_child(argv: Sequence[str], env: Mapping[str, str], *, log_path: str,
              timeout_s: Optional[float] = None) -> int:
    """Run one tool in its OWN process group, appending its output to
    `log_path`. On a timeout, or when THIS process is told to stop, the child's
    group gets SIGTERM (the drill unwinds on it), a grace period, then SIGKILL,
    and only then does control return (or the interruption continue): the
    safety net must never run beside a live drill. Returns the exit code, or
    124 for a timeout."""
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(list(argv), env=dict(env), stdout=log,
                                stderr=subprocess.STDOUT, cwd=BOT,
                                start_new_session=True)
        try:
            return proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _stop_group(proc)
            return 124
        except BaseException:
            _stop_group(proc)
            raise


def verdict(*, preflight: Optional[int], drill: Optional[int],
            flat: Optional[int], recon: Optional[int],
            not_eligible: Optional[str] = None,
            assessment_failed: bool = False,
            reconciled: Optional[bool] = None) -> Dict[str, Any]:
    """The run's verdict from the exit codes (None = did not run).

    `outcome` says what the run LEARNED (tools/run_outcomes.py):
      ENTRY_NOT_ELIGIBLE  `not_eligible` is the reason code: the production
                          entry gate refused, before arming or from the armed
                          drill's own engine, and nothing was sent. A correct
                          strategy decision, so `ok` stays True - and it is not
                          a conformance pass;
      CONFORMANCE_PASSED  an order was placed, exited, captured and reconciled
                          (`reconciled`: the cycle is VENUE_RECONCILED) and the
                          venue ended flat - only this;
      CONFORMANCE_FAILED  anything else.
    """
    problems: List[str] = []
    if flat != 0:
        problems.append("the venue could not be verified flat: look at the "
                        "testnet account")
    if preflight != 0:
        problems.append("preflight failed (not flat, or the venue could not "
                        "be read): no drill was run")
    elif assessment_failed:
        problems.append("the entry economics could not be assessed (an input "
                        "was unreadable, stale or contradictory): no drill "
                        "was run")
    elif not_eligible:
        pass                      # a strategy refusal, not a problem
    elif drill not in (0, 5):
        problems.append(f"drill exit code {drill} is not a clean finish")
    elif recon not in (0, 2):
        problems.append(f"reconciliation did not produce a usable report "
                        f"(exit {recon}): the evidence path is NOT working")
    elif reconciled is not True:
        problems.append("the captured cycle is not VENUE_RECONCILED: the "
                        "order path is NOT proven")
    if problems:
        outcome = RO.CONFORMANCE_FAILED
    elif not_eligible:
        outcome = RO.ENTRY_NOT_ELIGIBLE
    else:
        outcome = RO.CONFORMANCE_PASSED
    return {"ok": not problems, "outcome": outcome,
            "conformance_passed": outcome == RO.CONFORMANCE_PASSED,
            "refusal_reason": not_eligible if outcome == RO.ENTRY_NOT_ELIGIBLE
            else None,
            "problems": problems, "preflight": preflight, "drill": drill,
            "ensure_flat": flat, "reconcile": recon}


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
             runner: Callable[..., int] = run_child,
             sleep: Callable[[float], None] = time.sleep) -> int:
    os.makedirs(run_dir, exist_ok=True)
    status = 0
    for attempt in range(PROBE_TRIES):
        status = prober()
        if status == 200:
            break
        if attempt + 1 < PROBE_TRIES:
            sleep(PROBE_PAUSE_S)
    if status != 200:
        _say(run_dir, f"testnet NOT reachable from this host (HTTP {status or 'no answer'}); "
                      "nothing sent")
        return EXIT_UNREACHABLE

    def clock_ms() -> int:
        return int(time.time() * 1000) if now_ms is None else now_ms

    def in_window() -> tuple:
        return NS.window_status(clock_ms(), min_lead_s=MIN_LEAD_MIN * 60,
                                max_lead_s=MAX_LEAD_MIN * 60)

    inside, lead, _settle = in_window()
    if not inside:
        _say(run_dir, f"no settlement {MIN_LEAD_MIN:.0f}-{MAX_LEAD_MIN:.0f} minutes "
                      f"away ({lead} s to the next); nothing sent")
        return EXIT_OUTSIDE
    cenv = child_env(env, run_dir)
    py = sys.executable
    # the evidence files live in their OWN folder: evidence_reconcile.py refuses
    # to write its report beside the evidence it read
    evdir = os.path.join(run_dir, "evidence")
    os.makedirs(evdir, exist_ok=True)
    log = os.path.join(run_dir, "run.log")
    ev = os.path.join(evdir, "drill_evidence.jsonl")
    out = os.path.join(run_dir, "drill_out.json")
    led = os.path.join(run_dir, "drill_ledger.json")
    rep = os.path.join(run_dir, "reconciliation.json")
    _say(run_dir, f"settlement in {lead} s: running")

    preflight: Optional[int] = None
    drill: Optional[int] = None
    flat: Optional[int] = None
    window_closed = False
    not_eligible: Optional[str] = None
    assessment_failed = False
    try:
        preflight = _guard(lambda: runner(
            [py, os.path.join(TOOLS, "drill.py")], cenv, log_path=log,
            timeout_s=PREFLIGHT_TIMEOUT_S))
        if preflight == 0:
            # Is the entry ELIGIBLE? Asked of the existing read-only preflight
            # (CarryEngine.entry_economics = carry_costs.entry_gate), never
            # re-derived here, and BEFORE anything is armed: a refusal is a
            # strategy decision, not a failed drill.
            elig_log = os.path.join(run_dir, "eligibility.log")
            elig_rc = _guard(lambda: runner(
                [py, os.path.join(TOOLS, "carry_preflight.py")], cenv,
                log_path=elig_log, timeout_s=PREFLIGHT_TIMEOUT_S))
            try:
                with open(elig_log, encoding="utf-8", errors="replace") as fh:
                    elig = RO.classify_preflight(
                        RO.parse_report(fh.read()), elig_rc,
                        expect_order_gate_closed=False)
            except OSError:
                elig = RO.classify_preflight(None, elig_rc)
            if elig["outcome"] == RO.ENTRY_NOT_ELIGIBLE:
                not_eligible = (elig["reasons"] or ["ENTRY_NOT_ELIGIBLE"])[0]
                _say(run_dir, f"ENTRY_NOT_ELIGIBLE ({not_eligible}): the "
                              "production entry gate refuses; nothing armed, "
                              "no order sent")
            elif elig["outcome"] != RO.MONITOR_READY:
                assessment_failed = True
                _say(run_dir, "the entry economics could not be assessed: "
                              + "; ".join(elig["reasons"]) + " (nothing armed)")
        if preflight == 0 and not not_eligible and not assessment_failed:
            inside, lead, _settle = in_window()
            if not inside:
                # a slow preflight must not push the order past the window
                window_closed = True
                _say(run_dir, f"the window closed during the preflight ({lead} s "
                              "to the next settlement): no drill")
            else:
                drill = _guard(lambda: runner(
                    [py, os.path.join(TOOLS, "drill.py"), "--arm",
                     "--hold-for-settlement", "--max-hold-hours", "0.5",
                     "--poll-seconds", "30", "--settle-wait-hours", "0",
                     "--min-settlements", "1", "--evidence-path", ev,
                     "--out", out, "--cash-ledger", led],
                    cenv, log_path=log, timeout_s=DRILL_TIMEOUT_S))
    finally:
        # ALWAYS, even if this process was told to stop (the child has already
        # been stopped by run_child) or a child could not be started: leave the
        # venue flat. A signal arriving NOW is recorded and acted on afterwards.
        _SIGNAL["protect"] = True
        try:
            flat = _guard(lambda: runner(
                [py, os.path.join(TOOLS, "close_testnet_short.py"), "--arm",
                 "--no-prompt", "--max-qty", MAX_CLOSE_QTY, "--evidence-path",
                 os.path.join(evdir, "ensure_flat_evidence.jsonl")],
                cenv, log_path=log, timeout_s=300))
        finally:
            _SIGNAL["protect"] = False
    if _SIGNAL["pending"]:
        pending, _SIGNAL["pending"] = _SIGNAL["pending"], 0
        raise Shutdown(f"signal {pending}")
    recon = None
    reconciled: Optional[bool] = None
    if drill is not None:
        # The armed drill's own engine may stand aside (funding moved between
        # the assessment and the open): nothing was sent, which is the gate
        # working, not a drill that failed.
        try:
            with open(out, encoding="utf-8") as fh:
                aside = RO.stood_aside_without_orders(json.load(fh))
        except (OSError, ValueError):
            aside = None
        if aside:
            not_eligible = aside
            _say(run_dir, f"ENTRY_NOT_ELIGIBLE ({aside}): the engine stood "
                          "aside at the open; no order was sent")
    if drill is not None and not not_eligible:
        args = [py, os.path.join(TOOLS, "evidence_reconcile.py"), "--evidence", ev,
                "--drill-report", out, "--now-ms", str(int(time.time() * 1000)),
                "--json", rep]
        if os.path.isfile(led):
            args += ["--ledger", led]
        recon = _guard(lambda: runner(args, cenv, log_path=log, timeout_s=300))
        if not (os.path.isfile(rep) and os.path.getsize(rep) > 0):
            recon = 99
        _guard(lambda: runner([py, os.path.join(TOOLS, "recon_summary.py"), rep],
                              cenv, log_path=os.path.join(run_dir, "summary.txt"),
                              timeout_s=60))
        try:
            with open(rep, encoding="utf-8") as fh:
                reconciled = RO.cycle_reconciled(json.load(fh))
        except (OSError, ValueError):
            reconciled = False
    result = verdict(preflight=preflight, drill=drill, flat=flat, recon=recon,
                     not_eligible=not_eligible,
                     assessment_failed=assessment_failed, reconciled=reconciled)
    if window_closed:
        result["problems"].append("the window closed during the preflight: no drill")
        result.update(ok=False, outcome=RO.CONFORMANCE_FAILED,
                      conformance_passed=False, refusal_reason=None)
    with open(os.path.join(run_dir, "verdict.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, sort_keys=True)
    _say(run_dir, "VERDICT " + json.dumps(result, sort_keys=True))
    if result["outcome"] == RO.ENTRY_NOT_ELIGIBLE:
        return EXIT_NOT_ELIGIBLE
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


def prune_runs(root: str, keep: int = KEEP_RUNS) -> List[str]:
    """Delete the oldest `run-<time>` folders beyond `keep`. Only folders whose
    name is exactly that shape, directly under the root, are ever touched."""
    pat = re.compile(r"^run-\d{8}T\d{6}Z$")
    try:
        names = sorted(n for n in os.listdir(root)
                       if pat.match(n) and os.path.isdir(os.path.join(root, n))
                       and not os.path.islink(os.path.join(root, n)))
    except OSError:
        return []
    doomed = names[:max(0, len(names) - keep)]
    for name in doomed:
        shutil.rmtree(os.path.join(root, name), ignore_errors=True)
    return doomed


def acquire_lock(root: str):
    """An exclusive lock on <root>/loop.lock, or None if another loop holds it."""
    import fcntl
    os.makedirs(root, exist_ok=True)
    # "a" does not truncate: a refused second loop must not wipe the holder's PID
    handle = open(os.path.join(root, "loop.lock"), "a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
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
        try:
            code = once(os.path.join(root, f"run-{stamp}"), env=env)
        except Shutdown as exc:
            _say(root, f"told to stop ({exc}); the safety net has run: loop ends")
            return EXIT_SIGNALLED
        runs += 1
        _say(root, f"run {stamp} ended with exit {code}")
        prune_runs(root)
        if max_runs and runs >= max_runs:
            return code
        # step past this window so one window is never run twice
        while NS.window_status(int(clock() * 1000), min_lead_s=MIN_LEAD_MIN * 60,
                               max_lead_s=MAX_LEAD_MIN * 60)[0]:
            if os.path.exists(os.path.join(root, "STOP")):
                break
            sleep(30.0)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--conformance", action="store_true",
                    help="REQUIRED. This runner places TESTNET orders (capped, "
                         "reduce-only safety net). The deployed default is the "
                         "read-only tools/runner_monitor.py; this is the "
                         "explicitly invoked order-path conformance run")
    ap.add_argument("--run-dir", required=True,
                    help="one run: files go here. With --loop: the run ROOT "
                         "(a run-<time> folder is made per run)")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--max-runs", type=int, default=0)
    args = ap.parse_args(argv)
    if not args.conformance:
        print("refusing: this runner can place testnet orders and must be "
              "invoked explicitly with --conformance. For unattended "
              "monitoring use tools/runner_monitor.py, which cannot trade.",
              file=sys.stderr)
        return EXIT_REFUSED
    missing = [k for k in CREDENTIALS if not os.environ.get(k)]
    if missing:
        print(f"refusing: {', '.join(missing)} not set in the environment "
              "(testnet credentials; never printed)", file=sys.stderr)
        return EXIT_REFUSED
    if args.max_runs < 0:
        print("refusing: --max-runs must be >= 0", file=sys.stderr)
        return EXIT_REFUSED
    install_signal_handlers()
    if not args.loop:
        try:
            return run_once(args.run_dir, env=os.environ)
        except Shutdown as exc:
            print(f"told to stop ({exc}); the safety net has run", flush=True)
            return EXIT_SIGNALLED
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
