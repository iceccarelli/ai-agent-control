#!/usr/bin/env python3
"""The ``carry-research`` worker core: PUBLIC-DATA RESEARCH ONLY. It schedules and runs deterministic research jobs from the durable queue (``research_jobs``), and turns what they find into typed,
quarantined proposals (``agent_contracts``). It has NO exchange credential, NO order path, NO network access of its own and imports nothing that can reach a venue (a test walks its imports):
data comes from directories the collector already wrote, read through the read-only ``dataset_inventory``.

What runs today (each is deterministic and offline; an external model is never required):
  dataset_inventory   counts / sizes / name-derived coverage for each configured data root -> DATA_GAP proposals for missing days, INFRASTRUCTURE_FINDING when a root is missing or empty
  data_freshness      is the newest day named in each root recent enough? -> DATA_GAP when the corpus has stopped growing

Every job names its inputs and the code version (a digest of the source files that produce the result), so a finding can always be traced to the exact code that made it. The status file
(``status.json``) is what the independent monitor and the owner read: when it last ticked, the queue counts and the freshness of every root.

    python3 tools/research_service.py run --state-dir DIR --root datasets=/path --root backups=/path [--once]
    python3 tools/research_service.py status --state-dir DIR
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

import agent_contracts as AC
import dataset_inventory as DI
import research_jobs as RJ

WORKER_PREFIX = "research"
STALE_AFTER_DAYS = 2
TICK_S = 300
HERE = os.path.dirname(os.path.abspath(__file__))
SOURCES = ("research_service.py", "dataset_inventory.py", "research_jobs.py", "agent_contracts.py")


def code_version() -> str:
    """A 40-hex digest of the source files that produce a research result: provenance that needs no git checkout in the image."""
    h = hashlib.sha256()
    for name in SOURCES:
        with open(os.path.join(HERE, name), "rb") as fh:
            h.update(name.encode() + b"\0" + fh.read())
    return h.hexdigest()[:40]


def _prop(kind: str, role: str, summary: str, rationale: str, now_ms: int, params: Mapping[str, Any], refs: Sequence[str]) -> Dict[str, Any]:
    return {"role": role, "kind": kind, "summary": summary[:290], "rationale": rationale[:1900], "parameters": dict(params), "evidence_refs": list(refs)[:20], "created_ms": int(now_ms)}


# ---------------------------------------------------------------------------------------------------- handlers: (params, roots, now_ms) -> proposals
def handle_dataset_inventory(params: Mapping[str, Any], roots: Mapping[str, str], now_ms: int) -> List[Dict[str, Any]]:
    label = str(params.get("root"))
    path = roots.get(label)
    out: List[Dict[str, Any]] = []
    if not path:
        return [_prop("INFRASTRUCTURE_FINDING", "infrastructure_ops", f"data root {label} is not configured", "the research worker was given no path for this label", now_ms, {"root_label": label}, [])]
    rep = DI.inventory(path)
    if not rep["exists"] or rep["files"] == 0:
        return [_prop("INFRASTRUCTURE_FINDING", "infrastructure_ops", f"data root {label} is missing or empty", "the inventory found no files under the configured path", now_ms,
                      {"root_label": label, "files": rep["files"]}, [f"inventory:{label}"])]
    for name, d in sorted(rep["datasets"].items()):
        cov = d["named_coverage"]
        if cov["missing_days_between"] > 0:
            out.append(_prop("DATA_GAP", "infrastructure_ops", f"{label}/{name}: {cov['missing_days_between']} day(s) missing between {cov['first']} and {cov['last']}",
                             "day tokens in file names skip days inside the covered range; coverage is inferred from names, not from file contents", now_ms,
                             {"root_label": label, "dataset": name[:60], "first_day": cov["first"], "last_day": cov["last"], "missing_days": cov["missing_days_between"],
                              "distinct_days": cov["distinct_days"], "files": d["files"]}, [f"inventory:{label}:{name[:60]}"]))
    out.append(_prop("INFRASTRUCTURE_FINDING", "infrastructure_ops", f"{label}: {rep['files']} files, {rep['bytes']} bytes, {len(rep['datasets'])} dataset(s)",
                     "read-only inventory; no database was opened", now_ms, {"root_label": label, "files": rep["files"], "bytes": rep["bytes"], "datasets": len(rep["datasets"]),
                                                                              "databases_not_opened": len(rep["databases_present_not_opened"])}, [f"inventory:{label}"]))
    return out


def handle_data_freshness(params: Mapping[str, Any], roots: Mapping[str, str], now_ms: int) -> List[Dict[str, Any]]:
    label = str(params.get("root"))
    path = roots.get(label)
    today = dt.datetime.fromtimestamp(now_ms / 1000.0, dt.timezone.utc).date()
    if not path:
        return []
    rep = DI.inventory(path)
    out = []
    for name, d in sorted(rep["datasets"].items()):
        last = d["named_coverage"]["last"]
        if last is None:
            continue
        age = (today - dt.date.fromisoformat(last)).days
        if age > STALE_AFTER_DAYS:
            out.append(_prop("DATA_GAP", "infrastructure_ops", f"{label}/{name}: newest named day is {last} ({age} days old)",
                             "the newest day token in this dataset's file names is older than the freshness threshold, so collection may have stopped", now_ms,
                             {"root_label": label, "dataset": name[:60], "last_day": last, "age_days": age, "stale_after_days": STALE_AFTER_DAYS}, [f"inventory:{label}:{name[:60]}"]))
    return out


HANDLERS: Dict[str, Callable[[Mapping[str, Any], Mapping[str, str], int], List[Dict[str, Any]]]] = {"dataset_inventory": handle_dataset_inventory, "data_freshness": handle_data_freshness}


# ---------------------------------------------------------------------------------------------------- the service
class ResearchService:
    def __init__(self, state_dir: str, roots: Mapping[str, str], clock: Callable[[], int] = lambda: int(time.time() * 1000), worker: str = "research-1") -> None:
        self.dir, self.roots, self.clock, self.worker = state_dir, dict(roots), clock, f"{WORKER_PREFIX}-{worker}"[:80]
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        self.q = RJ.JobQueue(os.path.join(state_dir, "jobs.sqlite"), clock)
        self.quarantine = os.path.join(state_dir, "proposals")
        self.version = code_version()

    def schedule(self) -> int:
        """Idempotent: one inventory and one freshness job per root per UTC hour (the dedupe key makes a restart or a second tick a no-op)."""
        now = int(self.clock())
        hour = time.strftime("%Y%m%dT%H", time.gmtime(now / 1000.0))
        made = 0
        for label, path in sorted(self.roots.items()):
            ref = [{"name": f"root:{label}", "sha256": hashlib.sha256(os.path.abspath(path).encode()).hexdigest()}]
            for kind in ("dataset_inventory", "data_freshness"):
                _jid, new = self.q.submit("infrastructure_ops", kind, {"refs": ref, "params": {"root": label}}, dedupe_key=f"{kind}:{label}:{hour}", code_version=self.version,
                                          config={"stale_after_days": STALE_AFTER_DAYS}, max_attempts=3, timeout_s=900)
                made += int(new)
        return made

    def work(self, max_jobs: int = 20) -> Dict[str, int]:
        done = {"SUCCEEDED": 0, "QUARANTINED": 0, "FAILED": 0, "STALE": 0}
        for _ in range(max_jobs):
            job = self.q.claim(self.worker, roles=["infrastructure_ops"])
            if job is None:
                break
            h = HANDLERS.get(job["kind"])
            if h is None:
                self.q.fail(job["id"], job["token"], "NO_HANDLER:" + job["kind"])
                done["FAILED"] += 1
                continue
            try:
                props = h(job["inputs"]["params"], self.roots, int(self.clock()))
            except Exception as exc:                                    # noqa: BLE001 - a crashing handler is a failed attempt, never a dead service
                self.q.fail(job["id"], job["token"], f"HANDLER_ERROR:{type(exc).__name__}")
                done["FAILED"] += 1
                continue
            res = self.q.complete(job["id"], job["token"], props)
            done[res] = done.get(res, 0) + 1
            if res == "SUCCEEDED":
                for p in props:
                    try:
                        AC.submit(self.quarantine, p)                    # DATA, never executed: a human or the pre-registration process reads it
                    except AC.ProposalRefused:
                        pass
        return done

    def status(self) -> Dict[str, Any]:
        roots = {}
        for label, path in sorted(self.roots.items()):
            rep = DI.inventory(path)
            newest = max((d["named_coverage"]["last"] for d in rep["datasets"].values() if d["named_coverage"]["last"]), default=None)
            roots[label] = {"exists": rep["exists"], "files": rep["files"], "bytes": rep["bytes"], "newest_named_day": newest}
        st = {"schema": "research_status.v1", "at_ms": int(self.clock()), "worker": self.worker, "code_version": self.version, "queue": self.q.stats(), "roots": roots,
              "capabilities": {"orders": False, "exchange_credentials": False, "network": False}}
        tmp = os.path.join(self.dir, ".status.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh, sort_keys=True, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, os.path.join(self.dir, "status.json"))
        return st

    def tick(self) -> Dict[str, Any]:
        made = self.schedule()
        done = self.work()
        return {"scheduled": made, "worked": done, "status": self.status()}


def main(argv: Optional[Sequence[str]] = None, *, sleep: Callable[[float], None] = time.sleep) -> int:
    ap = argparse.ArgumentParser(description="carry-research worker core (public data only; no credential, no order)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "status"):
        p = sub.add_parser(name)
        p.add_argument("--state-dir", required=True)
        p.add_argument("--root", action="append", default=[], help="LABEL=PATH of a data directory to inspect (read-only)")
        if name == "run":
            p.add_argument("--once", action="store_true")
            p.add_argument("--tick-seconds", type=int, default=TICK_S)
    a = ap.parse_args(list(argv) if argv is not None else None)
    roots = {}
    for r in a.root:
        label, _, path = r.partition("=")
        if not label or not path:
            print("refused: --root LABEL=PATH")
            return 2
        roots[label] = path
    svc = ResearchService(a.state_dir, roots)
    if a.cmd == "status":
        print(json.dumps(svc.status(), indent=1, sort_keys=True))
        return 0
    while True:
        res = svc.tick()
        print(json.dumps({"scheduled": res["scheduled"], "worked": res["worked"]}, sort_keys=True), flush=True)
        if a.once:
            return 0
        sleep(a.tick_seconds)


if __name__ == "__main__":
    sys.exit(main())
