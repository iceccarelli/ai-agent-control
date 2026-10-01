"""tools/testnet_session.py — refuse mainnet; never set live_authorized; ledger shape."""
from __future__ import annotations

import json
import os
import sys
import types
from unittest import mock

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import testnet_session as ts  # noqa: E402


def _cfg(**over):
    base = {
        "BYBIT_VENUE": "testnet",
        "PAPER_TRADING": False,
        "BYBIT_API_KEY": "key123",
        "BYBIT_API_SECRET": "secret123",
        "CATEGORY": "spot",
        "TAKER_FEE": 0.001,
    }
    base.update(over)
    return types.SimpleNamespace(**base)


class TestRefuseMainnet:
    def test_cli_refuses_mainnet_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BYBIT_VENUE", "mainnet")
        failure = tmp_path / "fail.json"
        ledger = tmp_path / "cash_ledger.json"
        rc = ts.main([
            "--failure-out", str(failure),
            "--ledger", str(ledger),
            "--state-db", str(tmp_path / "s.db"),
            "--evidence-path", str(tmp_path / "e.jsonl"),
        ])
        assert rc == 1
        payload = json.loads(failure.read_text())
        assert "mainnet" in payload["failure"].lower()
        assert payload["allows_live"] is False
        assert payload["live_authorized"] is False
        assert not ledger.exists()

    def test_cli_refuses_demo_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BYBIT_VENUE", "demo")
        failure = tmp_path / "fail.json"
        rc = ts.main([
            "--failure-out", str(failure),
            "--ledger", str(tmp_path / "cash_ledger.json"),
            "--state-db", str(tmp_path / "s.db"),
            "--evidence-path", str(tmp_path / "e.jsonl"),
        ])
        assert rc == 1
        assert "demo" in json.loads(failure.read_text())["failure"].lower()


class TestNeverSetsLiveAuthorized:
    def test_source_does_not_assign_live_authorized(self):
        src = open(ts.__file__, encoding="utf-8").read()
        # Must never arm live. Docstring may name the forbidden tokens;
        # executable assignments / env writes of them must not appear.
        import ast
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    name = getattr(t, "id", None) or getattr(
                        getattr(t, "attr", None), "lower", lambda: None)()
                    if isinstance(t, ast.Attribute):
                        name = t.attr
                    if name in ("live_authorized", "allows_live"):
                        # only False / falsey constant writes are ok
                        if isinstance(node.value, ast.Constant) and node.value.value is False:
                            continue
                        if isinstance(node.value, ast.NameConstant) and node.value.value is False:
                            continue
                        pytest.fail(f"assigns {name} to non-False")
            if isinstance(node, ast.Call):
                # os.environ[...] = ... patterns via subscript store
                pass
        assert "LIVE_TRADING_ACK=" not in src
        assert 'os.environ["LIVE_TRADING_ACK"]' not in src
        assert "allows_live = True" not in src
        assert "live_authorized = True" not in src

    def test_ledger_always_allows_live_false(self):
        ledger = ts.build_cash_ledger(
            symbol="BTCUSDT", category="spot", n_fills=2,
            entry_price=100.0, exit_price=101.0, qty=1.0, flat=True)
        assert ledger["allows_live"] is False
        assert set([
            "date_utc", "category", "symbol", "n_fills", "pnl_usd",
            "fees_usd", "flat", "allows_live",
        ]).issubset(ledger.keys())


class TestNotionalCapThisPathOnly:
    def test_capped_sizer_reduces_qty(self):
        from position_sizing import SizingResult, SizingRequest

        class Inner:
            def calculate_position_size(self, request):
                return SizingResult(
                    symbol=request.symbol, qty=1.0, notional=100000.0,
                    reason="OK", method="test")

        capped = ts._CappedSizer(Inner(), max_notional_usd=1000.0)
        req = SizingRequest(
            symbol="BTCUSDT", side="Buy", current_price=100000.0,
            current_portfolio_value=10000.0, stop_loss_distance=0.02)
        # SizingRequest may need filters — construct minimally
        try:
            out = capped.calculate_position_size(req)
        except TypeError:
            # If SizingRequest signature differs, use a SimpleNamespace.
            req = types.SimpleNamespace(
                symbol="BTCUSDT", current_price=100000.0, filters=None)
            out = capped.calculate_position_size(req)
        assert out.qty <= 1000.0 / 100000.0 + 1e-12
        assert out.qty < 1.0


class TestSubmitAndFlattenNames:
    def test_submit_order_calls_engine_execute(self):
        calls = {}

        class Eng:
            def execute(self, intent):
                calls["intent"] = intent
                return types.SimpleNamespace(
                    ok=True, stage="complete", reason="ENTERED",
                    qty=0.01, detail={})

        ts.submit_order(
            Eng(), symbol="BTCUSDT", entry_price=100.0,
            stop_price=98.0, take_profit=105.0)
        assert calls["intent"].signal_type == "BUY"
        assert calls["intent"].symbol == "BTCUSDT"

    def test_flatten_calls_close_position(self):
        calls = {}

        class Eng:
            def close_position(self, *, symbol, reason):
                calls["symbol"] = symbol
                calls["reason"] = reason
                return types.SimpleNamespace(
                    ok=True, reason="CLOSED", detail={})

        ts.flatten_position(Eng(), symbol="BTCUSDT")
        assert calls["symbol"] == "BTCUSDT"


class TestZeroFillsExitCode:
    def test_main_returns_2_on_zero_fills(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BYBIT_VENUE", "testnet")
        monkeypatch.delenv("USE_TESTNET", raising=False)

        fake_cfg = _cfg()
        fake_store = types.SimpleNamespace(
            open_positions=lambda: [],
            close=lambda: None,
        )
        fake_client = types.SimpleNamespace(
            base_url="https://api-testnet.bybit.com",
            get_last_price=lambda s: 100000.0,
        )
        fake_engine = types.SimpleNamespace(
            paper=False,
            sizer=None,
            execute=lambda intent: types.SimpleNamespace(
                ok=False, stage="pre_gate", reason="BLOCKED", qty=0.0,
                detail={}),
            close_position=lambda **kw: types.SimpleNamespace(
                ok=False, reason="NO_SUCH_POSITION", detail={}),
        )

        def fake_build(**kw):
            return fake_cfg, fake_store, fake_client, fake_engine, object

        def fake_preflight(cfg, client):
            return types.SimpleNamespace(ok=True, checks={"environment": "ok"})

        monkeypatch.setattr(ts, "_build_session_stack", fake_build)
        monkeypatch.setattr(ts.tcr, "run_preflight", fake_preflight)

        failure = tmp_path / "fail.json"
        rc = ts.main([
            "--failure-out", str(failure),
            "--ledger", str(tmp_path / "cash_ledger.json"),
            "--state-db", str(tmp_path / "s.db"),
            "--evidence-path", str(tmp_path / "e.jsonl"),
        ])
        assert rc == 2
        assert json.loads(failure.read_text())["entry_fills"] == 0
