"""Does a strategy have enough evidence of POSITIVE NET expectancy to be given capital?

Pure. No clock, no store, no venue, no network, no file writes. The clock is a
parameter. The answer is one of three, and only one of them allows capital:

  ALLOCATE      every check below passed. Necessary for capital, never sufficient
                for anything else: it is not a promotion, not an approval and not
                an arming step (``grants_owner_approval`` is always False, the
                verdict has no field a gate could read as a human sign-off, and
                this module imports nothing from ``promotion_gate``).
  BLOCK         affirmative evidence AGAINST: negative net expectancy, the
                evidence overlaps its own discovery window, a drawdown or
                profit-concentration breach, expectancy that does not survive
                removing the single largest win.
  INSUFFICIENT  the evidence cannot say: unknown expectancy, stale or malformed
                evidence, too few trades, an unknown discovery window or
                drawdown limit, or a positive mean whose lower confidence bound
                is not above zero. Unknown is NOT zero and is not "probably
                fine": it gets no capital.

BLOCK dominates INSUFFICIENT dominates ALLOCATE. All reasons are reported, not
only the first. The policy thresholds are frozen below and
``EvidencePolicy.validate`` refuses any policy looser than the default, so a
caller cannot loosen a threshold by passing a different one. They were declared
before looking at any strategy's numbers and are not tuned.

WHAT THE INPUT MUST BE
======================
Closed trades NET of fees, funding, borrow and slippage as the caller measured
them (this module cannot verify that; ``net_basis`` is a required, non-blank
label so the claim is at least attributable). Each trade has an entry and exit
time (epoch seconds). The discovery window is the interval of data used to find
or tune the strategy; a strategy cannot certify on data it was discovered on, so
a trade that overlaps it BLOCKs the whole evidence set. An unknown discovery
window is INSUFFICIENT, never "no overlap".

ENTRY DECISION RULE
===================
``evaluate_entry_benefit`` prices one prospective entry over its expected hold:

  net = income - round-trip fees - spread - slippage - financing
        - capital opportunity cost - model haircut

Every component is returned. A component that is missing, non-finite or out of
domain is UNAVAILABLE and makes the decision REFUSE; it is never read as 0. It
also refuses when the net is not positive, when the net is smaller than
``MIN_SIGMA_MULTIPLE`` times the stated income uncertainty, and when the
strategy's evidence verdict is not ALLOCATE.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

ALLOCATE = "ALLOCATE"
BLOCK = "BLOCK"
INSUFFICIENT = "INSUFFICIENT"
VERDICTS = (ALLOCATE, BLOCK, INSUFFICIENT)

# Reason codes (BLOCK)
NEGATIVE_NET_EXPECTANCY = "NEGATIVE_NET_EXPECTANCY"
OVERLAPS_DISCOVERY_WINDOW = "OVERLAPS_DISCOVERY_WINDOW"
DRAWDOWN_BREACH = "DRAWDOWN_BREACH"
PROFIT_CONCENTRATION = "PROFIT_CONCENTRATION"
EXPECTANCY_EXCL_LARGEST_WIN_NOT_POSITIVE = "EXPECTANCY_EXCL_LARGEST_WIN_NOT_POSITIVE"
# Reason codes (INSUFFICIENT)
NO_EVIDENCE = "NO_EVIDENCE"
UNKNOWN_EXPECTANCY = "UNKNOWN_EXPECTANCY"
STALE_EVIDENCE = "STALE_EVIDENCE"
INVALID_EVIDENCE = "INVALID_EVIDENCE"
INSUFFICIENT_TRADES = "INSUFFICIENT_TRADES"
DISCOVERY_WINDOW_UNKNOWN = "DISCOVERY_WINDOW_UNKNOWN"
DRAWDOWN_LIMIT_UNKNOWN = "DRAWDOWN_LIMIT_UNKNOWN"
EXPECTANCY_NOT_SUPPORTED = "EXPECTANCY_NOT_SUPPORTED"
UNSOURCED_NET_BASIS = "UNSOURCED_NET_BASIS"
ALL_CHECKS_PASSED = "ALL_CHECKS_PASSED"

UNAVAILABLE = "UNAVAILABLE"
FUTURE_TOLERANCE_S = 300.0


@dataclass(frozen=True)
class EvidencePolicy:
    """Frozen thresholds. ``validate`` refuses anything looser than the default."""
    min_trades: int = 20                 # = promotion_gate.MIN_FORWARD_TRADES
    max_age_s: float = 48 * 3600.0       # = forward_status.DEFAULT_MAX_AGE_S
    max_top1_profit_share: float = 0.5   # largest win / gross profit
    lcb_z: float = 1.645                 # one-sided 95% lower bound on mean

    def validate(self) -> None:
        base = EvidencePolicy()
        ok = (_finite(self.min_trades) and self.min_trades >= base.min_trades
              and _finite(self.max_age_s) and 0 < self.max_age_s <= base.max_age_s
              and _finite(self.max_top1_profit_share)
              and 0 < self.max_top1_profit_share <= base.max_top1_profit_share
              and _finite(self.lcb_z) and self.lcb_z >= base.lcb_z)
        if not ok:
            raise ValueError(
                "EvidencePolicy may be stricter than the default, never looser: "
                f"{self!r} vs {base!r}")


DEFAULT_POLICY = EvidencePolicy()


@dataclass(frozen=True)
class EvidenceTrade:
    entry_s: float
    exit_s: float
    net: float                 # net of costs, in the evidence's unit (R or USD)


@dataclass(frozen=True)
class StrategyEvidence:
    strategy_id: str
    trades: Tuple[EvidenceTrade, ...]
    observed_at_s: Optional[float]
    net_basis: str                                   # what "net" nets, and who says so
    discovery_window_s: Optional[Tuple[float, float]]   # (start, end) or None = unknown
    drawdown_limit: Optional[float]                  # same unit as net, > 0, None = unknown


@dataclass(frozen=True)
class EvidenceVerdict:
    strategy_id: str
    verdict: str
    reasons: Tuple[str, ...]
    metrics: Mapping[str, Any]
    #: Always False. A verdict is not an approval and nothing reads it as one.
    grants_owner_approval: bool = field(default=False, init=False)

    @property
    def allows_capital(self) -> bool:
        return self.verdict == ALLOCATE

    def as_dict(self) -> Dict[str, Any]:
        return {"strategy_id": self.strategy_id, "verdict": self.verdict,
                "reasons": list(self.reasons), "metrics": dict(self.metrics),
                "grants_owner_approval": False}


def _finite(x: Any) -> bool:
    return (isinstance(x, (int, float)) and not isinstance(x, bool)
            and math.isfinite(x))


def max_drawdown(nets_in_exit_order: Sequence[float]) -> float:
    """Largest peak-to-trough fall of the cumulative net, peak starting at 0."""
    peak = cum = worst = 0.0
    for x in nets_in_exit_order:
        cum += x
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return worst


def evaluate_strategy_evidence(ev: Optional[StrategyEvidence], *, now_s: float,
                               policy: EvidencePolicy = DEFAULT_POLICY,
                               strategy_id: str = "") -> EvidenceVerdict:
    """Pure verdict. Never raises on bad evidence (that is INSUFFICIENT); raises
    ValueError only for an unusable clock or a policy looser than the default."""
    policy.validate()
    if not (_finite(now_s) and now_s > 0):
        raise ValueError(f"now_s must be a finite epoch, not {now_s!r}")
    if ev is None:
        return EvidenceVerdict(strategy_id, INSUFFICIENT, (NO_EVIDENCE,), {})
    block: List[str] = []
    insuf: List[str] = []
    m: Dict[str, Any] = {"net_basis": ev.net_basis}

    if not isinstance(ev.net_basis, str) or not ev.net_basis.strip():
        insuf.append(UNSOURCED_NET_BASIS)

    trades = list(ev.trades or ())
    bad = [t for t in trades if not (
        isinstance(t, EvidenceTrade) and _finite(t.net) and _finite(t.entry_s)
        and _finite(t.exit_s) and t.exit_s >= t.entry_s)]
    n = len(trades)
    m["n_trades"] = n
    m["min_trades"] = policy.min_trades
    if bad:
        insuf.append(f"{INVALID_EVIDENCE}:malformed_trades={len(bad)}")
        trades = [t for t in trades if t not in bad]
    # One trade copied N times has zero variance and a flattering confidence
    # bound; a repeated (entry, exit) window is the same trade, not a sample.
    seen_windows: set = set()
    duplicates = 0
    for t in trades:
        key = (t.entry_s, t.exit_s)
        duplicates += key in seen_windows
        seen_windows.add(key)
    if duplicates:
        insuf.append(f"{INVALID_EVIDENCE}:duplicate_trades={duplicates}")

    # staleness / clock
    if not _finite(ev.observed_at_s):
        insuf.append(f"{INVALID_EVIDENCE}:observed_at")
    else:
        age = now_s - ev.observed_at_s
        m["age_s"] = age
        if age < -FUTURE_TOLERANCE_S:
            insuf.append(f"{INVALID_EVIDENCE}:observed_in_future")
        elif age > policy.max_age_s:
            insuf.append(f"{STALE_EVIDENCE}:{age / 3600.0:.1f}h>"
                         f"{policy.max_age_s / 3600.0:.0f}h")
        if trades and max(t.exit_s for t in trades) > ev.observed_at_s:
            insuf.append(f"{INVALID_EVIDENCE}:trade_closes_after_observation")

    if not trades:
        insuf.append(UNKNOWN_EXPECTANCY)
    if n < policy.min_trades:
        insuf.append(f"{INSUFFICIENT_TRADES}:{n}<{policy.min_trades}")

    # discovery overlap (self-certification)
    dw = ev.discovery_window_s
    if (not isinstance(dw, tuple) or len(dw) != 2 or not _finite(dw[0])
            or not _finite(dw[1]) or dw[1] < dw[0]):
        insuf.append(DISCOVERY_WINDOW_UNKNOWN)
    else:
        over = [t for t in trades if t.entry_s <= dw[1] and t.exit_s >= dw[0]]
        m["trades_overlapping_discovery"] = len(over)
        if over:
            block.append(f"{OVERLAPS_DISCOVERY_WINDOW}:{len(over)}_of_{len(trades)}")

    if trades:
        ordered = sorted(trades, key=lambda t: (t.exit_s, t.entry_s))
        nets = [t.net for t in ordered]
        k = len(nets)
        mean = sum(nets) / k
        m["mean_net"] = mean
        m["total_net"] = sum(nets)
        if mean <= 0:
            block.append(f"{NEGATIVE_NET_EXPECTANCY}:mean={mean:+.4f}")
        if k >= 2:
            var = sum((x - mean) ** 2 for x in nets) / (k - 1)
            lcb = mean - policy.lcb_z * math.sqrt(var / k)
            m["mean_lcb"] = lcb
            if mean > 0 and lcb <= 0:
                insuf.append(f"{EXPECTANCY_NOT_SUPPORTED}:lcb={lcb:+.4f}")
        wins = [x for x in nets if x > 0]
        if wins:
            biggest = max(wins)
            ex = list(nets)
            ex.remove(biggest)
            ex_mean = (sum(ex) / len(ex)) if ex else 0.0
            m["mean_net_excl_largest_win"] = ex_mean
            gross = sum(wins)
            share = biggest / gross
            m["top1_profit_share"] = share
            if ex_mean <= 0:
                block.append(f"{EXPECTANCY_EXCL_LARGEST_WIN_NOT_POSITIVE}:"
                             f"{ex_mean:+.4f}")
            if share > policy.max_top1_profit_share:
                block.append(f"{PROFIT_CONCENTRATION}:{share:.2f}>"
                             f"{policy.max_top1_profit_share:.2f}")
        dd = max_drawdown(nets)
        m["max_drawdown"] = dd
        if not (_finite(ev.drawdown_limit) and ev.drawdown_limit > 0):
            insuf.append(DRAWDOWN_LIMIT_UNKNOWN)
        elif dd > ev.drawdown_limit:
            block.append(f"{DRAWDOWN_BREACH}:{dd:.4f}>{ev.drawdown_limit:.4f}")
        m["drawdown_limit"] = ev.drawdown_limit

    if block:
        v, reasons = BLOCK, tuple(block + insuf)
    elif insuf:
        v, reasons = INSUFFICIENT, tuple(insuf)
    else:
        v, reasons = ALLOCATE, (ALL_CHECKS_PASSED,)
    return EvidenceVerdict(ev.strategy_id or strategy_id, v, reasons, m)


def evidence_from_forward_status(fs: Any, *, strategy_id: str,
                                 discovery_window_s: Optional[Tuple[float, float]],
                                 drawdown_limit: Optional[float]) -> StrategyEvidence:
    """Adapt ``forward_status.ForwardStatus`` (net R per closed paper trade).
    ``fs`` is only read. The discovery window and drawdown limit must come from
    the caller; they are not in the artefact, and None stays INSUFFICIENT."""
    import calendar
    import time as _t

    def ep(s: Any) -> Optional[float]:
        try:
            return float(calendar.timegm(_t.strptime(s, "%Y-%m-%dT%H:%M:%SZ")))
        except (TypeError, ValueError):
            return None

    trades: List[EvidenceTrade] = []
    for r in getattr(fs, "trades", ()) or ():
        e, x = ep(r.get("entry_utc")), ep(r.get("exit_utc"))
        if e is not None and x is not None and _finite(r.get("net_r")):
            trades.append(EvidenceTrade(e, x, float(r["net_r"])))
    state = getattr(fs, "state", "")
    obs = ep(getattr(fs, "observed_at_utc", None))
    if state in ("NO_DATA", "UNREADABLE"):
        obs = None
        trades = []
    return StrategyEvidence(
        strategy_id=strategy_id, trades=tuple(trades), observed_at_s=obs,
        net_basis="forward_shadow net_r (paper, modelled costs)",
        discovery_window_s=discovery_window_s, drawdown_limit=drawdown_limit)


def status_report(verdicts: Mapping[str, EvidenceVerdict],
                  forward: Any = None) -> Dict[str, Any]:
    """Status to emit: verdicts plus the forward-shadow facts, UNFILTERED, so a
    negative forward result is never summarised away by an ALLOCATE elsewhere."""
    out: Dict[str, Any] = {
        "verdicts": {k: v.as_dict() for k, v in sorted(verdicts.items())},
        "capital_allowed_for": sorted(k for k, v in verdicts.items()
                                      if v.allows_capital),
        "grants_owner_approval": False,
        "note": "paper observations are not cash; no verdict is an approval"}
    if forward is not None:
        out["forward_shadow"] = {
            "state": getattr(forward, "state", None),
            "evidence_state": getattr(forward, "evidence_state", None),
            "n_closed_trades": getattr(forward, "n_closed_trades", None),
            "mean_net_r": getattr(forward, "mean_net_r", None),
            "trades_vs_gate": getattr(forward, "trades_vs_gate", None),
            "days_vs_gate": getattr(forward, "days_vs_gate", None),
            "net_r_per_trade": [t.get("net_r") for t in
                                (getattr(forward, "trades", ()) or ())]}
    return out


# -- entry decision rule -------------------------------------------------------

#: Net benefit must exceed this many standard deviations of income uncertainty.
MIN_SIGMA_MULTIPLE = 1.0

REFUSE_UNAVAILABLE = "COMPONENT_UNAVAILABLE"
REFUSE_NEGATIVE = "NET_BENEFIT_NOT_POSITIVE"
REFUSE_TOO_UNCERTAIN = "NET_BENEFIT_WITHIN_UNCERTAINTY"
REFUSE_NO_EVIDENCE = "STRATEGY_EVIDENCE_NOT_ALLOCATE"


@dataclass(frozen=True)
class EntryDecision:
    allowed: bool
    reasons: Tuple[str, ...]
    #: bps of notional over the hold; UNAVAILABLE where an input was unusable.
    components_bps: Mapping[str, Any]
    net_benefit_bps: Optional[float]
    net_benefit_usd: Optional[float]
    hold_days: Optional[float]


def evaluate_entry_benefit(*, expected_funding_bps_per_settlement: Any,
                           hold_days: Any, notional_usd: Any,
                           round_trip_fee_bps: Any, spread_bps: Any,
                           expected_slippage_bps: Any, borrow_apr: Any,
                           opportunity_cost_apr: Any,
                           income_haircut_fraction: Any,
                           income_uncertainty_bps: Any,
                           evidence: Optional[EvidenceVerdict],
                           settlements_per_day: Any = 3) -> EntryDecision:
    """Expected net benefit of one entry over its expected hold. Pure.

    Every cost argument is REQUIRED (no default); None/NaN/inf/negative is
    UNAVAILABLE and refuses. ``income_haircut_fraction`` in [0, 1] is the share
    of expected income not believed; ``income_uncertainty_bps`` is one standard
    deviation of the hold's income. ``borrow_apr = 0`` is a statement that the
    leg is owned, which the caller must make explicitly.
    """
    def pos(x: Any) -> bool:
        return _finite(x) and x > 0

    def nonneg(x: Any) -> bool:
        return _finite(x) and x >= 0

    comps: Dict[str, Any] = {k: UNAVAILABLE for k in (
        "income", "fees", "spread", "slippage", "financing",
        "opportunity_cost", "model_haircut")}
    bad: List[str] = []
    if not pos(hold_days):
        bad.append("hold_days")
    if not pos(notional_usd):
        bad.append("notional_usd")
    if not _finite(expected_funding_bps_per_settlement):
        bad.append("expected_funding_bps_per_settlement")
    if not pos(settlements_per_day):
        bad.append("settlements_per_day")
    for name, val in (("round_trip_fee_bps", round_trip_fee_bps),
                      ("spread_bps", spread_bps),
                      ("expected_slippage_bps", expected_slippage_bps),
                      ("borrow_apr", borrow_apr),
                      ("opportunity_cost_apr", opportunity_cost_apr),
                      ("income_uncertainty_bps", income_uncertainty_bps)):
        if not nonneg(val):
            bad.append(name)
    if not (_finite(income_haircut_fraction) and 0 <= income_haircut_fraction <= 1):
        bad.append("income_haircut_fraction")
    if bad:
        return EntryDecision(False, (f"{REFUSE_UNAVAILABLE}:" + ",".join(bad),),
                             comps, None, None,
                             float(hold_days) if pos(hold_days) else None)
    h = float(hold_days)
    income = float(expected_funding_bps_per_settlement) * float(settlements_per_day) * h
    comps.update(
        income=income, fees=float(round_trip_fee_bps), spread=float(spread_bps),
        slippage=float(expected_slippage_bps),
        financing=float(borrow_apr) / 365.0 * 1e4 * h,
        opportunity_cost=float(opportunity_cost_apr) / 365.0 * 1e4 * h,
        model_haircut=max(income, 0.0) * float(income_haircut_fraction))
    net = comps["income"] - sum(comps[k] for k in (
        "fees", "spread", "slippage", "financing", "opportunity_cost",
        "model_haircut"))
    if not math.isfinite(net):
        return EntryDecision(False, (f"{REFUSE_UNAVAILABLE}:arithmetic_overflow",),
                             comps, None, None, h)
    reasons: List[str] = []
    if evidence is None or not isinstance(evidence, EvidenceVerdict) \
            or evidence.verdict != ALLOCATE:
        reasons.append(f"{REFUSE_NO_EVIDENCE}:"
                       f"{getattr(evidence, 'verdict', 'NONE')}")
    if net <= 0:
        reasons.append(f"{REFUSE_NEGATIVE}:{net:+.2f}bps")
    elif net < MIN_SIGMA_MULTIPLE * float(income_uncertainty_bps):
        reasons.append(f"{REFUSE_TOO_UNCERTAIN}:{net:.2f}<"
                       f"{MIN_SIGMA_MULTIPLE * float(income_uncertainty_bps):.2f}bps")
    usd = net / 1e4 * float(notional_usd)
    return EntryDecision(not reasons, tuple(reasons) or ("NET_BENEFIT_POSITIVE",),
                         comps, net, usd, h)
