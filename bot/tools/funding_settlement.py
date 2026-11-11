#!/usr/bin/env python3
"""FUNDING SETTLEMENT RECOGNITION for one Testnet session: which settlements the hedge was really held through, what cash each one paid or charged, and whether the venue's own records can explain it.
Pure functions: no I/O, no network, no clock. The session feeds the result to the one ledger (``ms_accounting.Ledger.record_funding`` / ``declare_no_funding``); nothing here is a second ledger.

WHAT IS AND IS NOT KNOWN (state of the evidence, 2026-11): no real Bybit ``execType=Funding`` execution row and no real ``SETTLEMENT`` transaction-log row has ever been captured in this repository. The
sign of ``execFee`` on a Funding row is therefore UNVERIFIED. This module never trusts it blindly and never flips it:

  * hypothesis H1 (the only one acted on): cash received = - execFee (the same convention as a Trade row: a positive fee is a payment out). Cash > 0 = received, < 0 = paid (the ledger's sign).
  * the PHYSICAL EXPECTATION is independent of the row's convention: a short position receives when the public funding rate is positive, so  expected_cash = - signed_position * mark * rate.
  * a row is BOOKED only when its H1 cash agrees in sign with that expectation and in magnitude within a tolerance (and, when wallet samples straddle the settlement with no fill between them, the
    wallet's USDT delta agrees too). A row that disagrees is QUARANTINED, funding stays UNKNOWN (None, never zero), and the raw row is preserved in the evidence: an owner who captures one real row
    can then settle the convention; until then a wrong sign can never be booked as profit.

Settlement times and rates come from the public funding history (``/v5/market/funding/history``), never from a hard-coded 00/08/16 UTC grid; if that read is unavailable nothing is booked and nothing is
declared (UNKNOWN). Funding is ZERO only when a COMPLETE public history proves no print fell inside the held window (``declare_none``). A print inside the window with no matching row is UNKNOWN
(PENDING while the venue's history may still be catching up, MISSING after the grace), never zero. A fill within 60 s of a print makes that print AMBIGUOUS: not booked. Rows outside the window, for
another symbol, or from an earlier session are listed and never booked. Duplicate rows (same ``execId``) collapse; a second row at the same print with another id and the same amount is flagged and not
added; with a different amount it is a CONFLICT (unknown). Fee currency must be USDT.
"""
from __future__ import annotations

from decimal import Decimal as D
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

ATTRIBUTION_SLACK_MS = 60_000          # a perp fill this close to a print makes the held quantity at the print ambiguous
MATCH_TOLERANCE_MS = 300_000           # a row belongs to the print it is closest to within 5 minutes
GRACE_MS = 1_800_000                   # a missing row is PENDING for 30 minutes after the print, MISSING afterwards (UNKNOWN either way)
SIGN_MIN_EXPECTED_USD = D("0.005")     # below this the expected cash is too small for a sign check: the amount itself must be ~ the expectation
MAG_TOL_MARK = D("0.05")               # relative magnitude tolerance when the row states the settlement mark
MAG_TOL_PROXY = D("0.10")              # ... when only a nearby fill price is available as the mark
WALLET_TOL_ABS = D("0.02")
WALLET_TOL_REL = D("0.05")
EPS = D("1e-9")
QUOTE = "USDT"


def _d(x: Any) -> Optional[D]:
    if x is None or isinstance(x, bool) or x == "":
        return None
    try:
        v = D(str(x))
    except Exception:                                                           # noqa: BLE001
        return None
    return v if v.is_finite() else None


def _i(x: Any) -> Optional[int]:
    try:
        v = int(str(x))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _s(x: Optional[D], places: int = 8) -> Optional[str]:
    return None if x is None else format(x.quantize(D(1).scaleb(-places)), "f")


def parse_row(x: Mapping[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """One venue Funding row as a canonical dict, or (None, reason) when it cannot be used. Never raises: a malformed funding row must never fail the venue read it arrived in."""
    eid = x.get("exec_id")
    if not eid:
        return None, "NO_EXEC_ID"
    ts = _i(x.get("exec_time_ms"))
    if ts is None:
        return None, "NO_EXEC_TIME"
    fee = _d(x.get("fee"))
    if fee is None:
        return None, "NO_FEE"                                                   # a blank fee is UNKNOWN, never zero
    ccy = x.get("fee_ccy")
    if ccy != QUOTE:
        return None, f"FEE_CURRENCY_{ccy if ccy else 'UNKNOWN'}"
    return {"exec_id": str(eid), "symbol": x.get("symbol"), "ts_ms": ts, "fee": fee, "mark": _d(x.get("mark")) or _d(x.get("price")), "side": x.get("side"), "qty": _d(x.get("qty"))}, None


def _position_timeline(fills: Sequence[Mapping[str, Any]]) -> Tuple[List[Tuple[int, D, D]], Optional[int], Optional[int], bool]:
    """[(ts, signed_qty_after, price)] sorted; first fill ts; the ts at which the position returned to flat (None if still open); whether any fill exists."""
    ev = sorted(((int(f["ts_ms"]), (D(1) if f["side"] == "Buy" else D(-1)) * D(str(f["qty"])), D(str(f.get("price") or 0))) for f in fills if f.get("ts_ms") is not None), key=lambda e: e[0])
    pos, out, flat_ts, first = D(0), [], None, None
    for ts, q, px in ev:
        pos += q
        out.append((ts, pos, px))
        first = ts if first is None else first
        if abs(pos) <= EPS and flat_ts is None and first is not None:
            flat_ts = ts
        elif abs(pos) > EPS:
            flat_ts = None
    return out, first, flat_ts, bool(ev)


def _pos_before(tl: Sequence[Tuple[int, D, D]], ts: int) -> D:
    p = D(0)
    for t, after, _px in tl:
        if t < ts:
            p = after
        else:
            break
    return p


def _nearest_price(tl: Sequence[Tuple[int, D, D]], ts: int) -> Optional[D]:
    best = min(((abs(t - ts), px) for t, _p, px in tl if px > 0), default=None, key=lambda e: e[0])
    return None if best is None else best[1]


def _wallet_delta(samples: Sequence[Tuple[int, Any]], print_ts: int, fill_ts: Sequence[int]) -> Optional[Tuple[D, int, int]]:
    """(delta, before_ts, after_ts) from the last wallet sample before the print and the first after it, only when no fill of ANY leg fell between them; else None."""
    pts = sorted(((int(t), _d(v)) for t, v in samples if _d(v) is not None), key=lambda e: e[0])
    before = [p for p in pts if p[0] < print_ts]
    after = [p for p in pts if p[0] > print_ts]
    if not before or not after:
        return None
    b, a = before[-1], after[0]
    if any(b[0] <= t <= a[0] for t in fill_ts):
        return None
    return a[1] - b[1], b[0], a[0]


def recognise(*, symbol: str, perp_fills: Sequence[Mapping[str, Any]], other_fill_ts: Sequence[int] = (), prints: Optional[Sequence[Mapping[str, Any]]], rows: Sequence[Mapping[str, Any]],
              wallet_samples: Sequence[Tuple[int, Any]] = (), now_ms: int) -> Dict[str, Any]:
    """The funding story of ONE perp symbol in ONE session. ``perp_fills``: {ts_ms, side, qty, price} of the session's perp executions; ``other_fill_ts``: every other leg's fill times (a wallet delta
    across a fill is not funding); ``prints``: the public history {ts_ms, rate} (None = unavailable); ``rows``: the venue's non-Trade execution rows as the executor keeps them."""
    tl, t_in, flat_ts, any_fill = _position_timeline(perp_fills)
    res: Dict[str, Any] = {"schema": "funding_settlement.v1", "symbol": symbol, "sign_hypothesis": "H1_CASH_IS_MINUS_EXECFEE_UNVERIFIED", "window": None, "prints_available": prints is not None,
                           "prints": [], "actions": [], "unmatched_rows": [], "flags": [], "all_prints_resolved": False, "booked_usd": None}
    if not any_fill or t_in is None:
        res["flags"].append("NO_PERP_FILLS")
        return res
    t_out = flat_ts if flat_ts is not None else int(now_ms)
    res["window"] = {"first_fill_ms": t_in, "end_ms": t_out, "flat": flat_ts is not None}
    if prints is None:
        res["flags"].append("PRINTS_UNAVAILABLE")                                  # nothing booked, nothing declared: funding stays UNKNOWN
        return res
    parsed: Dict[str, Dict[str, Any]] = {}
    bad_rows: List[Dict[str, Any]] = []
    for r in rows:
        if r.get("type") not in (None, "Funding") or r.get("symbol") != symbol:
            bad_rows.append({"exec_id": r.get("exec_id"), "reason": "NOT_THIS_SYMBOL_OR_NOT_FUNDING"})
            continue
        row, why = parse_row(r)
        if row is None:
            bad_rows.append({"exec_id": r.get("exec_id"), "reason": "UNUSABLE:" + str(why)})
            continue
        parsed.setdefault(row["exec_id"], row)                                      # the same execution seen twice is ONE
    held = [p for p in sorted(prints, key=lambda p: int(p["ts_ms"])) if t_in < int(p["ts_ms"]) <= t_out]
    used: set = set()
    all_fill_ts = [int(f["ts_ms"]) for f in perp_fills if f.get("ts_ms") is not None] + [int(t) for t in other_fill_ts]
    resolved = True
    booked = D(0)
    any_booked = False
    for p in held:
        pts, rate = int(p["ts_ms"]), _d(p.get("rate"))
        pos = _pos_before(tl, pts)
        entry: Dict[str, Any] = {"ts_ms": pts, "rate": _s(rate, 10), "signed_position_qty": _s(pos), "status": None, "amount_usd": None, "expected_usd": None, "row_id": None, "wallet": None, "reason": None}
        res["prints"].append(entry)
        if abs(pos) <= EPS:
            entry["status"] = "NOT_HELD"
            continue
        if any(abs(int(f["ts_ms"]) - pts) <= ATTRIBUTION_SLACK_MS for f in perp_fills if f.get("ts_ms") is not None):
            entry["status"], entry["reason"] = "AMBIGUOUS_INSTANT", "a perp fill within 60 s of the settlement: the held quantity at the print is not certain"
            res["actions"].append({"action": "record", "ts_ms": pts, "amount_usd": None, "prov": "UNKNOWN"})
            resolved = False
            continue
        cands = sorted((r for r in parsed.values() if abs(r["ts_ms"] - pts) <= MATCH_TOLERANCE_MS and r["exec_id"] not in used), key=lambda r: (abs(r["ts_ms"] - pts), r["exec_id"]))
        if not cands:
            late = int(now_ms) - pts > GRACE_MS
            entry["status"] = "NO_ROW_MISSING" if late else "NO_ROW_PENDING"
            entry["reason"] = "no Funding row for this settlement" + ("" if late else " yet (inside the grace window)")
            res["actions"].append({"action": "record", "ts_ms": pts, "amount_usd": None, "prov": "UNKNOWN"})
            resolved = False
            continue
        row = cands[0]
        used.add(row["exec_id"])
        entry["row_id"] = row["exec_id"]
        extra = [c for c in cands[1:]]
        for c in extra:
            used.add(c["exec_id"])
        if any(c["fee"] != row["fee"] for c in extra):
            entry["status"], entry["reason"] = "CONFLICT", "two rows at one settlement with different amounts"
            res["flags"].append(f"FUNDING_CONFLICT:{symbol}:{pts}")
            res["actions"].append({"action": "record", "ts_ms": pts, "amount_usd": None, "prov": "UNKNOWN"})
            resolved = False
            continue
        if extra:
            res["flags"].append(f"DUPLICATE_PRINT_ROW:{symbol}:{pts}:{len(extra)}")      # flagged, NOT added: a real second payment would look identical
        cash = -row["fee"]
        mark, proxy = row["mark"], False
        if mark is None or mark <= 0:
            mark, proxy = _nearest_price(tl, pts), True
        expected = None if (rate is None or mark is None) else -pos * mark * rate
        entry["expected_usd"], entry["amount_usd"] = _s(expected), _s(cash)
        if expected is None:
            entry["status"], entry["reason"] = "QUARANTINED_ROW", "no rate or no mark to check the row against"
            res["actions"].append({"action": "record", "ts_ms": pts, "amount_usd": None, "prov": "UNKNOWN"})
            resolved = False
            continue
        if abs(expected) < SIGN_MIN_EXPECTED_USD:
            ok_sign = abs(cash - expected) <= D("0.01")
            ok_mag = ok_sign
        else:
            ok_sign = (cash > 0) == (expected > 0) and cash != 0
            ok_mag = abs(cash - expected) <= abs(expected) * (MAG_TOL_PROXY if proxy else MAG_TOL_MARK)
        if not ok_sign:
            entry["status"], entry["reason"] = "QUARANTINED_SIGN", "the row's cash (H1) has the opposite sign to the physical expectation: the convention is UNVERIFIED, nothing is booked and nothing is flipped"
            res["actions"].append({"action": "record", "ts_ms": pts, "amount_usd": None, "prov": "UNKNOWN"})
            resolved = False
            continue
        if not ok_mag:
            entry["status"], entry["reason"] = "QUARANTINED_MAGNITUDE", "the row's size disagrees with position x mark x rate"
            res["actions"].append({"action": "record", "ts_ms": pts, "amount_usd": None, "prov": "UNKNOWN"})
            resolved = False
            continue
        w = _wallet_delta(wallet_samples, pts, all_fill_ts)
        if w is None:
            entry["wallet"] = {"status": "NOT_SAMPLED"}
            entry["status"] = "BOOKED"
            res["flags"].append(f"WALLET_UNCONFIRMED:{symbol}:{pts}")
        else:
            delta, b_ts, a_ts = w
            ok_w = abs(delta - cash) <= WALLET_TOL_ABS + WALLET_TOL_REL * abs(cash)
            entry["wallet"] = {"status": "CONFIRMED" if ok_w else "MISMATCH", "delta_usd": _s(delta), "before_ms": b_ts, "after_ms": a_ts}
            if not ok_w:
                entry["status"], entry["reason"] = "WALLET_MISMATCH", "the wallet's USDT change across the settlement does not equal the row: the cash record cannot explain it"
                res["actions"].append({"action": "record", "ts_ms": pts, "amount_usd": None, "prov": "UNKNOWN"})
                resolved = False
                continue
            entry["status"] = "BOOKED_WALLET_CONFIRMED"
        res["actions"].append({"action": "record", "ts_ms": pts, "amount_usd": _s(cash), "prov": "MEASURED"})
        booked += cash
        any_booked = True
    held_positions = [e for e in res["prints"] if e["status"] != "NOT_HELD"]
    for r in parsed.values():
        if r["exec_id"] not in used:
            why = "BEFORE_FIRST_FILL" if r["ts_ms"] <= t_in else "AFTER_WINDOW" if r["ts_ms"] > t_out + MATCH_TOLERANCE_MS else "NO_HELD_PRINT_NEARBY"
            res["unmatched_rows"].append({"exec_id": r["exec_id"], "ts_ms": r["ts_ms"], "reason": why})
    res["unmatched_rows"] += bad_rows
    if not held_positions:
        res["actions"].append({"action": "declare_none", "reason": "the complete public funding history shows no settlement while the perp position was held", "prov": "DERIVED"})
        res["all_prints_resolved"] = True
        res["booked_usd"] = "0"
    else:
        res["all_prints_resolved"] = resolved
        res["booked_usd"] = _s(booked) if (resolved and any_booked) else None
    return res
