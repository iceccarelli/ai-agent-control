#!/usr/bin/env python3
"""Is the entry gate's assumed holding period realistic? Measure it. No retuning.

`carry_costs.evaluate_entry` amortises the round trip over ASSUMED_HOLD_DAYS and
sizes the basis budget from it. The engine does not hold for that long by
decree: it exits when funding inverts. This tool drives the REAL `CarryEngine`
through the recorded settlement history (the existing `carry_replay`, nothing
re-implemented) and reports what the engine actually did with its holds, then
asks how the unchanged gate would behave at other assumed holds.

WHAT IT REPORTS
  * the distribution of completed holding durations and why positions exited;
  * funding the gate expected at entry (its own smoothed rate x days held)
    against funding actually earned;
  * per-trade funding, basis and round-trip fees, reconciled to the replay's own
    totals (the tool says so if they do not reconcile);
  * sensitivity A: each completed trade cut short at N days, fees paid once;
  * sensitivity B: how many of the historical entries the SAME evaluator would
    still have passed at other assumed holds, and what those entries netted.

WHAT IT IS NOT
  Historical model evidence, not proof of live profitability. Sensitivity B is
  IN-SAMPLE: it describes the past and must not be used to choose a horizon.
  Changing ASSUMED_HOLD_DAYS needs out-of-sample validation and a human
  decision; this tool changes nothing and recommends no value.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

DAY_MS = 86_400_000


def quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile of an ascending sequence."""
    if not sorted_values:
        raise ValueError("no values")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = q * (len(sorted_values) - 1)
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    frac = pos - lo
    return float(sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac)


def hold_distribution(days: Sequence[float]) -> Dict[str, Any]:
    """Distribution of completed holding durations, in days."""
    ds = sorted(float(d) for d in days)
    if not ds:
        return {"n": 0}
    return {
        "n": len(ds), "min": ds[0], "p10": quantile(ds, .10),
        "p25": quantile(ds, .25), "median": quantile(ds, .50),
        "mean": sum(ds) / len(ds), "p75": quantile(ds, .75),
        "p90": quantile(ds, .90), "max": ds[-1],
        "share_under_7d": sum(d < 7 for d in ds) / len(ds),
        "share_at_least_30d": sum(d >= 30 for d in ds) / len(ds),
    }


def hold_verdict(assumed_days: float, dist: Dict[str, Any]) -> Dict[str, Any]:
    """Compare the gate's assumed hold with what the engine actually did.

    OPTIMISTIC when the assumption exceeds BOTH the median and the mean of the
    completed holds: the typical trade, and the average trade, are shorter than
    the horizon the gate amortises the round trip over. No value is proposed.
    """
    if not dist.get("n"):
        return {"verdict": "NO_COMPLETED_HOLDS", "assumed_hold_days": assumed_days}
    mean, median = dist["mean"], dist["median"]
    if assumed_days > mean and assumed_days > median:
        verdict = "GATE_OPTIMISTIC_ABOUT_HOLD"
    elif assumed_days > median:
        verdict = "GATE_ABOVE_MEDIAN_BELOW_MEAN"
    else:
        verdict = "GATE_NOT_OPTIMISTIC_ABOUT_HOLD"
    return {
        "verdict": verdict, "assumed_hold_days": assumed_days,
        "measured_mean_days": mean, "measured_median_days": median,
        "assumed_over_mean": assumed_days / mean if mean else None,
        "assumed_over_median": assumed_days / median if median else None,
    }


def assess(verdict: Dict[str, Any], funding: Dict[str, Any],
           completed_net: Dict[str, Any]) -> str:
    """One honest sentence that keeps the two halves of the evidence together."""
    ratio = funding.get("earned_over_expected")
    parts = []
    if verdict.get("verdict") == "GATE_OPTIMISTIC_ABOUT_HOLD":
        parts.append(
            f"The gate amortises the round trip over "
            f"{verdict['assumed_hold_days']:.0f} days but completed holds had a "
            f"median of {verdict['measured_median_days']:.1f} and a mean of "
            f"{verdict['measured_mean_days']:.1f} days, so for the typical trade "
            "it is optimistic about how long the cost is spread.")
    if ratio is not None and ratio > 1.5:
        parts.append(
            f"In the same replay the engine earned {ratio:.2f}x the funding the "
            "gate's smoothed rate predicted at entry, so the hold optimism is "
            "offset in aggregate by a conservative funding expectation; this is "
            "NOT shown to make entries mispriced overall.")
    elif ratio is not None and ratio < 0.8:
        parts.append(
            f"The engine earned only {ratio:.2f}x the funding the gate "
            "predicted, so the gate is optimistic on funding as well.")
    share = completed_net.get("losing_share")
    if share is not None and share > 0.5:
        parts.append(
            f"{share:.0%} of completed trades lost money after fees; the book's "
            "profit comes from a minority of long holds that cannot be "
            "identified at entry.")
    parts.append("ASSUMED_HOLD_DAYS is not changed here: replacing it needs "
                 "out-of-sample validation and a human decision, and the "
                 "in-sample table below must not be used to choose it.")
    return " ".join(parts)


def amortised_bps_per_day(round_trip_bps: float, hold_days: float) -> float:
    return float(round_trip_bps) / max(hold_days, 1e-9)


def _trade_net(t: Dict[str, Any], *, taker_perp_bps: float,
               taker_spot_bps: float, mode: str) -> Dict[str, float]:
    """Funding, basis and fees of ONE trade, from the replay's own fills."""
    q = t["qty"]                         # perp quantity the engine actually shorted
    spot_q = t.get("spot_qty", q)         # net BTC held after any base-coin fee
    spot_entry_q = t.get("spot_entry_qty", spot_q)  # gross spot BUY fill
    basis = (-q * (t["exit_perp"] - t["entry_perp"])
             + spot_q * (t["exit_spot"] - t["entry_spot"]))
    open_at_end = bool(t.get("open_at_end"))
    perp_fee = q * t["entry_perp"] * taker_perp_bps / 1e4
    if not open_at_end:                       # an open position has not paid exit
        perp_fee += q * t["exit_perp"] * taker_perp_bps / 1e4
    fees = perp_fee
    if mode == "acquire":
        # Spot entry fee is paid on the GROSS purchase; the wallet then holds
        # less BTC if the venue withholds the base-coin fee. The exit fee is on
        # the net amount sold. Using one quantity for all four legs hid this.
        fees += spot_entry_q * t["entry_spot"] * taker_spot_bps / 1e4
        if not open_at_end:
            fees += spot_q * t["exit_spot"] * taker_spot_bps / 1e4
    funding = float(t["funding"])
    return {"funding": funding, "basis": basis, "fees": fees,
            "net": funding + basis - fees}


def run_audit(repo: str = ROOT, *, notional: float = 100_000.0,
              mode: str = "overlay", style: str = "taker",
              borrow_apr: float = 0.0) -> Dict[str, Any]:
    import carry_backtest as cb
    import carry_costs as cc
    import carry_replay as cr
    from carry_engine import CarryEngine

    rows, meta = cb.load_bybit_settlements(repo)
    impact = cb.impact_bps_per_leg(repo, notional)[0]
    r = cr.replay(rows, notional=notional, borrow_apr=borrow_apr,
                  execution_mode=mode, execution_style=style, impact_bps=impact)
    trades: List[Dict[str, Any]] = r["trade_rows"]
    idx = {int(x.ms): i for i, x in enumerate(rows)}
    taker_perp, taker_spot = cc.TAKER_BPS_PERP, cc.TAKER_BPS_SPOT
    round_trip = (2 * taker_perp if mode == "overlay"
                  else 2 * (taker_spot + taker_perp))
    history = CarryEngine.FUNDING_HISTORY

    econ = [_trade_net(t, taker_perp_bps=taker_perp, taker_spot_bps=taker_spot,
                       mode=mode) for t in trades]
    completed = [(t, e) for t, e in zip(trades, econ)
                 if not t.get("open_at_end")]
    days = [(t["closed_ms"] - t["opened_ms"]) / DAY_MS for t, _ in completed]
    dist = hold_distribution(days)

    def prints_at(i: int) -> List[float]:
        return [x.rate * 1e4 for x in rows[max(0, i - history + 1): i + 1]]

    # The gate's expectation at entry versus what the engine actually earned.
    expected = realised = 0.0
    reproduces = 0
    for t, _e in zip(trades, econ):
        i0 = idx[int(t["opened_ms"])]
        sm = cc.ewma_funding_bps(prints_at(i0)) or 0.0
        d = (t["closed_ms"] - t["opened_ms"]) / DAY_MS
        expected += (sm * cc.FUNDING_PERIODS_PER_DAY / 1e4) * (
            t["qty"] * t["entry_perp"]) * d
        realised += float(t["funding"])
        v = cc.entry_gate(
            funding_prints_bps=prints_at(i0), perp=t["entry_perp"],
            spot=t["entry_spot"], borrow_apr=borrow_apr,
            round_trip_bps=round_trip)
        reproduces += bool(v.allowed)

    tot = {k: sum(e[k] for e in econ) for k in ("funding", "basis", "fees", "net")}
    reconciles = (abs(tot["funding"] - r["funding_usd"]) < 1.0
                  and abs(tot["basis"] - r["basis_usd"]) < 1.0
                  and abs(tot["fees"] + r["fees_usd"]) < 1.0)

    def capped(t: Dict[str, Any], cap_days: float) -> Dict[str, float]:
        i0, i1 = idx[int(t["opened_ms"])], idx[int(t["closed_ms"])]
        if t.get("open_at_end") or (i1 - i0) / cc.FUNDING_PERIODS_PER_DAY <= cap_days:
            return _trade_net(t, taker_perp_bps=taker_perp,
                              taker_spot_bps=taker_spot, mode=mode)
        k = i0 + int(round(cap_days * cc.FUNDING_PERIODS_PER_DAY))
        bar = rows[k]
        cut = dict(t)
        cut["exit_perp"] = bar.perp * (1 + impact / 1e4)   # buy the short back
        cut["exit_spot"] = bar.spot
        cut["funding"] = sum(rows[j].rate * t["qty"] * rows[j].perp
                             for j in range(i0 + 1, k + 1))
        return _trade_net(cut, taker_perp_bps=taker_perp,
                          taker_spot_bps=taker_spot, mode=mode)

    total_net = sum(e["net"] for _, e in completed) or float("nan")
    cap_table = []
    for cap in (3, 7, 14, 30, None):
        nets = [(_trade_net(t, taker_perp_bps=taker_perp,
                            taker_spot_bps=taker_spot, mode=mode)
                 if cap is None else capped(t, cap))["net"]
                for t, _ in completed]
        ns = sorted(nets)
        cap_table.append({
            "cap_days": cap, "trades": len(nets),
            "losing": sum(n < 0 for n in nets),
            "losing_share": sum(n < 0 for n in nets) / len(nets),
            "mean_net_usd": sum(nets) / len(nets),
            "median_net_usd": quantile(ns, .5),
            "share_of_full_hold_net": sum(nets) / total_net,
        })

    holds = [("assumed (frozen)", cc.ASSUMED_HOLD_DAYS)]
    if dist.get("n"):
        holds += [("measured mean", dist["mean"]),
                  ("measured median", dist["median"])]
    horizon_table = []
    for label, h in holds:
        passed_net = refused_net = 0.0
        passed = 0
        for (t, e) in zip(trades, econ):
            i0 = idx[int(t["opened_ms"])]
            v = cc.evaluate_entry(
                funding_prints_bps=prints_at(i0), perp=t["entry_perp"],
                spot=t["entry_spot"], borrow_apr=borrow_apr,
                round_trip_bps=round_trip, hold_days=h)
            if v.allowed:
                passed += 1
                passed_net += e["net"]
            else:
                refused_net += e["net"]
        horizon_table.append({
            "label": label, "hold_days": h, "entries_passed": passed,
            "entries_total": len(trades), "net_of_passed_usd": passed_net,
            "net_of_refused_usd": refused_net,
            "amortised_round_trip_bps_per_day": amortised_bps_per_day(
                round_trip, h)})

    reasons: Dict[str, int] = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1

    result = {
        "corpus": meta.get("corpus"), "mode": mode, "style": style,
        "window": r["window"], "prints": len(rows), "years": r["years"],
        "notional_usd": notional, "borrow_apr": borrow_apr,
        "round_trip_bps": round_trip, "engine_funding_history": history,
        "trades": len(trades), "completed_trades": len(completed),
        "exit_reasons": reasons,
        "hold_days": dist,
        "verdict": hold_verdict(cc.ASSUMED_HOLD_DAYS, dist),
        "funding_vs_expectation": {
            "expected_by_gate_usd": expected, "earned_usd": realised,
            "earned_over_expected": (realised / expected if expected else None)},
        "totals": {"funding_usd": tot["funding"], "basis_usd": tot["basis"],
                   "fees_usd": -tot["fees"], "borrow_usd": r["borrow_usd"],
                   "net_usd": r["net_usd"],
                   "net_annualised_pct": r["net_annualised_pct"]},
        "completed_trade_net": {
            "losing_share": sum(e["net"] < 0 for _, e in completed) / len(completed)
            if completed else None,
            "mean_usd": (sum(e["net"] for _, e in completed) / len(completed)
                         if completed else None),
            "median_usd": (quantile(sorted(e["net"] for _, e in completed), .5)
                           if completed else None)},
        "assessment": None,
        "liquidation": r["liquidation"],
        "reconciles_with_replay": reconciles,
        "gate_reproduces_every_historical_entry": reproduces == len(trades),
        "gate_reproduced_entries": reproduces,
        "sensitivity_to_early_exit": cap_table,
        "sensitivity_to_assumed_hold_in_sample": horizon_table,
        "caveats": [
            "historical replay of the real engine, not live evidence",
            "sensitivity_to_assumed_hold is in-sample and must not be used to "
            "pick a horizon",
            "early-exit sensitivity cuts each completed trade short and pays "
            "fees once; the engine would otherwise re-enter",
            "ASSUMED_HOLD_DAYS is unchanged by this tool",
        ],
    }
    result["assessment"] = assess(result["verdict"],
                                  result["funding_vs_expectation"],
                                  result["completed_trade_net"])
    return result


def _print(a: Dict[str, Any]) -> None:
    d, v = a["hold_days"], a["verdict"]
    print("=" * 74)
    print("CARRY HOLD AUDIT - the engine's real holds against the gate's assumption")
    print("=" * 74)
    print(f"  corpus {a['corpus']}  window {a['window']}  ({a['prints']} prints, "
          f"{a['years']:.2f} yr)")
    print(f"  book   {a['mode'].upper()}/{a['style']}  ${a['notional_usd']:,.0f}  "
          f"round trip {a['round_trip_bps']:.1f} bps  borrow "
          f"{a['borrow_apr'] * 100:.1f}%/yr")
    if not a["liquidation"]["production_equivalent"]:
        print(f"  !! NOT PRODUCTION-EQUIVALENT: liquidation gate "
              f"{a['liquidation']['gate']} (no liqPrice/accountMMRate in the "
              "corpus); these holds ran without the production liquidation "
              "exit")
    print(f"  trades {a['trades']} ({a['completed_trades']} completed); exits: "
          f"{a['exit_reasons']}")
    print(f"\n  completed holds (days): min {d['min']:.1f}  p25 {d['p25']:.1f}  "
          f"median {d['median']:.1f}  mean {d['mean']:.1f}  p75 {d['p75']:.1f}  "
          f"p90 {d['p90']:.1f}  max {d['max']:.1f}")
    print(f"    under 7 days: {d['share_under_7d']:.0%}   at least 30 days: "
          f"{d['share_at_least_30d']:.0%}")
    print(f"  gate assumes {v['assumed_hold_days']:.0f} days -> {v['verdict']}  "
          f"(assumed/mean {v['assumed_over_mean']:.2f}x, assumed/median "
          f"{v['assumed_over_median']:.2f}x)")
    f = a["funding_vs_expectation"]
    print(f"  funding: the gate's smoothed-rate expectation ${f['expected_by_gate_usd']:,.0f}"
          f"  earned ${f['earned_usd']:,.0f}  ({f['earned_over_expected']:.2f}x)")
    t = a["totals"]
    print(f"  totals : funding ${t['funding_usd']:,.0f}  basis ${t['basis_usd']:,.0f}  "
          f"fees ${t['fees_usd']:,.0f}  net ${t['net_usd']:,.0f} "
          f"({t['net_annualised_pct']:+.2f}%/yr)   reconciles with replay: "
          f"{a['reconciles_with_replay']}")
    c = a["completed_trade_net"]
    print(f"  completed trades: {c['losing_share']:.0%} lose money; median "
          f"${c['median_usd']:,.0f}, mean ${c['mean_usd']:,.0f} per trade")
    print("\n  early-exit sensitivity (each trade cut at N days, fees paid once):")
    for row in a["sensitivity_to_early_exit"]:
        cap = "full hold" if row["cap_days"] is None else f"{row['cap_days']} d"
        print(f"    {cap:>9}: {row['losing_share']:4.0%} lose  mean "
              f"${row['mean_net_usd']:8,.0f}  median ${row['median_net_usd']:8,.0f}"
              f"  {row['share_of_full_hold_net']:6.1%} of the full-hold net")
    print("\n  the SAME evaluator at other assumed holds (IN-SAMPLE, do not tune):")
    for row in a["sensitivity_to_assumed_hold_in_sample"]:
        print(f"    {row['label']:>16} {row['hold_days']:5.1f} d: passes "
              f"{row['entries_passed']:>2}/{row['entries_total']}  net of passed "
              f"${row['net_of_passed_usd']:9,.0f}  of refused "
              f"${row['net_of_refused_usd']:9,.0f}")
    print("\n  ASSESSMENT: " + a["assessment"])
    print("  gate reproduces every historical entry at the frozen hold: "
          f"{a['gate_reproduces_every_historical_entry']}")
    print("\n  " + "\n  ".join(a["caveats"]))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=ROOT)
    ap.add_argument("--notional", type=float, default=100_000.0)
    ap.add_argument("--mode", choices=("overlay", "acquire"), default="overlay")
    ap.add_argument("--style", choices=("taker", "maker_first"), default="taker")
    ap.add_argument("--borrow-apr", type=float, default=0.0)
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)
    audit = run_audit(args.repo, notional=args.notional, mode=args.mode,
                      style=args.style, borrow_apr=args.borrow_apr)
    _print(audit)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(audit, fh, indent=2, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
