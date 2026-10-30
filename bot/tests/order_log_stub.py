"""An in-memory order log with the StateStore's contract, for stub stores.

Test doubles that stand in for the StateStore in wiring tests need the order-log
methods the carry broker now requires before it will send anything. This keeps
their behaviour honest by running the REAL lifecycle rules (order_lifecycle)
rather than returning canned answers.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import order_lifecycle as ol                    # noqa: E402
from persistence import OrderEventResult        # noqa: E402


class InMemoryOrderLog:
    def _orders(self):
        if not hasattr(self, "_order_rows"):
            self._order_rows = {}
            self._order_event_keys = set()
        return self._order_rows

    def record_order_intent(self, order_link_id, symbol, side, qty, *, leg,
                            cycle_id, order_type="Market", price=0.0,
                            purpose=None):
        rows = self._orders()
        if order_link_id in rows:
            return False
        rows[order_link_id] = {
            "order_link_id": order_link_id, "symbol": symbol, "side": side,
            "qty": float(qty), "status": ol.PENDING, "filled_qty": 0.0,
            "avg_price": 0.0, "leg": leg, "cycle_id": cycle_id,
            "purpose": purpose or f"carry-{leg}", "exchange_id": "",
            "created_epoch": 0.0}
        return True

    def apply_order_event(self, order_link_id, kind, *, event_key=None,
                          filled_qty=None, avg_price=None, exchange_id="",
                          detail=None):
        rows = self._orders()
        row = rows.get(order_link_id)
        if row is None:
            return OrderEventResult(False, "", "", "NO_SUCH_ORDER")
        if event_key is not None:
            if event_key in self._order_event_keys:
                return OrderEventResult(False, row["status"], row["status"],
                                        "DUPLICATE_EVENT")
        t = ol.decide(row["status"], kind, recorded_qty=row["filled_qty"],
                      requested_qty=row["qty"], filled_qty=filled_qty)
        if not t.applied:
            return OrderEventResult(False, row["status"], row["status"], t.reason)
        before = row["status"]
        row["status"] = t.new_status or before
        if t.new_filled_qty is not None:
            row["filled_qty"] = max(row["filled_qty"], t.new_filled_qty)
            if avg_price is not None:
                row["avg_price"] = float(avg_price)
        if exchange_id:
            row["exchange_id"] = exchange_id
        if event_key is not None:
            self._order_event_keys.add(event_key)
        return OrderEventResult(True, row["status"], before, t.reason)

    def get_order(self, order_link_id):
        row = self._orders().get(order_link_id)
        return dict(row) if row else None

    def unresolved_carry_orders(self):
        return [dict(r) for r in self._orders().values()
                if r["status"] not in ol.TERMINAL]
