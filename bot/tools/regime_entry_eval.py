#!/usr/bin/env python3
"""Pre-registered evaluator: funding-regime-conditioned spot+perp carry (``funding_regime_conditioned_entry``).

    python3 tools/regime_entry_eval.py --history-root R --universe-dir U [--spot-dir S] [--mark-interval 240] [--ledger-dir L] [--json OUT]

Offline, read-only, no clock, no network, no credential, no order. ONE hypothesis, fixed in ``REGISTRATION`` below (its digest is printed and recorded with the result) BEFORE this code
reads any returns. It is a single trial in the persistent multiple-testing ledger, not a search: nothing here is a parameter grid, and changing a registered number is a different
registration (a new id, a new trial, a larger family).

THE HYPOTHESIS. Plain spot+perp carry on a fixed weekly hold earns less than its round trip (the competition's SPOT_PERP_CARRY family: mean -7.2 bps per 7-day row after costs). The round
trip is paid once per hold, so (a) a longer hold amortises it and (b) entering only while the funding REGIME is rich and persistent avoids paying it for nothing. The regime state at
decision time t uses ONLY prints with settlement/observation time <= t:
    ON  <=>  trailing ``formation_days`` mean funding >= ``min_mean_funding_bps_per_day`` (each print normalised by the interval of ITS OWN segment)  AND
             at least ``min_positive_print_share`` of those prints are positive  AND  at least ``min_prints`` prints exist.
Entry: every hedgeable symbol (a Bybit spot pair in the CURRENT snapshot) whose state is ON at a decision on the non-overlapping ``hold_days`` grid, equal weight. Exit: the end of the hold
(or the contract's deliveryTime), no discretion. Primary metric per row: mean over legs of (funding earned - spot+perp taker round trip) in bps of notional. The level 2.0 bps/day is
ARITHMETIC ON COSTS, not a fitted number: a 30-day hold must repay 31.0 bps of round trip = 1.03 bps/day, and the registration asks for twice that. It was fixed before this module read any
return; but the same capture window was already seen by other families, so this is NOT a clean hold-out and the result is labelled accordingly.

WHAT IT CANNOT DO. The verdict is capped at INSUFFICIENT_DATA: a pass needs prospective evidence and the owner's attestation. With ``hold_days=30`` a 457-day capture yields about 15
non-overlapping rows, far below the 50 the repository requires for an out-of-sample claim, so today this reports a DESCRIPTIVE in-sample result and says so. That is the point: it tells you
whether conditioning is worth collecting prospective evidence for, not whether to trade.

THE BASIS RESIDUAL. A hedged pair's price legs cancel only to first order. If an ``index-kline-<interval>`` dataset is present and verifies, the residual
(-(mark return) on the short perp + (index return) on the long spot proxy) is MEASURED per leg and reported next to the primary metric; it is never folded into the registered metric (so the
ledger series is identical with or without it). The index price is Bybit's multi-exchange index, a PROXY for the Bybit spot pair, not that pair's own price: the residual includes the
index-vs-spot tracking error. With no index dataset the residual is UNMEASURED.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import candidate_readiness as CR  # noqa: E402
import carry_costs as CC  # noqa: E402
import market_history as MH  # noqa: E402
import research_harness as RH  # noqa: E402
import statistical_power as SP  # noqa: E402
import strategy_verdict as SV  # noqa: E402

SCHEMA = "regime_entry_eval/v1"
FAMILY = "funding_regime_conditioned_entry"
MS_DAY = CR.MS_DAY
ALPHA = 0.05

REGISTRATION: Dict[str, Any] = {
    "registration_id": "funding_regime_conditioned_entry@reg-1",
    "hypothesis": ("conditioning spot+perp carry entry on a past-only funding regime state, with a hold long enough to amortise the round trip, earns a positive net edge after "
                   "the spot+perp taker round trip that the unconditional weekly carry does not"),
    "universe": "perps with a Bybit spot pair in the CURRENT spot snapshot (HEDGEABLE_ON_BYBIT), listing age >= 30 days, active at the decision time",
    "regime_state": {"formation_days": 7, "min_mean_funding_bps_per_day": 2.0, "min_positive_print_share": 0.8, "min_prints": 3},
    "level_rationale": "about twice the daily amortisation of the 31.4 bps spot+perp taker round trip (31.0 fees + 0.4 assumed spread) over the registered 30-day hold (31.4 / 30 = 1.05 bps/day): arithmetic on costs, not a fitted value",
    "entry": "every symbol whose regime state is ON at a decision time on the registered grid, equal weight",
    "hold_days": 30,
    "grid": "non-overlapping hold_days grid, candidate_readiness.holding_grid anchored at the first UTC day boundary >= first print + formation_days",
    "exit": "the end of the hold, or the contract's deliveryTime if earlier; no discretion",
    "min_listing_age_days": 30,
    "cost_scenario": {"style": "taker", "position_class": "acquire_spot_perp", "round_trip_bps": None},      # filled from carry_costs below (authoritative)
    "primary_metric": "mean over a row's legs of (funding earned - round trip), bps of notional per row; the basis residual is reported separately and never enters it",
    "min_rows_for_a_test": SV.MIN_OBSERVATIONS["oos_test"],
    "alpha": ALPHA,
    "variants": 1,
    "verdict_ceiling": "INSUFFICIENT_DATA",
    "disclosures": [
        "not a clean hold-out: the capture window was already used by the SPOT_PERP_CARRY / PERP_* families",
        "SURVIVORS_ONLY unless the universe capture observed delistings",
        "spot hedgeability is a CURRENT snapshot, never projected backwards",
        "execution is a TAKER assumption (spread ASSUMED); no depth/fill evidence",
    ],
}


def _round_trip() -> float:
    return float(CC.position_class_costs("acquire_spot_perp", "taker")["total_bps"])


def registration() -> Dict[str, Any]:
    reg = json.loads(json.dumps(REGISTRATION))
    reg["cost_scenario"]["round_trip_bps"] = _round_trip()
    return reg


def registration_digest(reg: Optional[Mapping[str, Any]] = None) -> str:
    reg = reg if reg is not None else registration()
    return hashlib.sha256(json.dumps(reg, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# ---------------------------------------------------------------- the regime state (past-only)
def regime_state(s: CR.SymbolSeries, t: int, p: Mapping[str, Any]) -> Tuple[Optional[bool], Dict[str, Any]]:
    """(ON/OFF/None, diagnostics) at decision time ``t`` from prints with key <= t ONLY. None = unknown (no interval for a print, too few prints): never ON."""
    f_ms = int(p["formation_days"]) * MS_DAY
    per_day: List[float] = []
    positive = 0
    for ts, v in s.funding:
        if ts > t:
            break
        if ts > t - f_ms:
            iv = CR._interval_at(s, ts)
            if iv is None or iv <= 0:
                return None, {"why": "interval_unknown_for_a_print"}
            per_day.append(v * 1440.0 / iv)
            positive += 1 if v > 0 else 0
    n = len(per_day)
    if n < int(p["min_prints"]):
        return None, {"why": "too_few_prints", "n_prints": n}
    mean = statistics.fmean(per_day)
    share = positive / n
    on = mean >= float(p["min_mean_funding_bps_per_day"]) and share >= float(p["min_positive_print_share"])
    return on, {"n_prints": n, "mean_bps_per_day": mean, "positive_share": share}


# ---------------------------------------------------------------- optional index series (basis residual)
def load_index_series(root: str, interval: str, symbols: Sequence[str]) -> Tuple[Dict[str, List[Tuple[int, float]]], Dict[str, Any]]:
    """The verified ``index-kline-<interval>`` closes keyed by OBSERVATION time (candle start + interval), or ({}, reason) when the dataset is absent / does not verify."""
    name = MH.dataset_dirname(MH.INDEX, interval)
    if not os.path.isdir(os.path.join(root, name)):
        return {}, {"status": "UNMEASURED", "reason": f"no {name} dataset under the history root (collect it with market_history run-plan --datasets all)"}
    try:
        man, ddir = CR._verified_dataset(root, name)
    except RH.HarnessError as exc:
        return {}, {"status": "UNMEASURED", "reason": f"{name} does not verify: {str(exc)[:200]}"}
    iv_ms = MH.KLINE_INTERVALS_MS.get(str(interval))
    out: Dict[str, List[Tuple[int, float]]] = {}
    for sym in symbols:
        st = (man.get("symbols") or {}).get(sym) or {}
        if st.get("status") != "COMPLETE":
            continue
        try:
            rows = CR._rows(ddir, sym)
            pts = []
            for r in rows:
                ot = r.get("observation_time")
                if not isinstance(ot, int) or isinstance(ot, bool) or (iv_ms and ot < r["ts"] + iv_ms):
                    raise RH.LookAheadError(f"{sym}: an index row claims its close is knowable before the candle ended")
                pts.append((ot, float(r["v"]["close"])))
            out[sym] = sorted(pts)
        except RH.LookAheadError:
            raise
        except (KeyError, ValueError, TypeError, OSError):
            continue
    return out, {"status": "MEASURED" if out else "UNMEASURED", "dataset": name, "symbols": len(out), "manifest_digest": man.get("manifest_digest"),
                 "proxy_note": "Bybit's multi-exchange INDEX price stands in for the Bybit spot pair's own price: the residual includes the index-vs-spot tracking error"}


# ---------------------------------------------------------------- the evaluation
def run_registered(series: Sequence[CR.SymbolSeries], *, reg: Optional[Mapping[str, Any]] = None, index: Optional[Mapping[str, Sequence[Tuple[int, float]]]] = None) -> Dict[str, Any]:
    reg = reg or registration()
    p = reg["regime_state"]
    h_ms = int(reg["hold_days"]) * MS_DAY
    f_ms = int(p["formation_days"]) * MS_DAY
    rt = float(reg["cost_scenario"]["round_trip_bps"])
    index = index or {}
    t0 = min((s.funding[0][0] for s in series if s.funding), default=None)
    t1 = max((s.funding[-1][0] for s in series if s.funding), default=None)
    if t0 is None:
        return {"slots": [], "rows": [], "empty_slots": 0, "legs": [], "skipped": {}, "excl": {}, "unpriced": []}
    slots = CR.holding_grid(t0, t1, formation_days=int(p["formation_days"]), hold_days=int(reg["hold_days"]))
    rows: List[Dict[str, Any]] = []
    legs_all: List[Dict[str, Any]] = []
    excl: Dict[str, int] = {}
    skipped: Dict[str, int] = {}
    unpriced: List[Dict[str, Any]] = []
    empty = 0
    for t in slots:
        legs: List[Dict[str, Any]] = []
        for s in sorted(series, key=lambda x: x.symbol):
            why = CR.eligibility(s, t, CR.FAMILY_CARRY, f_ms, int(reg["min_listing_age_days"]))
            if why is None:
                on, diag = regime_state(s, t, p)
                if on is None:
                    why = "state_unknown"
                elif not on:
                    why = "regime_off"
            if why is not None:
                excl[why] = excl.get(why, 0) + 1
                continue
            w = CR._leg_window(s, t, h_ms, None)
            if w["m0"] is None or w["m1"] is None or w["m0"] <= 0:
                skipped["leg_unpriced"] = skipped.get("leg_unpriced", 0) + 1
                if w["terminal"]:
                    unpriced.append({"symbol": s.symbol, "t": t, "delivery_ms": s.delivery_ms})
                continue
            leg: Dict[str, Any] = {"symbol": s.symbol, "t": t, "interval_min": s.interval_min, "funding_bps": w["funding_raw_bps"], "prints": w["prints"], "cost_bps": rt,
                                   "net_bps": w["funding_raw_bps"] - rt, "closed_during_hold": w["terminal"], "state_at_entry": diag}
            persists = []
            for k in (1, 2, 3):                                              # ex-post persistence diagnostic only: states at later checkpoints use prints known THEN
                tc = t + k * 7 * MS_DAY
                if tc < w["end"]:
                    st, _ = regime_state(s, tc, p)
                    persists.append(st)
            leg["state_off_at_checkpoints"] = sum(1 for x in persists if x is False)
            leg["checkpoints"] = len(persists)
            idx = index.get(s.symbol)
            if idx:
                bound = CR.staleness_bound_ms(s, None)
                i0, i1 = CR.price_at(idx, t, bound), CR.price_at(idx, w["end"], bound)
                if i0 and i1 and i0 > 0:
                    leg["basis_residual_bps"] = -(w["m1"] / w["m0"] - 1.0) * 1e4 + (i1 / i0 - 1.0) * 1e4
            legs.append(leg)
        if not legs:
            empty += 1
            continue
        legs_all.extend(legs)
        row: Dict[str, Any] = {"t": t, "n_legs": len(legs), "funding_bps": statistics.fmean(x["funding_bps"] for x in legs), "cost_bps": rt,
                               "net_bps": statistics.fmean(x["net_bps"] for x in legs)}
        res = [x["basis_residual_bps"] for x in legs if "basis_residual_bps" in x]
        row["basis_residual_bps"] = statistics.fmean(res) if res else None
        row["basis_legs_measured"] = len(res)
        rows.append(row)
    return {"slots": slots, "rows": rows, "empty_slots": empty, "legs": legs_all, "skipped": skipped, "excl": dict(sorted(excl.items())), "unpriced": unpriced}


def _drawdown(xs: Sequence[float]) -> float:
    cum = peak = mdd = 0.0
    for x in xs:
        cum += x
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return mdd


def descriptive(res: Mapping[str, Any]) -> Dict[str, Any]:
    rows, legs = res["rows"], res["legs"]
    net = [r["net_bps"] for r in rows]
    out: Dict[str, Any] = {"label": "IN_SAMPLE_DESCRIPTIVE_NOT_A_TEST", "rows": len(rows), "slots": len(res["slots"]), "empty_slots_no_symbol_on": res["empty_slots"],
                           "coverage_rows_over_slots": (len(rows) / len(res["slots"])) if res["slots"] else None, "legs": len(legs)}
    if net:
        out.update({"row_mean_net_bps": statistics.fmean(net), "row_median_net_bps": statistics.median(net), "row_sd_net_bps": statistics.stdev(net) if len(net) > 1 else None,
                    "row_worst_net_bps": min(net), "row_best_net_bps": max(net), "rows_positive_share": sum(1 for x in net if x > 0) / len(net), "max_drawdown_bps": _drawdown(net),
                    "mean_legs_per_row": statistics.fmean(r["n_legs"] for r in rows)})
        ln = [x["net_bps"] for x in legs]
        out.update({"leg_mean_net_bps": statistics.fmean(ln), "leg_median_net_bps": statistics.median(ln), "leg_positive_share": sum(1 for x in ln if x > 0) / len(ln)})
        by_sym: Dict[str, float] = {}
        for x in legs:
            by_sym[x["symbol"]] = by_sym.get(x["symbol"], 0.0) + x["net_bps"]
        pos = sorted((v for v in by_sym.values() if v > 0), reverse=True)
        tot = sum(pos)
        out["concentration_top5_symbols_share_of_positive_net"] = (sum(pos[:5]) / tot) if tot > 0 else None
        out["symbols_ever_entered"] = len(by_sym)
        by_iv: Dict[str, List[float]] = {}
        for x in legs:
            by_iv.setdefault(str(x["interval_min"]), []).append(x["net_bps"])
        out["by_funding_interval_min"] = {k: {"legs": len(v), "mean_net_bps": statistics.fmean(v)} for k, v in sorted(by_iv.items())}
        chk = [x for x in legs if x["checkpoints"]]
        out["regime_persistence"] = {"legs_with_checkpoints": len(chk), "share_with_any_checkpoint_off": (sum(1 for x in chk if x["state_off_at_checkpoints"] > 0) / len(chk)) if chk else None,
                                     "note": "ex-post diagnostic: how often the entry condition was already false at a later weekly checkpoint (the registered exit is still the fixed hold)"}
        bas = [r["basis_residual_bps"] for r in rows if r["basis_residual_bps"] is not None]
        out["basis_residual"] = ({"status": "MEASURED_PROXY", "rows": len(bas), "mean_bps": statistics.fmean(bas), "sd_bps": statistics.stdev(bas) if len(bas) > 1 else None,
                                  "worst_row_bps": min(bas), "row_mean_net_after_basis_bps": statistics.fmean(r["net_bps"] + r["basis_residual_bps"] for r in rows if r["basis_residual_bps"] is not None)}
                                 if bas else {"status": "UNMEASURED", "note": "no verified index-kline dataset"})
    return out


def evaluate(series: Sequence[CR.SymbolSeries], *, ledger_path: str, universe_info: Optional[Mapping[str, Any]] = None, index: Optional[Mapping[str, Sequence[Tuple[int, float]]]] = None,
             reg: Optional[Mapping[str, Any]] = None, allow_unverified_key_basis_for_tests: bool = False) -> Dict[str, Any]:
    if not isinstance(ledger_path, str) or not ledger_path:
        raise RH.HarnessError("LEDGER_PATH_REQUIRED: the family count must survive a restart")
    unkeyed = sorted(s.symbol for s in series if s.key_basis is not CR._KEY_BASIS_OBSERVED)
    if unkeyed and allow_unverified_key_basis_for_tests is not True:
        raise RH.HarnessError(f"{len(unkeyed)} series lack the loader's key_basis token: marks/funding keyed by candle start would leak the future; first: {unkeyed[:3]}")
    if os.path.realpath(ledger_path).startswith(os.path.join(BOT, "state") + os.sep):
        raise RH.HarnessError("the ledger may not live under bot/state")
    os.makedirs(os.path.dirname(os.path.abspath(ledger_path)), exist_ok=True)
    reg = reg or registration()
    digest = registration_digest(reg)
    survivorship = CR._survivorship_of(universe_info)
    warnings: List[str] = []
    if survivorship == CR.SURVIVORS_ONLY:
        warnings.append("SURVIVORS_ONLY: no survivorship-free claim")
    out: Dict[str, Any] = {"schema": SCHEMA, "family": FAMILY, "registration": reg, "registration_digest": digest, "survivorship": survivorship, "survivorship_free_claim": False,
                           "warnings": warnings, "ceiling": "INSUFFICIENT_DATA", "symbols_in_series": len(series), "time_mode": "REPLAY_OBSERVATION_TIME",
                           "multiple_testing_scope": "PERSISTENT_LEDGER_FILE", "ledger_path": os.path.abspath(ledger_path),
                           "not_a_clean_holdout": "the capture window was already used by other families: thresholds were fixed from cost arithmetic before this module read a return, but the data is not untouched"}
    if any(s.interval_min is None for s in series):
        out.update({"verdict": "INSUFFICIENT_DATA", "status": "FUNDING_NORMALISATION_UNKNOWN", "reason": "a symbol carries no fundingInterval"})
        return out
    res = run_registered(series, reg=reg, index=index)
    rows = res["rows"]
    n = len(rows)
    min_rows = int(reg["min_rows_for_a_test"])
    out["hedgeable_symbols"] = sum(1 for s in series if s.spot_listed is True)
    out["eligibility_exclusions_by_reason"] = res["excl"]
    out["skipped_legs_by_reason"] = res["skipped"]
    out["terminal_exit_unpriced"] = len(res["unpriced"])
    if res["unpriced"]:
        warnings.append(f"TERMINAL_EXIT_UNPRICED: {len(res['unpriced'])} delisted legs have no price at delivery: capped at INSUFFICIENT_DATA")
    if out["hedgeable_symbols"] == 0:
        warnings.append("NO_HEDGEABLE_SYMBOLS: pass --spot-dir; without a spot snapshot hedgeability is unknown and nothing is eligible")
    out["rows_detail"] = rows
    out["descriptive"] = descriptive(res)
    ledger = RH.VariantLedger(FAMILY, path=ledger_path)
    name = f"{reg['registration_id']}|{reg['cost_scenario']['style']}"
    params = {"registration_digest": digest, "hold_days": reg["hold_days"], "regime_state": reg["regime_state"], "round_trip_bps": reg["cost_scenario"]["round_trip_bps"]}
    net = [r["net_bps"] for r in rows]
    if n < min_rows:
        if not ledger.knows(name):                         # an identical re-run is the same trial, not a new one
            ledger.record_rejected(name, params, f"only {n} non-overlapping {reg['hold_days']}-day rows (< {min_rows})")
        out.update({"status": "TOO_FEW_HOLDING_ROWS", "verdict": "INSUFFICIENT_DATA", "n_variants_counted": ledger.n_variants, "rows": n,
                    "reason": (f"{n} non-overlapping {reg['hold_days']}-day rows; {min_rows} are required for an out-of-sample claim. A {reg['hold_days']}-day hold needs "
                               f"~{min_rows * int(reg['hold_days'])} days of history; the result below is DESCRIPTIVE ONLY")})
        return out
    if RH.series_digest(net) not in ledger.returns_digests(name):
        ledger.record(name, params, net, horizon=1)
    st = RH.significance(net)
    m = ledger.n_variants
    adj = SP.adjust_pvalues([st["p_one_sided"]], "holm", alpha=ALPHA, n_tested=m)["adjusted"][0]
    enough = st["independent_observations"] >= min_rows
    verdict = "REJECTED" if enough and (st["mean"] is None or st["mean"] <= 0 or adj > ALPHA) else "INSUFFICIENT_DATA"
    if res["unpriced"]:
        verdict = "INSUFFICIENT_DATA"
    out.update({"status": "TEST_DONE", "verdict": verdict, "n_variants_counted": m, "rows": n, "mean_net_bps": st["mean"], "independent_observations": st["independent_observations"],
                "p_one_sided": st["p_one_sided"], "p_adjusted": adj, "ci90": st.get("ci"), "label": "HISTORICAL_TEST_ONE_REGIME_NOT_PROSPECTIVE"})
    return out


def run_on_root(history_root: str, *, universe_dir: str, ledger_path: str, spot_dir: Optional[str] = None, mark_interval: Optional[str] = None) -> Dict[str, Any]:
    series, info = CR.load_history_root(history_root, universe_dir=universe_dir, spot_dir=spot_dir, mark_interval=mark_interval)
    index, idx_info = load_index_series(history_root, str(info["mark_interval"]), [s.symbol for s in series])
    out = evaluate(series, ledger_path=ledger_path, universe_info=info, index=index)
    out["basis_measurement"] = idx_info
    out["input"] = {k: info.get(k) for k in ("root", "mark_interval", "history_window", "history_coverage", "datasets", "universe", "survivorship", "hedge", "non_complete_symbols",
                                             "symbols_loaded", "warnings")}
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="pre-registered funding-regime-conditioned carry evaluator (offline, read-only, one trial)")
    ap.add_argument("--history-root", required=True)
    ap.add_argument("--universe-dir", required=True)
    ap.add_argument("--spot-dir", default=None)
    ap.add_argument("--mark-interval", default=None)
    ap.add_argument("--ledger-dir", default=None)
    ap.add_argument("--json", default=None, help="write the full report here")
    ap.add_argument("--print-registration", action="store_true", help="print the frozen registration and its digest, then exit")
    a = ap.parse_args(argv)
    if a.print_registration:
        print(json.dumps({"registration": registration(), "digest": registration_digest()}, indent=1, sort_keys=True))
        return 0
    lpath = CR.default_ledger_path(FAMILY, a.history_root, a.ledger_dir)
    try:
        out = run_on_root(a.history_root, universe_dir=a.universe_dir, ledger_path=lpath, spot_dir=a.spot_dir, mark_interval=a.mark_interval)
    except RH.HarnessError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1, sort_keys=True, default=str)
    d = out["descriptive"]
    print(f"{out['registration']['registration_id']}  digest {out['registration_digest'][:16]}  verdict {out['verdict']}  status {out['status']}")
    print(f"rows {d['rows']}/{d['slots']} slots (empty {d['empty_slots_no_symbol_on']}), legs {d['legs']}, symbols entered {d.get('symbols_ever_entered')}, hedgeable {out['hedgeable_symbols']}")
    if d.get("rows"):
        print(f"row net bps: mean {d['row_mean_net_bps']:.1f}  median {d['row_median_net_bps']:.1f}  worst {d['row_worst_net_bps']:.1f}  positive {d['rows_positive_share']:.0%}  "
              f"max drawdown {d['max_drawdown_bps']:.1f}; top-5 symbols hold {d['concentration_top5_symbols_share_of_positive_net']}")
        print("basis residual:", json.dumps(d.get("basis_residual")))
    print("reason:", out.get("reason"))
    print("warnings:", "; ".join(out["warnings"]) or "none")
    print("NOT a trade recommendation: capped at INSUFFICIENT_DATA; descriptive in-sample unless the row count reaches the registered minimum.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
