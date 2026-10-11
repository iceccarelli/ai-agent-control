#!/usr/bin/env python3
"""Show what Bybit ACTUALLY returns for fees on past filled orders. Read-only.

The fee parser (`venue_fees`) was written from Bybit's documentation, and its
tests use documented-shape fixtures. This tool is how that is checked against a
REAL payload: it reads recent order history from the testnet account, prints
only the fee-related fields of each filled order next to what the parser made of
them, and sends nothing. No order, no key, no signature is printed.

Verdicts: FEE_PAYLOAD_OBSERVED | NO_FILLED_ORDERS_TO_OBSERVE (exit 0), or
PREFLIGHT_REFUSED (exit 3, wrong environment, nothing read).

What to look for in the output:
  has_cumFeeDetail true  -> the modern field is present on this venue
  parsed.known true      -> the parser understood it
  parsed.known false     -> the parser did NOT; paste this output so the
                            parser can be fixed against the real shape
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from typing import Any, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

FEE_FIELDS = ("cumFeeDetail", "cumExecFee", "feeCurrency")


def probe(client: Any, symbol: str, limit: int = 20) -> Dict[str, Any]:
    """Read recent filled orders per category; pair raw fee fields with the parse."""
    import venue_fees

    observed: List[Dict[str, Any]] = []
    errors: Dict[str, str] = {}
    for category in ("spot", "linear"):
        try:
            payload = client._request(
                "GET", "/v5/order/history", signed=True,
                params={"category": category, "symbol": symbol,
                        "limit": int(limit)})
        except Exception as exc:  # noqa: BLE001
            errors[category] = f"{type(exc).__name__}: {exc}"
            continue
        for row in (payload.get("result", {}).get("list") or []):
            try:
                filled = float(row.get("cumExecQty") or 0) > 0
            except (TypeError, ValueError):
                filled = False
            if not filled:
                continue
            fees = venue_fees.extract_order_fees(row)
            observed.append({
                "category": category,
                "order": str(row.get("orderId", ""))[:8] + "...",
                "side": row.get("side"), "type": row.get("orderType"),
                "status": row.get("orderStatus"),
                "qty": row.get("cumExecQty"), "avgPrice": row.get("avgPrice"),
                "has_cumFeeDetail": "cumFeeDetail" in row,
                "raw_fee_fields": {k: row[k] for k in FEE_FIELDS if k in row},
                "parsed": fees.to_dict(),
            })
    return {
        "verdict": ("FEE_PAYLOAD_OBSERVED" if observed
                    else "NO_FILLED_ORDERS_TO_OBSERVE"),
        "filled_orders_seen": len(observed),
        "with_cumFeeDetail": sum(o["has_cumFeeDetail"] for o in observed),
        "parsed_known": sum(o["parsed"]["known"] for o in observed),
        "parsed_unknown": sum(not o["parsed"]["known"] for o in observed),
        "read_errors": errors,
        "orders": observed,
    }


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args(argv)

    from tools.carry_preflight import load_testnet_config
    cfg, refusal = load_testnet_config()
    if refusal is not None:
        print(refusal[0])
        return refusal[1]

    import bybit_connection as bc
    import persistence
    store = persistence.StateStore(os.path.join(
        tempfile.mkdtemp(prefix="fee_probe_"), "state.db"))
    try:
        client = bc.BybitClient(config=cfg, store=store)
        report = probe(client, args.symbol, args.limit)
    finally:
        store.close()
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
