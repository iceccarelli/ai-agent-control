"""One coherent financial model of a hedged BTC carry trade. Pure arithmetic on DECLARED inputs.

Every component of the trade is a separate, named USD amount, and they reconcile: the net is
exactly the sum of the lines. Nothing is netted away silently, defaulted to zero, or inferred
from a leveraged figure. This is a model of what a trade WOULD earn under stated assumptions;
feeding it the measured fills and the account-settled funding of a reconciled trade replaces the
assumptions line by line, but the model itself proves nothing about the market.

Inputs that are not known stay UNKNOWN (``None``): they are excluded from the sum and listed in
``unknowns`` so a result can never look more complete than it is.

TWO RETURNS, because one flatters:
  * on HEDGED NOTIONAL        net / notional
  * on ECONOMIC CAPITAL       net / (BTC inventory value + extra margin posted + reserve)
    The inventory is capital whether the client already owned it or bought it for the hedge:
    it could have been sold, so it counts in full. Leverage only moves how much EXTRA margin is
    posted; it adds no edge, and ``return_on_posted_margin`` is printed only to be labelled so.

HURDLES: 10 / 15 / 20 %/yr net, on economic capital, are PROVISIONAL SCREENING SCENARIOS. They
are not owner-approved requirements, they carry no alternative-return, currency or risk-premium
assumption of their own (those are reported separately and default to UNSPECIFIED), and a pass
against them is not a claim that a strategy is profitable.

Basis and funding are different cash flows and are kept apart: basis P&L is entry basis minus
exit basis (a short perp against long spot gains when the perp/spot gap narrows); funding is the
settled funding path. A forecast is never booked as basis, nor basis as funding.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

PROVISIONAL = "PROVISIONAL_SCREENING_HURDLE_NOT_OWNER_APPROVED"
SCREENING_HURDLES_PCT = (10.0, 15.0, 20.0)
SETTLEMENTS_PER_DAY = 3.0
UNSPECIFIED = "UNSPECIFIED"


def _fin(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


@dataclass(frozen=True)
class Position:
    notional_usd: float                 # perp short notional at entry (the hedged notional)
    hold_days: float
    btc_inventory_usd: float            # economic value of the BTC backing the hedge at entry
    btc_was_owned: bool                 # information only: the economics are the same either way
    margin_posted_usd: float            # EXTRA quote collateral posted beyond the inventory
    collateral_haircut: float           # in [0, 1): the venue's haircut on BTC used as collateral
    reserve_usd: float                  # idle reserve held against margin calls
    maintenance_margin_rate: float      # fraction of notional; must be supplied from the venue's rules
    initial_margin_rate: float          # fraction of notional required to open; from the venue's rules
    hedge_ratio: float = 1.0            # perp qty / inventory qty (1.0 = fully hedged)


@dataclass(frozen=True)
class Costs:
    perp_entry_fee_bps: float
    perp_exit_fee_bps: float
    spot_entry_fee_bps: float = 0.0     # 0 only where no spot is traded (overlay)
    spot_exit_fee_bps: float = 0.0
    rebate_bps: float = 0.0             # maker rebate earned, as a positive number
    spread_bps: Optional[float] = None
    slippage_bps: Optional[float] = None
    impact_bps: Optional[float] = None
    latency_bps: Optional[float] = None     # UNKNOWN until measured
    conversion_bps: Optional[float] = None  # EUR/USD/USDT conversion
    transfer_usd: Optional[float] = None
    custody_apr: Optional[float] = None
    operational_usd: Optional[float] = None
    borrow_apr: Optional[float] = None
    borrowed_usd: float = 0.0
    fee_provenance: str = "UNVERIFIED"      # ACCOUNT_VERIFIED only when read from the account
    maker_taker: str = "taker"


def _check_position(p: Position) -> None:
    for name in ("notional_usd", "hold_days", "btc_inventory_usd", "maintenance_margin_rate", "initial_margin_rate"):
        v = getattr(p, name)
        if not (_fin(v) and v > 0):
            raise ValueError(f"{name} must be a positive finite number, got {v!r}")
    for name in ("margin_posted_usd", "reserve_usd"):
        v = getattr(p, name)
        if not (_fin(v) and v >= 0):
            raise ValueError(f"{name} must be a non-negative finite number, got {v!r}")
    if not (_fin(p.collateral_haircut) and 0.0 <= p.collateral_haircut < 1.0):
        raise ValueError("collateral_haircut must be in [0, 1)")
    if not (_fin(p.hedge_ratio) and 0.0 < p.hedge_ratio <= 2.0):
        raise ValueError("hedge_ratio must be in (0, 2]")


def compute(position: Position, costs: Costs, funding_bps: Sequence[float], *,
            entry_basis_bps: float = 0.0, exit_basis_bps: float = 0.0,
            funding_source: str = "SCENARIO", alternative_return_apr_pct: Optional[float] = None,
            risk_premium_pct: Optional[float] = None) -> Dict[str, Any]:
    """The full ledger of one trade. `funding_bps` is the per-settlement funding path received
    (positive) or paid (negative) by the short perpetual; `funding_source` must say what it is
    (PREDICTED, PUBLIC_REALISED, ACCOUNT_SETTLED or SCENARIO) because only the last is cash."""
    _check_position(position)
    if funding_source not in ("PREDICTED", "PUBLIC_REALISED", "ACCOUNT_SETTLED", "SCENARIO"):
        raise ValueError(f"funding_source {funding_source!r}")
    if any(not _fin(f) for f in funding_bps) or not (_fin(entry_basis_bps) and _fin(exit_basis_bps)):
        raise ValueError("funding path and basis must be finite")
    N = position.notional_usd
    hold = position.hold_days
    usd = lambda bps: N * bps / 1e4                       # noqa: E731
    unknowns: List[str] = []

    def opt(name: str, value: Optional[float], to_usd) -> float:
        if value is None:
            unknowns.append(name)
            return 0.0
        if not (_fin(value) and value >= 0):
            raise ValueError(f"{name} must be a non-negative finite number")
        return to_usd(value)

    comp: Dict[str, float] = {}
    comp["funding_income_usd"] = usd(sum(funding_bps))
    comp["basis_pnl_usd"] = usd(entry_basis_bps - exit_basis_bps)
    comp["rebates_usd"] = usd(costs.rebate_bps)
    comp["perp_entry_fee_usd"] = -usd(costs.perp_entry_fee_bps)
    comp["perp_exit_fee_usd"] = -usd(costs.perp_exit_fee_bps)
    comp["spot_entry_fee_usd"] = -usd(costs.spot_entry_fee_bps)
    comp["spot_exit_fee_usd"] = -usd(costs.spot_exit_fee_bps)
    comp["spread_usd"] = -opt("spread", costs.spread_bps, usd)
    comp["slippage_usd"] = -opt("slippage", costs.slippage_bps, usd)
    comp["market_impact_usd"] = -opt("market_impact", costs.impact_bps, usd)
    comp["latency_usd"] = -opt("latency", costs.latency_bps, usd)
    comp["conversion_usd"] = -opt("conversion", costs.conversion_bps, usd)
    comp["transfer_usd"] = -opt("transfer", costs.transfer_usd, lambda v: v)
    comp["custody_usd"] = -opt("custody", costs.custody_apr, lambda v: v * position.btc_inventory_usd * hold / 365.0)
    comp["operational_usd"] = -opt("operational", costs.operational_usd, lambda v: v)
    comp["borrow_usd"] = -opt("borrow", costs.borrow_apr, lambda v: v * costs.borrowed_usd * hold / 365.0)
    net = sum(comp.values())
    n_settle = len(funding_bps)

    inventory = position.btc_inventory_usd
    economic_capital = inventory + position.margin_posted_usd + position.reserve_usd
    required_im = position.initial_margin_rate * N
    available_collateral = inventory * (1.0 - position.collateral_haircut) + position.margin_posted_usd
    equity = available_collateral
    mm = position.maintenance_margin_rate * N
    ann = lambda v: v * 365.0 / hold                      # noqa: E731  simple, not compounded
    ret_notional = ann(net / N) * 100.0
    ret_capital = ann(net / economic_capital) * 100.0
    hedged_total_bps = (sum(c for k, c in comp.items() if c < 0) / N) * 1e4
    # Explicit costs only: fees, spread, slippage, impact, latency, conversion, transfer, custody, operational and borrow,
    # less rebates. Funding paid and an adverse basis are P&L, not costs, and are NOT in this number (the legacy
    # ``cost_bps_of_notional_round_trip`` below adds them whenever they are negative and ignores them when positive).
    explicit_cost_bps = -(sum(c for k, c in comp.items() if k not in ("funding_income_usd", "basis_pnl_usd")) / N) * 1e4
    # An unpriced cost is counted as ZERO above, so with any unknown the net, the returns and the break-even are
    # OPTIMISTIC bounds: the true cost can only be higher. A failure on this bound is conclusive; a pass is not.
    incomplete = bool(unknowns)
    fees_verified = costs.fee_provenance == "ACCOUNT_VERIFIED"
    # A holding period that does not match the funding path makes the annualised figures meaningless in either direction
    # (a 30-day hold with 3 prints reads -1 %/yr, a 3-day hold with 90 prints +52 %/yr), so it is reported and disables
    # every "meets" verdict rather than being annualised silently.
    expected_settlements = hold * SETTLEMENTS_PER_DAY
    path_matches_hold = abs(n_settle - expected_settlements) <= 1.0
    out: Dict[str, Any] = {
        "components_usd": comp, "net_pnl_usd": net,
        "net_is_optimistic_bound": incomplete,
        "net_reading": ("OPTIMISTIC BOUND: " + str(len(set(unknowns))) + " unpriced cost(s) are counted as zero, so the true "
                        "net is this or LOWER" if incomplete else "net of every declared cost"),
        "consistency": {"funding_path_matches_hold": path_matches_hold, "settlements_in_path": n_settle,
                        "settlements_expected_from_hold": expected_settlements,
                        "reading": ("consistent" if path_matches_hold else
                                    "INCONSISTENT: the annualised returns are not meaningful; read meets_if_consistent / verdict, not meets")},
        "explicit_cost_bps_of_notional": explicit_cost_bps,
        "reconciles": math.isclose(net, sum(comp.values()), rel_tol=0, abs_tol=1e-9),
        "unknowns": sorted(set(unknowns)),
        "position": {"hold_days": hold, "notional_usd": N},
        "funding": {"source": funding_source, "settlements": n_settle, "sum_bps": sum(funding_bps),
                    "is_cash": funding_source == "ACCOUNT_SETTLED"},
        "fees": {"provenance": costs.fee_provenance, "maker_taker": costs.maker_taker,
                 "verified": costs.fee_provenance == "ACCOUNT_VERIFIED"},
        "capital": {"hedged_notional_usd": N, "btc_inventory_usd": inventory, "btc_was_owned": position.btc_was_owned,
                    "margin_posted_extra_usd": position.margin_posted_usd, "reserve_usd": position.reserve_usd,
                    "economic_capital_usd": economic_capital,
                    "required_initial_margin_usd": required_im, "available_collateral_usd": available_collateral,
                    "initial_margin_sufficient": available_collateral >= required_im,
                    "unhedged_inventory_usd": max(0.0, inventory * (1.0 - position.hedge_ratio))},
        "returns": {
            "period_pct_on_notional": net / N * 100.0, "annualised_pct_on_notional": ret_notional,
            "period_pct_on_economic_capital": net / economic_capital * 100.0,
            "annualised_pct_on_economic_capital": ret_capital,
            "annualised_pct_on_posted_margin": (ann(net / position.margin_posted_usd) * 100.0
                                                if position.margin_posted_usd > 0 else None),
            "posted_margin_note": "leverage amplifies this figure; it is NOT an edge",
            "convention": "simple annualisation (period return x 365 / hold days), not compounded"},
        "liquidation": {
            "conservative_adverse_move_pct": max(0.0, (equity - mm) / N * 100.0),
            "note": "CONSERVATIVE: treats collateral as static and ignores the hedge's offsetting "
                    "gain on the BTC inventory; basis shock, gaps and fees are not included"},
        "cost_bps_of_notional_round_trip": -hedged_total_bps,
        "cost_bps_of_notional_round_trip_includes": "every negative component, including funding PAID and an ADVERSE basis; "
                                                    "see explicit_cost_bps_of_notional for costs alone",
        "breakeven_funding_bps_per_settlement": ((-(sum(c for k, c in comp.items() if k != "funding_income_usd") / N) * 1e4) / n_settle
                                                  if n_settle else None),
        "breakeven_is_lower_bound": incomplete,
        "alternative_return": {"apr_pct": alternative_return_apr_pct if alternative_return_apr_pct is not None else UNSPECIFIED,
                               "currency": "USD", "capital_denominator": "economic_capital",
                               "risk_premium_pct": risk_premium_pct if risk_premium_pct is not None else UNSPECIFIED},
        "hurdles": [{"hurdle_apr_pct": h, "status": PROVISIONAL,
                     # the LEGACY fields keep their original values; the fields after them are the honest reading
                     "annualised_pct_on_economic_capital": ret_capital, "meets": ret_capital >= h,
                     "meets_means": "meets on the MODELLED costs only" if unknowns else "meets on all declared costs",
                     "incomplete": incomplete, "unpriced_inputs": sorted(set(unknowns)),
                     "margin_pct_points": ret_capital - h,
                     "consistent_input": path_matches_hold,
                     "meets_if_consistent": path_matches_hold and ret_capital >= h,
                     "verdict": ("INCONSISTENT_INPUT" if not path_matches_hold
                                 else (("FAILS_EVEN_ON_AN_OPTIMISTIC_BOUND" if incomplete else "FAILS_ON_ALL_DECLARED_COSTS")
                                       if ret_capital < h
                                       else ("PASSES_ONLY_ON_MODELLED_COSTS" if incomplete
                                             else ("MEETS_ON_DECLARED_COSTS_FEES_NOT_ACCOUNT_VERIFIED" if not fees_verified
                                                   else "MEETS_ON_ALL_DECLARED_COSTS")))),
                     # a failure is conclusive whenever the input is consistent (costs only rise); a pass is conclusive only with every
                     # cost declared AND the fees read from the account
                     "conclusive": path_matches_hold and (ret_capital < h or (not incomplete and fees_verified))} for h in SCREENING_HURDLES_PCT],
        "reading": "arithmetic on declared inputs; a pass is not a profitability claim",
    }
    return out


def funding_paths(base_bps: float, n: int) -> Dict[str, List[float]]:
    """Named SCENARIO paths for sensitivity. None of them is a forecast."""
    half = n // 2
    shock = [base_bps] * n
    if n >= 3:
        shock[n // 3] = -20.0                       # one adverse settlement
    return {"constant": [base_bps] * n, "half": [base_bps * 0.5] * n, "zero": [0.0] * n,
            "reversal": [base_bps] * half + [-base_bps] * (n - half), "one_adverse_shock": shock}


def sensitivity(position: Position, costs: Costs, base_funding_bps: float, *,
                holds_days: Sequence[float] = (7, 14, 30, 60, 90), entry_basis_bps: float = 0.0,
                exit_basis_bps: float = 0.0) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for d in holds_days:
        pos = Position(**{**position.__dict__, "hold_days": float(d)})
        n = max(1, int(round(d * SETTLEMENTS_PER_DAY)))
        for name, path in funding_paths(base_funding_bps, n).items():
            r = compute(pos, costs, path, entry_basis_bps=entry_basis_bps, exit_basis_bps=exit_basis_bps,
                        funding_source="SCENARIO")
            rows.append({"hold_days": d, "path": name, "net_pnl_usd": r["net_pnl_usd"],
                         "annualised_pct_on_economic_capital": r["returns"]["annualised_pct_on_economic_capital"],
                         "incomplete": r["hurdles"][0]["incomplete"],
                         "meets_10": r["hurdles"][0]["meets"], "meets_15": r["hurdles"][1]["meets"],
                         "meets_20": r["hurdles"][2]["meets"]})
    return rows
