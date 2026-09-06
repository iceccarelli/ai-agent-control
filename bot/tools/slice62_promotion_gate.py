#!/usr/bin/env python3
"""Re-evaluate the inert promotion gate against REAL forward data. It refuses.

WHY THIS RUN IS DIFFERENT FROM SLICES 59-61
===========================================
Three previous slices re-evaluated this gate against a corpus that ended on the
day the cleared measurement ended. `forward_shadow_clean` read 0 / 20 because
zero bars existed to observe.

Bars now exist — one closed daily bar, `2026-08-10`, proven by prefix hash
(§45b). **The item still reads 0 / 20**, and the reason is the whole argument of
this slice: the gate counts CLOSED FORWARD TRADES, not bars, not flags, not
entries. One bar is pilot progress. It is not an observation, and the gate is
built so that wanting it to be one changes nothing.

WHAT THIS TOOL WRITES AND WHAT IT CANNOT
========================================
It writes a DATED snapshot, `artifacts/slice62_promotion_gate.json`. It does not
repoint `promotion_gate.GATE_PATH`, which stays on the canonical slice-59
declaration: re-aiming the live gate at a fresher file every slice would make
the gate's own wiring a moving part.

It cannot set `live_authorized`, cannot write config, cannot clear the kill
switch and cannot mark a human item complete. It reads the two machine items
back out of `evaluate_promotion_gate`, which re-derives them from the live tree,
so nothing it writes here can assert them.

**It refuses to write at all if the verdict is not REFUSE.** A tool that would
happily record a promotion is a tool that could be used to record one.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import promotion_gate as pg  # noqa: E402
import shadow  # noqa: E402

SCHEMA = "promotion_gate/3"
PREVIOUS = "artifacts/slice61_promotion_gate.json"
FRESHNESS = "artifacts/slice62_data_freshness.json"
FORWARD = "artifacts/slice62_forward_shadow.json"


def load(path: str) -> dict:
    with open(os.path.join(REPO, path), encoding="utf-8") as handle:
        return json.load(handle)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=os.path.join(
        REPO, "artifacts", "slice62_promotion_gate.json"))
    args = parser.parse_args(argv)

    previous = load(PREVIOUS)
    freshness = load(FRESHNESS)
    forward = load(FORWARD)
    verdict = pg.evaluate_promotion_gate()

    forward_trades = int(forward["forward_n_trades"])
    closed_bars = int(freshness["after_t1_linear"])

    checklist = json.loads(json.dumps(previous["checklist"]))  # deep copy

    # The ONLY item whose evidence this slice can legitimately touch. It is
    # owned by observation, and an observation happened: bars arrived and were
    # scored. The COUNT is unchanged, and that is the finding.
    checklist["forward_shadow_clean"].update({
        "complete": forward_trades >= pg.MIN_FORWARD_TRADES,
        "forward_trades_to_date": forward_trades,
        "forward_days_to_date": 0,
        "evidence": (
            f"{forward_trades} of {pg.MIN_FORWARD_TRADES} forward closed "
            f"trades; 0 of {pg.MIN_FORWARD_DAYS} days. For the FIRST time in "
            f"the programme a real extension arrived and was measured: "
            f"{closed_bars} closed linear daily bar(s) strictly after t1, "
            f"proven by prefix hash rather than by a note — measured history "
            f"is byte-identical to its slice-57 pins and every corpus file is "
            f"append-only. The count is nevertheless unchanged, because the "
            f"gate counts CLOSED FORWARD TRADES and a bar is not a trade. With "
            f"a 5-bar horizon and next-open entry, 6 closed forward bars must "
            f"exist before the first forward trade can close; 1 does. The rule "
            f"also stood aside on that bar: no funding setup, and it is the "
            f"last bar of the corpus so no entry bar exists. See "
            f"{FRESHNESS} and {FORWARD}."),
        "extension_present_this_slice": bool(freshness["extension_present"]),
        "closed_forward_bars": closed_bars,
        "why_bars_are_not_trades": (
            "A trade is an observation only when it CLOSES. Counting bars, "
            "flags or entries would be the slice-59 substitution — history or "
            "activity relabelled as experience — in the smallest form still "
            "available to this programme."),
    })

    payload = {
        "schema": SCHEMA,
        "slice": 62,
        "supersedes": PREVIOUS,
        "signal": previous["signal"],
        "symbol": previous["symbol"],
        "doc": previous["doc"],
        "design_note": "EDGE.md §45e (the forward rule and its ceiling)",
        "default": "REFUSE",
        "max_notional_usd": shadow.SHADOW_MAX_NOTIONAL_USD,
        "min_forward_trades": pg.MIN_FORWARD_TRADES,
        "min_forward_days": pg.MIN_FORWARD_DAYS,
        "minimums_moved_this_slice": False,

        "checklist": checklist,

        "templates_present": previous["templates_present"],
        "templates": previous["templates"],
        "templates_are_not_a_checklist_item":
            previous["templates_are_not_a_checklist_item"],
        "signatures_present": False,
        "signatures_forged": False,
        "human_items_completed_in_code": 0,

        "items_total": len(verdict.items),
        "items_complete": sum(1 for item in verdict.items if item.complete),
        "items_complete_unchanged_from_slice_61":
            sum(1 for item in verdict.items if item.complete)
            == previous["items_complete"],
        "items_requiring_a_human": previous["items_requiring_a_human"],
        "note_on_ownership": previous["note_on_ownership"],
        "note_on_fail_closed": previous["note_on_fail_closed"],

        "evaluated": {
            "gate_path_read_by_the_gate": os.path.relpath(pg.GATE_PATH, REPO),
            "gate_path_repointed_this_slice": False,
            "why_not_repointed": (
                "The canonical declaration stays on the slice-59 file. "
                "Re-aiming the live gate at a fresher artefact every slice "
                "would make the gate's own wiring a moving part, and a gate "
                "whose input can be swapped is a gate that can be swapped for "
                "a satisfied one."),
            "allows_live": bool(verdict.allows_live),
            "items": [
                {"key": item.key, "owner": item.owner,
                 "complete": bool(item.complete), "evidence": item.evidence}
                for item in verdict.items],
        },
        "promotion_gate_allows_live": bool(pg.promotion_gate_allows_live()),

        "forward_evidence_this_slice": {
            "extension_present": bool(freshness["extension_present"]),
            "closed_forward_bars": closed_bars,
            "forward_n_trades": forward_trades,
            "is_forward_observation": bool(forward["is_forward_observation"]),
            "max_possible_forward_trades_today":
                forward["ceiling"]["max_possible_forward_closed_trades"],
            "is_stage1_evidence": False,
            "registration_eligible": False,
            "shadow_mean_treated_as_stage1_evidence": False,
        },
        "closer_to_autonomous_profit_agent": False,
        "why_not_closer": (
            "One post-t1 day is pilot progress, not autonomy. Six of the eight "
            "items are human acts and none of them happened; the observation "
            "item moved from 'no data existed' to 'data existed and produced "
            "no closed trade', which is a better-evidenced zero, not a "
            "smaller gap."),

        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, cwd=REPO).strip(),
    }

    if payload["promotion_gate_allows_live"] or payload["evaluated"][
            "allows_live"]:
        raise SystemExit(
            "REFUSING TO WRITE: the gate did not refuse. This tool records a "
            "refusal; it is not a mechanism for recording a promotion, and a "
            "promotion reached on this tree would be a defect to investigate "
            "before it is a result to file.")

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    print("=" * 78)
    print("SLICE 62 — PROMOTION GATE   (re-evaluated against REAL forward data)")
    print("=" * 78)
    print(f"  default                      : REFUSE")
    print(f"  promotion_gate_allows_live() : "
          f"{payload['promotion_gate_allows_live']}")
    print(f"  items complete               : {payload['items_complete']} / "
          f"{payload['items_total']}   (unchanged from slice 61: "
          f"{payload['items_complete_unchanged_from_slice_61']})")
    print(f"  minimums moved               : "
          f"{payload['minimums_moved_this_slice']}  "
          f"({pg.MIN_FORWARD_TRADES} trades / {pg.MIN_FORWARD_DAYS} days)")
    print(f"  human items completed in code: "
          f"{payload['human_items_completed_in_code']}")
    print(f"  signatures forged            : {payload['signatures_forged']}")
    print()
    for item in verdict.items:
        mark = "COMPLETE" if item.complete else "incomplete"
        print(f"    {mark:10s} {item.owner:20s} {item.key}")
    print()
    print("  THE ITEM THAT COULD HAVE MOVED, AND DID NOT:")
    print(f"    extension present          : "
          f"{payload['forward_evidence_this_slice']['extension_present']}")
    print(f"    closed forward bars        : {closed_bars}")
    print(f"    forward closed TRADES      : {forward_trades} / "
          f"{pg.MIN_FORWARD_TRADES}")
    print(f"    is_forward_observation     : "
          f"{payload['forward_evidence_this_slice']['is_forward_observation']}")
    print()
    print("    A bar is not a trade. The gate counts closed forward trades.")
    print()
    print(f"artefact: {os.path.relpath(args.out, REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
