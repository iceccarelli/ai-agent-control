"""Is the recorder actually collecting? Judged from the evidence's own clocks, never its past.

A recorder that stopped yesterday must not look healthy because the rows it wrote before stopping
were valid. Staleness is computed against NOW (supplied by the caller), and a stale recorder is
reported STALE whatever its last rows said. Cadence, gaps, uptime, timestamp skew, reconnects,
disk and supervisor restarts are reported as measured; nothing is inferred where a counter is
absent (it is reported as null).
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

DEFAULT_CADENCE_S = 300.0
STALE_AFTER_CADENCES = 3.0
GAP_AFTER_CADENCES = 2.0
RECENT_WINDOW_S = 24 * 3600.0         # a gap only degrades the verdict while it is recent;
RECENT_GAP_S = 3600.0                 # an old deploy gap stays in the list and in `completeness`, not in the verdict
CLOCK_AHEAD_TOLERANCE_S = 120.0       # a row dated further ahead than this is not from "now"


def _pct(xs: Sequence[float], p: float) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))]


def report(rows: Sequence[Mapping[str, Any]], *, now_ms: int, cadence_s: float = DEFAULT_CADENCE_S,
           supervisor: Optional[Mapping[str, Any]] = None, status: Optional[Mapping[str, Any]] = None,
           disk_bytes_by_day: Optional[Mapping[str, int]] = None) -> Dict[str, Any]:
    if not rows:
        return {"verdict": "NO_DATA", "why": "no validated rows: nothing can be called healthy",
                "now_ms": now_ms}
    ts = [r["at_ms"] for r in rows]
    first, last = ts[0], ts[-1]
    cad_ms = cadence_s * 1000.0
    window_s = (last - first) / 1000.0
    expected = int(window_s / cadence_s) + 1
    gaps: List[Dict[str, Any]] = []
    missing = 0
    for a, b in zip(ts, ts[1:]):
        if b - a > GAP_AFTER_CADENCES * cad_ms:
            gaps.append({"from_ms": a, "to_ms": b, "seconds": (b - a) / 1000.0})
            missing += int(round((b - a) / cad_ms)) - 1
    gap_s = sum(g["seconds"] for g in gaps)
    age_s = (now_ms - last) / 1000.0
    stale = age_s > STALE_AFTER_CADENCES * cadence_s
    skew = [r["at_ms"] - r["times"]["perp_book_exch_ts_ms"] for r in rows
            if isinstance((r.get("times") or {}).get("perp_book_exch_ts_ms"), (int, float))]
    last_health = (rows[-1].get("recorder") or {})
    first_health = (rows[0].get("recorder") or {})

    def delta(key: str) -> Optional[float]:
        a, b = first_health.get(key), last_health.get(key)
        return (b - a) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None

    faults = [r for r in rows if (r.get("recorder") or {}).get("store_fault")]
    out: Dict[str, Any] = {
        "now_ms": now_ms, "first_ms": first, "last_ms": last, "last_row_age_s": age_s,
        "stale": stale, "stale_after_s": STALE_AFTER_CADENCES * cadence_s,
        "window_hours": window_s / 3600.0, "rows": len(rows), "expected_rows": expected,
        "cadence_s": cadence_s, "completeness": min(1.0, len(rows) / expected) if expected else None,
        "gaps": gaps, "missing_intervals": missing, "max_gap_s": max((g["seconds"] for g in gaps), default=0.0),
        "uptime_fraction_in_window": (1.0 - gap_s / window_s) if window_s > 0 else None,
        "receive_minus_exchange_ms": {"n": len(skew), "median": _pct(skew, 0.5), "p95": _pct(skew, 0.95),
                                      "max": max(skew) if skew else None},
        "feed": {"linear_reconnects_in_window": delta("linear_reconnects"),
                 "spot_reconnects_in_window": delta("spot_reconnects"),
                 "rest_errors_in_window": delta("rest_errors"),
                 "book_rejections_in_window": delta("book_rejections"),
                 "rows_with_store_fault": len(faults)},
        "disk": {"free_bytes_last": last_health.get("free_bytes"), "min_free_bytes": last_health.get("min_free_bytes"),
                 "capacity_bytes_by_day": dict(disk_bytes_by_day or {})},
        "supervisor": None if supervisor is None else {"recorder_restarts": supervisor.get("recorder_restarts"),
                                                       "recorder_last_exit": supervisor.get("recorder_last_exit"),
                                                       "consecutive_failures": supervisor.get("recorder_consecutive_failures")},
        "recorder_errors": None if status is None else (status.get("capacity") or {}).get("errors"),
    }
    recent_gap = any(g["to_ms"] >= last - RECENT_WINDOW_S * 1000 and g["seconds"] > RECENT_GAP_S for g in gaps)
    out["recent_gap"] = recent_gap
    degraded = recent_gap or bool(faults) or (out["completeness"] or 1.0) < 0.9
    ahead = age_s < -CLOCK_AHEAD_TOLERANCE_S
    out["clock_ahead"] = ahead
    out["verdict"] = "CLOCK_AHEAD" if ahead else ("STALE" if stale else ("DEGRADED" if degraded else "HEALTHY"))
    out["why"] = ("the newest row is dated in the future: a wrong clock, or rows from another time; not collecting evidence"
                  if ahead else
                  (f"last row is {age_s / 60:.0f} min old (limit {STALE_AFTER_CADENCES * cadence_s / 60:.0f} min): "
                  "the recorder is not collecting" if stale else
                  ("a recent gap, store faults or incomplete cadence in the window" if degraded else "collecting at cadence")))
    return out
