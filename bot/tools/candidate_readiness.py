#!/usr/bin/env python3
"""Candidate readiness: for every research candidate (research/candidates/*.json plus the frozen BTC carry description), what is actually known, and the honest verdict.

    python3 tools/candidate_readiness.py [--data-root DIR] [--json OUT] [--spot-symbols FILE]

Offline, read-only. It never reads a credential, the network, bot/state or a database, and it writes nothing unless --json names a file. For each candidate it answers,
with a reason and a data basis for each: required data present (real manifests under --data-root; absent = MISSING, never assumed), point in time, universe complete
enough for the claim, a stated economic mechanism and counterparty, executable through the CURRENTLY QUALIFIED venue capabilities (Bybit only; cross-venue is
RESEARCH_ONLY and never executable), edge against fees + spread + slippage + funding (UNKNOWN when an input is unknown), price-leg / hedge risk measured, capacity supported
by depth, the multiple-testing family size, whether out-of-sample / prospective evidence is possible, and a final verdict from the ladder
(UNTESTED / INSUFFICIENT_DATA / REJECTED). Nothing here promotes a candidate: a verdict above INSUFFICIENT_DATA needs walk-forward and prospective evidence this tool cannot create.

Labels used everywhere: COMMITTED_CORPUS_IN_SAMPLE (data in this repo, the gate constants were chosen with all of it visible), MODELLED (a stated execution scenario, never
measured), ARITHMETIC_ON_DECLARED_INPUTS, OWNER_OBSERVED (a live probe by the owner, unverifiable here), TRAILING_IN_SAMPLE_OBSERVATION (not an expectation).

Also here: ``evaluate_ranked_funding``, the evaluator for "rank perps by trailing funding, short the top" on market_history captures: it separates the FORMATION window from
the HOLDING window, counts every ranking-rule variant in the multiple-testing family, normalises funding to bps per day per symbol, reports PERP-ONLY symbols (no Bybit spot
pair) only as directional price+funding P&L (never as carry), and refuses a verdict above INSUFFICIENT_DATA.
"""
from __future__ import annotations

import argparse
import csv
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
PARTICIPATION_CAP = 0.01                  # ASSUMED share of open interest one position may be (labelled assumed wherever used)


# ---------------------------------------------------------------- authoritative costs (adapter: sub-agent 4 owns the single definition)
def cost_constants() -> Dict[str, Any]:
    """The cost constants as carry_costs exposes them. Preference: the single authoritative ROUND_TRIP_* names; otherwise the parts this tree has, with what is missing said."""
    fee_rt = getattr(CC, "ROUND_TRIP_FEE_BPS", None)
    spread = getattr(CC, "ASSUMED_ROUND_TRIP_SPREAD_BPS", None)
    total = getattr(CC, "ROUND_TRIP_TOTAL_COST_BPS", None)
    basis = "carry_costs authoritative ROUND_TRIP_* constants"
    if fee_rt is None:
        fee_rt, basis = 2.0 * CC.TAKER_BPS_PERP, "carry_costs.TAKER_BPS_PERP x 2 (the authoritative ROUND_TRIP_FEE_BPS is not present in this tree)"
    if total is None:
        total = fee_rt + spread if spread is not None else None
    return {"perp_taker_bps": CC.TAKER_BPS_PERP, "spot_taker_bps": CC.TAKER_BPS_SPOT, "perp_round_trip_fee_bps": float(fee_rt),
            "assumed_round_trip_spread_bps": None if spread is None else float(spread),
            "perp_round_trip_total_bps": None if total is None else float(total),
            "spot_round_trip_fee_bps": 2.0 * CC.TAKER_BPS_SPOT, "basis": basis,
            "spread_included": spread is not None,
            "note": None if spread is not None else "spread not available as an authoritative constant here: every net figure below is FEES ONLY, i.e. an UPPER bound on net"}


def scenarios(c: Mapping[str, Any]) -> Dict[str, float]:
    """Round-trip bps of the PERP legs for an overlay on owned BTC (no spot trades). MAKER_FIRST_* are MODELLED, not measured."""
    perp_taker = float(CC.TAKER_BPS_PERP)
    out = {"TAKER_FEES_ONLY": c["perp_round_trip_fee_bps"]}
    if c["perp_round_trip_total_bps"] is not None:
        out["TAKER_FEES_PLUS_SPREAD"] = c["perp_round_trip_total_bps"]
    out["TAKER_FEES_PLUS_ASSUMED_SLIPPAGE_1BPS_PER_LEG"] = c["perp_round_trip_fee_bps"] + 2.0
    out["MAKER_FIRST_MODELLED_entry_maker_exit_taker"] = float(OM.PERP_MAKER_BPS) + perp_taker       # config: exits are taker under maker_first
    out["ALL_MAKER_MODELLED_best_case_unreachable"] = 2.0 * float(OM.PERP_MAKER_BPS)
    return out


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
    return {"status": st, "pit_basis": u.pit_basis, "problems": u.survivorship_problems()[:6], "unverified_segments": u.unverified_segments[:6],
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
    tk = sc.get("TAKER_FEES_PLUS_SPREAD", sc["TAKER_FEES_ONLY"])
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
    taker = "TAKER_FEES_PLUS_SPREAD" if "TAKER_FEES_PLUS_SPREAD" in econ["scenarios_round_trip_bps"] else "TAKER_FEES_ONLY"
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
        be = (costs["perp_round_trip_total_bps"] if costs["perp_round_trip_total_bps"] is not None else costs["perp_round_trip_fee_bps"]) / 7.0
        assumed = [f"{m}.{k}" for m in ("fee_model", "slippage_model", "funding_model") for k, e in (spec.get(m) or {}).items()
                   if isinstance(e, dict) and e.get("provenance") in ("assumed", "unknown")]
        row["edge_vs_costs"] = {"value": UNKNOWN, "label": LABEL_ARITH,
                                "reason": f"no committed data measures this edge; inputs still 'assumed': {assumed[:6]}",
                                "breakeven_requirement": f"{be:.2f} bps/day of funding+price edge over a 7-day hold just repays a perp round trip ("
                                                         f"{'fees+spread' if costs['spread_included'] else 'fees only'}); per 8h print {be * 480 / 1440:.2f}, per 4h print {be * 240 / 1440:.2f}, per 1h print {be * 60 / 1440:.2f}"}
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


@dataclass
class SymbolSeries:
    """One perp from a market_history capture. ``funding`` = [(ts_ms, rate_bps_per_print)], ``mark`` = [(ts_ms, price)], both ascending.
    ``spot_listed``: True/False from the captured SPOT universe, None = unknown (then hedgeability is UNKNOWN, never assumed)."""
    symbol: str
    interval_min: Optional[int]
    funding: List[Tuple[int, float]]
    mark: List[Tuple[int, float]]
    launch_ms: int = 0
    delivery_ms: Optional[int] = None
    spot_listed: Optional[bool] = None
    oi_usd: Optional[float] = None


def hedgeability(s: SymbolSeries) -> str:
    return {True: "HEDGEABLE_ON_BYBIT", False: "PERP_ONLY"}.get(s.spot_listed, "HEDGE_UNKNOWN")


def _last_at_or_before(seq: Sequence[Tuple[int, float]], t: int) -> Optional[float]:
    lo, hi = 0, len(seq)
    while lo < hi:
        mid = (lo + hi) // 2
        if seq[mid][0] <= t:
            lo = mid + 1
        else:
            hi = mid
    return seq[lo - 1][1] if lo else None


def _sum_range(seq: Sequence[Tuple[int, float]], a: int, b: int) -> Tuple[float, int]:
    vals = [v for ts, v in seq if a < ts <= b]
    return sum(vals), len(vals)


def formation_score(s: SymbolSeries, t: int, formation_ms: int) -> Optional[float]:
    """Mean funding in bps PER DAY over (t - formation, t], interval-normalised, using ONLY prints with ts <= t. None when unknown (no interval / no prints)."""
    if s.interval_min is None:
        return None
    vals = [v for ts, v in s.funding if t - formation_ms < ts <= t]
    if not vals:
        return None
    return statistics.fmean(vals) * 1440.0 / s.interval_min


def _active(s: SymbolSeries, t: int) -> bool:
    return s.launch_ms <= t and (s.delivery_ms is None or t < s.delivery_ms)


def run_variant(series: Sequence[SymbolSeries], *, formation_days: int, hold_days: int, top_k: int, round_trip_bps: float, t_start: int, t_end: int) -> Dict[str, Any]:
    """Non-overlapping holds on a fixed rebalance grid. At each t: rank on the FORMATION window ending at t, hold (t, t+hold] (capped at a Closed contract's deliveryTime),
    short the top ``top_k`` perp-only. Per row: directional total = funding received + price leg of the short - perp round-trip cost (measured from mark prices);
    for HEDGEABLE symbols also the funding-net-of-cost 'carry' (basis residual UNMEASURED here)."""
    f_ms, h_ms = formation_days * MS_DAY, hold_days * MS_DAY
    rows: List[Dict[str, Any]] = []
    skipped = 0
    t = t_start
    while t + h_ms <= t_end:
        scored = sorted(((formation_score(s, t, f_ms), s) for s in series if _active(s, t)), key=lambda x: (-(x[0] if x[0] is not None else -1e18), x[1].symbol))
        picks = [(sc, s) for sc, s in scored if sc is not None][:top_k]
        legs = []
        for sc, s in picks:
            end = min(t + h_ms, s.delivery_ms) if s.delivery_ms is not None else t + h_ms
            m0, m1 = _last_at_or_before(s.mark, t), _last_at_or_before(s.mark, end)
            fund, nprints = _sum_range(s.funding, t, end)
            if m0 is None or m1 is None or m0 <= 0:
                skipped += 1
                continue
            price_bps = -(m1 / m0 - 1.0) * 1e4
            hed = hedgeability(s)
            leg = {"symbol": s.symbol, "interval_min": s.interval_min, "hedgeability": hed, "formation_bps_per_day": sc, "funding_bps": fund, "prints": nprints,
                   "price_leg_bps": price_bps, "directional_total_bps": fund + price_bps - round_trip_bps, "closed_during_hold": s.delivery_ms is not None and s.delivery_ms <= t + h_ms}
            if hed == "HEDGEABLE_ON_BYBIT":
                leg["hedged_carry_funding_net_of_cost_bps"] = fund - round_trip_bps - 2.0 * float(CC.TAKER_BPS_SPOT)
                leg["basis_residual"] = "UNMEASURED"
            if s.oi_usd:
                leg["max_notional_usd_assumed_1pct_of_oi"] = s.oi_usd * PARTICIPATION_CAP
            else:
                leg["max_notional_usd_assumed_1pct_of_oi"] = None
            legs.append(leg)
        if legs:
            rows.append({"t": t, "legs": legs, "row_total_bps": statistics.fmean(l["directional_total_bps"] for l in legs)})
        t += h_ms
    return {"rows": rows, "skipped_legs_without_marks": skipped}


def evaluate_ranked_funding(series: Sequence[SymbolSeries], *, spec: Optional[Mapping[str, Any]] = None, formation_days_grid: Sequence[int] = (3, 7, 14, 30),
                            hold_days: int = 7, top_k: int = 1, round_trip_bps: Optional[float] = None, min_rows: int = 60, alpha: float = 0.05) -> Dict[str, Any]:
    """Walk-forward evaluation of 'short the perps with the highest trailing funding'. Every formation window in the grid is a variant counted in the multiple-testing
    family; the variant used out of sample is chosen on the TRAIN rows only. Returns a verdict capped at INSUFFICIENT_DATA (REJECTED when an adequately sized
    out-of-sample is non-positive). The trailing same-window observation is reported separately and labelled; it is not an expectation."""
    spec = spec if spec is not None else dict(load_candidates()[[n for n, _ in load_candidates()].index("cross_sectional_funding")][1])
    c = cost_constants()
    cost = float(round_trip_bps) if round_trip_bps is not None else (c["perp_round_trip_total_bps"] if c["perp_round_trip_total_bps"] is not None else c["perp_round_trip_fee_bps"])
    unknown_iv = sorted(s.symbol for s in series if s.interval_min is None)
    out: Dict[str, Any] = {"schema": SCHEMA + "/ranked_funding", "round_trip_bps_used": cost, "cost_basis": c["basis"] + ("" if c["spread_included"] else "; FEES ONLY (spread unavailable)"),
                           "symbols": len(series), "hedgeability_counts": {}, "intervals_min_counts": {}, "symbols_without_interval": unknown_iv, "closed_contracts": sum(1 for s in series if s.delivery_ms),
                           "ranking_rule_variants": list(formation_days_grid), "hold_days": hold_days, "top_k": top_k, "ceiling": "INSUFFICIENT_DATA"}
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
    ledger = RH.VariantLedger(fam)
    per: Dict[str, List[float]] = {}
    grid_rows: Dict[str, List[Dict[str, Any]]] = {}
    for F in formation_days_grid:
        start = t0 + max(formation_days_grid) * MS_DAY
        r = run_variant(series, formation_days=F, hold_days=hold_days, top_k=top_k, round_trip_bps=cost, t_start=start, t_end=t1)
        grid_rows[f"F{F}"] = r["rows"]
        per[f"F{F}"] = [x["row_total_bps"] for x in r["rows"]]
    n_rows = min((len(v) for v in per.values()), default=0)
    out["rows_per_variant"] = n_rows
    out["trailing_observation"] = _trailing(series, formation_days_grid[-1] if formation_days_grid else 7)
    if n_rows < min_rows:
        for name, rets in per.items():
            ledger.record_rejected(name, {"formation_days": name, "hold_days": hold_days, "top_k": top_k}, f"only {n_rows} non-overlapping holding rows (< {min_rows})")
        out.update({"verdict": "INSUFFICIENT_DATA", "status": "TOO_FEW_HOLDING_ROWS", "family": fam, "n_variants_counted": ledger.n_variants,
                    "reason": f"{n_rows} non-overlapping {hold_days}-day holding rows after the formation lead-in; need >= {min_rows}. The same-window trailing sums below are "
                              f"{LABEL_TRAILING}, selected after the fact, not an expectation and not a verdict"})
        return out
    cut = {k: v[:n_rows] for k, v in per.items()}
    for name, rets in cut.items():
        ledger.record(name, {"formation_days": name, "hold_days": hold_days, "top_k": top_k}, rets, horizon=1)
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
    out.update({"status": "WALK_FORWARD_DONE", "verdict": verdict, "family": fam, "n_variants_counted": m, "correction": "holm", "oos_rows": len(oos), "oos_mean_total_bps_per_row": mean,
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
        m0, m1 = _last_at_or_before(s.mark, lo), _last_at_or_before(s.mark, t)
        price = None if m0 is None or m1 is None or m0 <= 0 else -(m1 / m0 - 1.0) * 1e4
        hed = hedgeability(s)
        r = {"symbol": s.symbol, "hedgeability": hed, "interval_min": s.interval_min, "prints": n, "gross_funding_bps_window": fund,
             "gross_funding_bps_per_day": None if s.interval_min is None or n == 0 else fund / n * 1440.0 / s.interval_min,
             "short_price_leg_bps_same_window": price}
        if hed != "HEDGEABLE_ON_BYBIT":
            r["note"] = "no Bybit spot pair confirmed: an UNHEDGED short is directional price exposure plus funding, not a carry"
        rows.append(r)
    rows.sort(key=lambda r: -(r["gross_funding_bps_window"]))
    return {"label": LABEL_TRAILING, "is_expectation": False, "window_days": days, "selection": "ranked AFTER the fact on this same window", "top": rows[:10]}


# ---------------------------------------------------------------- market_history ingestion (adapter: layout is owned by the market_history writer)
def load_market_history(path: str, *, reader: Optional[Callable[[str, Mapping[str, Any]], List[SymbolSeries]]] = None) -> Tuple[List[SymbolSeries], Dict[str, Any]]:
    """Read a market_history/v1 directory: manifest schema, dataset_status and coverage are checked here; rows come from ``reader`` or the default reader.
    DEFAULT READER LAYOUT (an ADAPTER ASSUMPTION to reconcile with the collector): manifest["symbols"] = {SYM: {"fundingInterval_min", "launchTime", "deliveryTime", "spot_listed",
    "oi_usd", "funding_file", "mark_file"}}; each *_file is a CSV with header ts_ms,value (funding value in bps per print; mark is a price) inside the directory."""
    m = _read_manifest(os.path.join(path, "manifest.json"))
    if m is None:
        raise RH.HarnessError("market_history manifest missing or unreadable")
    if (m.get("schema") if m.get("schema") is not None else m.get("schema_version")) != "market_history/v1":
        raise RH.HarnessError("manifest schema is not market_history/v1")
    info = {"dataset_status": m.get("dataset_status"), "coverage": m.get("coverage")}
    if m.get("dataset_status") not in ("COMPLETE", "OK", "PASS"):
        info["warning"] = f"dataset_status {m.get('dataset_status')!r} is not COMPLETE: the evaluation inherits that"
    return (reader or _default_reader)(path, m), info


def _csv(path: str) -> List[Tuple[int, float]]:
    with open(path, newline="", encoding="utf-8") as f:
        return sorted((int(r["ts_ms"]), float(r["value"])) for r in csv.DictReader(f))


def _default_reader(path: str, m: Mapping[str, Any]) -> List[SymbolSeries]:
    out = []
    syms = m.get("symbols")
    if not isinstance(syms, dict):
        raise RH.HarnessError("manifest has no symbols map (reconcile the default reader with the collector layout or pass reader=)")
    for sym in sorted(syms):
        e = syms[sym]
        for k in ("funding_file", "mark_file"):
            fn = e.get(k)
            if not isinstance(fn, str) or fn != os.path.basename(fn):
                raise RH.HarnessError(f"{sym}: unsafe or missing {k}")
        iv = e.get("fundingInterval_min")
        d = e.get("deliveryTime")
        out.append(SymbolSeries(sym, iv if isinstance(iv, int) and not isinstance(iv, bool) and iv > 0 else None, _csv(os.path.join(path, e["funding_file"])),
                                _csv(os.path.join(path, e["mark_file"])), int(e.get("launchTime") or 0), int(d) if d else None, e.get("spot_listed"), e.get("oi_usd")))
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Candidate readiness table (offline, read-only).")
    ap.add_argument("--data-root", default=None, help="directory searched for real capture manifests; absent = MISSING")
    ap.add_argument("--json", default="", help="write the machine-readable report here")
    ap.add_argument("--market-history", default="", help="a market_history/v1 directory: also run the ranked-funding walk-forward on it")
    a = ap.parse_args(argv)
    rep = build_report(data_root=a.data_root)
    if a.market_history:
        series, info = load_market_history(a.market_history)
        rep["ranked_funding"] = {"input": info, **evaluate_ranked_funding(series)}
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
