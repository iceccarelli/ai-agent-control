#!/usr/bin/env python3
"""Append-only census of every hypothesis this programme has ever tested.

You cannot correct for searches you did not record. This file is the
counter that `tools/deflated_sharpe.py` needs, and it did not exist.

Seeded retroactively from the twelve families in signals/ and their frozen
status. THAT SEED IS A FLOOR, NOT A CENSUS: every parameter grid explored
and abandoned, every variant tried once, every re-run over a different
window is also a trial. The true N is larger than 12, so the true
correction is harsher than the one this file currently supports. Anyone who
remembers such a trial should add it - the registry only gets more honest.

Append-only by construction: `record()` refuses to overwrite an existing
trial_id, mirroring the corpus discipline used everywhere else in this tree.
Nothing here is read by any gate.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import deflated_sharpe as ds  # noqa: E402
import provenance as _provenance  # noqa: E402

DEFAULT_PATH = os.path.join(ROOT, "artifacts", "hypothesis_registry.json")

#: The twelve families present in signals/, with the status the tree records.
#: `cleared` means it passed its pre-declared Stage-1 bar; everything else is
#: frozen ABSENT. Each is one trial against the same price path.
def _seed_trials() -> List[Dict[str, Any]]:
    """Derive the trial list from project_status, not from a hand-typed list.

    IMPORTANT: `signals/` contains only 10 .py files, but
    `project_status.FROZEN_ABSENT` records ELEVEN frozen families - it
    includes `technical_analysis` and `closed_analyser`, which were measured
    and frozen without a surviving signals/ module. Seeding from the
    directory listing would therefore have UNDERCOUNTED the search width by
    two and produced a weaker correction than the evidence supports. The
    authoritative record is project_status, so that is what is read.

    11 frozen + 1 cleared = 12 recorded trials.
    """
    trials: List[Dict[str, Any]] = []
    try:
        sys.path.insert(0, ROOT)
        import project_status as ps
        frozen = list(ps.FROZEN_ABSENT)
    except Exception:  # noqa: BLE001
        frozen = []
    for name in frozen:
        trials.append({"trial_id": name, "status": "frozen_absent",
                       "source": "project_status.FROZEN_ABSENT"})
    trials.append({
        "trial_id": "funding_carry_fade_btc_v1", "status": "cleared_stage1",
        "source": "project_status/STAGE1_VERDICT",
        "note": "n=41, M1~95.13, M2=96.0. Forward: n=2, mean net R -0.8358. "
                "98.4% of setups are SHORT; 77.1% sit exactly at the funding "
                "threshold; carry over horizon 15bps vs 25bps round trip."})
    return trials


def load(path: str = DEFAULT_PATH) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {"schema": "hypothesis_registry/1", "trials": [],
                "seeded": False}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save(registry: Dict[str, Any], path: str = DEFAULT_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    registry["git_commit"] = _provenance.git_commit(ROOT)
    registry["updated_utc"] = dt.datetime.now(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(registry, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def record(registry: Dict[str, Any], trial_id: str, **fields: Any) -> Dict[str, Any]:
    """Append one trial. Refuses to overwrite an existing id."""
    existing = {t["trial_id"] for t in registry.get("trials", [])}
    if trial_id in existing:
        raise ValueError(
            f"trial_id {trial_id!r} already recorded; the registry is "
            f"append-only. A re-run under different parameters is a NEW "
            f"trial and needs its own id.")
    entry = {"trial_id": trial_id,
             "recorded_utc": dt.datetime.now(dt.timezone.utc).strftime(
                 "%Y-%m-%dT%H:%M:%SZ")}
    entry.update(fields)
    registry.setdefault("trials", []).append(entry)
    return entry


def seed(registry: Dict[str, Any]) -> int:
    added = 0
    for trial in _seed_trials():
        try:
            record(registry, trial["trial_id"],
                   **{k: v for k, v in trial.items() if k != "trial_id"})
            added += 1
        except ValueError:
            continue
    registry["seeded"] = True
    registry["seed_is_a_floor_not_a_census"] = (
        "Abandoned parameter grids, one-off variants and re-runs over "
        "different windows are trials too and are NOT counted here. The true "
        "search width is larger than this count, so the true multiple-testing "
        "correction is harsher than what this registry currently supports.")
    return added


def summary(registry: Dict[str, Any], alpha: float = 0.05) -> Dict[str, Any]:
    trials = registry.get("trials", [])
    n = len(trials)
    cleared = [t for t in trials if t.get("status") == "cleared_stage1"]
    out: Dict[str, Any] = {
        "n_trials_recorded": n,
        "n_cleared": len(cleared),
        "cleared_ids": [t["trial_id"] for t in cleared],
        "alpha": alpha,
    }
    if n >= 1:
        out["family_wise_false_positive_rate"] = \
            ds.family_wise_false_positive_rate(n, alpha)
        out["expected_max_sharpe_under_null"] = \
            ds.expected_max_sharpe_under_null(n)
        out["reading"] = (
            f"Under a global null in which NO family has edge, the chance that "
            f"at least one of {n} clears a {1 - alpha:.0%} bar is "
            f"{out['family_wise_false_positive_rate']:.1%}. "
            + ("A single clear at this width is not evidence of edge."
               if out["family_wise_false_positive_rate"] > alpha
               else "A single clear at this width is meaningful."))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=DEFAULT_PATH)
    ap.add_argument("--seed", action="store_true",
                    help="add the twelve known families (idempotent)")
    ap.add_argument("--add", default="", help="record a new trial id")
    ap.add_argument("--status", default="tested")
    ap.add_argument("--note", default="")
    args = ap.parse_args(argv)

    registry = load(args.path)
    if args.seed:
        print(f"seeded {seed(registry)} new trial(s)")
        save(registry, args.path)
    if args.add:
        record(registry, args.add, status=args.status, note=args.note)
        save(registry, args.path)
        print(f"recorded {args.add}")
    s = summary(registry)
    for k, v in s.items():
        print(f"{k:36s} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
