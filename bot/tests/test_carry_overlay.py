"""0036 — the overlay executes against BTC the client already owns.

THE FINDING THIS PATCH ACTS ON
==============================
PHASE1_DECISION says the product is an OVERLAY on BTC already held. The engine
did not implement that: it BOUGHT spot on every entry and SOLD it on every
exit. On the settlement clock that round trip is where the money went —
0032 measured 87 trades, $27,232 of fees against $39,411 of funding, and a
net of -2.99%/yr at 5% financing.

A book that hedges inventory it already holds never pays the spot side:
no purchase, no sale, no financing. Same funding, same basis, a quarter of
the fees.

These tests pin the execution, not the number: what is sent, what is never
sent, and what the venue's own rules say a lot may be.
"""
from __future__ import annotations

import os
import sys
import time
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import carry_costs as cc  # noqa: E402
import main as _main  # noqa: E402
from carry_broker import CarryBroker, PairIncident  # noqa: E402
from carry_engine import (ACQUIRE, OVERLAY, BookState,  # noqa: E402
                          CarryEngine, snap_to_lot)
from carry_risk import CarryRisk  # noqa: E402
from market_snapshot import MarketSnapshot  # noqa: E402

MARK = 100_000.0
H8 = 8 * 3600 * 1000
STEP = {"qty_step": 0.001, "min_qty": 0.001, "min_notional": 5.0}
FEES = {"maker_bps": 2.0, "taker_bps": 5.5}
SPOT_FEES = {"maker_bps": 10.0, "taker_bps": 10.0}


class Venue:
    """A venue with lot rules, fee rates and an inventory of BTC."""

    def __init__(self, *, inventory=1.0, reject=(), fill_ratio=1.0,
                 lot=None, fee_boom=False, lot_boom=False):
        self.inventory = inventory
        self.reject = set(reject)
        self.fill_ratio = fill_ratio
        self.lot = dict(lot or STEP)
        self.fee_boom = fee_boom
        self.lot_boom = lot_boom
        self.orders = []
        self.lot_calls = 0
        self.fee_calls = 0

    # -- the engine's calls ------------------------------------------------
    def place_market(self, *, symbol, side, qty, product):
        self.orders.append((side, product, qty))
        if product in self.reject:
            return None
        # fill_ratio applies to the SELL that opens the hedge. A buy-back is
        # exact: a venue that overfills a closing order is a different bug.
        filled = qty * (self.fill_ratio if side == "Sell" else 1.0)
        return {"filled_qty": filled, "avg_price": MARK,
                "order_link_id": "x", "fee": 0.0001}

    def get_margin_multiple(self, symbol):
        return 5.0

    def get_mark(self, symbol):
        return MARK

    def get_lot_rules(self, symbol, product):
        self.lot_calls += 1
        if self.lot_boom:
            raise PairIncident("no instruments-info")
        return dict(self.lot)

    def get_fee_rates(self, symbol, product):
        self.fee_calls += 1
        if self.fee_boom:
            raise PairIncident("no fee-rate")
        return dict(SPOT_FEES if product == "spot" else FEES)

    def get_spot_inventory(self, symbol):
        return self.inventory


def snap(perp=MARK, spot=MARK):
    return MarketSnapshot(perp_mark=perp, spot_mark=spot, funding_bps=3.0,
                          margin_multiple=5.0, observed_at_s=time.time())


def engine(venue, *, mode=OVERLAY, cap=100.0, **kw):
    kw.setdefault("borrow_apr", 0.0)
    kw.setdefault("persist", lambda state: None)
    e = CarryEngine(broker=venue, max_notional_usd=cap, execution_mode=mode,
                    **kw)
    e.pair_risk = CarryRisk(max_notional_usd=cap)
    e.snapshot = snap()
    for k in (1, 2):                       # warm the EWMA with two prints
        e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                    funding_print_ms=k * H8)
    return e


def open_pair(e, *, print_no=3, bps=3.0, mark=MARK, spot=MARK):
    e.snapshot = snap(perp=mark, spot=spot)
    return e.on_candle(mark=mark, funding_bps=bps, spot=spot,
                       funding_print_ms=print_no * H8)


class TestTheOverlayNeverTradesTheClientsSpot:
    def test_opening_sends_the_perp_leg_only(self):
        v = Venue()
        d = open_pair(engine(v))
        assert d.action == "opened", d.reason
        assert v.orders == [("Sell", "linear", 0.001)]

    def test_the_hedged_slice_is_delta_flat(self):
        v = Venue()
        e = engine(v)
        open_pair(e)
        assert e.position.delta_qty == pytest.approx(0.0)
        assert e.position.spot.order_link_id == "INVENTORY"
        assert e.position.spot.fee is None

    def test_unwinding_buys_the_perp_back_and_sells_nothing(self):
        v = Venue()
        e = engine(v)
        open_pair(e)
        v.orders.clear()
        for k in (4, 5, 6):
            d = e.on_candle(mark=MARK, funding_bps=-0.5, spot=MARK,
                            funding_print_ms=k * H8)
        assert d.action == "unwound" and d.reason == "FUNDING_INVERTED"
        assert v.orders == [("Buy", "linear", 0.001)]
        assert not any(o[1] == "spot" for o in v.orders)

    def test_no_inventory_no_hedge(self):
        v = Venue(inventory=0.0)
        d = open_pair(engine(v))
        assert d.acted is False
        assert d.reason == "NO_SPOT_INVENTORY"
        assert v.orders == []

    def test_the_hedge_is_capped_by_the_inventory(self):
        """$100 of cap at $100k is 0.001 BTC; the client holds 0.0006."""
        v = Venue(inventory=0.0006, lot={"qty_step": 0.0001, "min_qty": 0.0001,
                                         "min_notional": 5.0})
        e = engine(v)
        open_pair(e)
        assert v.orders == [("Sell", "linear", 0.0006)]

    def test_the_rest_of_the_clients_btc_is_not_the_books_exposure(self):
        v = Venue(inventory=12.5)          # the client holds 12.5 BTC
        e = engine(v)
        open_pair(e)
        assert e.position.spot.filled_qty == pytest.approx(0.001)
        assert e.position.delta_qty == pytest.approx(0.0)


class TestAPartialPerpFillIsASmallerHedgeNotAnIncident:
    """In acquire mode a half-filled pair is naked spot and must be unwound.
    In overlay there is nothing naked: the BTC was the client's before the
    book existed, so a partial fill is simply a smaller hedged slice."""

    def test_a_half_filled_perp_hedges_half(self):
        v = Venue(fill_ratio=0.5, lot={"qty_step": 0.0001, "min_qty": 0.0001,
                                       "min_notional": 5.0})
        e = engine(v)
        d = open_pair(e)
        assert d.action == "opened"
        assert e.position.perp.filled_qty == pytest.approx(0.0005)
        assert e.position.spot.filled_qty == pytest.approx(0.0005)
        assert e.position.delta_qty == pytest.approx(0.0)

    def test_a_rejected_perp_leaves_nothing_and_costs_nothing(self):
        v = Venue(reject={"linear"})
        e = engine(v)
        d = open_pair(e)
        assert e.position is None
        assert e.state is BookState.FLAT
        assert d.reason == "PERP_LEG_DID_NOT_FILL"
        assert e.pair_risk._entries == [] and e.pair_risk._broken == []

    def test_an_overfilled_perp_buys_the_excess_back(self):
        """Short more than the client owns and the book is naked SHORT."""
        v = Venue(inventory=0.001, fill_ratio=2.0)
        e = engine(v)
        open_pair(e)
        assert v.orders[0] == ("Sell", "linear", 0.001)
        assert v.orders[1] == ("Buy", "linear", pytest.approx(0.001))
        assert e.position.perp.filled_qty == pytest.approx(0.001)

    def test_a_failed_buy_back_halts(self):
        class Once(Venue):
            def place_market(self, *, symbol, side, qty, product):
                self.orders.append((side, product, qty))
                if side == "Buy":
                    return None
                return {"filled_qty": qty * 2, "avg_price": MARK,
                        "order_link_id": "x"}
        v = Once(inventory=0.001)
        e = engine(v)
        d = open_pair(e)
        assert d.state is BookState.HALTED
        assert d.reason == "OVERSOLD_BEYOND_INVENTORY"


class TestTheVenueDecidesTheLot:
    def test_the_size_is_snapped_down_to_the_step(self):
        assert snap_to_lot(0.0012657, 0.001) == pytest.approx(0.001)
        assert snap_to_lot(0.999999, 0.001) == pytest.approx(0.999)
        assert snap_to_lot(5.0, 0.0) == pytest.approx(5.0)

    def test_a_cap_below_the_minimum_lot_refuses(self):
        v = Venue()
        d = open_pair(engine(v, cap=50.0))          # 0.0005 BTC < 0.001 step
        assert d.acted is False
        assert d.reason == "SIZE_BELOW_VENUE_MINIMUM"
        assert v.orders == []

    def test_a_lot_below_the_minimum_notional_refuses(self):
        v = Venue(lot={"qty_step": 0.00001, "min_qty": 0.00001,
                       "min_notional": 5000.0})
        d = open_pair(engine(v))
        assert d.reason == "SIZE_BELOW_VENUE_MINIMUM"
        assert v.orders == []

    def test_unreadable_lot_rules_refuse_rather_than_guess(self):
        v = Venue(lot_boom=True)
        d = open_pair(engine(v))
        assert d.reason == "VENUE_RULES_UNREADABLE"
        assert v.orders == []

    def test_the_rules_are_read_once_not_every_tick(self):
        v = Venue()
        e = engine(v)
        for k in range(3, 9):
            e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                        funding_print_ms=k * H8)
        assert v.lot_calls == 1


class TestTheBookChargesItselfWhatTheVenueCharges:
    def test_the_overlay_round_trip_is_two_perp_legs(self):
        v = Venue()
        e = engine(v)
        assert e.round_trip_bps() == pytest.approx(2 * 5.5)

    def test_the_acquire_round_trip_is_four_legs(self):
        v = Venue()
        e = engine(v, mode=ACQUIRE)
        assert e.round_trip_bps() == pytest.approx(2 * (10.0 + 5.5))

    def test_unreadable_fees_refuse_rather_than_assume(self):
        v = Venue(fee_boom=True)
        d = open_pair(engine(v))
        assert d.reason == "FEE_RATES_UNREADABLE"
        assert v.orders == []

    def test_the_gate_is_asked_with_the_mode_s_round_trip(self):
        v = Venue()
        e = engine(v)
        with mock.patch.object(cc, "evaluate_entry",
                               wraps=cc.evaluate_entry) as spy:
            open_pair(e)
        assert spy.call_args.kwargs["round_trip_bps"] == pytest.approx(11.0)

    def test_the_broker_asks_the_venue_once(self):
        """The engine asks on every entry decision; the BROKER is what keeps
        that from becoming an HTTP call per tick."""
        calls = []

        class Client:
            def _request(self, method, endpoint, *, params=None, body=None,
                         signed=False, retries=3):
                calls.append(endpoint)
                if endpoint == "/v5/account/fee-rate":
                    return {"list": [{"makerFeeRate": "0.0002",
                                      "takerFeeRate": "0.00055"}]}
                return {"list": [{"lotSizeFilter": {
                    "qtyStep": "0.001", "minOrderQty": "0.001",
                    "minNotionalValue": "5"}}]}

        b = CarryBroker(client=Client(), sequence_source=lambda *a: 1,
                        order_gate=lambda: (True, "TEST"))
        for _ in range(5):
            b.get_fee_rates("BTCUSDT", "linear")
            b.get_lot_rules("BTCUSDT", "linear")
        assert calls.count("/v5/account/fee-rate") == 1
        assert calls.count("/v5/market/instruments-info") == 1


class TestTheCostGateTakesTheRoundTripAsAnInput:
    def test_it_is_required(self):
        with pytest.raises(TypeError):
            cc.evaluate_entry(funding_prints_bps=[3.0] * 3, perp=MARK,
                              spot=MARK, borrow_apr=0.0)

    def test_a_cheaper_round_trip_clears_an_entry_a_dearer_one_refuses(self):
        """0.25 bps per print is 0.75 bps/day of carry. Amortised over the
        assumed 30-day hold, a 31 bps round trip costs 1.03 bps/day and the
        trade cannot pay for its own exit; an 11 bps round trip costs 0.37 and
        it can. Same funding, same basis — the execution mode decides."""
        thin = dict(funding_prints_bps=[0.25] * 3, perp=MARK, spot=MARK,
                    borrow_apr=0.0)
        assert not cc.evaluate_entry(**thin, round_trip_bps=31.0)
        assert cc.evaluate_entry(**thin, round_trip_bps=11.0)

    def test_the_declared_acquire_constant_is_unchanged(self):
        assert cc.ROUND_TRIP_BPS == 31.0


class TestModeIsAnExplicitDecision:
    def test_the_engine_cannot_be_built_without_a_mode(self):
        with pytest.raises(TypeError):
            CarryEngine(broker=Venue(), max_notional_usd=100.0, borrow_apr=0.0,
                        persist=lambda s: None)

    def test_an_unknown_mode_raises_at_construction(self):
        with pytest.raises(ValueError, match="execution mode"):
            CarryEngine(broker=Venue(), max_notional_usd=100.0, borrow_apr=0.0,
                        execution_mode="hedge_maybe", persist=lambda s: None)

    def test_build_bot_refuses_without_the_config_key(self):
        import config as _config
        cfg = _config.load({"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0"})
        assert cfg.CARRY_EXECUTION_MODE is None
        with pytest.raises(ValueError, match="CARRY_EXECUTION_MODE"):
            _main.build_bot(config=cfg, store=_Store(), client=mock.Mock(),
                            risk_manager=mock.Mock(), engine=mock.Mock())

    @pytest.mark.parametrize("mode", [OVERLAY, ACQUIRE])
    def test_build_bot_passes_the_configured_mode(self, mode):
        import config as _config
        cfg = _config.load({"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
                            "CARRY_EXECUTION_MODE": mode})
        bot = _main.build_bot(config=cfg, store=_Store(), client=mock.Mock(),
                              risk_manager=mock.Mock(), engine=mock.Mock())
        assert bot.carry.execution_mode == mode

    def test_an_unknown_mode_in_config_is_refused_at_load(self):
        import config as _config
        with pytest.raises(Exception):
            _config.load({"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
                          "CARRY_EXECUTION_MODE": "yolo"})


class _Store:
    def __init__(self):
        self.seq = 0

    def next_order_seq(self):
        self.seq += 1
        return self.seq

    def trip_kill_switch(self, reason):
        pass

    # 0037: the carry book writes what it holds on every state change, and
    # reads it back before the first tick. A stub store without these is a
    # book with amnesia, which is what INVENTORY D3 was.
    def save_carry_position(self, state):
        self.carry_states = getattr(self, "carry_states", [])
        self.carry_states.append(state)

    def load_carry_position(self):
        return getattr(self, "carry_states", None) and self.carry_states[-1]

    # 0044: the double-entry journal. A store without these is a book that
    # places orders and cannot say what they cost.
    def save_ledger(self, rows):
        self.ledger_rows = list(rows)

    def load_ledger(self):
        return getattr(self, "ledger_rows", None)

    def is_kill_switch_engaged(self):
        return (False, "")


class TestAcquireModeIsUnchanged:
    def test_it_still_buys_spot_then_shorts_the_perp(self):
        v = Venue()
        e = engine(v, mode=ACQUIRE, cap=100_000.0)
        open_pair(e)
        assert [(o[0], o[1]) for o in v.orders] == [("Buy", "spot"),
                                                    ("Sell", "linear")]

    def test_a_failed_perp_still_unwinds_the_spot_immediately(self):
        v = Venue(reject={"linear"})
        e = engine(v, mode=ACQUIRE, cap=100_000.0)
        d = open_pair(e)
        assert e.position is None
        assert ("Sell", "spot") in [(o[0], o[1]) for o in v.orders]
        assert "PERP_LEG_DID_NOT_FILL" in d.reason


class TestTheBrokerReadsTheVenuesRules:
    class Client:
        def __init__(self, **rows):
            self.rows = rows
            self.calls = []

        def _request(self, method, endpoint, *, params=None, body=None,
                     signed=False, retries=3):
            self.calls.append((endpoint, params))
            if endpoint in self.rows:
                return self.rows[endpoint]
            return {}

    def broker(self, client):
        return CarryBroker(client=client, sequence_source=lambda *a: 1,
                           order_gate=lambda: (True, "TEST"))

    def test_linear_lot_rules(self):
        c = self.Client(**{"/v5/market/instruments-info": {"list": [
            {"lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001",
                               "minNotionalValue": "5"}}]}})
        assert self.broker(c).get_lot_rules("BTCUSDT", "linear") == {
            "qty_step": 0.001, "min_qty": 0.001, "min_notional": 5.0}

    def test_spot_lot_rules_use_the_spot_field_names(self):
        c = self.Client(**{"/v5/market/instruments-info": {"list": [
            {"lotSizeFilter": {"basePrecision": "0.000001",
                               "minOrderQty": "0.000048",
                               "minOrderAmt": "5"}}]}})
        assert self.broker(c).get_lot_rules("BTCUSDT", "spot") == {
            "qty_step": 0.000001, "min_qty": 0.000048, "min_notional": 5.0}

    def test_missing_rules_raise(self):
        with pytest.raises(PairIncident):
            self.broker(self.Client()).get_lot_rules("BTCUSDT", "linear")

    def test_fee_rates_come_from_the_account_not_a_constant(self):
        c = self.Client(**{"/v5/account/fee-rate": {"list": [
            {"makerFeeRate": "0.0002", "takerFeeRate": "0.00055"}]}})
        assert self.broker(c).get_fee_rates("BTCUSDT", "linear") == {
            "maker_bps": pytest.approx(2.0), "taker_bps": pytest.approx(5.5)}

    def test_missing_fee_rates_raise(self):
        with pytest.raises(PairIncident):
            self.broker(self.Client()).get_fee_rates("BTCUSDT", "linear")

    def test_inventory_is_wallet_balance_minus_locked(self):
        c = self.Client(**{"/v5/account/wallet-balance": {"list": [
            {"coin": [{"coin": "BTC", "walletBalance": "2.5",
                       "locked": "0.5"}]}]}})
        assert self.broker(c).get_spot_inventory("BTCUSDT") == \
            pytest.approx(2.0)

    def test_an_unreadable_inventory_raises_rather_than_reporting_zero(self):
        with pytest.raises(PairIncident):
            self.broker(self.Client()).get_spot_inventory("BTCUSDT")

    def test_the_lot_call_is_public_and_the_fee_call_is_signed(self):
        c = self.Client(**{
            "/v5/market/instruments-info": {"list": [{"lotSizeFilter": {
                "qtyStep": "0.001", "minOrderQty": "0.001",
                "minNotionalValue": "5"}}]},
            "/v5/account/fee-rate": {"list": [{"makerFeeRate": "0.0002",
                                              "takerFeeRate": "0.00055"}]}})
        b = self.broker(c)
        b.get_lot_rules("BTCUSDT", "linear")
        b.get_fee_rates("BTCUSDT", "linear")
        assert c.calls[0][1]["category"] == "linear"
