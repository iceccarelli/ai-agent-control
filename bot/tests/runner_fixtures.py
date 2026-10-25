"""Offline reports shaped like what `carry_preflight.py` prints and what
`evidence_reconcile.py` writes, for the runner tests. No network, no venue."""
from __future__ import annotations

from typing import Any, Dict, Optional

DECISION = {
    "schema": "entry_decision/1", "execution_mode": "overlay",
    "execution_style": "taker", "fee_rates": {"perp_taker_bps": 5.5},
    "round_trip_bps_estimate": 11.0,
    "funding": {"smoothed_bps_per_8h_print": 1.0, "prints_in_history": 8,
                "newest_print_age_hours": 2.0},
    "gate": {"state": "PASS", "reason": "CARRY_CLEARS_COSTS_AND_BASIS"},
    "hold_gate": {"settlements_needed": 11, "settlements_budget": 17},
}


def preflight_report(state: str = "PASS", *, reason: Optional[str] = None,
                     verdict: str = "PREFLIGHT_OK", blockers=None,
                     order_gate_open: bool = False,
                     venue: str = "testnet", use_testnet: bool = True,
                     decision: Optional[Dict[str, Any]] = None
                     ) -> Dict[str, Any]:
    """`state`: PASS | REFUSED | UNAVAILABLE."""
    reasons = {"PASS": "CARRY_CLEARS_COSTS_AND_BASIS",
               "REFUSED": "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD",
               "UNAVAILABLE": "funding history unreadable"}
    return {
        "verdict": verdict, "venue": venue, "use_testnet": use_testnet,
        "paper_trading": not order_gate_open,
        "order_gate_would_allow_orders": order_gate_open,
        "blockers": list(blockers or []),
        "entry_economics": {
            "state": f"ENTRY_ECONOMICS_{state}",
            "reason": reasons[state] if reason is None else reason,
            "decision": decision if decision is not None else dict(
                DECISION, gate={"state": state,
                                "reason": reasons[state] if reason is None else reason}),
        },
    }


def reconciliation(provenance: str = "VENUE_RECONCILED",
                   resolved: bool = True) -> Dict[str, Any]:
    return {"schema": "evidence_reconciliation/1",
            "carry_cycle": [{"provenance": provenance, "resolved": resolved}]}
