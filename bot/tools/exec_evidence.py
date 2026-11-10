"""Durable, idempotent, append-only EXECUTION EVIDENCE path from a worker to Supabase, plus a global executor lease.

Supabase is an evidence / telemetry layer here. It is NEVER the trading authority and never a command channel: the
venue's own reads and the owner accounting stay the financial truth. Nothing in this module decides to place, size or
close an order; the lease only answers "may this process keep executing", and it FAILS CLOSED.

Pure standard library. This module reads NO environment variable: the caller injects the project URL, the
service-role key and the owner id. The key is held in an opaque wrapper, sent only as a request header, scrubbed from
every error text, and never appears in a repr, log line, exception, event or row.

Parts
  Outbox         durable local JSONL outbox (segments + cursor, atomic cursor writes, hard capacity with an explicit
                 OVERFLOW incident instead of a silent drop, immutable event_id + content_sha256, monotonic seq,
                 resume after restart, duplicate suppression, single-process lock).
  RestClient     PostgREST client for the service role: HTTPS to <project>.supabase.co only, no redirects, timeouts,
                 bounded retries with backoff.
  EvidenceSync   drain(): outbox -> carry.exec_event_put, per-event INSERTED / UNCHANGED / CONFLICT / FAILED. Never
                 overwrites, never drops on failure, resumable. A CONFLICT (or a deterministically rejected event) is
                 reported and quarantined on disk, never retried silently.
  LeaseClient    acquire / renew / release with a fencing token; any network, HTTP or parse error means NOT held;
                 assert_held() for the execution loop before every order-capable step.
  build_heartbeat the shared heartbeat dict.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple

try:  # the repository's own scanner and canonical hash (importable without side effects)
    import supabase_sync as _ss
except ImportError:  # pragma: no cover - run from a different working directory
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import supabase_sync as _ss

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX
    _fcntl = None

KINDS = ("SESSION_STATE", "DECISION", "OPPORTUNITY_EVAL", "PLAN", "RISK_ASSESSMENT", "ORDER", "FILL", "FUNDING", "EXIT",
         "RECONCILIATION", "COST", "DAILY_PNL", "INCIDENT", "PROVENANCE")
ENGINE_STATES = ("INITIALISING", "DATA_CHECK", "RESEARCHING", "WAITING_FOR_OPPORTUNITY", "PLAN_PROPOSED", "RISK_APPROVED",
                 "EXECUTING", "HEDGED", "EXITING", "RECONCILING", "FLAT", "PAUSED", "ALERT", "STOPPED", "RECOVERING",
                 "UNRECONCILED", "DRAINING")
MODES = ("PAPER", "TESTNET")
INSERTED, UNCHANGED, CONFLICT, FAILED = "INSERTED", "UNCHANGED", "CONFLICT", "FAILED"
SOURCE = "tools/exec_evidence.py"
SCHEMA_VERSION = 1

_EVENT_ID = re.compile(r"^[A-Za-z0-9._:=-]{8,200}$")
_NAME = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
_SB_HOST = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\.supabase\.co$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_PGCODE = re.compile(r"^([0-9A-Z]{5}|PGRST\d{3})$")


class ExecEvidenceError(Exception):
    """Base class. Messages never contain the service-role key or a payload value."""


class SecretRefused(ExecEvidenceError):
    def __init__(self, findings):
        self.findings = tuple(findings)
        super().__init__("secret guard refused the payload (value never echoed): " + "; ".join(self.findings[:10]))


class EventRefused(ExecEvidenceError):
    """The event itself is malformed."""


class EventConflict(ExecEvidenceError):
    """The same event_id was appended again with DIFFERENT content: refused, never overwritten."""


class OutboxOverflow(ExecEvidenceError):
    """The outbox is at its hard capacity. The event was NOT stored; an OVERFLOW incident was recorded. The caller must
    treat this as 'evidence cannot be retained': stop opening risk until the outbox drains."""

    def __init__(self, dropped: int):
        self.dropped = dropped
        super().__init__(f"outbox at capacity: event not stored (dropped so far: {dropped}); OVERFLOW incident recorded")


class OutboxCorrupt(ExecEvidenceError):
    pass


class OutboxLocked(ExecEvidenceError):
    pass


class ClientRefused(ExecEvidenceError):
    pass


class RestError(ExecEvidenceError):
    def __init__(self, kind: str, status: Optional[int] = None, code: Optional[str] = None, retryable: bool = True):
        self.kind, self.status, self.code, self.retryable = kind, status, code, retryable
        super().__init__(f"supabase request failed: {kind}" + (f" http={status}" if status else "") + (f" code={code}" if code else ""))


class LeaseNotHeld(ExecEvidenceError):
    pass


# --------------------------------------------------------------------------- small helpers

def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(ms: int) -> str:
    return _dt.datetime.fromtimestamp(ms / 1000.0, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)


def _atomic_write(path: str, data: bytes) -> None:
    """temp file in the same directory + fsync + rename + directory fsync."""
    d = os.path.dirname(path)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(d)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def guard_payload(obj: Any, what: str = "event") -> None:
    """The repository's secret scanner (tools/supabase_sync.scan_value) over every key and value, recursively. Raises
    SecretRefused with paths and rule names only."""
    findings = [f"{p}:{r}" for p, r in _ss.scan_value(obj, what)]
    if findings:
        raise SecretRefused(findings)


def make_event(kind: str, payload: Mapping[str, Any], *, session_id: Optional[str] = None, occurred_at_ms: Optional[int] = None,
               event_id: Optional[str] = None, seq: int = 0) -> Dict[str, Any]:
    """Build the shared event dict. content_sha256 covers kind, session_id, payload and occurred_at_ms (NOT seq, which the
    outbox assigns), so a logical event re-built after a restart has the same hash and the same event_id."""
    if kind not in KINDS:
        raise EventRefused(f"unknown kind {kind!r}")
    if not isinstance(payload, Mapping):
        raise EventRefused("payload must be a mapping")
    if session_id is not None and (not isinstance(session_id, str) or not _NAME.match(session_id)):
        raise EventRefused("bad session_id")
    ts = _now_ms() if occurred_at_ms is None else occurred_at_ms
    if isinstance(ts, bool) or not isinstance(ts, int) or ts <= 0:
        raise EventRefused("occurred_at_ms must be a positive int")
    try:
        norm = _ss.normalise(dict(payload))
    except _ss.SyncError as exc:
        raise EventRefused(f"payload not representable: {exc}") from None
    guard_payload({"kind": kind, "session_id": session_id, "payload": norm})
    sha = _ss.content_sha256({"kind": kind, "session_id": session_id, "payload": norm, "occurred_at_ms": ts})
    eid = event_id if event_id is not None else f"{kind.lower()}:{sha[:40]}"
    if not isinstance(eid, str) or not _EVENT_ID.match(eid):
        raise EventRefused("bad event_id")
    return {"event_id": eid, "kind": kind, "session_id": session_id, "seq": seq, "payload": norm,
            "content_sha256": sha, "occurred_at_ms": ts}


# --------------------------------------------------------------------------- outbox

class Outbox:
    """Durable local outbox on the volume.

    Files in ``directory``: seg-NNNNNNNN.jsonl (one canonical JSON event per line, fsynced), cursor.json (what has been
    safely handed to Supabase; written by temp+fsync+rename), overflow.json (explicit loss marker), quarantine.jsonl
    (conflicting / rejected events, kept for the owner), outbox.lock.
    A torn trailing line (crash mid-append) is truncated on open; any other unreadable line raises OutboxCorrupt: nothing is
    skipped silently.
    """

    def __init__(self, directory: str, *, max_bytes: int = 64 * 1024 * 1024, max_events: int = 200_000,
                 segment_bytes: int = 1024 * 1024, reserve_bytes: int = 256 * 1024, clock: Callable[[], int] = _now_ms):
        if max_bytes < 4096 or segment_bytes < 512 or reserve_bytes < 512 or max_events < 1:
            raise ValueError("capacity settings too small")
        self.dir = os.path.abspath(directory)
        self.max_bytes, self.max_events, self.segment_bytes, self.reserve_bytes = max_bytes, max_events, segment_bytes, reserve_bytes
        self._clock = clock
        self._lock = threading.RLock()
        os.makedirs(self.dir, exist_ok=True)
        self._lockfd = None
        self._take_process_lock()
        self._ids: "OrderedDict[str, str]" = OrderedDict()   # event_id -> content_sha256 (pending + recently drained)
        self._seq = 0
        self._pending = 0
        self._cursor = {"seg": 1, "off": 0, "seq": 0}
        self._active = 1
        self._load()

    # -- lifecycle
    def _take_process_lock(self) -> None:
        if _fcntl is None:  # pragma: no cover
            return
        fd = os.open(os.path.join(self.dir, "outbox.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise OutboxLocked("another process holds this outbox") from None
        self._lockfd = fd

    def close(self) -> None:
        with self._lock:
            if self._lockfd is not None:
                try:
                    os.close(self._lockfd)  # releases the flock
                finally:
                    self._lockfd = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def __del__(self):  # pragma: no cover
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    # -- paths
    def _seg(self, n: int) -> str:
        return os.path.join(self.dir, f"seg-{n:08d}.jsonl")

    def _segments(self) -> List[int]:
        out = []
        for name in os.listdir(self.dir):
            m = re.fullmatch(r"seg-(\d{8})\.jsonl", name)
            if m:
                out.append(int(m.group(1)))
        return sorted(out)

    def _size(self, n: int) -> int:
        try:
            return os.path.getsize(self._seg(n))
        except FileNotFoundError:
            return 0

    # -- load / repair
    def _load(self) -> None:
        cpath = os.path.join(self.dir, "cursor.json")
        if os.path.exists(cpath):
            try:
                with open(cpath, "rb") as f:
                    c = json.loads(f.read())
                self._cursor = {"seg": int(c["seg"]), "off": int(c["off"]), "seq": int(c["seq"])}
            except (ValueError, KeyError, TypeError):
                raise OutboxCorrupt("cursor.json unreadable") from None
        segs = self._segments()
        self._seq = self._cursor["seq"]
        self._active = segs[-1] if segs else max(1, self._cursor["seg"])
        self._pending = 0
        for n in segs:
            last = n == segs[-1]
            path = self._seg(n)
            good = 0
            with open(path, "rb") as f:
                data = f.read()
            pos = 0
            while pos < len(data):
                nl = data.find(b"\n", pos)
                if nl < 0:
                    if not last:
                        raise OutboxCorrupt(f"segment {n} has an unterminated line")
                    break  # torn tail of the active segment: repaired below
                try:
                    ev = json.loads(data[pos:nl])
                    eid, sha, seq = ev["event_id"], ev["content_sha256"], int(ev["seq"])
                except (ValueError, KeyError, TypeError):
                    raise OutboxCorrupt(f"segment {n} has an unreadable line") from None
                self._ids[eid] = sha
                self._seq = max(self._seq, seq)
                pos = nl + 1
                good = pos
            if good < len(data):
                with open(path, "r+b") as f:
                    f.truncate(good)
                    f.flush()
                    os.fsync(f.fileno())
        # pending = events strictly after the cursor
        self._pending = sum(1 for _ in self.iter_pending())
        self._trim_ids()

    def _trim_ids(self, keep: int = 100_000) -> None:
        while len(self._ids) > keep:
            self._ids.popitem(last=False)

    # -- state
    @property
    def pending(self) -> int:
        with self._lock:
            return self._pending

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def total_bytes(self) -> int:
        return sum(self._size(n) for n in self._segments())

    @property
    def overflow_dropped(self) -> int:
        try:
            with open(os.path.join(self.dir, "overflow.json"), "rb") as f:
                return int(json.loads(f.read())["dropped"])
        except (OSError, ValueError, KeyError, TypeError):
            return 0

    def quarantined(self) -> List[Dict[str, Any]]:
        out = []
        try:
            with open(os.path.join(self.dir, "quarantine.jsonl"), "rb") as f:
                for line in f:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except FileNotFoundError:
            pass
        return out

    # -- append
    def append(self, kind: str, payload: Mapping[str, Any], *, session_id: Optional[str] = None,
               occurred_at_ms: Optional[int] = None, event_id: Optional[str] = None) -> Dict[str, Any]:
        """Store one event durably (fsynced before return) and return the event dict. An identical re-append returns the
        stored event (duplicate suppressed). Same event_id with different content raises EventConflict. At capacity raises
        OutboxOverflow after recording an OVERFLOW incident."""
        ts = self._clock() if occurred_at_ms is None else occurred_at_ms
        ev = make_event(kind, payload, session_id=session_id, occurred_at_ms=ts, event_id=event_id)
        with self._lock:
            known = self._ids.get(ev["event_id"])
            if known is not None:
                if known == ev["content_sha256"]:
                    return self._find(ev["event_id"]) or ev
                self._quarantine({"reason": "LOCAL_CONFLICT", "event": ev, "stored_sha256": known})
                raise EventConflict(f"event_id {ev['event_id']} already stored with different content")
            self._recover_overflow_marker()
            return self._store(ev, reserve=(kind == "INCIDENT"))

    def _find(self, event_id: str) -> Optional[Dict[str, Any]]:
        for _s, _o, ev in self._iter_all():
            if ev["event_id"] == event_id:
                return ev
        return None

    def _store(self, ev: Dict[str, Any], *, reserve: bool) -> Dict[str, Any]:
        ev = dict(ev)
        ev["seq"] = self._seq + 1
        line = (_dumps(ev) + "\n").encode()
        limit = self.max_bytes + (self.reserve_bytes if reserve else 0)
        if self.total_bytes() + len(line) > limit or (self._pending >= self.max_events and not reserve):
            self._overflow(ev)
            raise OutboxOverflow(self.overflow_dropped)
        if self._size(self._active) >= self.segment_bytes:
            self._active += 1
        path = self._seg(self._active)
        new_file = not os.path.exists(path)
        with open(path, "ab") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        if new_file:
            _fsync_dir(self.dir)
        self._seq = ev["seq"]
        self._pending += 1
        self._ids[ev["event_id"]] = ev["content_sha256"]
        self._trim_ids()
        return ev

    def _overflow(self, dropped_event: Mapping[str, Any]) -> None:
        path = os.path.join(self.dir, "overflow.json")
        state = {"dropped": 0, "first_ms": self._clock(), "incident": False}
        try:
            with open(path, "rb") as f:
                state.update(json.loads(f.read()))
        except (OSError, ValueError):
            pass
        state["dropped"] = int(state["dropped"]) + 1
        if not state["incident"]:
            inc = make_event("INCIDENT", {"incident": "OUTBOX_OVERFLOW", "state": "OPEN", "max_bytes": self.max_bytes,
                                          "max_events": self.max_events, "first_dropped_kind": dropped_event["kind"]},
                             occurred_at_ms=int(state["first_ms"]),
                             event_id="incident:outbox-overflow:" + hashlib.sha256(f"{self.dir}|{state['first_ms']}".encode()).hexdigest()[:24])
            if inc["event_id"] not in self._ids:
                try:
                    self._store(inc, reserve=True)
                    state["incident"] = True
                except OutboxOverflow:  # even the reserve is full: the marker file below is the record
                    pass
            else:
                state["incident"] = True
        _atomic_write(path, _dumps(state).encode())

    def _recover_overflow_marker(self) -> None:
        """Space is available again: close the overflow incident with the total dropped, then clear the marker."""
        path = os.path.join(self.dir, "overflow.json")
        if not os.path.exists(path):
            return
        if self.total_bytes() > self.max_bytes // 2 or self._pending >= self.max_events // 2:
            return
        try:
            with open(path, "rb") as f:
                state = json.loads(f.read())
        except (OSError, ValueError):
            return
        end = make_event("INCIDENT", {"incident": "OUTBOX_OVERFLOW", "state": "ENDED", "dropped_events": int(state["dropped"])},
                         occurred_at_ms=self._clock(),
                         event_id="incident:outbox-overflow-end:" + hashlib.sha256(f"{self.dir}|{state['first_ms']}|{state['dropped']}".encode()).hexdigest()[:24])
        if end["event_id"] not in self._ids:
            self._store(end, reserve=True)
        os.remove(path)
        _fsync_dir(self.dir)

    def _quarantine(self, record: Mapping[str, Any]) -> None:
        with open(os.path.join(self.dir, "quarantine.jsonl"), "ab") as f:
            f.write((_dumps(record) + "\n").encode())
            f.flush()
            os.fsync(f.fileno())

    # -- read side
    def _iter_all(self) -> Iterator[Tuple[int, int, Dict[str, Any]]]:
        for n in self._segments():
            with open(self._seg(n), "rb") as f:
                data = f.read()
            pos = 0
            while True:
                nl = data.find(b"\n", pos)
                if nl < 0:
                    break
                yield n, nl + 1, json.loads(data[pos:nl])
                pos = nl + 1

    def iter_pending(self) -> Iterator[Tuple[int, int, Dict[str, Any]]]:
        """(segment, end_offset, event) for every event after the cursor, in order."""
        cs, co = self._cursor["seg"], self._cursor["off"]
        for n, end, ev in self._iter_all():
            if (n, end) > (cs, co):
                yield n, end, ev

    def commit(self, seg: int, off: int) -> None:
        """Advance the cursor (durably) past an event that Supabase has stored or that was quarantined."""
        with self._lock:
            if (seg, off) <= (self._cursor["seg"], self._cursor["off"]):
                return
            segs = self._segments()
            if off >= self._size(seg):
                later = [n for n in segs if n > seg]
                if later:
                    seg, off = later[0], 0
            self._cursor = {"seg": seg, "off": off, "seq": self._seq}
            _atomic_write(os.path.join(self.dir, "cursor.json"), _dumps(self._cursor).encode())
            for n in segs:
                if n < seg:
                    os.remove(self._seg(n))
            self._pending = sum(1 for _ in self.iter_pending())

    def quarantine_item(self, ev: Mapping[str, Any], reason: str, detail: str = "") -> None:
        with self._lock:
            self._quarantine({"reason": reason, "detail": detail[:200], "event": dict(ev)})


# --------------------------------------------------------------------------- REST client

class _Secret:
    __slots__ = ("_v",)

    def __init__(self, v: str):
        self._v = v

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("secret is not serialisable")

    def __copy__(self):
        raise TypeError("secret is not copyable")

    def __deepcopy__(self, memo):
        raise TypeError("secret is not copyable")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise RestError("redirect_refused", code, retryable=False)


class RestClient:
    """PostgREST client for the service role. ``base_url`` must be https://<project>.supabase.co. The key is supplied by the
    caller and is never stored in a plain attribute, printed, logged or included in any exception text."""

    def __init__(self, base_url: str, service_key: str, *, timeout: float = 10.0, retries: int = 3, backoff: float = 0.5,
                 schema: str = "carry", allow_insecure_loopback_for_tests: bool = False,
                 sleep: Callable[[float], None] = time.sleep, max_response_bytes: int = 1_048_576):
        if not isinstance(service_key, str) or not service_key or re.search(r"\s", service_key) or len(service_key) > 4096:
            raise ClientRefused("service key must be a non-empty string without whitespace")
        u = urllib.parse.urlsplit(base_url if isinstance(base_url, str) else "")
        host = (u.hostname or "").lower()
        if u.username or u.password or u.query or u.fragment or u.path not in ("", "/"):
            raise ClientRefused("base_url must be a bare origin")
        if allow_insecure_loopback_for_tests:
            if host != "127.0.0.1" or u.scheme not in ("http", "https"):
                raise ClientRefused("insecure test mode is allowed only for 127.0.0.1")
            self._loopback = True
        else:
            if u.scheme != "https":
                raise ClientRefused("https is required")
            if not _SB_HOST.match(host) or u.port not in (None, 443):
                raise ClientRefused("host must be <project>.supabase.co")
            self._loopback = False
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,40}", schema):
            raise ClientRefused("bad schema name")
        self._origin = f"{u.scheme}://{host}" + (f":{u.port}" if u.port and not (u.scheme == "https" and u.port == 443) else "")
        self._host = host
        self._key = _Secret(service_key)
        self._timeout, self._retries, self._backoff, self._schema = float(timeout), max(0, int(retries)), float(backoff), schema
        self._sleep = sleep
        self._max = int(max_response_bytes)
        handlers: List[Any] = [_NoRedirect()]
        if self._loopback:
            handlers.append(urllib.request.ProxyHandler({}))
        self._opener = urllib.request.build_opener(*handlers)

    def __repr__(self) -> str:
        return f"RestClient(host={self._host!r}, key=<redacted>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("RestClient is not serialisable")

    def _scrub(self, text: str) -> str:
        k = self._key.reveal()
        return text.replace(k, "<redacted>") if k else text

    def rpc(self, function: str, args: Mapping[str, Any], *, retries: Optional[int] = None) -> Any:
        """POST /rest/v1/rpc/<function>; returns the parsed JSON. Raises RestError (retryable flag set) on failure."""
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,60}", function):
            raise ClientRefused("bad function name")
        body = json.dumps(args, separators=(",", ":"), allow_nan=False).encode()
        url = f"{self._origin}/rest/v1/rpc/{function}"
        tries = (self._retries if retries is None else max(0, retries)) + 1
        last: Optional[RestError] = None
        for i in range(tries):
            if i:
                self._sleep(min(self._backoff * (2 ** (i - 1)), 30.0))
            try:
                return self._once(url, body)
            except RestError as exc:
                last = exc
                if not exc.retryable:
                    raise
        assert last is not None
        raise last

    def _once(self, url: str, body: bytes) -> Any:
        key = self._key.reveal()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json",
            "Accept": "application/json", "Content-Profile": self._schema, "Accept-Profile": self._schema})
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                raw = resp.read(self._max + 1)
                status = resp.status
        except RestError:
            raise
        except urllib.error.HTTPError as exc:
            code = None
            try:
                parsed = json.loads(exc.read(4096) or b"{}")
                c = parsed.get("code") if isinstance(parsed, dict) else None
                code = c if isinstance(c, str) and _PGCODE.match(c) else None
            except (ValueError, OSError):
                pass
            s = exc.code
            retryable = s in (401, 403, 404, 408, 425, 429) or s >= 500
            raise RestError("http_error", s, code, retryable) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # the reason text is scrubbed, then dropped: only the exception class is kept
            self._scrub(str(exc))
            raise RestError("network:" + type(exc).__name__) from None
        if status in (301, 302, 303, 307, 308):
            raise RestError("redirect_refused", status, retryable=False)
        if len(raw) > self._max:
            raise RestError("response_too_large", status, retryable=False)
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise RestError("bad_response", status) from None


# --------------------------------------------------------------------------- evidence sync

class DrainReport:
    def __init__(self):
        self.counts = {INSERTED: 0, UNCHANGED: 0, CONFLICT: 0, FAILED: 0}
        self.quarantined = 0
        self.pending = 0
        self.stopped: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.counts[CONFLICT] == 0 and self.counts[FAILED] == 0 and self.quarantined == 0

    def __repr__(self) -> str:
        return f"DrainReport({self.counts}, quarantined={self.quarantined}, pending={self.pending}, stopped={self.stopped!r})"


class EvidenceSync:
    def __init__(self, outbox: Outbox, client: RestClient, owner_id: str, *, batch: int = 50, source: str = SOURCE):
        if not isinstance(owner_id, str) or not _UUID.match(owner_id):
            raise ClientRefused("owner_id must be a lowercase uuid")
        self.outbox, self.client, self.owner_id, self.batch, self.source = outbox, client, owner_id, max(1, batch), source

    def _row(self, ev: Mapping[str, Any]) -> Dict[str, Any]:
        return {"owner_id": self.owner_id, "event_id": ev["event_id"], "kind": ev["kind"], "session_id": ev["session_id"],
                "seq": ev["seq"], "payload": ev["payload"], "occurred_at": _iso(ev["occurred_at_ms"]),
                "source": self.source, "schema_version": SCHEMA_VERSION, "content_sha256": ev["content_sha256"]}

    def _send(self, ev: Mapping[str, Any]) -> Tuple[str, Optional[RestError]]:
        try:
            verdict = self.client.rpc("exec_event_put", {"p_row": self._row(ev)})
        except RestError as exc:
            return FAILED, exc
        if verdict in (INSERTED, UNCHANGED, CONFLICT):
            return verdict, None
        return FAILED, RestError("bad_response", retryable=True)

    def drain(self, max_events: Optional[int] = None) -> DrainReport:
        """Send pending events in order. INSERTED / UNCHANGED advance the cursor; CONFLICT and deterministic rejections are
        quarantined (kept on disk, reported, cursor advances, never retried); any retryable failure stops the drain and leaves
        the cursor so the next drain resumes at the same event. Re-running after a crash is safe (the database reports
        UNCHANGED for what it already stored)."""
        rep = DrainReport()
        sent = 0
        last: Optional[Tuple[int, int]] = None
        try:
            for seg, off, ev in list(self.outbox.iter_pending()):
                if max_events is not None and sent >= max_events:
                    break
                findings = [f"{p}:{r}" for p, r in _ss.scan_value({"payload": ev["payload"]}, "event")]
                if findings:  # defence in depth: never send what the guard refuses now
                    self.outbox.quarantine_item(ev, "SECRET_GUARD", "; ".join(findings[:5]))
                    rep.quarantined += 1
                    rep.counts[FAILED] += 1
                    last = (seg, off)
                    continue
                verdict, err = self._send(ev)
                sent += 1
                if verdict in (INSERTED, UNCHANGED):
                    rep.counts[verdict] += 1
                    last = (seg, off)
                elif verdict == CONFLICT:
                    rep.counts[CONFLICT] += 1
                    self.outbox.quarantine_item(ev, "CONFLICT", "remote has the event_id (or hash) with different content")
                    rep.quarantined += 1
                    last = (seg, off)
                else:
                    assert err is not None
                    rep.counts[FAILED] += 1
                    if err.retryable:
                        rep.stopped = str(err)
                        break
                    self.outbox.quarantine_item(ev, "REJECTED", str(err))   # deterministic refusal: retrying cannot help
                    rep.quarantined += 1
                    last = (seg, off)
                if last is not None and sent % self.batch == 0:
                    self.outbox.commit(*last)
        finally:
            if last is not None:
                self.outbox.commit(*last)
            rep.pending = self.outbox.pending
        return rep

    def send_heartbeat(self, hb: Mapping[str, Any]) -> str:
        """Best-effort telemetry (not outboxed; a missed heartbeat is itself the signal). Returns a verdict string."""
        row = dict(validate_heartbeat(hb))
        guard_payload(row, "heartbeat")
        row.update({"owner_id": self.owner_id, "source": self.source, "schema_version": SCHEMA_VERSION,
                    "content_sha256": _ss.content_sha256(hb)})
        try:
            verdict = self.client.rpc("exec_heartbeat_put", {"p_row": row}, retries=0)
        except RestError:
            return FAILED
        return verdict if verdict in (INSERTED, UNCHANGED, CONFLICT) else FAILED


# --------------------------------------------------------------------------- lease

class LeaseClient:
    """Global single-writer lease with a fencing token. FAILS CLOSED: any network / HTTP / parse error leaves this client
    NOT holding the lease. Call renew() about every ttl/3; call assert_held() before every order-capable step."""

    def __init__(self, client: RestClient, owner_id: str, holder: str, *, ttl_seconds: int = 30,
                 margin_seconds: Optional[float] = None, monotonic: Callable[[], float] = time.monotonic):
        if not isinstance(owner_id, str) or not _UUID.match(owner_id):
            raise ClientRefused("owner_id must be a lowercase uuid")
        if not isinstance(holder, str) or not _NAME.match(holder):
            raise ClientRefused("bad holder name")
        if not (1 <= int(ttl_seconds) <= 3600):
            raise ClientRefused("ttl_seconds must be 1..3600")
        self.client, self.owner_id, self.holder, self.ttl = client, owner_id, holder, int(ttl_seconds)
        self.margin = float(self.ttl) * 0.25 if margin_seconds is None else float(margin_seconds)
        self._mono = monotonic
        self._token: Optional[int] = None
        self._deadline = 0.0

    @property
    def token(self) -> Optional[int]:
        """The fencing token this client believes it holds (None when not held / expired locally)."""
        return self._token if self.held else None

    @property
    def held(self) -> bool:
        return self._token is not None and self._mono() < self._deadline

    def _drop(self) -> None:
        self._token, self._deadline = None, 0.0

    def acquire(self) -> bool:
        t0 = self._mono()
        self._drop()
        try:
            res = self.client.rpc("exec_lease_acquire", {"p_owner": self.owner_id, "p_holder": self.holder, "p_ttl_seconds": self.ttl}, retries=0)
        except RestError:
            return False
        if isinstance(res, bool) or not isinstance(res, int) or res < 1:
            return False   # null = another holder; anything else = unparseable: not held
        self._token, self._deadline = res, t0 + self.ttl - self.margin
        return True

    def renew(self) -> bool:
        token = self._token
        if token is None:
            return False
        t0 = self._mono()
        self._drop()
        try:
            res = self.client.rpc("exec_lease_renew", {"p_owner": self.owner_id, "p_holder": self.holder, "p_token": token,
                                                       "p_ttl_seconds": self.ttl}, retries=0)
        except RestError:
            return False
        if res is not True:
            return False
        self._token, self._deadline = token, t0 + self.ttl - self.margin
        return True

    def release(self) -> bool:
        token = self._token
        self._drop()
        if token is None:
            return False
        try:
            return self.client.rpc("exec_lease_release", {"p_owner": self.owner_id, "p_holder": self.holder, "p_token": token}, retries=0) is True
        except RestError:
            return False

    def assert_held(self, *, verify_remote: bool = False) -> int:
        """Return the fencing token or raise LeaseNotHeld. Local check by default (cheap, conservative: the deadline is
        measured from the START of the last successful acquire/renew, minus a margin). ``verify_remote=True`` renews first, a
        round trip that proves the lease with the database."""
        if verify_remote and not self.renew():
            raise LeaseNotHeld("lease not held (remote verification failed)")
        if not self.held:
            raise LeaseNotHeld("lease not held or locally expired: do not take an order-capable step")
        assert self._token is not None
        return self._token


# --------------------------------------------------------------------------- heartbeat

HEARTBEAT_FIELDS = ("worker_id", "app", "machine_id", "region", "code_revision", "runtime_version", "run_id", "mode", "state",
                    "session_id", "last_bybit_read_ms", "last_market_data_ms", "last_reconcile_ms", "last_supabase_sync_ms",
                    "outbox_pending", "disk_free_bytes", "lease_token", "seq", "at_ms")
_HB_STR = ("worker_id", "app", "machine_id", "region", "code_revision", "runtime_version", "run_id")
_HB_OPT_INT = ("last_bybit_read_ms", "last_market_data_ms", "last_reconcile_ms", "last_supabase_sync_ms", "lease_token")


def _int(v: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
        raise EventRefused(f"heartbeat {name} must be an int >= {minimum}")
    return v


def validate_heartbeat(hb: Mapping[str, Any]) -> Dict[str, Any]:
    """The shared contract, exactly: no missing and no extra fields."""
    if set(hb) != set(HEARTBEAT_FIELDS):
        raise EventRefused("heartbeat fields differ from the contract")
    out: Dict[str, Any] = {}
    for k in _HB_STR:
        v = hb[k]
        if not isinstance(v, str) or not v.strip() or len(v) > 200:
            raise EventRefused(f"heartbeat {k} must be a non-empty string")
        out[k] = v
    if hb["mode"] not in MODES:
        raise EventRefused("heartbeat mode must be PAPER or TESTNET")
    if hb["state"] not in ENGINE_STATES:
        raise EventRefused("heartbeat state is not an engine state")
    sid = hb["session_id"]
    if sid is not None and (not isinstance(sid, str) or not sid.strip()):
        raise EventRefused("heartbeat session_id must be null or a non-empty string")
    out["mode"], out["state"], out["session_id"] = hb["mode"], hb["state"], sid
    for k in _HB_OPT_INT:
        out[k] = None if hb[k] is None else _int(hb[k], k, minimum=1 if k == "lease_token" else 0)
    out["outbox_pending"] = _int(hb["outbox_pending"], "outbox_pending")
    out["disk_free_bytes"] = _int(hb["disk_free_bytes"], "disk_free_bytes")
    out["seq"] = _int(hb["seq"], "seq")
    out["at_ms"] = _int(hb["at_ms"], "at_ms", minimum=1)
    return {k: out[k] for k in HEARTBEAT_FIELDS}


def disk_free_bytes(path: str) -> int:
    return int(shutil.disk_usage(path).free)


def build_heartbeat(*, worker_id: str, app: str, machine_id: str, region: str, code_revision: str, runtime_version: str,
                    run_id: str, mode: str, state: str, seq: int, outbox_pending: int, disk_free: int,
                    session_id: Optional[str] = None, last_bybit_read_ms: Optional[int] = None,
                    last_market_data_ms: Optional[int] = None, last_reconcile_ms: Optional[int] = None,
                    last_supabase_sync_ms: Optional[int] = None, lease_token: Optional[int] = None,
                    at_ms: Optional[int] = None) -> Dict[str, Any]:
    """Build and validate the shared heartbeat dict (values are passed in; nothing is read from the environment)."""
    return validate_heartbeat({
        "worker_id": worker_id, "app": app, "machine_id": machine_id, "region": region, "code_revision": code_revision,
        "runtime_version": runtime_version, "run_id": run_id, "mode": mode, "state": state, "session_id": session_id,
        "last_bybit_read_ms": last_bybit_read_ms, "last_market_data_ms": last_market_data_ms,
        "last_reconcile_ms": last_reconcile_ms, "last_supabase_sync_ms": last_supabase_sync_ms,
        "outbox_pending": outbox_pending, "disk_free_bytes": disk_free, "lease_token": lease_token, "seq": seq,
        "at_ms": _now_ms() if at_ms is None else at_ms})
