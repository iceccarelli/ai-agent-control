#!/usr/bin/env python3
"""State, from the artefacts themselves, what the forward-evidence trail does and does not show.

    python3 tools/forward_lineage.py [--now EPOCH_SECONDS]

READ-ONLY and offline. It reads ``artifacts/forward_shadow_current.json`` and asks
``forward_status`` (the same code the promotion gate uses) for its verdict, then lists
the facts a reader needs to avoid mistaking the count for evidence:

* the trades are PAPER observations produced by a SIMULATED shadow, never cash;
* they belong to the DIRECTIONAL funding-fade shadow, not to the delta-neutral carry
  book (``promotion_gate`` says as much: no evidence source for the carry book is wired);
* how old the file is, which commit produced it (and whether that tree was dirty);
* every number is in one place, so "4 trades" in one file and "0 forward trades" in
  another can be traced to a difference of definition or of date rather than argued.

It never decides to promote anything. It cannot: it has no write path.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, Optional

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)

ARTIFACT = os.path.join(BOT, "artifacts", "forward_shadow_current.json")


def lineage(artifact: str = ARTIFACT, *, now: Optional[float] = None) -> Dict[str, Any]:
    import forward_status as fs
    now = time.time() if now is None else float(now)
    with open(artifact, encoding="utf-8") as handle:
        raw = json.load(handle)
    verdict = fs.evaluate_forward_evidence(artifact, now=now)
    trades = raw.get("trades") or []
    commit = str(raw.get("git_commit", ""))
    return {
        "artifact": os.path.relpath(artifact, BOT),
        "evidence_class": "SIMULATED/PAPER",
        "is_cash": False,
        "belongs_to": {"signal": raw.get("signal"), "symbol": raw.get("symbol"),
                       "book": "DIRECTIONAL shadow; not the delta-neutral carry book"},
        "counts": {"forward_n_trades_field": raw.get("forward_n_trades"),
                   "trades_listed": len(trades),
                   "forward_observations_to_date": raw.get("forward_observations_to_date"),
                   "ceiling_max_possible_forward_closed_trades":
                       (raw.get("ceiling") or {}).get("max_possible_forward_closed_trades"),
                   "gate_trades_vs_minimum": verdict.trades_vs_gate,
                   "gate_days_vs_minimum": verdict.days_vs_gate},
        "consistent": raw.get("forward_n_trades") == len(trades),
        "observed_at_utc": raw.get("observed_at_utc"),
        "age_hours": None if verdict.age_s is None else round(verdict.age_s / 3600.0, 1),
        "gate_state": verdict.state,
        "produced_by_commit": commit,
        "produced_from_dirty_tree": commit.endswith("-dirty"),
        "is_stage1_evidence": raw.get("is_stage1_evidence"),
        "registration_eligible": raw.get("registration_eligible"),
        "reading": ("A paper count from a stale, dirty-tree shadow of a different book. It is "
                    "not evidence for the carry book, not cash, and not an out-of-sample "
                    "result for any rule."),
    }


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--now", type=float, default=None)
    args = ap.parse_args(argv)
    out = lineage(now=args.now)
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0 if out["consistent"] else 1


if __name__ == "__main__":
    sys.exit(main())
