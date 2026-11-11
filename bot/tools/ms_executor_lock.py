#!/usr/bin/env python3
"""ONE LOCAL WRITER per Testnet account on this host: a kernel advisory lock (``flock``) held for the whole time an order-capable session (``execute``) or a reconcile / unwind is running.

Why it exists. The Supabase lease keeps two EXECUTORS apart (the autopilot, a manual execute, the legacy drill) but ``reconcile`` / ``reconcile --unwind`` take no lease, so an owner's emergency unwind could
run concurrently with the autopilot's own session: two processes appending to one hash-chained journal (reproduced: the second writer produces a sequence gap that makes every later load refuse the
journal, which disables the emergency tool itself), a perp bought back while the autopilot is still sending its spot leg (a naked spot long), exits racing each other. The lock makes that impossible on one
host without ever blocking an exit:

  * ``execute`` holds the lock for the whole session (the autopilot calls ``execute`` in-process, so it is covered automatically). A second ``execute`` is REFUSED while it is held.
  * ``reconcile`` (read, adopt, cancel) is REFUSED while it is held: it can cancel a live entry leg and must not interleave with the session that owns the journal.
  * ``reconcile --unwind`` (the emergency exit) never gives up and never stacks a second writer: when the lock is held it reads WHO holds it, engages THAT session's kill switch (a file: cancel entries,
    reduce-only unwind, flat check, all by the single journal writer that already owns the lock) and waits a bounded time for the lock; once it holds the lock it verifies the venue and exits. If the holder
    never lets go, the unwind exits non-zero and says who to stop. A reduce-only exit is therefore always available and always the only writer.
  * The kernel releases the lock when the process dies, so there is no TTL, no stale lock and nothing to clear by hand. The holder's identity is a JSON note written AFTER the lock is taken (pid, role,
    state dir, start time); a reader treats a malformed note as 'unknown holder'.

This is a LOCAL lock (same host / same volume as the owner capability file). Across hosts the Supabase lease remains the account-level authority. It sends nothing and imports nothing that can reach a venue.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

try:
    import fcntl as _fcntl
except ImportError:                                                  # pragma: no cover - non-POSIX: no lock available, so nothing may claim to hold one
    _fcntl = None

LOCK_NAME = "ms_executor.lock"


def lock_path(capability_path: str) -> str:
    """The lock lives beside the owner capability file (the same directory the per-day entry counter uses): one per account directory, never inside a repository work tree."""
    return os.path.join(os.path.dirname(os.path.abspath(capability_path)), LOCK_NAME)


def resolve_path(env: Any) -> str:
    """Where the lock lives for a process that has no capability path of its own (the legacy drill and its guarded parent): ``MS_EXECUTOR_LOCK_PATH`` if the parent passed one, else the same
    per-account directory the autopilot and the session tool use (``~/.ai-agent-control``)."""
    p = (env.get("MS_EXECUTOR_LOCK_PATH") if hasattr(env, "get") else None) or os.environ.get("MS_EXECUTOR_LOCK_PATH")
    return str(p) if p else os.path.join(os.path.expanduser("~"), ".ai-agent-control", LOCK_NAME)


class ExecutorLock:
    def __init__(self, path: str, *, role: str, state_dir: str, now_ms: int) -> None:
        self.path, self.role, self.state_dir, self.now_ms = path, role, state_dir, int(now_ms)
        self._fd: Optional[int] = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        """Non-blocking. True = this process now holds the lock (and has written who it is). False = another process holds it, or no lock could be taken at all (fail closed)."""
        if _fcntl is None or self._fd is not None:
            return self._fd is not None
        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError:
            return False
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError:                                              # BlockingIOError: held by someone else; anything else: not held by us either
            os.close(fd)
            return False
        try:
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps({"pid": os.getpid(), "role": self.role, "state_dir": self.state_dir, "started_ms": self.now_ms}, sort_keys=True).encode("utf-8"))
            os.fsync(fd)
        except OSError:
            pass                                                     # the note is advisory; holding the lock is what matters
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            os.ftruncate(self._fd, 0)
        except OSError:
            pass
        try:
            os.close(self._fd)                                       # closing the descriptor drops the flock
        finally:
            self._fd = None

    def __enter__(self) -> "ExecutorLock":
        return self

    def __exit__(self, *_a: Any) -> None:
        self.release()


def read_holder(path: str) -> Optional[Dict[str, Any]]:
    """Who the lock file says holds it, or None (no note, unreadable, malformed). A note is only meaningful while the lock is held: the holder clears it on release."""
    try:
        with open(path, "rb") as fh:
            doc = json.loads(fh.read(4096).decode("utf-8") or "null")
    except (OSError, ValueError):
        return None
    if not (isinstance(doc, dict) and isinstance(doc.get("state_dir"), str) and doc["state_dir"] and isinstance(doc.get("pid"), int)):
        return None
    return {"pid": doc["pid"], "role": str(doc.get("role"))[:40], "state_dir": doc["state_dir"], "started_ms": doc.get("started_ms")}
