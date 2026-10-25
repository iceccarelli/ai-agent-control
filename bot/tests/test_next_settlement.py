"""When is a carry drill allowed to start? Only shortly before a settlement."""
from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import next_settlement as N  # noqa: E402


def ms(y, mo, d, h, mi=0, s=0):
    return int(dt.datetime(y, mo, d, h, mi, s, tzinfo=dt.timezone.utc).timestamp() * 1000)


class TestNextSettlement:
    @pytest.mark.parametrize("now,expected", [
        (ms(2026, 10, 25, 11, 48), ms(2026, 10, 25, 16)),
        (ms(2026, 10, 25, 0, 0, 1), ms(2026, 10, 25, 8)),
        (ms(2026, 10, 25, 7, 59, 59), ms(2026, 10, 25, 8)),
        (ms(2026, 10, 25, 16, 0, 1), ms(2026, 10, 26, 0)),
        (ms(2026, 10, 25, 23, 59, 59), ms(2026, 10, 26, 0)),
        (ms(2026, 12, 31, 20), ms(2027, 1, 1, 0)),
    ])
    def test_the_first_settlement_after_now(self, now, expected):
        assert N.next_settlement_ms(now) == expected

    def test_exactly_on_a_settlement_means_the_next_one(self):
        assert N.next_settlement_ms(ms(2026, 10, 25, 8)) == ms(2026, 10, 25, 16)


class TestTheWindow:
    def status(self, now, lo=4, hi=20):
        return N.window_status(now, min_lead_s=lo * 60, max_lead_s=hi * 60)

    @pytest.mark.parametrize("minute,inside", [
        (39, False), (40, True), (45, True), (50, True), (55, True), (56, True),
        (57, False), (59, False)])
    def test_before_the_16_00_settlement(self, minute, inside):
        assert self.status(ms(2026, 10, 25, 15, minute))[0] is inside

    def test_just_after_a_settlement_is_outside(self):
        assert self.status(ms(2026, 10, 25, 16, 2))[0] is False

    def test_it_reports_the_lead_and_the_settlement(self):
        inside, lead, settle = self.status(ms(2026, 10, 25, 15, 50))
        assert (inside, lead, settle) == (True, 600, ms(2026, 10, 25, 16))

    @pytest.mark.parametrize("lo,hi", [(-1, 5), (10, 5)])
    def test_a_nonsense_window_is_refused(self, lo, hi):
        with pytest.raises(ValueError):
            N.window_status(0, min_lead_s=lo * 60, max_lead_s=hi * 60)


class TestTheCommandLine:
    def test_inside_is_exit_0_outside_is_exit_10(self, capsys):
        assert N.main(["--now-ms", str(ms(2026, 10, 25, 15, 50))]) == 0
        out = capsys.readouterr().out
        assert "inside_window=true" in out and "seconds_to_settlement=600" in out
        assert N.main(["--now-ms", str(ms(2026, 10, 25, 11, 0))]) == 10
        assert "inside_window=false" in capsys.readouterr().out

    def test_a_bad_window_is_a_refusal(self):
        assert N.main(["--now-ms", "1", "--min-lead-minutes", "30",
                       "--max-lead-minutes", "5"]) == 3
