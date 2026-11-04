"""Candidate entry rule `candidate_carry_depth/v1` (declared 2026-11-04).

A SEPARATE, VERSIONED candidate. It does not change, retune or stand in for the
frozen `funding_carry_fade_v1` or for the production carry gates; it is evaluated
beside them, forward only, and it can never place an order: its output is a
recommendation record, and every real entry still passes the production cost gate,
the pair gate, the entry-benefit gate with a strategy-evidence verdict, capital
allocation and the order gates.

What it adds over the baseline, and nothing else:

  1. costs measured from the LIVE order book (`md_economics`), not from past fills
     or a mid price, with the unknowns (latency, account fee tier, collateral)
     left unknown and the entry refused until they are stated;
  2. a forecast-consistency check: the venue's forecast for the NEXT settlement
     must be positive and not below the smoothed history by more than
     `forecast_floor_sd_multiple` standard deviations of the settled prints. A
     forecast that has already turned says the regime the history describes may be
     over. The multiple is a declared hypothesis, not a fitted value.

NULL HYPOTHESIS (stated before any forward observation): this rule adds no net
return over the production gate. It is held to that null; there is no claim of
edge, no threshold was tuned on any holdout, and nothing here reads one.
Evaluation is forward and independent (reconciled cycles only, after all costs,
against holding BTC on the same capital basis), with settlements treated as
dependent samples.

Pure: no I/O, no clock, no network.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import carry_costs as _costs
import md_economics as ME
import md_state as MS
import strategy_evidence as _se

VERSION = "candidate_carry_depth/v1"
DECLARED = "2026-11-04"

PARAMS: Dict[str, Any] = {
    "hold_days": float(_costs.DEMONSTRATED_HOLD_DAYS),
    "min_sigma_multiple": float(_se.MIN_SIGMA_MULTIPLE),
    "min_settled_prints": int(_costs.EWMA_MIN_PRINTS),
    "forecast_floor_sd_multiple": 1.0,
    "forecast_must_be_positive": True,
    "cost_source": ME.DEPTH_SOURCE,
}

REQUIRES_BEFORE_ANY_ORDER = (
    "production cost gate", "pair gate (CarryRisk)", "entry-benefit gate with a strategy-evidence "
    "ALLOCATE verdict", "capital allocation", "order gates (kill switch, arming chain)",
    "owner-approved limits")

ENTER, REFUSE = "ENTER", "REFUSE"


def params_hash() -> str:
    body = json.dumps({"version": VERSION, "params": PARAMS}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


@dataclass(frozen=True)
class Decision:
    action: str                                   # ENTER | REFUSE (a recommendation only)
    reasons: Tuple[str, ...]
    economics: ME.EntryEconomics
    forecast_bps: Optional[float]
    smoothed_bps: Optional[float]
    version: str = VERSION
    params_hash: str = ""
    simulated: bool = True                         # never an order, never cash

    def to_record(self) -> Dict[str, Any]:
        e = self.economics
        return {"version": self.version, "params_hash": self.params_hash, "action": self.action,
                "reasons": list(self.reasons), "simulated": True, "order_placed": False,
                "forecast_bps": self.forecast_bps, "smoothed_bps": self.smoothed_bps,
                "net_benefit_bps": e.net_benefit_bps, "complete": e.complete, "missing": list(e.missing),
                "classes": e.by_class(), "depth": e.depth.as_dict(),
                "requires_before_any_order": list(REQUIRES_BEFORE_ANY_ORDER)}


def decide(view: MS.MarketView, *, mode: str, notional_usd: float,
           funding_history_bps: Sequence[float], borrow_apr: Optional[float],
           opportunity_cost_apr: Optional[float], latency_allowance_bps: Optional[float],
           fees: Optional[ME.FeeSchedule] = None, income_haircut_fraction: float = 0.0,
           account: Optional[Mapping[str, Any]] = None) -> Decision:
    reasons: List[str] = []
    h = params_hash()

    reasons += [f"DATA:{r}" for r in view.refusals(MS.ENTRY)]
    econ = ME.build_entry_economics(
        view, mode=mode, notional_usd=notional_usd, funding_history_bps=funding_history_bps, fees=fees,
        hold_days=PARAMS["hold_days"], borrow_apr=borrow_apr, opportunity_cost_apr=opportunity_cost_apr,
        income_haircut_fraction=income_haircut_fraction, latency_allowance_bps=latency_allowance_bps,
        account=account)
    if econ.missing or not econ.complete:
        # `not complete` with nothing named missing is itself a defect, never a pass
        reasons.append("INCOMPLETE:" + (",".join(econ.missing) or "UNNAMED"))
    if len([x for x in funding_history_bps if isinstance(x, (int, float)) and math.isfinite(x)]) \
            < PARAMS["min_settled_prints"]:
        reasons.append(f"TOO_FEW_SETTLED_PRINTS:<{PARAMS['min_settled_prints']}")

    forecast = None if view.funding_rate is None else view.funding_rate * 1e4
    smoothed = next((i.value for i in econ.items if i.name == "expected_funding_bps_per_settlement"), None)
    if econ.decision is not None:
        reasons += [r for r in econ.decision.reasons
                    if not r.startswith((_se.REFUSE_NO_EVIDENCE, "NET_BENEFIT_POSITIVE"))]
    if forecast is None:
        reasons.append("FORECAST_UNAVAILABLE")
    else:
        if PARAMS["forecast_must_be_positive"] and not forecast > 0:
            reasons.append(f"FORECAST_NOT_POSITIVE:{forecast:+.3f}bps")
        sd = _sd(funding_history_bps)
        if smoothed is not None and sd is not None:
            floor = smoothed - PARAMS["forecast_floor_sd_multiple"] * sd
            if forecast < floor:
                reasons.append(f"FORECAST_BELOW_HISTORY:{forecast:+.3f}<{floor:+.3f}bps")
    return Decision(ENTER if not reasons else REFUSE, tuple(reasons), econ, forecast, smoothed,
                    VERSION, h)


def _sd(xs: Sequence[float]) -> Optional[float]:
    v = [float(x) for x in xs if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)]
    if len(v) < 2:
        return None
    m = sum(v) / len(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))
