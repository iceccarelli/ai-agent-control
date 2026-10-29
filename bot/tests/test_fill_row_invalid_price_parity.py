"""An entry whose FILL row carries an unusable perp price is abstained, in both
simulators.

The decision row's price is gated, but with ``latency_rows >= 1`` the fill row's
is not re-checked. Python then raised ``ZeroDivisionError`` (the run died) while
the C++ core computed ``qty = notional / 0 = inf`` and returned ``trades=1`` with
NaN P&L, so a native sweep could report a number the reference simulator could
not produce. Both now count the entry in ``unresolved_entries`` and abstain.
Research simulators only; no order path.
"""
from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import carry_backtest as cb  # noqa: E402
import carry_core as core  # noqa: E402

H8 = 8 * 3600 * 1000


@pytest.fixture(scope="module")
def native():
    if not core.build() and not core.available():
        pytest.skip("no compiler and no libcarrycore.so")
    return core


def _rows(bad_index, bad_perp, n=6):
    rows = [cb.Settlement(ms=1_700_000_000_000 + i * H8, perp=100.0, spot=100.0,
                          rate=3e-4, perp_high=101.0, perp_low=99.0)
            for i in range(n)]
    r = rows[bad_index]
    rows[bad_index] = cb.Settlement(r.ms, bad_perp, r.spot, r.rate,
                                    r.perp_high, r.perp_low)
    return rows


@pytest.mark.parametrize("bad", [0.0, float("nan"), float("inf")])
def test_python_abstains_on_an_unusable_fill_row_price(bad):
    out = cb.simulate_settlements_series(_rows(1, bad), gated=False,
                                         mode=cb.OVERLAY, latency_rows=1,
                                         borrow_apr=0.0, impact_bps=0.0)
    assert out["unresolved_entries"] >= 1
    assert math.isfinite(out["net_usd"])


@pytest.mark.parametrize("bad", [0.0, float("nan"), float("inf")])
def test_the_native_core_agrees_with_python(native, bad):
    rows = _rows(1, bad)
    py = cb.simulate_settlements_series(rows, gated=False, mode=cb.OVERLAY,
                                        latency_rows=1, borrow_apr=0.0,
                                        impact_bps=0.0)
    nat = core.simulate(rows, core.params(gated=False, overlay=True,
                                          latency_rows=1, borrow_apr=0.0,
                                          impact_bps=0.0))
    assert nat["unresolved_entries"] == py["unresolved_entries"] >= 1
    assert nat["trades"] == py["trades"]
    assert nat["funding"] == pytest.approx(py["gross_funding_usd"], abs=0.01)
    assert math.isfinite(nat["net"]) and math.isfinite(py["net_usd"])
    assert nat["net"] == pytest.approx(py["net_usd"], abs=0.01)


def test_the_zero_latency_path_abstains_too(native):
    rows = _rows(0, 0.0)
    kw = dict(gated=False, latency_rows=0, borrow_apr=0.0, impact_bps=0.0)
    py = cb.simulate_settlements_series(rows, mode=cb.OVERLAY, **kw)
    nat = core.simulate(rows, core.params(overlay=True, **kw))
    assert nat["unresolved_entries"] == py["unresolved_entries"] >= 1
    assert nat["trades"] == py["trades"]
    assert math.isfinite(nat["net"]) and math.isfinite(py["net_usd"])
    assert nat["net"] == pytest.approx(py["net_usd"], abs=0.01)
