#!/usr/bin/env python3
"""The investment-committee view of the recorded Bybit evidence. Read-only, offline, order-less.

    python3 tools/profitability_report.py --dir /data/market [--history /data/monitor/monitor.jsonl] [--json]

Joins every layer of the evidence into one report and refuses to let a strong layer paper over a
weak one:

  1. VALIDATION   schema capacity/2, mixed/duplicate/regressed streams, quarantined legacy rows
  2. INTEGRITY    per-file hash chain, with the chain heads the monitor copied out of the evidence
                  directory used as anchors (a second copy on the same volume; archive monitor.jsonl
                  off the machine for them to be truly external). Every file counts, not just those
                  that still hold rows
  3. RELIABILITY  cadence, gaps, staleness against NOW, supervisor restarts, disk
  4. INDEPENDENCE raw snapshots vs unique settlements vs effective sample size
  5. FUNDING      forecast vs the realised PUBLIC print, no forward fill
  6. ECONOMICS    per $100k hedged notional under ILLUSTRATIVE position inputs (not your account),
                  with the provisional 10/15/20 %/yr screens on economic capital
  7. OPPORTUNITIES the six-status evaluation of every registered hypothesis

No status authorises trading. The venue rules used for liquidation headroom (haircut, margin
rates) are ASSUMED and labelled so: they are not read from the exchange. The economics block uses the
LOWER confidence bound of realised funding, never the forecast, and is WITHHELD (not printed with a
caveat) unless the evidence is valid, the chain verifies for EVERY file, the recorder is HEALTHY, the
evidence is mainnet, and the funding evaluation passes both its count and its effective-sample-size
floors. A number the gates have not cleared is not shown.
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
        if not ver["tamper_evident_ok"]:            # EVERY file counts, including one that lost all its rows
            chain_ok = False
        files.append({"file": os.path.basename(p), "rows": len(rows), "schema_v2_rows": len(v2),
                      "quarantined_lines": quarantined,
                      "chain": {k: ver[k] for k in ("ok", "tamper_evident_ok", "n_chained", "unchained_tail", "head", "issues",
                                                    "resealed_rows", "anchored")},
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

    identity = {"environments": sorted({r["environment"] for r in accepted}), "symbols": sorted({r["symbol"] for r in accepted}),
                "modes": sorted({r["mode"] for r in accepted}), "venues": sorted({r["venue"] for r in accepted}),
                "categories": sorted({r["category"] for r in accepted})}
    env = identity["environments"][0] if len(identity["environments"]) == 1 else None
    withheld: List[str] = []
    if not val["stream_ok"]:
        withheld.append("the evidence stream failed validation")
    if not (chain_ok and any_v2_file):
        withheld.append("the chain does not verify for every file")
    if rel["verdict"] != "HEALTHY":
        withheld.append(f"the recorder is {rel['verdict']}")
    if env != "mainnet":
        withheld.append(f"the evidence environment is {env or identity['environments'] or 'absent'}, not mainnet")
    if not fe:
        withheld.append("no funding evaluation")
    else:
        if fe["status"] != "OK":
            withheld.extend(fe.get("status_reasons") or ["the funding evaluation is not OK"])
        if fe["realised_bps"]["ci"].get("status") != "OK":
            withheld.append(f"no confidence interval on realised funding ({fe['realised_bps']['ci'].get('status')})")
    if total_cost is None:
        withheld.append("no measured round-trip cost at the target size")

    economics = None
    if not withheld:
        f_lo = fe["realised_bps"]["ci"]["lo"]
        n_settle = max(1, int(round(hold_days * TE.SETTLEMENTS_PER_DAY)))
        pos = TE.Position(notional_usd=notional_usd, hold_days=hold_days, btc_inventory_usd=notional_usd, btc_was_owned=True,
                          margin_posted_usd=0.0, collateral_haircut=ASSUMED_VENUE_RULES["collateral_haircut"],
                          reserve_usd=0.1 * notional_usd, maintenance_margin_rate=ASSUMED_VENUE_RULES["maintenance_margin_rate"],
                          initial_margin_rate=ASSUMED_VENUE_RULES["initial_margin_rate"])
        perp_fee = accepted[-1]["fees"]["perp_taker_bps"]
        spread_mean = ES.mean([p["spread_bps"] for p in measured])
        slip_mean = ES.mean([p["slippage_bps"] for p in measured])
        # measured walk of the DISPLAYED book only: market impact beyond it and latency are UNKNOWN, not zero
        costs = TE.Costs(perp_entry_fee_bps=perp_fee, perp_exit_fee_bps=perp_fee, spread_bps=spread_mean, slippage_bps=slip_mean,
                         impact_bps=None, latency_bps=None, fee_provenance="UNVERIFIED")
        economics = {"inputs": {"basis": "PER $%s HEDGED NOTIONAL; ILLUSTRATIVE POSITION, NOT YOUR ACCOUNT" % f"{notional_usd:,.0f}",
                                "hold_days": hold_days, "funding_used_bps_per_settlement": f_lo,
                                "funding_used_is": "LOWER 95% block-bootstrap bound of the realised public mean",
                                "venue_rules": ASSUMED_VENUE_RULES},
                     "result": TE.compute(pos, costs, [f_lo] * n_settle, funding_source="PUBLIC_REALISED")}
    bundle = {"carry": {"validation_ok": val["stream_ok"], "chain_ok": chain_ok and any_v2_file, "environment": env,
                        "reliability_verdict": rel["verdict"], "funding_eval": fe or {}, "economics": economics["result"] if economics else {},
                        "fillable_share": fillable, "round_trip_cost_bps": total_cost, "prospective": {}},
              "rows": accepted, "rows_by_symbol": {accepted[0]["symbol"]: accepted} if accepted else {},
              "slice57": _json(os.path.join(artifacts_dir or os.path.join(BOT, "artifacts"),
                                            "slice57_stage1_funding_carry_fade_btc_v1.json"))}
    return {
        "generated_at_ms": now_ms, "directory": os.path.basename(os.path.abspath(directory)),
        "evidence_identity": identity,
        "validation": {k: val[k] for k in ("n_input", "n_valid_rows", "n_rejected", "rejected_share", "n_legacy_quarantined",
                                           "stream_ok", "problems", "note")},
        "integrity": {"files": files, "chain_ok_for_v2_files": chain_ok and any_v2_file,
                      "anchored": bool(files) and all(f["external_anchors"] > 0 for f in files),
                      "anchor_note": "anchors are copies of the chain head in monitor.jsonl on the SAME volume; they are "
                                     "external to the evidence directory only until the owner archives them off the machine"},
        "reliability": rel, "independence": indep, "funding_evaluation": fe,
        "capacity": {"target_notional_usd": notional_usd, "book_fillable_snapshot_share": fillable,
                     "mean_total_round_trip_bps": total_cost, "mean_book_round_trip_bps": book_cost,
                     "fee_provenance": fee_prov,
                     "max_book_fillable_notional_usd": max((r.get("max_displayed_fillable_notional_usd") or 0.0 for r in accepted), default=None),
                     "wording": "'book-fillable snapshots' = snapshots in which the DISPLAYED book could fill the size; "
                                "not executable liquidity"},
        "economics": economics, "economics_withheld_because": withheld,
        "opportunities": OE.evaluate_all(bundle),
        "authorizes_trading": False,
    }


def _f(x: Any, nd: int = 2) -> str:
    return "n/a" if x is None else (f"{x:.{nd}f}" if isinstance(x, (int, float)) else str(x))


def render(rep: Dict[str, Any]) -> str:
    v, i, r = rep["validation"], rep["integrity"], rep["reliability"]
    idn = rep["evidence_identity"]
    out = ["PROFITABILITY REPORT - read-only, order-less; no status authorises trading",
           "EVIDENCE      venue %s, category %s, environment %s, symbol %s, mode %s" % tuple(
               ",".join(idn[k]) or "none" for k in ("venues", "categories", "environments", "symbols", "modes")), ""]
    out.append(f"1 VALIDATION  rows in {v['n_input']}, schema-v2 valid {v['n_valid_rows']}, REJECTED {v['n_rejected']} "
               f"({v['rejected_share']:.1%}), legacy v1 quarantined {v['n_legacy_quarantined']} (never used), "
               f"stream ok: {v['stream_ok']}, problems: {len(v['problems'])}")
    for p in v["problems"][:5]:
        out.append(f"    ! {p['code']}: {p['detail']}")
    out.append("2 INTEGRITY   chain verifies for EVERY file: %s; %s" % (
        i["chain_ok_for_v2_files"], "ANCHORED to monitor history" if i["anchored"] else
        "UNANCHORED (a consistent truncation of the newest rows would not be detected)"))
    for f in i["files"]:
        iss = ",".join(x["code"] for x in f["chain"]["issues"]) or "none"
        q = f", QUARANTINED final line(s): {len(f['quarantined_lines'])}" if f["quarantined_lines"] else ""
        rs = f", RESEALED rows adopted after a crash: {f['chain']['resealed_rows']}" if f["chain"].get("resealed_rows") else ""
        out.append(f"    {f['file']}: rows {f['rows']} (v2 {f['schema_v2_rows']}), issues: {iss}, external anchors {f['external_anchors']}{q}{rs}")
    out.append(f"3 RELIABILITY {r['verdict']}: {r.get('why', '')}")
    if r.get("rows"):
        out.append(f"    {r['rows']} rows over {_f(r['window_hours'])} h, completeness {_f(r['completeness'], 3)}, gaps {len(r['gaps'])}, "
                   f"last row {_f(r['last_row_age_s'] / 60.0, 1)} min old, supervisor restarts "
                   f"{(r['supervisor'] or {}).get('recorder_restarts', 'n/a')}")
    ind = rep["independence"]
    if ind:
        out.append(f"4 DEPENDENCE  raw snapshots {ind['raw_snapshots']}, unique settlements {ind['unique_settlements']} "
                   f"(serially dependent), effective sample size of the forecast series {_f(ind['effective_sample_size'], 1)}")
    fe = rep["funding_evaluation"]
    if fe:
        o = fe["by_offset"].get(30) or {}
        out.append(f"5 FUNDING     {fe['joined']} settlements joined forecast->realised public print "
                   f"(missing realised {fe['missing_realised']}, anomalies {len(fe['anomalies'])}); status {fe['status']} "
                   f"(needs {fe['min_independent']} joined AND effective sample size >= {fe['min_ess']:g}; realised ESS "
                   f"{_f(fe['realised_bps'].get('ess'), 1)})")
        for why in fe.get("status_reasons") or []:
            out.append(f"    ! {why}")
        if o.get("n"):
            out.append(f"    forecast 30 min before: bias {_f(o['bias_bps'], 3)} bps, MAE {_f(o['mae_bps'], 3)}, sign accuracy {_f(o['sign_accuracy'], 2)}")
    cap = rep["capacity"]
    out.append(f"    book-fillable snapshots at ${cap['target_notional_usd']:,.0f}: {_f(cap['book_fillable_snapshot_share'], 2)}, "
               f"MEAN round trip {_f(cap['mean_total_round_trip_bps'])} bps (a LOWER BOUND: displayed book + {cap['fee_provenance']} fees; "
               f"no latency, no impact beyond the book), largest displayed size in ANY single snapshot "
               f"${_f(cap['max_book_fillable_notional_usd'], 0)} (best case, not typical)")
    e = rep["economics"]
    if e:
        res = e["result"]
        out.append(f"6 ECONOMICS   {e['inputs']['basis']}, hold {e['inputs']['hold_days']:g} d, funding = {_f(e['inputs']['funding_used_bps_per_settlement'], 3)} bps/settlement ({e['inputs']['funding_used_is']})")
        out.append(f"    MODELLED net {_f(res['net_pnl_usd'])} USD; {_f(res['returns']['annualised_pct_on_economic_capital'])}%/yr on economic capital, "
                   f"{_f(res['returns']['annualised_pct_on_notional'])}%/yr on notional; UNPRICED (excluded, not zero): {', '.join(res['unknowns']) or 'none'}")
        out.append("    screens (" + TE.PROVISIONAL + "), on modelled costs only: " + ", ".join(f"{h['hurdle_apr_pct']:g}% -> {'meets' if h['meets'] else 'misses'}" for h in res["hurdles"]))
    else:
        out.append("6 ECONOMICS   WITHHELD: " + "; ".join(rep["economics_withheld_because"]))
    out.append("7 OPPORTUNITIES")
    for o in rep["opportunities"]:
        out.append(f"    {o['strategy']:<34} {o['origin']:<26} {o['status'] or 'FROZEN_ABSENT (never re-evaluated)'}")
        if o["status"] and o["reasons"]:
            out.append(f"        - {o['reasons'][0][:150]}")
    return "\n".join(out)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", required=True)
    ap.add_argument("--history", default=None, help="monitor.jsonl: its copied chain heads are anchors (same volume; archive it off the machine to make them external)")
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
