"""Estimated funding can never become verified, realised cash.

The engine's own funding number (an ESTIMATE) and what the venue actually paid
(a SETTLEMENT) are different things. This file pins every way the two could be
confused, including rows persisted before provenance existed:

  * a legacy estimate and the venue's row for the same settlement are not both
    counted: the venue row supersedes the estimate by an appended REVERSAL, and
    nothing in the journal is rewritten or deleted;
  * a funding row with no basis is an estimate to every reader, never cash;
  * a NEW entry cannot claim venue provenance without the venue row it came from;
  * a reconciliation record does not outlive the evidence it cites;
  * an estimate inside a lifecycle keeps that lifecycle unreconciled.
"""
from __future__ import annotations

import copy
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for p in (BOT, os.path.join(BOT, "tools"), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import carry_performance as perf                                # noqa: E402
import funding_reconcile as R                                   # noqa: E402
import ledger as L                                              # noqa: E402
from ledger_fixtures import legacy_journal, vraw                # noqa: E402
from test_reconciled_funding_late_row import (H, H8, MARK, S, evid,  # noqa: E402
                                              hist, pay)

LO = S - 2 * H - 60_000
NOW = S + 20 * H
ENV = {"actual_reconciled_eligible": True, "booked_figures_class": "UNAVAILABLE",
       "reasons": []}


def closed_journal(post_between=lambda j: None):
    j = L.Journal()
    L.perp_open(j, ms=S - 2 * H, ref="o1", qty=0.5, price=MARK, fee_usd=1,
                fee_status="confirmed")
    post_between(j)
    L.perp_close(j, ms=S + 2 * H, ref="c1", qty=0.5, price=MARK,
                 entry_price=MARK, fee_usd=1, fee_status="confirmed")
    return j


def reconcile(j, rows, now=NOW):
    R.reconcile_funding(j, history=hist([S - H8, S, S + H8], LO, now),
                        evidence=evid(rows, LO, now), now_ms=now)


def cls(j, now=NOW):
    rc = perf.cycle_funding_reconciliation(j, now_ms=now)
    cycles = perf.cycles_from_journal(j)
    for c in cycles:
        perf._classify_cycle(c, ENV, rc, [])
    return [c["evidence_class"] for c in cycles]


def estimate_journal(ref="fund:S1", usd=1.0, ms=None):
    """A journal holding the ENGINE'S estimate for the settlement at S."""
    return closed_journal(lambda j: L.funding(j, ms=S + H if ms is None else ms,
                                              ref=ref, usd=usd))


def restart(j):
    return L.Journal.from_rows(j.to_rows())


# -- A. a legacy estimate and the venue's row for it are not both counted -----------------

@pytest.mark.parametrize("persisted_as", ["no_basis_key", "engine_estimate"])
def test_the_venue_row_supersedes_a_legacy_estimate_instead_of_adding_to_it(persisted_as):
    j = estimate_journal()
    if persisted_as == "no_basis_key":
        j = legacy_journal(j, drop=("basis",))
    n_before = len(j.entries)
    L.book_funding_payments(j, [pay(1, S, 1.0)], ms=NOW)
    reconcile(j, [pay(1, S, 1.0)])
    st = j.statement(now_ms=NOW)
    assert st["funding_usd"] == pytest.approx(1.0)              # one dollar, not two
    assert st["funding_venue_usd"] == pytest.approx(1.0)
    assert st["funding_estimated_entries"] == 0
    assert st["funding_confirmed"] is True
    assert cls(j) == ["ACTUAL_RECONCILED"]
    # the audit trail: the estimate is still there, and so is its reversal
    kinds = [(e.kind, e.ref) for e in j.entries]
    assert ("funding", "fund:S1") in kinds
    assert any(k == "reversal" and r.startswith("fund:S1") for k, r in kinds)
    assert len(j.entries) > n_before                            # appended, never rewritten


def test_the_original_entries_are_untouched_by_the_supersession():
    j = estimate_journal()
    before = copy.deepcopy([(e.ms, e.kind, e.ref, e.basis, e.cycle,
                             [(l.account, l.usd, l.qty) for l in e.lines])
                            for e in j.entries])
    L.book_funding_payments(j, [pay(1, S, 1.0)], ms=NOW)
    after = [(e.ms, e.kind, e.ref, e.basis, e.cycle,
              [(l.account, l.usd, l.qty) for l in e.lines]) for e in j.entries]
    assert after[:len(before)] == before


def test_a_different_amount_supersedes_too_and_cash_is_the_venues_not_the_estimates():
    j = estimate_journal(usd=0.90)
    L.book_funding_payments(j, [pay(1, S, 1.0)], ms=NOW)
    st = j.statement(now_ms=NOW)
    assert st["funding_usd"] == pytest.approx(1.0)
    assert st["funding_venue_usd"] == pytest.approx(1.0)


def test_a_restart_before_the_venue_row_changes_nothing():
    j = restart(estimate_journal())
    L.book_funding_payments(j, [pay(1, S, 1.0)], ms=NOW)
    reconcile(j, [pay(1, S, 1.0)])
    assert j.statement(now_ms=NOW)["funding_usd"] == pytest.approx(1.0)
    assert cls(j) == ["ACTUAL_RECONCILED"]


def test_a_replay_after_the_supersession_and_a_restart_adds_nothing():
    j = estimate_journal()
    L.book_funding_payments(j, [pay(1, S, 1.0)], ms=NOW)
    j2 = restart(j)
    res = L.book_funding_payments(j2, [pay(1, S, 1.0)], ms=NOW + H)
    assert res["booked"] == 0 and res["duplicate"] == 1
    assert j2.statement(now_ms=NOW)["funding_usd"] == pytest.approx(1.0)
    assert len([e for e in j2.entries if e.kind == "reversal"]) == 1   # superseded once


def test_an_estimate_for_a_different_settlement_is_not_superseded_and_stays_visible():
    j = estimate_journal(ref="est:other", usd=5.0)
    L.book_funding_payments(j, [pay(1, S, 1.0)], ms=NOW)
    reconcile(j, [pay(1, S, 1.0)])
    st = j.statement(now_ms=NOW)
    assert st["funding_estimated_entries"] == 1
    assert st["funding_confirmed"] is False
    assert cls(j) != ["ACTUAL_RECONCILED"]


def test_a_conflicting_amount_under_a_venue_id_that_an_estimate_shares_is_still_cash_only_once():
    j = estimate_journal(usd=1.0)
    L.book_funding_payments(j, [pay(1, S, 7.0)], ms=NOW)
    st = j.statement(now_ms=NOW)
    assert st["funding_usd"] == pytest.approx(7.0)             # the venue's number, once
    assert st["funding_estimated_entries"] == 0


# -- B. a funding row with no basis is an estimate to every reader ----------------------------

def test_a_blank_basis_row_is_an_estimate_in_the_statement_and_the_cycle_report():
    j = legacy_journal(estimate_journal(ref="est:x", usd=5.0), drop=(), basis="")
    assert [e.basis for e in j.entries if e.kind == "funding"] == [""]    # stored as written
    L.book_funding_payments(j, [pay(1, S, 1.0)], ms=NOW)
    reconcile(j, [pay(1, S, 1.0)])
    st = j.statement(now_ms=NOW)
    assert st["funding_confirmed"] is False
    assert st["funding_estimated_entries"] == 1
    assert cls(j) != ["ACTUAL_RECONCILED"]


def test_estimated_dollars_are_reported_apart_from_venue_dollars():
    j = legacy_journal(estimate_journal(ref="est:x", usd=5.0), drop=(), basis="")
    L.book_funding_payments(j, [pay(1, S, 1.0)], ms=NOW)
    st = j.statement(now_ms=NOW)
    assert st["funding_venue_usd"] == pytest.approx(1.0)
    assert st["funding_estimated_usd"] == pytest.approx(5.0)


# -- C. new entries cannot claim provenance they do not have -------------------------------------

def test_a_new_entry_cannot_be_labelled_venue_without_the_venue_row():
    j = L.Journal()
    L.perp_open(j, ms=S - 2 * H, ref="o1", qty=0.5, price=MARK, fee_usd=1,
                fee_status="confirmed")
    with pytest.raises(L.LedgerError):
        L.funding(j, ms=S + H, ref="fund:FAKE", usd=5.0, basis="venue")
    with pytest.raises(L.LedgerError):
        L.funding(j, ms=S + H, ref="fund:FAKE", usd=5.0, basis="venue",
                  raw={"venue_id": "FAKE"})               # partial evidence is not evidence
    assert [e for e in j.entries if e.kind == "funding"] == []     # a refusal writes nothing


def test_a_new_funding_entry_cannot_have_a_blank_basis():
    j = L.Journal()
    L.perp_open(j, ms=S - 2 * H, ref="o1", qty=0.5, price=MARK, fee_usd=1,
                fee_status="confirmed")
    with pytest.raises(L.LedgerError):
        j.post(L.Entry(ms=S + H, kind="funding", ref="x", basis="",
                       lines=[L.Line(L.USDT, 50.0, 50.0), L.Line(L.FUNDING, -50.0)]))


def test_the_label_is_accepted_with_the_whole_venue_row():
    j = L.Journal()
    L.perp_open(j, ms=S - 2 * H, ref="o1", qty=0.5, price=MARK, fee_usd=1,
                fee_status="confirmed")
    L.funding(j, ms=S + H, ref="fund:S1", usd=1.0, basis="venue", raw=vraw("S1", S, 1.0))
    assert j.statement()["funding_venue_usd"] == pytest.approx(1.0)


def test_rows_persisted_before_provenance_still_load_as_written():
    j = legacy_journal(closed_journal(
        lambda j: L.funding(j, ms=S + H, ref="fund:S1", usd=1.0, basis="venue",
                            raw=vraw("S1", S, 1.0))), drop=("basis",))
    assert j.statement()["funding_venue_usd"] == pytest.approx(0.0)    # not venue cash
    k = legacy_journal(closed_journal(
        lambda j: L.funding(j, ms=S + H, ref="fund:S1", usd=1.0, basis="venue",
                            raw=vraw("S1", S, 1.0))), drop=(), basis="venue")
    assert k.entries                                            # loads without raising


def test_the_booking_path_never_trips_the_gate():
    j = closed_journal()
    res = L.book_funding_payments(j, [pay(1, S, 1.0), pay(2, S + H8, -0.4)], ms=NOW)
    assert res["booked"] + res["unattributed"] == 2


# -- D. a reconciliation record does not outlive the evidence it cites -----------------------------

def reconciled_with_one_row():
    j = closed_journal(lambda j: L.book_funding_payments(j, [pay(1, S, 1.0)], ms=S + H))
    reconcile(j, [pay(1, S, 1.0)])
    (row,) = L.lifecycle_reconciliation(j)
    assert row["reconciled"] is True
    return j


def test_the_baseline_record_is_reconciled():
    assert L.lifecycle_reconciliation(reconciled_with_one_row())[0]["reconciled"]


@pytest.mark.parametrize("mutate,reason", [
    (lambda ev: ev.__setitem__("history", {"complete": False}), "RECORD_CITES_INCOMPLETE_HISTORY"),
    (lambda ev: ev.__setitem__("account_log", {"complete": False}), "RECORD_CITES_INCOMPLETE_ACCOUNT_LOG"),
    (lambda ev: ev.__setitem__("missing_expected", 2), "RECORD_CITES_MISSING_EXPECTED"),
    (lambda ev: ev.__setitem__("ambiguous_ownership", 1), "RECORD_CITES_AMBIGUOUS_OWNERSHIP"),
    (lambda ev: ev.__setitem__("unread_markers", 1), "RECORD_CITES_UNREAD_MARKERS"),
    (lambda ev: ev.__setitem__("account_rows_without_public_history", 1),
     "RECORD_CITES_ACCOUNT_ROWS_WITHOUT_PUBLIC_HISTORY"),
    (lambda ev: ev.__setitem__("missing_expected", "n/a"), "RECORD_CITES_MISSING_EXPECTED"),
])
def test_a_record_that_cites_bad_evidence_is_not_trusted(mutate, reason):
    j = reconciled_with_one_row()
    for e in j.entries:
        if e.kind == L.RECONCILIATION_KIND:
            mutate(e.fee_raw["evidence"])
    (row,) = L.lifecycle_reconciliation(j)
    assert row["reconciled"] is False
    assert row["state"] == "CONTRADICTED_AFTER_RECONCILIATION"
    assert reason in row["reasons"]


def test_a_record_with_none_of_those_keys_is_tolerated():
    j = reconciled_with_one_row()
    for e in j.entries:
        if e.kind == L.RECONCILIATION_KIND:
            for k in ("history", "account_log", "missing_expected", "unread_markers"):
                e.fee_raw["evidence"].pop(k, None)
    assert L.lifecycle_reconciliation(j)[0]["reconciled"] is True


# -- E. an estimate inside a lifecycle keeps it unreconciled -----------------------------------------

def test_an_estimate_inside_the_lifecycle_keeps_it_unreconciled():
    """The engine's own number for a DIFFERENT settlement sits inside the
    window: the lifecycle's funding is not wholly the venue's."""
    j = estimate_journal(ref="est:other", usd=5.0)
    L.book_funding_payments(j, [pay(1, S, 1.0)], ms=NOW)
    reconcile(j, [pay(1, S, 1.0)])
    (row,) = L.lifecycle_reconciliation(j)
    assert row["reconciled"] is False
    assert "ESTIMATED_FUNDING_IN_LIFECYCLE" in row["reasons"]
    assert cls(j) != ["ACTUAL_RECONCILED"]


def test_an_estimate_stamped_after_the_lifecycle_is_caught_by_the_cycle_layer():
    j = reconciled_with_one_row()
    L.funding(j, ms=NOW + H, ref="est:late", usd=9.0)        # stamps only move forward
    st = j.statement(now_ms=NOW + H)
    assert st["funding_confirmed"] is False
    assert cls(j, now=NOW + H) != ["ACTUAL_RECONCILED"]


def test_a_superseded_estimate_does_not_contradict_the_lifecycle():
    j = estimate_journal()
    L.book_funding_payments(j, [pay(1, S, 1.0)], ms=NOW)
    reconcile(j, [pay(1, S, 1.0)])
    (row,) = L.lifecycle_reconciliation(j)
    assert row["reconciled"] is True


# -- F. nothing converts an estimate by relabelling ------------------------------------------------

def test_relabelling_a_persisted_estimate_as_blank_does_not_make_it_cash():
    j = estimate_journal(ref="est:x", usd=5.0)
    k = legacy_journal(j, drop=(), basis="")
    L.book_funding_payments(k, [pay(1, S, 1.0)], ms=NOW)
    reconcile(k, [pay(1, S, 1.0)])
    st = k.statement(now_ms=NOW)
    assert st["funding_confirmed"] is False
    assert cls(k) != ["ACTUAL_RECONCILED"]


def test_reversing_a_venue_row_does_not_turn_the_estimate_counters_into_cash():
    j = estimate_journal(ref="est:x", usd=5.0)
    j.reverse("est:x", ms=NOW, why="test")
    st = j.statement(now_ms=NOW)
    assert st["funding_venue_usd"] == pytest.approx(0.0)
