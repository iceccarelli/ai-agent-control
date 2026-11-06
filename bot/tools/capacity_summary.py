#!/usr/bin/env python3
"""What the recorded MAINNET market says about the carry, in the numbers a decision needs.

    python3 tools/capacity_summary.py --dir /data/market [--hold-settlements 21] [--json]
    fly ssh console -a carry-drill -C "python3 tools/capacity_summary.py --dir /data/market"

Reads the ``capacity-YYYYMMDD.jsonl`` rows the market recorder writes (public mainnet data;
nothing else, no network, no credential) and answers:

  * how long and how densely the market was observed;
  * the venue's funding FORECAST per settlement on MAINNET: how often it is positive, and
    how often the forecast PROJECTION covers the ESTIMATED cost of a round trip held
    ``--hold-settlements`` settlements (a projection, never a realised outcome);
  * the round-trip cost at each trade size (book + published taker fees, a lower bound),
    and the settlements needed to break even at the window's mean forecast;
  * the largest size the displayed book could fill ("book-fillable snapshots": displayed, not executable).

THE STRICT, INTEGRITY-CHECKED VIEW is ``tools/profitability_report.py``; this is the quick view and
treats every row it can read as a row.

THIS IS DESCRIPTIVE AND IN-SAMPLE. The forecast is the venue's estimate for the NEXT
settlement, not a settled payment; it is sampled from the same window it is summarised over.
Nothing here is an out-of-sample result, a return, or a reason to deploy capital: the
verdict belongs to the pre-declared forward evidence window. It exists so that the choice of
size, fee tier and hold is made from recorded market numbers, not from the testnet's.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence

SETTLEMENTS_PER_DAY = 3.0          # Bybit BTCUSDT linear: every 8 hours


def load_rows(directory: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in sorted(glob.glob(os.path.join(directory, "capacity-*.jsonl"))):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue                       # a torn last line must not hide the rest
                if isinstance(r, dict) and "curve" in r and "at_ms" in r:
                    rows.append(r)
    rows.sort(key=lambda r: r["at_ms"])
    return rows


def _q(xs: Sequence[float], p: float) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))]


def summarise(rows: Sequence[Dict[str, Any]], *, hold_settlements: float = 21.0) -> Dict[str, Any]:
    if not rows:
        return {"samples": 0, "note": "no capacity rows: the recorder has not written any yet"}
    first, last = rows[0]["at_ms"], rows[-1]["at_ms"]
    fc = [r["funding_forecast_bps_per_settlement"] for r in rows
          if isinstance(r.get("funding_forecast_bps_per_settlement"), (int, float))]
    mean_f = statistics.fmean(fc) if fc else None
    sizes: Dict[float, List[Dict[str, Any]]] = {}
    for r in rows:
        for p in r["curve"]:
            sizes.setdefault(float(p["notional_usd"]), []).append(p)
    table = []
    for n in sorted(sizes):
        pts = sizes[n]
        ok = [p for p in pts if p.get("status") == "MEASURED"]
        costs = [p["total_round_trip_bps"] for p in ok]
        row: Dict[str, Any] = {"notional_usd": n, "book_fillable_snapshot_share": len(ok) / len(pts)}
        if costs:
            c = statistics.fmean(costs)
            row.update(mean_total_round_trip_bps=c, p95_total_round_trip_bps=_q(costs, 0.95),
                       fee_round_trip_bps=ok[0].get("fee_round_trip_bps"))
            if mean_f and mean_f > 0:
                row["breakeven_settlements_at_mean_forecast"] = c / mean_f
                row["net_bps_after_hold_at_mean_forecast"] = hold_settlements * mean_f - c
            share = None
            if fc:
                share = sum(1 for f in fc if f * hold_settlements >= c) / len(fc)
            row["share_of_snapshots_where_forecast_projection_covers_estimated_cost"] = share
        table.append(row)
    return {
        "samples": len(rows), "window_hours": round((last - first) / 3_600_000.0, 2),
        "first_ms": first, "last_ms": last,
        "environment": rows[-1].get("environment"), "fee_source": rows[-1].get("fee_source"),
        "funding_forecast_bps_per_settlement": None if not fc else {
            "mean": mean_f, "median": statistics.median(fc), "min": min(fc), "max": max(fc),
            "share_positive": sum(1 for f in fc if f > 0) / len(fc)},
        "hold_settlements": hold_settlements, "hold_days": hold_settlements / SETTLEMENTS_PER_DAY,
        "by_size": table,
        "max_displayed_fillable_notional_usd": max((r.get("max_displayed_fillable_notional_usd") or 0.0) for r in rows) or None,
        "caveats": ["descriptive and in-sample; the forecast is the venue's estimate, not a payment",
                    "cost is a lower bound: latency, borrow and the account's real fee tier are UNKNOWN",
                    "a short window says nothing about a regime; this is not an out-of-sample result"],
    }


def regime(rows: Sequence[Dict[str, Any]], *, hurdle_apr_pct: Optional[float], hold_settlements: float = 21.0,
           notional_usd: float = 100_000.0, min_hours: float = 72.0) -> Dict[str, Any]:
    """Is the recorded mainnet funding regime paying more than the owner's hurdle, after the
    cost of getting in and out, annualised? A verdict about the WINDOW, not a forecast.

        gross APR   = mean forecast (bps / settlement) x 3 x 365
        cost APR    = round-trip cost at `notional_usd` x (365 / hold days)   [paid once per hold]
        net APR     = gross - cost
        OPEN        net APR >= hurdle      CLOSED  net APR < hurdle
        INSUFFICIENT the window is shorter than `min_hours`, or the hurdle was not supplied

    The hurdle is the owner's (what the same capital earns elsewhere, plus the premium for the
    risks the tool cannot see). Nothing here chooses it."""
    if not rows:
        return {"verdict": "INSUFFICIENT", "why": "no capacity rows"}
    hours = (rows[-1]["at_ms"] - rows[0]["at_ms"]) / 3_600_000.0
    fc = [r["funding_forecast_bps_per_settlement"] for r in rows
          if isinstance(r.get("funding_forecast_bps_per_settlement"), (int, float))]
    costs = []
    for r in rows:
        for p in r["curve"]:
            if float(p["notional_usd"]) == float(notional_usd) and p.get("status") == "MEASURED":
                costs.append(p["total_round_trip_bps"])
    if not fc or not costs:
        return {"verdict": "INSUFFICIENT", "why": f"no measured cost / forecast at ${notional_usd:,.0f}",
                "window_hours": round(hours, 2)}
    mean_f, cost = statistics.fmean(fc), statistics.fmean(costs)
    hold_days = hold_settlements / SETTLEMENTS_PER_DAY
    gross_apr = mean_f * SETTLEMENTS_PER_DAY * 365.0 / 100.0
    cost_apr = cost * (365.0 / hold_days) / 100.0
    net_apr = gross_apr - cost_apr
    out: Dict[str, Any] = {
        "window_hours": round(hours, 2), "notional_usd": notional_usd, "hold_days": hold_days,
        "mean_forecast_bps_per_settlement": mean_f, "mean_round_trip_cost_bps": cost,
        "gross_apr_pct": gross_apr, "cost_apr_pct": cost_apr, "net_apr_pct": net_apr,
        "hurdle_apr_pct": hurdle_apr_pct, "min_hours": min_hours}
    if hurdle_apr_pct is None:
        out.update(verdict="INSUFFICIENT", why="no hurdle supplied (--hurdle-apr): the tool does not choose one")
    elif hours < min_hours:
        out.update(verdict="INSUFFICIENT", why=f"window {hours:.1f} h is shorter than {min_hours:g} h")
    else:
        out["verdict"] = "OPEN" if net_apr >= hurdle_apr_pct else "CLOSED"
    out["caveat"] = ("a statement about the recorded window using the venue's forecast, in-sample; "
                     "latency, borrow, capital cost and funding sign changes are not in it")
    return out


def render(s: Dict[str, Any]) -> str:
    if not s.get("samples"):
        return s["note"]
    out = [f"{s['samples']} samples over {s['window_hours']} h of {s['environment']} data "
           f"(fees: {s['fee_source']})"]
    f = s["funding_forecast_bps_per_settlement"]
    if f:
        out.append(f"funding forecast per settlement (bps): mean {f['mean']:+.3f}, median {f['median']:+.3f}, "
                   f"min {f['min']:+.3f}, max {f['max']:+.3f}, positive {f['share_positive']:.0%} of the time")
    out.append(f"hold assumed: {s['hold_settlements']:g} settlements ({s['hold_days']:.1f} days)")
    out.append("book-fillable = snapshots in which the DISPLAYED book could fill the size (not executable liquidity); "
               "proj-covers = share of snapshots where the forecast PROJECTION covers the ESTIMATED cost (not a realised outcome)")
    out.append(f"{'size USD':>12} {'book-fillable':>13} {'cost bps':>9} {'p95':>7} {'breakeven':>10} {'net bps/hold':>13} {'proj-covers':>11}")
    for r in s["by_size"]:
        if "mean_total_round_trip_bps" not in r:
            out.append(f"{r['notional_usd']:>12,.0f} {r['book_fillable_snapshot_share']:>13.0%}   (not fillable on the displayed book)")
            continue
        be = r.get("breakeven_settlements_at_mean_forecast")
        net = r.get("net_bps_after_hold_at_mean_forecast")
        cov = r.get("share_of_snapshots_where_forecast_projection_covers_estimated_cost")
        out.append(f"{r['notional_usd']:>12,.0f} {r['book_fillable_snapshot_share']:>13.0%} {r['mean_total_round_trip_bps']:>9.2f} "
                   f"{r['p95_total_round_trip_bps']:>7.2f} {('%.1f' % be) if be else 'n/a':>10} "
                   f"{('%+.1f' % net) if net is not None else 'n/a':>13} {('%.0f%%' % (cov * 100)) if cov is not None else 'n/a':>11}")
    out.append("in-sample and descriptive; not a return and not an out-of-sample result")
    return "\n".join(out)


def _render_regime(r: Optional[Dict[str, Any]]) -> str:
    if not r:
        return ""
    if "net_apr_pct" not in r:
        return f"\nREGIME: {r['verdict']} ({r.get('why')})"
    return (f"\nREGIME at ${r['notional_usd']:,.0f}, hold {r['hold_days']:.1f} d: gross {r['gross_apr_pct']:.2f}%/yr "
            f"- cost {r['cost_apr_pct']:.2f}%/yr = net {r['net_apr_pct']:+.2f}%/yr vs hurdle "
            f"{'n/a' if r['hurdle_apr_pct'] is None else '%.2f%%/yr' % r['hurdle_apr_pct']} -> {r['verdict']}"
            + (f" ({r['why']})" if r.get("why") else "") + "\n" + r["caveat"])


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", required=True)
    ap.add_argument("--hold-settlements", type=float, default=21.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--regime", action="store_true", help="also print the regime verdict")
    ap.add_argument("--hurdle-apr", type=float, default=None,
                    help="the owner's hurdle in %%/yr, NET of trading costs; implies --regime")
    ap.add_argument("--regime-notional", type=float, default=100_000.0)
    ap.add_argument("--min-hours", type=float, default=72.0)
    a = ap.parse_args(argv)
    if not (a.hold_settlements > 0):
        print("--hold-settlements must be positive", file=sys.stderr)
        return 3
    rows = load_rows(a.dir)
    s = summarise(rows, hold_settlements=a.hold_settlements)
    if a.hurdle_apr is not None or a.regime:
        s["regime"] = regime(rows, hurdle_apr_pct=a.hurdle_apr, hold_settlements=a.hold_settlements,
                             notional_usd=a.regime_notional, min_hours=a.min_hours)
    print(json.dumps(s, indent=2, sort_keys=True) if a.json else render(s) + _render_regime(s.get("regime")))
    return 0 if s.get("samples") else 2


if __name__ == "__main__":
    sys.exit(main())
