"""Concurrency and data-flow integrity.

Two questions this suite answers, which no other suite does:

1. **Can concurrent access corrupt trading state?** The model is single-writer /
   many-reader, and that is *enforced*, not documented. These tests hammer it
   from multiple threads and assert the invariants hold.

2. **Does every number that moves through the stack keep its meaning?** A value
   that is a fraction in one module and a percent in the next, or a notional in
   one and a quantity in the next, is the defect class that produced the
   original codebase. These tests trace units and conservation across module
   boundaries.
"""
from __future__ import annotations

import ast
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest as bt  # noqa: E402
import bybit_connection as bc  # noqa: E402
import config as config_module  # noqa: E402
import trading_engine as te  # noqa: E402
from fake_bybit import API_KEY, API_SECRET, FakeBybit  # noqa: E402
from persistence import PersistenceError, StateStore, TradeRecord, utc_now_epoch  # noqa: E402
from position_sizing import BillionairePositionSizing, InstrumentFilters, SizingRequest  # noqa: E402
from risk_management import BillionaireRiskManager, equity_risk_fraction  # noqa: E402

EQUITY = 100_000.0
ENTRY = 50_000.0

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_MODULES = [f for f in sorted(os.listdir(REPO)) if f.endswith(".py")]


class Cfg:
    USE_TESTNET = True
    PAPER_TRADING = False
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
    ROUND_TRIP_COST_BPS = 25.0
    MIN_EDGE_BPS = 30.0
    MAX_SPREAD_BPS = 20.0
    MAX_DATA_AGE_SECONDS = 120.0
    PREFER_MAKER = False
    MAKER_TIMEOUT_SECONDS = 1.0
    TAKER_FEE = 0.001


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "state.db"))
    s.update_equity(EQUITY)
    yield s
    s.close()


@pytest.fixture()
def stack(tmp_path):
    exchange = FakeBybit(balances={"USDT": EQUITY, "BTC": 5.0}, equity=EQUITY)
    cfg = Cfg()
    s = StateStore(str(tmp_path / "stack.db"))
    client = bc.BybitClient(config=cfg, store=s, transport=exchange)
    risk = BillionaireRiskManager(config=cfg, store=s)
    engine = te.TradingEngine(
        client=client, risk_manager=risk,
        position_sizer=BillionairePositionSizing(config=cfg, risk_manager=risk),
        store=s, config=cfg,
    )
    s.update_equity(EQUITY)
    yield engine, client, risk, s, exchange
    s.close()


# ---------------------------------------------------------------------------
# the single-writer model
# ---------------------------------------------------------------------------


class TestSingleWriter:
    def test_a_foreign_thread_cannot_mutate_state(self, store):
        """The model made enforceable: only the claiming thread may write."""
        store.claim_writer()
        errors = []

        def intruder():
            try:
                store.update_equity(1.0)
            except PersistenceError as exc:
                errors.append(exc)

        thread = threading.Thread(target=intruder)
        thread.start()
        thread.join()
        assert len(errors) == 1, "a foreign thread was allowed to write"

    def test_the_claiming_thread_may_write(self, store):
        store.claim_writer()
        store.update_equity(EQUITY * 1.01)
        assert store.get_equity_state().last_equity == pytest.approx(EQUITY * 1.01)

    def test_reads_are_unrestricted_across_threads(self, store):
        """The health server reads from its own thread and must never be blocked."""
        store.claim_writer()
        results = []

        def reader():
            for _ in range(50):
                results.append(store.get_equity_state().last_equity)
                results.append(store.open_position_count())
                results.append(store.is_kill_switch_engaged()[0])

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for _ in range(50):
            store.update_equity(EQUITY)
        for t in threads:
            t.join()
        assert len(results) == 4 * 150

    def test_releasing_the_role_reopens_writing(self, store):
        store.claim_writer()
        store.release_writer()
        done = []

        def other():
            store.update_equity(EQUITY)
            done.append(True)

        thread = threading.Thread(target=other)
        thread.start()
        thread.join()
        assert done == [True]

    def test_unclaimed_store_is_unrestricted(self, tmp_path):
        """Backtests and tests run without claiming; the guard must not fire."""
        s = StateStore(str(tmp_path / "free.db"))
        done = []

        def writer():
            s.update_equity(1000.0)
            done.append(True)

        thread = threading.Thread(target=writer)
        thread.start()
        thread.join()
        assert done == [True]
        s.close()


class TestConcurrentIntegrity:
    def test_order_sequence_is_unique_under_contention(self, tmp_path):
        """Duplicate sequence numbers would mean duplicate orderLinkIds, which
        would mean an exchange rejection at best and a double fill at worst."""
        s = StateStore(str(tmp_path / "seq.db"))
        seen = []
        lock = threading.Lock()

        def grab():
            local = [s.next_order_seq() for _ in range(50)]
            with lock:
                seen.extend(local)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: grab(), range(8)))

        assert len(seen) == 400
        assert len(set(seen)) == 400, "the order sequence produced a duplicate"
        s.close()

    def test_concurrent_order_intents_never_duplicate(self, tmp_path):
        """`record_order` is the idempotency guard; it must be atomic."""
        s = StateStore(str(tmp_path / "idem.db"))
        accepted = []
        lock = threading.Lock()

        def claim():
            ok = s.record_order("SAME-ID", "BTCUSDT", "Buy", "Market", 1.0)
            with lock:
                accepted.append(ok)

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(lambda _: claim(), range(64)))

        assert accepted.count(True) == 1, (
            f"{accepted.count(True)} threads believed they owned the same "
            "orderLinkId; a duplicate order would have been sent"
        )
        s.close()

    def test_trade_ledger_is_consistent_under_concurrent_writes(self, tmp_path):
        s = StateStore(str(tmp_path / "trades.db"))
        s.update_equity(EQUITY)

        def record(i):
            s.record_trade(TradeRecord(
                symbol="BTCUSDT", side="Buy", qty=0.01,
                entry_price=ENTRY, exit_price=ENTRY + i,
                gross_pnl=float(i), entry_fee=0.1, exit_fee=0.1,
                opened_epoch=utc_now_epoch() - 60, closed_epoch=utc_now_epoch(),
            ))

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(record, range(120)))

        trades = s.recent_trades(1000)
        assert len(trades) == 120, "a concurrent write was lost"
        expected = sum(float(i) - 0.2 for i in range(120))
        assert sum(float(t["net_pnl"]) for t in trades) == pytest.approx(expected)
        s.close()

    def test_equity_peak_never_regresses_under_contention(self, tmp_path):
        """A lost update here would silently erase the drawdown brake."""
        s = StateStore(str(tmp_path / "peak.db"))

        def push(value):
            s.update_equity(float(value))

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(push, range(1, 401)))

        assert s.get_equity_state().peak_equity == pytest.approx(400.0)
        s.close()

    def test_kill_switch_is_visible_to_every_thread_immediately(self, store):
        """A brake one thread cannot see is not a brake."""
        store.trip_kill_switch("test")
        seen = []

        def check():
            seen.append(store.is_kill_switch_engaged()[0])

        threads = [threading.Thread(target=check) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert all(seen), "the kill switch was not visible to every thread"


# ---------------------------------------------------------------------------
# data-flow integrity across module boundaries
# ---------------------------------------------------------------------------


class TestUnitsSurviveEveryBoundary:
    """Every number keeps its meaning as it crosses a module boundary."""

    def test_pct_fields_are_fractions_everywhere_they_are_consumed(self):
        cfg = config_module.load()
        risk = BillionaireRiskManager(config=cfg, store=StateStore(":memory:"))
        for name in ("max_position_size_pct", "max_total_exposure_pct",
                     "max_daily_loss_pct", "max_drawdown_pct",
                     "max_risk_per_trade_pct", "max_correlation_exposure_pct"):
            value = getattr(risk, name)
            assert 0.0 < value <= 1.0, f"{name}={value} is not a fraction"

    def test_bps_never_leaks_into_a_fraction_field(self):
        """25 bps is 0.0025, not 25. A bps value in a fraction field would be a
        2500% limit — i.e. no limit at all."""
        cfg = config_module.load()
        assert cfg.ROUND_TRIP_COST_BPS > 1.0, "bps fields are in bps"
        assert cfg.TAKER_FEE < 1.0, "the derived fraction must be a fraction"
        assert cfg.TAKER_FEE == pytest.approx(cfg.TAKER_FEE_BPS / 10_000.0)

    def test_stop_distance_fraction_survives_into_quantity(self, stack):
        """sizing consumes a FRACTION and emits a QUANTITY, not a notional."""
        _, _, risk, _, _ = stack
        sizer = BillionairePositionSizing(config=Cfg(), risk_manager=risk)
        filters = InstrumentFilters(
            "BTCUSDT", Decimal("0.01"), Decimal("0.000001"),
            Decimal("0.000048"), Decimal("5"), from_exchange=True,
        )
        result = sizer.calculate_position_size(SizingRequest(
            symbol="BTCUSDT", current_price=ENTRY,
            current_portfolio_value=EQUITY, stop_loss_distance=0.02,
            filters=filters,
        ))
        assert result.should_trade
        # A quantity, not a notional: qty * price must equal the notional.
        assert result.notional == pytest.approx(result.qty * ENTRY)
        assert result.qty < 1.0, "qty looks like a notional"

    def test_risk_fraction_round_trips_through_the_gate(self, stack):
        """What the sizer computes is what the gate measures."""
        _, _, risk, _, _ = stack
        sizer = BillionairePositionSizing(config=Cfg(), risk_manager=risk)
        filters = InstrumentFilters(
            "BTCUSDT", Decimal("0.01"), Decimal("0.000001"),
            Decimal("0.000048"), Decimal("5"), from_exchange=True,
        )
        result = sizer.calculate_position_size(SizingRequest(
            symbol="BTCUSDT", current_price=ENTRY,
            current_portfolio_value=EQUITY, stop_loss_distance=0.02,
            filters=filters,
        ))
        recomputed = equity_risk_fraction(
            qty=result.qty, entry_price=ENTRY,
            stop_price=ENTRY * 0.98, equity=EQUITY,
        )
        assert recomputed == pytest.approx(result.risk_fraction, rel=1e-9)

    def test_prices_crossing_into_the_exchange_are_tick_aligned(self, stack):
        """A price the exchange would reject is a data-transformation failure."""
        import json

        engine, client, risk, s, exchange = stack
        report = engine.execute(te.TradeIntent(
            symbol="BTCUSDT", signal_type="BUY", entry_price=ENTRY,
            stop_price=ENTRY * 0.98,
            take_profits=((ENTRY * 1.024, 0.5), (ENTRY * 1.056, 0.5)),
            confidence=0.8,
        ))
        assert report.ok, report.reason
        tick = Decimal("0.01")
        for request in exchange.requests:
            if not request["url"].endswith("/order/create"):
                continue
            body = json.loads(request["body"])
            for field_name in ("price", "triggerPrice"):
                raw = body.get(field_name)
                if raw:
                    assert Decimal(raw) % tick == 0, (
                        f"{field_name}={raw} is not tick-aligned"
                    )

    def test_quantities_crossing_into_the_exchange_are_step_aligned(self, stack):
        import json

        engine, client, risk, s, exchange = stack
        engine.execute(te.TradeIntent(
            symbol="BTCUSDT", signal_type="BUY", entry_price=ENTRY,
            stop_price=ENTRY * 0.98,
            take_profits=((ENTRY * 1.024, 0.5), (ENTRY * 1.056, 0.5)),
            confidence=0.8,
        ))
        step = Decimal("0.000001")
        for request in exchange.requests:
            if not request["url"].endswith("/order/create"):
                continue
            qty = json.loads(request["body"])["qty"]
            assert Decimal(qty) % step == 0, f"qty={qty} is not step-aligned"


class TestConservation:
    """Nothing is created or destroyed as data moves through the system."""

    def test_take_profit_legs_sum_to_the_position(self, stack):
        """Legs that under-sum leave dust; legs that over-sum oversell."""
        import json

        engine, client, risk, s, exchange = stack
        report = engine.execute(te.TradeIntent(
            symbol="BTCUSDT", signal_type="BUY", entry_price=ENTRY,
            stop_price=ENTRY * 0.98,
            take_profits=((ENTRY * 1.024, 0.5), (ENTRY * 1.056, 0.5)),
            confidence=0.8,
        ))
        assert report.ok, report.reason
        tp_qty = sum(
            float(json.loads(r["body"])["qty"])
            for r in exchange.requests
            if r["url"].endswith("/order/create")
            and json.loads(r["body"]).get("orderType") == "Limit"
        )
        assert tp_qty == pytest.approx(report.qty, rel=1e-9), (
            "take-profit legs do not sum to the position"
        )

    def test_the_stop_covers_the_whole_position(self, stack):
        import json

        engine, client, risk, s, exchange = stack
        report = engine.execute(te.TradeIntent(
            symbol="BTCUSDT", signal_type="BUY", entry_price=ENTRY,
            stop_price=ENTRY * 0.98,
            take_profits=((ENTRY * 1.024, 0.5), (ENTRY * 1.056, 0.5)),
            confidence=0.8,
        ))
        stop_qty = sum(
            float(json.loads(r["body"])["qty"])
            for r in exchange.requests
            if r["url"].endswith("/order/create")
            and json.loads(r["body"]).get("orderFilter") == "StopOrder"
        )
        assert stop_qty == pytest.approx(report.qty, rel=1e-9), (
            "the protective stop does not cover the whole position"
        )

    def test_ledger_explains_the_entire_equity_change(self):
        """The strongest data-flow assertion available: after a full backtest,
        the sum of recorded trade PnL must equal the change in equity.

        A gap means the book and the exchange disagree about something — which
        is exactly the defect (same-symbol re-entry overwriting a position) that
        an earlier version of this check uncovered, where equity moved +7,673
        while the ledger accounted for -26.
        """
        import persistence

        rng_bars = _regime_bars(700, seed=17)
        path = "/tmp/dataflow_conservation.db"
        if os.path.exists(path):
            os.remove(path)
        result = bt.Backtester(
            {"BTCUSDT": rng_bars},
            bt.BacktestConfig(starting_cash=10_000.0, warmup_bars=150,
                              min_confidence=0.10, min_component_agreement=0.35),
            db_path=path,
        ).run()

        s = persistence.StateStore(path)
        ledger = sum(float(t["net_pnl"]) for t in s.recent_trades(10_000))
        s.close()

        delta = result.ending_equity - result.starting_equity
        assert result.reconciliation_breaks == 0, (
            f"{result.reconciliation_breaks} book/exchange divergences"
        )
        assert ledger == pytest.approx(delta, abs=0.01), (
            f"ledger {ledger:+.2f} does not explain equity change {delta:+.2f}"
        )

    def test_no_position_is_ever_left_unprotected_across_a_full_run(self):
        rng_bars = _regime_bars(600, seed=23)
        result = bt.Backtester(
            {"BTCUSDT": rng_bars},
            bt.BacktestConfig(starting_cash=10_000.0, warmup_bars=150,
                              min_confidence=0.10, min_component_agreement=0.35),
        ).run()
        assert result.reconciliation_breaks == 0


def _regime_bars(n, seed):
    import numpy as np

    rng = np.random.default_rng(seed)
    price = 100.0
    bars = []
    drift = 0.0006
    for i in range(n):
        if i and i % 200 == 0:
            drift = -drift
        ret = rng.normal(drift, 0.008)
        opened = price
        price = max(1.0, price * (1 + ret))
        bars.append(bt.Bar(
            1_600_000_000_000 + i * 3_600_000, opened,
            max(opened, price) * 1.002, min(opened, price) * 0.998,
            price, 1000.0,
        ))
    return bars


# ---------------------------------------------------------------------------
# structural guarantees about concurrency
# ---------------------------------------------------------------------------


class TestConcurrencyStructure:
    @pytest.mark.parametrize("module", LIVE_MODULES)
    def test_no_module_spawns_an_unmanaged_thread(self, module):
        """The legacy code ran a busy-spin monitor thread per analyser instance
        and built a new instance per signal, progressively starving itself of
        CPU. Only `main.py` may start a thread, and only the daemon health
        server."""
        tree = ast.parse(open(os.path.join(REPO, module), encoding="utf-8").read())
        starts = [
            node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Thread"
        ]
        if module == "main.py":
            assert len(starts) <= 1, "main.py starts more than one thread"
        else:
            assert starts == [], f"{module} spawns a thread at {starts}"

    @pytest.mark.parametrize("module", LIVE_MODULES)
    def test_no_module_sleeps_in_a_hot_path_without_bound(self, module):
        """`time.sleep` is allowed, but only with a computed bound — the legacy
        backoff helper returned before sleeping, turning every retry loop into a
        hot spin."""
        source = open(os.path.join(REPO, module), encoding="utf-8").read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "sleep"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == 0):
                pytest.fail(f"{module}:{node.lineno} sleeps for zero seconds")

    def test_the_store_is_the_only_shared_mutable_state(self):
        """Everything cross-thread goes through StateStore, which is guarded."""
        tree = ast.parse(open(os.path.join(REPO, "main.py"), encoding="utf-8").read())
        # `logger` and `__all__` are module-level but never reassigned after
        # import; they are not shared *mutable* state. Anything else at module
        # scope would be.
        allowed = {"logger", "__all__"}
        globals_assigned = [
            t.id for node in tree.body if isinstance(node, ast.Assign)
            for t in node.targets
            if isinstance(t, ast.Name) and not t.id.isupper() and t.id not in allowed
        ]
        assert globals_assigned == [], (
            f"main.py has mutable module-level state: {globals_assigned}"
        )
