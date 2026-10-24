"""In a REAL stack `bot.carry.broker` is a LedgerBroker around the CarryBroker.

The ledger delegates explicitly, so it carries none of the adapter's reads
(`sent_order_links`, `read_order`, `get_order_executions`) and no `client`. The
drill's final-state capture, `tools/drill_final_state.py` and the testnet-host
check all asked the WRAPPER, so on a real stack the capture never ran, the tool
would have crashed and the host check silently skipped itself. The earlier
tests passed because their fake brokers carried those attributes directly.
These tests wrap every fake the way the real stack does.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
sys.path.insert(0, HERE)

import config as _config  # noqa: E402
import drill as D  # noqa: E402
import drill_final_state as T  # noqa: E402
import ledger as L  # noqa: E402
from bybit_connection import TESTNET_REST  # noqa: E402
from carry_broker import CarryBroker  # noqa: E402
import evidence_reconcile as R  # noqa: E402
import test_evidence_reconcile_carry as C  # noqa: E402
from test_drill_final_state import CapVenue  # noqa: E402
from test_drill_final_state_tool import ToolBroker, seeded  # noqa: E402


def wrapped(fake):
    return L.LedgerBroker(fake, L.Journal(), ms=lambda: 1)


@pytest.fixture()
def real_bot(monkeypatch, tmp_path):
    env = {"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
           "CARRY_EXECUTION_MODE": "overlay", "BYBIT_VENUE": "testnet",
           "PAPER_TRADING": "0", "STATE_DB_PATH": str(tmp_path / "s.db")}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(_config, "_CFG", _config.load(env))
    return D._load_bot("")


class TestTheRealStackShape:
    def test_the_ledger_hides_what_the_capture_needs(self, real_bot):
        top = real_bot.carry.broker
        assert isinstance(top, L.LedgerBroker)
        for name in ("sent_order_links", "read_order", "get_order_executions",
                     "client"):
            assert not hasattr(top, name), name       # the defect, pinned

    def test_unwrap_reaches_the_adapter_that_does(self, real_bot):
        adapter = D.unwrap_broker(real_bot.carry.broker)
        assert isinstance(adapter, CarryBroker)
        for name in ("sent_order_links", "read_order", "get_order_executions"):
            assert hasattr(adapter, name), name
        assert adapter.client is real_bot.client

    def test_the_testnet_host_check_now_really_runs_and_passes_on_testnet(
            self, real_bot):
        adapter = D.unwrap_broker(real_bot.carry.broker)
        assert adapter.client.base_url == TESTNET_REST
        assert D.session_refusal(real_bot.cfg, adapter.client) is None

    def test_and_it_refuses_a_client_on_another_host(self, real_bot):
        class Other:
            base_url = "https://api.bybit.com"
        assert "not the testnet host" in D.session_refusal(real_bot.cfg, Other())

    def test_a_plain_object_and_a_fake_are_their_own_adapter(self):
        fake = CapVenue()
        assert D.unwrap_broker(fake) is fake
        assert D.unwrap_broker(wrapped(fake)) is fake
        assert D.unwrap_broker(wrapped(wrapped(fake))) is fake


class TestTheCaptureWorksThroughTheWrapper:
    def test_capture_final_venue_state_sees_the_orders(self):
        fake = CapVenue()
        fake.place_market(symbol="BTCUSDT", side="Sell", qty=0.001, product="linear")
        got = D.capture_final_venue_state(wrapped(fake))
        assert got["complete"] is True and got["orders"] == 1

    def test_a_broker_that_cannot_name_its_orders_is_not_complete(self):
        class Plain:
            def get_perp_position(self, symbol):
                return 0.0
        got = D.capture_final_venue_state(Plain())
        assert got["complete"] is False
        assert "cannot say which orders" in got["failures"][0]
        assert D.capture_final_venue_state(wrapped(Plain()))["complete"] is False

    def test_a_whole_drill_through_the_wrapper_captures_and_exits_0(self):
        report = D.run_drill(broker=wrapped(CapVenue()), arm=True, notional=100.0,
                             venue="testnet", capture_final=True)
        assert report["verdict"] == "PASSED"
        assert report["evidence_capture"]["complete"] is True
        assert report["evidence_capture"]["orders"] == 2
        assert report["orders_sent"] == 2
        assert D.drill_exit_code(report, hold=False) == 0

    def test_without_the_unwrap_this_would_have_been_exit_6(self, monkeypatch):
        """The old behaviour, pinned as what the fix removes."""
        monkeypatch.setattr(D, "unwrap_broker", lambda b: b)
        report = D.run_drill(broker=wrapped(CapVenue()), arm=True, notional=100.0,
                             venue="testnet", capture_final=True)
        assert report["evidence_capture"]["complete"] is False
        assert D.drill_exit_code(report, hold=False) == 6


class TestTheAfterTheFactToolThroughTheWrapper:
    def test_it_no_longer_crashes_on_a_wrapped_broker(self):
        broker = wrapped(seeded(ToolBroker()))
        result = T.run(broker, R.capture_from_records(C.build()),
                       symbol="BTCUSDT", now_ms=C.NOW)
        assert result["complete"] is True and result["orders"] == 2

    def test_a_wrapped_broker_without_the_adapter_is_a_failure_not_a_crash(self):
        class Plain:
            def get_funding_evidence(self, *a, **k):
                raise AssertionError("must not be reached")
        result = T.run(wrapped(Plain()), R.capture_from_records(C.build()),
                       symbol="BTCUSDT", now_ms=C.NOW)
        assert result["complete"] is False
