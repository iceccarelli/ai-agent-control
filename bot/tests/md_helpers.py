"""Helpers for the market-data tests: the real-frame fixture, a fake clock and a
scripted venue WebSocket. The fixture (`fixtures/md/public_feed_20261104.jsonl.gz`)
is verbatim public Bybit v5 traffic captured from the live mainnet public feed
on 2026-11-04 (no credentials involved): 850 frames of BTCUSDT linear and spot
books, tickers and trades, with the local receive time of each."""
from __future__ import annotations

import gzip
import json
import os
import sys
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ws_transport as WT  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "md", "public_feed_20261104.jsonl.gz")
BYBIT_STUDY = os.path.join(os.path.dirname(HERE), "research", "exchange_study", "bybit")


def load_frames() -> List[Dict[str, Any]]:
    with gzip.open(FIXTURE, "rt") as fh:
        return [json.loads(line) for line in fh]


def load_study(name: str) -> Any:
    with gzip.open(os.path.join(BYBIT_STUDY, name), "rt") as fh:
        return json.load(fh)


class FakeClock:
    """A controllable monotonic + wall clock. Time moves only when told."""

    def __init__(self, mono: float = 1000.0, wall_ms: int = 1_793_820_000_000) -> None:
        self.m, self.w = mono, wall_ms

    def mono(self) -> float:
        return self.m

    def wall_ms(self) -> int:
        return self.w

    def advance(self, seconds: float) -> None:
        self.m += seconds
        self.w += int(seconds * 1000)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)


class ScriptedWS:
    """A venue WebSocket that plays a script. Items: ("text", str), ("timeout",),
    ("raise", Exception). A sent {"op":"ping"} queues a pong, like the venue."""

    def __init__(self, script: Optional[List[tuple]] = None, *, fail_connect: Optional[Exception] = None,
                 auto_pong: bool = True) -> None:
        self.script = list(script or [])
        self.sent: List[Dict[str, Any]] = []
        self.fail_connect = fail_connect
        self.connected = False
        self.closed = False
        self.auto_pong = auto_pong

    def connect(self) -> None:
        if self.fail_connect:
            raise self.fail_connect
        self.connected = True

    def send_text(self, text: str) -> None:
        msg = json.loads(text)
        self.sent.append(msg)
        if msg.get("op") == "ping" and self.auto_pong:
            self.script.insert(0, ("text", json.dumps({"success": True, "ret_msg": "pong", "op": "ping"})))

    def push(self, *items: tuple) -> None:
        self.script.extend(items)

    def recv_message(self, *, timeout: Optional[float] = None):
        if not self.script:
            raise TimeoutError("scripted: nothing to read")
        item = self.script.pop(0)
        if item[0] == "text":
            return WT.OP_TEXT, item[1].encode()
        if item[0] == "binary":
            return WT.OP_BINARY, item[1]
        if item[0] == "timeout":
            raise TimeoutError("scripted timeout")
        if item[0] == "raise":
            raise item[1]
        raise AssertionError(item)

    def close(self) -> None:
        self.closed = True
        self.connected = False


class Factory:
    """Hands out one scripted socket per connect, in order, and records the URLs."""

    def __init__(self, *sockets: ScriptedWS) -> None:
        self.sockets = list(sockets)
        self.urls: List[str] = []
        self.made: List[ScriptedWS] = []

    def __call__(self, url: str) -> ScriptedWS:
        self.urls.append(url)
        ws = self.sockets.pop(0) if self.sockets else ScriptedWS()
        self.made.append(ws)
        return ws


def book_frame(topic: str, typ: str, ts: int, bids, asks, u: int, seq: int, symbol: str = "BTCUSDT",
               cts: Optional[int] = None) -> str:
    j = {"topic": topic, "type": typ, "ts": ts,
         "data": {"s": symbol, "b": [[str(p), str(q)] for p, q in bids],
                  "a": [[str(p), str(q)] for p, q in asks], "u": u, "seq": seq},
         "cts": cts if cts is not None else ts - 1}
    return json.dumps(j)


def ticker_frame(typ: str, ts: int, **fields) -> str:
    return json.dumps({"topic": "tickers.BTCUSDT", "type": typ, "ts": ts, "cs": ts,
                       "data": dict({"symbol": "BTCUSDT"}, **{k: str(v) for k, v in fields.items()})})
