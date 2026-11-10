#!/usr/bin/env python3
"""Durable job queue for the research and challenge agents (the ``carry-research`` service). Stdlib only (sqlite3, WAL). No network, no venue, no order, no credential: this module imports
nothing that can reach an exchange (a test walks its imports) and its only output is DATA in a database file and typed proposals validated by ``agent_contracts``.

A JOB is one bounded unit of research. Each has a role (one of ``agent_contracts.ROLES``), typed inputs (named references to exact data by sha256, plus a few scalar parameters), the code
version and the configuration digest that produce its output (so a result always names the exact data AND code), a time limit, a retry policy and, if it calls an external model, a cost
estimate counted against a DAILY budget.

LIFECYCLE   QUEUED -> RUNNING -> SUCCEEDED
                         |-> QUEUED (retry with exponential backoff)   -> ... -> DEAD (attempts exhausted)
                         |-> QUARANTINED (the output was not a valid proposal: a deterministic fault, never retried)
LEASES      claim() sets a lease (now + timeout) and a FENCING TOKEN that grows with every claim. complete/fail/heartbeat with a stale token are refused, so a worker that was too slow (and whose
            job was handed to another) can never overwrite the newer attempt. An expired lease is reclaimed on the next claim and counts as a consumed attempt (a hung job cannot loop forever).
OUTPUT      every output item goes through ``agent_contracts.validate`` (closed keys, no decision inputs, no secrets, no code); the role must match the job's role.
REVIEW      nothing is promotable on its own say-so: a candidate output needs a SUCCEEDED review job from a DIFFERENT worker with role quant_challenger/red_team and the verdict SURVIVES. Even then
            "promotable" only means a human or the deterministic pre-registration process MAY register it: it still faces the economics gate, the risk caps and the authority before any order.
BUDGET      jobs flagged ``needs_model`` are claimed only while the day's call count and cost estimate stay within the configured limits (0 = no external model allowed at all). Deterministic
            jobs are never blocked by the budget.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import agent_contracts as AC

STATUSES = ("QUEUED", "RUNNING", "SUCCEEDED", "DEAD", "QUARANTINED")
REVIEW_ROLES = ("quant_challenger", "red_team")
BACKOFF_BASE_S, BACKOFF_MAX_S = 30, 3600
SCHEMA = """
create table if not exists jobs (
  id text primary key, dedupe_key text not null unique, role text not null, kind text not null, inputs_json text not null, inputs_digest text not null,
  code_version text not null, config_digest text not null, status text not null, attempts integer not null default 0, max_attempts integer not null, timeout_s integer not null,
  not_before_ms integer not null default 0, lease_owner text, lease_token integer not null default 0, lease_expires_ms integer, needs_model integer not null default 0,
  est_cost_micros integer not null default 0, created_ms integer not null, updated_ms integer not null, last_error text, completed_by text, output_digest text, output_json text);
create table if not exists budget (day text primary key, calls integer not null default 0, micros integer not null default 0);
create table if not exists events (seq integer primary key autoincrement, job_id text not null, at_ms integer not null, event text not null, detail text);
create table if not exists reviews (producer_job text not null, review_job text not null, verdict text not null, at_ms integer not null, primary key (producer_job, review_job));
create trigger if not exists events_no_update before update on events begin select raise(abort, 'events are append-only'); end;
create trigger if not exists events_no_delete before delete on events begin select raise(abort, 'events are append-only'); end;
"""


class JobRefused(ValueError):
    pass


def _canon(o: Any) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


def _sha(o: Any) -> str:
    return hashlib.sha256(_canon(o).encode("utf-8")).hexdigest()


def validate_inputs(inputs: Mapping[str, Any]) -> Dict[str, Any]:
    """refs: [{name, sha256}] naming EXACT data (never a path, never a URL); params: a few lowercase scalars. Anything else is refused."""
    if not isinstance(inputs, Mapping) or set(inputs) - {"refs", "params"}:
        raise JobRefused("inputs: only 'refs' and 'params' are allowed")
    refs, params = inputs.get("refs", []), inputs.get("params", {})
    if not isinstance(refs, list) or len(refs) > 50:
        raise JobRefused("inputs.refs: a list of at most 50")
    for r in refs:
        if not (isinstance(r, Mapping) and set(r) == {"name", "sha256"} and isinstance(r["name"], str) and re.fullmatch(r"[A-Za-z0-9_.:@-]{1,120}", r["name"])
                and isinstance(r["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", r["sha256"])):
            raise JobRefused("inputs.refs: each ref is {name, sha256} with a 64-hex digest")
    if not isinstance(params, Mapping) or len(params) > AC.MAX_PARAMS:
        raise JobRefused("inputs.params: a small mapping")
    for k, v in params.items():
        if not isinstance(k, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", k) or AC.FORBIDDEN_KEYS.search(k) or AC.FORBIDDEN_TOKENS & set(k.split("_")):
            raise JobRefused(f"inputs.params.{k!r}: a decision input / secret / command-like name is not allowed")
        if isinstance(v, bool) or not isinstance(v, (int, float, str)) or (isinstance(v, str) and len(v) > 200) or (isinstance(v, float) and v != v):
            raise JobRefused(f"inputs.params.{k}: finite scalars only")
    return {"refs": [dict(r) for r in refs], "params": dict(params)}


class JobQueue:
    def __init__(self, path: str, clock: Callable[[], int] = lambda: int(time.time() * 1000), *, budget_calls_per_day: int = 0, budget_usd_per_day: float = 0.0) -> None:
        self.path, self.clock = path, clock
        self.calls_limit, self.micros_limit = int(budget_calls_per_day), int(round(float(budget_usd_per_day) * 1_000_000))
        os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("pragma journal_mode=wal")
        self._db.execute("pragma synchronous=full")
        self._db.executescript(SCHEMA)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def close(self) -> None:
        self._db.close()

    # ------------------------------------------------------------------ helpers
    def _tx(self):
        class _T:
            def __init__(s, db):
                s.db = db

            def __enter__(s):
                s.db.execute("begin immediate")
                return s.db

            def __exit__(s, et, ev, tb):
                s.db.execute("rollback" if et else "commit")
                return False
        return _T(self._db)

    def _event(self, db: sqlite3.Connection, job_id: str, event: str, detail: str = "") -> None:
        db.execute("insert into events(job_id, at_ms, event, detail) values (?,?,?,?)", (job_id, int(self.clock()), event, detail[:300]))

    def _day(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(int(self.clock()) / 1000.0))

    # ------------------------------------------------------------------ submit (idempotent by dedupe_key)
    def submit(self, role: str, kind: str, inputs: Mapping[str, Any], *, dedupe_key: str, code_version: str, config: Mapping[str, Any], max_attempts: int = 3, timeout_s: int = 600,
               needs_model: bool = False, est_cost_usd: float = 0.0, not_before_ms: int = 0) -> Tuple[str, bool]:
        if role not in AC.ROLES:
            raise JobRefused("role")
        if not (isinstance(kind, str) and re.fullmatch(r"[a-z][a-z0-9_]{1,40}", kind)) or not (isinstance(dedupe_key, str) and re.fullmatch(r"[A-Za-z0-9_.:@-]{1,160}", dedupe_key)):
            raise JobRefused("kind / dedupe_key")
        if not (isinstance(code_version, str) and re.fullmatch(r"[0-9a-f]{7,40}", code_version)):
            raise JobRefused("code_version: the git commit that runs the job")
        if not (1 <= int(max_attempts) <= 10 and 1 <= int(timeout_s) <= 6 * 3600):
            raise JobRefused("max_attempts 1..10, timeout_s 1..21600")
        if needs_model and not (0 <= float(est_cost_usd) <= 50):
            raise JobRefused("est_cost_usd")
        inp = validate_inputs(inputs)
        jid = "job_" + _sha({"k": dedupe_key})[:24]
        now = int(self.clock())
        with self._tx() as db:
            if db.execute("select 1 from jobs where dedupe_key=?", (dedupe_key,)).fetchone():
                return jid, False
            db.execute("insert into jobs(id, dedupe_key, role, kind, inputs_json, inputs_digest, code_version, config_digest, status, max_attempts, timeout_s, not_before_ms, needs_model,"
                       " est_cost_micros, created_ms, updated_ms) values (?,?,?,?,?,?,?,?, 'QUEUED', ?,?,?,?,?,?,?)",
                       (jid, dedupe_key, role, kind, _canon(inp), _sha(inp), code_version, _sha(dict(config)), int(max_attempts), int(timeout_s), int(not_before_ms), int(bool(needs_model)),
                        int(round(float(est_cost_usd) * 1_000_000)) if needs_model else 0, now, now))
            self._event(db, jid, "SUBMITTED", f"{role}/{kind}")
        return jid, True

    # ------------------------------------------------------------------ claim / heartbeat / complete / fail
    def _retry_or_dead(self, db: sqlite3.Connection, row: sqlite3.Row, error: str, now: int) -> str:
        if int(row["attempts"]) >= int(row["max_attempts"]):
            db.execute("update jobs set status='DEAD', lease_owner=null, lease_expires_ms=null, last_error=?, updated_ms=? where id=?", (error[:300], now, row["id"]))
            self._event(db, row["id"], "DEAD", error)
            return "DEAD"
        wait = min(BACKOFF_BASE_S * (2 ** (int(row["attempts"]) - 1)), BACKOFF_MAX_S)
        db.execute("update jobs set status='QUEUED', lease_owner=null, lease_expires_ms=null, not_before_ms=?, last_error=?, updated_ms=? where id=?", (now + wait * 1000, error[:300], now, row["id"]))
        self._event(db, row["id"], "RETRY_SCHEDULED", f"in {wait}s: {error}")
        return "QUEUED"

    def claim(self, worker: str, roles: Optional[Sequence[str]] = None) -> Optional[Dict[str, Any]]:
        if not (isinstance(worker, str) and re.fullmatch(r"[A-Za-z0-9_.:@-]{1,80}", worker)):
            raise JobRefused("worker id")
        now = int(self.clock())
        with self._tx() as db:
            for row in db.execute("select * from jobs where status='RUNNING' and lease_expires_ms <= ?", (now,)).fetchall():     # a hung worker: its attempt is spent
                self._retry_or_dead(db, row, "LEASE_EXPIRED", int(row["lease_expires_ms"]))                                       # backoff counts from the EXPIRY, not from this lazy sweep
            day = self._day()
            b = db.execute("select calls, micros from budget where day=?", (day,)).fetchone()
            calls, micros = (int(b["calls"]), int(b["micros"])) if b else (0, 0)
            q = "select * from jobs where status='QUEUED' and not_before_ms <= ?"
            args: List[Any] = [now]
            if roles:
                q += " and role in (%s)" % ",".join("?" * len(roles))
                args += list(roles)
            for row in db.execute(q + " order by created_ms, id", args).fetchall():
                if row["needs_model"]:
                    if self.calls_limit <= 0 or calls + 1 > self.calls_limit or micros + int(row["est_cost_micros"]) > self.micros_limit:
                        continue                                                              # over budget today: the job waits, deterministic jobs are not affected
                    db.execute("insert into budget(day, calls, micros) values (?,1,?) on conflict(day) do update set calls=calls+1, micros=micros+excluded.micros", (day, int(row["est_cost_micros"])))
                token = int(row["lease_token"]) + 1
                db.execute("update jobs set status='RUNNING', attempts=attempts+1, lease_owner=?, lease_token=?, lease_expires_ms=?, updated_ms=? where id=?",
                           (worker, token, now + int(row["timeout_s"]) * 1000, now, row["id"]))
                self._event(db, row["id"], "CLAIMED", f"{worker} token={token}")
                return {"id": row["id"], "role": row["role"], "kind": row["kind"], "inputs": json.loads(row["inputs_json"]), "inputs_digest": row["inputs_digest"], "code_version": row["code_version"],
                        "config_digest": row["config_digest"], "token": token, "attempt": int(row["attempts"]) + 1, "timeout_s": int(row["timeout_s"])}
        return None

    def _current(self, db: sqlite3.Connection, job_id: str, token: int) -> Optional[sqlite3.Row]:
        r = db.execute("select * from jobs where id=? and status='RUNNING' and lease_token=? and lease_expires_ms > ?", (job_id, int(token), int(self.clock()))).fetchone()
        return r

    def heartbeat(self, job_id: str, token: int) -> bool:
        with self._tx() as db:
            r = self._current(db, job_id, token)
            if r is None:
                return False
            db.execute("update jobs set lease_expires_ms=?, updated_ms=? where id=?", (int(self.clock()) + int(r["timeout_s"]) * 1000, int(self.clock()), job_id))
            return True

    def complete(self, job_id: str, token: int, outputs: Sequence[Mapping[str, Any]]) -> str:
        """SUCCEEDED / QUARANTINED / STALE. Never raises on a bad output: a malformed result is data to quarantine, not a crash."""
        now = int(self.clock())
        with self._tx() as db:
            r = self._current(db, job_id, token)
            if r is None:
                return "STALE"
            try:
                clean = [AC.validate(o) for o in outputs]
                if any(c["role"] != r["role"] for c in clean):
                    raise AC.ProposalRefused("an output names a role other than the job's role")
            except (AC.ProposalRefused, TypeError, KeyError) as exc:
                db.execute("update jobs set status='QUARANTINED', lease_owner=null, lease_expires_ms=null, last_error=?, updated_ms=? where id=?", (("OUTPUT_QUARANTINED:" + str(exc))[:300], now, job_id))
                self._event(db, job_id, "QUARANTINED", str(exc))
                return "QUARANTINED"
            db.execute("update jobs set status='SUCCEEDED', completed_by=lease_owner, lease_owner=null, lease_expires_ms=null, output_json=?, output_digest=?, last_error=null, updated_ms=? where id=?",
                       (_canon(clean), _sha(clean), now, job_id))
            self._event(db, job_id, "SUCCEEDED", f"{len(clean)} proposal(s)")
            return "SUCCEEDED"

    def fail(self, job_id: str, token: int, error: str) -> str:
        now = int(self.clock())
        with self._tx() as db:
            r = self._current(db, job_id, token)
            if r is None:
                return "STALE"
            return self._retry_or_dead(db, r, "FAILED:" + str(error), now)

    # ------------------------------------------------------------------ independent review
    def record_review(self, producer_job: str, review_job: str) -> str:
        """Link a SUCCEEDED challenge job to the output it challenged. Returns the verdict (SURVIVES / FAILS) or a REFUSED:<why> string; never self-approval."""
        with self._tx() as db:
            p = db.execute("select * from jobs where id=?", (producer_job,)).fetchone()
            c = db.execute("select * from jobs where id=?", (review_job,)).fetchone()
            if not p or not c or p["status"] != "SUCCEEDED" or c["status"] != "SUCCEEDED":
                return "REFUSED:both jobs must have SUCCEEDED"
            if c["role"] not in REVIEW_ROLES or p["role"] in REVIEW_ROLES:
                return "REFUSED:the reviewer must be a challenger and the subject must not be"
            if c["completed_by"] == p["completed_by"]:
                return "REFUSED:the reviewer is the producer (no self-approval)"
            digests = {r["sha256"] for r in json.loads(c["inputs_json"])["refs"]}
            if p["output_digest"] not in digests:
                return "REFUSED:the review job did not take the producer's output digest as an input"
            verdicts = [o.get("parameters", {}).get("verdict") for o in json.loads(c["output_json"]) if o["kind"] in ("CHALLENGE", "REJECTION")]
            v = "SURVIVES" if verdicts and all(x == "SURVIVES" for x in verdicts) else "FAILS"     # anything but an explicit, unanimous SURVIVES is a failure
            db.execute("insert or replace into reviews(producer_job, review_job, verdict, at_ms) values (?,?,?,?)", (producer_job, review_job, v, int(self.clock())))
            self._event(db, producer_job, "REVIEWED", f"{review_job}:{v}")
            return v

    def promotable(self, producer_job: str) -> bool:
        """True only if EVERY recorded review of this output says SURVIVES and at least one exists. 'Promotable' = a human or the pre-registration process MAY register it; nothing executes."""
        rows = self._db.execute("select verdict from reviews where producer_job=?", (producer_job,)).fetchall()
        return bool(rows) and all(r["verdict"] == "SURVIVES" for r in rows)

    # ------------------------------------------------------------------ reporting
    def stats(self) -> Dict[str, Any]:
        counts = {s: 0 for s in STATUSES}
        for r in self._db.execute("select status, count(*) n from jobs group by status"):
            counts[r["status"]] = int(r["n"])
        b = self._db.execute("select calls, micros from budget where day=?", (self._day(),)).fetchone()
        return {"jobs": counts, "oldest_queued_ms": (self._db.execute("select min(created_ms) m from jobs where status='QUEUED'").fetchone()["m"]),
                "budget_today": {"calls": int(b["calls"]) if b else 0, "usd": (int(b["micros"]) if b else 0) / 1e6, "calls_limit": self.calls_limit, "usd_limit": self.micros_limit / 1e6}}

    def history(self, job_id: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._db.execute("select at_ms, event, detail from events where job_id=? order by seq", (job_id,))]
