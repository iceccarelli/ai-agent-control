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
import math
import re
import statistics
import sys
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA_NOTE = ("every figure is a model reading of public testnet data; none is "
               "evidence of profit")

#: The only outcomes the read-only monitor writes (tools/run_outcomes.py). A row
#: saying anything else (CONFORMANCE_PASSED belongs to the order-capable drill,
#: which never writes this file) was not produced by this monitor.
KNOWN_OUTCOMES = ("ENTRY_NOT_ELIGIBLE", "MONITOR_READY", "MONITOR_FAILED")


#: How far ahead of this machine's clock a line may be stamped before the
#: history is called inconsistent. A runner's clock and the operator's differ by
#: seconds; a stamp hours or years ahead is a forged line or a broken clock, and
#: either way "how long since the last cycle" cannot be answered.
FUTURE_TOLERANCE_MINUTES = 5.0

#: An unparseable line that still CLAIMS an order was sent (a torn write cut
#: after the field, say). Matched on text because it cannot be parsed.
_ORDER_SENT_TRUE = re.compile(
    r"""["']?order_sent["']?\s*:\s*(?:true|1|["']true["'])""", re.IGNORECASE)


def scan(text: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """(cycle rows, non-blank lines that were NOT read as a cycle row).

    A row is a JSON object with an `at` and an `outcome`. Everything else (a torn
    last line, a forged fragment) is skipped, never guessed at, but it is
    returned so the caller can count it and refuse to let it hide an
    `order_sent` claim."""
    rows: List[Dict[str, Any]] = []
    unread: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            unread.append(line)
            continue
        if isinstance(obj, dict) and isinstance(obj.get("at"), str) \
                and isinstance(obj.get("outcome"), str):
            rows.append(obj)
        else:
            unread.append(line)
    return rows, unread


def parse_lines(text: str) -> List[Dict[str, Any]]:
    """Every line that is a JSON object with an `at` and an `outcome`; anything
    else (a torn last line, a blank) is skipped, never guessed at."""
    return scan(text)[0]


def _claims_an_order(line: str) -> bool:
    """True when an unreadable line says an order was sent (or is not false)."""
    try:
        obj = json.loads(line)
    except ValueError:
        return bool(_ORDER_SENT_TRUE.search(line))
    return isinstance(obj, dict) and "order_sent" in obj \
        and obj["order_sent"] is not False


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
              stale_minutes: float = 150.0, last_n: int = 0,
              raw_text: Optional[str] = None) -> Dict[str, Any]:
    """`raw_text` is the file the rows came from. With it, lines that were not
    read as cycle rows are counted and any that claim an order was sent raise the
    alarm; without it only the rows are judged (the old call)."""
    unread = scan(raw_text)[1] if raw_text is not None else []
    forged = [u for u in unread if _claims_an_order(u)]
    if not rows:
        if forged:
            return {"health": "ALARM", "exit": 3, "problems": [],
                    "alarms": [f"{len(forged)} unreadable line(s) claim an "
                               "order was sent; no cycle row could be read"],
                    "unreadable_lines": len(unread)}
        return {"health": "NO_DATA", "exit": 2, "problems": ["no readable cycles"],
                "unreadable_lines": len(unread)}
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
    ahead = [r["at"] for r in rows
             if (_epoch(r["at"]) or 0.0) - now > FUTURE_TOLERANCE_MINUTES * 60.0]
    if ahead:
        problems.append(f"FUTURE: {len(ahead)} line(s) are stamped later than "
                        f"this machine's clock (first {ahead[0]}): a broken "
                        "clock or a forged line, so the age of the last cycle "
                        "cannot be trusted")
    foreign = sorted({str(r["outcome"]) for r in rows
                      if r["outcome"] not in KNOWN_OUTCOMES})
    if foreign:
        problems.append(f"OUTCOME: {len(foreign)} outcome value(s) the monitor "
                        f"cannot produce ({', '.join(map(repr, foreign[:3]))}): "
                        "a hand-edited or foreign history is not a healthy "
                        "runner")
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
    if forged:
        alarms.append(f"{len(forged)} line(s) that could not be read as a cycle "
                      "claim an order was sent (or carry order_sent other than "
                      "false): the monitor cannot send orders")
    outcomes = Counter(r["outcome"] for r in window)
    judged = [r for r in window if r["outcome"] != "MONITOR_FAILED"]
    open_n = sum(1 for r in judged if r["outcome"] == "MONITOR_READY")
    # Only a REFUSED cycle has a refusal reason. A cycle that passed carries
    # the gate's allow code (CARRY_CLEARS_COSTS_AND_BASIS), which is not one.
    reasons = Counter(r.get("gate_reason") or "UNRECORDED" for r in judged
                      if r["outcome"] == "ENTRY_NOT_ELIGIBLE")
    basis = _stat([r.get("basis_bps") for r in window])
    budget = _stat([r.get("basis_budget_bps") for r in window])
    out = {
        "health": "ALARM" if alarms else "PROBLEM" if problems else "OK",
        "exit": 3 if alarms else 1 if problems else 0,
        "problems": problems, "alarms": alarms,
        "unreadable_lines": len(unread),
        "cycles_total": len(rows), "cycles_in_window": len(window),
        "first": window[0]["at"], "last": last["at"],
        "last_cycle_age_minutes": None if age_min is None else round(age_min, 1),
        "outcomes": dict(outcomes),
        "gate_open_cycles": open_n, "gate_judged_cycles": len(judged),
        "gate_open_fraction": (open_n / len(judged)) if judged else None,
        "refusal_reasons": dict(reasons),
        "shadow_states": dict(Counter(r.get("shadow_state") or "none" for r in window)),
        "basis_bps": basis, "basis_budget_bps": budget,
        "mainnet_states": dict(Counter(r["mainnet_state"] for r in window
                                       if r.get("mainnet_state"))),
        "mainnet_basis_bps": _stat([r.get("mainnet_basis_bps") for r in window]),
        "smoothed_funding_bps": _stat([r.get("smoothed_funding_bps") for r in window]),
        "note": SCHEMA_NOTE,
    }
    return out


def render(s: Dict[str, Any]) -> str:
    if s["health"] == "NO_DATA":
        return "NO DATA: no readable cycles in the input"
    if "cycles_total" not in s:                    # an alarm raised with no rows
        return "\n".join([f"HEALTH {s['health']}"]
                          + [f"  !! {x}" for x in s["alarms"]])
    lines = [f"HEALTH {s['health']}"]
    for key in ("alarms", "problems"):
        lines += [f"  !! {x}" for x in s[key]]
    if s.get("unreadable_lines"):
        lines.append(f"  note: {s['unreadable_lines']} line(s) could not be read "
                     "as a cycle row and were skipped")
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
    if s.get("mainnet_states"):
        mb = s.get("mainnet_basis_bps")
        latest = f"  basis min {mb['min']:+.0f} median {mb['median']:+.0f} max {mb['max']:+.0f} bps" if mb else ""
        lines.append("mainnet    " + ", ".join(f"{k} x{v}" for k, v in sorted(s["mainnet_states"].items()))
                     + latest + "  (the real market's public numbers; observations, not profit)")
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
    if not (math.isfinite(args.stale_minutes) and args.stale_minutes > 0):
        # NaN and +inf make `age > limit` false for every age, so no runner
        # could ever be stale; zero and below are always stale. None is a limit.
        print(f"--stale-minutes must be a finite number above zero, not "
              f"{args.stale_minutes!r}", file=sys.stderr)
        return 2
    try:
        text = sys.stdin.read() if args.path == "-" else open(args.path, encoding="utf-8").read()
    except OSError as exc:
        print(f"cannot read {args.path}: {exc}", file=sys.stderr)
        return 2
    summary = summarise(parse_lines(text), now=time.time() if args.now is None else args.now,
                        stale_minutes=args.stale_minutes, last_n=args.last,
                        raw_text=text)
    print(json.dumps(summary, indent=2, sort_keys=True) if args.json else render(summary))
    return int(summary["exit"])


if __name__ == "__main__":
    raise SystemExit(main())
