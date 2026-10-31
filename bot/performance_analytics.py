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
    "overlay_opportunity_cost",
    "annualise_simple",
    "max_drawdown_usd",
    "downside_deviation",
    "profit_concentration",
    "expectancy",
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
    #: Trades whose BOTH fee legs are venue-confirmed vs the rest (estimated,
    #: caller-supplied, unknown, or legacy rows with no provenance). ``net_pnl``
    #: above counts every trade; it is realised net economics only when
    #: ``fees_confirmed`` is True.
    fee_confirmed_trades: int = 0
    fee_unconfirmed_trades: int = 0
    #: Exits still without an authoritative price. Their positions are NOT in
    #: any figure above; a statement built on this report must say so.
    unpriced_exits: int = 0
    unpriced_stuck: int = 0

    @property
    def fees_confirmed(self) -> bool:
        return self.trades > 0 and self.fee_unconfirmed_trades == 0

    def summary(self) -> str:
        """One-line human summary that never overstates what is known."""
        text = self._summary_core()
        if self.trades and not self.fees_confirmed:
            text += (f" [PROVISIONAL: {self.fee_unconfirmed_trades} of "
                     f"{self.trades} trades carry unconfirmed fees]")
        if self.unpriced_exits:
            text += (f" [UNPRICED: {self.unpriced_exits} exit(s) excluded"
                     + (f", {self.unpriced_stuck} STUCK past review"
                        if self.unpriced_stuck else "") + "]")
        return text

    def _summary_core(self) -> str:
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
            pieces = trade.get("pieces")
            if pieces:
                # a position that left over several days: each exit on its own
                # day (never everything on the day of the last one)
                for piece in pieces:
                    by_day[str(piece["day"])] += float(piece["net_pnl"])
                continue
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
        try:
            un = self.store.unpriced_exit_status()
        except Exception:  # noqa: BLE001
            un = {"count": 0, "stuck": []}
        if un["count"]:
            notes.append(
                f"{un['count']} exit(s) have no authoritative price and are "
                "excluded from every figure here, along with their positions"
                + (f"; {len(un['stuck'])} are stuck past the review window"
                   if un["stuck"] else ""))
        try:
            fu = self.store.funding_unbooked_summary()
        except Exception:  # noqa: BLE001
            fu = {"count": 0, "statuses": {}}
        if fu["count"]:
            notes.append(
                f"{fu['count']} closed linear trade(s) have NO funding booked "
                f"({fu['statuses']}); their net P&L excludes funding the venue "
                "may have paid or charged")
        try:
            partial = self.store.partial_open_lifecycles()
        except Exception:  # noqa: BLE001
            partial = []
        if partial:
            notes.append(
                f"{len(partial)} position(s) are still open after booking an "
                "exit; they are not results yet and their realised pieces are "
                "excluded from every figure here")
        if any(t.get("spans_days") for t in trades) or any(
                (t.get("meta") or "").find('"funding_booked": true') >= 0
                or (t.get("meta") or "").find('"funding_booked":true') >= 0
                for t in trades):
            notes.append(
                "daily figures (Sharpe, Sortino, daily returns) are the day "
                "each EXIT was booked: funding earned over several days is "
                "booked on the exit's day, so they describe booking days, not "
                "earning days, and are not evidence for sizing or expectancy")
        unpriced = dict(unpriced_exits=int(un["count"]),
                        unpriced_stuck=len(un["stuck"]))

        if not trades:
            return PerformanceReport(
                notes=tuple(["no closed trades recorded"] + notes), **unpriced)

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

        confirmed = sum(
            1 for t in trades
            if t.get("entry_fee_realized") and t.get("exit_fee_realized"))
        if confirmed < n:
            notes.append(
                f"{n - confirmed} of {n} trades have fees that are estimated, "
                "caller-supplied, unknown or unlabelled; net PnL and "
                "expectancy are PROVISIONAL, not realised")
        report_kwargs: Dict[str, Any] = dict(
            trades=n, wins=len(wins), losses=len(losses),
            gross_pnl=gross, fees=fees, net_pnl=net, days=days,
            fee_confirmed_trades=confirmed,
            fee_unconfirmed_trades=n - confirmed, **unpriced,
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
# incremental economics of one overlay cycle (opportunity cost)
# ---------------------------------------------------------------------------

#: Every figure below is over the cycle's own holding period (first perp open
#: to last perp close), simple, not annualised and not compounded.
OPPORTUNITY_TIME_BASIS = ("the cycle's holding period (first perp open to last "
                          "perp close), simple interest, not annualised, not "
                          "compounded")

#: The counterfactual BTC leg is valued at the cycle's own perp fill prices: the
#: journal records no spot mark at those instants. The spot price differed by
#: the basis, which is therefore NOT in the counterfactual.
UNHEDGED_PRICE_BASIS = ("the cycle's volume-weighted perp entry and exit fill "
                        "prices; no spot mark is journalled at those instants, "
                        "so the perp-spot basis is not in this counterfactual")


def _finite(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def overlay_opportunity_cost(cycle: Mapping[str, Any], *,
                             benchmark_apr_pct: Optional[float],
                             benchmark_source: str = "") -> Dict[str, Any]:
    """What one closed hedged cycle earned against two alternatives.

    (a) the same BTC held UNHEDGED over the same period, and (b) the capital
    the hedged pair committed held at a cash benchmark rate the OPERATOR
    supplies. No benchmark is ever invented or defaulted: without one, (b) is
    UNAVAILABLE with the reason. Every figure names its denominator and time
    basis. The figures are counterfactuals computed from the cycle's booked
    prices, so the comparison is HISTORICAL_SIMULATION evidence; the overlay's
    own result keeps the evidence class the caller assigned it
    (``cycle["evidence_class"]``) and is never upgraded here.

    ``cycle`` keys read: cycle_id, outcome, evidence_class, overlay,
    hedged_qty, perp_entry_price, perp_exit_price, hold_days, net_usd,
    revalue_usd, capital_committed_usd, fees_complete, funding_complete.
    """
    cid = cycle.get("cycle_id")
    leg_class = str(cycle.get("evidence_class") or "UNAVAILABLE")
    out: Dict[str, Any] = {"cycle_id": cid,
                           "overlay_leg_evidence_class": leg_class,
                           "time_basis": OPPORTUNITY_TIME_BASIS}
    if cycle.get("outcome") != "CLOSED":
        out.update(evidence_class="UNAVAILABLE",
                   why=f"cycle outcome {cycle.get('outcome')!r}: only a "
                       "closed hedged cycle has a holding period to compare")
        return out
    days = _finite(cycle.get("hold_days"))
    net = _finite(cycle.get("net_usd"))
    reval = _finite(cycle.get("revalue_usd")) or 0.0
    incomplete = [name for name, ok in (
        ("fees", cycle.get("fees_complete")),
        ("funding", cycle.get("funding_complete"))) if not ok]
    if days is None or days <= 0 or net is None:
        out.update(evidence_class="UNAVAILABLE",
                   why="no positive holding period or no booked net for this "
                       "cycle")
        return out
    # The overlay's increment: what the hedge (short perp, its funding and its
    # costs) added, with any inventory revaluation taken back out so the BTC
    # leg is not counted twice.
    increment = net - reval
    out["overlay_increment_usd"] = increment
    out["overlay_increment_basis"] = (
        "booked cycle net (funding - fees - financing + realised price legs) "
        "minus any inventory revaluation entries")
    if incomplete:
        out["overlay_increment_incomplete"] = incomplete
        out["overlay_increment_note"] = (
            "not complete for this cycle: " + ", ".join(incomplete) + "; the "
            "increment is the BOOKED figure, not a final one")
    qty = _finite(cycle.get("hedged_qty"))
    p0 = _finite(cycle.get("perp_entry_price"))
    p1 = _finite(cycle.get("perp_exit_price"))
    prices_ok = qty is not None and qty > 0 and p0 and p1 and p0 > 0 and p1 > 0

    # (a) the same BTC, unhedged
    if not cycle.get("overlay"):
        out["vs_unhedged_btc"] = {
            "available": False, "evidence_class": "UNAVAILABLE",
            "why": ("not an overlay cycle: the book bought its spot leg, so the "
                    "unhedged alternative would need a purchase whose fee and "
                    "price were never observed")}
        pair = increment
    elif not prices_ok:
        out["vs_unhedged_btc"] = {
            "available": False, "evidence_class": "UNAVAILABLE",
            "why": "the hedged quantity or a perp fill price is not in the "
                   "journal for this cycle"}
        pair = None
    else:
        unhedged = qty * (p1 - p0)
        pair = unhedged + increment
        notional = qty * p0
        out["vs_unhedged_btc"] = {
            "available": True, "evidence_class": "HISTORICAL_SIMULATION",
            "hedged_qty_btc": qty, "price_basis": UNHEDGED_PRICE_BASIS,
            "unhedged_btc_pnl_usd": unhedged,
            "hedged_pair_pnl_usd": pair,
            "incremental_vs_unhedged_usd": increment,
            "denominator": "hedged notional at entry = hedged_qty_btc x perp "
                           "entry price",
            "denominator_usd": notional,
            "unhedged_return_pct": 100.0 * unhedged / notional,
            "hedged_pair_return_pct": 100.0 * pair / notional,
            "time_basis": OPPORTUNITY_TIME_BASIS,
            "note": ("the overlay gives up the BTC's price move in exchange "
                     "for funding; this is the size of that trade over the "
                     "period, not a risk-adjusted comparison")}

    # (b) the committed capital at an operator-supplied benchmark rate
    apr = _finite(benchmark_apr_pct)
    capital = _finite(cycle.get("capital_committed_usd"))
    if benchmark_apr_pct is None:
        out["vs_benchmark_rate"] = {
            "available": False, "evidence_class": "UNAVAILABLE",
            "why": ("no benchmark rate was supplied by the operator "
                    "(--cash-apr); none is assumed and none is defaulted")}
    elif apr is None or apr <= -100.0 or apr > 100.0:
        out["vs_benchmark_rate"] = {
            "available": False, "evidence_class": "UNAVAILABLE",
            "why": f"benchmark rate {benchmark_apr_pct!r} is not a finite "
                   "annual percentage in (-100, 100]"}
    elif capital is None or capital <= 0:
        out["vs_benchmark_rate"] = {
            "available": False, "evidence_class": "UNAVAILABLE",
            "why": "no committed capital for this cycle (no observed venue "
                   "margin, or no spot leg value), so no denominator"}
    elif pair is None:
        out["vs_benchmark_rate"] = {
            "available": False, "evidence_class": "UNAVAILABLE",
            "why": "the hedged pair's result is not computable for this cycle"}
    else:
        bench = capital * apr / 100.0 * days / 365.0
        out["vs_benchmark_rate"] = {
            "available": True, "evidence_class": "HISTORICAL_SIMULATION",
            "benchmark_apr_pct": apr,
            "benchmark_source": benchmark_source or "operator-supplied",
            "denominator": ("committed capital = observed venue initial margin "
                            "+ spot leg (cost if bought, hedged inventory at "
                            "entry if the client's)"),
            "denominator_usd": capital,
            "hold_days": days,
            "benchmark_alternative_usd": bench,
            "hedged_pair_pnl_usd": pair,
            "incremental_vs_benchmark_usd": pair - bench,
            "hedged_pair_return_pct": 100.0 * pair / capital,
            "benchmark_return_pct": apr * days / 365.0,
            "time_basis": OPPORTUNITY_TIME_BASIS}
    avail = [k for k in ("vs_unhedged_btc", "vs_benchmark_rate")
             if out[k]["available"]]
    out["evidence_class"] = "HISTORICAL_SIMULATION" if avail else "UNAVAILABLE"
    if not avail:
        out["why"] = "neither alternative is computable for this cycle"
    return out


# ---------------------------------------------------------------------------
# per-cycle net-economics statistics (pure; used by tools/carry_performance.py)
# ---------------------------------------------------------------------------

#: A return is annualised ONLY from at least this many closed cycles AND this
#: many days in market. Below either, an annualised figure is a decimal point
#: on noise (one funding print annualises to hundreds of percent), so it is
#: None with the reason. One rule for every annualised figure in the report.
MIN_ANNUALISE_CYCLES = 5
MIN_ANNUALISE_DAYS = 30.0
#: Dispersion / expectancy statistics need at least this many cycles.
MIN_CYCLES_FOR_DISPERSION = 3


def annualise_simple(return_pct: Optional[float], days: Optional[float],
                     n_cycles: int) -> Tuple[Optional[float], str]:
    """Simple (non-compounded) annualisation, or (None, reason)."""
    r, d = _finite(return_pct), _finite(days)
    if r is None or d is None:
        return None, "return or duration not available"
    if n_cycles < MIN_ANNUALISE_CYCLES:
        return None, (f"SAMPLE_TOO_SMALL: {n_cycles} closed cycle(s) < "
                      f"{MIN_ANNUALISE_CYCLES}; not annualised")
    if d < MIN_ANNUALISE_DAYS:
        return None, (f"DURATION_TOO_SHORT: {d:.1f} days in market < "
                      f"{MIN_ANNUALISE_DAYS:g}; not annualised")
    return r * 365.0 / d, ""


def max_drawdown_usd(nets_in_time_order: Sequence[float]) -> float:
    """Largest peak-to-trough fall of the cumulative net, starting from 0."""
    cum = peak = dd = 0.0
    for n in nets_in_time_order:
        cum += n
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def downside_deviation(values: Sequence[float], target: float = 0.0
                       ) -> Optional[float]:
    """sqrt(mean(min(x - target, 0)^2)) over ALL observations (population
    form, the usual semideviation). None below the dispersion minimum."""
    if len(values) < MIN_CYCLES_FOR_DISPERSION:
        return None
    return math.sqrt(sum(min(v - target, 0.0) ** 2 for v in values)
                     / len(values))


def profit_concentration(nets: Sequence[float]) -> Dict[str, Any]:
    """Share of gross winning net held by the largest winner, and by the top
    quartile; plus the total with the largest winner removed."""
    wins = sorted((n for n in nets if n > 0), reverse=True)
    total = sum(nets)
    out: Dict[str, Any] = {
        "largest_winner_usd": wins[0] if wins else None,
        "largest_winner_share_of_winning_net_pct": (
            100.0 * wins[0] / sum(wins) if wins else None),
        "net_excluding_largest_winner_usd": (total - wins[0]) if wins else total,
        "winning_cycles": len(wins)}
    if len(wins) >= 4:
        k = max(1, len(wins) // 4)
        out["top_quartile_share_of_winning_net_pct"] = (
            100.0 * sum(wins[:k]) / sum(wins))
    else:
        out["top_quartile_share_of_winning_net_pct"] = None
    return out


def expectancy(nets: Sequence[float]) -> Dict[str, Any]:
    """Mean net per closed cycle (already after every booked cost)."""
    if not nets:
        return {"expectancy_per_cycle_usd": None, "win_rate": None,
                "avg_win_usd": None, "avg_loss_usd": None}
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]
    return {"expectancy_per_cycle_usd": sum(nets) / len(nets),
            "win_rate": len(wins) / len(nets),
            "avg_win_usd": sum(wins) / len(wins) if wins else None,
            "avg_loss_usd": sum(losses) / len(losses) if losses else None}


# ---------------------------------------------------------------------------
# module-level accessors other modules import
# ---------------------------------------------------------------------------


def get_global_metrics(store: StateStore) -> PerformanceReport:
    """Metrics for the persisted account state."""
    return PerformanceAnalytics(store).report()


def get_perf(store: StateStore, starting_equity: Optional[float] = None) -> PerformanceReport:
    return PerformanceAnalytics(store).report(starting_equity)
