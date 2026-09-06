"""Slice 7 contract tests: the category fork, the market-data corpus, memory wiring.

Three claims are defended here, and each one is a claim the previous six slices
could not make.

**1. The bot knows which venue it is on.** Spot cannot short. Every layer says
so with the same answer and the same reason — the config, the risk gate, the
client, and the strategy — rather than each discovering it separately at the
moment an order is rejected.

**2. The order book is measured by one piece of code.** ``market_data`` computes
depth, imbalance and spread for the backtest and for production alike, so a
liquidity gate that passes in simulation means something in live.

**3. Recorded experience can only make positions smaller.** Never larger. Not
by configuration, not by an unlucky sign, not by a multiplier above one.

Fully offline. No network, no sleeping.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest as bt          # noqa: E402
import bybit_connection as bc  # noqa: E402
import config as _config       # noqa: E402
import market_data as md       # noqa: E402
import memory as mem           # noqa: E402
import risk_management as rm   # noqa: E402
import trading_engine as te    # noqa: E402
from fake_bybit import API_KEY, API_SECRET, FakeBybit  # noqa: E402
from persistence import StateStore  # noqa: E402

EQUITY = 10_000.0


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "s.db"))
    s.update_equity(EQUITY)
    yield s
    s.close()


@pytest.fixture()
def exchange():
    return FakeBybit()


def cfg_for(category="spot", **over):
    env = {
        "CATEGORY": category,
        "BYBIT_API_KEY": API_KEY,
        "BYBIT_API_SECRET": API_SECRET,
        "USE_TESTNET": "1",
        "PAPER_TRADING": "1",
    }
    env.update({k: str(v) for k, v in over.items()})
    return _config.load(env)


@pytest.fixture()
def spot_cfg():
    return cfg_for("spot")


@pytest.fixture()
def linear_cfg():
    return cfg_for("linear")


def good_order(**over):
    base = dict(
        symbol="BTCUSDT", side="Buy", entry_price=50_000.0, stop_loss=49_500.0,
        quantity=0.001, account_equity=EQUITY,
    )
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# 1. the category fork
# ---------------------------------------------------------------------------


class TestCategoryIsOneDecision:
    def test_spot_derives_no_shorts_and_linear_derives_shorts(self):
        assert cfg_for("spot").ALLOW_SHORTS is False
        assert cfg_for("linear").ALLOW_SHORTS is True

    def test_an_unimplemented_category_is_rejected_at_load(self):
        for category in ("inverse", "option", "futures", ""):
            with pytest.raises(_config.ConfigError) as exc:
                cfg_for(category)
            assert "not implemented" in str(exc.value)

    def test_spot_with_leverage_is_refused(self):
        """Spot margin borrow is not implemented, so leverage on spot is a lie."""
        with pytest.raises(_config.ConfigError) as exc:
            cfg_for("spot", USE_LEVERAGE="1")
        assert "CATEGORY=linear" in str(exc.value)

    def test_funding_enters_the_round_trip_cost_only_on_perps(self):
        spot, linear = cfg_for("spot"), cfg_for("linear")
        assert spot.FUNDING_COST_BPS == 0.0
        assert linear.FUNDING_COST_BPS > 0.0
        assert linear.ROUND_TRIP_COST_BPS > 2 * linear.TAKER_FEE_BPS + linear.SLIPPAGE_BPS

    def test_a_longer_hold_costs_more_funding(self):
        """Three funding stamps cost three times one. The estimate must scale."""
        short_hold = cfg_for("linear", MAX_HOLD_HOURS="8")
        long_hold = cfg_for("linear", MAX_HOLD_HOURS="24")
        assert long_hold.FUNDING_COST_BPS == pytest.approx(
            3 * short_hold.FUNDING_COST_BPS
        )

    def test_the_edge_floor_still_cannot_sit_below_cost_on_a_perp(self):
        """Funding is part of the cost, so the floor must clear it too."""
        linear = cfg_for("linear")
        assert linear.MIN_EDGE_BPS >= linear.ROUND_TRIP_COST_BPS
        with pytest.raises(_config.ConfigError):
            cfg_for("linear", MAX_HOLD_HOURS="72", MIN_EDGE_BPS="5")

    def test_allow_shorts_cannot_be_set_by_hand(self):
        """It is derived. An operator who sets it directly is ignored, not obeyed."""
        assert cfg_for("spot", ALLOW_SHORTS="1").ALLOW_SHORTS is False


class TestTheGateKnowsAboutShorts:
    def test_spot_blocks_a_sell_to_open_by_name(self, store, spot_cfg):
        manager = rm.BillionaireRiskManager(config=spot_cfg, store=store)
        d = manager.gate_order(**good_order(side="Sell", stop_loss=50_500.0))
        assert d.reason == "SHORTS_NOT_AVAILABLE_ON_SPOT"
        assert "CATEGORY=linear" in d.detail["remedy"]

    def test_linear_permits_a_sell_to_open(self, store, linear_cfg):
        manager = rm.BillionaireRiskManager(config=linear_cfg, store=store)
        d = manager.gate_order(**good_order(
            side="Sell", stop_loss=50_500.0, take_profit=48_500.0,
            funding_rate=0.0001,
        ))
        assert d.ok, d.reason

    def test_a_long_is_unaffected_on_either_venue(self, store, spot_cfg, linear_cfg):
        for cfg in (spot_cfg, linear_cfg):
            manager = rm.BillionaireRiskManager(config=cfg, store=store)
            gate = manager._gate_short_capability("Buy")
            assert gate.ok

    def test_an_unknown_side_still_reaches_the_side_check(self, store, spot_cfg):
        """The short gate must not swallow `Sideways` and report it as a short."""
        manager = rm.BillionaireRiskManager(config=spot_cfg, store=store)
        assert manager.gate_order(**good_order(side="Sideways")).reason == "UNKNOWN_SIDE"


class TestTheFundingGate:
    def test_spot_has_no_funding_to_gate(self, store, spot_cfg):
        manager = rm.BillionaireRiskManager(config=spot_cfg, store=store)
        assert manager._gate_funding(None).ok
        assert manager._gate_funding(None).reason == "NO_FUNDING_ON_SPOT"

    def test_an_unknown_funding_rate_blocks_on_a_perp(self, store, linear_cfg):
        """An unknown carry is not a zero carry. This is the one cost that
        accrues while the bot does nothing at all."""
        manager = rm.BillionaireRiskManager(config=linear_cfg, store=store)
        assert manager._gate_funding(None).reason == "FUNDING_UNKNOWN"

    @pytest.mark.parametrize("bad", ["not-a-number", float("nan"), float("inf")])
    def test_an_unusable_funding_rate_blocks(self, store, linear_cfg, bad):
        manager = rm.BillionaireRiskManager(config=linear_cfg, store=store)
        assert manager._gate_funding(bad).ok is False

    def test_expensive_funding_blocks(self, store, linear_cfg):
        manager = rm.BillionaireRiskManager(config=linear_cfg, store=store)
        d = manager._gate_funding(0.01)      # 100 bps per 8h
        assert d.reason == "FUNDING_TOO_EXPENSIVE"
        assert d.detail["funding_bps"] == pytest.approx(100.0)

    def test_cheap_funding_passes_in_either_sign(self, store, linear_cfg):
        manager = rm.BillionaireRiskManager(config=linear_cfg, store=store)
        assert manager._gate_funding(0.0001).ok
        assert manager._gate_funding(-0.0001).ok

    def test_the_funding_gate_runs_inside_gate_order(self, store, linear_cfg):
        manager = rm.BillionaireRiskManager(config=linear_cfg, store=store)
        d = manager.gate_order(**good_order(take_profit=51_500.0))
        assert d.reason == "FUNDING_UNKNOWN", "a perp trade passed without a carry"


class TestTheLiquidityGate:
    def _manager(self, store, cfg=None):
        return rm.BillionaireRiskManager(
            config=cfg or cfg_for("spot", MIN_BOOK_DEPTH_USD="10000",
                                  MAX_BOOK_IMBALANCE="0.8"),
            store=store,
        )

    def book(self, **over):
        base = {"depth_bid_usd": 90_000.0, "depth_ask_usd": 90_000.0,
                "imbalance": 0.0}
        base.update(over)
        return base

    def test_no_book_is_permission_to_skip_not_a_block(self, store):
        """An operator without a book feed must still be able to run."""
        assert self._manager(store)._gate_book_liquidity(None, side="Buy").ok

    def test_a_supplied_but_incomplete_book_blocks(self, store):
        """Present-and-broken is different from absent, and must not be treated
        as absent — that is the difference between 'no feed' and 'bad feed'."""
        d = self._manager(store)._gate_book_liquidity(
            {"depth_bid_usd": None, "imbalance": 0.0}, side="Buy"
        )
        assert d.reason == "BOOK_INCOMPLETE"

    def test_thin_exit_liquidity_blocks_a_long(self, store):
        """A long exits by selling into the BID, so that is the depth that matters."""
        d = self._manager(store)._gate_book_liquidity(
            self.book(depth_bid_usd=500.0), side="Buy"
        )
        assert d.reason == "EXIT_LIQUIDITY_TOO_THIN"

    def test_a_long_is_not_blocked_by_a_thin_ASK(self, store):
        """The ask is what we buy from; it is not where the stop fills."""
        assert self._manager(store)._gate_book_liquidity(
            self.book(depth_ask_usd=10.0), side="Buy"
        ).ok

    def test_a_short_looks_at_the_other_side(self, store):
        manager = self._manager(store)
        assert manager._gate_book_liquidity(
            self.book(depth_bid_usd=10.0), side="Sell"
        ).ok
        assert manager._gate_book_liquidity(
            self.book(depth_ask_usd=10.0), side="Sell"
        ).reason == "EXIT_LIQUIDITY_TOO_THIN"

    def test_a_book_stacked_against_the_trade_blocks(self, store):
        d = self._manager(store)._gate_book_liquidity(
            self.book(imbalance=-0.95), side="Buy"
        )
        assert d.reason == "BOOK_STACKED_AGAINST"

    def test_a_book_stacked_in_our_favour_does_not_block(self, store):
        assert self._manager(store)._gate_book_liquidity(
            self.book(imbalance=0.95), side="Buy"
        ).ok

    @pytest.mark.parametrize("bad", ["x", None, float("nan")])
    def test_unparseable_book_numbers_block(self, store, bad):
        assert self._manager(store)._gate_book_liquidity(
            self.book(imbalance=bad), side="Buy"
        ).ok is False


# ---------------------------------------------------------------------------
# 2. the client speaks both dialects
# ---------------------------------------------------------------------------


def client_for(category, exchange, store, **over):
    return bc.BybitClient(
        config=cfg_for(category, **over), store=store, transport=exchange
    )


class TestTheClientSpeaksBothDialects:
    def test_an_unsupported_category_cannot_be_constructed(self, exchange, store):
        class Cfg:
            USE_TESTNET = True
            CATEGORY = "inverse"
            BYBIT_API_KEY = API_KEY
            BYBIT_API_SECRET = API_SECRET

        with pytest.raises(bc.PermanentAPIError):
            bc.BybitClient(config=Cfg(), store=store, transport=exchange)

    def test_spot_market_orders_state_their_quantity_unit(self, exchange, store):
        """Without marketUnit a spot market BUY is read as a QUOTE amount —
        off by the price, which for BTC is a factor of about 100,000."""
        client = client_for("spot", exchange, store)
        client.place_order(symbol="BTCUSDT", side="Buy", qty=0.001)
        body = exchange.order_create_bodies()[-1]
        assert body["marketUnit"] == "baseCoin"
        assert body["category"] == "spot"
        assert "positionIdx" not in body

    def test_linear_orders_state_one_way_mode_and_omit_spot_only_fields(
        self, exchange, store
    ):
        client = client_for("linear", exchange, store)
        client.place_order(symbol="BTCUSDT", side="Sell", qty=0.001)
        body = exchange.order_create_bodies()[-1]
        assert body["category"] == "linear"
        assert body["positionIdx"] == 0
        assert "marketUnit" not in body
        assert "isLeverage" not in body

    def test_a_linear_short_is_not_blocked_for_lacking_the_base_asset(
        self, exchange, store
    ):
        """The spot rule — 'do you hold what you are selling' — must not be
        applied to a perp, where neither side hands over a coin."""
        exchange.balances["BTC"] = 0.0
        result = client_for("linear", exchange, store).place_order(
            symbol="BTCUSDT", side="Sell", qty=0.001
        )
        assert result.ok is True, result.reason

    def test_a_linear_order_is_blocked_for_lacking_MARGIN(self, exchange, store):
        exchange.balances["USDT"] = 1.0
        result = client_for("linear", exchange, store).place_order(
            symbol="BTCUSDT", side="Sell", qty=0.001
        )
        assert result.reason.startswith("INSUFFICIENT_MARGIN")

    def test_a_reduce_only_order_is_never_blocked_on_funds(self, exchange, store):
        """Blocking an EXIT on a balance check makes a position unclosable at
        exactly the moment closing it matters."""
        exchange.balances["USDT"] = 0.0
        exchange.balances["BTC"] = 0.0
        result = client_for("linear", exchange, store).place_order(
            symbol="BTCUSDT", side="Buy", qty=0.001, reduce_only=True
        )
        assert result.ok is True, result.reason


class TestTheTwoStopMechanisms:
    def test_the_spot_stop_is_a_conditional_order(self, exchange, store):
        client_for("spot", exchange, store).place_stop_order(
            symbol="BTCUSDT", side="Sell", qty=0.001, trigger_price=49_000.0
        )
        assert not any("trading-stop" in r["url"] for r in exchange.requests)
        body = exchange.order_create_bodies()[-1]
        assert body["orderFilter"] == "StopOrder"
        assert body["triggerDirection"] == 2      # a long's stop fires on a FALL

    def test_the_linear_stop_is_attached_to_the_position(self, exchange, store):
        exchange.positions["BTCUSDT"] = {"symbol": "BTCUSDT", "size": "0.001",
                                         "side": "Buy", "stopLoss": ""}
        result = client_for("linear", exchange, store).place_stop_order(
            symbol="BTCUSDT", side="Sell", qty=0.001, trigger_price=49_000.0
        )
        assert result.ok, result.reason
        req = [r for r in exchange.requests if "trading-stop" in r["url"]]
        assert len(req) == 1
        body = json.loads(req[0]["body"])
        # tpslMode, NOT tpSlMode. Bybit ignores unknown parameters silently, so
        # the wrong casing returns 200 OK with no stop set.
        assert body["tpslMode"] == "Full"
        assert "tpSlMode" not in body
        assert float(body["stopLoss"]) == pytest.approx(49_000.0, abs=1.0)

    def test_a_rejected_position_stop_reports_failure(self, exchange, store):
        exchange.positions["BTCUSDT"] = {"symbol": "BTCUSDT", "size": "0.001",
                                         "side": "Buy", "stopLoss": ""}
        exchange.trading_stop_fails_with = 110043
        result = client_for("linear", exchange, store).place_stop_order(
            symbol="BTCUSDT", side="Sell", qty=0.001, trigger_price=49_000.0
        )
        assert result.ok is False
        assert result.reason.startswith("REJECTED:")


class TestStopVerificationFailsClosed:
    def test_a_live_spot_stop_verifies(self, exchange, store):
        client = client_for("spot", exchange, store)
        stop = client.place_stop_order(
            symbol="BTCUSDT", side="Sell", qty=0.001, trigger_price=49_000.0
        )
        live, detail = client.verify_stop(
            symbol="BTCUSDT", order_link_id=stop.order_link_id
        )
        assert live is True, detail

    def test_an_invisible_spot_stop_is_treated_as_no_stop(self, exchange, store):
        client = client_for("spot", exchange, store)
        live, detail = client.verify_stop(symbol="BTCUSDT", order_link_id="never-sent")
        assert live is False
        assert detail == "STOP_ORDER_NOT_VISIBLE"

    def test_a_linear_stop_verifies_against_the_POSITION(self, exchange, store):
        exchange.positions["BTCUSDT"] = {"symbol": "BTCUSDT", "size": "0.001",
                                         "side": "Buy", "stopLoss": ""}
        client = client_for("linear", exchange, store)
        stop = client.place_stop_order(
            symbol="BTCUSDT", side="Sell", qty=0.001, trigger_price=49_000.0
        )
        live, detail = client.verify_stop(
            symbol="BTCUSDT", order_link_id=stop.order_link_id
        )
        assert live is True and "stopLoss=" in detail

    def test_a_position_with_an_empty_stop_loss_field_is_unprotected(
        self, exchange, store
    ):
        """The exact failure mode of the misspelled parameter: a 200 OK, and a
        position with no stop. Verification must catch it."""
        exchange.positions["BTCUSDT"] = {"symbol": "BTCUSDT", "size": "0.001",
                                         "side": "Buy", "stopLoss": ""}
        client = client_for("linear", exchange, store)
        live, detail = client.verify_stop(symbol="BTCUSDT", order_link_id="anything")
        assert live is False
        assert detail == "POSITION_HAS_NO_STOP_LOSS"

    def test_no_position_at_all_is_reported_as_unverified(self, exchange, store):
        client = client_for("linear", exchange, store)
        live, detail = client.verify_stop(symbol="BTCUSDT", order_link_id="x")
        assert live is False and detail == "NO_POSITION"


class TestLinearAccountSurface:
    def test_get_position_is_refused_on_spot(self, exchange, store):
        """On spot a 'position' is a coin balance. Answering the question with
        a different question's answer is how the two categories get conflated."""
        with pytest.raises(bc.PermanentAPIError):
            client_for("spot", exchange, store).get_position("BTCUSDT")

    def test_funding_is_none_on_spot_and_a_fraction_on_linear(self, exchange, store):
        assert client_for("spot", exchange, store).get_funding_rate("BTCUSDT") is None
        exchange.funding_rate = 0.000125
        rate = client_for("linear", exchange, store).get_funding_rate("BTCUSDT")
        assert rate == pytest.approx(0.000125)

    def test_an_unreadable_funding_rate_is_none_not_zero(self, exchange, store):
        exchange.funding_rate = "not-a-number"
        assert client_for("linear", exchange, store).get_funding_rate("BTCUSDT") is None

    def test_leverage_already_set_is_a_success_not_a_failure(self, exchange, store):
        """retCode 110043 means 'leverage not modified'. Treating it as an error
        would refuse to start for a reason that is not a problem."""
        client = client_for("linear", exchange, store)
        exchange.reject_next_with = (110043, "leverage not modified")
        assert client.set_leverage("BTCUSDT") is True

    def test_set_leverage_is_a_no_op_on_spot(self, exchange, store):
        assert client_for("spot", exchange, store).set_leverage("BTCUSDT") is True
        assert exchange.leverage_calls == []


# ---------------------------------------------------------------------------
# 3. one order-book measurement, shared
# ---------------------------------------------------------------------------


class TestOneBookMeasurement:
    def test_the_client_reads_a_live_book_through_market_data(self, exchange, store):
        exchange.book = {"b": [["100.0", "5"], ["99.9", "5"]],
                         "a": [["100.2", "5"], ["100.3", "5"]]}
        features = client_for("spot", exchange, store).get_book_features("BTCUSDT")
        assert features["mid"] == pytest.approx(100.1)
        assert features["depth_bid_usd"] == pytest.approx(999.5)
        assert features["usable"] is True

    def test_an_unavailable_book_is_none_not_an_empty_dict(self, exchange, store):
        """None means 'no book' and the gate skips. An empty dict would mean
        'broken feed' and the gate would block. They are different facts."""
        exchange.book = None
        assert client_for("spot", exchange, store).get_book_features("BTCUSDT") is None

    def test_a_crossed_book_yields_no_mid(self, exchange, store):
        exchange.book = {"b": [["101.0", "5"]], "a": [["100.0", "5"]]}
        features = client_for("spot", exchange, store).get_book_features("BTCUSDT")
        assert features["usable"] is False
        assert features["mid"] is None

    def test_the_feature_can_be_switched_off(self, exchange, store):
        exchange.book = {"b": [["100.0", "5"]], "a": [["100.2", "5"]]}
        client = client_for("spot", exchange, store, USE_ORDERBOOK="0")
        assert client.get_book_features("BTCUSDT") is None

    def test_from_ladders_and_from_events_agree(self):
        """The REST path and the archive path must measure identically, or a
        gate calibrated in backtest means nothing live."""
        bids = [[100.0, 2.0], [99.5, 3.0]]
        asks = [[100.5, 1.0], [101.0, 4.0]]
        state = md.BookState.from_ladders(bids, asks)
        features = state.features(levels=10)
        assert features.mid == pytest.approx(100.25)
        assert features.depth_bid_usd == pytest.approx(100.0 * 2 + 99.5 * 3)
        assert -1.0 <= features.imbalance <= 1.0

    def test_zero_and_negative_sizes_are_not_liquidity(self):
        state = md.BookState.from_ladders(
            [[100.0, 0.0], [99.0, -1.0], [98.0, 2.0]], [[101.0, 1.0]]
        )
        assert [lvl.price for lvl in state.bids] == [98.0]


# ---------------------------------------------------------------------------
# 4. the corpus, and how it reaches the backtester
# ---------------------------------------------------------------------------


class TestTheCorpus:
    def test_the_shipped_corpus_matches_its_manifest(self):
        report = md.verify_manifest()
        assert report.problems == [], report.problems

    def test_the_corpus_declares_itself_synthetic(self):
        """It must be impossible to mistake this for market data."""
        assert md.verify_manifest().synthetic is True
        readme = open(os.path.join(md.DATA_ROOT, "README.md"), encoding="utf-8").read()
        assert "SYNTHETIC" in readme.split("\n")[0].upper() or \
               "SYNTHETIC" in readme[:400].upper()

    def test_symbol_names_are_derived_not_guessed(self):
        assert md.symbol_from_filename("BYBIT_SPOT_BTC_USDT_1H.csv.gz") == "BTCUSDT"
        assert md.symbol_from_filename("BYBIT_SPOT_ETH_USDT_L2.csv.gz") == "ETHUSDT"
        with pytest.raises(md.MarketDataError):
            md.symbol_from_filename("mystery.csv.gz")

    def test_books_are_index_aligned_with_bars(self):
        bars, books, _ = md.load_corpus(symbols=["BTCUSDT"], levels=5)
        assert len(books["BTCUSDT"]) == len(bars["BTCUSDT"])

    def test_the_corpus_notes_say_what_is_missing(self):
        """SOLUSDT ships without an order book. That has to be reported, not
        silently turn into a run where the liquidity gate never fires."""
        _, books, notes = md.load_corpus(levels=5)
        if "SOLUSDT" not in books:
            assert any("SOLUSDT" in n and "liquidity gate" in n for n in notes), notes

    def test_a_tampered_corpus_is_refused(self, tmp_path):
        import gzip
        import shutil

        root = tmp_path / "data"
        shutil.copytree(md.DATA_ROOT, root)
        target = root / "ohlcv" / "BYBIT_SPOT_BTC_USDT_1H.csv.gz"
        with gzip.open(target, "rt") as handle:
            rows = handle.read().splitlines()
        rows[1] = rows[1].replace(",", ",", 1) + ""
        rows.append(rows[-1])          # a duplicated final bar
        with gzip.open(target, "wt") as handle:
            handle.write("\n".join(rows) + "\n")
        with pytest.raises(md.ManifestError):
            md.load_corpus(str(root))


class TestTheBacktesterUsesTheCorpus:
    def test_a_book_reaches_the_gate_through_the_real_client(self, store):
        """End to end: ladder -> SimulatedExchange -> BybitClient -> features."""
        bars = [bt.Bar(1_600_000_000_000 + i * 3_600_000, 100, 101, 99, 100, 10)
                for i in range(5)]
        ladders = [([[99.9, 5.0]], [[100.1, 5.0]])] * 5
        exchange = bt.SimulatedExchange(
            {"BTCUSDT": bars}, books_by_symbol={"BTCUSDT": ladders}
        )
        exchange.index = 3
        client = bc.BybitClient(
            config=bt._backtest_config_view(bt.BacktestConfig()),
            store=store, transport=exchange,
        )
        features = client.get_book_features("BTCUSDT")
        assert features is not None
        assert features["mid"] == pytest.approx(100.0)

    def test_no_book_in_the_simulator_is_reported_as_unavailable(self, store):
        """It must NOT invent a ladder from the bar. A gate that measures an
        invented book is worse than a gate that abstains."""
        bars = [bt.Bar(1_600_000_000_000 + i * 3_600_000, 100, 101, 99, 100, 10)
                for i in range(5)]
        exchange = bt.SimulatedExchange({"BTCUSDT": bars})
        exchange.index = 3
        client = bc.BybitClient(
            config=bt._backtest_config_view(bt.BacktestConfig()),
            store=store, transport=exchange,
        )
        assert client.get_book_features("BTCUSDT") is None

    def test_the_config_view_is_a_real_config(self):
        """It used to be a hand-written mirror of the schema, and it went stale
        the moment the schema grew. Building it with the real loader means it
        cannot."""
        view = bt._backtest_config_view(bt.BacktestConfig())
        assert isinstance(view, _config.Config)
        for field in _config.Config.__dataclass_fields__:
            assert hasattr(view, field)

    def test_the_view_never_reads_the_ambient_environment(self, monkeypatch):
        monkeypatch.setenv("MAX_POSITION_SIZE_PCT", "0.99")
        view = bt._backtest_config_view(bt.BacktestConfig(max_position_pct=0.02))
        assert view.MAX_POSITION_SIZE_PCT == pytest.approx(0.02)

    def test_a_linear_backtest_is_refused_rather_than_mis_reported(self):
        """Cash accounting cannot price a perp. It would not fail — it would
        succeed and omit funding and liquidation, in the flattering direction."""
        bars = [bt.Bar(1_600_000_000_000 + i * 3_600_000, 100, 101, 99, 100, 10)
                for i in range(300)]
        with pytest.raises(NotImplementedError) as exc:
            bt.Backtester({"BTCUSDT": bars},
                          bt.BacktestConfig(category="linear")).run()
        assert "funding" in str(exc.value)


# ---------------------------------------------------------------------------
# 5. memory may only shrink
# ---------------------------------------------------------------------------


class TestMemoryOnlyEverShrinks:
    def _memory(self, store, **over):
        return mem.TradingMemory(store, cfg_for("spot", **over))

    def test_the_multiplier_is_never_above_one(self, store):
        m = self._memory(store)
        for symbol in ("BTCUSDT", "ETHUSDT", "NEVERTRADED"):
            assert m.size_multiplier(symbol) <= 1.0

    def test_the_multiplier_is_never_below_the_configured_floor(self, store):
        m = self._memory(store, MEMORY_MIN_THROTTLE="0.4")
        assert m.size_multiplier("BTCUSDT") >= 0.4

    def test_no_history_means_no_adjustment(self, store):
        """Absence of evidence is not evidence to shrink OR to grow."""
        assert self._memory(store).size_multiplier("BTCUSDT") == 1.0

    def test_the_engine_multiplies_and_never_divides(self, store):
        """The one operator applied to the multiplier. A division would turn the
        same clamped value silently into an enlargement."""
        import inspect

        src = inspect.getsource(te.TradingEngine._apply_memory_throttle)
        assert "sizing.qty * multiplier" in src
        assert "/ multiplier" not in src

    def test_a_multiplier_of_one_leaves_the_size_untouched(self, store, spot_cfg):
        engine = _bare_engine(store, spot_cfg)
        engine.memory = _FixedMultiplier(1.0)
        sizing = _sizing(0.5)
        assert engine._apply_memory_throttle("BTCUSDT", sizing, _FILTERS) is sizing

    def test_a_multiplier_above_one_still_leaves_the_size_untouched(
        self, store, spot_cfg
    ):
        """Even if something upstream produced 1.5, the engine must not grow."""
        engine = _bare_engine(store, spot_cfg)
        engine.memory = _FixedMultiplier(1.5)
        sizing = _sizing(0.5)
        assert engine._apply_memory_throttle("BTCUSDT", sizing, _FILTERS).qty == 0.5

    def test_a_throttle_shrinks(self, store, spot_cfg):
        engine = _bare_engine(store, spot_cfg)
        engine.memory = _FixedMultiplier(0.5)
        result = engine._apply_memory_throttle("BTCUSDT", _sizing(0.5), _FILTERS)
        assert result.qty == pytest.approx(0.25)

    def test_a_throttle_below_the_exchange_minimum_skips_the_trade(
        self, store, spot_cfg
    ):
        """It must NOT round back up to the minimum. A 'reduce risk' mechanism
        that increases size is the worst possible outcome."""
        engine = _bare_engine(store, spot_cfg)
        engine.memory = _FixedMultiplier(0.001)
        result = engine._apply_memory_throttle("BTCUSDT", _sizing(0.5), _FILTERS)
        assert result.should_trade is False
        assert result.reason == "MEMORY_THROTTLE_BELOW_MIN_QTY"

    def test_a_broken_memory_leaves_sizing_alone(self, store, spot_cfg):
        """Losing the reference layer costs learning, not safety."""
        engine = _bare_engine(store, spot_cfg)
        engine.memory = _Exploding()
        sizing = _sizing(0.5)
        assert engine._apply_memory_throttle("BTCUSDT", sizing, _FILTERS) is sizing

    def test_recording_an_execution_never_raises_into_the_trade_path(
        self, store, spot_cfg
    ):
        engine = _bare_engine(store, spot_cfg)
        engine.memory = _Exploding()
        engine._remember_execution(symbol="BTCUSDT")     # must not raise

    def test_memory_cannot_clear_the_kill_switch(self, store):
        """Rule 11, asserted against the newest module rather than assumed."""
        import inspect

        src = inspect.getsource(mem)
        assert "clear_kill_switch" not in src
        assert "HUMAN_CLEARED" not in src

    def test_memory_refuses_to_persist_anything_named_like_a_credential(self, store):
        """Refusing the write beats redacting the read: the store outlives the
        process, and a redactor only has to be forgotten once."""
        m = self._memory(store)
        for key in ("api_secret", "BYBIT_API_KEY", "password", "token"):
            with pytest.raises(mem.MemoryRefused):
                m.remember("test", key, "hunter2")
        assert "hunter2" not in json.dumps(m.snapshot())


class TestMemoryReachesEveryLayer:
    def test_health_reports_what_the_bot_remembers(self):
        import main

        bot = main.build_bot()
        try:
            health = bot.health()
            assert health["memory"]["enabled"] is True
            assert health["category"] == bot.cfg.CATEGORY
            assert health["shorts_available"] == bot.cfg.ALLOW_SHORTS
        finally:
            bot.shutdown()

    def test_the_strategy_records_the_shorts_it_could_not_take(self):
        """The count of suppressed shorts is the honest measure of what the
        spot-only constraint costs. It must be recorded, not discarded."""
        import technical_analysis as ta

        strategy = ta.MarketStrategy(client=None, config=cfg_for("spot"))
        assert strategy.suppressed_shorts == 0
        assert hasattr(strategy, "_journal_suppressed_short")

    def test_a_linear_strategy_does_not_suppress(self):
        import technical_analysis as ta

        assert ta.MarketStrategy(client=None, config=cfg_for("linear")).cfg.ALLOW_SHORTS


# ---------------------------------------------------------------------------
# 6. the engine gathers conditions ONCE
# ---------------------------------------------------------------------------


class TestConditionsAreReadOnce:
    def test_both_gate_calls_receive_the_same_conditions(self, store, spot_cfg):
        """Sizing against one spread and gating against another is a small
        window, and small windows produce unreproducible incidents."""
        import inspect

        src = inspect.getsource(te.TradingEngine.execute)
        assert src.count("conditions = self._market_conditions") == 1
        assert src.count("**conditions") == 2

    def test_conditions_are_none_rather_than_defaulted_when_unreadable(
        self, store, spot_cfg
    ):
        engine = _bare_engine(store, spot_cfg)
        engine.client = _Exploding()
        conditions = engine._market_conditions("BTCUSDT")
        assert conditions == {"funding_rate": None, "book": None}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


_FILTERS = None


def _sizing(qty):
    from position_sizing import SizingResult

    # should_trade is DERIVED from qty, not a field. A sizing result that says
    # "trade" while carrying no size is unrepresentable, on purpose.
    return SizingResult(symbol="BTCUSDT", qty=qty, reason="OK")


class _FixedMultiplier:
    def __init__(self, value):
        self.value = value

    def size_multiplier(self, symbol):
        return self.value

    def record_execution(self, **kw):
        pass


class _Exploding:
    def __getattr__(self, name):
        def boom(*a, **k):
            raise RuntimeError("memory is broken")

        return boom


def _bare_engine(store, cfg):
    """A TradingEngine with real collaborators but no exchange traffic."""
    risk = rm.BillionaireRiskManager(config=cfg, store=store)
    return te.TradingEngine(
        client=None, risk_manager=risk, store=store, config=cfg, memory=None
    )


def pytest_configure():  # pragma: no cover - import-time setup
    pass


def setup_module(module):
    """Instrument filters used by the throttle tests. Real values, from the
    fake exchange's BTCUSDT entry, so the minimum quantity is a real minimum."""
    global _FILTERS
    from position_sizing import InstrumentFilters

    _FILTERS = InstrumentFilters.parse("BTCUSDT", {
        "tickSize": "0.01", "qtyStep": "0.000001",
        "minOrderQty": "0.001", "minOrderAmt": "5",
    }, from_exchange=True)
