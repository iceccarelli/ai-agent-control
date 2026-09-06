"""trade_stats.py — the payoff geometry, measured in units of the risk taken.

WHY THIS MODULE EXISTS
======================
Slice 8 measured the classical strategy on seven years of real BTC data: 53
trades for −0.34%, losing in **all four** walk-forward folds. One of those folds
had a **54.2% win rate and was still negative**.

Nothing in ``BacktestResult`` could explain that, because return and win rate
cannot explain it. A 54% win rate losing money is not a contradiction and it is
not a bug in the accounting — it is a statement about *payoff geometry*: the
average loss was larger than the average win, so the break-even hit rate implied
by the payoffs was above 54%. That single comparison — **observed hit rate next
to the break-even hit rate its own payoffs imply** — is the whole diagnosis, and
it was not being computed anywhere.

So this module computes it, and everything that hangs off it.

THE UNIT: R
-----------
Every number here is denominated in **R**, the risk actually taken on the trade::

    risk_amount = |entry_price - initial_stop_price| * qty
    R           = pnl / risk_amount

R is the right unit because it is the only one that makes trades comparable. A
$50 profit means nothing until you know whether $200 or $2,000 was at risk to
get it, and an equity-space return conflates the payoff geometry with the sizing
policy — which is exactly the conflation that let a losing geometry hide behind a
2% position cap for eight slices.

Two variants are reported side by side, never merged:

* **fee-exclusive R** — what the geometry did;
* **fee-inclusive R** — what the account got.

The gap between them is the cost drag, and on a 1-ATR stop it is a large
fraction of an R. A backtest that reports only one of these cannot tell "the
exits are wrong" from "the stop is too tight to survive the fees".

THE RULES THIS MODULE FOLLOWS
-----------------------------
1. **Every statistic returns ``None`` when the sample cannot support it**, on the
   same convention as ``performance_analytics.py``: 30 trades for a rate or a
   ratio, 5 observations for a conditional mean, 10 for a distribution. An
   expectancy from 53 trades is reported *with its confidence interval* and the
   summary says out loud when that interval straddles zero.
2. **Counts and sums are facts; rates and means are estimates.** Facts are always
   reported. Estimates are gated. A bucket of three stop-outs gets its count and
   its total R, and ``None`` for its hit rate.
3. **A structurally impossible input is refused, not absorbed.** A zero-width
   stop would make R infinite; it raises. A missing stop makes R *unknown*; it
   is recorded as ``None`` and counted, and the report says how many trades were
   excluded. Missing and impossible are different things and are treated
   differently.
4. **No approximation of things that were not measured.** MAE/MFE are reported
   only for trades that actually carry excursion prices, with the coverage
   printed next to them.

DEPENDENCIES
------------
Standard library and numpy, deliberately and permanently. Nothing here imports
``config``, ``persistence`` or ``backtest``, so the same code answers the same
question from a tool, from a unit test, and from inside the backtester — and the
number in the report block is provably the number the test checked.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import (
    Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union,
)

import numpy as np

__all__ = [
    "Trade",
    "Distribution",
    "ExitReasonStats",
    "TradeStats",
    "compute_trade_stats",
    "r_multiple",
    "break_even_hit_rate",
    "cost_drag_r",
    "canonical_exit_reason",
    "direction_of",
    "MIN_TRADES_FOR_RATIOS",
    "MIN_TRADES_FOR_DISTRIBUTION",
    "MIN_OBSERVATIONS_FOR_MEAN",
    "MIN_TRADES_PER_BUCKET",
    "CONFIDENCE_LEVEL",
    "BOOTSTRAP_RESAMPLES",
    "BOOTSTRAP_SEED",
]

# ---------------------------------------------------------------------------
# thresholds — the sample sizes below which a number is not reported
# ---------------------------------------------------------------------------

#: Rates and ratios (hit rate, profit factor, expectancy) need this many trades.
#: Deliberately identical to ``performance_analytics.MIN_TRADES_FOR_RATIOS`` so
#: the two reports agree about what "enough" means; a project with two different
#: honesty thresholds has neither.
MIN_TRADES_FOR_RATIOS = 30

#: A conditional mean (average win, average loss, mean bars held for winners)
#: needs this many observations *in that condition*. 30 trades containing 2 wins
#: does not give you an average win — it gives you two numbers.
MIN_OBSERVATIONS_FOR_MEAN = 5

#: A distribution (median, deciles) needs this many observations. Below it the
#: p10 and p90 are just the minimum and maximum wearing a percentile's name.
MIN_TRADES_FOR_DISTRIBUTION = 10

#: A per-exit-reason bucket reports its count and total R at any size, but its
#: rates only above this.
MIN_TRADES_PER_BUCKET = 5

#: Two-sided confidence level for the expectancy interval.
CONFIDENCE_LEVEL = 0.95

#: Resamples for the bootstrap interval. The bootstrap is used rather than a
#: normal or Student-t interval because the R distribution is not remotely
#: normal: it is hard-floored near −1 (a stop that fills at its trigger) with a
#: long right tail, so a symmetric interval around the mean would misstate both
#: ends. The percentile bootstrap makes no distributional assumption.
BOOTSTRAP_RESAMPLES = 5_000

#: Fixed so the reported interval is reproducible. A confidence interval that
#: moves between two runs of the same data is not evidence, and "run it again
#: until the interval clears zero" is a failure mode this repository can afford
#: to design out.
BOOTSTRAP_SEED = 20_240_915

#: Chunk size cap for the bootstrap index matrix, in elements. Bounds peak
#: memory at a few tens of MB regardless of how many trades are supplied.
_BOOTSTRAP_CHUNK_ELEMENTS = 2_000_000

_BUY_WORDS = frozenset({"buy", "long", "b"})
_SELL_WORDS = frozenset({"sell", "short", "s"})

#: Raw exit-reason strings this stack actually emits, mapped onto the four
#: canonical outcomes plus ``end_of_data``. Anything unrecognised is passed
#: through lower-cased as its own bucket rather than being filed under "manual",
#: because inventing a category is a way of hiding one.
_EXIT_REASON_ALIASES: Dict[str, str] = {
    "stop": "stop",
    "stop_loss": "stop",
    "stoploss": "stop",
    "sl": "stop",
    "stopped": "stop",
    "stopped_out": "stop",
    "tp": "take_profit",
    "take_profit": "take_profit",
    "takeprofit": "take_profit",
    "target": "take_profit",
    "timeout": "timeout",
    "time_stop": "timeout",
    "max_hold": "timeout",
    "max_hold_hours": "timeout",
    "horizon": "timeout",
    "expiry": "timeout",
    "manual": "manual",
    "human": "manual",
    "discretionary": "manual",
    "close": "manual",
    "closed": "manual",
    "backtest_end": "end_of_data",
    "end_of_data": "end_of_data",
    "eod": "end_of_data",
    "": "unknown",
}

#: Accepted spellings for each ``Trade`` field when building one from a mapping.
#: Kept short and explicit on purpose: a permissive alias table is how a
#: ``stop_price`` that actually holds the *current* stop ends up being reported
#: as the initial one.
_FIELD_ALIASES: Dict[str, Tuple[str, ...]] = {
    "entry_price": ("entry_price", "entry"),
    "exit_price": ("exit_price", "exit"),
    "qty": ("qty", "quantity", "size"),
    "side": ("side", "direction"),
    "initial_stop_price": ("initial_stop_price", "initial_stop", "stop_price"),
    "entry_fee": ("entry_fee",),
    "exit_fee": ("exit_fee",),
    "entry_index": ("entry_index", "entry_bar", "opened_epoch"),
    "exit_index": ("exit_index", "exit_bar", "closed_epoch"),
    "exit_reason": ("exit_reason", "reason"),
    "mae_price": ("mae_price", "worst_price"),
    "mfe_price": ("mfe_price", "best_price"),
    "symbol": ("symbol",),
}


# ---------------------------------------------------------------------------
# small pure helpers
# ---------------------------------------------------------------------------


def direction_of(side: Any) -> float:
    """``+1.0`` for a long, ``-1.0`` for a short. Raises on anything else.

    There is no default. A side this function does not recognise is not a
    long — it is a record whose direction is unknown, and guessing it silently
    flips the sign of every statistic computed from the trade. Slice 5's
    prefix-matched ``"Sideways"`` becoming ``"Sell"`` is the same defect and it
    cost a rebuild to find.
    """
    text = str(side).strip().lower()
    if text in _BUY_WORDS:
        return 1.0
    if text in _SELL_WORDS:
        return -1.0
    raise ValueError(
        f"unrecognised side {side!r}; expected one of "
        f"{sorted(_BUY_WORDS | _SELL_WORDS)} — refusing to guess a direction"
    )


def canonical_exit_reason(raw: Any) -> str:
    """Map an exit-reason string onto a canonical bucket name.

    Unknown reasons are lower-cased and returned unchanged rather than being
    folded into ``manual``. A breakdown that quietly absorbs unfamiliar exits
    into a familiar bucket reports a clean four-way split that is not true.
    """
    if raw is None:
        return "unknown"
    text = str(raw).strip().lower()
    if text in _EXIT_REASON_ALIASES:
        return _EXIT_REASON_ALIASES[text]
    return text.replace(" ", "_").replace("-", "_")


def break_even_hit_rate(payoff_ratio: Optional[float]) -> Optional[float]:
    """Hit rate at which a payoff ratio breaks even: ``1 / (1 + payoff)``.

    ``payoff_ratio`` is average win divided by average loss, both as positive
    magnitudes. At 2:1 the answer is 1/3 — the number slice 8's labelling study
    ran into: a 30.7% unconditional hit rate against a 33.3% requirement, which
    is negative expectancy *before* costs and independent of signal quality.

    Returns ``None`` for a non-positive or non-finite payoff ratio. A strategy
    whose average win is zero cannot break even at any hit rate, and reporting
    ``1.0`` for it would read as "you need to win every trade", which is a
    materially different and much more optimistic claim.
    """
    if payoff_ratio is None:
        return None
    value = float(payoff_ratio)
    if not math.isfinite(value) or value <= 0.0:
        return None
    return 1.0 / (1.0 + value)


def cost_drag_r(round_trip_cost_bps: float, stop_distance_bps: float) -> float:
    """Fraction of one R consumed by the round trip: ``cost_bps / stop_bps``.

    This is the mechanical reason a tight stop is expensive, and it is why the
    stop distance is a *cost* decision as much as a risk decision. A 25 bps round
    trip against a 100 bps stop is 0.25R gone before the market does anything —
    which, at a 33% break-even hit rate, is roughly four points of hit rate that
    the entry signal has to find just to stand still.

    Raises rather than returning infinity on a zero or negative stop distance. A
    caller asking what fraction of nothing the fees represent has a bug, and an
    ``inf`` propagates into a mean and poisons a whole report silently.
    """
    cost = float(round_trip_cost_bps)
    distance = float(stop_distance_bps)
    if not math.isfinite(cost):
        raise ValueError(f"round_trip_cost_bps must be finite, got {cost!r}")
    if not math.isfinite(distance) or distance <= 0.0:
        raise ValueError(
            f"stop_distance_bps must be finite and > 0, got {distance!r}; a "
            "zero-width stop has no R to express the cost in, and the answer "
            "is undefined rather than infinite"
        )
    return cost / distance


def r_multiple(
    *,
    entry_price: float,
    exit_price: float,
    qty: float,
    side: Any,
    initial_stop_price: float,
    fees: float = 0.0,
) -> float:
    """The central formula, isolated so it can be checked by hand.

    ``(exit - entry) * direction * qty  /  (|entry - initial_stop| * qty)``,
    less ``fees`` in the numerator when they are supplied.

    ``qty`` cancels in the fee-exclusive case and is kept in the signature
    anyway, because it does *not* cancel once fees are involved: fees are an
    absolute amount, so their cost in R depends on how large the position was
    relative to its risk.
    """
    entry = float(entry_price)
    stop = float(initial_stop_price)
    quantity = float(qty)
    risk_per_unit = abs(entry - stop)
    if not math.isfinite(risk_per_unit) or risk_per_unit <= 0.0:
        raise ValueError(
            f"zero-width stop: entry {entry!r} equals initial stop {stop!r}, so "
            "the risk taken was zero and the R-multiple is undefined, not "
            "infinite — refusing to report a number"
        )
    if not math.isfinite(quantity) or quantity <= 0.0:
        raise ValueError(f"qty must be finite and > 0, got {qty!r}")
    risk_amount = risk_per_unit * quantity
    gross = (float(exit_price) - entry) * direction_of(side) * quantity
    return (gross - float(fees)) / risk_amount


# ---------------------------------------------------------------------------
# the input record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Trade:
    """One closed trade, carrying enough detail to express it in R.

    Validation happens at construction, so an impossible record cannot reach a
    statistic. The distinction the validation draws, and the one this whole
    module turns on:

    * ``initial_stop_price is None`` — the risk taken was **not recorded**. R is
      ``None``, the trade is counted in ``trades_without_stop``, and it is
      excluded from every R statistic. Honest ignorance.
    * ``initial_stop_price == entry_price``, or a stop on the wrong side of the
      entry — the record is **impossible**. It raises. Absorbing it would mean
      dividing by zero or reporting a negative risk as a positive one.
    """

    entry_price: float
    exit_price: float
    qty: float
    side: str
    initial_stop_price: Optional[float] = None
    entry_fee: float = 0.0
    exit_fee: float = 0.0
    entry_index: Optional[float] = None
    exit_index: Optional[float] = None
    exit_reason: str = "unknown"
    mae_price: Optional[float] = None
    mfe_price: Optional[float] = None
    symbol: str = ""

    # -- validation --------------------------------------------------------

    def __post_init__(self) -> None:
        for name in ("entry_price", "exit_price", "qty"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(
                    f"{name} must be finite and > 0, got {getattr(self, name)!r}"
                )
        for name in ("entry_fee", "exit_fee"):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite, got {value!r}")

        direction = direction_of(self.side)   # raises on an unknown side

        stop = self.initial_stop_price
        if stop is not None:
            stop_value = float(stop)
            if not math.isfinite(stop_value) or stop_value <= 0.0:
                raise ValueError(
                    f"initial_stop_price must be finite and > 0 when supplied, "
                    f"got {stop!r}; use None for 'not recorded'"
                )
            risk_per_unit = (float(self.entry_price) - stop_value) * direction
            if risk_per_unit <= 0.0:
                raise ValueError(
                    f"initial stop {stop_value!r} is not on the losing side of "
                    f"entry {self.entry_price!r} for a {self.side!r}: the risk "
                    "taken is zero or negative, so the R-multiple is undefined"
                )

        if self.entry_index is not None and self.exit_index is not None:
            if float(self.exit_index) < float(self.entry_index):
                raise ValueError(
                    f"exit_index {self.exit_index!r} precedes entry_index "
                    f"{self.entry_index!r}; the record is corrupt"
                )

        for name in ("mae_price", "mfe_price"):
            value = getattr(self, name)
            if value is not None and (
                not math.isfinite(float(value)) or float(value) <= 0.0
            ):
                raise ValueError(f"{name} must be finite and > 0 when supplied")

        if self.mae_price is not None and self.mfe_price is not None:
            span = (float(self.mfe_price) - float(self.mae_price)) * direction
            if span < 0.0:
                raise ValueError(
                    f"MFE {self.mfe_price!r} is on the adverse side of MAE "
                    f"{self.mae_price!r} for a {self.side!r}; the excursions "
                    "have been swapped"
                )

    # -- derived quantities ------------------------------------------------

    @property
    def direction(self) -> float:
        return direction_of(self.side)

    @property
    def fees(self) -> float:
        """Round-trip fees. Kept separate from PnL so the drag stays visible."""
        return float(self.entry_fee) + float(self.exit_fee)

    @property
    def gross_pnl(self) -> float:
        """PnL before fees, in quote currency."""
        return (
            (float(self.exit_price) - float(self.entry_price))
            * self.direction * float(self.qty)
        )

    @property
    def net_pnl(self) -> float:
        """PnL after both legs' fees. This is what the account actually got."""
        return self.gross_pnl - self.fees

    @property
    def risk_per_unit(self) -> Optional[float]:
        """``|entry - initial_stop|``, or ``None`` when no stop was recorded."""
        if self.initial_stop_price is None:
            return None
        return abs(float(self.entry_price) - float(self.initial_stop_price))

    @property
    def risk_amount(self) -> Optional[float]:
        """The denominator of R: risk per unit times size."""
        per_unit = self.risk_per_unit
        if per_unit is None:
            return None
        return per_unit * float(self.qty)

    @property
    def stop_distance_bps(self) -> Optional[float]:
        """Stop distance as basis points of the entry price."""
        per_unit = self.risk_per_unit
        if per_unit is None:
            return None
        return per_unit / float(self.entry_price) * 10_000.0

    @property
    def fee_bps(self) -> float:
        """Round-trip fees as basis points of entry notional."""
        notional = float(self.entry_price) * float(self.qty)
        return self.fees / notional * 10_000.0

    @property
    def r_gross(self) -> Optional[float]:
        """R before fees — what the payoff geometry did."""
        risk = self.risk_amount
        if risk is None:
            return None
        return self.gross_pnl / risk

    @property
    def r_net(self) -> Optional[float]:
        """R after fees — what the account got. The number to act on."""
        risk = self.risk_amount
        if risk is None:
            return None
        return self.net_pnl / risk

    @property
    def r_fees(self) -> Optional[float]:
        """Cost drag for this trade, in R. Exactly ``r_gross - r_net``."""
        risk = self.risk_amount
        if risk is None:
            return None
        return self.fees / risk

    @property
    def bars_held(self) -> Optional[float]:
        """Time to resolution, in whatever unit the indices are in."""
        if self.entry_index is None or self.exit_index is None:
            return None
        return float(self.exit_index) - float(self.entry_index)

    @property
    def mae_r(self) -> Optional[float]:
        """Maximum adverse excursion in R, as a positive magnitude.

        Clamped at zero rather than refused when the recorded extreme lies on
        the favourable side: that is not a corrupt record, it is a trade that
        never traded against its entry, and its adverse excursion is genuinely
        zero.
        """
        per_unit = self.risk_per_unit
        if per_unit is None or self.mae_price is None:
            return None
        adverse = (float(self.entry_price) - float(self.mae_price)) * self.direction
        return max(0.0, adverse) / per_unit

    @property
    def mfe_r(self) -> Optional[float]:
        """Maximum favourable excursion in R, as a positive magnitude."""
        per_unit = self.risk_per_unit
        if per_unit is None or self.mfe_price is None:
            return None
        favourable = (float(self.mfe_price) - float(self.entry_price)) * self.direction
        return max(0.0, favourable) / per_unit

    @property
    def is_win(self) -> Optional[bool]:
        """Fee-aware, matching ``persistence.TradeRecord.is_win``.

        A trade that made money before fees and lost after them is a LOSS. The
        legacy analytics counted it as a win, and that number fed Kelly.
        """
        r = self.r_net
        if r is None:
            return None
        return r > 0.0

    @property
    def canonical_reason(self) -> str:
        return canonical_exit_reason(self.exit_reason)

    # -- construction ------------------------------------------------------

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "Trade":
        """Build from a dict-like record, e.g. a ``StateStore`` trade row.

        Required keys: ``entry_price``, ``exit_price``, ``qty``, ``side`` (each
        with a small set of accepted spellings). Everything else is optional and
        absent means ``None``, never a plausible default.

        If the mapping *also* carries a ``gross_pnl``, it is cross-checked
        against the value recomputed from the prices, and a disagreement raises.
        That check exists because a wrong ``side`` is otherwise invisible: the
        PnL is right and the sign of every R is wrong.

        **On the time axis:** ``entry_index``/``exit_index`` may be bar indices
        or timestamps — ``opened_epoch``/``closed_epoch`` are accepted as the
        last aliases for exactly that reason. Time to resolution is then
        expressed in whatever unit was supplied, and the report labels it
        "bars held" regardless. Do not mix the two in one call: a sample half in
        bars and half in seconds produces a median that is neither.
        """
        def pick(field_name: str) -> Any:
            for alias in _FIELD_ALIASES[field_name]:
                if alias in row and row[alias] is not None:
                    return row[alias]
            return None

        missing = [
            name for name in ("entry_price", "exit_price", "qty", "side")
            if pick(name) is None
        ]
        if missing:
            raise ValueError(
                f"trade record is missing required field(s) {missing}; "
                f"available keys were {sorted(row.keys())}"
            )

        stop = pick("initial_stop_price")
        # A stop of exactly zero is how this stack spells "no stop recorded"
        # (``positions.stop_price`` defaults to 0.0 before the bracket is
        # placed), so it is read as unknown rather than as a stop at price zero.
        stop_value = None if stop is None or float(stop) <= 0.0 else float(stop)

        entry_index = pick("entry_index")
        exit_index = pick("exit_index")

        trade = cls(
            entry_price=float(pick("entry_price")),
            exit_price=float(pick("exit_price")),
            qty=float(pick("qty")),
            side=str(pick("side")),
            initial_stop_price=stop_value,
            entry_fee=float(pick("entry_fee") or 0.0),
            exit_fee=float(pick("exit_fee") or 0.0),
            entry_index=None if entry_index is None else float(entry_index),
            exit_index=None if exit_index is None else float(exit_index),
            exit_reason=str(pick("exit_reason") or "unknown"),
            mae_price=None if pick("mae_price") is None else float(pick("mae_price")),
            mfe_price=None if pick("mfe_price") is None else float(pick("mfe_price")),
            symbol=str(pick("symbol") or ""),
        )

        recorded = row.get("gross_pnl")
        if recorded is not None:
            expected = trade.gross_pnl
            tolerance = 1e-6 * max(1.0, abs(expected))
            if abs(float(recorded) - expected) > tolerance:
                raise ValueError(
                    f"recorded gross_pnl {float(recorded)!r} disagrees with the "
                    f"value implied by the prices and side ({expected!r}); the "
                    "record is internally inconsistent — most often a side that "
                    "was written the wrong way round"
                )
        return trade


TradeLike = Union[Trade, Mapping[str, Any]]


# ---------------------------------------------------------------------------
# report parts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Distribution:
    """Shape of a sample. Built only when there are enough observations."""

    n: int
    mean: float
    median: float
    p10: float
    p90: float
    minimum: float
    maximum: float

    @classmethod
    def of(
        cls,
        values: Sequence[float],
        minimum_n: int = MIN_TRADES_FOR_DISTRIBUTION,
    ) -> Optional["Distribution"]:
        """``None`` below ``minimum_n``, because deciles from six points are
        just the extremes with a percentile's name on them."""
        clean = [float(v) for v in values if v is not None and math.isfinite(float(v))]
        if len(clean) < minimum_n:
            return None
        array = np.asarray(clean, dtype=float)
        return cls(
            n=int(array.size),
            mean=float(array.mean()),
            median=float(np.percentile(array, 50)),
            p10=float(np.percentile(array, 10)),
            p90=float(np.percentile(array, 90)),
            minimum=float(array.min()),
            maximum=float(array.max()),
        )

    def compact(self, unit: str = "") -> str:
        return (
            f"n={self.n} median {self.median:,.2f}{unit} "
            f"p10 {self.p10:,.2f}{unit} p90 {self.p90:,.2f}{unit} "
            f"max {self.maximum:,.2f}{unit}"
        )


@dataclass(frozen=True)
class ExitReasonStats:
    """One row of the exit-reason breakdown.

    ``trades``, ``share`` and ``total_r`` are facts about the sample and are
    always populated. ``hit_rate`` and ``avg_r`` are estimates and are ``None``
    below ``MIN_TRADES_PER_BUCKET``.
    """

    reason: str
    trades: int
    share: float
    total_r: float
    hit_rate: Optional[float] = None
    avg_r: Optional[float] = None
    avg_bars_held: Optional[float] = None

    def line(self) -> str:
        rate = "n/a" if self.hit_rate is None else f"{self.hit_rate:.0%}"
        held = "n/a" if self.avg_bars_held is None else f"{self.avg_bars_held:.1f}"
        return (
            f"{self.reason:<12} {self.trades:>5} ({self.share:>5.1%})  "
            f"total {self.total_r:>+8.2f}R  hit {rate:>4}  bars {held:>6}"
        )


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TradeStats:
    """Payoff geometry, measured. ``None`` means "not enough data", never zero.

    The field this exists for is ``hit_rate_edge``: observed hit rate minus the
    break-even hit rate implied by the observed payoffs. Negative means the
    geometry loses money at the hit rate it actually achieved, which is a
    different — and more actionable — finding than "the return was negative".
    """

    trades: int = 0
    trades_with_r: int = 0
    trades_without_stop: int = 0
    wins: int = 0
    losses: int = 0

    # facts
    total_r_gross: float = 0.0
    total_r_net: float = 0.0
    total_r_fees: float = 0.0

    # estimates
    hit_rate: Optional[float] = None
    avg_win_r: Optional[float] = None
    avg_loss_r: Optional[float] = None          # positive magnitude
    payoff_ratio: Optional[float] = None
    break_even_hit_rate: Optional[float] = None
    hit_rate_edge: Optional[float] = None
    profit_factor: Optional[float] = None
    expectancy_r: Optional[float] = None        # after fees
    expectancy_r_gross: Optional[float] = None  # before fees
    expectancy_r_ci: Optional[Tuple[float, float]] = None
    expectancy_r_gross_ci: Optional[Tuple[float, float]] = None
    expectancy_excludes_zero: Optional[bool] = None
    confidence_level: float = CONFIDENCE_LEVEL

    # costs
    mean_stop_distance_bps: Optional[float] = None
    mean_fee_bps: Optional[float] = None
    realised_cost_drag_r: Optional[float] = None
    modelled_cost_drag_r: Optional[float] = None
    round_trip_cost_bps: Optional[float] = None

    # time to resolution
    bars_held: Optional[Distribution] = None
    bars_held_wins: Optional[Distribution] = None
    bars_held_losses: Optional[Distribution] = None
    winners_resolve_slower: Optional[bool] = None

    # excursions
    mae_r: Optional[Distribution] = None
    mfe_r: Optional[Distribution] = None
    excursion_coverage: int = 0

    r_net_distribution: Optional[Distribution] = None
    by_exit_reason: Dict[str, ExitReasonStats] = field(default_factory=dict)
    notes: Tuple[str, ...] = ()

    @property
    def insufficient_data(self) -> bool:
        """True when the sample cannot support the headline comparison."""
        return self.hit_rate_edge is None

    # -- reporting ---------------------------------------------------------

    def summary(self) -> str:
        """A report block in the style of ``BacktestResult.summary()``.

        Formatting rule: a statistic that is ``None`` prints ``n/a`` with the
        reason, never a zero. The gap between "we measured zero" and "we cannot
        say" is the entire point of the module.
        """
        width = 19

        def row(label: str, value: str) -> str:
            return f"{label:<{width}}: {value}"

        def continuation(value: str) -> str:
            """An unlabelled follow-on line, with no colon to read as a field."""
            return f"{'':<{width}}  {value}"

        lines: List[str] = ["-- payoff geometry (R = the risk actually taken) --"]

        if self.trades == 0:
            lines.append(row("R-multiples", "n/a (no closed trades)"))
            for note in self.notes:
                lines.append(row("note", note))
            return "\n".join(lines)

        lines.append(row(
            "trades with R",
            f"{self.trades_with_r} of {self.trades}"
            + (f" ({self.trades_without_stop} without a recorded stop)"
               if self.trades_without_stop else ""),
        ))
        lines.append(row(
            "hit rate (R)",
            f"{self.hit_rate:.1%}  ({self.wins}W / {self.losses}L)"
            if self.hit_rate is not None
            else f"n/a ({self.wins}W / {self.losses}L; needs "
                 f"{MIN_TRADES_FOR_RATIOS} trades)",
        ))
        lines.append(row(
            "avg win / loss",
            f"{self.avg_win_r:+.2f}R / {-self.avg_loss_r:+.2f}R"
            if self.avg_win_r is not None and self.avg_loss_r is not None
            else f"n/a (needs {MIN_OBSERVATIONS_FOR_MEAN} of each)",
        ))
        lines.append(row(
            "payoff ratio",
            f"{self.payoff_ratio:.2f}:1" if self.payoff_ratio is not None else "n/a",
        ))
        lines.append(row(
            "break-even hit",
            f"{self.break_even_hit_rate:.1%}  (implied by the payoff ratio)"
            if self.break_even_hit_rate is not None else "n/a",
        ))
        if self.hit_rate_edge is not None:
            verdict = "ABOVE" if self.hit_rate_edge > 0 else "BELOW"
            lines.append(row(
                "hit-rate edge",
                f"{self.hit_rate_edge * 100:+.1f} pts — the observed hit rate is "
                f"{verdict} break-even",
            ))
        else:
            lines.append(row("hit-rate edge", "n/a (sample too small to judge)"))
        lines.append(row(
            "profit factor",
            f"{self.profit_factor:.2f}" if self.profit_factor is not None else "n/a",
        ))
        lines.append(row(
            "expectancy gross",
            f"{self.expectancy_r_gross:+.4f}R"
            + (f"  {self.confidence_level:.0%} CI "
               f"[{self.expectancy_r_gross_ci[0]:+.3f}, "
               f"{self.expectancy_r_gross_ci[1]:+.3f}]"
               if self.expectancy_r_gross_ci else "")
            if self.expectancy_r_gross is not None
            else f"n/a (needs {MIN_TRADES_FOR_RATIOS} trades)",
        ))
        lines.append(row(
            "expectancy net",
            f"{self.expectancy_r:+.4f}R"
            + (f"  {self.confidence_level:.0%} CI "
               f"[{self.expectancy_r_ci[0]:+.3f}, {self.expectancy_r_ci[1]:+.3f}]"
               if self.expectancy_r_ci else "")
            if self.expectancy_r is not None
            else f"n/a (needs {MIN_TRADES_FOR_RATIOS} trades)",
        ))
        if self.expectancy_excludes_zero is False:
            lines.append(continuation(
                "the interval straddles zero: this sample cannot tell a "
                "positive edge from a negative one"
            ))
        lines.append(row(
            "cost drag",
            f"{self.realised_cost_drag_r:.4f}R per trade "
            f"({self.mean_fee_bps:.1f} bps of fees / "
            f"{self.mean_stop_distance_bps:.1f} bps of stop)"
            if self.realised_cost_drag_r is not None
            and self.mean_fee_bps is not None
            and self.mean_stop_distance_bps is not None
            else "n/a",
        ))
        if self.modelled_cost_drag_r is not None:
            lines.append(row(
                "cost drag (model)",
                f"{self.modelled_cost_drag_r:.4f}R at "
                f"{self.round_trip_cost_bps:.1f} bps round trip",
            ))
        lines.append(row(
            "R distribution",
            self.r_net_distribution.compact("R")
            if self.r_net_distribution is not None
            else f"n/a (needs {MIN_TRADES_FOR_DISTRIBUTION} trades)",
        ))
        lines.append(row(
            "bars held",
            self.bars_held.compact()
            if self.bars_held is not None else "n/a (no bar indices recorded)",
        ))
        if self.bars_held_wins is not None:
            lines.append(row("bars held (wins)",
                             f"n={self.bars_held_wins.n} "
                             f"median {self.bars_held_wins.median:,.1f}"))
        if self.bars_held_losses is not None:
            lines.append(row("bars held (losses)",
                             f"n={self.bars_held_losses.n} "
                             f"median {self.bars_held_losses.median:,.1f}"))
        if self.winners_resolve_slower is True:
            lines.append(continuation(
                "winners resolve SLOWER than losers — capital sits in the "
                "trades that pay least"))
        lines.append(row(
            "MAE / MFE",
            f"{self.mae_r.compact('R')}  |  {self.mfe_r.compact('R')}"
            if self.mae_r is not None and self.mfe_r is not None
            else f"n/a (excursion prices recorded for {self.excursion_coverage} "
                 f"of {self.trades} trades — not approximated)",
        ))
        if self.by_exit_reason:
            lines.append("by exit reason     :")
            for stats in sorted(
                self.by_exit_reason.values(), key=lambda s: -s.trades
            ):
                lines.append(f"  {stats.line()}")
        for note in self.notes:
            lines.append(row("note", note))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# the computation
# ---------------------------------------------------------------------------


def _bootstrap_mean_cis(
    series: Sequence[np.ndarray],
    *,
    level: float = CONFIDENCE_LEVEL,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> Optional[List[Tuple[float, float]]]:
    """Percentile-bootstrap CIs for the means of several aligned samples.

    All series are resampled with the **same** indices, so the gross and net
    intervals describe the same resampled worlds rather than two unrelated ones.
    Returns ``None`` below ``MIN_TRADES_FOR_RATIOS``: a confidence interval from
    eight trades is wide enough to contain any conclusion you like, which makes
    it decoration rather than evidence.
    """
    if not series:
        return None
    n = int(series[0].size)
    if n < MIN_TRADES_FOR_RATIOS:
        return None
    if any(int(s.size) != n for s in series):
        raise ValueError("bootstrap series must be aligned and equal length")

    rng = np.random.default_rng(seed)
    means = [np.empty(resamples, dtype=float) for _ in series]
    chunk = max(1, _BOOTSTRAP_CHUNK_ELEMENTS // max(1, n))
    done = 0
    while done < resamples:
        take = min(chunk, resamples - done)
        idx = rng.integers(0, n, size=(take, n))
        for target, values in zip(means, series):
            target[done:done + take] = values[idx].mean(axis=1)
        done += take

    lo_q = 100.0 * (1.0 - level) / 2.0
    hi_q = 100.0 * (1.0 + level) / 2.0
    return [
        (float(np.percentile(m, lo_q)), float(np.percentile(m, hi_q)))
        for m in means
    ]


def _as_trades(trades: Iterable[TradeLike]) -> List[Trade]:
    out: List[Trade] = []
    for item in trades:
        out.append(item if isinstance(item, Trade) else Trade.from_mapping(item))
    return out


def compute_trade_stats(
    trades: Iterable[TradeLike],
    *,
    round_trip_cost_bps: Optional[float] = None,
    confidence_level: float = CONFIDENCE_LEVEL,
    bootstrap_resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> TradeStats:
    """Compute every payoff-geometry statistic the sample can support.

    ``trades`` may be :class:`Trade` instances or mappings (a ``StateStore``
    trade row joined with its initial stop, for example). Mappings are validated
    on the way in, so a record that cannot be expressed in R raises here rather
    than becoming a plausible-looking number three functions later.

    ``round_trip_cost_bps``, when supplied, adds the *modelled* cost drag —
    ``cost_bps / mean_stop_bps`` — next to the realised one. The two answer
    different questions: realised drag is what these trades actually paid,
    modelled drag is what a stop of this width costs at an assumed fee schedule,
    and comparing them is how you find out whether the fee assumption is a
    number or a wish.
    """
    records = _as_trades(trades)
    notes: List[str] = []

    if not records:
        return TradeStats(notes=("no closed trades supplied",))

    with_r = [t for t in records if t.r_net is not None]
    without_stop = len(records) - len(with_r)
    if without_stop:
        notes.append(
            f"{without_stop} of {len(records)} trades carry no initial stop "
            "price and are excluded from every R statistic — their risk was "
            "not recorded, so their R is unknown rather than zero"
        )

    stats_kwargs: Dict[str, Any] = dict(
        trades=len(records),
        trades_with_r=len(with_r),
        trades_without_stop=without_stop,
        confidence_level=confidence_level,
        round_trip_cost_bps=(
            None if round_trip_cost_bps is None else float(round_trip_cost_bps)
        ),
    )

    if not with_r:
        notes.append(
            "no trade carries an initial stop price, so no R-multiple can be "
            "computed; record the stop at entry to make this measurable"
        )
        return TradeStats(notes=tuple(notes), **stats_kwargs)

    r_net = np.array([t.r_net for t in with_r], dtype=float)
    r_gross = np.array([t.r_gross for t in with_r], dtype=float)
    r_fees = np.array([t.r_fees for t in with_r], dtype=float)

    # Fee-aware, exactly as ``persistence.TradeRecord.is_win`` defines it: a
    # trade that made money gross and lost after fees is a LOSS. Zero is a loss
    # too, on the same convention ``performance_analytics`` uses — a scratch is
    # not a win.
    win_mask = r_net > 0.0
    wins = r_net[win_mask]
    losses = r_net[~win_mask]

    stats_kwargs.update(
        wins=int(wins.size),
        losses=int(losses.size),
        total_r_gross=float(r_gross.sum()),
        total_r_net=float(r_net.sum()),
        total_r_fees=float(r_fees.sum()),
        r_net_distribution=Distribution.of(r_net.tolist()),
    )

    n = int(r_net.size)

    # -- rates and ratios --------------------------------------------------
    hit_rate = avg_win = avg_loss = payoff = break_even = edge = None
    profit_factor = expectancy = expectancy_gross = None
    ci_net = ci_gross = None
    excludes_zero = None

    if n >= MIN_TRADES_FOR_RATIOS:
        hit_rate = float(wins.size) / n
        expectancy = float(r_net.mean())
        expectancy_gross = float(r_gross.mean())
        intervals = _bootstrap_mean_cis(
            [r_net, r_gross], level=confidence_level,
            resamples=bootstrap_resamples, seed=seed,
        )
        if intervals is not None:
            ci_net, ci_gross = intervals
            excludes_zero = not (ci_net[0] <= 0.0 <= ci_net[1])
            if not excludes_zero:
                notes.append(
                    f"the {confidence_level:.0%} interval for expectancy "
                    f"straddles zero at n={n}; this sample is consistent with "
                    "both a positive and a negative edge and is not a basis "
                    "for risking capital"
                )
        gross_wins = float(wins.sum())
        gross_losses = float(abs(losses.sum()))
        if gross_losses > 0.0:
            profit_factor = gross_wins / gross_losses
        elif gross_wins > 0.0:
            notes.append(
                "no losing trades in the sample; profit factor is undefined, "
                "not infinite"
            )
    else:
        notes.append(
            f"only {n} trades with an R-multiple; rates and ratios need "
            f"{MIN_TRADES_FOR_RATIOS} and are reported as None rather than as "
            "a number"
        )

    if wins.size >= MIN_OBSERVATIONS_FOR_MEAN:
        avg_win = float(wins.mean())
    if losses.size >= MIN_OBSERVATIONS_FOR_MEAN:
        avg_loss = float(abs(losses.mean()))

    if avg_win is not None and avg_loss is not None and avg_loss > 0.0:
        payoff = avg_win / avg_loss
        break_even = break_even_hit_rate(payoff)
        if break_even is not None and hit_rate is not None:
            edge = hit_rate - break_even
    elif avg_win is None or avg_loss is None:
        notes.append(
            f"a payoff ratio needs at least {MIN_OBSERVATIONS_FOR_MEAN} wins "
            f"and {MIN_OBSERVATIONS_FOR_MEAN} losses; this sample has "
            f"{wins.size} and {losses.size}"
        )

    stats_kwargs.update(
        hit_rate=hit_rate, avg_win_r=avg_win, avg_loss_r=avg_loss,
        payoff_ratio=payoff, break_even_hit_rate=break_even, hit_rate_edge=edge,
        profit_factor=profit_factor, expectancy_r=expectancy,
        expectancy_r_gross=expectancy_gross, expectancy_r_ci=ci_net,
        expectancy_r_gross_ci=ci_gross, expectancy_excludes_zero=excludes_zero,
    )

    # -- costs -------------------------------------------------------------
    stop_bps = np.array([t.stop_distance_bps for t in with_r], dtype=float)
    fee_bps = np.array([t.fee_bps for t in with_r], dtype=float)
    mean_stop_bps = float(stop_bps.mean())
    stats_kwargs.update(
        mean_stop_distance_bps=mean_stop_bps,
        mean_fee_bps=float(fee_bps.mean()),
        realised_cost_drag_r=float(r_fees.mean()),
    )
    if round_trip_cost_bps is not None:
        stats_kwargs["modelled_cost_drag_r"] = cost_drag_r(
            float(round_trip_cost_bps), mean_stop_bps
        )

    # -- time to resolution ------------------------------------------------
    held = [t.bars_held for t in with_r if t.bars_held is not None]
    held_wins = [
        t.bars_held for t in with_r
        if t.bars_held is not None and t.r_net is not None and t.r_net > 0.0
    ]
    held_losses = [
        t.bars_held for t in with_r
        if t.bars_held is not None and t.r_net is not None and t.r_net <= 0.0
    ]
    bars_all = Distribution.of(held)
    bars_wins = Distribution.of(held_wins, MIN_OBSERVATIONS_FOR_MEAN)
    bars_losses = Distribution.of(held_losses, MIN_OBSERVATIONS_FOR_MEAN)
    slower = None
    if bars_wins is not None and bars_losses is not None:
        slower = bars_wins.median > bars_losses.median
        if slower:
            notes.append(
                "winners take longer to resolve than losers "
                f"(median {bars_wins.median:.1f} vs {bars_losses.median:.1f} "
                "bars); capital is tied up longest in the trades that pay least"
            )
    if not held:
        notes.append(
            "no bar indices recorded, so time-to-resolution is unmeasured"
        )
    stats_kwargs.update(
        bars_held=bars_all, bars_held_wins=bars_wins,
        bars_held_losses=bars_losses, winners_resolve_slower=slower,
    )

    # -- excursions --------------------------------------------------------
    mae_values = [t.mae_r for t in with_r if t.mae_r is not None]
    mfe_values = [t.mfe_r for t in with_r if t.mfe_r is not None]
    coverage = sum(
        1 for t in with_r if t.mae_r is not None and t.mfe_r is not None
    )
    stats_kwargs.update(
        mae_r=Distribution.of(mae_values),
        mfe_r=Distribution.of(mfe_values),
        excursion_coverage=coverage,
    )
    if coverage == 0:
        notes.append(
            "no MAE/MFE prices recorded; excursions are reported as None "
            "rather than approximated from entry and exit prices"
        )
    elif coverage < len(with_r):
        notes.append(
            f"MAE/MFE available for {coverage} of {len(with_r)} trades; the "
            "distributions describe that subset only"
        )

    # -- exit reasons ------------------------------------------------------
    buckets: Dict[str, List[Trade]] = {}
    for trade in with_r:
        buckets.setdefault(trade.canonical_reason, []).append(trade)

    by_reason: Dict[str, ExitReasonStats] = {}
    for reason, group in buckets.items():
        group_r = np.array([t.r_net for t in group], dtype=float)
        group_held = [t.bars_held for t in group if t.bars_held is not None]
        big_enough = len(group) >= MIN_TRADES_PER_BUCKET
        by_reason[reason] = ExitReasonStats(
            reason=reason,
            trades=len(group),
            share=len(group) / n,
            total_r=float(group_r.sum()),
            hit_rate=float((group_r > 0.0).mean()) if big_enough else None,
            avg_r=float(group_r.mean()) if big_enough else None,
            avg_bars_held=(
                float(np.mean(group_held))
                if big_enough and len(group_held) >= MIN_TRADES_PER_BUCKET
                else None
            ),
        )
    stats_kwargs["by_exit_reason"] = by_reason

    return TradeStats(notes=tuple(notes), **stats_kwargs)
