"""Lifecycle-scoped funding reconciliation: is this book's funding PROVEN?

The ledger books what the venue paid (`ledger.book_funding_payments`) and flags
what it cannot claim. What it did not do, until this module, is *positively*
establish completeness: a book that was never reconciled had flagged nothing,
and the absence of a flag read as "confirmed". Here every position lifecycle
(`ledger.lifecycles`: one continuous non-flat perp interval, reconstructed from
the persisted journal alone) is reconciled against two independent venue reads
and the outcome is written to the journal as a $0 `funding_reconciliation`
entry, so it survives a restart and `Journal.statement()` can refuse to call
funding confirmed without it.

The two reads
-------------
* the PUBLIC funding history over the lifecycle's whole interval
  (`CarryBroker.get_funding_history_range`, walked backwards past the
  200-row limit): which settlements happened;
* the ACCOUNT settlement rows over the same interval
  (`CarryBroker.get_funding_evidence`): what this account was paid.

What decides FULLY_RECONCILED (every one must hold)
---------------------------------------------------
1. both reads cover the lifecycle interval and are complete (no failed
   window, no malformed row, no gap in the print schedule);
2. every public settlement the book provably held through (same signed
   exposure both sides of it, a minute either way) and old enough to have been
   delivered has exactly one account row, matched one-to-one;
3. no settlement fell on a moment the book's ownership cannot establish (a
   fill within a minute of it);
4. no account row in the interval is unattributed, in identity conflict, or
   has no public settlement behind it;
5. no `funding_missing` / `funding_unread` flag was ever written for it.

Anything else is PROVISIONAL with the reasons listed. Estimates never enter.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import ledger as L

#: Recorded in every record: the live account's signed-size behaviour has not
#: been observed (the account had no SETTLEMENT rows). The ledger treats a size
#: that does not match the book's signed quantity as NOT the book's, so an
#: unsigned venue encoding fails closed rather than attributing.
SIZE_SEMANTICS = "documented_signed_unconfirmed_on_live_account"


def _qty(journal: L.Journal, ms: int) -> float:
    return L._perp_qty_at(journal, ms)


def _classify_print(journal: L.Journal, t: int) -> str:
    """held | flat | ambiguous, from the journal's signed exposure around `t`."""
    before = _qty(journal, t - L.ATTRIBUTION_SLACK_MS)
    after = _qty(journal, t + L.ATTRIBUTION_SLACK_MS)
    dust = L.DUST[L.PERP]
    if abs(before) <= dust and abs(after) <= dust:
        return "flat"
    if abs(after - before) <= dust and abs(after) > dust:
        return "held"
    return "ambiguous"


def _match(prints: List[int], rows: List[int]) -> Tuple[int, List[int], List[int]]:
    """One-to-one match of print times to row times within the tolerance.

    Returns ``(matched, unmatched_prints, unmatched_rows)``. Greedy by smallest
    distance, so two prints can never share one row.
    """
    tol = L.PRINT_MATCH_TOLERANCE_MS
    pairs = sorted((abs(p - r), i, j) for i, p in enumerate(prints)
                   for j, r in enumerate(rows) if abs(p - r) <= tol)
    used_p, used_r = set(), set()
    for _d, i, j in pairs:
        if i in used_p or j in used_r:
            continue
        used_p.add(i)
        used_r.add(j)
    return (len(used_p),
            [p for i, p in enumerate(prints) if i not in used_p],
            [r for j, r in enumerate(rows) if j not in used_r])


def _covers(read: Any, start: int, end: int) -> bool:
    return (read is not None and int(read.start_ms) <= start
            and int(read.end_ms) >= end)


def assess_lifecycle(journal: L.Journal, lc: L.Lifecycle, *, history: Any,
                     evidence: Any, now_ms: int) -> Dict[str, Any]:
    """The reconciliation record for one lifecycle (a plain, JSON-safe dict)."""
    end = lc.end_ms if lc.end_ms is not None else int(now_ms)
    judged_to = int(now_ms) - L.MISSING_GRACE_MS
    reasons: List[str] = []

    hist_prints: List[int] = []
    if history is None:
        reasons.append("FUNDING_HISTORY_NOT_READ")
    else:
        if getattr(history, "errors", None):
            reasons.append("FUNDING_HISTORY_UNREADABLE")
        elif not _covers(history, lc.start_ms, end):
            reasons.append("FUNDING_HISTORY_DOES_NOT_COVER_LIFECYCLE")
        elif getattr(history, "gaps", None):
            reasons.append("FUNDING_HISTORY_HAS_GAPS")
        hist_prints = [int(ms) for _bps, ms in history.prints]
    if evidence is None:
        reasons.append("ACCOUNT_SETTLEMENT_LOG_NOT_READ")
    elif not evidence.complete:
        reasons.append("ACCOUNT_SETTLEMENT_LOG_INCOMPLETE")
    elif not _covers(evidence, lc.start_ms - L.ATTRIBUTION_SLACK_MS, end):
        reasons.append("ACCOUNT_SETTLEMENT_LOG_DOES_NOT_COVER_LIFECYCLE")

    # Every judgement below is about THIS lifecycle's interval only.
    lo = lc.start_ms - L.ATTRIBUTION_SLACK_MS
    hi = end + L.ATTRIBUTION_SLACK_MS
    in_life = [t for t in hist_prints if lo <= t <= hi]
    held, ambiguous, pending = [], [], []
    for t in in_life:
        if t > judged_to:
            pending.append(t)
            continue
        kind = _classify_print(journal, t)
        if kind == "held":
            held.append(t)
        elif kind == "ambiguous":
            ambiguous.append(t)

    flags = L.lifecycle_flags(journal, lc)
    booked = flags["booked"]
    attributed = [e for e in booked if e.basis == "venue"]
    unattributed, conflicts = flags["unattributed"], flags["conflicts"]
    row_ms = [int(e.fee_raw["venue_ms"]) for e in booked]

    matched, missing, orphan_rows = _match(held, row_ms)
    flagged_missing, unread = flags["missing"], flags["unread"]

    # Only a COMPLETE history can say a row has no public settlement behind it.
    history_whole = (history is not None and not reasons.count(
        "FUNDING_HISTORY_UNREADABLE") and not reasons.count(
        "FUNDING_HISTORY_HAS_GAPS") and not reasons.count(
        "FUNDING_HISTORY_DOES_NOT_COVER_LIFECYCLE"))
    rows_without_print = 0
    if history_whole:
        _m, _up, _ur = _match(in_life, row_ms)
        rows_without_print = len(_ur)
    if missing or flagged_missing:
        reasons.append("EXPECTED_SETTLEMENT_MISSING")
    if ambiguous:
        reasons.append("OWNERSHIP_AMBIGUOUS_AT_SETTLEMENT")
    if unattributed:
        reasons.append("UNATTRIBUTED_SETTLEMENT_ROWS")
    if conflicts:
        reasons.append("IDENTITY_CONFLICT")
    if rows_without_print:
        reasons.append("ACCOUNT_SETTLEMENT_WITHOUT_PUBLIC_HISTORY")
    if unread:
        reasons.append("FUNDING_UNREADABLE_WHILE_OPEN")
    if lc.end_ms is not None and judged_to < lc.end_ms:
        reasons.append("SETTLEMENTS_NOT_YET_JUDGEABLE")

    state = "FULLY_RECONCILED" if not reasons else "PROVISIONAL"
    final = (lc.end_ms is not None and state != "FULLY_RECONCILED"
             and int(now_ms) - lc.end_ms > L.RECONCILIATION_FINAL_AFTER_MS)
    return {
        "lifecycle_id": lc.id, "start_ms": lc.start_ms, "end_ms": lc.end_ms,
        "fingerprint": list(lc.fingerprint), "state": state,
        "reasons": reasons, "as_of_ms": int(now_ms), "judged_to_ms": judged_to,
        "final": bool(final),
        "evidence": {
            "size_semantics": SIZE_SEMANTICS,
            "history": history.to_dict() if history is not None else None,
            "account_log": evidence.to_dict() if evidence is not None else None,
            "public_settlements_in_lifecycle": len(in_life),
            "expected_settlements": len(held),
            "matched_settlements": matched,
            "missing_expected": len(missing),
            "flagged_missing": len(flagged_missing),
            "ambiguous_ownership": len(ambiguous),
            "pending_delivery_grace": len(pending),
            "account_rows_attributed": len(attributed),
            "account_rows_unattributed": len(unattributed),
            "account_rows_identity_conflict": len(conflicts),
            "account_rows_without_public_history": rows_without_print,
            "unread_markers": len(unread),
            "no_settlement_rows_to_observe": (
                evidence is not None and evidence.complete
                and not evidence.payments and not booked),
        },
    }


def _signature(rec: Dict[str, Any]) -> str:
    """What makes a new record worth writing: everything but the clock."""
    ev = dict(rec["evidence"])
    for k in ("history", "account_log"):
        ev.pop(k, None)
    ev["history_complete"] = (rec["evidence"].get("history") or {}).get(
        "complete")
    ev["log_complete"] = (rec["evidence"].get("account_log") or {}).get(
        "complete")
    return json.dumps([rec["fingerprint"], rec["state"], rec["reasons"],
                       rec["final"], ev], sort_keys=True, default=str)


def pending_lifecycles(journal: L.Journal) -> List[L.Lifecycle]:
    """Lifecycles still worth reading the venue for: open, or not yet final.

    A closed lifecycle that is FULLY_RECONCILED (judged to its end) or has been
    finalised as provisional needs no more polling.
    """
    status = {r["lifecycle_id"]: r for r in L.lifecycle_reconciliation(journal)}
    latest: Dict[str, Dict[str, Any]] = {}
    for e in journal.entries:
        if e.kind == L.RECONCILIATION_KIND:
            latest[str(e.fee_raw.get("lifecycle_id"))] = e.fee_raw
    out = []
    for lc in L.lifecycles(journal):
        if lc.end_ms is None:
            out.append(lc)
            continue
        rec = latest.get(lc.id)
        if status[lc.id]["reconciled"]:
            continue       # (a CONTRADICTED one is not "reconciled": it re-enters)
        if rec is not None and rec.get("final") and list(
                rec.get("fingerprint", [])) == list(lc.fingerprint):
            continue
        out.append(lc)
    return out


def reconcile_funding(journal: L.Journal, *, history: Any, evidence: Any,
                      now_ms: int) -> List[Dict[str, Any]]:
    """Assess every pending lifecycle and persist any CHANGED outcome.

    ``history``/``evidence`` are the two venue reads (or None when unreadable).
    Returns the records that were written. Idempotent: re-running with the same
    inputs writes nothing, so a restart or a repeated poll cannot flood the
    journal or change a status by being repeated.
    """
    written: List[Dict[str, Any]] = []
    # A settlement the book provably held through, old enough to be delivered,
    # with no account row is flagged ONCE and permanently - but only on the word
    # of two COMPLETE reads: an unreadable log proves nothing about absence.
    if (history is not None and evidence is not None and history.complete
            and evidence.complete):
        L.check_funding_completeness(
            journal, [ms for _bps, ms in history.prints], now_ms=now_ms)
    latest: Dict[str, str] = {}
    latest_at: Dict[str, int] = {}
    for e in journal.entries:
        if e.kind == L.RECONCILIATION_KIND:
            rec = dict(e.fee_raw)
            if rec.get("lifecycle_id"):
                latest[str(rec["lifecycle_id"])] = _signature(rec)
                latest_at[str(rec["lifecycle_id"])] = int(rec.get("as_of_ms", 0))
    for lc in pending_lifecycles(journal):
        rec = assess_lifecycle(journal, lc, history=history,
                               evidence=evidence, now_ms=now_ms)
        # An unchanged outcome is not rewritten - except that an OPEN lifecycle's
        # record is refreshed before it ages out (`ledger.RECORD_MAX_AGE_MS`),
        # which is what tells a later reader the book was still being checked.
        if latest.get(lc.id) == _signature(rec) and not (
                lc.end_ms is None and int(now_ms) - latest_at.get(lc.id, 0)
                > L.RECORD_MAX_AGE_MS // 3):
            continue
        stamp = max(int(now_ms), journal.entries[-1].ms if journal.entries else 0)
        journal.post(L.Entry(
            ms=stamp, kind=L.RECONCILIATION_KIND,
            ref=f"fundrec:{lc.id}:{stamp}:{len(journal.entries)}",
            lines=[L.Line(L.USDT, 0.0), L.Line(L.FUNDING, 0.0)],
            memo=f"funding reconciliation {rec['state']}: "
                 f"{', '.join(rec['reasons']) or 'complete'}"[:300],
            fee_raw=rec))
        written.append(rec)
    return written
