"""The market-data path prices funding per 8h settlement; a feed that states another interval (or none) is refused, as carry_broker already does on the REST path.
Reported by the data-capture worker: LiveFeedBroker.get_funding_bps returned a per-settlement bps value for ANY fundingIntervalHour."""
from __future__ import annotations

import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import md_broker as MB  # noqa: E402
import md_state as S  # noqa: E402


def broker(interval, rate=0.0001):
    view = types.SimpleNamespace(environment="mainnet", symbol="BTCUSDT", funding_interval_h=interval, funding_rate=rate,
                                 refusals=lambda mode: [])
    return MB.LiveFeedBroker(lambda: view, delegate=None, expected_environment="mainnet", perp_symbol="BTCUSDT", spot_symbol="BTCUSDT")


def test_positive_control_eight_hours_prices_the_rate_in_bps():
    assert broker(8, 0.0001).get_funding_bps("BTCUSDT") == pytest.approx(1.0)


@pytest.mark.parametrize("interval", [1, 2, 4, 6, 12, 0, -8, None, True, 8.5, "8"])
def test_any_other_or_missing_interval_is_refused(interval):
    with pytest.raises(S.MarketUnusable, match="funding_interval"):
        broker(interval).get_funding_bps("BTCUSDT")


def test_a_missing_rate_is_still_refused_not_zero():
    with pytest.raises(Exception):
        broker(8, None).get_funding_bps("BTCUSDT")
