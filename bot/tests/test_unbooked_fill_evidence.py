"""A venue that accepted an order whose local booking then failed.

Where it happens: `LedgerBroker.place_market` -> `_book` raises (a ledger
refusal, an unreadable fee, a bug) AFTER `inner.place_market` returned a fill.
The old code raised `BookingFailedAfterFill` (market only; post-only and a
cancel-time increment raised a bare error) and the sink was never told: the only
evidence of the order was an exception string. A fill record the journal does
not hold was also left in `executions`, looking like a measured fill.

Distinct stages, kept apart in the event: the order was SUBMITTED, the venue
RESPONDED (accepted, reported a fill), the booking FAILED. Whether the position
really changed is a separate, independent read (`position_verified` is None
here and is never inferred). The event is never a fill, never an outcome miss
and never a cost sample. No path re-sends an order because accounting failed.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for p in (BOT, os.path.join(BOT, "tools"), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import drill                                                  # noqa: E402
import ledger as L                                            # noqa: E402

QTY = 0.001


class Venue:
    VENUE = "testnet"
    px = 100_000.0

    def __init__(self, *, rest=False):
        self.sent, self.rest, self.cancelled = [], rest, []

    def get_mark(self, s): return self.px
    def get_book_top(self, symbol, product, *, timeout_s=None):
        return {"bid": self.px - 5, "ask": self.px + 5}

    def place_market(self, *, symbol, side, qty, product):
        self.sent.append(("market", side, qty, product))
        return {"filled_qty": qty, "avg_price": self.px,
                "order_link_id": f"M{len(self.sent)}", "fee": 0.2,
                "fee_status": "confirmed"}

    def place_post_only(self, *, symbol, side, qty, price, product):
        self.sent.append(("post_only", side, qty, product))
        return {"filled_qty": 0.0 if self.rest else qty, "avg_price": price,
                "order_link_id": f"P{len(self.sent)}", "fee": 0.2,
                "fee_status": "confirmed", "resting": self.rest}

    def cancel_order(self, *, symbol, order_link_id, product):
        self.cancelled.append(order_link_id)
        return {"filled_qty": QTY, "avg_price": self.px, "resting": False,
                "status": "Cancelled", "updated_ms": 1_700_000_000_000}


def broker(venue, sink=None):
    return L.LedgerBroker(venue, L.Journal(), ms=lambda: 1_700_000_000_000,
                          record_sink=sink)


@pytest.fixture()
def failing_booking(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ledger refused the entry")
    for name in ("perp_open", "perp_close", "spot_buy", "spot_sell"):
        monkeypatch.setattr(L, name, boom)


def sell(b):
    return b.place_market(symbol="BTCUSDT", side="Sell", qty=QTY,
                          product="linear")


# -- the event ----------------------------------------------------------------------

def test_a_market_booking_failure_emits_an_unbooked_event_and_no_fill(failing_booking):
    got, v = [], Venue()
    b = broker(v, got.append)
    with pytest.raises(L.BookingFailedAfterFill):
        sell(b)
    assert [e["type"] for e in got] == ["unbooked"]
    rec = got[0]["execution"]
    assert rec["status"] == "UNBOOKED" and rec["booking"] == "FAILED"
    life = rec["lifecycle"]
    assert life["order_submitted"] is True
    assert life["venue_response_received"] is True
    assert life["reported_fill"] is True
    assert life["locally_booked"] is False
    assert life["position_verified"] is None          # never inferred here
    assert "ledger refused" in life["booking_error"]
    assert rec["reported_filled_qty"] == QTY and rec["order_link_id"] == "M1"
    assert "filled_qty" not in rec and "reference_basis" not in rec
    assert len(v.sent) == 1                           # no second order


def test_no_fill_looking_record_is_left_behind_for_an_unbooked_order(failing_booking):
    b = broker(Venue())
    with pytest.raises(L.BookingFailedAfterFill):
        sell(b)
    assert [r.get("status") for r in b.executions] == ["UNBOOKED"]
    assert b.journal.entries == []                    # nothing was booked


def test_a_post_only_booking_failure_is_wrapped_and_recorded(failing_booking):
    got, v = [], Venue()
    b = broker(v, got.append)
    with pytest.raises(L.BookingFailedAfterFill):
        b.place_post_only(symbol="BTCUSDT", side="Sell", qty=QTY, price=100_000.0,
                          product="linear")
    assert [e["type"] for e in got] == ["unbooked"]
    assert got[0]["execution"]["order_type"] == "post_only"
    assert len(v.sent) == 1


def test_a_cancel_time_increment_booking_failure_is_wrapped_and_recorded(
        monkeypatch):
    got, v = [], Venue(rest=True)
    b = broker(v, got.append)
    b.place_post_only(symbol="BTCUSDT", side="Sell", qty=QTY, price=100_000.0,
                      product="linear")                  # rests, books nothing
    got.clear()

    def boom(*a, **k):
        raise RuntimeError("ledger refused the increment")
    monkeypatch.setattr(L, "perp_open", boom)
    with pytest.raises(L.BookingFailedAfterFill):
        b.cancel_order(symbol="BTCUSDT", order_link_id="P1", product="linear")
    assert [e["type"] for e in got] == ["unbooked"]
    assert len(v.sent) == 1 and v.cancelled == ["P1"]


def test_a_sink_failure_while_recording_the_unbooked_event_changes_nothing(
        failing_booking):
    def bad_sink(_e):
        raise OSError("disk full")
    b = broker(Venue(), bad_sink)
    with pytest.raises(L.BookingFailedAfterFill):     # still this, not OSError
        sell(b)
    assert b.sink_errors == 1


# -- no path re-sends an order because accounting failed ----------------------------

def test_the_engine_halts_and_sends_exactly_one_order(failing_booking):
    from carry_engine import BookState
    from test_carry_cold_start import H8, MARK, Venue as CV, engine
    v = CV(inventory=5.0)
    e = engine(L.LedgerBroker(v, L.Journal(), ms=lambda: 1_000), mode="overlay")
    for k in (3, 4, 5, 6):
        e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                    funding_print_ms=k * H8, timestamp_ms=k * H8)
    assert e.state is BookState.HALTED
    assert len(v.orders) == 1                         # the one that was booked-failed


# -- the file, the flush and the reader ---------------------------------------------

def test_the_writer_stores_it_under_its_own_kind_once(tmp_path, failing_booking):
    path = str(tmp_path / "r.jsonl")
    written = set()
    b = broker(Venue(), drill.RecordWriter(path, "testnet", written))
    with pytest.raises(L.BookingFailedAfterFill):
        sell(b)
    lines = [json.loads(x) for x in open(path).read().splitlines()]
    assert [x["kind"] for x in lines] == [drill.UNBOOKED_KIND]
    # the end-of-run flush finds it already written
    assert drill.append_execution_records(path, b.journal, "testnet", written,
                                          outcomes=b.executions) == 0
    assert len(open(path).read().splitlines()) == 1


def test_the_flush_alone_stores_it_under_its_own_kind(tmp_path, failing_booking):
    path = str(tmp_path / "r.jsonl")
    b = broker(Venue())
    with pytest.raises(L.BookingFailedAfterFill):
        sell(b)
    assert drill.append_execution_records(path, b.journal, "testnet", set(),
                                          outcomes=b.executions) == 1
    assert json.loads(open(path).read())["kind"] == drill.UNBOOKED_KIND


def test_the_report_counts_it_as_unbooked_not_as_a_miss_or_a_sample(
        tmp_path, failing_booking):
    import execution_samples_report as esr
    path = str(tmp_path / "r.jsonl")
    b = broker(Venue(), drill.RecordWriter(path, "testnet", set()))
    with pytest.raises(L.BookingFailedAfterFill):
        sell(b)
    out = esr.build("", mode="overlay", notional_usd=600.0, perp="BTCUSDT",
                    spot="BTCUSDT", now_ms=1_700_000_100_000, records=[path],
                    environment="testnet")
    assert out["legs"][0]["n"] == 0 and out["legs"][1]["n"] == 0
    ub = out["unbooked_orders"]
    assert ub["total"] == 1 and ub["counted_as_samples"] == 0
    assert ub["by_leg"]["linear/Sell/market"] == 1
    assert "UNCERTAIN" in ub["note"].upper()
    assert out["outcomes"]["miss_rate"] == {}         # not an unfilled miss
