#!/usr/bin/env python3
"""The investment-committee view of the recorded Bybit evidence. Read-only, offline, order-less.

    python3 tools/profitability_report.py --dir /data/market [--history /data/monitor/monitor.jsonl] [--json]

Joins every layer of the evidence into one report and refuses to let a strong layer paper over a
weak one:

  1. VALIDATION   schema capacity/2, mixed/duplicate/regressed streams, quarantined legacy rows
  2. INTEGRITY    per-file hash chain, with the chain heads the monitor copied OUT of the evidence
                  directory used as external anchors
  3. RELIABILITY  cadence, gaps, staleness against NOW, supervisor restarts, disk
  4. INDEPENDENCE raw snapshots vs unique settlements vs effective sample size
  5. FUNDING      forecast vs the realised PUBLIC print, no forward fill
  6. ECONOMICS    per $100k hedged notional under ILLUSTRATIVE position inputs (not your account),
                  with the provisional 10/15/20 %/yr screens on economic capital
  7. OPPORTUNITIES the six-status evaluation of every registered hypothesis

No status authorises trading. The venue rules used for liquidation headroom (haircut, margin
rates) are ASSUMED and labelled so: they are not read from the exchange. The economics block uses the
LOWER confidence bound of realised funding, never the forecast.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import capacity_schema as CS      # noqa: E402
import evidence_chain as EC       # noqa: E402
import evidence_stats as ES       # noqa: E402
import funding_eval as FE         # noqa: E402
import opportunity_engine as OE   # noqa: E402
import recorder_reliability as RR # noqa: E402
import trade_economics as TE      # noqa: E402

ASSUMED_VENUE_RULES = {"collateral_haircut": 0.10, "initial_margin_rate": 0.10, "maintenance_margin_rate": 0.005,
                       "status": "ASSUMED_NOT_VENUE_VERIFIED"}


def _json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as fh:
            v = json.load(fh)
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def anchors_from_history(path: Optional[str]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    if not path:
        return out
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                f, h, n = r.get("recorder_chain_file"), r.get("recorder_chain_head"), r.get("recorder_chain_n")
                if isinstance(f, str) and isinstance(h, str) and isinstance(n, int):
                    out.setdefault(f, []).append({"n": n, "head": h})
    except OSError:
        pass
    return out


def build(directory: str, *, now_ms: int, history: Optional[str] = None, notional_usd: float = 100_000.0,
          hold_days: float = 30.0, artifacts_dir: Optional[str] = None) -> Dict[str, Any]:
    paths = sorted(p for p in glob.glob(os.path.join(directory, "capacity-*.jsonl")))
    anchors = anchors_from_history(history)
    files: List[Dict[str, Any]] = []
    all_rows: List[Any] = []
    chain_ok = True
    any_v2_file = False
    for p in paths:
        lines, quarantined = EC.read_lines(p)
        rows = EC.rows_of(lines)
        v2 = [r for r in rows if isinstance(r, dict) and r.get("schema") == CS.SCHEMA]
        ver = EC.verify(p, anchors=anchors.get(os.path.basename(p)))
        if v2:
            any_v2_file = True
            if not ver["tamper_evident_ok"]:
                chain_ok = False
        files.append({"file": os.path.basename(p), "rows": len(rows), "schema_v2_rows": len(v2),
                      "quarantined_lines": quarantined, "chain": {k: ver[k] for k in ("ok", "tamper_evident_ok", "n_chained", "unchained_tail", "head", "issues")},
                      "external_anchors": len(anchors.get(os.path.basename(p), []))})
        all_rows.extend(rows)
    val = CS.validate_stream(all_rows)
    accepted = val["accepted"]
    status = _json(os.path.join(directory, "recorder_status.json"))
    sup = _json(os.path.join(directory, "supervisor_status.json"))
    sizes = {os.path.basename(p)[9:17]: os.path.getsize(p) for p in paths}
    rel = RR.report(accepted, now_ms=now_ms, supervisor=sup, status=status, disk_bytes_by_day=sizes)
    indep = ES.independence_report(accepted) if accepted else None
    fe = FE.evaluate(accepted) if accepted else None

    # measured cost and fillability at the target size
    pts = [p for r in accepted for p in r["curve"] if float(p["notional_usd"]) == float(notional_usd)]
    measured = [p for p in pts if p["status"] == "MEASURED"]
    fillable = (len(measured) / len(pts)) if pts else None
    total_cost = ES.mean([p["total_round_trip_bps"] for p in measured]) if measured else None
    book_cost = ES.mean([p["book_round_trip_bps"] for p in measured]) if measured else None
    fee_prov = accepted[-1]["fees"]["source"] if accepted else None

    economics = None
    if fe and fe["realised_bps"]["ci"].get("status") == "OK" and total_cost is not None:
        f_lo = fe["realised_bps"]["ci"]["lo"]
        n_settle = max(1, int(round(hold_days * TE.SETTLEMENTS_PER_DAY)))
        pos = TE.Position(notional_usd=notional_usd, hold_days=hold_days, btc_inventory_usd=notional_usd, btc_was_owned=True,
                          margin_posted_usd=0.0, collateral_haircut=ASSUMED_VENUE_RULES["collateral_haircut"],
                          reserve_usd=0.1 * notional_usd, maintenance_margin_rate=ASSUMED_VENUE_RULES["maintenance_margin_rate"],
                          initial_margin_rate=ASSUMED_VENUE_RULES["initial_margin_rate"])
        perp_fee = accepted[-1]["fees"]["perp_taker_bps"]
        costs = TE.Costs(perp_entry_fee_bps=perp_fee, perp_exit_fee_bps=perp_fee, spread_bps=book_cost, slippage_bps=0.0,
                         impact_bps=0.0, latency_bps=None, fee_provenance="UNVERIFIED")
        economics = {"inputs": {"basis": "PER $%s HEDGED NOTIONAL; ILLUSTRATIVE POSITION, NOT YOUR ACCOUNT" % f"{notional_usd:,.0f}",
                                "hold_days": hold_days, "funding_used_bps_per_settlement": f_lo,
                                "funding_used_is": "LOWER 95% block-bootstrap bound of the realised public mean",
                                "venue_rules": ASSUMED_VENUE_RULES},
                     "result": TE.compute(pos, costs, [f_lo] * n_settle, funding_source="PUBLIC_REALISED")}
    bundle = {"carry": {"validation_ok": val["stream_ok"], "chain_ok": chain_ok and any_v2_file,
                        "reliability_verdict": rel["verdict"], "funding_eval": fe or {}, "economics": economics["result"] if economics else {},
                        "fillable_share": fillable, "round_trip_cost_bps": total_cost, "prospective": {}},
              "rows": accepted, "rows_by_symbol": {accepted[0]["symbol"]: accepted} if accepted else {},
              "slice57": _json(os.path.join(artifacts_dir or os.path.join(BOT, "artifacts"),
                                            "slice57_stage1_funding_carry_fade_btc_v1.json"))}
    return {
        "generated_at_ms": now_ms, "directory": os.path.basename(os.path.abspath(directory)),
        "validation": {k: val[k] for k in ("n_input", "n_valid_rows", "n_legacy_quarantined", "stream_ok", "problems", "note")},
        "integrity": {"files": files, "chain_ok_for_v2_files": chain_ok and any_v2_file},
        "reliability": rel, "independence": indep, "funding_evaluation": fe,
        "capacity": {"target_notional_usd": notional_usd, "book_fillable_snapshot_share": fillable,
                     "mean_total_round_trip_bps": total_cost, "mean_book_round_trip_bps": book_cost,
                     "fee_provenance": fee_prov,
                     "max_book_fillable_notional_usd": max((r.get("max_displayed_fillable_notional_usd") or 0.0 for r in accepted), default=None),
                     "wording": "'book-fillable snapshots' = snapshots in which the DISPLAYED book could fill the size; "
                                "not executable liquidity"},
        "economics": economics,
        "opportunities": OE.evaluate_all(bundle),
        "authorizes_trading": False,
    }


def _f(x: Any, nd: int = 2) -> str:
    return "n/a" if x is None else (f"{x:.{nd}f}" if isinstance(x, (int, float)) else str(x))


def render(rep: Dict[str, Any]) -> str:
    v, i, r = rep["validation"], rep["integrity"], rep["reliability"]
    out = ["PROFITABILITY REPORT - read-only, order-less; no status authorises trading", ""]
    out.append(f"1 VALIDATION  rows in {v['n_input']}, schema-v2 valid {v['n_valid_rows']}, legacy v1 quarantined "
               f"{v['n_legacy_quarantined']} (never used), stream ok: {v['stream_ok']}, problems: {len(v['problems'])}")
    for p in v["problems"][:5]:
        out.append(f"    ! {p['code']}: {p['detail']}")
    out.append("2 INTEGRITY   chain verifies for schema-v2 files: %s" % i["chain_ok_for_v2_files"])
    for f in i["files"]:
        iss = ",".join(x["code"] for x in f["chain"]["issues"]) or "none"
        q = f", QUARANTINED final line(s): {len(f['quarantined_lines'])}" if f["quarantined_lines"] else ""
        out.append(f"    {f['file']}: rows {f['rows']} (v2 {f['schema_v2_rows']}), issues: {iss}, external anchors {f['external_anchors']}{q}")
    out.append(f"3 RELIABILITY {r['verdict']}: {r.get('why', '')}")
    if r.get("rows"):
        out.append(f"    {r['rows']} rows over {_f(r['window_hours'])} h, completeness {_f(r['completeness'], 3)}, gaps {len(r['gaps'])}, "
                   f"last row {_f(r['last_row_age_s'] / 60.0, 1)} min old, supervisor restarts "
                   f"{(r['supervisor'] or {}).get('recorder_restarts', 'n/a')}")
    ind = rep["independence"]
    if ind:
        out.append(f"4 INDEPENDENCE raw snapshots {ind['raw_snapshots']}, unique settlements {ind['unique_settlements']}, "
                   f"independent events {ind['independent_events']}, effective sample size {_f(ind['effective_sample_size'], 1)}")
    fe = rep["funding_evaluation"]
    if fe:
        o = fe["by_offset"].get(30) or {}
        out.append(f"5 FUNDING     {fe['joined']} settlements joined forecast->realised public print "
                   f"(missing realised {fe['missing_realised']}, anomalies {len(fe['anomalies'])}); status {fe['status']} "
                   f"(need {fe['min_independent']})")
        if o.get("n"):
            out.append(f"    forecast 30 min before: bias {_f(o['bias_bps'], 3)} bps, MAE {_f(o['mae_bps'], 3)}, sign accuracy {_f(o['sign_accuracy'], 2)}")
    cap = rep["capacity"]
    out.append(f"    book-fillable snapshots at ${cap['target_notional_usd']:,.0f}: {_f(cap['book_fillable_snapshot_share'], 2)}, "
               f"round trip {_f(cap['mean_total_round_trip_bps'])} bps ({cap['fee_provenance']}), max displayed size "
               f"${_f(cap['max_book_fillable_notional_usd'], 0)}")
    e = rep["economics"]
    if e:
        res = e["result"]
        out.append(f"6 ECONOMICS   {e['inputs']['basis']}, hold {e['inputs']['hold_days']:g} d, funding = {_f(e['inputs']['funding_used_bps_per_settlement'], 3)} bps/settlement ({e['inputs']['funding_used_is']})")
        out.append(f"    net {_f(res['net_pnl_usd'])} USD; {_f(res['returns']['annualised_pct_on_economic_capital'])}%/yr on economic capital, "
                   f"{_f(res['returns']['annualised_pct_on_notional'])}%/yr on notional; unknown inputs: {', '.join(res['unknowns']) or 'none'}")
        out.append("    screens (" + TE.PROVISIONAL + "): " + ", ".join(f"{h['hurdle_apr_pct']:g}% -> {'meets' if h['meets'] else 'misses'}" for h in res["hurdles"]))
    else:
        out.append("6 ECONOMICS   not computed: no confidence interval on realised funding yet (insufficient joined settlements)")
    out.append("7 OPPORTUNITIES")
    for o in rep["opportunities"]:
        out.append(f"    {o['strategy']:<34} {o['origin']:<26} {o['status'] or 'FROZEN_ABSENT (never re-evaluated)'}")
        if o["status"] and o["reasons"]:
            out.append(f"        - {o['reasons'][0][:150]}")
    return "\n".join(out)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", required=True)
    ap.add_argument("--history", default=None, help="monitor.jsonl: its copied chain heads are external anchors")
    ap.add_argument("--notional", type=float, default=100_000.0)
    ap.add_argument("--hold-days", type=float, default=30.0)
    ap.add_argument("--now-ms", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    now = a.now_ms if a.now_ms is not None else int(time.time() * 1000)
    rep = build(a.dir, now_ms=now, history=a.history, notional_usd=a.notional, hold_days=a.hold_days)
    print(json.dumps(rep, indent=2, sort_keys=True, default=str) if a.json else render(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
