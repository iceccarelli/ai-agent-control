#!/usr/bin/env python3
"""Search the rule space fast, and refuse to pick a winner from it.

WHY THIS TOOL IS DANGEROUS AND WHAT STOPS IT
============================================
0032 measured what the engine's exit rule costs on the real settlement clock:
86 of 87 trades leave on three negative prints, the median hold is 5.3 days,
and the median trade collects $27 of funding against a round trip it cannot
pay for. The obvious next move is to sweep the rule space and take the best
cell. That is precisely how `funding_carry_fade_btc_v1` was cleared and then
lost money forward, and it is what rule 20 forbids.

So this tool does the search — 30,000 simulations a second through the native
core (tools/carry_core.py) — and then REFUSES to name a winner:

  * every run records its SEARCH WIDTH in the hypothesis registry, because a
    correction that does not know how many cells were looked at is not a
    correction;
  * `--best` demands an out-of-sample window and asks
    tools/reserved_holdout.py whether anything has read it. Every window this
    corpus covers HAS been read, so today the honest answer is a refusal and
    the only admissible holdout is forward: settlements after the last one in
    the ledger.

The grid it prints is a MAP OF WHAT THE RULE WOULD HAVE DONE, in sample. It is
not a recommendation, and the banner says so on every run.

    python3 tools/carry_sweep.py --repo .
    python3 tools/carry_sweep.py --repo . --best --holdout-from 2026-09-10T00:00:00Z
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import carry_backtest as cb          # noqa: E402
import carry_core as core            # noqa: E402

#: The grid. Declared here, in the source, so a sweep is a reviewable act.
ENTRY_BPS = [round(0.1 * i, 2) for i in range(1, 25)]        # 0.1 .. 2.4
HOLD_DAYS = [5, 7, 10, 14, 20, 30, 45, 60, 90, 120, 180, 240]
EXIT_PRINTS = [1, 2, 3, 4, 6]

#: The dataset every cell is scored on, for the read ledger and the registry.
DATASET = "BYBIT_LINEAR_BTC_USDT_FUNDING"


def grid_id() -> str:
    blob = json.dumps({"entry": ENTRY_BPS, "hold": HOLD_DAYS,
                       "exit": EXIT_PRINTS}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def run(repo: str, *, notional: float, borrow_apr: float, overlay: bool,
        impact: bool = True) -> Dict[str, Any]:
    rows, meta = cb.load_bybit_settlements(repo)
    impact_bps = (core_impact := cb.impact_bps_per_leg(repo, notional)[0]) \
        if impact else 0.0
    base = core.params(
        notional=notional, borrow_apr=borrow_apr, impact_bps=impact_bps,
        round_trip_bps=cb.round_trip_bps_for(cb.OVERLAY if overlay
                                             else cb.ACQUIRE),
        gated=True, overlay=overlay,
        taker_spot_bps=cb.TAKER_BPS_SPOT, taker_perp_bps=cb.TAKER_BPS_PERP)
    started = time.perf_counter()
    cells = core.sweep(rows, base, entry_grid=ENTRY_BPS, hold_grid=HOLD_DAYS,
                       exit_grid=EXIT_PRINTS)
    elapsed = time.perf_counter() - started
    years = (rows[-1].ms - rows[0].ms) / (365.25 * 86_400_000)
    for cell in cells:
        cell["net_annualised_pct"] = 100.0 * cell["net"] / notional / years
    return {"cells": cells, "seconds": elapsed, "settlements": len(rows),
            "window": f"{cb._iso_ms(rows[0].ms)} .. {cb._iso_ms(rows[-1].ms)}",
            "last_settlement_ms": rows[-1].ms, "years": years,
            "impact_bps_per_leg": impact_bps, "corpus": meta["corpus"],
            "notional": notional, "borrow_apr": borrow_apr,
            "overlay": overlay}


def record_width(report: Dict[str, Any], *, path: Optional[str] = None) -> str:
    """Write the SEARCH WIDTH to the registry. A correction that does not know
    how many cells were looked at is not a correction."""
    import hypothesis_registry as hr
    registry = hr.load(path) if path else hr.load()
    trial_id = f"carry_rule_sweep@{grid_id()}"
    note = (f"{len(report['cells'])} configurations scored over "
            f"{report['settlements']} settlements ({report['window']}), "
            f"grid entry={ENTRY_BPS[0]}..{ENTRY_BPS[-1]} bps, "
            f"hold={HOLD_DAYS[0]}..{HOLD_DAYS[-1]} days, "
            f"exit={EXIT_PRINTS}. IN-SAMPLE: this window is fully read "
            "(data_read_ledger.json), so no cell may be chosen from it. The "
            "admissible holdout is forward only.")
    try:
        hr.record(registry, trial_id, status="search_width_recorded",
                  source="tools/carry_sweep.py", note=note)
        hr.save(registry, path) if path else hr.save(registry)
    except ValueError:
        pass                      # already recorded; the registry is append-only
    return trial_id


def holdout_is_untouched(dataset: str, from_utc: str, to_utc: str,
                         path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every read that overlaps the proposed holdout. Empty means untouched."""
    import reserved_holdout as rh
    ledger = rh.load(path) if path else rh.load()
    return rh.overlaps(ledger, dataset=dataset, from_utc=from_utc,
                       to_utc=to_utc)


BANNER = """\
  ################################################################
  #  IN-SAMPLE MAP, NOT A CHOICE.                                #
  #  Every cell below was scored on a window this programme has  #
  #  already read. Picking the best one is the selection path     #
  #  that cleared funding_carry_fade_btc_v1 and then lost money.  #
  ################################################################"""


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=ROOT)
    ap.add_argument("--notional", type=float, default=100_000.0)
    ap.add_argument("--borrow-apr", type=float, default=0.0)
    ap.add_argument("--acquire", action="store_true",
                    help="score the spot-buying book instead of the overlay")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--best", action="store_true",
                    help="name a winner (refused without a clean holdout)")
    ap.add_argument("--holdout-from", default="")
    ap.add_argument("--holdout-to", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    if not core.available() and not core.build():
        print("libcarrycore.so is not built and there is no compiler here.\n"
              "The same arithmetic runs in tools/carry_backtest.py, one "
              "configuration at a time.", file=sys.stderr)
        return 2

    report = run(args.repo, notional=args.notional, borrow_apr=args.borrow_apr,
                 overlay=not args.acquire)
    trial_id = record_width(report)
    cells = sorted(report["cells"], key=lambda c: c["net"], reverse=True)

    print("=" * 74)
    print(f"CARRY RULE SWEEP — {len(cells)} configurations in "
          f"{report['seconds'] * 1000:.0f} ms "
          f"({len(cells) / max(report['seconds'], 1e-9):,.0f}/s, {core.version()})")
    print("=" * 74)
    print(f"  corpus     {report['corpus']}")
    print(f"  window     {report['window']}  ({report['years']:.2f} yr)")
    print(f"  book       {'OVERLAY' if report['overlay'] else 'ACQUIRE'}, "
          f"${report['notional']:,.0f}, borrow {report['borrow_apr'] * 100:.1f}%"
          f"/yr, impact {report['impact_bps_per_leg']:.2f} bps/leg")
    print(f"  registered {trial_id}")
    print()
    print(BANNER)
    print()
    print(f"  {'entry':>6} {'hold':>5} {'exit':>5} {'net %/yr':>10} "
          f"{'trades':>7} {'losing':>7}")
    for cell in cells[:args.top]:
        print(f"  {cell['entry_bps']:6.2f} {cell['hold_days']:5.0f} "
              f"{cell['negative_exit_prints']:5d} "
              f"{cell['net_annualised_pct']:+9.2f}% {cell['trades']:7d} "
              f"{cell['losing_trades']:7d}")
    worst = cells[-1]
    print(f"  {'...':>6}")
    print(f"  {worst['entry_bps']:6.2f} {worst['hold_days']:5.0f} "
          f"{worst['negative_exit_prints']:5d} "
          f"{worst['net_annualised_pct']:+9.2f}% {worst['trades']:7d} "
          f"{worst['losing_trades']:7d}   <- worst cell")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"\n  wrote {args.out}")

    if not args.best:
        print("\n  No cell is chosen. --best asks for an out-of-sample window.")
        return 0

    if not args.holdout_from:
        print("\n  REFUSED: --best needs --holdout-from (and usually "
              "--holdout-to). A winner without an untouched window is a "
              "winner chosen by looking.", file=sys.stderr)
        return 1
    to_utc = args.holdout_to or cb._iso_ms(report["last_settlement_ms"])
    reads = holdout_is_untouched(DATASET, args.holdout_from, to_utc)
    if reads:
        print(f"\n  REFUSED: {len(reads)} recorded read(s) overlap "
              f"{args.holdout_from} .. {to_utc}:", file=sys.stderr)
        for read in reads[:5]:
            print(f"    {read['read_by']}  {read['from_utc']} .. "
                  f"{read['to_utc']}", file=sys.stderr)
        print("  The only admissible holdout for this rule is FORWARD: "
              f"settlements after {cb._iso_ms(report['last_settlement_ms'])}, "
              "fetched after they print.", file=sys.stderr)
        return 1

    print("\n  The window is untouched. Score the rule on it with "
          "tools/carry_backtest.py --clock 8h once the data exists, and "
          "record the result against the registered trial.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
