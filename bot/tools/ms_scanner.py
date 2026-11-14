"""Multi-symbol opportunity scanner: PURE functions from (instruments, snapshots, spec, params, decision_ms) to ranked ``ms_types.Opportunity`` rows.

    scan(instruments, snapshots, *, spec, candidate_id, spec_digest, decision_ms, params) -> list[Opportunity]

No I/O, no clock, no network, no credential, no randomness: the same inputs give the same list in the same order. It reads ONLY what the snapshots say was known at
``decision_ms`` (a snapshot stamped after the decision is refused, never read). It places nothing and promotes nothing: it estimates, and says what it could not.

Rules that hold for every row
  * Interval-normalised funding: each trailing print is converted with ITS OWN interval (``trailing_funding_intervals_min``); an unknown/unusable current interval excludes the
    symbol (FUNDING_INTERVAL_UNKNOWN), and missing/mismatched per-print intervals exclude it (FUNDING_INTERVAL_PER_PRINT_UNKNOWN); 480 and the current interval are never assumed for a print. Persistence defaults to a MODELLED 0.5 decay of the trailing funding received; a stronger provenance always ranks above a weaker one. The robust level is ``min(mean, median)`` of the trailing per-day rates (a single spike cannot lift it).
  * Tradability: a NEW entry is opened only in status ``Trading`` (ms_types.TRADABLE_STATUS). PreLaunch / PendingOpen / Settling / Delivering / Closed / any unknown status
    is excluded with NOT_TRADING:<status>; a launch in the future, or a delivery inside the hold, is excluded too.
  * Instrument rules: tick size, quantity step and minimum quantity must be known (else RULES_UNKNOWN); the order quantity is rounded DOWN to the step and must reach
    minOrderQty and the minimum notional.
  * Microstructure: no book / crossed book / book older than ``max_book_age_ms`` / unknown age / spread above ``max_spread_bps`` are excluded. Slippage is a depth WALK of the real book at
    the evaluated size (entry on one side, exit on the opposite side), never a constant.
  * Costs come from carry_costs' authoritative names, each with provenance: fees VENUE_STATED (base tier), the round-trip spread is the larger of the MEASURED book spread and
    the ASSUMED 0.4 bps, maker rebates / adverse selection / missed fills are MODELLED, spot-leg slippage (no spot book in a snapshot) is ASSUMED, financing is the owner's STATED alternative
    return on the capital the position commits (MODELLED once stated, the label the executor's edge gate gives an owner's own figure) and is UNKNOWN until it is stated: an unstated rate is ``FINANCING_UNKNOWN``, never a default of 0.0 (Round 20).
  * Unknown input => ``net_bps_horizon`` None and unrankable (``rank_score`` None). Never zero, never defaulted.
  * Capacity: the largest per-leg notional at which the round-trip slippage still fits inside the edge that remains after every other cost (bounded by visible depth, the
    venue's max market quantity and a participation cap of open interest).
  * ``rank_score`` = expected NET (never gross) bps over the hold, per unit of constrained capital, divided by the risk score (bps of one-sigma horizon uncertainty): a
    risk-adjusted net return on capital. Eligible rows sort by it (descending, ties by symbol); excluded rows follow, sorted by symbol.
  * A perp-only spread / directional book is NEVER a carry: kinds are ``SPOT_PERP_CARRY`` (hedged on Bybit spot), ``PERP_FUNDING_SPREAD`` (short high / long low, perp only) and
    ``PERP_DIRECTIONAL_FUNDING`` (short only); their risk models differ and so do their costs.
"""
from __future__ import annotations

import math
import os
import statistics
import sys
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import carry_costs as CC  # noqa: E402
import ms_types as T  # noqa: E402
from ms_types import BookLevel, CostLine, Instrument, MarketSnapshot, Opportunity, dec, funding_bps_per_day, round_down_to_step, weakest  # noqa: E402

SCHEMA = "ms_scanner/v1"
EXCLUSIONS = (
    "NO_INSTRUMENT", "NO_SNAPSHOT", "SNAPSHOT_FROM_FUTURE", "STALE_SNAPSHOT", "NEXT_FUNDING_IN_PAST", "NOT_TRADING", "NOT_LAUNCHED", "DELIVERY_WITHIN_HOLD",
    "NO_SPOT_HEDGE", "HEDGE_UNKNOWN", "NO_MARK", "FUNDING_INTERVAL_UNKNOWN", "FUNDING_INTERVAL_PER_PRINT_UNKNOWN", "INSUFFICIENT_FUNDING_HISTORY", "NO_BOOK", "BOOK_CROSSED", "BOOK_AGE_UNKNOWN", "BOOK_STALE",
    "SPREAD_TOO_WIDE", "LOW_TURNOVER", "LOW_OPEN_INTEREST", "RULES_UNKNOWN", "BELOW_MIN_ORDER_QTY", "BELOW_MIN_NOTIONAL", "DEPTH_INSUFFICIENT", "CAPACITY_BELOW_MIN",
    "NO_SPOT_MARK", "BASIS_TOO_WIDE", "VOL_UNKNOWN", "FINANCING_UNKNOWN", "NET_BELOW_MIN", "NET_UNKNOWN", "FUNDING_NOT_POSITIVE", "NO_PAIR")
DEFAULT_PARAMS: Dict[str, Any] = {
    "hold_days": 7.0, "formation_days": 3.0, "min_prints": 3, "notional_usd": 10_000.0, "max_book_age_ms": 5_000, "max_snapshot_age_ms": 60_000, "max_spread_bps": 10.0,
    "min_turnover24h_usd": None, "min_open_interest_usd": None, "min_net_bps": 0.0, "persistence": 0.5, "margin_frac": 0.2, "oi_participation": 0.01, "risk_floor_bps": 1.0,
    "spot_slippage_bps_per_leg": 1.0, "basis_floor_bps": 2.0, "max_entry_basis_bps": 50.0, "opportunity_cost_apr": None, "p_missed_fill": 0.25,
    "top_k": 3, "price_vol_bps_per_day": {}, "pair_corr": 0.0,
}
KINDS = T.OPP_KINDS
STYLES = ("TAKER", "MAKER_FIRST")
CAP_PROV = "MODELLED"


class ScanError(ValueError):
    """A malformed call (bad spec / params / decision time). Malformed MARKET data is never an error: it is an exclusion."""


def _params(params: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    p = dict(DEFAULT_PARAMS)
    for k, v in (params or {}).items():
        if k not in DEFAULT_PARAMS:
            raise ScanError(f"unknown scanner parameter {k!r}")
        p[k] = v
    if isinstance(p["persistence"], (int, float)) and not isinstance(p["persistence"], bool) and p["persistence"] > 1.0:
        raise ScanError("persistence is a haircut in (0, 1]: trailing funding cannot be assumed to grow over the hold")
    for k in ("hold_days", "formation_days", "notional_usd", "max_spread_bps", "margin_frac", "oi_participation", "risk_floor_bps", "persistence"):
        v = p[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            raise ScanError(f"parameter {k} must be a positive finite number")
    if not isinstance(p["min_prints"], int) or p["min_prints"] < 1 or not isinstance(p["top_k"], int) or p["top_k"] < 1:
        raise ScanError("min_prints and top_k must be positive integers")
    if not 0.0 <= float(p["p_missed_fill"]) <= 1.0 or not -1.0 <= float(p["pair_corr"]) <= 1.0:
        raise ScanError("p_missed_fill must be in [0,1] and pair_corr in [-1,1]")
    apr = p["opportunity_cost_apr"]
    # Round 20: None = the owner has NOT stated an alternative return (UNKNOWN, never 0.0); anything else must be a finite non-negative real number (a bool, a string, NaN, inf or a negative is refused,
    # never coerced: float("0.05") or float(True) would turn a mistake into a price)
    if apr is not None and (isinstance(apr, bool) or not isinstance(apr, (int, float)) or not math.isfinite(apr) or apr < 0):
        raise ScanError("opportunity_cost_apr must be None (unstated) or a finite non-negative number: the owner's alternative return per year on the capital the position commits")
    return p


def _fin(x: Any) -> Optional[float]:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


# ---------------------------------------------------------------- book arithmetic (pure)
def _levels(snap: MarketSnapshot, side: str) -> Tuple[BookLevel, ...]:
    """Levels a market order of ``side`` consumes: a Sell hits the bids, a Buy lifts the asks."""
    return snap.bids if side == "Sell" else snap.asks


def depth_notional_usd(levels: Sequence[BookLevel]) -> float:
    return float(sum(l.price * l.qty for l in levels))


def walk_slippage_bps(levels: Sequence[BookLevel], notional_usd: float, mid: float) -> Optional[float]:
    """Slippage BEYOND the top of book, in bps of mid, of a market order for ``notional_usd``: (best - vwap) / mid for a sell, (vwap - best) / mid for a buy (the book is ordered
    best-first, so the sign is the absolute distance). None when the visible depth cannot absorb the size (the size is then not executable, not free)."""
    if notional_usd <= 0:
        return 0.0
    if not levels or mid <= 0:
        return None
    best = levels[0].price
    left, spent, qty = notional_usd, 0.0, 0.0
    for l in levels:
        if l.price <= 0 or l.qty <= 0:
            return None
        take = min(left, l.price * l.qty)
        spent += take
        qty += take / l.price
        left -= take
        if left <= 1e-9:
            break
    if left > 1e-6:
        return None
    vwap = spent / qty
    return abs(vwap - best) / mid * 1e4


def _mid(s: MarketSnapshot) -> Optional[float]:
    if not s.bids or not s.asks:
        return None
    return 0.5 * (s.bids[0].price + s.asks[0].price)


# ---------------------------------------------------------------- per-symbol validation
def _snapshot_exclusions(inst: Optional[Instrument], snap: Optional[MarketSnapshot], decision_ms: int, p: Mapping[str, Any]) -> List[str]:
    ex: List[str] = []
    if inst is None:
        ex.append("NO_INSTRUMENT")
    if snap is None:
        ex.append("NO_SNAPSHOT")
    if inst is None or snap is None:
        return ex
    if snap.decision_ms > decision_ms:
        ex.append("SNAPSHOT_FROM_FUTURE")
    elif decision_ms - snap.decision_ms > p["max_snapshot_age_ms"]:
        ex.append("STALE_SNAPSHOT")
    if snap.next_funding_ms is not None and snap.next_funding_ms <= decision_ms:
        ex.append("NEXT_FUNDING_IN_PAST")             # a settlement has happened since this snapshot's funding view: its trailing prints are missing the newest one
    if inst.status != T.TRADABLE_STATUS:
        ex.append(f"NOT_TRADING:{inst.status}")
    if inst.launch_ms is not None and inst.launch_ms > decision_ms:
        ex.append("NOT_LAUNCHED")
    if inst.delivery_ms is not None and inst.delivery_ms <= decision_ms + int(p["hold_days"] * T.MS_PER_DAY):
        ex.append("DELIVERY_WITHIN_HOLD")
    return ex


def _book_exclusions(snap: MarketSnapshot, p: Mapping[str, Any]) -> List[str]:
    ex: List[str] = []
    if _fin(snap.mark) is None or snap.mark <= 0:
        ex.append("NO_MARK")
    if not snap.bids or not snap.asks:
        ex.append("NO_BOOK")
    else:
        sp = snap.spread_bps()
        if sp is None:
            ex.append("BOOK_CROSSED")
        elif sp > p["max_spread_bps"]:
            ex.append("SPREAD_TOO_WIDE")
        if snap.book_age_ms is None:
            ex.append("BOOK_AGE_UNKNOWN")
        elif snap.book_age_ms > p["max_book_age_ms"] or snap.book_age_ms < 0:
            ex.append("BOOK_STALE")
    if p["min_turnover24h_usd"] is not None and (snap.turnover24h_usd is None or snap.turnover24h_usd < p["min_turnover24h_usd"]):
        ex.append("LOW_TURNOVER")
    if p["min_open_interest_usd"] is not None and (snap.open_interest_usd is None or snap.open_interest_usd < p["min_open_interest_usd"]):
        ex.append("LOW_OPEN_INTEREST")
    return ex


def _funding(snap: MarketSnapshot, p: Mapping[str, Any]) -> Tuple[Optional[Dict[str, float]], List[str]]:
    """Robust interval-normalised funding from the trailing prints known at the decision. (stats, exclusions).

    EVERY print is normalised with ITS OWN interval (``trailing_funding_intervals_min``, the interval in force at that print): bps_per_day = bps * 1440 / interval_i. Empty or
    length-mismatched / unusable per-print intervals mean the per-print interval is UNKNOWN: FUNDING_INTERVAL_PER_PRINT_UNKNOWN, never the current interval as a default (a
    480 -> 240 change would otherwise overstate every older print up to 2x). The formation window is the newest prints covering ``formation_days`` of interval time."""
    iv = snap.funding_interval_min
    if funding_bps_per_day(1.0, iv) is None:
        return None, ["FUNDING_INTERVAL_UNKNOWN"]
    xs = [x for x in (snap.trailing_funding_bps_per_print or ())]
    ivs = list(snap.trailing_funding_intervals_min or ())
    if len(ivs) != len(xs) or (xs and any(funding_bps_per_day(1.0, v) is None for v in ivs)):
        return None, ["FUNDING_INTERVAL_PER_PRINT_UNKNOWN"]
    if any(_fin(x) is None for x in xs):
        return None, ["INSUFFICIENT_FUNDING_HISTORY"]
    need_min = float(p["formation_days"]) * 1440.0
    got, k = 0.0, 0
    for v in reversed(ivs):                      # newest first, until the window's interval time is covered
        k += 1
        got += float(v)
        if got >= need_min - 1e-9:
            break
    xs, ivs = xs[len(xs) - k:], ivs[len(ivs) - k:]
    if len(xs) < int(p["min_prints"]):
        return None, ["INSUFFICIENT_FUNDING_HISTORY"]
    day = [x * 1440.0 / v for x, v in zip(xs, ivs)]
    mean, med = statistics.fmean(day), statistics.median(day)
    sd = statistics.stdev(day) if len(day) > 1 else 0.0
    return {"mean_bps_per_day": mean, "median_bps_per_day": med, "sd_bps_per_day": sd, "n_prints": float(len(xs)), "interval_min": float(iv)}, []


def leg_funding_bps_per_day(f: Mapping[str, float], side: str, persistence: float) -> float:
    """Conservative expected funding per day for a leg: a SHORT receives the rate, so it is credited ``min(mean, median)`` (a spike cannot lift it); a LONG pays the rate, so it is
    charged ``max(mean, median)``. ``persistence`` (<= 1 haircut) scales only what is RECEIVED, never what is paid."""
    if side == "Sell":
        v = min(f["mean_bps_per_day"], f["median_bps_per_day"])
        return v * persistence if v > 0 else v
    v = -max(f["mean_bps_per_day"], f["median_bps_per_day"])
    return v * persistence if v > 0 else v


# ---------------------------------------------------------------- costs
def _fee_lines(kind: str, style: str) -> Tuple[List[CostLine], List[str]]:
    """Entry / exit fee lines per unit of position notional (bps), from carry_costs' constants. Carry: spot + perp each way; spread: gross-notional weights (each leg 0.5) so the two perp
    legs cost one perp leg's fee per unit of gross; directional: one perp leg. MAKER_FIRST rests the PERP entry at the MODELLED maker fee and exits taker; the spot leg has no maker figure
    in carry_costs and stays taker (never invented)."""
    perp_t, perp_m, spot_t = float(CC.TAKER_BPS_PERP), float(CC.MAKER_BPS_PERP_MODELLED), float(CC.TAKER_BPS_SPOT)
    reasons: List[str] = []
    if kind == "SPOT_PERP_CARRY":
        base_entry, base_exit, spot_part = perp_t + spot_t, perp_t + spot_t, spot_t
    else:
        base_entry, base_exit, spot_part = perp_t, perp_t, 0.0
    if style == "MAKER_FIRST":
        entry = (perp_m + spot_part)
        prov = "MODELLED"
        reasons.append("MAKER_FIRST: perp entry fee is the MODELLED maker fee; exit is taker; the spot leg (if any) stays taker (no maker figure in carry_costs)")
    else:
        entry, prov = base_entry, "VENUE_STATED"
    return [CostLine("entry_fee", entry, prov), CostLine("exit_fee", base_exit, "VENUE_STATED")], reasons


def _legs_for(kind: str, a: str, b: Optional[str]) -> Tuple[Tuple[str, str], ...]:
    if kind == "SPOT_PERP_CARRY":
        return ((f"SPOT:{a}", "Buy"), (a, "Sell"))
    if kind == "PERP_DIRECTIONAL_FUNDING":
        return ((a, "Sell"),)
    return ((a, "Sell"), (b or "", "Buy"))


def _rules_exclusions(inst: Instrument, snap: MarketSnapshot, notional_usd: float) -> List[str]:
    if inst.tick_size is None or inst.qty_step is None or inst.min_order_qty is None:
        return ["RULES_UNKNOWN"]
    try:
        if dec(inst.tick_size) <= 0 or dec(inst.qty_step) <= 0 or dec(inst.min_order_qty) <= 0:
            return ["RULES_UNKNOWN"]
        qty = dec(round_down_to_step(Decimal(str(notional_usd)) / Decimal(str(snap.mark)), inst.qty_step))
    except (ArithmeticError, ValueError):
        return ["RULES_UNKNOWN"]
    ex: List[str] = []
    if qty < dec(inst.min_order_qty):
        ex.append("BELOW_MIN_ORDER_QTY")
    if inst.min_notional_usd is not None and qty * Decimal(str(snap.mark)) < dec(inst.min_notional_usd):
        ex.append("BELOW_MIN_NOTIONAL")
    return ex


# ---------------------------------------------------------------- one candidate (single leg book or pair)
def committed_capital_factor(kind: str, margin_frac: float) -> float:
    """Capital committed per unit of position notional on the scanner's own basis: the spot leg paid in full plus perp margin for a carry, perp margin alone for a perp-only kind.
    The ranking denominator, the financing charge here and the pipeline's realised cost of capital all use this one function, so they cannot drift apart."""
    m = float(margin_frac)
    return (1.0 + m) if kind == "SPOT_PERP_CARRY" else m


def _evaluate(kind: str, style: str, legs_inst: Sequence[Tuple[Instrument, MarketSnapshot, str]], f_leg_day: Sequence[Dict[str, float]], candidate_id: str, spec_digest: str,
              decision_ms: int, p: Mapping[str, Any], base_exclusions: Sequence[str], legs: Tuple[Tuple[str, str], ...]) -> Opportunity:
    """Economics of one opportunity. ``legs_inst`` = the PERP legs (instrument, snapshot, side); carry has one perp leg plus the implicit spot leg."""
    hold = float(p["hold_days"])
    ex = list(base_exclusions)
    reasons: List[str] = []
    costs: List[CostLine] = []
    n_legs = len(legs_inst)
    w = 1.0 / n_legs if kind == "PERP_FUNDING_SPREAD" else 1.0
    margin = float(p["margin_frac"])
    capital_factor = committed_capital_factor(kind, margin)          # committed capital per unit of position notional: the SAME factor rank_score divides by
    # --- funding (interval-normalised, per unit of position notional)
    funding_day = 0.0
    for (inst, snap, side), f in zip(legs_inst, f_leg_day):
        funding_day += w * leg_funding_bps_per_day(f, side, float(p["persistence"]))
    funding_h = funding_day * hold
    reasons.append(f"funding persistence {float(p['persistence'])} (MODELLED): the trailing per-day funding RECEIVED is scaled by this over the hold; what is paid is never scaled")
    if kind != "PERP_FUNDING_SPREAD" and funding_day <= 0:
        ex.append("FUNDING_NOT_POSITIVE")
    # --- fees (carry_costs names, provenance)
    fee_lines, fee_reasons = _fee_lines(kind, style)
    reasons += fee_reasons
    costs += fee_lines
    # --- spread: the larger of the MEASURED book round trip and the ASSUMED carry_costs figure, per unit of position notional
    meas_rt = sum(w * (s.spread_bps() or 0.0) for _i, s, _sd in legs_inst)
    assumed_rt = float(CC.ASSUMED_ROUND_TRIP_SPREAD_BPS)
    spread_bps, spread_prov = (meas_rt, "MEASURED") if meas_rt >= assumed_rt else (assumed_rt, "ASSUMED")
    costs.append(CostLine("spread", spread_bps, spread_prov))
    reasons.append(f"round-trip spread {spread_bps:.3f} bps ({spread_prov}: measured book {meas_rt:.3f} vs carry_costs assumed {assumed_rt})")
    # --- adverse selection / missed fills (maker-first only; MODELLED)
    if style == "MAKER_FIRST":
        half_spread_entry = sum(w * 0.5 * (s.spread_bps() or 0.0) for _i, s, _sd in legs_inst)
        costs.append(CostLine("adverse_selection", half_spread_entry, "MODELLED"))
        extra_if_missed = (float(CC.TAKER_BPS_PERP) - float(CC.MAKER_BPS_PERP_MODELLED)) + half_spread_entry
        costs.append(CostLine("missed_fill", float(p["p_missed_fill"]) * extra_if_missed, "MODELLED"))
        reasons.append("MAKER_FIRST adverse selection = half the measured spread, missed fills = p x (taker - maker + half spread): MODELLED, not measured")
    # --- spot leg slippage (no spot book in a snapshot) and financing (carry only)
    if kind == "SPOT_PERP_CARRY":
        costs.append(CostLine("hedge", 2.0 * float(p["spot_slippage_bps_per_leg"]), "ASSUMED"))
        reasons.append("SPOT_DEPTH_UNKNOWN: a snapshot carries no spot book; spot-leg slippage is the ASSUMED parameter and capacity is bounded by the perp book only")
    # --- financing: the owner's alternative return on the capital this position COMMITS (carry: spot + margin; perp-only: margin). UNKNOWN until stated: an unstated rate is not a free loan.
    apr = p["opportunity_cost_apr"]
    if apr is None:
        costs.append(CostLine("financing", None, "UNKNOWN"))
        ex.append("FINANCING_UNKNOWN")
        reasons.append("FINANCING_UNKNOWN: the owner's alternative return on the committed capital is not stated (opportunity_cost_apr), so no net, rank or capacity is stated: an unpriced cost of capital is "
                       "not zero (state it, a stated 0 is a statement)")
    else:
        costs.append(CostLine("financing", float(apr) / 365.0 * 1e4 * hold * capital_factor, "MODELLED"))
        reasons.append(f"financing = the STATED alternative return {float(apr):.4%}/yr on {capital_factor:.2f}x committed capital per unit of notional (the ranking's own capital factor; margin_frac "
                       f"{margin} is a research assumption, 5x: the autopilot's executed structure commits 2.04x at leverage 1) over the {hold:g}-day hold: MODELLED (a stated parameter, not an observation)")
    # --- known-cost edge before slippage -> capacity by depth walk
    pre = [c.bps for c in costs]
    edge_pre = None if any(x is None for x in pre) else funding_h - sum(pre)

    def slip_rt(n: float) -> Optional[float]:
        tot = 0.0
        for inst, snap, side in legs_inst:
            mid = _mid(snap)
            a = walk_slippage_bps(_levels(snap, side), n, mid or 0.0)
            b = walk_slippage_bps(_levels(snap, "Buy" if side == "Sell" else "Sell"), n, mid or 0.0)
            if a is None or b is None:
                return None
            tot += w * (a + b)
        return tot
    depth = min(min(depth_notional_usd(_levels(s, sd)), depth_notional_usd(_levels(s, "Buy" if sd == "Sell" else "Sell"))) for _i, s, sd in legs_inst)
    cap = None
    if edge_pre is not None:
        if edge_pre <= 0:
            cap = 0.0
        else:
            top = slip_rt(depth * 0.999999)
            if top is not None and top <= edge_pre:
                cap = depth
            else:
                lo, hi = 0.0, depth
                for _ in range(50):
                    mid_n = 0.5 * (lo + hi)
                    sl = slip_rt(mid_n)
                    if sl is not None and sl <= edge_pre:
                        lo = mid_n
                    else:
                        hi = mid_n
                cap = lo
        # venue and participation bounds
        for inst, snap, _sd in legs_inst:
            if inst.max_mkt_order_qty is not None and snap.mark:
                cap = min(cap, float(dec(inst.max_mkt_order_qty)) * snap.mark)
            if snap.open_interest_usd is not None:
                cap = min(cap, float(p["oi_participation"]) * snap.open_interest_usd)
        reasons.append(f"capacity {cap:,.0f} USD per leg: size at which round-trip slippage exceeds the {edge_pre:.2f} bps edge left after other costs, bounded by visible depth {depth:,.0f}, max market qty and {p['oi_participation']:.0%} of OI [{CAP_PROV}]")
    n_eval = None if cap is None else min(float(p["notional_usd"]), cap)
    slippage = None
    if n_eval is not None and n_eval > 0:
        slippage = slip_rt(n_eval)
        if slippage is None:
            ex.append("DEPTH_INSUFFICIENT")
        else:
            costs.append(CostLine("slippage", slippage, "DERIVED"))
    elif n_eval is not None:
        ex.append("CAPACITY_BELOW_MIN")
    # --- instrument rules at the evaluated size
    if n_eval is not None and n_eval > 0:
        for inst, snap, _sd in legs_inst:
            r = _rules_exclusions(inst, snap, n_eval)
            ex += r
            if "BELOW_MIN_ORDER_QTY" in r or "BELOW_MIN_NOTIONAL" in r:
                ex.append("CAPACITY_BELOW_MIN")
    else:
        for inst, snap, _sd in legs_inst:
            if inst.tick_size is None or inst.qty_step is None or inst.min_order_qty is None:
                ex.append("RULES_UNKNOWN")
    # --- price leg and risk
    price_leg: Optional[float] = 0.0
    sig_funding = float(statistics.fmean(f["sd_bps_per_day"] for f in f_leg_day)) * hold
    sigmas = [sig_funding]
    vols = p["price_vol_bps_per_day"] if isinstance(p["price_vol_bps_per_day"], Mapping) else {}
    risk: Optional[float]
    if kind == "SPOT_PERP_CARRY":
        snap = legs_inst[0][1]
        if _fin(snap.spot_mark) is None or snap.spot_mark <= 0:
            ex.append("NO_SPOT_MARK")                    # without the spot price the hedge, the basis and the price-leg residual are all unknown: net is unknown
            price_leg, risk = None, None
        else:
            basis = (snap.mark - snap.spot_mark) / snap.spot_mark * 1e4
            if abs(basis) > float(p["max_entry_basis_bps"]):
                ex.append("BASIS_TOO_WIDE")
            sigmas.append(abs(basis) + float(p["basis_floor_bps"]))
            risk = math.sqrt(sum(x * x for x in sigmas))
            reasons.append(f"entry basis {basis:+.2f} bps (perp - spot); basis risk sigma = |basis| + ASSUMED floor {p['basis_floor_bps']} bps; price legs hedged (expected price leg 0)")
    else:
        vs = [_fin(vols.get(i.symbol)) for i, _s, _sd in legs_inst]
        if any(v is None or v < 0 for v in vs):
            ex.append("VOL_UNKNOWN")
            price_leg, risk = None, None
        else:
            hv = [v * math.sqrt(hold) for v in vs]                      # type: ignore[operator]
            if kind == "PERP_FUNDING_SPREAD":
                rho = float(p["pair_corr"])
                pair = math.sqrt(max(0.0, (w * hv[0]) ** 2 + (w * hv[1]) ** 2 + 2 * rho * (w * hv[0]) * (w * hv[1])))
                sigmas.append(pair)
                reasons.append(f"pair price sigma {pair:.1f} bps with assumed correlation {rho} (the spread is dollar-neutral, not beta-neutral by construction)")
            else:
                sigmas.append(hv[0])
                reasons.append("directional: one unhedged perp short, its full price sigma is risk (this is NOT a carry)")
            risk = math.sqrt(sum(x * x for x in sigmas))
    # --- net, rank
    net = None
    unknown = [c.name for c in costs if c.bps is None]
    if edge_pre is not None and slippage is not None and price_leg is not None and not unknown:
        net = funding_h + price_leg - sum(c.bps for c in costs)          # type: ignore[misc]
    if net is None and "NET_UNKNOWN" not in ex and not any(e in ex for e in ("DEPTH_INSUFFICIENT", "CAPACITY_BELOW_MIN", "VOL_UNKNOWN", "NO_SPOT_MARK")):
        ex.append("NET_UNKNOWN")
    if net is not None and net < float(p["min_net_bps"]):
        ex.append("NET_BELOW_MIN")
    ex = sorted(set(ex))
    rank = None
    if net is not None and risk is not None and not ex:
        rank = (net / capital_factor) / max(risk, float(p["risk_floor_bps"]))
    provs = [c.prov for c in costs] + ["DERIVED", CAP_PROV]
    if kind == "SPOT_PERP_CARRY":
        provs.append("MEASURED")
    prov = weakest(*provs) if net is not None else "UNKNOWN"
    return Opportunity(candidate_id=candidate_id, spec_digest=spec_digest, kind=kind, decision_ms=decision_ms, legs=legs, hold_days=hold,
                       expected_funding_bps_horizon=funding_h, expected_price_leg_bps=price_leg, costs=tuple(costs), net_bps_horizon=net, capacity_usd=cap,
                       risk_score=risk, rank_score=rank, prov=prov, reasons=tuple(reasons), exclusions=tuple(ex))


def _excluded(kind: str, symbol: str, candidate_id: str, spec_digest: str, decision_ms: int, hold: float, ex: Sequence[str], b: Optional[str] = None) -> Opportunity:
    legs = () if not symbol else (((symbol, "Sell"),) if b == "" else _legs_for(kind, symbol, b))      # a spread row excluded at the single-symbol stage names only its symbol
    return Opportunity(candidate_id=candidate_id, spec_digest=spec_digest, kind=kind, decision_ms=decision_ms, legs=legs, hold_days=hold,
                       expected_funding_bps_horizon=None, expected_price_leg_bps=None, costs=(), net_bps_horizon=None, capacity_usd=None, risk_score=None, rank_score=None,
                       prov="UNKNOWN", reasons=(), exclusions=tuple(sorted(set(ex))))


def _as_map(x: Union[Mapping[str, Any], Sequence[Any]], attr: str = "symbol") -> Dict[str, Any]:
    if isinstance(x, Mapping):
        return dict(x)
    return {getattr(v, attr): v for v in x}


def scan(instruments: Union[Mapping[str, Instrument], Sequence[Instrument]], snapshots: Union[Mapping[str, MarketSnapshot], Sequence[MarketSnapshot]], *, spec: Mapping[str, Any],
         candidate_id: str, spec_digest: str, decision_ms: int, params: Optional[Mapping[str, Any]] = None) -> List[Opportunity]:
    """Rank the opportunities of ``spec['kind']`` (default SPOT_PERP_CARRY) at ``decision_ms``; ``spec['style']`` is TAKER (default) or MAKER_FIRST. Every instrument yields a row:
    eligible rows (no exclusions, rankable) first by ``rank_score`` descending then symbol; excluded rows after, by symbol, each with its reason codes. For PERP_FUNDING_SPREAD a row is a PAIR
    (short a high-funding perp, long a low-funding perp) drawn from the top/bottom ``top_k`` eligible symbols."""
    if isinstance(decision_ms, bool) or not isinstance(decision_ms, int) or decision_ms <= 0:
        raise ScanError("decision_ms must be a positive integer (milliseconds UTC)")
    kind = spec.get("kind", "SPOT_PERP_CARRY") if isinstance(spec, Mapping) else None
    style = spec.get("style", "TAKER") if isinstance(spec, Mapping) else None
    if kind not in KINDS or style not in STYLES:
        raise ScanError(f"spec.kind must be one of {KINDS} and spec.style one of {STYLES}")
    p = _params(params)
    insts, snaps = _as_map(instruments), _as_map(snapshots)
    out: List[Opportunity] = []
    singles: Dict[str, Tuple[Instrument, MarketSnapshot, Dict[str, float], List[str]]] = {}
    for sym in sorted(set(insts) | set(snaps)):
        inst, snap = insts.get(sym), snaps.get(sym)
        ex = _snapshot_exclusions(inst, snap, decision_ms, p)
        if inst is not None and snap is not None and "SNAPSHOT_FROM_FUTURE" not in ex:
            ex += _book_exclusions(snap, p)
            if kind == "SPOT_PERP_CARRY":
                if inst.spot_hedge == "PERP_ONLY":
                    ex.append("NO_SPOT_HEDGE")
                elif inst.spot_hedge != "HEDGEABLE_ON_BYBIT":
                    ex.append("HEDGE_UNKNOWN")
            f, fex = _funding(snap, p)
            ex += fex
            if f is None or any(e in ex for e in ("NO_MARK", "NO_BOOK", "BOOK_CROSSED")):
                out.append(_excluded(kind, sym, candidate_id, spec_digest, decision_ms, float(p["hold_days"]), ex, "" if kind == "PERP_FUNDING_SPREAD" else None))
                continue
            if kind == "PERP_FUNDING_SPREAD":
                singles[sym] = (inst, snap, f, ex)            # a pair is judged as a whole below; here only the per-symbol gates (status, freshness, book, interval) apply
                continue
            out.append(_evaluate(kind, style, [(inst, snap, "Sell")], [f], candidate_id, spec_digest, decision_ms, p, ex, _legs_for(kind, sym, None)))
        else:
            out.append(_excluded(kind, sym, candidate_id, spec_digest, decision_ms, float(p["hold_days"]), ex, "" if kind == "PERP_FUNDING_SPREAD" else None))
    if kind == "PERP_FUNDING_SPREAD":
        out += _pairs(singles, style, candidate_id, spec_digest, decision_ms, p)
    rankable = sorted((o for o in out if o.rank_score is not None and not o.exclusions), key=lambda o: (T.PROVS.index(o.prov) if o.prov in T.PROVS else len(T.PROVS), -o.rank_score, o.legs))        # type: ignore[operator]
    rest = sorted((o for o in out if o.rank_score is None or o.exclusions), key=lambda o: (o.legs, o.exclusions))
    return rankable + rest


def _pairs(singles: Mapping[str, Tuple[Instrument, MarketSnapshot, Dict[str, float], List[str]]], style: str, candidate_id: str, spec_digest: str, decision_ms: int,
           p: Mapping[str, Any]) -> List[Opportunity]:
    """Pairs short-high / long-low from the top-k and bottom-k symbols that passed every single-leg gate (their own exclusions, ignoring the funding sign). Symbols that failed a gate
    are reported as excluded rows; a spread needs at least two eligible symbols."""
    rows: List[Opportunity] = []
    ok: List[str] = []
    for sym in sorted(singles):
        inst, snap, f, bad = singles[sym]
        if bad:
            rows.append(_excluded("PERP_FUNDING_SPREAD", sym, candidate_id, spec_digest, decision_ms, float(p["hold_days"]), bad, ""))
        elif _fin(dict(p["price_vol_bps_per_day"]).get(sym)) is None:
            rows.append(_excluded("PERP_FUNDING_SPREAD", sym, candidate_id, spec_digest, decision_ms, float(p["hold_days"]), ["VOL_UNKNOWN"], ""))
        else:
            ok.append(sym)
    if len(ok) < 2:
        if singles:
            rows.append(_excluded("PERP_FUNDING_SPREAD", "", candidate_id, spec_digest, decision_ms, float(p["hold_days"]), ["NO_PAIR"], ""))
        return rows
    ranked = sorted(ok, key=lambda s: (-min(singles[s][2]["mean_bps_per_day"], singles[s][2]["median_bps_per_day"]), s))
    k = min(int(p["top_k"]), len(ranked) // 2)
    shorts, longs = ranked[:k], ranked[-k:]
    for a in shorts:
        for b in longs:
            ia, sa, fa, _x = singles[a]
            ib, sb, fb, _y = singles[b]
            rows.append(_evaluate("PERP_FUNDING_SPREAD", style, [(ia, sa, "Sell"), (ib, sb, "Buy")], [fa, fb], candidate_id, spec_digest, decision_ms, p, [], ((a, "Sell"), (b, "Buy"))))
    for s in ranked[k:len(ranked) - k]:                       # middle of the ranking: not drawn as a leg, reported so nothing disappears silently
        rows.append(_excluded("PERP_FUNDING_SPREAD", s, candidate_id, spec_digest, decision_ms, float(p["hold_days"]), ["NO_PAIR"], ""))
    return rows
