"""A database holding one kind of money is never resumed as another.

The carry journal, position and order rows had no venue scope: a testnet journal
with venue funding in it was resumed by a mainnet process, whose statement then
showed that funding as realised (independent review finding 7). Startup now
refuses a database that has recorded a DIFFERENT environment, and refuses a
mainnet start on a ledger of unknown origin. Offline; real StateStore.
"""
from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import config as _config                                     # noqa: E402
import ledger as L                                           # noqa: E402
import main as _main                                         # noqa: E402
from persistence import StateStore                           # noqa: E402


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "s.db"))
    yield s
    s.close()


def with_ledger(store):
    j = L.Journal()
    L.funding(j, ms=1_700_000_000_000, ref="f", usd=123.45, basis="venue")
    store.save_ledger(j.to_rows())


def test_a_fresh_database_is_compatible_with_any_environment(store):
    for env in ("testnet", "paper", "mainnet"):
        assert _main.environment_conflict(store, env) is None


def test_a_testnet_database_is_refused_by_a_mainnet_process(store):
    store.record_execution_environment("testnet")
    why = _main.environment_conflict(store, "mainnet")
    assert why and "testnet" in why and "mainnet" in why


@pytest.mark.parametrize("seen,now", [("paper", "testnet"), ("testnet", "paper"),
                                      ("mainnet", "testnet"), ("paper", "mainnet")])
def test_any_other_environment_is_a_conflict(store, seen, now):
    store.record_execution_environment(seen)
    assert _main.environment_conflict(store, now) is not None


def test_the_same_environment_is_fine(store):
    store.record_execution_environment("testnet")
    assert _main.environment_conflict(store, "testnet") is None


def test_a_database_that_ever_held_two_environments_is_refused_by_all(store):
    store.record_execution_environment("testnet")
    store.record_execution_environment("paper")
    for env in ("testnet", "paper", "mainnet"):
        assert _main.environment_conflict(store, env) is not None


def test_a_mainnet_process_refuses_a_ledger_of_unknown_origin(store):
    with_ledger(store)                                   # rows, no recorded environment
    why = _main.environment_conflict(store, "mainnet")
    assert why and "unknown" in why.lower()
    assert _main.environment_conflict(store, "testnet") is None       # a sandbox may adopt it


def test_startup_refuses_and_records_nothing_on_a_conflict(store):
    store.record_execution_environment("testnet")
    cfg = _config.load({"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
                        "CARRY_EXECUTION_MODE": "overlay"})
    import dataclasses
    mainnet_like = dataclasses.replace(cfg, USE_TESTNET=False, PAPER_TRADING=True)
    bot = _main.build_bot(config=mainnet_like, store=store, client=mock.Mock(),
                          risk_manager=mock.Mock(), engine=mock.Mock())
    bot.cfg, bot.store = mainnet_like, store
    # paper on a testnet-recorded database is also a conflict
    assert bot.startup() is False
    assert store.execution_environments_seen() == ["testnet"]
