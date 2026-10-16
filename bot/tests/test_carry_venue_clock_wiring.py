"""The clock-skew gate runs against the production broker.

`market_snapshot.take_snapshot` checks the venue's clock against the local
one only when the broker exposes `get_venue_time_s`; `MarketSnapshot.
assert_fresh` then refuses a skew above MAX_CLOCK_SKEW_S. The shadow broker
exposes it, so shadow books were clock-checked. `CarryBroker` did not, so
the live book never was: every freshness and settlement-age judgement in
production ran on the container's own clock alone, and `clock_checked` was
always False. The reader is the PUBLIC `/v5/market/time` (no key, no
signature). Built through `build_bot`, so it is the wrapped production broker
that is tested. Offline.
"""
from __future__ import annotations

import os
import sys
import time
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config as _config  # noqa: E402
import main as _main  # noqa: E402
from market_snapshot import MAX_CLOCK_SKEW_S, StaleMarket, take_snapshot  # noqa: E402
from test_carry_wiring_authority import _Store  # noqa: E402

OK = {"retCode": 0, "retMsg": "OK"}


def built_bot(*, venue_offset_s=0.0, time_fails=False):
    def _request(method, endpoint, **kw):
        if endpoint == "/v5/market/time":
            if time_fails:
                raise RuntimeError("503")
            return {**OK, "result": {
                "timeSecond": str(int(time.time() + venue_offset_s)),
                "timeNano": "0"}}
        if endpoint == "/v5/market/tickers":
            return {**OK, "result": {"list": [{
                "markPrice": "100000", "lastPrice": "100000",
                "fundingRate": "0.0001", "bid1Price": "99999.5",
                "ask1Price": "100000.5"}]}}
        if endpoint == "/v5/market/funding/history":
            return {**OK, "result": {"list": [{
                "symbol": "BTCUSDT", "fundingRate": "0.0001",
                "fundingRateTimestamp": str(int((time.time() - 3600) * 1000))}]}}
        if endpoint == "/v5/position/list":
            return {**OK, "result": {"list": [{"side": "", "size": "0",
                                               "positionIdx": 0}]}}
        if endpoint == "/v5/account/wallet-balance":
            return {**OK, "result": {"list": [{"coin": [
                {"coin": "BTC", "walletBalance": "5", "locked": "0"}]}]}}
        return {**OK, "result": {"list": []}}

    client = mock.Mock()
    client.base_url = "https://api-testnet.bybit.com"
    client._request = mock.Mock(side_effect=_request)
    cfg = _config.load({"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
                        "CARRY_EXECUTION_MODE": "overlay",
                        "USE_TESTNET": "1", "PAPER_TRADING": "1"})
    bot = _main.build_bot(config=cfg, store=_Store(), client=client,
                          risk_manager=mock.Mock(), engine=mock.Mock())
    return bot, client


def snapshot(bot):
    return take_snapshot(bot.carry.broker, perp_symbol="BTCUSDT",
                         spot_symbol="BTCUSDT")


def test_the_production_view_is_clock_checked():
    bot, client = built_bot()
    view = snapshot(bot)
    assert view.detail["clock_checked"] is True
    view.assert_fresh()
    time_calls = [c for c in client._request.call_args_list
                  if c.args[:2] == ("GET", "/v5/market/time")]
    assert time_calls and not time_calls[0].kwargs.get("signed"), \
        "venue time is a public read"


def test_a_venue_clock_far_from_ours_refuses_the_view():
    bot, _ = built_bot(venue_offset_s=4 * MAX_CLOCK_SKEW_S)
    view = snapshot(bot)
    with pytest.raises(StaleMarket, match="clock"):
        view.assert_fresh()


def test_an_unreadable_venue_clock_skips_the_check_and_says_so():
    bot, _ = built_bot(time_fails=True)
    view = snapshot(bot)
    assert view.detail["clock_checked"] is False
    view.assert_fresh()


def test_a_malformed_venue_time_is_not_a_time():
    from carry_broker import PairIncident
    bot, client = built_bot()
    real = client._request.side_effect

    def bad(method, endpoint, **kw):
        if endpoint == "/v5/market/time":
            return {**OK, "result": {"timeSecond": "soon"}}
        return real(method, endpoint, **kw)
    client._request.side_effect = bad
    with pytest.raises(PairIncident):
        bot.carry.broker.get_venue_time_s()
