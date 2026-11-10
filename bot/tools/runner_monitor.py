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
import threading
import time
import traceback
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
#: Launched as `python3 tools/runner_monitor.py`, sys.path[0] is tools/, so the
#: application modules one level up (carry_shadow, the engine) are not importable
#: unless the root is added. The first deployed cycle failed on exactly this.
if BOT not in sys.path:
    sys.path.insert(1, BOT)

import run_outcomes as RO
import runner_alerts as RA  # noqa: E402
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
    env.pop(RA.ENV_VAR, None)          # the Slack webhook is the parent's, not the child's
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


def mainnet_observation(db: Optional[str],
                        observer: Optional[Callable[[], Dict[str, Any]]] = None
                        ) -> Optional[Dict[str, Any]]:
    """One observation of Bybit's PUBLIC MAINNET market, or None when none is
    configured (`tools/mainnet_public.py`: unauthenticated GETs on three
    market-data paths, its own append-only evidence file, never the testnet one).

    OBSERVATION COLLECTION, reported next to the cycle outcome and never folded
    into it: a refused, geo-blocked or broken read cannot fail a good cycle, and
    a good one proves nothing about the monitor. It never raises an Exception.
    """
    if not db:
        return None
    try:
        import mainnet_public as MPUB
        if observer is None:
            observer = MPUB.MainnetPublicReader().observe
        row = MPUB.append(db, observer())
        o = row["obs"]
        return {"state": "RECORDED", "db": db, "seq": row["seq"],
                "basis_bps": (o["perp_mark"] - o["spot_mark"]) / o["spot_mark"] * 1e4}
    except Exception as exc:                                   # noqa: BLE001
        refused = type(exc).__name__ == "MainnetRefusal"
        return {"state": "REFUSED" if refused else "FAULT", "db": db,
                "detail": f"{type(exc).__name__}: {exc}"[:300]}


def _write_json(path: str, obj: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True, default=str)
    os.replace(tmp, path)


def _history_line(root: str, record: Dict[str, Any]) -> bool:
    """Append the cycle to monitor.jsonl. False when it could not be written."""
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
        "basis_bps": decision.get("basis_bps"),
        "basis_budget_bps": decision.get("basis_budget_bps"),
    }
    mainnet = record.get("mainnet")
    if isinstance(mainnet, dict):
        line["mainnet_state"] = mainnet.get("state")
        if isinstance(mainnet.get("basis_bps"), (int, float)):
            line["mainnet_basis_bps"] = mainnet["basis_bps"]
        if mainnet.get("detail"):
            line["mainnet_detail"] = str(mainnet["detail"])[:300]
    recorder = record.get("recorder")
    if isinstance(recorder, dict):
        line["recorder_state"] = recorder.get("state")
        for key in ("age_s", "growth_age_s", "heartbeat_errors"):
            if isinstance(recorder.get(key), (int, float)):
                line["recorder_" + key] = recorder[key]
        for key in ("recorder_restarts", "recorder_watchdog_kills", "recorder_consecutive_failures"):          # already prefixed: the supervisor's own counters
            if isinstance(recorder.get(key), (int, float)):
                line[key] = recorder[key]
        for key in ("records", "free_mb", "capacity_rows", "max_fillable_usd", "chain_n"):
            if isinstance(recorder.get(key), (int, float)):
                line["recorder_" + key] = recorder[key]
        # the chain head is copied OUT of the evidence directory into this history (and from there
        # to the external watcher), so rewriting the evidence cannot also rewrite its anchor
        if recorder.get("chain_head"):
            line["recorder_chain_file"] = recorder.get("chain_file")
            line["recorder_chain_head"] = recorder["chain_head"]
        if recorder.get("fault") or recorder.get("detail"):
            line["recorder_detail"] = str(recorder.get("fault") or recorder.get("detail"))[:300]
    shadow = record.get("shadow")
    if isinstance(shadow, dict):
        line["shadow_state"] = shadow.get("state")
        line["shadow_decision_reason"] = shadow.get("decision_reason")
        if shadow.get("detail"):
            # a FAULT or REFUSED with no reason in the history is a dead end
            line["shadow_detail"] = str(shadow["detail"])[:300]
    try:
        with open(os.path.join(root, "monitor.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, sort_keys=True, default=str) + "\n")
    except OSError:
        return False
    return True


#: A recorder status file older than this is a recorder that has stopped reporting
#: (it writes every 30 s); five minutes is ten missed beats.
RECORDER_STALE_S = 300.0
#: A recorder whose HEARTBEAT is fresh but whose log has not grown for this long is alive and not persisting (a dead feed, a wedged writer, a full volume). BTCUSDT's books update many
#: times a second, so five minutes without one new record is not a quiet market.
RECORDER_NO_GROWTH_S = 300.0


def _supervisor_counters(status_path: str) -> Dict[str, Any]:
    """The supervisor's own restart counters (numbers only), for the history line: a recorder that is RECORDING at every sample but keeps being restarted is a fault the state alone hides."""
    sup = os.path.join(os.path.dirname(os.path.abspath(status_path)), "supervisor_status.json")
    try:
        with open(sup, encoding="utf-8") as fh:
            d = json.load(fh)
        return {k: d.get(k) for k in ("recorder_restarts", "recorder_watchdog_kills", "recorder_consecutive_failures") if isinstance(d.get(k), (int, float)) and not isinstance(d.get(k), bool)}
    except Exception:  # noqa: BLE001
        return {}


def _supervisor_summary(status_path: str) -> str:
    """What the supervisor (PID 1 of the runner) knows about the recorder, from ``supervisor_status.json`` in the recorder's own directory. Never raises."""
    sup = os.path.join(os.path.dirname(os.path.abspath(status_path)), "supervisor_status.json")
    try:
        with open(sup, encoding="utf-8") as fh:
            d = json.load(fh)
        alive = "running" if d.get("recorder_pid") else "not running"
        return (f"supervisor: recorder {alive}, restarts {d.get('recorder_restarts')}, consecutive failures {d.get('recorder_consecutive_failures')}, "
                f"last exit {d.get('recorder_last_exit')}, watchdog kills {d.get('recorder_watchdog_kills', 0)}")
    except FileNotFoundError:
        return "no supervisor status file"
    except Exception as exc:  # noqa: BLE001
        return f"supervisor status unreadable ({type(exc).__name__})"


def recorder_observation(path: Optional[str], *, now: Optional[float] = None
                         ) -> Optional[Dict[str, Any]]:
    """What the market recorder says about itself, or None when no recorder is configured.

    DATA COLLECTION, reported next to the cycle outcome and never folded into it: a dead
    recorder cannot fail a good cycle (the monitor's job is the read-only check), but it is
    visible in every history line and alerted on by the external watcher. Never raises."""
    if not path:
        return None
    now = time.time() if now is None else now
    try:
        with open(path, encoding="utf-8") as fh:
            st = json.load(fh)
        age = now - float(st["now_ms"]) / 1000.0
        feed = st.get("feed") or {}
        store = st.get("store") or {}
        cap = st.get("capacity") or {}
        fault = store.get("fault")
        progress = st.get("progress") or {}
        growth_age = progress.get("growth_age_s")
        state = ("STALE" if age > RECORDER_STALE_S
                 else "FAULT" if fault or not feed.get("threads_alive", False)
                 else "NOT_PERSISTING" if isinstance(growth_age, (int, float)) and growth_age > RECORDER_NO_GROWTH_S
                 else "RECORDING")
        detail = None
        if state == "STALE":
            detail = (f"recorder_status.json is {age:.0f} s old (limit {RECORDER_STALE_S:.0f} s): the recorder has stopped reporting; "
                      f"{_supervisor_summary(path)}")[:300]
        elif state == "NOT_PERSISTING":
            detail = (f"the recorder reports (heartbeat {age:.0f} s old) but its log has not grown for {growth_age:.0f} s "
                      f"(limit {RECORDER_NO_GROWTH_S:.0f} s): nothing is being persisted; {_supervisor_summary(path)}")[:300]
        return {"state": state, "age_s": round(age, 1), "detail": detail, "growth_age_s": growth_age,
                "heartbeat_errors": (st.get("heartbeat") or {}).get("status_errors"), **_supervisor_counters(path), "records": store.get("records"),
                "free_mb": None if store.get("free_bytes") is None else round(store["free_bytes"] / 1048576.0, 1),
                "fault": fault, "capacity_rows": cap.get("rows"),
                "max_fillable_usd": cap.get("max_displayed_fillable_notional_usd"),
                "chain_file": (cap.get("chain_head") or {}).get("file"),
                "chain_n": (cap.get("chain_head") or {}).get("n"),
                "chain_head": (cap.get("chain_head") or {}).get("head"),
                "entry_refusals": st.get("entry_refusals")}
    except FileNotFoundError:
        return {"state": "ABSENT", "detail": "no status file: the recorder has not started"}
    except Exception as exc:                                   # noqa: BLE001
        return {"state": "UNREADABLE", "detail": f"{type(exc).__name__}: {exc}"[:200]}


def monitor_once(run_dir: str, *, env: Mapping[str, str],
                 prober: Callable[[], int] = UD.probe,
                 runner: Callable[..., int] = UD.run_child,
                 sleep: Callable[[float], None] = time.sleep,
                 history_root: Optional[str] = None,
                 shadow_db: Optional[str] = None,
                 shadow_stepper: Optional[Callable[..., Dict[str, Any]]] = None,
                 mainnet_db: Optional[str] = None,
                 mainnet_observer: Optional[Callable[[], Dict[str, Any]]] = None,
                 recorder_status: Optional[str] = None
                 ) -> int:
    """One read-only cycle. Returns the exit code; writes `monitor.json`.

    With `shadow_db`, the cycle also feeds one public observation to the
    post-gate forward shadow book (see `shadow_observation`)."""
    os.makedirs(run_dir, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Taken first and recorded whatever else happens: a day on which testnet is
    # unreachable is still a day on which the real market had a basis.
    mainnet = mainnet_observation(mainnet_db, mainnet_observer)
    recorder = recorder_observation(recorder_status)

    def finish(result: Dict[str, Any], code: int) -> int:
        record = {"at": stamp, "schema": "monitor/1", **result}
        if mainnet is not None:
            record["mainnet"] = mainnet
        if recorder is not None:
            record["recorder"] = recorder
        _write_json(os.path.join(run_dir, "monitor.json"), record)
        if not _history_line(history_root or run_dir, record):
            # The history is the operator's only view of the runner; a lost line
            # is survivable (staleness will show it) but never silent.
            UD._say(run_dir, "WARNING: the monitor.jsonl history line could "
                             "not be written for this cycle")
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
    # THIS cycle's evidence only. The child appends to the log, and the report is
    # read back from it: an earlier cycle's report left in a reused run
    # directory (`--once`) would be classified as this cycle's when the child
    # cannot start, and would make a second report (ambiguous, refused) when it
    # can. A log that cannot be emptied cannot be trusted either.
    try:
        open(log, "wb").close()
    except OSError as exc:
        return finish({
            "outcome": RO.MONITOR_FAILED, "order_sent": False,
            "reasons": [f"the preflight log could not be emptied "
                        f"({type(exc).__name__}): an earlier report cannot "
                        "be ruled out as this cycle's"],
            "assessment": RO._assessment(None)}, EXIT_FAILED)
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


#: A cycle interval above this makes the 150-minute stale and hung limits fire
#: on a healthy runner.
MAX_INTERVAL_MINUTES = 120.0


class _Alerts:
    """Slack alerts around the loop. Everything here is contained: it cannot
    change a cycle's outcome, the loop's exit code or any gate. A stop signal is
    a BaseException and still passes through."""

    def __init__(self, root: str, notifier: Optional[RA.Notifier],
                 clock: Callable[[], float],
                 forward_artifact: Optional[str] = None) -> None:
        self.root, self.notifier, self.clock = root, notifier, clock
        #: A forward-shadow artefact whose freshness is checked each cycle
        #: (runner_alerts.forward_status); None: no forward check.
        self.forward_artifact = forward_artifact
        self.throttle = RA.Throttle()
        self._digest_last = clock()
        self.watchdog_throttle = RA.Throttle()   # its own: one shared set of
        # 'active' keys made the watchdog's [hung] look like `stale` clearing
        self.watchdog = RA.Watchdog(clock=clock)

    def _deliver(self, throttle: "RA.Throttle", alert: "RA.Alert") -> str:
        """Send one alert. An alert Slack did not accept stays OWED: the throttle
        forgets it was sent, so the next cycle tries again."""
        result = self.notifier.send(alert)
        if result == "sent":
            throttle.delivered(alert)
        else:
            throttle.unsent(alert)
        UD._say(self.root, f"alert {alert.key} {alert.severity}: {result}")
        return result

    def announce(self) -> None:
        """One INFO message per process start: proof the secret is wired and the
        connection works, after every deploy, restart and crash-restart."""
        try:
            n = self.notifier
            if n is None or not n.enabled:
                UD._say(self.root, "ALERTS DISABLED: "
                        + (n.disabled_reason if n is not None else
                           f"no notifier configured ({RA.ENV_VAR} unread)")
                        + "; a dead or failing runner will not page anyone")
                return
            result = n.send(RA.Alert(
                "startup", RA.INFO,
                f"alerts are ON: the monitor started. Pages: no cycle within "
                f"{RA.STALE_MINUTES:g} min, a MONITOR_FAILED streak, a shadow "
                "fault, any order_sent other than false"
                + (", stale/unreadable/missing forward-shadow evidence"
                   if self.forward_artifact else "")
                + ". One digest a day (a WARNING when the history is not "
                "healthy); silence otherwise means healthy."))
            UD._say(self.root, f"alert startup {RA.INFO}: {result}")
        except Exception as exc:                               # noqa: BLE001
            UD._say(self.root, f"alert startup failed ({type(exc).__name__}); "
                               "the monitor is unaffected")

    def after_cycle(self) -> None:
        try:
            self.watchdog.beat()
            n = self.notifier
            if n is None or not n.enabled:
                # Loud, every cycle: a runner that cannot page anyone must say so.
                UD._say(self.root, "ALERTS DISABLED: "
                        + (n.disabled_reason if n is not None else
                           f"no notifier configured ({RA.ENV_VAR} unread)")
                        + "; a dead or failing runner will not page anyone")
                return
            try:
                with open(os.path.join(self.root, "monitor.jsonl"),
                          encoding="utf-8", errors="replace") as fh:
                    raw = fh.read()
            except OSError:
                raw = ""
            now = self.clock()
            forward = RA.forward_status(self.forward_artifact, now)
            for alert in self.throttle.plan(RA.evaluate(raw, now, forward=forward),
                                            now):
                self._deliver(self.throttle, alert)
            if now - self._digest_last >= RA.DIGEST_INTERVAL_S:
                # the clock only advances when the digest was DELIVERED
                digest = RA.build_digest(raw, now, forward=forward)
                if self._deliver(self.throttle, digest) == "sent":
                    self._digest_last = now
        except Exception as exc:                               # noqa: BLE001
            UD._say(self.root, f"alert step failed ({type(exc).__name__}); "
                               "the monitor is unaffected")

    def start_watchdog(self) -> Callable[[], None]:
        """A thread that reports a loop stuck INSIDE a cycle. Returns its stop."""
        n = self.notifier
        if n is None or not n.enabled:
            return lambda: None
        stop = threading.Event()

        def run() -> None:
            while not stop.wait(RA.WATCHDOG_POLL_S):
                try:
                    hung = self.watchdog.tick()
                    if hung is not None:
                        for a in self.watchdog_throttle.plan([hung], self.clock()):
                            self._deliver(self.watchdog_throttle, a)
                except Exception as exc:                       # noqa: BLE001
                    UD._say(self.root, f"watchdog failed ({type(exc).__name__})")

        t = threading.Thread(target=run, name="alert-watchdog", daemon=True)
        t.start()
        return stop.set


def loop(root: str, *, env: Mapping[str, str], interval_s: float,
         max_runs: int = 0, clock: Callable[[], float] = time.time,
         sleep: Callable[[float], None] = time.sleep,
         once: Callable[..., int] = monitor_once,
         shadow_db: Optional[str] = None,
         notifier: Optional[RA.Notifier] = None,
         mainnet_db: Optional[str] = None,
         forward_artifact: Optional[str] = None,
         recorder_status: Optional[str] = None) -> int:
    """A stop signal ends the loop with 143 wherever it lands, including the
    sleep between cycles where it spends nearly all its time: under the Fly
    on-failure restart policy a deploy or stop must not look like a failure."""
    alerts = _Alerts(root, notifier, clock, forward_artifact=forward_artifact)
    alerts.announce()
    stop_watchdog = alerts.start_watchdog()
    try:
        return _loop(root, env=env, interval_s=interval_s, max_runs=max_runs,
                     clock=clock, sleep=sleep, once=once, shadow_db=shadow_db,
                     alerts=alerts, mainnet_db=mainnet_db,
                     recorder_status=recorder_status)
    except UD.Shutdown as exc:
        UD._say(root, f"told to stop ({exc}): monitor loop ends")
        return EXIT_SIGNALLED
    finally:
        stop_watchdog()


def _record_crash(root: str, exc: BaseException,
                  clock: Callable[[], float]) -> int:
    """A cycle raised something unexpected. Say so in the history and the log and
    return the failing exit code; the loop goes on to its next cycle.

    Only the exception TYPE and where it was raised are recorded, never its text:
    an exception message can carry whatever the failing call was handling. The
    history line is a MONITOR_FAILED, so the existing failed-streak alert pages on
    two in a row, and `order_sent` is False, as it always is for this monitor.
    """
    frames = traceback.extract_tb(exc.__traceback__)
    where = (f"{os.path.basename(frames[-1].filename)}:{frames[-1].lineno}"
             if frames else "unknown")
    kind = type(exc).__name__
    UD._say(root, f"cycle crashed ({kind}) at {where}; recorded as MONITOR_FAILED, "
                  "the loop continues")
    record = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock())),
              "outcome": RO.MONITOR_FAILED, "order_sent": False,
              "reasons": [f"the monitor cycle crashed: {kind} ({where})"]}
    if not _history_line(root, record):
        UD._say(root, "WARNING: the monitor.jsonl history line could not be "
                      "written for this crashed cycle")
    return EXIT_FAILED


def _loop(root: str, *, env: Mapping[str, str], interval_s: float,
          max_runs: int, clock: Callable[[], float],
          sleep: Callable[[float], None], once: Callable[..., int],
          shadow_db: Optional[str] = None,
          alerts: Optional["_Alerts"] = None,
          mainnet_db: Optional[str] = None,
          recorder_status: Optional[str] = None) -> int:
    alerts = alerts or _Alerts(root, None, clock)
    runs = 0
    while True:
        if os.path.exists(os.path.join(root, "STOP")):
            UD._say(root, "STOP file present: monitor loop ends")
            return EXIT_OK
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(clock()))
        try:
            extra = {"shadow_db": shadow_db} if shadow_db else {}
            if mainnet_db:
                extra["mainnet_db"] = mainnet_db
            if recorder_status:
                extra["recorder_status"] = recorder_status
            code = once(os.path.join(root, f"run-{stamp}"), env=env,
                        history_root=root, **extra)
        except UD.Shutdown as exc:
            UD._say(root, f"told to stop ({exc}): monitor loop ends")
            return EXIT_SIGNALLED
        except Exception as exc:                               # noqa: BLE001
            code = _record_crash(root, exc, clock)
        runs += 1
        alerts.after_cycle()
        try:
            UD.prune_runs(root)
        except Exception as exc:                               # noqa: BLE001
            UD._say(root, f"prune failed ({type(exc).__name__}); the loop continues")
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
    ap.add_argument("--mainnet-db", default=None,
                    help="also append one observation per cycle of Bybit's PUBLIC "
                         "MAINNET market (unauthenticated GETs, read-only) to this "
                         "JSONL evidence file; never the testnet shadow book")
    ap.add_argument("--recorder-status", default=None,
                    help="copy the market recorder's health (its recorder_status.json) "
                         "into every history line, so the external watcher can alert "
                         "when the recorder stops (read-only)")
    ap.add_argument("--forward-artifact", default=None,
                    help="alert when this forward-shadow artefact (e.g. "
                         "artifacts/forward_shadow_current.json) is stale, "
                         "unreadable or missing (forward_status.py; read-only)")
    args = ap.parse_args(argv)
    missing = [k for k in UD.CREDENTIALS if not os.environ.get(k)]
    if missing:
        print(f"refusing: {', '.join(missing)} not set in the environment "
              "(testnet credentials; never printed)", file=sys.stderr)
        return EXIT_REFUSED
    # `not (x >= 1.0)`, not `x < 1.0`: NaN fails both comparisons, and a NaN
    # interval never sleeps (`waited < nan` is false), an infinite one never
    # starts a second cycle.
    if args.max_runs < 0 or not (1.0 <= args.interval_minutes <= MAX_INTERVAL_MINUTES):
        print("refusing: --max-runs must be >= 0 and --interval-minutes between 1 "
              f"and {MAX_INTERVAL_MINUTES:g} (the stale and hung alerts fire at "
              f"{RA.STALE_MINUTES:g} min)",
              file=sys.stderr)
        return EXIT_REFUSED
    UD.install_signal_handlers()
    if args.once:
        try:
            return monitor_once(args.run_dir, env=os.environ,
                                shadow_db=args.shadow_db,
                                recorder_status=args.recorder_status)
        except UD.Shutdown as exc:
            print(f"told to stop ({exc})", flush=True)
            return EXIT_SIGNALLED
    lock = UD.acquire_lock(args.run_dir)
    if lock is None:
        print("refusing: another loop is running on this run root",
              file=sys.stderr)
        return EXIT_REFUSED
    if args.mainnet_db and (
            args.mainnet_db == args.shadow_db
            or args.mainnet_db.lower().endswith((".db", ".sqlite", ".sqlite3"))):
        print("refusing: --mainnet-db is its own JSONL evidence file, never the "
              "shadow book or a SQLite database", file=sys.stderr)
        return EXIT_REFUSED
    try:
        notifier = RA.Notifier(os.environ,
                               say=lambda line: UD._say(args.run_dir, line))
        return loop(args.run_dir, env=os.environ, notifier=notifier,
                    interval_s=args.interval_minutes * 60.0,
                    max_runs=args.max_runs, shadow_db=args.shadow_db,
                    mainnet_db=args.mainnet_db,
                    forward_artifact=args.forward_artifact,
                    recorder_status=args.recorder_status)
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
