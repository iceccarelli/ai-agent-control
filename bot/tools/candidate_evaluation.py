#!/usr/bin/env python3
"""One common evaluation interface for every candidate family, with ONE closed status vocabulary.

Round 22 (the owner's directive: use the eleven candidates as an opportunity set; do not manufacture results). ``candidate_competition.run_competition`` already produces one row per candidate on the same
data, the same costs and the same multiple-testing ledger; this module is a PURE VIEW over that report (and the candidate specifications) that states, for each candidate, the fields the directive names

    candidate_id, hypothesis, data requirements, execution requirements, cost model, capital model, sample size, gross edge, net edge, confidence, drawdown, failure modes, status

and maps the competition's own verdicts and readiness ladder onto the closed status set

    UNTESTED | DATA_INVALID | INSUFFICIENT | NEGATIVE | PROMISING | QUALIFIED

It computes no edge, reads no data, no ledger and no clock, and sends nothing: a figure it does not find in the report is the string ``UNKNOWN`` with the reason, never a zero. QUALIFIED needs the ladder's EXECUTION_READY
(a started prospective window with its floor of settlements, executable on a qualified venue) AND a positive, significant net edge: a positive backtest is never QUALIFIED (it is at most PROMISING, and only when the ladder
says so). Today no candidate is QUALIFIED; the honest summary is NO WINNER.

Usage:  python3 tools/candidate_evaluation.py COMPETITION.json [--specs DIR] [--json]      (COMPETITION.json: the output of ``candidate_competition.py --json``)"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path[:0] = [p for p in (HERE, BOT) if p not in sys.path]
import economic_model as EM  # noqa: E402

SCHEMA = "candidate_evaluation/v1"
UNTESTED, DATA_INVALID, INSUFFICIENT, NEGATIVE, PROMISING, QUALIFIED = "UNTESTED", "DATA_INVALID", "INSUFFICIENT", "NEGATIVE", "PROMISING", "QUALIFIED"
STATUSES = (UNTESTED, DATA_INVALID, INSUFFICIENT, NEGATIVE, PROMISING, QUALIFIED)
#: the eleven registered candidate families (research/candidates/*.json), the opportunity set
FAMILIES = ("cash_and_carry", "cross_interval_funding_dispersion", "cross_sectional_funding", "cross_venue_funding", "funding_regime_conditioned_entry", "hedgeable_majors_basis_convergence", "liquidity_imbalance",
            "market_neutral_relative_value", "passive_execution", "spot_perp_basis", "spread_microstructure")
UNKNOWN = "UNKNOWN"
#: the capital structure each family's execution model declares (economic_model): used ONLY to quote the capital multiple the ONE definition gives; any other family is UNKNOWN, not guessed
CAPITAL_STRUCTURE = {"cash_and_carry": EM.ACQUIRED_SPOT_PERP, "spot_perp_basis": EM.ACQUIRED_SPOT_PERP, "hedgeable_majors_basis_convergence": EM.ACQUIRED_SPOT_PERP,
                     "cross_sectional_funding": EM.PERP_ONLY, "cross_interval_funding_dispersion": EM.PERP_ONLY, "market_neutral_relative_value": EM.PERP_ONLY}
_LADDER_PROMISING = ("PROMISING", "PROSPECTIVE")
_LADDER_QUALIFIED = ("EXECUTION_READY", "OWNER_PROFIT_EVIDENCED")
#: the dataset states that say the data are valid AND sufficient (candidate_readiness: SUFFICIENT_DATA_STATES); anything else, or no state at all, can never be QUALIFIED
SUFFICIENT_DATASET_STATES = ("DATA_SUFFICIENT_NOT_EVALUATED", "DATA_SUFFICIENT_CANDIDATE_REJECTED", "DATA_SUFFICIENT_ELIGIBLE_FOR_FURTHER_REVIEW")
#: independent observations below which no claim is carried (the repository's holdout floor)
MIN_INDEPENDENT_OBSERVATIONS = 50
#: worst first: a duplicate row for one family is judged by its WORST status (fail closed)
_BADNESS = (NEGATIVE, DATA_INVALID, INSUFFICIENT, UNTESTED, PROMISING, QUALIFIED)


def load_specs(specs_dir: str) -> Dict[str, Dict[str, Any]]:
    """``research/candidates/<family>.json`` by family name; unreadable files are skipped (the view then says UNKNOWN for the fields they would have given)."""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        names = sorted(os.listdir(specs_dir))
    except OSError:
        return out
    for n in names:
        if n.endswith(".json"):
            try:
                with open(os.path.join(specs_dir, n), encoding="utf-8") as fh:
                    d = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(d, dict):
                out[n[:-5]] = d
    return out


def _num(x: Any) -> Optional[float]:
    if isinstance(x, bool) or x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def status_of(row: Mapping[str, Any]) -> Tuple[str, List[str]]:
    """(status, reasons) for one competition row. Order matters and is conservative: an invalid dataset beats everything; a rejection beats a ladder level; QUALIFIED needs the top of the ladder."""
    ds = row.get("dataset_state") if isinstance(row.get("dataset_state"), Mapping) else {}
    ds_state = str(ds.get("state") or "")
    if ds_state in ("DATA_UNAVAILABLE", "DATA_INVALID", "DATA_INCOMPLETE"):
        return DATA_INVALID, [f"DATASET_{ds_state}"] + [str(r) for r in (ds.get("reasons") or [])[:3]]
    verdict = row.get("verdict")
    level = row.get("readiness_level")
    net = _num((row.get("net_edge") or {}).get("value_bps")) if isinstance(row.get("net_edge"), Mapping) else None
    sig = (row.get("net_edge") or {}).get("significant") if isinstance(row.get("net_edge"), Mapping) else None
    if verdict == "REJECTED":
        return NEGATIVE, ["COMPETITION_VERDICT_REJECTED"] + [str(r) for r in (row.get("rejection_reasons") or [])[:2]]
    if level in _LADDER_QUALIFIED and net is not None and net > 0 and sig is True:
        missing = _qualification_gaps(row, ds_state)
        if not missing:
            return QUALIFIED, [f"LADDER_{level}", "NET_EDGE_POSITIVE_AND_SIGNIFICANT"]
        return PROMISING, [f"LADDER_{level}", "NOT_QUALIFIED: " + "; ".join(missing)]
    if level in _LADDER_PROMISING or level in _LADDER_QUALIFIED:
        return PROMISING, [f"LADDER_{level}"] + ([] if level in _LADDER_PROMISING else ["NOT_QUALIFIED: positive significant net edge not shown"])
    if verdict == "INSUFFICIENT_DATA":
        return INSUFFICIENT, ["COMPETITION_VERDICT_INSUFFICIENT_DATA"] + [str(r) for r in (row.get("rejection_reasons") or [])[:2]]
    return UNTESTED, [str(c) for c in [(row.get("evaluator") or {}).get("code")] if c] or ["NO_TEST_RUN"]


def _qualification_gaps(row: Mapping[str, Any], ds_state: str) -> List[str]:
    """What a QUALIFIED claim still needs that the row does not state. A missing fact is a gap, never a pass (Round 22 review: a row with only a ladder level, a positive edge and significant=True was QUALIFIED)."""
    gaps: List[str] = []
    if ds_state not in SUFFICIENT_DATASET_STATES:
        gaps.append(f"dataset_state {ds_state or 'NOT_STATED'} is not valid-and-sufficient")
    mt = row.get("multiple_testing")
    if not isinstance(mt, Mapping) or _num(mt.get("n_variants_counted")) is None:
        gaps.append("no multiple-testing count (the number of variants tried is not stated)")
    n = _sample(row).get("independent_observations")
    if _num(n) is None or float(n) < MIN_INDEPENDENT_OBSERVATIONS:
        gaps.append(f"independent observations {n} below the floor of {MIN_INDEPENDENT_OBSERVATIONS}")
    return gaps


def _capital_model(family: str) -> Dict[str, Any]:
    st = CAPITAL_STRUCTURE.get(family)
    if st is None:
        return {"structure": UNKNOWN, "reason": "the family's execution model declares no capital structure the economic model can price"}
    one = EM.structure_capital_usd(st, 1, 1).value
    return {"structure": st, "capital_per_usd_notional_at_leverage_1": str(one), "provenance": "DERIVED from economic_model.structure_capital_usd (the ONE capital definition)"}


def _sample(row: Mapping[str, Any]) -> Dict[str, Any]:
    fams = row.get("families") if isinstance(row.get("families"), Mapping) else None
    mt = row.get("multiple_testing") if isinstance(row.get("multiple_testing"), Mapping) else {}
    out: Dict[str, Any] = {"trials_counted": mt.get("n_variants_counted"), "persisted_trials": mt.get("persisted_variants")}
    n = None
    if fams:
        vals = [f.get("n_rows") if isinstance(f, Mapping) else None for f in fams.values()]
        vals = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
        n = max(vals) if vals else None
    out["independent_observations"] = n if n is not None else UNKNOWN
    if n is None:
        out["reason"] = "the competition row carries no independent-observation count for this family"
    return out


def candidate_view(row: Mapping[str, Any], spec: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    name = str(row.get("name"))
    status, reasons = status_of(row)
    spec = spec if isinstance(spec, Mapping) else {}
    ne = row.get("net_edge") if isinstance(row.get("net_edge"), Mapping) else {}
    net = ne.get("value_bps")
    return {
        "candidate_id": row.get("candidate") or name, "family": name, "status": status, "status_reasons": reasons[:5],
        "hypothesis": spec.get("hypothesis") or UNKNOWN,
        "data_requirements": row.get("data_readiness") if isinstance(row.get("data_readiness"), Mapping) else UNKNOWN,
        "execution_requirements": {"execution_model": spec.get("execution_model") or UNKNOWN, "executable_on_qualified_venue": (row.get("executable_on_qualified_venue") or {}).get("executable") if isinstance(row.get("executable_on_qualified_venue"), Mapping) else UNKNOWN,
                                   "evaluator_wired": (row.get("evaluator") or {}).get("wired") if isinstance(row.get("evaluator"), Mapping) else UNKNOWN},
        "cost_model": {"fee_model": spec.get("fee_model") or UNKNOWN, "slippage_model": spec.get("slippage_model") or UNKNOWN, "funding_model": spec.get("funding_model") or UNKNOWN,
                       "note": "figures marked 'assumed' in the specification are assumptions, not measurements"},
        "capital_model": {**_capital_model(name), "capacity": spec.get("capacity_model") or UNKNOWN},
        "sample_size": _sample(row),
        "gross_edge": {"value_bps": UNKNOWN, "reason": "the competition row reports the NET edge and the basis it was charged on; a gross figure is not separately published, so none is invented"},
        "net_edge": {"value_bps": net if net is not None else UNKNOWN, "basis": ne.get("basis"), "break_even": ne.get("required_to_break_even"), "significant": ne.get("significant")},
        "confidence": {"significant": ne.get("significant"), "oos_possible": row.get("oos_possible"), "readiness_level": row.get("readiness_level"),
                       "multiple_testing_family": (row.get("multiple_testing") or {}).get("family") if isinstance(row.get("multiple_testing"), Mapping) else None},
        "drawdown": {"model": spec.get("drawdown_model") or UNKNOWN, "measured": UNKNOWN, "reason": "no candidate has a measured drawdown on out-of-sample or prospective data"},
        "failure_modes": {"risk_model": spec.get("risk_model") or UNKNOWN, "rejection_conditions": list(spec.get("rejection_conditions") or [])[:6], "findings": [str(x) for x in (row.get("rejection_reasons") or [])[:6]]},
        "claim": "an in-sample or Testnet figure is not owner profit and not Mainnet expectancy",
    }


def view(report: Mapping[str, Any], specs: Optional[Mapping[str, Mapping[str, Any]]] = None) -> Dict[str, Any]:
    """The common evaluation of every candidate in ``report`` (a ``candidate_competition/v1`` report). Families absent from the report are listed as UNTESTED with the reason, so the opportunity set is always complete."""
    specs = specs or {}
    by_name: Dict[str, List[Mapping[str, Any]]] = {}
    for r in (report.get("results") or []):
        if isinstance(r, Mapping):
            by_name.setdefault(str(r.get("name")), []).append(r)
    # one row per family: if the report carries several, the WORST status decides (a later positive row can never overwrite an earlier rejection)
    rows = {name: min(rs, key=lambda x: _BADNESS.index(status_of(x)[0])) for name, rs in by_name.items()}
    cands: List[Dict[str, Any]] = []
    for fam in FAMILIES:
        if fam in rows:
            cands.append(candidate_view(rows[fam], specs.get(fam)))
        else:
            cands.append({"candidate_id": fam, "family": fam, "status": UNTESTED, "status_reasons": ["NOT_IN_THE_COMPETITION_REPORT"], "hypothesis": (specs.get(fam) or {}).get("hypothesis") or UNKNOWN,
                          "net_edge": {"value_bps": UNKNOWN}, "claim": "an in-sample or Testnet figure is not owner profit and not Mainnet expectancy"})
    extra = sorted(set(rows) - set(FAMILIES))
    counts = {s: sum(1 for c in cands if c["status"] == s) for s in STATUSES}
    qualified = [c["candidate_id"] for c in cands if c["status"] == QUALIFIED]
    return {"schema": SCHEMA, "statuses": list(STATUSES), "candidates": cands, "counts": counts, "qualified": qualified, "not_in_the_set": extra,
            "winner": qualified[0] if qualified else None,
            "statement": ("QUALIFIED: " + ", ".join(qualified) + " (still needs the owner's attestation and a bounded Testnet then Mainnet decision; never automatic)" if qualified else
                          "NO WINNER: no candidate is QUALIFIED. " + ", ".join(f"{n} {s.lower()}" for s, n in counts.items() if n) + ". Nothing here is a trade recommendation."),
            "caveat": "a positive in-sample backtest is never QUALIFIED; QUALIFIED needs a started prospective window, an executable qualified venue and a positive significant net edge"}


def evidence_for(view_doc: Mapping[str, Any], candidate_id: Optional[str]) -> Dict[str, Any]:
    """The ``evidence`` input of ``eligibility_gate.evaluate`` for the candidate the owner's capability names (matched by id or family); UNTESTED when it is not found."""
    for c in view_doc.get("candidates", []):
        # an id with a spec revision (name@spec-N) must match EXACTLY: a different revision of the same family is a different specification. A bare family name matches the family.
        exact = bool(candidate_id) and c.get("candidate_id") == candidate_id
        bare = bool(candidate_id) and "@" not in str(candidate_id) and c.get("family") == candidate_id
        if exact or bare:
            return {"status": c["status"], "candidate_id": c["candidate_id"], "reasons": c.get("status_reasons", [])}
    return {"status": UNTESTED, "candidate_id": candidate_id, "reasons": ["CANDIDATE_NOT_FOUND_IN_THE_EVALUATION"]}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("competition", help="a candidate_competition/v1 JSON report")
    ap.add_argument("--specs", default=os.path.join(BOT, "research", "candidates"), help="the candidate specification directory")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        with open(args.competition, encoding="utf-8") as fh:
            rep = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"cannot read {args.competition}: {type(exc).__name__}", file=sys.stderr)
        return 2
    out = view(rep, load_specs(args.specs))
    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True, default=str))
    else:
        for c in out["candidates"]:
            print(f"{c['status']:12} {c['family']:40} net={c['net_edge']['value_bps']}  {'; '.join(c['status_reasons'][:2])[:110]}")
        print(out["statement"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
