"""The preflight tells the operator what the PRODUCTION entry evaluator says.

Three questions stay separate: could the venue be read (readiness), does the
production entry-cost gate pass (economics), and may an order be sent (the carry
order gate). None of them can stand in for another.

Everything here is offline: a scripted market stands in for the venue, and no
credential is used. Fixtures follow the documented response shapes; they are not
captures of a real account.
"""
from __future__ import annotations

import inspect
import os
import sys
import time
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import carry_costs  # noqa: E402
import carry_engine  # noqa: E402
import main as _main  # noqa: E402
from carry_broker import CarryBroker, CarryOrderRefused  # noqa: E402
from test_carry_broker import FakeClient, envelope  # noqa: E402
from tools import carry_preflight as pf  # noqa: E402

H = 3_600_000
PERP = 82_000.0


class Market(FakeClient):
    """A scripted public market + account. Counts every non-GET request."""

    def __init__(self, *, rate_bps=3.0, n_prints=8, newest_age_h=2.0,
                 basis_bps=2.0, spot_taker=0.001, perp_taker=0.00055,
                 fee_fails=False, perp_qty=0.0, **kw):
        super().__init__(mark=PERP, spot_qty=1.0, perp_qty=perp_qty, **kw)
        self.basis_bps, self.spot_taker, self.perp_taker = (
            basis_bps, spot_taker, perp_taker)
        self.fee_fails = fee_fails
        now = int(time.time() * 1000)
        newest = now - int(newest_age_h * H)
        # newest first, as the venue returns them
        self.prints = [(rate_bps / 1e4, newest - i * 8 * H)
                       for i in range(n_prints)]
        self.non_gets = []

    def _request(self, method, endpoint, *, params=None, body=None,
                 signed=False, retries=3):
        if method != "GET":
            self.non_gets.append((method, endpoint))
        params = params or {}
        if endpoint == "/v5/market/tickers":
            spot = PERP / (1.0 + self.basis_bps / 1e4)
            if params.get("category") == "spot":
                return envelope({"list": [{"lastPrice": str(spot),
                                           "bid1Price": str(spot - 1),
                                           "ask1Price": str(spot + 1)}]})
            return envelope({"list": [{"markPrice": str(PERP),
                                       "lastPrice": str(PERP),
                                       "fundingRate": "0.0001",
                                       "bid1Price": str(PERP - 1),
                                       "ask1Price": str(PERP + 1)}]})
        if endpoint == "/v5/market/funding/history":
            rows = [{"fundingRate": str(r), "fundingRateTimestamp": str(t)}
                    for r, t in self.prints][: int(params.get("limit", 200))]
            return envelope({"list": rows})
        if endpoint == "/v5/account/fee-rate":
            if self.fee_fails:
                raise RuntimeError("fee-rate down")
            taker = (self.spot_taker if params.get("category") == "spot"
                     else self.perp_taker)
            return envelope({"list": [{"makerFeeRate": "0.0002",
                                       "takerFeeRate": str(taker)}]})
        if endpoint == "/v5/account/wallet-balance":
            return envelope({"list": [{"coin": [{
                "coin": "BTC", "walletBalance": str(self.spot_qty),
                "locked": "0"}]}]})
        if endpoint == "/v5/order/realtime":
            return envelope({"list": []})
        return super()._request(method, endpoint, params=params, body=body,
                                signed=signed, retries=retries)


def cfg(paper=False):
    return SimpleNamespace(PAPER_TRADING=paper, USE_TESTNET=True,
                           BYBIT_VENUE="testnet")


def preflight(market, *, mode="overlay", style="taker", borrow=0.0, paper=False):
    broker = CarryBroker(client=market, sequence_source=lambda *a: 0,
                         order_gate=lambda: (False, "preflight is read-only"))
    report = pf.run_preflight(
        broker=broker, client=SimpleNamespace(get_equity=lambda: 9800.0),
        cfg=cfg(paper), spot_symbol="BTCUSDT", perp_symbol="BTCUSDT",
        execution_mode=mode, known_perp_qty=0.0, execution_style=style,
        borrow_apr=borrow)
    return report, broker


class TestItIsTheProductionEvaluator:
    def test_the_engine_decides_through_the_same_method_the_preflight_calls(self):
        src = inspect.getsource(carry_engine.CarryEngine.on_candle)
        assert "entry_economics" in src
        assert "evaluate_entry" not in src          # one path, not a copy
        assert "evaluate_entry" in inspect.getsource(
            carry_engine.CarryEngine.entry_economics)

    def test_the_report_equals_a_direct_call_with_the_actual_history_fees_and_mode(
            self):
        m = Market(rate_bps=3.0)
        report, _ = preflight(m, mode="overlay")
        e = report["entry_economics"]
        direct = carry_costs.evaluate_entry(
            funding_prints_bps=[3.0] * 8, perp=PERP,
            spot=PERP / (1.0 + 2.0 / 1e4), borrow_apr=0.0,
            round_trip_bps=2 * 5.5)                  # overlay: two perp legs
        assert e["state"] == "ENTRY_ECONOMICS_PASS"
        ev = e["evaluator"]
        assert ev["reason"] == direct.reason
        for key, attr in (("net_edge_bps_per_day", "net_edge_bps_per_day"),
                          ("expected_carry_bps_per_day",
                           "expected_carry_bps_per_day"),
                          ("entry_basis_bps", "entry_basis_bps"),
                          ("basis_budget_bps", "basis_budget_bps")):
            assert ev[key] == pytest.approx(getattr(direct, attr), rel=1e-9)
        assert e["round_trip_bps"] == pytest.approx(11.0)
        assert e["inputs"]["assumed_hold_days"] == carry_costs.ASSUMED_HOLD_DAYS

    def test_the_evaluator_is_really_consulted_not_re_derived(self, monkeypatch):
        sentinel = carry_costs.CarryVerdict(
            False, "SENTINEL_FROM_THE_PRODUCTION_EVALUATOR",
            net_edge_bps_per_day=-123.0)
        seen = {}

        def spy(**kw):
            seen.update(kw)
            return sentinel
        monkeypatch.setattr(carry_costs, "evaluate_entry", spy)
        report, _ = preflight(Market())
        e = report["entry_economics"]
        assert e["state"] == "ENTRY_ECONOMICS_REFUSED"
        assert e["reason"] == "SENTINEL_FROM_THE_PRODUCTION_EVALUATOR"
        assert e["evaluator"]["net_edge_bps_per_day"] == -123.0
        # and it was handed the actual history, fee-derived round trip and mode
        assert seen["funding_prints_bps"] == pytest.approx([3.0] * 8)
        assert seen["round_trip_bps"] == pytest.approx(11.0)
        assert seen["borrow_apr"] == 0.0


class TestOverlayAndAcquireAreEconomicallyDistinct:
    def test_same_market_overlay_passes_acquire_is_refused_on_cost(self):
        # 0.3 bps per print: ~0.9 bps/day of carry. Overlay pays two perp legs
        # (11 bps), acquire pays the spot round trip too (31 bps).
        overlay, _ = preflight(Market(rate_bps=0.3), mode="overlay")
        acquire, _ = preflight(Market(rate_bps=0.3), mode="acquire")
        o, a = overlay["entry_economics"], acquire["entry_economics"]
        assert o["state"] == "ENTRY_ECONOMICS_PASS"
        assert a["state"] == "ENTRY_ECONOMICS_REFUSED"
        assert a["reason"] == "CARRY_BELOW_COST_OF_CAPITAL"
        assert o["round_trip_bps"] == pytest.approx(11.0)
        assert a["round_trip_bps"] == pytest.approx(31.0)

    def test_the_account_specific_fee_tier_moves_the_round_trip(self):
        report, _ = preflight(Market(perp_taker=0.0003), mode="overlay")
        assert report["entry_economics"]["round_trip_bps"] == pytest.approx(6.0)


class TestMissingOrStaleInputsRefuseEntry:
    def test_too_little_funding_history_is_refused_by_the_evaluator(self):
        report, _ = preflight(Market(n_prints=2))
        e = report["entry_economics"]
        assert e["state"] == "ENTRY_ECONOMICS_REFUSED"
        assert e["reason"] == "INSUFFICIENT_FUNDING_HISTORY"
        assert e["history"]["prints"] == 2 and e["history"]["enough_for_ewma"] is False

    def test_no_history_at_all_is_not_a_pass(self):
        report, _ = preflight(Market(n_prints=0))
        assert report["entry_economics"]["state"] != "ENTRY_ECONOMICS_PASS"

    def test_a_stale_last_print_makes_the_market_view_unusable_not_a_pass(self):
        report, _ = preflight(Market(newest_age_h=20.0))
        e = report["entry_economics"]
        assert e["state"] == "ENTRY_ECONOMICS_UNAVAILABLE"
        assert e["market_view"]["usable"] is False
        assert "settlement has been missed" in e["market_view"]["error"]
        assert e["evaluator"] is None            # no verdict from a stale view

    def test_unreadable_fee_rates_are_unavailable_never_assumed(self):
        report, _ = preflight(Market(fee_fails=True))
        e = report["entry_economics"]
        assert e["state"] == "ENTRY_ECONOMICS_UNAVAILABLE"
        assert "FEE_RATES_UNREADABLE" in e["reason"]

    def test_missing_borrow_or_style_configuration_is_unavailable(self):
        report, _ = preflight(Market(), borrow=None)
        assert report["entry_economics"]["state"] == "ENTRY_ECONOMICS_UNAVAILABLE"

    def test_negative_funding_is_a_refusal_with_the_evaluators_reason(self):
        report, _ = preflight(Market(rate_bps=-10.0))
        e = report["entry_economics"]
        assert (e["state"], e["reason"]) == (
            "ENTRY_ECONOMICS_REFUSED", "FUNDING_NOT_POSITIVE")

    def test_history_coverage_and_freshness_are_reported(self):
        e = preflight(Market(newest_age_h=2.0))[0]["entry_economics"]
        h = e["history"]
        assert h["prints"] == 8 and h["span_days"] == pytest.approx(7 * 8 / 24, abs=0.01)
        assert h["newest_age_hours"] == pytest.approx(2.0, abs=0.05)
        assert e["market_view"]["usable"] and e["market_view"]["basis_bps"] > 0
        assert e["market_view"]["observed_age_s"] < 60


class TestTheThreeStatesCannotOverrideEachOther:
    def test_readiness_ok_with_a_refused_entry_is_not_a_trade_signal(self):
        report, _ = preflight(Market(rate_bps=-10.0))
        assert report["verdict"] == "PREFLIGHT_OK"
        assert report["entry_economics"]["state"] == "ENTRY_ECONOMICS_REFUSED"
        assert report["summary"]["readiness"] == "PREFLIGHT_OK"
        assert report["summary"]["entry_economics"].startswith(
            "ENTRY_ECONOMICS_REFUSED")
        assert "not a profitable entry" in " ".join(report["notes"])

    def test_the_exit_code_keeps_readiness_and_economics_apart(self):
        ok_refused, _ = preflight(Market(rate_bps=-10.0))
        assert pf.exit_code(ok_refused, require_economics_pass=False) == 0
        assert pf.exit_code(ok_refused, require_economics_pass=True) == 5
        ok_pass, _ = preflight(Market())
        assert pf.exit_code(ok_pass, require_economics_pass=True) == 0
        blocked, _ = preflight(Market(perp_qty=0.004))
        assert blocked["verdict"] == "PREFLIGHT_BLOCKED"
        assert pf.exit_code(blocked, require_economics_pass=True) == 2

    def test_an_economics_pass_does_not_open_a_closed_order_gate(self):
        report, _ = preflight(Market(), paper=True)
        assert report["entry_economics"]["state"] == "ENTRY_ECONOMICS_PASS"
        auth = report["order_authorization"]
        assert auth["gate_allows_orders"] is False
        assert "PAPER_TRADING=1" in auth["reason"]
        assert report["summary"]["order_gate"].startswith("REFUSES_ORDERS")

    def test_the_reported_gate_IS_the_production_gate(self):
        for paper in (True, False):
            report, _ = preflight(Market(), paper=paper)
            allowed, reason = _main.carry_orders_permitted(cfg(paper))
            assert report["order_authorization"]["gate_allows_orders"] == allowed
            assert report["order_authorization"]["reason"] == reason
        # and build_bot hands the broker this very function, plus the check
        # that the client targets the venue the config names (a strictly
        # stronger gate that delegates to it rather than copying its logic)
        assert "carry_orders_permitted_for(cfg, bot.client)" in \
            inspect.getsource(_main.build_bot)
        assert "carry_orders_permitted(cfg)" in \
            inspect.getsource(_main.carry_orders_permitted_for)


class TestStructurallyReadOnly:
    def test_a_full_preflight_sends_no_non_get_request(self):
        market = Market()
        report, _ = preflight(market, mode="acquire")
        assert report["entry_economics"]["state"] in (
            "ENTRY_ECONOMICS_PASS", "ENTRY_ECONOMICS_REFUSED")
        assert market.non_gets == []

    def test_its_broker_refuses_every_order_even_if_asked(self):
        _, broker = preflight(Market())
        with pytest.raises(CarryOrderRefused):
            broker.place_market(symbol="BTCUSDT", side="Sell", qty=0.001,
                                product="linear")
        with pytest.raises(CarryOrderRefused):
            broker.place_post_only(symbol="BTCUSDT", side="Sell", qty=0.001,
                                   price=PERP, product="linear")

    def test_it_never_runs_the_engines_trading_loop(self):
        import ast
        tree = ast.parse(inspect.getsource(pf.assess_entry_economics).lstrip())
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert "entry_economics" in called
        assert not called & {"on_candle", "_open", "_take", "_fire", "place_market",
                             "place_post_only", "cancel_order"}
