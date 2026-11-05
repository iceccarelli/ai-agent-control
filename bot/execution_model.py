"""Taker vs maker vs hybrid execution for a delta-neutral carry position. Pure arithmetic, no I/O.

PHASE 5 QUESTION: does posting passive (post-only) orders on the entry and the exit of the hedge
beat crossing the spread, once non-fills, rejections, adverse selection and legging are charged?
This module compares three policies, for a short BTCUSDT linear perp against owned (OVERLAY) or
bought (ACQUIRE) spot:

  TAKER   cross the displayed book on every leg.
  MAKER   rest post-only at the touch, cancel at the timeout, never cross. A non-fill is charged.
  HYBRID  rest post-only at the touch; at the timeout (or on a post-only rejection) cancel and
          chase the unfilled remainder as a taker.

THREE EVIDENCE CLASSES, KEPT APART IN EVERY OUTPUT (top-level keys, never merged):

  PUBLIC_DISPLAYED_DEPTH  what the book DISPLAYED (md_economics.walk_leg / depth_costs). Not a fill.
  SIMULATED_FILL          what this MODEL says under stated parameters. Never reported as measured.
  ACCOUNT_EXECUTION       real fills of THIS account with fee records (`AccountExecutionRecord`).
                          NOTHING in this repository produces them. Without them the measured
                          section is NOT_AVAILABLE; with fewer than the stated minimum it is
                          INSUFFICIENT. Only `measure_account_execution` can say MEASURED.

PARAMETERS. Every uncertain input (fill probability, queue ahead, cancel rate, adverse selection,
latency, volatility, maker fee, penalty for a missed fill, ...) is a `Param` with provenance in
{MEASURED, ESTIMATED, UNKNOWN} and a source, or an `Assumption` inside an explicit `Assumptions`
object (provenance ASSUMED, with a rationale). ASSUMED values are echoed back in the output.
Nothing is defaulted: an UNKNOWN input makes every mode that needs it REFUSED, listing the
unknown set, and no cost number is produced for that mode. A MEASURED label on a model input is a
CLAIM the caller makes; the verdict never trusts it, only ACCOUNT_EXECUTION records count.

A TOUCH IS NOT A FILL. `simulate_maker_fill` fills a resting order only from trade volume that
printed AT or THROUGH its price after it joined the queue, and only beyond the volume ahead of it
(reduced by an ASSUMED fraction of the queue that cancels). A quote touching the price with no
volume through it fills nothing. Volume exactly equal to the queue ahead fills nothing.

CONVENTIONS (all costs are bps of position notional over the ROUND TRIP, cost positive = paid):
  * Reference is the decision-time mid. Taker leg = half spread + book-walk slippage (+ impact
    beyond displayed depth) + taker fee + latency charge. It reconciles with
    md_economics.depth_costs on the same book (spread + slippage are those numbers).
  * Maker leg, if filled = -half spread (captured) + maker fee (negative = rebate) + adverse
    selection (the conditional post-fill mark-to-market loss, bps).
  * ONE-SIDED ADVERSE MOVE. Over an interval t the price is driftless with sd = vol * sqrt(t). The
    cost of being exposed to it is charged as its ONE-SIDED expectation, E[max(Z,0)] * vol *
    sqrt(t) with E[max(Z,0)] = 1/sqrt(2*pi): favourable moves are NOT credited. That is a risk
    charge, deliberately conservative toward maker/hybrid, not a forecast of P&L. It prices
    (a) the taker's latency, (b) the DELAY of a chase (decision to chase = 2*latency + timeout,
    or 3*latency after a rejection), (c) LEGGING: a hedge leg in place while the other is not.
  * LEGGING window uses the worst case: a maker fill is taken to land at its arrival (latency),
    so the exposure to the leg still waiting is as long as it can be. Both hedge legs are
    treated as independent and each charged against the OUTRIGHT vol (for a spread trade this
    overstates the delay cost): never flattering to maker.
  * POST-ONLY REJECTION: an order priced off the decision-time touch is rejected if the market
    moved through it during the latency: P = Phi(-spread_bps / (vol * sqrt(latency))).

LIMITATIONS a reader must keep in mind. Not modelled: fee-tier changes, funding timing around a
settlement, repricing after a rejection, a maker order larger than its queue, retries of a failed
taker leg inside HYBRID/MAKER, correlation between the two legs' fills, the favourable half of a
move. The post-only rejection estimate is a continuous-diffusion one: against a one-tick spread it
saturates near 0.5, so supply `post_only_reject_probability` once it is observed (the verdict
floors it by the measured rejection rate in any case). Pure MAKER's cost hinges on the
`nonfill_penalty_bps` nobody measures, so it never decides the verdict; HYBRID, which always
establishes the position, does. Fill probability is all-or-nothing in the DIRECT model; partial
fills exist in the queue model and in `simulate_maker_fill`. A model that is silent is not a model
that is zero.

The break-even numbers answer "what would have to be true", not "what will happen".
"""
from __future__ import annotations

import itertools
import math
import random
from dataclasses import dataclass, field, replace
from statistics import NormalDist
from typing import (Any, Callable, ClassVar, Dict, Iterable, List, Mapping, Optional, Sequence,
                    Tuple)

import evidence_stats as _es
import md_economics as _me
import trade_economics as _te

SCHEMA = "execution_model/1"

PUBLIC_DISPLAYED_DEPTH = "PUBLIC_DISPLAYED_DEPTH"
SIMULATED_FILL = "SIMULATED_FILL"
ACCOUNT_EXECUTION = "ACCOUNT_EXECUTION"
EVIDENCE_CLASSES = (PUBLIC_DISPLAYED_DEPTH, SIMULATED_FILL, ACCOUNT_EXECUTION)

MEASURED, ESTIMATED, ASSUMED, UNKNOWN = "MEASURED", "ESTIMATED", "ASSUMED", "UNKNOWN"
PROVENANCES = (MEASURED, ESTIMATED, ASSUMED, UNKNOWN)

TAKER, MAKER, HYBRID = "TAKER", "MAKER", "HYBRID"
MODES = (TAKER, MAKER, HYBRID)

OVERLAY, ACQUIRE = _me.OVERLAY, _me.ACQUIRE
ENTRY, EXIT = "entry", "exit"

TAKER_ONLY = "TAKER_ONLY"
MAKER_SUPPORTED_BY_MEASURED_FILLS = "MAKER_SUPPORTED_BY_MEASURED_FILLS"
MAKER_NOT_SUPPORTED = "MAKER_NOT_SUPPORTED"
VERDICTS = (TAKER_ONLY, MAKER_SUPPORTED_BY_MEASURED_FILLS, MAKER_NOT_SUPPORTED)

NOT_AVAILABLE, INSUFFICIENT = "NOT_AVAILABLE", "INSUFFICIENT"
COMPUTED, REFUSED = "COMPUTED", "REFUSED"

#: What `measure_account_execution` insists on before it will say MEASURED. Stated, not tuned.
MIN_MAKER_ATTEMPTS = 30            # terminal post-only orders, filled or not (a miss is data)
MIN_MAKER_FILLS = 10               # fills needed for a realized cost / adverse-selection estimate
MIN_SPAN_MS = 24 * 3_600_000       # the attempts must span a day: fill rates are regime dependent
MIN_TAKER_FILLS = 10               # informational taker realized cost

DEFAULT_SEED = 20261105
DEFAULT_N_PATHS = 2000
MIN_N_PATHS = 100
PHI0 = 1.0 / math.sqrt(2.0 * math.pi)       # E[max(Z, 0)], Z ~ N(0, 1)
P_GRID = (0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
ADVERSE_GRID_BPS = (0.0, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0)

COMPONENTS = ("spread", "fees", "slippage", "impact", "adverse_selection", "delay", "legging",
              "unwind", "penalty")


# -- errors and small helpers ------------------------------------------------------------------------

class UnknownParameterError(ValueError):
    """A function that needs a number was handed an UNKNOWN one. Never defaulted."""

    def __init__(self, unknown: Sequence[str]):
        self.unknown = tuple(unknown)
        super().__init__("UNKNOWN parameters, refusing to default them: " + ", ".join(self.unknown))


class EvidenceClassError(ValueError):
    """An object of the wrong evidence class was offered where another is required."""


def _fin(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _pos(x: Any) -> bool:
    return _fin(x) and x > 0


def _nonneg(x: Any) -> bool:
    return _fin(x) and x >= 0


def _zero() -> Dict[str, float]:
    return {k: 0.0 for k in COMPONENTS}


# -- parameters with provenance ----------------------------------------------------------------------

@dataclass(frozen=True)
class Param:
    """A number with provenance. ASSUMED is not constructible here: assumed values go through an
    explicit `Assumptions` object so they cannot be mistaken for evidence. UNKNOWN carries no value."""
    value: Optional[float]
    provenance: str
    source: str = ""

    def __post_init__(self) -> None:
        if self.provenance not in PROVENANCES:
            raise ValueError(f"provenance {self.provenance!r} not in {PROVENANCES}")
        if self.provenance == ASSUMED:
            raise ValueError("ASSUMED values are supplied only through an explicit Assumptions object")
        if self.provenance == UNKNOWN:
            if self.value is not None:
                raise ValueError("an UNKNOWN parameter carries no value")
            return
        if not _fin(self.value):
            raise ValueError(f"{self.provenance} parameter needs a finite number, got {self.value!r}")
        if not (isinstance(self.source, str) and self.source.strip()):
            raise ValueError(f"{self.provenance} parameter needs a stated source: provenance without one is a claim")


def measured(value: float, source: str) -> Param:
    return Param(value, MEASURED, source)


def estimated(value: float, source: str) -> Param:
    return Param(value, ESTIMATED, source)


UNKNOWN_PARAM = Param(None, UNKNOWN)


@dataclass(frozen=True)
class Assumption:
    value: float
    rationale: str

    def __post_init__(self) -> None:
        if not _fin(self.value):
            raise ValueError("an assumption needs a finite number")
        if not (isinstance(self.rationale, str) and self.rationale.strip()):
            raise ValueError("an assumption needs a stated rationale")


@dataclass(frozen=True)
class ParamSpec:
    name: str
    lo: float
    hi: float
    unit: str
    meaning: str
    lo_open: bool = False


def _specs(*rows: ParamSpec) -> Dict[str, ParamSpec]:
    return {r.name: r for r in rows}


PARAM_SPECS: Mapping[str, ParamSpec] = _specs(
    ParamSpec("fill_probability", 0.0, 1.0, "probability",
              "P(a resting post-only order is completely filled within the timeout | accepted)"),
    ParamSpec("queue_ahead_btc", 0.0, 1e7, "BTC", "volume resting ahead of the order at its price on arrival"),
    ParamSpec("cancel_fraction_ahead", 0.0, 1.0, "fraction",
              "share of the volume ahead that cancels instead of trading (unobservable from public data)"),
    ParamSpec("through_volume_rate_btc_per_s", 0.0, 1e6, "BTC/s",
              "rate at which trade volume prints AT or THROUGH the order's price"),
    ParamSpec("mean_trade_size_btc", 0.0, 1e6, "BTC", "mean size of those prints", lo_open=True),
    ParamSpec("adverse_selection_bps", 0.0, 1000.0, "bps",
              "mean adverse mark-to-market of a maker order CONDITIONAL on it being filled"),
    ParamSpec("latency_ms", 0.0, 600_000.0, "ms", "decision-to-venue latency of one order action"),
    ParamSpec("vol_bps_per_sqrt_s", 0.0, 10_000.0, "bps per sqrt(second)", "sd of the price move over one second"),
    ParamSpec("maker_fee_bps_perp", -50.0, 100.0, "bps", "maker fee on the perp leg; NEGATIVE is a rebate"),
    ParamSpec("maker_fee_bps_spot", -50.0, 100.0, "bps", "maker fee on the spot leg; NEGATIVE is a rebate"),
    ParamSpec("post_only_reject_probability", 0.0, 1.0, "probability",
              "OPTIONAL: P(a post-only order is rejected on arrival); replaces the diffusion estimate when supplied"),
    ParamSpec("beyond_depth_impact_bps", 0.0, 10_000.0, "bps",
              "extra adverse price beyond the last displayed level for quantity the book does not display"),
    ParamSpec("taker_leg_fail_probability", 0.0, 1.0, "probability",
              "P(one taker hedge leg fails to fill on its first attempt), ACQUIRE only"),
    ParamSpec("nonfill_penalty_bps", 0.0, 10_000.0, "bps",
              "cost charged per unit of a phase a MAKER-only policy fails to establish (the missed edge)"),
)

_STRUCTURAL = ("queue_ahead_btc", "cancel_fraction_ahead", "through_volume_rate_btc_per_s", "mean_trade_size_btc")
_FILL_LIKE = ("fill_probability", "adverse_selection_bps") + _STRUCTURAL


@dataclass(frozen=True)
class Assumptions:
    """The ONLY way an ASSUMED value enters. Echoed back, with its rationale, in every output."""
    values: Mapping[str, Assumption] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, a in self.values.items():
            if name not in PARAM_SPECS:
                raise ValueError(f"assumption for unknown parameter {name!r}")
            if not isinstance(a, Assumption):
                raise TypeError(f"assumption {name!r} must be an Assumption")

    @staticmethod
    def of(**kw: Assumption) -> "Assumptions":
        return Assumptions(dict(kw))

    def echo(self) -> Dict[str, Dict[str, Any]]:
        return {n: {"value": a.value, "provenance": ASSUMED, "rationale": a.rationale}
                for n, a in sorted(self.values.items())}


def _check_range(name: str, v: float) -> None:
    s = PARAM_SPECS[name]
    ok = _fin(v) and (v > s.lo if s.lo_open else v >= s.lo) and v <= s.hi
    if not ok:
        raise ValueError(f"{name}={v!r} outside its valid range {'(' if s.lo_open else '['}{s.lo}, {s.hi}] {s.unit}")


@dataclass(frozen=True)
class ResolvedParams:
    values: Mapping[str, float]
    provenance: Mapping[str, str]
    source: Mapping[str, str]
    declared_unknown: Tuple[str, ...]

    def missing(self, names: Iterable[str]) -> List[str]:
        return [n for n in names if n not in self.values]

    def echo(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for n in sorted(self.values):
            row: Dict[str, Any] = {"value": self.values[n], "unit": PARAM_SPECS[n].unit}
            if self.provenance[n] == ASSUMED:
                row.update(provenance=ASSUMED, rationale=self.source[n])
            else:
                row.update(declared_provenance=self.provenance[n], source=self.source[n],
                           verified_by_account_records=False)
            out[n] = row
        for n in self.declared_unknown:
            out[n] = {"value": None, "provenance": UNKNOWN, "unit": PARAM_SPECS[n].unit}
        return out


def resolve_params(params: Optional[Mapping[str, Param]] = None,
                   assumptions: Optional[Assumptions] = None) -> ResolvedParams:
    """Merge supplied `Param`s and explicit `Assumptions`. A name given twice, an unknown name, a
    non-`Param`, or an out-of-range value raises: a typo must not silently become a missing input."""
    params = dict(params or {})
    assumptions = assumptions or Assumptions()
    values: Dict[str, float] = {}
    prov: Dict[str, str] = {}
    src: Dict[str, str] = {}
    unknown: List[str] = []
    for n, p in params.items():
        if n not in PARAM_SPECS:
            raise ValueError(f"unknown parameter name {n!r}")
        if not isinstance(p, Param):
            raise TypeError(f"parameter {n!r} must be a Param (value + provenance + source)")
        if n in assumptions.values:
            raise ValueError(f"{n!r} is both supplied as a Param and as an Assumption")
        if p.provenance == UNKNOWN:
            unknown.append(n)
            continue
        _check_range(n, p.value)
        values[n], prov[n], src[n] = float(p.value), p.provenance, p.source
    for n, a in assumptions.values.items():
        _check_range(n, a.value)
        values[n], prov[n], src[n] = float(a.value), ASSUMED, a.rationale
    return ResolvedParams(values, prov, src, tuple(sorted(unknown)))


def _scalar(x: Any, name: str, *, lo: float, hi: float = math.inf, lo_open: bool = False) -> Tuple[float, str]:
    """A bare number, a `Param` or an `Assumption`. UNKNOWN refuses; the provenance is returned."""
    prov = "UNLABELLED_CALLER_VALUE"
    if isinstance(x, Assumption):
        v, prov = x.value, ASSUMED
    elif isinstance(x, Param):
        if x.provenance == UNKNOWN:
            raise UnknownParameterError((name,))
        v, prov = x.value, x.provenance
    else:
        v = x
    if not (_fin(v) and (v > lo if lo_open else v >= lo) and v <= hi):
        raise ValueError(f"{name}={v!r} outside its valid range")
    return float(v), prov


# -- the maker fill primitive: a touch is not a fill ----------------------------------------------------

TRADE, QUOTE_TOUCH = "TRADE", "QUOTE_TOUCH"
FILLED, PARTIAL, NOT_FILLED, REJECTED_POST_ONLY = "FILLED", "PARTIAL", "NOT_FILLED", "REJECTED_POST_ONLY"


@dataclass(frozen=True)
class TradeEvent:
    """One event after the order joined the queue. `qty` is the volume that PRINTED. A quote touch
    (kind QUOTE_TOUCH) carries no volume and never fills anything. With `side`/`price` given to
    `simulate_maker_fill`, `price` and `aggressor` ("BUY"/"SELL" taker side) decide whether a print
    is at/through the order's price; without them the caller vouches the events are already at/through."""
    t_ms: float
    qty: float = 0.0
    price: Optional[float] = None
    aggressor: Optional[str] = None
    kind: str = TRADE

    @staticmethod
    def touch(t_ms: float, price: Optional[float] = None) -> "TradeEvent":
        return TradeEvent(t_ms, 0.0, price, None, QUOTE_TOUCH)


@dataclass(frozen=True)
class MakerFillResult:
    status: str                          # FILLED | PARTIAL | NOT_FILLED | REJECTED_POST_ONLY
    reason: str
    order_size: float
    filled_qty: float
    fills: Tuple[Tuple[float, float], ...]       # (t_ms, qty) increments
    first_fill_ms: Optional[float]
    complete_ms: Optional[float]
    queue_ahead: float
    cancel_fraction_ahead: float
    effective_queue_ahead: float
    volume_through: float                # volume that counted (at/through the price, inside the horizon)
    queue_cleared: bool
    touches_ignored: int
    events_ignored: int
    events_after_horizon: int
    provenance: Mapping[str, str]
    assumed_inputs: Tuple[str, ...] = ("cancel_fraction_ahead",)

    @property
    def evidence_class(self) -> str:
        return SIMULATED_FILL

    @property
    def fill_fraction(self) -> float:
        return self.filled_qty / self.order_size if self.order_size > 0 else 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {"evidence_class": SIMULATED_FILL, "is_measured": False, "status": self.status, "reason": self.reason,
                "order_size": self.order_size, "filled_qty": self.filled_qty, "fill_fraction": self.fill_fraction,
                "fills": [list(f) for f in self.fills], "first_fill_ms": self.first_fill_ms,
                "complete_ms": self.complete_ms, "queue_ahead": self.queue_ahead,
                "cancel_fraction_ahead": self.cancel_fraction_ahead,
                "effective_queue_ahead": self.effective_queue_ahead, "volume_through": self.volume_through,
                "queue_cleared": self.queue_cleared, "touches_ignored": self.touches_ignored,
                "events_ignored": self.events_ignored, "events_after_horizon": self.events_after_horizon,
                "input_provenance": dict(self.provenance), "assumed_inputs": list(self.assumed_inputs)}


def _as_event(e: Any) -> TradeEvent:
    if isinstance(e, TradeEvent):
        ev = e
    elif isinstance(e, (tuple, list)) and len(e) == 2:
        ev = TradeEvent(e[0], e[1])
    else:
        raise ValueError("a trade event is a TradeEvent or a (t_ms, qty) tuple")
    if not _nonneg(ev.t_ms):
        raise ValueError(f"event time must be a finite non-negative number, got {ev.t_ms!r}")
    if not _nonneg(ev.qty):
        raise ValueError(f"event qty must be a finite non-negative number, got {ev.qty!r}")
    if ev.kind not in (TRADE, QUOTE_TOUCH):
        raise ValueError(f"event kind {ev.kind!r}")
    if ev.price is not None and not _pos(ev.price):
        raise ValueError("event price must be positive")
    if ev.aggressor not in (None, "BUY", "SELL"):
        raise ValueError("aggressor must be BUY, SELL or None")
    return ev


def simulate_maker_fill(queue_ahead: Any, order_size: Any, trade_events: Iterable[Any],
                        cancel_fraction_ahead: Any, *, side: Optional[str] = None,
                        price: Optional[float] = None, horizon_ms: Optional[float] = None) -> MakerFillResult:
    """Fill a resting order from the volume that traded through its price. Output class SIMULATED_FILL.

    The order fills only after cumulative counted volume EXCEEDS ``queue_ahead * (1 -
    cancel_fraction_ahead)``; the excess fills it, up to ``order_size`` (partial fills accumulate).
    Volume equal to the queue fills nothing. Quote touches, wrong-side prints and prints worse than
    the order's price count for nothing. With ``side`` ("buy"/"sell") and ``price`` given, a print
    strictly THROUGH the price (a buy order, a trade below it) means the whole queue and the order
    were consumed and fills the remainder; a print AT the price counts as ordinary volume. Events
    later than ``horizon_ms`` (the timeout) are ignored. ``cancel_fraction_ahead`` is an ASSUMED
    quantity: an UNKNOWN one refuses. Events must be time-ordered."""
    q, qprov = _scalar(queue_ahead, "queue_ahead", lo=0.0)
    size, sprov = _scalar(order_size, "order_size", lo=0.0, lo_open=True)
    c, cprov = _scalar(cancel_fraction_ahead, "cancel_fraction_ahead", lo=0.0, hi=1.0)
    if (side is None) != (price is None):
        raise ValueError("side and price are given together or not at all")
    if side is not None:
        if side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        if not _pos(price):
            raise ValueError("price must be positive")
    if horizon_ms is not None and not _nonneg(horizon_ms):
        raise ValueError("horizon_ms must be a non-negative number")
    eff = q * (1.0 - c)
    cum, filled = 0.0, 0.0
    fills: List[Tuple[float, float]] = []
    touches = ignored = late = 0
    last_t = -1.0
    for raw in trade_events:
        ev = _as_event(raw)
        if ev.t_ms < last_t:
            raise ValueError("trade_events must be ordered by time")
        last_t = ev.t_ms
        if horizon_ms is not None and ev.t_ms > horizon_ms:
            late += 1
            continue
        if ev.kind == QUOTE_TOUCH:
            touches += 1
            continue
        if filled >= size:
            continue                              # complete: later events are validated above, not counted
        through = False
        if side is not None:
            if ev.price is None:
                ignored += 1                      # cannot prove it traded at/through: counts for nothing
                continue
            if side == "buy":
                ok = ev.price <= price and ev.aggressor in (None, "SELL")
                through = ev.price < price
            else:
                ok = ev.price >= price and ev.aggressor in (None, "BUY")
                through = ev.price > price
            if not ok:
                ignored += 1
                continue
        if ev.qty <= 0.0:
            continue
        if through:
            take = size - filled
            cum = max(cum, eff) + ev.qty
        else:
            before = cum
            cum += ev.qty
            take = min(size - filled, max(0.0, cum - max(before, eff)))
        if take > 0.0:
            filled += take
            fills.append((ev.t_ms, take))
        if filled >= size - 1e-12 * max(1.0, size):
            filled = size
    if filled >= size:
        status, reason = FILLED, ""
    elif filled > 0.0:
        status, reason = PARTIAL, "TIMEOUT_OR_EVENTS_EXHAUSTED"
    else:
        status = NOT_FILLED
        if cum > 0.0:
            reason = "QUEUE_NOT_CLEARED"
        else:
            reason = "TOUCH_WITHOUT_VOLUME" if touches else "NO_TRADE_VOLUME"
    return MakerFillResult(
        status, reason, size, filled, tuple(fills), fills[0][0] if fills else None,
        fills[-1][0] if status == FILLED else None, q, c, eff, cum, cum > eff, touches, ignored, late,
        {"queue_ahead": qprov, "order_size": sprov, "cancel_fraction_ahead": cprov})


@dataclass(frozen=True)
class PostOnlyCheck:
    accepted: bool
    status: str                           # "ACCEPTED" | REJECTED_POST_ONLY
    reason: str


def check_post_only(side: str, price: float, best_bid: float, best_ask: float) -> PostOnlyCheck:
    """A post-only order that would match on arrival is REJECTED (cancelled), never filled: a buy at
    or above the best ask, a sell at or below the best bid."""
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    if not (_pos(price) and _pos(best_bid) and _pos(best_ask)):
        raise ValueError("price and book must be positive finite numbers")
    if best_bid >= best_ask:
        raise ValueError("the book is crossed or locked")
    crosses = price >= best_ask if side == "buy" else price <= best_bid
    if crosses:
        return PostOnlyCheck(False, REJECTED_POST_ONLY, "WOULD_CROSS_THE_BOOK")
    return PostOnlyCheck(True, "ACCEPTED", "")


def submit_post_only(side: str, price: float, size: Any, best_bid: float, best_ask: float,
                     queue_ahead: Any, trade_events: Iterable[Any], cancel_fraction_ahead: Any,
                     horizon_ms: Optional[float] = None) -> MakerFillResult:
    """`check_post_only` and then `simulate_maker_fill`. A rejection returns ZERO fill and consumes
    no trade event, whatever the tape later shows."""
    chk = check_post_only(side, price, best_bid, best_ask)
    sz, sprov = _scalar(size, "order_size", lo=0.0, lo_open=True)
    q, qprov = _scalar(queue_ahead, "queue_ahead", lo=0.0)
    c, cprov = _scalar(cancel_fraction_ahead, "cancel_fraction_ahead", lo=0.0, hi=1.0)
    if not chk.accepted:
        return MakerFillResult(REJECTED_POST_ONLY, chk.reason, sz, 0.0, (), None, None, q, c, q * (1.0 - c),
                               0.0, False, 0, 0, 0, {"queue_ahead": qprov, "order_size": sprov,
                                                       "cancel_fraction_ahead": cprov})
    return simulate_maker_fill(q, sz, trade_events, c, side=side, price=price, horizon_ms=horizon_ms)


def post_only_reject_probability(spread_bps: float, vol_bps_per_sqrt_s: float, latency_ms: float) -> float:
    """P(the market moved through a post-only order priced off the decision-time touch during the
    latency): Phi(-spread / (vol * sqrt(latency))). In [0, 0.5); 0 with no latency or no vol."""
    if not _pos(spread_bps) or not _nonneg(vol_bps_per_sqrt_s) or not _nonneg(latency_ms):
        raise ValueError("spread must be positive; vol and latency non-negative finite numbers")
    sd = vol_bps_per_sqrt_s * math.sqrt(latency_ms / 1000.0)
    return 0.0 if sd <= 0.0 else NormalDist().cdf(-spread_bps / sd)


def expected_adverse_move_bps(vol_bps_per_sqrt_s: float, elapsed_ms: float) -> float:
    """The one-sided expected adverse move over ``elapsed_ms``: vol * sqrt(t) / sqrt(2*pi)."""
    if not _nonneg(vol_bps_per_sqrt_s) or not _nonneg(elapsed_ms):
        raise ValueError("vol and elapsed time must be non-negative finite numbers")
    return PHI0 * vol_bps_per_sqrt_s * math.sqrt(elapsed_ms / 1000.0)


def legging_probability(p_leg_a: float, p_leg_b: float) -> float:
    """P(exactly one of two independent hedge legs is in place) = a(1-b) + b(1-a), in [0, 1]."""
    for p in (p_leg_a, p_leg_b):
        if not (_fin(p) and 0.0 <= p <= 1.0):
            raise ValueError("leg probabilities must be in [0, 1]")
    return p_leg_a * (1.0 - p_leg_b) + p_leg_b * (1.0 - p_leg_a)


def legging_risk(p_leg_a: float, p_leg_b: float, window_ms: float, vol_bps_per_sqrt_s: float) -> Dict[str, Any]:
    """Probability of one hedge leg filling without the other, and the one-sided expected adverse
    move of the naked leg over the legging window. SIMULATED_FILL: arithmetic on stated inputs."""
    p = legging_probability(p_leg_a, p_leg_b)
    move = expected_adverse_move_bps(vol_bps_per_sqrt_s, window_ms)
    return {"evidence_class": SIMULATED_FILL, "is_measured": False, "probability_one_leg": p,
            "window_ms": window_ms, "expected_adverse_move_bps_if_legged": move, "expected_cost_bps": p * move,
            "convention": "one-sided expected adverse move of the naked leg; favourable moves not credited"}


# -- ACCOUNT_EXECUTION: the only thing that can be MEASURED ----------------------------------------------

MAKER_POST_ONLY, TAKER_ORDER = "MAKER_POST_ONLY", "TAKER_ORDER"
TERMINAL_STATUSES = ("FILLED", "PARTIALLY_FILLED", "CANCELLED_UNFILLED", "EXPIRED_UNFILLED", "REJECTED_POST_ONLY")


@dataclass(frozen=True)
class AccountExecutionRecord:
    """The terminal outcome of ONE order of THIS account, as the venue's own execution/fee records
    state it. NOTHING in this repository produces these; the caller must have read them from the
    account. A record that filled needs a fee record (`fee_quote` and `fee_record_id`) to count.
    The constructor refuses a source that calls itself simulated: a tripwire, not a proof of provenance
    (the type cannot know where the numbers really came from; the caller attests to that)."""
    order_id: str
    symbol: str
    market: str                       # "linear_perp" | "spot"
    side: str                         # "buy" | "sell"
    intent: str                       # MAKER_POST_ONLY | TAKER_ORDER
    status: str                       # one of TERMINAL_STATUSES
    qty_requested: float
    qty_filled: float
    submitted_ms: float
    terminal_ms: float
    source: str
    avg_fill_price: Optional[float] = None
    fee_quote: Optional[float] = None          # fee paid in the quote currency; negative = rebate
    fee_currency: str = "USDT"
    fee_record_id: str = ""
    arrival_mid: Optional[float] = None        # public mid at submission
    mid_after_fill: Optional[float] = None     # public mid `markout_horizon_ms` after the fill
    markout_horizon_ms: Optional[float] = None

    EVIDENCE_CLASS: ClassVar[str] = ACCOUNT_EXECUTION

    @property
    def evidence_class(self) -> str:
        return ACCOUNT_EXECUTION

    def __post_init__(self) -> None:
        for n in ("order_id", "symbol", "source", "fee_currency"):
            v = getattr(self, n)
            if not (isinstance(v, str) and v.strip()):
                raise ValueError(f"{n} must be a non-empty string")
        words = self.source.upper().replace("_", " ").replace("-", " ").replace("/", " ").split()
        if any(w.startswith("SIMULAT") or w in ("SIM", "SYNTHETIC", "BACKTEST", "MODELLED", "MODELED") for w in words):
            raise EvidenceClassError("a simulated source cannot be an ACCOUNT_EXECUTION record")
        if self.market not in ("linear_perp", "spot"):
            raise ValueError("market must be 'linear_perp' or 'spot'")
        if self.side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        if self.intent not in (MAKER_POST_ONLY, TAKER_ORDER):
            raise ValueError(f"intent {self.intent!r}")
        if self.status not in TERMINAL_STATUSES:
            raise ValueError(f"status {self.status!r} is not terminal: {TERMINAL_STATUSES}")
        if not _pos(self.qty_requested) or not _nonneg(self.qty_filled):
            raise ValueError("qty_requested must be positive and qty_filled non-negative")
        if not (_nonneg(self.submitted_ms) and _nonneg(self.terminal_ms) and self.terminal_ms >= self.submitted_ms):
            raise ValueError("submitted_ms/terminal_ms must be ordered non-negative numbers")
        full = self.qty_filled >= self.qty_requested * (1.0 - 1e-9)
        if self.qty_filled > self.qty_requested * (1.0 + 1e-9):
            raise ValueError("qty_filled exceeds qty_requested")
        if self.status == "FILLED" and not full:
            raise ValueError("status FILLED but qty_filled < qty_requested")
        if self.status == "PARTIALLY_FILLED" and (full or self.qty_filled <= 0.0):
            raise ValueError("status PARTIALLY_FILLED needs 0 < qty_filled < qty_requested")
        if self.status in ("CANCELLED_UNFILLED", "EXPIRED_UNFILLED", "REJECTED_POST_ONLY") and self.qty_filled != 0.0:
            raise ValueError(f"status {self.status} needs qty_filled == 0")
        if self.qty_filled > 0.0 and not _pos(self.avg_fill_price):
            raise ValueError("a filled order needs a positive avg_fill_price")
        for n in ("avg_fill_price", "arrival_mid", "mid_after_fill", "markout_horizon_ms"):
            v = getattr(self, n)
            if v is not None and not _pos(v) and not (n == "markout_horizon_ms" and _nonneg(v)):
                raise ValueError(f"{n} must be a positive finite number when given")
        if self.fee_quote is not None and not _fin(self.fee_quote):
            raise ValueError("fee_quote must be finite when given")


def _wilson(k: float, n: float, z: float = 1.959963984540054) -> Tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    ph = k / n
    den = 1.0 + z * z / n
    centre = (ph + z * z / (2.0 * n)) / den
    half = z * math.sqrt(ph * (1.0 - ph) / n + z * z / (4.0 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def _fee_bps(r: AccountExecutionRecord) -> Optional[float]:
    if r.qty_filled <= 0.0 or r.fee_quote is None or r.fee_currency.upper() not in ("USDT", "USD", "USDC"):
        return None
    return r.fee_quote / (r.qty_filled * r.avg_fill_price) * 1e4


def _slip_bps(r: AccountExecutionRecord) -> Optional[float]:
    if r.qty_filled <= 0.0 or r.arrival_mid is None:
        return None
    sgn = 1.0 if r.side == "buy" else -1.0
    return sgn * (r.avg_fill_price - r.arrival_mid) / r.arrival_mid * 1e4


def _markout_bps(r: AccountExecutionRecord) -> Optional[float]:
    if r.qty_filled <= 0.0 or r.mid_after_fill is None or r.markout_horizon_ms is None:
        return None
    sgn = 1.0 if r.side == "buy" else -1.0
    return sgn * (r.avg_fill_price - r.mid_after_fill) / r.avg_fill_price * 1e4


def measure_account_execution(records: Optional[Iterable[Any]]) -> Dict[str, Any]:
    """The MEASURED section. NOT_AVAILABLE when no records are supplied; INSUFFICIENT when a
    supplied set is below the stated minimums (no estimate is then shown, only counts); MEASURED
    only above them. Anything that is not an `AccountExecutionRecord` raises `EvidenceClassError`:
    a simulated result can never be counted as an account fill."""
    base: Dict[str, Any] = {
        "evidence_class": ACCOUNT_EXECUTION, "is_simulated": False,
        "minimums": {"maker_attempts": MIN_MAKER_ATTEMPTS, "maker_fills": MIN_MAKER_FILLS,
                     "span_ms": MIN_SPAN_MS, "taker_fills_for_taker_cost": MIN_TAKER_FILLS}}
    if records is None:
        return {**base, "status": NOT_AVAILABLE, "reasons": ["NO_ACCOUNT_EXECUTION_RECORDS_SUPPLIED"],
                "reading": "nothing in this repository produces account fills; maker economics have no measured basis"}
    if isinstance(records, (str, bytes, Mapping)):
        raise EvidenceClassError("records must be an iterable of AccountExecutionRecord")
    recs = list(records)
    for r in recs:
        if not isinstance(r, AccountExecutionRecord):
            raise EvidenceClassError(
                f"{type(r).__name__} is not an AccountExecutionRecord (evidence class "
                f"{getattr(r, 'evidence_class', 'UNSTATED')}): simulated or derived results are never account fills")
    if not recs:
        return {**base, "status": NOT_AVAILABLE, "reasons": ["NO_ACCOUNT_EXECUTION_RECORDS_SUPPLIED"],
                "reading": "nothing in this repository produces account fills; maker economics have no measured basis"}

    seen, usable, excluded = set(), [], {}
    for r in sorted(recs, key=lambda x: (x.submitted_ms, x.order_id)):
        if r.order_id in seen:
            excluded["DUPLICATE_ORDER_ID"] = excluded.get("DUPLICATE_ORDER_ID", 0) + 1
        elif r.qty_filled > 0.0 and (r.fee_quote is None or not r.fee_record_id.strip()):
            excluded["FILL_WITHOUT_FEE_RECORD"] = excluded.get("FILL_WITHOUT_FEE_RECORD", 0) + 1
        else:
            usable.append(r)
        seen.add(r.order_id)
    makers = [r for r in usable if r.intent == MAKER_POST_ONLY]
    takers = [r for r in usable if r.intent == TAKER_ORDER and r.qty_filled > 0.0]
    maker_fills = [r for r in makers if r.qty_filled > 0.0]
    span = (max(r.submitted_ms for r in makers) - min(r.submitted_ms for r in makers)) if makers else 0.0
    counts = {"records_supplied": len(recs), "records_usable": len(usable), "excluded": dict(sorted(excluded.items())),
              "maker_attempts": len(makers), "maker_fills": len(maker_fills), "taker_fills": len(takers),
              "maker_span_ms": span}
    reasons: List[str] = []
    if len(makers) < MIN_MAKER_ATTEMPTS:
        reasons.append(f"MAKER_ATTEMPTS_{len(makers)}_BELOW_MINIMUM_{MIN_MAKER_ATTEMPTS}")
    if len(maker_fills) < MIN_MAKER_FILLS:
        reasons.append(f"MAKER_FILLS_{len(maker_fills)}_BELOW_MINIMUM_{MIN_MAKER_FILLS}")
    if span < MIN_SPAN_MS:
        reasons.append("MAKER_ATTEMPTS_DO_NOT_SPAN_THE_MINIMUM_WINDOW")
    if reasons:
        return {**base, "status": INSUFFICIENT, "reasons": reasons, "counts": counts, "estimates_withheld": True,
                "reading": "records were supplied but are below the stated minimum: no estimate is offered"}

    flags = [1.0 if r.status == "FILLED" else 0.0 for r in makers]
    k, n = sum(flags), len(flags)
    notes: List[str] = []
    if len(set(flags)) == 1:
        n_eff = float(n)
        notes.append("CONSTANT_FILL_SERIES_SERIAL_DEPENDENCE_UNDETECTABLE")
    else:
        n_eff = min(float(n), _es.conservative_ess(flags) or float(n))
    lo, hi = _wilson(k / n * n_eff, n_eff)
    rej = sum(1 for r in makers if r.status == "REJECTED_POST_ONLY")
    scale = n_eff / n
    acc_n = (n - rej) * scale                       # a rejection is not a fill ATTEMPT at the queue
    acc_lo = _wilson(k * scale, acc_n)[0] if acc_n > 0 else 0.0
    rej_hi = _wilson(rej * scale, n_eff)[1]
    costs = [c for c in ((_slip_bps(r), _fee_bps(r)) for r in maker_fills) if c[0] is not None and c[1] is not None]
    marks = [m for m in (_markout_bps(r) for r in maker_fills) if m is not None]
    adverse: Dict[str, Any]
    if len(marks) >= MIN_MAKER_FILLS:
        ci = _es.block_bootstrap_ci(marks)
        adverse = {"status": "MEASURED" if ci.get("status") == "OK" else INSUFFICIENT, "n": len(marks),
                   "mean_bps": _mean(marks),
                   "upper_bound_bps": ci["hi"] if ci.get("status") == "OK" else None, "interval": ci}
    else:
        adverse = {"status": INSUFFICIENT, "n": len(marks), "mean_bps": None, "upper_bound_bps": None,
                   "reason": f"fewer than {MIN_MAKER_FILLS} fills carry a mark-out"}
    fee_by_market: Dict[str, Any] = {}
    for m in ("linear_perp", "spot"):
        fb = [_fee_bps(r) for r in maker_fills if r.market == m and _fee_bps(r) is not None]
        if len(fb) >= MIN_MAKER_FILLS:
            fee_by_market[m] = {"n": len(fb), "mean_bps": _mean(fb)}
    maker_out = {
        "attempts": n, "full_fills": int(k), "partial_fills": sum(1 for r in makers if r.status == "PARTIALLY_FILLED"),
        "post_only_rejections": rej, "post_only_rejection_rate_upper_95": rej_hi,
        "unfilled": sum(1 for r in makers if r.qty_filled == 0.0),
        "full_fill_rate": k / n, "full_fill_rate_effective_n": n_eff,
        "full_fill_rate_lower_95": lo, "full_fill_rate_upper_95": hi,
        "full_fill_rate_given_accepted_lower_95": acc_lo,
        "volume_weighted_fill_fraction": sum(r.qty_filled for r in makers) / sum(r.qty_requested for r in makers),
        "realized_cost_bps_vs_arrival_mid": _mean([a + b for a, b in costs]) if costs else None,
        "realized_cost_n": len(costs), "adverse_selection": adverse, "realized_fee_bps_by_market": fee_by_market,
        "notes": notes}
    tk_costs = [c for c in ((_slip_bps(r), _fee_bps(r)) for r in takers) if c[0] is not None and c[1] is not None]
    taker_out = {"fills": len(takers),
                 "realized_cost_bps_vs_arrival_mid": (_mean([a + b for a, b in tk_costs])
                                                       if len(tk_costs) >= MIN_TAKER_FILLS else None),
                 "realized_cost_n": len(tk_costs)}
    return {**base, "status": MEASURED, "reasons": [], "counts": counts, "maker": maker_out, "taker": taker_out,
            "reading": "realized outcomes of this account's own orders; conservative bounds, not point forecasts"}


# -- the book: taker economics and the displayed-depth section ---------------------------------------------

@dataclass(frozen=True)
class _Leg:
    name: str
    instrument: str                  # "perp" | "spot"
    phase: str                       # ENTRY | EXIT
    side: str                        # taker side of this leg: "buy" | "sell"
    half_spread_bps: float
    slip_bps: float                  # displayed-book walk share
    impact_bps: float                # beyond-displayed-depth share
    taker_fee_bps: float
    maker_fee_bps: Optional[float]
    unwind_bps: float = 0.0          # cost of reversing this leg at taker (the other phase's leg)

    @property
    def taker_nolat_bps(self) -> float:
        return self.half_spread_bps + self.slip_bps + self.impact_bps + self.taker_fee_bps


@dataclass(frozen=True)
class ExecutionScenario:
    """Everything the comparison needs. ``timeout_ms`` is the maker policy's resting time (a chosen
    policy, not an uncertain input). ``params`` carries `Param`s; ``assumptions`` the ASSUMED ones."""
    depth: Mapping[str, Sequence[Tuple[float, float]]]
    mode: str
    notional_usd: float
    ref_price: float
    timeout_ms: float
    taker_fees: _me.FeeSchedule
    params: Mapping[str, Param] = field(default_factory=dict)
    assumptions: Assumptions = field(default_factory=Assumptions)


def _leg_specs(mode: str) -> List[Tuple[str, str, str, str]]:
    specs = [("perp", ENTRY, "sell", "perp_sell_entry"), ("perp", EXIT, "buy", "perp_buy_exit")]
    if mode == ACQUIRE:
        specs += [("spot", ENTRY, "buy", "spot_buy_entry"), ("spot", EXIT, "sell", "spot_sell_exit")]
    return specs


def _extended_walk(name: str, side: str, qty: float, bids: Sequence[Tuple[float, float]],
                   asks: Sequence[Tuple[float, float]], impact_bps: Optional[float]):
    """(half_spread, slip, impact, problem, needs_impact). Within displayed depth this IS
    md_economics.walk_leg. Beyond it, only with an explicit impact parameter, and the extra part is
    reported as `impact`, never as displayed depth."""
    w = _me.walk_leg(name, side, qty, bids, asks)
    if w.status == _me.MEASURED:
        return w.half_spread_bps, w.slippage_bps, 0.0, "", False
    if w.reason != "DEPTH_INSUFFICIENT":
        return 0.0, 0.0, 0.0, f"{name}:{w.reason}", False
    if impact_bps is None:
        return 0.0, 0.0, 0.0, "", True
    levels = bids if side == "sell" else asks
    disp = sum(s for _, s in levels)
    if disp <= 0.0:
        return 0.0, 0.0, 0.0, f"{name}:NO_DISPLAYED_DEPTH", False
    wd = _me.walk_leg(name, side, disp, bids, asks)
    if wd.status != _me.MEASURED:
        return 0.0, 0.0, 0.0, f"{name}:{wd.reason}", False
    frac = disp / qty
    touch, last = levels[0][0], levels[-1][0]
    beyond = abs(last - touch) / touch * 1e4 + last / touch * impact_bps
    return wd.half_spread_bps, frac * wd.slippage_bps, (1.0 - frac) * beyond, "", True


@dataclass(frozen=True)
class _Prepared:
    scenario: ExecutionScenario
    R: ResolvedParams
    qty: float
    phases: Optional[Tuple[Tuple[_Leg, ...], ...]]      # (entry legs, exit legs)
    book_problems: Tuple[str, ...]
    needs_impact: bool
    dc: _me.DepthCosts


def _prepare(s: ExecutionScenario) -> _Prepared:
    if not isinstance(s, ExecutionScenario):
        raise TypeError("scenario must be an ExecutionScenario")
    if s.mode not in (OVERLAY, ACQUIRE):
        raise ValueError(f"mode {s.mode!r} not in ({OVERLAY!r}, {ACQUIRE!r})")
    if not (_pos(s.notional_usd) and _pos(s.ref_price)):
        raise ValueError("notional_usd and ref_price must be positive finite numbers")
    if not _nonneg(s.timeout_ms):
        raise ValueError("timeout_ms must be a non-negative finite number")
    if not isinstance(s.taker_fees, _me.FeeSchedule):
        raise TypeError("taker_fees must be a md_economics.FeeSchedule")
    R = resolve_params(s.params, s.assumptions)
    if "fill_probability" in R.values and any(n in R.values for n in _STRUCTURAL):
        raise ValueError("supply either fill_probability or the queue model, not both: two fill models would disagree silently")
    qty = s.notional_usd / s.ref_price
    dc = _me.depth_costs(s.depth, mode=s.mode, notional_usd=s.notional_usd, ref_price=s.ref_price)
    impact = R.values.get("beyond_depth_impact_bps")
    raw: Dict[Tuple[str, str], Tuple[float, float, float]] = {}
    problems: List[str] = []
    needs_impact = False
    for inst, phase, side, name in _leg_specs(s.mode):
        bids = s.depth.get(f"{inst}_bids") or ()
        asks = s.depth.get(f"{inst}_asks") or ()
        h, slip, imp, prob, ni = _extended_walk(name, side, qty, bids, asks, impact)
        needs_impact = needs_impact or ni
        if prob:
            problems.append(prob)
        raw[(inst, phase)] = (h, slip, imp)
    phases: Optional[Tuple[Tuple[_Leg, ...], ...]] = None
    if not problems and not (needs_impact and impact is None):
        def fee(inst: str) -> float:
            return s.taker_fees.perp_taker_bps if inst == "perp" else s.taker_fees.spot_taker_bps

        def mfee(inst: str) -> Optional[float]:
            return R.values.get("maker_fee_bps_perp" if inst == "perp" else "maker_fee_bps_spot")

        def unwind(inst: str, phase: str) -> float:
            h, slip, imp = raw[(inst, EXIT if phase == ENTRY else ENTRY)]
            return h + slip + imp + fee(inst)

        out = []
        for ph in (ENTRY, EXIT):
            legs = []
            for inst, phase, side, name in _leg_specs(s.mode):
                if phase != ph:
                    continue
                h, slip, imp = raw[(inst, phase)]
                legs.append(_Leg(name, inst, phase, side, h, slip, imp, fee(inst), mfee(inst),
                                 unwind(inst, phase) if s.mode == ACQUIRE else 0.0))
            out.append(tuple(legs))
        phases = tuple(out)
    return _Prepared(s, R, qty, phases, tuple(problems), needs_impact, dc)


def _public_section(prep: _Prepared) -> Dict[str, Any]:
    dc = prep.dc
    ok = dc.spread_bps is not None and dc.slippage_bps is not None
    legs = [{"leg": l.leg, "side": l.side, "qty": l.qty, "status": COMPUTED if l.status == _me.MEASURED else "UNAVAILABLE",
             "reason": l.reason, "touch": l.touch, "avg_price": l.avg_price, "half_spread_bps": l.half_spread_bps,
             "slippage_bps": l.slippage_bps, "levels_used": l.levels_used} for l in dc.legs]
    return {"evidence_class": PUBLIC_DISPLAYED_DEPTH, "source": _me.DEPTH_SOURCE, "is_fill": False,
            "status": COMPUTED if ok else "UNAVAILABLE",
            "reasons": [r for r in dc.reasons if r != "LATENCY_ALLOWANCE_NOT_SUPPLIED"],
            "spread_bps_round_trip": dc.spread_bps, "slippage_bps_round_trip": dc.slippage_bps, "legs": legs,
            "reading": "what the book DISPLAYED: a walk of resting liquidity, with no latency, no fee, no queue and no fill"}


# -- the cost engine ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _K:
    latency_ms: float
    timeout_ms: float
    sigma: float
    adverse_bps: float
    penalty_bps: float
    taker_fail_prob: float          # used only by TAKER in ACQUIRE, where it is a REQUIRED input
    reject_floor: float = 0.0       # measured post-only rejection (upper bound), if the verdict has one
    reject_override: Optional[float] = None     # a supplied rejection probability replaces the diffusion estimate


def _adv(sigma: float, elapsed_ms: float, z: float) -> float:
    return z * sigma * math.sqrt(max(0.0, elapsed_ms) / 1000.0)


def _taker_phase(legs: Sequence[_Leg], k: _K, one_leg: float, z: Callable[[], float]
                 ) -> Tuple[Dict[str, float], bool]:
    comp = _zero()
    for leg in legs:
        comp["spread"] += leg.half_spread_bps
        comp["fees"] += leg.taker_fee_bps
        comp["slippage"] += leg.slip_bps
        comp["impact"] += leg.impact_bps
        comp["delay"] += _adv(k.sigma, k.latency_ms, z())
    legged = False
    if len(legs) == 2 and one_leg > 0.0:
        comp["legging"] += one_leg * _adv(k.sigma, 2.0 * k.latency_ms, z())      # detect + resend
        legged = True
    return comp, legged


def _hybrid_mismatch_sq_s(phis: Sequence[float], t_rem: Sequence[float], latency_ms: float) -> float:
    """Integral over time of (fraction done on leg A - fraction done on leg B)^2, in seconds. A
    maker-filled share is done at the order's arrival; the chased remainder at t_rem."""
    (pa, ta), (pb, tb) = zip(phis, t_rem)
    pts = sorted({latency_ms, ta, tb})
    total = 0.0
    for t0, t1 in zip(pts, pts[1:]):
        da = 1.0 if (pa >= 1.0 or t0 >= ta) else pa
        db = 1.0 if (pb >= 1.0 or t0 >= tb) else pb
        total += (da - db) ** 2 * (t1 - t0) / 1000.0
    return total


def _phase_cost(policy: str, legs: Sequence[_Leg], outs: Sequence[Tuple[bool, float]], k: _K,
                z: Callable[[], float]) -> Tuple[Dict[str, float], Dict[str, bool]]:
    """Cost of one phase for MAKER or HYBRID given each leg's outcome (rejected?, filled fraction)."""
    comp = _zero()
    phis: List[float] = []
    t_rem: List[float] = []
    for leg, (rej, phi) in zip(legs, outs):
        phi = 0.0 if rej else float(phi)
        phis.append(phi)
        tr = 3.0 * k.latency_ms if rej else 2.0 * k.latency_ms + k.timeout_ms
        t_rem.append(tr)
        comp["spread"] -= phi * leg.half_spread_bps
        comp["fees"] += phi * leg.maker_fee_bps
        comp["adverse_selection"] += phi * k.adverse_bps
        rest = 1.0 - phi
        if policy == HYBRID and rest > 0.0:
            comp["spread"] += rest * leg.half_spread_bps
            comp["fees"] += rest * leg.taker_fee_bps
            comp["slippage"] += rest * leg.slip_bps
            comp["impact"] += rest * leg.impact_bps
            comp["delay"] += rest * _adv(k.sigma, tr, z())
    legged = unest = False
    if policy == HYBRID:
        if len(legs) == 2:
            sq = _hybrid_mismatch_sq_s(phis, t_rem, k.latency_ms)
            if sq > 0.0:
                comp["legging"] += k.sigma * math.sqrt(sq) * z()
                legged = True
    else:
        e = min(phis)
        for leg, phi in zip(legs, phis):
            if phi - e > 0.0:
                comp["unwind"] += (phi - e) * leg.unwind_bps
        comp["penalty"] += (1.0 - e) * k.penalty_bps
        unest = e < 1.0 - 1e-12
        if len(legs) == 2 and max(phis) - e > 1e-12:
            window = max(0.0, t_rem[phis.index(e)] - k.latency_ms)
            comp["legging"] += k.sigma * (max(phis) - e) * math.sqrt(window / 1000.0) * z()
            legged = True
    return comp, {"legged": legged, "unestablished": unest}


def _const_z() -> float:
    return PHI0


def _reject_prob(leg: _Leg, k: _K) -> float:
    q = (k.reject_override if k.reject_override is not None
         else post_only_reject_probability(2.0 * leg.half_spread_bps, k.sigma, k.latency_ms))
    return max(q, k.reject_floor)


def _classes(leg: _Leg, k: _K, p: float) -> Tuple[Tuple[bool, float, float], ...]:
    q = _reject_prob(leg, k)
    return ((True, 0.0, q), (False, 1.0, (1.0 - q) * p), (False, 0.0, (1.0 - q) * (1.0 - p)))


def _expected_policy(policy: str, phases: Sequence[Sequence[_Leg]], k: _K, p: Optional[float]) -> Dict[str, Any]:
    """The exact expectation (no sampling) in the DIRECT fill model, by enumerating every outcome
    (rejected / filled / unfilled per leg) with its probability."""
    comp = _zero()
    p_clean_leg = p_clean_unest = 1.0
    for legs in phases:
        if policy == TAKER:
            f = k.taker_fail_prob if len(legs) == 2 else 0.0
            w = 2.0 * f * (1.0 - f)
            c, _ = _taker_phase(legs, k, w, _const_z)
            pl, pu = w, 0.0
        else:
            c, pl, pu = _zero(), 0.0, 0.0
            for combo in itertools.product(*[_classes(l, k, p) for l in legs]):
                w = 1.0
                for cl in combo:
                    w *= cl[2]
                if w <= 0.0:
                    continue
                cc, fl = _phase_cost(policy, legs, [(cl[0], cl[1]) for cl in combo], k, _const_z)
                for key in COMPONENTS:
                    c[key] += w * cc[key]
                pl += w if fl["legged"] else 0.0
                pu += w if fl["unestablished"] else 0.0
        for key in COMPONENTS:
            comp[key] += c[key]
        p_clean_leg *= 1.0 - pl
        p_clean_unest *= 1.0 - pu
    return {"components": comp, "total": sum(comp.values()), "p_one_leg_failure": 1.0 - p_clean_leg,
            "p_nonfill": 1.0 - p_clean_unest}


def _quantile(sorted_xs: Sequence[float], q: float) -> float:
    pos = (len(sorted_xs) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def _dist(xs: Sequence[float]) -> Dict[str, Any]:
    n = len(xs)
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    s = sorted(xs)
    return {"n_paths": n, "mean": m, "std": sd, "se_mean": sd / math.sqrt(n), "p05": _quantile(s, 0.05),
            "p95": _quantile(s, 0.95), "min": s[0], "max": s[-1]}


class _ZStream:
    """Replays one path's pre-drawn one-sided normals so MAKER and HYBRID see the same market."""

    def __init__(self, zs: Sequence[float]):
        self._zs, self._i = zs, 0

    def __call__(self) -> float:
        v = self._zs[self._i % len(self._zs)]
        self._i += 1
        return v


@dataclass(frozen=True)
class _FillModel:
    kind: str                                   # "DIRECT" | "STRUCTURAL"
    p: Optional[float] = None
    queue: float = 0.0
    cancel: float = 0.0
    rate_btc_s: float = 0.0
    mean_size: float = 1.0


def _fill_model(R: ResolvedParams) -> Tuple[Optional[_FillModel], List[str]]:
    v = R.values
    if "fill_probability" in v:
        return _FillModel("DIRECT", p=v["fill_probability"]), []
    have = [n for n in _STRUCTURAL if n in v]
    if len(have) == len(_STRUCTURAL):
        return _FillModel("STRUCTURAL", queue=v["queue_ahead_btc"], cancel=v["cancel_fraction_ahead"],
                          rate_btc_s=v["through_volume_rate_btc_per_s"], mean_size=v["mean_trade_size_btc"]), []
    if have:
        return None, [n for n in _STRUCTURAL if n not in v]
    return None, ["fill_probability"]


def _sample_fill(fm: _FillModel, qty: float, timeout_ms: float, rng: random.Random) -> float:
    """Filled fraction of one ACCEPTED order. DIRECT: all or nothing. STRUCTURAL: the order joins a
    queue and volume prints through its price as a compound Poisson process; fills come from
    `simulate_maker_fill`, so a partial fill and a clean miss both happen."""
    if fm.kind == "DIRECT":
        return 1.0 if rng.random() < fm.p else 0.0
    events: List[Tuple[float, float]] = []
    lam = fm.rate_btc_s / (fm.mean_size * 1000.0)          # prints per ms
    need = fm.queue * (1.0 - fm.cancel) + qty
    cum, t = 0.0, 0.0
    if lam > 0.0:
        while True:
            t += rng.expovariate(lam)
            if t > timeout_ms:
                break
            sz = rng.expovariate(1.0 / fm.mean_size)
            events.append((t, sz))
            cum += sz
            if cum > need:
                break
    res = simulate_maker_fill(fm.queue, qty, events, fm.cancel, horizon_ms=timeout_ms)
    return res.fill_fraction


def _monte_carlo(policies: Sequence[str], phases: Sequence[Sequence[_Leg]], ks: Mapping[str, _K],
                 fm: Optional[_FillModel], qty: float, n_paths: int, seed: int) -> Dict[str, Dict[str, Any]]:
    rng = random.Random(seed)
    acc = {p: {"totals": [], "comp": _zero(), "legged": 0, "unest": 0, "fill_sum": 0.0, "full": 0, "legs": 0,
               "rej": 0} for p in policies}
    any_k = next(iter(ks.values()))
    for _ in range(n_paths):
        zs = [max(rng.gauss(0.0, 1.0), 0.0) for _ in range(12)]
        outs_by_phase: List[List[Tuple[bool, float]]] = []
        fails_by_phase: List[List[bool]] = []
        for legs in phases:
            o, f = [], []
            for leg in legs:
                if HYBRID in policies or MAKER in policies:
                    rej = rng.random() < _reject_prob(leg, any_k)
                    phi = 0.0 if rej else _sample_fill(fm, qty, any_k.timeout_ms, rng)
                    o.append((rej, phi))
                if TAKER in policies:
                    f.append(rng.random() < ks[TAKER].taker_fail_prob)
            outs_by_phase.append(o)
            fails_by_phase.append(f)
        for pol in policies:
            k = ks[pol]
            zf = _ZStream(zs)
            comp_path, legged, unest = _zero(), False, False
            for legs, outs, fails in zip(phases, outs_by_phase, fails_by_phase):
                if pol == TAKER:
                    one = 1.0 if (len(legs) == 2 and sum(fails) == 1) else 0.0
                    c, fl = _taker_phase(legs, k, one, zf)
                    legged = legged or fl
                else:
                    c, fl2 = _phase_cost(pol, legs, outs, k, zf)
                    legged = legged or fl2["legged"]
                    unest = unest or fl2["unestablished"]
                    a = acc[pol]
                    for rej, phi in outs:
                        a["legs"] += 1
                        a["rej"] += 1 if rej else 0
                        a["fill_sum"] += phi
                        a["full"] += 1 if (not rej and phi >= 1.0) else 0
                for key in COMPONENTS:
                    comp_path[key] += c[key]
            a = acc[pol]
            a["totals"].append(sum(comp_path.values()))
            for key in COMPONENTS:
                a["comp"][key] += comp_path[key]
            a["legged"] += 1 if legged else 0
            a["unest"] += 1 if unest else 0
    out: Dict[str, Dict[str, Any]] = {}
    for pol in policies:
        a = acc[pol]
        d = _dist(a["totals"])
        legs_n = max(a["legs"], 1)
        out[pol] = {"distribution": d, "components_mean": {k_: v / n_paths for k_, v in a["comp"].items()},
                    "p_one_leg_failure": a["legged"] / n_paths, "p_nonfill": a["unest"] / n_paths,
                    "p_post_only_reject_per_leg": a["rej"] / legs_n if pol != TAKER else None,
                    "mean_fill_fraction_per_leg": a["fill_sum"] / legs_n if pol != TAKER else None,
                    "p_full_fill_per_leg": a["full"] / legs_n if pol != TAKER else None}
    return out


# -- parameter requirements ----------------------------------------------------------------------------------

def _needs(policy: str, prep: _Prepared) -> List[str]:
    names = ["latency_ms", "vol_bps_per_sqrt_s"]
    if prep.needs_impact:
        names.append("beyond_depth_impact_bps")
    if policy == TAKER:
        if prep.scenario.mode == ACQUIRE:
            names.append("taker_leg_fail_probability")
        return names
    names.append("maker_fee_bps_perp")
    if prep.scenario.mode == ACQUIRE:
        names.append("maker_fee_bps_spot")
    if policy == MAKER:
        names.append("nonfill_penalty_bps")
    return names


def _missing(policy: str, prep: _Prepared, *, fill: bool, adverse: bool) -> List[str]:
    miss = prep.R.missing(_needs(policy, prep))
    if policy != TAKER:
        if adverse and "adverse_selection_bps" not in prep.R.values:
            miss.append("adverse_selection_bps")
        if fill:
            miss += _fill_model(prep.R)[1]
    return miss


def _k(prep: _Prepared, policy: str, adverse: float, reject_floor: float = 0.0) -> _K:
    v = prep.R.values
    return _K(v["latency_ms"], prep.scenario.timeout_ms, v["vol_bps_per_sqrt_s"], adverse,
              v["nonfill_penalty_bps"] if policy == MAKER else 0.0,
              v["taker_leg_fail_probability"] if (policy == TAKER and prep.scenario.mode == ACQUIRE) else 0.0,
              reject_floor, v.get("post_only_reject_probability"))


def _phases_with_fees(prep: _Prepared, fee_override: Optional[Mapping[str, float]]) -> Tuple[Tuple[_Leg, ...], ...]:
    if not fee_override:
        return prep.phases
    return tuple(tuple(replace(l, maker_fee_bps=max(l.maker_fee_bps, fee_override.get(l.instrument, l.maker_fee_bps)))
                       for l in legs) for legs in prep.phases)


def _cost_at(policy: str, prep: _Prepared, p: Optional[float], a: float,
             fee_override: Optional[Mapping[str, float]] = None, reject_floor: float = 0.0) -> Dict[str, Any]:
    return _expected_policy(policy, _phases_with_fees(prep, fee_override), _k(prep, policy, a, reject_floor), p)


# -- break-even -----------------------------------------------------------------------------------------------

def _be_setup(scenario: ExecutionScenario, policy: str):
    if policy not in (MAKER, HYBRID):
        raise ValueError("break-even is for MAKER or HYBRID against TAKER")
    prep = _prepare(scenario)
    if prep.book_problems:
        return prep, {"status": REFUSED, "reason": "BOOK_UNUSABLE", "book_problems": list(prep.book_problems),
                      "unknown_parameters": []}
    miss = sorted(set(_missing(TAKER, prep, fill=False, adverse=False)
                      + _missing(policy, prep, fill=False, adverse=False)))
    if miss:
        return prep, {"status": REFUSED, "reason": "UNKNOWN_PARAMETERS", "unknown_parameters": miss}
    return prep, None


def min_fill_probability_to_beat_taker(scenario: ExecutionScenario, policy: str, adverse_selection_bps: float,
                                       *, resolution: int = 200) -> Dict[str, Any]:
    """The smallest fill probability p* such that the policy's expected round-trip cost is no
    greater than TAKER's for every p in [p*, 1], at the stated adverse selection. A scan at
    1/resolution steps, refined by bisection at the boundary. WHAT WOULD HAVE TO BE TRUE."""
    a, _ = _scalar(adverse_selection_bps, "adverse_selection_bps", lo=0.0, hi=1000.0)
    prep, ref = _be_setup(scenario, policy)
    if ref:
        return {"evidence_class": SIMULATED_FILL, "is_measured": False, "forecast": False, **ref}
    taker = _cost_at(TAKER, prep, None, 0.0)["total"]

    def gap(p: float) -> float:
        return _cost_at(policy, prep, p, a)["total"] - taker

    out = {"evidence_class": SIMULATED_FILL, "is_measured": False, "forecast": False, "status": COMPUTED,
           "policy": policy, "adverse_selection_bps": a, "taker_cost_bps": taker, "min_fill_probability": None,
           "reason": ""}
    if gap(1.0) > 1e-12:
        out["reason"] = "NEVER_CHEAPER_THAN_TAKER_EVEN_AT_CERTAIN_FILL"
        out["cost_at_certain_fill_bps"] = taker + gap(1.0)
        return out
    grid = [i / resolution for i in range(resolution + 1)]
    first_ok = 1.0
    for p in reversed(grid):
        if gap(p) <= 1e-12:
            first_ok = p
        else:
            break
    if first_ok == 0.0:
        out["min_fill_probability"], out["reason"] = 0.0, "CHEAPER_AT_ANY_FILL_PROBABILITY"
        return out
    lo, hi = first_ok - 1.0 / resolution, first_ok          # gap(lo) > 0 >= gap(hi)
    for _ in range(60):
        mid = (lo + hi) / 2.0
        lo, hi = (mid, hi) if gap(mid) > 1e-12 else (lo, mid)
    out["min_fill_probability"] = hi
    return out


def max_adverse_selection_to_beat_taker(scenario: ExecutionScenario, policy: str, fill_probability: float
                                        ) -> Dict[str, Any]:
    """The largest adverse selection (bps per maker fill) at which the policy still costs no more
    than TAKER, at the stated fill probability. The cost is exactly linear in it. WHAT WOULD HAVE
    TO BE TRUE, not a forecast."""
    p, _ = _scalar(fill_probability, "fill_probability", lo=0.0, hi=1.0)
    prep, ref = _be_setup(scenario, policy)
    if ref:
        return {"evidence_class": SIMULATED_FILL, "is_measured": False, "forecast": False, **ref}
    taker = _cost_at(TAKER, prep, None, 0.0)["total"]
    c0 = _cost_at(policy, prep, p, 0.0)["total"]
    slope = _cost_at(policy, prep, p, 1.0)["total"] - c0
    out = {"evidence_class": SIMULATED_FILL, "is_measured": False, "forecast": False, "status": COMPUTED,
           "policy": policy, "fill_probability": p, "taker_cost_bps": taker, "cost_at_zero_adverse_selection_bps": c0,
           "cost_per_bps_of_adverse_selection": slope, "max_adverse_selection_bps": None, "reason": ""}
    if c0 > taker + 1e-12:
        out["reason"] = "NEVER_CHEAPER_THAN_TAKER_EVEN_WITH_ZERO_ADVERSE_SELECTION"
    elif slope <= 1e-15:
        out["reason"] = "NO_MAKER_FILLS_AT_THIS_PROBABILITY_ADVERSE_SELECTION_IS_NOT_ENGAGED"
    else:
        out["max_adverse_selection_bps"] = (taker - c0) / slope
    return out


def breakeven_analysis(scenario: ExecutionScenario, *, p_grid: Sequence[float] = P_GRID,
                       adverse_grid: Sequence[float] = ADVERSE_GRID_BPS) -> Dict[str, Any]:
    """Break-even and sensitivity for MAKER and HYBRID against TAKER. It does not need (and does
    not use) the scenario's fill probability or adverse selection: it solves for them."""
    out: Dict[str, Any] = {
        "evidence_class": SIMULATED_FILL, "is_measured": False, "kind": "WHAT_WOULD_HAVE_TO_BE_TRUE",
        "forecast": False,
        "reading": "the fill probability and adverse selection at which each policy would tie TAKER under the "
                   "other stated parameters. No fill has been observed here: these are conditions, not predictions."}
    for pol in (HYBRID, MAKER):
        prep, ref = _be_setup(scenario, pol)
        if ref:
            out[pol] = {"evidence_class": SIMULATED_FILL, "forecast": False, **ref}
            continue
        taker = _cost_at(TAKER, prep, None, 0.0)["total"]
        rows_p = [min_fill_probability_to_beat_taker(scenario, pol, a) for a in adverse_grid]
        rows_a = [max_adverse_selection_to_beat_taker(scenario, pol, p) for p in p_grid]
        cells = [[_cost_at(pol, prep, p, a)["total"] - taker for a in adverse_grid] for p in p_grid]
        out[pol] = {
            "evidence_class": SIMULATED_FILL, "forecast": False, "status": COMPUTED, "taker_cost_bps": taker,
            "best_case_cost_bps": _cost_at(pol, prep, 1.0, 0.0)["total"],
            "min_fill_probability_by_adverse_selection": [
                {"adverse_selection_bps": r["adverse_selection_bps"], "min_fill_probability": r["min_fill_probability"],
                 "reason": r["reason"]} for r in rows_p],
            "max_adverse_selection_by_fill_probability": [
                {"fill_probability": r["fill_probability"], "max_adverse_selection_bps": r["max_adverse_selection_bps"],
                 "reason": r["reason"]} for r in rows_a],
            "sensitivity_grid": {"fill_probability_axis": list(p_grid), "adverse_selection_axis_bps": list(adverse_grid),
                                 "cost_minus_taker_bps": cells,
                                 "beats_taker": [[c < 0.0 for c in row] for row in cells]}}
    return out


# -- the comparison -----------------------------------------------------------------------------------------------

def _refused(policy: str, unknown: Sequence[str], reason: str, **extra: Any) -> Dict[str, Any]:
    return {"evidence_class": SIMULATED_FILL, "is_measured": False, "policy": policy, "status": REFUSED,
            "reason": reason, "unknown_parameters": list(unknown), "expected_cost_bps": None, **extra}


def _mode_dict(policy: str, analytic: Optional[Dict[str, Any]], mc: Dict[str, Any]) -> Dict[str, Any]:
    d = mc["distribution"]
    if analytic is not None:
        comps, total, method = analytic["components"], analytic["total"], "ANALYTIC_ENUMERATION"
        p_leg, p_nf = analytic["p_one_leg_failure"], analytic["p_nonfill"]
    else:
        comps, total, method = mc["components_mean"], d["mean"], "MONTE_CARLO_MEAN"
        p_leg, p_nf = mc["p_one_leg_failure"], mc["p_nonfill"]
    out = {"evidence_class": SIMULATED_FILL, "is_measured": False, "policy": policy, "status": COMPUTED,
           "expected_cost_bps": total, "expected_cost_method": method,
           "components_bps": dict(comps), "components_reconcile": math.isclose(sum(comps.values()), total, abs_tol=1e-9),
           "monte_carlo": d, "p_one_leg_failure": p_leg, "p_nonfill": p_nf,
           "p_one_leg_failure_monte_carlo": mc["p_one_leg_failure"], "p_nonfill_monte_carlo": mc["p_nonfill"]}
    if policy != TAKER:
        out["p_post_only_reject_per_leg"] = mc["p_post_only_reject_per_leg"]
        out["mean_fill_fraction_per_leg"] = mc["mean_fill_fraction_per_leg"]
        out["p_full_fill_per_leg"] = mc["p_full_fill_per_leg"]
    return out


def compare_execution_modes(scenario: ExecutionScenario, *, account_records: Optional[Iterable[Any]] = None,
                            n_paths: int = DEFAULT_N_PATHS, seed: int = DEFAULT_SEED,
                            p_grid: Sequence[float] = P_GRID,
                            adverse_grid: Sequence[float] = ADVERSE_GRID_BPS) -> Dict[str, Any]:
    """Expected all-in ROUND-TRIP cost in bps of TAKER, MAKER and HYBRID with a seeded Monte Carlo
    distribution, the break-even analysis, the measured (account) section and the verdict. The three
    evidence classes are separate top-level keys. A mode whose inputs include an UNKNOWN is REFUSED
    and carries no cost."""
    if isinstance(n_paths, bool) or not isinstance(n_paths, int) or n_paths < MIN_N_PATHS:
        raise ValueError(f"n_paths must be an int >= {MIN_N_PATHS}")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an int: there is no wall clock and no unseeded randomness here")
    prep = _prepare(scenario)
    measured_section = measure_account_execution(account_records)
    modes: Dict[str, Dict[str, Any]] = {}
    fm, fm_missing = _fill_model(prep.R)
    runnable: List[str] = []
    for pol in MODES:
        if prep.book_problems:
            modes[pol] = _refused(pol, [], "BOOK_UNUSABLE", book_problems=list(prep.book_problems))
            continue
        miss = _missing(pol, prep, fill=True, adverse=True)
        if miss:
            extra: Dict[str, Any] = {}
            if pol == TAKER and prep.phases is not None:
                known = _zero()
                for legs in prep.phases:
                    for l in legs:
                        known["spread"] += l.half_spread_bps
                        known["slippage"] += l.slip_bps
                        known["impact"] += l.impact_bps
                        known["fees"] += l.taker_fee_bps
                extra["lower_bound_bps_not_a_cost"] = {"value": sum(known.values()), "components": known,
                                                        "omitted": "latency, legging: parameters UNKNOWN"}
            modes[pol] = _refused(pol, miss, "UNKNOWN_PARAMETERS", **extra)
        else:
            runnable.append(pol)
    if runnable:
        ks = {pol: _k(prep, pol, prep.R.values.get("adverse_selection_bps", 0.0)) for pol in runnable}
        mc = _monte_carlo(runnable, prep.phases, ks, fm if any(p != TAKER for p in runnable) else None,
                          prep.qty, n_paths, seed)
        for pol in runnable:
            analytic = None
            if pol == TAKER or (fm is not None and fm.kind == "DIRECT"):
                analytic = _expected_policy(pol, prep.phases, ks[pol], fm.p if fm and fm.kind == "DIRECT" else None)
            modes[pol] = _mode_dict(pol, analytic, mc[pol])
            if pol != TAKER:
                modes[pol]["post_only_reject_probability_by_leg"] = {
                    f"{l.name}": _reject_prob(l, ks[pol]) for legs in prep.phases for l in legs}
            if fm is not None and pol != TAKER:
                modes[pol]["fill_model"] = (
                    {"kind": "DIRECT", "fill_probability": fm.p} if fm.kind == "DIRECT"
                    else {"kind": "STRUCTURAL", "queue_ahead_btc": fm.queue, "cancel_fraction_ahead": fm.cancel,
                          "through_volume_rate_btc_per_s": fm.rate_btc_s, "mean_trade_size_btc": fm.mean_size,
                          "implied_full_fill_probability_per_leg": modes[pol]["p_full_fill_per_leg"]})
    claimed = sorted(n for n in _FILL_LIKE if prep.R.provenance.get(n) == MEASURED)
    sim = {"evidence_class": SIMULATED_FILL, "is_measured": False, "seed": seed, "n_paths": n_paths,
           "parameters": prep.R.echo(), "assumptions_echo": scenario.assumptions.echo(),
           "declared_measured_but_unverified_by_account_records": claimed,
           "unknown_parameters_by_mode": {m: modes[m]["unknown_parameters"] for m in MODES if modes[m]["status"] == REFUSED},
           "modes": modes, "breakeven": breakeven_analysis(scenario, p_grid=p_grid, adverse_grid=adverse_grid),
           "reading": "model output under stated parameters; every number here is SIMULATED_FILL, none is a measured fill"}
    return {"schema": SCHEMA,
            "scenario": {"mode": scenario.mode, "notional_usd": scenario.notional_usd, "ref_price": scenario.ref_price,
                         "qty_btc_per_leg": prep.qty, "timeout_ms": scenario.timeout_ms,
                         "taker_fee_bps": {"perp": scenario.taker_fees.perp_taker_bps,
                                           "spot": scenario.taker_fees.spot_taker_bps,
                                           "declared_provenance": (MEASURED if scenario.taker_fees.source ==
                                                                   _me.FEE_ACCOUNT_VERIFIED else ESTIMATED),
                                           "source": scenario.taker_fees.source},
                         "round_trip": "entry + exit"},
            PUBLIC_DISPLAYED_DEPTH: _public_section(prep),
            SIMULATED_FILL: sim,
            ACCOUNT_EXECUTION: measured_section,
            "verdict": _verdict(prep, sim["breakeven"], measured_section, 0.0)}


# -- the verdict ----------------------------------------------------------------------------------------------------

def _verdict(prep: _Prepared, be: Dict[str, Any], measured_section: Dict[str, Any],
             min_edge_bps: float) -> Dict[str, Any]:
    out: Dict[str, Any] = {"verdict": MAKER_NOT_SUPPORTED, "reasons": [],
                           "account_execution_status": measured_section["status"],
                           "maker_economics_supported": False, "model_at_measured_bounds": None,
                           "what_would_have_to_be_true": None}
    h = be.get(HYBRID, {})
    if h.get("status") == COMPUTED:
        out["what_would_have_to_be_true"] = {
            "evidence_class": SIMULATED_FILL, "forecast": False, "policy": HYBRID,
            "taker_cost_bps": h["taker_cost_bps"], "best_case_cost_bps": h["best_case_cost_bps"],
            "min_fill_probability_by_adverse_selection": h["min_fill_probability_by_adverse_selection"],
            "max_adverse_selection_by_fill_probability": h["max_adverse_selection_by_fill_probability"],
            "reading": "conditions under which HYBRID would tie TAKER; no fill has been observed to say they hold"}
    if measured_section["status"] == NOT_AVAILABLE:
        out["reasons"] += ["NO_MEASURED_FILL_OBSERVATIONS: maker economics are UNSUPPORTED",
                           "break-even figures are 'what would have to be true', not a forecast"]
        return out
    if measured_section["status"] == INSUFFICIENT:
        out["reasons"] += ["MEASURED_FILL_OBSERVATIONS_INSUFFICIENT: maker economics are UNSUPPORTED"]
        out["reasons"] += list(measured_section["reasons"])
        return out
    mk = measured_section["maker"]
    adv = mk["adverse_selection"]
    if adv["status"] != MEASURED or adv["upper_bound_bps"] is None:
        out["reasons"].append("ADVERSE_SELECTION_NOT_MEASURABLE_FROM_THE_SUPPLIED_RECORDS")
        return out
    if prep.book_problems:
        out["reasons"].append("BOOK_UNUSABLE_SO_THE_TAKER_BASELINE_IS_UNPRICED: " + ",".join(prep.book_problems))
        return out
    miss_t = _missing(TAKER, prep, fill=False, adverse=False)
    miss_h = _missing(HYBRID, prep, fill=False, adverse=False)
    if miss_t or miss_h:
        out["reasons"].append("CANNOT_EVALUATE_UNKNOWN_PARAMETERS: " + ",".join(sorted(set(miss_t + miss_h))))
        return out
    p_lo = mk["full_fill_rate_given_accepted_lower_95"]
    q_hi = mk["post_only_rejection_rate_upper_95"]
    a_hi = max(0.0, adv["upper_bound_bps"])
    fee_over = {}
    for market, inst in (("linear_perp", "perp"), ("spot", "spot")):
        if market in mk["realized_fee_bps_by_market"]:
            fee_over[inst] = mk["realized_fee_bps_by_market"][market]["mean_bps"]
    taker = _cost_at(TAKER, prep, None, 0.0)["total"]
    hybrid = _cost_at(HYBRID, prep, p_lo, a_hi, fee_over, q_hi)["total"]
    maker_only = (None if _missing(MAKER, prep, fill=False, adverse=False)
                  else _cost_at(MAKER, prep, p_lo, a_hi, fee_over, q_hi)["total"])
    out["model_at_measured_bounds"] = {
        "evidence_class": SIMULATED_FILL, "is_measured": False,
        "inputs_from_account_records": {"fill_probability_given_accepted_lower_95": p_lo,
                                        "post_only_rejection_floor_upper_95": q_hi,
                                        "adverse_selection_upper_bps": a_hi,
                                        "maker_fee_bps_by_instrument_used_if_higher_than_modelled": fee_over},
        "taker_cost_bps": taker, "hybrid_cost_bps": hybrid, "supporting_policy": HYBRID,
        "maker_only_cost_bps_not_used": maker_only,
        "maker_only_note": "pure MAKER is cheap or dear mostly by the nonfill_penalty_bps nobody measured; "
                           "it never decides the verdict. HYBRID always establishes the position.",
        "min_edge_bps_required": min_edge_bps,
        "reading": "the MODEL evaluated at account-measured conservative bounds; a model output, not a measured cost"}
    if hybrid < taker - min_edge_bps:
        out.update(verdict=MAKER_SUPPORTED_BY_MEASURED_FILLS, maker_economics_supported=True)
        out["reasons"].append(f"HYBRID costs {hybrid:.3f} bps vs TAKER {taker:.3f} bps at the measured lower fill "
                              f"bound {p_lo:.3f} and upper adverse-selection bound {a_hi:.3f} bps")
    else:
        out.update(verdict=TAKER_ONLY)
        out["reasons"].append(f"even at measured bounds HYBRID costs {hybrid:.3f} bps vs TAKER {taker:.3f} bps: "
                              f"no edge of {min_edge_bps} bps")
    return out


def recommend_mode(scenario: ExecutionScenario, account_records: Optional[Iterable[Any]] = None, *,
                   min_edge_bps: float = 0.0) -> Dict[str, Any]:
    """TAKER_ONLY | MAKER_SUPPORTED_BY_MEASURED_FILLS | MAKER_NOT_SUPPORTED, each with reasons.

    MAKER_NOT_SUPPORTED is the default and the only possible answer unless ACCOUNT_EXECUTION records
    clear the stated minimums: no assumed or estimated parameter, however flattering, can change it.
    Only then is the model evaluated at the records' conservative bounds (lower fill probability,
    upper adverse selection); MAKER_SUPPORTED_BY_MEASURED_FILLS needs the best maker policy to beat
    TAKER there by at least ``min_edge_bps``, otherwise TAKER_ONLY."""
    if not _nonneg(min_edge_bps):
        raise ValueError("min_edge_bps must be a non-negative finite number")
    prep = _prepare(scenario)
    return _verdict(prep, breakeven_analysis(scenario), measure_account_execution(account_records),
                    float(min_edge_bps))


def trade_economics_costs_for_taker(comparison: Mapping[str, Any]) -> _te.Costs:
    """The TAKER components of a comparison as `trade_economics.Costs`, so the repo's ledger
    (`trade_economics.compute`) reproduces the same round-trip bps. Excludes the taker legging
    expectation (not a `Costs` line); it is zero in OVERLAY."""
    t = comparison[SIMULATED_FILL]["modes"][TAKER]
    if t["status"] != COMPUTED:
        raise ValueError("the TAKER mode was refused; there is nothing to convert")
    fee = comparison["scenario"]["taker_fee_bps"]
    c = t["components_bps"]
    acquire = comparison["scenario"]["mode"] == ACQUIRE
    verified = fee["source"] == _me.FEE_ACCOUNT_VERIFIED
    return _te.Costs(perp_entry_fee_bps=fee["perp"], perp_exit_fee_bps=fee["perp"],
                     spot_entry_fee_bps=fee["spot"] if acquire else 0.0,
                     spot_exit_fee_bps=fee["spot"] if acquire else 0.0,
                     spread_bps=c["spread"], slippage_bps=c["slippage"], impact_bps=c["impact"],
                     latency_bps=c["delay"], fee_provenance="ACCOUNT_VERIFIED" if verified else "UNVERIFIED",
                     maker_taker="taker")

