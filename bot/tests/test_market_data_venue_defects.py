"""Defects found by the testnet-readiness audit. Each test failed before its fix."""
from __future__ import annotations

import json
import os
import socket
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bybit_connection as bc  # noqa: E402
import market_snapshot as ms  # noqa: E402
import ws_transport as wt  # noqa: E402
from fake_bybit import API_KEY, API_SECRET, FakeBybit  # noqa: E402
from persistence import StateStore  # noqa: E402


class Cfg:
    USE_TESTNET = True
    BYBIT_API_KEY = API_KEY
    BYBIT_API_SECRET = API_SECRET
    BYBIT_RECV_WINDOW_MS = 5000
    REQUEST_TIMEOUT_SECONDS = 5.0
    USE_LEVERAGE = False
    SYMBOL_FILTERS_TTL = 3600
    ORDERLINK_PREFIX = "BB"
    CATEGORY = "spot"


def _client(tmp_path, transport, category="spot"):
    cfg = Cfg()
    cfg.CATEGORY = category
    return bc.BybitClient(config=cfg, store=StateStore(str(tmp_path / "s.db")),
                          transport=transport)


class Scripted:
    """A Transport answering every request with one canned body."""

    def __init__(self, body):
        self.body, self.calls = body, 0

    def request(self, method, url, **kw):
        self.calls += 1
        return 200, json.dumps(self.body)


def _ok(result, **extra):
    return {"retCode": 0, "retMsg": "OK", "result": result, **extra}


# -- ws: an announced frame length is not trusted ---------------------------

def test_ws_refuses_absurd_frame_length_instead_of_allocating():
    a, b = socket.socketpair()
    try:
        ws = wt.PrivateWebSocket("wss://x/y")
        ws._sock = a
        b.sendall(bytes([0x81, 127]) + struct.pack("!Q", 1 << 40))
        with pytest.raises(wt.WSProtocolError):
            ws.recv(timeout=1.0)
    finally:
        a.close(); b.close()


# -- instrument filters: a zero is not a rule -------------------------------

@pytest.mark.parametrize("field,value", [("tickSize", "0"), ("qtyStep", "0"),
                                         ("minOrderQty", "0"),
                                         ("minNotionalValue", "0")])
def test_zero_instrument_filter_is_refused(tmp_path, field, value):
    row = {"symbol": "BTCUSDT", "priceFilter": {"tickSize": "0.1"},
           "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001",
                             "minNotionalValue": "5"}}
    sect = "priceFilter" if field == "tickSize" else "lotSizeFilter"
    row[sect][field] = value
    c = _client(tmp_path, Scripted(_ok({"list": [row]})), "linear")
    with pytest.raises(bc.PermanentAPIError):
        c.get_instrument_filters("BTCUSDT")


def test_non_trading_instrument_is_refused(tmp_path):
    row = {"symbol": "BTCUSDT", "status": "Closed",
           "priceFilter": {"tickSize": "0.1"},
           "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001",
                             "minNotionalValue": "5"}}
    c = _client(tmp_path, Scripted(_ok({"list": [row]})), "linear")
    with pytest.raises(bc.PermanentAPIError):
        c.get_instrument_filters("BTCUSDT")


# -- hedge mode: the first row is not THE position --------------------------

def test_get_position_refuses_hedge_mode_rows(tmp_path):
    rows = [{"symbol": "BTCUSDT", "positionIdx": 1, "size": "0.5", "side": "Buy"},
            {"symbol": "BTCUSDT", "positionIdx": 2, "size": "0.5", "side": "Sell"}]
    c = _client(tmp_path, Scripted(_ok({"list": rows})), "linear")
    with pytest.raises(bc.PermanentAPIError):
        c.get_position("BTCUSDT")


# -- clock: an absurd venue time must not become the skew -------------------

def test_sync_time_exposes_skew_and_gate(tmp_path):
    import time
    far = int((time.time() + 3600) * 1e9)
    c = _client(tmp_path, Scripted({"retCode": 0, "result": {"timeNano": str(far)}}))
    skew = c.sync_time()
    assert skew > 3_000_000
    with pytest.raises(bc.PermanentAPIError):
        c.assert_clock_within(max_skew_ms=5_000)


def test_sync_time_rejects_nonpositive_server_time(tmp_path):
    c = _client(tmp_path, Scripted({"retCode": 0, "result": {"timeSecond": "0"}}))
    with pytest.raises(bc.PermanentAPIError):
        c.sync_time()


# -- spot/perp mapping ------------------------------------------------------

class _Broker:
    def get_mark(self, s): return 100.0
    def get_spot_mark(self, s): return 100.0
    def get_funding_bps(self, s): return 1.0
    def get_funding_print(self, s): return 1.0, 0
    def get_perp_position(self, s): return 0.0


@pytest.mark.parametrize("perp,spot", [("1000PEPEUSDT", "PEPEUSDT"),
                                       ("BTCUSDT", "ETHUSDT")])
def test_take_snapshot_refuses_mismapped_legs(perp, spot):
    with pytest.raises(ms.StaleMarket):
        ms.take_snapshot(_Broker(), perp_symbol=perp, spot_symbol=spot)
