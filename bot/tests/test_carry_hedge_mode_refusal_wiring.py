"""The hedge-mode refusal must reach cold start through the PRODUCTION wiring.

`TradingBot.carry_cold_start` asks `broker.get_position_mode` and skips the
check when the broker has no such reader. In production the engine's broker
is `ledger.LedgerBroker`, which forwards venue reads one by one and did not
forward this one, so a hedge-mode account started anyway: the refusal added
for it (test_carry_hedge_mode_refusal.py) only ever ran against a bare
`CarryBroker`. This test builds the bot the way main does.
"""
from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config as _config  # noqa: E402
import main as _main  # noqa: E402
from ledger import LedgerBroker  # noqa: E402
from test_carry_wiring_authority import _Store  # noqa: E402

OK = {"retCode": 0, "retMsg": "OK"}
FLAT_ONE_WAY = [{"side": "", "size": "0", "positionIdx": 0,
                 "positionIM": "0", "positionMM": "0"}]
FLAT_HEDGE = [{"side": "", "size": "0", "positionIdx": 1,
               "positionIM": "0", "positionMM": "0"},
              {"side": "", "size": "0", "positionIdx": 2,
               "positionIM": "0", "positionMM": "0"}]


def built_bot(position_rows):
    def _request(method, endpoint, **kw):
        if endpoint == "/v5/position/list":
            return {**OK, "result": {"list": position_rows}}
        if endpoint == "/v5/account/wallet-balance":
            return {**OK, "result": {"list": [{"coin": [
                {"coin": "BTC", "walletBalance": "5", "locked": "0"}]}]}}
        if endpoint in ("/v5/order/realtime", "/v5/order/history",
                        "/v5/market/funding/history"):
            return {**OK, "result": {"list": []}}
        return {**OK, "result": {}}

    client = mock.Mock()
    client.base_url = "https://api-testnet.bybit.com"
    client._request = mock.Mock(side_effect=_request)
    cfg = _config.load({"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
                        "CARRY_EXECUTION_MODE": "overlay",
                        "USE_TESTNET": "1", "PAPER_TRADING": "1"})
    bot = _main.build_bot(config=cfg, store=_Store(), client=client,
                          risk_manager=mock.Mock(), engine=mock.Mock())
    assert isinstance(bot.carry.broker, LedgerBroker)   # the production path
    return bot, client


def test_the_production_broker_exposes_the_position_mode_reader():
    bot, _ = built_bot(FLAT_ONE_WAY)
    assert bot.carry.broker.get_position_mode("BTCUSDT") == "one_way"


def test_a_hedge_mode_account_is_refused_through_build_bot():
    bot, client = built_bot(FLAT_HEDGE)
    assert bot.carry_cold_start() is False
    assert not [c for c in client._request.call_args_list
                if c.args[:2] == ("POST", "/v5/order/create")]


def test_a_one_way_account_starts_through_build_bot():
    bot, _ = built_bot(FLAT_ONE_WAY)
    assert bot.carry_cold_start() is True
