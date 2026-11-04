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
from ledger_fixtures import legacy_journal as legacy_rows, vraw  # noqa: E402

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


def test_a_late_duplicate_row_is_kept_as_evidence_not_credited_and_downgrades():
    j, now = reconciled_journal()
    res = L.book_funding_payments(j, [pay(2, S, 1.0)], ms=now + 3 * H)
    assert res["booked"] == 0 and res["unattributed"] == 1
    assert j.statement()["funding_usd"] == pytest.approx(1.0)   # NOT re-credited
    row = state(j)
    assert row["reconciled"] is False
    assert row["state"] == "CONTRADICTED_AFTER_RECONCILIATION"
    assert "UNATTRIBUTED_SETTLEMENT_ROWS" in row["reasons"]
    ev = [e for e in j.entries if e.kind == "funding_unattributed"]
    assert len(ev) == 1 and "already reconciled" in ev[0].memo
    assert ev[0].fee_raw["venue_usd"] == 1.0                  # the evidence is complete


def test_the_row_count_check_still_catches_a_row_that_bypassed_the_booking_path():
    """Defence in depth: venue-basis cash posted by another route is still not
    vouched for by a record that never counted it."""
    j, now = reconciled_journal()
    L.funding(j, ms=now + 3 * H, ref="fund:X9", usd=1.0, basis="venue",
              raw={"venue_id": "X9", "venue_ms": S, "venue_usd": 1.0,
                   "symbol": SYM, "size": -0.5})
    row = state(j)
    assert row["reconciled"] is False
    assert "SETTLEMENT_ROWS_CHANGED_SINCE_RECONCILIATION" in row["reasons"]


def test_a_malformed_row_count_vouches_for_nothing_and_does_not_crash_the_statement():
    j, _ = reconciled_journal()
    for e in j.entries:
        if e.kind == L.RECONCILIATION_KIND:
            e.fee_raw["evidence"]["account_rows_attributed"] = "n/a"
    row = state(j)                       # must not raise
    assert row["reconciled"] is False
    j.statement()


def test_a_legitimate_new_settlement_on_an_open_lifecycle_does_not_contradict_it():
    """An open lifecycle keeps receiving settlements. A changed row count there
    is expected, not a contradiction; the next assessment judges the new row."""
    now = S + H
    j = L.Journal()
    L.perp_open(j, ms=S - 2 * H, ref="o1", qty=0.5, price=MARK, fee_usd=1,
                fee_status="confirmed")
    L.book_funding_payments(j, [pay(1, S, 1.0)], ms=now)
    R.reconcile_funding(j, history=hist([S - H8, S], S - 2 * H - 60_000, now),
                        evidence=evid([pay(1, S, 1.0)], S - 2 * H - 60_000, now),
                        now_ms=now)
    assert state(j)["open"] is True and state(j)["reconciled"] is True
    later = S + H8 + H
    L.book_funding_payments(j, [pay(2, S + H8, 0.8)], ms=later)
    assert cash(j) == pytest.approx(1.8)                       # a real settlement is cash
    assert state(j)["reconciled"] is True                      # and nothing blocks the book
    R.reconcile_funding(j, history=hist([S - H8, S, S + H8], S - 2 * H - 60_000, later),
                        evidence=evid([pay(1, S, 1.0), pay(2, S + H8, 0.8)],
                                      S - 2 * H - 60_000, later), now_ms=later)
    assert state(j)["reconciled"] is True


def test_a_duplicate_on_an_open_lifecycle_is_caught_by_the_next_assessment():
    now = S + H
    j = L.Journal()
    L.perp_open(j, ms=S - 2 * H, ref="o1", qty=0.5, price=MARK, fee_usd=1,
                fee_status="confirmed")
    L.book_funding_payments(j, [pay(1, S, 1.0), pay(2, S, 1.0)], ms=now)
    R.reconcile_funding(j, history=hist([S - H8, S], S - 2 * H - 60_000, now),
                        evidence=evid([pay(1, S, 1.0), pay(2, S, 1.0)],
                                      S - 2 * H - 60_000, now), now_ms=now)
    assert state(j)["reconciled"] is False
    assert "ACCOUNT_SETTLEMENT_WITHOUT_PUBLIC_HISTORY" in state(j)["reasons"]


def test_re_reading_the_same_row_changes_nothing():
    j, now = reconciled_journal()
    L.book_funding_payments(j, [pay(1, S, 1.0)], ms=now + 3 * H)   # same id
    assert j.statement()["funding_usd"] == pytest.approx(1.0)
    assert state(j)["reconciled"] is True


def test_re_assessing_after_a_late_duplicate_keeps_the_lifecycle_unreconciled():
    j, now = reconciled_journal()
    later = now + 3 * H
    L.book_funding_payments(j, [pay(2, S, 1.0)], ms=later)
    written = R.reconcile_funding(
        j, history=hist([S - H8, S, S + H8], S - 2 * H - 60_000, later),
        evidence=evid([pay(1, S, 1.0), pay(2, S, 1.0)], S - 2 * H - 60_000, later),
        now_ms=later)
    assert written, "the contradicted lifecycle must re-enter assessment"
    row = state(j)
    assert row["reconciled"] is False
    assert "UNATTRIBUTED_SETTLEMENT_ROWS" in row["reasons"]
    assert cash(j) == pytest.approx(1.0)


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


# -- the ten invariants, one adversarial case each ---------------------------------

def cash(j):
    return j.statement()["funding_usd"]


def restart(j):
    """What a process restart does: the journal comes back from its persisted rows."""
    return L.Journal.from_rows(j.to_rows())


def test_the_same_settlement_delivered_twice_before_reconciliation_counts_once():
    j = L.Journal()
    L.perp_open(j, ms=S - 2 * H, ref="o1", qty=0.5, price=MARK, fee_usd=1,
                fee_status="confirmed")
    r1 = L.book_funding_payments(j, [pay(1, S, 1.0)], ms=S + H)
    r2 = L.book_funding_payments(j, [pay(1, S, 1.0), pay(1, S, 1.0)], ms=S + 2 * H)
    assert (r1["booked"], r2["booked"], r2["duplicate"]) == (1, 0, 2)
    assert cash(j) == pytest.approx(1.0)


def test_the_same_settlement_replayed_after_reconciliation_adds_nothing_and_keeps_it_reconciled():
    j, now = reconciled_journal()
    before = [(e.ms, e.kind, e.ref, e.basis, [(l.account, l.usd) for l in e.lines])
              for e in j.entries]
    res = L.book_funding_payments(j, [pay(1, S, 1.0)], ms=now + 5 * H)
    assert res["booked"] == 0 and res["duplicate"] == 1
    assert cash(j) == pytest.approx(1.0) and state(j)["reconciled"] is True
    after = [(e.ms, e.kind, e.ref, e.basis, [(l.account, l.usd) for l in e.lines])
             for e in j.entries]
    assert after == before                       # nothing was written, let alone rewritten


def test_replay_after_a_process_restart_does_not_double_credit_or_lose_reconciliation():
    j, now = reconciled_journal()
    j2 = restart(j)
    assert cash(j2) == pytest.approx(1.0) and state(j2)["reconciled"] is True
    res = L.book_funding_payments(j2, [pay(1, S, 1.0)], ms=now + 5 * H)
    assert res["duplicate"] == 1 and cash(j2) == pytest.approx(1.0)
    assert state(j2)["reconciled"] is True


def test_a_late_duplicate_after_a_restart_is_still_not_credited_and_still_downgrades():
    j, now = reconciled_journal()
    j2 = restart(j)
    L.book_funding_payments(j2, [pay(2, S, 1.0)], ms=now + 3 * H)
    assert cash(j2) == pytest.approx(1.0)
    assert state(j2)["reconciled"] is False
    j3 = restart(j2)                                        # and it survives another one
    assert cash(j3) == pytest.approx(1.0) and state(j3)["reconciled"] is False


def test_a_conflicting_amount_under_the_same_id_is_refused_as_income_and_flagged():
    j, now = reconciled_journal()
    res = L.book_funding_payments(j, [pay(1, S, 7.0)], ms=now + 3 * H)
    assert res["id_collisions"] == 1 and res["booked"] == 0
    assert cash(j) == pytest.approx(1.0)                      # not a dollar of it is income
    row = state(j)
    assert row["reconciled"] is False
    assert "IDENTITY_CONFLICT" in row["reasons"]


def test_distinct_settlements_at_the_same_timestamp_are_not_collapsed_by_time():
    j = L.Journal()
    L.perp_open(j, ms=S - 2 * H, ref="o1", qty=0.5, price=MARK, fee_usd=1,
                fee_status="confirmed")
    res = L.book_funding_payments(j, [pay(1, S, 1.0), pay(2, S, 1.0)], ms=S + H)
    assert res["booked"] == 2 and res["duplicate"] == 0
    assert cash(j) == pytest.approx(2.0)


def test_delivery_order_does_not_change_the_cash():
    rows = [pay(1, S, 1.0), pay(2, S + H8, -0.4), pay(3, S + 2 * H8, 0.7)]

    def run(batches):
        j = L.Journal()
        L.perp_open(j, ms=S - 2 * H, ref="o1", qty=0.5, price=MARK, fee_usd=1,
                    fee_status="confirmed")
        t = S + 3 * H8
        for b in batches:
            L.book_funding_payments(j, b, ms=t)
            t += H
        return cash(j)
    in_order = run([rows])
    assert run([list(reversed(rows))]) == pytest.approx(in_order)
    assert run([[rows[2]], [rows[0]], [rows[1], rows[0]]]) == pytest.approx(in_order)
    assert in_order == pytest.approx(1.3)


def test_a_duplicate_after_a_delayed_reconciliation_is_counted_by_it_not_hidden():
    """The first reconciliation record is PROVISIONAL (log not yet judgeable);
    a duplicate arrives; the later full reconciliation must refuse to call the
    doubled cash reconciled."""
    j = closed_journal = L.Journal()
    L.perp_open(j, ms=S - 2 * H, ref="o1", qty=0.5, price=MARK, fee_usd=1,
                fee_status="confirmed")
    L.perp_close(j, ms=S + 2 * H, ref="c1", qty=0.5, price=MARK,
                 entry_price=MARK, fee_usd=1, fee_status="confirmed")
    early = S + 3 * H
    L.book_funding_payments(j, [pay(1, S, 1.0)], ms=early)
    R.reconcile_funding(j, history=hist([S - H8, S, S + H8], S - 2 * H - 60_000, early),
                        evidence=evid([pay(1, S, 1.0)], S - 2 * H - 60_000, early),
                        now_ms=early)
    assert state(j)["reconciled"] is False                    # too early to judge
    L.book_funding_payments(j, [pay(2, S, 1.0)], ms=early + H)
    late = S + 20 * H
    R.reconcile_funding(
        j, history=hist([S - H8, S, S + H8], S - 2 * H - 60_000, late),
        evidence=evid([pay(1, S, 1.0), pay(2, S, 1.0)], S - 2 * H - 60_000, late),
        now_ms=late)
    row = state(j)
    assert row["reconciled"] is False
    assert "ACCOUNT_SETTLEMENT_WITHOUT_PUBLIC_HISTORY" in row["reasons"]
    assert cash(j) == pytest.approx(2.0)                      # visible, not reconciled


def test_a_genuine_late_settlement_inside_a_reconciled_lifecycle_cannot_re_credit_silently():
    j, now = reconciled_journal()
    # the lifecycle spans S-2h..S+2h; a row for another settlement inside it
    L.book_funding_payments(j, [pay(9, S + H, 0.5)], ms=now + 3 * H)
    assert state(j)["reconciled"] is False
    assert cash(j) == pytest.approx(1.0)       # kept as evidence, a human decides


def test_partial_or_contradictory_evidence_stays_visible_after_a_downgrade():
    j, now = reconciled_journal()
    L.book_funding_payments(j, [pay(2, S, 1.0)], ms=now + 3 * H)
    row = state(j)
    assert row["state"] == "CONTRADICTED_AFTER_RECONCILIATION"
    assert row["reasons"] == ["UNATTRIBUTED_SETTLEMENT_ROWS"]
    # the audit trail of the earlier FULLY_RECONCILED record is still in the journal
    recs = [e for e in j.entries if e.kind == L.RECONCILIATION_KIND]
    assert recs and recs[0].fee_raw["state"] == "FULLY_RECONCILED"


# -- legacy records: no stored identity ------------------------------------------------

def legacy_journal(usd=1.0, ref="fund:S1"):
    """A venue-basis funding entry written before identity (venue_id) was stored."""
    j = L.Journal()
    L.perp_open(j, ms=S - 2 * H, ref="o1", qty=0.5, price=MARK, fee_usd=1,
                fee_status="confirmed")
    L.funding(j, ms=S + H, ref=ref, usd=usd, basis="venue", raw=vraw(ref, S, usd))
    # persisted before identity existed: no venue_id/venue_ms/venue_usd on the row
    rows = j.to_rows()
    for r in rows:
        if r["kind"] == "funding":
            r["fee_raw"] = {}
    return L.Journal.from_rows(rows)


def test_a_legacy_venue_entry_is_not_booked_again_when_its_payment_is_replayed():
    j = legacy_journal()
    assert "venue_id" not in j.entries[-1].fee_raw
    res = L.book_funding_payments(j, [pay(1, S, 1.0)], ms=S + 2 * H)
    assert res["duplicate"] == 1 and res["booked"] == 0
    assert cash(j) == pytest.approx(1.0)


def test_a_legacy_entry_with_a_different_amount_under_the_same_id_is_a_conflict():
    j = legacy_journal(usd=1.0)
    res = L.book_funding_payments(j, [pay(1, S, 4.0)], ms=S + 2 * H)
    assert res["id_collisions"] == 1 and res["booked"] == 0
    assert cash(j) == pytest.approx(1.0)


def test_an_unrelated_new_payment_still_books_alongside_a_legacy_entry():
    j = legacy_journal()
    res = L.book_funding_payments(j, [pay(2, S + H8, 0.5)], ms=S + 9 * H)
    assert res["booked"] == 1 and cash(j) == pytest.approx(1.5)


def test_a_legacy_row_with_no_basis_never_counts_as_venue_cash():
    """Rows written before provenance existed come back as engine estimates."""
    j = L.Journal()
    L.perp_open(j, ms=S - 2 * H, ref="o1", qty=0.5, price=MARK, fee_usd=1,
                fee_status="confirmed")
    L.funding(j, ms=S + H, ref="fund:S1", usd=1.0, basis="venue",
              raw=vraw("fund:S1", S, 1.0))
    j2 = legacy_rows(j, drop=("basis",))
    assert j2.statement()["funding_venue_usd"] == pytest.approx(0.0)


def test_a_legacy_entry_answers_only_one_replay():
    j = legacy_journal()
    res = L.book_funding_payments(j, [pay(1, S, 1.0)], ms=S + 2 * H)
    assert res["duplicate"] == 1
    again = L.book_funding_payments(j, [pay(1, S + H8, 1.0)], ms=S + 9 * H)
    assert again["duplicate"] == 0 and again["booked"] == 0 and again["id_collisions"] == 1
    assert cash(j) == pytest.approx(1.0)


def test_a_legacy_entry_does_not_swallow_a_payment_settled_after_it_was_booked():
    """Same id and amount, but the payment's settlement is LATER than the moment
    the legacy entry was booked: it cannot be the one that entry recorded."""
    j = legacy_journal()                    # booked at S + 1h
    res = L.book_funding_payments(j, [pay(1, S + 2 * H8, 1.0)], ms=S + 3 * H8)
    assert res["duplicate"] == 0
    assert res["id_collisions"] == 1        # visible evidence, not a silent drop
    assert cash(j) == pytest.approx(1.0)


def test_an_id_containing_a_hash_is_matched_whole():
    j = legacy_journal(ref="fund:SAB#7")
    res = L.book_funding_payments(j, [pay("AB#7", S, 1.0)], ms=S + 2 * H)   # id "SAB#7"
    assert res["duplicate"] == 1 and res["booked"] == 0
    assert cash(j) == pytest.approx(1.0)


def test_funding_completeness_agrees_with_ownership_for_a_flat_across_settlement_book():
    j = L.Journal()
    L.perp_open(j, ms=S - 2 * H, ref="o1", qty=0.5, price=MARK, fee_usd=1,
                fee_status="confirmed")
    L.perp_close(j, ms=S - 10_000, ref="c1", qty=0.5, price=MARK,
                 entry_price=MARK, fee_usd=1, fee_status="confirmed")
    L.perp_open(j, ms=S + 10_000, ref="o2", qty=0.5, price=MARK, fee_usd=1,
                fee_status="confirmed")
    assert R._classify_print(j, S) == "ambiguous"
    assert L.check_funding_completeness(j, [S], now_ms=S + 20 * H) == 0   # no false "missing"
