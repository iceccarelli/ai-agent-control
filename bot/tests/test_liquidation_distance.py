"""0048 — F4: the margin gate measured the wrong thing.

WHAT F4 SAYS, AND WHY IT MATTERS MORE THAN THE REST
===================================================
`CarryEngine` guards the short with `positionIM / positionMM` and a floor of
2.0. That ratio is a LEVERAGE / RISK-TIER quantity: at Bybit's tier 1 it is
0.66% over 0.33%, so it sits near 2.0 by construction and barely moves as the
price does. It is not headroom. **A short being squeezed would not trip it.**

Every other open item in INVENTORY costs money slowly — a fee booked wrong, a
number quoted too confidently. This one loses the position: a liquidated perp
leg leaves the client naked long in exactly the market that just moved against
them, which is the single failure this book exists to prevent.

WHAT THE VENUE ACTUALLY GIVES YOU
=================================
`/v5/position/list` returns `liqPrice` — the estimated liquidation price — and
`/v5/account/wallet-balance` returns `accountMMRate`, which is what UTA cross
margin liquidates on. Distance from mark to `liqPrice`, as a percentage, is a
number a human can read: *the price can move X% against this before it is
gone.*

`liqPrice` is `""` when it falls outside the venue's price bounds. That is SAFE
— there is no reachable liquidation price — and it is recorded as that rather
than converted into a number nobody measured.

THE FLOOR, MEASURED RATHER THAN CHOSEN
======================================
Over the 4,500-settlement Bybit corpus:

    worst 8h close-to-close move against a short   8.00%
    worst intra-settlement high above the close   11.52%
    worst 24h                                     17.93%

`MIN_LIQUIDATION_DISTANCE_PCT = 15.0` sits above both of the first two. It is
not a guess and it is not tuned to make anything pass.

WHY IT IS A HOLD-TIME CHECK
===========================
Before the book opens there is no position, so the venue has no `liqPrice` to
report. This cannot be an entry gate. It is checked on every tick while the
pair is HELD, which is the whole of the exposure F4 describes.

NOTHING IS WEAKENED
===================
The IM/MM floor stays exactly as it was and is still checked. This is an
additional gate, not a replacement, and a position must satisfy both.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from carry_engine import (MIN_LIQUIDATION_DISTANCE_PCT, BookState,  # noqa: E402
                          CarryEngine)
from carry_risk import CarryRisk  # noqa: E402
from market_snapshot import MarketSnapshot  # noqa: E402

MARK = 100_000.0
H8 = 8 * 3600 * 1000


class Venue:
    def __init__(self, *, liq=None, mm_rate=None, raises=False):
        self.liq = liq
        self.mm_rate = mm_rate
        self.raises = raises
        self.orders = []

    def place_market(self, *, symbol, side, qty, product):
        self.orders.append((side, product, qty))
        return {"filled_qty": qty, "avg_price": MARK, "order_link_id": "x",
                "fee": qty * MARK * 5.5 / 1e4}

    def get_margin_multiple(self, symbol):
        return 5.0

    def get_mark(self, symbol):
        return MARK

    def get_lot_rules(self, symbol, product):
        return {"qty_step": 1e-6, "min_qty": 1e-6, "min_notional": 0.0}

    def get_fee_rates(self, symbol, product):
        return {"maker_bps": 2.0,
                "taker_bps": 10.0 if product == "spot" else 5.5}

    def get_spot_inventory(self, symbol):
        return 5.0

    def get_liquidation_view(self, symbol):
        if self.raises:
            raise RuntimeError("position list unreadable")
        return {"size": 0.001, "side": "Sell", "mark": MARK,
                "liq_price": self.liq,
                "distance_pct": (None if self.liq is None
                                 else 100.0 * (self.liq - MARK) / MARK),
                "account_mm_rate": self.mm_rate,
                "reason": ("BEYOND_VENUE_PRICE_BOUNDS" if self.liq is None
                           else "")}


def opened(venue, **kw):
    e = CarryEngine(broker=venue, max_notional_usd=100.0, borrow_apr=0.0,
                    execution_mode="overlay", persist=lambda s: None, **kw)
    e.pair_risk = CarryRisk(max_notional_usd=100.0)
    e.snapshot = MarketSnapshot(perp_mark=MARK, spot_mark=MARK,
                                funding_bps=3.0, margin_multiple=5.0,
                                observed_at_s=time.time())
    for k in (1, 2):
        e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                    funding_print_ms=k * H8, timestamp_ms=k * H8)
    d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                    funding_print_ms=3 * H8, timestamp_ms=3 * H8)
    assert d.action == "opened", d.reason
    return e


class TestTheFloorCameFromMeasurement:
    def test_it_clears_the_worst_move_in_the_corpus(self):
        """8.00% worst 8h, 11.52% worst intra-settlement excursion."""
        assert MIN_LIQUIDATION_DISTANCE_PCT >= 11.52
        assert MIN_LIQUIDATION_DISTANCE_PCT == 15.0

    def test_the_measurement_is_written_down_where_the_number_is(self):
        import inspect

        import carry_engine
        source = inspect.getsource(carry_engine)
        cut = source.index("MIN_LIQUIDATION_DISTANCE_PCT")
        nearby = source[max(0, cut - 1600):cut + 200]
        assert "11.52" in nearby and "8.00" in nearby, \
            "the number must carry the measurement that produced it"


class TestAShortBeingSqueezedGetsOut:
    def test_a_close_liquidation_price_unwinds_the_book(self):
        venue = Venue(liq=MARK * 1.05)          # 5% away, under the floor
        e = opened(venue, require_liquidation_check=True)
        venue.orders.clear()
        d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                        funding_print_ms=4 * H8, timestamp_ms=4 * H8)
        assert d.action == "unwound"
        assert "LIQUIDATION" in d.reason
        assert venue.orders, "it must actually close the leg"

    def test_a_distant_liquidation_price_is_left_alone(self):
        venue = Venue(liq=MARK * 1.60)
        e = opened(venue, require_liquidation_check=True)
        d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                        funding_print_ms=4 * H8, timestamp_ms=4 * H8)
        assert d.action == "hold"

    def test_beyond_the_venues_bounds_is_safe_not_unknown(self):
        """Bybit returns "" when liqPrice is outside its price bounds. There
        is no reachable liquidation price, which is the safest state there
        is — it must not read as missing data and halt the book."""
        venue = Venue(liq=None)
        e = opened(venue, require_liquidation_check=True)
        d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                        funding_print_ms=4 * H8, timestamp_ms=4 * H8)
        assert d.action == "hold"

    def test_the_exact_floor_is_the_boundary(self):
        for pct, expect in ((MIN_LIQUIDATION_DISTANCE_PCT - 0.1, "unwound"),
                            (MIN_LIQUIDATION_DISTANCE_PCT + 0.1, "hold")):
            venue = Venue(liq=MARK * (1.0 + pct / 100.0))
            e = opened(venue, require_liquidation_check=True)
            d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                            funding_print_ms=4 * H8, timestamp_ms=4 * H8)
            assert d.action == expect, (pct, d.reason)


class TestTheAccountRateIsCheckedToo:
    def test_a_high_account_mm_rate_unwinds(self):
        """Under UTA cross margin liquidation is ACCOUNT level, so a comfortable
        per-position liqPrice is not the whole answer."""
        venue = Venue(liq=MARK * 2.0, mm_rate=0.85)
        e = opened(venue, require_liquidation_check=True)
        d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                        funding_print_ms=4 * H8, timestamp_ms=4 * H8)
        assert d.action == "unwound"
        assert "ACCOUNT" in d.reason

    def test_a_low_account_mm_rate_is_fine(self):
        venue = Venue(liq=MARK * 2.0, mm_rate=0.05)
        e = opened(venue, require_liquidation_check=True)
        assert e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                           funding_print_ms=4 * H8,
                           timestamp_ms=4 * H8).action == "hold"


class TestUnreadableIsNotSafe:
    def test_a_venue_that_will_not_say_halts(self):
        """An unreadable liquidation price is a margin call you cannot see —
        the same position `_margin_multiple` has taken since 0023."""
        venue = Venue(raises=True)
        e = opened(venue, require_liquidation_check=True)
        d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                        funding_print_ms=4 * H8, timestamp_ms=4 * H8)
        assert d.action == "halted"
        assert e.state is BookState.HALTED


class TestNothingExistingIsWeakened:
    def test_the_im_mm_floor_is_still_checked(self):
        import inspect

        import carry_engine
        body = inspect.getsource(carry_engine.CarryEngine._check_margin)
        assert "min_margin_multiple" in body
        assert "MARGIN_UNREADABLE" in body

    def test_the_old_floor_still_unwinds_on_its_own(self):
        venue = Venue(liq=MARK * 2.0)
        e = opened(venue, require_liquidation_check=True)
        venue.get_margin_multiple = lambda s: 1.1   # degrade AFTER opening
        d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                        funding_print_ms=4 * H8, timestamp_ms=4 * H8)
        assert d.action == "unwound"
        assert "MARGIN_HEADROOM" in d.reason

    def test_a_book_without_the_check_behaves_exactly_as_before(self):
        """Default off, so no existing fixture changes behaviour. build_bot
        turns it on, and a test below asserts that."""
        venue = Venue(raises=True)
        e = opened(venue)
        assert e.require_liquidation_check is False
        assert e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                           funding_print_ms=4 * H8,
                           timestamp_ms=4 * H8).action == "hold"


class TestTheLiveBookTurnsItOn:
    def test_build_bot_requires_it(self):
        import config as _config
        import main as _main
        from unittest import mock
        cfg = _config.load({"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
                            "CARRY_EXECUTION_MODE": "overlay"})
        bot = _main.build_bot(config=cfg, store=_Store(), client=mock.Mock(),
                              risk_manager=mock.Mock(), engine=mock.Mock())
        assert bot.carry.require_liquidation_check is True


class TestTheBrokerReadsTheRightFields:
    class Client:
        def __init__(self, position=None, wallet=None):
            self.position = position
            self.wallet = wallet

        def _request(self, method, endpoint, *, params=None, body=None,
                     signed=False, retries=3):
            if endpoint == "/v5/position/list":
                return self.position
            if endpoint == "/v5/account/wallet-balance":
                return self.wallet
            return {}

    def broker(self, client):
        from carry_broker import CarryBroker
        return CarryBroker(client=client, sequence_source=lambda *a: 1,
                           order_gate=lambda: (True, "TEST"))

    def test_it_reads_liq_price_and_computes_the_distance(self):
        c = self.Client(
            position={"list": [{"size": "0.001", "side": "Sell",
                                "markPrice": "100000", "liqPrice": "115000",
                                "avgPrice": "100000"}]},
            wallet={"list": [{"accountMMRate": "0.12"}]})
        view = self.broker(c).get_liquidation_view("BTCUSDT")
        assert view["liq_price"] == pytest.approx(115_000.0)
        assert view["distance_pct"] == pytest.approx(15.0)
        assert view["account_mm_rate"] == pytest.approx(0.12)

    def test_an_empty_liq_price_is_out_of_bounds_not_zero(self):
        c = self.Client(
            position={"list": [{"size": "0.001", "side": "Sell",
                                "markPrice": "100000", "liqPrice": ""}]},
            wallet={"list": [{"accountMMRate": "0.1"}]})
        view = self.broker(c).get_liquidation_view("BTCUSDT")
        assert view["liq_price"] is None
        assert view["distance_pct"] is None
        assert view["reason"] == "BEYOND_VENUE_PRICE_BOUNDS"

    def test_a_long_liquidates_downwards(self):
        c = self.Client(
            position={"list": [{"size": "1", "side": "Buy",
                                "markPrice": "100000", "liqPrice": "80000"}]},
            wallet={"list": [{"accountMMRate": "0.1"}]})
        view = self.broker(c).get_liquidation_view("BTCUSDT")
        assert view["distance_pct"] == pytest.approx(20.0)

    def test_no_position_is_not_an_error(self):
        c = self.Client(position={"list": []},
                        wallet={"list": [{"accountMMRate": "0"}]})
        view = self.broker(c).get_liquidation_view("BTCUSDT")
        assert view["size"] == 0.0
        assert view["distance_pct"] is None

    def test_an_unreadable_position_raises(self):
        from carry_broker import PairIncident
        c = self.Client(position={}, wallet={"list": []})
        with pytest.raises(PairIncident):
            self.broker(c).get_liquidation_view("BTCUSDT")


class _Store:
    def __init__(self):
        self.seq = 0

    def next_order_seq(self):
        self.seq += 1
        return self.seq

    def trip_kill_switch(self, reason):
        pass

    def is_kill_switch_engaged(self):
        return (False, "")

    def save_carry_position(self, state):
        pass

    def load_carry_position(self):
        return None

    def save_ledger(self, rows):
        self.ledger_rows = list(rows)

    def load_ledger(self):
        return getattr(self, "ledger_rows", None)

    def open_positions(self):
        return []
