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

It never changes ``carry_costs`` / ``ledger`` / ``funding_reconcile`` / ``carry_broker``; tests cross-check against them.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
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


def to_decimal(x: Any) -> Decimal:
    if isinstance(x, bool) or isinstance(x, float):
        raise EconomicModelError(f"floats/bools are refused for exact arithmetic: {x!r} (pass a str, int or Decimal)")
    try:
        d = Decimal(x) if isinstance(x, (int, Decimal)) else Decimal(str(x).strip())
    except Exception as exc:
        raise EconomicModelError(f"not a decimal: {x!r}") from exc
    if not d.is_finite():
        raise EconomicModelError(f"non-finite: {x!r}")
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


def carry_gross_edge(perp: Instrument, expected_rate: Quantity, n_settlements: int, *, short: bool = True) -> Quantity:
    """THEORETICAL edge in bps of notional: sign * rate * n. The rate must lie inside the instrument's own cap/floor, else UNKNOWN."""
    if perp.category != "linear":
        raise EconomicModelError("funding edge needs a perpetual")
    if n_settlements <= 0:
        raise EconomicModelError("n_settlements must be positive")
    if not expected_rate.known:
        return unknown("bps")
    r = expected_rate.value
    if r > perp.funding_cap or r < perp.funding_floor:
        return unknown("bps")                              # outside the venue's own bounds: a unit slip, not an edge
    sign = Decimal(1) if short else Decimal(-1)
    return expected_rate * sign * BPS * Decimal(n_settlements) * Quantity(Decimal(1), "measured", "bps")


@dataclass(frozen=True)
class Settlement:
    ts_ms: int
    symbol: str
    rate: Decimal                  # per-settlement fraction as the venue printed it
    position_qty: Decimal          # signed: < 0 short
    mark: Optional[Decimal]
    cash_usd: Decimal              # reported by the venue/export: + received


MAGNITUDE_BRACKET = Decimal(2)      # same bracket funding_reconcile uses; a x10 slip or a cumulative field falls outside it


@dataclass(frozen=True)
class FundingRecon:
    total: Quantity
    flags: Tuple[str, ...]
    missing_ts: Tuple[int, ...]
    rows: int


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
    for r in rows:
        if r.symbol != perp.symbol:
            flags.append("WRONG_SYMBOL_ROW")
        if r.rate > perp.funding_cap or r.rate < perp.funding_floor:
            flags.append("RATE_OUTSIDE_VENUE_BOUNDS")
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
    flags_t = tuple(sorted(set(flags)))
    if flags_t:
        return FundingRecon(unknown("usd"), flags_t, missing, len(rows))
    return FundingRecon(Quantity(sum((r.cash_usd for r in rows), Decimal(0)), "measured", "usd"), (), (), len(rows))


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


@dataclass(frozen=True)
class ChainResult:
    components: Mapping[str, Quantity]
    stages: Mapping[str, Quantity]
    executable: bool
    blockers: Tuple[str, ...]
    capital_utilisation: Quantity          # notional / margin
    digest: str

    @property
    def unknown_inputs(self) -> Tuple[str, ...]:
        return tuple(sorted(k for k, q in self.components.items() if not q.known))

    @property
    def assumed_inputs(self) -> Tuple[str, ...]:
        return tuple(sorted(k for k, q in self.components.items() if q.provenance == "assumed"))

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
    if leg.role == "maker":                                  # no crossing; the price of not crossing is adverse selection
        return fee, Quantity(Decimal(0), "derived", "bps"), Quantity(Decimal(0), "derived", "bps"), blockers
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


def evaluate_chain(scn: Scenario) -> ChainResult:
    n = to_decimal(scn.notional_usd)
    if n <= 0 or not scn.legs:
        raise EconomicModelError("notional must be positive and at least one leg is required")
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
    hold_frac = to_decimal(scn.hold_days) / Decimal(365) * BPS
    hedge_err = abs(Quantity(Decimal(1), "measured") - scn.hedge_ratio) * scn.adverse_move_bps
    comps: Dict[str, Quantity] = {
        "fees": qsum(fees, "bps"), "spread": qsum(spreads, "bps"), "slippage": qsum(slips, "bps"),
        "adverse_selection": adverse, "impact": scn.impact_bps, "basis": scn.basis_cost_bps,
        "borrow": scn.borrow_apr * hold_frac, "opportunity_cost": scn.opportunity_cost_apr * hold_frac,
        "hedge_error": hedge_err,
    }
    executable = not blockers
    theo = scn.gross_edge_bps
    exe = (theo - comps["fees"] - comps["spread"] - comps["slippage"]) if executable else unknown("bps")
    cap = exe
    for k in ("adverse_selection", "impact", "basis", "borrow", "opportunity_cost", "hedge_error"):
        cap = cap - comps[k]
    stages = {"THEORETICAL": theo, "EXECUTABLE": exe, "CAPTURABLE": cap, "REALISED": unknown("bps")}
    util = (Quantity(n, "measured", "usd") / scn.margin_usd) if scn.margin_usd.known else unknown("ratio")
    return ChainResult(comps, stages, executable, tuple(blockers), util, digest((scn,)))


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
    fee_ccy: str                   # quote coin or base coin
    ts_ms: int


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


@dataclass(frozen=True)
class RealisedResult:
    pnl_usd: Quantity
    flags: Tuple[str, ...]
    components: Mapping[str, Quantity]


def realised_result(*, fills: Sequence[Fill], expected_fill_ids: Sequence[str], funding: FundingRecon,
                    venue_net_qty: Mapping[str, Optional[Decimal]], marks: Mapping[str, Optional[Decimal]],
                    export: Optional[ExportCheck] = None, quote_coin: str = "USDT") -> RealisedResult:
    """Realised PnL in quote currency. UNKNOWN (never a number) when the fills, funding, export, inventory read or a mark is not whole.
    Inventory is the VENUE's net position read; fill arithmetic that disagrees with it is a flag, not an inference."""
    flags: List[str] = []
    if export is not None and not export.complete:
        flags.append("OWNER_EXPORT_INCOMPLETE")
    fr = reconcile_fills(fills, expected_fill_ids)
    flags.extend(fr.flags)
    flags.extend(f"FUNDING:{f}" for f in funding.flags)
    cash = Decimal(0)
    fees = Decimal(0)
    net: Dict[str, Decimal] = {}
    for f in fills:
        sgn = Decimal(1) if f.side == "buy" else Decimal(-1)
        cash += -sgn * f.qty * f.price
        fees += f.fee if f.fee_ccy == quote_coin else f.fee * f.price
        net[f.symbol] = net.get(f.symbol, Decimal(0)) + sgn * f.qty
    inventory = Decimal(0)
    for sym, arith in net.items():
        venue = venue_net_qty.get(sym)
        if venue is None:
            flags.append(f"VENUE_POSITION_READ_MISSING:{sym}")
            continue
        if venue != arith:
            flags.append(f"INVENTORY_MISMATCH:{sym}")
            continue
        if venue != 0:
            mk = marks.get(sym)
            if mk is None or mk <= 0:
                flags.append(f"UNPRICED_INVENTORY:{sym}")
                continue
            inventory += venue * mk
    comps = {"trade_cash": Quantity(cash, "measured", "usd"), "fees": Quantity(fees, "measured", "usd"),
             "funding": funding.total, "inventory_value": Quantity(inventory, "measured", "usd")}
    if flags:
        return RealisedResult(unknown("usd"), tuple(sorted(set(flags))), comps)
    total = comps["trade_cash"] - comps["fees"] + comps["funding"] + comps["inventory_value"]
    return RealisedResult(total, (), comps)


@dataclass(frozen=True)
class ProfitAudit:
    status: str        # CONFIRMED | PHANTOM_UNKNOWN_COMPONENTS | UNOBSERVED_COMPONENTS | MISMATCH
    detail: Tuple[str, ...]


def audit_reported_profit(reported: Any, components: Mapping[str, Quantity], tolerance: Any = "0.01") -> ProfitAudit:
    """A reported profit is only CONFIRMED when every component is known AND observed and they sum to it."""
    unk = tuple(sorted(k for k, q in components.items() if not q.known))
    if unk:
        return ProfitAudit("PHANTOM_UNKNOWN_COMPONENTS", unk)
    soft = tuple(sorted(k for k, q in components.items() if not q.observed))
    if soft:
        return ProfitAudit("UNOBSERVED_COMPONENTS", soft)
    total = sum((q.value for q in components.values()), Decimal(0))
    if abs(total - to_decimal(reported)) > to_decimal(tolerance):
        return ProfitAudit("MISMATCH", (f"sum={total}", f"reported={to_decimal(reported)}"))
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


def cost_model_evidence(chain: ChainResult, *, recorded_ms: int = 0, data_sha256: Optional[str] = None,
                        assumed_counts_as_unknown: bool = True):
    """PASS only with zero unknown (and, strictly, zero ASSUMED) cost inputs and a positive net edge. Unknowns -> INCONCLUSIVE;
    a fully known non-positive edge -> FAIL (killing is easy)."""
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
    elif assumed and assumed_counts_as_unknown:
        result, count, edge_v = sv.INCONCLUSIVE, len(assumed), float(edge.value)
    else:
        result, count, edge_v = sv.PASS, 0, float(edge.value)
    metrics = {"unknown_cost_inputs": count, "net_edge_bps_after_costs": edge_v,
               "unknown_names": unk, "assumed_names": assumed, "executable": chain.executable, "blockers": list(chain.blockers),
               "components_bps": {k: (None if not q.known else str(q.value)) for k, q in chain.components.items()}}
    return sv.EvidenceItem(stage="cost_model", result=result, provenance=_prov_label(list(chain.components.values())),
                           metrics=metrics, data_sha256=data_sha256 or chain.digest, recorded_ms=recorded_ms)


def capacity_evidence(capacity: Quantity, *, floor_bps: Any, min_viable_capacity_usd: Any, data_sha256: str,
                      recorded_ms: int = 0, assumed_counts_as_unknown: bool = True):
    sv = _sv()
    need = to_decimal(min_viable_capacity_usd)
    metrics: Dict[str, Any] = {"capacity_usd": as_float(capacity), "edge_floor_bps": float(to_decimal(floor_bps)),
                               "min_viable_capacity_usd": float(need)}
    if not capacity.known:
        result = sv.INCONCLUSIVE
    elif capacity.value < need:
        result = sv.FAIL
    elif capacity.provenance == "assumed" and assumed_counts_as_unknown:
        result = sv.INCONCLUSIVE
    else:
        result = sv.PASS
    return sv.EvidenceItem(stage="capacity_test", result=result, provenance=_prov_label([capacity]), metrics=metrics,
                           data_sha256=data_sha256, recorded_ms=recorded_ms)
