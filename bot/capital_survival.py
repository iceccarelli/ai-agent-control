"""OFFLINE leverage and portfolio-survival simulation for a delta-neutral BTC funding carry.

Short BTCUSDT linear perpetual against BTC inventory used as collateral, studied at leverage
1x, 1.25x, 1.5x, 2x, 3x, 5x and 10x. Pure arithmetic on DECLARED inputs: no I/O, no clock, no
network, no unseeded randomness, no import-time side effects. It imports only the standard
library, ``trade_economics`` and ``evidence_stats``, so it cannot reach an order, broker, client,
store, configuration or venue code (a test pins that). It can NEVER size a real position: it only
simulates, every output carries ``authorizes_trading = False``, and levels above 1x are
analysis only, labelled NOT_RECOMMENDABLE. There is no automatic scaling logic of any kind.

LEVERAGE, precisely
  L = N / C, where N is the HEDGED NOTIONAL (the perp short; the BTC long it hedges is the same
  size) and C is the own capital that carries the hedge: BTC inventory at its full, un-haircut
  market value. The FULLY ALLOCATED ECONOMIC CAPITAL is E = C + R, R being the idle reserve (the
  ``trade_economics`` definition). With R = 0 this is exactly hedged notional / fully allocated
  economic capital. With R > 0 the reserve sits OUTSIDE L but INSIDE every return denominator,
  because a literal N / E could not equal 1.0 without borrowing the reserve itself; the literal
  ratio is reported next to L as ``notional_over_economic_capital`` so the reserve's drag shows.
  ``recommend_exposure`` speaks the literal ratio: a cap of 0.4 means N <= 0.4 E.

HOW EACH PIECE ENTERS (per leverage L, hedge capital C, haircut h, rates im and mm)
  hedged notional       N = L C                 the BTC long is N, so the perp short is N
  FINANCING             F = N - C = (L-1) C     the extra notional is bought with borrowed USDT;
                                                zero at 1x. The rate is UNKNOWN unless the caller
                                                supplies SCENARIO rates: it is never defaulted
  collateral haircut    h N                     the margin credit the venue withholds on the BTC
  margin equity         E0 = N (1-h) - F        haircut collateral net of the borrowing
                                                (= C - h N; extra USDT margin bought with
                                                borrowed money changes nothing, so none is modelled)
  initial margin        IM = im N               the book OPENS only if E0 >= IM
  maintenance margin    MM = mm N               liquidation when equity <= MM at the marked notional
  reserve               R = rho N               off-venue idle USDT. It is NOT in E0 and NOT counted
                                                in any survival test (a gap does not wait for a manual
                                                top-up); ``survives_with_reserve_topup`` and
                                                ``with_reserve_topup_pct`` show what it would add
  returns               net / N and net / E     computed by ``trade_economics.compute``; the
                                                per-notional return is identical at every L, only
                                                the return on capital moves (leverage is not an edge)

LIQUIDATION HEADROOM = the adverse perp-vs-collateral move that exhausts equity: the perp mark
rises by x while the collateral's offsetting gain is NOT credited, until E0 - x N = mm N (1+x):
x = (E0 - MM) / (N (1+mm)). The mirror (collateral falls by x, the perp's gain not credited) is
(E0 - MM) / (N (1-h)). ``liquidation_headroom_pct`` is the smaller. Both are CONSERVATIVE: they
ignore that a working hedge offsets the move.

VENUE RULES are ASSUMED_NOT_VENUE_VERIFIED: the built-in haircut, margin rates and liquidation
fee are placeholders. A caller's ``venue_rules`` is VENUE_VERIFIED only if it is marked
``verified is True`` AND names a ``source`` AND differs from the placeholders; anything else stays
ASSUMED. The label travels into every output and is re-derived, never trusted, by the
recommendation.

STRESS SCENARIOS (``StressScenario``; the default parameters are ILLUSTRATIVE_NOT_CALIBRATED).
Every scenario is evaluated as a book that would have to be CLOSED at the stress point: equity
= E0 + perp mark + collateral credit change + funding run - exit cost x multiplier - financing
accrual, tested against the maintenance margin at the marked notional. With a perp return
Pr = (1+gap)(1+basis) the marks are -N (Pr-1) on the perp and (1-h) N gap on the collateral. In a
gap during which the hedge CANNOT BE REBALANCED only the adverse leg is marked and the offsetting
gain is withheld (a stress assumption, not a venue behaviour). Funding reversal is a run of
negative prints paid by the short. CORRELATED scenarios stack gap, basis, funding and an exit-cost
multiplier at once; they are worst-case stackings, not forecasts, and the signs are not claimed to
co-occur in a real market.

PATH RISK is a seeded block bootstrap of caller-supplied history (funding prints, daily basis
levels, daily returns). Without history it returns INSUFFICIENT; it never invents a distribution.
It reports VaR/CVaR (95%, 99%) of the horizon loss and the maximum drawdown, as a percent of
economic capital. Daily closes miss intraday extremes, a stationary basis cannot represent a
permanent regime shift, and independently resampled series lose their cross-dependence (use
``joint=True`` on aligned data); the deterministic scenarios exist for what history never held.

KELLY is a research input only: ``fractional_kelly`` uses the LOWER confidence bound of the mean
edge, so a non-positive bound gives 0. ``recommend_exposure`` returns ``max_leverage`` of 0.0
(ZERO EXPOSURE) or a research ceiling stated in hedged notional over FULLY ALLOCATED economic capital (N / E): never above the 1x
book's own N / E = 1 / (1 + reserve fraction) (the hard cap and the survival limit are levels of L = N / C and are converted with
it, so a reserve lowers the ceiling and the ceiling can never permit financed notional), never above the Kelly or the
stress-survival limit, with ``authorizes_trading = False`` always.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import evidence_stats as ES
import trade_economics as TE

LEVERAGES: Tuple[float, ...] = (1.0, 1.25, 1.5, 2.0, 3.0, 5.0, 10.0)
MAX_RECOMMENDED_LEVERAGE = 1.0

ASSUMED_VENUE_RULES = "ASSUMED_NOT_VENUE_VERIFIED"
VERIFIED_VENUE_RULES = "VENUE_VERIFIED"
FINANCING_UNKNOWN = "FINANCING_UNKNOWN"
FINANCING_NOT_REQUIRED = "NOT_REQUIRED"
FINANCING_SCENARIO = "SCENARIO_RATES_SUPPLIED"
NOT_RECOMMENDABLE = "NOT_RECOMMENDABLE"
WITHIN_CEILING = "WITHIN_1X_RESEARCH_CEILING"
INSUFFICIENT = "INSUFFICIENT"
UNKNOWN = "UNKNOWN"
UNSPECIFIED = TE.UNSPECIFIED
ZERO_EXPOSURE = "ZERO_EXPOSURE"
STRESS_ILLUSTRATIVE = "ILLUSTRATIVE_NOT_CALIBRATED"
STRESS_CALLER = "CALLER_SUPPLIED"
FEES_VERIFIED = "ACCOUNT_VERIFIED"
MIN_HISTORY_DAYS = 30
MIN_FUNDING_PRINTS = 90
MIN_PATHS = 200
SETTLEMENTS_PER_DAY = 3
KIND = "OFFLINE_SIMULATION_ONLY"

LEVERAGE_DEFINITION = ("L = hedged notional N / hedge capital C (own BTC at full value); economic capital E = C + reserve; "
                       "equals hedged notional / economic capital when the reserve is zero; financing F = (L-1) C")


def _fin(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _num(name: str, x: Any, *, gt: Optional[float] = None, ge: Optional[float] = None,
         lt: Optional[float] = None, le: Optional[float] = None) -> float:
    if not _fin(x):
        raise ValueError(f"{name} must be a finite number, got {x!r}")
    if (gt is not None and not x > gt) or (ge is not None and not x >= ge) \
            or (lt is not None and not x < lt) or (le is not None and not x <= le):
        raise ValueError(f"{name} out of range: {x!r}")
    return float(x)


# --------------------------------------------------------------------------------------------
# Venue rules
# --------------------------------------------------------------------------------------------
_PLACEHOLDERS: Dict[str, float] = {"btc_collateral_haircut": 0.10, "initial_margin_rate": 0.05,
                                   "maintenance_margin_rate": 0.005, "liquidation_fee_bps": 50.0}


@dataclass(frozen=True)
class VenueRules:
    btc_collateral_haircut: float       # in [0, 1): credit withheld on BTC used as collateral
    initial_margin_rate: float          # fraction of perp notional required to open
    maintenance_margin_rate: float      # fraction of perp notional below which equity is liquidated
    liquidation_fee_bps: float          # cost of a forced close, bps of the marked notional
    verified: bool = False              # the CALLER's claim that these are the venue's own rules
    source: str = ""                    # where they were read from; required for the claim to count

    def __post_init__(self) -> None:
        _num("btc_collateral_haircut", self.btc_collateral_haircut, ge=0.0, lt=1.0)
        _num("initial_margin_rate", self.initial_margin_rate, gt=0.0, le=1.0)
        _num("maintenance_margin_rate", self.maintenance_margin_rate, gt=0.0, le=1.0)
        _num("liquidation_fee_bps", self.liquidation_fee_bps, ge=0.0)
        if self.maintenance_margin_rate > self.initial_margin_rate:
            raise ValueError("maintenance_margin_rate cannot exceed initial_margin_rate")


def assumed_venue_rules() -> VenueRules:
    """The module's own placeholders. They are ASSUMED_NOT_VENUE_VERIFIED and can never be upgraded."""
    return VenueRules(**_PLACEHOLDERS)


def coerce_venue_rules(obj: Any) -> VenueRules:
    if obj is None:
        return assumed_venue_rules()
    if isinstance(obj, VenueRules):
        return obj
    if isinstance(obj, Mapping):
        extra = set(obj) - set(_PLACEHOLDERS) - {"verified", "source"}
        missing = set(_PLACEHOLDERS) - set(obj)
        if extra or missing:
            raise ValueError(f"venue_rules: unknown keys {sorted(extra)}, missing keys {sorted(missing)}")
        return VenueRules(**{k: obj[k] for k in _PLACEHOLDERS}, verified=obj.get("verified", False) is True,
                          source=obj.get("source", "") if isinstance(obj.get("source", ""), str) else "")
    raise ValueError("venue_rules must be None, a VenueRules or a mapping")


def _rules_status(r: VenueRules) -> Tuple[str, str]:
    if r.verified is not True:
        return ASSUMED_VENUE_RULES, "not marked verified"
    if not (isinstance(r.source, str) and r.source.strip()):
        return ASSUMED_VENUE_RULES, "marked verified without a source: not upgraded"
    if all(getattr(r, k) == v for k, v in _PLACEHOLDERS.items()):
        return ASSUMED_VENUE_RULES, "marked verified but identical to this module's placeholders: not upgraded"
    return VERIFIED_VENUE_RULES, "caller-asserted: this module cannot check the rules against the venue"


def venue_rules_status(obj: Any) -> str:
    return _rules_status(coerce_venue_rules(obj))[0]


def _rules_view(r: VenueRules) -> Dict[str, Any]:
    status, note = _rules_status(r)
    return {"btc_collateral_haircut": r.btc_collateral_haircut, "initial_margin_rate": r.initial_margin_rate,
            "maintenance_margin_rate": r.maintenance_margin_rate, "liquidation_fee_bps": r.liquidation_fee_bps,
            "verified": r.verified is True, "source": r.source if isinstance(r.source, str) else "",
            "status": status, "note": note}


def _status_from_view(view: Any) -> str:
    """Re-derive the status from the stored fields, so a hand-edited label cannot upgrade anything."""
    try:
        r = VenueRules(**{k: view[k] for k in _PLACEHOLDERS}, verified=view.get("verified") is True,
                       source=view.get("source", "") if isinstance(view.get("source", ""), str) else "")
        derived = _rules_status(r)[0]
    except (ValueError, TypeError, KeyError, AttributeError):
        return ASSUMED_VENUE_RULES
    return derived if view.get("status") == derived else ASSUMED_VENUE_RULES


# --------------------------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class CarryInputs:
    """Everything the simulation needs, all declared. Nothing here has a financing default."""
    capital_usd: float                                  # C: own BTC at full value that carries the hedge
    hold_days: float
    funding_bps_path: Sequence[float]                   # per-settlement funding of the base case (SCENARIO)
    costs: TE.Costs                                     # borrow_apr / borrowed_usd here are IGNORED
    reserve_frac_of_notional: float                     # rho: idle reserve R = rho N; REQUIRED, never defaulted
    venue_rules: Any = None                             # None -> ASSUMED placeholders
    financing_apr_scenarios: Optional[Mapping[str, float]] = None   # fractions per year; None -> FINANCING_UNKNOWN
    alternative_return_apr_pct: Optional[float] = None  # None -> UNSPECIFIED

    def __post_init__(self) -> None:
        _num("capital_usd", self.capital_usd, gt=0.0)
        _num("hold_days", self.hold_days, gt=0.0)
        _num("reserve_frac_of_notional", self.reserve_frac_of_notional, ge=0.0)
        if not isinstance(self.costs, TE.Costs):
            raise ValueError("costs must be a trade_economics.Costs")
        path = tuple(self.funding_bps_path)
        if not path or any(not _fin(x) for x in path):
            raise ValueError("funding_bps_path must be a non-empty sequence of finite numbers")
        object.__setattr__(self, "funding_bps_path", path)
        coerce_venue_rules(self.venue_rules)
        if self.financing_apr_scenarios is not None:
            if not isinstance(self.financing_apr_scenarios, Mapping):
                raise ValueError("financing_apr_scenarios must be a mapping of name -> annual rate (a fraction)")
            for k, v in self.financing_apr_scenarios.items():
                if not (isinstance(k, str) and k):
                    raise ValueError("financing scenario names must be non-empty strings")
                _num(f"financing rate {k!r} (a fraction per year, 0.08 = 8%)", v, ge=0.0, le=1.0)
        if self.alternative_return_apr_pct is not None:
            _num("alternative_return_apr_pct", self.alternative_return_apr_pct)


def _check_leverage(leverage: Any) -> float:
    return _num("leverage", leverage, ge=1.0)


def _exit_cost(costs: TE.Costs) -> Tuple[float, List[str]]:
    unpriced = [n for n in ("spread_bps", "slippage_bps", "impact_bps") if getattr(costs, n) is None]
    bps = (costs.perp_exit_fee_bps + costs.spot_exit_fee_bps + sum(getattr(costs, n) or 0.0 for n in
           ("spread_bps", "slippage_bps", "impact_bps")))
    return bps, unpriced


def _returns_view(r: Mapping[str, Any]) -> Dict[str, Any]:
    ret = r["returns"]
    return {"net_pnl_usd": r["net_pnl_usd"], "reconciles": r["reconciles"],
            "period_pct_on_notional": ret["period_pct_on_notional"],
            "annualised_pct_on_notional": ret["annualised_pct_on_notional"],
            "period_pct_on_economic_capital": ret["period_pct_on_economic_capital"],
            "annualised_pct_on_economic_capital": ret["annualised_pct_on_economic_capital"],
            "economic_capital_usd": r["capital"]["economic_capital_usd"],
            "components_usd": dict(r["components_usd"]), "unpriced_inputs": list(r["unknowns"]),
            "hurdles": [{"hurdle_apr_pct": h["hurdle_apr_pct"], "status": h["status"], "meets": h["meets"],
                         "incomplete": h["incomplete"]} for h in r["hurdles"]]}


def leverage_book(inputs: CarryInputs, leverage: float) -> Dict[str, Any]:
    """The static book at one leverage: capital, margin, headroom, returns, financing, opportunity cost."""
    L = _check_leverage(leverage)
    rules = coerce_venue_rules(inputs.venue_rules)
    status, _ = _rules_status(rules)
    h, im, mm = rules.btc_collateral_haircut, rules.initial_margin_rate, rules.maintenance_margin_rate
    C = inputs.capital_usd
    N = L * C
    F = (L - 1.0) * C
    R = inputs.reserve_frac_of_notional * N
    E_eco = C + R
    credit = N * (1.0 - h)
    E0 = credit - F
    IM, MM = im * N, mm * N
    span = max(0.0, E0 - MM)
    up = span / (N * (1.0 + mm))
    dn = span / credit
    up_reserve = max(0.0, E0 + R - MM) / (N * (1.0 + mm))
    opens = E0 >= IM
    exit_bps, exit_unpriced = _exit_cost(inputs.costs)

    def run(apr: Optional[float], borrowed: float) -> Dict[str, Any]:
        pos = TE.Position(notional_usd=N, hold_days=inputs.hold_days, btc_inventory_usd=C, btc_was_owned=True,
                          margin_posted_usd=0.0, collateral_haircut=h, reserve_usd=R, maintenance_margin_rate=mm,
                          initial_margin_rate=im, hedge_ratio=1.0)
        costs = replace(inputs.costs, borrow_apr=apr, borrowed_usd=borrowed)
        return TE.compute(pos, costs, list(inputs.funding_bps_path), funding_source="SCENARIO",
                          alternative_return_apr_pct=inputs.alternative_return_apr_pct)

    before = _returns_view(run(0.0, 0.0))
    extra_unpriced = []
    if F > 0.0 and inputs.costs.custody_apr:
        extra_unpriced.append("custody_on_financed_btc")      # TE charges custody on own capital only
    alt = inputs.alternative_return_apr_pct
    opp_usd = (alt / 100.0 * E_eco * inputs.hold_days / 365.0) if alt is not None else UNKNOWN

    def excess(view: Mapping[str, Any]) -> Any:
        return UNKNOWN if alt is None else view["annualised_pct_on_economic_capital"] - alt

    after: Dict[str, Dict[str, Any]] = {}
    if F == 0.0:
        fin_status = FINANCING_NOT_REQUIRED
        after["NO_FINANCING"] = {**before, "financing_apr": 0.0, "excess_over_alternative_pct": excess(before)}
    elif inputs.financing_apr_scenarios:
        fin_status = FINANCING_SCENARIO
        for name, apr in inputs.financing_apr_scenarios.items():
            v = _returns_view(run(apr, F))
            after[name] = {**v, "financing_apr": apr, "excess_over_alternative_pct": excess(v)}
    else:
        fin_status = FINANCING_UNKNOWN
    excess_by = {k: v["excess_over_alternative_pct"] for k, v in after.items()} or {FINANCING_UNKNOWN: UNKNOWN}
    fees_ok = inputs.costs.fee_provenance == FEES_VERIFIED
    return {
        "authorizes_trading": False, "kind": KIND, "leverage": L,
        "label": NOT_RECOMMENDABLE if L > 1.0 else WITHIN_CEILING,
        "venue_rules": _rules_view(rules), "venue_rules_status": status,
        "capital": {"hedge_capital_usd": C, "hedged_notional_usd": N, "financed_notional_usd": F, "reserve_usd": R,
                    "economic_capital_usd": E_eco, "notional_over_hedge_capital": N / C,
                    "notional_over_economic_capital": N / E_eco},
        "margin": {"collateral_haircut_usd": h * N, "collateral_credit_usd": credit, "venue_equity_usd": E0,
                   "required_initial_margin_usd": IM, "maintenance_margin_usd": MM,
                   "initial_margin_surplus_usd": E0 - IM, "opens": opens,
                   "rates": {"btc_collateral_haircut": h, "initial_margin_rate": im, "maintenance_margin_rate": mm,
                             "liquidation_fee_bps": rules.liquidation_fee_bps}},
        "headroom": {"liquidation_headroom_pct": min(up, dn) * 100.0, "perp_up_vs_collateral_pct": up * 100.0,
                     "collateral_down_vs_perp_pct": dn * 100.0, "with_reserve_topup_pct": up_reserve * 100.0,
                     "definition": "adverse perp-vs-collateral move that takes margin equity to maintenance margin; "
                                   "conservative, ignores the hedge's own offset; the reserve is not counted"},
        "financing": {"status": fin_status, "financed_notional_usd": F,
                      "scenario_rates": dict(inputs.financing_apr_scenarios or {}) if F > 0.0 else {}},
        "returns": {"before_financing": before, "after_financing": after,
                    "before_financing_note": ("EXCLUDES the cost of financing the extra notional: not a net return "
                                              "while financing is UNKNOWN" if F > 0.0 else "no financing is needed")},
        "opportunity_cost": {"alternative_return_apr_pct": alt if alt is not None else UNSPECIFIED,
                             "capital_tied_up_usd": E_eco, "opportunity_cost_usd": opp_usd,
                             "excess_over_alternative_pct_by_financing": excess_by},
        "exit_cost": {"base_bps": exit_bps, "unpriced": exit_unpriced},
        "fees": {"provenance": inputs.costs.fee_provenance, "verified": fees_ok},
        "unpriced_inputs": sorted(set(before["unpriced_inputs"]) | set(extra_unpriced)),
    }


# --------------------------------------------------------------------------------------------
# Stress scenarios
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class StressScenario:
    name: str
    gap_pct: float = 0.0                    # signed percent move of spot, + up
    basis_bps: float = 0.0                  # signed change of perp minus spot, + perp rich (adverse to the short)
    funding_bps_per_print: float = 0.0      # signed per settlement; NEGATIVE = paid by the short
    funding_prints: int = 0
    exit_cost_mult: float = 1.0             # liquidity deterioration; applied to the whole modelled exit cost
    hedge_rebalanced: bool = True           # False = the gap leaves no time to rebalance: only the adverse leg is marked

    def __post_init__(self) -> None:
        if not (isinstance(self.name, str) and self.name):
            raise ValueError("scenario name must be a non-empty string")
        _num("gap_pct", self.gap_pct, gt=-100.0)
        _num("basis_bps", self.basis_bps)
        _num("funding_bps_per_print", self.funding_bps_per_print)
        if not (isinstance(self.funding_prints, int) and not isinstance(self.funding_prints, bool)
                and self.funding_prints >= 0):
            raise ValueError("funding_prints must be a non-negative integer")
        _num("exit_cost_mult", self.exit_cost_mult, ge=1.0)
        if not isinstance(self.hedge_rebalanced, bool):
            raise ValueError("hedge_rebalanced must be a bool")
        if 1.0 + self.basis_bps / 1e4 <= 0.0:
            raise ValueError("basis_bps would make the perp price non-positive")

    @property
    def duration_days(self) -> float:
        return self.funding_prints / float(SETTLEMENTS_PER_DAY)


def combine(name: str, *parts: StressScenario) -> StressScenario:
    """A CORRELATED scenario: the parts happen together. Two parts may not both move the same
    dimension (gap, basis, funding, exit cost): stacking them would be a different scenario."""
    if len(parts) < 2:
        raise ValueError("combine needs at least two scenarios")
    seen: Dict[str, List[str]] = {}
    for p in parts:
        for dim, active in (("gap", p.gap_pct != 0.0), ("basis", p.basis_bps != 0.0),
                            ("funding", p.funding_prints > 0 and p.funding_bps_per_print != 0.0),
                            ("exit_cost", p.exit_cost_mult != 1.0)):
            if active:
                seen.setdefault(dim, []).append(p.name)
    clash = {d: n for d, n in seen.items() if len(n) > 1}
    if clash:
        raise ValueError(f"parts move the same dimension: {clash}")
    return StressScenario(name, gap_pct=sum(p.gap_pct for p in parts), basis_bps=sum(p.basis_bps for p in parts),
                          funding_bps_per_print=sum(p.funding_bps_per_print for p in parts),
                          funding_prints=max(p.funding_prints for p in parts),
                          exit_cost_mult=max(p.exit_cost_mult for p in parts),
                          hedge_rebalanced=all(p.hedge_rebalanced for p in parts))


def default_stress_scenarios() -> Tuple[StressScenario, ...]:
    """Named stresses. ILLUSTRATIVE_NOT_CALIBRATED: sizes are round numbers chosen to be uncomfortable,
    not fitted to any history; a caller with a view supplies its own list."""
    S = StressScenario
    singles = (
        S("basis_up_150bps", basis_bps=150.0), S("basis_down_150bps", basis_bps=-150.0),
        S("basis_up_400bps", basis_bps=400.0), S("basis_down_400bps", basis_bps=-400.0),
        S("funding_reversal_5bps_x9", funding_bps_per_print=-5.0, funding_prints=9),
        S("funding_reversal_20bps_x21", funding_bps_per_print=-20.0, funding_prints=21),
        S("gap_up_10pct", gap_pct=10.0), S("gap_down_10pct", gap_pct=-10.0),
        S("gap_up_25pct", gap_pct=25.0), S("gap_down_25pct", gap_pct=-25.0),
        S("gap_up_10pct_no_rebalance", gap_pct=10.0, hedge_rebalanced=False),
        S("gap_down_10pct_no_rebalance", gap_pct=-10.0, hedge_rebalanced=False),
        S("gap_up_25pct_no_rebalance", gap_pct=25.0, hedge_rebalanced=False),
        S("gap_down_25pct_no_rebalance", gap_pct=-25.0, hedge_rebalanced=False),
        S("exit_cost_x3", exit_cost_mult=3.0), S("exit_cost_x10", exit_cost_mult=10.0))
    funding = S("f", funding_bps_per_print=-10.0, funding_prints=9)
    basis = S("b", basis_bps=200.0)
    exit3 = S("x", exit_cost_mult=3.0)
    correlated = (
        combine("correlated_squeeze_gap_basis_funding_exit_x3", S("g", gap_pct=20.0, hedge_rebalanced=False),
                basis, funding, exit3),
        combine("correlated_crash_gap_basis_funding_exit_x3", S("g", gap_pct=-20.0, hedge_rebalanced=False),
                basis, funding, exit3))
    return singles + correlated


def evaluate_scenario(book: Mapping[str, Any], scenario: StressScenario, *,
                      financing_apr: Optional[float] = None) -> Dict[str, Any]:
    """One stress on one book. `financing_apr`, if given, accrues on the financed notional for the
    scenario's funding run; without it the accrual is omitted and an L>1 result says FINANCING_UNKNOWN."""
    cap, mg = book["capital"], book["margin"]
    rt = mg["rates"]
    N, R, F = cap["hedged_notional_usd"], cap["reserve_usd"], cap["financed_notional_usd"]
    h, mm, liq_bps = rt["btc_collateral_haircut"], rt["maintenance_margin_rate"], rt["liquidation_fee_bps"]
    E0 = mg["venue_equity_usd"]
    g, beta = scenario.gap_pct / 100.0, scenario.basis_bps / 1e4
    Pr = (1.0 + g) * (1.0 + beta)                                    # perp price relative to entry
    perp, coll = -N * (Pr - 1.0), (1.0 - h) * N * g
    perp_c, coll_c = (perp, coll) if scenario.hedge_rebalanced else (min(0.0, perp), min(0.0, coll))
    funding = N * scenario.funding_bps_per_print * scenario.funding_prints / 1e4
    exit_cost = scenario.exit_cost_mult * book["exit_cost"]["base_bps"] * N / 1e4
    accrues = F > 0.0 and financing_apr is not None
    accrual = F * financing_apr * scenario.duration_days / 365.0 if accrues else 0.0
    E1 = E0 + perp_c + coll_c + funding - exit_cost - accrual
    mm1 = mm * N * max(1.0, Pr)
    liquidated = E1 <= mm1
    opens = mg["opens"]
    survives = opens and not liquidated
    fin_status = book["financing"]["status"]
    return {
        "authorizes_trading": False, "name": scenario.name,
        "scenario": {"gap_pct": scenario.gap_pct, "basis_bps": scenario.basis_bps,
                     "funding_bps_per_print": scenario.funding_bps_per_print, "funding_prints": scenario.funding_prints,
                     "exit_cost_mult": scenario.exit_cost_mult, "hedge_rebalanced": scenario.hedge_rebalanced},
        "status": "SURVIVES" if survives else ("LIQUIDATED" if opens else "NOT_OPENABLE"),
        "survives": survives, "liquidated": liquidated, "opens": opens,
        "equity_before_usd": E0, "equity_after_usd": E1, "maintenance_margin_after_usd": mm1,
        "margin_buffer_usd": E1 - mm1,
        "survives_with_reserve_topup": bool(opens and E1 + R > mm1),
        "equity_change_pct_of_economic_capital": (E1 - E0) / cap["economic_capital_usd"] * 100.0,
        "liquidation_cost_usd": (liq_bps / 1e4 * N * max(1.0, Pr)) if liquidated else 0.0,
        "components_usd": {"perp_mark": perp_c, "collateral_credit_change": coll_c, "funding_run": funding,
                           "exit_cost": -exit_cost, "financing_accrual": -accrual,
                           "offsetting_gain_withheld": (perp - perp_c) + (coll - coll_c)},
        "financing_status": fin_status, "financing_accrual_included": accrues or F == 0.0,
        "exit_cost_unpriced": list(book["exit_cost"]["unpriced"]),
        "venue_rules_status": book["venue_rules_status"],
        "reading": "survival assumes NO manual top-up from the reserve; the book is evaluated as if closed at the stress point",
    }


def survival_table(inputs: CarryInputs, scenarios: Optional[Sequence[StressScenario]] = None,
                   leverages: Sequence[float] = LEVERAGES) -> Dict[str, Any]:
    """Every leverage against every scenario. ``stress_survival_limit`` is the highest level such that it
    and every lower level survive all scenarios (0.0 if even the lowest does not). Levels above 1x are
    analysis only (NOT_RECOMMENDABLE) and the limit is not a recommendation."""
    scen = tuple(default_stress_scenarios() if scenarios is None else scenarios)
    if not scen or any(not isinstance(s, StressScenario) for s in scen):
        raise ValueError("scenarios must be a non-empty sequence of StressScenario")
    if len({s.name for s in scen}) != len(scen):
        raise ValueError("scenario names must be unique")
    levels = sorted({_check_leverage(x) for x in leverages})
    if not levels:
        raise ValueError("at least one leverage is required")
    rates = inputs.financing_apr_scenarios or {}
    stress_apr = max(rates.values()) if rates else None
    books, limit, still_ok = [], 0.0, True
    for L in levels:
        book = leverage_book(inputs, L)
        results = {s.name: evaluate_scenario(book, s, financing_apr=stress_apr) for s in scen}
        failing = [n for n, r in results.items() if not r["survives"]]
        book["stress"] = {"results": results, "failing": failing, "survives_all": not failing,
                          "financing_apr_used_for_accrual": stress_apr}
        books.append(book)
        still_ok = still_ok and not failing
        if still_ok:
            limit = L
    first = books[0]
    return {
        "authorizes_trading": False, "kind": KIND, "leverage_definition": LEVERAGE_DEFINITION,
        "venue_rules": first["venue_rules"], "venue_rules_status": first["venue_rules_status"],
        "fee_provenance": inputs.costs.fee_provenance,
        "financing": {"scenario_rates_supplied": bool(rates),
                      "levels_financing_unknown": [b["leverage"] for b in books
                                                   if b["financing"]["status"] == FINANCING_UNKNOWN]},
        "stress_params_status": STRESS_ILLUSTRATIVE if scenarios is None else STRESS_CALLER,
        "scenario_names": [s.name for s in scen], "books": books,
        "stress_survival_limit": limit,
        "stress_survival_limit_note": "analysis only: the highest grid level whose book and all lower books survive every "
                                      "scenario with no reserve top-up; NOT a recommendation, and illustrative unless "
                                      "the scenarios were calibrated by the caller",
        "recommendation_ceiling": MAX_RECOMMENDED_LEVERAGE,
    }


# --------------------------------------------------------------------------------------------
# Path-based risk: seeded block bootstrap of caller-supplied history
# --------------------------------------------------------------------------------------------
def _insufficient(reason: str, **kw: Any) -> Dict[str, Any]:
    return {"authorizes_trading": False, "kind": KIND, "status": INSUFFICIENT, "reason": reason, **kw}


def _block_len(n: int, ess: Optional[float]) -> int:
    tau = n / max(1.0, ess or 1.0)
    return int(min(max(1, n // 2), max(round(n ** (1.0 / 3.0)), math.ceil(2.0 * tau))))


def _draw(rng: random.Random, xs: Sequence[float], need: int, block: int) -> List[float]:
    out: List[float] = []
    starts = len(xs) - block + 1
    while len(out) < need:
        i = rng.randrange(starts)
        out.extend(xs[i:i + block])
    return out[:need]


def _draw_joint(rng: random.Random, rets: Sequence[float], basis: Sequence[float], fund: Sequence[float],
                days: int, block: int) -> Tuple[List[float], List[float], List[float]]:
    """Whole-day blocks drawn from ALIGNED series: day i brings its return, its basis and its three funding prints."""
    spd = SETTLEMENTS_PER_DAY
    r_d: List[float] = []
    b_d: List[float] = []
    f_d: List[float] = []
    while len(r_d) < days:
        i = rng.randrange(len(rets) - block + 1)
        r_d.extend(rets[i:i + block])
        b_d.extend(basis[i:i + block])
        f_d.extend(fund[spd * i:spd * (i + block)])
    return r_d[:days], b_d[:days], f_d[:spd * days]


def _tail(sorted_losses: Sequence[float], q: float) -> Tuple[float, float]:
    n = len(sorted_losses)
    k = max(1, math.ceil(q * n))
    return sorted_losses[k - 1], math.fsum(sorted_losses[k - 1:]) / (n - k + 1)


def _pick(sorted_vals: Sequence[float], q: float) -> float:
    return sorted_vals[min(len(sorted_vals) - 1, max(0, math.ceil(q * len(sorted_vals)) - 1))]


def _clean(name: str, xs: Any) -> Optional[List[float]]:
    if xs is None:
        return None
    try:
        vals = list(xs)
    except TypeError:
        raise ValueError(f"history[{name!r}] must be a sequence of numbers")
    if any(not _fin(v) for v in vals):
        raise ValueError(f"history[{name!r}] contains a non-finite or non-numeric value")
    return [float(v) for v in vals]


def path_risk(inputs: CarryInputs, history: Optional[Mapping[str, Sequence[float]]], *, horizon_days: int,
              n_paths: int = 2000, seed: int = ES.DEFAULT_SEED, leverages: Sequence[float] = LEVERAGES,
              joint: bool = False, entry_basis_bps: Optional[float] = None,
              block_days: Optional[int] = None) -> Dict[str, Any]:
    """VaR/CVaR and maximum drawdown of the horizon P&L from a seeded block bootstrap of
    ``history = {"funding_bps": per-settlement prints, "basis_bps": daily basis levels, "daily_returns": fractions}``.
    No history (or too little) returns INSUFFICIENT: nothing is invented. Losses are percent of economic
    capital, positive = loss; the hedge is assumed intact (price moves reach equity only through the
    collateral haircut), liquidation is tested every day, and a liquidated path pays the venue's
    liquidation fee and stops. L>1 without financing scenarios is FINANCING_UNKNOWN and EXCLUDES financing."""
    if not (isinstance(horizon_days, int) and not isinstance(horizon_days, bool) and horizon_days >= 1):
        raise ValueError("horizon_days must be a positive integer")
    if not (isinstance(n_paths, int) and not isinstance(n_paths, bool) and n_paths >= MIN_PATHS):
        raise ValueError(f"n_paths must be an integer >= {MIN_PATHS}")
    if not (isinstance(seed, int) and not isinstance(seed, bool)):
        raise ValueError("seed must be an integer")
    levels = sorted({_check_leverage(x) for x in leverages})
    if not levels:
        raise ValueError("at least one leverage is required")
    if entry_basis_bps is not None:
        _num("entry_basis_bps", entry_basis_bps)
    if history is None or not isinstance(history, Mapping):
        return _insufficient("no history supplied: path metrics are not invented",
                             missing=["funding_bps", "basis_bps", "daily_returns"])
    fund, basis, rets = (_clean(k, history.get(k)) for k in ("funding_bps", "basis_bps", "daily_returns"))
    need = {"funding_bps": (fund, MIN_FUNDING_PRINTS), "basis_bps": (basis, MIN_HISTORY_DAYS),
            "daily_returns": (rets, MIN_HISTORY_DAYS)}
    short = {k: {"have": 0 if v is None else len(v), "need": m} for k, (v, m) in need.items()
             if v is None or len(v) < m}
    if short:
        return _insufficient("history missing or too short", short=short, missing=sorted(short))
    assert fund is not None and basis is not None and rets is not None
    if any(r <= -1.0 for r in rets):
        raise ValueError("daily_returns must be greater than -1")
    n_days = len(rets)
    if joint and not (len(basis) == n_days and len(fund) == SETTLEMENTS_PER_DAY * n_days):
        return _insufficient("joint resampling needs aligned series: basis_bps and daily_returns the same length, "
                             "funding_bps three times as long", lengths={"daily_returns": n_days,
                             "basis_bps": len(basis), "funding_bps": len(fund)})
    ess = {"funding_bps": ES.conservative_ess(fund), "basis_bps": ES.conservative_ess(basis),
           "daily_returns": ES.conservative_ess(rets)}
    b_f = _block_len(len(fund), ess["funding_bps"])
    b_b, b_r = _block_len(len(basis), ess["basis_bps"]), _block_len(n_days, ess["daily_returns"])
    if block_days is not None:
        if not (isinstance(block_days, int) and not isinstance(block_days, bool) and 1 <= block_days <= n_days // 2):
            raise ValueError("block_days must be an integer in [1, n_days // 2]")
        b_f, b_b, b_r = block_days * SETTLEMENTS_PER_DAY, block_days, block_days
    b_f, b_b, b_r = min(b_f, len(fund) // 2), min(b_b, len(basis) // 2), min(b_r, n_days // 2)
    b_joint = min(n_days // 2, max(b_r, b_b, math.ceil(b_f / SETTLEMENTS_PER_DAY))) if block_days is None else block_days
    b_entry = float(sorted(basis)[len(basis) // 2]) if entry_basis_bps is None else float(entry_basis_bps)

    H, rng, spd = horizon_days, random.Random(seed), SETTLEMENTS_PER_DAY
    paths: List[Tuple[List[float], List[float], List[float]]] = []
    for _ in range(n_paths):
        if joint:
            r_d, b_d, f_d = _draw_joint(rng, rets, basis, fund, H, b_joint)
        else:
            r_d, b_d, f_d = _draw(rng, rets, H, b_r), _draw(rng, basis, H, b_b), _draw(rng, fund, spd * H, b_f)
        W, B, CF = [1.0], [0.0], [0.0]
        for d in range(H):
            W.append(W[-1] * (1.0 + r_d[d]))
            B.append((b_d[d] - b_entry) / 1e4)
            CF.append(CF[-1] + sum(f_d[spd * d:spd * (d + 1)]) / 1e4)
        paths.append((W, B, CF))

    rules = coerce_venue_rules(inputs.venue_rules)
    status, _ = _rules_status(rules)
    rates = inputs.financing_apr_scenarios or {}
    books_out, unpriced_all = [], set()
    for L in levels:
        book = leverage_book(inputs, L)
        cap, mg = book["capital"], book["margin"]
        N, C, R, F, E0 = (cap["hedged_notional_usd"], cap["hedge_capital_usd"], cap["reserve_usd"],
                          cap["financed_notional_usd"], mg["venue_equity_usd"])
        pos = TE.Position(notional_usd=N, hold_days=float(H), btc_inventory_usd=C, btc_was_owned=True,
                          margin_posted_usd=0.0, collateral_haircut=rules.btc_collateral_haircut, reserve_usd=R,
                          maintenance_margin_rate=rules.maintenance_margin_rate,
                          initial_margin_rate=rules.initial_margin_rate, hedge_ratio=1.0)
        fixed = TE.compute(pos, replace(inputs.costs, borrow_apr=0.0, borrowed_usd=0.0), [0.0], funding_source="SCENARIO")
        comp = fixed["components_usd"]
        entry_cost = -(comp["perp_entry_fee_usd"] + comp["spot_entry_fee_usd"])
        exit_cost = -sum(v for k, v in comp.items() if k not in
                         ("funding_income_usd", "basis_pnl_usd", "perp_entry_fee_usd", "spot_entry_fee_usd", "borrow_usd"))
        unpriced_all |= set(fixed["unknowns"])
        if F == 0.0:
            cases: Dict[str, Optional[float]] = {"NO_FINANCING": 0.0}
        elif rates:
            cases = dict(rates)
        else:
            cases = {FINANCING_UNKNOWN: None}
        out_cases: Dict[str, Any] = {}
        for cname, apr in cases.items():
            if not mg["opens"]:
                out_cases[cname] = {"status": "NOT_OPENABLE", "reason": "initial margin not met at entry"}
                continue
            out_cases[cname] = _simulate(paths, N=N, E_eco=C + R, E0=E0, h=rules.btc_collateral_haircut,
                                         mm=rules.maintenance_margin_rate, liq_bps=rules.liquidation_fee_bps,
                                         fin_day=(F * apr / 365.0 if apr is not None else 0.0),
                                         entry_cost=entry_cost, exit_cost=exit_cost, H=H)
            out_cases[cname]["financing_apr"] = apr
            out_cases[cname]["excludes_financing"] = apr is None and F > 0.0
        books_out.append({"leverage": L, "label": book["label"], "opens": mg["opens"],
                          "financing_status": book["financing"]["status"], "cases": out_cases})
    return {
        "authorizes_trading": False, "kind": KIND, "status": "OK", "seed": seed, "n_paths": n_paths,
        "horizon_days": H, "joint": joint, "venue_rules_status": status,
        "entry_basis_bps": b_entry, "entry_basis_source": "CALLER" if entry_basis_bps is not None else "HISTORY_MEDIAN",
        "history": {"n_days": n_days, "n_funding_prints": len(fund), "ess": ess,
                    "block": {"joint_days": b_joint} if joint else {"funding_prints": b_f, "basis_days": b_b, "return_days": b_r}},
        "unpriced_costs": sorted(unpriced_all - {"borrow"}), "books": books_out,
        "limitations": ["daily closes miss intraday extremes, so liquidation is understated",
                        "the basis is treated as stationary around its entry level: a permanent shift is not representable",
                        "series resampled independently lose their cross-dependence" if not joint else
                        "joint resampling keeps cross-dependence but only within the observed blocks",
                        "a resampled path set cannot see a tail the history never contained"],
    }


def _simulate(paths: Sequence[Tuple[List[float], List[float], List[float]]], *, N: float, E_eco: float, E0: float,
              h: float, mm: float, liq_bps: float, fin_day: float, entry_cost: float, exit_cost: float,
              H: int) -> Dict[str, Any]:
    losses: List[float] = []
    mdds: List[float] = []
    pnls: List[float] = []
    liquidated = 0
    for W, B, CF in paths:
        peak, mdd, pnl, dead = E_eco, 0.0, -entry_cost, False
        for t in range(1, H + 1):
            fin = fin_day * t
            pnl = N * (CF[t] - W[t] * B[t]) - fin - entry_cost
            pr = W[t] * (1.0 + B[t])
            margin_eq = E0 - N * (W[t] * (h + B[t]) - h) + N * CF[t] - fin - entry_cost
            if margin_eq <= mm * N * max(1.0, pr):
                pnl -= liq_bps / 1e4 * N * max(1.0, pr)
                dead = True
            elif t == H:
                pnl -= exit_cost
            eq = E_eco + pnl
            peak = max(peak, eq)
            mdd = max(mdd, (peak - eq) / peak)
            if dead:
                break
        liquidated += dead
        pnls.append(pnl)
        losses.append(-pnl / E_eco * 100.0)
        mdds.append(mdd * 100.0)
    losses.sort()
    mdds.sort()
    v95, c95 = _tail(losses, 0.95)
    v99, c99 = _tail(losses, 0.99)
    return {"status": "OK", "var_95_loss_pct": v95, "cvar_95_loss_pct": c95, "var_99_loss_pct": v99,
            "cvar_99_loss_pct": c99, "mean_pnl_pct_of_economic_capital": math.fsum(pnls) / len(pnls) / E_eco * 100.0,
            "max_drawdown_pct": {"median": _pick(mdds, 0.5), "p95": _pick(mdds, 0.95), "worst": mdds[-1]},
            "liquidation_fraction": liquidated / len(paths), "n_liquidated": liquidated, "n_paths": len(paths)}


def funding_edge_lower_bound(funding_bps: Sequence[float], *, breakeven_bps_per_settlement: float = 0.0,
                             alpha: float = 0.05, B: int = 2000, seed: int = ES.DEFAULT_SEED) -> Dict[str, Any]:
    """The mean funding print and its LOWER confidence bound (evidence_stats' serial-dependence-aware
    interval), less the per-settlement break-even cost. Anything but an OK interval is INSUFFICIENT."""
    be = _num("breakeven_bps_per_settlement", breakeven_bps_per_settlement)
    xs = [float(v) for v in funding_bps]
    if any(not math.isfinite(v) for v in xs):
        raise ValueError("funding_bps must be finite")
    ci = ES.block_bootstrap_ci(xs, B=B, seed=seed, alpha=alpha)
    if ci["status"] != "OK":
        return {"authorizes_trading": False, "status": INSUFFICIENT, "funding_evidence_status": INSUFFICIENT,
                "ci_status": ci["status"], "reason": ci.get("reason"), "edge_lower_bound_bps": None}
    return {"authorizes_trading": False, "status": "OK", "funding_evidence_status": "OK", "ci_status": "OK",
            "n": ci["n"], "ess": ci["ess"], "mean_bps": ci["estimate"], "mean_lower_bound_bps": ci["lo"],
            "breakeven_bps_per_settlement": be, "edge_lower_bound_bps": ci["lo"] - be}


# --------------------------------------------------------------------------------------------
# Kelly (research input only) and the recommendation ceiling
# --------------------------------------------------------------------------------------------
def kelly_fraction(mean: float, variance: float) -> float:
    """f* = mean / variance for a per-period return stream per unit of exposure (a multiple of capital).
    Zero for a non-positive mean. A research number, not a position size."""
    m = _num("mean", mean)
    v = _num("variance", variance, gt=0.0)
    return m / v if m > 0.0 else 0.0


def fractional_kelly(mean_lower_bound: Optional[float], variance: float, fraction: float) -> float:
    """`fraction` x Kelly evaluated at the LOWER confidence bound of the mean edge, so estimation
    uncertainty shrinks the answer. A missing, non-finite or non-positive bound gives 0.0."""
    f = _num("fraction", fraction, gt=0.0, le=1.0)
    v = _num("variance", variance, gt=0.0)
    if mean_lower_bound is None or not _fin(mean_lower_bound) or mean_lower_bound <= 0.0:
        return 0.0
    return f * kelly_fraction(mean_lower_bound, v)


def kelly_research(mean: float, mean_lower_bound: Optional[float], variance: float, fraction: float) -> Dict[str, Any]:
    lb_ok = _fin(mean_lower_bound) and mean_lower_bound > 0.0
    return {"authorizes_trading": False, "research_input_only": True,
            "kelly_full_at_point_mean": kelly_fraction(mean, variance), "fraction": fraction,
            "kelly_fractional_at_lower_bound": fractional_kelly(mean_lower_bound, variance, fraction),
            "reasons": [] if lb_ok else ["LOWER_BOUND_NOT_POSITIVE"],
            "reading": "a multiple of capital for research; it never sizes anything and cannot lift recommend_exposure"}


def _stress_at_1x(table: Mapping[str, Any]) -> Tuple[bool, List[str]]:
    for b in table.get("books") or []:
        if isinstance(b, Mapping) and b.get("leverage") == 1.0:
            results = (b.get("stress") or {}).get("results") or {}
            if results:
                return True, sorted(n for n, r in results.items() if not (isinstance(r, Mapping) and r.get("survives") is True))
    return False, []


def recommend_exposure(table: Mapping[str, Any], *, edge_lower_bound: Optional[float], funding_evidence_status: Optional[str],
                       kelly_fractional: Optional[float] = None, financing_status: Optional[str] = None) -> Dict[str, Any]:
    """A research CEILING on N / E, never an instruction. ``max_leverage`` is 0.0 (ZERO EXPOSURE) whenever any
    of these holds, each reported as a reason: the lower-bound edge is not positive (or unsupplied); the
    funding evidence is not OK; the 1x book is not stress-tested or a stress scenario liquidates it; the venue
    rules are only ASSUMED; financing is UNKNOWN; the fee tier is unverified; a supplied Kelly fraction is zero.
    Otherwise it is min(1.0, stress-survival limit, Kelly if supplied): never above 1.0. Levels above 1x are
    NOT_RECOMMENDABLE. ``authorizes_trading`` is always False. `financing_status` defaults to the 1x book's
    (NOT_REQUIRED); pass FINANCING_UNKNOWN if any part of the hedge would be financed at an unknown rate."""
    if not isinstance(table, Mapping):
        table = {}
    reasons: List[str] = []
    if not _fin(edge_lower_bound):
        reasons.append("EDGE_LOWER_BOUND_UNSUPPLIED")
    elif edge_lower_bound <= 0.0:
        reasons.append("EDGE_LOWER_BOUND_NOT_POSITIVE")
    if funding_evidence_status != "OK":
        reasons.append("FUNDING_EVIDENCE_INSUFFICIENT")
    evaluated, failing = _stress_at_1x(table)
    if not evaluated:
        reasons.append("STRESS_NOT_EVALUATED_AT_1X")
    elif failing:
        reasons.append("STRESS_SCENARIO_LIQUIDATES")
    rules_status = _status_from_view(table.get("venue_rules")) if isinstance(table.get("venue_rules"), Mapping) \
        else ASSUMED_VENUE_RULES
    if table.get("venue_rules_status") != VERIFIED_VENUE_RULES:
        rules_status = ASSUMED_VENUE_RULES
    if rules_status != VERIFIED_VENUE_RULES:
        reasons.append("VENUE_RULES_ASSUMED_NOT_VERIFIED")
    fin = financing_status
    if fin is None:
        fin = next(((b.get("financing") or {}).get("status", FINANCING_UNKNOWN) for b in table.get("books") or []
                    if isinstance(b, Mapping) and b.get("leverage") == 1.0), FINANCING_UNKNOWN)
    if fin == FINANCING_UNKNOWN:
        reasons.append("FINANCING_UNKNOWN")
    if table.get("fee_provenance") != FEES_VERIFIED:
        reasons.append("FEE_TIER_UNVERIFIED")
    if kelly_fractional is not None and not (_fin(kelly_fractional) and kelly_fractional > 0.0):
        reasons.append("KELLY_FRACTION_ZERO")
    limit = table.get("stress_survival_limit")
    limit = float(limit) if _fin(limit) else 0.0
    if limit <= 0.0 and not any(r.startswith("STRESS_") for r in reasons):
        reasons.append("STRESS_SURVIVAL_LIMIT_ZERO")
    # The ceiling is stated in N / E (hedged notional over FULLY ALLOCATED economic capital). The hard cap and the survival limit are
    # levels of L = N / C, and with a reserve E > C, so they are converted with the 1x book's own N / E: a cap of "1x" must not let
    # N / E exceed what the 1x book itself has (1 / (1 + reserve fraction)), or the ceiling would permit financed notional.
    one_x = next((b for b in table.get("books") or [] if isinstance(b, Mapping) and b.get("leverage") == 1.0), None)
    ne1 = ((one_x or {}).get("capital") or {}).get("notional_over_economic_capital")
    to_ne = float(ne1) if _fin(ne1) and 0.0 < ne1 <= 1.0 else 1.0
    if not (_fin(ne1) and 0.0 < ne1 <= 1.0):
        # without the 1x book's own N / E the caps cannot be converted into the ceiling's units: say so and recommend ZERO, rather
        # than silently treating N / E as 1 (which would state a ceiling above the true one whenever a reserve is held)
        reasons.append("NOTIONAL_OVER_ECONOMIC_CAPITAL_UNKNOWN")
    limit = limit * to_ne
    caps = {"HARD_CAP_1X": MAX_RECOMMENDED_LEVERAGE * to_ne, "STRESS_SURVIVAL_LIMIT": limit}
    if kelly_fractional is not None and _fin(kelly_fractional):
        caps["KELLY"] = float(kelly_fractional)
    limiting = min(caps, key=lambda k: (caps[k], k != "HARD_CAP_1X"))
    ceiling = 0.0 if reasons else max(0.0, min(caps.values()))
    return {
        "authorizes_trading": False, "kind": KIND, "max_leverage": ceiling,
        "label": ZERO_EXPOSURE if ceiling == 0.0 else "RESEARCH_CEILING_NOT_AN_INSTRUCTION",
        "zero_exposure": ceiling == 0.0, "reasons": reasons,
        "limiting_factor": None if reasons else limiting,
        "limits": {"hard_cap": MAX_RECOMMENDED_LEVERAGE * to_ne, "stress_survival_limit": limit, "units": "N / E (hedged notional over economic capital)",
                   "kelly_fractional": kelly_fractional},
        "levels_above_1x": NOT_RECOMMENDABLE, "venue_rules_status": rules_status,
        "stress_failures_at_1x": failing, "financing_status": fin,
        "reading": "a ceiling on hedged notional / economic capital for research; it authorizes nothing and "
                   "nothing here scales it up",
    }
