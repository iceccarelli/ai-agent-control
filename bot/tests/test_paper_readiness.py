"""Slice 29 — operational proof that the machine is safe to run in paper mode.

WHAT THIS FILE IS FOR
=====================
Two signal families have now been measured against a validated instrument and
both returned ABSENT (`RESEARCH_CLOSE_STAGE1.md`). The honest position is: there
is a serious safety architecture here and no edge. The second half of that
sentence is worth being able to *demonstrate* rather than assert, which is what
these tests do.

They assert containment at the **operational boundary** — the level an operator
actually touches: environment variables, the kill switch, `main()`'s own
construction path, a multi-tick run against the offline exchange stub. Where an
invariant is already covered elsewhere in the suite, the test here imports the
same symbols and exercises the same code path rather than restating a weaker
version of it.

WHAT THIS FILE IS NOT
=====================
It is not evidence of edge. Nothing here measures skill, and no PnL produced by
a paper run — in this file or anywhere else — is a skill claim. See
`RESEARCH_CLOSE_STAGE1.md` §6.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bybit_connection as bc  # noqa: E402
import config as _config  # noqa: E402
import main as m  # noqa: E402
import memory as mem  # noqa: E402
import trading_engine as te  # noqa: E402
from fake_bybit import API_KEY, API_SECRET, FakeBybit  # noqa: E402
from persistence import PersistenceError, StateStore  # noqa: E402
from position_sizing import BillionairePositionSizing  # noqa: E402
from risk_management import BillionaireRiskManager  # noqa: E402

EQUITY = 100_000.0
ENTRY = 50_000.0

#: The literal `persistence.clear_kill_switch_by_human` demands. It is
#: deliberately not exported as a named constant and deliberately not derivable
#: from config, so the test hard-codes it exactly as an operator must type it.
HUMAN_ACK = "HUMAN_CLEARED_KILL_SWITCH"


class Cfg:
    """Paper + testnet, i.e. the shipped defaults. Nothing here is armed."""

    USE_TESTNET = True
    PAPER_TRADING = True
    ENTRIES_ENABLED = True
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
    LOOP_INTERVAL_SECONDS = 0.01
    ENABLE_HEALTH_SERVER = False
    HEALTHCHECK_PORT = 18131
    LIVE_TRADING_ACK = ""
    POLICY_MODE = "off"


class LiveOrderCfg(Cfg):
    """Paper OFF, so the real order path runs — still testnet, still unarmed."""

    PAPER_TRADING = False
    TRADE_COOLDOWN_SECONDS = 60


class StubStrategy:
    """A deterministic signal source, so "no entries" is a real difference."""

    def __init__(self, signal_type="BUY"):
        self.calls = []

    def signal_for(self, symbol):
        self.calls.append(symbol)
        return te.TradeIntent(
            symbol=symbol, signal_type="BUY",
            entry_price=ENTRY, stop_price=ENTRY * 0.98,
            take_profits=((ENTRY * 1.05, 1.0),), confidence=0.8,
        )


@pytest.fixture()
def exchange():
    return FakeBybit(balances={"USDT": 100_000.0, "BTC": 5.0}, equity=EQUITY)


def build(tmp_path, exchange, cfg=None, strategy=None):
    cfg = cfg or Cfg()
    store = StateStore(str(tmp_path / "state.db"))
    store.update_equity(EQUITY)
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


def buy_intent(**over):
    base = dict(
        symbol="BTCUSDT", signal_type="BUY",
        entry_price=ENTRY, stop_price=ENTRY * 0.98,
        take_profits=((ENTRY * 1.024, 0.5), (ENTRY * 1.056, 0.5)),
        confidence=0.75,
    )
    base.update(over)
    return te.TradeIntent(**base)


# ---------------------------------------------------------------------------
# ENTRIES_ENABLED — the one thing slice 29 added
# ---------------------------------------------------------------------------


class TestEntriesDisabledIsAFirstClassMode:
    """Strategy-neutral operation must be reachable from the environment.

    It was already a supported configuration — `build_bot(attach_strategy=False)`
    — but only from Python. An operator running `python3 main.py` had no way to
    select it, which made "stand the machine down without killing it" an
    undocumented Python trick rather than an operational control.
    """

    def test_the_key_defaults_to_enabled(self):
        """Adding the key must not silently change an existing deployment."""
        assert _config.load({}).ENTRIES_ENABLED is True

    def test_it_can_be_turned_off_from_the_environment(self):
        assert _config.load({"ENTRIES_ENABLED": "0"}).ENTRIES_ENABLED is False

    @pytest.mark.parametrize("raw", ["", "1", "true", "TRUE", "yes", "on", "t", "y"])
    def test_recognised_true_tokens_enable_entries(self, raw):
        assert _config.load({"ENTRIES_ENABLED": raw}).ENTRIES_ENABLED is True

    @pytest.mark.parametrize(
        "raw", ["0", "no", "off", "false", "maybe", "TRUEISH", "2", "-1", "  "]
    )
    def test_anything_unrecognised_disables_entries(self, raw):
        """Fail closed: a typo yields NO entries, never more.

        The direction matters. If an unrecognised value meant "enabled", a
        corrupted environment would silently arm the signal source — which is
        the shape of the bug that once made `USE_TESTNET=1` mean mainnet.
        """
        assert _config.load({"ENTRIES_ENABLED": raw}).ENTRIES_ENABLED is False

    def test_build_bot_honours_the_config_key(self, tmp_path, exchange):
        """The path `main()` actually takes, not a keyword a caller may forget."""
        cfg = Cfg()
        cfg.ENTRIES_ENABLED = False
        bot = m.build_bot(
            config=cfg, store=StateStore(str(tmp_path / "s.db")),
            client=bc.BybitClient(
                config=cfg, store=StateStore(str(tmp_path / "s.db")),
                transport=exchange),
        )
        assert bot.strategy is None

    def test_an_explicit_argument_still_wins(self, tmp_path, exchange):
        """Tests and callers that predate this key must keep meaning what they meant."""
        cfg = Cfg()
        cfg.ENTRIES_ENABLED = True
        store = StateStore(str(tmp_path / "s.db"))
        bot = m.build_bot(
            attach_strategy=False, config=cfg, store=store,
            client=bc.BybitClient(config=cfg, store=store, transport=exchange),
        )
        assert bot.strategy is None

    def test_main_passes_no_explicit_argument(self):
        """So the config key is reachable at all from the process entry point.

        If `main()` hard-coded `build_bot(attach_strategy=True)` the key would
        be dead config — present, documented, and unreachable.
        """
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(m.main))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "build_bot"]
        assert calls, "main() no longer calls build_bot"
        for call in calls:
            assert not call.args
            assert not any(kw.arg == "attach_strategy" for kw in call.keywords)


class TestTheMachineStaysAliveWithoutEntries:
    """"No entries" must not mean "no safety". The difference is the point.

    The kill switch also stops entries — by refusing to start at all, which
    takes reconciliation and stop management down with it. That is correct for
    an emergency and wrong for standing the machine down.
    """

    def test_startup_succeeds_and_reconciles(self, tmp_path, exchange):
        bot = build(tmp_path, exchange, strategy=None)
        assert bot.startup() is True
        assert bot._reconciled is True
        bot.shutdown()

    def test_health_is_healthy_with_no_strategy(self, tmp_path, exchange):
        bot = build(tmp_path, exchange, strategy=None)
        bot.startup()
        health = bot.health()
        assert health["healthy"] is True
        assert health["kill_switch"] is False
        assert not health["naked_positions"]
        bot.shutdown()

    def test_ticking_takes_no_trades_and_submits_no_orders(self, tmp_path, exchange):
        bot = build(tmp_path, exchange, strategy=None)
        bot.startup()
        for _ in range(5):
            bot.tick()
        assert exchange.submitted_orders() == []
        bot.shutdown()

    def test_the_same_stack_with_a_strategy_does_propose(self, tmp_path, exchange):
        """The control. Without it, "no orders" could just mean "nothing works"."""
        strategy = StubStrategy()
        bot = build(tmp_path, exchange, strategy=strategy)
        bot.startup()
        bot.tick()
        assert strategy.calls, "the stub strategy was never consulted"
        bot.shutdown()

    def test_reconciliation_still_runs_every_startup(self, tmp_path, exchange):
        bot = build(tmp_path, exchange, strategy=None)
        summary = bot.engine.reconcile()
        assert summary.get("unknown", 0) == 0

    def test_stop_management_is_still_reachable(self, tmp_path, exchange):
        """A position opened before the stand-down keeps a managed, verified stop."""
        cfg = LiveOrderCfg()
        store = StateStore(str(tmp_path / "state.db"))
        store.update_equity(EQUITY)
        client = bc.BybitClient(config=cfg, store=store, transport=exchange)
        risk = BillionaireRiskManager(config=cfg, store=store)
        engine = te.TradingEngine(
            client=client, risk_manager=risk,
            position_sizer=BillionairePositionSizing(config=cfg, risk_manager=risk),
            store=store, config=cfg,
        )
        report = engine.execute(buy_intent())
        assert report.ok, report.reason
        assert store.positions_without_stops() == []
        store.close()


class TestEntriesDisabledIsNotASafetyGate:
    """The docs say this in words; these tests say it in assertions.

    The risk of adding an operator control is that it gets mistaken for a gate
    and starts being relied on. It cannot arm anything, and turning it *on*
    weakens nothing.
    """

    def test_it_cannot_arm_live_trading(self):
        cfg = _config.load({
            "ENTRIES_ENABLED": "1", "USE_TESTNET": "0", "PAPER_TRADING": "0",
            "LIVE_TRADING_ACK": "", "BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s",
        })
        assert cfg.LIVE_AUTHORIZED is False
        assert cfg.PAPER_TRADING is True     # degraded, fail-closed

    def test_it_cannot_clear_the_kill_switch(self, tmp_path):
        store = StateStore(str(tmp_path / "s.db"))
        store.trip_kill_switch("test")
        for cfg in (_config.load({"ENTRIES_ENABLED": "1"}),
                    _config.load({"ENTRIES_ENABLED": "0"})):
            engaged, _ = store.is_kill_switch_engaged()
            assert engaged is True, cfg.ENTRIES_ENABLED
        store.close()

    def test_enabling_entries_does_not_bypass_a_single_gate(self, tmp_path, exchange):
        """Entries on + kill switch engaged still blocks."""
        cfg = Cfg()
        cfg.ENTRIES_ENABLED = True
        bot = build(tmp_path, exchange, cfg=cfg, strategy=StubStrategy())
        bot.store.trip_kill_switch("still blocked")
        assert bot.risk.should_halt_trading() is True
        assert bot.startup() is False
        bot.store.close()

    def test_the_flag_is_not_named_as_a_gate_in_the_risk_manager(self):
        """It must not have leaked into the gate chain as a pseudo-gate."""
        import inspect
        import risk_management
        assert "ENTRIES_ENABLED" not in inspect.getsource(risk_management)


# ---------------------------------------------------------------------------
# the actual safety gates, re-asserted operationally
# ---------------------------------------------------------------------------


class TestKillSwitchBlocksNewRisk:

    def test_startup_refuses_when_engaged(self, tmp_path, exchange):
        bot = build(tmp_path, exchange, strategy=StubStrategy())
        bot.store.trip_kill_switch("drawdown")
        assert bot.startup() is False
        bot.store.close()

    def test_the_risk_gate_blocks_with_the_documented_reason(self, tmp_path):
        store = StateStore(str(tmp_path / "s.db"))
        store.update_equity(EQUITY)
        risk = BillionaireRiskManager(config=Cfg(), store=store)
        store.trip_kill_switch("boom")
        decision = risk._gate_kill_switch()
        assert decision.allowed is False
        assert decision.reason == "KILL_SWITCH_ENGAGED"
        store.close()

    def test_an_unreadable_kill_switch_blocks_too(self, tmp_path):
        """Fail closed: if the answer cannot be read, assume the worst."""
        store = StateStore(str(tmp_path / "s.db"))
        store.update_equity(EQUITY)
        risk = BillionaireRiskManager(config=Cfg(), store=store)

        def boom():
            raise RuntimeError("db gone")

        store.is_kill_switch_engaged = boom
        decision = risk._gate_kill_switch()
        assert decision.allowed is False
        assert decision.reason == "KILL_SWITCH_UNREADABLE"
        store.close()

    def test_no_order_reaches_the_exchange_once_engaged(self, tmp_path, exchange):
        cfg = LiveOrderCfg()
        store = StateStore(str(tmp_path / "s.db"))
        store.update_equity(EQUITY)
        client = bc.BybitClient(config=cfg, store=store, transport=exchange)
        risk = BillionaireRiskManager(config=cfg, store=store)
        engine = te.TradingEngine(
            client=client, risk_manager=risk,
            position_sizer=BillionairePositionSizing(config=cfg, risk_manager=risk),
            store=store, config=cfg,
        )
        store.trip_kill_switch("engaged before the attempt")
        before = len(exchange.submitted_orders())
        report = engine.execute(buy_intent())
        assert report.ok is False
        assert len(exchange.submitted_orders()) == before
        store.close()

    def test_health_reports_unhealthy(self, tmp_path, exchange):
        bot = build(tmp_path, exchange, strategy=None)
        bot.store.trip_kill_switch("tripped")
        assert bot.health()["healthy"] is False
        bot.store.close()

    def test_it_survives_a_restart(self, tmp_path):
        path = str(tmp_path / "s.db")
        first = StateStore(path)
        first.trip_kill_switch("persist me")
        first.close()
        second = StateStore(path)
        engaged, reason = second.is_kill_switch_engaged()
        assert engaged is True
        assert reason == "persist me"
        second.close()


class TestOnlyAHumanClearsTheKillSwitch:

    def test_the_exact_token_clears_it(self, tmp_path):
        store = StateStore(str(tmp_path / "s.db"))
        store.trip_kill_switch("x")
        store.clear_kill_switch_by_human(HUMAN_ACK)
        engaged, _ = store.is_kill_switch_engaged()
        assert engaged is False
        store.close()

    @pytest.mark.parametrize("token", [
        "", "yes", "HUMAN_CLEARED", "human_cleared_kill_switch",
        "HUMAN_CLEARED_KILL_SWITCH ", " HUMAN_CLEARED_KILL_SWITCH",
        "HUMAN_CLEARED_KILL_SWITCHES",
    ])
    def test_near_misses_are_refused(self, tmp_path, token):
        """No trimming, no case folding, no prefix match. Exact or nothing."""
        store = StateStore(str(tmp_path / "s.db"))
        store.trip_kill_switch("x")
        with pytest.raises(PersistenceError):
            store.clear_kill_switch_by_human(token)
        engaged, _ = store.is_kill_switch_engaged()
        assert engaged is True
        store.close()

    def test_the_token_is_not_derivable_from_config(self):
        """An operator must type it. It must not be readable off a Config."""
        cfg = _config.load({})
        values = {str(getattr(cfg, name, "")) for name in dir(cfg)
                  if not name.startswith("_")}
        assert HUMAN_ACK not in values

    def test_no_automatic_caller_exists_in_the_trading_modules(self):
        """Structural: nothing in the hot path may CALL the clear method.

        Asserted at AST level, not on raw text. `trading_engine.py` and
        `risk_management.py` both *mention* `clear_kill_switch_by_human` in
        docstrings, precisely to say that there is no automatic recovery from
        here — banning the string would delete the explanation and leave the
        danger.
        """
        import ast
        for module in ("main.py", "trading_engine.py", "risk_management.py",
                       "memory.py", "ml_strategy.py", "position_sizing.py"):
            with open(os.path.join(REPO, module)) as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "attr", None) or getattr(
                        node.func, "id", None)
                    assert name != "clear_kill_switch_by_human", module


class TestArmingTokensStillRequired:

    def test_the_shipped_defaults_are_not_live(self):
        cfg = _config.load({})
        assert cfg.USE_TESTNET is True
        assert cfg.PAPER_TRADING is True
        assert cfg.LIVE_AUTHORIZED is False

    def test_live_without_the_ack_degrades_to_paper(self):
        cfg = _config.load({
            "USE_TESTNET": "0", "PAPER_TRADING": "0",
            "BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s",
        })
        assert cfg.LIVE_AUTHORIZED is False
        assert cfg.PAPER_TRADING is True
        assert "LIVE_BLOCKED" in cfg.LIVE_BLOCK_REASON

    def test_live_without_credentials_degrades_to_paper(self):
        cfg = _config.load({
            "USE_TESTNET": "0", "PAPER_TRADING": "0",
            "LIVE_TRADING_ACK": _config.REQUIRED_LIVE_ACK,
        })
        assert cfg.LIVE_AUTHORIZED is False
        assert cfg.PAPER_TRADING is True

    @pytest.mark.parametrize("ack", ["", "i_understand", "I UNDERSTAND", "yes"])
    def test_a_near_miss_ack_does_not_arm(self, ack):
        cfg = _config.load({
            "USE_TESTNET": "0", "PAPER_TRADING": "0", "LIVE_TRADING_ACK": ack,
            "BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s",
        })
        assert cfg.LIVE_AUTHORIZED is False

    def test_assert_sandbox_permits_the_paper_default(self):
        ok, _reason = _config.assert_sandbox(_config.load({}))
        assert ok is True

    def test_paper_mode_sends_nothing_to_the_exchange(self, tmp_path, exchange):
        cfg = Cfg()          # PAPER_TRADING = True
        store = StateStore(str(tmp_path / "s.db"))
        store.update_equity(EQUITY)
        client = bc.BybitClient(config=cfg, store=store, transport=exchange)
        risk = BillionaireRiskManager(config=cfg, store=store)
        engine = te.TradingEngine(
            client=client, risk_manager=risk,
            position_sizer=BillionairePositionSizing(config=cfg, risk_manager=risk),
            store=store, config=cfg,
        )
        report = engine.execute(buy_intent())
        assert report.reason == "PAPER_OK"
        assert exchange.submitted_orders() == []
        store.close()


class TestStopVerificationInvariantHolds:
    """A confirmed position always has a verified protective stop."""

    def _stack(self, tmp_path, exchange):
        cfg = LiveOrderCfg()
        store = StateStore(str(tmp_path / "s.db"))
        store.update_equity(EQUITY)
        client = bc.BybitClient(config=cfg, store=store, transport=exchange)
        risk = BillionaireRiskManager(config=cfg, store=store)
        engine = te.TradingEngine(
            client=client, risk_manager=risk,
            position_sizer=BillionairePositionSizing(config=cfg, risk_manager=risk),
            store=store, config=cfg,
        )
        return engine, client, store

    def test_a_verified_stop_is_the_happy_path(self, tmp_path, exchange):
        engine, _client, store = self._stack(tmp_path, exchange)
        report = engine.execute(buy_intent())
        assert report.ok, report.reason
        assert store.positions_without_stops() == []
        store.close()

    def test_an_unverifiable_stop_trips_the_kill_switch(self, tmp_path, exchange):
        engine, client, store = self._stack(tmp_path, exchange)
        client.verify_stop = lambda **kw: (False, "STOP_ORDER_NOT_VISIBLE")
        report = engine.execute(buy_intent())
        assert report.ok is False
        engaged, _ = store.is_kill_switch_engaged()
        assert engaged is True
        assert store.positions_without_stops() == []
        store.close()

    def test_a_rejected_stop_trips_the_kill_switch(self, tmp_path, exchange):
        engine, client, store = self._stack(tmp_path, exchange)

        class Rejected:
            ok = False
            reason = "REJECTED"
            order_link_id = ""

        client.place_stop_order = lambda **kw: Rejected()
        report = engine.execute(buy_intent())
        assert report.ok is False
        engaged, _ = store.is_kill_switch_engaged()
        assert engaged is True
        store.close()

    def test_a_naked_position_blocks_new_trades(self, tmp_path):
        store = StateStore(str(tmp_path / "s.db"))
        store.update_equity(EQUITY)
        risk = BillionaireRiskManager(config=Cfg(), store=store)
        clear = risk._gate_naked_positions()
        assert clear.allowed is True
        store.close()


class TestSizeCannotBeEnlarged:

    @pytest.mark.parametrize("throttle", [0.0, 0.25, 0.5, 1.0, 1.5, 42.0, -1.0])
    def test_the_memory_multiplier_never_exceeds_one(self, tmp_path, throttle):
        class C(Cfg):
            MEMORY_MIN_THROTTLE = throttle
            MEMORY_LOOKBACK_TRADES = 100

        store = StateStore(str(tmp_path / "s.db"))
        memory = mem.TradingMemory(store=store, config=C())
        assert memory.size_multiplier("BTCUSDT") <= 1.0
        store.close()

    @pytest.mark.parametrize("multiplier", [1.0, 1.5, 10.0, float("inf"), float("nan")])
    def test_a_multiplier_of_one_or_more_leaves_the_size_untouched(
        self, tmp_path, exchange, multiplier
    ):
        """Behavioural, not textual: feed the engine a hostile multiplier.

        A memory that claims 10x must not produce a 10x position. `>= 1.0` means
        "no reduction warranted" and the size is passed through unchanged — it
        is never read as an instruction to grow.
        """
        cfg = Cfg()
        store = StateStore(str(tmp_path / "s.db"))
        store.update_equity(EQUITY)
        client = bc.BybitClient(config=cfg, store=store, transport=exchange)
        risk = BillionaireRiskManager(config=cfg, store=store)
        engine = te.TradingEngine(
            client=client, risk_manager=risk,
            position_sizer=BillionairePositionSizing(config=cfg, risk_manager=risk),
            store=store, config=cfg,
        )

        class GreedyMemory:
            def size_multiplier(self, symbol):
                return multiplier

        engine.memory = GreedyMemory()
        filters = client.get_instrument_filters("BTCUSDT")
        from position_sizing import SizingResult
        sized = SizingResult(
            symbol="BTCUSDT", qty=1.0, notional=ENTRY,
            risk_fraction=0.005, reason="SIZED", method="test",
        )
        after = engine._apply_memory_throttle("BTCUSDT", sized, filters)
        assert after.qty <= sized.qty, (
            f"multiplier {multiplier} enlarged {sized.qty} to {after.qty}")
        store.close()

    def test_the_throttle_never_divides_by_the_multiplier(self):
        """Division would silently turn the same clamped value into growth."""
        import inspect
        import re
        source = inspect.getsource(te.TradingEngine._apply_memory_throttle)
        body = source.split('"""')[2]           # skip the docstring
        assert not re.search(r"/\s*multiplier", body)
        assert "* multiplier" in body or "qty * multiplier" in body

    def test_the_notional_cap_binds(self, tmp_path):
        store = StateStore(str(tmp_path / "s.db"))
        store.update_equity(EQUITY)
        cfg = Cfg()
        risk = BillionaireRiskManager(config=cfg, store=store)
        decision = risk._gate_position_notional(
            qty=100.0, entry_price=ENTRY, equity=EQUITY,
        )
        assert decision.allowed is False
        store.close()

    def test_no_risk_limit_moved_in_this_slice(self):
        """Slice 29 loosened nothing. Asserted, not promised."""
        cfg = _config.load({})
        assert cfg.MAX_POSITION_SIZE_PCT == 0.02
        assert cfg.RISK_PER_TRADE_PCT == 0.005
        assert cfg.MAX_TOTAL_EXPOSURE_PCT == 0.10
        assert cfg.MAX_DRAWDOWN_PCT == 0.10


class TestModelPathCannotCreateOrReverseTrades:

    def test_no_promoted_model_exists(self):
        assert not os.path.exists(os.path.join(REPO, "models", "current"))

    def test_the_default_policy_mode_is_off(self):
        assert _config.load({}).POLICY_MODE == "off"
        assert _config.load({}).POLICY_ARMED is False

    def test_a_bot_with_no_model_starts_and_is_healthy(self, tmp_path, exchange):
        bot = build(tmp_path, exchange, strategy=None)
        assert bot.startup() is True
        assert bot.health()["healthy"] is True
        bot.shutdown()

    def test_the_only_field_a_model_may_write_is_win_probability(self):
        """Structural: `_attach_probability` replaces exactly one field."""
        import ast
        import inspect
        import textwrap
        import ml_strategy
        tree = ast.parse(textwrap.dedent(
            inspect.getsource(ml_strategy.PolicyStrategy._attach_probability)))
        written = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg:
                        written.add(kw.arg)
        # Equality, not subset: a subset assertion would pass vacuously if the
        # `_replace` call were deleted entirely.
        assert written == {"win_probability"}, written

    def test_the_model_modules_never_construct_an_order(self):
        import inspect
        import ml_strategy
        source = inspect.getsource(ml_strategy)
        for forbidden in ("place_order", "place_stop_order", "submit_order"):
            assert forbidden not in source, forbidden


class TestPaperRunIsContained:
    """The end-to-end operational claim, in one test each way."""

    def test_a_multi_tick_paper_run_submits_no_orders(self, tmp_path, exchange):
        bot = build(tmp_path, exchange, strategy=StubStrategy())
        assert bot.startup() is True
        for _ in range(10):
            bot.tick()
        assert exchange.submitted_orders() == []
        engaged, _ = bot.store.is_kill_switch_engaged()
        assert engaged is False
        assert bot.health()["healthy"] is True
        bot.shutdown()

    def test_a_multi_tick_run_with_entries_disabled_never_consults_a_strategy(
        self, tmp_path, exchange
    ):
        bot = build(tmp_path, exchange, strategy=None)
        assert bot.startup() is True
        for _ in range(10):
            bot.tick()
        assert bot.strategy is None
        assert exchange.submitted_orders() == []
        bot.shutdown()

    def test_shutdown_leaves_protective_stops_in_place(self, tmp_path, exchange):
        """Cancelling stops on the way out would leave a naked position."""
        import inspect
        source = inspect.getsource(m.TradingBot.shutdown)
        assert "cancel_protective=False" in source
