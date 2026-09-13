#!/usr/bin/env python3
"""Simulate the delta-neutral carry book. Two legs. Every cost. Read-only.

WHY THIS EXISTS
===============
The figure this programme has been quoting — 6.84%/yr — is a NAIVE SUM OF
FUNDING PRINTS. It is not a simulation. It contains no entries, no exits, no
fees, no borrow cost, and no basis. It is the gross income line of a business
whose expenses have never been written down.

That is exactly the mistake that killed the previous eleven signal families:
a number that looked like a return and was actually an input.

This tool charges the book for existing.

WHAT IS MODELLED
================
  FUNDING     received per print while the pair is open. The income.
  FEES        taker on both legs, in and out. One-time per round trip.
  BASIS       (perp - spot) at entry vs at exit. THE COST NOBODY BOOKS.
              Funding is rich precisely when the perp trades ABOVE spot, so you
              systematically open the short leg into a wide basis. If that basis
              narrows before you close, the convergence is a loss that offsets
              some or all of the carry you collected. A carry model without a
              basis term is an income statement with no cost of goods sold.
  BORROW      financing on the spot leg. It is collateral you had to fund.
  MARGIN      the short perp loses USDT when price rises. Tracked so a top-up
              requirement is visible rather than assumed away.

WHAT IS NOT MODELLED, AND WHY THAT MATTERS
==========================================
  SLIPPAGE beyond the taker fee. At $100 it is noise; at $1m it is the trade.
  DEPTH. No order book in this corpus at daily resolution.
  INTRADAY basis. Daily closes only, so an intraday blowout is invisible.
  LIQUIDATION. The short leg cannot be liquidated in this simulation because
  daily closes hide the wick that would do it.

THE ATTRIBUTION IDENTITY (0031)
===============================
Every run prints, and refuses to print unless it sums:

    funding + basis + fees + borrow + impact + other = NET

Costs are negative numbers in that line. `basis` is further split into the
price term of each leg (long spot, short perp); those two add up to the basis
exactly, which is where "the price cancels" is SHOWN rather than asserted.

WHAT "QUOTABLE" MEANS (0031)
============================
0020 made `is_a_quotable_return` track provenance alone: same venue, same quote
currency. That is necessary and it is not sufficient. A return may be quoted to
anyone only when EVERY condition in QUOTABLE_CONDITIONS holds:

    same_venue_same_quote_spot           the basis is not a cross-venue proxy
    settlement_clock_basis               prices at 00/08/16 UTC, not daily closes
    impact_haircut_applied               at least a conservative impact term
    rule_is_what_the_engine_runs         CarryEngine always runs the GATED rule
    rule_scored_on_an_untouched_window   a certified, registered holdout

Daily closes fail the second. The gated rule fails the fifth (its parameters
were chosen after 0018, PHASE1_DECISION). The ungated rule fails the fourth.
So no run of this tool is quotable today, and it says why, condition by
condition.

THE DATA, STATED LOUDLY
=======================
Daily mode: Binance USDT-M perp 1d, Binance BTCUSDT spot 1d, Binance funding.
Same venue and quote currency, so the basis is not a proxy. It is still a
DAILY CLOSE, not the price at 00/08/16 UTC when funding changes hands, so it
is not quotable (rule 19).

The Bitstamp USD series is no longer a spot source (0031). It carried a
cross-venue spread and the USD/USDT peg straight into the basis term, and it
must not be able to produce an allocator-facing number by being the only
file present. If the Binance spot file is missing the tool refuses.

    python3 tools/carry_backtest.py --repo .
"""
from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import gzip
import json
import math
import os
import statistics
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Costs, declared BEFORE the run and not tuned afterwards.
TAKER_BPS_SPOT = 10.0        # Binance spot taker, no VIP
TAKER_BPS_PERP = 5.5         # Binance USDT-M taker, no VIP
BORROW_APR = 0.05            # financing on the spot leg, 5%/yr
ENTRY_FUNDING_BPS = 0.2      # do not open below this 8h rate
NEGATIVE_EXIT_PRINTS = 3     # consecutive negative prints before unwinding
FUNDING_PERIODS_PER_DAY = 3


# DATA-READ LEDGER (slice 78). This tool reads price and funding corpora
# directly rather than through market_data.load_corpus, so the observer hook
# does not see it. Recording here keeps the ledger honest: a future holdout
# cannot be certified untouched if a read went unrecorded, and a DIAGNOSTIC
# read contaminates a selection decision exactly as a scoring read does.
#
# 0032: the import and the hook are separate. Run as a script, only tools/ is
# on sys.path; install() imports market_data from one level up, raised, and
# the old single try-block then set the WHOLE ledger to None — so no CLI run
# ever recorded a read. _record() below needs only reserved_holdout itself.
try:
    import reserved_holdout as _read_ledger
except Exception as _ledger_exc:  # noqa: BLE001 - never blocks a measurement
    _read_ledger = None
    _READ_LEDGER_UNAVAILABLE = repr(_ledger_exc)
if _read_ledger is not None:
    try:
        _read_ledger.install()
    except Exception:  # noqa: BLE001 - the hook is optional; _record is not
        pass


def _open(path: str):
    return gzip.open(path, "rt") if path.endswith(".gz") else open(path, "rt")


def _day(text: str) -> dt.date:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date()


@dataclass
class Trade:
    opened: dt.date
    closed: Optional[dt.date] = None
    qty: float = 0.0
    entry_spot: float = 0.0
    entry_perp: float = 0.0
    exit_spot: float = 0.0
    exit_perp: float = 0.0
    funding: float = 0.0
    fees: float = 0.0
    borrow: float = 0.0
    #: Conservative impact haircut, a COST (>= 0 here, negative in the
    #: attribution line). Zero in daily mode, where it is not modelled and the
    #: quotable condition `impact_haircut_applied` says so.
    impact: float = 0.0
    #: Any term not named above. Declared so the identity has nowhere to hide
    #: a residual; nothing books to it today.
    other: float = 0.0
    reason: str = ""
    #: True when the window ended with the pair still on. The exit is a MARK
    #: (with exit fees charged as if closed), not a closed trade.
    open_at_end: bool = False
    #: Settlement clock only: worst perp HIGH against the short entry, in %.
    max_adverse_short_pct: float = 0.0

    @property
    def notional(self) -> float:
        return self.qty * self.entry_perp

    @property
    def entry_basis_bps(self) -> float:
        return (self.entry_perp / self.entry_spot - 1.0) * 1e4

    @property
    def exit_basis_bps(self) -> float:
        return (self.exit_perp / self.exit_spot - 1.0) * 1e4

    @property
    def spot_leg_price_pnl(self) -> float:
        """Long spot: gains what spot gains."""
        return self.qty * (self.exit_spot - self.entry_spot)

    @property
    def perp_leg_price_pnl(self) -> float:
        """Short perp: gains what the perp loses."""
        return self.qty * (self.entry_perp - self.exit_perp)

    @property
    def basis_pnl(self) -> float:
        """Short perp + long spot. You earn the basis you SHED between entry
        and exit: enter wide, exit narrow, and the convergence pays you. Enter
        narrow, exit wide, and it costs you. Identically the sum of the two
        legs' price terms, because both legs hold the same BTC quantity."""
        return self.qty * ((self.entry_perp - self.exit_perp)
                           - (self.entry_spot - self.exit_spot))

    @property
    def net(self) -> float:
        return (self.funding + self.basis_pnl - self.fees - self.borrow
                - self.impact + self.other)

    @property
    def days(self) -> int:
        return (self.closed - self.opened).days if self.closed else 0


#: Spot sources in order of preference. The basis term is
#: (perp - spot) / spot, so a spot leg from a different VENUE or a different
#: QUOTE CURRENCY injects that venue's spread and that currency's peg directly
#: into what the tool reports as carry P&L. USDT has traded ~5% off USD before
#: (March 2023). Every one of those basis points would be booked as strategy
#: return by a naive proxy.
#:
#: Only the first entry produces a quotable number, because only the first
#: entry is the same venue and the same quote currency as the perp leg.
SPOT_SOURCES = [
    ("data/real_spot_btc/ohlcv/BINANCE_SPOT_BTC_USDT_1D.csv.gz",
     "BINANCE_SPOT_BTC_USDT (same venue, same quote currency as the perp)",
     True),
    # BITSTAMP_SPOT_BTC_USD removed in 0031. See the module docstring.
]

#: A daily bar for a day that has not closed is not a close. Binance returns the
#: in-progress bar from its klines endpoint and tools/fetch_binance_klines.py
#: writes it, so the last row of a freshly fetched series is routinely a partial
#: day wearing a close's clothes. Dropping it here rather than trusting the
#: fetcher is the same rule the linear corpus already enforces: never write the
#: open UTC daily bar as a close.
def drop_open_bar(series: Dict[dt.date, float]) -> Dict[dt.date, float]:
    """Remove any bar for today or later. Today has not finished."""
    today = dt.datetime.now(dt.timezone.utc).date()
    return {d: v for d, v in series.items() if d < today}


def resolve_spot(repo: str):
    """The best spot series present, its provenance, and whether it is quotable."""
    for rel, label, quotable in SPOT_SOURCES:
        path = os.path.join(repo, rel)
        if os.path.exists(path):
            series = drop_open_bar(load_series(path))
            return series, label, quotable
    raise SystemExit(
        "no spot series found. Fetch one:\n"
        "  python3 tools/fetch_binance_klines.py --symbols BTCUSDT "
        "--intervals 1d --years 4 --out data/real_spot_btc")


#: Corpus defects that invalidate a backtest, and the one that does not.
#:
#: STALENESS IS NOT A DEFECT HERE. The committed corpus is deliberately FROZEN:
#: it records what slice 76 measured, and tests/test_slice76_fifteen_day_window
#: and tests/test_slice77_selection_contamination pin its row count for exactly
#: that reason. Refreshing it in git would retroactively falsify what those
#: slices saw. The LIVE book does not read this corpus at all — it reads the
#: venue through MarketSnapshot, which has its own freshness gate.
#:
#: So a frozen corpus is stale by design and a backtest on it is still valid.
#:
#: STRUCTURAL defects are different. An interior hole means a day the venue had
#: and this file does not, so the simulation silently skips it. An open bar
#: means a day that had not finished, priced as though it had. Neither is a
#: matter of age, and a number computed over either is wrong rather than old.
STRUCTURAL_DEFECTS = ("OPEN_BAR_IN_FILE", "NOT_MONOTONIC", "DUPLICATE_DAYS",
                      "EMPTY", "MISSING")


def corpus_verdict(repo: str):
    """Health of the three series, with staleness excluded. See above."""
    import corpus_health
    report = corpus_health.check(repo, max_stale_days=10_000)
    structural = [p for p in report["problems"]
                  if any(d in p for d in STRUCTURAL_DEFECTS)]
    holes = report.get("alignment_gaps", [])
    return report, structural, holes


def _carry_costs():
    """Import the gates lazily so the backtester still runs without them."""
    import sys as _sys
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if here not in _sys.path:
        _sys.path.insert(0, here)
    import carry_costs
    return carry_costs


def load_series(path: str) -> Dict[dt.date, float]:
    with _open(path) as handle:
        return {_day(r["time_period_start"]): float(r["price_close"])
                for r in csv.DictReader(handle)}


def load_funding(path: str):
    with _open(path) as handle:
        rows = list(csv.DictReader(handle))
    return ([int(r["funding_time_ms"]) for r in rows],
            [float(r["funding_rate"]) for r in rows])


#: The five conditions a return must meet before it may be quoted to anyone.
#: `is_a_quotable_return` is their conjunction. Each is computed, none assumed.
QUOTABLE_CONDITIONS = (
    "same_venue_same_quote_spot",
    "settlement_clock_basis",
    "impact_haircut_applied",
    "rule_is_what_the_engine_runs",
    "rule_scored_on_an_untouched_window",
)

#: No holdout has been certified untouched and scored for either rule.
#: `tools/reserved_holdout.py --certify` must pass on a registered window, and
#: the score must exist, before this may change — in a patch of its own, with
#: the certification output in the commit message. It is a module constant so
#: that flipping it is a visible diff, never a runtime argument.
HOLDOUT_SCORED = False

_WHY = {
    "same_venue_same_quote_spot":
        "the spot leg is priced on a DIFFERENT VENUE in a DIFFERENT CURRENCY, "
        "so the basis carries cross-venue spread and USD-vs-USDT depeg",
    "settlement_clock_basis":
        "prices are DAILY CLOSES, not the 00/08/16 UTC settlement instants "
        "where funding changes hands and the basis is actually paid",
    "impact_haircut_applied":
        "no impact haircut: fills are charged the taker fee only, as if the "
        "book never moved a price",
    "rule_is_what_the_engine_runs":
        "this is the UNGATED rule — CarryEngine always runs the gated rule, "
        "so this number describes a book nobody will run",
    "rule_scored_on_an_untouched_window":
        "no registered holdout has been certified and scored — the gated "
        "parameters are IN-SAMPLE (chosen after 0018, PHASE1_DECISION)",
}


def quotability(*, same_venue: bool, settlement_clock: bool,
                impact_applied: bool, gated: bool) -> Dict[str, Any]:
    conditions = {
        "same_venue_same_quote_spot": bool(same_venue),
        "settlement_clock_basis": bool(settlement_clock),
        "impact_haircut_applied": bool(impact_applied),
        "rule_is_what_the_engine_runs": bool(gated),
        "rule_scored_on_an_untouched_window": bool(HOLDOUT_SCORED),
    }
    unmet = [k for k in QUOTABLE_CONDITIONS if not conditions[k]]
    return {"quotable_conditions": conditions,
            "is_a_quotable_return": not unmet,
            "why_not_quotable": "; ".join(_WHY[k] for k in unmet)}


#: The identity must hold to this many dollars or the tool refuses to print.
IDENTITY_TOLERANCE_USD = 1e-6


def attribution(trades: List[Trade]) -> Dict[str, float]:
    """funding + basis + fees + borrow + impact + other = NET, costs negative.

    Raises if the parts do not sum. A report whose lines do not add up is not
    a report, and the one place an unexplained residual could hide is the
    place an allocator will look first.
    """
    a = {
        "funding_usd": sum(t.funding for t in trades),
        "basis_usd": sum(t.basis_pnl for t in trades),
        "fees_usd": 0.0 - sum(t.fees for t in trades),
        "borrow_usd": 0.0 - sum(t.borrow for t in trades),
        "impact_usd": 0.0 - sum(t.impact for t in trades),
        "other_usd": sum(t.other for t in trades),
        "spot_leg_price_usd": sum(t.spot_leg_price_pnl for t in trades),
        "perp_leg_price_usd": sum(t.perp_leg_price_pnl for t in trades),
        "net_usd": sum(t.net for t in trades),
    }
    parts = (a["funding_usd"] + a["basis_usd"] + a["fees_usd"]
             + a["borrow_usd"] + a["impact_usd"] + a["other_usd"])
    a["residual_usd"] = a["net_usd"] - parts
    legs = a["spot_leg_price_usd"] + a["perp_leg_price_usd"] - a["basis_usd"]
    scale = max(1.0, abs(a["net_usd"]), abs(a["basis_usd"]))
    if abs(a["residual_usd"]) > IDENTITY_TOLERANCE_USD * scale or \
            abs(legs) > IDENTITY_TOLERANCE_USD * scale:
        raise ArithmeticError(
            f"attribution does not sum: residual {a['residual_usd']!r}, "
            f"leg/basis mismatch {legs!r}. Refusing to print a NET whose "
            "parts do not add up.")
    return a


def simulate_series(*, perp: Dict[dt.date, float], spot: Dict[dt.date, float],
                    ftimes: List[int], frates: List[float],
                    notional: float = 100_000.0,
                    entry_bps: float = ENTRY_FUNDING_BPS,
                    borrow_apr: float = BORROW_APR,
                    gated: bool = False) -> Dict[str, Any]:
    """The daily simulation on in-memory series. `simulate()` loads and calls
    this; tests call it on synthetic series to prove the hedge cancels."""
    days = sorted(set(perp) & set(spot))
    if not days:
        raise SystemExit("no overlapping days between spot and perp")

    trades: List[Trade] = []
    live: Optional[Trade] = None
    negative_streak = 0
    history: List[float] = []
    refusals: Dict[str, int] = {}

    for today in days:
        p, s = perp[today], spot[today]

        # Funding actually printed on this UTC day, in bps.
        start = int(dt.datetime.combine(
            today, dt.time(), dt.timezone.utc).timestamp() * 1000)
        end = start + 86_400_000
        lo = bisect.bisect_left(ftimes, start)
        hi = bisect.bisect_left(ftimes, end)
        prints = frates[lo:hi]
        day_bps = sum(prints) * 1e4
        last_bps = (prints[-1] * 1e4) if prints else 0.0

        if live is not None:
            history.append(last_bps)
            # Income and financing accrue whether or not anything is traded.
            live.funding += (day_bps / 1e4) * live.qty * p
            live.borrow += (borrow_apr / 365.0) * live.qty * s

            if last_bps < 0:
                negative_streak += 1
            else:
                negative_streak = 0

            if negative_streak >= NEGATIVE_EXIT_PRINTS:
                live.closed, live.exit_spot, live.exit_perp = today, s, p
                live.fees += live.qty * (s * TAKER_BPS_SPOT
                                         + p * TAKER_BPS_PERP) / 1e4
                live.reason = "FUNDING_INVERTED"
                trades.append(live)
                live = None
                negative_streak = 0
            continue

        if gated:
            history.append(last_bps)
            verdict = _carry_costs().evaluate_entry(
                funding_prints_bps=history[-8:], perp=p, spot=s,
                borrow_apr=borrow_apr)
            may_open = bool(verdict)
            if not may_open:
                refusals[verdict.reason] = refusals.get(verdict.reason, 0) + 1
        else:
            may_open = last_bps >= entry_bps

        if may_open:
            qty = notional / p
            live = Trade(opened=today, qty=qty, entry_spot=s, entry_perp=p)
            live.fees = qty * (s * TAKER_BPS_SPOT + p * TAKER_BPS_PERP) / 1e4
            negative_streak = 0

    if live is not None:
        last = days[-1]
        live.closed, live.exit_spot, live.exit_perp = last, spot[last], perp[last]
        live.fees += live.qty * (spot[last] * TAKER_BPS_SPOT
                                 + perp[last] * TAKER_BPS_PERP) / 1e4
        live.reason = "OPEN_AT_END"
        live.open_at_end = True
        trades.append(live)

    years = max((days[-1] - days[0]).days, 1) / 365.25
    a = attribution(trades)
    held = sum(t.days for t in trades)
    closed = [t for t in trades if not t.open_at_end]
    marked = [t for t in trades if t.open_at_end]

    # Naive figure this replaces, for direct contrast.
    naive = sum(frates) * 100.0

    return {
        "clock": "daily_close",
        "window": f"{days[0]} .. {days[-1]}",
        "years": years,
        "notional_usd": notional,
        "trades": len(trades),
        "closed_trades": len(closed),
        "closed_net_usd": sum(t.net for t in closed),
        "open_at_end_marked_usd": sum(t.net for t in marked),
        "days_in_market": held,
        "market_exposure_pct": 100.0 * held / max(1, (days[-1] - days[0]).days),
        "attribution": a,
        "gross_funding_usd": a["funding_usd"],
        "basis_pnl_usd": a["basis_usd"],
        "fees_usd": -a["fees_usd"],
        "borrow_usd": -a["borrow_usd"],
        "impact_usd": -a["impact_usd"],
        "net_usd": a["net_usd"],
        "net_return_pct": 100.0 * a["net_usd"] / notional,
        "net_annualised_pct": (100.0 * a["net_usd"] / notional) / years,
        "naive_funding_sum_pct_per_yr": naive / years,
        "round_trip_bps": 2 * TAKER_BPS_SPOT + 2 * TAKER_BPS_PERP,
        "borrow_apr": borrow_apr,
        "mean_entry_basis_bps": statistics.mean(
            t.entry_basis_bps for t in trades) if trades else 0.0,
        "mean_exit_basis_bps": statistics.mean(
            t.exit_basis_bps for t in trades) if trades else 0.0,
        "worst_trade_usd": min((t.net for t in trades), default=0.0),
        "best_trade_usd": max((t.net for t in trades), default=0.0),
        "losing_trades": sum(1 for t in trades if t.net < 0),
        "gated": gated,
        "refusals": refusals,
        "trade_log": [{"opened": str(t.opened), "closed": str(t.closed),
                       "days": t.days, "funding": round(t.funding, 2),
                       "basis": round(t.basis_pnl, 2), "fees": round(t.fees, 2),
                       "borrow": round(t.borrow, 2),
                       "impact": round(t.impact, 2),
                       "other": round(t.other, 2), "net": round(t.net, 2),
                       "entry_basis_bps": round(t.entry_basis_bps, 1),
                       "exit_basis_bps": round(t.exit_basis_bps, 1),
                       "reason": t.reason, "open_at_end": t.open_at_end}
                      for t in trades],
    }


def simulate(repo: str, *, notional: float = 100_000.0,
             entry_bps: float = ENTRY_FUNDING_BPS,
             borrow_apr: float = BORROW_APR,
             gated: bool = False) -> Dict[str, Any]:
    """Daily-close simulation over the committed Binance corpora.

    `gated=True` routes every entry through carry_costs.evaluate_entry — the
    rule CarryEngine actually runs. Run both. The difference between them is
    the only evidence that the gates are worth having: a gate that changes no
    trade is decoration, and a gate that refuses everything is a way of not
    trading dressed as risk management.
    """
    perp_path = os.path.join(
        repo, "data/real_linear_1d/ohlcv/BINANCE_LINEAR_BTC_USDT_1D.csv.gz")
    perp = load_series(perp_path)
    spot, spot_source, same_venue = resolve_spot(repo)
    ftimes, frates = load_funding(os.path.join(
        repo, "data/real_funding/funding/BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz"))

    # A number computed over a corpus with an interior hole or an open bar is
    # WRONG, not old. carry_backtest used to intersect the three series in
    # silence: a day present in one and missing from another simply vanished
    # from the simulation, and nothing said so.
    health, structural, holes = corpus_verdict(repo)
    if structural:
        raise SystemExit(
            "REFUSING TO BACKTEST — structural corpus defects:\n  "
            + "\n  ".join(structural)
            + "\nStaleness is fine (the corpus is deliberately frozen). These "
              "are not staleness: they are days priced as closed that were not, "
              "or rows the simulation cannot trust.")

    r = simulate_series(perp=perp, spot=spot, ftimes=ftimes, frates=frates,
                        notional=notional, entry_bps=entry_bps,
                        borrow_apr=borrow_apr, gated=gated)
    _record("BINANCE_LINEAR_BTC_USDT_1D", _iso_day(min(perp)),
            _iso_day(max(perp)), "daily carry backtest (perp leg)")
    _record("BINANCE_SPOT_BTC_USDT_1D", _iso_day(min(spot)),
            _iso_day(max(spot)), "daily carry backtest (spot leg)")
    _record("BINANCE_LINEAR_BTC_USDT_FUNDING", _iso_ms(ftimes[0]),
            _iso_ms(ftimes[-1]), "daily carry backtest (funding)")
    r.update(quotability(same_venue=same_venue, settlement_clock=False,
                         impact_applied=False, gated=gated))
    r.update({
        "spot_source": spot_source,
        "basis_is_a_proxy": not same_venue,
        "open_bar_dropped": True,
        "not_modelled": ["slippage beyond taker fees", "order book depth",
                         "intraday basis", "liquidation of the short leg",
                         "settlement-clock prices (daily closes only)"],
        "corpus_health": {
            "structural_defects": structural,
            "interior_holes": holes,
            "series": [{"name": x["name"], "rows": x.get("rows"),
                        "last": x.get("last"), "sha256": x.get("sha256")}
                       for x in health["series"]],
            "note": ("staleness is deliberately NOT a defect: the committed "
                     "corpus is frozen so the slice tests keep recording what "
                     "past measurements saw. The live book reads the venue."),
        },
    })
    return r


# ---------------------------------------------------------------------------
# DATA-READ LEDGER — recorded explicitly (0032)
# ---------------------------------------------------------------------------
#: install() above hooks market_data.load_corpus. This tool never calls it, so
#: until 0032 not one of its reads reached the ledger (INVENTORY D6). Each read
#: is now recorded here, by dataset and range, through the same idempotent
#: auto_record the loader uses. Tests set LEDGER_DISABLED=1, so a test run
#: records nothing. None means reserved_holdout.DEFAULT_PATH.
LEDGER_PATH: Optional[str] = None
READER = "tools/carry_backtest.py"


def _iso_ms(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000.0, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _iso_day(day: dt.date) -> str:
    return f"{day.isoformat()}T00:00:00Z"


def _record(dataset: str, first: str, last: str, purpose: str) -> None:
    if _read_ledger is None:
        return
    kwargs = {} if LEDGER_PATH is None else {"path": LEDGER_PATH}
    _read_ledger.auto_record(dataset=dataset, from_utc=first, to_utc=last,
                             read_by=READER, purpose=purpose, **kwargs)


# ---------------------------------------------------------------------------
# THE SETTLEMENT CLOCK (0032)
# ---------------------------------------------------------------------------
#: Funding settles at 00:00, 08:00 and 16:00 UTC. The price at a settlement is
#: the CLOSE of the bar that ENDS at it — the convention 0027 fixed for the
#: Binance 8h fetcher, and the one data/real_bybit_btc_4h/MANIFEST.json states.
SETTLEMENT_MS = 8 * 3600 * 1000
BAR_4H_MS = 4 * 3600 * 1000

BYBIT_DIR = "data/real_bybit_btc_4h"
BYBIT_SPOT_4H = f"{BYBIT_DIR}/ohlcv/BYBIT_SPOT_BTC_USDT_4H.csv.gz"
BYBIT_PERP_4H = f"{BYBIT_DIR}/ohlcv/BYBIT_LINEAR_BTC_USDT_4H.csv.gz"
BYBIT_FUNDING = f"{BYBIT_DIR}/funding/BYBIT_LINEAR_BTC_USDT_FUNDING.csv.gz"
BINANCE_8H_DIR = "data/real_settlement_8h"

#: Conservative impact haircut, per leg, in bps. DECLARED BEFORE THE RUN, not
#: measured: the BTCUSDT tick at ~$79k is ~0.01 bps, so a 1 bp floor is about
#: a hundred ticks of half-spread-plus-slippage per market order. Measuring the
#: real number needs L1/L2 tick data, which this tree does not have. It is a
#: haircut, and it is flagged as one wherever it is printed.
IMPACT_FLOOR_BPS_PER_LEG = 1.0
#: The only depth data in the tree: Binance USD-M bookDepth, median cumulative
#: notional per 1% band, per day (0028). Used for a LINEAR walk through the
#: THINNEST day's 1% band: walking to depth d fills at ~d/2 from mid. Binance
#: futures depth stands in for BOTH Bybit legs — Bybit books are thinner and
#: spot books thinner still, which is why the floor, not the walk, dominates at
#: any size this book may trade.
DEPTH_FILE = "research/exchange_study/binance_bookdepth_median.json"

#: Short-leg maintenance margin at the lowest Bybit risk tier, read from the
#: study set, for the leverage table in the wick layer. Isolated-margin
#: approximation: UTA cross margin with spot collateral behaves differently.
RISK_LIMIT_FILE = "research/exchange_study/bybit/risk_limit.json.gz"


@dataclass(frozen=True)
class Settlement:
    ms: int
    perp: float
    spot: float
    rate: float                 # funding rate printed AT ms (fraction)
    perp_high: float            # highest perp trade in the 8h ending at ms
    perp_low: float


def _when(ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000.0, dt.timezone.utc)


def _load_4h(path: str) -> Dict[int, tuple]:
    out: Dict[int, tuple] = {}
    with _open(path) as handle:
        for r in csv.DictReader(handle):
            t = r["time_period_start"].replace("Z", "+00:00")
            if len(t.split(".")) == 2 and "+" in t:   # 7-digit fraction
                t = t.split(".")[0] + "+00:00"
            ms = int(dt.datetime.fromisoformat(t).timestamp() * 1000)
            out[ms] = (float(r["price_open"]), float(r["price_high"]),
                       float(r["price_low"]), float(r["price_close"]))
    return out


def load_bybit_settlements(repo: str):
    """Bybit spot + Bybit perp + Bybit funding, one row per settlement."""
    paths = [os.path.join(repo, p) for p in (BYBIT_SPOT_4H, BYBIT_PERP_4H,
                                             BYBIT_FUNDING)]
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        raise SystemExit("no same-venue settlement corpus. Build it:\n"
                         "  python3 tools/promote_bybit_study_set.py --write")
    spot = _load_4h(paths[0])
    perp = _load_4h(paths[1])
    with _open(paths[2]) as handle:
        funding = {int(r["funding_time_ms"]): float(r["funding_rate"])
                   for r in csv.DictReader(handle)}
    rows: List[Settlement] = []
    lo, hi = min(perp), max(perp) + BAR_4H_MS
    for ms in sorted(funding):
        if ms < lo + BAR_4H_MS or ms > hi:
            continue                        # outside kline coverage, at the edges
        last = ms - BAR_4H_MS
        first = ms - 2 * BAR_4H_MS
        if last not in perp or last not in spot:
            raise SystemExit(f"REFUSING — no 4h bar ends at settlement "
                             f"{_iso_ms(ms)}; the corpus has an interior hole")
        window = [perp[b] for b in (first, last) if b in perp]
        rows.append(Settlement(ms=ms, perp=perp[last][3], spot=spot[last][3],
                               rate=funding[ms],
                               perp_high=max(w[1] for w in window),
                               perp_low=min(w[2] for w in window)))
    if not rows:
        raise SystemExit("no settlements overlap the 4h klines")
    first_bar, last_bar = _iso_ms(min(perp)), _iso_ms(max(perp))
    _record("BYBIT_SPOT_BTC_USDT_4H", first_bar, last_bar,
            "settlement-clock carry backtest (spot leg)")
    _record("BYBIT_LINEAR_BTC_USDT_4H", first_bar, last_bar,
            "settlement-clock carry backtest (perp leg)")
    _record("BYBIT_LINEAR_BTC_USDT_FUNDING", _iso_ms(rows[0].ms),
            _iso_ms(rows[-1].ms), "settlement-clock carry backtest (funding)")
    meta = {"same_venue": True,
            "label": ("BYBIT spot 4h + BYBIT linear 4h + BYBIT funding — same "
                      "venue and quote currency as each other AND as the "
                      "venue CarryBroker trades on"),
            "corpus": BYBIT_DIR}
    return rows, meta


def load_binance_settlements(repo: str):
    """Binance 8h klines (0027's fetcher) + the Binance funding corpus."""
    folder = os.path.join(repo, BINANCE_8H_DIR)
    perp_p = os.path.join(folder, "BINANCE_PERP_BTCUSDT_8H.csv.gz")
    spot_p = os.path.join(folder, "BINANCE_SPOT_BTCUSDT_8H.csv.gz")
    if not (os.path.exists(perp_p) and os.path.exists(spot_p)):
        raise SystemExit(
            "no Binance 8h settlement klines in this checkout. Fetch them "
            "(both legs), from a host that can reach Binance:\n"
            "  python3 tools/fetch_settlement_klines.py --write\n"
            "or run the same-venue Bybit clock:  --clock 8h --venue bybit")

    def load(path):
        with _open(path) as handle:
            return {int(r["open_time_ms"]) + SETTLEMENT_MS:
                    (float(r["price_high"]), float(r["price_low"]),
                     float(r["price_close"]))
                    for r in csv.DictReader(handle)}

    perp, spot = load(perp_p), load(spot_p)
    ftimes, frates = load_funding(os.path.join(
        repo, "data/real_funding/funding/BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz"))
    rows: List[Settlement] = []
    for ms, rate in zip(ftimes, frates):
        stamp = int(round(ms / SETTLEMENT_MS)) * SETTLEMENT_MS
        if abs(stamp - ms) > 60_000 or stamp not in perp or stamp not in spot:
            continue
        rows.append(Settlement(ms=stamp, perp=perp[stamp][2],
                               spot=spot[stamp][2], rate=rate,
                               perp_high=perp[stamp][0],
                               perp_low=perp[stamp][1]))
    if not rows:
        raise SystemExit("no Binance settlements overlap the funding corpus")
    _record("BINANCE_PERP_BTCUSDT_8H", _iso_ms(rows[0].ms), _iso_ms(rows[-1].ms),
            "settlement-clock carry backtest (perp leg)")
    _record("BINANCE_SPOT_BTCUSDT_8H", _iso_ms(rows[0].ms), _iso_ms(rows[-1].ms),
            "settlement-clock carry backtest (spot leg)")
    _record("BINANCE_LINEAR_BTC_USDT_FUNDING", _iso_ms(rows[0].ms),
            _iso_ms(rows[-1].ms), "settlement-clock carry backtest (funding)")
    return rows, {"same_venue": True, "corpus": BINANCE_8H_DIR,
                  "label": ("BINANCE spot 8h + BINANCE USDT-M 8h + BINANCE "
                            "funding — same venue and quote currency; NOT the "
                            "venue CarryBroker trades on")}


def impact_bps_per_leg(repo: str, notional: float):
    """Floor + linear walk through the thinnest measured 1% band. Refuses
    without depth data rather than guessing a number."""
    path = os.path.join(repo, DEPTH_FILE)
    if not os.path.exists(path):
        raise SystemExit(f"no depth data at {DEPTH_FILE}; refusing to invent "
                         "an impact number. Run with --no-impact and read the "
                         "impact condition as unmet.")
    with open(path, encoding="utf-8") as handle:
        days = json.load(handle)["days"]
    thinnest = min(min(float(b["1.0"]), float(b["-1.0"]))
                   for b in days.values() if "1.0" in b and "-1.0" in b)
    walk = 0.5 * 100.0 * float(notional) / thinnest
    return IMPACT_FLOOR_BPS_PER_LEG + walk, {
        "floor_bps": IMPACT_FLOOR_BPS_PER_LEG, "walk_bps": walk,
        "thinnest_1pct_band_usd": thinnest, "depth_source": DEPTH_FILE}


def simulate_settlements_series(rows: List[Settlement], *,
                                notional: float = 100_000.0,
                                entry_bps: float = ENTRY_FUNDING_BPS,
                                borrow_apr: float = BORROW_APR,
                                gated: bool = False,
                                impact_bps: float = 0.0) -> Dict[str, Any]:
    """One step per funding print. The rules are the engine's, in its units:
    a print is a print (not a day, not a tick), three consecutive negative
    PRINTS exit, and the EWMA sees the last eight PRINTS."""
    trades: List[Trade] = []
    live: Optional[Trade] = None
    streak = 0
    history: List[float] = []
    refusals: Dict[str, int] = {}
    per_print_borrow = borrow_apr / (365.0 * FUNDING_PERIODS_PER_DAY)

    def charge(t: Trade, s: float, p: float) -> None:
        t.fees += t.qty * (s * TAKER_BPS_SPOT + p * TAKER_BPS_PERP) / 1e4
        t.impact += t.qty * (s + p) * impact_bps / 1e4

    for st in rows:
        r_bps = st.rate * 1e4
        history.append(r_bps)
        if live is not None:
            # Held through this settlement: the short receives (pays) it.
            live.funding += st.rate * live.qty * st.perp
            live.borrow += per_print_borrow * live.qty * st.spot
            live.max_adverse_short_pct = max(
                live.max_adverse_short_pct,
                100.0 * (st.perp_high / live.entry_perp - 1.0))
            streak = streak + 1 if r_bps < 0 else 0
            if streak >= NEGATIVE_EXIT_PRINTS:
                live.closed = _when(st.ms)
                live.exit_spot, live.exit_perp = st.spot, st.perp
                charge(live, st.spot, st.perp)
                live.reason = "FUNDING_INVERTED"
                trades.append(live)
                live, streak = None, 0
            continue

        if gated:
            verdict = _carry_costs().evaluate_entry(
                funding_prints_bps=history[-8:], perp=st.perp, spot=st.spot,
                borrow_apr=borrow_apr)
            may_open = bool(verdict)
            if not may_open:
                refusals[verdict.reason] = refusals.get(verdict.reason, 0) + 1
        else:
            may_open = r_bps >= entry_bps

        if may_open:
            qty = notional / st.perp
            live = Trade(opened=_when(st.ms), qty=qty, entry_spot=st.spot,
                         entry_perp=st.perp)
            charge(live, st.spot, st.perp)
            streak = 0

    if live is not None:
        last = rows[-1]
        live.closed = _when(last.ms)
        live.exit_spot, live.exit_perp = last.spot, last.perp
        charge(live, last.spot, last.perp)
        live.reason = "OPEN_AT_END"
        live.open_at_end = True
        trades.append(live)

    a = attribution(trades)
    span_days = max((rows[-1].ms - rows[0].ms) / 86_400_000.0, 1.0)
    years = span_days / 365.25
    held = sum((t.closed - t.opened).total_seconds() / 86_400.0
               for t in trades)
    closed = [t for t in trades if not t.open_at_end]
    marked = [t for t in trades if t.open_at_end]
    return {
        "clock": "settlement_8h",
        "window": f"{_iso_ms(rows[0].ms)} .. {_iso_ms(rows[-1].ms)}",
        "years": years, "notional_usd": notional,
        "settlements": len(rows),
        "trades": len(trades), "closed_trades": len(closed),
        "closed_net_usd": sum(t.net for t in closed),
        "open_at_end_marked_usd": sum(t.net for t in marked),
        "market_exposure_pct": 100.0 * held / span_days,
        "attribution": a,
        "gross_funding_usd": a["funding_usd"],
        "basis_pnl_usd": a["basis_usd"],
        "fees_usd": -a["fees_usd"], "borrow_usd": -a["borrow_usd"],
        "impact_usd": -a["impact_usd"], "net_usd": a["net_usd"],
        "net_return_pct": 100.0 * a["net_usd"] / notional,
        "net_annualised_pct": (100.0 * a["net_usd"] / notional) / years,
        "naive_funding_sum_pct_per_yr": 100.0 * sum(s.rate for s in rows) / years,
        "borrow_apr": borrow_apr, "impact_bps_per_leg": impact_bps,
        "mean_entry_basis_bps": statistics.mean(
            t.entry_basis_bps for t in trades) if trades else 0.0,
        "mean_exit_basis_bps": statistics.mean(
            t.exit_basis_bps for t in trades) if trades else 0.0,
        "worst_trade_usd": min((t.net for t in trades), default=0.0),
        "best_trade_usd": max((t.net for t in trades), default=0.0),
        "losing_trades": sum(1 for t in trades if t.net < 0),
        "max_adverse_short_excursion_pct": max(
            (t.max_adverse_short_pct for t in trades), default=0.0),
        "wick_note": ("perp HIGHS of the 4h/8h bars against each short entry. "
                      "This is NOT a tick wick: the liquidation engine sees "
                      "every trade, and a 4h high is only a floor on it. Over "
                      "a hold of months it is the price rise itself, which the "
                      "short's margin must absorb from the spot collateral or "
                      "be liquidated."),
        "gated": gated, "refusals": refusals,
        "trade_log": [{"opened": str(t.opened), "closed": str(t.closed),
                       "funding": round(t.funding, 2),
                       "basis": round(t.basis_pnl, 2), "fees": round(t.fees, 2),
                       "borrow": round(t.borrow, 2),
                       "impact": round(t.impact, 2),
                       "other": round(t.other, 2), "net": round(t.net, 2),
                       "entry_basis_bps": round(t.entry_basis_bps, 1),
                       "exit_basis_bps": round(t.exit_basis_bps, 1),
                       "max_adverse_short_pct": round(t.max_adverse_short_pct, 2),
                       "reason": t.reason, "open_at_end": t.open_at_end}
                      for t in trades],
    }


def simulate_settlement(repo: str, *, venue: str = "bybit",
                        notional: float = 100_000.0,
                        entry_bps: float = ENTRY_FUNDING_BPS,
                        borrow_apr: float = BORROW_APR, gated: bool = False,
                        impact: bool = True) -> Dict[str, Any]:
    """The settlement-clock run. Same venue; prices at 00/08/16 UTC."""
    if venue == "bybit":
        rows, meta = load_bybit_settlements(repo)
    elif venue == "binance":
        rows, meta = load_binance_settlements(repo)
    else:
        raise SystemExit(f"unknown venue {venue!r}; refusing rather than "
                         "defaulting")
    bps, detail = (impact_bps_per_leg(repo, notional) if impact
                   else (0.0, {"note": "impact NOT applied (--no-impact)"}))
    r = simulate_settlements_series(rows, notional=notional,
                                    entry_bps=entry_bps, borrow_apr=borrow_apr,
                                    gated=gated, impact_bps=bps)
    r.update(quotability(same_venue=meta["same_venue"], settlement_clock=True,
                         impact_applied=impact, gated=gated))
    r.update({"venue": venue, "spot_source": meta["label"],
              "corpus": meta["corpus"], "basis_is_a_proxy": False,
              "impact_detail": detail,
              "not_modelled": ["tick wicks (4h highs only)",
                               "maker/taker mix (all taker: conservative)",
                               "measured spread (impact is a declared haircut)",
                               "venue funding noise beyond the printed rate",
                               "the fee on a spot BUY charged in BTC",
                               "liquidation of the short leg"]})
    return r


#: Named stress windows, first-class. Dates are UTC and inclusive.
STRESS = (
    ("2020-03-12 COVID crash", "2020-03-11", "2020-03-14", None),
    ("2022-11 FTX collapse", "2022-11-06", "2022-11-14", None),
    ("2024-08-05 yen-carry unwind", "2024-08-03", "2024-08-07", "2024-08-05"),
    ("2025-02-03 (0028 1m set)", "2025-02-02", "2025-02-04",
     "2025-02-03"),
    ("2026-04-15 (0028 1m set)", "2026-04-14", "2026-04-16", "2026-04-15"),
    ("USDT depeg", None, None, None),
)


def _stress_1m(repo: str, day: str, entry_perp: float) -> Optional[Dict[str, Any]]:
    folder = os.path.join(repo, "research/exchange_study/stress")
    try:
        with gzip.open(os.path.join(folder, f"perp_1m_{day}.json.gz"), "rt") as h:
            perp = {int(k[0]): (float(k[2]), float(k[4])) for k in json.load(h)}
        with gzip.open(os.path.join(folder, f"spot_1m_{day}.json.gz"), "rt") as h:
            spot = {int(k[0]): float(k[4]) for k in json.load(h)}
    except FileNotFoundError:
        return None
    common = sorted(set(perp) & set(spot))
    if not common:
        return None
    basis = [(perp[t][1] / spot[t] - 1.0) * 1e4 for t in common]
    return {"minutes": len(common),
            "venue": ("BINANCE (the 0028 stress files are Binance 12-field "
                      "klines) — cross-venue against the Bybit entry price"),
            "max_1m_high_vs_window_entry_pct":
                100.0 * (max(perp[t][0] for t in common) / entry_perp - 1.0),
            "basis_1m_min_bps": min(basis), "basis_1m_max_bps": max(basis)}


def stress_scenarios(repo: str, *, notional: float = 100_000.0,
                     borrow_apr: float = BORROW_APR) -> Dict[str, Any]:
    """A pair ALREADY ON when each window opens, held through it. No entry or
    exit fees: the question is what the window does to a book, not what it
    costs to trade into it."""
    rows, meta = load_bybit_settlements(repo)
    first_day = _when(rows[0].ms).date()
    mm = None
    try:
        with gzip.open(os.path.join(repo, RISK_LIMIT_FILE), "rt") as h:
            tiers = json.load(h)["result"]["list"]
        mm = float(sorted(tiers, key=lambda t: float(t["riskLimitValue"]))[0]
                   ["maintenanceMargin"])
    except (OSError, KeyError, ValueError, IndexError):
        mm = None
    out = []
    for name, start, end, day1m in STRESS:
        if start is None:
            out.append({"name": name, "status": "NOT_MEASURABLE",
                        "reason": ("both legs and all P&L are in USDT, so a "
                                   "USDT/USD depeg moves the book's USD NAV, "
                                   "not the pair's USDT P&L; no USDT/USD "
                                   "series exists in this tree to measure it")})
            continue
        lo = dt.datetime.fromisoformat(start + "T00:00:00+00:00")
        hi = dt.datetime.fromisoformat(end + "T23:59:59+00:00")
        window = [r for r in rows if lo <= _when(r.ms) <= hi]
        if len(window) < 2:
            out.append({"name": name, "status": "DATA_ABSENT",
                        "reason": (f"the same-venue corpus starts {first_day}; "
                                   "no settlement in this window. Cross-venue "
                                   "or daily data would not be the same "
                                   "measurement and is not substituted")})
            continue
        e = window[0]
        qty = notional / e.perp
        funding = sum(r.rate * qty * r.perp for r in window[1:])
        borrow = sum(borrow_apr / (365.0 * 3) * qty * r.spot for r in window[1:])
        x = window[-1]
        basis = qty * ((e.perp - x.perp) - (e.spot - x.spot))
        net = funding + basis - borrow
        adverse = 100.0 * (max(r.perp_high for r in window[1:]) / e.perp - 1.0)
        bases = [(r.perp / r.spot - 1.0) * 1e4 for r in window]
        entry = {"name": name, "status": "MEASURED",
                 "window": f"{_iso_ms(e.ms)} .. {_iso_ms(x.ms)}",
                 "settlements": len(window),
                 "price_swing_pct": 100.0 * (max(r.perp_high for r in window)
                                             / min(r.perp_low for r in window)
                                             - 1.0),
                 "settlement_basis_min_bps": min(bases),
                 "settlement_basis_max_bps": max(bases),
                 "funding_min_bps": min(r.rate for r in window) * 1e4,
                 "negative_prints": sum(1 for r in window if r.rate < 0),
                 "max_adverse_short_4h_pct": adverse,
                 "attribution": {"funding_usd": funding, "basis_usd": basis,
                                 "borrow_usd": -borrow, "net_usd": net}}
        if mm is not None:
            entry["short_leg_isolated_survives_4h"] = {
                f"{lev}x": adverse < 100.0 * (1.0 / lev - mm)
                for lev in (1, 2, 3, 5, 10)}
        if day1m:
            one = _stress_1m(repo, day1m, e.perp)
            if one:
                entry["one_minute"] = one
        out.append(entry)
    return {"scenarios": out, "maintenance_margin_tier1": mm,
            "label": ("4h settlement closes and 4h highs. Close survival is NOT "
                      "wick survival: a liquidation engine sees every trade, "
                      "and a 1m high is still only a floor on the wick."),
            "corpus": meta["corpus"]}


def print_stress(s: Dict[str, Any]) -> None:
    print("  STRESS — a pair already on, held through each window")
    print(f"  {s['label']}")
    for sc in s["scenarios"]:
        if sc["status"] != "MEASURED":
            print(f"    {sc['name']:40s} {sc['status']}: {sc['reason']}")
            continue
        a = sc["attribution"]
        print(f"    {sc['name']:40s} swing {sc['price_swing_pct']:5.1f}%  "
              f"basis {sc['settlement_basis_min_bps']:+.1f}..{sc['settlement_basis_max_bps']:+.1f} bps  "
              f"neg prints {sc['negative_prints']}")
        print(f"      funding {a['funding_usd']:+,.2f} + basis {a['basis_usd']:+,.2f}"
              f" + borrow {a['borrow_usd']:+,.2f} = NET {a['net_usd']:+,.2f}"
              f"   short-leg 4h adverse {sc['max_adverse_short_4h_pct']:+.2f}%")
        if "short_leg_isolated_survives_4h" in sc:
            lv = sc["short_leg_isolated_survives_4h"]
            print("      isolated short survives the 4h high at: "
                  + " ".join(f"{k}={'yes' if v else 'NO'}" for k, v in lv.items()))
        if "one_minute" in sc:
            m = sc["one_minute"]
            print(f"      1m (Binance, cross-venue): high "
                  f"{m['max_1m_high_vs_window_entry_pct']:+.2f}% vs "
                  f"window entry, basis {m['basis_1m_min_bps']:+.1f}.."
                  f"{m['basis_1m_max_bps']:+.1f} bps over {m['minutes']} min")


def print_attribution(r: Dict[str, Any]) -> None:
    a = r["attribution"]
    print("  ATTRIBUTION  (costs negative; must sum, or the tool refuses)")
    print(f"    funding            ${a['funding_usd']:>12,.2f}")
    print(f"    basis              ${a['basis_usd']:>12,.2f}"
          f"   = spot leg {a['spot_leg_price_usd']:+,.2f}"
          f" + perp leg {a['perp_leg_price_usd']:+,.2f}")
    print(f"    fees               ${a['fees_usd']:>12,.2f}")
    print(f"    borrow             ${a['borrow_usd']:>12,.2f}")
    print(f"    impact             ${a['impact_usd']:>12,.2f}"
          + ("" if r["quotable_conditions"]["impact_haircut_applied"]
             else "   <- NOT MODELLED in this run"))
    print(f"    other              ${a['other_usd']:>12,.2f}")
    print(f"    {'-' * 40}")
    print(f"    = NET              ${a['net_usd']:>12,.2f}"
          f"   residual {a['residual_usd']:+.2e}")
    print(f"    closed trades {r['closed_trades']} of {r['trades']}: "
          f"${r['closed_net_usd']:,.2f} closed, "
          f"${r['open_at_end_marked_usd']:,.2f} marked at window end")


def print_quotable(r: Dict[str, Any]) -> None:
    print("  " + "!" * 66)
    print(f"  QUOTABLE: {r['is_a_quotable_return']}")
    for name in QUOTABLE_CONDITIONS:
        ok = r["quotable_conditions"][name]
        print(f"    [{'x' if ok else ' '}] {name}")
    if r.get("spot_source"):
        print(f"  spot source: {r['spot_source']}")
    for line in (r["why_not_quotable"] or "").split("; "):
        if line:
            print(f"    - {line}")
    print("  " + "!" * 66)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--notional", type=float, default=100_000.0)
    parser.add_argument("--entry-bps", type=float, default=ENTRY_FUNDING_BPS)
    parser.add_argument("--borrow-apr", type=float, default=BORROW_APR,
                        help="0.0 models a book that ALREADY OWNS the BTC and "
                             "is monetising it rather than financing it")
    parser.add_argument("--gated", action="store_true",
                        help="route entries through carry_costs.evaluate_entry")
    parser.add_argument("--matrix", action="store_true",
                        help="borrow x gate sweep — the table PHASE1_DECISION needs")
    parser.add_argument("--clock", choices=("daily", "8h"), default="daily",
                        help="daily closes (Binance corpora) or the funding "
                             "settlement clock 00/08/16 UTC")
    parser.add_argument("--venue", choices=("bybit", "binance"),
                        default="bybit",
                        help="8h clock only: bybit = the venue the broker "
                             "trades on (committed corpus); binance needs "
                             "tools/fetch_settlement_klines.py --write first")
    parser.add_argument("--no-impact", action="store_true",
                        help="8h clock only: drop the impact haircut (the "
                             "impact condition then reads unmet)")
    parser.add_argument("--stress", action="store_true",
                        help="8h clock only: print the named stress windows")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    if args.clock == "8h":
        return _main_settlement(args)

    if args.matrix:
        print("=" * 74)
        print("BORROW x GATE MATRIX — net %/yr  (daily closes: NOT QUOTABLE)")
        print("=" * 74)
        head = simulate(args.repo, notional=args.notional)
        print(f"  spot source: {head['spot_source']}")
        print(f"  quotable   : {head['is_a_quotable_return']}")
        print(f"  window     : {head['window']}\n")
        print(f"  {'borrow':>8} {'UNGATED':>18} {'GATED (in-sample)':>22}")
        for apr in (0.0, 0.03, 0.05, 0.08):
            u = simulate(args.repo, notional=args.notional, borrow_apr=apr)
            g = simulate(args.repo, notional=args.notional, borrow_apr=apr,
                         gated=True)
            print(f"  {apr*100:7.1f}% "
                  f"{u['net_annualised_pct']:+8.2f}%/yr n={u['trades']:<3d} "
                  f"{g['net_annualised_pct']:+8.2f}%/yr n={g['trades']:<3d}")
        print()
        print("  A gate that changes no trade is decoration. A gate that")
        print("  refuses everything is not trading, dressed as risk management.")
        print("  Read BOTH the return and the trade count.")
        return 0

    r = simulate(args.repo, notional=args.notional,
                 entry_bps=args.entry_bps, borrow_apr=args.borrow_apr,
                 gated=args.gated)

    print("=" * 74)
    print("CARRY BACKTEST — two legs, every cost  (clock: daily close)")
    print("=" * 74)
    print(f"  window            {r['window']}  ({r['years']:.2f} yr)")
    print(f"  notional          ${r['notional_usd']:,.0f}   borrow {r['borrow_apr']*100:.1f}%/yr"
          f"   rule {'GATED (in-sample)' if r['gated'] else 'UNGATED'}")
    print(f"  trades            {r['trades']}   "
          f"in market {r['market_exposure_pct']:.1f}% of days")
    print()
    print_attribution(r)
    print()
    print(f"  net annualised          {r['net_annualised_pct']:+.2f}%/yr")
    print(f"  naive funding sum       {r['naive_funding_sum_pct_per_yr']:+.2f}%/yr"
          "   <- income, not a return")
    print()
    print(f"  mean entry basis        {r['mean_entry_basis_bps']:+.0f} bps")
    print(f"  mean exit  basis        {r['mean_exit_basis_bps']:+.0f} bps")
    print(f"  losing trades           {r['losing_trades']} of {r['trades']}")
    print(f"  worst / best trade      ${r['worst_trade_usd']:,.0f} / "
          f"${r['best_trade_usd']:,.0f}")
    print()
    print_quotable(r)
    print("\n  CORPUS THIS WAS COMPUTED FROM")
    for series in r["corpus_health"]["series"]:
        print(f"    {series['name']:9s} n={series['rows']:5d}  "
              f"last {series['last']}  sha {series['sha256'][:16]}…")
    if r["corpus_health"]["interior_holes"]:
        print(f"    ! {len(r['corpus_health']['interior_holes'])} interior "
              "hole(s) — days one series has and another does not")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(r, handle, indent=2)
    return 0


def _main_settlement(args) -> int:
    impact = not args.no_impact
    if args.matrix:
        print("=" * 74)
        print("BORROW x GATE MATRIX — net %/yr  (clock: settlement 00/08/16 UTC)")
        print("=" * 74)
        head = simulate_settlement(args.repo, venue=args.venue,
                                   notional=args.notional, impact=impact)
        print(f"  source     : {head['spot_source']}")
        print(f"  window     : {head['window']}")
        print(f"  impact     : {head['impact_bps_per_leg']:.2f} bps/leg"
              f"   quotable: {head['is_a_quotable_return']}\n")
        print(f"  {'borrow':>8} {'UNGATED':>18} {'GATED (in-sample)':>22}")
        for apr in (0.0, 0.03, 0.05, 0.08):
            u = simulate_settlement(args.repo, venue=args.venue,
                                    notional=args.notional, borrow_apr=apr,
                                    impact=impact)
            g = simulate_settlement(args.repo, venue=args.venue,
                                    notional=args.notional, borrow_apr=apr,
                                    gated=True, impact=impact)
            print(f"  {apr*100:7.1f}% "
                  f"{u['net_annualised_pct']:+8.2f}%/yr n={u['trades']:<3d} "
                  f"{g['net_annualised_pct']:+8.2f}%/yr n={g['trades']:<3d}")
        print("\n  NOT QUOTABLE: ungated is not the engine's rule; gated is "
              "in-sample.")
        return 0

    r = simulate_settlement(args.repo, venue=args.venue,
                            notional=args.notional, entry_bps=args.entry_bps,
                            borrow_apr=args.borrow_apr, gated=args.gated,
                            impact=impact)
    print("=" * 74)
    print("CARRY BACKTEST — two legs, every cost  "
          "(clock: settlement 00/08/16 UTC)")
    print("=" * 74)
    print(f"  source            {r['spot_source']}")
    print(f"  window            {r['window']}  ({r['years']:.2f} yr, "
          f"{r['settlements']} prints)")
    print(f"  notional          ${r['notional_usd']:,.0f}   borrow "
          f"{r['borrow_apr']*100:.1f}%/yr   rule "
          f"{'GATED (in-sample)' if r['gated'] else 'UNGATED'}")
    if impact:
        d = r["impact_detail"]
        print(f"  impact haircut    {r['impact_bps_per_leg']:.3f} bps/leg = "
              f"floor {d['floor_bps']:.2f} (DECLARED) + walk {d['walk_bps']:.3f}"
              f" (thinnest 1% band ${d['thinnest_1pct_band_usd']/1e6:,.0f}M)")
    print(f"  trades            {r['trades']}   in market "
          f"{r['market_exposure_pct']:.1f}% of the window")
    print()
    print_attribution(r)
    print()
    print(f"  net annualised          {r['net_annualised_pct']:+.2f}%/yr")
    print(f"  naive funding sum       {r['naive_funding_sum_pct_per_yr']:+.2f}%/yr"
          "   <- income, not a return")
    print(f"  mean entry / exit basis {r['mean_entry_basis_bps']:+.1f} / "
          f"{r['mean_exit_basis_bps']:+.1f} bps  (at settlement)")
    print(f"  losing trades           {r['losing_trades']} of {r['trades']}")
    print(f"  worst / best trade      ${r['worst_trade_usd']:,.0f} / "
          f"${r['best_trade_usd']:,.0f}")
    print(f"  worst short-leg adverse {r['max_adverse_short_excursion_pct']:+.2f}%"
          f"  ({r['wick_note']})")
    print()
    print_quotable(r)
    if args.stress:
        print()
        print_stress(stress_scenarios(args.repo, notional=args.notional,
                                      borrow_apr=args.borrow_apr))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(r, handle, indent=2, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
