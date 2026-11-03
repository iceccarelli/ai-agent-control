"""The inputs only the OWNER can supply, stated as what they are.

Three settings decide whether the carry book's entries can ever be qualified:
the discovery window (when the strategy was designed and tuned, so evidence must
come from after it), the drawdown limit (the largest drawdown the evidence may
show, in USD), and the entry net-benefit mode. This module reads them and says,
for the status page and the log, which are set, which are missing, which are
unusable, and what that means. It suggests no value for any of them and has no
default to fall back on; the two evidence inputs have none in `config` either.

Pure: no I/O. The same functions feed the evidence provider, so what the status
reports and what the gate uses cannot drift apart.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

SET, UNSET, INVALID = "SET", "UNSET", "INVALID"


def _num(x: Any) -> Optional[float]:
    if isinstance(x, bool) or x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def discovery_window(cfg: Any, *, now_s: Optional[float] = None  # now_s kept for callers
                     ) -> Tuple[Optional[Tuple[float, float]], str, str]:
    """(window, state, reason). The window is None unless both ends are set,
    and start < end. Whether evidence is independent of the window (including a
    window still open) is `strategy_evidence`'s judgement, not repeated here."""
    start = _num(getattr(cfg, "CARRY_EVIDENCE_DISCOVERY_START_EPOCH", None))
    end = _num(getattr(cfg, "CARRY_EVIDENCE_DISCOVERY_END_EPOCH", None))
    if start is None and end is None:
        return None, UNSET, "neither end of the discovery window is set"
    if start is None or end is None:
        return None, INVALID, "only one end of the discovery window is set"
    if not start < end:
        return None, INVALID, "the window's start is not before its end"
    return (start, end), SET, ""


def drawdown_limit(cfg: Any) -> Tuple[Optional[float], str, str]:
    raw = getattr(cfg, "CARRY_EVIDENCE_DRAWDOWN_LIMIT_USD", None)
    if raw is None:
        return None, UNSET, "no drawdown limit is set"
    v = _num(raw)
    if v is None or v <= 0:
        return None, INVALID, "the drawdown limit is not a positive number"
    return v, SET, ""


def opportunity_cost_assumption(cfg: Any) -> Dict[str, Any]:
    """What the entry gate assumes about the OWNER's capital. Not a required
    input and never given a value here: no verified hurdle rate exists in this
    repository, and an unset setting is not a rate. The gate's default is 0.0,
    which prices the owner's capital (BTC already held, margin posted) as free.
    `cfg` may carry None (the setting was not supplied) or the parsed number."""
    raw = getattr(cfg, "CARRY_OPPORTUNITY_COST_APR", None)
    v = _num(raw)
    if raw is None:
        state, meaning = "UNSET_TREATED_AS_ZERO", (
            "the entry gate treats the owner's capital as FREE (0.0 "
            "opportunity cost); no hurdle rate has been supplied and none is "
            "suggested")
    elif v is None or v < 0:
        state, meaning = INVALID, "not a usable non-negative number"
    elif v == 0.0:
        state, meaning = "ZERO_UNSET_OR_EXPLICIT", (
            "0.0: either unset or explicitly zero (indistinguishable once "
            "parsed); the entry gate treats the owner's capital as FREE")
    else:
        state, meaning = "USER_SUPPLIED", (
            f"{v:.4f}/yr as supplied by the operator; this software has not "
            "verified it against any actual alternative use of the capital")
    return {"name": "CARRY_OPPORTUNITY_COST_APR", "state": state,
            "value": v, "meaning": meaning,
            "affects_policy_inputs_complete": False}


def unknown_status() -> Dict[str, Any]:
    """The same shape as `owner_policy_status`, for when no configuration is
    available: nothing is known, so nothing is claimed."""
    return {
        "discovery_window": {"state": "UNKNOWN", "reason": "no configuration",
                             "start_epoch": None, "end_epoch": None},
        "drawdown_limit_usd": {"state": "UNKNOWN", "reason": "no configuration",
                               "value": None},
        "entry_benefit_mode": None, "income_haircut_fraction": None,
        "missing_or_unusable": ["configuration unavailable"],
        "policy_inputs_complete": False,
        "assumptions": [opportunity_cost_assumption(None)],
        "consequence": "unknown", "note": "these are owner decisions; this "
                                          "software suggests no values"}


def owner_policy_status(cfg: Any, *, now_s: Optional[float] = None
                        ) -> Dict[str, Any]:
    """Which owner inputs exist, and the consequence of those that do not."""
    window, w_state, w_why = discovery_window(cfg, now_s=now_s)
    limit, d_state, d_why = drawdown_limit(cfg)
    missing: List[str] = []
    if w_state != SET:
        missing.append("CARRY_EVIDENCE_DISCOVERY_START_EPOCH/_END_EPOCH")
    if d_state != SET:
        missing.append("CARRY_EVIDENCE_DRAWDOWN_LIMIT_USD")
    mode = str(getattr(cfg, "CARRY_ENTRY_BENEFIT_MODE", "enforce"))
    ok = not missing
    return {
        "discovery_window": {"state": w_state, "reason": w_why,
                             "start_epoch": window[0] if window else None,
                             "end_epoch": window[1] if window else None},
        "drawdown_limit_usd": {"state": d_state, "reason": d_why, "value": limit},
        "entry_benefit_mode": mode,
        "income_haircut_fraction": getattr(cfg, "CARRY_INCOME_HAIRCUT_FRACTION", None),
        "missing_or_unusable": missing,
        "policy_inputs_complete": ok,
        # Assumptions the gate makes on the owner's behalf. They are reported,
        # not required: they do not change `policy_inputs_complete`.
        "assumptions": [opportunity_cost_assumption(cfg)],
        "consequence": (
            "the evidence inputs are set; under 'enforce' entries stay refused "
            "until real execution samples AND reconciled cycles that pass the "
            "evidence policy also exist"
            if ok else
            "strategy evidence stays INSUFFICIENT, so under 'enforce' the entry "
            "gate refuses every entry; under 'observe' entries are not refused "
            "but are still not strategy-qualified"),
        "note": "these are owner decisions; this software suggests no values",
    }
