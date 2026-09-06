"""performance_analytics.py — metrics computed from persisted, fee-inclusive trades.

WHAT REPLACED WHAT
==================
The as-received module (1,693 lines, retained at
``_dead/performance_analytics_legacy.py``) produced numbers that were wrong in
ways that mattered downstream, because the Kelly sizer consumed them:

1. **It deadlocked.** ``CompoundingMetrics.get_summary()`` acquired a
   non-reentrant lock it already held, so the *first* summary call hung the
   trade-recording path permanently. Fixed here by holding no locks at all —
   every read goes to SQLite, which does its own locking.
2. **Sharpe annualised per-trade returns by x365.** That is meaningless: it
   treats "one trade" as "one day". Sharpe is computed here on **daily
   aggregated** returns and annualised by ``sqrt(365)``, and returns ``None``
   when there is not enough history rather than a confident-looking number.
3. **Win rate counted gross PnL.** Fee-blind, so it overstated the edge — and it
   was the input to Kelly. Every metric here is net of fees by construction,
   because ``StateStore`` records ``entry_fee`` and ``exit_fee`` per trade.
4. **A "validation report" was generated from ``random.Random(42)``** with
   hardcoded pass numbers. Deleted outright; fabricated evidence is worse than
   no evidence.
5. **``EnhancedAWSIntegration`` did not exist**, yet three modules imported it
   for CloudWatch alerting — so all production alerting was silently disabled.
   The name is gone; there is no alerting rather than fake alerting.

EVERY METRIC RETURNS ``None`` WHEN IT CANNOT BE COMPUTED HONESTLY.
That is the whole design. A Sharpe ratio from four trades is not a small
Sharpe ratio, it is noise with a decimal point.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from persistence import StateStore, utc_day

logger = logging.getLogger("analytics")

__all__ = [
    "PerformanceReport",
    "PerformanceAnalytics",
    "get_global_metrics",
    "get_perf",
    "MIN_TRADES_FOR_RATIOS",
    "TRADING_DAYS_PER_YEAR",
]

#: Crypto trades every day, so the annualisation factor is 365, not 252.
TRADING_DAYS_PER_YEAR = 365

#: Below this, ratio-style metrics return None instead of a number.
MIN_TRADES_FOR_RATIOS = 30


@dataclass(frozen=True)
class PerformanceReport:
    """Measured performance. ``None`` means "not enough data", never zero.

    The distinction matters: a caller that treats ``None`` as ``0.0`` will
    conclude the strategy has no edge, whereas the truth is that we do not yet
    know. Kelly reads this distinction.
    """

    trades: int = 0
    wins: int = 0
    losses: int = 0
    gross_pnl: float = 0.0
    fees: float = 0.0
    net_pnl: float = 0.0
    win_rate: Optional[float] = None
    avg_win: Optional[float] = None
    avg_loss: Optional[float] = None
    profit_factor: Optional[float] = None
    expectancy: Optional[float] = None
    sharpe: Optional[float] = None
    sortino: Optional[float] = None
    max_drawdown: Optional[float] = None
    days: int = 0
    insufficient_data: bool = True
    notes: Tuple[str, ...] = ()

    def summary(self) -> str:
        """One-line human summary that never overstates what is known."""
        if self.insufficient_data:
            return (
                f"{self.trades} trades — insufficient for ratio metrics "
                f"(need {MIN_TRADES_FOR_RATIOS}); net PnL {self.net_pnl:+.2f} "
                f"after {self.fees:.2f} in fees"
            )
        return (
            f"{self.trades} trades over {self.days}d | "
            f"win rate {self.win_rate:.1%} (fee-aware) | "
            f"net {self.net_pnl:+.2f} after {self.fees:.2f} fees | "
            f"PF {self.profit_factor:.2f} | "
            f"Sharpe {self.sharpe:.2f}" if self.sharpe is not None else
            f"{self.trades} trades over {self.days}d | "
            f"win rate {self.win_rate:.1%} | net {self.net_pnl:+.2f}"
        )


class PerformanceAnalytics:
    """Reads persisted trades and computes metrics. Holds no state of its own.

    Stateless by design: the legacy class kept an in-memory shadow of the trade
    history behind a lock, which is what made it deadlock and what let it drift
    from the durable record.
    """

    def __init__(self, store: StateStore) -> None:
        self.store = store

    # -- primitives --------------------------------------------------------

    def _trades(self, limit: int = 10_000) -> List[Dict[str, Any]]:
        return self.store.recent_trades(limit)

    @staticmethod
    def _daily_returns(trades: Sequence[Mapping[str, Any]]) -> Dict[str, float]:
        """Net PnL aggregated by UTC day.

        Aggregating to days is what makes an annualised Sharpe meaningful. The
        legacy code annualised *per-trade* returns by x365, which produces a
        number that scales with trading frequency rather than with risk-adjusted
        return.
        """
        by_day: Dict[str, float] = defaultdict(float)
        for trade in trades:
            day = str(trade.get("day") or utc_day(float(trade.get("closed_epoch", 0))))
            by_day[day] += float(trade.get("net_pnl", 0.0))
        return dict(by_day)

    @staticmethod
    def _stdev(values: Sequence[float]) -> Optional[float]:
        """Sample standard deviation (ddof=1). ``None`` for fewer than 2 points."""
        n = len(values)
        if n < 2:
            return None
        mean = sum(values) / n
        variance = sum((x - mean) ** 2 for x in values) / (n - 1)
        return math.sqrt(variance)

    # -- the report --------------------------------------------------------

    def report(self, starting_equity: Optional[float] = None) -> PerformanceReport:
        """Compute everything that can be computed honestly from the record."""
        trades = self._trades()
        notes: List[str] = []

        if not trades:
            return PerformanceReport(notes=("no closed trades recorded",))

        gross = sum(float(t.get("gross_pnl", 0.0)) for t in trades)
        fees = sum(
            float(t.get("entry_fee", 0.0)) + float(t.get("exit_fee", 0.0))
            for t in trades
        )
        net_values = [float(t.get("net_pnl", 0.0)) for t in trades]
        net = sum(net_values)

        # Fee-aware: a trade that made money gross but lost after fees is a LOSS.
        wins = [v for v in net_values if v > 0.0]
        losses = [v for v in net_values if v <= 0.0]
        n = len(trades)

        by_day = self._daily_returns(trades)
        days = len(by_day)

        report_kwargs: Dict[str, Any] = dict(
            trades=n, wins=len(wins), losses=len(losses),
            gross_pnl=gross, fees=fees, net_pnl=net, days=days,
        )

        if n < MIN_TRADES_FOR_RATIOS:
            notes.append(
                f"only {n} trades; ratio metrics need {MIN_TRADES_FOR_RATIOS} "
                "and are reported as None rather than as a number"
            )
            return PerformanceReport(
                insufficient_data=True, notes=tuple(notes), **report_kwargs
            )

        win_rate = len(wins) / n
        avg_win = (sum(wins) / len(wins)) if wins else None
        avg_loss = (abs(sum(losses)) / len(losses)) if losses else None

        gross_wins = sum(wins)
        gross_losses = abs(sum(losses))
        if gross_losses > 0:
            profit_factor = gross_wins / gross_losses
        elif gross_wins > 0:
            profit_factor = None
            notes.append("no losing trades yet; profit factor is undefined, not infinite")
        else:
            profit_factor = None

        expectancy = net / n

        sharpe = sortino = None
        if starting_equity and starting_equity > 0 and days >= 2:
            daily_returns = [v / starting_equity for v in by_day.values()]
            deviation = self._stdev(daily_returns)
            mean_daily = sum(daily_returns) / len(daily_returns)
            if deviation and deviation > 0:
                sharpe = (mean_daily / deviation) * math.sqrt(TRADING_DAYS_PER_YEAR)
            else:
                notes.append("daily return volatility is zero; Sharpe is undefined")
            downside = [r for r in daily_returns if r < 0.0]
            downside_dev = self._stdev(downside) if len(downside) >= 2 else None
            if downside_dev and downside_dev > 0:
                sortino = (mean_daily / downside_dev) * math.sqrt(TRADING_DAYS_PER_YEAR)
            elif not downside:
                notes.append("no losing days yet; Sortino is undefined, not infinite")
        elif days < 2:
            notes.append("fewer than 2 distinct trading days; Sharpe needs a series")
        else:
            notes.append("no starting equity supplied; Sharpe/Sortino not computed")

        max_drawdown = self._max_drawdown_from_equity_curve(trades, starting_equity)

        return PerformanceReport(
            win_rate=win_rate, avg_win=avg_win, avg_loss=avg_loss,
            profit_factor=profit_factor, expectancy=expectancy,
            sharpe=sharpe, sortino=sortino, max_drawdown=max_drawdown,
            insufficient_data=False, notes=tuple(notes), **report_kwargs
        )

    @staticmethod
    def _max_drawdown_from_equity_curve(
        trades: Sequence[Mapping[str, Any]], starting_equity: Optional[float]
    ) -> Optional[float]:
        """Peak-to-trough drawdown of the realised equity curve, as a FRACTION."""
        if not starting_equity or starting_equity <= 0:
            return None
        ordered = sorted(trades, key=lambda t: float(t.get("closed_epoch", 0.0)))
        equity = float(starting_equity)
        peak = equity
        worst = 0.0
        for trade in ordered:
            equity += float(trade.get("net_pnl", 0.0))
            peak = max(peak, equity)
            if peak > 0:
                worst = max(worst, (peak - equity) / peak)
        return worst

    # -- edge decay --------------------------------------------------------

    def edge_decay(self, window: int = 30) -> Optional[float]:
        """Ratio of recent expectancy to lifetime expectancy, or ``None``.

        Below 1.0 the recent edge is worse than the historical one. Intended as
        a *throttle* input — it can only reduce size, never increase it.
        """
        trades = self._trades()
        if len(trades) < max(window * 2, MIN_TRADES_FOR_RATIOS):
            return None
        recent = [float(t["net_pnl"]) for t in trades[:window]]
        overall = [float(t["net_pnl"]) for t in trades]
        recent_expectancy = sum(recent) / len(recent)
        overall_expectancy = sum(overall) / len(overall)
        if overall_expectancy <= 0:
            return 0.0
        return max(0.0, recent_expectancy / overall_expectancy)

    def size_throttle(self, window: int = 30) -> float:
        """Multiplier in [0, 1] for position size, from edge decay.

        Returns 1.0 (no throttle) when there is not enough data to judge —
        the base risk budget already applies, and inventing a throttle from
        thin data would be as arbitrary as inventing a boost.
        """
        decay = self.edge_decay(window)
        if decay is None:
            return 1.0
        return max(0.0, min(1.0, decay))


# ---------------------------------------------------------------------------
# module-level accessors other modules import
# ---------------------------------------------------------------------------


def get_global_metrics(store: StateStore) -> PerformanceReport:
    """Metrics for the persisted account state."""
    return PerformanceAnalytics(store).report()


def get_perf(store: StateStore, starting_equity: Optional[float] = None) -> PerformanceReport:
    return PerformanceAnalytics(store).report(starting_equity)
