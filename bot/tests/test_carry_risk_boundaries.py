"""`CarryRisk.gate_open` on its exact boundaries, and on a margin it cannot read.

Mutation testing of carry_risk.py left survivors that `test_carry_risk.py` does
not kill: the exact margin floor, the persisted-allowance trim keeping the
OLDEST dates instead of the newest, and `exhaust_today` ignoring a broken pair.
Reading the gate found one real fail-open at this layer:

    margin = float(snapshot.margin_multiple)
    if margin is not None and margin < floor: block

NaN is not below any floor, so a snapshot whose margin is NaN was APPROVED here.
In production `MarketSnapshot.assert_fresh` (called just above) rejects it
first, so no order was reachable; the gate was relying on a different module for
the one check it names. A pair gate that "must not be more permissive than the
engine it guards" now refuses a margin it cannot read by itself, with a reason.

Only refusals are added. No floor, cap or count moves.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from carry_risk import MIN_MARGIN_MULTIPLE, CarryRisk  # noqa: E402
from market_snapshot import MarketSnapshot  # noqa: E402

UTC = dt.timezone.utc


def snap(**kw):
    base = dict(perp_mark=100_000.0, spot_mark=100_000.0, funding_bps=1.0,
                margin_multiple=5.0, observed_at_s=time.time())
    base.update(kw)
    return MarketSnapshot(**base)


class Unchecked:
    """A snapshot whose own freshness check passes everything: the gate under
    test must not depend on it for the margin."""

    def __init__(self, margin):
        self.margin_multiple = margin
        self.basis_bps = 0.0

    def assert_fresh(self):
        return None


def gate(snapshot, **kw):
    return CarryRisk(store=None, max_notional_usd=100.0).gate_open(
        notional_usd=kw.pop("notional_usd", 50.0), snapshot=snapshot,
        has_open_pair=False, **kw)


# -- the margin floor ---------------------------------------------------------

def test_a_margin_exactly_on_the_floor_is_approved():
    assert gate(snap(margin_multiple=MIN_MARGIN_MULTIPLE)).ok


def test_a_margin_just_below_the_floor_is_refused():
    d = gate(snap(margin_multiple=math.nextafter(MIN_MARGIN_MULTIPLE, 0.0)))
    assert not d and d.reason == "MARGIN_HEADROOM_TOO_THIN"


def test_a_flat_book_has_no_margin_to_judge_and_is_approved():
    assert gate(snap(margin_multiple=None)).ok


@pytest.mark.parametrize("margin", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_margin_is_refused_without_the_snapshots_help(margin):
    d = gate(Unchecked(margin))
    assert not d and d.reason == "MARGIN_UNUSABLE"


@pytest.mark.parametrize("margin", ["abc", [], {}])
def test_a_margin_that_is_not_a_number_is_refused_not_raised(margin):
    d = gate(Unchecked(margin))
    assert not d and d.reason == "MARGIN_UNUSABLE"


# -- the cap and the day's allowance ----------------------------------------

def test_a_notional_exactly_on_the_cap_is_approved_and_a_cent_over_is_not():
    assert gate(snap(), notional_usd=100.0).ok
    d = gate(snap(), notional_usd=100.01)
    assert not d and d.reason == "NOTIONAL_ABOVE_CAP"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -1.0, None, "50"])
def test_an_unusable_notional_is_refused(bad):
    assert not gate(snap(), notional_usd=bad)


def test_loading_a_long_allowance_keeps_the_newest_dates_not_the_oldest():
    """Today's spent entry is the NEWEST date. Trimming to the first 32 instead
    of the last 32 dropped it and let a second entry through."""
    today = dt.date(2026, 10, 26)
    dates = [(today - dt.timedelta(days=40 - i)).isoformat() for i in range(41)]
    assert dates[-1] == today.isoformat()
    r = CarryRisk(store=None, max_notional_usd=100.0)
    assert r.load_days({"entries": dates, "broken": []})
    d = r.gate_open(notional_usd=50.0, snapshot=snap(), has_open_pair=False,
                    now=dt.datetime(2026, 10, 26, 12, 0, tzinfo=UTC))
    assert not d and d.reason == "ENTRY_LIMIT_REACHED_TODAY"
    assert len(r.export_days()["entries"]) == 32


def test_the_allowance_is_counted_per_utc_day_and_broken_pairs_spend_it():
    r = CarryRisk(store=None, max_notional_usd=100.0)
    noon = dt.datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    r.record_broken_pair(now=noon)
    d = r.gate_open(notional_usd=50.0, snapshot=snap(), has_open_pair=False,
                    now=noon)
    assert not d and d.reason == "ENTRY_LIMIT_REACHED_TODAY"
    assert d.detail["broken_pairs_today"] == 1 and d.detail["landed_pairs_today"] == 0
    tomorrow = noon + dt.timedelta(days=1)
    assert r.gate_open(notional_usd=50.0, snapshot=snap(), has_open_pair=False,
                       now=tomorrow).ok


def test_exhaust_today_adds_only_the_shortfall_and_counts_broken_pairs():
    """A broken pair already spent today's one entry: exhausting adds nothing,
    and repeating it never inflates the count."""
    r = CarryRisk(store=None, max_notional_usd=100.0)
    noon = dt.datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    r.record_broken_pair(now=noon)
    assert r.exhaust_today(now=noon) == 0
    assert r.export_days()["entries"] == []
    fresh = CarryRisk(store=None, max_notional_usd=100.0)
    assert fresh.exhaust_today(now=noon) == 1
    assert fresh.exhaust_today(now=noon) == 0
    assert len(fresh.export_days()["entries"]) == 1
