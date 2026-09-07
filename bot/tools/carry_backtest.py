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

THE DATA PROBLEM, STATED LOUDLY
===============================
This repo has NO BINANCE BTC SPOT SERIES. The only BTC spot available is
BITSTAMP_SPOT_BTC_USD_1D, which differs from the Binance USDT-M perp in two
ways that both land directly on the basis term:

  VENUE     Bitstamp is not Binance. Cross-venue spread is real and moves.
  CURRENCY  USD is not USDT. USDT has depegged before — roughly 5% in
            March 2023 — and every one of those basis points would be
            attributed to carry P&L by this simulation.

So the basis number here is a PROXY and the tool says so in its own output. It
is DIAGNOSTIC, not a return you may quote to anyone. Fetching Binance BTC spot
1d is the single highest-value data task in the project and it is one call to
the same endpoint the corpus already uses.

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
try:
    import reserved_holdout as _read_ledger
    _read_ledger.install()
except Exception as _ledger_exc:  # noqa: BLE001 - never blocks a measurement
    _read_ledger = None
    _READ_LEDGER_UNAVAILABLE = repr(_ledger_exc)


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
    reason: str = ""

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
    def basis_pnl(self) -> float:
        """Short perp + long spot. You earn the basis you SHED between entry
        and exit: enter wide, exit narrow, and the convergence pays you. Enter
        narrow, exit wide, and it costs you."""
        return self.qty * ((self.entry_perp - self.exit_perp)
                           - (self.entry_spot - self.exit_spot))

    @property
    def net(self) -> float:
        return self.funding + self.basis_pnl - self.fees - self.borrow

    @property
    def days(self) -> int:
        return (self.closed - self.opened).days if self.closed else 0


def load_series(path: str) -> Dict[dt.date, float]:
    with _open(path) as handle:
        return {_day(r["time_period_start"]): float(r["price_close"])
                for r in csv.DictReader(handle)}


def load_funding(path: str):
    with _open(path) as handle:
        rows = list(csv.DictReader(handle))
    return ([int(r["funding_time_ms"]) for r in rows],
            [float(r["funding_rate"]) for r in rows])


def simulate(repo: str, *, notional: float = 100_000.0,
             entry_bps: float = ENTRY_FUNDING_BPS,
             borrow_apr: float = BORROW_APR) -> Dict[str, Any]:
    perp = load_series(os.path.join(
        repo, "data/real_linear_1d/ohlcv/BINANCE_LINEAR_BTC_USDT_1D.csv.gz"))
    spot = load_series(os.path.join(
        repo, "data/real_1d/ohlcv/BITSTAMP_SPOT_BTC_USD_1D.csv.gz"))
    ftimes, frates = load_funding(os.path.join(
        repo, "data/real_funding/funding/BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz"))

    days = sorted(set(perp) & set(spot))
    if not days:
        raise SystemExit("no overlapping days between spot and perp")

    trades: List[Trade] = []
    live: Optional[Trade] = None
    negative_streak = 0
    round_trip_bps = 2 * TAKER_BPS_SPOT + 2 * TAKER_BPS_PERP

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

        if last_bps >= entry_bps:
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
        trades.append(live)

    years = (days[-1] - days[0]).days / 365.25
    total = {k: sum(getattr(t, k) for t in trades)
             for k in ("funding", "fees", "borrow")}
    total["basis_pnl"] = sum(t.basis_pnl for t in trades)
    total["net"] = sum(t.net for t in trades)
    held = sum(t.days for t in trades)

    # Naive figure this replaces, for direct contrast.
    naive = sum(frates) * 100.0

    return {
        "window": f"{days[0]} .. {days[-1]}",
        "years": years,
        "notional_usd": notional,
        "trades": len(trades),
        "days_in_market": held,
        "market_exposure_pct": 100.0 * held / max(1, (days[-1] - days[0]).days),
        "gross_funding_usd": total["funding"],
        "basis_pnl_usd": total["basis_pnl"],
        "fees_usd": total["fees"],
        "borrow_usd": total["borrow"],
        "net_usd": total["net"],
        "net_return_pct": 100.0 * total["net"] / notional,
        "net_annualised_pct": (100.0 * total["net"] / notional) / years,
        "naive_funding_sum_pct_per_yr": naive / years,
        "round_trip_bps": round_trip_bps,
        "borrow_apr": borrow_apr,
        "mean_entry_basis_bps": statistics.mean(
            t.entry_basis_bps for t in trades) if trades else 0.0,
        "mean_exit_basis_bps": statistics.mean(
            t.exit_basis_bps for t in trades) if trades else 0.0,
        "worst_trade_usd": min((t.net for t in trades), default=0.0),
        "best_trade_usd": max((t.net for t in trades), default=0.0),
        "losing_trades": sum(1 for t in trades if t.net < 0),
        "spot_proxy": "BITSTAMP_SPOT_BTC_USD (NOT Binance, NOT USDT)",
        "basis_is_a_proxy": True,
        "is_a_quotable_return": False,
        "why_not_quotable": (
            "the spot leg is priced on a DIFFERENT VENUE in a DIFFERENT "
            "CURRENCY, so the basis term carries Bitstamp-vs-Binance spread "
            "and USD-vs-USDT depeg. Fetch Binance BTCUSDT spot 1d before "
            "quoting any figure from this tool."),
        "not_modelled": ["slippage beyond taker fees", "order book depth",
                         "intraday basis", "liquidation of the short leg"],
        "trade_log": [{"opened": str(t.opened), "closed": str(t.closed),
                       "days": t.days, "funding": round(t.funding, 2),
                       "basis": round(t.basis_pnl, 2), "fees": round(t.fees, 2),
                       "borrow": round(t.borrow, 2), "net": round(t.net, 2),
                       "entry_basis_bps": round(t.entry_basis_bps, 1),
                       "exit_basis_bps": round(t.exit_basis_bps, 1),
                       "reason": t.reason} for t in trades],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--notional", type=float, default=100_000.0)
    parser.add_argument("--entry-bps", type=float, default=ENTRY_FUNDING_BPS)
    parser.add_argument("--borrow-apr", type=float, default=BORROW_APR,
                        help="0.0 models a book that ALREADY OWNS the BTC and "
                             "is monetising it rather than financing it")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    r = simulate(args.repo, notional=args.notional,
                 entry_bps=args.entry_bps, borrow_apr=args.borrow_apr)

    print("=" * 74)
    print("CARRY BACKTEST — two legs, every cost")
    print("=" * 74)
    print(f"  window            {r['window']}  ({r['years']:.2f} yr)")
    print(f"  notional          ${r['notional_usd']:,.0f}   borrow {r['borrow_apr']*100:.1f}%/yr")
    print(f"  trades            {r['trades']}   "
          f"in market {r['market_exposure_pct']:.1f}% of days")
    print()
    print("  P&L DECOMPOSITION")
    print(f"    funding received   ${r['gross_funding_usd']:>12,.0f}")
    print(f"    basis              ${r['basis_pnl_usd']:>12,.0f}   "
          "<- the term the naive figure ignores")
    print(f"    fees               ${-r['fees_usd']:>12,.0f}")
    print(f"    borrow             ${-r['borrow_usd']:>12,.0f}")
    print(f"    {'-' * 40}")
    print(f"    NET                ${r['net_usd']:>12,.0f}   "
          f"= {r['net_return_pct']:+.2f}% over the window")
    print()
    print(f"  net annualised          {r['net_annualised_pct']:+.2f}%/yr")
    print(f"  naive funding sum       {r['naive_funding_sum_pct_per_yr']:+.2f}%/yr"
          "   <- the figure this replaces")
    print()
    print(f"  mean entry basis        {r['mean_entry_basis_bps']:+.0f} bps")
    print(f"  mean exit  basis        {r['mean_exit_basis_bps']:+.0f} bps")
    print(f"  losing trades           {r['losing_trades']} of {r['trades']}")
    print(f"  worst / best trade      ${r['worst_trade_usd']:,.0f} / "
          f"${r['best_trade_usd']:,.0f}")
    print()
    print("  " + "!" * 66)
    print(f"  QUOTABLE: {r['is_a_quotable_return']}")
    print(f"  spot proxy: {r['spot_proxy']}")
    for line in r["why_not_quotable"].split(", "):
        print(f"    {line}")
    print("  " + "!" * 66)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(r, handle, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
