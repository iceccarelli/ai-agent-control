"""Whether a carry pair is worth opening. Three gates, no opinions.

WHY THIS EXISTS
===============
0018 charged the book for existing and the answer was uncomfortable:

    funding +$42,843 · basis -$4,772 · fees -$6,426 · borrow -$24,624
    NET +$7,020 = +1.77%/yr at 5% financing, +7.96% unfinanced
    15 of 20 trades lost money

`CarryEngine` opens on one condition: the last funding print cleared
MIN_ENTRY_FUNDING_BPS. That is not a decision, it is a reflex. It ignores what
the money costs, what the basis will take back, and whether the rate that just
printed is representative of the next eight hours.

Three gates, each answering a question the engine currently does not ask.

GATE 1 — BASIS
==============
Measured over the backtest window: mean entry basis +6 bps, mean exit +29 bps.
The book was systematically SHORTING THE PERP INTO A NARROW BASIS AND CLOSING
INTO A WIDE ONE, which is the losing direction. Short perp plus long spot earns
the basis it SHEDS between entry and exit.

This is not bad luck, it is structural: funding is rich precisely when the perp
trades above spot, so the signal that says "open" and the condition that says
"the basis will cost you" are the same condition. Nothing in the engine
separates them.

The gate refuses when entry basis exceeds the carry you expect to collect over
the hold. Paying 30 bps of basis to collect 9 bps of funding is a decision
nobody would make explicitly, so it must not be reachable implicitly.

GATE 2 — COST OF CAPITAL
========================
Borrow was 57% of gross income. The spot leg is financed and the engine has
never known that. A book that opens on funding alone is comparing its revenue
to zero.

The gate refuses when expected carry does not clear borrow plus the amortised
round trip. At 5% APR that is ~1.4 bps/day of financing against ~1.9 bps/day of
carry at the threshold rate — the trade is marginal at the threshold and the
threshold was chosen before anyone computed this.

GATE 3 — EWMA, NOT THE LAST PRINT
=================================
The engine opens on the LAST funding print. One print is 8 hours of a rate that
mean-reverts. A three-print EWMA is one day, weights the most recent print
highest, and stops the book opening into a rate that has already turned.

WHAT THIS MODULE DOES NOT DO
============================
It does not place orders, hold state, or know what a venue is. It takes numbers
and returns a verdict. `CarryEngine` may consult it; the gates are pure and
independently testable, which is the only reason a risk rule can be trusted.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

#: Taker fees, declared here so the amortisation is visible rather than buried.
#: These are the ACQUIRE-mode figures (buy spot, short perp, reverse both) and
#: they are what the 0018-0032 backtests charged.
TAKER_BPS_SPOT = 10.0
TAKER_BPS_PERP = 5.5
ROUND_TRIP_BPS = 2 * TAKER_BPS_SPOT + 2 * TAKER_BPS_PERP   # 31.0

#: 0036: `evaluate_entry` takes the round trip as a REQUIRED argument instead
#: of reading the constant above. An overlay on BTC the client already owns
#: never trades the spot leg, so its round trip is two perp legs — 11 bps at
#: taker, and less with a maker fill. Charging it 31 refuses entries that pay
#: for themselves three times over; charging a spot-buying book 11 would let
#: through entries that cannot. The number belongs to the CALLER's execution
#: mode and fee tier, and the live engine reads it from the venue.

#: Financing on the spot leg. 0.0 models a book that ALREADY OWNS the BTC and is
#: monetising it. The difference between those two worlds is the difference
#: between a business and a hobby, so it is a REQUIRED argument at every call
#: site. Until 0033 this comment said so and the signature defaulted it to
#: 0.05 anyway (INVENTORY D7); the constant is gone so nothing can reach for it.

#: Funding prints per day on Binance USDT-M and Bybit linear.
FUNDING_PERIODS_PER_DAY = 3

#: How long a pair is expected to be held, in days. Amortises the one-time round
#: trip and sizes the basis the carry must cover.
#:
#: DERIVED, not chosen. The 0018 backtest ran 20 trades across 1,453 days at
#: 94.8% exposure, so the realised mean hold was ~69 days. This is set to 30 —
#: well under that — because a gate that assumes a LONG hold to justify an entry
#: is a gate arguing for its own conclusion, and because a position exited early
#: on inverted funding pays the full round trip over fewer days.
#:
#: The sensitivity is not academic. At a 7-day hold the round trip alone is
#: 4.43 bps/day against 1.80 bps/day of carry at MIN_ENTRY_FUNDING_BPS: the
#: trade cannot pay for its own entry. At 30 days it is 1.03. This single number
#: decides whether the strategy is a carry book or a fee generator, and any
#: change to it must be justified against a measured hold, never tuned until an
#: entry passes.
ASSUMED_HOLD_DAYS = 30.0

#: EWMA smoothing over funding prints. 0.5 puts half the weight on the newest
#: print, a quarter on the one before, and so on: responsive to a turn without
#: opening on a single spike.
EWMA_ALPHA = 0.5
EWMA_MIN_PRINTS = 3


@dataclass(frozen=True)
class CarryVerdict:
    """Open or refuse, and the arithmetic behind it."""

    allowed: bool
    reason: str
    expected_carry_bps_per_day: float = 0.0
    borrow_bps_per_day: float = 0.0
    amortised_round_trip_bps_per_day: float = 0.0
    net_edge_bps_per_day: float = 0.0
    entry_basis_bps: float = 0.0
    basis_budget_bps: float = 0.0
    smoothed_funding_bps: float = 0.0
    detail: Dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.allowed


def ewma_funding_bps(prints_bps: Sequence[float], *,
                     alpha: float = EWMA_ALPHA) -> Optional[float]:
    """Smoothed funding in bps per 8h print, newest last.

    Returns None below EWMA_MIN_PRINTS. None means REFUSE at the call site, not
    "assume zero" and not "fall back to the last print": a book with too little
    history to know the rate should not open on it.
    """
    values = [float(p) for p in prints_bps if p is not None and math.isfinite(p)]
    if len(values) < EWMA_MIN_PRINTS:
        return None
    smoothed = values[0]
    for value in values[1:]:
        smoothed = alpha * value + (1.0 - alpha) * smoothed
    return smoothed


def basis_bps(perp: float, spot: float) -> float:
    """(perp - spot) / spot in bps. Positive means the perp trades RICH, which
    is both why funding is positive and why the basis will cost you."""
    if not (math.isfinite(perp) and math.isfinite(spot)) or spot <= 0:
        raise ValueError(f"basis undefined for perp={perp!r} spot={spot!r}")
    return (perp / spot - 1.0) * 1e4


def evaluate_entry(*, funding_prints_bps: Sequence[float], perp: float,
                   spot: float, borrow_apr: float, round_trip_bps: float,
                   hold_days: float = ASSUMED_HOLD_DAYS) -> CarryVerdict:
    """May the book open this pair?

    Order matters. Cheapest and most decisive checks first, so a refusal names
    the binding constraint rather than the first thing that happened to fail.
    """
    smoothed = ewma_funding_bps(funding_prints_bps)
    if smoothed is None:
        return CarryVerdict(
            False, "INSUFFICIENT_FUNDING_HISTORY",
            detail={"prints": len(list(funding_prints_bps)),
                    "required": EWMA_MIN_PRINTS,
                    "note": "too little history to know the rate; not an "
                            "invitation to assume zero or use the last print"})

    if smoothed <= 0:
        return CarryVerdict(False, "FUNDING_NOT_POSITIVE",
                            smoothed_funding_bps=smoothed)

    carry_per_day = smoothed * FUNDING_PERIODS_PER_DAY
    borrow_per_day = (borrow_apr / 365.0) * 1e4
    round_trip_per_day = float(round_trip_bps) / max(hold_days, 1e-9)
    net_edge = carry_per_day - borrow_per_day - round_trip_per_day

    try:
        entry_basis = basis_bps(perp, spot)
    except ValueError as exc:
        return CarryVerdict(False, "BASIS_UNREADABLE",
                            smoothed_funding_bps=smoothed,
                            detail={"error": str(exc)})

    # GATE 2 — does the carry clear what the money costs?
    if net_edge <= 0:
        return CarryVerdict(
            False, "CARRY_BELOW_COST_OF_CAPITAL",
            expected_carry_bps_per_day=carry_per_day,
            borrow_bps_per_day=borrow_per_day,
            amortised_round_trip_bps_per_day=round_trip_per_day,
            net_edge_bps_per_day=net_edge, entry_basis_bps=entry_basis,
            smoothed_funding_bps=smoothed,
            detail={"note": "borrow was 57% of gross income in the 0018 "
                            "backtest; a book that opens on funding alone is "
                            "comparing revenue to zero"})

    # GATE 1 — will the basis take back more than the hold will earn?
    # Budget is the NET edge over the hold, not the gross carry: paying basis
    # out of money that is already spoken for by borrow and fees is how a
    # positive-carry trade closes negative.
    basis_budget = net_edge * hold_days
    if entry_basis > basis_budget:
        return CarryVerdict(
            False, "ENTRY_BASIS_EXCEEDS_CARRY_BUDGET",
            expected_carry_bps_per_day=carry_per_day,
            borrow_bps_per_day=borrow_per_day,
            amortised_round_trip_bps_per_day=round_trip_per_day,
            net_edge_bps_per_day=net_edge, entry_basis_bps=entry_basis,
            basis_budget_bps=basis_budget, smoothed_funding_bps=smoothed,
            detail={"note": "short perp + long spot earns the basis it SHEDS. "
                            "Opening into a wide basis is paying up front for "
                            "carry you have not collected yet."})

    return CarryVerdict(
        True, "CARRY_CLEARS_COSTS_AND_BASIS",
        expected_carry_bps_per_day=carry_per_day,
        borrow_bps_per_day=borrow_per_day,
        amortised_round_trip_bps_per_day=round_trip_per_day,
        net_edge_bps_per_day=net_edge, entry_basis_bps=entry_basis,
        basis_budget_bps=basis_budget, smoothed_funding_bps=smoothed)
