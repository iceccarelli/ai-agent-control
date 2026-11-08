"""runtime_capability wired into BybitClient._request: a collector/monitor/backup client cannot sign or POST even if its code is buggy, and the refusal happens before the
transport, before signing and before any order counter. Offline; the transport is a recorder."""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, HERE)

import bybit_connection as bc  # noqa: E402
import runtime_capability as RC  # noqa: E402
from fake_bybit import API_KEY, API_SECRET, FakeBybit  # noqa: E402
from persistence import StateStore  # noqa: E402


pytestmark = pytest.mark.real_entry_authority      # the production entry authority, not conftest's permissive stand-in


class Cfg:
    BYBIT_VENUE = "testnet"
    USE_TESTNET = True
    PAPER_TRADING = False
    LIVE_TRADING_ACK = ""
    BYBIT_API_KEY = API_KEY
    BYBIT_API_SECRET = API_SECRET
    BYBIT_RECV_WINDOW_MS = 5000
    REQUEST_TIMEOUT_SECONDS = 5.0
    USE_LEVERAGE = False
    SYMBOL_FILTERS_TTL = 3600
    ORDERLINK_PREFIX = "BB"
    STATE_DB_PATH = ""


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "s.db"))
    yield s
    s.close()


def client(store, cap):
    v = FakeBybit()
    c = bc.BybitClient(config=Cfg(), store=store, transport=v)
    c.capability = cap
    return c, v


ORDER = {"category": "linear", "symbol": "BTCUSDT", "side": "Buy", "orderType": "Market", "qty": "0.001", "reduceOnly": True, "orderLinkId": "W1"}


def test_a_collector_cannot_sign_or_post_even_a_reduce_only_exit(store):
    c, v = client(store, RC.Capability.for_role(RC.PUBLIC_COLLECTOR))
    with pytest.raises(bc.OrderNotSent, match="CAPABILITY_REFUSED"):
        c._request("POST", "/v5/order/create", body=ORDER, signed=True)
    with pytest.raises(bc.OrderNotSent, match="CAPABILITY_REFUSED"):
        c._request("GET", "/v5/account/wallet-balance", params={"accountType": "UNIFIED"}, signed=True)
    assert v.requests == [] and c.orders_submitted == 0


def test_a_collector_can_read_public_market_data(store):
    c, v = client(store, RC.Capability.for_role(RC.PUBLIC_COLLECTOR))
    c._request("GET", "/v5/market/time", signed=False)
    assert len(v.requests) == 1


def test_a_read_only_monitor_reads_the_account_but_cannot_post(store):
    c, v = client(store, RC.Capability.for_role(RC.READ_ONLY_MONITOR))
    c._request("GET", "/v5/account/wallet-balance", params={"accountType": "UNIFIED"}, signed=True)
    with pytest.raises(bc.OrderNotSent, match="CAPABILITY_REFUSED"):
        c._request("POST", "/v5/order/create", body=ORDER, signed=True)
    with pytest.raises(bc.OrderNotSent, match="CAPABILITY_REFUSED"):
        c._request("POST", "/v5/position/set-leverage", body={"category": "linear", "symbol": "BTCUSDT", "buyLeverage": "1", "sellLeverage": "1"}, signed=True)
    assert len(v.requests) == 1


def test_a_backup_role_cannot_make_any_request(store):
    c, v = client(store, RC.Capability.for_role(RC.EVIDENCE_BACKUP))
    with pytest.raises(bc.OrderNotSent):
        c._request("GET", "/v5/market/time", signed=False)
    assert v.requests == []


def test_the_testnet_executor_role_passes_the_capability_but_still_meets_the_entry_authority(store):
    cap = RC.Capability.for_role(RC.TESTNET_EXECUTOR, venue="testnet", credential_class="testnet_dedicated")
    c, v = client(store, cap)
    c._request("POST", "/v5/order/create", body=ORDER, signed=True)                       # a reduce-only exit: capability ok, no authority needed
    assert len(v.requests) == 1
    with pytest.raises(bc.OrderNotSent, match="ENTRY_REFUSED"):                            # an OPENING order: the capability allows it, the authority (none installed) refuses
        c._request("POST", "/v5/order/create", body=dict(ORDER, side="Sell", reduceOnly=False, orderLinkId="W2"), signed=True)


def test_no_capability_means_the_existing_behaviour(store):
    c, v = client(store, None)
    c._request("GET", "/v5/market/time", signed=False)
    assert len(v.requests) == 1
