#!/usr/bin/env python3
"""Taker vs maker-first for the carry hedge: a SENSITIVITY, not a measurement.

WHAT THIS IS
============
The overlay opens and closes ONE perp leg each way. `CarryEngine` can send it
as a taker order or rest it first (MAKER_FIRST, `_rest_then_take`) and cross
whatever is left after `maker_wait_s`. The fee difference is easy to state
(5.5 vs 2.0 bps a leg on Bybit's base tier); what it is WORTH is not, because a
resting order can fail to fill, fill in part, fill exactly when the price is
about to move against it, and leave the book waiting while the market drifts.

This tool prices those effects from EXPLICIT, PARAMETERISED ASSUMPTIONS and
reports where maker-first stops beating taker: the break-even fill probability
and the break-even adverse selection. It is arithmetic on stated inputs.

WHAT THIS IS NOT
================
It is NOT a measurement. This repository holds NO measured fill data for the
carry book: no post-only fill rates, no queue positions, no fill-time markouts,
no partial-fill distribution. Every default below is a placeholder chosen to be
visible, not an estimate, and the output says `measured_fill_data_used: false`
on every line. Nothing here changes the production execution style, and
nothing here may be quoted as "maker-first saves N bps": the most it can say is
"IF the fill probability is at least p*, and adverse selection at most a*, then
maker-first is cheaper under these assumptions".

THE MODEL (per leg, in bps of the leg's notional)
=================================================
    taker   = T + I
    maker   =   p       * [ f * (M + A) + (1 - f) * (T + I + D) ]     filled (in part)
              + (1 - p) * [ T + I + D + X ]                            missed: crossed late
              + L * c                                                  waiting L prints
              + h * (T + I)                                            final hedge mismatch

    T  taker fee            M  maker fee            I  taker impact / half spread
    p  fill probability     f  share filled at maker, given a fill
    A  adverse selection on the maker-filled quantity
    D  price drift while resting, on the quantity crossed after the wait
    X  extra cost of a missed fill (the market has left: wider cross)
    L  fill delay in funding prints; c the carry cost per print of waiting:
       at ENTRY the funding not yet earned, at EXIT the (inverted) funding
       still paid, i.e. max(0, -exit funding)
    h  share of the leg left mismatched at the end and corrected at taker

Everything is linear in p and in A, so the break-evens are closed form:

    p*  = (Y + C - taker) / (Y - X1)      Y  = T+I+D+X, X1 = f(M+A)+(1-f)(T+I+D)
    A*  = A + (taker - maker) / (p f)     C  = L c + h (T+I)

    python3 tools/execution_economics.py
    python3 tools/execution_economics.py --fill-probability 0.6 --adverse-selection-bps 2
    python3 tools/execution_economics.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import asdict, dataclass, replace
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

LABEL = ("SENSITIVITY UNDER STATED ASSUMPTIONS - NOT A MEASUREMENT. No measured "
         "fill data exists in this repository; every input is an assumption.")

#: Placeholder maker fee, bps per leg. Bybit's base-tier linear maker fee, the
#: figure the 0040 maker-first upper bound used. An ASSUMPTION here: the live
#: engine reads the account's actual tier from the venue.
ASSUMED_MAKER_BPS = 2.0


@dataclass(frozen=True)
class LegAssumptions:
    """One leg's execution assumptions. Every field is an input, none is data."""

    taker_bps: float
    maker_bps: float = ASSUMED_MAKER_BPS
    taker_impact_bps: float = 0.0
    fill_probability: float = 0.5
    partial_fill_share: float = 1.0
    adverse_selection_bps: float = 0.0
    price_drift_bps: float = 0.0
    missed_fill_cost_bps: float = 0.0
    fill_delay_prints: float = 0.0
    carry_cost_per_print_bps: float = 0.0
    final_hedge_mismatch_share: float = 0.0

    def problems(self) -> List[str]:
        bad: List[str] = []
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value):
                bad.append(f"{name} is not a finite number")
        if bad:
            return bad
        for name in ("fill_probability", "partial_fill_share",
                     "final_hedge_mismatch_share"):
            if not 0.0 <= getattr(self, name) <= 1.0:
                bad.append(f"{name} must be within [0, 1]")
        for name in ("taker_bps", "maker_bps", "taker_impact_bps",
                     "fill_delay_prints", "carry_cost_per_print_bps",
                     "missed_fill_cost_bps"):
            if getattr(self, name) < 0:
                bad.append(f"{name} cannot be negative")
        # adverse selection and drift are signed on purpose: a favourable
        # drift is an assumption someone may want to state, and is labelled.
        return bad


def _check(a: LegAssumptions) -> None:
    bad = a.problems()
    if bad:
        raise ValueError("; ".join(bad))


def taker_leg_bps(a: LegAssumptions) -> float:
    _check(a)
    return a.taker_bps + a.taker_impact_bps


def _parts(a: LegAssumptions) -> Dict[str, float]:
    cross = a.taker_bps + a.taker_impact_bps
    filled = (a.partial_fill_share * (a.maker_bps + a.adverse_selection_bps)
              + (1.0 - a.partial_fill_share) * (cross + a.price_drift_bps))
    missed = cross + a.price_drift_bps + a.missed_fill_cost_bps
    fixed = (a.fill_delay_prints * a.carry_cost_per_print_bps
             + a.final_hedge_mismatch_share * cross)
    return {"cross": cross, "filled": filled, "missed": missed, "fixed": fixed}


def maker_first_leg_bps(a: LegAssumptions) -> float:
    _check(a)
    k = _parts(a)
    p = a.fill_probability
    return p * k["filled"] + (1.0 - p) * k["missed"] + k["fixed"]


def breakeven_fill_probability(a: LegAssumptions) -> Dict[str, Any]:
    """The fill probability at which maker-first costs exactly what taker does.

    `value` is None when no probability in [0, 1] breaks even, and `regime`
    says which side wins everywhere."""
    _check(a)
    k = _parts(a)
    slope = k["filled"] - k["missed"]            # d(maker)/dp
    gap_at_zero = k["missed"] + k["fixed"] - k["cross"]   # maker - taker at p=0
    if slope == 0.0:
        return {"value": None,
                "regime": ("maker_first_never_cheaper" if gap_at_zero >= 0
                           else "maker_first_always_cheaper"),
                "note": "a fill costs the same as a miss: p does not matter"}
    p_star = -gap_at_zero / slope
    if slope > 0:
        # filling is WORSE than missing (adverse selection beyond the cross)
        regime = ("maker_first_never_cheaper" if p_star <= 0
                  else "maker_first_always_cheaper" if p_star > 1
                  else "maker_first_cheaper_only_below")
    else:
        regime = ("maker_first_always_cheaper" if p_star <= 0
                  else "maker_first_never_cheaper" if p_star > 1
                  else "maker_first_cheaper_above")
    p_star = p_star + 0.0                       # -0.0 -> 0.0
    value = (None if regime == "maker_first_never_cheaper"
             or not 0.0 <= p_star <= 1.0 else p_star)
    return {"value": value, "unclamped": p_star, "regime": regime,
            "note": ("p* = 0 means a missed fill costs no more than crossing "
                     "at once, so resting is never worse; that is a property "
                     "of the zero drift / miss / delay assumptions, not a "
                     "finding") if value == 0.0 else ""}


def breakeven_adverse_selection_bps(a: LegAssumptions) -> Dict[str, Any]:
    """The adverse selection on maker fills at which maker-first stops winning.
    None when no maker fill is assumed (p * f == 0): then A does not enter."""
    _check(a)
    weight = a.fill_probability * a.partial_fill_share
    if weight == 0.0:
        return {"value": None, "note": "no maker-filled quantity is assumed, "
                                       "so adverse selection cannot matter"}
    gap = taker_leg_bps(a) - maker_first_leg_bps(a)
    return {"value": a.adverse_selection_bps + gap / weight}


def compare(entry: LegAssumptions, exit_: LegAssumptions, *,
            notional_usd: float = 100_000.0,
            hold_days: Optional[float] = None) -> Dict[str, Any]:
    """The overlay round trip (one perp leg in, one out) both ways."""
    if hold_days is None:
        import carry_costs
        hold_days = carry_costs.DEMONSTRATED_HOLD_DAYS
    if not (math.isfinite(notional_usd) and notional_usd > 0):
        raise ValueError("notional_usd must be a positive number")
    if not (math.isfinite(hold_days) and hold_days > 0):
        raise ValueError("hold_days must be a positive number")
    legs = {}
    for name, a in (("entry", entry), ("exit", exit_)):
        taker = taker_leg_bps(a)
        maker = maker_first_leg_bps(a)
        legs[name] = {
            "assumptions": asdict(a),
            "taker_bps": taker, "maker_first_bps": maker,
            "maker_first_saving_bps": taker - maker,
            "breakeven_fill_probability": breakeven_fill_probability(a),
            "breakeven_adverse_selection_bps":
                breakeven_adverse_selection_bps(a),
        }
    taker_rt = legs["entry"]["taker_bps"] + legs["exit"]["taker_bps"]
    maker_rt = legs["entry"]["maker_first_bps"] + legs["exit"]["maker_first_bps"]
    saving = taker_rt - maker_rt
    return {
        "label": LABEL,
        "measured_fill_data_used": False,
        "production_execution_style_changed": False,
        "execution_mode": "overlay",
        # carry_engine: only the two ENTRY paths may rest (patient=True);
        # every exit crosses at once whatever execution_style says. The exit
        # leg here is therefore a hypothetical the engine does not run.
        "production_exit_style": "taker_always",
        "exit_leg_is_hypothetical": True,
        "legs": legs,
        "round_trip": {
            "taker_bps": taker_rt, "maker_first_bps": maker_rt,
            "maker_first_saving_bps": saving,
            "maker_first_cheaper": saving > 0,
            "saving_usd_per_round_trip": saving / 1e4 * notional_usd,
            "saving_bps_per_day_over_hold": saving / hold_days,
            "hold_days": hold_days,
        },
        "notional_usd": notional_usd,
    }


def sensitivity_grid(base: LegAssumptions, *, fill_probabilities: List[float],
                     adverse_selection_bps: List[float]) -> List[Dict[str, Any]]:
    """maker-first saving per leg across p x A, everything else as `base`."""
    out = []
    for p in fill_probabilities:
        for adv in adverse_selection_bps:
            a = replace(base, fill_probability=p, adverse_selection_bps=adv)
            out.append({"fill_probability": p, "adverse_selection_bps": adv,
                        "maker_first_saving_bps":
                            taker_leg_bps(a) - maker_first_leg_bps(a)})
    return out


def _leg_from_args(args: argparse.Namespace, *, carry_cost: float
                   ) -> LegAssumptions:
    return LegAssumptions(
        taker_bps=args.taker_bps, maker_bps=args.maker_bps,
        taker_impact_bps=args.taker_impact_bps,
        fill_probability=args.fill_probability,
        partial_fill_share=args.partial_fill_share,
        adverse_selection_bps=args.adverse_selection_bps,
        price_drift_bps=args.price_drift_bps,
        missed_fill_cost_bps=args.missed_fill_cost_bps,
        fill_delay_prints=args.fill_delay_prints,
        carry_cost_per_print_bps=carry_cost,
        final_hedge_mismatch_share=args.final_hedge_mismatch_share)


def main(argv: Optional[List[str]] = None) -> int:
    import carry_costs
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--taker-bps", type=float, default=carry_costs.TAKER_BPS_PERP)
    ap.add_argument("--maker-bps", type=float, default=ASSUMED_MAKER_BPS)
    ap.add_argument("--taker-impact-bps", type=float, default=0.0)
    ap.add_argument("--fill-probability", type=float, default=0.5)
    ap.add_argument("--partial-fill-share", type=float, default=1.0,
                    help="share filled at maker, given a fill")
    ap.add_argument("--adverse-selection-bps", type=float, default=0.0)
    ap.add_argument("--price-drift-bps", type=float, default=0.0,
                    help="adverse drift while resting, on what is crossed late")
    ap.add_argument("--missed-fill-cost-bps", type=float, default=0.0)
    ap.add_argument("--fill-delay-prints", type=float, default=0.0)
    ap.add_argument("--entry-funding-bps", type=float, default=0.0,
                    help="funding per print not earned while the entry rests")
    ap.add_argument("--exit-funding-bps", type=float, default=0.0,
                    help="funding per print at exit (negative = paid while "
                         "the exit rests)")
    ap.add_argument("--final-hedge-mismatch-share", type=float, default=0.0)
    ap.add_argument("--notional", type=float, default=100_000.0)
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)

    entry = _leg_from_args(args, carry_cost=max(0.0, args.entry_funding_bps))
    exit_ = _leg_from_args(args, carry_cost=max(0.0, -args.exit_funding_bps))
    try:
        report = compare(entry, exit_, notional_usd=args.notional)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    report["grid"] = sensitivity_grid(
        entry, fill_probabilities=[0.1, 0.25, 0.5, 0.75, 0.9, 1.0],
        adverse_selection_bps=[0.0, 1.0, 2.0, 3.5, 5.0])

    print(LABEL)
    print("measured_fill_data_used: false   production style: unchanged\n")
    for name in ("entry", "exit"):
        leg = report["legs"][name]
        p = leg["breakeven_fill_probability"]
        a = leg["breakeven_adverse_selection_bps"]
        print(f"  {name:<5} taker {leg['taker_bps']:6.2f} bps   maker-first "
              f"{leg['maker_first_bps']:6.2f} bps   saving "
              f"{leg['maker_first_saving_bps']:+6.2f}")
        print(f"        break-even fill probability: "
              f"{'n/a' if p['value'] is None else format(p['value'], '.3f')}"
              f" ({p['regime']})")
        print(f"        break-even adverse selection: "
              f"{'n/a' if a['value'] is None else format(a['value'], '.2f') + ' bps'}")
    rt = report["round_trip"]
    print(f"\n  round trip: taker {rt['taker_bps']:.2f} bps, maker-first "
          f"{rt['maker_first_bps']:.2f} bps, saving {rt['maker_first_saving_bps']:+.2f}"
          f" bps = ${rt['saving_usd_per_round_trip']:+,.2f} on "
          f"${report['notional_usd']:,.0f}, {rt['saving_bps_per_day_over_hold']:+.3f}"
          f" bps/day over a {rt['hold_days']}-day hold")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
