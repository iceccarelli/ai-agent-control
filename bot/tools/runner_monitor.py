#!/usr/bin/env python3
"""READ-ONLY unattended monitor for the testnet carry runner. It cannot trade.

This is what the deployed runner runs by default. Each cycle it
  1. checks the public testnet is reachable from this host;
  2. runs `carry_preflight.py` (the existing read-only evaluation: venue reads,
     safety preconditions, and `CarryEngine.entry_economics`, i.e.
     `carry_costs.entry_gate`, the very gate `on_candle` asks - nothing is
     re-implemented here);
  3. classifies the result (tools/run_outcomes.py) and appends one line to
     `monitor.jsonl`.

It never starts `drill.py` or `close_testnet_short.py`, and nothing it runs can
place or cancel an order: the child's environment sets PAPER_TRADING=1 (the
carry order gate refuses every order), the preflight's broker has its own gate
that refuses every order, and a report that says the gate would allow orders is
a failure. `tests/test_runner_monitor.py` pins all three.

With `--shadow-db PATH` each cycle also records one public observation in the
post-gate forward shadow book (carry_shadow.py forward-step): data collection,
reported beside the outcome and never part of it.

OUTCOMES (stdout and `monitor.json`)
  ENTRY_NOT_ELIGIBLE  readiness good, the production entry gate refuses
  MONITOR_READY       readiness good, the production entry gate passes
  MONITOR_FAILED      something required was unreadable, contradictory, failed
Exit codes: 0 READY or NOT_ELIGIBLE (both are correct readings), 1 FAILED,
3 refused (credentials missing / another loop on the run root / bad arguments),
4 the testnet is not reachable, 143 told to stop.

    python3 tools/runner_monitor.py --run-dir /data/monitor --loop
    python3 tools/runner_monitor.py --run-dir /tmp/m --once
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import run_outcomes as RO  # noqa: E402
import unattended_drill as UD  # noqa: E402  (probe, child runner, lock, prune)

PREFLIGHT_TIMEOUT_S = 300
DEFAULT_INTERVAL_S = 3600.0
EXIT_OK, EXIT_FAILED, EXIT_REFUSED, EXIT_UNREACHABLE = 0, 1, 3, 4
EXIT_SIGNALLED = UD.EXIT_SIGNALLED

#: What the preflight child sees, whatever the parent's environment says.
#: PAPER_TRADING=1 closes the carry order gate; the preflight closes its own.
MONITOR_ENV = {
    "BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
    "CARRY_EXECUTION_MODE": "overlay", "CARRY_EXECUTION_STYLE": "taker",
    "BYBIT_VENUE": "testnet", "USE_TESTNET": "1", "PAPER_TRADING": "1",
}


def monitor_env(base: Mapping[str, str], run_dir: str) -> Dict[str, str]:
    env = dict(base)
    env.pop("LIVE_TRADING_ACK", None)
    env.update(MONITOR_ENV)
    env["STATE_DB_PATH"] = os.path.join(run_dir, "no_running_book.db")
    return env


def shadow_observation(db: Optional[str],
                       stepper: Optional[Callable[..., Dict[str, Any]]] = None
                       ) -> Optional[Dict[str, Any]]:
    """One forward-shadow observation, or None when none is configured.

    The post-gate forward book (`carry_shadow.py forward-init`) is fed public
    testnet market data only: an unauthenticated GET through the shadow module's
    own reader, a simulated venue, no exchange client. It is OBSERVATION
    COLLECTION and is reported next to the monitor outcome, never folded into it:
    a refused, stale or halted shadow step cannot turn a good monitor reading
    into a failure, and a good one proves nothing about the monitor.
    The book is created deliberately, not here: a missing book is SKIPPED.
    """
    if not db:
        return None
    if not os.path.exists(db):
        return {"state": "SKIPPED_NOT_INITIALISED", "db": db,
                "detail": "run carry_shadow.py forward-init first; the monitor "
                          "never creates a shadow book"}
    try:
        if stepper is None:
            import carry_shadow
            stepper = carry_shadow.forward_step
        result = stepper(db)
        record = result.get("record") or {}
        out = {"state": result.get("outcome", "UNKNOWN"), "db": db,
               "seq": result.get("seq"),
               "decision_reason": (record.get("decision") or {}).get("reason"),
               "fills_simulated": len(record.get("intents_and_fills") or [])}
        if result.get("reason"):
            out["detail"] = str(result["reason"])
        return out
    except Exception as exc:                                   # noqa: BLE001
        code = getattr(exc, "code", None)
        return {"state": "REFUSED" if code is not None else "FAULT", "db": db,
                "code": code, "detail": f"{type(exc).__name__}: {exc}"}


def _write_json(path: str, obj: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True, default=str)
    os.replace(tmp, path)


def _history_line(root: str, record: Dict[str, Any]) -> None:
    def mapping(x: Any) -> Dict[str, Any]:
        return x if isinstance(x, dict) else {}
    decision = mapping(mapping(record.get("assessment")).get("entry_decision"))
    funding = mapping(decision.get("funding"))
    hold = mapping(decision.get("hold_gate"))
    line = {
        "at": record["at"], "outcome": record["outcome"],
        "reasons": record["reasons"], "order_sent": False,
        "gate_reason": mapping(decision.get("gate")).get("reason"),
        "smoothed_funding_bps": funding.get("smoothed_bps_per_8h_print"),
        "round_trip_bps": decision.get("round_trip_bps_estimate"),
        "settlements_needed": hold.get("settlements_needed"),
        "settlements_budget": hold.get("settlements_budget"),
        "newest_print_age_hours": funding.get("newest_print_age_hours"),
    }
    shadow = record.get("shadow")
    if isinstance(shadow, dict):
        line["shadow_state"] = shadow.get("state")
        line["shadow_decision_reason"] = shadow.get("decision_reason")
    try:
        with open(os.path.join(root, "monitor.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, sort_keys=True, default=str) + "\n")
    except OSError:
        pass


def monitor_once(run_dir: str, *, env: Mapping[str, str],
                 prober: Callable[[], int] = UD.probe,
                 runner: Callable[..., int] = UD.run_child,
                 sleep: Callable[[float], None] = time.sleep,
                 history_root: Optional[str] = None,
                 shadow_db: Optional[str] = None,
                 shadow_stepper: Optional[Callable[..., Dict[str, Any]]] = None
                 ) -> int:
    """One read-only cycle. Returns the exit code; writes `monitor.json`.

    With `shadow_db`, the cycle also feeds one public observation to the
    post-gate forward shadow book (see `shadow_observation`)."""
    os.makedirs(run_dir, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def finish(result: Dict[str, Any], code: int) -> int:
        record = {"at": stamp, "schema": "monitor/1", **result}
        _write_json(os.path.join(run_dir, "monitor.json"), record)
        _history_line(history_root or run_dir, record)
        UD._say(run_dir, f"{record['outcome']}: " + "; ".join(record["reasons"]))
        return code

    status = 0
    for attempt in range(UD.PROBE_TRIES):
        status = prober()
        if status == 200:
            break
        if attempt + 1 < UD.PROBE_TRIES:
            sleep(UD.PROBE_PAUSE_S)
    if status != 200:
        return finish({
            "outcome": RO.MONITOR_FAILED, "order_sent": False,
            "reasons": [f"testnet not reachable from this host (HTTP "
                        f"{status or 'no answer'})"],
            "assessment": RO._assessment(None)}, EXIT_UNREACHABLE)

    log = os.path.join(run_dir, "preflight.log")
    cenv = monitor_env(env, run_dir)
    try:
        rc: Optional[int] = int(runner(
            [sys.executable, os.path.join(UD.TOOLS, "carry_preflight.py")],
            cenv, log_path=log, timeout_s=PREFLIGHT_TIMEOUT_S))
    except Exception:                                          # noqa: BLE001
        rc = None
    try:
        with open(log, encoding="utf-8", errors="replace") as fh:
            report = RO.parse_report(fh.read())
    except Exception:                                          # noqa: BLE001
        report = None                  # unreadable output claims nothing
    result = RO.classify_preflight(report, rc, expect_order_gate_closed=True)
    shadow = shadow_observation(shadow_db, shadow_stepper)
    if shadow is not None:
        result = {**result, "shadow": shadow}
    return finish(result, EXIT_FAILED if result["outcome"] == RO.MONITOR_FAILED
                  else EXIT_OK)


def loop(root: str, *, env: Mapping[str, str], interval_s: float,
         max_runs: int = 0, clock: Callable[[], float] = time.time,
         sleep: Callable[[float], None] = time.sleep,
         once: Callable[..., int] = monitor_once,
         shadow_db: Optional[str] = None) -> int:
    """A stop signal ends the loop with 143 wherever it lands, including the
    sleep between cycles where it spends nearly all its time: under the Fly
    on-failure restart policy a deploy or stop must not look like a failure."""
    try:
        return _loop(root, env=env, interval_s=interval_s, max_runs=max_runs,
                     clock=clock, sleep=sleep, once=once, shadow_db=shadow_db)
    except UD.Shutdown as exc:
        UD._say(root, f"told to stop ({exc}): monitor loop ends")
        return EXIT_SIGNALLED


def _loop(root: str, *, env: Mapping[str, str], interval_s: float,
          max_runs: int, clock: Callable[[], float],
          sleep: Callable[[float], None], once: Callable[..., int],
          shadow_db: Optional[str] = None) -> int:
    runs = 0
    while True:
        if os.path.exists(os.path.join(root, "STOP")):
            UD._say(root, "STOP file present: monitor loop ends")
            return EXIT_OK
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(clock()))
        try:
            extra = {"shadow_db": shadow_db} if shadow_db else {}
            code = once(os.path.join(root, f"run-{stamp}"), env=env,
                        history_root=root, **extra)
        except UD.Shutdown as exc:
            UD._say(root, f"told to stop ({exc}): monitor loop ends")
            return EXIT_SIGNALLED
        runs += 1
        UD.prune_runs(root)
        if max_runs and runs >= max_runs:
            return code
        waited = 0.0
        while waited < interval_s:                    # short slices: STOP, signals
            if os.path.exists(os.path.join(root, "STOP")):
                break
            step = min(30.0, interval_s - waited)
            sleep(step)
            waited += step


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run-dir", required=True,
                    help="with --loop: the run ROOT (a run-<time> folder per cycle)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    ap.add_argument("--interval-minutes", type=float,
                    default=DEFAULT_INTERVAL_S / 60.0)
    ap.add_argument("--max-runs", type=int, default=0)
    ap.add_argument("--shadow-db", default=None,
                    help="also feed one PUBLIC observation per cycle to this "
                         "post-gate forward shadow book (created beforehand with "
                         "carry_shadow.py forward-init; never created here)")
    args = ap.parse_args(argv)
    missing = [k for k in UD.CREDENTIALS if not os.environ.get(k)]
    if missing:
        print(f"refusing: {', '.join(missing)} not set in the environment "
              "(testnet credentials; never printed)", file=sys.stderr)
        return EXIT_REFUSED
    if args.max_runs < 0 or args.interval_minutes < 1.0:
        print("refusing: --max-runs must be >= 0 and --interval-minutes >= 1",
              file=sys.stderr)
        return EXIT_REFUSED
    UD.install_signal_handlers()
    if args.once:
        try:
            return monitor_once(args.run_dir, env=os.environ,
                                shadow_db=args.shadow_db)
        except UD.Shutdown as exc:
            print(f"told to stop ({exc})", flush=True)
            return EXIT_SIGNALLED
    lock = UD.acquire_lock(args.run_dir)
    if lock is None:
        print("refusing: another loop is running on this run root",
              file=sys.stderr)
        return EXIT_REFUSED
    try:
        return loop(args.run_dir, env=os.environ,
                    interval_s=args.interval_minutes * 60.0,
                    max_runs=args.max_runs, shadow_db=args.shadow_db)
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
