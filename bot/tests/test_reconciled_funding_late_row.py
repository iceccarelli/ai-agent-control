"""A settlement row that arrives AFTER a lifecycle was reconciled must not leave
it reconciled.

A closed lifecycle is judged once and then no longer polled. If the account log
later delivers a second row for the same settlement (new id, same time, same
amount), the ledger books it, funding cash doubles, and nothing used to notice:
the reconciliation record still said FULLY_RECONCILED against rows it had never
counted. The record states how many rows it attributed; the journal must still
hold exactly that many."""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (os.path.dirname(HERE), os.path.join(os.path.dirname(HERE), "tools")):
    sys.path.insert(0, p)

import carry_broker as CB                                       # noqa: E402
import funding_reconcile as R                                   # noqa: E402
import ledger as L                                              # noqa: E402

H = 3_600_000
H8 = 8 * H
T0 = 1_800_000_000_000
S = T0 + H8
SYM = "BTCUSDT"
MARK = 1e5


def pay(i, t, usd, size=-0.5):
    return {"id": f"S{i}", "ms": t, "usd": usd, "currency": "USDT",
            "size": size, "symbol": SYM, "sign": "confirmed"}


def hist(prints, a, b):
    return CB.FundingHistory(start_ms=a, end_ms=b,
                             prints=[(1.0, int(t)) for t in prints])


def evid(rows, a, b):
    return CB.SettlementEvidence(start_ms=a, end_ms=b, payments=list(rows),
                                 windows=1, pages=1, rows_returned=len(rows))


def reconciled_journal():
    now = S + 20 * H
    j = L.Journal()
    L.perp_open(j, ms=S - 2 * H, ref="o1", qty=0.5, price=MARK, fee_usd=1,
                fee_status="confirmed")
    L.perp_close(j, ms=S + 2 * H, ref="c1", qty=0.5, price=MARK,
                 entry_price=MARK, fee_usd=1, fee_status="confirmed")
    rows = [pay(1, S, 1.0)]
    L.book_funding_payments(j, rows, ms=now)
    R.reconcile_funding(
        j, history=hist([S - H8, S, S + H8], S - 2 * H - 60_000, now),
        evidence=evid(rows, S - 2 * H - 60_000, now), now_ms=now)
    return j, now


def state(j):
    (row,) = L.lifecycle_reconciliation(j)
    return row


def test_the_reconciled_baseline_is_reconciled():
    j, _ = reconciled_journal()
    assert state(j)["reconciled"] is True
    assert j.statement()["funding_usd"] == pytest.approx(1.0)


def test_a_late_duplicate_row_downgrades_the_reconciliation():
    j, now = reconciled_journal()
    L.book_funding_payments(j, [pay(2, S, 1.0)], ms=now + 3 * H)
    assert j.statement()["funding_usd"] == pytest.approx(2.0)   # it IS booked
    row = state(j)
    assert row["reconciled"] is False
    assert row["state"] == "CONTRADICTED_AFTER_RECONCILIATION"
    assert "SETTLEMENT_ROWS_CHANGED_SINCE_RECONCILIATION" in row["reasons"]


def test_re_reading_the_same_row_changes_nothing():
    j, now = reconciled_journal()
    L.book_funding_payments(j, [pay(1, S, 1.0)], ms=now + 3 * H)   # same id
    assert j.statement()["funding_usd"] == pytest.approx(1.0)
    assert state(j)["reconciled"] is True


def test_a_re_assessment_can_reconcile_again_with_the_new_row_counted():
    """The downgrade is not a dead end: judging the lifecycle again counts both
    rows, and the public-history cross-check then flags the extra one."""
    j, now = reconciled_journal()
    later = now + 3 * H
    L.book_funding_payments(j, [pay(2, S, 1.0)], ms=later)
    R.reconcile_funding(
        j, history=hist([S - H8, S, S + H8], S - 2 * H - 60_000, later),
        evidence=evid([pay(1, S, 1.0), pay(2, S, 1.0)],
                      S - 2 * H - 60_000, later), now_ms=later)
    row = state(j)
    assert row["reconciled"] is False
    assert "ACCOUNT_SETTLEMENT_WITHOUT_PUBLIC_HISTORY" in row["reasons"] \
        or "SETTLEMENT_ROWS_CHANGED_SINCE_RECONCILIATION" in row["reasons"]


def test_a_record_that_never_stated_a_row_count_is_not_downgraded_by_it():
    """Hand-built records in older fixtures omit the count; they keep the
    behaviour they had (the other contradictions still apply)."""
    j, _ = reconciled_journal()
    for e in j.entries:
        if e.kind == L.RECONCILIATION_KIND:
            e.fee_raw["evidence"].pop("account_rows_attributed", None)
    assert state(j)["reconciled"] is True


def test_a_book_flat_across_the_settlement_is_not_held():
    """Close at S-10s and reopen the same size at S+10s: the quantity is equal
    at S-60s and S+60s, but the book held nothing at S. A row for S is not
    provably the book's and must not be attributed."""
    j = L.Journal()
    L.perp_open(j, ms=S - 2 * H, ref="o1", qty=0.5, price=MARK, fee_usd=1,
                fee_status="confirmed")
    L.perp_close(j, ms=S - 10_000, ref="c1", qty=0.5, price=MARK,
                 entry_price=MARK, fee_usd=1, fee_status="confirmed")
    L.perp_open(j, ms=S + 10_000, ref="o2", qty=0.5, price=MARK, fee_usd=1,
                fee_status="confirmed")
    assert R._classify_print(j, S) == "ambiguous"
    assert L._attribution_problem(j, pay(1, S, 1.0)) is not None


def test_a_position_held_untouched_through_the_settlement_is_still_attributed():
    j = L.Journal()
    L.perp_open(j, ms=S - 2 * H, ref="o1", qty=0.5, price=MARK, fee_usd=1,
                fee_status="confirmed")
    assert R._classify_print(j, S) == "held"
    assert L._attribution_problem(j, pay(1, S, 1.0)) is None


def test_the_cycle_report_stops_calling_a_doubled_cycle_actual_reconciled():
    import carry_performance as perf
    env = {"actual_reconciled_eligible": True,
           "booked_figures_class": "UNAVAILABLE", "reasons": []}

    def cls(j, now):
        cyc = perf.cycles_from_journal(j)
        recon = perf.cycle_funding_reconciliation(j, now_ms=now)
        for c in cyc:
            perf._classify_cycle(c, env, recon, [])
        return cyc[0]["evidence_class"]
    j, now = reconciled_journal()
    assert cls(j, now) == "ACTUAL_RECONCILED"
    L.book_funding_payments(j, [pay(2, S, 1.0)], ms=now + 3 * H)
    assert cls(j, now + 3 * H) != "ACTUAL_RECONCILED"
