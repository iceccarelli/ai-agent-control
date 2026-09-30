#!/usr/bin/env python3
"""Raw venue evidence: a redacted, hash-chained, environment-honest capture
of what a venue adapter actually sent and got back.

WHY THIS MODULE EXISTS
=======================
`bybit_connection.py` talks to Bybit. `tools/linear_stop_venue_drill.py` runs
a scripted sequence against it and prints a transcript. Neither one produces
a durable, tamper-evident artefact of the raw exchange: nothing today
redacts a captured request/response before it is written anywhere, nothing
independently checks that a record's claimed environment (mainnet / testnet
/ demo / simulation) actually matches the host the request was sent to, and
nothing links one capture to the next so a record cannot be quietly
inserted, deleted, or reordered after the fact.

This module is that layer, and only that layer. It does not talk to a
venue, does not know about orders/positions/reconciliation, and does not
duplicate `persistence.StateStore`'s order-lifecycle tables or
`promote_forward_shadow.py`'s promotion-gate machinery. It is a pure
capture/redaction/hash-chain primitive that any adapter or drill can build
an `EvidenceRecord` from and append to a durable log.

THE LAW THIS ENFORCES
======================
"A mainnet record cannot be generated from a testnet transcript. A testnet
drill cannot become 'live execution proof.' A simulation cannot become
'venue evidence.'" `validate_environment_claim` is what makes that a
checked fact rather than a naming convention: a declared environment that
the request URL itself contradicts is refused before a record is ever
built.

WIRED INTO THE REAL ADAPTER, NOT AN UNUSED HELPER
====================================================
`EvidenceCapturingTransport` below implements the exact `Transport`
interface `bybit_connection.BybitClient` calls (`request(method, url,
headers=, params=, body=, timeout=)` -> `(status_code, text)`), so wrapping
whatever transport a `BybitClient` is given makes EVERY real request that
client sends — `place_order`, `place_stop_order`, `cancel_order`,
`get_position`, all of it — automatically produce a redacted,
environment-checked, hash-chained evidence record, from the one seam every
call already passes through, without touching `bybit_connection.py` or
duplicating capture logic at each call site. This module never imports
`bybit_connection` (see below), so the wrapper is duck-typed against that
interface rather than inheriting from it.

NO REAL VENUE EVIDENCE SHIPS WITH THIS MODULE
================================================
This repository has no execution host with live Bybit connectivity in this
session (confirmed: no BYBIT_API_KEY/BYBIT_API_SECRET are set, and the
container's network policy refuses api-testnet.bybit.com outright). Nothing
here fabricates a captured transcript to fill that gap — every example and
every test constructs its own synthetic request/response and marks it
accordingly. Real testnet/demo evidence, the moment an execution host and
credentials exist, is captured automatically by wrapping that
`BybitClient`'s transport in `EvidenceCapturingTransport` — never invented
after the fact.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)

import provenance as prov  # noqa: E402

logger = logging.getLogger("venue_evidence")

SCHEMA = "venue_evidence/1"

# ---------------------------------------------------------------------------
# environment — one vocabulary, reused from docs/human/VENUE_0046.md /
# bybit_connection.VENUE_REST, never invented a second time
# ---------------------------------------------------------------------------

#: `simulation` (no network call at all — a backtest/offline replay) and
#: `unknown` (the default, deliberately, because evidence that does not know
#: where it ran must not be readable as the strongest possible claim — see
#: VENUE_0046.md) are the only two values not already in
#: `bybit_connection.VENUE_REST`.
ENVIRONMENTS = frozenset({"mainnet", "testnet", "demo", "simulation", "unknown"})

#: Environments a real network call could actually have reached.
#: `simulation` and `unknown` never did, by construction.
NETWORKED_ENVIRONMENTS = frozenset({"mainnet", "testnet", "demo"})

#: Inlined from `bybit_connection.VENUE_REST` rather than imported: this
#: module must recognise these hosts even when redacting evidence produced
#: by a client this process never constructed (e.g. from a saved
#: transcript), so it cannot depend on constructing a live `BybitClient` —
#: see the module docstring on why this stays dependency-free.
VENUE_HOSTS = {
    "mainnet": "api.bybit.com",
    "testnet": "api-testnet.bybit.com",
    "demo": "api-demo.bybit.com",
}

#: Inlined from `bybit_connection.MAINNET_WS_PRIVATE`/`TESTNET_WS_PRIVATE`
#: for the same reason `VENUE_HOSTS` is inlined above. There is no demo
#: private-WS endpoint in bybit_connection.py today, so there is none
#: here either — inventing one would be exactly the kind of unverified
#: claim this module exists to refuse.
WS_VENUE_HOSTS = {
    "mainnet": "stream.bybit.com",
    "testnet": "stream-testnet.bybit.com",
}

GENESIS_PREV_HASH = "0" * 64

#: Field names (case-insensitive, wherever nested) that hold a secret or a
#: credential and must never reach a persisted evidence record.
_SENSITIVE_KEYS = frozenset({
    "api_key", "apikey", "api_secret", "apisecret", "secret",
    "x-bapi-api-key", "x-bapi-sign", "sign", "signature", "authorization",
    "token", "access_token", "refresh_token", "password",
})
_REDACTED = "***REDACTED***"


class EvidenceRefused(RuntimeError):
    """The record cannot be trusted as evidence. Nothing was written."""


def infer_environment_from_url(url: str) -> str:
    """Best-effort environment name from a request URL's host — REST or
    private-WS, checked against the same two independent host tables
    (`VENUE_HOSTS`, `WS_VENUE_HOSTS`); a URL is only ever one or the
    other, so checking both is unambiguous.

    Returns "unknown" rather than guessing when the host does not match any
    known venue — never defaults to a networked environment.
    """
    text = str(url or "")
    for env, host in VENUE_HOSTS.items():
        if host in text:
            return env
    for env, host in WS_VENUE_HOSTS.items():
        if host in text:
            return env
    return "unknown"


def validate_environment_claim(*, declared_environment: str,
                               request_url: str = "") -> None:
    """Refuse a declared environment the request URL itself contradicts.

    This is the concrete check the module exists for: a testnet transcript
    cannot become mainnet evidence, and a simulation cannot become venue
    evidence, no matter what a caller claims. Raises `EvidenceRefused` on
    any mismatch. A declared environment with no URL to check it against is
    only accepted when it does not claim a real network call in the first
    place (`simulation`/`unknown`) — a networked claim always needs a URL.
    """
    if declared_environment not in ENVIRONMENTS:
        raise EvidenceRefused(
            f"declared_environment={declared_environment!r} is not one of "
            f"{sorted(ENVIRONMENTS)}")

    if not request_url:
        if declared_environment in NETWORKED_ENVIRONMENTS:
            raise EvidenceRefused(
                f"declared_environment={declared_environment!r} claims a "
                "real venue call but no request_url was captured to verify "
                "it against")
        return

    actual = infer_environment_from_url(request_url)
    if declared_environment in NETWORKED_ENVIRONMENTS and actual != declared_environment:
        raise EvidenceRefused(
            f"declared_environment={declared_environment!r} does not match "
            f"the request URL's actual host ({request_url!r} resolves to "
            f"{actual!r}); a transcript cannot claim a stronger or "
            f"different venue than the one it actually reached")
    if declared_environment in ("simulation", "unknown") and actual in NETWORKED_ENVIRONMENTS:
        raise EvidenceRefused(
            f"declared_environment={declared_environment!r} but the request "
            f"URL ({request_url!r}) actually reached the real {actual!r} "
            "venue — a real network call cannot be filed as simulation or "
            "unknown evidence")


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------


def redact(value: Any) -> Any:
    """Recursively replace every value under a sensitive key with a fixed
    marker, at any depth inside a dict/list structure.

    Never mutates `value` — returns a new structure, so a caller's original
    raw payload (which may still be needed, e.g. to actually sign a
    request) is untouched.
    """
    if isinstance(value, dict):
        return {
            k: _REDACTED if str(k).lower() in _SENSITIVE_KEYS else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------


def content_hash(doc: Dict[str, Any]) -> str:
    """Canonical sha256 over `doc`'s logical content — same convention as
    `promote_forward_shadow.content_hash`: `sort_keys=True` makes the hash
    independent of key order."""
    canonical = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EvidenceRecord:
    """One raw venue evidence record: what was sent, what came back, where
    it ran, and a hash chain linking it to the record before it."""

    venue: str
    environment: str
    captured_at_utc: str
    method: str
    request_url: str
    request: Dict[str, Any]
    response: Dict[str, Any]
    order_link_id: str
    git_commit: str
    prev_hash: str
    schema: str = SCHEMA
    #: "rest" (default — every pre-existing caller of `build_record` is a
    #: REST call, unchanged) or "ws" (a private-WebSocket event; see
    #: `build_ws_event_record`). Makes the two sources distinguishable in
    #: a chain without inventing a second evidence format.
    transport: str = "rest"
    #: The deterministic, venue-derived event identity (see
    #: `private_ws_consumer.py`'s canonical-identity rules) for a WS
    #: record. Empty for a REST record, which is already identified by
    #: `order_link_id`/`request_url`/`captured_at_utc`.
    venue_event_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        body = {
            "schema": self.schema,
            "venue": self.venue,
            "environment": self.environment,
            "captured_at_utc": self.captured_at_utc,
            "method": self.method,
            "request_url": self.request_url,
            "request": self.request,
            "response": self.response,
            "order_link_id": self.order_link_id,
            "git_commit": self.git_commit,
            "prev_hash": self.prev_hash,
            "transport": self.transport,
            "venue_event_id": self.venue_event_id,
        }
        body["record_hash"] = content_hash(body)
        return body


def build_record(*, venue: str, environment: str, method: str,
                 request_url: str, request: Dict[str, Any],
                 response: Dict[str, Any], order_link_id: str = "",
                 prev_hash: str = GENESIS_PREV_HASH,
                 captured_at_utc: Optional[str] = None,
                 repo: Optional[str] = None,
                 transport: str = "rest",
                 venue_event_id: str = "") -> Dict[str, Any]:
    """Build one evidence record as a plain dict, ready for
    `append_evidence`.

    Redacts `request`/`response`, validates the environment claim against
    `request_url`, and stamps git provenance. A pure builder with no file
    I/O, so it can be unit tested and reused by any venue adapter without a
    write side effect forced on it.
    """
    validate_environment_claim(declared_environment=environment,
                               request_url=request_url)

    record = EvidenceRecord(
        venue=venue,
        environment=environment,
        captured_at_utc=captured_at_utc or dt.datetime.now(
            dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        method=method,
        request_url=request_url,
        request=redact(request),
        response=redact(response),
        order_link_id=order_link_id,
        git_commit=prov.git_commit(repo),
        prev_hash=prev_hash,
        transport=transport,
        venue_event_id=venue_event_id,
    )
    return record.to_dict()


def build_ws_event_record(*, venue: str, environment: str, ws_url: str,
                          venue_event_id: str, topic: str,
                          order_link_id: str = "",
                          payload: Optional[Dict[str, Any]] = None,
                          duplicate: bool = False,
                          prev_hash: str = GENESIS_PREV_HASH,
                          captured_at_utc: Optional[str] = None,
                          observed_at_utc: Optional[str] = None,
                          repo: Optional[str] = None) -> Dict[str, Any]:
    """Build one evidence record for a private-WS event.

    Same guarantees as `build_record` (redaction, environment validation
    against `ws_url` via `WS_VENUE_HOSTS`, git provenance, hash chaining)
    — this is the same mechanism, not a parallel one, with a shape suited
    to a server-pushed event rather than a client request/response pair:
    `request` records what this connection subscribed to (`topic`),
    `response` is the redacted raw payload actually observed, and
    `venue_event_id`/`transport="ws"` carry the deterministic identity
    and source so a reader can tell a WS record from a REST one without
    guessing from its shape.

    `captured_at_utc` is when this record was durably WRITTEN (normally
    the writer thread's own time, at drain/apply); `observed_at_utc`, when
    given, is when the event was first OBSERVED (the WS thread's own
    time, at enqueue) and is carried in `request` alongside `topic`/
    `duplicate` — preserving the observed-vs-applied distinction without
    a new top-level schema field (see private_ws_consumer.py's FIX 1).
    """
    request: Dict[str, Any] = {"topic": topic, "duplicate": duplicate}
    if observed_at_utc is not None:
        request["observed_at_utc"] = observed_at_utc
    return build_record(
        venue=venue, environment=environment, method="WS_EVENT",
        request_url=ws_url, request=request,
        response=payload or {}, order_link_id=order_link_id,
        prev_hash=prev_hash, captured_at_utc=captured_at_utc, repo=repo,
        transport="ws", venue_event_id=venue_event_id)


def build_record_from_order_result(order_result: Any, *, venue: str,
                                   environment: str, request_url: str,
                                   request: Dict[str, Any],
                                   prev_hash: str = GENESIS_PREV_HASH,
                                   captured_at_utc: Optional[str] = None,
                                   repo: Optional[str] = None
                                   ) -> Dict[str, Any]:
    """Build an evidence record from a `bybit_connection.OrderResult` (or
    anything duck-typed the same way: `ok`/`order_id`/`order_link_id`/
    `reason`/`ret_code`/`raw`), without importing `bybit_connection` —
    this module stays dependency-free (see module docstring) so it can be
    unit tested and reused without pulling in config/persistence/
    market_data.
    """
    response = {
        "ok": bool(getattr(order_result, "ok", False)),
        "order_id": str(getattr(order_result, "order_id", "")),
        "order_link_id": str(getattr(order_result, "order_link_id", "")),
        "reason": str(getattr(order_result, "reason", "")),
        "ret_code": getattr(order_result, "ret_code", 0),
        "raw": dict(getattr(order_result, "raw", {}) or {}),
    }
    return build_record(
        venue=venue, environment=environment, method="POST",
        request_url=request_url, request=request, response=response,
        order_link_id=response["order_link_id"], prev_hash=prev_hash,
        captured_at_utc=captured_at_utc, repo=repo)


# ---------------------------------------------------------------------------
# append-only, hash-chained log
# ---------------------------------------------------------------------------


def last_record_hash(path: str) -> str:
    """The `record_hash` of the last line in an evidence JSONL file, or the
    genesis hash if the file does not exist or is empty.

    Reads the file's actual last line rather than a separately tracked
    pointer — there is nowhere else the "current tip" fact could silently
    drift from what the file actually contains.
    """
    if not os.path.exists(path):
        return GENESIS_PREV_HASH
    with open(path, encoding="utf-8") as handle:
        lines = [ln for ln in handle if ln.strip()]
    if not lines:
        return GENESIS_PREV_HASH
    last = json.loads(lines[-1])
    return str(last.get("record_hash", GENESIS_PREV_HASH))


def append_evidence(path: str, record: Dict[str, Any]) -> None:
    """Append one evidence record to `path`, refusing if it does not chain
    from the file's actual current tip.

    Append-only: never rewrites or truncates an existing line. Fsyncs
    before returning, matching `promote_forward_shadow.py`'s durable-write
    convention.
    """
    expected_prev = last_record_hash(path)
    if record.get("prev_hash") != expected_prev:
        raise EvidenceRefused(
            f"record's prev_hash ({record.get('prev_hash')!r}) does not "
            f"match {path!r}'s actual current tip ({expected_prev!r}); "
            "build the record with prev_hash=last_record_hash(path) "
            "immediately before appending")
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def verify_chain(path: str) -> List[str]:
    """Replay every record in an evidence file and confirm: (1) each
    record's own `record_hash` matches its actual content, and (2) each
    record's `prev_hash` matches the previous record's `record_hash`.

    This is what makes the file's own claim of order and completeness
    checkable rather than merely asserted. Returns human-readable reasons;
    empty means the chain verifies clean (including a missing/empty file —
    a chain that has not started yet is not a broken one).
    """
    reasons: List[str] = []
    if not os.path.exists(path):
        return reasons
    prev_hash = GENESIS_PREV_HASH
    with open(path, encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            declared_hash = record.get("record_hash", "")
            body = {k: v for k, v in record.items() if k != "record_hash"}
            actual_hash = content_hash(body)
            if declared_hash != actual_hash:
                reasons.append(
                    f"line {lineno}: record_hash does not match its own "
                    "content (tampered or hand-edited record)")
            if record.get("prev_hash") != prev_hash:
                reasons.append(
                    f"line {lineno}: prev_hash "
                    f"{record.get('prev_hash')!r} does not chain from the "
                    f"prior record's hash {prev_hash!r} (a record was "
                    "inserted, deleted, or reordered)")
            prev_hash = declared_hash
    return reasons


# ---------------------------------------------------------------------------
# the real adapter integration point
# ---------------------------------------------------------------------------


class EvidenceCapturingTransport:
    """Wraps a `bybit_connection.Transport`-shaped object so every real
    request that passes through it also produces an evidence record.

    Duck-typed rather than subclassed: this module never imports
    `bybit_connection` (see module docstring — no coupling to
    config/persistence/market_data), and `bybit_connection.BybitClient`
    never does an `isinstance` check on its transport, only calls
    `.request(...)` — confirmed by reading bybit_connection.py before
    writing this class.

    Evidence capture is deliberately best-effort: a capture bug must
    degrade observability, never execution. Any exception while building
    or appending the record is logged at `error` level (loud — this is not
    swallowed silently) and the real request's result is returned exactly
    as the wrapped transport produced it, unaffected.
    """

    def __init__(self, inner: Any, *, evidence_path: str, venue: str,
                environment: str) -> None:
        self._inner = inner
        self._evidence_path = evidence_path
        self._venue = venue
        self._environment = environment

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Optional[Mapping[str, Any]] = None,
        body: Optional[str] = None,
        timeout: float = 10.0,
    ) -> Tuple[int, str]:
        status, text = self._inner.request(
            method, url, headers=headers, params=params, body=body,
            timeout=timeout)
        try:
            self._capture(method=method, url=url, headers=headers,
                          params=params, body=body, status=status, text=text)
        except Exception:  # noqa: BLE001
            logger.error(
                "venue evidence capture failed for %s %s (trading is "
                "unaffected; this call's real result is still returned)",
                method, url, exc_info=True)
        return status, text

    def _capture(self, *, method: str, url: str, headers: Mapping[str, str],
                params: Optional[Mapping[str, Any]], body: Optional[str],
                status: int, text: str) -> None:
        request_payload: Dict[str, Any] = {
            "headers": dict(headers),
            "params": dict(params or {}),
            "body": body,
        }
        try:
            response_payload = json.loads(text) if text else {}
            if not isinstance(response_payload, dict):
                response_payload = {"_body": response_payload}
        except ValueError:
            response_payload = {"_raw_text": text}
        response_payload["_status_code"] = status

        prev_hash = last_record_hash(self._evidence_path)
        record = build_record(
            venue=self._venue, environment=self._environment, method=method,
            request_url=url, request=request_payload,
            response=response_payload, prev_hash=prev_hash)
        append_evidence(self._evidence_path, record)
