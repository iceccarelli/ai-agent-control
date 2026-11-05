"""Forecast funding versus the funding that was actually printed. Pure; no future leakage.

For each settlement T (a distinct ``next_settlement_ms`` in the validated rows):

  * the FORECAST is the row, at a PREDECLARED offset before T (default 60, 30 and 10 minutes,
    tolerance 3 minutes), whose ``next_settlement_ms`` is T. Nothing after T is consulted;
  * the REALISED value is the public funding print whose ``last_print_settled_ms`` equals T,
    read from a row observed at or after T. A settlement with no such print is counted as
    MISSING and left out. Nothing is forward-filled, interpolated or borrowed from a
    neighbour; a print that appears BEFORE its own settlement is flagged as an ANOMALY and the
    settlement is excluded.

This is the PUBLIC print. It is not the bot's predicted income, not the payment attributable
to a position, and not an account settlement; those are separate, and only the last, once
reconciled against authoritative account records, is cash.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import evidence_stats as ES

DEFAULT_OFFSETS_MIN = (60, 30, 10)
TOL_MIN = 3
MIN_INDEPENDENT = 30
#: Funding is persistent, so 30 joined settlements can carry far fewer independent draws. A claim
#: needs the EFFECTIVE sample size of the realised series, not just the count.
MIN_ESS = 30.0


def _pct(xs: Sequence[float], p: float) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))]


def join(rows: Sequence[Mapping[str, Any]], *, offsets_min: Sequence[int] = DEFAULT_OFFSETS_MIN,
         tol_min: int = TOL_MIN) -> Dict[str, Any]:
    groups = ES.settlement_groups(rows)
    after: Dict[int, List[Mapping[str, Any]]] = {}
    anomalies: List[Dict[str, Any]] = []
    for r in rows:
        f = r["funding"]
        lps = f.get("last_print_settled_ms")
        if lps is None:
            continue
        if r["at_ms"] < lps:
            anomalies.append({"settlement_ms": lps, "at_ms": r["at_ms"], "why": "print seen before its settlement"})
        else:
            after.setdefault(int(lps), []).append(r)
    joined: List[Dict[str, Any]] = []
    missing_realised = 0
    missing_forecast = 0
    bad = {a["settlement_ms"] for a in anomalies}
    for T in sorted(groups):
        if T in bad:
            continue
        prints = after.get(T)
        if not prints:
            missing_realised += 1
            continue
        realised = {round(p["funding"]["last_print_bps"], 9) for p in prints}
        # two different public prints for one settlement: keep the one worst for a short carry
        # (the lowest) and flag the settlement, rather than let the first-read value win silently
        rec = {"settlement_ms": T, "realised_bps": min(p["funding"]["last_print_bps"] for p in prints),
               "realised_conflict": len(realised) > 1, "forecasts": {}, "naive_last_print": {}}
        for off in offsets_min:
            target = T - off * 60_000
            best = None
            for r in groups[T]:
                d = abs(r["at_ms"] - target)
                if d <= tol_min * 60_000 and r["at_ms"] < T and (best is None or d < best[0]):
                    best = (d, r)
            if best is not None:
                rec["forecasts"][off] = best[1]["funding"]["forecast_bps"]
                lp = best[1]["funding"].get("last_print_bps")
                if lp is not None:
                    rec["naive_last_print"][off] = lp          # the previous print, known at that time
        if not rec["forecasts"]:
            missing_forecast += 1
            continue
        joined.append(rec)
    return {"joined": joined, "settlements_seen": len(groups), "missing_realised": missing_realised,
            "missing_forecast": missing_forecast, "anomalies": anomalies}


def _adjacent(joined: Sequence[Mapping[str, Any]], interval_ms: int) -> List[Tuple[float, float]]:
    out = []
    for a, b in zip(joined, joined[1:]):
        if b["settlement_ms"] - a["settlement_ms"] == interval_ms:     # never bridge a missing settlement
            out.append((a["realised_bps"], b["realised_bps"]))
    return out


def evaluate(rows: Sequence[Mapping[str, Any]], *, offsets_min: Sequence[int] = DEFAULT_OFFSETS_MIN,
             tol_min: int = TOL_MIN, interval_ms: int = 8 * 3_600_000) -> Dict[str, Any]:
    j = join(rows, offsets_min=offsets_min, tol_min=tol_min)
    joined = j["joined"]
    realised = [r["realised_bps"] for r in joined]
    ess = ES.effective_sample_size(realised)
    conflicts = sum(1 for r in joined if r["realised_conflict"])
    why: List[str] = []
    if len(joined) < MIN_INDEPENDENT:
        why.append(f"{len(joined)} joined settlements, {MIN_INDEPENDENT} needed")
    if ess is None or ess < MIN_ESS:
        why.append(f"effective sample size of the realised series is {'n/a' if ess is None else round(ess, 1)}, "
                   f"{MIN_ESS:g} needed (funding is persistent: the count overstates the evidence)")
    if conflicts:
        why.append(f"{conflicts} settlement(s) with conflicting public prints")
    if j["anomalies"]:
        why.append(f"{len(j['anomalies'])} print(s) observed before their own settlement")
    out: Dict[str, Any] = {
        "settlements_seen": j["settlements_seen"], "joined": len(joined),
        "missing_realised": j["missing_realised"], "missing_forecast": j["missing_forecast"],
        "anomalies": j["anomalies"], "conflicting_prints": conflicts,
        "status": "OK" if not why else "INSUFFICIENT", "status_reasons": why,
        "min_independent": MIN_INDEPENDENT, "min_ess": MIN_ESS, "offsets_min": list(offsets_min),
        "realised_bps": {"n": len(realised), "ess": ess, "mean": ES.mean(realised),
                         "min": min(realised) if realised else None, "p05": _pct(realised, 0.05),
                         "share_negative": (sum(1 for x in realised if x < 0) / len(realised)) if realised else None,
                         "count_below_minus_1bps": sum(1 for x in realised if x < -1.0),
                         "ci": ES.block_bootstrap_ci(realised)},
        "by_offset": {}, "persistence": None, "reversals": None,
    }
    for off in offsets_min:
        pairs = [(r["forecasts"][off], r["realised_bps"], r["naive_last_print"].get(off)) for r in joined if off in r["forecasts"]]
        if not pairs:
            out["by_offset"][off] = {"n": 0, "status": "INSUFFICIENT"}
            continue
        err = [f - r for f, r, _ in pairs]
        sign = [(f > 0) == (r > 0) for f, r, _ in pairs if f != 0 and r != 0]
        naive = [abs(n - r) for _, r, n in pairs if n is not None]
        buckets: Dict[str, Dict[str, Any]] = {}
        for name, lo, hi in (("negative", -1e9, 0.0), ("0_to_0.5", 0.0, 0.5), ("0.5_to_1", 0.5, 1.0), ("1_plus", 1.0, 1e9)):
            sel = [(f, r) for f, r, _ in pairs if lo <= f < hi]
            buckets[name] = {"n": len(sel), "mean_forecast": ES.mean([f for f, _ in sel]),
                             "mean_realised": ES.mean([r for _, r in sel])}
        out["by_offset"][off] = {
            "n": len(pairs), "status": "OK" if len(pairs) >= MIN_INDEPENDENT else "INSUFFICIENT",
            "bias_bps": ES.mean(err), "mae_bps": ES.mean([abs(e) for e in err]),
            "rmse_bps": math.sqrt(sum(e * e for e in err) / len(err)),
            "sign_accuracy": (sum(sign) / len(sign)) if sign else None, "sign_n": len(sign),
            "mae_naive_last_print_bps": ES.mean(naive), "bias_ci": ES.block_bootstrap_ci(err),
            "calibration_by_forecast_bucket": buckets}
    adj = _adjacent(joined, interval_ms)
    if len(adj) >= 4:
        a = [x for x, _ in adj]
        b = [y for _, y in adj]
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
        out["persistence"] = {"adjacent_pairs": len(adj),
                              "lag1_correlation": (sum((x - ma) * (y - mb) for x, y in adj) / den) if den > 0 else None}
        out["reversals"] = {"adjacent_pairs": len(adj),
                            "sign_change_share": sum(1 for x, y in adj if (x > 0) != (y > 0)) / len(adj)}
    out["note"] = ("the public realised print; not predicted income, not a position's payment, "
                   "not an account settlement")
    return out
