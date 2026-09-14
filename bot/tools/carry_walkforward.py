#!/usr/bin/env python3
"""Choose on the past. Score on the next month. Never look ahead.

THE ONE UNTICKED BOX
====================
`carry_backtest --clock 8h --mode overlay --gated` prints +7.23%/yr and
`QUOTABLE: False`, with four of five conditions met and exactly one missing:

    [ ] rule_scored_on_an_untouched_window

The gated rule's parameters were settled after 0018 with the whole corpus
visible. Nothing in +7.23%/yr distinguishes a carry edge that exists from one
that was fitted, and that distinction is the entire distance between this book
and an asset somebody can put money behind.

Only a REGISTERED FORWARD HOLDOUT ticks that box, and it needs settlements that
have not happened yet. This tool is the strongest evidence obtainable today:

    at the end of every month, refit using ONLY what had printed by then;
    trade the following month with that choice;
    never let a choice be scored on a settlement it was allowed to see.

If a rule refit blind earns about what the frozen constants earn, the frozen
constants were not bought with hindsight. If blind refitting collapses, the
+7.23%/yr is a number about this corpus and not about carry.

WHAT THIS IS NOT
================
NOT a holdout, and NOT quotable. Walk-forward reads the same corpus dozens of
times; `is_a_quotable_return` is False on every run and the box stays unticked.
It also names NO WINNER. A tool that returned "the best parameters" would be
the selection path that cleared `funding_carry_fade_btc_v1` and then lost money
forward, wearing a lab coat.

THE DEFECT THIS ALSO FIXES
==========================
The gated rule never reads an entry threshold. `may_open_gated` decides on the
EWMA of funding, the cost of capital and the basis budget; `entry_bps` appears
only in the UNGATED branch. Measured:

    entry 0.1 bps -> net $29,696.41, 79 trades
    entry 2.4 bps -> net $29,696.41, 79 trades

So 0039's "1,440 configurations" was 60 distinct gated rules printed 24 times,
and the parameters the shipped rule actually has — `ewma_alpha`,
`ewma_min_prints` — were never searched at all. The map had the wrong axes.
These are the right ones.

    python3 tools/carry_walkforward.py --repo .
    python3 tools/carry_walkforward.py --repo . --step-days 90 --json out.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import carry_backtest as cb          # noqa: E402
import carry_core as core            # noqa: E402

DAY_MS = 86_400_000

#: THE AXES THE GATED RULE ACTUALLY HAS. Each name appears in
#: `may_open_gated` or in the exit rule; `tests/test_carry_walkforward.py`
#: asserts that against the C source, so an axis cannot drift into being
#: decorative the way `entry_bps` did.
HOLD_DAYS = [5.0, 7.0, 10.0, 14.0, 20.0, 30.0, 45.0, 60.0, 90.0, 120.0,
             180.0, 240.0]
EXIT_PRINTS = [1, 2, 3, 4, 6]
EWMA_ALPHA = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
AXES = ("hold_days", "negative_exit_prints", "ewma_alpha")

DATASET = "BYBIT_LINEAR_BTC_USDT_FUNDING"


def _frozen() -> Dict[str, Any]:
    """The shipped constants, READ from where they live.

    Never literals: if someone retunes `carry_costs`, the frozen line moves
    with it and the comparison below stays a comparison.
    """
    sys.path.insert(0, ROOT)
    import carry_costs
    import carry_engine
    return {"hold_days": float(carry_costs.ASSUMED_HOLD_DAYS),
            "ewma_alpha": float(carry_costs.EWMA_ALPHA),
            "negative_exit_prints":
                int(carry_engine.NEGATIVE_FUNDING_EXIT_PRINTS)}


FROZEN = _frozen()


def grid_id() -> str:
    blob = json.dumps({"hold": HOLD_DAYS, "exit": EXIT_PRINTS,
                       "alpha": EWMA_ALPHA}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


# -- parameters -----------------------------------------------------------

def base_params(*, notional: float, borrow_apr: float, impact_bps: float,
                overlay: bool):
    """The costs and the book. Everything the SEARCH varies is overwritten
    per cell; everything here is held fixed across the whole run."""
    return core.params(
        notional=notional, borrow_apr=borrow_apr, impact_bps=impact_bps,
        round_trip_bps=cb.round_trip_bps_for(cb.OVERLAY if overlay
                                             else cb.ACQUIRE),
        gated=True, overlay=overlay,
        taker_spot_bps=cb.TAKER_BPS_SPOT, taker_perp_bps=cb.TAKER_BPS_PERP)


def cell_params(base, cell: Dict[str, Any]):
    p = core.params(
        notional=base.notional, entry_bps=0.0, borrow_apr=base.borrow_apr,
        impact_bps=base.impact_bps, round_trip_bps=base.round_trip_bps,
        hold_days=float(cell["hold_days"]),
        taker_spot_bps=base.taker_spot_bps, taker_perp_bps=base.taker_perp_bps,
        ewma_alpha=float(cell["ewma_alpha"]), gated=True,
        overlay=bool(base.overlay), ewma_min_prints=base.ewma_min_prints,
        negative_exit_prints=int(cell["negative_exit_prints"]),
        funding_history=base.funding_history)
    return p


def _key(cell: Dict[str, Any]) -> Tuple[float, int, float]:
    return (float(cell["hold_days"]), int(cell["negative_exit_prints"]),
            float(cell["ewma_alpha"]))


# -- the walk -------------------------------------------------------------

def index_at(rows: Sequence[Any], ms: int) -> int:
    """First row at or after `ms`. Plain scan: 4,500 rows, once per segment."""
    for i, r in enumerate(rows):
        if r.ms >= ms:
            return i
    return len(rows)


def segments(rows: Sequence[Any], *, burn_in_days: float,
             step_days: float) -> List[Tuple[int, int]]:
    """`(fit_end, score_end)` pairs. Contiguous, non-overlapping, forward.

    `fit_end` is exclusive: the choice for a segment sees `rows[:fit_end]` and
    is scored on `rows[fit_end:score_end]`, which it has never seen.
    """
    if not rows:
        return []
    start = rows[0].ms
    out: List[Tuple[int, int]] = []
    fit_end = index_at(rows, start + int(burn_in_days * DAY_MS))
    while fit_end < len(rows):
        score_end = index_at(rows, rows[fit_end].ms + int(step_days * DAY_MS))
        if score_end <= fit_end:
            break
        out.append((fit_end, min(score_end, len(rows))))
        if score_end >= len(rows):
            break
        fit_end = score_end
    return out


def sweep_cells(rows: Sequence[Any], base) -> List[Dict[str, Any]]:
    """Every (hold, exit, alpha) on `rows`. One native call per alpha.

    `entry_grid` is a single dead value: the gated rule does not read it, and
    passing 24 of them is how 0039 counted 60 rules as 1,440.
    """
    cells: List[Dict[str, Any]] = []
    for alpha in EWMA_ALPHA:
        p = core.params(
            notional=base.notional, entry_bps=0.0, borrow_apr=base.borrow_apr,
            impact_bps=base.impact_bps, round_trip_bps=base.round_trip_bps,
            hold_days=base.hold_days, taker_spot_bps=base.taker_spot_bps,
            taker_perp_bps=base.taker_perp_bps, ewma_alpha=float(alpha),
            gated=True, overlay=bool(base.overlay),
            ewma_min_prints=base.ewma_min_prints,
            negative_exit_prints=base.negative_exit_prints,
            funding_history=base.funding_history)
        for cell in core.sweep(rows, p, entry_grid=[0.0], hold_grid=HOLD_DAYS,
                               exit_grid=EXIT_PRINTS):
            cell["ewma_alpha"] = float(alpha)
            cell.pop("entry_bps", None)
            cells.append(cell)
    return cells


def choose(rows_prefix: Sequence[Any], base) -> Tuple[float, int, float]:
    """The best cell on what had printed by the boundary. Deterministic.

    Ties break on the smallest (hold, exit, alpha) so the same prefix always
    yields the same choice — a walk-forward whose answer depends on dict order
    is not a measurement.
    """
    cells = sweep_cells(rows_prefix, base)
    best = max(cells, key=lambda c: (c["net"], -c["hold_days"],
                                     -c["negative_exit_prints"],
                                     -c["ewma_alpha"]))
    return _key(best)


def score(rows: Sequence[Any], t1: int, t2: int, p) -> float:
    """P&L over `[t1, t2)` as a DIFFERENCE OF PREFIXES.

    `net(rows[:t2]) - net(rows[:t1])`, same parameters. Both runs start at row
    0, so the EWMA is warm at t1 and the segment is not handed a book that
    springs into existence flat. It also credits the rule with the position it
    would already have been holding at t1 UNDER ITS OWN HISTORY, which is the
    right counterfactual for "what would this rule have earned next month".

    It is not a pathwise-continuous book: the position carried into t1 is the
    one this cell would have held, not the one last month's cell did. Stated
    rather than smoothed over.
    """
    return float(segment_result(rows, t1, t2, p)["net"])


#: Counters that are cumulative in the native Result, so a difference of
#: prefixes is the segment's own count. `max_adverse_short_pct` is NOT here:
#: a maximum does not subtract.
_ADDITIVE = ("funding", "basis", "fees", "borrow", "impact", "net",
             "days_in_market", "trades", "closed_trades", "losing_trades")


def segment_result(rows: Sequence[Any], t1: int, t2: int,
                   p) -> Dict[str, float]:
    """Every additive term over `[t1, t2)`, by the same difference.

    Telescoping is exact for a fixed cell: summing the segments reproduces
    `net(rows[:last]) - net(rows[:first])` to the bit, which is what makes
    these lines a decomposition of one continuous run rather than a stack of
    little backtests. `tests/test_carry_walkforward.py` asserts it.
    """
    head = core.simulate(rows[:t2], p)
    if t1 <= 0:
        return {k: float(head[k]) for k in _ADDITIVE}
    tail = core.simulate(rows[:t1], p)
    return {k: float(head[k]) - float(tail[k]) for k in _ADDITIVE}


def run_rows(rows: Sequence[Any], *, notional: float, borrow_apr: float,
             impact_bps: float, overlay: bool, burn_in_days: float,
             step_days: float) -> Dict[str, Any]:
    base = base_params(notional=notional, borrow_apr=borrow_apr,
                       impact_bps=impact_bps, overlay=overlay)
    segs = segments(rows, burn_in_days=burn_in_days, step_days=step_days)
    if not segs:
        raise SystemExit(
            f"{len(rows)} settlements is not enough to walk forward with a "
            f"{burn_in_days:.0f}-day burn-in and a {step_days:.0f}-day step. "
            "Refusing rather than shrinking the burn-in until it fits: a "
            "choice made on two months of funding is not a choice.")

    started = time.perf_counter()
    frozen_p = cell_params(base, FROZEN)
    chosen: List[Tuple[float, int, float]] = []
    wf_seg: List[Dict[str, float]] = []
    fz_seg: List[Dict[str, float]] = []
    for fit_end, score_end in segs:
        pick = choose(rows[:fit_end], base)
        chosen.append(pick)
        p = cell_params(base, {"hold_days": pick[0],
                               "negative_exit_prints": pick[1],
                               "ewma_alpha": pick[2]})
        wf_seg.append(segment_result(rows, fit_end, score_end, p))
        fz_seg.append(segment_result(rows, fit_end, score_end, frozen_p))
    wf_net = [x["net"] for x in wf_seg]
    fz_net = [x["net"] for x in fz_seg]
    elapsed = time.perf_counter() - started

    # THE ORACLE. One cell, chosen with the whole window visible, scored on the
    # same segments. It is the hindsight ceiling and it is NOT adoptable: it is
    # printed so the gap between it and the blind lines is visible.
    oracle_key = choose(rows, base)
    oracle_p = cell_params(base, {"hold_days": oracle_key[0],
                                  "negative_exit_prints": oracle_key[1],
                                  "ewma_alpha": oracle_key[2]})
    or_seg = [segment_result(rows, a, b, oracle_p) for a, b in segs]
    or_net = [x["net"] for x in or_seg]

    span_ms = rows[segs[0][0]].ms - rows[0].ms
    oos_ms = rows[segs[-1][1] - 1].ms - rows[segs[0][0]].ms
    oos_years = max(oos_ms / (365.25 * DAY_MS), 1e-9)
    per_refit = len(HOLD_DAYS) * len(EXIT_PRINTS) * len(EWMA_ALPHA)

    def line(segments_out: List[Dict[str, float]]) -> Dict[str, Any]:
        values = [x["net"] for x in segments_out]
        total = sum(values)
        ranked = sorted(values, reverse=True)
        days = sum(x["days_in_market"] for x in segments_out)
        span_days = oos_years * 365.25
        return {
            "net_usd": total,
            "net_annualised_pct": 100.0 * total / notional / oos_years,
            "positive_segments": sum(1 for v in values if v > 0),
            "worst_segment_usd": min(values) if values else 0.0,
            "best_segment_usd": max(values) if values else 0.0,
            "median_segment_usd": statistics.median(values) if values else 0.0,
            # CHURN. Same days in the market for a sixth of the entries is the
            # whole of D14 in one pair of numbers.
            "trades": sum(x["trades"] for x in segments_out),
            "fees_usd": -sum(x["fees"] for x in segments_out),
            "funding_usd": sum(x["funding"] for x in segments_out),
            "days_in_market": days,
            "market_exposure_pct": 100.0 * days / max(span_days, 1e-9),
            # HOW LONG ONE SHORT IS HELD. The cheap rule is cheap because it
            # stops churning, and it stops churning by holding the perp short
            # for months. Same book, different risk product.
            "mean_hold_days": (days / sum(x["trades"] for x in segments_out)
                               if sum(x["trades"] for x in segments_out) else
                               0.0),
            # CONCENTRATION. A carry book earns in spikes; a mean that hides
            # that is a mean nobody can plan against.
            "top1_share": (ranked[0] / total) if total > 0 and ranked else 0.0,
            "top3_share": (sum(ranked[:3]) / total) if total > 0 else 0.0,
            "top6_share": (sum(ranked[:6]) / total) if total > 0 else 0.0,
        }

    stable = len({c for c in chosen})
    return {
        "window": f"{cb._iso_ms(rows[0].ms)} .. {cb._iso_ms(rows[-1].ms)}",
        "settlements": len(rows),
        "burn_in_days": burn_in_days, "step_days": step_days,
        "burn_in_actual_days": span_ms / DAY_MS,
        "oos_window": (f"{cb._iso_ms(rows[segs[0][0]].ms)} .. "
                       f"{cb._iso_ms(rows[segs[-1][1] - 1].ms)}"),
        "oos_years": oos_years,
        "segments": [list(s) for s in segs],
        "notional_usd": notional, "borrow_apr": borrow_apr,
        "impact_bps_per_leg": impact_bps,
        "execution_mode": cb.OVERLAY if overlay else cb.ACQUIRE,
        "seconds": elapsed,
        "cells_per_refit": per_refit,
        "cells_examined": per_refit * len(segs),
        "axes": {"hold_days": HOLD_DAYS, "negative_exit_prints": EXIT_PRINTS,
                 "ewma_alpha": EWMA_ALPHA},
        "frozen_cell": dict(FROZEN),
        "oracle_cell": {"hold_days": oracle_key[0],
                        "negative_exit_prints": oracle_key[1],
                        "ewma_alpha": oracle_key[2]},
        "chosen_cells": [list(c) for c in chosen],
        "distinct_choices": stable,
        "frozen_segment_net": fz_net,
        "walk_forward_segment_net": wf_net,
        "oracle_segment_net": or_net,
        "frozen_net": sum(fz_net),
        "walk_forward_net": sum(wf_net),
        "oracle_net": sum(or_net),
        "frozen": line(fz_seg),
        "walk_forward": line(wf_seg),
        "oracle": line(or_seg),
        "hindsight_premium_usd": sum(or_net) - sum(wf_net),
        "is_a_quotable_return": False,
        "why_not_quotable":
            "walk-forward is not a registered holdout: this corpus was read "
            "before the parameters were chosen and is read again here, dozens "
            "of times. It is evidence about the rule's fragility, not a "
            "return anyone may quote. The admissible holdout is forward only.",
    }


def deflated(report: Dict[str, Any], values: List[float],
             n_trials: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Deflated Sharpe on the OOS segment returns, corrected for the search.

    The correction needs the number of rules the search LOOKED at, and this is
    the number that 0039 got wrong: 1,440 cells of which 1,380 were the same
    60 rules with a threshold the gated rule never reads. `cells_examined`
    here counts distinct rules.
    """
    try:
        import deflated_sharpe as ds
    except Exception:                                        # noqa: BLE001
        return None
    returns = [v / report["notional_usd"] for v in values]
    if len(returns) < 3 or max(returns) == min(returns):
        return None
    try:
        out = ds.deflated_sharpe_ratio(
            returns, int(n_trials if n_trials is not None
                         else report["cells_examined"]))
    except Exception:                                        # noqa: BLE001
        return None
    return dict(out) if isinstance(out, dict) else {"dsr": float(out)}


def record_width(report: Dict[str, Any], *, path: Optional[str] = None) -> str:
    """Write the true search width to the registry.

    `cells_examined` counts DISTINCT gated rules. 0039 recorded 1,440 per
    sweep when 1,380 of them were duplicates of the other 60 — a correction
    fed a number that describes the loop, not the search.
    """
    import hypothesis_registry as hr
    registry = hr.load(path) if path else hr.load()
    trial_id = f"carry_walkforward@{grid_id()}"
    note = (f"{report['cells_examined']} distinct gated rules scored "
            f"({report['cells_per_refit']} per refit x "
            f"{len(report['segments'])} refits) over "
            f"{report['settlements']} settlements ({report['window']}), "
            f"burn-in {report['burn_in_days']:.0f}d, step "
            f"{report['step_days']:.0f}d. Axes: hold_days, "
            "negative_exit_prints, ewma_alpha — entry_bps is NOT an axis, the "
            "gated rule does not read it. WALK-FORWARD IS NOT A HOLDOUT: not "
            "quotable, and no cell is adopted.")
    try:
        hr.record(registry, trial_id, status="search_width_recorded",
                  source="tools/carry_walkforward.py", note=note)
        hr.save(registry, path) if path else hr.save(registry)
    except ValueError:
        pass                      # already recorded; the registry is append-only
    return trial_id


BANNER = """\
  ################################################################
  #  NOT A HOLDOUT AND NOT QUOTABLE.                             #
  #  Blind refitting on a corpus that was already read is        #
  #  evidence about FRAGILITY, not an out-of-sample return.      #
  #  No cell below is adopted. The admissible holdout is         #
  #  forward: settlements that have not printed yet.             #
  ################################################################"""


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=ROOT)
    ap.add_argument("--notional", type=float, default=100_000.0)
    ap.add_argument("--borrow-apr", type=float, default=0.0)
    ap.add_argument("--acquire", action="store_true")
    ap.add_argument("--burn-in-days", type=float, default=365.0)
    ap.add_argument("--step-days", type=float, default=30.0)
    ap.add_argument("--no-impact", action="store_true")
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)

    if not core.available() and not core.build():
        print("libcarrycore.so is not built and there is no compiler here.\n"
              "A walk-forward is thousands of simulations; the Python "
              "simulator in tools/carry_backtest.py runs one at a time.",
              file=sys.stderr)
        return 2

    rows, meta = cb.load_bybit_settlements(args.repo)
    impact_bps = (0.0 if args.no_impact
                  else cb.impact_bps_per_leg(args.repo, args.notional)[0])
    r = run_rows(rows, notional=args.notional, borrow_apr=args.borrow_apr,
                 impact_bps=impact_bps, overlay=not args.acquire,
                 burn_in_days=args.burn_in_days, step_days=args.step_days)
    r["corpus"] = meta["corpus"]
    r["spot_source"] = meta["label"]
    trial_id = record_width(r)

    print("=" * 74)
    print("CARRY WALK-FORWARD — chosen on the past, scored on the next month")
    print("=" * 74)
    print(f"  corpus       {r['corpus']}")
    print(f"  window       {r['window']}  ({r['settlements']} prints)")
    print(f"  out of sample{'':1} {r['oos_window']}  "
          f"({r['oos_years']:.2f} yr, {len(r['segments'])} segments of "
          f"{r['step_days']:.0f}d after a {r['burn_in_actual_days']:.0f}d "
          "burn-in)")
    print(f"  book         {r['execution_mode'].upper()}, "
          f"${r['notional_usd']:,.0f}, borrow {r['borrow_apr'] * 100:.1f}%/yr,"
          f" impact {r['impact_bps_per_leg']:.2f} bps/leg")
    print(f"  search       {r['cells_per_refit']} distinct gated rules per "
          f"refit x {len(r['segments'])} refits = {r['cells_examined']} "
          f"({r['seconds']:.2f}s, {core.version()})")
    print(f"  axes         hold_days x negative_exit_prints x ewma_alpha "
          "(entry_bps is NOT read by the gated rule)")
    print(f"  registered   {trial_id}")
    print()
    print(BANNER)
    print()
    print(f"  {'line':<14}{'net':>11}{'%/yr':>9}{'+segs':>8}"
          f"{'median mo':>11}{'worst mo':>10}{'trades':>8}{'fees':>10}"
          f"{'in mkt':>8}")
    for name, key in (("FROZEN", "frozen"), ("WALK-FORWARD", "walk_forward"),
                      ("ORACLE", "oracle")):
        ln = r[key]
        print(f"  {name:<14}{ln['net_usd']:>11,.0f}"
              f"{ln['net_annualised_pct']:>+8.2f}%"
              f"{ln['positive_segments']:>5}/{len(r['segments']):<2}"
              f"{ln['median_segment_usd']:>11,.0f}"
              f"{ln['worst_segment_usd']:>10,.0f}"
              f"{ln['trades']:>8,.0f}{-ln['fees_usd']:>10,.0f}"
              f"{ln['market_exposure_pct']:>7.0f}%")
    print()
    print("  WHERE THE MONEY COMES FROM  (share of net earned by the best "
          "months)")
    for name, key in (("FROZEN", "frozen"), ("WALK-FORWARD", "walk_forward"),
                      ("ORACLE", "oracle")):
        ln = r[key]
        print(f"    {name:<14} best month {ln['top1_share']:5.1%}   "
              f"best 3 {ln['top3_share']:5.1%}   best 6 of "
              f"{len(r['segments'])} {ln['top6_share']:5.1%}")
    print("    A carry book earns in spikes. A mean that hides that is a mean "
          "nobody can size against.")
    print()
    fz = r["frozen_cell"]
    orc = r["oracle_cell"]
    print(f"  FROZEN       hold {fz['hold_days']:.0f}d, exit "
          f"{fz['negative_exit_prints']} prints, alpha {fz['ewma_alpha']:.1f}"
          "   (carry_costs / carry_engine, as shipped)")
    print(f"  ORACLE       hold {orc['hold_days']:.0f}d, exit "
          f"{orc['negative_exit_prints']} prints, alpha "
          f"{orc['ewma_alpha']:.1f}   (whole window visible — NOT adoptable)")
    print(f"  hindsight premium  ${r['hindsight_premium_usd']:,.0f} "
          "— what seeing the whole window was worth over refitting blind")
    print(f"  choice stability   {r['distinct_choices']} distinct cells "
          f"chosen across {len(r['segments'])} refits")

    print()
    print("  HOW LONG ONE SHORT IS HELD")
    for name, key in (("FROZEN", "frozen"), ("WALK-FORWARD", "walk_forward"),
                      ("ORACLE", "oracle")):
        ln = r[key]
        print(f"    {name:<14}{ln['mean_hold_days']:6.0f} days per trade "
              f"({ln['trades']:,.0f} trades, {ln['market_exposure_pct']:.0f}% "
              "of the window in the market)")
    print("    The cheap line is cheap because it STOPS CHURNING, and it stops")
    print("    churning by holding one short for months. Over that horizon the")
    print("    adverse excursion is the price rise itself — measured +168% on")
    print("    this corpus — which the short's margin must absorb from the")
    print("    spot collateral or be liquidated. Whether it can is INVENTORY")
    print("    F4, which is open: positionIM/positionMM is a risk-tier ratio,")
    print("    not liquidation distance. These are not the same product and")
    print("    the net column alone cannot choose between them.")
    print()
    print("  DEFLATED SHARPE — the observed Sharpe against the best a search "
          "this wide")
    print("  would be expected to produce from rules with no edge at all.")
    for name, key, trials in (
            ("walk-forward", "walk_forward_segment_net", r["cells_examined"]),
            ("frozen (>=)", "frozen_segment_net", len(HOLD_DAYS)
             * len(EXIT_PRINTS) * len(EWMA_ALPHA))):
        out = deflated(r, r[key], trials)
        r.setdefault("deflated_sharpe", {})[name] = out
        if out is None:
            continue
        print(f"    {name:<13} Sharpe {out.get('observed_sharpe', 0):+.3f} "
              f"vs expected-max {out.get('expected_max_sharpe_under_null', 0):+.3f} "
              f"under {trials:,} trial(s)  ->  DSR {out.get('dsr', 0):.3f}, "
              f"z {out.get('z', 0):+.3f}")
        if out.get("z", 0) <= 0:
            print("                  THE SEARCH EXPLAINS IT. On "
                  f"{out.get('n_observations', 0):.0f} monthly observations a "
                  "Sharpe this size is\n                  what the BEST of "
                  "that many edgeless rules would have shown.")
        print(f"                  skew {out.get('skew', 0):+.2f}, kurtosis "
              f"{out.get('kurtosis', 0):.1f} — the months are not normal and "
              "the\n                  Sharpe is a poor summary of them; see "
              "the concentration above.")
    print("    The frozen line is charged one grid of trials, NOT one. Its")
    print("    constants were not pre-registered: they were chosen after 0018")
    print("    with this corpus visible, so the true trial count is at least")
    print("    this and its evidence is an UPPER bound.")

    print()
    print("  QUOTABLE: False")
    print(f"    - {r['why_not_quotable']}")
    print("    - and the deflated Sharpe says why it MATTERS rather than "
          "being a formality:")
    print("      a wide enough search finds a +13%/yr line in noise. Only "
          "settlements")
    print("      that had not printed when the rule was fixed can tell the "
          "two apart.")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(r, fh, indent=2)
        print(f"\n  wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
