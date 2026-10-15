"""The day's entry allowance is state, not process memory.

`CarryRisk` counted landed and broken pairs in lists that died with the
process. A crash or restart after an entry (Fly restarts on failure) reset the
count to zero, so the book could pay a second round trip the same UTC day -
the thrash the allowance exists to prevent. Offline and synthetic.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from carry_engine import CarryEngine  # noqa: E402
from carry_risk import CarryRisk  # noqa: E402
from market_snapshot import MarketSnapshot  # noqa: E402

NOON = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc)
TOMORROW = NOON + dt.timedelta(days=1)


class Venue:
    pass


def engine():
    e = CarryEngine(broker=Venue(), max_notional_usd=100.0, borrow_apr=0.0,
                    execution_mode="overlay", persist=lambda s: None)
    e.pair_risk = CarryRisk(max_notional_usd=100.0)
    return e


def gate(e, now):
    snap = MarketSnapshot(perp_mark=100_000.0, spot_mark=100_000.0,
                          funding_bps=1.0, margin_multiple=5.0,
                          observed_at_s=time.time())
    return e.pair_risk.gate_open(notional_usd=100.0, snapshot=snap,
                                 has_open_pair=False, now=now)


def restarted_from(old):
    new = engine()
    new.restore(old.to_state())
    return new


def test_a_landed_entry_still_counts_after_a_restart():
    a = engine()
    a.pair_risk.record_entry(now=NOON)
    assert gate(a, NOON).reason == "ENTRY_LIMIT_REACHED_TODAY"
    b = restarted_from(a)
    d = gate(b, NOON)
    assert not d and d.reason == "ENTRY_LIMIT_REACHED_TODAY"


def test_a_broken_pair_also_still_counts_after_a_restart():
    a = engine()
    a.pair_risk.record_broken_pair(now=NOON)
    b = restarted_from(a)
    assert gate(b, NOON).reason == "ENTRY_LIMIT_REACHED_TODAY"


def test_the_allowance_renews_on_the_next_utc_day():
    a = engine()
    a.pair_risk.record_entry(now=NOON)
    b = restarted_from(a)
    assert gate(b, TOMORROW).ok


def test_state_with_no_record_or_garbage_changes_nothing():
    for junk in (None, {}, {"entries": ["not-a-date", 7], "broken": "x"}):
        b = engine()
        state = {"position": None, "perp_symbol": "BTCUSDT"}
        if junk is not None:
            state["entry_days"] = junk
        b.restore(state)
        assert gate(b, NOON).ok


def test_an_engine_without_a_pair_gate_still_serialises():
    e = CarryEngine(broker=Venue(), max_notional_usd=100.0, borrow_apr=0.0,
                    execution_mode="overlay", persist=lambda s: None)
    assert "entry_days" not in e.to_state()


def test_a_flat_restart_through_cold_start_keeps_the_spent_day():
    """The case that matters: opened and closed earlier today, flat now. Cold
    start is CLEAN (nothing to resume) yet the allowance must still load."""
    a = engine()
    a.pair_risk.record_entry(now=NOON)
    ledger = a.to_state()                       # what the store persisted
    ledger["book_state"] = "FLAT"
    b = engine()
    d = b.cold_start(ledger=ledger, venue_perp_qty=0.0, venue_spot_qty=0.0,
                     open_orders=[])
    assert d.action.name == "CLEAN"
    assert gate(b, NOON).reason == "ENTRY_LIMIT_REACHED_TODAY"
