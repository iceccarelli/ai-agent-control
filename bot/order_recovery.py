"""Settle the carry orders a restart finds unresolved, from the venue's own word.

A process that dies mid-order leaves rows in the order log that are not final.
This reads each one against the venue (read-only; it SENDS NOTHING unless asked
to cancel a resting order) and moves it through the lifecycle:

  pending                   never left (the submit marker is written before the
                            request, so pending proves no request was made)
                            -> never_sent
  the venue shows the order -> its fills and final state are recorded
  the venue shows nothing   -> never_sent ONLY once the time in which a signed
                            request could still be accepted has passed
                            (ABSENCE_PROOF_S); sooner it stays
                            reconciliation_required. A timeout is never treated
                            as a failure.
  the venue cannot be read  -> reconciliation_required

Exposure is never abandoned: an order with fills ends filled or cancelled-with-
fills, or stays unresolved. Whatever is still unresolved blocks the next cycle
(CarryRisk) and the start of the book (main.carry_cold_start).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import order_lifecycle as ol

logger = logging.getLogger(__name__)

#: How long after a request left before "the venue shows no such order" proves
#: it never will. Carry requests are signed with a receive window of seconds
#: (BYBIT_RECV_WINDOW_MS, default 5000); the venue refuses one that arrives
#: after it. Sixty seconds is twelve default windows: past it a request that was
#: not processed can no longer be.
ABSENCE_PROOF_S = 60.0


@dataclass(frozen=True)
class Resolution:
    order_link_id: str
    leg: str
    outcome: str            # the order's status after this pass
    detail: str = ""


@dataclass
class RecoveryReport:
    resolved: List[Resolution] = field(default_factory=list)
    unresolved: List[Resolution] = field(default_factory=list)
    #: cycle id -> {leg: cumulative filled quantity} for every order read.
    filled_by_cycle: Dict[str, Dict[str, float]] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not self.unresolved


def resolve_unresolved_orders(broker: Any, store: Any, *,
                              now_epoch: Optional[float] = None,
                              cancel_resting: bool = False) -> RecoveryReport:
    report = RecoveryReport()
    now = time.time() if now_epoch is None else float(now_epoch)

    def apply(link: str, kind: str, **kw: Any) -> None:
        store.apply_order_event(link, kind, **kw)

    for row in store.unresolved_carry_orders():
        link = str(row["order_link_id"])
        symbol = str(row["symbol"])
        status = str(row["status"])
        product = str(row.get("leg") or "")
        if product not in ("spot", "linear"):
            product = "linear" if "linear" in str(row.get("purpose", "")) else "spot"
        requested = float(row["qty"])

        if status == ol.PENDING:
            apply(link, ol.NEVER_SENT,
                  detail={"proof": "intent recorded; the submit marker is "
                                   "written before any request leaves"})
            _file(report, store, link, product, "pending intent")
            continue
        try:
            state = broker.read_order(symbol, link, product)
        except Exception as exc:  # noqa: BLE001
            apply(link, ol.RECONCILE_REQUIRED,
                  detail={"why": f"venue unreadable: {type(exc).__name__}"})
            _file(report, store, link, product,
                  f"venue unreadable ({type(exc).__name__})")
            continue

        if state is None:
            recorded = float(row.get("filled_qty") or 0.0)
            age = now - float(row.get("created_epoch") or now)
            if (status in (ol.SUBMITTED, ol.TIMED_OUT, ol.UNKNOWN,
                           ol.RECONCILIATION_REQUIRED)
                    and recorded <= 0 and age >= ABSENCE_PROOF_S):
                apply(link, ol.NEVER_SENT, detail={
                    "proof": f"no such order at the venue {age:.0f}s after "
                             "the request; a signed request cannot be "
                             "accepted after its receive window"})
                _file(report, store, link, product, "absent past the window")
            else:
                apply(link, ol.RECONCILE_REQUIRED, detail={
                    "why": "the venue shows no such order yet; absence is "
                           "not proof this soon after the request"})
                _file(report, store, link, product, "absent, not yet provable")
            continue

        cum = float(state.get("filled_qty") or 0.0)
        avg = float(state.get("avg_price") or 0.0)
        if cum > 0:
            apply(link, ol.FILL_REPORT, filled_qty=cum, avg_price=avg or None,
                  event_key=f"{link}:fill:{cum:.12g}")
        if state.get("resting"):
            apply(link, ol.ACK)
            if cancel_resting:
                try:
                    broker.cancel_order(symbol=symbol, order_link_id=link,
                                        product=product)
                except Exception as exc:  # noqa: BLE001
                    apply(link, ol.RECONCILE_REQUIRED,
                          detail={"why": f"cancel failed: {type(exc).__name__}"})
            _file(report, store, link, product, "resting at the venue")
            continue
        # Final at the venue.
        if cum > 0:
            if cum < requested * (1.0 - 1e-9):
                apply(link, ol.CANCEL_CONFIRM, filled_qty=cum)
        elif str(state.get("status", "")).lower() == "rejected":
            apply(link, ol.REJECT)
        else:
            apply(link, ol.CANCEL_CONFIRM, filled_qty=0.0)
        _file(report, store, link, product, f"venue: {state.get('status')}")

    return report


def _file(report: RecoveryReport, store: Any, link: str, product: str,
          detail: str) -> None:
    row = store.get_order(link) or {}
    status = str(row.get("status", ""))
    res = Resolution(link, product, status, detail)
    (report.resolved if status in ol.TERMINAL else report.unresolved).append(res)
    cycle = str(row.get("cycle_id") or "")
    if cycle:
        legs = report.filled_by_cycle.setdefault(cycle, {})
        legs[product] = max(legs.get(product, 0.0),
                            float(row.get("filled_qty") or 0.0))
    logger.warning("order recovery: %s %s -> %s (%s)", link, product, status,
                   detail)
