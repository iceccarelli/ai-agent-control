"""Read-only audit of a PRIVATE market-data root (the corpus written by ``market_collector`` and ``market_history``): is the data there, fresh, complete, backed up and
restorable, and does it cover the research universe? Nothing here fetches, signs, writes or repairs.

NOT the carry-watch workflow. ``.github/workflows`` carry-watch concerns the Fly monitor's history (an artifact of the paper monitor), not this corpus; the owner's market
data lives outside Git and CI never sees it. This tool is what answers "is MY collected data healthy?" and it is run by the owner on the machine that holds the root. It also
does not touch the ``data_freshness`` pytest marker or its exclusion in CI (those tests concern the committed paper corpora).

Three separate axes in the output (they are never merged into one green light):
  code_correctness    NOT ASSESSED HERE. Whether the code is right is the CI gate's question; a healthy corpus says nothing about it and vice versa.
  data_freshness      NO_DATA | STALE | PARTIAL | HEALTHY.
                      NO_DATA   there is no data at all (no COMPLETE cycle, no history dataset): NEVER HEALTHY, whatever else is true.
                      STALE     data exists but a cycle stream or a dataset/symbol/field is older than its expected cadence allows.
                      PARTIAL   data exists and is not stale but something is missing or unproved: a gap, a dataset that is not COMPLETE or does not re-hash, a status partition
                                the universe capture never took (Closed, PreLaunch), a failed or never-run backup, a failed or never-run restore-check, a skipped verification.
                      HEALTHY   real, complete, fresh data AND a backup, a restore-check and a seal that were VERIFIED HERE (``--backup-dir --receipts --seal-manifest``: the
                                restore-check and the seal check are run live) AND young enough (DEFAULT_OPS_MAX_AGE_MS, tightened by the cadence the collector records; or
                                ``--ops-max-age-ms``) AND every dataset re-hashes AND every history window ended within the history max age (DEFAULT 3 days, or
                                ``--history-max-age-ms``; a window that ended long ago can never be HEALTHY). ``health.json`` is UNAUTHENTICATED (anyone with the directory
                                can write it): an ok record in it that was not confirmed by a live check is reported as ``*_CLAIMED_NOT_VERIFIED`` (PARTIAL) and can never make
                                HEALTHY; a failure it admits to is believed (that direction is safe). Anything else is not HEALTHY.
  readiness_vocabulary  (additive; the verdicts above are unchanged) three SEPARATE claims: data_ready = every history dataset COMPLETE, re-hashed here and not stale (a property of
                      files; NO_DATA is never DATA_READY); research_ready = RESEARCH_READY | NOT_RESEARCH_READY | NOT_ASSESSED, decided ONLY by re-deriving a sealed
                      ``tools/backfill_plan.py`` plan (``--plan``) against this root (same universe capture, same symbol set, required window COMPLETE and verified); without
                      ``--plan`` it is NOT_ASSESSED, never inferred; strategy_ready = always NOT_ASSESSED. Each history section also reports the symbols, dates and
                      intervals it covers and does not cover (``coverage``).
  research_readiness  NO_DATA | NOT_READY | READY. READY needs every declared research-universe symbol COMPLETE in every history dataset present, the status partitions
                      present, and no dataset UNVERIFIED/CONFLICTED. It is independent of backup state and of freshness.
Exit code: 0 only for HEALTHY; 1 PARTIAL; 3 STALE; 4 NO_DATA; 2 refusal (root inside Git, not a directory). ``--require-ready`` makes a non-READY readiness a non-zero exit too.

Latest observed timestamp is reported per dataset / symbol / field: history datasets (last row of each symbol), cycle fields (tickers, book, trades per symbol, the receive time of
the newest artifact in the newest cycles). Expected cadence is the dataset's declared interval, or the collector's interval (health report, else the median cycle spacing).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import backfill_plan as BP  # noqa: E402
import evidence_chain as EC  # noqa: E402
import market_collector as MC  # noqa: E402
import market_history as MH  # noqa: E402

SCHEMA = "data_freshness_audit/v1"
STALE_FACTOR = 3                                    # newer than 3 expected intervals is fresh (cycles); history uses MH.STALE_INTERVALS past the window end
REQUIRED_STATUS_PARTITIONS = ("Trading", "Closed", "PreLaunch")
MAX_CYCLE_MANIFESTS = 200
DAY_MS = 86_400_000
DEFAULT_HISTORY_MAX_AGE_MS = 3 * DAY_MS               # a history window that ended longer ago than this is STALE unless the owner passes --history-max-age-ms
DEFAULT_OPS_MAX_AGE_MS = 3 * DAY_MS                   # newest seal / backup (receipt) / restore-check older than this is STALE; the collector's recorded cadence x OPS_CADENCE_FACTOR can only TIGHTEN it
OPS_CADENCE_FACTOR = 3
FUTURE_SKEW_MS = 5 * 60_000                           # data stamped later than now + this is DATA_FROM_THE_FUTURE (a wrong audit clock, a skewed host or a forged row): PARTIAL at best, never HEALTHY
EXIT = {"HEALTHY": 0, "PARTIAL": 1, "STALE": 3, "NO_DATA": 4}


def _load_json(path: str) -> Optional[Any]:
    try:
        with open(path, "rb") as fh:
            return json.loads(fh.read())
    except (OSError, ValueError):
        return None


def load_research_universe(path: str) -> List[str]:
    """A research-universe file: a JSON list, a JSON object with ``symbols``, or one symbol per line (# comments allowed)."""
    with open(path, "rb") as fh:
        raw = fh.read().decode("utf-8", "strict")
    try:
        doc = json.loads(raw)
        syms = doc.get("symbols") if isinstance(doc, dict) else doc
    except ValueError:
        syms = [ln.split("#")[0].strip() for ln in raw.splitlines()]
    out = sorted({s for s in syms if isinstance(s, str) and s})
    bad = [s for s in out if not MC._SYMBOL.fullmatch(s)]
    if bad or not out:
        raise ValueError(f"research universe file: no symbols or invalid symbols {bad[:3]}")
    return out


def _future(issues: List[Dict[str, Any]], now: int, what: str, t: Any) -> bool:
    """Record DATA_FROM_THE_FUTURE (PARTIAL) when ``t`` is later than ``now`` + FUTURE_SKEW_MS. A negative age is not freshness: it is evidence the clock or the data is wrong."""
    if isinstance(t, int) and not isinstance(t, bool) and t > now + FUTURE_SKEW_MS:
        issues.append({"axis": "data_freshness", "level": "PARTIAL", "code": "DATA_FROM_THE_FUTURE", "detail": f"{what} is {(t - now) // 1000}s after the audit clock (tolerance {FUTURE_SKEW_MS // 1000}s)"})
        return True
    return False


def _cycle_section(root: str, now: int, health: Optional[Mapping[str, Any]], issues: List[Dict[str, Any]]) -> Dict[str, Any]:
    log = os.path.join(root, MC.LOG_NAME)
    sec: Dict[str, Any] = {"present": os.path.isfile(log), "latest_complete_cycle": None, "cycles": 0, "fields": {}, "gaps": [], "expected_interval_ms": None, "stale": False}
    if not sec["present"]:
        return sec
    ver = EC.verify(log)
    if not ver.get("ok", False):
        issues.append({"axis": "data_freshness", "level": "PARTIAL", "code": "CYCLE_LOG_DOES_NOT_VERIFY", "detail": f"{MC.LOG_NAME} chain fails"})
    rows = [r for r in MC._log_rows(log) if isinstance(r, dict) and isinstance(r.get("cycle_ms"), int)]
    sec["cycles"] = len(rows)
    complete = [r for r in rows if os.path.isfile(os.path.join(root, str(r.get("cycle")), MC.COMPLETE))]
    if not complete:
        return sec
    complete.sort(key=lambda r: r["cycle_ms"])
    last = complete[-1]
    sec["latest_complete_cycle"] = {"cycle": last["cycle"], "cycle_ms": last["cycle_ms"], "status": last.get("status"), "age_ms": now - last["cycle_ms"]}
    _future(issues, now, f"cycle {last['cycle']} cycle_ms", last["cycle_ms"])
    interval = (health or {}).get("interval_ms")
    if not isinstance(interval, int) or interval <= 0:
        spacing = sorted(b["cycle_ms"] - a["cycle_ms"] for a, b in zip(complete, complete[1:]))
        interval = spacing[len(spacing) // 2] if spacing else None
    sec["expected_interval_ms"] = interval
    if interval and now - last["cycle_ms"] > STALE_FACTOR * interval:
        sec["stale"] = True
    if last.get("status") != "OK":
        issues.append({"axis": "data_freshness", "level": "PARTIAL", "code": "LATEST_CYCLE_NOT_OK", "detail": f"{last['cycle']} is {last.get('status')}"})
    for r in complete:
        for g in r.get("gaps") or []:
            sec["gaps"].append({"cycle": r["cycle"], "missed_expected_cycles": g.get("missed_expected_cycles"), "gap_ms": g.get("gap_ms")})
    if sec["gaps"]:
        issues.append({"axis": "data_freshness", "level": "PARTIAL", "code": "CYCLE_GAPS", "detail": f"{len(sec['gaps'])} recorded gap(s) between cycles (never filled)"})
    # newest observation per cycle field and symbol, from the newest manifests
    newest: Dict[str, int] = {}
    for r in complete[-MAX_CYCLE_MANIFESTS:]:
        man = _load_json(os.path.join(root, r["cycle"], "manifest.json"))
        if not isinstance(man, dict):
            continue
        for a in man.get("artifacts", []):
            if isinstance(a, dict) and isinstance(a.get("recv_ms"), int) and a.get("kind") in ("tickers", "book", "trades"):
                k = f"{a['kind']}:{a.get('symbol', '*')}"
                newest[k] = max(newest.get(k, 0), a["recv_ms"])
    sec["fields"] = {k: {"latest_ms": v, "age_ms": now - v} for k, v in sorted(newest.items())}
    fut = sorted(k for k, v in newest.items() if v > now + FUTURE_SKEW_MS)
    if fut:
        _future(issues, now, f"cycle field(s) {fut[:5]}", max(newest[k] for k in fut))
    # a symbol/field that the newest cycle no longer has is stale relative to the stream
    if interval:
        stale_fields = sorted(k for k, v in newest.items() if now - v > STALE_FACTOR * interval)
        sec["stale_fields"] = stale_fields
        if stale_fields and not sec["stale"]:
            sec["stale_symbols_or_fields"] = len(stale_fields)
    return sec


def _history_sections(root: str, now: int, verify: bool, issues: List[Dict[str, Any]], universe: Optional[Sequence[str]], history_max_age_ms: Optional[int] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        mp = os.path.join(d, "manifest.json")
        if not os.path.isdir(d) or os.path.islink(d) or name.startswith("cycle-") or not os.path.isfile(mp):
            continue
        man = _load_json(mp)
        if not isinstance(man, dict) or man.get("schema") != MH.SCHEMA or not isinstance(man.get("symbols"), dict):
            continue
        sec: Dict[str, Any] = {"dataset_status": man.get("dataset_status"), "symbols": {}, "stale_symbols": [], "incomplete_symbols": [], "verify": "SKIPPED"}
        for sym, rec in sorted(man["symbols"].items()):
            cov = rec.get("coverage") or {}
            last = cov.get("last_ts")
            iv = cov.get("observed_interval_ms") or cov.get("interval_ms")
            delivery = cov.get("delivery_ms")
            entry = {"status": rec.get("status"), "first_ts": cov.get("first_ts"), "latest_observed_ts": last, "expected_cadence_ms": iv, "rows": cov.get("rows"), "gap_count": rec.get("gap_count"),
                     "age_ms": (now - last) if isinstance(last, int) else None}
            sec["symbols"][sym] = entry
            if _future(issues, now, f"{name}/{sym} last_ts", last):
                entry["from_the_future"] = True
            if rec.get("status") != "COMPLETE":
                sec["incomplete_symbols"].append(sym)
            if isinstance(last, int) and iv and not delivery and now - last > MH.STALE_INTERVALS * iv + (now - (man.get("request") or {}).get("end_ms", now)):
                sec["stale_symbols"].append(sym)               # data older than its cadence allows, relative to the requested window end
        # A backfill is judged against ITS OWN requested window end for per-symbol lag, AND its window end must be recent (history max age, default 3 days): a window that ended
        # long ago is STALE, never HEALTHY. A continuing feed is judged by the cycle section.
        wend = (man.get("request") or {}).get("end_ms")
        max_age = DEFAULT_HISTORY_MAX_AGE_MS if history_max_age_ms is None else history_max_age_ms
        _future(issues, now, f"{name} window end", wend)
        if not isinstance(wend, int):
            sec["stale_symbols"] = sorted(man["symbols"])                      # a dataset that does not say when its window ended cannot be shown fresh
        elif now - wend > max_age:
            sec["stale_symbols"] = sorted(man["symbols"])
            sec["window_age_ms"] = now - wend
        if verify:
            v = MH.verify_dataset(d)
            sec["verify"] = "OK" if v["ok"] else "FAILED"
            if not v["ok"]:
                sec["verify_problems"] = v["problems"][:10]
                issues.append({"axis": "data_freshness", "level": "PARTIAL", "code": "DATASET_DOES_NOT_VERIFY", "detail": f"{name}: {v['problems'][0]}"})
        else:
            issues.append({"axis": "data_freshness", "level": "PARTIAL", "code": "VERIFICATION_SKIPPED", "detail": name})
        if man.get("dataset_status") != "COMPLETE":
            issues.append({"axis": "data_freshness", "level": "PARTIAL", "code": f"DATASET_{man.get('dataset_status')}", "detail": f"{name}: {len(sec['incomplete_symbols'])} symbol(s) not COMPLETE"})
        if universe is not None:
            have = {s for s, r in sec["symbols"].items() if r["status"] == "COMPLETE"}
            sec["research_universe"] = {"declared": len(universe), "complete": len(have & set(universe)), "missing": sorted(set(universe) - have)[:200],
                                        "missing_count": len(set(universe) - have)}
        sec["coverage"] = _dataset_coverage(sec)
        out[name] = sec
    return out


def _dataset_coverage(sec: Mapping[str, Any]) -> Dict[str, Any]:
    """What one history dataset covers and what it does not: symbols, dates, intervals. Pure over the section built from its manifest."""
    syms = sec["symbols"]
    firsts = [r["first_ts"] for r in syms.values() if isinstance(r.get("first_ts"), int)]
    lasts = [r["latest_observed_ts"] for r in syms.values() if isinstance(r.get("latest_observed_ts"), int)]
    ivs: Dict[str, int] = {}
    for r in syms.values():
        k = str(r.get("expected_cadence_ms"))
        ivs[k] = ivs.get(k, 0) + 1
    notc = sorted(s for s, r in syms.items() if r.get("status") != "COMPLETE")
    return {"symbols_total": len(syms), "symbols_complete": len(syms) - len(notc), "symbols_not_complete": notc[:200], "symbols_not_complete_count": len(notc),
            "first_ts_min": min(firsts) if firsts else None, "last_ts_max": max(lasts) if lasts else None, "intervals_ms": dict(sorted(ivs.items())),
            "symbols_without_rows": sorted(s for s, r in syms.items() if not r.get("rows"))[:200]}


def _readiness_vocabulary(history: Mapping[str, Any], has_rows: bool, plan_assessment: Mapping[str, Any]) -> Dict[str, Any]:
    """Three separate claims, never merged: DATA_READY (these datasets are whole, re-hash and are not stale: a property of FILES), RESEARCH_READY (a backfill_plan, re-derived here,
    says the required window and symbol set are verified COMPLETE: a property of COVERAGE) and strategy readiness (never assessed here)."""
    per: Dict[str, Any] = {}
    for name, h in history.items():
        ok = h.get("dataset_status") == "COMPLETE" and h["verify"] == "OK" and not h["stale_symbols"]
        per[name] = {"data_ready": ok, "dataset_status": h.get("dataset_status"), "verify": h["verify"], "stale_symbols": len(h["stale_symbols"]), "incomplete_symbols": len(h["incomplete_symbols"])}
    if not has_rows or not history:
        data = "NO_DATA"
    else:
        data = "DATA_READY" if all(p["data_ready"] for p in per.values()) else "NOT_DATA_READY"
    return {"data_ready": {"verdict": data, "per_dataset": per,
                           "note": "dataset-level only: every history dataset COMPLETE, re-hashed here and not stale. Backups and the cycle stream are the data_freshness axis, not this one. NO_DATA is never DATA_READY."},
            "research_ready": plan_assessment,
            "strategy_ready": {"verdict": "NOT_ASSESSED", "note": "no strategy-level readiness is claimed by this tool; a READY on the research_readiness axis is declared-universe COVERAGE only"}}


def _plan_assessment(plan_file: Optional[str], root: str) -> Dict[str, Any]:
    """RESEARCH_READY only from a freshly re-derived ``backfill_plan``: the sealed plan file is loaded (its digest must hold), re-run against THIS root, and must agree on the universe
    capture and the symbol set. A plan file's own readiness claim is never read. No plan = NOT_ASSESSED, never inferred."""
    if not plan_file:
        return {"verdict": "NOT_ASSESSED", "reason": "no --plan given: coverage of the required window and symbol set is not judged without a backfill_plan"}
    try:
        doc = MH.load_plan(plan_file)
        fresh = BP.replan(doc, root)
    except Exception as exc:                          # noqa: BLE001 - an unreadable plan / vanished universe is a stated non-answer, not a traceback
        return {"verdict": "NOT_ASSESSED", "reason": f"the plan could not be re-derived: {type(exc).__name__}: {str(exc)[:200]}"}
    ok = bool(fresh["readiness"]["research_ready"]) and fresh["same_universe_as_sealed_plan"] and fresh["same_symbol_set_as_sealed_plan"]
    return {"verdict": "RESEARCH_READY" if ok else "NOT_RESEARCH_READY", "plan_digest": doc["plan_digest"], "window_days": fresh["window"]["effective_days"], "eligible_symbols": fresh["eligible_count"],
            "unmet": fresh["readiness"]["unmet"], "blockers": fresh["blockers"], "same_universe_as_sealed_plan": fresh["same_universe_as_sealed_plan"],
            "same_symbol_set_as_sealed_plan": fresh["same_symbol_set_as_sealed_plan"], "basis": "backfill_plan re-derived now from the sealed plan's parameters against this root"}


def _status_partitions(root: str, explicit: Optional[str], issues: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Which instrument status partitions the universe captures took. A capture that records no partitions (schema v1: instruments-info without a status argument serves
    Trading only) cannot show that Closed or PreLaunch contracts were captured, so they are MISSING."""
    dirs: List[str] = [explicit] if explicit else []
    if not explicit:
        for name in sorted(os.listdir(root)):
            u = os.path.join(root, name, "universe")
            if name.startswith("cycle-") and os.path.isfile(os.path.join(u, "manifest.json")):
                dirs = [u]                                  # the newest cycle's capture
    res: Dict[str, Any] = {"captures_examined": len(dirs), "present": [], "missing": list(REQUIRED_STATUS_PARTITIONS), "schema": None}
    if not dirs:
        return res
    man = _load_json(os.path.join(dirs[-1], "manifest.json")) or {}
    res["schema"] = man.get("schema") or man.get("schema_version")
    cov = man.get("status_coverage")                 # schema 2: {status: outcome}; a partition counts only when the venue ANSWERED (OBSERVED_ROWS / OBSERVED_EMPTY)
    present = set()
    if isinstance(cov, dict):
        present |= {k for k, v in cov.items() if isinstance(v, str) and v.startswith("OBSERVED")}
        res["outcomes"] = dict(sorted((k, v) for k, v in cov.items() if isinstance(v, str)))
    res["present"] = sorted(present)
    res["missing"] = [s for s in REQUIRED_STATUS_PARTITIONS if s not in present]
    res["delisting_observability"] = man.get("delisting_observability", "NOT_RECORDED")
    if res["missing"]:
        issues.append({"axis": "research_readiness", "level": "NOT_READY", "code": "MISSING_STATUS_PARTITIONS", "detail": f"universe capture lacks {res['missing']}"})
    return res


def _safe(fn: Callable[..., Dict[str, Any]], *args: Any) -> Dict[str, Any]:
    """A live check over damaged input is a FAILED check with a reason, never a traceback."""
    try:
        return fn(*args)
    except Exception as exc:                          # noqa: BLE001 - damaged evidence must be reported, whatever shape the damage takes
        return {"ok": False, "problems": [f"check could not complete: {type(exc).__name__}: {str(exc)[:200]}"], "checked": None}


def _newest_at_ms(rows: Sequence[Any]) -> Optional[int]:
    ts = [r.get("at_ms") for r in rows if isinstance(r, dict) and isinstance(r.get("at_ms"), int) and not isinstance(r.get("at_ms"), bool)]
    return max(ts) if ts else None


def _ops_limit(health: Optional[Mapping[str, Any]], key: str, override: Optional[int]) -> int:
    """Oldest acceptable age of a seal / backup / restore-check. An explicit value wins. Else DEFAULT_OPS_MAX_AGE_MS, TIGHTENED (never loosened: health.json is unauthenticated)
    by OPS_CADENCE_FACTOR x the cadence the collector recorded for that hook."""
    if override is not None:
        return override
    cad = ((health or {}).get("hook_cadence_ms") or {}).get(key)
    if isinstance(cad, int) and not isinstance(cad, bool) and cad > 0:
        return min(DEFAULT_OPS_MAX_AGE_MS, OPS_CADENCE_FACTOR * cad)
    return DEFAULT_OPS_MAX_AGE_MS


def _backup_section(root: str, health: Optional[Mapping[str, Any]], live: Mapping[str, Optional[str]], issues: List[Dict[str, Any]], now: int = 0,
                    ops_max_age_ms: Optional[int] = None) -> Dict[str, Any]:
    sec: Dict[str, Any] = {"backup": None, "restore_check": None, "seal": None, "source": "health_report", "stale": False}
    for key, name in (("last_backup", "backup"), ("last_restore_check", "restore_check"), ("last_seal", "seal")):
        rec = (health or {}).get(key)
        sec[name] = dict(rec, verified=False) if isinstance(rec, dict) else None
    at: Dict[str, Optional[int]] = {k: (v or {}).get("at_ms") if isinstance(v, dict) else None for k, v in (("backup", sec["backup"]), ("restore_check", sec["restore_check"]), ("seal", sec["seal"]))}
    if live.get("backup_dir") and live.get("receipts") and live.get("seal_manifest"):
        rc = _safe(MC.restore_check, live["backup_dir"], live["receipts"], live["seal_manifest"])
        ck = _safe(MC.check_cycles, root, live["seal_manifest"])
        try:
            receipt_at = _newest_at_ms(MC._log_rows(live["receipts"]))
            seal_at = _newest_at_ms(MC._log_rows(live["seal_manifest"]))
        except Exception:                                                       # noqa: BLE001
            receipt_at = seal_at = None
        sec["restore_check"] = {"ok": bool(rc["ok"]), "live": True, "verified": True, "at_ms": now, "detail": {"problems": rc.get("problems", [])[:5], "checked": rc.get("checked")}}
        sec["seal"] = {"ok": bool(ck["ok"]), "live": True, "verified": True, "at_ms": seal_at, "detail": {"problems": ck.get("problems", [])[:5]}}
        sec["backup"] = {"ok": bool(rc["ok"]) and receipt_at is not None, "live": True, "verified": True, "at_ms": receipt_at,
                         "detail": "every sealed cycle has a receipt that re-hashes (restore-check); at_ms is the newest receipt" if rc["ok"] and receipt_at is not None
                         else {"problems": rc.get("problems", [])[:5] or ["no receipt on record"]}}
        at = {"backup": receipt_at, "restore_check": now, "seal": seal_at}
        sec["source"] = "live_check"
    for name, label, key in (("backup", "BACKUP", "last_backup"), ("restore_check", "RESTORE_CHECK", "last_restore_check"), ("seal", "SEAL", "last_seal")):
        rec = sec[name]
        if rec is None:
            issues.append({"axis": "data_freshness", "level": "PARTIAL", "code": f"{label}_NEVER_RAN", "detail": f"no {name.replace('_', '-')} on record: nothing proves the data can be restored"})
            continue
        if not rec.get("ok"):
            issues.append({"axis": "data_freshness", "level": "PARTIAL", "code": f"{label}_FAILED", "detail": json.dumps(rec.get("detail"), default=str)[:300]})
            continue
        if not rec.get("verified"):                                              # an ok record nothing here confirmed: health.json is unauthenticated
            issues.append({"axis": "data_freshness", "level": "PARTIAL", "code": f"{label}_CLAIMED_NOT_VERIFIED",
                           "detail": f"{MC.HEALTH_NAME} claims an ok {name.replace('_', '-')} but no live check was run (pass --backup-dir --receipts --seal-manifest); a claim is not evidence"})
        t = at[name]
        if not isinstance(t, int) or isinstance(t, bool) or t < 0 or t > now + FUTURE_SKEW_MS:
            issues.append({"axis": "data_freshness", "level": "PARTIAL", "code": f"{label}_BAD_TIMESTAMP", "detail": f"the {name.replace('_', '-')} record has no usable past timestamp ({t!r})"})
            continue
        limit = _ops_limit(health, key, ops_max_age_ms)
        if now - t > limit:
            sec["stale"] = True
            issues.append({"axis": "data_freshness", "level": "STALE", "code": f"{label}_STALE", "detail": f"newest {name.replace('_', '-')} is {(now - t) // 1000}s old; the limit is {limit // 1000}s"})
    return sec


def audit(root: str, *, now_ms: Optional[int] = None, universe_file: Optional[str] = None, universe_capture: Optional[str] = None, verify: bool = True, history_max_age_ms: Optional[int] = None,
          backup_dir: Optional[str] = None, receipts: Optional[str] = None, seal_manifest: Optional[str] = None, ops_max_age_ms: Optional[int] = None,
          plan_file: Optional[str] = None, clock: Callable[[], int] = lambda: int(time.time() * 1000)) -> Dict[str, Any]:
    now = int(clock()) if now_ms is None else now_ms
    root = os.path.abspath(root)
    problems = MC.root_guard(root, 0)
    if not os.path.isdir(root):
        raise ValueError(f"{root} is not a directory")
    if any("git work tree" in p or "symbolic link" in p for p in problems):
        raise ValueError("refusing to audit a data root inside Git or behind a link: " + "; ".join(problems))
    issues: List[Dict[str, Any]] = []
    for p in problems:
        issues.append({"axis": "data_freshness", "level": "PARTIAL", "code": "ROOT_PERMISSIONS", "detail": p})
    health = _load_json(os.path.join(root, MC.HEALTH_NAME))
    health = health if isinstance(health, dict) else None
    universe = load_research_universe(universe_file) if universe_file else None
    cycles = _cycle_section(root, now, health, issues)
    history = _history_sections(root, now, verify, issues, universe, history_max_age_ms)
    parts = _status_partitions(root, universe_capture, issues)
    backups = _backup_section(root, health, {"backup_dir": backup_dir, "receipts": receipts, "seal_manifest": seal_manifest}, issues, now, ops_max_age_ms)
    if health:
        if health.get("status") == "REFUSED":
            issues.append({"axis": "data_freshness", "level": "PARTIAL", "code": "COLLECTOR_REFUSED", "detail": json.dumps(health.get("refusal"), default=str)[:200]})
        if int(health.get("consecutive_failures") or 0) > 0:
            issues.append({"axis": "data_freshness", "level": "PARTIAL", "code": "COLLECTOR_FAILING", "detail": f"{health['consecutive_failures']} consecutive failure(s)"})
    has_data = cycles["latest_complete_cycle"] is not None or any(h["symbols"] for h in history.values()) or any(h.get("dataset_status") for h in history.values())
    has_rows = cycles["latest_complete_cycle"] is not None or any((s.get("rows") or 0) > 0 for h in history.values() for s in h["symbols"].values())
    stale = bool(cycles["stale"] or cycles.get("stale_fields") or backups["stale"]) or any(h["stale_symbols"] or h.get("dataset_status") == "STALE" for h in history.values())
    if not has_data or not has_rows:
        freshness = "NO_DATA"
    elif stale:
        freshness = "STALE"
    elif any(i["axis"] == "data_freshness" for i in issues):
        freshness = "PARTIAL"
    else:
        freshness = "HEALTHY"
    if freshness == "STALE":
        issues.append({"axis": "data_freshness", "level": "STALE", "code": "STALE_DATA", "detail": "a cycle stream or a dataset/symbol/field is older than its cadence allows"})
    # research readiness
    ready_issues = [i for i in issues if i["axis"] == "research_readiness"]
    if not has_rows:
        readiness = "NO_DATA"
    else:
        reasons = list(ready_issues)
        for name, h in history.items():
            if h.get("dataset_status") in ("UNVERIFIED", "CONFLICTED") or h["verify"] == "FAILED":
                reasons.append({"code": "DATASET_UNTRUSTED", "detail": f"{name}: {h.get('dataset_status')} verify={h['verify']}"})
            ru = h.get("research_universe")
            if ru and ru["missing_count"]:
                reasons.append({"code": "UNIVERSE_NOT_COVERED", "detail": f"{name}: {ru['missing_count']} of {ru['declared']} declared symbols are not COMPLETE"})
        if universe is None:
            reasons.append({"code": "NO_RESEARCH_UNIVERSE_DECLARED", "detail": "pass --research-universe FILE: without it coverage cannot be judged"})
        if not history:
            reasons.append({"code": "NO_HISTORY_DATASETS", "detail": "no market_history dataset in the root"})
        readiness = "READY" if not reasons else "NOT_READY"
        ready_issues = reasons
    vocab = _readiness_vocabulary(history, has_rows, _plan_assessment(plan_file, root))
    return {"schema": SCHEMA, "root": root, "now_ms": now, "readiness_vocabulary": vocab,
            "axes": {"code_correctness": {"verdict": "NOT_ASSESSED", "note": "not assessed by this tool; see the CI correctness gate"},
                     "data_freshness": {"verdict": freshness, "issues": [i for i in issues if i["axis"] == "data_freshness" or i.get("level") == "STALE"]},
                     "research_readiness": {"verdict": readiness, "issues": ready_issues, "scope": "coverage of a declared symbol list by COMPLETE history datasets; NOT strategy readiness (see readiness_vocabulary)"}},
            "cycles": cycles, "history": history, "status_partitions": parts, "backups": backups, "collector_health": health,
            "note": "read-only; HEALTHY needs real complete fresh data AND a passing backup and restore-check; NO_DATA is never healthy"}


def main(argv: Optional[Sequence[str]] = None, *, clock: Callable[[], int] = lambda: int(time.time() * 1000)) -> int:
    ap = argparse.ArgumentParser(description="Read-only audit of a private market-data root (not the Fly carry-watch).")
    ap.add_argument("--root", required=True)
    ap.add_argument("--research-universe", help="JSON list / {symbols:[...]} / one symbol per line")
    ap.add_argument("--universe-capture", help="universe_capture directory to read status partitions from (default: the newest cycle's)")
    ap.add_argument("--backup-dir"); ap.add_argument("--receipts"); ap.add_argument("--seal-manifest")
    ap.add_argument("--skip-verify", action="store_true", help="do not re-hash history datasets (HEALTHY is then impossible)")
    ap.add_argument("--now-ms", type=int)
    ap.add_argument("--history-max-age-ms", type=int, help="a history dataset whose requested window ended longer ago than this is STALE (default: 3 days)")
    ap.add_argument("--ops-max-age-ms", type=int, help="the newest seal / backup receipt / restore-check older than this is STALE (default: 3 days, tightened by the collector's recorded cadence)")
    ap.add_argument("--plan", help="a sealed backfill_plan --out file: RESEARCH_READY is judged by re-deriving that plan against --root (never from the file's own claim)")
    ap.add_argument("--require-ready", action="store_true")
    a = ap.parse_args(argv)
    try:
        rep = audit(a.root, now_ms=a.now_ms, universe_file=a.research_universe, universe_capture=a.universe_capture, verify=not a.skip_verify, history_max_age_ms=a.history_max_age_ms, ops_max_age_ms=a.ops_max_age_ms, backup_dir=a.backup_dir,
                    receipts=a.receipts, seal_manifest=a.seal_manifest, plan_file=a.plan, clock=clock)
    except Exception as exc:                          # noqa: BLE001 - damaged input is a clean message and a non-zero exit, never a traceback
        print(f"data_freshness_audit: {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr)
        return 2
    print(json.dumps(rep, indent=2, sort_keys=True, default=str))
    rc = EXIT[rep["axes"]["data_freshness"]["verdict"]]
    if rc == 0 and a.require_ready and rep["axes"]["research_readiness"]["verdict"] != "READY":
        return 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
