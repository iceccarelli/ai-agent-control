"""The carry book executes, or it refuses and says which leg failed.

These are not research assertions. Each one is a way this trade kills an
account, written down so the code cannot do it.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from carry_engine import BookState, CarryEngine, DELTA_BAND  # noqa: E402
from carry_risk import CarryRisk  # noqa: E402
from market_snapshot import MarketSnapshot  # noqa: E402


class FakeBroker:
    """A venue that fills what you ask unless told to misbehave."""

    def __init__(self, *, mark=100_000.0, margin=5.0, fill_ratio=1.0,
                 reject=(), slip=0.0):
        self.mark = mark
        self.margin = margin
        self.fill_ratio = fill_ratio
        self.reject = set(reject)          # {"spot", "linear"}
        self.slip = slip
        self.orders = []

    def place_market(self, *, symbol, side, qty, product):
        self.orders.append((symbol, side, qty, product))
        if product in self.reject:
            return None
        price = self.mark * (1 + self.slip if side == "Buy" else 1 - self.slip)
        return {"filled_qty": qty * self.fill_ratio, "avg_price": price,
                "order_link_id": f"{product}-{len(self.orders)}"}

    def get_margin_multiple(self, symbol):
        return self.margin

    def get_mark(self, symbol):
        return self.mark

    # 0036: the venue's own lot rules and fee tier. A 1e-6 step leaves every
    # size in this file unchanged; the fee table is what the round trip is
    # computed from instead of a constant.
    def get_lot_rules(self, symbol, product):
        return {"qty_step": 1e-6, "min_qty": 1e-6, "min_notional": 0.0}

    def get_fee_rates(self, symbol, product):
        return {"maker_bps": 2.0,
                "taker_bps": 10.0 if product == "spot" else 5.5}


class _SpotFillsThenVenueDies(FakeBroker):
    """The spot buy lands. Every order after it is rejected — including the
    sell that would flatten it. A real venue does this during an outage."""

    def place_market(self, *, symbol, side, qty, product):
        self.orders.append((symbol, side, qty, product))
        if len(self.orders) == 1 and product == "spot" and side == "Buy":
            return {"filled_qty": qty, "avg_price": self.mark,
                    "order_link_id": "spot-1"}
        return None


SPOT = 100_000.0


class _Wired(CarryEngine):
    """The engine as main.tick drives it (0033).

    Since 0033 no first leg leaves without the pair gate AND one market
    snapshot whose marks are the marks the decision used. main.tick takes that
    snapshot and hands it over before every on_candle; this stamps one from
    the candle's own arguments, the same way, so these tests exercise the
    production path instead of a bare engine that production can no longer
    build. The refusals themselves are tested in test_carry_fail_closed.py.
    """

    #: 0034: every candle in THIS file is one settled funding print — that
    #: is what these tests have always meant by "a print". The engine now
    #: needs the print's settlement stamp to tell a print from a tick, so the
    #: fixture supplies one per candle, eight hours apart. What a TICK does
    #: (the same stamp again) is tested in test_carry_funding_prints.py.
    _H8 = 8 * 3600 * 1000

    def on_candle(self, *, mark, funding_bps, spot=None, timestamp_ms=0,
                  funding_print_ms=None):
        import math
        import time
        usable = (spot is not None and isinstance(mark, (int, float))
                  and math.isfinite(mark) and mark > 0)
        self.snapshot = MarketSnapshot(
            perp_mark=mark, spot_mark=spot, funding_bps=funding_bps,
            margin_multiple=float(getattr(self.broker, "margin", 5.0)),
            observed_at_s=time.time()) if usable else None
        if funding_print_ms is None:
            self._stamp = getattr(self, "_stamp", 0) + self._H8
            funding_print_ms = self._stamp
        return super().on_candle(mark=mark, funding_bps=funding_bps,
                                 spot=spot, timestamp_ms=timestamp_ms,
                                 funding_print_ms=funding_print_ms)


def engine(broker, *, primed=True, **kw):
    """A CarryEngine ready to open.

    0021 made two things true that these tests must now respect:

      `spot` is REQUIRED to open. Without it the basis is unknown, and an
      unknown basis is not a small one, so the engine refuses.

      The EWMA needs three funding prints. Below that the engine returns
      INSUFFICIENT_FUNDING_HISTORY rather than falling back to the last print.

    `primed=True` feeds the two warm-up prints so a test can reach the entry
    path in one candle, the way a book that has been running does.
    `primed=False` leaves the history empty to test the warm-up itself.
    """
    kw.setdefault("max_notional_usd", 100_000.0)
    kw.setdefault("borrow_apr", 0.05)
    # 0036: these tests are about the ACQUIRE path — the book buying the spot
    # leg. The overlay path has its own file.
    kw.setdefault("execution_mode", "acquire")
    kw.setdefault("persist", lambda state: None)
    eng = _Wired(broker=broker, **kw)
    eng.pair_risk = CarryRisk(max_notional_usd=kw["max_notional_usd"])
    if primed:
        for _ in range(2):
            eng.on_candle(mark=100_000.0, funding_bps=3.0, spot=SPOT)
    return eng


class TestItActuallyOpens:
    def test_rich_funding_opens_both_legs(self):
        broker = FakeBroker()
        eng = engine(broker)
        decision = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        assert decision.acted is True
        assert decision.action == "opened"
        assert eng.state is BookState.HEDGED
        assert len(broker.orders) == 2, "a carry position is TWO orders"

    def test_the_legs_are_opposite_and_equal(self):
        broker = FakeBroker()
        eng = engine(broker)
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        spot, perp = broker.orders
        assert spot[1] == "Buy" and spot[3] == "spot"
        assert perp[1] == "Sell" and perp[3] == "linear"
        assert spot[2] == pytest.approx(perp[2])

    def test_the_book_is_delta_flat_after_opening(self):
        eng = engine(FakeBroker())
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        assert eng.position.delta_qty == pytest.approx(0.0)

    def test_it_collects_funding_while_hedged(self):
        eng = engine(FakeBroker())
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        assert eng.position.funding_collected > 0


class TestOneLegNeverStandsAlone:
    """The failure that kills funds: waking up delta-long at 3am."""

    def test_a_failed_perp_leg_unwinds_the_spot_immediately(self):
        broker = FakeBroker(reject={"linear"})
        eng = engine(broker)
        decision = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        assert eng.position is None, "a naked spot leg was left open"
        assert eng.state is BookState.FLAT
        assert "PERP_LEG_DID_NOT_FILL" in decision.reason
        sides = [(o[1], o[3]) for o in broker.orders]
        assert ("Sell", "spot") in sides, "the naked spot was not sold"

    def test_a_failed_spot_leg_never_shorts_the_perp(self):
        broker = FakeBroker(reject={"spot"})
        eng = engine(broker)
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        assert eng.position is None
        assert not any(o[3] == "linear" for o in broker.orders), \
            "the perp was shorted with no spot behind it"

    def test_both_legs_rejected_leaves_nothing_open(self):
        """Nothing filled means nothing naked. FLAT, not halted."""
        broker = FakeBroker(reject={"spot", "linear"})
        eng = engine(broker)
        decision = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        assert eng.position is None
        assert eng.state is BookState.FLAT
        assert decision.reason == "SPOT_LEG_DID_NOT_FILL"

    def test_a_naked_spot_that_cannot_be_sold_halts_the_book(self):
        """THE scenario that ends firms: spot filled, hedge rejected, and the
        unwind rejected too. The book is naked long and cannot self-correct, so
        it must stop and shout for a human rather than keep trading."""
        broker = _SpotFillsThenVenueDies()
        eng = engine(broker)
        decision = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        assert decision.state is BookState.HALTED
        assert eng.state is BookState.HALTED
        assert decision.reason == "NAKED_SPOT_UNWIND_FAILED"

    def test_halted_books_refuse_every_later_candle(self):
        eng = engine(_SpotFillsThenVenueDies())
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        after = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=5.0)
        assert after.acted is False
        assert after.reason == "KILL_SWITCH_ENGAGED"

    def test_the_kill_switch_is_called_on_halt(self):
        tripped = []
        eng = engine(_SpotFillsThenVenueDies(), kill_switch=tripped.append)
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        assert tripped, "the book halted without tripping the switch"


class TestDeltaIsMeasuredNotAssumed:
    def test_drift_past_the_band_triggers_a_rebalance(self):
        broker = FakeBroker()
        eng = engine(broker)
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        eng.position.perp.filled_qty *= 0.90          # hedge slipped 10%
        decision = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        assert decision.action == "rebalanced"
        assert decision.acted is True

    def test_drift_inside_the_band_does_not_churn(self):
        broker = FakeBroker()
        eng = engine(broker)
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        before = len(broker.orders)
        eng.position.perp.filled_qty *= (1 - DELTA_BAND / 4)
        decision = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        assert decision.action == "hold"
        assert len(broker.orders) == before, "it traded inside the band"

    def test_rebalancing_uses_the_perp_not_the_spot(self):
        broker = FakeBroker()
        eng = engine(broker)
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        broker.orders.clear()
        eng.position.perp.filled_qty *= 0.90
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        assert all(o[3] == "linear" for o in broker.orders), \
            "collateral was sold to fix a hedge"

    def test_legs_that_land_unpaired_are_unwound_not_kept(self):
        broker = FakeBroker(fill_ratio=1.0)
        eng = engine(broker)
        original = broker.place_market

        def partial(*, symbol, side, qty, product):
            result = original(symbol=symbol, side=side, qty=qty, product=product)
            if result and product == "linear":
                result["filled_qty"] = qty * 0.5     # half a hedge
            return result

        broker.place_market = partial
        decision = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        assert eng.position is None
        assert "UNPAIRED" in decision.reason or decision.action == "unwound"


class TestTheShortLegCanBeLiquidated:
    def test_thin_margin_refuses_to_open(self):
        eng = engine(FakeBroker(margin=1.2))
        decision = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=5.0)
        assert decision.acted is False
        assert decision.reason == "MARGIN_HEADROOM_TOO_THIN"

    def test_margin_falling_while_open_unwinds_the_book(self):
        broker = FakeBroker(margin=5.0)
        eng = engine(broker)
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        broker.margin = 1.1
        decision = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        assert decision.action == "unwound"
        assert eng.position is None

    def test_unreadable_margin_halts_rather_than_assumes(self):
        broker = FakeBroker()
        eng = engine(broker)
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        broker.get_margin_multiple = lambda s: (_ for _ in ()).throw(RuntimeError())
        decision = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        assert decision.state is BookState.HALTED


class TestFundingDrivesEntryAndExit:
    def test_thin_funding_stands_aside(self):
        """Thin carry cannot clear borrow plus the amortised round trip.

        The book is built from an EWMA, so it is primed with THIN prints here.
        One thin print after a rich history does not stand the book down, and
        that is correct: a smoother that collapses on a single observation is
        just the last print with extra steps.
        """
        broker = FakeBroker()
        eng = engine(broker, primed=False)
        for _ in range(3):
            decision = eng.on_candle(spot=SPOT, mark=100_000.0,
                                     funding_bps=0.05)
        assert decision.acted is False
        assert decision.reason == "CARRY_BELOW_COST_OF_CAPITAL"
        assert not broker.orders

    def test_one_thin_print_does_not_stand_a_rich_book_down(self):
        """The other half of the same rule, asserted explicitly."""
        broker = FakeBroker()
        eng = engine(broker)          # primed with 3.0 bps
        decision = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=0.05)
        assert decision.acted is True

    def test_the_warm_up_is_required_before_any_entry(self):
        broker = FakeBroker()
        eng = engine(broker, primed=False)
        decision = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=5.0)
        assert decision.acted is False
        assert decision.reason == "INSUFFICIENT_FUNDING_HISTORY"
        assert not broker.orders

    def test_opening_without_a_spot_mark_is_refused(self):
        broker = FakeBroker()
        eng = engine(broker)
        decision = eng.on_candle(mark=100_000.0, funding_bps=5.0)
        assert decision.acted is False
        assert decision.reason == "SPOT_UNAVAILABLE_BASIS_UNKNOWN"
        assert not broker.orders

    def test_a_wide_entry_basis_is_refused(self):
        broker = FakeBroker()
        eng = engine(broker)
        decision = eng.on_candle(spot=SPOT, mark=SPOT * 1.02, funding_bps=3.0)
        assert decision.acted is False
        assert decision.reason == "ENTRY_BASIS_EXCEEDS_CARRY_BUDGET"

    def test_sustained_negative_funding_unwinds(self):
        eng = engine(FakeBroker())
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        for _ in range(2):
            eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=-0.5)
        assert eng.position is not None, "it left after one negative print"
        decision = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=-0.5)
        assert decision.action == "unwound"
        assert decision.reason == "FUNDING_INVERTED"

    def test_one_negative_print_does_not_panic(self):
        eng = engine(FakeBroker())
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=-0.5)
        assert eng.position is not None

    def test_the_streak_resets_when_funding_returns(self):
        eng = engine(FakeBroker())
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=-0.5)
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        assert eng.position.negative_funding_streak == 0


class TestBadDataHaltsRatherThanTrades:
    @pytest.mark.parametrize("mark", [float("nan"), float("inf"), 0.0, -100.0])
    def test_an_unusable_mark_halts(self, mark):
        eng = engine(FakeBroker())
        decision = eng.on_candle(spot=SPOT, mark=mark, funding_bps=5.0)
        assert decision.acted is False
        assert decision.state is BookState.HALTED


class TestItCannotExpressADirectionalBet:
    def test_on_candle_takes_no_direction_parameter(self):
        import inspect
        params = set(inspect.signature(CarryEngine.on_candle).parameters)
        for banned in ("side", "direction", "long", "short", "signal"):
            assert banned not in params, (
                f"on_candle accepts {banned!r}; this engine must be incapable "
                "of taking a directional position")

    def test_no_module_assigns_live_authorized(self):
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "carry_engine.py"), encoding="utf-8") as fh:
            body = fh.read()
        assert "live_authorized" not in body
