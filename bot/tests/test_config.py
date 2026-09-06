"""Contract tests for the single config loader.

Fully offline. Config must never touch the network at import or load time.

The properties map 1:1 to the rules the module exists to enforce:

  fractions everywhere      -> TestFractionConvention
  fail closed at load time  -> TestFailClosed, TestCrossFieldValidation
  no fabricated data        -> TestNoFabricatedData
  one source of truth       -> TestSingleSourceOfTruth
  the live gate             -> TestLiveGate
"""
from __future__ import annotations

import ast
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_module  # noqa: E402

ConfigError = config_module.ConfigError

LIVE_ENV = {
    "USE_TESTNET": "0",
    "PAPER_TRADING": "0",
    "LIVE_TRADING_ACK": "I_UNDERSTAND",
    "BYBIT_API_KEY": "key",
    "BYBIT_API_SECRET": "secret",
}


def load(**overrides: str):
    """Load from an explicit env mapping — never the ambient os.environ."""
    return config_module.load(dict(overrides))


PCT_VARS = [
    "MAX_POSITION_SIZE_PCT",
    "MAX_DAILY_LOSS_PCT",
    "STOP_LOSS_PCT",
    "MAX_TOTAL_EXPOSURE_PCT",
    "MAX_DRAWDOWN_PCT",
    "MAX_CORRELATION_EXPOSURE_PCT",
    "MIN_CONFIDENCE",
    "MIN_COMPONENT_AGREEMENT",
]


# ---------------------------------------------------------------------------
# units
# ---------------------------------------------------------------------------


class TestFractionConvention:
    @pytest.mark.parametrize("var", PCT_VARS)
    def test_fraction_is_preserved_exactly(self, var):
        assert getattr(load(**{var: "0.02"}), var) == pytest.approx(0.02)

    @pytest.mark.parametrize("var", PCT_VARS)
    def test_percent_style_value_is_rejected_not_rescaled(self, var):
        """A value > 1 is the 100x bug class. Reject, never divide.

        Silent rescaling is what let one variable mean 70% in one module and
        0.7% in another, in the same process.
        """
        with pytest.raises(ConfigError) as excinfo:
            load(**{var: "2"})
        assert "FRACTION" in str(excinfo.value)

    @pytest.mark.parametrize("var", PCT_VARS)
    def test_negative_is_rejected(self, var):
        with pytest.raises(ConfigError):
            load(**{var: "-0.01"})

    def test_all_pct_defaults_are_fractions(self):
        cfg = load()
        for var in PCT_VARS:
            value = getattr(cfg, var)
            assert 0.0 <= value <= 1.0, f"default {var}={value} is not a fraction"

    def test_error_message_shows_the_correct_value(self):
        with pytest.raises(ConfigError) as excinfo:
            load(MAX_POSITION_SIZE_PCT="50")
        assert "0.5" in str(excinfo.value)

    def test_bps_fields_convert_to_fractions_once(self):
        """Fees are quoted in bps; converting once here beats converting at
        every use site."""
        cfg = load(TAKER_FEE_BPS="10", SLIPPAGE_BPS="5")
        assert cfg.TAKER_FEE == pytest.approx(0.001)
        assert cfg.SLIPPAGE == pytest.approx(0.0005)
        assert cfg.ROUND_TRIP_COST_BPS == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# parsers
# ---------------------------------------------------------------------------


class TestParsers:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1", True), ("true", True), ("TRUE", True), ("yes", True),
            ("on", True), ("t", True), ("y", True),
            ("0", False), ("false", False), ("no", False), ("off", False),
            ("", False), ("banana", False), (None, False),
        ],
    )
    def test_parse_bool_truth_table(self, raw, expected):
        assert config_module.parse_bool(raw, False) is expected

    def test_use_testnet_accepts_numeric_one(self):
        """A predecessor compared `== "true"`, so USE_TESTNET=1 meant MAINNET."""
        for token in ("1", "true", "yes", "on"):
            assert load(USE_TESTNET=token).USE_TESTNET is True
        assert load(USE_TESTNET="0").USE_TESTNET is False

    def test_default_is_testnet_and_paper(self):
        cfg = load()
        assert cfg.USE_TESTNET is True
        assert cfg.PAPER_TRADING is True

    def test_symbols_are_parsed_and_validated(self):
        assert load(TRADING_SYMBOLS="btcusdt, ethusdt").TRADING_SYMBOLS == (
            "BTCUSDT", "ETHUSDT"
        )

    @pytest.mark.parametrize("bad", ["BTC-USDT", "b", "", "BTC USDT!"])
    def test_bad_symbols_are_rejected(self, bad):
        with pytest.raises(ConfigError):
            load(TRADING_SYMBOLS=bad)

    def test_unknown_timeframe_is_rejected(self):
        with pytest.raises(ConfigError):
            load(PRIMARY_TIMEFRAME="1fortnight")

    def test_indicator_thresholds_merge_over_defaults(self):
        cfg = load(INDICATOR_THRESHOLDS='{"RSI_PERIOD": 21}')
        assert cfg.INDICATOR_THRESHOLDS["RSI_PERIOD"] == 21
        assert "MACD_FAST" in cfg.INDICATOR_THRESHOLDS

    def test_malformed_json_is_rejected(self):
        with pytest.raises(ConfigError):
            load(INDICATOR_THRESHOLDS="{not json")


# ---------------------------------------------------------------------------
# the live gate
# ---------------------------------------------------------------------------


class TestLiveGate:
    def test_sandbox_may_start(self):
        """assert_sandbox is the STARTUP gate: paper/testnet is a PASS."""
        ok, why = config_module.assert_sandbox(load())
        assert ok is True, why

    def test_live_fully_authorised(self):
        cfg = load(**LIVE_ENV)
        assert config_module.is_live_authorized(cfg)[0] is True
        assert config_module.assert_sandbox(cfg)[0] is True
        assert cfg.PAPER_TRADING is False

    @pytest.mark.parametrize(
        "drop", ["LIVE_TRADING_ACK", "BYBIT_API_KEY", "BYBIT_API_SECRET"]
    )
    def test_missing_any_requirement_blocks_live(self, drop):
        cfg = load(**{k: v for k, v in LIVE_ENV.items() if k != drop})
        assert config_module.is_live_authorized(cfg)[0] is False

    def test_wrong_ack_blocks_live(self):
        cfg = load(**{**LIVE_ENV, "LIVE_TRADING_ACK": "yes"})
        assert config_module.is_live_authorized(cfg)[0] is False

    def test_unauthorised_live_request_degrades_to_paper(self):
        cfg = load(USE_TESTNET="0", PAPER_TRADING="0")
        assert cfg.LIVE_AUTHORIZED is False
        assert cfg.PAPER_TRADING is True, "must degrade to paper, not stay armed"
        assert cfg.LIVE_REQUESTED is True, "the operator's intent is still recorded"

    def test_force_live_is_inert(self):
        cfg = load(**{**LIVE_ENV, "LIVE_TRADING_ACK": "", "FORCE_LIVE": "1"})
        assert config_module.is_live_authorized(cfg)[0] is False
        assert cfg.PAPER_TRADING is True

    def test_force_live_appears_nowhere_in_the_codebase(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        offenders = []
        for name in sorted(os.listdir(repo)):
            if not name.endswith(".py"):
                continue
            for lineno, line in enumerate(
                open(os.path.join(repo, name), encoding="utf-8"), 1
            ):
                if "FORCE_LIVE" in line and not line.lstrip().startswith("#"):
                    if "``" in line or '"""' in line:
                        continue
                    offenders.append(f"{name}:{lineno}")
        assert offenders == [], f"FORCE_LIVE still referenced: {offenders}"

    def test_base_url_follows_the_testnet_flag(self):
        assert "testnet" in load(USE_TESTNET="1").BYBIT_BASE_URL
        assert "testnet" not in load(**LIVE_ENV).BYBIT_BASE_URL


# ---------------------------------------------------------------------------
# cross-field validation
# ---------------------------------------------------------------------------


class TestCrossFieldValidation:
    def test_risk_budget_above_position_cap_is_rejected(self):
        """The cap would always bind, so the risk budget would be decorative."""
        with pytest.raises(ConfigError):
            load(RISK_PER_TRADE_PCT="0.05", MAX_POSITION_SIZE_PCT="0.02")

    def test_position_cap_above_total_exposure_is_rejected(self):
        with pytest.raises(ConfigError):
            load(MAX_POSITION_SIZE_PCT="0.5", MAX_TOTAL_EXPOSURE_PCT="0.1")

    def test_daily_loss_above_drawdown_is_rejected(self):
        """Otherwise the daily brake never engages before the kill switch."""
        with pytest.raises(ConfigError):
            load(MAX_DAILY_LOSS_PCT="0.5", MAX_DRAWDOWN_PCT="0.1")

    def test_edge_floor_below_round_trip_cost_is_rejected(self):
        """Accepting trades that cannot pay their own fees is the core defect
        this gate exists to prevent."""
        with pytest.raises(ConfigError) as excinfo:
            load(MIN_EDGE_BPS="1", TAKER_FEE_BPS="10", SLIPPAGE_BPS="5")
        assert "round-trip" in str(excinfo.value)

    def test_default_edge_floor_clears_default_costs(self):
        cfg = load()
        assert cfg.MIN_EDGE_BPS >= cfg.ROUND_TRIP_COST_BPS

    def test_edge_floor_default_tracks_configured_fees(self):
        """Raising fees must raise the edge floor, not leave it stale."""
        cheap = load(TAKER_FEE_BPS="1", SLIPPAGE_BPS="1")
        dear = load(TAKER_FEE_BPS="40", SLIPPAGE_BPS="20")
        assert dear.MIN_EDGE_BPS > cheap.MIN_EDGE_BPS

    def test_secondary_timeframe_must_be_higher_than_primary(self):
        """A timeframe confirming itself is not confluence."""
        with pytest.raises(ConfigError):
            load(PRIMARY_TIMEFRAME="240", SECONDARY_TIMEFRAMES="60")

    def test_primary_timeframe_cannot_appear_in_secondaries(self):
        with pytest.raises(ConfigError):
            load(PRIMARY_TIMEFRAME="60", SECONDARY_TIMEFRAMES="60,240")

    def test_leverage_cannot_exceed_the_eu_cap(self):
        with pytest.raises(ConfigError):
            load(MAX_LEVERAGE_EU="5", DEFAULT_LEVERAGE="10")


# ---------------------------------------------------------------------------
# fail closed
# ---------------------------------------------------------------------------


class TestFailClosed:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"MAX_POSITION_SIZE_PCT": "not-a-number"},
            {"MAX_OPEN_POSITIONS": "lots"},
            {"LOG_LEVEL": "CHATTY"},
            {"MAX_LEVERAGE_EU": "9999"},
            {"MIN_RISK_REWARD_RATIO": "0.5"},
            {"BYBIT_RECV_WINDOW_MS": "999999"},
            {"HEALTHCHECK_PORT": "70000"},
            {"KLINE_LOOKBACK": "5"},
            {"MIN_KELLY_SAMPLES": "2"},
        ],
    )
    def test_bad_value_raises_at_load_time(self, kwargs):
        with pytest.raises(ConfigError):
            load(**kwargs)

    def test_sub_one_risk_reward_is_rejected(self):
        """RR below 1.0 accepts trades that lose money on average."""
        with pytest.raises(ConfigError):
            load(MIN_RISK_REWARD_RATIO="0.5")

    def test_kelly_sample_floor_cannot_be_lowered_below_thirty(self):
        with pytest.raises(ConfigError):
            load(MIN_KELLY_SAMPLES="5")


# ---------------------------------------------------------------------------
# no fabricated data, no global mutation
# ---------------------------------------------------------------------------


class TestNoFabricatedData:
    def test_no_credentials_in_defaults(self):
        cfg = load()
        assert cfg.BYBIT_API_KEY == ""
        assert cfg.BYBIT_API_SECRET == ""
        assert cfg.HAS_CREDENTIALS is False

    def test_credentials_never_leak_through_a_loggable_surface(self):
        cfg = load(BYBIT_API_KEY="SECRETKEY123", BYBIT_API_SECRET="SECRETVAL456")
        for surface in (repr(cfg), str(cfg), str(cfg.safe_dict())):
            assert "SECRETKEY123" not in surface
            assert "SECRETVAL456" not in surface

    def test_redaction_does_not_leak_length(self):
        assert config_module.redact("a") == config_module.redact("a" * 64)

    def test_as_dict_is_the_only_surface_carrying_secrets(self):
        cfg = load(BYBIT_API_KEY="SECRETKEY123", BYBIT_API_SECRET="x")
        assert cfg.as_dict()["BYBIT_API_KEY"] == "SECRETKEY123"
        assert "do not log" in (type(cfg).as_dict.__doc__ or "").lower()

    def test_no_offline_instrument_filters(self):
        """Fabricated tick/step tables mis-size and silently reject orders."""
        assert not hasattr(config_module, "OFFLINE_FILTERS_SEED")

    def test_no_fallback_balance_constant(self):
        for name in ("DEFAULT_ACCOUNT_BALANCE", "FALLBACK_BALANCE_USDT",
                     "INITIAL_CAPITAL"):
            assert not hasattr(load(), name)


class TestNoEnvironmentMutation:
    def test_load_does_not_touch_os_environ(self):
        before = dict(os.environ)
        load(**LIVE_ENV)
        config_module.load()
        assert dict(os.environ) == before

    def test_import_does_not_mutate_os_environ(self):
        before = dict(os.environ)
        importlib.reload(config_module)
        assert dict(os.environ) == before

    def test_os_getenv_is_not_monkeypatched(self):
        assert os.getenv.__module__ == "os"

    def test_no_environ_writes_in_source(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.py"
        )
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
                value = node.value
                if isinstance(value, ast.Attribute) and value.attr == "environ":
                    pytest.fail(f"config.py writes os.environ at line {node.lineno}")


# ---------------------------------------------------------------------------
# one source of truth
# ---------------------------------------------------------------------------


class TestSingleSourceOfTruth:
    def test_load_is_deterministic(self):
        a, b = load(**LIVE_ENV), load(**LIVE_ENV)
        assert a.as_dict() == b.as_dict()
        assert a.checksum() == b.checksum()

    def test_different_env_gives_a_different_checksum(self):
        assert load(MAX_POSITION_SIZE_PCT="0.01").checksum() != \
            load(MAX_POSITION_SIZE_PCT="0.02").checksum()

    def test_config_is_immutable(self):
        """Nothing may mutate the config behind another module's back."""
        cfg = load()
        with pytest.raises(Exception):
            cfg.MAX_POSITION_SIZE_PCT = 0.99

    def test_no_duplicate_top_level_definitions(self):
        import collections

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.py"
        )
        tree = ast.parse(open(path, encoding="utf-8").read())
        names = collections.Counter(
            n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        )
        assert {k: v for k, v in names.items() if v > 1} == {}

    def test_the_exported_surface_is_small_and_deliberate(self):
        """Every legacy compatibility alias is gone: the modules that needed
        them no longer exist, and an unused export is a future accident.

        Slice 8 added two names, and both are the same KIND of thing as
        REQUIRED_LIVE_ACK: a token or an enumeration that other modules must
        agree with exactly. If `ml_strategy` and `config` each spelled the
        acknowledgement string themselves, they would eventually spell it
        differently, and the failure mode of that is a gate that silently
        never arms — or silently always does.
        """
        assert set(config_module.__all__) == {
            "Config", "ConfigError", "load", "reload", "get_config_object",
            "assert_sandbox", "is_live_authorized", "setup_logging",
            "parse_bool", "parse_pct", "redact", "REQUIRED_LIVE_ACK",
            "REQUIRED_POLICY_ACK", "POLICY_MODES",
        }

    def test_every_live_module_imports_cleanly(self):
        for module in ("persistence", "risk_management", "position_sizing",
                       "bybit_connection", "trading_engine", "pure_indicators",
                       "technical_analysis", "performance_analytics",
                       "main", "backtest"):
            importlib.import_module(module)
