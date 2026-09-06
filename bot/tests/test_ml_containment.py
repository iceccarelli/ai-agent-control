"""Slice 8: proof that the learned policy cannot escape the safety architecture.

This file exists to answer one question, and it asks it in as many ways as it
can: **can a model — a good one, a broken one, or a hostile one — cause this
system to do something the classical stack would have refused?**

The answer must be no, for every gate, in every mode. A model that can raise a
limit is not a trading model, it is a way of laundering a limit change through
something that sounds scientific.

The organising claim of slice 8 in one line:

    A model may PROPOSE. It may not decide, resize, override, or resume.

Fully offline. No network, no sleeping, no training of real models here (that
belongs in `tests/test_policy.py` and `tests/test_training.py`).
"""
from __future__ import annotations

import ast
import inspect
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as _config      # noqa: E402
import ml_strategy as ml      # noqa: E402
import risk_management as rm  # noqa: E402
import trading_engine as te   # noqa: E402
from persistence import StateStore  # noqa: E402

EQUITY = 10_000.0
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _code_only(module: str) -> str:
    """Source with docstrings and comments stripped.

    A module that documents what it must not do necessarily contains those
    words. Grepping the raw text would make the documentation of a guarantee
    fail the test for that guarantee — a real false positive that has bitten
    this codebase before (TEST_REPORT §4).
    """
    path = os.path.join(REPO, module)
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


# ---------------------------------------------------------------------------
# doubles
# ---------------------------------------------------------------------------


class FakeDecision:
    """Whatever a model might return, including things it should not."""

    def __init__(self, *, usable=True, probability=0.9, direction="LONG",
                 reason="OK"):
        self.usable = usable
        self.probability = probability
        self.direction = direction
        self.reason = reason

    @property
    def probability_in_direction(self):
        if self.probability is None:
            return None
        return self.probability if self.direction != "SHORT" else 1.0 - self.probability


class FakePolicy:
    """A model that says exactly what a test tells it to."""

    artefact_hash = "deadbeefcafe0000"

    def __init__(self, decision=None):
        self.decision = decision if decision is not None else FakeDecision()
        self.calls = 0

    def predict_edge(self, features, **kw):
        self.calls += 1
        return self.decision


def _candles(n=400, seed=7):
    """Real `Candles`, long enough that the feature window is warm.

    The wrapper refuses to consult the model on a cold window, so a doubles-only
    fixture would silently exercise the refusal path in every test and prove
    nothing about the live one. These are genuine candles through the genuine
    feature pipeline.
    """
    import numpy as np

    from technical_analysis import Candles

    rng = np.random.default_rng(seed)
    price = 30_000.0
    o, h, l, c, v = [], [], [], [], []
    for _ in range(n):
        opened = price
        price = max(1.0, price * (1 + rng.normal(0.0002, 0.006)))
        o.append(opened)
        h.append(max(opened, price) * (1 + abs(rng.normal(0, 0.001))))
        l.append(min(opened, price) * (1 - abs(rng.normal(0, 0.001))))
        c.append(price)
        v.append(abs(rng.normal(1000, 200)) + 1.0)
    return Candles(tuple(o), tuple(h), tuple(l), tuple(c), tuple(v), "60")


def _rows(n=400, seed=7):
    """Bybit kline rows behind `_candles`, so the real feature pipeline runs."""
    candles = _candles(n, seed)
    return [
        [str(1_600_000_000_000 + i * 3_600_000), str(candles.open[i]),
         str(candles.high[i]), str(candles.low[i]), str(candles.close[i]),
         str(candles.volume[i]), "0"]
        for i in range(len(candles))
    ]


class FakeBase:
    """A classical strategy that always wants the same long."""

    def __init__(self, intent=None, action="BUY", confidence=0.4):
        self._intent = intent if intent is not None else _intent()
        self.last_signal = type("S", (), {"action": action,
                                          "confidence": confidence})()
        self.last_candles = _candles()
        self.last_rows = _rows()
        self.client = None
        self.interval = "60"
        self.lookback = 400
        self.calls = 0

    def signal_for(self, symbol):
        self.calls += 1
        return self._intent


def _intent(**over):
    base = dict(symbol="BTCUSDT", signal_type="BUY", entry_price=50_000.0,
                stop_price=49_500.0,
                take_profits=((50_600.0, 0.5), (51_400.0, 0.5)),
                confidence=0.4)
    base.update(over)
    return te.TradeIntent(**base)


def cfg_for(mode="off", **over):
    env = {"POLICY_MODE": mode, "USE_TESTNET": "1", "PAPER_TRADING": "1"}
    if mode == "live":
        env.setdefault("POLICY_ACK", _config.REQUIRED_POLICY_ACK)
    env.update({k: str(v) for k, v in over.items()})
    return _config.load(env)


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "s.db"))
    s.update_equity(EQUITY)
    yield s
    s.close()


def good_order(**over):
    base = dict(symbol="BTCUSDT", side="Buy", entry_price=50_000.0,
                stop_loss=49_500.0, quantity=0.001, account_equity=EQUITY)
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# 1. confidence is not a probability — the slice-8 correctness fix
# ---------------------------------------------------------------------------


class TestConfidenceIsNotAProbability:
    def test_confidence_no_longer_drives_the_edge_arithmetic(self, store):
        """Through slice 7 the edge gate used `p = confidence`.

        `confidence` is an agreement-weighted vote magnitude. It lives in [0,1]
        and looks like a probability, which is exactly why the conflation
        survived six slices. It has no frequency interpretation: 0.15 means
        "the components mildly agree", not "wins 15% of the time".

        The evidence it was wrong is arithmetic. At p=0.15 with a 2:1 payoff,
        0.15*2 - 0.85*1 is negative before a single basis point of cost, so the
        gate rejected essentially everything — for a reason that was a property
        of the input, not of the trade.
        """
        manager = rm.BillionaireRiskManager(config=cfg_for(), store=store)
        low = manager._gate_expected_edge(
            entry_price=50_000.0, stop_price=49_500.0,
            take_profit_price=51_500.0, confidence=0.05,
        )
        high = manager._gate_expected_edge(
            entry_price=50_000.0, stop_price=49_500.0,
            take_profit_price=51_500.0, confidence=0.95,
        )
        assert low.detail == high.detail, (
            "confidence still changes the edge computation; it must not"
        )
        assert "reward-only" in low.reason or "reward-only" in str(low.detail)

    def test_with_no_calibrated_probability_the_fallback_is_the_STRICTER_one(
        self, store
    ):
        """Reward-leg-alone against cost, not a coin flip, and not confidence.

        A coin flip would be the charitable assumption. The charitable
        assumption is the wrong default for a gate whose entire job is to
        refuse trades that cannot pay for themselves.
        """
        manager = rm.BillionaireRiskManager(config=cfg_for(), store=store)
        decision = manager._gate_expected_edge(
            entry_price=50_000.0, stop_price=49_500.0,
            take_profit_price=50_100.0, confidence=0.99,
        )
        assert decision.ok is False, "a 20 bps target cleared a 25 bps round trip"

    def test_a_calibrated_probability_is_used(self, store):
        manager = rm.BillionaireRiskManager(config=cfg_for(), store=store)
        generous = manager._gate_expected_edge(
            entry_price=50_000.0, stop_price=49_500.0,
            take_profit_price=51_500.0, confidence=None, win_probability=0.9,
        )
        stingy = manager._gate_expected_edge(
            entry_price=50_000.0, stop_price=49_500.0,
            take_profit_price=51_500.0, confidence=None, win_probability=0.1,
        )
        assert generous.detail["edge_bps"] > stingy.detail["edge_bps"]
        assert stingy.ok is False

    @pytest.mark.parametrize("bad", [1.7, -0.2, float("nan"), float("inf")])
    def test_a_probability_outside_zero_one_BLOCKS_rather_than_clamping(
        self, store, bad
    ):
        """Clamping a broken model's 1.7 to 1.0 would produce the most
        confident trade the system can express, from the least trustworthy
        input it has ever received."""
        manager = rm.BillionaireRiskManager(config=cfg_for(), store=store)
        decision = manager._gate_expected_edge(
            entry_price=50_000.0, stop_price=49_500.0,
            take_profit_price=51_500.0, confidence=None, win_probability=bad,
        )
        assert decision.ok is False
        assert decision.reason == "WIN_PROBABILITY_INVALID"

    def test_the_gate_chain_passes_win_probability_through(self, store):
        manager = rm.BillionaireRiskManager(config=cfg_for(), store=store)
        blocked = manager.gate_order(
            **good_order(take_profit=51_500.0, win_probability=0.05)
        )
        assert blocked.reason == "EDGE_BELOW_COST"

    def test_intent_carries_probability_and_confidence_separately(self):
        intent = _intent(confidence=0.4, win_probability=0.62)
        assert intent.confidence == 0.4
        assert intent.win_probability == 0.62

    def test_a_plain_intent_has_no_probability(self):
        """The default must be "not estimated", never a placeholder."""
        assert _intent().win_probability is None


# ---------------------------------------------------------------------------
# 2. the three modes
# ---------------------------------------------------------------------------


class TestTheThreeModes:
    def test_only_three_modes_exist(self):
        assert _config.POLICY_MODES == ("off", "shadow", "live")

    @pytest.mark.parametrize("bad", ["on", "auto", "yes", "maybe", "LIVE_ISH", ""])
    def test_an_unknown_mode_is_refused_not_interpreted(self, bad):
        """The plausible misreading of an unknown mode is the permissive one."""
        with pytest.raises(_config.ConfigError):
            cfg_for(bad)

    def test_live_without_the_acknowledgement_degrades_to_unarmed(self):
        cfg = _config.load({"POLICY_MODE": "live"})
        assert cfg.POLICY_MODE == "live"
        assert cfg.POLICY_ARMED is False
        assert "POLICY_BLOCKED" in cfg.POLICY_BLOCK_REASON

    def test_live_with_the_acknowledgement_arms(self):
        assert cfg_for("live").POLICY_ARMED is True

    def test_shadow_is_never_armed(self):
        assert cfg_for("shadow").POLICY_ARMED is False

    def test_armed_cannot_be_set_by_hand(self):
        """It is derived from the mode and the acknowledgement.

        An operator who writes POLICY_ARMED=1 is ignored rather than obeyed —
        the environment variable is never read, so there is no code path by
        which it could take effect.
        """
        cfg = _config.load({"POLICY_MODE": "shadow", "POLICY_ARMED": "1"})
        assert cfg.POLICY_ARMED is False


class TestShadowModeChangesNothing:
    def test_in_shadow_the_intent_is_returned_byte_for_byte_unchanged(self):
        """The load-bearing test for shadow mode.

        Not "the probability is None" — *identical object*. If the wrapper
        cannot change the intent at all, then no argument about model quality,
        calibration or drift can make shadow mode unsafe.
        """
        base = FakeBase()
        strategy = ml.PolicyStrategy(
            base, config=cfg_for("shadow"), policy=FakePolicy(),
        )
        result = strategy.signal_for("BTCUSDT")
        assert result is base._intent
        assert result.win_probability is None

    def test_the_model_IS_evaluated_in_shadow(self):
        """Shadow is not "off with extra steps" — the point is the record."""
        policy = FakePolicy()
        strategy = ml.PolicyStrategy(
            FakeBase(), config=cfg_for("shadow"), policy=policy,
        )
        strategy.signal_for("BTCUSDT")
        assert strategy.decisions_evaluated == 1

    def test_shadow_decisions_are_journaled(self):
        recorded = []

        class Store:
            def journal(self, symbol, kind, reason, detail):
                recorded.append((symbol, kind, reason, detail))

        strategy = ml.PolicyStrategy(
            FakeBase(), config=cfg_for("shadow"), policy=FakePolicy(), store=Store(),
        )
        strategy.signal_for("BTCUSDT")
        assert recorded and recorded[0][1] == "POLICY"

    def test_the_shadow_record_keeps_BOTH_views(self):
        """The question is never "was the model right" — it is "was it right
        where it disagreed". A record with only the model's side cannot say."""
        strategy = ml.PolicyStrategy(
            FakeBase(action="BUY", confidence=0.42),
            config=cfg_for("shadow"), policy=FakePolicy(),
        )
        strategy.signal_for("BTCUSDT")
        record = strategy.shadow_log[-1].as_dict()
        assert record["model_direction"] == "Buy"
        assert record["classical_action"] == "BUY"
        assert record["classical_confidence"] == pytest.approx(0.42)

    def test_the_shadow_log_is_bounded(self):
        """An unbounded list in a process that runs for weeks is a memory leak
        with a plausible excuse."""
        strategy = ml.PolicyStrategy(
            FakeBase(), config=cfg_for("shadow"), policy=FakePolicy(),
        )
        strategy._shadow_log_limit = 10
        for _ in range(50):
            strategy.signal_for("BTCUSDT")
        assert len(strategy.shadow_log) == 10

    def test_mode_off_never_even_loads_a_model(self):
        strategy = ml.PolicyStrategy(FakeBase(), config=cfg_for("off"))
        assert strategy.policy is None
        assert strategy.unavailable_reason == ml.PolicyUnavailableReason.MODE_OFF


# ---------------------------------------------------------------------------
# 3. what the model may and may not do when it IS live
# ---------------------------------------------------------------------------


class TestTheModelMayOnlyPropose:
    def _strategy(self, decision, **cfg_over):
        return ml.PolicyStrategy(
            FakeBase(), config=cfg_for("live", **cfg_over),
            policy=FakePolicy(decision),
        )

    def test_live_attaches_only_the_probability(self):
        result = self._strategy(FakeDecision(probability=0.71)).signal_for("BTCUSDT")
        assert result.win_probability == pytest.approx(0.71)
        # Everything else is the classical strategy's, untouched.
        assert result.entry_price == 50_000.0
        assert result.stop_price == 49_500.0
        assert result.take_profits == ((50_600.0, 0.5), (51_400.0, 0.5))
        assert result.signal_type == "BUY"

    def test_the_model_cannot_reverse_a_trade(self):
        """A disagreeing model gets ignored, not obeyed. Allowing a reversal
        would make the model the decision-maker rather than an input."""
        strategy = self._strategy(FakeDecision(direction="SHORT", probability=0.9))
        result = strategy.signal_for("BTCUSDT")
        assert result.signal_type == "BUY"
        assert result.win_probability is None
        assert strategy.disagreements == 1

    def test_the_model_cannot_create_a_trade_from_nothing(self):
        """No classical intent, no trade — however confident the model is."""
        base = FakeBase()
        base._intent = None
        strategy = ml.PolicyStrategy(
            base, config=cfg_for("live"),
            policy=FakePolicy(FakeDecision(probability=0.99)),
        )
        assert strategy.signal_for("BTCUSDT") is None

    def test_an_unusable_decision_leaves_the_intent_alone(self):
        result = self._strategy(
            FakeDecision(usable=False, probability=None, reason="NAN_FEATURE")
        ).signal_for("BTCUSDT")
        assert result.win_probability is None

    def test_a_broken_model_makes_the_system_MORE_cautious(self):
        """The direction that matters. With no probability the edge gate falls
        back to reward-only, which is stricter than any assumption it could
        make instead — so a model failure tightens the system."""
        result = self._strategy(
            FakeDecision(usable=False, probability=None)
        ).signal_for("BTCUSDT")
        assert result.win_probability is None

    def test_a_model_that_raises_does_not_break_the_loop(self):
        class Exploding:
            artefact_hash = "x"

            def predict_edge(self, *a, **k):
                raise RuntimeError("model is broken")

        base = FakeBase()
        strategy = ml.PolicyStrategy(
            base, config=cfg_for("live"), policy=Exploding(),
        )
        assert strategy.signal_for("BTCUSDT") is base._intent

    def test_POLICY_MIN_PROBABILITY_can_only_withhold(self):
        """The floor removes a probability; it never substitutes a higher one."""
        result = self._strategy(
            FakeDecision(probability=0.3), POLICY_MIN_PROBABILITY=0.6
        ).signal_for("BTCUSDT")
        assert result.win_probability is None

    def test_a_short_probability_is_oriented_to_the_trade(self):
        """`probability` is P(long wins). For a short it is 1-p. Getting this
        backwards makes the gate most permissive exactly when the model is most
        against the trade."""
        short_intent = _intent(signal_type="SELL", stop_price=50_500.0,
                               take_profits=((49_400.0, 0.5), (48_600.0, 0.5)))
        base = FakeBase(intent=short_intent)
        strategy = ml.PolicyStrategy(
            base, config=cfg_for("live"),
            policy=FakePolicy(FakeDecision(direction="SHORT", probability=0.2)),
        )
        result = strategy.signal_for("BTCUSDT")
        assert result.win_probability == pytest.approx(0.8)


class TestTheModelCannotTouchTheSafetyMachinery:
    def test_ml_strategy_cannot_clear_the_kill_switch(self):
        source = _code_only("ml_strategy.py")
        tree = ast.parse(open(os.path.join(REPO, "ml_strategy.py"),
                              encoding="utf-8").read())
        names = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        } | {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        }
        assert not any("clear_kill" in n for n in names)
        assert "HUMAN_CLEARED" not in source

    def test_policy_and_features_cannot_reach_the_risk_manager(self):
        """Import direction is the cheapest possible enforcement: a module that
        cannot import the gates cannot call them."""
        for module in ("policy.py", "features.py"):
            source = open(os.path.join(REPO, module), encoding="utf-8").read()
            tree = ast.parse(source)
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            for forbidden in ("risk_management", "trading_engine", "persistence",
                              "bybit_connection", "main"):
                assert forbidden not in imported, f"{module} imports {forbidden}"

    def test_ml_strategy_never_constructs_an_order(self):
        source = _code_only("ml_strategy.py")
        for forbidden in ("place_order", "place_stop_order", "place_take_profit",
                          "cancel_order", "OrderResult"):
            assert forbidden not in source, f"ml_strategy references {forbidden}"

    def test_the_model_cannot_write_a_position_size(self):
        """Checked against CODE, not prose.

        The module docstring says the words `size_multiplier` and `kill switch`
        precisely because it is explaining what it must not do. A naive grep
        would fail on the documentation of the guarantee it is checking.
        """
        for forbidden in ("size_multiplier", "calculate_position_size",
                          "upsert_position", "record_trade"):
            assert forbidden not in _code_only("ml_strategy.py"), forbidden

    def test_the_only_field_the_model_writes_is_win_probability(self):
        """Enumerated from the source, so adding a second one fails this test.

        This is the narrowest statement of the whole slice: of the seven fields
        on a `TradeIntent`, the model may write exactly one, and it is the one
        that carries no geometry.
        """
        tree = ast.parse(open(os.path.join(REPO, "ml_strategy.py"),
                              encoding="utf-8").read())
        target = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_attach_probability"
        )
        replaced = set()
        for node in ast.walk(target):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_replace":
                replaced.update(kw.arg for kw in node.keywords)
        assert replaced == {"win_probability"}, replaced

    def test_every_gate_still_runs_when_a_probability_is_attached(self, store):
        """A model output must not skip a gate. The kill switch is the sharpest
        version of that question."""
        manager = rm.BillionaireRiskManager(config=cfg_for(), store=store)
        store.trip_kill_switch("test")
        decision = manager.gate_order(
            **good_order(take_profit=51_500.0, win_probability=0.99)
        )
        assert decision.reason == "KILL_SWITCH_ENGAGED"

    def test_a_perfect_probability_cannot_beat_the_position_cap(self, store):
        manager = rm.BillionaireRiskManager(config=cfg_for(), store=store)
        decision = manager.gate_order(
            **good_order(quantity=100.0, take_profit=51_500.0, win_probability=1.0)
        )
        assert decision.ok is False
        assert "KILL" not in decision.reason


# ---------------------------------------------------------------------------
# 4. drift: automatic demotion, manual promotion
# ---------------------------------------------------------------------------


class TestDriftDemotesButNeverPromotes:
    def test_a_well_behaved_model_does_not_drift(self):
        monitor = ml.PolicyDriftMonitor(baseline_brier=0.20, min_samples=10)
        for i in range(40):
            monitor.record_outcome(0.6, won=(i % 10) < 6)
        assert monitor.has_drifted() is False

    def test_a_model_whose_predictions_stop_matching_reality_drifts(self):
        monitor = ml.PolicyDriftMonitor(baseline_brier=0.20, min_samples=10,
                                        tolerance=0.05)
        for _ in range(40):
            monitor.record_outcome(0.9, won=False)
        assert monitor.has_drifted() is True
        assert "exceeds the training baseline" in monitor.summary()

    def test_drift_stops_the_model_proposing(self):
        monitor = ml.PolicyDriftMonitor(baseline_brier=0.20, min_samples=5)
        for _ in range(20):
            monitor.record_outcome(0.95, won=False)
        strategy = ml.PolicyStrategy(
            FakeBase(), config=cfg_for("live"), policy=FakePolicy(),
            drift_monitor=monitor,
        )
        allowed, reason = strategy.may_propose()
        assert allowed is False
        assert ml.PolicyUnavailableReason.DRIFTED in reason

    def test_a_drifted_model_leaves_the_intent_unchanged(self):
        monitor = ml.PolicyDriftMonitor(baseline_brier=0.20, min_samples=5)
        for _ in range(20):
            monitor.record_outcome(0.95, won=False)
        base = FakeBase()
        strategy = ml.PolicyStrategy(
            base, config=cfg_for("live"), policy=FakePolicy(),
            drift_monitor=monitor,
        )
        assert strategy.signal_for("BTCUSDT") is base._intent

    def test_drift_never_un_fires_on_its_own(self):
        """A model that drifted and then looked fine for twenty trades has not
        been vindicated; it has produced twenty trades."""
        monitor = ml.PolicyDriftMonitor(baseline_brier=0.20, min_samples=5)
        for _ in range(20):
            monitor.record_outcome(0.95, won=False)
        assert monitor.has_drifted()
        for _ in range(500):
            monitor.record_outcome(0.5, won=True)
        assert monitor.has_drifted() is True

    def test_drift_needs_enough_samples_before_it_fires(self):
        monitor = ml.PolicyDriftMonitor(baseline_brier=0.20, min_samples=50)
        for _ in range(10):
            monitor.record_outcome(0.99, won=False)
        assert monitor.has_drifted() is False

    def test_with_no_baseline_there_is_nothing_to_drift_from(self):
        monitor = ml.PolicyDriftMonitor(baseline_brier=None, min_samples=5)
        for _ in range(50):
            monitor.record_outcome(0.99, won=False)
        assert monitor.has_drifted() is False

    def test_the_monitor_cannot_promote(self):
        """Its only authority is to make `may_propose` return False."""
        source = _code_only("ml_strategy.py")
        for forbidden in ("promote", "clear_kill", "size_multiplier",
                          "POLICY_ARMED ="):
            assert forbidden not in source, forbidden

    @pytest.mark.parametrize("bad", [1.5, -0.1, float("nan")])
    def test_an_impossible_predicted_probability_is_discarded(self, bad):
        monitor = ml.PolicyDriftMonitor(baseline_brier=0.2, min_samples=1)
        monitor.record_outcome(bad, won=True)
        assert monitor.snapshot()["samples"] == 0


# ---------------------------------------------------------------------------
# 5. loading fails closed
# ---------------------------------------------------------------------------


class TestLoadingFailsClosed:
    def test_a_missing_artefact_is_a_normal_state_not_a_crash(self):
        cfg = cfg_for("live", POLICY_PATH="/nonexistent/model")
        loaded, reason = ml.load_policy_for(cfg)
        assert loaded is None
        assert reason

    def test_mode_off_short_circuits_before_any_import(self):
        loaded, reason = ml.load_policy_for(cfg_for("off"))
        assert loaded is None
        assert reason == ml.PolicyUnavailableReason.MODE_OFF

    def test_load_never_raises_whatever_is_wrong(self, tmp_path):
        garbage = tmp_path / "model"
        garbage.mkdir()
        (garbage / "MANIFEST.json").write_text("{not json")
        loaded, reason = ml.load_policy_for(
            cfg_for("live", POLICY_PATH=str(garbage))
        )
        assert loaded is None and reason

    def test_promotion_is_required_at_LOAD_time(self):
        """There is no scenario in which the right answer is "load an
        unpromoted model and remember not to use it"."""
        source = inspect.getsource(ml.load_policy_for)
        assert "require_promotion=True" in source

    def test_a_bot_with_no_model_still_starts_and_is_healthy(self):
        import main

        bot = main.build_bot()
        try:
            health = bot.health()
            assert health["policy"]["mode"] == "off"
            assert health["policy"]["model_loaded"] is False
        finally:
            bot.shutdown()

    def test_health_reports_the_policy_even_when_there_is_none(self):
        """"Off", "loaded but refusing", and "drifted" are three different
        situations. An absent key would make them look the same."""
        import main

        bot = main.build_bot()
        try:
            assert "policy" in bot.health()
        finally:
            bot.shutdown()


# ---------------------------------------------------------------------------
# 6. the snapshot an operator reads
# ---------------------------------------------------------------------------


class TestTheOperatorCanSeeWhatIsHappening:
    def test_the_snapshot_names_why_the_model_is_not_proposing(self):
        strategy = ml.PolicyStrategy(
            FakeBase(), config=cfg_for("shadow"), policy=FakePolicy(),
        )
        snapshot = strategy.snapshot()
        assert snapshot["may_propose"] is False
        assert "shadow" in snapshot["reason"]
        assert snapshot["model_loaded"] is True

    def test_the_snapshot_counts_disagreements(self):
        strategy = ml.PolicyStrategy(
            FakeBase(), config=cfg_for("live"),
            policy=FakePolicy(FakeDecision(direction="SHORT")),
        )
        strategy.signal_for("BTCUSDT")
        assert strategy.snapshot()["disagreements"] == 1

    def test_the_snapshot_carries_no_credentials(self):
        import json

        strategy = ml.PolicyStrategy(
            FakeBase(), config=cfg_for("live"), policy=FakePolicy(),
        )
        dumped = json.dumps(strategy.snapshot())
        for forbidden in ("api", "secret", "key", "token"):
            assert forbidden not in dumped.lower()

    def test_the_wrapper_is_a_drop_in_for_the_orchestrator(self):
        """`TradingBot` must not need a second code path for "the ML case" —
        two paths is how one of them stops being tested."""
        strategy = ml.PolicyStrategy(FakeBase(), config=cfg_for("off"))
        assert callable(strategy.signal_for)
        assert hasattr(strategy, "last_signal")
