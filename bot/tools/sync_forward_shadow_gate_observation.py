#!/usr/bin/env python3
"""Sync the promotion gate's `forward_shadow_clean` counters to the
installed forward-shadow artifact.

WHY THIS EXISTS
================
`artifacts/slice59_promotion_gate.json` is a static JSON file. Nothing
updates it automatically when a new `forward_shadow_current.json` is
installed or promoted, so it can silently go stale — the gate keeps
reporting yesterday's Stage B counters while every OTHER reader
(`control_plane_tick.py`, `reviewer_verdict.py`, this repo's own docs) has
already moved on. That desync is exactly what this tool corrects, and
nothing else: it touches ONE checklist item
(`checklist.forward_shadow_clean`) and no other field in the gate file.

WHAT THIS TOOL WILL NOT DO
===========================
* It never writes `human_risk_memo_signed`, `linear_protective_stop_verified`,
  `live_trading_ack_present`, `m4_recent_half_accepted_or_recovered`,
  `kill_switch_drill_recorded`, or any machine item — those stay exactly as
  they were.
* It never sets `complete: true` unless BOTH `trades >= 20` AND
  `days >= 180` are true of the freshly-resolved counters. There is no
  argument or code path that overrides this.
* It never touches `config`, `live_authorized`, or the kill switch, and it
  prints no secret.
* It is dry-run by default. `--write` is required to persist anything.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, Optional, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from stage_b_bars import resolve_closed_forward_bars  # noqa: E402
import promotion_gate as pg  # noqa: E402

DEFAULT_SHADOW = os.path.join(REPO, "artifacts", "forward_shadow_current.json")
DEFAULT_GATE = pg.GATE_PATH

MIN_TRADES = 20
MIN_DAYS = 180

_LEAD_RE = re.compile(
    r"\d+ of 20 forward closed trades; \d+ of 180 forward days,")
_TAIL_RE = re.compile(
    r"(NOT complete|COMPLETE): \d+ [<>=]+ 20 and \d+ [<>=]+ 180")


def _load_json(path: Optional[str]) -> Optional[Dict[str, Any]]:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:  # noqa: BLE001
        return None
    return payload if isinstance(payload, dict) else None


def resolve_observation(shadow: Optional[Dict[str, Any]]
                         ) -> Optional[Tuple[int, int]]:
    """(trades, days) from a shadow document, or None if unresolvable."""
    if not isinstance(shadow, dict):
        return None
    trades = shadow.get("forward_n_trades")
    if not isinstance(trades, int):
        return None
    try:
        days = resolve_closed_forward_bars(shadow)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(days, int):
        return None
    return trades, days


def _rewrite_evidence(evidence: str, trades: int, days: int,
                       complete: bool) -> str:
    """Swap only the leading counter clause and the completeness tail.

    Every other word of the existing honesty text (the ladder, the
    sixteen-slices note, `how_to_satisfy`) is untouched — this function
    never invents new prose, it only re-numbers the two clauses whose job
    is to state the counters.
    """
    evidence = evidence or ""
    new_lead = f"{trades} of 20 forward closed trades; {days} of 180 forward days,"
    if _LEAD_RE.search(evidence):
        evidence = _LEAD_RE.sub(new_lead, evidence, count=1)
    else:
        evidence = f"{new_lead} {evidence}".strip()

    if complete:
        new_tail = f"COMPLETE: {trades} >= 20 and {days} >= 180"
    else:
        new_tail = f"NOT complete: {trades} < 20 and {days} < 180"
    if _TAIL_RE.search(evidence):
        evidence = _TAIL_RE.sub(new_tail, evidence, count=1)
    elif not complete:
        evidence = evidence.rstrip()
        if not evidence.endswith("."):
            evidence += "."
        evidence += (f" {new_tail}, and the monitors need 10 and 20 closed "
                     f"trades before they arm.")
    return evidence


def sync(*, shadow_path: str = DEFAULT_SHADOW, gate_path: str = DEFAULT_GATE,
          write: bool = False) -> Dict[str, Any]:
    shadow = _load_json(shadow_path)
    if shadow is None:
        return {"ok": False,
                "reason": f"shadow file missing or unreadable: {shadow_path}"}

    observation = resolve_observation(shadow)
    if observation is None:
        return {"ok": False,
                "reason": ("could not resolve forward_n_trades / closed "
                           "forward days from the shadow document")}
    trades, days = observation

    gate = _load_json(gate_path)
    if gate is None or not isinstance(gate.get("checklist"), dict):
        return {"ok": False,
                "reason": (f"gate file missing, unreadable, or has no "
                           f"checklist: {gate_path}")}

    item = gate["checklist"].get("forward_shadow_clean")
    if not isinstance(item, dict):
        return {"ok": False,
                "reason": "gate file has no checklist.forward_shadow_clean"}

    old_trades = item.get("forward_trades_to_date")
    old_days = item.get("forward_days_to_date")
    old_complete = item.get("complete")
    old_evidence = item.get("evidence", "")

    complete = bool(trades >= MIN_TRADES and days >= MIN_DAYS)
    new_evidence = _rewrite_evidence(old_evidence, trades, days, complete)

    changed = (old_trades != trades or old_days != days
               or old_complete != complete or old_evidence != new_evidence)

    result: Dict[str, Any] = {
        "ok": True,
        "changed": changed,
        "written": False,
        "gate_path": gate_path,
        "shadow_path": shadow_path,
        "old": {"forward_trades_to_date": old_trades,
                "forward_days_to_date": old_days,
                "complete": old_complete},
        "new": {"forward_trades_to_date": trades,
                "forward_days_to_date": days,
                "complete": complete},
        "evidence_old": old_evidence,
        "evidence_new": new_evidence,
    }

    if write and changed:
        item["forward_trades_to_date"] = trades
        item["forward_days_to_date"] = days
        item["complete"] = complete
        item["evidence"] = new_evidence
        with open(gate_path, "w", encoding="utf-8") as handle:
            json.dump(gate, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        result["written"] = True

    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shadow", default=DEFAULT_SHADOW,
                        help="forward-shadow artifact to read (default: %(default)s)")
    parser.add_argument("--gate", default=DEFAULT_GATE,
                        help="promotion gate JSON to sync (default: %(default)s)")
    parser.add_argument("--write", action="store_true",
                        help="persist the change; default is dry-run (print only)")
    args = parser.parse_args(argv)

    result = sync(shadow_path=args.shadow, gate_path=args.gate,
                  write=args.write)
    if not result["ok"]:
        print(f"REFUSE: {result['reason']}")
        return 1

    old, new = result["old"], result["new"]
    print(f"forward_trades_to_date: {old['forward_trades_to_date']} -> "
          f"{new['forward_trades_to_date']}")
    print(f"forward_days_to_date:   {old['forward_days_to_date']} -> "
          f"{new['forward_days_to_date']}")
    print(f"complete:               {old['complete']} -> {new['complete']}")
    if not result["changed"]:
        print("no desync: gate already matches the shadow artifact.")
    elif result["written"]:
        print(f"WRITTEN: {result['gate_path']}")
    else:
        print("DRY-RUN: no file written. Pass --write to persist.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
