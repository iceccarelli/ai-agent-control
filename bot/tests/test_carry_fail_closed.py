"""0033 — Phase C: every gate on the carry path fails closed, proven by running.

INVENTORY.md found each of these by running the carry path against a stub
venue. Each test below reproduces one finding and asserts the refusal.
"""
from __future__ import annotations

import ast
import os
import sys
import time
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import carry_costs as cc  # noqa: E402
import main as _main  # noqa: E402
from carry_broker import CarryBroker, CarryOrderRefused, LegFill  # noqa: E402
from carry_engine import BookState, CarryEngine  # noqa: E402
from carry_risk import CarryRisk  # noqa: E402
from market_snapshot import MarketSnapshot  # noqa: E402

REPO = os.path.join(os.path.dirname(__file__), "..")
MARK = 100_000.0
H8 = 8 * 3600 * 1000
_STAMP = [0]


def _print_ms():
    """0034: a new settled-print stamp per candle (8h apart)."""
    _STAMP[0] += H8
    return _STAMP[0]


class Venue:
    """Fills what it is asked unless told to reject a product."""

    def __init__(self, reject=()):
        self.reject = set(reject)
        self.orders = []

    def place_market(self, *, symbol, side, qty, product):
        self.orders.append((side, product))
        if product in self.reject:
            return None
        return {"filled_qty": qty, "avg_price": MARK, "order_link_id": "x"}

    def get_margin_multiple(self, symbol):
        return 5.0

    def get_mark(self, symbol):
        return MARK

    # 0036: the venue's own lot rules and fee tier. A 1e-6 step leaves every
    # size in this file unchanged; the fee table is what the round trip is
    # computed from instead of a constant.
    def get_lot_rules(self, symbol, product):
        return {"qty_step": 1e-6, "min_qty": 1e-6, "min_notional": 0.0}

    def get_fee_rates(self, symbol, product):
        return {"maker_bps": 2.0,
                "taker_bps": 10.0 if product == "spot" else 5.5}


def snap(perp=MARK, spot=MARK):
    return MarketSnapshot(perp_mark=perp, spot_mark=spot, funding_bps=3.0,
                          margin_multiple=5.0, observed_at_s=time.time())


def warmed(engine):
    """Three prints of history, taken with the gate absent so no leg can go."""
    gate, view = engine.pair_risk, engine.snapshot
    engine.pair_risk, engine.snapshot = None, None
    for _ in range(2):
        engine.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                           funding_print_ms=_print_ms())
    engine.pair_risk, engine.snapshot = gate, view
    return engine


def engine(venue, *, gate=True, view=True, **kw):
    kw.setdefault("borrow_apr", 0.05)
    kw.setdefault("max_notional_usd", 100.0)
    kw.setdefault("execution_mode", "acquire")
    kw.setdefault("persist", lambda state: None)
    e = CarryEngine(broker=venue, **kw)
    e.pair_risk = CarryRisk(max_notional_usd=100.0) if gate else None
    e.snapshot = snap() if view else None
    return warmed(e)


class TestNoFirstLegWithoutTheGateAndTheView:
    def test_a_wired_engine_opens(self):
        v = Venue()
        d = engine(v).on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                           funding_print_ms=_print_ms())
        assert d.action == "opened" and len(v.orders) == 2

    def test_no_pair_gate_no_leg(self):
        v = Venue()
        d = engine(v, gate=False).on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                           funding_print_ms=_print_ms())
        assert d.acted is False
        assert d.reason == "PAIR_GATE_ABSENT"
        assert v.orders == []

    def test_no_snapshot_no_leg(self):
        v = Venue()
        d = engine(v, view=False).on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                           funding_print_ms=_print_ms())
        assert d.reason == "SNAPSHOT_ABSENT"
        assert v.orders == []

    def test_a_snapshot_of_a_different_instant_is_refused(self):
        """One stamp, not three loose reads: the gate must judge the same
        observation the entry decision used."""
        v = Venue()
        e = engine(v)
        e.snapshot = snap(perp=MARK * 1.001)
        d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                           funding_print_ms=_print_ms())
        assert d.reason == "SNAPSHOT_MISMATCH"
        assert v.orders == []


class TestBorrowIsRequiredEverywhere:
    def test_the_engine_cannot_be_built_without_it(self):
        with pytest.raises(TypeError):
            CarryEngine(broker=Venue(), max_notional_usd=100.0,
                        execution_mode="acquire", persist=lambda s: None)

    def test_the_cost_gate_cannot_be_asked_without_it(self):
        with pytest.raises(TypeError):
            cc.evaluate_entry(funding_prints_bps=[3.0] * 3, perp=MARK, spot=MARK)

    def test_no_module_level_borrow_default_is_left_in_the_cost_gate(self):
        assert not hasattr(cc, "DEFAULT_BORROW_APR")

    def test_build_bot_refuses_a_carry_book_without_a_financing_rate(self):
        import config as _config
        cfg = _config.load({"BOOK_MODE": "carry"})
        assert cfg.CARRY_BORROW_APR is None
        with pytest.raises(ValueError, match="CARRY_BORROW_APR"):
            _main.build_bot(config=cfg, store=_Store(), client=mock.Mock(),
                            risk_manager=mock.Mock(), engine=mock.Mock())

    @pytest.mark.parametrize("apr", ["0.0", "0.05"])
    def test_build_bot_passes_the_configured_rate(self, apr):
        import config as _config
        cfg = _config.load({"BOOK_MODE": "carry", "CARRY_BORROW_APR": apr,
                            "CARRY_EXECUTION_MODE": "acquire"})
        bot = _main.build_bot(config=cfg, store=_Store(), client=mock.Mock(),
                              risk_manager=mock.Mock(), engine=mock.Mock())
        assert bot.carry.borrow_apr == pytest.approx(float(apr))

    @pytest.mark.parametrize("bad", ["-0.01", "1.5", "five", "nan"])
    def test_a_nonsense_rate_is_refused_at_load(self, bad):
        import config as _config
        with pytest.raises(Exception):
            _config.load({"BOOK_MODE": "carry", "CARRY_BORROW_APR": bad})


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

    def is_kill_switch_engaged(self):
        return (False, "")


def _carry_bot(**env):
    import config as _config
    base = {"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
            "CARRY_EXECUTION_MODE": "acquire"}
    base.update(env)
    def _request(method, endpoint, **kw):
        # 0036: the engine prices its own exit before it decides, so the fake
        # venue answers the fee table and the lot rules.
        if endpoint == "/v5/account/fee-rate":
            return {"list": [{"makerFeeRate": "0.0002",
                              "takerFeeRate": "0.00055"}]}
        if endpoint == "/v5/market/instruments-info":
            return {"list": [{"lotSizeFilter": {
                "qtyStep": "0.000001", "minOrderQty": "0.000001",
                "basePrecision": "0.000001", "minOrderAmt": "5",
                "minNotionalValue": "5"}}]}
        if endpoint == "/v5/account/wallet-balance":
            return {"list": [{"coin": [{"coin": "BTC", "walletBalance": "5",
                                        "locked": "0"}]}]}
        return {"orderLinkId": "x"}

    client = mock.Mock()
    client._request = mock.Mock(side_effect=_request)
    bot = _main.build_bot(config=_config.load(base), store=_Store(),
                          client=client, risk_manager=mock.Mock(),
                          engine=mock.Mock())
    return bot, client


def _created(client):
    return [c for c in client._request.call_args_list
            if c.args[:2] == ("POST", "/v5/order/create")]


class TestPaperMeansNoCarryOrder:
    """INVENTORY D2: USE_TESTNET=0 PAPER_TRADING=1 reported SANDBOX (PAPER)
    and the carry broker still sent a mainnet order body."""

    @pytest.mark.parametrize("testnet", ["0", "1"])
    def test_paper_refuses_every_carry_order(self, testnet):
        bot, client = _carry_bot(USE_TESTNET=testnet, PAPER_TRADING="1")
        with pytest.raises(CarryOrderRefused, match="PAPER"):
            bot.carry.broker.place_market(symbol="BTCUSDT", side="Buy",
                                          qty=0.001, product="spot")
        assert _created(client) == []

    def test_mainnet_without_the_ack_degrades_to_paper_and_refuses(self):
        """config already turns an un-acknowledged mainnet request into PAPER;
        the broker then refuses on PAPER."""
        bot, client = _carry_bot(USE_TESTNET="0", PAPER_TRADING="0")
        with pytest.raises(CarryOrderRefused, match="PAPER"):
            bot.carry.broker.place_market(symbol="BTCUSDT", side="Buy",
                                          qty=0.001, product="spot")
        assert _created(client) == []

    LIVE = {"USE_TESTNET": "0", "PAPER_TRADING": "0",
            "LIVE_TRADING_ACK": "I_UNDERSTAND", "BYBIT_API_KEY": "k" * 18,
            "BYBIT_API_SECRET": "s" * 36}

    def test_live_authorised_but_unsigned_gate_is_refused(self):
        bot, client = _carry_bot(**self.LIVE)
        with pytest.raises(CarryOrderRefused, match="promotion gate"):
            bot.carry.broker.place_market(symbol="BTCUSDT", side="Buy",
                                          qty=0.001, product="spot")
        assert _created(client) == []

    def test_authorisation_revoked_at_runtime_is_refused(self, monkeypatch):
        import config as _config
        bot, client = _carry_bot(**self.LIVE)
        monkeypatch.setattr(_config, "is_live_authorized",
                            lambda cfg=None: (False, "revoked"))
        with pytest.raises(CarryOrderRefused, match="mainnet"):
            bot.carry.broker.place_market(symbol="BTCUSDT", side="Buy",
                                          qty=0.001, product="spot")
        assert _created(client) == []

    def test_testnet_without_paper_may_send(self):
        bot, client = _carry_bot(USE_TESTNET="1", PAPER_TRADING="0")
        bot.carry.broker.place_market(symbol="BTCUSDT", side="Buy",
                                      qty=0.001, product="spot")
        assert len(_created(client)) == 1

    def test_the_broker_cannot_be_built_without_an_order_gate(self):
        with pytest.raises(TypeError):
            CarryBroker(client=mock.Mock(), sequence_source=lambda *a: 1)

    def test_an_unreadable_gate_refuses(self):
        def broken():
            raise RuntimeError("config gone")
        b = CarryBroker(client=mock.Mock(), sequence_source=lambda *a: 1,
                        order_gate=broken)
        with pytest.raises(CarryOrderRefused):
            b.place_market(symbol="BTCUSDT", side="Buy", qty=0.001,
                           product="spot")

    def test_the_engine_names_the_refusal(self):
        bot, _client = _carry_bot(USE_TESTNET="1", PAPER_TRADING="1")
        e = bot.carry
        e.snapshot = snap()
        warmed(e)
        d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                           funding_print_ms=_print_ms())
        assert d.acted is False
        assert "PAPER" in str(d.detail.get("error", ""))


class TestTheSpotLegIsSizedInBtc:
    """INVENTORY F1: without marketUnit a spot market BUY is a USDT amount."""

    def _body(self, product, side="Buy"):
        client = mock.Mock()
        client._request = mock.Mock(return_value={"orderLinkId": "x",
                                                  "cumExecQty": "0.001",
                                                  "avgPrice": "100000"})
        CarryBroker(client=client, sequence_source=lambda *a: 1,
                    order_gate=lambda: (True, "TEST")).place_market(
            symbol="BTCUSDT", side=side, qty=0.001, product=product)
        return client._request.call_args_list[0].kwargs["body"]

    @pytest.mark.parametrize("side", ["Buy", "Sell"])
    def test_spot_market_orders_say_base_coin(self, side):
        assert self._body("spot", side)["marketUnit"] == "baseCoin"

    def test_linear_orders_carry_no_market_unit(self):
        assert "marketUnit" not in self._body("linear", "Sell")


class TestAFeeIsKnownOrItIsUnknown:
    def test_cum_exec_fee_is_carried(self):
        fill = CarryBroker._fill_from({"cumExecQty": "0.001", "avgPrice": "1",
                                       "cumExecFee": "0.000001"}, "x")
        assert fill.fee == pytest.approx(0.000001)
        assert fill.as_engine_result()["fee"] == pytest.approx(0.000001)

    def test_an_absent_fee_is_none_not_zero(self):
        fill = CarryBroker._fill_from({"cumExecQty": "0.001",
                                       "avgPrice": "1"}, "x")
        assert fill.fee is None

    def test_the_engine_keeps_unknown_as_unknown(self):
        v = Venue()
        e = engine(v)
        e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                           funding_print_ms=_print_ms())
        assert e.position.spot.fee is None
        assert e.position.perp.fee is None

    def test_no_carry_module_books_a_zero_fee(self):
        for name in ("carry_engine.py", "carry_broker.py", "carry_costs.py",
                     "carry_risk.py", "market_snapshot.py",
                     os.path.join("tools", "carry_backtest.py")):
            tree = ast.parse(open(os.path.join(REPO, name),
                                  encoding="utf-8").read())
            for node in ast.walk(tree):
                if isinstance(node, ast.keyword) and node.arg == "fee":
                    assert not (isinstance(node.value, ast.Constant)
                                and node.value.value == 0), name
                if isinstance(node, ast.Assign):
                    for t in node.targets:
                        tname = getattr(t, "id", getattr(t, "attr", ""))
                        if tname == "fee":
                            assert not (isinstance(node.value, ast.Constant)
                                        and node.value.value == 0), name


class TestABrokenPairCannotThrash:
    """INVENTORY D4: a venue rejecting the perp leg produced 8 spot round
    trips in 10 ticks. A broken pair costs a round trip; it spends the day."""

    def test_ten_ticks_one_round_trip(self):
        v = Venue(reject={"linear"})
        e = engine(v)
        for _ in range(10):
            e.snapshot = snap()
            e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                           funding_print_ms=_print_ms())
        spot = [o for o in v.orders if o[1] == "spot"]
        assert spot == [("Buy", "spot"), ("Sell", "spot")]
        assert e.state is BookState.FLAT

    def test_a_broken_pair_is_not_recorded_as_an_entry(self):
        v = Venue(reject={"linear"})
        e = engine(v)
        e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                           funding_print_ms=_print_ms())
        assert e.pair_risk._entries == []
        assert len(e.pair_risk._broken) == 1

    def test_the_gate_reports_why(self):
        r = CarryRisk(max_notional_usd=100.0)
        r.record_broken_pair()
        d = r.gate_open(notional_usd=100.0, snapshot=snap(), has_open_pair=False)
        assert d.reason == "ENTRY_LIMIT_REACHED_TODAY"
        assert d.detail["broken_pairs_today"] == 1


class TestRecordEntryOnlyAfterBothLegs:
    @pytest.mark.parametrize("reject,expected", [((), 1), (("spot",), 0),
                                                 (("linear",), 0)])
    def test_only_a_landed_pair_is_an_entry(self, reject, expected):
        v = Venue(reject=set(reject))
        e = engine(v)
        e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                           funding_print_ms=_print_ms())
        assert len(e.pair_risk._entries) == expected
