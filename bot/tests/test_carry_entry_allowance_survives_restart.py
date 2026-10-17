"""The day's entry allowance is persisted state, and unprovable state fails closed.

`CarryRisk` counted landed and broken pairs in memory, so a restart reset the
count and the book could pay a second round trip the same UTC day. Persisting it
created the opposite hole: a ledger with no (or corrupt) allowance record read
as "nothing spent". Rules pinned here:

* a first-ever run (no ledger) keeps its full allowance;
* an EXISTING ledger whose `entry_days` is missing, mistyped, missing a bucket
  or holding an invalid date spends the rest of the UTC day, and that fact is
  written through the real persistence path so a second restart keeps it;
* a valid record survives a restart, loads idempotently, and renews next day.

Offline and synthetic. The store used is the real `StateStore` on a temp file.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from carry_engine import CarryEngine  # noqa: E402
from carry_risk import CarryRisk  # noqa: E402
from market_snapshot import MarketSnapshot  # noqa: E402
from persistence import PersistenceError, StateStore  # noqa: E402

NOON = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc)
TOMORROW = NOON + dt.timedelta(days=1)
LIMIT = "ENTRY_LIMIT_REACHED_TODAY"


class Venue:
    pass


def engine(persist=None, now=NOON):
    e = CarryEngine(broker=Venue(), max_notional_usd=100.0, borrow_apr=0.0,
                    execution_mode="overlay",
                    persist=persist or (lambda s: None))
    e.pair_risk = CarryRisk(max_notional_usd=100.0)
    e._utc_now = lambda: now
    return e


def gate(e, now):
    snap = MarketSnapshot(perp_mark=100_000.0, spot_mark=100_000.0,
                          funding_bps=1.0, margin_multiple=5.0,
                          observed_at_s=time.time())
    return e.pair_risk.gate_open(notional_usd=100.0, snapshot=snap,
                                 has_open_pair=False, now=now)


def flat_cold_start(e, ledger):
    return e.cold_start(ledger=ledger, venue_perp_qty=0.0, venue_spot_qty=0.0,
                        open_orders=[])


def flat_ledger(**extra):
    ledger = {"book_state": "FLAT", "position": None, "perp_symbol": "BTCUSDT"}
    ledger.update(extra)
    return ledger


# -- fresh installation versus existing ledger --------------------------------

def test_a_first_ever_run_keeps_its_full_allowance():
    e = engine()
    assert flat_cold_start(e, None).action.name == "CLEAN"
    assert gate(e, NOON).ok


def test_an_existing_ledger_with_no_allowance_record_spends_today():
    e = engine()
    assert flat_cold_start(e, flat_ledger()).action.name == "CLEAN"
    d = gate(e, NOON)
    assert not d and d.reason == LIMIT


def test_the_fail_closed_day_ends_at_the_utc_day_boundary():
    e = engine()
    flat_cold_start(e, flat_ledger())
    assert gate(e, TOMORROW).ok


def test_an_existing_empty_carry_payload_is_corruption_not_a_fresh_install(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.save_carry_position(flat_ledger())
    store._exec("UPDATE carry_position SET payload = '' WHERE id = 1")
    with pytest.raises(PersistenceError, match="payload is empty"):
        store.load_carry_position()


# -- missing and malformed records --------------------------------------------

BAD_RECORDS = [
    pytest.param(None, id="missing"),
    pytest.param("2026-10-15", id="string"),
    pytest.param(["2026-10-15"], id="list"),
    pytest.param(7, id="int"),
    pytest.param({}, id="empty"),
    pytest.param({"entries": []}, id="no-broken-bucket"),
    pytest.param({"broken": []}, id="no-entries-bucket"),
    pytest.param({"entries": "x", "broken": []}, id="bucket-not-a-list"),
    pytest.param({"entries": ["not-a-date"], "broken": []}, id="invalid-date"),
    pytest.param({"entries": [20261015], "broken": []}, id="non-string-date"),
    pytest.param({"entries": [None], "broken": []}, id="null-date"),
    pytest.param({"entries": ["2026-02-30"], "broken": []}, id="impossible-date"),
    pytest.param({"entries": ["2026-10-14"], "broken": [3]}, id="bad-in-2nd"),
]


@pytest.mark.parametrize("record", BAD_RECORDS)
def test_an_unreadable_allowance_never_grants_an_entry(record):
    e = engine()
    flat_cold_start(e, flat_ledger(entry_days=record))
    assert gate(e, NOON).reason == LIMIT


@pytest.mark.parametrize("record", BAD_RECORDS)
def test_the_same_holds_when_a_held_pair_is_resumed(record):
    """RESUME goes through `restore`, not the CLEAN branch."""
    e = engine()
    e.restore(flat_ledger(entry_days=record))
    assert gate(e, NOON).reason == LIMIT


def test_a_partly_bad_record_does_not_load_its_good_half():
    e = engine()
    flat_cold_start(e, flat_ledger(entry_days={
        "entries": ["2026-10-01"], "broken": ["nope"]}))
    assert gate(e, NOON).reason == LIMIT        # today is spent
    assert e.pair_risk.export_days()["entries"] == ["2026-10-15"]


def test_valid_historical_days_are_preserved_alongside_the_fail_closed_day():
    e = engine()
    flat_cold_start(e, flat_ledger(entry_days={"entries": ["2026-10-01"]}))
    # (incomplete record: today spent; the unreadable record loaded nothing)
    assert gate(e, TOMORROW).ok


# -- valid state survives a restart -------------------------------------------

def restarted_from(old, now=NOON):
    new = engine(now=now)
    new.restore(old.to_state())
    return new


def test_a_landed_entry_still_counts_after_a_restart():
    a = engine()
    a.pair_risk.record_entry(now=NOON)
    d = gate(restarted_from(a), NOON)
    assert not d and d.reason == LIMIT


def test_a_broken_pair_also_still_counts_after_a_restart():
    a = engine()
    a.pair_risk.record_broken_pair(now=NOON)
    assert gate(restarted_from(a), NOON).reason == LIMIT


def test_a_valid_record_renews_on_the_next_utc_day():
    a = engine()
    a.pair_risk.record_entry(now=NOON)
    assert gate(restarted_from(a), TOMORROW).ok


def test_a_valid_record_with_empty_buckets_leaves_the_allowance_open():
    e = engine()
    flat_cold_start(e, flat_ledger(entry_days={"entries": [], "broken": []}))
    assert gate(e, NOON).ok


def test_loading_twice_does_not_double_count():
    a = engine()
    a.pair_risk.record_entry(now=NOON)
    state = a.to_state()
    b = engine()
    b.restore(state)
    b.restore(state)
    assert b.pair_risk.export_days()["entries"] == ["2026-10-15"]


def test_exhausting_twice_does_not_inflate_the_count():
    e = engine()
    flat_cold_start(e, flat_ledger())
    flat_cold_start(e, flat_ledger())
    assert e.pair_risk.export_days()["entries"] == ["2026-10-15"]


def test_an_engine_without_a_pair_gate_still_serialises():
    e = CarryEngine(broker=Venue(), max_notional_usd=100.0, borrow_apr=0.0,
                    execution_mode="overlay", persist=lambda s: None)
    assert "entry_days" not in e.to_state()


# -- flat cold start after a trade closed today --------------------------------

def test_a_flat_restart_after_a_trade_closed_today_keeps_the_spent_day():
    a = engine()
    a.pair_risk.record_entry(now=NOON)
    ledger = a.to_state()
    ledger["book_state"] = "FLAT"
    b = engine()
    assert flat_cold_start(b, ledger).action.name == "CLEAN"
    assert gate(b, NOON).reason == LIMIT


# -- through the real persistence path ----------------------------------------

def test_the_fail_closed_state_is_written_through_the_real_store(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.save_carry_position(flat_ledger())          # legacy: no entry_days
    e = engine(persist=store.save_carry_position)
    flat_cold_start(e, store.load_carry_position())
    assert gate(e, NOON).reason == LIMIT
    # the marker is now IN THE STORE, in the valid format ...
    saved = store.load_carry_position()
    assert saved["entry_days"] == {"entries": ["2026-10-15"], "broken": []}
    # ... so the next process loads a VALID record and is still blocked today
    again = engine(persist=store.save_carry_position)
    flat_cold_start(again, store.load_carry_position())
    assert gate(again, NOON).reason == LIMIT
    assert gate(again, TOMORROW).ok


def test_a_corrupt_record_in_the_real_store_fails_closed_and_is_repaired(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.save_carry_position(flat_ledger(
        entry_days={"entries": ["garbage"], "broken": []}))
    e = engine(persist=store.save_carry_position)
    flat_cold_start(e, store.load_carry_position())
    assert gate(e, NOON).reason == LIMIT
    assert store.load_carry_position()["entry_days"] == {
        "entries": ["2026-10-15"], "broken": []}


def test_an_unwritable_store_still_blocks_this_process():
    def boom(state):
        raise OSError("disk full")
    e = engine(persist=boom)
    flat_cold_start(e, flat_ledger())
    assert gate(e, NOON).reason == LIMIT


def test_a_first_run_writes_nothing_until_there_is_something_to_record():
    writes = []
    e = engine(persist=writes.append)
    flat_cold_start(e, None)
    assert writes == [] and gate(e, NOON).ok
