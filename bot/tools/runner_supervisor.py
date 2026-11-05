#!/usr/bin/env python3
"""PID 1 of the read-only runner: keeps the monitor and the market recorder both alive.

    python3 tools/runner_supervisor.py [--recorder --recorder-dir D ...] -- <monitor command>

The monitor (``runner_monitor.py``) is the runner's reason to exist and must not be
disturbed by anything. The recorder (``market_recorder.py``) is an extra: a second
process that records the depth feed. This supervisor makes that arrangement safe:

  * the MONITOR is the primary: when it exits, the recorder is stopped and the supervisor
    exits with the monitor's exit code, so the platform's restart policy sees exactly what
    it saw before the recorder existed;
  * the RECORDER is restarted when it dies, with a capped exponential backoff that resets
    after it has run stably; a recorder that cannot start never stops the monitor;
  * SIGTERM/SIGINT (a deploy, a stop) is forwarded to both, with a grace period, then
    SIGKILL; the supervisor exits 143;
  * without ``--recorder`` it replaces itself with the monitor (``exec``), i.e. it is
    byte-for-byte the previous behaviour.

It places no order and reads no credential. Its own state is ``supervisor_status.json`` in
the recorder directory (atomic).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
EXIT_SIGNALLED = 143
STATUS_FILE = "supervisor_status.json"


def _atomic_write(path: str, obj: Any) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class Supervisor:
    def __init__(self, monitor_cmd: Sequence[str], recorder_cmd: Optional[Sequence[str]], *,
                 status_dir: Optional[str] = None, grace_s: float = 30.0,
                 backoff_base_s: float = 5.0, backoff_max_s: float = 300.0,
                 stable_after_s: float = 600.0, poll_s: float = 0.25,
                 mono: Callable[[], float] = time.monotonic) -> None:
        self.monitor_cmd, self.recorder_cmd = list(monitor_cmd), list(recorder_cmd) if recorder_cmd else None
        self.status_dir, self.grace_s = status_dir, float(grace_s)
        self.backoff_base_s, self.backoff_max_s = backoff_base_s, backoff_max_s
        self.stable_after_s, self.poll_s, self.mono = stable_after_s, poll_s, mono
        self.monitor: Optional[subprocess.Popen] = None
        self.recorder: Optional[subprocess.Popen] = None
        self.recorder_started_at = 0.0
        self.recorder_restarts = 0
        self.recorder_failures = 0            # consecutive short lives, drives the backoff
        self.recorder_next_start = 0.0
        self.recorder_last_exit: Optional[int] = None
        self.stopping = False
        self.started_wall = time.time()

    # -- children --------------------------------------------------------------------------------------
    def _spawn(self, cmd: Sequence[str]) -> subprocess.Popen:
        return subprocess.Popen(list(cmd))

    def _start_recorder(self) -> None:
        try:
            self.recorder = self._spawn(self.recorder_cmd or [])
            self.recorder_started_at = self.mono()
        except OSError as exc:
            print(f"supervisor: the recorder could not be started ({type(exc).__name__}); "
                  "the monitor is unaffected", file=sys.stderr, flush=True)
            self.recorder = None
            self._schedule_restart(0.0)

    def _schedule_restart(self, lived_s: float) -> None:
        self.recorder_failures = 0 if lived_s >= self.stable_after_s else self.recorder_failures + 1
        delay = min(self.backoff_max_s, self.backoff_base_s * (2 ** max(0, self.recorder_failures - 1)))
        self.recorder_next_start = self.mono() + delay

    def _write_status(self) -> None:
        if not self.status_dir:
            return
        try:
            os.makedirs(self.status_dir, exist_ok=True)
            _atomic_write(os.path.join(self.status_dir, STATUS_FILE), {
                "schema": "supervisor/1", "pid": os.getpid(), "started": self.started_wall,
                "now": time.time(),
                "monitor_pid": getattr(self.monitor, "pid", None),
                "recorder_enabled": self.recorder_cmd is not None,
                "recorder_pid": getattr(self.recorder, "pid", None),
                "recorder_restarts": self.recorder_restarts,
                "recorder_consecutive_failures": self.recorder_failures,
                "recorder_last_exit": self.recorder_last_exit})
        except OSError:
            pass                           # status is a convenience, never a reason to stop

    def _terminate(self, procs: List[Optional[subprocess.Popen]]) -> None:
        live = [p for p in procs if p is not None and p.poll() is None]
        for p in live:
            try:
                p.terminate()
            except OSError:
                pass
        deadline = self.mono() + self.grace_s
        while live and self.mono() < deadline:
            live = [p for p in live if p.poll() is None]
            if live:
                time.sleep(min(self.poll_s, 0.1))
        for p in live:
            try:
                p.kill()
                p.wait(5)
            except OSError:
                pass

    def request_stop(self, *_a: Any) -> None:
        self.stopping = True

    # -- the loop --------------------------------------------------------------------------------------
    def run(self) -> int:
        self.monitor = self._spawn(self.monitor_cmd)
        if self.recorder_cmd:
            self._start_recorder()
        self._write_status()
        last_status = self.mono()
        while True:
            if self.stopping:
                self._terminate([self.monitor, self.recorder])
                self._write_status()
                return EXIT_SIGNALLED
            rc = self.monitor.poll()
            if rc is not None:
                self._terminate([self.recorder])
                self._write_status()
                return int(rc)
            if self.recorder_cmd:
                if self.recorder is not None:
                    r = self.recorder.poll()
                    if r is not None:
                        lived = self.mono() - self.recorder_started_at
                        self.recorder_last_exit = int(r)
                        self.recorder = None
                        self.recorder_restarts += 1
                        self._schedule_restart(lived)
                        print(f"supervisor: the recorder exited {r} after {lived:.0f}s; restart in "
                              f"{self.recorder_next_start - self.mono():.0f}s", file=sys.stderr, flush=True)
                        self._write_status()
                elif self.mono() >= self.recorder_next_start:
                    self._start_recorder()
                    self._write_status()
            if self.mono() - last_status >= 30.0:
                last_status = self.mono()
                self._write_status()
            time.sleep(self.poll_s)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--" not in args:
        print("usage: runner_supervisor.py [options] -- <monitor command>", file=sys.stderr)
        return 3
    cut = args.index("--")
    opts, monitor_cmd = args[:cut], args[cut + 1:]
    if not monitor_cmd:
        print("no monitor command after --", file=sys.stderr)
        return 3
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--recorder", action="store_true")
    ap.add_argument("--recorder-dir", default="/data/market")
    ap.add_argument("--recorder-environment", choices=("mainnet", "testnet"), default="mainnet")
    ap.add_argument("--recorder-min-free-mb", type=int, default=512)
    ap.add_argument("--recorder-retention-days", type=int, default=2)
    ap.add_argument("--grace-s", type=float, default=30.0)
    a = ap.parse_args(opts)

    if not a.recorder:
        os.execvp(monitor_cmd[0], monitor_cmd)      # exactly the previous behaviour
        return 3                                     # not reached

    recorder_cmd = [sys.executable, os.path.join(HERE, "market_recorder.py"),
                    "--dir", a.recorder_dir, "--environment", a.recorder_environment,
                    "--min-free-mb", str(a.recorder_min_free_mb),
                    "--retention-days", str(a.recorder_retention_days)]
    sup = Supervisor(monitor_cmd, recorder_cmd, status_dir=a.recorder_dir, grace_s=a.grace_s)
    signal.signal(signal.SIGTERM, sup.request_stop)
    signal.signal(signal.SIGINT, sup.request_stop)
    return sup.run()


if __name__ == "__main__":
    sys.exit(main())
