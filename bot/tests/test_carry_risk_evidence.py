"""Record what the production liquidation gate reads, and replay on it.

The live engine reads the venue's `liqPrice`, `accountMMRate` and margin ratio
on every held tick and used to throw the observation away, so no replay could
ever stop being "research output". Now the observation is persisted (the reads
are the ones the gate already makes - no new venue request) and a replay of
the same window can be run on it as `recorded` evidence: the latest
observation AT OR BEFORE each bar, never a later one; missing or stale
evidence halts the engine like any unreadable liquidation price.
Real StateStore (tmp file), real CarryBroker over Bybit-shaped payloads, real
CarryEngine and replay(). Offline.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import carry_backtest as cb          # noqa: E402
import carry_replay as rp            # noqa: E402
from carry_engine import BookState   # noqa: E402
from persistence import StateStore   # noqa: E402
from test_carry_replay import H8, START, rows  # noqa: E402
from test_carry_venue_direction_integration import (  # noqa: E402
    broker_with_rows, held_engine)

POS = {"side": "Sell", "size": "0.001", "positionIdx": 0, "markPrice": "100000",
       "positionIM": "20", "positionMM": "5"}


def obs(ms, **kw):
    base = dict(ms=ms, symbol="BTCUSDT", side="Sell", size=0.001,
                mark=100_000.0, liq_price=130_000.0, distance_pct=30.0,
                account_mm_rate=0.05, margin_multiple=6.0, reason="")
    base.update(kw)
    return base


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "state.db"))
    yield s
    s.close()


# -- 1. persistence -----------------------------------------------------------

def test_observations_round_trip_in_time_order(store):
    store.record_risk_observation(obs(2_000))
    store.record_risk_observation(obs(1_000, liq_price=None, distance_pct=None,
                                      reason="BEYOND_VENUE_PRICE_BOUNDS"))
    got = store.load_risk_observations()
    assert [o["ms"] for o in got] == [1_000, 2_000]
    assert got[0]["liq_price"] is None and got[0]["distance_pct"] is None
    assert got[0]["reason"] == "BEYOND_VENUE_PRICE_BOUNDS"
    assert got[1]["liq_price"] == 130_000.0
    assert [o["ms"] for o in store.load_risk_observations(since_ms=1_500)] == [2_000]


def test_an_observation_is_never_overwritten(store):
    store.record_risk_observation(obs(1_000, mark=100_000.0))
    store.record_risk_observation(obs(1_000, mark=1.0))
    got = store.load_risk_observations()
    assert len(got) == 1 and got[0]["mark"] == 100_000.0


def test_an_existing_database_gains_the_table_additively(tmp_path):
    path = str(tmp_path / "old.db")
    s = StateStore(path)
    s.close()
    con = sqlite3.connect(path)
    con.execute("DROP TABLE carry_risk_observation")
    con.commit()
    con.close()
    s2 = StateStore(path)
    try:
        s2.record_risk_observation(obs(1_000))
        assert len(s2.load_risk_observations()) == 1
    finally:
        s2.close()


# -- 2. the engine hands the gate's own read to an observer ---------------------

def gated_engine(row, sink):
    engine = held_engine(broker_with_rows(row))
    engine.require_liquidation_check = True
    engine.risk_observer = sink
    engine._last_tick_ms = 1_700_000_000_000
    return engine


def test_the_gates_read_is_recorded_with_the_margin_ratio(store):
    engine = gated_engine(dict(POS, liqPrice="130000"),
                          store.record_risk_observation)
    assert engine._check_margin() is None
    (o,) = store.load_risk_observations()
    assert o["ms"] == 1_700_000_000_000 and o["symbol"] == "BTCUSDT"
    assert o["side"] == "Sell" and o["size"] == pytest.approx(0.001)
    assert o["liq_price"] == 130_000.0
    assert o["distance_pct"] == pytest.approx(30.0)
    assert o["margin_multiple"] == pytest.approx(4.0)       # IM 20 / MM 5
    assert o["reason"] == ""


def test_the_venues_unreachable_statement_is_recorded_as_such(store):
    engine = gated_engine(dict(POS, liqPrice=""), store.record_risk_observation)
    assert engine._check_margin() is None
    (o,) = store.load_risk_observations()
    assert o["liq_price"] is None and o["reason"] == "BEYOND_VENUE_PRICE_BOUNDS"


def test_a_failing_observer_cannot_change_the_trading_decision():
    def broken(_o):
        raise RuntimeError("disk full")
    engine = gated_engine(dict(POS, liqPrice="101000"), broken)
    d = engine._check_margin()
    assert d is not None and d.reason.startswith("LIQUIDATION_")   # still unwinds


def test_nothing_is_recorded_when_the_gate_is_off(store):
    engine = gated_engine(dict(POS, liqPrice="130000"),
                          store.record_risk_observation)
    engine.require_liquidation_check = False
    engine._check_margin()
    assert store.load_risk_observations() == []


def test_build_bot_wires_the_production_store_to_the_engine():
    from unittest import mock
    import config as _config
    import main as _main
    from test_carry_wiring_authority import _Store
    s = _Store()
    s.record_risk_observation = lambda o: None
    cfg = _config.load({"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
                        "CARRY_EXECUTION_MODE": "overlay"})
    bot = _main.build_bot(config=cfg, store=s, client=mock.Mock(),
                          risk_manager=mock.Mock(), engine=mock.Mock())
    assert bot.carry.risk_observer is s.record_risk_observation


# -- 3. replay on what was recorded --------------------------------------------

def window(n=60):
    return rows(n)


def inputs_from(observations, **kw):
    return rp.LiquidationInputs.from_observations(
        observations, source="state db fixture", **kw)


UNREACHABLE = dict(liq_price=None, distance_pct=None,
                   reason="BEYOND_VENUE_PRICE_BOUNDS")


def covering(data, **kw):
    """One observation per bar, as the live tick would leave."""
    return [obs(r.ms, **kw) for r in data]


def test_recorded_evidence_makes_the_replay_production_equivalent():
    data = window()
    r = rp.replay(data, notional=100_000.0, borrow_apr=0.0,
                  liquidation=inputs_from(covering(data, **UNREACHABLE)))
    liq = r["liquidation"]
    assert liq["gate"] == "EVALUATED" and liq["evidence_kind"] == "recorded"
    assert liq["production_equivalent"] is True
    assert r["halted_at_ms"] is None and r["trades"] == 1
    assert liq["unreachable_ticks"] >= 1


def test_a_recorded_liquidation_price_drives_the_production_decision():
    data = window()
    first = rp.replay(data, notional=100_000.0, borrow_apr=0.0)
    entry = first["trade_rows"][0]["entry_perp"]
    # the venue said liquidation sits 5% above the entry: inside the floor
    ev = inputs_from(covering(data, liq_price=entry * 1.05,
                              distance_pct=5.0))
    r = rp.replay(data, notional=100_000.0, borrow_apr=0.0, liquidation=ev)
    assert r["trade_rows"][0]["reason"].startswith("LIQUIDATION_")


def test_the_replay_never_reads_an_observation_from_the_future():
    data = window()
    first = rp.replay(data, notional=100_000.0, borrow_apr=0.0)
    idx = first["trade_rows"][0]["entry_index"]
    entry = first["trade_rows"][0]["entry_perp"]
    future_only = [obs(r.ms, liq_price=entry * 1.05, distance_pct=5.0)
                   for r in data[idx + 4:]]
    ev = inputs_from(future_only + [obs(data[0].ms, **UNREACHABLE)],
                     max_age_ms=10 * H8)
    trace = []
    rp.replay(data, notional=100_000.0, borrow_apr=0.0, liquidation=ev,
              trace=trace)
    # before idx + 4 only the first (benign) observation exists; the 5%
    # observation recorded later must not have changed a single earlier tick
    assert all(t["reason"] != "" for t in trace[: idx + 4])
    assert not any(str(t["reason"]).startswith("LIQUIDATION_")
                   for t in trace[: idx + 4])


def _skipped_like_main_tick(r):
    """A bar with no usable evidence has no market view; main.tick skips such a
    tick. The replay must do the same - never crash, never invent a value."""
    return (r["reasons"].get("SNAPSHOT_REFUSED", 0) >= 1
            and r["liquidation"]["ticks_evaluated"] == 0
            and not any(str(k).startswith("LIQUIDATION_")
                        for k in r["reasons"]))


def test_no_observation_at_or_before_a_bar_skips_the_tick():
    data = window()
    r = rp.replay(data, notional=100_000.0, borrow_apr=0.0,
                  liquidation=inputs_from(covering(data, **UNREACHABLE)[20:]))
    assert r["reasons"].get("stale:LiquidationEvidenceMissing", 0) >= 1
    assert r["reasons"].get("SNAPSHOT_REFUSED", 0) >= 1
    assert r["liquidation"]["ticks_evaluated"] < len(data) - 20 + 1


def test_a_stale_observation_is_refused_not_reused():
    data = window()
    only_first = [obs(data[0].ms, **UNREACHABLE)]
    r = rp.replay(data, notional=100_000.0, borrow_apr=0.0,
                  liquidation=inputs_from(only_first))      # default 8h bound
    assert _skipped_like_main_tick(r)
    assert r["reasons"].get("stale:LiquidationEvidenceInvalid", 0) >= 1


def test_unusable_recorded_values_are_refused():
    data = window()
    r = rp.replay(data, notional=100_000.0, borrow_apr=0.0,
                  liquidation=inputs_from(covering(
                      data, margin_multiple=float("nan"), **UNREACHABLE)))
    assert _skipped_like_main_tick(r)
    r = rp.replay(data, notional=100_000.0, borrow_apr=0.0,
                  liquidation=inputs_from(covering(data, liq_price=float("nan"))))
    assert r["reasons"].get("LIQUIDATION_PRICE_UNREADABLE", 0) >= 1
    assert r["halted_at_ms"] is not None


def test_a_missing_price_the_venue_never_called_unreachable_is_refused():
    data = window()
    r = rp.replay(data, notional=100_000.0, borrow_apr=0.0,
                  liquidation=inputs_from(covering(
                      data, liq_price=None, distance_pct=None, reason="")))
    assert r["reasons"].get("LIQUIDATION_PRICE_UNREADABLE", 0) >= 1


def test_from_observations_validates_its_own_inputs():
    with pytest.raises(ValueError):
        rp.LiquidationInputs.from_observations([], source="x")
    with pytest.raises(ValueError):
        rp.LiquidationInputs.from_observations([obs(1)], source=" ")
    with pytest.raises(ValueError):
        rp.LiquidationInputs.from_observations([obs(1)], source="x",
                                               max_age_ms=0)


# -- 4. loading a recording read-only --------------------------------------------

def test_a_state_database_is_loaded_read_only_and_labelled(store, tmp_path):
    for i in range(3):
        store.record_risk_observation(obs(START + i * H8))
    path = str(tmp_path / "state.db")
    before = os.path.getmtime(path)
    ev = rp.recorded_liquidation_from_db(path)
    assert ev.kind == "recorded" and "state.db" in ev.source
    assert "3 observations" in ev.source
    assert os.path.getmtime(path) == before


def test_a_database_without_observations_is_refused(tmp_path):
    s = StateStore(str(tmp_path / "empty.db"))
    s.close()
    with pytest.raises(rp.ReplayRefusal):
        rp.recorded_liquidation_from_db(str(tmp_path / "empty.db"))
    with pytest.raises(rp.ReplayRefusal):
        rp.recorded_liquidation_from_db(str(tmp_path / "nope.db"))
