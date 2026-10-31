"""An exit gets a decision-time book from the tick's snapshot, so its spread and
slippage can be measured, without a read at the moment of the order."""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carry_engine as ce                                    # noqa: E402
import market_snapshot as ms                                 # noqa: E402
from market_snapshot import MarketSnapshot                   # noqa: E402
from test_paired_leg_recovery import (FeeVenue, MARK, modern, stack,  # noqa: E402
                                      warm_and_open)


class Broker:
    def __init__(self, top=None, raises=False):
        self.top, self.raises, self.calls = top, raises, []

    def get_mark(self, s): return 100.0
    def get_spot_mark(self, s): return 100.0
    def get_funding_bps(self, s): return 1.0
    def get_funding_print(self, s): return (1.0, int(time.time() * 1000) - 3_600_000)
    def get_perp_position(self, s): return 0.0
    def get_margin_multiple(self, s): return 5.0

    def get_book_top(self, symbol, product):
        self.calls.append((symbol, product))
        if self.raises:
            raise RuntimeError("no ticker")
        return self.top


def snap(broker):
    return ms.take_snapshot(broker, perp_symbol="BTCUSDT", spot_symbol="BTCUSDT")


# -- the snapshot -------------------------------------------------------------------

def test_the_snapshot_carries_each_legs_touch_and_when_it_was_read():
    b = Broker({"bid": 99.9, "ask": 100.1})
    s = snap(b)
    assert s.perp_book == (99.9, 100.1) and s.spot_book == (99.9, 100.1)
    assert s.book_observed_s is not None
    assert b.calls == [("BTCUSDT", "linear"), ("BTCUSDT", "spot")]


def test_a_failed_or_missing_or_bad_touch_never_fails_the_snapshot():
    assert snap(Broker(raises=True)).perp_book is None
    assert snap(Broker({"bid": 101.0, "ask": 100.0})).perp_book is None   # crossed
    assert snap(Broker({"bid": float("nan"), "ask": 100.0})).perp_book is None
    assert snap(Broker({"bid": 0.0, "ask": 100.0})).perp_book is None
    assert snap(Broker({"bid": "x", "ask": 1})).perp_book is None
    s = snap(Broker(raises=True))
    assert s.book_observed_s is None and s.perp_mark == 100.0
    nobook = Broker()
    del Broker.get_book_top
    try:
        assert snap(nobook).perp_book is None
    finally:
        Broker.get_book_top = lambda self, symbol, product: None


def test_the_touch_is_outside_the_freshness_judgement():
    s = snap(Broker({"bid": 99.9, "ask": 100.1}))
    s.assert_fresh()                                        # unaffected by the books


# -- the engine ---------------------------------------------------------------------

class Spy:
    def __init__(self):
        self.calls = []

    def begin_decision(self, **kw):
        self.calls.append(kw)

    def end_decision(self, **kw):
        pass


def engine_with(view):
    e = ce.CarryEngine.__new__(ce.CarryEngine)
    e.broker = Spy()
    e.snapshot = view
    e._decision_marks = {"linear": 100.0, "spot": 100.0}
    return e


def view(age=0.5, perp=(99.9, 100.1), spot=(99.8, 100.2)):
    return MarketSnapshot(perp_mark=100.0, spot_mark=100.0, funding_bps=1.0,
                          margin_multiple=None, observed_at_s=time.time(),
                          perp_book=perp, spot_book=spot,
                          book_observed_s=time.time() - age)


def test_an_exit_uses_the_ticks_touch_and_does_not_read_the_book():
    e = engine_with(view())
    e._declare_decision("BTCUSDT", "linear", "Buy", 0.01)      # exit
    kw = e.broker.calls[0]
    assert (kw["bid"], kw["ask"]) == (99.9, 100.1) and kw["observed_ms"] > 0
    assert "capture_book" not in kw        # a book was supplied; nothing is read


def test_the_spot_exit_uses_the_spot_touch():
    e = engine_with(view())
    e._declare_decision("BTCUSDT", "spot", "Sell", 0.01)
    kw = e.broker.calls[0]
    assert (kw["bid"], kw["ask"]) == (99.8, 100.2)


def test_an_exit_without_a_fresh_touch_takes_no_extra_read():
    for v in (view(age=10.0), view(perp=None), None):
        e = engine_with(v)
        e._declare_decision("BTCUSDT", "linear", "Buy", 0.01)
        kw = e.broker.calls[0]
        assert kw.get("capture_book") is False and "bid" not in kw


def test_an_entry_without_a_fresh_touch_still_reads_the_book_itself():
    e = engine_with(view(age=10.0))
    e._declare_decision("BTCUSDT", "linear", "Sell", 0.01)     # entry
    kw = e.broker.calls[0]
    assert "capture_book" not in kw and "bid" not in kw


def test_a_bad_touch_in_the_snapshot_is_not_used():
    for bad in ((100.5, 100.1), (float("nan"), 1.0), (0.0, 1.0), ("x", "y")):
        e = engine_with(view(perp=bad))
        assert e._tick_book("linear") is None


# -- end to end: the exit leg is measured --------------------------------------------

def test_an_unwind_exit_is_book_measured_in_the_journal(tmp_path):
    store, venue, broker, engine = stack(str(tmp_path / "s.db"),
                                         FeeVenue(modern, spot_qty=0.0))
    assert warm_and_open(engine).action == "opened"
    engine.position.negative_funding_streak = ce.NEGATIVE_FUNDING_EXIT_PRINTS
    engine.snapshot = MarketSnapshot(
        perp_mark=MARK, spot_mark=MARK, funding_bps=-3.0, margin_multiple=5.0,
        observed_at_s=time.time(), perp_book=(MARK - 5.0, MARK + 5.0),
        spot_book=(MARK - 5.0, MARK + 5.0), book_observed_s=time.time())
    d = engine.on_candle(mark=MARK, funding_bps=-3.0, spot=MARK,
                         funding_print_ms=0)
    assert d.action == "unwound"
    exits = [e for e in engine.broker.journal.entries
             if e.kind in ("perp_close", "spot_sell")]
    assert exits
    for e in exits:
        rec = e.fee_raw["execution"]
        assert rec["status"] == "MEASURED" and rec["reference_basis"] == "executable_side", rec
        assert rec["spread_cost_usd"] is not None
    store.close()
