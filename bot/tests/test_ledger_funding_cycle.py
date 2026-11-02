"""A venue funding row belongs to the cycle that held the position at the
SETTLEMENT, not to whichever cycle is current when the row is delivered."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools"))

import ledger as L                       # noqa: E402
import carry_performance as perf         # noqa: E402

H = 3_600_000
T0 = 1_700_000_000_000


def pay(i, ms, usd, size=-1.0):
    return {"id": f"p{i}", "ms": ms, "usd": usd, "currency": "USDT",
            "size": size, "symbol": "BTCUSDT", "sign": "confirmed"}


def two_cycles_with_a_late_row(j):
    L.perp_open(j, ms=T0, ref="a-o", qty=1.0, price=100_000.0, fee_usd=55.0,
                fee_status="confirmed")
    L.perp_close(j, ms=T0 + 9 * H, ref="a-c", qty=1.0, price=100_000.0,
                 entry_price=100_000.0, fee_usd=55.0, fee_status="confirmed")
    L.perp_open(j, ms=T0 + 10 * H, ref="b-o", qty=1.0, price=100_000.0,
                fee_usd=55.0, fee_status="confirmed")
    # the 8h settlement (inside A) is delivered only after B has opened
    return L.book_funding_payments(j, [pay(1, T0 + 8 * H, 12.5)],
                                   ms=T0 + 11 * H)


def test_a_late_delivered_row_is_booked_to_the_cycle_that_held_through_it():
    j = L.Journal()
    out = two_cycles_with_a_late_row(j)
    assert out["booked"] == 1
    a, b = perf.cycles_from_journal(j)
    assert a["funding_usd"] == pytest.approx(12.5)
    assert b["funding_usd"] == pytest.approx(0.0)
    assert a["closed"] and not b["closed"]


def test_the_row_keeps_its_cycle_across_a_reload():
    j = L.Journal()
    two_cycles_with_a_late_row(j)
    again = L.Journal.from_rows(j.to_rows())
    fund = [e for e in again.entries if e.kind == "funding"][0]
    assert fund.cycle == [e for e in again.entries if e.ref == "a-o"][0].cycle


def test_a_row_for_the_open_cycle_is_unchanged():
    j = L.Journal()
    L.perp_open(j, ms=T0, ref="a-o", qty=1.0, price=100_000.0, fee_usd=55.0,
                fee_status="confirmed")
    L.book_funding_payments(j, [pay(1, T0 + 8 * H, 12.5)], ms=T0 + 9 * H)
    (a,) = perf.cycles_from_journal(j)
    assert a["funding_usd"] == pytest.approx(12.5)
