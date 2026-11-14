#!/usr/bin/env python3
"""Candidate readiness: for every research candidate (research/candidates/*.json plus the frozen BTC carry description), what is actually known, and the honest verdict.

    python3 tools/candidate_readiness.py [--data-root DIR] [--json OUT] [--history-root ROOT --universe-dir DIR [--spot-dir DIR] [--mark-interval IV]]

Offline, read-only. It never reads a credential, the network, bot/state or a database, and it writes nothing unless --json names a file. For each candidate it answers,
with a reason and a data basis for each: required data present (real manifests under --data-root; absent = MISSING, never assumed), point in time, universe complete
enough for the claim, a stated economic mechanism and counterparty, executable through the CURRENTLY QUALIFIED venue capabilities (Bybit only; cross-venue is
RESEARCH_ONLY and never executable), edge against fees + spread + slippage + funding (UNKNOWN when an input is unknown), price-leg / hedge risk measured, capacity supported
by depth, the multiple-testing family size, whether out-of-sample / prospective evidence is possible, and a final verdict from the ladder
(UNTESTED / INSUFFICIENT_DATA / REJECTED). Nothing here promotes a candidate: a verdict above INSUFFICIENT_DATA needs walk-forward and prospective evidence this tool cannot create.

Labels used everywhere: COMMITTED_CORPUS_IN_SAMPLE (data in this repo, the gate constants were chosen with all of it visible), MODELLED (a stated execution scenario, never
measured), ARITHMETIC_ON_DECLARED_INPUTS, OWNER_OBSERVED (a live probe by the owner, unverifiable here), TRAILING_IN_SAMPLE_OBSERVATION (not an expectation).

Provenance labels, stated plainly. ``RAW_RESPONSES_VERIFIED`` (research_harness / universe_capture) means SELF-CONSISTENT BYTES AS CAPTURED BY THE OWNER: hashes, cursor chains and
re-derived coverage agree. It is NOT authenticated as produced by the exchange. ``spot_listed`` / hedgeability come from ONE CURRENT spot snapshot (``CURRENT_SNAPSHOT_ONLY``): they label legs
and never take part in any historical ranking or selection decision.

Also here: ``evaluate_ranked_funding``, the evaluator for "rank perps by trailing funding, short the top" on market_history captures: it separates the FORMATION window from
the HOLDING window, counts every ranking-rule variant in the multiple-testing family, normalises funding to bps per day per symbol, reports PERP-ONLY symbols (no Bybit spot
pair) only as directional price+funding P&L (never as carry), and refuses a verdict above INSUFFICIENT_DATA.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import carry_costs as CC  # noqa: E402
import cash_basis as CB  # noqa: E402
import economic_model as EM  # noqa: E402
import opportunity_map as OM  # noqa: E402
import research_harness as RH  # noqa: E402
import statistical_power as SP  # noqa: E402
import strategy_verdict as SV  # noqa: E402
import venues  # noqa: E402
import market_history as MH  # noqa: E402
import universe_capture as UC  # noqa: E402

SCHEMA = "candidate_readiness/v1"
CAND_DIR = os.path.join(BOT, "research", "candidates")
MISSING = "MISSING"
UNKNOWN = "UNKNOWN"
RESEARCH_ONLY = "RESEARCH_ONLY"
LABEL_CORPUS = "COMMITTED_CORPUS_IN_SAMPLE"
LABEL_MODELLED = "MODELLED"
LABEL_ARITH = "ARITHMETIC_ON_DECLARED_INPUTS"
LABEL_OWNER = "OWNER_OBSERVED"
LABEL_TRAILING = "TRAILING_IN_SAMPLE_OBSERVATION"
OWNER_INTERVAL_NOTE = ("OWNER_OBSERVED (unverified here): Bybit funding interval is per symbol (480m, 240m, sometimes 60m); 14 of the top 20 positive-funding perps over $5M "
                       "turnover were 240m; ~26% of historical USDT perps are Closed (318 delisted vs 890 Trading)")
#: candidates whose edge is the BTC funding carry the committed Bybit corpus can test
CORPUS_CARRY = ("cash_and_carry", "btcusdt_funding_carry_overlay")
HOLDS_PRINTS = (1, 3, 9, 17, 51)          # 8h, 1d, 3d, ~5.8d (the demonstrated hold), 17d
PIT_LABEL_NOTE = ("RAW_RESPONSES_VERIFIED means self-consistent bytes as captured by the owner (hashes, cursor chains and re-derived coverage agree); it is NOT authenticated as produced "
                  "by the exchange")
HEDGE_BASIS = "CURRENT_SNAPSHOT_ONLY"       # spot_listed / hedgeability come from ONE current spot snapshot: a label, never an input to a historical decision
SURVIVORS_ONLY = "SURVIVORS_ONLY"
DELISTINGS_OBSERVED = "DELISTINGS_OBSERVED"
PARTICIPATION_CAP = 0.01                  # ASSUMED share of open interest one position may be (labelled assumed wherever used)


# ---------------------------------------------------------------- authoritative costs (adapter: sub-agent 4 owns the single definition)
def cost_constants() -> Dict[str, Any]:
    """The cost constants exactly as carry_costs defines them (the single authority). The 0.4 bps spread is ASSUMED (unmeasured); the fee is venue_stated (base tier)."""
    ov = CC.position_class_costs("overlay_owned_spot")
    acq = CC.position_class_costs("acquire_spot_perp")
    return {"perp_taker_bps": CC.TAKER_BPS_PERP, "spot_taker_bps": CC.TAKER_BPS_SPOT,
            "perp_round_trip_fee_bps": float(CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY), "assumed_round_trip_spread_bps": float(CC.ASSUMED_ROUND_TRIP_SPREAD_BPS),
            "perp_round_trip_total_bps": float(CC.ROUND_TRIP_TOTAL_COST_BPS_PERP_OVERLAY), "spot_perp_round_trip_fee_bps": float(CC.ROUND_TRIP_FEE_BPS_SPOT_PERP),
            "spot_perp_round_trip_total_bps": acq["total_bps"], "overlay_provenance": ov["provenance"], "maker_modelled_round_trip_bps": float(CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY_MAKER_MODELLED),
            "basis": "carry_costs authoritative constants (fee venue_stated base tier; spread ASSUMED, unmeasured; maker MODELLED)", "spread_included": True,
            "note": "the 0.4 bps spread is an ASSUMPTION: the net figures carry it, they do not measure it"}


def scenarios(c: Mapping[str, Any]) -> Dict[str, float]:
    """Round-trip bps of the PERP legs for an overlay on owned BTC (no spot trades). MAKER_* are MODELLED (zero adverse selection, zero missed fills), not measured."""
    return {"TAKER_FEES_ONLY": c["perp_round_trip_fee_bps"], "TAKER_FEES_PLUS_SPREAD": c["perp_round_trip_total_bps"],
            "TAKER_FEES_PLUS_ASSUMED_SLIPPAGE_1BPS_PER_LEG": c["perp_round_trip_fee_bps"] + 2.0,
            "MAKER_FIRST_MODELLED_entry_maker_exit_taker": float(CC.MAKER_BPS_PERP_MODELLED) + float(CC.TAKER_BPS_PERP),
            "ALL_MAKER_MODELLED_best_case_unreachable": c["maker_modelled_round_trip_bps"]}


# ---------------------------------------------------------------- candidates
def load_candidates() -> List[Tuple[str, Dict[str, Any]]]:
    out = []
    try:
        names = sorted(os.listdir(CAND_DIR))
    except OSError as exc:       # Round 18 (red team): the autopilot image ships these modules but not research/candidates: a traceback is not a refusal
        raise RH.HarnessError(f"CANDIDATE_SPECS_NOT_SHIPPED: the candidate specifications directory {CAND_DIR!r} is not readable here ({type(exc).__name__}); "
                              "nothing was evaluated (the research chain needs the specs, a verified history root and a universe capture)") from exc
    for fn in names:
        if fn.endswith(".json"):
            with open(os.path.join(CAND_DIR, fn), encoding="utf-8") as f:
                out.append((fn[:-5], json.load(f)))
    out.append(("btcusdt_funding_carry_overlay", SV.btc_carry_candidate()))
    return out


# ---------------------------------------------------------------- real manifests under --data-root
def _read_manifest(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "rb") as f:
            m = json.loads(f.read().decode("utf-8", "strict"))
        return m if isinstance(m, dict) else None
    except (OSError, ValueError, RecursionError):
        return None


WARMUP_DAYS = 7                       # backfill_plan's registered warm-up; the required span is ITS number (backfill_plan.required_window), never re-derived here


def required_span(formation_days: int, hold_days: int, min_rows: int = 60, warmup_days: int = WARMUP_DAYS) -> Dict[str, Any]:
    """The exact history span 60 independent holding rows need: warmup + formation + min_rows x hold (backfill_plan.required_window, 7 + 30 + 60 x 7 = 457 days at the registered values)."""
    import backfill_plan as BP
    w = BP.required_window(None, 1, formation_days, hold_days, min_rows, warmup_days)
    return {"needed_days": w["needed_days"], "components": w["components"], "formula": w["formula"]}


def resolve_universe_dir(path: Any) -> Tuple[Optional[str], Optional[str]]:
    """(dir, None) for a universe_capture directory, or for a PARENT dir holding exactly one capture below it; (None, reason) otherwise (ambiguous parents are refused, never guessed)."""
    if not isinstance(path, str) or not path or not os.path.isdir(path) or os.path.islink(path):
        return None, "not a directory"
    m = _read_manifest(os.path.join(path, "manifest.json"))
    sch = None if m is None else (m.get("schema") if m.get("schema") is not None else m.get("schema_version"))
    if sch == RH.UNIVERSE_CAPTURE_SCHEMA:
        return path, None
    kids = []
    for n in sorted(os.listdir(path))[:500]:
        d = os.path.join(path, n)
        if n == "state" or os.path.islink(d) or not os.path.isdir(d):
            continue
        mk = _read_manifest(os.path.join(d, "manifest.json"))
        if mk is not None and (mk.get("schema") == RH.UNIVERSE_CAPTURE_SCHEMA):
            kids.append(d)
    if len(kids) == 1:
        return kids[0], None
    if len(kids) > 1:
        return None, f"ambiguous: {len(kids)} universe captures below it ({[os.path.basename(k) for k in kids][:5]}); pass one"
    legacy = m is not None and m.get("schema_version") is not None
    return None, ("a LEGACY schema_version " + str(m.get("schema_version")) + " capture: no status partitions, never counted") if legacy else f"manifest schema is {sch!r}, not {RH.UNIVERSE_CAPTURE_SCHEMA}"


def scan_data_root(root: Optional[str], *, max_depth: int = 4, universe_dirs: Sequence[str] = ()) -> Dict[str, Any]:
    """Find capture manifests (schema universe_capture/v2, market_history/v1) under ``root``. No root = nothing was looked for = MISSING, not assumed present.

    ``universe_dirs`` are universe_capture directories passed EXPLICITLY (the cross-sectional path's required input): they are recognised wherever they live (inside or outside
    ``root``) and listed in ``universe_capture_dirs`` / ``universe_dirs_explicit``; a path that is not a universe_capture/v2 directory is listed in ``universe_dirs_rejected`` with the
    reason, never silently dropped. ``universe_dir_supplied`` is True iff at least one explicit directory was given and recognised."""
    res: Dict[str, Any] = {"root": root, "universe_capture_dirs": [], "universe_dirs_explicit": [], "universe_dirs_rejected": [], "universe_dir_supplied": False,
                           "market_history": [], "spot_snapshots": [], "checked": root is not None,
                           "legacy_universe_captures_not_counted": [], "sibling_universe_captures_outside_scanned_root": [], "findings": [],
                           "rule": "only manifests UNDER the scanned root are found (depth <= 4, no symlinks, 'state' skipped); a universe capture counts only when schema is exactly 'universe_capture/v2'"}
    explicit: List[str] = []
    for ud in universe_dirs or ():
        if not isinstance(ud, str) or not ud:
            res["universe_dirs_rejected"].append({"dir": str(ud), "reason": "empty universe dir"})
            continue
        resolved, why = resolve_universe_dir(ud)
        if resolved is None:
            res["universe_dirs_rejected"].append({"dir": ud, "reason": why})
            continue
        explicit.append(os.path.realpath(resolved))
    res["universe_dirs_explicit"] = sorted(set(explicit))
    res["universe_dir_supplied"] = bool(explicit)
    found: List[str] = list(explicit)
    if root is None or not os.path.isdir(root):
        res["checked"] = False
        res["universe_capture_dirs"] = sorted(set(found))
        return res
    base = os.path.realpath(root)
    for dp, dns, fns in os.walk(base):
        if dp[len(base):].count(os.sep) >= max_depth:
            dns[:] = []
        dns[:] = [d for d in dns if d != "state" and not os.path.islink(os.path.join(dp, d))]
        if "manifest.json" in fns:
            m = _read_manifest(os.path.join(dp, "manifest.json"))
            schema = None if m is None else (m.get("schema") if m.get("schema") is not None else m.get("schema_version"))
            if schema == RH.UNIVERSE_CAPTURE_SCHEMA:
                found.append(dp)
            elif m is not None and m.get("schema_version") is not None and m.get("schema") is None and (m.get("universe") or m.get("universe_file")):
                res["legacy_universe_captures_not_counted"].append({"dir": dp, "schema_version": m.get("schema_version")})
            elif schema == "market_history/v1" and m.get("dataset") == MH.SPOT:
                res["spot_snapshots"].append({"dir": dp, "dataset_status": m.get("dataset_status")})
            elif schema == "market_history/v1":
                res["market_history"].append({"dir": dp, "dataset_status": m.get("dataset_status"), "coverage": m.get("coverage")})
    res["universe_capture_dirs"] = sorted(set(found))
    parent = os.path.dirname(base)
    if os.path.isdir(parent):                                       # a capture BESIDE the scanned root (datasets/universe-... next to datasets/md-...) is invisible to a scan of the root
        for n in sorted(os.listdir(parent))[:200]:
            d = os.path.join(parent, n)
            if n == "state" or os.path.islink(d) or os.path.realpath(d) == base or not os.path.isfile(os.path.join(d, "manifest.json")):
                continue
            mk = _read_manifest(os.path.join(d, "manifest.json"))
            sch = None if mk is None else (mk.get("schema") if mk.get("schema") is not None else mk.get("schema_version"))
            if sch == RH.UNIVERSE_CAPTURE_SCHEMA or (mk is not None and mk.get("schema_version") is not None and mk.get("schema") is None):
                res["sibling_universe_captures_outside_scanned_root"].append({"dir": d, "schema": sch, "counted": False, "why": "beside, not under, the scanned root: pass it with --universe-dir"})
    if res["sibling_universe_captures_outside_scanned_root"] and not res["universe_capture_dirs"]:
        res["findings"].append("LOUD: universe_capture_dirs is empty because the universe capture lies BESIDE the scanned root, not under it: " + str([x["dir"] for x in res["sibling_universe_captures_outside_scanned_root"]][:3]) + "; pass --universe-dir (required by the cross-sectional path)")
    if res["legacy_universe_captures_not_counted"]:
        res["findings"].append("LOUD: legacy (schema_version 1) universe captures were found and NOT counted: they carry no status partitions, so they cannot show delistings")
    res["market_history"].sort(key=lambda x: x["dir"])
    res["spot_snapshots"].sort(key=lambda x: x["dir"])
    return res


def _needs_universe(spec: Mapping[str, Any]) -> bool:
    txt = (str(spec.get("hypothesis", "")) + " " + " ".join(str(d.get("name", "")) for d in spec.get("required_data", []) if isinstance(d, dict))).lower()
    return "universe" in txt or "delisted" in txt or "listing" in txt


def _iso(ms: Any) -> Optional[str]:
    import datetime as _dt
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return None
    return _dt.datetime.fromtimestamp(ms / 1000.0, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rel(path: str, root: Optional[str]) -> str:
    if root and os.path.isdir(root):
        try:
            r = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
            if not r.startswith(".."):
                return r
        except ValueError:
            pass
    return os.path.realpath(path)


def verified_inventory(scan: Mapping[str, Any]) -> Dict[str, Any]:
    """Re-verify, from the bytes, every capture the scan found (market_history ``verify_dataset``; universe ``load_universe_dir`` = ``verify_capture_dir`` + COMPLETE|PARTIAL).
    A manifest that merely exists and parses is NOT data: ``verified`` is the verifier's answer. Offline; reads only the supplied directories."""
    datasets: List[Dict[str, Any]] = []
    for m in scan.get("market_history", []):
        d = m["dir"]
        man = _read_manifest(os.path.join(d, "manifest.json")) or {}
        try:
            v = MH.verify_dataset(d)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            v = {"ok": False, "problems": [f"verify_dataset raised {type(exc).__name__}"]}
        syms = man.get("symbols") if isinstance(man.get("symbols"), dict) else {}
        firsts, lasts = [], []
        for r in syms.values():
            cov = r.get("coverage") if isinstance(r, dict) and isinstance(r.get("coverage"), dict) else {}
            if isinstance(cov.get("served_first_ts"), int) and isinstance(cov.get("served_last_ts"), int):
                firsts.append(cov["served_first_ts"])
                lasts.append(cov["served_last_ts"])
        top = m.get("coverage") if isinstance(m.get("coverage"), dict) else {}
        if not firsts and isinstance(top.get("first_ts"), int) and isinstance(top.get("last_ts"), int):
            firsts, lasts = [top["first_ts"]], [top["last_ts"]]
        by_status: Dict[str, int] = {}
        for r in syms.values():
            st = str(r.get("status")) if isinstance(r, dict) else "None"
            by_status[st] = by_status.get(st, 0) + 1
        datasets.append({"dir": d, "rel": _rel(d, scan.get("root")), "dataset": man.get("dataset"), "interval": man.get("interval"), "verified": bool(v.get("ok")),
                         "problems": [str(x)[:160] for x in v.get("problems", [])[:4]], "dataset_status": man.get("dataset_status"), "digest": man.get("manifest_digest"),
                         "symbols": sorted(syms), "by_symbol_status": dict(sorted(by_status.items())), "first_ts": min(firsts) if firsts else None, "last_ts": max(lasts) if lasts else None})
    universes: List[Dict[str, Any]] = []
    for d in scan.get("universe_capture_dirs", []):
        explicit = os.path.realpath(d) in set(scan.get("universe_dirs_explicit", []))
        try:
            recs, info = MH.load_universe_dir(d)
            universes.append({"dir": d, "rel": _rel(d, scan.get("root")), "explicit": explicit, "verified": True, "problems": [], "dataset_status": info.get("dataset_status"),
                              "digest": info.get("manifest_sha256"), "symbols": info.get("symbols"), "delisting_observability": info.get("delisting_observability"),
                              "status_coverage": info.get("status_coverage"), "statuses_in_capture": info.get("statuses_in_capture"), "snapshot_ms": info.get("snapshot_ms"),
                              "capture_schema": info.get("capture_schema")})
        except (MH.HistoryError, UC.CaptureError, OSError, ValueError, KeyError, TypeError) as exc:
            universes.append({"dir": d, "rel": _rel(d, scan.get("root")), "explicit": explicit, "verified": False, "problems": [str(exc)[:200]], "dataset_status": None, "digest": None,
                              "symbols": None, "delisting_observability": "NOT_OBSERVED", "status_coverage": None, "statuses_in_capture": None, "snapshot_ms": None, "capture_schema": None})
    spots: List[Dict[str, Any]] = []
    for sp in scan.get("spot_snapshots", []):
        man = _read_manifest(os.path.join(sp["dir"], "manifest.json")) or {}
        try:
            v = MH.verify_spot(sp["dir"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            v = {"ok": False, "problems": [f"verify_spot raised {type(exc).__name__}"]}
        spots.append({"dir": sp["dir"], "rel": _rel(sp["dir"], scan.get("root")), "verified": bool(v.get("ok")), "problems": [str(x)[:160] for x in v.get("problems", [])[:3]],
                      "dataset_status": man.get("dataset_status"), "digest": man.get("manifest_digest")})
    return {"datasets": datasets, "universes": universes, "spot_snapshots": spots}


def survivorship_of_universes(inv: Mapping[str, Any]) -> str:
    """DELISTINGS_OBSERVED only when a VERIFIED universe capture's Closed partition served rows; everything else is SURVIVORS_ONLY (fail closed)."""
    for u in inv.get("universes", []):
        cov = u.get("status_coverage") if isinstance(u.get("status_coverage"), Mapping) else {}
        if u.get("verified") and u.get("delisting_observability") == "OBSERVED" and cov.get("Closed") == "OBSERVED_ROWS":
            return DELISTINGS_OBSERVED
    return SURVIVORS_ONLY


def universe_assessment(spec: Mapping[str, Any], scan: Mapping[str, Any], inv: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    if not _needs_universe(spec):
        return {"status": "NOT_APPLICABLE_SINGLE_INSTRUMENT", "reason": "the candidate trades one named instrument (BTCUSDT), so there is no universe to survive"}
    if not scan["checked"] and not scan.get("universe_capture_dirs"):
        return {"status": MISSING, "reason": "UNIVERSE_DIR_REQUIRED: no --universe-dir and no --data-root given: no universe_capture manifest was looked for, so none is assumed"}
    if not scan["universe_capture_dirs"]:
        return {"status": MISSING, "reason": "UNIVERSE_DIR_REQUIRED: no universe_capture/v2 manifest under the data root and none passed with --universe-dir"}
    inv = inv if inv is not None else verified_inventory(scan)
    good = [u for u in inv["universes"] if u["verified"]]
    if not good:
        return {"status": "UNVERIFIED", "reason": "universe capture directories exist but none verifies (verify_capture_dir): " + "; ".join(p for u in inv["universes"] for p in u["problems"])[:300],
                "captures": len(scan["universe_capture_dirs"]), "survivorship": SURVIVORS_ONLY}
    cov = [(d["first_ts"], d["last_ts"]) for d in inv["datasets"] if d["verified"] and isinstance(d["first_ts"], int) and isinstance(d["last_ts"], int)]
    if not cov:
        return {"status": "UNVERIFIED", "reason": "universe captures verify but no verified market_history coverage fixes a study window, so coverage cannot be judged",
                "captures": len(scan["universe_capture_dirs"]), "survivorship": survivorship_of_universes(inv)}
    lo, hi = min(c[0] for c in cov), max(c[1] for c in cov)
    if hi <= lo:
        return {"status": "UNVERIFIED", "reason": "market_history coverage window is empty"}
    try:
        u = RH.PointInTimeUniverse.from_capture_dirs([x["dir"] for x in good], study_from_ms=lo, study_to_ms=hi)
    except RH.HarnessError as exc:
        return {"status": "UNVERIFIED", "reason": f"point-in-time universe refused: {str(exc)[:200]}", "survivorship": survivorship_of_universes(inv)}
    st = {RH.PIT_RAW: "VERIFIED_RAW_RESPONSES", RH.PIT_PARTIAL: "PARTIAL", RH.PIT_ASSERTED: "UNVERIFIED"}[u.pit_basis]
    return {"status": st, "pit_basis": u.pit_basis, "pit_basis_note": PIT_LABEL_NOTE, "problems": u.survivorship_problems()[:6], "unverified_segments": u.unverified_segments[:6],
            "study_window_ms": [lo, hi], "funding_normalisation_known": u.funding_normalisation_known, "captures": len(scan["universe_capture_dirs"]),
            "explicit": bool(scan.get("universe_dir_supplied")), "survivorship": survivorship_of_universes(inv),
            "closed_contracts_seen": len(u.delisted_between(0, 2 ** 62))}


# ---------------------------------------------------------------- required data: verified manifests, never the spec's own declaration
MICROSTRUCTURE = ("liquidity_imbalance", "passive_execution", "spread_microstructure")
ITEM_VERIFIED, ITEM_INCOMPLETE, ITEM_MISSING, ITEM_FAILS, ITEM_DECLARED = "VERIFIED_MANIFEST", "VERIFIED_MANIFEST_INCOMPLETE", "MISSING", "MANIFEST_FAILS_VERIFICATION", "DECLARED_ONLY_NOT_VERIFIED"
ITEM_COMMITTED = "COMMITTED_CORPUS_READ"
_ITEM_ORDER = (ITEM_FAILS, ITEM_MISSING, ITEM_DECLARED, ITEM_INCOMPLETE, ITEM_COMMITTED, ITEM_VERIFIED)           # worst first


def classify_required(name: str) -> Tuple[str, ...]:
    """The data kinds a ``required_data`` item needs. Only universe / funding / mark have a verified capture schema in this repository; every other kind is unverifiable here."""
    n = name.lower()
    kinds = set()
    if "two venues" in n or "per-venue" in n:
        kinds.add("cross_venue")
    if any(k in n for k in ("depth", "l2", "order book", "book ", "spread per", "trade tape", "trade-by-trade", "order-by-order", "latency", "queue position")):
        kinds.add("depth")
    if "spot price" in n or "spot and perp" in n or "hedge-leg" in n or "synchronised" in n or "basis" in n:
        kinds.add("spot_price")
    if any(k in n for k in ("listing", "delisting", "universe")):
        kinds.add("universe")
    if "funding" in n and "cross_venue" not in kinds:
        kinds.add("funding")
    if ("mark" in n or "index price" in n) and "spot_price" not in kinds:
        kinds.add("mark")
    if "fee" in n or "borrow" in n or "financing" in n:
        kinds.add("other")
    return tuple(sorted(kinds)) or ("other",)


def _kind_status(kind: str, inv: Optional[Mapping[str, Any]]) -> Tuple[str, str]:
    if kind in ("depth", "spot_price", "cross_venue"):
        return ITEM_MISSING, f"no verified capture schema for {kind} data exists in this repository"
    if kind == "other":
        return ITEM_DECLARED, "no manifest schema covers this item; the spec's declaration is not evidence"
    if inv is None:
        return ITEM_MISSING, "no data root / universe dir supplied: nothing was looked for"
    if kind == "universe":
        us = inv["universes"]
        if not us:
            return ITEM_MISSING, "no universe_capture/v2 directory supplied or found"
        ok = [u for u in us if u["verified"]]
        if not ok:
            return ITEM_FAILS, "; ".join(p for u in us for p in u["problems"])[:200]
        return (ITEM_VERIFIED if any(u["dataset_status"] == "COMPLETE" for u in ok) else ITEM_INCOMPLETE), f"universe dataset_status {sorted({str(u['dataset_status']) for u in ok})}"
    want = {"funding": ("funding",), "mark": ("mark-kline", "index-kline")}[kind]
    ds = [d for d in inv["datasets"] if d["dataset"] in want]
    if not ds:
        return ITEM_MISSING, f"no {'/'.join(want)} market_history manifest under the data root"
    ok = [d for d in ds if d["verified"]]
    if not ok:
        return ITEM_FAILS, "; ".join(p for d in ds for p in d["problems"])[:200]
    return (ITEM_VERIFIED if all(d["dataset_status"] == "COMPLETE" for d in ok) else ITEM_INCOMPLETE), f"dataset_status {sorted({str(d['dataset_status']) for d in ok})}"


def required_data_items(name: str, spec: Mapping[str, Any], inv: Optional[Mapping[str, Any]], *, committed_funding_ok: bool = False) -> List[Dict[str, Any]]:
    """Every required_data item with the status the ACTUAL manifests give it. ``spec_repo_has`` is the spec's declaration, shown for contrast and never trusted."""
    out = []
    for d in spec.get("required_data", []):
        if not isinstance(d, dict):
            continue
        nm = str(d.get("name", ""))
        kinds = classify_required(nm)
        sts = []
        details = []
        for k in kinds:
            if k == "funding" and name in CORPUS_CARRY and committed_funding_ok:
                st, det = ITEM_COMMITTED, f"{LABEL_CORPUS}: data/real_bybit_btc_4h read by the loader (in-sample, BTCUSDT 8h only)"
            else:
                st, det = _kind_status(k, inv)
            sts.append(st)
            details.append(f"{k}: {det}")
        worst = min(sts, key=_ITEM_ORDER.index)
        exec_only = set(kinds) == {"depth"} and name not in MICROSTRUCTURE
        out.append({"name": nm, "kinds": list(kinds), "spec_repo_has": d.get("repo_has"), "status": worst, "trusted": worst in (ITEM_VERIFIED, ITEM_INCOMPLETE, ITEM_COMMITTED),
                    "complete": worst in (ITEM_VERIFIED, ITEM_COMMITTED), "execution_only": exec_only, "detail": "; ".join(details)[:300]})
    return out


def _data_present(spec: Mapping[str, Any], scan: Mapping[str, Any], inv: Optional[Mapping[str, Any]] = None, *, name: str = "", committed_funding_ok: bool = False) -> Dict[str, Any]:
    items = required_data_items(name, spec, inv, committed_funding_ok=committed_funding_ok)
    rd = [d for d in spec.get("required_data", []) if isinstance(d, dict)]
    critical = [i for i in items if not i["execution_only"]]
    all_ok = bool(items) and all(i["trusted"] and i["complete"] for i in items)
    crit_ok = bool(critical) and all(i["trusted"] and i["complete"] for i in critical)
    return {"spec_declares_missing": [d["name"] for d in rd if d.get("repo_has") is False], "spec_declares_present": [d["name"] for d in rd if d.get("repo_has") is True],
            "manifests_checked": scan["checked"] or bool(scan.get("universe_capture_dirs")), "items": items,
            "value": "VERIFIED_ALL" if all_ok else "MISSING", "evaluation_data_verified": crit_ok,
            "n_items": len(items), "n_trusted": sum(1 for i in items if i["trusted"]),
            "execution_only_unverified": [i["name"] for i in items if i["execution_only"] and not (i["trusted"] and i["complete"])],
            "note": "the spec's repo_has is a declaration and is NOT trusted: an item counts only when a manifest under --data-root / --universe-dir passes its verifier (VERIFIED_MANIFEST); "
                    "depth-only items gate EXECUTION_READY, not DATA_READY"}


# ---------------------------------------------------------------- the readiness vocabulary
READINESS_LEVELS = ("DATA_READY", "RESEARCH_READY", "PROMISING", "PROSPECTIVE", "EXECUTION_READY", "OWNER_PROFIT_EVIDENCED")
MIN_PROMISING_OOS_OBS = SV.MIN_OBSERVATIONS["oos_test"]


def _real(x: Any) -> bool:
    """A finite real number (not a bool): the only kind of value a statistical bar may be passed on. NaN <= 0 and NaN > alpha are both False, so a NaN used to slip through every bar."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _cash_comparison_problems(ev: Mapping[str, Any], alpha: float) -> List[str]:
    """Round 18 (quant specialist): PROMISING used to need only a positive nominal out-of-sample mean (bps of GROSS notional, a zero cost of capital), so an absent or unknown comparison with cash was
    'no objection': a candidate that never beat the owner's alternative on the capital it ties up could be ranked top tier and pass the allocation gate. The decision statistic is the EXCESS over cash on
    the committed capital, so PROMISING now also needs at least one KNOWN cash comparison (``vs_cash``: a walk-forward on that excess) that is not REJECTED and passes the SAME bars as the nominal one:
    a positive out-of-sample mean, a Holm-adjusted p within alpha over the whole family, and enough independent observations. Absent or unknown = CASH_COMPARISON_UNKNOWN: nothing promotes on an
    unknown cost of capital. A basis whose walk-forward did not run (its cash series does not cover enough rows) is unknown and does not decide; EVERY basis that did run must pass (so the lowest bar,
    a lender's-floor series, cannot rescue a candidate the owner's own stated alternative does not), and at least one must have run. This only tightens; a block that is REJECTED already demoted the
    verdict in evaluate_family."""
    blocks = ev.get("vs_cash")
    if not isinstance(blocks, Mapping) or not blocks:
        return ["CASH_COMPARISON_UNKNOWN: no excess-over-cash walk-forward is attached (no cash basis was applied, or none covers the window): the nominal bps of notional charge no cost of capital"]
    ran, failed, unknown = [], [], []
    for label, b in sorted(blocks.items()):
        if not isinstance(b, Mapping) or b.get("status") != "WALK_FORWARD_DONE":
            unknown.append(f"{label}: {b.get('status') if isinstance(b, Mapping) else 'malformed'}")
            continue
        mean, padj, nobs = b.get("oos_mean_excess_bps_of_capital_per_row"), b.get("p_adjusted"), b.get("oos_independent_observations")
        bad = []
        if b.get("verdict") == "REJECTED":
            bad.append("REJECTED")
        if not _real(mean) or mean <= 0:
            bad.append(f"excess mean {mean} is not a positive number")
        if not _real(padj) or padj < 0 or padj > alpha:
            bad.append(f"Holm-adjusted p {padj} is not within [0, {alpha}]")
        if not _real(nobs) or nobs < MIN_PROMISING_OOS_OBS:
            bad.append(f"{nobs} independent observations < {MIN_PROMISING_OOS_OBS}")
        (failed if bad else ran).append(f"{label}: " + "; ".join(bad) if bad else label)
    if failed or not ran:
        return ["CASH_COMPARISON_UNKNOWN_OR_NOT_PASSED: every cash basis that ran must show a positive, significant excess over cash on the committed capital, and at least one must have run ("
                + " | ".join((failed + unknown))[:400] + ")"]
    return []


def readiness_levels(*, data_ready: bool, data_reasons: Sequence[str] = (), pit_ok: bool, survivorship: Optional[str], mechanism_ok: bool,
                     evaluation: Optional[Mapping[str, Any]] = None, prospective: Optional[Mapping[str, Any]] = None, executable: bool = False,
                     owner_evidence: Any = None, alpha: float = 0.05) -> Dict[str, Any]:
    """The six-level readiness vocabulary, CUMULATIVE (a level is reached only if every level below it is). Levels never borrow from one another:

    DATA_READY      the evaluation-critical datasets verify from their manifests and are COMPLETE. A dataset-level fact: it says nothing about any strategy.
    RESEARCH_READY  DATA_READY + point-in-time universe verified + survivorship not SURVIVORS_ONLY + stated mechanism. The evaluation is now *meaningful*; no edge is implied.
    PROMISING       RESEARCH_READY + a walk-forward evaluation with positive out-of-sample mean, Holm-adjusted p <= alpha over the WHOLE family, enough independent observations and
                    no unpriced terminal exits. Still in-sample-to-capture: not a pass.
    PROSPECTIVE     PROMISING + a registered prospective window that has STARTED and observed at least its floor of settlements (this tool never reads a journal: unreachable here).
    EXECUTION_READY PROSPECTIVE + executable on a qualified venue (never a RESEARCH_ONLY candidate).
    OWNER_PROFIT_EVIDENCED  owner-signed evidence verified by the owner's ledger. This tool cannot read or create it: never reached here (a caller-supplied dict is ignored).

    ``survivorship`` None = not applicable (single instrument). ``highest_supportable_verdict`` never exceeds INSUFFICIENT_DATA: REJECTED when an adequately sized evaluation lost, or earlier when the out-of-sample net is significantly negative (``early_rejection``: reject-only)."""
    L: Dict[str, Dict[str, Any]] = {}
    ev = evaluation if isinstance(evaluation, Mapping) else None

    def put(name: str, ok: bool, reasons: Sequence[str]) -> None:
        prev_ok = name == READINESS_LEVELS[0] or L[READINESS_LEVELS[READINESS_LEVELS.index(name) - 1]]["reached"]
        rs = list(reasons)
        if ok and not prev_ok:
            ok = False
            rs.append(f"not reached because {READINESS_LEVELS[READINESS_LEVELS.index(name) - 1]} is not reached")
        L[name] = {"reached": bool(ok), "reasons": rs}

    put("DATA_READY", bool(data_ready), list(data_reasons) or ([] if data_ready else ["evaluation-critical data is not verified and COMPLETE from manifests"]))
    rr = []
    if not pit_ok:
        rr.append("point-in-time universe not verified (or the universe dir was not supplied)")
    if survivorship == SURVIVORS_ONLY:
        rr.append("SURVIVORS_ONLY: delistings were not observed, so no survivorship-free claim can be made; a dataset-level DATA_READY does not become strategy-level READY")
    if not mechanism_ok:
        rr.append("no stated counterparty / economic mechanism")
    put("RESEARCH_READY", pit_ok and survivorship != SURVIVORS_ONLY and mechanism_ok, rr)
    pr: List[str] = []
    promising = False
    if ev is None or ev.get("status") != "WALK_FORWARD_DONE":
        pr.append("no completed walk-forward evaluation" + ("" if ev is None else f" (status {ev.get('status')})"))
    else:
        mean, padj, nobs = ev.get("oos_mean_total_bps_per_row"), ev.get("p_adjusted"), ev.get("oos_independent_observations")
        if ev.get("verdict") == "REJECTED":
            pr.append("the evaluation was REJECTED")
        if not _real(mean) or mean <= 0:
            pr.append(f"out-of-sample mean {mean} is not a positive number")
        if not _real(padj) or padj < 0 or padj > alpha:
            pr.append(f"Holm-adjusted p {padj} is not within [0, {alpha}] over the whole family")
        if not _real(nobs) or nobs < MIN_PROMISING_OOS_OBS:
            pr.append(f"{nobs} independent OOS observations < {MIN_PROMISING_OOS_OBS}")
        if ev.get("terminal_exit_unpriced"):
            pr.append("unpriced terminal exits")
        if "holdout_status" in ev and ev.get("holdout_status") not in ("RESERVED", "SPENT"):
            pr.append(f"the evaluation was exploratory (holdout {ev.get('holdout_status')}): NOT_A_HOLDOUT_STUDY cannot reach PROMISING")
        pr += _cash_comparison_problems(ev, alpha)
        promising = not pr
    put("PROMISING", promising, pr)
    pp = prospective if isinstance(prospective, Mapping) else {}
    obs, floor = pp.get("settlements_observed") or 0, pp.get("min_settlements") or 0
    started = bool(pp.get("started")) and floor > 0 and obs >= floor
    put("PROSPECTIVE", started, [] if started else ["no started prospective window with its floor of settlements observed (this tool never reads a trial journal)"])
    put("EXECUTION_READY", bool(executable), [] if executable else ["no qualified order path for this mechanism (research-only, unqualified venue, or not Bybit)"])
    put("OWNER_PROFIT_EVIDENCED", False, ["owner-signed evidence is verified only by the owner's ledger/attestation; this tool never reads or creates it"])
    reached = [n for n in READINESS_LEVELS if L[n]["reached"]]
    if ev is not None and ev.get("status") == "WALK_FORWARD_DONE" and ev.get("verdict") == "REJECTED":
        hv = "REJECTED"
    elif L["DATA_READY"]["reached"] or ev is not None:
        hv = "INSUFFICIENT_DATA"
    else:
        hv = "UNTESTED"
    return {"levels": L, "highest": reached[-1] if reached else "NONE", "highest_supportable_verdict": hv,
            "survivorship_free_claim": bool(pit_ok and survivorship == DELISTINGS_OBSERVED),
            "notes": ["levels are cumulative; DATA_READY is a property of datasets, the rest are properties of a strategy", "no level here promotes a candidate: the verdict ceiling is INSUFFICIENT_DATA",
                      "PROMISING stands on an ESTIMATED count of independent observations and a Holm-adjusted p computed from it. The estimate assumes AR-type dependence and cannot see a slow hidden "
                      "regime: an independent audit measured false-pass rates 5 to 28 times the stated level on regime-driven nulls at n 100 to 250 (docs/MONEY_MISSION.md, Round 14). PROMISING is a "
                      "reason to look, not evidence: declare the regime dwell of the mechanism before any holdout is read."]}


# ---------------------------------------------------------------- the dataset-state vocabulary and the immutable dataset identity (Round 18)
#: What a captured dataset IS, before any strategy reads it. One word per state, precedence UNAVAILABLE > INVALID > INCOMPLETE > INSUFFICIENT: a download that exited 0 is none of these.
DATA_UNAVAILABLE = "DATA_UNAVAILABLE"                                    # nothing to judge: no history root, no universe capture, no dataset
DATA_INCOMPLETE = "DATA_INCOMPLETE"                                      # verified bytes, but not every lifecycle-eligible symbol is COMPLETE over the whole required window
DATA_INVALID = "DATA_INVALID"                                            # something present does not re-verify, is CONFLICTED / UNVERIFIED, or the loader refused it
DATA_VALID_BUT_INSUFFICIENT = "DATA_VALID_BUT_INSUFFICIENT"              # complete and valid, but too short / survivors-only / the evaluator's own verdict is "not enough"
DATA_SUFFICIENT_NOT_EVALUATED = "DATA_SUFFICIENT_NOT_EVALUATED"          # sufficient for the registered evaluation; no candidate has been run on it (never an edge)
DATA_SUFFICIENT_CANDIDATE_REJECTED = "DATA_SUFFICIENT_CANDIDATE_REJECTED"
DATA_SUFFICIENT_ELIGIBLE_FOR_FURTHER_REVIEW = "DATA_SUFFICIENT_ELIGIBLE_FOR_FURTHER_REVIEW"
DATASET_STATES = (DATA_UNAVAILABLE, DATA_INCOMPLETE, DATA_INVALID, DATA_VALID_BUT_INSUFFICIENT, DATA_SUFFICIENT_NOT_EVALUATED, DATA_SUFFICIENT_CANDIDATE_REJECTED,
                  DATA_SUFFICIENT_ELIGIBLE_FOR_FURTHER_REVIEW)
#: the states in which every lifecycle-eligible symbol is verified and COMPLETE over the whole REQUESTED window (the window the funding manifest asked for, or the explicit one; not the 457 days: a
#: complete but short history is DATA_VALID_BUT_INSUFFICIENT): the only states a dataset-level DATA_READY may stand on
COMPLETE_DATA_STATES = DATASET_STATES[3:]
SUFFICIENT_DATA_STATES = DATASET_STATES[4:]
#: the evaluation geometry the sufficiency window is measured against: pinned by a test to ``evaluate_ranked_funding``'s own defaults (the 457 days of backfill_plan.required_window)
STATE_FORMATION_DAYS, STATE_HOLD_DAYS, STATE_MIN_ROWS = 30, 7, 60
_FINDING_CLASSES = ("unavailable", "invalid", "incomplete", "insufficient")
_VERDICT_TO_STATE = {"REJECTED": DATA_SUFFICIENT_CANDIDATE_REJECTED, "PROMISING": DATA_SUFFICIENT_ELIGIBLE_FOR_FURTHER_REVIEW, "QUALIFIED": DATA_SUFFICIENT_ELIGIBLE_FOR_FURTHER_REVIEW}
_EVALUATOR_NOT_ENOUGH = ("INSUFFICIENT_DATA", "UNTESTED")


#: A mark price that does not move for this long is not a market: a stalled index, a halted or delivering contract, a feed that repeats its last value. The price leg of a funding position on such a
#: series is exactly zero for every hold, so the series reads as a PERFECT hedge and the squeeze or crash a short suffers is erased (a planted +0.03%/print rally in two top-funding names inflated the
#: out-of-sample mean 12x, 4.3 -> 51.6 bps/row, with nothing flagged). Frozen BEFORE the owner's data is read; a funding rate is NOT held to the same rule (a run of the 1 bp baseline is real).
FROZEN_MARK_MIN_HOURS = 48
#: ... and never fewer than this many candles whatever the interval: two equal DAILY closes are a low-priced coin on a coarse tick, not a stalled feed (the registered evaluators read 240-minute marks: 12 candles).
FROZEN_MARK_MIN_CANDLES = 12


def frozen_mark_threshold(interval_ms: Optional[int]) -> int:
    """How many identical consecutive closes make a mark series frozen: 48 hours of candles, never fewer than ``FROZEN_MARK_MIN_CANDLES`` (and 12 when the interval is not stated)."""
    if not isinstance(interval_ms, int) or isinstance(interval_ms, bool) or interval_ms <= 0:
        return FROZEN_MARK_MIN_CANDLES
    return max(FROZEN_MARK_MIN_CANDLES, int(math.ceil(FROZEN_MARK_MIN_HOURS * 3_600_000 / interval_ms)))


def has_stuck_window(values: Sequence[Any], window: int, max_distinct: int = 2) -> bool:
    """True when ANY run of ``window`` consecutive values holds at most ``max_distinct`` distinct values (a feed that dithers by a tick is as stuck as one that repeats). Shorter input: False."""
    n = len(values)
    if window <= 0 or n < window:
        return False
    counts: Dict[Any, int] = {}
    for i, v in enumerate(values):
        counts[v] = counts.get(v, 0) + 1
        if i >= window:
            out = values[i - window]
            counts[out] -= 1
            if not counts[out]:
                del counts[out]
        if i >= window - 1 and len(counts) <= max_distinct:
            return True
    return False


def longest_identical_run(values: Sequence[Any]) -> int:
    """The longest run of consecutive EQUAL values (0 for an empty sequence)."""
    best = run = 0
    prev: Any = object()
    for v in values:
        run = run + 1 if v == prev else 1
        prev = v
        if run > best:
            best = run
    return best


def dataset_state(findings: Mapping[str, Sequence[str]], *, candidate_verdict: Optional[str] = None) -> Dict[str, Any]:
    """The state of a dataset from its findings (a dict of reason lists keyed ``unavailable`` / ``invalid`` / ``incomplete`` / ``insufficient``; absent = none). The worst non-empty
    class names the state and its reasons; ``candidate_verdict`` (the evaluator's own word: REJECTED / PROMISING / QUALIFIED / INSUFFICIENT_DATA / UNTESTED, or None = none was run)
    only matters when the data themselves are sufficient: REJECTED -> DATA_SUFFICIENT_CANDIDATE_REJECTED, PROMISING or better -> DATA_SUFFICIENT_ELIGIBLE_FOR_FURTHER_REVIEW, and an
    evaluator that itself says the sample is not enough keeps the dataset DATA_VALID_BUT_INSUFFICIENT. A dataset-state is a statement about bytes and coverage: it is never an edge,
    an authority or a profit. An unknown finding class or verdict raises: a typo must not read as 'sufficient'."""
    if not isinstance(findings, Mapping) or set(findings) - set(_FINDING_CLASSES):
        raise ValueError(f"findings must be a mapping with keys from {_FINDING_CLASSES}")
    if candidate_verdict is not None and candidate_verdict not in (*_VERDICT_TO_STATE, *_EVALUATOR_NOT_ENOUGH):
        raise ValueError(f"unknown candidate verdict {candidate_verdict!r}")
    f = {k: [str(x) for x in (findings.get(k) or ())] for k in _FINDING_CLASSES}
    if f["unavailable"]:
        state, why = DATA_UNAVAILABLE, f["unavailable"]
    elif f["invalid"]:
        state, why = DATA_INVALID, f["invalid"]
    elif f["incomplete"]:
        state, why = DATA_INCOMPLETE, f["incomplete"]
    elif f["insufficient"]:
        state, why = DATA_VALID_BUT_INSUFFICIENT, f["insufficient"]
    elif candidate_verdict is None:
        state, why = DATA_SUFFICIENT_NOT_EVALUATED, []
    elif candidate_verdict in _VERDICT_TO_STATE:
        state, why = _VERDICT_TO_STATE[candidate_verdict], []
    else:
        state, why = DATA_VALID_BUT_INSUFFICIENT, [f"the evaluator's own verdict is {candidate_verdict}: the sample cannot carry a claim"]
    return {"schema": SCHEMA + "/dataset_state", "state": state, "reasons": why, "findings": f, "candidate_verdict": candidate_verdict,
            "complete_over_requested_window": state in COMPLETE_DATA_STATES, "sufficient": state in SUFFICIENT_DATA_STATES,
            "note": "a statement about verified bytes and coverage only: no DATA_SUFFICIENT_* state is an edge, an authority to trade or a profit; DATA_READY stands on it"}


def dataset_identity(components: Sequence[Mapping[str, Any]], *, extra: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """A reproducible identity for a dataset: the sha256 of the canonical JSON of its components (each a ``kind`` / ``name`` / ``digest`` from a manifest the verifiers RE-HASHED) and of
    the facts the evaluation depends on (window, mark interval, funnel digest). No path, clock or machine enters it, so a byte-identical copy has the same identity and one changed byte in
    any verified page (which changes its manifest digest) has a different one; a result stamped with it names the exact data it read. A component that is not verified, or has no
    digest, cannot be identified: ``identifies_every_component`` is then False and ``digest`` is None (a dataset that does not re-hash has no identity, so the identity of the pristine
    copy is never printed for a tampered one)."""
    comps = sorted(({"kind": str(c.get("kind")), "name": str(c.get("name")), "digest": c["digest"] if isinstance(c.get("digest"), str) and c["digest"] else None,
                     "verified": c.get("verified") is True} for c in components), key=lambda c: (c["kind"], c["name"], c["digest"] or ""))
    body = {"schema": SCHEMA + "/dataset_identity", "components": comps, "extra": {str(k): (extra or {})[k] for k in sorted(extra or {}, key=str)}}
    identified = bool(comps) and all(c["digest"] and c["verified"] for c in comps)
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest() if identified else None
    return {**body, "digest": digest, "identifies_every_component": identified}


def _coverage_findings(*, eligible: Sequence[str], covered: Sequence[str], not_complete: Sequence[str], ended_unaccounted: int, window_days: Optional[float],
                       survivorship: Optional[str]) -> Dict[str, List[str]]:
    """The per-symbol and window findings every dataset-state shares (the loader and the report call this, so the two can never disagree). ``eligible`` = the funnel's lifecycle-eligible
    symbols, ``covered`` = those with a loaded series, ``not_complete`` = loaded symbols not COMPLETE over the whole required window in every required dataset. A cross-section of the
    symbols somebody happened to collect is not the universe: every eligible symbol must be loaded AND complete before the dataset is complete."""
    out: Dict[str, List[str]] = {"incomplete": [], "insufficient": []}
    if not eligible:                 # "every eligible symbol is complete" is vacuously true of an empty set: Round 18 review, a verified capture with NO eligible symbol printed the go signal
        out["incomplete"].append("NO_ELIGIBLE_SYMBOLS: the universe has no lifecycle-eligible perpetual in the window (or none has a series before the holdout cut): there is nothing for a cross-section to be complete over")
    missing = sorted(set(eligible) - set(covered))
    if missing:
        out["incomplete"].append(f"{len(missing)} of {len(eligible)} lifecycle-eligible symbols have no usable history in the datasets (first: {missing[:5]})")
    short = sorted(set(not_complete))
    if short:
        out["incomplete"].append(f"{len(short)} loaded symbols are not COMPLETE over the whole required window (first: {short[:5]})")
    if ended_unaccounted:
        out["incomplete"].append(f"{ended_unaccounted} Closed/delivered perpetuals overlapping the window are neither in the series nor excluded for a reason that proves them out of scope")
    need = grid_requirements(formation_days=STATE_FORMATION_DAYS, hold_days=STATE_HOLD_DAYS, min_rows=STATE_MIN_ROWS)["window_days_required"]
    if window_days is None or window_days < need:
        out["insufficient"].append(f"history window {None if window_days is None else round(window_days, 1)} days < {need} days: {STATE_MIN_ROWS} independent {STATE_HOLD_DAYS}-day holding rows need "
                                   f"warmup + {STATE_FORMATION_DAYS}-day formation + {STATE_MIN_ROWS} x {STATE_HOLD_DAYS} (backfill_plan.required_window)")
    if survivorship != DELISTINGS_OBSERVED:
        out["insufficient"].append(f"survivorship is {survivorship}: delistings were not observed, so no survivorship-free claim can be made")
    return out


# ---------------------------------------------------------------- the BTC funding carry on the committed corpus
def btc_carry_economics(repo: str = os.path.dirname(BOT)) -> Dict[str, Any]:
    """Net of cost on data/real_bybit_btc_4h (8h Bybit BTCUSDT settlements + 4h spot/perp bars). IN-SAMPLE, 8h only. Unconditional forced holds entered after EVERY
    settlement (no foresight, no gate), per hold length and cost scenario, plus the production-gated sequence at taker cost. Nothing here is out-of-sample."""
    try:
        rows = OM.load_bybit(BOT)
    except (OSError, ValueError, KeyError) as exc:
        return {"status": UNKNOWN, "reason": f"committed corpus not readable: {exc}"}
    c = cost_constants()
    sc = scenarios(c)
    mat = OM.hold_cost_matrix(rows, HOLDS_PRINTS, sc)
    gate_rt = 2.0 * CC.TAKER_BPS_PERP
    tk = sc["TAKER_FEES_PLUS_SPREAD"]
    seq = OM.gated_sequence(rows, OM.HOLD_PRINTS, rt_gate_bps=gate_rt, cost_bps=tk)
    nets = [t["net_bps"] for t in seq["trades"]]
    year_ms = 365 * 86_400_000
    last = rows[-1].ms
    trail = [t["net_bps"] for t in seq["trades"] if t["ms"] > last - year_ms]
    first_y = {}
    for t in seq["trades"]:
        first_y.setdefault(OM._year(t["ms"]), []).append(t["net_bps"])
    long_by_year: Dict[str, Any] = {}
    for h in (17, 51):
        by: Dict[int, List[float]] = {}
        for e in OM.all_forced(rows, h, cost_bps=tk):
            by.setdefault(OM._year(e["ms"]), []).append(e["net_bps"])
        long_by_year[f"{h}_prints"] = {str(y): statistics.fmean(v) for y, v in sorted(by.items())}
    cap = _on_capital_after_cash(rows, c, tk, seq)
    return {"on_capital_after_cash": cap, "long_hold_by_year_mean_net_bps": long_by_year, "long_hold_cost_bps": tk,
            "basis": f"{LABEL_CORPUS}: {OM.BYBIT_DIR}, {len(rows)} Bybit BTCUSDT 8h settlements {OM._iso(rows[0].ms)}..{OM._iso(last)}; funding interval 8h ONLY",
            "cost_constants": c, "scenarios_round_trip_bps": sc,
            "unconditional_forced_hold": mat,
            "gated_sequential_taker": {"cost_bps": tk, "hold_prints": OM.HOLD_PRINTS, "n": len(nets), "mean_net_bps": statistics.fmean(nets) if nets else None,
                                       "trailing_365d_n": len(trail), "trailing_365d_mean_net_bps": statistics.fmean(trail) if trail else None,
                                       "by_year_mean_net_bps": {str(y): statistics.fmean(v) for y, v in sorted(first_y.items())},
                                       "note": "the gate's constants were set with this whole corpus visible: in-sample, not a test"},
            "non_8h_intervals": {"btc_bybit": "NO_DATA in the committed corpus (Bybit BTCUSDT funding is 480m only)",
                                 "other": "non-8h prints exist only for Binance SOL (98 x 2h, 3 x 4h): not Bybit, not executable, too short to test",
                                 "arithmetic_only": "interval does not change the carry per day: required funding bps/day = round_trip_bps / hold_days; per print = bps/day x interval_min / 1440"},
            "breakeven_bps_per_day": {name: {f"{h}_prints": cost / (h / CC.FUNDING_PERIODS_PER_DAY) for h in HOLDS_PRINTS} for name, cost in sc.items()},
            "label": LABEL_CORPUS}


CAP_OVERLAY = "overlay_owned_spot"
CAP_ACQUIRE = "acquire_spot_perp"


def _on_capital_after_cash(rows: Sequence[Any], c: Mapping[str, Any], overlay_cost_bps: float, gated: Mapping[str, Any]) -> Dict[str, Any]:
    """The BTC carry re-expressed as a return on the CAPITAL TIED UP, net of the cash that capital would earn. Every other number in this report is bps of NOTIONAL with zero capital cost, which
    is the most favourable denominator there is: a hedged position that does not beat USD cash is not a money-maker however positive its notional net. The cash series is the repo's own OKX USDT
    lending history (a lender's floor, not the owner's opportunity cost) and a hold the series cannot cover is UNKNOWN and dropped, never free."""
    curve = OM.load_cash_curve(BOT)
    # Round 19: ONE definition per structure. The overlay keeps the research assumption (the FAVOURABLE basis: used to REJECT, since a carry that fails on it fails on every dearer one), and the
    # ACQUIRE class is priced at what the executor commits (the registry's 2.04x at leverage 1), no longer at the favourable overlay factor.
    factors = {CAP_OVERLAY: float(OM.CAPITAL_PER_NOTIONAL), CAP_ACQUIRE: float(EM.structure_capital_usd(EM.ACQUIRED_SPOT_PERP, 1, 1).value)}
    base = {"capital_per_notional": OM.CAPITAL_PER_NOTIONAL, "capital_per_notional_by_class": factors,
            "capital_basis": "spot leg (paid, or owned and so forgone) + an ASSUMED ~15% perp initial margin + 1% buffer per USD of notional (the research overlay basis; the live Testnet plan at leverage 1 "
                             "commits 2.04x, so this is the FAVOURABLE basis for carry)",
            "cash_series": {"source": OM.OKX_LENDING, "label": OM.CASH_BENCHMARK_LABEL, "first_utc": OM._iso(curve.first_ms()), "last_utc": OM._iso(curve.last_ms())}, "label": LABEL_CORPUS}
    if not curve:
        return {**base, "status": UNKNOWN, "reason": "CASH_SERIES_UNAVAILABLE: no cost of capital can be applied; the notional figures elsewhere in this report carry NO capital cost and are not a return on capital"}
    classes = {CAP_OVERLAY: overlay_cost_bps, CAP_ACQUIRE: float(c["spot_perp_round_trip_total_bps"])}
    out: Dict[str, Any] = {}
    base_entries = {h: OM.all_forced(rows, h, cost_bps=0.0) for h in HOLDS_PRINTS}          # funding + basis shed per entry; each class subtracts its own round trip (net = gross - cost)
    for cls, cost in classes.items():
        holds: Dict[str, Any] = {}
        for h in HOLDS_PRINTS:
            ent, unk = OM.on_capital([{**e, "net_bps": e["net_bps"] - cost, "cost_bps": cost} for e in base_entries[h]], curve, capital_factor=factors[cls])
            holds[str(h)] = OM.excess_summary(ent, unk, hold=h)
            if cls == CAP_OVERLAY and ent and holds[str(h)].get("cash_coverage_ok"):          # how strong is the loss, by the repo's own rejection standard (the policies the competition ranks)
                holds[str(h)]["rejection_bars"] = {"nominal": rejection_bar([e["net_bps"] for e in ent], basis="mean_net_bps_of_notional_zero_capital_cost"),
                                                   "excess": rejection_bar([e["excess_over_cash_bps_of_capital"] for e in ent], basis="mean_excess_over_cash_bps_of_capital")}
        out[cls] = {"round_trip_bps": cost, "holds": holds}
    # the same overlay hold priced at each stated capital multiple, so a favourable-basis figure is never read as evidence about the executed structure (the sign flips with the multiple: committed
    # data, +1.4%/yr at 1.0x, +0.2% at 1.16x, -3.1% at 2.04x on the whole replay)
    grid_hold = OM.HOLD_LONG_PRINTS if OM.HOLD_LONG_PRINTS in HOLDS_PRINTS else max(HOLDS_PRINTS)
    upper = float(EM.structure_capital_usd(EM.OWNED_INVENTORY_OVERLAY, 1, 1).value)
    grid: Dict[str, Any] = {}
    for label, f, what in (("inventory_only", 1.0, "the held coin only: its margin is covered by it as collateral at ratio 1 (a best case the venue must state)"),
                           ("research_assumption", float(OM.CAPITAL_PER_NOTIONAL), "the research overlay: held coin + an ASSUMED ~15% margin + 1% buffer (the favourable basis; used to reject)"),
                           ("no_collateral_credit", upper, "held coin + the perp margin paid in separate cash + the plan buffer: the upper bound until the owner states the venue's collateral ratio")):
        ent, unk = OM.on_capital([{**e, "net_bps": e["net_bps"] - overlay_cost_bps, "cost_bps": overlay_cost_bps} for e in base_entries[grid_hold]], curve, capital_factor=f)
        sm = OM.excess_summary(ent, unk, hold=grid_hold)
        grid[label] = {"capital_per_notional": f, "hold_prints": grid_hold, "n": sm.get("n"), "mean_excess_over_cash_bps_of_capital": sm.get("mean_excess_over_cash_bps_of_capital"), "meaning": what}
    gt, gunk = OM.on_capital(list(gated["trades"]), curve)
    gs = OM.excess_summary(gt, gunk, hold=OM.HOLD_PRINTS)
    if gt and gs.get("cash_coverage_ok"):
        gs["rejection_bars"] = {"nominal": rejection_bar([e["net_bps"] for e in gt], basis="mean_net_bps_of_notional_zero_capital_cost"),
                                "excess": rejection_bar([e["excess_over_cash_bps_of_capital"] for e in gt], basis="mean_excess_over_cash_bps_of_capital")}
    return {**base, "status": "COMPUTED", "classes": out, "gated_sequential_overlay": gs, "overlay_capital_grid": grid,
            "reading": "mean_excess_over_cash_bps_of_capital below zero = the position earns LESS than the cash alternative on the capital it ties up, after every modelled cost; "
                       "the nominal bps-of-notional figures elsewhere in this report do not charge that capital"}


def _carry_verdict(econ: Mapping[str, Any]) -> Tuple[str, List[str], Dict[str, Any]]:
    """Readiness verdict for the BTC carry candidates from the committed corpus. REJECTED only for the unconditional single-print harvest (CI wholly below zero at the
    cost the engine pays); the gated policy is marginal in-sample and negative in the trailing year, which is INSUFFICIENT_DATA, never a pass."""
    reasons: List[str] = []
    mat = econ["unconditional_forced_hold"]
    taker = "TAKER_FEES_PLUS_SPREAD"
    one = next(m for m in mat if m["hold_prints"] == 1 and m["scenario"] == taker)
    s1 = one["summary"]
    ci = one["mean_block_ci90"]
    neg_single = s1.get("n", 0) > 0 and s1["mean"] < 0 and ci is not None and ci[1] < 0
    reasons.append(f"single-print harvest at {taker} ({econ['scenarios_round_trip_bps'][taker]:.1f} bps round trip): mean net {s1['mean']:.2f} bps/entry, n={s1['n']} overlapping, "
                   f"block-CI90 {None if ci is None else [round(x, 2) for x in ci]} [{LABEL_CORPUS}]")
    best = max((m for m in mat if m["scenario"] == taker), key=lambda m: m["summary"].get("mean", -1e9))
    reasons.append(f"best unconditional hold length at {taker}: {best['hold_prints']} prints, mean net {best['summary']['mean']:.2f} bps [{LABEL_CORPUS}]")
    g = econ["gated_sequential_taker"]
    if g["n"]:
        reasons.append(f"production-gated sequence at taker: n={g['n']}, mean net {g['mean_net_bps']:.2f} bps; trailing 365d n={g['trailing_365d_n']} mean "
                       f"{g['trailing_365d_mean_net_bps'] if g['trailing_365d_mean_net_bps'] is None else round(g['trailing_365d_mean_net_bps'], 2)} bps [{LABEL_CORPUS}; gate tuned on this corpus]")
    reasons.append("unconditional 51-print (17d) hold by entry year (net bps at the same cost, the edge is a regime, not a constant): "
                   + ", ".join(f"{y}: {v:.1f}" for y, v in econ["long_hold_by_year_mean_net_bps"]["51_prints"].items()) + f" [{LABEL_CORPUS}]")
    cap = econ.get("on_capital_after_cash") or {}
    ov = ((cap.get("classes") or {}).get(CAP_OVERLAY) or {}).get("holds", {}) if cap.get("status") == "COMPUTED" else {}
    o, a = ov.get(str(OM.HOLD_LONG_PRINTS)) or {}, (((cap.get("classes") or {}).get(CAP_ACQUIRE) or {}).get("holds", {}) if ov else {}).get(str(OM.HOLD_LONG_PRINTS)) or {}
    if o.get("mean_excess_over_cash_bps_of_capital") is not None:
        ci = o["excess_block_ci90"]
        reasons.append(f"ON CAPITAL, AFTER CASH (capital {cap['capital_per_notional']}x notional; cash = {cap['cash_series']['label']}): the {OM.HOLD_LONG_PRINTS}-print hold's nominal net "
                       f"{o['mean_nominal_net_bps_of_notional']:.1f} bps of notional is {o['mean_net_bps_on_capital']:.1f} bps on capital and {o['mean_excess_over_cash_bps_of_capital']:.1f} bps EXCESS over cash "
                       f"(overlay; block-CI90 {None if ci is None else [round(x, 1) for x in ci]}, {o['annualised_excess_pct_of_capital']:.2f}%/yr extrapolated"
                       + (f"; acquire at {cap['capital_per_notional_by_class'][CAP_ACQUIRE]:.2f}x {a['mean_excess_over_cash_bps_of_capital']:.1f} bps" if a.get("mean_excess_over_cash_bps_of_capital") is not None else "") + f")"
                       + (f"; overlay at the executed no-credit basis {cap['overlay_capital_grid']['no_collateral_credit']['capital_per_notional']:.2f}x "
                          f"{cap['overlay_capital_grid']['no_collateral_credit']['mean_excess_over_cash_bps_of_capital']:.1f} bps"
                          if ((cap.get('overlay_capital_grid') or {}).get('no_collateral_credit') or {}).get('mean_excess_over_cash_bps_of_capital') is not None else "") + f" [{LABEL_CORPUS}]")
    else:
        reasons.append("CASH_UNKNOWN: the cost of capital could not be applied (no cash series, or it covers too little of the history); every net figure above is bps of NOTIONAL with ZERO capital cost, "
                       "not a return on capital")
    # the same rule the competition applies: a policy that loses after costs, or that is positive on notional but loses to cash on the capital it ties up, is rejected; the candidate is REJECTED
    # only when EVERY policy is (so the two verdict tools can never disagree on the same evidence)
    gated_cap = (cap.get("gated_sequential_overlay") or {}) if cap.get("status") == "COMPUTED" else {}
    lost: List[bool] = []
    for m in mat:
        if m["scenario"] != taker:
            continue
        nominal = m["summary"].get("mean")
        ex = (ov.get(str(m["hold_prints"])) or {}).get("mean_excess_over_cash_bps_of_capital")
        lost.append(bool((nominal is not None and nominal < 0) or (ex is not None and ex < 0)))
    gex = gated_cap.get("mean_excess_over_cash_bps_of_capital")
    gated_lost = bool((g["n"] and g["mean_net_bps"] is not None and g["mean_net_bps"] < 0) or (gex is not None and gex < 0))
    lost.append(gated_lost)
    all_rejected = bool(lost) and all(lost)
    policies = {"single_print_harvest": "REJECTED" if neg_single else UNKNOWN, "gated_policy": "REJECTED" if gated_lost else "INSUFFICIENT_DATA",
                "long_hold_vs_cash": ("REJECTED" if (o.get("mean_excess_over_cash_bps_of_capital") is not None and o["mean_excess_over_cash_bps_of_capital"] < 0) else
                                      (UNKNOWN if o.get("mean_excess_over_cash_bps_of_capital") is None else "INSUFFICIENT_DATA"))}
    meeting, not_meeting = [], []
    for m in mat:
        if m["scenario"] != taker:
            continue
        h = m["hold_prints"]
        hold = ov.get(str(h)) or {}
        bars = hold.get("rejection_bars") or {}
        bar = bars.get("excess") or bars.get("nominal") or {}                                  # the like-for-like test is against CASH; the nominal test only when no cash series covers it
        (meeting if bar.get("meets_early_rejection_standard") else not_meeting).append(f"{h}_prints")
    reasons.append("STATISTICAL STRENGTH of the loss, by the repo's own early-rejection standard (>= 12 independent observations, lower-tail p <= 0.01, widest 90% interval wholly below zero; applied IN-SAMPLE to "
                   f"overlapping entries): meeting it: {meeting or 'none'}; not meeting it: {not_meeting or 'none'}. A point-estimate loss that does not meet it is a loss BY POINT ESTIMATE only; the "
                   "verdict below is the competition's documented in-sample one, not a statistical or out-of-sample rejection. Registered holds are at most 17 days; longer holds were never registered")
    reasons.append("no out-of-sample settlement exists in the repo; no price-leg/basis residual beyond the 4h-bar basis shed; no L2 depth: INSUFFICIENT_DATA is the ceiling"
                   + (" (every policy loses after costs or to cash: REJECTED)" if all_rejected else ""))
    return ("REJECTED" if all_rejected else "INSUFFICIENT_DATA"), reasons, policies



# ---------------------------------------------------------------- one row
def _executable(name: str, spec: Mapping[str, Any]) -> Dict[str, Any]:
    q = list(venues.qualified_names())
    if name.startswith("cross_venue") or "second venue" in str(spec.get("execution_model", "")).lower():
        return {"value": RESEARCH_ONLY, "executable": False, "reason": f"needs a second venue; the only qualified venues are {q} and the second venue is unqualified (venues.py)"}
    if name in CORPUS_CARRY:
        return {"value": "YES_PAPER_TESTNET_BTCUSDT_ONLY", "executable": True,
                "reason": f"the carry engine is proven for BTCUSDT 8h funding only (CarryEngine.FUNDING_8H_PROVEN) on qualified venues {q}; paper/testnet only, live is refused"}
    return {"value": "NO", "executable": False,
            "reason": f"no qualified order path for this mechanism: the only order-capable strategy is the BTCUSDT carry engine (qualified venues {q}); other symbols/mechanisms are research only"}


def _mechanism(spec: Mapping[str, Any]) -> Dict[str, Any]:
    cp, em = str(spec.get("counterparty", "")).strip(), str(spec.get("economic_mechanism", "")).strip()
    ok = len(cp) > 15 and len(em) > 40
    return {"present": ok, "counterparty": cp[:200] if cp else "ABSENT", "mechanism_excerpt": em[:240] if em else "ABSENT",
            "flag": None if ok else "FLAG_NO_STATED_COUNTERPARTY_OR_MECHANISM"}


def _family(name: str, spec: Mapping[str, Any]) -> Dict[str, Any]:
    fam = RH.normalise_family(spec.get("multiple_testing_family") or spec.get("candidate_id"))
    n, note = 0, "no variant has been recorded; the family size is UNKNOWN until a VariantLedger exists, and every future variant counts"
    if name == "cross_sectional_funding":
        n, note = len(RH.LOOKBACKS) * len(RH.HOLDS), "the committed demo grid enumerates lookback x hold variants (survivors only, so not a valid test)"
    elif name == "btcusdt_funding_carry_overlay":
        import carry_prospective as CP
        n, note = len(CP.HURDLES_PCT), "registered hurdle scenarios (PROVISIONAL)"
    return {"family": fam, "variants_enumerated": n, "note": note, "correction": "holm (default) / bh via VariantLedger.selection; deflated Sharpe via tools/deflated_sharpe on the best variant"}


def candidate_row(name: str, spec: Mapping[str, Any], scan: Mapping[str, Any], econ: Optional[Mapping[str, Any]], inv: Optional[Mapping[str, Any]] = None,
                  evaluation: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    cid = str(spec.get("candidate_id"))
    ladder = SV.evaluate(spec, [])
    bias = spec.get("bias_declarations", {}) if isinstance(spec.get("bias_declarations"), dict) else {}
    inv = inv if inv is not None else (verified_inventory(scan) if (scan["checked"] or scan.get("universe_capture_dirs")) else None)
    uni = universe_assessment(spec, scan, inv)
    needs_u = _needs_universe(spec)
    u_dir = {"required": needs_u, "supplied": bool(scan.get("universe_dir_supplied")),
             "status": "NOT_REQUIRED" if not needs_u else (("SUPPLIED_VERIFIED" if inv and any(u["verified"] and u["explicit"] for u in inv["universes"]) else "SUPPLIED_UNVERIFIED") if scan.get("universe_dir_supplied") else "UNIVERSE_DIR_REQUIRED"),
             "discovered_under_data_root_not_supplied": [] if scan.get("universe_dir_supplied") else list(scan.get("universe_capture_dirs", []))[:4],
             "rejected": scan.get("universe_dirs_rejected", [])}
    if needs_u and not u_dir["supplied"]:
        uni = {**uni, "reason": "UNIVERSE_DIR_REQUIRED: the cross-sectional path takes --universe-dir as an explicit required input; none was passed (" + str(uni.get("reason", uni["status"]))[:160] + ")"}
        if uni["status"] != MISSING:
            uni = {**uni, "status": uni["status"], "assessment_is_informational_only": True}
    ex = _executable(name, spec)
    costs = cost_constants()
    carry = name in CORPUS_CARRY and econ is not None and "unconditional_forced_hold" in econ
    row: Dict[str, Any] = {
        "candidate": cid, "name": name,
        "required_data": _data_present(spec, scan, inv, name=name, committed_funding_ok=carry),
        "universe_dir": u_dir,
        "point_in_time": {"value": uni["status"], "detail": uni, "declared_by_spec": bias.get("point_in_time_universe")},
        "universe_complete_for_claim": uni["status"] in ("VERIFIED_RAW_RESPONSES", "NOT_APPLICABLE_SINGLE_INSTRUMENT") and (not needs_u or u_dir["supplied"]),
        "economic_mechanism": _mechanism(spec),
        "executable_on_qualified_venues": ex,
        "price_leg_hedge_risk_measured": {"value": bool(bias.get("hedge_completeness_modelled")),
                                          "reason": "declared by the spec; its own required_data says " + ("the data is in the repo" if bias.get("hedge_completeness_modelled") else "hedge-leg data is missing or partial")},
        "capacity_supported_by_depth": {"value": bool(bias.get("fills_modelled_against_real_depth")),
                                        "reason": "declared by the spec (L2 depth for the traded instrument); capacity_model: " + json.dumps(spec.get("capacity_model", {}))[:160]},
        "multiple_testing": _family(name, spec),
        "oos_or_prospective_possible": _oos(spec),
        "ladder_verdict": ladder.verdict, "ladder_reasons": ladder.reasons[:5], "spec_problems": ladder.spec_problems[:8],
    }
    if carry:
        verdict, reasons, policies = _carry_verdict(econ)
        row["edge_vs_costs"] = {"value": "NEGATIVE_AT_HOLDS_UNDER_17_PRINTS_POSITIVE_ONLY_AT_LONG_HOLDS_IN_SAMPLE_REGIME_DEPENDENT" if policies["single_print_harvest"] == "REJECTED" else UNKNOWN,
                                "policies": policies, "label": LABEL_CORPUS, "see": "economics.btc_carry_corpus"}
        row["readiness_verdict"], row["readiness_reasons"] = verdict, reasons
    else:
        be = costs["perp_round_trip_total_bps"] / 7.0
        assumed = [f"{m}.{k}" for m in ("fee_model", "slippage_model", "funding_model") for k, e in (spec.get(m) or {}).items()
                   if isinstance(e, dict) and e.get("provenance") in ("assumed", "unknown")]
        row["edge_vs_costs"] = {"value": UNKNOWN, "label": LABEL_ARITH,
                                "reason": f"no committed data measures this edge; inputs still 'assumed': {assumed[:6]}",
                                "breakeven_requirement": f"{be:.2f} bps/day of funding+price edge over a 7-day hold just repays a perp round trip ("
                                                         f"fees + ASSUMED spread); per 8h print {be * 480 / 1440:.2f}, per 4h print {be * 240 / 1440:.2f}, per 1h print {be * 60 / 1440:.2f}"}
        if not row["required_data"]["spec_declares_missing"] and uni["status"] in ("NOT_APPLICABLE_SINGLE_INSTRUMENT",):
            row["readiness_verdict"] = "INSUFFICIENT_DATA"
        else:
            row["readiness_verdict"] = "UNTESTED"
        row["readiness_reasons"] = ["no test has been run on any data for this mechanism",
                                    "required data missing per spec: " + "; ".join(row["required_data"]["spec_declares_missing"][:4])]
    if ex["value"] == RESEARCH_ONLY:
        row["readiness_reasons"].append("RESEARCH_ONLY: a second venue is unqualified; this candidate can never be marked executable")
    if name == "cross_sectional_funding":
        row["readiness_reasons"].append("PERP-ONLY symbols (no Bybit spot pair) are directional price+funding, never carry; formation and holding windows must be separate (evaluate_ranked_funding)")
        row["interval_note"] = OWNER_INTERVAL_NOTE
    if needs_u and not u_dir["supplied"]:
        row["readiness_reasons"].append("UNIVERSE_DIR_REQUIRED: the cross-sectional path refuses to evaluate without an explicit --universe-dir (a verified universe_capture); nothing was evaluated")
    row["verdict_rank_ceiling"] = "INSUFFICIENT_DATA"
    pit_ok = row["universe_complete_for_claim"]
    surv = None if not needs_u else uni.get("survivorship", SURVIVORS_ONLY)
    dr = row["required_data"]["evaluation_data_verified"] and (not needs_u or u_dir["status"] == "SUPPLIED_VERIFIED") and not (needs_u and not u_dir["supplied"])
    # Round 18: an evaluation that ran on a history in which some lifecycle-eligible symbol is missing or not COMPLETE over the window (its own ``input.dataset_state``) cannot stand on
    # DATA_READY, whatever the dataset-level manifests say
    ev_state = None
    if isinstance(evaluation, Mapping):
        ev_state = evaluation.get("dataset_state") if isinstance(evaluation.get("dataset_state"), Mapping) else \
            (evaluation["input"].get("dataset_state") if isinstance(evaluation.get("input"), Mapping) else None)
    ev_state_reasons: List[str] = []
    if isinstance(ev_state, Mapping) and ev_state.get("state") not in COMPLETE_DATA_STATES:
        dr = False
        ev_state_reasons = [f"the evaluation's own dataset is {ev_state.get('state')}: " + "; ".join(str(x) for x in (ev_state.get("reasons") or [])[:2])]
    pp = spec.get("prospective_protocol") if isinstance(spec.get("prospective_protocol"), dict) else {}
    row["readiness"] = readiness_levels(
        data_ready=bool(dr), data_reasons=[f"{i['name'][:60]}: {i['status']}" for i in row["required_data"]["items"] if not i["execution_only"] and not (i["trusted"] and i["complete"])] + ev_state_reasons,
        pit_ok=bool(pit_ok), survivorship=surv, mechanism_ok=bool(row["economic_mechanism"]["present"]), evaluation=evaluation,
        prospective={"started": pp.get("start_ms") is not None, "settlements_observed": 0, "min_settlements": pp.get("min_settlements")}, executable=bool(ex["executable"]))
    row["readiness_level"] = row["readiness"]["highest"]
    return row


def _oos(spec: Mapping[str, Any]) -> Dict[str, Any]:
    pp = spec.get("prospective_protocol")
    if isinstance(pp, dict):
        return {"value": "POSSIBLE_NOT_STARTED" if pp.get("start_ms") is None else "STARTED", "min_duration_ms": pp.get("min_duration_ms"), "min_settlements": pp.get("min_settlements")}
    if pp:
        return {"value": "REGISTERED_PLAN", "plan": str(pp)[:120]}
    return {"value": "NO_PROTOCOL"}


def build_report(*, data_root: Optional[str] = None, with_economics: bool = True, universe_dir: Optional[str] = None,
                 evaluations: Optional[Mapping[str, Mapping[str, Any]]] = None) -> Dict[str, Any]:
    """The readiness table. ``universe_dir`` is the explicit universe_capture directory of the cross-sectional path: it is recognised wherever it lives (``data_scan`` lists it) and a
    candidate that needs a universe is reported UNIVERSE_DIR_REQUIRED without it. Every ``required_data`` item is judged from re-verified manifests, never from the spec's repo_has.
    ``evaluations`` ({candidate name: evaluation dict}) lets a caller attach a completed walk-forward so PROMISING can be judged."""
    scan = scan_data_root(data_root, universe_dirs=[universe_dir] if universe_dir else ())
    inv = verified_inventory(scan) if (scan["checked"] or scan["universe_capture_dirs"]) else None
    econ = btc_carry_economics() if with_economics else None
    rows = [candidate_row(n, s, scan, econ, inv, (evaluations or {}).get(n)) for n, s in load_candidates()]
    return {"schema": SCHEMA, "tool": "tools/candidate_readiness.py", "data_scan": scan, "findings": list(scan.get("findings", [])), "inventory": inv, "cost_constants": cost_constants(), "owner_observed": OWNER_INTERVAL_NOTE,
            "economics": {"btc_carry_corpus": econ}, "candidates": rows, "readiness_levels": list(READINESS_LEVELS),
            "caveats": ["every number sourced from the committed corpus is IN-SAMPLE; no out-of-sample settlement exists in this repository",
                        "MAKER_FIRST figures are a MODELLED execution scenario, never measured; fee tier is the published BASE tier, the account's tier is read at run time",
                        "no verdict here exceeds INSUFFICIENT_DATA: that needs walk-forward and prospective evidence, a registry and the owner's attestation",
                        "a dataset-level DATA_READY never becomes strategy-level READY; a SURVIVORS_ONLY evaluation implies no survivorship-free claim"]}


def render_table(rep: Mapping[str, Any]) -> str:
    cols = ("candidate", "data", "PIT", "universe_ok", "mechanism", "executable", "edge_vs_cost", "hedge", "depth", "family", "oos", "verdict")
    lines = ["\t".join(cols)]
    for r in rep["candidates"]:
        lines.append("\t".join(str(x) for x in (
            r["name"], r["required_data"]["value"], r["point_in_time"]["value"], r["universe_complete_for_claim"], "yes" if r["economic_mechanism"]["present"] else "FLAG",
            r["executable_on_qualified_venues"]["value"], r["edge_vs_costs"]["value"], r["price_leg_hedge_risk_measured"]["value"],
            r["capacity_supported_by_depth"]["value"], f"{r['multiple_testing']['family']}:{r['multiple_testing']['variants_enumerated']}",
            r["oos_or_prospective_possible"]["value"], r["readiness_verdict"])))
    return "\n".join(lines)


# ---------------------------------------------------------------- ranked-funding evaluator (formation window != holding window)
MS_DAY = 86_400_000
KEY_BASIS_LABEL = "OBSERVATION_TIME"      # a LABEL for reports only: it proves nothing and nothing compares against it


class _Sentinel:
    """A process-local capability token, compared by identity. It cannot be written in JSON, typed into a test fixture by accident, or reconstructed by copy/pickle."""
    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return f"<{self._name}>"

    def __copy__(self) -> "_Sentinel":
        return self

    def __deepcopy__(self, _memo: Any) -> "_Sentinel":
        return self

    def __reduce__(self) -> Any:
        raise TypeError("a loader-only token is never serialised")


_KEY_BASIS_OBSERVED = _Sentinel("key_basis:observation_time (load_history_root only)")      # attached by ``load_history_root`` and by nothing else
_UNIVERSE_SEAL = _Sentinel("universe verdict seal (load_history_root only)")


@dataclass(frozen=True)
class UniverseVerdict:
    """The survivorship facts ``load_history_root`` derived, frozen. ``evaluate_ranked_funding`` reads these and never the (mutable, forgeable) info dict."""
    survivorship: str
    closed_count: int
    closed_names: Tuple[str, ...]
    _seal: Any = field(default=None, repr=False, compare=False)


class LoadedInfo(dict):
    """The ``info`` dict returned by ``load_history_root``: a plain dict for printing, plus the frozen ``verdict`` the evaluator trusts. A copy (``dict(info)``,
    ``{**info}``) or a hand-made dict has no verdict and is therefore UNVERIFIED (SURVIVORS_ONLY)."""
    verdict: Optional[UniverseVerdict] = None
STALENESS_INTERVALS = 2                   # a mark older than this many mark intervals at t is not "the price at t"
NAMES_CAP = 20


@dataclass
class SymbolSeries:
    """One perp from a market_history capture. ``funding`` = [(ts_ms, rate_bps_per_print)], ``mark`` = [(OBSERVATION time ms, price)], both ascending.
    The mark is keyed by WHEN IT IS KNOWABLE (the close of the candle that starts at ``ts`` is known only at ``ts + interval``), never by the candle start: a decision at t may
    use only keys <= t. ``spot_listed``: True/False from ONE current spot snapshot (``CURRENT_SNAPSHOT_ONLY``), None = unknown (then hedgeability is UNKNOWN, never assumed).
    ``funding_intervals`` = [(first_ts, last_ts, interval_min)] from the funding manifest's segments: each print is normalised by the interval of the segment that contains it
    (the earliest containing segment for a boundary print). None = hand-built series: ``interval_min`` applies to every print."""
    symbol: str
    interval_min: Optional[int]
    funding: List[Tuple[int, float]]
    mark: List[Tuple[int, float]]
    launch_ms: int = 0
    delivery_ms: Optional[int] = None
    spot_listed: Optional[bool] = None
    oi_usd: Optional[float] = None
    funding_intervals: Optional[List[Tuple[int, int, int]]] = None
    key_basis: Any = None                    # the loader-only sentinel ``_KEY_BASIS_OBSERVED`` (identity compare) only when built by ``load_history_root``; anything else (a string, True) = hand-built, unverified
    mark_interval_ms: Optional[int] = None   # the mark-kline interval from the mark manifest (staleness bound = 2 intervals); None = inferred from the mark spacing
    settlement_price: Optional[float] = None  # the venue's delivery/settlement price of a Closed contract, from a verified source. NO loader fills it today: terminal exits are then priced only by a mark within one interval of delivery


def hedgeability(s: SymbolSeries) -> str:
    return {True: "HEDGEABLE_ON_BYBIT", False: "PERP_ONLY"}.get(s.spot_listed, "HEDGE_UNKNOWN")


def _last_row_at_or_before(seq: Sequence[Tuple[int, float]], t: int) -> Optional[Tuple[int, float]]:
    lo, hi = 0, len(seq)
    while lo < hi:
        mid = (lo + hi) // 2
        if seq[mid][0] <= t:
            lo = mid + 1
        else:
            hi = mid
    return seq[lo - 1] if lo else None


def assert_usable_at(observation_time: int, decision_time: int) -> None:
    """``market_history.usable_at`` (replay mode: a historical backfill read as if received on time) or LookAheadError. Receipt time is not stored per row, so the
    result says ``time_mode: REPLAY_OBSERVATION_TIME``: a live decision would additionally need local_receive_time <= decision."""
    if not MH.usable_at({"observation_time": observation_time}, decision_time, "replay"):
        raise RH.LookAheadError(f"a value observable at {observation_time} was read by a decision at {decision_time}")


def price_at(seq: Sequence[Tuple[int, float]], t: int, max_staleness_ms: Optional[int] = None) -> Optional[float]:
    """The last price KNOWABLE at ``t`` (key = observation time <= t), checked against ``usable_at``; None when nothing is known yet, or, with ``max_staleness_ms``, when the
    last known price is older than that (a stale price is never carried forward silently)."""
    row = _last_row_at_or_before(seq, t)
    if row is None:
        return None
    if max_staleness_ms is not None and t - row[0] > max_staleness_ms:
        return None
    assert_usable_at(row[0], t)
    return row[1]


def _last_at_or_before(seq: Sequence[Tuple[int, float]], t: int) -> Optional[float]:
    return price_at(seq, t)


def _sum_range(seq: Sequence[Tuple[int, float]], a: int, b: int) -> Tuple[float, int]:
    vals = [v for ts, v in seq if a < ts <= b]
    return sum(vals), len(vals)


def _interval_at(s: SymbolSeries, ts: int) -> Optional[float]:
    """The funding interval (minutes) in force for the print at ``ts``: its manifest segment when known, else the single declared interval (hand-built series only)."""
    if s.funding_intervals is None:
        return s.interval_min
    for a, b, iv in s.funding_intervals:
        if a <= ts <= b:
            return iv
    return None


def formation_score(s: SymbolSeries, t: int, formation_ms: int) -> Optional[float]:
    """Mean funding in bps PER DAY over (t - formation, t], using ONLY prints with ts <= t, each print normalised by the interval of ITS OWN segment (a symbol that moved
    from 480m to 240m is not rescaled by today's interval). None when unknown (no interval for a print / no prints)."""
    if s.interval_min is None:
        return None
    per_day: List[float] = []
    for ts, v in s.funding:
        if t - formation_ms < ts <= t:
            iv = _interval_at(s, ts)
            if iv is None or iv <= 0:
                return None
            per_day.append(v * 1440.0 / iv)
    if not per_day:
        return None
    return statistics.fmean(per_day)


def _active(s: SymbolSeries, t: int) -> bool:
    return s.launch_ms <= t and (s.delivery_ms is None or t < s.delivery_ms)


def staleness_bound_ms(s: SymbolSeries, override: Optional[int] = None) -> Optional[int]:
    """Largest age (ms) of the last mark that may still price a leg: ``override``, else 2 x the manifest mark interval, else 2 x the median mark spacing (hand-built series),
    else None (fewer than two marks: nothing to infer from)."""
    if override is not None:
        return int(override)
    iv = s.mark_interval_ms if isinstance(s.mark_interval_ms, int) and not isinstance(s.mark_interval_ms, bool) and s.mark_interval_ms > 0 else None
    if iv is None and len(s.mark) >= 2:
        iv = int(statistics.median(b[0] - a[0] for a, b in zip(s.mark, s.mark[1:]) if b[0] > a[0]) or 0) or None
    return STALENESS_INTERVALS * iv if iv else None


def _terminal_price(s: SymbolSeries, override: Optional[int]) -> Tuple[Optional[float], Optional[str]]:
    """The price a leg is closed at when its contract is delivered during the hold: a verified settlement price, else the last mark at or before deliveryTime when it is no
    older than ONE mark interval (``override``, when given and smaller, tightens it). (None, None) = unpriced."""
    sp = s.settlement_price
    if isinstance(sp, (int, float)) and not isinstance(sp, bool) and math.isfinite(sp) and sp > 0:
        return float(sp), "SETTLEMENT_PRICE"
    iv = s.mark_interval_ms if isinstance(s.mark_interval_ms, int) and not isinstance(s.mark_interval_ms, bool) and s.mark_interval_ms > 0 else None
    if iv is None and len(s.mark) >= 2:
        iv = int(statistics.median(b[0] - a[0] for a, b in zip(s.mark, s.mark[1:]) if b[0] > a[0]) or 0) or None
    if iv is None or s.delivery_ms is None:
        return None, None
    if override is not None:
        iv = min(iv, int(override))
    row = _last_row_at_or_before(s.mark, s.delivery_ms)
    if row is None or s.delivery_ms - row[0] > iv or not row[1] > 0:
        return None, None
    return row[1], "LAST_MARK_WITHIN_1_MARK_INTERVAL_OF_DELIVERY"


def run_variant(series: Sequence[SymbolSeries], *, formation_days: int, hold_days: int, top_k: int, round_trip_bps: float, t_start: int, t_end: int,
                max_staleness_ms: Optional[int] = None) -> Dict[str, Any]:
    """Non-overlapping holds on a fixed rebalance grid. At each t: rank on the FORMATION window ending at t, hold (t, t+hold] (capped at a Closed contract's deliveryTime),
    short the top ``top_k`` perp-only. Per row: directional total = funding received + price leg of the short - perp round-trip cost (measured from mark prices);
    for HEDGEABLE symbols also the funding-net-of-cost 'carry' (basis residual UNMEASURED here).
    A stale entry or exit mark (older than ``max_staleness_ms``, default 2 mark intervals) SKIPS the leg (``skipped_legs_without_marks``).
    A TERMINAL exit (a contract delivered/closed during the hold) is never priced by the ordinary 2-interval staleness rule: a delisting can crash inside the last interval. It is priced
    only by a verified ``settlement_price``, else by the last mark when that mark is within ONE mark interval of deliveryTime (the residual is a move inside that final interval, stated
    on the leg as ``terminal_price_basis``). Every other terminal leg, and every entry-stale leg of a contract delisted during the hold, is a TERMINAL EXIT UNPRICED: the loss is
    unknown, the leg is listed in ``terminal_exit_unpriced_legs`` (with its reason) and the evaluation cannot pass."""
    f_ms, h_ms = formation_days * MS_DAY, hold_days * MS_DAY
    rows: List[Dict[str, Any]] = []
    skipped = 0
    unpriced: List[Dict[str, Any]] = []
    t = t_start
    while t + h_ms <= t_end:
        scored = sorted(((formation_score(s, t, f_ms), s) for s in series if _active(s, t)), key=lambda x: (-(x[0] if x[0] is not None else -1e18), x[1].symbol))
        picks = [(sc, s) for sc, s in scored if sc is not None][:top_k]
        legs = []
        for sc, s in picks:
            end = min(t + h_ms, s.delivery_ms) if s.delivery_ms is not None else t + h_ms
            bound = staleness_bound_ms(s, max_staleness_ms)
            terminal = s.delivery_ms is not None and s.delivery_ms <= t + h_ms
            m0 = price_at(s.mark, t, bound)
            terminal_basis = None
            if terminal:
                m1, terminal_basis = _terminal_price(s, max_staleness_ms)
            else:
                m1 = price_at(s.mark, end, bound)
            fund, nprints = _sum_range(s.funding, t, end)
            if m0 is None or m1 is None or m0 <= 0:
                skipped += 1
                if terminal:
                    unpriced.append({"symbol": s.symbol, "t": t, "delivery_ms": s.delivery_ms,
                                     "reason": "entry_mark_stale_and_delisted_during_hold" if (m0 is None or m0 <= 0) else "no_settlement_price_and_last_mark_older_than_1_mark_interval_at_delivery"})
                continue
            price_bps = -(m1 / m0 - 1.0) * 1e4
            hed = hedgeability(s)
            leg = {"symbol": s.symbol, "interval_min": s.interval_min, "hedgeability": hed, "formation_bps_per_day": sc, "funding_bps": fund, "prints": nprints, "hedgeability_basis": HEDGE_BASIS,
                   "price_leg_bps": price_bps, "directional_total_bps": fund + price_bps - round_trip_bps, "closed_during_hold": terminal}
            if terminal:
                leg["terminal_price_basis"] = terminal_basis
            if hed == "HEDGEABLE_ON_BYBIT":
                leg["hedged_carry_funding_net_of_cost_bps"] = fund - float(CC.position_class_costs("acquire_spot_perp")["total_bps"])      # spot + perp round trip
                leg["basis_residual"] = "UNMEASURED"
            if s.oi_usd:
                leg["max_notional_usd_assumed_1pct_of_oi"] = s.oi_usd * PARTICIPATION_CAP
            else:
                leg["max_notional_usd_assumed_1pct_of_oi"] = None
            legs.append(leg)
        if legs:
            rows.append({"t": t, "legs": legs, "row_total_bps": statistics.fmean(l["directional_total_bps"] for l in legs)})
        t += h_ms
    return {"rows": rows, "skipped_legs_without_marks": skipped, "terminal_exit_unpriced": len(unpriced), "terminal_exit_unpriced_legs": unpriced,
            "terminal_exit_unpriced_symbols": sorted({x["symbol"] for x in unpriced})[:NAMES_CAP], "terminal_bound": "settlement_price_else_last_mark_within_1_mark_interval_of_delivery",
            "staleness_bound": "explicit" if max_staleness_ms is not None else f"{STALENESS_INTERVALS}x_mark_interval_per_symbol", "time_mode": "REPLAY_OBSERVATION_TIME"}


def _verified_verdict(universe_info: Any) -> Optional[UniverseVerdict]:
    """The loader's own frozen verdict, or None for anything else (a plain dict, a copy, a subclass built elsewhere): UNVERIFIED."""
    v = getattr(universe_info, "verdict", None) if isinstance(universe_info, LoadedInfo) else None
    return v if isinstance(v, UniverseVerdict) and v._seal is _UNIVERSE_SEAL else None


def _survivorship_of(universe_info: Any) -> str:
    v = _verified_verdict(universe_info)
    if v is None or v.survivorship != DELISTINGS_OBSERVED or v.closed_count != 0:
        return SURVIVORS_ONLY                                                           # unverified, nothing said, or Closed contracts missing: survivors only (fail closed)
    return DELISTINGS_OBSERVED


def evaluate_ranked_funding(series: Sequence[SymbolSeries], *, spec: Optional[Mapping[str, Any]] = None, formation_days_grid: Sequence[int] = (3, 7, 14, 30),
                            hold_days: int = 7, top_k: int = 1, round_trip_bps: Optional[float] = None, min_rows: int = 60, alpha: float = 0.05,
                            hold_days_grid: Optional[Sequence[int]] = None, top_k_grid: Optional[Sequence[int]] = None,
                            cost_scenarios: Optional[Mapping[str, float]] = None, universe_info: Optional[Mapping[str, Any]] = None,
                            ledger_path: Optional[str] = None, max_staleness_ms: Optional[int] = None,
                            allow_unverified_key_basis_for_tests: bool = False) -> Dict[str, Any]:
    """Walk-forward evaluation of 'short the perps with the highest trailing funding'. EVERY variant tried in the call is a member of the multiple-testing family:
    formation windows x hold lengths x top-k x cost scenarios (``variants_tried``; the count is ``n_variants_counted``). Variants that share the primary ``hold_days`` share
    one time grid and may be SELECTED (on the TRAIN rows only); a different hold length has a different row grid, so it is COUNTED but never selectable. Returns a verdict
    capped at INSUFFICIENT_DATA (REJECTED when an adequately sized out-of-sample is non-positive, or earlier by the reject-only ``early_rejection`` rule). The trailing same-window observation is reported separately and labelled;
    it is not an expectation.

    ``universe_info`` is the ``info`` from ``load_history_root``: it supplies ``survivorship`` (absent = SURVIVORS_ONLY). ``multiple_testing_scope`` is PER_PROCESS for the
    default path-less ledger (the count survives only inside this process) and PERSISTENT_LEDGER_FILE with ``ledger_path``."""
    unkeyed = sorted(s.symbol for s in series if s.key_basis is not _KEY_BASIS_OBSERVED)
    if unkeyed and not allow_unverified_key_basis_for_tests:
        raise RH.HarnessError(f"{len(unkeyed)} series lack the loader's key_basis token (only load_history_root sets it; a string is not accepted): marks/funding keyed by candle start would leak the future; "
                              f"first: {unkeyed[:3]}")
    _loaded = universe_info.get("symbols_loaded_names") if (isinstance(universe_info, Mapping) and _verified_verdict(universe_info) is not None) else None
    if _loaded is not None and sorted(s.symbol for s in series) != list(_loaded):
        raise RH.HarnessError("SERIES_DISAGREES_WITH_LOADED_INFO: the series differ from the symbols the loader returned, so its coverage, dataset state and identity describe a different universe "
                              f"(loaded {len(_loaded)}, evaluated {len(series)}); a symbol is never dropped by the caller after the loader judged the root")
    spec = spec if spec is not None else _default_spec()
    c = cost_constants()
    cost = float(round_trip_bps) if round_trip_bps is not None else c["perp_round_trip_total_bps"]
    holds = sorted({int(hold_days), *(int(h) for h in (hold_days_grid or ()))})
    ks = sorted({int(k) for k in (top_k_grid or (top_k,))})
    costs = {str(k): float(v) for k, v in (cost_scenarios or {"base": cost}).items()}
    unknown_iv = sorted(s.symbol for s in series if s.interval_min is None)
    survivorship = _survivorship_of(universe_info)
    warnings: List[str] = []
    if survivorship == SURVIVORS_ONLY:
        warnings.append("SURVIVORS_ONLY: delistings were not observed in the universe capture (Closed partition refused, empty or absent): the result is a survivorship-biased "
                        "evaluation and no point-in-time claim")
    verdict = _verified_verdict(universe_info)
    cin = {"count": verdict.closed_count, "names": list(verdict.closed_names)} if verdict is not None else None
    if cin and cin["count"]:
        warnings.append(f"SURVIVORS_ONLY: {cin['count']} Closed/delivered perpetuals of the universe record that overlap the study window are absent from the evaluated series "
                        f"(first: {cin['names'][:5]})")
    elif universe_info is not None and verdict is None:
        warnings.append("SURVIVORS_ONLY (universe info UNVERIFIED): it was not produced by load_history_root (a hand-made or copied dict), so a DELISTINGS_OBSERVED claim in it, "
                        "or any closed_in_universe_not_in_series check, is ignored (downgraded)")
    if unkeyed:
        warnings.append(f"KEY_BASIS_UNVERIFIED: {len(unkeyed)} hand-built series were not keyed by observation time here (test-only path): the verdict is capped at INSUFFICIENT_DATA")
    if any(sp.spot_listed is not None for sp in series):
        warnings.append("hedgeability comes from ONE current spot snapshot (CURRENT_SNAPSHOT_ONLY): it labels legs, it is not a historical fact")
    out: Dict[str, Any] = {"schema": SCHEMA + "/ranked_funding", "round_trip_bps_used": cost, "cost_basis": c["basis"],
                           "symbols": len(series), "hedgeability_counts": {}, "intervals_min_counts": {}, "symbols_without_interval": unknown_iv, "closed_contracts": sum(1 for s in series if s.delivery_ms),
                           "ranking_rule_variants": list(formation_days_grid), "hold_days": hold_days, "top_k": top_k, "ceiling": "INSUFFICIENT_DATA",
                           "hold_days_grid": holds, "top_k_grid": ks, "cost_scenarios_bps": dict(costs), "survivorship": survivorship, "warnings": warnings,
                           "closed_in_universe_not_in_series": cin, "key_basis_unverified": bool(unkeyed),
                           "survivorship_free_claim": survivorship == DELISTINGS_OBSERVED,
                           "history_coverage": (universe_info or {}).get("history_coverage") if isinstance(universe_info, Mapping) else None,
                           "dataset_state": universe_info.get("dataset_state") if _verified_verdict(universe_info) is not None else None,        # only the loader's own sealed info: a hand-made dict cannot forge it
                           "frozen_mark_symbols": universe_info.get("frozen_mark_symbols") if _verified_verdict(universe_info) is not None else None,
                           "loaded_identity": universe_info.get("loaded_identity") if _verified_verdict(universe_info) is not None else None,
                           "survivorship_claim_note": ("DELISTINGS_OBSERVED: Closed contracts the venue still serves were observed; an absent symbol is still not proof it was never delisted"
                                                       if survivorship == DELISTINGS_OBSERVED else "SURVIVORS_ONLY: no survivorship-free claim can be made from this evaluation"),
                           "universe_info_verified": verdict is not None,
                           "skipped_legs_without_marks": 0, "terminal_exit_unpriced": 0,
                           "evidence_class": "NOT_EVALUATED", "multiple_testing_scope": "PER_PROCESS" if ledger_path is None else "PERSISTENT_LEDGER_FILE",
                           "time_mode": "REPLAY_OBSERVATION_TIME", "hedgeability_basis": HEDGE_BASIS, "pit_label_note": PIT_LABEL_NOTE}
    for s in series:
        out["hedgeability_counts"][hedgeability(s)] = out["hedgeability_counts"].get(hedgeability(s), 0) + 1
        k = str(s.interval_min)
        out["intervals_min_counts"][k] = out["intervals_min_counts"].get(k, 0) + 1
    if unknown_iv:
        out.update({"verdict": "INSUFFICIENT_DATA", "status": "FUNDING_NORMALISATION_UNKNOWN",
                    "reason": f"{len(unknown_iv)} symbols carry no fundingInterval; funding cannot be normalised, so the candidate cannot pass"})
        return out
    if not series:
        out.update({"verdict": "UNTESTED", "status": "NO_SERIES", "reason": "no series given"})
        return out
    t0 = min(min((ts for ts, _ in s.funding), default=10 ** 18) for s in series)
    t1 = max(max((ts for ts, _ in s.funding), default=0) for s in series)
    fam = RH.normalise_family(spec.get("multiple_testing_family") or spec.get("candidate_id"))
    ledger = RH.VariantLedger(fam, path=ledger_path)
    skipped_by_variant: Dict[str, int] = {}
    unpriced_keys: set = set()
    per: Dict[str, List[float]] = {}                  # selectable: same hold as the primary => one row grid
    counted_only: Dict[str, Tuple[List[float], int]] = {}
    params_of: Dict[str, Dict[str, Any]] = {}
    for F in formation_days_grid:
        start = t0 + max(formation_days_grid) * MS_DAY
        for h in holds:
            for k in ks:
                for cname, cbps in costs.items():
                    name = f"F{F}|H{h}|K{k}|C{cname}"
                    params_of[name] = {"formation_days": F, "hold_days": h, "top_k": k, "round_trip_bps": cbps}
                    r = run_variant(series, formation_days=F, hold_days=h, top_k=k, round_trip_bps=cbps, t_start=start, t_end=t1, max_staleness_ms=max_staleness_ms)
                    skipped_by_variant[name] = r["skipped_legs_without_marks"]
                    unpriced_keys.update((x["symbol"], x["t"], h) for x in r["terminal_exit_unpriced_legs"])
                    rets = [x["row_total_bps"] for x in r["rows"]]
                    if h == hold_days:
                        per[name] = rets
                    else:
                        counted_only[name] = (rets, h)
    out["variants_tried"] = sorted(list(per) + list(counted_only))
    out["skipped_legs_without_marks"] = max(skipped_by_variant.values(), default=0)
    out["skipped_legs_without_marks_by_variant"] = dict(sorted(skipped_by_variant.items()))
    out["terminal_exit_unpriced"] = len({(a, b) for a, b, _ in unpriced_keys})
    out["terminal_exit_unpriced_symbols"] = sorted({a for a, _, _ in unpriced_keys})[:NAMES_CAP]
    out["staleness_bound"] = "explicit" if max_staleness_ms is not None else f"{STALENESS_INTERVALS}x_mark_interval_per_symbol"
    if out["terminal_exit_unpriced"]:
        warnings.append(f"TERMINAL_EXIT_UNPRICED: {out['terminal_exit_unpriced']} delisted legs have no fresh mark at delivery (loss unknown): the verdict is capped at INSUFFICIENT_DATA")
    n_rows = min((len(v) for v in per.values()), default=0)
    out["rows_per_variant"] = n_rows
    out["trailing_observation"] = _trailing(series, formation_days_grid[-1] if formation_days_grid else 7)
    if n_rows < min_rows:
        for name in out["variants_tried"]:
            if not ledger.knows(name):                      # an identical re-run is the same trial, not a new one (evaluate_family and regime_entry_eval already say so)
                ledger.record_rejected(name, params_of[name], f"only {n_rows} non-overlapping holding rows (< {min_rows})")
        out.update({"verdict": "INSUFFICIENT_DATA", "status": "TOO_FEW_HOLDING_ROWS", "evidence_class": "SHORT_DIAGNOSTIC_NOT_STRATEGY_EVIDENCE", "family": fam, "n_variants_counted": ledger.n_variants,
                    "n_variants_tried_in_call": len(out["variants_tried"]),
                    "required_span": grid_requirements(formation_days=max(formation_days_grid), hold_days=hold_days, min_rows=min_rows, span_days=(t1 - t0) / MS_DAY),
                    "reason": f"{n_rows} non-overlapping {hold_days}-day holding rows after the formation lead-in; need >= {min_rows}, which needs "
                              f"{grid_requirements(formation_days=max(formation_days_grid), hold_days=hold_days, min_rows=min_rows)['window_days_required']} days of history (warmup + formation + {min_rows} x {hold_days}); "
                              f"this capture spans {(t1 - t0) / MS_DAY:.1f} days. The same-window trailing sums below are "
                              f"{LABEL_TRAILING}, selected after the fact, not an expectation and not a verdict"})
        return out
    cut = {k: v[:n_rows] for k, v in per.items()}
    for name, rets in cut.items():
        if RH.series_digest(rets) not in ledger.returns_digests(name):          # same variant on the very same series = the same trial; new data or a new name counts
            ledger.record(name, params_of[name], rets, horizon=1)
    for name, (rets, h) in counted_only.items():
        if len(rets) >= 2:
            if RH.series_digest(rets) not in ledger.returns_digests(name):
                ledger.record(name, params_of[name], rets, horizon=1)
        elif not ledger.knows(name):
            ledger.record_rejected(name, params_of[name], "fewer than two holding rows for this hold length")
    folds = RH.walk_forward_folds(n_rows, train_min=max(20, n_rows // 2), test_len=max(5, n_rows // 10), label_horizon=1, embargo=1)
    oos: List[float] = []
    chosen: List[str] = []
    for f in folds:
        pick = RH.train_only_best({k: [v[i] for i in f.train] for k, v in cut.items()})
        chosen.append(pick)
        oos.extend(cut[pick][i] for i in f.test)
    st = RH.significance(oos)
    m = ledger.n_variants
    adj = SP.adjust_pvalues([st["p_one_sided"]], "holm", alpha=alpha, n_tested=m)["adjusted"][0]
    mean = st["mean"]
    enough = st["independent_observations"] >= SV.MIN_OBSERVATIONS["oos_test"]
    verdict = "REJECTED" if enough and (mean is None or mean <= 0 or adj > alpha) else "INSUFFICIENT_DATA"
    early = early_rejection(st)
    early_applied = False
    if verdict == "INSUFFICIENT_DATA" and early:
        verdict, early_applied = "REJECTED", True         # reject-only: a significantly negative net cannot clear its costs, whatever the sample size
    if out["terminal_exit_unpriced"] or unkeyed:
        verdict, early_applied = "INSUFFICIENT_DATA", False   # unknown terminal losses / unverified timing: neither a pass nor a rejection can be claimed
    out["reject_early"] = early if early_applied else None
    out["reject_early_suppressed_by_cap"] = bool(early and not early_applied and verdict != "REJECTED")
    out.update({"status": "WALK_FORWARD_DONE", "evidence_class": "FULL_SPAN_WALK_FORWARD_NOT_PROFIT_EVIDENCE", "verdict": verdict, "family": fam, "n_variants_counted": m, "n_variants_tried_in_call": len(out["variants_tried"]),
                "correction": "holm", "oos_rows": len(oos), "oos_mean_total_bps_per_row": mean,
                "oos_independent_observations": st["independent_observations"], "p_one_sided": st["p_one_sided"], "p_adjusted": adj, "selected_per_fold": chosen,
                "label": "OUT_OF_SAMPLE_WITHIN_THE_CAPTURE_ONLY: one regime, one capture; not prospective",
                "reason": "directional total = funding + short price leg - perp round trip; PERP_ONLY legs are never carry. A pass would still need prospective evidence"
                          + ((" | REJECTED_EARLY: " + early["rule"]) if early_applied else "")})
    return out


def _trailing(series: Sequence[SymbolSeries], days: int) -> Dict[str, Any]:
    """The same-window funding sums for the current top of the ranking: a LABELLED trailing observation (selected after the fact), per symbol with its hedgeability."""
    t = max((max((ts for ts, _ in s.funding), default=0) for s in series), default=0)
    lo = t - days * MS_DAY
    rows = []
    for s in series:
        fund, n = _sum_range(s.funding, lo, t)
        m0, m1 = price_at(s.mark, lo), price_at(s.mark, t)
        price = None if m0 is None or m1 is None or m0 <= 0 else -(m1 / m0 - 1.0) * 1e4
        hed = hedgeability(s)
        per_day = [v * 1440.0 / iv for ts, v in s.funding if lo < ts <= t for iv in [_interval_at(s, ts)] if iv]
        r = {"symbol": s.symbol, "hedgeability": hed, "interval_min": s.interval_min, "prints": n, "gross_funding_bps_window": fund,
             "gross_funding_bps_per_day": None if s.interval_min is None or n == 0 or len(per_day) != n else statistics.fmean(per_day),
             "short_price_leg_bps_same_window": price}
        if hed != "HEDGEABLE_ON_BYBIT":
            r["note"] = "no Bybit spot pair confirmed: an UNHEDGED short is directional price exposure plus funding, not a carry"
        rows.append(r)
    rows.sort(key=lambda r: -(r["gross_funding_bps_window"]))
    return {"label": LABEL_TRAILING, "is_expectation": False, "window_days": days, "selection": "ranked AFTER the fact on this same window", "top": rows[:10]}


# ---------------------------------------------------------------- market_history ingestion (the layout is owned by tools/market_history.py)
EXCLUDED_SYMBOL_STATUSES = ("CONFLICTED", "UNVERIFIED")
SPOT_VERDICT_TO_LISTED = {"SPOT_TRADING": True, "SPOT_NOT_TRADING": False, "NO_SPOT_PAIR": False}      # UNRESOLVED_MULTIPLIER and anything else: unknown (None)


def _default_spec() -> Dict[str, Any]:
    """The ranked-funding candidate spec, found BY NAME (never by position in the candidate list)."""
    for n, sp in load_candidates():
        if n == "cross_sectional_funding":
            return dict(sp)
    raise RH.HarnessError("candidate 'cross_sectional_funding' not found among the research candidates")


def _verified_dataset(root: str, name: str) -> Tuple[Dict[str, Any], str]:
    ddir = os.path.join(root, name)
    if os.path.islink(ddir) or not os.path.isdir(ddir):
        raise RH.HarnessError(f"dataset {name!r} not found under the history root")
    v = MH.verify_dataset(ddir)
    if not v.get("ok"):
        raise RH.HarnessError(f"dataset {name!r} does not verify: " + "; ".join(str(x) for x in v.get("problems", [])[:3]))
    with open(os.path.join(ddir, "manifest.json"), "rb") as fh:
        man = json.loads(fh.read())
    return man, ddir


def _rows(ddir: str, sym: str) -> List[Dict[str, Any]]:
    out = []
    with open(os.path.join(ddir, sym, "rows.jsonl"), "rb") as fh:
        for line in fh.read().splitlines():
            r = json.loads(line)
            if isinstance(r, dict) and isinstance(r.get("ts"), int) and isinstance(r.get("v"), dict):
                out.append(r)
    return out


def _mark_dataset_name(root: str, mark_interval: Optional[str]) -> Tuple[str, str]:
    if mark_interval is not None:
        return MH.dataset_dirname(MH.MARK, str(mark_interval)), str(mark_interval)
    found = sorted(n for n in (os.listdir(root) if os.path.isdir(root) else []) if n.startswith(MH.MARK + "-"))
    if len(found) != 1:
        raise RH.HarnessError(f"mark_interval not given and {len(found)} mark-kline datasets found under the root (need exactly one)")
    return found[0], found[0][len(MH.MARK) + 1:]


def funding_interval_segments(man_sym: Mapping[str, Any], print_ts: Sequence[int]) -> Tuple[Optional[List[Tuple[int, int, int]]], Optional[str]]:
    """[(first_ts, last_ts, interval_min)] from one symbol's funding-manifest ``segments`` (the intervals market_history inferred from the print spacing or took from the
    instrument record), or (None, reason) when ANY print falls in no segment: its interval is then unknown and the symbol is excluded, never normalised by a guess (the
    current interval of the instrument record included). A boundary print belongs to the earlier segment."""
    segs_in = man_sym.get("segments") if isinstance(man_sym, Mapping) else None
    out: List[Tuple[int, int, int]] = []
    for sg in segs_in if isinstance(segs_in, list) else []:
        try:
            a, b, ims = int(sg["first_ts"]), int(sg["last_ts"]), int(sg["interval_ms"])
        except (KeyError, TypeError, ValueError):
            return None, "funding_interval_unknown_for_all_prints(malformed segment)"
        if ims <= 0 or ims % 60_000 or b < a:
            return None, "funding_interval_unknown_for_all_prints(segment interval not a whole number of minutes)"
        out.append((a, b, ims // 60_000))
    if not out:
        return None, "funding_interval_unknown_for_all_prints(manifest carries no segments)"
    out.sort()
    bad = [ts for ts in print_ts if not any(a <= ts <= b for a, b, _ in out)]
    if bad:
        return None, f"funding_interval_unknown_for_{len(bad)}_prints(first at {bad[0]})"
    return out, None


def _survivorship_info(uinfo: Mapping[str, Any]) -> Tuple[str, str]:
    cov = uinfo.get("status_coverage") if isinstance(uinfo.get("status_coverage"), Mapping) else {}
    closed = cov.get("Closed") if isinstance(cov, Mapping) else None
    obs = uinfo.get("delisting_observability")
    note = UC.describe_delisting_observability(obs, closed)
    return (DELISTINGS_OBSERVED if obs == "OBSERVED" and closed == "OBSERVED_ROWS" else SURVIVORS_ONLY), note


def load_history_root(root: str, *, universe_dir: str, spot_dir: Optional[str] = None, mark_interval: Optional[str] = None, window: Optional[Tuple[int, int]] = None,
                      include_statuses: Sequence[str] = ('Trading', 'Closed'), quote_coins: Optional[Sequence[str]] = ('USDT',), classifier: Optional[Any] = None,
                      cut_ms: Optional[int] = None) -> Tuple[List[SymbolSeries], Dict[str, Any]]:
    """Series from a market_history root written by ``tools/market_history.py``: ``<root>/funding``, ``<root>/mark-kline-<interval>`` (rows.jsonl per symbol), plus a verified
    universe_capture directory (launch / delivery / fundingInterval_min) and optionally a spot-instruments snapshot (hedgeability).

    Nothing unverified is evaluated: each dataset must pass ``market_history.verify_dataset``, the universe ``universe_capture.verify_capture_dir`` with a RE-DERIVED
    dataset_status of COMPLETE or PARTIAL (PARTIAL is a warning), the spot snapshot ``verify_spot``; otherwise HarnessError. Funding is stored as a decimal FRACTION per print
    (bps = rate * 1e4); the mark is the kline close, KEYED BY ITS ``observation_time`` (candle start + interval; a row claiming earlier raises LookAheadError). Each funding print is
    normalised by the interval of its own manifest segment (a symbol with unknown intervals for any print is excluded and listed). ``info['survivorship']`` is SURVIVORS_ONLY unless the
    Closed partition served rows (then DELISTINGS_OBSERVED); SURVIVORS_ONLY adds a loud warning. No point-in-time claim is made here (``point_in_time_claim`` is False). Symbols must be in both datasets AND the universe; CONFLICTED / UNVERIFIED symbols and symbols missing anywhere are
    excluded and LISTED in ``info['excluded']``; other non-COMPLETE symbol statuses are kept and reported in ``info['non_complete_symbols']``. Gaps are never filled.

    THE FUNNEL. Which symbols are loaded is decided by ``backfill_plan.funnel`` (the same function ``plan``, ``diagnose`` and the audit call), not here: this function loads EXACTLY ``funnel.loadable``
    and embeds the funnel report as ``info['funnel']``. ``window`` = the (from_ms, to_ms) the history must cover (default: the window the funding manifest itself requested).

    ``cut_ms`` is the RESERVED-HOLDOUT boundary (``history_holdout``): the study window ends there and no funding print or mark close observed at or after it is built into a series, the
    funding-interval segments are clipped at it, and the funnel's COVERAGE window ends there (a symbol with no row before it is dropped and listed). What is NOT cut: facts the manifests
    compute over the WHOLE dataset (a symbol's non-COMPLETE status, a conflict, an interval disagreement) can still flag or exclude a symbol; they carry no return information. Only
    ``history_holdout`` passes it; an evaluator must go through ``history_holdout.load`` (a test enforces that nothing else calls this function)."""
    from decimal import Decimal, InvalidOperation
    if not isinstance(universe_dir, str) or not universe_dir:
        raise RH.HarnessError("universe_dir is required: instrument attributes do not live in the history manifests")
    if not os.path.isdir(root) or os.path.islink(root):
        raise RH.HarnessError("history root not found")
    try:
        recs, uinfo = MH.load_universe_dir(universe_dir)          # verify_capture_dir ok AND re-derived status COMPLETE|PARTIAL, else HistoryError
    except (MH.HistoryError, OSError, UC.CaptureError) as exc:
        raise RH.HarnessError(f"universe capture refused: {exc}") from exc
    mark_name, mark_iv = _mark_dataset_name(root, mark_interval)
    fman, fdir = _verified_dataset(root, MH.FUNDING)
    mman, mdir = _verified_dataset(root, mark_name)
    import backfill_plan as BP            # lazy: backfill_plan imports this module lazily too
    if cut_ms is not None:
        if isinstance(cut_ms, bool) or not isinstance(cut_ms, int) or cut_ms <= 0:
            raise RH.HarnessError(f"cut_ms must be a positive epoch-millisecond integer, got {cut_ms!r}")
        base_window = window if window is not None else BP.window_of_funding_manifest(os.path.abspath(root))
        if base_window is None:
            raise RH.HarnessError("HOLDOUT_CUT_NEEDS_A_WINDOW: the funding dataset records no requested window to cut")
        if cut_ms <= int(base_window[0]):
            raise RH.HarnessError(f"HOLDOUT_CUT_BEFORE_WINDOW_START: the cut {cut_ms} is not after the window start {int(base_window[0])}: nothing would be left to evaluate")
        window = (int(base_window[0]), min(int(base_window[1]), int(cut_ms)))
    try:
        fn = BP.funnel(universe_dir, root, window=window, window_source=BP.WINDOW_FROM_EXPLICIT if window is not None else BP.WINDOW_FROM_FUNDING_MANIFEST, include_statuses=include_statuses,
                       quote_coins=quote_coins, required_datasets=(MH.FUNDING, MH.MARK), intervals={MH.MARK: mark_iv}, report_datasets=(MH.FUNDING, MH.MARK), classifier=classifier,
                       universe=(recs, uinfo), pre_verified=(MH.FUNDING, mark_name))
    except BP.PlanError as exc:
        raise RH.HarnessError(str(exc)) from exc
    verdicts: Dict[str, str] = {}
    hedge_info: Optional[Dict[str, Any]] = None
    if spot_dir is not None:
        try:
            h = MH.hedge_availability(recs, spot_dir)
        except (MH.HistoryError, OSError, ValueError, KeyError) as exc:
            raise RH.HarnessError(f"spot snapshot refused: {exc}") from exc
        verdicts = {k: x["verdict"] for k, x in h["by_symbol"].items()}
        hedge_info = {"spot_snapshot": h["spot_snapshot"], "counts": h["counts"], "basis": HEDGE_BASIS,
                      "note": "spot_listed / hedgeability come from ONE current spot snapshot; they label legs and never enter a historical decision"}
    non_complete: Dict[str, Dict[str, str]] = {}
    mismatch: List[str] = []
    warnings: List[str] = []
    survivorship, delisting_note = _survivorship_info(uinfo)
    if survivorship == SURVIVORS_ONLY:
        warnings.append(f"SURVIVORS_ONLY (survivorship bias, no point-in-time claim): {delisting_note}")
    if uinfo.get("dataset_status") != "COMPLETE":
        warnings.append(f"universe capture re-derived dataset_status {uinfo.get('dataset_status')!r}: the evaluation inherits that")
    for nm, man in (("funding", fman), (mark_name, mman)):
        if man.get("dataset_status") != "COMPLETE":
            warnings.append(f"dataset {nm} dataset_status {man.get('dataset_status')!r}: the evaluation inherits that")
    excluded = [{"symbol": sy, "reason": why} for sy, why in sorted(fn.reasons_for_symbols_in_datasets().items())]
    frozen_marks: List[Dict[str, Any]] = []
    series: List[SymbolSeries] = []
    cut_dropped: List[str] = []
    delivery_hidden: List[str] = []                                         # symbols whose delisting instant lies in the reserved region (hidden from the explore series)
    for sym in fn.loadable:
        sts = {"funding": fman["symbols"][sym].get("status"), mark_name: mman["symbols"][sym].get("status")}
        if any(v != "COMPLETE" for v in sts.values()):
            non_complete[sym] = {k: str(v) for k, v in sts.items() if v != "COMPLETE"}
        try:
            frows = _rows(fdir, sym)
            mrows = _rows(mdir, sym)
            iv_ms = (mman["symbols"][sym].get("spec") or {}).get("interval_ms")
            for r in frows:
                if not isinstance(r.get("observation_time"), int) or r["observation_time"] < r["ts"]:
                    raise RH.LookAheadError(f"{sym}: a funding print claims an observation_time before its own settlement")
            for r in mrows:
                ot = r.get("observation_time")
                if not isinstance(ot, int) or isinstance(ot, bool) or ot < r["ts"] + (iv_ms if isinstance(iv_ms, int) and iv_ms > 0 else 1):
                    raise RH.LookAheadError(f"{sym}: a mark row claims its close is knowable before the candle ended (observation_time {ot!r}, candle start {r['ts']}, interval {iv_ms!r})")
            fund = sorted((r["observation_time"], float(Decimal(r["v"]["fundingRate"]) * 10000)) for r in frows if cut_ms is None or r["observation_time"] < cut_ms)
            mark = sorted((r["observation_time"], float(Decimal(r["v"]["close"]))) for r in mrows if cut_ms is None or r["observation_time"] < cut_ms)
            _need = frozen_mark_threshold(iv_ms)
            _closes = [c for _t, c in mark]
            _run = longest_identical_run(_closes)
            _stuck = has_stuck_window(_closes, 2 * _need)
            if _run >= _need or _stuck:
                frozen_marks.append({"symbol": sym, "run_candles": _run, "min_candles": _need, "min_hours": FROZEN_MARK_MIN_HOURS, "stuck_window_candles": 2 * _need if _stuck else None})
        except RH.HarnessError:
            raise
        except (KeyError, InvalidOperation, ValueError, TypeError, OSError) as exc:
            raise RH.HarnessError(f"{sym}: rows.jsonl unreadable after verification: {type(exc).__name__}") from exc
        segs, seg_why = funding_interval_segments(fman["symbols"][sym], [t for t, _ in fund])
        if cut_ms is not None and (not fund or not mark):
            # a late listing: its first print or candle is AT or after the holdout boundary, so it has nothing to explore. The funnel admitted it (it launched before the cut); dropping it here keeps
            # one such symbol from aborting every explore load, and the boundary is frozen, so an abort would be permanent.
            cut_dropped.append(sym)
            non_complete.pop(sym, None)
            excluded.append({"symbol": sym, "reason": "no_rows_before_the_holdout_cut"})
            continue
        if not fund or not mark or segs is None:
            raise RH.HarnessError(f"FUNNEL_DISAGREES_WITH_LOADER: {sym} passed the funnel but its rows are unusable ({'no_rows' if not fund or not mark else seg_why})")
        if cut_ms is not None:
            # the interval segments are facts about the whole dataset: one that begins inside the reserved region (a funding-cap regime change) must not reach the series, and one that
            # spans the boundary ends at it
            segs = [(s0, min(s1, int(cut_ms) - 1), sv) for (s0, s1, sv) in segs if s0 < cut_ms]
        if "interval_disagrees_with_record" in (fman["symbols"][sym].get("reasons") or []):
            mismatch.append(sym)
        rec = recs[sym]
        iv = rec.get("fundingInterval_min")
        d = rec.get("deliveryTime")
        if rec.get("status") in RH.ENDED_STATUSES and not (isinstance(d, int) and not isinstance(d, bool) and d > 0):
            raise RH.HarnessError(f"closed_without_delivery: {sym} is an ended contract with no delivery time (it would be immortal: stale exit price) but the lifecycle classifier let it through")
        launch = rec.get("launchTime")
        delivery = d if isinstance(d, int) and not isinstance(d, bool) and d > 0 else None
        if cut_ms is not None and delivery is not None and delivery >= int(cut_ms):
            # A delisting inside the reserved region is a fact about the reserved period: it decides which explore holds are 'terminal' and so which are priced or dropped. The explore series
            # must not know it (independent re-review: ``delivery_ms`` carried a delisting instant from the reserved region). Hidden, and listed.
            delivery_hidden.append(sym)
            delivery = None
        series.append(SymbolSeries(sym, iv if isinstance(iv, int) and not isinstance(iv, bool) and iv > 0 else None, fund, mark,
                                   launch if isinstance(launch, int) and not isinstance(launch, bool) and launch > 0 else 0,
                                   delivery, SPOT_VERDICT_TO_LISTED.get(verdicts.get(sym)) if verdicts else None,
                                   funding_intervals=segs, key_basis=_KEY_BASIS_OBSERVED,
                                   mark_interval_ms=iv_ms if isinstance(iv_ms, int) and not isinstance(iv_ms, bool) and iv_ms > 0 else None))
    if [x.symbol for x in series] != [y for y in fn.loadable if y not in set(cut_dropped)]:
        raise RH.HarnessError("FUNNEL_DISAGREES_WITH_LOADER: the loaded series are not the funnel's loadable symbols")
    in_series = {x.symbol for x in series}
    w0, w1 = fn.window
    closed_missing = sorted(fn.ended_unaccounted)
    closed_info = {"count": len(closed_missing), "names": closed_missing[:NAMES_CAP], "names_truncated": len(closed_missing) > NAMES_CAP,
                   "window_ms": [w0, w1], "rule": "every Closed/Delivering contract must be in the evaluated series or excluded for a reason that PROVES it out of scope (backfill_plan.EXPLAINED_EXCLUSION_PREFIXES); closed_without_delivery and unknown classes are unaccounted"}
    if closed_missing:
        if survivorship == DELISTINGS_OBSERVED:
            survivorship = SURVIVORS_ONLY
        warnings.append(f"SURVIVORS_ONLY: {len(closed_missing)} Closed/delivered perpetuals in the universe record overlap the study window but are absent from the evaluated series "
                        f"(first: {closed_missing[:5]}): the evaluated series is survivors-only whatever the universe capture observed")
    if cut_dropped:
        warnings.append(f"{len(cut_dropped)} symbols have no funding print or mark close before the holdout boundary (late listings; dropped, listed in excluded): {cut_dropped[:5]}")
    if non_complete:
        warnings.append(f"{len(non_complete)} symbols are not COMPLETE in a dataset (kept, listed in non_complete_symbols)")
    short_cov = sorted(set(fn.loadable) - set(fn.complete) - set(non_complete))
    if short_cov:
        warnings.append(f"{len(short_cov)} loaded symbols are not COMPLETE over the required window {w0}..{w1} (their datasets are shorter than the window; first: {short_cov[:5]})")
    if excluded:
        warnings.append(f"{len(excluded)} symbols excluded (listed in excluded)")
    n_elig = len(fn.eligible)
    cov_frac = (len(series) / n_elig) if n_elig else None
    history_cov = {"universe_eligible_symbols": n_elig, "symbols_in_history": len(series), "fraction": cov_frac,
                   "status": "HISTORY_COVERS_FEW_ELIGIBLE_SYMBOLS" if cov_frac is not None and cov_frac < 0.5 else ("OK" if cov_frac is not None else "NO_ELIGIBLE_SYMBOLS"),
                   "note": "eligible = the funnel's lifecycle-eligible perpetuals (Trading + Closed/Delivering overlapping the window); the history datasets hold only the symbols they were collected for "
                           "(an explicit --symbols list gives a small cross-section): the evaluation is over THOSE symbols, not the venue"}
    if history_cov["status"] == "HISTORY_COVERS_FEW_ELIGIBLE_SYMBOLS":
        warnings.append(f"HISTORY_COVERS_FEW_ELIGIBLE_SYMBOLS: the history datasets hold {len(series)} of {n_elig} eligible universe symbols; collect the rest (backfill_plan.plan) before trusting a cross-section")
    # the dataset-state (Round 18): the SAME per-symbol funnel facts the warnings above are made of, as one word, and the identity of exactly what was loaded. A late listing dropped at the holdout
    # boundary has nothing to explore and is not a coverage failure; every other lifecycle-eligible symbol must be loaded AND complete over the whole window.
    _cut_set = set(cut_dropped)
    _sf = _coverage_findings(eligible=[s for s in fn.eligible if s not in _cut_set], covered=[x.symbol for x in series],
                             not_complete=[s for s in set(fn.loadable) - set(fn.complete) if s not in _cut_set], ended_unaccounted=len(closed_missing),
                             window_days=(w1 - w0) / MS_DAY, survivorship=survivorship)
    state_findings: Dict[str, List[str]] = {"unavailable": [], "invalid": [], **_sf}
    for _fm in frozen_marks:
        state_findings["invalid"].append(f"{_fm['symbol']}: the mark price is frozen ({_fm['run_candles']} identical consecutive candles against a limit of {_fm['min_candles']}"
                                         + (f"; and a window of {_fm['stuck_window_candles']} candles holds at most two distinct closes" if _fm["stuck_window_candles"] else "")
                                         + f"): not a market price series, its price leg is near zero for every hold. Confirm it against the venue's public klines. This root can be neither evaluated as complete nor reserved while it holds the symbol, "
                                         f"and a NEW root without it still lists the symbol as lifecycle-eligible in the universe capture, which reads DATA_INCOMPLETE: an owner-registered exclusion policy (decision D7, "
                                         f"docs/MONEY_MISSION.md) is needed before any --reserve; nothing here drops a symbol")
    for _nm, _man in (("funding", fman), (mark_name, mman), ("the universe capture", uinfo)):
        _st = _man.get("dataset_status")
        if _st in ("CONFLICTED", "UNVERIFIED"):
            state_findings["invalid"].append(f"{_nm}: dataset_status is {_st!r}")
        elif _st != "COMPLETE":
            state_findings["incomplete"].append(f"{_nm}: dataset_status is {_st!r}, not COMPLETE")
    id_components = [{"kind": "market_history", "name": "funding", "digest": fman.get("manifest_digest"), "verified": True},        # _verified_dataset / load_universe_dir raised unless they re-hashed
                     {"kind": "market_history", "name": mark_name, "digest": mman.get("manifest_digest"), "verified": True},
                     {"kind": "universe_capture", "name": "universe", "digest": uinfo.get("manifest_sha256"), "verified": True}]
    if spot_dir is not None:             # re-verified by hedge_availability above (it raised otherwise); the carry family's universe is selected by it, so a different snapshot is a different input
        id_components.append({"kind": "spot_snapshot", "name": "spot", "digest": (_read_manifest(os.path.join(spot_dir, "manifest.json")) or {}).get("manifest_digest"), "verified": True})
    identity = dataset_identity(
        id_components,
        extra={"funnel_digest": fn.report().get("funnel_digest"), "window_ms": [w0, w1], "mark_interval": mark_iv, "holdout_cut_ms": cut_ms,
               "include_statuses": list(include_statuses), "quote_coins": list(quote_coins) if quote_coins else None})
    if not identity["identifies_every_component"]:
        state_findings["invalid"].append("a loaded manifest carries no digest: the dataset cannot be identified")
    info = LoadedInfo({"root": os.path.abspath(root), "mark_interval": mark_iv, "history_coverage": history_cov,
                       "dataset_state": dataset_state(state_findings), "loaded_identity": identity, "frozen_mark_symbols": frozen_marks,
                       "loaded_identity_scope": "the funding + mark-kline datasets, the universe capture and (when supplied) the spot snapshot this load READ; index-kline / open-interest are not loaded: "
                                                "the report-level dataset_identity of dataset_report covers every manifest under the data root",
                            "datasets": {"funding": {"verified": True, "dataset_status": fman.get("dataset_status"), "manifest_digest": fman.get("manifest_digest")},
                                         mark_name: {"verified": True, "dataset_status": mman.get("dataset_status"), "manifest_digest": mman.get("manifest_digest")}},
                            "universe": {"dir": uinfo.get("dir"), "dataset_status": uinfo.get("dataset_status"), "manifest_sha256": uinfo.get("manifest_sha256"), "symbols": uinfo.get("symbols"),
                                         "delisting_observability": uinfo.get("delisting_observability"), "status_coverage": uinfo.get("status_coverage"), "delisting_note": delisting_note},
                            "survivorship": survivorship, "funnel": fn.report(), "history_window": {"from_ms": w0, "to_ms": w1, "days": round((w1 - w0) / MS_DAY, 3), "source": fn.window_source},
                            "not_complete_over_window": sorted(set(fn.loadable) - set(fn.complete)), "closed_in_universe_not_in_series": closed_info, "point_in_time_claim": False,
                            "point_in_time_note": "this loader builds series from capture snapshots; it makes no point-in-time claim (use PointInTimeUniverse.from_capture_dirs for that). " + PIT_LABEL_NOTE,
                            "time_basis": "marks and funding are keyed by observation_time (replay mode: receipt time is not stored per row)",
                            "interval_mismatch_symbols": mismatch, "holdout_cut_ms": cut_ms, "holdout_dropped_no_rows_before_cut": cut_dropped, "holdout_delivery_hidden": delivery_hidden,
                            "non_complete_symbols": non_complete, "excluded": excluded, "symbols_loaded": len(series), "warnings": warnings})
    info.verdict = UniverseVerdict(survivorship, len(closed_missing), tuple(closed_missing[:NAMES_CAP]), _UNIVERSE_SEAL)
    if hedge_info is not None:
        info["hedge_verdicts"] = {s.symbol: verdicts.get(s.symbol) for s in series}
        info["hedge"] = hedge_info
    for _fm in frozen_marks:
        warnings.append(f"FROZEN_MARK_PRICE:{_fm['symbol']}: {_fm['run_candles']} identical consecutive mark closes; the root is DATA_INVALID and the symbol is NOT removed from the series")
    info["symbols_loaded_names"] = sorted(x.symbol for x in series)                     # what the loader actually returned: an evaluation refuses a series that differs (SERIES_DISAGREES_WITH_LOADED_INFO)
    if warnings:
        info["warning"] = "; ".join(warnings)
    return series, info


# ---------------------------------------------------------------- three strategy families, three specs, three risk models
FAMILY_CARRY, FAMILY_SPREAD, FAMILY_DIRECTIONAL = "SPOT_PERP_CARRY", "PERP_FUNDING_SPREAD", "PERP_DIRECTIONAL_FUNDING"
FAMILIES = (FAMILY_CARRY, FAMILY_SPREAD, FAMILY_DIRECTIONAL)
FAMILY_SPECS: Dict[str, Dict[str, Any]] = {
    FAMILY_CARRY: {"is_carry": True, "legs": "short the perp + long the SAME-venue Bybit spot (delta-neutral)", "universe": "only symbols with a confirmed Bybit spot pair",
                   "cost_position_class": "acquire_spot_perp", "risk_model": "basis residual + spot/perp margin; price legs cancel to first order; the basis move is UNMEASURED here (no spot series)",
                   "price_leg": "hedged: ~0, basis_residual UNMEASURED", "name_rule": "may be called carry"},
    FAMILY_SPREAD: {"is_carry": False, "legs": "short top-k / long bottom-k PERPS by trailing funding, dollar-neutral, perp only", "universe": "every active perp (no spot needed)",
                    "cost_position_class": "perp_only_unhedged", "risk_model": "residual factor / beta exposure of the long-short price book + idiosyncratic squeeze risk; funding is NOT the whole P&L",
                    "price_leg": "measured per leg from marks; beta/correlation to the equal-weight universe reported", "name_rule": "NEVER called carry"},
    FAMILY_DIRECTIONAL: {"is_carry": False, "legs": "short top-k perps by trailing funding, no hedge", "universe": "every active perp",
                         "cost_position_class": "perp_only_unhedged", "risk_model": "outright short-perp price exposure (beta to the market is ~ -1) plus funding; a squeeze can exceed funding by orders of magnitude",
                         "price_leg": "measured per leg from marks", "name_rule": "NEVER called carry"},
}


#: PRE-REGISTERED early-rejection rule (constants fixed here, not tuned to any result). A family whose out-of-sample NET mean is negative and significantly so cannot clear its costs; waiting for 50
#: independent observations before saying so only delays the owner's decision. It can only REJECT: promotion keeps its floor (SV.MIN_OBSERVATIONS) and every other gate. THREE independent conditions must all
#: hold: (1) at least 12 independent observations by the ESS floor, (2) a lower-tail p <= 0.01 from the ESS-adjusted t, and (3) the WIDEST of the bootstrap / ESS-t / batch-means 90% intervals
#: (RH.significance ``ci``) lies wholly below zero. The ESS is estimated from the same short series, so (2) alone is anti-conservative at n of 24-40 under serial dependence (the independent review measured
#: 3-6% false rejection at alpha 0.025 and ESS >= 8); with (1)-(3) the simulated false-rejection rate under a ZERO true edge is about 0.2-2.5% for n 24-60 and rho 0-0.6 (500 trials per cell, so +-0.7 pp),
#: per look: repeated re-runs on a growing capture accumulate. THAT FIGURE IS FOR AUTOREGRESSIVE-TYPE DEPENDENCE ONLY: the Round 14 audit measured 5.6% to 18% for a two-state regime (+-1 sd, n 24-100), 11.9% for a
#: slow regime hidden under noise (+-0.3 sd, mean dwell 100, n = 100) and 14% for a seasonal-masked level, because the ESS estimate cannot see persistence longer than the sample. The rule can only REJECT, so it never
#: authorises anything, but REJECTED is terminal: read it with the size of the loss, not the p alone. Power is deliberately modest: about 70% at a mean of -1 sd at n=24 (90% at n=40) and 27% at -0.5 sd; a borderline loser stays INSUFFICIENT_DATA.
REJECT_EARLY_MIN_INDEPENDENT = 12
REJECT_EARLY_ALPHA = 0.01


def early_rejection(st: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The early-rejection evidence from ``RH.significance`` output (computed WITH its ``ci``), or None. Fails closed: a missing, non-finite or malformed input is never a rejection."""
    try:
        mean, t, p1 = float(st.get("mean")), float(st.get("t")), float(st.get("p_one_sided"))
        ind = int(st.get("independent_observations") or 0)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(mean) and math.isfinite(t) and math.isfinite(p1)) or ind < REJECT_EARLY_MIN_INDEPENDENT or not (mean < 0):
        return None
    p_lower = 1.0 - p1
    if not (p_lower <= REJECT_EARLY_ALPHA):                                 # also False for NaN
        return None
    ci = st.get("ci")
    hi = ci.get("hi") if isinstance(ci, Mapping) and ci.get("status") == "OK" else None
    if not (isinstance(hi, (int, float)) and math.isfinite(float(hi)) and float(hi) < 0):
        return None                                                          # no usable second method, or it does not exclude zero: no claim
    return {"rule": (f">= {REJECT_EARLY_MIN_INDEPENDENT} independent OOS observations, negative mean, lower-tail p <= {REJECT_EARLY_ALPHA} (ESS-adjusted) AND the widest 90% interval wholly below zero; "
                     "per look, taker cost scenario only (the modelled maker scenario is never selected), reject-only: never promotes"),
            "independent_observations": ind, "mean_bps": mean, "t": t, "p_lower": p_lower, "alpha": REJECT_EARLY_ALPHA, "ci_hi_bps": float(hi)}


def rejection_bar(xs: Sequence[float], *, basis: str) -> Dict[str, Any]:
    """Would this series meet the repo's early-rejection standard (``early_rejection``)? Reported WITH the numbers so a point-estimate loss is never read as a statistical rejection. Applied here to
    IN-SAMPLE overlapping entries (the standard is defined for out-of-sample rows; the ESS adjustment handles the serial dependence, the in-sample caveat remains), so it describes how strong the
    evidence of a loss is, it does not turn the in-sample verdict into an out-of-sample one. Fails closed: too few points or an unusable interval is 'does not meet'."""
    vals = [float(x) for x in xs]
    out: Dict[str, Any] = {"basis": basis, "scope": "IN_SAMPLE_OVERLAPPING_ENTRIES", "n": len(vals), "meets_early_rejection_standard": False, "mean": None, "p_lower": None,
                           "independent_observations": 0, "widest_ci90": None,
                           "rule": (f">= {REJECT_EARLY_MIN_INDEPENDENT} independent observations (ESS floor), negative mean, lower-tail p <= {REJECT_EARLY_ALPHA} (ESS-adjusted) AND the widest 90% interval "
                                    "wholly below zero")}
    if len(vals) < 8:
        return out
    st = RH.significance(vals, ci=True)
    ci = st.get("ci") if isinstance(st.get("ci"), Mapping) and st["ci"].get("status") == "OK" else None
    p1 = st.get("p_one_sided")
    out.update({"mean": st.get("mean"), "independent_observations": int(st.get("independent_observations") or 0), "p_lower": (None if p1 is None else 1.0 - float(p1)),
                "widest_ci90": None if ci is None else [ci.get("lo"), ci.get("hi")], "meets_early_rejection_standard": early_rejection(st) is not None})
    return out


def family_costs(kind: str) -> Dict[str, Dict[str, Any]]:
    """Round-trip cost scenarios per family from carry_costs' authoritative names (fee venue_stated, spread ASSUMED, maker MODELLED). A scenario with no figure (spot maker) is absent."""
    if kind not in FAMILY_SPECS:
        raise RH.HarnessError(f"unknown strategy family {kind!r}")
    cls = FAMILY_SPECS[kind]["cost_position_class"]
    out: Dict[str, Dict[str, Any]] = {}
    for style, nm in (("taker", "taker"), ("maker", "maker_modelled")):
        c = CC.position_class_costs(cls, style)
        if c.get("total_bps") is not None:
            out[nm] = {"bps": float(c["total_bps"]), "fee_bps": float(c["fee_bps"]), "spread_bps": float(c["spread_bps"]),
                       "prov": "ASSUMED" if style == "taker" else "MODELLED",
                       "fee_prov": "VENUE_STATED" if style == "taker" else "MODELLED", "spread_prov": "ASSUMED" if style == "taker" else "MODELLED"}
    return out


def holding_grid(t_first_ms: int, t_last_ms: int, *, formation_days: int, hold_days: int) -> List[int]:
    """Decision times of the NON-OVERLAPPING holding grid: the first decision is the first UTC-day boundary at or after ``t_first + formation`` (the formation window needs its full
    history), then one every ``hold_days``; a row needs its whole holding window inside the data (``t + hold <= t_last``). Deterministic, depends on the data span only."""
    if hold_days < 1 or formation_days < 0:
        raise RH.HarnessError("hold_days must be >= 1 and formation_days >= 0")
    first = -(-(t_first_ms + formation_days * MS_DAY) // MS_DAY) * MS_DAY
    out, t = [], first
    while t + hold_days * MS_DAY <= t_last_ms:
        out.append(t)
        t += hold_days * MS_DAY
    return out


def grid_requirements(*, formation_days: int, hold_days: int, min_rows: int = 60, span_days: Optional[float] = None, warmup_days: int = WARMUP_DAYS) -> Dict[str, Any]:
    """Exactly what window yields ``min_rows`` independent holding rows, using backfill_plan's numbers: warmup + formation + min_rows x hold_days (7 + 30 + 60 x 7 = 457 days at the
    registered values), one row per ``hold_days`` whatever the number of symbols."""
    w = required_span(formation_days, hold_days, min_rows, warmup_days)
    need = w["needed_days"]
    out: Dict[str, Any] = {"formation_days": formation_days, "hold_days": hold_days, "warmup_days": warmup_days, "rows_required": min_rows, "holding_span_days_required": min_rows * hold_days,
                           "window_days_required": need, "components": w["components"],
                           "statement": f"{min_rows} non-overlapping {hold_days}-day holding rows need {need} days of funding + mark history = warmup {warmup_days} + formation {formation_days} + "
                                        f"{min_rows} x {hold_days} (backfill_plan.required_window); one row per {hold_days} days, whatever the number of symbols"}
    if span_days is not None:
        out["window_days_available"] = round(span_days, 3)
        out["rows_possible_by_calendar"] = max(0, int((span_days - formation_days - warmup_days) // hold_days))
        out["shortfall_days"] = round(max(0.0, need - span_days), 3)
    return out


def eligibility(s: SymbolSeries, t: int, kind: str, formation_ms: int, min_listing_age_days: int) -> Optional[str]:
    """None when the symbol may enter at ``t``, else the reason code. Uses only facts known at t: launch (past), delivery <= t (already gone), formation prints <= t, the entry mark
    knowable at t. Whether the contract is delivered LATER is never used to include or exclude it (that is a forced exit during the hold, priced or counted as unpriced)."""
    if min_listing_age_days > 0 and not s.launch_ms:
        return "launch_unknown"
    if s.launch_ms and t < s.launch_ms:
        return "not_launched"
    if s.delivery_ms is not None and t >= s.delivery_ms:
        return "delivered"
    if min_listing_age_days > 0 and t - s.launch_ms < min_listing_age_days * MS_DAY:
        return "listing_age"
    if kind == FAMILY_CARRY and s.spot_listed is not True:
        return "hedge_unknown" if s.spot_listed is None else "no_bybit_spot_pair"
    return None


def _leg_window(s: SymbolSeries, t: int, h_ms: int, max_staleness_ms: Optional[int]) -> Dict[str, Any]:
    end = t + h_ms if s.delivery_ms is None else min(t + h_ms, s.delivery_ms)
    bound = staleness_bound_ms(s, max_staleness_ms)
    terminal = s.delivery_ms is not None and s.delivery_ms <= t + h_ms
    m0 = price_at(s.mark, t, bound)
    basis = None
    if terminal:
        m1, basis = _terminal_price(s, max_staleness_ms)
    else:
        m1 = price_at(s.mark, end, bound)
    fund, n = _sum_range(s.funding, t, end)
    return {"m0": m0, "m1": m1, "end": end, "terminal": terminal, "terminal_basis": basis, "funding_raw_bps": fund, "prints": n}


def run_family_variant(series: Sequence[SymbolSeries], kind: str, *, slots: Sequence[int], formation_days: int, hold_days: int, top_k: int, round_trip_bps: float,
                       min_listing_age_days: int = 30, max_staleness_ms: Optional[int] = None, memo: Optional[Dict[Any, Any]] = None) -> Dict[str, Any]:
    """One variant of one family on the fixed ``slots`` grid. Rank on the FORMATION window ending at t (interval-normalised bps/day), hold (t, t+hold] with a delivery cap.
    Per leg: funding earned (short +) / paid (long -), price-leg return (short -ret, long +ret) from marks, round-trip cost on its notional. Row values are per unit of GROSS notional:
    SPREAD = 0.5 x mean(short legs) + 0.5 x mean(long legs); CARRY / DIRECTIONAL = mean(short legs). ``market_bps`` is the equal-weight price return of EVERY eligible perp over the same
    window (the factor the book is regressed on). A spread row needs 2 x top_k eligible ranked symbols and every leg priced, else it is skipped with a reason (never half-hedged)."""
    f_ms, h_ms = formation_days * MS_DAY, hold_days * MS_DAY
    is_spread = kind == FAMILY_SPREAD
    memo = memo if memo is not None else {}

    def fscore(s: SymbolSeries, t: int) -> Optional[float]:
        k = ("f", s.symbol, t, f_ms)
        if k not in memo:
            memo[k] = formation_score(s, t, f_ms)
        return memo[k]

    def window(s: SymbolSeries, t: int) -> Dict[str, Any]:
        k = ("w", s.symbol, t, h_ms, max_staleness_ms)
        if k not in memo:
            memo[k] = _leg_window(s, t, h_ms, max_staleness_ms)
        return memo[k]
    rows: List[Dict[str, Any]] = []
    skipped: Dict[str, int] = {}
    excl: Dict[str, int] = {}
    unpriced: List[Dict[str, Any]] = []
    for t in slots:
        scored = []
        for s in series:
            why = eligibility(s, t, kind, f_ms, min_listing_age_days)
            if why is None:
                sc = fscore(s, t)
                if sc is None:
                    why = "no_formation_prints"
                elif price_at(s.mark, t, staleness_bound_ms(s, max_staleness_ms)) in (None, 0.0):
                    why = "no_entry_mark"
                else:
                    scored.append((sc, s))
            if why is not None:
                excl[why] = excl.get(why, 0) + 1
        scored.sort(key=lambda x: (-x[0], x[1].symbol))
        need = 2 * top_k if is_spread else top_k
        if len(scored) < need:
            skipped["insufficient_eligible_symbols"] = skipped.get("insufficient_eligible_symbols", 0) + 1
            continue
        shorts = scored[:top_k]
        longs = scored[-top_k:] if is_spread else []
        legs: List[Dict[str, Any]] = []
        row_bad = None
        for side, group in (("Sell", shorts), ("Buy", longs)):
            for sc, s in group:
                w = window(s, t)
                if w["m0"] is None or w["m1"] is None or w["m0"] <= 0:
                    if w["terminal"]:
                        unpriced.append({"symbol": s.symbol, "t": t, "delivery_ms": s.delivery_ms})
                    row_bad = row_bad or f"leg_unpriced:{s.symbol}"
                    continue
                ret = (w["m1"] / w["m0"] - 1.0) * 1e4
                sign = 1.0 if side == "Sell" else -1.0
                price = -ret if side == "Sell" else ret
                fund = sign * w["funding_raw_bps"]
                if kind == FAMILY_CARRY:
                    price = 0.0                                   # hedged: the spot leg offsets the price leg to first order; the basis residual is UNMEASURED
                legs.append({"symbol": s.symbol, "side": side, "interval_min": s.interval_min, "hedgeability": hedgeability(s), "formation_bps_per_day": sc, "funding_bps": fund,
                             "prints": w["prints"], "price_bps": price, "cost_bps": round_trip_bps, "net_bps": fund + price - round_trip_bps, "closed_during_hold": w["terminal"],
                             "terminal_price_basis": w["terminal_basis"], "raw_price_ret_bps": ret})
        if row_bad and (is_spread or not legs):
            skipped[row_bad.split(":")[0]] = skipped.get(row_bad.split(":")[0], 0) + 1
            continue
        mk = []
        for s in series:
            if eligibility(s, t, FAMILY_SPREAD, f_ms, min_listing_age_days) is None:
                w = window(s, t)
                if w["m0"] and w["m1"] and w["m0"] > 0:
                    mk.append((w["m1"] / w["m0"] - 1.0) * 1e4)
        side_avg = lambda sd, key: statistics.fmean(l[key] for l in legs if l["side"] == sd)           # noqa: E731
        if is_spread:
            if not any(l["side"] == "Sell" for l in legs) or not any(l["side"] == "Buy" for l in legs):
                skipped["one_side_unpriced"] = skipped.get("one_side_unpriced", 0) + 1
                continue
            agg = {k: 0.5 * side_avg("Sell", k) + 0.5 * side_avg("Buy", k) for k in ("funding_bps", "price_bps", "cost_bps", "net_bps")}
            agg["short_price_bps"], agg["long_price_bps"] = side_avg("Sell", "price_bps"), side_avg("Buy", "price_bps")
            agg["short_funding_bps"], agg["long_funding_bps"] = side_avg("Sell", "funding_bps"), side_avg("Buy", "funding_bps")
        else:
            agg = {k: statistics.fmean(l[k] for l in legs) for k in ("funding_bps", "price_bps", "cost_bps", "net_bps")}
        rows.append({"t": t, "legs": legs, "market_bps": statistics.fmean(mk) if mk else None, **agg})
    return {"kind": kind, "rows": rows, "skipped_rows_by_reason": dict(sorted(skipped.items())), "eligibility_exclusions_by_reason": dict(sorted(excl.items())),
            "terminal_exit_unpriced_legs": unpriced, "time_mode": "REPLAY_OBSERVATION_TIME"}


def _ols(xs: Sequence[float], ys: Sequence[float]) -> Optional[Tuple[float, float, Optional[float]]]:
    """(beta, alpha, correlation) of y on x; None under 3 points or zero x-variance."""
    n = len(xs)
    if n < 3:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / sxx, my - sxy / sxx * mx, (sxy / math.sqrt(sxx * syy) if syy > 0 else None)


def book_analytics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Return decomposition and exposure of a book's per-row series: mean/sd/per-row Sharpe, max drawdown of the cumulative net, funding / price / cost means, beta and correlation of the
    PRICE component to the equal-weight market factor, and the residual (alpha) mean / sd after that beta. All in bps per row (one holding period)."""
    n = len(rows)
    out: Dict[str, Any] = {"n": n}
    if not n:
        return out
    net = [r["net_bps"] for r in rows]
    cum, peak, mdd = 0.0, 0.0, 0.0
    for x in net:
        cum += x
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    sd = statistics.stdev(net) if n > 1 else None
    out.update({"mean_net_bps": statistics.fmean(net), "sd_net_bps": sd, "per_row_sharpe": (statistics.fmean(net) / sd) if sd else None, "max_drawdown_bps": mdd,
                "worst_row_bps": min(net), "mean_funding_bps": statistics.fmean(r["funding_bps"] for r in rows), "mean_price_bps": statistics.fmean(r["price_bps"] for r in rows),
                "mean_cost_bps": statistics.fmean(r["cost_bps"] for r in rows), "funding_minus_cost_bps": statistics.fmean(r["funding_bps"] - r["cost_bps"] for r in rows)})
    pairs = [(r["market_bps"], r["price_bps"]) for r in rows if r.get("market_bps") is not None]
    ols = _ols([p[0] for p in pairs], [p[1] for p in pairs])
    if ols is None:
        out.update({"beta_to_market": None, "corr_to_market": None, "residual_price_mean_bps": None, "residual_price_sd_bps": None, "factor_note": "fewer than 3 rows or no market variance"})
    else:
        beta, alpha, corr = ols
        resid = [y - beta * x for x, y in pairs]
        out.update({"beta_to_market": beta, "corr_to_market": corr, "residual_price_mean_bps": statistics.fmean(resid), "residual_price_sd_bps": statistics.stdev(resid) if len(resid) > 1 else None,
                    "market_mean_bps": statistics.fmean(p[0] for p in pairs), "market_sd_bps": statistics.stdev(p[0] for p in pairs) if len(pairs) > 1 else None})
    if rows and "short_price_bps" in rows[0]:
        out.update({"mean_short_leg_price_bps": statistics.fmean(r["short_price_bps"] for r in rows), "mean_long_leg_price_bps": statistics.fmean(r["long_price_bps"] for r in rows),
                    "mean_short_leg_funding_bps": statistics.fmean(r["short_funding_bps"] for r in rows), "mean_long_leg_funding_bps": statistics.fmean(r["long_funding_bps"] for r in rows)})
    return out


def _vs_cash_descriptive(basis: Any, is_carry: bool, variants: Mapping[str, Mapping[int, Mapping[str, Any]]], common: Sequence[int], hold_days: int) -> Dict[str, Any]:
    """IN-SAMPLE, descriptive only (too few rows for a test): per variant the mean net per row, the cash it displaces on the committed capital and the mean excess, with the hurdle a row's gross return must clear.
    Nothing is recorded in the multiple-testing ledger and nothing here is a verdict."""
    capital = CB.capital_per_gross_notional(is_carry)
    per: Dict[str, Any] = {}
    for name, rows in variants.items():
        ex, unknown = CB.excess_rows([rows[t] for t in common], hold_days=hold_days, capital=capital, basis=basis)
        if ex:
            vals = list(ex.values())
            per[name] = {"rows": len(vals), "rows_without_cash": len(unknown), "mean_net_bps_of_gross": statistics.fmean(v["net_bps_of_gross"] for v in vals),
                         "mean_cash_bps_of_capital": statistics.fmean(v["cash_bps_of_capital"] for v in vals), "mean_cash_hurdle_bps_of_gross": statistics.fmean(v["cash_hurdle_bps_of_gross"] for v in vals),
                         "mean_excess_bps_of_capital": statistics.fmean(v["excess_bps_of_capital"] for v in vals)}
        else:
            per[name] = {"rows": 0, "rows_without_cash": len(unknown), "note": "the cash source covers none of these rows: UNKNOWN, never priced at zero"}
    return {"label": "IN_SAMPLE_DESCRIPTIVE_NOT_A_TEST", "basis": basis.describe(), "capital_per_gross_notional": capital, "variants": per}


def _vs_cash_walk_forward(basis: Any, *, kind: str, is_carry: bool, selectable: Sequence[str], by_t: Callable[[str], Mapping[int, Mapping[str, Any]]], common_sorted: Sequence[int], hold_days: int, ledger: Any,
                          params_of: Mapping[str, Mapping[str, Any]], alpha: float, min_rows: int) -> Dict[str, Any]:
    """The family's walk-forward on the EXCESS over ``basis`` on the capital committed. Rows the cash source cannot price (a hole, a window before its series begins) are dropped from EVERY variant together and
    counted; the cash-adjusted variants are recorded in the family's persistent ledger under their own names (``...|X<source>``: a new trial each, an identical re-run the same trial), so the Holm correction
    below counts them. Reject-only: ``verdict`` is REJECTED or INSUFFICIENT_DATA, never a pass."""
    capital = CB.capital_per_gross_notional(is_carry)
    per_variant: Dict[str, Dict[int, Dict[str, float]]] = {}
    unknown_ts: set = set()
    for name in selectable:
        ex, unknown = CB.excess_rows([by_t(name)[t] for t in common_sorted], hold_days=hold_days, capital=capital, basis=basis)
        per_variant[name] = ex
        unknown_ts |= set(unknown)
    ts = [t for t in common_sorted if t not in unknown_ts]
    blk: Dict[str, Any] = {"basis": basis.describe(), "capital_per_gross_notional": capital, "statistic": "excess_bps_of_capital = net_bps_of_gross_notional / capital - cash_bps_over_the_hold",
                           "rows_aligned": len(common_sorted), "rows_without_cash": len(unknown_ts), "rows": len(ts)}
    if len(ts) < min_rows:
        blk.update({"status": "TOO_FEW_ROWS_WITH_CASH", "verdict": "INSUFFICIENT_DATA", "reason": f"{len(ts)} rows have cash coverage; need >= {min_rows}"})
        return blk
    series_x = {f"{n}|X{basis.label}": [per_variant[n][t]["excess_bps_of_capital"] for t in ts] for n in selectable}
    for xn, rets in series_x.items():
        base = xn.rsplit("|X", 1)[0]
        if RH.series_digest(rets) not in ledger.returns_digests(xn):
            ledger.record(xn, {**params_of[base], "statistic": "EXCESS_OVER_CASH_ON_CAPITAL", "cash_basis": basis.label, "capital_per_gross_notional": capital}, rets, horizon=1)
    folds = RH.walk_forward_folds(len(ts), train_min=max(20, len(ts) // 2), test_len=max(5, len(ts) // 10), label_horizon=1, embargo=1)
    oos: List[float] = []
    picks: List[str] = []
    idx: List[Tuple[str, int]] = []
    for f in folds:
        pick = RH.train_only_best({k: [v[i] for i in f.train] for k, v in series_x.items()})
        picks.append(pick)
        oos.extend(series_x[pick][i] for i in f.test)
        idx.extend((pick, i) for i in f.test)
    st = RH.significance(oos)
    m = ledger.n_variants
    adj = SP.adjust_pvalues([st["p_one_sided"]], "holm", alpha=alpha, n_tested=m)["adjusted"][0]
    mean = st["mean"]
    enough = st["independent_observations"] >= SV.MIN_OBSERVATIONS["oos_test"]
    verdict = "REJECTED" if enough and (mean is None or mean <= 0 or adj > alpha) else "INSUFFICIENT_DATA"
    early = early_rejection(st)
    if verdict == "INSUFFICIENT_DATA" and early:
        verdict = "REJECTED"
    chosen = [per_variant[p.rsplit("|X", 1)[0]][ts[i]] for p, i in idx]
    blk.update({"status": "WALK_FORWARD_DONE", "verdict": verdict, "n_variants_counted": m, "correction": "holm", "oos_rows": len(oos), "oos_mean_excess_bps_of_capital_per_row": mean,
                "oos_independent_observations": st["independent_observations"], "p_one_sided": st["p_one_sided"], "p_adjusted": adj, "selected_per_fold": picks,
                "reject_early": early if verdict == "REJECTED" and not enough else None,
                "oos_mean_net_bps_of_gross_per_row": statistics.fmean(c["net_bps_of_gross"] for c in chosen) if chosen else None,
                "oos_mean_cash_hurdle_bps_of_gross_per_row": statistics.fmean(c["cash_hurdle_bps_of_gross"] for c in chosen) if chosen else None,
                "label": "OUT_OF_SAMPLE_WITHIN_THE_CAPTURE_ONLY; REJECT-ONLY: a negative or insignificant excess over cash rejects, a positive one is never a promotion"})
    return blk


def evaluate_family(series: Sequence[SymbolSeries], kind: str, *, ledger_path: Optional[str], universe_info: Optional[Mapping[str, Any]] = None,
                    formation_days_grid: Sequence[int] = (7, 14, 30), hold_days: int = 7, top_k_grid: Sequence[int] = (1, 3), min_rows: int = 60, alpha: float = 0.05,
                    min_listing_age_days: int = 30, max_staleness_ms: Optional[int] = None, allow_unverified_key_basis_for_tests: bool = False,
                    cash_bases: Sequence[Any] = ()) -> Dict[str, Any]:
    """Walk-forward of one strategy family on the fixed non-overlapping grid. Every (formation x top_k x cost scenario) variant is recorded in the family's PERSISTENT ledger
    (``ledger_path`` is required: a fresh process cannot reset the count) and is a member of the multiple-testing family. Variants are aligned on decision time: only slots where
    every variant produced a row are used, so the row count follows from the data. The verdict is capped at INSUFFICIENT_DATA; REJECTED when an adequately sized
    out-of-sample is non-positive or not significant after Holm over the whole family. Perp-only families are never labelled carry."""
    if kind not in FAMILY_SPECS:
        raise RH.HarnessError(f"unknown strategy family {kind!r}")
    if not isinstance(ledger_path, str) or not ledger_path:
        raise RH.HarnessError("LEDGER_PATH_REQUIRED: the family count must survive a restart")
    unkeyed = sorted(s.symbol for s in series if s.key_basis is not _KEY_BASIS_OBSERVED)
    if unkeyed and allow_unverified_key_basis_for_tests is not True:
        raise RH.HarnessError(f"{len(unkeyed)} series lack the loader's key_basis token: marks/funding keyed by candle start would leak the future; first: {unkeyed[:3]}")
    spec = FAMILY_SPECS[kind]
    survivorship = _survivorship_of(universe_info)
    warnings: List[str] = []
    if survivorship == SURVIVORS_ONLY:
        warnings.append("SURVIVORS_ONLY: no survivorship-free claim; the result is a survivorship-biased evaluation")
    if unkeyed:
        warnings.append("KEY_BASIS_UNVERIFIED (test-only path): capped at INSUFFICIENT_DATA")
    costs = family_costs(kind)
    out: Dict[str, Any] = {"schema": SCHEMA + "/family", "family": kind, "family_spec": spec, "is_carry": spec["is_carry"], "name_rule": spec["name_rule"], "survivorship": survivorship,
                           "survivorship_free_claim": survivorship == DELISTINGS_OBSERVED, "warnings": warnings, "ceiling": "INSUFFICIENT_DATA", "cost_scenarios": costs,
                           "cost_basis": "carry_costs authoritative names: fee VENUE_STATED (base tier), spread ASSUMED 0.4, maker MODELLED", "symbols": len(series),
                           "hedgeability_basis": HEDGE_BASIS, "multiple_testing_scope": "PERSISTENT_LEDGER_FILE", "ledger_path": ledger_path and os.path.abspath(ledger_path),
                           "time_mode": "REPLAY_OBSERVATION_TIME", "selection": "formation-window information only; never realised future funding",
                           "dataset_state": universe_info.get("dataset_state") if _verified_verdict(universe_info) is not None else None,
                           "loaded_identity": universe_info.get("loaded_identity") if _verified_verdict(universe_info) is not None else None}
    if any(s.interval_min is None for s in series):
        out.update({"verdict": "INSUFFICIENT_DATA", "status": "FUNDING_NORMALISATION_UNKNOWN", "reason": "a symbol carries no fundingInterval"})
        return out
    if not series:
        out.update({"verdict": "UNTESTED", "status": "NO_SERIES", "reason": "no series given"})
        return out
    if spec["is_carry"] and all(s.spot_listed is None for s in series):
        # The carry family's universe is "symbols with a confirmed Bybit spot pair". With no hedgeability fact for ANY symbol (no --spot-dir, or a snapshot none of whose pairs
        # resolved) that universe is empty, so nothing could have been tried: counting six rejected variants would only make the later real run harder to pass.
        out.update({"verdict": "UNTESTED", "status": "HEDGEABILITY_UNKNOWN_FOR_EVERY_SYMBOL",
                    "reason": "no symbol has a hedgeability fact: no spot snapshot was supplied (--spot-dir) or none of its pairs resolved, so the carry universe is empty and nothing was tried "
                              "(nothing was written to the multiple-testing ledger)"})
        return out
    t0 = min(min((ts for ts, _ in s.funding), default=10 ** 18) for s in series)
    t1 = max(max((ts for ts, _ in s.funding), default=0) for s in series)
    fmax = max(formation_days_grid)
    slots = holding_grid(t0, t1, formation_days=fmax, hold_days=hold_days)
    out["grid"] = {**grid_requirements(formation_days=fmax, hold_days=hold_days, min_rows=min_rows, span_days=(t1 - t0) / MS_DAY), "slots": len(slots),
                   "first_decision_ms": slots[0] if slots else None, "last_decision_ms": slots[-1] if slots else None}
    fam = RH.normalise_family(f"cross_sectional_funding.{kind.lower()}")
    ledger = RH.VariantLedger(fam, path=ledger_path)
    results: Dict[str, Dict[str, Any]] = {}
    params_of: Dict[str, Dict[str, Any]] = {}
    memo: Dict[Any, Any] = {}
    for F in formation_days_grid:
        for k in top_k_grid:
            for cname, c in costs.items():
                name = f"{kind}|F{F}|H{hold_days}|K{k}|C{cname}"
                params_of[name] = {"kind": kind, "formation_days": F, "hold_days": hold_days, "top_k": k, "round_trip_bps": c["bps"], "min_listing_age_days": min_listing_age_days}
                results[name] = run_family_variant(series, kind, slots=slots, formation_days=F, hold_days=hold_days, top_k=k, round_trip_bps=c["bps"],
                                                   min_listing_age_days=min_listing_age_days, max_staleness_ms=max_staleness_ms, memo=memo)
    # FAIL CLOSED: only a variant whose cost provenance is KNOWN and not MODELLED can be selected. A MODELLED cost (maker: 100% fill, zero adverse selection, never measured) is counted in the
    # multiple-testing family but never selected and never shown as the headline: train_only_best picks the cheapest variant on identical funding, so its modelled advantage would drive the figure.
    def _prov_of(name: str) -> Optional[str]:
        return (costs.get(name.rsplit("|C", 1)[-1]) or {}).get("prov")
    unselectable = sorted(n for n in results if _prov_of(n) in (None, "MODELLED"))
    out["selection_excludes_modelled_cost_variants"] = unselectable
    common = set(slots)
    for r in results.values():
        common &= {x["t"] for x in r["rows"]}
    common_sorted = sorted(common)
    n_rows = len(common_sorted)
    unpriced = {(x["symbol"], x["t"]) for r in results.values() for x in r["terminal_exit_unpriced_legs"]}
    out.update({"variants_tried": sorted(results), "rows_per_variant": {k: len(v["rows"]) for k, v in sorted(results.items())}, "independent_holding_rows": n_rows,
                "terminal_exit_unpriced": len(unpriced), "terminal_exit_unpriced_symbols": sorted({a for a, _ in unpriced})[:NAMES_CAP],
                "skipped_rows_by_reason": {k: v["skipped_rows_by_reason"] for k, v in sorted(results.items())},
                "eligibility_exclusions_by_reason": next(iter(results.values()))["eligibility_exclusions_by_reason"] if results else {}})
    if unpriced:
        warnings.append(f"TERMINAL_EXIT_UNPRICED: {len(unpriced)} delisted legs have no price at delivery: capped at INSUFFICIENT_DATA")

    def by_t(name: str) -> Dict[int, Mapping[str, Any]]:
        return {x["t"]: x for x in results[name]["rows"]}
    if n_rows < min_rows:
        for name in sorted(results):
            if not ledger.knows(name):                      # an identical re-run is the same trial, not a new one
                ledger.record_rejected(name, params_of[name], f"only {n_rows} non-overlapping {hold_days}-day rows (< {min_rows})")
        out.update({"verdict": "INSUFFICIENT_DATA", "status": "TOO_FEW_HOLDING_ROWS", "family_ledger": fam, "n_variants_counted": ledger.n_variants,
                    "reason": f"{n_rows} aligned {hold_days}-day holding rows; need >= {min_rows}. " + out["grid"]["statement"]
                              + f" This capture spans {out['grid']['window_days_available']} days (shortfall {out['grid']['shortfall_days']} days)",
                    "descriptive_only": {"label": "IN_SAMPLE_DESCRIPTIVE_NOT_A_TEST",
                                         "book_analytics": {n: book_analytics([by_t(n)[t] for t in common_sorted]) for n in sorted(results) if n not in unselectable} if n_rows else {},
                                         "book_analytics_modelled_scenarios": {n: book_analytics([by_t(n)[t] for t in common_sorted]) for n in unselectable} if n_rows else {},
                                         "modelled_scenarios_note": "MODELLED-cost scenarios (never measured): shown apart, never the headline and never selectable"}})
        if cash_bases and n_rows:
            out["descriptive_only"]["vs_cash_in_sample"] = {b.label: _vs_cash_descriptive(b, spec["is_carry"], {n: by_t(n) for n in results if n not in unselectable}, common_sorted, hold_days) for b in cash_bases}
        return out
    series_by = {n: [by_t(n)[t]["net_bps"] for t in common_sorted] for n in sorted(results)}
    for name, rets in series_by.items():
        if RH.series_digest(rets) not in ledger.returns_digests(name):          # same variant on the very same series = the same trial; new data or a new name counts
            ledger.record(name, params_of[name], rets, horizon=1)
    # A MODELLED-cost variant (maker: 100% fill, zero adverse selection, never measured) is counted in the family but is NEVER selectable: train_only_best picks the highest training mean, which on
    # identical funding is always the cheapest variant, so its modelled 7 bps would drive the out-of-sample figure the competition ranks.
    selectable = {k: v for k, v in series_by.items() if k not in unselectable}
    if not selectable:
        out.update({"status": "NO_SELECTABLE_VARIANT", "verdict": "INSUFFICIENT_DATA", "family_ledger": fam, "n_variants_counted": ledger.n_variants,
                    "reason": "every variant carries a MODELLED cost: none can be selected as a result"})
        return out
    folds = RH.walk_forward_folds(n_rows, train_min=max(20, n_rows // 2), test_len=max(5, n_rows // 10), label_horizon=1, embargo=1)
    oos: List[float] = []
    chosen: List[str] = []
    test_idx: List[int] = []
    for f in folds:
        pick = RH.train_only_best({k: [v[i] for i in f.train] for k, v in selectable.items()})
        chosen.append(pick)
        oos.extend(series_by[pick][i] for i in f.test)
        test_idx.extend((pick, i) for i in f.test)                                           # type: ignore[arg-type]
    st = RH.significance(oos)
    m = ledger.n_variants
    m_nominal = m                                                      # what the NOMINAL p-value below is adjusted for (reported as n_variants_used_for_nominal_p)
    adj = SP.adjust_pvalues([st["p_one_sided"]], "holm", alpha=alpha, n_tested=m)["adjusted"][0]
    mean = st["mean"]
    enough = st["independent_observations"] >= SV.MIN_OBSERVATIONS["oos_test"]
    verdict = "REJECTED" if enough and (mean is None or mean <= 0 or adj > alpha) else "INSUFFICIENT_DATA"
    early = early_rejection(st)
    early_applied = False
    if verdict == "INSUFFICIENT_DATA" and early:
        verdict, early_applied = "REJECTED", True         # reject-only: a significantly negative net cannot clear its costs, whatever the sample size
    if unpriced or unkeyed:
        verdict, early_applied = "INSUFFICIENT_DATA", False   # unknown terminal losses / unverified timing: neither a pass nor a rejection can be claimed
    oos_rows = [by_t(pick)[common_sorted[i]] for pick, i in test_idx]
    if cash_bases:
        # The statistic a decision needs is the EXCESS over cash on the capital committed (cash_basis). Each cash source is its own, separately COUNTED set of variants (a new trial per variant and source); a
        # result against one never stands in for another. REJECT-ONLY: a family whose excess over cash is significantly non-positive is REJECTED; nothing here can promote one.
        out["vs_cash"] = {}
        for basis in cash_bases:
            blk = _vs_cash_walk_forward(basis, kind=kind, is_carry=spec["is_carry"], selectable=sorted(selectable), by_t=by_t, common_sorted=common_sorted, hold_days=hold_days, ledger=ledger, params_of=params_of,
                                        alpha=alpha, min_rows=min_rows)
            out["vs_cash"][basis.label] = blk
        m = ledger.n_variants                                          # the family grew by the cash-adjusted variants: reported (and used by their own Holm); THIS run's nominal p-value used n_variants_used_for_nominal_p.
        #                                                                A LATER nominal run on the same persistent ledger starts from the larger count (the ledger remembers every trial): conservative, never a promotion.
        rejecting = sorted(lbl for lbl, b in out["vs_cash"].items() if b.get("verdict") == "REJECTED")
        if rejecting and verdict != "REJECTED" and not (unpriced or unkeyed):
            verdict = "REJECTED"
            out["rejected_vs_cash"] = rejecting
    out["reject_early"] = early if early_applied else None
    out["reject_early_suppressed_by_cap"] = bool(early and not early_applied and verdict != "REJECTED")
    out.update({"status": "WALK_FORWARD_DONE", "verdict": verdict, "family_ledger": fam, "n_variants_counted": m, "n_variants_used_for_nominal_p": m_nominal, "n_variants_tried_in_call": len(results), "correction": "holm",
                "oos_rows": len(oos), "oos_mean_total_bps_per_row": mean, "oos_independent_observations": st["independent_observations"], "p_one_sided": st["p_one_sided"],
                "p_adjusted": adj, "selected_per_fold": chosen, "oos_book_analytics": book_analytics(oos_rows),
                "label": "OUT_OF_SAMPLE_WITHIN_THE_CAPTURE_ONLY: one regime, one capture; not prospective",
                "reason": (("carry: funding - spot+perp round trip (basis residual UNMEASURED)" if kind == FAMILY_CARRY else
                           "perp-only book: funding earned/paid + measured price legs - perp round trips; NOT carry")
                          + ((" | REJECTED_EARLY: " + early["rule"]) if early_applied else "")
                          + ((" | REJECTED_VS_CASH: " + ",".join(out["rejected_vs_cash"])) if out.get("rejected_vs_cash") else ""))})
    return out


# ---------------------------------------------------------------- the cross-sectional path: explicit universe, verified data, reproducible report
def _require_universe_dir(universe_dir: Any) -> str:
    if not isinstance(universe_dir, str) or not universe_dir.strip():
        raise RH.HarnessError("UNIVERSE_DIR_REQUIRED: the cross-sectional path needs an explicit verified universe_capture directory (--universe-dir); "
                              "without it survivorship, lifecycle and funding intervals cannot be established and nothing is evaluated")
    return universe_dir


def ledger_dir_candidates(data_root: Optional[str], ledger_dir: Optional[str] = None) -> List[str]:
    """Every directory the ledger-directory rule can resolve to, in precedence order, deduplicated and absolute: ``ledger_dir``, $CANDIDATE_LEDGER_DIR, ``<parent of data_root>/candidate_ledgers``
    (only when a data root is given) and ``~/.cache/ai-agent-control/candidate_ledgers``. The FIRST is the rule's answer (``ledger_dir_for``); the whole list is where an evaluation of this data
    could have left its ledgers, which is what the holdout reservation must look in (a run made with another ``--ledger-dir``, or with the variable set, is otherwise invisible to it)."""
    out: List[str] = []
    if ledger_dir:
        out.append(os.path.abspath(ledger_dir))
    env = os.environ.get("CANDIDATE_LEDGER_DIR")
    if env:
        out.append(os.path.abspath(env))
    if data_root:
        out.append(os.path.join(os.path.dirname(os.path.realpath(data_root)), "candidate_ledgers"))
    out.append(os.path.join(os.path.expanduser("~"), ".cache", "ai-agent-control", "candidate_ledgers"))
    return list(dict.fromkeys(out))


def ledger_dir_for(data_root: Optional[str], ledger_dir: Optional[str] = None) -> str:
    """THE rule for where a research ledger directory is (pure: nothing is created): ``ledger_dir``, else $CANDIDATE_LEDGER_DIR, else ``<parent of data_root>/candidate_ledgers``, else
    ``~/.cache/ai-agent-control/candidate_ledgers`` (``ledger_dir_candidates``' first entry). Every research CLI resolves its ledgers through this one function: two tools that each re-derive the
    rule drift apart, and a family counted in two directories is two families (the multiple-testing count then understates the trials). ``--ledger FILE`` on candidate_readiness.py names one file
    and bypasses this rule on purpose. The reserved-holdout registry does NOT live here: it is per user (``history_holdout.default_registry_path``), so no ledger directory can give a read a clean slate."""
    return ledger_dir_candidates(data_root, ledger_dir)[0]


def default_ledger_path(family: str, data_root: str, ledger_dir: Optional[str] = None) -> str:
    """The persistent multiple-testing ledger of ``family``: ``ledger_dir_for(...)`` / ``<family>.jsonl``.
    It is deliberately OUTSIDE the data root (evidence directories stay byte-for-byte what the verifier hashed) and never under bot/state."""
    path = os.path.join(ledger_dir_for(data_root, ledger_dir), RH.normalise_family(family) + ".jsonl")
    if os.path.realpath(path).startswith(os.path.join(BOT, "state") + os.sep):
        raise RH.HarnessError("the ledger may not live under bot/state")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def evaluate_cross_sectional(history_root: str, *, universe_dir: Optional[str], ledger_path: Optional[str], holdout_ledger: Any, spot_dir: Optional[str] = None,
                             mark_interval: Optional[str] = None, spec: Optional[Mapping[str, Any]] = None, unreserved_exploratory: bool = False, reader: str = "",
                             cash_bases: Sequence[Any] = (), **kw: Any) -> Dict[str, Any]:
    """Load a market_history root against its verified universe capture and run the ranked-funding walk-forward. The universe dir is REQUIRED (HarnessError UNIVERSE_DIR_REQUIRED) and
    so is a persistent ``ledger_path``: a fresh process must not reset the multiple-testing family count (HarnessError otherwise).

    ``holdout_ledger`` is REQUIRED: the per-user reserved-holdout registry (``history_holdout.default_registry_path()``), or ``history_holdout.UNGUARDED`` to read the whole window on
    purpose (tests, embeddings; stamped NOT_GUARDED). With a registry the load is CUT at the reserved boundary and refused (HOLDOUT_NOT_RESERVED, with the exact command) when nothing is
    reserved, unless ``unreserved_exploratory`` (stamped NOT_A_HOLDOUT_STUDY, recorded, and the time it read is burned). Whatever the stamp, an evaluation that was not run on a reserved
    explore window cannot reach PROMISING (``readiness_levels``)."""
    _require_universe_dir(universe_dir)
    if not isinstance(ledger_path, str) or not ledger_path:
        raise RH.HarnessError("LEDGER_PATH_REQUIRED: a cross-sectional evaluation records its variants in a persistent ledger so the family count survives a restart")
    import history_holdout as HH            # lazy: history_holdout imports this module
    series, info = HH.load(history_root, universe_dir=universe_dir, spot_dir=spot_dir, mark_interval=mark_interval, ledger_path=holdout_ledger,
                           reader=reader or "candidate_readiness.evaluate_cross_sectional", unreserved_exploratory=unreserved_exploratory)
    out = {"input": info, **evaluate_ranked_funding(series, universe_info=info, ledger_path=ledger_path, spec=spec, **kw)}
    hw = info.get("history_window") or {}
    if out.get("evidence_class") == "SHORT_DIAGNOSTIC_NOT_STRATEGY_EVIDENCE" and isinstance(out.get("required_span"), dict):
        out["history_shortfall"] = {"required_days": out["required_span"].get("window_days_required"), "loaded_history_window_days": hw.get("days"),
                                    "note": "this run is a SHORT DIAGNOSTIC: it must never be quoted as evidence for or against the strategy"}
    stem = os.path.splitext(ledger_path)[0]
    cash_bases = tuple(cash_bases or ())
    out["families"] = {k: evaluate_family(series, k, ledger_path=f"{stem}.{k.lower()}.jsonl", universe_info=info, cash_bases=cash_bases) for k in FAMILIES}
    out["cash_bases"] = [b.describe() for b in cash_bases] if cash_bases else []
    hstatus = (info.get("holdout") or {}).get("status")
    out["holdout_status"] = hstatus
    for f in out["families"].values():
        f["holdout_status"] = hstatus                                   # read by readiness_levels: an exploratory evaluation cannot reach PROMISING
    return out


#: what each data kind a cross-sectional study would ALSO like and this repository cannot verify
CROSS_SECTIONAL_WISHLIST = ("index-kline (basis/hedge price)", "open-interest (capacity)", "orderbook depth/spread (capacity and slippage; no capture schema exists)",
                            "spot price history (hedge-leg basis)", "borrow/financing (spot-perp carry)")
PIT_LIMITATIONS = (
    "RAW_RESPONSES_VERIFIED means self-consistent bytes as captured by the owner (hashes, cursor chains and re-derived coverage agree); it is NOT authenticated as produced by the exchange",
    "spot hedgeability is CURRENT_SNAPSHOT_ONLY: one spot-instruments snapshot labels legs and is never projected backwards",
    "instrument rule fields (tick/step/min notional/launch/delivery/funding interval) are as of the capture, not a history of rule changes",
    "marks and funding are read in REPLAY_OBSERVATION_TIME: per-row local receipt time is not stored, so a live decision would additionally need local_receive_time <= decision",
)


def dataset_report(*, data_root: Optional[str], universe_dir: Optional[str], spot_dir: Optional[str] = None, mark_interval: Optional[str] = None) -> Dict[str, Any]:
    """Everything a reader needs to judge a cross-sectional dataset, REPRODUCIBLE from the manifest files under ``data_root`` + ``universe_dir`` (no repo declaration is read):
    symbols included / excluded + why, source manifests (with digests and whether each re-verifies), coverage dates, funding intervals, incomplete datasets, missing fields,
    status-partition coverage, PIT limitations, the readiness levels and the highest supportable verdict. The universe dir is required: without it ``status`` is UNIVERSE_DIR_REQUIRED
    and nothing is included. A dataset that fails verification yields ``status`` REFUSED and no symbols. Offline, read-only, deterministic (no clock)."""
    scan = scan_data_root(data_root, universe_dirs=[universe_dir] if universe_dir else ())
    inv = verified_inventory(scan) if (scan["checked"] or scan["universe_capture_dirs"]) else {"datasets": [], "universes": [], "spot_snapshots": []}
    manifests = ([{"kind": f"market_history:{d['dataset']}" + (f"-{d['interval']}" if d.get("interval") else ""), "path": d["rel"], "digest": d["digest"], "dataset_status": d["dataset_status"],
                   "verified": d["verified"], "problems": d["problems"]} for d in inv["datasets"]]
                 + [{"kind": "spot_snapshot", "path": q["rel"], "digest": q["digest"], "dataset_status": q["dataset_status"], "verified": q["verified"], "problems": q["problems"]}
                    for q in inv.get("spot_snapshots", [])]
                 + [{"kind": "universe_capture", "path": u["rel"], "digest": u["digest"], "dataset_status": u["dataset_status"], "verified": u["verified"], "problems": u["problems"],
                     "explicit": u["explicit"]} for u in inv["universes"]])
    uni_good = [u for u in inv["universes"] if u["verified"] and u["explicit"]]
    surv = survivorship_of_universes({"universes": uni_good})
    rep: Dict[str, Any] = {"schema": SCHEMA + "/dataset_report", "status": "OK", "reason": "", "universe_dir": {"supplied": scan["universe_dir_supplied"], "rejected": scan["universe_dirs_rejected"]},
                           "source_manifests": manifests, "symbols_included": [], "symbols_excluded": [], "non_complete_symbols": {}, "coverage": {}, "intervals_min": {},
                           "incomplete_datasets": [{"dataset": m["kind"], "dataset_status": m["dataset_status"], "verified": m["verified"]} for m in manifests
                                                   if not m["verified"] or m["dataset_status"] != "COMPLETE"],
                           "missing_fields": [], "status_partition_coverage": {u["rel"]: {"delisting_observability": u["delisting_observability"], "status_coverage": u["status_coverage"],
                                                                                           "statuses_in_capture": u["statuses_in_capture"]} for u in inv["universes"]},
                           "survivorship": surv, "survivorship_free_claim": False, "pit_limitations": list(PIT_LIMITATIONS), "warnings": []}
    info: Optional[Dict[str, Any]] = None
    have = {d["dataset"] for d in inv["datasets"]}
    for need, label in (("funding", "funding"), ("mark-kline", "mark-kline"), ("index-kline", "index-kline (basis/hedge price)"), ("open-interest", "open-interest (capacity)")):
        if need not in have:
            rep["missing_fields"].append(f"{label}: no manifest under the data root")
    rep["missing_fields"] += [x for x in CROSS_SECTIONAL_WISHLIST if x.startswith(("orderbook", "spot price", "borrow"))]
    if spot_dir is None:
        rep["missing_fields"].append("spot-instruments snapshot (hedgeability): not supplied, so every symbol is HEDGE_UNKNOWN")
    if not scan["universe_dir_supplied"]:
        rep.update({"status": "UNIVERSE_DIR_REQUIRED", "reason": "UNIVERSE_DIR_REQUIRED: no (valid) --universe-dir was supplied; the cross-sectional path refuses to include any symbol without it"})
    elif not uni_good:
        rep.update({"status": "REFUSED", "reason": "the supplied universe capture does not verify: " + "; ".join(p for u in inv["universes"] for p in u["problems"])[:240]})
    elif any(not d["verified"] for d in inv["datasets"]) or any(not q["verified"] for q in inv.get("spot_snapshots", [])):
        rep.update({"status": "REFUSED", "reason": "a dataset under the data root does not re-verify: "
                    + "; ".join(p for d in list(inv["datasets"]) + list(inv.get("spot_snapshots", [])) for p in d["problems"])[:240]})
    elif not scan["checked"] or not scan["market_history"]:
        rep.update({"status": "REFUSED", "reason": "no market_history datasets under the data root"})
    else:
        try:
            series, info = load_history_root(data_root, universe_dir=universe_dir, spot_dir=spot_dir, mark_interval=mark_interval)          # METADATA ONLY: symbol lists and coverage, never a return series
        except RH.HarnessError as exc:
            rep.update({"status": "REFUSED", "reason": str(exc)[:300]})
        else:
            rep["symbols_included"] = sorted(x.symbol for x in series)
            urecs, _ui = MH.load_universe_dir(universe_dir)
            have_sym = {x.symbol for x in series} | {x["symbol"] for x in info["excluded"]}
            not_collected = [{"symbol": sy, "reason": "in_universe_not_in_any_history_dataset", "universe_status": str(r.get("status"))}
                             for sy, r in sorted(urecs.items()) if sy not in have_sym]
            rep["symbols_excluded"] = sorted(list(info["excluded"]) + not_collected, key=lambda x: x["symbol"])
            rep["universe_symbols"] = len(urecs)
            rep["non_complete_symbols"] = info["non_complete_symbols"]
            rep["survivorship"] = surv = info["survivorship"]            # the EVALUATED series' survivorship (Closed contracts absent from the series downgrade it), not the capture's
            rep["warnings"] = list(info["warnings"])
            rep["closed_in_universe_not_in_series"] = info["closed_in_universe_not_in_series"]
            for x in series:
                rep["intervals_min"][str(x.interval_min)] = rep["intervals_min"].get(str(x.interval_min), 0) + 1
            for name, d in sorted(info["datasets"].items()):
                src = next((q for q in inv["datasets"] if os.path.basename(q["dir"]) == name), None)
                if src and src["first_ts"] is not None:
                    rep["coverage"][name] = {"first_ts": src["first_ts"], "last_ts": src["last_ts"], "first_iso": _iso(src["first_ts"]), "last_iso": _iso(src["last_ts"]),
                                             "window_days": round((src["last_ts"] - src["first_ts"]) / MS_DAY, 3), "dataset_status": d["dataset_status"]}
            if "funding" in rep["coverage"]:
                rep["coverage"]["window"] = rep["coverage"]["funding"]
            if info.get("hedge"):
                rep["hedge_counts"] = info["hedge"]["counts"]
            else:
                rep["warnings"].append("no spot snapshot: hedgeability is HEDGE_UNKNOWN for every symbol; no SPOT_PERP_CARRY claim is possible")
    surv_known = rep["status"] == "OK"
    # ---- the dataset-state (Round 18). DATA_READY used to stand on dataset-level statuses alone: a history holding ONE of 4 lifecycle-eligible symbols read DATA_READY. It now stands on the
    # state: every eligible symbol loaded AND COMPLETE over the whole required window in every required dataset (the funnel's own result, the same one backfill_plan.plan decides
    # research_ready on), plus verified bytes, plus a COMPLETE universe capture.
    findings: Dict[str, List[str]] = {"unavailable": [], "invalid": [], "incomplete": [], "insufficient": []}
    if not scan["universe_dir_supplied"]:
        findings["unavailable"].append("UNIVERSE_DIR_REQUIRED: no (valid) universe capture was supplied")
    if not scan["checked"] or not scan["market_history"]:
        findings["unavailable"].append("no market_history dataset under the data root")
    for m in manifests:
        if not m["verified"]:
            findings["invalid"].append(f"{m['kind']} does not re-verify: {'; '.join(str(p) for p in m['problems'])[:120]}")
        elif m["dataset_status"] in ("CONFLICTED", "UNVERIFIED"):
            findings["invalid"].append(f"{m['kind']} dataset_status is {m['dataset_status']}")
    if inv["datasets"]:
        for need in ("funding", "mark-kline"):
            if need not in have:
                findings["incomplete"].append(f"the required dataset {need} is absent under the data root")
    for d in inv["datasets"]:
        if d["dataset"] in ("funding", "mark-kline", "index-kline") and d["verified"] and d["dataset_status"] not in ("COMPLETE", "CONFLICTED", "UNVERIFIED"):
            findings["incomplete"].append(f"{d['dataset']} dataset_status is {d['dataset_status']}, not COMPLETE")
    for u in uni_good:
        if u["dataset_status"] != "COMPLETE":
            findings["incomplete"].append(f"the universe capture's dataset_status is {u['dataset_status']}, not COMPLETE")
    if rep["status"] == "REFUSED" and not findings["invalid"] and not findings["unavailable"]:
        reason = str(rep["reason"])
        (findings["unavailable"] if ("not found" in reason or "no market_history" in reason or "mark_interval not given" in reason) else findings["invalid"]).append(reason[:200])      # unknown refusal = invalid: fail closed
    if info is not None:
        for k in ("invalid", "incomplete", "insufficient"):
            findings[k] += list(info["dataset_state"]["findings"][k])
    findings = {k: list(dict.fromkeys(v)) for k, v in findings.items()}
    dstate = dataset_state(findings)
    idx_ds = next((d for d in inv["datasets"] if d["dataset"] == "index-kline"), None)
    basis_ok = bool(idx_ds and idx_ds["verified"] and idx_ds["dataset_status"] == "COMPLETE")
    idx_missing: List[str] = []
    if basis_ok:                                   # a dataset-level COMPLETE is not per-symbol coverage (the same defect the state fixes for funding and mark): every loaded symbol needs its own COMPLETE index series
        try:
            with open(os.path.join(idx_ds["dir"], "manifest.json"), "rb") as _fh:
                _isyms = (json.loads(_fh.read()).get("symbols") or {})
        except (OSError, ValueError, AttributeError):
            _isyms = {}
        idx_missing = sorted(sy for sy in rep["symbols_included"] if not (isinstance(_isyms, dict) and isinstance(_isyms.get(sy), dict) and _isyms[sy].get("status") == "COMPLETE"))
    hedgeable = int((rep.get("hedge_counts") or {}).get("SPOT_TRADING", 0) or 0)
    carry_why = ([] if spot_dir is not None else ["no spot-instruments snapshot: hedgeability is unknown for every symbol"]) \
        + ([] if spot_dir is None or hedgeable > 0 else ["no loaded symbol has a Bybit spot pair that is trading (SPOT_TRADING count 0): nothing is hedgeable"]) \
        + ([] if basis_ok else ["no verified COMPLETE index-kline dataset: the spot-vs-perp basis is UNMEASURED, so a hedged-carry claim is impossible"]) \
        + ([] if not idx_missing else [f"index-kline is not COMPLETE for {len(idx_missing)} of {len(rep['symbols_included'])} loaded symbols (first: {idx_missing[:5]}): the basis is UNMEASURED for them"]) \
        + ([] if dstate["sufficient"] else ["the dataset itself is not " + DATA_SUFFICIENT_NOT_EVALUATED])
    rep["dataset_state"] = dstate
    rep["dataset_identity"] = dataset_identity(
        [{"kind": m["kind"], "name": m["kind"], "digest": m["digest"], "verified": m["verified"]} for m in manifests],
        extra={"funnel_digest": ((info or {}).get("funnel") or {}).get("funnel_digest"), "history_window": (info or {}).get("history_window"), "mark_interval": (info or {}).get("mark_interval")})
    if not rep["dataset_identity"]["identifies_every_component"] and dstate["complete_over_requested_window"]:
        findings["invalid"].append("a manifest carries no digest: the dataset cannot be identified")
        dstate = rep["dataset_state"] = dataset_state(findings)
    if dstate["state"] in (DATA_UNAVAILABLE, DATA_INVALID):          # the identity of a dataset that is not usable must never read as a certificate (a CONFLICTED dataset re-hashes fine)
        rep["dataset_identity"] = {**rep["dataset_identity"], "digest": None, "identifies_every_component": False,
                                   "reason": f"{dstate['state']}: a dataset that is not usable has no identity"}
    rep["capabilities"] = {"perp_only_ranked_funding": dstate["sufficient"],
                           "spot_hedged_carry": {"supported": bool(dstate["sufficient"] and spot_dir is not None and basis_ok and not idx_missing and hedgeable > 0), "why_not": carry_why}}
    data_ready = dstate["state"] in COMPLETE_DATA_STATES
    rep["readiness"] = readiness_levels(
        data_ready=data_ready, data_reasons=[] if data_ready else (dstate["reasons"] or [rep["reason"] or "funding/mark-kline/universe not all verified and COMPLETE"]),
        pit_ok=surv_known and surv == DELISTINGS_OBSERVED, survivorship=surv, mechanism_ok=True)
    rep["survivorship_free_claim"] = rep["readiness"]["survivorship_free_claim"]
    rep["highest_supportable_verdict"] = rep["readiness"]["highest_supportable_verdict"] if surv_known else "UNTESTED"
    rep["candidate_level_note"] = "this is a DATASET report: it reaches at most RESEARCH_READY and never implies any strategy has an edge"
    return rep


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Candidate readiness table (offline, read-only).")
    ap.add_argument("--data-root", default=None, help="directory searched for real capture manifests; absent = MISSING")
    ap.add_argument("--json", default="", help="write the machine-readable report here")
    ap.add_argument("--history-root", default="", help="a market_history root (funding/, mark-kline-<interval>/): also run the ranked-funding walk-forward on it")
    ap.add_argument("--universe-dir", default="", help="the verified universe_capture directory the history was built from: REQUIRED by the cross-sectional path (required with --history-root); recognised in data_scan when passed")
    ap.add_argument("--ledger", default="", help="persistent multiple-testing ledger file for --history-root (default: <parent of the history root>/candidate_ledgers/<family>.jsonl: the family count survives a restart)")
    ap.add_argument("--dataset-report", action="store_true",
                    help="print the reproducible dataset report (dataset_state, dataset_identity, per-symbol coverage) for --data-root/--history-root + --universe-dir and exit; "
                         "exit 0 only for DATA_SUFFICIENT_*, 3 for DATA_VALID_BUT_INSUFFICIENT, 2 for UNAVAILABLE / INCOMPLETE / INVALID")
    ap.add_argument("--spot-dir", default=None, help="a spot-instruments snapshot dir (hedgeability; absent = unknown)")
    ap.add_argument("--mark-interval", default=None, help="mark-kline interval (e.g. 240); needed when the root holds more than one")
    ap.add_argument("--unreserved-exploratory", action="store_true",
                    help="evaluate --history-root WITHOUT a reserved holdout: the whole window is read, the result is stamped NOT_A_HOLDOUT_STUDY and the dataset can never be reserved afterwards "
                         "(default: refuse until `tools/history_holdout.py --reserve` has been run)")
    a = ap.parse_args(argv)
    if a.history_root and not a.universe_dir:
        ap.error("--history-root needs --universe-dir (UNIVERSE_DIR_REQUIRED)")
    if a.dataset_report:
        dr = dataset_report(data_root=a.data_root or a.history_root or None, universe_dir=a.universe_dir or None, spot_dir=a.spot_dir, mark_interval=a.mark_interval)
        print(json.dumps(dr, indent=2, sort_keys=True, default=str))
        st = dr["dataset_state"]["state"]
        ident = dr["dataset_identity"]["digest"]
        print(f"DATASET_STATE: {st}  (dataset_identity {ident[:16] + '...' if ident else 'NONE: a component is unverified or has no digest'})", file=sys.stderr)
        return 0 if st in SUFFICIENT_DATA_STATES else (3 if st == DATA_VALID_BUT_INSUFFICIENT else 2)      # a download that exited 0 is not a complete dataset: only DATA_SUFFICIENT_* exits 0
    try:
        rep = build_report(data_root=a.data_root or a.history_root or None, universe_dir=a.universe_dir or None)
    except RH.HarnessError as exc:
        ap.exit(2, f"refused: {exc}\n")
    if a.data_root and not a.universe_dir and any(not r["universe_dir"]["supplied"] and r["universe_dir"]["required"] for r in rep["candidates"]):
        print("UNIVERSE_DIR_REQUIRED: no --universe-dir passed; the cross-sectional rows are NOT evaluated and cannot reach RESEARCH_READY", file=sys.stderr)
    if a.history_root:
        try:
            import history_holdout as HH
            lpath = a.ledger or default_ledger_path("cross_sectional_funding", a.history_root)
            rep["ranked_funding"] = evaluate_cross_sectional(a.history_root, universe_dir=a.universe_dir, ledger_path=lpath, spot_dir=a.spot_dir, mark_interval=a.mark_interval,
                                                             holdout_ledger=HH.default_registry_path(), unreserved_exploratory=a.unreserved_exploratory,
                                                             reader="candidate_readiness.py --history-root")
        except RH.HarnessError as exc:
            ap.exit(2, f"refused: {exc}\n")
    print(render_table(rep))
    for r in rep["candidates"]:
        print(f"\n{r['name']}: {r['readiness_verdict']} (ladder {r['ladder_verdict']})")
        for x in r["readiness_reasons"][:6]:
            print("   - " + x)
    if rep.get("ranked_funding"):
        rf = rep["ranked_funding"]
        print(f"\nranked funding: {rf['verdict']} {rf.get('status')} - {rf.get('reason', '')}")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=2, sort_keys=True, default=str)
        print("wrote " + a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
