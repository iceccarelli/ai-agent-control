"""Proposed risk limits and what each would have done, for the OWNER to choose from. Activates nothing; makes no request; reads only a funding history file.

    python3 tools/risk_policy_proposal.py [--funding FILE.json.gz] [--notional-usd 100] [--json]

Every number is derived from the committed Bybit BTCUSDT funding history (a measured, in-sample series). A limit tied to what the HISTORY did is a proposal about
funding-only risk on a delta-neutral short: it does NOT cover basis, execution failure, margin or liquidation risk, which have no series here and are labelled
UNMEASURED. Each limit is offered at three settings with its financial trade-off (how often it would have stopped the position, and what each stop costs in fees).
The recommended setting is a recommendation, not a decision: ``risk_policy.Policy.approved_by_owner`` stays False until the owner sets it.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import carry_costs as CC  # noqa: E402

DEFAULT_FUNDING = os.path.join(BOT, "research", "exchange_study", "bybit", "funding_BTCUSDT.json.gz")
SETTLEMENTS_PER_DAY = 3
WINDOW_30D = 90


def load_bps(path: str) -> List[float]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        rows = json.load(fh)
    pairs = sorted((int(r["fundingRateTimestamp"]), float(r["fundingRate"]) * 1e4) for r in rows)
    return [v for _t, v in pairs]


def _pct(xs: Sequence[float], p: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))]


def window_drawdowns_bps(series: Sequence[float], window: int) -> List[float]:
    """For every start, the worst peak-to-trough fall (bps of notional, >= 0) of cumulative funding received over the next `window` settlements."""
    out: List[float] = []
    for i in range(0, len(series) - window + 1):
        cum = peak = 0.0
        worst = 0.0
        for v in series[i:i + window]:
            cum += v
            peak = max(peak, cum)
            worst = max(worst, peak - cum)
        out.append(worst)
    return out


def rolling_mean_shortfall_share(series: Sequence[float], window: int, required_bps: float) -> float:
    """Share of dates (with a full window behind them) on which the trailing mean funding was below the required level."""
    if len(series) < window:
        return float("nan")
    s = sum(series[:window]); n = below = 0
    for i in range(window, len(series) + 1):
        n += 1
        below += 1 if s / window < required_bps else 0
        if i < len(series):
            s += series[i] - series[i - window]
    return below / n


def propose(series: Sequence[float], *, notional_usd: float) -> Dict[str, Any]:
    if len(series) < 2 * WINDOW_30D:
        raise ValueError("not enough funding history to propose limits from")
    rt_bps = 2 * CC.TAKER_BPS_PERP                       # overlay: perp legs only (ASSUMED taker schedule)
    rt_usd = notional_usd * rt_bps / 1e4
    dd = window_drawdowns_bps(series, WINDOW_30D)
    import carry_prospective as CP           # the frozen plan's own requirement (read only)
    required = CP.required_bps(10.0)
    opts = []
    for label, p in (("tight", 0.50), ("balanced", 0.95), ("conservative", 1.0)):
        lim_bps = _pct(dd, p)
        lim_usd = notional_usd * lim_bps / 1e4
        # a stop fires when a window's drawdown EXCEEDS the limit: share of historical 30-day windows that would have stopped, and what the stop costs
        breaches = sum(1 for x in dd if x > lim_bps) / len(dd)
        opts.append({"setting": label, "strategy_drawdown_limit_bps_of_notional": round(lim_bps, 2), "strategy_drawdown_limit_usd": round(lim_usd, 4),
                     "historical_30d_windows_that_would_have_stopped": round(breaches, 4),
                     "cost_per_stop_usd": round(rt_usd, 4),
                     "trade_off": "tighter = stops more often and pays the round trip each time; looser = tolerates a deeper funding-only drawdown before stopping"})
    mean_all = sum(series) / len(series)
    last = series[-1095:]
    be_settlements = rt_bps / (sum(last) / len(last)) if sum(last) > 0 else None
    return {
        "inputs": {"history_prints": len(series), "funding_mean_bps_all": round(mean_all, 4), "funding_mean_bps_last_365d": round(sum(last) / len(last), 4),
                   "notional_usd": notional_usd, "round_trip_bps_overlay_ASSUMED": rt_bps, "label": "measured history; assumed cost schedule"},
        "strategy_drawdown_funding_only": {"options": opts, "recommended": "balanced",
                                           "scope": "FUNDING ONLY on a delta-neutral short. Basis, execution failure and margin risk are UNMEASURED here."},
        "max_hold": {"break_even_settlements_at_last_365d_mean": None if be_settlements is None else round(be_settlements, 1),
                     "break_even_days": None if be_settlements is None else round(be_settlements / SETTLEMENTS_PER_DAY, 1),
                     "options_days": [14, 30, 60], "recommended_days": 30,
                     "trade_off": "shorter holds pay the round trip more often; longer holds carry more exposure to a funding-regime change. The break-even is only the point where funding has covered the assumed round trip."},
        "persistent_shortfall": {"required_funding_bps_for_10pct_hurdle": None if required is None else round(required, 4),
                                 "share_of_history_below_required_trailing_90_prints": round(rolling_mean_shortfall_share(series, WINDOW_30D, required), 4) if required else None,
                                 "rule": "suspend NEW entries while the trailing 90-print mean funding is below the hurdle-implied requirement",
                                 "consequence": "if that share is near 1, the rule would have kept the strategy out of the market almost always: the honest reading is that the hurdle is not being met, not that the rule is too tight"},
        "unmeasured_limits": ["per_trade_loss_usd", "daily_loss_usd", "portfolio_drawdown_usd", "max_unhedged_usd", "min_margin_multiple", "min_liquidation_distance_pct",
                              "min_liquidity_usd", "max_data_age_s", "max_correlated_exposure_usd"],
        "unmeasured_why": "no series in this repository measures them; they need execution records, venue margin facts and depth data (the recorder and testnet session produce these)",
        "decision": "OWNER DECISION REQUIRED. risk_policy.Policy.approved_by_owner stays False; capital stays ZERO.",
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Proposed risk limits with their financial trade-offs. Activates nothing.")
    ap.add_argument("--funding", default=DEFAULT_FUNDING)
    ap.add_argument("--notional-usd", type=float, default=100.0)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        rep = propose(load_bps(a.funding), notional_usd=a.notional_usd)
    except (OSError, ValueError, KeyError) as exc:
        print(f"risk_policy_proposal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(rep, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
