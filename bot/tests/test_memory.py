"""The shared reference layer: what the bot remembers, and what it must never do with it.

Three questions this suite answers, which no other suite does:

1. **Does memory survive the thing memory is for?** State that evaporates on
   restart is not memory, it is a cache. Every table here is written, the store
   is closed, reopened at the same path, and read back.

2. **Can remembered experience ever make a position bigger?** No — and that is
   not a matter of the arithmetic happening to work out. `size_multiplier` is
   hammered with negative counts, NaN, infinities, win rates of 99, a corrupted
   `MEMORY_MIN_THROTTLE` of 5.0, and an empty ledger. It is also asserted
   *structurally*, over the AST, that the function has exactly one return and
   that return is the clamp.

3. **Can it leak a credential or reach a brake?** The snapshot is dumped into a
   health response and into offline learning sessions, so it is scanned for
   configured secrets. And memory.py is asserted, over the AST, never to name
   the kill switch, the breaker, or any other mutating risk call.
"""
from __future__ import annotations

import ast
import json
import math
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import memory as mem  # noqa: E402
import persistence  # noqa: E402
from memory import (  # noqa: E402
    MIN_EXECUTION_SAMPLES,
    MemoryRefused,
    SymbolExperience,
    TradingMemory,
    _clamp_multiplier,
)
from persistence import (  # noqa: E402
    PersistenceError,
    StateStore,
    TradeRecord,
    slippage_bps,
    utc_now_epoch,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMORY_SOURCE = open(os.path.join(REPO, "memory.py"), encoding="utf-8").read()
MEMORY_TREE = ast.parse(MEMORY_SOURCE)

EQUITY = 100_000.0
ENTRY = 100.0


class Cfg:
    """A stand-in Config carrying only what memory.py reads."""

    MEMORY_ENABLED = True
    MEMORY_MIN_SAMPLES = 5
    MEMORY_LOOKBACK_TRADES = 200
    MEMORY_MIN_THROTTLE = 0.25
    SLIPPAGE_BPS = 5.0
    BYBIT_API_KEY = "AKIA_NOT_A_REAL_KEY_9times"
    BYBIT_API_SECRET = "s3cr3t_shhh_do_not_leak_me"


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "memory.db"))
    s.update_equity(EQUITY)
    yield s
    s.close()


@pytest.fixture()
def brain(store):
    return TradingMemory(store, Cfg())


def add_trade(store, symbol="BTCUSDT", net=1.0, qty=1.0, entry=ENTRY, meta=None,
              opened=None, closed=None):
    """Record one closed trade with an exact NET pnl (fees folded in)."""
    now = utc_now_epoch()
    store.record_trade(TradeRecord(
        symbol=symbol, side="Buy", qty=qty, entry_price=entry,
        exit_price=entry + net, gross_pnl=net + 0.2,
        entry_fee=0.1, exit_fee=0.1,
        opened_epoch=(now - 60) if opened is None else opened,
        closed_epoch=now if closed is None else closed,
        meta=dict(meta or {}),
    ))


def add_trades(store, n, symbol="BTCUSDT", net=1.0, **kwargs):
    for _ in range(n):
        add_trade(store, symbol=symbol, net=net, **kwargs)


# ---------------------------------------------------------------------------
# 1. the sample floor — None, never a confident-looking zero
# ---------------------------------------------------------------------------


class TestSampleFloor:
    def test_no_trades_at_all_is_none(self, brain):
        assert brain.symbol_experience("BTCUSDT") is None

    def test_one_below_the_floor_is_none(self, store, brain):
        add_trades(store, Cfg.MEMORY_MIN_SAMPLES - 1)
        assert brain.symbol_experience("BTCUSDT") is None

    def test_exactly_at_the_floor_is_reported(self, store, brain):
        add_trades(store, Cfg.MEMORY_MIN_SAMPLES)
        experience = brain.symbol_experience("BTCUSDT")
        assert experience is not None
        assert experience.samples == Cfg.MEMORY_MIN_SAMPLES

    def test_none_is_not_a_zeroed_record(self, store, brain):
        """A caller must not be able to read `win_rate == 0.0` from ignorance."""
        add_trades(store, 2, net=-1.0)
        assert brain.symbol_experience("BTCUSDT") is None

    def test_the_floor_is_per_symbol_not_per_account(self, store, brain):
        add_trades(store, Cfg.MEMORY_MIN_SAMPLES, symbol="BTCUSDT")
        add_trades(store, 2, symbol="ETHUSDT")
        assert brain.symbol_experience("BTCUSDT") is not None
        assert brain.symbol_experience("ETHUSDT") is None

    def test_trades_with_zero_notional_do_not_count_toward_the_floor(self, store, brain):
        """A zero-notional trade has no return; counting it would let the floor
        be cleared by rows that carry no information."""
        add_trades(store, Cfg.MEMORY_MIN_SAMPLES, qty=0.0)
        assert brain.symbol_experience("BTCUSDT") is None

    def test_symbols_below_the_floor_are_reported_separately(self, store, brain):
        add_trades(store, 3, symbol="ETHUSDT")
        assert brain.symbols_below_floor() == {"ETHUSDT": 3}

    def test_all_symbol_experience_omits_symbols_below_the_floor(self, store, brain):
        add_trades(store, Cfg.MEMORY_MIN_SAMPLES, symbol="BTCUSDT")
        add_trades(store, 1, symbol="ETHUSDT")
        assert set(brain.all_symbol_experience()) == {"BTCUSDT"}


# ---------------------------------------------------------------------------
# 2. the experience arithmetic
# ---------------------------------------------------------------------------


class TestSymbolExperience:
    def test_win_rate_is_fee_aware(self, store, brain):
        """net_pnl is already after fees; a positive net is the only win."""
        add_trades(store, 3, net=2.0)
        add_trades(store, 3, net=-1.0)
        experience = brain.symbol_experience("BTCUSDT")
        assert experience.wins == 3 and experience.losses == 3
        assert experience.win_rate == pytest.approx(0.5)

    def test_expectancy_is_mean_net_pnl(self, store, brain):
        add_trades(store, 5, net=2.0)
        assert brain.symbol_experience("BTCUSDT").expectancy == pytest.approx(2.0)

    def test_mean_return_is_a_fraction_of_notional(self, store, brain):
        add_trades(store, 5, net=1.0, qty=1.0, entry=100.0)
        experience = brain.symbol_experience("BTCUSDT")
        assert experience.mean_return == pytest.approx(0.01)

    def test_edge_bps_is_the_mean_return_in_bps(self, store, brain):
        add_trades(store, 5, net=1.0, qty=1.0, entry=100.0)
        experience = brain.symbol_experience("BTCUSDT")
        assert experience.edge_bps == pytest.approx(100.0)
        assert experience.edge_bps == pytest.approx(experience.mean_return * 10_000)

    def test_median_return_is_not_the_mean(self, store, brain):
        for net in (1.0, 1.0, 1.0, 1.0, 100.0):
            add_trade(store, net=net)
        experience = brain.symbol_experience("BTCUSDT")
        assert experience.median_return == pytest.approx(0.01)
        assert experience.mean_return > experience.median_return

    def test_losses_produce_a_negative_edge(self, store, brain):
        add_trades(store, 6, net=-1.0)
        assert brain.symbol_experience("BTCUSDT").edge_bps < 0.0

    def test_fees_are_totalled(self, store, brain):
        add_trades(store, 5)
        assert brain.symbol_experience("BTCUSDT").total_fees == pytest.approx(1.0)

    def test_last_updated_is_the_newest_close(self, store, brain):
        add_trades(store, 5)
        experience = brain.symbol_experience("BTCUSDT")
        assert experience.last_updated == pytest.approx(utc_now_epoch(), abs=5.0)

    def test_max_adverse_excursion_in_bps_is_read_from_meta(self, store, brain):
        add_trades(store, 5, meta={"mae_bps": 40.0})
        experience = brain.symbol_experience("BTCUSDT")
        assert experience.mean_mae_bps == pytest.approx(40.0)
        assert experience.worst_mae_bps == pytest.approx(40.0)
        assert experience.mae_samples == 5

    def test_max_adverse_excursion_as_a_fraction_is_converted(self, store, brain):
        """House rule: a bare ratio is a FRACTION. 0.004 is 40 bps, not 0.004 bps."""
        add_trades(store, 5, meta={"max_adverse_excursion": 0.004})
        assert brain.symbol_experience("BTCUSDT").mean_mae_bps == pytest.approx(40.0)

    def test_absent_excursion_is_none_not_zero(self, store, brain):
        add_trades(store, 5)
        experience = brain.symbol_experience("BTCUSDT")
        assert experience.mean_mae_bps is None
        assert experience.mae_samples == 0

    def test_lookback_bounds_the_sample(self, store):
        cfg = Cfg()
        cfg.MEMORY_LOOKBACK_TRADES = 7
        add_trades(store, 30)
        assert TradingMemory(store, cfg).symbol_experience("BTCUSDT").samples == 7

    def test_one_symbols_history_never_leaks_into_another(self, store, brain):
        add_trades(store, 10, symbol="BTCUSDT", net=5.0)
        add_trades(store, 10, symbol="ETHUSDT", net=-5.0)
        assert brain.symbol_experience("BTCUSDT").edge_bps > 0
        assert brain.symbol_experience("ETHUSDT").edge_bps < 0


# ---------------------------------------------------------------------------
# 3. restart survival — the whole point of a memory layer
# ---------------------------------------------------------------------------


class TestSurvivesRestart:
    def test_symbol_experience_survives_a_restart(self, tmp_path):
        path = str(tmp_path / "restart.db")
        first = StateStore(path)
        first.update_equity(EQUITY)
        add_trades(first, 8, net=3.0)
        before = TradingMemory(first, Cfg()).symbol_experience("BTCUSDT")
        first.close()

        second = StateStore(path)
        after = TradingMemory(second, Cfg()).symbol_experience("BTCUSDT")
        second.close()
        assert before == after

    def test_execution_quality_survives_a_restart(self, tmp_path):
        path = str(tmp_path / "restart_exec.db")
        first = StateStore(path)
        TradingMemory(first, Cfg()).record_execution(
            "BTCUSDT", "Buy", intended_price=100.0, fill_price=100.5, qty=1.0
        )
        first.close()

        second = StateStore(path)
        stats = TradingMemory(second, Cfg()).execution_quality("BTCUSDT")
        second.close()
        assert stats is not None and stats.mean_slippage_bps == pytest.approx(50.0)

    def test_regime_history_survives_a_restart(self, tmp_path):
        path = str(tmp_path / "restart_regime.db")
        first = StateStore(path)
        TradingMemory(first, Cfg()).record_regime("BTCUSDT", "trend_up")
        first.close()

        second = StateStore(path)
        assert TradingMemory(second, Cfg()).current_regime("BTCUSDT") == "trend_up"
        second.close()

    def test_key_value_memory_survives_a_restart(self, tmp_path):
        path = str(tmp_path / "restart_kv.db")
        first = StateStore(path)
        TradingMemory(first, Cfg()).remember("engine", "last_bar", {"ts": 42})
        first.close()

        second = StateStore(path)
        assert TradingMemory(second, Cfg()).recall("engine", "last_bar") == {"ts": 42}
        second.close()

    def test_block_reasons_survive_a_restart(self, tmp_path):
        path = str(tmp_path / "restart_blocks.db")
        first = StateStore(path)
        first.journal("BTCUSDT", "BLOCK", "edge_below_cost", {})
        first.close()

        second = StateStore(path)
        assert TradingMemory(second, Cfg()).top_block_reasons(5) == [
            ("edge_below_cost", 1)
        ]
        second.close()

    def test_an_existing_v1_database_gains_the_memory_tables(self, tmp_path):
        """Migration must be tolerant: an older store opens and grows, it does
        not refuse. A store that will not open takes the brakes offline exactly
        when a rollback needs them."""
        import sqlite3

        path = str(tmp_path / "legacy.db")
        legacy = sqlite3.connect(path)
        legacy.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        legacy.execute("INSERT INTO meta VALUES ('schema_version', '1')")
        legacy.commit()
        legacy.close()

        upgraded = StateStore(path)
        assert upgraded.schema_version() == 2
        upgraded.record_regime("BTCUSDT", "chop")
        assert upgraded.current_regime("BTCUSDT") == "chop"
        upgraded.close()

    def test_migration_adds_a_missing_column_in_place(self, tmp_path):
        """A partially upgraded database is repaired, not rejected."""
        import sqlite3

        path = str(tmp_path / "partial.db")
        seed = StateStore(path)
        seed.close()
        raw = sqlite3.connect(path)
        raw.execute("ALTER TABLE regime_history RENAME TO regime_history_old")
        raw.execute(
            "CREATE TABLE regime_history (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts_epoch REAL NOT NULL, symbol TEXT NOT NULL, regime TEXT NOT NULL)"
        )
        raw.commit()
        raw.close()

        reopened = StateStore(path)
        columns = {
            row["name"]
            for row in reopened._query("SELECT name FROM pragma_table_info('regime_history')")
        }
        assert "detail" in columns
        reopened.close()

    def test_the_ledger_is_not_duplicated_by_the_memory_layer(self, tmp_path):
        """Memory derives; it does not keep its own copy of the trades."""
        path = str(tmp_path / "single_truth.db")
        s = StateStore(path)
        s.update_equity(EQUITY)
        add_trades(s, 6)
        brain = TradingMemory(s, Cfg())
        brain.snapshot()
        assert len(s.recent_trades(1000)) == 6
        s.close()


# ---------------------------------------------------------------------------
# 4. THE HARD RULE — the multiplier may only ever shrink
# ---------------------------------------------------------------------------


def _experience(**overrides):
    """A SymbolExperience with arbitrary, possibly nonsensical, contents."""
    base = dict(
        symbol="BTCUSDT", samples=50, wins=25, losses=25, win_rate=0.5,
        expectancy=0.0, mean_return=0.0, median_return=0.0, edge_bps=0.0,
        total_net_pnl=0.0, total_fees=0.0,
    )
    base.update(overrides)
    return SymbolExperience(**base)


ADVERSARIAL_EXPERIENCE = [
    _experience(edge_bps=float("nan")),
    _experience(edge_bps=float("inf")),
    _experience(edge_bps=float("-inf")),
    _experience(edge_bps=1e18),
    _experience(edge_bps=-1e18),
    _experience(win_rate=99.0, edge_bps=5000.0),
    _experience(win_rate=-4.0, edge_bps=-5000.0),
    _experience(samples=-5, wins=-3, losses=-2),
    _experience(samples=0, win_rate=float("nan"), expectancy=float("inf")),
    _experience(expectancy=float("nan"), mean_return=float("nan"),
                edge_bps=float("nan")),
    _experience(edge_bps=0.0),
    _experience(edge_bps=1e-300),
]

ADVERSARIAL_THROTTLES = [
    0.25, 0.0, 1.0, 5.0, -1.0, 1e18, float("nan"), float("inf"),
    float("-inf"), None, "banana", [], object(),
]


class TestSizeMultiplierNeverEnlarges:
    @pytest.mark.parametrize("experience", ADVERSARIAL_EXPERIENCE)
    @pytest.mark.parametrize("throttle", ADVERSARIAL_THROTTLES)
    def test_adversarial_experience_and_config_never_exceed_one(
        self, store, experience, throttle
    ):
        """The cartesian product of every hostile input we can construct."""
        cfg = Cfg()
        cfg.MEMORY_MIN_THROTTLE = throttle
        brain = TradingMemory(store, cfg)
        brain.symbol_experience = lambda symbol: experience  # type: ignore[assignment]
        value = brain.size_multiplier("BTCUSDT")
        assert isinstance(value, float)
        assert value <= 1.0, f"memory enlarged a position: {value}"
        assert 0.0 <= value
        assert math.isfinite(value)

    def test_a_corrupted_min_throttle_above_one_cannot_become_a_boost(self, store):
        """MEMORY_MIN_THROTTLE is a *floor*. A floor of 5.0 is nonsense, and the
        one thing it must not do is be honoured as the returned minimum."""
        cfg = Cfg()
        cfg.MEMORY_MIN_THROTTLE = 5.0
        brain = TradingMemory(store, cfg)
        brain.symbol_experience = lambda symbol: _experience(edge_bps=-1e9)
        assert brain.size_multiplier("BTCUSDT") == 1.0

    def test_a_spectacular_winning_streak_earns_no_boost(self, store, brain):
        """'It worked last time' is not additional risk budget."""
        add_trades(store, 60, net=50.0)
        experience = brain.symbol_experience("BTCUSDT")
        assert experience.win_rate == 1.0 and experience.edge_bps > 1000
        assert brain.size_multiplier("BTCUSDT") == 1.0

    def test_a_losing_history_shrinks(self, store, brain):
        add_trades(store, 30, net=-1.0, qty=1.0, entry=100.0)  # -100 bps/trade
        assert brain.size_multiplier("BTCUSDT") < 1.0

    def test_the_shrink_is_bounded_below_by_the_configured_floor(self, store, brain):
        add_trades(store, 30, net=-500.0, qty=1.0, entry=100.0)
        assert brain.size_multiplier("BTCUSDT") == pytest.approx(Cfg.MEMORY_MIN_THROTTLE)

    def test_the_multiplier_is_monotone_in_how_bad_the_history_is(self, tmp_path):
        results = []
        for i, loss in enumerate((-0.1, -0.5, -0.9)):
            s = StateStore(str(tmp_path / f"mono{i}.db"))
            s.update_equity(EQUITY)
            add_trades(s, 30, net=loss, qty=1.0, entry=100.0)
            results.append(TradingMemory(s, Cfg()).size_multiplier("BTCUSDT"))
            s.close()
        assert results == sorted(results, reverse=True)

    @pytest.mark.parametrize("value,floor", [
        (2.0, 0.25), (1.0000001, 0.25), (float("inf"), 0.25),
        (1e308, 0.0), (10, 1.0), (True, 0.25), (3, 0.5), (1.0, 1.0),
    ])
    def test_the_clamp_is_total(self, value, floor):
        assert _clamp_multiplier(value, floor) <= 1.0

    @pytest.mark.parametrize("value", [
        float("nan"), None, "abc", [], {}, object(), -1.0, -1e18,
    ])
    def test_unusable_evidence_collapses_to_the_floor_not_to_a_boost(self, value):
        assert _clamp_multiplier(value, 0.25) == 0.25

    def test_the_clamp_floor_is_itself_clamped(self):
        assert _clamp_multiplier(0.5, 9.0) <= 1.0
        assert _clamp_multiplier(float("nan"), 9.0) == 1.0
        assert _clamp_multiplier(float("nan"), -3.0) == 0.0

    def test_a_value_inside_the_band_passes_through(self):
        assert _clamp_multiplier(0.6, 0.25) == pytest.approx(0.6)


class TestSizeMultiplierWithNoEvidence:
    def test_empty_history_is_exactly_one(self, brain):
        assert brain.size_multiplier("BTCUSDT") == 1.0

    def test_below_the_sample_floor_is_exactly_one(self, store, brain):
        add_trades(store, Cfg.MEMORY_MIN_SAMPLES - 1, net=-100.0)
        assert brain.size_multiplier("BTCUSDT") == 1.0

    def test_an_unknown_symbol_is_exactly_one(self, store, brain):
        add_trades(store, 30, symbol="ETHUSDT", net=-50.0)
        assert brain.size_multiplier("BTCUSDT") == 1.0

    def test_disabled_memory_is_exactly_one_however_bad_the_history(self, store):
        cfg = Cfg()
        cfg.MEMORY_ENABLED = False
        add_trades(store, 40, net=-500.0)
        assert TradingMemory(store, cfg).size_multiplier("BTCUSDT") == 1.0

    def test_a_flat_history_is_exactly_one(self, store, brain):
        add_trades(store, 30, net=0.0)
        assert brain.size_multiplier("BTCUSDT") == 1.0

    def test_an_unreadable_store_shrinks_rather_than_permits(self, store):
        """Fail-closed for a shrink-only knob means shrink, not 1.0."""
        cfg = Cfg()
        brain = TradingMemory(store, cfg)

        def boom(_symbol):
            raise PersistenceError("ledger unreadable")

        brain.symbol_experience = boom  # type: ignore[assignment]
        assert brain.size_multiplier("BTCUSDT") == pytest.approx(cfg.MEMORY_MIN_THROTTLE)

    def test_size_multipliers_covers_every_traded_symbol(self, store, brain):
        add_trades(store, 6, symbol="BTCUSDT")
        add_trades(store, 6, symbol="ETHUSDT")
        multipliers = brain.size_multipliers()
        assert set(multipliers) == {"BTCUSDT", "ETHUSDT"}
        assert all(v <= 1.0 for v in multipliers.values())


class TestSizeMultiplierStructure:
    """Properties of the source, not of one execution."""

    @staticmethod
    def _func(name):
        for node in ast.walk(MEMORY_TREE):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        raise AssertionError(f"memory.py has no function {name}")

    def test_size_multiplier_has_exactly_one_return(self):
        returns = [n for n in ast.walk(self._func("size_multiplier"))
                   if isinstance(n, ast.Return)]
        assert len(returns) == 1, (
            "size_multiplier has more than one exit; each is a place where a "
            "value above 1.0 could escape the clamp"
        )

    def test_the_one_return_is_the_clamp(self):
        node = self._func("size_multiplier")
        returned = [n for n in ast.walk(node) if isinstance(n, ast.Return)][0]
        assert isinstance(returned.value, ast.Call)
        assert isinstance(returned.value.func, ast.Name)
        assert returned.value.func.id == "_clamp_multiplier"

    def test_the_clamp_caps_at_a_literal_one(self):
        """`min(1.0, ...)` must be present as a literal — not read from config,
        which is exactly what a hostile or corrupted config would target."""
        source = ast.get_source_segment(MEMORY_SOURCE, self._func("_clamp_multiplier"))
        assert "min(1.0," in source.replace(" ", ""), (
            "the clamp's upper bound is not a literal 1.0; a configurable cap "
            "is a cap an operator can raise by accident"
        )

    def test_no_module_level_state_in_memory(self):
        allowed = {"logger", "__all__"}
        assigned = [
            t.id for node in MEMORY_TREE.body if isinstance(node, ast.Assign)
            for t in node.targets
            if isinstance(t, ast.Name) and not t.id.isupper() and t.id not in allowed
        ]
        assert assigned == [], f"memory.py has mutable module-level state: {assigned}"


class TestMemoryCannotReachABrake:
    """The standing instruction: nothing may override a risk limit or reset the
    kill switch. This asserts it over the AST rather than trusting review."""

    FORBIDDEN = {
        "trip_kill_switch", "clear_kill_switch_by_human", "trip_breaker",
        "clear_breaker", "set_consecutive_losses", "update_equity",
        "ensure_daily_anchor", "record_trade", "upsert_position",
        "remove_position", "set_position_stop", "set_symbol_cooldown",
        "next_order_seq", "record_order", "update_order_status",
    }

    def test_memory_never_names_a_mutating_risk_call(self):
        called = {
            node.func.attr for node in ast.walk(MEMORY_TREE)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert not (called & self.FORBIDDEN), (
            f"memory.py reaches a brake: {sorted(called & self.FORBIDDEN)}"
        )

    def test_memory_imports_nothing_from_the_risk_layer(self):
        imported = {
            node.module for node in ast.walk(MEMORY_TREE)
            if isinstance(node, ast.ImportFrom)
        } | {
            alias.name for node in ast.walk(MEMORY_TREE)
            if isinstance(node, ast.Import) for alias in node.names
        }
        assert "risk_management" not in imported
        assert "trading_engine" not in imported

    def test_memory_does_not_read_the_environment(self):
        for node in ast.walk(MEMORY_TREE):
            if isinstance(node, ast.Attribute) and node.attr in ("getenv", "environ"):
                pytest.fail(f"memory.py reads the environment at line {node.lineno}")

    def test_memory_spawns_no_thread(self):
        starts = [
            node.lineno for node in ast.walk(MEMORY_TREE)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Thread"
        ]
        assert starts == []

    def test_running_the_kill_switch_is_unaffected_by_memory(self, store, brain):
        """Belt and braces: exercise everything, then assert the brakes are as
        they were."""
        add_trades(store, 30, net=-100.0)
        store.trip_kill_switch("test")
        brain.size_multiplier("BTCUSDT")
        brain.snapshot()
        engaged, reason = store.is_kill_switch_engaged()
        assert engaged and reason == "test"

    def test_memory_does_not_move_the_breaker(self, store, brain):
        store.trip_breaker("test", 3600.0)
        before = store.get_breaker()
        add_trades(store, 30, net=-100.0)
        brain.size_multiplier("BTCUSDT")
        brain.snapshot()
        assert store.get_breaker() == before


# ---------------------------------------------------------------------------
# 5. execution quality — the measurement that can falsify the cost model
# ---------------------------------------------------------------------------


class TestSlippageDirection:
    def test_buying_above_the_intended_price_is_adverse(self):
        assert slippage_bps("Buy", 100.0, 100.5) == pytest.approx(50.0)

    def test_buying_below_the_intended_price_is_favourable(self):
        assert slippage_bps("Buy", 100.0, 99.5) == pytest.approx(-50.0)

    def test_selling_below_the_intended_price_is_adverse(self):
        assert slippage_bps("Sell", 100.0, 99.5) == pytest.approx(50.0)

    def test_selling_above_the_intended_price_is_favourable(self):
        assert slippage_bps("Sell", 100.0, 100.5) == pytest.approx(-50.0)

    def test_the_sign_is_symmetric_between_the_sides(self):
        assert slippage_bps("Buy", 100.0, 101.0) == pytest.approx(
            slippage_bps("Sell", 100.0, 99.0)
        )

    @pytest.mark.parametrize("side", ["Sideways", "", "banana", "BUYY"])
    def test_an_unknown_side_is_refused_not_guessed(self, side):
        with pytest.raises(PersistenceError):
            slippage_bps(side, 100.0, 101.0)

    @pytest.mark.parametrize("price", [0.0, -1.0])
    def test_a_non_positive_reference_price_is_refused(self, price):
        with pytest.raises(PersistenceError):
            slippage_bps("Buy", price, 101.0)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf")])
    def test_non_finite_prices_are_refused(self, bad):
        with pytest.raises(PersistenceError):
            slippage_bps("Buy", 100.0, bad)


class TestExecutionQuality:
    def test_no_records_is_none_not_a_zeroed_report(self, brain):
        assert brain.execution_quality() is None

    def test_a_buy_records_positive_slippage_when_it_pays_up(self, store, brain):
        brain.record_execution("BTCUSDT", "Buy", 100.0, 100.5, qty=1.0)
        assert brain.execution_quality("BTCUSDT").mean_slippage_bps == pytest.approx(50.0)

    def test_a_sell_records_positive_slippage_when_it_sells_down(self, store, brain):
        brain.record_execution("BTCUSDT", "Sell", 100.0, 99.5, qty=1.0)
        assert brain.execution_quality("BTCUSDT").mean_slippage_bps == pytest.approx(50.0)

    def test_a_favourable_fill_pulls_the_mean_down(self, brain):
        brain.record_execution("BTCUSDT", "Buy", 100.0, 100.5, qty=1.0)
        brain.record_execution("BTCUSDT", "Buy", 100.0, 99.5, qty=1.0)
        assert brain.execution_quality("BTCUSDT").mean_slippage_bps == pytest.approx(0.0)

    def test_a_rejected_order_records_no_slippage_at_all(self, brain):
        """NULL, not 0.0 — a fabricated zero would drag the mean toward 'free'."""
        brain.record_execution("BTCUSDT", "Buy", 100.0, outcome="rejected",
                               reason="insufficient_balance")
        stats = brain.execution_quality("BTCUSDT")
        assert stats.slippage_samples == 0
        assert stats.mean_slippage_bps is None
        assert stats.rejects == 1

    def test_reject_reasons_are_counted(self, brain):
        for _ in range(3):
            brain.record_execution("BTCUSDT", "Buy", 100.0, outcome="rejected",
                                   reason="min_notional")
        brain.record_execution("BTCUSDT", "Buy", 100.0, outcome="rejected",
                               reason="price_filter")
        stats = brain.execution_quality("BTCUSDT")
        assert stats.reject_reasons == {"min_notional": 3, "price_filter": 1}

    def test_cancel_reasons_are_counted_separately_from_rejects(self, brain):
        brain.record_execution("BTCUSDT", "Buy", 100.0, outcome="cancelled",
                               reason="maker_timeout")
        stats = brain.execution_quality("BTCUSDT")
        assert stats.cancels == 1 and stats.rejects == 0
        assert stats.cancel_reasons == {"maker_timeout": 1}

    def test_maker_fill_rate_is_over_fills_only(self, brain):
        brain.record_execution("BTCUSDT", "Buy", 100.0, 100.0, qty=1.0, is_maker=True)
        brain.record_execution("BTCUSDT", "Buy", 100.0, 100.0, qty=1.0, is_maker=False)
        brain.record_execution("BTCUSDT", "Buy", 100.0, outcome="rejected")
        assert brain.execution_quality("BTCUSDT").maker_fill_rate == pytest.approx(0.5)

    def test_maker_fill_rate_is_none_when_nothing_filled(self, brain):
        brain.record_execution("BTCUSDT", "Buy", 100.0, outcome="rejected")
        assert brain.execution_quality("BTCUSDT").maker_fill_rate is None

    def test_mean_time_to_fill_is_measured_from_both_epochs(self, brain):
        brain.record_execution("BTCUSDT", "Buy", 100.0, 100.0, qty=1.0,
                               requested_epoch=1_000.0, filled_epoch=1_002.0)
        brain.record_execution("BTCUSDT", "Buy", 100.0, 100.0, qty=1.0,
                               requested_epoch=1_000.0, filled_epoch=1_004.0)
        assert brain.execution_quality("BTCUSDT").mean_time_to_fill_s == pytest.approx(3.0)

    def test_a_negative_duration_is_dropped_not_recorded(self, brain):
        """A fill 'before' its request is a clock problem, not a fast fill."""
        brain.record_execution("BTCUSDT", "Buy", 100.0, 100.0, qty=1.0,
                               requested_epoch=1_000.0, filled_epoch=990.0)
        assert brain.execution_quality("BTCUSDT").fill_time_samples == 0

    def test_time_to_fill_is_absent_without_both_epochs(self, brain):
        brain.record_execution("BTCUSDT", "Buy", 100.0, 100.0, qty=1.0)
        stats = brain.execution_quality("BTCUSDT")
        assert stats.mean_time_to_fill_s is None and stats.fill_time_samples == 0

    def test_worst_slippage_is_the_worst_not_the_last(self, brain):
        for fill in (100.1, 101.0, 100.2):
            brain.record_execution("BTCUSDT", "Buy", 100.0, fill, qty=1.0)
        assert brain.execution_quality("BTCUSDT").worst_slippage_bps == pytest.approx(100.0)

    def test_an_unknown_outcome_is_refused(self, store):
        with pytest.raises(PersistenceError):
            store.record_execution("BTCUSDT", "Buy", 100.0, outcome="maybe")

    def test_an_unknown_side_is_refused(self, store):
        with pytest.raises(PersistenceError):
            store.record_execution("BTCUSDT", "Sideways", 100.0)

    def test_scope_separates_symbols(self, brain):
        brain.record_execution("BTCUSDT", "Buy", 100.0, 101.0, qty=1.0)
        brain.record_execution("ETHUSDT", "Buy", 100.0, 100.0, qty=1.0)
        assert brain.execution_quality("BTCUSDT").mean_slippage_bps == pytest.approx(100.0)
        assert brain.execution_quality("ETHUSDT").mean_slippage_bps == pytest.approx(0.0)
        assert brain.execution_quality().scope == "*"
        assert brain.execution_quality().samples == 2

    def test_cost_model_error_is_none_below_the_sample_floor(self, brain):
        brain.record_execution("BTCUSDT", "Buy", 100.0, 101.0, qty=1.0)
        assert brain.cost_model_error_bps("BTCUSDT") is None

    def test_cost_model_error_reports_the_gap_against_the_assumption(self, brain):
        for _ in range(MIN_EXECUTION_SAMPLES):
            brain.record_execution("BTCUSDT", "Buy", 100.0, 100.15, qty=1.0)
        # 15 bps measured against an assumed 5 bps.
        assert brain.cost_model_error_bps("BTCUSDT") == pytest.approx(10.0)

    def test_expensive_execution_shrinks_the_size(self, store, brain):
        add_trades(store, 10, net=0.0)
        assert brain.size_multiplier("BTCUSDT") == 1.0
        for _ in range(MIN_EXECUTION_SAMPLES):
            brain.record_execution("BTCUSDT", "Buy", 100.0, 100.20, qty=1.0)
        assert brain.size_multiplier("BTCUSDT") < 1.0

    def test_cheap_execution_earns_no_boost(self, store, brain):
        add_trades(store, 10, net=0.0)
        for _ in range(MIN_EXECUTION_SAMPLES):
            brain.record_execution("BTCUSDT", "Buy", 100.0, 99.9, qty=1.0)
        assert brain.size_multiplier("BTCUSDT") == 1.0


# ---------------------------------------------------------------------------
# 6. regime memory
# ---------------------------------------------------------------------------


class TestRegimeMemory:
    def test_current_regime_is_none_before_any_observation(self, brain):
        assert brain.current_regime("BTCUSDT") is None

    def test_the_latest_observation_wins(self, brain):
        brain.record_regime("BTCUSDT", "chop", ts_epoch=1_000.0)
        brain.record_regime("BTCUSDT", "trend_up", ts_epoch=2_000.0)
        assert brain.current_regime("BTCUSDT") == "trend_up"

    def test_regimes_do_not_leak_between_symbols(self, brain):
        brain.record_regime("BTCUSDT", "trend_up", ts_epoch=1_000.0)
        assert brain.current_regime("ETHUSDT") is None

    def test_an_empty_regime_label_is_refused(self, store):
        with pytest.raises(PersistenceError):
            store.record_regime("BTCUSDT", "   ")

    def test_trades_are_attributed_to_the_regime_at_OPEN_time(self, store, brain):
        brain.record_regime("BTCUSDT", "chop", ts_epoch=1_000.0)
        add_trade(store, opened=1_500.0, closed=1_600.0, net=1.0)
        brain.record_regime("BTCUSDT", "trend_up", ts_epoch=1_550.0)
        stats = brain.regime_stats()
        assert stats["chop"].samples == 1
        assert "trend_up" not in stats

    def test_trades_before_any_observation_are_unlabelled_not_dropped(self, store, brain):
        add_trade(store, opened=100.0, closed=200.0, net=1.0)
        brain.record_regime("BTCUSDT", "chop", ts_epoch=1_000.0)
        assert brain.regime_stats()["unlabelled"].samples == 1

    def test_regime_stats_are_insufficient_below_the_floor(self, store, brain):
        brain.record_regime("BTCUSDT", "chop", ts_epoch=1.0)
        add_trades(store, 2, net=1.0)
        stats = brain.regime_stats()["chop"]
        assert stats.insufficient_data
        assert stats.win_rate is None and stats.expectancy is None
        assert stats.samples == 2  # the count is still honest

    def test_regime_stats_report_once_the_floor_is_met(self, store, brain):
        brain.record_regime("BTCUSDT", "chop", ts_epoch=1.0)
        add_trades(store, Cfg.MEMORY_MIN_SAMPLES, net=1.0, qty=1.0, entry=100.0)
        stats = brain.regime_stats()["chop"]
        assert not stats.insufficient_data
        assert stats.win_rate == pytest.approx(1.0)
        assert stats.edge_bps == pytest.approx(100.0)

    def test_regime_stats_list_the_symbols_involved(self, store, brain):
        brain.record_regime("BTCUSDT", "chop", ts_epoch=1.0)
        brain.record_regime("ETHUSDT", "chop", ts_epoch=1.0)
        add_trades(store, 3, symbol="BTCUSDT")
        add_trades(store, 3, symbol="ETHUSDT")
        assert brain.regime_stats()["chop"].symbols == ("BTCUSDT", "ETHUSDT")

    def test_losses_and_wins_are_separated_by_regime(self, store, brain):
        brain.record_regime("BTCUSDT", "trend_up", ts_epoch=1_000.0)
        add_trade(store, opened=1_100.0, closed=1_200.0, net=5.0)
        brain.record_regime("BTCUSDT", "chop", ts_epoch=2_000.0)
        add_trade(store, opened=2_100.0, closed=2_200.0, net=-5.0)
        stats = brain.regime_stats()
        assert stats["trend_up"].total_net_pnl == pytest.approx(5.0)
        assert stats["chop"].total_net_pnl == pytest.approx(-5.0)

    def test_regime_history_is_not_a_copy_of_the_ledger(self, store, brain):
        """The regime table stores observations only; outcomes are joined."""
        brain.record_regime("BTCUSDT", "chop", ts_epoch=1.0)
        rows = store.recent_regimes()
        assert set(rows[0]) == {"id", "ts_epoch", "symbol", "regime", "detail"}


# ---------------------------------------------------------------------------
# 7. block-reason memory
# ---------------------------------------------------------------------------


class TestBlockReasons:
    def test_no_journal_is_an_empty_aggregate(self, brain):
        assert brain.block_reasons() == {}
        assert brain.top_block_reasons() == []
        assert brain.total_blocks() == 0

    def test_reasons_aggregate_across_symbols(self, store, brain):
        for _ in range(3):
            store.journal("BTCUSDT", "BLOCK", "edge_below_cost", {})
        for _ in range(2):
            store.journal("ETHUSDT", "BLOCK", "edge_below_cost", {})
        store.journal("ETHUSDT", "BLOCK", "spread_too_wide", {})
        assert brain.block_reasons() == {"edge_below_cost": 5, "spread_too_wide": 1}

    def test_allowed_decisions_are_not_counted_as_blocks(self, store, brain):
        store.journal("BTCUSDT", "ALLOW", "approved", {})
        store.journal("BTCUSDT", "ENTERED", "bracketed", {})
        store.journal("BTCUSDT", "BLOCK", "spread_too_wide", {})
        assert brain.total_blocks() == 1

    def test_outcomes_are_not_counted_as_blocks(self, store, brain):
        """CLOSED and EMERGENCY are outcomes; counting them would inflate the
        number the operator uses to judge whether the gates are too tight."""
        store.journal("BTCUSDT", "CLOSED", "take_profit", {})
        store.journal("BTCUSDT", "EMERGENCY", "stop_rejected", {})
        assert brain.block_reasons() == {}

    def test_top_block_reasons_is_ordered_by_count(self, store, brain):
        for _ in range(5):
            store.journal("BTCUSDT", "BLOCK", "edge_below_cost", {})
        for _ in range(2):
            store.journal("BTCUSDT", "BLOCK", "spread_too_wide", {})
        store.journal("BTCUSDT", "BLOCK", "stale_data", {})
        assert brain.top_block_reasons(2) == [
            ("edge_below_cost", 5), ("spread_too_wide", 2)
        ]

    def test_ties_break_alphabetically_so_the_dashboard_is_stable(self, store, brain):
        store.journal("BTCUSDT", "BLOCK", "zebra", {})
        store.journal("BTCUSDT", "BLOCK", "alpha", {})
        assert brain.top_block_reasons(2) == [("alpha", 1), ("zebra", 1)]

    @pytest.mark.parametrize("n", [0, -1, -100])
    def test_a_non_positive_n_returns_nothing(self, store, brain, n):
        store.journal("BTCUSDT", "BLOCK", "edge_below_cost", {})
        assert brain.top_block_reasons(n) == []

    def test_block_reasons_can_be_scoped_to_one_symbol(self, store, brain):
        store.journal("BTCUSDT", "BLOCK", "edge_below_cost", {})
        store.journal("ETHUSDT", "BLOCK", "spread_too_wide", {})
        assert brain.block_reasons("BTCUSDT") == {"edge_below_cost": 1}

    def test_block_reasons_by_symbol_separates_a_bad_instrument(self, store, brain):
        for _ in range(4):
            store.journal("ETHUSDT", "BLOCK", "spread_too_wide", {})
        store.journal("BTCUSDT", "BLOCK", "edge_below_cost", {})
        by_symbol = brain.block_reasons_by_symbol()
        assert by_symbol["ETHUSDT"] == {"spread_too_wide": 4}
        assert by_symbol["BTCUSDT"] == {"edge_below_cost": 1}

    def test_an_empty_decision_filter_matches_nothing_rather_than_erroring(self, store):
        store.journal("BTCUSDT", "BLOCK", "x", {})
        assert store.decision_reason_counts(decisions=()) == []

    def test_a_null_decision_filter_aggregates_everything(self, store):
        store.journal("BTCUSDT", "BLOCK", "x", {})
        store.journal("BTCUSDT", "ALLOW", "y", {})
        rows = store.decision_reason_counts(decisions=None)
        assert sum(int(r["n"]) for r in rows) == 2


# ---------------------------------------------------------------------------
# 8. the key/value layer
# ---------------------------------------------------------------------------


class TestKeyValueMemory:
    def test_a_value_round_trips(self, brain):
        brain.remember("engine", "last_bar", {"ts": 1, "close": 2.5})
        assert brain.recall("engine", "last_bar") == {"ts": 1, "close": 2.5}

    def test_a_missing_key_returns_the_default(self, brain):
        assert brain.recall("engine", "nope", default=7) == 7

    def test_writing_twice_updates_rather_than_duplicates(self, brain):
        brain.remember("engine", "k", 1)
        brain.remember("engine", "k", 2)
        assert brain.recall("engine", "k") == 2
        assert brain.recall_namespace("engine") == {"k": 2}

    def test_namespaces_are_isolated(self, brain):
        brain.remember("engine", "k", 1)
        brain.remember("strategy", "k", 2)
        assert brain.recall("engine", "k") == 1
        assert brain.recall("strategy", "k") == 2

    def test_forget_removes_and_reports(self, brain):
        brain.remember("engine", "k", 1)
        assert brain.forget("engine", "k") is True
        assert brain.forget("engine", "k") is False
        assert brain.recall("engine", "k") is None

    def test_a_non_serialisable_value_is_refused_at_the_write(self, brain):
        with pytest.raises(PersistenceError):
            brain.remember("engine", "k", object())

    @pytest.mark.parametrize("key", [
        "api_key", "BYBIT_API_SECRET", "auth", "session_id", "bearer_token",
        "user_password", "private_key",
    ])
    def test_credential_shaped_keys_are_refused_not_redacted(self, brain, key):
        """Refusing the write is what keeps a secret off disk; redaction at read
        time is only a backstop, and the store outlives the process."""
        with pytest.raises(MemoryRefused):
            brain.remember("engine", key, "value")

    def test_a_credential_shaped_namespace_is_refused(self, brain):
        with pytest.raises(MemoryRefused):
            brain.remember("secrets", "k", "v")

    def test_the_reserved_namespace_prefix_is_refused(self, brain):
        with pytest.raises(MemoryRefused):
            brain.remember("_internal", "k", "v")

    def test_an_empty_namespace_or_key_is_refused(self, store):
        with pytest.raises(PersistenceError):
            store.memory_put("", "k", 1)
        with pytest.raises(PersistenceError):
            store.memory_put("ns", "", 1)

    def test_a_poisoned_value_does_not_blind_the_whole_namespace(self, store, brain):
        brain.remember("engine", "good", 1)
        store._exec(
            "INSERT INTO memory_kv(namespace, key, value, updated_epoch)"
            " VALUES('engine','bad','{not json', 0.0)"
        )
        assert brain.recall_namespace("engine") == {"good": 1}


# ---------------------------------------------------------------------------
# 9. concurrency — reads are free, writes are the writer's alone
# ---------------------------------------------------------------------------


class TestSingleWriterAppliesToMemory:
    def test_a_foreign_thread_cannot_record_an_execution(self, store, brain):
        store.claim_writer()
        errors = []

        def intruder():
            try:
                brain.record_execution("BTCUSDT", "Buy", 100.0, 100.1, qty=1.0)
            except PersistenceError as exc:
                errors.append(exc)

        thread = threading.Thread(target=intruder)
        thread.start()
        thread.join()
        assert len(errors) == 1, "a foreign thread wrote execution memory"

    def test_a_foreign_thread_cannot_record_a_regime(self, store, brain):
        store.claim_writer()
        errors = []

        def intruder():
            try:
                brain.record_regime("BTCUSDT", "chop")
            except PersistenceError as exc:
                errors.append(exc)

        thread = threading.Thread(target=intruder)
        thread.start()
        thread.join()
        assert len(errors) == 1

    def test_a_foreign_thread_cannot_remember(self, store, brain):
        store.claim_writer()
        errors = []

        def intruder():
            try:
                brain.remember("engine", "k", 1)
            except PersistenceError as exc:
                errors.append(exc)

        thread = threading.Thread(target=intruder)
        thread.start()
        thread.join()
        assert len(errors) == 1

    def test_a_foreign_thread_cannot_forget(self, store, brain):
        brain.remember("engine", "k", 1)
        store.claim_writer()
        errors = []

        def intruder():
            try:
                brain.forget("engine", "k")
            except PersistenceError as exc:
                errors.append(exc)

        thread = threading.Thread(target=intruder)
        thread.start()
        thread.join()
        assert len(errors) == 1
        assert brain.recall("engine", "k") == 1

    def test_every_read_works_from_a_non_writer_thread(self, store, brain):
        """The health thread reads this and must never be blocked or refused."""
        add_trades(store, 10)
        brain.record_execution("BTCUSDT", "Buy", 100.0, 100.1, qty=1.0)
        brain.record_regime("BTCUSDT", "chop")
        brain.remember("engine", "k", 1)
        store.journal("BTCUSDT", "BLOCK", "edge_below_cost", {})
        store.claim_writer()

        failures = []
        results = []

        def reader():
            try:
                results.append(brain.symbol_experience("BTCUSDT"))
                results.append(brain.execution_quality("BTCUSDT"))
                results.append(brain.regime_stats())
                results.append(brain.block_reasons())
                results.append(brain.top_block_reasons(5))
                results.append(brain.current_regime("BTCUSDT"))
                results.append(brain.recall("engine", "k"))
                results.append(brain.size_multiplier("BTCUSDT"))
                results.append(brain.snapshot())
            except Exception as exc:  # noqa: BLE001
                failures.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert failures == [], f"a read required the writer role: {failures}"
        assert len(results) == 4 * 9

    def test_concurrent_execution_writes_are_all_recorded(self, tmp_path):
        s = StateStore(str(tmp_path / "conc.db"))
        brain = TradingMemory(s, Cfg())

        def record(i):
            brain.record_execution("BTCUSDT", "Buy", 100.0, 100.0 + i / 100.0, qty=1.0)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(record, range(60)))

        assert brain.execution_quality("BTCUSDT").samples == 60
        s.close()

    def test_reads_are_not_blocked_while_the_writer_writes(self, store, brain):
        add_trades(store, 10)
        store.claim_writer()
        seen = []

        def reader():
            for _ in range(30):
                seen.append(brain.size_multiplier("BTCUSDT"))

        threads = [threading.Thread(target=reader) for _ in range(3)]
        for t in threads:
            t.start()
        for _ in range(30):
            brain.record_execution("BTCUSDT", "Buy", 100.0, 100.1, qty=1.0)
        for t in threads:
            t.join()
        assert len(seen) == 90
        assert all(v <= 1.0 for v in seen)


# ---------------------------------------------------------------------------
# 10. the snapshot
# ---------------------------------------------------------------------------


def _walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _walk_strings(v)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


@pytest.fixture()
def populated(store):
    brain = TradingMemory(store, Cfg())
    add_trades(store, 10, symbol="BTCUSDT", net=1.0)
    add_trades(store, 3, symbol="ETHUSDT", net=-1.0)
    brain.record_execution("BTCUSDT", "Buy", 100.0, 100.1, qty=1.0, is_maker=True,
                           requested_epoch=1.0, filled_epoch=2.0)
    brain.record_execution("BTCUSDT", "Sell", 100.0, 99.9, qty=1.0,
                           outcome="rejected", reason="min_notional")
    brain.record_regime("BTCUSDT", "trend_up", ts_epoch=1.0)
    brain.remember("engine", "last_bar", {"ts": 7})
    store.journal("BTCUSDT", "BLOCK", "edge_below_cost", {})
    return brain


class TestSnapshot:
    def test_it_round_trips_through_json(self, populated):
        payload = populated.snapshot()
        assert json.loads(json.dumps(payload)) == payload

    def test_it_is_strict_json_with_no_nan_tokens(self, populated):
        """`json.dumps` emits a bare `NaN` by default, which is not JSON; a
        monitor that fails to parse the health body cannot distinguish that from
        the bot being down."""
        json.dumps(populated.snapshot(), allow_nan=False)

    def test_it_contains_no_configured_credential(self, populated):
        blob = json.dumps(populated.snapshot())
        assert Cfg.BYBIT_API_KEY not in blob
        assert Cfg.BYBIT_API_SECRET not in blob

    def test_a_credential_smuggled_under_an_innocent_key_is_redacted(self, store):
        """`remember` refuses secret-shaped *names*; this catches secret *values*."""
        brain = TradingMemory(store, Cfg())
        brain.remember("engine", "note", f"connect with {Cfg.BYBIT_API_KEY}")
        blob = json.dumps(brain.snapshot())
        assert Cfg.BYBIT_API_KEY not in blob
        assert mem.REDACTED in blob

    def test_a_credential_shaped_key_written_directly_to_the_store_is_redacted(self, store):
        brain = TradingMemory(store, Cfg())
        store.memory_put("engine", "api_key", "leak-me-please")
        payload = brain.snapshot()
        assert payload["kv"]["engine"]["api_key"] == mem.REDACTED

    def test_it_reports_the_thresholds_it_used(self, populated):
        thresholds = populated.snapshot()["thresholds"]
        assert thresholds["min_samples"] == Cfg.MEMORY_MIN_SAMPLES
        assert thresholds["lookback_trades"] == Cfg.MEMORY_LOOKBACK_TRADES
        assert thresholds["min_throttle"] == Cfg.MEMORY_MIN_THROTTLE

    def test_it_separates_measured_symbols_from_unmeasured_ones(self, populated):
        payload = populated.snapshot()
        assert "BTCUSDT" in payload["symbols"]
        assert "ETHUSDT" not in payload["symbols"]
        assert payload["symbols_below_sample_floor"] == {"ETHUSDT": 3}

    def test_every_size_multiplier_in_the_snapshot_is_at_most_one(self, populated):
        assert all(v <= 1.0 for v in populated.snapshot()["size_multipliers"].values())

    def test_it_carries_execution_quality(self, populated):
        quality = populated.snapshot()["execution_quality"]
        assert quality["overall"]["samples"] == 2
        assert quality["by_symbol"]["BTCUSDT"]["rejects"] == 1

    def test_it_carries_blocks_and_regimes(self, populated):
        payload = populated.snapshot()
        assert payload["blocks"]["total"] == 1
        assert payload["blocks"]["top"] == [["edge_below_cost", 1]]
        assert payload["current_regime"]["BTCUSDT"] == "trend_up"
        assert "trend_up" in payload["regimes"]

    def test_an_empty_store_still_produces_a_valid_snapshot(self, brain):
        payload = brain.snapshot()
        json.dumps(payload, allow_nan=False)
        assert payload["symbols"] == {}
        assert payload["errors"] == []

    def test_a_broken_section_is_reported_not_raised(self, populated):
        """The health endpoint may not raise; a failure must be visible instead."""
        def boom():
            raise RuntimeError("db exploded")

        populated.all_symbol_experience = boom  # type: ignore[assignment]
        payload = populated.snapshot()
        assert payload["symbols"] == {}
        assert any("symbols" in e for e in payload["errors"])

    def test_it_is_tagged_with_a_schema_version(self, populated):
        assert populated.snapshot()["schema"] == mem.SNAPSHOT_SCHEMA

    def test_it_reports_whether_memory_is_enabled(self, store):
        cfg = Cfg()
        cfg.MEMORY_ENABLED = False
        assert TradingMemory(store, cfg).snapshot()["enabled"] is False

    def test_no_snapshot_string_looks_like_a_long_opaque_secret(self, populated):
        for text in _walk_strings(populated.snapshot()):
            assert Cfg.BYBIT_API_SECRET not in text


# ---------------------------------------------------------------------------
# 11. the store's existing surface is untouched
# ---------------------------------------------------------------------------


class TestExistingSurfaceIsPreserved:
    @pytest.mark.parametrize("name,signature", [
        ("record_trade", ["trade"]),
        ("recent_trades", ["limit"]),
        ("net_returns", ["limit"]),
        ("journal", ["symbol", "decision", "reason", "detail"]),
        ("recent_decisions", ["limit"]),
        ("upsert_position", ["symbol", "side", "qty", "entry_price",
                             "stop_price", "order_link_id", "meta"]),
        ("record_order", ["order_link_id", "symbol", "side", "order_type", "qty",
                          "price", "purpose", "status", "meta"]),
        ("update_equity", ["equity"]),
        ("trip_kill_switch", ["reason"]),
        ("clear_kill_switch_by_human", ["operator_ack"]),
    ])
    def test_signature_is_unchanged(self, name, signature):
        import inspect

        params = list(inspect.signature(getattr(StateStore, name)).parameters)
        assert params[0] == "self"
        assert params[1:] == signature

    def test_the_kill_switch_still_needs_the_human_token(self, store):
        store.trip_kill_switch("x")
        with pytest.raises(PersistenceError):
            store.clear_kill_switch_by_human("please")
        assert store.is_kill_switch_engaged()[0]

    def test_the_schema_version_is_recorded_as_two(self, store):
        assert store.schema_version() == 2

    def test_the_memory_tables_exist(self, store):
        names = {
            str(r["name"]) for r in store._query(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"execution_quality", "regime_history", "memory_kv"} <= names
