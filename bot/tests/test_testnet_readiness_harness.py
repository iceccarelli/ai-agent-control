"""Offline harness: the REAL BybitClient / PrivateWebSocket / take_snapshot
driven through fake transports, feeding assess_testnet_readiness. No network."""
from __future__ import annotations

import gzip
import json
import os
import socket
import struct
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bybit_connection as bc  # noqa: E402
import market_snapshot as ms  # noqa: E402
import testnet_readiness as tr  # noqa: E402
import ws_transport as wt  # noqa: E402
from fake_bybit import API_KEY, API_SECRET  # noqa: E402
from persistence import StateStore  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
H8 = 8 * 3600 * 1000


def _row(name):
    p = os.path.join(HERE, "..", "research", "exchange_study", "bybit", name)
    return json.load(gzip.open(p))["result"]["list"][0]


class Cfg:
    USE_TESTNET = True
    BYBIT_API_KEY = API_KEY
    BYBIT_API_SECRET = API_SECRET
    BYBIT_RECV_WINDOW_MS = 5000
    REQUEST_TIMEOUT_SECONDS = 5.0
    USE_LEVERAGE = False
    SYMBOL_FILTERS_TTL = 3600
    ORDERLINK_PREFIX = "BB"
    CATEGORY = "linear"


class FakeLinearVenue:
    """A Transport serving linear instrument, ticker, funding and time data,
    with switches for the failure modes under test."""

    def __init__(self):
        self.clock_offset_s = 0.0
        self.funding_rows = []
        self.disconnected = False
        self.position_rows = []
        self.paths = []
        self.last_print_ms = int(time.time() * 1000) // H8 * H8

    def request(self, method, url, *, headers, params=None, body=None, timeout=10.0):
        path = url.split("?", 1)[0].replace("https://api-testnet.bybit.com", "")
        self.paths.append(path)
        if self.disconnected:
            raise ConnectionError("socket reset")
        ok = lambda res, **x: (200, json.dumps({"retCode": 0, "result": res, **x}))
        if path == "/v5/market/time":
            return ok({"timeNano": str(int((time.time() + self.clock_offset_s) * 1e9))})
        if path == "/v5/market/instruments-info":
            return ok({"list": [_row("instrument_linear.json.gz")]})
        if path == "/v5/market/funding/history":
            return ok({"list": self.funding_rows})
        if path == "/v5/market/tickers":
            return ok({"list": [{"symbol": "BTCUSDT", "lastPrice": "50000",
                                 "bid1Price": "49999.9", "ask1Price": "50000.1",
                                 "fundingRate": "0.0001"}]},
                      time=int(time.time() * 1000))
        if path == "/v5/position/list":
            return ok({"list": self.position_rows})
        return 200, json.dumps({"retCode": 10001, "retMsg": "unknown", "result": {}})


@pytest.fixture()
def venue():
    return FakeLinearVenue()


@pytest.fixture()
def client(tmp_path, venue, monkeypatch):
    monkeypatch.setattr(bc.BybitClient, "_backoff", staticmethod(lambda a: None))
    monkeypatch.setattr(bc.BybitClient, "_rate_limit_backoff", lambda self, a: None)
    return bc.BybitClient(config=Cfg(), store=StateStore(str(tmp_path / "s.db")),
                          transport=venue)


def _rows(n, now_ms, dup=False):
    base = now_ms // H8 * H8
    rows = [{"symbol": "BTCUSDT", "fundingRate": "0.0001",
             "fundingRateTimestamp": str(base - i * H8)} for i in range(n)]  # newest first
    if dup:
        rows.insert(1, dict(rows[0]))
    return rows


def _verdict(client, venue, *, now_ms=None, **over):
    now_ms = now_ms or int(time.time() * 1000)
    skew = client.sync_time()
    f = client.get_instrument_filters("BTCUSDT")
    hist = client.get_funding_history_fractions("BTCUSDT")
    mode = "hedge" if any(str(r.get("positionIdx")) in ("1", "2") for r in venue.position_rows) else "one_way"
    kw = dict(venue=client.venue, use_testnet=client.testnet, live_trading=False,
              live_authorized=client.describe()["live_authorized"],
              endpoint_host=client.describe()["host"],
              perp_instrument=_row("instrument_linear.json.gz"),
              spot_instrument=_row("instrument_spot.json.gz"),
              snapshot_ages_s={"perp_mark": 1, "spot_mark": 1, "funding": 1},
              venue_clock_offset_ms=skew, position_mode=mode,
              margin_mode="REGULAR_MARGIN", funding_history=hist,
              now_ms=now_ms + 1000)
    kw.update(over)
    assert f.from_exchange
    return tr.assess_testnet_readiness(**kw)


def test_instrument_metadata_parses_from_venue(client):
    f = client.get_instrument_filters("BTCUSDT")
    assert (str(f.tick_size), str(f.qty_step), str(f.min_qty), str(f.min_notional)) == \
        ("0.10", "0.001", "0.001", "5")


def test_healthy_venue_is_ready(client, venue):
    venue.funding_rows = _rows(5, int(time.time() * 1000))
    v = _verdict(client, venue)
    assert v.ready, v.refusals
    assert client.describe()["orders_submitted"] == 0
    assert not any("order" in p for p in venue.paths)


def test_duplicate_funding_messages_are_collapsed(client, venue):
    venue.funding_rows = _rows(5, int(time.time() * 1000), dup=True)
    hist = client.get_funding_history_fractions("BTCUSDT")
    stamps = [s for s, _ in hist]
    assert stamps == sorted(set(stamps)) and len(stamps) == 5


def test_missing_funding_event_is_incomplete(client, venue):
    rows = _rows(6, int(time.time() * 1000))
    del rows[2]
    venue.funding_rows = rows
    assert _verdict(client, venue).has(tr.INCOMPLETE_FUNDING_HISTORY)


def test_malformed_funding_row_is_a_missing_event(client, venue):
    rows = _rows(6, int(time.time() * 1000))
    rows[3]["fundingRate"] = "n/a"
    venue.funding_rows = rows
    assert _verdict(client, venue).has(tr.INCOMPLETE_FUNDING_HISTORY)


def test_clock_drift_refused(client, venue):
    venue.funding_rows = _rows(5, int(time.time() * 1000))
    venue.clock_offset_s = 600.0
    v = _verdict(client, venue)
    assert v.has(tr.EXCESSIVE_CLOCK_DRIFT)
    with pytest.raises(bc.PermanentAPIError):
        client.assert_clock_within(5_000)


def test_disconnect_surfaces_typed_error_and_recovers(client, venue):
    venue.funding_rows = _rows(5, int(time.time() * 1000))
    venue.disconnected = True
    with pytest.raises(bc.TransientAPIError):
        client.get_instrument_filters("BTCUSDT", force=True)
    with pytest.raises(bc.TransientAPIError):
        client.sync_time()
    venue.disconnected = False                       # reconnect
    assert _verdict(client, venue).ready


def test_hedge_mode_is_refused(client, venue):
    venue.funding_rows = _rows(5, int(time.time() * 1000))
    venue.position_rows = [{"symbol": "BTCUSDT", "positionIdx": 1, "size": "0"},
                           {"symbol": "BTCUSDT", "positionIdx": 2, "size": "0"}]
    assert _verdict(client, venue).has(tr.INVALID_POSITION_MODE)
    assert client.get_position("BTCUSDT") is None     # flat is flat; the readiness
    #                                                  verdict is what refuses the mode
    venue.position_rows[1]["size"] = "0.5"
    with pytest.raises(bc.PermanentAPIError):          # a held hedge slot is never "the" position
        client.get_position("BTCUSDT")


def test_mainnet_client_is_refused_and_sends_nothing(tmp_path, venue):
    cfg = Cfg()
    cfg.USE_TESTNET = False
    c = bc.BybitClient(config=cfg, store=StateStore(str(tmp_path / "m.db")), transport=venue)
    with pytest.raises(bc.PermanentAPIError):
        c._request("POST", "/v5/order/create", body={"symbol": "BTCUSDT"}, signed=True)
    assert venue.paths == []
    d = c.describe()
    v = tr.assess_testnet_readiness(
        venue=c.venue, use_testnet=c.testnet, live_trading=False,
        live_authorized=d["live_authorized"], endpoint_host=d["host"],
        perp_instrument=None, spot_instrument=None, snapshot_ages_s=None,
        venue_clock_offset_ms=0, position_mode="one_way", margin_mode=None,
        funding_required=False)
    assert v.has(tr.ACCIDENTAL_MAINNET)


# -- price freshness through the REAL take_snapshot ---------------------------

class Broker:
    def __init__(self, print_ms, venue_time=None):
        self.print_ms, self.venue_time = print_ms, venue_time

    def get_mark(self, s): return 100_000.0
    def get_spot_mark(self, s): return 100_010.0
    def get_funding_bps(self, s): return 1.0
    def get_funding_print(self, s): return 1.0, self.print_ms
    def get_perp_position(self, s): return 0.0

    def get_venue_time_s(self):
        if self.venue_time is None:
            raise ConnectionError("down")
        return self.venue_time


def _snap(broker, now):
    return ms.take_snapshot(broker, perp_symbol="BTCUSDT", spot_symbol="BTCUSDT", now_s=now)


def test_snapshot_freshness_and_missed_funding_event():
    now = time.time()
    last = int(now * 1000) // H8 * H8
    _snap(Broker(last, now), now).assert_fresh()
    stale_obs = _snap(Broker(last, now), now - 600)
    with pytest.raises(ms.StaleMarket):
        stale_obs.assert_fresh()
    with pytest.raises(ms.StaleMarket):                     # missed settlement
        _snap(Broker(last - 2 * H8, now), now).assert_fresh()
    with pytest.raises(ms.StaleMarket):                     # venue clock 5 min off
        _snap(Broker(last, now + 300), now).assert_fresh()
    s = _snap(Broker(last, None), now)                      # venue time unreadable
    assert s.detail["clock_checked"] is False


# -- websocket: disconnect, duplicate frames, reconnect ---------------------

def _srv_frame(sock, opcode, payload):
    sock.sendall(bytes([0x80 | opcode, len(payload)]) + payload)


def _ws(sock):
    ws = wt.PrivateWebSocket("wss://stream-testnet.bybit.com/v5/private")
    ws._sock = sock
    return ws


def test_ws_duplicate_frames_are_delivered_verbatim_for_consumer_dedupe():
    a, b = socket.socketpair()
    try:
        ws = _ws(a)
        msg = json.dumps({"topic": "execution", "id": "x1"}).encode()
        _srv_frame(b, wt.OP_TEXT, msg)
        _srv_frame(b, wt.OP_TEXT, msg)
        assert ws.recv(timeout=1) == (wt.OP_TEXT, msg)
        assert ws.recv(timeout=1) == (wt.OP_TEXT, msg)       # transport does not hide it
    finally:
        a.close(); b.close()


def test_ws_disconnect_raises_closed_and_reconnect_is_a_fresh_object():
    a, b = socket.socketpair()
    ws = _ws(a)
    b.close()                                                 # peer vanishes
    with pytest.raises(wt.WSClosed):
        ws.recv(timeout=1)
    ws.close()
    with pytest.raises(wt.WSClosed):
        ws.recv(timeout=1)                                    # closed stays closed
    c, d = socket.socketpair()                                # "reconnect"
    try:
        _srv_frame(d, wt.OP_TEXT, b"{}")
        assert _ws(c).recv(timeout=1) == (wt.OP_TEXT, b"{}")
    finally:
        c.close(); d.close()


def test_ws_truncated_frame_is_closed_not_misframed():
    a, b = socket.socketpair()
    try:
        ws = _ws(a)
        b.sendall(bytes([0x81, 10]) + b"abc")
        b.close()
        with pytest.raises(wt.WSClosed):
            ws.recv(timeout=1)
    finally:
        a.close()
