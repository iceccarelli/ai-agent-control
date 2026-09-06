"""config.py — the single configuration loader.

THE ONE RULE
============
This is the **only** module in the codebase that reads the environment. Nothing
else calls ``os.getenv``, and nothing patches it. ``tests/test_no_dead_imports.py``
enforces both mechanically.

UNITS
-----
**Every ``*_PCT`` value is a fraction**: ``0.02`` means 2%. A value greater than
1.0 is **rejected at load time**, not silently divided by 100. Silent
renormalisation is how a predecessor of this file ended up reading the same
variable as 70% in one module and 0.7% in another, in the same process.

Fee and spread limits are expressed in **basis points** (``TAKER_FEE_BPS=10``
means 0.10%) because that is how exchanges quote them, and converting once here
is safer than converting at each use site. The fraction form is exposed as a
derived property.

THE LIVE GATE
-------------
Arming real money requires **all four**, simultaneously, with no override::

    USE_TESTNET=0  AND  PAPER_TRADING=0
                   AND  LIVE_TRADING_ACK == "I_UNDERSTAND"
                   AND  both credentials present

Anything less degrades to paper and logs why. There is no ``FORCE_LIVE``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Tuple

__all__ = [
    "Config",
    "ConfigError",
    "load",
    "reload",
    "get_config_object",
    "assert_sandbox",
    "is_live_authorized",
    "setup_logging",
    "parse_bool",
    "parse_pct",
    "redact",
    "REQUIRED_LIVE_ACK",
    "REQUIRED_POLICY_ACK",
    "POLICY_MODES",
]

logger = logging.getLogger("config")

REQUIRED_LIVE_ACK = "I_UNDERSTAND"

_TRUE_TOKENS = frozenset({"1", "true", "t", "yes", "y", "on"})

_TIMEFRAME_SECONDS: Dict[str, int] = {
    "1": 60, "3": 180, "5": 300, "15": 900, "30": 1800,
    "60": 3600, "120": 7200, "240": 14400, "360": 21600, "720": 43200,
    "D": 86400, "W": 604800, "M": 2592000,
}

_MAINNET_REST = "https://api.bybit.com"
_TESTNET_REST = "https://api-testnet.bybit.com"

# The only two product categories this codebase implements.
#
#   spot   — no native short. A SELL can only close an existing long.
#   linear — USDT perpetual. Shorts are first-class; funding is charged; the
#            protective stop uses /v5/position/trading-stop, which is INVALID
#            for spot. See MARKET_CATEGORIES.md for the full fork.
#
# Anything else (inverse, option) is not implemented and is rejected at load
# rather than accepted and mis-routed at order time.
_SUPPORTED_CATEGORIES = ("spot", "linear")

DEFAULT_INDICATOR_THRESHOLDS: Dict[str, float] = {
    "RSI_PERIOD": 14, "RSI_OVERSOLD": 30, "RSI_OVERBOUGHT": 70,
    "ATR_PERIOD": 14, "BB_TIMEFRAME": 20, "BB_STD": 2.0,
    "ADX_PERIOD": 14, "ADX_TREND_MIN": 25,
    "MACD_FAST": 12, "MACD_SLOW": 26, "MACD_SIGNAL": 9,
}


class ConfigError(ValueError):
    """Configuration is unusable. Raised at load time, never swallowed."""


# ---------------------------------------------------------------------------
# parsers — one per type, used everywhere
# ---------------------------------------------------------------------------


def parse_bool(value: Any, default: bool = False) -> bool:
    """Accepts ``1/true/t/yes/y/on`` case-insensitively; everything else False.

    A predecessor compared ``USE_TESTNET == "true"``, so the Dockerfile's
    safety default of ``USE_TESTNET=1`` silently meant **mainnet**.
    """
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE_TOKENS


def parse_pct(env: Mapping[str, str], name: str, default: float) -> float:
    """A fraction in [0, 1]. A percent-style value is REJECTED, not rescaled."""
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        raise ConfigError(f"{name}={raw!r} is not a number")
    if not math.isfinite(value):
        raise ConfigError(f"{name}={raw!r} is not finite")
    if value < 0.0:
        raise ConfigError(f"{name}={value} is negative; percentages are fractions")
    if value > 1.0:
        raise ConfigError(
            f"{name}={value} looks like a percent. Every *_PCT value in this "
            f"codebase is a FRACTION: use {value / 100:g} for {value:g}%. "
            "Rejecting rather than guessing, because guessing is how the same "
            "variable came to mean 70% and 0.7% in one process."
        )
    return value


def parse_float(
    env: Mapping[str, str], name: str, default: float,
    *, low: float = -math.inf, high: float = math.inf,
) -> float:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        value = float(default)
    else:
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            raise ConfigError(f"{name}={raw!r} is not a number")
    if not math.isfinite(value) or not (low <= value <= high):
        raise ConfigError(f"{name}={value} is outside [{low}, {high}]")
    return value


def parse_int(
    env: Mapping[str, str], name: str, default: int,
    *, low: int = -(2**31), high: int = 2**31,
) -> int:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        value = int(default)
    else:
        try:
            value = int(float(str(raw).strip()))
        except (TypeError, ValueError):
            raise ConfigError(f"{name}={raw!r} is not an integer")
    if not (low <= value <= high):
        raise ConfigError(f"{name}={value} is outside [{low}, {high}]")
    return value


def parse_str(env: Mapping[str, str], name: str, default: str = "") -> str:
    raw = env.get(name)
    return default if raw is None else str(raw).strip()


def parse_symbols(env: Mapping[str, str], name: str, default: str) -> Tuple[str, ...]:
    raw = parse_str(env, name, default)
    symbols = tuple(s.strip().upper() for s in raw.split(",") if s.strip())
    for symbol in symbols:
        if not re.fullmatch(r"[A-Z0-9]{4,20}", symbol):
            raise ConfigError(f"{name}: {symbol!r} is not a valid symbol")
    if not symbols:
        raise ConfigError(f"{name} is empty; nothing to trade")
    return symbols


def parse_timeframes(env: Mapping[str, str], name: str, default: str) -> Tuple[str, ...]:
    raw = parse_str(env, name, default)
    frames = tuple(s.strip() for s in raw.split(",") if s.strip())
    for frame in frames:
        if frame not in _TIMEFRAME_SECONDS:
            raise ConfigError(
                f"{name}: {frame!r} is not a Bybit interval "
                f"({', '.join(sorted(_TIMEFRAME_SECONDS))})"
            )
    return frames


def parse_json_dict(
    env: Mapping[str, str], name: str, default: Mapping[str, float]
) -> Dict[str, float]:
    raw = env.get(name)
    if not raw:
        return dict(default)
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        raise ConfigError(f"{name} is not valid JSON: {exc}")
    if not isinstance(parsed, dict):
        raise ConfigError(f"{name} must be a JSON object")
    return {**dict(default), **{str(k): float(v) for k, v in parsed.items()}}


def redact(secret: Optional[str]) -> str:
    """Fixed-width mask. Never reveals length, which is itself information."""
    return "***" if secret else ""


# ---------------------------------------------------------------------------
# the config object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """Immutable, validated configuration. One instance per process."""

    # -- mode ---------------------------------------------------------------
    USE_TESTNET: bool
    PAPER_TRADING: bool
    LIVE_TRADING_ACK: str
    LIVE_AUTHORIZED: bool
    LIVE_REQUESTED: bool
    LIVE_BLOCK_REASON: str

    # -- credentials --------------------------------------------------------
    BYBIT_API_KEY: str
    BYBIT_API_SECRET: str
    HAS_CREDENTIALS: bool

    # -- product category ---------------------------------------------------
    CATEGORY: str
    ALLOW_SHORTS: bool
    FUNDING_RATE_BPS_PER_8H: float
    MAX_FUNDING_BPS: float
    MAX_HOLD_HOURS: float

    # -- exchange -----------------------------------------------------------
    BYBIT_BASE_URL: str
    BYBIT_RECV_WINDOW_MS: int
    REQUEST_TIMEOUT_SECONDS: float
    SYMBOL_FILTERS_TTL: int
    ORDERLINK_PREFIX: str

    # -- universe -----------------------------------------------------------
    TRADING_SYMBOLS: Tuple[str, ...]
    PRIMARY_TIMEFRAME: str
    SECONDARY_TIMEFRAMES: Tuple[str, ...]
    KLINE_LOOKBACK: int

    # -- leverage -----------------------------------------------------------
    USE_LEVERAGE: bool
    DEFAULT_LEVERAGE: int
    MAX_LEVERAGE_EU: int

    # -- risk: FRACTIONS ----------------------------------------------------
    MAX_POSITION_SIZE_PCT: float
    MAX_TOTAL_EXPOSURE_PCT: float
    MAX_CORRELATION_EXPOSURE_PCT: float
    RISK_PER_TRADE_PCT: float
    MAX_DAILY_LOSS_PCT: float
    MAX_DRAWDOWN_PCT: float
    STOP_LOSS_PCT: float
    MAX_OPEN_POSITIONS: int
    MAX_CONSECUTIVE_LOSSES: int
    TRADE_COOLDOWN_SECONDS: float
    CIRCUIT_BREAKER_HOURS: float

    # -- costs: BASIS POINTS ------------------------------------------------
    TAKER_FEE_BPS: float
    MAKER_FEE_BPS: float
    SLIPPAGE_BPS: float
    MAX_SPREAD_BPS: float
    MIN_EDGE_BPS: float

    # -- signal gates -------------------------------------------------------
    MIN_CONFIDENCE: float
    MIN_COMPONENT_AGREEMENT: float
    MIN_RISK_REWARD_RATIO: float
    MIN_KELLY_SAMPLES: int
    STOP_ATR_MULTIPLE: float
    INDICATOR_THRESHOLDS: Dict[str, float]

    # -- execution ----------------------------------------------------------
    PREFER_MAKER: bool
    MAKER_TIMEOUT_SECONDS: float
    MAX_DATA_AGE_SECONDS: float
    BREAKEVEN_AFTER_FIRST_TP: bool

    # -- market data --------------------------------------------------------
    DATA_DIR: str
    USE_ORDERBOOK: bool
    BOOK_DEPTH_LEVELS: int
    MIN_BOOK_DEPTH_USD: float
    MAX_BOOK_IMBALANCE: float
    MAX_SPREAD_ANOMALY_PCT: float

    # -- the learned policy -------------------------------------------------
    POLICY_MODE: str
    POLICY_PATH: str
    POLICY_ACK: str
    POLICY_ARMED: bool
    POLICY_BLOCK_REASON: str
    POLICY_MIN_PROBABILITY: float

    # -- memory (the shared reference layer) --------------------------------
    MEMORY_ENABLED: bool
    MEMORY_MIN_SAMPLES: int
    MEMORY_LOOKBACK_TRADES: int
    MEMORY_MIN_THROTTLE: float

    # -- runtime ------------------------------------------------------------
    LOOP_INTERVAL_SECONDS: float
    ENABLE_HEALTH_SERVER: bool
    HEALTHCHECK_HOST: str
    HEALTHCHECK_PORT: int
    DECISIONS_RETENTION_DAYS: int
    LOG_LEVEL: str
    STATE_DB_PATH: str

    #: Optional path for the operator-facing paper session log (JSONL). Empty
    #: disables the file; the session is still journalled to the state store.
    #: Never contains credentials and never contains a PnL figure — see
    #: ``session_log.py``.
    PAPER_SESSION_LOG_PATH: str

    #: Whether ``build_bot`` attaches a signal source at all.
    #:
    #: False means the bot runs its loop, keeps state reconciled, manages the
    #: stops of any position it already holds, and serves health — and proposes
    #: **no new entries**, because there is nothing to propose them.
    #:
    #: This is an OPERATOR CONTROL, not a safety gate. The safety gates are the
    #: kill switch, the live-arming chain and the risk gate set; this flag is
    #: not a substitute for any of them and must never be described as one. It
    #: exists because strategy-neutral operation was already a first-class
    #: configuration (``build_bot(attach_strategy=False)``) that an operator
    #: running ``python3 main.py`` had no way to select.
    ENTRIES_ENABLED: bool

    #: Which book this process runs. "carry" (delta-neutral spot/perp pair,
    #: collects funding) or "directional" (the legacy technical voter, whose
    #: Stage-1 verdict is ABSENT). build_bot attaches ONE and returns: they
    #: share a liquidation price and must never run in the same process.
    BOOK_MODE: str = "directional"
    CARRY_SPOT_SYMBOL: str = "BTCUSDT"
    CARRY_PERP_SYMBOL: str = "BTCUSDT"

    # -- derived cost fractions --------------------------------------------

    @property
    def TAKER_FEE(self) -> float:
        return self.TAKER_FEE_BPS / 10_000.0

    @property
    def MAKER_FEE(self) -> float:
        return self.MAKER_FEE_BPS / 10_000.0

    @property
    def SLIPPAGE(self) -> float:
        return self.SLIPPAGE_BPS / 10_000.0

    @property
    def FUNDING_COST_BPS(self) -> float:
        """Expected funding paid over one trade, in bps. Zero on spot.

        Perpetuals charge funding every 8 hours. A strategy that holds through
        three funding stamps pays three times, and a cost model that ignores it
        will report an edge the account never sees. The estimate is deliberately
        one-sided: it assumes the trade PAYS funding for its whole maximum hold,
        because assuming it RECEIVES funding is how an optimistic cost model
        gets built.
        """
        if self.CATEGORY != "linear":
            return 0.0
        return self.FUNDING_RATE_BPS_PER_8H * max(1.0, self.MAX_HOLD_HOURS / 8.0)

    @property
    def ROUND_TRIP_COST_BPS(self) -> float:
        """Entry + exit fees, slippage, and (on perps) funding, in bps.

        This is the number a trade's expected move must clear before it is
        worth taking. Computing it once here stops each caller inventing its
        own (usually optimistic) version.
        """
        return 2.0 * self.TAKER_FEE_BPS + self.SLIPPAGE_BPS + self.FUNDING_COST_BPS

    def safe_dict(self) -> Dict[str, Any]:
        """Loggable view. Credentials redacted."""
        data = asdict(self)
        data["BYBIT_API_KEY"] = redact(self.BYBIT_API_KEY)
        data["BYBIT_API_SECRET"] = redact(self.BYBIT_API_SECRET)
        return data

    def as_dict(self) -> Dict[str, Any]:
        """Full copy INCLUDING secrets. Do not log this."""
        return asdict(self)

    def checksum(self) -> str:
        return hashlib.blake2b(
            json.dumps(self.safe_dict(), sort_keys=True, default=str).encode(),
            digest_size=8,
        ).hexdigest()

    def __repr__(self) -> str:
        mode = "LIVE" if self.LIVE_AUTHORIZED else (
            "PAPER" if self.PAPER_TRADING else "TESTNET"
        )
        return (
            f"Config(mode={mode}, symbols={len(self.TRADING_SYMBOLS)}, "
            f"max_pos={self.MAX_POSITION_SIZE_PCT:.4f}, "
            f"risk={self.RISK_PER_TRADE_PCT:.4f}, checksum={self.checksum()})"
        )

    __str__ = __repr__


# ---------------------------------------------------------------------------
# the live gate
# ---------------------------------------------------------------------------


def _resolve_live_gate(
    *, use_testnet: bool, paper_trading: bool, ack: str,
    api_key: str, api_secret: str,
) -> Tuple[bool, bool, str]:
    """Returns ``(live_requested, live_authorized, reason)``."""
    requested = (not use_testnet) and (not paper_trading)
    if not requested:
        modes = [m for m, on in (("TESTNET", use_testnet), ("PAPER", paper_trading)) if on]
        return False, False, f"SANDBOX ({'+'.join(modes)}) — live trading not armed"
    if ack != REQUIRED_LIVE_ACK:
        return True, False, (
            f"LIVE_BLOCKED: LIVE_TRADING_ACK must equal {REQUIRED_LIVE_ACK!r} "
            f"(got {'***' if ack else 'unset'})"
        )
    if not (api_key and api_secret):
        return True, False, "LIVE_BLOCKED: BYBIT_API_KEY/BYBIT_API_SECRET are not both set"
    return True, True, "LIVE_AUTHORIZED"


#: The three states a learned policy can be in. There is no fourth, and there
#: is no numeric "how much to trust it" dial — a dial invites splitting the
#: difference, and "half-trusted model" is not a state anyone can reason about.
POLICY_MODES = ("off", "shadow", "live")

REQUIRED_POLICY_ACK = "I_UNDERSTAND_MODEL_RISK"


def _resolve_policy_gate(*, mode: str, ack: str) -> Tuple[bool, str]:
    """``(armed, reason)`` — may a model's output reach a real order?

    Deliberately shaped like the live-trading gate above, because it is the same
    kind of decision: something that can lose money is being switched on, and it
    must take a specific human act to do it rather than a default.

    * ``off``    — no model is loaded at all. The classical strategy runs alone.
    * ``shadow`` — the model runs, and every decision it would have taken is
      journaled. **It cannot place an order.** This is the only mode in which an
      unproven model should ever see production data.
    * ``live``   — the model proposes trades, and needs the exact acknowledgement
      token. Even then it only *proposes*: every gate downstream still applies,
      and a model output cannot raise a limit, resize a position upward, or
      clear the kill switch.

    Anything unrecognised is refused rather than interpreted, because the
    plausible misreading of an unknown mode is the permissive one.
    """
    if mode not in POLICY_MODES:
        raise ConfigError(
            f"POLICY_MODE={mode!r} is not one of {POLICY_MODES}. Refusing to "
            "guess: the charitable reading of an unknown mode is the dangerous one."
        )
    if mode == "off":
        return False, "POLICY_OFF — no model is loaded"
    if mode == "shadow":
        return False, "POLICY_SHADOW — the model runs and is journaled; it cannot trade"
    if ack != REQUIRED_POLICY_ACK:
        return False, (
            f"POLICY_BLOCKED: POLICY_MODE=live requires POLICY_ACK="
            f"{REQUIRED_POLICY_ACK!r} (got {'***' if ack else 'unset'}); "
            "degrading to shadow"
        )
    return True, "POLICY_LIVE — a promoted model may propose trades"


def is_live_authorized(cfg: Any = None) -> Tuple[bool, str]:
    """``(live_allowed, reason)`` — is real money armed?"""
    cfg = _CFG if cfg is None else cfg
    if isinstance(cfg, Config):
        return cfg.LIVE_AUTHORIZED, cfg.LIVE_BLOCK_REASON
    get = (cfg.get if isinstance(cfg, Mapping) else lambda k, d=None: getattr(cfg, k, d))
    _, authorized, reason = _resolve_live_gate(
        use_testnet=parse_bool(get("USE_TESTNET", True), True),
        paper_trading=parse_bool(get("PAPER_TRADING", True), True),
        ack=str(get("LIVE_TRADING_ACK", "") or ""),
        api_key=str(get("BYBIT_API_KEY", "") or ""),
        api_secret=str(get("BYBIT_API_SECRET", "") or ""),
    )
    return authorized, reason


def assert_sandbox(cfg: Any = None) -> Tuple[bool, str]:
    """``(may_start, reason)`` — the STARTUP gate.

    ``True`` when sandboxed **or** fully authorised for live. ``False`` only
    when live was requested without complete authorisation.

    Being in paper/testnet is a PASS. An earlier rewrite inverted this and
    would have refused to start the bot in the mode it runs in by default.
    """
    live_ok, reason = is_live_authorized(cfg)
    if live_ok:
        return True, reason
    cfg = _CFG if cfg is None else cfg
    get = (cfg.get if isinstance(cfg, Mapping) else lambda k, d=None: getattr(cfg, k, d))
    if parse_bool(get("USE_TESTNET", True), True) or parse_bool(get("PAPER_TRADING", True), True):
        return True, reason
    return False, reason


# ---------------------------------------------------------------------------
# the loader
# ---------------------------------------------------------------------------


def load(env: Optional[Mapping[str, str]] = None) -> Config:
    """Build a Config from ``env`` (defaults to ``os.environ``).

    Never mutates the environment. Never returns a partially valid object: any
    problem raises :class:`ConfigError` here, at startup, rather than surfacing
    as a strange number during a trade.
    """
    env = os.environ if env is None else env

    use_testnet = parse_bool(env.get("USE_TESTNET"), True)
    paper_trading = parse_bool(env.get("PAPER_TRADING"), True)
    ack = parse_str(env, "LIVE_TRADING_ACK")
    api_key = parse_str(env, "BYBIT_API_KEY")
    api_secret = parse_str(env, "BYBIT_API_SECRET")

    requested, authorized, reason = _resolve_live_gate(
        use_testnet=use_testnet, paper_trading=paper_trading,
        ack=ack, api_key=api_key, api_secret=api_secret,
    )
    if requested and not authorized:
        # Fail closed: degrade to paper rather than crash-loop or arm live.
        logger.warning("%s; loading in PAPER mode.", reason)
        paper_trading = True

    category = parse_str(env, "CATEGORY", "spot").lower()
    if category not in _SUPPORTED_CATEGORIES:
        raise ConfigError(
            f"CATEGORY={category!r} is not implemented. This codebase supports "
            f"{' and '.join(_SUPPORTED_CATEGORIES)} only. 'inverse' and 'option' "
            "have different margin, stop and settlement mechanics; accepting the "
            "name without the implementation would mis-route live orders."
        )

    max_leverage_eu = parse_int(env, "MAX_LEVERAGE_EU", 10, low=1, high=10)
    default_leverage = parse_int(env, "DEFAULT_LEVERAGE", 1, low=1, high=max_leverage_eu)

    # Costs are resolved before the Config is built so the derived edge floor
    # can see all of them, funding included.
    taker_bps = parse_float(env, "TAKER_FEE_BPS", 10.0 if category == "spot" else 5.5,
                            low=0.0, high=500.0)
    maker_bps = parse_float(env, "MAKER_FEE_BPS", 10.0 if category == "spot" else 2.0,
                            low=0.0, high=500.0)
    slippage_bps = parse_float(env, "SLIPPAGE_BPS", 5.0, low=0.0, high=500.0)
    funding_bps = parse_float(env, "FUNDING_RATE_BPS_PER_8H", 1.0, low=0.0, high=500.0)
    max_hold_hours = parse_float(env, "MAX_HOLD_HOURS", 8.0, low=0.25, high=720.0)
    funding_cost_bps = (
        0.0 if category != "linear"
        else funding_bps * max(1.0, max_hold_hours / 8.0)
    )
    round_trip_bps = 2.0 * taker_bps + slippage_bps + funding_cost_bps

    max_position = parse_pct(env, "MAX_POSITION_SIZE_PCT", 0.02)
    risk_per_trade = parse_pct(env, "RISK_PER_TRADE_PCT", min(0.005, max_position))
    max_exposure = parse_pct(env, "MAX_TOTAL_EXPOSURE_PCT", 0.10)
    min_rr = parse_float(env, "MIN_RISK_REWARD_RATIO", 2.0, low=1.0, high=100.0)

    policy_mode = parse_str(env, "POLICY_MODE", "off").lower()
    policy_path = parse_str(env, "POLICY_PATH", "models/current")
    policy_ack = parse_str(env, "POLICY_ACK")
    policy_armed, policy_reason = _resolve_policy_gate(
        mode=policy_mode, ack=policy_ack,
    )

    log_level = parse_str(env, "LOG_LEVEL", "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "WARN", "ERROR", "CRITICAL"}:
        raise ConfigError(f"LOG_LEVEL={log_level!r} is not a logging level")

    cfg = Config(
        USE_TESTNET=use_testnet,
        PAPER_TRADING=paper_trading,
        LIVE_TRADING_ACK=ack,
        LIVE_AUTHORIZED=authorized,
        LIVE_REQUESTED=requested,
        LIVE_BLOCK_REASON=reason,

        BYBIT_API_KEY=api_key,
        BYBIT_API_SECRET=api_secret,
        HAS_CREDENTIALS=bool(api_key and api_secret),

        CATEGORY=category,
        # Not a user switch. Spot has no native short: a SELL can only reduce a
        # long. Letting an operator set ALLOW_SHORTS=1 on spot would produce
        # orders the venue rejects, or worse, that silently sell inventory the
        # bot does not have.
        ALLOW_SHORTS=(category == "linear"),
        FUNDING_RATE_BPS_PER_8H=funding_bps,
        MAX_FUNDING_BPS=parse_float(env, "MAX_FUNDING_BPS", 30.0, low=0.0, high=1000.0),
        MAX_HOLD_HOURS=max_hold_hours,

        BYBIT_BASE_URL=_TESTNET_REST if use_testnet else _MAINNET_REST,
        BYBIT_RECV_WINDOW_MS=parse_int(env, "BYBIT_RECV_WINDOW_MS", 5000, low=1000, high=10_000),
        REQUEST_TIMEOUT_SECONDS=parse_float(env, "REQUEST_TIMEOUT_SECONDS", 10.0, low=1.0, high=60.0),
        SYMBOL_FILTERS_TTL=parse_int(env, "SYMBOL_FILTERS_TTL", 3600, low=60, high=86_400),
        ORDERLINK_PREFIX=parse_str(env, "ORDERLINK_PREFIX", "BB")[:8] or "BB",

        TRADING_SYMBOLS=parse_symbols(env, "TRADING_SYMBOLS", "BTCUSDT"),
        # DAILY, changed from hourly in slice 9 on measured evidence.
        #
        # The round-trip cost is fixed in basis points while the ATR risk unit
        # scales with the bar. On hourly BTC a 1-ATR stop is ~40 bps, so a 25
        # bps round trip is ~60% of the entire risk unit; on daily bars the
        # same stop is ~600 bps and the round trip is ~3%. Measured across 84
        # geometries on seven years of real data: ZERO hourly cells had
        # positive net expectancy, and 72 of 84 daily cells did — with the
        # take-profit and stop ratios completely unchanged.
        #
        # The geometry parameters were never the problem. The timeframe was.
        # See GEOMETRY.md.
        PRIMARY_TIMEFRAME=parse_timeframes(env, "PRIMARY_TIMEFRAME", "D")[0],
        SECONDARY_TIMEFRAMES=parse_timeframes(env, "SECONDARY_TIMEFRAMES", "W"),
        KLINE_LOOKBACK=parse_int(env, "KLINE_LOOKBACK", 300, low=100, high=1000),

        USE_LEVERAGE=parse_bool(env.get("USE_LEVERAGE"), False),
        DEFAULT_LEVERAGE=default_leverage,
        MAX_LEVERAGE_EU=max_leverage_eu,

        MAX_POSITION_SIZE_PCT=max_position,
        MAX_TOTAL_EXPOSURE_PCT=max_exposure,
        MAX_CORRELATION_EXPOSURE_PCT=parse_pct(env, "MAX_CORRELATION_EXPOSURE_PCT", 0.30),
        RISK_PER_TRADE_PCT=risk_per_trade,
        MAX_DAILY_LOSS_PCT=parse_pct(env, "MAX_DAILY_LOSS_PCT", 0.02),
        MAX_DRAWDOWN_PCT=parse_pct(env, "MAX_DRAWDOWN_PCT", 0.10),
        STOP_LOSS_PCT=parse_pct(env, "STOP_LOSS_PCT", 0.02),
        MAX_OPEN_POSITIONS=parse_int(env, "MAX_OPEN_POSITIONS", 3, low=1, high=50),
        MAX_CONSECUTIVE_LOSSES=parse_int(env, "MAX_CONSECUTIVE_LOSSES", 4, low=1, high=100),
        TRADE_COOLDOWN_SECONDS=parse_float(env, "TRADE_COOLDOWN_SECONDS", 60.0, low=0.0, high=86_400.0),
        CIRCUIT_BREAKER_HOURS=parse_float(env, "CIRCUIT_BREAKER_HOURS", 2.0, low=0.0, high=168.0),

        # Bybit spot defaults: 0.10% both sides. Linear taker/maker are lower
        # (0.055%/0.02%) but funding is charged, which the edge floor includes.
        TAKER_FEE_BPS=taker_bps,
        MAKER_FEE_BPS=maker_bps,
        SLIPPAGE_BPS=slippage_bps,
        MAX_SPREAD_BPS=parse_float(env, "MAX_SPREAD_BPS", 20.0, low=0.1, high=1000.0),
        # Default DERIVED from costs, not a fixed number: an edge floor that
        # sits below the round-trip cost accepts trades that cannot pay for
        # themselves. The +5 bps is the margin that makes a marginal trade
        # worth its execution risk.
        MIN_EDGE_BPS=parse_float(
            env, "MIN_EDGE_BPS", round_trip_bps + 5.0, low=0.0, high=10_000.0,
        ),

        MIN_CONFIDENCE=parse_pct(env, "MIN_CONFIDENCE", 0.60),
        MIN_COMPONENT_AGREEMENT=parse_pct(env, "MIN_COMPONENT_AGREEMENT", 0.60),
        MIN_RISK_REWARD_RATIO=min_rr,
        MIN_KELLY_SAMPLES=parse_int(env, "MIN_KELLY_SAMPLES", 30, low=30, high=10_000),
        STOP_ATR_MULTIPLE=parse_float(env, "STOP_ATR_MULTIPLE", 2.0, low=0.5, high=10.0),
        INDICATOR_THRESHOLDS=parse_json_dict(
            env, "INDICATOR_THRESHOLDS", DEFAULT_INDICATOR_THRESHOLDS
        ),

        PREFER_MAKER=parse_bool(env.get("PREFER_MAKER"), False),
        MAKER_TIMEOUT_SECONDS=parse_float(env, "MAKER_TIMEOUT_SECONDS", 20.0, low=1.0, high=600.0),
        MAX_DATA_AGE_SECONDS=parse_float(env, "MAX_DATA_AGE_SECONDS", 120.0, low=1.0, high=3600.0),
        BREAKEVEN_AFTER_FIRST_TP=parse_bool(env.get("BREAKEVEN_AFTER_FIRST_TP"), True),

        DATA_DIR=parse_str(env, "DATA_DIR", "data"),
        USE_ORDERBOOK=parse_bool(env.get("USE_ORDERBOOK"), True),
        BOOK_DEPTH_LEVELS=parse_int(env, "BOOK_DEPTH_LEVELS", 25, low=1, high=200),
        MIN_BOOK_DEPTH_USD=parse_float(env, "MIN_BOOK_DEPTH_USD", 25_000.0, low=0.0, high=1e9),
        MAX_BOOK_IMBALANCE=parse_pct(env, "MAX_BOOK_IMBALANCE", 0.85),
        # CoinAPI's own anomaly threshold: a quote whose spread exceeds ±67% of
        # mid is a broken feed, not a market.
        MAX_SPREAD_ANOMALY_PCT=parse_pct(env, "MAX_SPREAD_ANOMALY_PCT", 0.67),

        POLICY_MODE=policy_mode,
        POLICY_PATH=policy_path,
        POLICY_ACK=policy_ack,
        POLICY_ARMED=policy_armed,
        POLICY_BLOCK_REASON=policy_reason,
        POLICY_MIN_PROBABILITY=parse_pct(env, "POLICY_MIN_PROBABILITY", 0.0),

        MEMORY_ENABLED=parse_bool(env.get("MEMORY_ENABLED"), True),
        MEMORY_MIN_SAMPLES=parse_int(env, "MEMORY_MIN_SAMPLES", 20, low=5, high=10_000),
        MEMORY_LOOKBACK_TRADES=parse_int(env, "MEMORY_LOOKBACK_TRADES", 500, low=30, high=100_000),
        # The floor on how far recorded experience may shrink a size. It can
        # never raise one — see memory.size_multiplier.
        MEMORY_MIN_THROTTLE=parse_pct(env, "MEMORY_MIN_THROTTLE", 0.25),

        LOOP_INTERVAL_SECONDS=parse_float(env, "LOOP_INTERVAL_SECONDS", 60.0, low=0.01, high=3600.0),
        ENABLE_HEALTH_SERVER=parse_bool(env.get("ENABLE_HEALTH_SERVER"), False),
        # Loopback by default. The endpoint is unauthenticated and reports
        # positions and kill-switch state; exposing it on every interface is
        # an operator decision, not a default.
        HEALTHCHECK_HOST=parse_str(env, "HEALTHCHECK_HOST", "127.0.0.1"),
        HEALTHCHECK_PORT=parse_int(env, "HEALTHCHECK_PORT", 8081, low=1, high=65_535),
        # The decision journal grows one row per gate block per tick. A gate
        # that blocks permanently writes ~1,440 rows/day/symbol. Bounded here.
        DECISIONS_RETENTION_DAYS=parse_int(env, "DECISIONS_RETENTION_DAYS", 90,
                                           low=1, high=3_650),
        LOG_LEVEL="WARNING" if log_level == "WARN" else log_level,
        STATE_DB_PATH=parse_str(env, "STATE_DB_PATH", "state/trading_state.db"),
        # Which book this process runs. "carry" = delta-neutral spot/perp pair
        # collecting funding; "directional" = the legacy technical voter, whose
        # Stage-1 verdict is ABSENT. They are mutually exclusive: build_bot
        # attaches one and returns. Default stays "directional" because dozens
        # of tests predate the carry book, so selecting carry is a deliberate
        # operator act rather than something that happens by upgrade.
        BOOK_MODE=parse_str(env, "BOOK_MODE", "directional"),
        CARRY_SPOT_SYMBOL=parse_str(env, "CARRY_SPOT_SYMBOL", "BTCUSDT"),
        CARRY_PERP_SYMBOL=parse_str(env, "CARRY_PERP_SYMBOL", "BTCUSDT"),
        PAPER_SESSION_LOG_PATH=parse_str(env, "PAPER_SESSION_LOG_PATH", ""),
        # Default True so an existing deployment behaves exactly as before.
        # parse_bool maps every unrecognised value to False, so a typo or a
        # corrupted environment yields NO ENTRIES, never more — that is the
        # fail-closed direction for this key.
        ENTRIES_ENABLED=parse_bool(env.get("ENTRIES_ENABLED"), True),
    )
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    """Cross-field checks that a single parser cannot make."""
    problems = []
    if cfg.RISK_PER_TRADE_PCT > cfg.MAX_POSITION_SIZE_PCT:
        problems.append(
            f"RISK_PER_TRADE_PCT ({cfg.RISK_PER_TRADE_PCT}) exceeds "
            f"MAX_POSITION_SIZE_PCT ({cfg.MAX_POSITION_SIZE_PCT}): the risk "
            "budget could never be reached, so the cap would always bind"
        )
    if cfg.MAX_POSITION_SIZE_PCT > cfg.MAX_TOTAL_EXPOSURE_PCT:
        problems.append(
            f"MAX_POSITION_SIZE_PCT ({cfg.MAX_POSITION_SIZE_PCT}) exceeds "
            f"MAX_TOTAL_EXPOSURE_PCT ({cfg.MAX_TOTAL_EXPOSURE_PCT})"
        )
    if cfg.MAX_DAILY_LOSS_PCT > cfg.MAX_DRAWDOWN_PCT:
        problems.append(
            "MAX_DAILY_LOSS_PCT exceeds MAX_DRAWDOWN_PCT: the daily brake would "
            "never engage before the drawdown kill switch"
        )
    if cfg.MIN_EDGE_BPS < cfg.ROUND_TRIP_COST_BPS:
        problems.append(
            f"MIN_EDGE_BPS ({cfg.MIN_EDGE_BPS}) is below the round-trip cost "
            f"({cfg.ROUND_TRIP_COST_BPS:.1f} bps): trades would be accepted that "
            "cannot cover their own fees"
        )
    if cfg.CATEGORY == "spot" and cfg.USE_LEVERAGE:
        problems.append(
            "USE_LEVERAGE=1 with CATEGORY=spot: spot margin borrowing is not "
            "implemented. Either set CATEGORY=linear (USDT perpetual, where "
            "leverage and shorts are native) or leave leverage off"
        )
    if cfg.CATEGORY == "spot" and cfg.DEFAULT_LEVERAGE != 1:
        problems.append(
            f"DEFAULT_LEVERAGE={cfg.DEFAULT_LEVERAGE} with CATEGORY=spot: spot "
            "positions are unlevered in this codebase"
        )
    if cfg.ALLOW_SHORTS != (cfg.CATEGORY == "linear"):
        problems.append(
            "ALLOW_SHORTS is derived from CATEGORY and must not be set directly"
        )
    if cfg.POLICY_MODE == "live" and not cfg.POLICY_ARMED:
        # Not an error: the gate already degraded it to shadow and said why.
        # Recorded here so the reason travels with the config object.
        logger.warning("%s", cfg.POLICY_BLOCK_REASON)
    if cfg.POLICY_ARMED and cfg.POLICY_MODE != "live":
        problems.append(
            "POLICY_ARMED is derived from POLICY_MODE and must not be set directly"
        )
    if cfg.MEMORY_MIN_THROTTLE <= 0.0:
        problems.append(
            "MEMORY_MIN_THROTTLE must be > 0: a zero floor lets recorded "
            "experience silently size every trade to nothing"
        )
    if cfg.MAX_HOLD_HOURS / 8.0 > 12.0 and cfg.CATEGORY == "linear":
        problems.append(
            f"MAX_HOLD_HOURS={cfg.MAX_HOLD_HOURS} on a perpetual implies "
            f"{cfg.FUNDING_COST_BPS:.1f} bps of funding per trade; that is a "
            "carry position, not a trade this engine's stop model covers"
        )
    # The cost-to-bar check. Not a hard limit — the honest version needs a
    # realised ATR, which config cannot have — but a loud warning at the one
    # moment somebody is choosing a timeframe.
    bar_seconds = _TIMEFRAME_SECONDS.get(cfg.PRIMARY_TIMEFRAME, 3600)
    if bar_seconds <= 3600 and cfg.ROUND_TRIP_COST_BPS >= 15.0:
        logger.warning(
            "PRIMARY_TIMEFRAME=%s with a %.0f bps round trip: on bars this "
            "short the ATR risk unit is of the same order as the cost, so most "
            "of every R is spent on execution. Measured on seven years of real "
            "BTC data, ZERO of 84 hourly geometries had positive net "
            "expectancy and 72 of 84 daily ones did. See GEOMETRY.md.",
            cfg.PRIMARY_TIMEFRAME, cfg.ROUND_TRIP_COST_BPS,
        )
    if cfg.PRIMARY_TIMEFRAME in cfg.SECONDARY_TIMEFRAMES:
        problems.append(
            f"PRIMARY_TIMEFRAME {cfg.PRIMARY_TIMEFRAME} also appears in "
            "SECONDARY_TIMEFRAMES; a timeframe confirming itself is not confluence"
        )
    for name in ("SECONDARY_TIMEFRAMES",):
        for frame in getattr(cfg, name):
            if _TIMEFRAME_SECONDS[frame] <= _TIMEFRAME_SECONDS[cfg.PRIMARY_TIMEFRAME]:
                problems.append(
                    f"{name} contains {frame}, which is not higher than "
                    f"PRIMARY_TIMEFRAME {cfg.PRIMARY_TIMEFRAME}"
                )
    if problems:
        raise ConfigError("; ".join(problems))


# ---------------------------------------------------------------------------
# process config
# ---------------------------------------------------------------------------

_CFG: Config = load()


def get_config_object() -> Config:
    """The process-wide canonical config."""
    return _CFG


def reload() -> Config:
    """Re-read the environment and replace the process config.

    Callers holding a previous instance keep it — the object is immutable by
    design, so nothing changes underneath a running trade.
    """
    global _CFG
    _CFG = load()
    return _CFG


def setup_logging(cfg: Optional[Config] = None) -> None:
    """Configure the root logger. No module ever calls ``logging.disable``."""
    cfg = _CFG if cfg is None else cfg
    logging.basicConfig(
        level=getattr(logging, str(getattr(cfg, "LOG_LEVEL", "INFO")), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)-10s %(message)s",
        force=True,
    )


def utc_day(ts: Optional[float] = None) -> str:
    """UTC day key. One day-boundary definition for the whole codebase."""
    import time

    return datetime.fromtimestamp(
        time.time() if ts is None else ts, tz=timezone.utc
    ).strftime("%Y-%m-%d")
