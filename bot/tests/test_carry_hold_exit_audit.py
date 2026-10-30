"""Holding and exit logic: defects found by the carry audit, failing first.

DEFECT 1 — a stale print re-booked on every tick while HELD
-----------------------------------------------------------
`CarryEngine.on_candle` asks `_drop_history_the_clock_disproves` to discard a
funding history whose newest stamp is older than MAX_PRINT_GAP_MS. After the
drop it re-asked `is_new()`, and with the watermark cleared the SAME stale
settlement read as new: it was appended to the EWMA, booked into
`funding_collected` and moved the negative streak - on every tick, so three
ticks carrying one negative settlement exited FUNDING_INVERTED on one print,
and a positive one was booked sixty times an hour (the 0034 bug, by another
door). The re-ask exists for a watermark stamped in the FUTURE (a corrupt
ledger), where the real print is older than the watermark and must count;
for a STALE watermark nothing older than it can ever be new.

`main.tick` reaches it only past `MarketSnapshot.assert_fresh`, which refuses
a print more than one interval old, so production is guarded upstream; the
engine's own invariant (one settlement is booked once) did not hold. The
replay and any other caller of `on_candle` relied on that upstream guard.

Offline and synthetic: no venue, every number invented.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import test_carry_overlay as ov  # noqa: E402
from carry_engine import BookState, CarryEngine  # noqa: E402

H = 3_600_000
H8 = 8 * H


def held_engine():
    venue = ov.Venue()
    e = ov.engine(venue)
    opened = ov.open_pair(e)          # print 3 at 3 * H8, tick clock 0
    assert opened.action == "opened", opened
    return e, venue


def test_a_stale_negative_print_is_not_counted_once_per_tick():
    e, venue = held_engine()
    stamp = 4 * H8
    first = e.on_candle(mark=ov.MARK, funding_bps=-1.0, spot=ov.MARK,
                        timestamp_ms=stamp + 60_000, funding_print_ms=stamp)
    assert first.action == "hold", first
    assert e.position.negative_funding_streak == 1
    booked = e.position.funding_collected
    # the feed keeps handing back the same settlement, long after it is stale
    for k in range(1, 6):
        now = stamp + CarryEngine.MAX_PRINT_GAP_MS + k * 60_000
        d = e.on_candle(mark=ov.MARK, funding_bps=-1.0, spot=ov.MARK,
                        timestamp_ms=now, funding_print_ms=stamp)
        assert d.reason != "FUNDING_INVERTED", (k, d)
        assert e.position is not None
    assert e.position.negative_funding_streak == 1
    assert e.position.funding_collected == booked
    assert [o[:2] for o in venue.orders] == [("Sell", "linear")]
    assert e.state is BookState.HEDGED


def test_a_stale_positive_print_is_booked_once():
    e, _ = held_engine()
    stamp = 4 * H8
    e.on_candle(mark=ov.MARK, funding_bps=2.0, spot=ov.MARK,
                timestamp_ms=stamp + 60_000, funding_print_ms=stamp)
    booked = e.position.funding_collected
    assert booked > 0
    for k in range(1, 4):
        e.on_candle(mark=ov.MARK, funding_bps=2.0, spot=ov.MARK,
                    timestamp_ms=stamp + CarryEngine.MAX_PRINT_GAP_MS
                    + k * 60_000, funding_print_ms=stamp)
    assert e.position.funding_collected == booked


def test_the_next_real_print_after_the_gap_still_counts():
    e, _ = held_engine()
    stamp = 4 * H8
    e.on_candle(mark=ov.MARK, funding_bps=-1.0, spot=ov.MARK,
                timestamp_ms=stamp + 60_000, funding_print_ms=stamp)
    e.on_candle(mark=ov.MARK, funding_bps=-1.0, spot=ov.MARK,
                timestamp_ms=stamp + CarryEngine.MAX_PRINT_GAP_MS + 60_000,
                funding_print_ms=stamp)
    later = stamp + 2 * H8
    e.on_candle(mark=ov.MARK, funding_bps=-1.0, spot=ov.MARK,
                timestamp_ms=later + 60_000, funding_print_ms=later)
    assert e.position.negative_funding_streak == 2
    assert e._last_print_ms == later
