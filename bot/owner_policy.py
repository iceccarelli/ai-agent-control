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
import time
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


def discovery_window(cfg: Any, *, now_s: Optional[float] = None
                     ) -> Tuple[Optional[Tuple[float, float]], str, str]:
    """(window, state, reason). The window is None unless both ends are set,
    start < end, and the window has ended (a window still open is not
    'before' anything)."""
    start = _num(getattr(cfg, "CARRY_EVIDENCE_DISCOVERY_START_EPOCH", None))
    end = _num(getattr(cfg, "CARRY_EVIDENCE_DISCOVERY_END_EPOCH", None))
    if start is None and end is None:
        return None, UNSET, "neither end of the discovery window is set"
    if start is None or end is None:
        return None, INVALID, "only one end of the discovery window is set"
    if not start < end:
        return None, INVALID, "the window's start is not before its end"
    now = time.time() if now_s is None else float(now_s)
    if end > now:
        return None, INVALID, ("the window has not ended yet; evidence cannot "
                               "be independent of a period still open")
    return (start, end), SET, ""


def drawdown_limit(cfg: Any) -> Tuple[Optional[float], str, str]:
    raw = getattr(cfg, "CARRY_EVIDENCE_DRAWDOWN_LIMIT_USD", None)
    if raw is None:
        return None, UNSET, "no drawdown limit is set"
    v = _num(raw)
    if v is None or v <= 0:
        return None, INVALID, "the drawdown limit is not a positive number"
    return v, SET, ""


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
        "evidence_can_qualify": ok,
        "consequence": (
            "strategy evidence can be judged once real reconciled cycles exist"
            if ok else
            "strategy evidence stays INSUFFICIENT, so under 'enforce' the entry "
            "gate refuses every entry; under 'observe' entries are not refused "
            "but are still not strategy-qualified"),
        "note": "these are owner decisions; this software suggests no values",
    }
