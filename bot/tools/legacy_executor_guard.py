#!/usr/bin/env python3
"""SINGLE-WRITER guard for the LEGACY order-capable runs (``tools/unattended_drill.py --conformance`` on the carry-drill app).

The unattended autopilot is the one order-capable service, and it holds a global lease in Supabase (fencing token, server clock). A second order-capable process on the same Testnet account
would be a second hedge. This guard makes the legacy conformance run take THE SAME lease before every armed run, so the two can never trade concurrently:

  * no Supabase configuration (SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_OWNER_ID)  ->  REFUSED: the run places nothing (the deployed default, so the legacy path is inert)
  * the lease is held by another executor (the autopilot, another legacy run)                 ->  REFUSED
  * the lease backend does not answer                                                          ->  REFUSED (an unanswerable authority check is not authority)
  * otherwise the lease is held (ttl up to an hour: it covers one armed run; a crash frees it by expiry, never forever) and RELEASED in ``finally``

Fail-closed everywhere; it never acquires on behalf of anyone else and never touches the exchange. It imports ``exec_evidence`` lazily so a host without Supabase config never loads a network client.
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

TTL_SECONDS = 3600
SERVICE_NAME = "legacy-drill"


class Handle:
    def __init__(self, lease: Any) -> None:
        self._lease = lease
        self.released = False

    @property
    def token(self) -> Optional[int]:
        return getattr(self._lease, "token", None)

    def renew(self) -> bool:
        """The SERVER's answer (an ``exec_lease_renew``), never this process's memory of a deadline. False on any doubt: a renewal that cannot be proven is a lost lease."""
        if self.released:
            return False
        try:
            return bool(self._lease.renew())
        except Exception:                                            # noqa: BLE001
            return False

    def release(self) -> bool:
        if self.released:
            return False
        self.released = True
        try:
            return bool(self._lease.release())
        except Exception:                                            # noqa: BLE001 - a failed release is freed by the ttl; it must never mask the run's own result
            return False


def acquire(env: Mapping[str, str], *, lease_factory: Optional[Callable[[str, str, str, int], Any]] = None, ttl_seconds: int = TTL_SECONDS,
            service: str = SERVICE_NAME) -> Tuple[Optional[Handle], str]:
    """(handle, "") when the shared lease is held now; (None, reason) otherwise. ``lease_factory(url, key, owner, holder)`` exists so tests need no network."""
    url, key, owner = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_ROLE_KEY"), env.get("SUPABASE_OWNER_ID")
    if not (url and key and owner):
        return None, "NO_GLOBAL_LEASE_CONFIGURED: the autopilot is the only order-capable service; this legacy path places nothing without the shared lease"
    holder = f"{service}:{env.get('FLY_MACHINE_ID') or 'host'}:{uuid.uuid4().hex[:8]}"
    try:
        if lease_factory is not None:
            lease = lease_factory(url, key, owner, holder)               # (the factory receives the default ttl; tests build their own client)
        else:
            import exec_evidence as XE
            lease = XE.LeaseClient(XE.RestClient(url, key, retries=0), owner, holder, ttl_seconds=int(ttl_seconds))
        ok = bool(lease.acquire())
    except Exception as exc:                                         # noqa: BLE001 - includes configuration refusals and a backend that does not answer
        return None, f"LEASE_UNAVAILABLE:{type(exc).__name__}"
    if not ok:
        return None, "LEASE_HELD_BY_ANOTHER_EXECUTOR"
    return Handle(lease), ""


def single_writer(env: Mapping[str, str], *, state_dir: Optional[str] = None) -> Tuple[Optional[Callable[[], None]], str]:
    """The authority any ARMED legacy entry tool needs before it may send an order: the local account lock (``ms_executor_lock``: one writer per account on this host) AND the shared Supabase lease (one
    executor per account everywhere). Started BY the guarded unattended drill (which already holds both) a child recognises its parent (the lock's note names the parent's pid and role) and proceeds.
    Returns (release, "") or (None, reason); no lease configuration, a lease held elsewhere or an unreadable lock all REFUSE, and anything taken is given back on a refusal."""
    import ms_executor_lock as XL
    path = XL.resolve_path(env)
    lock = XL.ExecutorLock(path, role="legacy-armed", state_dir=state_dir or os.getcwd(), now_ms=int(time.time() * 1000))
    if lock.try_acquire():
        handle, why = acquire(env)
        if handle is None:
            lock.release()
            return None, why

        def release() -> None:
            handle.release()
            lock.release()
        return release, ""
    holder = XL.read_holder(path)
    if holder is not None and holder["role"] == "legacy-drill" and holder["pid"] == os.getppid():
        return (lambda: None), ""                                          # the guarded parent holds the lock and the lease for this child's whole life
    return None, ("another order-capable process holds the account lock" + (f" ({holder['role']}, pid {holder['pid']})" if holder else "")
                  + "; two writers on one Testnet account are never allowed")


def run_guarded_cli(main_fn: Callable[[Optional[list]], int], argv: Optional[Sequence[str]] = None, *, env: Mapping[str, str],
                    safe_flags: Sequence[str] = ("-h", "--help", "--dry-run", "--self-check-offline")) -> int:
    """The ``__main__`` wrapper of a legacy tool that can send orders: unless the command line asks only for help / a dry run / an offline self-check (no network, no order), it first takes
    ``single_writer``. ``main_fn`` itself is unchanged (its own tests and gates still apply); only the command-line entry is guarded."""
    args = list(sys.argv[1:] if argv is None else argv)
    if any(a in safe_flags or a.split("=", 1)[0] in safe_flags for a in args):
        return main_fn(args)
    release, why = single_writer(env)                                   # the caller passes its own variables in: this module reads none by itself
    if release is None:
        print(f"REFUSED: {why}", file=sys.stderr)
        return 2
    try:
        return main_fn(args)
    finally:
        release()
