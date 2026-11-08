"""The ONE economic model every strategy consumes (offline, pure, no I/O, no venue, no orders).

Design rules, each enforced in code and attacked in tests/test_economic_faults.py:

* A number carries its PROVENANCE (``measured`` > ``venue_stated`` > ``derived`` > ``modelled`` > ``assumed`` > ``unknown``).
  Arithmetic PROPAGATES: any unknown operand makes the result unknown (value None, never 0); a result's provenance is the WEAKEST of
  its inputs, floored at ``derived`` (a computed number is never "measured"). An unknown can never be summed into a number.
  ``Quantity(1, measured) != Quantity(1, modelled)``: an estimate is not an observation.
* Venue-facing quantities (price, qty, tick, step, notional, fee, cash) are exact ``Decimal``. Floats are refused.
* The cost chain is THEORETICAL -> EXECUTABLE -> CAPTURABLE -> REALISED. Each stage is a Quantity in bps of notional; a stage whose
  inputs are not known is unknown, so a PASS can only be built from fully known, fresh inputs.
* Funding sign convention: positive rate => LONGS PAY, SHORTS RECEIVE (cash = -signed_qty * mark * rate; signed_qty < 0 is short).
* A fee tier carries an AGE; a tier older than the stated limit is UNKNOWN, not current.
* FLAT is the venue's own position read, never fill arithmetic (``realised_result`` needs the venue's read and refuses a mismatch).
* The funding edge is DERIVED from the hold (``carry_gross_edge(hold_days=)``: n = hold_days * the instrument's own settlements per day); a scenario whose
  hold differs from the edge's is refused. A known cost input below zero is refused (a negative cost would manufacture edge); hold_days must be positive.
* ``assumed`` anywhere that matters (a cost, the gross edge, the hedge ratio, a maker probability) can never PASS: it makes the evidence INCONCLUSIVE.
* ``realised_result`` reports REALISED P&L only (average-cost closed trades + funding - fees). Open inventory's mark-to-market is reported separately, with
  its mark's timestamp/provenance; funding must be backed by fills and by a position that matches them; a fee in a foreign currency is refused.

It never changes ``carry_costs`` / ``ledger`` / ``funding_reconcile`` / ``carry_broker``; tests cross-check against them.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

BPS = Decimal(10000)
HOUR_MS = 3_600_000

# --------------------------------------------------------------------------------------------------------------------------
# Quantity: a number with provenance
# --------------------------------------------------------------------------------------------------------------------------
PROVENANCE = ("measured", "venue_stated", "derived", "modelled", "assumed", "unknown")   # strongest -> weakest
_RANK = {p: i for i, p in enumerate(PROVENANCE)}
OBSERVED = frozenset({"measured", "venue_stated"})


class EconomicModelError(ValueError):
    """An input the model refuses (never silently repaired)."""


# One canonical spelling of a number: ASCII digits, optional sign, optional fraction, optional exponent of at most two digits.
# Python's own Decimal()/int() also accept underscores ('1_0' == 10), surrounding spaces and non-ASCII digits; none of that enters the model.
_NUMBER_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,2})?", re.ASCII)
MAX_NUMBER_CHARS = 64
MAX_ADJUSTED_EXPONENT = 30        # |adjusted exponent| above this (1e30, 1e-30) is a unit slip or an attack, not a venue quantity


def to_decimal(x: Any) -> Decimal:
    if isinstance(x, bool) or isinstance(x, float):
        raise EconomicModelError(f"floats/bools are refused for exact arithmetic: {x!r} (pass a str, int or Decimal)")
    if isinstance(x, str):
        if len(x) > MAX_NUMBER_CHARS or not _NUMBER_RE.fullmatch(x):
            raise EconomicModelError(f"not a canonical decimal (ASCII digits, optional sign/fraction/short exponent only): {x!r}")
        d = Decimal(x)
    elif isinstance(x, (int, Decimal)):
        d = Decimal(x)
    else:
        raise EconomicModelError(f"not a decimal: {type(x).__name__} (pass a str, int or Decimal)")
    if not d.is_finite():
        raise EconomicModelError(f"non-finite: {x!r}")
    if d != 0 and abs(d.adjusted()) > MAX_ADJUSTED_EXPONENT:
        raise EconomicModelError(f"magnitude out of bounds (|exponent| > {MAX_ADJUSTED_EXPONENT}): {x!r}")
    return d


def weakest(*provs: str) -> str:
    return max(provs, key=lambda p: _RANK[p])


@dataclass(frozen=True)
class Quantity:
    value: Optional[Decimal]
    provenance: str
    unit: str = ""

    def __post_init__(self) -> None:
        if self.provenance not in _RANK:
            raise EconomicModelError(f"unknown provenance {self.provenance!r}")
        if self.value is None:
            if self.provenance != "unknown":
                raise EconomicModelError("a value of None must carry provenance 'unknown'")
        else:
            if self.provenance == "unknown":
                raise EconomicModelError("provenance 'unknown' must not carry a value")
            object.__setattr__(self, "value", to_decimal(self.value))

    @property
    def known(self) -> bool:
        return self.value is not None

    @property
    def observed(self) -> bool:
        return self.known and self.provenance in OBSERVED

    def require(self) -> Decimal:
        if self.value is None:
            raise EconomicModelError("unknown quantity used as a number")
        return self.value

    # arithmetic: unknown in -> unknown out; provenance = weakest(inputs, derived)
    def _bin(self, other: Any, fn) -> "Quantity":
        o = _coerce(other)
        prov = weakest(self.provenance, o.provenance, "derived") if (self.known and o.known) else "unknown"
        if not (self.known and o.known):
            return Quantity(None, "unknown", self.unit or o.unit)
        try:
            v = fn(self.value, o.value)
        except ArithmeticError:
            return Quantity(None, "unknown", self.unit)
        return Quantity(v, prov, self.unit or o.unit)

    def __add__(self, o): return self._bin(o, lambda a, b: a + b)
    def __radd__(self, o): return _coerce(o)._bin(self, lambda a, b: a + b)
    def __sub__(self, o): return self._bin(o, lambda a, b: a - b)
    def __rsub__(self, o): return _coerce(o)._bin(self, lambda a, b: a - b)
    def __mul__(self, o): return self._bin(o, lambda a, b: a * b)
    def __rmul__(self, o): return _coerce(o)._bin(self, lambda a, b: a * b)
    def __truediv__(self, o): return self._bin(o, lambda a, b: a / b)       # /0 -> unknown, never inf
    def __neg__(self): return self._bin(Decimal(-1), lambda a, b: a * b)
    def __abs__(self): return self._bin(Decimal(1), lambda a, b: abs(a * b))
    __hash__ = None  # type: ignore[assignment]


def _coerce(o: Any) -> Quantity:
    if isinstance(o, Quantity):
        return o
    return Quantity(to_decimal(o), "measured")          # an exact constant


def unknown(unit: str = "") -> Quantity:
    return Quantity(None, "unknown", unit)


def qsum(items: Sequence[Quantity], unit: str = "") -> Quantity:
    total = Quantity(Decimal(0), "derived", unit)
    for q in items:
        total = total + q
    return total


def at_least(q: Quantity, floor: Any) -> Optional[bool]:
    """Tri-state comparison: None when q is unknown (an unknown is neither above nor below)."""
    return None if not q.known else q.value >= to_decimal(floor)


def as_float(q: Quantity) -> Optional[float]:
    return None if not q.known else float(q.value)


# --------------------------------------------------------------------------------------------------------------------------
# Instrument metadata
# --------------------------------------------------------------------------------------------------------------------------
ALLOWED_INTERVAL_HOURS = (1, 2, 4, 8)
MAX_FUNDING_FRACTION = Decimal("0.05")          # a per-settlement cap above 5% is a unit error, not a venue
STATUSES = ("Trading", "PreLaunch", "Settling", "Delisted", "Closed")


@dataclass(frozen=True)
class Instrument:
    symbol: str
    category: str                                # "linear" | "spot"
    base_coin: str
    quote_coin: str
    tick_size: Decimal
    qty_step: Decimal
    min_qty: Decimal
    min_notional: Decimal
    status: str
    funding_interval_hours: Optional[int] = None  # perps only; NEVER assumed 8h
    funding_cap: Optional[Decimal] = None         # per-settlement FRACTION (0.0075 = 75 bps)
    funding_floor: Optional[Decimal] = None
    source_ms: int = 0

    def problems(self) -> List[str]:
        p: List[str] = []
        if not self.symbol or self.symbol != self.symbol.upper():
            p.append("SYMBOL_INVALID")
        if self.category not in ("linear", "spot"):
            p.append("CATEGORY_INVALID")
        if self.symbol and self.base_coin and self.quote_coin and self.symbol != self.base_coin + self.quote_coin:
            p.append("SYMBOL_DOES_NOT_MATCH_COINS")
        for n in ("tick_size", "qty_step", "min_qty"):
            if not isinstance(getattr(self, n), Decimal) or getattr(self, n) <= 0:
                p.append(f"{n.upper()}_NOT_POSITIVE")
        if not isinstance(self.min_notional, Decimal) or self.min_notional < 0:
            p.append("MIN_NOTIONAL_INVALID")
        if self.status not in STATUSES:
            p.append("STATUS_UNKNOWN")
        if self.category == "linear":
            if self.funding_interval_hours not in ALLOWED_INTERVAL_HOURS:
                p.append("FUNDING_INTERVAL_INVALID")
            cap, flr = self.funding_cap, self.funding_floor
            if not isinstance(cap, Decimal) or not isinstance(flr, Decimal):
                p.append("FUNDING_BOUNDS_MISSING")
            else:
                if cap < 0 or flr > 0 or cap < flr:
                    p.append("FUNDING_BOUNDS_INCONSISTENT")
                if max(abs(cap), abs(flr)) > MAX_FUNDING_FRACTION:
                    p.append("FUNDING_BOUNDS_IMPLAUSIBLE_UNIT")
        return p

    def validate(self) -> "Instrument":
        pr = self.problems()
        if pr:
            raise EconomicModelError(f"instrument {self.symbol!r} invalid: {pr}")
        return self

    @property
    def tradable(self) -> bool:
        return self.status == "Trading"

    @property
    def interval_ms(self) -> int:
        if self.funding_interval_hours is None:
            raise EconomicModelError(f"{self.symbol} ({self.category}) has no funding interval")
        return self.funding_interval_hours * HOUR_MS

    @property
    def settlements_per_day(self) -> Decimal:
        return Decimal(24) / Decimal(self.funding_interval_hours or 0) if self.funding_interval_hours else Decimal(0)

    def round_qty(self, qty: Decimal) -> Decimal:
        return (qty / self.qty_step).to_integral_value(rounding=ROUND_DOWN) * self.qty_step

    def order_problems(self, qty: Decimal, price: Decimal) -> List[str]:
        p: List[str] = []
        if not self.tradable:
            p.append("NOT_TRADING")
        if qty < self.min_qty:
            p.append("BELOW_MIN_QTY")
        if qty % self.qty_step != 0:
            p.append("QTY_OFF_STEP")
        if price % self.tick_size != 0:
            p.append("PRICE_OFF_TICK")
        if qty * price < self.min_notional:
            p.append("BELOW_MIN_NOTIONAL")
        return p

    @staticmethod
    def from_venue_row(row: Mapping[str, Any], *, expect_symbol: str, expect_category: str,
                       source_ms: int = 0) -> "Instrument":
        """Strict parse of a Bybit-shaped instruments-info row. A row for another symbol/category is REFUSED."""
        try:
            if row["symbol"] != expect_symbol:
                raise EconomicModelError(f"wrong symbol row: got {row['symbol']!r} expected {expect_symbol!r}")
            cat = row["category"]                     # required: a row that does not say what it is is not trusted
            if cat != expect_category:
                raise EconomicModelError(f"wrong category row: got {cat!r} expected {expect_category!r}")
            lot, pf = row["lotSizeFilter"], row["priceFilter"]
            interval = None
            cap = flr = None
            if expect_category == "linear":
                minutes = to_decimal(row["fundingInterval"])               # Bybit states minutes
                if minutes % 60 != 0:
                    raise EconomicModelError(f"funding interval {minutes} min is not whole hours")
                interval = int(minutes / 60)
                cap, flr = to_decimal(row["upperFundingRate"]), to_decimal(row["lowerFundingRate"])
            inst = Instrument(
                symbol=expect_symbol, category=expect_category, base_coin=str(row["baseCoin"]), quote_coin=str(row["quoteCoin"]),
                tick_size=to_decimal(pf["tickSize"]), qty_step=to_decimal(lot["qtyStep"]), min_qty=to_decimal(lot["minOrderQty"]),
                min_notional=to_decimal(lot.get("minNotionalValue", lot.get("minOrderAmt", "0"))), status=str(row["status"]),
                funding_interval_hours=interval, funding_cap=cap, funding_floor=flr, source_ms=int(source_ms))
        except KeyError as exc:
            raise EconomicModelError(f"instrument row missing field {exc}") from exc
        return inst.validate()


def select_instrument(rows: Sequence[Mapping[str, Any]], symbol: str, category: str, source_ms: int = 0) -> Instrument:
    hits = [r for r in rows if r.get("symbol") == symbol]
    if len(hits) != 1:
        raise EconomicModelError(f"expected exactly one row for {symbol!r}, found {len(hits)}")
    return Instrument.from_venue_row(hits[0], expect_symbol=symbol, expect_category=category, source_ms=source_ms)


# --------------------------------------------------------------------------------------------------------------------------
# Fees (with age) and units
# --------------------------------------------------------------------------------------------------------------------------
MAX_FEE_FRACTION = Decimal("0.005")             # 50 bps: above this a "fee" is a unit slip (5.5 read as a fraction, percent as fraction)
MIN_NONZERO_FEE_FRACTION = Decimal("0.00001")   # 0.1 bps: a smaller nonzero fee is a unit slip the other way (percent read as bps)


def fee_fraction(value: Any, unit: str) -> Decimal:
    """Convert a stated fee to a per-notional FRACTION. unit in {'fraction','bps','percent'}; implausible results are refused."""
    v = to_decimal(value)
    if unit == "fraction":
        f = v
    elif unit == "bps":
        f = v / BPS
    elif unit == "percent":
        f = v / Decimal(100)
    else:
        raise EconomicModelError(f"unknown fee unit {unit!r}")
    if abs(f) > MAX_FEE_FRACTION:
        raise EconomicModelError(f"fee {value!r} as {unit} = {f} of notional exceeds {MAX_FEE_FRACTION}: wrong unit?")
    if f != 0 and abs(f) < MIN_NONZERO_FEE_FRACTION:
        raise EconomicModelError(f"fee {value!r} as {unit} = {f} is below 0.1 bps: wrong unit?")
    return f


@dataclass(frozen=True)
class FeeTier:
    maker: Decimal                 # fraction; NEGATIVE = rebate
    taker: Decimal
    source: str                    # measured | venue_stated | assumed
    observed_ms: Optional[int] = None

    def __post_init__(self) -> None:
        if self.source not in ("measured", "venue_stated", "assumed"):
            raise EconomicModelError(f"fee tier source {self.source!r}")
        for r in (self.maker, self.taker):
            if not isinstance(r, Decimal) or abs(r) > MAX_FEE_FRACTION:
                raise EconomicModelError("fee rates must be Decimal fractions within +-50 bps")
        if self.taker < 0:
            raise EconomicModelError("a negative taker fee is not a venue schedule")
        if self.source != "assumed" and self.observed_ms is None:
            raise EconomicModelError("a measured/venue_stated fee tier must carry observed_ms (its age)")

    def rate(self, role: str, now_ms: int, max_age_ms: int) -> Quantity:
        if role not in ("maker", "taker"):
            raise EconomicModelError(f"role {role!r}")
        if self.observed_ms is not None:
            age = now_ms - self.observed_ms
            if age < 0 or age > max_age_ms:               # stale OR from the future: not current
                return unknown("fraction")
        return Quantity(self.maker if role == "maker" else self.taker, self.source, "fraction")


def held_qty_after_fee(qty: Decimal, fee_rate: Decimal, fee_in_base: bool) -> Decimal:
    """A BUY whose fee is taken in the base coin leaves qty*(1-rate) held; a hedge sized to `qty` is over-hedged."""
    return qty * (Decimal(1) - fee_rate) if fee_in_base else qty


# --------------------------------------------------------------------------------------------------------------------------
# Books: depth, slippage, executability
# --------------------------------------------------------------------------------------------------------------------------
Level = Tuple[Decimal, Decimal]


@dataclass(frozen=True)
class Book:
    bids: Tuple[Level, ...]        # best first (descending price)
    asks: Tuple[Level, ...]        # best first (ascending price)
    ts_ms: int

    def problems(self) -> List[str]:
        p: List[str] = []
        if not self.bids or not self.asks:
            return ["BOOK_EMPTY_SIDE"]
        for side, name, desc in ((self.bids, "BIDS", True), (self.asks, "ASKS", False)):
            if any(px <= 0 or q <= 0 for px, q in side):
                p.append(f"{name}_NONPOSITIVE")
            prices = [px for px, _ in side]
            if prices != sorted(set(prices), reverse=desc):
                p.append(f"{name}_NOT_STRICTLY_ORDERED")
        if self.bids[0][0] >= self.asks[0][0]:
            p.append("BOOK_CROSSED")
        return p

    @property
    def mid(self) -> Decimal:
        return (self.bids[0][0] + self.asks[0][0]) / 2

    def total_qty(self, side: str) -> Decimal:
        return sum(((q for _, q in (self.asks if side == "buy" else self.bids))), Decimal(0))


@dataclass(frozen=True)
class Walk:
    executable: bool
    reason: str
    side: str
    qty: Decimal
    avg_price: Optional[Decimal] = None
    half_spread_bps: Optional[Decimal] = None      # cost of crossing from mid to the touch
    slippage_bps: Optional[Decimal] = None         # cost of walking deeper than the touch

    @property
    def total_bps(self) -> Optional[Decimal]:
        return None if self.avg_price is None else self.half_spread_bps + self.slippage_bps


def walk_book(book: Book, side: str, qty: Decimal, *, now_ms: int, max_age_ms: int) -> Walk:
    if side not in ("buy", "sell"):
        raise EconomicModelError(f"side {side!r}")
    qty = to_decimal(qty)
    bad = book.problems()
    if bad:
        return Walk(False, "BAD_BOOK:" + ",".join(bad), side, qty)
    age = now_ms - book.ts_ms
    if age < 0 or age > max_age_ms:
        return Walk(False, "STALE_BOOK", side, qty)
    if qty <= 0:
        return Walk(False, "QTY_NOT_POSITIVE", side, qty)
    levels = book.asks if side == "buy" else book.bids
    left, cost = qty, Decimal(0)
    for px, q in levels:
        take = min(left, q)
        cost += take * px
        left -= take
        if left == 0:
            break
    if left > 0:
        return Walk(False, "BOOK_TOO_THIN", side, qty)
    avg = cost / qty
    touch, mid = levels[0][0], book.mid
    half = abs(touch - mid) / mid * BPS
    slip = abs(avg - touch) / mid * BPS
    return Walk(True, "OK", side, qty, avg, half, slip)


# --------------------------------------------------------------------------------------------------------------------------
# Funding: expected edge and settlement screening
# --------------------------------------------------------------------------------------------------------------------------
def funding_cash(rate_fraction: Decimal, signed_qty: Decimal, mark: Decimal) -> Decimal:
    """Cash for one settlement. positive rate => longs pay, shorts receive. +cash = received."""
    return -signed_qty * mark * rate_fraction


@dataclass(frozen=True)
class GrossEdge(Quantity):
    """A funding edge that REMEMBERS the hold it was built for (``hold_days``, ``n_settlements``). ``evaluate_chain`` refuses a scenario whose hold differs:
    the edge is derived from the hold, never an independently stated settlement count."""
    hold_days: Optional[Decimal] = None
    n_settlements: Optional[Decimal] = None


def carry_gross_edge(perp: Instrument, expected_rate: Quantity, *, hold_days: Any, short: bool = True) -> Quantity:
    """THEORETICAL edge in bps of notional: sign * rate * n, with n = hold_days * the instrument's OWN settlements per day (a whole number of settlements).
    There is no free ``n_settlements``: 24 settlements for a 1-day hold on an 8h perp (8x overstated) cannot be expressed. The rate must lie inside the
    instrument's own cap/floor, else UNKNOWN."""
    if perp.category != "linear":
        raise EconomicModelError("funding edge needs a perpetual")
    hd = to_decimal(hold_days)
    if hd <= 0:
        raise EconomicModelError("hold_days must be positive")
    n = hd * perp.settlements_per_day
    if n <= 0 or n != n.to_integral_value():
        raise EconomicModelError(f"hold_days={hd} on a {perp.funding_interval_hours}h perp is {n} settlements; a hold must cover a whole number of settlements")
    if not expected_rate.known:
        return unknown("bps")
    r = expected_rate.value
    if r > perp.funding_cap or r < perp.funding_floor:
        return unknown("bps")                              # outside the venue's own bounds: a unit slip, not an edge
    sign = Decimal(1) if short else Decimal(-1)
    q = expected_rate * sign * BPS * n * Quantity(Decimal(1), "measured", "bps")
    return GrossEdge(q.value, q.provenance, "bps", hd, n)


@dataclass(frozen=True)
class Settlement:
    ts_ms: int
    symbol: str
    rate: Decimal                  # per-settlement fraction as the venue printed it
    position_qty: Decimal          # signed: < 0 short
    mark: Optional[Decimal]
    cash_usd: Decimal              # reported by the venue/export: + received


MAGNITUDE_BRACKET = Decimal(2)      # same per-row bracket funding_reconcile uses; a x10 slip or a cumulative field falls outside it
# A x2 per-row bracket alone lets a consistent +-50% bias through, so the AGGREGATE of the checkable rows must also agree with the venue's own formula.
# Rows of both signs average out; a one-sided deviation does not.
AGGREGATE_BIAS_TOLERANCE = Decimal("0.10")
FUNDING_CASH_DUST = Decimal("0.01")  # quote units: cash this small at a zero rate is rounding, anything more is invented funding


@dataclass(frozen=True)
class FundingRecon:
    total: Quantity
    flags: Tuple[str, ...]
    missing_ts: Tuple[int, ...]
    rows: int
    symbol: Optional[str] = None                                   # what the rows are about ...
    positions: Tuple[Tuple[int, Decimal], ...] = ()                # ... and the (ts_ms, signed position_qty) each row claims, for a cross-check against fills
    window: Optional[Tuple[int, int]] = None                       # the (start_ms, end_ms] this reconciliation covers ...
    settlement_ts: Tuple[int, ...] = ()                            # ... and the settlement timestamps it actually holds, so realised_result can tell what it does NOT cover
    interval_ms: Optional[int] = None                              # the instrument's own settlement grid


def expected_settlement_times(perp: Instrument, start_ms: int, end_ms: int) -> List[int]:
    """Every settlement t with start_ms < t <= end_ms on the instrument's OWN interval grid (UTC multiples)."""
    iv = perp.interval_ms
    first = (start_ms // iv + 1) * iv
    return list(range(first, end_ms + 1, iv))


def reconcile_funding(perp: Instrument, rows: Sequence[Settlement], start_ms: int, end_ms: int) -> FundingRecon:
    flags: List[str] = []
    seen: Dict[int, int] = {}
    for r in rows:
        seen[r.ts_ms] = seen.get(r.ts_ms, 0) + 1
    if any(c > 1 for c in seen.values()):
        flags.append("DUPLICATE_SETTLEMENT")
    want = expected_settlement_times(perp, start_ms, end_ms)
    missing = tuple(t for t in want if t not in seen)
    if missing:
        flags.append("MISSING_SETTLEMENT")
    wantset = set(want)
    if any(t not in wantset for t in seen):
        flags.append("SETTLEMENT_OFF_INTERVAL_GRID")       # catches a venue/export on another interval than the instrument's
    exp_sum, cash_sum = Decimal(0), Decimal(0)
    for r in rows:
        if r.symbol != perp.symbol:
            flags.append("WRONG_SYMBOL_ROW")
        if r.rate > perp.funding_cap or r.rate < perp.funding_floor:
            flags.append("RATE_OUTSIDE_VENUE_BOUNDS")
        if r.rate == 0 and abs(r.cash_usd) > FUNDING_CASH_DUST:
            flags.append("FUNDING_CASH_AT_ZERO_RATE")           # a zero rate pays nothing: cash here is invented, whatever the position
        if r.mark is None or r.mark <= 0 or r.position_qty == 0:
            flags.append("UNCHECKABLE_ROW")
            continue
        exp = funding_cash(r.rate, r.position_qty, r.mark)
        if r.rate != 0:
            if (r.cash_usd > 0) != (exp > 0) and r.cash_usd != 0:
                flags.append("FUNDING_SIGN_CONTRADICTION")
                continue
            ratio = abs(r.cash_usd) / abs(exp)
            if ratio > MAGNITUDE_BRACKET or ratio * MAGNITUDE_BRACKET < 1:
                flags.append("FUNDING_MAGNITUDE_CONTRADICTION")
            exp_sum += exp
            cash_sum += r.cash_usd
    if exp_sum != 0 and abs(cash_sum - exp_sum) > AGGREGATE_BIAS_TOLERANCE * abs(exp_sum):
        flags.append("FUNDING_SYSTEMATIC_BIAS")
    flags_t = tuple(sorted(set(flags)))
    pos = tuple((r.ts_ms, r.position_qty) for r in sorted(rows, key=lambda x: x.ts_ms))
    if flags_t:
        return FundingRecon(unknown("usd"), flags_t, missing, len(rows), perp.symbol, pos, (start_ms, end_ms), tuple(sorted(seen)), perp.interval_ms)
    return FundingRecon(Quantity(sum((r.cash_usd for r in rows), Decimal(0)), "measured", "usd"), (), (), len(rows), perp.symbol, pos,
                        (start_ms, end_ms), tuple(sorted(seen)), perp.interval_ms)


# --------------------------------------------------------------------------------------------------------------------------
# The cost chain
# --------------------------------------------------------------------------------------------------------------------------
STAGES = ("THEORETICAL", "EXECUTABLE", "CAPTURABLE", "REALISED")


@dataclass(frozen=True)
class Leg:
    name: str
    instrument: Instrument
    side: str                              # ENTRY side: 'buy' | 'sell'
    book: Optional[Book]
    fee_tier: Optional[FeeTier]
    role: str = "taker"                    # 'taker' | 'maker'
    fee_in_base: bool = False


@dataclass(frozen=True)
class Scenario:
    legs: Tuple[Leg, ...]
    notional_usd: Decimal
    gross_edge_bps: Quantity               # THEORETICAL (funding*n, or a spread's gross capture), bps of notional
    hold_days: Decimal
    now_ms: int
    max_book_age_ms: int
    max_fee_age_ms: int
    impact_bps: Quantity = field(default_factory=lambda: unknown("bps"))
    basis_cost_bps: Quantity = field(default_factory=lambda: unknown("bps"))
    borrow_apr: Quantity = field(default_factory=lambda: unknown("fraction"))
    opportunity_cost_apr: Quantity = field(default_factory=lambda: unknown("fraction"))
    hedge_ratio: Quantity = field(default_factory=lambda: unknown("ratio"))        # hedge qty / exposure qty, 1 = perfect
    adverse_move_bps: Quantity = field(default_factory=lambda: unknown("bps"))      # move a residual exposure is exposed to
    adverse_selection_bps: Quantity = field(default_factory=lambda: unknown("bps"))  # per maker leg round trip
    margin_usd: Quantity = field(default_factory=lambda: unknown("usd"))
    # probability that ONE maker leg fills; required (explicit, with provenance) for any maker leg, else the maker leg's cost is UNKNOWN
    maker_fill_probability: Quantity = field(default_factory=lambda: unknown("probability"))


@dataclass(frozen=True)
class ChainResult:
    components: Mapping[str, Quantity]
    stages: Mapping[str, Quantity]
    executable: bool
    blockers: Tuple[str, ...]
    capital_utilisation: Quantity          # notional / margin
    digest: str
    scenario_inputs: Mapping[str, Quantity] = field(default_factory=dict)      # the scenario's own non-cost inputs (gross edge, hedge ratio ...) with provenance

    @property
    def unknown_inputs(self) -> Tuple[str, ...]:
        return tuple(sorted(k for k, q in self.components.items() if not q.known))

    @property
    def assumed_inputs(self) -> Tuple[str, ...]:
        """Every input whose number is an ASSUMPTION: cost components AND the scenario's gross edge / hedge ratio / maker probability. Any one blocks a PASS."""
        names = {k for k, q in self.components.items() if q.provenance == "assumed"}
        names |= {k for k, q in self.scenario_inputs.items() if q.known and q.provenance == "assumed"}
        return tuple(sorted(names))

    @property
    def net_edge_bps(self) -> Quantity:
        return self.stages["CAPTURABLE"]


def canonical(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, Quantity):
        return {"v": None if obj.value is None else str(obj.value), "p": obj.provenance, "u": obj.unit}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: canonical(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Mapping):
        return {str(k): canonical(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, (list, tuple)):
        return [canonical(v) for v in obj]
    return obj


def digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(canonical(obj), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _leg_costs(leg: Leg, notional: Decimal, scn: Scenario) -> Tuple[Quantity, Quantity, Quantity, List[str]]:
    """(fee_bps, spread_bps, slippage_bps, blockers) for ONE leg's round trip (entry + exit), in bps of the shared notional."""
    blockers: List[str] = []
    rate = leg.fee_tier.rate(leg.role, scn.now_ms, scn.max_fee_age_ms) if leg.fee_tier else unknown("fraction")
    fee = rate * Decimal(2) * BPS
    if leg.book is None:
        return fee, unknown("bps"), unknown("bps"), blockers + [f"{leg.name}:NO_BOOK"]
    ref = leg.book.mid if not leg.book.problems() else None
    if ref is None:
        return fee, unknown("bps"), unknown("bps"), blockers + [f"{leg.name}:BAD_BOOK"]
    qty = leg.instrument.round_qty(notional / ref)
    op = leg.instrument.order_problems(qty, ref.quantize(leg.instrument.tick_size, rounding=ROUND_DOWN))
    op = [x for x in op if x != "PRICE_OFF_TICK"]
    if op:
        return fee, unknown("bps"), unknown("bps"), blockers + [f"{leg.name}:{x}" for x in op]
    exit_side = "sell" if leg.side == "buy" else "buy"
    if leg.role == "maker":
        # not crossing is only free if the order fills: the zero spread/slippage is as strong as the stated fill probability and never "derived"
        p = scn.maker_fill_probability
        if not p.known:
            return fee, unknown("bps"), unknown("bps"), blockers + [f"{leg.name}:MAKER_FILL_PROBABILITY_UNKNOWN"]
        zero = Quantity(Decimal(0), p.provenance, "bps")
        return fee, zero, zero, blockers
    ent = walk_book(leg.book, leg.side, qty, now_ms=scn.now_ms, max_age_ms=scn.max_book_age_ms)
    ext = walk_book(leg.book, exit_side, qty, now_ms=scn.now_ms, max_age_ms=scn.max_book_age_ms)
    for w in (ent, ext):
        if not w.executable:
            blockers.append(f"{leg.name}:{w.side}:{w.reason}")
    if blockers and not (ent.executable and ext.executable):
        return fee, unknown("bps"), unknown("bps"), blockers
    spread = Quantity(ent.half_spread_bps + ext.half_spread_bps, "modelled", "bps")     # a walked snapshot is a model, not a fill
    slip = Quantity(ent.slippage_bps + ext.slippage_bps, "modelled", "bps")
    return fee, spread, slip, blockers


def _validate_scenario(scn: Scenario) -> None:
    """Refuse inputs that would make a cost into an edge. An UNKNOWN input stays unknown (INCONCLUSIVE); a KNOWN one must be a non-negative cost."""
    hold = to_decimal(scn.hold_days)
    if hold <= 0:
        raise EconomicModelError(f"hold_days must be positive, got {hold}")
    for name in ("impact_bps", "basis_cost_bps", "borrow_apr", "opportunity_cost_apr", "adverse_move_bps", "adverse_selection_bps", "hedge_ratio"):
        q = getattr(scn, name)
        if not isinstance(q, Quantity):
            raise EconomicModelError(f"{name} must be a Quantity")
        if q.known and q.value < 0:
            raise EconomicModelError(f"{name} is a cost/ratio and cannot be negative (got {q.value}): a negative cost would add edge out of nothing")
    ge = scn.gross_edge_bps
    if isinstance(ge, GrossEdge) and ge.hold_days is not None and ge.hold_days != hold:
        raise EconomicModelError(f"the funding edge was built for hold_days={ge.hold_days} ({ge.n_settlements} settlements) but the scenario holds {hold}")


def evaluate_chain(scn: Scenario) -> ChainResult:
    n = to_decimal(scn.notional_usd)
    if n <= 0 or not scn.legs:
        raise EconomicModelError("notional must be positive and at least one leg is required")
    _validate_scenario(scn)
    fees: List[Quantity] = []
    spreads: List[Quantity] = []
    slips: List[Quantity] = []
    blockers: List[str] = []
    for leg in scn.legs:
        f, sp, sl, b = _leg_costs(leg, n, scn)
        fees.append(f), spreads.append(sp), slips.append(sl), blockers.extend(b)
        if not leg.instrument.tradable:
            blockers.append(f"{leg.name}:NOT_TRADING")
    maker_legs = sum(1 for l in scn.legs if l.role == "maker")
    adverse = (scn.adverse_selection_bps * Decimal(maker_legs)) if maker_legs else Quantity(Decimal(0), "derived", "bps")
    p_fill = scn.maker_fill_probability
    if p_fill.known and not (Decimal(0) < p_fill.value <= Decimal(1)):
        raise EconomicModelError(f"maker_fill_probability must lie in (0, 1], got {p_fill.value}")
    if not maker_legs:
        maker_fill = Quantity(Decimal(0), "derived", "bps")
    elif p_fill.known and scn.gross_edge_bps.known:
        # every maker leg must fill (independently) for the trade to exist: the expected edge forgone is (1 - p^k) of the gross edge
        maker_fill = (Quantity(Decimal(1), "measured") - Quantity(p_fill.value ** maker_legs, p_fill.provenance)) * \
            Quantity(max(scn.gross_edge_bps.value, Decimal(0)), scn.gross_edge_bps.provenance, "bps")
    else:
        maker_fill = unknown("bps")
    hold_frac = to_decimal(scn.hold_days) / Decimal(365) * BPS
    hedge_err = abs(Quantity(Decimal(1), "measured") - scn.hedge_ratio) * scn.adverse_move_bps
    comps: Dict[str, Quantity] = {
        "fees": qsum(fees, "bps"), "spread": qsum(spreads, "bps"), "slippage": qsum(slips, "bps"),
        "adverse_selection": adverse, "maker_fill": maker_fill, "impact": scn.impact_bps, "basis": scn.basis_cost_bps,
        "borrow": scn.borrow_apr * hold_frac, "opportunity_cost": scn.opportunity_cost_apr * hold_frac,
        "hedge_error": hedge_err,
    }
    executable = not blockers
    theo = scn.gross_edge_bps
    exe = (theo - comps["fees"] - comps["spread"] - comps["slippage"]) if executable else unknown("bps")
    cap = exe
    for k in ("adverse_selection", "maker_fill", "impact", "basis", "borrow", "opportunity_cost", "hedge_error"):
        cap = cap - comps[k]
    stages = {"THEORETICAL": theo, "EXECUTABLE": exe, "CAPTURABLE": cap, "REALISED": unknown("bps")}
    util = (Quantity(n, "measured", "usd") / scn.margin_usd) if scn.margin_usd.known else unknown("ratio")
    inputs = {"gross_edge": scn.gross_edge_bps, "hedge_ratio": scn.hedge_ratio, "adverse_move": scn.adverse_move_bps}
    if maker_legs:
        inputs["maker_fill_probability"] = p_fill
    return ChainResult(comps, stages, executable, tuple(blockers), util, digest((scn,)), inputs)


def round_trip_fee_bps(rates: Sequence[Quantity]) -> Quantity:
    """Entry + exit for every leg: 2 * sum(rate) in bps."""
    return qsum(list(rates)) * Decimal(2) * BPS


def required_funding_bps_per_settlement(hurdle_pct: Any, hold_days: Any, round_trip_bps: Any, reserve: Any,
                                        settlements_per_day: Any) -> Decimal:
    """Average funding (bps per settlement) at which the annualised return on FULLY ALLOCATED capital equals the hurdle."""
    h, d, c, r, s = (to_decimal(x) for x in (hurdle_pct, hold_days, round_trip_bps, reserve, settlements_per_day))
    need_net = h / Decimal(100) * (Decimal(1) + r) * d / Decimal(365) * BPS
    return (c + need_net) / (s * d)


def breakeven_funding_bps(settlements: Any, round_trip_bps: Any, borrow_bps_per_day: Any = 0,
                          settlements_per_day: Any = 3) -> Decimal:
    n = to_decimal(settlements)
    if n <= 0:
        raise EconomicModelError("settlements must be positive")
    return to_decimal(round_trip_bps) / n + to_decimal(borrow_bps_per_day) / to_decimal(settlements_per_day)


def basis_bps(perp: Any, spot: Any) -> Decimal:
    p, s = to_decimal(perp), to_decimal(spot)
    if p <= 0 or s <= 0:
        raise EconomicModelError("basis undefined for non-positive price")
    return (p / s - 1) * BPS


# --------------------------------------------------------------------------------------------------------------------------
# Capacity
# --------------------------------------------------------------------------------------------------------------------------
def _with_notional(scn: Scenario, n: Decimal) -> Scenario:
    return dataclasses.replace(scn, notional_usd=n)


def depth_limit_usd(scn: Scenario) -> Optional[Decimal]:
    """Largest notional every taker leg's book can absorb on BOTH the entry and the exit side."""
    best: Optional[Decimal] = None
    for leg in scn.legs:
        if leg.role != "taker":
            continue
        if leg.book is None or leg.book.problems():
            return None
        lim = min(leg.book.total_qty("buy"), leg.book.total_qty("sell")) * leg.book.mid
        best = lim if best is None else min(best, lim)
    return best


def capacity_usd(scn: Scenario, floor_bps: Any) -> Quantity:
    """Max notional such that CAPTURABLE net edge >= floor_bps. Unknown if any cost input is unknown; 0 if even the smallest order fails."""
    floor = to_decimal(floor_bps)
    hi = depth_limit_usd(scn)
    if hi is None:
        return unknown("usd")
    probe = evaluate_chain(_with_notional(scn, hi))
    if probe.unknown_inputs and any(k not in ("spread", "slippage") for k in probe.unknown_inputs):
        return unknown("usd")
    lo_n = max(max(l.instrument.min_notional, l.instrument.min_qty * l.book.mid) for l in scn.legs if l.book is not None) + Decimal(1)

    def ok(n: Decimal) -> Optional[bool]:
        r = evaluate_chain(_with_notional(scn, n))
        if not r.executable:
            return False
        return at_least(r.net_edge_bps, floor)

    first = ok(lo_n)
    if first is None:
        return unknown("usd")
    prov = weakest(*(q.provenance for q in probe.components.values() if q.known), "derived")
    if not first:
        return Quantity(Decimal(0), prov, "usd")
    if ok(hi):
        return Quantity(hi.quantize(Decimal("0.01"), rounding=ROUND_DOWN), prov, "usd")
    lo, up = lo_n, hi
    for _ in range(60):
        mid = (lo + up) / 2
        if ok(mid):
            lo = mid
        else:
            up = mid
    return Quantity(lo.quantize(Decimal("0.01"), rounding=ROUND_DOWN), prov, "usd")


# --------------------------------------------------------------------------------------------------------------------------
# Realised: fills, hedge, owner export, reported profit
# --------------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Fill:
    fill_id: str
    symbol: str
    side: str                      # 'buy' | 'sell'
    qty: Decimal
    price: Decimal
    fee: Decimal                   # >= 0 cost (negative = rebate), in fee_ccy
    fee_ccy: str                   # quote coin or the symbol's base coin (anything else is refused in realised_result)
    ts_ms: int
    category: str = "linear"       # 'spot' | 'linear': the SAME symbol string is two different positions on two legs; never netted together

    def __post_init__(self) -> None:
        if self.category not in ("spot", "linear"):
            raise EconomicModelError(f"fill category must be 'spot' or 'linear', got {self.category!r}")
        if not isinstance(self.fill_id, str) or not self.fill_id or not isinstance(self.symbol, str) or not self.symbol:
            raise EconomicModelError("a fill needs a fill_id and a symbol")
        if self.side not in ("buy", "sell"):
            raise EconomicModelError(f"fill side must be 'buy' or 'sell', got {self.side!r}")
        for name in ("qty", "price", "fee"):
            v = getattr(self, name)
            if not isinstance(v, Decimal) or not v.is_finite():
                raise EconomicModelError(f"fill {name} must be a finite Decimal (floats are refused), got {v!r}")
        if self.qty <= 0 or self.price <= 0:
            raise EconomicModelError("fill qty and price must be positive")
        if not isinstance(self.fee_ccy, str) or not self.fee_ccy or isinstance(self.ts_ms, bool) or not isinstance(self.ts_ms, int):
            raise EconomicModelError("a fill needs a fee_ccy and an integer ts_ms")


@dataclass(frozen=True)
class FillRecon:
    ok: bool
    flags: Tuple[str, ...]
    duplicates: Tuple[str, ...]
    missing: Tuple[str, ...]


def reconcile_fills(fills: Sequence[Fill], expected_ids: Sequence[str]) -> FillRecon:
    ids = [f.fill_id for f in fills]
    dups = tuple(sorted({i for i in ids if ids.count(i) > 1}))
    missing = tuple(sorted(set(expected_ids) - set(ids)))
    unexpected = set(ids) - set(expected_ids)
    flags = []
    if dups:
        flags.append("DUPLICATE_FILL")
    if missing:
        flags.append("MISSING_FILL")
    if unexpected:
        flags.append("UNEXPECTED_FILL")
    return FillRecon(not flags, tuple(flags), dups, missing)


@dataclass(frozen=True)
class HedgeCheck:
    neutral: bool
    ratio: Optional[Decimal]
    residual_qty: Decimal


def hedge_check(held_long_qty: Decimal, short_qty: Decimal, tolerance: Any = "0.01") -> HedgeCheck:
    held, short = to_decimal(held_long_qty), to_decimal(short_qty)
    if short <= 0:
        return HedgeCheck(False, None, held - short)
    ratio = held / short
    return HedgeCheck(abs(Decimal(1) - ratio) <= to_decimal(tolerance), ratio, held - short)


EXPORT_SECTIONS = ("fills", "settlements")


@dataclass(frozen=True)
class ExportCheck:
    complete: bool
    problems: Tuple[str, ...]


def check_owner_export(export: Mapping[str, Any], window_start_ms: int, window_end_ms: int,
                       required: Sequence[str] = EXPORT_SECTIONS) -> ExportCheck:
    p: List[str] = []
    for s in required:
        v = export.get(s)
        if not isinstance(v, (list, tuple)):
            p.append(f"SECTION_MISSING:{s}")
        elif export.get(f"{s}_declared_rows") is not None and export[f"{s}_declared_rows"] != len(v):
            p.append(f"ROW_COUNT_MISMATCH:{s}")
    if export.get("truncated") is not False:
        p.append("TRUNCATION_NOT_RULED_OUT")
    cf, ct = export.get("covers_from_ms"), export.get("covers_to_ms")
    if not isinstance(cf, int) or not isinstance(ct, int) or cf > window_start_ms or ct < window_end_ms:
        p.append("WINDOW_NOT_COVERED")
    sha = export.get("sha256")
    if not (isinstance(sha, str) and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)):
        p.append("NO_EXPORT_HASH")
    return ExportCheck(not p, tuple(p))


MAX_MARK_AGE_MS = 60_000     # a mark older than this (or from the future) relative to ``as_of_ms`` is not a price for today's inventory


@dataclass(frozen=True)
class Mark:
    """A price with WHEN and WHERE it was observed. A bare Decimal mark carries neither, so its unrealised value is only ``assumed``."""
    price: Decimal
    ts_ms: int
    source: str                    # e.g. 'venue_mark', 'venue_last', 'index'

    def __post_init__(self) -> None:
        if not isinstance(self.price, Decimal) or not self.price.is_finite() or self.price <= 0:
            raise EconomicModelError("a mark price must be a positive finite Decimal")
        if isinstance(self.ts_ms, bool) or not isinstance(self.ts_ms, int):
            raise EconomicModelError("a mark needs an integer ts_ms")
        if not isinstance(self.source, str) or not self.source.strip():
            raise EconomicModelError("a mark needs a source")


@dataclass(frozen=True)
class RealisedResult:
    """``pnl_usd`` is REALISED only: closed-trade P&L (average cost) + funding received - fees. Open inventory's mark-to-market is ``unrealised_usd``,
    reported separately and never added to ``pnl_usd``."""
    pnl_usd: Quantity
    flags: Tuple[str, ...]
    components: Mapping[str, Quantity]
    unrealised_usd: Quantity = field(default_factory=lambda: unknown("usd"))
    open_inventory: Mapping[str, Decimal] = field(default_factory=dict)


def _held_settlements(perp_fills: Sequence[Fill], funding: FundingRecon, as_of_ms: Optional[int]) -> set:
    """Every settlement timestamp (on the instrument's own grid) at which the fill-derived linear position was non-zero. A position open at the end of the
    fills is held up to the latest of the reconciled window's end and ``as_of_ms``."""
    iv = funding.interval_ms
    assert iv
    pos = Decimal(0)
    spans: List[Tuple[int, Optional[int]]] = []
    start: Optional[int] = None
    for f in perp_fills:
        pos += (Decimal(1) if f.side == "buy" else Decimal(-1)) * f.qty
        if pos != 0 and start is None:
            start = f.ts_ms
        elif pos == 0 and start is not None:
            spans.append((start, f.ts_ms))
            start = None
    if start is not None:
        spans.append((start, None))
    end_open = max([funding.window[1] if funding.window else 0] + ([as_of_ms] if as_of_ms is not None else []))
    held = set()
    for s, e in spans:
        hi = end_open if e is None else e - 1               # position is zero AT the closing fill's timestamp (same rule as the position cross-check)
        held.update(range(((s + iv - 1) // iv) * iv, hi + 1, iv))
    return held


def realised_result(*, fills: Sequence[Fill], expected_fill_ids: Sequence[str], funding: FundingRecon,
                    venue_net_qty: Mapping[str, Optional[Decimal]], marks: Mapping[str, Any],
                    export: Optional[ExportCheck] = None, quote_coin: str = "USDT", base_coins: Optional[Mapping[str, str]] = None,
                    hedge: Optional[HedgeCheck] = None, claim_hedged: bool = False, as_of_ms: Optional[int] = None,
                    max_mark_age_ms: int = MAX_MARK_AGE_MS) -> RealisedResult:
    """REALISED PnL in quote currency. UNKNOWN (never a number) when the fills, funding, export, inventory read, a fee currency or a mark is not whole.
    Inventory is the VENUE's net position read; fill arithmetic that disagrees with it is a flag, not an inference. Open inventory is marked to market into
    ``unrealised_usd`` (a ``Mark`` with a timestamp and source, fresh against ``as_of_ms``; a bare Decimal is only 'assumed'). ``claim_hedged`` demands a
    neutral ``hedge`` check. Funding must be backed by fills: rows with no fills, or whose position differs from the fill-derived position at that
    settlement, are flagged."""
    flags: List[str] = []
    bases = dict(base_coins or {})
    if export is not None and not export.complete:
        flags.append("OWNER_EXPORT_INCOMPLETE")
    fr = reconcile_fills(fills, expected_fill_ids)
    flags.extend(fr.flags)
    flags.extend(f"FUNDING:{f}" for f in funding.flags)
    if claim_hedged:
        if hedge is None:
            flags.append("HEDGE_CHECK_MISSING")
        elif not hedge.neutral:
            flags.append("HEDGE_NOT_NEUTRAL")
    # fees, in quote currency; a fee in any other currency is never multiplied by a price on faith
    fees = Decimal(0)
    for f in fills:
        if f.fee_ccy == quote_coin:
            fees += f.fee
        elif bases.get(f.symbol) == f.fee_ccy:
            fees += f.fee * f.price
        else:
            flags.append(f"FEE_CCY_UNSUPPORTED:{f.symbol}:{f.fee_ccy}")
    # average-cost accounting per (symbol, category): a spot BTCUSDT leg and a linear BTCUSDT leg are different positions and never net against each other
    cats: Dict[str, set] = {}
    for f in fills:
        cats.setdefault(f.symbol, set()).add(f.category)

    def keyof(sym: str, cat: str):
        return sym if len(cats[sym]) == 1 else (sym, cat)

    def label(k) -> str:
        return k if isinstance(k, str) else f"{k[0]}:{k[1]}"

    def lookup(m: Mapping[Any, Any], k):
        if isinstance(k, tuple):
            return m.get(k)
        return m.get(k) if k in m else m.get((k, next(iter(cats[k]))))

    net: Dict[Any, Decimal] = {}
    cost: Dict[Any, Decimal] = {}              # signed cost basis of the open position (position * average price)
    realised_trading = Decimal(0)
    for f in sorted(fills, key=lambda x: x.ts_ms):
        k = keyof(f.symbol, f.category)
        sgn = Decimal(1) if f.side == "buy" else Decimal(-1)
        pos, basis = net.get(k, Decimal(0)), cost.get(k, Decimal(0))
        if pos == 0 or (pos > 0) == (sgn > 0):
            pos, basis = pos + sgn * f.qty, basis + sgn * f.qty * f.price
        else:
            closing = min(f.qty, abs(pos))
            avg = basis / pos
            realised_trading += (f.price - avg) * closing * (Decimal(1) if pos > 0 else Decimal(-1))
            pos2 = pos + sgn * closing
            basis = avg * pos2
            rest = f.qty - closing
            pos = pos2
            if rest > 0:
                pos, basis = sgn * rest, sgn * rest * f.price
        net[k], cost[k] = pos, basis
    for sym, cs in cats.items():
        if len(cs) > 1 and sum(1 for c in cs if net.get((sym, c), Decimal(0)) != 0) > 1:
            flags.append(f"OPEN_LEGS_NOT_REALISED:{sym}")              # two open legs are open inventory (unrealised, per leg), never a realised hedge profit
    # funding must be backed by fills, and must cover every settlement while the (linear) position was open
    if funding.rows > 0:
        if not fills:
            flags.append("FUNDING_WITHOUT_FILLS")
        elif funding.symbol is None or len(funding.positions) != funding.rows:
            flags.append("FUNDING_POSITIONS_UNVERIFIED")
        else:
            perp_fills = sorted((f for f in fills if f.symbol == funding.symbol and f.category == "linear"), key=lambda x: x.ts_ms)
            for ts, claimed in funding.positions:
                derived = sum((((Decimal(1) if f.side == "buy" else Decimal(-1)) * f.qty) for f in perp_fills if f.ts_ms <= ts), Decimal(0))
                if derived != claimed:
                    flags.append(f"FUNDING_POSITION_MISMATCH:{funding.symbol}")
                    break
            if funding.window is None or funding.interval_ms is None:
                flags.append("FUNDING_WINDOW_UNVERIFIED")
            else:
                held = _held_settlements(perp_fills, funding, as_of_ms)
                have = set(funding.settlement_ts)
                if any(t not in have for t in held):
                    flags.append("FUNDING_SETTLEMENT_UNCOVERED")
                if any(t not in held for t in have):
                    flags.append("FUNDING_ROW_OUTSIDE_POSITION")
    # inventory is the venue's read PER (symbol, category); every open lot is marked with a stamped, fresh mark
    open_inv: Dict[Any, Decimal] = {}
    unreal = Decimal(0)
    unreal_prov = "measured"
    for k, pos in net.items():
        venue = lookup(venue_net_qty, k)
        if venue is None:
            flags.append(f"VENUE_POSITION_READ_MISSING:{label(k)}")
            continue
        if venue != pos:
            flags.append(f"INVENTORY_MISMATCH:{label(k)}")
            continue
        if venue == 0:
            continue
        open_inv[k] = venue
        mk = lookup(marks, k)
        if isinstance(mk, Mark):
            if as_of_ms is None:
                unreal_prov = weakest(unreal_prov, "modelled")           # stamped, but nothing to judge its age against
            elif mk.ts_ms > as_of_ms or as_of_ms - mk.ts_ms > max_mark_age_ms:
                flags.append(f"STALE_MARK:{label(k)}")
                continue
            price = mk.price
        elif mk is not None and mk > 0:
            price = mk
            unreal_prov = weakest(unreal_prov, "assumed")                # an unstamped number: no time, no source
        else:
            flags.append(f"UNPRICED_INVENTORY:{label(k)}")
            continue
        unreal += venue * price - cost[k]
    comps = {"realised_trading": Quantity(realised_trading, "derived", "usd"), "fees": Quantity(fees, "measured", "usd"), "funding": funding.total}
    if flags:
        return RealisedResult(unknown("usd"), tuple(sorted(set(flags))), comps, unknown("usd"), open_inv)
    total = comps["realised_trading"] - comps["fees"] + comps["funding"]
    return RealisedResult(total, (), comps, Quantity(unreal, unreal_prov, "usd"), open_inv)


@dataclass(frozen=True)
class ProfitAudit:
    status: str        # CONFIRMED | INCOMPLETE_NO_COMPONENTS | PHANTOM_UNKNOWN_COMPONENTS | UNOBSERVED_COMPONENTS | MISMATCH
    detail: Tuple[str, ...]


MAX_AUDIT_TOLERANCE = Decimal("0.05")      # quote-currency units (rounding dust); a wider tolerance would let a reported number confirm itself


def audit_reported_profit(reported: Any, components: Mapping[str, Quantity], tolerance: Any = "0.01") -> ProfitAudit:
    """A reported profit is only CONFIRMED when at least one component exists, every component is known AND observed, and they sum to it.
    ``tolerance`` must lie in [0, MAX_AUDIT_TOLERANCE]; anything else is refused (not clamped)."""
    tol = to_decimal(tolerance)
    if tol < 0 or tol > MAX_AUDIT_TOLERANCE:
        raise EconomicModelError(f"audit tolerance {tol} outside [0, {MAX_AUDIT_TOLERANCE}]")
    rep_d = to_decimal(reported)
    if not isinstance(components, Mapping) or not all(isinstance(q, Quantity) for q in components.values()):
        raise EconomicModelError("components must be a mapping of Quantity")
    if not components:
        return ProfitAudit("INCOMPLETE_NO_COMPONENTS", ("no components were checked",))
    unk = tuple(sorted(k for k, q in components.items() if not q.known))
    if unk:
        return ProfitAudit("PHANTOM_UNKNOWN_COMPONENTS", unk)
    soft = tuple(sorted(k for k, q in components.items() if not q.observed))
    if soft:
        return ProfitAudit("UNOBSERVED_COMPONENTS", soft)
    total = sum((q.value for q in components.values()), Decimal(0))
    if abs(total - rep_d) > tol:
        return ProfitAudit("MISMATCH", (f"sum={total}", f"reported={rep_d}"))
    return ProfitAudit("CONFIRMED", ())


# --------------------------------------------------------------------------------------------------------------------------
# Evidence builders for tools/strategy_verdict.py (stages cost_model and capacity_test)
# --------------------------------------------------------------------------------------------------------------------------
def _sv():
    try:
        from tools import strategy_verdict as sv          # type: ignore
    except ImportError:                                    # bot/tools on sys.path instead of bot/
        import strategy_verdict as sv                      # type: ignore
    return sv


def _prov_label(qs: Sequence[Quantity]) -> str:
    known = [q.provenance for q in qs if q.known]
    return "economic_model:weakest=" + (weakest(*known) if known else "unknown")


_HEX64 = re.compile(r"[0-9a-f]{64}", re.ASCII)


def _input_hash(data_sha256: Any, own: Optional[str] = None) -> str:
    """The hash of the caller's REAL input data. Required; never defaulted to the scenario's own digest (that would certify itself)."""
    if not isinstance(data_sha256, str) or not _HEX64.fullmatch(data_sha256):
        raise EconomicModelError("data_sha256 is required: the lowercase SHA-256 of the caller's real input data")
    if own is not None and data_sha256 == own:
        raise EconomicModelError("data_sha256 equals the scenario's own digest: evidence must be bound to the input data, not to itself")
    return data_sha256


def _stamp(spec: Mapping[str, Any]) -> Dict[str, str]:
    """candidate_id + spec_digest of the exact spec this evidence is about; ``strategy_verdict.counted`` refuses a PASS that does not match its spec."""
    sv = _sv()
    if not isinstance(spec, Mapping) or not isinstance(spec.get("candidate_id"), str) or not spec["candidate_id"]:
        raise EconomicModelError("evidence must be built against a candidate spec with a candidate_id")
    return {"candidate_id": spec["candidate_id"], "spec_digest": sv.spec_digest(spec)}


COST_CHAIN_SPEC_KEYS = ("fee_model", "slippage_model", "funding_model", "capacity_model", "execution_model", "holding_period")


def cost_chain_digest(spec: Mapping[str, Any]) -> str:
    """PUBLIC, STABLE: the SHA-256 of the spec's cost-chain declarations (``COST_CHAIN_SPEC_KEYS``, canonical JSON; an absent key counts as null). Both
    ``cost_model_evidence`` and ``capacity_evidence`` stamp it as ``metrics['chain_digest']``; the verdict engine recomputes it from the spec it holds and
    refuses evidence whose digest differs, so evidence cannot be carried over to a spec whose costs were edited. Prose fields do not move it."""
    if not isinstance(spec, Mapping):
        raise EconomicModelError("cost_chain_digest needs a candidate spec mapping")
    return digest({k: spec.get(k) for k in COST_CHAIN_SPEC_KEYS})


def cost_model_evidence(chain: ChainResult, *, spec: Mapping[str, Any], data_sha256: Optional[str] = None, recorded_ms: int = 0):
    """PASS only with zero unknown and zero ASSUMED cost inputs and a positive net edge (an assumed input is ALWAYS unknown for a PASS: its number is in
    the metrics, never in the result). Unknowns -> INCONCLUSIVE; a fully known non-positive edge -> FAIL (killing is easy).
    ``data_sha256`` is the caller's real input-data hash and is required."""
    sha = _input_hash(data_sha256, chain.digest)
    stamp = _stamp(spec)
    sv = _sv()
    unk = list(chain.unknown_inputs)
    if not chain.executable:
        unk.append("EXECUTABLE")
    if not chain.stages["THEORETICAL"].known:
        unk.append("gross_edge")
    unk = sorted(set(unk))
    assumed = list(chain.assumed_inputs)
    edge = chain.net_edge_bps
    if unk or not edge.known:
        result, count, edge_v = sv.INCONCLUSIVE, max(len(unk), 1), None
    elif edge.value <= 0:
        result, count, edge_v = sv.FAIL, 0, float(edge.value)
    elif assumed:
        result, count, edge_v = sv.INCONCLUSIVE, len(assumed), float(edge.value)
    else:
        result, count, edge_v = sv.PASS, 0, float(edge.value)
    metrics = {"chain_digest": cost_chain_digest(spec), "scenario_digest": chain.digest, "unknown_cost_inputs": count, "net_edge_bps_after_costs": edge_v,
               "unknown_names": unk, "assumed_names": assumed, "executable": chain.executable, "blockers": list(chain.blockers),
               "components_bps": {k: (None if not q.known else str(q.value)) for k, q in chain.components.items()}}
    return sv.EvidenceItem(stage="cost_model", result=result, provenance=_prov_label(list(chain.components.values())),
                           metrics=metrics, data_sha256=sha, recorded_ms=recorded_ms, **stamp)


def capacity_evidence(capacity: Quantity, *, spec: Mapping[str, Any], floor_bps: Any, min_viable_capacity_usd: Any, data_sha256: Optional[str] = None,
                      recorded_ms: int = 0):
    sha = _input_hash(data_sha256)
    stamp = _stamp(spec)
    sv = _sv()
    need = to_decimal(min_viable_capacity_usd)
    metrics: Dict[str, Any] = {"chain_digest": cost_chain_digest(spec), "capacity_usd": as_float(capacity), "edge_floor_bps": float(to_decimal(floor_bps)),
                               "min_viable_capacity_usd": float(need)}
    if not capacity.known:
        result = sv.INCONCLUSIVE
    elif capacity.value < need:
        result = sv.FAIL
    elif capacity.provenance == "assumed":
        result = sv.INCONCLUSIVE
    else:
        result = sv.PASS
    return sv.EvidenceItem(stage="capacity_test", result=result, provenance=_prov_label([capacity]), metrics=metrics,
                           data_sha256=sha, recorded_ms=recorded_ms, **stamp)
