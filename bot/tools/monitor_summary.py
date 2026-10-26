#!/usr/bin/env python3
"""Read the runner's `monitor.jsonl` and say, plainly, whether it is healthy and
what it has learned. Read-only; stdin or a file; sends nothing anywhere.

    fly ssh console -a carry-drill -C "cat /data/monitor/monitor.jsonl" \\
        | python3 tools/monitor_summary.py
    python3 tools/monitor_summary.py /path/to/monitor.jsonl --stale-minutes 150

HEALTH (the exit code):
  0  the last cycle is recent, it was not MONITOR_FAILED, and no order was ever
     reported sent
  1  stale (no cycle within --stale-minutes: the runner is down or stuck), or the
     last cycle was MONITOR_FAILED, or its shadow step faulted/was refused
  2  no readable cycles at all
  3  a line says `order_sent: true`. The monitor cannot send orders, so this is an
     alarm about the data, not a reading: investigate before anything else

LEARNING (printed, never part of the exit code): how often the entry gate was
open, which reasons closed it, and the perp-over-spot basis the cycles saw. A
basis that is persistently far outside the carry budget on the testnet says the
testnet's own spot market is distorted, not that the strategy is wrong, and no
count here is evidence of profit.
"""
from __future__ import annotations

import argparse
import calendar
import json
import statistics
import sys
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence

SCHEMA_NOTE = ("every figure is a model reading of public testnet data; none is "
               "evidence of profit")


def parse_lines(text: str) -> List[Dict[str, Any]]:
    """Every line that is a JSON object with an `at` and an `outcome`; anything
    else (a torn last line, a blank) is skipped, never guessed at."""
    rows: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("at"), str) \
                and isinstance(obj.get("outcome"), str):
            rows.append(obj)
    return rows


def _epoch(stamp: str) -> Optional[float]:
    try:
        return float(calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def _stat(values: Sequence[float]) -> Optional[Dict[str, float]]:
    vals = [float(v) for v in values
            if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not vals:
        return None
    return {"n": len(vals), "min": min(vals), "median": statistics.median(vals),
            "max": max(vals)}


def summarise(rows: List[Dict[str, Any]], *, now: float,
              stale_minutes: float = 150.0, last_n: int = 0) -> Dict[str, Any]:
    if not rows:
        return {"health": "NO_DATA", "exit": 2, "problems": ["no readable cycles"]}
    window = rows[-last_n:] if last_n else rows
    last = rows[-1]
    problems: List[str] = []
    alarms: List[str] = []
    ts = _epoch(last["at"])
    age_min = None if ts is None else (now - ts) / 60.0
    if age_min is None:
        problems.append(f"the last cycle's time {last['at']!r} is unreadable")
    elif age_min > stale_minutes:
        problems.append(f"STALE: the last cycle was {age_min:.0f} min ago "
                        f"(limit {stale_minutes:.0f}); the runner is down or stuck")
    if last["outcome"] == "MONITOR_FAILED":
        problems.append("the last cycle was MONITOR_FAILED: "
                        + "; ".join(map(str, last.get("reasons") or [])))
    if last.get("shadow_state") in ("FAULT", "REFUSED"):
        problems.append(f"the last shadow step was {last['shadow_state']}: "
                        f"{last.get('shadow_detail', 'no detail recorded')}")
    sent = [r["at"] for r in rows if r.get("order_sent") is not False]
    if sent:
        alarms.append(f"{len(sent)} line(s) do not say order_sent=false "
                      f"(first {sent[0]}): the monitor cannot send orders")
    outcomes = Counter(r["outcome"] for r in window)
    judged = [r for r in window if r["outcome"] != "MONITOR_FAILED"]
    open_n = sum(1 for r in judged if r["outcome"] == "MONITOR_READY")
    reasons = Counter(r.get("gate_reason") for r in judged if r.get("gate_reason")
                      or r["outcome"] == "ENTRY_NOT_ELIGIBLE")
    basis = _stat([r.get("basis_bps") for r in window])
    budget = _stat([r.get("basis_budget_bps") for r in window])
    out = {
        "health": "ALARM" if alarms else "PROBLEM" if problems else "OK",
        "exit": 3 if alarms else 1 if problems else 0,
        "problems": problems, "alarms": alarms,
        "cycles_total": len(rows), "cycles_in_window": len(window),
        "first": window[0]["at"], "last": last["at"],
        "last_cycle_age_minutes": None if age_min is None else round(age_min, 1),
        "outcomes": dict(outcomes),
        "gate_open_cycles": open_n, "gate_judged_cycles": len(judged),
        "gate_open_fraction": (open_n / len(judged)) if judged else None,
        "refusal_reasons": dict(reasons),
        "shadow_states": dict(Counter(r.get("shadow_state") or "none" for r in window)),
        "basis_bps": basis, "basis_budget_bps": budget,
        "smoothed_funding_bps": _stat([r.get("smoothed_funding_bps") for r in window]),
        "note": SCHEMA_NOTE,
    }
    return out


def render(s: Dict[str, Any]) -> str:
    if s["health"] == "NO_DATA":
        return "NO DATA: no readable cycles in the input"
    lines = [f"HEALTH {s['health']}"]
    for key in ("alarms", "problems"):
        lines += [f"  !! {x}" for x in s[key]]
    lines.append(f"cycles {s['cycles_in_window']} of {s['cycles_total']}  "
                 f"{s['first']} .. {s['last']}  (last {s['last_cycle_age_minutes']} min ago)")
    lines.append("outcomes   " + ", ".join(f"{k} x{v}" for k, v in sorted(s["outcomes"].items())))
    if s["gate_judged_cycles"]:
        lines.append(f"gate open  {s['gate_open_cycles']}/{s['gate_judged_cycles']} "
                     f"cycles ({100 * s['gate_open_fraction']:.0f}%)")
    if s["refusal_reasons"]:
        lines.append("refused by " + ", ".join(
            f"{k} x{v}" for k, v in sorted(s["refusal_reasons"].items(),
                                           key=lambda kv: -kv[1])))
    if s["basis_bps"]:
        b = s["basis_bps"]
        extra = ""
        if s["basis_budget_bps"]:
            extra = f"  (budget median {s['basis_budget_bps']['median']:.0f})"
        lines.append(f"basis bps  min {b['min']:.0f} median {b['median']:.0f} "
                     f"max {b['max']:.0f}{extra}")
    if s["smoothed_funding_bps"]:
        f = s["smoothed_funding_bps"]
        lines.append(f"funding    smoothed bps/8h min {f['min']:.2f} median "
                     f"{f['median']:.2f} max {f['max']:.2f}")
    lines.append("shadow     " + ", ".join(f"{k} x{v}" for k, v in sorted(s["shadow_states"].items())))
    lines.append(s["note"])
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("path", nargs="?", default="-", help="monitor.jsonl, or - for stdin")
    ap.add_argument("--stale-minutes", type=float, default=150.0)
    ap.add_argument("--last", type=int, default=0, help="only the last N cycles")
    ap.add_argument("--now", type=float, default=None, help="epoch seconds (tests)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        text = sys.stdin.read() if args.path == "-" else open(args.path, encoding="utf-8").read()
    except OSError as exc:
        print(f"cannot read {args.path}: {exc}", file=sys.stderr)
        return 2
    summary = summarise(parse_lines(text), now=time.time() if args.now is None else args.now,
                        stale_minutes=args.stale_minutes, last_n=args.last)
    print(json.dumps(summary, indent=2, sort_keys=True) if args.json else render(summary))
    return int(summary["exit"])


if __name__ == "__main__":
    raise SystemExit(main())
