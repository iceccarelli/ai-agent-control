#!/usr/bin/env python3
"""Show what Bybit ACTUALLY returns for fees and funding. Read-only, privacy-safe.

The fee and funding parsers were written from Bybit's documentation and tested on
documented-shape fixtures. This tool is how that is checked against a REAL
payload from the operator's testnet account. It sends nothing: GET requests
only, no order, no key, no signature is ever printed.

WHAT THE DEFAULT REPORT CONTAINS (and what it never contains)
  contains   field-presence counts, whether each field is present AND parseable,
             whether a position `size` is present, parseable and its numeric sign
             (which does NOT say what the sign means: signed-size semantics
             stay UNVERIFIED here; only the reconciliation path tests them),
             record types, the parser's verdict per order (known/unknown, which
             field it came from, which coins), page and completeness
             information, and coarse shape facts (id length, row age in days).
  never      balances, cash flow, fee amounts, funding amounts, position
             sizes, order quantities or prices, full transaction or order ids,
             keys, secrets, signatures.
  --raw-samples   an explicit, operator-chosen option that ALSO prints up to
             three sample rows per read with funding, size and truncated ids.
             It prints a privacy warning first. Balances, cash flow and fees of
             the account are still never printed.

COMPLETENESS
  The transaction log is cursor-paginated. Every cursor is followed with the
  production reader's own paginator (`carry_broker.read_log_pages`). A request
  error, a malformed page, a repeated cursor or running out of pages gives
  LOG_OBSERVATION_INCOMPLETE with the reason; a single page is never reported as
  the whole log, and no claim about `size` is made from an incomplete read.

Verdicts: FEE_PAYLOAD_OBSERVED | NO_FILLED_ORDERS_TO_OBSERVE for orders;
SETTLEMENT_ROWS_OBSERVED | NO_SETTLEMENT_ROWS_TO_OBSERVE |
LOG_OBSERVATION_INCOMPLETE for the log; PREFLIGHT_REFUSED (exit 3) in a wrong
environment, with nothing read. Documentation is not proof of what this account
returns: a verdict here is evidence only for the rows it actually saw.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

FEE_FIELDS = ("cumFeeDetail", "cumExecFee", "feeCurrency")
DAY_MS = 86_400_000

#: Printed to stderr before any raw output.
RAW_WARNING = (
    "PRIVACY WARNING: --raw-samples prints real account amounts (funding, "
    "position size, fee detail) and truncated ids to this terminal. Do not "
    "paste, commit or share the output. Balances, cash flow and fees of the "
    "account are still never printed, and no key, secret or signature ever is.")

#: Only these row fields can appear in --raw-samples. Never balances/cash flow.
RAW_LOG_FIELDS = ("id", "symbol", "type", "category", "funding", "size",
                  "currency", "transactionTime", "side")


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _short(value: Any) -> str:
    text = str(value)
    return text[:6] + "..." if len(text) > 6 else text


# ---------------------------------------------------------------------------
# orders (fee payload)
# ---------------------------------------------------------------------------


def probe(client: Any, symbol: str, limit: int = 20, *,
          raw_samples: bool = False) -> Dict[str, Any]:
    """Recent filled orders per category: what fee fields exist, and the parse."""
    import venue_fees

    observed: List[Dict[str, Any]] = []
    errors: Dict[str, str] = {}
    possibly_truncated = False
    for category in ("spot", "linear"):
        try:
            payload = client._request(
                "GET", "/v5/order/history", signed=True,
                params={"category": category, "symbol": symbol,
                        "limit": int(limit)})
        except Exception as exc:  # noqa: BLE001
            errors[category] = f"{type(exc).__name__}: {exc}"
            continue
        rows = (payload.get("result", {}).get("list") or [])
        possibly_truncated = possibly_truncated or len(rows) >= int(limit)
        for row in rows:
            if (_num(row.get("cumExecQty")) or 0.0) <= 0:
                continue
            fees = venue_fees.extract_order_fees(row)
            entry: Dict[str, Any] = {
                "category": category,
                "side": row.get("side"), "type": row.get("orderType"),
                "status": row.get("orderStatus"),
                "has_cumFeeDetail": "cumFeeDetail" in row,
                "fee_fields_present": [k for k in FEE_FIELDS if k in row],
                "parsed_known": fees.known,
                "parsed_source": fees.source or None,
                "fee_currencies": sorted({c.currency or "?"
                                          for c in fees.components}),
                "fee_components": len(fees.components),
            }
            if raw_samples:
                entry["order"] = _short(row.get("orderId", ""))
                entry["qty"] = row.get("cumExecQty")
                entry["avgPrice"] = row.get("avgPrice")
                entry["raw_fee_fields"] = {k: row[k] for k in FEE_FIELDS
                                           if k in row}
                entry["parsed"] = fees.to_dict()
            observed.append(entry)
    return {
        "verdict": ("FEE_PAYLOAD_OBSERVED" if observed
                    else "NO_FILLED_ORDERS_TO_OBSERVE"),
        "filled_orders_seen": len(observed),
        "with_cumFeeDetail": sum(o["has_cumFeeDetail"] for o in observed),
        "parsed_known": sum(o["parsed_known"] for o in observed),
        "parsed_unknown": sum(not o["parsed_known"] for o in observed),
        "history_limit_per_category": int(limit),
        "history_possibly_truncated": possibly_truncated,
        "read_errors": errors,
        "orders": observed,
    }


# ---------------------------------------------------------------------------
# transaction log
# ---------------------------------------------------------------------------


def _canonical(row: Dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, default=str)


def dedupe_rows(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """Collapse byte-identical rows (overlap); keep every DISTINCT row.

    Identity is the whole record. A venue id is never trusted alone: two
    distinct financial events that share an id are both kept.
    """
    seen: set = set()
    unique: List[Dict[str, Any]] = []
    for r in rows:
        key = _canonical(r)
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    return unique, len(rows) - len(unique)


def summarize_rows(rows: List[Dict[str, Any]], symbol: str, *,
                   now_ms: int) -> Dict[str, Any]:
    """Field presence and parseability over ALL rows. No amounts, no raw ids."""
    keys: Dict[str, int] = {}
    funding = {"present": 0, "parseable": 0, "positive": 0, "negative": 0,
               "zero": 0}
    # Field presence and the sign of the NUMBER only. A negative number is not
    # thereby a short: the probe has no independent position to compare with,
    # so nothing here says how the venue encodes direction.
    size = {"present": 0, "parseable": 0, "negative": 0, "positive": 0,
            "zero": 0}
    ids: List[str] = []
    times: List[float] = []
    currencies: set = set()
    symbols: set = set()
    for r in rows:
        for k in r:
            keys[k] = keys.get(k, 0) + 1
        f = r.get("funding")
        if f is not None and str(f).strip() != "":
            funding["present"] += 1
            v = _num(f)
            if v is not None:
                funding["parseable"] += 1
                funding["positive" if v > 0 else "negative" if v < 0
                        else "zero"] += 1
        s = r.get("size")
        if s is not None and str(s).strip() != "":
            size["present"] += 1
            v = _num(s)
            if v is not None:
                size["parseable"] += 1
                size["negative" if v < 0 else "positive" if v > 0
                     else "zero"] += 1
        if r.get("id") not in (None, ""):
            ids.append(str(r["id"]))
        t = _num(r.get("transactionTime"))
        if t is not None:
            times.append(t)
        if r.get("currency"):
            currencies.add(str(r["currency"]).upper())
        if r.get("symbol"):
            symbols.add(str(r["symbol"]))
    by_id: Dict[str, set] = {}
    for r in rows:
        if r.get("id") not in (None, ""):
            by_id.setdefault(str(r["id"]), set()).add(_canonical(r))
    shapes = set()
    for i in ids:
        shapes.add("digits" if i.isdigit() else "alnum" if i.isalnum()
                   else "has_separators")
    return {
        "rows": len(rows),
        "types_seen": sorted({str(r.get("type")) for r in rows}),
        "key_presence": dict(sorted(keys.items())),
        "funding": funding,
        "size": size,
        "id": {"present": len(ids), "distinct": len(set(ids)),
               "ids_shared_by_distinct_rows": sum(
                   1 for v in by_id.values() if len(v) > 1),
               "length_min": min((len(i) for i in ids), default=None),
               "length_max": max((len(i) for i in ids), default=None),
               "shapes": sorted(shapes)},
        "transactionTime": {
            "parseable": len(times),
            "oldest_age_days": (round((now_ms - min(times)) / DAY_MS, 1)
                                if times else None),
            "newest_age_days": (round((now_ms - max(times)) / DAY_MS, 1)
                                if times else None)},
        "currencies": sorted(currencies),
        "symbol_rows": sum(1 for r in rows if r.get("symbol") == symbol),
        "distinct_symbols": len(symbols),
    }


def _read(client: Any, extra: Dict[str, Any], windows: List[Tuple[int, int]],
          max_pages: int) -> Dict[str, Any]:
    from carry_broker import TXLOG_PAGE_SIZE, read_log_pages

    rows: List[Dict[str, Any]] = []
    pages = 0
    errors: List[str] = []
    for ws, we in windows:
        res = read_log_pages(
            lambda p: client._request("GET", "/v5/account/transaction-log",
                                      signed=True, params=p),
            {"accountType": "UNIFIED", "limit": TXLOG_PAGE_SIZE,
             "startTime": ws, "endTime": we, **extra},
            max_pages=max_pages)
        pages += res.pages
        rows.extend(res.rows)
        if not res.complete:
            errors.append(f"window {ws}..{we}: {res.error}")
    return {"rows": rows, "pages": pages, "errors": errors}


def probe_transaction_log(client: Any, symbol: str, *, now_ms: int,
                          days: int = 7, max_pages: Optional[int] = None,
                          raw_samples: bool = False) -> Dict[str, Any]:
    """Which fields do REAL transaction-log rows carry? Read-only, complete or
    explicitly incomplete.

    Two reads over the same windows: SETTLEMENT rows (what the book books) and
    rows of any type (the log's real shape). Each follows every cursor.
    """
    from carry_broker import TXLOG_MAX_PAGES, txlog_windows

    max_pages = TXLOG_MAX_PAGES if max_pages is None else int(max_pages)
    start = now_ms - int(days) * DAY_MS + 60_000
    windows = txlog_windows(start, now_ms)
    out: Dict[str, Any] = {
        "window_days": int(days), "windows": len(windows),
        "max_pages_per_window": max_pages, "reads": {},
        "privacy": ("default report: counts and parseability only; no account "
                    "amounts, balances or full ids"
                    if not raw_samples else
                    "RAW SAMPLES INCLUDED at the operator's request; see "
                    "the warning on stderr"),
    }
    all_errors: List[str] = []
    for name, extra in (("settlement", {"type": "SETTLEMENT",
                                        "category": "linear"}),
                        ("any_type", {})):
        raw = _read(client, extra, windows, max_pages)
        unique, dups = dedupe_rows(raw["rows"])
        summary = summarize_rows(unique, symbol, now_ms=now_ms)
        summary.update({
            "pages": raw["pages"],
            "complete": not raw["errors"],
            "errors": raw["errors"],
            "rows_returned": len(raw["rows"]),
            "duplicates_collapsed": dups,
        })
        if raw_samples:
            summary["raw_samples"] = [
                {k: (_short(r[k]) if k == "id" else r[k])
                 for k in RAW_LOG_FIELDS if k in r} for r in unique[:3]]
        out["reads"][name] = summary
        all_errors.extend(f"{name}: {e}" for e in raw["errors"])

    settle = out["reads"]["settlement"]
    out["complete"] = not all_errors
    out["errors"] = all_errors
    out["settlement_rows_seen"] = settle["rows"]
    # What this probe can and cannot establish, stated in the output itself.
    out["signed_size_semantics"] = "UNVERIFIED"
    out["signed_size_semantics_reason"] = (
        "this read-only probe has no independent signed position for the "
        "account at any settlement time, so it cannot tell what the sign of "
        "`size` means. Ownership is established only by the production "
        "reconciliation path (ledger.book_funding_payments), which compares "
        "the row's signed size with the book's own signed perp position.")
    if all_errors:
        out["verdict"] = "LOG_OBSERVATION_INCOMPLETE"
        out["size_field_observation"] = {"status": "OBSERVATION_INCOMPLETE"}
        out["note"] = ("the log read did not complete, so nothing here is a "
                       "claim about the whole log: " + "; ".join(all_errors))
        return out
    if not settle["rows"]:
        out["verdict"] = "NO_SETTLEMENT_ROWS_TO_OBSERVE"
        out["size_field_observation"] = {"status": "NO_SETTLEMENT_ROWS_OBSERVED"}
        out["note"] = (
            "the log was read completely and holds no SETTLEMENT rows in the "
            "queried window; the shape above comes from other row types only "
            "and says nothing about SETTLEMENT rows' `size`")
        return out
    sz = settle["size"]
    usable = sz["negative"] + sz["positive"]
    out["verdict"] = "SETTLEMENT_ROWS_OBSERVED"
    out["size_field_observation"] = {
        "status": ("ALL_ROWS_HAVE_PARSEABLE_NONZERO_SIZE"
                   if usable == settle["rows"]
                   else "SOME_ROWS_LACK_PARSEABLE_NONZERO_SIZE"),
        "rows": settle["rows"], "size_present": sz["present"],
        "size_parseable": sz["parseable"], "numeric_negative": sz["negative"],
        "numeric_positive": sz["positive"], "numeric_zero": sz["zero"],
        "size_absent_blank_or_unparseable": settle["rows"] - sz["parseable"],
        "meaning": "field presence and numeric sign only; not a statement "
                   "about long/short encoding",
    }
    out["note"] = (
        "SETTLEMENT rows were observed. Whether `size` is signed by position "
        "direction is NOT verified by this probe, whatever the values' signs. "
        + ("Every row has a parseable nonzero `size`, so the production path "
           "can compare it with the book's signed position; it will treat any "
           "row that does not match as unattributed and keep the statement "
           "provisional."
           if usable == settle["rows"] else
           "Some rows have an absent, blank, zero or unparseable `size`: the "
           "production path cannot attribute those, books them at $0 as "
           "unattributed, and keeps the statement provisional."))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--limit", type=int, default=20,
                    help="recent orders per category to inspect (a sample)")
    ap.add_argument("--funding", action="store_true",
                    help="also observe the account transaction log's real shape")
    ap.add_argument("--days", type=int, default=7,
                    help="transaction-log look-back, 1..90 (windows of <=7 days)")
    ap.add_argument("--raw-samples", action="store_true",
                    help="PRIVACY: also print sample rows with real amounts")
    args = ap.parse_args(argv)
    if not 1 <= args.days <= 90:
        print(json.dumps({"verdict": "PREFLIGHT_REFUSED",
                          "reason": "--days must be between 1 and 90"}))
        return 3

    from tools.carry_preflight import load_testnet_config
    cfg, refusal = load_testnet_config()
    if refusal is not None:
        print(refusal[0])
        return refusal[1]

    import bybit_connection as bc
    import persistence
    if args.raw_samples:
        print(RAW_WARNING, file=sys.stderr)
    store = persistence.StateStore(os.path.join(
        tempfile.mkdtemp(prefix="fee_probe_"), "state.db"))
    try:
        client = bc.BybitClient(config=cfg, store=store)
        report = probe(client, args.symbol, args.limit,
                       raw_samples=args.raw_samples)
        if args.funding:
            import time
            report["transaction_log"] = probe_transaction_log(
                client, args.symbol, now_ms=int(time.time() * 1000),
                days=args.days, raw_samples=args.raw_samples)
    finally:
        store.close()
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    complete = report.get("transaction_log", {}).get("complete", True)
    return 0 if complete else 4


if __name__ == "__main__":
    sys.exit(main())
