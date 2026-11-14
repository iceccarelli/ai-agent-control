"""recorder_health.py -- ONE pure classification of the market recorder's health, in a closed vocabulary, with the numbers behind it.

    healthy | degraded | stalled | storage_blocked | image_mismatch | unknown

No I/O, no clock, no network: the caller (``runner_monitor.recorder_observation``) reads the recorder's own status file, the volume (``statvfs``), the supervisor's counters and the
runtime provenance, and hands them in. The classification is FAIL-CLOSED: ``healthy`` needs fresh, readable, growing, unblocked evidence AND a verified image. Anything the inputs
cannot establish is ``unknown`` (or a worse named state), never ``healthy``:

  * no status, an unreadable status, a status older than ``STALE_S``, a status stamped in the future  -> never healthy (a stale tail is a recorder that stopped reporting, not a healthy one)
  * volume free space below the write reserve (the store refuses every write)                          -> ``storage_blocked``
  * a fresh heartbeat whose log has not grown, dead feed threads, or a store fault                    -> ``stalled``
  * the running image's provenance missing, unverifiable or different from the approved one            -> ``image_mismatch`` (the recorder may be writing; it is not approved to be the recorder)
  * writing, but within one alert-headroom of the reserve, projected to reach it soon, a volume that cannot hold the retention, supervisor restarts, heartbeat errors -> ``degraded``

Precedence when several hold: storage_blocked > stalled > image_mismatch > unknown > degraded > healthy; ``reasons`` lists EVERY condition found, so none is hidden by another.
The metrics (``metrics``) are the numbers an owner or a watcher needs: last durable write and commit and their ages, record-count progression, effective reserve and retention, volume
capacity, bytes remaining above the reserve, the measured daily rate, estimated hours to the reserve and whether the volume holds the retention, plus process, recorder and image identity.
A figure the evidence cannot give is ``None`` (UNKNOWN), never 0.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional

SCHEMA = "recorder_health/v1"
STATES = ("healthy", "degraded", "stalled", "storage_blocked", "image_mismatch", "unknown")
#: precedence (index = rank; the LOWEST index wins when several conditions hold)
_PRECEDENCE = ("storage_blocked", "stalled", "image_mismatch", "unknown", "degraded", "healthy")

#: a recorder status older than this has stopped reporting (it writes every 30 s: ten missed beats)
STALE_S = 300.0
#: a status stamped further than this ahead of the reader's clock is not a fresh heartbeat
FUTURE_SKEW_S = 120.0
#: a fresh heartbeat whose log has not grown for this long is alive and not persisting (BTCUSDT books update many times a second)
NO_GROWTH_S = 300.0
#: free space above the reserve below which the recorder is "degraded" (one alert headroom, 1 GiB; the early-warning alert uses the same figure)
HEADROOM_BYTES = 1024 * 1024 * 1024
#: estimated time to reach the reserve below which the recorder is "degraded"
MIN_HOURS_TO_RESERVE = 24.0
#: the hourly retention pass can leave an expired day on disk for up to an hour while the new day starts (md_store; the size model's 1 + 1/24)
HOURLY_PASS_FACTOR = 1 + 1 / 24
DAY_S = 86_400.0

IMAGE_OK = "VERIFIED"
IMAGE_STATES = ("VERIFIED", "MISMATCH", "MISSING", "NOT_CHECKED")


def _num(x: Any) -> Optional[float]:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _daily_rate_bytes(segment_bytes: Optional[Mapping[str, Any]], now_s: float) -> Dict[str, Optional[float]]:
    """The measured daily write rate from the segment sizes: the mean of the fully CLOSED days (oldest and newest excluded when there are at least three: the oldest may be partial and the newest is
    open), and today's rate in bytes per second from the open day's size over the seconds elapsed in the UTC day. Any of them is None when the evidence does not support it."""
    out: Dict[str, Any] = {"closed_day_mean_bytes": None, "today_bytes": None, "today_bytes_per_s": None, "closed_day_basis": None}
    if not isinstance(segment_bytes, Mapping) or not segment_bytes:
        return out
    days = sorted(d for d in segment_bytes if isinstance(d, str) and len(d) == 8 and d.isdigit() and _num(segment_bytes[d]) is not None)
    if not days:
        return out
    # With retention 1 the store holds exactly two days (one closed, one open), so "oldest and newest excluded" would leave NO closed day and the capacity model would say nothing at the very
    # setting that fits the smallest volume. A single older day is then used as a LOWER BOUND on the daily rate (it may be a partial day) and labelled so: never presented as a mean of full days.
    if len(days) >= 3:
        closed, out["closed_day_basis"] = days[1:-1], "mean_of_closed_days"
    elif len(days) == 2:
        closed, out["closed_day_basis"] = days[:1], "single_closed_day_lower_bound"
    else:
        closed = []
    if closed:
        out["closed_day_mean_bytes"] = sum(float(segment_bytes[d]) for d in closed) / len(closed)
    import datetime as dt
    today = dt.datetime.fromtimestamp(now_s, dt.timezone.utc).strftime("%Y%m%d")
    if days[-1] == today:
        elapsed = now_s - dt.datetime.strptime(today, "%Y%m%d").replace(tzinfo=dt.timezone.utc).timestamp()
        out["today_bytes"] = float(segment_bytes[today])
        if elapsed >= 1800.0:                                      # under half an hour into the day the ratio is noise
            out["today_bytes_per_s"] = float(segment_bytes[today]) / elapsed
    return out


def prune_on_resume(segment_bytes: Optional[Mapping[str, Any]], retention_days: Optional[float], now_s: float) -> Dict[str, Any]:
    """What the FIRST write after the volume recovers would delete. The store prunes whole closed days older than ``retention_days`` against the wall clock, bounded by its newest segment; a stalled
    store never opened today's segment, so its newest segment is old and shields every retained day. The first successful write opens today's segment, the bound jumps to the wall clock and every closed
    day before (today - retention) goes at the next retention pass, however valuable it is as evidence (REPRODUCED Round 22: R=2 on a stalled store whose newest day is 5 days old). Pure and read-only:
    ``days``/``bytes`` are what would be removed, ``None`` when the sizes or the retention are unknown."""
    out: Dict[str, Any] = {"days": None, "bytes": None, "day_list": None}
    r = _num(retention_days)
    if r is None or r <= 0 or not isinstance(segment_bytes, Mapping):
        return out
    import datetime as dt
    cutoff = dt.datetime.fromtimestamp(now_s - r * DAY_S, dt.timezone.utc).strftime("%Y%m%d")
    gone = sorted(d for d in segment_bytes if isinstance(d, str) and len(d) == 8 and d.isdigit() and d < cutoff and _num(segment_bytes[d]) is not None)
    out["day_list"], out["days"], out["bytes"] = gone, len(gone), int(sum(float(segment_bytes[d]) for d in gone))
    return out


def classify(status: Optional[Mapping[str, Any]], *, now_s: float, free_bytes: Optional[int] = None, total_bytes: Optional[int] = None,
             supervisor: Optional[Mapping[str, Any]] = None, provenance: Optional[Mapping[str, Any]] = None, status_read_error: Optional[str] = None) -> Dict[str, Any]:
    """Classify the recorder. ``status``: the parsed ``recorder_status.json`` (None when absent or unreadable; ``status_read_error`` says why). ``free_bytes`` / ``total_bytes``: the volume read NOW (statvfs; the
    status file's own figure is used only when this is absent, and labelled). ``supervisor``: ``supervisor_status.json``. ``provenance``: ``{"status": VERIFIED|MISMATCH|MISSING|NOT_CHECKED, ...}`` from the runtime provenance check."""
    reasons: List[Dict[str, str]] = []
    states: List[str] = []

    def add(state: str, code: str, detail: str) -> None:
        states.append(state)
        reasons.append({"state": state, "code": code, "detail": detail})

    st = status if isinstance(status, Mapping) else None
    store = (st or {}).get("store") if isinstance((st or {}).get("store"), Mapping) else {}
    progress = (st or {}).get("progress") if isinstance((st or {}).get("progress"), Mapping) else {}
    feed = (st or {}).get("feed") if isinstance((st or {}).get("feed"), Mapping) else {}
    hb = (st or {}).get("heartbeat") if isinstance((st or {}).get("heartbeat"), Mapping) else {}
    sup = supervisor if isinstance(supervisor, Mapping) else {}

    # ---- what the volume says (read NOW), falling back to the recorder's own last figure
    free_src = "statvfs"
    free = _num(free_bytes)
    if free is None:
        free, free_src = _num(store.get("free_bytes")), "recorder_status"
    if free is None:
        free_src = "unavailable"
    reserve = _num(store.get("min_free_bytes"))
    retention = _num(store.get("retention_days"))
    total = _num(total_bytes)

    # ---- freshness of the evidence
    now_ms = _num((st or {}).get("now_ms"))
    age = None if now_ms is None else now_s - now_ms / 1000.0
    stale = st is not None and (age is None or age > STALE_S)
    future = st is not None and age is not None and age < -FUTURE_SKEW_S
    growth_age = _num(progress.get("growth_age_s"))
    if st is None:
        add("unknown", "STATUS_UNREADABLE", f"recorder_status.json is absent or unreadable ({status_read_error or 'no reason given'}): the recorder's health cannot be read")
    elif future:
        add("unknown", "STATUS_FROM_THE_FUTURE", f"the status is stamped {-age:.0f} s ahead of this clock (tolerance {FUTURE_SKEW_S:.0f} s): a clock step or a frozen file is not a heartbeat")
    elif stale:
        add("unknown", "STATUS_STALE", "the status carries no timestamp" if age is None else f"the status is {age:.0f} s old (limit {STALE_S:.0f} s): the recorder has stopped reporting")

    # ---- storage
    blocked = False
    if free is not None and reserve is not None and free < reserve:
        blocked = True
        add("storage_blocked", "FREE_BELOW_RESERVE", f"{free / 1048576.0:.0f} MiB free ({free_src}) < the {reserve / 1048576.0:.0f} MiB write reserve: the store refuses every write")
    elif store.get("fault") and "reserve" in str(store.get("fault")) and free is None:
        blocked = True
        add("storage_blocked", "STORE_REPORTS_RESERVE_FAULT", str(store.get("fault"))[:200])

    # ---- what cannot be established is never healthy (Round 22 review: a status with no store block and an unreadable volume was `healthy`)
    if st is not None and not blocked and (free is None or reserve is None):
        add("unknown", "HEADROOM_UNKNOWN", f"free space ({free_src}) or the write reserve is not known: the headroom above the reserve cannot be established")

    # ---- is it persisting? (only a FRESH status can say)
    fresh = st is not None and not stale and not future
    if fresh and not blocked:
        if not feed.get("threads_alive", False):
            add("stalled", "FEED_THREADS_DEAD", "the recorder's feed threads are not alive")
        if growth_age is not None and growth_age > NO_GROWTH_S:
            add("stalled", "NO_GROWTH", f"the heartbeat is fresh but no market record was persisted for {growth_age:.0f} s (limit {NO_GROWTH_S:.0f} s)")
        if growth_age is None:
            add("unknown", "GROWTH_UNKNOWN", "the status carries no growth figure: persistence cannot be established")
        if store.get("fault"):
            add("stalled", "STORE_FAULT", str(store.get("fault"))[:200])
    if st is not None and (stale or future) and not blocked:
        if sup and not sup.get("recorder_pid"):
            add("stalled", "RECORDER_PROCESS_NOT_RUNNING", f"the status is not fresh and the supervisor reports no recorder process (restarts {sup.get('recorder_restarts')}, last exit {sup.get('recorder_last_exit')})")

    # ---- image provenance: healthy requires VERIFIED
    prov_status = str((provenance or {}).get("status")) if isinstance(provenance, Mapping) else "NOT_CHECKED"
    if prov_status not in IMAGE_STATES:
        prov_status = "NOT_CHECKED"
    if prov_status in ("MISMATCH", "MISSING"):
        add("image_mismatch", f"IMAGE_{prov_status}", str((provenance or {}).get("detail") or "the running image's provenance does not establish the approved recorder implementation")[:240])
    elif prov_status == "NOT_CHECKED":
        add("unknown", "IMAGE_NOT_CHECKED", "no runtime provenance was supplied: the running image is not established as the approved one, so the recorder cannot be called healthy")

    # ---- projections and capacity (need the sizes)
    rates = _daily_rate_bytes(store.get("segment_bytes"), now_s)
    resume = prune_on_resume(store.get("segment_bytes"), retention, now_s)
    rate_day = rates["closed_day_mean_bytes"]
    rate_s = rates["today_bytes_per_s"]
    if rate_s is None and rate_day is not None:
        rate_s = rate_day / DAY_S
    elif rate_s is not None and rate_day is not None:
        rate_s = max(rate_s, rate_day / DAY_S)                                  # the larger of the two: an optimistic projection is the dangerous one
    remaining = None if free is None or reserve is None else max(0.0, free - reserve)
    hours_to_reserve = None if remaining is None or not rate_s else round(remaining / rate_s / 3600.0, 1)
    fits = None
    needed = None
    if rate_day is not None and retention is not None and reserve is not None and total is not None:
        needed = (retention + 1) * rate_day * HOURLY_PASS_FACTOR + reserve
        fits = total >= needed
    if st is not None and (retention is None or not isinstance(store.get("segment_bytes"), Mapping) or not store.get("segment_bytes")):
        add("degraded", "CAPACITY_NOT_REPORTED", "the status carries no retention or per-day segment sizes (an older recorder, or an empty store): the daily rate, the time to the reserve and whether the volume holds the retention cannot be computed")
    if not blocked:
        if remaining is not None and reserve is not None and remaining < HEADROOM_BYTES:
            add("degraded", "WITHIN_HEADROOM_OF_RESERVE", f"{remaining / 1048576.0:.0f} MiB above the reserve (headroom {HEADROOM_BYTES / 1048576.0:.0f} MiB)")
        if hours_to_reserve is not None and hours_to_reserve < MIN_HOURS_TO_RESERVE:
            add("degraded", "RESERVE_WITHIN_HOURS", f"at the current rate the reserve is reached in about {hours_to_reserve:.1f} h (limit {MIN_HOURS_TO_RESERVE:.0f} h), before any pruning")
        if fits is False:
            add("degraded", "VOLUME_BELOW_RETENTION_NEED", f"the volume ({total / 1e9:.2f} GB) is below what retention {retention:.0f} needs at the measured rate ({needed / 1e9:.2f} GB incl. the reserve)")
    else:
        if fits is False:
            reasons.append({"state": "storage_blocked", "code": "VOLUME_BELOW_RETENTION_NEED", "detail": f"the volume ({total / 1e9:.2f} GB) is below what retention {retention:.0f} needs at the measured rate ({needed / 1e9:.2f} GB incl. the reserve): freeing a day does not fix it"})
    if resume["days"] and (blocked or "stalled" in states or (st is not None and (stale or future))):
        add("storage_blocked" if blocked else ("stalled" if "stalled" in states else "unknown"), "RESUME_WILL_PRUNE_RETAINED_DAYS",
            f"when writing resumes the store opens today's segment and its retention pass removes {resume['days']} closed day(s) ({resume['bytes'] / 1e9:.2f} GB: {', '.join(resume['day_list'])}) older than retention {retention:.0f} d: preserve or back them up BEFORE freeing space")
    restarts = _num(sup.get("recorder_restarts")) if sup else None
    kills = _num(sup.get("recorder_watchdog_kills")) if sup else None
    if (restarts or 0) > 0 or (kills or 0) > 0:
        add("degraded", "SUPERVISOR_RESTARTS", f"the supervisor restarted the recorder {int(restarts or 0)} time(s), {int(kills or 0)} watchdog kill(s)")
    if (_num(hb.get("status_errors")) or 0) > 0 or (_num(hb.get("maintenance_errors")) or 0) > 0:
        add("degraded", "HEARTBEAT_OR_MAINTENANCE_ERRORS", f"status errors {hb.get('status_errors')}, maintenance errors {hb.get('maintenance_errors')}")

    state = "healthy"
    for cand in _PRECEDENCE:
        if cand in states:
            state = cand
            break
    last_write_ms = _num(progress.get("last_growth_ms"))
    last_commit_ms = _num(store.get("last_commit_ms"))
    counted_ms = _num(store.get("records_counted_ms"))
    metrics = {
        "last_write_ms": last_write_ms, "last_write_age_s": None if last_write_ms is None else round(now_s - last_write_ms / 1000.0, 1),
        "last_commit_ms": last_commit_ms, "last_commit_age_s": None if last_commit_ms is None else round(now_s - last_commit_ms / 1000.0, 1),
        "status_age_s": None if age is None else round(age, 1),
        "records": _num(store.get("records")), "records_counted_age_s": None if counted_ms is None else round(now_s - counted_ms / 1000.0, 1),
        "market_inputs": _num(progress.get("market_inputs")), "growth_age_s": growth_age,
        "effective_reserve_bytes": reserve, "effective_retention_days": retention,
        "volume_total_bytes": total, "free_bytes": free, "free_source": free_src, "bytes_remaining_above_reserve": remaining,
        "closed_day_mean_bytes": rate_day, "closed_day_basis": rates["closed_day_basis"], "today_bytes": rates["today_bytes"],
        "prune_on_resume_days": resume["days"], "prune_on_resume_bytes": resume["bytes"], "prune_on_resume_day_list": resume["day_list"], "write_rate_bytes_per_s": None if rate_s is None else round(rate_s, 3),
        "hours_to_reserve_no_prune": hours_to_reserve, "volume_needed_for_retention_bytes": None if needed is None else int(needed), "volume_holds_retention": fits,
        "process": {"pid": (st or {}).get("pid"), "started_ms": (st or {}).get("started_ms"), "uptime_s": (st or {}).get("uptime_s"),
                    "supervisor_recorder_pid": sup.get("recorder_pid") if sup else None, "restarts": restarts, "watchdog_kills": kills},
        "recorder": {"schema": (st or {}).get("schema"), "environment": (st or {}).get("environment"), "symbol": (st or {}).get("symbol")},
        "image": {"status": prov_status, **({k: provenance.get(k) for k in ("git_sha", "recorder_fingerprint", "image_ref", "environment_class", "detail") if k in provenance} if isinstance(provenance, Mapping) else {})},
    }
    return {"schema": SCHEMA, "state": state, "reasons": reasons, "metrics": metrics}


def is_healthy(result: Mapping[str, Any]) -> bool:
    return result.get("state") == "healthy"
