"""Round 22: the recorder's health is ONE closed vocabulary (healthy / degraded / stalled / storage_blocked / image_mismatch / unknown) and nothing that cannot be established is ever `healthy`.

The owner's situation (2026-11-14): a 5 GB volume, 509 MiB free against the 512 MiB write reserve, a status file that stopped being refreshed, a recorder count frozen at 21,837,668, one closed day of ~1.90 GB,
and an image from before the recorder's bounded recovery. Pure functions only: no clock, no I/O, no network."""
from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path[:0] = [BOT, os.path.join(BOT, "tools")]

import recorder_health as RH  # noqa: E402

MIB = 1024 * 1024
GB = 10 ** 9
NOW = dt.datetime(2026, 11, 14, 16, 0, 0, tzinfo=dt.timezone.utc).timestamp()          # 16:00 UTC: 16 h into the open day
VERIFIED = {"status": "VERIFIED", "git_sha": "f86e8db", "recorder_fingerprint": "ab" * 32, "image_ref": "registry.fly.io/carry-drill:deployment-x", "environment_class": "READ_ONLY_RECORDER"}


def seg(days):
    return {d: int(b) for d, b in days.items()}


def status(*, age=10.0, growth_age=5.0, free=4 * GB, reserve=512 * MIB, retention=2, fault=None, threads=True, segment_bytes=None, restarts=None, hb_errors=0, now=NOW, commit_age=3.0, records=21_837_668):
    return {"schema": "market_recorder/v1", "pid": 7, "environment": "mainnet", "symbol": "BTCUSDT", "started_ms": int((now - 90_000) * 1000), "now_ms": int((now - age) * 1000), "uptime_s": 90_000.0,
            "feed": {"threads_alive": threads}, "heartbeat": {"status_errors": hb_errors, "maintenance_errors": 0},
            "progress": {"newest_record_id": 5, "market_inputs": 5, "last_growth_ms": int((now - growth_age) * 1000), "growth_age_s": growth_age},
            "store": {"records": records, "records_counted_ms": int((now - 400) * 1000), "segments": 3, "free_bytes": free, "min_free_bytes": reserve, "retention_days": retention,
                      "fault": fault, "last_commit_ms": int((now - commit_age) * 1000), "segment_bytes": segment_bytes}}


def healthy_kw(**over):
    kw = dict(now_s=NOW, free_bytes=8 * GB, total_bytes=10 * GB, supervisor={"recorder_pid": 7, "recorder_restarts": 0, "recorder_watchdog_kills": 0}, provenance=VERIFIED)
    kw.update(over)
    return kw


def codes(res):
    return {r["code"] for r in res["reasons"]}


def test_a_fresh_growing_unblocked_verified_recorder_is_healthy_and_the_numbers_are_reported():
    days = seg({"20261112": 1.0 * GB, "20261113": 1.0 * GB, "20261114": 0.6 * GB})          # 16 h into the day: 0.6 GB so far = 1.0 GB/day
    res = RH.classify(status(segment_bytes=days, free=8 * GB), **healthy_kw())
    assert res["state"] == "healthy" and res["reasons"] == [], res["reasons"]
    m = res["metrics"]
    assert m["last_write_age_s"] == pytest.approx(5.0) and m["last_commit_age_s"] == pytest.approx(3.0) and m["records"] == 21_837_668
    assert m["effective_reserve_bytes"] == 512 * MIB and m["effective_retention_days"] == 2 and m["volume_total_bytes"] == 10 * GB
    assert m["bytes_remaining_above_reserve"] == pytest.approx(8 * GB - 512 * MIB) and m["free_source"] == "statvfs"
    assert m["hours_to_reserve_no_prune"] is not None and m["hours_to_reserve_no_prune"] > 24
    assert m["volume_holds_retention"] is True
    assert m["process"]["pid"] == 7 and m["recorder"]["symbol"] == "BTCUSDT" and m["image"]["status"] == "VERIFIED" and m["image"]["git_sha"] == "f86e8db"


@pytest.mark.parametrize("none_of_these", ["status_none", "stale", "future", "no_provenance", "growth_unknown"])
def test_nothing_that_cannot_be_established_is_ever_healthy(none_of_these):
    kw = healthy_kw()
    st = status(segment_bytes=None)
    if none_of_these == "status_none":
        st = None
    elif none_of_these == "stale":
        st = status(age=3600.0)
    elif none_of_these == "future":
        st = status(age=-1000.0)
    elif none_of_these == "no_provenance":
        kw["provenance"] = None
    elif none_of_these == "growth_unknown":
        st["progress"]["growth_age_s"] = None
    res = RH.classify(st, **kw)
    assert res["state"] != "healthy", (none_of_these, res["reasons"])
    assert res["state"] in RH.STATES


def test_the_owners_volume_is_storage_blocked_even_when_the_status_is_stale_and_says_why():
    """509 MiB free < the 512 MiB reserve (statvfs, read NOW); the status stopped being refreshed. The store refuses every write: storage_blocked, not 'unknown'."""
    res = RH.classify(status(age=7200.0, free=509 * MIB), **healthy_kw(free_bytes=509 * MIB, total_bytes=5_251_530_752, provenance={"status": "MISMATCH", "detail": "image differs in 49 files"}))
    assert res["state"] == "storage_blocked"
    assert {"FREE_BELOW_RESERVE", "STATUS_STALE", "IMAGE_MISMATCH"} <= codes(res), "every condition is listed, none hidden by the one that wins"
    m = res["metrics"]
    assert m["bytes_remaining_above_reserve"] == 0.0 and m["free_source"] == "statvfs" and m["image"]["status"] == "MISMATCH"


def test_one_closed_day_of_1_9_gb_does_not_fit_retention_2_on_a_5_gb_volume_and_the_tool_says_so():
    days = seg({"20261111": 1.9 * GB, "20261112": 1.9 * GB, "20261113": 1.9 * GB, "20261114": 1.2 * GB})
    res = RH.classify(status(segment_bytes=days, free=2 * GB), **healthy_kw(free_bytes=2 * GB, total_bytes=5 * GB))
    m = res["metrics"]
    assert m["closed_day_mean_bytes"] == pytest.approx(1.9 * GB)
    assert m["volume_holds_retention"] is False and m["volume_needed_for_retention_bytes"] == pytest.approx(3 * 1.9 * GB * RH.HOURLY_PASS_FACTOR + 512 * MIB, rel=1e-6)
    assert "VOLUME_BELOW_RETENTION_NEED" in codes(res) and res["state"] == "degraded"
    # the same measured rate on a 10 GB volume holds retention 2
    ok = RH.classify(status(segment_bytes=days, free=6 * GB), **healthy_kw(free_bytes=6 * GB, total_bytes=10 * GB))
    assert ok["metrics"]["volume_holds_retention"] is True and "VOLUME_BELOW_RETENTION_NEED" not in codes(ok)


def test_a_blocked_volume_that_cannot_hold_retention_says_that_freeing_a_day_will_not_fix_it():
    days = seg({"20261111": 1.9 * GB, "20261112": 1.9 * GB, "20261113": 1.9 * GB, "20261114": 0.4 * GB})
    res = RH.classify(status(segment_bytes=days, free=509 * MIB), **healthy_kw(free_bytes=509 * MIB, total_bytes=5 * GB))
    assert res["state"] == "storage_blocked"
    assert any(r["code"] == "VOLUME_BELOW_RETENTION_NEED" and "freeing a day does not fix it" in r["detail"] for r in res["reasons"])


def test_a_fresh_heartbeat_with_no_growth_is_stalled_and_dead_feed_threads_are_stalled():
    assert RH.classify(status(growth_age=900.0), **healthy_kw())["state"] == "stalled"
    assert "NO_GROWTH" in codes(RH.classify(status(growth_age=900.0), **healthy_kw()))
    dead = RH.classify(status(threads=False), **healthy_kw())
    assert dead["state"] == "stalled" and "FEED_THREADS_DEAD" in codes(dead)
    fault = RH.classify(status(fault="OperationalError: disk I/O error"), **healthy_kw())
    assert fault["state"] == "stalled" and "STORE_FAULT" in codes(fault)


def test_a_silent_status_with_no_recorder_process_is_stalled_and_without_that_evidence_unknown():
    gone = RH.classify(status(age=3600.0), **healthy_kw(supervisor={"recorder_pid": None, "recorder_restarts": 4, "recorder_last_exit": 1}))
    assert gone["state"] == "stalled" and "RECORDER_PROCESS_NOT_RUNNING" in codes(gone)
    silent = RH.classify(status(age=3600.0), **healthy_kw())
    assert silent["state"] == "unknown" and "STATUS_STALE" in codes(silent), "the process may be hung or alive: the evidence cannot say"


@pytest.mark.parametrize("prov,expect", [({"status": "MISMATCH", "detail": "49 files differ"}, "IMAGE_MISMATCH"), ({"status": "MISSING"}, "IMAGE_MISSING")])
def test_an_unverified_image_is_image_mismatch_even_when_the_recorder_writes_fine(prov, expect):
    res = RH.classify(status(), **healthy_kw(provenance=prov))
    assert res["state"] == "image_mismatch" and expect in codes(res)


def test_precedence_is_storage_blocked_then_stalled_then_image_mismatch_then_unknown_then_degraded():
    both = RH.classify(status(growth_age=900.0, free=300 * MIB), **healthy_kw(free_bytes=300 * MIB, provenance={"status": "MISMATCH"}))
    assert both["state"] == "storage_blocked" and {"FREE_BELOW_RESERVE", "IMAGE_MISMATCH"} <= codes(both)
    stalled_and_image = RH.classify(status(growth_age=900.0), **healthy_kw(provenance={"status": "MISMATCH"}))
    assert stalled_and_image["state"] == "stalled" and "IMAGE_MISMATCH" in codes(stalled_and_image)
    image_and_unknown = RH.classify(status(), **healthy_kw(provenance={"status": "MISMATCH"}, supervisor=None))
    assert image_and_unknown["state"] == "image_mismatch"
    degraded = RH.classify(status(restarts=2), **healthy_kw(supervisor={"recorder_pid": 7, "recorder_restarts": 2, "recorder_watchdog_kills": 0}))
    assert degraded["state"] == "degraded" and "SUPERVISOR_RESTARTS" in codes(degraded)


def test_degraded_when_close_to_the_reserve_or_projected_to_reach_it_within_a_day():
    near = RH.classify(status(free=700 * MIB), **healthy_kw(free_bytes=700 * MIB))
    assert near["state"] == "degraded" and "WITHIN_HEADROOM_OF_RESERVE" in codes(near)
    days = seg({"20261112": 2.0 * GB, "20261113": 2.0 * GB, "20261114": 1.4 * GB})
    soon = RH.classify(status(segment_bytes=days, free=int(1.5 * GB) + 512 * MIB), **healthy_kw(free_bytes=int(1.5 * GB) + 512 * MIB, total_bytes=20 * GB))
    assert soon["state"] == "degraded" and "RESERVE_WITHIN_HOURS" in codes(soon), soon["metrics"]["hours_to_reserve_no_prune"]


def test_a_figure_the_evidence_cannot_give_is_none_never_zero():
    res = RH.classify(status(segment_bytes=None), now_s=NOW, free_bytes=None, total_bytes=None, supervisor=None, provenance=VERIFIED)
    m = res["metrics"]
    assert m["closed_day_mean_bytes"] is None and m["hours_to_reserve_no_prune"] is None and m["volume_holds_retention"] is None and m["volume_total_bytes"] is None
    assert m["free_source"] == "recorder_status" and m["free_bytes"] == 4 * GB, "the recorder's own last figure is used only when the volume cannot be read, and labelled"
    none_at_all = RH.classify(None, now_s=NOW, status_read_error="FileNotFoundError")
    assert none_at_all["state"] == "unknown" and none_at_all["metrics"]["free_bytes"] is None and none_at_all["metrics"]["last_write_age_s"] is None
    assert any("FileNotFoundError" in r["detail"] for r in none_at_all["reasons"])


def test_a_crafted_status_cannot_make_the_classifier_raise_or_report_healthy():
    for junk in ({}, {"now_ms": "x", "store": "y", "progress": 3}, {"now_ms": float("nan")}, {"now_ms": NOW * 1000, "store": {"min_free_bytes": float("inf"), "free_bytes": -1}, "feed": []}):
        res = RH.classify(junk, **healthy_kw())
        assert res["state"] != "healthy" and res["state"] in RH.STATES
    assert RH.classify(["not", "a", "mapping"], **healthy_kw())["state"] == "unknown"


def test_every_state_name_is_in_the_closed_vocabulary_and_healthy_helper_matches():
    assert set(RH.STATES) == {"healthy", "degraded", "stalled", "storage_blocked", "image_mismatch", "unknown"}
    ok_days = seg({"20261112": 1.0 * GB, "20261113": 1.0 * GB, "20261114": 0.4 * GB})
    assert RH.is_healthy(RH.classify(status(segment_bytes=ok_days, free=8 * GB), **healthy_kw())) is True
    assert RH.is_healthy(RH.classify(status(segment_bytes=None, free=8 * GB), **healthy_kw())) is False        # no capacity evidence: not healthy (Round 22 review)
    assert RH.is_healthy({"state": "unknown"}) is False


def test_a_stalled_store_warns_that_resuming_will_prune_the_retained_evidence_days():
    """REPRODUCED (Round 22): the newest segment of a stalled store is old, so it shields every retained day; the first write opens today's segment and the retention pass then removes every closed day
    before today - retention. The owner must be told the bytes and days BEFORE extending the volume."""
    days = seg({"20261107": 1.9 * GB, "20261108": 1.9 * GB, "20261109": 0.6 * GB})
    res = RH.classify(status(segment_bytes=days, free=509 * MIB, retention=2), **healthy_kw(free_bytes=509 * MIB, total_bytes=5 * GB))
    m = res["metrics"]
    assert m["prune_on_resume_days"] == 3 and m["prune_on_resume_day_list"] == ["20261107", "20261108", "20261109"]
    assert m["prune_on_resume_bytes"] == pytest.approx(4.4 * GB)
    assert "RESUME_WILL_PRUNE_RETAINED_DAYS" in codes(res) and res["state"] == "storage_blocked"
    # a healthy writer whose newest day is today has nothing to warn about: the pass only ever drops what is past retention
    live = seg({"20261112": 1.9 * GB, "20261113": 1.9 * GB, "20261114": 0.7 * GB})
    ok = RH.classify(status(segment_bytes=live, free=6 * GB, retention=2), **healthy_kw(free_bytes=6 * GB, total_bytes=10 * GB))
    assert ok["metrics"]["prune_on_resume_days"] == 0 and "RESUME_WILL_PRUNE_RETAINED_DAYS" not in codes(ok)
    # unknown retention / sizes: nothing is claimed
    assert RH.prune_on_resume(None, 2, NOW)["days"] is None and RH.prune_on_resume(days, 0, NOW)["days"] is None


def test_retention_1_with_two_days_on_disk_still_has_a_labelled_daily_rate():
    days = seg({"20261113": 1.9 * GB, "20261114": 0.7 * GB})
    res = RH.classify(status(segment_bytes=days, free=2 * GB, retention=1), **healthy_kw(free_bytes=2 * GB, total_bytes=5 * GB))
    m = res["metrics"]
    assert m["closed_day_mean_bytes"] == pytest.approx(1.9 * GB) and m["closed_day_basis"] == "single_closed_day_lower_bound"
    assert m["volume_holds_retention"] is True          # (1+1) * 1.9 GB * 1.042 + 0.5 GiB < 5 GB
    assert RH.classify(status(segment_bytes=seg({"20261114": 0.7 * GB})), **healthy_kw())["metrics"]["closed_day_basis"] is None


def test_a_status_that_cannot_establish_headroom_or_capacity_is_never_healthy():
    """REPRODUCED (review): a fresh status with growth but no store block, an unreadable volume and a VERIFIED image was `healthy` with no reasons; free known but reserve unknown was healthy too."""
    ok_days = seg({"20261112": 1.0 * GB, "20261113": 1.0 * GB, "20261114": 0.4 * GB})
    bare = status(segment_bytes=ok_days)
    bare["store"] = {}
    res = RH.classify(bare, now_s=NOW, free_bytes=None, total_bytes=None, supervisor=None, provenance=VERIFIED)
    assert res["state"] != "healthy" and "HEADROOM_UNKNOWN" in codes(res)
    no_reserve = status(segment_bytes=ok_days, free=8 * GB)
    no_reserve["store"].pop("min_free_bytes")
    res2 = RH.classify(no_reserve, **healthy_kw())
    assert res2["state"] != "healthy" and "HEADROOM_UNKNOWN" in codes(res2)
    no_retention = status(segment_bytes=ok_days, free=8 * GB, retention=None)
    res3 = RH.classify(no_retention, **healthy_kw())
    assert res3["state"] != "healthy" and "CAPACITY_NOT_REPORTED" in codes(res3)
