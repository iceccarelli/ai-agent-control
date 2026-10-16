#!/usr/bin/env python3
"""What did a carry run actually book, and how much of it is confirmed?

Reads a state database READ-ONLY (SQLite `mode=ro`) and prints the carry
journal's statement plus the ordinary-trade fee evidence. It places nothing and
writes nothing.

It answers the ACCOUNTING questions only (funding attribution, fee confirmation,
fills, provenance of net PnL); whether the venue is readable, whether the entry
evaluator would open, and whether the order gate is open are `tools/
carry_preflight.py`'s, and are reported here as NOT_CHECKED.

The one rule it exists to enforce: a net figure is called REALISED only when
every fill's fee was venue-stated and booked by currency (`fees_confirmed`).
Otherwise it says PROVISIONAL and names the counts, so an approximate number
cannot be quoted as a result.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def build_report(db_path: str) -> Dict[str, Any]:
    import ledger as _ledger

    if not os.path.exists(db_path):
        raise FileNotFoundError(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        out: Dict[str, Any] = {"db": db_path}
        row = conn.execute(
            "SELECT payload FROM carry_ledger WHERE id = 1").fetchone() \
            if _has(conn, "carry_ledger") else None
        if row is None:
            out["carry"] = {"status": "NO_CARRY_LEDGER"}
        else:
            journal = _ledger.Journal.from_rows(json.loads(row["payload"] or "[]"))
            import time as _time
            st = journal.statement(now_ms=int(_time.time() * 1000))
            import operator_verdicts
            out["carry"] = {
                # The separate questions (accounting, fees, fills, provenance).
                # The venue-facing ones (readability, authorisation, economics)
                # are NOT_CHECKED: this tool reads only the database.
                "questions": operator_verdicts.nine_questions(statement=st),
                "funding_lifecycles": st["funding_lifecycles"],
                "funding_reconciled": st["funding_reconciled"],
                "net_pnl_provenance": operator_verdicts.net_pnl_provenance(
                    st["net_basis"]),
                # NO_FILLS first: "every fee confirmed" is vacuously true of a
                # book that never traded, and must not be printed as a result.
                # REALISED needs BOTH sides of the net observed: every fee
                # stated by the venue AND every funding payment a venue row.
                "status": (
                    "NO_FILLS" if (st["net_basis"] == "no_fills"
                                   or st["fills"] == 0) else
                    "PROVISIONAL_FEES_INCOMPLETE"
                    if not st["fees_confirmed"] else
                    "PROVISIONAL_FUNDING_UNVERIFIED"
                    if not st["funding_confirmed"] else "REALISED"),
                "funding_venue_usd": st["funding_venue_usd"],
                "funding_estimated_usd": st["funding_estimated_usd"],
                "funding_unattributed": st["funding_unattributed"],
                "funding_unread": st["funding_unread"],
                "entries": st["entries"], "funding_usd": st["funding_usd"],
                "fees_usd": st["fees_usd"], "borrow_usd": st["borrow_usd"],
                "realised_usd": st["realised_usd"], "net_usd": st["net_usd"],
                "fees_unknown": st["fees_unknown"],
                "fees_unresolved": st["fees_unresolved"],
                "fees_unlabelled": st["fees_unlabelled"],
                "open_perp_qty": st["open_perp_qty"],
                "fills": [{"ref": e.ref, "kind": e.kind,
                           "fee_status": e.fee_status}
                          for e in journal.entries if e.fee_status],
            }
        trades: List[sqlite3.Row] = conn.execute(
            "SELECT * FROM trades").fetchall() if _has(conn, "trades") else []
        confirmed = [t for t in trades
                     if "entry_fee_realized" in t.keys()
                     and t["entry_fee_realized"] and t["exit_fee_realized"]]
        out["liquidation_evidence"] = _liquidation_evidence(conn)
        out["ordinary_trades"] = {
            "trades": len(trades),
            "fees_confirmed_trades": len(confirmed),
            "fees_unconfirmed_trades": len(trades) - len(confirmed),
            "realised_net_pnl": sum(t["net_pnl"] for t in confirmed),
            "unconfirmed_net_pnl": sum(
                t["net_pnl"] for t in trades if t not in confirmed),
        }
        return out
    finally:
        conn.close()


#: One funding interval. An observation older than this does not describe the
#: settlement a replay bar summarises, so a gap longer than it is a stretch the
#: replay refuses (carry_replay.LiquidationInputs.from_observations).
FUNDING_INTERVAL_MS = 8 * 3600 * 1000


def _liquidation_evidence(conn: sqlite3.Connection) -> Dict[str, Any]:
    """What the production liquidation gate has read and kept: how much, over
    what window, how tight it got, and where the gaps are. Read-only."""
    if not _has(conn, "carry_risk_observation"):
        return {"status": "NONE", "observations": 0}
    rows = conn.execute(
        "SELECT ms, distance_pct, account_mm_rate, liq_price FROM "
        "carry_risk_observation ORDER BY ms").fetchall()
    if not rows:
        return {"status": "NONE", "observations": 0}
    stamps = [int(r["ms"]) for r in rows]
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    distances = [r["distance_pct"] for r in rows if r["distance_pct"] is not None]
    rates = [r["account_mm_rate"] for r in rows
             if r["account_mm_rate"] is not None]
    return {
        "status": "RECORDING", "observations": len(rows),
        "first_ms": stamps[0], "last_ms": stamps[-1],
        "min_distance_pct": min(distances) if distances else None,
        "max_account_mm_rate": max(rates) if rates else None,
        "unreachable_observations": sum(
            1 for r in rows if r["liq_price"] is None),
        "largest_gap_ms": max(gaps) if gaps else 0,
        "gaps_over_one_funding_interval": sum(
            1 for g in gaps if g > FUNDING_INTERVAL_MS),
    }


def _has(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone() is not None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db", help="path to the state database")
    args = ap.parse_args(argv)
    try:
        report = build_report(args.db)
    except FileNotFoundError as exc:
        print(json.dumps({"error": f"no such database: {exc}"}))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
