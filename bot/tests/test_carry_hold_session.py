"""The carry cycle that matters: open, HOLD across a funding settlement,
close, book what the venue paid, write a cash ledger -- or fail cleanly."""
from __future__ import annotations

import json
import os
import sys
import time
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
sys.path.insert(0, HERE)

import drill as D  # noqa: E402
from carry_broker import FundingHistory, SettlementEvidence  # noqa: E402
from test_drill import Venue  # noqa: E402


class HoldVenue(Venue):
    """A venue that pays one settlement while the pair is held, if told to."""

    def __init__(self, clock, *, pay_after_s=250.0, usd=4.25, sign="confirmed",
                 evidence_down_after_close=False, **kw):
        super().__init__(**kw)
        self.clock = clock
        self.pay_after_s = pay_after_s
        self.usd = usd
        self.sign = sign
        self.down_after_close = evidence_down_after_close
        self.opened_ms = None
        self.closed = False

    def place_market(self, **kw):
        res = super().place_market(**kw)
        if res is not None and kw["side"] == "Sell" and kw["product"] == "linear":
            self.opened_ms = int(time.time() * 1000)
        if res is not None and kw["side"] == "Buy" and kw["product"] == "linear":
            self.closed = True
        return res

    def _payments(self):
        if self.opened_ms is None or self.pay_after_s is None:
            return []
        t = self.opened_ms + int(self.pay_after_s * 1000)
        if time.time() * 1000 < t:
            return []
        return [{"id": "S1", "ms": t, "usd": self.usd, "currency": "USDT",
                 "size": -0.001, "symbol": "BTCUSDT", "change": self.usd,
                 "sign": self.sign, "sign_why": "", "category": "linear"}]

    def get_funding_evidence(self, symbol, start_ms, end_ms=None):
        ev = SettlementEvidence(start_ms=int(start_ms), end_ms=int(end_ms),
                                windows=1, pages=1)
        if self.down_after_close and self.closed:
            ev.errors.append("window unreadable: boom")
            return ev
        ev.payments = [p for p in self._payments()
                       if start_ms < p["ms"] <= end_ms]
        return ev

    def get_funding_history_range(self, symbol, start_ms, end_ms):
        h = FundingHistory(start_ms=int(start_ms), end_ms=int(end_ms))
        h.prints = [(3.0, p["ms"]) for p in self._payments()]
        return h


@pytest.fixture()
def clock(monkeypatch):
    skew = [0.0]
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + skew[0])
    return skew


def _plan(clock, **kw):
    kw.setdefault("poll_s", 120.0)
    kw.setdefault("max_hold_s", 3600.0)
    kw.setdefault("settle_wait_s", 7 * 3600.0)       # past the ledger's 6 h grace
    return D.HoldPlan(sleep=lambda s: clock.__setitem__(0, clock[0] + s), **kw)


def _run(venue, clock, tmp_path, **plan_kw):
    ledger = str(tmp_path / "cash_ledger.json")
    report = D.run_drill(broker=venue, arm=True, notional=100.0,
                         venue="testnet", hold=_plan(clock, **plan_kw),
                         cash_ledger=ledger, out=str(tmp_path / "drill.json"))
    return report, ledger


class TestAFullCycleThatBooksRealFunding:
    def test_it_holds_closes_books_the_settlement_and_writes_the_ledger(
            self, clock, tmp_path):
        venue = HoldVenue(clock)
        report, ledger = _run(venue, clock, tmp_path)
        assert report["verdict"] == "PASSED", [
            (s["stage"], s["why"]) for s in report["stages"] if not s["ok"]]
        assert report["funding_status"] == D.CONFIRMED
        assert venue.perp_qty == pytest.approx(0.0)         # flat at the venue
        data = json.load(open(ledger))
        assert data["schema"] == "cash_ledger/2"
        assert data["allows_live"] is False
        assert data["flat"] is True
        assert data["funding_status"] == D.CONFIRMED
        assert data["funding_settlements"] == 1
        assert data["funding_venue_usd"] == pytest.approx(4.25)
        assert data["funding_confirmed"] is True
        assert data["fees_confirmed"] in (True, False)      # stated, not assumed
        assert D.drill_exit_code(report, hold=True) == 0

    def test_the_stage_order_is_open_pair_hold_unwind_flat_funding_statement(
            self, clock, tmp_path):
        report, _ = _run(HoldVenue(clock), clock, tmp_path)
        assert [s["stage"] for s in report["stages"]][-7:] == [
            "open", "pair", "hold", "unwind", "final_reconcile",
            "funding_final", "statement"]


class TestInsideTheDeliveryGraceItIsPendingNeverConfirmed:
    def test_rows_booked_but_not_yet_judgeable_exit_5_not_0(self, clock, tmp_path):
        report, ledger = _run(HoldVenue(clock), clock, tmp_path,
                              settle_wait_s=600.0)
        assert report["funding_status"] == D.PENDING
        data = json.load(open(ledger))
        assert data["funding_status"] == D.PENDING
        assert data["funding_confirmed"] is False
        assert data["funding_venue_usd"] == pytest.approx(4.25)
        assert D.drill_exit_code(report, hold=True) == 5


class TestItFailsCleanlyWithoutRealFunding:
    def test_no_settlement_within_the_hold_unwinds_and_is_not_a_result(
            self, clock, tmp_path):
        venue = HoldVenue(clock, pay_after_s=None)
        report, ledger = _run(venue, clock, tmp_path, max_hold_s=600.0)
        assert report["verdict"] == "FAILED" and report["failed_stage"] == "hold"
        assert venue.perp_qty == pytest.approx(0.0)         # the pair was closed
        assert not os.path.exists(ledger)                   # nothing to claim
        assert D.drill_exit_code(report, hold=True) != 0

    def test_a_settlement_whose_sign_is_not_confirmed_never_counts(
            self, clock, tmp_path):
        venue = HoldVenue(clock, sign="mismatch")
        report, ledger = _run(venue, clock, tmp_path, max_hold_s=900.0)
        assert report["verdict"] == "FAILED" and report["failed_stage"] == "hold"
        assert not os.path.exists(ledger)
        assert venue.perp_qty == pytest.approx(0.0)

    def test_incomplete_evidence_after_the_close_is_unconfirmed_not_assumed(
            self, clock, tmp_path):
        venue = HoldVenue(clock, evidence_down_after_close=True)
        report, ledger = _run(venue, clock, tmp_path)
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "funding_final"
        assert report["funding_status"] == D.UNCONFIRMED
        data = json.load(open(ledger))                       # flat: stated honestly
        assert data["funding_status"] == D.UNCONFIRMED and data["allows_live"] is False
        assert D.drill_exit_code(report, hold=True) == 4

    def test_zero_fills_fails_cleanly_with_no_ledger(self, clock, tmp_path):
        report, ledger = _run(HoldVenue(clock, perp_fills=False), clock, tmp_path)
        assert report["verdict"] == "FAILED" and report["failed_stage"] == "open"
        assert not os.path.exists(ledger)
        assert D.drill_exit_code(report, hold=True) == 2


class TestItRefusesTheWrongEnvironment:
    def _cfg(self, **over):
        base = dict(BYBIT_VENUE="testnet", USE_TESTNET=True, PAPER_TRADING=False,
                    LIVE_TRADING_ACK="")
        base.update(over)
        return types.SimpleNamespace(**base)

    def test_testnet_unpaper_unarmed_is_accepted(self):
        assert D.session_refusal(self._cfg()) is None

    @pytest.mark.parametrize("over", [
        {"BYBIT_VENUE": "mainnet"}, {"BYBIT_VENUE": "demo"},
        {"USE_TESTNET": False}, {"PAPER_TRADING": True},
        {"LIVE_TRADING_ACK": "I_UNDERSTAND"},
    ])
    def test_everything_else_is_refused(self, over):
        assert D.session_refusal(self._cfg(**over))

    def test_a_client_on_the_mainnet_host_is_refused(self):
        from bybit_connection import MAINNET_REST
        client = types.SimpleNamespace(base_url=MAINNET_REST)
        assert "mainnet host" in D.session_refusal(self._cfg(), client)

    def test_hold_without_arm_is_refused_before_anything_loads(self):
        assert D.main(["--hold-for-settlement"]) == 3

    def test_zero_min_settlements_is_refused(self):
        assert D.main(["--arm", "--hold-for-settlement",
                       "--min-settlements", "0"]) == 3


class TestTheExitCodeIsHonest:
    def test_flat_but_not_confirmed_is_never_zero(self):
        rep = {"verdict": "PASSED", "still_open": False, "orders_sent": 4,
               "funding_status": D.NO_SETTLEMENT, "cash_ledger": ""}
        assert D.drill_exit_code(rep, hold=True) == 4

    def test_a_confirmed_status_without_a_ledger_file_is_never_zero(self):
        rep = {"verdict": "PASSED", "still_open": False, "orders_sent": 4,
               "funding_status": D.CONFIRMED, "cash_ledger": "/nonexistent"}
        assert D.drill_exit_code(rep, hold=True) == 4

    def test_not_flat_is_a_failure(self):
        rep = {"verdict": "FAILED", "still_open": True, "orders_sent": 4,
               "funding_status": D.CONFIRMED, "cash_ledger": ""}
        assert D.drill_exit_code(rep, hold=True) == 1
