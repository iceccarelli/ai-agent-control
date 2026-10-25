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
#: HISTORY OF THIS NUMBER, and why it is NOT being changed here.
#:
#: It was set to 30 when the 0018 backtest (20 trades over 1,453 days at 94.8%
#: exposure) showed a realised mean hold of ~69 days: "well under" the measured
#: hold, because a gate that assumes a LONG hold to justify an entry is arguing
#: for its own conclusion. That derivation described the OLD backtest, not the
#: current engine.
#:
#: MEASURED AGAIN on the engine as it runs now (`tools/carry_hold_audit.py`, the
#: real CarryEngine replayed over data/real_bybit_btc_4h, OVERLAY/taker): 78
#: completed holds, median ~5.8 days, mean ~16.9, 54% shorter than a week, 15%
#: at least 30, nearly all ended by FUNDING_INVERTED. So 30 is NOT under the
#: measured hold any more: it is ~1.8x the mean and ~5x the median, and for the
#: typical trade the round trip is spread over a far longer period than it is
#: held. The same replay earned ~3x the funding the gate's smoothed rate
#: predicted at entry, so in aggregate that optimism is offset by a conservative
#: funding expectation, and 69% of completed trades still lost money after fees:
#: the profit sits in a minority of long holds that cannot be told apart at entry.
#:
#: The sensitivity is not academic. At a 7-day hold the round trip alone is
#: 4.43 bps/day against 1.80 bps/day of carry at MIN_ENTRY_FUNDING_BPS: the
#: trade cannot pay for its own entry. At 30 days it is 1.03. This single number
#: decides whether the strategy is a carry book or a fee generator, and any
#: change to it must be justified against a measured hold OUT OF SAMPLE and
#: decided by a human, never tuned until an entry passes. Re-run the audit
#: before touching it; `tests/test_carry_hold_audit.py` freezes the value so a
#: change cannot slip in as a side effect.
ASSUMED_HOLD_DAYS = 30.0

#: The hold the entry must be able to PAY FOR ITS ROUND TRIP WITHIN, in days.
#:
#: `ASSUMED_HOLD_DAYS` (30) amortises the round trip inside `evaluate_entry`,
#: but nothing in the engine ENFORCES a 30 day hold: the only exit is
#: FUNDING_INVERTED, so an entry whose funding barely clears the amortised cost
#: is still admitted although the position is, as measured, usually closed long
#: before the cost is earned back. The measured median hold is ~5.8 days
#: (`tools/carry_hold_audit.py`, quoted above and frozen in
#: `tests/test_carry_hold_audit.py`). `hold_clears_round_trip` below refuses an
#: entry whose smoothed funding needs MORE than this to earn back the round trip.
#:
#: It only ever REFUSES more than `evaluate_entry` does and replaces nothing. It
#: is a human-owned parameter like ASSUMED_HOLD_DAYS: not tuned until an entry
#: passes, and not raised without an out-of-sample hold measurement.
#:
#: WIRED: `entry_gate` below runs `evaluate_entry` and then this check, and it
#: is the one entry decision `CarryEngine.entry_economics` (so the replay and
#: the preflight), both `carry_backtest` settlement/daily simulators and the C++
#: core (`carry_entry_gate`) all make. `tests/test_entry_gate_parity.py` feeds
#: identical inputs to the runtime, the Python simulator and the C++ core and
#: requires identical accept/refuse and reason codes. The value is frozen by
#: `tests/test_carry_cost_reality.py`; it was not tuned against any result.
DEMONSTRATED_HOLD_DAYS = 5.8

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
    # A perp of 0 or below is no price. It used to be priced as a -100% basis,
    # which a large enough smoothed funding could still budget for: an
    # unreadable mark is refused, not priced (carrycore.cpp does the same).
    if (not (math.isfinite(perp) and math.isfinite(spot))
            or spot <= 0 or perp <= 0):
        raise ValueError(f"basis undefined for perp={perp!r} spot={spot!r}")
    return (perp / spot - 1.0) * 1e4


def _usable(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _invalid_cost_inputs(*, borrow_apr: Any, round_trip_bps: Any,
                         hold_days: Any) -> List[str]:
    """Names of the inputs the gate may not price. Empty means all usable."""
    bad: List[str] = []
    for name, value, minimum_exclusive in (
            ("round_trip_bps", round_trip_bps, True),
            ("hold_days", hold_days, True), ("borrow_apr", borrow_apr, False)):
        number = _usable(value)
        if number is None or (number <= 0 if minimum_exclusive else number < 0):
            bad.append(name)
    return bad


def evaluate_entry(*, funding_prints_bps: Sequence[float], perp: float,
                   spot: float, borrow_apr: float, round_trip_bps: float,
                   hold_days: float = ASSUMED_HOLD_DAYS) -> CarryVerdict:
    """May the book open this pair?

    Order matters. Cheapest and most decisive checks first, so a refusal names
    the binding constraint rather than the first thing that happened to fail.
    """
    # EVERY COST INPUT MUST BE A USABLE NUMBER, or the book does not open.
    # A NaN anywhere below turns every comparison false: `net_edge <= 0` and
    # `entry_basis > budget` both fail to refuse and the entry is ALLOWED. A
    # missing, zero or negative round trip reads as free execution, and a
    # negative borrow as income. None of those is a conservative bound, so each
    # is refused and named rather than priced.
    invalid = _invalid_cost_inputs(borrow_apr=borrow_apr,
                                   round_trip_bps=round_trip_bps,
                                   hold_days=hold_days)
    if invalid:
        return CarryVerdict(
            False, "COST_INPUT_INVALID",
            detail={"fields": invalid,
                    "note": "a cost that is missing, non-finite, zero where it "
                            "must be positive, or negative is not priced; the "
                            "gate refuses instead of assuming it is free"})

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
    except (ValueError, TypeError) as exc:
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

    # The same budget, the other way: a perp trading at a DISCOUNT converges
    # back to spot by costing the short, so an entry basis below minus the
    # budget gives back more than the hold will earn.
    if entry_basis < -basis_budget:
        return CarryVerdict(
            False, "ENTRY_BASIS_DISCOUNT_EXCEEDS_CARRY_BUDGET",
            expected_carry_bps_per_day=carry_per_day,
            borrow_bps_per_day=borrow_per_day,
            amortised_round_trip_bps_per_day=round_trip_per_day,
            net_edge_bps_per_day=net_edge, entry_basis_bps=entry_basis,
            basis_budget_bps=basis_budget, smoothed_funding_bps=smoothed,
            detail={"note": "the perp trades below spot by more than the "
                            "net carry over the hold; convergence would "
                            "cost the short more than funding pays."})

    return CarryVerdict(
        True, "CARRY_CLEARS_COSTS_AND_BASIS",
        expected_carry_bps_per_day=carry_per_day,
        borrow_bps_per_day=borrow_per_day,
        amortised_round_trip_bps_per_day=round_trip_per_day,
        net_edge_bps_per_day=net_edge, entry_basis_bps=entry_basis,
        basis_budget_bps=basis_budget, smoothed_funding_bps=smoothed)


# -- cost reality: what a hold has to earn back ---------------------------------

def _positive_finite(value: Any) -> Optional[float]:
    number = _usable(value)
    return number if number is not None and number > 0 else None


def breakeven_funding_bps_per_settlement(
        settlements_held: Any, *, round_trip_bps: Any,
        borrow_bps_per_day: Any = 0.0) -> float:
    """Smallest flat funding, in bps per 8h settlement, that earns back the
    costs after holding `settlements_held` settlements.

        break_even = round_trip_bps / N + borrow_bps_per_day / 3

    Only costs the caller states appear: the round trip (two perp legs for the
    overlay, 11 bps at 5.5 bps taker, no spot leg) and borrow (0 for a book that
    already owns the BTC). Basis is NOT a term. An overlay does not trade the
    spot; basis is a mark-to-market on the hedge that converges to zero, and for
    short perp / long spot it is `entry_basis - exit_basis` (see
    `hedge_basis_pnl_bps`). It is a risk to net, never edge to credit.

    Funding is treated as flat and positive. It is variable and can flip sign,
    which only makes the real break-even longer.

    Raises ValueError for N <= 0, a non-positive round trip or a negative
    borrow, so a caller cannot price a free or zero-length hold.
    """
    n = _positive_finite(settlements_held)
    rt = _positive_finite(round_trip_bps)
    borrow = _usable(borrow_bps_per_day)
    if n is None or rt is None or borrow is None or borrow < 0:
        raise ValueError(
            f"break-even undefined for settlements_held={settlements_held!r} "
            f"round_trip_bps={round_trip_bps!r} "
            f"borrow_bps_per_day={borrow_bps_per_day!r}")
    return rt / n + borrow / FUNDING_PERIODS_PER_DAY


def min_settlements_to_clear(expected_funding_bps: Any, *,
                             round_trip_bps: Any,
                             borrow_bps_per_day: Any = 0.0) -> Optional[int]:
    """Fewest whole 8h settlements that must be held for flat
    `expected_funding_bps` per settlement to earn back the round trip.

    None means NEVER: funding at or below borrow (or not a usable number)
    leaves nothing to amortise against, and None must be read as a refusal.
    """
    rt = _positive_finite(round_trip_bps)
    borrow = _usable(borrow_bps_per_day)
    funding = _usable(expected_funding_bps)
    if rt is None or borrow is None or borrow < 0:
        raise ValueError(
            f"break-even undefined for round_trip_bps={round_trip_bps!r} "
            f"borrow_bps_per_day={borrow_bps_per_day!r}")
    if funding is None:
        return None
    net = round(funding - borrow / FUNDING_PERIODS_PER_DAY, 12)
    if net <= 0:
        return None
    # round() first so 11 / 1.0 is 11, not 11.000000000000002 -> 12
    return max(1, math.ceil(round(rt / net, 9)))


def hedge_basis_pnl_bps(entry_basis_bps: float, exit_basis_bps: float) -> float:
    """PnL in bps of a short-perp / long-spot hedge from the basis moving:
    `entry_basis - exit_basis`, basis being (perp - spot) / spot.

    Short perp earns (entry_perp - exit_perp), long spot earns
    (exit_spot - entry_spot); the sum is the basis it SHEDS. At the observed
    -40 bps (perp BELOW spot) converging to 0 the hedge LOSES 40 bps: the perp
    rises toward spot and the short pays for it. That is the same sign
    `evaluate_entry` uses for ENTRY_BASIS_DISCOUNT_EXCEEDS_CARRY_BUDGET. It is
    never counted as edge here.
    """
    return float(entry_basis_bps) - float(exit_basis_bps)


def hold_clears_round_trip(*, funding_prints_bps: Sequence[float],
                           round_trip_bps: float, borrow_apr: float,
                           hold_days: float = DEMONSTRATED_HOLD_DAYS
                           ) -> CarryVerdict:
    """May this entry open, judged on the hold that has been DEMONSTRATED?

    `evaluate_entry` amortises over ASSUMED_HOLD_DAYS. This asks the harder,
    unflattering question: does the smoothed funding earn back the round trip
    (plus borrow) within `hold_days`, by default the measured median hold?
    Fail-closed on every unusable input, on too little history, and on funding
    that never clears. Allowed means only "not refused here".
    """
    invalid = _invalid_cost_inputs(borrow_apr=borrow_apr,
                                   round_trip_bps=round_trip_bps,
                                   hold_days=hold_days)
    if invalid:
        return CarryVerdict(False, "COST_INPUT_INVALID",
                            detail={"fields": invalid})
    smoothed = ewma_funding_bps(funding_prints_bps)
    if smoothed is None:
        return CarryVerdict(False, "INSUFFICIENT_FUNDING_HISTORY",
                            detail={"required": EWMA_MIN_PRINTS})
    borrow_per_day = (float(borrow_apr) / 365.0) * 1e4
    budget = int(math.floor(round(float(hold_days) * FUNDING_PERIODS_PER_DAY, 9)))
    needed = min_settlements_to_clear(
        smoothed, round_trip_bps=float(round_trip_bps),
        borrow_bps_per_day=borrow_per_day)
    detail = {"settlements_needed": needed, "settlements_budget": budget,
              "hold_days": float(hold_days),
              "break_even_funding_bps_at_budget": (
                  breakeven_funding_bps_per_settlement(
                      max(budget, 1), round_trip_bps=float(round_trip_bps),
                      borrow_bps_per_day=borrow_per_day)),
              "note": "the funding does not earn back the round trip inside "
                      "the hold that has actually been measured; ASSUMED_HOLD_"
                      "DAYS is not enforced by anything"}
    if needed is None or needed > budget:
        return CarryVerdict(False, "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD",
                            smoothed_funding_bps=smoothed, detail=detail)
    return CarryVerdict(True, "HOLD_CLEARS_ROUND_TRIP",
                        smoothed_funding_bps=smoothed, detail=detail)


# -- the entry gate: what the engine, the simulators and the C++ core all ask ---

#: Reason codes, in the order carrycore.cpp numbers them (index = C++ code).
#: `tests/test_entry_gate_parity.py` pins this list against the native library.
GATE_REASONS = (
    "CARRY_CLEARS_COSTS_AND_BASIS",              # 0  allowed
    "COST_INPUT_INVALID",                        # 1
    "INSUFFICIENT_FUNDING_HISTORY",              # 2
    "FUNDING_NOT_POSITIVE",                      # 3
    "BASIS_UNREADABLE",                          # 4
    "CARRY_BELOW_COST_OF_CAPITAL",               # 5
    "ENTRY_BASIS_EXCEEDS_CARRY_BUDGET",          # 6
    "ENTRY_BASIS_DISCOUNT_EXCEEDS_CARRY_BUDGET",  # 7
    "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD",  # 8
)


def entry_gate(*, funding_prints_bps: Sequence[float], perp: float,
               spot: float, borrow_apr: float, round_trip_bps: float,
               hold_days: float = ASSUMED_HOLD_DAYS,
               demonstrated_hold_days: float = DEMONSTRATED_HOLD_DAYS
               ) -> CarryVerdict:
    """May the book open this pair? `evaluate_entry`, then the hold check.

    A pure refinement: every refusal `evaluate_entry` gives is returned
    unchanged, and only an entry it ALLOWS can be refused here, with
    BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD (or COST_INPUT_INVALID when the
    demonstrated hold is not a positive number). Nothing is ever allowed that
    `evaluate_entry` refused, exits are untouched, and no parameter moves.

    A refused entry carries `evaluate_entry`'s arithmetic (smoothed funding,
    basis, net edge) so callers log the same fields whichever gate refused.
    """
    verdict = evaluate_entry(
        funding_prints_bps=funding_prints_bps, perp=perp, spot=spot,
        borrow_apr=borrow_apr, round_trip_bps=round_trip_bps,
        hold_days=hold_days)
    if not verdict.allowed:
        return verdict
    horizon = hold_clears_round_trip(
        funding_prints_bps=funding_prints_bps, round_trip_bps=round_trip_bps,
        borrow_apr=borrow_apr, hold_days=demonstrated_hold_days)
    if horizon.allowed:
        return verdict
    from dataclasses import replace
    return replace(verdict, allowed=False, reason=horizon.reason,
                   detail=dict(horizon.detail))
