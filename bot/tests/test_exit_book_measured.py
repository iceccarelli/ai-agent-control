"""An EXIT is book-measured exactly like an ENTRY.

THE DEFECT (production samples report: linear/Sell 2 of 20 valid, linear/Buy
0 of 20, `NOT_BOOK_MEASURED: 2` on the Buy side)
================================================================================
`CarryEngine._declare_decision` told the ledger which price an order was decided
at, and it could not do so for an exit in two ways:

 1. OUTSIDE A TICK. `_decision_marks` is cleared when `on_candle` returns
    (`_clears_decision_marks`). The drill's final unwind, the held-position
    watch without a market view and the other risk exits call `_unwind`/`_fire`
    outside `on_candle`, so there was no decision price: the engine ended the
    context and the fill's execution record said `NO_DECISION_CONTEXT`
    (UNAVAILABLE). This is the path the drill's recorded exits took.
 2. INSIDE A TICK WITH AN OLD BOOK. The tick's snapshot book is usable for 3 s
    (`TICK_BOOK_MAX_AGE_S`). An exit with an older one (a drill holds across a
    settlement) was told `capture_book=False` ("getting out is never slowed"),
    so the record fell back to the MARK (`reference_basis == "mark"`), which the
    cost estimator drops as NOT_BOOK_MEASURED.

An entry with no fresh tick book reads the touch itself and is measured, so the
two sides of the round trip were sampled asymmetrically.

THE FIX: an exit with no fresh tick book takes a bounded, fail-open public
top-of-book read (public GET only, a hard wall-clock bound, the exit proceeds
unmeasured and is recorded as such if the read fails). A book is never invented
from the mark, the last price or a stale observation.

These tests drive the REAL engine -> LedgerBroker -> CarryBroker -> Journal
against a scripted venue.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import carry_engine as ce                                    # noqa: E402
import execution_record as xr                                # noqa: E402
import market_snapshot as msnap                              # noqa: E402
from market_snapshot import MarketSnapshot                   # noqa: E402
from test_carry_fee_truth import FeeVenue, MARK, modern      # noqa: E402
from test_carry_broker import envelope                       # noqa: E402
from test_paired_leg_recovery import stack, warm_and_open    # noqa: E402


class DepthVenue(FeeVenue):
    """FeeVenue that also serves the public order book and counts reads."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.depth_calls = []
        self.depth_fail = False
        self.timeouts = []                 # (endpoint, per-call timeout)
        self.hang_s = 0.0                  # >0: bounded reads hang this long
        self.bids = [(MARK - 5, 0.5), (MARK - 10, 1.0), (MARK - 20, 5.0)]
        self.asks = [(MARK + 5, 0.5), (MARK + 10, 1.0), (MARK + 20, 5.0)]
        self.book_ts = None                # None -> "now" at the venue

    def _request(self, method, endpoint, *, params=None, body=None,
                 signed=False, retries=3, timeout_s=None):
        if timeout_s is not None:
            self.timeouts.append((endpoint, timeout_s))
            if self.hang_s:
                # a venue that does not answer: the per-call timeout is what
                # ends the wait, as the real transport's does
                time.sleep(min(self.hang_s, timeout_s))
                raise TimeoutError("read timed out")
        if endpoint == "/v5/market/orderbook":
            assert signed is False, "the depth read is PUBLIC"
            assert method == "GET"
            self.depth_calls.append((params, retries))
            if self.depth_fail:
                raise RuntimeError("orderbook down")
            return envelope({
                "s": params["symbol"],
                "b": [[str(p), str(q)] for p, q in self.bids],
                "a": [[str(p), str(q)] for p, q in self.asks],
                "ts": self.book_ts or int(time.time() * 1000), "u": 1})
        return super()._request(method, endpoint, params=params, body=body,
                                signed=signed, retries=retries)


def old_view(age_s: float, *, mark=MARK):
    """A snapshot whose books were read `age_s` seconds ago (a drill that held
    across a settlement)."""
    t = time.time() - age_s
    return MarketSnapshot(
        perp_mark=mark, spot_mark=mark, funding_bps=-3.0, margin_multiple=5.0,
        observed_at_s=time.time(), perp_book=(mark - 5.0, mark + 5.0),
        spot_book=(mark - 5.0, mark + 5.0), book_observed_s=t)


def opened(tmp_path, venue=None):
    venue = venue or DepthVenue(modern, spot_qty=0.0)
    store, venue, broker, engine = stack(str(tmp_path / "s.db"), venue)
    assert warm_and_open(engine).action == "opened"
    return store, venue, engine


def fills(engine, *kinds):
    return [e for e in engine.broker.journal.entries if e.kind in kinds]


def test_the_entry_is_book_measured(tmp_path):
    store, _v, engine = opened(tmp_path)
    for e in fills(engine, "perp_open", "spot_buy"):
        rec = e.fee_raw["execution"]
        assert rec["status"] == xr.MEASURED
        assert rec["reference_basis"] == "executable_side", rec
    store.close()


def test_drill_style_unwind_outside_a_tick_is_book_measured(tmp_path):
    """THE REPRODUCER for cause 1. The drill calls `engine._unwind` straight
    after its hold loop, outside `on_candle`; seconds after the entry's book."""
    store, venue, engine = opened(tmp_path)
    assert engine._decision_marks == {}            # on_candle has returned
    engine.snapshot = old_view(60.0)               # the hold's last snapshot
    d = engine._unwind(MARK, "DRILL_COMPLETE")
    assert d.action == "unwound", d
    exits = fills(engine, "perp_close", "spot_sell")
    assert {e.kind for e in exits} == {"perp_close", "spot_sell"}
    for e in exits:
        rec = e.fee_raw["execution"]
        assert rec["status"] == xr.MEASURED, rec
        assert rec["reference_basis"] == "executable_side", rec
        assert rec["spread_cost_usd"] is not None
        assert rec["decision_bid"] and rec["decision_ask"]
    store.close()


def test_in_tick_exit_with_an_old_snapshot_book_is_book_measured(tmp_path):
    """Cause 2: inside `on_candle`, the snapshot's book is older than
    TICK_BOOK_MAX_AGE_S, so it must be re-read rather than skipped."""
    store, venue, engine = opened(tmp_path)
    engine.position.negative_funding_streak = ce.NEGATIVE_FUNDING_EXIT_PRINTS
    engine.snapshot = old_view(10.0)
    d = engine.on_candle(mark=MARK, funding_bps=-3.0, spot=MARK,
                         funding_print_ms=0)
    assert d.action == "unwound", d
    for e in fills(engine, "perp_close", "spot_sell"):
        rec = e.fee_raw["execution"]
        assert rec["status"] == xr.MEASURED, rec
        assert rec["reference_basis"] == "executable_side", rec
    store.close()


def test_exit_and_entry_are_sampled_alike(tmp_path):
    store, venue, engine = opened(tmp_path)
    engine.snapshot = old_view(60.0)
    engine._unwind(MARK, "DRILL_COMPLETE")
    by_kind = {e.kind: e.fee_raw["execution"] for e in engine.broker.journal.entries
               if e.kind in ("perp_open", "perp_close", "spot_buy", "spot_sell")}
    assert len(by_kind) == 4
    for rec in by_kind.values():
        assert (rec["status"], rec["reference_basis"]) == (
            xr.MEASURED, "executable_side"), rec
    store.close()


def test_a_fresh_tick_book_is_used_and_nothing_is_read(tmp_path):
    store, venue, engine = opened(tmp_path)
    venue.depth_calls.clear()
    reads_before = [c for c in venue.calls if c[1] == "/v5/market/tickers"]
    engine.position.negative_funding_streak = ce.NEGATIVE_FUNDING_EXIT_PRINTS
    engine.snapshot = old_view(0.2)
    engine.on_candle(mark=MARK, funding_bps=-3.0, spot=MARK, funding_print_ms=0)
    assert venue.depth_calls == []
    assert [c for c in venue.calls if c[1] == "/v5/market/tickers"] == reads_before
    store.close()


def test_stale_books_are_never_used(tmp_path):
    """An old snapshot book is not the decision book (it is re-read), and a book
    that is nevertheless older than the observation limit is rejected by the
    record, never repaired."""
    store, venue, engine = opened(tmp_path)
    for age in (ce.CarryEngine.TICK_BOOK_MAX_AGE_S + 0.5, 10.0, 60.0):
        engine.snapshot = old_view(age)
        assert engine._tick_book("linear") is None
        assert engine._tick_book("spot") is None
    engine.snapshot = old_view(0.1)
    assert engine._tick_book("linear") is not None
    ctx = xr.DecisionContext(venue="bybit", symbol="BTCUSDT", product="linear",
                             side="Buy", decision_ms=1_700_000_010_000, qty=0.01,
                             mark=MARK, bid=MARK - 5, ask=MARK + 5,
                             observed_ms=1_700_000_010_000
                             - xr.MAX_OBSERVATION_AGE_MS - 1)
    px = xr.validate_decision(ctx, now_ms=1_700_000_010_100)
    assert not px.ok and "OBSERVATION_STALE" in px.reasons
    store.close()


# -- fail open ------------------------------------------------------------------

def test_a_failed_book_read_never_stops_the_exit(tmp_path):
    venue = DepthVenue(modern, spot_qty=0.0)
    store, venue, engine = opened(tmp_path, venue)
    venue.depth_fail = True
    real = venue._request

    def request(method, endpoint, **kw):
        if endpoint == "/v5/market/tickers" and not kw.get("signed"):
            raise RuntimeError("tickers down")
        return real(method, endpoint, **kw)
    venue._request = request
    engine.snapshot = old_view(60.0)
    d = engine._unwind(MARK, "DRILL_COMPLETE")
    assert d.action == "unwound", d
    recs = [e.fee_raw["execution"] for e in fills(engine, "perp_close", "spot_sell")]
    assert recs
    for rec in recs:
        assert rec["reference_basis"] != "executable_side"
        assert rec["status"] in (xr.UNAVAILABLE, xr.MEASURED)
        # no book was invented: the decision carries no bid/ask
        assert rec["decision_bid"] is None and rec["decision_ask"] is None
    store.close()


def test_a_hanging_book_read_delays_the_exit_by_no_more_than_the_bound(tmp_path):
    """The venue answers nothing: each exit leg waits for its read's own
    timeout and no longer, and then goes on (unmeasured)."""
    venue = DepthVenue(modern, spot_qty=0.0)
    store, venue, engine = opened(tmp_path, venue)
    venue.hang_s = 30.0
    engine.snapshot = old_view(60.0)
    started = time.monotonic()
    d = engine._unwind(MARK, "DRILL_COMPLETE")
    took = time.monotonic() - started
    assert d.action == "unwound", d
    bound = ce.CarryEngine.EXIT_BOOK_READ_TIMEOUT_S
    assert venue.timeouts and all(t <= bound for _e, t in venue.timeouts)
    # two exit legs (perp + spot), each within one shared deadline of `bound`
    assert took < 2 * bound + 1.0, took
    for e in fills(engine, "perp_close", "spot_sell"):
        rec = e.fee_raw["execution"]
        assert rec["decision_bid"] is None and rec["reference_basis"] != "executable_side"
    store.close()


def test_the_exit_read_is_public_and_not_retried(tmp_path):
    store, venue, engine = opened(tmp_path)
    venue.depth_calls.clear()
    engine.snapshot = old_view(60.0)
    engine._unwind(MARK, "DRILL_COMPLETE")
    assert venue.depth_calls, "the exit should have read the book"
    for params, retries in venue.depth_calls:
        assert retries == 0
        assert params["symbol"] == "BTCUSDT"
    store.close()


def test_an_order_is_never_sent_by_the_book_read(tmp_path):
    store, venue, engine = opened(tmp_path)
    n_before = len(venue.created)
    engine.snapshot = old_view(60.0)
    engine._unwind(MARK, "DRILL_COMPLETE")
    # exactly the exit orders: perp buy + spot sell, nothing else
    new = venue.created[n_before:]
    assert sorted((b["category"], b["side"]) for b in new) == [
        ("linear", "Buy"), ("spot", "Sell")]
    store.close()


# -- the declaration itself ----------------------------------------------------------

class _Spy:
    def __init__(self):
        self.calls = []

    def begin_decision(self, **kw):
        self.calls.append(("begin", kw))

    def end_decision(self, **kw):
        self.calls.append(("end", kw))


def _engine(marks, view=None):
    e = ce.CarryEngine.__new__(ce.CarryEngine)
    e.broker = _Spy()
    e._decision_marks = marks
    e.snapshot = view
    return e


def test_an_exit_outside_a_tick_is_declared_without_a_mark():
    e = _engine({})                       # on_candle has returned
    e._declare_decision("BTCUSDT", "linear", "Buy", 0.01)
    e._declare_decision("BTCUSDT", "spot", "Sell", 0.01)
    assert [c[0] for c in e.broker.calls] == ["begin", "begin"]
    for _name, kw in e.broker.calls:
        assert kw["mark"] is None and kw["capture_book"] is True
        assert 0 < kw["book_timeout_s"] <= 2.0 and "bid" not in kw


def test_an_entry_outside_a_tick_is_still_not_declared():
    e = _engine({})
    e._declare_decision("BTCUSDT", "linear", "Sell", 0.01)
    e._declare_decision("BTCUSDT", "spot", "Buy", 0.01)
    assert [c[0] for c in e.broker.calls] == ["end", "end"]


def test_a_fresh_tick_book_is_passed_with_its_source_and_nothing_is_read():
    e = _engine({"linear": 100.0, "spot": 100.0},
                MarketSnapshot(perp_mark=100.0, spot_mark=100.0, funding_bps=1.0,
                               margin_multiple=None, observed_at_s=time.time(),
                               perp_book=(99.9, 100.1), spot_book=(99.9, 100.1),
                               book_observed_s=time.time() - 0.5))
    e._declare_decision("BTCUSDT", "linear", "Buy", 0.01)
    _n, kw = e.broker.calls[0]
    assert (kw["bid"], kw["ask"], kw["book_source"]) == (99.9, 100.1,
                                                         "tick_snapshot")
    assert "capture_book" not in kw and "book_timeout_s" not in kw


def test_the_exit_bound_is_a_small_fixed_number_of_seconds():
    assert 0 < ce.CarryEngine.EXIT_BOOK_READ_TIMEOUT_S <= 2.0
    assert ce.CarryEngine.TICK_BOOK_MAX_AGE_S < xr.MAX_OBSERVATION_AGE_MS / 1000.0
