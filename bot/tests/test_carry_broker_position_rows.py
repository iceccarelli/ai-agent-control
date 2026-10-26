"""Which position row does the book judge when the venue lists more than one?

In hedge mode Bybit lists a row per direction for a symbol, FLAT ones included,
in either order. The margin read, the liquidation view and the held-size read
used to take `rows[0]` only: a comfortable flat slot listed first made a short
that was 1% from liquidation read as safe, and a flat slot with zero maintenance
margin made the margin read raise on a position that was perfectly readable
(a halt does not close the short). The rows below are SYNTHETIC, written from
the documented position/list shape; whether the live testnet lists the flat slot
first was not observed.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from carry_broker import LINEAR, PairIncident  # noqa: E402
from test_carry_broker import FakeClient, broker, envelope  # noqa: E402

MARK = 100_000.0


def flat_long(**kw):
    row = {"positionIdx": 1, "side": "", "size": "0", "positionIM": "50",
           "positionMM": "10", "liqPrice": "", "markPrice": str(MARK)}
    row.update(kw)
    return row


def short(**kw):
    row = {"positionIdx": 2, "side": "Sell", "size": "0.001", "positionIM": "40",
           "positionMM": "10", "liqPrice": "101000", "markPrice": str(MARK)}
    row.update(kw)
    return row


def long_(**kw):
    row = {"positionIdx": 1, "side": "Buy", "size": "0.001", "positionIM": "40",
           "positionMM": "10", "liqPrice": "90000", "markPrice": str(MARK)}
    row.update(kw)
    return row


class Venue(FakeClient):
    def __init__(self, rows, **kw):
        super().__init__(**kw)
        self.rows = rows

    def _request(self, method, endpoint, **kw):
        if endpoint == "/v5/position/list":
            self.calls.append((method, endpoint, kw.get("params"), kw.get("body")))
            return envelope({"list": self.rows})
        return super()._request(method, endpoint, **kw)


def orders(*rows):
    """Every order the venue could list them in."""
    return [list(rows), list(reversed(rows))]


class TestTheShortIsJudgedNotTheFirstRow:
    @pytest.mark.parametrize("rows", orders(flat_long(), short()))
    def test_the_margin_is_the_shorts_not_the_flat_slots(self, rows):
        # short: IM 40 / MM 10 = 4.0; the flat slot would read 5.0
        assert broker(Venue(rows)).get_margin_multiple("BTCUSDT") == pytest.approx(4.0)

    @pytest.mark.parametrize("rows", orders(flat_long(positionMM="0", positionIM="0"),
                                            short(positionIM="10", positionMM="9")))
    def test_a_flat_slot_with_zero_margin_does_not_hide_a_readable_short(self, rows):
        assert broker(Venue(rows)).get_margin_multiple("BTCUSDT") == pytest.approx(10 / 9)

    @pytest.mark.parametrize("rows", orders(flat_long(), short()))
    def test_the_liquidation_view_is_the_shorts(self, rows):
        view = broker(Venue(rows)).get_liquidation_view("BTCUSDT")
        assert view["reason"] != "NO_POSITION"
        assert view["size"] == pytest.approx(0.001) and view["side"] == "Sell"
        assert view["distance_pct"] == pytest.approx(1.0)

    @pytest.mark.parametrize("rows", orders(flat_long(), short()))
    def test_the_held_size_is_the_shorts(self, rows):
        assert broker(Venue(rows))._holding("BTCUSDT", LINEAR) == pytest.approx(0.001)


class TestAmbiguityRefuses:
    @pytest.mark.parametrize("rows", orders(long_(), short()))
    def test_a_long_and_a_short_both_held_is_refused_everywhere(self, rows):
        b = broker(Venue(rows))
        with pytest.raises(PairIncident):
            b.get_margin_multiple("BTCUSDT")
        with pytest.raises(PairIncident):
            b.get_liquidation_view("BTCUSDT")
        with pytest.raises(PairIncident):
            b._holding("BTCUSDT", LINEAR)

    def test_two_sized_shorts_are_refused(self):
        rows = [short(), short(positionIdx=0)]
        b = broker(Venue(rows))
        with pytest.raises(PairIncident):
            b.get_margin_multiple("BTCUSDT")
        with pytest.raises(PairIncident):
            b._holding("BTCUSDT", LINEAR)

    def test_a_size_that_is_not_stated_is_refused_not_skipped(self):
        rows = [flat_long(), short(size="")]
        with pytest.raises(PairIncident):
            broker(Venue(rows)).get_margin_multiple("BTCUSDT")


class TestWhatDidNotChange:
    def test_a_single_row_is_read_exactly_as_before(self):
        b = broker(Venue([short()]))
        assert b.get_margin_multiple("BTCUSDT") == pytest.approx(4.0)
        assert b.get_liquidation_view("BTCUSDT")["distance_pct"] == pytest.approx(1.0)
        assert b._holding("BTCUSDT", LINEAR) == pytest.approx(0.001)

    def test_all_rows_flat_is_still_flat(self):
        rows = [flat_long(), flat_long(positionIdx=2)]
        b = broker(Venue(rows))
        assert b.get_liquidation_view("BTCUSDT")["reason"] == "NO_POSITION"
        assert b._holding("BTCUSDT", LINEAR) == 0.0

    def test_an_empty_list_is_still_no_position(self):
        b = broker(Venue([]))
        assert b.get_liquidation_view("BTCUSDT")["reason"] == "NO_POSITION"
        assert b._holding("BTCUSDT", LINEAR) == 0.0
        with pytest.raises(PairIncident):
            b.get_margin_multiple("BTCUSDT")

    def test_a_flat_book_still_has_no_margin_ratio(self):
        with pytest.raises(PairIncident):
            broker(Venue([flat_long(positionMM="0")])).get_margin_multiple("BTCUSDT")
