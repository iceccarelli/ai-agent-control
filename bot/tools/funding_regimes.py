#!/usr/bin/env python3
"""What the market paid, quarter by quarter, and where a replay's profit was earned.

A carry book's return is mostly the funding level it happened to sit through. A
single headline hides that, so this reads the settlement corpus and says, for
each calendar quarter, the mean funding (annualised, long pays short), the share
of positive prints, and which RANK-THIRD of the quarters it falls in. The thirds
come from the quarters themselves, so there is no threshold to tune. Given a
replay's JSON it then spreads each trade's net over the days it was held and
reports how much was earned in each third.

DESCRIPTIVE AND IN-SAMPLE. It describes a corpus that was already seen; it does
not predict the next regime, it does not select a rule, and it changes nothing.
Reads files only. No exchange, no order.

    python3 tools/carry_replay.py --repo . --json /tmp/replay.json
    python3 tools/funding_regimes.py --repo . --replay-json /tmp/replay.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

PRINTS_PER_DAY = 3
DAY_MS = 86_400_000


def _quarter(ms: int) -> str:
    d = dt.datetime.fromtimestamp(ms / 1000.0, dt.timezone.utc)
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def quarterly_table(rows: Sequence[Any]) -> List[Dict[str, Any]]:
    """One row per calendar quarter, in time order."""
    buckets: Dict[str, List[float]] = {}
    for r in rows:
        buckets.setdefault(_quarter(int(r.ms)), []).append(float(r.rate))
    table = []
    for quarter in sorted(buckets):
        rates = buckets[quarter]
        table.append({
            "quarter": quarter, "prints": len(rates),
            "mean_annualised_pct": 100.0 * PRINTS_PER_DAY * 365.0 * sum(rates) / len(rates),
            "pct_positive": 100.0 * sum(1 for x in rates if x > 0) / len(rates),
        })
    return table


def with_terciles(table: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Label each quarter low / mid / high by the RANK of its mean funding among
    the quarters. Ties break by time order, so the labelling is deterministic."""
    order = sorted(range(len(table)),
                   key=lambda i: (table[i]["mean_annualised_pct"], i))
    n = len(table)
    label = {}
    for rank, i in enumerate(order):
        label[i] = "low" if rank < n / 3.0 else ("mid" if rank < 2 * n / 3.0 else "high")
    return [dict(row, third=label[i]) for i, row in enumerate(table)]


def allocate_to_quarters(trades: Sequence[Dict[str, Any]],
                         nets: Sequence[float]) -> Dict[str, float]:
    """Spread each trade's net evenly over the calendar days it was held.

    An APPROXIMATION, stated as one: the engine does not record net per print,
    so a trade's net is assumed uniform across its holding period. The total is
    preserved exactly."""
    out: Dict[str, float] = {}
    for trade, net in zip(trades, nets):
        lo, hi = int(trade["opened_ms"]), int(trade["closed_ms"])
        days = max((hi - lo) / DAY_MS, 1e-9)
        t = lo
        while t < hi:
            day_end = min(hi, (t // DAY_MS + 1) * DAY_MS)
            q = _quarter(t)
            out[q] = out.get(q, 0.0) + net * ((day_end - t) / DAY_MS) / days
            t = day_end
        if hi <= lo:                      # a same-instant trade: all in one quarter
            q = _quarter(lo)
            out[q] = out.get(q, 0.0) + net
    return out


def attribute_by_third(table: Sequence[Dict[str, Any]],
                       trades: Sequence[Dict[str, Any]],
                       nets: Sequence[float]) -> Dict[str, Any]:
    third_of = {row["quarter"]: row["third"] for row in table}
    by_quarter = allocate_to_quarters(trades, nets)
    by_third = {"low": 0.0, "mid": 0.0, "high": 0.0}
    unplaced = 0.0
    for quarter, net in by_quarter.items():
        if quarter in third_of:
            by_third[third_of[quarter]] += net
        else:
            unplaced += net
    total = sum(nets)
    share = (by_third["high"] / total) if (nets and total > 0) else None
    # How much of each third the book was in the market, and what it earned per
    # day there: separates "was in more often" from "earned more when in".
    held_days = [max((int(t["closed_ms"]) - int(t["opened_ms"])) / DAY_MS, 0.0)
                 for t in trades]
    days_by_quarter = allocate_to_quarters(trades, held_days)
    days_by_third = {"low": 0.0, "mid": 0.0, "high": 0.0}
    for quarter, d in days_by_quarter.items():
        if quarter in third_of:
            days_by_third[third_of[quarter]] += d
    observed = {"low": 0.0, "mid": 0.0, "high": 0.0}
    for row in table:
        observed[row["third"]] += row["prints"] / float(PRINTS_PER_DAY)
    return {"net_by_quarter": by_quarter, "net_by_third": by_third,
            "net_outside_corpus": unplaced, "total_net": total,
            "share_in_high_third": share,
            "days_by_third": days_by_third,
            "pct_of_days_in_market_by_third": {
                k: (100.0 * days_by_third[k] / observed[k] if observed[k] else None)
                for k in days_by_third},
            "net_per_day_by_third": {
                k: (by_third[k] / days_by_third[k] if days_by_third[k] > 1e-9 else None)
                for k in days_by_third}}


def latest_window(rows: Sequence[Any], prints_back: int = 270) -> Dict[str, Any]:
    """The most recent `prints_back` prints (270 is about 90 days)."""
    tail = list(rows)[-prints_back:]
    if not tail:
        return {"prints": 0, "mean_annualised_pct": None}
    return {"prints": len(tail), "from_ms": int(tail[0].ms), "to_ms": int(tail[-1].ms),
            "mean_annualised_pct": 100.0 * PRINTS_PER_DAY * 365.0
            * sum(float(r.rate) for r in tail) / len(tail)}


def report_lines(table: Sequence[Dict[str, Any]], latest: Dict[str, Any],
                 attribution: Optional[Dict[str, Any]]) -> List[str]:
    lines = ["quarter   prints  mean funding     positive  third"]
    for row in table:
        lines.append(f"{row['quarter']}  {row['prints']:6d}  "
                     f"{row['mean_annualised_pct']:+8.2f}%/yr  "
                     f"{row['pct_positive']:6.1f}%  {row['third']}")
    means = sorted(r["mean_annualised_pct"] for r in table)
    if means:
        lines.append(f"quarters: {len(means)}; funding min {means[0]:+.1f}, median "
                     f"{means[len(means) // 2]:+.1f}, max {means[-1]:+.1f} %/yr; "
                     f"above 10%/yr in {sum(1 for m in means if m > 10)} of {len(means)}")
    if latest.get("mean_annualised_pct") is not None:
        lines.append(f"latest {latest['prints']} prints (about "
                     f"{latest['prints'] / PRINTS_PER_DAY:.0f} days): "
                     f"{latest['mean_annualised_pct']:+.2f}%/yr")
    if attribution is not None:
        t = attribution["net_by_third"]
        lines.append(f"replay net by third of quarters: low ${t['low']:,.0f}, "
                     f"mid ${t['mid']:,.0f}, high ${t['high']:,.0f}")
        if attribution["share_in_high_third"] is not None:
            lines.append(f"  {100.0 * attribution['share_in_high_third']:.1f}% of "
                         "the net was earned in the highest third of quarters")
        pct, per = attribution["pct_of_days_in_market_by_third"], attribution["net_per_day_by_third"]
        for k in ("low", "mid", "high"):
            rate = "n/a" if per[k] is None else f"${per[k]:,.2f} per day"
            share = "n/a" if pct[k] is None else f"{pct[k]:.0f}%"
            lines.append(f"  {k:4s} third: in the market {share} of the days; "
                         f"{rate} in the market")
        lines.append("  (each trade's net is spread evenly over the days it was held)")
    lines.append("DESCRIPTIVE, IN-SAMPLE: this describes a corpus already seen. It "
                 "is not a forecast and not a rule.")
    return lines


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=os.path.dirname(HERE),
                    help="the bot/ directory holding data/ (default: this checkout)")
    ap.add_argument("--replay-json",
                    help="a `carry_replay.py --json` file, to attribute its trades")
    args = ap.parse_args(argv)
    import carry_backtest as cb
    rows = cb.load_bybit_settlements(args.repo)[0]
    table = with_terciles(quarterly_table(rows))
    attribution = None
    if args.replay_json:
        with open(args.replay_json, encoding="utf-8") as handle:
            rep = json.load(handle)
        conc = rep.get("concentration") or {}
        trades = sorted(rep.get("trade_rows", []),
                        key=lambda t: (int(t["closed_ms"]), int(t["opened_ms"])))
        nets = conc.get("trade_nets_usd", [])
        if len(trades) != len(nets):
            print("refusing: the replay JSON has no per-trade nets matching its "
                  "trades (re-run carry_replay.py --json)", file=sys.stderr)
            return 3
        attribution = attribute_by_third(table, trades, nets)
    print("\n".join(report_lines(table, latest_window(rows), attribution)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
