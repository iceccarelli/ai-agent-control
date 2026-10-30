"""One cycle identity from the opening fill to the last booked cost.

A position cycle (flat -> opened -> flat) used to be inferred separately by each
reader. Now the journal stamps every entry with a stable `cycle` id, persists it,
and re-derives the same ids for rows written before the field existed. The
engine names the cycle `cyc-<opened_ms>` before its first leg, so the ledger
entries, the position and the liquidation observations carry one identity, and
it survives a restart. Offline; real Journal, engine, brokers and store.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import ledger as L                                   # noqa: E402
import carry_performance as perf                     # noqa: E402
from persistence import StateStore                   # noqa: E402
from test_carry_fee_truth import H8, MARK, build, modern, open_the_hedge  # noqa: E402

DAY = 86_400_000
T0 = 1_700_000_000_000


def one_cycle(j, day, tag, **kw):
    L.perp_open(j, ms=T0 + day * DAY, ref=f"{tag}-o", qty=1.0, price=100_000.0,
                fee_usd=55.0, fee_status="confirmed")
    L.funding(j, ms=T0 + (day + 1) * DAY, ref=f"{tag}-f", usd=10.0, basis="venue")
    L.borrow(j, ms=T0 + (day + 1) * DAY, ref=f"{tag}-b", usd=1.0)
    L.perp_close(j, ms=T0 + (day + 2) * DAY, ref=f"{tag}-c", qty=1.0,
                 price=100_000.0, entry_price=100_000.0, fee_usd=55.0,
                 fee_status="confirmed")


def test_every_entry_of_a_cycle_carries_the_same_id():
    j = L.Journal()
    L.opening_balance(j, ms=T0 - 1, btc=1.0, price=100_000.0)
    one_cycle(j, 0, "a")
    one_cycle(j, 5, "b")
    ids = [e.cycle for e in j.entries]
    assert ids[0] == ""                                   # the opening balance
    assert len(set(ids[1:5])) == 1 and len(set(ids[5:])) == 1
    assert ids[1] != ids[5] and all(ids[1:])


def test_a_named_cycle_uses_the_given_id():
    j = L.Journal()
    j.begin_cycle("cyc-123")
    one_cycle(j, 0, "a")
    assert {e.cycle for e in j.entries} == {"cyc-123"}


def test_an_unused_name_does_not_leak_into_the_next_cycle_it_was_not_for():
    j = L.Journal()
    j.begin_cycle("cyc-1")
    j.begin_cycle("cyc-2")                       # the first attempt never filled
    one_cycle(j, 0, "a")
    assert {e.cycle for e in j.entries} == {"cyc-2"}


def test_the_id_survives_persistence_and_a_restart_mid_hold():
    j = L.Journal()
    j.begin_cycle("cyc-9")
    L.perp_open(j, ms=T0, ref="o", qty=1.0, price=100_000.0, fee_usd=55.0,
                fee_status="confirmed")
    rows = j.to_rows()
    assert rows[0]["cycle"] == "cyc-9"
    reborn = L.Journal.from_rows(rows)
    L.funding(reborn, ms=T0 + DAY, ref="f", usd=5.0, basis="venue")
    L.perp_close(reborn, ms=T0 + 2 * DAY, ref="c", qty=1.0, price=100_000.0,
                 entry_price=100_000.0, fee_usd=55.0, fee_status="confirmed")
    assert {e.cycle for e in reborn.entries} == {"cyc-9"}


def test_rows_from_before_the_field_get_the_same_ids_a_fresh_replay_would():
    j = L.Journal()
    one_cycle(j, 0, "a")
    one_cycle(j, 5, "b")
    legacy = [{k: v for k, v in r.items() if k != "cycle"} for r in j.to_rows()]
    again = L.Journal.from_rows(legacy)
    assert [e.cycle for e in again.entries] == [e.cycle for e in j.entries]
    assert len({e.cycle for e in again.entries}) == 2


def test_the_performance_report_reads_the_ledgers_cycle_ids():
    j = L.Journal()
    one_cycle(j, 0, "a")
    one_cycle(j, 5, "b")
    cycles = perf.cycles_from_journal(j)
    assert [c["cycle_id"] for c in cycles] == sorted({e.cycle for e in j.entries})
    assert all(c["closed"] for c in cycles)


def test_the_engine_ledger_and_observations_share_one_cycle_identity(tmp_path):
    venue, journal, engine = build(modern)
    store = StateStore(str(tmp_path / "s.db"))
    try:
        engine.require_liquidation_check = True
        engine.risk_observer = store.record_risk_observation
        engine.broker.inner.client.margin_mode = "ISOLATED_MARGIN"
        assert open_the_hedge(engine).action == "opened"
        cycle = f"cyc-{engine.position.opened_ms}"
        assert engine.position.cycle_id == cycle
        fills = [e for e in journal.entries if e.kind in L.FILL_KINDS]
        assert fills and {e.cycle for e in fills} == {cycle}
        for row in store.load_risk_observations():
            assert row["cycle_opened_ms"] == engine.position.opened_ms
    finally:
        store.close()


def test_a_restored_engine_names_the_same_cycle():
    venue, journal, engine = build(modern)
    open_the_hedge(engine)
    from carry_engine import CarryEngine
    reborn = CarryEngine(broker=engine.broker, max_notional_usd=100_000.0,
                         borrow_apr=0.05, execution_mode="acquire",
                         persist=lambda s: None)
    reborn.restore(engine.to_state())
    assert reborn.position.cycle_id == engine.position.cycle_id


# -- collisions, repeats, partial fills, aborted attempts ----------------------------

def test_two_cycles_given_the_same_name_stay_two_cycles():
    j = L.Journal()
    j.begin_cycle("cyc-5")
    one_cycle(j, 0, "a")
    j.begin_cycle("cyc-5")                        # same millisecond, later cycle
    one_cycle(j, 5, "b")
    ids = [e.cycle for e in j.entries]
    assert len(set(ids[:4])) == 1 and len(set(ids[4:])) == 1
    assert ids[0] != ids[4]
    assert ids[0] == "cyc-5" and ids[4] == "cyc-5-2"


def test_collisions_are_resolved_the_same_way_on_a_replay():
    j = L.Journal()
    j.begin_cycle("cyc-5"); one_cycle(j, 0, "a")
    j.begin_cycle("cyc-5"); one_cycle(j, 5, "b")
    legacy = [{k: v for k, v in r.items() if k != "cycle"} for r in j.to_rows()]
    again = L.Journal.from_rows(legacy)
    assert len({e.cycle for e in again.entries}) == 2
    assert [e.cycle for e in L.Journal.from_rows(j.to_rows()).entries] == \
        [e.cycle for e in j.entries]


def test_naming_the_open_cycle_again_does_not_split_it():
    j = L.Journal()
    j.begin_cycle("cyc-7")
    L.perp_open(j, ms=T0, ref="o1", qty=0.5, price=100_000.0, fee_usd=10.0,
                fee_status="confirmed")
    j.begin_cycle("cyc-7")                        # the engine names it again
    L.perp_open(j, ms=T0 + 1, ref="o2", qty=0.5, price=100_000.0, fee_usd=10.0,
                fee_status="confirmed")
    L.funding(j, ms=T0 + DAY, ref="f", usd=3.0, basis="venue")
    L.perp_close(j, ms=T0 + 2 * DAY, ref="c", qty=1.0, price=100_000.0,
                 entry_price=100_000.0, fee_usd=20.0, fee_status="confirmed")
    assert {e.cycle for e in j.entries} == {"cyc-7"}


def test_additional_and_partial_fills_stay_in_one_cycle():
    j = L.Journal()
    L.spot_buy(j, ms=T0, ref="s1", qty=0.4, price=100_000.0, fee_btc=0.0004,
               fee_status="confirmed")
    L.perp_open(j, ms=T0 + 1, ref="o1", qty=0.3996, price=100_000.0, fee_usd=5.0,
                fee_status="confirmed")
    L.spot_buy(j, ms=T0 + 2, ref="s2", qty=0.6, price=100_000.0, fee_btc=0.0006,
               fee_status="confirmed")
    L.perp_open(j, ms=T0 + 3, ref="o2", qty=0.5994, price=100_000.0, fee_usd=5.0,
                fee_status="confirmed")
    L.perp_close(j, ms=T0 + DAY, ref="c1", qty=0.5, price=100_000.0,
                 entry_price=100_000.0, fee_usd=5.0, fee_status="confirmed")
    L.perp_close(j, ms=T0 + DAY + 1, ref="c2", qty=0.4990, price=100_000.0,
                 entry_price=100_000.0, fee_usd=5.0, fee_status="confirmed")
    assert len({e.cycle for e in j.entries}) == 1


def test_a_failed_attempt_is_its_own_closed_cycle_and_its_fees_are_counted():
    """Spot bought, perp never opened, spot sold back: a round trip of fees for
    nothing. It must not stay 'open' forever or vanish from the net."""
    j = L.Journal()
    j.begin_cycle("cyc-10")
    L.spot_buy(j, ms=T0, ref="s", qty=1.0, price=100_000.0, fee_btc=0.001,
               fee_status="confirmed")
    L.spot_sell(j, ms=T0 + 60_000, ref="ss", qty=0.999, price=100_000.0,
                cost_basis=100_000.0, fee_usd=55.0, fee_status="confirmed")
    j.begin_cycle("cyc-11")
    one_cycle(j, 1, "real")
    cycles = perf.cycles_from_journal(j)
    assert [c["cycle_id"] for c in cycles] == ["cyc-10", "cyc-11"]
    aborted, real = cycles
    assert aborted["outcome"] == "ABORTED_BEFORE_HEDGE" and aborted["closed"]
    assert real["outcome"] == "CLOSED"
    assert aborted["fees_usd"] == pytest.approx(100.0 + 55.0)   # 0.001 BTC fee + sale fee
    s = perf.summarise(cycles)
    assert s["aborted_cycles"] == 1 and s["closed_cycles"] == 2
    assert s["net_usd_confirmed"] == pytest.approx(
        aborted["net_usd"] + real["net_usd"])
    assert s["fees_usd"] == pytest.approx(aborted["fees_usd"] + real["fees_usd"])
    assert s["peak_notional_usd"] == pytest.approx(100_000.0)   # only the hedged one


def test_an_attempt_still_holding_spot_is_open_not_aborted():
    j = L.Journal()
    L.spot_buy(j, ms=T0, ref="s", qty=1.0, price=100_000.0, fee_btc=0.001,
               fee_status="confirmed")
    c = perf.cycles_from_journal(j)[0]
    assert c["outcome"] == "OPEN" and not c["closed"]


def test_a_suffixed_cycle_id_still_maps_to_its_observations():
    assert perf._cycle_ms("cyc-1700000000000") == 1700000000000
    assert perf._cycle_ms("cyc-1700000000000-2") == 1700000000000
    assert perf._cycle_ms("junk") is None
