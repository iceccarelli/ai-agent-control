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

(Reading it precisely: the premium branch is a SYMMETRIC bound on |basis|, not
a sign-specific cost. A perp at a premium converges back to spot in the short's
favour; a perp at a discount converges against it. The rule bounds how far the
basis may sit from zero relative to the carry, in both directions, and on the
public history it has never bound: no print exceeds +24 bps against a smallest
reachable budget of 47 bps, so a replay with it removed is bit-identical.)

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

# ---------------------------------------------------------------------------
# THE ONE AUTHORITATIVE COST VOCABULARY (units: bps of hedged notional, per
# ROUND TRIP = entry + exit; never per leg, per settlement or per day).
#
# Four different "round trips" circulated in this tree as bare numbers (11.0,
# 11.4, 31.0, 4.0). They are DIFFERENT QUANTITIES, not rival estimates:
#
#   ROUND_TRIP_FEE_BPS_PERP_OVERLAY          11.0  2 perp taker legs. FEE ONLY.
#                                                  What the entry gate prices
#                                                  (carry_core overlay,
#                                                  mainnet_public.GATE_ROUND_TRIP_BPS,
#                                                  CarryEngine.round_trip_bps()).
#   ROUND_TRIP_FEE_BPS_SPOT_PERP             31.0  2 spot + 2 perp taker legs
#                                                  (acquire mode) == ROUND_TRIP_BPS.
#   ASSUMED_ROUND_TRIP_SPREAD_BPS             0.4  spread/slippage allowance.
#   ROUND_TRIP_TOTAL_COST_BPS_PERP_OVERLAY   11.4  fee + that allowance. What the
#                                                  teardown, the profitability
#                                                  report and the FROZEN
#                                                  prospective plan (1.131
#                                                  bps/settlement hurdle) price.
#
# The 0.4 is ASSUMED. Nothing in the repository measured it: the first place
# 11.4 appears (tools/carry_teardown.py, ee9dd07) states it as "two perpetual
# taker legs, plus spread and slippage" with no derivation, and the engine's
# own note (carry_engine.py, EXECUTION_STYLES) measures the BTCUSDT touch at
# ~0.013 bps, so 0.4 is a deliberately conservative allowance, not an
# observation. It has no measurement behind it yet.
#
# The gate (11.0) and the hurdle (11.4) therefore differ ON PURPOSE: the gate is
# fee-only and the hurdle also carries the spread allowance. Neither may be
# edited without the other being re-read. tests/test_cost_contracts.py pins
# every relationship, and that CP.ROUND_TRIP_BPS (frozen) equals the total.
#
# Provenance of each (economic_model vocabulary, strongest -> weakest):
#   taker fees: venue_stated (Bybit's published base tier; the owner's account
#   tier is NOT verified); spread allowance: assumed; total: assumed (a derived
#   sum that contains an assumed term is as weak as that term).
# ---------------------------------------------------------------------------
ROUND_TRIP_FEE_BPS_PERP_OVERLAY = 2 * TAKER_BPS_PERP                      # 11.0
ROUND_TRIP_FEE_BPS_SPOT_PERP = 2 * (TAKER_BPS_SPOT + TAKER_BPS_PERP)      # 31.0
ASSUMED_ROUND_TRIP_SPREAD_BPS = 0.4
#: rounded only to strip float dust; 11.0 + 0.4 == 11.4 exactly in IEEE-754.
ROUND_TRIP_TOTAL_COST_BPS_PERP_OVERLAY = round(
    ROUND_TRIP_FEE_BPS_PERP_OVERLAY + ASSUMED_ROUND_TRIP_SPREAD_BPS, 10)  # 11.4

#: Maker fee per perp leg. A MODELLED SCENARIO input, not an observation: it is
#: config.py's linear default, the account tier is unverified, and no fill
#: probability has ever been measured (carry_engine.py MAKER_FIRST note,
#: tools/execution_economics.py). 4.0 is the best case "every leg rests and fills".
MAKER_BPS_PERP_MODELLED = 2.0
ROUND_TRIP_FEE_BPS_PERP_OVERLAY_MAKER_MODELLED = 2 * MAKER_BPS_PERP_MODELLED   # 4.0

# ---------------------------------------------------------------------------
# FUNDING INTERVAL CONVERSIONS (the ONE place a per-print rate becomes a per-day
# or per-horizon number). Funding intervals are PER SYMBOL (Bybit fundingInterval
# is stated in MINUTES: 480 / 240 / 60 ...) and the venue scales the base
# interest component by interval, so the SAME economic claim reads 1.00 bps per
# print at 480m, 0.50 at 240m and 0.125 at 60m: 3.00 bps/day each. Never assume
# 8h; an interval that is missing, <= 0, boolean, non-finite or does not divide
# 1440 is UNKNOWN (None), never defaulted.
# ---------------------------------------------------------------------------
MINUTES_PER_DAY = 1440


def interval_minutes_or_none(interval_minutes: Any) -> Optional[int]:
    """The funding interval in whole minutes if it is a positive divisor of 1440, else None (UNKNOWN)."""
    if isinstance(interval_minutes, bool) or not isinstance(interval_minutes, (int, float)):
        return None
    if not math.isfinite(interval_minutes) or interval_minutes <= 0 or interval_minutes != int(interval_minutes):
        return None
    m = int(interval_minutes)
    return m if MINUTES_PER_DAY % m == 0 else None


def settlements_per_day(interval_minutes: Any) -> Optional[float]:
    """1440 / interval_minutes; None (UNKNOWN) for an unusable interval."""
    m = interval_minutes_or_none(interval_minutes)
    return None if m is None else MINUTES_PER_DAY / m


def funding_bps_per_day(bps_per_print: Any, interval_minutes: Any) -> Optional[float]:
    """bps/day = bps_per_print * 1440 / interval_minutes. None if either input is unusable."""
    n = settlements_per_day(interval_minutes)
    r = None if isinstance(bps_per_print, bool) else _usable(bps_per_print)
    return None if n is None or r is None else r * n


def funding_bps_over_horizon(bps_per_print: Any, interval_minutes: Any, hold_days: Any) -> Optional[float]:
    """Funding collected over ``hold_days`` (bps of notional): bps_per_day * hold_days."""
    d, h = funding_bps_per_day(bps_per_print, interval_minutes), _positive_finite(hold_days)
    return None if d is None or h is None else d * h


def equivalent_hurdle_bps_per_print(hurdle_bps_per_print: Any, from_interval_minutes: Any, to_interval_minutes: Any) -> Optional[float]:
    """A per-print hurdle stated at one interval, restated at another with the same bps/day.
    The FROZEN 8h hurdle (1.131 bps/print at 480m) is HALF that per print at 240m. None if unusable."""
    day = funding_bps_per_day(hurdle_bps_per_print, from_interval_minutes)
    n = settlements_per_day(to_interval_minutes)
    return None if day is None or n is None else day / n


# ---------------------------------------------------------------------------
# WHICH COST APPLIES TO WHICH POSITION (hedged vs unhedged). Costs are per ROUND
# TRIP (entry + exit), charged ONCE per trade whatever the funding interval.
#
#   overlay_owned_spot          short perp vs BTC already owned: 2 perp legs, no spot
#                               trade. HEDGED. 11.0 fee. Holding the BTC has an
#                               opportunity cost that is NOT in this number.
#   acquire_spot_perp           buy spot + short perp: 2 spot + 2 perp legs. HEDGED. 31.0.
#   perp_only_hedged_elsewhere  a perp with NO Bybit spot pair, hedged by some other
#                               instrument. The 11.0 is the PERP leg only; the hedge
#                               instrument's own cost is UNKNOWN here, never zero.
#   perp_only_unhedged          one perp leg in and out: 5.5 + 5.5 = 11.0 as well, but
#                               there is NO hedge. The price leg is open directional
#                               risk, so funding is not "carry" and 11.0 understates
#                               the cost of being wrong. Not delta-neutral.
#
# The fee is identical (11.0) for three of the four; the CLASS is what differs,
# so a net quoted without its class is not comparable.
# ---------------------------------------------------------------------------
POSITION_CLASSES = ("overlay_owned_spot", "acquire_spot_perp", "perp_only_hedged_elsewhere", "perp_only_unhedged")


def position_class_costs(position_class: str, style: str = "taker") -> Dict[str, Any]:
    """Round-trip cost (bps of notional) for a position class, with provenance and whether a hedge exists. ``style`` 'taker' | 'maker'.
    maker = every perp leg rests and fills at MAKER_BPS_PERP_MODELLED with zero adverse selection and zero missed fills: MODELLED, an upper
    bound on savings, never an observation; no maker figure exists for the spot legs here (None)."""
    if position_class not in POSITION_CLASSES:
        raise ValueError(f"unknown position class {position_class!r}")
    if style not in ("taker", "maker"):
        raise ValueError(f"unknown style {style!r}")
    acquire = position_class == "acquire_spot_perp"
    hedged = position_class != "perp_only_unhedged"
    if style == "taker":
        fee = ROUND_TRIP_FEE_BPS_SPOT_PERP if acquire else ROUND_TRIP_FEE_BPS_PERP_OVERLAY
        spread, fee_prov = ASSUMED_ROUND_TRIP_SPREAD_BPS, "venue_stated"
    else:
        if acquire:
            return {"position_class": position_class, "style": style, "fee_bps": None, "spread_bps": None, "total_bps": None,
                    "provenance": "unknown", "hedged": hedged, "hedge_leg_cost_included": True, "note": "no maker figure for the spot legs"}
        fee, spread, fee_prov = ROUND_TRIP_FEE_BPS_PERP_OVERLAY_MAKER_MODELLED, 0.0, "modelled"
    return {"position_class": position_class, "style": style, "fee_bps": fee, "spread_bps": spread,
            "total_bps": round(fee + spread, 10), "fee_provenance": fee_prov,
            "provenance": "modelled" if style == "maker" else "assumed",   # taker total carries the ASSUMED 0.4; maker is a MODELLED scenario
            "hedged": hedged,
            "hedge_leg_cost_included": position_class in ("overlay_owned_spot", "acquire_spot_perp"),
            "open_directional_risk": not hedged,
            "note": {"perp_only_hedged_elsewhere": "perp leg only; the hedge instrument's cost is UNKNOWN, not zero",
                     "perp_only_unhedged": "no hedge: the price leg is open risk, funding is not carry"}.get(position_class, "")}


def annualise_bps_from_window(net_bps: Any, window_days: Any) -> Optional[Dict[str, Any]]:
    """net_bps * 365 / window_days as a LABELLED EXTRAPOLATION. It assumes the window repeats for a year, which nothing measured, so the result
    carries ``extrapolated_from_window`` True, provenance 'assumed' (an assumed input can never yield a verdict PASS) and ``may_feed_verdict``
    False. Use it for display only. None for an unusable net or window."""
    net, days = _usable(net_bps), _positive_finite(window_days)
    if net is None or days is None or isinstance(net_bps, bool):
        return None
    return {"value_bps_per_year": net * 365.0 / days, "extrapolated_from_window": True, "window_days": days,
            "provenance": "assumed", "may_feed_verdict": False,
            "label": f"EXTRAPOLATION of a {days:g}-day window x {365.0 / days:.3f}; not an observed annual figure"}


COST_PROVENANCE = {
    "TAKER_BPS_PERP": "venue_stated", "TAKER_BPS_SPOT": "venue_stated",
    "ROUND_TRIP_FEE_BPS_PERP_OVERLAY": "venue_stated",
    "ROUND_TRIP_FEE_BPS_SPOT_PERP": "venue_stated",
    "ASSUMED_ROUND_TRIP_SPREAD_BPS": "assumed",
    "ROUND_TRIP_TOTAL_COST_BPS_PERP_OVERLAY": "assumed",
    "MAKER_BPS_PERP_MODELLED": "modelled",
    "ROUND_TRIP_FEE_BPS_PERP_OVERLAY_MAKER_MODELLED": "modelled",
}

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

#: OPPORTUNITY COST OF CAPITAL (an explicit input, default 0.0).
#:
#: `borrow_apr` = 0.0 models a book that already owns the BTC: nothing is
#: financed. That is NOT a free position. Hedged BTC earns the carry and
#: nothing else, so the capital forgoes whatever the owner's next-best use of
#: it would return. `opportunity_cost_apr` states that return as a per-year
#: cost on the hedged notional, in the same units and with the same treatment
#: as `borrow_apr` (bps/day = apr / 365 * 1e4, charged per print by the
#: simulators). It is SEPARATE from borrow so a report can say which is which.
#:
#: There is no risk-free-rate source in this repository and none is invented:
#: the default is 0.0, which reproduces every earlier decision bit for bit, and
#: a value is the OPERATOR's statement, never a constant reached for here. A
#: missing, non-finite or negative value is refused (COST_INPUT_INVALID), as a
#: borrow rate is: a negative cost would be income nobody measured.
DEFAULT_OPPORTUNITY_COST_APR = 0.0

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
#: PROVENANCE OF THE FIGURES ABOVE (re-measured 2026-11-06, in-sample; the
#: constant is deliberately NOT changed here). They describe the engine with the
#: hold test UNWIRED: 76-77 entries, median 5.83 d, mean 17.2 d, 68% losing, net
#: $29,889 (+7.28%/yr on $100k). The hold test then WIRED (this constant, 5.8 d)
#: refuses 28 of those entries, and the same audit at HEAD reads 49 entries,
#: median 9.67 d, mean 23.9 d, 52% losing, net $33,050 (+8.05%/yr). So 5.8 is
#: population-CONDITIONAL: 76 of the 77 unwired entries would fail today's test
#: at their own entry print (median smoothed funding 0.28 bps against the 0.647
#: threshold), i.e. the "demonstrated" median comes from trades almost entirely
#: outside the population that now consumes it, and re-measuring it on the gated
#: population gives a different number (9.67), which is a different gate. There
#: is no fixed point (feeding each run's median back oscillates between 5.8 and
#: ~9). The constant is the STRICTER of the two, so the stale provenance makes
#: the gate more conservative than documented, not looser; raising it to the new
#: median would admit ~65 entries instead of 49 and is a human decision that must
#: not be tuned in-sample. Note also that the three horizons in one decision are
#: not the same: `evaluate_entry` amortises over ASSUMED_HOLD_DAYS (30 d) and
#: bounds basis by net_edge x 30 d, while the break-even below uses this 5.8 d;
#: on the funding level the gate is pessimistic, on the basis budget it is
#: optimistic.
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
    #: The opportunity cost of the hedged capital, bps/day (0.0 unless the
    #: caller stated one). Charged in `net_edge_bps_per_day` beside borrow.
    opportunity_cost_bps_per_day: float = 0.0

    def __bool__(self) -> bool:
        return self.allowed


def ewma_funding_bps(prints_bps: Sequence[float], *,
                     alpha: float = EWMA_ALPHA,
                     min_prints: int = EWMA_MIN_PRINTS) -> Optional[float]:
    """Smoothed funding in bps per 8h print, newest last.

    Returns None below EWMA_MIN_PRINTS. None means REFUSE at the call site, not
    "assume zero" and not "fall back to the last print": a book with too little
    history to know the rate should not open on it.
    """
    values = [float(p) for p in prints_bps if p is not None and math.isfinite(p)]
    if len(values) < min_prints or not values:
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
                         hold_days: Any,
                         opportunity_cost_apr: Any = DEFAULT_OPPORTUNITY_COST_APR
                         ) -> List[str]:
    """Names of the inputs the gate may not price. Empty means all usable."""
    bad: List[str] = []
    for name, value, minimum_exclusive in (
            ("round_trip_bps", round_trip_bps, True),
            ("hold_days", hold_days, True), ("borrow_apr", borrow_apr, False),
            ("opportunity_cost_apr", opportunity_cost_apr, False)):
        number = _usable(value)
        if number is None or (number <= 0 if minimum_exclusive else number < 0):
            bad.append(name)
    return bad


def evaluate_entry(*, funding_prints_bps: Sequence[float], perp: float,
                   spot: float, borrow_apr: float, round_trip_bps: float,
                   hold_days: float = ASSUMED_HOLD_DAYS,
                   ewma_alpha: float = EWMA_ALPHA,
                   ewma_min_prints: int = EWMA_MIN_PRINTS,
                   opportunity_cost_apr: float = DEFAULT_OPPORTUNITY_COST_APR
                   ) -> CarryVerdict:
    """May the book open this pair?

    `ewma_alpha` and `ewma_min_prints` default to the production constants;
    they exist so the simulators can state the values they ran with.
    `opportunity_cost_apr` (default 0.0) is charged beside borrow; see
    DEFAULT_OPPORTUNITY_COST_APR.

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
                                   hold_days=hold_days,
                                   opportunity_cost_apr=opportunity_cost_apr)
    if invalid:
        return CarryVerdict(
            False, "COST_INPUT_INVALID",
            detail={"fields": invalid,
                    "note": "a cost that is missing, non-finite, zero where it "
                            "must be positive, or negative is not priced; the "
                            "gate refuses instead of assuming it is free"})

    smoothed = ewma_funding_bps(funding_prints_bps, alpha=ewma_alpha,
                                min_prints=ewma_min_prints)
    if smoothed is None:
        return CarryVerdict(
            False, "INSUFFICIENT_FUNDING_HISTORY",
            detail={"prints": len(list(funding_prints_bps)),
                    "required": ewma_min_prints,
                    "note": "too little history to know the rate; not an "
                            "invitation to assume zero or use the last print"})

    if smoothed <= 0:
        return CarryVerdict(False, "FUNDING_NOT_POSITIVE",
                            smoothed_funding_bps=smoothed)

    carry_per_day = smoothed * FUNDING_PERIODS_PER_DAY
    borrow_per_day = (borrow_apr / 365.0) * 1e4
    opportunity_per_day = (float(opportunity_cost_apr) / 365.0) * 1e4
    round_trip_per_day = float(round_trip_bps) / max(hold_days, 1e-9)
    # Subtracted as its own term (carrycore.cpp in the same order): at the
    # default 0.0 this is `carry - borrow - 0.0 - round_trip`, which is the
    # pre-existing expression to the bit.
    net_edge = (carry_per_day - borrow_per_day - opportunity_per_day
                - round_trip_per_day)

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
            opportunity_cost_bps_per_day=opportunity_per_day,
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
    # AN EDGE THAT OVERFLOWED IS NOT AN EDGE. carry = inf and borrow = inf make
    # `net_edge` NaN, which fails `net_edge <= 0` and every comparison below and
    # so ALLOWED the entry; carry = inf alone makes the budget infinite and
    # admits any basis. Neither is a number anyone measured, so the gate refuses
    # and names it (carrycore.cpp mirrors this point;
    # tests/test_gate_arithmetic_overflow.py pins both).
    if not math.isfinite(basis_budget):
        return CarryVerdict(
            False, "COST_INPUT_INVALID", smoothed_funding_bps=smoothed,
            entry_basis_bps=entry_basis,
            detail={"fields": ["arithmetic_overflow"],
                    "note": "the carry, borrow or basis budget is not a finite "
                            "number; an overflowed edge is refused, never "
                            "compared"})
    if entry_basis > basis_budget:
        return CarryVerdict(
            False, "ENTRY_BASIS_EXCEEDS_CARRY_BUDGET",
            expected_carry_bps_per_day=carry_per_day,
            borrow_bps_per_day=borrow_per_day,
            opportunity_cost_bps_per_day=opportunity_per_day,
            amortised_round_trip_bps_per_day=round_trip_per_day,
            net_edge_bps_per_day=net_edge, entry_basis_bps=entry_basis,
            basis_budget_bps=basis_budget, smoothed_funding_bps=smoothed,
            detail={"note": "a symmetric basis-risk bound, not a sign-specific "
                            "cost: an entry basis further from zero than the "
                            "carry expected over the hold can cover is refused. "
                            "(A premium converges in the short's favour, "
                            "hedge_basis_pnl_bps > 0 on convergence; the bound "
                            "is kept because the premium can also widen before "
                            "it converges, and it is mirrored in the C++ core.)"})

    # The same budget, the other way: a perp trading at a DISCOUNT converges
    # back to spot by costing the short, so an entry basis below minus the
    # budget gives back more than the hold will earn.
    if entry_basis < -basis_budget:
        return CarryVerdict(
            False, "ENTRY_BASIS_DISCOUNT_EXCEEDS_CARRY_BUDGET",
            expected_carry_bps_per_day=carry_per_day,
            borrow_bps_per_day=borrow_per_day,
            opportunity_cost_bps_per_day=opportunity_per_day,
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
        opportunity_cost_bps_per_day=opportunity_per_day,
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

    COST RECOVERY, NOT A HURDLE. This is the funding at which the trade merely
    breaks even; it carries no return on capital. The funding that earns a
    declared annual hurdle on fully allocated capital is larger (about 9x for a
    10 % hurdle over a 30-day hold: `carry_prospective.required_bps`). A gate
    that passes on this number has shown cost recovery only; never quote it as
    "beats the hurdle".

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
    leaves nothing to amortise against, or the break-even is too many
    settlements to count at all; None must be read as a refusal.
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
    ratio = round(rt / net, 9)
    if not math.isfinite(ratio):
        # rt / net overflowed: no horizon anyone could hold covers it, which is
        # NEVER, not an OverflowError out of ceil (the native core refuses it).
        return None
    # round() first so 11 / 1.0 is 11, not 11.000000000000002 -> 12
    return max(1, math.ceil(ratio))


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
                           hold_days: float = DEMONSTRATED_HOLD_DAYS,
                           ewma_alpha: float = EWMA_ALPHA,
                           ewma_min_prints: int = EWMA_MIN_PRINTS,
                           opportunity_cost_apr: float =
                           DEFAULT_OPPORTUNITY_COST_APR
                           ) -> CarryVerdict:
    """May this entry open, judged on the hold that has been DEMONSTRATED?

    `evaluate_entry` amortises over ASSUMED_HOLD_DAYS. This asks the harder,
    unflattering question: does the smoothed funding earn back the round trip
    (plus borrow, plus the stated opportunity cost of the capital) within
    `hold_days`, by default the measured median hold?
    Fail-closed on every unusable input, on too little history, and on funding
    that never clears. Allowed means only "not refused here".
    """
    invalid = _invalid_cost_inputs(borrow_apr=borrow_apr,
                                   round_trip_bps=round_trip_bps,
                                   hold_days=hold_days,
                                   opportunity_cost_apr=opportunity_cost_apr)
    # A hold so long that its settlement count is not a finite number is not a
    # horizon anyone can enforce; refused, not compared (and not an OverflowError).
    if not invalid and not math.isfinite(float(hold_days) * FUNDING_PERIODS_PER_DAY):
        invalid = ["hold_days"]
    # Likewise a borrow rate whose per-day figure overflows: it used to raise
    # ValueError out of min_settlements_to_clear instead of refusing.
    if not invalid and not math.isfinite((float(borrow_apr) / 365.0) * 1e4):
        invalid = ["borrow_apr"]
    # The opportunity cost the same way, and the two together: each finite
    # alone can still sum past the largest double.
    if not invalid and not math.isfinite(
            (float(borrow_apr) / 365.0) * 1e4
            + (float(opportunity_cost_apr) / 365.0) * 1e4):
        invalid = ["opportunity_cost_apr"]
    if invalid:
        return CarryVerdict(False, "COST_INPUT_INVALID",
                            detail={"fields": invalid})
    smoothed = ewma_funding_bps(funding_prints_bps, alpha=ewma_alpha,
                                min_prints=ewma_min_prints)
    if smoothed is None:
        return CarryVerdict(False, "INSUFFICIENT_FUNDING_HISTORY",
                            detail={"required": ewma_min_prints})
    borrow_per_day = (float(borrow_apr) / 365.0) * 1e4
    opportunity_per_day = (float(opportunity_cost_apr) / 365.0) * 1e4
    # Capital cost per day, borrow first then opportunity (carrycore.cpp adds
    # them in this order). At the default 0.0 it is `borrow + 0.0`: unchanged.
    capital_per_day = borrow_per_day + opportunity_per_day
    budget = int(math.floor(round(float(hold_days) * FUNDING_PERIODS_PER_DAY, 9)))
    needed = min_settlements_to_clear(
        smoothed, round_trip_bps=float(round_trip_bps),
        borrow_bps_per_day=capital_per_day)
    detail = {"settlements_needed": needed, "settlements_budget": budget,
              "hold_days": float(hold_days),
              "break_even_funding_bps_at_budget": (
                  breakeven_funding_bps_per_settlement(
                      max(budget, 1), round_trip_bps=float(round_trip_bps),
                      borrow_bps_per_day=capital_per_day)),
              "note": "the funding does not earn back the round trip inside "
                      "the hold that has actually been measured; ASSUMED_HOLD_"
                      "DAYS is not enforced by anything"}
    if needed is None or needed > budget:
        return CarryVerdict(False, "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD",
                            smoothed_funding_bps=smoothed, detail=detail)
    return CarryVerdict(True, "HOLD_CLEARS_ROUND_TRIP",
                        smoothed_funding_bps=smoothed, detail=detail)


# -- decision observability: what was known when the gate answered --------------

def decision_audit(*, funding_prints_bps: Sequence[float], perp: Any, spot: Any,
                   borrow_apr: Any, round_trip_bps: Any, hold_days: Any,
                   demonstrated_hold_days: Any, ewma_alpha: Any = EWMA_ALPHA,
                   ewma_min_prints: Any = EWMA_MIN_PRINTS,
                   opportunity_cost_apr: Any = DEFAULT_OPPORTUNITY_COST_APR
                   ) -> Dict[str, Any]:
    """The arithmetic behind an entry decision, for an auditor. NOT a gate.

    Nothing reads this to decide. It is computed from the same inputs the gate
    saw and attached to its verdict so a refusal or an approval can be checked
    by hand: how volatile the recent funding was, how often it inverted, which
    hold each half of the gate used, and what the smoothed rate is expected to
    net over each hold. Any value that cannot be computed is None, and the
    function never raises (a failure here must not be able to change, or
    block, a decision that was already taken).

        funding_volatility_bps     population std of the finite prints, bps/8h
        funding_inversion_share    share of the finite prints that are <= 0
        expected_net_over_*_bps    (carry - borrow - opportunity) * hold - round trip
    """
    def num(value: Any) -> Optional[float]:
        try:
            return _usable(value)
        except Exception:  # noqa: BLE001 - observability never raises
            return None

    try:
        prints = [float(p) for p in (funding_prints_bps or ())
                  if num(p) is not None]
    except Exception:  # noqa: BLE001
        prints = []
    out: Dict[str, Any] = {
        "observability_only": True,
        "funding_prints_seen": len(prints),
        "funding_mean_bps": (sum(prints) / len(prints)) if prints else None,
        "funding_volatility_bps": None,
        "funding_inversion_share": (
            sum(1 for p in prints if p <= 0) / len(prints)) if prints else None,
        "assumed_hold_days": num(hold_days),
        "demonstrated_hold_days": num(demonstrated_hold_days),
        "hold_used": {"amortisation_and_basis_budget": "assumed_hold_days",
                      "break_even_round_trip": "demonstrated_hold_days"},
        "borrow_apr": num(borrow_apr),
        "opportunity_cost_apr": num(opportunity_cost_apr),
        "round_trip_bps": num(round_trip_bps),
        "smoothed_funding_bps": None,
        "carry_bps_per_day": None,
        "borrow_bps_per_day": None,
        "opportunity_cost_bps_per_day": None,
        "net_carry_bps_per_day": None,
        "expected_net_over_assumed_hold_bps": None,
        "expected_net_over_demonstrated_hold_bps": None,
        "entry_basis_bps": None,
    }
    try:
        if len(prints) >= 2:
            mean = out["funding_mean_bps"]
            var = sum((p - mean) ** 2 for p in prints) / len(prints)
            out["funding_volatility_bps"] = math.sqrt(var) if math.isfinite(var) else None
        elif len(prints) == 1:
            out["funding_volatility_bps"] = 0.0
        alpha, minimum = num(ewma_alpha), num(ewma_min_prints)
        if alpha is not None and minimum is not None:
            out["smoothed_funding_bps"] = ewma_funding_bps(
                prints, alpha=alpha, min_prints=int(minimum))
        smoothed = out["smoothed_funding_bps"]
        b, o = out["borrow_apr"], out["opportunity_cost_apr"]
        if b is not None:
            out["borrow_bps_per_day"] = (b / 365.0) * 1e4
        if o is not None:
            out["opportunity_cost_bps_per_day"] = (o / 365.0) * 1e4
        if smoothed is not None:
            out["carry_bps_per_day"] = smoothed * FUNDING_PERIODS_PER_DAY
        if None not in (out["carry_bps_per_day"], out["borrow_bps_per_day"],
                        out["opportunity_cost_bps_per_day"]):
            net = (out["carry_bps_per_day"] - out["borrow_bps_per_day"]
                   - out["opportunity_cost_bps_per_day"])
            out["net_carry_bps_per_day"] = net
            rt = out["round_trip_bps"]
            if rt is not None:
                for key, hold in (("expected_net_over_assumed_hold_bps",
                                   out["assumed_hold_days"]),
                                  ("expected_net_over_demonstrated_hold_bps",
                                   out["demonstrated_hold_days"])):
                    if hold is not None:
                        out[key] = net * hold - rt
        p_, s_ = num(perp), num(spot)
        if p_ is not None and s_ is not None and p_ > 0 and s_ > 0:
            out["entry_basis_bps"] = basis_bps(p_, s_)
    except Exception as exc:  # noqa: BLE001 - observability never raises
        out["audit_error"] = f"{type(exc).__name__}: {exc}"
    return out


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
               demonstrated_hold_days: float = DEMONSTRATED_HOLD_DAYS,
               ewma_alpha: float = EWMA_ALPHA,
               ewma_min_prints: int = EWMA_MIN_PRINTS,
               opportunity_cost_apr: float = DEFAULT_OPPORTUNITY_COST_APR
               ) -> CarryVerdict:
    """May the book open this pair? `evaluate_entry`, then the hold check.

    A pure refinement: every refusal `evaluate_entry` gives is returned
    unchanged, and only an entry it ALLOWS can be refused here, with
    BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD (or COST_INPUT_INVALID when the
    demonstrated hold is not a positive number). Nothing is ever allowed that
    `evaluate_entry` refused, exits are untouched, and no parameter moves.

    A refused entry carries `evaluate_entry`'s arithmetic (smoothed funding,
    basis, net edge) so callers log the same fields whichever gate refused.

    Every verdict also carries `detail["audit"]` (`decision_audit`): funding
    volatility and inversion share, the assumed and demonstrated holds and the
    expected net over each. It is attached AFTER the decision and nothing
    reads it to decide.
    """
    verdict = _entry_gate_decision(
        funding_prints_bps=funding_prints_bps, perp=perp, spot=spot,
        borrow_apr=borrow_apr, round_trip_bps=round_trip_bps,
        hold_days=hold_days, demonstrated_hold_days=demonstrated_hold_days,
        ewma_alpha=ewma_alpha, ewma_min_prints=ewma_min_prints,
        opportunity_cost_apr=opportunity_cost_apr)
    from dataclasses import replace
    audit = decision_audit(
        funding_prints_bps=funding_prints_bps, perp=perp, spot=spot,
        borrow_apr=borrow_apr, round_trip_bps=round_trip_bps,
        hold_days=hold_days, demonstrated_hold_days=demonstrated_hold_days,
        ewma_alpha=ewma_alpha, ewma_min_prints=ewma_min_prints,
        opportunity_cost_apr=opportunity_cost_apr)
    return replace(verdict, detail={**verdict.detail, "audit": audit})


def breakeven_funding_bps(*, perp: float, spot: float, borrow_apr: float,
                          round_trip_bps: float,
                          hold_days: float = ASSUMED_HOLD_DAYS,
                          demonstrated_hold_days: float = DEMONSTRATED_HOLD_DAYS,
                          ewma_alpha: float = EWMA_ALPHA,
                          ewma_min_prints: int = EWMA_MIN_PRINTS,
                          opportunity_cost_apr: float = DEFAULT_OPPORTUNITY_COST_APR,
                          ceiling_bps: float = 100.0) -> Optional[float]:
    """The smallest steady per-print funding (bps) at which `entry_gate` would
    allow opening NOW, or None when even `ceiling_bps` does not clear it.

    ADVISORY ONLY. It asks the production gate the question "what funding
    would you need?" by bisection, so it cannot drift from the gate; nothing
    decides on it and it places nothing. A steady rate is a model of what the
    funding would have to be, not a forecast that it will be.
    """
    def passes(f: float) -> bool:
        history = [f] * max(int(ewma_min_prints), 3)
        return bool(entry_gate(
            funding_prints_bps=history, perp=perp, spot=spot,
            borrow_apr=borrow_apr, round_trip_bps=round_trip_bps,
            hold_days=hold_days, demonstrated_hold_days=demonstrated_hold_days,
            ewma_alpha=ewma_alpha, ewma_min_prints=ewma_min_prints,
            opportunity_cost_apr=opportunity_cost_apr))

    lo, hi = 0.0, float(ceiling_bps)
    if not passes(hi):
        return None
    if passes(lo):
        return 0.0
    for _ in range(40):
        mid = (lo + hi) / 2.0
        if passes(mid):
            hi = mid
        else:
            lo = mid
    return hi


def _entry_gate_decision(*, funding_prints_bps: Sequence[float], perp: float,
                         spot: float, borrow_apr: float, round_trip_bps: float,
                         hold_days: float, demonstrated_hold_days: float,
                         ewma_alpha: float, ewma_min_prints: int,
                         opportunity_cost_apr: float) -> CarryVerdict:
    """`entry_gate`'s decision, before the audit is attached."""
    verdict = evaluate_entry(
        funding_prints_bps=funding_prints_bps, perp=perp, spot=spot,
        borrow_apr=borrow_apr, round_trip_bps=round_trip_bps,
        hold_days=hold_days, ewma_alpha=ewma_alpha,
        ewma_min_prints=ewma_min_prints,
        opportunity_cost_apr=opportunity_cost_apr)
    if not verdict.allowed:
        return verdict
    horizon = hold_clears_round_trip(
        funding_prints_bps=funding_prints_bps, round_trip_bps=round_trip_bps,
        borrow_apr=borrow_apr, hold_days=demonstrated_hold_days,
        ewma_alpha=ewma_alpha, ewma_min_prints=ewma_min_prints,
        opportunity_cost_apr=opportunity_cost_apr)
    if horizon.allowed:
        return verdict
    from dataclasses import replace
    return replace(verdict, allowed=False, reason=horizon.reason,
                   detail=dict(horizon.detail))
