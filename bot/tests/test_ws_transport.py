"""ws_transport.py: the RFC 6455 handshake and frame codec, proven against
real sockets (a `socket.socketpair()`, no TLS, no network) rather than
mocked — a hand-rolled protocol implementation is exactly the kind of code
where a mock can hide a real bug.
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ws_transport as wt  # noqa: E402


def _server_send_frame(server_sock, opcode: int, payload: bytes) -> None:
    """Encode and send an UNMASKED frame, as a real server must (RFC 6455
    forbids a server from masking) — the counterpart to
    PrivateWebSocket._send_frame, written independently here so a bug in
    one is not hidden by a matching bug in the other."""
    length = len(payload)
    header = bytes([0x80 | opcode])
    if length < 126:
        header += bytes([length])
    elif length < 65536:
        header += bytes([126]) + struct.pack("!H", length)
    else:
        header += bytes([127]) + struct.pack("!Q", length)
    server_sock.sendall(header + payload)


def _server_recv_frame(server_sock):
    """Decode one MASKED client frame — independent of
    PrivateWebSocket._send_frame's own encoding."""
    header = server_sock.recv(2)
    b0, b1 = header[0], header[1]
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    assert masked, "client frames must be masked per RFC 6455"
    if length == 126:
        length = struct.unpack("!H", server_sock.recv(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", server_sock.recv(8))[0]
    mask_key = server_sock.recv(4)
    masked_payload = server_sock.recv(length) if length else b""
    payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(masked_payload))
    return opcode, payload


class TestHandshake:
    def test_valid_accept_completes_the_handshake(self):
        client_sock, server_sock = socket.socketpair()
        ws = wt.PrivateWebSocket("wss://example.invalid/v5/private")

        captured = {}

        def fake_server():
            request = b""
            while b"\r\n\r\n" not in request:
                request += server_sock.recv(4096)
            captured["request"] = request.decode("ascii")
            key_line = [ln for ln in captured["request"].split("\r\n")
                       if ln.lower().startswith("sec-websocket-key")][0]
            key = key_line.split(":", 1)[1].strip()
            accept = base64.b64encode(
                hashlib.sha1((key + wt.WS_GUID).encode()).digest()).decode()
            response = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n")
            server_sock.sendall(response.encode("ascii"))

        import threading
        t = threading.Thread(target=fake_server)
        t.start()
        ws._handshake(client_sock, host="example.invalid", path="/v5/private")
        t.join(timeout=5)

        assert "GET /v5/private HTTP/1.1" in captured["request"]
        assert "Upgrade: websocket" in captured["request"]
        client_sock.close()
        server_sock.close()

    def test_wrong_accept_is_refused(self):
        client_sock, server_sock = socket.socketpair()

        def fake_server():
            request = b""
            while b"\r\n\r\n" not in request:
                request += server_sock.recv(4096)
            response = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Accept: dGhpcyBpcyB3cm9uZw==\r\n\r\n")
            server_sock.sendall(response.encode("ascii"))

        import threading
        t = threading.Thread(target=fake_server)
        t.start()
        ws = wt.PrivateWebSocket("wss://example.invalid/v5/private")
        with pytest.raises(wt.WSConnectionError, match="did not match"):
            ws._handshake(client_sock, host="example.invalid", path="/v5/private")
        t.join(timeout=5)
        client_sock.close()
        server_sock.close()

    def test_non_101_status_is_refused(self):
        client_sock, server_sock = socket.socketpair()

        def fake_server():
            request = b""
            while b"\r\n\r\n" not in request:
                request += server_sock.recv(4096)
            server_sock.sendall(b"HTTP/1.1 404 Not Found\r\n\r\n")

        import threading
        t = threading.Thread(target=fake_server)
        t.start()
        ws = wt.PrivateWebSocket("wss://example.invalid/v5/private")
        with pytest.raises(wt.WSConnectionError, match="404"):
            ws._handshake(client_sock, host="example.invalid", path="/v5/private")
        t.join(timeout=5)
        client_sock.close()
        server_sock.close()

    def test_plain_ws_scheme_is_refused_before_any_socket_is_opened(self):
        ws = wt.PrivateWebSocket("ws://example.invalid/v5/private")
        with pytest.raises(wt.WSConnectionError, match="non-TLS"):
            ws.connect()


class TestFramingRoundTrip:
    """Real socketpair, real bytes, no TLS. The client's send/recv and an
    independent server-side codec written just for this test."""

    def _connected_ws(self):
        client_sock, server_sock = socket.socketpair()
        ws = wt.PrivateWebSocket("wss://example.invalid/v5/private")
        ws._sock = client_sock
        return ws, server_sock

    def test_client_sends_a_correctly_masked_text_frame(self):
        ws, server_sock = self._connected_ws()
        ws.send_text('{"op":"auth"}')
        opcode, payload = _server_recv_frame(server_sock)
        assert opcode == wt.OP_TEXT
        assert payload == b'{"op":"auth"}'

    def test_client_receives_a_server_text_frame(self):
        ws, server_sock = self._connected_ws()
        _server_send_frame(server_sock, wt.OP_TEXT, b'{"topic":"order"}')
        opcode, payload = ws.recv(timeout=5)
        assert opcode == wt.OP_TEXT
        assert payload == b'{"topic":"order"}'

    def test_large_payload_uses_extended_length(self):
        ws, server_sock = self._connected_ws()
        big = b"x" * 70_000
        _server_send_frame(server_sock, wt.OP_TEXT, big)
        opcode, payload = ws.recv(timeout=5)
        assert opcode == wt.OP_TEXT
        assert payload == big

    def test_ping_from_server_is_delivered_as_a_ping_opcode(self):
        ws, server_sock = self._connected_ws()
        _server_send_frame(server_sock, wt.OP_PING, b"")
        opcode, _ = ws.recv(timeout=5)
        assert opcode == wt.OP_PING

    def test_client_pong_is_sent_correctly(self):
        ws, server_sock = self._connected_ws()
        ws.send_pong(b"payload")
        opcode, payload = _server_recv_frame(server_sock)
        assert opcode == wt.OP_PONG
        assert payload == b"payload"

    def test_close_frame_from_server_raises_wsclosed(self):
        ws, server_sock = self._connected_ws()
        _server_send_frame(server_sock, wt.OP_CLOSE, b"")
        with pytest.raises(wt.WSClosed):
            ws.recv(timeout=5)

    def test_dead_socket_raises_wsclosed_not_a_bare_oserror(self):
        ws, server_sock = self._connected_ws()
        server_sock.close()
        ws._sock.close()
        with pytest.raises(wt.WSClosed):
            ws.recv(timeout=5)

    def test_fragmented_frame_is_refused_not_misdecoded(self):
        """Deliberate scope limit (see module docstring): a non-final
        (FIN=0) frame must be refused, never silently treated as complete
        or as garbage payload."""
        ws, server_sock = self._connected_ws()
        # FIN=0, opcode=text: first byte 0x01 (no 0x80 FIN bit).
        header = bytes([0x01, 5])
        server_sock.sendall(header + b"hello")
        with pytest.raises(wt.WSProtocolError, match="fragmented"):
            ws.recv(timeout=5)

    def test_masked_server_frame_is_refused(self):
        """RFC 6455 forbids a server from masking. A server frame that
        claims to be masked is a protocol violation to refuse, not a
        format quirk to accommodate."""
        ws, server_sock = self._connected_ws()
        header = bytes([0x81, 0x80 | 5])  # FIN+text, MASK bit set + len 5
        server_sock.sendall(header + os.urandom(4) + b"hello")
        with pytest.raises(wt.WSProtocolError, match="masked"):
            ws.recv(timeout=5)

    def test_recv_timeout_raises_socket_timeout(self):
        ws, server_sock = self._connected_ws()
        with pytest.raises(socket.timeout):
            ws.recv(timeout=0.1)

    def test_close_sends_a_close_frame_and_closes_the_socket(self):
        ws, server_sock = self._connected_ws()
        ws.close()
        opcode, _ = _server_recv_frame(server_sock)
        assert opcode == wt.OP_CLOSE
