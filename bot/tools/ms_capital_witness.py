#!/usr/bin/env python3
"""THE SUPABASE WITNESS for the capital lineage (``ms_capital_anchor.Anchor`` talks to any object with this shape). It holds, OUTSIDE the machine, one small append-only row per lineage head, so a rollback of the
whole local volume (which no local copy can show) and a forked copy of the account's state become refusals.

NO MIGRATION. It uses only what the executor-lease migration (20261110000100) already provides: ``carry.exec_event_put`` (an idempotent writer: INSERTED / UNCHANGED / CONFLICT, with ``event_id`` as the key) and the
service role's SELECT on ``carry.exec_events`` (the read-only monitor already reads this table over PostgREST). Nothing here is a trading authority: like the lease it can only make the system REFUSE.

  write   a synchronous ``rpc exec_event_put`` of one PROVENANCE event per head: event_id ``capital:<acct24>:<seq 9 digits>``, a payload of short letter-led refs (never the raw hash, the account or any balance
          figure the evidence guard refuses) and the few stop-relevant facts. The content is a pure function of the lineage entry, so a retry after a lost response is UNCHANGED, and a DIFFERENT head at the
          same sequence is CONFLICT (two histories for one account).
  read    ``GET exec_events`` filtered to this book, newest sequence first, limit 1. An empty answer is what a key that cannot SEE the rows also gets, so "no history" is believed only after a VISIBILITY PROOF: an
          idempotent canary event is written and read back by its id. If it cannot be read back the witness says WitnessError, never "nothing is known".

WHAT IT CANNOT DO (stated, not hidden). The service role can write any row (``exec_events`` stores records, not proof): an operator holding the Fly secrets can append a chain-continuing head and is attributable, not
prevented. The database does not check chain continuity: the CLIENT reads the remote head and compares before it publishes. The owner must expose schema ``carry`` to PostgREST (the lease needs it too) and set
``SUPABASE_URL``, ``SUPABASE_SERVICE_ROLE_KEY``, ``SUPABASE_OWNER_ID`` and ``CAPITAL_WITNESS=supabase`` on the machine.
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Mapping, Optional, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BOT not in sys.path:
    sys.path.insert(0, BOT)
TOOLS = os.path.dirname(os.path.abspath(__file__))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import exec_evidence as XE  # noqa: E402
import ms_capital_anchor as AN  # noqa: E402

EVENT_KIND = "PROVENANCE"
STAGE = "CAPITAL_ANCHOR"
PROBE_STAGE = "CAPITAL_ANCHOR_PROBE"
PROBE_AT_MS = 1_700_000_000_000                 # a fixed past instant: the canary's content is identical in every process, so writing it twice is UNCHANGED, and it is never "ahead of the server"


def ref(hex_digest: str) -> str:
    """``h`` + 15 hex. A letter-led 16-character token can neither be 16 digits long (the guard's ``long_digit_run``; a bare 16-hex ref is all digits about once in 1,700) nor 24 hex characters long."""
    return "h" + str(hex_digest)[:15]


def book_ref(account: str) -> str:
    return "a" + str(account)[5:20]                # acct_ + 24 hex -> a + 15 hex


def event_id(account: str, seq: Any) -> str:
    return f"capital:{str(account)[5:29]}:{int(seq):09d}"


def probe_id(account: str) -> str:
    return f"capital:{str(account)[5:29]}:probe"


def _q4(x: Any) -> str:
    try:
        return format(Decimal(str(x)).quantize(Decimal("0.0001")), "f")
    except (InvalidOperation, ValueError):
        return "0.0000"


def payload_of(account: str, entry: Mapping[str, Any]) -> Dict[str, Any]:
    """What the remote holds of one lineage entry: the position (sequence, refs of this head / the previous head / the book digest) and the stop-relevant facts. Never a balance, a raw hash, a session id."""
    st = entry.get("state") or {}
    pend = st.get("pending")
    return {"stage": STAGE, "book_ref": book_ref(account), "seq": int(entry["seq"]), "entry": str(entry["kind"]), "prev_ref": ref(entry["prev"]), "head_ref": ref(entry["h"]), "state_ref": ref(entry["book_digest"]),
            "verified_result_usd": _q4(st.get("realised_usd", "0")), "loss_streak": int(st.get("loss_streak") or 0), "halted": bool(st.get("halted")), "review_open": bool(st.get("review_required")),
            "pending_open": bool(pend), "epoch": int(st.get("epoch") or 0)}


class SupabaseWitness:
    configured = True
    name = "SUPABASE_EVENTS"

    def __init__(self, client: Any, owner_id: str, *, required: bool = False) -> None:
        if not isinstance(owner_id, str) or not XE._UUID.match(owner_id):
            raise AN.AnchorRefused("WITNESS_MISCONFIGURED", "SUPABASE_OWNER_ID must be a lowercase uuid")
        self.client, self.owner_id, self.required = client, owner_id, bool(required)
        self._visible: Dict[str, bool] = {}

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, required: bool) -> "SupabaseWitness":
        url, key, owner = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_ROLE_KEY"), env.get("SUPABASE_OWNER_ID")
        if not (url and key and owner):
            raise AN.AnchorRefused("WITNESS_MISCONFIGURED", "CAPITAL_WITNESS=supabase needs SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY and SUPABASE_OWNER_ID")
        try:
            client = XE.RestClient(url, key, retries=1, timeout=10.0)
        except XE.ClientRefused as exc:
            raise AN.AnchorRefused("WITNESS_MISCONFIGURED", f"the Supabase client was refused: {str(exc)[:80]}") from None
        return cls(client, owner, required=required)

    # ---- the ref function the Anchor compares with (it never sends a raw hash)
    @staticmethod
    def ref_of(h: str) -> str:
        return ref(h)

    # ---- low-level
    def _put(self, ev: Mapping[str, Any]) -> str:
        row = {"owner_id": self.owner_id, "event_id": ev["event_id"], "kind": ev["kind"], "session_id": ev["session_id"], "seq": ev["seq"], "payload": ev["payload"],
               "occurred_at": XE._iso(ev["occurred_at_ms"]), "source": XE.SOURCE, "schema_version": XE.SCHEMA_VERSION, "content_sha256": ev["content_sha256"]}
        try:
            verdict = self.client.rpc("exec_event_put", {"p_row": row})
        except XE.RestError as exc:
            raise AN.WitnessError(f"the witness write failed ({exc.kind}{'' if not exc.status else ' http=' + str(exc.status)})") from None
        if verdict not in (XE.INSERTED, XE.UNCHANGED, XE.CONFLICT):
            raise AN.WitnessError("the witness answered something that is not a verdict")
        return str(verdict)

    def _rows(self, query: Mapping[str, str]) -> list:
        try:
            rows = self.client.select("exec_events", query)
        except XE.RestError as exc:
            raise AN.WitnessError(f"the witness read failed ({exc.kind}{'' if not exc.status else ' http=' + str(exc.status)})") from None
        if not isinstance(rows, list):
            raise AN.WitnessError("the witness read answered something that is not a list")
        return rows

    def _prove_visible(self, account: str) -> None:
        """An empty list may mean 'no rows' or 'a key that cannot see the rows'. Write an idempotent canary and read it back by its id; only then is an empty history believed."""
        if self._visible.get(account):
            return
        pid = probe_id(account)
        q = {"select": "event_id", "owner_id": f"eq.{self.owner_id}", "event_id": f"eq.{pid}", "limit": "1"}
        if not self._rows(q):
            try:
                ev = XE.make_event(EVENT_KIND, {"stage": PROBE_STAGE, "book_ref": book_ref(account)}, occurred_at_ms=PROBE_AT_MS, event_id=pid, seq=0)
            except XE.ExecEvidenceError as exc:
                raise AN.WitnessError(f"the visibility canary was refused by the evidence guard ({type(exc).__name__})") from None
            if self._put(ev) == XE.CONFLICT:
                raise AN.WitnessError("the visibility canary conflicts with another row")
            if not self._rows(q):
                raise AN.WitnessError("VISIBILITY_UNPROVEN: a row just written cannot be read back (is schema carry exposed to this key?)")
        self._visible[account] = True

    # ---- the Witness protocol
    def head(self, account: str) -> Optional[Tuple[int, str]]:
        """(seq, head_ref) of the newest head the witness holds for this account, or None when it holds none (and the key was proven able to see rows)."""
        self._prove_visible(account)
        rows = self._rows({"select": "seq,payload", "owner_id": f"eq.{self.owner_id}", "kind": f"eq.{EVENT_KIND}", "payload->>stage": f"eq.{STAGE}", "payload->>book_ref": f"eq.{book_ref(account)}",
                           "order": "seq.desc", "limit": "1"})
        if not rows:
            return None
        r = rows[0]
        p = r.get("payload") if isinstance(r, dict) else None
        seq = r.get("seq") if isinstance(r, dict) else None
        if (not isinstance(p, dict) or isinstance(seq, bool) or not isinstance(seq, int) or seq < 0 or p.get("seq") != seq or p.get("book_ref") != book_ref(account)
                or not isinstance(p.get("head_ref"), str) or len(p["head_ref"]) != 16 or p["head_ref"][0] != "h"):
            raise AN.WitnessError("the witness row has an unexpected shape")
        return seq, p["head_ref"]

    def publish(self, account: str, entry: Mapping[str, Any]) -> None:
        try:
            ev = XE.make_event(EVENT_KIND, payload_of(account, entry), occurred_at_ms=int(entry["ts_ms"]), event_id=event_id(account, entry["seq"]), seq=int(entry["seq"]))
        except XE.ExecEvidenceError as exc:
            raise AN.WitnessError(f"the head was refused by the evidence guard ({type(exc).__name__})") from None
        if self._put(ev) == XE.CONFLICT:
            raise AN.WitnessConflict(f"the witness holds a different head at seq {entry['seq']}")
