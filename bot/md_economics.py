"""Carry entry economics from the real order book.

The existing entry gate (`entry_benefit_gate` -> `strategy_evidence.evaluate_entry_benefit`)
prices spread and slippage from this book's own past fills; with no fills it is
blind. This module measures them from the LIVE depth instead, and keeps four
kinds of number apart, because passing one says nothing about the next:

  OBSERVED    stated by the venue in a validated frame (bid, ask, mark, index,
              the funding forecast, the next settlement time, open interest)
  CALCULATED  arithmetic on observed values, no assumption (mid, spread bps,
              executable average price of walking the displayed book for a
              quantity, hours to the next settlement)
  ESTIMATED   an assumption or a statistic that could be wrong (the expected
              funding per settlement, its uncertainty, the exit price taken from
              TODAY's book, the fee tier, a latency allowance)
  UNKNOWN     not available; named, never defaulted to zero (the account's real
              fee tier, available collateral, latency, borrow rate, the owner's
              opportunity cost until supplied)

It does not trade, does not read the account, and never extrapolates past the
displayed book: a quantity larger than the visible depth is UNAVAILABLE, not a
worse price.

Costs are bps of position notional over the ROUND TRIP, the units
`evaluate_entry_benefit` expects. A leg is walked as a market (taker) order:
entry sells the perp into the bids (and, when the book buys the long side, buys
spot from the asks); the exit takes the other side. The exit is priced on
today's book, which is an estimate, and is labelled one.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import carry_costs as _costs
import strategy_evidence as _se

OBSERVED, CALCULATED, ESTIMATED, UNKNOWN = "OBSERVED", "CALCULATED", "ESTIMATED", "UNKNOWN"
CLASSES = (OBSERVED, CALCULATED, ESTIMATED, UNKNOWN)

MEASURED = "MEASURED"
UNAVAILABLE = "UNAVAILABLE"

DEPTH_SOURCE = "BOOK_WALK_NO_LATENCY"
FEE_PUBLISHED_DEFAULT = "PUBLISHED_DEFAULT_TIER_UNVERIFIED_FOR_ACCOUNT"
FEE_ACCOUNT_VERIFIED = "ACCOUNT_VERIFIED"

OVERLAY, ACQUIRE = "overlay", "acquire"


def _fin(x: Any) -> bool:
    return not isinstance(x, bool) and isinstance(x, (int, float)) and math.isfinite(x)


def _pos(x: Any) -> bool:
    return _fin(x) and x > 0


@dataclass(frozen=True)
class Item:
    name: str
    klass: str
    value: Optional[float]
    unit: str
    source: str
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "class": self.klass, "value": self.value, "unit": self.unit,
                "source": self.source, "note": self.note}


@dataclass(frozen=True)
class FeeSchedule:
    perp_taker_bps: float
    spot_taker_bps: float
    source: str = FEE_PUBLISHED_DEFAULT

    @staticmethod
    def published_default() -> "FeeSchedule":
        return FeeSchedule(_costs.TAKER_BPS_PERP, _costs.TAKER_BPS_SPOT, FEE_PUBLISHED_DEFAULT)

    @staticmethod
    def from_account(rates: Mapping[str, Any]) -> "FeeSchedule":
        """From `broker.get_fee_rates(...)` results for the perp and the spot leg:
        {"perp": {"taker_bps": x}, "spot": {"taker_bps": y}}. Anything unusable
        raises: an unverified rate is never relabelled as verified."""
        perp, spot = float(rates["perp"]["taker_bps"]), float(rates["spot"]["taker_bps"])
        if not (_pos(perp) and _pos(spot)):
            raise ValueError("account fee rates must be positive finite numbers")
        return FeeSchedule(perp, spot, FEE_ACCOUNT_VERIFIED)


# -- walking the displayed book ------------------------------------------------------------------------

@dataclass(frozen=True)
class LegWalk:
    leg: str                          # "perp_sell_entry", "perp_buy_exit", ...
    side: str                         # "sell" | "buy"
    qty: float
    status: str                       # MEASURED | UNAVAILABLE
    reason: str
    touch: Optional[float]
    avg_price: Optional[float]
    half_spread_bps: Optional[float]  # (touch vs mid), the cost of crossing the spread
    slippage_bps: Optional[float]     # (avg fill vs touch), the cost of walking the book
    levels_used: int = 0


def walk_leg(leg: str, side: str, qty: float, bids: Sequence[Tuple[float, float]],
             asks: Sequence[Tuple[float, float]]) -> LegWalk:
    """Consume `qty` from the displayed side. Never extrapolates."""
    def bad(reason: str) -> LegWalk:
        return LegWalk(leg, side, qty, UNAVAILABLE, reason, None, None, None, None)

    if not _pos(qty):
        return bad("QTY_NOT_POSITIVE")
    if not bids or not asks:
        return bad("BOOK_ONE_SIDED_OR_EMPTY")
    for lad in (bids, asks):
        if any(not (_pos(p) and _fin(q) and q >= 0) for p, q in lad):
            return bad("BOOK_LEVEL_INVALID")
    if bids[0][0] >= asks[0][0]:
        return bad("BOOK_CROSSED")
    mid = (bids[0][0] + asks[0][0]) / 2.0
    levels = bids if side == "sell" else asks
    left, cost, used = float(qty), 0.0, 0
    for price, size in levels:
        take = min(left, size)
        cost += take * price
        left -= take
        used += 1
        if left <= 1e-12:
            left = 0.0
            break
    if left > 0.0:
        return LegWalk(leg, side, qty, UNAVAILABLE, "DEPTH_INSUFFICIENT", levels[0][0], None, None, None, used)
    avg = cost / qty
    touch = levels[0][0]
    half = abs(touch - mid) / mid * 1e4
    slip = abs(avg - touch) / touch * 1e4
    return LegWalk(leg, side, qty, MEASURED, "", touch, avg, half, slip, used)


@dataclass(frozen=True)
class DepthCosts:
    status: str                       # MEASURED | UNAVAILABLE
    reasons: Tuple[str, ...]
    spread_bps: Optional[float]       # round trip, bps of notional
    slippage_bps: Optional[float]     # round trip, bps of notional (book walk only)
    latency_allowance_bps: Optional[float]
    legs: Tuple[LegWalk, ...]
    source: str = DEPTH_SOURCE

    def as_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "reasons": list(self.reasons), "spread_bps": self.spread_bps,
                "slippage_bps": self.slippage_bps, "latency_allowance_bps": self.latency_allowance_bps,
                "source": self.source,
                "legs": [{"leg": l.leg, "side": l.side, "qty": l.qty, "status": l.status,
                          "reason": l.reason, "touch": l.touch, "avg_price": l.avg_price,
                          "half_spread_bps": l.half_spread_bps, "slippage_bps": l.slippage_bps,
                          "levels_used": l.levels_used} for l in self.legs]}


def depth_costs(depth: Mapping[str, Any], *, mode: str, notional_usd: float, ref_price: float,
                latency_allowance_bps: Optional[float] = None) -> DepthCosts:
    """Round-trip spread and book-walk slippage for one entry and its exit, from
    ``depth`` = {"perp_bids","perp_asks","spot_bids","spot_asks"} (best first).

    ``latency_allowance_bps`` is the owner's allowance for the price moving
    between the decision and the fill. Unsupplied, it stays None and the status
    is UNAVAILABLE: the book walk alone is a lower bound on cost, not the cost."""
    reasons: List[str] = []
    if mode not in (OVERLAY, ACQUIRE):
        return DepthCosts(UNAVAILABLE, ("MODE_UNKNOWN",), None, None, None, ())
    if not (_pos(notional_usd) and _pos(ref_price)):
        return DepthCosts(UNAVAILABLE, ("NOTIONAL_OR_PRICE_INVALID",), None, None, None, ())
    qty = notional_usd / ref_price
    pb, pa = depth.get("perp_bids") or (), depth.get("perp_asks") or ()
    legs: List[LegWalk] = [walk_leg("perp_sell_entry", "sell", qty, pb, pa),
                           walk_leg("perp_buy_exit", "buy", qty, pb, pa)]
    if mode == ACQUIRE:
        sb, sa = depth.get("spot_bids") or (), depth.get("spot_asks") or ()
        legs += [walk_leg("spot_buy_entry", "buy", qty, sb, sa),
                 walk_leg("spot_sell_exit", "sell", qty, sb, sa)]
    for l in legs:
        if l.status != MEASURED:
            reasons.append(f"{l.leg}:{l.reason}")
    if reasons:
        return DepthCosts(UNAVAILABLE, tuple(reasons), None, None, None, tuple(legs))
    # Each leg's cost is a fraction of ITS OWN notional (== the position notional
    # to first order), summed over the legs the round trip crosses.
    spread = sum(l.half_spread_bps for l in legs)
    slip = sum(l.slippage_bps for l in legs)
    if latency_allowance_bps is not None:
        if not (_fin(latency_allowance_bps) and latency_allowance_bps >= 0):
            return DepthCosts(UNAVAILABLE, ("LATENCY_ALLOWANCE_INVALID",), spread, slip, None, tuple(legs))
        return DepthCosts(MEASURED, (), spread, slip, float(latency_allowance_bps), tuple(legs))
    return DepthCosts(UNAVAILABLE, ("LATENCY_ALLOWANCE_NOT_SUPPLIED",), spread, slip, None, tuple(legs))


# -- the full entry economics ----------------------------------------------------------------------------

@dataclass(frozen=True)
class EntryEconomics:
    items: Tuple[Item, ...]
    depth: DepthCosts
    decision: Optional[_se.EntryDecision]
    complete: bool
    missing: Tuple[str, ...]

    def by_class(self) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {c: [] for c in CLASSES}
        for i in self.items:
            out[i.klass].append(i.as_dict())
        return out

    @property
    def net_benefit_bps(self) -> Optional[float]:
        return None if self.decision is None else self.decision.net_benefit_bps


def income_uncertainty_bps(history_bps: Sequence[float], hold_days: float,
                           settlements_per_day: float = _costs.FUNDING_PERIODS_PER_DAY) -> Optional[float]:
    """One standard deviation of the hold's funding income, treating the
    settlements as fully correlated (sd * n): the conservative reading the
    existing gate uses, not a fitted one. None below two prints."""
    xs = [float(x) for x in history_bps if _fin(x)]
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
    return sd * settlements_per_day * hold_days


def build_entry_economics(view: Any, *, mode: str, notional_usd: float,
                          funding_history_bps: Sequence[float], fees: Optional[FeeSchedule] = None,
                          hold_days: float = _costs.DEMONSTRATED_HOLD_DAYS,
                          borrow_apr: Optional[float] = None,
                          opportunity_cost_apr: Optional[float] = None,
                          income_haircut_fraction: float = 0.0,
                          latency_allowance_bps: Optional[float] = None,
                          account: Optional[Mapping[str, Any]] = None) -> EntryEconomics:
    """Everything an entry decision needs, each number with its class. `view` is
    a `md_state.MarketView`; `account` optionally carries private reads the
    caller made on the signed account ({"available_margin_usd": x, ...})."""
    items: List[Item] = []
    missing: List[str] = []

    def add(name, klass, value, unit, source, note=""):
        items.append(Item(name, klass, value, unit, source, note))

    # OBSERVED -----------------------------------------------------------------------------------------
    for name, val, unit in (("perp_bid", view.perp_bid, "USDT"), ("perp_ask", view.perp_ask, "USDT"),
                            ("spot_bid", view.spot_bid, "USDT"), ("spot_ask", view.spot_ask, "USDT"),
                            ("perp_mark", view.perp_mark, "USDT"), ("perp_index", view.perp_index, "USDT"),
                            ("funding_rate_forecast", None if view.funding_rate is None else view.funding_rate * 1e4,
                             "bps per settlement"),
                            ("next_funding_ms", view.next_funding_ms, "ms epoch"),
                            ("open_interest", view.open_interest, "BTC")):
        if val is None:
            missing.append(name)
        add(name, OBSERVED, None if val is None else float(val), unit, f"public feed ({view.environment})")
    # CALCULATED --------------------------------------------------------------------------------------------
    pm, sm = view.perp_mid, view.spot_mid
    add("perp_spread_bps", CALCULATED, view.perp_spread_bps, "bps", "perp bid/ask")
    add("spot_spread_bps", CALCULATED, view.spot_spread_bps, "bps", "spot bid/ask")
    add("mid_basis_bps", CALCULATED, view.basis_bps, "bps", "perp mid vs spot mid")
    add("mark_vs_index_bps", CALCULATED, view.mark_basis_bps, "bps", "perp mark vs index")
    hrs = None if view.next_funding_ms is None else (view.next_funding_ms - view.observed_wall_ms) / 3.6e6
    add("hours_to_next_settlement", CALCULATED, hrs, "hours", "nextFundingTime - observation time")
    settle_h = None if view.funding_interval_h is None else float(view.funding_interval_h)
    spd = None if not settle_h else 24.0 / settle_h
    add("settlements_per_day", CALCULATED, spd, "1/day", "fundingIntervalHour")
    ref = view.perp_mark if _pos(view.perp_mark) else pm
    depth_in = {"perp_bids": view.perp_bids, "perp_asks": view.perp_asks,
                "spot_bids": view.spot_bids, "spot_asks": view.spot_asks}
    dc = depth_costs(depth_in, mode=mode, notional_usd=notional_usd,
                     ref_price=ref if _pos(ref) else float("nan"), latency_allowance_bps=latency_allowance_bps)
    for reason in dc.reasons:
        if reason != "LATENCY_ALLOWANCE_NOT_SUPPLIED":          # reported below as its own unknown
            missing.append(f"depth:{reason}")
    for l in dc.legs:
        add(f"exec_avg_price_{l.leg}", CALCULATED, l.avg_price, "USDT",
            f"{DEPTH_SOURCE}: walk of the displayed book for {l.qty:.6f} BTC",
            l.reason if l.status != MEASURED else "")
    add("book_walk_spread_bps_round_trip", CALCULATED, dc.spread_bps, "bps of notional", DEPTH_SOURCE)
    add("book_walk_slippage_bps_round_trip", CALCULATED, dc.slippage_bps, "bps of notional", DEPTH_SOURCE)
    # ESTIMATED -----------------------------------------------------------------------------------------------
    fees = fees or FeeSchedule.published_default()
    perp_legs = 2
    spot_legs = 2 if mode == ACQUIRE else 0
    fee_bps = perp_legs * fees.perp_taker_bps + spot_legs * fees.spot_taker_bps
    add("round_trip_fee_bps", ESTIMATED, fee_bps, "bps of notional", fees.source,
        "taker on every leg" + ("" if fees.source == FEE_ACCOUNT_VERIFIED
                                 else "; the account's actual tier is not verified"))
    hist = [float(x) for x in funding_history_bps if _fin(x)]
    smoothed = _costs.ewma_funding_bps(hist)
    add("expected_funding_bps_per_settlement", ESTIMATED, smoothed, "bps per settlement",
        f"EWMA of {len(hist)} settled prints (REST history)",
        "" if smoothed is not None else f"needs >= {_costs.EWMA_MIN_PRINTS} settled prints")
    if smoothed is None:
        missing.append("expected_funding")
    unc = income_uncertainty_bps(hist, hold_days, spd or _costs.FUNDING_PERIODS_PER_DAY)
    add("income_uncertainty_bps", ESTIMATED, unc, "bps over the hold", "sd of settled prints x settlements, fully correlated")
    if unc is None:
        missing.append("income_uncertainty")
    add("hold_days", ESTIMATED, float(hold_days), "days", "carry_costs.DEMONSTRATED_HOLD_DAYS")
    add("exit_priced_on_todays_book", ESTIMATED, 1.0, "flag", DEPTH_SOURCE,
        "the exit's spread and slippage are taken from the book as it is now")
    add("income_haircut_fraction", ESTIMATED, float(income_haircut_fraction), "fraction", "operator")
    if latency_allowance_bps is not None:
        add("latency_allowance_bps", ESTIMATED, float(latency_allowance_bps), "bps of notional", "owner-supplied")
    # UNKNOWN ---------------------------------------------------------------------------------------------------
    if latency_allowance_bps is None:
        missing.append("latency_allowance")
        add("latency_allowance_bps", UNKNOWN, None, "bps of notional", "none",
            "no order has been timed against the venue; the book walk alone is a lower bound on cost")
    if fees.source != FEE_ACCOUNT_VERIFIED:
        add("account_fee_tier", UNKNOWN, None, "bps", "none", "needs the signed fee-rate endpoint")
    acct = dict(account or {})
    avail = acct.get("available_margin_usd")
    if _pos(avail):
        add("available_margin_usd", OBSERVED, float(avail), "USD", "signed account read")
    else:
        missing.append("available_margin")
        add("available_margin_usd", UNKNOWN, None, "USD", "none", "a private account read; the public feed cannot state it")
    for key, label in (("borrow_apr", borrow_apr), ("opportunity_cost_apr", opportunity_cost_apr)):
        if label is None:
            missing.append(key)
            add(key, UNKNOWN, None, "fraction per year", "none", "owner-supplied; 0.0 is a statement, not a default")
        else:
            add(key, ESTIMATED, float(label), "fraction per year", "owner-supplied")
    add("capital_usage_usd", CALCULATED, float(notional_usd), "USD",
        "notional; initial margin is leverage-dependent and read from the account, not assumed")

    decision = None
    if dc.status == MEASURED and smoothed is not None and unc is not None \
            and borrow_apr is not None and opportunity_cost_apr is not None and spd:
        adverse = _adverse_basis(view.perp_mark, sm)
        decision = _se.evaluate_entry_benefit(
            expected_funding_bps_per_settlement=smoothed, hold_days=hold_days, notional_usd=notional_usd,
            round_trip_fee_bps=fee_bps, spread_bps=dc.spread_bps,
            expected_slippage_bps=dc.slippage_bps + (dc.latency_allowance_bps or 0.0),
            borrow_apr=borrow_apr, opportunity_cost_apr=opportunity_cost_apr,
            income_haircut_fraction=income_haircut_fraction, income_uncertainty_bps=unc,
            evidence=None, settlements_per_day=spd,
            basis_convergence_bps=adverse if adverse is not None else _se.UNAVAILABLE)
    return EntryEconomics(tuple(items), dc, decision, complete=not missing and decision is not None,
                          missing=tuple(sorted(set(missing))))


def _adverse_basis(mark: Any, spot_mid: Any) -> Optional[float]:
    if not (_pos(mark) and _pos(spot_mid)):
        return None
    return max(0.0, -_costs.basis_bps(float(mark), float(spot_mid)))
