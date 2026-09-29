"""The WS integration proven at the ONLY level that matters: the real
application. REAL `TradingBot`, REAL `TradingEngine`, REAL `BybitClient`,
REAL `StateStore` — FakeBybit stands in for the REST venue, a scripted
`ws_transport`-shaped fake stands in for the socket. If this file passes,
`TradingBot` actually consumes WS events; if only
`tests/test_private_ws_consumer.py` passed, that would only prove the
consumer works in isolation, which is not the claim this session's
directive asked for.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bybit_connection as bc  # noqa: E402
import main as m  # noqa: E402
import trading_engine as te  # noqa: E402
import ws_transport as wt  # noqa: E402
from fake_bybit import API_KEY, API_SECRET, FakeBybit  # noqa: E402
from persistence import StateStore  # noqa: E402
from position_sizing import BillionairePositionSizing  # noqa: E402
from risk_management import BillionaireRiskManager  # noqa: E402

EQUITY = 100_000.0
ENTRY = 50_000.0
SYMBOL = "BTCUSDT"


class LiveWSCfg:
    """PAPER_TRADING off (the real order path runs) + PRIVATE_WS_ENABLED —
    the only two differences from test_orchestrator.py's Cfg that this
    scenario needs."""

    USE_TESTNET = True
    PAPER_TRADING = False
    PRIVATE_WS_ENABLED = True
    BYBIT_API_KEY = API_KEY
    BYBIT_API_SECRET = API_SECRET
    BYBIT_RECV_WINDOW_MS = 5000
    REQUEST_TIMEOUT_SECONDS = 5.0
    USE_LEVERAGE = False
    SYMBOL_FILTERS_TTL = 3600
    ORDERLINK_PREFIX = "BB"
    MAX_POSITION_SIZE_PCT = 0.02
    MAX_TOTAL_EXPOSURE_PCT = 0.10
    MAX_DAILY_LOSS_PCT = 0.02
    MAX_DRAWDOWN_PCT = 0.10
    STOP_LOSS_PCT = 0.02
    RISK_PER_TRADE_PCT = 0.005
    MAX_OPEN_POSITIONS = 4
    MAX_CONSECUTIVE_LOSSES = 4
    MIN_RISK_REWARD_RATIO = 2.0
    MIN_CONFIDENCE = 0.60
    MAX_CORRELATION_EXPOSURE_PCT = 0.30
    MAX_LEVERAGE_EU = 1
    DEFAULT_LEVERAGE = 1
    TRADE_COOLDOWN_SECONDS = 0
    CIRCUIT_BREAKER_HOURS = 2.0
    BREAKEVEN_AFTER_FIRST_TP = True
    TRADING_SYMBOLS = (SYMBOL,)
    LOOP_INTERVAL_SECONDS = 0.05
    ENABLE_HEALTH_SERVER = False
    HEALTHCHECK_PORT = 18097
    LIVE_TRADING_ACK = ""


class _StubStrategy:
    def __init__(self):
        self.calls = 0

    def signal_for(self, symbol):
        self.calls += 1
        return te.TradeIntent(
            symbol=symbol, signal_type="BUY",
            entry_price=ENTRY, stop_price=ENTRY * 0.98,
            take_profits=((ENTRY * 1.05, 1.0),), confidence=0.8,
        )


def _text_frame(obj):
    return (wt.OP_TEXT, json.dumps(obj).encode("utf-8"))


class _ScriptedTransport:
    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []
        self.connected = False
        self.closed = False

    def connect(self):
        self.connected = True

    def send_text(self, text):
        self.sent.append(text)

    def send_pong(self, payload):
        self.sent.append(("PONG", payload))

    def recv(self, timeout=None):
        if not self._frames:
            raise wt.WSClosed("scripted transport exhausted")
        return self._frames.pop(0)

    def close(self):
        self.closed = True


class _BlockingFactory:
    """`transport_factory()` for a `WSPrivateConsumer` that blocks until the
    test hands it a transport — this is what lets the test capture the
    REAL order/venue ids from a real `engine.execute()` call BEFORE
    building the WS script that references them, rather than guessing
    ids in advance."""

    def __init__(self):
        self._q: "queue.Queue" = queue.Queue()

    def provide(self, transport) -> None:
        self._q.put(transport)

    def __call__(self):
        return self._q.get(timeout=10)


def _build_bot(tmp_path, exchange, ws_factory):
    cfg = LiveWSCfg()
    store = StateStore(str(tmp_path / "state.db"))
    store.update_equity(EQUITY)
    client = bc.BybitClient(config=cfg, store=store, transport=exchange)
    risk = BillionaireRiskManager(config=cfg, store=store)
    sizer = BillionairePositionSizing(config=cfg, risk_manager=risk)
    engine = te.TradingEngine(
        client=client, risk_manager=risk, position_sizer=sizer,
        store=store, config=cfg,
    )
    strategy = _StubStrategy()
    bot = m.TradingBot(
        config=cfg, store=store, client=client, risk_manager=risk,
        engine=engine, strategy=strategy, ws_transport_factory=ws_factory,
    )
    return bot, store, strategy


def _wait_until(predicate, *, timeout=5.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture()
def exchange():
    return FakeBybit(balances={"USDT": 100_000.0, "BTC": 5.0}, equity=EQUITY)


class TestFullApplicationLifecycleWithWS:
    """The scenario from this session's directive, section 6, steps 1-14."""

    def test_startup_connects_authenticates_and_subscribes(self, tmp_path, exchange):
        ws_factory = _BlockingFactory()
        bot, store, strategy = _build_bot(tmp_path, exchange, ws_factory)

        assert bot.startup() is True
        assert bot.observation_status == "STARTING"
        assert bot.ws_thread is not None and bot.ws_thread.is_alive()

        # Hand the WS thread (blocked in transport_factory()) an
        # auth+subscribe-only transport to unblock it, then confirm it
        # actually sent the real auth frame this BybitClient would build.
        transport = _ScriptedTransport([
            _text_frame({"op": "auth", "success": True}),
            _text_frame({"op": "subscribe", "success": True}),
        ])
        ws_factory.provide(transport)
        assert _wait_until(lambda: len(transport.sent) >= 2)

        auth_sent = json.loads(transport.sent[0])
        assert auth_sent["op"] == "auth"
        assert auth_sent["args"][0] == LiveWSCfg.BYBIT_API_KEY
        subscribe_sent = json.loads(transport.sent[1])
        assert subscribe_sent["op"] == "subscribe"
        assert set(subscribe_sent["args"]) == {"order", "execution", "position"}

        bot.shutdown()
        assert bot.ws_thread is None
        assert transport.closed is True

    def test_full_lifecycle_order_fill_ws_events_reconciliation(
            self, tmp_path, exchange):
        """The real scenario: an order goes through the normal engine, the
        WS consumer independently observes it via scripted venue events,
        and the next tick's REST reconciliation clears the WS-flagged
        gap — never merely because a tick ran."""
        ws_factory = _BlockingFactory()
        bot, store, strategy = _build_bot(tmp_path, exchange, ws_factory)

        assert bot.startup() is True

        # Unblock the WS thread with auth+subscribe now, so it is
        # listening (blocked in recv()) by the time the order lands.
        entry_transport = _ScriptedTransport([
            _text_frame({"op": "auth", "success": True}),
            _text_frame({"op": "subscribe", "success": True}),
        ])
        ws_factory.provide(entry_transport)
        assert _wait_until(lambda: len(entry_transport.sent) >= 2)

        # Step through the REAL engine: signal -> intent -> risk ->
        # authorization -> order -> ack -> fill -> position -> protection.
        bot.tick()
        assert strategy.calls == 1
        entries = exchange.submitted_orders("entry")
        assert len(entries) == 1, "the real order path did not submit"
        stops = exchange.submitted_orders("stop")
        assert len(stops) == 1, "the real order path did not protect"
        assert store.positions_without_stops() == []

        entry = entries[0]
        # Since entry_transport's script is now exhausted, the WS
        # thread's next recv() raises WSClosed -> run_forever reconnects
        # -> the factory blocks again -> we now hand it the transport
        # carrying the REAL order/execution/position events, built from
        # the ids the real order above actually produced.
        events_transport = _ScriptedTransport([
            _text_frame({"op": "auth", "success": True}),
            _text_frame({"op": "subscribe", "success": True}),
            _text_frame({"topic": "order", "data": [{
                "symbol": SYMBOL, "orderId": entry["orderId"],
                "orderLinkId": entry["orderLinkId"],
                "orderStatus": "Filled", "updatedTime": "1",
            }]}),
            _text_frame({"topic": "execution", "data": [{
                "symbol": SYMBOL, "orderId": entry["orderId"],
                "orderLinkId": entry["orderLinkId"],
                "execId": "E-1", "execType": "Trade",
            }]}),
            _text_frame({"topic": "position", "data": [{
                "symbol": SYMBOL, "updatedTime": "2",
            }]}),
        ])
        ws_factory.provide(events_transport)

        assert _wait_until(lambda: bot.ws_consumer.applied_event_count >= 3), (
            "the WS consumer never applied the scripted order/execution/"
            "position events")

        # The order/execution events are QUEUED, not yet written — the WS
        # thread itself never touches the store (persistence.StateStore's
        # single-writer model; see private_ws_consumer.py's docstring).
        # (The order was already "filled" via the normal REST path inside
        # the first tick() — FakeBybit fills a market order immediately —
        # so the queue itself, not a status transition, is the proof here.)
        assert bot.ws_consumer._pending.qsize() >= 1, (
            "the WS thread wrote the store directly instead of queuing")
        # The position event never wrote the store directly either — it
        # only flagged a gap:
        assert bot.ws_consumer.needs_reconciliation is True
        assert SYMBOL in bot.ws_consumer.dirty_symbols
        # observation_status is LIVE-computed from needs_reconciliation
        # (see main.TradingBot.observation_status) — it must read DEGRADED
        # right now, even though no tick has run since the WS events
        # arrived. A stale "HEALTHY" here would be exactly the false-
        # healthy report the WS/REST bridge exists to prevent.
        assert bot.observation_status == "DEGRADED"

        # The next tick() does three things, in order: drains the queued
        # WS events onto the writer thread (this is where "filled" is
        # actually written), runs this cycle's REST calls
        # (observe_exits/check_naked_positions, unconditional, every
        # cycle), and only then clears the WS-flagged gap.
        bot.tick()

        row_after = store.get_order(entry["orderLinkId"])
        assert row_after["status"] == "filled"
        assert bot.ws_consumer._pending.qsize() == 0, (
            "the queued WS events were never drained by the writer thread")
        assert bot.ws_consumer.needs_reconciliation is False, (
            "the gap was not cleared by a verified REST reconciliation"
        )
        assert bot.ws_consumer.dirty_symbols == set()
        assert bot.observation_status == "HEALTHY"
        # Protection is still correct — REST remains the authority, and
        # nothing about the WS path touched it:
        assert store.positions_without_stops() == []

        bot.shutdown()

    def test_reconciliation_failure_leaves_the_gap_unresolved(
            self, tmp_path, exchange):
        """If this cycle's REST calls fail, the WS-flagged gap must stay
        open — never cleared just because a tick ran."""
        ws_factory = _BlockingFactory()
        bot, store, strategy = _build_bot(tmp_path, exchange, ws_factory)
        assert bot.startup() is True

        transport = _ScriptedTransport([
            _text_frame({"op": "auth", "success": True}),
            _text_frame({"op": "subscribe", "success": True}),
            _text_frame({"topic": "position", "data": [{
                "symbol": SYMBOL, "updatedTime": "1"}]}),
        ])
        ws_factory.provide(transport)
        assert _wait_until(lambda: bot.ws_consumer.needs_reconciliation is True)

        # Force this cycle's REST reconciliation to fail.
        original = bot.engine.observe_exits

        def _boom():
            raise RuntimeError("venue unreachable this cycle")

        bot.engine.observe_exits = _boom
        bot.tick()
        bot.engine.observe_exits = original

        assert bot.ws_consumer.needs_reconciliation is True, (
            "a failed REST cycle must not clear the WS-flagged gap")
        assert bot.observation_status == "DEGRADED"

        bot.shutdown()

    def test_ws_disabled_by_default_is_unaffected(self, tmp_path, exchange):
        """PRIVATE_WS_ENABLED defaults False — this whole feature must be
        inert, byte-for-byte the same as before it existed, unless
        explicitly turned on."""
        class _DefaultCfg(LiveWSCfg):
            PRIVATE_WS_ENABLED = False

        store = StateStore(str(tmp_path / "state.db"))
        store.update_equity(EQUITY)
        client = bc.BybitClient(config=_DefaultCfg(), store=store, transport=exchange)
        risk = BillionaireRiskManager(config=_DefaultCfg(), store=store)
        engine = te.TradingEngine(
            client=client, risk_manager=risk, store=store, config=_DefaultCfg())
        bot = m.TradingBot(config=_DefaultCfg(), store=store, client=client,
                          risk_manager=risk, engine=engine,
                          strategy=_StubStrategy())

        assert bot.startup() is True
        assert bot.observation_status == "DISABLED"
        assert bot.ws_thread is None
        bot.tick()  # must not raise, must not touch anything WS-shaped
        bot.shutdown()

    def test_shutdown_leaves_no_socket_or_thread_behind(self, tmp_path, exchange):
        ws_factory = _BlockingFactory()
        bot, store, strategy = _build_bot(tmp_path, exchange, ws_factory)
        bot.startup()
        transport = _ScriptedTransport([
            _text_frame({"op": "auth", "success": True}),
            _text_frame({"op": "subscribe", "success": True}),
        ])
        ws_factory.provide(transport)
        assert _wait_until(lambda: transport.connected)

        bot.shutdown()

        assert bot.ws_thread is None
        assert bot.ws_stop_event.is_set()
        assert transport.closed is True
