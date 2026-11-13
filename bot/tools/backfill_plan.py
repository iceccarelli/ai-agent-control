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
EXCLUDED_SYMBOL_STATUSES = ("CONFLICTED", "UNVERIFIED")                 # candidate_readiness.EXCLUDED_SYMBOL_STATUSES (a test pins the equality)
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

#: The fraction of the funding window ``history_holdout`` reserves (RESERVED_FRACTION; pinned equal by a test: this module cannot import it, ``candidate_readiness`` imports this one). Every evaluation of
#: the history is refused until a holdout is reserved, so the window that carries ONE registered test is the window LEFT AFTER the reserve, not the whole one.
HOLDOUT_RESERVE_FRACTION = 0.30


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
    keep = 1.0 - HOLDOUT_RESERVE_FRACTION
    needed_after = math.ceil(needed / keep - 1e-9)                       # the whole window that leaves ``needed`` days to explore once the last fraction is reserved
    explore_after = math.floor(span_days * keep + 1e-9) if span_days else None
    return {"components": {"warmup_days": warmup_days, "formation_days": formation_days, "holding_days": holding_days, "hold_days": hold_days, "min_holding_rows": min_holding_rows},
            "needed_days": needed, "needed_ms": needed * DAY_MS, "study_span_days": span_days or None,
            "effective_days": days, "effective_ms": days * DAY_MS, "effective_from_ms": study_to_ms - days * DAY_MS, "effective_to_ms": study_to_ms,
            "study_window_sufficient": span_days == 0 or span_days >= needed or study_from_ms is None,
            "needed_days_after_holdout_reserve": needed_after, "explore_days_after_holdout_reserve": explore_after,
            "study_window_sufficient_after_holdout_reserve": span_days == 0 or study_from_ms is None or explore_after >= needed,
            "holdout_reserve_fraction": HOLDOUT_RESERVE_FRACTION,
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
    return max(win_from, launch or win_from), MH.clamp_end_to_delivery(delivery, win_to)


def classify_universe(recs: Mapping[str, Mapping[str, Any]], win_from: int, win_to: int, include_statuses: Sequence[str], quote_coins: Optional[Sequence[str]],
                      classifier: Optional[Any] = None) -> Dict[str, Any]:
    eligible: Dict[str, Dict[str, Any]] = {}
    excluded: Dict[str, str] = {}
    notes: Dict[str, List[str]] = {}
    for s in sorted(recs):
        ok, why, nt = (classifier or classify_record)(recs[s], win_from, win_to, include_statuses, quote_coins)
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


def selection_of(man: Mapping[str, Any]) -> Dict[str, Any]:
    """How a dataset's symbol set was chosen, from its manifest alone: the recorded ``selection`` block, else (a legacy manifest) UNKNOWN_LEGACY with what the old fields allow us to say."""
    sel = man.get("selection")
    if isinstance(sel, dict) and sel.get("mode") in MH.SELECTION_MODES:
        return {"mode": sel["mode"], "symbols_n": sel.get("symbols_n"), "plan_digest": sel.get("plan_digest"), "recorded": True}
    u = man.get("universe") if isinstance(man.get("universe"), dict) else None
    hint = "no universe recorded (an explicit --symbols list looks likely)" if not u else ("symbols_from_plan recorded" if u.get("symbols_from_plan") else "universe recorded")
    return {"mode": "UNKNOWN_LEGACY", "symbols_n": len(man.get("symbols") or {}), "plan_digest": ((u or {}).get("symbols_from_plan") or {}).get("plan_digest"), "recorded": False, "legacy_hint": hint}


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


# ---------------------------------------------------------------- THE FUNNEL (one function; plan, diagnose, the loader and the audit all call it)

FUNNEL_SCHEMA = "backfill_plan/funnel/v1"
WINDOW_FROM_REQUIRED = "REQUIRED_SPAN"
WINDOW_FROM_EXPLICIT = "EXPLICIT"
WINDOW_FROM_FUNDING_MANIFEST = "FUNDING_MANIFEST_REQUEST"
Classifier = Callable[[Mapping[str, Any], int, int, Sequence[str], Optional[Sequence[str]]], Tuple[bool, str, List[str]]]


#: lifecycle exclusion reasons that PROVE an ended contract is out of the study scope (not a perpetual of the selected quote, never traded, outside the window, ...). Any other reason an ended
#: (Closed/Delivering) contract is not in the evaluated series (closed_without_delivery, funding_interval_unknown, an unknown classifier class) leaves it UNACCOUNTED: survivors-only.
EXPLAINED_EXCLUSION_PREFIXES = ("not_perpetual", "quote_not_selected", "prelaunch_no_history", "delivered_before_window", "launched_after_window", "never_trading", "status_not_included",
                                "OUT_OF_SCOPE_DATED_FUTURE", "OUT_OF_SCOPE_OTHER_CONTRACT", "CONFIRMED_CANCELLED_BEFORE_OPENING")        # universe_capture lifecycle classes that PROVE out of scope


def exclusion_is_explained(reason: str) -> bool:
    return _reason_key(reason) in EXPLAINED_EXCLUSION_PREFIXES


def default_classifier() -> Classifier:
    """The lifecycle classifier every consumer shares. ``universe_capture.classify_lifecycle`` (the scope verdict ``eligible_perpetual_lifecycle_universe``) is the authority when this tree
    has it; until then ``classify_record`` (perpetual, quote, status, launch/delivery overlap, funding interval known). Injectable so a test can stand in any class."""
    return classify_record


def window_of_funding_manifest(history_root: str) -> Optional[Tuple[int, int]]:
    """The window the funding dataset itself says it requested (manifest ``request.start_ms/end_ms``): the loader's default when it is given no required span."""
    rd = read_dataset(os.path.abspath(history_root), MH.FUNDING, False)
    req = (rd.get("manifest") or {}).get("request") if rd.get("present") else None
    if isinstance(req, dict) and _int(req.get("start_ms")) and _int(req.get("end_ms")) and req["start_ms"] < req["end_ms"]:
        return int(req["start_ms"]), int(req["end_ms"])
    return None



def history_evidence_from_datasets(views: Mapping[str, Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """{symbol: {funding_rows, kline_rows, coverage_complete}} from VERIFIED history datasets only: it lets ``universe_capture`` tell a perpetual that was cancelled before opening (no funding and
    no kline rows over a COMPLETE collection) from one that is merely unresolved. A symbol is included only when a verified funding dataset AND a verified kline-type dataset both hold it."""
    f = views.get(MH.FUNDING)
    if not f or not f["rd"].get("present") or f["rd"].get("verified") is not True:
        return {}
    kl = [v for d, v in views.items() if d in (MH.MARK, MH.KLINE, MH.INDEX) and v["rd"].get("present") and v["rd"].get("verified") is True]
    out: Dict[str, Dict[str, Any]] = {}
    for sym, fr in f["rd"]["manifest"]["symbols"].items():
        krs = [v["rd"]["manifest"]["symbols"][sym] for v in kl if sym in v["rd"]["manifest"]["symbols"]]
        if not krs:
            continue
        rows = lambda r: int((r.get("coverage") or {}).get("rows") or 0)              # noqa: E731
        out[sym] = {"funding_rows": rows(fr), "kline_rows": max(rows(k) for k in krs), "coverage_complete": fr.get("status") == "COMPLETE" and all(k.get("status") == "COMPLETE" for k in krs)}
    return out


def scope_classifier(scope: Mapping[str, Any]) -> Classifier:
    """The lifecycle classifier backed by ``universe_capture.eligible_instruments``: only an instrument the capture's scope verdict calls eligible can be eligible; an excluded perpetual
    is named by its lifecycle CLASS (OUT_OF_SCOPE_DATED_FUTURE, CONFIRMED_CANCELLED_BEFORE_OPENING, TERMINAL_UNRESOLVED_PENDINGOPEN, UNEXPLAINED_PARTITION_CONTRADICTION, ...)."""
    elig = scope.get("eligible") or {}
    notable = {e["symbol"]: e["klass"] for e in ((scope.get("lifecycle") or {}).get("notable") or []) if isinstance(e, dict) and "symbol" in e}

    def classify(rec: Mapping[str, Any], w0: int, w1: int, include: Sequence[str], quotes: Optional[Sequence[str]]) -> Tuple[bool, str, List[str]]:
        ok, why, notes = classify_record(rec, w0, w1, include, quotes)
        sym = rec.get("symbol")
        if sym in elig:
            return ok, why, notes
        if why.startswith(("not_perpetual", "quote_not_selected")):
            return False, why, notes
        if sym in notable:
            return False, notable[sym], notes
        return False, why or "NOT_IN_ELIGIBLE_LIFECYCLE_SET", notes
    return classify


class Funnel:
    """Captured universe -> eligible lifecycle instruments -> required datasets (funding, mark, ...) -> status / interval checks -> complete historical coverage.

    Built ONLY by :func:`funnel`; ``loadable`` is what ``candidate_readiness.load_history_root`` turns into series (it loads exactly this list), ``complete`` is the subset COMPLETE over
    the whole required range in every required dataset. ``report()`` is the one JSON-able view every reader embeds, so counts, reasons and symbol lists cannot differ between tools."""

    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)

    def report(self) -> Dict[str, Any]:
        return self._report

    def reasons_for_symbols_in_datasets(self) -> Dict[str, str]:
        return self._dataset_symbol_reasons


def funnel(universe_dir: str, history_root: Optional[str], *, window: Optional[Tuple[int, int]] = None, window_source: str = WINDOW_FROM_REQUIRED,
           include_statuses: Sequence[str] = DEFAULT_STATUSES, quote_coins: Optional[Sequence[str]] = DEFAULT_QUOTES, required_datasets: Sequence[str] = DEFAULT_REQUIRED,
           intervals: Optional[Mapping[str, str]] = None, report_datasets: Optional[Sequence[str]] = None, verify: bool = True, classifier: Optional[Classifier] = None,
           universe: Optional[Tuple[Mapping[str, Mapping[str, Any]], Mapping[str, Any]]] = None, pre_verified: Sequence[str] = ()) -> Funnel:
    """The ONE funnel. ``window`` = (from_ms, to_ms) the history must cover; None = the window the funding manifest itself requested. Raises PlanError when the universe capture does not
    verify (``market_history.load_universe_dir``) or no window can be determined."""
    iv = dict(DEFAULT_INTERVALS)
    iv.update(intervals or {})
    required = [d for d in DATASET_ORDER if d in required_datasets]
    bad = [d for d in required_datasets if d not in MH.DATASETS]
    if bad:
        raise PlanError(f"unknown required dataset(s) {bad}")
    shown = [d for d in DATASET_ORDER if d in (report_datasets if report_datasets is not None else DATASET_ORDER) or d in required]
    root_abs = os.path.abspath(history_root) if history_root is not None else None
    if window is None:
        window = window_of_funding_manifest(root_abs) if root_abs else None
        window_source = WINDOW_FROM_FUNDING_MANIFEST
        if window is None:
            raise PlanError("no study window given and the funding dataset records none (manifest request.start_ms/end_ms): pass the required window")
    w0, w1 = int(window[0]), int(window[1])
    include = effective_statuses(list(include_statuses))
    try:
        recs, uinfo = universe if universe is not None else MH.load_universe_dir(universe_dir)               # refuses anything that does not verify (integrity ok AND re-derived COMPLETE|PARTIAL)
    except (MH.HistoryError, OSError, UC.CaptureError) as exc:
        raise PlanError(f"universe capture refused: {exc}") from exc
    dirs = dataset_dirs(iv)
    ds_view: Dict[str, Dict[str, Any]] = {}
    sets: Dict[str, set] = {}
    for ds in shown:
        rd = read_dataset(root_abs, dirs[ds], verify and dirs[ds] not in pre_verified) if root_abs is not None else {"present": False}
        if rd["present"] and dirs[ds] in pre_verified:
            rd["verified"] = True
        ds_view[ds] = {"dataset": ds, "interval": None if ds == MH.FUNDING else iv[ds], "dirname": dirs[ds], "required": ds in required, "rd": rd}
        if rd["present"]:
            sets[ds] = set(rd["manifest"]["symbols"])
    evidence = history_evidence_from_datasets(ds_view)
    scope = UC.eligible_instruments(universe_dir, history_evidence=evidence or None) if classifier is None else None
    authority = "INJECTED_CLASSIFIER"
    if classifier is None:
        if scope["usable"]:
            classifier, authority = scope_classifier(scope), "universe_capture.eligible_instruments"
        else:
            classifier, authority = default_classifier(), "RECORD_FIELDS_FALLBACK(scope not usable: " + str(scope.get("withheld_reason"))[:120] + ")"
    cl = classify_universe(recs, w0, w1, include, list(quote_coins) if quote_coins else None, classifier)
    eligible: Dict[str, Dict[str, Any]] = cl["eligible"]
    esyms = sorted(eligible)
    swin = (scope or {}).get("eligible") or {}
    for ds in shown:
        v = ds_view[ds]
        interval = v["interval"]
        need = {}
        for s_ in esyms:
            a0, a1 = symbol_range(eligible[s_], w0, w1)
            lw = swin.get(s_, {}).get("window_ms")
            if lw and len(lw) == 2:                                    # the capture's own stated history window beats the record fields
                a0, a1 = max(w0, int(lw[0])), min(w1, int(lw[1]))
            need[s_] = (a0, a1, _iv_ms(ds, interval, eligible[s_]))
        rd, per = v["rd"], {}
        if rd["present"]:
            man = rd["manifest"]
            for s_ in esyms:
                per[s_] = assess_symbol(eligible[s_], man["symbols"].get(s_), (need[s_][0], need[s_][1]), need[s_][2], rd["verified"] is not False)
        else:
            for s_ in esyms:
                per[s_] = {"status": "MISSING", "missing_ranges": [[need[s_][0], need[s_][1]]], "reasons": ["dataset_absent" if root_abs is not None else "no_history_root"]}
        v["need"], v["per"] = need, per
    # ---- staged exclusions of ELIGIBLE symbols, in the loader's order (first failing check names the reason)
    stage_excluded: Dict[str, str] = {}
    stages: List[Dict[str, Any]] = [{"stage": "1_universe_rows_linear", "count": len(recs), "symbols": _cap(sorted(recs))},
                                    {"stage": "2_eligible_lifecycle", "count": len(esyms), "symbols": _cap(esyms)}]
    alive = list(esyms)
    for i, ds in enumerate(required, 3):
        view = ds_view[ds]
        keep = []
        for s in alive:
            if not view["rd"]["present"] or s not in sets.get(ds, set()):
                stage_excluded[s] = f"not_in_{ds.replace('-kline', '')}_dataset" if ds in (MH.MARK, MH.INDEX) else f"not_in_{ds}_dataset"
            else:
                keep.append(s)
        alive = keep
        stages.append({"stage": f"{i}_in_{ds}_dataset", "count": len(alive), "symbols": _cap(alive)})
    n = len(required) + 3
    mans = {ds: ds_view[ds]["rd"].get("manifest") for ds in required}
    names = {ds: ("funding" if ds == MH.FUNDING else ds_view[ds]["dirname"]) for ds in required}
    keep = []
    for s in alive:
        sts = {names[ds]: mans[ds]["symbols"][s].get("status") for ds in required}
        badst = {k: v for k, v in sts.items() if v in EXCLUDED_SYMBOL_STATUSES or v is None}
        if badst:
            stage_excluded[s] = "symbol_status:" + ",".join(f"{k}={v}" for k, v in sorted(badst.items()))
        else:
            keep.append(s)
    alive = keep
    stages.append({"stage": f"{n}_status_not_conflicted_or_unverified", "count": len(alive), "symbols": _cap(alive)})
    keep = []
    for s in alive:
        if any(not (mans[ds]["symbols"][s].get("coverage") or {}).get("rows") for ds in required):
            stage_excluded[s] = "no_rows"
        else:
            keep.append(s)
    alive = keep
    stages.append({"stage": f"{n + 1}_has_rows", "count": len(alive), "symbols": _cap(alive)})
    keep = []
    if MH.FUNDING in required and alive:
        import candidate_readiness as CR            # lazy: candidate_readiness imports this module lazily too
        fdir = ds_view[MH.FUNDING]["rd"]["dir"]
        for s in alive:
            segs, why = CR.funding_interval_segments(mans[MH.FUNDING]["symbols"][s], _row_ts(os.path.join(fdir, s, "rows.jsonl")))
            if segs is None:
                stage_excluded[s] = why or "funding_interval_unknown"
            else:
                keep.append(s)
        alive = keep
    stages.append({"stage": f"{n + 2}_funding_interval_known_for_every_print", "count": len(alive), "symbols": _cap(alive)})
    loadable = sorted(alive)
    complete = sorted(s for s in loadable if all(ds_view[ds]["per"][s]["status"] == "COMPLETE" and ds_view[ds]["rd"].get("verified") is True for ds in required))
    stages.append({"stage": f"{n + 3}_loadable", "count": len(loadable), "symbols": _cap(loadable)})
    stages.append({"stage": f"{n + 4}_complete_over_the_required_window", "count": len(complete), "symbols": _cap(complete)})
    # why every symbol that sits in a required dataset but is not loadable is out
    in_any = set().union(*(sets.get(ds, set()) for ds in required)) if required else set()
    ds_reasons: Dict[str, str] = {}
    for s in sorted(in_any - set(loadable)):
        ds_reasons[s] = "not_in_universe" if s not in recs else (cl["excluded"][s] if s in cl["excluded"] else stage_excluded.get(s, "unknown"))
    lc_ex = cl["excluded"]
    ended_unaccounted = {s: (stage_excluded.get(s) or lc_ex.get(s) or "not_loaded") for s, r in sorted(recs.items())
                         if r.get("status") in ENDED and s not in set(loadable) and (s in stage_excluded or (s in lc_ex and not exclusion_is_explained(lc_ex[s])))}
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
    by_reason: Dict[str, List[str]] = {}
    for s, why in {**cl["excluded"], **stage_excluded}.items():
        by_reason.setdefault(_reason_key(why), []).append(s)
    rep = {"schema": FUNNEL_SCHEMA, "window": {"from_ms": w0, "to_ms": w1}, "universe_manifest_sha256": uinfo.get("manifest_sha256"),
           "include_statuses": include, "quote_coins": list(quote_coins) if quote_coins else None, "required_datasets": required,
           "stages": stages, "eligible": esyms, "excluded_lifecycle": dict(sorted(cl["excluded"].items())), "excluded_stage": dict(sorted(stage_excluded.items())),
           "excluded_by_reason": {r: sorted(v) for r, v in sorted(by_reason.items())}, "loadable": loadable, "complete": complete,
           "symbols_in_datasets_not_loaded": dict(sorted(ds_reasons.items())), "ended_unaccounted": ended_unaccounted,
           "lifecycle_authority": authority, "lifecycle_class_counts": ((scope or {}).get("lifecycle") or {}).get("class_counts"), "scope_usable": (scope or {}).get("usable"),
           "scope_dataset_status": (scope or {}).get("dataset_status"), "history_evidence_symbols": sorted(evidence), "symbol_set_mismatch_datasets": sorted(mismatch)}
    rep["funnel_digest"] = MH._sha(MH._canon(rep))              # the digest covers WHAT the funnel decided, not how the window was named: the same inputs give the same digest in every tool
    rep["window_source"] = window_source
    rep["datasets"] = {ds: {"dirname": v["dirname"], "interval": v["interval"], "present": bool(v["rd"]["present"]), "verified": v["rd"].get("verified"), "required": v["required"],
                            "symbols_in_dataset": len(sets.get(ds, ())) if v["rd"]["present"] else None} for ds, v in ds_view.items()}
    return Funnel(window=(w0, w1), window_source=window_source, records=recs, uinfo=uinfo, lifecycle=cl, eligible=esyms, datasets=ds_view, symbol_sets=sets, symbol_set_mismatch=mismatch,
                  stage_excluded=stage_excluded, ended_unaccounted=ended_unaccounted, loadable=loadable, complete=complete, required=required, include_statuses=include, intervals=iv, _report=rep, _dataset_symbol_reasons=ds_reasons)


def _reason_key(why: str) -> str:
    return why.split("(")[0].split(":")[0]



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


# ---------------------------------------------------------------- required datasets of a candidate spec

SPEC_UNVERIFIABLE_KINDS = ("depth", "cross_venue")        # kinds no collector serves whatever else the item also names; an item that maps to NO dataset is unverifiable too


def datasets_for_spec(spec: Mapping[str, Any]) -> Dict[str, Any]:
    """The market_history datasets a candidate spec's ``required_data`` needs: funding, mark-kline, index-kline, kline, open-interest, plus the spot-instruments snapshot (spot availability).
    Items no collector here can serve (order-book depth, other venues, fees/borrow) are listed as UNVERIFIABLE and keep ``complete`` False: a spec is never called covered because a dataset
    for a DIFFERENT need exists. Pure; read from the spec's own item names."""
    import candidate_readiness as CR
    wanted: set = set()
    spot = False
    unver: List[Dict[str, Any]] = []
    items: List[Dict[str, Any]] = []
    for d in spec.get("required_data", []) if isinstance(spec, Mapping) else []:
        if not isinstance(d, dict):
            continue
        nm = str(d.get("name", ""))
        low = nm.lower()
        kinds = list(CR.classify_required(nm))
        ds: List[str] = []
        if "funding" in kinds:
            ds.append(MH.FUNDING)
        if "mark" in kinds:
            ds.append(MH.MARK)
        if "index price" in low or "mark/index" in low:
            ds.append(MH.INDEX)
        if "spot_price" in kinds and MH.INDEX not in ds:
            ds.append(MH.INDEX)                                          # the venue's spot INDEX series: a proxy for the spot leg, never a spot price
        if any(k in low for k in ("kline", "candle", "ohlc")):
            ds.append(MH.KLINE)
        if "open interest" in low or "open-interest" in low:
            ds.append(MH.OI)
        if "spot" in low or "hedge" in low or "hedgeab" in low:
            spot = True
        bad = [k for k in kinds if k in SPEC_UNVERIFIABLE_KINDS]
        if bad or (not ds and "universe" not in kinds and not spot):
            unver.append({"item": nm, "kinds": kinds, "why": "no collector or capture schema in this repository serves it"})
        wanted.update(ds)
        items.append({"item": nm, "kinds": kinds, "datasets": ds})
    ordered = [d for d in DATASET_ORDER if d in wanted]
    return {"datasets": ordered, "spot_availability": spot, "unverifiable": unver, "items": items,
            "note": "derived from the spec's required_data names; index-kline stands in for a spot price only as a labelled proxy"}


def history_shortfall(doc_window: Mapping[str, Any], datasets: Mapping[str, Any], required: Sequence[str]) -> Dict[str, Any]:
    """What remains, exactly and why, when the collected history cannot cover the required span: per required dataset the window it was REQUESTED for, the days it falls short by, and the
    symbols short for each cause (collected window shorter than required / the venue served no earlier rows / dataset absent)."""
    need_days, w0, w1 = doc_window["effective_days"], doc_window["effective_from_ms"], doc_window["effective_to_ms"]
    out: Dict[str, Any] = {"required_days": need_days, "required_from_ms": w0, "required_to_ms": w1, "datasets": {}}
    worst = 0.0
    for ds in required:
        e = datasets[ds]
        if not e["present"]:
            out["datasets"][ds] = {"cause": "DATASET_ABSENT", "short_by_days": need_days}
            worst = max(worst, need_days)
            continue
        req = e.get("request") or {}
        have = ((req.get("end_ms") or 0) - (req.get("start_ms") or 0)) / DAY_MS if req.get("start_ms") and req.get("end_ms") else None
        short = max(0.0, need_days - have) if have is not None else None
        causes: Dict[str, List[str]] = {}
        for sym, m in (e.get("missing_ranges") or {}).items():
            for r in m["reasons"]:
                if r in ("window_short_start", "window_short_end", "partial_without_a_locatable_range"):
                    causes.setdefault(r, []).append(sym)
        head = sorted(s for s, m in (e.get("missing_ranges") or {}).items() if m["status"] != "MISSING" and "window_short_start" not in m["reasons"] and m["ranges_ms"] and m["ranges_ms"][0][0] == w0)
        if head:
            causes["venue_served_no_rows_for_the_head_of_the_window"] = head
        out["datasets"][ds] = {"collected_request_window": [req.get("start_ms"), req.get("end_ms")], "collected_days": round(have, 3) if have is not None else None,
                               "short_by_days": round(short, 3) if short is not None else None, "causes": {k: _cap(sorted(v)) for k, v in sorted(causes.items())}}
        worst = max(worst, short or 0.0)
    out["short_by_days_max"] = round(worst, 3)
    out["covers_required_span"] = worst == 0.0
    out["remaining"] = ("nothing: every required dataset was requested for the whole span" if worst == 0.0 else
                        f"{round(worst, 1)} more day(s) of history: the datasets must be collected for the whole {need_days}-day window ({w0}..{w1}); re-plan and run-plan into a NEW root")
    out["evidence_class"] = "FULL_SPAN_REQUESTED" if worst == 0.0 else "SHORT_DIAGNOSTIC_NOT_STRATEGY_EVIDENCE"
    return out


# ---------------------------------------------------------------- plan

def plan(universe_dir: str, history_root: Optional[str] = None, *, study_from_ms: Optional[int], study_to_ms: int, formation_days: int, hold_days: int, min_holding_rows: int = 60,
         warmup_days: int = 7, include_statuses: Sequence[str] = DEFAULT_STATUSES, quote_coins: Optional[Sequence[str]] = DEFAULT_QUOTES, intervals: Optional[Mapping[str, str]] = None,
         required_datasets: Sequence[str] = DEFAULT_REQUIRED, workers: int = MH.DEFAULT_WORKERS, rate: float = MH.DEFAULT_RATE, verify: bool = True,
         plan_path: Optional[str] = None, out_root: Optional[str] = None, now_ms: Optional[int] = None, classifier: Optional[Any] = None, candidate_spec: Optional[Mapping[str, Any]] = None,
         spot_dir: Optional[str] = None) -> Dict[str, Any]:
    cand = datasets_for_spec(candidate_spec) if candidate_spec is not None else None
    if cand is not None:
        required_datasets = tuple(cand["datasets"]) or tuple(required_datasets)
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
    fn = funnel(universe_dir, history_root, window=(w0, w1), window_source=WINDOW_FROM_REQUIRED, include_statuses=requested_statuses, quote_coins=quote_coins, required_datasets=required_datasets,
                intervals=iv, verify=verify, classifier=classifier)
    recs, uinfo, cl, eligible, esyms = fn.records, fn.uinfo, fn.lifecycle, {s: fn.records[s] for s in fn.eligible}, fn.eligible
    blockers: List[Dict[str, str]] = []
    caveats: List[str] = []
    if not esyms:
        blockers.append({"code": "NO_ELIGIBLE_SYMBOLS", "detail": "no instrument of the verified universe overlaps the window under these filters"})
    if not win["study_window_sufficient"]:
        blockers.append({"code": "STUDY_WINDOW_TOO_SHORT", "detail": f"study span {win['study_span_days']}d < needed {win['needed_days']}d; the plan collects {win['effective_days']}d regardless"})
    if not win["study_window_sufficient_after_holdout_reserve"]:
        caveats.append(f"HOLDOUT: a {win['study_span_days']}-day study span leaves {win['explore_days_after_holdout_reserve']} days to explore after the {HOLDOUT_RESERVE_FRACTION:.0%} holdout reserve, short of the "
                       f"{win['needed_days']} days one registered evaluation needs: history_holdout --reserve will refuse it; collect at least {win['needed_days_after_holdout_reserve']} days")
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
    for ds in DATASET_ORDER:
        v = fn.datasets[ds]
        rd, per, need, interval = v["rd"], v["per"], v["need"], v["interval"]
        entry: Dict[str, Any] = {"dataset": ds, "interval": interval, "dirname": dirs[ds], "required": ds in required_datasets, "present": bool(rd["present"])}
        counts = {k: 0 for k in ("COMPLETE", "PARTIAL", "STALE", "CONFLICTED", "UNVERIFIED", "MISSING")}
        if rd["present"]:
            man = rd["manifest"]
            entry.update(dataset_status=man.get("dataset_status"), manifest_digest=man.get("manifest_digest"), verified=rd["verified"], verify_problems=rd["verify_problems"],
                         request=man.get("request"), collected_with_universe=(man.get("universe") or {}).get("dir") if isinstance(man.get("universe"), dict) else None,
                         collected_with_included_statuses=(man.get("universe") or {}).get("included_statuses") if isinstance(man.get("universe"), dict) else None,
                         selection=selection_of(man))
        for s, a in per.items():
            counts[a["status"]] += 1
        todo = sorted(s for s, a in per.items() if a["status"] != "COMPLETE")
        by_status: Dict[str, Any] = {k: _cap([s for s in esyms if per[s]["status"] == k]) for k in counts if counts[k]}
        listed = {s: {"status": per[s]["status"], "ranges_ms": per[s]["missing_ranges"], "reasons": per[s]["reasons"][:6]} for s in todo[:SYMBOLS_WITH_RANGES_CAP]}
        entry.update(counts=counts, symbols_by_status=by_status, to_collect=_cap(todo), missing_ranges=listed, missing_ranges_truncated=len(todo) > SYMBOLS_WITH_RANGES_CAP,
                     estimate=_estimate(ds, interval, need, todo, workers, rate))
        datasets[ds] = entry
    mismatch = fn.symbol_set_mismatch
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
    spot_cov: Optional[Dict[str, Any]] = None
    if cand is not None:
        for u in cand["unverifiable"]:
            unmet.append(f"UNVERIFIABLE required data: {u['item'][:80]} ({','.join(u['kinds'])}): {u['why']}")
        if cand["spot_availability"]:
            if spot_dir is None:
                spot_cov = {"status": "NOT_SUPPLIED"}
                unmet.append("spot availability: no --spot-dir (spot-instruments snapshot) supplied")
            else:
                try:
                    h = MH.hedge_availability({s: recs[s] for s in esyms}, spot_dir)
                    spot_cov = {"status": "SUPPLIED", "counts": h["counts"], "snapshot": h["spot_snapshot"], "basis": "CURRENT_SNAPSHOT_ONLY"}
                except (MH.HistoryError, OSError, ValueError, KeyError) as exc:
                    spot_cov = {"status": "REFUSED", "reason": str(exc)[:200]}
                    unmet.append("spot availability: the spot snapshot is refused")
    shortfall = history_shortfall(win, datasets, [d for d in DATASET_ORDER if d in required_datasets])
    if not shortfall["covers_required_span"]:
        unmet.append(f"history shorter than the required {win['effective_days']} days by {shortfall['short_by_days_max']} day(s): a run over it is a SHORT_DIAGNOSTIC, never strategy evidence")
    research_ready = bool(esyms) and not unmet and not blockers
    # commands
    commands = _commands(universe_dir, history_root, out_root, plan_path, win, include_statuses, iv, workers, rate, datasets, required_datasets)
    total_req = sum(datasets[d]["estimate"]["requests_total"] for d in DATASET_ORDER if datasets[d]["required"])
    rem_req = sum(datasets[d]["estimate"]["requests_remaining"] for d in DATASET_ORDER if datasets[d]["required"])
    rem_all = sum(datasets[d]["estimate"]["requests_remaining"] for d in DATASET_ORDER)
    doc = {"schema": SCHEMA,
           "parameters": {"study_from_ms": study_from_ms, "study_to_ms": study_to_ms, "formation_days": formation_days, "hold_days": hold_days, "min_holding_rows": min_holding_rows,
                          "warmup_days": warmup_days, "include_statuses": list(include_statuses), "include_statuses_requested": requested_statuses, "quote_coins": list(quote_coins) if quote_coins else None, "intervals": iv,
                          "required_datasets": list(required_datasets), "workers": workers, "rate": rate, "candidate_id": (candidate_spec or {}).get("candidate_id") if candidate_spec is not None else None,
                          "spot_dir": spot_dir},
           "window": win,
           "universe": {"dir": uinfo.get("dir"), "manifest_sha256": uinfo.get("manifest_sha256"), "dataset_status": uinfo.get("dataset_status"), "capture_schema": uinfo.get("capture_schema"),
                        "snapshot_ms": uinfo.get("snapshot_ms"), "rows_linear": len(recs), "statuses_in_capture": uinfo.get("statuses_in_capture"), "status_coverage": uinfo.get("status_coverage"),
                        "delisting_observability": uinfo.get("delisting_observability")},
           "eligible_count": len(esyms), "eligible_symbols": esyms,
           "eligible_by_status": {k: sum(1 for s in esyms if eligible[s].get("status") == k) for k in sorted({eligible[s].get("status") for s in esyms}, key=str)},
           "eligible_notes": {k: v for k, v in sorted(cl["notes"].items())},
           "excluded_count": len(cl["excluded"]), "excluded_by_reason": {r: _cap(v) for r, v in sorted(cl["by_reason"].items())}, "excluded_detail": dict(sorted(cl["excluded"].items())),
           "funnel": fn.report(), "candidate": ({"required": cand, "spot_coverage": spot_cov} if cand is not None else None), "history_shortfall": shortfall,
           "evidence_class": shortfall["evidence_class"], "history_root": os.path.abspath(history_root) if history_root else None, "datasets": datasets, "symbol_set_mismatch": mismatch,
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
    spec = None
    if p.get("candidate_id"):
        import candidate_readiness as CR
        spec = next((sp for _n, sp in CR.load_candidates() if sp.get("candidate_id") == p["candidate_id"]), None)
        if spec is None:
            raise PlanError(f"the sealed plan names candidate {p['candidate_id']!r}, which is not among the research candidates")
    fresh = plan(doc["universe"]["dir"], history_root, study_from_ms=p["study_from_ms"], study_to_ms=p["study_to_ms"], formation_days=p["formation_days"], hold_days=p["hold_days"],
                 min_holding_rows=p["min_holding_rows"], warmup_days=p["warmup_days"], include_statuses=p.get("include_statuses_requested") or p["include_statuses"],
                 quote_coins=p.get("quote_coins"), intervals=p["intervals"], required_datasets=p["required_datasets"], workers=p["workers"], rate=p["rate"], verify=verify,
                 candidate_spec=spec, spot_dir=p.get("spot_dir"))
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
             data_root: Optional[str] = None, classifier: Optional[Any] = None) -> Dict[str, Any]:
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
    include_statuses = effective_statuses(include_statuses)
    if recs:
        w0, w1 = win["effective_from_ms"], win["effective_to_ms"]
        required = [MH.FUNDING, MH.MARK] if mark_name else [MH.FUNDING]
        ivs = {MH.MARK: mark_name[len(MH.MARK) + 1:]} if mark_name else {}
        fn = funnel(universe_dir, history_root, window=(w0, w1), window_source=WINDOW_FROM_REQUIRED, include_statuses=include_statuses, quote_coins=quote_coins, required_datasets=required,
                    intervals=ivs, report_datasets=required, classifier=classifier)
        res["funnel"] = fn.report()
        elig = set(fn.eligible)
        res["steps"] = [{"step": st["stage"], "count": st["count"], "symbols": st["symbols"], "note": ""} for st in fn.report()["stages"]]
        res["steps"][0]["note"] = f"statuses: {uinfo.get('statuses_in_capture')}"
        res["steps"][1]["note"] = "excluded: " + json.dumps({r: len(v) for r, v in sorted(fn.lifecycle["by_reason"].items())})
        non_complete = sorted(s for s in fn.loadable if s not in fn.complete)
        res["steps"][-2]["note"] = f"the loader loads exactly this list; PARTIAL/STALE symbols are KEPT and warned (not COMPLETE over the required window: {len(non_complete)})"
        res["steps"][-1]["note"] = "COMPLETE over the whole required window in every required dataset (verified datasets only)"
        loader = {**fn.lifecycle["excluded"], **fn.stage_excluded}
        res["loader_exclusions"] = {"by_reason": _tally(loader.values()), "examples": {r: _cap(sorted(s for s, w in loader.items() if _reason_key(w) == r)) for r in sorted({_reason_key(w) for w in loader.values()})}}
        res["predicted_symbols_loaded"] = len(fn.loadable)
        res["selection"] = {"funding": selection_of(fund["manifest"]) if fund["present"] else None, mark_name or "mark-kline": selection_of(mark["manifest"]) if mark["present"] else None}
        if fund["present"] and mark["present"]:
            res["loader_run"] = _run_loader(CR, RH, history_root, universe_dir or "", mark_name[len(MH.MARK) + 1:] if mark_name else None, (w0, w1))
            actual = res["loader_run"].get("symbols_loaded")
            same = res["loader_run"].get("symbols") == fn.loadable if res["loader_run"].get("symbols") is not None else None
            res["funnel_matches_loader"] = ((actual == len(fn.loadable)) and same is not False) if actual is not None else None
            if res["funnel_matches_loader"] is False:
                blockers.append({"code": "FUNNEL_DISAGREES_WITH_LOADER", "detail": f"predicted {len(fn.loadable)} but candidate_readiness.load_history_root loaded {actual}: do not trust this funnel"})
        fsyms = fn.symbol_sets.get(MH.FUNDING, set())
        missing_f = sorted(elig - fsyms)
        if missing_f:
            u = fund["manifest"].get("universe") if fund["present"] else None
            sel = selection_of(fund["manifest"]) if fund["present"] else {"mode": "UNKNOWN_LEGACY"}
            how = (f"collected with selection mode {sel['mode']}" + (" (an explicit --symbols list)" if sel["mode"] == "EXPLICIT_SYMBOLS" else "") +
                   ("" if not u else f"; universe {u.get('dir')} with included_statuses={u.get('included_statuses')}"))
            blockers.append({"code": "HISTORY_COVERS_FEW_ELIGIBLE_SYMBOLS", "detail": f"{len(missing_f)} of {len(elig)} eligible symbols have no funding history; the funding dataset was {how}"})
        span = _span_days(fund, mark)
        res["history_span_days"] = span
        res["holding_rows_estimate"] = _holding_rows(fund, formation_days, hold_days)
        if res["holding_rows_estimate"] is not None and res["holding_rows_estimate"] < min_holding_rows:
            blockers.append({"code": "TOO_FEW_HOLDING_ROWS", "detail": f"history supports ~{res['holding_rows_estimate']} non-overlapping {hold_days}d holding rows after a {formation_days}d formation lead-in; need {min_holding_rows} "
                             f"(window needed: {win['needed_days']}d)"})
    params = dict(study_to_ms=study_to_ms, study_from_ms=study_from_ms, formation_days=formation_days, hold_days=hold_days, min_holding_rows=min_holding_rows, warmup_days=warmup_days,
                  include_statuses=list(include_statuses), quote_coins=list(quote_coins) if quote_coins else None, mark_interval=(mark_name[len(MH.MARK) + 1:] if mark_name else None))
    res["why_few_symbols"] = why_few_symbols(res, fund, mark, universe_dir, history_root, params)
    res["explanation"] = _explain(res)
    res["first_blocker"] = blockers[0] if blockers else None
    return res


def _cli_params(params: Mapping[str, Any]) -> str:
    out = [f"--study-to-ms {params['study_to_ms']}", f"--formation-days {params['formation_days']}", f"--hold-days {params['hold_days']}", f"--min-holding-rows {params['min_holding_rows']}",
           f"--warmup-days {params['warmup_days']}", "--include-statuses " + ",".join(x for x in params["include_statuses"] if x != "Delivering" or "Closed" not in params["include_statuses"])]
    if params.get("study_from_ms"):
        out.insert(1, f"--study-from-ms {params['study_from_ms']}")
    out.append("--quote-coins " + (",".join(params["quote_coins"]) if params.get("quote_coins") else ""))
    return " ".join(out)


def why_few_symbols(res: Mapping[str, Any], fund: Mapping[str, Any], mark: Mapping[str, Any], universe_dir: Optional[str], history_root: str, params: Mapping[str, Any]) -> Dict[str, Any]:
    """From the manifests alone: which of the four causes explains a small loaded cross-section, and the exact command sequence to reproduce it and to fix it into a NEW root.
    (a) the history was collected from an explicit --symbols list, (b) the universe dir was not supplied / not recognised, (c) lifecycle filtering, (d) dataset status.
    Each cause is APPLIES / DOES_NOT_APPLY / SUSPECTED (legacy manifest, no ``selection`` block) / UNKNOWN, with its evidence."""
    fr = res.get("funnel") or {}
    stages = {st["stage"].split("_", 1)[0]: st for st in fr.get("stages", [])}
    n_univ = (fr.get("stages") or [{"count": 0}])[0]["count"]
    n_elig = len(fr.get("eligible", []))
    n_load = len(fr.get("loadable", []))
    fsel = selection_of(fund["manifest"]) if fund.get("present") else {"mode": "UNKNOWN_LEGACY", "symbols_n": None}
    fman_u = fund["manifest"].get("universe") if fund.get("present") else None
    causes: List[Dict[str, Any]] = []
    sel_mode = fsel["mode"]
    if sel_mode == "EXPLICIT_SYMBOLS":
        a = "APPLIES"
    elif sel_mode == "UNKNOWN_LEGACY":
        a = "SUSPECTED" if not fman_u and fund.get("present") else ("DOES_NOT_APPLY" if fund.get("present") else "UNKNOWN")
    else:
        a = "DOES_NOT_APPLY"
    causes.append({"cause": "a_explicit_symbols_list", "verdict": a, "evidence": {"selection": fsel, "funding_symbols": len((fund.get("manifest") or {}).get("symbols") or {}) if fund.get("present") else None,
                                                                                      "eligible_symbols": n_elig, "universe_recorded_in_manifest": bool(fman_u)}})
    scan = res.get("data_scan") or {}
    ub = [b["code"] for b in res.get("blockers", []) if b["code"] in ("UNIVERSE_DIR_NOT_SUPPLIED", "UNIVERSE_REFUSED", "DATA_SCAN_FOUND_NO_UNIVERSE_CAPTURE")]
    causes.append({"cause": "b_universe_dir_not_supplied_or_recognised", "verdict": "APPLIES" if ub else "DOES_NOT_APPLY",
                   "evidence": {"blockers": ub, "scan_found_universe_capture_dirs": scan.get("universe_capture_dirs"), "sibling_manifests": [x["dir"] for x in scan.get("sibling_manifests_outside_scanned_root", [])]}})
    lc = (fr.get("excluded_by_reason") or {})
    lifecycle_n = len(fr.get("excluded_lifecycle", {}))
    lc_in_ds = sorted(x for x in fr.get("symbols_in_datasets_not_loaded", {}) if x in fr.get("excluded_lifecycle", {}))
    causes.append({"cause": "c_lifecycle_filtering", "verdict": "UNKNOWN" if not fr else ("APPLIES" if lc_in_ds else "DOES_NOT_APPLY"),
                   "evidence": {"universe_rows": n_univ, "eligible": n_elig, "excluded_lifecycle": lifecycle_n, "by_reason": {r: len(v) for r, v in sorted(lc.items())},
                                "collected_but_lifecycle_excluded": _cap(lc_in_ds),
                                "note": "APPLIES only when symbols that ARE in the datasets were dropped by the lifecycle rule; lifecycle exclusion of never-collected symbols shrinks the plan, not the loader"}})
    st_ex = {k: v for k, v in fr.get("excluded_stage", {}).items() if k in fr.get("symbols_in_datasets_not_loaded", {})}          # collected for SOME dataset yet not loadable; never-collected symbols are cause (a)'s symptom
    causes.append({"cause": "d_dataset_status", "verdict": "UNKNOWN" if not fr else ("APPLIES" if st_ex else "DOES_NOT_APPLY"),
                   "evidence": {"stage_exclusions_by_reason": _tally(st_ex.values()), "datasets": {k: {"present": v["present"], "verified": v["verified"]} for k, v in (fr.get("datasets") or {}).items()},
                                "examples": dict(list(st_ex.items())[:NAMES_CAP])}})
    primary = next((c["cause"] for c in causes if c["verdict"] == "APPLIES"), next((c["cause"] for c in causes if c["verdict"] == "SUSPECTED"), None))
    new_root = f"{history_root.rstrip('/')}-full-{params['study_to_ms']}"
    plan_path = f"{new_root}/plan.json"
    uni = universe_dir or "<UNIVERSE_CAPTURE_DIR>"
    cp = _cli_params(params)
    seq = [{"order": 1, "purpose": "reproduce this diagnosis (read-only, offline)", "command": f"python3 tools/backfill_plan.py diagnose --universe-capture {uni} --history-root {history_root} {cp}"},
           {"order": 2, "purpose": "seal a plan for the FULL eligible universe (read-only, offline; writes only PLAN.json under a NEW directory)",
            "command": f"mkdir -m 700 {new_root} && python3 tools/backfill_plan.py plan --universe-capture {uni} --history-root {history_root} {cp} --out {plan_path}"},
           {"order": 3, "purpose": "collect it dataset by dataset into the NEW root, resumable and rate-limited (PUBLIC read-only GETs, no credential; refuses an existing dataset)",
            "command": f"python3 tools/market_history.py run-plan --plan {plan_path} --out-root {new_root}/data --universe-capture {uni}"},
           {"order": 4, "purpose": "re-plan against the new root: research_ready and the funnel counts are decided here (offline)",
            "command": f"python3 tools/backfill_plan.py plan --universe-capture {uni} --history-root {new_root}/data {cp} --out {new_root}/replan.json"},
           {"order": 5, "purpose": "evaluate (offline) against the new root",
            "command": f"python3 tools/candidate_readiness.py --history-root {new_root}/data --universe-dir {uni}" + (f" --mark-interval {params['mark_interval']}" if params.get("mark_interval") else "")}]
    return {"primary_cause": primary, "causes": causes, "loaded_symbols": n_load, "eligible_symbols": n_elig, "universe_rows": n_univ, "command_sequence": seq,
            "note": "primary_cause is the first APPLIES (else SUSPECTED); SUSPECTED means a legacy manifest without a selection block: only a re-collection with this tree records the mode"}


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


def _run_loader(CR: Any, RH: Any, root: str, universe_dir: str, mark_interval: Optional[str], window: Optional[Tuple[int, int]] = None) -> Dict[str, Any]:
    try:
        series, info = CR.load_history_root(root, universe_dir=universe_dir, mark_interval=mark_interval, window=window)
    except RH.HarnessError as exc:
        return {"refused": str(exc)[:300], "symbols_loaded": None}
    except Exception as exc:                          # noqa: BLE001 - a diagnosis reports a loader crash, it does not die with it
        return {"refused": f"{type(exc).__name__}: {str(exc)[:200]}", "symbols_loaded": None}
    return {"symbols_loaded": len(series), "symbols": sorted(x.symbol for x in series), "funnel_digest": (info.get("funnel") or {}).get("funnel_digest"), "excluded": len(info.get("excluded", [])), "warnings": list(info.get("warnings", []))[:5]}


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
    p.add_argument("--candidate", default=None, help="a research candidate name (research/candidates/<name>.json): its required_data decides the required datasets (overrides --require)")
    p.add_argument("--spot-dir", default=None, help="a spot-instruments snapshot dir (spot availability coverage)")
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
            spec = None
            if a.candidate:
                import candidate_readiness as CR
                spec = next((sp for n, sp in CR.load_candidates() if n == a.candidate), None)
                if spec is None:
                    raise PlanError(f"no research candidate named {a.candidate!r}")
            doc = plan(a.universe_capture, a.history_root, candidate_spec=spec, spot_dir=a.spot_dir, **common, intervals={MH.KLINE: a.kline_interval, MH.MARK: a.mark_interval, MH.INDEX: a.index_interval, MH.OI: a.oi_interval},
                       required_datasets=_csv(a.require), workers=a.workers, rate=a.rate, verify=not a.no_verify, plan_path=a.out, now_ms=now)
            if a.out:
                MH._write_atomic(a.out, (json.dumps(doc, indent=1, sort_keys=True, allow_nan=False) + "\n").encode())
            print(json.dumps({k: doc[k] for k in ("schema", "window", "eligible_count", "excluded_count", "estimate", "blockers", "caveats", "readiness", "plan_digest", "evidence_class", "history_shortfall")}
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
