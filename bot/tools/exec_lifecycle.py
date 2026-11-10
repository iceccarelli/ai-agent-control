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


def session_reconciled(rec: Mapping[str, Any], result: str) -> Event:
    return ("RECONCILIATION", "SESSION_RECONCILED", {"source": "VENUE", "result": result, "venue_flat_verified": rec.get("flat_verified") is True, "basis": str(rec.get("basis"))[:30],
                                                     "orders": len(rec.get("orders") or []), "fills": len(rec.get("executions") or [])})


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
    return ("DAILY_PNL", "CAPITAL_BOOK", p)


def session_events(rec: Mapping[str, Any], result: str) -> List[Event]:
    """Everything the venue and the ledger said about a finished session, in lifecycle order."""
    return [*orders(rec), *fills(rec), venue_flat(rec), costs(rec), session_reconciled(rec, result)]
