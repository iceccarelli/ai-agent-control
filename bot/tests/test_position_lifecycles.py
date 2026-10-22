"""A position is the unit of performance, not an exit fill."""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from persistence import StateStore, TradeRecord  # noqa: E402


@pytest.fixture
def store(tmp_path):
    s = StateStore(str(tmp_path / "s.db"))
    s.claim_writer()
    s.update_equity(10_000.0)
    return s


def _exit(store, *, opened=1000.0, closed=2000.0, qty=0.01, gross=10.0,
          entry=60_000.0, exit_=59_000.0, symbol="BTCUSDT", provisional=False,
          fee=0.1):
    return store.record_trade(TradeRecord(
        symbol=symbol, side="Sell", qty=qty, entry_price=entry,
        exit_price=0.0 if provisional else exit_,
        price_provisional=provisional,
        gross_pnl=0.0 if provisional else gross, entry_fee=fee, exit_fee=fee,
        opened_epoch=opened, closed_epoch=closed))


def test_a_position_that_left_in_three_pieces_is_one_sample(store):
    # +4, +4 then a stop-out that loses 15: the position LOST 7 net-ish, but as
    # three rows it read as two wins and a loss
    _exit(store, opened=1000.0, closed=2000.0, qty=0.004, gross=4.0)
    _exit(store, opened=1000.0, closed=2001.0, qty=0.004, gross=4.0)
    _exit(store, opened=1000.0, closed=2002.0, qty=0.002, gross=-15.0)
    rets = store.net_returns()
    assert len(rets) == 1
    assert rets[0] < 0
    rows = store.recent_trades()
    assert len(rows) == 1 and rows[0]["exits"] == 3
    assert rows[0]["qty"] == pytest.approx(0.01)
    assert rows[0]["net_pnl"] == pytest.approx(4 + 4 - 15 - 6 * 0.1)


def test_a_position_still_open_is_not_a_result_yet(store):
    _exit(store, opened=1000.0, closed=2000.0, qty=0.004, gross=4.0)
    store._exec("INSERT INTO positions(symbol, side, qty, entry_price, stop_price,"
                " opened_epoch) VALUES('BTCUSDT','Sell',0.006,60000,61000,1000.0)")
    assert store.net_returns() == [] and store.recent_trades() == []
    store._exec("DELETE FROM positions")
    assert len(store.net_returns()) == 1


def test_one_unpriced_exit_keeps_the_whole_position_out_of_the_statistics(store):
    _exit(store, opened=1000.0, closed=2000.0, qty=0.004, gross=4.0)
    _exit(store, opened=1000.0, closed=2001.0, qty=0.006, provisional=True)
    assert store.net_returns() == [] and store.trades_for_symbol("BTCUSDT") == []


def test_separate_positions_stay_separate_samples_and_symbols_filter(store):
    _exit(store, opened=1000.0, closed=2000.0)
    _exit(store, opened=3000.0, closed=4000.0, gross=-5.0)
    _exit(store, opened=1000.0, closed=2000.0, symbol="ETHUSDT")
    assert len(store.net_returns()) == 3
    assert len(store.trades_for_symbol("BTCUSDT")) == 2
    assert [r["closed_epoch"] for r in store.recent_trades()][:2] == [4000.0, 2000.0]
    newest_first = [r["closed_epoch"] for r in store.trades_for_symbol("BTCUSDT")]
    assert newest_first == sorted(newest_first, reverse=True)


def test_the_limit_counts_positions_not_fills(store):
    for i in range(5):
        _exit(store, opened=1000.0 + i, closed=2000.0 + i, qty=0.005)
        _exit(store, opened=1000.0 + i, closed=2000.5 + i, qty=0.005)
    assert len(store.net_returns(limit=3)) == 3
    assert all(r["exits"] == 2 for r in store.recent_trades(3))


def test_funding_booked_on_an_exit_is_part_of_the_positions_return(store):
    a = store.record_trade(TradeRecord(
        symbol="BTCUSDT", side="Sell", qty=0.005, entry_price=60_000.0,
        exit_price=59_000.0, gross_pnl=5.0, entry_fee=0.1, exit_fee=0.1,
        opened_epoch=1000.0, closed_epoch=2000.0,
        meta={"funding_booked": False}))
    _exit(store, opened=1000.0, closed=2001.0, qty=0.005, gross=5.0)
    base = store.net_returns()[0]
    assert store.book_trade_funding(a, funding_usd=-3.0, payment_ids=["x"],
                                    status="BOOKED_FROM_SETTLEMENTS", evidence={})
    after = store.net_returns()
    assert len(after) == 1
    assert after[0] == pytest.approx(base - 3.0 / (0.01 * 60_000.0))


def test_regime_label_is_read_once_per_position(store):
    _exit(store, opened=1000.0, closed=2000.0, qty=0.005)
    _exit(store, opened=1000.0, closed=2001.0, qty=0.005)
    assert len(store.trades_with_regime()) == 1


def test_a_multi_day_position_is_attributed_to_the_days_of_its_exits(store):
    from performance_analytics import PerformanceAnalytics
    day1, day2 = 1_700_000_000.0, 1_700_000_000.0 + 86_400.0 * 2
    store.record_trade(TradeRecord(
        symbol="BTCUSDT", side="Sell", qty=0.005, entry_price=60_000.0,
        exit_price=59_000.0, gross_pnl=5.0, entry_fee=0.0, exit_fee=0.0,
        opened_epoch=day1 - 100, closed_epoch=day1))
    store.record_trade(TradeRecord(
        symbol="BTCUSDT", side="Sell", qty=0.005, entry_price=60_000.0,
        exit_price=61_000.0, gross_pnl=-5.0, entry_fee=0.0, exit_fee=0.0,
        opened_epoch=day1 - 100, closed_epoch=day2))
    lc = store.recent_trades()[0]
    assert lc["spans_days"] is True and len(lc["pieces"]) == 2
    by_day = PerformanceAnalytics._daily_returns([lc])
    assert sorted(by_day.values()) == [-5.0, 5.0]        # not one 0.0 day
    rep = PerformanceAnalytics(store).report()
    assert any("booking days, not earning days" in n for n in rep.notes)


def test_a_position_that_stays_open_after_an_exit_is_named_not_forgotten(store):
    _exit(store, opened=1000.0, closed=2000.0, qty=0.004, gross=4.0)
    store._exec("INSERT INTO positions(symbol, side, qty, entry_price, stop_price,"
                " opened_epoch) VALUES('BTCUSDT','Sell',0.006,60000,61000,1000.0)")
    [row] = store.partial_open_lifecycles(now=2000.0 + 3600)
    assert row["symbol"] == "BTCUSDT" and row["exits"] == 1
    assert row["since_last_exit_s"] == pytest.approx(3600.0)
    from performance_analytics import PerformanceAnalytics
    assert any("still open after booking an exit" in n
               for n in PerformanceAnalytics(store).report().notes)
