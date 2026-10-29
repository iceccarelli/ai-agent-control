"""What the market paid, quarter by quarter, and which part of a replay's profit
was earned in the generous quarters. Descriptive only: the buckets are the
rank-thirds of the quarters themselves, so there is no threshold to tune.
Hand-computed cases; offline."""
from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
sys.path.insert(0, os.path.dirname(HERE))

import funding_regimes as fr  # noqa: E402

H8 = 8 * 3600 * 1000


def ms(y, m, d, h=0):
    return int(dt.datetime(y, m, d, h, tzinfo=dt.timezone.utc).timestamp() * 1000)


class P:                      # a settlement print: only ms and rate matter here
    def __init__(self, ms_, rate):
        self.ms, self.rate = ms_, rate


def prints(year, month, n, rate):
    start = ms(year, month, 2)
    return [P(start + i * H8, rate) for i in range(n)]


def test_a_quarters_annualised_funding_is_rate_times_three_a_day_times_365():
    table = fr.quarterly_table(prints(2024, 1, 90, 0.0001))          # 1 bp per 8h
    assert len(table) == 1 and table[0]["quarter"] == "2024Q1"
    assert table[0]["prints"] == 90
    assert table[0]["mean_annualised_pct"] == pytest.approx(0.0001 * 3 * 365 * 100)
    assert table[0]["pct_positive"] == 100.0


def test_the_share_of_positive_prints_counts_only_positive():
    rows = prints(2024, 1, 4, 0.0001) + prints(2024, 2, 4, -0.0001)
    assert fr.quarterly_table(rows)[0]["pct_positive"] == 50.0


def test_quarters_are_split_into_rank_thirds_with_no_threshold():
    rows = []
    for i, rate in enumerate([0.00001, 0.00002, 0.00005, 0.00006, 0.0002, 0.0003]):
        rows += prints(2023 + i // 4, 1 + 3 * (i % 4), 10, rate)
    table = fr.with_terciles(fr.quarterly_table(rows))
    assert [t["third"] for t in table] == ["low", "low", "mid", "mid", "high", "high"]


def test_a_trade_is_spread_over_the_days_it_was_held():
    # 10 days: 4 in Q1, 6 in Q2 -> 40% / 60% of its net
    trade = {"opened_ms": ms(2024, 3, 28), "closed_ms": ms(2024, 4, 7)}
    out = fr.allocate_to_quarters([trade], [100.0])
    assert out["2024Q1"] == pytest.approx(40.0) and out["2024Q2"] == pytest.approx(60.0)
    assert sum(out.values()) == pytest.approx(100.0)


def test_attribution_by_third_adds_back_to_the_total_net():
    rows = []
    for i, rate in enumerate([0.00001, 0.00005, 0.0003]):
        rows += prints(2023, 1 + 3 * i, 30, rate)
    table = fr.with_terciles(fr.quarterly_table(rows))
    trades = [{"opened_ms": ms(2023, 1, 5), "closed_ms": ms(2023, 2, 5)},
              {"opened_ms": ms(2023, 7, 5), "closed_ms": ms(2023, 8, 5)}]
    out = fr.attribute_by_third(table, trades, [10.0, 90.0])
    assert sum(out["net_by_third"].values()) == pytest.approx(100.0)
    assert out["net_by_third"]["high"] == pytest.approx(90.0)
    assert out["share_in_high_third"] == pytest.approx(0.9)


def test_nothing_to_attribute_is_reported_as_none_not_zero():
    table = fr.with_terciles(fr.quarterly_table(prints(2024, 1, 10, 0.0001)))
    out = fr.attribute_by_third(table, [], [])
    assert out["share_in_high_third"] is None


def test_a_net_that_is_not_positive_has_no_share():
    table = fr.with_terciles(fr.quarterly_table(prints(2024, 1, 10, 0.0001)))
    trade = {"opened_ms": ms(2024, 1, 5), "closed_ms": ms(2024, 1, 9)}
    assert fr.attribute_by_third(table, [trade], [-5.0])["share_in_high_third"] is None


def test_the_latest_ninety_days_are_summarised_separately_from_the_quarters():
    rows = prints(2024, 1, 270, 0.0003) + prints(2024, 10, 270, 0.00001)
    latest = fr.latest_window(rows, prints_back=270)
    assert latest["mean_annualised_pct"] == pytest.approx(0.00001 * 3 * 365 * 100)
    assert latest["prints"] == 270
