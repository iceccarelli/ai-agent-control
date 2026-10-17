"""A funding history with a hole in it is not a history the book may enter on.

The EWMA behind the entry gate is a list of RATES with no stamps. If the process
was down (or a poll was skipped) for several settlements, the next print was
appended next to the last one it had seen: days-old rich prints kept most of the
weight of a smoothed rate that was then read as "the current regime". The
freshness check in `MarketSnapshot.assert_fresh` only vets the NEWEST print.

Scope: the 8h cadence is proven only for BTCUSDT (cached instrument metadata,
`fundingInterval` = 480). Any other perp is refused at the entry gate.

Offline and SYNTHETIC: every rate and stamp below is invented for the test. None
is a venue observation, and no test here can place an order (the venue double
has no order methods, so an unexpected entry would fail loudly).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import market_snapshot  # noqa: E402
from carry_engine import CarryEngine  # noqa: E402

MARK = 100_000.0
H = 3_600_000
H8 = 8 * H
RICH, FRESH = 10.0, 0.3        # synthetic bps; FRESH alone cannot pay for entry
INSUFFICIENT = "INSUFFICIENT_FUNDING_HISTORY"


class Venue:
    """Fees and lot rules only. No place_market: an entry would raise."""

    def get_lot_rules(self, symbol, product):
        return {"qty_step": 1e-6, "min_qty": 1e-6, "min_notional": 0.0}

    def get_fee_rates(self, symbol, product):
        return {"maker_bps": 2.0,
                "taker_bps": 10.0 if product == "spot" else 5.5}


def engine(history, last_print_ms, **kw):
    e = CarryEngine(broker=Venue(), max_notional_usd=100.0, borrow_apr=0.05,
                    execution_mode="acquire", persist=lambda s: None, **kw)
    state = {"position": None, "funding_history": history}
    if last_print_ms is not None:
        state["last_print_ms"] = last_print_ms
    e.restore(state)
    return e


def tick(e, stamp, bps=FRESH):
    return e.on_candle(mark=MARK, funding_bps=bps, spot=MARK,
                       funding_print_ms=stamp)


# -- the defect --------------------------------------------------------------

def test_missed_settlements_do_not_leave_stale_rich_prints_in_the_ewma():
    e = engine([RICH] * 4, 4 * H8)
    d = tick(e, 10 * H8)                      # six settlements later
    assert d.action == "stand_aside", d.reason
    assert d.reason == INSUFFICIENT
    assert e._funding_history == [FRESH]


def test_one_missed_settlement_is_enough():
    e = engine([RICH] * 4, 4 * H8)
    assert tick(e, 6 * H8).reason == INSUFFICIENT      # 16h after the last
    assert RICH not in e._funding_history


def test_contiguous_prints_keep_normal_smoothing():
    e = engine([RICH] * 4, 4 * H8)
    d = tick(e, 5 * H8)
    assert d.reason != INSUFFICIENT
    assert e._funding_history == [RICH] * 4 + [FRESH]


def test_gap_boundary_is_strictly_greater_than():
    at = engine([RICH] * 4, 4 * H8)
    tick(at, 4 * H8 + CarryEngine.MAX_PRINT_GAP_MS)
    assert at._funding_history == [RICH] * 4 + [FRESH]      # exactly 12h: kept
    over = engine([RICH] * 4, 4 * H8)
    tick(over, 4 * H8 + CarryEngine.MAX_PRINT_GAP_MS + 1)
    assert over._funding_history == [FRESH]                  # 12h + 1ms: cleared


def test_threshold_is_tied_to_the_snapshot_freshness_interval():
    assert CarryEngine.MAX_PRINT_GAP_MS == 1.5 * market_snapshot.FUNDING_INTERVAL_MS


# -- refusal, and eligibility returning only after fresh observations ---------

def test_entry_stays_refused_until_three_fresh_contiguous_prints():
    e = engine([RICH] * 4, 4 * H8)
    assert tick(e, 10 * H8).reason == INSUFFICIENT
    assert tick(e, 11 * H8).reason == INSUFFICIENT
    d = tick(e, 12 * H8)
    assert RICH not in e._funding_history
    assert d.reason != INSUFFICIENT              # judged on fresh data only,
    assert d.action == "stand_aside"             # and 0.3 bps cannot pay for it


def test_duplicate_and_out_of_order_stamps_cannot_fake_fresh_history():
    e = engine([RICH] * 4, 4 * H8)
    tick(e, 10 * H8)
    for stamp in (10 * H8, 10 * H8, 9 * H8, 5 * H8, 4 * H8):    # replay / older
        assert tick(e, stamp).reason == INSUFFICIENT
    assert e._funding_history == [FRESH]         # still ONE observation
    assert tick(e, 11 * H8).reason == INSUFFICIENT
    assert e._funding_history == [FRESH, FRESH]


def test_malformed_print_does_not_extend_the_history():
    e = engine([RICH] * 4, 4 * H8)
    for bps in (float("nan"), float("inf")):
        tick(e, 5 * H8, bps=bps)
    assert e._funding_history == [RICH] * 4      # ignored, last stamp unmoved


def test_non_finite_stamp_or_rate_is_ignored_and_never_raises():
    e = engine([RICH] * 4, 4 * H8)
    for stamp, bps in ((float("nan"), FRESH), (float("inf"), FRESH),
                       (5 * H8, float("-inf"))):
        tick(e, stamp, bps=bps)
    assert e._funding_history == [RICH] * 4
    assert e._last_print_ms == 4 * H8


# -- restart / restore --------------------------------------------------------

def test_restore_with_history_but_no_stamp_is_discarded():
    e = engine([RICH] * 4, None)
    assert e._funding_history == []
    assert tick(e, 5 * H8).reason == INSUFFICIENT


def test_restart_after_downtime_does_not_reintroduce_stale_history():
    e = engine([RICH] * 4, 4 * H8)
    tick(e, 10 * H8)                              # gap cleared, then persisted
    restarted = CarryEngine(broker=Venue(), max_notional_usd=100.0,
                            borrow_apr=0.05, execution_mode="acquire",
                            persist=lambda s: None)
    restarted.restore(e.to_state())
    assert restarted._funding_history == [FRESH]
    assert tick(restarted, 11 * H8).reason == INSUFFICIENT


def test_warm_up_keeps_only_the_newest_contiguous_run_and_dedupes():
    e = CarryEngine(broker=Venue(), max_notional_usd=100.0, borrow_apr=0.05,
                    execution_mode="acquire", persist=lambda s: None)
    prints = [(RICH, 1 * H8), (RICH, 2 * H8),            # old run
              (FRESH, 6 * H8), (FRESH, 6 * H8),          # hole before; duplicate
              (FRESH, 7 * H8), (FRESH, 8 * H8)]
    assert e.warm_funding_history(prints) == 3
    assert e._funding_history == [FRESH] * 3
    assert e._last_print_ms == 8 * H8


# -- scope: an unproven cadence never trades ---------------------------------

def test_symbol_without_proven_8h_cadence_is_refused_even_with_fresh_history():
    # A 4h symbol could hide a missed settlement inside the 12h tolerance, and
    # the carry-per-day maths would be wrong too. No metadata proves any other
    # symbol, so the gate refuses rather than guesses.
    e = engine([RICH] * 4, 4 * H8, perp_symbol="ETHUSDT", spot_symbol="ETHUSDT")
    d = tick(e, 5 * H8, bps=RICH)                # rich, contiguous, would pass
    assert d.action == "stand_aside"
    assert d.reason == "FUNDING_CADENCE_UNPROVEN"


def test_btcusdt_is_not_refused_for_cadence():
    e = engine([RICH] * 4, 4 * H8)
    assert tick(e, 5 * H8).reason != "FUNDING_CADENCE_UNPROVEN"


# -- persisted state belongs to ONE market -----------------------------------

def test_state_records_the_symbol_it_was_built_on():
    e = engine([RICH] * 4, 4 * H8)
    assert e.to_state()["perp_symbol"] == "BTCUSDT"


def test_history_from_another_symbol_is_not_restored():
    btc = engine([RICH] * 4, 4 * H8)
    other = CarryEngine(broker=Venue(), max_notional_usd=100.0, borrow_apr=0.05,
                        execution_mode="acquire", persist=lambda s: None,
                        perp_symbol="ETHUSDT", spot_symbol="ETHUSDT")
    other.restore(btc.to_state())
    assert other._funding_history == []
    assert other._last_print_ms is None


def test_discarded_restore_keeps_a_fresher_warm_up():
    e = CarryEngine(broker=Venue(), max_notional_usd=100.0, borrow_apr=0.05,
                    execution_mode="acquire", persist=lambda s: None)
    e.warm_funding_history([(FRESH, k * H8) for k in (7, 8, 9)])
    e.restore({"position": None, "funding_history": [RICH] * 4})  # no stamp
    assert e._funding_history == [FRESH] * 3
    assert e._last_print_ms == 9 * H8


def test_legacy_state_without_a_symbol_is_accepted_only_for_a_proven_symbol():
    legacy = {"position": None, "funding_history": [RICH] * 4,
              "last_print_ms": 4 * H8}
    eth = CarryEngine(broker=Venue(), max_notional_usd=100.0, borrow_apr=0.05,
                      execution_mode="acquire", persist=lambda s: None,
                      perp_symbol="ETHUSDT", spot_symbol="ETHUSDT")
    eth.restore(legacy)
    assert eth._funding_history == []
    btc = CarryEngine(broker=Venue(), max_notional_usd=100.0, borrow_apr=0.05,
                      execution_mode="acquire", persist=lambda s: None)
    btc.restore(legacy)
    assert btc._funding_history == [RICH] * 4


# -- a persisted history that cannot be trusted is not restored ---------------

import math  # noqa: E402

import pytest  # noqa: E402

NAN = float("nan")


def economics(e):
    return e.entry_economics(mark=MARK, spot=MARK, funding_bps=FRESH)


def test_control_clean_rich_history_is_eligible_through_the_entry_gate():
    e = engine([RICH] * 4, 4 * H8)
    assert economics(e).refusal is None          # the fixture CAN approve


@pytest.mark.parametrize("history", [
    [RICH] * 3 + [NAN] * 5,                # three stale prints, five holes
    [RICH] * 3 + [NAN] * 4 + [RICH],
    [NAN, NAN, RICH, RICH, RICH],
    [RICH, float("inf"), RICH, RICH],
    [RICH, "x", RICH, RICH],               # unparseable
    [RICH, None, RICH, RICH],
    [RICH, True, RICH, RICH],
    [RICH, "10", RICH, RICH],              # a string is not a recorded rate
])
def test_untrustworthy_restored_history_cannot_authorise_entry(history):
    e = engine(history, 4 * H8)                  # must not raise either
    assert e._funding_history == []
    refusal = economics(e).refusal
    assert refusal is not None and refusal.reason == INSUFFICIENT


@pytest.mark.parametrize("stamp", [NAN, float("inf"), "x", True, 0, -1])
def test_unreadable_last_print_stamp_discards_the_history(stamp):
    e = engine([RICH] * 4, stamp)
    assert e._funding_history == [] and e._last_print_ms is None
    assert economics(e).refusal.reason == INSUFFICIENT


def test_discarded_history_requires_fresh_contiguous_prints_again():
    e = engine([RICH] * 3 + [NAN] * 5, 4 * H8)
    assert tick(e, 5 * H8).reason == INSUFFICIENT
    assert tick(e, 6 * H8).reason == INSUFFICIENT
    assert e._funding_history == [FRESH, FRESH]
    assert all(math.isfinite(x) for x in e._funding_history)


# -- the tick clock must agree with the history it is asked to trade on -------

D = 24 * H


def clocked(e, now, stamp, bps=FRESH):
    return e.on_candle(mark=MARK, funding_bps=bps, spot=MARK,
                       timestamp_ms=now, funding_print_ms=stamp)


def test_stamp_less_tick_cannot_enter_on_a_history_that_aged_out():
    e = engine([RICH] * 4, 4 * H8)
    d = clocked(e, 4 * H8 + 10 * D, None)           # ten days on, no print
    assert d.reason == INSUFFICIENT
    assert e._funding_history == [] and e._last_print_ms is None


def test_history_inside_the_gap_limit_stays_eligible_without_a_stamp():
    e = engine([RICH] * 4, 4 * H8)
    now = 4 * H8 + CarryEngine.MAX_PRINT_GAP_MS      # exactly 12h: not a gap
    assert economics(e).refusal is None
    assert clocked(e, now, None).reason != INSUFFICIENT
    assert e._funding_history == [RICH] * 4


def test_future_ledger_stamp_cannot_freeze_a_stale_history():
    now = 100 * D
    e = engine([RICH] * 4, now + 30 * D)             # corrupt, far-future stamp
    d = clocked(e, now, now - H)                     # a real, fresh print
    assert d.reason == INSUFFICIENT
    assert e._funding_history == [FRESH]             # the fresh print counted
    assert e._last_print_ms == now - H


def test_fresh_contiguous_history_with_a_live_clock_is_untouched():
    now = 100 * D
    e = engine([RICH] * 4, now - H)
    assert clocked(e, now, now - H).reason != INSUFFICIENT
    assert e._funding_history == [RICH] * 4
