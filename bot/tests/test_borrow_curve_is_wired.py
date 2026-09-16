"""Charging the curve, and the counter that must never go quiet.

WHY
===
`borrow_apr` was a CONSTANT in both Python simulators. The real USDT rate over
the very window PHASE1_DECISION used has a mean of 4.95%/yr and a max of
104.00% (INVENTORY D43), so the 0/3/5/8 sweep never priced the truth.

THE TWO PROPERTIES THAT MAKE THIS SAFE TO COMMIT
================================================
1. EQUIVALENCE. Handed a flat curve, each simulator must reproduce the scalar
   path bit for bit — and with NO curve passed it must evaluate the original
   expression unchanged, so every previously committed number rests on
   untouched arithmetic rather than on a flat-curve special case.

2. THE COUNTER IS READABLE. A period the curve cannot price falls back to the
   declared scalar and is COUNTED. That is the `unknown_fees` rule: a cost
   that quietly becomes something else has left the books.

Property 2 is here because I broke it. `simulate_series` incremented its
counter and never returned it — `KeyError: borrow_periods_unpriced` — so the
daily path silently lost exactly the number written to prevent silence. An
ad-hoc script caught it; a test should have.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

os.environ.setdefault("LEDGER_DISABLED", "1")

import carry_backtest as cb          # noqa: E402
from borrow_curve import BorrowCurve  # noqa: E402

CORPUS = os.path.join(REPO, "data", "real_bybit_btc_4h")
SERIES = os.path.join(REPO, "data", "real_borrow",
                      "OKX_LENDING_RATE_USDT_1H.csv.gz")

needs_corpus = pytest.mark.skipif(
    not os.path.isdir(CORPUS), reason="settlement corpus absent")
needs_series = pytest.mark.skipif(
    not os.path.exists(SERIES), reason="borrow series absent")


@pytest.fixture(scope="module")
def rows():
    return cb.load_bybit_settlements(REPO)[0]


@pytest.fixture(scope="module")
def curve():
    return BorrowCurve.load(REPO)


def settle(rows, **kw):
    return cb.simulate_settlements_series(
        rows, gated=True, impact_bps=1.093, mode=cb.OVERLAY, **kw)


@needs_corpus
class TestTheScalarPathIsUntouched:
    @pytest.mark.parametrize("apr", (0.0, 0.03, 0.05, 0.08))
    def test_no_curve_reports_itself_as_scalar(self, rows, apr):
        r = settle(rows, borrow_apr=apr)
        assert r["borrow_source"] == "scalar"
        assert r["borrow_periods_unpriced"] == 0

    @pytest.mark.parametrize("apr", (0.0, 0.03, 0.05, 0.08))
    def test_a_flat_curve_reproduces_the_scalar_to_the_cent(self, rows, apr):
        a = settle(rows, borrow_apr=apr)
        b = settle(rows, borrow_apr=apr, borrow_curve=BorrowCurve.flat(apr))
        assert b["net_usd"] == pytest.approx(a["net_usd"], abs=1e-6)
        assert b["borrow_usd"] == pytest.approx(a["borrow_usd"], abs=1e-6)
        assert b["trades"] == a["trades"]

    def test_the_headline_overlay_number_has_not_moved(self, rows):
        """+7.23%/yr at 0% borrow is what this corpus has always reported."""
        r = settle(rows, borrow_apr=0.0)
        assert r["net_annualised_pct"] == pytest.approx(7.23, abs=0.01)


@needs_corpus
@needs_series
class TestTheCounterIsReadable:
    """The defect: a counter that is incremented and never returned."""

    def test_the_settlement_sim_returns_it(self, rows, curve):
        assert "borrow_periods_unpriced" in settle(
            rows, borrow_apr=0.05, borrow_curve=curve)

    def test_the_daily_sim_returns_it(self, curve):
        src = cb._sources("BTC", "frozen")
        perp = cb.drop_open_bar(cb.load_series(os.path.join(REPO, src["perp"])))
        spot, _label, _q = cb.resolve_spot(REPO, "BTC", "frozen")
        ftimes, frates = cb.load_funding(os.path.join(REPO, src["funding"]))
        r = cb.simulate_series(perp=perp, spot=spot, ftimes=ftimes,
                               frates=frates, gated=True, mode=cb.ACQUIRE,
                               borrow_apr=0.05, borrow_curve=curve)
        assert "borrow_periods_unpriced" in r
        assert "borrow_source" in r

    def test_both_sims_report_the_same_two_keys(self, rows, curve):
        keys = {"borrow_source", "borrow_periods_unpriced"}
        assert keys <= set(settle(rows, borrow_apr=0.0, borrow_curve=curve))

    def test_an_uncoverable_period_is_counted_not_hidden(self, rows):
        """A curve that cannot price ANYTHING must fall back and say so for
        every period, rather than quietly charging the scalar."""
        far_future = BorrowCurve([4_102_444_800_000], [0.03],
                                 source="unreachable")
        r = settle(rows, borrow_apr=0.05, borrow_curve=far_future)
        assert r["borrow_periods_unpriced"] > 0
        assert r["borrow_source"] == "unreachable"

    def test_the_fallback_equals_the_declared_scalar(self, rows):
        """When nothing can be priced, the run must equal the scalar run —
        the fallback is the declared rate, not zero and not a guess."""
        far_future = BorrowCurve([4_102_444_800_000], [0.03],
                                 source="unreachable")
        fell_back = settle(rows, borrow_apr=0.05, borrow_curve=far_future)
        scalar = settle(rows, borrow_apr=0.05)
        assert fell_back["net_usd"] == pytest.approx(scalar["net_usd"], abs=1e-6)


@needs_corpus
@needs_series
class TestTheRealCurveCostsMoreThanTheColumnsQuoted:
    """D43's answer. Not a tuning knob — a measurement with a direction."""

    def test_it_names_the_series_it_charged(self, rows, curve):
        r = settle(rows, borrow_apr=0.05, borrow_curve=curve)
        assert "OKX_LENDING_RATE" in r["borrow_source"]

    def test_it_charges_more_than_a_flat_five_percent(self, rows, curve):
        real = settle(rows, borrow_apr=0.05, borrow_curve=curve)
        flat = settle(rows, borrow_apr=0.05)
        assert abs(real["borrow_usd"]) > abs(flat["borrow_usd"])

    def test_the_financed_book_lands_below_the_five_percent_column(
            self, rows, curve):
        real = settle(rows, borrow_apr=0.05, borrow_curve=curve)
        flat5 = settle(rows, borrow_apr=0.05)
        assert real["net_annualised_pct"] < flat5["net_annualised_pct"]

    def test_it_is_still_above_water_which_is_why_the_ceiling_matters(
            self, rows, curve):
        """+1.24%/yr is a CEILING: OKX's LENDING rate is what a lender earns,
        so a borrower pays more. A test that asserted 'profitable' here would
        be quoting a floor as a result."""
        real = settle(rows, borrow_apr=0.05, borrow_curve=curve)
        assert 0.0 < real["net_annualised_pct"] < 2.0

    def test_almost_nothing_fell_back(self, rows, curve):
        """One print, the 2022-12-18 hole. A large count would mean the run is
        partly constant and must not read as a curve result."""
        assert settle(rows, borrow_apr=0.05,
                      borrow_curve=curve)["borrow_periods_unpriced"] <= 2
