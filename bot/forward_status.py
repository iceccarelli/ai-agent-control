"""Machine-derived facts about a forward-shadow artefact. Facts only.

WHY THIS EXISTS
===============
The promotion gate's ``forward_shadow_clean`` item carries its forward count as
a hand-typed sentence ("4 of 20 ... 51 of 180 days" in slice 59, "0 of 20" in
slice 76). Neither sentence is derived from ``artifacts/forward_shadow_current.json``
and nothing checks how old that artefact is. A number someone typed, about a
file nobody re-read, is not a measurement.

This module reads the artefact and reports what it actually contains, how old
it is, and what its closed trades say when the later half is compared with the
earlier half. It is pure and deterministic: the clock is a parameter (``now``)
and nothing is written, fetched or cached.

WHAT IT DOES NOT DO
===================
It decides nothing. It never marks a checklist item complete, never satisfies a
promotion-gate item and never authorises anything; ``ForwardStatus.is_gate_decision``
is always False and the gate comparisons (``n_closed_trades`` vs 20,
``forward_days_observed`` vs 180) are reported as plain facts for a reader to
weigh. These are PAPER shadow observations, not cash.

STATE (a closed set, ``STATES``), decided in this order, first match wins
==========================================================================
NO_DATA
    The artefact path does not exist.
UNREADABLE
    The file exists but cannot be used as evidence: not JSON, not an object,
    ``observed_at_utc`` missing or unparseable, ``trades`` not a list, a trade
    row without a parseable entry/exit time or a finite ``net_r``, an exit
    before its entry, a trade that closed after the observation was taken, an
    observation stamped more than ``FUTURE_TOLERANCE_S`` after ``now``, the
    same trade row listed twice, or a declared ``forward_n_trades`` that
    disagrees with the number of distinct trade rows. Contradictory evidence
    fails closed rather than being repaired.
STALE
    Readable, but ``now - observed_at_utc`` exceeds ``max_age_s``. The trade
    facts are still reported; ``evidence_state`` still says what they show.
INSUFFICIENT_SAMPLE
    Fresh, but fewer than ``min_trades_for_trend`` distinct closed trades.
DETERIORATING / RECOVERING / STABLE
    Fresh, with at least ``min_trades_for_trend`` closed trades. Trades are
    ordered by exit time (then entry time); with k = n // 2 the EARLIER half is
    the first k trades and the LATER half the last k (an odd middle trade is in
    neither). d = mean(later) - mean(earlier); se = sqrt(var_e/k + var_l/k)
    with sample variances. DETERIORATING when d < -2*se, RECOVERING when
    d > +2*se, STABLE otherwise. (When se is 0 any nonzero d decides.)

``evidence_state`` is the same verdict computed WITHOUT the staleness rule
(INSUFFICIENT_SAMPLE / DETERIORATING / RECOVERING / STABLE, or the
NO_DATA / UNREADABLE that precedes it), so a stale artefact still says what its
sample would support if it were current.

Defaults are declared, not tuned to the current artefact:

* ``DEFAULT_MAX_AGE_S`` = 48 h. The forward window is CLOSED DAILY BARS; an
  artefact re-observed each day is at most ~24 h old, so 48 h tolerates one
  missed daily observation and no more.
* ``DEFAULT_MIN_TRADES_FOR_TREND`` = 20, the artefact's own declared
  ``M4_halves.min_trades`` (the halves monitor does not arm below it) and the
  gate's ``MIN_FORWARD_TRADES``.
* ``GATE_MIN_FORWARD_TRADES`` / ``GATE_MIN_FORWARD_DAYS`` mirror
  ``promotion_gate.MIN_FORWARD_TRADES`` / ``MIN_FORWARD_DAYS`` (a test pins
  the relation); they are imported here as numbers only, never as a decision.
"""
from __future__ import annotations

import calendar
import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

NO_DATA = "NO_DATA"
UNREADABLE = "UNREADABLE"
STALE = "STALE"
INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
DETERIORATING = "DETERIORATING"
RECOVERING = "RECOVERING"
STABLE = "STABLE"
STATES: Tuple[str, ...] = (NO_DATA, UNREADABLE, STALE, INSUFFICIENT_SAMPLE,
                           DETERIORATING, RECOVERING, STABLE)

DEFAULT_MAX_AGE_S = 48 * 3600.0
DEFAULT_MIN_TRADES_FOR_TREND = 20
#: A stamp further than this ahead of ``now`` is a broken clock or a forged
#: file; either way its age cannot be answered. Same tolerance as
#: tools/monitor_summary.FUTURE_TOLERANCE_MINUTES.
FUTURE_TOLERANCE_S = 300.0
#: Mirrors of promotion_gate.MIN_FORWARD_TRADES / MIN_FORWARD_DAYS.
GATE_MIN_FORWARD_TRADES = 20
GATE_MIN_FORWARD_DAYS = 180
#: Width of the "no change" band, in standard errors of the halves' difference.
TREND_Z = 2.0

DEFAULT_ARTIFACT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "artifacts", "forward_shadow_current.json")

_STAMP = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class ForwardStatus:
    """What a forward-shadow artefact says, measured at ``now``. No verdict
    here is a gate decision (``is_gate_decision`` is always False)."""

    path: str
    now_utc: str
    state: str
    evidence_state: str
    reasons: Tuple[str, ...]
    max_age_s: float
    min_trades_for_trend: int
    signal: Optional[str] = None
    symbol: Optional[str] = None
    schema: Optional[str] = None
    observed_at_utc: Optional[str] = None
    age_s: Optional[float] = None
    fresh: Optional[bool] = None
    declared_n_trades: Optional[int] = None
    n_closed_trades: int = 0
    duplicate_trades: int = 0
    mean_net_r: Optional[float] = None
    trades: Tuple[Dict[str, Any], ...] = ()
    first_entry_utc: Optional[str] = None
    last_entry_utc: Optional[str] = None
    first_exit_utc: Optional[str] = None
    last_exit_utc: Optional[str] = None
    forward_days_observed: Optional[int] = None
    trade_days_distinct: int = 0
    earlier_half_mean_r: Optional[float] = None
    later_half_mean_r: Optional[float] = None
    halves_diff_r: Optional[float] = None
    halves_diff_se: Optional[float] = None
    min_forward_trades: int = GATE_MIN_FORWARD_TRADES
    min_forward_days: int = GATE_MIN_FORWARD_DAYS
    trades_at_or_above_min: bool = False
    days_at_or_above_min: bool = False
    is_gate_decision: bool = field(default=False, init=False)

    @property
    def trades_vs_gate(self) -> str:
        return f"{self.n_closed_trades} of {self.min_forward_trades}"

    @property
    def days_vs_gate(self) -> str:
        days = "?" if self.forward_days_observed is None else self.forward_days_observed
        return f"{days} of {self.min_forward_days}"

    def as_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["trades"] = [dict(t) for t in self.trades]
        out["reasons"] = list(self.reasons)
        out["trades_vs_gate"] = self.trades_vs_gate
        out["days_vs_gate"] = self.days_vs_gate
        out["is_gate_decision"] = False
        return out


def _epoch(stamp: Any) -> Optional[float]:
    if not isinstance(stamp, str):
        return None
    try:
        return float(calendar.timegm(time.strptime(stamp, _STAMP)))
    except ValueError:
        return None


def _utc(epoch: float) -> str:
    return time.strftime(_STAMP, time.gmtime(epoch))


def _finite(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _var(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    return sum((v - m) ** 2 for v in values) / (len(values) - 1)


def _halves(net_r: Sequence[float]) -> Tuple[float, float, float, float]:
    """(earlier mean, later mean, later-earlier, standard error of that)."""
    k = len(net_r) // 2
    early, late = list(net_r[:k]), list(net_r[-k:])
    se = math.sqrt(_var(early) / k + _var(late) / k)
    return _mean(early), _mean(late), _mean(late) - _mean(early), se


def _trend(n: int, diff: Optional[float], se: Optional[float],
           min_trades: int) -> str:
    if n < min_trades or diff is None or se is None:
        return INSUFFICIENT_SAMPLE
    band = TREND_Z * se
    if diff < -band:
        return DETERIORATING
    if diff > band:
        return RECOVERING
    return STABLE


def evaluate_forward_evidence(
        path: str = DEFAULT_ARTIFACT, *, now: float,
        max_age_s: float = DEFAULT_MAX_AGE_S,
        min_trades_for_trend: int = DEFAULT_MIN_TRADES_FOR_TREND,
        min_forward_trades: int = GATE_MIN_FORWARD_TRADES,
        min_forward_days: int = GATE_MIN_FORWARD_DAYS) -> ForwardStatus:
    """Read ``path`` and report its facts as of ``now`` (epoch seconds).

    ``now`` is required: a status without an explicit clock cannot be
    reproduced. ``max_age_s`` must be a finite positive number and
    ``min_trades_for_trend`` at least 2; anything else raises ValueError (a
    limit that cannot be compared would let every artefact pass). Never
    raises for anything about the FILE: those are NO_DATA / UNREADABLE.
    """
    if not (_finite(now) and now > 0):
        raise ValueError(f"now must be a finite epoch, not {now!r}")
    if not (_finite(max_age_s) and max_age_s > 0):
        raise ValueError(f"max_age_s must be finite and > 0, not {max_age_s!r}")
    if isinstance(min_trades_for_trend, bool) or not isinstance(
            min_trades_for_trend, int) or min_trades_for_trend < 2:
        raise ValueError("min_trades_for_trend must be an int >= 2")

    base = dict(path=str(path), now_utc=_utc(now), max_age_s=float(max_age_s),
                min_trades_for_trend=int(min_trades_for_trend),
                min_forward_trades=int(min_forward_trades),
                min_forward_days=int(min_forward_days))

    def refused(state: str, reason: str, **extra: Any) -> ForwardStatus:
        return ForwardStatus(state=state, evidence_state=state,
                             reasons=(reason,), **base, **extra)

    if not os.path.exists(path):
        return refused(NO_DATA, f"no forward-shadow artefact at {path}")
    try:
        with open(path, encoding="utf-8") as fh:
            art = json.load(fh)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return refused(UNREADABLE, f"artefact unreadable ({type(exc).__name__})")
    if not isinstance(art, dict):
        return refused(UNREADABLE, "artefact is not a JSON object")

    ident = dict(signal=art.get("signal") if isinstance(art.get("signal"), str) else None,
                 symbol=art.get("symbol") if isinstance(art.get("symbol"), str) else None,
                 schema=art.get("schema") if isinstance(art.get("schema"), str) else None)
    observed = _epoch(art.get("observed_at_utc"))
    if observed is None:
        return refused(UNREADABLE, "observed_at_utc is missing or not "
                                   f"{_STAMP}: {art.get('observed_at_utc')!r}",
                       **ident)
    ident["observed_at_utc"] = art["observed_at_utc"]
    age = now - observed
    ident["age_s"] = age
    if age < -FUTURE_TOLERANCE_S:
        return refused(UNREADABLE, f"observed_at_utc is {-age:.0f}s after now: "
                                   "a broken clock or a forged stamp", **ident)

    raw_trades = art.get("trades")
    if not isinstance(raw_trades, list):
        return refused(UNREADABLE, "trades is missing or not a list", **ident)
    declared = art.get("forward_n_trades")
    declared_n = declared if isinstance(declared, int) and not isinstance(
        declared, bool) else None
    ident["declared_n_trades"] = declared_n

    problems: List[str] = []
    rows: List[Dict[str, Any]] = []
    seen = set()
    duplicates = 0
    for i, t in enumerate(raw_trades):
        if not isinstance(t, dict):
            problems.append(f"trade {i} is not an object")
            continue
        entry, exit_ = _epoch(t.get("entry_utc")), _epoch(t.get("exit_utc"))
        if entry is None or exit_ is None:
            problems.append(f"trade {i} has no parseable entry_utc/exit_utc")
            continue
        if not _finite(t.get("net_r")):
            problems.append(f"trade {i} net_r is {t.get('net_r')!r}")
            continue
        if exit_ < entry:
            problems.append(f"trade {i} exits before it enters")
            continue
        if exit_ > observed:
            problems.append(f"trade {i} exits after the observation was taken")
            continue
        key = (t["entry_utc"], t["exit_utc"], t.get("direction"),
               float(t["net_r"]))
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        rows.append({"entry_utc": t["entry_utc"], "exit_utc": t["exit_utc"],
                     "direction": t.get("direction"),
                     "net_r": float(t["net_r"]),
                     "notional_usd": t.get("notional_usd"),
                     "_order": (exit_, entry)})
    if duplicates:
        problems.append(f"{duplicates} trade row(s) repeat an earlier row; a "
                        "repeated trade is never counted twice")
    if declared_n is None:
        problems.append(f"forward_n_trades is {declared!r}, not an integer")
    elif not problems and declared_n != len(rows):
        problems.append(f"forward_n_trades says {declared_n} but the artefact "
                        f"lists {len(rows)} distinct closed trades")

    rows.sort(key=lambda r: r["_order"])
    for r in rows:
        r.pop("_order")
    net = [r["net_r"] for r in rows]
    n = len(rows)

    days_observed: Optional[int] = None
    window = art.get("forward_window")
    if isinstance(window, dict) and isinstance(window.get("bar_utcs"), list):
        stamps = [_epoch(s) for s in window["bar_utcs"]]
        if any(s is None for s in stamps):
            problems.append("forward_window.bar_utcs holds an unparseable stamp")
        else:
            days_observed = len({time.strftime("%Y-%m-%d", time.gmtime(s))
                                 for s in stamps if s <= observed})

    trade_days = {r[k][:10] for r in rows for k in ("entry_utc", "exit_utc")}
    early = late = diff = se = None
    if n >= 2:
        early, late, diff, se = _halves(net)
    evidence = _trend(n, diff, se, min_trades_for_trend)

    facts = dict(
        ident, n_closed_trades=n, duplicate_trades=duplicates,
        mean_net_r=_mean(net) if net else None, trades=tuple(rows),
        first_entry_utc=min((r["entry_utc"] for r in rows), default=None),
        last_entry_utc=max((r["entry_utc"] for r in rows), default=None),
        first_exit_utc=rows[0]["exit_utc"] if rows else None,
        last_exit_utc=rows[-1]["exit_utc"] if rows else None,
        forward_days_observed=days_observed,
        trade_days_distinct=len(trade_days),
        earlier_half_mean_r=early, later_half_mean_r=late,
        halves_diff_r=diff, halves_diff_se=se,
        trades_at_or_above_min=n >= min_forward_trades,
        days_at_or_above_min=(days_observed is not None
                              and days_observed >= min_forward_days))

    if problems:
        return ForwardStatus(state=UNREADABLE, evidence_state=UNREADABLE,
                             reasons=tuple(problems), fresh=None,
                             **base, **facts)

    fresh = age <= max_age_s
    reasons: List[str] = []
    if not fresh:
        reasons.append(f"observed_at_utc {art['observed_at_utc']} is "
                       f"{age / 3600.0:.1f}h old (limit {max_age_s / 3600.0:.1f}h)")
    if evidence == INSUFFICIENT_SAMPLE:
        reasons.append(f"{n} closed trades; a halves comparison needs "
                       f"{min_trades_for_trend}")
    else:
        reasons.append(f"later-half mean {late:+.4f}R vs earlier {early:+.4f}R "
                       f"(diff {diff:+.4f}R, {TREND_Z:g}*se {TREND_Z * se:.4f}R)")
    reasons.append(f"forward trades {n} of {min_forward_trades}; forward days "
                   f"{'?' if days_observed is None else days_observed} of "
                   f"{min_forward_days} (facts, not a gate decision)")
    return ForwardStatus(state=evidence if fresh else STALE,
                         evidence_state=evidence, reasons=tuple(reasons),
                         fresh=fresh, **base, **facts)
