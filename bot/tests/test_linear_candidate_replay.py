"""`tools/linear_candidate_replay.py`: the candidate's schedule as linear orders.

Synthetic tests pin the driver (entry, verified stop, resting target, time
stop, bracket failure, reconciliation, restart, determinism). One test reads
the committed corpus and checks the four forward trades are the forward-shadow
artefact's four, and that the output is labelled NOT independent confirmation.
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pytest

HERE = os.path.dirname(__file__)
REPO = os.path.join(HERE, "..")
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import backtest as bt                                    # noqa: E402
import linear_candidate_replay as lcr                    # noqa: E402
from signals import funding_carry_fade_btc_v1 as fb      # noqa: E402

DAY = 86_400_000
T0 = 1_700_006_400_000            # a UTC midnight
assert T0 % DAY == 0


def series(prices, *, wick=0.002):
    out = []
    prev = prices[0]
    for i, p in enumerate(prices):
        out.append(bt.Bar(T0 + i * DAY, prev, max(prev, p) * (1 + wick),
                          min(prev, p) * (1 - wick), p, 1_000.0))
        prev = p
    return out


def params(**kw):
    base = dict(notional_usd=1_000.0, starting_cash=2_000.0, leverage=1.0,
                taker_fee_bps=5.5, maker_fee_bps=2.0, slippage_bps=5.0,
                spread_bps=1.0)
    base.update(kw)
    return lcr.ReplayParams(**base)


def replay(bars, schedule, *, atr=2_000.0, funding=None, **kw):
    funding = funding if funding is not None else [
        (T0 + k * 8 * 3_600_000 + 3, 0.0001) for k in range(3 * len(bars))]
    return lcr.CandidateReplay(bars, funding, schedule, params(**kw),
                               atr=np.full(len(bars), atr))


def short_at(index, segment="late_oos"):
    return lcr.Candidate(index, fb.SHORT_SETUP, segment, 0.0, fb.HORIZON)


FLAT = [50_000.0] * 20


class TestTheDriver:
    def test_a_quiet_trade_exits_on_the_time_stop_after_horizon(self):
        r = replay(series(FLAT), [short_at(3)])
        r.run()
        (t,) = r.trades
        assert t["exit_kinds"] == ["time"]
        assert t["bars_held"] == 1 + fb.HORIZON
        assert t["stop_verified"] is True and t["unprotected_bars"] == 0
        # qty floored to 0.001: 1000 / 50000 = 0.02
        assert t["qty"] == pytest.approx(0.02)
        # a short receives the positive rate at every settlement it held:
        # 3 per day, opened at the close of bar 3, closed at the close of bar
        # 9 -> settlements at the starts of bars 4..9 and inside them
        assert t["funding_usd"] > 0
        # ledger identity checked inside the driver; here: net decomposes
        assert t["net_usd"] == pytest.approx(
            t["gross_after_slippage_usd"] - t["fees_usd"] + t["funding_usd"])
        # entry slippage + half spread: 0.02 * 50000 * (5 + 0.5) bps, twice
        assert t["slippage_and_spread_usd"] == pytest.approx(
            2 * 0.02 * 50_000.0 * 0.00055, rel=1e-6)

    def test_the_stop_is_on_the_position_at_ref_plus_stop_atr(self):
        prices = FLAT[:5] + [56_000.0] + FLAT[6:]
        r = replay(series(prices), [short_at(3)])
        r.run()
        (t,) = r.trades
        assert t["stop"] == pytest.approx(50_000.0 + fb.STOP_ATR * 2_000.0)
        assert t["exit_kinds"] == ["stop"]
        assert t["net_r"] < -1.0                     # costs on top of -1R

    def test_the_target_rests_and_fills_at_the_maker_fee(self):
        prices = FLAT[:5] + [46_000.0] + FLAT[6:]
        r = replay(series(prices), [short_at(3)])
        r.run()
        (t,) = r.trades
        assert t["exit_kinds"] == ["tp"]
        assert t["take_profit"] == pytest.approx(
            50_000.0 - fb.TAKE_PROFIT_ATR * 2_000.0)
        assert 0.9 < t["net_r"] < 1.0

    def test_a_notional_below_one_lot_is_skipped_not_rounded_up(self):
        r = replay(series(FLAT), [short_at(3)], notional_usd=40.0)
        r.run()
        assert r.trades == []
        assert r.skipped[0]["reason"] == "notional_below_min_qty"

    def test_an_overlapping_candidate_is_skipped(self):
        r = replay(series(FLAT), [short_at(3), short_at(5)])
        r.run()
        assert len(r.trades) == 1
        assert r.skipped == [{"signal_index": 5, "segment": "late_oos",
                              "reason": "position_open"}]


class TestFailClosed:
    def test_a_stop_that_cannot_be_written_closes_and_halts(self):
        r = replay(series(FLAT), [short_at(3), short_at(12)])
        r.ex.fail_next_trading_stops = 1
        r.run()
        (t,) = r.trades
        assert t["stop_verified"] is False
        assert t["exit_kinds"] == ["bracket_failure"]
        assert r.halted and "bracket failure" in r.halted
        assert r.skipped[-1]["reason"] == "halted"
        assert set(r.ex.balances) == {"USDT"}

    def test_a_book_that_disagrees_with_the_venue_raises(self):
        r = replay(series(FLAT), [short_at(3)])
        r.run(stop_after_bar=5)
        r.book += 0.001
        with pytest.raises(bt.PositionReconciliationError):
            r.run()


class TestRestartAndDeterminism:
    PRICES = [50_000 * (1 + 0.03 * np.sin(i / 2.0)) for i in range(40)]

    def _schedule(self):
        return [short_at(3), short_at(11), short_at(19, "forward"), short_at(27)]

    def test_two_runs_are_identical(self):
        a = replay(series(self.PRICES), self._schedule())
        b = replay(series(self.PRICES), self._schedule())
        a.run()
        b.run()
        assert len(a.trades) >= 3
        assert json.dumps(lcr.summarise(a), sort_keys=True) == \
            json.dumps(lcr.summarise(b), sort_keys=True)

    def test_a_resumed_replay_is_identical(self):
        bars = series(self.PRICES)
        funding = [(T0 + k * 8 * 3_600_000 + 5, 0.0001 * ((-1) ** k))
                   for k in range(3 * len(bars))]
        straight = replay(bars, self._schedule(), funding=funding)
        straight.run()

        part = replay(bars, self._schedule(), funding=funding)
        part.run(stop_after_bar=14)                  # mid-trade
        assert part.open is not None
        state = json.loads(json.dumps(part.to_state()))
        resumed = lcr.CandidateReplay.from_state(
            bars, funding, self._schedule(), params(), state,
            atr=np.full(len(bars), 2_000.0))
        resumed.run()
        assert json.dumps(lcr.summarise(resumed), sort_keys=True) == \
            json.dumps(lcr.summarise(straight), sort_keys=True)
        assert resumed.ex.to_state() == straight.ex.to_state()


class TestOnTheCommittedCorpus:
    def test_the_forward_trades_are_the_artefacts_and_it_is_labelled(self):
        folds = fb.load_folds()
        bars, funding = lcr.load_inputs(
            os.path.join(REPO, "data", "real_linear_1d"),
            os.path.join(REPO, "data", "real_funding", "funding",
                         "BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz"))
        schedule = lcr.build_schedule(bars, funding, folds)
        r = lcr.CandidateReplay(
            bars, list(zip(funding.times_ms, funding.rates)), schedule,
            params(notional_usd=10_000.0, starting_cash=20_000.0))
        r.run()
        out = lcr.summarise(r)
        assert out["is_independent_confirmation"] is False
        assert "NOT INDEPENDENT" in out["label"]
        forward = [t for t in out["trades"] if t["segment"] == "forward"]
        with open(os.path.join(REPO, "artifacts",
                               "forward_shadow_current.json")) as handle:
            artefact = json.load(handle)
        entries = [t["entry_utc"][:10] for t in artefact["trades"]]
        signals = [t["signal_utc"][:10] for t in forward]
        assert len(signals) == len(entries) == 4
        # the venue entry fills at the decision bar's close == next bar's open
        import datetime as dt
        for signal, entry in zip(signals, entries):
            assert (dt.date.fromisoformat(signal) + dt.timedelta(days=1)
                    == dt.date.fromisoformat(entry))
        assert all(t["stop_verified"] for t in out["trades"])
        assert out["account"]["funding_settlements_without_print"] == 0
        assert out["account"]["liquidations"] == 0
