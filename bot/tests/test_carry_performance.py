"""Net performance of a carry book, read only from what the ledger booked.

Per cycle (flat -> opened -> flat) and in total: confirmed versus provisional
net, funding, fees, financing, the price leg, return on the notional committed,
drawdown, concentration and holding time. Nothing is estimated: a cycle whose
fees or funding are not venue-confirmed is reported as provisional and kept out
of the confirmed total, a quantity the ledger never recorded (margin) is None
with the reason, and a book with no closed cycle gets no ratios at all.

Figures are a hand-computed FIXTURE, not an observation.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import ledger as L                      # noqa: E402
import carry_performance as perf        # noqa: E402

DAY = 86_400_000
T0 = 1_700_000_000_000


def cycle(j, day_open, day_close, *, funding, close_price=100_000.0, tag,
          fee_status="confirmed", basis="venue", qty=1.0):
    L.perp_open(j, ms=T0 + day_open * DAY, ref=f"{tag}-o", qty=qty,
                price=100_000.0, fee_usd=55.0, fee_status=fee_status)
    mid = (day_open + day_close) // 2
    L.funding(j, ms=T0 + mid * DAY, ref=f"{tag}-f", usd=funding, basis=basis)
    L.perp_close(j, ms=T0 + day_close * DAY, ref=f"{tag}-c", qty=qty,
                 price=close_price, entry_price=100_000.0, fee_usd=55.0,
                 fee_status=fee_status)


@pytest.fixture()
def journal():
    j = L.Journal()
    cycle(j, 0, 10, funding=300.0, tag="a")                       # +190
    cycle(j, 12, 14, funding=-20.0, close_price=101_000.0, tag="b")  # -1130
    cycle(j, 20, 40, funding=600.0, tag="c")                      # +490
    return j


def test_each_cycle_is_booked_from_the_ledger(journal):
    cycles = perf.cycles_from_journal(journal)
    assert [c["closed"] for c in cycles] == [True, True, True]
    assert [round(c["net_usd"], 2) for c in cycles] == [190.0, -1130.0, 490.0]
    assert [round(c["hold_days"], 2) for c in cycles] == [10.0, 2.0, 20.0]
    assert cycles[1]["realised_usd"] == pytest.approx(-1000.0)
    assert cycles[0]["fees_usd"] == pytest.approx(110.0)
    assert cycles[0]["funding_usd"] == pytest.approx(300.0)
    assert all(c["basis"] == "confirmed" for c in cycles)
    assert cycles[0]["notional_usd"] == pytest.approx(100_000.0)


def test_the_cycle_nets_add_up_to_the_ledger_statement(journal):
    cycles = perf.cycles_from_journal(journal)
    assert sum(c["net_usd"] for c in cycles) == pytest.approx(
        journal.statement()["net_usd"])


def test_the_summary_figures(journal):
    s = perf.summarise(perf.cycles_from_journal(journal))
    assert s["closed_cycles"] == 3 and s["open_cycles"] == 0
    assert s["net_usd_confirmed"] == pytest.approx(-450.0)
    assert s["net_usd_provisional"] == 0.0
    assert s["funding_usd"] == pytest.approx(880.0)
    assert s["fees_usd"] == pytest.approx(330.0)
    assert s["price_leg_usd"] == pytest.approx(-1000.0)
    assert s["gross_before_costs_usd"] == pytest.approx(-120.0)   # funding + price
    assert s["peak_notional_usd"] == pytest.approx(100_000.0)
    assert s["return_on_peak_notional_pct"] == pytest.approx(-0.45)
    assert s["days_in_market"] == pytest.approx(32.0)
    assert s["annualised_on_notional_in_market_pct"] == pytest.approx(
        -450.0 / 100_000.0 * 100 * 365 / 32, rel=1e-6)
    assert s["max_drawdown_usd"] == pytest.approx(1130.0)
    assert s["win_rate"] == pytest.approx(2 / 3)
    assert s["best_cycle_share_of_winning_net_pct"] == pytest.approx(
        100 * 490 / 680, rel=1e-6)
    assert s["mean_hold_days"] == pytest.approx(32 / 3)
    assert s["margin_committed_usd"] is None
    assert "not recorded" in s["margin_note"]


def test_an_unconfirmed_cycle_is_provisional_and_kept_out_of_confirmed_net():
    j = L.Journal()
    cycle(j, 0, 10, funding=300.0, tag="a")
    cycle(j, 12, 20, funding=100.0, tag="b", fee_status="unknown")
    cycle(j, 22, 30, funding=100.0, tag="c", basis="engine_estimate")
    cycles = perf.cycles_from_journal(j)
    assert [c["basis"] for c in cycles] == [
        "confirmed", "provisional_fees_incomplete",
        "provisional_funding_unverified"]
    s = perf.summarise(cycles)
    assert s["net_usd_confirmed"] == pytest.approx(190.0)
    assert s["net_usd_provisional"] == pytest.approx(
        (100.0 - 110.0) + (100.0 - 110.0)) and s["provisional_cycles"] == 2


def test_an_open_cycle_is_reported_open_and_not_counted_as_a_result():
    j = L.Journal()
    cycle(j, 0, 10, funding=300.0, tag="a")
    L.perp_open(j, ms=T0 + 12 * DAY, ref="b-o", qty=1.0, price=100_000.0,
                fee_usd=55.0, fee_status="confirmed")
    s = perf.summarise(perf.cycles_from_journal(j))
    assert s["closed_cycles"] == 1 and s["open_cycles"] == 1
    assert s["net_usd_confirmed"] == pytest.approx(190.0)   # open one excluded


def test_no_closed_cycle_means_no_ratios():
    s = perf.summarise(perf.cycles_from_journal(L.Journal()))
    assert s["status"] == "NO_CLOSED_CYCLES"
    for key in ("return_on_peak_notional_pct", "win_rate", "max_drawdown_usd",
                "annualised_on_notional_in_market_pct"):
        assert s.get(key) is None


def test_a_cycle_shorter_than_a_day_gets_no_annualised_figure():
    j = L.Journal()
    L.perp_open(j, ms=T0, ref="o", qty=1.0, price=100_000.0, fee_usd=55.0,
                fee_status="confirmed")
    L.perp_close(j, ms=T0 + 3_600_000, ref="c", qty=1.0, price=100_000.0,
                 entry_price=100_000.0, fee_usd=55.0, fee_status="confirmed")
    s = perf.summarise(perf.cycles_from_journal(j))
    assert s["annualised_on_notional_in_market_pct"] is None
    assert s["net_usd_confirmed"] == pytest.approx(-110.0)


def test_a_notional_the_ledger_cannot_state_is_none_not_guessed():
    j = L.Journal()
    j.post(L.Entry(ms=T0, kind="perp_open", ref="o",
                   lines=[L.Line(L.PERP, 0.0, qty=-1.0),
                          L.Line(L.FEES, 55.0), L.Line(L.USDT, -55.0, qty=-55.0)],
                   memo="", fee_status="confirmed"))
    L.perp_close(j, ms=T0 + DAY, ref="c", qty=1.0, price=100_000.0,
                 entry_price=100_000.0, fee_usd=55.0, fee_status="confirmed")
    c = perf.cycles_from_journal(j)[0]
    assert c["notional_usd"] is None
    s = perf.summarise([c])
    assert s["return_on_peak_notional_pct"] is None


def test_the_cli_reads_a_state_database_read_only(tmp_path, journal, capsys):
    from persistence import StateStore
    db = str(tmp_path / "s.db")
    store = StateStore(db)
    store.save_ledger(journal.to_rows())
    store.close()
    before = os.path.getmtime(db)
    assert perf.main(["--db", db]) == 0
    out = capsys.readouterr().out
    assert "confirmed net" in out and "-450.00" in out
    assert "margin" in out and "not recorded" in out
    assert os.path.getmtime(db) == before
    con = sqlite3.connect(db)
    con.close()


def test_the_cli_refuses_a_missing_database(tmp_path):
    assert perf.main(["--db", str(tmp_path / "nope.db")]) == 2
