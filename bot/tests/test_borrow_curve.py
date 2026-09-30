"""Financing as a series, and the refusals that keep it honest.

WHY THIS FILE EXISTS
====================
`borrow_apr` is a CONSTANT at every call site and the matrices sweep it at
0/3/5/8 %/yr. Over the exact window PHASE1_DECISION was computed on, the real
USDT rate has a mean of 4.98%/yr, a median of 3.00%, and a range of 1.00% to
104.00%. A flat number is not that (INVENTORY D43).

THE BOUND IN THIS MODULE WAS WRONG ON FIRST WRITE, AND THIS IS THE RECORD
========================================================================
`MAX_STALENESS_MS` was 12h, chosen "to cover every gap the series actually
has". The series has exactly one gap and it is 10h wide, so a 12h bound
ADMITTED the only defect present: a lookup inside the hole returned a rate
instead of refusing. A bound chosen to cover the gaps is a description, not a
guard. It is 4h now, and `test_the_documented_hole_is_refused` is the thing
that would have caught it.

THE HOLE IS NOT NOISE. The rate is 1.00%/yr immediately before it and 7.30%/yr
immediately after — a 7x jump across 10 unobserved hours. Carrying either
value across that gap is a fabrication with real magnitude.

WHAT THE NUMBER IS
==================
OKX's public savings LENDING rate: what a lender earns. A borrower pays more,
so every value is a FLOOR on the cost of financing, and a book that fails to
clear its costs against these rates fails harder against real ones.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

from borrow_curve import (DEFAULT_PATH, MAX_STALENESS_MS,  # noqa: E402
                          BorrowCurve, BorrowCurveError)

HOUR = 3_600_000
CORPUS = os.path.join(REPO, DEFAULT_PATH)

needs_corpus = pytest.mark.skipif(
    not os.path.exists(CORPUS),
    reason="the borrow series is not in this checkout")


def ms(y, m, d, h=12):
    return int(dt.datetime(y, m, d, h, tzinfo=dt.timezone.utc).timestamp() * 1000)


def hourly(n, start=0, rate=0.03):
    return BorrowCurve([start + i * HOUR for i in range(n)], [rate] * n,
                       source="test")


class TestAFlatCurveReproducesTheScalarExactly:
    """The equivalence that makes wiring this in safe: handed a constant, every
    consumer must produce bit-for-bit what the constant produced."""

    @pytest.mark.parametrize("apr", (0.0, 0.03, 0.05, 0.08, 0.1234))
    def test_per_day_matches_the_scalar_arithmetic(self, apr):
        assert BorrowCurve.flat(apr).per_day(0) == apr / 365.0

    @pytest.mark.parametrize("apr", (0.0, 0.03, 0.05, 0.08, 0.1234))
    def test_per_print_matches_the_scalar_arithmetic(self, apr):
        assert BorrowCurve.flat(apr).per_print(0) == apr / (365.0 * 3)

    def test_a_flat_curve_answers_at_any_instant(self):
        """It has one observation; it must not trip its own staleness bound,
        or the equivalence harness would refuse the thing it exists to test."""
        flat = BorrowCurve.flat(0.05)
        assert flat.rate_at(ms(2019, 1, 1)) == 0.05
        assert flat.rate_at(ms(2030, 1, 1)) == 0.05

    def test_the_periods_per_day_is_not_hardcoded(self):
        assert BorrowCurve.flat(0.06).per_print(0, periods_per_day=1) == \
            0.06 / 365.0


class TestItRefusesRatherThanInventing:
    def test_before_the_first_observation_is_refused(self):
        """32.2% of the seven-year corpus predates the venue floor. Filling it
        backwards would price 2019 at 2021's rate and call it a measurement."""
        curve = hourly(100, start=ms(2022, 1, 1))
        with pytest.raises(BorrowCurveError) as exc:
            curve.rate_at(ms(2021, 1, 1))
        assert "before this curve begins" in str(exc.value)

    def test_past_the_staleness_bound_is_refused(self):
        curve = hourly(10, start=0)
        last = 9 * HOUR
        assert curve.rate_at(last + MAX_STALENESS_MS) == 0.03
        with pytest.raises(BorrowCurveError) as exc:
            curve.rate_at(last + MAX_STALENESS_MS + 1)
        assert "stale" in str(exc.value)

    def test_the_bound_is_tighter_than_the_gap_it_guards(self):
        """The defect this file records: a 12h bound against a 10h hole
        admitted the hole. The bound must be STRICTLY tighter than the widest
        gap in the real series, or it guards nothing."""
        assert MAX_STALENESS_MS < 10 * HOUR

    def test_covers_agrees_with_rate_at(self):
        curve = hourly(10, start=0)
        assert curve.covers(5 * HOUR)
        assert not curve.covers(-HOUR)
        assert not curve.covers(9 * HOUR + MAX_STALENESS_MS + 1)

    def test_a_non_monotonic_series_is_refused_at_construction(self):
        with pytest.raises(BorrowCurveError):
            BorrowCurve([3 * HOUR, HOUR, 2 * HOUR], [0.01, 0.02, 0.03])

    def test_an_empty_series_is_refused(self):
        with pytest.raises(BorrowCurveError):
            BorrowCurve([], [])

    def test_a_missing_file_names_the_fetcher(self):
        with pytest.raises(BorrowCurveError) as exc:
            BorrowCurve.load(REPO, path="data/nope/absent.csv.gz")
        assert "fetch_borrow_rates" in str(exc.value)


class TestTheLookupTakesTheLastObservationAtOrBefore:
    def test_exactly_on_an_observation(self):
        curve = BorrowCurve([0, HOUR, 2 * HOUR], [0.01, 0.02, 0.03])
        assert curve.rate_at(HOUR) == 0.02

    def test_between_observations_takes_the_earlier(self):
        """Never the later one: at time t the next print has not happened."""
        curve = BorrowCurve([0, HOUR, 2 * HOUR], [0.01, 0.02, 0.03])
        assert curve.rate_at(HOUR + 60_000) == 0.02

    def test_the_last_observation_carries_within_the_bound(self):
        curve = BorrowCurve([0], [0.07])
        assert curve.rate_at(MAX_STALENESS_MS) == 0.07


@needs_corpus
class TestTheRealSeries:
    @pytest.fixture(scope="class")
    def curve(self):
        return BorrowCurve.load(REPO)

    def test_it_starts_at_the_venue_floor(self, curve):
        first = dt.datetime.fromtimestamp(curve.first_ms / 1000, dt.timezone.utc)
        assert first.strftime("%Y-%m-%d") == "2021-12-14"

    def test_it_has_exactly_one_gap(self, curve):
        assert len(curve.gaps()) == 1

    def test_the_documented_hole_is_refused(self, curve):
        """2022-12-18 08:00 -> 18:00. This is the assertion that would have
        caught the 12h bound."""
        assert not curve.covers(ms(2022, 12, 18, 14))

    def test_the_hole_is_a_real_discontinuity_not_noise(self, curve):
        """1.00%/yr before, 7.30%/yr after. Carrying either across 10
        unobserved hours is a fabrication with magnitude."""
        before = curve.rate_at(ms(2022, 12, 18, 8))
        after = curve.rate_at(ms(2022, 12, 18, 18))
        assert after > before * 5

    @pytest.mark.data_freshness
    def test_it_prices_essentially_all_of_the_frozen_window(self, curve):
        """The refusal must cost coverage, but not much: one print.

        `curve` is frozen at its own last observation, but the funding
        corpus it is priced against keeps growing with the daily accrual.
        Every new print past the curve's own end is, by definition, one more
        unpriceable stamp — so this count is supposed to drift wider as real
        time passes without the curve being re-extended. See bot/pytest.ini.
        """
        import csv
        import gzip
        path = os.path.join(
            REPO, "data/real_funding/funding/BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz")
        with gzip.open(path, "rt") as handle:
            stamps = [int(r["funding_time_ms"]) for r in csv.DictReader(handle)]
        priceable = sum(1 for t in stamps if curve.covers(t))
        assert priceable == len(stamps) - 1

    def test_the_seven_year_corpus_is_only_partly_priceable(self, curve):
        """The finding that bounds this whole line of work: the financed book
        cannot be costed where most of its return lives."""
        import csv
        import gzip
        path = os.path.join(
            REPO,
            "data/real_funding_full/funding/BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz")
        with gzip.open(path, "rt") as handle:
            stamps = [int(r["funding_time_ms"]) for r in csv.DictReader(handle)]
        refused = sum(1 for t in stamps if not curve.covers(t))
        assert refused > 2_400          # ~32% of 7,688

    def test_the_summary_says_what_the_number_is(self, curve):
        """A LENDING rate quoted as a borrowing cost is a cost understated."""
        assert "FLOOR" in curve.summary()["what_this_is"]
