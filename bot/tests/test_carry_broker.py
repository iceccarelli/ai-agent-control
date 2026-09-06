"""The venue adapter places pairs, survives retries, and shouts on mismatch."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from carry_broker import (CarryBroker, LegFill, PairIncident,  # noqa: E402
                          LINEAR, SPOT)


class Boom(Exception):
    def __init__(self, ret_code):
        super().__init__(f"retCode {ret_code}")
        self.ret_code = ret_code


class FakeClient:
    """A venue. Records calls; misbehaves when told."""

    def __init__(self, *, mark=100_000.0, im=5.0, mm=1.0,
                 spot_qty=1.0, perp_qty=1.0, raise_on_create=None):
        self.mark = mark
        self.im = im
        self.mm = mm
        self.spot_qty = spot_qty
        self.perp_qty = perp_qty
        self.raise_on_create = raise_on_create
        self.calls = []
        self.created = []

    def _request(self, method, endpoint, *, params=None, body=None,
                 signed=False, retries=3):
        self.calls.append((method, endpoint, params, body))
        if endpoint == "/v5/order/create":
            self.created.append(body)
            if self.raise_on_create is not None:
                raise Boom(self.raise_on_create)
            return {"orderLinkId": body["orderLinkId"],
                    "cumExecQty": body["qty"], "avgPrice": str(self.mark)}
        if endpoint == "/v5/order/realtime":
            return {"list": [{"cumExecQty": "0.5", "avgPrice": str(self.mark),
                              "orderLinkId": params.get("orderLinkId")}]}
        if endpoint == "/v5/market/tickers":
            return {"list": [{"markPrice": str(self.mark)}]}
        if endpoint == "/v5/position/list":
            return {"list": [{"positionIM": str(self.im),
                              "positionMM": str(self.mm),
                              "size": str(self.perp_qty)}]}
        if endpoint == "/v5/account/wallet-balance":
            return {"list": [{"coin": [{"walletBalance": str(self.spot_qty)}]}]}
        return {}


def broker(client, **kw):
    seq = kw.pop("sequence_source", lambda product, symbol, purpose: 7)
    return CarryBroker(client=client, sequence_source=seq, **kw)


class TestItPlacesBothProducts:
    @pytest.mark.parametrize("product", [SPOT, LINEAR])
    def test_a_leg_routes_to_its_own_category(self, product):
        client = FakeClient()
        result = broker(client).place_market(
            symbol="BTCUSDT", side="Buy", qty=0.5, product=product)
        assert result["filled_qty"] == pytest.approx(0.5)
        assert client.created[0]["category"] == product

    def test_an_unknown_product_is_refused_not_defaulted(self):
        """Routing spot to the linear endpoint is a naked short."""
        with pytest.raises(ValueError):
            broker(FakeClient()).place_market(
                symbol="BTCUSDT", side="Buy", qty=0.5, product="inverse")

    @pytest.mark.parametrize("qty", [0.0, -1.0, float("nan"), float("inf")])
    def test_an_unusable_quantity_places_nothing(self, qty):
        client = FakeClient()
        assert broker(client).place_market(
            symbol="BTCUSDT", side="Buy", qty=qty, product=SPOT) is None
        assert not client.created

    def test_quantity_is_never_scientific_notation(self):
        """Bybit rejects `1e-05`."""
        client = FakeClient()
        broker(client).place_market(symbol="BTCUSDT", side="Buy",
                                    qty=0.00001, product=SPOT)
        assert "e" not in client.created[0]["qty"].lower()


class TestARetryCannotDoubleALeg:
    def test_the_link_id_is_deterministic(self):
        a, b = FakeClient(), FakeClient()
        for client in (a, b):
            broker(client).place_market(symbol="BTCUSDT", side="Buy",
                                        qty=0.5, product=SPOT)
        assert a.created[0]["orderLinkId"] == b.created[0]["orderLinkId"], (
            "two processes with the same sequence produced different ids; a "
            "retry after a timeout would open a SECOND leg")

    def test_the_two_legs_of_a_pair_get_different_ids(self):
        client = FakeClient()
        api = broker(client)
        api.place_market(symbol="BTCUSDT", side="Buy", qty=0.5, product=SPOT)
        api.place_market(symbol="BTCUSDT", side="Sell", qty=0.5, product=LINEAR)
        assert client.created[0]["orderLinkId"] != client.created[1]["orderLinkId"]

    @pytest.mark.parametrize("ret_code", [110072, 170130])
    def test_a_duplicate_id_is_resolved_by_query_not_recorded_as_failure(
            self, ret_code):
        """The duplicate means THE ORDER EXISTS. Recording a rejection here
        leaves a real leg the ledger does not know about."""
        client = FakeClient(raise_on_create=ret_code)
        result = broker(client).place_market(
            symbol="BTCUSDT", side="Buy", qty=0.5, product=SPOT)
        assert result is not None, "a landed order was reported as a failure"
        assert result["filled_qty"] == pytest.approx(0.5)
        assert any(c[1] == "/v5/order/realtime" for c in client.calls)

    def test_an_unreadable_duplicate_raises_an_incident(self):
        client = FakeClient(raise_on_create=110072)
        original = client._request

        def no_readback(method, endpoint, **kw):
            if endpoint == "/v5/order/realtime":
                return {"list": []}
            return original(method, endpoint, **kw)

        client._request = no_readback
        with pytest.raises(PairIncident):
            broker(client).place_market(symbol="BTCUSDT", side="Buy",
                                        qty=0.5, product=SPOT)

    def test_an_ordinary_failure_returns_none_rather_than_raising(self):
        client = FakeClient(raise_on_create=10001)
        assert broker(client).place_market(
            symbol="BTCUSDT", side="Buy", qty=0.5, product=SPOT) is None


class TestMarginIsNeverAssumed:
    def test_headroom_is_im_over_mm(self):
        assert broker(FakeClient(im=5.0, mm=1.0)).get_margin_multiple(
            "BTCUSDT") == pytest.approx(5.0)

    def test_a_missing_position_row_raises(self):
        client = FakeClient()
        client._request = lambda *a, **k: {"list": []}
        with pytest.raises(PairIncident):
            broker(client).get_margin_multiple("BTCUSDT")

    def test_zero_maintenance_margin_raises_rather_than_dividing(self):
        with pytest.raises(PairIncident):
            broker(FakeClient(mm=0.0)).get_margin_multiple("BTCUSDT")

    def test_there_is_no_comfortable_default(self):
        """A swallowed exception here would be the worst line in the file.

        Checked by AST so the docstring explaining the rule does not trip it —
        the same trick market_data uses for its import guard.
        """
        import ast
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "carry_broker.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        target = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef)
                      and n.name == "get_margin_multiple")
        handlers = [n for n in ast.walk(target)
                    if isinstance(n, ast.ExceptHandler)]
        assert not handlers, (
            "get_margin_multiple catches an exception; an unreadable margin "
            "must propagate so CarryEngine halts on it")


class TestMarkMustBeUsable:
    def test_a_good_mark_comes_back(self):
        assert broker(FakeClient(mark=99_000.0)).get_mark("BTCUSDT") == \
            pytest.approx(99_000.0)

    def test_a_zero_mark_raises(self):
        with pytest.raises(PairIncident):
            broker(FakeClient(mark=0.0)).get_mark("BTCUSDT")


class TestReconciliationTreatsMismatchAsAnIncident:
    def test_matched_legs_report_paired(self):
        report = broker(FakeClient(spot_qty=1.0, perp_qty=1.0)).reconcile_pair(
            spot_symbol="BTCUSDT", perp_symbol="BTCUSDT", expected_qty=1.0)
        assert report["verdict"] == "PAIRED"
        assert report["naked_side"] == ""

    def test_a_missing_perp_leg_names_the_naked_side(self):
        report = broker(FakeClient(spot_qty=1.0, perp_qty=0.5)).reconcile_pair(
            spot_symbol="BTCUSDT", perp_symbol="BTCUSDT", expected_qty=1.0)
        assert report["verdict"] == "INCIDENT"
        assert report["naked_side"] == "spot"

    def test_a_missing_spot_leg_names_the_naked_side(self):
        report = broker(FakeClient(spot_qty=0.4, perp_qty=1.0)).reconcile_pair(
            spot_symbol="BTCUSDT", perp_symbol="BTCUSDT", expected_qty=1.0)
        assert report["naked_side"] == "perp"

    def test_legs_that_match_each_other_but_not_the_book_is_an_incident(self):
        """Someone traded by hand, or a fill was missed. Both are incidents."""
        report = broker(FakeClient(spot_qty=2.0, perp_qty=2.0)).reconcile_pair(
            spot_symbol="BTCUSDT", perp_symbol="BTCUSDT", expected_qty=1.0)
        assert report["legs_match_each_other"] is True
        assert report["venue_agrees_with_book"] is False
        assert report["verdict"] == "INCIDENT"

    def test_reconciliation_never_writes(self):
        client = FakeClient(spot_qty=2.0, perp_qty=0.5)
        broker(client).reconcile_pair(spot_symbol="BTCUSDT",
                                      perp_symbol="BTCUSDT", expected_qty=1.0)
        assert all(method == "GET" for method, *_ in client.calls), (
            "reconciliation wrote to the venue; it may only observe")


class TestItDecidesNothing:
    def test_the_module_has_no_funding_or_direction_logic(self):
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "carry_broker.py"), encoding="utf-8") as fh:
            body = fh.read()
        for banned in ("FUND_ABS", "def should_", "signal_type"):
            assert banned not in body

    def test_no_module_assigns_live_authorized(self):
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "carry_broker.py"), encoding="utf-8") as fh:
            assert "live_authorized" not in fh.read()


class TestItDrivesTheEngine:
    def test_the_engine_opens_a_pair_through_this_adapter(self):
        """End to end: CarryEngine + CarryBroker + a venue."""
        from carry_engine import BookState, CarryEngine
        client = FakeClient(im=5.0, mm=1.0)
        eng = CarryEngine(broker=broker(client), max_notional_usd=100_000.0)
        decision = eng.on_candle(mark=100_000.0, funding_bps=1.0)
        assert decision.acted is True
        assert eng.state is BookState.HEDGED
        assert len(client.created) == 2
        assert {c["category"] for c in client.created} == {SPOT, LINEAR}
        assert {c["side"] for c in client.created} == {"Buy", "Sell"}
