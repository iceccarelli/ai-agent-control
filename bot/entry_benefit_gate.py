"""The entry net-benefit requirement, as an ADDITIONAL gate on the carry entry.

The mechanical gate (`carry_costs.entry_gate`) and the pair gate still decide
first and are never replaced. This gate then asks a different question: with
what this book has actually paid to execute, does the expected income over a
conservative hold exceed every cost by more than its own uncertainty? It keeps
five layers apart, because passing one says nothing about the next:

  mechanical            the production cost gate (decided before this runs)
  entry benefit         costs supported by real inputs, net benefit > 0 and
                        beyond one sigma of the income's uncertainty
  strategy evidence     an ALLOCATE verdict from `strategy_evidence` (none is
                        supplied by this repository today, so this is refused)
  capital allocation    NOT decided here (the capital reservation follows)
  order permission      NOT decided here (the order gates follow)

A missing or unreliable input is a refusal with its name, never zero. The gate
only ever sits in front of an ENTRY: exits do not pass through it.

`mode` is "enforce" (refuse) or "observe" (record what it WOULD refuse and let
the entry continue, loudly marked): observe exists so an operator can see the
verdicts accumulate; it is an explicit choice, never a default.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional

import carry_costs as _costs
import execution_cost_estimate as _est
import md_economics as _mde
import strategy_evidence as _se

ENFORCE, OBSERVE = "enforce", "observe"
MODES = (ENFORCE, OBSERVE)

BENEFIT_UNAVAILABLE = "ENTRY_BENEFIT_UNAVAILABLE"
BENEFIT_NOT_SUPPORTED = "ENTRY_BENEFIT_NOT_SUPPORTED"
EVIDENCE_INSUFFICIENT = "STRATEGY_EVIDENCE_INSUFFICIENT"
SETTLEMENTS_PER_DAY = _costs.FUNDING_PERIODS_PER_DAY


def adverse_basis_bps(mark: Any, spot: Any) -> Optional[float]:
    """What the entry basis gives back when it converges to zero, in bps.

    Short perp / long spot earns `entry_basis - exit_basis` (see
    `carry_costs.hedge_basis_pnl_bps`), basis = perp/spot - 1. A perp BELOW spot
    (negative basis) loses its whole discount on convergence; a premium may gain
    but is never credited, so it costs 0. None when either price is unreadable:
    an unknown basis is not a small one."""
    if not (_finite(mark) and _finite(spot)) or mark <= 0 or spot <= 0:
        return None
    return max(0.0, -_costs.basis_bps(float(mark), float(spot)))


def _finite(x: Any) -> bool:
    return (not isinstance(x, bool) and x is not None
            and isinstance(x, (int, float)) and math.isfinite(x))


@dataclass(frozen=True)
class EntryBenefitResult:
    allowed: bool            # False = the entry is refused (enforce) ...
    would_allow: bool        # ... and this is the verdict whatever the mode
    reason: str              # "" when it would allow
    enforcing: bool
    trace: Mapping[str, Any]


class EntryBenefitGate:
    def __init__(self, *, mode: str = ENFORCE,
                 income_haircut_fraction: float = 0.0,
                 evidence_provider: Optional[Callable[[], Any]] = None,
                 strategy_id: str = "btc_funding_carry",
                 cost_policy: _est.CostSamplePolicy = _est.DEFAULT_POLICY,
                 latency_allowance_bps: Optional[float] = None) -> None:
        if mode not in MODES:
            raise ValueError(f"entry-benefit mode must be one of {MODES}, "
                             f"not {mode!r}")
        if not (_finite(income_haircut_fraction)
                and 0.0 <= income_haircut_fraction <= 1.0):
            raise ValueError("income_haircut_fraction must be within [0, 1]")
        cost_policy.validate()
        self.mode = mode
        self.income_haircut_fraction = float(income_haircut_fraction)
        self.evidence_provider = evidence_provider
        self.strategy_id = str(strategy_id)
        self.cost_policy = cost_policy
        #: The owner's allowance, in bps of notional over the round trip, for the
        #: price moving between the decision and the fill. None = not supplied:
        #: the live-book walk is then only a lower bound and is not used as a cost.
        if latency_allowance_bps is not None and not (
                _finite(latency_allowance_bps) and latency_allowance_bps >= 0):
            raise ValueError("latency_allowance_bps must be a finite number >= 0")
        self.latency_allowance_bps = (None if latency_allowance_bps is None
                                      else float(latency_allowance_bps))

    # -- identity of the rule, so a trace can be tied to what produced it -------
    def fingerprint(self) -> str:
        body = {
            "mode": self.mode, "haircut": self.income_haircut_fraction,
            "strategy_id": self.strategy_id,
            "hold_days": _costs.DEMONSTRATED_HOLD_DAYS,
            "basis_term": "discount_converges_to_zero_premium_not_credited",
            "min_sigma": _se.MIN_SIGMA_MULTIPLE,
            "settlements_per_day": SETTLEMENTS_PER_DAY,
            "cost_policy": vars(self.cost_policy),
            "latency_allowance_bps": self.latency_allowance_bps,
            "depth_source": _mde.DEPTH_SOURCE,
            "evidence_policy": vars(_se.DEFAULT_POLICY)}
        raw = json.dumps(body, sort_keys=True, default=str).encode()
        return hashlib.sha256(raw).hexdigest()[:16]

    def _with_live_depth(self, est: Dict[str, Any], snapshot: Any, execution_mode: str,
                         notional_usd: Any, mark: Any):
        """Fold the live book's measured costs into the execution-cost estimate.

        Where the book's own fills AND the live book both measure a cost, the WORSE
        of the two is used (a past fill cannot show what the book offers now, and
        the book cannot show what fills actually cost). Where only the live book
        measures it, it is used with the owner's latency allowance, and is
        UNAVAILABLE without one. A snapshot without depth leaves `est` untouched."""
        detail = getattr(snapshot, "detail", None)
        depth = detail.get("depth") if isinstance(detail, dict) else None
        if not isinstance(depth, dict):
            return est, None
        dc = _mde.depth_costs(depth, mode=str(execution_mode), notional_usd=notional_usd,
                              ref_price=mark, latency_allowance_bps=self.latency_allowance_bps)
        trace = dc.as_dict()
        merged = dict(est)
        if dc.status != _mde.MEASURED:
            trace["used"] = False
            return merged, trace
        if est.get("status") == _est.MEASURED:
            merged["spread_bps"] = max(float(est["spread_bps"]), dc.spread_bps)
            merged["slippage_bps"] = max(float(est["slippage_bps"]),
                                         dc.slippage_bps + dc.latency_allowance_bps)
            merged["cost_source"] = "WORSE_OF_OWN_FILLS_AND_LIVE_BOOK"
        else:
            merged.update(status=_est.MEASURED, reasons=[], spread_bps=dc.spread_bps,
                          slippage_bps=dc.slippage_bps + dc.latency_allowance_bps,
                          cost_source=_mde.DEPTH_SOURCE)
        trace["used"] = True
        return merged, trace

    def check(self, *, now_ms: int, entries: Any, execution_mode: str,
              perp_symbol: str, spot_symbol: str, mark: float, spot: float,
              funding_history: List[float], last_print_ms: Optional[int],
              round_trip_bps: Any, borrow_apr: Any, opportunity_cost_apr: Any,
              notional_usd: Any, execution_style: str = "taker",
              mechanical_reason: str = "", snapshot: Any = None) -> EntryBenefitResult:
        """The verdict for opening NOW. Pure given its arguments."""
        enforcing = self.mode == ENFORCE
        history = [float(x) for x in funding_history if _finite(x)]
        smoothed = _costs.ewma_funding_bps(history)
        hold = float(_costs.DEMONSTRATED_HOLD_DAYS)
        n_settle = SETTLEMENTS_PER_DAY * hold

        # -- income and its uncertainty, from the same prints the gate used -------
        sd = None
        if len(history) >= 2:
            m = sum(history) / len(history)
            sd = math.sqrt(sum((x - m) ** 2 for x in history) / (len(history) - 1))
        # Settlements within a hold are not independent; treating them as fully
        # correlated (sd * n) is the conservative reading, not a fitted one.
        uncertainty = None if sd is None else sd * n_settle

        est = _est.estimate_round_trip(
            entries, as_of_ms=now_ms, execution_mode=execution_mode,
            perp_symbol=perp_symbol, spot_symbol=spot_symbol,
            planned_notional_usd=notional_usd if _finite(notional_usd) else 0.0,
            order_types=(("market", "post_only")
                         if str(execution_style) == "maker_first"
                         else ("market",)),
            policy=self.cost_policy)

        est, depth_trace = self._with_live_depth(est, snapshot, execution_mode, notional_usd, mark)

        verdict_obj = None
        if self.evidence_provider is not None:
            try:
                verdict_obj = self.evidence_provider()
            except Exception:  # noqa: BLE001 - an unreadable verdict is none
                verdict_obj = None

        decision = _se.evaluate_entry_benefit(
            expected_funding_bps_per_settlement=smoothed,
            hold_days=hold, notional_usd=notional_usd,
            round_trip_fee_bps=round_trip_bps,
            spread_bps=est["spread_bps"],
            expected_slippage_bps=est["slippage_bps"],
            borrow_apr=borrow_apr, opportunity_cost_apr=opportunity_cost_apr,
            income_haircut_fraction=self.income_haircut_fraction,
            income_uncertainty_bps=uncertainty, evidence=verdict_obj,
            settlements_per_day=SETTLEMENTS_PER_DAY,
            basis_convergence_bps=adverse_basis_bps(mark, spot))

        # `evaluate_entry_benefit` labels a pass "NET_BENEFIT_POSITIVE"; that is
        # not a refusal reason, and the evidence refusal is judged separately.
        benefit_reasons = [r for r in decision.reasons
                           if not r.startswith((_se.REFUSE_NO_EVIDENCE,
                                                "NET_BENEFIT_POSITIVE"))]
        unavailable = [r for r in benefit_reasons
                       if r.startswith(_se.REFUSE_UNAVAILABLE)]
        # The estimator's unavailability is the precise cause when the spread
        # and slippage are what is missing; carry its reasons through.
        if est["status"] != _est.MEASURED and not unavailable:
            unavailable.append(f"{_se.REFUSE_UNAVAILABLE}:execution_cost")
        benefit_ok = not benefit_reasons and est["status"] == _est.MEASURED
        # A verdict counts only when it is for THIS strategy: a verdict about
        # another one (or a cached object of unknown origin) unlocks nothing.
        evidence_ok = (isinstance(verdict_obj, _se.EvidenceVerdict)
                       and verdict_obj.verdict == _se.ALLOCATE
                       and verdict_obj.strategy_id == self.strategy_id)

        if unavailable:
            reason = BENEFIT_UNAVAILABLE
        elif not benefit_ok:
            reason = BENEFIT_NOT_SUPPORTED
        elif not evidence_ok:
            reason = EVIDENCE_INSUFFICIENT
        else:
            reason = ""

        evidence_state = (verdict_obj.verdict if isinstance(
            verdict_obj, _se.EvidenceVerdict) else "NO_VERDICT_SUPPLIED")
        trace: Dict[str, Any] = {
            "outcome": reason or "ENTRY_ELIGIBLE",
            "mode": self.mode, "would_refuse": bool(reason),
            "as_of_ms": int(now_ms),
            "instrument": {"perp": perp_symbol, "spot": spot_symbol,
                           "execution_mode": execution_mode},
            "fingerprint": self.fingerprint(),
            "layers": {
                "mechanical": {"passed": True, "reason": mechanical_reason,
                               "note": "this gate is reached only after the production "
                                       "cost gate and the pair gate passed (by "
                                       "position in the call path); it does "
                                       "not re-decide or replace them"},
                "entry_benefit": {
                    "passed": benefit_ok, "reasons": benefit_reasons,
                    "net_benefit_bps": decision.net_benefit_bps,
                    "net_benefit_usd": decision.net_benefit_usd,
                    "components_bps": dict(decision.components_bps),
                    "min_sigma_multiple": _se.MIN_SIGMA_MULTIPLE},
                "strategy_evidence": {
                    "passed": evidence_ok, "classification": evidence_state,
                    "expected_strategy_id": self.strategy_id,
                    "verdict_strategy_id": getattr(verdict_obj, "strategy_id", None),
                    "reasons": list(getattr(verdict_obj, "reasons", ()) or ())},
                "capital_allocation": {"passed": None,
                                       "note": "not decided here; the "
                                               "reservation follows"},
                "order_permission": {"passed": None,
                                     "note": "not decided here; the order "
                                             "gates follow"}},
            "inputs": {
                "units": {"funding": "bps per 8h settlement", "costs": "bps of "
                          "position notional", "hold": "days"},
                "smoothed_funding_bps": smoothed,
                "funding_sample_count": len(history),
                "funding_last_print_ms": last_print_ms,
                "funding_age_s": (None if last_print_ms is None else
                                  max(0, now_ms - int(last_print_ms)) / 1000.0),
                "income_uncertainty_bps": uncertainty,
                "entry_basis_bps": (None if adverse_basis_bps(mark, spot) is None
                                    else _costs.basis_bps(mark, spot)),
                "hold_days": hold, "round_trip_fee_bps": round_trip_bps,
                "borrow_apr": borrow_apr,
                "opportunity_cost_apr": opportunity_cost_apr,
                "income_haircut_fraction": self.income_haircut_fraction,
                "notional_usd": notional_usd,
                "execution_cost": {
                    "status": est["status"], "reasons": est["reasons"],
                    "spread_bps": est["spread_bps"],
                    "slippage_bps": est["slippage_bps"],
                    "cost_source": est.get("cost_source", "OWN_FILLS"),
                    "live_depth": depth_trace,
                    "legs": [{k: l[k] for k in ("leg", "status", "n",
                                                "n_in_size_band", "period_ms",
                                                "newest_age_days", "rejected")}
                             for l in est["legs"]],
                    "policy": est["policy"]}}}
        would_allow = not reason
        return EntryBenefitResult(
            allowed=would_allow or not enforcing, would_allow=would_allow,
            reason=reason, enforcing=enforcing, trace=trace)
