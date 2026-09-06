"""technical_analysis.py — the strategy layer.

Turns closed candles into a :class:`trading_engine.TradeIntent`, or into nothing
at all. It is the only module allowed to decide *whether* to trade; every module
downstream decides only *whether it is safe to*.

WHAT REPLACED WHAT
==================
The as-received module (6,199 lines, retained at
``_dead/technical_analysis_legacy.py``) contained two unrelated signal
pipelines, four competing module-level entry points, and a busy-spin monitoring
thread that pinned a CPU core per instance — while the live path constructed a
new instance *per signal*, so the bot progressively starved itself of CPU.

Its output was not usable as a probability:

* **Confidence was explicitly uncapped.** Volume, regime and a 2.5x "booster"
  multiplier stacked, so values above 2.0 were routine. Anything comparing it
  to a threshold, or feeding it to Kelly, was comparing a number to a different
  kind of number.
* **Neutral became BUY.** A score of exactly 0 mapped to BUY at confidence
  >= 0.55, and the decision thresholds were hardcoded at 0.05/0.03 with the
  comment "allow 90%+ of signals to pass".
* **Errors increased confluence.** A failing indicator was *removed* from the
  component list, so a single surviving component reported "confluence 1.0".
* **It evaluated the unclosed candle** — textbook repainting.
* **Fallbacks were long-biased**, so during a data outage the bot leaned BUY.

THE RULES THIS MODULE ENFORCES
------------------------------
1. **Confidence is a probability in [0, 1].** It is a weighted mean of component
   votes; there is no multiplier anywhere and no path that can exceed 1.0.
2. **HOLD is the default outcome**, returned whenever the evidence is weak, the
   data is short, or anything raises. No fabricated direction, ever.
3. **A failing component counts as an abstention, not an absence.** It stays in
   the denominator, so errors *reduce* confluence rather than inflating it.
4. **Closed candles only.** The unclosed bar is dropped once, in
   ``BybitClient.get_klines``, and never re-introduced here.
5. **Multi-timeframe compares closed bars to closed bars.** A higher timeframe
   contributes only if its most recent closed bar is genuinely complete.
6. **Every threshold comes from config**, in fractions.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

import config as _config
import pure_indicators as pi
from pure_indicators import InsufficientData

logger = logging.getLogger("ta")

__all__ = [
    "Candles",
    "MarketStrategy",
    "ComponentVote",
    "Signal",
    "Regime",
    "TechnicalAnalysis",
    "detect_regime",
    "candles_from_klines",
]


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candles:
    """A block of **closed** OHLCV candles, oldest first.

    Immutable and validated on construction, so a downstream indicator cannot
    receive ragged or misordered data.
    """

    open: Tuple[float, ...]
    high: Tuple[float, ...]
    low: Tuple[float, ...]
    close: Tuple[float, ...]
    volume: Tuple[float, ...]
    timeframe: str = ""

    def __post_init__(self) -> None:
        lengths = {
            len(self.open), len(self.high), len(self.low),
            len(self.close), len(self.volume),
        }
        if len(lengths) != 1:
            raise ValueError(f"ragged candle series: lengths {sorted(lengths)}")
        for name in ("high", "low", "close", "open"):
            series = getattr(self, name)
            if any(not math.isfinite(x) or x <= 0 for x in series):
                raise ValueError(f"{name} contains a non-positive or non-finite price")
        for i, (h, l) in enumerate(zip(self.high, self.low)):
            if h < l:
                raise ValueError(f"candle {i}: high {h} is below low {l}")

    def __len__(self) -> int:
        return len(self.close)

    @property
    def last_close(self) -> float:
        return self.close[-1]


def candles_from_klines(rows: Sequence[Sequence[str]], timeframe: str = "") -> Candles:
    """Build :class:`Candles` from Bybit V5 kline rows.

    Bybit returns ``[startTime, open, high, low, close, volume, turnover]``.
    ``BybitClient.get_klines`` has already reversed them to oldest-first and
    dropped the in-progress candle; this function does **not** re-sort, because
    silently repairing ordering here would mask a bug there.
    """
    if not rows:
        raise InsufficientData("no klines supplied")
    return Candles(
        open=tuple(float(r[1]) for r in rows),
        high=tuple(float(r[2]) for r in rows),
        low=tuple(float(r[3]) for r in rows),
        close=tuple(float(r[4]) for r in rows),
        volume=tuple(float(r[5]) for r in rows),
        timeframe=timeframe,
    )


# ---------------------------------------------------------------------------
# outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComponentVote:
    """One indicator's opinion.

    ``direction`` is -1 (bearish), 0 (neutral/abstain) or +1 (bullish).
    ``strength`` is in [0, 1] and expresses conviction, not magnitude.

    An abstention (``ok=False``) still occupies a slot in the denominator. That
    is the fix for the legacy behaviour where a failing indicator was dropped
    from the list, so one surviving component reported perfect confluence.
    """

    name: str
    direction: int = 0
    strength: float = 0.0
    ok: bool = True
    detail: str = ""

    def __post_init__(self) -> None:
        if self.direction not in (-1, 0, 1):
            raise ValueError(f"direction must be -1, 0 or 1; got {self.direction}")
        if not (0.0 <= self.strength <= 1.0):
            raise ValueError(f"strength must be in [0,1]; got {self.strength}")

    @property
    def score(self) -> float:
        return 0.0 if not self.ok else float(self.direction) * float(self.strength)


class Regime:
    """Market regime labels. ``UNKNOWN`` is honest and common."""

    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Signal:
    """The strategy's verdict.

    ``confidence`` is a probability in [0, 1] — enforced in ``__post_init__``,
    not by convention.
    """

    symbol: str
    action: str = "HOLD"          # BUY | SELL | HOLD
    confidence: float = 0.0
    entry_price: float = 0.0
    stop_price: float = 0.0
    take_profits: Tuple[Tuple[float, float], ...] = ()
    regime: str = Regime.UNKNOWN
    votes: Tuple[ComponentVote, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if self.action not in ("BUY", "SELL", "HOLD"):
            raise ValueError(f"action must be BUY, SELL or HOLD; got {self.action!r}")
        if not (0.0 <= self.confidence <= 1.0) or not math.isfinite(self.confidence):
            raise ValueError(
                f"confidence must be a probability in [0,1]; got {self.confidence}. "
                "The legacy pipeline routinely produced values above 2.0."
            )

    @property
    def is_actionable(self) -> bool:
        return self.action in ("BUY", "SELL")

    @property
    def agreement(self) -> float:
        """Fraction of components that voted with the final direction.

        Abstentions count against agreement, by design.
        """
        if not self.votes:
            return 0.0
        target = 1 if self.action == "BUY" else -1 if self.action == "SELL" else 0
        if target == 0:
            return 0.0
        return sum(1 for v in self.votes if v.ok and v.direction == target) / len(self.votes)


# ---------------------------------------------------------------------------
# regime
# ---------------------------------------------------------------------------


def detect_regime(candles: Candles, adx_period: int = 14, adx_trend_min: float = 25.0) -> str:
    """Classify the regime from ADX and directional movement.

    Returns ``UNKNOWN`` rather than guessing when there is not enough data —
    downstream code multiplies by regime, so a fabricated label is a fabricated
    position size.
    """
    try:
        result = pi.adx(candles.high, candles.low, candles.close, adx_period)
    except (InsufficientData, ValueError):
        return Regime.UNKNOWN
    if result.adx.size == 0:
        return Regime.UNKNOWN
    strength = float(result.adx[-1])
    if strength < adx_trend_min:
        return Regime.RANGING
    return (
        Regime.TRENDING_UP
        if float(result.plus_di[-1]) >= float(result.minus_di[-1])
        else Regime.TRENDING_DOWN
    )


# ---------------------------------------------------------------------------
# the analyser
# ---------------------------------------------------------------------------


class TechnicalAnalysis:
    """Produces a :class:`Signal` from closed candles.

    Stateless between calls and cheap to construct — no threads, no caches, no
    network. The legacy class spawned a busy-spin monitoring thread per
    instance while the live path built one per signal.
    """

    def __init__(self, config: Any = None) -> None:
        self.cfg = config if config is not None else _config.get_config_object()
        thresholds = dict(getattr(self.cfg, "INDICATOR_THRESHOLDS", {}) or {})

        self.rsi_period = int(thresholds.get("RSI_PERIOD", 14))
        self.rsi_oversold = float(thresholds.get("RSI_OVERSOLD", 30))
        self.rsi_overbought = float(thresholds.get("RSI_OVERBOUGHT", 70))
        self.atr_period = int(thresholds.get("ATR_PERIOD", 14))
        self.bb_period = int(thresholds.get("BB_TIMEFRAME", 20))
        self.bb_std = float(thresholds.get("BB_STD", 2.0))
        self.adx_period = int(thresholds.get("ADX_PERIOD", 14))
        self.adx_trend_min = float(thresholds.get("ADX_TREND_MIN", 25))
        self.macd_fast = int(thresholds.get("MACD_FAST", 12))
        self.macd_slow = int(thresholds.get("MACD_SLOW", 26))
        self.macd_signal = int(thresholds.get("MACD_SIGNAL", 9))

        # FRACTIONS, from the one config object.
        self.min_confidence = float(getattr(self.cfg, "MIN_CONFIDENCE", 0.60))
        self.stop_atr_multiple = float(getattr(self.cfg, "STOP_ATR_MULTIPLE", 2.0))
        self.min_risk_reward = float(getattr(self.cfg, "MIN_RISK_REWARD_RATIO", 2.0))
        self.max_stop_fraction = float(getattr(self.cfg, "STOP_LOSS_PCT", 0.02)) * 3.0
        #: Minimum fraction of components that must agree before acting.
        self.min_agreement = float(getattr(self.cfg, "MIN_COMPONENT_AGREEMENT", 0.6))

    # -- components --------------------------------------------------------

    def _vote_rsi(self, candles: Candles) -> ComponentVote:
        try:
            values = pi.rsi(candles.close, self.rsi_period)
        except (InsufficientData, ValueError) as exc:
            return ComponentVote("rsi", ok=False, detail=str(exc))
        latest = float(values[-1])
        if latest <= self.rsi_oversold:
            span = max(1e-9, self.rsi_oversold)
            return ComponentVote("rsi", 1, min(1.0, (self.rsi_oversold - latest) / span),
                                 detail=f"rsi={latest:.1f}")
        if latest >= self.rsi_overbought:
            span = max(1e-9, 100.0 - self.rsi_overbought)
            return ComponentVote("rsi", -1, min(1.0, (latest - self.rsi_overbought) / span),
                                 detail=f"rsi={latest:.1f}")
        return ComponentVote("rsi", 0, 0.0, detail=f"rsi={latest:.1f}")

    def _vote_macd(self, candles: Candles) -> ComponentVote:
        try:
            result = pi.macd(candles.close, self.macd_fast, self.macd_slow,
                             self.macd_signal)
        except (InsufficientData, ValueError) as exc:
            return ComponentVote("macd", ok=False, detail=str(exc))
        histogram = float(result.histogram[-1])
        reference = float(np.mean(np.abs(result.histogram))) or 1e-9
        strength = min(1.0, abs(histogram) / (2.0 * reference))
        if histogram > 0:
            return ComponentVote("macd", 1, strength, detail=f"hist={histogram:.4f}")
        if histogram < 0:
            return ComponentVote("macd", -1, strength, detail=f"hist={histogram:.4f}")
        return ComponentVote("macd", 0, 0.0)

    def _vote_bollinger(self, candles: Candles) -> ComponentVote:
        try:
            bands = pi.bollinger(candles.close, self.bb_period, self.bb_std)
        except (InsufficientData, ValueError) as exc:
            return ComponentVote("bollinger", ok=False, detail=str(exc))
        price = candles.last_close
        upper, lower = float(bands.upper[-1]), float(bands.lower[-1])
        width = max(1e-12, upper - lower)
        if price <= lower:
            return ComponentVote("bollinger", 1, min(1.0, (lower - price) / width + 0.5),
                                 detail="below lower band")
        if price >= upper:
            return ComponentVote("bollinger", -1, min(1.0, (price - upper) / width + 0.5),
                                 detail="above upper band")
        return ComponentVote("bollinger", 0, 0.0, detail="inside bands")

    def _vote_supertrend(self, candles: Candles) -> ComponentVote:
        try:
            result = pi.supertrend(candles.high, candles.low, candles.close)
        except (InsufficientData, ValueError) as exc:
            return ComponentVote("supertrend", ok=False, detail=str(exc))
        trend = int(result.trend[-1])
        return ComponentVote("supertrend", trend, 0.7 if trend else 0.0,
                             detail=f"trend={trend}")

    def _vote_trend(self, candles: Candles) -> ComponentVote:
        """Fast/slow SMA relationship — a plain trend filter."""
        try:
            fast = pi.sma(candles.close, 20)
            slow = pi.sma(candles.close, 50)
        except (InsufficientData, ValueError) as exc:
            return ComponentVote("trend", ok=False, detail=str(exc))
        fast_last, slow_last = float(fast[-1]), float(slow[-1])
        separation = abs(fast_last - slow_last) / max(1e-12, slow_last)
        strength = min(1.0, separation / 0.02)
        if fast_last > slow_last:
            return ComponentVote("trend", 1, strength, detail="fast above slow")
        if fast_last < slow_last:
            return ComponentVote("trend", -1, strength, detail="fast below slow")
        return ComponentVote("trend", 0, 0.0)

    # -- the pipeline ------------------------------------------------------

    def analyse(
        self,
        symbol: str,
        candles: Candles,
        higher_timeframe: Optional[Candles] = None,
    ) -> Signal:
        """Produce a signal. Returns HOLD on any problem, never a guess."""
        try:
            return self._analyse(symbol, candles, higher_timeframe)
        except Exception as exc:  # noqa: BLE001
            # A crashing strategy must produce no trade, not a default direction.
            logger.exception("analysis failed for %s", symbol)
            return Signal(symbol=symbol, action="HOLD", confidence=0.0,
                          reason=f"ANALYSIS_ERROR: {type(exc).__name__}: {exc}")

    def _analyse(
        self,
        symbol: str,
        candles: Candles,
        higher_timeframe: Optional[Candles],
    ) -> Signal:
        if len(candles) < 60:
            return Signal(symbol=symbol, reason=f"INSUFFICIENT_HISTORY:{len(candles)}")

        votes: List[ComponentVote] = [
            self._vote_rsi(candles),
            self._vote_macd(candles),
            self._vote_bollinger(candles),
            self._vote_supertrend(candles),
            self._vote_trend(candles),
        ]

        # Multi-timeframe confluence: closed bars compared to closed bars.
        # `higher_timeframe` has already had its unclosed bar dropped by the
        # client, exactly like the base series — the legacy code compared a
        # partially formed higher bar to a completed lower one.
        if higher_timeframe is not None and len(higher_timeframe) >= 60:
            htf = self._vote_trend(higher_timeframe)
            votes.append(ComponentVote(
                "htf_trend", htf.direction, htf.strength, htf.ok,
                detail=f"[{higher_timeframe.timeframe}] {htf.detail}",
            ))

        # Abstentions stay in the denominator.
        total = len(votes)
        net_score = sum(v.score for v in votes) / total
        healthy = sum(1 for v in votes if v.ok)
        if healthy < max(3, total // 2):
            return Signal(symbol=symbol, votes=tuple(votes),
                          reason=f"TOO_FEW_HEALTHY_COMPONENTS:{healthy}/{total}")

        regime = detect_regime(candles, self.adx_period, self.adx_trend_min)

        action = "HOLD"
        if net_score > 0:
            action = "BUY"
        elif net_score < 0:
            action = "SELL"
        if action == "HOLD":
            return Signal(symbol=symbol, votes=tuple(votes), regime=regime,
                          reason="NO_NET_DIRECTION")

        # Confidence: |net score| scaled by agreement. Both are in [0,1], so the
        # product is too. There is no multiplier and no booster.
        target = 1 if action == "BUY" else -1
        agreement = sum(1 for v in votes if v.ok and v.direction == target) / total
        confidence = max(0.0, min(1.0, abs(net_score) * (0.5 + 0.5 * agreement)))

        if agreement < self.min_agreement:
            return Signal(symbol=symbol, votes=tuple(votes), regime=regime,
                          confidence=confidence,
                          reason=f"WEAK_AGREEMENT:{agreement:.2f}")
        if confidence < self.min_confidence:
            return Signal(symbol=symbol, votes=tuple(votes), regime=regime,
                          confidence=confidence,
                          reason=f"BELOW_MIN_CONFIDENCE:{confidence:.3f}")

        # A trend-following signal against a clearly opposing trend is dropped.
        if regime == Regime.TRENDING_DOWN and action == "BUY":
            return Signal(symbol=symbol, votes=tuple(votes), regime=regime,
                          confidence=confidence, reason="AGAINST_DOWNTREND")
        if regime == Regime.TRENDING_UP and action == "SELL":
            return Signal(symbol=symbol, votes=tuple(votes), regime=regime,
                          confidence=confidence, reason="AGAINST_UPTREND")

        levels = self._levels(candles, action)
        if levels is None:
            return Signal(symbol=symbol, votes=tuple(votes), regime=regime,
                          confidence=confidence, reason="NO_VALID_LEVELS")
        entry, stop, take_profits = levels

        return Signal(
            symbol=symbol, action=action, confidence=confidence,
            entry_price=entry, stop_price=stop, take_profits=take_profits,
            regime=regime, votes=tuple(votes), reason="OK",
        )

    def _levels(
        self, candles: Candles, action: str
    ) -> Optional[Tuple[float, float, Tuple[Tuple[float, float], ...]]]:
        """ATR-based stop and take-profit ladder.

        Returns ``None`` rather than substituting a default when ATR is
        unusable. The legacy ATR returned zeros on error, which produced a stop
        at the entry price.
        """
        try:
            atr_value = pi.atr_last(
                candles.high, candles.low, candles.close, self.atr_period
            )
        except (InsufficientData, ValueError):
            return None

        entry = candles.last_close
        distance = self.stop_atr_multiple * atr_value
        # Cap the stop distance so a volatility spike cannot produce an
        # enormous risk-per-unit that the sizer then divides by.
        distance = min(distance, entry * self.max_stop_fraction)
        if distance <= 0:
            return None

        # The ladder's SIZE-WEIGHTED AVERAGE must equal the configured minimum
        # risk/reward, not merely its farthest leg. An earlier version placed
        # legs at 0.5x and 1.0x of the required ratio, which averages to 0.75x —
        # so the strategy emitted trades that its own risk/reward gate would
        # (correctly) reject. Legs at 0.6x and 1.4x average to exactly 1.0x.
        near, far = 0.6, 1.4
        reward = distance * self.min_risk_reward
        if action == "BUY":
            stop = entry - distance
            legs = ((entry + reward * near, 0.5), (entry + reward * far, 0.5))
        else:
            stop = entry + distance
            legs = ((entry - reward * near, 0.5), (entry - reward * far, 0.5))
        if stop <= 0:
            return None
        return entry, stop, legs

    # -- the seam the orchestrator uses ------------------------------------

    def signal_for(self, symbol: str) -> None:
        """Present so ``TradingBot`` can accept a ``TechnicalAnalysis`` directly.

        It always returns ``None`` — this class has no market-data source of its
        own by design. Use :class:`MarketStrategy`, which composes an analyser
        with a client.
        """
        return None


class MarketStrategy:
    """Adapter: exchange candles in, ``TradeIntent`` out.

    This is the seam between the strategy layer and the execution stack. It is
    the *only* place a :class:`Signal` becomes a :class:`~trading_engine.TradeIntent`,
    so the mapping from "what we think" to "what we will do" exists once.
    """

    def __init__(
        self,
        client: Any,
        analyser: Optional[TechnicalAnalysis] = None,
        interval: str = "60",
        higher_interval: Optional[str] = "240",
        lookback: int = 300,
        config: Any = None,
        memory: Any = None,
    ) -> None:
        self.client = client
        self.cfg = config if config is not None else _config.get_config_object()
        self.analyser = analyser or TechnicalAnalysis(config=self.cfg)
        self.interval = interval
        self.higher_interval = higher_interval
        self.lookback = int(lookback)
        self.last_signal: Optional[Signal] = None
        #: The closed candles the last signal was computed from. Exposed so a
        #: model wrapper can reason about the SAME bars rather than re-fetching:
        #: two fetches a few hundred milliseconds apart can straddle a bar
        #: close, and then the model and the strategy are reasoning about
        #: different markets while appearing to agree.
        self.last_candles: Optional[Candles] = None
        #: The raw kline rows behind ``last_candles``. ``Candles`` is columnar
        #: and carries no timestamps; the feature pipeline needs per-bar times
        #: for its cyclical time encodings, so the rows are kept as well rather
        #: than re-fetched (a second fetch can straddle a bar close).
        self.last_rows: Optional[Sequence[Sequence[str]]] = None
        #: Optional reference layer. Present, suppressed shorts and observed
        #: regimes are recorded; absent, the strategy behaves identically.
        self.memory = memory
        #: Counts of directional views this venue could not express. Cheap,
        #: in-process, and the honest denominator for "is spot costing us?".
        self.suppressed_shorts: int = 0

    def _journal_suppressed_short(self, symbol: str, signal: Signal) -> None:
        """Count and record a directional view this venue cannot express."""
        self.suppressed_shorts += 1
        if self.memory is None:
            return
        try:
            self.memory.remember(
                "strategy", f"suppressed_shorts:{symbol}",
                {"count": self.suppressed_shorts,
                 "last_confidence": round(float(signal.confidence), 4),
                 "last_regime": signal.regime},
            )
        except Exception:  # noqa: BLE001
            logger.debug("could not record suppressed short for %s", symbol)

    def signal_for(self, symbol: str):
        """Return a ``TradeIntent`` or ``None``. Never raises into the loop."""
        from trading_engine import TradeIntent   # local import: avoids a cycle

        try:
            rows = self.client.get_klines(symbol, self.interval, self.lookback)
            candles = candles_from_klines(rows, self.interval)
            higher = None
            if self.higher_interval:
                try:
                    higher_rows = self.client.get_klines(
                        symbol, self.higher_interval, self.lookback
                    )
                    higher = candles_from_klines(higher_rows, self.higher_interval)
                except Exception:  # noqa: BLE001
                    # A missing higher timeframe means one fewer component, not
                    # a fabricated one.
                    higher = None
            self.last_candles = candles
            self.last_rows = rows
            signal = self.analyser.analyse(symbol, candles, higher)
        except Exception as exc:  # noqa: BLE001
            logger.exception("strategy failed for %s", symbol)
            self.last_signal = Signal(symbol=symbol, reason=f"ERROR: {exc}")
            return None

        self.last_signal = signal
        if not signal.is_actionable:
            logger.debug("%s: %s (%s)", symbol, signal.action, signal.reason)
            return None

        # A SELL on a venue that cannot short is not a trade, and it is not a
        # HOLD either — it is a real directional view the account cannot express.
        # Suppressing it silently would make a long-only bot look like it agreed
        # with the market; it is recorded with its own reason so the operator can
        # count how much opinion the spot-only constraint is discarding, and
        # decide from evidence whether CATEGORY=linear is worth it.
        if signal.action == "SELL" and not getattr(self.cfg, "ALLOW_SHORTS", False):
            self.last_signal = replace(
                signal, reason=f"SHORT_SUPPRESSED_ON_{self.cfg.CATEGORY.upper()}"
            )
            logger.info(
                "%s: short signal suppressed (%s cannot short); confidence was %.3f",
                symbol, self.cfg.CATEGORY, signal.confidence,
            )
            self._journal_suppressed_short(symbol, signal)
            return None

        return TradeIntent(
            symbol=signal.symbol,
            signal_type=signal.action,
            entry_price=signal.entry_price,
            stop_price=signal.stop_price,
            take_profits=signal.take_profits,
            confidence=signal.confidence,
            reason=f"{signal.regime}|{signal.reason}",
        )
