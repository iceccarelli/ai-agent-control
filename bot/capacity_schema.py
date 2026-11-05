"""Strict validation of recorded capacity evidence (schema capacity/2).

Evidence is only as good as the rule for rejecting it. Everything here is pure: rows in,
verdicts out, no I/O. A row either meets the schema or is REJECTED with a named reason;
nothing is repaired, defaulted or forward-filled, and a stream whose rows disagree with each
other about venue, environment, symbol, mode or schema is rejected as a whole, because mixed
evidence cannot be summarised honestly.

Legacy v1 rows (no ``schema`` key; recorded before provenance existed) are QUARANTINED, not
rejected as corrupt: they are counted and reported, but they carry no source identity and a different shape, so
they NEVER enter a statistic. There is deliberately no opt-in.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import md_capacity as MC

SCHEMA = MC.SCHEMA

# issue codes
MISSING_FIELD = "MISSING_FIELD"
BAD_TYPE = "BAD_TYPE"
NON_FINITE = "NON_FINITE"
BAD_SCHEMA = "INCOMPATIBLE_SCHEMA"
LEGACY = "LEGACY_V1_QUARANTINED"
MIXED = "MIXED_STREAM"
DUPLICATE_ID = "DUPLICATE_ROW_ID"
TIME_REGRESSION = "TIME_REGRESSION"
BAD_TIMESTAMP = "INVALID_TIMESTAMP"
BAD_SETTLEMENT = "IMPOSSIBLE_SETTLEMENT_CLOCK"
BAD_BOOK = "INVALID_BOOK_LEVELS"
BAD_CURVE = "INVALID_CURVE"
CORRUPT = "CORRUPT_RECORD"

_REQUIRED_TOP = ("schema", "model_version", "venue", "category", "environment", "symbol", "mode",
                 "row_id", "source", "at_ms", "times", "funding", "book", "fees", "curve")
_MIN_MS = 1_500_000_000_000          # 2017: anything earlier is not a Bybit-era wall clock
_MAX_MS = 4_000_000_000_000


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _fin(x: Any) -> bool:
    return _num(x) and math.isfinite(x)


def _issue(code: str, detail: str) -> Tuple[str, str]:
    return (code, detail)


def validate_row(row: Any) -> List[Tuple[str, str]]:
    """Problems with ONE row, independent of its neighbours. Empty means valid."""
    if not isinstance(row, dict):
        return [_issue(CORRUPT, f"row is {type(row).__name__}, not an object")]
    if "schema" not in row:
        return [_issue(LEGACY, "no schema key: a v1 row with no source identity")]
    if row.get("schema") != SCHEMA:
        return [_issue(BAD_SCHEMA, f"schema {row.get('schema')!r}, this reader accepts {SCHEMA!r}")]
    out: List[Tuple[str, str]] = []
    for k in _REQUIRED_TOP:
        if k not in row or row[k] is None:
            out.append(_issue(MISSING_FIELD, k))
    if out:
        return out
    if row["venue"] != MC.VENUE:
        out.append(_issue(MIXED, f"venue {row['venue']!r}"))
    if row["category"] != MC.CATEGORY:
        out.append(_issue(MIXED, f"category {row['category']!r}"))
    if row["environment"] not in ("mainnet", "testnet"):
        out.append(_issue(BAD_TYPE, f"environment {row['environment']!r}"))
    if not isinstance(row["symbol"], str) or not row["symbol"]:
        out.append(_issue(BAD_TYPE, "symbol"))
    src = row["source"]
    if not isinstance(src, dict) or not isinstance(src.get("store_record_id"), int) \
            or isinstance(src.get("store_record_id"), bool) or src["store_record_id"] < 0:
        out.append(_issue(MISSING_FIELD, "source.store_record_id (the market event this row was computed at)"))
    elif row["row_id"] != f"{row['environment']}:{row['symbol']}:{src['store_record_id']}":
        out.append(_issue(BAD_TYPE, "row_id does not match environment:symbol:store_record_id"))
    for k in ("perp_update_id", "perp_seq"):
        if not isinstance(src, dict) or not isinstance(src.get(k), int):
            out.append(_issue(MISSING_FIELD, f"source.{k}"))
    at = row["at_ms"]
    if not _fin(at) or not (_MIN_MS <= at <= _MAX_MS):
        out.append(_issue(BAD_TIMESTAMP, f"at_ms {at!r}"))
    t = row["times"]
    if not isinstance(t, dict):
        out.append(_issue(BAD_TYPE, "times"))
    else:
        ex = t.get("perp_book_exch_ts_ms")
        if not _fin(ex) or not (_MIN_MS <= ex <= _MAX_MS):
            out.append(_issue(BAD_TIMESTAMP, f"times.perp_book_exch_ts_ms {ex!r}"))
        elif _fin(at) and ex > at + 60_000:
            out.append(_issue(BAD_TIMESTAMP, "exchange timestamp more than 60 s after local receive"))
    f = row["funding"]
    if not isinstance(f, dict):
        out.append(_issue(BAD_TYPE, "funding"))
    else:
        fc, ns, iv = f.get("forecast_bps"), f.get("next_settlement_ms"), f.get("interval_h")
        if not _fin(fc):
            out.append(_issue(NON_FINITE if fc is not None else MISSING_FIELD, "funding.forecast_bps"))
        if not _fin(ns):
            out.append(_issue(BAD_SETTLEMENT, f"next_settlement_ms {ns!r}"))
        elif _fin(at) and not (at < ns <= at + 24 * 3_600_000 + 60_000):
            out.append(_issue(BAD_SETTLEMENT, "next settlement not in (now, now + 24 h]"))
        if not (isinstance(iv, int) and not isinstance(iv, bool) and 1 <= iv <= 24):
            out.append(_issue(BAD_SETTLEMENT, f"funding interval {iv!r}"))
        elif _fin(ns) and _fin(at) and ns - at > iv * 3_600_000 + 60_000:
            out.append(_issue(BAD_SETTLEMENT, "next settlement farther away than one funding interval"))
        lp, lps = f.get("last_print_bps"), f.get("last_print_settled_ms")
        if (lp is None) != (lps is None) or (lp is not None and not (_fin(lp) and _fin(lps))):
            out.append(_issue(NON_FINITE, "last print"))
        elif lps is not None and _fin(at) and lps > at:
            out.append(_issue(BAD_SETTLEMENT, "last funding print settled in the future"))
    b = row["book"]
    if not isinstance(b, dict) or any(not (isinstance(b.get(k), int) and b[k] >= 0) for k in
                                      ("perp_bid_levels", "perp_ask_levels", "spot_bid_levels", "spot_ask_levels")):
        out.append(_issue(BAD_BOOK, "level counts"))
    fe = row["fees"]
    if not isinstance(fe, dict) or not isinstance(fe.get("source"), str) \
            or not (_fin(fe.get("perp_taker_bps")) and fe["perp_taker_bps"] > 0):
        out.append(_issue(MISSING_FIELD, "fees provenance"))
    curve = row["curve"]
    if not isinstance(curve, list) or not curve:
        out.append(_issue(BAD_CURVE, "empty or not a list"))
    else:
        last_n = 0.0
        for p in curve:
            if not isinstance(p, dict) or not _fin(p.get("notional_usd")) or p["notional_usd"] <= 0 \
                    or p["notional_usd"] <= last_n or p.get("status") not in ("MEASURED", "UNAVAILABLE"):
                out.append(_issue(BAD_CURVE, "notionals must be positive, increasing, with a status"))
                break
            last_n = p["notional_usd"]
            if p["status"] == "MEASURED":
                keys = ("spread_bps", "slippage_bps", "total_round_trip_bps")
                if any(not (_fin(p.get(k)) and p[k] >= 0) for k in keys):
                    out.append(_issue(NON_FINITE, f"MEASURED point at {p['notional_usd']} has a bad cost"))
                    break
    return out


def validate_stream(rows: Sequence[Any]) -> Dict[str, Any]:
    """Validate rows in file order. Returns the accepted rows (those that passed AND are
    consistent with the stream) and every problem found. `accepted` is empty when the
    stream is mixed: partial acceptance of contradictory evidence is not offered."""
    problems: List[Dict[str, Any]] = []
    legacy = 0
    valid: List[Dict[str, Any]] = []
    for i, r in enumerate(rows):
        issues = validate_row(r)
        if issues and issues[0][0] == LEGACY:
            legacy += 1
            continue
        for code, detail in issues:
            problems.append({"index": i, "code": code, "detail": detail})
        if not issues:
            valid.append(r)
    v2 = [r for r in valid if r.get("schema") == SCHEMA]
    mixed = False
    for key in ("environment", "symbol", "mode", "venue", "category"):
        vals = {r[key] for r in v2}
        if len(vals) > 1:
            mixed = True
            problems.append({"index": None, "code": MIXED, "detail": f"{key} takes {sorted(map(str, vals))}"})
    seen: Dict[str, int] = {}
    prev_at: Optional[float] = None
    for i, r in enumerate(v2):
        rid = r["row_id"]
        if rid in seen:
            problems.append({"index": i, "code": DUPLICATE_ID, "detail": f"{rid} (first at {seen[rid]})"})
        seen.setdefault(rid, i)
        if prev_at is not None and r["at_ms"] < prev_at:
            problems.append({"index": i, "code": TIME_REGRESSION, "detail": f"{r['at_ms']} < {prev_at}"})
        prev_at = max(prev_at or 0, r["at_ms"])
    bad_codes = {p["code"] for p in problems}
    stream_bad = bool(bad_codes & {MIXED, DUPLICATE_ID, TIME_REGRESSION})
    accepted = [] if (mixed or stream_bad) else v2
    return {"accepted": accepted, "n_input": len(rows), "n_valid_rows": len(v2),
            "n_legacy_quarantined": legacy,
            "problems": problems, "stream_ok": not stream_bad and bool(v2),
            "note": "no repair, default or forward-fill is applied to any record"}
