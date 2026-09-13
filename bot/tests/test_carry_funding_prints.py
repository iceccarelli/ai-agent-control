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


def opened(bps=3.0):
    """An engine that has seen prints 1..3 and opened a $100 pair at print 3."""
    e = CarryEngine(broker=Venue(), max_notional_usd=100.0, borrow_apr=0.05)
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
        e = CarryEngine(broker=Venue(), max_notional_usd=100.0, borrow_apr=0.05)
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
        e = CarryEngine(broker=Venue(), max_notional_usd=100.0, borrow_apr=0.05)
        for _ in range(10):
            e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                        funding_print_ms=H8)
        assert e._funding_history == [3.0]

    def test_without_a_stamp_nothing_is_a_print(self):
        """No stamp, no print: nothing booked, and the warm-up never completes,
        so the book cannot open on data it cannot place in time."""
        e = CarryEngine(broker=Venue(), max_notional_usd=100.0, borrow_apr=0.05)
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
                            borrow_apr=0.0)
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
