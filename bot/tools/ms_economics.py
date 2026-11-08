"""Economics of the multi-symbol layer: breakeven edge, base AND pessimistic scenarios, maker-first expected cost, a stress set, and a leverage ceiling.

Offline and pure. Every figure that is a fee, a spread allowance or a funding conversion comes from ``carry_costs`` (the authoritative vocabulary): this module introduces no
fee literal. What it adds are SCENARIO MAGNITUDES for the pessimistic case and the stress set (``PESSIMISTIC_FACTORS``, ``DEFAULT_STRESS_PARAMS``): they are labelled ASSUMED,
are stated shocks (not estimates), and the owner overrides them; a result built on them is never evidence of edge.

Rules:
  * Every input is an ``Inp(value, prov)``; ``value`` None is UNKNOWN. UNKNOWN is never zero: it propagates to ``None`` and to verdict ``UNKNOWN``.
  * A result inherits the WEAKEST provenance of its inputs; ``PASS`` requires weakest in {MEASURED, VENUE_STATED, DERIVED, MODELLED}; ASSUMED/UNKNOWN give ``NOT_PROVEN``.
  * WHAT ``assess`` ENFORCES BEFORE IT SAYS ``PASS`` (each is a separate cap to ``NOT_PROVEN``, with its own reason code; ``FAIL`` / ``UNKNOWN`` still win):
      - the provenance of every INPUT is in STRONG_ENOUGH (a label, not proof: MODELLED and DERIVED are stated by the caller);
      - ``scenario_prov`` (the provenance of the PESSIMISTIC factors) is in STRONG_ENOUGH. The built-in factors are ASSUMED shocks, so with them ``assess`` can never PASS: pass
        ``pessimistic_prov`` only for factors the owner has grounded in measurement;
      - a ZERO tail probability (operational failure, liquidation, and a maker fill probability of 1.0 = zero no-fill) is accepted only as MEASURED with a stated ``n_samples`` > 0
        (``ZERO_TAIL_PROBABILITY_NOT_MEASURED:<input>``): "0.0, MODELLED" is the absence of a model, not evidence;
      - the expected funding is NOT in-sample: ``EconInputs.funding_is_in_sample`` defaults to True (``FUNDING_IS_IN_SAMPLE``), and a funding input labelled DERIVED or whose ``basis``
        mentions TRAILING_IN_SAMPLE never supports PASS (a trailing mean of the data the candidate was chosen on is a description, not a forecast).
    So ``PASS`` is a necessary, not a sufficient, condition for edge. It does not look at whether the inputs are right.
  * Fee tier is VENUE_STATED (Bybit's published base tier) unless the caller passes a MEASURED figure from the owner account. A negative fee (rebate) is refused: never assumed.
  * A free execution (entry+exit fee == 0) that is not MEASURED can never PASS (``ZERO_FEE_NOT_MEASURED``).
  * Maker-first expected cost INCLUDES the probability of not being filled at all (``p_no_fill``); an unknown fill probability makes the expectation unknown, never 1.
  * Leverage is never an input to return. ``max_safe_leverage`` takes only liquidation geometry (stress move, maintenance margin, liquidation fee, buffer, venue/policy caps).
"""
from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carry_costs as CC  # noqa: E402
from ms_types import PROVS, weakest  # noqa: E402

STRONG_ENOUGH = ("MEASURED", "VENUE_STATED", "DERIVED", "MODELLED")
STYLES = ("taker", "maker")
_ONE_TIME = ("entry_fee", "exit_fee", "spread", "slippage", "adverse_selection", "hedge", "operational", "liquidation")


@dataclass(frozen=True)
class Inp:
    """One economic input: ``value`` None = UNKNOWN. ``n_samples`` (observations behind a MEASURED figure) and ``basis`` (free text, e.g. 'TRAILING_IN_SAMPLE') are optional."""
    value: Optional[float]
    prov: str = "UNKNOWN"
    n_samples: Optional[int] = None
    basis: str = ""

    def __post_init__(self) -> None:
        if self.prov not in PROVS:
            raise ValueError(f"provenance {self.prov!r}")
        if self.n_samples is not None and (type(self.n_samples) is not int or self.n_samples < 0):
            raise ValueError("n_samples must be a non-negative int or None")
        if not isinstance(self.basis, str):
            raise ValueError("basis must be a string")

    @property
    def known(self) -> bool:
        return isinstance(self.value, (int, float)) and not isinstance(self.value, bool) and math.isfinite(self.value)


UNKNOWN = Inp(None, "UNKNOWN")


def _prov_from_cc(p: Any) -> str:
    return {"venue_stated": "VENUE_STATED", "modelled": "MODELLED", "assumed": "ASSUMED"}.get(str(p), "UNKNOWN")


def fee_inputs(position_class: str, style: str, *, measured_round_trip_fee_bps: Optional[float] = None) -> Dict[str, Inp]:
    """Round-trip fee and spread as ``Inp`` for a position class, from ``carry_costs.position_class_costs`` (the authority). A MEASURED owner-account round-trip fee overrides
    the venue-stated tier (and is MEASURED). A rebate (negative) is refused. The taker spread is carry_costs' ASSUMED allowance until a measured spread is supplied."""
    c = CC.position_class_costs(position_class, style)
    if measured_round_trip_fee_bps is not None:
        m = measured_round_trip_fee_bps
        if isinstance(m, bool) or not isinstance(m, (int, float)) or not math.isfinite(m) or m < 0:
            raise ValueError("a measured fee must be a finite number >= 0: rebates are never assumed")
        fee = Inp(float(m), "MEASURED")
    elif c["fee_bps"] is None:
        fee = UNKNOWN
    else:
        fee = Inp(float(c["fee_bps"]), _prov_from_cc(c.get("fee_provenance")))
    spread = UNKNOWN if c["spread_bps"] is None else Inp(float(c["spread_bps"]), "ASSUMED" if style == "taker" else "MODELLED")
    return {"fee_round_trip_bps": fee, "spread_round_trip_bps": spread}


@dataclass(frozen=True)
class EconInputs:
    position_class: str
    style: str                                   # "taker" | "maker" (maker-first with the fallback below)
    hold_days: Inp
    funding_bps_per_print: Inp
    funding_interval_min: Optional[int]
    taker_fee_round_trip_bps: Inp
    maker_fee_round_trip_bps: Inp = UNKNOWN
    spread_round_trip_bps: Inp = UNKNOWN
    slippage_round_trip_bps: Inp = UNKNOWN
    adverse_selection_bps: Inp = UNKNOWN         # cost of being filled by informed flow, maker only, round trip
    p_fill_per_leg: Inp = UNKNOWN                # probability a resting leg fills before max_wait; maker only
    n_legs: int = 2
    cancel_cost_bps: Inp = UNKNOWN               # cost of a cancelled attempt that did not fill
    missed_edge_bps: Inp = UNKNOWN               # edge forgone when the attempt is abandoned instead of taken
    unfilled_policy: str = "taker_fallback"      # "taker_fallback" | "skip"
    hedge_cost_bps: Inp = UNKNOWN
    financing_apr: Inp = UNKNOWN                 # borrow; 0.0 only as an explicit statement for owned inventory
    opportunity_cost_apr: Inp = UNKNOWN
    op_failure_prob: Inp = UNKNOWN
    op_failure_loss_bps: Inp = UNKNOWN
    liquidation_prob: Inp = UNKNOWN
    liquidation_loss_bps: Inp = UNKNOWN
    funding_is_in_sample: bool = True            # True unless the owner states the expected funding is a forward / out-of-sample figure; True can never PASS


#: PESSIMISTIC scenario factors: ASSUMED stated shocks (not estimates). Multipliers on costs (>= 1), haircuts on income/probability (<= 1).
PESSIMISTIC_FACTORS = {"fee": 1.5, "spread": 2.0, "slippage": 2.0, "adverse_selection": 2.0, "cancel": 2.0, "p_fill": 0.5, "funding": 0.5, "financing": 1.5, "op_prob": 2.0,
                       "liq_prob": 2.0, "hedge": 2.0}
PESSIMISTIC_FACTORS_PROV = "ASSUMED"            # stated shocks, not estimates: with them ``assess`` can never PASS
IDENTITY_FACTORS = {k: 1.0 for k in PESSIMISTIC_FACTORS}
SCENARIOS = {"base": IDENTITY_FACTORS, "pessimistic": PESSIMISTIC_FACTORS}


def _scale(i: Inp, f: float) -> Inp:
    return Inp(i.value * f if i.known else None, i.prov)


def maker_first_expected_cost(*, maker_side_fee_bps: Inp, taker_side_fee_bps: Inp, spread_side_bps: Inp, slippage_side_bps: Inp, adverse_selection_side_bps: Inp,
                              p_fill_per_leg: Inp, n_legs: int, cancel_cost_bps: Inp, missed_edge_bps: Inp, unfilled_policy: str = "taker_fallback") -> Dict[str, Any]:
    """Expected cost (bps of hedged notional) of ONE maker-first side (an entry or an exit) INCLUDING the chance that nothing fills.

    All ``n_legs`` must fill: ``p_all = p_fill_per_leg ** n_legs`` (legs independent: MODELLED). ``p_no_fill = 1 - p_all``.
      filled      : maker fee + adverse selection
      not filled  : cancel cost, then either taker fallback (taker fee + spread + slippage) or abandon (missed edge)
    ``expected`` is None when any needed input is UNKNOWN, in particular when the fill probability is unknown (a maker that always fills is not an assumption made here).
    """
    if unfilled_policy not in ("taker_fallback", "skip"):
        raise ValueError("unfilled_policy")
    p = p_fill_per_leg
    need = [maker_side_fee_bps, adverse_selection_side_bps, cancel_cost_bps, p] + ([taker_side_fee_bps, spread_side_bps, slippage_side_bps] if unfilled_policy == "taker_fallback" else [missed_edge_bps])
    out: Dict[str, Any] = {"p_fill_all": None, "p_no_fill": None, "cost_if_filled_bps": None, "cost_if_not_filled_bps": None, "expected_bps": None,
                           "unfilled_policy": unfilled_policy, "prov": weakest(*(i.prov for i in need), "MODELLED")}
    if not all(i.known for i in need) or n_legs < 1 or not (0.0 <= p.value <= 1.0):
        out["unknown"] = [i for i, x in enumerate(need) if not x.known] + ([] if (not p.known or 0.0 <= p.value <= 1.0) else ["p_out_of_range"])
        return out
    p_all = p.value ** n_legs
    filled = maker_side_fee_bps.value + adverse_selection_side_bps.value
    if unfilled_policy == "taker_fallback":
        notf = cancel_cost_bps.value + taker_side_fee_bps.value + spread_side_bps.value + slippage_side_bps.value
    else:
        notf = cancel_cost_bps.value + missed_edge_bps.value
    out.update(p_fill_all=p_all, p_no_fill=1.0 - p_all, cost_if_filled_bps=filled, cost_if_not_filled_bps=notf, expected_bps=p_all * filled + (1.0 - p_all) * notf)
    return out


def _scenario(inp: EconInputs, f: Mapping[str, float]) -> Dict[str, Any]:
    unknown: List[str] = []
    comp: Dict[str, Optional[float]] = {}
    provs: List[str] = []

    def take(name: str, i: Inp) -> Optional[float]:
        provs.append(i.prov)
        if not i.known:
            unknown.append(name)
            return None
        return i.value

    hold = take("hold_days", inp.hold_days)
    taker_rt = _scale(inp.taker_fee_round_trip_bps, f["fee"])
    if inp.style == "taker":
        comp["entry_fee"] = None if not taker_rt.known else taker_rt.value / 2.0
        comp["exit_fee"] = comp["entry_fee"]
        if not taker_rt.known:
            unknown += ["entry_fee", "exit_fee"]
        provs.append(taker_rt.prov)
        comp["spread"] = take("spread", _scale(inp.spread_round_trip_bps, f["spread"]))
        comp["slippage"] = take("slippage", _scale(inp.slippage_round_trip_bps, f["slippage"]))
        comp["adverse_selection"] = 0.0                                   # a taker is not picked off while resting
        extra: Dict[str, Any] = {}
    else:
        pf = Inp(inp.p_fill_per_leg.value * f["p_fill"] if inp.p_fill_per_leg.known else None, inp.p_fill_per_leg.prov)
        maker_rt, spread_rt, slip_rt = _scale(inp.maker_fee_round_trip_bps, f["fee"]), _scale(inp.spread_round_trip_bps, f["spread"]), _scale(inp.slippage_round_trip_bps, f["slippage"])
        half = lambda i: Inp(i.value / 2.0 if i.known else None, i.prov)   # noqa: E731
        side = maker_first_expected_cost(maker_side_fee_bps=half(maker_rt), taker_side_fee_bps=half(taker_rt), spread_side_bps=half(spread_rt), slippage_side_bps=half(slip_rt),
                                         adverse_selection_side_bps=half(_scale(inp.adverse_selection_bps, f["adverse_selection"])), p_fill_per_leg=pf, n_legs=inp.n_legs,
                                         cancel_cost_bps=_scale(inp.cancel_cost_bps, f["cancel"]), missed_edge_bps=inp.missed_edge_bps, unfilled_policy=inp.unfilled_policy)
        provs.append(side["prov"])
        if side["expected_bps"] is None:
            unknown += ["maker_first_expected_cost"]
        comp["entry_fee"] = comp["exit_fee"] = side["expected_bps"]         # the whole expected side cost (fees, spread, slippage, adverse, cancel) is booked here
        comp["spread"] = comp["slippage"] = comp["adverse_selection"] = 0.0 if side["expected_bps"] is not None else None
        extra = {"maker_first_side": side}
    comp["hedge"] = take("hedge", _scale(inp.hedge_cost_bps, f["hedge"]))
    po, pl = take("op_failure_prob", _scale(inp.op_failure_prob, f["op_prob"])), take("op_failure_loss_bps", inp.op_failure_loss_bps)
    comp["operational"] = None if po is None or pl is None else po * pl
    qp, ql = take("liquidation_prob", _scale(inp.liquidation_prob, f["liq_prob"])), take("liquidation_loss_bps", inp.liquidation_loss_bps)
    comp["liquidation"] = None if qp is None or ql is None else qp * ql
    one_time = None if any(comp[k] is None for k in _ONE_TIME) else float(sum(comp[k] for k in _ONE_TIME))
    # time-proportional: financing (carry_costs convention: apr / 365 * 1e4 bps per day) and funding income (carry_costs conversion)
    fin_apr, opp_apr = take("financing_apr", _scale(inp.financing_apr, f["financing"])), take("opportunity_cost_apr", _scale(inp.opportunity_cost_apr, f["financing"]))
    fin_day = None if fin_apr is None or opp_apr is None else (fin_apr + opp_apr) / 365.0 * 1e4
    fund_print = take("funding_bps_per_print", _scale(inp.funding_bps_per_print, f["funding"] if (inp.funding_bps_per_print.value or 0) > 0 else 1.0))
    fund_day = CC.funding_bps_per_day(fund_print, inp.funding_interval_min) if fund_print is not None else None
    if fund_print is not None and fund_day is None:
        unknown.append("funding_interval_min")
    prints = None
    if hold is not None and hold > 0 and CC.interval_minutes_or_none(inp.funding_interval_min) is not None:
        prints = int(math.floor(hold * CC.MINUTES_PER_DAY / CC.interval_minutes_or_none(inp.funding_interval_min) + 1e-9))
    fin_total = None if fin_day is None or hold is None else fin_day * hold
    fund_total = None if fund_day is None or hold is None else fund_day * hold
    net = None if one_time is None or fin_total is None or fund_total is None else fund_total - one_time - fin_total
    req_total = None if one_time is None or fin_total is None else one_time + fin_total
    be_print = None if req_total is None or not prints else req_total / prints
    be_day = None if req_total is None or hold is None or hold <= 0 else req_total / hold
    be_hold = None
    if one_time is not None and fund_day is not None and fin_day is not None and CC.interval_minutes_or_none(inp.funding_interval_min) is not None:
        spare = fund_day - fin_day
        if spare > 0:
            n = CC.MINUTES_PER_DAY / inp.funding_interval_min
            be_hold = math.ceil(one_time / (spare / n) - 1e-9) / n          # whole prints only
    reasons: List[str] = []
    if inp.style == "taker" and comp["entry_fee"] == 0 and taker_rt.prov != "MEASURED":
        reasons.append("ZERO_FEE_NOT_MEASURED")
    if inp.style == "maker" and inp.maker_fee_round_trip_bps.known and inp.maker_fee_round_trip_bps.value == 0 and inp.maker_fee_round_trip_bps.prov != "MEASURED":
        reasons.append("ZERO_FEE_NOT_MEASURED")
    prov = weakest(*provs) if provs else "UNKNOWN"
    return dict(components=comp, one_time_cost_bps=one_time, financing_bps_per_day=fin_day, financing_bps_horizon=fin_total, funding_bps_per_day=fund_day,
                expected_funding_bps_horizon=fund_total, expected_net_bps=net, funding_prints_in_hold=prints, breakeven_gross_edge_bps_per_interval=be_print,
                breakeven_gross_edge_bps_per_day=be_day, breakeven_hold_days=be_hold, unknown=sorted(set(map(str, unknown))), prov=prov, reasons=reasons, **extra)


def breakeven_table(inp: EconInputs, hold_days_grid: Sequence[float]) -> List[Dict[str, Any]]:
    """Breakeven gross funding per interval for each holding period in ``hold_days_grid`` (caller-chosen), base scenario."""
    rows = []
    for h in hold_days_grid:
        s = _scenario(EconInputs(**{**inp.__dict__, "hold_days": Inp(float(h), inp.hold_days.prov)}), SCENARIOS["base"])
        rows.append({"hold_days": h, "breakeven_gross_edge_bps_per_interval": s["breakeven_gross_edge_bps_per_interval"], "funding_prints_in_hold": s["funding_prints_in_hold"]})
    return rows


def _is_zero_measured(i: Inp) -> bool:
    return i.prov == "MEASURED" and type(i.n_samples) is int and i.n_samples > 0


def evidence_caps(inp: EconInputs, scenario_prov: str) -> List[str]:
    """Reasons that stop a positive result from being ``PASS`` (see the module docstring). Pure; independent of the numbers."""
    caps: List[str] = []
    if scenario_prov not in STRONG_ENOUGH:
        caps.append("PESSIMISTIC_FACTORS_" + scenario_prov)
    tails = [("op_failure_prob", inp.op_failure_prob, 0.0), ("liquidation_prob", inp.liquidation_prob, 0.0)]
    if inp.style == "maker":
        tails.append(("p_no_fill", inp.p_fill_per_leg, 1.0))                  # a fill probability of 1.0 is a no-fill probability of 0
    for name, i, zero in tails:
        if i.known and i.value == zero and not _is_zero_measured(i):
            caps.append(f"ZERO_TAIL_PROBABILITY_NOT_MEASURED:{name}")
    f = inp.funding_bps_per_print
    if inp.funding_is_in_sample is not False or f.prov == "DERIVED" or "TRAILING_IN_SAMPLE" in f.basis.upper():
        caps.append("FUNDING_IS_IN_SAMPLE")
    return caps


def assess(inp: EconInputs, *, pessimistic_prov: str = PESSIMISTIC_FACTORS_PROV) -> Dict[str, Any]:
    """Base and PESSIMISTIC scenarios side by side and a verdict: PASS | FAIL | NOT_PROVEN | UNKNOWN. ``PASS`` is capped to ``NOT_PROVEN`` by ``evidence_caps``."""
    if inp.style not in STYLES or inp.position_class not in CC.POSITION_CLASSES:
        raise ValueError("style/position_class")
    if pessimistic_prov not in PROVS:
        raise ValueError(f"pessimistic_prov {pessimistic_prov!r}")
    out = {"base": _scenario(inp, SCENARIOS["base"]), "pessimistic": _scenario(inp, SCENARIOS["pessimistic"]), "scenario_prov": pessimistic_prov,
           "position_class": inp.position_class, "style": inp.style}
    b, p = out["base"], out["pessimistic"]
    reasons = list(dict.fromkeys(b["reasons"] + p["reasons"]))
    caps = evidence_caps(inp, pessimistic_prov)
    if b["expected_net_bps"] is None or p["expected_net_bps"] is None:
        verdict = "UNKNOWN"
        reasons += ["UNKNOWN_INPUT:" + ",".join(b["unknown"] or p["unknown"])]
    elif b["expected_net_bps"] <= 0 or p["expected_net_bps"] <= 0:
        verdict = "FAIL"
        reasons.append("BASE_NET_NOT_POSITIVE" if b["expected_net_bps"] <= 0 else "PESSIMISTIC_NET_NOT_POSITIVE")
    elif b["prov"] not in STRONG_ENOUGH or reasons or caps:
        verdict = "NOT_PROVEN"
        if b["prov"] not in STRONG_ENOUGH:
            reasons.append("WEAKEST_PROVENANCE:" + b["prov"])
        reasons += caps
    else:
        verdict = "PASS"
    if inp.position_class == "perp_only_unhedged":
        reasons.append("NOT_A_CARRY:unhedged_price_leg")
        verdict = "NOT_PROVEN" if verdict == "PASS" else verdict
    out.update(verdict=verdict, reasons=reasons, prov=b["prov"], evidence_caps=caps)
    return out


# ---------------------------------------------------------------------------------------------------- stress set

@dataclass(frozen=True)
class StressParams:
    """Stated shocks (fractions, bps, days). ``None`` = the shock is UNKNOWN and its scenario cannot be shown survivable. ASSUMED by construction: they are not forecasts."""
    funding_reversal_bps_per_day: Optional[float]
    funding_reversal_days: Optional[float]
    basis_widen_bps: Optional[float]
    liquidity_exit_slippage_bps: Optional[float]
    liquidation_fee_bps: Optional[float]
    correlated_move_frac: Optional[float]
    gap_frac: Optional[float]
    high_vol_move_frac: Optional[float]
    partial_hedge_ratio: Optional[float]
    partial_hedge_move_frac: Optional[float]
    ack_unknown_move_frac: Optional[float]
    outage_move_frac: Optional[float]
    outage_days: Optional[float]
    fee_tier_delta_bps_per_side: Optional[float]


DEFAULT_STRESS_PARAMS = StressParams(funding_reversal_bps_per_day=30.0, funding_reversal_days=3.0, basis_widen_bps=100.0, liquidity_exit_slippage_bps=50.0, liquidation_fee_bps=60.0,
                                     correlated_move_frac=0.15, gap_frac=0.10, high_vol_move_frac=0.12, partial_hedge_ratio=0.5, partial_hedge_move_frac=0.10,
                                     ack_unknown_move_frac=0.05, outage_move_frac=0.10, outage_days=1.0, fee_tier_delta_bps_per_side=5.0)
STRESS_SCENARIOS = ("funding_reversal", "basis_widening", "liquidity_disappearing", "correlated_liquidation", "gap", "high_volatility", "partial_hedge", "order_ack_uncertainty",
                    "api_outage", "fee_tier_change")


def _book_geometry(positions: Mapping[str, Mapping[str, Any]]) -> Optional[Dict[str, float]]:
    spot: Dict[str, float] = {}
    perp: Dict[str, float] = {}
    for k, p in positions.items():
        n = p.get("notional_usd") if isinstance(p, Mapping) else None
        if isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) or p.get("category") not in ("linear", "spot"):
            return None
        base = k[5:] if str(k).startswith("SPOT:") else str(k)
        d = spot if p["category"] == "spot" else perp
        d[base] = d.get(base, 0.0) + float(n)
    bases = set(spot) | set(perp)
    hedged = sum(min(abs(spot.get(b, 0.0)), abs(perp.get(b, 0.0))) for b in bases if spot.get(b, 0.0) * perp.get(b, 0.0) < 0)
    return {"gross": sum(abs(v) for v in list(spot.values()) + list(perp.values())), "perp_gross": sum(abs(v) for v in perp.values()),
            "perp_max": max([abs(v) for v in perp.values()] + [0.0]), "max_single": max([abs(v) for v in list(spot.values()) + list(perp.values())] + [0.0]),
            "hedged": hedged, "unhedged_net": sum(abs(spot.get(b, 0.0) + perp.get(b, 0.0)) for b in bases)}


def stress_set(book: Mapping[str, Any], policy: Mapping[str, Any], params: StressParams = DEFAULT_STRESS_PARAMS) -> Dict[str, Dict[str, Any]]:
    """Ten stress scenarios. ``book``: {equity_usd, positions {symbol: {category, notional_usd signed}}, maintenance_margin_frac}. Each result:
    {loss_usd, margin_drawdown_usd, equity_after_usd, leverage_after, survives, reasons}. ``survives`` is True only if, after max(loss, margin drawdown) (spot gains are NOT
    margin: the perp leg is funded in cash), equity stays above maintenance margin of the perp legs and leverage stays within ``policy['max_leverage']``. None = unknown input."""
    g = _book_geometry(book.get("positions") or {}) if isinstance(book.get("positions"), Mapping) else None
    eq, mmr = book.get("equity_usd"), book.get("maintenance_margin_frac")
    lev_cap = policy.get("max_leverage") if isinstance(policy, Mapping) else None
    ok_book = g is not None and isinstance(eq, (int, float)) and not isinstance(eq, bool) and math.isfinite(eq) and eq > 0
    P = params

    def f(*names: str) -> Optional[List[float]]:
        vals = [getattr(P, n) for n in names]
        return None if any(v is None or isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in vals) else [float(v) for v in vals]

    def res(loss: Optional[float], margin: Optional[float], extra_reasons: Sequence[str] = ()) -> Dict[str, Any]:
        reasons = list(extra_reasons)
        if not ok_book:
            reasons.append("BOOK_UNKNOWN")
        if loss is None or margin is None:
            reasons.append("STRESS_PARAM_UNKNOWN")
        if reasons:
            return {"loss_usd": loss, "margin_drawdown_usd": margin, "equity_after_usd": None, "leverage_after": None, "survives": None, "reasons": reasons}
        after = eq - max(loss, margin)
        lev_after = g["gross"] / after if after > 0 else math.inf
        why: List[str] = []
        if after <= 0:
            why.append("EQUITY_WIPED")
        if not isinstance(mmr, (int, float)) or isinstance(mmr, bool) or not math.isfinite(mmr) or mmr < 0:
            return {"loss_usd": loss, "margin_drawdown_usd": margin, "equity_after_usd": after, "leverage_after": lev_after, "survives": None,
                    "reasons": why + ["MAINTENANCE_MARGIN_UNKNOWN"]}
        if after <= mmr * g["perp_gross"]:
            why.append("LIQUIDATED")
        if not isinstance(lev_cap, (int, float)) or isinstance(lev_cap, bool) or lev_cap < 1:
            return {"loss_usd": loss, "margin_drawdown_usd": margin, "equity_after_usd": after, "leverage_after": lev_after, "survives": None, "reasons": why + ["POLICY_LEVERAGE_UNKNOWN"]}
        if lev_after > lev_cap:
            why.append("LEVERAGE_ABOVE_POLICY")
        return {"loss_usd": loss, "margin_drawdown_usd": margin, "equity_after_usd": after, "leverage_after": lev_after, "survives": not why, "reasons": why}

    def calc(names: Tuple[str, ...], fn) -> Dict[str, Any]:   # noqa: ANN001
        v = f(*names)
        if v is None or g is None:
            return res(None, None)
        loss, margin = fn(*v)
        return res(loss, margin)

    out: Dict[str, Dict[str, Any]] = {}
    out["funding_reversal"] = calc(("funding_reversal_bps_per_day", "funding_reversal_days"), lambda b, d: (g["perp_gross"] * b / 1e4 * d,) * 2)
    out["basis_widening"] = calc(("basis_widen_bps",), lambda b: (g["hedged"] * b / 1e4,) * 2)
    out["liquidity_disappearing"] = calc(("liquidity_exit_slippage_bps",), lambda b: (g["gross"] * b / 1e4,) * 2)
    out["correlated_liquidation"] = calc(("liquidity_exit_slippage_bps", "liquidation_fee_bps", "correlated_move_frac"),
                                         lambda s, lf, m: (g["gross"] * (s + lf) / 1e4 + g["unhedged_net"] * m, g["perp_gross"] * m + g["gross"] * (s + lf) / 1e4))
    out["gap"] = calc(("gap_frac",), lambda m: (g["unhedged_net"] * m, g["perp_gross"] * m))
    out["high_volatility"] = calc(("high_vol_move_frac",), lambda m: (g["unhedged_net"] * m, g["perp_gross"] * m))
    out["partial_hedge"] = calc(("partial_hedge_ratio", "partial_hedge_move_frac"),
                                lambda r, m: ((g["unhedged_net"] + (1.0 - min(r, 1.0)) * g["hedged"]) * m, g["perp_gross"] * m))
    out["order_ack_uncertainty"] = calc(("ack_unknown_move_frac",), lambda m: (g["max_single"] * m, g["perp_max"] * m))
    out["api_outage"] = calc(("outage_move_frac", "funding_reversal_bps_per_day", "outage_days"),
                             lambda m, b, d: (g["unhedged_net"] * m + g["perp_gross"] * b / 1e4 * d, g["perp_gross"] * m + g["perp_gross"] * b / 1e4 * d))
    out["fee_tier_change"] = calc(("fee_tier_delta_bps_per_side",), lambda b: (g["gross"] * b * 2 / 1e4, 0.0))
    assert tuple(out) == STRESS_SCENARIOS
    return out


def stress_summary(results: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """The portfolio survives only if EVERY scenario is explicitly True. Unknown is not survival."""
    bad = sorted(k for k, v in results.items() if v.get("survives") is not True)
    return {"all_survive": not bad and set(results) == set(STRESS_SCENARIOS), "failing_or_unknown": bad}


# ---------------------------------------------------------------------------------------------------- leverage

def max_safe_leverage(*, stress_adverse_move_frac: Optional[float], maintenance_margin_frac: Optional[float], liquidation_fee_frac: Optional[float] = 0.0,
                      buffer_frac: Optional[float] = 0.0, venue_max_leverage: Optional[float] = None, policy_max_leverage: Optional[float] = None) -> Optional[float]:
    """The most leverage whose liquidation distance still exceeds the stress move: ``1 / (move + maintenance margin + liquidation fee + buffer)``, capped by the venue and the
    policy, rounded DOWN to 0.01. It takes NO return, edge or funding input by design: leverage is never chosen to manufacture return. None if any geometry input is unknown.
    A value below 1 means the book cannot be held safely even unlevered against that stress."""
    vals = (stress_adverse_move_frac, maintenance_margin_frac, liquidation_fee_frac, buffer_frac)
    if any(v is None or isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in vals):
        return None
    d = sum(vals)
    if d <= 0:
        return None                              # zero distance would be infinite leverage: that is an unknown, not a licence
    lev = 1.0 / d
    for cap in (venue_max_leverage, policy_max_leverage):
        if cap is not None:
            if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not math.isfinite(cap) or cap <= 0:
                return None
            lev = min(lev, float(cap))
    return math.floor(lev * 100 + 1e-9) / 100.0
