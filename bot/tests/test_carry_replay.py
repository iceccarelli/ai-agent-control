"""0043 — replay the ENGINE, and the clock defect that replaying it exposed.

WHY THIS FILE EXISTS
====================
`tools/carry_backtest.py` does not import `CarryEngine`. It never has. It
shares `carry_costs.evaluate_entry` with the trading program and reimplements
everything else: sizing, both-legs-or-neither, the negative-funding streak, the
margin floor, the unwind, the fee accounting, the day's entry allowance.

So every number this repository has published came from a program that is not
the trading program, under a banner reading `[x] rule_is_what_the_engine_runs`.
That box is true of the GATE. It was never true of the STATE MACHINE, and the
state machine is what decides whether an order is sent.

THE DEFECT IT EXPOSED
=====================
`CarryRisk.gate_open` and `record_entry` both date an entry from
`datetime.now(utc)` — the WALL clock — and `CarryEngine._open` has the tick's
`timestamp_ms` in hand and does not pass it.

Live, those two clocks are the same clock, so this is not a live bug and it is
not reported as one. What it is: a risk limit that can only be observed in
production. Replay 4,500 settlements and they all fall on today, the day's
allowance is spent on the first one and never returns, and the book stands
aside for four years. A daily limit nobody can test against a calendar is a
daily limit nobody can test.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import pytest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import carry_backtest as cb          # noqa: E402
import carry_replay as rp            # noqa: E402
from carry_engine import BookState, CarryEngine  # noqa: E402
from carry_risk import CarryRisk     # noqa: E402

H8 = 8 * 3600 * 1000
#: A day in 2020, so "today" for the wall clock can never be one of these.
START = 1_600_000_000_000


def rows(n=120, rate=0.0006, start=START, price0=30_000.0, cycle=None):
    """`cycle` alternates regimes: `cycle` positive prints, then four negative
    ones — enough to trip NEGATIVE_FUNDING_EXIT_PRINTS and put the book flat
    on a later UTC day, so a re-entry has to come out of a fresh allowance."""
    out = []
    for i in range(n):
        price = price0 * (1.0 + 0.0002 * i)
        r = rate
        if cycle:
            r = rate if (i % (cycle + 4)) < cycle else -rate
        out.append(cb.Settlement(ms=start + i * H8, perp=price,
                                 spot=price * (1 - 0.0004), rate=r,
                                 perp_high=price * 1.01,
                                 perp_low=price * 0.99))
    return out


class TestTheGateIsToldWhatTimeTheMarketThinksItIs:
    def test_the_engine_passes_the_ticks_timestamp_to_the_gate(self):
        """The fix. Without it the gate dates every entry `now`, so a replay,
        a drill reconstruction or any simulation spends the day's allowance
        once and never gets it back."""
        seen = {}

        class Spy(CarryRisk):
            def gate_open(self, *, notional_usd, snapshot, has_open_pair,
                          now=None):
                seen["gate"] = now
                return super().gate_open(notional_usd=notional_usd,
                                         snapshot=snapshot,
                                         has_open_pair=has_open_pair, now=now)

            def record_entry(self, *, now=None):
                seen["record"] = now
                return super().record_entry(now=now)

        data = rows(10)
        report = rp.replay(data, notional=100_000.0, borrow_apr=0.0,
                           risk_class=Spy)
        assert report["trades"] >= 1
        assert seen.get("gate") is not None, \
            "gate_open was called without a market time"
        assert seen["gate"].year == 2020, seen["gate"]
        assert seen.get("record") is not None
        assert seen["record"].date() == seen["gate"].date()

    def test_a_tick_with_no_timestamp_still_uses_the_wall_clock(self):
        """`timestamp_ms` defaults to 0. A book driven without one must behave
        exactly as it did before this change, not date its entries to 1970."""
        engine = CarryEngine(broker=object(), max_notional_usd=100.0,
                             borrow_apr=0.0, execution_mode="overlay",
                             persist=lambda s: None)
        assert engine._gate_now(0) is None
        moment = engine._gate_now(START)
        assert moment.tzinfo is dt.timezone.utc
        assert moment.year == 2020

    def test_the_allowance_resets_on_the_market_calendar(self):
        """Three settlements a day for four days: the engine may open on each
        of the four, not once ever."""
        data = rows(90, cycle=12)
        report = rp.replay(data, notional=100_000.0, borrow_apr=0.0)
        assert report["trades"] > 1, report["reasons"]

    def test_the_limit_still_binds_within_one_market_day(self):
        """The fix must not become a loophole: two entries on the same UTC
        day are still refused."""
        gate = CarryRisk(max_notional_usd=100.0)
        noon = dt.datetime(2020, 9, 13, 12, tzinfo=dt.timezone.utc)
        snapshot = rp.replay_snapshot()
        assert gate.gate_open(notional_usd=100.0, snapshot=snapshot,
                              has_open_pair=False, now=noon)
        gate.record_entry(now=noon)
        later = noon.replace(hour=20)
        blocked = gate.gate_open(notional_usd=100.0, snapshot=snapshot,
                                 has_open_pair=False, now=later)
        assert not blocked
        assert blocked.reason == "ENTRY_LIMIT_REACHED_TODAY"
        tomorrow = noon + dt.timedelta(days=1)
        assert gate.gate_open(notional_usd=100.0, snapshot=snapshot,
                              has_open_pair=False, now=tomorrow)


class TestTheReplayDrivesTheRealThing:
    def test_it_drives_carry_engine_and_not_a_copy(self):
        import inspect
        source = inspect.getsource(rp)
        assert "from carry_engine import" in source
        assert "engine.on_candle(" in source
        # and it must go through the same freshness gate main.tick uses
        assert "assert_fresh()" in source
        assert "take_snapshot(" in source

    def test_a_refused_snapshot_sends_no_order(self):
        """If the view is stale the live bot returns without touching the
        book. The replay must do the same or it measures a bot that trades
        on data the real one refuses."""
        data = rows(30)
        # a bar with a nonsense mark: assert_fresh refuses it
        data[10] = cb.Settlement(ms=data[10].ms, perp=0.0, spot=data[10].spot,
                                 rate=data[10].rate, perp_high=1.0,
                                 perp_low=1.0)
        report = rp.replay(data, notional=100_000.0, borrow_apr=0.0)
        assert report["reasons"].get("SNAPSHOT_REFUSED", 0) >= 1

    def test_the_venue_lot_step_is_the_real_one(self):
        """0036: at the $100 cap `cap / mark` is not a multiple of 0.001, and
        the simulator has no concept of that at all."""
        assert rp.LINEAR_RULES["qty_step"] == 0.001
        # BTC above $100k: the $100 cap buys 0.00090 BTC, which snaps DOWN to
        # zero lots. Not a smaller book — a rejected order.
        report = rp.replay(rows(30, price0=111_000.0), notional=100.0,
                           borrow_apr=0.0)
        assert report["reasons"].get("SIZE_BELOW_VENUE_MINIMUM", 0) >= 1
        assert report["trades"] == 0

    def test_a_halt_stops_the_replay_where_the_process_would_stop(self):
        report = rp.replay(rows(30), notional=100_000.0, borrow_apr=0.0,
                           halt_after=5)
        assert report["halted_at_ms"] is not None
        assert report["settlements_walked"] <= 7

    def test_every_unmodelled_assumption_is_named(self):
        report = rp.replay(rows(30), notional=100_000.0, borrow_apr=0.0)
        joined = " ".join(report["not_modelled"]).lower()
        for term in ("latency", "fill probability", "f4"):
            assert term in joined, term


class TestTheDiffIsHonest:
    def test_it_reports_a_trade_count_disagreement(self):
        engine = {"trades": 1, "net_usd": 100.0, "market_exposure_pct": 0.1,
                  "reasons": {}}
        sim = {"trades": 79, "net_usd": 29_695.0,
               "market_exposure_pct": 91.0}
        problems = rp.diff(engine, sim)
        assert any("TRADES" in p for p in problems)
        assert any("NET" in p for p in problems)

    def test_agreement_produces_no_complaint(self):
        same = {"trades": 5, "net_usd": 100.0, "market_exposure_pct": 50.0,
                "reasons": {}}
        assert rp.diff(same, dict(same)) == []


class TestTheEngineAndTheSimulatorAgree:
    """The regression that could not exist before 0043.

    `carry_backtest` and `CarryEngine` had never been compared. They are now,
    over the whole Bybit settlement corpus, and any future change that makes
    them disagree by more than venue lot snapping fails here.

    The residual is real and is the engine being RIGHT: it snaps every size to
    Bybit's 0.001 step and refuses below the minimum, which the simulator has
    no concept of. That moves funding and fees by about 0.2% and nothing else.
    """

    corpus = pytest.mark.skipif(
        not os.path.exists(os.path.join(
            os.path.dirname(__file__), "..", "data", "real_bybit_btc_4h")),
        reason="the same-venue corpus is not built here")

    @corpus
    def test_they_agree_on_the_trades_and_the_money(self):
        repo = os.path.join(os.path.dirname(__file__), "..")
        data, _meta = cb.load_bybit_settlements(repo)
        impact = cb.impact_bps_per_leg(repo, 100_000.0)[0]
        engine = rp.replay(data, notional=100_000.0, borrow_apr=0.0,
                           impact_bps=impact)
        sim = cb.simulate_settlements_series(
            data, notional=100_000.0, borrow_apr=0.0, gated=True,
            impact_bps=impact, mode="overlay")

        assert engine["trades"] == sim["trades"]
        assert engine["market_exposure_pct"] == pytest.approx(
            sim["market_exposure_pct"], abs=0.05)
        # 1% is far wider than the 0.24% measured, and far tighter than any of
        # the three defects 0043 found (26%, 553%, and a book that stood aside
        # for four years).
        assert engine["net_usd"] == pytest.approx(sim["net_usd"], rel=0.01)

    @corpus
    def test_the_disagreement_that_remains_is_an_exit_that_never_happened(self):
        """Attributed to the cent, so nobody has to wonder about it.

        One trade is still open when the window ends. The simulator marks it
        closed and charges an exit fee; the engine has not sent that order, so
        it has not paid it. The ENGINE is right — a position you are still
        holding has not paid to be closed — and that single unsent fee is the
        whole of the gap.
        """
        repo = os.path.join(os.path.dirname(__file__), "..")
        data, _meta = cb.load_bybit_settlements(repo)
        engine = rp.replay(data, notional=100_000.0, borrow_apr=0.0,
                           impact_bps=0.0)
        sim = cb.simulate_settlements_series(
            data, notional=100_000.0, borrow_apr=0.0, gated=True,
            impact_bps=0.0, mode="overlay")

        # one open at the end, and one fewer close than open
        still_open = [t for t in engine["trade_rows"] if t.get("open_at_end")]
        assert len(still_open) == 1
        assert engine["orders"] == 2 * engine["trades"] - 1

        unsent = (still_open[0]["qty"] * still_open[0]["exit_perp"]
                  * cb.TAKER_BPS_PERP / 1e4)
        fee_gap = engine["fees_usd"] + sim["fees_usd"]
        assert fee_gap == pytest.approx(unsent, rel=0.10)

        # and the net gap is that fee, less the funding the snapped lot did
        # not earn. Nothing else is unexplained.
        residual = (engine["net_usd"] - sim["net_usd"]) - fee_gap
        assert abs(residual) < 0.001 * abs(sim["net_usd"])


class TestSuppliedDataIsRefusedRatherThanGuessedAt:
    def test_a_missing_column_is_named(self, tmp_path):
        path = tmp_path / "bars.csv"
        path.write_text("ms,perp,spot\n1,2,3\n")
        with pytest.raises(rp.ReplayRefusal) as err:
            rp.load_bars(str(path))
        assert "rate" in str(err.value)

    def test_bps_mistaken_for_a_fraction_is_caught(self, tmp_path):
        """The commonest upload error: a funding column already in bps. 1.0
        as a fraction is 100% per 8h print."""
        path = tmp_path / "bars.csv"
        path.write_text("ms,perp,spot,rate,perp_high,perp_low\n"
                        "0,30000,29990,1.0,30100,29900\n")
        with pytest.raises(rp.ReplayRefusal) as err:
            rp.load_bars(str(path))
        assert "bps" in str(err.value)

    def test_duplicate_stamps_are_refused_not_deduplicated(self, tmp_path):
        path = tmp_path / "bars.csv"
        path.write_text("ms,perp,spot,rate,perp_high,perp_low\n"
                        "0,30000,29990,0.0001,30100,29900\n"
                        "0,30001,29991,0.0002,30100,29900\n")
        with pytest.raises(rp.ReplayRefusal):
            rp.load_bars(str(path))

    def test_a_good_file_loads_sorted(self, tmp_path):
        path = tmp_path / "bars.csv"
        path.write_text("ms,perp,spot,rate,perp_high,perp_low\n"
                        "28800000,30001,29991,0.0002,30100,29900\n"
                        "0,30000,29990,0.0001,30100,29900\n")
        loaded = rp.load_bars(str(path))
        assert [r.ms for r in loaded] == [0, 28800000]
        assert loaded[0].rate == pytest.approx(0.0001)
