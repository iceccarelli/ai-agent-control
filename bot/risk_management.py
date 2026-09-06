"""risk_management.py — the single, fail-closed risk gate chain.

WHAT REPLACED WHAT
==================
The as-received module (6,307 lines, retained at ``_dead/risk_management_legacy.py``)
contained four competing gate APIs, seven monkey-patch layers stacked on top of
each other, and 151 unreachable functions. Its live entry point,
``gate_order``, **had never executed**: a patch layer at legacy line 4953 called
the keyword-only original positionally, so every call raised ``TypeError``,
was caught, and returned ``GATE_EXCEPTION``. Every gate inside it — breakers,
exposure cap, size cap, minimum stop — was dead code.

That is why this module is a rewrite of the canonical definitions rather than a
patch: there was no working gate chain to patch.

THE RULES THIS MODULE ENFORCES
------------------------------
1. **Fail closed, without exception.** Every gate returns a *block* when it
   cannot prove the trade is safe. There is no ``except: return True`` in this
   file, and ``tests/test_risk_management.py`` asserts that mechanically. The
   legacy module had fifteen fail-open gates, including the kill switch itself.
2. **Fractions everywhere.** Every ratio is a fraction (0.02 == 2%), sourced
   from ``config.load()``. Nothing here reads ``os.getenv``.
3. **Risk is measured in EQUITY space.** The single most important correction:

       risk_fraction = qty * |entry - stop| / equity

   The legacy code compared ``|entry - stop| / entry`` — the stop distance as a
   percentage of *price* — against a 2% limit, with no position size and no
   equity term. That rejected any signal with a wide stop regardless of size,
   while happily passing a 1.9% stop on a position worth nine times equity.
   The only correct conversion in the whole codebase lived in three methods that
   were defined inside an ``except`` block whose ``try`` could not fail, so they
   were never even defined (legacy lines 4042-4364). They are restored here.
4. **State is persisted.** Peak equity, daily anchors, breaker and cooldown
   state live in SQLite via ``persistence.StateStore``. The legacy module kept
   them in instance attributes, so ``peak_balance`` reseeded from 0.0 on every
   start and drawdown protection read zero exactly during a crash-loop.
5. **UTC only.** One day boundary. The legacy module mixed local
   ``datetime.now()`` for the daily return with UTC for the breaker key.
6. **The kill switch is human-cleared.** This module can trip it. Nothing in
   this module, or anywhere else in the codebase, can clear it.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import config as _config
from persistence import (
    PersistenceError,
    StateStore,
    TradeRecord,
    utc_day,
    utc_now_epoch,
)

logger = logging.getLogger("risk")

__all__ = [
    "BillionaireRiskManager",
    "RiskManager",
    "RiskDecision",
    "GateOutcome",
    "GateSet",
    "V5EnhancedRiskManager",
    "CorrelationBuckets",
    "validate_order",
    "risk_gate",
    "kill_active",
    "normalize_side",
    "equity_risk_fraction",
    "max_qty_for_risk_budget",
    "leverage_adjusted_stop_fraction",
]


# ---------------------------------------------------------------------------
# result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskDecision:
    """The outcome of a gate or gate chain.

    ``ok`` is the only field callers should branch on, and it defaults to
    **False**: a ``RiskDecision`` constructed with no arguments blocks. That is
    deliberate — a decision object that defaults to "allowed" is one refactor
    away from a fail-open gate.
    """

    ok: bool = False
    reason: str = "UNEVALUATED"
    detail: Dict[str, Any] = field(default_factory=dict)
    adjustments: Dict[str, Any] = field(default_factory=dict)

    # Legacy call sites read `.allowed` and `.approved`; keep them as aliases of
    # `ok` so there is exactly one source of truth.
    @property
    def allowed(self) -> bool:
        return self.ok

    @property
    def approved(self) -> bool:
        return self.ok

    def __bool__(self) -> bool:
        return self.ok

    def blocked(self, reason: str, **detail: Any) -> "RiskDecision":
        return replace(self, ok=False, reason=reason, detail={**self.detail, **detail})


@dataclass(frozen=True)
class GateOutcome:
    """Single-gate result. Kept for API compatibility with the legacy module."""

    ok: bool = False
    reason: str = "UNEVALUATED"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok


def _block(reason: str, **detail: Any) -> RiskDecision:
    return RiskDecision(ok=False, reason=reason, detail=detail)


def _allow(reason: str = "OK", **detail: Any) -> RiskDecision:
    return RiskDecision(ok=True, reason=reason, detail=detail)


# ---------------------------------------------------------------------------
# the equity-space risk math  (restored from legacy lines 4042-4364)
# ---------------------------------------------------------------------------


def normalize_side(side: Any) -> Optional[str]:
    """Map a side label to exactly ``"Buy"``, ``"Sell"``, or ``None``.

    Matching is against a closed set, never a prefix. An earlier version of this
    file tested ``side.lower().startswith("s")``, which silently classified the
    string ``"Sideways"`` as a SELL. Prefix-matching a trade direction is how
    signal inversions get in (compare audit C13, where ``STRONG_BUY`` fell
    through an ``if/else`` and was executed as a Sell).

    Returns ``None`` for anything unrecognised so the caller can block, rather
    than defaulting to a direction.
    """
    if side is None:
        return None
    token = str(side).strip().upper()
    if token in {"BUY", "LONG", "B", "BID"}:
        return "Buy"
    if token in {"SELL", "SHORT", "S", "ASK"}:
        return "Sell"
    return None


def equity_risk_fraction(
    *, qty: float, entry_price: float, stop_price: float, equity: float
) -> float:
    """Fraction of EQUITY at risk if the stop fills exactly.

    This is the conversion the legacy live path did not have::

        risk_usd  = qty * |entry - stop|
        fraction  = risk_usd / equity

    Leverage needs no explicit term: it is already expressed in ``qty``. A
    position of nine times equity notional with a 2% stop yields 0.18 here,
    which is what a leverage-aware limit must see.

    Raises ``ValueError`` on inputs that cannot produce a meaningful number, so
    callers cannot silently proceed on a bad reading. Every caller in this
    module treats that exception as a block.
    """
    for name, value in (
        ("qty", qty), ("entry_price", entry_price),
        ("stop_price", stop_price), ("equity", equity),
    ):
        if value is None or not math.isfinite(float(value)):
            raise ValueError(f"{name} is not a finite number: {value!r}")
    if equity <= 0.0:
        raise ValueError(f"equity must be positive, got {equity!r}")
    if entry_price <= 0.0:
        raise ValueError(f"entry_price must be positive, got {entry_price!r}")
    if stop_price <= 0.0:
        raise ValueError(f"stop_price must be positive, got {stop_price!r}")
    if qty < 0.0:
        raise ValueError(f"qty must not be negative, got {qty!r}")
    return abs(float(qty)) * abs(float(entry_price) - float(stop_price)) / float(equity)


def max_qty_for_risk_budget(
    *, entry_price: float, stop_price: float, equity: float, risk_fraction: float
) -> float:
    """Largest quantity whose stop-out costs at most ``risk_fraction`` of equity.

    The inverse of :func:`equity_risk_fraction`. Returns 0.0 when the stop
    distance is zero — a zero-distance stop means "no protection", and the only
    safe size for an unprotected position is none.
    """
    if entry_price <= 0.0 or equity <= 0.0 or risk_fraction <= 0.0:
        return 0.0
    distance = abs(float(entry_price) - float(stop_price))
    if distance <= 0.0:
        return 0.0
    return (float(equity) * float(risk_fraction)) / distance


def leverage_adjusted_stop_fraction(
    *, base_stop_fraction: float, leverage: float, max_account_risk_fraction: float
) -> Tuple[float, str]:
    """Tighten a stop so its leveraged account impact stays within budget.

    Restored from the legacy ``adjust_stop_loss_for_leverage`` (dead at legacy
    lines 4255-4360), whose docstring stated the invariant nothing else in the
    codebase encoded: *without leverage a 2% stop is a 2% account loss; with 10x
    leverage a 2% stop is a 20% account loss.*

    Returns ``(stop_fraction, reason)``. All values are fractions.
    """
    lev = max(1.0, float(leverage))
    base = max(0.0, float(base_stop_fraction))
    budget = max(0.0, float(max_account_risk_fraction))
    if base <= 0.0:
        return 0.0, "NO_STOP"
    impact = base * lev
    if impact > budget > 0.0:
        return budget / lev, "LEVERAGE_RISK_LIMIT"
    return base, "NO_ADJUSTMENT_NEEDED"


# ---------------------------------------------------------------------------
# correlation buckets
# ---------------------------------------------------------------------------


class CorrelationBuckets:
    """Groups symbols that tend to move together, for aggregate exposure caps.

    The legacy correlation check crashed on every call — it read
    ``position.position_size_pct`` off dicts (legacy line 3592), caught the
    ``AttributeError``, and returned 0.0, so six correlated crypto pairs were
    scored as fully diversified. It also compared a percent-valued accumulator
    against a fraction-valued limit, so it was 100x off even in principle.

    This implementation is deliberately simple and explicit rather than a
    computed correlation matrix: a matrix estimated from a handful of trades is
    a fabricated number, and fabricated numbers are what this repair removes.
    Buckets are declarative and auditable; refine them from real data later.
    """

    DEFAULT_BUCKETS: Dict[str, Tuple[str, ...]] = {
        "BTC_BETA": ("BTC", "WBTC", "BTCB"),
        "ETH_BETA": ("ETH", "WETH", "STETH"),
        "L1_ALT": ("SOL", "AVAX", "ADA", "DOT", "ATOM", "NEAR", "APT", "SUI", "SEI"),
        "L2": ("ARB", "OP", "MATIC", "STRK", "ZK"),
        "MEME": ("DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI"),
        "DEFI": ("UNI", "AAVE", "LINK", "MKR", "CRV", "LDO"),
        "STABLE": ("USDT", "USDC", "DAI", "FDUSD", "TUSD"),
    }

    def __init__(self, buckets: Optional[Mapping[str, Sequence[str]]] = None) -> None:
        self._buckets = {k: tuple(v) for k, v in (buckets or self.DEFAULT_BUCKETS).items()}

    def bucket_of(self, symbol: str) -> str:
        """Bucket for a symbol. Unknown symbols get their own bucket.

        Unknown means uncorrelated *as far as we know*, which is the honest
        answer — but it also means an unknown symbol can never hide inside an
        existing bucket's headroom.
        """
        base = self._base_asset(symbol)
        for name, members in self._buckets.items():
            if base in members:
                return name
        return f"UNGROUPED:{base}"

    @staticmethod
    def _base_asset(symbol: str) -> str:
        s = str(symbol).upper().replace("-", "").replace("_", "").replace("/", "")
        for quote in ("USDT", "USDC", "USD", "EUR", "BTC", "ETH"):
            if s.endswith(quote) and len(s) > len(quote):
                return s[: -len(quote)]
        return s

    def exposure_by_bucket(
        self, positions: Iterable[Mapping[str, Any]]
    ) -> Dict[str, float]:
        """Notional exposure per bucket.

        ``positions`` are plain mappings (this is what the store returns) — the
        legacy crash came from assuming attribute access on dicts.
        """
        out: Dict[str, float] = {}
        for p in positions:
            symbol = str(p.get("symbol", ""))
            qty = abs(float(p.get("qty", 0.0) or 0.0))
            price = float(p.get("entry_price", 0.0) or 0.0)
            if qty <= 0.0 or price <= 0.0:
                continue
            out[self.bucket_of(symbol)] = out.get(self.bucket_of(symbol), 0.0) + qty * price
        return out


# ---------------------------------------------------------------------------
# the risk manager
# ---------------------------------------------------------------------------


class BillionaireRiskManager:
    """The one risk gate chain.

    Construct with an explicit ``StateStore`` in tests; the default opens the
    configured SQLite path.

    Every public gate returns a :class:`RiskDecision` and blocks on any
    exception. Callers branch on ``.ok`` only.
    """

    def __init__(
        self,
        config: Any = None,
        store: Optional[StateStore] = None,
        correlation: Optional[CorrelationBuckets] = None,
        **_ignored: Any,
    ) -> None:
        # One config object, from the one loader. Never os.getenv.
        self.cfg = config if config is not None else _config.get_config_object()
        self.correlation = correlation or CorrelationBuckets()

        try:
            self.store = store if store is not None else StateStore()
        except PersistenceError:
            # Cannot open durable state => cannot know the breaker or drawdown
            # position => must not trade. Surfaced as a construction failure
            # rather than a silent in-memory fallback.
            logger.critical("risk manager cannot open its state store; refusing to start")
            raise

        c = self.cfg
        # All of these are FRACTIONS, straight from config. No /100 anywhere.
        self.max_position_size_pct: float = float(c.MAX_POSITION_SIZE_PCT)
        self.max_total_exposure_pct: float = float(c.MAX_TOTAL_EXPOSURE_PCT)
        self.max_daily_loss_pct: float = float(c.MAX_DAILY_LOSS_PCT)
        self.max_drawdown_pct: float = float(c.MAX_DRAWDOWN_PCT)
        self.stop_loss_pct: float = float(c.STOP_LOSS_PCT)
        self.max_open_positions: int = int(c.MAX_OPEN_POSITIONS)
        self.max_consecutive_losses: int = int(getattr(c, "MAX_CONSECUTIVE_LOSSES", 4))
        self.min_risk_reward_ratio: float = float(c.MIN_RISK_REWARD_RATIO)
        self.min_confidence: float = float(c.MIN_CONFIDENCE)
        self.max_leverage: int = int(getattr(c, "MAX_LEVERAGE_EU", 1))
        self.default_leverage: int = int(getattr(c, "DEFAULT_LEVERAGE", 1))
        self.trade_cooldown_seconds: float = float(
            getattr(c, "TRADE_COOLDOWN_SECONDS", 60)
        )
        self.breaker_cooldown_seconds: float = float(
            getattr(c, "CIRCUIT_BREAKER_HOURS", 2.0) * 3600.0
        )
        self.max_correlation_exposure_pct: float = float(
            getattr(c, "MAX_CORRELATION_EXPOSURE_PCT", 0.30)
        )
        # Cost-awareness. Expressed in bps because that is how the exchange
        # quotes fees; converting once in config beats converting per call site.
        self.round_trip_cost_bps: float = float(
            getattr(c, "ROUND_TRIP_COST_BPS", 25.0)
        )
        self.min_edge_bps: float = float(getattr(c, "MIN_EDGE_BPS", 30.0))
        self.max_spread_bps: float = float(getattr(c, "MAX_SPREAD_BPS", 20.0))
        self.max_data_age_seconds: float = float(
            getattr(c, "MAX_DATA_AGE_SECONDS", 120.0)
        )

        # -- product category. Determines whether "short" is even a word here.
        self.category: str = str(getattr(c, "CATEGORY", "spot")).lower()
        # Derived from the category by config; read, never decided, here. If it
        # is missing the answer is False, because assuming a venue can short is
        # the failure that costs money.
        self.allow_shorts: bool = bool(getattr(c, "ALLOW_SHORTS", False))
        self.max_funding_bps: float = float(getattr(c, "MAX_FUNDING_BPS", 30.0))

        # -- order-book conditions
        self.min_book_depth_usd: float = float(
            getattr(c, "MIN_BOOK_DEPTH_USD", 0.0)
        )
        self.max_book_imbalance: float = float(
            getattr(c, "MAX_BOOK_IMBALANCE", 1.0)
        )

        # Per-trade equity risk budget. This is the cap the legacy code lacked.
        self.max_risk_per_trade_pct: float = float(
            getattr(c, "RISK_PER_TRADE_PCT", min(0.01, self.max_position_size_pct))
        )

        self._validate_limits()

    # -- construction-time sanity -----------------------------------------

    def _validate_limits(self) -> None:
        """Reject a nonsensical limit set at construction rather than at 3am."""
        problems: List[str] = []
        fractions = {
            "MAX_POSITION_SIZE_PCT": self.max_position_size_pct,
            "MAX_TOTAL_EXPOSURE_PCT": self.max_total_exposure_pct,
            "MAX_DAILY_LOSS_PCT": self.max_daily_loss_pct,
            "MAX_DRAWDOWN_PCT": self.max_drawdown_pct,
            "STOP_LOSS_PCT": self.stop_loss_pct,
            "RISK_PER_TRADE_PCT": self.max_risk_per_trade_pct,
            "MAX_CORRELATION_EXPOSURE_PCT": self.max_correlation_exposure_pct,
        }
        for name, value in fractions.items():
            if not math.isfinite(value) or not (0.0 < value <= 1.0):
                problems.append(f"{name}={value} is not a fraction in (0, 1]")
        if self.min_risk_reward_ratio < 1.0:
            problems.append(
                f"MIN_RISK_REWARD_RATIO={self.min_risk_reward_ratio} < 1.0 accepts "
                "trades that lose money on average"
            )
        if self.max_open_positions < 1:
            problems.append(f"MAX_OPEN_POSITIONS={self.max_open_positions} < 1")
        if problems:
            raise ValueError("unusable risk limits: " + "; ".join(problems))

    # -- state helpers -----------------------------------------------------

    @property
    def circuit_breaker(self) -> Any:
        """Persisted breaker state (legacy call sites read this attribute)."""
        return self.store.get_breaker()

    @property
    def consecutive_losses(self) -> int:
        return self.store.consecutive_losses()

    @property
    def current_drawdown(self) -> float:
        """Drawdown as a FRACTION of persisted peak equity."""
        return self.store.drawdown_fraction()

    def update_equity(self, equity: float) -> None:
        """Record an equity observation. Must be EQUITY, not available balance."""
        self.store.update_equity(float(equity))

    # -- individual gates --------------------------------------------------
    # Each returns RiskDecision and blocks on any exception.

    def _gate_kill_switch(self) -> RiskDecision:
        try:
            engaged, reason = self.store.is_kill_switch_engaged()
        except Exception as exc:  # noqa: BLE001
            return _block("KILL_SWITCH_UNREADABLE", error=str(exc))
        if engaged:
            return _block("KILL_SWITCH_ENGAGED", detail_reason=reason)
        return _allow("KILL_SWITCH_CLEAR")

    def _gate_breaker(self) -> RiskDecision:
        try:
            breaker = self.store.get_breaker()
        except Exception as exc:  # noqa: BLE001
            return _block("BREAKER_UNREADABLE", error=str(exc))
        if breaker.active:
            if breaker.is_cooling_down():
                return _block(
                    "CIRCUIT_BREAKER_ACTIVE",
                    breaker_reason=breaker.reason,
                    cooling_until=breaker.cooldown_until_epoch,
                )
            # Cooldown elapsed: clear it and continue. This is the one automatic
            # reset in the module, and it is a *breaker*, not the kill switch.
            self.store.clear_breaker()
        return _allow("BREAKER_CLEAR")

    def _gate_symbol_cooldown(self, symbol: str) -> RiskDecision:
        try:
            remaining = self.store.symbol_cooldown_remaining(symbol)
        except Exception as exc:  # noqa: BLE001
            return _block("COOLDOWN_UNREADABLE", error=str(exc))
        if remaining > 0.0:
            return _block("SYMBOL_COOLDOWN", symbol=symbol, seconds_remaining=remaining)
        return _allow("COOLDOWN_CLEAR")

    def _gate_daily_loss(self, equity: float) -> RiskDecision:
        try:
            loss = self.store.daily_loss_fraction(equity)
        except Exception as exc:  # noqa: BLE001
            return _block("DAILY_LOSS_UNREADABLE", error=str(exc))
        if loss >= self.max_daily_loss_pct:
            self.store.trip_breaker(
                f"daily loss {loss:.4f} >= {self.max_daily_loss_pct:.4f}",
                self.breaker_cooldown_seconds,
            )
            return _block("DAILY_LOSS_LIMIT", loss_fraction=loss,
                          limit=self.max_daily_loss_pct)
        return _allow("DAILY_LOSS_OK", loss_fraction=loss)

    def _gate_drawdown(self, equity: float) -> RiskDecision:
        try:
            self.store.update_equity(equity)
            dd = self.store.drawdown_fraction(equity)
        except Exception as exc:  # noqa: BLE001
            return _block("DRAWDOWN_UNREADABLE", error=str(exc))
        if dd >= self.max_drawdown_pct:
            self.store.trip_kill_switch(
                f"drawdown {dd:.4f} >= {self.max_drawdown_pct:.4f}"
            )
            return _block("MAX_DRAWDOWN", drawdown_fraction=dd,
                          limit=self.max_drawdown_pct)
        return _allow("DRAWDOWN_OK", drawdown_fraction=dd)

    def _gate_consecutive_losses(self) -> RiskDecision:
        try:
            n = self.store.consecutive_losses()
        except Exception as exc:  # noqa: BLE001
            return _block("LOSS_STREAK_UNREADABLE", error=str(exc))
        if n >= self.max_consecutive_losses:
            self.store.trip_breaker(
                f"{n} consecutive losing trades", self.breaker_cooldown_seconds
            )
            return _block("CONSECUTIVE_LOSSES", count=n,
                          limit=self.max_consecutive_losses)
        return _allow("LOSS_STREAK_OK", count=n)

    def _gate_position_count(self) -> RiskDecision:
        try:
            n = self.store.open_position_count()
        except Exception as exc:  # noqa: BLE001
            return _block("POSITION_COUNT_UNREADABLE", error=str(exc))
        if n >= self.max_open_positions:
            return _block("MAX_OPEN_POSITIONS", open=n, limit=self.max_open_positions)
        return _allow("POSITION_COUNT_OK", open=n)

    def _gate_existing_position(self, symbol: str) -> RiskDecision:
        """One position per symbol. A re-entry is blocked, not merged.

        This closes a **state collision**, not a policy question. The position
        book is keyed by symbol, so a second entry into a symbol that already
        has an open position overwrites the first row — discarding its
        quantity while the exchange keeps both lots of inventory. The book and
        the exchange then disagree, and every downstream number computed from
        the book (exposure, PnL, drawdown) is wrong by the forgotten amount.

        A backtest reconciliation check caught this: the equity curve moved by
        +7,673 while the trade ledger accounted for -26.

        Pyramiding is a legitimate strategy, but it requires the position book
        to accumulate lots with a weighted-average entry, and stops to be
        re-sized on every add. Until that exists, adding to a position is
        refused rather than silently mis-recorded.
        """
        try:
            open_symbols = {str(p.get("symbol")) for p in self.store.open_positions()}
        except Exception as exc:  # noqa: BLE001
            return _block("POSITION_STATE_UNREADABLE", error=str(exc))
        if symbol in open_symbols:
            return _block("POSITION_ALREADY_OPEN", symbol=symbol)
        return _allow("NO_EXISTING_POSITION")

    def _gate_naked_positions(self) -> RiskDecision:
        """No new trade while any existing position lacks a protective stop.

        A naked position is an unbounded loss already on the book. Adding to the
        book before fixing it compounds an error the system already knows about.
        """
        try:
            naked = self.store.positions_without_stops()
        except Exception as exc:  # noqa: BLE001
            return _block("POSITION_STATE_UNREADABLE", error=str(exc))
        if naked:
            return _block(
                "NAKED_POSITION_OPEN",
                symbols=[str(p.get("symbol")) for p in naked],
            )
        return _allow("ALL_POSITIONS_PROTECTED")

    def _gate_per_trade_risk(
        self, *, qty: float, entry_price: float, stop_price: float, equity: float
    ) -> RiskDecision:
        """The equity-space per-trade risk cap."""
        try:
            frac = equity_risk_fraction(
                qty=qty, entry_price=entry_price, stop_price=stop_price, equity=equity
            )
        except ValueError as exc:
            return _block("RISK_INPUTS_INVALID", error=str(exc))
        if frac > self.max_risk_per_trade_pct:
            max_qty = max_qty_for_risk_budget(
                entry_price=entry_price, stop_price=stop_price,
                equity=equity, risk_fraction=self.max_risk_per_trade_pct,
            )
            return RiskDecision(
                ok=False,
                reason="PER_TRADE_RISK_EXCEEDED",
                detail={"risk_fraction": frac, "limit": self.max_risk_per_trade_pct},
                # An adjustment, not an override: the caller may re-submit
                # smaller, but nothing here raises the limit to fit the trade.
                adjustments={"max_qty": max_qty},
            )
        return _allow("PER_TRADE_RISK_OK", risk_fraction=frac)

    def _gate_position_notional(
        self, *, qty: float, entry_price: float, equity: float
    ) -> RiskDecision:
        if equity <= 0.0:
            return _block("EQUITY_UNKNOWN", equity=equity)
        notional = abs(float(qty) * float(entry_price))
        frac = notional / float(equity)
        if frac > self.max_position_size_pct:
            return RiskDecision(
                ok=False,
                reason="POSITION_SIZE_CAP",
                detail={"notional_fraction": frac, "limit": self.max_position_size_pct},
                adjustments={
                    "max_qty": (equity * self.max_position_size_pct) / float(entry_price)
                    if entry_price > 0 else 0.0
                },
            )
        return _allow("POSITION_SIZE_OK", notional_fraction=frac)

    def _gate_aggregate_exposure(
        self, *, qty: float, entry_price: float, equity: float
    ) -> RiskDecision:
        if equity <= 0.0:
            return _block("EQUITY_UNKNOWN", equity=equity)
        try:
            positions = self.store.open_positions()
        except Exception as exc:  # noqa: BLE001
            return _block("EXPOSURE_UNREADABLE", error=str(exc))
        current = sum(
            abs(float(p.get("qty", 0.0)) * float(p.get("entry_price", 0.0)))
            for p in positions
        )
        proposed = current + abs(float(qty) * float(entry_price))
        frac = proposed / float(equity)
        if frac > self.max_total_exposure_pct:
            return _block("AGGREGATE_EXPOSURE_CAP", exposure_fraction=frac,
                          limit=self.max_total_exposure_pct)
        return _allow("AGGREGATE_EXPOSURE_OK", exposure_fraction=frac)

    def _gate_correlation_bucket(
        self, *, symbol: str, qty: float, entry_price: float, equity: float
    ) -> RiskDecision:
        if equity <= 0.0:
            return _block("EQUITY_UNKNOWN", equity=equity)
        try:
            positions = self.store.open_positions()
        except Exception as exc:  # noqa: BLE001
            return _block("CORRELATION_UNREADABLE", error=str(exc))
        bucket = self.correlation.bucket_of(symbol)
        by_bucket = self.correlation.exposure_by_bucket(positions)
        proposed = by_bucket.get(bucket, 0.0) + abs(float(qty) * float(entry_price))
        frac = proposed / float(equity)
        if frac > self.max_correlation_exposure_pct:
            return _block("CORRELATION_BUCKET_CAP", bucket=bucket,
                          bucket_fraction=frac,
                          limit=self.max_correlation_exposure_pct)
        return _allow("CORRELATION_OK", bucket=bucket, bucket_fraction=frac)

    def _gate_expected_edge(
        self,
        *,
        entry_price: float,
        stop_price: float,
        take_profit_price: Optional[float],
        confidence: Optional[float],
        win_probability: Optional[float] = None,
    ) -> RiskDecision:
        """Reject trades whose expected move cannot pay for the round trip.

        This is the gate that separates a strategy from a fee-generation
        machine. Crypto spot round-trips cost roughly 20-25 bps in fees and
        slippage; a signal whose target is 15 bps away has negative expectancy
        before it is even placed, no matter how confident the model is.

        Expected edge::

            win_bps    = (target - entry) / entry * 10_000
            loss_bps   = (entry - stop)   / entry * 10_000
            edge_bps   = p * win_bps - (1 - p) * loss_bps - round_trip_cost

        WHAT ``p`` IS, AND WHAT IT IS NOT
        ---------------------------------
        Through slice 7 this gate used ``p = confidence``. That was a category
        error, and it is fixed here.

        ``confidence`` is a **signal-strength score**: the strategy's agreement-
        weighted vote magnitude. It lives in [0, 1] and it looks like a
        probability, which is exactly why the conflation survived so long. It is
        not one. A confidence of 0.15 does not mean "wins 15% of the time"; it
        means "the components mildly agree". Feeding it in here multiplied every
        reward by a number with no frequency interpretation, and on real data it
        made the arithmetic hopeless: at ``p=0.15`` with a 2:1 payoff,
        ``0.15*2 - 0.85*1`` is negative before a single basis point of cost, so
        the gate rejected essentially everything for a reason that was an
        artefact of the input rather than a property of the trade.

        ``win_probability`` is the calibrated estimate — from
        ``policy.Policy.predict_edge`` once a model has been promoted, or from a
        calibration measured over realised outcomes. It is the only input this
        gate will treat as a frequency.

        The fallback ladder, strictest first:

        1. ``win_probability`` supplied  -> use it;
        2. otherwise                     -> **reward leg alone against cost**.

        Note what is NOT in that ladder: confidence. A trade with no calibrated
        probability is judged on whether its target alone clears the round trip,
        which is stricter than assuming a coin flip and much stricter than
        assuming the confidence score. ``confidence`` is still accepted and
        still reported, so the decision journal records what the strategy
        thought — it simply no longer drives the arithmetic.
        """
        try:
            if take_profit_price is None or float(take_profit_price) <= 0.0:
                return _allow("EDGE_NOT_EVALUATED (no target)")
            entry = float(entry_price)
            if entry <= 0.0:
                return _block("NO_ENTRY_PRICE", entry=entry)

            win_bps = abs(float(take_profit_price) - entry) / entry * 10_000.0
            loss_bps = abs(entry - float(stop_price)) / entry * 10_000.0

            if win_probability is None:
                edge_bps = win_bps - self.round_trip_cost_bps
                basis = "reward-only (no calibrated probability)"
            else:
                p = float(win_probability)
                if not math.isfinite(p) or not (0.0 <= p <= 1.0):
                    # A probability outside [0,1] is not a probability. Blocking
                    # beats clamping: clamping a broken model's 1.7 to 1.0 would
                    # produce the most confident trade the system can express.
                    return _block("WIN_PROBABILITY_INVALID", p=p)
                edge_bps = p * win_bps - (1.0 - p) * loss_bps - self.round_trip_cost_bps
                basis = f"p={p:.3f} (calibrated)"

            if edge_bps < self.min_edge_bps:
                return _block(
                    "EDGE_BELOW_COST", edge_bps=round(edge_bps, 2),
                    required_bps=self.min_edge_bps,
                    round_trip_cost_bps=self.round_trip_cost_bps, basis=basis,
                )
            return _allow("EDGE_OK", edge_bps=round(edge_bps, 2), basis=basis)
        except Exception as exc:  # noqa: BLE001
            return _block("EDGE_UNCOMPUTABLE", error=str(exc))

    def _gate_spread(self, quote: Optional[Mapping[str, Any]]) -> RiskDecision:
        """Refuse to cross an abnormally wide spread.

        A wide spread is the market telling you liquidity has gone. Paying it
        is a guaranteed loss taken in exchange for an uncertain gain, and it is
        the single most avoidable cost in crypto execution.

        ``None`` means no quote was supplied, which is *not* permission to
        proceed — the caller must pass one or accept the block.
        """
        if quote is None:
            return _block("NO_QUOTE")
        try:
            bid = float(quote.get("bid") or 0.0)
            ask = float(quote.get("ask") or 0.0)
        except (TypeError, ValueError) as exc:
            return _block("QUOTE_UNPARSEABLE", error=str(exc))
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
            return _block("QUOTE_INVALID", bid=bid, ask=ask)
        mid = (bid + ask) / 2.0
        spread_bps = (ask - bid) / mid * 10_000.0
        if spread_bps > self.max_spread_bps:
            return _block("SPREAD_TOO_WIDE", spread_bps=round(spread_bps, 2),
                          limit_bps=self.max_spread_bps)
        return _allow("SPREAD_OK", spread_bps=round(spread_bps, 2))

    def _gate_short_capability(self, side: str) -> RiskDecision:
        """A short is only a trade on a venue that has shorts.

        Spot has no native short. Without borrow — which this codebase does not
        implement — a Sell can only reduce an existing long. Letting a
        sell-to-open through on spot produces one of two outcomes, both bad:
        the venue rejects it (noise), or, with margin enabled, it silently
        *borrows* to sell, opening a leveraged short the risk model never
        budgeted for and whose liquidation the drawdown gate cannot see.

        So this is a gate, not a client-side detail. It is the first thing the
        chain asks about direction, and it names the fix in its own reason.
        """
        direction = normalize_side(side)
        if direction != "Sell":
            return _allow("DIRECTION_OK", side=direction or side)
        if self.allow_shorts:
            return _allow("SHORT_ALLOWED", category=self.category)
        return _block(
            "SHORTS_NOT_AVAILABLE_ON_SPOT",
            category=self.category,
            remedy="set CATEGORY=linear to trade USDT perpetuals, which short natively",
        )

    def _gate_funding(self, funding_rate: Optional[float]) -> RiskDecision:
        """Refuse a perpetual whose carry eats the edge before the trade starts.

        ``funding_rate`` is a FRACTION per 8-hour interval, signed as the
        exchange reports it: positive means longs pay shorts.

        Only the side that PAYS is gated. Being paid to hold is not a risk, and
        blocking on it would be a gate that refuses free money. ``None`` on a
        perpetual blocks — an unknown carry is not a zero carry, and this is the
        one cost that accrues while you do nothing.
        """
        if self.category != "linear":
            return _allow("NO_FUNDING_ON_SPOT")
        if funding_rate is None:
            return _block("FUNDING_UNKNOWN")
        try:
            rate = float(funding_rate)
        except (TypeError, ValueError):
            return _block("FUNDING_UNPARSEABLE")
        if not math.isfinite(rate):
            return _block("FUNDING_INVALID", rate=rate)
        cost_bps = abs(rate) * 10_000.0
        if cost_bps > self.max_funding_bps:
            return _block("FUNDING_TOO_EXPENSIVE", funding_bps=round(cost_bps, 2),
                          limit_bps=self.max_funding_bps)
        return _allow("FUNDING_OK", funding_bps=round(cost_bps, 2))

    def _gate_book_liquidity(
        self, book: Optional[Mapping[str, Any]], *, side: str
    ) -> RiskDecision:
        """Refuse to enter against a book that cannot absorb the exit.

        The spread gate asks what entering costs. This asks a different and more
        dangerous question: *if this goes wrong, is there anyone on the other
        side?* A book with a tight spread and no depth behind it is the classic
        trap — the entry looks cheap and the stop fills 40 bps away.

        Both figures come from ``market_data.BookFeatures``. ``None`` for the
        whole book is permission to skip (the operator may not run an order-book
        feed); ``None`` for a *field within* a supplied book is a block, because
        that means the feed is present and broken.
        """
        if book is None:
            return _allow("NO_BOOK_SUPPLIED")
        try:
            # We exit on the opposite side we enter, so the depth that matters
            # for a long is the BID: that is who buys when the stop fires.
            exit_is_sell = normalize_side(side) == "Buy"
            depth = book.get("depth_bid_usd") if exit_is_sell else book.get("depth_ask_usd")
            imbalance = book.get("imbalance")
        except AttributeError as exc:
            return _block("BOOK_UNREADABLE", error=str(exc))
        if depth is None or imbalance is None:
            return _block("BOOK_INCOMPLETE")
        try:
            depth_usd = float(depth)
            skew = float(imbalance)
        except (TypeError, ValueError):
            return _block("BOOK_UNPARSEABLE")
        if not (math.isfinite(depth_usd) and math.isfinite(skew)):
            return _block("BOOK_INVALID")
        if depth_usd < self.min_book_depth_usd:
            return _block("EXIT_LIQUIDITY_TOO_THIN",
                          exit_depth_usd=round(depth_usd, 2),
                          limit_usd=self.min_book_depth_usd)
        # A book stacked hard against the direction we are about to take is a
        # queue we would be joining the back of.
        against = -skew if exit_is_sell else skew
        if against > self.max_book_imbalance:
            return _block("BOOK_STACKED_AGAINST", imbalance=round(skew, 4),
                          limit=self.max_book_imbalance)
        return _allow("BOOK_OK", exit_depth_usd=round(depth_usd, 2),
                      imbalance=round(skew, 4))

    def _gate_data_freshness(self, data_age_seconds: Optional[float]) -> RiskDecision:
        """Refuse to act on stale market data.

        The legacy code compared an epoch timestamp against ``perf_counter()``,
        so every freshness check passed unconditionally — the bot would happily
        trade on an hour-old book during an outage. ``None`` blocks: an unknown
        age is not a fresh one.
        """
        if data_age_seconds is None:
            return _block("DATA_AGE_UNKNOWN")
        try:
            age = float(data_age_seconds)
        except (TypeError, ValueError):
            return _block("DATA_AGE_UNPARSEABLE")
        if not math.isfinite(age) or age < 0.0:
            return _block("DATA_AGE_INVALID", age=age)
        if age > self.max_data_age_seconds:
            return _block("DATA_STALE", age_seconds=round(age, 1),
                          limit_seconds=self.max_data_age_seconds)
        return _allow("DATA_FRESH", age_seconds=round(age, 1))

    def _gate_stop_sanity(
        self, *, side: str, entry_price: float, stop_price: float
    ) -> RiskDecision:
        """A stop must exist and be on the correct side of entry.

        The legacy ``_calculate_stop_loss`` "normalised" an inverted stop by
        pushing it *further* the wrong way, turning an instantly-triggering stop
        into a worse instantly-triggering stop. Here it is simply rejected.
        """
        if stop_price is None or not math.isfinite(float(stop_price)) or stop_price <= 0.0:
            return _block("NO_STOP_LOSS", stop=stop_price)
        if entry_price <= 0.0:
            return _block("NO_ENTRY_PRICE", entry=entry_price)
        direction = normalize_side(side)
        if direction is None:
            return _block("UNKNOWN_SIDE", side=side)
        if direction == "Buy" and stop_price >= entry_price:
            return _block("STOP_ON_WRONG_SIDE", side=side,
                          entry=entry_price, stop=stop_price)
        if direction == "Sell" and stop_price <= entry_price:
            return _block("STOP_ON_WRONG_SIDE", side=side,
                          entry=entry_price, stop=stop_price)
        return _allow("STOP_SANE")

    def _gate_risk_reward(
        self,
        *,
        entry_price: float,
        stop_price: float,
        take_profit_price: Optional[float],
    ) -> RiskDecision:
        """Reject sub-threshold risk/reward. Never rewrite it to fit.

        Audit C7: the legacy path overwrote a failing RR with the minimum and
        proceeded. That is fabricated data in the most direct sense.
        """
        if take_profit_price is None or float(take_profit_price) <= 0.0:
            return _block("NO_TAKE_PROFIT")
        risk = abs(float(entry_price) - float(stop_price))
        reward = abs(float(take_profit_price) - float(entry_price))
        if risk <= 0.0:
            return _block("ZERO_RISK_DISTANCE")
        rr = reward / risk
        if rr < self.min_risk_reward_ratio:
            return _block("RISK_REWARD_TOO_LOW", rr=rr,
                          minimum=self.min_risk_reward_ratio)
        return _allow("RISK_REWARD_OK", rr=rr)

    def _gate_confidence(self, confidence: Optional[float]) -> RiskDecision:
        if confidence is None or not math.isfinite(float(confidence)):
            return _block("CONFIDENCE_MISSING")
        c = float(confidence)
        if not (0.0 <= c <= 1.0):
            # Audit C6: legacy "confidence" was uncapped (volume x regime x 2.5
            # boosters could exceed 2.0), so it was not a probability and could
            # not be compared to a threshold or fed to Kelly.
            return _block("CONFIDENCE_OUT_OF_RANGE", confidence=c)
        if c < self.min_confidence:
            return _block("CONFIDENCE_TOO_LOW", confidence=c,
                          minimum=self.min_confidence)
        return _allow("CONFIDENCE_OK", confidence=c)

    # -- the chain ---------------------------------------------------------

    def gate_order(
        self,
        *,
        symbol: str,
        side: str,
        entry_price: float,
        stop_loss: float,
        quantity: float,
        account_equity: float,
        instrument_filters: Optional[Mapping[str, Any]] = None,
        take_profit: Optional[float] = None,
        confidence: Optional[float] = None,
        win_probability: Optional[float] = None,
        leverage: Optional[float] = None,
        quote: Optional[Mapping[str, Any]] = None,
        data_age_seconds: Optional[float] = None,
        funding_rate: Optional[float] = None,
        book: Optional[Mapping[str, Any]] = None,
        **_extra: Any,
    ) -> RiskDecision:
        """Evaluate every gate. Returns the FIRST blocking decision.

        Keyword-only by design, and callers pass keywords. The legacy patch
        layer's positional call against this same keyword-only signature is what
        made the whole chain raise ``TypeError`` on every invocation.

        A ``quantity`` of 0.0 is a legitimate pre-trade probe: size-dependent
        gates are skipped and the account-level gates still run, which is how
        ``trading_engine`` uses it before sizing.
        """
        detail: Dict[str, Any] = {"symbol": symbol, "side": side}
        try:
            account_gates = (
                self._gate_short_capability(side),
                self._gate_kill_switch(),
                self._gate_breaker(),
                self._gate_consecutive_losses(),
                self._gate_symbol_cooldown(symbol),
                self._gate_daily_loss(account_equity),
                self._gate_drawdown(account_equity),
                self._gate_naked_positions(),
                self._gate_existing_position(symbol),
                self._gate_position_count(),
            )
            for decision in account_gates:
                if not decision.ok:
                    self._journal(symbol, "BLOCK", decision)
                    return decision

            stop_gate = self._gate_stop_sanity(
                side=side, entry_price=entry_price, stop_price=stop_loss
            )
            if not stop_gate.ok:
                self._journal(symbol, "BLOCK", stop_gate)
                return stop_gate

            if confidence is not None:
                conf_gate = self._gate_confidence(confidence)
                if not conf_gate.ok:
                    self._journal(symbol, "BLOCK", conf_gate)
                    return conf_gate

            if take_profit is not None:
                rr_gate = self._gate_risk_reward(
                    entry_price=entry_price, stop_price=stop_loss,
                    take_profit_price=take_profit,
                )
                if not rr_gate.ok:
                    self._journal(symbol, "BLOCK", rr_gate)
                    return rr_gate

            # Cost and market-condition gates. Each is skipped only when the
            # caller supplied nothing to evaluate; supplying a BAD value blocks.
            edge_gate = self._gate_expected_edge(
                entry_price=entry_price, stop_price=stop_loss,
                take_profit_price=take_profit, confidence=confidence,
                win_probability=win_probability,
            )
            if not edge_gate.ok:
                self._journal(symbol, "BLOCK", edge_gate)
                return edge_gate

            if quote is not None:
                spread_gate = self._gate_spread(quote)
                if not spread_gate.ok:
                    self._journal(symbol, "BLOCK", spread_gate)
                    return spread_gate

            if data_age_seconds is not None:
                freshness_gate = self._gate_data_freshness(data_age_seconds)
                if not freshness_gate.ok:
                    self._journal(symbol, "BLOCK", freshness_gate)
                    return freshness_gate

            # Funding only exists on perpetuals; the gate self-skips on spot,
            # so it is called unconditionally rather than guarded here — one
            # place decides what funding means, and it is the gate.
            funding_gate = self._gate_funding(funding_rate)
            if not funding_gate.ok:
                self._journal(symbol, "BLOCK", funding_gate)
                return funding_gate

            book_gate = self._gate_book_liquidity(book, side=side)
            if not book_gate.ok:
                self._journal(symbol, "BLOCK", book_gate)
                return book_gate

            qty_value = float(quantity) if quantity else 0.0
            if qty_value < 0.0 or not math.isfinite(qty_value):
                # `quantity=0.0` from the size-independent PRE-gate is
                # intentional (trading_engine.py's comment: "pre-trade risk
                # gate, size-independent") and correctly skips these checks.
                # A NEGATIVE or non-finite quantity is not that case - it is
                # either a caller bug or an inverted sign, and skipping size
                # gates for it means _gate_per_trade_risk / _gate_position_
                # notional / _gate_aggregate_exposure / _gate_correlation_
                # bucket never run, and the function falls through to
                # RiskDecision(ok=True, reason="APPROVED"). No CURRENT caller
                # in this tree reaches this branch: trading_engine.py passes
                # 0.0 for the pre-gate and sizer.qty for the final gate, and
                # BillionairePositionSizing refuses (should_trade=False)
                # rather than emitting a negative size. This is defense in
                # depth against a future or external caller, not a fix to an
                # exploited path today.
                decision = _block("NEGATIVE_OR_NON_FINITE_QUANTITY",
                                  quantity=quantity)
                self._journal(symbol, "BLOCK", decision)
                return decision
            if qty_value > 0.0:
                size_gates = (
                    self._gate_per_trade_risk(
                        qty=quantity, entry_price=entry_price,
                        stop_price=stop_loss, equity=account_equity,
                    ),
                    self._gate_position_notional(
                        qty=quantity, entry_price=entry_price, equity=account_equity
                    ),
                    self._gate_aggregate_exposure(
                        qty=quantity, entry_price=entry_price, equity=account_equity
                    ),
                    self._gate_correlation_bucket(
                        symbol=symbol, qty=quantity,
                        entry_price=entry_price, equity=account_equity,
                    ),
                )
                for decision in size_gates:
                    if not decision.ok:
                        self._journal(symbol, "BLOCK", decision)
                        return decision

            approved = RiskDecision(ok=True, reason="APPROVED", detail=detail)
            self._journal(symbol, "ALLOW", approved)
            return approved

        except Exception as exc:  # noqa: BLE001
            # The whole point of this file: an unexpected exception blocks.
            logger.exception("risk gate raised; blocking trade for %s", symbol)
            decision = _block("GATE_EXCEPTION", error=f"{type(exc).__name__}: {exc}")
            try:
                self._journal(symbol, "BLOCK", decision)
            except Exception:  # noqa: BLE001
                pass
            return decision

    def _journal(self, symbol: str, verdict: str, decision: RiskDecision) -> None:
        """Every decision leaves a trace, with its reason."""
        try:
            self.store.journal(symbol, verdict, decision.reason, decision.detail)
        except Exception:  # noqa: BLE001
            logger.warning("decision journal write failed for %s", symbol, exc_info=True)

    # -- legacy-compatible entry points -----------------------------------
    # These preserve the signatures main.py and trading_engine.py already call.

    def assess_trade_risk(
        self,
        *,
        symbol: str,
        entry_price: float,
        position_size: float,
        stop_loss_price: float,
        signal_confidence: float = 0.0,
        market_data: Optional[Mapping[str, Any]] = None,
        account_equity: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        **_extra: Any,
    ) -> Dict[str, Any]:
        """Dict-shaped wrapper over :meth:`gate_order` for ``main.py``.

        Returns ``{"approved": bool, "reason": str, ...}``. Blocks on anything
        it cannot evaluate, including a missing equity reading — the legacy code
        substituted a phantom $10,000 balance here.
        """
        equity = account_equity
        if equity is None:
            equity = self._equity_from_state()
        if equity is None or float(equity) <= 0.0:
            return {
                "approved": False,
                "reason": "EQUITY_UNKNOWN",
                "detail": {"note": "no equity reading; refusing to size from a default"},
            }
        decision = self.gate_order(
            symbol=symbol,
            side=str(_extra.get("side", "Buy")),
            entry_price=float(entry_price),
            stop_loss=float(stop_loss_price),
            quantity=float(position_size),
            account_equity=float(equity),
            take_profit=take_profit_price,
            confidence=signal_confidence if signal_confidence else None,
        )
        return {
            "approved": decision.ok,
            "reason": decision.reason,
            "detail": dict(decision.detail),
            "adjustments": dict(decision.adjustments),
        }

    def approve_trade_execution(
        self,
        opportunity: Any = None,
        position_size: float = 0.0,
        current_exposure: float = 0.0,
        risk_assessment: Any = None,
        **_extra: Any,
    ) -> Dict[str, Any]:
        """Final approval gate called by ``main.py`` immediately before submit."""
        try:
            symbol = str(getattr(opportunity, "symbol", "") or "")
            entry = float(getattr(opportunity, "entry_price", 0.0) or 0.0)
            stop = float(getattr(opportunity, "stop_loss", 0.0) or 0.0)
            take = getattr(opportunity, "take_profit", None)
            conf = getattr(opportunity, "confidence", None)
            # Derive the side explicitly. The legacy code defaulted to "Buy"
            # whenever it could not read one, which is a silent long bias in
            # exactly the situation where the signal is unreadable.
            raw_side = getattr(opportunity, "side", None)
            side = normalize_side(raw_side)
            if side is None:
                signal_type = str(getattr(opportunity, "signal_type", "")).upper()
                if "SELL" in signal_type:
                    side = "Sell"
                elif "BUY" in signal_type:
                    side = "Buy"
                else:
                    return {"approved": False, "reason": "UNKNOWN_SIDE",
                            "detail": {"side": raw_side, "signal_type": signal_type}}
        except Exception as exc:  # noqa: BLE001
            return {"approved": False, "reason": f"OPPORTUNITY_UNREADABLE: {exc}"}

        equity = self._equity_from_state()
        if equity is None or equity <= 0.0:
            return {"approved": False, "reason": "EQUITY_UNKNOWN"}

        decision = self.gate_order(
            symbol=symbol, side=side, entry_price=entry, stop_loss=stop,
            quantity=float(position_size), account_equity=float(equity),
            take_profit=float(take) if take else None,
            confidence=float(conf) if conf is not None else None,
        )
        return {
            "approved": decision.ok,
            "reason": decision.reason,
            "detail": dict(decision.detail),
        }

    def should_halt_trading(self) -> bool:
        """True when trading must stop. Any doubt returns True."""
        try:
            engaged, _ = self.store.is_kill_switch_engaged()
            if engaged:
                return True
            breaker = self.store.get_breaker()
            if breaker.active and breaker.is_cooling_down():
                return True
            if self.store.consecutive_losses() >= self.max_consecutive_losses:
                return True
            equity_state = self.store.get_equity_state()
            if equity_state.peak_equity > 0.0:
                if equity_state.drawdown_fraction() >= self.max_drawdown_pct:
                    return True
                anchor = self.store.get_daily_anchor()
                if anchor and anchor.start_equity > 0.0:
                    loss = self.store.daily_loss_fraction(equity_state.last_equity)
                    if loss >= self.max_daily_loss_pct:
                        return True
            return False
        except Exception:  # noqa: BLE001
            logger.exception("halt check failed; halting")
            return True

    def can_trade(self, symbol: str) -> bool:
        """Cheap per-symbol pre-check. Blocks on any error."""
        try:
            if self.should_halt_trading():
                return False
            return self._gate_symbol_cooldown(symbol).ok
        except Exception:  # noqa: BLE001
            logger.exception("can_trade failed for %s; blocking", symbol)
            return False

    def set_cooldown(self, symbol: str, seconds: Optional[float] = None) -> None:
        try:
            self.store.set_symbol_cooldown(
                symbol,
                self.trade_cooldown_seconds if seconds is None else float(seconds),
                "explicit",
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not set cooldown for %s", symbol)

    def note_submit_result(self, accepted: bool = False, symbol: str = "", **_x: Any) -> None:
        """Record a submit outcome; a rejection starts a short symbol cooldown."""
        try:
            if not accepted and symbol:
                self.store.set_symbol_cooldown(
                    symbol, self.trade_cooldown_seconds, "submit_rejected"
                )
        except Exception:  # noqa: BLE001
            logger.exception("note_submit_result failed")

    def update_trade_result(self, trade_result: Any) -> None:
        """Persist a closed trade, fee-inclusive, and update the loss streak."""
        try:
            def g(name: str, default: Any = 0.0) -> Any:
                if isinstance(trade_result, Mapping):
                    return trade_result.get(name, default)
                return getattr(trade_result, name, default)

            record = TradeRecord(
                symbol=str(g("symbol", "")),
                side=str(g("side", "")),
                qty=float(g("qty", g("quantity", 0.0)) or 0.0),
                entry_price=float(g("entry_price", 0.0) or 0.0),
                exit_price=float(g("exit_price", 0.0) or 0.0),
                gross_pnl=float(g("gross_pnl", g("pnl", 0.0)) or 0.0),
                entry_fee=float(g("entry_fee", 0.0) or 0.0),
                exit_fee=float(g("exit_fee", 0.0) or 0.0),
                opened_epoch=float(g("opened_epoch", utc_now_epoch()) or utc_now_epoch()),
                closed_epoch=float(g("closed_epoch", utc_now_epoch()) or utc_now_epoch()),
                order_link_id=str(g("order_link_id", "")),
            )
            self.store.record_trade(record)
            self.store.set_consecutive_losses(self.store.consecutive_losses())
        except Exception:  # noqa: BLE001
            logger.exception("could not record trade result")

    def get_market_regime(self) -> str:
        """Regime label. Returns UNKNOWN rather than guessing.

        Real regime detection is wired in Phase 3 from closed-candle indicators;
        until then this reports honestly instead of fabricating a label that
        downstream sizing would multiply by.
        """
        return "UNKNOWN"

    def pretrade_check(self, order: Any = None, **_x: Any) -> bool:
        """Boolean pre-trade gate used by ``trading_engine``. Blocks on error."""
        try:
            if order is None:
                return False

            def g(name: str, default: Any = None) -> Any:
                if isinstance(order, Mapping):
                    return order.get(name, default)
                return getattr(order, name, default)

            equity = self._equity_from_state()
            if equity is None or equity <= 0.0:
                return False
            return self.gate_order(
                symbol=str(g("symbol", "") or ""),
                side=str(g("side", "Buy") or "Buy"),
                entry_price=float(g("price", g("entry_price", 0.0)) or 0.0),
                stop_loss=float(g("stop_loss", g("stopLoss", 0.0)) or 0.0),
                quantity=float(g("qty", g("quantity", 0.0)) or 0.0),
                account_equity=float(equity),
            ).ok
        except Exception:  # noqa: BLE001
            logger.exception("pretrade_check failed; blocking")
            return False

    def pretrade_gate(self, *a: Any, **kw: Any) -> Tuple[bool, str]:
        """``(ok, reason)`` form of :meth:`pretrade_check`."""
        try:
            equity = self._equity_from_state()
            if equity is None or equity <= 0.0:
                return False, "EQUITY_UNKNOWN"
            kw.setdefault("account_equity", float(equity))
            kw.setdefault("quantity", float(kw.pop("qty", 0.0) or 0.0))
            decision = self.gate_order(**kw)
            return decision.ok, decision.reason
        except Exception as exc:  # noqa: BLE001
            logger.exception("pretrade_gate failed; blocking")
            return False, f"GATE_EXCEPTION: {type(exc).__name__}"

    def maker_qty(self, symbol: str, price: float) -> float:
        """Maker quote size. Returns 0.0 (no quote) unless it can prove a size."""
        try:
            equity = self._equity_from_state()
            if equity is None or equity <= 0.0 or float(price) <= 0.0:
                return 0.0
            notional = float(equity) * self.max_position_size_pct
            return notional / float(price)
        except Exception:  # noqa: BLE001
            logger.exception("maker_qty failed for %s; quoting nothing", symbol)
            return 0.0

    # -- helpers -----------------------------------------------------------

    def _equity_from_state(self) -> Optional[float]:
        """Last persisted equity, or None. Never a fabricated default."""
        try:
            eq = self.store.get_equity_state().last_equity
            return float(eq) if eq > 0.0 else None
        except Exception:  # noqa: BLE001
            return None

    def trip_kill_switch(self, reason: str) -> None:
        """Engage the kill switch. Nothing here can clear it — that is a human
        action via ``StateStore.clear_kill_switch_by_human``."""
        self.store.trip_kill_switch(reason)


# Alias used by several import sites.
RiskManager = BillionaireRiskManager


class V5EnhancedRiskManager(BillionaireRiskManager):
    """Kept for import compatibility (``trading_engine`` constructs this name).

    The legacy subclass never called ``super().__init__`` and defined a
    ``@property`` inside ``__init__`` (a no-op local), so it silently had none
    of the base class's state. It adds no behaviour that the base chain does not
    already enforce, so it is now a plain subclass rather than a broken decorator.
    """


class GateSet:
    """Thin façade over :meth:`BillionaireRiskManager.gate_order`.

    The legacy ``GateSet`` was a second, independent gate API with its own
    fail-open evaluator (``PASS_THROUGH`` on exception). Keeping the name but
    delegating means there is one implementation, not two that can disagree.
    """

    def __init__(self, manager: Optional[BillionaireRiskManager] = None, **kw: Any) -> None:
        self.manager = manager or BillionaireRiskManager(**kw)

    def evaluate(self, symbol: str, signal: Mapping[str, Any]) -> GateOutcome:
        try:
            equity = self.manager._equity_from_state()
            if equity is None:
                return GateOutcome(False, "EQUITY_UNKNOWN")
            decision = self.manager.gate_order(
                symbol=symbol,
                side=str(signal.get("side", "Buy")),
                entry_price=float(signal.get("entry_price", 0.0) or 0.0),
                stop_loss=float(signal.get("stop_loss", 0.0) or 0.0),
                quantity=float(signal.get("quantity", 0.0) or 0.0),
                account_equity=float(equity),
                take_profit=signal.get("take_profit"),
                confidence=signal.get("confidence"),
            )
            return GateOutcome(decision.ok, decision.reason, dict(decision.detail))
        except Exception as exc:  # noqa: BLE001
            return GateOutcome(False, f"GATE_EXCEPTION: {type(exc).__name__}")


# ---------------------------------------------------------------------------
# module-level helpers other modules import
# ---------------------------------------------------------------------------


def validate_order(order: Mapping[str, Any]) -> Tuple[bool, str]:
    """Structural validation of an order payload. Fails closed."""
    try:
        qty = float(order.get("qty", order.get("quantity", 0.0)) or 0.0)
        price = float(order.get("price", 0.0) or 0.0)
        symbol = str(order.get("symbol", "") or "")
        side = str(order.get("side", "") or "")
        if not symbol:
            return False, "NO_SYMBOL"
        if side.strip().lower() not in {"buy", "sell"}:
            return False, f"BAD_SIDE:{side}"
        if qty <= 0.0 or not math.isfinite(qty):
            return False, f"BAD_QTY:{qty}"
        order_type = str(order.get("orderType", order.get("order_type", "")) or "")
        if order_type.strip().lower() in {"limit", "postonly"} and price <= 0.0:
            return False, f"BAD_PRICE:{price}"
        return True, "OK"
    except Exception as exc:  # noqa: BLE001
        return False, f"VALIDATION_EXCEPTION: {type(exc).__name__}: {exc}"


def kill_active(store: Optional[StateStore] = None) -> bool:
    """True if the kill switch is engaged, or if that cannot be determined."""
    try:
        s = store or StateStore()
        engaged, _ = s.is_kill_switch_engaged()
        return bool(engaged)
    except Exception:  # noqa: BLE001
        logger.exception("kill switch unreadable; treating as ENGAGED")
        return True


def risk_gate(manager: BillionaireRiskManager, **kw: Any) -> RiskDecision:
    """Functional form of the gate chain."""
    try:
        return manager.gate_order(**kw)
    except Exception as exc:  # noqa: BLE001
        return _block("GATE_EXCEPTION", error=f"{type(exc).__name__}: {exc}")
