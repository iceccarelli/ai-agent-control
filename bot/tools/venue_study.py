#!/usr/bin/env python3
"""Three questions the repo could not answer without exchange data. Read-only.

1. VENUE MISMATCH — every corpus is Binance; CarryBroker places orders on Bybit.
   Funding differs between venues, and if it differs by more than the edge, the
   whole backtest describes a trade you are not going to make.

2. CAPACITY — "$100 is a ruler" has been in this repo since slice 1 with no
   number behind it. Where does edge_bps - impact_bps reach zero?

3. STRESS — what a hedged pair feels when price moves 3-4% in a day. This is
   the question that decides whether leverage on the spread is sane.

Data lives in data/exchange_study/. It is a STUDY SET, fetched once and frozen,
not a corpus the book trades from. Nothing here feeds a gate.

    python3 tools/venue_study.py --repo .
"""
from __future__ import annotations

import argparse
import bisect
import collections
import csv
import datetime as dt
import gzip
import json
import os
import statistics
import sys
from typing import Any, Dict, List

#: NOT under data/. data/ holds CORPORA the book trades from, and
#: tests/test_market_data.py requires every file there to be in a MANIFEST.
#: This is a STUDY SET: fetched once, frozen, feeds no gate and no backtest.
#: Filing it as a corpus would claim a status it does not have.
STUDY = "research/exchange_study"
#: Edge per pair, derived: 9.70%/yr unfinanced x 3.98 yr / 18 trades.
#: Unfinanced on purpose — the financed case has no capacity question because
#: it has no edge to spend on impact.
EDGE_BPS_PER_PAIR = 214.0
#: A pair is four legs: buy spot, sell perp, then reverse both.
LEGS_PER_ROUND_TRIP = 4


def _load(path: str):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as handle:
        return json.load(handle)


def venue_funding(repo: str) -> Dict[str, Any]:
    """Bybit funding against Binance funding, matched print by print."""
    bypath = os.path.join(repo, STUDY, "bybit", "funding_BTCUSDT.json.gz")
    if not os.path.exists(bypath):
        return {"available": False, "reason": "no bybit funding in the study set"}
    bybit = {int(r["fundingRateTimestamp"]): float(r["fundingRate"])
             for r in _load(bypath)}
    bnpath = os.path.join(
        repo, "data/real_funding/funding/BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz")
    with gzip.open(bnpath, "rt") as handle:
        binance = {int(r["funding_time_ms"]): float(r["funding_rate"])
                   for r in csv.DictReader(handle)}

    keys = sorted(binance)
    pairs = []
    for when, rate in bybit.items():
        i = bisect.bisect_left(keys, when)
        for j in (i - 1, i):
            if 0 <= j < len(keys) and abs(keys[j] - when) < 300_000:
                pairs.append((rate * 1e4, binance[keys[j]] * 1e4))
                break
    if not pairs:
        return {"available": False, "reason": "no matched prints"}

    by = [p[0] for p in pairs]
    bn = [p[1] for p in pairs]
    diff = [a - b for a, b in zip(by, bn)]
    ann = lambda m: m / 1e4 * 3 * 365 * 100      # noqa: E731

    return {
        "available": True, "matched_prints": len(pairs),
        "bybit": {"mean_bps": statistics.mean(by),
                  "stdev_bps": statistics.pstdev(by),
                  "pct_positive": 100 * sum(1 for x in by if x > 0) / len(by),
                  "annualised_pct": ann(statistics.mean(by))},
        "binance": {"mean_bps": statistics.mean(bn),
                    "stdev_bps": statistics.pstdev(bn),
                    "pct_positive": 100 * sum(1 for x in bn if x > 0) / len(bn),
                    "annualised_pct": ann(statistics.mean(bn))},
        "difference": {"mean_bps": statistics.mean(diff),
                       "annualised_pct": ann(statistics.mean(diff)),
                       "bybit_richer_pct": 100 * sum(1 for x in diff if x > 0) / len(diff)},
        "reading": ("if the annualised difference is small relative to the edge, "
                    "a Binance-derived backtest describes the Bybit trade well "
                    "enough to act on. If it is not, the backtest is about a "
                    "trade you are not going to make."),
    }


def capacity(repo: str) -> Dict[str, Any]:
    path = os.path.join(repo, STUDY, "binance_bookdepth_median.json")
    if not os.path.exists(path):
        return {"available": False, "reason": "no depth summary in the study set"}
    data = json.load(open(path, encoding="utf-8"))
    per_day = {}
    for day, bands in data["days"].items():
        one = float(bands.get("1.0", 0.0))
        # Walking to depth d fills at roughly d/2 from mid, so the 1% band costs
        # ~50 bps per leg. Four legs is ~200 bps, which is the whole edge.
        per_day[day] = {"band_1pct_usd": one,
                        "round_trip_bps_at_that_size": 0.5 * 100 * LEGS_PER_ROUND_TRIP,
                        "edge_left_bps": EDGE_BPS_PER_PAIR - 200.0}
    sizes = [v["band_1pct_usd"] for v in per_day.values() if v["band_1pct_usd"]]
    return {
        "available": True, "edge_bps_per_pair": EDGE_BPS_PER_PAIR,
        "per_day": per_day,
        "thinnest_1pct_usd": min(sizes), "deepest_1pct_usd": max(sizes),
        "regime_spread_x": max(sizes) / min(sizes),
        "cannot_resolve": ("bookDepth bands are 1% granularity, so impact BELOW "
                           "1% is unresolvable. Sizing between $100k and $10m "
                           "needs L1/L2 tick data — a different dataset."),
        "reading": ("capacity is not a number, it is a number PER REGIME. The "
                    "book must size to the thin one."),
    }


def stress(repo: str) -> Dict[str, Any]:
    out: List[Dict[str, Any]] = []
    folder = os.path.join(repo, STUDY, "stress")
    if not os.path.isdir(folder):
        return {"available": False, "reason": "no stress set"}
    days = sorted({f.split("_1m_")[1].split(".")[0]
                   for f in os.listdir(folder) if "_1m_" in f})
    for day in days:
        try:
            perp = _load(os.path.join(folder, f"perp_1m_{day}.json.gz"))
            spot = _load(os.path.join(folder, f"spot_1m_{day}.json.gz"))
        except FileNotFoundError:
            continue
        p = {int(k[0]): float(k[4]) for k in perp}
        s = {int(k[0]): float(k[4]) for k in spot}
        common = sorted(set(p) & set(s))
        if not common:
            continue
        basis = [(p[t] / s[t] - 1.0) * 1e4 for t in common]
        px = [p[t] for t in common]
        out.append({
            "day": day, "minutes": len(common),
            "price_swing_pct": 100 * (max(px) / min(px) - 1.0),
            "basis_mean_bps": statistics.mean(basis),
            "basis_stdev_bps": statistics.pstdev(basis),
            "basis_range_bps": max(basis) - min(basis),
        })
    return {"available": True, "days": out,
            "reading": ("a hedged pair's P&L moves with the BASIS, not the "
                        "price. If price swings 4% and the basis moves 6 bps, "
                        "the hedge is doing exactly what it exists to do — and "
                        "that is the argument for leverage on the SPREAD that "
                        "does not exist for leverage on a view.")}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    v, c, s = (venue_funding(args.repo), capacity(args.repo), stress(args.repo))

    print("=" * 74)
    print("VENUE STUDY — three questions the repo could not answer")
    print("=" * 74)

    print("\n1. VENUE MISMATCH  (orders go to Bybit; every corpus is Binance)")
    if v["available"]:
        print(f"   {v['matched_prints']} matched funding prints")
        print(f"   {'':14s}{'BYBIT':>10s}{'BINANCE':>10s}")
        for label, key in (("mean bps", "mean_bps"), ("stdev", "stdev_bps"),
                           ("% positive", "pct_positive"),
                           ("annualised %", "annualised_pct")):
            print(f"   {label:14s}{v['bybit'][key]:>10.3f}{v['binance'][key]:>10.3f}")
        print(f"   difference: {v['difference']['annualised_pct']:+.2f} %/yr")
    else:
        print(f"   {v['reason']}")

    print("\n2. CAPACITY  (where edge_bps - impact_bps = 0)")
    if c["available"]:
        print(f"   edge {c['edge_bps_per_pair']:.0f} bps per pair, four legs per round trip")
        print(f"   thinnest 1% band ${c['thinnest_1pct_usd']/1e6:.0f}M   "
              f"deepest ${c['deepest_1pct_usd']/1e6:.0f}M   "
              f"spread {c['regime_spread_x']:.1f}x")
        print(f"   {c['reading']}")
        print(f"   LIMIT: {c['cannot_resolve']}")
    else:
        print(f"   {c['reason']}")

    print("\n3. STRESS  (what a hedged pair feels on a violent day)")
    if s["available"]:
        for d in s["days"]:
            print(f"   {d['day']}  price {d['price_swing_pct']:5.1f}%  "
                  f"basis range {d['basis_range_bps']:5.1f} bps  "
                  f"sd {d['basis_stdev_bps']:.1f}")
        print(f"   {s['reading']}")
    else:
        print(f"   {s['reason']}")

    if args.out:
        json.dump({"venue": v, "capacity": c, "stress": s},
                  open(args.out, "w", encoding="utf-8"), indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
