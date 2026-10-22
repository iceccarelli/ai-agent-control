"""Lifecycle funding reconciliation: adversarial tests.

Every settlement row, funding print and venue envelope here is SYNTHETIC TEST
DATA written from the documented v5 shapes. None of it is an observation of a
real account: the operator's TESTNET account has returned no SETTLEMENT rows at
all, so how its settlement rows encode the signed position size is UNCONFIRMED,
and passing these tests establishes implementation behaviour only.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import carry_broker as CB  # noqa: E402
import funding_reconcile as R  # noqa: E402
import ledger as L  # noqa: E402
from carry_broker import CarryBroker  # noqa: E402
from test_carry_broker import FakeClient, envelope  # noqa: E402

H = 3_600_000
H8 = 8 * H
T0 = 1_800_000_000_000
MARK = 100_000.0
SYM = "BTCUSDT"


# --------------------------------------------------------------------------
# fixtures: a venue that honours windows/limits/cursors like the documented API
# --------------------------------------------------------------------------

class Venue(FakeClient):
    def __init__(self, prints=(), rows=(), **kw):
        super().__init__(**kw)
        self.prints = list(prints)
        self.rows = list(rows)
        self.history_calls = []
        self.log_calls = []
        self.history_hook = None
        self.log_hook = None

    def _request(self, method, endpoint, *, params=None, body=None,
                 signed=False, retries=3):
        if endpoint == "/v5/market/funding/history":
            self.history_calls.append((method, dict(params)))
            if self.history_hook:
                out = self.history_hook(len(self.history_calls), params)
                if out is not None:
                    return out
            lo, hi = int(params["startTime"]), int(params["endTime"])
            stamps = sorted((t for t in self.prints if lo <= t <= hi),
                            reverse=True)[:int(params["limit"])]
            return envelope({"category": "linear", "list": [
                {"symbol": params["symbol"], "fundingRate": "0.0001",
                 "fundingRateTimestamp": str(t)} for t in stamps]})
        if endpoint == "/v5/account/transaction-log":
            self.log_calls.append((method, dict(params)))
            if self.log_hook:
                out = self.log_hook(len(self.log_calls), params)
                if out is not None:
                    return out
            rows = [r for r in self.rows if params["startTime"]
                    <= int(r["transactionTime"]) <= params["endTime"]]
            i = int(params.get("cursor") or 0)
            n = int(params["limit"])
            more = (i + 1) * n < len(rows)
            return envelope({"list": rows[i * n:(i + 1) * n],
                             "nextPageCursor": str(i + 1) if more else ""})
        return super()._request(method, endpoint, params=params, body=body,
                                signed=signed, retries=retries)


def broker(venue):
    return CarryBroker(client=venue, sequence_source=lambda *a: 0,
                       order_gate=lambda: (False, "read-only"))


def srow(i, funding, t, **kw):
    base = {"id": f"S{i}", "symbol": SYM, "type": "SETTLEMENT",
            "category": "linear", "funding": str(funding),
            "change": str(funding),
            "transactionTime": str(t), "currency": "USDT", "size": "-0.5"}
    base.update(kw)
    return base


def pay(i, usd, t, size=-0.5, currency="USDT"):
    return {"id": f"S{i}", "ms": t, "usd": usd, "currency": currency,
            "size": size, "symbol": SYM, "sign": "confirmed"}


def hedged(qty=0.5, at=T0):
    j = L.Journal()
    L.perp_open(j, ms=at, ref="o1", qty=qty, price=MARK, fee_usd=1.0,
                fee_status="confirmed")
    return j


def hist(prints, start, end, **kw):
    return CB.FundingHistory(start_ms=start, end_ms=end,
                             prints=[(1.0, int(t)) for t in sorted(prints)],
                             **kw)


def evid(payments, start, end, **kw):
    return CB.SettlementEvidence(start_ms=start, end_ms=end,
                                 payments=list(payments), windows=1, pages=1,
                                 rows_returned=len(payments), **kw)


def recon(j, prints, rows, *, now, start=T0 - 2 * H, history=None,
          evidence=None, book=True):
    """Book `rows` then reconcile against complete synthetic reads."""
    if book:
        L.book_funding_payments(j, rows, ms=now)
    return R.reconcile_funding(
        j, history=history or hist(prints, start, now),
        evidence=evidence or evid(rows, start, now), now_ms=now)


def state(j):
    return j.statement()["funding_lifecycles"]


# --------------------------------------------------------------------------
# funding history over a lifecycle: > 200 prints, windows, overlap, failures
# --------------------------------------------------------------------------

class TestFundingHistoryCoversTheWholeInterval:
    def test_more_than_200_prints_are_all_retrieved_across_requests(self):
        prints = [T0 + k * H8 for k in range(450)]
        v = Venue(prints=prints)
        got = broker(v).get_funding_history_range(SYM, T0, prints[-1])
        assert got.complete and len(got.prints) == 450
        assert got.expected == 450 and got.gaps == []
        assert got.requests >= 3                        # 200 + 200 + 50
        assert [ms for _b, ms in got.prints] == prints

    def test_the_request_overlap_is_deduplicated_not_double_counted(self):
        prints = [T0 + k * H8 for k in range(401)]        # exactly 2 full pages+1
        v = Venue(prints=prints)
        got = broker(v).get_funding_history_range(SYM, T0, prints[-1])
        assert len(got.prints) == 401 and len({m for _b, m in got.prints}) == 401
        # later requests END at the oldest stamp already seen (inclusive overlap)
        ends = [p["endTime"] for _m, p in v.history_calls]
        assert ends[1] == prints[-200]

    def test_a_window_of_exactly_one_page_is_still_confirmed_by_the_next_request(self):
        prints = [T0 + k * H8 for k in range(200)]
        got = broker(Venue(prints=prints)).get_funding_history_range(
            SYM, T0, prints[-1])
        assert got.complete and len(got.prints) == 200

    def test_a_failed_request_makes_the_history_incomplete_not_empty(self):
        v = Venue(prints=[T0 + k * H8 for k in range(450)])

        def boom(n, p):
            if n == 2:
                raise RuntimeError("down")
        v.history_hook = boom
        got = broker(v).get_funding_history_range(SYM, T0, T0 + 450 * H8)
        assert not got.complete and "request 2 failed" in got.errors[0]

    @pytest.mark.parametrize("payload", [
        {"retCode": 0, "result": {"list": "nope"}},
        {"retCode": 0, "result": []},
        {"retCode": 0},
        {"retCode": 0, "result": {"list": [1, 2]}},
        {"retCode": 0, "result": {"category": "spot", "list": []}},
    ])
    def test_a_malformed_or_wrong_category_envelope_is_incomplete(self, payload):
        v = Venue()
        v.history_hook = lambda n, p: payload
        got = broker(v).get_funding_history_range(SYM, T0, T0 + H8)
        assert not got.complete and got.errors

    @pytest.mark.parametrize("row,why", [
        ({"fundingRate": "abc", "fundingRateTimestamp": str(T0 + 1)}, "malformed"),
        ({"fundingRate": "nan", "fundingRateTimestamp": str(T0 + 1)}, "unusable"),
        ({"fundingRate": "0.0001", "fundingRateTimestamp": "0"}, "unusable"),
        ({"fundingRate": "0.0001"}, "malformed"),
        ({"symbol": "ETHUSDT", "fundingRate": "0.0001",
          "fundingRateTimestamp": str(T0 + 1)}, "another symbol"),
        ({"symbol": SYM, "fundingRate": "0.0001",
          "fundingRateTimestamp": str(T0 - 5)}, "outside the requested"),
    ])
    def test_a_bad_row_makes_the_history_incomplete(self, row, why):
        v = Venue()
        v.history_hook = lambda n, p: envelope({"category": "linear",
                                                "list": [row]})
        got = broker(v).get_funding_history_range(SYM, T0, T0 + H8)
        assert not got.complete and why in got.errors[0]

    def test_two_different_rates_for_one_stamp_is_a_contradiction(self):
        rows = [{"symbol": SYM, "fundingRate": "0.0001",
                 "fundingRateTimestamp": str(T0 + 1)},
                {"symbol": SYM, "fundingRate": "0.0002",
                 "fundingRateTimestamp": str(T0 + 1)}]
        v = Venue()
        v.history_hook = lambda n, p: envelope({"category": "linear",
                                                "list": rows})
        got = broker(v).get_funding_history_range(SYM, T0, T0 + H8)
        assert not got.complete and "two different rates" in got.errors[0]

    def test_a_full_page_that_makes_no_progress_is_refused_not_looped(self):
        stamp = T0 + H8
        page = envelope({"category": "linear", "list": [
            {"symbol": SYM, "fundingRate": "0.0001",
             "fundingRateTimestamp": str(stamp)}] * CB.FUNDING_HISTORY_PAGE})
        v = Venue()

        def stuck(n, p):
            assert n < 20, "runaway"
            return page
        v.history_hook = stuck
        got = broker(v).get_funding_history_range(SYM, T0, stamp)
        assert not got.complete and "no progress" in got.errors[0]

    def test_running_out_of_requests_is_incomplete(self, monkeypatch):
        monkeypatch.setattr(CB, "FUNDING_HISTORY_MAX_REQUESTS", 2)
        prints = [T0 + k * H8 for k in range(900)]
        got = broker(Venue(prints=prints)).get_funding_history_range(
            SYM, T0, prints[-1])
        assert not got.complete and "incomplete" in got.errors[0]

    def test_an_expired_start_shows_up_as_a_leading_gap_not_as_coverage(self):
        # retention: nothing older than T0+30*8h exists although T0 was asked for
        prints = [T0 + (30 + k) * H8 for k in range(40)]
        got = broker(Venue(prints=prints)).get_funding_history_range(
            SYM, T0, prints[-1])
        assert not got.complete and got.gaps[0][0] == T0

    def test_a_missing_print_inside_the_series_is_a_gap_with_an_expected_count(self):
        prints = [T0 + k * H8 for k in range(20) if k != 9]
        got = broker(Venue(prints=prints)).get_funding_history_range(
            SYM, T0, prints[-1])
        assert got.gaps == [(T0 + 8 * H8, T0 + 10 * H8)] and got.expected == 20

    def test_a_schedule_change_is_not_a_gap(self):
        eight = [T0 + k * H8 for k in range(10)]
        four = [eight[-1] + (k + 1) * 4 * H for k in range(10)]
        got = broker(Venue(prints=eight + four)).get_funding_history_range(
            SYM, T0, four[-1])
        assert got.complete and got.expected == 20

    def test_a_hole_inside_a_four_hour_run_is_still_a_gap(self):
        four = [T0 + k * 4 * H for k in range(20) if k != 10]
        got = broker(Venue(prints=four)).get_funding_history_range(
            SYM, T0, four[-1])
        assert len(got.gaps) == 1 and got.expected == 20

    def test_too_few_prints_over_more_than_a_day_cannot_be_called_complete(self):
        got = broker(Venue(prints=[T0 + H8])).get_funding_history_range(
            SYM, T0, T0 + 5 * 86_400_000)
        assert not got.complete

    def test_only_get_requests_are_made(self):
        v = Venue(prints=[T0 + k * H8 for k in range(10)])
        broker(v).get_funding_history_range(SYM, T0, T0 + 10 * H8)
        broker(v).get_funding_evidence(SYM, T0, T0 + 10 * H8)
        assert v.created == [] and all(c[0] == "GET" for c in v.calls)


# --------------------------------------------------------------------------
# account SETTLEMENT rows: independent validation of every row
# --------------------------------------------------------------------------

class TestSettlementRowsAreValidatedIndividually:
    def ev(self, rows, **kw):
        return broker(Venue(rows=rows)).get_funding_evidence(
            SYM, T0, kw.get("end", T0 + 10 * H))

    def test_a_supposedly_filtered_request_returning_another_type_is_rejected(self):
        e = self.ev([srow(1, "1.0", T0 + H),
                     srow(2, "9.0", T0 + 2 * H, type="TRADE")])
        assert [p["id"] for p in e.payments] == ["S1"]
        assert e.rejected == {"wrong_type": 1} and e.complete

    def test_a_row_that_does_not_state_its_type_cannot_be_proved_a_settlement(self):
        r = srow(1, "1.0", T0 + H)
        del r["type"]
        e = self.ev([r])
        assert e.payments == [] and e.rejected == {"no_type": 1}

    def test_a_row_for_another_category_is_rejected(self):
        e = self.ev([srow(1, "1.0", T0 + H, category="spot")])
        assert e.payments == [] and e.rejected == {"wrong_category": 1}

    def test_other_symbols_are_counted_but_are_not_a_fault(self):
        e = self.ev([srow(1, "1.0", T0 + H, symbol="ETHUSDT")])
        assert e.payments == [] and e.other_symbol_rows == 1 and e.complete

    @pytest.mark.parametrize("bad", ["abc", "nan", "inf"])
    def test_an_unparseable_amount_makes_the_evidence_incomplete(self, bad):
        e = self.ev([srow(1, bad, T0 + H), srow(2, "1.0", T0 + 2 * H)])
        assert not e.complete                       # absence now proves nothing
        assert [p["id"] for p in e.payments] == ["S2"]   # the valid row survives

    def test_a_row_outside_the_requested_interval_is_an_inconsistency(self):
        class Ignores(Venue):
            def _request(self, method, endpoint, **kw):
                if endpoint == "/v5/account/transaction-log":
                    return envelope({"list": [srow(1, "1.0", T0 - 10 * H)],
                                     "nextPageCursor": ""})
                return super()._request(method, endpoint, **kw)
        e = broker(Ignores()).get_funding_evidence(SYM, T0, T0 + H)
        assert not e.complete and e.payments == []

    def test_multiple_bounded_windows_with_boundary_overlap_dedupe_to_one_row(self):
        day = 86_400_000
        start = T0
        # the second window starts exactly where the first ended (1 ms overlap):
        edge = start + CB.TXLOG_WINDOW_MS
        e = broker(Venue(rows=[srow(1, "1.0", edge)])).get_funding_evidence(
            SYM, start, start + 15 * day)
        assert e.windows == 3 and len(e.payments) == 1
        assert e.duplicate_rows == 1                   # seen in both windows

    def test_a_failed_window_makes_the_whole_read_incomplete(self):
        v = Venue(rows=[srow(1, "1.0", T0 + H)])

        def fail(n, p):
            if n == 2:
                raise RuntimeError("down")
        v.log_hook = fail
        e = broker(v).get_funding_evidence(SYM, T0, T0 + 20 * 86_400_000)
        assert not e.complete and "unreadable" in e.errors[0]

    def test_a_cursor_cycle_is_incomplete(self):
        v = Venue()
        v.log_hook = lambda n, p: envelope(
            {"list": [srow(n, "1.0", T0 + H)], "nextPageCursor": "same"})
        e = broker(v).get_funding_evidence(SYM, T0, T0 + 2 * H)
        assert not e.complete and "cursor repeated" in e.errors[0]

    def test_a_pagination_that_never_ends_is_bounded(self):
        v = Venue()
        v.log_hook = lambda n, p: envelope(
            {"list": [], "nextPageCursor": f"c{n}"})
        e = broker(v).get_funding_evidence(SYM, T0, T0 + 2 * H)
        assert not e.complete and "pages" in e.errors[0]

    def test_a_span_beyond_the_log_retention_is_refused_not_clipped(self):
        e = broker(Venue()).get_funding_evidence(
            SYM, 0, 800 * 86_400_000)
        assert not e.complete and "holds at most" in e.errors[0]

    def test_no_settlement_rows_at_all_is_a_complete_empty_read(self):
        e = self.ev([])
        assert e.complete and e.payments == []

    def test_the_legacy_list_reader_still_raises_when_incomplete(self):
        with pytest.raises(CB.PairIncident):
            broker(Venue(rows=[srow(1, "abc", T0 + H)])).get_funding_payments(
                SYM, T0, T0 + 10 * H)


# --------------------------------------------------------------------------
# ownership from the persisted journal
# --------------------------------------------------------------------------

def fill(j, kind, ms, ref, qty, **kw):
    if kind == "open":
        L.perp_open(j, ms=ms, ref=ref, qty=qty, price=MARK, fee_usd=0.1,
                    fee_status="confirmed")
    else:
        L.perp_close(j, ms=ms, ref=ref, qty=qty, price=MARK, entry_price=MARK,
                     fee_usd=0.1, fee_status="confirmed")


class TestLifecyclesComeFromTheJournalNotFromSpotOrVenue:
    def test_partial_entries_and_multiple_fills_form_one_lifecycle(self):
        j = L.Journal()
        fill(j, "open", T0, "a", 0.2)
        fill(j, "open", T0 + H, "b", 0.1)
        fill(j, "open", T0 + 2 * H, "c", 0.2)
        lcs = L.lifecycles(j)
        assert len(lcs) == 1 and lcs[0].moves == 3 and lcs[0].end_ms is None

    def test_partial_reduction_keeps_it_open_and_flat_closes_it(self):
        j = L.Journal()
        fill(j, "open", T0, "a", 0.5)
        fill(j, "close", T0 + H, "b", 0.2)
        assert L.lifecycles(j)[0].end_ms is None
        fill(j, "close", T0 + 2 * H, "c", 0.3)
        lc = L.lifecycles(j)[0]
        assert lc.end_ms == T0 + 2 * H and lc.moves == 3

    def test_a_reopened_position_is_a_separate_lifecycle(self):
        j = L.Journal()
        fill(j, "open", T0, "a", 0.5)
        fill(j, "close", T0 + H, "b", 0.5)
        fill(j, "open", T0 + 5 * H, "c", 0.3)
        a, b = L.lifecycles(j)
        assert a.end_ms == T0 + H and b.start_ms == T0 + 5 * H and b.end_ms is None
        assert a.id != b.id

    def test_sub_dust_residue_is_flat(self):
        j = L.Journal()
        fill(j, "open", T0, "a", 0.5)
        fill(j, "close", T0 + H, "b", 0.5 - 1e-10)
        assert L.lifecycles(j)[0].end_ms == T0 + H

    def test_no_perp_exposure_means_no_lifecycle_whatever_the_spot_holds(self):
        j = L.Journal()
        L.opening_balance(j, ms=T0, btc=3.0, price=MARK)
        assert L.lifecycles(j) == []
        assert j.statement()["funding_reconciled"] is True      # vacuous, honest

    def test_lifecycles_are_identical_after_a_restart(self):
        j = hedged()
        fill(j, "close", T0 + H, "z", 0.5)
        again = L.Journal.from_rows(json.loads(json.dumps(j.to_rows())))
        assert L.lifecycles(again) == L.lifecycles(j)

    def test_equal_timestamp_fills_keep_journal_order(self):
        j = L.Journal()
        fill(j, "open", T0, "a", 0.5)
        fill(j, "close", T0, "b", 0.5)
        assert L.lifecycles(j)[0].end_ms == T0


class TestSignedOwnershipAtTheSettlementInstant:
    def book(self, j, **kw):
        return L.book_funding_payments(
            j, [pay(1, 2.0, T0 + 3 * H, **kw)], ms=T0 + 4 * H)

    def test_matching_signed_size_is_attributed(self):
        j = hedged(0.5)
        assert self.book(j, size=-0.5)["booked"] == 1

    def test_positive_size_for_a_short_is_not_ours_unsigned_encoding_fails_closed(self):
        j = hedged(0.5)
        out = self.book(j, size=0.5)
        assert out["unattributed"] == 1 and j.statement()["funding_usd"] == 0.0

    def test_account_exposure_different_from_the_books_is_not_attributed(self):
        j = hedged(0.5)
        assert self.book(j, size=-1.5)["unattributed"] == 1   # a manual trade too

    def test_missing_size_is_not_attributed(self):
        assert self.book(hedged(), size=None)["unattributed"] == 1

    def test_the_tolerance_is_one_percent_of_the_books_quantity(self):
        assert self.book(hedged(0.5), size=-0.5049)["booked"] == 1
        assert self.book(hedged(0.5), size=-0.5051)["unattributed"] == 1

    def test_partial_fill_before_settlement_is_judged_on_the_filled_quantity(self):
        j = L.Journal()
        fill(j, "open", T0, "a", 0.2)
        fill(j, "open", T0 + H, "b", 0.3)              # book is 0.5 by the print
        out = L.book_funding_payments(j, [pay(1, 1.0, T0 + 2 * H, size=-0.5)],
                                      ms=T0 + 3 * H)
        assert out["booked"] == 1
        out = L.book_funding_payments(j, [pay(2, 1.0, T0 + 30 * 60_000,
                                              size=-0.5)], ms=T0 + 3 * H)
        assert out["unattributed"] == 1             # the book held 0.2 then

    def test_a_fill_within_a_minute_of_the_settlement_is_ambiguous(self):
        j = hedged(0.5)
        fill(j, "close", T0 + 3 * H + 30_000, "c", 0.2)
        assert self.book(j, size=-0.5)["unattributed"] == 1


# --------------------------------------------------------------------------
# lifecycle reconciliation outcomes
# --------------------------------------------------------------------------

class TestReconciliationOutcomes:
    def test_a_lifecycle_with_no_record_is_not_confirmed(self):
        j = hedged()
        st = j.statement()
        assert st["funding_confirmed"] is False
        assert st["funding_lifecycles"][0]["state"] == "NOT_RECONCILED"

    def test_no_settlement_rows_and_none_expected_is_reconciled_and_says_so(self):
        j = hedged()
        recon(j, [], [], now=T0 + 3 * H)
        lc = state(j)[0]
        assert lc["state"] == "FULLY_RECONCILED"
        assert lc["evidence"]["no_settlement_rows_to_observe"] is True
        assert lc["evidence"]["expected_settlements"] == 0

    def test_a_held_settlement_with_its_row_is_fully_reconciled(self):
        j = hedged()
        recon(j, [T0 + 3 * H], [pay(1, 2.0, T0 + 3 * H)], now=T0 + 12 * H)
        lc = state(j)[0]
        assert lc["state"] == "FULLY_RECONCILED"
        assert lc["evidence"]["matched_settlements"] == 1
        assert j.statement()["funding_confirmed"] is True

    def test_an_expected_settlement_with_no_row_is_provisional_and_stays_so(self):
        j = hedged()
        recon(j, [T0 + 3 * H], [], now=T0 + 12 * H)
        lc = state(j)[0]
        assert lc["state"] == "PROVISIONAL"
        assert "EXPECTED_SETTLEMENT_MISSING" in lc["reasons"]
        # the row turns up later: the gap stays on record, nothing is promoted
        recon(j, [T0 + 3 * H], [pay(1, 2.0, T0 + 3 * H)], now=T0 + 20 * H)
        assert "EXPECTED_SETTLEMENT_MISSING" in state(j)[0]["reasons"]
        assert j.statement()["funding_confirmed"] is False

    def test_a_row_for_the_wrong_exposure_is_unattributed_so_provisional(self):
        j = hedged()
        recon(j, [T0 + 3 * H], [pay(1, 2.0, T0 + 3 * H, size=-9.0)],
              now=T0 + 12 * H)
        assert "UNATTRIBUTED_SETTLEMENT_ROWS" in state(j)[0]["reasons"]
        assert j.statement()["funding_usd"] == 0.0

    def test_two_prints_cannot_both_be_satisfied_by_one_row(self):
        j = hedged()
        recon(j, [T0 + 3 * H, T0 + 3 * H + 60_000],
              [pay(1, 2.0, T0 + 3 * H)], now=T0 + 12 * H)
        assert "EXPECTED_SETTLEMENT_MISSING" in state(j)[0]["reasons"]

    def test_an_account_row_with_no_public_print_is_inconsistent_coverage(self):
        j = hedged()
        recon(j, [], [pay(1, 2.0, T0 + 3 * H)], now=T0 + 12 * H)
        assert "ACCOUNT_SETTLEMENT_WITHOUT_PUBLIC_HISTORY" in state(j)[0]["reasons"]

    def test_an_identity_conflict_is_visible_and_provisional(self):
        j = hedged()
        recon(j, [T0 + 3 * H, T0 + 11 * H],
              [pay(1, 2.0, T0 + 3 * H), pay(1, 3.0, T0 + 11 * H)],
              now=T0 + 20 * H)
        assert "IDENTITY_CONFLICT" in state(j)[0]["reasons"]
        assert j.statement()["funding_usd"] == pytest.approx(2.0)

    def test_an_ambiguous_settlement_instant_is_never_claimed(self):
        j = hedged()
        fill(j, "close", T0 + 3 * H + 20_000, "c", 0.1)
        recon(j, [T0 + 3 * H], [], now=T0 + 12 * H)
        assert "OWNERSHIP_AMBIGUOUS_AT_SETTLEMENT" in state(j)[0]["reasons"]

    def test_an_incomplete_account_read_cannot_prove_absence(self):
        j = hedged()
        ev = evid([], T0 - 2 * H, T0 + 12 * H, errors=["window unreadable"])
        recon(j, [], [], now=T0 + 12 * H, evidence=ev)
        lc = state(j)[0]
        assert lc["state"] == "PROVISIONAL"
        assert "ACCOUNT_SETTLEMENT_LOG_INCOMPLETE" in lc["reasons"]

    def test_an_incomplete_history_read_cannot_prove_completeness(self):
        j = hedged()
        h = hist([], T0 - 2 * H, T0 + 12 * H, errors=["request failed"])
        recon(j, [], [], now=T0 + 12 * H, history=h)
        assert "FUNDING_HISTORY_UNREADABLE" in state(j)[0]["reasons"]

    def test_history_with_gaps_is_provisional(self):
        j = hedged()
        h = hist([T0 + 3 * H], T0 - 2 * H, T0 + 12 * H,
                 gaps=[(T0, T0 + 9 * H)])
        recon(j, [], [], now=T0 + 12 * H, history=h)
        assert "FUNDING_HISTORY_HAS_GAPS" in state(j)[0]["reasons"]

    def test_reads_that_do_not_cover_the_lifecycle_are_provisional(self):
        j = hedged()
        h = hist([], T0 + 5 * H, T0 + 12 * H)             # starts after the open
        e = evid([], T0 + 5 * H, T0 + 12 * H)
        recon(j, [], [], now=T0 + 12 * H, history=h, evidence=e)
        reasons = state(j)[0]["reasons"]
        assert "FUNDING_HISTORY_DOES_NOT_COVER_LIFECYCLE" in reasons
        assert "ACCOUNT_SETTLEMENT_LOG_DOES_NOT_COVER_LIFECYCLE" in reasons

    def test_unreadable_reads_are_provisional(self):
        j = hedged()
        R.reconcile_funding(j, history=None, evidence=None, now_ms=T0 + 12 * H)
        reasons = state(j)[0]["reasons"]
        assert "FUNDING_HISTORY_NOT_READ" in reasons
        assert "ACCOUNT_SETTLEMENT_LOG_NOT_READ" in reasons
        assert j.statement()["funding_confirmed"] is False

    def test_a_settlement_inside_the_delivery_grace_is_pending_not_missing(self):
        j = hedged()
        recon(j, [T0 + 3 * H], [], now=T0 + 4 * H)
        lc = state(j)[0]
        assert lc["evidence"]["pending_delivery_grace"] == 1
        assert "EXPECTED_SETTLEMENT_MISSING" not in lc["reasons"]

    def test_a_closed_lifecycle_is_not_reconciled_until_its_end_is_judgeable(self):
        j = hedged()
        fill(j, "close", T0 + 5 * H, "c", 0.5)
        recon(j, [], [], now=T0 + 6 * H)
        assert state(j)[0]["state"] == "PENDING_DELIVERY_GRACE"
        assert not state(j)[0]["reconciled"]
        assert "SETTLEMENTS_NOT_YET_JUDGEABLE" in state(j)[0]["reasons"]
        recon(j, [], [], now=T0 + 20 * H)
        assert state(j)[0]["state"] == "FULLY_RECONCILED"

    def test_a_fill_after_the_record_makes_it_stale(self):
        j = hedged()
        recon(j, [], [], now=T0 + 3 * H)
        assert state(j)[0]["reconciled"]
        fill(j, "open", T0 + 4 * H, "more", 0.1)
        s = state(j)[0]
        assert s["state"] == "STALE" and not s["reconciled"]
        assert j.statement()["funding_confirmed"] is False

    def test_one_unreconciled_lifecycle_blocks_confirmation_of_the_book(self):
        j = hedged()
        fill(j, "close", T0 + H, "c", 0.5)
        fill(j, "open", T0 + 30 * H, "d", 0.4)
        recon(j, [], [], now=T0 + 40 * H)
        assert [s["reconciled"] for s in state(j)] == [True, True]
        assert j.statement()["funding_confirmed"] is True
        # the second position grows: its record is now stale, the first stands
        fill(j, "open", T0 + 41 * H, "e", 0.1)
        assert [s["reconciled"] for s in state(j)] == [True, False]
        assert j.statement()["funding_confirmed"] is False

    def test_the_estimate_never_becomes_confirmed_income(self):
        j = hedged()
        recon(j, [], [], now=T0 + 3 * H)
        L.funding(j, ms=T0 + 4 * H, ref="est", usd=50.0)       # engine estimate
        st = j.statement()
        assert st["funding_confirmed"] is False and st["funding_venue_usd"] == 0.0
        assert st["funding_estimated_usd"] == pytest.approx(50.0)


class TestPaymentSignAgreesWithThePublicRate:
    """A short is paid when the public rate is positive. `hist` prints are
    +1 bps, so a booked row of the opposite sign contradicts the venue's own
    public rate: the sign convention is wrong somewhere and the lifecycle must
    not read as reconciled. SYNTHETIC rows; no real account has returned one."""

    def test_a_row_that_agrees_with_the_public_rate_reconciles(self):
        j = hedged()
        recon(j, [T0 + 3 * H], [pay(1, 2.0, T0 + 3 * H)], now=T0 + 12 * H)
        assert state(j)[0]["state"] == "FULLY_RECONCILED"
        assert state(j)[0]["evidence"]["rows_contradicting_public_rate_sign"] == 0

    def test_a_row_that_contradicts_it_is_provisional(self):
        j = hedged()
        recon(j, [T0 + 3 * H], [pay(1, -2.0, T0 + 3 * H)], now=T0 + 12 * H)
        lc = state(j)[0]
        assert lc["state"] == "PROVISIONAL"
        assert "FUNDING_SIGN_CONTRADICTS_PUBLIC_RATE" in lc["reasons"]
        assert j.statement()["funding_confirmed"] is False

    def test_a_negative_public_rate_with_a_paid_row_agrees(self):
        j = hedged()
        negative = CB.FundingHistory(
            start_ms=T0 - 2 * H, end_ms=T0 + 12 * H,
            prints=[(-1.0, T0 + 3 * H)])
        recon(j, [], [pay(1, -2.0, T0 + 3 * H)], now=T0 + 12 * H,
              history=negative)
        assert state(j)[0]["state"] == "FULLY_RECONCILED"

    def test_an_incomplete_history_does_not_judge_the_sign(self):
        j = hedged()
        recon(j, [T0 + 3 * H], [pay(1, -2.0, T0 + 3 * H)], now=T0 + 12 * H,
              history=hist([T0 + 3 * H], T0 - 2 * H, T0 + 12 * H,
                           errors=["boom"]))
        reasons = state(j)[0]["reasons"]
        assert "FUNDING_SIGN_CONTRADICTS_PUBLIC_RATE" not in reasons


class TestIdempotencyAndPersistence:
    def test_repeating_a_reconciliation_writes_nothing_new(self):
        j = hedged()
        first = recon(j, [T0 + 3 * H], [pay(1, 2.0, T0 + 3 * H)],
                      now=T0 + 12 * H)
        n = len(j.entries)
        second = recon(j, [T0 + 3 * H], [pay(1, 2.0, T0 + 3 * H)],
                       now=T0 + 13 * H)
        assert first and second == [] and len(j.entries) == n
        assert j.statement()["funding_usd"] == pytest.approx(2.0)

    def test_a_replayed_poll_after_restart_does_not_book_twice(self):
        j = hedged()
        rows = [pay(1, 2.0, T0 + 3 * H)]
        recon(j, [T0 + 3 * H], rows, now=T0 + 12 * H)
        reborn = L.Journal.from_rows(json.loads(json.dumps(j.to_rows())))
        out = L.book_funding_payments(reborn, rows, ms=T0 + 14 * H)
        assert out["duplicate"] == 1 and out["booked"] == 0
        assert reborn.statement()["funding_usd"] == pytest.approx(2.0)

    def test_provisional_status_survives_a_restart(self):
        j = hedged()
        recon(j, [T0 + 3 * H], [], now=T0 + 12 * H)
        reborn = L.Journal.from_rows(json.loads(json.dumps(j.to_rows())))
        st = reborn.statement()
        assert st["funding_confirmed"] is False
        assert "EXPECTED_SETTLEMENT_MISSING" in st["funding_lifecycles"][0][
            "reasons"]

    def test_reconciled_status_survives_a_restart_only_with_a_matching_history(self):
        j = hedged()
        recon(j, [], [], now=T0 + 3 * H)
        reborn = L.Journal.from_rows(json.loads(json.dumps(j.to_rows())))
        assert reborn.statement()["funding_confirmed"] is True

    def test_the_record_is_a_zero_dollar_entry_that_balances(self):
        j = hedged()
        before = j.statement()["net_usd"]
        recon(j, [], [], now=T0 + 3 * H)
        rec = [e for e in j.entries if e.kind == L.RECONCILIATION_KIND][0]
        assert all(l.usd == 0.0 for l in rec.lines)
        assert j.statement()["net_usd"] == before

    def test_a_finalised_provisional_lifecycle_stops_being_polled(self):
        j = hedged()
        fill(j, "close", T0 + 5 * H, "c", 0.5)
        late = T0 + 5 * H + L.RECONCILIATION_FINAL_AFTER_MS + 2 * H
        recon(j, [T0 + 3 * H], [], now=late)
        assert R.pending_lifecycles(j) == []
        assert state(j)[0]["state"] == "PROVISIONAL"       # final, not promoted

    def test_reconciled_closed_lifecycles_are_not_polled_again(self):
        j = hedged()
        fill(j, "close", T0 + 5 * H, "c", 0.5)
        recon(j, [], [], now=T0 + 20 * H)
        assert R.pending_lifecycles(j) == []


class TestPersistedRecordShape:
    def test_the_record_carries_the_fields_an_operator_needs(self):
        j = hedged()
        recon(j, [T0 + 3 * H], [pay(1, 2.0, T0 + 3 * H)], now=T0 + 12 * H)
        rec = [e for e in j.entries if e.kind == L.RECONCILIATION_KIND][0].fee_raw
        for key in ("lifecycle_id", "start_ms", "end_ms", "state", "reasons",
                    "as_of_ms", "judged_to_ms", "fingerprint", "evidence"):
            assert key in rec
        ev = rec["evidence"]
        for key in ("history", "account_log", "expected_settlements",
                    "matched_settlements", "missing_expected",
                    "account_rows_attributed", "account_rows_unattributed",
                    "account_rows_identity_conflict", "ambiguous_ownership",
                    "size_semantics"):
            assert key in ev
        assert "unconfirmed" in ev["size_semantics"]
        json.dumps(rec)                                   # JSON-safe


class TestMainRouteUsesTheReconciliation:
    def test_the_deployed_tick_writes_and_reads_the_record(self, tmp_path):
        from test_carry_funding_truth import build, make_bot, modern, tick
        from persistence import StateStore
        venue, journal, engine = build(modern, clock_offset_ms=-2 * H)
        bot = make_bot(venue, journal, engine,
                       StateStore(str(tmp_path / "c.db")))
        for k in (1, 2, 3):
            tick(bot, k)
        kinds = [e.kind for e in journal.entries]
        assert "funding_reconciliation" in kinds
        assert journal.statement()["funding_lifecycles"][0]["reconciled"]
        # unreadable history: the next record is PROVISIONAL, not silence
        def down(*a, **k):
            raise RuntimeError("history down")
        bot.carry.broker.get_funding_history_range = down
        tick(bot, 4)
        assert journal.statement()["funding_confirmed"] is False
        assert "FUNDING_HISTORY_NOT_READ" in journal.statement()[
            "funding_lifecycles"][0]["reasons"]


class TestVenueReadsAreThrottled:
    def test_ticks_inside_the_poll_interval_make_no_further_venue_reads(
            self, tmp_path):
        from test_carry_funding_truth import build, make_bot, modern, tick
        from persistence import StateStore
        venue, journal, engine = build(modern, clock_offset_ms=-2 * H)
        bot = make_bot(venue, journal, engine,
                       StateStore(str(tmp_path / "c.db")))
        for k in (1, 2, 3):
            tick(bot, k)
        reads = len(venue.txlog_calls)
        calls = len(venue.calls)
        for k in (4, 5, 6, 7):
            tick(bot, k, throttled=True)
        assert len(venue.txlog_calls) == reads
        funding_history = [c for c in venue.calls[calls:]
                           if c[1] == "/v5/market/funding/history"]
        assert funding_history == []


class TestReconciledStatusCannotOutliveItsEvidence:
    """Found in review of the merged code: a per-lifecycle 'reconciled' could
    contradict the statement, and an open lifecycle stayed confirmed forever."""

    def test_an_open_lifecycles_record_expires_when_read_with_a_clock(self):
        j = hedged()
        recon(j, [], [], now=T0 + 3 * H)
        assert j.statement()["funding_confirmed"] is True        # no clock: as written
        fresh = j.statement(now_ms=T0 + 4 * H)
        assert fresh["funding_confirmed"] is True
        old = j.statement(now_ms=T0 + 3 * H + L.RECORD_MAX_AGE_MS + 1)
        lc = old["funding_lifecycles"][0]
        assert old["funding_confirmed"] is False
        assert lc["state"] == "STALE_AS_OF"
        assert lc["reasons"] == ["RECORD_OLDER_THAN_MAX_AGE"]

    def test_the_bot_refreshes_an_unchanged_open_record_before_it_ages_out(self):
        j = hedged()
        recon(j, [], [], now=T0 + 3 * H)
        n = len(j.entries)
        recon(j, [], [], now=T0 + 4 * H)                  # inside a third of the max age
        assert len(j.entries) == n
        recon(j, [], [], now=T0 + 3 * H + L.RECORD_MAX_AGE_MS // 3 + 1)
        assert len(j.entries) == n + 1
        st = j.statement(now_ms=T0 + 3 * H + L.RECORD_MAX_AGE_MS // 3 + 2)
        assert st["funding_confirmed"] is True

    def test_a_closed_reconciled_lifecycle_does_not_expire(self):
        j = hedged()
        fill(j, "close", T0 + 5 * H, "c", 0.5)
        recon(j, [], [], now=T0 + 20 * H)
        assert j.statement(now_ms=T0 + 900 * 86_400_000)["funding_confirmed"]

    def test_contradicting_evidence_after_reconciliation_downgrades_it_and_keeps_the_trail(
            self):
        j = hedged()
        fill(j, "close", T0 + 5 * H, "c", 0.5)
        recon(j, [], [], now=T0 + 20 * H)
        assert state(j)[0]["reconciled"]
        L.book_funding_payments(
            j, [pay("X", 2.0, T0 + 3 * H), pay("X", 3.0, T0 + 3 * H + 1000)],
            ms=T0 + 21 * H)
        lc = state(j)[0]
        assert lc["state"] == "CONTRADICTED_AFTER_RECONCILIATION"
        assert not lc["reconciled"] and "IDENTITY_CONFLICT" in lc["reasons"]
        # it re-enters reconciliation and a new PROVISIONAL record is appended
        assert R.pending_lifecycles(j)
        recon(j, [T0 + 3 * H], [], now=T0 + 22 * H, book=False)
        records = [e.fee_raw["state"] for e in j.entries
                   if e.kind == L.RECONCILIATION_KIND]
        assert records[0] == "FULLY_RECONCILED" and records[-1] == "PROVISIONAL"
        assert len(records) >= 2                      # the earlier state is not erased

    def test_the_lifecycle_view_and_the_statement_cannot_disagree_on_an_unread_tail(
            self):
        j = hedged()
        fill(j, "close", T0 + 5 * H, "c", 0.5)
        L.mark_funding_unread(j, ms=T0 + 6 * H, why="down")     # inside the tail
        recon(j, [], [], now=T0 + 20 * H)
        st = j.statement()
        assert st["funding_confirmed"] is False
        assert "FUNDING_UNREADABLE_WHILE_OPEN" in st["funding_lifecycles"][0][
            "reasons"]
        assert not st["funding_lifecycles"][0]["reconciled"]

    def test_a_later_outage_does_not_taint_an_earlier_finished_lifecycle(self):
        j = hedged()
        fill(j, "close", T0 + 5 * H, "c", 0.5)
        recon(j, [], [], now=T0 + 20 * H)
        L.mark_funding_unread(j, ms=T0 + 400 * H, why="a later outage")
        assert state(j)[0]["reconciled"]              # lifecycle-scoped
        assert j.statement()["funding_confirmed"] is False   # the book is not

    def test_a_record_that_claims_whole_while_listing_reasons_is_not_trusted(self):
        j = hedged()
        recon(j, [], [], now=T0 + 3 * H)
        rows = j.to_rows()
        for r in rows:
            if r["kind"] == L.RECONCILIATION_KIND:
                r["fee_raw"]["reasons"] = ["EXPECTED_SETTLEMENT_MISSING"]
        st = L.Journal.from_rows(rows).statement()
        assert st["funding_lifecycles"][0]["state"] == "INCONSISTENT_RECORD"
        assert st["funding_confirmed"] is False

    def test_the_report_reads_with_a_clock(self, tmp_path):
        import time
        from persistence import StateStore
        from tools import carry_report
        now = int(time.time() * 1000)
        j = hedged(at=now - 3 * 86_400_000)          # three days ago, never refreshed
        recon(j, [], [], now=now - 3 * 86_400_000 + 3 * H,
              start=now - 3 * 86_400_000 - 2 * H)
        db = str(tmp_path / "r.db")
        s = StateStore(db)
        s.save_ledger(j.to_rows())
        s.close()
        rep = carry_report.build_report(db)["carry"]
        assert rep["status"] == "PROVISIONAL_FUNDING_UNVERIFIED"
        assert rep["funding_lifecycles"][0]["state"] == "STALE_AS_OF"


class TestBoundariesAndCorruption:
    """A settlement exactly on a fill, and a journal that cannot be trusted."""

    @pytest.mark.parametrize("offset,claimed", [
        (0, False), (-L.ATTRIBUTION_SLACK_MS + 1, False),
        (L.ATTRIBUTION_SLACK_MS, False),
        # A fill is stamped when it was BOOKED, which is never before it
        # happened. One stamped exactly a slack BEFORE the settlement therefore
        # happened at or before that instant, and the position is the same
        # through the whole window; one stamped a slack AFTER may have happened
        # earlier, so it is not claimed.
        (-L.ATTRIBUTION_SLACK_MS, True), (-L.ATTRIBUTION_SLACK_MS - 1, True),
        (L.ATTRIBUTION_SLACK_MS + 1, True)])
    def test_a_settlement_within_the_slack_of_a_fill_is_never_claimed(
            self, offset, claimed):
        j = hedged(0.5)
        t = T0 + 5 * H
        fill(j, "open", t + offset, "add", 0.1)
        # the size the venue would report on whichever side of the add it fell
        size = -0.5 if t < t + offset else -0.6
        out = L.book_funding_payments(j, [pay(1, 1.0, t, size=size)], ms=t + 2 * H)
        assert (out["booked"] == 1) is claimed, (offset, out)

    def test_out_of_order_or_corrupt_persisted_rows_are_refused_not_repaired(self):
        j = hedged()
        fill(j, "close", T0 + H, "c", 0.5)
        rows = j.to_rows()
        with pytest.raises(L.LedgerError):
            L.Journal.from_rows(list(reversed(rows)))
        broken = json.loads(json.dumps(rows))
        broken[0]["lines"][0]["usd"] += 5.0                  # no longer balances
        with pytest.raises(L.LedgerError):
            L.Journal.from_rows(broken)

    def test_spot_inventory_alone_never_creates_a_lifecycle_or_a_claim(self):
        j = L.Journal()
        L.opening_balance(j, ms=T0, btc=5.0, price=MARK)
        out = L.book_funding_payments(j, [pay(1, 9.0, T0 + H, size=-5.0)],
                                      ms=T0 + 2 * H)
        assert out["unattributed"] == 1 and L.lifecycles(j) == []

    def test_a_restart_does_not_move_a_lifecycles_start(self):
        j = hedged()
        fill(j, "open", T0 + 2 * H, "b", 0.1)
        before = L.lifecycles(j)[0]
        again = L.lifecycles(L.Journal.from_rows(
            json.loads(json.dumps(j.to_rows()))))[0]
        assert again.start_ms == before.start_ms == T0
        assert again.id == before.id
