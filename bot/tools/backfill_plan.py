"""Offline BACKFILL PLAN and LOADER DIAGNOSIS for the registered cross-sectional design. Pure reads of already-collected files; no network, no credential, no write other
than an optional ``--out PLAN.json``. The agent that wrote it never ran it against the owner's data (the sandbox cannot see it): the owner runs it.

Question 1 (``plan``): what exact history does the design need, for which symbols, what already exists, what is missing, and which commands close the gap?
  * WINDOW. needed_days = warmup_days + formation_days + min_holding_rows * hold_days (the evaluator needs min_holding_rows NON-overlapping hold windows AFTER a formation lead-in).
    The window ends at ``study_to_ms``; if the operator's own ``study_from_ms`` is earlier than that, the longer span wins. Stated in days and ms.
  * SYMBOLS. From a VERIFIED universe_capture (``market_history.load_universe_dir``: integrity ok and re-derived status COMPLETE|PARTIAL; anything else is refused). A symbol is
    eligible when its lifecycle overlaps the window: Trading, or Closed/Delivering whose [launch, delivery] overlaps it. Everything else is excluded WITH a reason.
  * HISTORY. If a history root exists, every dataset manifest (funding, kline-<iv>, mark-kline-<iv>, index-kline-<iv>, open-interest-<iv>) is read, ``verify_dataset`` re-hashes it,
    and each eligible symbol is COMPLETE / PARTIAL / STALE / CONFLICTED / UNVERIFIED / MISSING with the exact missing ranges. A COMPLETE manifest whose requested window does not
    reach the required range is PARTIAL (window_short): a claim about a shorter window is not coverage of this one.
  * SYMBOL SET. Every dataset must be collected for the SAME symbols as funding. A difference is a blocker (SYMBOL_SET_MISMATCH), never silently tolerated. The emitted commands all
    carry ``--symbols-from-plan PLAN.json`` so the sets cannot diverge by construction; a re-plan after the runs proves it.
  * COST. Request counts and a wall-clock LOWER BOUND from the real knobs (page limits, shared token-bucket rate). Retries and backoff are not in it.
  ``research_ready`` is True only when every required dataset is verified and every eligible symbol COMPLETE over the whole required range, with no symbol-set mismatch and a long
  enough study window. It is never inferred from a manifest's own claim alone.

Question 2 (``diagnose``): why would ``candidate_readiness.load_history_root`` load only N symbols? The funnel is computed with the loader's own filters, then the real loader is run
(read-only, offline) and the two must agree (``funnel_matches_loader``); a disagreement is reported loudly instead of trusted.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import market_history as MH  # noqa: E402
import universe_capture as UC  # noqa: E402

SCHEMA = "backfill_plan/v1"
DAY_MS = 86_400_000
ENDED = ("Closed", "Delivering")                      # research_harness.ENDED_STATUSES (kept literal: this module stays light)
DEFAULT_STATUSES = ("Trading", "Closed")
DEFAULT_QUOTES = ("USDT",)
DEFAULT_INTERVALS = {MH.KLINE: "240", MH.MARK: "240", MH.INDEX: "240", MH.OI: "4h"}     # defaults, operator-overridable; stated in the plan, never implied
DATASET_ORDER = (MH.FUNDING, MH.MARK, MH.INDEX, MH.KLINE, MH.OI)        # funding first (it fixes the symbol set), then mark for the SAME symbols, then the rest
DEFAULT_REQUIRED = (MH.FUNDING, MH.MARK)                                # what the ranked-funding evaluator reads
NAMES_CAP = 25
RANGES_PER_SYMBOL_CAP = 6
SYMBOLS_WITH_RANGES_CAP = 200


class PlanError(ValueError):
    """The plan cannot be made honestly (unverified universe, bad parameters)."""


def _int(v: Any) -> Optional[int]:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def _cap(names: Sequence[str]) -> Dict[str, Any]:
    return {"count": len(names), "names": list(names)[:NAMES_CAP], "names_truncated": len(names) > NAMES_CAP}


# ---------------------------------------------------------------- window

def required_window(study_from_ms: Optional[int], study_to_ms: int, formation_days: int, hold_days: int, min_holding_rows: int, warmup_days: int) -> Dict[str, Any]:
    for k, v in (("formation_days", formation_days), ("hold_days", hold_days), ("min_holding_rows", min_holding_rows)):
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise PlanError(f"{k} must be a positive integer")
    if not isinstance(warmup_days, int) or isinstance(warmup_days, bool) or warmup_days < 0:
        raise PlanError("warmup_days must be a non-negative integer")
    if not isinstance(study_to_ms, int) or isinstance(study_to_ms, bool) or study_to_ms <= 0:
        raise PlanError("study_to_ms must be a positive epoch ms")
    holding_days = min_holding_rows * hold_days
    needed = warmup_days + formation_days + holding_days
    span_days = 0
    if study_from_ms is not None:
        if study_from_ms >= study_to_ms:
            raise PlanError("study_from_ms must be before study_to_ms")
        span_days = math.ceil((study_to_ms - study_from_ms) / DAY_MS)
    days = max(needed, span_days)
    return {"components": {"warmup_days": warmup_days, "formation_days": formation_days, "holding_days": holding_days, "hold_days": hold_days, "min_holding_rows": min_holding_rows},
            "needed_days": needed, "needed_ms": needed * DAY_MS, "study_span_days": span_days or None,
            "effective_days": days, "effective_ms": days * DAY_MS, "effective_from_ms": study_to_ms - days * DAY_MS, "effective_to_ms": study_to_ms,
            "study_window_sufficient": span_days == 0 or span_days >= needed or study_from_ms is None,
            "formula": "warmup_days + formation_days + min_holding_rows * hold_days (non-overlapping hold windows after the formation lead-in)"}


# ---------------------------------------------------------------- eligibility

def effective_statuses(requested: Sequence[str]) -> List[str]:
    """Closed implies Delivering (both are ended contracts whose [launch, delivery] is researched alike: the loader's ENDED_STATUSES)."""
    out = list(dict.fromkeys(requested))
    if "Closed" in out and "Delivering" not in out:
        out.append("Delivering")
    return out


def classify_record(rec: Mapping[str, Any], win_from: int, win_to: int, include_statuses: Sequence[str], quote_coins: Optional[Sequence[str]]) -> Tuple[bool, str, List[str]]:
    """(eligible, reason, notes). Reasons are stable tokens; the same overlap rule as ``market_history.build_specs`` (delivered before the window / launched after it)."""
    notes: List[str] = []
    ctype = rec.get("contractType")
    if isinstance(ctype, str) and ctype != "LinearPerpetual":
        return False, f"not_perpetual:{ctype}", notes
    q = rec.get("quoteCoin")
    if quote_coins and isinstance(q, str) and q not in quote_coins:
        return False, f"quote_not_selected:{q}", notes
    status = rec.get("status")
    if status == "PreLaunch":
        return False, "prelaunch_no_history", notes
    if status not in include_statuses:
        return False, f"status_not_included:{status}", notes
    launch, delivery = _int(rec.get("launchTime")), _int(rec.get("deliveryTime"))
    if status in ENDED:
        if delivery is None:
            return False, "closed_without_delivery", notes
        if launch is not None and delivery <= launch:
            return False, "never_trading", notes
        if launch is None:
            notes.append("launch_unknown")
    elif launch is None:
        notes.append("launch_unknown")
    if delivery is not None and delivery < win_from:
        return False, "delivered_before_window", notes
    if launch is not None and launch > win_to:
        return False, "launched_after_window", notes
    iv = rec.get("fundingInterval_min")
    if not (isinstance(iv, int) and not isinstance(iv, bool) and iv > 0):
        return False, "funding_interval_unknown", notes
    return True, "", notes


def symbol_range(rec: Mapping[str, Any], win_from: int, win_to: int) -> Tuple[int, int]:
    launch, delivery = _int(rec.get("launchTime")), _int(rec.get("deliveryTime"))
    return max(win_from, launch or win_from), min(win_to, delivery) if delivery and rec.get("status") in ENDED else win_to


def classify_universe(recs: Mapping[str, Mapping[str, Any]], win_from: int, win_to: int, include_statuses: Sequence[str], quote_coins: Optional[Sequence[str]]) -> Dict[str, Any]:
    eligible: Dict[str, Dict[str, Any]] = {}
    excluded: Dict[str, str] = {}
    notes: Dict[str, List[str]] = {}
    for s in sorted(recs):
        ok, why, nt = classify_record(recs[s], win_from, win_to, include_statuses, quote_coins)
        if ok:
            eligible[s] = recs[s]
            if nt:
                notes[s] = nt
        else:
            excluded[s] = why
    by_reason: Dict[str, List[str]] = {}
    for s, why in excluded.items():
        by_reason.setdefault(why, []).append(s)
    return {"eligible": eligible, "excluded": excluded, "notes": notes, "by_reason": by_reason}


# ---------------------------------------------------------------- history datasets

def dataset_dirs(intervals: Mapping[str, str]) -> Dict[str, str]:
    return {MH.FUNDING: MH.dataset_dirname(MH.FUNDING, None), **{d: MH.dataset_dirname(d, intervals[d]) for d in (MH.MARK, MH.INDEX, MH.KLINE, MH.OI)}}


def _iv_ms(dataset: str, interval: Optional[str], rec: Mapping[str, Any]) -> Optional[int]:
    if dataset == MH.FUNDING:
        m = _int(rec.get("fundingInterval_min"))
        return m * 60_000 if m else None
    return MH.interval_ms_of(dataset, interval)


def _merge(ranges: List[List[int]]) -> List[List[int]]:
    out: List[List[int]] = []
    for a, b in sorted(r for r in ranges if r[0] <= r[1]):
        if out and a <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def assess_symbol(rec: Mapping[str, Any], man_sym: Optional[Mapping[str, Any]], need: Tuple[int, int], iv_ms: Optional[int], dataset_verified: bool) -> Dict[str, Any]:
    rs, re_ = need
    if man_sym is None:
        return {"status": "MISSING", "missing_ranges": [[rs, re_]], "reasons": ["not_in_dataset"]}
    base = man_sym.get("status")
    cov = man_sym.get("coverage") if isinstance(man_sym.get("coverage"), dict) else {}
    spec = man_sym.get("spec") if isinstance(man_sym.get("spec"), dict) else {}
    reasons = list(man_sym.get("reasons") or [])
    miss: List[List[int]] = []
    s0, s1 = _int(spec.get("start_ms")), _int(spec.get("end_ms"))
    if s0 is not None and s0 > rs:
        miss.append([rs, min(re_, s0 - 1)])
        reasons.append("window_short_start")
    if s1 is not None and s1 < re_:
        miss.append([max(rs, s1 + 1), re_])
        reasons.append("window_short_end")
    step = iv_ms or 1
    for g in man_sym.get("gaps") or []:
        if isinstance(g, dict) and isinstance(g.get("after_ts"), int) and isinstance(g.get("next_ts"), int):
            miss.append([g["after_ts"] + step, g["next_ts"] - step])
    first, last = cov.get("first_ts"), cov.get("last_ts")
    if isinstance(first, int) and (cov.get("head_missing") or 0) > 0:
        miss.append([rs, first - 1])
    if not isinstance(first, int):
        miss.append([rs, re_])
    elif isinstance(last, int) and last + 2 * step < re_ and base in ("PARTIAL", "STALE"):
        miss.append([last + step, re_])
    miss = _merge(miss)
    status = base if base in ("COMPLETE", "PARTIAL", "STALE", "CONFLICTED", "UNVERIFIED") else "UNVERIFIED"
    if status == "COMPLETE" and any(r.startswith("window_short") for r in reasons):
        status = "PARTIAL"
    if status in ("COMPLETE", "STALE") and not dataset_verified:
        status = "UNVERIFIED"
        reasons.append("dataset_does_not_verify")
    if status == "PARTIAL" and not miss:
        reasons.append("partial_without_a_locatable_range")
    return {"status": status, "missing_ranges": miss[:RANGES_PER_SYMBOL_CAP], "missing_ranges_truncated": len(miss) > RANGES_PER_SYMBOL_CAP, "reasons": reasons}


def read_dataset(root: str, dirname: str, verify: bool) -> Dict[str, Any]:
    ddir = os.path.join(root, dirname)
    mp = os.path.join(ddir, "manifest.json")
    if os.path.islink(ddir) or not os.path.isdir(ddir):
        return {"present": False, "dir": ddir}
    out: Dict[str, Any] = {"present": True, "dir": ddir, "manifest": None, "verified": None, "verify_problems": []}
    try:
        with open(mp, "rb") as fh:
            man = json.loads(fh.read())
    except (OSError, ValueError):
        man = None
    if not isinstance(man, dict) or man.get("schema") != MH.SCHEMA or not isinstance(man.get("symbols"), dict):
        out["present"] = False
        out["reason"] = "manifest.json missing or not market_history/v1"
        return out
    out["manifest"] = man
    if verify:
        v = MH.verify_dataset(ddir)
        out["verified"] = bool(v.get("ok"))
        out["verify_problems"] = [str(p) for p in v.get("problems", [])[:5]]
    return out


def _estimate(dataset: str, interval: Optional[str], need_by_symbol: Mapping[str, Tuple[int, int, Optional[int]]], not_complete: Sequence[str], workers: int, rate: float) -> Dict[str, Any]:
    limit = MH.DATASETS[dataset]["limit"]

    def reqs(sym: str) -> int:
        a, b, iv = need_by_symbol[sym]
        rows = (b - a) // iv + 1 if iv else 0
        return math.ceil(rows / limit) + 1                  # + the empty page that ends a symbol launched inside the window (upper bound)
    total = sum(reqs(s) for s in need_by_symbol)
    remaining = sum(reqs(s) for s in not_complete if s in need_by_symbol)
    return {"page_limit": limit, "requests_total": total, "requests_remaining": remaining, "workers": workers, "rate_per_s": rate,
            "wall_clock_lower_bound_s_total": round(total / rate, 1), "wall_clock_lower_bound_s_remaining": round(remaining / rate, 1),
            "note": "lower bound: requests / shared token-bucket rate; retries, backoff and server latency are not included; pages = ceil(rows/limit)+1"}


# ---------------------------------------------------------------- plan

def plan(universe_dir: str, history_root: Optional[str] = None, *, study_from_ms: Optional[int], study_to_ms: int, formation_days: int, hold_days: int, min_holding_rows: int = 60,
         warmup_days: int = 7, include_statuses: Sequence[str] = DEFAULT_STATUSES, quote_coins: Optional[Sequence[str]] = DEFAULT_QUOTES, intervals: Optional[Mapping[str, str]] = None,
         required_datasets: Sequence[str] = DEFAULT_REQUIRED, workers: int = MH.DEFAULT_WORKERS, rate: float = MH.DEFAULT_RATE, verify: bool = True,
         plan_path: Optional[str] = None, out_root: Optional[str] = None, now_ms: Optional[int] = None) -> Dict[str, Any]:
    iv = dict(DEFAULT_INTERVALS)
    iv.update(intervals or {})
    if MH.interval_ms_of(MH.MARK, iv[MH.MARK]) is None or MH.interval_ms_of(MH.KLINE, iv[MH.KLINE]) is None or MH.interval_ms_of(MH.INDEX, iv[MH.INDEX]) is None \
            or MH.interval_ms_of(MH.OI, iv[MH.OI]) is None:
        raise PlanError("an interval is not a fixed-grid interval of its dataset")
    bad = [d for d in required_datasets if d not in MH.DATASETS]
    if bad:
        raise PlanError(f"unknown required dataset(s) {bad}")
    if not isinstance(workers, int) or not 1 <= workers <= MH.MAX_WORKERS or not rate > 0:
        raise PlanError(f"workers must be 1..{MH.MAX_WORKERS} and rate > 0")
    win = required_window(study_from_ms, study_to_ms, formation_days, hold_days, min_holding_rows, warmup_days)
    w0, w1 = win["effective_from_ms"], win["effective_to_ms"]
    requested_statuses = list(include_statuses)
    include_statuses = effective_statuses(requested_statuses)
    try:
        recs, uinfo = MH.load_universe_dir(universe_dir)               # refuses anything that does not verify (integrity ok AND re-derived COMPLETE|PARTIAL)
    except (MH.HistoryError, OSError, UC.CaptureError) as exc:
        raise PlanError(f"universe capture refused: {exc}") from exc
    cl = classify_universe(recs, w0, w1, list(include_statuses), list(quote_coins) if quote_coins else None)
    eligible: Dict[str, Dict[str, Any]] = cl["eligible"]
    esyms = sorted(eligible)
    blockers: List[Dict[str, str]] = []
    caveats: List[str] = []
    if not esyms:
        blockers.append({"code": "NO_ELIGIBLE_SYMBOLS", "detail": "no instrument of the verified universe overlaps the window under these filters"})
    if not win["study_window_sufficient"]:
        blockers.append({"code": "STUDY_WINDOW_TOO_SHORT", "detail": f"study span {win['study_span_days']}d < needed {win['needed_days']}d; the plan collects {win['effective_days']}d regardless"})
    if uinfo.get("dataset_status") != "COMPLETE":
        caveats.append(f"universe capture re-derived dataset_status {uinfo.get('dataset_status')!r}: the symbol list is only as complete as that capture")
    cov = uinfo.get("status_coverage") if isinstance(uinfo.get("status_coverage"), dict) else {}
    for st in include_statuses:
        if uinfo.get("capture_schema") == 1 and st != "Trading":
            caveats.append(f"legacy schema-1 capture holds only Trading rows: status {st} cannot be listed (SURVIVORS_ONLY)")
        elif cov and not str(cov.get(st, "")).startswith("OBSERVED"):
            caveats.append(f"universe capture did not observe status partition {st} ({cov.get(st)!r}): that partition is absent from the symbol list")
    if now_ms is not None and study_to_ms > now_ms - MH.CLOSED_SAFETY_MS:
        caveats.append("study_to_ms is not behind the clock by the safety margin: the newest candle may be unclosed (reported PARTIAL by the collector)")
    dirs = dataset_dirs(iv)
    datasets: Dict[str, Any] = {}
    sets: Dict[str, set] = {}
    for ds in DATASET_ORDER:
        interval = None if ds == MH.FUNDING else iv[ds]
        need = {s: (*symbol_range(eligible[s], w0, w1), _iv_ms(ds, interval, eligible[s])) for s in esyms}
        entry: Dict[str, Any] = {"dataset": ds, "interval": interval, "dirname": dirs[ds], "required": ds in required_datasets, "present": False}
        if history_root is not None:
            rd = read_dataset(os.path.abspath(history_root), dirs[ds], verify)
            entry["present"] = bool(rd["present"])
        else:
            rd = {"present": False}
        counts = {k: 0 for k in ("COMPLETE", "PARTIAL", "STALE", "CONFLICTED", "UNVERIFIED", "MISSING")}
        per: Dict[str, Dict[str, Any]] = {}
        if rd["present"]:
            man = rd["manifest"]
            entry.update(dataset_status=man.get("dataset_status"), manifest_digest=man.get("manifest_digest"), verified=rd["verified"], verify_problems=rd["verify_problems"],
                         request=man.get("request"), collected_with_universe=(man.get("universe") or {}).get("dir") if isinstance(man.get("universe"), dict) else None,
                         collected_with_included_statuses=(man.get("universe") or {}).get("included_statuses") if isinstance(man.get("universe"), dict) else None)
            sets[ds] = set(man["symbols"])
            for s in esyms:
                per[s] = assess_symbol(eligible[s], man["symbols"].get(s), (need[s][0], need[s][1]), need[s][2], rd["verified"] is not False)
        else:
            for s in esyms:
                per[s] = {"status": "MISSING", "missing_ranges": [[need[s][0], need[s][1]]], "reasons": ["dataset_absent" if history_root is not None else "no_history_root"]}
        for s, a in per.items():
            counts[a["status"]] += 1
        todo = sorted(s for s, a in per.items() if a["status"] != "COMPLETE")
        by_status: Dict[str, Any] = {k: _cap([s for s in esyms if per[s]["status"] == k]) for k in counts if counts[k]}
        listed = {s: {"status": per[s]["status"], "ranges_ms": per[s]["missing_ranges"], "reasons": per[s]["reasons"][:6]} for s in todo[:SYMBOLS_WITH_RANGES_CAP]}
        entry.update(counts=counts, symbols_by_status=by_status, to_collect=_cap(todo), missing_ranges=listed, missing_ranges_truncated=len(todo) > SYMBOLS_WITH_RANGES_CAP,
                     estimate=_estimate(ds, interval, need, todo, workers, rate))
        datasets[ds] = entry
    # symbol-set coherence: every present dataset against funding (the dataset that fixes the set) and against the plan
    mismatch: Dict[str, Any] = {}
    ref_name = MH.FUNDING if MH.FUNDING in sets else (next(iter(sets)) if sets else None)
    plan_set = set(esyms)
    for ds, st in sets.items():
        d: Dict[str, Any] = {}
        if ref_name and ds != ref_name:
            only_ref, only_ds = sorted(sets[ref_name] - st), sorted(st - sets[ref_name])
            if only_ref:
                d[f"only_in_{ref_name}"] = _cap(only_ref)
            if only_ds:
                d["only_in_this_dataset"] = _cap(only_ds)
        outside = sorted(st - plan_set)
        if outside:
            d["not_in_plan"] = _cap(outside)
        if d:
            mismatch[ds] = d
    if mismatch:
        blockers.append({"code": "SYMBOL_SET_MISMATCH", "detail": "datasets were collected for different symbol sets: " + ", ".join(sorted(mismatch)) + " (re-collect with --symbols-from-plan)"})
    # readiness
    unmet = []
    for ds in required_datasets:
        e = datasets[ds]
        if not e["present"]:
            unmet.append(f"{ds}: dataset absent")
        elif e.get("verified") is not True:
            unmet.append(f"{ds}: does not verify" if e.get("verified") is False else f"{ds}: not verified here")
        elif e["counts"]["COMPLETE"] != len(esyms):
            unmet.append(f"{ds}: {len(esyms) - e['counts']['COMPLETE']} of {len(esyms)} eligible symbols not COMPLETE over the required range")
    research_ready = bool(esyms) and not unmet and not blockers
    # commands
    commands = _commands(universe_dir, history_root, out_root, plan_path, win, include_statuses, iv, workers, rate, datasets, required_datasets)
    total_req = sum(datasets[d]["estimate"]["requests_total"] for d in DATASET_ORDER if datasets[d]["required"])
    rem_req = sum(datasets[d]["estimate"]["requests_remaining"] for d in DATASET_ORDER if datasets[d]["required"])
    rem_all = sum(datasets[d]["estimate"]["requests_remaining"] for d in DATASET_ORDER)
    doc = {"schema": SCHEMA,
           "parameters": {"study_from_ms": study_from_ms, "study_to_ms": study_to_ms, "formation_days": formation_days, "hold_days": hold_days, "min_holding_rows": min_holding_rows,
                          "warmup_days": warmup_days, "include_statuses": list(include_statuses), "include_statuses_requested": requested_statuses, "quote_coins": list(quote_coins) if quote_coins else None, "intervals": iv,
                          "required_datasets": list(required_datasets), "workers": workers, "rate": rate},
           "window": win,
           "universe": {"dir": uinfo.get("dir"), "manifest_sha256": uinfo.get("manifest_sha256"), "dataset_status": uinfo.get("dataset_status"), "capture_schema": uinfo.get("capture_schema"),
                        "snapshot_ms": uinfo.get("snapshot_ms"), "rows_linear": len(recs), "statuses_in_capture": uinfo.get("statuses_in_capture"), "status_coverage": uinfo.get("status_coverage"),
                        "delisting_observability": uinfo.get("delisting_observability")},
           "eligible_count": len(esyms), "eligible_symbols": esyms,
           "eligible_by_status": {k: sum(1 for s in esyms if eligible[s].get("status") == k) for k in sorted({eligible[s].get("status") for s in esyms}, key=str)},
           "eligible_notes": {k: v for k, v in sorted(cl["notes"].items())},
           "excluded_count": len(cl["excluded"]), "excluded_by_reason": {r: _cap(v) for r, v in sorted(cl["by_reason"].items())}, "excluded_detail": dict(sorted(cl["excluded"].items())),
           "history_root": os.path.abspath(history_root) if history_root else None, "datasets": datasets, "symbol_set_mismatch": mismatch,
           "estimate": {"required_datasets_requests_total": total_req, "required_datasets_requests_remaining": rem_req, "all_datasets_requests_remaining": rem_all,
                        "required_datasets_wall_clock_lower_bound_s_remaining": round(rem_req / rate, 1), "all_datasets_wall_clock_lower_bound_s_remaining": round(rem_all / rate, 1)},
           "commands": commands, "blockers": blockers, "caveats": caveats,
           "readiness": {"research_ready": research_ready, "unmet": unmet, "basis": "every required dataset re-hashed here (verify_dataset) and every eligible symbol COMPLETE over its whole required range; never a manifest's own claim"}}
    doc["plan_digest"] = plan_digest(doc)
    return doc


def replan(doc: Mapping[str, Any], history_root: str, *, verify: bool = True) -> Dict[str, Any]:
    """Re-run a SEALED plan's own parameters against ``history_root`` now. Used by readers (``data_freshness_audit``) that must not believe a plan file's readiness claim: only a fresh
    re-derivation counts, and it must be about the same universe capture and the same symbol set as the sealed plan."""
    p = doc["parameters"]
    fresh = plan(doc["universe"]["dir"], history_root, study_from_ms=p["study_from_ms"], study_to_ms=p["study_to_ms"], formation_days=p["formation_days"], hold_days=p["hold_days"],
                 min_holding_rows=p["min_holding_rows"], warmup_days=p["warmup_days"], include_statuses=p.get("include_statuses_requested") or p["include_statuses"],
                 quote_coins=p.get("quote_coins"), intervals=p["intervals"], required_datasets=p["required_datasets"], workers=p["workers"], rate=p["rate"], verify=verify)
    fresh["same_universe_as_sealed_plan"] = fresh["universe"]["manifest_sha256"] == doc["universe"]["manifest_sha256"]
    fresh["same_symbol_set_as_sealed_plan"] = fresh["eligible_symbols"] == doc["eligible_symbols"]
    return fresh


def plan_digest(doc: Mapping[str, Any]) -> str:
    return MH.plan_digest(doc)


def _commands(universe_dir: str, history_root: Optional[str], out_root: Optional[str], plan_path: Optional[str], win: Mapping[str, Any], statuses: Sequence[str], iv: Mapping[str, str],
              workers: int, rate: float, datasets: Mapping[str, Any], required: Sequence[str]) -> List[Dict[str, Any]]:
    root = history_root or out_root or "<HISTORY_ROOT>"
    pp = plan_path or "<PLAN.json>"
    base = (f"--out-root {root} --symbols-from-plan {pp} --universe-capture {universe_dir} --include-statuses {','.join(statuses)} --days {win['effective_days']} "
            f"--end-ms {win['effective_to_ms']} --workers {workers} --rate {rate:g}")
    cmds: List[Dict[str, Any]] = []
    for i, ds in enumerate(DATASET_ORDER, 1):
        e = datasets[ds]
        todo = e["to_collect"]["count"]
        itv = f" --interval {iv[ds]}" if ds != MH.FUNDING else ""
        cmds.append({"order": len(cmds) + 1, "dataset": ds, "required_for_ranked_funding": ds in required, "symbols_to_collect": todo, "requests_remaining": e["estimate"]["requests_remaining"],
                     "command": f"python3 tools/market_history.py {ds} {base}{itv}", "resumable": "re-running resumes each symbol from its last verified page; a COMPLETE symbol costs one verify pass, no request beyond the window check",
                     "then": f"python3 tools/market_history.py verify --dir {root}/{e['dirname']}"})
    cmds.append({"order": len(cmds) + 1, "dataset": None, "command": f"python3 tools/backfill_plan.py plan --universe-capture {universe_dir} --history-root {root} --out {pp} ...(same parameters)",
                 "purpose": "re-plan: proves the symbol sets agree and every eligible symbol is COMPLETE; research_ready is decided there"})
    return cmds


# ---------------------------------------------------------------- diagnose

def _scan_notes(history_root: str, data_root: Optional[str]) -> Dict[str, Any]:
    import candidate_readiness as CR            # read-only helpers; imported lazily so ``plan`` stays light
    root = data_root or history_root
    scan = CR.scan_data_root(root)
    out: Dict[str, Any] = {"scanned_root": root, "checked": scan.get("checked"), "universe_capture_dirs": scan.get("universe_capture_dirs"),
                           "market_history_dirs": len(scan.get("market_history") or []),
                           "rule": "scan_data_root looks ONLY under the root it is given (depth <= 4, no symlinks, skips 'state') and counts a manifest as a universe capture only when its 'schema' is exactly "
                                   "'universe_capture/v2' (a legacy capture carries 'schema_version: 1' and is never counted)"}
    siblings: List[Dict[str, Any]] = []
    parent = os.path.dirname(os.path.abspath(root))
    if os.path.isdir(parent):
        for n in sorted(os.listdir(parent))[:200]:
            d = os.path.join(parent, n)
            mp = os.path.join(d, "manifest.json")
            if n == "state" or os.path.islink(d) or not os.path.isfile(mp) or os.path.abspath(d) == os.path.abspath(root):
                continue
            m = CR._read_manifest(mp)
            sch = None if m is None else (m.get("schema") if m.get("schema") is not None else m.get("schema_version"))
            siblings.append({"dir": d, "schema": sch, "scanned": False, "why_not_found": "it is a sibling of the scanned root, not under it"})
    out["sibling_manifests_outside_scanned_root"] = siblings
    return out


def diagnose(universe_dir: Optional[str], history_root: str, *, study_from_ms: Optional[int], study_to_ms: int, formation_days: int, hold_days: int, min_holding_rows: int = 60,
             warmup_days: int = 7, include_statuses: Sequence[str] = DEFAULT_STATUSES, quote_coins: Optional[Sequence[str]] = DEFAULT_QUOTES, mark_interval: Optional[str] = None,
             data_root: Optional[str] = None) -> Dict[str, Any]:
    import candidate_readiness as CR
    import research_harness as RH
    res: Dict[str, Any] = {"schema": SCHEMA + "/diagnose", "history_root": os.path.abspath(history_root), "blockers": [], "steps": [], "explanation": []}
    blockers: List[Dict[str, str]] = res["blockers"]
    res["data_scan"] = _scan_notes(history_root, data_root)
    if not universe_dir:
        blockers.append({"code": "UNIVERSE_DIR_NOT_SUPPLIED", "detail": "no --universe-capture: the loader cannot read launch/delivery/fundingInterval and refuses (universe_dir is required)"})
        if not res["data_scan"]["universe_capture_dirs"]:
            blockers.append({"code": "DATA_SCAN_FOUND_NO_UNIVERSE_CAPTURE", "detail": "data_scan.universe_capture_dirs is [] for the scanned root; see data_scan.sibling_manifests_outside_scanned_root"})
    win = required_window(study_from_ms, study_to_ms, formation_days, hold_days, min_holding_rows, warmup_days)
    res["window"] = win
    mark_dirs = sorted(n for n in (os.listdir(history_root) if os.path.isdir(history_root) else []) if n.startswith(MH.MARK + "-"))
    if mark_interval is not None:
        mark_name = MH.dataset_dirname(MH.MARK, str(mark_interval))
    elif len(mark_dirs) == 1:
        mark_name = mark_dirs[0]
    else:
        mark_name = None
        blockers.append({"code": "MARK_DATASET_AMBIGUOUS_OR_ABSENT", "detail": f"mark_interval not given and {len(mark_dirs)} mark-kline datasets found (need exactly one)"})
    fund = read_dataset(os.path.abspath(history_root), MH.FUNDING, True)
    mark = read_dataset(os.path.abspath(history_root), mark_name, True) if mark_name else {"present": False}
    for nm, ds in (("funding", fund), (mark_name or "mark-kline", mark)):
        if not ds["present"]:
            blockers.append({"code": "DATASET_ABSENT", "detail": f"{nm}: not found under the history root"})
        elif ds["verified"] is False:
            blockers.append({"code": "DATASET_DOES_NOT_VERIFY", "detail": f"{nm}: " + "; ".join(ds["verify_problems"][:3])})
    recs: Dict[str, Dict[str, Any]] = {}
    uinfo: Dict[str, Any] = {}
    if universe_dir:
        try:
            recs, uinfo = MH.load_universe_dir(universe_dir)
        except (MH.HistoryError, OSError, UC.CaptureError) as exc:
            blockers.append({"code": "UNIVERSE_REFUSED", "detail": str(exc)[:300]})
    steps = res["steps"]

    def step(name: str, syms: Sequence[str], note: str = "") -> None:
        steps.append({"step": name, "count": len(syms), "symbols": _cap(sorted(syms)), "note": note})
    include_statuses = effective_statuses(include_statuses)
    if recs:
        w0, w1 = win["effective_from_ms"], win["effective_to_ms"]
        cl = classify_universe(recs, w0, w1, list(include_statuses), list(quote_coins) if quote_coins else None)
        elig = set(cl["eligible"])
        step("1_universe_rows_linear", list(recs), f"statuses: {uinfo.get('statuses_in_capture')}")
        step("2_eligible_by_status_and_lifecycle", list(elig), "excluded: " + json.dumps({r: len(v) for r, v in sorted(cl["by_reason"].items())}))
        fsyms = set(fund["manifest"]["symbols"]) if fund["present"] else set()
        msyms = set(mark["manifest"]["symbols"]) if mark["present"] else set()
        s3 = elig & fsyms
        step("3_eligible_in_funding_dataset", list(s3), f"funding dataset holds {len(fsyms)} symbols in total")
        s4 = s3 & msyms
        step("4_and_in_mark_dataset", list(s4), f"mark dataset holds {len(msyms)} symbols in total")
        # the loader's own view: (funding U mark) symbols, filtered in the loader's order
        loader: Dict[str, str] = {}
        series_ok: List[str] = []
        non_complete: List[str] = []
        if fund["present"] and mark["present"]:
            fm, mm = fund["manifest"]["symbols"], mark["manifest"]["symbols"]
            for sym in sorted(set(fm) | set(mm)):
                if sym not in recs:
                    loader[sym] = "not_in_universe"
                elif sym not in fm:
                    loader[sym] = "not_in_funding_dataset"
                elif sym not in mm:
                    loader[sym] = "not_in_mark_dataset"
                else:
                    sts = {"f": fm[sym].get("status"), "m": mm[sym].get("status")}
                    if any(v in CR.EXCLUDED_SYMBOL_STATUSES or v is None for v in sts.values()):
                        loader[sym] = "symbol_status:" + ",".join(f"{k}={v}" for k, v in sorted(sts.items()))
                    else:
                        if any(v != "COMPLETE" for v in sts.values()):
                            non_complete.append(sym)
                        cf, cm = (fm[sym].get("coverage") or {}).get("rows"), (mm[sym].get("coverage") or {}).get("rows")
                        if not cf or not cm:
                            loader[sym] = "no_rows"
                            continue
                        ts = _row_ts(os.path.join(fund["dir"], sym, "rows.jsonl"))
                        segs, why = CR.funding_interval_segments(fm[sym], ts)
                        if segs is None:
                            loader[sym] = why or "funding_interval_unknown"
                            continue
                        r = recs[sym]
                        if r.get("status") in RH.ENDED_STATUSES and not _int(r.get("deliveryTime")):
                            loader[sym] = "closed_without_delivery"
                            continue
                        series_ok.append(sym)
            step("5_loader_status_filter_kept_symbols", [s for s in set(fm) & set(mm) if s in recs and s not in {k for k, v in loader.items() if v.startswith("symbol_status")}],
                 "the loader drops only CONFLICTED/UNVERIFIED; PARTIAL/STALE symbols are KEPT and warned (non_complete: %d)" % len(non_complete))
            step("6_complete_in_both", [s for s in s4 if fm[s].get("status") == "COMPLETE" and mm[s].get("status") == "COMPLETE"], "informational: the loader does not require COMPLETE")
            no_iv = [s for s, w in loader.items() if w.startswith("funding_interval_unknown")]
            step("7_funding_interval_known", [s for s in series_ok], f"dropped for unknown interval: {len(no_iv)}")
            step("8_not_excluded_loaded", series_ok, "excluded by reason: " + json.dumps(_tally(loader.values())))
            res["loader_exclusions"] = {"by_reason": _tally(loader.values()), "examples": {r: _cap(sorted(s for s, w in loader.items() if w.split("(")[0].split(":")[0] == r))
                                                                                      for r in sorted({w.split("(")[0].split(":")[0] for w in loader.values()})}}
            res["predicted_symbols_loaded"] = len(series_ok)
            # cross-check with the real loader
            res["loader_run"] = _run_loader(CR, RH, history_root, universe_dir or "", mark_interval if mark_interval is not None else (mark_name[len(MH.MARK) + 1:] if mark_name else None))
            actual = res["loader_run"].get("symbols_loaded")
            res["funnel_matches_loader"] = (actual == len(series_ok)) if actual is not None else None
            if res["funnel_matches_loader"] is False:
                blockers.append({"code": "FUNNEL_DISAGREES_WITH_LOADER", "detail": f"predicted {len(series_ok)} but candidate_readiness.load_history_root loaded {actual}: do not trust this funnel"})
            # coverage vs eligible
            missing_f = sorted(elig - fsyms)
            if missing_f:
                u = fund["manifest"].get("universe") if fund["present"] else None
                how = ("collected with an explicit --symbols list (the manifest records no universe)" if not u else
                       f"collected from universe {u.get('dir')} with included_statuses={u.get('included_statuses')}")
                blockers.append({"code": "HISTORY_COVERS_FEW_ELIGIBLE_SYMBOLS", "detail": f"{len(missing_f)} of {len(elig)} eligible symbols have no funding history; the funding dataset was {how}"})
            span = _span_days(fund, mark)
            res["history_span_days"] = span
            res["holding_rows_estimate"] = _holding_rows(fund, formation_days, hold_days)
            if res["holding_rows_estimate"] is not None and res["holding_rows_estimate"] < min_holding_rows:
                blockers.append({"code": "TOO_FEW_HOLDING_ROWS", "detail": f"history supports ~{res['holding_rows_estimate']} non-overlapping {hold_days}d holding rows after a {formation_days}d formation lead-in; need {min_holding_rows} "
                                 f"(window needed: {win['needed_days']}d)"})
    res["explanation"] = _explain(res)
    res["first_blocker"] = blockers[0] if blockers else None
    return res


def _row_ts(path: str) -> List[int]:
    out: List[int] = []
    try:
        with open(path, "rb") as fh:
            for line in fh:
                r = json.loads(line)
                if isinstance(r, dict) and isinstance(r.get("ts"), int) and isinstance(r.get("v"), dict):
                    out.append(r["ts"])
    except (OSError, ValueError):
        pass
    return out


def _tally(vals: Sequence[str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for v in vals:
        k = v.split("(")[0].split(":")[0]
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def _run_loader(CR: Any, RH: Any, root: str, universe_dir: str, mark_interval: Optional[str]) -> Dict[str, Any]:
    try:
        series, info = CR.load_history_root(root, universe_dir=universe_dir, mark_interval=mark_interval)
    except RH.HarnessError as exc:
        return {"refused": str(exc)[:300], "symbols_loaded": None}
    except Exception as exc:                          # noqa: BLE001 - a diagnosis reports a loader crash, it does not die with it
        return {"refused": f"{type(exc).__name__}: {str(exc)[:200]}", "symbols_loaded": None}
    return {"symbols_loaded": len(series), "excluded": len(info.get("excluded", [])), "warnings": list(info.get("warnings", []))[:5]}


def _span_days(fund: Mapping[str, Any], mark: Mapping[str, Any]) -> Optional[float]:
    f = _first_last(fund)
    return round((f[1] - f[0]) / DAY_MS, 2) if f else None


def _first_last(ds: Mapping[str, Any]) -> Optional[Tuple[int, int]]:
    if not ds.get("present"):
        return None
    firsts = [c["first_ts"] for r in ds["manifest"]["symbols"].values() for c in [r.get("coverage") or {}] if isinstance(c.get("first_ts"), int)]
    lasts = [c["last_ts"] for r in ds["manifest"]["symbols"].values() for c in [r.get("coverage") or {}] if isinstance(c.get("last_ts"), int)]
    return (min(firsts), max(lasts)) if firsts and lasts else None


def _holding_rows(fund: Mapping[str, Any], formation_days: int, hold_days: int) -> Optional[int]:
    fl = _first_last(fund)
    if not fl:
        return None
    return max(0, int((fl[1] - fl[0] - formation_days * DAY_MS) // (hold_days * DAY_MS)))


def _explain(res: Mapping[str, Any]) -> List[str]:
    out: List[str] = []
    for b in res["blockers"]:
        out.append(f"{b['code']}: {b['detail']}")
    for s in res["steps"]:
        out.append(f"{s['step']}: {s['count']}")
    if "predicted_symbols_loaded" in res:
        out.append(f"the loader keeps {res['predicted_symbols_loaded']} symbols")
    return out


# ---------------------------------------------------------------- CLI

def _csv(v: str) -> List[str]:
    return [x for x in v.split(",") if x]


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--universe-capture", required=False, default=None)
    p.add_argument("--history-root", default=None)
    p.add_argument("--study-from-ms", type=int, default=None)
    p.add_argument("--study-to-ms", type=int, default=None, help="default: the last closed UTC day boundary before now")
    p.add_argument("--formation-days", type=int, default=30)
    p.add_argument("--hold-days", type=int, default=7)
    p.add_argument("--min-holding-rows", type=int, default=60)
    p.add_argument("--warmup-days", type=int, default=7)
    p.add_argument("--include-statuses", default=",".join(DEFAULT_STATUSES))
    p.add_argument("--quote-coins", default=",".join(DEFAULT_QUOTES), help="empty = every quote coin")


def main(argv: Optional[Sequence[str]] = None, *, clock: Callable[[], int] = lambda: int(time.time() * 1000)) -> int:
    ap = argparse.ArgumentParser(description="Offline backfill plan and loader diagnosis (reads files only).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    _common(p)
    p.add_argument("--kline-interval", default=DEFAULT_INTERVALS[MH.KLINE])
    p.add_argument("--mark-interval", default=DEFAULT_INTERVALS[MH.MARK])
    p.add_argument("--index-interval", default=DEFAULT_INTERVALS[MH.INDEX])
    p.add_argument("--oi-interval", default=DEFAULT_INTERVALS[MH.OI])
    p.add_argument("--require", default=",".join(DEFAULT_REQUIRED), help="datasets that must be COMPLETE for research_ready")
    p.add_argument("--workers", type=int, default=MH.DEFAULT_WORKERS)
    p.add_argument("--rate", type=float, default=MH.DEFAULT_RATE)
    p.add_argument("--no-verify", action="store_true", help="do not re-hash datasets (research_ready is then impossible)")
    p.add_argument("--out", default=None, help="write the plan here (and print the commands against it)")
    d = sub.add_parser("diagnose")
    _common(d)
    d.add_argument("--mark-interval", default=None)
    d.add_argument("--data-root", default=None, help="the root you gave candidate_readiness --data-root (to explain data_scan.universe_capture_dirs)")
    a = ap.parse_args(argv)
    now = int(clock())
    to_ms = a.study_to_ms if a.study_to_ms is not None else ((now - MH.CLOSED_SAFETY_MS) // DAY_MS) * DAY_MS - 1
    try:
        common = dict(study_from_ms=a.study_from_ms, study_to_ms=to_ms, formation_days=a.formation_days, hold_days=a.hold_days, min_holding_rows=a.min_holding_rows,
                      warmup_days=a.warmup_days, include_statuses=_csv(a.include_statuses), quote_coins=_csv(a.quote_coins) or None)
        if a.cmd == "plan":
            if not a.universe_capture:
                raise PlanError("--universe-capture is required")
            doc = plan(a.universe_capture, a.history_root, **common, intervals={MH.KLINE: a.kline_interval, MH.MARK: a.mark_interval, MH.INDEX: a.index_interval, MH.OI: a.oi_interval},
                       required_datasets=_csv(a.require), workers=a.workers, rate=a.rate, verify=not a.no_verify, plan_path=a.out, now_ms=now)
            if a.out:
                MH._write_atomic(a.out, (json.dumps(doc, indent=1, sort_keys=True, allow_nan=False) + "\n").encode())
            print(json.dumps({k: doc[k] for k in ("schema", "window", "eligible_count", "excluded_count", "estimate", "blockers", "caveats", "readiness", "plan_digest")}
                             | {"datasets": {n: {"present": e["present"], "verified": e.get("verified"), "counts": e["counts"]} for n, e in doc["datasets"].items()},
                                "commands": [c["command"] for c in doc["commands"]], "written": a.out}, indent=2, sort_keys=True))
            return 0 if doc["readiness"]["research_ready"] else 1
        res = diagnose(a.universe_capture, a.history_root or "", **common, mark_interval=a.mark_interval, data_root=a.data_root)
        print(json.dumps(res, indent=2, sort_keys=True, default=str))
        return 1 if res["blockers"] else 0
    except (PlanError, MH.HistoryError, OSError, ValueError) as exc:
        print(f"backfill_plan: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
