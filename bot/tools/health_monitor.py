#!/usr/bin/env python3
"""Independent health monitor for the Fly execution worker. Standard library only.

A heartbeat emitted BY the worker cannot detect a dead worker, so this runs elsewhere
(a scheduled GitHub-hosted job) and judges ONLY what the worker stored in Supabase:
the latest ``carry.exec_heartbeats`` row per worker and recent ``carry.exec_events``
rows with ``kind = 'INCIDENT'`` (payload ``{code, severity, detail}``).

IT NEVER CONTACTS BYBIT AND HOLDS NO EXCHANGE CREDENTIAL. Its only network peers are the
project's ``*.supabase.co`` PostgREST endpoint (read-only GET, with a READ-ONLY key) and
the notifier endpoints from ``tools/notifier``. The key is never printed or logged.

FAIL CLOSED: an unreadable database, an unrecognised state, a missing field, a missing
notifier configuration and a failed notification are all findings and a nonzero exit.

Exit codes: 0 healthy | 1 a finding at or above ``--fail-at`` | 2 the data could not be
read or the monitor is misconfigured.

THRESHOLDS (module constants, seconds unless noted; the heartbeat cadence is the worker's
own, these assume it beats at least every ~60 s and the monitor runs every 10 min)
  HEARTBEAT_STALE_S     300   heartbeat older than this: worker not reporting
  STUCK_S               900   same (state, seq) observed this long while a session is active
  BYBIT_READ_STALE_S    300   last_bybit_read_ms age
  MARKET_DATA_STALE_S   300   last_market_data_ms age
  RECONCILE_STALE_S    1800   last_reconcile_ms age
  SUPABASE_SYNC_STALE_S 600   last_supabase_sync_ms age
  OUTBOX_PENDING_MAX    500   outbox_pending above this
  DISK_FREE_MIN_BYTES   1 GiB disk_free_bytes below this
  CLOCK_SKEW_S           120  a heartbeat dated this far in the future is UNKNOWN
  INCIDENT_WINDOW_S     3600  an INCIDENT event this recent is a finding
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence
from urllib.parse import quote, urlsplit

try:
    from tools import notifier as _n  # type: ignore
except Exception:  # pragma: no cover - run as a script from tools/
    import notifier as _n  # type: ignore

HEARTBEAT_STALE_S = 300
STUCK_S = 900
BYBIT_READ_STALE_S = 300
MARKET_DATA_STALE_S = 300
RECONCILE_STALE_S = 1800
SUPABASE_SYNC_STALE_S = 600
OUTBOX_PENDING_MAX = 500
DISK_FREE_MIN_BYTES = 1 << 30
CLOCK_SKEW_S = 120
INCIDENT_WINDOW_S = 3600
HEARTBEAT_LOOKBACK_S = 7 * 86400

#: ASSUMPTION to confirm with the owner of the tables: the epoch-ms column on carry.exec_events.
EVENT_TIME_COLUMN = "received_at"        # carry.exec_events has occurred_at / received_at (timestamptz), NOT at_ms: querying a column that does not exist would be a permanent HTTP 400

ENGINE_STATES = frozenset((
    "INITIALISING", "DATA_CHECK", "RESEARCHING", "WAITING_FOR_OPPORTUNITY", "PLAN_PROPOSED",
    "RISK_APPROVED", "EXECUTING", "HEDGED", "EXITING", "RECONCILING", "FLAT", "PAUSED", "ALERT",
    "STOPPED", "RECOVERING", "UNRECONCILED", "DRAINING"))
#: not in the shared contract but a worker may report it; treated as the bad state it names
EXTRA_STATES = frozenset(("NOT_FLAT",))
LEASE_STATES = frozenset(("EXECUTING", "HEDGED", "EXITING"))
#: states in which a missing data timestamp is normal (warming up / shut down)
WARMUP_STATES = frozenset(("INITIALISING", "DATA_CHECK", "STOPPED"))
NO_PROGRESS_EXPECTED = frozenset(("STOPPED", "PAUSED"))

SEVERITIES = {"INFO": 0, "WARNING": 1, "ERROR": 2, "CRITICAL": 3}

_STR = ("worker_id", "app", "machine_id", "region", "code_revision", "runtime_version", "run_id", "mode", "state")
_INT = ("outbox_pending", "disk_free_bytes", "seq", "at_ms")
_NULL_INT = ("last_bybit_read_ms", "last_market_data_ms", "last_reconcile_ms", "last_supabase_sync_ms",
             "lease_token")
_NULL_STR = ("session_id",)


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    worker_id: str
    detail: str

    @property
    def key(self) -> str:
        return f"{self.code}:{self.worker_id}:{self.detail.split(' ')[0] if self.code == 'INCIDENT' else ''}"

    @property
    def text(self) -> str:
        return f"{self.code} worker={self.worker_id}: {self.detail}"


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _problems(row) -> List[str]:
    if not isinstance(row, dict):
        return ["row is not an object"]
    out = []
    for f in _STR:
        if f not in row:
            out.append(f"{f} missing")
        elif not isinstance(row[f], str) or not row[f]:
            out.append(f"{f} invalid")
    for f in _INT:
        if f not in row:
            out.append(f"{f} missing")
        elif not _is_int(row[f]):
            out.append(f"{f} invalid")
    for f in _NULL_INT:
        if f not in row:
            out.append(f"{f} missing")
        elif row[f] is not None and not _is_int(row[f]):
            out.append(f"{f} invalid")
    for f in _NULL_STR:
        if f not in row:
            out.append(f"{f} missing")
        elif row[f] is not None and not isinstance(row[f], str):
            out.append(f"{f} invalid")
    return out


def latest_per_worker(rows: Sequence[dict]) -> List[dict]:
    """Newest row per worker_id (by at_ms; rows without a usable worker_id are kept as unknown)."""
    best: Dict[str, dict] = {}
    bad: List[dict] = []
    for r in rows:
        w = r.get("worker_id") if isinstance(r, dict) else None
        if not isinstance(w, str) or not w:
            bad.append(r if isinstance(r, dict) else {})
            continue
        cur = best.get(w)
        t = r.get("at_ms") if _is_int(r.get("at_ms")) else -1
        if cur is None or t >= (cur.get("at_ms") if _is_int(cur.get("at_ms")) else -1):
            best[w] = r
    return list(best.values()) + bad


def _incident_fields(ev) -> Optional[dict]:
    p = ev.get("payload") if isinstance(ev, dict) else None
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except Exception:
            p = None
    return p if isinstance(p, dict) else None


def evaluate(heartbeats: Sequence[dict], incidents: Sequence[dict], now_ms: int,
             observed: Optional[dict] = None, *, heartbeat_stale_s: int = HEARTBEAT_STALE_S,
             stuck_s: int = STUCK_S, bybit_read_stale_s: int = BYBIT_READ_STALE_S,
             market_data_stale_s: int = MARKET_DATA_STALE_S, reconcile_stale_s: int = RECONCILE_STALE_S,
             supabase_sync_stale_s: int = SUPABASE_SYNC_STALE_S, outbox_pending_max: int = OUTBOX_PENDING_MAX,
             disk_free_min_bytes: int = DISK_FREE_MIN_BYTES, incident_window_s: int = INCIDENT_WINDOW_S
             ) -> List[Finding]:
    """Pure judgement of stored rows. ``observed`` (caller-persisted dict) carries what the previous
    run saw (state/seq/lease per worker) so WORKER_STUCK and LEASE_LOST need no history table; it is
    updated in place. No healthy default: unrecognised or missing data is UNKNOWN."""
    obs = observed if observed is not None else {}
    findings: List[Finding] = []
    rows = latest_per_worker(heartbeats)
    if not rows:
        findings.append(Finding("HEARTBEAT_MISSING", "CRITICAL", "*",
                                "no heartbeat row exists for any worker"))
    seen = set()
    active_workers: List[str] = []
    for row in rows:
        probs = _problems(row)
        wid = row.get("worker_id") if isinstance(row, dict) and isinstance(row.get("worker_id"), str) and row.get("worker_id") else "?"
        state = row.get("state") if isinstance(row, dict) else None
        if probs:
            findings.append(Finding("UNKNOWN", "CRITICAL", wid, "unusable heartbeat: " + ", ".join(probs[:6])))
            if isinstance(state, str) and state not in ENGINE_STATES | EXTRA_STATES:
                findings.append(Finding("UNKNOWN", "CRITICAL", wid, f"unrecognised state {state[:40]!r}"))
            continue
        seen.add(wid)
        at = row["at_ms"]
        age_s = (now_ms - at) / 1000.0
        if state not in ENGINE_STATES | EXTRA_STATES:
            findings.append(Finding("UNKNOWN", "CRITICAL", wid, f"unrecognised state {state[:40]!r}"))
        if age_s < -CLOCK_SKEW_S:
            findings.append(Finding("UNKNOWN", "CRITICAL", wid, f"heartbeat dated {-age_s:.0f}s in the future"))
        fresh = -CLOCK_SKEW_S <= age_s <= heartbeat_stale_s
        if age_s > heartbeat_stale_s:
            findings.append(Finding("HEARTBEAT_STALE", "CRITICAL", wid,
                                    f"last heartbeat {age_s:.0f}s ago (limit {heartbeat_stale_s}s), state {state}"))
        if fresh and state != "STOPPED":
            active_workers.append(wid)
        # --- state classification
        if state == "ALERT":
            findings.append(Finding("ALERT", "CRITICAL", wid, "engine is in ALERT"))
        elif state == "UNRECONCILED":
            findings.append(Finding("UNRECONCILED", "CRITICAL", wid, "engine is UNRECONCILED"))
        elif state == "NOT_FLAT":
            findings.append(Finding("NOT_FLAT", "CRITICAL", wid, "worker reports NOT_FLAT"))
        elif state == "STOPPED":
            findings.append(Finding("WORKER_STOPPED", "WARNING", wid, "worker reports STOPPED"))
        elif state == "PAUSED":
            # Round 22: a PAUSED executor is not trading, and the reasons it pauses for (a mis-armed environment, a full state volume, a lost or unreachable lease, no grant) never raise an ALERT. It used to be
            # silent here, so a worker that had been refusing to run for days looked healthy. The reason is in the engine state on the machine (autopilot-ctl status), not in the heartbeat.
            findings.append(Finding("WORKER_PAUSED", "WARNING", wid, "worker reports PAUSED: it is not trading (read `autopilot-ctl status` for why)"))
        # --- freshness of the data the worker last saw (skipped when the heartbeat itself is stale)
        if age_s <= heartbeat_stale_s:
            for code, f, lim in (("BYBIT_READ_STALE", "last_bybit_read_ms", bybit_read_stale_s),
                                 ("MARKET_DATA_STALE", "last_market_data_ms", market_data_stale_s),
                                 ("RECONCILE_STALE", "last_reconcile_ms", reconcile_stale_s),
                                 ("SUPABASE_SYNC_STALE", "last_supabase_sync_ms", supabase_sync_stale_s)):
                if state == "STOPPED":
                    continue
                v = row[f]
                if v is None:
                    if state not in WARMUP_STATES:
                        findings.append(Finding(code, "ERROR", wid, f"{f} was never reported in state {state}"))
                elif (now_ms - v) / 1000.0 > lim:
                    findings.append(Finding(code, "ERROR", wid, f"{f} is {(now_ms - v) / 1000.0:.0f}s old (limit {lim}s)"))
        if row["outbox_pending"] > outbox_pending_max:
            findings.append(Finding("OUTBOX_BACKLOG", "ERROR", wid,
                                    f"outbox_pending={row['outbox_pending']} (limit {outbox_pending_max})"))
        if row["disk_free_bytes"] < disk_free_min_bytes:
            findings.append(Finding("DISK_LOW", "ERROR", wid,
                                    f"disk_free_bytes={row['disk_free_bytes']} (min {disk_free_min_bytes})"))
        # --- lease and stuck need the previous observation
        prev = obs.get(wid) if isinstance(obs.get(wid), dict) else None
        if state in LEASE_STATES:
            if row["lease_token"] is None:
                findings.append(Finding("LEASE_LOST", "CRITICAL", wid, f"lease_token is null in state {state}"))
            elif (prev and prev.get("run_id") == row["run_id"] and prev.get("lease_token") is not None
                  and prev.get("lease_token") != row["lease_token"]):
                findings.append(Finding("LEASE_LOST", "CRITICAL", wid,
                                        f"lease_token changed during run {row['run_id'][:24]} in state {state}"))
        since = now_ms
        if prev and prev.get("state") == state and prev.get("seq") == row["seq"] and _is_int(prev.get("since_ms")):
            since = prev["since_ms"]
        if (fresh and row["session_id"] is not None and state not in NO_PROGRESS_EXPECTED
                and (now_ms - since) / 1000.0 > stuck_s):
            findings.append(Finding("WORKER_STUCK", "CRITICAL", wid,
                                    f"state {state} seq {row['seq']} unchanged for {(now_ms - since) / 1000.0:.0f}s "
                                    f"with an active session"))
        obs[wid] = {"state": state, "seq": row["seq"], "since_ms": since,
                    "lease_token": row["lease_token"], "run_id": row["run_id"]}
    for w in [w for w in obs if w not in seen]:
        obs.pop(w, None)
    if len(active_workers) > 1:
        findings.append(Finding("MULTIPLE_WORKERS", "CRITICAL", "*",
                                f"{len(active_workers)} workers are reporting ({', '.join(sorted(active_workers))[:120]}): "
                                "duplicate executor danger"))
    # --- incident events
    for ev in incidents:
        p = _incident_fields(ev)
        if p is None:
            findings.append(Finding("UNKNOWN", "CRITICAL", "*", "unreadable INCIDENT event payload"))
            continue
        t = _event_ms(ev.get(EVENT_TIME_COLUMN)) if isinstance(ev, dict) else None
        if t is not None and (now_ms - t) / 1000.0 > incident_window_s:
            continue
        code = str(p.get("code", "")).strip()[:60]
        sev = str(p.get("severity", "")).upper()
        sev = sev if sev in SEVERITIES else "CRITICAL"
        detail = str(p.get("detail", ""))[:200]
        wid = str(ev.get("worker_id", "*")) if isinstance(ev, dict) else "*"
        if not code:
            findings.append(Finding("UNKNOWN", "CRITICAL", wid, "INCIDENT event without a code"))
        elif "RISK_LIMIT" in code.upper():
            findings.append(Finding("RISK_LIMIT_HIT", "CRITICAL" if sev == "INFO" else sev, wid, f"{code} {detail}".strip()))
        elif code.upper() == "NOT_FLAT":
            findings.append(Finding("NOT_FLAT", "CRITICAL", wid, f"incident {detail}".strip()))
        else:
            findings.append(Finding("INCIDENT", sev, wid, f"{code} {detail}".strip()))
    uniq: Dict[tuple, Finding] = {}
    for f in findings:
        uniq.setdefault((f.code, f.worker_id, f.detail), f)
    return list(uniq.values())


# ------------------------------------------------------------------ read-only PostgREST

class ReadError(Exception):
    """Fixed-text read failure; never carries the URL or the key."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # pragma: no cover - trivial
        return None


def validate_supabase_url(url: str) -> str:
    try:
        p = urlsplit(url.strip())
        host, port = p.hostname, p.port
    except Exception:
        raise ReadError("SUPABASE_URL is not a valid URL") from None
    if (p.scheme != "https" or not host or not host.endswith(".supabase.co") or host == ".supabase.co"
            or p.username or p.password or port not in (None, 443) or (p.path not in ("", "/"))):
        raise ReadError("SUPABASE_URL must be https://<project>.supabase.co")
    return f"https://{host}"


_PRIVILEGED_ROLES = frozenset({"service_role", "supabase_admin", "postgres"})


def _privileged_key(key: str) -> bool:
    """True for a key that is KNOWN to be privileged: a Supabase secret key (``sb_secret_...``) or a JWT whose ``role`` claim is service_role / supabase_admin / postgres. The monitor key must be a
    restricted READ-ONLY one (the workflow comment says so; nothing checked it): the executor lease, the heartbeats and the execution evidence live in the same project, and a service-role key
    in a repository secret is reachable from any branch's workflow. A key whose shape is not recognised is not refused here (a restricted key is a JWT with its own role)."""
    if key.startswith("sb_secret_"):
        return True
    parts = key.split(".")
    if len(parts) != 3:
        return False
    try:
        import base64
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(payload, dict) and str(payload.get("role", "")).lower() in _PRIVILEGED_ROLES


def postgrest_reader(base_url: str, key: str, *, timeout: float = 20.0, opener=None):
    """``read(table, query) -> list[dict]``: one read-only GET on schema ``carry``. The key is sent
    as a header only, never printed; failures raise ``ReadError`` with a fixed text."""
    base = validate_supabase_url(base_url)
    if not key or not re.fullmatch(r"[A-Za-z0-9._-]{16,4096}", key):
        raise ReadError("SUPABASE_MONITOR_KEY is missing or malformed")
    if _privileged_key(key):
        raise ReadError("SUPABASE_MONITOR_KEY is a privileged (service-role) key: the monitor reads with a restricted read-only key only")
    op = opener or urllib.request.build_opener(_NoRedirect)

    def read(table: str, query: str) -> List[dict]:
        req = urllib.request.Request(f"{base}/rest/v1/{table}?{query}", method="GET", headers={
            "apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json",
            "Accept-Profile": "carry", "User-Agent": "carry-health-monitor"})
        try:
            with op.open(req, timeout=timeout) as r:
                data = json.loads(r.read(8 * 1024 * 1024).decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise ReadError(f"read of {table} failed (HTTP {e.code})") from None
        except Exception as e:
            raise ReadError(f"read of {table} failed ({type(e).__name__})") from None
        if not isinstance(data, list):
            raise ReadError(f"read of {table} returned an unexpected shape")
        return data
    return read


def _iso(ms: int) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ms / 1000.0, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event_ms(v: Any) -> Optional[int]:
    """An event time as epoch ms: an int (ms) or an ISO-8601 timestamptz string. Anything else is None (the event is then KEPT, never skipped)."""
    if _is_int(v):
        return int(v)
    if isinstance(v, str):
        import datetime as _dt
        try:
            return int(_dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return None
    return None


def fetch(read: Callable[[str, str], List[dict]], now_ms: int):
    since = now_ms - HEARTBEAT_LOOKBACK_S * 1000
    hb = read("exec_heartbeats", f"select=*&at_ms=gte.{since}&order=at_ms.desc&limit=500")
    inc_since = now_ms - INCIDENT_WINDOW_S * 1000
    ev = read("exec_events", f"select=*&kind=eq.INCIDENT&{EVENT_TIME_COLUMN}=gte.{_iso(inc_since)}"
                             f"&order={EVENT_TIME_COLUMN}.desc&limit=200")
    return hb, ev


# ------------------------------------------------------------------------------- CLI

def _summary(findings: Sequence[Finding], extra: Sequence[str], exit_code: int) -> str:
    out = ["## Fly heartbeat monitor", "", f"Result: **{'RED' if exit_code else 'green'}** "
           f"({len(findings)} finding(s))", ""]
    if findings:
        out += ["| severity | code | worker | detail |", "|---|---|---|---|"]
        for f in findings:
            out.append("| {} | {} | {} | {} |".format(
                f.severity, f.code, f.worker_id, _n.sanitize(f.detail, limit=300).replace("|", "/").replace("\n", " ")))
    else:
        out.append("No findings.")
    out += [""] + [f"- {_n.sanitize(e, limit=300)}" for e in extra]
    return "\n".join(out) + "\n"


def cli(argv: Optional[Sequence[str]], env: Dict[str, str], *, reader=None, now_ms: Optional[int] = None,
        notifier_kwargs: Optional[dict] = None, out=None) -> int:
    ap = argparse.ArgumentParser(description="independent Fly worker health monitor (reads Supabase only)")
    ap.add_argument("--fail-at", choices=sorted(SEVERITIES, key=SEVERITIES.get), default="WARNING")
    ap.add_argument("--state-file", default=None)
    ap.add_argument("--summary-file", default=None)
    ap.add_argument("--renotify-minutes", type=int, default=360)
    args = ap.parse_args(argv)
    out = out or sys.stdout
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    floor = SEVERITIES[args.fail_at]
    state = _n.load_state(args.state_file)
    observed = state.setdefault("observed", {})
    findings: List[Finding] = []
    readable = True
    try:
        rd = reader or postgrest_reader(env.get("SUPABASE_URL", ""), env.get("SUPABASE_MONITOR_KEY", ""))
        hb, ev = fetch(rd, now)
        findings += evaluate(hb, ev, now, observed)
    except ReadError as e:
        readable = False
        findings.append(Finding("DATA_UNREADABLE", "CRITICAL", "*", str(e)))
    except Exception as e:  # nothing unexpected may turn into a pass
        readable = False
        findings.append(Finding("DATA_UNREADABLE", "CRITICAL", "*", f"monitor error ({type(e).__name__})"))

    extra: List[str] = []
    meta: List[Finding] = []
    try:
        notifiers = _n.build_notifiers(env.get("NOTIFY_WEBHOOK_URL"), env.get("TELEGRAM_BOT_TOKEN"),
                                       env.get("TELEGRAM_CHAT_ID"), **(notifier_kwargs or {}))
    except ValueError as e:
        notifiers = [_n.NotConfiguredNotifier("invalid notifier configuration")]
        meta.append(Finding("NOTIFIER_CONFIG_INVALID", "CRITICAL", "*", str(e)))
    if all(isinstance(n, _n.NotConfiguredNotifier) for n in notifiers):
        if not meta:
            meta.append(Finding("NOTIFIER_NOT_CONFIGURED", "CRITICAL", "*",
                                "no notifier configured: nobody would be told about an incident"))
    alerts = [_n.Alert(f.key, f.severity, f.text) for f in findings if SEVERITIES[f.severity] >= floor]
    outcome = _n.dispatch(notifiers, alerts, state, now, args.renotify_minutes * 60_000, allow_recovery=readable)
    for r in outcome.results:
        extra.append(f"notify {r.adapter}: {r.status} {r.detail}".strip())
    if outcome.failed and not outcome.alerts_sent and not outcome.recovered:
        meta.append(Finding("NOTIFY_FAILED", "CRITICAL", "*", "no notifier delivered the message"))
    findings += meta
    if args.state_file:
        try:
            _n.save_state(args.state_file, state)
        except OSError:
            findings.append(Finding("STATE_NOT_SAVED", "WARNING", "*", "could not persist the notifier state"))
    bad = [f for f in findings if SEVERITIES[f.severity] >= floor]
    code = 2 if any(f.code in ("DATA_UNREADABLE",) for f in findings) else (1 if bad else 0)
    for f in findings:
        print(f"{f.severity} {f.code} worker={f.worker_id} {_n.sanitize(f.detail, limit=300)}", file=out)
    print(f"health_monitor: {len(findings)} finding(s), exit {code}", file=out)
    if args.summary_file:
        try:
            with open(args.summary_file, "a", encoding="utf-8") as h:
                h.write(_summary(findings, extra, code))
        except OSError:
            pass
    return code


if __name__ == "__main__":
    # the ONLY environment read: named, non-exchange variables, never echoed
    raise SystemExit(cli(sys.argv[1:], dict(os.environ)))
