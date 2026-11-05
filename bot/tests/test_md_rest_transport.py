"""Public REST parsers and client; the public WebSocket transport.

REST parsers are checked against REAL venue payloads already tracked in the repo:
the mainnet public captures of 2026-09-09 (instrument rules, funding history) and
the testnet order-book capture. The transport is checked over real sockets
(`socket.socketpair`, a loopback proxy), not a mock of the protocol."""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import threading

import pytest

import md_events as E
import md_rest as R
import md_transport as T
import md_wire as W
import ws_transport as WT
from md_helpers import HERE, load_study

CTX = W.FrameContext(1, E.LINEAR, "rest", 1_793_820_000_000, 5.0, E.SOURCE_REST)


# -- REST parsing on real payloads -----------------------------------------------------------------

def test_real_mainnet_instrument_rules_parse_to_the_exchange_stated_values():
    lin = R.parse_rest("/v5/market/instruments-info", {"category": "linear", "symbol": "BTCUSDT"},
                       200, json.dumps(load_study("instrument_linear.json.gz")), CTX)
    spec = lin.specs[0]
    assert lin.rejections == () and (spec.category, spec.symbol, spec.status) == ("linear", "BTCUSDT", "Trading")
    assert (spec.tick_size, spec.qty_step, spec.min_qty) == (0.1, 0.001, 0.001)
    assert spec.max_qty == 1500.0 and spec.max_market_qty == 150.0 and spec.min_notional == 5.0
    assert spec.funding_interval_min == 480 and spec.upper_funding_rate == 0.00333
    assert spec.lower_funding_rate == -0.00333 and spec.base_coin == "BTC" and spec.quote_coin == "USDT"
    sp = R.parse_rest("/v5/market/instruments-info", {"category": "spot", "symbol": "BTCUSDT"},
                      200, json.dumps(load_study("instrument_spot.json.gz")), CTX)
    s = sp.specs[0]
    assert (s.tick_size, s.qty_step, s.min_qty, s.max_market_qty) == (0.1, 0.000001, 0.000001, 120.0)
    assert s.min_notional == 5.0 and s.funding_interval_min is None


def test_real_funding_history_parses_newest_print_by_time():
    rows = load_study("funding_BTCUSDT.json.gz")                       # oldest first, 8h cadence
    newest_first = list(reversed(rows[-5:]))
    body = json.dumps({"retCode": 0, "retMsg": "OK", "result": {"category": "linear", "list": newest_first}})
    p = R.parse_rest("/v5/market/funding/history", {"category": "linear", "symbol": "BTCUSDT"}, 200, body, CTX)
    assert p.rejections == () and len(p.prints) == 5
    top = max(p.prints, key=lambda x: x.settled_ms)
    assert top.settled_ms == int(rows[-1]["fundingRateTimestamp"]) and top.rate == float(rows[-1]["fundingRate"])
    assert abs(top.rate) < 0.01                                        # a fraction (1e-4 = 1 bp), not bps


def test_real_testnet_order_book_touch_parses():
    path = os.path.join(os.path.dirname(HERE), "artifacts", "testnet_conformance_evidence.jsonl")
    with open(path) as fh:
        rec = next(json.loads(line) for line in fh if "/v5/market/orderbook" in line)
    resp = dict(rec["response"])
    resp.pop("_status_code", None)
    from urllib.parse import parse_qs, urlsplit
    cat = parse_qs(urlsplit(rec["request_url"]).query)["category"][0]
    p = R.parse_rest("/v5/market/orderbook", {"category": cat, "symbol": "BTCUSDT"}, 200, json.dumps(resp), CTX)
    assert p.rejections == () and p.touch[0] == cat and p.touch[1] < p.touch[2]


def test_server_time_parses():
    body = json.dumps({"retCode": 0, "result": {"timeSecond": "1790751563", "timeNano": "1790751563310813250"},
                       "time": 1790751563310})
    assert R.parse_rest("/v5/market/time", {}, 200, body, CTX).server_time_ms == 1790751563310


@pytest.mark.parametrize("status,body,reason", [
    (403, '<html>The Amazon CloudFront distribution is configured to block access from your country</html>', E.GEO_BLOCKED),
    (403, '{"error":"The Amazon CloudFront distribution is configured to block access from your country"}', E.GEO_BLOCKED),
    (403, "Forbidden", E.RATE_LIMITED),
    (429, "slow down", E.RATE_LIMITED),
    (200, json.dumps({"retCode": 10006, "retMsg": "Too many visits"}), E.RATE_LIMITED),
    (200, json.dumps({"retCode": 10001, "retMsg": "params error"}), E.RET_CODE_NOT_OK),
    (500, json.dumps({"retCode": 0, "result": {}}), E.RET_CODE_NOT_OK),
    (200, "<html>", E.NOT_JSON),
    (200, "[1]", E.NOT_AN_OBJECT),
    (200, json.dumps({"retCode": 0, "result": []}), E.MISSING_FIELD),
])
def test_unusable_rest_answers_are_named_rejections(status, body, reason):
    p = R.parse_rest("/v5/market/tickers", {"category": "linear"}, status, body, CTX)
    assert p.rejections and p.rejections[0].reason == reason


def test_bad_numbers_in_a_rest_body_are_rejected_not_defaulted():
    good = load_study("instrument_linear.json.gz")
    bad = json.loads(json.dumps(good))
    bad["result"]["list"][0]["priceFilter"]["tickSize"] = "0"
    assert R.parse_rest("/v5/market/instruments-info", {"category": "linear"}, 200, json.dumps(bad), CTX).rejections
    bad = json.loads(json.dumps(good))
    del bad["result"]["list"][0]["lotSizeFilter"]["qtyStep"]
    assert R.parse_rest("/v5/market/instruments-info", {"category": "linear"}, 200, json.dumps(bad), CTX).rejections
    funding = {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "fundingRate": "7",
                                                  "fundingRateTimestamp": "1790000000000"}]}}
    assert R.parse_rest("/v5/market/funding/history", {}, 200, json.dumps(funding), CTX).rejections
    crossed = {"retCode": 0, "result": {"b": [["101", "1"]], "a": [["100", "1"]], "ts": 1}}
    assert R.parse_rest("/v5/market/orderbook", {"category": "spot"}, 200, json.dumps(crossed), CTX).rejections


# -- the REST client ---------------------------------------------------------------------------------------

class FakeNet:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.urls = []

    def __call__(self, url, timeout):
        self.urls.append(url)
        a = self.answers.pop(0) if self.answers else (200, '{"retCode":0,"result":{}}')
        if isinstance(a, Exception):
            raise a
        return a[0], a[1], {}


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def client(net, clock, **kw):
    return R.PublicRest(net, clock=clock.now, sleep=clock.sleep, **kw)


def test_only_allow_listed_public_endpoints_over_https_can_be_called():
    c = client(FakeNet(), Clock())
    for bad in ("/v5/order/create", "/v5/account/wallet-balance", "/v5/position/list", "/anything"):
        with pytest.raises(ValueError):
            c.get(bad, {})
    with pytest.raises(ValueError):
        R.PublicRest(FakeNet(), base_url="http://api.bybit.com")
    assert all(e.startswith("/v5/market/") for e in R.ALLOWED_ENDPOINTS)


def test_requests_are_sorted_get_urls_and_rate_limited_by_a_token_bucket():
    clk, net = Clock(), FakeNet()
    c = client(net, clk, rate_per_s=2.0, burst=2)
    for _ in range(4):
        c.get("/v5/market/tickers", {"symbol": "BTCUSDT", "category": "linear"})
    assert net.urls[0] == "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT"
    assert clk.slept and sum(clk.slept) >= 0.9               # 2 burst + 2 more at 2/s


def test_an_ip_limit_stops_calling_for_the_ban_window():
    clk, net = Clock(), FakeNet((429, "x"))
    c = client(net, clk)
    c.get("/v5/market/time", {})
    with pytest.raises(R.RestBlocked):
        c.get("/v5/market/time", {})
    assert len(net.urls) == 1
    clk.t += R.IP_BAN_WINDOW_S + 1
    c.get("/v5/market/time", {})
    assert len(net.urls) == 2


def test_a_retcode_10006_also_opens_the_ban_window_but_a_geo_block_does_not():
    clk = Clock()
    net = FakeNet((200, json.dumps({"retCode": 10006})))
    c = client(net, clk)
    c.get("/v5/market/time", {})
    with pytest.raises(R.RestBlocked):
        c.get("/v5/market/time", {})
    clk2 = Clock()
    geo = client(FakeNet((403, "CloudFront ... country")), clk2)
    geo.get("/v5/market/time", {})
    geo.get("/v5/market/time", {})                                # not a ban: no state change
    assert geo.blocked_until == 0.0


def test_a_transport_failure_is_its_own_error():
    c = client(FakeNet(R.RestTransportError("timeout")), Clock())
    with pytest.raises(R.RestTransportError):
        c.get("/v5/market/time", {})


# -- the WebSocket transport (real sockets) ----------------------------------------------------------------------

def frame(op, payload=b"", fin=True, masked=False):
    b0 = (0x80 if fin else 0) | op
    n = len(payload)
    head = bytes([b0]) + (bytes([n | (0x80 if masked else 0)]) if n < 126 else
                          bytes([126 | (0x80 if masked else 0)]) + struct.pack("!H", n))
    return head + payload


@pytest.fixture
def pair():
    server, client_side = socket.socketpair()
    ws = T.PublicWebSocket("wss://stream.example/v5/public/spot")
    ws._sock = client_side
    yield ws, server
    for s in (server, client_side):
        s.close()


def test_a_fragmented_message_is_reassembled(pair):
    ws, srv = pair
    msg = json.dumps({"topic": "x", "data": "y" * 300}).encode()
    srv.sendall(frame(T.OP_TEXT, msg[:100], fin=False) + frame(T.OP_CONT, msg[100:200], fin=False)
                + frame(T.OP_CONT, msg[200:], fin=True))
    op, data = ws.recv_message(timeout=2)
    assert op == T.OP_TEXT and data == msg


def test_a_ping_is_answered_even_inside_a_fragmented_message(pair):
    ws, srv = pair
    srv.sendall(frame(T.OP_TEXT, b"ab", fin=False) + frame(T.OP_PING, b"hi") + frame(T.OP_CONT, b"cd"))
    op, data = ws.recv_message(timeout=2)
    assert data == b"abcd"
    srv.settimeout(2)
    reply = srv.recv(64)
    assert reply[0] & 0x0F == T.OP_PONG                          # client frames are masked pongs


def test_control_frames_and_close(pair):
    ws, srv = pair
    srv.sendall(frame(T.OP_PONG, b""))
    assert ws.recv_message(timeout=2)[0] == T.OP_PONG
    srv.sendall(frame(T.OP_CLOSE, b"\x03\xe8"))
    with pytest.raises(WT.WSClosed):
        ws.recv_message(timeout=2)


@pytest.mark.parametrize("bad", [
    frame(T.OP_TEXT, b"x", masked=True),                         # masked server frame
    frame(T.OP_CONT, b"x"),                                      # continuation with nothing to continue
    frame(T.OP_PING, b"x" * 126, fin=True)[:2] + b"\x00\x7e" + b"x" * 126,   # oversize control
    bytes([0xC1, 0x01, 0x41]),                                   # reserved bit (compression) not negotiated
    frame(0x3, b"x"),                                            # unsupported opcode
])
def test_protocol_violations_are_refused(pair, bad):
    ws, srv = pair
    srv.sendall(bad)
    with pytest.raises(WT.WSProtocolError):
        ws.recv_message(timeout=2)


def test_a_new_data_frame_inside_a_fragmented_message_is_refused(pair):
    ws, srv = pair
    srv.sendall(frame(T.OP_TEXT, b"ab", fin=False) + frame(T.OP_TEXT, b"cd"))
    with pytest.raises(WT.WSProtocolError):
        ws.recv_message(timeout=2)


def test_a_reassembled_message_over_the_cap_is_refused(pair):
    ws, srv = pair
    ws.MAX_MESSAGE_BYTES = 10
    srv.sendall(frame(T.OP_TEXT, b"x" * 6, fin=False) + frame(T.OP_CONT, b"y" * 6))
    with pytest.raises(WT.WSProtocolError):
        ws.recv_message(timeout=2)


def test_read_timeout_is_a_timeout_not_a_close(pair):
    ws, _ = pair
    with pytest.raises(socket.timeout):
        ws.recv_message(timeout=0.05)


def _proxy(reply: bytes, seen: dict):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += conn.recv(4096)
        seen["request"] = buf.decode()
        conn.sendall(reply)
        conn.close()
        srv.close()
    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1]


def test_the_proxy_tunnel_sends_connect_with_credentials_and_returns_the_socket():
    seen = {}
    port = _proxy(b"HTTP/1.1 200 Connection Established\r\n\r\n", seen)
    ws = T.PublicWebSocket("wss://stream.bybit.com/v5/public/linear",
                           proxy_url=f"http://user:pw@127.0.0.1:{port}")
    ws._tls = lambda sock, host: sock                              # the tunnel is under test, not TLS
    sock = ws._open_socket("stream.bybit.com", 443)
    sock.close()
    lines = seen["request"].split("\r\n")
    assert lines[0] == "CONNECT stream.bybit.com:443 HTTP/1.1"
    token = base64.b64encode(b"user:pw").decode()
    assert f"Proxy-Authorization: Basic {token}" in lines


def test_a_proxy_that_refuses_the_tunnel_is_a_connection_error():
    port = _proxy(b"HTTP/1.1 403 Forbidden\r\n\r\n", {})
    ws = T.PublicWebSocket("wss://stream.bybit.com/x", proxy_url=f"http://127.0.0.1:{port}")
    ws._tls = lambda sock, host: sock
    with pytest.raises(WT.WSConnectionError):
        ws._open_socket("stream.bybit.com", 443)


def test_the_public_transport_reads_no_environment_and_carries_no_credentials():
    src = open(os.path.join(os.path.dirname(HERE), "md_transport.py")).read()
    assert "os.environ" not in src and "getenv" not in src and "API_KEY" not in src.upper().replace("APIKEY", "")
