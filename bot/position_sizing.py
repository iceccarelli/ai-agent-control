"""position_sizing.py — one equity-space sizing path.

WHAT REPLACED WHAT
==================
The as-received module (6,013 lines, retained at
``_dead/position_sizing_legacy.py``) contained **nine** independent sizing
implementations that disagreed with each other, four multiplier chains, eleven
places that silently enlarged a position to reach the exchange minimum, and a
Kelly function that floored a *proven losing edge* at 5% of capital.

The single worst behaviour, verified by running it::

    >>> calculate_kelly_criterion(win_rate=0.10, avg_win=1.0, avg_loss=5.0)
    0.05

A 10% win rate against a 1:5 payoff is a catastrophic edge. The legacy code
clamped the negative Kelly to 0.0 on one line and then raised it back to 0.05 on
the next (legacy:5933-5936), with no sample-size guard at all — two trades were
enough to drive live sizing, and *zero* trades returned 0.10.

Meanwhile the one statistically sound implementation, ``kelly_log_opt``
(bootstrapped expected-log-growth on post-cost returns, with a lower-confidence
quantile shrink), was computed into ``meta['kelly_fraction_override']`` and
**never read** — the key appears exactly once in 6,013 lines. It is the Kelly
engine here.

THE RULES THIS MODULE ENFORCES
------------------------------
1. **Size from risk, not from balance.** ``qty = equity * risk_fraction /
   stop_distance``. The legacy live path sized at a percentage of *balance*
   times leverage times uncapped confidence multipliers, which is unrelated to
   how much the trade can actually lose.
2. **Negative edge sizes to zero.** No floor. Ever.
3. **Sample-size guard.** Below the minimum sample count, Kelly is not used at
   all — the configured base risk applies. Kelly never *increases* risk beyond
   the configured cap; it can only shrink it.
4. **Never bump up to a minimum.** If the risk-correct size is below the
   exchange minimum, the trade is **skipped**. The legacy code enlarged it in
   eleven places, turning a rejected small trade into an oversized real one, and
   in one case turned a 10% scale-out into a 100% liquidation.
5. **Snap down, never up.** Quantities round DOWN to the step; prices round to
   the tick in the conservative direction.
6. **No fabricated inputs.** No $10,000 fallback balance, no 1.0 fallback price,
   no synthesised instrument filters. Missing input means size 0, logged.
7. **Fractions everywhere**, from the one config object. This module does not
   read the environment.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from enum import Enum
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

import config as _config
from risk_management import (
    BillionaireRiskManager,
    equity_risk_fraction,
    max_qty_for_risk_budget,
    normalize_side,
)

logger = logging.getLogger("sizing")

__all__ = [
    "MarketRegime",
    "SizingRequest",
    "SizingResult",
    "InstrumentFilters",
    "BillionairePositionSizing",
    "ExchangeAwarePositionSizer",
    "PositionSizer",
    "kelly_log_opt",
    "_golden_section_min",
    "kelly_fraction_from_returns",
    "calculate_position_size_with_kelly",
    "compute_size",
    "compute",
    "size_order",
    "eu_size_plan",
    "kelly_like_size",
    "plan_quantity",
    "clamp_to_exchange",
    "snap_qty_down",
    "snap_price",
    "get_profit_tracker",
    "cycles",
    "MIN_KELLY_SAMPLES",
]

#: Minimum number of closed, fee-inclusive trades before Kelly is consulted.
#: Below this the configured base risk applies unchanged.
#:
#: The mission specifies >= 30. Note that the legacy ``kelly_log_opt`` used 100,
#: which is the more defensible number: a Kelly fraction estimated from 30
#: samples has a very wide confidence interval, which is why the bootstrap here
#: takes the lower quantile rather than the mean. 30 is a floor, not a
#: recommendation.
MIN_KELLY_SAMPLES = 30


class MarketRegime(Enum):
    """Market regime labels. ``UNKNOWN`` is a first-class, honest answer."""

    BULL_TRENDING = "bull_trending"
    BEAR_TRENDING = "bear_trending"
    SIDEWAYS = "sideways"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"
    MOMENTUM = "momentum"
    MEAN_REVERSION = "mean_reversion"
    BREAKOUT = "breakout"
    CONSOLIDATION = "consolidation"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstrumentFilters:
    """Exchange trading rules for one symbol.

    There is deliberately **no default set**. The legacy code returned a
    hardcoded ``{tickSize: 0.01, stepSize: 0.001, minNotional: 5.0}`` for every
    symbol from a cache that nothing ever wrote (audit C14), so quantities were
    never actually quantised and the min-notional guard always passed. Real
    filters differ per symbol (ADA steps by 0.1; some symbols tick below a
    cent), so fabricated ones produce rejected or mis-sized orders — including
    on the *exit* side.

    ``from_exchange`` records provenance so a caller can assert that filters
    came from a real fetch.
    """

    symbol: str
    tick_size: Decimal
    qty_step: Decimal
    min_qty: Decimal
    min_notional: Decimal
    from_exchange: bool = False

    @classmethod
    def parse(cls, symbol: str, raw: Mapping[str, Any], *, from_exchange: bool = False):
        """Build from a Bybit V5 ``instruments-info`` payload. Raises on anything
        missing — a partial filter set is not usable and must not be guessed."""
        def need(*keys: str) -> Decimal:
            for k in keys:
                if k in raw and raw[k] not in (None, ""):
                    return Decimal(str(raw[k]))
            raise ValueError(
                f"instrument filters for {symbol} missing {keys!r}; refusing to "
                "substitute a default"
            )

        return cls(
            symbol=symbol,
            tick_size=need("tickSize", "tick_size", "price_tick"),
            qty_step=need("qtyStep", "stepSize", "qty_step", "basePrecision"),
            min_qty=need("minOrderQty", "minQty", "min_qty"),
            min_notional=need("minOrderAmt", "minNotional", "min_notional"),
            from_exchange=from_exchange,
        )


@dataclass
class SizingRequest:
    """Sizing inputs. Field names preserved from the legacy dataclass so the
    engine's construction site keeps working."""

    symbol: str
    signal_strength: float = 0.0
    signal_confidence: float = 0.0
    signal_quality: float = 0.5
    market_regime: MarketRegime = MarketRegime.UNKNOWN
    current_price: float = 0.0
    volatility: float = 0.0
    expected_return_pct: float = 0.0
    risk_reward_ratio: float = 0.0
    current_portfolio_value: float = 0.0
    current_positions: Dict[str, float] = field(default_factory=dict)
    liquidity_score: float = 1.0
    momentum_score: float = 0.0
    trend_strength: float = 0.0
    #: FRACTION of price between entry and stop (0.02 == 2% away).
    stop_loss_distance: float = 0.0
    #: FRACTION of equity this trade may risk.
    max_position_risk: float = 0.0
    side: str = "Buy"
    filters: Optional[InstrumentFilters] = None
    recent_returns: List[float] = field(default_factory=list)


@dataclass(frozen=True)
class SizingResult:
    """Sizing outcome. ``qty`` of 0.0 means *do not trade*, and ``reason`` says why.

    Zero is a legitimate, common answer. The legacy sizers treated a zero
    quantity as a problem to be corrected upward.
    """

    symbol: str
    qty: float = 0.0
    notional: float = 0.0
    risk_fraction: float = 0.0
    reason: str = "NOT_SIZED"
    method: str = "none"
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def position_size(self) -> float:
        return self.qty

    @property
    def should_trade(self) -> bool:
        return self.qty > 0.0

    def __bool__(self) -> bool:
        return self.should_trade


def _skip(symbol: str, reason: str, **detail: Any) -> SizingResult:
    logger.info("sizing skip %s: %s %s", symbol, reason, detail or "")
    return SizingResult(symbol=symbol, qty=0.0, reason=reason, detail=detail)


# ---------------------------------------------------------------------------
# exchange conformance — snap DOWN, never up
# ---------------------------------------------------------------------------


def snap_qty_down(qty: float, step: Decimal) -> Decimal:
    """Round a quantity DOWN to the exchange step.

    Down, always. Rounding up crosses the risk budget that produced the number.
    The legacy code used ``round()`` in one sizer and an explicit ceil-to-step
    in four others.
    """
    if step <= 0:
        raise ValueError(f"qty step must be positive, got {step}")
    q = Decimal(str(qty))
    if q <= 0:
        return Decimal("0")
    return (q / step).to_integral_value(rounding=ROUND_DOWN) * step


def snap_price(price: float, tick: Decimal, *, side: str, is_stop: bool = False) -> Decimal:
    """Round a price to the tick in the conservative direction.

    For an entry: a Buy rounds DOWN (do not pay more), a Sell rounds UP.
    For a stop: the direction inverts — a Buy's stop rounds UP (tighter, exits
    sooner), a Sell's stop rounds DOWN. Being conservative on a stop means
    accepting a slightly smaller loss, never a larger one.
    """
    if tick <= 0:
        raise ValueError(f"tick size must be positive, got {tick}")
    p = Decimal(str(price))
    direction = normalize_side(side)
    if direction is None:
        raise ValueError(f"cannot snap a price for an unknown side: {side!r}")
    round_up = (direction == "Sell") if not is_stop else (direction == "Buy")
    mode = ROUND_UP if round_up else ROUND_DOWN
    return (p / tick).to_integral_value(rounding=mode) * tick


def clamp_to_exchange(
    symbol: str, price: float, qty: float, filters: InstrumentFilters
) -> Tuple[float, str]:
    """Conform a quantity to the exchange rules.

    Returns ``(qty, reason)``. A quantity that cannot legally be traded comes
    back as ``0.0`` with a reason — it is **never** enlarged to become legal.
    """
    if price <= 0.0:
        return 0.0, "NO_PRICE"
    if qty <= 0.0:
        return 0.0, "ZERO_QTY"

    snapped = snap_qty_down(qty, filters.qty_step)
    if snapped <= 0:
        return 0.0, "BELOW_QTY_STEP"
    if snapped < filters.min_qty:
        # Bumping up to min_qty here is what the legacy code did. It converts a
        # size the risk engine deliberately kept small into a larger one it
        # never approved.
        return 0.0, "BELOW_MIN_QTY"

    notional = snapped * Decimal(str(price))
    if notional < filters.min_notional:
        return 0.0, "BELOW_MIN_NOTIONAL"
    return float(snapped), "OK"


# ---------------------------------------------------------------------------
# Kelly
# ---------------------------------------------------------------------------


def _golden_section_min(func, bounds, tol: float = 1e-6) -> float:
    """Minimise a unimodal function on ``[a, b]`` without scipy.

    Defined at module level, not inside an ``except ImportError`` block. The
    legacy codebase hid 89 definitions inside exception handlers; that pattern
    makes reachability depend on whether an import happened to fail, so this
    file does not use it even for a benign optional-dependency fallback.
    """
    a, b = float(bounds[0]), float(bounds[1])
    invphi = (math.sqrt(5.0) - 1.0) / 2.0
    invphi2 = (3.0 - math.sqrt(5.0)) / 2.0
    h = b - a
    if h <= tol:
        return (a + b) / 2.0
    n = max(1, int(math.ceil(math.log(tol / h) / math.log(invphi))))
    c, d = a + invphi2 * h, a + invphi * h
    yc, yd = func(c), func(d)
    for _ in range(n):
        if yc < yd:
            b, d, yd = d, c, yc
            h *= invphi
            c = a + invphi2 * h
            yc = func(c)
        else:
            a, c, yc = c, d, yd
            h *= invphi
            d = a + invphi * h
            yd = func(d)
    return ((a + d) / 2.0) if yc < yd else ((c + b) / 2.0)


try:  # optional dependency; both branches bind the SAME module-level name
    from scipy.optimize import minimize_scalar as _scipy_minimize_scalar
except ImportError:
    _scipy_minimize_scalar = None


def _argmin_bounded(func, lo: float, hi: float) -> float:
    """Argmin of ``func`` on ``[lo, hi]``, via scipy when available."""
    if _scipy_minimize_scalar is not None:
        return float(_scipy_minimize_scalar(func, bounds=(lo, hi), method="bounded").x)
    return _golden_section_min(func, (lo, hi))


def kelly_log_opt(
    returns: Sequence[float],
    f_cap: float = 0.50,
    conf_q: float = 0.25,
    n_boot: int = 1000,
    rng: Optional[Any] = None,
    frac_of_f: float = 0.25,
    hard_cap: float = 0.30,
    min_samples: int = MIN_KELLY_SAMPLES,
) -> float:
    """Conservative Kelly fraction via expected log growth on post-cost returns.

    Ported essentially unchanged from the legacy module (legacy:4604-4709),
    where it was the only statistically sound sizing math in the file and its
    result was written to a dict key that nothing read. Two changes:

    * the sample-size floor is a parameter rather than a hardcoded 100;
    * ``returns`` **must** already be net of fees and slippage — enforced by
      construction now, because the only caller reads them from
      ``StateStore.net_returns()``, which is fee-inclusive by definition.

    Returns 0.0 for insufficient data, an all-zero sample, or a non-positive
    edge. There is no floor.
    """
    r = np.asarray(list(returns), dtype=float)
    r = r[np.isfinite(r)]
    if r.size < int(min_samples) or np.allclose(r, 0.0):
        return 0.0

    f_cap = float(max(0.0, f_cap))
    conf_q = float(min(max(conf_q, 0.0), 1.0))
    frac_of_f = float(max(0.0, frac_of_f))
    hard_cap = float(max(0.0, hard_cap))

    def _neg_log_growth(f: float, sample: np.ndarray) -> float:
        if f <= 0.0:
            return 0.0
        f = min(f, f_cap)
        x = np.clip(1.0 + f * sample, 1e-6, None)
        return -float(np.mean(np.log(x)))

    def _opt_f(sample: np.ndarray) -> float:
        return _argmin_bounded(lambda f: _neg_log_growth(f, sample), 0.0, f_cap)

    gen = rng if rng is not None else np.random.default_rng()
    Fs = np.empty(int(n_boot), dtype=float)
    for i in range(int(n_boot)):
        Fs[i] = _opt_f(gen.choice(r, size=r.size, replace=True))

    f_star = float(np.quantile(Fs, conf_q))
    f_star = min(frac_of_f * f_star, hard_cap)
    if not np.isfinite(f_star) or f_star <= 0.0:
        return 0.0
    return float(f_star)


def kelly_fraction_from_returns(
    returns: Sequence[float], *, cap: float, min_samples: int = MIN_KELLY_SAMPLES
) -> Tuple[float, str]:
    """Risk fraction to use, and why.

    Kelly can only ever **shrink** the configured cap, never raise it. That
    asymmetry is the point: a strategy with a good recent run must not be
    allowed to talk the risk engine into a bigger position than the operator
    configured.
    """
    clean = [float(x) for x in returns if math.isfinite(float(x))]
    if len(clean) < int(min_samples):
        return cap, f"BASE_RISK (only {len(clean)} trades, need {min_samples})"
    if sum(clean) <= 0.0:
        # Negative expectancy. Size zero. The legacy code floored this at 5%.
        return 0.0, "NEGATIVE_EDGE"
    f = kelly_log_opt(clean, min_samples=min_samples)
    if f <= 0.0:
        return 0.0, "KELLY_ZERO"
    return min(f, cap), f"KELLY({f:.4f}) capped at {cap:.4f}"


# ---------------------------------------------------------------------------
# the sizer
# ---------------------------------------------------------------------------


class BillionairePositionSizing:
    """The one sizing path: equity-space risk sizing with hard caps.

    ``risk_manager`` is optional; when supplied, its persisted trade history
    drives Kelly and its caps are consulted, so sizing and gating cannot
    disagree about the limits.
    """

    def __init__(
        self,
        config: Any = None,
        risk_manager: Optional[BillionaireRiskManager] = None,
        **_ignored: Any,
    ) -> None:
        self.cfg = config if config is not None else _config.get_config_object()
        self.risk_manager = risk_manager
        c = self.cfg
        self.max_position_size_pct = float(c.MAX_POSITION_SIZE_PCT)
        self.max_total_exposure_pct = float(c.MAX_TOTAL_EXPOSURE_PCT)
        self.risk_per_trade_pct = float(
            getattr(c, "RISK_PER_TRADE_PCT", min(0.01, self.max_position_size_pct))
        )
        self.max_leverage = float(getattr(c, "MAX_LEVERAGE_EU", 1))
        self.min_kelly_samples = int(
            getattr(c, "MIN_KELLY_SAMPLES", MIN_KELLY_SAMPLES)
        )
        # Kept as an attribute purely so legacy call sites that read
        # `sizer.config[...]` keep working; it is a view, not a second config.
        self.config: Dict[str, Any] = {
            "RISK_PER_TRADE": self.risk_per_trade_pct,
            "MAX_POSITION_PCT": self.max_position_size_pct,
        }

    # -- the core ----------------------------------------------------------

    def calculate_position_size(self, request: SizingRequest) -> SizingResult:
        """Size a trade from its risk, then clamp it to every hard cap.

        Order of operations matters: risk first, caps second, exchange
        conformance last. Each stage can only reduce the number.
        """
        symbol = str(getattr(request, "symbol", "") or "")
        if not symbol:
            return _skip("", "NO_SYMBOL")

        price = float(getattr(request, "current_price", 0.0) or 0.0)
        equity = float(getattr(request, "current_portfolio_value", 0.0) or 0.0)
        stop_fraction = float(getattr(request, "stop_loss_distance", 0.0) or 0.0)

        # -- inputs must be real ------------------------------------------
        if price <= 0.0 or not math.isfinite(price):
            return _skip(symbol, "NO_PRICE", price=price)
        if equity <= 0.0 or not math.isfinite(equity):
            # Legacy substituted $10,000 here.
            return _skip(symbol, "NO_EQUITY", equity=equity)
        if stop_fraction <= 0.0 or not math.isfinite(stop_fraction):
            # No stop distance means no risk denominator. Sizing without one is
            # what let the legacy path divide by an all-zero ATR.
            return _skip(symbol, "NO_STOP_DISTANCE", stop_distance=stop_fraction)
        if stop_fraction >= 1.0:
            return _skip(symbol, "STOP_DISTANCE_NOT_A_FRACTION",
                         stop_distance=stop_fraction)

        filters = getattr(request, "filters", None)
        if filters is None:
            # No fabricated filters. Without them the quantity cannot be made
            # legal, and guessing is audit finding C14.
            return _skip(symbol, "NO_INSTRUMENT_FILTERS")

        # -- risk budget ---------------------------------------------------
        requested_risk = float(getattr(request, "max_position_risk", 0.0) or 0.0)
        cap = self.risk_per_trade_pct
        if requested_risk > 0.0:
            # The caller may ask for LESS risk than configured, never more.
            cap = min(cap, requested_risk)

        returns = self._recent_returns()
        risk_fraction, kelly_reason = kelly_fraction_from_returns(
            returns, cap=cap, min_samples=self.min_kelly_samples
        )
        if risk_fraction <= 0.0:
            return _skip(symbol, "NO_RISK_BUDGET", kelly=kelly_reason,
                         samples=len(returns))

        # -- size from risk ------------------------------------------------
        stop_distance_abs = price * stop_fraction
        qty = max_qty_for_risk_budget(
            entry_price=price,
            stop_price=price - stop_distance_abs,
            equity=equity,
            risk_fraction=risk_fraction,
        )
        if qty <= 0.0:
            return _skip(symbol, "ZERO_QTY_FROM_RISK")
        method = "equity_risk"

        # -- hard caps (each may only reduce) ------------------------------
        qty, cap_reason = self._apply_caps(symbol, qty, price, equity)
        if qty <= 0.0:
            return _skip(symbol, cap_reason, price=price, equity=equity)

        # -- exchange conformance -------------------------------------------
        qty, conform_reason = clamp_to_exchange(symbol, price, qty, filters)
        if qty <= 0.0:
            # Skip, never bump up.
            return _skip(symbol, conform_reason, price=price)

        realised_risk = equity_risk_fraction(
            qty=qty, entry_price=price,
            stop_price=price - stop_distance_abs, equity=equity,
        )
        return SizingResult(
            symbol=symbol,
            qty=qty,
            notional=qty * price,
            risk_fraction=realised_risk,
            reason="SIZED",
            method=method,
            detail={
                "kelly": kelly_reason,
                "risk_budget": risk_fraction,
                "caps": cap_reason,
                "samples": len(returns),
            },
        )

    # Legacy alias.
    def compute_position_size(self, request: SizingRequest) -> SizingResult:
        return self.calculate_position_size(request)

    def _apply_caps(
        self, symbol: str, qty: float, price: float, equity: float
    ) -> Tuple[float, str]:
        """Per-position, aggregate and correlation-bucket notional caps.

        Returns ``(qty, reason)``. Every cap reduces; none raises.
        """
        reasons: List[str] = []

        # per-position notional
        max_qty_position = (equity * self.max_position_size_pct) / price
        if qty > max_qty_position:
            qty = max_qty_position
            reasons.append("position_cap")

        rm = self.risk_manager
        if rm is not None:
            try:
                positions = rm.store.open_positions()
            except Exception:  # noqa: BLE001
                # Cannot read exposure => cannot prove the cap holds => no size.
                logger.exception("exposure unreadable while sizing %s", symbol)
                return 0.0, "EXPOSURE_UNREADABLE"

            current = sum(
                abs(float(p.get("qty", 0.0)) * float(p.get("entry_price", 0.0)))
                for p in positions
            )
            headroom = equity * self.max_total_exposure_pct - current
            if headroom <= 0.0:
                return 0.0, "AGGREGATE_EXPOSURE_FULL"
            if qty * price > headroom:
                qty = headroom / price
                reasons.append("aggregate_cap")

            bucket = rm.correlation.bucket_of(symbol)
            by_bucket = rm.correlation.exposure_by_bucket(positions)
            bucket_headroom = (
                equity * rm.max_correlation_exposure_pct - by_bucket.get(bucket, 0.0)
            )
            if bucket_headroom <= 0.0:
                return 0.0, "CORRELATION_BUCKET_FULL"
            if qty * price > bucket_headroom:
                qty = bucket_headroom / price
                reasons.append("correlation_cap")

        return qty, "+".join(reasons) if reasons else "none"

    def _recent_returns(self) -> List[float]:
        """Fee-inclusive per-trade returns from persisted history, or empty."""
        rm = self.risk_manager
        if rm is None:
            return []
        try:
            return list(rm.store.net_returns())
        except Exception:  # noqa: BLE001
            logger.exception("could not read trade history; sizing without Kelly")
            return []


class ExchangeAwarePositionSizer(BillionairePositionSizing):
    """Kept for import compatibility.

    The legacy subclass existed to bump quantities UP to the exchange minimum
    (legacy:4469-4475). Exchange conformance now happens inside the base sizer
    and only ever rounds down, so this subclass adds nothing and overrides
    nothing.
    """


PositionSizer = BillionairePositionSizing


# ---------------------------------------------------------------------------
# module-level entry points other modules import
# ---------------------------------------------------------------------------


def plan_quantity(
    symbol: str,
    side: str,
    price: float,
    equity: float,
    stop_distance_fraction: float,
    filters: Optional[InstrumentFilters] = None,
    risk_manager: Optional[BillionaireRiskManager] = None,
    **_extra: Any,
) -> SizingResult:
    """Functional entry point used by ``trading_engine``."""
    sizer = BillionairePositionSizing(risk_manager=risk_manager)
    return sizer.calculate_position_size(
        SizingRequest(
            symbol=symbol, side=side, current_price=price,
            current_portfolio_value=equity,
            stop_loss_distance=stop_distance_fraction, filters=filters,
        )
    )


def compute_size(*a: Any, **kw: Any) -> SizingResult:
    """Legacy name. Delegates to the one sizer so the two cannot disagree."""
    return plan_quantity(*a, **kw)


def compute(*a: Any, **kw: Any) -> SizingResult:
    return plan_quantity(*a, **kw)


def size_order(*a: Any, **kw: Any) -> SizingResult:
    return plan_quantity(*a, **kw)


def eu_size_plan(*a: Any, **kw: Any) -> SizingResult:
    """Legacy EU-cap planner. The EU notional cap is enforced by the config
    limits and the risk manager's caps, so this is the same path."""
    return plan_quantity(*a, **kw)


def kelly_like_size(*a: Any, **kw: Any) -> SizingResult:
    return plan_quantity(*a, **kw)


def calculate_position_size_with_kelly(
    symbol: str = "",
    price: float = 0.0,
    equity: float = 0.0,
    stop_distance_fraction: float = 0.0,
    filters: Optional[InstrumentFilters] = None,
    risk_manager: Optional[BillionaireRiskManager] = None,
    **_extra: Any,
) -> SizingResult:
    """The name ``main.py`` calls. Same single path; Kelly is applied inside it.

    The legacy function of this name returned 10% of capital when there was no
    history at all, and 5% on a proven losing edge.
    """
    return plan_quantity(
        symbol, "Buy", price, equity, stop_distance_fraction,
        filters=filters, risk_manager=risk_manager,
    )


class _ProfitTracker:
    """Read-only view of persisted performance. No in-memory shadow state."""

    def __init__(self, risk_manager: Optional[BillionaireRiskManager] = None) -> None:
        self.risk_manager = risk_manager

    def net_returns(self) -> List[float]:
        if self.risk_manager is None:
            return []
        try:
            return list(self.risk_manager.store.net_returns())
        except Exception:  # noqa: BLE001
            return []

    def win_rate(self) -> Optional[float]:
        """Fee-aware win rate, or None when there is not enough history.

        None, not 0.5. The legacy tracker returned 0.0 for an empty history,
        which the Kelly path then read as "0% win rate" and answered with a 10%
        position.
        """
        returns = self.net_returns()
        if len(returns) < MIN_KELLY_SAMPLES:
            return None
        return sum(1 for r in returns if r > 0.0) / len(returns)


def get_profit_tracker(
    risk_manager: Optional[BillionaireRiskManager] = None, **_x: Any
) -> _ProfitTracker:
    return _ProfitTracker(risk_manager)


def cycles(*_a: Any, **_k: Any) -> int:
    """Legacy counter hook. Returns 0; nothing in the live path reads it."""
    return 0
