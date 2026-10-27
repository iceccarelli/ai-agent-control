"""Measure what a market order costs, instead of assuming it.

The cost model charges `IMPACT_FLOOR_BPS_PER_LEG = 1.0` bps per leg: "declared
before the run, not measured" (carry_backtest.py). Each mainnet observation now
also records the real top of the public order book, so the assumption can be
compared with what Bybit's book actually charges for a market order of a given
size. This is a MEASUREMENT of one cost assumption. It is not part of the frozen
protocol verdict and does not change it; it says nothing about profit.

Offline: a fake GET answers; nothing here contacts a venue.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, BOT)

import mainnet_public as MP  # noqa: E402
from test_mainnet_protocol import DECLARED_HASH  # noqa: E402
from test_mainnet_public import Resp, market  # noqa: E402

MID = 100_000.0


def book(bid=99_999.5, ask=100_000.5, depth_btc=5.0, step=1.0, levels=50):
    """A synthetic public order book (v5 shape: b/a lists of [price, size])."""
    return {"s": "BTCUSDT", "ts": 1700000000000, "u": 1,
            "b": [[f"{bid - i * step}", f"{depth_btc}"] for i in range(levels)],
            "a": [[f"{ask + i * step}", f"{depth_btc}"] for i in range(levels)]}


def with_books(perp_book=None, spot_book=None, fail_book=False, **kw):
    base = market(**kw)

    def get(url, params, timeout):
        if url.endswith("/v5/market/orderbook"):
            get.calls.append((url, dict(params), timeout))
            if fail_book:
                raise TimeoutError("book unavailable")
            return Resp({"retCode": 0, "result": (
                perp_book if params.get("category") == "linear" else spot_book) or book()})
        return base(url, params, timeout)
    get.calls = base.calls
    return get


class TestTheWalk:
    """walk_impact_bps(levels, mid, notional, side): the VWAP a market order of
    `notional` USD gets walking the book, against the mid, in bps."""

    asks = [(100_001.0, 1.0), (100_003.0, 2.0)]
    bids = [(99_999.0, 1.0), (99_997.0, 2.0)]

    def test_a_small_order_pays_the_half_spread(self):
        # $1 off a $100,000 mid is 0.1 bps
        assert MP.walk_impact_bps(self.asks, MID, 1_000.0, "buy") == pytest.approx(0.1)
        assert MP.walk_impact_bps(self.bids, MID, 1_000.0, "sell") == pytest.approx(0.1)

    def test_a_bigger_order_walks_into_the_next_level(self):
        n = 100_001.0 + 100_003.0                      # all of level 1 plus 1 BTC of level 2
        qty = 1.0 + 1.0
        vwap = n / qty
        assert MP.walk_impact_bps(self.asks, MID, n, "buy") == pytest.approx(
            (vwap - MID) / MID * 1e4)

    def test_selling_is_priced_against_the_bids(self):
        n = 99_999.0 + 99_997.0
        vwap = n / 2.0
        assert MP.walk_impact_bps(self.bids, MID, n, "sell") == pytest.approx(
            (MID - vwap) / MID * 1e4)

    def test_an_order_the_book_cannot_fill_has_no_number_not_a_guess(self):
        assert MP.walk_impact_bps(self.asks, MID, 10_000_000.0, "buy") is None

    @pytest.mark.parametrize("bad", [0.0, -5.0, float("nan"), float("inf")])
    def test_a_nonsense_notional_has_no_number(self, bad):
        assert MP.walk_impact_bps(self.asks, MID, bad, "buy") is None

    def test_an_empty_book_or_a_bad_mid_has_no_number(self):
        assert MP.walk_impact_bps([], MID, 1_000.0, "buy") is None
        assert MP.walk_impact_bps(self.asks, 0.0, 1_000.0, "buy") is None
        assert MP.walk_impact_bps(self.asks, float("nan"), 1_000.0, "buy") is None

    def test_an_unknown_side_is_refused(self):
        with pytest.raises(ValueError):
            MP.walk_impact_bps(self.asks, MID, 1_000.0, "hold")


class TestTheSnapshot:
    def test_it_records_spread_and_impact_for_each_declared_size(self):
        snap = MP.book_snapshot(book(), notionals=(10_000, 100_000))
        assert snap["best_bid"] == 99_999.5 and snap["best_ask"] == 100_000.5
        assert snap["mid"] == pytest.approx(MID)
        assert snap["spread_bps"] == pytest.approx(1.0 / MID * 1e4)
        small = snap["impact_bps"]["10000"]
        assert small["buy"] == pytest.approx(0.5 / MID * 1e4, rel=0.05)
        assert snap["impact_bps"]["100000"]["buy"] >= small["buy"]

    def test_levels_are_sorted_whatever_order_the_venue_lists_them(self):
        shuffled = book()
        shuffled["a"] = list(reversed(shuffled["a"]))
        shuffled["b"] = list(reversed(shuffled["b"]))
        assert MP.book_snapshot(shuffled, notionals=(10_000,))["best_ask"] == 100_000.5

    @pytest.mark.parametrize("mutate", [
        lambda b: b.update(b=[]), lambda b: b.update(a=[]),
        lambda b: b.update(a=[["99999.0", "1"]]),                 # crossed
        lambda b: b.update(b=[["x", "1"]]),
        lambda b: b.update(b=[["100", "-1"]]),
        lambda b: b.update(a=[["nan", "1"]]),
    ])
    def test_a_broken_book_is_refused(self, mutate):
        b = book()
        mutate(b)
        with pytest.raises(MP.MainnetRefusal):
            MP.book_snapshot(b, notionals=(10_000,))

    def test_too_thin_a_book_leaves_that_size_empty_but_keeps_the_rest(self):
        thin = book(depth_btc=0.01, levels=3)
        snap = MP.book_snapshot(thin, notionals=(10, 100_000_000))
        assert snap["impact_bps"]["10"]["buy"] is not None
        assert snap["impact_bps"]["100000000"]["buy"] is None


class TestObserveRecordsTheBook:
    def test_the_observation_carries_both_books(self):
        obs = MP.MainnetPublicReader(http_get=with_books()).observe()
        assert set(obs["book"]) == {"linear", "spot"}
        assert obs["book"]["linear"]["impact_bps"]["100000"]["sell"] is not None

    def test_it_asks_the_public_orderbook_with_a_get_and_no_key(self):
        get = with_books()
        MP.MainnetPublicReader(http_get=get).observe()
        asked = [(u, p) for u, p, _ in get.calls if u.endswith("/v5/market/orderbook")]
        assert {p["category"] for _, p in asked} == {"linear", "spot"}
        assert all(p["symbol"] == "BTCUSDT" and int(p["limit"]) == MP.BOOK_LEVELS
                   for _, p in asked)
        for _, p in asked:
            assert not {k.lower() for k in p} & {"api_key", "sign", "timestamp"}

    def test_a_failed_book_read_does_not_cost_the_observation(self):
        obs = MP.MainnetPublicReader(http_get=with_books(fail_book=True)).observe()
        assert obs["book"] is None and "TimeoutError" in obs["book_error"]
        assert obs["perp_mark"] == 100_000.0 and obs["settled_prints"]

    def test_a_broken_book_is_recorded_as_an_error_not_as_numbers(self):
        crossed = book()
        crossed["a"] = [["99000.0", "1"]]
        obs = MP.MainnetPublicReader(http_get=with_books(perp_book=crossed)).observe()
        assert obs["book"] is None and "MainnetRefusal" in obs["book_error"]

    def test_the_public_paths_are_exactly_these_four(self):
        assert MP.PUBLIC_PATHS == ("/v5/market/time", "/v5/market/tickers",
                                   "/v5/market/funding/history", "/v5/market/orderbook")

    def test_the_default_get_still_refuses_any_trading_path(self, monkeypatch):
        monkeypatch.setattr(MP.urllib.request, "build_opener",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
        for path in ("/v5/order/create", "/v5/position/list",
                     "/v5/market/orderbook/../../order/create"):
            with pytest.raises(MP.MainnetRefusal):
                MP.default_get("https://api.bybit.com" + path, {}, 1.0)


class TestTheFrozenProtocolIsUntouched:
    def test_the_hash_is_the_declared_one(self):
        assert MP.protocol_hash() == DECLARED_HASH

    def test_the_book_does_not_enter_the_verdict(self, tmp_path):
        db = str(tmp_path / "m.jsonl")
        MP.append(db, MP.MainnetPublicReader(http_get=with_books()).observe())
        r = MP.report(db)
        assert r["protocol"]["verdict"] == "INSUFFICIENT_REAL_MARKET_SAMPLE"
        rep = {"observations": 600, "span_hours": 40 * 24.0, "chain_issues": [],
               "gate": {"open_fraction": 0.0}, "slippage": {"anything": 1}}
        assert MP.protocol_verdict(rep) == "GATE_RARELY_OPEN_ON_REAL_MARKET"


class TestTheSlippageReport:
    def filled(self, tmp_path, n, **bookkw):
        db = str(tmp_path / "m.jsonl")
        for i in range(n):
            MP.append(db, MP.MainnetPublicReader(
                http_get=with_books(perp_book=book(**bookkw),
                                    spot_book=book(**bookkw))).observe())
        return db

    def test_the_assumption_is_the_cost_models_own_floor(self):
        import carry_backtest as cb
        assert MP.ASSUMED_IMPACT_BPS_PER_LEG == cb.IMPACT_FLOOR_BPS_PER_LEG

    def test_it_reports_measured_impact_against_the_assumption(self, tmp_path):
        s = MP.report(self.filled(tmp_path, 3))["slippage"]
        assert s["observations_with_book"] == 3
        assert s["assumed_bps_per_leg"] == 1.0
        leg = s["per_leg"]["100000"]["linear_sell"]
        assert leg["n"] == 3 and leg["median"] >= 0.0 and leg["p95"] >= leg["median"]
        assert 0.0 <= s["share_of_legs_above_assumption"] <= 1.0

    def test_a_thin_book_is_reported_as_costing_more_than_assumed(self, tmp_path):
        s = MP.report(self.filled(tmp_path, 2, depth_btc=0.2, step=20.0))["slippage"]
        assert s["share_of_legs_above_assumption"] > 0.5
        assert s["round_trip_bps"]["100000"]["median"] > 4 * MP.ASSUMED_IMPACT_BPS_PER_LEG

    def test_the_round_trip_is_the_sum_of_the_four_legs(self, tmp_path):
        s = MP.report(self.filled(tmp_path, 1))["slippage"]
        legs = s["per_leg"]["100000"]
        total = (legs["linear_sell"]["median"] + legs["spot_buy"]["median"]
                 + legs["linear_buy"]["median"] + legs["spot_sell"]["median"])
        assert s["round_trip_bps"]["100000"]["median"] == pytest.approx(total)

    def test_each_leg_comes_from_its_own_book(self, tmp_path):
        db = str(tmp_path / "m.jsonl")
        deep, thin = book(depth_btc=50.0), book(depth_btc=0.3, step=30.0)
        MP.append(db, MP.MainnetPublicReader(
            http_get=with_books(perp_book=deep, spot_book=thin)).observe())
        legs = MP.report(db)["slippage"]["per_leg"]["100000"]
        want_spot = MP.book_snapshot(thin)["impact_bps"]["100000"]
        want_perp = MP.book_snapshot(deep)["impact_bps"]["100000"]
        assert legs["spot_buy"]["median"] == pytest.approx(want_spot["buy"])
        assert legs["spot_sell"]["median"] == pytest.approx(want_spot["sell"])
        assert legs["linear_sell"]["median"] == pytest.approx(want_perp["sell"])
        assert legs["linear_buy"]["median"] == pytest.approx(want_perp["buy"])
        assert legs["spot_buy"]["median"] > legs["linear_sell"]["median"]

    def test_a_size_one_leg_cannot_fill_has_no_round_trip_for_that_size(self, tmp_path):
        """Deep asks but thin bids on spot: spot BUY prices, spot SELL cannot. The
        round trip needs all four legs, so it must be empty, never a 3-leg sum."""
        db = str(tmp_path / "m.jsonl")
        lopsided = book(depth_btc=50.0)
        lopsided["b"] = [[p, "0.002"] for p, _ in lopsided["b"][:3]]
        MP.append(db, MP.MainnetPublicReader(
            http_get=with_books(perp_book=book(depth_btc=50.0), spot_book=lopsided)).observe())
        s = MP.report(db)["slippage"]
        assert s["per_leg"]["100000"]["spot_buy"] is not None
        assert s["per_leg"]["100000"]["spot_sell"] is None
        assert s["round_trip_bps"]["100000"] is None
        assert s["observations_with_book"] == 1

    def test_it_labels_itself_a_measurement_not_a_verdict_or_a_profit_claim(self, tmp_path):
        s = MP.report(self.filled(tmp_path, 1))["slippage"]
        note = s["what_it_is"].lower()
        assert "measurement" in note and "not a verdict" in note and "not profit" in note

    def test_rows_without_a_book_are_counted_not_guessed(self, tmp_path):
        db = str(tmp_path / "m.jsonl")
        MP.append(db, MP.MainnetPublicReader(http_get=with_books(fail_book=True)).observe())
        s = MP.report(db)["slippage"]
        assert s["observations_with_book"] == 0 and "per_leg" not in s

    def test_an_empty_file_has_no_slippage_section_to_misread(self, tmp_path):
        assert "slippage" not in MP.report(str(tmp_path / "none.jsonl"))


class TestTheStatusScriptShowsIt:
    def test_the_measured_cost_is_printed_beside_the_assumption(self, tmp_path):
        from test_mainnet_collect import TestTheStatusScriptSection as T
        db = str(tmp_path / "m.jsonl")
        for _ in range(2):
            MP.append(db, MP.MainnetPublicReader(http_get=with_books()).observe())
        out = T().run(tmp_path, json.dumps(MP.report(db))).stdout
        assert "measured slippage" in out and "assumed 1.0 bps per leg" in out
        assert "round trip" in out and "not a verdict" in out
