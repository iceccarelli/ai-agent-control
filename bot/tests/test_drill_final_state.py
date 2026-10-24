"""The carry drill states its own cost, captures the venue's final rows, and
stops cleanly.

Three defects, one file:
  (a) SIGTERM/Ctrl-C during the hold printed a traceback although the report
      was written;
  (b) `abort_unwind` recorded what the venue held but not the exit price or
      fee, so an aborted drill could not say what it had cost;
  (c) the drill never read an execution list, so no fill was venue-proven.
All against a fake venue; nothing here says what Bybit returns.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
sys.path.insert(0, HERE)

import drill as D  # noqa: E402
import evidence_reconcile as R  # noqa: E402
from carry_broker import CarryBroker, LINEAR, PairIncident  # noqa: E402
from test_carry_broker import FakeClient, broker as make_broker, envelope  # noqa: E402
from test_carry_hold_session import HoldVenue, _plan, clock  # noqa: E402,F401
from test_drill import MARK, Venue  # noqa: E402

QTY = 0.001                              # what the fake wallet sizes to at $100
FEE = QTY * MARK * 5.5 / 1e4             # the fake venue's per-leg fee


class CapVenue(Venue):
    """A fake venue that can also be read back, as CarryBroker can."""

    def __init__(self, **kw):
        self.exec_shortfall = 0.0
        self.resting = False
        self.missing = False
        self.read_boom = False
        super().__init__(**kw)
        self.sent_order_links = []
        self.filled = {}
        self.position_reads = 0

    def place_market(self, **kw):
        res = super().place_market(**kw)
        link = res["order_link_id"] if res else f"unfilled-{len(self.sent_order_links)}"
        self.sent_order_links.append((kw["product"], kw["symbol"], link))
        if res:
            self.filled[link] = res["filled_qty"]
        return res

    def read_order(self, symbol, order_link_id, product="linear"):
        if self.read_boom:
            raise RuntimeError("venue read failed")
        if self.missing:
            return None
        return {"filled_qty": self.filled.get(order_link_id, 0.0),
                "resting": self.resting, "status": "Filled"}

    def get_order_executions(self, symbol, *, order_link_id="", order_id="",
                             product="linear"):
        q = self.filled.get(order_link_id, 0.0) - self.exec_shortfall
        return [{"execQty": str(q)}] if q > 0 else []

    def get_perp_position(self, symbol):
        self.position_reads += 1
        return super().get_perp_position(symbol)


def run(broker, **kw):
    kw.setdefault("arm", True)
    kw.setdefault("notional", 100.0)
    kw.setdefault("venue", "testnet")
    return D.run_drill(broker=broker, **kw)


# ---------------------------------------------------------------------------
# (b) the cost of the round trip, from the booked fills
# ---------------------------------------------------------------------------

def fill(side, qty, price, fee, ref, status="confirmed", product="linear"):
    return {"ref": ref, "ms": 1, "product": product, "side": side, "qty": qty,
            "price": price, "fee_usdt": fee, "fee_btc": 0.0,
            "fee_status": status, "fee_currencies": ["USDT"]}


class TestRoundTripSummary:
    def test_a_closed_pair_states_price_fee_and_the_perp_pnl(self):
        s = D.round_trip_summary([
            fill("Sell", 0.001, 80620.4, 0.04434122, "o"),
            fill("Buy", 0.001, 80650.0, 0.04435, "c")])
        assert s["closed"] is True and s["fees_confirmed"] is True
        assert s["entry_price"] == pytest.approx(80620.4)
        assert s["exit_price"] == pytest.approx(80650.0)
        assert s["exit_fee_usdt"] == pytest.approx(0.04435)
        assert s["perp_gross_usdt"] == pytest.approx((80620.4 - 80650.0) * 0.001)
        assert s["fees_usdt"] == pytest.approx(0.04434122 + 0.04435)
        assert s["perp_net_before_funding_usdt"] == pytest.approx(
            -0.0296 - 0.04434122 - 0.04435)
        assert s["price_move_bps"] == pytest.approx((80650.0 / 80620.4 - 1) * 1e4)
        assert s["fee_bps_of_entry_notional"] == pytest.approx(
            (0.04434122 + 0.04435) / (80620.4 * 0.001) * 1e4)
        assert s["exit_order_link_ids"] == ["c"]
        assert "spot" in s["scope"] and "funding" in s["scope"]

    def test_no_close_means_no_exit_figures_not_zeros(self):
        s = D.round_trip_summary([fill("Sell", 0.001, 80000.0, 0.04, "o")])
        assert s["closed"] is False
        assert s["exit_price"] is None and s["perp_gross_usdt"] is None
        assert s["perp_net_before_funding_usdt"] is None

    def test_a_partial_close_is_not_closed(self):
        s = D.round_trip_summary([fill("Sell", 0.002, 80000.0, 0.08, "o"),
                                  fill("Buy", 0.001, 80000.0, 0.04, "c")])
        assert s["closed"] is False
        assert s["perp_gross_usdt"] == pytest.approx(0.0)

    def test_two_entry_fills_are_volume_weighted(self):
        s = D.round_trip_summary([fill("Sell", 0.001, 80000.0, 0.0, "a"),
                                  fill("Sell", 0.003, 80400.0, 0.0, "b")])
        assert s["entry_price"] == pytest.approx(80300.0)

    def test_spot_fills_are_not_the_perp_leg(self):
        s = D.round_trip_summary([fill("Buy", 1.0, 1.0, 9.0, "s", product="spot")])
        assert s["entry_qty"] == 0 and s["fees_usdt"] == 0

    def test_an_unconfirmed_fee_is_flagged_not_trusted(self):
        s = D.round_trip_summary([fill("Sell", 0.001, 80000.0, 0.0, "o",
                                       status="unknown")])
        assert s["fees_confirmed"] is False

    def test_nothing_in_nothing_out(self):
        s = D.round_trip_summary([])
        assert s["fees_confirmed"] is False and s["closed"] is False
        assert s["entry_price"] is None


class Interrupting(HoldVenue):
    def get_mark(self, symbol):
        if self.opened_ms is not None and not self.closed:
            raise KeyboardInterrupt("signal 15")
        return super().get_mark(symbol)


class TestAnAbortStatesItsOwnCost:
    def test_abort_unwind_carries_the_exit_price_and_fee(self, clock, tmp_path):
        venue = Interrupting(clock)
        out = str(tmp_path / "d.json")
        with pytest.raises(KeyboardInterrupt) as caught:
            D.run_drill(broker=venue, arm=True, notional=100.0, venue="testnet",
                        hold=_plan(clock), out=out)
        abort = next(s for s in json.load(open(out))["stages"]
                     if s["stage"] == "abort_unwind")
        assert abort["ok"] is True
        ev = abort["evidence"]
        assert ev["exit_qty"] == pytest.approx(QTY)
        assert ev["exit_price"] == pytest.approx(MARK)
        assert ev["exit_fee_usdt"] == pytest.approx(FEE)
        assert len(ev["exit_order_link_ids"]) == 1
        trip = caught.value.drill_report["round_trip"]
        assert trip["closed"] is True
        assert trip["fees_usdt"] == pytest.approx(2 * FEE)
        # the fake venue states a bare number with no currency: said, not hidden
        assert trip["fees_confirmed"] is False

    def test_a_clean_unwind_stage_carries_it_too(self):
        report = run(Venue())
        unwind = next(s for s in report["stages"] if s["stage"] == "unwind")
        assert unwind["evidence"]["exit_price"] == pytest.approx(MARK)
        assert unwind["evidence"]["exit_fee_usdt"] == pytest.approx(FEE)
        assert report["round_trip"]["perp_gross_usdt"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# (a) interrupts
# ---------------------------------------------------------------------------

class TestAnInterruptIsAnOutcomeNotATraceback:
    def test_the_report_is_attached_flagged_and_written(self, clock, tmp_path):
        venue = Interrupting(clock)
        out = str(tmp_path / "d.json")
        with pytest.raises(KeyboardInterrupt) as caught:
            D.run_drill(broker=venue, arm=True, notional=100.0, venue="testnet",
                        hold=_plan(clock), out=out)
        report = caught.value.drill_report
        assert report["interrupted"] is True and report["verdict"] == "FAILED"
        assert report["still_open"] is False
        assert venue.perp_qty == pytest.approx(0.0)
        assert json.load(open(out))["interrupted"] is True
        assert [s["stage"] for s in report["stages"]
                if s["stage"] in ("interrupted", "abort_unwind")] == [
            "interrupted", "abort_unwind"]

    def test_exit_codes(self):
        flat = {"verdict": "FAILED", "interrupted": True, "still_open": False}
        assert D.drill_exit_code(flat, hold=True) == 130
        assert D.drill_exit_code(flat, hold=False) == 130
        assert D.drill_exit_code(dict(flat, still_open=True), hold=True) == 1

    def _stub_main(self, monkeypatch, venue, clock):
        cfg = types.SimpleNamespace(BYBIT_VENUE="testnet", USE_TESTNET=True,
                                    PAPER_TRADING=False, LIVE_TRADING_ACK="",
                                    STATE_DB_PATH="")
        bot = types.SimpleNamespace(cfg=cfg, carry=types.SimpleNamespace(broker=venue))
        monkeypatch.setattr(D, "_load_bot", lambda *a, **k: bot)
        monkeypatch.setattr(D.InterruptGuard, "install", lambda self: None)
        monkeypatch.setattr(D.HoldPlan, "sleep",
                            lambda s: clock.__setitem__(0, clock[0] + s),
                            raising=False)

    def test_main_prints_a_sentence_and_exits_130_with_no_traceback(
            self, monkeypatch, clock, tmp_path, capsys):
        venue = Interrupting(clock)
        self._stub_main(monkeypatch, venue, clock)
        out, led = str(tmp_path / "d.json"), str(tmp_path / "l.json")
        code = D.main(["--arm", "--hold-for-settlement", "--out", out,
                       "--cash-ledger", led, "--poll-seconds", "1"])
        text = capsys.readouterr()
        assert code == 130
        assert "Traceback" not in text.err and "Traceback" not in text.out
        assert "INTERRUPTED (signal 15)" in text.err
        assert "ROUND TRIP" in text.out
        assert json.load(open(out))["interrupted"] is True
        assert venue.perp_qty == pytest.approx(0.0)

    def test_main_with_the_book_still_open_exits_1(self, monkeypatch, clock,
                                                   tmp_path, capsys):
        class Stuck(Interrupting):
            unwind_fills = False
        venue = Stuck(clock, unwind_fills=False)
        self._stub_main(monkeypatch, venue, clock)
        code = D.main(["--arm", "--hold-for-settlement", "--poll-seconds", "1",
                       "--out", str(tmp_path / "d.json"),
                       "--cash-ledger", str(tmp_path / "l.json")])
        assert code == 1
        assert "HUMAN MUST ACT" in capsys.readouterr().out

    def test_an_interrupt_before_any_order_is_a_sentence_too(
            self, monkeypatch, clock, capsys):
        class Early(HoldVenue):
            def get_mark(self, symbol):
                raise KeyboardInterrupt("signal 2")
        self._stub_main(monkeypatch, Early(clock), clock)
        code = D.main(["--arm", "--hold-for-settlement", "--poll-seconds", "1",
                       "--out", "/tmp/_unused_d.json", "--cash-ledger", "/tmp/_u.json"])
        assert code == 130
        assert "before the drill produced a report" in capsys.readouterr().err


class TestTheInterruptGuard:
    def test_first_signal_raises_the_rest_are_ignored(self, capsys):
        g = D.InterruptGuard()
        with pytest.raises(KeyboardInterrupt, match="signal 15"):
            g.handler(15, None)
        g.handler(15, None)                    # during recovery: must not raise
        g.handler(2, None)
        err = capsys.readouterr().err
        assert err.count("ignored") == 2

    def test_it_covers_sigint_sigterm_and_sighup(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(signal, "signal", lambda sig, h: seen.setdefault(sig, h))
        D.InterruptGuard().install()
        assert {signal.SIGINT, signal.SIGTERM, signal.SIGHUP} <= set(seen)


# ---------------------------------------------------------------------------
# (c) the final venue state
# ---------------------------------------------------------------------------

class TestFinalVenueStateCapture:
    def _opened(self, venue):
        venue.place_market(symbol="BTCUSDT", side="Sell", qty=QTY, product="linear")
        return venue

    def test_a_complete_capture_reads_each_order_its_fills_and_the_position(self):
        v = CapVenue()
        self._opened(v)
        v.place_market(symbol="BTCUSDT", side="Buy", qty=QTY, product="linear")
        got = D.capture_final_venue_state(v)
        assert got["complete"] is True and got["orders"] == 2
        assert got["reads"] == 2 * 2 + 1 and v.position_reads == 1

    @pytest.mark.parametrize("attr,needle", [
        ("missing", "returned no order"), ("resting", "not a final state"),
        ("read_boom", "venue read failed")])
    def test_any_unreadable_order_is_a_failure(self, attr, needle):
        v = self._opened(CapVenue())
        setattr(v, attr, True)
        got = D.capture_final_venue_state(v)
        assert got["complete"] is False
        assert needle in got["failures"][0]

    def test_execution_rows_that_do_not_add_up_are_retried_then_failed(self):
        v = self._opened(CapVenue())
        v.exec_shortfall = 0.0005
        sleeps = []
        got = D.capture_final_venue_state(v, sleep=sleeps.append)
        assert got["complete"] is False
        assert "execution rows add up to" in got["failures"][0]
        assert len(sleeps) == D.FINAL_READ_TRIES - 1

    def test_a_lagging_execution_list_that_catches_up_is_accepted(self):
        v = self._opened(CapVenue())
        v.exec_shortfall = 0.0005
        calls = []

        def catch_up(_s):
            calls.append(1)
            v.exec_shortfall = 0.0
        got = D.capture_final_venue_state(v, sleep=catch_up)
        assert got["complete"] is True and len(calls) == 1

    def test_an_unreadable_position_is_a_failure(self):
        v = self._opened(CapVenue())
        v.get_perp_position = lambda s: (_ for _ in ()).throw(RuntimeError("down"))
        got = D.capture_final_venue_state(v)
        assert got["complete"] is False
        assert any(f.startswith("position:") for f in got["failures"])

    def test_a_broker_that_cannot_read_executions_fails_closed(self):
        class NoReads(Venue):
            sent_order_links = [("linear", "BTCUSDT", "x")]
        got = D.capture_final_venue_state(NoReads())
        assert got["complete"] is False

    def test_an_order_that_filled_nothing_needs_no_execution_rows(self):
        v = CapVenue()
        v.sent_order_links.append(("linear", "BTCUSDT", "never-filled"))
        assert D.capture_final_venue_state(v)["complete"] is True

    def test_spot_orders_are_not_read_here(self):
        v = CapVenue()
        v.sent_order_links.append(("spot", "BTCUSDT", "s"))
        assert D.capture_final_venue_state(v)["orders"] == 0


class TestTheCaptureIsWiredIntoTheDrill:
    def test_after_a_pass_the_stage_and_the_report_say_so(self):
        report = run(CapVenue(), capture_final=True)
        assert report["verdict"] == "PASSED"
        stage = next(s for s in report["stages"] if s["stage"] == "venue_final_state")
        assert stage["ok"] is True
        assert report["evidence_capture"]["requested"] is True
        assert report["evidence_capture"]["complete"] is True
        assert D.drill_exit_code(report, hold=False) == 0

    def test_it_is_off_by_default(self):
        report = run(CapVenue())
        assert "evidence_capture" not in report
        assert all(s["stage"] != "venue_final_state" for s in report["stages"])

    def test_an_incomplete_capture_does_not_change_the_verdict_but_never_exits_0(self):
        v = CapVenue()
        v.missing = True
        report = run(v, capture_final=True)
        assert report["verdict"] == "PASSED"
        assert report["evidence_capture"]["complete"] is False
        assert D.drill_exit_code(report, hold=False) == 6

    def test_it_runs_after_a_failed_stage_too(self):
        v = CapVenue(unwind_fills=False)
        report = run(v, capture_final=True)
        assert report["verdict"] == "FAILED"
        assert report["evidence_capture"]["orders"] >= 1

    def test_it_runs_after_an_interrupt(self, clock, tmp_path):
        class Both(Interrupting, CapVenue):
            pass
        venue = Both(clock)
        with pytest.raises(KeyboardInterrupt) as caught:
            D.run_drill(broker=venue, arm=True, notional=100.0, venue="testnet",
                        hold=_plan(clock), capture_final=True)
        capture = caught.value.drill_report["evidence_capture"]
        assert capture["complete"] is True and capture["orders"] == 2

    def test_a_broker_that_sent_nothing_has_nothing_to_capture(self):
        report = run(CapVenue(perp_fills=False), capture_final=True)
        assert report["verdict"] == "FAILED"
        assert "evidence_capture" not in report

    def test_the_flag_reaches_run_drill_from_the_cli(self, monkeypatch, tmp_path):
        seen = {}
        cfg = types.SimpleNamespace(BYBIT_VENUE="testnet", USE_TESTNET=True,
                                    PAPER_TRADING=False, LIVE_TRADING_ACK="",
                                    STATE_DB_PATH="")
        bot = types.SimpleNamespace(cfg=cfg, carry=types.SimpleNamespace(
            broker=CapVenue()))
        monkeypatch.setattr(D, "_load_bot", lambda *a, **k: bot)
        monkeypatch.setattr(D.InterruptGuard, "install", lambda self: None)
        real = D.run_drill
        monkeypatch.setattr(D, "run_drill", lambda **kw: (
            seen.update(kw), real(**kw))[1])
        D.main(["--arm", "--evidence-path", str(tmp_path / "e.jsonl"),
                "--out", str(tmp_path / "o.json")])
        assert seen["capture_final"] is True


# ---------------------------------------------------------------------------
# the claims the report carries, and what the reconciler makes of them
# ---------------------------------------------------------------------------

class TestTheReportCarriesItsClaims:
    def test_legs_are_listed_with_link_ids_and_reduce_only_on_the_close(self):
        report = run(CapVenue())
        claims = report["claims"]
        assert claims["schema"] == "drill_claims/1"
        o, c = claims["carry_leg_claims"]
        assert (o["kind"], o["side"], o["reduce_only"]) == ("perp_open", "Sell", False)
        assert (c["kind"], c["side"], c["reduce_only"]) == ("perp_close", "Buy", True)
        assert o["source"] == "testnet_claim" and o["environment"] == "testnet"
        assert o["order_link_id"] and o["order_link_id"] != c["order_link_id"]
        assert o["qty"] == pytest.approx(QTY) and o["price"] == pytest.approx(MARK)
        assert o["fee"] == pytest.approx(FEE)

    def test_an_unknown_venue_is_a_local_record(self):
        report = run(CapVenue(), venue="unknown")
        leg = report["claims"]["carry_leg_claims"][0]
        assert (leg["source"], leg["environment"]) == ("local", "unknown")

    def test_funding_claims_come_from_rows_the_venue_paid(self, clock, tmp_path):
        venue = HoldVenue(clock)
        report = D.run_drill(broker=venue, arm=True, notional=100.0,
                             venue="testnet", hold=_plan(clock),
                             cash_ledger=str(tmp_path / "l.json"))
        (f,) = report["claims"]["funding_claims"]
        assert f["venue_id"] == "S1" and f["amount"] == pytest.approx(4.25)
        assert f["size"] == pytest.approx(-0.001) and f["source"] == "testnet_claim"

    def test_a_preflight_only_report_has_no_claims(self):
        report = run(CapVenue(), arm=False)
        assert "claims" not in report and "round_trip" not in report

    def test_the_fake_venues_unlabelled_fee_can_never_reconcile(self):
        """A bare fee number has no currency evidence; the claim says so and the
        reconciler refuses to upgrade it, whatever the capture holds."""
        report = run(CapVenue())
        legs = report["claims"]["carry_leg_claims"]
        assert legs[0]["fee_status"] == "unlabelled"
        verdict = R.reconcile_carry_legs(legs, [], now_ms=2_000_000_000_000)
        assert all("FEE_NOT_CONFIRMED" in v["issues"] and not v["resolved"]
                   for v in verdict)


# ---------------------------------------------------------------------------
# the broker's new read-only methods
# ---------------------------------------------------------------------------

class ExecClient(FakeClient):
    def __init__(self, pages, **kw):
        super().__init__(**kw)
        self.pages = pages

    def _request(self, method, endpoint, *, params=None, body=None,
                 signed=False, retries=3):
        if endpoint == "/v5/execution/list":
            self.calls.append((method, endpoint, params, body))
            page = self.pages.pop(0)
            if isinstance(page, Exception):
                raise page
            return page
        return super()._request(method, endpoint, params=params, body=body,
                                signed=signed, retries=retries)


def page(rows, cursor=""):
    return envelope({"list": rows, "nextPageCursor": cursor})


class TestGetOrderExecutions:
    def test_it_pages_to_the_end_with_the_linear_category(self):
        c = ExecClient([page([{"execId": "1"}], "cur1"), page([{"execId": "2"}])])
        rows = make_broker(c).get_order_executions("BTCUSDT", order_link_id="L")
        assert [r["execId"] for r in rows] == ["1", "2"]
        first, second = [x for x in c.calls if x[1] == "/v5/execution/list"]
        assert first[0] == "GET" and first[2]["category"] == LINEAR
        assert first[2]["orderLinkId"] == "L" and "cursor" not in first[2]
        assert second[2]["cursor"] == "cur1"

    def test_by_order_id_it_filters_on_the_order_id(self):
        c = ExecClient([page([])])
        make_broker(c).get_order_executions("BTCUSDT", order_id="O")
        params = c.calls[-1][2]
        assert params["orderId"] == "O" and "orderLinkId" not in params

    def test_an_empty_list_is_an_answer_a_missing_list_is_not(self):
        assert make_broker(ExecClient([page([])])).get_order_executions(
            "BTCUSDT", order_link_id="L") == []
        with pytest.raises(PairIncident, match="no list"):
            make_broker(ExecClient([envelope({})])).get_order_executions(
                "BTCUSDT", order_link_id="L")

    def test_a_repeated_cursor_and_the_page_cap_never_look_complete(self):
        with pytest.raises(PairIncident, match="cursor repeated"):
            make_broker(ExecClient([page([], "a"), page([], "a")])
                        ).get_order_executions("BTCUSDT", order_link_id="L")
        with pytest.raises(PairIncident, match="page cap"):
            make_broker(ExecClient([page([], f"c{i}") for i in range(30)])
                        ).get_order_executions("BTCUSDT", order_link_id="L")

    def test_it_needs_an_id_and_a_known_product(self):
        b = make_broker(ExecClient([]))
        with pytest.raises(PairIncident):
            b.get_order_executions("BTCUSDT")
        with pytest.raises(ValueError):
            b.get_order_executions("BTCUSDT", order_link_id="L", product="option")

    def test_a_transport_failure_propagates(self):
        with pytest.raises(RuntimeError):
            make_broker(ExecClient([RuntimeError("down")])).get_order_executions(
                "BTCUSDT", order_link_id="L")


class TestSentOrderLinks:
    def test_the_link_is_recorded_before_the_request_goes_out(self):
        class Dies(FakeClient):
            def _request(self, method, endpoint, **kw):
                if endpoint == "/v5/order/create":
                    raise ConnectionError("reset")
                return super()._request(method, endpoint, **kw)
        b = make_broker(Dies())
        with pytest.raises(Exception):
            b.place_market(symbol="BTCUSDT", side="Sell", qty=0.001, product=LINEAR)
        assert len(b.sent_order_links) == 1
        product, symbol, link = b.sent_order_links[0]
        assert (product, symbol) == (LINEAR, "BTCUSDT") and link

    def test_each_order_adds_one(self):
        b = make_broker(FakeClient())
        b.place_market(symbol="BTCUSDT", side="Sell", qty=0.001, product=LINEAR)
        b.place_market(symbol="BTCUSDT", side="Buy", qty=0.001, product=LINEAR)
        assert len(b.sent_order_links) == 2
        assert b.sent_order_links[0][2] != b.sent_order_links[1][2]

    def test_read_order_is_the_venues_own_record(self):
        b = make_broker(FakeClient())
        state = b.read_order("BTCUSDT", "L1", LINEAR)
        assert state["order_link_id"] == "L1" and state["filled_qty"] == 0.5
        assert state["resting"] is False


class TestLedgerBrokerKeepsAnIndexOfItsFills:
    def test_price_link_and_fee_currency_survive_booking(self):
        import ledger as L
        journal = L.Journal()
        inner = types.SimpleNamespace(place_market=lambda **kw: {
            "filled_qty": 0.001, "avg_price": 80000.0, "order_link_id": "LNK",
            "fee": 0.044, "fee_detail": {"known": True, "source": "t", "note": "",
                                         "components": [{"amount": 0.044,
                                                         "currency": "USDT"}]}})
        b = L.LedgerBroker(inner, journal, ms=lambda: 1)
        b.place_market(symbol="BTCUSDT", side="Sell", qty=0.001, product="linear")
        (f,) = b.fills
        assert f["ref"] == "LNK" and f["price"] == 80000.0 and f["qty"] == 0.001
        assert f["fee_usdt"] == pytest.approx(0.044) and f["fee_status"] == "confirmed"
        assert f["fee_currencies"] == ["USDT"] and f["side"] == "Sell"

    def test_a_booking_that_fails_is_not_indexed(self):
        import ledger as L
        inner = types.SimpleNamespace(place_market=lambda **kw: {
            "filled_qty": 0.001, "avg_price": 80000.0, "order_link_id": "X"})
        b = L.LedgerBroker(inner, L.Journal(), ms=lambda: 1)
        with pytest.raises(Exception):          # a fill with no side cannot be booked
            b.place_market(symbol="BTCUSDT", side="", qty=0.001, product="linear")
        assert b.fills == []
