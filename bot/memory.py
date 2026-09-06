"""memory.py — the shared reference layer: what this bot has learned, durably.

WHY THIS MODULE EXISTS
======================
Every module in this stack already *records* something.  ``trades`` holds
fee-inclusive outcomes, ``decisions`` holds the reason every gate blocked,
``positions`` holds what is open.  What none of them provide is a place to
*read* that experience back in a form a decision can use.  So the knowledge was
being thrown away at the point it became useful:

* The Kelly sizer read raw returns and nothing about *which symbol* produced
  them, so a symbol that has lost money in eleven of its last twelve trades was
  sized identically to one that has not.
* ``MIN_EDGE_BPS`` is derived from an **assumed** round-trip cost.  Nothing
  measured whether the assumption was true, so the single number the whole
  edge calculation rests on had never been checked against a real fill.
* The operator could see that trades were being blocked but not *what* was
  blocking them, because the journal is append-only prose with no aggregate.
* Regime labels were computed every cycle and discarded, so "how has this
  actually done in this regime before" was unanswerable.

This module is the answer to one operator sentence: *"enable this trading bot
remember all important information, and this information be accessed by all the
functions and folders and triggers that we have in place."*

WHAT IT IS NOT
--------------
1. **Not a second source of truth.**  Almost everything here is *derived* at
   read time from the tables that already exist.  The one genuinely new fact is
   execution quality — intended price versus fill price — because that
   comparison is nowhere else in the store.  Regime outcomes are computed by
   joining ``regime_history`` back to ``trades``; there is deliberately no
   outcome column to drift from the ledger.
2. **Not a risk system.**  This module cannot block, allow, trip, or clear
   anything.  It exposes exactly one number that touches trading —
   :meth:`TradingMemory.size_multiplier` — and that number can only ever
   *shrink* a position.  It never touches a risk limit, a gate outcome, the
   circuit breaker, or the kill switch.  ``tests/test_memory.py`` asserts this
   structurally, over the AST, not by convention.
3. **Not an oracle.**  Below ``cfg.MEMORY_MIN_SAMPLES`` every statistic is
   ``None``, in the same spirit as ``performance_analytics``: a win rate from
   four trades is not a low-confidence win rate, it is noise with a decimal
   point.  ``None`` and ``0.0`` mean different things and are never conflated.

THE HARD RULE
-------------
``size_multiplier`` returns a value in ``[cfg.MEMORY_MIN_THROTTLE, 1.0]`` and
**can never exceed 1.0**.  This is enforced three ways, because a sizing
multiplier that can exceed one is a leverage mechanism that nobody reviewed:

* the function has a single ``return``, and it is the clamp;
* the clamp caps at a literal ``1.0`` *after* clamping the configured floor to
  ``1.0``, so even a corrupted ``MEMORY_MIN_THROTTLE`` of ``5.0`` cannot leak
  through;
* every input path is filtered for non-finite values before it reaches the
  arithmetic, and any that survives collapses to the floor, not to a boost.

READ-CHEAP, WRITE-GUARDED
-------------------------
Every read here goes through ``StateStore._query``, which does **not** require
the writer role — the health thread reads this on its own thread and must never
be blocked or refused.  Every write (:meth:`record_execution`,
:meth:`record_regime`, :meth:`remember`) goes through the normal single-writer
path and is refused from a non-writer thread exactly like any other mutation.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from persistence import (
    MEMORY_KEY_CONVENTION,
    PersistenceError,
    StateStore,
    slippage_bps,
    utc_now_epoch,
)

logger = logging.getLogger("memory")

__all__ = [
    "TradingMemory",
    "SymbolExperience",
    "ExecutionQualityStats",
    "RegimeStats",
    "MemoryRefused",
    "BLOCKING_DECISIONS",
    "ADVERSE_EDGE_REFERENCE_BPS",
    "MIN_EXECUTION_SAMPLES",
    "REDACTED",
    "SNAPSHOT_SCHEMA",
    "MEMORY_KEY_CONVENTION",
]

#: Journal verdicts that mean "this trade did not happen because a gate said
#: no".  ``EMERGENCY`` and ``CLOSED`` are outcomes, not blocks, and are excluded
#: — counting them as blocks would inflate the very number the operator uses to
#: decide whether the gates are too tight.
BLOCKING_DECISIONS: Tuple[str, ...] = ("BLOCK", "BLOCKED", "REJECT", "REJECTED", "DENY")

#: The realised edge, in bps, at which the size throttle reaches its floor.
#: A symbol losing an average of 100 bps per trade net of fees has demonstrated
#: the opposite of an edge; between 0 and -100 the throttle interpolates.
#: This is a *shrink* scale only — a positive realised edge earns no boost,
#: because "it worked before" is not additional risk budget.
ADVERSE_EDGE_REFERENCE_BPS = 100.0

#: Execution records needed before slippage may influence sizing at all.
MIN_EXECUTION_SAMPLES = 10

#: What a redacted value looks like in a snapshot.
REDACTED = "<redacted>"

#: Version tag on :meth:`TradingMemory.snapshot` output, so a consumer (the
#: health endpoint, an offline learning session) can tell what shape it got.
SNAPSHOT_SCHEMA = "trading_memory/1"

#: Anything whose *name* matches this never appears in a snapshot and can never
#: be written through :meth:`TradingMemory.remember`.  The pattern is
#: deliberately over-broad: a false positive costs one redacted field, a false
#: negative puts an API secret in a health response.
_SECRET_NAME_RE = re.compile(
    r"(?i)(api[_-]?key|secret|passw|pwd|token|credential|private[_-]?key|"
    r"signature|bearer|session[_-]?id|\bauth\b|access[_-]?key)"
)

#: Shortest credential-looking string worth scanning for in snapshot values.
#: Below this a "secret" is more likely to be a coincidence than a leak.
_MIN_SECRET_LENGTH = 6


class MemoryRefused(PersistenceError):
    """A memory write was refused on principle rather than failing technically.

    Subclasses :class:`PersistenceError` so callers that already treat
    persistence failures as blocking need no new handler.
    """


# ---------------------------------------------------------------------------
# value types — every optional field means "not enough data", never zero
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SymbolExperience:
    """What the ledger says about one symbol, fee-inclusive throughout.

    Constructed only when there are at least ``MEMORY_MIN_SAMPLES`` usable
    trades; below that :meth:`TradingMemory.symbol_experience` returns ``None``
    and this object is never built.  There is therefore no "insufficient data"
    variant of it to mistake for a real one.
    """

    symbol: str
    samples: int
    wins: int
    losses: int
    win_rate: float
    expectancy: float          # mean NET pnl per trade, in quote currency
    mean_return: float         # fraction of the trade's own notional
    median_return: float       # fraction
    edge_bps: float            # mean_return expressed in bps
    total_net_pnl: float
    total_fees: float
    mean_mae_bps: Optional[float] = None   # max adverse excursion, if recorded
    worst_mae_bps: Optional[float] = None
    mae_samples: int = 0
    first_epoch: float = 0.0
    last_updated: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class ExecutionQualityStats:
    """Intended versus achieved.  The measurement that can falsify the cost model.

    ``scope`` is a symbol, or ``"*"`` for the whole account.  Every metric is
    ``None`` when its own sub-sample is empty: a maker fill rate with no fills
    is not 0.0, and a mean slippage over zero measurements is not 0.0 either.
    """

    scope: str
    samples: int
    fills: int
    partials: int
    rejects: int
    cancels: int
    unfilled: int
    mean_slippage_bps: Optional[float] = None
    median_slippage_bps: Optional[float] = None
    worst_slippage_bps: Optional[float] = None
    slippage_samples: int = 0
    maker_fill_rate: Optional[float] = None
    mean_time_to_fill_s: Optional[float] = None
    fill_time_samples: int = 0
    reject_reasons: Dict[str, int] = field(default_factory=dict)
    cancel_reasons: Dict[str, int] = field(default_factory=dict)
    last_updated: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class RegimeStats:
    """How trades opened under one regime label actually turned out.

    Unlike :class:`SymbolExperience` this object *is* built below the sample
    floor, because "we have taken 3 trades in chop and know nothing about it" is
    itself information the operator wants on the screen.  The judgement fields
    (``win_rate``, ``expectancy``) are ``None`` until the floor is met, and
    ``insufficient_data`` says so explicitly rather than leaving the caller to
    infer it from a ``None``.
    """

    regime: str
    samples: int
    wins: int
    losses: int
    total_net_pnl: float
    win_rate: Optional[float] = None
    expectancy: Optional[float] = None
    mean_return: Optional[float] = None
    edge_bps: Optional[float] = None
    symbols: Tuple[str, ...] = ()
    insufficient_data: bool = True
    last_updated: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return _jsonable(asdict(self))


# ---------------------------------------------------------------------------
# pure helpers — no I/O, no state, so they can be tested adversarially alone
# ---------------------------------------------------------------------------


def _finite(value: Any) -> Optional[float]:
    """``float(value)`` if it is a real, finite number; otherwise ``None``.

    Every number that enters the statistics passes through here.  That is what
    makes the adversarial cases (NaN, inf, ``None``, ``"abc"``, a list) boring:
    they are dropped at the boundary instead of poisoning a mean that a sizing
    decision then reads.
    """
    if isinstance(value, bool):  # bool is an int; a flag is not a measurement
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _mean(values: Sequence[float]) -> Optional[float]:
    return (sum(values) / len(values)) if values else None


def _median(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _clamp_multiplier(value: Any, floor: Any) -> float:
    """Force any input whatsoever into ``[floor, 1.0]``, with ``floor <= 1.0``.

    THE guarantee of this module, isolated into eight lines so it can be read in
    one sitting and hammered by tests on its own.  It is total: there is no
    input — NaN, inf, ``None``, a string, a negative, a configured floor above
    one — for which it returns more than ``1.0``.

    Note the order.  The floor is clamped to ``[0.0, 1.0]`` *first*, so a
    corrupted ``MEMORY_MIN_THROTTLE`` cannot become a boost by being returned as
    the "minimum".  Then the value is capped at a literal ``1.0``.
    """
    safe_floor = _finite(floor)
    if safe_floor is None:
        safe_floor = 0.0
    safe_floor = min(1.0, max(0.0, safe_floor))
    candidate = _finite(value)
    if candidate is None:
        # Unusable evidence throttles to the floor rather than to 1.0. This
        # direction is the safe one: the multiplier only ever shrinks, so the
        # conservative reading of "I cannot tell" is "take less".
        return safe_floor
    return min(1.0, max(safe_floor, candidate))


def _jsonable(value: Any) -> Any:
    """Recursively coerce to something ``json.dumps`` accepts without a default.

    Non-finite floats become ``None``.  ``json.dumps`` would otherwise emit the
    bare tokens ``NaN`` / ``Infinity``, which are not JSON; a health endpoint
    that returns them produces a parse error in whatever is monitoring it, which
    is indistinguishable from the bot being down.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    return str(value)


def _looks_secret(name: Any) -> bool:
    return bool(_SECRET_NAME_RE.search(str(name)))


# ---------------------------------------------------------------------------
# the memory layer
# ---------------------------------------------------------------------------


class TradingMemory:
    """Durable, shared experience.  One instance per store; holds no cache.

    Stateless on purpose, exactly like ``PerformanceAnalytics``: an in-memory
    shadow of the ledger is one more thing that can disagree with the ledger,
    and every read here is a bounded SQLite query behind a lock that is already
    held for far heavier work elsewhere.

    Construct it wherever a ``StateStore`` and a ``Config`` are already in
    hand::

        memory = TradingMemory(store, cfg)
        qty *= memory.size_multiplier("BTCUSDT")   # can only shrink
        payload["memory"] = memory.snapshot()      # health endpoint, read-only
    """

    def __init__(self, store: StateStore, config: Any) -> None:
        self.store = store
        self.cfg = config

    # -- configuration, defensively read -----------------------------------
    #
    # `Config` is a frozen dataclass with these fields, but this module is also
    # constructed in tests and backtests with stand-in config objects. Reading
    # through getattr with a documented default keeps a missing field from
    # turning into an AttributeError inside a health response.

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "MEMORY_ENABLED", True))

    @property
    def min_samples(self) -> int:
        raw = _finite(getattr(self.cfg, "MEMORY_MIN_SAMPLES", 20))
        # A floor below 1 would let a single trade define an "edge".
        return max(1, int(raw)) if raw is not None else 20

    @property
    def lookback(self) -> int:
        raw = _finite(getattr(self.cfg, "MEMORY_LOOKBACK_TRADES", 500))
        return max(1, int(raw)) if raw is not None else 500

    @property
    def min_throttle(self) -> float:
        """The configured floor, already clamped into ``[0.0, 1.0]``.

        Clamped *here* rather than at the point of use so that there is no path
        in this class on which an out-of-range configured floor is visible.
        """
        raw = _finite(getattr(self.cfg, "MEMORY_MIN_THROTTLE", 0.25))
        if raw is None:
            return 0.25
        return min(1.0, max(0.0, raw))

    # -- 1. per-symbol experience ------------------------------------------

    def symbol_experience(self, symbol: str) -> Optional[SymbolExperience]:
        """Fee-inclusive experience for one symbol, or ``None``.

        ``None`` means "fewer than ``MEMORY_MIN_SAMPLES`` usable trades", and it
        is returned rather than a zeroed record for the reason the whole
        analytics module exists: a caller that reads ``win_rate == 0.0`` will
        conclude the symbol loses, when the truth is that we do not yet know.

        "Usable" excludes any trade with a zero or non-finite notional or a
        non-finite PnL.  Those are dropped silently at this boundary rather than
        allowed to produce a NaN mean that a sizing decision would then read.
        """
        rows = self.store.trades_for_symbol(str(symbol), self.lookback)
        returns: List[float] = []
        nets: List[float] = []
        fees = 0.0
        mae: List[float] = []
        first = last = 0.0

        for row in rows:
            qty = _finite(row.get("qty"))
            entry = _finite(row.get("entry_price"))
            net = _finite(row.get("net_pnl"))
            if qty is None or entry is None or net is None:
                continue
            notional = abs(qty * entry)
            if notional <= 0.0:
                continue
            ret = net / notional
            if not math.isfinite(ret):
                continue
            returns.append(ret)
            nets.append(net)
            for key in ("entry_fee", "exit_fee"):
                fee = _finite(row.get(key))
                if fee is not None:
                    fees += fee
            excursion = self._mae_bps(row.get("meta"))
            if excursion is not None:
                mae.append(excursion)
            closed = _finite(row.get("closed_epoch")) or 0.0
            last = max(last, closed)
            first = closed if first == 0.0 else min(first, closed)

        if len(returns) < self.min_samples:
            return None

        wins = sum(1 for v in nets if v > 0.0)  # fee-aware, matching TradeRecord
        mean_return = sum(returns) / len(returns)
        return SymbolExperience(
            symbol=str(symbol),
            samples=len(returns),
            wins=wins,
            losses=len(nets) - wins,
            win_rate=wins / len(nets),
            expectancy=sum(nets) / len(nets),
            mean_return=mean_return,
            median_return=_median(returns) or 0.0,
            edge_bps=mean_return * 10_000.0,
            total_net_pnl=sum(nets),
            total_fees=fees,
            mean_mae_bps=_mean(mae),
            worst_mae_bps=max(mae) if mae else None,
            mae_samples=len(mae),
            first_epoch=first,
            last_updated=last,
        )

    @staticmethod
    def _mae_bps(meta: Any) -> Optional[float]:
        """Max adverse excursion in bps from a trade's ``meta`` blob, if present.

        Convention, in priority order: ``mae_bps`` (already bps),
        ``max_adverse_excursion`` / ``mae`` (a FRACTION, per the house rule, so
        multiplied by 10,000 here).  Absent or unparseable means absent — this
        returns ``None`` and the statistic simply is not reported, because "if
        available" must never become "assumed zero", which would understate
        every drawdown the bot has actually lived through.
        """
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (TypeError, ValueError):
                return None
        if not isinstance(meta, Mapping):
            return None
        direct = _finite(meta.get("mae_bps"))
        if direct is not None:
            return abs(direct)
        for key in ("max_adverse_excursion", "mae"):
            fraction = _finite(meta.get(key))
            if fraction is not None:
                return abs(fraction) * 10_000.0
        return None

    def all_symbol_experience(self) -> Dict[str, SymbolExperience]:
        """Every symbol that clears the sample floor.  Symbols below it are
        absent from the mapping rather than present with empty values."""
        out: Dict[str, SymbolExperience] = {}
        for symbol in self.store.traded_symbols():
            experience = self.symbol_experience(symbol)
            if experience is not None:
                out[symbol] = experience
        return out

    def symbols_below_floor(self) -> Dict[str, int]:
        """Symbols that have traded but cannot yet be judged, and their counts.

        Reported separately and explicitly so the operator can see the
        difference between "no edge measured" and "not measured yet" — which is
        the distinction the whole module is built around.
        """
        out: Dict[str, int] = {}
        for symbol in self.store.traded_symbols():
            if self.symbol_experience(symbol) is None:
                out[symbol] = len(self.store.trades_for_symbol(symbol, self.lookback))
        return out

    # -- 2. execution quality ----------------------------------------------

    def record_execution(
        self,
        symbol: str,
        side: str,
        intended_price: float,
        fill_price: float = 0.0,
        qty: float = 0.0,
        order_link_id: str = "",
        order_type: str = "",
        purpose: str = "entry",
        outcome: str = "filled",
        reason: str = "",
        is_maker: bool = False,
        requested_epoch: Optional[float] = None,
        filled_epoch: Optional[float] = None,
        meta: Optional[Mapping[str, Any]] = None,
    ) -> int:
        """Record one execution.  A **write**: single-writer rules apply.

        Delegates to :meth:`StateStore.record_execution` unchanged rather than
        reimplementing the slippage sign, so there is exactly one definition of
        which direction costs money.
        """
        return self.store.record_execution(
            symbol=symbol, side=side, intended_price=intended_price,
            fill_price=fill_price, qty=qty, order_link_id=order_link_id,
            order_type=order_type, purpose=purpose, outcome=outcome,
            reason=reason, is_maker=is_maker, requested_epoch=requested_epoch,
            filled_epoch=filled_epoch, meta=meta,
        )

    def execution_quality(
        self, symbol: Optional[str] = None, limit: Optional[int] = None
    ) -> Optional[ExecutionQualityStats]:
        """Realised execution cost, or ``None`` when nothing has been recorded.

        ``None`` for "no records at all"; a populated object with ``None``
        metrics where a particular sub-sample is empty.  The distinction lets
        the health endpoint say "we have 40 executions and none of them filled"
        rather than reporting a maker fill rate of zero, which reads as a
        working system with bad luck.
        """
        cap = self.lookback if limit is None else int(limit)
        rows = self.store.recent_executions(limit=cap, symbol=symbol)
        if not rows:
            return None

        slips: List[float] = []
        fill_times: List[float] = []
        fills = partials = rejects = cancels = unfilled = maker_fills = 0
        reject_reasons: Dict[str, int] = {}
        cancel_reasons: Dict[str, int] = {}
        last = 0.0

        for row in rows:
            outcome = str(row.get("outcome") or "").lower()
            reason = str(row.get("reason") or "") or "unspecified"
            if outcome in ("filled", "partial"):
                if outcome == "filled":
                    fills += 1
                else:
                    partials += 1
                if int(row.get("is_maker") or 0):
                    maker_fills += 1
                slip = _finite(row.get("slippage_bps"))
                if slip is not None:
                    slips.append(slip)
                ttf = _finite(row.get("time_to_fill_s"))
                if ttf is not None and ttf >= 0.0:
                    fill_times.append(ttf)
            elif outcome == "rejected":
                rejects += 1
                reject_reasons[reason] = reject_reasons.get(reason, 0) + 1
            elif outcome == "cancelled":
                cancels += 1
                cancel_reasons[reason] = cancel_reasons.get(reason, 0) + 1
            else:
                unfilled += 1
            ts = _finite(row.get("ts_epoch"))
            if ts is not None:
                last = max(last, ts)

        filled_total = fills + partials
        return ExecutionQualityStats(
            scope=str(symbol) if symbol else "*",
            samples=len(rows),
            fills=fills,
            partials=partials,
            rejects=rejects,
            cancels=cancels,
            unfilled=unfilled,
            mean_slippage_bps=_mean(slips),
            median_slippage_bps=_median(slips),
            worst_slippage_bps=max(slips) if slips else None,
            slippage_samples=len(slips),
            maker_fill_rate=(maker_fills / filled_total) if filled_total else None,
            mean_time_to_fill_s=_mean(fill_times),
            fill_time_samples=len(fill_times),
            reject_reasons=dict(sorted(reject_reasons.items())),
            cancel_reasons=dict(sorted(cancel_reasons.items())),
            last_updated=last,
        )

    def cost_model_error_bps(self, symbol: Optional[str] = None) -> Optional[float]:
        """Measured slippage minus the slippage the cost model assumes, in bps.

        Positive means the bot is being charged more than ``MIN_EDGE_BPS`` was
        built on — i.e. every edge calculation in the stack is optimistic by
        this much.  ``None`` below :data:`MIN_EXECUTION_SAMPLES`, because the
        operator would act on this number and a two-sample answer is not a
        number to act on.

        Reporting only.  Nothing in this module feeds it back into
        ``MIN_EDGE_BPS`` — a bot that relaxes its own cost floor because trading
        turned out to be expensive has the sign backwards, and adjusting a risk
        parameter automatically is exactly what this module must not do.
        """
        stats = self.execution_quality(symbol)
        if stats is None or stats.slippage_samples < MIN_EXECUTION_SAMPLES:
            return None
        measured = stats.mean_slippage_bps
        assumed = _finite(getattr(self.cfg, "SLIPPAGE_BPS", None))
        if measured is None or assumed is None:
            return None
        return measured - assumed

    # -- 3. regime memory ---------------------------------------------------

    def record_regime(
        self,
        symbol: str,
        regime: str,
        ts_epoch: Optional[float] = None,
        detail: Optional[Mapping[str, Any]] = None,
    ) -> int:
        """Observe a regime for a symbol.  A **write**: single-writer rules apply."""
        return self.store.record_regime(
            symbol=symbol, regime=regime, ts_epoch=ts_epoch, detail=detail
        )

    def current_regime(self, symbol: str) -> Optional[str]:
        """The last regime observed for ``symbol``, or ``None`` if never observed."""
        return self.store.current_regime(str(symbol))

    def regime_stats(self, limit: Optional[int] = None) -> Dict[str, RegimeStats]:
        """How trades opened under each regime actually turned out.

        Answers "how has this strategy actually done in this regime before".
        Trades opened before any regime observation for their symbol are grouped
        under ``"unlabelled"`` rather than dropped: an operator who sees 200
        trades in memory and 40 across the regimes needs the other 160 accounted
        for, not silently discarded.
        """
        cap = self.lookback if limit is None else int(limit)
        buckets: Dict[str, Dict[str, Any]] = {}

        for row in self.store.trades_with_regime(limit=cap):
            label = str(row.get("regime") or "unlabelled")
            qty = _finite(row.get("qty"))
            entry = _finite(row.get("entry_price"))
            net = _finite(row.get("net_pnl"))
            if qty is None or entry is None or net is None:
                continue
            notional = abs(qty * entry)
            if notional <= 0.0:
                continue
            ret = net / notional
            if not math.isfinite(ret):
                continue
            bucket = buckets.setdefault(
                label, {"nets": [], "returns": [], "symbols": set(), "last": 0.0}
            )
            bucket["nets"].append(net)
            bucket["returns"].append(ret)
            bucket["symbols"].add(str(row.get("symbol") or ""))
            closed = _finite(row.get("closed_epoch")) or 0.0
            bucket["last"] = max(bucket["last"], closed)

        out: Dict[str, RegimeStats] = {}
        for label, bucket in buckets.items():
            nets: List[float] = bucket["nets"]
            returns: List[float] = bucket["returns"]
            wins = sum(1 for v in nets if v > 0.0)
            enough = len(nets) >= self.min_samples
            mean_return = _mean(returns) if enough else None
            out[label] = RegimeStats(
                regime=label,
                samples=len(nets),
                wins=wins,
                losses=len(nets) - wins,
                total_net_pnl=sum(nets),
                win_rate=(wins / len(nets)) if enough else None,
                expectancy=(sum(nets) / len(nets)) if enough else None,
                mean_return=mean_return,
                edge_bps=(mean_return * 10_000.0) if mean_return is not None else None,
                symbols=tuple(sorted(s for s in bucket["symbols"] if s)),
                insufficient_data=not enough,
                last_updated=bucket["last"],
            )
        return out

    # -- 4. block-reason memory --------------------------------------------

    def block_reasons(self, symbol: Optional[str] = None) -> Dict[str, int]:
        """Counts of every reason a gate refused a trade, highest first."""
        counts: Dict[str, int] = {}
        for row in self.store.decision_reason_counts(
            decisions=BLOCKING_DECISIONS, symbol=symbol
        ):
            reason = str(row.get("reason") or "") or "unspecified"
            counts[reason] = counts.get(reason, 0) + int(row.get("n") or 0)
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def block_reasons_by_symbol(self) -> Dict[str, Dict[str, int]]:
        """The same counts, split by symbol — which is how a *single* bad
        instrument is distinguished from a gate that is too tight everywhere."""
        out: Dict[str, Dict[str, int]] = {}
        for row in self.store.decision_reason_counts(decisions=BLOCKING_DECISIONS):
            symbol = str(row.get("symbol") or "") or "unknown"
            reason = str(row.get("reason") or "") or "unspecified"
            bucket = out.setdefault(symbol, {})
            bucket[reason] = bucket.get(reason, 0) + int(row.get("n") or 0)
        return {
            symbol: dict(sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0])))
            for symbol, reasons in sorted(out.items())
        }

    def top_block_reasons(self, n: int = 10) -> List[Tuple[str, int]]:
        """The ``n`` most common block reasons as ``(reason, count)`` pairs.

        Ties break alphabetically so the operator's dashboard does not reorder
        itself between refreshes for no reason.  ``n <= 0`` returns ``[]``.
        """
        if int(n) <= 0:
            return []
        return list(self.block_reasons().items())[: int(n)]

    def total_blocks(self) -> int:
        return sum(self.block_reasons().values())

    # -- 5. the size multiplier — shrink only ------------------------------

    def size_multiplier(self, symbol: str) -> float:
        """A sizing multiplier in ``[MEMORY_MIN_THROTTLE, 1.0]``.  **Never above 1.0.**

        This is the only number in this module that touches a trading decision,
        and it is an input to *sizing only*.  It does not and must not influence
        a risk limit, a gate outcome, the circuit breaker, or the kill switch.
        Multiply a computed quantity by it; never add it to a budget.

        Returns exactly ``1.0`` — "no opinion" — when memory is disabled, when
        the symbol has fewer than ``MEMORY_MIN_SAMPLES`` trades, or when the
        recorded experience is not adverse.  Positive experience earns **no**
        boost: "it worked last time" is not additional risk budget, and a
        mechanism that grows size on a winning streak is a martingale with a
        statistics vocabulary.

        The single ``return`` below is the clamp.  That is deliberate and is
        asserted structurally by ``tests/test_memory.py``: it makes exceeding
        1.0 impossible to introduce by editing the reasoning above it.
        """
        return _clamp_multiplier(self._raw_size_multiplier(symbol), self.min_throttle)

    def _raw_size_multiplier(self, symbol: str) -> float:
        """The unclamped opinion.  Every branch produces a factor of at most 1.0,
        but nothing depends on that being true — :func:`_clamp_multiplier` does
        not trust this function, and no future edit to it can widen the range."""
        if not self.enabled:
            return 1.0
        try:
            experience = self.symbol_experience(symbol)
        except PersistenceError:
            # An unreadable ledger is not evidence of a good symbol. The
            # conservative direction for a shrink-only knob is "shrink"; the
            # risk gates, which fail closed, will normally have blocked the
            # trade outright before sizing is ever reached.
            logger.warning("memory unreadable for %s; throttling to floor", symbol)
            return 0.0
        if experience is None:
            return 1.0

        factor = 1.0

        # (a) Realised, fee-inclusive edge. Only the adverse side is acted on.
        edge = _finite(experience.edge_bps)
        if edge is not None and edge < 0.0:
            factor *= max(0.0, 1.0 + edge / ADVERSE_EDGE_REFERENCE_BPS)

        # (b) Execution cost against the assumed cost. If the bot is really
        # paying twice the modelled slippage, every size derived from the
        # modelled figure is too large by roughly that ratio.
        try:
            quality = self.execution_quality(symbol)
        except PersistenceError:
            logger.warning("execution memory unreadable for %s", symbol)
            quality = None
        if quality is not None and quality.slippage_samples >= MIN_EXECUTION_SAMPLES:
            measured = _finite(quality.mean_slippage_bps)
            assumed = _finite(getattr(self.cfg, "SLIPPAGE_BPS", None))
            if (
                measured is not None
                and assumed is not None
                and assumed > 0.0
                and measured > assumed
            ):
                factor *= assumed / measured

        return factor

    def size_multipliers(self) -> Dict[str, float]:
        """The multiplier for every symbol the ledger knows about."""
        return {s: self.size_multiplier(s) for s in self.store.traded_symbols()}

    # -- 6. free-form namespaced memory ------------------------------------

    def remember(self, namespace: str, key: str, value: Any) -> None:
        """Store a fact that does not deserve its own table.  A **write**.

        Convention (``persistence.MEMORY_KEY_CONVENTION``)::

            <module>/<snake_case_noun>[:<SYMBOL>] -> JSON value

        Namespaces beginning with ``"_"`` are reserved for this module.

        Keys or namespaces that look like credentials are **refused**, not
        redacted.  Redaction at read time is a backstop; refusing the write is
        what keeps a secret from reaching disk at all, and the store outlives
        the process that wrote it.
        """
        if _looks_secret(namespace) or _looks_secret(key):
            raise MemoryRefused(
                f"refusing to persist {namespace}/{key}: the name looks like a "
                "credential. Memory is dumped into health responses and offline "
                "learning sessions; secrets belong in the environment, which "
                "only config.py reads."
            )
        if str(namespace).startswith("_"):
            raise MemoryRefused(
                f"namespace {namespace!r} is reserved for the memory layer"
            )
        self.store.memory_put(namespace, key, value)

    def recall(self, namespace: str, key: str, default: Any = None) -> Any:
        """Read a remembered fact.  A read — no writer role required."""
        return self.store.memory_get(namespace, key, default)

    def recall_namespace(self, namespace: str) -> Dict[str, Any]:
        return self.store.memory_namespace(namespace)

    def forget(self, namespace: str, key: str) -> bool:
        """Delete one remembered fact.  A **write**.  True if it existed."""
        return self.store.memory_forget(namespace, key)

    # -- 7. the snapshot ----------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """Everything remembered, as JSON-serialisable, credential-free data.

        Two consumers, one shape: the health endpoint (which must never raise,
        so each section is guarded independently and failures are *reported*
        under ``"errors"`` rather than swallowed) and an offline learning
        session on runpod.io (which needs the whole picture, not a summary).

        Every value passes through :func:`_jsonable` (so ``json.dumps`` cannot
        fail or emit non-JSON ``NaN``) and :meth:`_scrub` (so nothing that looks
        like a credential, and nothing that *equals* a configured credential,
        can leave the process).
        """
        errors: List[str] = []

        def section(name: str, producer: Any, fallback: Any) -> Any:
            try:
                return producer()
            except Exception as exc:  # noqa: BLE001 - a health endpoint may not raise
                logger.warning("memory snapshot section %s failed", name, exc_info=True)
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                return fallback

        experience = section("symbols", self.all_symbol_experience, {})
        payload: Dict[str, Any] = {
            "schema": SNAPSHOT_SCHEMA,
            "generated_epoch": utc_now_epoch(),
            "enabled": self.enabled,
            "thresholds": {
                "min_samples": self.min_samples,
                "lookback_trades": self.lookback,
                "min_throttle": self.min_throttle,
                "adverse_edge_reference_bps": ADVERSE_EDGE_REFERENCE_BPS,
                "min_execution_samples": MIN_EXECUTION_SAMPLES,
            },
            "symbols": {s: e.as_dict() for s, e in experience.items()},
            "symbols_below_sample_floor": section(
                "symbols_below_sample_floor", self.symbols_below_floor, {}
            ),
            "size_multipliers": section("size_multipliers", self.size_multipliers, {}),
            "execution_quality": section(
                "execution_quality",
                lambda: {
                    "overall": (
                        stats.as_dict()
                        if (stats := self.execution_quality()) is not None
                        else None
                    ),
                    "by_symbol": {
                        symbol: per.as_dict()
                        for symbol in self.store.traded_symbols()
                        if (per := self.execution_quality(symbol)) is not None
                    },
                },
                {"overall": None, "by_symbol": {}},
            ),
            "cost_model_error_bps": section(
                "cost_model_error_bps", self.cost_model_error_bps, None
            ),
            "regimes": section(
                "regimes",
                lambda: {k: v.as_dict() for k, v in self.regime_stats().items()},
                {},
            ),
            "current_regime": section(
                "current_regime",
                lambda: {
                    symbol: self.current_regime(symbol)
                    for symbol in self.store.traded_symbols()
                },
                {},
            ),
            "blocks": section(
                "blocks",
                lambda: {
                    "total": self.total_blocks(),
                    "top": [list(pair) for pair in self.top_block_reasons(20)],
                    "by_symbol": self.block_reasons_by_symbol(),
                },
                {"total": 0, "top": [], "by_symbol": {}},
            ),
            "kv": section("kv", self._kv_dump, {}),
            "errors": errors,
        }
        return self._scrub(_jsonable(payload))

    def _kv_dump(self) -> Dict[str, Dict[str, Any]]:
        return {ns: self.store.memory_namespace(ns) for ns in self.store.memory_namespaces()}

    def _configured_secrets(self) -> Tuple[str, ...]:
        """Credential *values* currently configured, so a snapshot can be checked
        against them rather than only against suspicious field names.

        Belt and braces: :meth:`remember` already refuses secret-looking names,
        but a secret stored under an innocent key would slip past that.  This
        catches it by value.
        """
        found: List[str] = []
        for name in dir(self.cfg):
            if name.startswith("_") or not _looks_secret(name):
                continue
            try:
                value = getattr(self.cfg, name)
            except Exception:  # noqa: BLE001 - a property may raise; skip it
                continue
            if isinstance(value, str) and len(value) >= _MIN_SECRET_LENGTH:
                found.append(value)
        return tuple(found)

    def _scrub(self, value: Any, _secrets: Optional[Tuple[str, ...]] = None) -> Any:
        """Redact anything whose key looks like a credential, or whose value is one."""
        secrets = self._configured_secrets() if _secrets is None else _secrets
        if isinstance(value, Mapping):
            out: Dict[str, Any] = {}
            for key, item in value.items():
                if _looks_secret(key):
                    out[str(key)] = REDACTED
                else:
                    out[str(key)] = self._scrub(item, secrets)
            return out
        if isinstance(value, list):
            return [self._scrub(item, secrets) for item in value]
        if isinstance(value, str):
            for secret in secrets:
                if secret and secret in value:
                    return REDACTED
        return value
