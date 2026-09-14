"""0034 — a funding print is a print, not a tick.

INVENTORY D1: main.tick calls on_candle every LOOP_INTERVAL_SECONDS (60 s)
with the ticker's PREDICTED rate, and the engine booked that rate into
funding_collected on every call — 60 ticks at 1 bps on $100 booked $0.60,
where one real 8h print is $0.01. The negative-funding streak and the EWMA
counted ticks too: three negative MINUTES unwound the book.

INVENTORY D10: negative prints were paid and never booked.
INVENTORY F6: the ticker's fundingRate is the NEXT settlement's rate; the
settled print lives at /v5/market/funding/history.
"""
from __future__ import annotations

import os
import sys
import time
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main as _main  # noqa: E402
import market_snapshot as ms  # noqa: E402
from carry_broker import CarryBroker, PairIncident  # noqa: E402
from carry_engine import CarryEngine  # noqa: E402
from carry_risk import CarryRisk  # noqa: E402
from market_snapshot import MarketSnapshot, StaleMarket  # noqa: E402

MARK = 100_000.0
H8 = 8 * 3600 * 1000


class Venue:
    def __init__(self):
        self.orders = []

    def place_market(self, *, symbol, side, qty, product):
        self.orders.append((side, product))
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


def opened(bps=3.0):
    """An engine that has seen prints 1..3 and opened a $100 pair at print 3."""
    e = CarryEngine(broker=Venue(), max_notional_usd=100.0, borrow_apr=0.05,
                    execution_mode="acquire", persist=lambda s: None)
    e.pair_risk = CarryRisk(max_notional_usd=100.0)
    e.snapshot = MarketSnapshot(perp_mark=MARK, spot_mark=MARK,
                                funding_bps=bps, margin_multiple=5.0,
                                observed_at_s=time.time())
    for k in (1, 2):
        e.on_candle(mark=MARK, funding_bps=bps, spot=MARK,
                    funding_print_ms=k * H8)
    d = e.on_candle(mark=MARK, funding_bps=bps, spot=MARK,
                    funding_print_ms=3 * H8)
    assert d.action == "opened", d.reason
    return e


class TestAFreshBookIsNotBlindForADay:
    """0045 — the EWMA starts empty, and the venue has been publishing the
    whole time.

    `evaluate_entry` refuses below EWMA_MIN_PRINTS settled prints. Prints come
    every eight hours. So a new deploy — a new volume, a first run, a machine
    the host moved — stood aside for 24 hours with
    INSUFFICIENT_FUNDING_HISTORY while `/v5/market/funding/history` was
    returning the last two hundred prints on request.

    There is no look-ahead in reading them: every one is SETTLED, already
    paid, exactly what `on_candle` would have recorded had the process been
    running. It is catching up, not peeking.
    """

    def _engine(self):
        # acquire, matching this file's Venue, which has no wallet read
        e = CarryEngine(broker=Venue(), max_notional_usd=100.0,
                        borrow_apr=0.0, execution_mode="acquire",
                        persist=lambda s: None)
        e.pair_risk = CarryRisk(max_notional_usd=100.0)
        e.snapshot = MarketSnapshot(perp_mark=MARK, spot_mark=MARK,
                                    funding_bps=3.0, margin_multiple=5.0,
                                    observed_at_s=time.time())
        return e

    def test_a_cold_engine_stands_aside(self):
        e = self._engine()
        d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                        funding_print_ms=9 * H8)
        assert d.reason == "INSUFFICIENT_FUNDING_HISTORY"

    def test_a_warmed_engine_can_decide_immediately(self):
        e = self._engine()
        assert e.warm_funding_history(
            [(3.0, k * H8) for k in range(1, 9)]) == 8
        d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                        funding_print_ms=9 * H8, timestamp_ms=9 * H8)
        assert d.reason != "INSUFFICIENT_FUNDING_HISTORY"
        assert d.action == "opened", d.reason

    def test_the_newest_warmed_print_is_not_collected_again(self):
        """Its stamp becomes the watermark. Without that the book books a
        print it was not holding through."""
        e = self._engine()
        e.warm_funding_history([(3.0, k * H8) for k in range(1, 9)])
        assert e._last_print_ms == 8 * H8

    def test_a_restored_history_is_never_overwritten(self):
        """A restart that read its own ledger knows more than the venue's last
        eight, including which prints this book actually held through."""
        e = self._engine()
        e.restore({"book_state": "FLAT", "position": None,
                   "last_print_ms": 5 * H8, "funding_history": [1.0, 2.0]})
        assert e.warm_funding_history([(9.9, k * H8) for k in range(1, 9)]) == 0
        assert e._funding_history == [1.0, 2.0]
        assert e._last_print_ms == 5 * H8

    def test_prints_are_sorted_and_truncated(self):
        e = self._engine()
        e.warm_funding_history([(float(k), k * H8) for k in (5, 1, 9, 3, 12,
                                                             7, 2, 11, 4, 6)])
        assert len(e._funding_history) == e.FUNDING_HISTORY
        assert e._funding_history[-1] == 12.0
        assert e._last_print_ms == 12 * H8

    def test_malformed_rows_are_dropped_not_booked(self):
        e = self._engine()
        taken = e.warm_funding_history(
            [(float("nan"), H8), (3.0, 2 * H8), (1.0, 0), (2.0, 3 * H8)])
        assert taken == 2
        assert e._funding_history == [3.0, 2.0]

    def test_nothing_usable_leaves_it_cold_rather_than_guessing(self):
        e = self._engine()
        assert e.warm_funding_history([]) == 0
        assert e._funding_history == []
        assert e._last_print_ms is None


class TestFundingIsPaidOnWhatThePositionIsWorthNow:
    """0043 — the venue pays funding on the position's value at the SETTLEMENT
    mark, not at the price the position was opened at.

    `funding_collected` booked `perp.notional`, which is `filled_qty *
    avg_price` and is frozen at the fill. Replaying the engine over the Bybit
    settlement corpus booked $29,371 where the same 79 trades earn $39,444 —
    **26% of the funding missing**, because BTC rose while the positions were
    held and the entry price never moved.

    It changes no decision: nothing gates on `funding_collected`. It is the
    number a client would be shown as "funding collected", and Phase E calls
    itself a bankable ledger.

    The error's SIGN follows the price. A book whose whole claim is that price
    direction does not matter must not report a P&L whose error is a function
    of price direction.
    """

    def test_a_doubled_mark_doubles_the_print(self):
        e = opened()
        e.on_candle(mark=2 * MARK, funding_bps=1.0, spot=2 * MARK,
                    funding_print_ms=4 * H8)
        qty = e.position.perp.filled_qty
        assert e.position.funding_collected == pytest.approx(
            1.0 / 1e4 * qty * 2 * MARK)

    def test_a_halved_mark_halves_it(self):
        """The other direction, so the fix cannot be a constant that happens
        to suit a rising corpus."""
        e = opened()
        e.on_candle(mark=MARK / 2, funding_bps=1.0, spot=MARK / 2,
                    funding_print_ms=4 * H8)
        qty = e.position.perp.filled_qty
        assert e.position.funding_collected == pytest.approx(
            1.0 / 1e4 * qty * MARK / 2)

    def test_the_entry_price_is_not_what_is_paid_on(self):
        e = opened()
        e.on_candle(mark=3 * MARK, funding_bps=2.0, spot=3 * MARK,
                    funding_print_ms=4 * H8)
        on_entry = 2.0 / 1e4 * e.position.perp.notional
        assert e.position.funding_collected != pytest.approx(on_entry)

    def test_a_negative_print_is_still_signed_and_marked(self):
        """D10 must survive the fix: a print that is PAID is booked, and it is
        booked on the current mark too."""
        e = opened()
        e.on_candle(mark=2 * MARK, funding_bps=-1.5, spot=2 * MARK,
                    funding_print_ms=4 * H8)
        qty = e.position.perp.filled_qty
        assert e.position.funding_collected == pytest.approx(
            -1.5 / 1e4 * qty * 2 * MARK)
        assert e.position.negative_funding_streak == 1

    def test_an_unusable_mark_does_not_silently_book_zero(self):
        """A mark the engine cannot use must not turn a real funding payment
        into a $0.00 line in the ledger."""
        e = opened()
        before = e.position.funding_collected
        e.on_candle(mark=float("nan"), funding_bps=1.0, spot=MARK,
                    funding_print_ms=4 * H8)
        assert e.position.funding_collected == before or \
            e.state.name == "HALTED"


class TestOnePrintIsBookedOnce:
    def test_sixty_ticks_of_one_print_book_it_once(self):
        e = opened()
        for _ in range(60):
            e.on_candle(mark=MARK, funding_bps=1.0, spot=MARK,
                        funding_print_ms=4 * H8)
        assert e.position.funding_collected == pytest.approx(
            1.0 / 1e4 * e.position.perp.notional)

    def test_the_entry_print_is_not_collected(self):
        """The book was not holding at the settlement that triggered entry."""
        e = opened()
        for _ in range(5):
            e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                        funding_print_ms=3 * H8)
        assert e.position.funding_collected == 0.0

    def test_a_print_stamped_before_the_open_is_not_booked(self):
        e = CarryEngine(broker=Venue(), max_notional_usd=100.0, borrow_apr=0.05,
                    execution_mode="acquire", persist=lambda s: None)
        e.pair_risk = CarryRisk(max_notional_usd=100.0)
        e.snapshot = MarketSnapshot(perp_mark=MARK, spot_mark=MARK,
                                    funding_bps=3.0, margin_multiple=5.0,
                                    observed_at_s=time.time())
        for k in (1, 2, 3):
            e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                        funding_print_ms=k * H8, timestamp_ms=10 * H8)
        assert e.position is not None
        # A late-arriving record for an older settlement is history, not income.
        e._last_print_ms = 0
        e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                    funding_print_ms=5 * H8, timestamp_ms=10 * H8 + 1)
        assert e.position.funding_collected == 0.0

    def test_negative_prints_are_booked_as_paid(self):
        e = opened()
        e.on_candle(mark=MARK, funding_bps=2.0, spot=MARK, funding_print_ms=4 * H8)
        e.on_candle(mark=MARK, funding_bps=-0.5, spot=MARK, funding_print_ms=5 * H8)
        n = e.position.perp.notional
        assert e.position.funding_collected == pytest.approx((2.0 - 0.5) / 1e4 * n)


class TestTheStreakCountsPrints:
    def test_three_negative_ticks_of_one_print_do_not_unwind(self):
        e = opened()
        for _ in range(3):
            d = e.on_candle(mark=MARK, funding_bps=-0.5, spot=MARK,
                            funding_print_ms=4 * H8)
        assert e.position is not None
        assert d.action == "hold"

    def test_three_negative_prints_unwind(self):
        e = opened()
        for k in (4, 5, 6):
            d = e.on_candle(mark=MARK, funding_bps=-0.5, spot=MARK,
                            funding_print_ms=k * H8)
        assert d.action == "unwound"
        assert d.reason == "FUNDING_INVERTED"


class TestTheEwmaSeesPrints:
    def test_ten_ticks_of_one_print_are_one_observation(self):
        e = CarryEngine(broker=Venue(), max_notional_usd=100.0, borrow_apr=0.05,
                    execution_mode="acquire", persist=lambda s: None)
        for _ in range(10):
            e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                        funding_print_ms=H8)
        assert e._funding_history == [3.0]

    def test_without_a_stamp_nothing_is_a_print(self):
        """No stamp, no print: nothing booked, and the warm-up never completes,
        so the book cannot open on data it cannot place in time."""
        e = CarryEngine(broker=Venue(), max_notional_usd=100.0, borrow_apr=0.05,
                    execution_mode="acquire", persist=lambda s: None)
        e.pair_risk = CarryRisk(max_notional_usd=100.0)
        for _ in range(10):
            d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK)
        assert e._funding_history == []
        assert d.reason == "INSUFFICIENT_FUNDING_HISTORY"
        assert e.broker.orders == []


class _Client:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def _request(self, method, endpoint, *, params=None, body=None,
                 signed=False, retries=3):
        self.calls.append((method, endpoint, params))
        if endpoint == "/v5/market/funding/history":
            return {"list": self.rows}
        return {}


def _broker(rows):
    return CarryBroker(client=_Client(rows), sequence_source=lambda *a: 1,
                       order_gate=lambda: (True, "TEST"))


class TestTheBrokerReadsTheSettledPrint:
    def test_it_reads_funding_history_not_the_ticker(self):
        b = _broker([{"symbol": "BTCUSDT", "fundingRate": "-0.00001",
                      "fundingRateTimestamp": str(5 * H8)}])
        bps, stamp = b.get_funding_print("BTCUSDT")
        assert bps == pytest.approx(-0.1)
        assert stamp == 5 * H8
        method, endpoint, params = b.client.calls[0]
        assert endpoint == "/v5/market/funding/history"
        assert params["category"] == "linear" and params["limit"] == 1

    @pytest.mark.parametrize("rows", [
        [], [{"fundingRate": "0.0001"}],
        [{"fundingRate": "nan", "fundingRateTimestamp": str(H8)}],
        [{"fundingRate": "0.0001", "fundingRateTimestamp": "0"}]])
    def test_an_unreadable_print_raises(self, rows):
        with pytest.raises(PairIncident):
            _broker(rows).get_funding_print("BTCUSDT")


class _SnapBroker:
    def __init__(self, stamp_ms):
        self.stamp_ms = stamp_ms

    def get_mark(self, s):
        return MARK

    def get_spot_mark(self, s):
        return MARK

    def get_funding_bps(self, s):
        return 5.0            # the PREDICTED rate

    def get_funding_print(self, s):
        return (-1.0, self.stamp_ms)   # the SETTLED print

    def get_margin_multiple(self, s):
        return 5.0

    def get_lot_rules(self, symbol, product):
        return {"qty_step": 1e-6, "min_qty": 1e-6, "min_notional": 0.0}

    def get_fee_rates(self, symbol, product):
        return {"maker_bps": 2.0,
                "taker_bps": 10.0 if product == "spot" else 5.5}


def _latest_settlement_ms(now_s):
    return int(now_s * 1000) // H8 * H8


class TestTheSnapshotCarriesTheSettledPrint:
    def test_both_rates_are_kept_and_named(self):
        now = time.time()
        s = ms.take_snapshot(_SnapBroker(_latest_settlement_ms(now)),
                             perp_symbol="BTCUSDT", spot_symbol="BTCUSDT",
                             now_s=now)
        assert s.funding_bps == 5.0
        assert s.funding_print_bps == -1.0
        assert s.funding_print_ms == _latest_settlement_ms(now)
        s.assert_fresh()

    def test_a_broker_without_the_print_cannot_snapshot(self):
        class Old(_SnapBroker):
            get_funding_print = None
        with pytest.raises(TypeError):
            ms.take_snapshot(Old(0), perp_symbol="BTCUSDT",
                             spot_symbol="BTCUSDT")

    def test_a_missed_settlement_is_a_stale_view(self):
        """The last settled print is older than one funding interval (plus
        the snapshot's own age limit): a settlement has been missed and the
        EWMA is built on history the venue has moved past."""
        now = time.time()
        stamp = int(now * 1000) - ms.FUNDING_INTERVAL_MS - 10 * 60 * 1000
        s = ms.take_snapshot(_SnapBroker(stamp), perp_symbol="BTCUSDT",
                             spot_symbol="BTCUSDT", now_s=now)
        with pytest.raises(StaleMarket, match="settlement"):
            s.assert_fresh()

    def test_a_print_inside_the_interval_is_fresh(self):
        now = time.time()
        stamp = int(now * 1000) - ms.FUNDING_INTERVAL_MS + 10 * 60 * 1000
        ms.take_snapshot(_SnapBroker(stamp), perp_symbol="BTCUSDT",
                         spot_symbol="BTCUSDT", now_s=now).assert_fresh()

    def test_a_print_stamped_in_the_future_is_refused(self):
        now = time.time()
        stamp = int(now * 1000) + 10 * 60 * 1000
        s = ms.take_snapshot(_SnapBroker(stamp), perp_symbol="BTCUSDT",
                             spot_symbol="BTCUSDT", now_s=now)
        with pytest.raises(StaleMarket, match="future"):
            s.assert_fresh()


class _TickBroker(Venue, _SnapBroker):
    def __init__(self, stamp_ms):
        Venue.__init__(self)
        _SnapBroker.__init__(self, stamp_ms)

    def get_funding_print(self, s):
        return (2.0, self.stamp_ms)


def _bot(broker):
    bot = _main.TradingBot.__new__(_main.TradingBot)
    bot.symbols = ["BTCUSDT"]
    bot.store = mock.Mock(is_kill_switch_engaged=lambda: (False, ""))
    bot.strategy = None
    bot._stop = mock.Mock(is_set=lambda: False)
    bot.session = mock.Mock()
    bot.client = mock.Mock(get_equity=lambda: 1_000_000.0)
    bot.risk = mock.Mock(should_halt_trading=lambda: False,
                         update_equity=lambda e: None)
    bot.engine = mock.Mock(observe_exits=lambda: {})
    bot.carry = CarryEngine(broker=broker, max_notional_usd=100.0,
                            borrow_apr=0.0, execution_mode="acquire",
                            persist=lambda s: None)
    bot.carry.pair_risk = CarryRisk(max_notional_usd=100.0)
    return bot


class TestTheTickPassesTheSettledPrint:
    def test_the_engine_sees_the_settled_rate_not_the_predicted_one(self):
        latest = _latest_settlement_ms(time.time())
        broker = _TickBroker(latest)
        bot = _bot(broker)
        bot.tick()
        assert bot.carry._funding_history == [2.0]      # settled, not 5.0

    def test_sixty_ticks_inside_one_settlement_window_book_one_print(
            self, monkeypatch):
        latest = _latest_settlement_ms(time.time())
        broker = _TickBroker(latest)
        bot = _bot(broker)
        for k in (2, 1):                 # two earlier prints, already settled
            bot.carry.on_candle(mark=MARK, funding_bps=2.0, spot=MARK,
                                funding_print_ms=latest - k * H8)
        bot.tick()                       # the third print: the pair opens
        assert bot.carry.position is not None
        # Eight hours pass. The next settlement prints; the loop ticks sixty
        # times inside that window. It is ONE print.
        later = (latest + H8) / 1000.0 + 60.0
        monkeypatch.setattr(time, "time", lambda: later)
        broker.stamp_ms = latest + H8
        for _ in range(60):
            bot.tick()
        assert bot.carry.position.funding_collected == pytest.approx(
            2.0 / 1e4 * bot.carry.position.perp.notional)
