"""ml_strategy.py — the seam where a learned model meets the execution stack.

WHAT THIS MODULE IS FOR
=======================
Slice 8 added a trainable policy (:mod:`policy`) and a feature pipeline
(:mod:`features`). This module is the *only* place their output is allowed to
influence a trade, and its entire design is about limiting what that influence
can be.

    A model may propose. It may not decide, resize, override, or resume.

Concretely, and enforced by construction rather than by convention:

* The model's output enters the system as **one field**:
  ``TradeIntent.win_probability``, a calibrated probability. It does not set a
  quantity, a limit, a stop, or a threshold.
* Every gate in :mod:`risk_management` still runs, in the same order, with the
  same limits. The model cannot skip one, and there is no code path here that
  constructs an order.
* ``memory.size_multiplier`` still applies afterwards and still only shrinks.
* Nothing here can clear the kill switch. The string does not appear in this
  file and a test asserts that.

WHY A SEPARATE MODULE RATHER THAN A FLAG ON MarketStrategy
----------------------------------------------------------
Because "the model is on" and "the model is trusted" are different questions,
and a boolean on the existing strategy would have collapsed them. Here they are
three explicit states — ``off``, ``shadow``, ``live`` — resolved once in
``config``, and the middle one is the important one:

**Shadow mode is where an unproven model belongs.** It sees exactly the data it
would see in production, forms exactly the decision it would form, and its
decision is written to the journal and to memory. It cannot place an order,
because in shadow mode this module never returns an intent at all. After a few
weeks of that, an operator has something no backtest can produce: a record of
what the model would have done on data that did not exist when it was trained.

THE PROMOTION DIRECTION
-----------------------
Promotion is manual and demotion is automatic. That asymmetry is deliberate and
it is the opposite of what an "autonomous agent" would do:

* To promote, a human sets ``POLICY_MODE=live`` **and** the exact
  ``POLICY_ACK`` token, having read a training report where the model already
  cleared ``policy.meets_promotion_criteria``. Three separate acts.
* To demote, nothing is required. :class:`PolicyDriftMonitor` compares realised
  outcomes against what the model predicted and, when they diverge past a
  threshold, the model stops being consulted for live proposals.

Automatic promotion is how a system talks itself into a bigger position after a
lucky streak. Automatic demotion is how it stops paying for a broken one.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import config as _config

logger = logging.getLogger("ml_strategy")


class _Bar(NamedTuple):
    """The minimal bar shape ``features.compute_features`` accepts.

    Deliberately local and deliberately minimal. Importing ``backtest.Bar``
    would drag the evaluation harness into the live process, and importing
    ``market_data.Bar`` would pull in its microsecond timestamp machinery for
    no benefit — the feature pipeline documents that it accepts *any* object
    with these six attributes, and this is the smallest thing that is one.
    """

    start_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float

__all__ = [
    "PolicyStrategy",
    "ShadowDecision",
    "PolicyDriftMonitor",
    "load_policy_for",
    "PolicyUnavailableReason",
]


class PolicyUnavailableReason:
    """Why no model is being consulted. Every one of these is a normal state.

    A missing or unpromoted model is not an error condition — it is the default,
    and the correct behaviour is to run the classical strategy unchanged. These
    strings exist so an operator can tell *which* normal state they are in
    without reading the code.
    """

    MODE_OFF = "POLICY_MODE=off"
    NO_SKLEARN = "scikit-learn is not installed"
    NO_ARTEFACT = "no model artefact at POLICY_PATH"
    SCHEMA_MISMATCH = "the artefact was trained against a different feature schema"
    NOT_PROMOTED = "the artefact did not clear its promotion criteria"
    LOAD_FAILED = "the artefact could not be loaded"
    DRIFTED = "live outcomes diverged from the model's predictions"


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


def load_policy_for(cfg: Any) -> Tuple[Optional[Any], str]:
    """``(policy_or_None, reason)`` — load the model, or say why there is none.

    Never raises. A trading loop that cannot start because a *model* is missing
    is a trading loop with its priorities inverted: the model is an enhancement,
    the execution stack is the product.

    Every failure mode collapses to the same safe outcome — no model — and the
    reason travels with it so the health endpoint can show it.
    """
    mode = str(getattr(cfg, "POLICY_MODE", "off")).lower()
    if mode == "off":
        return None, PolicyUnavailableReason.MODE_OFF

    try:
        import features as _features
        import policy as _policy
    except Exception as exc:  # noqa: BLE001
        return None, f"{PolicyUnavailableReason.LOAD_FAILED}: {exc}"

    if not _policy.sklearn_available():
        return None, (
            f"{PolicyUnavailableReason.NO_SKLEARN}: {_policy.unavailability_reason()}"
        )

    path = str(getattr(cfg, "POLICY_PATH", "models/current"))
    loaded, problem = _policy.try_load(
        path,
        feature_names=list(_features.FEATURE_NAMES),
        schema_version=_features.FEATURE_SCHEMA_VERSION,
        # The promotion check is enforced at LOAD time, not at prediction time.
        # A model that failed its criteria should never occupy memory in a
        # trading process; there is no scenario in which the right answer is
        # "load it and remember not to use it".
        require_promotion=True,
    )
    if loaded is None:
        return None, problem or PolicyUnavailableReason.NO_ARTEFACT
    logger.info("policy loaded from %s (%s)", path, loaded.artefact_hash[:12])
    return loaded, ""


# ---------------------------------------------------------------------------
# what a shadow decision records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShadowDecision:
    """One decision the model formed but was not permitted to act on.

    Recorded whole, including the classical strategy's own view at the same
    bar, because the question an operator eventually has to answer is not "was
    the model right" but "was it right *where it disagreed*". A record that
    keeps only the model's side cannot answer that.
    """

    symbol: str
    epoch: float
    model_direction: Optional[str]
    model_probability: Optional[float]
    model_usable: bool
    model_reason: str
    classical_action: str
    classical_confidence: Optional[float]
    agreed: Optional[bool]
    detail: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "epoch": round(self.epoch, 3),
            "model_direction": self.model_direction,
            "model_probability": (
                None if self.model_probability is None
                else round(float(self.model_probability), 6)
            ),
            "model_usable": self.model_usable,
            "model_reason": self.model_reason,
            "classical_action": self.classical_action,
            "classical_confidence": (
                None if self.classical_confidence is None
                else round(float(self.classical_confidence), 6)
            ),
            "agreed": self.agreed,
            **self.detail,
        }


# ---------------------------------------------------------------------------
# the strategy wrapper
# ---------------------------------------------------------------------------


class PolicyStrategy:
    """Wraps a classical strategy and, optionally, a model.

    It is a drop-in for ``MarketStrategy``: ``signal_for(symbol)`` returns a
    ``TradeIntent`` or ``None``, and ``TradingBot`` cannot tell the difference.
    That is on purpose — the orchestrator should not grow a second code path for
    "the ML case", because two paths is how one of them stops being tested.

    The model's contribution is bounded to filling in
    ``TradeIntent.win_probability``. Everything else about the intent — symbol,
    direction, entry, stop, the take-profit ladder — comes from the classical
    strategy, which is still the thing that has to make geometric sense.

    Three behaviours, one per mode:

    ``off``
        Pure pass-through. Not a wrapper that happens to do nothing: the model
        is never even loaded.

    ``shadow``
        The classical intent passes through **untouched**, with no
        ``win_probability``, exactly as if this module did not exist. The model
        is evaluated and its decision is journaled. This is the mode in which
        the model's presence is provably unable to change a single order.

    ``live``
        The model's calibrated probability is attached to the intent, where the
        edge gate consumes it. If the model is unusable at that bar — missing
        features, a cold window, a mismatched schema — the probability is left
        ``None`` and the gate falls back to judging the reward leg alone, which
        is *stricter*. A broken model therefore makes the system more cautious,
        not less. That direction is the whole design.
    """

    def __init__(
        self,
        base_strategy: Any,
        *,
        config: Any = None,
        policy: Any = None,
        memory: Any = None,
        store: Any = None,
        drift_monitor: Optional["PolicyDriftMonitor"] = None,
    ) -> None:
        self.base = base_strategy
        self.cfg = config if config is not None else _config.get_config_object()
        self.memory = memory
        self.store = store
        self.mode = str(getattr(self.cfg, "POLICY_MODE", "off")).lower()
        self.armed = bool(getattr(self.cfg, "POLICY_ARMED", False))
        self.min_probability = float(getattr(self.cfg, "POLICY_MIN_PROBABILITY", 0.0))

        if policy is not None:
            self.policy, self.unavailable_reason = policy, ""
        else:
            self.policy, self.unavailable_reason = load_policy_for(self.cfg)

        self.drift = drift_monitor
        #: Every shadow decision this process has formed. Bounded, because an
        #: unbounded list in a process that runs for weeks is a memory leak with
        #: a plausible excuse.
        self.shadow_log: List[ShadowDecision] = []
        self._shadow_log_limit = 5_000
        self.decisions_evaluated = 0
        self.decisions_usable = 0
        self.disagreements = 0

    # -- the seam ---------------------------------------------------------

    @property
    def last_signal(self) -> Any:
        """Delegated so the orchestrator's logging and health keep working."""
        return getattr(self.base, "last_signal", None)

    def may_propose(self) -> Tuple[bool, str]:
        """Is the model permitted to influence an order right now?

        Four conditions, all required. Any one of them failing means the
        classical strategy runs alone — which is a working system, not a
        degraded one.
        """
        if self.policy is None:
            return False, self.unavailable_reason or PolicyUnavailableReason.NO_ARTEFACT
        if self.mode != "live":
            return False, f"POLICY_MODE={self.mode}"
        if not self.armed:
            return False, str(getattr(self.cfg, "POLICY_BLOCK_REASON", "not armed"))
        if self.drift is not None and self.drift.has_drifted():
            return False, f"{PolicyUnavailableReason.DRIFTED}: {self.drift.summary()}"
        return True, "armed"

    def signal_for(self, symbol: str):
        """Return a ``TradeIntent`` or ``None``. Never raises into the loop."""
        intent = self.base.signal_for(symbol)

        if self.policy is None:
            return intent

        decision = self._evaluate(symbol)
        self._journal(symbol, decision, intent)

        allowed, reason = self.may_propose()
        if not allowed:
            # Shadow, unarmed, or drifted. The intent goes through EXACTLY as
            # the classical strategy produced it — no probability attached, so
            # the edge gate uses its strict fallback and the model has provably
            # changed nothing.
            return intent

        if intent is None or decision is None or not decision.usable:
            return intent

        return self._attach_probability(intent, decision)

    # -- internals --------------------------------------------------------

    def _evaluate(self, symbol: str):
        """Ask the model. Returns a ``PolicyDecision`` or ``None``.

        Every failure here yields ``None`` and is logged, never raised. The
        model sits beside the trading loop, not inside it.
        """
        self.decisions_evaluated += 1
        try:
            import features as _features

            candles = self._candles_for(symbol)
            if candles is None:
                return None
            vector = _features.compute_features(candles, book=self._book_for(symbol))
            if not vector.warm:
                # A cold window produces values that are present and
                # off-distribution — the most dangerous shape of wrong, because
                # nothing downstream can see it. Refuse rather than serve.
                return None
            decision = self.policy.predict_edge(
                vector.as_dict(),
                schema_version=_features.FEATURE_SCHEMA_VERSION,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("policy evaluation failed for %s: %s", symbol, exc)
            return None
        if getattr(decision, "usable", False):
            self.decisions_usable += 1
        return decision

    def _candles_for(self, symbol: str):
        """The bars the classical strategy just used, in the shape features want.

        Two constraints meet here.

        First, it must be the **same** bars. Re-fetching would be both wasteful
        and wrong: two fetches a few hundred milliseconds apart can straddle a
        bar close, and then the model and the strategy are reasoning about
        different markets while appearing to agree. ``MarketStrategy`` therefore
        stashes what it used, and this reads that.

        Second, the shapes differ. ``technical_analysis.Candles`` is columnar
        and carries no timestamps; ``features.compute_features`` wants a
        sequence of bar objects with ``start_ms``, because several of its
        features are cyclical encodings of the hour and weekday. The raw kline
        rows have the times, so the conversion happens from those.

        Returns ``None`` — never a partial series — when the rows are absent.
        A model asked about bars that do not exist should not answer.
        """
        rows = getattr(self.base, "last_rows", None)
        if not rows:
            return None
        try:
            return [
                _Bar(int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                     float(r[4]), float(r[5]))
                for r in rows
            ]
        except (TypeError, ValueError, IndexError):
            return None

    def _book_for(self, symbol: str):
        client = getattr(self.base, "client", None)
        reader = getattr(client, "get_book_features", None)
        if not callable(reader):
            return None
        try:
            return reader(symbol)
        except Exception:  # noqa: BLE001
            return None

    def _attach_probability(self, intent: Any, decision: Any) -> Any:
        """Fill in ``win_probability``, or return the intent unchanged.

        The only mutation this module performs on an order-bound object, and it
        is a copy — ``TradeIntent`` is frozen.

        Two refusals worth naming:

        * If the model's direction contradicts the classical intent, the
          probability is **not** attached and the intent is returned as-is. The
          model does not get to reverse a trade; it gets to be ignored. A
          disagreement is recorded, because a persistent one is information.
        * If ``POLICY_MIN_PROBABILITY`` is set and the estimate falls below it,
          nothing is attached — so the strict reward-only fallback applies. The
          model may raise this system's caution and may not lower it.
        """
        from dataclasses import replace as _replace

        side = getattr(intent, "side", None)
        model_side = self._side_of(decision)
        if model_side is not None and side is not None and model_side != side:
            self.disagreements += 1
            logger.info(
                "model disagrees with the classical signal on %s "
                "(model=%s classical=%s); the classical intent stands unmodified",
                getattr(intent, "symbol", "?"), model_side, side,
            )
            return intent

        probability = self._probability_in_direction(decision, side)
        if probability is None or not math.isfinite(probability):
            return intent
        if probability < self.min_probability:
            return intent
        return _replace(intent, win_probability=float(probability))

    @staticmethod
    def _side_of(decision: Any) -> Optional[str]:
        direction = str(getattr(decision, "direction", "") or "").upper()
        if "LONG" in direction:
            return "Buy"
        if "SHORT" in direction:
            return "Sell"
        return None

    @staticmethod
    def _probability_in_direction(decision: Any, side: Optional[str]) -> Optional[float]:
        """P(this trade wins), oriented to the trade we are actually taking.

        ``Policy.probability`` is P(long wins). For a short that is ``1 - p``.
        Getting this backwards would make the gate most permissive exactly when
        the model is most against the trade, so the conversion lives in one
        place and is tested directly.
        """
        raw = getattr(decision, "probability_in_direction", None)
        if raw is not None:
            return float(raw)
        probability = getattr(decision, "probability", None)
        if probability is None:
            return None
        probability = float(probability)
        return probability if side != "Sell" else 1.0 - probability

    def _journal(self, symbol: str, decision: Any, intent: Any) -> None:
        """Record what the model thought. Never raises into the trade path."""
        if decision is None and self.policy is None:
            return
        signal = self.last_signal
        classical_action = str(getattr(signal, "action", "HOLD") or "HOLD")
        model_side = self._side_of(decision) if decision is not None else None
        intent_side = getattr(intent, "side", None)

        record = ShadowDecision(
            symbol=symbol,
            epoch=_now(),
            model_direction=model_side,
            model_probability=(
                None if decision is None else getattr(decision, "probability", None)
            ),
            model_usable=bool(getattr(decision, "usable", False)),
            model_reason=str(getattr(decision, "reason", "NO_DECISION")),
            classical_action=classical_action,
            classical_confidence=getattr(signal, "confidence", None),
            agreed=(
                None if (model_side is None or intent_side is None)
                else model_side == intent_side
            ),
            detail={"mode": self.mode, "armed": self.armed},
        )
        self.shadow_log.append(record)
        if len(self.shadow_log) > self._shadow_log_limit:
            del self.shadow_log[: len(self.shadow_log) - self._shadow_log_limit]

        try:
            if self.store is not None:
                self.store.journal(symbol, "POLICY", record.model_reason,
                                   record.as_dict())
            if self.memory is not None:
                self.memory.remember("policy", f"last_decision:{symbol}",
                                     record.as_dict())
        except Exception:  # noqa: BLE001
            logger.debug("could not journal the policy decision for %s", symbol)

    # -- reporting --------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """A compact view for the health endpoint. Read-only, no credentials."""
        allowed, reason = self.may_propose()
        return {
            "mode": self.mode,
            "armed": self.armed,
            "may_propose": allowed,
            "reason": reason,
            "model_loaded": self.policy is not None,
            "model_hash": (
                None if self.policy is None
                else str(getattr(self.policy, "artefact_hash", ""))[:12]
            ),
            "unavailable_reason": self.unavailable_reason or None,
            "decisions_evaluated": self.decisions_evaluated,
            "decisions_usable": self.decisions_usable,
            "disagreements": self.disagreements,
            "shadow_records": len(self.shadow_log),
            "drift": None if self.drift is None else self.drift.snapshot(),
        }


# ---------------------------------------------------------------------------
# drift
# ---------------------------------------------------------------------------


class PolicyDriftMonitor:
    """Compares what the model predicted against what actually happened.

    The measure is the **Brier score** on live outcomes — mean squared error
    between the predicted probability and the realised 0/1 result — against the
    Brier score the model achieved out-of-sample at training time. Squared error
    is the right measure here because it is what the edge gate's arithmetic is
    sensitive to: the gate multiplies ``p`` by a reward, so being wrong by 0.2
    hurts four times as much as being wrong by 0.1.

    Its only authority is to make ``PolicyStrategy.may_propose`` return False.
    It cannot resize a position, trip the kill switch, or retrain anything. When
    it fires, the system reverts to the classical strategy — which is a state
    the operator has already seen working.

    It never un-fires on its own. A model that drifted and then looked fine
    again for twenty trades has not been vindicated; it has produced twenty
    trades. Clearing it is a human act, like every other re-arming in this
    codebase.
    """

    def __init__(
        self,
        *,
        baseline_brier: Optional[float] = None,
        min_samples: int = 50,
        tolerance: float = 0.05,
    ) -> None:
        self.baseline_brier = (
            None if baseline_brier is None else float(baseline_brier)
        )
        self.min_samples = int(min_samples)
        self.tolerance = float(tolerance)
        self._predictions: List[Tuple[float, float]] = []
        self._drifted = False
        self._drift_reason = ""

    def record_outcome(self, predicted_probability: float, won: bool) -> None:
        """One resolved trade: what we said, and what happened."""
        probability = float(predicted_probability)
        if not math.isfinite(probability) or not (0.0 <= probability <= 1.0):
            return
        self._predictions.append((probability, 1.0 if won else 0.0))
        self._evaluate()

    def _evaluate(self) -> None:
        if self._drifted or self.baseline_brier is None:
            return
        if len(self._predictions) < self.min_samples:
            return
        live = self.live_brier()
        if live is None:
            return
        if live > self.baseline_brier + self.tolerance:
            self._drifted = True
            self._drift_reason = (
                f"live Brier {live:.4f} exceeds the training baseline "
                f"{self.baseline_brier:.4f} by more than {self.tolerance:.4f} "
                f"over {len(self._predictions)} resolved trades"
            )
            logger.critical(
                "POLICY DRIFT: %s. Reverting to the classical strategy; a human "
                "must review before the model is consulted again.",
                self._drift_reason,
            )

    def live_brier(self) -> Optional[float]:
        if not self._predictions:
            return None
        return sum((p - y) ** 2 for p, y in self._predictions) / len(self._predictions)

    def has_drifted(self) -> bool:
        return self._drifted

    def summary(self) -> str:
        return self._drift_reason or "no drift detected"

    def snapshot(self) -> Dict[str, Any]:
        live = self.live_brier()
        return {
            "samples": len(self._predictions),
            "min_samples": self.min_samples,
            "baseline_brier": self.baseline_brier,
            "live_brier": None if live is None else round(live, 6),
            "tolerance": self.tolerance,
            "drifted": self._drifted,
            "reason": self._drift_reason or None,
        }


def _now() -> float:
    """Wall clock, in epoch seconds. Never ``perf_counter``."""
    import time

    return time.time()
