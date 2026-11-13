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
import cash_basis as CB  # noqa: E402
import opportunity_map as OM  # noqa: E402
import research_harness as RH  # noqa: E402

SCHEMA = "candidate_competition/v1"
COMPETING = ("cross_sectional_funding", "spot_perp_basis", "cash_and_carry", "market_neutral_relative_value", "passive_execution", "liquidity_imbalance", "spread_microstructure")
RESEARCH_ONLY = ("cross_venue_funding",)
#: Round 18 (red team): the families whose completed walk-forward the competition puts in front of the readiness ladder (``evals`` in ``run_competition``). Every other row can only be REJECTED in-sample
#: or UNTESTED: PROMISING is unreachable for it whatever the data, so its NO WINNER is STRUCTURAL (``NO_EVALUATOR_WIRED:<family>``), not a measurement. Wiring a BTC carry out-of-sample evaluator
#: spends the owner's out-of-sample lock and is the owner's decision; this tuple is pinned by a test against the evaluations the function really builds.
WIRED_EVALUATORS = ("cross_sectional_funding",)
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
    return CR.ledger_dir_for(data_root, ledger_dir)                    # THE one rule: candidate_readiness, regime_entry_eval, history_holdout and this tool all resolve it here


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
    cap = econ.get("on_capital_after_cash") or {}
    cap_ok = cap.get("status") == "COMPUTED"
    cap_holds = ((cap.get("classes") or {}).get(CR.CAP_OVERLAY) or {}).get("holds", {}) if cap_ok else {}
    latest = lambda d: (max(d), d[max(d)]) if d else (None, None)          # noqa: E731
    out: List[Dict[str, Any]] = []
    for h, m in sorted(mat.items()):
        s = m["summary"]
        key = f"{h}_prints"
        ly, lv = latest(by_year.get(key, {}))
        mean = s.get("mean")
        ci = m.get("mean_block_ci90")
        ch = cap_holds.get(str(h)) or {}
        excess = ch.get("mean_excess_over_cash_bps_of_capital")
        lost_nominal = mean is not None and mean < 0
        lost_to_cash = (not lost_nominal) and excess is not None and excess < 0
        rejected = lost_nominal or lost_to_cash
        rb = ch.get("rejection_bars") or {}
        bar = (rb.get("excess") or rb.get("nominal")) if rejected else None            # the like-for-like test is against CASH (the alternative); the nominal (zero capital cost) test only when no cash series covers it
        out.append({"policy": f"unconditional_{h}_print_hold", "hold_prints": h, "mean_net_bps": mean, "n_overlapping_entries": s.get("n"), "mean_block_ci90": ci,
                    "mean_excess_over_cash_bps_of_capital": excess, "excess_block_ci90": ch.get("excess_block_ci90"), "mean_net_bps_on_capital": ch.get("mean_net_bps_on_capital"),
                    "n_cash_unknown": ch.get("n_cash_unknown"), "annualised_excess_pct_of_capital": ch.get("annualised_excess_pct_of_capital"),
                    "latest_year": ly, "latest_year_mean_net_bps": lv, "label": CORPUS_LABEL,
                    "verdict": "REJECTED" if rejected else "INSUFFICIENT_DATA", "statistical_rejection": bar,
                    "note": (("loses after costs over the whole corpus" if lost_nominal else
                              (f"positive on notional ({mean:.1f} bps, ZERO capital cost) but {excess:.1f} bps EXCESS over cash on the capital it ties up: it does not beat the cash alternative" if lost_to_cash else
                               ("in-sample positive, regime dependent" + (f"; latest year {ly}: {lv:.1f} bps" if lv is not None and lv <= 0 else "")
                                + ("" if (cap_ok and excess is not None) else "; CASH_UNKNOWN: no cost of capital applied")))) + _strength_note(rejected, bar))})
    g = econ["gated_sequential_taker"]
    gc = cap.get("gated_sequential_overlay") if cap_ok else None
    g_excess = (gc or {}).get("mean_excess_over_cash_bps_of_capital")
    g_nominal_lost = g["mean_net_bps"] is not None and g["mean_net_bps"] < 0
    g_cash_lost = (not g_nominal_lost) and g_excess is not None and g_excess < 0
    g_rejected = g_nominal_lost or g_cash_lost
    g_rb = (gc or {}).get("rejection_bars") or {}
    g_bar = (g_rb.get("excess") or g_rb.get("nominal")) if g_rejected else None
    out.append({"policy": "production_gated_sequence", "hold_prints": g["hold_prints"], "mean_net_bps": g["mean_net_bps"], "n_overlapping_entries": g["n"], "trailing_365d_mean_net_bps": g["trailing_365d_mean_net_bps"],
                "trailing_365d_n": g["trailing_365d_n"], "mean_excess_over_cash_bps_of_capital": g_excess, "excess_block_ci90": (gc or {}).get("excess_block_ci90"),
                "label": CORPUS_LABEL + " (gate constants tuned on this corpus)",
                "verdict": "REJECTED" if g_rejected else "INSUFFICIENT_DATA", "statistical_rejection": g_bar,
                "note": ((f"positive on notional ({g['mean_net_bps']:.1f} bps, ZERO capital cost) but {g_excess:.1f} bps EXCESS over cash on the capital it ties up; " if g_cash_lost else "")
                         + (f"trailing 365d mean {g['trailing_365d_mean_net_bps']:.1f} bps over n={g['trailing_365d_n']}" if g["trailing_365d_mean_net_bps"] is not None else "no trailing trades")
                         + _strength_note(g_rejected, g_bar))})
    return out


def _table_caveat(meeting: Sequence[str], not_meeting: Sequence[str]) -> Optional[str]:
    """The short first reason of the BTC row (the ranked table prints only the first, 90 characters): which REJECTED policies are a point-estimate loss only. None when no policy is REJECTED:
    'all 0 REJECTED policies meet the bar' is a vacuous sentence (independent re-review, finding 13)."""
    short = lambda n: n.replace("unconditional_", "").replace("_print_hold", "p").replace("production_gated_sequence", "gated_seq")             # noqa: E731
    if not_meeting:
        return "IN-SAMPLE; point-estimate only (NOT stat. rejected): " + ", ".join(short(n) for n in not_meeting)
    if meeting:
        return f"IN-SAMPLE; all {len(meeting)} REJECTED policies meet the stat. rejection bar"
    return None


def _strength_note(rejected: bool, bar: Optional[Mapping[str, Any]]) -> str:
    """Say, in the note, whether a REJECTED policy meets the repo's early-rejection standard, and when it does NOT, WHICH criterion failed with its own number (a point-estimate loss that does not meet the
    standard is not a statistical rejection). The standard is the conjunction (candidate_readiness.early_rejection): >= REJECT_EARLY_MIN_INDEPENDENT independent observations, a negative mean, lower-tail
    p <= REJECT_EARLY_ALPHA (ESS-adjusted) AND the widest 90% interval wholly below zero. Each failed criterion is printed as itself: an earlier version printed ``p=0.005 > 0.01`` when the interval was what
    failed, and 'does not exclude break-even' when the interval was wholly negative (second independent review)."""
    if not rejected or not bar:
        return ""
    if bar.get("meets_early_rejection_standard"):
        return f"; meets the repo's early-rejection standard in-sample (ESS-adjusted lower-tail p={bar['p_lower']:.4f}, widest 90% interval {[round(x, 1) for x in bar['widest_ci90']]} wholly below zero)"
    ci, p, ind = bar.get("widest_ci90"), bar.get("p_lower"), bar.get("independent_observations")
    failed: List[str] = []
    if not isinstance(ind, int) or ind < CR.REJECT_EARLY_MIN_INDEPENDENT:
        failed.append(f"only {ind if isinstance(ind, int) else 0} independent observations (the standard needs {CR.REJECT_EARLY_MIN_INDEPENDENT})")
    if not isinstance(p, (int, float)):
        failed.append("no usable lower-tail p")
    elif not (p <= CR.REJECT_EARLY_ALPHA):
        failed.append(f"ESS-adjusted lower-tail p={p:.3f} is above {CR.REJECT_EARLY_ALPHA}")
    if not (isinstance(ci, (list, tuple)) and len(ci) == 2 and all(isinstance(x, (int, float)) for x in ci)):
        failed.append("no usable 90% interval")
    elif not (ci[1] < 0):
        failed.append(f"the widest 90% interval {[round(x, 1) for x in ci]} " + ("includes zero" if ci[0] < 0 < ci[1] else "is not wholly below zero"))
    return ("; NOT statistically rejected at the repo's early-rejection standard (" + ("; ".join(failed) if failed else "a criterion of the standard could not be evaluated")
            + "): the point estimate loses, the standard for calling it a rejection is not met")


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
def run_cross_sectional(data_root: Optional[str], universe_dir: Optional[str], ldir: str, *, spot_dir: Optional[str], mark_interval: Optional[str], history_root: Optional[str],
                        unreserved_exploratory: bool = False, holdout_registry: Any = None, cash_bases: Sequence[Any] = ()) -> Dict[str, Any]:
    """The cross-sectional evaluation or the loud reason it did not run. Never raises on missing/failed data. The load is CUT at the reserved-holdout boundary and refused (HOLDOUT_NOT_RESERVED,
    with the exact command) when nothing is reserved in the per-user registry (``holdout_registry``, default ``history_holdout.default_registry_path()``; ``ldir`` only holds the
    multiple-testing ledgers), unless ``unreserved_exploratory`` (stamped NOT_A_HOLDOUT_STUDY, recorded, and the time it read is burned)."""
    root = history_root or data_root
    if not universe_dir:
        return {"status": "UNIVERSE_DIR_REQUIRED", "reason": "UNIVERSE_DIR_REQUIRED: the cross-sectional path takes an explicit verified universe_capture directory (--universe-dir); nothing was evaluated"}
    if not root:
        return {"status": "NO_HISTORY_ROOT", "reason": "no --data-root (market_history root) given: nothing to evaluate"}
    import history_holdout as HH
    try:
        out = CR.evaluate_cross_sectional(root, universe_dir=universe_dir, ledger_path=_path("cross_sectional_funding", ldir), spot_dir=spot_dir, mark_interval=mark_interval,
                                          holdout_ledger=HH.default_registry_path() if holdout_registry is None else holdout_registry, unreserved_exploratory=unreserved_exploratory,
                                          reader="candidate_competition", cash_bases=cash_bases)
    except RH.HarnessError as exc:
        return {"status": "REFUSED", "reason": str(exc)[:600]}
    return {"status": "EVALUATED", "evaluation": out}


def _xs_summary(res: Mapping[str, Any]) -> Dict[str, Any]:
    if res["status"] != "EVALUATED":
        return {"verdict": "UNTESTED", "net_edge": None, "reasons": [res["reason"]], "families": {}, "evaluation": None, "survivorship": None, "holdout": None}
    ev = res["evaluation"]
    fams = ev["families"]
    reasons: List[str] = []
    verdicts = []
    best = None
    fam_out: Dict[str, Any] = {}
    excess_by_basis: Dict[str, List[float]] = {}
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
                      "p_adjusted": f.get("p_adjusted"), "n_variants_counted": f.get("n_variants_counted"), "n_variants_used_for_nominal_p": f.get("n_variants_used_for_nominal_p"),
                      "survivorship": f.get("survivorship"), "reason": f.get("reason"),
                      "rejected_early": f.get("reject_early") is not None, "oos_independent_observations": f.get("oos_independent_observations"),
                      "oos_beta_to_market": (f.get("oos_book_analytics") or {}).get("beta_to_market"),
                      "oos_max_drawdown_bps": (f.get("oos_book_analytics") or {}).get("max_drawdown_bps"),
                      "vs_cash": {lbl: {k2: b.get(k2) for k2 in ("verdict", "status", "rows", "rows_without_cash", "capital_per_gross_notional", "oos_mean_excess_bps_of_capital_per_row", "p_adjusted",
                                                                 "oos_mean_cash_hurdle_bps_of_gross_per_row", "oos_independent_observations", "reason")} for lbl, b in (f.get("vs_cash") or {}).items()}}
        for lbl, b in (f.get("vs_cash") or {}).items():
            x = b.get("oos_mean_excess_bps_of_capital_per_row")
            if x is not None:
                excess_by_basis.setdefault(lbl, []).append(x)
        if oos is not None and (best is None or oos > best):
            best = oos
        if f.get("reason"):
            reasons.append(f"{k}: {f['status']}: {str(f['reason'])[:200]}")
    rf = ev
    if rf.get("history_coverage", {}) and (rf["history_coverage"] or {}).get("status") == "HISTORY_COVERS_FEW_ELIGIBLE_SYMBOLS":
        reasons.append("HISTORY_COVERS_FEW_ELIGIBLE_SYMBOLS: " + str(rf["history_coverage"]["symbols_in_history"]) + " of " + str(rf["history_coverage"]["universe_eligible_symbols"]) + " eligible symbols")
    if ev.get("survivorship") == CR.SURVIVORS_ONLY:
        reasons.append("SURVIVORS_ONLY: no survivorship-free claim")
    ho = (ev.get("input") or {}).get("holdout") if isinstance(ev.get("input"), Mapping) else None
    if isinstance(ho, Mapping) and ho.get("status") != "RESERVED":
        reasons.insert(0, f"HOLDOUT {ho.get('status')}: {ho.get('label')}")           # an exploratory read must be the first thing a reader of this row sees
    verdict = "REJECTED" if verdicts and all(v == "REJECTED" for v in verdicts) else "INSUFFICIENT_DATA"
    net_edge, basis = best, "NOMINAL_BPS_OF_GROSS_NOTIONAL (no cash alternative was applied: this is NOT a comparison with cash)"
    if excess_by_basis:
        # With a cash source applied the headline is the EXCESS over cash on the capital committed, and it is the CONSERVATIVE one: the lowest, over the sources applied, of each source's best family. A figure that
        # ignores the cash the book displaces is never the headline when the cash is known (the nominal figure stays in every family row).
        net_edge = min(max(v) for v in excess_by_basis.values())
        basis = "EXCESS_OVER_CASH_BPS_OF_CAPITAL_PER_ROW (lowest over the cash sources applied: " + ", ".join(sorted(excess_by_basis)) + ")"
        if best is not None and best > 0 and net_edge <= 0:
            reasons.append(f"VS_CASH: the nominal out-of-sample mean is +{best:.1f} bps of gross notional per row but the excess over cash on the capital committed is {net_edge:+.1f} bps of capital per row")
    return {"verdict": verdict, "net_edge": net_edge, "net_edge_basis": basis, "reasons": reasons, "families": fam_out, "evaluation": ev, "survivorship": ev.get("survivorship"),
            "holdout": dict(ho) if isinstance(ho, Mapping) else None}


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
                    ledger_dir: Optional[str] = None, history_root: Optional[str] = None, with_economics: bool = True, unreserved_exploratory: bool = False,
                    holdout_registry: Any = None, cash_bases: Sequence[Any] = ()) -> Dict[str, Any]:
    """Run the competition. Deterministic for fixed inputs (no clock). The ledger is persistent by default (``default_ledger_dir``)."""
    ldir = default_ledger_dir(data_root or history_root, ledger_dir)
    base = CR.build_report(data_root=data_root or history_root, universe_dir=universe_dir, with_economics=with_economics)
    xs = _xs_summary(run_cross_sectional(data_root, universe_dir, ldir, spot_dir=spot_dir, mark_interval=mark_interval, history_root=history_root, unreserved_exploratory=unreserved_exploratory,
                                           holdout_registry=holdout_registry, cash_bases=cash_bases))
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
        persisted_trials = mt_led.persisted_variants         # what the family's LEDGER FILE holds: not `n_variants`, which a long process raises to a family-wide high-water mark, and not the cross-sectional total re-stated below
        mt = {"family": r["multiple_testing"]["family"], "n_variants_counted": mt_led.n_variants, "persisted_variants": persisted_trials, "scope": "PERSISTENT_LEDGER_FILE", "ledger_path": mt_path}
        if name == "cash_and_carry" and econ is not None and "unconditional_forced_hold" in econ:
            pols = btc_policies(econ)
            lost = [p for p in pols if p["verdict"] == "REJECTED"]
            kept = [p for p in pols if p["verdict"] != "REJECTED"]
            verdict = "REJECTED" if pols and not kept else "INSUFFICIENT_DATA"
            best = max((p for p in pols if p.get("mean_net_bps") is not None), key=lambda p: p["mean_net_bps"])
            nominal = best["mean_net_bps"]
            with_cash = [p for p in pols if p.get("mean_excess_over_cash_bps_of_capital") is not None]
            if with_cash:
                # the headline is the EXCESS over cash on the capital tied up (the like-for-like USD-risk alternative), never the notional figure that charges no capital
                bx = max(with_cash, key=lambda p: p["mean_excess_over_cash_bps_of_capital"])
                net = bx["mean_excess_over_cash_bps_of_capital"]
                net_basis = (f"{CORPUS_LABEL}: best policy {bx['policy']}: EXCESS over cash on 1.16x capital (OKX USDT lending, a lender's floor, not the owner's cost) after taker + ASSUMED spread "
                             f"(11.4 bps round trip); the nominal best is {nominal:.1f} bps of NOTIONAL at ZERO capital cost; in-sample, regime dependent")
                extra["nominal_net_bps_zero_capital_cost"] = nominal
            else:
                net, net_basis = nominal, (f"{CORPUS_LABEL}: best policy {best['policy']} at taker + ASSUMED spread (11.4 bps round trip), bps of NOTIONAL with ZERO capital cost "
                                           "(CASH_UNKNOWN: the cost of capital could not be applied); in-sample, regime dependent")
            reasons = [f"REJECTED policy {p['policy']}: mean net {p['mean_net_bps']:.1f} bps of notional" + (f", excess over cash {p['mean_excess_over_cash_bps_of_capital']:.1f} bps of capital"
                                                                                                         if p.get("mean_excess_over_cash_bps_of_capital") is not None else "")
                       + f" ({p['note']})" for p in lost] + \
                      [f"not rejected but not a pass: {p['policy']}: mean net {p['mean_net_bps']:.1f} bps ({p['note']})" for p in kept] + \
                      ["no out-of-sample settlement exists in the repo; the gate constants were tuned on this corpus"]
            mt = btc_multiple_testing(econ, ldir)
            extra["policies"] = pols
            meeting = [p["policy"] for p in pols if p["verdict"] == "REJECTED" and (p.get("statistical_rejection") or {}).get("meets_early_rejection_standard")]
            not_meeting = [p["policy"] for p in pols if p["verdict"] == "REJECTED" and not (p.get("statistical_rejection") or {}).get("meets_early_rejection_standard")]
            extra["statistical_rejection_summary"] = {"meeting_the_standard": meeting, "not_meeting_the_standard": not_meeting, "scope": "IN_SAMPLE_OVERLAPPING_ENTRIES",
                                                      "reading": "REJECTED is the documented in-sample verdict (a loss after costs or to cash by point estimate); the repo's stronger early-rejection standard is met only by the policies "
                                                                 "listed; registered holds are at most 17 days, longer holds (90/365 days) were never registered and this verdict does not cover them"}
            reasons.append(f"REJECTED is an IN-SAMPLE point-estimate verdict on registered holds of at most 17 days; policies meeting the repo's statistical rejection standard: {len(meeting)} of {len(meeting) + len(not_meeting)}"
                           + (f"; NOT statistically rejected: {', '.join(not_meeting)}" if not_meeting else "") + "; longer holds were never registered")
            first = _table_caveat(meeting, not_meeting)
            if first:                                           # the ranked table prints reasons[0]: the caveat must be what a reader sees first
                reasons.insert(0, first)
        elif name == "cross_sectional_funding":
            verdict, reasons = xs["verdict"], xs["reasons"] + [x for x in reasons if "UNIVERSE_DIR_REQUIRED" in x]
            net = xs["net_edge"]
            # the row's label is the one the summary computed (it was hard-coded as 'net bps per 7-day row', so an EXCESS over cash in bps of CAPITAL sat in the row labelled as a nominal figure)
            net_basis = (f"OUT_OF_SAMPLE_WITHIN_THE_CAPTURE_ONLY: {xs['net_edge_basis']}; out-of-sample mean per 7-day row (perp-only families are NOT carry)" if net is not None else
                         "UNKNOWN: not evaluated, or too few holding rows for a test (descriptive in-sample figures are in families[])")
            extra["families"] = xs["families"]
            extra["survivorship"] = xs["survivorship"]          # the table must show it: a survivors-only cross-section flatters every net edge it prints
            extra["holdout"] = xs["holdout"]
            mt = {**mt, "per_family": {k: v["n_variants_counted"] for k, v in xs["families"].items()}}
            if xs["families"]:
                mt["n_variants_counted"] = sum(v["n_variants_counted"] or 0 for v in xs["families"].values())
        elif name in RESEARCH_ONLY:
            verdict = "UNTESTED"
        if verdict not in ("REJECTED", "INSUFFICIENT_DATA", "UNTESTED"):
            verdict = "UNTESTED"
        # A family whose persistent ledger has COUNTED trials has been tried: 'no test has been run' is false, its data was read once, and offering it as a next
        # hypothesis would invite a further variant on that data (a wider multiple-testing family). Round 17: reg-1 and reg-2 were counted yet the row said UNTESTED.
        # Only the PERSISTENT ledger counts here (`persisted_trials`): a refused or unevaluated run states a count of its own (the cross-sectional row's re-stated total) and has
        # recorded nothing, which `tests/test_history_holdout.py` pins (a refused run leaves no ledger file and stays UNTESTED).
        n_counted = persisted_trials or 0
        if verdict == "UNTESTED" and name not in RESEARCH_ONLY and n_counted > 0:
            verdict = "INSUFFICIENT_DATA"
            reasons = [x for x in reasons if not x.startswith("no test has been run")]
            reasons.insert(0, f"TRIALS_ALREADY_COUNTED: {n_counted} variant(s) of family {mt['family']!r} are in the persistent multiple-testing ledger, so this mechanism "
                              "has been tried and its data read; the result is not recorded here, and another variant on that data widens the family")
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
        wired = name in WIRED_EVALUATORS
        row["evaluator"] = {"wired": wired, "code": None if wired else f"NO_EVALUATOR_WIRED:{name}"}
        if name == "cross_sectional_funding":
            # The data state of the history this evaluation read, with the candidate's verdict folded in (Round 18 review: the two DATA_SUFFICIENT_* verdict states were never produced). REJECTED on sufficient
            # data -> DATA_SUFFICIENT_CANDIDATE_REJECTED; PROMISING on the ladder -> ..._ELIGIBLE_FOR_FURTHER_REVIEW; an evaluator that says "not enough" keeps DATA_VALID_BUT_INSUFFICIENT; any data finding wins.
            fam_ev = (evals or {}).get("cross_sectional_funding") if evals else None
            ds0 = fam_ev.get("dataset_state") if isinstance(fam_ev, Mapping) else None
            if isinstance(ds0, Mapping) and isinstance(ds0.get("findings"), Mapping):
                cv = "PROMISING" if lv["levels"]["PROMISING"]["reached"] else (verdict if verdict in ("REJECTED", "INSUFFICIENT_DATA", "UNTESTED") else "INSUFFICIENT_DATA")
                row["dataset_state"] = CR.dataset_state(ds0["findings"], candidate_verdict=cv)
        if not wired:                                                # reasons only: no verdict, level, rank or net edge above depends on this
            row["rejection_reasons"] = reasons[:11] + [f"NO_EVALUATOR_WIRED:{name}: no walk-forward evaluator is connected to this family in run_competition, so PROMISING is unreachable "
                                                         "for it whatever the data: NO WINNER for this row is structural, not measured"]
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
                           "next_hypotheses": [], "caveats": base["caveats"],
                           "no_winner_breakdown": {"evaluator_wired": sorted(x["name"] for x in rows if x["evaluator"]["wired"]),
                                                   "structural_no_evaluator_wired": sorted(x["name"] for x in rows if not x["evaluator"]["wired"]),
                                                   "reading": "a row without a wired evaluator cannot reach PROMISING whatever the data: its NO WINNER is structural, not an evaluated failure"}}
    if all_failed:
        out["next_hypotheses"] = [{"candidate": x["candidate"], "name": x["name"], "status": "UNTESTED (registered draft, specification only)", "rationale": x.get("rationale", ""),
                                   "data_missing": x["data_readiness"]["missing"]} for x in rows
                                  if x["origin"] == "DRAFT_PROPOSED" and x["verdict"] == "UNTESTED" and not (x["multiple_testing"].get("persisted_variants") or 0)]
    if universe_dir is None and (data_root or history_root):
        out["findings"].append("UNIVERSE_DIR_REQUIRED: no --universe-dir; the cross-sectional rows were not evaluated")
    return out


def render_table(rep: Mapping[str, Any]) -> str:
    cols = ("rank", "candidate", "origin", "verdict", "level", "net_edge_bps", "data", "PIT", "survivorship", "holdout", "exec", "mt_n", "top_reason")
    lines = ["\t".join(cols)]
    for r in rep["results"]:
        ne = r["net_edge"]["value_bps"]
        lines.append("\t".join(str(x) for x in (r["rank"] if r["rank"] is not None else "-", r["name"], r["origin"], r["verdict"], r["readiness_level"], "n/a" if ne is None else f"{ne:.1f}",
                                                r["data_readiness"]["value"], r["point_in_time"], r.get("survivorship") or "-", (r.get("holdout") or {}).get("status") or "-",
                                                r["executable_on_qualified_venue"]["value"], r["multiple_testing"]["n_variants_counted"],
                                                (r["rejection_reasons"][0][:90] if r["rejection_reasons"] else ""))))
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
    ap.add_argument("--unreserved-exploratory", action="store_true",
                    help="evaluate the cross-sectional rows WITHOUT a reserved holdout: the whole window is read, the result is stamped NOT_A_HOLDOUT_STUDY and the dataset can never be reserved "
                         "afterwards (default: refuse until `tools/history_holdout.py --reserve` has been run)")
    ap.add_argument("--no-historical-cash", action="store_true", help="do NOT compare the cross-sectional rows with the repository's historical cash series (OKX USDT lending): the result is then bps of gross notional "
                    "with no cash alternative applied, and says so")
    ap.add_argument("--owner-cash-apr", default=None, help="YOUR stated cost of capital as a plain decimal fraction per year (0.04 = 4%%): a separate cash source, never substituted for the historical series")
    ap.add_argument("--owner-cash-source", default=None, help="where that figure comes from (free text)")
    ap.add_argument("--owner-cash-stated-utc", default=None, help="the UTC date you stated it (YYYY-MM-DD); a statement older than the gate's freshness limit is refused")
    a = ap.parse_args(argv)
    import time as _t
    import ms_edge_gate as EG
    bases: List[Any] = []
    if not a.no_historical_cash:
        hb = CB.historical_basis(BOT)
        if hb is not None:
            bases.append(hb)
        else:
            print("NOTE: the repository's cash series is unreadable: no cash alternative is applied (UNKNOWN, never zero)")
    if any(x is not None for x in (a.owner_cash_apr, a.owner_cash_source, a.owner_cash_stated_utc)):
        stmt = {k: v for k, v in ((EG.ENV_CASH_APR, a.owner_cash_apr), (EG.ENV_CASH_SOURCE, a.owner_cash_source), (EG.ENV_CASH_STATED_UTC, a.owner_cash_stated_utc)) if v is not None}
        ob, info = CB.owner_basis(stmt, int(_t.time() * 1000))
        if ob is None:
            print(f"NOTE: your cost-of-capital statement is not usable ({info.get('state')}: {'; '.join(info.get('problems') or [])}): no OWNER_STATED comparison was made, and the historical series was NOT substituted for it")
        else:
            bases.append(ob)
    try:
        rep = run_competition(data_root=a.data_root, universe_dir=a.universe_dir, spot_dir=a.spot_dir, mark_interval=a.mark_interval, ledger_dir=a.ledger_dir, history_root=a.history_root,
                              unreserved_exploratory=a.unreserved_exploratory, cash_bases=bases)
    except RH.HarnessError as exc:                                   # a typed refusal (e.g. CANDIDATE_SPECS_NOT_SHIPPED), never a traceback
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(render_table(rep))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=2, sort_keys=True, default=str)
        print("wrote " + a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
