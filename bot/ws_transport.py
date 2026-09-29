"""A minimal, correct RFC 6455 WebSocket client, stdlib only.

WHY THIS EXISTS
================
`bybit_connection.py` has a private-WS URL (`ws_private_url`) and an auth
frame builder (`BybitClient.ws_auth_message`), but nothing that actually
opens a socket — see that module's own "-- private websocket --" section.
Closing that gap needs a WebSocket client. This repo's dependency list is
deliberately short (`requirements.txt`: "every dependency is attack
surface"), and nothing else in this codebase uses `asyncio` — `main.py`
uses `threading` for its one background thread (the health server), so a
synchronous, thread-driven client fits the existing concurrency model
rather than introducing a second one.

This module is the transport ONLY: connect, handshake, send a text frame,
receive a text/control frame, close. It knows nothing about Bybit's
message schema, order state, or auth signing — `private_ws_consumer.py`
is the layer that knows that, and it is written against this module's
small `PrivateWebSocket` interface, not against raw sockets, so it can be
tested with a fake transport instead of a real network connection.

DELIBERATE SCOPE LIMIT — NO FRAGMENTED FRAMES
================================================
RFC 6455 allows a message to span multiple frames (FIN=0 continuation).
Bybit's private-stream payloads are small, single-frame JSON messages in
practice. Rather than implement continuation-frame reassembly (real
complexity, no test coverage against the actual venue to validate it
against, and a subtle place to get wrong), `_read_frame` refuses
(`WSProtocolError`) on a non-final frame. That is fail-closed: an
unsupported wire shape is refused loudly, never silently misdecoded into
a truncated or corrupted message.
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
from typing import Optional, Tuple
from urllib.parse import urlsplit

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WSError(Exception):
    """Base class for every error this module raises."""


class WSConnectionError(WSError):
    """The socket/TLS/HTTP-upgrade handshake failed."""


class WSProtocolError(WSError):
    """A frame violated this client's (deliberately strict) expectations."""


class WSClosed(WSError):
    """The connection is closed — a clean close frame or the socket died."""


def _make_sec_websocket_key() -> str:
    return base64.b64encode(os.urandom(16)).decode("ascii")


def _expected_accept(key: str) -> str:
    digest = hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


class PrivateWebSocket:
    """A single client connection to one WebSocket URL.

    Not thread-safe for concurrent send+recv from two threads at once —
    the consumer that uses this owns one reader loop and calls `send_text`
    from that same loop (e.g. to answer a ping), matching how
    `private_ws_consumer.WSPrivateConsumer` drives it.
    """

    def __init__(self, url: str, *, connect_timeout: float = 10.0) -> None:
        self.url = url
        self._connect_timeout = connect_timeout
        self._sock: Optional[socket.socket] = None

    def connect(self) -> None:
        """Scheme check + TCP + TLS + handshake. Split into
        `_open_socket`/`_handshake` so tests can exercise the handshake and
        frame codec against a plain `socket.socketpair()` — real sockets,
        real bytes, no TLS and no network needed to prove the protocol
        logic itself is correct.
        """
        parts = urlsplit(self.url)
        if parts.scheme != "wss":
            raise WSConnectionError(
                f"refusing non-TLS websocket scheme {parts.scheme!r} — "
                "a private venue stream is never sent over plain ws://")
        host = parts.hostname
        port = parts.port or 443
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"

        sock = self._open_socket(host, port)
        self._handshake(sock, host=host, path=path)
        self._sock = sock

    def _open_socket(self, host: str, port: int) -> ssl.SSLSocket:
        raw_sock = socket.create_connection((host, port), timeout=self._connect_timeout)
        context = ssl.create_default_context()
        return context.wrap_socket(raw_sock, server_hostname=host)

    def _handshake(self, sock: socket.socket, *, host: str, path: str) -> None:
        key = _make_sec_websocket_key()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(request.encode("ascii"))

        response = self._read_http_response(sock)
        status_line = response.split("\r\n", 1)[0]
        if " 101 " not in f" {status_line} ":
            raise WSConnectionError(f"handshake refused: {status_line!r}")
        headers = self._parse_headers(response)
        accept = headers.get("sec-websocket-accept", "")
        if accept != _expected_accept(key):
            raise WSConnectionError(
                "handshake Sec-WebSocket-Accept did not match the expected "
                "value — refusing a connection that cannot be verified as "
                "the server we asked for")

    @staticmethod
    def _read_http_response(sock: socket.socket) -> str:
        buf = b""
        sock.settimeout(10.0)
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise WSConnectionError("connection closed during handshake")
            buf += chunk
            if len(buf) > 65536:
                raise WSConnectionError("handshake response too large")
        return buf.decode("iso-8859-1", errors="replace")

    @staticmethod
    def _parse_headers(response: str) -> dict:
        headers = {}
        for line in response.split("\r\n")[1:]:
            if not line or ":" not in line:
                continue
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
        return headers

    # -- framing -------------------------------------------------------

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def send_pong(self, payload: bytes = b"") -> None:
        self._send_frame(OP_PONG, payload)

    def send_ping(self, payload: bytes = b"") -> None:
        self._send_frame(OP_PING, payload)

    def close(self) -> None:
        sock = self._sock
        self._sock = None
        if sock is None:
            return
        try:
            self._send_frame(OP_CLOSE, b"", sock=sock)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _send_frame(self, opcode: int, payload: bytes, *,
                    sock: Optional[socket.socket] = None) -> None:
        sock = sock or self._sock
        if sock is None:
            raise WSClosed("not connected")
        mask_key = os.urandom(4)
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        length = len(payload)
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", length)
        try:
            sock.sendall(header + mask_key + masked)
        except OSError as exc:
            raise WSClosed(f"send failed: {exc}") from exc

    def recv(self, *, timeout: Optional[float] = None) -> Tuple[int, bytes]:
        """One frame: (opcode, payload). Raises WSClosed on a close frame
        or a dead socket, WSProtocolError on anything this client refuses
        to interpret (see module docstring re: fragmentation)."""
        sock = self._sock
        if sock is None:
            raise WSClosed("not connected")
        try:
            sock.settimeout(timeout)
            header = self._recv_exact(sock, 2)
        except socket.timeout:
            raise
        except OSError as exc:
            raise WSClosed(f"recv failed: {exc}") from exc
        b0, b1 = header[0], header[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if not fin:
            raise WSProtocolError(
                "fragmented frame received; this client does not "
                "reassemble continuation frames (see module docstring)")
        if masked:
            raise WSProtocolError(
                "server frame was masked; RFC 6455 forbids this — refusing "
                "a frame from a server that violates the spec")
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(sock, 8))[0]
        payload = self._recv_exact(sock, length) if length else b""
        if opcode == OP_CLOSE:
            raise WSClosed(f"server sent close frame: {payload!r}")
        return opcode, payload

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> bytes:
        if n == 0:
            return b""
        chunks = []
        remaining = n
        while remaining > 0:
            chunk = sock.recv(remaining)
            if not chunk:
                raise WSClosed("connection closed mid-frame")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
