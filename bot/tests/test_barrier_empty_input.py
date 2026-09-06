"""barrier_r_for_all_bars must return the same arity on every path.

Finding: the empty-input early return gave a 2-tuple while every real
caller (tools/, tests/ — 70+ sites) unpacks 3 values
(`indices, net, bars_used`). Any input too short to produce an eligible
bar — a fresh corpus slice, an all-NaN ATR window, a horizon that
consumes the whole series — hit this path and crashed with a ValueError
at the CALL SITE rather than at the point of the actual problem (no
eligible bars), which is a strictly worse failure mode for a research
tool: it looks like a bug in the caller, not a fact about the data.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import skill_test as sk  # noqa: E402
from backtest import Bar  # noqa: E402


def _bars(n, start_ms=0, day_ms=86_400_000):
    return [Bar(start_ms + i * day_ms, 100.0, 101.0, 99.0, 100.0, 1_000.0)
            for i in range(n)]


def test_too_short_for_any_eligible_bar_returns_three_empty_arrays():
    bars = _bars(3)  # far shorter than atr_period + horizon
    result = sk.barrier_r_for_all_bars(
        bars, side="long", entry_on="next_open", stop_atr=1.5,
        take_profit_atr=1.5, horizon=5, round_trip_bps=25.0, atr_period=14,
    )
    assert len(result) == 3
    indices, net, bars_used = result
    assert indices.size == net.size == bars_used.size == 0
    assert indices.dtype == int and bars_used.dtype == np.int32


def test_the_documented_calling_convention_actually_works_on_empty_input():
    """This is the exact unpack every real call site uses."""
    bars = _bars(3)
    indices, net, used = sk.barrier_r_for_all_bars(
        bars, side="short", entry_on="close", stop_atr=1.5,
        take_profit_atr=1.0, horizon=5, round_trip_bps=25.0, atr_period=14,
    )
    assert indices.tolist() == []


def test_non_empty_path_is_unchanged_and_still_returns_three():
    bars = _bars(40)
    result = sk.barrier_r_for_all_bars(
        bars, side="long", entry_on="next_open", stop_atr=1.5,
        take_profit_atr=1.5, horizon=5, round_trip_bps=25.0, atr_period=14,
    )
    assert len(result) == 3
    assert result[0].size > 0
