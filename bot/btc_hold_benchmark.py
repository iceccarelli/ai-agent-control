"""The yardstick a carry result must clear, defined before any result exists.

A carry cycle commits capital (margin) for a holding period and books a net
result AFTER every cost the ledger recorded. The alternative use of the same
capital over the same period is to hold BTC, whose result is the price return
over that period. A cycle "beats the hold" only if its net return on committed
capital exceeds the BTC return over the same interval; the strategy beats the
benchmark only if that holds on average AND the shortfall risk is bounded:

    excess_i = net_i / capital_i  -  (P_end_i / P_start_i - 1)

verdict GO needs, ALL of: an evidence class of ACTUAL_RECONCILED (a testnet,
paper, simulated or provisional cycle is never counted), at least
`min_cycles` confirmed cycles, mean(excess) - z * stderr(excess) > 0, and the
total net after costs > 0. `min_cycles` and `z` are OWNER inputs with no
default: while either is unset the verdict cannot be GO (it is INSUFFICIENT and
says which input is missing). NO_GO needs the same evidence AND a confident
shortfall (mean + z * stderr < 0) or a non-positive net. Anything else is
INSUFFICIENT. Pure; invents no threshold and reads no market.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence

GO, NO_GO, INSUFFICIENT = "GO", "NO_GO", "INSUFFICIENT"
ACTUAL = "ACTUAL_RECONCILED"


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def evaluate(cycles: Sequence[Mapping[str, Any]], *, evidence_class: str,
             min_cycles: Optional[int] = None, z: Optional[float] = None
             ) -> Dict[str, Any]:
    """Each cycle: net_usd, capital_usd (>0), price_start, price_end (>0),
    confirmed (bool). Unusable or unconfirmed cycles are excluded and counted."""
    used: List[float] = []
    nets: List[float] = []
    excluded = {"unconfirmed": 0, "unusable": 0}
    for c in cycles:
        if c.get("confirmed") is not True:
            excluded["unconfirmed"] += 1
            continue
        net, cap = _num(c.get("net_usd")), _num(c.get("capital_usd"))
        p0, p1 = _num(c.get("price_start")), _num(c.get("price_end"))
        if None in (net, cap, p0, p1) or cap <= 0 or p0 <= 0 or p1 <= 0:
            excluded["unusable"] += 1
            continue
        used.append(net / cap - (p1 / p0 - 1.0))
        nets.append(net)
    n = len(used)
    out: Dict[str, Any] = {"n_cycles": n, "excluded": excluded,
                           "evidence_class": evidence_class,
                           "total_net_usd": sum(nets) if n else None,
                           "mean_excess": None, "stderr_excess": None}
    if n:
        mean = sum(used) / n
        out["mean_excess"] = mean
        if n > 1:
            var = sum((x - mean) ** 2 for x in used) / (n - 1)
            out["stderr_excess"] = math.sqrt(var / n)
    missing = [name for name, v in (("min_cycles", min_cycles), ("z", z)) if v is None]
    if evidence_class != ACTUAL:
        return {**out, "verdict": INSUFFICIENT,
                "why": f"evidence class {evidence_class} is not ACTUAL_RECONCILED; "
                       "it can never support a GO or a NO_GO"}
    if missing:
        return {**out, "verdict": INSUFFICIENT,
                "why": "owner input(s) not set: " + ", ".join(missing)}
    if (isinstance(min_cycles, bool) or not isinstance(min_cycles, int) or min_cycles < 2
            or _num(z) is None or z <= 0):
        return {**out, "verdict": INSUFFICIENT, "why": "min_cycles must be an int >= 2 and z > 0"}
    if n < min_cycles or out["stderr_excess"] is None:
        return {**out, "verdict": INSUFFICIENT,
                "why": f"{n} confirmed cycle(s); the owner requires {min_cycles}"}
    lo = out["mean_excess"] - z * out["stderr_excess"]
    hi = out["mean_excess"] + z * out["stderr_excess"]
    out["excess_lower"], out["excess_upper"] = lo, hi
    if out["total_net_usd"] <= 0 or hi < 0:
        return {**out, "verdict": NO_GO,
                "why": "net after costs is not positive, or the shortfall against "
                       "holding BTC is confident"}
    if lo > 0:
        return {**out, "verdict": GO, "why": "confirmed, reconciled cycles beat the "
                "BTC hold on average with the owner's confidence margin"}
    return {**out, "verdict": INSUFFICIENT, "why": "the interval around the excess "
            "straddles zero; more reconciled cycles are needed"}
