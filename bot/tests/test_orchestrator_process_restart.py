"""The complete top-level golden path — TradingBot.tick(), not
TradingEngine.execute() directly — across a real OS process crash.

WHY THIS FILE EXISTS
=====================
Prior coverage proved two different things separately:

- `tests/test_lifecycle_integration.py` proves order -> fill -> position ->
  protective-stop -> reconcile is correct AT THE ENGINE LEVEL
  (`TradingEngine.execute()`/`.reconcile()` called directly).
- `tests/test_orchestrator.py` proves `TradingBot.tick()`/`.run()` correctly
  wires strategy signal -> `engine.execute()`, but its assertions stop at
  "the engine was called with the right intent" — it does not check the
  fill/position/protection chain that follows.
- `tests/test_process_restart_recovery.py` proves recovery survives a real
  OS process crash, but drives `BybitClient`/`TradingEngine` directly, never
  `TradingBot`.

No single test combines all three: the actual top-level entry point
(`TradingBot.tick()`) driving the complete chain (signal -> intent -> risk
-> order -> ack -> fill -> position -> protection -> reconciliation ->
durable persistence) AND surviving a real process crash in the middle of
it. This file is that combination — the "golden path" from this session's
directive, run through the actual orchestrator, not a stand-in for it.

THE SPECIFIC ADVERSARIAL CASE
================================
Process 1's strategy signals BUY, `tick()` opens a position and its
protective stop, then the process crashes hard (`os._exit`, no cleanup —
see test_process_restart_recovery.py's docstring for why this is a fair
proxy for a real kill).

Process 2 is a fresh `TradingBot`, fresh interpreter, fresh `FakeBybit`
reconstructed only from a snapshot of what process 1's venue held — built
with the SAME strategy that will signal BUY again, exactly as a real
strategy would on the next scheduled cycle with no memory of the crash.
`startup()` reconciles first (as production does), then `tick()` runs.
The assertion that matters: this must NOT open a second position.
`risk_management.py`'s `POSITION_ALREADY_OPEN` gate reads
`store.open_positions()` — this proves that gate holds up when
`store` is a fresh connection to the SAME ON-DISK FILE after a real crash,
not merely the same Python object across two in-process reconstructions.
"""
from __future__ import annotations

import multiprocessing
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))

EQUITY = 100_000.0
ENTRY = 50_000.0


class LiveCfg:
    """Paper mode OFF, so the real order path runs — duplicated (not
    imported) from the sibling restart-recovery test file so this file has
    no cross-test-module coupling, matching the rest of the suite's
    convention of a local Cfg per file."""

    USE_TESTNET = True
    PAPER_TRADING = False
    BYBIT_API_KEY = "test-key"
    BYBIT_API_SECRET = "test-secret"
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
    TRADING_SYMBOLS = ("BTCUSDT",)
    LOOP_INTERVAL_SECONDS = 0.05
    ENABLE_HEALTH_SERVER = False
    HEALTHCHECK_PORT = 18098
    LIVE_TRADING_ACK = ""


def _import_path() -> None:
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    if TESTS not in sys.path:
        sys.path.insert(0, TESTS)


def _put_and_crash(out_queue, payload: dict) -> None:
    """See test_process_restart_recovery.py's `_put_and_crash` docstring:
    flush the (purely instrumental) queue before the hard, cleanup-free
    exit that stands in for a real crash."""
    out_queue.put(payload)
    out_queue.close()
    out_queue.join_thread()
    os._exit(0)


def _venue_snapshot(exchange) -> dict:
    return {
        "balances": dict(exchange.balances),
        "orders": {k: dict(v) for k, v in exchange.orders.items()},
        "positions": {k: dict(v) for k, v in exchange.positions.items()},
    }


def _venue_from_snapshot(snapshot: dict):
    from fake_bybit import FakeBybit

    exchange = FakeBybit(balances=dict(snapshot["balances"]), equity=EQUITY)
    exchange.orders = {k: dict(v) for k, v in snapshot["orders"].items()}
    exchange.positions = {k: dict(v) for k, v in snapshot["positions"].items()}
    return exchange


class _StubStrategy:
    """Always signals BUY for every symbol it is asked about — a strategy
    with no memory of anything, exactly like a real one re-invoked cold
    after a restart."""

    def signal_for(self, symbol):
        import trading_engine as te
        return te.TradeIntent(
            symbol=symbol, signal_type="BUY",
            entry_price=ENTRY, stop_price=ENTRY * 0.98,
            take_profits=((ENTRY * 1.05, 1.0),), confidence=0.8,
        )


def _build_bot(store, exchange):
    import bybit_connection as bc
    import trading_engine as te
    import main as m
    from position_sizing import BillionairePositionSizing
    from risk_management import BillionaireRiskManager

    cfg = LiveCfg()
    client = bc.BybitClient(config=cfg, store=store, transport=exchange)
    risk = BillionaireRiskManager(config=cfg, store=store)
    sizer = BillionairePositionSizing(config=cfg, risk_manager=risk)
    engine = te.TradingEngine(
        client=client, risk_manager=risk, position_sizer=sizer,
        store=store, config=cfg,
    )
    return m.TradingBot(
        config=cfg, store=store, client=client, risk_manager=risk,
        engine=engine, strategy=_StubStrategy(),
    )


def _proc1_tick_and_crash(db_path: str, out_queue) -> None:
    """One real cycle through the actual orchestrator: startup() (first-run
    reconcile, a no-op) then tick() (signal -> intent -> risk -> execute,
    which itself does order -> ack -> fill -> protective stop). Then a hard
    crash, before anything else can happen."""
    _import_path()
    from persistence import StateStore
    from fake_bybit import FakeBybit

    exchange = FakeBybit(balances={"USDT": 100_000.0, "BTC": 5.0}, equity=EQUITY)
    store = StateStore(db_path)
    store.update_equity(EQUITY)
    bot = _build_bot(store, exchange)

    started = bot.startup()
    bot.tick()

    positions = store.open_positions()
    _put_and_crash(out_queue, {
        "started": bool(started),
        "positions_after_tick": len(positions),
        "naked_after_tick": store.positions_without_stops(),
        "entry_orders_on_venue": len(exchange.submitted_orders("entry")),
        "stop_orders_on_venue": len(exchange.submitted_orders("stop")),
        "venue": _venue_snapshot(exchange),
    })


def _proc2_tick_and_recover(db_path: str, venue_snapshot: dict,
                            out_queue) -> None:
    """A genuinely fresh process, fresh TradingBot, fresh FakeBybit built
    only from process 1's venue snapshot — same strategy, same intent to
    BUY, no memory of anything. startup() reconciles, then tick() runs
    again exactly as the next scheduled cycle would."""
    _import_path()
    from persistence import StateStore

    exchange = _venue_from_snapshot(venue_snapshot)
    store = StateStore(db_path)
    bot = _build_bot(store, exchange)

    started = bot.startup()
    bot.tick()

    positions = store.open_positions()
    _put_and_crash(out_queue, {
        "started": bool(started),
        "positions_after_tick": len(positions),
        "naked_after_tick": store.positions_without_stops(),
        "entry_orders_on_venue": len(exchange.submitted_orders("entry")),
        "stop_orders_on_venue": len(exchange.submitted_orders("stop")),
    })


def _run(target, args) -> dict:
    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    proc = ctx.Process(target=target, args=(*args, queue))
    proc.start()
    result = queue.get(timeout=30)
    proc.join(timeout=10)
    assert proc.exitcode == 0, f"child process exited abnormally: {proc.exitcode}"
    return result


@pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(),
    reason="fork start method unavailable on this platform")
class TestGoldenPathThroughTheOrchestrator:
    def test_one_tick_opens_a_protected_position(self, tmp_path):
        """The top-level entry point, not the engine directly: one
        TradingBot.tick() call takes a BUY signal all the way to an open,
        protected position."""
        db_path = str(tmp_path / "state.db")
        r1 = _run(_proc1_tick_and_crash, (db_path,))

        assert r1["started"] is True
        assert r1["positions_after_tick"] == 1
        assert r1["naked_after_tick"] == []
        assert r1["entry_orders_on_venue"] == 1
        assert r1["stop_orders_on_venue"] == 1

    def test_restart_then_tick_does_not_open_a_second_position(self, tmp_path):
        """The full adversarial case: a fresh process, fresh interpreter,
        same always-BUY strategy, restarted right after the first process's
        entry landed. POSITION_ALREADY_OPEN must hold across the crash."""
        db_path = str(tmp_path / "state.db")

        r1 = _run(_proc1_tick_and_crash, (db_path,))
        assert r1["positions_after_tick"] == 1

        r2 = _run(_proc2_tick_and_recover, (db_path, r1["venue"]))

        assert r2["started"] is True
        assert r2["positions_after_tick"] == 1, (
            "a second real OS process, restarted with the same always-BUY "
            "strategy, opened a SECOND position instead of being blocked "
            "by POSITION_ALREADY_OPEN reading durable state")
        assert r2["entry_orders_on_venue"] == 1, (
            "restart resubmitted a duplicate entry order")
        assert r2["naked_after_tick"] == [], (
            "the surviving position lost its protective stop across restart")
        assert r2["stop_orders_on_venue"] == 1
