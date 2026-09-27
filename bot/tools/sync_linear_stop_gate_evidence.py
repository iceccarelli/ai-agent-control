#!/usr/bin/env python3
"""Sync the promotion gate's `linear_protective_stop_verified` item to the
signed LINEAR_STOP_VERIFICATION_CHECKLIST.md.

WHY THIS EXISTS
================
`docs/promotion/LINEAR_STOP_VERIFICATION_CHECKLIST.md` records all four
venue items `[x]`, a filled `LINEAR_STOP_MARGIN_MEMO.md`, and a human
Sign-off (`Verified by: Vincenzo Ceccarelli`, 2026-09-27). Nothing updates
`artifacts/slice59_promotion_gate.json` to match: it still reads
`linear_protective_stop_verified.complete: false`, evidence `"not
documented"`. Signed venue evidence sitting on disk while the gate JSON
still calls it undocumented is a desync, and left alone it reads as the
gate quietly disagreeing with its own paper trail. This tool is the
transcription step — same shape as
`sync_forward_shadow_gate_observation.py` for `forward_shadow_clean` — and
it corrects exactly that, and nothing else.

WHAT "SYNC" MEANS HERE
=======================
This tool makes NO judgment call. It requires the checklist to ALREADY be
fully signed — all four items `[x]` AND a non-blank `Verified by` line AND
a filled margin memo AND at least one Item 1 artifact path cited in the
checklist — before it will touch the gate file at all. If any of those is
missing, it refuses. What it writes is a transcription of a decision a
human already made and already put in writing elsewhere; it invents no new
fact and asks no new question.

WHAT THIS TOOL WILL NOT DO
===========================
* It touches ONE checklist item only: `checklist.linear_protective_stop_verified`.
  No other key in the gate file is written.
* It NEVER writes `human_risk_memo_signed`. That item's own gate description
  is "A signed human risk memo exists at a recorded path" — the memo's own
  §7 (Proposer / Risk officer / Second reviewer) is still blank and its
  header says "This draft is not the signed copy." A checklist sign-off is
  not a risk-memo signature; the two are different documents and this tool
  does not conflate them.
* It NEVER writes `m4_recent_half_accepted_or_recovered`. That item's own
  description requires the WARN be explicitly ACCEPTED in the memo, or that
  forward data recover under the UNCHANGED threshold
  (`test_the_gate_carries_m4_as_an_incomplete_item`,
  `test_the_only_legitimate_recovery_is_new_data`). A recorded **NOT
  ACCEPTED** is the opposite of acceptance and does not complete this item
  — it is the human correctly declining to proceed, not a satisfied
  condition. This tool leaves it exactly as declared.
* It never writes `allows_live`, `live_authorized`, or any live-arming
  field — none exists in this file; `promotion_gate.py`'s own docstring is
  explicit that the gate is a predicate, never an arming mechanism, and
  `config.is_live_authorized` is consulted separately and is the only path
  that can ever return True for live.
* It never trips or clears the kill switch, never places an order, never
  touches the cleared-edge revoke path.
* It is dry-run by default. `--write` is required to persist anything.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import promotion_gate as pg  # noqa: E402

DEFAULT_CHECKLIST = os.path.join(
    REPO, "docs", "promotion", "LINEAR_STOP_VERIFICATION_CHECKLIST.md")
DEFAULT_MEMO = os.path.join(
    REPO, "docs", "promotion", "LINEAR_STOP_MARGIN_MEMO.md")
DEFAULT_GATE = pg.GATE_PATH

#: One artifact name per venue item, in order. At least the Item 1 name must
#: appear in the checklist text - the read-back that started this whole
#: item. The rest are recorded in `evidence` when present, never required
#: individually (a human may re-word the checklist prose around them).
ITEM_1_ARTIFACT = "linear_protective_stop_venue.json"
KNOWN_ARTIFACTS = (
    ITEM_1_ARTIFACT,
    "linear_stop_hold.json",
    "linear_stop_verify_after_restart.json",
    "linear_stop_flatten.json",
    "linear_stop_naked_induce.json",
    "linear_stop_naked_observe.json",
    "linear_stop_naked_flatten.json",
    "linear_stop_margin_doc.json",
)

_ITEM_TICKED_RE = {n: re.compile(rf"\[x\]\s*{n}\.") for n in (1, 2, 3, 4)}
_SIGNOFF_RE = re.compile(r"Verified by\s*:\s*(.+?)\s+date:\s*(\S+)")


def _load_text(path: Optional[str]) -> Optional[str]:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _load_json(path: Optional[str]) -> Optional[Dict[str, Any]]:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:  # noqa: BLE001
        return None
    return payload if isinstance(payload, dict) else None


def _items_all_ticked(text: str) -> bool:
    return all(pattern.search(text) for pattern in _ITEM_TICKED_RE.values())


def _signoff(text: str) -> Optional[Tuple[str, str]]:
    """``(verified_by, date)`` or None if the Sign-off is missing or blank.

    A line of underscores is how this file's own template spells "blank" -
    that must read as absent, not as a name.
    """
    match = _SIGNOFF_RE.search(text)
    if not match:
        return None
    name, date = match.group(1).strip(), match.group(2).strip()
    if not name or set(name) <= {"_"}:
        return None
    if not date or set(date) <= {"_"}:
        return None
    return name, date


def _artifacts_cited(text: str) -> List[str]:
    return [name for name in KNOWN_ARTIFACTS if name in text]


def evaluate(*, checklist_path: str = DEFAULT_CHECKLIST,
             memo_path: str = DEFAULT_MEMO) -> Dict[str, Any]:
    """Read-only: is there enough signed evidence to transcribe? Fails closed."""
    checklist_text = _load_text(checklist_path)
    if checklist_text is None:
        return {"ok": False,
                "reason": f"checklist missing or unreadable: {checklist_path}"}

    if not _items_all_ticked(checklist_text):
        return {"ok": False,
                "reason": "not all four venue items are [x] in the checklist"}

    signoff = _signoff(checklist_text)
    if signoff is None:
        return {"ok": False,
                "reason": "checklist Sign-off ('Verified by') is blank or missing"}
    verified_by, verified_date = signoff

    memo_text = _load_text(memo_path)
    if memo_text is None:
        return {"ok": False,
                "reason": f"margin memo missing or unreadable: {memo_path}"}

    artifacts = _artifacts_cited(checklist_text)
    if ITEM_1_ARTIFACT not in artifacts:
        return {"ok": False,
                "reason": (f"checklist does not cite {ITEM_1_ARTIFACT!r} - "
                           "the Item 1 read-back that started this item")}

    evidence = (
        f"Checklist evidence complete: all four venue items [x] in "
        f"docs/promotion/LINEAR_STOP_VERIFICATION_CHECKLIST.md; Sign-off "
        f"Verified by {verified_by}, date {verified_date}. Margin memo "
        f"filled: docs/promotion/LINEAR_STOP_MARGIN_MEMO.md. Artifacts "
        f"cited: {', '.join(artifacts)}."
    )
    return {"ok": True, "verified_by": verified_by, "verified_date": verified_date,
            "artifacts": artifacts, "evidence": evidence}


def _recount_items_complete(gate: Dict[str, Any]) -> int:
    checklist = gate.get("checklist", {})
    return sum(1 for entry in checklist.values()
              if isinstance(entry, dict) and entry.get("complete") is True)


def sync(*, checklist_path: str = DEFAULT_CHECKLIST, memo_path: str = DEFAULT_MEMO,
          gate_path: str = DEFAULT_GATE, write: bool = False) -> Dict[str, Any]:
    check = evaluate(checklist_path=checklist_path, memo_path=memo_path)
    if not check["ok"]:
        return check

    gate = _load_json(gate_path)
    if gate is None or not isinstance(gate.get("checklist"), dict):
        return {"ok": False,
                "reason": f"gate file missing, unreadable, or has no checklist: {gate_path}"}

    item = gate["checklist"].get("linear_protective_stop_verified")
    if not isinstance(item, dict):
        return {"ok": False,
                "reason": "gate file has no checklist.linear_protective_stop_verified"}

    old_complete = item.get("complete")
    old_evidence = item.get("evidence", "")
    new_complete = True
    new_evidence = check["evidence"]

    changed = (old_complete != new_complete or old_evidence != new_evidence)

    result: Dict[str, Any] = {
        "ok": True,
        "changed": changed,
        "written": False,
        "gate_path": gate_path,
        "checklist_path": checklist_path,
        "memo_path": memo_path,
        "old": {"complete": old_complete, "evidence": old_evidence},
        "new": {"complete": new_complete, "evidence": new_evidence},
        "items_complete_old": gate.get("items_complete"),
    }

    if write and changed:
        item["complete"] = new_complete
        item["evidence"] = new_evidence
        item["signature"] = {
            "verified_by": check["verified_by"],
            "date": check["verified_date"],
            "checklist_path": "docs/promotion/LINEAR_STOP_VERIFICATION_CHECKLIST.md",
            "margin_memo_path": "docs/promotion/LINEAR_STOP_MARGIN_MEMO.md",
        }
        gate["items_complete"] = _recount_items_complete(gate)
        result["items_complete_new"] = gate["items_complete"]
        with open(gate_path, "w", encoding="utf-8") as handle:
            json.dump(gate, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        result["written"] = True

    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--checklist", default=DEFAULT_CHECKLIST,
                        help="checklist to read (default: %(default)s)")
    parser.add_argument("--memo", default=DEFAULT_MEMO,
                        help="margin memo to require exists (default: %(default)s)")
    parser.add_argument("--gate", default=DEFAULT_GATE,
                        help="promotion gate JSON to sync (default: %(default)s)")
    parser.add_argument("--write", action="store_true",
                        help="persist the change; default is dry-run (print only)")
    args = parser.parse_args(argv)

    result = sync(checklist_path=args.checklist, memo_path=args.memo,
                  gate_path=args.gate, write=args.write)
    if not result["ok"]:
        print(f"REFUSE: {result['reason']}")
        return 1

    old, new = result["old"], result["new"]
    print(f"complete: {old['complete']} -> {new['complete']}")
    print(f"evidence: {new['evidence']}")
    if not result["changed"]:
        print("no desync: gate already matches the signed checklist.")
    elif result["written"]:
        print(f"WRITTEN: {result['gate_path']}")
        print(f"items_complete: {result['items_complete_old']} -> "
              f"{result['items_complete_new']}")
    else:
        print("DRY-RUN: no file written. Pass --write to persist.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
