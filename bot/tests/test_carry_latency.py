"""Signal is not fill: the settlement-clock simulator separates the decision
from the order reaching the book, in Python and in the C++ core alike.

Rows are synthetic and tiny so every dollar can be derived by hand. The real
corpus is used only for the Python/C++ differential."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import carry_backtest as cb  # noqa: E402
import carry_core as core  # noqa: E402

REPO = os.path.join(os.path.dirname(__file__), "..")
HI, LO = 0.0001, -0.0001          # a 1 bp print, either sign
NOTIONAL = 60_000.0


def row(i, rate, perp=60_000.0, spot=60_000.0):
    return cb.Settlement(ms=1_700_006_400_000 + i * cb.SETTLEMENT_MS, perp=perp,
                         spot=spot, rate=rate, perp_high=perp, perp_low=perp)


def run(rows, latency, **kw):
    kw.setdefault("borrow_apr", 0.0)
    kw.setdefault("impact_bps", 0.0)
    return cb.simulate_settlements_series(rows, notional=NOTIONAL,
                                          latency_rows=latency, **kw)


class TestAnEntryFillsAfterItsSignal:
    def test_it_opens_on_the_fill_row_at_that_rows_prices(self):
        rows = [row(0, HI, perp=60_000.0), row(1, HI, perp=61_000.0),
                row(2, HI, perp=62_000.0), row(3, HI, perp=63_000.0)]
        t = run(rows, 1)["trade_log"][0]
        assert t["opened"] == str(cb._when(rows[1].ms))
        assert t["opened"] != str(cb._when(rows[0].ms))

    def test_zero_latency_is_the_old_model(self):
        rows = [row(i, HI) for i in range(4)]
        t = run(rows, 0)["trade_log"][0]
        assert t["opened"] == str(cb._when(rows[0].ms))

    def test_the_fill_can_never_precede_the_decision(self):
        rows = [row(i, HI) for i in range(8)]
        for latency in (0, 1, 2, 3):
            first = run(rows, latency)["trade_log"][0]
            assert first["opened"] == str(cb._when(rows[latency].ms))

    def test_the_print_stamped_at_the_fill_instant_is_not_the_entrys(self):
        rows = [row(i, HI) for i in range(5)]
        # signal 0, fill 1: owns prints 2, 3, 4 (not 0, not 1)
        assert run(rows, 1)["attribution"]["funding_usd"] == pytest.approx(
            3 * HI * NOTIONAL)
        assert run(rows, 0)["attribution"]["funding_usd"] == pytest.approx(
            4 * HI * NOTIONAL)
        assert run(rows, 2)["attribution"]["funding_usd"] == pytest.approx(
            2 * HI * NOTIONAL)

    def test_no_new_signal_is_taken_while_one_is_pending(self):
        rows = [row(i, HI) for i in range(6)]
        assert run(rows, 2)["trades"] == 1


class TestAnEntryThePastTheDataIsAbstainedNotInvented:
    def test_a_fill_row_beyond_the_data_is_counted_and_not_traded(self):
        rows = [row(0, 0.0), row(1, 0.0), row(2, HI)]     # signal on the last row
        r = run(rows, 1)
        assert r["trades"] == 0 and r["unresolved_entries"] == 1

    def test_a_fill_on_the_last_row_has_no_print_left_and_is_abstained(self):
        rows = [row(0, 0.0), row(1, HI), row(2, 0.0)]     # fills on the last row
        r = run(rows, 1)
        assert r["trades"] == 0 and r["unresolved_entries"] == 1

    def test_no_zero_day_round_trip_pays_fees_for_nothing(self):
        rows = [row(0, 0.0), row(1, HI), row(2, 0.0)]
        r = run(rows, 1)
        assert r["fees_usd"] == 0.0 and r["net_usd"] == 0.0


class TestAnExitFillsAfterItsSignal:
    # signal on row 0; entry fills (L=1) on row 1; negatives on rows 2, 3, 4
    def _rows(self, **last):
        rows = [row(0, HI), row(1, HI), row(2, LO), row(3, LO), row(4, LO),
                row(5, HI, perp=61_000.0), row(6, HI, perp=62_000.0)]
        return rows

    def test_the_exit_is_filled_after_the_third_negative_print(self):
        rows = self._rows()
        t = run(rows, 1)["trade_log"][0]
        assert t["reason"] == "FUNDING_INVERTED"
        assert t["closed"] == str(cb._when(rows[5].ms))     # signal row 4, fill 5

    def test_zero_latency_exits_on_the_signal_row(self):
        rows = self._rows()
        t = run(rows, 0)["trade_log"][0]
        assert t["closed"] == str(cb._when(rows[4].ms))

    def test_a_receipt_at_the_exit_fill_instant_is_not_assumed(self):
        rows = self._rows()                  # row 5 prints +HI, the exit fill row
        f = run(rows, 1)["attribution"]["funding_usd"]
        # held prints 2,3,4 are negative: three payments, row 5's receipt omitted
        assert f == pytest.approx(3 * LO * NOTIONAL)

    def test_a_payment_at_the_exit_fill_instant_is_charged(self):
        rows = self._rows()
        rows[5] = row(5, LO, perp=61_000.0)
        f = run(rows, 1)["attribution"]["funding_usd"]
        # qty is 1.0 (fill at 60k); three payments at 60k, the fourth at 61k
        assert f == pytest.approx(LO * (3 * 60_000.0 + 61_000.0))

    def test_a_decision_whose_fill_is_past_the_data_leaves_the_position_open(self):
        rows = [row(0, HI), row(1, HI), row(2, LO), row(3, LO), row(4, LO)]
        r = run(rows, 1)                     # exit signal on the last row
        t = r["trade_log"][0]
        assert r["unresolved_exits"] == 1
        assert t["reason"] == "OPEN_AT_END" and t["open_at_end"] is True
        assert r["closed_trades"] == 0


class TestTheBooksStillBalance:
    @pytest.mark.parametrize("latency", (0, 1, 2, 3))
    def test_the_attribution_identity_holds(self, latency):
        rows = [row(i, HI if i % 5 < 3 else LO, perp=60_000.0 + 40 * i,
                    spot=60_000.0 + 37 * i) for i in range(60)]
        # attribution() refuses to return unless net == sum of its terms
        r = run(rows, latency, borrow_apr=0.05, impact_bps=1.0)
        a = r["attribution"]
        assert a["net_usd"] == pytest.approx(
            a["funding_usd"] + a["basis_usd"] + a["fees_usd"] + a["borrow_usd"]
            + a["impact_usd"] + a.get("other_usd", 0.0), abs=1e-6)

    def test_a_negative_latency_is_refused(self):
        with pytest.raises(ValueError):
            run([row(0, HI), row(1, HI)], -1)
        with pytest.raises(ValueError):
            core.params(latency_rows=-1)

    def test_the_result_says_what_it_assumed(self):
        r = run([row(i, HI) for i in range(5)], 1)
        assert r["latency_rows"] == 1 and "not a measured latency" in r["latency_note"]
        assert "NOT modelled" in r["latency_note"]

    def test_the_default_is_not_the_zero_latency_bound(self):
        assert cb.LATENCY_ROWS >= 1 and core.LATENCY_ROWS == cb.LATENCY_ROWS


def _native():
    built = core.build()
    if not built and not core.available():
        pytest.skip("no compiler and no libcarrycore.so")


def _cpp(rows, latency, **kw):
    p = core.params(notional=NOTIONAL, borrow_apr=kw.get("borrow_apr", 0.0),
                    impact_bps=kw.get("impact_bps", 0.0),
                    taker_spot_bps=cb.TAKER_BPS_SPOT,
                    taker_perp_bps=cb.TAKER_BPS_PERP, overlay=False,
                    latency_rows=latency)
    return core.simulate(rows, p)


SCENARIOS = {
    "clean entry and exit": [row(0, HI), row(1, HI), row(2, LO), row(3, LO),
                             row(4, LO), row(5, HI), row(6, HI), row(7, HI)],
    "exit signal on the last row": [row(0, HI), row(1, HI), row(2, LO),
                                    row(3, LO), row(4, LO)],
    "entry past the data": [row(0, 0.0), row(1, 0.0), row(2, HI)],
    "fill on the last row": [row(0, 0.0), row(1, HI), row(2, 0.0)],
    "payment at the exit fill": [row(0, HI), row(1, HI), row(2, LO), row(3, LO),
                                 row(4, LO), row(5, LO, perp=61_000.0),
                                 row(6, HI)],
    "reentry": [row(i, HI if i in (0, 1, 2, 9, 10, 11, 12) else LO)
                for i in range(16)],
}


class TestCppAndPythonAgreeOnTheNewModel:
    @pytest.mark.parametrize("latency", (0, 1, 2, 3))
    @pytest.mark.parametrize("name", sorted(SCENARIOS))
    def test_synthetic_scenarios(self, name, latency):
        _native()
        rows = SCENARIOS[name]
        py = run(rows, latency, mode=cb.ACQUIRE)
        cpp = _cpp(rows, latency)
        assert cpp["trades"] == py["trades"]
        assert cpp["closed_trades"] == py["closed_trades"]
        assert cpp["unresolved_entries"] == py["unresolved_entries"]
        assert cpp["unresolved_exits"] == py["unresolved_exits"]
        a = py["attribution"]
        assert cpp["net"] == pytest.approx(a["net_usd"], abs=0.01)
        assert cpp["funding"] == pytest.approx(a["funding_usd"], abs=0.01)
        assert cpp["fees"] == pytest.approx(-a["fees_usd"], abs=0.01)

    @pytest.mark.parametrize("latency", (0, 1, 2, 3))
    def test_real_corpus(self, latency):
        _native()
        rows = cb.load_bybit_settlements(REPO)[0]
        py = cb.simulate_settlements_series(
            rows, notional=NOTIONAL, borrow_apr=0.03, gated=True,
            impact_bps=1.093, mode=cb.OVERLAY, latency_rows=latency)
        p = core.params(notional=NOTIONAL, borrow_apr=0.03, impact_bps=1.093,
                        round_trip_bps=cb.round_trip_bps_for(cb.OVERLAY),
                        gated=True, overlay=True,
                        taker_spot_bps=cb.TAKER_BPS_SPOT,
                        taker_perp_bps=cb.TAKER_BPS_PERP, latency_rows=latency)
        cpp = core.simulate(rows, p)
        assert cpp["trades"] == py["trades"]
        assert cpp["net"] == pytest.approx(py["attribution"]["net_usd"], abs=0.01)
        assert cpp["unresolved_entries"] == py["unresolved_entries"]
        assert cpp["unresolved_exits"] == py["unresolved_exits"]

    def test_the_library_version_names_the_latency_model(self):
        _native()
        assert "latency" in core.version()


class TestTheLatencySettingSurvivesTheSearchHarness:
    """A base params carrying latency 0 must not be silently reset to the
    default by the rebuilds in walk-forward and the sweep."""

    @pytest.fixture(scope="class")
    def wf(self):
        import carry_walkforward as wf
        return wf

    def test_cell_params_keep_the_base_latency(self, wf):
        for latency in (0, 2):
            base = wf.base_params(notional=NOTIONAL, borrow_apr=0.0,
                                  impact_bps=0.0, overlay=True,
                                  latency_rows=latency)
            cell = wf.cell_params(base, {"hold_days": 30, "ewma_alpha": 0.5,
                                         "negative_exit_prints": 3})
            assert base.latency_rows == cell.latency_rows == latency

    def test_the_sweep_scores_every_cell_at_the_base_latency(self, wf):
        _native()
        rows = cb.load_bybit_settlements(REPO)[0]
        out = {}
        for latency in (0, 2):
            base = wf.base_params(notional=NOTIONAL, borrow_apr=0.0,
                                  impact_bps=0.0, overlay=True,
                                  latency_rows=latency)
            out[latency] = {(c["hold_days"], c["negative_exit_prints"],
                             c["ewma_alpha"]): c["net"]
                            for c in wf.sweep_cells(rows, base)}
        assert out[0] != out[2]            # the setting reached the native core
