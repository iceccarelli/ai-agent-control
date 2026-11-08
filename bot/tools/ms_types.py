"""Shared, dependency-free vocabulary of the MULTI-SYMBOL portfolio / execution layer (scanner -> portfolio -> order groups -> replay/paper -> accounting).

Owned by the integration lead. Workers import from here and may NOT change a field's meaning; if a contract must change they ask the lead. Nothing in this module
imports a venue client, a transport, a credential or the clock: it is pure data. Money is USD, rates are BASIS POINTS unless a name says ``frac``; timestamps are
integer milliseconds UTC; every number that can be UNKNOWN is Optional and None means unknown (never zero).

Provenance of every economic input (``Prov``): MEASURED (observed in captured data or the owner account), VENUE_STATED (the venue's published figure), DERIVED
(computed from measured inputs), MODELLED (a stated model, e.g. queue/fill model), ASSUMED (nobody measured it), UNKNOWN. A result inherits the WEAKEST provenance of its
inputs; an ASSUMED/UNKNOWN input can never produce a PASS.

Execution here is REPLAY / PAPER ONLY. ``SIM`` transports are the only ones the portfolio layer may be wired to in this repository state; a real or testnet
transport is a separately reviewed capability that is disabled and never dispatched in this mission.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Tuple

MS_PER_MIN = 60_000
MS_PER_DAY = 86_400_000

PROVS = ("MEASURED", "VENUE_STATED", "DERIVED", "MODELLED", "ASSUMED", "UNKNOWN")
_PROV_RANK = {p: i for i, p in enumerate(PROVS)}          # larger = weaker


def weakest(*provs: str) -> str:
    """The weakest provenance among ``provs`` (UNKNOWN if none given or any is not a known label)."""
    if not provs or any(p not in _PROV_RANK for p in provs):
        return "UNKNOWN"
    return max(provs, key=lambda p: _PROV_RANK[p])


# ---------------------------------------------------------------- instruments and market state

TRADABLE_STATUS = "Trading"          # the ONLY status in which a NEW entry may be opened


@dataclass(frozen=True)
class Instrument:
    """One USDT-linear perpetual as recorded by a verified universe capture (universe_capture schema-2 row). ``funding_interval_min`` None = unknown."""
    symbol: str
    symbol_id: Optional[str]
    status: str                              # Trading | PreLaunch | PendingOpen | Settling | Delivering | Closed | <unknown string>
    launch_ms: Optional[int]
    delivery_ms: Optional[int]               # perpetual delisting time when known
    funding_interval_min: Optional[int]
    base_coin: str = ""
    quote_coin: str = "USDT"
    tick_size: Optional[str] = None          # decimal STRINGS exactly as the venue states them
    qty_step: Optional[str] = None
    min_order_qty: Optional[str] = None
    max_order_qty: Optional[str] = None
    max_mkt_order_qty: Optional[str] = None
    min_notional_usd: Optional[str] = None
    max_leverage: Optional[str] = None
    spot_symbol: Optional[str] = None        # the SPOT pair that can hedge it on the same venue, None = none/unknown (see spot_hedge below)
    spot_hedge: str = "HEDGE_UNKNOWN"        # HEDGEABLE_ON_BYBIT | PERP_ONLY | HEDGE_UNKNOWN  (CURRENT snapshot only; never projected backwards)

    def tradable_at(self, t_ms: int) -> bool:
        """Existed and was tradable at t: status Trading is only known NOW; for history use lifecycle bounds (launch <= t < delivery)."""
        if self.launch_ms is not None and t_ms < self.launch_ms:
            return False
        if self.delivery_ms is not None and t_ms >= self.delivery_ms:
            return False
        return True


@dataclass(frozen=True)
class BookLevel:
    price: float
    qty: float            # base units


@dataclass(frozen=True)
class MarketSnapshot:
    """What was KNOWN about one symbol at ``decision_ms``. Every field is from data whose local receive time is <= decision_ms (the loader enforces it).
    Missing/stale fields are None, never defaulted."""
    symbol: str
    decision_ms: int
    mark: Optional[float] = None
    index: Optional[float] = None
    bids: Tuple[BookLevel, ...] = ()
    asks: Tuple[BookLevel, ...] = ()
    book_age_ms: Optional[int] = None
    last_funding_bps_per_print: Optional[float] = None
    trailing_funding_bps_per_print: Tuple[float, ...] = ()      # oldest -> newest, all with settlement time <= decision_ms
    trailing_funding_intervals_min: Tuple[int, ...] = ()        # the funding interval IN FORCE AT EACH trailing print (same length as the prints, oldest -> newest); empty/mismatched => per-print interval UNKNOWN, a consumer must not normalise the prints with funding_interval_min
    funding_interval_min: Optional[int] = None                  # the interval in force AT decision time (not today's)
    next_funding_ms: Optional[int] = None
    open_interest_usd: Optional[float] = None
    turnover24h_usd: Optional[float] = None
    spot_mark: Optional[float] = None

    def spread_bps(self) -> Optional[float]:
        if not self.bids or not self.asks or self.bids[0].price <= 0 or self.asks[0].price <= 0 or self.asks[0].price < self.bids[0].price:
            return None
        mid = 0.5 * (self.asks[0].price + self.bids[0].price)
        return (self.asks[0].price - self.bids[0].price) / mid * 1e4


# ---------------------------------------------------------------- opportunity scoring

OPP_KINDS = ("SPOT_PERP_CARRY", "PERP_FUNDING_SPREAD", "PERP_DIRECTIONAL_FUNDING")      # different strategies, different risk models; the third is NOT a carry


@dataclass(frozen=True)
class CostLine:
    name: str                       # entry_fee | exit_fee | spread | slippage | adverse_selection | missed_fill | hedge | financing | operational
    bps: Optional[float]            # None = UNKNOWN
    prov: str


@dataclass(frozen=True)
class Opportunity:
    candidate_id: str
    spec_digest: str
    kind: str
    decision_ms: int
    legs: Tuple[Tuple[str, str], ...]               # ((symbol, "Sell"|"Buy"), ...) perp legs; spot legs are named "SPOT:<symbol>"
    hold_days: float
    expected_funding_bps_horizon: Optional[float]   # interval-normalised, formation-window information only
    expected_price_leg_bps: Optional[float]         # usually 0 for a hedged carry; for a spread = measured residual
    costs: Tuple[CostLine, ...]
    net_bps_horizon: Optional[float]                # None if any required input is UNKNOWN
    capacity_usd: Optional[float]
    risk_score: Optional[float]                     # higher = riskier; see ms_risk
    rank_score: Optional[float]                     # expected risk-adjusted NET per unit of constrained capital; None = unrankable
    prov: str                                       # weakest provenance among the inputs
    reasons: Tuple[str, ...] = ()
    exclusions: Tuple[str, ...] = ()                # non-empty => NOT eligible, with the reason codes


# ---------------------------------------------------------------- order groups and the lifecycle

class GroupState(str, Enum):
    PLANNED = "PLANNED"
    RISK_APPROVED = "RISK_APPROVED"
    SUBMITTED = "SUBMITTED"
    PARTIAL = "PARTIAL"                 # at least one leg partially filled
    HEDGED = "HEDGED"                   # all legs filled to within tolerance
    UNHEDGED = "UNHEDGED"               # a leg failed/partial beyond tolerance: no new exposure, recovery policy runs
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME" # lost ack / crash between acceptance and persistence: reconcile BEFORE any resend
    EXITING = "EXITING"
    CLOSED = "CLOSED"
    FAILED = "FAILED"
    HALTED = "HALTED"


GROUP_STATES = tuple(s.value for s in GroupState)
TERMINAL_STATES = ("CLOSED", "FAILED")


@dataclass(frozen=True)
class LegPlan:
    leg_id: str                         # durable, unique within the group (idempotency key = group_id + leg_id)
    symbol: str
    category: str                       # "linear" | "spot"
    side: str                           # "Buy" | "Sell"
    qty: str                            # decimal string, already rounded to the instrument qty_step
    order_type: str                     # "Limit" | "Market"
    price: Optional[str]                # limit price, tick-rounded; None for Market
    post_only: bool = False
    reduce_only: bool = False
    time_in_force: str = "GTC"


@dataclass(frozen=True)
class OrderGroupPlan:
    group_id: str
    candidate_id: str
    spec_digest: str
    policy_version: str
    created_ms: int
    legs: Tuple[LegPlan, ...]
    style: str                          # "MAKER_FIRST" | "TAKER"
    max_wait_ms: int                    # a maker leg unfilled after this is cancelled; never silently converted
    notional_usd: float                 # the group's gross notional at plan time (sum of |leg qty*ref price|)


@dataclass(frozen=True)
class Fill:
    fill_id: str                        # venue exec id; duplicates are dropped by id
    group_id: str
    leg_id: str
    symbol: str
    side: str
    qty: float
    price: float
    fee_usd: float                      # >= 0 charged; rebates are NOT assumed
    fee_ccy: str
    liquidity: str                      # "MAKER" | "TAKER"
    ts_ms: int
    prov: str                           # "SIMULATED" | "MODELLED" | "MEASURED"


# ---------------------------------------------------------------- helpers every module uses (single implementations)

def dec(x: Any) -> Decimal:
    return Decimal(str(x))


def round_down_to_step(x: Any, step: Any) -> str:
    """Round ``x`` DOWN to a multiple of ``step`` (qty rule). Returns a plain decimal string. step must be > 0."""
    s = dec(step)
    if s <= 0:
        raise ValueError("step must be positive")
    q = (dec(x) / s).to_integral_value(rounding=ROUND_DOWN) * s
    return format(q.normalize(), "f") if q != 0 else "0"


def round_to_tick(x: Any, tick: Any, *, side: str, passive: bool) -> str:
    """Tick-round a limit price. A PASSIVE buy rounds DOWN and a PASSIVE sell rounds UP (never crosses by rounding); an aggressive order rounds the other way."""
    t = dec(tick)
    if t <= 0:
        raise ValueError("tick must be positive")
    n = dec(x) / t
    down = (side == "Buy") == bool(passive)
    q = n.to_integral_value(rounding=ROUND_DOWN if down else "ROUND_UP") * t
    return format(q.normalize(), "f")


def funding_bps_per_day(bps_per_print: Optional[float], interval_min: Optional[int]) -> Optional[float]:
    """Interval-normalised funding. None for unknown/unusable interval (never defaults to 480)."""
    if bps_per_print is None or interval_min is None or isinstance(interval_min, bool) or interval_min <= 0 or 1440 % interval_min:
        return None
    return bps_per_print * (1440.0 / interval_min)
