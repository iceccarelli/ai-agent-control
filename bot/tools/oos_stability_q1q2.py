#!/usr/bin/env python3
"""Split the OOS window in half and report each side. A DIAGNOSTIC, not a clause.

WHY THIS EXISTS
===============
A single percentile is very good at hiding a concentrated episode. 41 trades
over two years can produce a mean net R of +0.17 because the rule works, or
because three trades in one month went well and the rest went nowhere. M1 and
M2 cannot tell those apart; they rank the schedule as a whole.

So the OOS window is split in half by bar index — the same rule that produced
the fold cut, applied once more — and each half's trade count and mean net R
are reported.

WHAT IT CANNOT DO
=================
**It cannot fail the gate and it cannot rescue it.** The intake lists five
clauses and this is not one of them; adding a sixth after seeing the numbers
would be moving the goalposts, and moving them against a passing result is
exactly as dishonest as moving them toward a failing one. This is here so a
reader can see the shape of the evidence, and so that a decision to trade it
would be taken with that shape visible.

The halves are small — roughly 20 trades each — so a difference between them is
weak evidence of anything on its own. That is stated here rather than left for
a reader to infer, because a diagnostic that invites over-reading is worse than
no diagnostic.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import numpy as np  # noqa: E402

import backtest as bt  # noqa: E402
import market_data as md  # noqa: E402
import skill_test as sk  # noqa: E402
from signals import funding_carry_fade_btc_v1 as fb  # noqa: E402

import edge_measurement as em  # noqa: E402
import provenance as _provenance  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir",
                        default=os.path.join(REPO, "data", "real_linear_1d"))
    parser.add_argument("--funding-data", default=os.path.join(
        REPO, "data", "real_funding", "funding",
        "BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz"))
    parser.add_argument("--folds", default=fb.FOLDS_PATH)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--warmup", type=int, default=200)
    parser.add_argument("--out", default=os.path.join(
        REPO, "artifacts", "slice57_oos_stability_Q1Q2.json"))
    args = parser.parse_args(argv)

    fb.require_supported_symbol(args.symbol)
    folds = fb.load_folds(args.folds)
    if not fb.folds_are_unmodified(args.folds):
        print("REFUSING: fold calendar hash does not match the lock log.")
        return 2

    loaded, _b, _n = md.load_corpus(args.data_dir, verify=False)
    bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
            for b in loaded[args.symbol]]
    funding = fb.load_funding(args.funding_data)

    flags, directions = fb.flags_and_directions(bars, funding,
                                                warmup=args.warmup)
    flags = fb.restrict_flags_to_late(flags, bars, folds)

    side_lookup, side_net = {}, {}
    for tag, side in ((fb.LONG_SETUP, "long"), (fb.SHORT_SETUP, "short")):
        idx, net, used = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=fb.TAKE_PROFIT_ATR, stop_atr=fb.STOP_ATR,
            horizon=fb.HORIZON, atr_period=fb.ATR_PERIOD,
            round_trip_bps=fb.ROUND_TRIP_BPS, side=side, entry_on=fb.ENTRY_ON)
        side_net[tag] = fb.funding_adjusted_net_r(
            bars, funding, idx, net, used, tag,
            stop_atr=fb.STOP_ATR, atr_period=fb.ATR_PERIOD)
        side_lookup[tag] = {int(i): p for p, i in enumerate(idx)}

    eligible = np.zeros(len(bars), dtype=bool)
    eligible[sorted(set(side_lookup[fb.LONG_SETUP]) &
                    set(side_lookup[fb.SHORT_SETUP]))] = True
    eligible[:args.warmup] = False
    late = np.zeros(len(bars), dtype=bool)
    late[list(fb.late_window_indices(bars, folds))] = True
    eligible = eligible & late

    entries = sk.simulate_schedule(em.tradable_flags(flags, eligible),
                                   warmup=args.warmup, lockup=fb.LOCKUP)
    entries = [e for e in entries if e in directions]

    # The OOS window halved by BAR INDEX — the same rule that made the cut.
    lo, hi = folds["mid_index"], folds["n_bars_total"]
    q_split = lo + (hi - lo) // 2

    def leg(indices):
        rs = [float(side_net[directions[i]][side_lookup[directions[i]][i]])
              for i in indices]
        return {
            "n_trades": len(rs),
            "mean_net_r": float(np.mean(rs)) if rs else None,
            "sum_net_r": float(np.sum(rs)) if rs else None,
            "positive_trades": int(sum(1 for r in rs if r > 0)),
            "best_trade_r": float(max(rs)) if rs else None,
            "worst_trade_r": float(min(rs)) if rs else None,
        }

    q1_idx = [e for e in entries if e < q_split]
    q2_idx = [e for e in entries if e >= q_split]
    q1, q2 = leg(q1_idx), leg(q2_idx)
    whole = leg(entries)

    # How much of the total does the single best trade carry? The concentration
    # question a mean cannot answer.
    all_r = sorted((float(side_net[directions[i]][side_lookup[directions[i]][i]])
                    for i in entries), reverse=True)
    top1 = all_r[0] / sum(all_r) if sum(all_r) else None
    top3 = sum(all_r[:3]) / sum(all_r) if sum(all_r) else None

    payload = {
        "schema": "oos_stability/1",
        "slice": 57,
        "signal": fb.NAME,
        "symbol": args.symbol,
        "is_a_gate_clause": False,
        "note": ("A DIAGNOSTIC. The intake lists five clauses and this is not "
                 "one of them. It cannot fail the gate and it cannot rescue "
                 "it. Each half holds roughly 20 trades, so a difference "
                 "between them is weak evidence on its own — stated here "
                 "rather than left for a reader to infer."),
        "method": "oos_window_halved_by_bar_index",
        "folds_sha256": fb.folds_sha256(args.folds),
        "oos_bar_range": [folds["mid_index"], folds["n_bars_total"]],
        "q_split_index": q_split,
        "q1": q1,
        "q2": q2,
        "oos_whole": whole,
        "concentration": {
            "top_1_trade_share_of_total_r": top1,
            "top_3_trades_share_of_total_r": top3,
            "note": ("Share of the summed net R carried by the best 1 and best "
                     "3 trades. A mean of +0.17 over 41 trades reads very "
                     "differently if one trade carries most of it."),
        },
        "git_commit": _provenance.git_commit(REPO),
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    print("=" * 78)
    print("SLICE 57 — OOS STABILITY (Q1 / Q2)   DIAGNOSTIC, NOT A GATE CLAUSE")
    print("=" * 78)
    print(f"  OOS window bars   : {folds['mid_index']}..{folds['n_bars_total']}"
          f"   split at index {q_split}")
    for label, block in (("Q1 (earlier half)", q1), ("Q2 (later half)", q2),
                         ("OOS whole", whole)):
        mean = block["mean_net_r"]
        print(f"  {label:18s}: {block['n_trades']:3d} trades   "
              f"mean net R {mean:+.4f}   "
              f"{block['positive_trades']}/{block['n_trades']} positive"
              if mean is not None else
              f"  {label:18s}: {block['n_trades']:3d} trades")
    print()
    print(f"  best trade carries  {100 * top1:.1f}% of the summed net R")
    print(f"  best three carry    {100 * top3:.1f}%")
    print()
    print("Neither number can fail the gate or rescue it. They are here so a")
    print("reader can see whether a passing OOS mean came from a rule working")
    print("or from a handful of trades, and each half is small enough that a")
    print("difference between them is weak evidence on its own.")
    print(f"\nartefact: {os.path.relpath(args.out, REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
