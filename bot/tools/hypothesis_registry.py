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

TAMPER-EVIDENCE. Every trial carries a SHA-256 over its registration fields
(``entry_digest``); `seal()` stores them in ``integrity`` and `verify()` fails a
registry from which a trial was REMOVED or whose registration fields were ALTERED
(supersession, amendments and evidence are appended later and are outside the
digest). The default registry file may not be missing: `load()` raises instead of
returning an empty registry, because an empty registry silently removes the
multiple-testing penalty. A test pins the digest of the trials that existed when
this was introduced (``LEGACY_SEAL``), so the file in git cannot be quietly edited.
What this does NOT do: stop someone with write access from rewriting the file AND
the test; the custody is git history and review, not this code.

NEW EXPERIMENTS use `register_experiment()` (every field below is required),
`add_evidence()` (one entry per EVIDENCE LEVEL, never a single 'passed' flag),
`amend()` and `abandon()` (a failed or abandoned variant is recorded, not deleted).
`summary()` reports the integrity problems and never lets the working search width
fall below the number of recorded trials.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from typing import Any, Dict, Iterable, List, Optional

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


class RegistryError(ValueError):
    """The registry is missing, unreadable or fails its integrity check."""


#: The seven kinds of evidence. Each is its own claim; none implies another.
EVIDENCE_LEVELS = ("RESEARCH_CANDIDATE", "BACKTEST", "OUT_OF_SAMPLE", "PROSPECTIVE",
                   "SIMULATED_EXECUTION", "TESTNET_MECHANICS", "OWNER_ACCOUNT_VERIFIED")
#: Fields a new experiment must state BEFORE it is run.
REQUIRED_REGISTRATION = ("hypothesis_id", "mechanism", "family", "strategy", "universe", "input_data_sha256", "code_sha", "data_window",
                         "parameter_grid", "costs", "statistical_design", "train_test", "multiple_testing_family", "prospective_period")
#: Appended after registration; outside the registration digest.
_MUTABLE = ("superseded_by", "superseded_reason", "superseded_utc", "evidence", "amendments", "abandoned")
#: The trials that existed when the digest was introduced; the digest of their (id, entry_digest) pairs is pinned in the test suite.
LEGACY_IDS_KEY = "legacy_ids"


def entry_digest(entry: Dict[str, Any]) -> str:
    """SHA-256 over an entry's registration fields (everything except what is legitimately appended later)."""
    frozen = {k: v for k, v in entry.items() if k not in _MUTABLE}
    return hashlib.sha256(json.dumps(frozen, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _item_hash(item: Any) -> str:
    return hashlib.sha256(json.dumps(item, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _appended_state(t: Dict[str, Any]) -> Dict[str, Any]:
    """What may only GROW: evidence and amendments (append-only lists), abandonment and supersession (once set, never unset or changed)."""
    ab = t.get("abandoned")
    return {"evidence": [_item_hash(x) for x in t.get("evidence", [])], "amendments": [_item_hash(x) for x in t.get("amendments", [])],
            "abandoned": None if ab is None else _item_hash(ab), "superseded_by": t.get("superseded_by")}


def seal(registry: Dict[str, Any]) -> None:
    """Record every trial's digest AND the state of its append-only fields. Called by `save()`, which first refuses if the previous seal no longer holds."""
    registry["integrity"] = {"scheme": "sha256-per-entry/2",
                             "entries": {t["trial_id"]: entry_digest(t) for t in registry.get("trials", [])},
                             "appended": {t["trial_id"]: _appended_state(t) for t in registry.get("trials", [])}}


def verify(registry: Dict[str, Any], *, require_ids: Iterable[str] = ()) -> List[str]:
    """Problems found, empty if none. Removal and alteration are named; so is a missing seal, an append-only field that shrank or changed (evidence edited
    or wiped, abandonment or supersession removed) and any required id that is absent."""
    problems: List[str] = []
    trials = {t.get("trial_id"): t for t in registry.get("trials", [])}
    block = registry.get("integrity")
    if not isinstance(block, dict) or not isinstance(block.get("entries"), dict):
        problems.append("registry is not sealed: no per-trial digests, so a removed or edited trial cannot be detected")
        sealed: Dict[str, str] = {}
        appended: Dict[str, Any] = {}
    else:
        sealed = block["entries"]
        appended = block.get("appended") if isinstance(block.get("appended"), dict) else {}      # absent in a v1 seal: adopted by the next save()
    for tid, dig in sealed.items():
        if tid not in trials:
            problems.append(f"trial {tid!r} was REMOVED after it was sealed")
        elif entry_digest(trials[tid]) != dig:
            problems.append(f"trial {tid!r} was ALTERED after it was sealed")
    for tid, was in appended.items():
        t = trials.get(tid)
        if t is None:
            continue
        try:
            now = _appended_state(t)
        except (ValueError, TypeError):
            problems.append(f"trial {tid!r} has an append-only field that cannot be read (NaN or non-JSON value)")
            continue
        for key in ("evidence", "amendments"):
            if now[key][:len(was.get(key, []))] != was.get(key, []):
                problems.append(f"trial {tid!r}: {key} was ALTERED or removed after it was sealed")
        if was.get("abandoned") is not None and now["abandoned"] != was["abandoned"]:
            problems.append(f"trial {tid!r}: abandonment was removed or changed after it was sealed")
        if was.get("superseded_by") is not None and now["superseded_by"] != was["superseded_by"]:
            problems.append(f"trial {tid!r}: supersession was removed or changed after it was sealed")
    for tid in trials:
        if sealed and tid not in sealed:
            problems.append(f"trial {tid!r} is not sealed (added without save())")
    for tid in require_ids:
        if tid not in trials:
            problems.append(f"required trial {tid!r} is absent")
    return problems


def legacy_seal(registry: Dict[str, Any], ids: Iterable[str]) -> str:
    """One digest over the named trials: what the test suite pins so the committed file cannot be edited quietly."""
    trials = {t["trial_id"]: t for t in registry.get("trials", [])}
    pairs = sorted((i, entry_digest(trials[i])) for i in ids if i in trials)
    return hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode("utf-8")).hexdigest()


def load(path: Optional[str] = None, *, missing_ok: Optional[bool] = None) -> Dict[str, Any]:
    """The registry. A missing DEFAULT file raises (an empty registry would silently drop the multiple-testing penalty);
    an explicit other path may be missing (a scratch registry in a test or a new project)."""
    path = DEFAULT_PATH if path is None else path          # resolved at CALL time so a redirected DEFAULT_PATH is honoured
    if missing_ok is None:
        missing_ok = os.path.abspath(path) != os.path.abspath(DEFAULT_PATH)
    if not os.path.isfile(path):
        if not missing_ok:
            raise RegistryError(f"{path} is missing: refusing to treat a deleted registry as an empty one")
        return {"schema": "hypothesis_registry/1", "trials": [],
                "seeded": False}
    with open(path, encoding="utf-8") as fh:
        try:
            doc = json.load(fh)
        except ValueError as exc:
            raise RegistryError(f"{os.path.basename(path)} is not valid JSON") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("trials", []), list):
        raise RegistryError(f"{os.path.basename(path)} has no trial list")
    return doc


def save(registry: Dict[str, Any], path: str = DEFAULT_PATH, *, accept_tampering: bool = False) -> None:
    """Write the registry. REFUSES if the previous seal no longer holds (a removed/altered trial or shrunk evidence): re-sealing then would launder the
    tampering into the new baseline (reproduced: any save() made verify() clean again). ``accept_tampering`` is the owner's explicit re-baseline."""
    if isinstance(registry.get("integrity"), dict) and not accept_tampering:
        broken = [p for p in verify(registry) if "not sealed" not in p]
        if broken:
            raise RegistryError(f"refusing to save over tampering (it would become the new baseline): {broken[:3]}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    seal(registry)
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


def supersede(registry: Dict[str, Any], old_id: str, by: str, reason: str
              ) -> Dict[str, Any]:
    """Mark a recorded trial as superseded by a LATER trial, touching nothing else.

    The registry is append-only, so when the production rule changes the old
    entry cannot be edited: its note stays exactly as it was written, with its
    measurements. What may be added is a pointer from it to the trial that now
    describes the rule the system runs, so a reader of the old note is not left
    believing it is current. Adds `superseded_by`, `superseded_reason` and
    `superseded_utc`; refuses an unknown id, a missing successor, a self-pointer
    or an entry that was already superseded.
    """
    by_id = {t["trial_id"]: t for t in registry.get("trials", [])}
    if old_id not in by_id:
        raise ValueError(f"unknown trial_id {old_id!r}")
    if by not in by_id:
        raise ValueError(f"successor {by!r} is not a recorded trial; record it "
                         "first")
    if old_id == by:
        raise ValueError("a trial cannot supersede itself")
    entry = by_id[old_id]
    if "superseded_by" in entry:
        raise ValueError(f"{old_id!r} is already superseded by "
                         f"{entry['superseded_by']!r}")
    if not str(reason).strip():
        raise ValueError("a supersession needs a reason")
    entry["superseded_by"] = by
    entry["superseded_reason"] = reason
    entry["superseded_utc"] = dt.datetime.now(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
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


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def register_experiment(registry: Dict[str, Any], trial_id: str, **fields: Any) -> Dict[str, Any]:
    """Register a new experiment BEFORE it runs. Every field in REQUIRED_REGISTRATION must be present and non-empty
    (``prospective_period`` may be the explicit string ``"none"``). The registration is then fixed: the digest covers it."""
    missing = [k for k in REQUIRED_REGISTRATION if k not in fields or fields[k] in (None, "", [], {})]
    if missing:
        raise ValueError(f"experiment {trial_id!r} is not registered: missing {missing}")
    extra = sorted(set(fields) - set(REQUIRED_REGISTRATION) - {"note", "source"})
    if extra:
        raise ValueError(f"unknown registration field(s) {extra}; put commentary in 'note'")
    return record(registry, trial_id, status="registered", registered_utc=_now(), evidence=[], amendments=[], **fields)


def _entry(registry: Dict[str, Any], trial_id: str) -> Dict[str, Any]:
    for t in registry.get("trials", []):
        if t.get("trial_id") == trial_id:
            return t
    raise ValueError(f"unknown trial_id {trial_id!r}")


def add_evidence(registry: Dict[str, Any], trial_id: str, level: str, *, result: str, provenance: str, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Append ONE piece of evidence at ONE level. There is no 'passed' flag: read `evidence_status`."""
    if level not in EVIDENCE_LEVELS:
        raise ValueError(f"evidence level {level!r} is not one of {EVIDENCE_LEVELS}")
    if not str(result).strip() or not str(provenance).strip():
        raise ValueError("evidence needs a result and its provenance (the file/run/commit it came from)")
    entry = _entry(registry, trial_id)
    if entry.get("abandoned"):
        raise ValueError(f"{trial_id!r} is abandoned; evidence cannot be added to it")
    if entry.get("superseded_by"):
        raise ValueError(f"{trial_id!r} is superseded by {entry['superseded_by']!r}; add the evidence to the trial that replaced it")
    item = {"level": level, "result": str(result), "provenance": str(provenance), "details": dict(details or {}), "recorded_utc": _now()}
    entry.setdefault("evidence", []).append(item)
    return item


def evidence_status(entry: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Latest result at each of the seven levels (None where there is no evidence). Deliberately NOT collapsed into one verdict."""
    out: Dict[str, Optional[str]] = {lv: None for lv in EVIDENCE_LEVELS}
    for ev in entry.get("evidence", []):
        out[ev["level"]] = ev["result"]
    return out


def amend(registry: Dict[str, Any], trial_id: str, what: str, why: str) -> Dict[str, Any]:
    """Record a post-registration change as an AMENDMENT with its time. The registered fields themselves never change."""
    if not str(what).strip() or not str(why).strip():
        raise ValueError("an amendment states what changed and why")
    item = {"what": what, "why": why, "recorded_utc": _now()}
    _entry(registry, trial_id).setdefault("amendments", []).append(item)
    return item


def abandon(registry: Dict[str, Any], trial_id: str, reason: str) -> Dict[str, Any]:
    """Mark a variant abandoned. It stays in the registry and in the search width: an abandoned trial is still a trial."""
    if not str(reason).strip():
        raise ValueError("abandoning a trial needs a reason")
    entry = _entry(registry, trial_id)
    if entry.get("abandoned"):
        raise ValueError(f"{trial_id!r} is already abandoned")
    entry["abandoned"] = {"reason": reason, "recorded_utc": _now()}
    return entry


def register_btc_prospective(registry: Dict[str, Any]) -> Dict[str, Any]:
    """Link the FROZEN BTC prospective plan into the registry (read-only: nothing in the plan changes). Idempotent by trial id."""
    import carry_prospective as CP
    tid = "btcusdt_funding_carry_overlay@prospective-1"
    for t in registry.get("trials", []):
        if t.get("trial_id") == tid:
            return t
    digests = {f"{h:g}": CP.PINNED_DIGESTS[h] for h in CP.HURDLES_PCT}
    return register_experiment(
        registry, tid, hypothesis_id="btc_funding_carry_net_of_costs_exceeds_hurdle",
        mechanism="a delta-neutral short BTCUSDT linear perpetual against owned BTC receives positive funding; the registered question is whether mean "
                  "excess funding per 8h settlement exceeds the hurdle-implied requirement after the plan's costs",
        family="funding_carry", strategy="btcusdt_funding_carry_overlay", universe=["BTCUSDT"],
        input_data_sha256={"registered_plan_digests_by_hurdle_pct": digests},
        code_sha="tools/carry_prospective.py (pinned by PINNED_DIGESTS; see tools/frozen_proof.py)",
        data_window={"start_ms": CP.WINDOW_START_MS, "end_ms": CP.WINDOW_END_MS, "days": CP.WINDOW_DAYS},
        parameter_grid={"hurdles_pct": list(CP.HURDLES_PCT)},
        costs="as in the registered plan parameters (assumed, not measured); see carry_prospective.PARAMETERS",
        statistical_design="one-sided t on mean excess funding, Holm over 3 hurdles, alpha 0.05, power 0.8, ESS-adjusted; coverage floor "
                           f"{CP.MIN_COVERAGE}; evaluated only through tools/trial_journal.py",
        train_test="none: prospective only; no data before the window is evaluated",
        multiple_testing_family="the three registered hurdles (Holm)",
        prospective_period=f"{CP.WINDOW_START_MS}..{CP.WINDOW_END_MS}",
        source="tools/carry_prospective.py",
        note="FROZEN. This entry only links the plan into the registry; it changes nothing in it.")


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
        out.update(_search_width(registry, trials, n, alpha))
    out["integrity_problems"] = verify(registry)
    return out


def _search_width(registry: Dict[str, Any], trials: List[Dict[str, Any]],
                  n: int, alpha: float) -> Dict[str, Any]:
    """The WORKING width, when the registry states one.

    `n_trials_recorded` counts a rule sweep as ONE trial, which understates the
    search; the raw sum of the sweeps' printed counts double-counts (duplicate
    cells, and one rule space scored in many refit windows). The registry's
    `search_width` block states the number of DISTINCT rules the sweeps scored;
    the working width swaps the sweeps' entries for that number and adds nothing
    for a sweep that is not recorded. Absent the block, nothing changes.
    """
    block = registry.get("search_width")
    if not isinstance(block, dict):
        return {}
    sweeps = set(block.get("sweep_trial_ids") or [])
    present = sum(1 for t in trials if t.get("trial_id") in sweeps)
    rules = block.get("distinct_gated_rules_scored", 0)
    if isinstance(rules, bool) or not isinstance(rules, int) or rules < 0:
        raise RegistryError("search_width.distinct_gated_rules_scored must be a non-negative integer")
    # The working width can never be narrower than the recorded count: a shrunk or negative rule count would otherwise WEAKEN the correction.
    n_working = max(n, (n - present) + rules)
    fwer = ds.family_wise_false_positive_rate(n_working, alpha)
    return {"search_width": {
        "n_recorded": n, "n_working": n_working,
        "distinct_gated_rules_scored": rules,
        "family_wise_false_positive_rate_working": fwer,
        "expected_max_sharpe_under_null_working":
            ds.expected_max_sharpe_under_null(n_working),
        "reading": (
            f"{n} recorded trials count the rule sweeps as {present}. They scored "
            f"{rules} distinct rules in-sample, so the working width is "
            f"{n_working}: under a global null the chance that at least one "
            f"clears a {1 - alpha:.0%} bar is {fwer:.1%}. A single in-sample "
            "clear at this width is not evidence of edge. The effective "
            "independent count is lower (the rules are correlated) and is not "
            "estimated here, so read the correction at the harsh end.")}}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=DEFAULT_PATH)
    ap.add_argument("--seed", action="store_true",
                    help="add the twelve known families (idempotent)")
    ap.add_argument("--add", default="", help="record a new trial id")
    ap.add_argument("--status", default="tested")
    ap.add_argument("--note", default="")
    ap.add_argument("--register-prospective", action="store_true", help="link the frozen BTC prospective plan into the registry (idempotent)")
    args = ap.parse_args(argv)

    registry = load(args.path, missing_ok=bool(args.seed))
    if args.register_prospective:
        register_btc_prospective(registry)
        save(registry, args.path)
        print("prospective plan linked")
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
