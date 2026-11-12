#!/usr/bin/env python3
"""The unattended autopilot's PERSISTENT operating state and its owner-facing markers. Pure file semantics (atomic writes), no network, no orders.

STATES (every transition has a source event and a timestamp; the file is the truth, not the process): INITIALISING, DATA_CHECK, RESEARCHING, WAITING_FOR_OPPORTUNITY, PLAN_PROPOSED,
RISK_APPROVED, EXECUTING, HEDGED, EXITING, RECONCILING, FLAT, PAUSED, ALERT, STOPPED, RECOVERING, UNRECONCILED, DRAINING.

MARKERS (files in the autopilot root; each means something DIFFERENT and none is cleared by a restart):
  ALERT    a critical incident (NOT_FLAT, unexpected refusal, repeated errors, an unresolved prior session). No new exposure until a human clears it AFTER the venue proves flat.
  PAUSED   trading authority or evidence is missing or ambiguous (no/expired/revoked grant, lease not held, recovery could not prove flat). Cleared by the owner's `resume`.
  STOP     the owner's intentional stop: no new sessions. The process stays alive (so an always-on restart policy cannot loop) and keeps reporting; remove STOP to resume.
  DRAIN    the owner asks for a safe deployment: no new sessions; the active one finishes or unwinds; DRAINED is written once the venue is proven flat.
  DRAINED  written by the loop only: {at_ms, venue_flat_proof}. Deployment is safe only while it is fresh.
The process never exits because of a marker; it exits only on a stop SIGNAL (SIGTERM/SIGHUP/SIGINT) or a fatal error.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Callable, Dict, List, Optional

STATES = ("INITIALISING", "DATA_CHECK", "RESEARCHING", "WAITING_FOR_OPPORTUNITY", "PLAN_PROPOSED", "RISK_APPROVED", "EXECUTING", "HEDGED", "EXITING", "RECONCILING", "FLAT",
          "PAUSED", "ALERT", "STOPPED", "RECOVERING", "UNRECONCILED", "DRAINING")
HEALTHY_IDLE = frozenset({"WAITING_FOR_OPPORTUNITY", "RESEARCHING", "DATA_CHECK", "FLAT"})
NO_NEW_EXPOSURE = frozenset({"PAUSED", "ALERT", "STOPPED", "RECOVERING", "UNRECONCILED", "DRAINING", "INITIALISING"})
IN_SESSION = frozenset({"PLAN_PROPOSED", "RISK_APPROVED", "EXECUTING", "HEDGED", "EXITING", "RECONCILING"})
MARKERS = ("ALERT", "PAUSED", "STOP", "DRAIN", "DRAINED")
HISTORY_MAX = 100


def write_json_atomic(path: str, obj: Any) -> None:
    """temp file in the same directory + fsync + rename (+ directory fsync): a crash leaves the old file or the new one, never half of either."""
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, sort_keys=True, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def read_json(path: str) -> Optional[Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError, RecursionError):                       # a deeply nested file is as unreadable as a corrupt one (RecursionError is not a ValueError)
        return None


class EngineState:
    def __init__(self, root: str, clock: Callable[[], int]) -> None:
        self.root, self.clock = root, clock
        os.makedirs(root, mode=0o700, exist_ok=True)

    # ---- the state file
    @property
    def path(self) -> str:
        return os.path.join(self.root, "engine_state.json")

    def load(self) -> Dict[str, Any]:
        d = read_json(self.path)
        return d if isinstance(d, dict) and d.get("state") in STATES else {"state": "INITIALISING", "since_ms": None, "source": "no_state_file", "history": [], "seq": 0}

    def previous_state(self) -> str:
        """The state the PREVIOUS process left (read before the first transition of this one): a session state here means it died mid-session."""
        return str(self.load()["state"])

    def transition(self, state: str, source: str, **extra: Any) -> Dict[str, Any]:
        if state not in STATES:
            raise ValueError(f"unknown engine state {state!r}")
        cur = self.load()
        now = int(self.clock())
        if cur["state"] == state and not extra:
            return cur
        hist: List[Dict[str, Any]] = list(cur.get("history", []))[-(HISTORY_MAX - 1):]
        hist.append({"state": state, "at_ms": now, "source": source})
        new = {"state": state, "since_ms": now, "source": source, "seq": int(cur.get("seq", 0)) + 1, "history": hist, **extra}
        write_json_atomic(self.path, new)
        return new

    # ---- markers
    def marker_path(self, name: str) -> str:
        if name not in MARKERS:
            raise ValueError(name)
        return os.path.join(self.root, name)

    def has(self, name: str) -> bool:
        return os.path.lexists(self.marker_path(name))

    def set(self, name: str, payload: Dict[str, Any]) -> None:
        write_json_atomic(self.marker_path(name), {**payload, "at_ms": int(self.clock())})
        self._log(name, "SET", payload)

    def clear(self, name: str, reason: str) -> bool:
        try:
            os.unlink(self.marker_path(name))
        except FileNotFoundError:
            return False
        self._log(name, "CLEARED", {"reason": reason})
        return True

    def read(self, name: str) -> Optional[Any]:
        return read_json(self.marker_path(name))

    def audit(self, name: str, action: str, payload: Dict[str, Any]) -> None:
        """An OWNER action that is not itself a marker (a capital acknowledgement, a declared transfer, a refused attempt) gets the same append-only, fsynced row. Raises ``OSError`` when the row cannot
        be written: a caller that must not change anything unaudited lets that stop it."""
        self._log(name, action, payload)

    def _log(self, name: str, action: str, payload: Dict[str, Any]) -> None:
        """Append-only marker log: a cleared ALERT is still on record (who/why/when)."""
        fd = os.open(os.path.join(self.root, "marker_log.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.write(fd, (json.dumps({"marker": name, "action": action, "at_ms": int(self.clock()), "payload": payload}, sort_keys=True, default=str) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
