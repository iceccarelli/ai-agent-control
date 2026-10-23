"""The corrected funding window, on both kinds of bar the corpora produce, with
every boundary probed one millisecond either side."""
from __future__ import annotations

import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import funding_window as fw  # noqa: E402
from signals import funding_carry_fade_v1 as frozen  # noqa: E402

DAY = 86_400_000
T0 = 1_660_000_000_000 // DAY * DAY            # a UTC midnight


def real_bar(i):
    """As the production loader builds them: end_us is 23:59:59.000."""
    start = T0 + i * DAY
    return types.SimpleNamespace(start_ms=start, end_us=(start + DAY - 1000) * 1000)


def fixture_bar(i):
    """As the hand-built test bars are: no end_us (the -1 ms fallback)."""
    return types.SimpleNamespace(start_ms=T0 + i * DAY)


@pytest.fixture(params=[real_bar, fixture_bar], ids=["real_bars", "fixture_bars"])
def bars(request):
    return [request.param(i) for i in range(6)]


def series(*stamps, rate=0.001):
    return frozen.FundingSeries(sorted(stamps), [rate] * len(stamps))


def short(f, bars, held=1, **kw):
    return fw.funding_paid_over_window(f, bars, 0, held, frozen.SHORT_SETUP, **kw)


class TestTheEntryBoundary:
    def test_a_print_one_ms_before_the_open_belongs_to_the_prior_holder(self, bars):
        T = bars[1].start_ms
        assert short(series(T - 1), bars) == 0.0

    def test_a_print_exactly_at_the_open_belongs_to_the_prior_holder(self, bars):
        T = bars[1].start_ms
        assert short(series(T), bars) == 0.0

    def test_a_print_one_ms_after_the_open_is_the_new_holders(self, bars):
        T = bars[1].start_ms
        assert short(series(T + 1), bars) == pytest.approx(-0.001)   # a short RECEIVES

    def test_a_print_in_the_gap_between_signal_close_and_entry_open_is_not_credited(
            self, bars):
        gap = fw.close_time_ms(bars[0]) + 1
        if gap < bars[1].start_ms:
            assert short(series(gap), bars) == 0.0


class TestTheExitBoundary:
    def test_one_ms_before_the_exit_close_is_held_through(self, bars):
        end = fw.close_time_ms(bars[2])
        assert short(series(end - 1), bars) == pytest.approx(-0.001)

    def test_exactly_at_the_exit_close_is_paid_by_the_holder(self, bars):
        end = fw.close_time_ms(bars[2])
        assert short(series(end), bars) == pytest.approx(-0.001)

    def test_one_ms_after_the_exit_close_is_not(self, bars):
        end = fw.close_time_ms(bars[2])
        assert short(series(end + 1), bars) == 0.0


class TestSigns:
    def test_a_long_pays_what_a_short_receives(self, bars):
        T = bars[1].start_ms
        f = series(T + 10)
        assert fw.funding_paid_over_window(
            f, bars, 0, 1, frozen.LONG_SETUP) == pytest.approx(0.001)
        assert short(f, bars) == pytest.approx(-0.001)

    def test_an_unknown_direction_is_refused(self, bars):
        with pytest.raises(ValueError):
            fw.funding_paid_over_window(series(1), bars, 0, 1, "sideways")

    def test_a_signal_on_the_last_bar_has_no_entry(self, bars):
        with pytest.raises(ValueError):
            fw.funding_paid_over_window(series(1), bars, len(bars) - 1, 1,
                                        frozen.SHORT_SETUP)


class TestAnIntrabarExitCannotCollectWhatItMightNotHaveHeld:
    def test_a_receipt_after_the_exit_bars_open_is_not_credited(self, bars):
        open_exit = bars[2].start_ms
        f = series(open_exit + 8 * 3_600_000)              # later that day
        assert short(f, bars, intrabar_exit=True) == 0.0       # a short would receive
        assert short(f, bars, intrabar_exit=False) == pytest.approx(-0.001)

    def test_a_payment_after_the_exit_bars_open_is_still_charged(self, bars):
        open_exit = bars[2].start_ms
        f = series(open_exit + 8 * 3_600_000)
        assert fw.funding_paid_over_window(
            f, bars, 0, 1, frozen.LONG_SETUP, intrabar_exit=True) == pytest.approx(0.001)

    def test_prints_before_the_exit_bars_open_count_normally(self, bars):
        T = bars[1].start_ms
        f = series(T + 8 * 3_600_000)
        assert short(f, bars, intrabar_exit=True) == pytest.approx(-0.001)

    def test_a_negative_rate_is_a_payment_by_a_short(self, bars):
        open_exit = bars[2].start_ms
        f = series(open_exit + 3_600_000, rate=-0.002)
        assert short(f, bars, intrabar_exit=True) == pytest.approx(0.002)


class TestTheFrozenMeasurementIsLeftAlone:
    """Documents the difference and pins the frozen function's old behaviour, so
    the corrected module cannot be mistaken for, or silently replace, it."""

    def test_the_frozen_function_still_credits_the_print_at_the_entry_open(self):
        bars = [real_bar(i) for i in range(6)]
        T = bars[1].start_ms
        f = series(T)
        assert frozen.funding_paid_over_hold(f, bars, 0, 1, frozen.SHORT_SETUP) \
            == pytest.approx(-0.001)                        # the known optimism
        assert short(f, bars) == 0.0                        # the corrected value

    def test_the_frozen_window_still_starts_before_the_entry_open(self):
        bars = [real_bar(i) for i in range(6)]
        assert frozen.close_time_ms(bars[0]) == bars[1].start_ms - 1000
        fixtures = [fixture_bar(i) for i in range(6)]
        assert frozen.close_time_ms(fixtures[0]) == fixtures[1].start_ms - 1

    def test_the_frozen_function_is_the_one_the_btc_variant_shares(self):
        from signals import funding_carry_fade_btc_v1 as btc
        assert btc.funding_paid_over_hold is frozen.funding_paid_over_hold
