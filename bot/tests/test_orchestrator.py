"""Orchestrator tests: startup order, health, the loop, shutdown.

The properties here are the ones the legacy `main.py` violated structurally
rather than logically — code below the `__main__` block that could never run, a
health endpoint that did not exist, a `stop()` that cancelled nothing.
"""
from __future__ import annotations

import ast
import json
import os
import sys
import threading
import time
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bybit_connection as bc  # noqa: E402
import main as m  # noqa: E402
import trading_engine as te  # noqa: E402
from fake_bybit import API_KEY, API_SECRET, FakeBybit  # noqa: E402
from persistence import StateStore  # noqa: E402
from position_sizing import BillionairePositionSizing  # noqa: E402
from risk_management import BillionaireRiskManager  # noqa: E402

ENTRY = 50_000.0


class Cfg:
    USE_TESTNET = True
    PAPER_TRADING = True
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
    TRADING_SYMBOLS = ("BTCUSDT",)
    LOOP_INTERVAL_SECONDS = 0.05
    ENABLE_HEALTH_SERVER = False
    HEALTHCHECK_PORT = 18099
    LIVE_TRADING_ACK = ""


class StubStrategy:
    """A deterministic signal source, so the loop has something to act on."""

    def __init__(self, signal_type="BUY", calls=None):
        self.signal_type = signal_type
        self.calls = calls if calls is not None else []

    def signal_for(self, symbol):
        self.calls.append(symbol)
        return te.TradeIntent(
            symbol=symbol, signal_type=self.signal_type,
            entry_price=ENTRY, stop_price=ENTRY * 0.98,
            take_profits=((ENTRY * 1.05, 1.0),), confidence=0.8,
        )


def build(tmp_path, exchange, cfg=None, strategy=None):
    cfg = cfg or Cfg()
    store = StateStore(str(tmp_path / "state.db"))
    store.update_equity(100_000.0)
    client = bc.BybitClient(config=cfg, store=store, transport=exchange)
    risk = BillionaireRiskManager(config=cfg, store=store)
    engine = te.TradingEngine(
        client=client, risk_manager=risk,
        position_sizer=BillionairePositionSizing(config=cfg, risk_manager=risk),
        store=store, config=cfg,
    )
    return m.TradingBot(
        config=cfg, store=store, client=client,
        risk_manager=risk, engine=engine, strategy=strategy,
    )


@pytest.fixture()
def exchange():
    return FakeBybit(balances={"USDT": 100_000.0, "BTC": 5.0}, equity=100_000.0)


# ---------------------------------------------------------------------------
# startup order
# ---------------------------------------------------------------------------


class TestStartup:
    def test_startup_succeeds_in_sandbox(self, tmp_path, exchange):
        bot = build(tmp_path, exchange)
        assert bot.startup() is True
        bot.shutdown()

    def test_reconciliation_runs_before_trading(self, tmp_path, exchange):
        """Audit C18: the legacy bot restarted with amnesia and stacked
        positions on top of forgotten ones."""
        bot = build(tmp_path, exchange)
        assert bot._reconciled is False
        bot.startup()
        assert bot._reconciled is True
        bot.shutdown()

    def test_an_engaged_kill_switch_prevents_startup(self, tmp_path, exchange):
        bot = build(tmp_path, exchange)
        bot.store.trip_kill_switch("previous run")
        assert bot.startup() is False
        bot.shutdown()

    def test_unresolvable_orders_prevent_startup(self, tmp_path, exchange):
        """If we cannot tell what happened to an order, we do not trade."""
        bot = build(tmp_path, exchange)
        bot.store.record_order("BB-entry-1-x", "BTCUSDT", "Buy", "Market", 0.01)
        exchange.scripted = [(500, "boom")] * 40
        assert bot.startup() is False
        bot.shutdown()

    def test_unreachable_exchange_prevents_startup(self, tmp_path, exchange):
        bot = build(tmp_path, exchange)

        def dead(*a, **k):
            raise ConnectionError("no route to host")

        exchange.request = dead
        assert bot.startup() is False
        bot.shutdown()

    def test_live_without_full_authorisation_degrades_to_paper(
        self, tmp_path, exchange
    ):
        """Audit C3/C4: the legacy default was mainnet with paper hardcoded off."""
        cfg = Cfg()
        cfg.USE_TESTNET = False
        cfg.PAPER_TRADING = False
        cfg.LIVE_TRADING_ACK = ""
        bot = build(tmp_path, exchange, cfg=cfg)
        armed, _ = __import__("config").is_live_authorized(cfg)
        assert armed is False
        bot.shutdown()


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_reports_real_state(self, tmp_path, exchange):
        bot = build(tmp_path, exchange)
        bot.startup()
        health = bot.health()
        assert health["healthy"] is True
        assert health["reconciled"] is True
        assert health["paper"] is True
        assert health["testnet"] is True
        bot.shutdown()

    def test_health_is_unhealthy_when_the_kill_switch_trips(self, tmp_path, exchange):
        bot = build(tmp_path, exchange)
        bot.startup()
        bot.store.trip_kill_switch("test")
        assert bot.health()["healthy"] is False
        bot.shutdown()

    def test_health_is_unhealthy_when_a_position_is_naked(self, tmp_path, exchange):
        bot = build(tmp_path, exchange)
        bot.startup()
        bot.store.upsert_position("ETHUSDT", "Buy", 1.0, 3000.0, stop_price=0.0)
        health = bot.health()
        assert health["healthy"] is False
        assert health["naked_positions"] == ["ETHUSDT"]
        bot.shutdown()

    def test_health_server_actually_serves(self, tmp_path, exchange):
        """The legacy config advertised a health check with no server behind it."""
        cfg = Cfg()
        cfg.ENABLE_HEALTH_SERVER = True
        cfg.HEALTHCHECK_PORT = 18111
        bot = build(tmp_path, exchange, cfg=cfg)
        bot.startup()
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:18111/health", timeout=5
            ) as response:
                payload = json.loads(response.read())
            assert payload["reconciled"] is True
        finally:
            bot.shutdown()

    def test_health_endpoint_returns_503_when_unhealthy(self, tmp_path, exchange):
        cfg = Cfg()
        cfg.ENABLE_HEALTH_SERVER = True
        cfg.HEALTHCHECK_PORT = 18112
        bot = build(tmp_path, exchange, cfg=cfg)
        bot.startup()
        bot.store.trip_kill_switch("test")
        try:
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                urllib.request.urlopen("http://127.0.0.1:18112/health", timeout=5)
            assert excinfo.value.code == 503
        finally:
            bot.shutdown()


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------


class TestLoop:
    def test_a_cycle_with_no_strategy_takes_no_trades(self, tmp_path, exchange):
        """The legacy fallback fabricated BUY signals at confidence 0.66."""
        bot = build(tmp_path, exchange, strategy=None)
        bot.startup()
        bot.tick()
        assert exchange.orders == {}
        bot.shutdown()

    def test_a_cycle_with_a_strategy_evaluates_each_symbol(self, tmp_path, exchange):
        strategy = StubStrategy()
        bot = build(tmp_path, exchange, strategy=strategy)
        bot.startup()
        bot.tick()
        assert strategy.calls == ["BTCUSDT"]
        bot.shutdown()

    def test_a_halted_risk_layer_stops_the_cycle(self, tmp_path, exchange):
        strategy = StubStrategy()
        bot = build(tmp_path, exchange, strategy=strategy)
        bot.startup()
        bot.store.trip_breaker("test", 3600)
        bot.tick()
        assert strategy.calls == [], "traded while the breaker was active"
        bot.shutdown()

    def test_a_strategy_exception_does_not_kill_the_loop(self, tmp_path, exchange):
        class Exploding:
            def signal_for(self, symbol):
                raise RuntimeError("indicator blew up")

        bot = build(tmp_path, exchange, strategy=Exploding())
        bot.startup()
        bot.tick()   # must not raise
        assert exchange.orders == {}
        bot.shutdown()

    def test_run_starts_and_stops_cleanly(self, tmp_path, exchange):
        bot = build(tmp_path, exchange, strategy=StubStrategy())
        result: dict = {}

        thread = threading.Thread(target=lambda: result.update(code=bot.run()))
        thread.start()
        time.sleep(0.5)
        bot.stop()
        thread.join(timeout=10)

        assert thread.is_alive() is False, "the loop did not stop"
        assert result.get("code") == 0

    def test_run_refuses_to_start_when_startup_fails(self, tmp_path, exchange):
        bot = build(tmp_path, exchange)
        bot.store.trip_kill_switch("test")
        assert bot.run() == 1


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------


class TestStructure:
    def test_nothing_is_defined_below_the_main_block(self):
        """The legacy file called main() two thirds of the way down, so five
        orchestrator classes below that point were undefined at call time."""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"
        )
        tree = ast.parse(open(path, encoding="utf-8").read())
        main_block_line = None
        for node in tree.body:
            if isinstance(node, ast.If) and ast.unparse(node.test).startswith(
                "__name__"
            ):
                main_block_line = node.lineno
        assert main_block_line is not None, "no __main__ block found"
        later = [
            n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.lineno > main_block_line
        ]
        assert later == [], f"definitions after the __main__ block: {later}"

    def test_only_one_orchestrator_class(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"
        )
        tree = ast.parse(open(path, encoding="utf-8").read())
        classes = [
            n.name for n in tree.body
            if isinstance(n, ast.ClassDef) and not n.name.startswith("_")
        ]
        assert classes == ["TradingBot"], f"expected one orchestrator, got {classes}"

    def test_no_contrarian_or_fabricated_signal_code(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"
        )
        src = open(path, encoding="utf-8").read()
        for banned in ("ENABLE_CONTRARIAN", "ENABLE_EMA_FALLBACK_SIGNALS"):
            code = [
                ln for ln in src.splitlines()
                if banned in ln and not ln.lstrip().startswith("#") and "``" not in ln
            ]
            assert code == [], f"{banned} still present: {code}"

    def test_shutdown_leaves_protective_stops_in_place(self, tmp_path, exchange):
        cfg = Cfg()
        cfg.PAPER_TRADING = False
        bot = build(tmp_path, exchange, cfg=cfg)
        bot.startup()
        report = bot.engine.execute(te.TradeIntent(
            symbol="BTCUSDT", signal_type="BUY", entry_price=ENTRY,
            stop_price=ENTRY * 0.98, confidence=0.8,
        ))
        assert report.ok, report.reason
        bot.shutdown()
        assert exchange.orders[report.stop_order_link_id]["orderStatus"] == "Untriggered"
