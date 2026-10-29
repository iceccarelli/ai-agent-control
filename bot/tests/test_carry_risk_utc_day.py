"""The daily entry allowance is a UTC day whatever timezone ``now`` carries.

``CarryRisk`` took ``now.date()`` in the datetime's own zone, so the same
instant bucketed into different days: an entry recorded for 2026-01-02 01:00 UTC
expressed at UTC+10 (11:00 on the 2nd) and one at UTC-10 (15:00 on the 1st)
disagreed, letting a second entry through on one UTC day or blocking on the
previous day's. A naive datetime is read as UTC. Offline; no snapshot or store.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from carry_risk import CarryRisk  # noqa: E402

UTC = dt.timezone.utc
PLUS10 = dt.timezone(dt.timedelta(hours=10))
MINUS10 = dt.timezone(dt.timedelta(hours=-10))
SNAP = SimpleNamespace(assert_fresh=lambda: None, margin_multiple=None)


def _risk():
    return CarryRisk(max_notional_usd=100.0)


def _gate(risk, now):
    return risk.gate_open(notional_usd=100.0, snapshot=SNAP,
                          has_open_pair=False, now=now)


def test_same_utc_day_in_a_different_zone_is_the_same_day():
    risk = _risk()
    risk.record_entry(now=dt.datetime(2026, 1, 2, 1, 0, tzinfo=UTC))
    # 2026-01-02 11:00 at UTC+10 is 01:00 UTC on the 2nd: same day, blocked.
    d = _gate(risk, dt.datetime(2026, 1, 2, 11, 0, tzinfo=PLUS10))
    assert d.ok is False and d.reason == "ENTRY_LIMIT_REACHED_TODAY"


def test_a_next_utc_day_seen_from_a_zone_still_on_the_previous_date_is_allowed():
    risk = _risk()
    risk.record_entry(now=dt.datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    # 2026-01-02 01:00 UTC is 2026-01-01 15:00 at UTC-10: a new UTC day.
    assert _gate(risk, dt.datetime(2026, 1, 1, 15, 0, tzinfo=MINUS10)).ok


def test_recording_in_a_zone_uses_the_utc_day():
    risk = _risk()
    risk.record_entry(now=dt.datetime(2026, 1, 1, 15, 0, tzinfo=MINUS10))
    assert risk.export_days()["entries"] == ["2026-01-02"]


def test_a_naive_datetime_is_read_as_utc():
    risk = _risk()
    risk.record_entry(now=dt.datetime(2026, 1, 2, 1, 0))
    assert risk.export_days()["entries"] == ["2026-01-02"]
