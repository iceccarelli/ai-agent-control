"""Every entry decision's layered trace is durable, once per change, and
explaining a decision never changes it."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import entry_benefit_gate as ebg                              # noqa: E402
from persistence import StateStore                            # noqa: E402
from test_paired_leg_recovery import FeeVenue, MARK, modern, stack, warm_and_open  # noqa: E402


def engine(tmp_path, gate=None):
    store, venue, broker, eng = stack(str(tmp_path / "s.db"),
                                      FeeVenue(modern, spot_qty=0.0))
    eng.entry_benefit = gate or ebg.EntryBenefitGate()
    return store, eng


def rows(store):
    return [r for r in store.recent_decisions(100)
            if r["decision"] == "CARRY_ENTRY_TRACE"]


def test_a_refused_entry_leaves_its_trace_in_the_decision_journal(tmp_path):
    store, eng = engine(tmp_path)
    eng.trace_sink = lambda t: store.journal("BTCUSDT", "CARRY_ENTRY_TRACE",
                                             str(t["outcome"]), t)
    warm_and_open(eng)
    got = rows(store)
    assert len(got) == 1 and got[0]["reason"] == ebg.BENEFIT_UNAVAILABLE
    detail = json.loads(got[0]["detail"])
    assert detail["layers"]["mechanical"]["passed"] is True
    assert detail["fingerprint"] and detail["instrument"]["perp"] == "BTCUSDT"
    store.close()


def test_a_trace_survives_a_restart(tmp_path):
    store, eng = engine(tmp_path)
    eng.trace_sink = lambda t: store.journal("BTCUSDT", "CARRY_ENTRY_TRACE",
                                             str(t["outcome"]), t)
    warm_and_open(eng)
    store.close()
    again = StateStore(str(tmp_path / "s.db"))
    assert len(rows(again)) == 1
    again.close()


def test_an_unchanged_outcome_is_written_once_until_the_heartbeat(tmp_path):
    store, eng = engine(tmp_path)
    written = []
    eng.trace_sink = lambda t: written.append(t["outcome"])
    for _ in range(5):
        eng._persist_trace({"outcome": "X"})
    assert written == ["X"]
    eng._persist_trace({"outcome": "Y"})                       # a change is written
    assert written == ["X", "Y"]
    eng._trace_persisted = ("Y", eng._trace_persisted[1] - eng.TRACE_HEARTBEAT_S - 1)
    eng._persist_trace({"outcome": "Y"})                       # the heartbeat
    assert written == ["X", "Y", "Y"]
    store.close()


def test_a_failing_sink_never_changes_the_decision(tmp_path):
    store, eng = engine(tmp_path)

    def boom(trace):
        raise RuntimeError("disk full")
    eng.trace_sink = boom
    d = warm_and_open(eng)
    assert d.action == "stand_aside" and d.reason == ebg.BENEFIT_UNAVAILABLE
    assert eng._trace_persisted[0] == ""                       # not recorded as written
    store.close()


def test_no_sink_means_memory_only(tmp_path):
    store, eng = engine(tmp_path)
    warm_and_open(eng)
    assert eng.last_entry_trace and rows(store) == []
    store.close()


def test_the_wired_book_journals_through_the_store(tmp_path):
    import config as _config
    import main as _main
    cfg = _config.load({"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
                        "CARRY_EXECUTION_MODE": "overlay"})
    store = StateStore(str(tmp_path / "w.db"))
    from unittest import mock
    bot = _main.build_bot(config=cfg, store=store, client=mock.Mock(),
                          risk_manager=mock.Mock(), engine=mock.Mock())
    assert callable(bot.carry.trace_sink)
    bot.carry.trace_sink({"outcome": "ENTRY_BENEFIT_UNAVAILABLE", "x": 1})
    got = rows(store)
    assert got and got[0]["symbol"] == bot.carry.perp_symbol
    store.close()
