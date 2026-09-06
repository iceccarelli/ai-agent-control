"""A live-armed process must consult the promotion gate before any network call.

Invariant protected: promotion_gate_allows_live() was a report generator that
nothing on the order path read (audit S20). With the four env flags set, the
shell went live on mainnet with the unmeasured technical-analysis voter and no
notional cap. These tests pin the new refusal in main.TradingBot.startup.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import bybit_connection as bc  # noqa: E402
import main as m  # noqa: E402
import trading_engine as te  # noqa: E402
from fake_bybit import API_KEY, API_SECRET, FakeBybit  # noqa: E402
from persistence import StateStore  # noqa: E402
from position_sizing import BillionairePositionSizing  # noqa: E402
from risk_management import BillionaireRiskManager  # noqa: E402


from test_orchestrator import Cfg  # noqa: E402


class LiveCfg(Cfg):
    USE_TESTNET = False
    PAPER_TRADING = False
    LIVE_TRADING_ACK = "I_UNDERSTAND"


class SandboxCfg(LiveCfg):
    USE_TESTNET = True
    PAPER_TRADING = True


class NamedStrategy:
    signal_name = "funding_carry_fade_btc_v1"

    def signal_for(self, symbol):
        return None


class UnnamedStrategy:
    def signal_for(self, symbol):
        return None


def build(tmp_path, cfg, strategy=None):
    exchange = FakeBybit(balances={"USDT": 1_000.0}, equity=1_000.0)
    store = StateStore(str(tmp_path / "state.db"))
    store.update_equity(1_000.0)
    client = bc.BybitClient(config=cfg, store=store, transport=exchange)
    risk = BillionaireRiskManager(config=cfg, store=store)
    engine = te.TradingEngine(
        client=client, risk_manager=risk,
        position_sizer=BillionairePositionSizing(config=cfg, risk_manager=risk),
        store=store, config=cfg,
    )
    bot = m.TradingBot(config=cfg, store=store, client=client,
                       risk_manager=risk, engine=engine, strategy=strategy)
    return bot, exchange


def test_live_armed_with_gate_false_refuses_before_any_network_call(tmp_path):
    bot, exchange = build(tmp_path, LiveCfg(), strategy=UnnamedStrategy())
    n_before = len(exchange.requests)
    assert bot.startup() is False
    # sync_time is the first network call in startup; it must not have run.
    assert len(exchange.requests) == n_before
    bot.shutdown()


def test_live_armed_check_names_the_gate_as_the_refuser(tmp_path):
    bot, _ = build(tmp_path, LiveCfg(), strategy=NamedStrategy())
    allowed, why = bot.live_promotion_check()
    assert allowed is False
    assert "promotion gate" in why
    bot.shutdown()


def test_unnamed_strategy_is_refused_even_if_the_gate_said_yes(tmp_path, monkeypatch):
    import promotion_gate
    monkeypatch.setattr(promotion_gate, "promotion_gate_allows_live",
                        lambda **kw: True)
    bot, _ = build(tmp_path, LiveCfg(), strategy=UnnamedStrategy())
    allowed, why = bot.live_promotion_check()
    assert allowed is False
    assert "not the cleared edge signal" in why
    bot.shutdown()


def test_named_strategy_matching_cleared_signal_passes_when_gate_says_yes(
        tmp_path, monkeypatch):
    import promotion_gate
    import project_status
    monkeypatch.setattr(promotion_gate, "promotion_gate_allows_live",
                        lambda **kw: True)

    class Status:
        cleared_edge_signal = "funding_carry_fade_btc_v1"
    monkeypatch.setattr(project_status, "current", lambda cfg=None, **k: Status())
    bot, _ = build(tmp_path, LiveCfg(), strategy=NamedStrategy())
    allowed, _ = bot.live_promotion_check()
    assert allowed is True
    bot.shutdown()


def test_gate_exception_refuses(tmp_path, monkeypatch):
    import promotion_gate

    def boom(**kw):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(promotion_gate, "promotion_gate_allows_live", boom)
    bot, _ = build(tmp_path, LiveCfg(), strategy=NamedStrategy())
    allowed, why = bot.live_promotion_check()
    assert allowed is False and "could not be evaluated" in why
    bot.shutdown()


def test_sandbox_never_consults_the_gate(tmp_path, monkeypatch):
    import promotion_gate

    def boom(**kw):
        raise AssertionError("gate must not be consulted in sandbox")
    monkeypatch.setattr(promotion_gate, "promotion_gate_allows_live", boom)
    bot, _ = build(tmp_path, SandboxCfg(), strategy=UnnamedStrategy())
    assert bot.startup() is True
    bot.shutdown()


def test_shadow_strategy_declares_the_cleared_name():
    import shadow
    import shadow_strategy
    assert shadow_strategy.ShadowStrategy.signal_name == shadow.SHADOW_SIGNAL
    assert shadow.SHADOW_SIGNAL == "funding_carry_fade_btc_v1"


def test_main_does_not_read_the_research_field_itself():
    """The research record stays inert as a permission: only promotion_gate
    reads cleared_edge_signal, and only to refuse. Mirrors the guard in
    tests/test_funding_carry_fade_btc_v1.py."""
    import inspect
    assert "cleared_edge_signal" not in inspect.getsource(m)


def test_identity_check_is_negative_only(monkeypatch):
    import promotion_gate

    class Status:
        cleared_edge_signal = "funding_carry_fade_btc_v1"
    import project_status
    monkeypatch.setattr(project_status, "current", lambda cfg=None, **k: Status())
    assert promotion_gate.strategy_is_the_cleared_signal(None)[0] is True
    assert promotion_gate.strategy_is_the_cleared_signal(UnnamedStrategy())[0] is False
    assert promotion_gate.strategy_is_the_cleared_signal(NamedStrategy())[0] is True
    # a True here is NOT live: the gate's own predicate still refuses on this tree
    assert promotion_gate.promotion_gate_allows_live() is False
