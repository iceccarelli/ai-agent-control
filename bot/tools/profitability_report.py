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
LOWER confidence bound of realised funding (the lower of the window's and the most recent third's),
never the forecast, and is WITHHELD (not printed with a caveat) unless the evidence is valid, EVERY chained
file is complete and consistent (an unchained row, a broken chain, an anchored file that has lost its sidecars or its rows,
or an anchored file that has been removed all block it; a pre-chain LEGACY file with no v2 rows and no anchor is listed and
does not), the recorder is HEALTHY, the evidence is mainnet, and the funding evaluation passes both its
count and its effective-sample-size floors and the engine's 90-settlement floor. Rows adopted by a repair
are left out of every statistic. A number the gates have not cleared is not shown.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import anchor_archive as AA       # noqa: E402
import capital_survival as CAP    # noqa: E402
import capacity_schema as CS      # noqa: E402
import carry_costs as CC          # noqa: E402
import evidence_chain as EC       # noqa: E402
import evidence_stats as ES       # noqa: E402
import funding_eval as FE         # noqa: E402
import opportunity_engine as OE   # noqa: E402
import recorder_reliability as RR # noqa: E402
import statistical_power as SP    # noqa: E402
import trade_economics as TE      # noqa: E402

ASSUMED_VENUE_RULES = {"collateral_haircut": 0.10, "initial_margin_rate": 0.10, "maintenance_margin_rate": 0.005,
                       "status": "ASSUMED_NOT_VENUE_VERIFIED"}


def _json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as fh:
            v = json.load(fh)
        return v if isinstance(v, dict) else None
    except (OSError, ValueError, RecursionError):
        return None


CAPACITY_RETENTION_DAYS = 365       # the recorder's default capacity retention; an older anchored file may have been pruned


def _file_day_ms(name: str) -> Optional[int]:
    """UTC midnight (ms) of ``capacity-YYYYMMDD.jsonl``, or None."""
    import calendar
    m = name[9:17] if name.startswith("capacity-") and name.endswith(".jsonl") else ""
    if len(m) != 8 or not m.isdigit():
        return None
    try:
        return int(calendar.timegm((int(m[:4]), int(m[4:6]), int(m[6:8]), 0, 0, 0))) * 1000
    except (ValueError, OverflowError):
        return None


def _utc_day(ms: float) -> str:
    import time
    return time.strftime("%Y%m%d", time.gmtime(ms / 1000.0))


def _retention_days(value: Any) -> int:
    """The capacity retention the OWNER says the recorder runs with (``--capacity-retention-days``; 365 is the recorder's default, 0 means it
    keeps everything). It is deliberately NOT read from the evidence directory: whoever can delete a day of evidence can also edit a status
    file there, and the missing-anchored-file test is the only thing that notices a deleted day."""
    if type(value) is not int or not (0 <= value <= 36_500):
        raise ValueError("capacity_retention_days must be an integer between 0 and 36500")
    return value


def anchors_from_history(path: Optional[str]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    if not path:
        return out
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:         # bytes that are not UTF-8 must not stop the report
            for line in fh:
                try:
                    r = json.loads(line)
                except (ValueError, RecursionError):                       # a deeply nested line is junk, not a crash
                    continue
                if not isinstance(r, dict):
                    continue
                f, h, n = r.get("recorder_chain_file"), r.get("recorder_chain_head"), r.get("recorder_chain_n")
                if isinstance(f, str) and isinstance(h, str) and isinstance(n, int):
                    out.setdefault(f, []).append({"n": n, "head": h})
    except OSError:
        pass
    return out



def _power_section(fe: Optional[Dict[str, Any]], notional_usd: float, hold_days: float) -> Dict[str, Any]:
    """How many settlements it would take to tell funding that clears the lowest screening hurdle from noise, at the family-wise
    alpha, from the observed variance and dependence. A statement about WHAT WOULD BE NEEDED, not a result."""
    if not fe or fe["realised_bps"].get("sd") in (None, 0) or not fe["realised_bps"].get("ess_ratio"):
        return {"status": "INSUFFICIENT", "reason": "no joined settlements with variance and a measured effective sample size yet"}
    rb = fe["realised_bps"]
    n_settle = max(1.0, hold_days * 3.0)
    lowest = min(TE.SCREENING_HURDLES_PCT)
    # mean funding per settlement (bps of hedged notional) at which an ILLUSTRATIVE position (inventory = notional, 10 % reserve)
    # earns the lowest screen after an ASSUMED round trip (carry_costs: 11.0 fee + 0.4 assumed spread): reserve-inclusive capital x hurdle x hold / 365, plus the cost
    threshold = (1.1 * lowest / 100.0 * hold_days / 365.0 * 1e4 + CC.ROUND_TRIP_TOTAL_COST_BPS_PERP_OVERLAY) / n_settle
    rows = []
    for edge in (0.1, 0.25, 0.5):
        r = SP.required_observations(rb["sd"], edge, alpha=0.05, power=0.8, n_hypotheses=OE.FAMILY_TRIALS, adjust="holm",
                                     ess_ratio=rb["ess_ratio"], two_sided=True, method="t")
        rows.append({"edge_above_threshold_bps_per_settlement": edge, "status": r["status"],
                     "required_effective_n": r.get("required_effective_n"), "required_raw_settlements": r.get("required_raw_n"),
                     "required_days": None if r.get("required_raw_n") is None else r["required_raw_n"] / 3.0,
                     "available_joined_settlements": fe["joined"], "available_effective_n": rb.get("ess")})
    return {"status": "OK", "threshold_bps_per_settlement": threshold, "observed_sd_bps": rb["sd"], "ess_ratio": rb["ess_ratio"],
            "alpha": 0.05, "power": 0.8, "n_hypotheses": OE.FAMILY_TRIALS, "adjust": "holm", "two_sided": True, "method": "t", "table": rows,
            "reading": (f"to show mean funding exceeds {threshold:.2f} bps/settlement (what the illustrative position needs to earn the lowest "
                        f"{TE.PROVISIONAL} screen after an ASSUMED {CC.ROUND_TRIP_TOTAL_COST_BPS_PERP_OVERLAY:g} bps round trip) by the edge in each row, at the family-wise level and 80% power, "
                        "given the variance and dependence OBSERVED so far; only as good as that sd and ESS ratio"),
            "classes": {"threshold": "ASSUMED (illustrative position and cost)", "sd_and_ess_ratio": "ESTIMATED", "counts": "CALCULATED"}}


def _exposure_section(fe, economics, withheld, accepted, notional_usd, hold_days, total_cost) -> Dict[str, Any]:
    """The capital-survival recommendation. It is ALLOWED to be, and today is, ZERO exposure; every reason is listed. Leverage
    levels above 1x are analysis only and are never recommended."""
    status = fe["status"] if fe else None
    if economics is None or fe is None or not accepted:
        rec = CAP.recommend_exposure({}, edge_lower_bound=None, funding_evidence_status=status)
        return {"recommendation": rec, "table": None, "reading": "ZERO exposure: the evidence gates for the economics block are not cleared, so no "
                "survival table is built from it"}
    res = economics["result"]
    f_lo = economics["inputs"]["funding_used_bps_per_settlement"]
    edge_lb = f_lo - (res.get("breakeven_funding_bps_per_settlement") or 0.0)
    costs = TE.Costs(perp_entry_fee_bps=accepted[-1]["fees"]["perp_taker_bps"], perp_exit_fee_bps=accepted[-1]["fees"]["perp_taker_bps"],
                     spread_bps=None, fee_provenance="UNVERIFIED")      # UNKNOWN is None: a 0.0 here is "priced at zero" to capital_survival
    inputs = CAP.CarryInputs(capital_usd=notional_usd, hold_days=hold_days, funding_bps_path=[f_lo] * max(1, int(round(hold_days * 3))),
                             costs=costs, reserve_frac_of_notional=0.1)
    table = CAP.survival_table(inputs)
    rec = CAP.recommend_exposure(table, edge_lower_bound=edge_lb, funding_evidence_status=status)
    return {"recommendation": rec, "edge_lower_bound_bps_per_settlement_over_breakeven": edge_lb,
            "stress_survival_limit_analysis_only": table.get("stress_survival_limit"), "venue_rules_status": table.get("venue_rules_status"),
            "reading": "a research ceiling on exposure, never an instruction; levels above 1x are analysis only"}


def build(directory: str, *, now_ms: int, history: Optional[str] = None, notional_usd: float = 100_000.0,
          hold_days: float = 30.0, artifacts_dir: Optional[str] = None, anchor_archive: Optional[str] = None,
          archive_head: Optional[str] = None, archive_entries: Optional[int] = None,
          capacity_retention_days: int = CAPACITY_RETENTION_DAYS) -> Dict[str, Any]:
    paths = sorted(p for p in glob.glob(os.path.join(directory, "capacity-*.jsonl")))
    anchors = anchors_from_history(history)
    archive = None
    archive_only: Dict[str, List[Dict[str, Any]]] = {}
    if anchor_archive:
        if archive_entries is not None and (type(archive_entries) is not int or archive_entries < 1):
            raise ValueError("archive_entries must be an integer >= 1 (the entry count when the owner stored the head)")
        archive = AA.verify(anchor_archive, owner_held_head=archive_head, owner_held_entries=archive_entries)
        entries, _problems = AA.read_archive(anchor_archive)
        for fname, lst in AA.anchors_by_file(entries).items():          # entries before any break in the archive's own chain
            anchors.setdefault(fname, []).extend(lst)
        # an archive that has GROWN since the owner stored its head is accepted, but only the entries inside the stored prefix are pinned by
        # the owner; later ones are not external evidence of anything (an attacker with the archive could append them)
        pinned = entries[:archive_entries] if (archive_head and archive_entries is not None) else entries
        for fname, lst in AA.anchors_by_file(pinned).items():
            archive_only[fname] = list(lst)
    files: List[Dict[str, Any]] = []
    all_rows: List[Any] = []
    blockers: List[str] = []
    for p in paths:
        base = os.path.basename(p)
        # READ-ONLY: verify, then admit only the chained prefix of a file whose chain is internally consistent.
        # A row without a chain entry never enters any statistic; the report never repairs what it reads.
        admitted, ver = EC.admitted_rows(p, anchors=anchors.get(base))
        lines, quarantined = EC.read_lines(p)
        every = EC.rows_of(lines)
        v2_any = any(isinstance(r, dict) and r.get("schema") == CS.SCHEMA for r in every)
        # "legacy" is only a file with NO sidecar, NO v2 rows and NO anchor ever recorded for it: an anchored file that lost its
        # sidecar or its rows is a deletion/replacement, not a legacy file
        pre_chain = (not os.path.exists(p + ".chain")) and not v2_any and not anchors.get(base)
        if pre_chain:
            # a legacy file from before the chain existed: only schema-less rows, which never enter a statistic and
            # cannot be "tampered into" evidence. It is listed, counted as legacy, and does not block anything.
            admitted, state = every, "LEGACY_PRE_CHAIN"
        else:
            state = ("ELIGIBLE" if ver["economic_evidence_eligible"] else
                     "UNCHAINED_TAIL" if ver["internally_consistent"] and ver["unchained_tail"] else "CHAIN_BROKEN")
            if not ver["economic_evidence_eligible"]:
                codes = ",".join(sorted({i["code"] for i in ver["issues"]})) or "unknown"
                blockers.append(f"{base}: {ver['unchained_tail']} unchained row(s)" if state == "UNCHAINED_TAIL"
                                else f"{base}: chain issues {codes}")
        # anchors per SOURCE: a file counts as externally anchored only when the ARCHIVE alone anchors it (copies in monitor.jsonl
        # sit on the recorder's volume, and merging the two must not let them stand in for an archive that never saw this file)
        by_archive = (EC.verify(p, archive_only.get(base))["anchor_status"] if (archive and not pre_chain) else None)
        files.append({"file": base, "rows": len(every), "admitted_rows": len(admitted), "state": state,
                      "archive_anchor_status": by_archive,
                      "schema_v2_rows": sum(1 for r in every if isinstance(r, dict) and r.get("schema") == CS.SCHEMA),
                      "quarantined_lines": quarantined,
                      "chain": {k: ver[k] for k in ("ok", "internally_consistent", "fully_chained", "economic_evidence_eligible",
                                                    "verified_prefix_rows", "n_chained", "unchained_tail", "head", "issues",
                                                    "resealed_rows", "anchor_status", "chain_rows", "latest_anchor_rows",
                                                    "anchor_lag_rows", "torn_aside_bytes")},
                      "external_anchors": len(anchors.get(base, []))})
        all_rows.extend(admitted)
    # a file the anchors know about that is no longer there (inside the capacity retention) was removed, not pruned
    present = {os.path.basename(p) for p in paths}
    missing_anchored: List[str] = []
    retention = _retention_days(capacity_retention_days)
    cutoff_day = _utc_day(now_ms - retention * 86_400_000) if retention > 0 else None
    for fname in sorted(anchors):
        if fname in present:
            continue
        # the recorder prunes a day only when its date is strictly BEFORE the date of (now - retention); a file on or after that date
        # (or any file, when the recorder keeps everything) cannot have been pruned
        if _file_day_ms(fname) is not None and (cutoff_day is None or fname[9:17] >= cutoff_day):
            missing_anchored.append(fname)
            kept = f"the {retention}-day retention" if retention > 0 else "a recorder that keeps every day"
            blockers.append(f"{fname}: anchors exist for this file but it is gone (inside {kept}): removed, not pruned")
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

    def _count(key: str, value: Any) -> int:
        return sum(1 for f in files if f["state"] != "LEGACY_PRE_CHAIN" and f["chain"][key] == value)

    chain_files = [f for f in files if f["state"] != "LEGACY_PRE_CHAIN"]
    if archive and archive["problems"]:
        blockers.append("the anchor archive fails its own chain: " + "; ".join(archive["problems"][:2]))
    if archive and archive["owner_held_head_matches"] is False:
        blockers.append("the anchor archive does NOT match the head the owner stored off-machine")
    summary = {
        "validated_rows": val["n_valid_rows"], "rejected_rows": val["n_rejected"],
        "legacy_quarantined_rows": val["n_legacy_quarantined"],
        "unchained_rows": sum(f["chain"]["unchained_tail"] for f in chain_files),
        "resealed_rows": sum(f["chain"]["resealed_rows"] for f in chain_files),
        "torn_lines_quarantined": sum(len(f["quarantined_lines"]) for f in files),
        "files_missing_but_anchored": missing_anchored,
        "files_total": len(files), "files_legacy_pre_chain": len(files) - len(chain_files),
        "files_internally_verified": sum(1 for f in chain_files if f["chain"]["internally_consistent"]),
        "files_fully_chained": sum(1 for f in chain_files if f["chain"]["fully_chained"]),
        "files_anchored_current": _count("anchor_status", "ANCHORED_CURRENT"),
        "files_anchored_lagging": _count("anchor_status", "ANCHORED_LAGGING"),
        "files_stale_anchor": _count("anchor_status", "STALE_ANCHOR"),
        "files_unanchored": _count("anchor_status", "UNANCHORED"),
        "files_anchor_conflict": _count("anchor_status", "ANCHOR_CONFLICT"),
        "files_anchored_by_archive": sum(1 for f in chain_files if f.get("archive_anchor_status") in ("ANCHORED_CURRENT", "ANCHORED_LAGGING")),
        "max_anchor_lag_rows": max((f["chain"]["anchor_lag_rows"] for f in chain_files if f["chain"]["anchor_lag_rows"] is not None), default=None),
        "anchor_location": ((archive or {}).get("location") if archive and archive["location"] != "NO_ARCHIVE" else
                            "SAME_VOLUME_COPY_NOT_VERIFIED_OFF_MACHINE" if any(f["external_anchors"] for f in files) else "NONE"),
        "anchor_archive": archive,
        "economic_evidence_blockers": blockers,
        "economic_evidence_eligible": bool(chain_files) and not blockers and val["stream_ok"],
    }
    identity = {"environments": sorted({r["environment"] for r in accepted}), "symbols": sorted({r["symbol"] for r in accepted}),
                "modes": sorted({r["mode"] for r in accepted}), "venues": sorted({r["venue"] for r in accepted}),
                "categories": sorted({r["category"] for r in accepted})}
    env = identity["environments"][0] if len(identity["environments"]) == 1 else None
    withheld: List[str] = []
    if not val["stream_ok"]:
        withheld.append("the evidence stream failed validation")
    if not summary["economic_evidence_eligible"]:
        withheld.extend(blockers or ["no chained evidence file"])
        if summary["unchained_rows"]:
            withheld.append(f"{summary['unchained_rows']} unchained row(s) exist; they are excluded and an economic calculation "
                            "cannot rest on a file that is not completely chained (explicit repair is a separate, visible act)")
    if rel["verdict"] != "HEALTHY":
        withheld.append(f"the recorder is {rel['verdict']}")
    if env != "mainnet":
        withheld.append(f"the evidence environment is {env or identity['environments'] or 'absent'}, not mainnet")
    if not fe:
        withheld.append("no funding evaluation")
    else:
        if fe["joined"] < OE.MIN_SETTLEMENTS:
            withheld.append(f"{fe['joined']} joined settlements; the opportunity engine needs {OE.MIN_SETTLEMENTS} before it will screen anything")
        if fe["status"] != "OK":
            withheld.extend(fe.get("status_reasons") or ["the funding evaluation is not OK"])
        if fe["realised_bps"]["ci"].get("status") != "OK":
            withheld.append(f"no confidence interval on realised funding ({fe['realised_bps']['ci'].get('status')})")
    if total_cost is None:
        withheld.append("no measured round-trip cost at the target size")

    economics = None
    if not withheld:
        f_lo = fe["realised_bps"]["conservative_lo_bps"]
        interval_h = accepted[-1]["funding"]["interval_h"]
        n_settle = max(1, int(round(hold_days * 24.0 / interval_h)))
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
                                "funding_used_is": fe["realised_bps"]["conservative_lo_is"] + " of the realised public mean",
                                "venue_rules": ASSUMED_VENUE_RULES},
                     "result": TE.compute(pos, costs, [f_lo] * n_settle, funding_source="PUBLIC_REALISED")}
    power = _power_section(fe, notional_usd, hold_days)
    exposure = _exposure_section(fe, economics, withheld, accepted, notional_usd, hold_days, total_cost)
    bundle = {"carry": {"validation_ok": val["stream_ok"], "chain_ok": summary["economic_evidence_eligible"], "integrity": summary, "environment": env,
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
        "integrity": {"files": files, "summary": summary,
                      "anchor_note": "anchors read from monitor.jsonl are copies of the chain head on the SAME volume: this tool cannot "
                                     "verify that any copy exists off the machine"},
        "reliability": rel, "independence": indep, "funding_evaluation": fe,
        "capacity": {"target_notional_usd": notional_usd, "book_fillable_snapshot_share": fillable,
                     "mean_total_round_trip_bps": total_cost, "mean_book_round_trip_bps": book_cost,
                     "fee_provenance": fee_prov,
                     "max_book_fillable_notional_usd": max((r.get("max_displayed_fillable_notional_usd") or 0.0 for r in accepted), default=None),
                     "wording": "'book-fillable snapshots' = snapshots in which the DISPLAYED book could fill the size; "
                                "not executable liquidity"},
        "economics": economics, "economics_withheld_because": withheld,
        "required_observations": power, "exposure_recommendation": exposure,
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
    sm = i["summary"]
    out.append("2 INTEGRITY   (separate states, no single flag)")
    out.append(f"    rows: validated {sm['validated_rows']}, rejected {sm['rejected_rows']}, legacy quarantined {sm['legacy_quarantined_rows']}, "
               f"UNCHAINED {sm['unchained_rows']} (excluded), resealed {sm['resealed_rows']}, torn lines quarantined {sm['torn_lines_quarantined']}")
    out.append(f"    files: {sm['files_total']} ({sm['files_legacy_pre_chain']} legacy pre-chain), internally verified {sm['files_internally_verified']}, "
               f"completely chained {sm['files_fully_chained']}")
    out.append(f"    anchors (copies in monitor.jsonl, {sm['anchor_location'].replace('_', ' ').lower()}): current {sm['files_anchored_current']}, "
               f"lagging {sm['files_anchored_lagging']} (max lag {sm['max_anchor_lag_rows']} rows), STALE {sm['files_stale_anchor']}, "
               f"UNANCHORED {sm['files_unanchored']}, CONFLICT {sm['files_anchor_conflict']}; anchored by the ARCHIVE itself: "
               f"{sm['files_anchored_by_archive']} of {sm['files_total'] - sm['files_legacy_pre_chain']} chained files"
               + ("  - a consistent truncation of the unanchored newest rows would not be detected" if sm["files_unanchored"] or sm["files_stale_anchor"] else ""))
    out.append(f"    economic-evidence eligible (every chained file complete and consistent, stream valid): {sm['economic_evidence_eligible']}"
               + ("" if sm["economic_evidence_eligible"] else "  <- " + "; ".join(sm["economic_evidence_blockers"][:3])))
    for f in i["files"]:
        iss = ",".join(x["code"] for x in f["chain"]["issues"]) or "none"
        q = f", QUARANTINED final line(s): {len(f['quarantined_lines'])}" if f["quarantined_lines"] else ""
        rs = f", RESEALED rows adopted after a crash: {f['chain']['resealed_rows']}" if f["chain"].get("resealed_rows") else ""
        rs += f", torn bytes SET ASIDE in {f['file']}.torn: {f['chain']['torn_aside_bytes']}" if f["chain"].get("torn_aside_bytes") else ""
        lag = "" if f["chain"]["anchor_lag_rows"] is None else f" (lag {f['chain']['anchor_lag_rows']} of {f['chain']['chain_rows']} rows)"
        out.append(f"    {f['file']}: {f['state']}, rows {f['rows']} (admitted {f['admitted_rows']}, v2 {f['schema_v2_rows']}), "
                   f"issues: {iss}, anchor {f['chain']['anchor_status']}{lag}{q}{rs}")
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
    pw = rep["required_observations"]
    if pw.get("status") == "OK":
        out.append(f"6b POWER      {pw['reading']}")
        for row in pw["table"]:
            out.append(f"    edge +{row['edge_above_threshold_bps_per_settlement']:g} bps: {row['required_effective_n']} effective = "
                       f"{row['required_raw_settlements']} settlements ({_f(row['required_days'], 0)} days) needed; have "
                       f"{row['available_joined_settlements']} joined, ESS {_f(row['available_effective_n'], 1)}")
    else:
        out.append(f"6b POWER      {pw.get('reason')}")
    ex = rep["exposure_recommendation"]["recommendation"]
    out.append(f"6c EXPOSURE   research ceiling {ex['max_leverage']:g}x ({ex['label']}); " + ("reasons: " + ", ".join(ex["reasons"]) if ex["reasons"] else "no blocking reason")
               + "; not an instruction")
    out.append("7 OPPORTUNITIES")
    for o in rep["opportunities"]:
        out.append(f"    {o['strategy']:<34} {o['origin']:<26} {o['status'] or 'FROZEN_ABSENT (never re-evaluated)'}")
        if o["status"]:
            for why in o["reasons"][:3]:
                out.append(f"        - {why[:220]}")
    return "\n".join(out)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", required=True)
    ap.add_argument("--history", default=None, help="monitor.jsonl: its copied chain heads are anchors (same volume; archive it off the machine to make them external)")
    ap.add_argument("--anchor-archive", default=None, help="tools/anchor_archive.py archive kept off the recorder machine")
    ap.add_argument("--archive-head", default=None, help="the archive head you stored somewhere the recorder machine cannot reach")
    ap.add_argument("--archive-entries", type=int, default=None, help="the entry count at the time you stored it (a longer archive is then accepted)")
    ap.add_argument("--capacity-retention-days", type=int, default=CAPACITY_RETENTION_DAYS,
                    help="the capacity retention the recorder runs with (its --capacity-retention-days; 0 = keeps everything); decides which anchored "
                         "files that are gone are 'removed' and which 'pruned'")
    ap.add_argument("--notional", type=float, default=100_000.0)
    ap.add_argument("--hold-days", type=float, default=30.0)
    ap.add_argument("--now-ms", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not (0 <= a.capacity_retention_days <= 36_500):
        ap.error("--capacity-retention-days must be between 0 and 36500")
    if a.archive_entries is not None and a.archive_entries < 1:
        ap.error("--archive-entries must be at least 1")
    if not (math.isfinite(a.hold_days) and 0 < a.hold_days <= 3650):
        ap.error("--hold-days must be a finite number of days in (0, 3650]")
    if not (math.isfinite(a.notional) and a.notional > 0):
        ap.error("--notional must be a finite positive number of USD")
    now = a.now_ms if a.now_ms is not None else int(time.time() * 1000)
    rep = build(a.dir, now_ms=now, history=a.history, notional_usd=a.notional, hold_days=a.hold_days,
                anchor_archive=a.anchor_archive, archive_head=a.archive_head, archive_entries=a.archive_entries,
                capacity_retention_days=a.capacity_retention_days)
    print(json.dumps(rep, indent=2, sort_keys=True, default=str) if a.json else render(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
