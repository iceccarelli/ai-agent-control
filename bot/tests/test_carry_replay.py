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
import carry_hold_audit as ha         # noqa: E402
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
            impact_bps=impact, mode="overlay",
            latency_rows=0)   # the replay engine fills on the decision row

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
            impact_bps=0.0, mode="overlay",
            latency_rows=0)

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


class TestAcquireFeeNetAccounting:
    def test_acquire_replay_keeps_gross_spot_entry_separate_from_net_inventory(self):
        report = rp.replay(rows(90, cycle=12), notional=100_000.0,
                           borrow_apr=0.0, execution_mode="acquire")
        trades = report["trade_rows"]
        assert trades, report["reasons"]
        for trade in trades:
            # Replay must use the coin the wallet actually holds for basis,
            # while preserving the gross fill for the entry-fee calculation.
            assert trade["spot_entry_qty"] > trade["spot_qty"] > 0.0
            # The perp must never hedge more BTC than the post-fee spot holds.
            assert trade["qty"] <= trade["spot_qty"] + 1e-9

    def test_acquire_hold_audit_prices_net_spot_pnl_but_gross_entry_fee(self):
        trade = {
            "qty": 1.0,
            "spot_qty": 0.999,
            "spot_entry_qty": 1.0,
            "entry_perp": 100_000.0,
            "exit_perp": 101_000.0,
            "entry_spot": 100_000.0,
            "exit_spot": 101_000.0,
            "funding": 20.0,
            "open_at_end": False,
        }
        result = ha._trade_net(trade, taker_perp_bps=5.5,
                               taker_spot_bps=10.0, mode="acquire")
        # -$1 is the basis P&L on 0.999 BTC net held versus a 1 BTC short.
        # Entry fee uses the gross buy; exit fee uses only the net BTC sold.
        assert result["basis"] == pytest.approx(-1.0)
        assert result["fees"] == pytest.approx(311.449)
        assert result["net"] == pytest.approx(-292.449)


class TestTheReplayDoesNotPresentAnUnchargedSpreadAsATotal:
    """Economic-cost audit (hand-computed): flat 100000 corpus, overlay, 100k notional, one trade, impact 0: the broker declares a touch of
    99999.935/100000.065 yet both market legs fill at exactly 100000.0, so basis is 0.0 (a charged half-spread would be -0.13) and fees are 110.0.
    The dollars are tiny; the headline must still say what it leaves out."""

    def test_zero_impact_is_flagged_and_the_unmodelled_costs_are_named(self):
        report = rp.replay(rows(30), notional=100_000.0, borrow_apr=0.0)
        assert report["impact_bps_per_leg"] == 0.0
        assert "NO SPREAD, SLIPPAGE OR IMPACT IS CHARGED" in report["execution_cost_warning"]
        joined = " ".join(report["not_modelled"]).lower()
        for term in ("half-spread and slippage", "market impact beyond the declared haircut", "omits the costs named here", "four legs"):
            assert term in joined, term
        assert "upper bound" not in joined and "upper bound" not in report["execution_cost_warning"].lower()

    def test_a_declared_impact_removes_the_zero_charge_warning(self):
        report = rp.replay(rows(30), notional=100_000.0, borrow_apr=0.0, impact_bps=1.0)
        assert report["impact_bps_per_leg"] == 1.0 and report["execution_cost_warning"] is None
        assert "not a total" in " ".join(report["not_modelled"]).lower()          # a declared haircut is still not a measured cost


# ---------------------------------------------------------------------------
# Round 16: the owner-capital view. The headline return divides by the fixed
# ENTRY notional; the owner's alternative is judged on the capital the pair
# actually commits (economic_model.hedged_pair_capital_usd). The view says what
# the net is on that capital, that this IS the break-even cost of capital, and
# never reads an unstated cost of capital as a stated zero.
# ---------------------------------------------------------------------------

class TestTheOwnerCapitalView:
    D = {"avg_marked_hedged_notional_usd": 100_000.0}

    def test_the_net_is_judged_on_the_capital_the_repo_rule_commits(self):
        v = rp.owner_capital_view(self.D, net_usd=10_000.0, years=2.0)
        assert v["available"] is True
        assert v["capital_multiple_of_marked_notional"] == pytest.approx(2.04)
        assert v["avg_committed_capital_usd"] == pytest.approx(204_000.0)
        assert v["net_return_on_committed_capital_pct_per_year"] == pytest.approx(
            100.0 * 10_000.0 / 204_000.0 / 2.0)

    def test_that_return_is_the_break_even_cost_of_capital(self):
        v = rp.owner_capital_view(self.D, net_usd=10_000.0, years=2.0)
        assert v["break_even_cost_of_capital_pct"] == pytest.approx(
            v["net_return_on_committed_capital_pct_per_year"])

    def test_leverage_changes_the_committed_capital_not_a_constant(self):
        v2 = rp.owner_capital_view(self.D, net_usd=10_000.0, years=2.0,
                                   leverage=2.0)
        assert v2["capital_multiple_of_marked_notional"] == pytest.approx(
            (1 + 1 / 2.0) * 1.02)
        v1 = rp.owner_capital_view(self.D, net_usd=10_000.0, years=2.0)
        assert (v2["net_return_on_committed_capital_pct_per_year"]
                > v1["net_return_on_committed_capital_pct_per_year"])

    def test_an_unstated_cost_of_capital_is_unstated_not_zero(self):
        v = rp.owner_capital_view(self.D, net_usd=10_000.0, years=2.0)
        assert v["cost_of_capital"] == {"state": "UNSTATED", "apr": None,
                                        "source": None}
        assert "excess_over_stated_cost_pct_per_year" not in v
        text = "\n".join(rp.owner_capital_lines(v))
        assert "COST OF CAPITAL UNSTATED" in text
        assert "NOT assumed" in text

    def test_a_stated_cost_gives_the_excess_on_the_same_capital(self):
        v = rp.owner_capital_view(self.D, net_usd=10_000.0, years=2.0,
                                  stated_apr=0.04)
        assert v["cost_of_capital"]["state"] == "STATED"
        assert v["excess_over_stated_cost_pct_per_year"] == pytest.approx(
            v["net_return_on_committed_capital_pct_per_year"] - 4.0)
        assert "UNSTATED" not in "\n".join(rp.owner_capital_lines(v))

    def test_an_explicit_zero_is_a_stated_zero_and_an_absent_value_is_not(self):
        zero = rp.owner_capital_view(self.D, net_usd=1.0, years=1.0,
                                     stated_apr=0.0)
        assert zero["cost_of_capital"]["state"] == "STATED"
        none = rp.owner_capital_view(self.D, net_usd=1.0, years=1.0)
        assert none["cost_of_capital"]["state"] == "UNSTATED"

    @pytest.mark.parametrize("d", [{}, {"avg_marked_hedged_notional_usd": None},
                                   {"avg_marked_hedged_notional_usd": 0.0},
                                   {"avg_marked_hedged_notional_usd": float("nan")}])
    def test_an_unreproducible_notional_claims_nothing(self, d):
        v = rp.owner_capital_view(d, net_usd=10_000.0, years=2.0)
        assert v["available"] is False and v["why"]
        assert "not claimed" in "\n".join(rp.owner_capital_lines(v))

    def test_a_real_replay_carries_the_view_and_says_it_is_unstated(self):
        r = rp.replay(rows(n=240), notional=100_000.0, borrow_apr=0.0,
                      execution_mode="overlay")
        v = r["owner_capital"]
        assert v["cost_of_capital"]["state"] == "UNSTATED"
        if v["available"]:
            assert v["capital_multiple_of_marked_notional"] == pytest.approx(2.04)
        stated = rp.replay(rows(n=240), notional=100_000.0, borrow_apr=0.0,
                           execution_mode="overlay", opportunity_cost_apr=0.03,
                           cost_of_capital_stated=True)
        assert stated["owner_capital"]["cost_of_capital"]["state"] == "STATED"


# ---------------------------------------------------------------------------
# Round 17: the overlay's hedge loses CASH when BTC rises, and the journal holds
# no USDT to pay it from. A4/A1 reproduced on the 4.1-year corpus that the
# journal's USDT account was negative 81% of the time (average -$86,887, low
# -$180,747), so the interest on it is 55-88% of the $33,050 net at 5-8%. The
# replay MEASURES the deficit always and charges interest ONLY on a rate the
# operator states (never a default), without moving the journal or net_usd.
# ---------------------------------------------------------------------------

DAY_MS = 86_400_000


def _journal_with_deficit(*moves, notional=100_000.0):
    """A journal whose USDT account takes `moves` = [(ms, usd), ...]: each a
    balanced posting against REALISED (a perp_close's cash leg)."""
    import ledger as L
    j = L.Journal()
    for n, (ms, usd) in enumerate(moves):
        j.post(L.Entry(ms=ms, kind="adjustment", ref=f"m{n}",
                       lines=[L.Line(L.USDT, usd, qty=usd),
                              L.Line(L.REALISED, -usd)]))
    return j


YEAR_MS = int(365.25 * DAY_MS)


class TestTheUsdtFinancingProfile:
    def test_the_deficit_is_the_time_integral_of_the_negative_balance(self):
        t0 = START
        # -100k for the first half-year, then back to 0 for the second
        j = _journal_with_deficit((t0, -100_000.0), (t0 + YEAR_MS // 2, +100_000.0))
        f = rp.usdt_financing_profile(j, start_ms=t0, end_ms=t0 + YEAR_MS,
                                      notional=100_000.0)
        assert f["available"] is True
        assert f["deficit_usd_years"] == pytest.approx(50_000.0)
        assert f["avg_deficit_usd"] == pytest.approx(50_000.0)
        assert f["max_deficit_usd"] == pytest.approx(100_000.0)
        assert f["share_of_time_in_deficit"] == pytest.approx(0.5)
        assert f["state"] == "UNSTATED" and f["interest_usd"] is None

    def test_interest_is_charged_only_on_a_stated_rate_and_only_on_the_deficit(self):
        t0 = START
        j = _journal_with_deficit((t0, -100_000.0), (t0 + YEAR_MS // 2, +100_000.0))
        f = rp.usdt_financing_profile(j, start_ms=t0, end_ms=t0 + YEAR_MS,
                                      notional=100_000.0, usdt_borrow_apr=0.05)
        assert f["state"] == "STATED"
        assert f["interest_usd"] == pytest.approx(0.05 * 50_000.0)
        zero = rp.usdt_financing_profile(j, start_ms=t0, end_ms=t0 + YEAR_MS,
                                         notional=100_000.0, usdt_borrow_apr=0.0)
        assert zero["state"] == "STATED" and zero["interest_usd"] == 0.0

    def test_the_rules_margin_cash_is_posted_first(self):
        """The capital rule (hedged_pair_capital_usd) commits perp margin cash of
        notional/leverage x 1.02 = $102,000 here; only the deficit BEYOND it is
        borrowed on that view."""
        t0 = START
        j = _journal_with_deficit((t0, -150_000.0), (t0 + YEAR_MS // 2, +150_000.0))
        f = rp.usdt_financing_profile(j, start_ms=t0, end_ms=t0 + YEAR_MS,
                                      notional=100_000.0, usdt_borrow_apr=0.05)
        assert f["margin_cash_assumed_usd"] == pytest.approx(102_000.0)
        assert f["deficit_beyond_margin_cash"]["usd_years"] == pytest.approx(
            (150_000.0 - 102_000.0) * 0.5)
        assert f["interest_after_margin_cash_usd"] == pytest.approx(
            0.05 * (150_000.0 - 102_000.0) * 0.5)
        assert f["interest_usd"] == pytest.approx(0.05 * 150_000.0 * 0.5)
        # a deficit inside the margin cash needs no loan on that view
        j2 = _journal_with_deficit((t0, -90_000.0), (t0 + YEAR_MS // 2, +90_000.0))
        f2 = rp.usdt_financing_profile(j2, start_ms=t0, end_ms=t0 + YEAR_MS,
                                       notional=100_000.0, usdt_borrow_apr=0.05)
        assert f2["interest_after_margin_cash_usd"] == 0.0
        assert f2["interest_usd"] == pytest.approx(0.05 * 90_000.0 * 0.5)

    def test_a_book_that_never_goes_negative_has_a_measured_zero_deficit(self):
        t0 = START
        j = _journal_with_deficit((t0, +5_000.0))
        f = rp.usdt_financing_profile(j, start_ms=t0, end_ms=t0 + YEAR_MS,
                                      notional=100_000.0, usdt_borrow_apr=0.05)
        assert f["max_deficit_usd"] == 0.0 and f["avg_deficit_usd"] == 0.0
        assert f["share_of_time_in_deficit"] == 0.0 and f["max_deficit_at"] is None
        assert f["interest_usd"] == 0.0       # MEASURED zero, not an unknown

    def test_a_balance_that_dips_and_recovers_at_one_timestamp_is_not_a_deficit_that_lasted(self):
        """Red team round 2: the postings of one millisecond are applied together (their order is arbitrary), so the transient between a close's realised loss and the
        same-ms funding credit is not the maximum; a dip that lasts even 1 ms is."""
        t0 = START
        j = _journal_with_deficit((t0 + 1_000, -80_000.0), (t0 + 1_000, +80_000.0))
        f = rp.usdt_financing_profile(j, start_ms=t0, end_ms=t0 + YEAR_MS,
                                      notional=100_000.0)
        assert f["max_deficit_usd"] == 0.0 and f["deficit_usd_years"] == 0.0 and f["max_deficit_at"] is None
        j2 = _journal_with_deficit((t0 + 1_000, -80_000.0), (t0 + 1_001, +80_000.0))
        f2 = rp.usdt_financing_profile(j2, start_ms=t0, end_ms=t0 + YEAR_MS,
                                       notional=100_000.0)
        assert f2["max_deficit_usd"] == pytest.approx(80_000.0) and f2["deficit_usd_years"] > 0.0

    def test_the_share_counts_only_material_deficits_and_the_integral_counts_all(self):
        t0 = START
        # a $500 deficit for the whole year (below 1% of $100k) is priced, but is not "negative for 100% of the window"
        j = _journal_with_deficit((t0, -500.0))
        f = rp.usdt_financing_profile(j, start_ms=t0, end_ms=t0 + YEAR_MS,
                                      notional=100_000.0, usdt_borrow_apr=0.05)
        assert f["share_of_time_in_deficit"] == 0.0 and f["share_of_time_in_any_deficit"] == pytest.approx(1.0)
        assert f["deficit_usd_years"] == pytest.approx(500.0) and f["interest_usd"] == pytest.approx(25.0)
        assert f["deficit_floor_usd"] == pytest.approx(1_000.0)

    def test_a_posting_that_is_not_a_finite_number_gives_no_profile_not_a_silent_zero(self):
        import ledger as L
        j = L.Journal()
        e = L.Entry(ms=START, kind="adjustment", ref="x", lines=[L.Line(L.USDT, -1.0, qty=-1.0), L.Line(L.REALISED, 1.0)])
        j.post(e)
        e.lines[0].usd = float("nan")                                    # a corrupted row that slipped past posting
        f = rp.usdt_financing_profile(j, start_ms=START, end_ms=START + YEAR_MS, notional=100_000.0)
        assert f["available"] is False and "finite" in f["why"]

    @pytest.mark.parametrize("bad", [-0.01, float("nan"), float("inf"), True, "5%"])
    def test_a_rate_that_is_not_a_finite_non_negative_number_is_refused(self, bad):
        j = _journal_with_deficit((START, -1.0))
        with pytest.raises(ValueError):
            rp.usdt_financing_profile(j, start_ms=START, end_ms=START + YEAR_MS,
                                      notional=100_000.0, usdt_borrow_apr=bad)

    def test_an_empty_window_claims_nothing(self):
        j = _journal_with_deficit((START, -1.0))
        f = rp.usdt_financing_profile(j, start_ms=START, end_ms=START,
                                      notional=100_000.0)
        assert f["available"] is False and f["why"]


def _rally_rows(n=140):
    """BTC rallies 0.6% a print with a small positive funding rate: the engine
    enters, the book later exits on negative funding at a higher price, and the
    short's realised loss leaves the journal's USDT account in deficit."""
    out, price = [], 30_000.0
    for i in range(n):
        price *= 1.006
        rate = 0.0001 if i < 100 else -0.0003      # four negative prints force the exit
        out.append(cb.Settlement(ms=START + i * H8, perp=price,
                                 spot=price * (1 - 0.0004), rate=rate,
                                 perp_high=price * 1.003, perp_low=price * 0.997))
    return out


class TestTheReplayStatesItsFinancing:
    def test_a_rally_leaves_a_measured_deficit_and_the_net_says_it_is_an_upper_bound(self):
        r = rp.replay(_rally_rows(), notional=100_000.0, borrow_apr=0.0,
                      execution_mode="overlay")
        f = r["usdt_financing"]
        assert r["trades"] >= 1
        assert f["available"] and f["state"] == "UNSTATED" and f["interest_usd"] is None
        assert f["max_deficit_usd"] > 0 and f["avg_deficit_usd"] > 0
        assert 0 < f["share_of_time_in_deficit"] <= 1
        assert r["owner_capital"]["break_even_status"] == "UPPER_BOUND_USDT_FINANCING_UNSTATED"
        joined = " ".join(r["not_modelled"]).lower()
        assert "negative usdt" in joined and "higher than the net after" in joined
        assert r["net_after_usdt_financing_usd"] is None

    def test_the_final_deficit_is_the_journals_net_realised(self):
        """The journal opens at 0 USDT, so after the last close its USDT balance IS
        its net realised cash: the profile cannot disagree with the statement."""
        r = rp.replay(_rally_rows(), notional=100_000.0, borrow_apr=0.0,
                      execution_mode="overlay")
        net_cash = r["ledger"]["net_usd"]
        assert net_cash < 0, "the fixture must leave the short's loss in the account"
        assert r["usdt_financing"]["max_deficit_usd"] >= -net_cash - 1e-6

    def test_a_stated_rate_charges_the_measured_deficit_and_moves_nothing_else(self):
        base = rp.replay(_rally_rows(), notional=100_000.0, borrow_apr=0.0,
                         execution_mode="overlay")
        stated = rp.replay(_rally_rows(), notional=100_000.0, borrow_apr=0.0,
                           execution_mode="overlay", usdt_borrow_apr=0.05)
        f = stated["usdt_financing"]
        assert f["state"] == "STATED"
        assert f["interest_usd"] == pytest.approx(0.05 * f["deficit_usd_years"])
        assert stated["net_after_usdt_financing_usd"] == pytest.approx(
            stated["net_usd"] - f["interest_usd"])
        # the journal and the headline net are what they were: financing is a
        # stated overlay on the report, never a posting
        for k in ("net_usd", "funding_usd", "basis_usd", "fees_usd", "trades",
                  "ledger_net_usd", "ledger_entries", "ledger_agrees"):
            assert stated[k] == base[k], k
        assert stated["owner_capital"]["break_even_status"] == "AFTER_STATED_USDT_FINANCING_WHOLE_DEFICIT"
        assert "negative usdt" not in " ".join(stated["not_modelled"]).lower() \
            or "stated" in " ".join(stated["not_modelled"]).lower()
        # linear in the rate: it is the measured balance times the rate
        eight = rp.replay(_rally_rows(), notional=100_000.0, borrow_apr=0.0,
                          execution_mode="overlay", usdt_borrow_apr=0.08)
        assert eight["usdt_financing"]["interest_usd"] == pytest.approx(
            1.6 * f["interest_usd"])

    def test_the_return_on_committed_capital_is_after_the_interest_on_the_whole_deficit(self):
        """Red team round 2: the margin cash is still posted as the NEXT short's margin, so the break-even is taken after the interest on the WHOLE deficit (the same base as
        `net_after_usdt_financing_usd`); netting the margin cash first is shown beside it as an optimistic bound, never as the break-even."""
        stated = rp.replay(_rally_rows(), notional=100_000.0, borrow_apr=0.0,
                           execution_mode="overlay", usdt_borrow_apr=0.08)
        v, f = stated["owner_capital"], stated["usdt_financing"]
        if v.get("available"):
            committed, years = v["avg_committed_capital_usd"], stated["years"]
            expect = 100.0 * (stated["net_usd"] - f["interest_usd"]) / committed / years
            assert v["net_return_after_usdt_financing_pct_per_year"] == pytest.approx(expect)
            assert v["break_even_cost_of_capital_pct"] == pytest.approx(expect)
            opt = v["net_return_after_usdt_financing_margin_cash_netted_optimistic_pct_per_year"]
            assert opt >= v["net_return_after_usdt_financing_pct_per_year"]
            assert v["net_return_after_usdt_financing_pct_per_year"] <= v["net_return_before_usdt_financing_pct_per_year"]

    def test_no_default_rate_is_ever_applied(self):
        r = rp.replay(_rally_rows(), notional=100_000.0, borrow_apr=0.0,
                      execution_mode="overlay")
        assert r["usdt_financing"]["usdt_borrow_apr"] is None
        assert r["net_after_usdt_financing_usd"] is None


class TestAcquireHasNoUsdtFinancingProfile:
    def test_in_acquire_the_spot_purchase_is_committed_capital_not_a_deficit(self):
        for apr in (None, 0.05):
            r = rp.replay(_rally_rows(), notional=100_000.0, borrow_apr=0.0,
                          execution_mode="acquire", usdt_borrow_apr=apr)
            f = r["usdt_financing"]
            assert f["available"] is False and "ACQUIRE" in f["why"]
            assert f["interest_usd"] is None and r["net_after_usdt_financing_usd"] is None
            assert not any("negative usdt" in x.lower() for x in r["not_modelled"])
            assert "break_even_status" not in r["owner_capital"]
            text = "\n".join(rp.usdt_financing_lines(f, net_usd=r["net_usd"]))
            assert "not measured" in text
