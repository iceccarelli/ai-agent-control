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



def _fee_detail(usd=0.0):
    """A labelled quote-coin fee, as the venue reports it. A spot BUY with an
    unlabelled fee cannot establish the BTC actually held and the engine halts
    on it (SPOT_FEE_UNVERIFIABLE), so the fakes must say the currency."""
    return {"fee_detail": {"known": True, "source": "cumFeeDetail",
                           "note": "scripted fee in this test venue",
                           "components": [{"amount": usd,
                                           "currency": "USDT"}]}}

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
                "order_link_id": f"{product}-{len(self.orders)}",
                **_fee_detail()}

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
                    "order_link_id": "spot-1", **_fee_detail()}
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


def engine(broker, *, primed=True, prime_bps=6.0, **kw):
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
        # 0051: 6.0, not 3.0. These tests are about EXECUTION mechanics (what is
        # sent, unwound, halted), not about whether the economics admit an entry,
        # and they are ACQUIRE mode: a 31 bps round trip plus 5% financing. The
        # entry gate now also requires the funding to earn back the round trip
        # inside the measured hold (17 settlements); with primes of 3.0 the
        # smoothed rate at the opening candle is 2.0 bps/8h, which needs 21 and
        # is correctly refused. A richer prime keeps these fixtures reaching the
        # path they test. The refusal itself is pinned in test_entry_gate_parity.
        # The two basis-budget tests pass prime_bps=3.0: their budget is balanced
        # on a 3.0 smoothed rate (198 bps against a 200 bps basis) and keep
        # exactly the conditions they were written for.
        for _ in range(2):
            eng.on_candle(mark=100_000.0, funding_bps=prime_bps, spot=SPOT)
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
            # Only the SELL that OPENS the hedge fills short — which is what
            # "legs that land unpaired" means. A closing buy-back is exact
            # here, the same convention `test_carry_overlay.Venue` uses, and
            # for the same reason: a close that fills short is a different
            # failure, and it now has its own file
            # (`test_unwind_completeness.py`). Before that distinction existed
            # this stub halved the buy-back too, so the unwind left 0.25 BTC
            # short at the venue and the engine still reported FLAT.
            if result and product == "linear" and side == "Sell":
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
        eng = engine(broker, prime_bps=3.0)
        decision = eng.on_candle(spot=SPOT, mark=SPOT * 1.02, funding_bps=3.0)
        assert decision.acted is False
        assert decision.reason == "ENTRY_BASIS_EXCEEDS_CARRY_BUDGET"

    def test_a_perp_discount_beyond_the_budget_is_refused(self):
        broker = FakeBroker()
        eng = engine(broker, prime_bps=3.0)
        decision = eng.on_candle(spot=SPOT, mark=SPOT * 0.98, funding_bps=3.0)
        assert decision.acted is False
        assert decision.reason == "ENTRY_BASIS_DISCOUNT_EXCEEDS_CARRY_BUDGET"
        assert not broker.orders

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


def test_a_halted_book_holding_a_position_keeps_reading_its_margin(caplog):
    import logging
    import carry_engine as ce
    from carry_engine import CarryPosition, Leg

    class V:
        def get_margin_multiple(self, symbol):
            return 1.1

    e = ce.CarryEngine(broker=V(), max_notional_usd=100.0, borrow_apr=0.0,
                       execution_mode="overlay", persist=lambda s: None)
    e.position = CarryPosition(
        spot=Leg(symbol="BTCUSDT", side="Buy", product="spot"),
        perp=Leg(symbol="BTCUSDT", side="Sell", product="linear"),
        opened_ms=1)
    e.state = ce.BookState.HALTED
    with caplog.at_level(logging.CRITICAL):
        d = e.on_candle(mark=100_000.0, funding_bps=1.0, timestamp_ms=10_000)
    assert d.action == "halted" or d.reason == "KILL_SWITCH_ENGAGED"
    assert d.detail["halted_margin_multiple"] == 1.1
    assert "BELOW" in caplog.text


def test_there_is_no_funding_floor_knob_that_nothing_reads():
    import inspect
    from carry_engine import CarryEngine
    assert "min_entry_funding_bps" not in inspect.signature(
        CarryEngine.__init__).parameters


# ---------------------------------------------------------------------------
# The continuation exit (Round 16). The legacy funding exit leaves after three
# consecutive negative prints whatever they are worth; the continuation exit
# leaves when the funding shortfall paid while holding reaches the priced round
# trip. It changes ONLY the funding exit: it is opt-in and the default is the
# original rule, bit for bit.
# ---------------------------------------------------------------------------

class TestContinuationExit:
    @staticmethod
    def _held(policy, **kw):
        # The shared helper defaults to 5% borrow, which IS a holding cost
        # (0.457 bps per print, charged by the rule). These tests state their
        # own capital cost; the default here is none.
        kw.setdefault("borrow_apr", 0.0)
        eng = engine(FakeBroker(), exit_policy=policy, **kw)
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=6.0)
        assert eng.position is not None
        return eng

    def test_the_default_is_the_original_rule(self):
        import inspect
        assert inspect.signature(CarryEngine.__init__).parameters[
            "exit_policy"].default == "funding_streak"
        assert engine(FakeBroker()).exit_policy == "funding_streak"

    def test_an_unknown_policy_is_refused(self):
        with pytest.raises(ValueError):
            engine(FakeBroker(), exit_policy="hold_forever")

    def test_three_tiny_negative_prints_leave_under_the_legacy_rule(self):
        eng = self._held("funding_streak")
        for _ in range(2):
            eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=-0.1)
        d = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=-0.1)
        assert d.action == "unwound" and d.reason == "FUNDING_INVERTED"

    def test_three_tiny_negative_prints_do_not_pay_a_round_trip_to_leave(self):
        eng = self._held("continuation")
        for _ in range(3):
            d = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=-0.1)
        assert eng.position is not None, d
        assert d.action == "hold"
        assert d.detail["exit_policy"] == "continuation"
        assert d.detail["exit_shortfall_bps"] == pytest.approx(0.3)
        assert eng.position.negative_funding_streak == 3   # still counted

    def test_one_print_that_costs_a_round_trip_leaves_at_once(self):
        eng = self._held("continuation")
        rt = eng.round_trip_bps()
        d = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=-(rt + 1.0))
        assert d.action == "unwound" and d.reason == "FUNDING_SHORTFALL"

    def test_shortfall_accumulates_across_prints_and_leaves_at_the_round_trip(self):
        eng = self._held("continuation")
        rt = eng.round_trip_bps()
        step = rt / 4.0 + 0.01
        for i in range(3):
            d = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=-step)
            assert eng.position is not None, f"left early at print {i + 1}"
        d = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=-step)
        assert d.action == "unwound" and d.reason == "FUNDING_SHORTFALL"

    def test_a_dip_that_recovers_is_paid_back_and_never_leaves(self):
        eng = self._held("continuation")
        rt = eng.round_trip_bps()
        for _ in range(10):
            eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=-rt * 0.6)
            eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=rt * 0.7)
        assert eng.position is not None
        assert eng.position.exit_shortfall_bps == 0.0

    def test_the_shortfall_is_kept_under_the_legacy_policy_for_the_trace(self):
        eng = self._held("funding_streak")
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=-0.5)
        assert eng.position.exit_shortfall_bps == pytest.approx(0.5)

    def test_the_owner_cost_of_capital_charges_every_print_below_it(self):
        """A print that pays less than holding costs adds the difference, even
        when it is positive: funding of 0.1 bps does not pay a 0.4 bps hold."""
        eng = self._held("continuation", opportunity_cost_apr=0.044)
        c = 0.044 / 365.0 * 1e4 / 3.0
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=0.1)
        assert eng.position.exit_shortfall_bps == pytest.approx(c - 0.1)

    def test_the_shortfall_survives_a_restart(self):
        eng = self._held("continuation")
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=-2.0)
        state = eng.to_state()
        assert state["position"]["exit_shortfall_bps"] == pytest.approx(2.0)
        again = engine(FakeBroker(), exit_policy="continuation", primed=False,
                       borrow_apr=0.0)
        again.restore(state)
        assert again.position.exit_shortfall_bps == pytest.approx(2.0)
        assert again.position.exit_shortfall_known is True

    def test_a_ledger_without_the_shortfall_is_unknown_and_the_original_rule_decides(self):
        """UNKNOWN is not zero. A pair restored without its shortfall (a ledger
        written before it existed, or a value that is not a finite number) has
        an unknown bleed already paid: forgiving it would hold the pair longer
        than any fresh episode would. The ORIGINAL funding rule decides for it
        until it closes."""
        eng = self._held("continuation")
        state = eng.to_state()
        for bad in ("absent", float("nan"), -1.0, "x", True):
            s = dict(state, position=dict(state["position"]))
            if bad == "absent":
                del s["position"]["exit_shortfall_bps"]
            else:
                s["position"]["exit_shortfall_bps"] = bad
            again = engine(FakeBroker(), exit_policy="continuation",
                           primed=False, borrow_apr=0.0)
            again.restore(s)
            assert again.position.exit_shortfall_bps == 0.0, bad
            assert again.position.exit_shortfall_known is False, bad
        # and the flag itself survives a second restart (never re-learned as 0)
        again.position.exit_shortfall_known = False
        third = engine(FakeBroker(), exit_policy="continuation", primed=False,
                       borrow_apr=0.0)
        third.restore(again.to_state())
        assert third.position.exit_shortfall_known is False

    def test_an_unknown_shortfall_leaves_on_three_negative_prints_even_though_they_are_tiny(self):
        eng = self._held("continuation")
        state = eng.to_state()
        del state["position"]["exit_shortfall_bps"]
        again = engine(FakeBroker(), exit_policy="continuation", primed=False,
                       borrow_apr=0.0)
        again.restore(state)
        again._funding_history = [6.0, 6.0, 6.0]
        again._last_print_ms = eng._last_print_ms
        again._stamp = eng._stamp          # the fixture's stamps continue after it
        for _ in range(2):
            again.on_candle(spot=SPOT, mark=100_000.0, funding_bps=-0.1)
        d = again.on_candle(spot=SPOT, mark=100_000.0, funding_bps=-0.1)
        assert d.action == "unwound" and d.reason == "FUNDING_INVERTED"

    def test_settlements_missed_while_down_advance_the_shortfall(self):
        """REVIEW FINDING (Round 16): the cold-start catch-up moved the negative
        streak but never the shortfall, so a restart FORGAVE the bleed: N missed
        negative prints cost nothing towards the continuation exit."""
        eng = self._held("continuation")
        rt = eng.round_trip_bps()
        h8 = 8 * 3600 * 1000
        last = eng._last_print_ms
        eng._ledger_last_print_ms = last
        miss = -(rt / 3.0 + 0.5)                    # three of them cost > a round trip
        eng.venue_prints_at_start = [(miss, last + i * h8) for i in range(1, 5)]
        assert eng._catch_up_missed_prints() == 3   # the newest is left for on_candle
        assert eng.position.exit_shortfall_bps == pytest.approx(-3 * miss)
        assert eng.position.exit_shortfall_bps > rt
        d = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        assert d.action == "unwound" and d.reason == "FUNDING_SHORTFALL"

    def test_a_missed_dip_that_recovers_does_not_leave(self):
        eng = self._held("continuation")
        rt = eng.round_trip_bps()
        h8 = 8 * 3600 * 1000
        last = eng._last_print_ms
        eng._ledger_last_print_ms = last
        eng.venue_prints_at_start = [(-rt / 4.0, last + h8), (rt, last + 2 * h8),
                                     (-rt / 4.0, last + 3 * h8),
                                     (1.0, last + 4 * h8)]
        assert eng._catch_up_missed_prints() == 3
        assert eng.position.exit_shortfall_bps == pytest.approx(rt / 4.0 - 0.0)
        d = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0)
        assert eng.position is not None and d.action == "hold"

    def test_an_unpriced_round_trip_falls_back_to_the_legacy_exit(self):
        """The venue will not state the fee: the continuation rule cannot judge,
        so the ORIGINAL rule decides (never holding on an unmeasured number)."""
        eng = self._held("continuation")

        def unreadable():
            raise RuntimeError("fee rates unreadable")
        eng.round_trip_bps = unreadable
        # one huge negative print: the continuation rule would leave, the
        # legacy rule (needs three) holds
        d = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=-50.0)
        assert eng.position is not None and d.action == "hold"
        for _ in range(2):
            d = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=-50.0)
        assert d.action == "unwound" and d.reason == "FUNDING_INVERTED"

    def test_the_outlook_names_the_rule_that_decides(self):
        eng = self._held("continuation")
        rules = {r["id"]: r for r in eng._exit_outlook(100_000.0)["rules"]}
        assert rules["FUNDING_SHORTFALL"]["decides"] is True
        assert rules["FUNDING_INVERTED"]["decides"] is False
        assert rules["FUNDING_INVERTED"]["active"] is None
        legacy = self._held("funding_streak")
        rules = {r["id"]: r for r in legacy._exit_outlook(100_000.0)["rules"]}
        assert rules["FUNDING_INVERTED"]["decides"] is True
        assert rules["FUNDING_SHORTFALL"]["active"] is None


# ---------------------------------------------------------------------------
# Round 17 red team on the continuation exit: a held print that cannot advance
# the shortfall leaves the accumulator one print short, and the position still
# read as "known" (shortfall 0 -> "funding is paying for the hold" -> stay). An
# unknown must stay unknown: the original rule then decides, as for a restored
# pair. (A constructor check on `borrow_apr` was tried and withdrawn: the gate
# refuses every entry on an unusable rate, pinned in test_entry_gate_parity, and
# config and build_bot refuse it upstream.)
# ---------------------------------------------------------------------------

class TestAnUnadvanceableShortfallIsUnknown:
    @staticmethod
    def _held():
        return TestContinuationExit._held("continuation")

    def test_a_print_that_cannot_advance_the_shortfall_makes_it_unknown(self):
        eng = self._held()
        assert eng.position.exit_shortfall_known is True
        eng.borrow_apr = float("nan")           # an unusable capital cost: the step cannot be taken
        d = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=-50.0)   # worth far more than a round trip
        assert eng.position is not None and eng.position.exit_shortfall_known is False
        assert eng.position.exit_shortfall_bps == 0.0, "the missed print is not guessed into the accumulator"
        assert d.action == "hold"               # one negative print is below the legacy rule's three

    def test_the_legacy_rule_decides_while_the_shortfall_is_unknown(self):
        eng = self._held()
        eng.borrow_apr = float("nan")
        for _ in range(2):
            eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=-50.0)
        d = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=-50.0)
        assert d.action == "unwound" and d.reason == "FUNDING_INVERTED"

    def test_it_does_not_become_known_again_because_a_later_print_could_be_stepped(self):
        eng = self._held()
        eng.borrow_apr = float("nan")
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=-50.0)
        eng.borrow_apr = 0.0                    # the cost is usable again, but the missed print is still missing
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=6.0)
        assert eng.position.exit_shortfall_known is False

    def test_the_unknown_survives_a_restart(self):
        eng = self._held()
        eng.borrow_apr = float("nan")
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=-50.0)
        assert eng.to_state()["position"]["exit_shortfall_known"] is False

    def test_the_exit_trace_names_the_rule_that_decides_while_the_shortfall_is_unknown(self):
        """Red team round 2: under continuation with an unknown shortfall the trace still said FUNDING_INVERTED decides=False while the legacy rule was the one deciding."""
        eng = self._held()
        rules = {r["id"]: r for r in eng._exit_outlook(100_000.0)["rules"]}
        assert rules["FUNDING_SHORTFALL"]["decides"] is True and rules["FUNDING_INVERTED"]["decides"] is False   # known: unchanged
        eng.borrow_apr = float("nan")
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=-0.1)
        assert eng.position.exit_shortfall_known is False
        rules = {r["id"]: r for r in eng._exit_outlook(100_000.0)["rules"]}
        assert rules["FUNDING_INVERTED"]["decides"] is True and rules["FUNDING_INVERTED"]["active"] is not None
        assert rules["FUNDING_SHORTFALL"]["decides"] is False and rules["FUNDING_SHORTFALL"]["active"] is None
        assert rules["FUNDING_SHORTFALL"]["shortfall_known"] is False

    @pytest.mark.parametrize("bad", [-0.01, float("nan"), float("inf")])
    def test_an_unusable_borrow_rate_leaves_every_held_shortfall_unknown(self, bad):
        """The engine is built with the rate as given (the gate refuses every ENTRY on it, pinned in test_entry_gate_parity; config and build_bot already
        refuse it upstream): a position that is nevertheless held can never advance its shortfall, and each such print leaves it unknown, never 0."""
        eng = self._held()
        eng.borrow_apr = bad
        eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=-0.1)
        assert eng.position.exit_shortfall_known is False
