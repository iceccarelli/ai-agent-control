"""A close that fills SHORT is not a close, and an overlay must keep looking.

WHAT THESE TESTS PIN
====================
Two defects, both reproduced against the engine before they were repaired, both
on the OVERLAY path that PHASE1_DECISION chose as the product.

1. A PARTIALLY FILLED CLOSE WAS RECORDED AS A COMPLETE ONE.

   All three exit paths — `_unwind` in overlay, `_unwind` in acquire, and
   `_emergency_unwind_spot` — tested only `is None` / `filled_qty <= 0` on the
   closing order and then set `position = None; state = FLAT`. A market order
   that fills half leaves the remainder AT THE VENUE. The book read FLAT, the
   next candle was free to open a SECOND hedge against the same margin and the
   same liquidation price, and `plan_cold_start` could not catch it on a
   restart because the ledger agreed with itself — it had been told the
   position was closed.

   Measured on a stub venue: a 1.0 BTC overlay hedge unwound against a venue
   filling half left **$50,000 of unhedged short** that nothing in the process
   knew about. The trigger was a margin collapse, so the orders that fill short
   are exactly the ones sent into the market that just moved.

   THE ASYMMETRY IS THE POINT. A short fill on the way IN is a smaller hedge
   and is handled as one (`TestAPartialPerpFillIsASmallerHedgeNotAnIncident`
   in `test_carry_overlay.py`). A short fill on the way OUT is an exposure
   nobody is managing. `tools/drill.py`'s `final_reconcile` stage has always
   checked the venue for a residual after an unwind; the engine never did.

2. THE OVERLAY NEVER RE-READ THE INVENTORY IT HEDGES.

   `get_spot_inventory` was called once, in `_open_overlay`, and then only at
   cold start. The overlay's entire safety argument is that the long side is
   the CLIENT's own coin, so the short is a hedge rather than a bet. A client
   who sold, withdrew or re-pledged their BTC left this book naked short,
   reporting HEDGED_AND_COLLECTING every sixty seconds, until somebody happened
   to restart the process. `plan_cold_start` has always refused exactly that
   state at startup — "the inventory this short was written against has left".
   These pin the same check on every candle, where the exposure accrues.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from carry_engine import (ACQUIRE, OVERLAY, BookState,  # noqa: E402
                          CarryEngine)
from carry_risk import CarryRisk  # noqa: E402
from market_snapshot import MarketSnapshot  # noqa: E402

MARK = 100_000.0
STEP = {"qty_step": 0.001, "min_qty": 0.001, "min_notional": 5.0}
FEES = {"maker_bps": 2.0, "taker_bps": 5.5}
SPOT_FEES = {"maker_bps": 10.0, "taker_bps": 10.0}


class Venue:
    """What the venue ACTUALLY holds, tracked apart from what the book believes.

    `open_fill` applies to orders that OPEN exposure and `close_fill` to orders
    that REDUCE it. They are separate knobs because they are separate failures,
    and these tests are about the second one — the same distinction
    `test_carry_overlay.Venue` makes when it says a buy-back is exact.
    """

    def __init__(self, *, inventory=1.0, margin=5.0, open_fill=1.0,
                 close_fill=1.0, reject=(), inventory_boom=False):
        self.inventory = inventory
        self.margin = margin
        self.open_fill = open_fill
        self.close_fill = close_fill
        self.reject = set(reject)
        self.inventory_boom = inventory_boom
        self.perp_short = 0.0
        self.spot_long = 0.0
        self.orders = []
        self.inventory_calls = 0

    @staticmethod
    def _is_closing(side, product):
        return (product == "linear" and side == "Buy") or \
               (product == "spot" and side == "Sell")

    def place_market(self, *, symbol, side, qty, product):
        if product in self.reject:
            self.orders.append((side, product, qty, 0.0))
            return None
        ratio = self.close_fill if self._is_closing(side, product) \
            else self.open_fill
        filled = qty * ratio
        self.orders.append((side, product, qty, filled))
        if filled <= 0:
            return None
        # The venue reports a spot BUY fee in BTC and everything else in USDT.
        # Keep the simulated wallet net of the base-coin fee, just like the
        # production account.
        fee = (0.5 / MARK if (product == "spot" and side == "Buy") else 0.5)
        fee_currency = "BTC" if (product == "spot" and side == "Buy") else "USDT"
        if product == "linear":
            self.perp_short += filled if side == "Sell" else -filled
        elif side == "Buy":
            self.spot_long += max(0.0, filled - fee)
        else:
            self.spot_long -= filled
        fee_detail = {"known": True, "source": "cumFeeDetail",
                      "components": [{"amount": fee, "currency": fee_currency}]}
        return {"filled_qty": filled, "avg_price": MARK,
                "order_link_id": f"o{len(self.orders)}", "fee": fee,
                "fee_detail": fee_detail}

    def get_margin_multiple(self, symbol):
        return self.margin

    def get_mark(self, symbol):
        return MARK

    def get_spot_mark(self, symbol):
        return MARK

    def get_lot_rules(self, symbol, product):
        if product == "spot":
            # Bybit BTC spot basePrecision, not the linear 0.001 lot.
            return {"qty_step": 1e-6, "min_qty": 1e-6, "min_notional": 5.0}
        return dict(STEP)

    def get_fee_rates(self, symbol, product):
        return dict(SPOT_FEES if product == "spot" else FEES)

    def get_spot_inventory(self, symbol):
        self.inventory_calls += 1
        if self.inventory_boom:
            raise RuntimeError("wallet-balance unreadable")
        return self.inventory

    def get_liquidation_view(self, symbol):
        return {"distance_pct": None, "account_mm_rate": 0.01,
                "margin_mode": "REGULAR_MARGIN",
                "reason": "BEYOND_VENUE_PRICE_BOUNDS"}


def build(venue, *, mode=OVERLAY, cap=100_000.0):
    """An engine with the pair gate attached and the EWMA already warm."""
    written = []
    tripped = []
    eng = CarryEngine(broker=venue, kill_switch=tripped.append,
                      spot_symbol="BTCUSDT", perp_symbol="BTCUSDT",
                      max_notional_usd=cap, borrow_apr=0.0,
                      execution_mode=mode, persist=written.append,
                      require_liquidation_check=True)
    eng.pair_risk = CarryRisk(store=None, max_notional_usd=cap)
    eng._funding_history = [3.0, 3.0, 3.0]
    eng.written = written
    eng.tripped = tripped
    return eng


def observe(eng):
    eng.snapshot = MarketSnapshot(
        perp_mark=MARK, spot_mark=MARK, funding_bps=3.0,
        margin_multiple=eng.broker.margin, observed_at_s=time.time(),
        funding_print_bps=3.0, funding_print_ms=int(time.time() * 1000))


def candle(eng, *, ms, print_ms=None):
    observe(eng)
    return eng.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                         timestamp_ms=ms, funding_print_ms=print_ms or ms)


# ---------------------------------------------------------------------------
# 1. a close that fills short
# ---------------------------------------------------------------------------


class TestAPartialCloseIsNotAClose:
    def test_overlay_a_half_filled_buy_back_halts_and_keeps_the_residual(self):
        v = Venue()
        eng = build(v)
        candle(eng, ms=1_000)
        assert v.perp_short == pytest.approx(1.0)

        v.close_fill = 0.5
        v.margin = 1.1                       # force the unwind
        d = candle(eng, ms=61_000, print_ms=1_000)

        assert d.state is BookState.HALTED
        assert d.reason == "UNWIND_PARTIAL"
        assert eng.position is not None, "the book forgot a live short"
        assert eng.position.perp.filled_qty == pytest.approx(0.5)
        assert v.perp_short == pytest.approx(0.5)
        assert eng.position.perp.filled_qty == pytest.approx(v.perp_short)

    def test_the_residual_is_written_down_so_a_restart_can_see_it(self):
        v = Venue()
        eng = build(v)
        candle(eng, ms=1_000)
        v.close_fill = 0.5
        v.margin = 1.1
        candle(eng, ms=61_000, print_ms=1_000)

        last = eng.written[-1]
        assert last["book_state"] == "HALTED"
        assert last["position"] is not None
        assert last["position"]["perp"]["filled_qty"] == pytest.approx(0.5)

    def test_a_partial_close_trips_the_kill_switch(self):
        v = Venue()
        eng = build(v)
        candle(eng, ms=1_000)
        v.close_fill = 0.5
        v.margin = 1.1
        candle(eng, ms=61_000, print_ms=1_000)
        assert eng.tripped, "a naked residual did not call a human"

    def test_a_halted_book_opens_nothing_on_the_next_candle(self):
        """The failure this prevents: FLAT plus a live short is a second pair."""
        v = Venue()
        eng = build(v)
        candle(eng, ms=1_000)
        v.close_fill = 0.5
        v.margin = 1.1
        candle(eng, ms=61_000, print_ms=1_000)
        before = len(v.orders)
        v.margin = 5.0                        # the squeeze passes
        d = candle(eng, ms=121_000, print_ms=121_000)
        assert d.reason == "KILL_SWITCH_ENGAGED"
        assert len(v.orders) == before, "a halted book sent an order"

    def test_a_complete_buy_back_still_goes_flat(self):
        """The regression guard: a full close is unchanged."""
        v = Venue()
        eng = build(v)
        candle(eng, ms=1_000)
        v.margin = 1.1
        d = candle(eng, ms=61_000, print_ms=1_000)
        assert d.action == "unwound"
        assert eng.position is None
        assert eng.state is BookState.FLAT
        assert v.perp_short == pytest.approx(0.0)

    def test_acquire_a_half_filled_close_halts_on_either_leg(self):
        v = Venue()
        eng = build(v, mode=ACQUIRE)
        candle(eng, ms=1_000)
        assert v.spot_long == pytest.approx(eng.position.spot_held_qty)
        assert v.perp_short == pytest.approx(eng.position.perp.filled_qty)

        v.close_fill = 0.5
        v.margin = 1.1
        d = candle(eng, ms=61_000, print_ms=1_000)

        assert d.state is BookState.HALTED
        assert d.reason == "UNWIND_PARTIAL"
        assert eng.position.perp.filled_qty == pytest.approx(v.perp_short)
        assert eng.position.spot.filled_qty == pytest.approx(v.spot_long)

    def test_the_emergency_spot_unwind_halts_when_the_sale_fills_short(self):
        """The perp never landed and only half the naked spot could be sold."""
        v = Venue(reject={"linear"}, close_fill=0.5)
        eng = build(v, mode=ACQUIRE)
        d = candle(eng, ms=1_000)

        assert d.state is BookState.HALTED
        assert d.reason == "NAKED_SPOT_UNWIND_PARTIAL"
        assert eng.position is not None
        assert eng.position.spot.filled_qty == pytest.approx(v.spot_long)
        assert v.spot_long == pytest.approx(0.5 - 0.25 / MARK)
        # No hedge is claimed for it, because none exists.
        assert eng.position.perp.filled_qty == pytest.approx(0.0)

    def test_a_complete_emergency_spot_unwind_still_goes_flat(self):
        v = Venue(reject={"linear"})
        eng = build(v, mode=ACQUIRE)
        d = candle(eng, ms=1_000)
        assert d.action == "unwound"
        assert eng.position is None
        assert v.spot_long == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 2. the inventory an overlay hedges can leave
# ---------------------------------------------------------------------------


class TestTheOverlayKeepsCheckingItsInventory:
    def test_inventory_withdrawn_entirely_closes_the_hedge(self):
        v = Venue()
        eng = build(v)
        candle(eng, ms=1_000)
        v.inventory = 0.0                     # the client moves their BTC out

        d = candle(eng, ms=61_000, print_ms=61_000)
        assert d.action == "unwound"
        assert d.reason == "INVENTORY_WITHDRAWN"
        assert eng.position is None
        assert v.perp_short == pytest.approx(0.0)

    def test_inventory_halved_reduces_the_hedge_to_what_is_covered(self):
        v = Venue()
        eng = build(v)
        candle(eng, ms=1_000)
        v.inventory = 0.5

        d = candle(eng, ms=61_000, print_ms=61_000)
        assert d.reason == "HEDGE_REDUCED_TO_INVENTORY"
        assert eng.state is BookState.HEDGED
        assert v.perp_short == pytest.approx(0.5)
        assert eng.position.perp.filled_qty == pytest.approx(0.5)
        assert eng.position.delta_qty == pytest.approx(0.0)

    def test_a_book_whose_inventory_is_intact_is_left_alone(self):
        v = Venue()
        eng = build(v)
        candle(eng, ms=1_000)
        before = len(v.orders)
        d = candle(eng, ms=61_000, print_ms=61_000)
        assert d.action == "hold"
        assert d.reason == "HEDGED_AND_COLLECTING"
        assert len(v.orders) == before, "it traded against an intact inventory"

    def test_an_unreadable_inventory_halts_rather_than_assuming(self):
        v = Venue()
        eng = build(v)
        candle(eng, ms=1_000)
        v.inventory_boom = True

        d = candle(eng, ms=61_000, print_ms=61_000)
        assert d.state is BookState.HALTED
        assert d.reason == "INVENTORY_UNREADABLE"

    def test_inventory_gone_and_the_buy_back_refused_halts(self):
        v = Venue()
        eng = build(v)
        candle(eng, ms=1_000)
        v.inventory = 0.0
        v.reject = {"linear"}

        d = candle(eng, ms=61_000, print_ms=61_000)
        assert d.state is BookState.HALTED
        assert d.reason == "INVENTORY_LEFT_AND_HEDGE_WOULD_NOT_CLOSE"

    def test_a_shortfall_below_one_venue_lot_does_not_halt_the_book(self):
        """Dust is not an incident, and halting on it would make the gate
        unusable. There is no order that would reduce it."""
        v = Venue()
        eng = build(v)
        candle(eng, ms=1_000)
        v.inventory = 1.0 - 0.0005            # half a lot uncovered
        before = len(v.orders)

        d = candle(eng, ms=61_000, print_ms=61_000)
        assert eng.state is BookState.HEDGED
        assert d.action == "hold"
        assert len(v.orders) == before
        # and funding still books, because the book is still a book
        assert eng.position.funding_collected != 0.0

    def test_acquire_mode_never_asks_about_inventory(self):
        """In acquire the book bought its own spot; the client's wallet is not
        the hedge, and reading it would be a gate on somebody else's money."""
        v = Venue()
        eng = build(v, mode=ACQUIRE)
        candle(eng, ms=1_000)
        before = v.inventory_calls
        candle(eng, ms=61_000, print_ms=61_000)
        assert v.inventory_calls == before

    def test_the_reduced_hedge_is_written_down(self):
        v = Venue()
        eng = build(v)
        candle(eng, ms=1_000)
        v.inventory = 0.5
        candle(eng, ms=61_000, print_ms=61_000)
        last = eng.written[-1]
        assert last["position"]["perp"]["filled_qty"] == pytest.approx(0.5)


class _VenueReadsItsShort(Venue):
    def get_perp_position(self, symbol):
        return self.perp_short


@pytest.mark.parametrize("mode", [OVERLAY, ACQUIRE])
def test_a_margin_exit_closes_the_venues_short_not_only_the_recorded_one(mode):
    """A margin/liquidation exit runs before the per-tick size check. Closing
    only the recorded size marked the book FLAT with the excess still short."""
    v = _VenueReadsItsShort()
    eng = build(v, mode=mode)
    candle(eng, ms=1_000)
    v.perp_short += 0.4          # a fill the book missed, or a manual add
    v.margin = 1.1
    d = candle(eng, ms=61_000, print_ms=1_000)
    assert d.action == "unwound"
    assert eng.state is BookState.FLAT
    assert v.perp_short < 1e-6


def test_a_naked_spot_that_cannot_be_sold_is_written_to_the_ledger():
    """ACQUIRE: the perp is rejected and the emergency spot sale fills nothing.
    The halt used to leave `position: null` in the ledger beside a naked long."""
    v = Venue(reject={"linear"}, close_fill=0.0)
    eng = build(v, mode=ACQUIRE)
    d = candle(eng, ms=1_000)
    assert d.reason == "NAKED_SPOT_UNWIND_FAILED"
    assert eng.state is BookState.HALTED
    assert eng.written[-1]["book_state"] == "HALTED"
    assert eng.written[-1]["position"] is not None
    assert eng.written[-1]["position"]["spot"]["filled_qty"] > 0


def test_a_failing_ledger_write_on_a_print_tick_does_not_stop_the_margin_exit():
    """The held print is booked in memory first; its write is made after the
    gates, so a failing store cannot leave a below-floor short open."""
    v = _VenueReadsItsShort()
    eng = build(v, mode=OVERLAY)
    candle(eng, ms=1_000)
    v.margin = 1.1

    def broken(state):
        raise OSError("disk full")
    eng.persist = broken
    candle(eng, ms=61_000, print_ms=61_000)
    # The exit itself was sent and filled; the failing store then halts the
    # book on the unwind's own write, as it always did.
    assert v.perp_short < 1e-6


@pytest.mark.parametrize("mode", [OVERLAY, ACQUIRE])
def test_a_margin_exit_after_a_partial_liquidation_closes_what_the_venue_holds(mode):
    """The reduce-only buy is refused above the venue's short: after a partial
    liquidation the book is larger than the venue and the exit used to ask for
    the book's number and halt with the short still open."""
    class Reduces(_VenueReadsItsShort):
        def place_market(self, *, symbol, side, qty, product):
            if product == "linear" and side == "Buy" and qty > self.perp_short + 1e-9:
                return None                  # reduceOnly above the position
            return super().place_market(symbol=symbol, side=side, qty=qty,
                                        product=product)
    v = Reduces()
    eng = build(v, mode=mode)
    candle(eng, ms=1_000)
    v.perp_short *= 0.4                      # the venue liquidated 60%
    v.margin = 1.1
    d = candle(eng, ms=61_000, print_ms=1_000)
    assert d.action == "unwound", (d.action, d.reason)
    assert eng.state is BookState.FLAT
    assert v.perp_short < 1e-6


def test_snap_to_lot_does_not_floor_float_noise_one_lot_too_low():
    from carry_engine import snap_to_lot
    assert snap_to_lot(0.026049 - 0.000026, 1e-6) == pytest.approx(0.026023)
    assert snap_to_lot(0.00127, 0.001) == pytest.approx(0.001)
    assert snap_to_lot(0.0009999, 0.001) == 0.0


@pytest.mark.parametrize("mode", [OVERLAY, ACQUIRE])
def test_a_flat_read_never_lets_the_exit_skip_the_buy_back(mode):
    """A lagging position list reads 0.0 while the short is still open. Going
    FLAT on that left a live short behind a FLAT book; the recorded size is
    still bought back (reduce-only) and the short really closes."""
    class Lagging(_VenueReadsItsShort):
        def get_perp_position(self, symbol):
            return 0.0                       # the list lags the real position
    v = Lagging()
    eng = build(v, mode=mode)
    candle(eng, ms=1_000)
    v.margin = 1.1
    d = candle(eng, ms=61_000, print_ms=1_000)
    assert d.action == "unwound", (d.action, d.reason)
    assert v.perp_short < 1e-6, "the exit went FLAT with the short still open"


@pytest.mark.parametrize("mode", [OVERLAY, ACQUIRE])
def test_a_refused_buy_back_keeps_the_position_and_halts(mode):
    """If the venue refuses the reduce-only buy (the short really is gone, or
    anything else), the book cannot tell which: it halts WITH the position."""
    class Refuses(_VenueReadsItsShort):
        def place_market(self, *, symbol, side, qty, product):
            if product == "linear" and side == "Buy":
                return None
            return super().place_market(symbol=symbol, side=side, qty=qty,
                                        product=product)
    v = Refuses()
    eng = build(v, mode=mode)
    candle(eng, ms=1_000)
    v.margin = 1.1
    d = candle(eng, ms=61_000, print_ms=1_000)
    assert d.action == "halted" and d.reason == "UNWIND_INCOMPLETE"
    assert eng.position is not None and eng.state is BookState.HALTED


def test_an_unreadable_venue_position_still_closes_the_recorded_short():
    class Unreadable(_VenueReadsItsShort):
        def get_perp_position(self, symbol):
            raise RuntimeError("position list unreadable")
    v = Unreadable()
    eng = build(v, mode=OVERLAY)
    candle(eng, ms=1_000)
    v.margin = 1.1
    d = candle(eng, ms=61_000, print_ms=1_000)
    assert d.action == "unwound", (d.action, d.reason)
    assert v.perp_short < 1e-6


def test_an_overlay_partial_close_never_records_more_spot_than_the_client_holds():
    """The venue short being closed was LARGER than the book's (a fill the book
    missed) and the buy-back filled only a tenth: the remainder left to hedge
    must not be written as client spot above what the book recorded."""
    class Half(_VenueReadsItsShort):
        def place_market(self, *, symbol, side, qty, product):
            if product == "linear" and side == "Buy":
                qty = qty * 0.1              # fills a tenth of what was asked
            return super().place_market(symbol=symbol, side=side, qty=qty,
                                        product=product)
    v = Half()
    eng = build(v, mode=OVERLAY)
    candle(eng, ms=1_000)
    book_spot = eng.position.spot.filled_qty
    v.perp_short += 0.4                      # a short the book never recorded
    v.margin = 1.1
    d = candle(eng, ms=61_000, print_ms=1_000)
    assert d.reason == "UNWIND_PARTIAL", (d.action, d.reason)
    assert eng.position.spot.filled_qty <= book_spot + 1e-9


@pytest.mark.parametrize("mode", [OVERLAY, ACQUIRE])
def test_an_exit_sized_from_a_stale_read_does_not_declare_flat_over_a_live_short(mode):
    """The close was sized from one stale position read (a fraction of the real
    short). FLAT was inferred from the fills; it is now confirmed against the
    venue, so the short that is still open is recorded and the book halts, and
    (ACQUIRE) the spot that hedges it is not sold with it."""
    class StaleOnce(_VenueReadsItsShort):
        reads = 0

        def get_perp_position(self, symbol):
            type(self).reads += 1
            return self.perp_short * 0.2 if type(self).reads == 1 \
                else self.perp_short
    v = StaleOnce()
    eng = build(v, mode=mode)
    candle(eng, ms=1_000)
    short_before = v.perp_short
    spot_before = getattr(v, "spot_long", 0.0)
    v.margin = 1.1
    d = candle(eng, ms=61_000, print_ms=1_000)
    assert d.action == "halted" and d.reason == "UNWIND_PARTIAL", (d.action, d.reason)
    assert eng.state is BookState.HALTED and eng.position is not None
    assert v.perp_short == pytest.approx(0.8 * short_before)
    assert eng.position.perp.filled_qty == pytest.approx(v.perp_short)
    if mode == ACQUIRE:
        assert v.spot_long == pytest.approx(0.8 * spot_before, rel=0.05), (
            "the spot hedging the surviving short was sold")


@pytest.mark.parametrize("mode", [OVERLAY, ACQUIRE])
def test_a_partial_liquidation_still_closes_to_flat_and_sells_all_the_spot(mode):
    """The case the venue-size sizing exists for must still end FLAT."""
    v = _VenueReadsItsShort()
    eng = build(v, mode=mode)
    candle(eng, ms=1_000)
    v.perp_short *= 0.4
    v.margin = 1.1
    d = candle(eng, ms=61_000, print_ms=1_000)
    assert d.action == "unwound" and eng.state is BookState.FLAT
    assert v.perp_short < 1e-6
    if mode == ACQUIRE:
        assert v.spot_long < 1e-6


class _ReadsScript(_VenueReadsItsShort):
    """get_perp_position answers from a script first (one entry per call), then
    tells the truth. Buy-backs of the linear leg fill `fill_ratio` of the ask."""
    def __init__(self, script=(), fill_ratio=1.0):
        super().__init__()
        self.script = list(script)
        self.fill_ratio = fill_ratio

    def get_perp_position(self, symbol):
        if self.script:
            value = self.script.pop(0)
            return self.perp_short if value is None else value
        return self.perp_short

    def place_market(self, *, symbol, side, qty, product):
        if product == "linear" and side == "Buy":
            qty = qty * self.fill_ratio
        return super().place_market(symbol=symbol, side=side, qty=qty,
                                    product=product)


@pytest.mark.parametrize("mode", [OVERLAY, ACQUIRE])
def test_a_flat_read_after_a_partial_close_cannot_erase_the_unfilled_remainder(mode):
    """The buy-back filled 80%; the post-close position read lies flat (a
    lagging or empty list). The venue may only ADD to what the fills say is
    left: the book halts with the short that is still open, not FLAT."""
    v = _ReadsScript(script=[None, 0.0], fill_ratio=0.8)   # sizing read ok, then flat
    eng = build(v, mode=mode)
    eng.UNWIND_REREAD_WAIT_S = 0
    candle(eng, ms=1_000)
    v.script = [None, 0.0]
    short_before = v.perp_short
    v.margin = 1.1
    d = candle(eng, ms=61_000, print_ms=1_000)
    assert d.action == "halted" and d.reason == "UNWIND_PARTIAL", (d.action, d.reason)
    assert eng.position is not None and eng.state is BookState.HALTED
    assert v.perp_short == pytest.approx(0.2 * short_before)
    assert eng.position.perp.filled_qty == pytest.approx(0.2 * short_before)


@pytest.mark.parametrize("mode", [OVERLAY, ACQUIRE])
def test_a_lagging_read_right_after_a_complete_close_is_re_read_not_believed(mode):
    """The close completed but the first position read shows the PRE-close
    size. The disagreement is re-read after a pause before it is believed, so
    a finished exit is not turned into a false partial halt (which in ACQUIRE
    left the spot long unsold)."""
    v = _ReadsScript(script=[])
    eng = build(v, mode=mode)
    eng.UNWIND_REREAD_WAIT_S = 0
    candle(eng, ms=1_000)
    pre_close = v.perp_short
    v.script = [None, pre_close]          # sizing read ok, post-close read lags
    v.margin = 1.1
    d = candle(eng, ms=61_000, print_ms=1_000)
    assert d.action == "unwound" and eng.state is BookState.FLAT, (d.action, d.reason)
    assert v.perp_short < 1e-6
    if mode == ACQUIRE:
        assert v.spot_long < 1e-6


@pytest.mark.parametrize("mode", [OVERLAY, ACQUIRE])
def test_a_stale_read_on_a_partial_close_is_re_read_before_it_is_believed(mode):
    """The buy-back filled 60%; the post-close list still shows the PRE-close
    size once. The excess over the fills is re-read after a pause, so the
    persisted short is what the venue holds, not the stale figure."""
    v = _ReadsScript(script=[], fill_ratio=0.6)
    eng = build(v, mode=mode)
    eng.UNWIND_REREAD_WAIT_S = 0
    candle(eng, ms=1_000)
    pre_close = v.perp_short
    v.script = [None, pre_close]            # sizing read ok, then one stale read
    v.margin = 1.1
    d = candle(eng, ms=61_000, print_ms=1_000)
    assert d.reason == "UNWIND_PARTIAL", (d.action, d.reason)
    assert eng.position.perp.filled_qty == pytest.approx(0.4 * pre_close)
    assert v.perp_short == pytest.approx(0.4 * pre_close)


def test_a_halt_whose_write_failed_is_written_once_the_store_recovers():
    """`_halt` swallowed a failed persist and never tried again, leaving the
    ledger showing a book (and no noted exposure) that the halt had replaced."""
    v = _VenueReadsItsShort()
    eng = build(v, mode=OVERLAY)
    candle(eng, ms=1_000)
    writes = []
    healthy = eng.persist
    state = {"fail": True}

    def flaky(s):
        if state["fail"]:
            raise OSError("disk full")
        writes.append(s)
        healthy(s)
    eng.persist = flaky
    v.perp_short += 0.4                     # venue drift -> VENUE_POSITION_DRIFT halt
    d = candle(eng, ms=61_000, print_ms=1_000)
    assert d.action == "halted" and not writes
    state["fail"] = False
    candle(eng, ms=121_000, print_ms=1_000)
    assert writes and writes[-1]["book_state"] == "HALTED"
