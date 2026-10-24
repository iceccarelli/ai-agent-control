"""`tools/drill_final_state.py`: the missing venue reads, added to a finished
drill's evidence chain. Fake brokers only; nothing here claims what Bybit says.
"""
from __future__ import annotations

import json
import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
sys.path.insert(0, HERE)

import drill as D  # noqa: E402
import drill_final_state as T  # noqa: E402
import evidence_reconcile as R  # noqa: E402
import test_evidence_reconcile_carry as C  # noqa: E402
from test_drill_final_state import CapVenue  # noqa: E402


class Evidence:
    def __init__(self, complete=True, rows=1, errors=()):
        self.complete, self.payments, self.errors = complete, [0] * rows, list(errors)


class ToolBroker(CapVenue):
    """A CapVenue that also answers the settlement read, and remembers it."""

    def __init__(self, evidence=None, **kw):
        super().__init__(**kw)
        self.evidence = evidence or Evidence()
        self.settlement_window = None

    def get_funding_evidence(self, symbol, start_ms, end_ms=None):
        self.settlement_window = (symbol, start_ms, end_ms)
        if isinstance(self.evidence, Exception):
            raise self.evidence
        return self.evidence


def capture_of(records=None):
    return R.capture_from_records(C.build() if records is None else records)


def seeded(broker):
    """Pretend the two orders in C.build() were filled for 0.001."""
    broker.filled = {C.OPEN_LINK: C.QTY, C.CLOSE_LINK: C.QTY}
    return broker


class TestDiscovery:
    def test_links_come_from_the_files_own_create_records_oldest_first(self):
        orders, first = T.discover_sent_orders(capture_of())
        assert orders == [("linear", "BTCUSDT", C.OPEN_LINK),
                          ("linear", "BTCUSDT", C.CLOSE_LINK)]
        assert first == C.OPEN_MS + 1_000      # the first create's capture time

    def test_a_failed_create_is_not_an_order(self):
        records = C.build()
        for r in records:
            if r["method"] == "POST":
                r["response"]["retCode"] = 10001
        orders, first = T.discover_sent_orders(capture_of(C._rechain(records)))
        assert orders == [] and first is None

    def test_other_environments_are_ignored(self):
        records = C.build()
        for r in records:
            r["request_url"] = r["request_url"].replace("api-testnet", "api")
            r["environment"] = "mainnet"
        orders, _ = T.discover_sent_orders(capture_of(C._rechain(records)))
        assert orders == []

    def test_an_empty_file_discovers_nothing(self):
        assert T.discover_sent_orders(capture_of([])) == ([], None)


class TestRun:
    def test_every_discovered_order_is_read_and_the_settlement_window_is_taken(self):
        broker = seeded(ToolBroker())
        now = C.NOW
        result = T.run(broker, capture_of(), symbol="BTCUSDT", now_ms=now)
        assert result["complete"] is True and result["orders"] == 2
        assert result["settlement_rows"] == 1
        symbol, start, end = broker.settlement_window
        assert (symbol, end) == ("BTCUSDT", now)
        assert start == C.OPEN_MS + 1_000 - T.SETTLEMENT_LEAD_MS

    def test_an_incomplete_settlement_read_is_not_complete(self):
        broker = seeded(ToolBroker(Evidence(complete=False, errors=["window x"])))
        result = T.run(broker, capture_of(), symbol="BTCUSDT", now_ms=C.NOW)
        assert result["complete"] is False
        assert any("settlement read incomplete" in f for f in result["failures"])

    def test_a_failing_settlement_read_is_a_failure_not_a_crash(self):
        broker = seeded(ToolBroker(RuntimeError("down")))
        result = T.run(broker, capture_of(), symbol="BTCUSDT", now_ms=C.NOW)
        assert result["complete"] is False
        assert any("settlement read failed" in f for f in result["failures"])

    def test_an_order_the_venue_no_longer_knows_fails_the_run(self):
        broker = seeded(ToolBroker())
        broker.missing = True
        result = T.run(broker, capture_of(), symbol="BTCUSDT", now_ms=C.NOW)
        assert result["complete"] is False

    def test_a_file_with_no_orders_is_a_failure_and_reads_nothing(self):
        broker = ToolBroker()
        result = T.run(broker, capture_of([]), symbol="BTCUSDT", now_ms=C.NOW)
        assert result["complete"] is False and broker.settlement_window is None


class TestMain:
    def _write(self, tmp_path, records):
        path = tmp_path / "ev.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        return str(path)

    def _bot(self, broker, **cfg_over):
        cfg = dict(BYBIT_VENUE="testnet", USE_TESTNET=True, PAPER_TRADING=False,
                   LIVE_TRADING_ACK="", STATE_DB_PATH="")
        cfg.update(cfg_over)
        return types.SimpleNamespace(cfg=types.SimpleNamespace(**cfg),
                                     carry=types.SimpleNamespace(broker=broker))

    def test_a_missing_file_is_refused(self, tmp_path, capsys):
        assert T.main(["--evidence-path", str(tmp_path / "nope.jsonl")]) == 3

    def test_a_broken_chain_is_refused_before_anything_is_appended(
            self, tmp_path, capsys, monkeypatch):
        records = C.build()
        records[1]["response"]["result"]["list"][0]["execQty"] = "9"
        path = self._write(tmp_path, records)
        before = open(path).read()
        monkeypatch.setattr(D, "_load_bot", lambda *a, **k: pytest.fail("loaded"))
        assert T.main(["--evidence-path", path]) == 3
        assert open(path).read() == before
        assert "does not verify" in capsys.readouterr().err

    def test_a_complete_read_prints_json_and_exits_0(self, tmp_path, capsys, monkeypatch):
        path = self._write(tmp_path, C.build())
        monkeypatch.setattr(D, "_load_bot",
                            lambda p: self._bot(seeded(ToolBroker())))
        assert T.main(["--evidence-path", path]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["complete"] is True and out["orders"] == 2

    def test_an_incomplete_read_exits_6(self, tmp_path, capsys, monkeypatch):
        path = self._write(tmp_path, C.build())
        broker = seeded(ToolBroker())
        broker.missing = True
        monkeypatch.setattr(D, "_load_bot", lambda p: self._bot(broker))
        assert T.main(["--evidence-path", path]) == 6

    @pytest.mark.parametrize("over", [{"BYBIT_VENUE": "mainnet"},
                                      {"USE_TESTNET": False},
                                      {"PAPER_TRADING": True},
                                      {"LIVE_TRADING_ACK": "yes"}])
    def test_anything_but_an_unarmed_testnet_stack_is_refused(
            self, tmp_path, monkeypatch, over):
        path = self._write(tmp_path, C.build())
        monkeypatch.setattr(D, "_load_bot",
                            lambda p: self._bot(seeded(ToolBroker()), **over))
        assert T.main(["--evidence-path", path]) == 3

    def test_a_stack_that_will_not_start_exits_2(self, tmp_path, monkeypatch):
        path = self._write(tmp_path, C.build())

        def boom(p):
            raise RuntimeError("evidence capture is for the testnet venue only")
        monkeypatch.setattr(D, "_load_bot", boom)
        assert T.main(["--evidence-path", path]) == 2

    def test_a_non_carry_book_exits_2(self, tmp_path, monkeypatch):
        path = self._write(tmp_path, C.build())
        bot = self._bot(ToolBroker())
        bot.carry = None
        monkeypatch.setattr(D, "_load_bot", lambda p: bot)
        assert T.main(["--evidence-path", path]) == 2

    def test_the_tool_only_reads(self):
        """No order-sending name appears in this module's CODE (docstrings may
        say 'placed'); the only broker calls are the capture's GETs."""
        import ast
        tree = ast.parse(open(T.__file__, encoding="utf-8").read())
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} \
            | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert not names & {"place_market", "place_post_only", "cancel_order",
                            "_unwind", "_request", "trip_kill_switch"}
