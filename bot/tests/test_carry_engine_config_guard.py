"""The engine's own risk parameters must be usable numbers.

A NaN delta band never compares greater than drift, so the book would never
rebalance; a NaN margin floor never compares above the margin, so it would
never exit on thin headroom. The constructor refuses rather than trading with
a brake that cannot fire.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from carry_engine import CarryEngine  # noqa: E402


def build(**kw):
    args = dict(broker=object(), borrow_apr=0.0, execution_mode="overlay",
                persist=lambda s: None)
    args.update(kw)
    return CarryEngine(**args)


def test_defaults_construct():
    assert build() is not None


@pytest.mark.parametrize("name", ["max_notional_usd", "delta_band",
                                  "min_margin_multiple"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -1.0])
def test_a_brake_that_cannot_fire_is_refused(name, bad):
    with pytest.raises(ValueError, match=name):
        build(**{name: bad})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.01])
def test_borrow_apr_must_be_a_non_negative_finite_number(bad):
    with pytest.raises(ValueError, match="borrow_apr"):
        build(borrow_apr=bad)
