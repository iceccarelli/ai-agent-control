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
* **Gap / crossed / off-tick / rejected book frame**: the engine marks the book
  unsynced; the worker unsubscribes and resubscribes that topic to get a fresh
  snapshot, and reconnects if none arrives within `resync_timeout_s`.
* **A silent topic**: other traffic can flow while the order-book topic is dead;
  a book that has not been updated for `book_silence_s` is resubscribed, and
  reconnected if that does not bring it back.
* **Heartbeat**: an application ping every `ping_interval_s`; a missing pong or
  total silence is a lost heartbeat and a reconnect. Binary frames never count as
  traffic.
* **A failing store or a worker bug** never ends a thread: the failure is
  recorded, entries stop (`store_fault`), and the worker keeps trying.
* **Rate limits**: subscribe requests are chunked to the venue's argument limit,
  reconnects are spaced by the capped backoff, REST uses a token bucket with a
  ban-window stop, and the settled funding print is re-read when a new settlement
  is due (not on a timer that is pushed forward by every other refresh).

No credentials, no private channel, no order path.
"""
from __future__ import annotations

import collections.abc
import json
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import md_engine as EN
import md_events as E
import md_rest as R
import md_store as ST
import venues as _venues
import ws_transport as WT

WS_BASE_MAINNET = "wss://" + _venues.get("mainnet").ws_private_host
WS_BASE_TESTNET = "wss://" + _venues.get("testnet").ws_private_host
#: environment -> (websocket base, REST base). The feed's environment must equal
#: the engine's venue: testnet prices never drive a mainnet book or the reverse.
ENDPOINTS = {"mainnet": (WS_BASE_MAINNET, R.MAINNET), "testnet": (WS_BASE_TESTNET, R.TESTNET)}
#: Bybit allows at most 10 args per subscribe request on spot; the same chunk is
#: used for both products.
SUBSCRIBE_CHUNK = 10
#: 2**16 seconds is far past any cap; bounding the exponent keeps the arithmetic finite
#: however long the venue stays unreachable.
MAX_BACKOFF_EXPONENT = 16


class ReadOnlyMapping(collections.abc.Mapping):
    """An immutable copy of a mapping: reads work, any assignment raises TypeError, and a second ``__init__`` is refused. It is
    immutable against accident and against ordinary code; it is NOT a defence against code that deliberately reaches into ``_d``
    (Python has no such thing), and nothing in this repository does."""
    __slots__ = ("_d",)

    def __init__(self, d: Any) -> None:
        try:
            object.__getattribute__(self, "_d")
        except AttributeError:
            object.__setattr__(self, "_d", dict(d))
            return
        raise TypeError("read-only: already initialised")

    def __delattr__(self, *_a: Any) -> None:
        raise TypeError("read-only")

    def __getitem__(self, k: Any) -> Any:
        return self._d[k]

    def __iter__(self):
        return iter(self._d)

    def __len__(self) -> int:
        return len(self._d)

    def __setattr__(self, *_a: Any) -> None:
        raise TypeError("read-only")

    def __repr__(self) -> str:
        return f"ReadOnlyMapping({self._d!r})"


@dataclass(frozen=True)
class MarketSnapshot:
    """A view, the identifiers of the market events it reflects, and the wall/monotonic clocks it was taken at: one
    read, one instant. Immutable; ``source`` is a read-only mapping."""
    view: Any
    source: Any
    wall_ms: int
    mono_s: float


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
    book_silence_s: float = 10.0
    backoff_base_s: float = 1.0
    backoff_max_s: float = 60.0
    stable_after_s: float = 60.0
    rest_refresh_s: float = 30.0
    rest_retry_s: float = 60.0
    rest_geo_retry_s: float = 600.0
    funding_grace_s: float = 45.0
    funding_retry_s: float = 60.0
    specs_refresh_s: float = 3600.0
    view_log_interval_s: float = 5.0
    #: UTC days of closed segments to keep (0 = keep all, an operator decision), and how
    #: often retention is applied. A segment is only ever removed whole and from the
    #: oldest end, with a chain checkpoint, so the rest still verifies.
    retention_days: int = 0
    retention_every_s: float = 3600.0
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


def _backoff(cfg: FeedConfig, failures: int, rng: random.Random) -> float:
    step = cfg.backoff_base_s * (2 ** min(max(failures - 1, 0), MAX_BACKOFF_EXPONENT))
    return min(cfg.backoff_max_s, step) * (0.5 + rng.random() * 0.5)      # jitter: 50-100%


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
        self.book_resubscribed_at: Optional[float] = None
        self.failures = 0
        self.next_attempt: float = 0.0
        self.reconnects = 0
        self.binary_frames = 0
        self.errors = 0
        self.last_error = ""
        self.stop = threading.Event()

    # -- event helpers (clocks are sampled inside the engine's lock) ----------------------------

    def _conn_event(self, event: str, reason: str = "") -> EN.ApplyOutcome:
        return self.engine.record(
            ST.CONN, wall_ms=self.wall_ms, mono_s=self.mono, category=self.category,
            conn_id=self.conn_id, topic="", payload=json.dumps({"event": event, "reason": reason}))

    def _frame(self, text: str) -> EN.ApplyOutcome:
        return self.engine.record(ST.FRAME, wall_ms=self.wall_ms, mono_s=self.mono,
                                  category=self.category, conn_id=self.conn_id, topic="",
                                  payload=text)

    # -- connection lifecycle ------------------------------------------------------------------

    def _connect(self) -> None:
        self.conn_seq += 1
        self.conn_id = f"{self.category}-{self.conn_seq}"
        ws = self._factory(self.cfg.url(self.category))
        try:
            ws.connect()
            now = self.mono()
            self.connected_at = self.last_frame_mono = self.last_ping_mono = now
            self.resync_since = self.book_resubscribed_at = None
            self.ws = ws
            self._conn_event("connected")
            topics = self.cfg.topics()
            for i in range(0, len(topics), SUBSCRIBE_CHUNK):
                ws.send_text(json.dumps({"op": "subscribe", "args": topics[i:i + SUBSCRIBE_CHUNK]}))
        except BaseException:
            try:                                  # a half-open socket must not leak
                ws.close()
            except Exception:  # noqa: BLE001
                pass
            self.ws = None
            raise

    def _fail(self, reason: str) -> None:
        """Common tail of every disconnect: count it and schedule the retry."""
        self.failures += 1
        self.reconnects += 1
        self.next_attempt = self.mono() + _backoff(self.cfg, self.failures, self._rng)
        self.last_error = reason

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
        # The market state is told directly AND the event is logged, in one critical
        # section (no logged view can fall between them, so replay agrees). Whatever
        # happens to the log, a dropped connection is never believed live.
        with self.engine.lock:
            self.engine.state.on_disconnected(self.category)
            try:
                self._conn_event("disconnected", reason)
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                self.last_error = f"log write failed: {type(exc).__name__}: {exc}"
        self._fail(reason)

    def _resubscribe_book(self) -> None:
        topic = f"orderbook.{self.cfg.depth}.{self.cfg.symbol}"
        try:
            self.ws.send_text(json.dumps({"op": "unsubscribe", "args": [topic]}))
            self.ws.send_text(json.dumps({"op": "subscribe", "args": [topic]}))
        except WT.WSError as exc:
            self._drop(f"resubscribe failed: {exc}")
            return
        now = self.mono()
        self.resync_since = self.book_resubscribed_at = now
        self._conn_event("invalidate", "RESUBSCRIBED")

    def _book_liveness(self, now: float) -> bool:
        """A book topic can die while tickers and pongs keep the socket looking
        busy. If the book has been silent for `book_silence_s`, resubscribe it once;
        if that does not bring it back within another `book_silence_s`, reconnect.
        Returns True when it dropped the connection."""
        if self.resync_since is not None:
            return False                              # a resync is already in flight
        book = self.engine.state.books[self.category]
        if book.synced and book.last_applied_mono_s is not None:
            last = book.last_applied_mono_s
        else:                                         # unsynced: judge from the last attempt to start it
            last = self.book_resubscribed_at or self.connected_at or now
        if now - last <= self.cfg.book_silence_s:
            return False
        resubscribed_since_last_update = (self.book_resubscribed_at is not None
                                          and self.book_resubscribed_at >= last)
        if not resubscribed_since_last_update:
            self._resubscribe_book()
            return self.ws is None
        if now - self.book_resubscribed_at > self.cfg.book_silence_s:
            self._drop("BOOK_TOPIC_SILENT: a resubscribe did not bring the book back")
            return True
        return False

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
                self.connected_at = None
                self.ws = None
                with self.engine.lock:
                    self.engine.state.on_disconnected(self.category)
                    try:
                        self._conn_event("disconnected", f"connect failed: {type(exc).__name__}: {exc}")
                    except Exception as log_exc:  # noqa: BLE001
                        self.errors += 1
                        self.last_error = f"log write failed: {type(log_exc).__name__}: {log_exc}"
                self._fail(f"connect failed: {type(exc).__name__}: {exc}")
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
        if self._book_liveness(now):
            return
        try:
            op, data = self.ws.recv_message(timeout=self.cfg.recv_timeout_s)
        except TimeoutError:                      # nothing arrived within the wait: idle, not dead
            return
        except WT.WSError as exc:
            self._drop(f"{type(exc).__name__}: {exc}")
            return
        except OSError as exc:
            if "timed out" in str(exc):
                return
            self._drop(f"{type(exc).__name__}: {exc}")
            return
        if op != WT.OP_TEXT:
            if op == WT.OP_BINARY:
                self.binary_frames += 1           # not traffic: it must not keep a dead feed alive
            else:
                self.last_frame_mono = self.mono()   # a pong/ping proves the socket, not the data
            return
        self.last_frame_mono = self.mono()
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
        if self.resync_since is not None and self.engine.state.books[self.category].synced:
            self.resync_since = None              # a fresh snapshot ends a resync

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001 - the worker must outlive any one bug
                self.errors += 1
                self.last_error = f"WORKER_ERROR {type(exc).__name__}: {exc}"
                try:
                    self._drop(self.last_error)
                except Exception:  # noqa: BLE001 - even a failing drop must not end the thread
                    self.ws = None
                    self.next_attempt = self.mono() + self.cfg.backoff_max_s
                self._sleep(self.cfg.recv_timeout_s)
        if self.ws is not None:
            try:
                self._drop("stopped")
            except Exception:  # noqa: BLE001
                pass


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
        self.next_funding_try = 0.0
        self.next_specs = 0.0
        self.last_error = ""
        self.errors = 0
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
        self.engine.record(ST.REST, wall_ms=self.wall_ms, mono_s=self.mono, category=category,
                           conn_id="rest", topic=endpoint, payload=payload)
        ok = status == 200
        if not ok:
            self.last_error = f"HTTP {status}"
        return ok

    def _funding_due(self, wall_ms: int) -> bool:
        """A new settlement should exist and we do not hold it. Never on a timer
        that other refreshes can push forward."""
        st = self.engine.state
        lp = st.last_print
        if lp is None:
            return True
        hours = st.tickers[E.LINEAR].fields.get("fundingIntervalHour")
        interval_ms = int(hours * 3_600_000) if isinstance(hours, (int, float)) and hours > 0 else 8 * 3_600_000
        return wall_ms > lp.settled_ms + interval_ms + int(self.cfg.funding_grace_s * 1000)

    def step(self) -> None:
        now = self.mono()
        if now < self.next_refresh:
            return
        sym = self.cfg.symbol
        wall = self.wall_ms()
        if not self.bootstrapped:
            plan = R.bootstrap_plan(sym)
        else:
            plan = list(R.refresh_plan(sym))
            if self._funding_due(wall) and now >= self.next_funding_try:
                plan += [p for p in R.bootstrap_plan(sym) if "funding" in p[0]]
                self.next_funding_try = now + self.cfg.funding_retry_s
            if now >= self.next_specs:
                plan += [p for p in R.bootstrap_plan(sym) if "instruments" in p[0]]
                self.next_specs = now + self.cfg.specs_refresh_s
        all_ok = True
        for endpoint, params, cat in plan:
            all_ok = self._do(endpoint, params, cat) and all_ok
        geo = self.engine.state.rejections.get(E.GEO_BLOCKED, 0) > 0 and not all_ok
        if not self.bootstrapped:
            self.bootstrapped = all_ok
            if all_ok:
                self.next_specs = now + self.cfg.specs_refresh_s
        self.next_refresh = now + (self.cfg.rest_refresh_s if all_ok else
                                   (self.cfg.rest_geo_retry_s if geo else self.cfg.rest_retry_s))

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.next_refresh = self.mono() + self.cfg.rest_retry_s     # never a hot retry loop
            self._sleep(1.0)


class Feed:
    """Owns the workers, the view logger and the threads."""

    def __init__(self, cfg: FeedConfig, engine: EN.Engine, transport_factory: Callable[[str], Any],
                 rest_client: Optional[R.PublicRest] = None, *, mono: Clock = time.monotonic,
                 wall_ms: Callable[[], int] = _wall_ms, rng: Optional[random.Random] = None) -> None:
        if engine.state.environment != cfg.environment or engine.state.symbol != cfg.symbol:
            raise ValueError("the engine's market state and the feed config disagree about "
                             f"the environment/symbol ({engine.state.environment}/{engine.state.symbol} "
                             f"vs {cfg.environment}/{cfg.symbol})")
        self.cfg, self.engine, self.mono, self.wall_ms = cfg, engine, mono, wall_ms
        self.streams: Dict[str, StreamWorker] = {
            c: StreamWorker(c, cfg, engine, transport_factory, mono=mono, wall_ms=wall_ms,
                            rng=rng) for c in (E.LINEAR, E.SPOT)}
        self.rest = RestWorker(cfg, engine, rest_client, mono=mono, wall_ms=wall_ms) \
            if rest_client is not None else None
        self._threads: List[threading.Thread] = []
        self._stop = threading.Event()
        self.view_errors = 0
        self.retention_errors = 0
        self.emergency_may_prune: Optional[Callable[[str], bool]] = None   # default-deny: set only by an owner who named a verified backup

    def view(self):
        """The current validated view (not logged)."""
        with self.engine.lock:
            return self.engine.state.view(self.mono(), self.wall_ms())

    def snapshot(self, identify, wall_ms=None):
        """ONE immutable snapshot: the validated view, the event identifiers it was computed at and the clocks, all read
        under the engine lock that every market event is applied under. Reading the view and then, separately, the
        identifiers lets an event land in between, and the row would then name a later event than the one its numbers
        came from. ``identify(engine)`` is called while the lock is held and must only READ ids."""
        with self.engine.lock:
            mono, wall = self.mono(), int((wall_ms or self.wall_ms)())
            view = self.engine.state.view(mono, wall)
            ids = dict(identify(self.engine))
        return MarketSnapshot(view=view, source=ReadOnlyMapping(ids), wall_ms=wall, mono_s=mono)

    def alive(self) -> bool:
        """Every worker thread is running. A dead thread is a feed that will never recover."""
        return bool(self._threads) and all(t.is_alive() for t in self._threads)

    def status(self) -> Dict[str, Any]:
        return {"threads_alive": self.alive(), "store_fault": self.engine.state.store_fault,
                "view_errors": self.view_errors, "retention_errors": self.retention_errors,
                "streams": {c: {"connected": w.ws is not None, "reconnects": w.reconnects,
                                "errors": w.errors, "last_error": w.last_error,
                                "binary_frames": w.binary_frames} for c, w in self.streams.items()},
                "rest": None if self.rest is None else {"bootstrapped": self.rest.bootstrapped,
                                                        "errors": self.rest.errors,
                                                        "last_error": self.rest.last_error}}

    def _view_logger(self) -> None:
        next_retention = self.mono()
        while not self._stop.wait(self.cfg.view_log_interval_s):
            try:
                self.engine.log_view(self.mono(), self.wall_ms())
            except Exception:  # noqa: BLE001
                self.view_errors += 1
            if self.cfg.retention_days > 0 and self.mono() >= next_retention:
                next_retention = self.mono() + self.cfg.retention_every_s
                self.apply_retention()
            self.emergency_retention()

    def emergency_retention(self) -> List[str]:
        """When the volume is below its reserve, first run the AGE-based retention now (the same rule, never a segment inside the retention window, which the hourly pass would
        otherwise leave for up to ~59 minutes after every UTC midnight: the day that just expired is exactly what frees the space the next day's segment needs), and only if that
        frees nothing free the OLDEST closed segment under the backup-key rule (a volume that fills inside one retention window would sit in FAULT for ever). Counted in
        ``emergency_prunes``; a failure is counted, never raised into the feed."""
        store = self.engine.store
        if store is None or not hasattr(store, "low_space") or self.cfg.retention_days <= 0:     # retention 0 = keep everything
            return []
        try:
            if not store.low_space():
                return []
            expired = self.apply_retention()
            if expired:
                return expired
            with self.engine.lock:
                return store.emergency_prune(self.emergency_may_prune)
        except Exception:  # noqa: BLE001
            self.retention_errors += 1
            return []

    def apply_retention(self) -> List[str]:
        """Prune old segments under the engine's lock so no append interleaves with it.
        A pruning failure is counted and reported, never raised into the feed."""
        store = self.engine.store
        if store is None or self.cfg.retention_days <= 0:
            return []
        try:
            with self.engine.lock:
                return store.apply_retention(self.wall_ms(), self.cfg.retention_days)
        except Exception:  # noqa: BLE001
            self.retention_errors += 1
            return []

    def start(self) -> None:
        self.engine.start_session(self.wall_ms, self.mono)
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
            try:
                self.engine.store.flush()
            except Exception:  # noqa: BLE001
                pass
