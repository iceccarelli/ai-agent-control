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
    for fn in sorted(os.listdir(CAND_DIR)):
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


def scan_data_root(root: Optional[str], *, max_depth: int = 4) -> Dict[str, Any]:
    """Find capture manifests (schema universe_capture/v2, market_history/v1) under ``root``. No root = nothing was looked for = MISSING, not assumed present."""
    res: Dict[str, Any] = {"root": root, "universe_capture_dirs": [], "market_history": [], "checked": root is not None}
    if root is None or not os.path.isdir(root):
        res["checked"] = False
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
                res["universe_capture_dirs"].append(dp)
            elif schema == "market_history/v1":
                res["market_history"].append({"dir": dp, "dataset_status": m.get("dataset_status"), "coverage": m.get("coverage")})
    res["universe_capture_dirs"].sort()
    return res


def _needs_universe(spec: Mapping[str, Any]) -> bool:
    txt = (str(spec.get("hypothesis", "")) + " " + " ".join(str(d.get("name", "")) for d in spec.get("required_data", []) if isinstance(d, dict))).lower()
    return "universe" in txt or "delisted" in txt or "listing" in txt


def universe_assessment(spec: Mapping[str, Any], scan: Mapping[str, Any]) -> Dict[str, Any]:
    if not _needs_universe(spec):
        return {"status": "NOT_APPLICABLE_SINGLE_INSTRUMENT", "reason": "the candidate trades one named instrument (BTCUSDT), so there is no universe to survive"}
    if not scan["checked"]:
        return {"status": MISSING, "reason": "no --data-root given: no universe_capture manifest was looked for, so none is assumed"}
    if not scan["universe_capture_dirs"]:
        return {"status": MISSING, "reason": "no universe_capture/v2 manifest under the data root"}
    mh = scan["market_history"]
    cov = [c["coverage"] for c in mh if isinstance(c.get("coverage"), dict) and isinstance(c["coverage"].get("first_ts"), int) and isinstance(c["coverage"].get("last_ts"), int)]
    if not cov:
        return {"status": "UNVERIFIED", "reason": "universe captures exist but no market_history coverage fixes a study window, so coverage cannot be judged",
                "captures": len(scan["universe_capture_dirs"])}
    lo, hi = min(c["first_ts"] for c in cov), max(c["last_ts"] for c in cov)
    if hi <= lo:
        return {"status": "UNVERIFIED", "reason": "market_history coverage window is empty"}
    u = RH.PointInTimeUniverse.from_capture_dirs(scan["universe_capture_dirs"], study_from_ms=lo, study_to_ms=hi)
    st = {RH.PIT_RAW: "VERIFIED_RAW_RESPONSES", RH.PIT_PARTIAL: "PARTIAL", RH.PIT_ASSERTED: "UNVERIFIED"}[u.pit_basis]
    return {"status": st, "pit_basis": u.pit_basis, "pit_basis_note": PIT_LABEL_NOTE, "problems": u.survivorship_problems()[:6], "unverified_segments": u.unverified_segments[:6],
            "study_window_ms": [lo, hi], "funding_normalisation_known": u.funding_normalisation_known, "captures": len(scan["universe_capture_dirs"]),
            "closed_contracts_seen": len(u.delisted_between(0, 2 ** 62))}


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
    return {"long_hold_by_year_mean_net_bps": long_by_year, "long_hold_cost_bps": tk,
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
    reasons.append("no out-of-sample settlement exists in the repo; no price-leg/basis residual beyond the 4h-bar basis shed; no L2 depth: INSUFFICIENT_DATA is the ceiling")
    policies = {"single_print_harvest": "REJECTED" if neg_single else UNKNOWN, "gated_policy": "INSUFFICIENT_DATA"}
    return "INSUFFICIENT_DATA", reasons, policies


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


def _data_present(spec: Mapping[str, Any], scan: Mapping[str, Any]) -> Dict[str, Any]:
    rd = [d for d in spec.get("required_data", []) if isinstance(d, dict)]
    declared_missing = [d["name"] for d in rd if d.get("repo_has") is False]
    return {"spec_declares_missing": declared_missing, "spec_declares_present": [d["name"] for d in rd if d.get("repo_has") is True],
            "manifests_checked": scan["checked"],
            "value": MISSING if (declared_missing and not scan["checked"]) else ("MISSING" if declared_missing else "PRESENT_PER_SPEC_UNVERIFIED_BY_MANIFEST"),
            "note": "the spec's repo_has is a declaration; only a verified manifest under --data-root counts as present data"}


def candidate_row(name: str, spec: Mapping[str, Any], scan: Mapping[str, Any], econ: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    cid = str(spec.get("candidate_id"))
    ladder = SV.evaluate(spec, [])
    bias = spec.get("bias_declarations", {}) if isinstance(spec.get("bias_declarations"), dict) else {}
    uni = universe_assessment(spec, scan)
    ex = _executable(name, spec)
    costs = cost_constants()
    carry = name in CORPUS_CARRY and econ is not None and "unconditional_forced_hold" in econ
    row: Dict[str, Any] = {
        "candidate": cid, "name": name,
        "required_data": _data_present(spec, scan),
        "point_in_time": {"value": uni["status"], "detail": uni, "declared_by_spec": bias.get("point_in_time_universe")},
        "universe_complete_for_claim": uni["status"] in ("VERIFIED_RAW_RESPONSES", "NOT_APPLICABLE_SINGLE_INSTRUMENT"),
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
    row["verdict_rank_ceiling"] = "INSUFFICIENT_DATA"
    return row


def _oos(spec: Mapping[str, Any]) -> Dict[str, Any]:
    pp = spec.get("prospective_protocol")
    if isinstance(pp, dict):
        return {"value": "POSSIBLE_NOT_STARTED" if pp.get("start_ms") is None else "STARTED", "min_duration_ms": pp.get("min_duration_ms"), "min_settlements": pp.get("min_settlements")}
    if pp:
        return {"value": "REGISTERED_PLAN", "plan": str(pp)[:120]}
    return {"value": "NO_PROTOCOL"}


def build_report(*, data_root: Optional[str] = None, with_economics: bool = True) -> Dict[str, Any]:
    scan = scan_data_root(data_root)
    econ = btc_carry_economics() if with_economics else None
    rows = [candidate_row(n, s, scan, econ) for n, s in load_candidates()]
    return {"schema": SCHEMA, "tool": "tools/candidate_readiness.py", "data_scan": scan, "cost_constants": cost_constants(), "owner_observed": OWNER_INTERVAL_NOTE,
            "economics": {"btc_carry_corpus": econ}, "candidates": rows,
            "caveats": ["every number sourced from the committed corpus is IN-SAMPLE; no out-of-sample settlement exists in this repository",
                        "MAKER_FIRST figures are a MODELLED execution scenario, never measured; fee tier is the published BASE tier, the account's tier is read at run time",
                        "no verdict here exceeds INSUFFICIENT_DATA: that needs walk-forward and prospective evidence, a registry and the owner's attestation"]}


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
KEY_BASIS_OBSERVATION = "OBSERVATION_TIME"
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
    key_basis: Optional[str] = None          # KEY_BASIS_OBSERVATION only when built by ``load_history_root`` (marks/funding keyed by observation time); None = hand-built, unverified
    mark_interval_ms: Optional[int] = None   # the mark-kline interval from the mark manifest (staleness bound = 2 intervals); None = inferred from the mark spacing


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


def run_variant(series: Sequence[SymbolSeries], *, formation_days: int, hold_days: int, top_k: int, round_trip_bps: float, t_start: int, t_end: int,
                max_staleness_ms: Optional[int] = None) -> Dict[str, Any]:
    """Non-overlapping holds on a fixed rebalance grid. At each t: rank on the FORMATION window ending at t, hold (t, t+hold] (capped at a Closed contract's deliveryTime),
    short the top ``top_k`` perp-only. Per row: directional total = funding received + price leg of the short - perp round-trip cost (measured from mark prices);
    for HEDGEABLE symbols also the funding-net-of-cost 'carry' (basis residual UNMEASURED here).
    A stale entry or exit mark (older than ``max_staleness_ms``, default 2 mark intervals) SKIPS the leg (``skipped_legs_without_marks``). A contract that closed during the hold
    and whose exit mark is stale/absent is a TERMINAL EXIT UNPRICED: the loss is unknown, the leg is listed in ``terminal_exit_unpriced_legs`` and the evaluation cannot pass."""
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
            m0, m1 = price_at(s.mark, t, bound), price_at(s.mark, end, bound)
            fund, nprints = _sum_range(s.funding, t, end)
            if m0 is None or m1 is None or m0 <= 0:
                skipped += 1
                if m0 is not None and m0 > 0 and m1 is None and s.delivery_ms is not None and s.delivery_ms <= t + h_ms:
                    unpriced.append({"symbol": s.symbol, "t": t, "delivery_ms": s.delivery_ms})
                continue
            price_bps = -(m1 / m0 - 1.0) * 1e4
            hed = hedgeability(s)
            leg = {"symbol": s.symbol, "interval_min": s.interval_min, "hedgeability": hed, "formation_bps_per_day": sc, "funding_bps": fund, "prints": nprints, "hedgeability_basis": HEDGE_BASIS,
                   "price_leg_bps": price_bps, "directional_total_bps": fund + price_bps - round_trip_bps, "closed_during_hold": s.delivery_ms is not None and s.delivery_ms <= t + h_ms}
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
            "staleness_bound": "explicit" if max_staleness_ms is not None else f"{STALENESS_INTERVALS}x_mark_interval_per_symbol", "time_mode": "REPLAY_OBSERVATION_TIME"}


def _survivorship_of(universe_info: Optional[Mapping[str, Any]]) -> str:
    v = universe_info.get("survivorship") if isinstance(universe_info, Mapping) else None
    if v != DELISTINGS_OBSERVED:
        return SURVIVORS_ONLY                                                           # nothing said = survivors only (fail closed)
    missing = universe_info.get("closed_in_universe_not_in_series")
    if not isinstance(missing, Mapping) or missing.get("count") != 0:                   # absent (cannot be verified) or non-empty: the series is survivors-only
        return SURVIVORS_ONLY
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
    capped at INSUFFICIENT_DATA (REJECTED when an adequately sized out-of-sample is non-positive). The trailing same-window observation is reported separately and labelled;
    it is not an expectation.

    ``universe_info`` is the ``info`` from ``load_history_root``: it supplies ``survivorship`` (absent = SURVIVORS_ONLY). ``multiple_testing_scope`` is PER_PROCESS for the
    default path-less ledger (the count survives only inside this process) and PERSISTENT_LEDGER_FILE with ``ledger_path``."""
    unkeyed = sorted(s.symbol for s in series if s.key_basis != KEY_BASIS_OBSERVATION)
    if unkeyed and not allow_unverified_key_basis_for_tests:
        raise RH.HarnessError(f"{len(unkeyed)} series lack key_basis={KEY_BASIS_OBSERVATION!r} (only load_history_root sets it): marks/funding keyed by candle start would leak the future; "
                              f"first: {unkeyed[:3]}")
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
    cin = universe_info.get("closed_in_universe_not_in_series") if isinstance(universe_info, Mapping) else None
    if isinstance(cin, Mapping) and cin.get("count"):
        warnings.append(f"SURVIVORS_ONLY: {cin.get('count')} Closed/delivered perpetuals of the universe record that overlap the study window are absent from the evaluated series "
                        f"(first: {list(cin.get('names') or [])[:5]})")
    elif isinstance(universe_info, Mapping) and universe_info.get("survivorship") == DELISTINGS_OBSERVED and survivorship == SURVIVORS_ONLY:
        warnings.append("SURVIVORS_ONLY (downgraded from a DELISTINGS_OBSERVED claim): " + (
            "the claim carries no closed_in_universe_not_in_series check, so it cannot be verified against the evaluated series"))
    if unkeyed:
        warnings.append(f"KEY_BASIS_UNVERIFIED: {len(unkeyed)} hand-built series were not keyed by observation time here (test-only path): the verdict is capped at INSUFFICIENT_DATA")
    if any(sp.spot_listed is not None for sp in series):
        warnings.append("hedgeability comes from ONE current spot snapshot (CURRENT_SNAPSHOT_ONLY): it labels legs, it is not a historical fact")
    out: Dict[str, Any] = {"schema": SCHEMA + "/ranked_funding", "round_trip_bps_used": cost, "cost_basis": c["basis"],
                           "symbols": len(series), "hedgeability_counts": {}, "intervals_min_counts": {}, "symbols_without_interval": unknown_iv, "closed_contracts": sum(1 for s in series if s.delivery_ms),
                           "ranking_rule_variants": list(formation_days_grid), "hold_days": hold_days, "top_k": top_k, "ceiling": "INSUFFICIENT_DATA",
                           "hold_days_grid": holds, "top_k_grid": ks, "cost_scenarios_bps": dict(costs), "survivorship": survivorship, "warnings": warnings,
                           "closed_in_universe_not_in_series": dict(cin) if isinstance(cin, Mapping) else None, "key_basis_unverified": bool(unkeyed),
                           "skipped_legs_without_marks": 0, "terminal_exit_unpriced": 0,
                           "multiple_testing_scope": "PER_PROCESS" if ledger_path is None else "PERSISTENT_LEDGER_FILE",
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
            ledger.record_rejected(name, params_of[name], f"only {n_rows} non-overlapping holding rows (< {min_rows})")
        out.update({"verdict": "INSUFFICIENT_DATA", "status": "TOO_FEW_HOLDING_ROWS", "family": fam, "n_variants_counted": ledger.n_variants,
                    "n_variants_tried_in_call": len(out["variants_tried"]),
                    "reason": f"{n_rows} non-overlapping {hold_days}-day holding rows after the formation lead-in; need >= {min_rows}. The same-window trailing sums below are "
                              f"{LABEL_TRAILING}, selected after the fact, not an expectation and not a verdict"})
        return out
    cut = {k: v[:n_rows] for k, v in per.items()}
    for name, rets in cut.items():
        ledger.record(name, params_of[name], rets, horizon=1)
    for name, (rets, h) in counted_only.items():
        if len(rets) >= 2:
            ledger.record(name, params_of[name], rets, horizon=1)
        else:
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
    if out["terminal_exit_unpriced"] or unkeyed:
        verdict = "INSUFFICIENT_DATA"                   # unknown terminal losses / unverified timing: neither a pass nor a rejection can be claimed
    out.update({"status": "WALK_FORWARD_DONE", "verdict": verdict, "family": fam, "n_variants_counted": m, "n_variants_tried_in_call": len(out["variants_tried"]),
                "correction": "holm", "oos_rows": len(oos), "oos_mean_total_bps_per_row": mean,
                "oos_independent_observations": st["independent_observations"], "p_one_sided": st["p_one_sided"], "p_adjusted": adj, "selected_per_fold": chosen,
                "label": "OUT_OF_SAMPLE_WITHIN_THE_CAPTURE_ONLY: one regime, one capture; not prospective",
                "reason": "directional total = funding + short price leg - perp round trip; PERP_ONLY legs are never carry. A pass would still need prospective evidence"})
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


def load_history_root(root: str, *, universe_dir: str, spot_dir: Optional[str] = None, mark_interval: Optional[str] = None) -> Tuple[List[SymbolSeries], Dict[str, Any]]:
    """Series from a market_history root written by ``tools/market_history.py``: ``<root>/funding``, ``<root>/mark-kline-<interval>`` (rows.jsonl per symbol), plus a verified
    universe_capture directory (launch / delivery / fundingInterval_min) and optionally a spot-instruments snapshot (hedgeability).

    Nothing unverified is evaluated: each dataset must pass ``market_history.verify_dataset``, the universe ``universe_capture.verify_capture_dir`` with a RE-DERIVED
    dataset_status of COMPLETE or PARTIAL (PARTIAL is a warning), the spot snapshot ``verify_spot``; otherwise HarnessError. Funding is stored as a decimal FRACTION per print
    (bps = rate * 1e4); the mark is the kline close, KEYED BY ITS ``observation_time`` (candle start + interval; a row claiming earlier raises LookAheadError). Each funding print is
    normalised by the interval of its own manifest segment (a symbol with unknown intervals for any print is excluded and listed). ``info['survivorship']`` is SURVIVORS_ONLY unless the
    Closed partition served rows (then DELISTINGS_OBSERVED); SURVIVORS_ONLY adds a loud warning. No point-in-time claim is made here (``point_in_time_claim`` is False). Symbols must be in both datasets AND the universe; CONFLICTED / UNVERIFIED symbols and symbols missing anywhere are
    excluded and LISTED in ``info['excluded']``; other non-COMPLETE symbol statuses are kept and reported in ``info['non_complete_symbols']``. Gaps are never filled."""
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
    excluded: List[Dict[str, str]] = []
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
    allsyms = sorted(set(fman["symbols"]) | set(mman["symbols"]))
    series: List[SymbolSeries] = []
    for sym in allsyms:
        why = None
        if sym not in recs:
            why = "not_in_universe"
        elif sym not in fman["symbols"]:
            why = "not_in_funding_dataset"
        elif sym not in mman["symbols"]:
            why = "not_in_mark_dataset"
        else:
            sts = {"funding": fman["symbols"][sym].get("status"), mark_name: mman["symbols"][sym].get("status")}
            bad = {k: v for k, v in sts.items() if v in EXCLUDED_SYMBOL_STATUSES or v is None}
            if bad:
                why = "symbol_status:" + ",".join(f"{k}={v}" for k, v in sorted(bad.items()))
            elif any(v != "COMPLETE" for v in sts.values()):
                non_complete[sym] = {k: str(v) for k, v in sts.items() if v != "COMPLETE"}
        if why:
            excluded.append({"symbol": sym, "reason": why})
            continue
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
            fund = sorted((r["observation_time"], float(Decimal(r["v"]["fundingRate"]) * 10000)) for r in frows)
            mark = sorted((r["observation_time"], float(Decimal(r["v"]["close"]))) for r in mrows)
        except RH.HarnessError:
            raise
        except (KeyError, InvalidOperation, ValueError, TypeError, OSError) as exc:
            raise RH.HarnessError(f"{sym}: rows.jsonl unreadable after verification: {type(exc).__name__}") from exc
        if not fund or not mark:
            excluded.append({"symbol": sym, "reason": "no_rows"})
            continue
        segs, seg_why = funding_interval_segments(fman["symbols"][sym], [t for t, _ in fund])
        if segs is None:
            excluded.append({"symbol": sym, "reason": seg_why or "funding_interval_unknown"})
            continue
        if "interval_disagrees_with_record" in (fman["symbols"][sym].get("reasons") or []):
            mismatch.append(sym)
        rec = recs[sym]
        iv = rec.get("fundingInterval_min")
        d = rec.get("deliveryTime")
        if rec.get("status") in RH.ENDED_STATUSES and not (isinstance(d, int) and not isinstance(d, bool) and d > 0):
            excluded.append({"symbol": sym, "reason": "closed_without_delivery"})          # an ended contract with no delivery time would be immortal (stale exit price)
            continue
        launch = rec.get("launchTime")
        series.append(SymbolSeries(sym, iv if isinstance(iv, int) and not isinstance(iv, bool) and iv > 0 else None, fund, mark,
                                   launch if isinstance(launch, int) and not isinstance(launch, bool) and launch > 0 else 0,
                                   d if isinstance(d, int) and not isinstance(d, bool) and d > 0 else None, SPOT_VERDICT_TO_LISTED.get(verdicts.get(sym)) if verdicts else None,
                                   funding_intervals=segs, key_basis=KEY_BASIS_OBSERVATION,
                                   mark_interval_ms=iv_ms if isinstance(iv_ms, int) and not isinstance(iv_ms, bool) and iv_ms > 0 else None))
    in_series = {x.symbol for x in series}
    w0 = min((x.funding[0][0] for x in series), default=None)
    w1 = max((x.funding[-1][0] for x in series), default=None)

    def _overlaps(r: Mapping[str, Any]) -> bool:
        dl, ln = r.get("deliveryTime"), r.get("launchTime")
        dl = dl if isinstance(dl, int) and not isinstance(dl, bool) and dl > 0 else None
        ln = ln if isinstance(ln, int) and not isinstance(ln, bool) and ln > 0 else None
        return (w0 is None or dl is None or dl >= w0) and (w1 is None or ln is None or ln <= w1)
    closed_missing = sorted(sy for sy, r in recs.items() if r.get("status") in RH.ENDED_STATUSES and sy not in in_series and _overlaps(r))
    closed_info = {"count": len(closed_missing), "names": closed_missing[:NAMES_CAP], "names_truncated": len(closed_missing) > NAMES_CAP,
                   "window_ms": [w0, w1], "rule": "every Closed/Delivering perpetual of the universe record overlapping the study window must be in the evaluated series"}
    if closed_missing:
        if survivorship == DELISTINGS_OBSERVED:
            survivorship = SURVIVORS_ONLY
        warnings.append(f"SURVIVORS_ONLY: {len(closed_missing)} Closed/delivered perpetuals in the universe record overlap the study window but are absent from the evaluated series "
                        f"(first: {closed_missing[:5]}): the evaluated series is survivors-only whatever the universe capture observed")
    if non_complete:
        warnings.append(f"{len(non_complete)} symbols are not COMPLETE in a dataset (kept, listed in non_complete_symbols)")
    if excluded:
        warnings.append(f"{len(excluded)} symbols excluded (listed in excluded)")
    info: Dict[str, Any] = {"root": os.path.abspath(root), "mark_interval": mark_iv,
                            "datasets": {"funding": {"verified": True, "dataset_status": fman.get("dataset_status"), "manifest_digest": fman.get("manifest_digest")},
                                         mark_name: {"verified": True, "dataset_status": mman.get("dataset_status"), "manifest_digest": mman.get("manifest_digest")}},
                            "universe": {"dir": uinfo.get("dir"), "dataset_status": uinfo.get("dataset_status"), "manifest_sha256": uinfo.get("manifest_sha256"), "symbols": uinfo.get("symbols"),
                                         "delisting_observability": uinfo.get("delisting_observability"), "status_coverage": uinfo.get("status_coverage"), "delisting_note": delisting_note},
                            "survivorship": survivorship, "closed_in_universe_not_in_series": closed_info, "point_in_time_claim": False,
                            "point_in_time_note": "this loader builds series from capture snapshots; it makes no point-in-time claim (use PointInTimeUniverse.from_capture_dirs for that). " + PIT_LABEL_NOTE,
                            "time_basis": "marks and funding are keyed by observation_time (replay mode: receipt time is not stored per row)",
                            "interval_mismatch_symbols": mismatch,
                            "non_complete_symbols": non_complete, "excluded": excluded, "symbols_loaded": len(series), "warnings": warnings}
    if hedge_info is not None:
        info["hedge_verdicts"] = {s.symbol: verdicts.get(s.symbol) for s in series}
        info["hedge"] = hedge_info
    if warnings:
        info["warning"] = "; ".join(warnings)
    return series, info


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Candidate readiness table (offline, read-only).")
    ap.add_argument("--data-root", default=None, help="directory searched for real capture manifests; absent = MISSING")
    ap.add_argument("--json", default="", help="write the machine-readable report here")
    ap.add_argument("--history-root", default="", help="a market_history root (funding/, mark-kline-<interval>/): also run the ranked-funding walk-forward on it")
    ap.add_argument("--universe-dir", default="", help="the verified universe_capture directory the history was built from (required with --history-root)")
    ap.add_argument("--spot-dir", default=None, help="a spot-instruments snapshot dir (hedgeability; absent = unknown)")
    ap.add_argument("--mark-interval", default=None, help="mark-kline interval (e.g. 240); needed when the root holds more than one")
    a = ap.parse_args(argv)
    if a.history_root and not a.universe_dir:
        ap.error("--history-root needs --universe-dir")
    rep = build_report(data_root=a.data_root)
    if a.history_root:
        try:
            series, info = load_history_root(a.history_root, universe_dir=a.universe_dir, spot_dir=a.spot_dir, mark_interval=a.mark_interval)
        except RH.HarnessError as exc:
            ap.exit(2, f"refused: {exc}\n")
        rep["ranked_funding"] = {"input": info, **evaluate_ranked_funding(series, universe_info=info)}
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
