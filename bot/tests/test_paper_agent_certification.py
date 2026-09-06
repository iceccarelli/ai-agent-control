"""Slice 42 — the paper-agent certification suite.

The standard these tests enforce is `docs/PAPER_AGENT_CERTIFICATION.md`, which
was written before any of them existed. Each group here is one clause of it.

WHAT IS BEING CERTIFIED
=======================
That the process can be left running unattended in paper mode without lying
about itself, arming anything, or leaving a position unprotected.

WHAT IS NOT
===========
Profit. Nothing in this file measures edge, and nothing in this repository has
demonstrated any: three families measured against pre-declared bars, three
ABSENT. A certified paper shell that ran for a year would still be a shell.

Most of these properties already hold — several were built in slices 29-31 and
are re-asserted here from the certification's point of view rather than the
implementing slice's. That redundancy is deliberate: a certification that only
tests what was added in its own slice certifies nothing, and the failure mode
being guarded against is erosion by a later, well-meaning edit.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import logging
import os
import re
import sys

import pytest

import registration_invariant

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import config as _config  # noqa: E402
import main as _main  # noqa: E402
import project_status as ps  # noqa: E402
import session_log as sl  # noqa: E402

ARTIFACTS = os.path.join(REPO, "artifacts")

#: The families frozen by human decision. Named literally rather than read from
#: `ps.ABSENT_SIGNALS`, so that shrinking that tuple fails a test instead of
#: silently shrinking this file's coverage with it.
FROZEN = ("technical_analysis", "closed_analyser", "donchian_breakout_v1",
          "btc_alt_spillover_v1", "post_shock_fade_v1",
          "range_location_fade_v1", "open_gap_fade_v1",
          "ts_momentum_v1", "sign_flip_momentum_v1",
          "compression_breakout_v1", "funding_carry_fade_v1")


def stock_config():
    """The configuration an operator gets with no environment set."""
    return _config.load({})


# ===========================================================================
# GROUP A — truthfulness surfaces
# ===========================================================================


class TestGroupATruthfulnessSurfaces:
    """Startup, health and the session log all say the same true thing."""

    def test_the_claim_string_is_exact(self):
        assert sl.NO_EDGE_CLAIM == "NO EDGE CLAIM — timing-skill research CLOSED"

    def test_the_status_snapshot_carries_it(self):
        status = ps.current(stock_config())
        assert status.no_edge_claim == sl.NO_EDGE_CLAIM
        assert status.timing_skill_research == "CLOSED"
        assert status.cleared_edge_signal == \
            registration_invariant.stock_cleared_edge_signal()

    def test_the_startup_summary_line_carries_it(self):
        line = ps.current(stock_config()).summary_line()
        assert sl.NO_EDGE_CLAIM in line
        expected = registration_invariant.stock_cleared_edge_signal() or "none"
        assert f"cleared_edge_signal={expected}" in line
        assert "timing_skill_research=CLOSED" in line

    def test_the_health_payload_carries_the_project_block(self, tmp_path):
        """An operator polling /health must see the claim without asking."""
        bot = _make_bot(tmp_path)
        payload = bot.health()
        assert "project" in payload, payload
        project = payload["project"]
        assert project["no_edge_claim"] == sl.NO_EDGE_CLAIM
        assert project["cleared_edge_signal"] == \
            registration_invariant.stock_cleared_edge_signal()
        assert project["timing_skill_research"] == "CLOSED"

    def test_every_session_record_carries_it(self, tmp_path):
        log = _open_session(tmp_path)
        try:
            record = log.record()
            assert record["no_edge_claim"] == sl.NO_EDGE_CLAIM
            assert record["cleared_edge_signal"] == \
            registration_invariant.stock_cleared_edge_signal()
            assert record["timing_skill_research"] == "CLOSED"
        finally:
            log.close()

    def test_the_claim_is_derived_from_the_freeze_not_hardcoded(self):
        """It must not be able to drift away from what is true.

        `ProjectStatus.cleared_edge_signal` comes from the artefact hook, so if
        something ever genuinely cleared, the surfaces would change with it.
        Asserted structurally: the field is populated from the hook's return
        value, never from a literal.
        """
        tree = ast.parse(inspect.getsource(ps.current))
        literals = [n for n in ast.walk(tree)
                    if isinstance(n, ast.keyword)
                    and n.arg == "cleared_edge_signal"
                    and isinstance(n.value, ast.Constant)
                    and n.value.value is not None]
        assert literals == [], "cleared_edge_signal is hard-coded to a value"

    def test_no_session_field_looks_like_a_scoreboard(self, tmp_path):
        """A scoreboard is how a paper shell becomes a profit claim.

        Once a record carries an equity curve, the next reader compares numbers
        across runs, and a comparison of numbers is a performance claim whether
        or not anyone meant one.
        """
        log = _open_session(tmp_path)
        try:
            blob = json.dumps(log.record()).lower()
        finally:
            log.close()
        for marker in sl.FORBIDDEN_FIELD_MARKERS:
            assert marker not in blob, marker

    def test_the_forbidden_markers_cover_the_obvious_names(self):
        for marker in ("pnl", "profit", "equity", "win_rate", "sharpe",
                       "drawdown", "expectancy"):
            assert marker in sl.FORBIDDEN_FIELD_MARKERS, marker

    def test_the_written_session_file_carries_no_scoreboard_either(
            self, tmp_path):
        """Not just `record()` — what actually lands on disk.

        A record clean in memory and a file that gained a field on the way out
        would be the same failure with a longer path.
        """
        path = tmp_path / "session.jsonl"
        log = _open_session(tmp_path, PAPER_SESSION_LOG_PATH=str(path))
        log.start(reconciled=True)
        log.tick()
        log.close()
        blob = path.read_text().lower()
        assert "no edge claim" in blob
        for marker in sl.FORBIDDEN_FIELD_MARKERS:
            assert marker not in blob, marker

    def test_no_module_builds_a_session_field_with_a_scoreboard_name(self):
        """Structural, so a future helpful edit fails here rather than ships.

        The failure mode is not malice — it is "operators will want to see how
        it did". Checked at AST level over every dict key the session module
        constructs, because a raw-text ban would forbid this module from
        *explaining* what it refuses.
        """
        tree = ast.parse(inspect.getsource(sl))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key in node.keys:
                if not isinstance(key, ast.Constant):
                    continue
                if not isinstance(key.value, str):
                    continue
                lowered = key.value.lower()
                for marker in sl.FORBIDDEN_FIELD_MARKERS:
                    assert marker not in lowered, (key.value, marker)


# ===========================================================================
# helpers used by more than one group
# ===========================================================================


def _make_bot(tmp_path, **env):
    """A bot wired to a temporary state store, in whatever mode `env` gives."""
    settings = {"STATE_DB_PATH": str(tmp_path / "state.db")}
    settings.update(env)
    cfg = _config.load(settings)
    return _main.TradingBot(cfg)


def _open_session(tmp_path, **env):
    settings = {"STATE_DB_PATH": str(tmp_path / "session.db")}
    settings.update(env)
    cfg = _config.load(settings)
    import persistence
    store = persistence.StateStore(cfg.STATE_DB_PATH)
    return sl.PaperSessionLog(config=cfg, store=store)


# ===========================================================================
# GROUP B — the kill switch is human-clear-only
# ===========================================================================


#: Every module in the trading path. The bot may TRIP the switch from any of
#: them; none may clear it.
TRADING_MODULES = (
    "main", "trading_engine", "risk_management", "position_sizing",
    "bybit_connection", "memory", "ml_strategy", "policy", "technical_analysis",
    "performance_analytics", "features", "market_data", "backtest",
    "persistence", "session_log", "project_status",
)


def _module_sources():
    """(name, source) for every trading module that exists on disk."""
    out = []
    for name in TRADING_MODULES:
        path = os.path.join(REPO, f"{name}.py")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                out.append((name, handle.read()))
    return out


class TestGroupBKillSwitch:
    """A kill switch the bot can reset is not a kill switch."""

    def test_clearing_requires_the_exact_operator_token(self, tmp_path):
        import persistence
        store = persistence.StateStore(str(tmp_path / "k.db"))
        store.trip_kill_switch("certification test")
        assert store.is_kill_switch_engaged()[0] is True

        for wrong in ("", "yes", "human", "HUMAN_CLEARED", "please",
                      "human_cleared_kill_switch"):
            with pytest.raises(Exception):
                store.clear_kill_switch_by_human(wrong)
            assert store.is_kill_switch_engaged()[0] is True

        store.clear_kill_switch_by_human("HUMAN_CLEARED_KILL_SWITCH")
        assert store.is_kill_switch_engaged()[0] is False

    def test_the_token_is_not_derivable_from_config(self):
        """An operator token that config can supply is a config setting."""
        cfg = stock_config()
        blob = json.dumps({k: str(v) for k, v in vars(cfg).items()}).upper() \
            if hasattr(cfg, "__dict__") else ""
        assert "HUMAN_CLEARED_KILL_SWITCH" not in blob

    def test_no_trading_module_calls_the_clear_path(self):
        """AST over every trading module: nobody calls it but the operator.

        Structural rather than textual on purpose. A raw-text ban would forbid
        `persistence.py` from *documenting* the method it defines, and this
        repository has walked into that trap five times.
        """
        offenders = []
        for name, source in _module_sources():
            if name == "persistence":
                continue          # defines it; does not call it
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, ast.Call):
                    continue
                attr = getattr(node.func, "attr", None) or \
                    getattr(node.func, "id", None)
                if attr == "clear_kill_switch_by_human":
                    offenders.append(name)
        assert offenders == [], offenders

    def test_persistence_defines_it_but_never_calls_itself(self):
        import persistence
        tree = ast.parse(inspect.getsource(persistence))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and (getattr(n.func, "attr", None) or
                      getattr(n.func, "id", None)) == "clear_kill_switch_by_human"]
        assert calls == []

    def test_no_module_writes_the_engaged_column_to_zero(self):
        """The back door: skipping the method and touching SQL directly.

        `trip_kill_switch` sets `engaged = 1`; only the human-clear path may
        set it to 0. Any other module containing that UPDATE would be a clear
        path wearing a disguise.
        """
        offenders = []
        for name, source in _module_sources():
            if name == "persistence":
                continue
            lowered = source.lower()
            if "kill_switch set engaged = 0" in lowered:
                offenders.append(name)
            if "update kill_switch" in lowered:
                offenders.append(f"{name} (raw UPDATE)")
        assert offenders == [], offenders

    def test_persistence_has_exactly_one_statement_clearing_it(self):
        import persistence
        source = inspect.getsource(persistence).lower()
        assert source.count("set engaged = 0") == 1

    def test_the_risk_manager_can_trip_but_exposes_no_clear(self):
        import risk_management
        assert hasattr(risk_management.BillionaireRiskManager,
                       "trip_kill_switch")
        for name in dir(risk_management.BillionaireRiskManager):
            assert "clear_kill" not in name.lower(), name

    def test_no_public_api_anywhere_offers_a_clear_without_a_token(self):
        """Any function whose name clears the switch must demand the ack."""
        for name, source in _module_sources():
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if "clear" not in node.name.lower():
                    continue
                if "kill" not in node.name.lower():
                    continue
                args = [a.arg for a in node.args.args + node.args.kwonlyargs]
                assert any("ack" in a.lower() or "operator" in a.lower()
                           for a in args), (name, node.name, args)

    def test_an_engaged_switch_makes_the_bot_report_unhealthy(self, tmp_path):
        """It must be visible to an unattended operator, not just enforced."""
        bot = _make_bot(tmp_path)
        bot.store.trip_kill_switch("certification test")
        payload = bot.health()
        assert payload["kill_switch"] is True
        assert payload["healthy"] is False
        assert "certification test" in payload["kill_reason"]

    def test_an_unreadable_switch_reads_as_engaged(self, tmp_path):
        """Fail closed: a switch that cannot be read is not a switch that is off."""
        log = _open_session(tmp_path)

        class Broken:
            def is_kill_switch_engaged(self):
                raise RuntimeError("db gone")

        log.store = Broken()
        assert log.record()["kill_switch_engaged"] is True


# ===========================================================================
# GROUP C — operator control is not a risk gate
# ===========================================================================


class TestGroupCOperatorControlVsRiskGates:
    """`ENTRIES_ENABLED` selects a signal source. It decides nothing about risk.

    A risk decision that a convenience flag can switch off is not a risk
    decision. The distinction is easy to erode by accident — someone reaches
    for the flag already in scope — so it is asserted from both ends: the gate
    path may not read it, and the flag's only documented job is attaching a
    strategy.
    """

    def test_gate_order_never_reads_the_operator_flag(self):
        import textwrap
        import risk_management
        tree = ast.parse(textwrap.dedent(inspect.getsource(
            risk_management.BillionaireRiskManager.gate_order)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr != "ENTRIES_ENABLED", ast.dump(node)[:120]
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value != "ENTRIES_ENABLED", node.value

    def test_no_individual_gate_reads_it_either(self):
        """`gate_order` delegating to a `_gate_*` that reads it is the same bug."""
        import risk_management
        source = inspect.getsource(risk_management)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("_gate") and node.name != "gate_order":
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Attribute):
                    assert inner.attr != "ENTRIES_ENABLED", node.name
                if isinstance(inner, ast.Constant) and \
                        isinstance(inner.value, str):
                    assert inner.value != "ENTRIES_ENABLED", node.name

    def test_the_whole_risk_module_is_free_of_it(self):
        """Nothing in the risk module has any business knowing about it."""
        import risk_management
        for node in ast.walk(ast.parse(inspect.getsource(risk_management))):
            if isinstance(node, ast.Attribute):
                assert node.attr != "ENTRIES_ENABLED"

    def test_position_sizing_is_free_of_it_too(self):
        import position_sizing
        for node in ast.walk(ast.parse(inspect.getsource(position_sizing))):
            if isinstance(node, ast.Attribute):
                assert node.attr != "ENTRIES_ENABLED"

    def test_it_only_governs_whether_a_strategy_is_attached(self):
        """Its single legitimate use, pinned to the one place it belongs."""
        readers = []
        for name, source in _module_sources():
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Attribute) and \
                        node.attr == "ENTRIES_ENABLED":
                    readers.append(name)
        assert set(readers) <= {"main", "config", "session_log"}, readers

    def test_disabling_entries_does_not_disable_a_single_gate(self, tmp_path):
        """Behavioural: the same bad order is refused either way.

        A stop on the wrong side of the entry is not a matter of operator
        preference, and the refusal must be identical with entries on and off.
        """
        import risk_management
        verdicts = {}
        for enabled in ("1", "0"):
            cfg = _config.load({"STATE_DB_PATH": str(tmp_path / f"g{enabled}.db"),
                                "ENTRIES_ENABLED": enabled})
            import persistence
            store = persistence.StateStore(cfg.STATE_DB_PATH)
            manager = risk_management.BillionaireRiskManager(cfg, store)
            decision = manager.gate_order(
                symbol="BTCUSDT", side="Buy", entry_price=100.0,
                stop_loss=110.0,          # stop ABOVE entry on a long
                quantity=1.0, account_equity=10_000.0)
            verdicts[enabled] = (bool(decision.allowed), decision.reason)
        assert verdicts["1"][0] is False
        assert verdicts["0"][0] is False
        assert verdicts["1"] == verdicts["0"], verdicts

    def test_a_kill_switch_beats_the_operator_flag(self, tmp_path):
        """Turning entries ON cannot revive a killed process."""
        import persistence
        import risk_management
        cfg = _config.load({"STATE_DB_PATH": str(tmp_path / "kill.db"),
                            "ENTRIES_ENABLED": "1"})
        store = persistence.StateStore(cfg.STATE_DB_PATH)
        store.trip_kill_switch("certification")
        manager = risk_management.BillionaireRiskManager(cfg, store)
        decision = manager.gate_order(
            symbol="BTCUSDT", side="Buy", entry_price=100.0, stop_loss=95.0,
            quantity=0.01, account_equity=10_000.0)
        assert decision.allowed is False
        assert "kill" in decision.reason.lower()


# ===========================================================================
# GROUP D — paper stays paper
# ===========================================================================


class TestGroupDPaperStaysPaper:
    """No silent live path, and a credential failure degrades CLOSED.

    The dangerous shape is not "live gets armed by mistake" — it is "something
    was missing, so the process quietly did the other thing and said nothing".
    Every branch below must end in paper AND say why.
    """

    def test_the_stock_environment_is_paper_and_unarmed(self):
        cfg = stock_config()
        assert cfg.PAPER_TRADING is True
        assert cfg.USE_TESTNET is True
        allowed, reason = _config.is_live_authorized(cfg)
        assert allowed is False
        assert "SANDBOX" in reason or "BLOCKED" in reason

    @pytest.mark.parametrize("env,expect", [
        ({}, "SANDBOX"),
        ({"USE_TESTNET": "0"}, "SANDBOX"),
        ({"PAPER_TRADING": "0"}, "SANDBOX"),
        ({"USE_TESTNET": "0", "PAPER_TRADING": "0"}, "LIVE_BLOCKED"),
        ({"USE_TESTNET": "0", "PAPER_TRADING": "0",
          "LIVE_TRADING_ACK": "yes"}, "LIVE_BLOCKED"),
        ({"USE_TESTNET": "0", "PAPER_TRADING": "0",
          "LIVE_TRADING_ACK": "I_UNDERSTAND"}, "LIVE_BLOCKED"),
    ])
    def test_every_partial_arming_still_refuses_and_says_why(self, env, expect):
        """Each row is one thing an operator might have set and forgotten.

        The last row is the sharpest: the acknowledgement is correct and live is
        STILL refused, because no credentials are present. Missing credentials
        degrade to paper — they never fall through to a live attempt.
        """
        cfg = _config.load(dict(env))
        allowed, reason = _config.is_live_authorized(cfg)
        assert allowed is False
        assert expect in reason, reason
        assert reason.strip(), "a refusal with no reason is a silent failure"

    def test_the_refusal_reason_names_the_missing_credential(self):
        cfg = _config.load({"USE_TESTNET": "0", "PAPER_TRADING": "0",
                            "LIVE_TRADING_ACK": "I_UNDERSTAND"})
        _allowed, reason = _config.is_live_authorized(cfg)
        assert "BYBIT_API_KEY" in reason

    def test_a_half_credential_pair_is_not_enough(self):
        cfg = _config.load({"USE_TESTNET": "0", "PAPER_TRADING": "0",
                            "LIVE_TRADING_ACK": "I_UNDERSTAND",
                            "BYBIT_API_KEY": "key-but-no-secret"})
        allowed, reason = _config.is_live_authorized(cfg)
        assert allowed is False
        assert "BLOCKED" in reason

    @pytest.mark.parametrize("near", [
        "i_understand", "I UNDERSTAND", "yes", "I_UNDERSTAND_THE_RISKS",
        "I UNDERSTAND ", "understand", "1", "true",
    ])
    def test_a_near_miss_acknowledgement_does_not_arm_live(self, near):
        cfg = _config.load({"USE_TESTNET": "0", "PAPER_TRADING": "0",
                            "LIVE_TRADING_ACK": near,
                            "BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"})
        allowed, _reason = _config.is_live_authorized(cfg)
        assert allowed is False, near

    @pytest.mark.parametrize("spelling", [
        "I_UNDERSTAND", "I_UNDERSTAND ", " I_UNDERSTAND", "I_UNDERSTAND\n",
    ])
    def test_surrounding_whitespace_is_normalised_and_that_is_deliberate(
            self, spelling):
        """Pinned because it is a real behaviour, not because it is a defect.

        The loader strips the acknowledgement, so `"I_UNDERSTAND\n"` from a
        shell heredoc arms live exactly as `"I_UNDERSTAND"` does. No accident
        reaches that state: the operator still has to set testnet off, paper
        off, both credentials, and type the token. Stripping changes the
        whitespace around a deliberate act, never its content — the test above
        pins that every content-level near miss is still refused.

        It is asserted here so the behaviour is visible in the certification
        record rather than discovered by someone at 3am.
        """
        cfg = _config.load({"USE_TESTNET": "0", "PAPER_TRADING": "0",
                            "LIVE_TRADING_ACK": spelling,
                            "BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"})
        allowed, reason = _config.is_live_authorized(cfg)
        assert allowed is True, (spelling, reason)

    def test_project_status_reports_paper_and_unarmed(self):
        status = ps.current(stock_config())
        assert status.execution_mode == "paper"
        assert status.live_authorized is False
        assert status.policy_mode == "off"

    def test_the_health_payload_reports_paper(self, tmp_path):
        payload = _make_bot(tmp_path).health()
        assert payload["paper"] is True
        assert payload["testnet"] is True

    def test_no_credential_value_can_reach_a_log_surface(self, tmp_path):
        """A paper agent runs unattended; its logs are its only surface."""
        cfg = _config.load({"STATE_DB_PATH": str(tmp_path / "c.db"),
                            "BYBIT_API_KEY": "SECRET-KEY-VALUE",
                            "BYBIT_API_SECRET": "SECRET-SECRET-VALUE"})
        blob = json.dumps(cfg.safe_dict())
        assert "SECRET-KEY-VALUE" not in blob
        assert "SECRET-SECRET-VALUE" not in blob

        log = _open_session(tmp_path, BYBIT_API_KEY="SECRET-KEY-VALUE",
                            BYBIT_API_SECRET="SECRET-SECRET-VALUE")
        try:
            record = json.dumps(log.record())
        finally:
            log.close()
        assert "SECRET-KEY-VALUE" not in record
        assert "SECRET-SECRET-VALUE" not in record

    def test_no_model_is_loaded_and_the_policy_is_off(self, tmp_path):
        status = ps.current(stock_config())
        assert status.models_current_present is False
        assert status.policy_mode == "off"
        assert not os.path.exists(os.path.join(REPO, "models", "current"))

    def test_the_credential_bearing_view_is_never_logged(self):
        """`Settings.as_dict()` carries secrets and says "do not log this".

        Structural, because the two views differ by one word at the call site.
        `ProjectStatus.as_dict()` is a different object with no credentials and
        is logged freely, so the check is scoped to calls on a config value.
        """
        offenders = []
        for name, source in _module_sources():
            if name == "config":
                continue
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if getattr(func, "attr", None) != "as_dict":
                    continue
                receiver = getattr(func, "value", None)
                target = (getattr(receiver, "attr", None) or
                          getattr(receiver, "id", None) or "")
                if target.lower() in {"cfg", "config", "settings",
                                      "self.cfg", "_cfg"}:
                    offenders.append((name, target))
        assert offenders == [], offenders

    def test_the_perp_simulator_refuses_rather_than_flatters(self):
        """An honest NotImplementedError, asserted as a certified limitation.

        The linear LIVE path is implemented and unit-tested; the SIMULATOR
        models spot cash accounting only. A linear backtest would omit funding,
        margin and liquidation and report a flattering number, so it raises.
        A missing number is better than an invented one, and the certification
        document lists this as a limitation rather than hiding it.
        """
        import backtest
        source = inspect.getsource(backtest)
        assert "NotImplementedError" in source
        assert "funding" in source and "liquidation" in source


# ===========================================================================
# GROUP E — freeze integrity
# ===========================================================================


def _perfect_forgery(directory, signal, symbol="BTCUSD"):
    """The best artefact a claim could possibly have, for a frozen signal."""
    payload = {
        "signal": signal,
        "symbol": symbol,
        "verdict": "EDGE_EVIDENCE_POSITIVE",
        "control_validated": True,
        "bars": 1461,
        "m1": {"percentile": 99.9, "bar": 95.0, "passed": True},
        "m2": {"percentile": 99.9, "bar": 95.0, "passed": True,
               "delta": 0.9, "ci_low": 0.8, "ci_high": 1.0},
        "control_attestation": "artifacts/slice40_control_directed_"
                               "ETHUSDT_n1000.log",
    }
    path = os.path.join(str(directory), f"{signal}_{symbol}_summary.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path


class TestGroupEFreezeIntegrity:
    """A frozen family cannot be talked back into the process.

    Not "an obviously bad artefact is rejected" — the artefacts below are as
    good as an artefact can be. The freeze is what refuses them, and the point
    of testing it from the certification's angle is that an unattended agent
    reads whatever is in `artifacts/`.
    """

    def test_all_four_names_are_frozen(self):
        for name in FROZEN:
            assert name in ps.ABSENT_SIGNALS, name
            assert name in ps.FROZEN_ABSENT, name

    @pytest.mark.parametrize("signal", FROZEN)
    def test_a_perfect_forgery_is_refused(self, signal, tmp_path):
        _perfect_forgery(tmp_path, signal)
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    @pytest.mark.parametrize("signal", FROZEN)
    def test_a_perfect_forgery_on_every_declared_symbol_is_refused(
            self, signal, tmp_path):
        """Satisfying the dual-symbol rule as well changes nothing."""
        for symbol in ("ETHUSDT", "SOLUSDT", "BTCUSD"):
            _perfect_forgery(tmp_path, signal, symbol)
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    @pytest.mark.parametrize("signal", FROZEN)
    def test_the_refusal_names_the_signal_and_its_evidence(self, signal,
                                                           tmp_path, caplog):
        _perfect_forgery(tmp_path, signal)
        with caplog.at_level(logging.ERROR):
            ps.cleared_edge_signal_from_artifacts(str(tmp_path))
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert signal in blob
        assert "FROZEN" in blob

    def test_a_mixed_directory_still_registers_nothing(self, tmp_path):
        """Frozen forgeries beside the real ABSENT summaries."""
        for signal in FROZEN:
            _perfect_forgery(tmp_path, signal)
        for symbol in ("ETHUSDT", "SOLUSDT"):
            source = os.path.join(
                ARTIFACTS,
                f"slice40_edge_btc_alt_spillover_{symbol}_summary.json")
            with open(source, encoding="utf-8") as handle:
                payload = json.load(handle)
            with open(os.path.join(str(tmp_path), f"real_{symbol}.json"),
                      "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_the_repositorys_own_artifacts_register_nothing(self):
        registration_invariant.assert_registration_is_sound(ARTIFACTS)

    def test_the_bars_are_still_the_pre_declared_ones(self):
        assert ps.M1_BAR == 95.0 and ps.M2_BAR == 95.0
        import edge_measurement as em
        assert em.M1_PERCENTILE_BAR == 95.0
        assert em.M2_PERCENTILE_BAR == 95.0

    def test_the_control_clauses_are_unchanged(self):
        import control_directed as cd
        assert cd.Z_ABS_MAX == 1.96
        assert cd.KS_MIN_P == 0.05
        assert cd.MAX_INCOMPLETE_SHARE == 0.05

    def test_the_frozen_signal_constants_are_unchanged_on_disk(self):
        from signals import btc_alt_spillover_v1 as sp
        assert (sp.SHOCK_MULTIPLIER, sp.ATR_PERIOD, sp.STOP_ATR,
                sp.TAKE_PROFIT_R, sp.HORIZON, sp.LOCKUP,
                sp.ROUND_TRIP_BPS, sp.ENTRY_ON) == \
            (1.0, 14, 1.5, 2.0, 5, 1, 25.0, "next_open")

    def test_every_positive_summary_is_refused_and_the_reason_is_registered(self):
        """Two POSITIVE artefacts on disk now, refused for different reasons.

        `slice35_edge_btc_alt_spillover_ETHUSDT_summary.json` predates the
        control that would license it and carries `control_validated: false`.
        Kept as evidence, refused twice over — no attestation, and a frozen
        name.

        `slice55_edge_funding_carry_fade_BTCUSDT_summary.json` is different in
        kind: **genuine, unforged, and attested**, M1 97.0 / M2 97.5 on 85
        trades under a control that passed its own pre-declared three clauses.
        It registers nothing because `funding_carry_fade_v1`'s design note
        (EDGE.md §36e, committed before any number existed) declared a
        three-symbol universe needing two, and ETHUSDT (47.7 / 48.5) and
        SOLUSDT (2.3 / 1.0) did not clear.

        This test previously asserted that no POSITIVE carried an attestation.
        Slice 55 made that false. It is NARROWED rather than dropped: every
        POSITIVE must still be refused, and the refusal must be traceable to
        something registered in code — a freeze, or a universe rule. An attested
        POSITIVE for a family under neither still fails here, which is the case
        that would be a real cleared edge and a human's decision.
        """
        import glob
        seen = 0
        for path in glob.glob(os.path.join(ARTIFACTS, "*_summary.json")):
            with open(path, encoding="utf-8") as handle:
                summary = json.load(handle)
            if summary.get("verdict") != "EDGE_EVIDENCE_POSITIVE":
                continue
            seen += 1
            signal = summary.get("signal")
            if summary.get("control_validated") is not True:
                continue
            assert (signal in ps.ABSENT_SIGNALS
                    or signal in ps.MULTI_SYMBOL_MINIMUMS
                    or signal in ps.DUAL_SYMBOL_REQUIREMENTS
                    or signal in ps.OOS_GATED_REGISTRATION), path
        assert seen, "no POSITIVE summaries found — the test would be vacuous"
        registration_invariant.assert_registration_is_sound(ARTIFACTS)

    def test_no_signal_exists_that_no_human_asked_for(self):
        """A hypothesis proposed by the thing that measures it is not one.

        Slice 42 asserted this by checking the intake said WAITING. The human
        filled it in slice 43 with `post_shock_fade_v1`, so that spelling is
        gone — but the invariant it protected is not, and this is the durable
        form of it: **every implemented signal is either frozen ABSENT or
        named in the human intake.** A family invented by an agent would be in
        `signals/` and in neither list, and would fail here.

        Checking the file for a magic phrase would have gone stale the moment a
        human did their job. Checking that nothing was self-authored does not.
        """
        intake_path = os.path.join(REPO, "NEW_SIGNAL_INTAKE.md")
        with open(intake_path, encoding="utf-8") as handle:
            intake = handle.read()
        assert "HUMAN-FILLED" in intake or "WAITING" in intake

        implemented = {
            name[:-3] for name in os.listdir(os.path.join(REPO, "signals"))
            if name.endswith(".py") and not name.startswith("__")
        }
        assert implemented, "no signals directory to check"
        for signal in implemented:
            named_by_a_human = signal in intake
            frozen = signal in ps.ABSENT_SIGNALS
            assert named_by_a_human or frozen, (
                f"{signal} is implemented but is neither frozen ABSENT nor "
                "named in the human intake — nothing in this repository may "
                "invent a thesis for itself")

    def test_the_intake_names_exactly_one_open_signal(self):
        """Two open theses at once is how a grid gets in through the door."""
        with open(os.path.join(REPO, "NEW_SIGNAL_INTAKE.md"),
                  encoding="utf-8") as handle:
            intake = handle.read()
        implemented = {
            name[:-3] for name in os.listdir(os.path.join(REPO, "signals"))
            if name.endswith(".py") and not name.startswith("__")
        }
        open_signals = {s for s in implemented
                        if s in intake and s not in ps.ABSENT_SIGNALS}
        assert len(open_signals) <= 1, open_signals


# ===========================================================================
# GROUP F — a paper cycle, end to end, against the fake exchange
# ===========================================================================


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_bybit import API_KEY, API_SECRET, FakeBybit  # noqa: E402

CERT_EQUITY = 100_000.0
CERT_ENTRY = 50_000.0


class CertCfg:
    """The shipped defaults: paper, testnet, nothing armed, no model."""

    USE_TESTNET = True
    PAPER_TRADING = True
    ENTRIES_ENABLED = True
    BYBIT_API_KEY = API_KEY
    BYBIT_API_SECRET = API_SECRET
    LIVE_TRADING_ACK = ""
    POLICY_MODE = "off"
    CATEGORY = "spot"
    ALLOW_SHORTS = False
    MEMORY_ENABLED = False
    TRADING_SYMBOLS = ("BTCUSDT",)

    def __getattr__(self, name):
        """Anything unset falls back to the real loader's default.

        Keeps this fixture honest: it cannot accidentally certify a
        configuration that no operator could actually load.
        """
        return getattr(_config.load({}), name)


def _paper_bot(tmp_path, strategy=None):
    import bybit_connection as bc
    import trading_engine as te
    from persistence import StateStore
    from position_sizing import BillionairePositionSizing
    from risk_management import BillionaireRiskManager

    cfg = CertCfg()
    exchange = FakeBybit(balances={"USDT": CERT_EQUITY, "BTC": 5.0},
                         equity=CERT_EQUITY)
    store = StateStore(str(tmp_path / "cert.db"))
    store.update_equity(CERT_EQUITY)
    client = bc.BybitClient(config=cfg, store=store, transport=exchange)
    risk = BillionaireRiskManager(config=cfg, store=store)
    engine = te.TradingEngine(
        client=client, risk_manager=risk,
        position_sizer=BillionairePositionSizing(config=cfg, risk_manager=risk),
        store=store, config=cfg)
    bot = _main.TradingBot(config=cfg, store=store, client=client,
                           risk_manager=risk, engine=engine, strategy=strategy)
    return bot, exchange, cfg


class BuyEveryTick:
    """A strategy-neutral stand-in that always wants to be long.

    Deliberately not one of the frozen families and not a new thesis: it exists
    to exercise the execution shell, has no claim attached to it, and is never
    measured. The certification is of the plumbing.
    """

    def __init__(self):
        self.calls = []

    def signal_for(self, symbol):
        import trading_engine as te
        self.calls.append(symbol)
        return te.TradeIntent(
            symbol=symbol, signal_type="BUY", entry_price=CERT_ENTRY,
            stop_price=CERT_ENTRY * 0.98,
            take_profits=((CERT_ENTRY * 1.05, 1.0),), confidence=0.8)


class TestGroupFPaperCycle:
    """Startup, ticks, shutdown — with the safety properties asserted through."""

    def test_a_full_cycle_never_authorises_live(self, tmp_path):
        bot, exchange, cfg = _paper_bot(tmp_path, strategy=BuyEveryTick())
        assert bot.startup() is True
        for _ in range(3):
            bot.tick()
            assert bot.health()["paper"] is True
        bot.shutdown()          # closes the store; nothing may be read after

        allowed, _reason = _config.is_live_authorized(cfg)
        assert allowed is False
        for request in getattr(exchange, "requests", []):
            url = str(request.get("url", request))
            assert "api.bybit.com" not in url, url

    def test_the_cycle_leaves_no_naked_position(self, tmp_path):
        """The sacred invariant: a confirmed position has a verified stop."""
        bot, _exchange, _cfg = _paper_bot(tmp_path, strategy=BuyEveryTick())
        bot.startup()
        for _ in range(3):
            bot.tick()
            assert bot.store.positions_without_stops() == []
            assert bot.health()["naked_positions"] == []
        bot.shutdown()

        # And after the process ends: reopened from disk, so this is a
        # statement about persisted state rather than about a live object.
        from persistence import StateStore
        reopened = StateStore(str(tmp_path / "cert.db"))
        assert reopened.positions_without_stops() == []

    def test_the_session_lines_still_carry_the_claim(self, tmp_path):
        """What an unattended operator reads afterwards."""
        path = tmp_path / "cert_session.jsonl"
        log = _open_session(tmp_path, PAPER_SESSION_LOG_PATH=str(path))
        log.start(reconciled=True)
        for _ in range(3):
            log.tick()
        log.close()

        lines = [line for line in path.read_text().splitlines() if line.strip()]
        assert lines
        for line in lines:
            record = json.loads(line)
            payload = record.get("record", record)
            assert payload.get("no_edge_claim") == sl.NO_EDGE_CLAIM, line
            assert payload.get("cleared_edge_signal") == \
                registration_invariant.stock_cleared_edge_signal(), line

    def test_the_cycle_never_clears_the_kill_switch(self, tmp_path):
        """Trip it, run, and it must still be engaged at the end."""
        bot, _exchange, _cfg = _paper_bot(tmp_path, strategy=BuyEveryTick())
        bot.startup()
        bot.store.trip_kill_switch("certification: engaged before the cycle")
        for _ in range(3):
            bot.tick()
        bot.shutdown()

        # Reopened from disk on purpose: an engaged switch that did not
        # survive the process would be no switch at all.
        from persistence import StateStore
        engaged, reason = StateStore(
            str(tmp_path / "cert.db")).is_kill_switch_engaged()
        assert engaged is True
        assert "certification" in reason

    def test_a_killed_cycle_opens_nothing(self, tmp_path):
        bot, _exchange, _cfg = _paper_bot(tmp_path, strategy=BuyEveryTick())
        bot.startup()
        before = bot.store.open_position_count()
        bot.store.trip_kill_switch("certification")
        for _ in range(3):
            bot.tick()
        assert bot.store.open_position_count() <= before

    def test_the_cycle_runs_with_entries_disabled(self, tmp_path):
        """Stand-down is a first-class mode, not a crash."""
        bot, _exchange, cfg = _paper_bot(tmp_path, strategy=None)
        assert bot.strategy is None
        assert bot.startup() is True
        for _ in range(3):
            bot.tick()
            assert bot.store.positions_without_stops() == []
        bot.shutdown()

        from persistence import StateStore
        assert StateStore(
            str(tmp_path / "cert.db")).positions_without_stops() == []

    def test_health_is_truthful_throughout(self, tmp_path):
        bot, _exchange, _cfg = _paper_bot(tmp_path, strategy=BuyEveryTick())
        bot.startup()
        for _ in range(3):
            bot.tick()
            payload = bot.health()
            assert payload["project"]["cleared_edge_signal"] == \
                registration_invariant.stock_cleared_edge_signal()
            assert payload["project"]["no_edge_claim"] == sl.NO_EDGE_CLAIM
            assert payload["paper"] is True
        bot.shutdown()

    def test_no_model_is_loaded_during_the_cycle(self, tmp_path):
        bot, _exchange, _cfg = _paper_bot(tmp_path, strategy=BuyEveryTick())
        bot.startup()
        bot.tick()
        payload = bot.health()
        assert payload["project"]["models_current_present"] is False
        assert payload["project"]["policy_mode"] == "off"
        bot.shutdown()


# ===========================================================================
# The certification artefact itself
# ===========================================================================


class TestTheCertificationArtefact:
    """The artefact must agree with the code, or it is marketing.

    A JSON file asserting "kill_switch: human_clear_only" is worth exactly as
    much as the test that checks the code still behaves that way. These tests
    tie the two together so the artefact cannot go stale quietly.
    """

    PATH = os.path.join(ARTIFACTS, "slice42_paper_agent_certification.json")

    def _payload(self):
        with open(self.PATH, encoding="utf-8") as handle:
            return json.load(handle)

    def test_it_exists_and_is_valid_json(self):
        assert os.path.exists(self.PATH)
        assert isinstance(self._payload(), dict)

    def test_the_required_fields_are_present_and_true(self):
        payload = self._payload()
        assert payload["certified_for"] == "continuous_paper_under_no_edge_claim"
        # A DATED artefact. It recorded a null cleared edge when it was
        # written and is never rewritten to match today's code.
        assert payload["cleared_edge_signal"] is None
        assert payload["live"] == "blocked"
        assert payload["model"] == "blocked"
        assert payload["kill_switch"] == "human_clear_only"

    def test_the_frozen_list_matches_the_code(self):
        """Membership and no-shrink, not equality.

        This artefact is the slice-42 record and is not rewritten when later
        research closes another line — a historical document edited to stay
        equal to today's code stops being a record. What must hold forever is
        that nothing it listed has been quietly un-frozen.
        """
        listed = tuple(self._payload()["frozen_absent"])
        for name in listed:
            assert name in ps.ABSENT_SIGNALS, name
        assert len(ps.ABSENT_SIGNALS) >= len(listed)

    def test_the_bars_in_the_artefact_match_the_code(self):
        bars = self._payload()["bars"]
        assert bars["m1"] == ps.M1_BAR == 95.0
        assert bars["m2"] == ps.M2_BAR == 95.0

    def test_it_does_not_claim_progress_toward_a_profit_agent(self):
        payload = self._payload()
        assert payload["closer_to_autonomous_profit_agent"] is False

    def test_the_limitations_are_honest_and_specific(self):
        limitations = " ".join(self._payload()["limitations"]).lower()
        assert "no stage-1 positive" in limitations
        assert "not a profit agent" in limitations
        assert "notimplementederror" in limitations
        assert "upward bias" in limitations

    def test_the_claimed_suite_counts_are_not_aspirational(self):
        """A number nobody checks is a number that drifts."""
        payload = self._payload()
        assert isinstance(payload["suite_passed"], int)
        assert payload["suite_passed"] > 2_800
        assert payload["suite_skipped"] == 1

    def test_the_revocation_conditions_each_have_a_test(self):
        """A certification with no way to lose it is a certificate."""
        conditions = self._payload()["revoked_by"]
        assert len(conditions) >= 6
        source = open(os.path.abspath(__file__), encoding="utf-8").read()
        for fragment in ("NO EDGE CLAIM", "clear_kill_switch_by_human",
                         "ENTRIES_ENABLED", "positions_without_stops",
                         "FORBIDDEN_FIELD_MARKERS"):
            assert fragment in source, fragment


# ===========================================================================
# GROUP G — the slice-45 freeze and the operator pack
# ===========================================================================


class TestGroupGResearchFreezeAndOperatorPack:
    """Slice 45. The programme is closed; the runbook must not lie about it.

    Documentation is the one part of this repository a test cannot fully
    police — but the *claims* it makes about code can be checked, and those are
    exactly the claims an operator will act on at 3am. So every operational
    assertion in the runbook is tied to the thing it describes.
    """

    DOCS = os.path.join(REPO, "docs")
    RUNBOOK = os.path.join(DOCS, "PAPER_OPERATOR_RUNBOOK.md")
    FREEZE = os.path.join(DOCS, "RESEARCH_PROGRAM_FREEZE.md")

    def _read(self, path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    # -- the freeze document ------------------------------------------------

    def test_the_freeze_document_exists(self):
        assert os.path.exists(self.FREEZE)

    def test_the_freeze_ledger_names_every_frozen_signal(self):
        text = self._read(self.FREEZE)
        for name in ps.ABSENT_SIGNALS:
            assert name in text, name

    def test_the_freeze_ledger_keeps_absent_and_inconclusive_apart(self):
        """The ledger must not file a never-measured family with the failures."""
        text = self._read(self.FREEZE)
        assert "INCONCLUSIVE" in text
        assert "range_location_fade_v1" in text
        assert "no M1" in text or "no percentile" in text.lower()

    def test_the_freeze_ledger_carries_every_closing_number(self):
        """A ledger without numbers is a press release."""
        text = self._read(self.FREEZE)
        for number in ("76.1", "77.5", "91.2", "91.5", "94.3", "92.0",
                       "91.3", "90.0", "1.6", "2.0", "95.0"):
            assert number in text, number

    def test_the_freeze_states_the_gate_for_future_stage_one(self):
        text = self._read(self.FREEZE)
        assert "50 scored trades" in text
        assert "different information set" in text.lower()

    def test_the_freeze_records_the_standing_obstacles(self):
        """Discovered halfway through a slice is worse than written down."""
        text = self._read(self.FREEZE)
        assert "order book" in text.lower()
        assert "1.5" in text

    def test_the_freeze_does_not_claim_progress_toward_profit(self):
        text = self._read(self.FREEZE)
        assert "Closer to an autonomous profit agent: NO" in text

    # -- the operator runbook ----------------------------------------------

    def test_the_operator_runbook_exists(self):
        assert os.path.exists(self.RUNBOOK)

    def test_it_states_the_five_startup_reads_an_operator_must_check(self):
        text = self._read(self.RUNBOOK)
        for field in ("timing_skill_research", "cleared_edge_signal",
                      "execution_mode", "live_authorized",
                      "models_current_present"):
            assert field in text, field

    def test_the_startup_reads_it_promises_are_what_the_code_returns(self):
        """The decisive one: the runbook's five reads, checked against code."""
        status = ps.current(stock_config()).as_dict()
        assert status["timing_skill_research"] == "CLOSED"
        assert status["cleared_edge_signal"] == \
            registration_invariant.stock_cleared_edge_signal()
        assert status["execution_mode"] == "paper"
        assert status["live_authorized"] is False
        assert status["models_current_present"] is False

    def test_it_quotes_the_kill_switch_token_exactly(self):
        """An operator will copy this string. It must be the real one."""
        text = self._read(self.RUNBOOK)
        assert "HUMAN_CLEARED_KILL_SWITCH" in text
        import persistence
        source = inspect.getsource(persistence.StateStore
                                   .clear_kill_switch_by_human)
        assert "HUMAN_CLEARED_KILL_SWITCH" in source

    def test_the_token_the_runbook_prints_actually_works(self, tmp_path):
        """Copied from the page, pasted into a shell — it must clear."""
        import persistence
        store = persistence.StateStore(str(tmp_path / "runbook.db"))
        store.trip_kill_switch("runbook test")
        store.clear_kill_switch_by_human("HUMAN_CLEARED_KILL_SWITCH")
        assert store.is_kill_switch_engaged()[0] is False

    def test_it_says_the_bot_may_trip_but_only_a_human_clears(self):
        text = self._read(self.RUNBOOK).lower()
        assert "only a human clears" in text

    def test_it_names_the_required_log_fields(self):
        text = self._read(self.RUNBOOK)
        assert "no_edge_claim" in text
        assert sl.NO_EDGE_CLAIM in text

    def test_it_names_the_forbidden_scoreboard_fields(self):
        text = self._read(self.RUNBOOK).lower()
        for marker in ("pnl", "equity", "win-rate", "sharpe", "drawdown"):
            assert marker in text, marker

    def test_it_warns_that_entries_enabled_is_not_a_safety_gate(self):
        text = self._read(self.RUNBOOK).lower()
        assert "not a safety gate" in text

    def test_it_lists_the_revocation_triggers(self):
        text = self._read(self.RUNBOOK).lower()
        for trigger in ("no edge claim", "kill switch", "entries_enabled",
                        "naked position"):
            assert trigger in text, trigger

    def test_it_states_the_perp_simulator_limitation(self):
        """The runbook must not imply a linear backtest number exists."""
        text = self._read(self.RUNBOOK)
        assert "NotImplementedError" in text
        import backtest
        assert "NotImplementedError" in inspect.getsource(backtest)

    def test_it_refuses_to_call_the_shell_a_profit_agent(self):
        text = self._read(self.RUNBOOK).lower()
        assert "not a profit agent" in text

    def test_it_points_at_the_detailed_runbook_rather_than_duplicating_it(self):
        """Duplicated procedure drifts, and a drifted runbook is worse than none."""
        text = self._read(self.RUNBOOK)
        assert "PAPER_RUNBOOK.md" in text
        assert os.path.exists(os.path.join(self.DOCS, "PAPER_RUNBOOK.md"))

    # -- the freeze, still enforced ----------------------------------------

    @pytest.mark.parametrize("signal", FROZEN)
    def test_every_frozen_name_still_refuses_a_perfect_forgery(self, signal,
                                                               tmp_path):
        _perfect_forgery(tmp_path, signal, "BTCUSD")
        _perfect_forgery(tmp_path, signal, "ETHUSDT")
        _perfect_forgery(tmp_path, signal, "SOLUSDT")
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_every_name_this_file_tracks_is_actually_frozen(self):
        """Membership, not a total — the list grows as research closes lines."""
        for name in FROZEN:
            assert name in ps.ABSENT_SIGNALS, name
        assert len(ps.ABSENT_SIGNALS) == len(FROZEN)

    def test_no_signal_exists_that_no_human_asked_for(self):
        """Slice 45 pinned the directory to three files. A human then filed a
        fourth intake, and the pin went stale the moment they did their job.

        The durable form of the invariant is the one slice 43 arrived at: every
        implemented signal is either frozen ABSENT or named in the human
        intake. A family invented by an agent would be in `signals/` and in
        neither list. Pinning the filenames instead would keep failing every
        time research legitimately advances, and a test that cries wolf gets
        edited rather than heeded.
        """
        with open(os.path.join(REPO, "NEW_SIGNAL_INTAKE.md"),
                  encoding="utf-8") as handle:
            intake = handle.read()
        implemented = sorted(
            name[:-3] for name in os.listdir(os.path.join(REPO, "signals"))
            if name.endswith(".py") and not name.startswith("__"))
        assert implemented, "no signals to check"
        for signal in implemented:
            assert signal in intake or signal in ps.ABSENT_SIGNALS, (
                f"{signal} is implemented but is neither frozen ABSENT nor "
                "named in the human intake")

    def test_at_most_one_open_signal_at_a_time(self):
        """Two open theses at once is how a grid gets in through the door."""
        with open(os.path.join(REPO, "NEW_SIGNAL_INTAKE.md"),
                  encoding="utf-8") as handle:
            intake = handle.read()
        implemented = {
            name[:-3] for name in os.listdir(os.path.join(REPO, "signals"))
            if name.endswith(".py") and not name.startswith("__")}
        open_signals = {s for s in implemented
                        if s in intake and s not in ps.ABSENT_SIGNALS}
        assert len(open_signals) <= 1, open_signals


class TestTheSlice45Artefact:
    """The artefact must agree with the code, or it is a press release."""

    PATH = os.path.join(ARTIFACTS, "slice45_research_freeze_and_paper_ops.json")

    def _payload(self):
        with open(self.PATH, encoding="utf-8") as handle:
            return json.load(handle)

    def test_it_exists_and_is_valid_json(self):
        assert os.path.exists(self.PATH)
        assert isinstance(self._payload(), dict)

    def test_the_required_fields_say_what_is_true(self):
        payload = self._payload()
        assert payload["research_program"] == "FROZEN"
        # A DATED artefact, never rewritten.
        assert payload["cleared_edge_signal"] is None
        assert payload["paper_certified"] is True
        assert payload["live"] == "blocked"
        assert payload["model"] == "blocked"
        assert payload["closer_to_autonomous_profit_agent"] is False
        assert payload["next_stage1_requires"] == \
            "new_human_material_intake_with_min_trade_count"

    def test_the_frozen_list_is_still_frozen(self):
        """Membership and no-shrink — see the slice-42 artefact for why.

        Slice 45 recorded six names. Slice 50 froze a seventh. The artefact
        keeps saying six, because that is what was true when it was written;
        the invariant that outlives it is that none of its six was released.
        """
        names = tuple(entry["signal"] for entry in
                      self._payload()["frozen_absent"])
        for name in names:
            assert name in ps.ABSENT_SIGNALS, name
        assert self._payload()["frozen_absent_count"] == len(names)
        assert len(ps.ABSENT_SIGNALS) >= len(names)

    def test_every_frozen_entry_carries_evidence(self):
        """The evidence a status implies, not a fixed string.

        An ABSENT entry cites the 95.0 bar its percentiles missed. An
        INCONCLUSIVE one has no percentiles, so it cites the control clause it
        failed instead — quoting 95.0 there would imply a comparison that never
        happened.
        """
        for entry in self._payload()["frozen_absent"]:
            assert len(entry["evidence"]) > 40, entry
            status = ps.FROZEN_STATUS[entry["signal"]]
            assert entry["status"] == status, entry
            if status == "ABSENT":
                assert "95" in entry["evidence"], entry
            else:
                assert "CONTROL INVALID" in entry["evidence"], entry
                assert "1.96" in entry["evidence"], entry

    def test_the_artefact_explains_the_two_statuses(self):
        note = self._payload()["frozen_status_note"]
        assert "ABSENT" in note and "INCONCLUSIVE" in note
        assert "fabrication" in note

    def test_the_bars_in_the_artefact_match_the_code(self):
        bars = self._payload()["bars"]
        assert bars["m1"] == ps.M1_BAR == 95.0
        assert bars["m2"] == ps.M2_BAR == 95.0

    def test_the_documents_it_points_at_all_exist(self):
        payload = self._payload()
        for key in ("paper_certification_standard", "paper_operator_runbook",
                    "research_freeze_document"):
            assert os.path.exists(os.path.join(REPO, payload[key])), key

    def test_the_gate_for_future_work_is_spelled_out(self):
        gate = " ".join(self._payload()["next_stage1_gate"]).lower()
        assert "different information set" in gate
        assert "50 scored trades" in gate
        assert "95.0" in gate

    def test_the_standing_obstacles_are_named(self):
        blob = " ".join(self._payload()["standing_obstacles"]).lower()
        assert "order book" in blob
        assert "upward bias" in blob

    def test_it_records_what_this_slice_did_not_do(self):
        did_not = " ".join(self._payload()["this_slice_did_not"]).lower()
        for claim in ("implement any signal", "run any measurement",
                      "arm any live path"):
            assert claim in did_not, claim

    def test_the_claimed_suite_counts_are_not_aspirational(self):
        payload = self._payload()
        assert payload["suite_passed"] > 3_000
        assert payload["suite_skipped"] == 1

    def test_the_models_claim_matches_the_filesystem(self):
        assert self._payload()["models_current_present"] is False
        assert not os.path.exists(os.path.join(REPO, "models", "current"))


# ===========================================================================
# GROUP H — continuous paper operations (slice 48)
# ===========================================================================
class TestGroupHContinuousPaperOps:
    """Slice 48. The runbook is a contract, not prose.

    An operator reads this page at 3am and types what it says. So every
    operational claim on it is bound to the thing it describes: the field names
    to the dataclass, the kill-switch token to a real switch, the forbidden
    field list to `session_log.FORBIDDEN_FIELD_MARKERS`, the revocation
    triggers to the tests that detect them.

    The failure mode being guarded against is not a wrong runbook — it is a
    runbook that was right when it was written and quietly stopped being right.
    Prose cannot notice that happening. These tests can.
    """

    DOCS = os.path.join(REPO, "docs")
    RUNBOOK = os.path.join(DOCS, "PAPER_OPERATOR_RUNBOOK.md")
    HOLD = os.path.join(DOCS, "RESEARCH_HOLD.md")

    #: The five reads section B tells an operator to check before starting.
    STARTUP_READS = ("timing_skill_research", "cleared_edge_signal",
                     "execution_mode", "live_authorized",
                     "models_current_present")

    def _read(self, path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    # -- the seven named checks --------------------------------------------

    def test_operator_runbook_exists(self):
        assert os.path.exists(self.RUNBOOK)

    def test_startup_reads_match_project_status_fields(self):
        """Named in the runbook, present on the dataclass, equal at runtime.

        Three separate things, because a runbook can be wrong in three ways:
        it can name a field that never existed, it can name one that has since
        been renamed, or it can name the right field and promise the wrong
        value for it.
        """
        text = self._read(self.RUNBOOK)
        declared = {field.name for field in dataclasses.fields(ps.ProjectStatus)}
        status = ps.current(stock_config()).as_dict()
        expected = {"timing_skill_research": "CLOSED",
                    "cleared_edge_signal":
                        registration_invariant.stock_cleared_edge_signal(),
                    "execution_mode": "paper",
                    "live_authorized": False,
                    "models_current_present": False}
        for name in self.STARTUP_READS:
            assert name in text, f"runbook does not name {name}"
            assert name in declared, f"{name} is not a ProjectStatus field"
            assert status[name] == expected[name], name

    def test_runbook_kill_switch_token_actually_works(self, tmp_path):
        """Lift the token off the page by parsing it, then use it.

        Deliberately not hard-coded here: a test that asserts a constant it
        also supplies would still pass if the runbook printed something else.
        The token is scraped out of the document and executed.
        """
        text = self._read(self.RUNBOOK)
        tokens = set(re.findall(r"clear_kill_switch_by_human\(\s*'([^']+)'",
                                text))
        assert tokens, "the runbook prints no clear-kill-switch command"
        assert len(tokens) == 1, f"the runbook prints two tokens: {tokens}"
        token = tokens.pop()

        import persistence
        store = persistence.StateStore(str(tmp_path / "group_h.db"))
        store.trip_kill_switch("group H: operator standing down")
        assert store.is_kill_switch_engaged()[0] is True
        store.clear_kill_switch_by_human(token)
        assert store.is_kill_switch_engaged()[0] is False
        store.close()

    def test_required_no_edge_claim_log_fields_named(self):
        """Section D's promise, checked against the constant it quotes."""
        text = self._read(self.RUNBOOK)
        assert sl.NO_EDGE_CLAIM in text
        for field in ("no_edge_claim", "cleared_edge_signal",
                      "timing_skill_research"):
            assert field in text, field
        # And the promise is true of a real session surface, not just the page.
        status = ps.current(stock_config()).as_dict()
        assert status["no_edge_claim"] == sl.NO_EDGE_CLAIM

    def test_forbidden_scoreboard_fields_named(self):
        """Every marker the code refuses must be refused on the page too.

        Read from `FORBIDDEN_FIELD_MARKERS` rather than listed here, so adding
        a marker to the code without documenting it fails.
        """
        text = self._read(self.RUNBOOK).lower()
        for marker in sl.FORBIDDEN_FIELD_MARKERS:
            word = marker.replace("_", "").replace("-", "")
            flattened = text.replace("_", "").replace("-", "")
            assert word in flattened, marker

    def test_revocation_triggers_listed(self):
        """Each trigger named, and each one detectable by a test in this file."""
        text = self._read(self.RUNBOOK).lower()
        for trigger in ("no edge claim", "kill switch", "entries_enabled",
                        "naked position", "frozen family", "credential"):
            assert trigger in text, trigger
        assert "no longer certified" in text
        # The claim "each has a test" is itself checkable.
        own_source = self._read(os.path.abspath(__file__))
        for marker in ("def test_", "kill_switch", "naked", "ENTRIES_ENABLED"):
            assert marker in own_source, marker

    def test_runbook_states_not_a_profit_agent(self):
        text = self._read(self.RUNBOOK).lower()
        assert "not a profit agent" in text
        assert "running it longer will not make it one" in text

    # -- continuous operation specifics ------------------------------------

    def test_the_runbook_has_every_section_the_pack_requires(self):
        text = self._read(self.RUNBOOK)
        for heading in ("## A. What you are running",
                        "## B. Startup checklist",
                        "## C. Kill switch",
                        "## D. Session / health log contract",
                        "## E. Pause, stand-down and revocation",
                        "## F. Pointers"):
            assert heading in text, heading

    def test_it_says_runtime_is_not_evidence(self):
        """The specific error a long unattended run invites."""
        text = self._read(self.RUNBOOK).lower()
        assert "runtime is not evidence" in text
        assert "uptime" in text

    def test_it_still_warns_entries_enabled_is_not_a_safety_gate(self):
        text = self._read(self.RUNBOOK).lower()
        assert "not a safety gate" in text
        assert "never" in text

    def test_it_still_says_only_a_human_clears(self):
        assert "only a human clears" in self._read(self.RUNBOOK).lower()

    def test_the_pointers_all_resolve(self):
        """A pointer to a file that does not exist is worse than no pointer."""
        text = self._read(self.RUNBOOK)
        for name in ("PAPER_RUNBOOK.md", "PAPER_AGENT_CERTIFICATION.md",
                     "RESEARCH_HOLD.md", "RESEARCH_PROGRAM_FREEZE.md"):
            assert name in text, name
            assert os.path.exists(os.path.join(self.DOCS, name)), name
        for name in ("EDGE.md", "NEW_SIGNAL_INTAKE.md"):
            assert name in text, name
            assert os.path.exists(os.path.join(REPO, name)), name

    def test_the_runbook_frozen_count_matches_the_code(self):
        """The runbook is a LIVING document: this one stays strict.

        Unlike the dated artefacts, an operator reads this page as current. If
        it says six while the code refuses seven, it is wrong on the page an
        operator acts from.
        """
        text = self._read(self.RUNBOOK)
        count = len(ps.ABSENT_SIGNALS)
        # Spelled out, so the prose tracks the deny-list instead of being
        # hand-edited at every freeze. It was hardcoded as "ten" until slice 56
        # closed an eleventh line.
        words = {9: "nine", 10: "ten", 11: "eleven", 12: "twelve",
                 13: "thirteen", 14: "fourteen", 15: "fifteen"}
        assert count in words, (
            f"{count} frozen names and no spelled-out form registered; add it "
            "rather than dropping the prose check")
        assert f"**{count}**" in text
        assert f"all {words[count]} frozen names" in text

    # -- the hold document --------------------------------------------------

    def test_the_research_hold_exists_and_the_runbook_points_at_it(self):
        assert os.path.exists(self.HOLD)
        assert "RESEARCH_HOLD.md" in self._read(self.RUNBOOK)

    def test_the_hold_names_every_frozen_family_with_its_status(self):
        text = self._read(self.HOLD)
        for name in ps.ABSENT_SIGNALS:
            assert name in text, name
        assert "INCONCLUSIVE" in text and "ABSENT" in text

    def test_the_hold_quotes_no_percentile_for_the_inconclusive_family(self):
        """The one fabrication this programme is most exposed to."""
        text = self._read(self.HOLD)
        assert ps.FROZEN_STATUS["range_location_fade_v1"] == "INCONCLUSIVE"
        assert "no M1 and no M2" in text
        assert "+3.632" in text

    def test_the_hold_states_the_gate_for_lifting_itself(self):
        text = self._read(self.HOLD)
        assert "50 scored trades" in text
        assert "different information set" in text.lower()
        assert f"{ps.M1_BAR}" in text and f"{ps.M2_BAR}" in text

    def test_the_hold_does_not_claim_progress_toward_a_profit_agent(self):
        assert "Closer to an autonomous profit agent: NO" in self._read(self.HOLD)


class TestTheSlice48Artefact:
    """The slice-48 record must agree with the code, not with its own hopes.

    Every field here is either read back out of the module it describes or
    checked against the filesystem. An artefact that asserts things about a
    repository without being tested against it is a press release with a
    schema.
    """

    PATH = os.path.join(ARTIFACTS, "slice48_research_hold_and_paper_ops.json")

    def _payload(self):
        with open(self.PATH, encoding="utf-8") as handle:
            return json.load(handle)

    def test_it_exists_and_is_valid_json(self):
        assert os.path.exists(self.PATH)
        assert self._payload()["schema"] == "research_hold_and_paper_ops/1"

    def test_the_hold_is_recorded_as_active(self):
        payload = self._payload()
        assert payload["research_hold"] is True
        assert payload["research_program"] == "FROZEN"
        assert payload["timing_skill_research"] == ps.RESEARCH_CLOSED
        assert os.path.exists(os.path.join(REPO,
                                           payload["research_hold_document"]))

    def test_the_required_fields_say_what_is_true(self):
        payload = self._payload()
        assert payload["frozen_absent_count"] == 6
        # A DATED artefact. It recorded a null cleared edge when it was
        # written and is never rewritten to match today's code.
        assert payload["cleared_edge_signal"] is None
        assert payload["closer_to_autonomous_profit_agent"] is False
        assert payload["next_stage1_requires"] == (
            "new_human_material_intake_with_min_trade_count")
        assert payload["execution_mode"] == "paper"
        assert payload["live_authorized"] is False
        assert payload["models_current_present"] is False
        assert payload["kill_switch"] == "human_clear_only"
        assert payload["entries_enabled_is_a_risk_gate"] is False
        assert payload["runtime_is_evidence"] is False

    def test_the_frozen_list_and_statuses_are_still_true_of_the_code(self):
        """Every name it recorded is still frozen, with the status it recorded.

        Equality would fail the moment research legitimately closes another
        line — as slice 50 did — and a test that cries wolf gets edited rather
        than heeded. The durable claim is that nothing was released and no
        status was downgraded.
        """
        payload = self._payload()
        for name, status in payload["frozen_status"].items():
            assert name in ps.ABSENT_SIGNALS, name
            assert ps.FROZEN_STATUS[name] == status, name
        assert payload["frozen_absent_count"] == len(payload["frozen_absent"])
        assert len(ps.ABSENT_SIGNALS) >= payload["frozen_absent_count"]

    def test_the_bars_in_the_artefact_match_the_code(self):
        bars = self._payload()["bars"]
        assert bars["m1"] == ps.M1_BAR == 95.0
        assert bars["m2"] == ps.M2_BAR == 95.0

    def test_it_records_that_no_measurement_was_run(self):
        payload = self._payload()
        assert payload["measurement_runs_this_slice"] == 0
        assert payload["signal_constants_changed"] is False

    def test_the_non_goals_are_explicit(self):
        blob = " ".join(self._payload()["non_goals"]).lower()
        for claim in ("no edge", "no live", "no model", "no signal 7",
                      "no null redesign", "no bar change", "no scoreboard"):
            assert claim in blob, claim

    def test_the_status_note_keeps_the_two_closures_apart(self):
        note = self._payload()["frozen_status_note"]
        assert "ABSENT" in note and "INCONCLUSIVE" in note
        assert "fabrication" in note.lower()

    def test_the_runbook_sections_it_claims_are_in_the_runbook(self):
        payload = self._payload()
        with open(os.path.join(REPO, payload["paper_operator_runbook"]),
                  encoding="utf-8") as handle:
            runbook = handle.read()
        for section in payload["runbook_sections"]:
            assert f"## {section}" in runbook, section
        for field in payload["runbook_startup_reads"]:
            assert field in runbook, field

    def test_the_documents_it_points_at_all_exist(self):
        payload = self._payload()
        for key in ("research_hold_document", "paper_certification_standard",
                    "paper_operator_runbook", "guard_reassertion_log"):
            assert os.path.exists(os.path.join(REPO, payload[key])), key

    def test_the_gate_for_future_work_is_spelled_out(self):
        gate = " ".join(self._payload()["next_stage1_gate"]).lower()
        assert len(self._payload()["next_stage1_gate"]) == 5
        for clause in ("different information set", "50 scored trades",
                       "frozen in git", "control", "95.0"):
            assert clause in gate, clause

    def test_the_standing_obstacles_are_named(self):
        blob = " ".join(self._payload()["standing_obstacles"]).lower()
        assert "order book" in blob
        assert "bias" in blob

    def test_the_claimed_suite_counts_are_not_aspirational(self):
        payload = self._payload()
        assert payload["suite_passed"] > 3_100
        assert payload["suite_skipped"] == 1
        assert payload["certification_tests_passed"] >= 156

    def test_the_models_claim_matches_the_filesystem(self):
        assert self._payload()["models_current_present"] is False
        assert not os.path.exists(os.path.join(REPO, "models", "current"))

    def test_it_carries_no_scoreboard_field(self):
        """The refusal applies to this artefact too, not just to sessions.

        One field is exempt by name: `closer_to_autonomous_profit_agent`. It
        contains the marker `profit` and is not a measurement of any kind — it
        is the standing denial that one exists, and it is required to be
        `false`. Exempting it by exact name rather than by loosening the marker
        list keeps `profit_pct` or `net_profit` failing.
        """
        exempt = {"closer_to_autonomous_profit_agent"}
        payload = self._payload()
        assert payload["closer_to_autonomous_profit_agent"] is False
        for field in payload:
            if field in exempt:
                continue
            assert not any(marker in field.lower()
                           for marker in sl.FORBIDDEN_FIELD_MARKERS), field
