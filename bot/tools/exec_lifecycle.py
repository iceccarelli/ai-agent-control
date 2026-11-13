#!/usr/bin/env python3
"""The execution lifecycle as EVIDENCE: pure builders that turn what the autopilot and the venue actually produced into ``(kind, stage, payload)`` events for the durable outbox.

No I/O, no network, no clock, no venue import. Every payload is built to pass ``exec_evidence.make_event``'s secret guard (neutral key names, decimals rounded to 4 places, venue order / fill ids
shortened to 16-hex references: the full ids stay in the local session record) and every builder states, in ``source``, WHICH KIND OF FACT it reports, because they are different things:

  LOCAL   a decision this process made (allocation, plan, risk approval, lease check)
  VENUE   a record the exchange returned (orders, executions, the final position read)
  LEDGER  arithmetic over venue records (fees, price leg); NOT cash
  CASH    the venue's wallet cash read, settled in the capital book (the only source of realised results)

Nothing here proves profit: ``COST`` is components, ``RECONCILIATION`` says whether the venue's own position read was flat and what the cash settlement said, and ``DAILY_PNL`` carries the
capital book's VERIFIED realised figures (never unrealised, never unexplained cash) under neutral names. A successful outbox append is not venue confirmation; a flat venue read is not profit.

Stages (the intended chain; each event carries its own ``stage``):
  OPPORTUNITY / ECONOMICS_APPROVED (edge)  ->  CAPITAL_ALLOCATED  ->  LEASE_VERIFIED  ->  PLAN_CREATED  ->  RISK_APPROVED  ->  ORDER_* / FILL_*  ->  VENUE_FLAT_VERIFIED
  ->  COSTS_BOOKED  ->  SESSION_RECONCILED  ->  (next session) CAPITAL_SETTLED  ->  CAPITAL_BOOK
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal as D
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

Event = Tuple[str, str, Dict[str, Any]]              # (kind, stage, payload)


def ref16(x: Any) -> Optional[str]:
    """A stable short reference for a venue identifier (never the identifier itself)."""
    if x is None or str(x) == "":
        return None
    return hashlib.sha256(str(x).encode("utf-8")).hexdigest()[:16]


def num(x: Any, places: int = 4) -> Optional[str]:
    """A decimal string rounded to ``places`` (the evidence guard refuses long digit runs); None / non-numeric stays None."""
    if x is None or isinstance(x, bool):
        return None
    try:
        v = D(str(x))
    except Exception:
        return None
    if not v.is_finite():
        return None
    return format(v.quantize(D(1).scaleb(-places)), "f")


def capital_basis(capital: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The cost-of-capital basis of a verdict, compact and rounded for the evidence guard (long digit runs are refused): the state, the capital committed, the stated rate with its source, time and
    provenance, and the charge in bps of notional. An ABSENT statement shows as state UNSTATED with no figures; it is never rendered as a zero."""
    c = dict(capital or {})
    stated = c.get("cash_alternative_stated_ms")
    return {"state": None if c.get("state") is None else str(c["state"])[:20], "committed_usd": num(c.get("committed_usd"), 2), "multiple_of_notional": num(c.get("multiple_of_notional"), 4),
            "cash_alternative_apr": num(c.get("cash_alternative_apr"), 6), "cash_alternative_source": None if c.get("cash_alternative_source") is None else str(c["cash_alternative_source"])[:60],
            "cash_alternative_stated_ms": stated if isinstance(stated, int) and not isinstance(stated, bool) else None,
            "cash_alternative_provenance": None if c.get("cash_alternative_provenance") is None else str(c["cash_alternative_provenance"])[:20],
            "opportunity_cost_bps": num(c.get("opportunity_cost_bps"), 4)}


def neutral(v: Any) -> Any:
    """A value safe for the evidence guard: a number-like string or float becomes a decimal rounded to 4 places (the guard refuses long digit runs such as 0.30000000000000004), anything else is a
    short string; None and ints stay as they are."""
    if v is None or isinstance(v, bool) or (isinstance(v, int)):
        return v
    n = num(v, 4)
    return n if n is not None else str(v)[:40]


def event_id(session_id: str, kind: str, stage: str, n: int = 0) -> str:
    """Deterministic: re-emitting the same stage of the same session is a duplicate, never a second record."""
    return f"{session_id}:{kind}:{stage}:{n}"[:200]


def capital_allocated(alloc: Mapping[str, Any], settlement: Mapping[str, Any]) -> Event:
    return ("PROVENANCE", "CAPITAL_ALLOCATED", {"source": "LOCAL", "notional_usd": num(alloc.get("notional_usd")), "binding_limit": str(alloc.get("binding_limit"))[:40],
                                                "reasons": [str(r)[:60] for r in list(alloc.get("reasons", []))[:6]], "caps_usd": {str(k)[:30]: num(v) for k, v in dict(alloc.get("caps", {})).items()},
                                                "scale": num(alloc.get("scale")), "loss_streak": alloc.get("loss_streak"), "prior_settlement": str(settlement.get("status"))[:40]})


def lease_verified(fence: Optional[int], strong: bool) -> Event:
    return ("PROVENANCE", "LEASE_VERIFIED", {"source": "LOCAL", "fencing_generation": fence, "server_checked": bool(strong)})


def plan_created(plan: Mapping[str, Any]) -> Event:
    legs = [{"leg": str(r.get("leg_id"))[:12], "category": r.get("category"), "side": r.get("side"), "qty": num(r.get("qty"), 6), "price": num(r.get("price"), 2), "reduce_only": bool(r.get("reduce_only"))}
            for r in list(plan.get("legs", []))[:6] if isinstance(r, Mapping)]
    return ("PLAN", "PLAN_CREATED", {"source": "LOCAL", "plan_ref": ref16(plan.get("plan_digest")), "symbol": plan.get("symbol"), "notional_usd": num(plan.get("notional_usd")),
                                     "hold_s": (plan.get("timing") or {}).get("hold_s"), "style": plan.get("style"), "legs": legs})


def risk_approved(assess: Mapping[str, Any]) -> Event:
    per = {str(k)[:40]: bool(v.get("approved")) for k, v in dict(assess.get("risk_per_leg", {})).items() if isinstance(v, Mapping)}
    grp = assess.get("risk_whole_group") or {}
    return ("RISK_ASSESSMENT", "RISK_APPROVED", {"source": "LOCAL", "per_leg_approved": per, "group_approved": bool(grp.get("approved")),
                                                "reasons": [str(r)[:80] for r in list(grp.get("reasons", []))[:6]]})


def orders(rec: Mapping[str, Any]) -> List[Event]:
    out: List[Event] = []
    for o in list(rec.get("orders") or [])[:20]:
        link = str(o.get("link_id") or "")
        out.append(("ORDER", "ORDER_VENUE_RECORD", {"source": "VENUE", "order_ref": ref16(o.get("order_id")), "link_ref": ref16(link), "leg": link.rsplit("-", 1)[-1][:12],
                                                    "symbol": o.get("symbol"), "side": o.get("side"), "qty": num(o.get("qty"), 6), "filled_qty": num(o.get("filled_qty"), 6),
                                                    "avg_price": num(o.get("avg_price"), 2), "status": str(o.get("status"))[:30]}))
    return out


def fills(rec: Mapping[str, Any]) -> List[Event]:
    out: List[Event] = []
    for x in list(rec.get("executions") or [])[:40]:
        out.append(("FILL", "FILL_VENUE_RECORD", {"source": "VENUE", "fill_ref": ref16(x.get("exec_id")), "link_ref": ref16(x.get("link_id")), "symbol": x.get("symbol"), "side": x.get("side"),
                                                  "qty": num(x.get("qty"), 6), "price": num(x.get("price"), 2), "fee_usd": num(x.get("fee_usd"), 6), "at_ms": x.get("ts_ms")}))
    return out


def venue_flat(rec: Mapping[str, Any]) -> Event:
    fp = rec.get("final_position")
    return ("EXIT", "VENUE_FLAT_VERIFIED", {"source": "VENUE", "venue_flat_verified": rec.get("flat_verified") is True,
                                            "residual_by_leg": None if not isinstance(fp, Mapping) else {str(k)[:24]: num(v, 6) for k, v in fp.items()}})


def costs(rec: Mapping[str, Any]) -> Event:
    n = rec.get("net") if isinstance(rec.get("net"), Mapping) else {}
    return ("COST", "COSTS_BOOKED", {"source": "LEDGER", "fees_usd": num(n.get("fees_usd"), 6), "funding_usd": num(n.get("funding_usd"), 6), "price_leg_usd": num(n.get("price_pnl_usd"), 6),
                                     "funding_was_read": n.get("funding_usd") is not None, "note": "components only: realised cash is settled from the venue wallet read, not from this"})


def session_reconciled(rec: Mapping[str, Any], result: str, reconciliation: Optional[Mapping[str, Any]] = None) -> Event:
    """``reconciliation`` = the session's ``reconciliation.json`` (the FINAL ledger-vs-venue result). Absent (an older session, a write that failed) the payload is exactly what it was: nothing is
    invented, and a missing outcome is never read as 'reconciled'."""
    payload: Dict[str, Any] = {"source": "VENUE", "result": result, "venue_flat_verified": rec.get("flat_verified") is True, "basis": str(rec.get("basis"))[:30],
                               "orders": len(rec.get("orders") or []), "fills": len(rec.get("executions") or [])}
    if isinstance(reconciliation, Mapping) and reconciliation.get("schema") == "ms_session_reconciliation/1":
        payload["venue_reconciled"] = reconciliation.get("venue_reconciled") if isinstance(reconciliation.get("venue_reconciled"), bool) else None
        payload["view_complete"] = reconciliation.get("view_complete") is True
        payload["finding_codes"] = [str(c)[:60] for c in (reconciliation.get("finding_codes") or [])][:12]
        payload["wallet_checked"] = bool((reconciliation.get("checked") or {}).get("wallet"))        # `venue_reconciled` compares executions, ledger fills, positions and fees: not the wallet, unless this says so
    return ("RECONCILIATION", "SESSION_RECONCILED", payload)


def capital_settled(settled: Mapping[str, Any]) -> Optional[Event]:
    if settled.get("status") in (None, "NO_PENDING", "CASH_UNKNOWN"):
        return None
    return ("RECONCILIATION", "CAPITAL_SETTLED", {"source": "CASH", "for_session": str(settled.get("session_id"))[:60], "status": settled.get("status"), "cash_change_usd": num(settled.get("delta_usd")),
                                                  "transfers_removed_usd": num(settled.get("transfers_removed_usd")), "credited_usd": num(settled.get("credited_usd")),
                                                  "unverified_excess_usd": num(settled.get("unverified_excess_usd")), "session_flat_verified": bool(settled.get("flat_verified"))})


def capital_book(report: Mapping[str, Any]) -> Event:
    """The verified figures under neutral names (the evidence guard refuses account / P&L key names). TESTNET SIMULATION."""
    keys = ("start_cash_usd", "verified_realised_usd", "verified_gains_usd", "verified_losses_usd", "basis_usd", "reserve_usd", "sizing_basis_usd", "unverified_excess_usd", "max_drawdown_now_usd")
    p: Dict[str, Any] = {"source": "CASH", "label": "TESTNET SIMULATION, verified realised cash only"}
    rename = {"verified_realised_usd": "verified_result_usd", "verified_gains_usd": "verified_gain_total_usd", "verified_losses_usd": "verified_loss_total_usd"}
    for k in keys:
        p[rename.get(k, k)] = num(report.get(k))
    p.update({"lifecycles_settled": report.get("lifecycles_settled"), "wins": report.get("wins"), "losses": report.get("losses"), "profit_factor": report.get("profit_factor"),
              "loss_streak": report.get("loss_streak"), "halted": bool(report.get("halted"))})
    # A new EPOCH (the owner's audited ``capital --rebaseline``) restarts every figure above; the result over ALL epochs is stated beside them so a record can never read as a fresh account.
    p["epoch"] = report.get("epoch")
    p["all_epochs_verified_result_usd"] = num(report.get("lifetime_cumulative_realised_usd"))
    return ("DAILY_PNL", "CAPITAL_BOOK", p)


def decision_record(result: Mapping[str, Any], *, code_sha: Optional[str] = None, margin_per_usd: Optional[Any] = None, loss_cap_usd: Optional[Any] = None,
                    risk_limits: Optional[Mapping[str, Any]] = None, portfolio: Optional[Mapping[str, Any]] = None, venue: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The REPRODUCIBLE record of one moment-of-decision evaluation (pure). ``decision_id`` is a hash of everything that determined the answer: the digest of the market INPUTS (books,
    instrument rows, funding observation, fee tiers), the verdict digest, the decision, the size, the owner's floor, the candidate and the code revision. The same inputs and code give the same
    id and the same decision; a different market state, fee tier, size or code can never share one. Full digests stay in the local result; the evidence payload carries 16-hex references.
    ``capital_required_usd`` is notional x the cash margin per USD of the hedged pair; ``loss_cap_usd`` is the POLICY per-session loss ceiling, not a modelled loss."""
    edge = result.get("edge") if isinstance(result.get("edge"), Mapping) else None
    auth = result.get("authority") if isinstance(result.get("authority"), Mapping) else {}
    cand = {"candidate_id": auth.get("candidate_id"), "spec_ref": ref16(auth.get("spec_digest"))} if auth.get("candidate_id") else None
    portfolio_n = None if portfolio is None else {str(k)[:40]: neutral(v) for k, v in dict(portfolio).items()}
    core = {"inputs": result.get("inputs_digest"), "edge": None if edge is None else edge.get("digest"), "decision": result.get("decision"), "symbol": result.get("symbol"),
            "evaluated": result.get("evaluated_ms") if result.get("observed_ms") is None else None,            # a decision with NO market observation (no data, no opportunity) is unique to its moment
            "notional": result.get("notional_usd"), "floor": result.get("owner_funding_floor_bps"), "candidate": cand, "code": code_sha,
            "portfolio": None if portfolio_n is None else json.dumps(portfolio_n, sort_keys=True, default=str), "venue": None if venue is None else json.dumps(dict(venue), sort_keys=True, default=str)}
    did = "dec_" + hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()[:20]
    assumed = [str(a) for a in (edge or {}).get("assumed_inputs", [])]
    notional = result.get("notional_usd")
    rec: Dict[str, Any] = {
        "decision_id": did, "decision": result.get("decision"), "reasons": [str(x)[:120] for x in list(result.get("why", []))[:6]], "symbol": result.get("symbol"),
        "notional_usd": num(notional, 2), "owner_funding_floor_bps": num(result.get("owner_funding_floor_bps")), "funding_bps": num(result.get("funding_bps")),
        "seconds_to_settlement": num(result.get("seconds_to_settlement"), 1), "hold_s": result.get("hold_s"), "candidate": cand,
        "code_sha": None if code_sha is None else str(code_sha)[:12],                                      # the guard refuses a 24+ hex token as secret-shaped; the FULL revision is inside the id hash
        "evaluated_ms": result.get("evaluated_ms"),
        "inputs_ref": ref16(result.get("inputs_digest")), "observed_ms": result.get("observed_ms"), "data_scope": "TESTNET_DATA_NOT_MAINNET_EXPECTANCY", "source": "LOCAL",
        "authority_gaps": [str(g)[:40] for g in list(result.get("authority_gaps", []))[:10]]}
    if edge is not None:
        rec["edge"] = {"session_class": edge.get("session_class"), "net_edge_bps": num(edge.get("net_edge_bps")), "expected_funding_bps": num(edge.get("expected_funding_bps")),
                       "required_funding_bps": num(edge.get("required_funding_bps")), "expected_net_usd": num(edge.get("expected_net_usd")),
                       "costs_bps": {str(k)[:30]: num(v) for k, v in dict(edge.get("costs_bps", {})).items()}, "assumed_inputs": assumed[:10],
                       "settlements_in_hold": edge.get("settlements_in_hold"), "edge_ref": ref16(edge.get("digest")),
                       "capital_basis": capital_basis(edge.get("capital"))}
        rec["fee_tier"] = "ASSUMED" if any("fee" in a.lower() for a in assumed) else "MEASURED"
    rec["venue"] = None if venue is None else {str(k)[:20]: str(v)[:60] for k, v in dict(venue).items()}
    ms = result.get("observed_ms")
    if isinstance(ms, int) and ms > 0:
        import datetime as _dt
        rec["observed_utc"] = _dt.datetime.fromtimestamp(ms / 1000.0, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    inp = result.get("inputs") if isinstance(result.get("inputs"), Mapping) else None
    if inp is not None:
        rec["market_inputs"] = {str(k)[:40]: (v if isinstance(v, int) and not isinstance(v, bool) else None if v is None else str(v)[:40]) for k, v in dict(inp).items()}
        ages = [v for k, v in inp.items() if k.endswith("_book_age_ms") and isinstance(v, int)]
        rec["data_freshness_ms"] = {"oldest_book_age": max(ages) if ages else None, "inputs_observed_ms": inp.get("observed_ms")}
    rec["settlement"] = {"next_ms": result.get("next_settlement_ms"), "seconds_to_settlement": num(result.get("seconds_to_settlement"), 1), "rate_bps": num(result.get("funding_bps")),
                         "settlements_in_hold": (edge or {}).get("settlements_in_hold") if edge is not None else None}
    if edge is not None:
        try:                                                                       # the SCENARIOS, all arithmetic on the verdict's own declared inputs (labelled as such)
            costs = [D(str(v)) for v in dict(edge.get("costs_bps", {})).values() if v is not None]
            known = len(costs) == len(dict(edge.get("costs_bps", {}))) and bool(costs)
            n, rate = int(edge.get("settlements_in_hold") or 0), D(str(result.get("funding_bps")))
            total_cost = sum(costs, D(0))
            rec["scenarios"] = {"label": "ARITHMETIC_ON_DECLARED_INPUTS", "gross_expected_funding_bps": num(rate * n), "total_cost_bps": num(total_cost) if known else None,
                                "gross_before_haircut_net_bps": num(rate * n - total_cost) if known else None,
                                "conservative_expected_net_bps": num(edge.get("net_edge_bps")), "conservative_basis": "the economic model's net edge after its 30% funding haircut and all modelled costs (net_edge_bps); the 2 bps hurdle is the pass threshold, not a deduction",
                                "stress_half_funding_net_bps": num(rate * n / 2 - total_cost) if known else None, "required_funding_bps": num(edge.get("required_funding_bps"))}
        except (ArithmeticError, ValueError, TypeError):                           # noqa: BLE001
            rec["scenarios"] = {"label": "ARITHMETIC_ON_DECLARED_INPUTS", "unavailable": True}
    if risk_limits is not None:
        rec["risk_limits"] = {str(k)[:40]: neutral(v) for k, v in dict(risk_limits).items()}
    if portfolio is not None:
        rec["portfolio_context"] = dict(portfolio_n or {})
    if notional is not None and margin_per_usd is not None:
        try:
            rec["capital_required_usd"] = num(D(str(notional)) * D(str(margin_per_usd)), 2)
        except Exception:                                                       # noqa: BLE001
            rec["capital_required_usd"] = None
    if loss_cap_usd is not None:
        rec["worst_credible_loss_usd"] = {"value": num(loss_cap_usd, 2), "basis": "POLICY_PER_SESSION_LOSS_CEILING_NOT_A_MODEL"}
    return rec


def execution_quality(doc: Optional[Mapping[str, Any]]) -> List[Event]:
    """MEASURED per-leg and per-group execution quality (``exec_quality``) as evidence: kind COST (an existing kind: no migration), stage ``EXECUTION_QUALITY_LEG`` / ``..._GROUP``. Every number is
    the venue's own record or arithmetic over it; a field the venue did not give is the string ``UNKNOWN`` / null and is named in ``unknown``. Venue ids are shortened to 16-hex references;
    the same session re-emitted is the same ids and the same bytes (a duplicate, never a second record)."""
    if not isinstance(doc, Mapping):
        return []
    out: List[Event] = []
    for l in list(doc.get("legs") or [])[:12]:
        if not isinstance(l, Mapping):
            continue
        c, t, f, r, cx = l.get("cost") or {}, l.get("timing") or {}, l.get("fills") or {}, l.get("reference"), l.get("cancel")
        out.append(("COST", "EXECUTION_QUALITY_LEG", {
            "source": "VENUE", "leg": str(l.get("leg_id"))[:12], "role": l.get("kind"), "link_ref": ref16(l.get("link_id")), "decision_id": l.get("decision_id"), "candidate_id": l.get("candidate_id"),
            "category": (l.get("instrument") or {}).get("category"), "symbol": (l.get("instrument") or {}).get("symbol"), "side": l.get("side"), "intent": l.get("intent"),
            "order_type": l.get("order_type"), "time_in_force": l.get("time_in_force"), "planned_qty": num(l.get("planned_qty"), 6), "planned_price": num(l.get("planned_price"), 2),
            "reference": None if not isinstance(r, Mapping) else {"bid": num(r.get("bid"), 4), "ask": num(r.get("ask"), 4), "mid": num(r.get("mid"), 4), "at_ms": r.get("ts_ms"),
                                                               "origin": r.get("source"), "age_ms": r.get("age_ms_at_send")},
            "sent_ms": t.get("sent_ms"), "acked_ms": t.get("acked_ms"), "ack_latency_ms": t.get("ack_latency_ms"), "venue_created_ms": t.get("venue_created_ms"),
            "first_fill_ms": t.get("first_fill_ms"), "last_fill_ms": t.get("last_fill_ms"), "order_to_first_fill_ms": t.get("order_to_first_fill_ms"),
            "order_status": (l.get("order") or {}).get("status"), "order_ref": ref16((l.get("order") or {}).get("venue_order_ref")),
            "fill_count": f.get("count"), "fill_refs": [ref16(i) for i in list(f.get("ids") or [])[:8]], "fill_qty": num(f.get("qty"), 6), "fill_vwap": num(f.get("vwap"), 4),
            "fill_ratio": num(f.get("fill_ratio"), 4), "duplicate_events_collapsed": f.get("duplicate_events_collapsed"), "liquidity": l.get("liquidity"),
            "fee_usd": num(c.get("fee_usd"), 6), "fee_bps": num((l.get("fee") or {}).get("bps_of_notional"), 4), "fee_rate_origin": (l.get("fee") or {}).get("rate_provenance"),
            "price_vs_mid_usd": num(c.get("price_vs_mid_usd"), 6), "walk_vs_touch_usd": num(c.get("walk_vs_touch_usd"), 6), "half_spread_usd": num(c.get("half_spread_usd"), 6),
            "slippage_vs_mid_bps": num(c.get("slippage_vs_mid_bps"), 4), "slippage_vs_touch_bps": num(c.get("slippage_vs_touch_bps"), 4), "cost_total_usd": num(c.get("total_usd"), 6),
            "cost_total_bps": num(c.get("total_bps"), 4), "sign_convention": "positive = cost to us relative to the reference",
            "cancel": None if not isinstance(cx, Mapping) else {k: cx.get(k) for k in ("sent_ms", "acked_ms", "result", "latency_ms", "fills_after_request", "fills_within_clock_tolerance")},
            "unknown": [str(u)[:30] for u in list(l.get("unknown") or [])[:12]]}))
    g = doc.get("group")
    if isinstance(g, Mapping):
        out.append(("COST", "EXECUTION_QUALITY_GROUP", {
            "source": "VENUE", "decision_id": g.get("decision_id"), "style": g.get("style"), "planned_notional_usd": num(g.get("planned_notional_usd"), 2),
            "pair_notional_usd": num(g.get("position_notional_usd"), 4), "legs": g.get("legs"), "entry_legs_planned": g.get("entry_legs_planned"), "entry_legs_filled": g.get("entry_legs_filled"),
            "exit_legs": g.get("exit_legs"), "all_entry_legs_filled": g.get("all_entry_legs_filled"), "unhedged_window_ms": g.get("unhedged_window_ms"),
            "unwind_first_fill_ms": g.get("unwind_first_fill_ms"), "unwind_last_fill_ms": g.get("unwind_last_fill_ms"),
            "residual_qty": {str(k)[:24]: num(v, 6) for k, v in dict(g.get("residual_qty_by_instrument") or {}).items()}, "venue_flat_verified": g.get("venue_flat_verified"),
            "attempt_completed": g.get("attempt_completed"), "cost_total_usd": num(g.get("total_execution_cost_usd"), 6),
            "cost_total_bps_of_pair": num(g.get("total_execution_cost_bps_of_position_notional"), 4), "cost_complete": g.get("cost_complete"),
            "unknown": [str(u)[:30] for u in list(g.get("unknown") or [])[:12]]}))
    return out


def funding_events(doc: Optional[Mapping[str, Any]]) -> List[Event]:
    """Funding settlement recognition (``funding_settlement``) as evidence: kind FUNDING, one ``FUNDING_SETTLEMENT`` per settlement the hedge was held through (status, cash, the physical expectation,
    the cash check, the venue row id as a 16-hex reference) and one ``FUNDING_RECOGNITION`` summary per symbol. ``source`` is VENUE: a booked amount is the venue's row agreeing with physics, a
    quarantined or unknown one says so. The sign convention is UNVERIFIED and the payload says it. Nothing here is cash: realised results are settled from the wallet in the capital book."""
    if not isinstance(doc, Mapping):
        return []
    out: List[Event] = []
    suppressed = doc.get("suppressed")                                          # the session withdrew this document (the held window is unknown): NO print may carry a verdict or a figure
    for r in list(doc.get("recognitions") or [])[:4]:
        if not isinstance(r, Mapping):
            continue
        for p in list(r.get("prints") or [])[:20]:
            if suppressed:
                p = {**p, "status": "SUPPRESSED", "amount_usd": None, "expected_usd": None, "wallet": None, "reason": f"SUPPRESSED:{suppressed}"}
            w = p.get("wallet") if isinstance(p.get("wallet"), Mapping) else {}
            out.append(("FUNDING", "FUNDING_SETTLEMENT", {"source": "VENUE", "symbol": r.get("symbol"), "settlement_ms": p.get("ts_ms"), "rate": num(p.get("rate"), 8), "held_qty": num(p.get("signed_position_qty"), 6),
                                                          "status": p.get("status"), "amount_usd": num(p.get("amount_usd"), 6) if str(p.get("status")).startswith("BOOKED") else None,          # a quarantined / mismatched row's cash is NOT an amount: it must not be summable
                                                          "row_cash_if_h1_usd": None if str(p.get("status")).startswith("BOOKED") else num(p.get("amount_usd"), 6), "expected_usd": num(p.get("expected_usd"), 6), "row_ref": ref16(p.get("row_id")),
                                                          "cash_check": w.get("status"), "cash_check_delta_usd": num(w.get("delta_usd"), 6), "reason": str(p.get("reason") or "")[:160] or None}))
        out.append(("FUNDING", "FUNDING_RECOGNITION", {"source": "VENUE", "symbol": r.get("symbol"), "sign_hypothesis": r.get("sign_hypothesis"), "prints_available": r.get("prints_available"),
                                                        "window": r.get("window"), "all_resolved": r.get("all_prints_resolved"), "booked_usd": num(r.get("booked_usd"), 6),
                                                        "flags": [str(f)[:80] for f in list(r.get("flags") or [])[:10]], "unmatched_rows": len(r.get("unmatched_rows") or []),
                                                        "note": "funding is UNKNOWN unless booked; realised cash is settled from the wallet, not from this"}))
    return out


def session_events(rec: Mapping[str, Any], result: str, quality: Optional[Mapping[str, Any]] = None, funding: Optional[Mapping[str, Any]] = None,
                   reconciliation: Optional[Mapping[str, Any]] = None) -> List[Event]:
    """Everything the venue and the ledger said about a finished session, in lifecycle order (``quality`` = the session's measured execution quality, ``funding`` = its settlement recognition,
    ``reconciliation`` = its final ledger-vs-venue result, when written)."""
    return [*orders(rec), *fills(rec), venue_flat(rec), costs(rec), *execution_quality(quality), *funding_events(funding), session_reconciled(rec, result, reconciliation)]
