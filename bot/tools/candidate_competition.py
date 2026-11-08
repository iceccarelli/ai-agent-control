#!/usr/bin/env python3
"""Candidate competition: run every credible research candidate through the same ruthless checks and rank what is left. NEVER a forced winner.

    python3 tools/candidate_competition.py [--data-root R] [--universe-dir U] [--json OUT] [--spot-dir S] [--mark-interval IV] [--ledger-dir L]

Offline, read-only, no clock, no network, no credential, no order. For each candidate (cross_sectional_funding, spot_perp_basis, cash_and_carry, market_neutral_relative_value,
passive_execution, liquidity_imbalance, spread_microstructure; cross_venue_funding is RESEARCH_ONLY and never ranked; registered next-hypothesis drafts compete as UNTESTED) it reports:
data readiness from re-verified manifests, point-in-time status, universe completeness, the mechanism, net edge after fees / spread / slippage / funding (None when nothing measures it),
price-leg / hedge risk, capacity, the multiple-testing family size from a PERSISTENT ledger, whether out-of-sample evidence is possible, whether it is executable on the qualified venue
(Bybit only), the highest supportable verdict and the rejection reasons.

Verdicts: REJECTED when it loses after costs; INSUFFICIENT_DATA when evidence exists but cannot support a claim; UNTESTED when the data to test it is missing. Nothing exceeds
INSUFFICIENT_DATA here: a pass needs prospective evidence and the owner's attestation. ``winner`` is None unless a candidate reached PROMISING AND nothing contradicts it; today it is None
and the output says why. Where no data root is given the BTC rows still run on the committed corpus and are labelled COMMITTED_CORPUS_IN_SAMPLE.

Multiple testing: the ledger is PERSISTENT BY DEFAULT (``<ledger-dir>/<family>.jsonl``; default ``$CANDIDATE_LEDGER_DIR`` or the parent of the data root / ``~/.cache``). A fresh process
reloads the count from the file; an identical re-run is the same trial, new data or a new variant enlarges the family.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import candidate_readiness as CR  # noqa: E402
import opportunity_map as OM  # noqa: E402
import research_harness as RH  # noqa: E402

SCHEMA = "candidate_competition/v1"
COMPETING = ("cross_sectional_funding", "spot_perp_basis", "cash_and_carry", "market_neutral_relative_value", "passive_execution", "liquidity_imbalance", "spread_microstructure")
RESEARCH_ONLY = ("cross_venue_funding",)
BTC_ROWS = ("cash_and_carry",)
CORPUS_LABEL = CR.LABEL_CORPUS
VERDICT_TIER = {"PROMISING": 0, "EVALUATED_POSITIVE": 1, "INSUFFICIENT_DATA": 2, "UNTESTED": 3, "REJECTED": 4}
#: why each draft hypothesis is the next thing worth testing with the same infrastructure (the specs themselves are research/candidates/*.json)
DRAFT_RATIONALE = {
    "funding_regime_conditioned_entry": "the committed BTC corpus shows the long-hold edge is a regime (51-print hold net by entry year: negative in 2022 and 2026, strongly positive in 2023-2024); a past-only regime state is "
                                        "the cheapest conditioning to test on data already in the repo, with the same hurdle/ledger machinery",
    "cross_interval_funding_dispersion": "Bybit funding intervals are per symbol (480/240/60 min); ranking within an interval cohort separates the economic signal from a re-pricing-cadence artefact and needs only the "
                                         "funding + mark + universe datasets the cross-sectional path already reads",
    "hedgeable_majors_basis_convergence": "a basis-convergence trade on majors that have a Bybit spot pair earns the basis move rather than a funding level, so it does not depend on a funding regime; it needs "
                                          "synchronised spot/perp prices the repo does not yet have",
}


def default_ledger_dir(data_root: Optional[str], ledger_dir: Optional[str]) -> str:
    if ledger_dir:
        return os.path.abspath(ledger_dir)
    env = os.environ.get("CANDIDATE_LEDGER_DIR")
    if env:
        return os.path.abspath(env)
    if data_root:
        return os.path.join(os.path.dirname(os.path.realpath(data_root)), "candidate_ledgers")
    return os.path.join(os.path.expanduser("~"), ".cache", "ai-agent-control", "candidate_ledgers")


def _ledger(family: str, ldir: str) -> Tuple[Any, str]:
    path = _path(family, ldir)
    return RH.VariantLedger(family, path=path), path


def _path(family: str, ldir: str) -> str:
    path = os.path.join(ldir, RH.normalise_family(family) + ".jsonl")
    if os.path.realpath(path).startswith(os.path.join(BOT, "state") + os.sep):
        raise RH.HarnessError("the ledger may not live under bot/state")
    os.makedirs(ldir, exist_ok=True)
    return path


# ---------------------------------------------------------------- the BTC rows (committed corpus, in-sample)
def btc_policies(econ: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Per-policy economics of the BTC carry on the committed corpus at the taker + assumed-spread cost. A policy is REJECTED when it loses after costs over the WHOLE in-sample corpus
    (where its constants were tuned); a policy that wins in-sample but is negative in the latest year/regime is INSUFFICIENT_DATA with that stated, never a pass."""
    tk = "TAKER_FEES_PLUS_SPREAD"
    mat = {m["hold_prints"]: m for m in econ["unconditional_forced_hold"] if m["scenario"] == tk}
    by_year = econ["long_hold_by_year_mean_net_bps"]
    latest = lambda d: (max(d), d[max(d)]) if d else (None, None)          # noqa: E731
    out: List[Dict[str, Any]] = []
    for h, m in sorted(mat.items()):
        s = m["summary"]
        key = f"{h}_prints"
        ly, lv = latest(by_year.get(key, {}))
        mean = s.get("mean")
        ci = m.get("mean_block_ci90")
        rejected = mean is not None and mean < 0
        out.append({"policy": f"unconditional_{h}_print_hold", "hold_prints": h, "mean_net_bps": mean, "n_overlapping_entries": s.get("n"), "mean_block_ci90": ci,
                    "latest_year": ly, "latest_year_mean_net_bps": lv, "label": CORPUS_LABEL,
                    "verdict": "REJECTED" if rejected else "INSUFFICIENT_DATA",
                    "note": ("loses after costs over the whole corpus" if rejected else
                             ("in-sample positive, regime dependent" + (f"; latest year {ly}: {lv:.1f} bps" if lv is not None and lv <= 0 else "")))})
    g = econ["gated_sequential_taker"]
    out.append({"policy": "production_gated_sequence", "hold_prints": g["hold_prints"], "mean_net_bps": g["mean_net_bps"], "n_overlapping_entries": g["n"], "trailing_365d_mean_net_bps": g["trailing_365d_mean_net_bps"],
                "trailing_365d_n": g["trailing_365d_n"], "label": CORPUS_LABEL + " (gate constants tuned on this corpus)",
                "verdict": "REJECTED" if g["mean_net_bps"] is not None and g["mean_net_bps"] < 0 else "INSUFFICIENT_DATA",
                "note": f"trailing 365d mean {g['trailing_365d_mean_net_bps']:.1f} bps over n={g['trailing_365d_n']}" if g["trailing_365d_mean_net_bps"] is not None else "no trailing trades"})
    return out


def btc_multiple_testing(econ: Optional[Mapping[str, Any]], ldir: str) -> Dict[str, Any]:
    """Record every BTC hold-length variant in the persistent ``cash_and_carry`` ledger (an identical re-run is not a new trial) and report the family's Holm result."""
    led, path = _ledger("cash_and_carry", ldir)
    try:
        rows = OM.load_bybit(BOT)
    except (OSError, ValueError, KeyError):
        return {"family": "cash_and_carry", "n_variants_counted": led.n_variants, "scope": "PERSISTENT_LEDGER_FILE", "ledger_path": path, "note": "committed corpus unreadable: nothing recorded"}
    cost = CR.cost_constants()["perp_round_trip_total_bps"]
    for h in CR.HOLDS_PRINTS:
        rets = [e["net_bps"] for e in OM.all_forced(rows, h, cost_bps=cost)]
        name = f"forced_hold|prints={h}|cost={cost}"
        if rets and RH.series_digest(rets) not in led.returns_digests(name):
            led.record(name, {"hold_prints": h, "round_trip_bps": cost}, rets, horizon=h)
    sel = led.selection()
    return {"family": "cash_and_carry", "n_variants_counted": led.n_variants, "scope": "PERSISTENT_LEDGER_FILE", "ledger_path": path, "best_variant": sel["best_variant"],
            "best_p_adjusted": sel["best_p_adjusted"], "significant_after_adjustment": sel["significant_after_adjustment"], "method": sel["method"],
            "note": "overlapping in-sample holds: serial dependence is handled by the ledger's ESS; this is not an out-of-sample test"}


# ---------------------------------------------------------------- cross-sectional
def run_cross_sectional(data_root: Optional[str], universe_dir: Optional[str], ldir: str, *, spot_dir: Optional[str], mark_interval: Optional[str], history_root: Optional[str]) -> Dict[str, Any]:
    """The cross-sectional evaluation or the loud reason it did not run. Never raises on missing/failed data."""
    root = history_root or data_root
    if not universe_dir:
        return {"status": "UNIVERSE_DIR_REQUIRED", "reason": "UNIVERSE_DIR_REQUIRED: the cross-sectional path takes an explicit verified universe_capture directory (--universe-dir); nothing was evaluated"}
    if not root:
        return {"status": "NO_HISTORY_ROOT", "reason": "no --data-root (market_history root) given: nothing to evaluate"}
    try:
        out = CR.evaluate_cross_sectional(root, universe_dir=universe_dir, ledger_path=_path("cross_sectional_funding", ldir), spot_dir=spot_dir, mark_interval=mark_interval)
    except RH.HarnessError as exc:
        return {"status": "REFUSED", "reason": str(exc)[:300]}
    return {"status": "EVALUATED", "evaluation": out}


def _xs_summary(res: Mapping[str, Any]) -> Dict[str, Any]:
    if res["status"] != "EVALUATED":
        return {"verdict": "UNTESTED", "net_edge": None, "reasons": [res["reason"]], "families": {}, "evaluation": None}
    ev = res["evaluation"]
    fams = ev["families"]
    reasons: List[str] = []
    verdicts = []
    best = None
    fam_out: Dict[str, Any] = {}
    for k, f in fams.items():
        verdicts.append(f["verdict"])
        oos = f.get("oos_mean_total_bps_per_row") if f.get("status") == "WALK_FORWARD_DONE" else None
        desc = None
        if f.get("status") == "TOO_FEW_HOLDING_ROWS":
            ba = (f.get("descriptive_only") or {}).get("book_analytics") or {}
            means = [v["mean_net_bps"] for v in ba.values() if isinstance(v, Mapping) and v.get("mean_net_bps") is not None]
            desc = max(means) if means else None
        fam_out[k] = {"verdict": f["verdict"], "status": f.get("status"), "is_carry": f["is_carry"], "rows": f.get("independent_holding_rows"), "oos_mean_net_bps_per_row": oos,
                      "descriptive_best_in_sample_net_bps": desc, "descriptive_label": "IN_SAMPLE_DESCRIPTIVE_NOT_A_TEST" if desc is not None else None,
                      "p_adjusted": f.get("p_adjusted"), "n_variants_counted": f.get("n_variants_counted"), "survivorship": f.get("survivorship"), "reason": f.get("reason"),
                      "oos_beta_to_market": (f.get("oos_book_analytics") or {}).get("beta_to_market"),
                      "oos_max_drawdown_bps": (f.get("oos_book_analytics") or {}).get("max_drawdown_bps")}
        if oos is not None and (best is None or oos > best):
            best = oos
        if f.get("reason"):
            reasons.append(f"{k}: {f['status']}: {str(f['reason'])[:200]}")
    rf = ev
    if rf.get("history_coverage", {}) and (rf["history_coverage"] or {}).get("status") == "HISTORY_COVERS_FEW_ELIGIBLE_SYMBOLS":
        reasons.append("HISTORY_COVERS_FEW_ELIGIBLE_SYMBOLS: " + str(rf["history_coverage"]["symbols_in_history"]) + " of " + str(rf["history_coverage"]["universe_eligible_symbols"]) + " eligible symbols")
    if ev.get("survivorship") == CR.SURVIVORS_ONLY:
        reasons.append("SURVIVORS_ONLY: no survivorship-free claim")
    verdict = "REJECTED" if verdicts and all(v == "REJECTED" for v in verdicts) else "INSUFFICIENT_DATA"
    return {"verdict": verdict, "net_edge": best, "reasons": reasons, "families": fam_out, "evaluation": ev}


# ---------------------------------------------------------------- the competition
def _tier(row: Mapping[str, Any]) -> int:
    v = row["verdict"]
    if row["readiness_level"] in ("PROMISING", "PROSPECTIVE", "EXECUTION_READY", "OWNER_PROFIT_EVIDENCED"):
        return VERDICT_TIER["PROMISING"]
    ne = row["net_edge"]["value_bps"]
    if v == "INSUFFICIENT_DATA" and ne is not None and ne > 0:
        return VERDICT_TIER["EVALUATED_POSITIVE"]
    return VERDICT_TIER[v]


def run_competition(*, data_root: Optional[str] = None, universe_dir: Optional[str] = None, spot_dir: Optional[str] = None, mark_interval: Optional[str] = None,
                    ledger_dir: Optional[str] = None, history_root: Optional[str] = None, with_economics: bool = True) -> Dict[str, Any]:
    """Run the competition. Deterministic for fixed inputs (no clock). The ledger is persistent by default (``default_ledger_dir``)."""
    ldir = default_ledger_dir(data_root or history_root, ledger_dir)
    base = CR.build_report(data_root=data_root or history_root, universe_dir=universe_dir, with_economics=with_economics)
    xs = _xs_summary(run_cross_sectional(data_root, universe_dir, ldir, spot_dir=spot_dir, mark_interval=mark_interval, history_root=history_root))
    evals = {"cross_sectional_funding": xs["evaluation"]["families"].get("PERP_FUNDING_SPREAD")} if xs["evaluation"] else None
    if evals and evals["cross_sectional_funding"] is not None:
        base = CR.build_report(data_root=data_root or history_root, universe_dir=universe_dir, with_economics=with_economics, evaluations=evals)
    econ = base["economics"]["btc_carry_corpus"]
    rows: List[Dict[str, Any]] = []
    for r in base["candidates"]:
        name = r["name"]
        if name == "btcusdt_funding_carry_overlay":
            continue
        draft = name not in COMPETING and name not in RESEARCH_ONLY
        verdict, reasons = r["readiness_verdict"], list(r["readiness_reasons"])
        net: Optional[float] = None
        net_basis = "UNKNOWN: no committed or supplied data measures this edge"
        extra: Dict[str, Any] = {}
        mt_led, mt_path = _ledger(RH.normalise_family(r["multiple_testing"]["family"]), ldir)
        mt = {"family": r["multiple_testing"]["family"], "n_variants_counted": mt_led.n_variants, "scope": "PERSISTENT_LEDGER_FILE", "ledger_path": mt_path}
        if name == "cash_and_carry" and econ is not None and "unconditional_forced_hold" in econ:
            pols = btc_policies(econ)
            lost = [p for p in pols if p["verdict"] == "REJECTED"]
            kept = [p for p in pols if p["verdict"] != "REJECTED"]
            verdict = "REJECTED" if pols and not kept else "INSUFFICIENT_DATA"
            best = max((p for p in pols if p.get("mean_net_bps") is not None), key=lambda p: p["mean_net_bps"])
            net, net_basis = best["mean_net_bps"], f"{CORPUS_LABEL}: best policy {best['policy']} at taker + ASSUMED spread (11.4 bps round trip); in-sample, regime dependent"
            reasons = [f"REJECTED policy {p['policy']}: mean net {p['mean_net_bps']:.1f} bps after costs ({p['note']})" for p in lost] + \
                      [f"not rejected but not a pass: {p['policy']}: mean net {p['mean_net_bps']:.1f} bps ({p['note']})" for p in kept] + \
                      ["no out-of-sample settlement exists in the repo; the gate constants were tuned on this corpus"]
            mt = btc_multiple_testing(econ, ldir)
            extra["policies"] = pols
        elif name == "cross_sectional_funding":
            verdict, reasons = xs["verdict"], xs["reasons"] + [x for x in reasons if "UNIVERSE_DIR_REQUIRED" in x]
            net = xs["net_edge"]
            net_basis = ("OUT_OF_SAMPLE_WITHIN_THE_CAPTURE_ONLY: best family's out-of-sample mean net bps per 7-day row (perp-only families are NOT carry)" if net is not None else
                         "UNKNOWN: not evaluated, or too few holding rows for a test (descriptive in-sample figures are in families[])")
            extra["families"] = xs["families"]
            mt = {**mt, "per_family": {k: v["n_variants_counted"] for k, v in xs["families"].items()}}
            if xs["families"]:
                mt["n_variants_counted"] = sum(v["n_variants_counted"] or 0 for v in xs["families"].values())
        elif name in RESEARCH_ONLY:
            verdict = "UNTESTED"
        if verdict not in ("REJECTED", "INSUFFICIENT_DATA", "UNTESTED"):
            verdict = "UNTESTED"
        lv = r["readiness"]
        row = {"candidate": r["candidate"], "name": name, "origin": "DRAFT_PROPOSED" if draft else ("RESEARCH_ONLY" if name in RESEARCH_ONLY else "SPEC"),
               "verdict": verdict, "highest_supportable_verdict": verdict, "readiness_level": lv["highest"], "readiness_levels": {k: v["reached"] for k, v in lv["levels"].items()},
               "data_readiness": {"value": r["required_data"]["value"], "trusted_items": r["required_data"]["n_trusted"], "items": r["required_data"]["n_items"],
                                  "missing": [i["name"] for i in r["required_data"]["items"] if not (i["trusted"] and i["complete"])]},
               "point_in_time": r["point_in_time"]["value"], "universe_dir": r["universe_dir"]["status"], "universe_complete_for_claim": r["universe_complete_for_claim"],
               "mechanism_present": r["economic_mechanism"]["present"],
               "net_edge": {"value_bps": net, "basis": net_basis, "required_to_break_even": r["edge_vs_costs"].get("breakeven_requirement")},
               "price_leg_hedge_risk_measured": r["price_leg_hedge_risk_measured"]["value"], "capacity_supported_by_depth": r["capacity_supported_by_depth"]["value"],
               "multiple_testing": mt, "oos_possible": r["oos_or_prospective_possible"]["value"],
               "executable_on_qualified_venue": {"value": r["executable_on_qualified_venues"]["value"], "executable": r["executable_on_qualified_venues"]["executable"],
                                                 "venues": "Bybit only (venues.qualified_names)"},
               "rejection_reasons": reasons[:12], "label": CORPUS_LABEL if name == "cash_and_carry" else None, **extra}
        if draft:
            row["rationale"] = DRAFT_RATIONALE.get(name, "")
        rows.append(row)
    ranked = sorted((x for x in rows if x["origin"] != "RESEARCH_ONLY"), key=lambda x: (_tier(x), -(x["net_edge"]["value_bps"] if x["net_edge"]["value_bps"] is not None else -1e18), x["name"]))
    for i, x in enumerate(ranked, 1):
        x["rank"] = i
    for x in rows:
        x.setdefault("rank", None)
    promising = [x for x in ranked if _tier(x) == VERDICT_TIER["PROMISING"]]
    all_failed = not promising
    out: Dict[str, Any] = {"schema": SCHEMA, "tool": "tools/candidate_competition.py", "inputs": {"data_root": data_root, "universe_dir": universe_dir, "spot_dir": spot_dir, "history_root": history_root},
                           "ledger": {"dir": ldir, "scope": "PERSISTENT_LEDGER_FILE", "note": "a fresh process reloads the family count from these files; an identical re-run is the same trial"},
                           "findings": base.get("findings", []), "data_scan": base["data_scan"], "results": sorted(rows, key=lambda x: (x["rank"] is None, x["rank"] or 0, x["name"])),
                           "winner": None if all_failed else promising[0]["name"], "all_failed": all_failed,
                           "winner_statement": ("NO WINNER: no candidate reached PROMISING; the ranking orders evidence, it does not pick a strategy. Nothing here is a trade recommendation."
                                                if all_failed else f"{promising[0]['name']} reached PROMISING (in-sample to its capture): still INSUFFICIENT_DATA until prospective evidence and the owner's attestation"),
                           "next_hypotheses": [], "caveats": base["caveats"]}
    if all_failed:
        out["next_hypotheses"] = [{"candidate": x["candidate"], "name": x["name"], "status": "UNTESTED (registered draft, specification only)", "rationale": x.get("rationale", ""),
                                   "data_missing": x["data_readiness"]["missing"]} for x in rows if x["origin"] == "DRAFT_PROPOSED"]
    if universe_dir is None and (data_root or history_root):
        out["findings"].append("UNIVERSE_DIR_REQUIRED: no --universe-dir; the cross-sectional rows were not evaluated")
    return out


def render_table(rep: Mapping[str, Any]) -> str:
    cols = ("rank", "candidate", "origin", "verdict", "level", "net_edge_bps", "data", "PIT", "exec", "mt_n", "top_reason")
    lines = ["\t".join(cols)]
    for r in rep["results"]:
        ne = r["net_edge"]["value_bps"]
        lines.append("\t".join(str(x) for x in (r["rank"] if r["rank"] is not None else "-", r["name"], r["origin"], r["verdict"], r["readiness_level"], "n/a" if ne is None else f"{ne:.1f}",
                                                r["data_readiness"]["value"], r["point_in_time"], r["executable_on_qualified_venue"]["value"], r["multiple_testing"]["n_variants_counted"],
                                                (r["rejection_reasons"][0][:70] if r["rejection_reasons"] else ""))))
    lines.append("")
    lines.append(rep["winner_statement"])
    for f in rep["findings"]:
        lines.append("FINDING: " + f)
    if rep["next_hypotheses"]:
        lines.append("next hypotheses (UNTESTED drafts): " + ", ".join(h["name"] for h in rep["next_hypotheses"]))
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Candidate competition (offline, read-only, never a forced winner).")
    ap.add_argument("--data-root", default=None, help="market_history root (funding/, mark-kline-<interval>/); also scanned for manifests")
    ap.add_argument("--universe-dir", default=None, help="verified universe_capture directory (or a parent holding exactly one): REQUIRED for the cross-sectional rows")
    ap.add_argument("--history-root", default=None, help="market_history root if different from --data-root")
    ap.add_argument("--spot-dir", default=None)
    ap.add_argument("--mark-interval", default=None)
    ap.add_argument("--ledger-dir", default=None, help="persistent multiple-testing ledgers (default: $CANDIDATE_LEDGER_DIR, <parent of data root>/candidate_ledgers, or ~/.cache)")
    ap.add_argument("--json", default="", help="write the machine-readable result here")
    a = ap.parse_args(argv)
    rep = run_competition(data_root=a.data_root, universe_dir=a.universe_dir, spot_dir=a.spot_dir, mark_interval=a.mark_interval, ledger_dir=a.ledger_dir, history_root=a.history_root)
    print(render_table(rep))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=2, sort_keys=True, default=str)
        print("wrote " + a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
