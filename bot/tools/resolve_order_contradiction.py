#!/usr/bin/env python3
"""Clear ONE contradicted carry order in the local state database, on evidence.

An order is `contradicted` when the venue's report disagrees with the record
(more fill than recorded, a rejection with fills). Nothing automatic clears it:
a resolution needs the quantity a human verified AGAINST THE VENUE (statement,
execution list) and a written reason. This tool writes that one lifecycle event.

IT TALKS TO NO VENUE. It opens the local database only, reads no credential and
sends no order. Without `--apply` it prints what it would record and stops.

    python3 tools/resolve_order_contradiction.py --db PATH --order-link-id ID \\
        --verified-qty 0.012 --avg-price 64000 --evidence "execution list 2026-.." \\
        --confirm-not-resting
    ... add --apply to write the event.
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import order_lifecycle as lifecycle  # noqa: E402
from persistence import StateStore  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--order-link-id", required=True)
    ap.add_argument("--verified-qty", type=float, required=True)
    ap.add_argument("--avg-price", type=float, default=None)
    ap.add_argument("--evidence", required=True,
                    help="what verified the quantity, in words")
    ap.add_argument("--confirm-not-resting", action="store_true",
                    help="you verified at the venue that the order is no longer "
                         "live (a cancelled/filled lookup), not merely unfilled")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    if not args.evidence.strip():
        print("REFUSED: --evidence must say what verified the quantity")
        return 2
    if not (0 <= args.verified_qty < float("inf")):
        print("REFUSED: --verified-qty must be a finite number >= 0")
        return 2
    if not os.path.exists(args.db):
        print(f"REFUSED: no database at {args.db}")
        return 2
    store = StateStore(args.db)
    try:
        row = store.get_order(args.order_link_id)
        if row is None:
            print("REFUSED: no such order")
            return 2
        status = str(row["status"])
        print(f"order {args.order_link_id}: status={status} "
              f"recorded_fill={row.get('filled_qty')} requested={row['qty']}")
        if status != lifecycle.CONTRADICTED:
            print(f"REFUSED: only a {lifecycle.CONTRADICTED} order is resolved "
                  "here")
            return 2
        print(f"would record verified fill {args.verified_qty} "
              f"(evidence: {args.evidence})")
        if args.apply and not args.confirm_not_resting:
            print("REFUSED: --apply needs --confirm-not-resting; a resolved "
                  "order stops blocking entries, so it must not still be live")
            return 2
        if not args.apply:
            print("DRY RUN: nothing written (add --apply)")
            return 0
        result = store.apply_order_event(
            args.order_link_id, lifecycle.RESOLVE_CONTRADICTION,
            filled_qty=args.verified_qty, avg_price=args.avg_price,
            detail={"evidence": args.evidence, "tool": "resolve_order_contradiction"})
        print(f"{'APPLIED' if result.applied else 'REFUSED'}: "
              f"{result.previous} -> {result.status} ({result.reason})")
        return 0 if result.applied else 2
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
