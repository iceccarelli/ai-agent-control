"""The live overlay journal books the hedged BTC leg.

An overlay never trades the client's coin, so the journal held only the short's
price move: a falling market read as profit and a rising one as loss, and the
evidence adapter (rightly) refused every such cycle. `LedgerBroker` can now mark
the hedged BTC from the spot it SAW at entry to the spot it saw at exit, per
close fill, with the same `revalue_inventory` entry the replay uses.

Honesty rules pinned here: off by default (acquire already realises its spot);
a missing reference on either side books nothing (the cycle stays excluded from
evidence, never guessed); the entry reference survives a restart; and a failure
to mark never touches the fill that was already booked.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import carry_performance as perf  # noqa: E402
import ledger as L  # noqa: E402
from test_carry_cold_start import H8, MARK, Venue, engine  # noqa: E402
from carry_engine import OVERLAY, BookState, ColdStart  # noqa: E402

QTY = 0.001


class PricedVenue(Venue):
    px = MARK

    def place_market(self, *, symbol, side, qty, product):
        out = super().place_market(symbol=symbol, side=side, qty=qty,
                                   product=product)
        out["avg_price"] = self.px
        return out


def revals(journal):
    return [e for e in journal.entries if e.kind == "revalue"]


def broker(journal=None, *, on=True, venue=None):
    v = venue or PricedVenue(inventory=5.0)
    return v, L.LedgerBroker(v, journal or L.Journal(), ms=lambda: 1_000,
                             spot_symbol="BTCUSDT", book_overlay_inventory=on)


def sell(b, qty, px, ref_spot, venue):
    venue.px = px
    b.note_hedge_reference(ref_spot)
    b.place_market(symbol="BTCUSDT", side="Sell", qty=qty, product="linear")


def buy(b, qty, px, ref_spot, venue):
    venue.px = px
    b.note_hedge_reference(ref_spot)
    b.place_market(symbol="BTCUSDT", side="Buy", qty=qty, product="linear")


def test_a_hedged_overlay_cycle_books_both_legs():
    v, b = broker()
    sell(b, QTY, 100_000.0, 100_000.0, v)
    buy(b, QTY, 90_000.0, 90_000.0, v)
    (r,) = revals(b.journal)
    assert r.memo.startswith(f"{QTY} BTC from 100000.0 to 90000.0")
    # short +$10 is CASH; the client's BTC -$10 is a MARK (never sold). The
    # pair is flat to price, and the two are reported apart.
    st = b.journal.statement()
    assert st["realised_usd"] == pytest.approx(10.0)
    assert st["inventory_mark_usd"] == pytest.approx(-10.0)
    assert st["realised_usd"] + st["inventory_mark_usd"] == pytest.approx(0.0)
    assert st["pair_net_usd"] == pytest.approx(st["net_usd"] - 10.0)


def test_the_cycle_is_reported_with_its_inventory_leg_booked():
    v, b = broker()
    sell(b, QTY, 100_000.0, 100_000.0, v)
    buy(b, QTY, 90_000.0, 90_000.0, v)
    cyc = [c for c in perf.cycles_from_journal(b.journal)
           if c.get("closed")]
    assert cyc and cyc[0].get("revalue_entries")


def test_it_is_off_unless_asked():
    v, b = broker(on=False)
    sell(b, QTY, 100_000.0, 100_000.0, v)
    buy(b, QTY, 90_000.0, 90_000.0, v)
    assert revals(b.journal) == []


def test_no_entry_reference_books_nothing():
    v, b = broker()
    sell(b, QTY, 100_000.0, None, v)               # spot unknown at the open
    buy(b, QTY, 90_000.0, 90_000.0, v)
    assert revals(b.journal) == []


def test_no_exit_reference_books_nothing():
    v, b = broker()
    sell(b, QTY, 100_000.0, 100_000.0, v)
    buy(b, QTY, 90_000.0, None, v)
    assert revals(b.journal) == []


def test_a_partial_entry_reference_is_not_averaged_over_a_gap():
    v, b = broker()
    sell(b, QTY, 100_000.0, 100_000.0, v)
    sell(b, QTY, 100_000.0, None, v)               # second add has no reference
    buy(b, 2 * QTY, 90_000.0, 90_000.0, v)
    assert revals(b.journal) == []


def test_partial_closes_mark_only_what_they_close_from_the_weighted_entry():
    v, b = broker()
    sell(b, QTY, 100_000.0, 100_000.0, v)
    sell(b, QTY, 100_000.0, 110_000.0, v)          # weighted entry 105,000
    buy(b, QTY, 90_000.0, 90_000.0, v)
    buy(b, QTY, 80_000.0, 80_000.0, v)
    first, second = revals(b.journal)
    assert first.memo.startswith(f"{QTY} BTC from 105000.0 to 90000.0")
    assert second.memo.startswith(f"{QTY} BTC from 105000.0 to 80000.0")


def test_a_failed_mark_never_unbooks_the_fill():
    v, b = broker()
    sell(b, QTY, 100_000.0, 100_000.0, v)

    def boom(*a, **k):
        raise RuntimeError("mark failed")
    orig = L.revalue_inventory
    L.revalue_inventory = boom
    try:
        buy(b, QTY, 90_000.0, 90_000.0, v)
    finally:
        L.revalue_inventory = orig
    assert any(e.kind == "perp_close" for e in b.journal.entries)
    assert revals(b.journal) == []


def test_the_entry_reference_survives_a_restart():
    v, b = broker()
    sell(b, QTY, 100_000.0, 100_000.0, v)
    rows = json.loads(json.dumps(b.journal.to_rows()))
    j2 = L.Journal.from_rows(rows)
    v2, b2 = broker(j2, venue=PricedVenue(inventory=5.0, perp=QTY))
    b2.resume_basis(perp_qty=QTY, perp_price=100_000.0)
    buy(b2, QTY, 90_000.0, 90_000.0, v2)
    (r,) = revals(j2)
    assert r.memo.startswith(f"{QTY} BTC from 100000.0 to 90000.0")


def test_a_restart_with_no_recorded_reference_books_nothing():
    v, b = broker(on=False)                        # opened by an older build
    sell(b, QTY, 100_000.0, 100_000.0, v)
    j2 = L.Journal.from_rows(json.loads(json.dumps(b.journal.to_rows())))
    v2, b2 = broker(j2, venue=PricedVenue(inventory=5.0, perp=QTY))
    b2.resume_basis(perp_qty=QTY, perp_price=100_000.0)
    buy(b2, QTY, 90_000.0, 90_000.0, v2)
    assert revals(j2) == []


def test_the_real_engine_tells_the_broker_the_spot_it_decided_on():
    v = PricedVenue(inventory=5.0)
    j = L.Journal()
    b = L.LedgerBroker(v, j, ms=lambda: 1_000, spot_symbol="BTCUSDT",
                       book_overlay_inventory=True)
    e = engine(b, mode=OVERLAY)
    d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                    funding_print_ms=3 * H8, timestamp_ms=3 * H8)
    assert d.action == "opened", d.reason
    v.px = 90_000.0
    for k in (4, 5, 6):
        e.on_candle(mark=90_000.0, funding_bps=-0.5, spot=90_000.0,
                    funding_print_ms=k * H8, timestamp_ms=k * H8)
    assert e.state is BookState.FLAT
    (r,) = revals(j)
    assert "to 90000.0" in r.memo and f"from {float(MARK)}" in r.memo


def test_main_enables_it_for_the_overlay_only():
    src = open(os.path.join(os.path.dirname(__file__), "..", "main.py")).read()
    assert "book_overlay_inventory=" in src
    i = src.index("book_overlay_inventory=")
    assert "OVERLAY" in src[i:i + 120] or "overlay" in src[i:i + 120]
