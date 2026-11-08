#!/usr/bin/env python3
"""Economic teardown of the BTC funding-carry strategy, from real public history. Offline, read-only, order-less.

    python3 tools/carry_teardown.py [--funding F.json.gz] [--perp-4h P.json.gz] [--spot-4h S.json.gz]
                                    [--round-trip-bps 11.4] [--reserve 0.10] [--json]
                                    [--owner-hurdle-pct X --owner-max-drawdown-pct Y ...]

The question it answers is the first economic one: does a short BTCUSDT perpetual against owned BTC collect more funding
than it costs to hold, persistently enough to matter on fully allocated capital, in the market that actually existed?

Every number carries exactly one provenance tag:

  PUBLIC_OBSERVATION  read from the committed public Bybit history (funding prints, 4h perp and spot klines, risk-limit table).
                      Public, observed, IN-SAMPLE, not account data and not tick-level.
  SIMULATED           computed by replaying a declared rule over that history (a rolling-start holding simulation).
  ASSUMED             a declared input that nobody measured (the 11.4 bps round trip = 11.0 published two-leg perp taker fee
                      + 0.4 ASSUMED spread allowance, see carry_costs; not the owner's account fee tier; the 10% reserve; the holding period).
  UNKNOWN             a required input that is not available. Never defaulted to a favourable value.
  MEASURED / ACCOUNT_VERIFIED   exist as tags and are NEVER produced here: no measurement of fills and no account record is read.

Costs enter exactly where the engine charges them (``trade_economics``): two perpetual taker legs, plus spread and slippage. The
BTC is already owned in the overlay, so no spot trades are charged; the opportunity cost of holding that BTC is a separate,
owner-defined number (``alternative_return``). Basis P&L is taken from 4h kline closes and opens (the entry bar ENDS at the first
settled funding time, the exit bar OPENS at the last), so no future price is used and the resolution is coarse: intra-bar basis
excursions are not seen. Latency, market impact, borrow and operational cost are UNKNOWN and are listed, not zero.

Rolling-start windows overlap heavily. Their count is NOT the sample size: the report prints the number of non-overlapping windows
and the effective sample size, and sizes a future study with ``statistical_power``.

Nothing here authorises trading; the capital recommendation printed is ZERO unless a predeclared condition holds, and even then it
is a research recommendation.
"""
from __future__ import annotations

import argparse
import datetime
import gzip
import json
import math
import os
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import carry_costs as CC               # noqa: E402  (the one definition of every round-trip cost figure)
import evidence_stats as ES            # noqa: E402
import statistical_power as SP         # noqa: E402
import trade_economics as TE           # noqa: E402

STUDY = os.path.join(BOT, "research", "exchange_study", "bybit")
SETTLE_MS = 8 * 3_600_000
BAR_MS = 4 * 3_600_000
PER_DAY = 3
TAGS = ("MEASURED", "ACCOUNT_VERIFIED", "PUBLIC_OBSERVATION", "SIMULATED", "ASSUMED", "UNKNOWN")
#: 11.4 = 11.0 (2 perp taker legs, published base tier: venue_stated) + 0.4 (ASSUMED spread allowance, unmeasured). NOT the owner's account tier.
DEFAULT_ROUND_TRIP_BPS = CC.ROUND_TRIP_TOTAL_COST_BPS_PERP_OVERLAY
COST_SCENARIOS_BPS = (6.0, DEFAULT_ROUND_TRIP_BPS, 17.0)
HOLD_DAYS = (7, 14, 30, 60, 90)
HURDLES_PCT = TE.SCREENING_HURDLES_PCT
FAMILY_TRIALS = 16                      # the opportunity engine's predeclared variant family


class DataError(ValueError):
    """The supplied history is not usable evidence."""


def _fin(x: Any) -> bool:
    """A real, finite number (not a bool); an int beyond float range is not one and must not raise."""
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return False
    try:
        return math.isfinite(x)
    except OverflowError:
        return False


# ---------------------------------------------------------------- loading and validation

def _open_json(path: str) -> Any:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def parse_funding(rows: Any) -> Tuple[List[int], List[float]]:
    """Funding prints as ``[{'fundingRate': '0.0001', 'fundingRateTimestamp': '...'}]`` -> (timestamps ms, rate in bps). Strictly
    increasing, finite, on the 8h UTC grid; duplicates and off-grid stamps are errors, gaps are allowed and handled downstream."""
    if not isinstance(rows, list) or not rows:
        raise DataError("funding history is empty or not a list")
    out = []
    for i, r in enumerate(rows):
        try:
            t, x = int(r["fundingRateTimestamp"]), float(r["fundingRate"]) * 1e4
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise DataError(f"funding row {i} is malformed: {exc!r}") from exc
        if not math.isfinite(x) or abs(x) > 300.0:
            raise DataError(f"funding row {i}: {x} bps is not a plausible per-settlement rate")
        if t % SETTLE_MS != 0:
            raise DataError(f"funding row {i}: timestamp {t} is not on the 8h UTC settlement grid")
        out.append((t, x))
    out.sort()
    ts = [t for t, _ in out]
    if len(set(ts)) != len(ts):
        raise DataError("duplicate funding timestamps")
    return ts, [x for _, x in out]


def parse_klines(rows: Any) -> Dict[int, Tuple[float, float]]:
    """4h klines ``[start_ms, open, high, low, close, ...]`` -> ``{start_ms: (open, close)}``; positive finite prices, 4h grid."""
    if not isinstance(rows, list) or not rows:
        raise DataError("kline history is empty or not a list")
    out: Dict[int, Tuple[float, float]] = {}
    for i, r in enumerate(rows):
        try:
            t, o, h, lo, c = int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])
        except (IndexError, KeyError, TypeError, ValueError, OverflowError) as exc:
            raise DataError(f"kline row {i} is malformed: {exc!r}") from exc
        if not all(math.isfinite(x) and x > 0 for x in (o, h, lo, c)) or t % BAR_MS != 0:
            raise DataError(f"kline row {i} is not a valid 4h bar")
        if not (lo <= min(o, c) and max(o, c) <= h):
            raise DataError(f"kline row {i}: low/high do not bracket the open and close")
        if t in out:
            raise DataError("duplicate kline start times")
        out[t] = (o, c)
    return out


def load_study(funding_path: Optional[str] = None, perp_path: Optional[str] = None, spot_path: Optional[str] = None):
    ts, f = parse_funding(_open_json(funding_path or os.path.join(STUDY, "funding_BTCUSDT.json.gz")))
    perp = parse_klines(_open_json(perp_path or os.path.join(STUDY, "linear_BTCUSDT_240.json.gz")))
    spot = parse_klines(_open_json(spot_path or os.path.join(STUDY, "spot_BTCUSDT_240.json.gz")))
    return ts, f, perp, spot


# ---------------------------------------------------------------- closed-form economics

def breakeven(cost_bps: float, funding_bps: float) -> Dict[str, Any]:
    """Settlements and days of UNINTERRUPTED funding at ``funding_bps`` per settlement to recover ``cost_bps``. None if funding <= 0."""
    if not (math.isfinite(cost_bps) and cost_bps >= 0 and math.isfinite(funding_bps)):
        raise ValueError("cost must be non-negative and funding finite")
    if funding_bps <= 0:
        return {"settlements": None, "days": None, "note": "funding is not positive: the cost is never recovered"}
    n = cost_bps / funding_bps
    return {"settlements": n, "days": n / PER_DAY}


def net_return(funding_bps: float, hold_days: float, cost_bps: float, reserve: float) -> Dict[str, float]:
    """Net period and annualised (simple) return for a position held ``hold_days`` collecting ``funding_bps`` per settlement.
    Hedged notional N, economic capital N * (1 + reserve) (the BTC is owned; the reserve is idle USDT)."""
    net_bps = funding_bps * PER_DAY * hold_days - cost_bps
    ann_notional = net_bps / 1e4 * 365.0 / hold_days * 100.0
    return {"net_bps": net_bps, "annualised_pct_on_notional": ann_notional,
            "annualised_pct_on_capital": ann_notional / (1.0 + reserve)}


def required_funding(hurdle_pct: float, hold_days: float, cost_bps: float, reserve: float) -> float:
    """Average funding per settlement (bps) at which the annualised return on FULLY ALLOCATED capital equals ``hurdle_pct``."""
    need_net_bps = hurdle_pct / 100.0 * (1.0 + reserve) * hold_days / 365.0 * 1e4
    return (cost_bps + need_net_bps) / (PER_DAY * hold_days)


def _q(sorted_xs: Sequence[float], p: float) -> float:
    return sorted_xs[min(len(sorted_xs) - 1, max(0, int(p * (len(sorted_xs) - 1))))]


def _year(ms: int) -> int:
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).year


# ---------------------------------------------------------------- history statistics

def settlement_stats(ts: Sequence[int], f: Sequence[float], baseline_bps: float = 1.0) -> Dict[str, Any]:
    def block(xs: Sequence[float]) -> Dict[str, Any]:
        s = sorted(xs)
        n = len(xs)
        return {"n": n, "mean_bps": sum(xs) / n, "median_bps": _q(s, 0.5), "p05_bps": _q(s, 0.05), "p95_bps": _q(s, 0.95),
                "share_negative": sum(1 for x in xs if x < 0) / n,
                "share_at_or_below_baseline": sum(1 for x in xs if x <= baseline_bps + 1e-9) / n,
                "max_bps": s[-1], "annualised_gross_pct_on_notional": sum(xs) / n * PER_DAY * 365 / 1e4 * 100.0}
    years: Dict[int, List[float]] = {}
    for t, x in zip(ts, f):
        years.setdefault(_year(t), []).append(x)
    out = {"all": block(f), "by_year": {y: block(v) for y, v in sorted(years.items())},
           "last_365d": block(f[-365 * PER_DAY:]), "last_180d": block(f[-180 * PER_DAY:]), "last_90d": block(f[-90 * PER_DAY:]),
           "baseline_bps": baseline_bps}
    out["acf"] = {k: ES.autocorr(list(f), k) for k in (1, 2, 3, 6, 9, 21, 30)}
    out["effective_sample_size"] = ES.conservative_ess(list(f))
    out["ess_ratio"] = out["effective_sample_size"] / len(f)
    runs: List[int] = []
    cur, sign = 0, None
    for x in f:
        s = x > 0
        if s == sign:
            cur += 1
        else:
            if cur:
                runs.append(cur)
            cur, sign = 1, s
    runs.append(cur)
    out["sign_regime_runs"] = {"count": len(runs), "mean_len_settlements": sum(runs) / len(runs), "max_len_settlements": max(runs)}
    return out


def _window(ts: Sequence[int], f: Sequence[float], perp: Mapping[int, Tuple[float, float]], spot: Mapping[int, Tuple[float, float]],
            i: int, m: int) -> Optional[Tuple[float, float, float, float]]:
    """One holding window of ``m`` settlements starting at index ``i``, for a FIXED BTC quantity, every term in bps of the ENTRY notional
    N = Q * s_in: ``(funding, basis_in, basis_out, worst_basis)``. Entry is at the close of the 4h bar that ENDS at the first settlement,
    exit at the open of the bar that STARTS at the last, so no later price is used. None when any bar the window needs is missing."""
    b_in, b_out = ts[i] - BAR_MS, ts[i + m - 1]
    if b_in not in perp or b_in not in spot or b_out not in perp or b_out not in spot:
        return None
    if any(ts[k] not in perp for k in range(i, i + m)):               # funding is marked at the perp open of each settlement
        return None
    s_in = spot[b_in][1]
    basis_in = (perp[b_in][1] - s_in) / s_in * 1e4
    basis_out = (perp[b_out][0] - spot[b_out][0]) / s_in * 1e4        # (P_out - S_out) / s_in: the exit basis over the ENTRY spot
    fund = sum(f[k] * (perp[ts[k]][0] / s_in) for k in range(i, i + m))   # rate x the perp open at each settlement, per entry notional
    worst = basis_in
    for k in range(i, i + m):                                         # worst (highest) basis seen at a bar OPEN during the hold
        bk = ts[k]
        if bk in spot:
            worst = max(worst, (perp[bk][0] - spot[bk][0]) / s_in * 1e4)
    return fund, basis_in, basis_out, worst


def rolling_starts(ts: Sequence[int], f: Sequence[float], perp: Mapping[int, Tuple[float, float]], spot: Mapping[int, Tuple[float, float]],
                   hold_days: int, *, first_start_ms: Optional[int] = None, costs_bps: Sequence[float] = COST_SCENARIOS_BPS) -> Dict[str, Any]:
    """Hold the hedge for ``hold_days`` from EVERY possible start settlement and report the distribution of what it would have earned.

    Entry is at the close of the 4h bar that ENDS at the first settlement being collected; exit is at the OPEN of the bar that
    STARTS at the last settlement collected, so the position is held through every settlement counted and no later price is used.
    A window with a missing settlement or a missing bar is excluded and counted, never bridged. The P&L is
    funding + (entry basis - exit basis) - cost, in bps of the ENTRY hedged notional (short perp, long spot). The hedge is a fixed BTC
    quantity: funding is rate x the perp open at each settlement (so it grows when the price rises) and the exit basis is divided by the
    ENTRY spot. Costs stay a constant bps of entry notional (the exit leg's notional differs by the price ratio: a stated approximation)."""
    m = hold_days * PER_DAY
    idx = {t: i for i, t in enumerate(ts)}
    funding_only: List[float] = []
    with_basis: List[float] = []
    starts: List[int] = []
    excluded = 0
    adverse: List[float] = []
    for i in range(len(ts) - m + 1):
        if ts[i + m - 1] - ts[i] != (m - 1) * SETTLE_MS:
            excluded += 1                                                  # a gap inside the window
            continue
        if first_start_ms is not None and ts[i] < first_start_ms:
            continue
        w = _window(ts, f, perp, spot, i, m)
        if w is None:                                                      # a bar the window needs is missing: excluded, never bridged
            excluded += 1
            continue
        fund, basis_in, basis_out, worst = w
        funding_only.append(fund)
        with_basis.append(fund + (basis_in - basis_out))
        starts.append(ts[i])
        adverse.append(worst - basis_in)
    if not funding_only:
        return {"windows": 0, "excluded": excluded, "hold_days": hold_days}

    def dist(xs: List[float]) -> Dict[str, Any]:
        s = sorted(xs)
        d = {"mean_bps": sum(xs) / len(xs), "p05_bps": _q(s, .05), "p25_bps": _q(s, .25), "median_bps": _q(s, .5),
             "p75_bps": _q(s, .75), "p95_bps": _q(s, .95), "min_bps": s[0]}
        d["share_net_positive_after_cost"] = {str(c): sum(1 for x in xs if x - c > 0) / len(xs) for c in costs_bps}
        return d
    stride = [with_basis[j] for j in range(0, len(with_basis), m)]         # non-overlapping windows only
    return {"hold_days": hold_days, "windows": len(funding_only), "excluded": excluded, "non_overlapping_windows": len(stride),
            "funding_only": dist(funding_only), "funding_plus_basis": dist(with_basis),
            "non_overlapping_funding_plus_basis": dist(stride) if len(stride) >= 2 else None,
            "adverse_basis_widening_bps": {"p50": _q(sorted(adverse), .5), "p95": _q(sorted(adverse), .95), "p99": _q(sorted(adverse), .99),
                                            "max": max(adverse)},
            "first_start_ms": starts[0], "last_start_ms": starts[-1]}


# ---------------------------------------------------------------- criteria and the evidence ladder

def kill_criteria(owner: Optional[Mapping[str, Any]], required_by_hurdle: Mapping[float, float]) -> Dict[str, Any]:
    """The conditions, derived BEFORE new data is observed. A value the owner did not supply is UNKNOWN and is replaced by a labelled
    scenario table, never silently by a number."""
    owner = dict(owner or {})
    out: Dict[str, Any] = {}
    owner_fields = {
        "min_acceptable_net_return_pct_on_capital": "the lowest net %/yr on fully allocated capital worth the operational risk",
        "alternative_return_pct": "what the same capital earns elsewhere with comparable risk (cash, T-bills, staking)",
        "max_tolerable_drawdown_pct": "the largest peak-to-trough loss of allocated capital you would accept",
        "max_capital_at_risk_usd": "the most capital you would commit during the research and pilot phases",
        "max_round_trip_cost_bps": "the most execution cost you would accept",
        "max_operational_risk": "your tolerance for operational incidents",
    }
    for k, why in owner_fields.items():
        v = owner.get(k)
        if v is not None and k != "max_operational_risk" and not (_fin(v) and (v >= 0 or k == "alternative_return_pct")):
            raise ValueError(f"{k} must be a finite number{'' if k == 'alternative_return_pct' else ' >= 0'}, got {v!r}")
        out[k] = ({"value": v, "tag": "ASSUMED", "provenance": "OWNER_SUPPLIED", "meaning": why} if v is not None else
                  {"value": None, "tag": "UNKNOWN", "provenance": "OWNER_INPUT_MISSING", "meaning": why})
    out["scenario_hurdles_pct"] = {"tag": "ASSUMED", "provenance": "PROVISIONAL_SCREENING_HURDLE_NOT_OWNER_APPROVED",
                                   "values": list(HURDLES_PCT), "required_funding_bps_per_settlement": dict(required_by_hurdle)}
    out["statistical"] = {"tag": "ASSUMED", "provenance": "SCREENING_CONTROL_NOT_PROOF",
                          "family_wise_alpha": 0.05, "power": 0.8, "min_effective_sample_size": 30, "min_settlements_candidate": 90,
                          "min_prospective_settlements": 270, "variants_in_family": FAMILY_TRIALS, "adjustment": "holm"}
    out["min_evidence_coverage"] = {"tag": "ASSUMED", "value": 0.9, "meaning": "recorder completeness over the evaluation window"}
    return out


LADDER = ("REJECT_OR_SUSPEND", "CONTINUE_RESEARCH", "RESEARCH_CANDIDATE_REVIEW", "PROSPECTIVE_EDGE_REVIEW",
          "ECONOMIC_RECONCILIATION_REQUIRED", "EXECUTION_READINESS_REVIEW")


def decide(evidence: Mapping[str, Any], required_bps: float) -> Dict[str, Any]:
    """Map PROSPECTIVE evidence to the next step. ``required_bps`` is the average funding per settlement the strategy needs to meet the
    lowest screening hurdle. ``evidence`` keys: prospective_settlements, effective_sample_size, mean_bps, lower_bound_bps,
    upper_bound_bps, account_reconciled (bool), owner_authorisation (bool). Capital is ZERO in every branch except the last, and the
    last only asks for a SEPARATE owner review: this function authorises nothing."""
    if not (_fin(required_bps) and required_bps > 0):
        raise ValueError("required_bps must be a positive finite number")
    if not isinstance(evidence, Mapping):
        evidence = {}
    out = {"capital_recommendation": "ZERO", "authorizes_trading": False, "required_bps": required_bps, "reasons": []}
    n, ess = evidence.get("prospective_settlements", 0), evidence.get("effective_sample_size", 0)
    lo, hi, mean = evidence.get("lower_bound_bps"), evidence.get("upper_bound_bps"), evidence.get("mean_bps")
    bad = [k for k, v in (("prospective_settlements", n), ("effective_sample_size", ess)) if not (_fin(v) and v >= 0)]
    bad += [k for k, v in (("lower_bound_bps", lo), ("upper_bound_bps", hi), ("mean_bps", mean)) if v is not None and not _fin(v)]
    if lo is not None and hi is not None and not bad and lo > hi:
        bad.append("lower_bound_bps > upper_bound_bps")
    if not bad and _fin(n) and _fin(ess) and ess > n:
        bad.append("effective_sample_size > prospective_settlements (an effective sample cannot exceed the sample)")
    if not bad and mean is not None and lo is not None and hi is not None and not (lo <= mean <= hi):
        bad.append("mean_bps outside [lower_bound_bps, upper_bound_bps]")
    if bad:                                              # unusable evidence is never a step up: the lowest rung that is not a rejection
        out.update(step="CONTINUE_RESEARCH", reasons=["the evidence is not usable (missing, non-finite, mistyped or inverted): " + ", ".join(bad)])
        return out
    if n >= 90 and ess >= 30 and (lo is None or hi is None or mean is None):
        out.update(step="CONTINUE_RESEARCH", reasons=["mean, lower bound and upper bound are all needed to read the evidence; one is missing"])
        return out
    if n < 90 or ess < 30:
        out.update(step="CONTINUE_RESEARCH", reasons=[f"{n} prospective settlements and an effective sample size of {ess}: below the 90 / 30 floors"])
        return out
    if hi is not None and hi < required_bps:
        out.update(step="REJECT_OR_SUSPEND", reasons=[f"the upper 95% bound of funding ({hi:.3f} bps) is below the {required_bps:.3f} bps the lowest screen needs: "
                                                      "the evidence rejects the strategy at this cost"])
        return out
    if lo is None or lo < required_bps:
        out.update(step="CONTINUE_RESEARCH", reasons=["the interval still includes funding below the requirement; the data cannot yet tell"])
        return out
    if n < 270:
        out.update(step="RESEARCH_CANDIDATE_REVIEW", reasons=["lower bound clears the requirement in the screening window; not yet 270 prospective settlements"])
        return out
    if evidence.get("account_reconciled") is not True:           # exactly True: "no" / "false" / 1 are not a reconciliation
        out.update(step="PROSPECTIVE_EDGE_REVIEW", reasons=["public-market edge supported prospectively; no account fees, fills or settlements reconcile it"])
        return out
    if evidence.get("owner_authorisation") is not True:
        out.update(step="ECONOMIC_RECONCILIATION_REQUIRED", reasons=["reconciled economics exist; a separate owner authorisation does not"])
        return out
    out.update(step="EXECUTION_READINESS_REVIEW", reasons=["every gate is met; this asks for a SEPARATE owner review and authorises nothing"])
    return out


# ---------------------------------------------------------------- the report

def waterfall(funding_bps: float, hold_days: float, round_trip_bps: float, reserve: float, notional: float = 100_000.0) -> Dict[str, Any]:
    """The ledger the engine itself uses (``trade_economics.compute``) for one illustrative position, with unknowns listed."""
    fee = round_trip_bps / 2.0
    pos = TE.Position(notional_usd=notional, hold_days=float(hold_days), btc_inventory_usd=notional, btc_was_owned=True,
                      margin_posted_usd=0.0, collateral_haircut=0.1, reserve_usd=reserve * notional,
                      maintenance_margin_rate=0.005, initial_margin_rate=0.1)
    costs = TE.Costs(perp_entry_fee_bps=fee, perp_exit_fee_bps=fee, fee_provenance="UNVERIFIED")
    r = TE.compute(pos, costs, [funding_bps] * int(round(hold_days * PER_DAY)), funding_source="PUBLIC_REALISED")
    return {"tag": "SIMULATED", "inputs": {"funding_bps_per_settlement": {"value": funding_bps, "tag": "PUBLIC_OBSERVATION"},
                                          "round_trip_bps": {"value": round_trip_bps, "tag": "ASSUMED", "source": "published taker schedule, account tier unverified"},
                                          "hold_days": {"value": hold_days, "tag": "ASSUMED"}, "reserve": {"value": reserve, "tag": "ASSUMED"}},
            "components_usd": r["components_usd"], "net_pnl_usd": r["net_pnl_usd"],
            "annualised_pct_on_notional": r["returns"]["annualised_pct_on_notional"],
            "annualised_pct_on_economic_capital": r["returns"]["annualised_pct_on_economic_capital"],
            "unknown_inputs": r["unknowns"], "unknown_inputs_tag": "UNKNOWN",
            "net_is_optimistic_bound": r["net_is_optimistic_bound"],
            "hurdle_verdicts": {str(h["hurdle_apr_pct"]): h["verdict"] for h in r["hurdles"]}}


def build(funding_path: Optional[str] = None, perp_path: Optional[str] = None, spot_path: Optional[str] = None, *,
          round_trip_bps: float = DEFAULT_ROUND_TRIP_BPS, reserve: float = 0.10, owner: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    ts, f, perp, spot = load_study(funding_path, perp_path, spot_path)
    if len(ts) < 365 * PER_DAY:
        raise DataError(f"{len(ts)} funding prints: the teardown needs at least 365 days ({365 * PER_DAY}) to compare the recent regime")
    st = settlement_stats(ts, f)
    first_recent = ts[-365 * PER_DAY]
    recent_mean = st["last_365d"]["mean_bps"]
    all_mean = st["all"]["mean_bps"]
    peak_year = max(st["by_year"], key=lambda y: st["by_year"][y]["mean_bps"])
    scenarios = {"history_mean": all_mean, f"{peak_year}_peak_year": st["by_year"][peak_year]["mean_bps"],
                 "last_365d": recent_mean, "last_90d": st["last_90d"]["mean_bps"], "baseline_1bp_cap": 1.0}
    hurdle_req = {h: required_funding(h, 30, round_trip_bps, reserve) for h in HURDLES_PCT}
    rolling = {str(h): rolling_starts(ts, f, perp, spot, h) for h in HOLD_DAYS}
    rolling_recent = {str(h): rolling_starts(ts, f, perp, spot, h, first_start_ms=first_recent) for h in HOLD_DAYS}
    by_year_30: Dict[str, Any] = {}
    # start-year breakdown of the 30-day net outcome (start dates inside each calendar year only)
    m = 30 * PER_DAY
    for y in sorted(st["by_year"]):
        lo_ms = int(datetime.datetime(y, 1, 1, tzinfo=datetime.timezone.utc).timestamp() * 1000)
        hi_ms = int(datetime.datetime(y + 1, 1, 1, tzinfo=datetime.timezone.utc).timestamp() * 1000)
        vals = []
        for i in range(len(ts) - m + 1):
            if lo_ms <= ts[i] < hi_ms and ts[i + m - 1] - ts[i] == (m - 1) * SETTLE_MS:
                w = _window(ts, f, perp, spot, i, m)
                if w is not None:
                    vals.append(w[0] + w[1] - w[2] - round_trip_bps)
        by_year_30[str(y)] = None if not vals else {"windows": len(vals), "mean_net_bps": sum(vals) / len(vals), "share_net_positive": sum(1 for v in vals if v > 0) / len(vals),
                                                    "annualised_net_pct_on_capital_at_mean": vals and (sum(vals) / len(vals)) / 1e4 * 365 / 30 * 100 / (1 + reserve)}
    grid = {str(h): {str(x): net_return(x, h, round_trip_bps, reserve) for x in (-1.0, -0.5, 0.0, 0.26, 0.5, 0.57, 1.0, 1.095, 2.0, 3.0)} for h in (7, 30, 90, 180)}
    be = {str(x): breakeven(round_trip_bps, x) for x in (0.26, 0.364, 0.57, 0.65, 1.0, 1.095)}
    sd_recent = math.sqrt(sum((x - recent_mean) ** 2 for x in f[-365 * PER_DAY:]) / (365 * PER_DAY - 1))
    rf = ES.conservative_ess(f[-365 * PER_DAY:]) / (365 * PER_DAY)
    sizing = {}
    for edge in (0.05, 0.1, 0.25):
        r = SP.required_observations(sd_recent, edge, alpha=0.05, power=0.8, n_hypotheses=FAMILY_TRIALS, adjust="holm", ess_ratio=max(rf, 1e-3),
                                     two_sided=True, method="t")
        sizing[str(edge)] = {"status": r["status"], "required_effective_n": r.get("required_effective_n"), "required_settlements": r.get("required_raw_n"),
                             "required_days": None if r.get("required_raw_n") is None else r["required_raw_n"] / PER_DAY}
    required_30 = {str(h): required_funding(h, 30, round_trip_bps, reserve) for h in HURDLES_PCT}
    return {
        "data": {"tag": "PUBLIC_OBSERVATION", "settlements": len(ts), "first_ms": ts[0], "last_ms": ts[-1],
                 "gaps_in_funding_grid": sum(1 for a, b in zip(ts, ts[1:]) if b - a != SETTLE_MS),
                 "klines_4h": {"perp": len(perp), "spot": len(spot)}, "in_sample": True, "tick_level": False},
        "funding_statistics": {"tag": "PUBLIC_OBSERVATION", **st},
        "break_even": {"tag": "SIMULATED", "round_trip_bps": {"value": round_trip_bps, "tag": "ASSUMED"}, "uninterrupted_funding": be},
        "net_return_grid": {"tag": "SIMULATED", "round_trip_bps": round_trip_bps, "reserve": reserve, "reserve_tag": "ASSUMED", "by_hold_days_then_funding_bps": grid},
        "required_funding_bps_per_settlement_30d": {"tag": "SIMULATED", "by_hurdle_pct": required_30,
                                                     "provisional": "PROVISIONAL_SCREENING_HURDLE_NOT_OWNER_APPROVED"},
        "rolling_start_all_history": {"tag": "SIMULATED", "by_hold_days": rolling},
        "rolling_start_last_365d_starts": {"tag": "SIMULATED", "by_hold_days": rolling_recent},
        "thirty_day_outcome_by_start_year": {"tag": "SIMULATED", "round_trip_bps": round_trip_bps, "by_year": by_year_30},
        "waterfalls": {name: waterfall(v, 30, round_trip_bps, reserve) for name, v in scenarios.items()},
        "future_study_sizing": {"tag": "SIMULATED", "uses": "recent-regime sd and effective-sample-size ratio (ESTIMATED from history), two-sided t, Holm over the 16-variant family",
                                "observed_sd_bps": sd_recent, "ess_ratio": rf, "by_edge_bps": sizing},
        "criteria": kill_criteria(owner, {h: required_30[str(h)] for h in HURDLES_PCT}),
        "capital_recommendation": {"value": "ZERO", "authorizes_trading": False,
                                   "reason": "no prospective evidence, no account economics, unverified fee tier, assumed margin rules"},
        "unknowns": ["account fee tier", "market impact", "latency", "borrow", "operational cost", "owner hurdle", "owner alternative return",
                     "tick-level basis", "queue/fill behaviour", "account funding settlements", "future funding regime"],
        "limits": ["in-sample public history, not a forecast", "4h klines: intra-bar basis excursions are invisible",
                   "the 11.4 bps round trip is 11.0 published fee + 0.4 ASSUMED spread allowance, not the owner's tier", "overlapping windows are not independent samples",
                   "no tick-level execution, queue or latency model", "the holding rule is mechanical (no entry filter), a filtered rule would need pre-registration",
                   "the rolling-start simulation holds a FIXED BTC quantity (funding at each settlement's perp open, exit basis over the ENTRY spot); costs stay a constant bps of entry notional"],
    }


def _p(x: Optional[float], nd: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def render(rep: Dict[str, Any]) -> str:
    d, fs = rep["data"], rep["funding_statistics"]
    first = datetime.datetime.fromtimestamp(d["first_ms"] / 1000, datetime.timezone.utc).date()
    last = datetime.datetime.fromtimestamp(d["last_ms"] / 1000, datetime.timezone.utc).date()
    o = [f"CARRY TEARDOWN - public history {first} .. {last}, {d['settlements']} settlements (PUBLIC_OBSERVATION, in-sample, 4h klines); capital recommendation: "
         f"{rep['capital_recommendation']['value']}", ""]
    o.append("1 FUNDING RECEIVED BY THE SHORT PERP (bps per 8h settlement; annualised = simple, on hedged notional)  [PUBLIC_OBSERVATION]")
    for y, b in fs["by_year"].items():
        o.append(f"    {y}: n {b['n']:>4}  mean {b['mean_bps']:.3f}  median {b['median_bps']:.3f}  negative {b['share_negative']:.0%}  at/below 1 bp {b['share_at_or_below_baseline']:.0%}  = {b['annualised_gross_pct_on_notional']:.2f} %/yr gross")
    for k in ("last_365d", "last_180d", "last_90d"):
        b = fs[k]
        o.append(f"    {k:>9}: mean {b['mean_bps']:.3f}  negative {b['share_negative']:.0%}  at/below 1 bp {b['share_at_or_below_baseline']:.0%}  = {b['annualised_gross_pct_on_notional']:.2f} %/yr gross")
    o.append(f"    persistence: lag-1 autocorrelation {fs['acf'][1]:.2f}, lag-21 {fs['acf'][21]:.2f}; effective sample size {fs['effective_sample_size']:.0f} of {d['settlements']} "
             f"(ratio {fs['ess_ratio']:.3f}); {fs['sign_regime_runs']['count']} sign regimes, longest {fs['sign_regime_runs']['max_len_settlements']} settlements")
    o.append("")
    o.append(f"2 BREAK-EVEN at an ASSUMED round trip of {rep['break_even']['round_trip_bps']['value']} bps (published schedule, account tier UNVERIFIED), uninterrupted funding  [SIMULATED]")
    for x, b in rep["break_even"]["uninterrupted_funding"].items():
        o.append(f"    funding {x} bps/settlement: " + ("never recovers" if b["settlements"] is None else f"{b['settlements']:.1f} settlements = {b['days']:.1f} days"))
    o.append("")
    o.append("3 FUNDING REQUIRED for a 30-day hold to earn the screening hurdle on FULLY ALLOCATED capital (bps/settlement)  [SIMULATED; hurdles are PROVISIONAL, not the owner's]")
    for h, v in rep["required_funding_bps_per_settlement_30d"]["by_hurdle_pct"].items():
        o.append(f"    {float(h):g} %/yr: {v:.3f} bps/settlement  ({v / (fs['last_365d']['mean_bps'] or 1):.1f}x the last-365d mean; funding AT the 1 bp baseline supplies {1.0 / v:.0%} of it)")
    o.append("")
    o.append("4 ROLLING-START HOLDING SIMULATION (every possible start; funding + 4h-bar basis - cost; windows overlap, see non-overlapping count)  [SIMULATED]")
    for label, key in (("all history", "rolling_start_all_history"), ("starts in the last 365d", "rolling_start_last_365d_starts")):
        o.append(f"  {label}:")
        for h, r in rep[key]["by_hold_days"].items():
            if not r.get("windows"):
                o.append(f"    {h:>3}d: no windows")
                continue
            fb = r["funding_plus_basis"]
            sh = fb["share_net_positive_after_cost"]
            o.append(f"    {h:>3}d: windows {r['windows']:>4} (non-overlapping {r['non_overlapping_windows']:>3})  funding+basis mean {fb['mean_bps']:.1f} p05 {fb['p05_bps']:.1f} med {fb['median_bps']:.1f} p95 {fb['p95_bps']:.1f} bps; "
                     f"net>0 after 6/11.4/17 bps: " + "/".join(f"{sh[k]:.0%}" for k in ("6.0", "11.4", "17.0")) + f"; adverse basis widening p95 {r['adverse_basis_widening_bps']['p95']:.1f} bps")
    o.append("  30-day outcome by START year (net of the assumed round trip):")
    for y, r in rep["thirty_day_outcome_by_start_year"]["by_year"].items():
        o.append(f"    {y}: " + ("no windows" if not r else f"windows {r['windows']}, mean net {r['mean_net_bps']:.1f} bps, net>0 {r['share_net_positive']:.0%}, ≈ {r['annualised_net_pct_on_capital_at_mean']:.2f} %/yr on capital at the mean"))
    o.append("")
    o.append("5 LEDGER (trade_economics) for one $100k position, 30-day hold, per funding regime  [SIMULATED; inputs tagged]")
    for name, w in rep["waterfalls"].items():
        o.append(f"    {name:<22} funding {w['inputs']['funding_bps_per_settlement']['value']:.3f} bps: net ${w['net_pnl_usd']:.0f}  = {w['annualised_pct_on_notional']:.2f} %/yr on notional, "
                 f"{w['annualised_pct_on_economic_capital']:.2f} %/yr on economic capital  [OPTIMISTIC BOUND: unpriced costs counted as zero: {', '.join(w['unknown_inputs'])}]")
    o.append("")
    s = rep["future_study_sizing"]
    o.append("6 SIZING A PROSPECTIVE STUDY  [SIMULATED, from ESTIMATED recent-regime sd and ESS ratio]")
    for e, r in s["by_edge_bps"].items():
        o.append(f"    to resolve a funding edge of {e} bps/settlement above the requirement: " + (f"{r['required_effective_n']} effective = {r['required_settlements']} settlements ({r['required_days']:.0f} days)" if r["status"] == "OK" else r["status"]))
    o.append("")
    c = rep["criteria"]
    o.append("7 CRITERIA (derived before new data; owner inputs are UNKNOWN unless supplied)")
    for k in ("min_acceptable_net_return_pct_on_capital", "alternative_return_pct", "max_tolerable_drawdown_pct", "max_capital_at_risk_usd", "max_round_trip_cost_bps"):
        o.append(f"    {k}: {c[k]['value'] if c[k]['value'] is not None else 'UNKNOWN'}  [{c[k]['tag']}]")
    o.append(f"    statistical screens (ASSUMED): alpha {c['statistical']['family_wise_alpha']} over {c['statistical']['variants_in_family']} variants, power {c['statistical']['power']}, ESS >= {c['statistical']['min_effective_sample_size']}")
    o.append("")
    o.append("UNKNOWN: " + "; ".join(rep["unknowns"]))
    o.append("LIMITS: " + "; ".join(rep["limits"]))
    return "\n".join(o)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--funding"), ap.add_argument("--perp-4h"), ap.add_argument("--spot-4h")
    ap.add_argument("--round-trip-bps", type=float, default=DEFAULT_ROUND_TRIP_BPS)
    ap.add_argument("--reserve", type=float, default=0.10)
    ap.add_argument("--owner-hurdle-pct", type=float), ap.add_argument("--owner-alternative-return-pct", type=float)
    ap.add_argument("--owner-max-drawdown-pct", type=float), ap.add_argument("--owner-max-capital-usd", type=float)
    ap.add_argument("--owner-max-round-trip-bps", type=float)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not (math.isfinite(a.round_trip_bps) and 0 <= a.round_trip_bps <= 200) or not (0 <= a.reserve <= 5):
        ap.error("--round-trip-bps must be in [0, 200] and --reserve in [0, 5]")
    owner = {k: v for k, v in {"min_acceptable_net_return_pct_on_capital": a.owner_hurdle_pct, "alternative_return_pct": a.owner_alternative_return_pct,
                               "max_tolerable_drawdown_pct": a.owner_max_drawdown_pct, "max_capital_at_risk_usd": a.owner_max_capital_usd,
                               "max_round_trip_cost_bps": a.owner_max_round_trip_bps}.items() if v is not None}
    try:
        rep = build(a.funding, a.perp_4h, a.spot_4h, round_trip_bps=a.round_trip_bps, reserve=a.reserve, owner=owner)
        out = json.dumps(rep, indent=2, sort_keys=True, default=str, allow_nan=False) if a.json else render(rep)
    except (DataError, OSError, ValueError, KeyError, TypeError, OverflowError, EOFError, RecursionError) as exc:
        # DataError, a missing/unreadable file, a non-JSON or bad-gzip history, a row of the wrong shape, a constant history (no variance to
        # size a study from), a NaN owner input: a message and exit 2, never a traceback
        print(f"refusing: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
