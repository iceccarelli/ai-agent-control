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

import uuid
from typing import Any, Callable, Mapping, Optional, Tuple

TTL_SECONDS = 3600
SERVICE_NAME = "legacy-drill"


class Handle:
    def __init__(self, lease: Any) -> None:
        self._lease = lease
        self.released = False

    @property
    def token(self) -> Optional[int]:
        return getattr(self._lease, "token", None)

    def release(self) -> bool:
        if self.released:
            return False
        self.released = True
        try:
            return bool(self._lease.release())
        except Exception:                                            # noqa: BLE001 - a failed release is freed by the ttl; it must never mask the run's own result
            return False


def acquire(env: Mapping[str, str], *, lease_factory: Optional[Callable[[str, str, str, int], Any]] = None) -> Tuple[Optional[Handle], str]:
    """(handle, "") when the shared lease is held now; (None, reason) otherwise. ``lease_factory(url, key, owner, holder)`` exists so tests need no network."""
    url, key, owner = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_ROLE_KEY"), env.get("SUPABASE_OWNER_ID")
    if not (url and key and owner):
        return None, "NO_GLOBAL_LEASE_CONFIGURED: the autopilot is the only order-capable service; this legacy path places nothing without the shared lease"
    holder = f"{SERVICE_NAME}:{env.get('FLY_MACHINE_ID') or 'host'}:{uuid.uuid4().hex[:8]}"
    try:
        if lease_factory is not None:
            lease = lease_factory(url, key, owner, holder)
        else:
            import exec_evidence as XE
            lease = XE.LeaseClient(XE.RestClient(url, key, retries=0), owner, holder, ttl_seconds=TTL_SECONDS)
        ok = bool(lease.acquire())
    except Exception as exc:                                         # noqa: BLE001 - includes configuration refusals and a backend that does not answer
        return None, f"LEASE_UNAVAILABLE:{type(exc).__name__}"
    if not ok:
        return None, "LEASE_HELD_BY_ANOTHER_EXECUTOR"
    return Handle(lease), ""
