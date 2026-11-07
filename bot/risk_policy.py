"""One risk-policy framework for a strategy and the portfolio: explicit status, explicit reason codes, fail-closed, and a breach latch that survives a restart.

A POLICY is a set of limits the OWNER has approved. ``Policy.approved_by_owner`` is False unless the owner's act sets it, and an unapproved policy REFUSES everything
(``NO_APPROVED_POLICY``): this module never activates a limit by itself, and it never raises a limit. A critical input that is missing or not a finite number is
a refusal (``MISSING_INPUT:<name>``), never "fine". A breach of a LOSS or FAULT limit latches the strategy SUSPENDED in a file: a restart does not clear it, and only
``RiskLatch.clear`` (an explicit act with a reason) does.

This is a pure decision module with a small durable latch. It is NOT yet consulted by the running book's entry path (that wiring is a separate, owner-approved change:
see tools/risk_policy_proposal.py for the proposed limits and their financial effect). Until then it is a tested library and a read-out, not a live control.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
from dataclasses import dataclass, fields
from typing import Any, Dict, List, Mapping, Optional, Tuple

ALLOW, REFUSE, SUSPEND = "ALLOW", "REFUSE", "SUSPEND"

#: limit name -> (state field it bounds, direction, latches). 'max' = the state value must not exceed the limit; 'min' = must not fall below it.
_RULES: Tuple[Tuple[str, str, str, bool], ...] = (
    ("per_trade_loss_usd", "trade_loss_usd", "max", True),
    ("daily_loss_usd", "loss_today_usd", "max", True),
    ("strategy_drawdown_usd", "strategy_drawdown_usd", "max", True),
    ("portfolio_drawdown_usd", "portfolio_drawdown_usd", "max", True),
    ("max_hold_hours", "hold_hours", "max", False),
    ("max_exposure_usd", "exposure_usd", "max", False),
    ("max_leverage", "leverage", "max", False),
    ("min_liquidity_usd", "liquidity_usd", "min", False),
    ("max_unhedged_usd", "unhedged_usd", "max", True),
    ("min_margin_multiple", "margin_multiple", "min", False),
    ("min_liquidation_distance_pct", "liquidation_distance_pct", "min", False),
    ("max_data_age_s", "data_age_s", "max", False),
    ("max_correlated_exposure_usd", "correlated_exposure_usd", "max", False),
    ("min_recent_funding_bps", "recent_mean_funding_bps", "min", False),
)


@dataclass(frozen=True)
class Policy:
    per_trade_loss_usd: float
    daily_loss_usd: float
    strategy_drawdown_usd: float
    portfolio_drawdown_usd: float
    max_hold_hours: float
    max_exposure_usd: float
    max_leverage: float
    min_liquidity_usd: float
    max_unhedged_usd: float
    min_margin_multiple: float
    min_liquidation_distance_pct: float
    max_data_age_s: float
    max_correlated_exposure_usd: float
    min_recent_funding_bps: float
    approved_by_owner: bool = False
    approval_note: str = ""


@dataclass(frozen=True)
class State:
    """Measured values at one decision point. None = not measured."""
    trade_loss_usd: Optional[float] = None
    loss_today_usd: Optional[float] = None
    strategy_drawdown_usd: Optional[float] = None
    portfolio_drawdown_usd: Optional[float] = None
    hold_hours: Optional[float] = None
    exposure_usd: Optional[float] = None
    leverage: Optional[float] = None
    liquidity_usd: Optional[float] = None
    unhedged_usd: Optional[float] = None
    margin_multiple: Optional[float] = None
    liquidation_distance_pct: Optional[float] = None
    data_age_s: Optional[float] = None
    correlated_exposure_usd: Optional[float] = None
    recent_mean_funding_bps: Optional[float] = None
    operational_fault: Optional[bool] = None


@dataclass(frozen=True)
class Decision:
    status: str
    reasons: Tuple[str, ...]
    latch: Tuple[str, ...] = ()          # the reasons that must latch the strategy suspended


def _finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


#: A measured value that is negative where a negative is impossible (an age, an exposure, a loss magnitude, a margin multiple) is a broken measurement, not a
#: very small number: it must not slip under a 'max' limit. Funding may legitimately be negative.
_MAY_BE_NEGATIVE = frozenset({"recent_mean_funding_bps"})
#: Limits are magnitudes; a negative one is a typo that would make a 'min' limit never bind.
_LIMIT_MAY_BE_NEGATIVE = frozenset({"min_recent_funding_bps"})


def evaluate(policy: Optional[Policy], state: State) -> Decision:
    """ALLOW only if the policy is owner-approved, every input is measured and no limit is breached and no fault is flagged."""
    if policy is None or policy.approved_by_owner is not True:          # strictly True: the string "False" is truthy and must not approve anything
        return Decision(REFUSE, ("NO_APPROVED_POLICY",))
    reasons: List[str] = []
    latch: List[str] = []
    for lim_name, field_name, direction, latches in _RULES:
        limit, value = getattr(policy, lim_name), getattr(state, field_name)
        if not _finite(limit) or (limit < 0 and lim_name not in _LIMIT_MAY_BE_NEGATIVE):
            reasons.append(f"POLICY_INVALID:{lim_name}")
            continue
        if not _finite(value) or (value < 0 and field_name not in _MAY_BE_NEGATIVE):
            reasons.append(f"MISSING_INPUT:{field_name}")
            continue
        if (direction == "max" and value > limit) or (direction == "min" and value < limit):
            code = f"BREACH:{lim_name}"
            reasons.append(code)
            if latches:
                latch.append(code)
    if state.operational_fault is None:
        reasons.append("MISSING_INPUT:operational_fault")
    elif state.operational_fault:
        reasons.append("OPERATIONAL_FAULT")
        latch.append("OPERATIONAL_FAULT")
    if not reasons:
        return Decision(ALLOW, ())
    return Decision(SUSPEND if latch else REFUSE, tuple(reasons), tuple(latch))


class RiskLatch:
    """Durable suspension. A latched strategy stays suspended across restarts until ``clear`` is called with a reason."""

    def __init__(self, path: str) -> None:
        self.path = path

    def _read(self) -> Dict[str, Any]:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, encoding="utf-8") as fh:
                doc = json.load(fh)
            if not isinstance(doc, dict):
                raise ValueError("not an object")
            return doc
        except (OSError, ValueError):
            return {"__unreadable__": {"reasons": ["LATCH_FILE_UNREADABLE"]}}      # an unreadable latch is a SUSPENDED latch

    def _write(self, doc: Mapping[str, Any]) -> None:
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def _locked(self):
        """Exclusive lock for a read-modify-write (two processes latching at once must not lose one)."""
        class _L:
            def __enter__(inner):
                inner.fd = os.open(self.path + ".lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
                fcntl.flock(inner.fd, fcntl.LOCK_EX)
                return inner

            def __exit__(inner, *a):
                os.close(inner.fd)
        return _L()

    def is_suspended(self, strategy: str) -> Tuple[bool, Tuple[str, ...]]:
        doc = self._read()
        for key in (strategy, "__unreadable__"):
            if key in doc:
                entry = doc[key]
                return True, tuple(entry.get("reasons", ())) if isinstance(entry, dict) else ("LATCH_ENTRY_UNREADABLE",)    # a malformed entry still suspends
        return False, ()

    def apply(self, strategy: str, decision: Decision, *, at_ms: int) -> Decision:
        """Record a latching decision; return the decision actually in force (a latched strategy is SUSPENDED whatever this check said)."""
        if decision.latch:
            with self._locked():
                doc = self._read()
                doc[strategy] = {"reasons": list(decision.latch), "since_ms": int(at_ms)}
                self._write(doc)
        sus, reasons = self.is_suspended(strategy)
        if sus and decision.status != SUSPEND:
            return Decision(SUSPEND, tuple(dict.fromkeys(("LATCHED",) + tuple(reasons) + decision.reasons)), tuple(reasons))
        return decision

    def clear(self, strategy: str, *, reason: str, at_ms: int) -> None:
        if not str(reason).strip():
            raise ValueError("clearing a latch needs a stated reason")
        with self._locked():
            doc = self._read()
            if "__unreadable__" in doc:
                raise ValueError("the latch file is unreadable: repair or replace it by hand before clearing anything")
            was = doc.pop(strategy, None)
            doc.setdefault("__history__", []).append({"cleared": strategy, "was": was, "reason": reason, "at_ms": int(at_ms)})      # the clearing is itself on record
            self._write(doc)
