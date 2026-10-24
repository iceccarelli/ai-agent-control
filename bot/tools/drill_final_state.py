#!/usr/bin/env python3
"""Read the venue's final state for a carry drill that is already over.

A drill run WITHOUT the final-state capture (or one that was killed before it
ran) leaves an evidence file with the orders it sent and the order rows it
polled, but no `execution/list` row, so nothing in it can be reconciled. This
tool adds the missing reads to that same hash-chained file:

  * `order/realtime` / `order/history` and `execution/list` for every
    successful order-create the file already holds (the links are read from
    the file's own `POST /v5/order/create` records, not typed in);
  * `position/list` (the flat proof);
  * the account's SETTLEMENT rows from just before the first order to now.

READ-ONLY and TESTNET-ONLY: GETs through the same evidence-capturing client the
drill uses, nothing placed, amended or cancelled. Run it only AFTER the drill
process has exited: two writers appending to one hash chain would break it
(the tool also refuses a file whose chain does not verify).

    python3 tools/drill_final_state.py --evidence-path /tmp/drill2_evidence.jsonl
    python3 tools/evidence_reconcile.py --evidence /tmp/drill2_evidence.jsonl \\
        --drill-report /tmp/drill2_out.json --ledger /tmp/drill2_ledger.json \\
        --now-ms "$(date +%s%3N)" --json "$HOME/recon2.json"
    # the report may not be written beside the evidence file it read

Exit 0 only when every order was read to a final state with execution rows that
add up, the position was read, and the settlement window was complete; 6 when
any of that is missing; 3 when refused; 2 when the stack cannot start.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

#: Start the settlement read this long before the first order.
SETTLEMENT_LEAD_MS = 60_000


def discover_sent_orders(capture: Any, env: str = "testnet"
                         ) -> Tuple[List[Tuple[str, str, str]], Optional[int]]:
    """`(product, symbol, orderLinkId)` of every successful create in the capture
    (oldest first) and the capture time of the earliest, in ms."""
    import evidence_reconcile as R
    index = R.VenueIndex(capture)
    found = []
    for (e, link), sent in index.creates.items():
        if e != env:
            continue
        for body, _result, stamp in sent:
            found.append((stamp if stamp is not None else 0,
                          str(body.get("category", "")),
                          str(body.get("symbol", "")), link))
    found.sort()
    orders = []
    for _stamp, product, symbol, link in found:
        if (product, symbol, link) not in orders:
            orders.append((product, symbol, link))
    first = min((s for s, *_ in found if s), default=None)
    return orders, first


def run(broker: Any, capture: Any, *, symbol: str, now_ms: int) -> Dict[str, Any]:
    """The reads, given a broker and the already-verified capture."""
    import drill as D
    orders, first_ms = discover_sent_orders(capture)
    if not orders:
        return {"complete": False, "orders": 0, "reads": 0,
                "failures": ["the evidence file holds no successful order "
                             "create on the testnet host"]}
    adapter = D.unwrap_broker(broker)
    if not hasattr(adapter, "sent_order_links"):
        return {"complete": False, "orders": 0, "reads": 0,
                "failures": ["the broker cannot take the orders to read"]}
    adapter.sent_order_links.extend(orders)
    result = D.capture_final_venue_state(broker, symbol)
    start = max(0, (first_ms or now_ms) - SETTLEMENT_LEAD_MS)
    try:
        ev = broker.get_funding_evidence(symbol, start, now_ms)
        result["settlement_rows"] = len(ev.payments)
        result["settlements_complete"] = bool(ev.complete)
        if not ev.complete:
            result["failures"] = list(result["failures"]) + [
                "settlement read incomplete: " + "; ".join(ev.errors[:3])]
    except Exception as exc:                                   # noqa: BLE001
        result["settlement_rows"] = 0
        result["settlements_complete"] = False
        result["failures"] = list(result["failures"]) + [
            f"settlement read failed: {type(exc).__name__}: {exc}"]
    result["complete"] = (bool(result["complete"])
                          and result["settlements_complete"])
    return result


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--evidence-path", required=True,
                    help="the drill's evidence JSONL; the reads are appended")
    ap.add_argument("--symbol", default="BTCUSDT")
    args = ap.parse_args(argv)

    import evidence_reconcile as R
    if not os.path.isfile(args.evidence_path):
        print(f"refusing: {os.path.basename(args.evidence_path)!r} does not exist",
              file=sys.stderr)
        return 3
    capture = R.load_capture(args.evidence_path)
    if not capture.intact:
        print("refusing: the evidence chain does not verify "
              f"({capture.chain_issues[:3]}); appending to it would hide that",
              file=sys.stderr)
        return 3

    import drill as D
    try:
        bot = D._load_bot(args.evidence_path)
    except Exception as exc:                                   # noqa: BLE001
        print(f"could not start the stack: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    if bot.carry is None:
        print("BOOK_MODE is not carry (set the same three keys the drill "
              "needs: BOOK_MODE=carry CARRY_BORROW_APR=0.0 "
              "CARRY_EXECUTION_MODE=overlay)", file=sys.stderr)
        return 2
    why_not = D.session_refusal(bot.cfg, getattr(
        D.unwrap_broker(bot.carry.broker), "client", None))
    if why_not:
        print(f"REFUSED: {why_not}", file=sys.stderr)
        return 3

    result = run(bot.carry.broker, capture, symbol=args.symbol,
                 now_ms=int(time.time() * 1000))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["complete"] else 6


if __name__ == "__main__":
    raise SystemExit(main())
