"""WebSocket transport for the PUBLIC market-data feed.

`ws_transport.PrivateWebSocket` is the strict client the private order stream
uses; it refuses fragmented frames and opens its own socket. A public book feed
needs two more things and gets them here, as a subclass, without loosening the
private client:

* **continuation frames** (RFC 6455 FIN=0) reassembled, with a total-size cap;
* an **HTTP CONNECT tunnel** through an explicit proxy URL (the caller passes
  it; this module reads no environment) before TLS.

Ping frames are answered, pong frames are returned as such, close frames raise.
The connection never carries credentials: it is a public stream.
"""
from __future__ import annotations

import base64
import socket
import ssl
import struct
from typing import Optional, Tuple
from urllib.parse import urlsplit

import ws_transport as WT

OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG, OP_CONT = (
    WT.OP_TEXT, WT.OP_BINARY, WT.OP_CLOSE, WT.OP_PING, WT.OP_PONG, WT.OP_CONTINUATION)


class PublicWebSocket(WT.PrivateWebSocket):
    #: Largest reassembled message accepted.
    MAX_MESSAGE_BYTES = 4 << 20

    def __init__(self, url: str, *, proxy_url: Optional[str] = None,
                 ca_file: Optional[str] = None, connect_timeout: float = 10.0) -> None:
        super().__init__(url, connect_timeout=connect_timeout)
        self._proxy_url = proxy_url
        self._ca_file = ca_file

    def _tls(self, sock: socket.socket, host: str) -> ssl.SSLSocket:
        ctx = (ssl.create_default_context(cafile=self._ca_file) if self._ca_file
               else ssl.create_default_context())
        return ctx.wrap_socket(sock, server_hostname=host)

    def _open_socket(self, host: str, port: int) -> ssl.SSLSocket:
        if not self._proxy_url:
            raw = socket.create_connection((host, port), timeout=self._connect_timeout)
            return self._tls(raw, host)
        p = urlsplit(self._proxy_url)
        if not p.hostname:
            raise WT.WSConnectionError("proxy url has no host")
        raw = socket.create_connection((p.hostname, p.port or 3128), timeout=self._connect_timeout)
        try:
            lines = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
            if p.username:
                token = base64.b64encode(f"{p.username}:{p.password or ''}".encode()).decode()
                lines.append(f"Proxy-Authorization: Basic {token}")
            raw.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
            raw.settimeout(self._connect_timeout)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = raw.recv(4096)
                if not chunk:
                    raise WT.WSConnectionError("proxy closed during CONNECT")
                buf += chunk
                if len(buf) > 65536:
                    raise WT.WSConnectionError("proxy answer too large")
            status = buf.split(b"\r\n", 1)[0].decode("iso-8859-1", "replace")
            if " 200" not in status:
                raise WT.WSConnectionError(f"proxy refused CONNECT: {status!r}")
            return self._tls(raw, host)
        except Exception:
            try:
                raw.close()
            except OSError:
                pass
            raise

    # -- frames ---------------------------------------------------------------------------

    def _read_header(self, sock: socket.socket, timeout: Optional[float]):
        try:
            sock.settimeout(timeout)
            first = self._recv_exact(sock, 1)
        except socket.timeout:
            raise
        except OSError as exc:
            raise WT.WSClosed(f"recv failed: {exc}") from exc
        try:
            sock.settimeout(self.MID_FRAME_TIMEOUT_S)
            second = self._recv_exact(sock, 1)
        except socket.timeout as exc:
            raise WT.WSClosed("timed out in the middle of a frame") from exc
        except OSError as exc:
            raise WT.WSClosed(f"recv failed: {exc}") from exc
        return first[0], second[0]

    def _read_frame(self, sock: socket.socket, timeout: Optional[float]) -> Tuple[bool, int, bytes]:
        b0, b1 = self._read_header(sock, timeout)
        fin, opcode = bool(b0 & 0x80), b0 & 0x0F
        if b0 & 0x70:
            raise WT.WSProtocolError("reserved bits set (no extension was negotiated)")
        if b1 & 0x80:
            raise WT.WSProtocolError("server frame was masked; RFC 6455 forbids this")
        length = b1 & 0x7F
        try:
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(sock, 2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(sock, 8))[0]
            if length > self.MAX_MESSAGE_BYTES:
                raise WT.WSProtocolError(f"frame announces {length} bytes")
            payload = self._recv_exact(sock, length) if length else b""
        except socket.timeout as exc:
            raise WT.WSClosed("timed out in the middle of a frame") from exc
        except OSError as exc:
            raise WT.WSClosed(f"recv failed: {exc}") from exc
        return fin, opcode, payload

    def recv_message(self, *, timeout: Optional[float] = None) -> Tuple[int, bytes]:
        """One complete message: (opcode, payload). Text and binary messages are
        reassembled from continuation frames; a ping is answered with a pong and
        returned as OP_PING; a pong is returned as OP_PONG; close raises."""
        sock = self._sock
        if sock is None:
            raise WT.WSClosed("not connected")
        fin, opcode, payload = self._read_frame(sock, timeout)
        if opcode in (OP_PING, OP_PONG, OP_CLOSE):
            if not fin or len(payload) > 125:
                raise WT.WSProtocolError("control frame must be final and <= 125 bytes")
            if opcode == OP_CLOSE:
                raise WT.WSClosed(f"server sent close frame: {payload!r}")
            if opcode == OP_PING:
                self.send_pong(payload)
            return opcode, payload
        if opcode == OP_CONT:
            raise WT.WSProtocolError("continuation frame with nothing to continue")
        if opcode not in (OP_TEXT, OP_BINARY):
            raise WT.WSProtocolError(f"unsupported opcode {opcode}")
        parts = [payload]
        total = len(payload)
        first_op = opcode
        while not fin:
            frame_fin, opcode, more = self._read_frame(sock, self.MID_FRAME_TIMEOUT_S)
            if opcode in (OP_PING, OP_PONG):          # control frames may interleave; they
                if opcode == OP_PING:                  # say nothing about the message's FIN
                    self.send_pong(more)
                continue
            if opcode == OP_CLOSE:
                raise WT.WSClosed(f"server sent close frame: {more!r}")
            if opcode != OP_CONT:
                raise WT.WSProtocolError("new data frame inside a fragmented message")
            fin = frame_fin
            total += len(more)
            if total > self.MAX_MESSAGE_BYTES:
                raise WT.WSProtocolError("reassembled message too large")
            parts.append(more)
        return first_op, b"".join(parts)
