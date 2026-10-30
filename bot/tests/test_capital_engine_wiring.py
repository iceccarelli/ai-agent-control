"""build_bot wires the portfolio authority, the opportunity cost and the
ENTRIES_ENABLED fix; startup records what kind of money the ledger holds.

ENTRIES_ENABLED=0 (or a typo: unknown values parse to False) used to leave
bot.carry as None, so a HELD pair got no cold start, no margin or liquidation
check and no exit while the log claimed stop management stayed live. Found by
the independent review. Real StateStore and real CarryEngine; mock client.
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
import main as _main                                         # noqa: E402
from carry_engine import BookState                           # noqa: E402
from persistence import StateStore                           # noqa: E402

BASE = {"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
        "CARRY_EXECUTION_MODE": "overlay"}


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "s.db"))
    yield s
    s.close()


def bot_with(store, **env):
    cfg = _config.load(dict(BASE, **env))
    return _main.build_bot(config=cfg, store=store, client=mock.Mock(),
                           risk_manager=mock.Mock(), engine=mock.Mock()), cfg


def test_the_portfolio_authority_is_attached_on_the_real_store(store):
    bot, cfg = bot_with(store)
    auth = bot.carry.capital_authority
    assert auth is not None and auth.store is store
    assert auth.ttl_s == pytest.approx(cfg.CAPITAL_RESERVATION_TTL_S)
    assert "venue=" in auth.account_scope and "testnet=" in auth.account_scope
    assert auth.reduction_allowed() is True


def test_a_test_double_store_gets_no_authority_and_no_error():
    from test_carry_wiring_authority import _Store
    cfg = _config.load(BASE)
    bot = _main.build_bot(config=cfg, store=_Store(), client=mock.Mock(),
                          risk_manager=mock.Mock(), engine=mock.Mock())
    assert bot.carry.capital_authority is None


def test_the_opportunity_cost_reaches_the_engine_and_defaults_to_zero(store):
    assert bot_with(store)[0].carry.opportunity_cost_apr == 0.0
    bot, _ = bot_with(store, CARRY_OPPORTUNITY_COST_APR="0.04")
    assert bot.carry.opportunity_cost_apr == pytest.approx(0.04)


@pytest.mark.parametrize("bad", ["-0.01", "nan", "2", "abc"])
def test_a_nonsense_opportunity_cost_is_refused_at_load(bad):
    with pytest.raises(_config.ConfigError):
        _config.load(dict(BASE, CARRY_OPPORTUNITY_COST_APR=bad))


@pytest.mark.parametrize("flag", ["0", "false", "typo-not-a-bool"])
def test_disabled_entries_still_attach_the_engine_that_watches_a_held_pair(store, flag):
    bot, _ = bot_with(store, ENTRIES_ENABLED=flag)
    assert bot.carry is not None
    assert bot.carry.entries_enabled is False


def test_enabled_entries_are_the_default(store):
    assert bot_with(store)[0].carry.entries_enabled is True


def test_with_entries_disabled_no_pair_opens():
    from test_paired_leg_recovery import stack, warm_and_open
    from test_carry_fee_truth import FeeVenue, modern
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), "e.db")
    st, venue, broker, engine = stack(path, FeeVenue(modern, spot_qty=0.0))
    engine.entries_enabled = False
    d = warm_and_open(engine)
    assert d.action == "stand_aside" and d.reason == "ENTRIES_DISABLED"
    assert venue.created == []
    st.close()


def test_a_held_pair_still_exits_with_entries_disabled():
    from test_paired_leg_recovery import stack, warm_and_open, fresh_view
    from test_carry_fee_truth import FeeVenue, modern, H8, MARK
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), "e.db")
    st, venue, broker, engine = stack(path, FeeVenue(modern, spot_qty=0.0))
    for i in (1, 2):
        fresh_view(engine)
        engine.on_candle(mark=MARK, funding_bps=3.0, spot=MARK, funding_print_ms=i * H8)
    fresh_view(engine)
    assert engine.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                            funding_print_ms=3 * H8).action == "opened"
    engine.entries_enabled = False                  # the operator switches entries off
    stamp = 4 * H8
    for _ in range(10):
        fresh_view(engine)
        engine.on_candle(mark=MARK, funding_bps=-4.0, spot=MARK, funding_print_ms=stamp)
        stamp += H8
        if engine.position is None:
            break
    assert engine.position is None and venue.perp_qty == 0.0
    st.close()


@pytest.mark.parametrize("env,expected", [
    ({"USE_TESTNET": "1", "PAPER_TRADING": "1"}, "testnet"),
    ({"USE_TESTNET": "1", "PAPER_TRADING": "0"}, "testnet"),
    ({"USE_TESTNET": "0", "PAPER_TRADING": "1"}, "paper"),
])
def test_the_execution_environment_follows_the_sandbox_flags(env, expected):
    assert _main.execution_environment_of(_config.load(dict(BASE, **env))) == expected


def test_an_unset_flag_is_never_mainnet():
    class Bare:
        pass
    assert _main.execution_environment_of(Bare()) == "testnet"


def test_startup_writes_the_environment_to_the_store(store):
    bot, cfg = bot_with(store, USE_TESTNET="1", PAPER_TRADING="1")
    bot.cfg, bot.store = cfg, store
    try:
        bot.startup()
    except Exception:  # noqa: BLE001 - only the recording step is under test
        pass
    assert store.execution_environment() == "testnet"
