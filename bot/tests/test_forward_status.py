"""forward_status: machine-derived facts about a forward-shadow artefact.

The committed artefact is read as it is (never rewritten); every other case is
a synthetic fixture in tmp_path.
"""
from __future__ import annotations

import calendar
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import forward_status as FS  # noqa: E402

REAL = os.path.join(os.path.dirname(__file__), "..", "artifacts",
                    "forward_shadow_current.json")


def ep(stamp: str) -> float:
    return float(calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")))


def day(i: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(ep("2027-01-01T00:00:00Z") + i * 86400))


def write(tmp_path, net_rs, *, observed=None, declared=None, extra=None,
          trades=None):
    if trades is None:
        trades = [{"entry_utc": day(2 * i), "exit_utc": day(2 * i + 1),
                   "direction": "SHORT_SETUP", "net_r": r,
                   "notional_usd": 100.0} for i, r in enumerate(net_rs)]
    art = {"schema": "forward_shadow_observation/15", "signal": "s",
           "symbol": "BTCUSDT",
           "observed_at_utc": observed or day(2 * len(trades) + 1),
           "forward_n_trades": len(trades) if declared is None else declared,
           "trades": trades,
           "forward_window": {"bar_utcs": [day(i) for i in range(2 * len(trades) + 1)]}}
    art.update(extra or {})
    p = tmp_path / "fwd.json"
    p.write_text(json.dumps(art))
    return str(p), ep(art["observed_at_utc"])


# -- the committed artefact, read as it is ----------------------------------

def test_the_committed_artefact_is_stale_today_and_insufficient():
    now = ep("2026-10-30T12:00:00Z")
    s = FS.evaluate_forward_evidence(REAL, now=now)
    assert s.state == FS.STALE
    assert s.evidence_state == FS.INSUFFICIENT_SAMPLE
    assert s.fresh is False
    assert s.signal == "funding_carry_fade_btc_v1" and s.symbol == "BTCUSDT"
    assert s.observed_at_utc == "2026-09-30T06:57:09Z"
    assert s.age_s == pytest.approx(now - ep("2026-09-30T06:57:09Z"))
    assert s.age_s > 29 * 86400
    assert s.n_closed_trades == 4 and s.declared_n_trades == 4
    assert [round(t["net_r"], 4) for t in s.trades] == [-1.0629, -0.6065,
                                                         -1.0293, -1.048]
    assert all(t["net_r"] < 0 for t in s.trades)
    assert s.mean_net_r == pytest.approx(-0.9367, abs=1e-4)
    assert s.first_entry_utc == "2026-08-20T00:00:00Z"
    assert s.last_exit_utc == "2026-09-21T00:00:00Z"
    assert s.forward_days_observed == 51
    assert s.trades_vs_gate == "4 of 20" and s.days_vs_gate == "51 of 180"
    assert s.trades_at_or_above_min is False and s.days_at_or_above_min is False
    assert s.is_gate_decision is False and s.as_dict()["is_gate_decision"] is False


def test_the_committed_artefact_on_its_own_day_is_fresh_but_insufficient():
    s = FS.evaluate_forward_evidence(REAL, now=ep("2026-09-30T12:00:00Z"))
    assert s.fresh is True and s.state == FS.INSUFFICIENT_SAMPLE


def test_the_gate_mirrors_match_promotion_gate():
    import promotion_gate as pg
    assert FS.GATE_MIN_FORWARD_TRADES == pg.MIN_FORWARD_TRADES
    assert FS.GATE_MIN_FORWARD_DAYS == pg.MIN_FORWARD_DAYS
    art = json.load(open(REAL))
    assert FS.DEFAULT_MIN_TRADES_FOR_TREND == \
        art["monitor_thresholds"]["M4_halves"]["min_trades"]


def test_reading_is_deterministic_and_never_writes(tmp_path):
    before = open(REAL, "rb").read()
    a = FS.evaluate_forward_evidence(REAL, now=ep("2026-10-30T00:00:00Z"))
    b = FS.evaluate_forward_evidence(REAL, now=ep("2026-10-30T00:00:00Z"))
    assert a == b
    assert open(REAL, "rb").read() == before


# -- unavailable evidence ---------------------------------------------------

def test_a_missing_file_is_no_data(tmp_path):
    s = FS.evaluate_forward_evidence(str(tmp_path / "nope.json"), now=1e9)
    assert s.state == s.evidence_state == FS.NO_DATA
    assert s.n_closed_trades == 0 and s.fresh is None


@pytest.mark.parametrize("body", ["not json", "[1,2]", "{\"trades\": []}",
                                  json.dumps({"observed_at_utc": "yesterday",
                                              "trades": []}),
                                  json.dumps({"observed_at_utc": "2027-01-01T00:00:00Z",
                                              "trades": "x",
                                              "forward_n_trades": 0})])
def test_garbage_is_unreadable(tmp_path, body):
    p = tmp_path / "g.json"
    p.write_text(body)
    s = FS.evaluate_forward_evidence(str(p), now=ep("2027-01-01T01:00:00Z"))
    assert s.state == FS.UNREADABLE and s.fresh is None


@pytest.mark.parametrize("bad", [
    {"net_r": float("nan")}, {"net_r": "1"}, {"net_r": True},
    {"entry_utc": "x"}, {"exit_utc": None},
    {"entry_utc": "2027-01-05T00:00:00Z", "exit_utc": "2027-01-04T00:00:00Z"},
    {"exit_utc": "2030-01-01T00:00:00Z"},     # closed after the observation
])
def test_an_impossible_trade_row_is_unreadable(tmp_path, bad):
    path, obs = write(tmp_path, [0.1, 0.2])
    art = json.load(open(path))
    art["trades"][1].update(bad)
    open(path, "w").write(json.dumps(art, allow_nan=True))
    s = FS.evaluate_forward_evidence(path, now=obs)
    assert s.state == FS.UNREADABLE


def test_a_declared_count_that_disagrees_is_unreadable(tmp_path):
    path, obs = write(tmp_path, [0.1, 0.2], declared=3)
    s = FS.evaluate_forward_evidence(path, now=obs)
    assert s.state == FS.UNREADABLE
    assert "forward_n_trades says 3" in " ".join(s.reasons)


def test_a_repeated_trade_is_never_counted_twice(tmp_path):
    path, obs = write(tmp_path, [0.1, 0.2])
    art = json.load(open(path))
    art["trades"].append(dict(art["trades"][0]))
    art["forward_n_trades"] = 3
    open(path, "w").write(json.dumps(art))
    s = FS.evaluate_forward_evidence(path, now=obs)
    assert s.state == FS.UNREADABLE
    assert s.n_closed_trades == 2 and s.duplicate_trades == 1


def test_a_future_stamp_is_unreadable_not_fresh(tmp_path):
    path, obs = write(tmp_path, [0.1])
    s = FS.evaluate_forward_evidence(path, now=obs - 3600)
    assert s.state == FS.UNREADABLE and "after now" in s.reasons[0]
    # within the clock tolerance it is simply fresh
    s = FS.evaluate_forward_evidence(path, now=obs - 60)
    assert s.fresh is True


@pytest.mark.parametrize("kw", [{"max_age_s": 0}, {"max_age_s": float("nan")},
                                {"max_age_s": float("inf")},
                                {"min_trades_for_trend": 1},
                                {"min_trades_for_trend": True}])
def test_limits_that_cannot_compare_are_refused(tmp_path, kw):
    path, obs = write(tmp_path, [0.1])
    with pytest.raises(ValueError):
        FS.evaluate_forward_evidence(path, now=obs, **kw)


def test_now_is_required():
    with pytest.raises(TypeError):
        FS.evaluate_forward_evidence(REAL)          # noqa


# -- staleness --------------------------------------------------------------

def test_stale_takes_precedence_but_the_evidence_is_still_reported(tmp_path):
    rs = [0.5] * 10 + [-1.0] * 10
    path, obs = write(tmp_path, rs)
    s = FS.evaluate_forward_evidence(path, now=obs + FS.DEFAULT_MAX_AGE_S + 1)
    assert s.state == FS.STALE and s.evidence_state == FS.DETERIORATING
    s = FS.evaluate_forward_evidence(path, now=obs + FS.DEFAULT_MAX_AGE_S)
    assert s.fresh is True and s.state == FS.DETERIORATING


def test_max_age_is_a_parameter(tmp_path):
    path, obs = write(tmp_path, [0.1])
    assert FS.evaluate_forward_evidence(path, now=obs + 7200,
                                        max_age_s=3600).state == FS.STALE
    assert FS.evaluate_forward_evidence(path, now=obs + 7200,
                                        max_age_s=10800).state == FS.INSUFFICIENT_SAMPLE


# -- trend states from synthetic fixtures -----------------------------------

def test_further_deterioration(tmp_path):
    rs = [-0.6, -0.9, -0.7, -0.8, -0.65, -0.75, -0.7, -0.85, -0.6, -0.8,
          -1.05, -1.1, -1.0, -1.2, -1.05, -1.1, -1.0, -1.15, -1.05, -1.1]
    path, obs = write(tmp_path, rs)
    s = FS.evaluate_forward_evidence(path, now=obs)
    assert s.state == FS.DETERIORATING
    assert s.halves_diff_r < -2 * s.halves_diff_se


def test_genuine_recovery(tmp_path):
    rs = [-1.0, -1.1, -0.9, -1.05, -1.0, -0.95, -1.1, -1.0, -0.9, -1.0,
          0.4, 0.6, 0.5, 0.3, 0.7, 0.5, 0.4, 0.6, 0.5, 0.45]
    path, obs = write(tmp_path, rs)
    s = FS.evaluate_forward_evidence(path, now=obs)
    assert s.state == FS.RECOVERING
    assert s.trades_at_or_above_min is True       # a fact, not a decision
    assert s.is_gate_decision is False


def test_noise_is_stable(tmp_path):
    rs = [1.0, -1.0] * 10
    path, obs = write(tmp_path, rs)
    assert FS.evaluate_forward_evidence(path, now=obs).state == FS.STABLE


def test_a_small_sample_never_claims_a_trend(tmp_path):
    rs = [0.5] * 9 + [-2.0] * 10                   # 19 < 20
    path, obs = write(tmp_path, rs)
    s = FS.evaluate_forward_evidence(path, now=obs)
    assert s.state == FS.INSUFFICIENT_SAMPLE


def test_trades_are_ordered_by_exit_not_by_file_order(tmp_path):
    trades = [{"entry_utc": day(10), "exit_utc": day(11), "net_r": 2.0},
              {"entry_utc": day(0), "exit_utc": day(1), "net_r": -1.0}]
    path, obs = write(tmp_path, None, trades=trades, observed=day(12))
    s = FS.evaluate_forward_evidence(path, now=obs, min_trades_for_trend=2)
    assert [t["net_r"] for t in s.trades] == [-1.0, 2.0]
    assert s.first_exit_utc == day(1) and s.last_exit_utc == day(11)
    assert s.halves_diff_r == pytest.approx(3.0)
    assert s.state == FS.RECOVERING               # se 0, nonzero diff


def test_every_state_is_in_the_closed_set(tmp_path):
    path, obs = write(tmp_path, [0.1])
    for now in (obs, obs + 1e7):
        s = FS.evaluate_forward_evidence(path, now=now)
        assert s.state in FS.STATES and s.evidence_state in FS.STATES
