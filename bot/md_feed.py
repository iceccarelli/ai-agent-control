"""The live public feed: connections, heartbeats, resyncs, REST bootstrap.

Two :class:`StreamWorker` s (linear perpetual, spot) each own one WebSocket and
run the same loop: connect, subscribe, read, and on any failure record it,
back off, and start again. A :class:`RestWorker` bootstraps and cross-checks
over public REST. Every input is appended to the durable log and folded into the
market state through `md_engine.Engine`, so the live run is replayable.

Failure handling, each of it exercised by the tests against a scripted venue:

* **Interruption / reconnect**: the state is told the connection dropped (books
  and tickers are invalidated, because frames were missed), the worker backs off
  exponentially with jitter, reconnects and resubscribes. Data is not trusted
  again until the new snapshots arrive.
* **Gap / crossed / off-tick book**: the engine marks the book unsynced; the
  worker unsubscribes and resubscribes that topic to get a fresh snapshot, and
  reconnects if none arrives within `resync_timeout_s`.
* **Heartbeat**: an application ping every `ping_interval_s`; a missing pong or
  total silence is a lost heartbeat and a reconnect.
* **Rate limits**: subscribe requests are chunked to the venue's argument limit,
  reconnects are spaced by the backoff, and REST uses a token bucket with a
  ban-window stop.

No credentials, no private channel, no order path.
"""
from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

import md_engine as EN
import md_events as E
import md_rest as R
import md_store as ST
import ws_transport as WT

WS_BASE_MAINNET = "wss://stream.bybit.com"
WS_BASE_TESTNET = "wss://stream-testnet.bybit.com"
#: environment -> (websocket base, REST base). The feed's environment must equal
#: the engine's venue: testnet prices never drive a mainnet book or the reverse.
ENDPOINTS = {"mainnet": (WS_BASE_MAINNET, R.MAINNET), "testnet": (WS_BASE_TESTNET, R.TESTNET)}
#: Bybit allows at most 10 args per subscribe request on spot; the same chunk is
#: used for both products.
SUBSCRIBE_CHUNK = 10


@dataclass(frozen=True)
class FeedConfig:
    symbol: str = "BTCUSDT"
    environment: str = "mainnet"
    ws_base: str = ""                 # "" = the environment's public stream
    depth: int = 50
    trades: bool = True
    ping_interval_s: float = 20.0
    pong_timeout_s: float = 10.0
    silence_timeout_s: float = 15.0
    resync_timeout_s: float = 5.0
    backoff_base_s: float = 1.0
    backoff_max_s: float = 60.0
    stable_after_s: float = 60.0
    rest_refresh_s: float = 30.0
    rest_retry_s: float = 60.0
    rest_geo_retry_s: float = 600.0
    funding_refresh_after_settle_s: float = 45.0
    view_log_interval_s: float = 5.0
    recv_timeout_s: float = 1.0

    def __post_init__(self) -> None:
        if self.environment not in ENDPOINTS:
            raise ValueError(f"environment {self.environment!r}")

    @property
    def resolved_ws_base(self) -> str:
        return self.ws_base or ENDPOINTS[self.environment][0]

    @property
    def rest_base(self) -> str:
        return ENDPOINTS[self.environment][1]

    def url(self, category: str) -> str:
        return f"{self.resolved_ws_base}/v5/public/{category}"

    def topics(self) -> List[str]:
        t = [f"orderbook.{self.depth}.{self.symbol}", f"tickers.{self.symbol}"]
        if self.trades:
            t.append(f"publicTrade.{self.symbol}")
        return t


Clock = Callable[[], float]


def _wall_ms() -> int:
    return int(time.time() * 1000)


class StreamWorker:
    """One public WebSocket connection (one product)."""

    def __init__(self, category: str, cfg: FeedConfig, engine: EN.Engine,
                 transport_factory: Callable[[str], Any], *, mono: Clock = time.monotonic,
                 wall_ms: Callable[[], int] = _wall_ms, rng: Optional[random.Random] = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.category, self.cfg, self.engine = category, cfg, engine
        self._factory = transport_factory
        self.mono, self.wall_ms, self._sleep = mono, wall_ms, sleep
        self._rng = rng or random.Random()
        self.ws: Any = None
        self.conn_seq = 0
        self.conn_id = ""
        self.connected_at: Optional[float] = None
        self.last_frame_mono: Optional[float] = None
        self.last_ping_mono: Optional[float] = None
        self.resync_since: Optional[float] = None
        self.failures = 0
        self.next_attempt: float = 0.0
        self.reconnects = 0
        self.stop = threading.Event()

    # -- event helpers ----------------------------------------------------------------------

    def _conn_event(self, event: str, reason: str = "") -> EN.ApplyOutcome:
        return self.engine.record(
            ST.CONN, wall_ms=self.wall_ms(), mono_s=self.mono(), category=self.category,
            conn_id=self.conn_id, topic="", payload=json.dumps({"event": event, "reason": reason}))

    def _frame(self, text: str) -> EN.ApplyOutcome:
        return self.engine.record(ST.FRAME, wall_ms=self.wall_ms(), mono_s=self.mono(),
                                  category=self.category, conn_id=self.conn_id, topic="",
                                  payload=text)

    # -- connection lifecycle ------------------------------------------------------------------

    def _connect(self) -> None:
        self.conn_seq += 1
        self.conn_id = f"{self.category}-{self.conn_seq}"
        ws = self._factory(self.cfg.url(self.category))
        ws.connect()
        self.ws = ws
        now = self.mono()
        self.connected_at = self.last_frame_mono = self.last_ping_mono = now
        self.resync_since = None
        self._conn_event("connected")
        topics = self.cfg.topics()
        for i in range(0, len(topics), SUBSCRIBE_CHUNK):
            ws.send_text(json.dumps({"op": "subscribe", "args": topics[i:i + SUBSCRIBE_CHUNK]}))

    def _drop(self, reason: str) -> None:
        ws, self.ws = self.ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self.connected_at is not None and self.mono() - self.connected_at >= self.cfg.stable_after_s:
            self.failures = 0
        self.connected_at = None
        self._conn_event("disconnected", reason)
        self.failures += 1
        delay = min(self.cfg.backoff_max_s, self.cfg.backoff_base_s * (2 ** (self.failures - 1)))
        delay *= 0.5 + self._rng.random() * 0.5          # jitter: 50-100% of the step
        self.next_attempt = self.mono() + delay
        self.reconnects += 1

    def _resubscribe_book(self) -> None:
        topic = f"orderbook.{self.cfg.depth}.{self.cfg.symbol}"
        try:
            self.ws.send_text(json.dumps({"op": "unsubscribe", "args": [topic]}))
            self.ws.send_text(json.dumps({"op": "subscribe", "args": [topic]}))
        except WT.WSError as exc:
            self._drop(f"resubscribe failed: {exc}")
            return
        self.resync_since = self.mono()
        self._conn_event("invalidate", "RESUBSCRIBED")

    # -- one iteration (the whole loop is `while not stop: step()`) -----------------------------------

    def step(self) -> None:
        now = self.mono()
        if self.ws is None:
            if now < self.next_attempt:
                self._sleep(min(self.cfg.recv_timeout_s, self.next_attempt - now))
                return
            try:
                self._connect()
            except Exception as exc:  # noqa: BLE001 - any connect failure is a retry
                self.ws = None
                self.connected_at = None
                self._conn_event("disconnected", f"connect failed: {type(exc).__name__}: {exc}")
                self.failures += 1
                delay = min(self.cfg.backoff_max_s,
                            self.cfg.backoff_base_s * (2 ** (self.failures - 1)))
                self.next_attempt = self.mono() + delay * (0.5 + self._rng.random() * 0.5)
            return
        if now - (self.last_ping_mono or now) >= self.cfg.ping_interval_s:
            try:
                self.ws.send_text(json.dumps({"op": "ping"}))
                self.last_ping_mono = now
            except WT.WSError as exc:
                self._drop(f"ping failed: {exc}")
                return
        pong = self.engine.last_pong_mono.get(self.category, self.connected_at or now)
        if now - pong > self.cfg.ping_interval_s + self.cfg.pong_timeout_s:
            self._drop("HEARTBEAT_LOST: no pong")
            return
        if now - (self.last_frame_mono or now) > self.cfg.silence_timeout_s:
            self._drop("STALE_FEED: no frame")
            return
        if self.resync_since is not None and now - self.resync_since > self.cfg.resync_timeout_s:
            self._drop("RESYNC_TIMEOUT: no snapshot after resubscribe")
            return
        try:
            op, data = self.ws.recv_message(timeout=self.cfg.recv_timeout_s)
        except TimeoutError:
            return
        except OSError as exc:
            if isinstance(exc, (WT.WSClosed,)) or not isinstance(exc, TimeoutError):
                if exc.__class__.__name__ == "timeout" or "timed out" in str(exc):
                    return
            self._drop(f"{type(exc).__name__}: {exc}")
            return
        except WT.WSError as exc:
            self._drop(f"{type(exc).__name__}: {exc}")
            return
        self.last_frame_mono = self.mono()
        if op != WT.OP_TEXT:
            return
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            self._drop("BINARY_GARBAGE: frame is not UTF-8")
            return
        out = self._frame(text)
        for inc in out.incidents:
            if inc.kind in (E.SEQUENCE_GAP, E.RESYNC_REQUESTED, E.OUT_OF_ORDER) \
                    and inc.data.get("category") == self.category and self.resync_since is None:
                self._resubscribe_book()
                break
        # a fresh snapshot ends a resync
        if self.resync_since is not None and self.engine.state.books[self.category].synced:
            self.resync_since = None

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001 - the worker must outlive any one bug
                self._drop(f"WORKER_ERROR {type(exc).__name__}: {exc}")
                self._sleep(self.cfg.recv_timeout_s)
        if self.ws is not None:
            self._drop("stopped")


class RestWorker:
    """Bootstrap and periodically cross-check over public REST, logging every
    exchange (failures included) to the durable log."""

    def __init__(self, cfg: FeedConfig, engine: EN.Engine, client: R.PublicRest, *,
                 mono: Clock = time.monotonic, wall_ms: Callable[[], int] = _wall_ms,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg, self.engine, self.client = cfg, engine, client
        self.mono, self.wall_ms, self._sleep = mono, wall_ms, sleep
        self.bootstrapped = False
        self.next_refresh = 0.0
        self.next_funding_refresh = 0.0
        self.last_error = ""
        self.stop = threading.Event()

    def _do(self, endpoint: str, params: Dict[str, Any], category: str) -> bool:
        try:
            status, body = self.client.get(endpoint, params)
        except R.RestBlocked as exc:
            self.last_error = f"BLOCKED: {exc}"
            return False
        except R.RestTransportError as exc:
            status, body = 0, f"transport error: {exc}"
            self.last_error = body
        payload = json.dumps({"endpoint": endpoint, "params": params, "http_status": status,
                              "body": body}, sort_keys=True)
        self.engine.record(ST.REST, wall_ms=self.wall_ms(), mono_s=self.mono(), category=category,
                           conn_id="rest", topic=endpoint, payload=payload)
        ok = status == 200
        if not ok:
            self.last_error = f"HTTP {status}"
        return ok

    def step(self) -> None:
        now = self.mono()
        if now < self.next_refresh:
            return
        sym = self.cfg.symbol
        plan = R.refresh_plan(sym) if self.bootstrapped else R.bootstrap_plan(sym)
        if now >= self.next_funding_refresh and self.bootstrapped:
            plan = plan + [p for p in R.bootstrap_plan(sym) if "funding" in p[0]]
        all_ok = True
        for endpoint, params, cat in plan:
            all_ok = self._do(endpoint, params, cat) and all_ok
        geo = self.engine.state.rejections.get(E.GEO_BLOCKED, 0) > 0 and not all_ok
        if not self.bootstrapped:
            self.bootstrapped = all_ok
        retry = (self.cfg.rest_geo_retry_s if geo else self.cfg.rest_retry_s)
        self.next_refresh = now + (self.cfg.rest_refresh_s if all_ok else retry)
        if all_ok:
            nxt = self.engine.state.tickers[E.LINEAR].fields.get("nextFundingTime")
            wall = self.wall_ms()
            if nxt and wall > nxt + self.cfg.funding_refresh_after_settle_s * 1000:
                self.next_funding_refresh = now
            else:
                self.next_funding_refresh = now + 1800.0

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
            self._sleep(1.0)


class Feed:
    """Owns the workers, the view logger and the threads."""

    def __init__(self, cfg: FeedConfig, engine: EN.Engine, transport_factory: Callable[[str], Any],
                 rest_client: Optional[R.PublicRest] = None, *, mono: Clock = time.monotonic,
                 wall_ms: Callable[[], int] = _wall_ms, rng: Optional[random.Random] = None) -> None:
        self.cfg, self.engine, self.mono, self.wall_ms = cfg, engine, mono, wall_ms
        self.streams: Dict[str, StreamWorker] = {
            c: StreamWorker(c, cfg, engine, transport_factory, mono=mono, wall_ms=wall_ms,
                            rng=rng) for c in (E.LINEAR, E.SPOT)}
        self.rest = RestWorker(cfg, engine, rest_client, mono=mono, wall_ms=wall_ms) \
            if rest_client is not None else None
        self._threads: List[threading.Thread] = []
        self._stop = threading.Event()

    def view(self):
        """The current validated view (not logged)."""
        with self.engine.lock:
            return self.engine.state.view(self.mono(), self.wall_ms())

    def _view_logger(self) -> None:
        while not self._stop.wait(self.cfg.view_log_interval_s):
            try:
                self.engine.log_view(self.mono(), self.wall_ms())
            except Exception:  # noqa: BLE001
                pass

    def start(self) -> None:
        for w in list(self.streams.values()) + ([self.rest] if self.rest else []):
            t = threading.Thread(target=w.run, name=f"md-{getattr(w, 'category', 'rest')}",
                                 daemon=True)
            t.start()
            self._threads.append(t)
        t = threading.Thread(target=self._view_logger, name="md-views", daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self, join_s: float = 5.0) -> None:
        self._stop.set()
        for w in list(self.streams.values()) + ([self.rest] if self.rest else []):
            w.stop.set()
        for t in self._threads:
            t.join(join_s)
        if self.engine.store is not None:
            self.engine.store.flush()
