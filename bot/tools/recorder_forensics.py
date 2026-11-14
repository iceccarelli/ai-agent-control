#!/usr/bin/env python3
"""recorder_forensics.py -- OFFLINE analysis of what the owner captured from the carry-drill machine. It never connects to Fly, a venue or Supabase and never writes into the evidence.

A running recorder process is not proof that it is writing; a supervisor with zero restarts is not proof that the data chain is healthy; a pruned segment is not proof that a backup exists. This tool
turns the owner's READ-ONLY captures into a verdict in which every statement carries its status: OBSERVED (read from a capture), INFERRED (derived by a stated rule from observations and, where
needed, from named assumptions) or UNKNOWN (the capture that would decide it is missing). Nothing is assumed healthy because it is silent.

    python3 tools/recorder_forensics.py commands                          # prints the read-only capture commands for the owner to run
    python3 tools/recorder_forensics.py analyse --evidence DIR [--receipts R.jsonl --backup B_DIR --manifest M.jsonl] [--now-ms N]

Evidence directory (every file optional; a missing one makes what it would decide UNKNOWN): df.txt, ls.txt, pruned.jsonl, recorder_status.json, supervisor_status.json, monitor_tail.jsonl, proc.txt,
image_actual.txt. ``--receipts / --backup / --manifest`` are the OWNER'S off-volume copies (md_seal's outside memory); the tool only reads them.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, HERE)

MIB = 1024 * 1024
DAY_MS = 86_400_000
#: from the recorder's own policy: free space below this is the write reserve (the image CMD passes --recorder-min-free-mb 512; used only when the process list cannot say).
DOCUMENTED_RESERVE_MIB = 512
#: the alert-free headroom above the reserve (runner_alerts: RECORDER_FREE_WARN_MB - RECORDER_RESERVE_MB).
ALERT_HEADROOM_MIB = 1024
#: the hourly retention pass can leave an expired day on disk for up to an hour while the new day starts (md_store; the size model's 1 + 1/24).
HOURLY_PASS_FACTOR = 1 + 1 / 24

#: a monitor tail older than this many hours at capture time says nothing about the present (the monitor writes about hourly: three missed intervals).
MONITOR_STALE_H = 3.0
#: the recorder caches its record count for 600 s (market_recorder count_every_s): a flat count over less than two cache periods can be the cache, not a stall.
NOT_WRITING_MIN_SPAN_MIN = 20.0

CAPTURE_COMMANDS = (
    ("df.txt", 'fly ssh console -a carry-drill -C "df -B1 /data"'),
    ("ls.txt", 'fly ssh console -a carry-drill -C "ls -l --time-style=full-iso /data/market"'),
    ("pruned.jsonl", 'fly ssh console -a carry-drill -C "cat /data/market/pruned.jsonl"'),
    ("recorder_status.json", 'fly ssh console -a carry-drill -C "cat /data/market/recorder_status.json"'),
    ("supervisor_status.json", 'fly ssh console -a carry-drill -C "cat /data/market/supervisor_status.json"'),
    ("monitor_tail.jsonl", 'fly ssh console -a carry-drill -C "tail -n 400 /data/monitor/monitor.jsonl"'),
    ("proc.txt", 'fly ssh console -a carry-drill -C "sh -c \'cat /proc/[0-9]*/cmdline | tr \\"\\\\000\\" \\"\\\\n\\"\'"'),
    ("image_actual.txt", "python3 bot/tools/image_manifest.py command      # prints ONE read-only sha256sum command: run it, save its output"),
)


def commands_text() -> str:
    lines = ["# READ-ONLY captures for tools/recorder_forensics.py (the owner runs them; nothing here writes, restarts, resizes or deletes). Save each output as the named file in ONE directory.",
             "# `ps` is absent from the drill image: /proc is read instead (all process command lines, one argument per line)."]
    for name, cmd in CAPTURE_COMMANDS:
        lines.append(f"{cmd} > {name}" if not cmd.startswith("python3") else f"# {name}: {cmd}")
    return "\n".join(lines) + "\n"


def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _json_lines(text: Optional[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


# ------------------------------------------------------------------------------------------ parsers (pure)

_DF_ROW = re.compile(r"(\d+)\s+(\d+)\s+(\d+)\s+\d+%\s+(/\S*)\s*$", re.M)


def parse_df(text: Optional[str]) -> Optional[Dict[str, int]]:
    """``df -B1 /data``: size, used, available in BYTES, read from the RIGHT of the row (``size used avail N% mount``) and only when the mount column is exactly ``/data``. ``available`` is f_bavail x f_frsize,
    exactly what the store's reserve check reads (md_store.free_bytes). A preamble line, a wrapped device name or the df of another mount is never read as the volume: it is UNKNOWN."""
    if not text:
        return None
    rows = [m for m in _DF_ROW.finditer(text) if m.group(4) == "/data"]
    if not rows:
        return None
    m = rows[-1]
    size, used, avail = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if size <= 0 or used + avail > size * 1.05:
        return None
    return {"size_bytes": size, "used_bytes": used, "available_bytes": avail}


_LS = re.compile(r"^\S+\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})(?:\.\d+)?\s+([+-]\d{4})\s+(\S.*)$")
_SEG = re.compile(r"^md-(\d{8})\.db(-wal|-shm)?$")


def parse_ls(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """``ls -l --time-style=full-iso``: per-day segment bytes (db + wal + shm) and every other file's bytes."""
    if not text:
        return None
    days: Dict[str, int] = {}
    other: Dict[str, int] = {}
    mtimes: Dict[str, str] = {}
    parsed = 0
    has_total = any(ln.strip().startswith("total ") for ln in text.splitlines())
    for ln in text.splitlines():
        m = _LS.match(ln.strip())
        if not m:
            continue
        parsed += 1
        size, date, clock, _tz, name = int(m.group(1)), m.group(2), m.group(3), m.group(4), m.group(5).strip()
        s = _SEG.match(name)
        if s:
            days[s.group(1)] = days.get(s.group(1), 0) + size
            if not s.group(2):
                mtimes[s.group(1)] = f"{date}T{clock}Z"
        else:
            other[name] = size
    if not parsed and not has_total:
        return None                 # an error message ("ls: cannot access ...") is not an empty directory
    return {"segment_bytes_by_day": dict(sorted(days.items())), "last_modified_utc_by_day": mtimes, "other_bytes": sum(other.values()), "other_files": sorted(other)}


def parse_proc(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """The /proc command lines, one argument per line (or NUL-separated): which recorder-side scripts appear and the flags that matter."""
    if not text:
        return None
    toks = [t for t in re.split(r"[\x00\n]+", text) if t != ""]
    names = {"market_recorder.py": "recorder", "runner_supervisor.py": "supervisor", "runner_monitor.py": "monitor"}
    present = {role: any(t.endswith(script) for t in toks) for script, role in names.items()}

    def flag(name: str) -> Optional[str]:
        for i, t in enumerate(toks):
            if t == name and i + 1 < len(toks):
                return toks[i + 1]
            if t.startswith(name + "="):
                return t.split("=", 1)[1]
        return None
    return {"processes_present": present, "retention_days": flag("--recorder-retention-days"), "min_free_mb": flag("--recorder-min-free-mb"), "n_tokens": len(toks),
            "order_capable_words": sorted({w for w in ("ms_testnet_autopilot.py", "ms_testnet_session.py", "drill.py", "unattended_drill.py", "carry_book") if any(w in t for t in toks)})}


def _int(x: Any) -> Optional[int]:
    return int(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def records_series(history: Sequence[Dict[str, Any]]) -> List[Tuple[str, int, Optional[str]]]:
    return [(str(r.get("at")), _int(r.get("recorder_records")), r.get("recorder_state")) for r in history if _int(r.get("recorder_records")) is not None]  # type: ignore[misc]


def _utc_text(ms: Any) -> Optional[str]:
    """A UTC timestamp for an epoch-millisecond value, or None when it is absent or not a representable time (crafted input is UNKNOWN, never an exception)."""
    v = _int(ms)
    if not v:
        return None
    try:
        return dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def _utc_ms(at: str) -> Optional[int]:
    try:
        return int(dt.datetime.strptime(at[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    except ValueError:
        return None


# ------------------------------------------------------------------------------------------ the analysis

def analyse(evidence: str, *, now_ms: Optional[int] = None, receipts: Optional[str] = None, backup: Optional[str] = None, manifest: Optional[str] = None, root: str = BOT) -> Dict[str, Any]:
    import time
    now_basis = "given"
    if now_ms is None:
        mt = [os.path.getmtime(os.path.join(evidence, f)) for f in os.listdir(evidence) if os.path.isfile(os.path.join(evidence, f))] if os.path.isdir(evidence) else []
        now_ms, now_basis = (int(max(mt) * 1000), "capture time (the newest evidence file's mtime)") if mt else (int(time.time() * 1000), "wall clock (no evidence files)")
    else:
        now_ms = int(now_ms)
    ob: Dict[str, Any] = {}
    inf: List[Dict[str, Any]] = []
    unknown: List[str] = []
    df = parse_df(_read(os.path.join(evidence, "df.txt")))
    ls = parse_ls(_read(os.path.join(evidence, "ls.txt")))
    proc = parse_proc(_read(os.path.join(evidence, "proc.txt")))
    pruned = _json_lines(_read(os.path.join(evidence, "pruned.jsonl")))
    history = _json_lines(_read(os.path.join(evidence, "monitor_tail.jsonl")))
    rec_status = next(iter(_json_lines(_read(os.path.join(evidence, "recorder_status.json")))), None)
    if rec_status is None:
        try:
            rec_status = json.loads(_read(os.path.join(evidence, "recorder_status.json")) or "null")
        except ValueError:
            rec_status = None

    # ---- reserve and retention: the process's own flags when captured, otherwise the documented value labelled as such
    reserve_mib: Optional[float] = None
    reserve_src = "UNKNOWN"
    if proc and proc["min_free_mb"] is not None:
        try:
            v = float(proc["min_free_mb"])
            if math.isfinite(v) and v > 0:
                reserve_mib, reserve_src = v, "OBSERVED from /proc command lines"
        except ValueError:
            pass
    if reserve_mib is None:
        reserve_mib, reserve_src = float(DOCUMENTED_RESERVE_MIB), "ASSUMED: the image CMD's documented value (no process list captured)"
        unknown.append("the reserve actually in force (capture proc.txt)")
    retention = None
    if proc and proc["retention_days"] is not None:
        try:
            retention = int(proc["retention_days"])
        except ValueError:
            retention = None
    if retention is None:
        unknown.append("the retention in force (--recorder-retention-days; capture proc.txt)")

    # ---- the volume
    if df:
        ob["volume"] = {**df, "reserve_mib": reserve_mib, "reserve_source": reserve_src, "free_mib": round(df["available_bytes"] / MIB, 1),
                        "free_below_reserve": df["available_bytes"] < reserve_mib * MIB, "source": "df.txt"}
    else:
        unknown.append("the volume's size, usage and free space (capture df.txt)")

    # ---- the segments and the measured daily rate
    days: Dict[str, int] = {}
    if ls:
        days = ls["segment_bytes_by_day"]
        ob["segments"] = {"bytes_by_day": days, "other_files_bytes": ls["other_bytes"], "other_files": ls["other_files"], "source": "ls.txt"}
        ordered = sorted(days)
        closed = ordered[1:-1] if len(ordered) >= 3 else []
        if closed:
            rate = sum(days[d] for d in closed) / len(closed)
            inf.append({"claim": "measured_daily_rate_bytes", "value": int(rate), "status": "INFERRED", "rule": f"mean on-disk size of the {len(closed)} fully closed day(s) {closed} (the oldest may be a partial first day and the newest is still open)"})
        else:
            unknown.append("a measured daily rate (needs at least three segment days on disk: the oldest may be partial and the newest is open)")
    else:
        unknown.append("the segments on disk and their sizes (capture ls.txt)")

    # ---- is it writing? (a process is not proof; a stale tail is not the present)
    series = records_series(history)
    writing: Dict[str, Any] = {"status": "UNKNOWN", "why": "no monitor history with recorder_records captured"}
    if not history:
        unknown.append("the monitor history (capture monitor_tail.jsonl)")
    if len(series) >= 3:
        last = series[-3:]
        counts = [c for _a, c, _s in last]
        t0, t1 = _utc_ms(last[0][0]), _utc_ms(last[-1][0])
        span_min = (t1 - t0) / 60000 if t0 is not None and t1 is not None else None
        age_h = (now_ms - t1) / 3_600_000 if t1 is not None else None
        if span_min is None or age_h is None:
            writing = {"status": "UNKNOWN", "why": "the monitor entries carry no readable timestamp"}
        elif age_h > MONITOR_STALE_H:
            writing = {"status": "UNKNOWN", "why": f"the newest monitor entry ({last[-1][0]}) is {age_h:.1f} h older than the capture: a stale tail is not evidence about the present (capture again, or the monitor itself is not running)"}
        elif len(set(counts)) == 1 and span_min >= NOT_WRITING_MIN_SPAN_MIN:
            writing = {"status": "NOT_WRITING", "why": f"recorder_records stayed at {counts[-1]} across the last 3 monitor entries ({span_min:.0f} min)", "basis": "OBSERVED"}
        elif counts[-1] > counts[0]:
            writing = {"status": "WRITING", "why": f"recorder_records grew {counts[0]} -> {counts[-1]} (newest entry {age_h:.1f} h before the capture)", "basis": "OBSERVED"}
        else:
            writing = {"status": "UNKNOWN", "why": f"recorder_records {counts} over {span_min:.0f} min is not conclusive (a flat count shorter than {NOT_WRITING_MIN_SPAN_MIN} min can be the recorder's own count cache)"}
    ob["writing"] = writing
    if not isinstance(rec_status, dict):
        unknown.append("the recorder's own status (capture recorder_status.json)")
    if isinstance(rec_status, dict):
        ob["recorder_status"] = {k: rec_status.get(k) for k in ("state", "store_fault", "free_bytes", "min_free_bytes", "emergency_prunes_denied") if k in rec_status}
    if proc:
        ob["processes"] = {**proc, "source": "proc.txt"}
        if proc["processes_present"].get("recorder") and writing["status"] == "NOT_WRITING":
            inf.append({"claim": "recorder_process_present_but_not_writing", "status": "INFERRED", "rule": "a recorder process appears in /proc AND the record count is frozen: presence is not progress"})
        if proc["order_capable_words"]:
            inf.append({"claim": "order_capable_words_in_process_list", "status": "OBSERVED", "value": proc["order_capable_words"], "rule": "carry-drill must be read-only: investigate before anything else"})
    else:
        unknown.append("the process list (capture proc.txt; `ps` is absent from the image)")

    # ---- does the volume hold the retention it was asked to hold?
    rate = next((x["value"] for x in inf if x["claim"] == "measured_daily_rate_bytes"), None)
    if df and ls:
        seg_total = sum(days.values())
        outside = df["used_bytes"] - seg_total - ls["other_bytes"]
        if outside > 0.02 * df["size_bytes"]:
            inf.append({"claim": "bytes_used_outside_the_market_files", "status": "INFERRED", "value": int(outside), "rule": "df used minus the segment and other files listed in /data/market: other writers on /data (monitor, shadow, mainnet), "
                                                                                                                          "deleted-but-open files or a partial listing; capture `du -sb /data/*` to say which"})
    if df and ls and retention is not None and rate:
        peak = (retention + 1) * rate * HOURLY_PASS_FACTOR
        other = ls["other_bytes"]
        need = peak + other + reserve_mib * MIB
        usable = df["available_bytes"] + sum(days.values())          # the room the recorder could own: free space plus what its own segments occupy (never the other writers' bytes)
        inf.append({"claim": "volume_meets_minimum_for_retention", "status": "INFERRED", "value": usable >= need,
                    "rule": "(available + the market segments' own bytes) >= (retention+1) x measured rate x 1.042 + other market files + reserve",
                    "needed_bytes": int(need), "usable_bytes": int(usable), "alert_free_needed_bytes": int(need + ALERT_HEADROOM_MIB * MIB),
                    "assumption": "the measured rate holds for the next days (it is a mean of closed days, not a forecast); other writers' bytes on /data are not counted as available"})
    elif df and retention is not None and rate and not ls:
        unknown.append("whether the volume holds the retention (capture ls.txt)")

    # ---- retention overdue? (pruning blocked)
    if days and retention is not None:
        oldest = sorted(days)[0]
        due = int(dt.datetime.strptime(oldest, "%Y%m%d").replace(tzinfo=dt.timezone.utc).timestamp() * 1000) + (retention + 1) * DAY_MS
        if now_ms >= due + 3_600_000:
            inf.append({"claim": "retention_overdue", "status": "INFERRED", "value": True, "oldest_day": oldest, "due_utc": dt.datetime.fromtimestamp(due / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "rule": "a healthy recorder removes day D at 00:00 UTC of D+retention+1 (<= 1 h late). It is still on disk: pruning is blocked. Candidate causes the captures cannot separate: an image older than the "
                                "bounded stalled-recorder recovery (the newest-segment bound), or a refused write that never opened the next day"})
        newest = sorted(days)[-1]
        if now_ms // DAY_MS * DAY_MS > int(dt.datetime.strptime(newest, "%Y%m%d").replace(tzinfo=dt.timezone.utc).timestamp() * 1000) and writing["status"] == "NOT_WRITING":
            inf.append({"claim": "next_day_segment_never_opened", "status": "INFERRED", "value": True, "newest_day": newest, "rule": "today's UTC day has no segment and the log is not growing: a refused write never opens the next day"})

    # ---- the prune receipts and their backups
    prune_rows = []
    for r in pruned:
        seg = r.get("segment")
        if not isinstance(seg, str):
            continue
        prune_rows.append({"segment": seg, "records": _int(r.get("records")), "last_chain": r.get("last_chain"), "uninitialised": bool(r.get("uninitialised", False)),
                           "pruned_at_utc": _utc_text(r.get("pruned_at_ms"))})
    ob["pruned_segments"] = {"rows": prune_rows, "source": "pruned.jsonl"} if pruned else {"rows": [], "source": "pruned.jsonl (absent or empty)"}
    if not pruned:
        unknown.append("whether anything was pruned (pruned.jsonl is absent or empty: a failed read cannot be told from an empty file)")
    backups = _backup_status(prune_rows, receipts, backup, manifest)
    ob["pruned_segment_backups"] = backups
    if not (receipts and backup):
        unknown.append("whether each pruned segment has a verified off-volume backup (supply --receipts R.jsonl and --backup DIR, the owner's md_seal outputs)")
    elif not (manifest and os.path.isfile(manifest)):
        unknown.append("whether the backups RESTORE (the restore check was not run: supply --manifest M.jsonl, the md_seal seal manifest)")

    # ---- the running image against THIS checkout's merged source
    actual = _read(os.path.join(evidence, "image_actual.txt"))
    if actual is not None:
        import image_manifest as IM
        res = IM.compare(IM.expected_listing(root), actual, root)
        ob["image"] = {"verdict": res["verdict"], "contains_this_recorder_implementation": res["contains_this_recorder_implementation"], "differing_files": res["differing"][:20],
                       "n_differing": len(res["differing"]), "conflicting_paths": res["conflicting_paths"], "expected_from": os.path.abspath(root)}
    else:
        unknown.append("whether the running image contains this checkout's recorder (capture image_actual.txt with image_manifest.py command)")

    findings: List[str] = []
    vol = ob.get("volume")
    if vol and vol["free_below_reserve"]:
        findings.append(f"FREE_BELOW_RESERVE: {vol['free_mib']} MiB free (OBSERVED) < {reserve_mib:g} MiB reserve ({reserve_src}): the store refuses every write")
    if writing["status"] == "NOT_WRITING":
        findings.append("NOT_WRITING: " + writing["why"] + " (OBSERVED)")
    for x in inf:
        if x["claim"] == "volume_meets_minimum_for_retention" and x["value"] is False:
            findings.append(f"VOLUME_BELOW_MINIMUM_FOR_RETENTION: needs {x['needed_bytes'] / 1e9:.2f} GB usable at the measured rate, has {x['usable_bytes'] / 1e9:.2f} GB (INFERRED)")
        if x["claim"] == "retention_overdue":
            findings.append(f"RETENTION_OVERDUE: day {x['oldest_day']} should have been removed at {x['due_utc']} (INFERRED)")
    image = ob.get("image")
    if image and image["verdict"] != "IDENTICAL_TO_EXPECTED":
        findings.append(f"IMAGE_DIFFERS_FROM_THIS_CHECKOUT: {image['verdict']} ({image['n_differing']} file(s))")
    unbacked = [b["segment"] for b in backups.get("rows", []) if b["status"] not in ("BACKED_UP_AND_VERIFIED", "NOT_APPLICABLE")]
    if unbacked and receipts and backup:
        findings.append(f"PRUNED_WITHOUT_VERIFIED_BACKUP: {unbacked}")
    core_known = bool(vol) and writing["status"] != "UNKNOWN" and bool(proc)
    verdict = "FINDINGS" if findings else ("NO_FINDING_ON_THE_CAPTURES_GIVEN" if core_known else "INCONCLUSIVE")
    return {"schema": "recorder_forensics/v1", "now_utc": dt.datetime.fromtimestamp(now_ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "now_basis": now_basis, "verdict": verdict,
            "observed": ob, "inferred": inf, "unknown": sorted(set(unknown)), "findings": findings,
            "note": "nothing here resizes, restarts, prunes or deletes; every decision is the owner's. A pruned day with no verified backup is unrecoverable"}


def _backup_status(prune_rows: Sequence[Dict[str, Any]], receipts: Optional[str], backup: Optional[str], manifest: Optional[str]) -> Dict[str, Any]:
    """Per pruned segment: BACKED_UP_AND_VERIFIED only when the receipts' chain verifies, a receipt exists, the copy exists with the receipted sha256, and the copy's records and last chain equal what the recorder
    itself wrote to pruned.jsonl. No receipts or backup directory supplied: UNKNOWN for every row (silence is not a backup)."""
    if not (receipts and backup):
        return {"rows": [{"segment": r["segment"], "status": "UNKNOWN", "why": "no --receipts/--backup supplied"} for r in prune_rows], "restore_check": None}
    import evidence_chain as EC
    import md_seal as SL
    rows: List[Dict[str, Any]] = []
    chain_ok = os.path.isfile(receipts) and bool(EC.verify(receipts).get("ok", False))
    got = {r["segment"]: r for r in EC.rows_of(EC.read_lines(receipts)[0]) if isinstance(r, dict) and "segment" in r} if chain_ok else {}
    for r in prune_rows:
        name = r["segment"]
        if r["uninitialised"]:
            rows.append({"segment": name, "status": "NOT_APPLICABLE", "why": "an uninitialised (empty) segment holds no record"})
        elif not chain_ok:
            rows.append({"segment": name, "status": "RECEIPTS_UNVERIFIED", "why": "the receipts' own chain does not verify or the file is absent"})
        elif name not in got:
            rows.append({"segment": name, "status": "NO_RECEIPT", "why": "pruned by retention and never backed up: unrecoverable"})
        else:
            rc, copy = got[name], os.path.join(backup, name)
            if not os.path.isfile(copy):
                rows.append({"segment": name, "status": "COPY_MISSING", "why": "receipted but the copy is not in the backup directory"})
            elif SL._sha256(copy) != rc.get("sha256"):
                rows.append({"segment": name, "status": "COPY_HASH_DIFFERS", "why": "the copy does not hash as its receipt"})
            else:
                d = SL.describe(backup, name, closed=True)
                same = d.get("last_chain") == r["last_chain"] == rc.get("last_chain") and (r["records"] is None or d.get("records") == r["records"])
                rows.append({"segment": name, "status": "BACKED_UP_AND_VERIFIED" if same else "CHAIN_OR_COUNT_DIFFERS",
                             "why": "copy hashes as receipted; records and last chain equal pruned.jsonl" if same else f"copy records {d.get('records')} / last chain differ from pruned.jsonl ({r['records']})"})
    rc_res = None
    if manifest and os.path.isfile(manifest):
        rc_res = SL.restore_check(backup, receipts, manifest)
    # VERIFIED needs the seal receipt AND a passing restore check (RUNBOOKS): a matching copy alone is not a backup that restores
    ok = bool(rc_res and rc_res.get("ok"))
    for row in rows:
        if row["status"] == "BACKED_UP_AND_VERIFIED" and not ok:
            row["status"] = "COPY_MATCHES_RECEIPT_RESTORE_NOT_VERIFIED"
            row["why"] += ("; the restore check was NOT run (supply --manifest, the md_seal seal manifest)" if rc_res is None else "; the restore check FAILED")
    return {"rows": rows, "restore_check": rc_res}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("commands")
    a = sub.add_parser("analyse")
    a.add_argument("--evidence", required=True)
    a.add_argument("--receipts", default=None)
    a.add_argument("--backup", default=None)
    a.add_argument("--manifest", default=None)
    a.add_argument("--now-ms", type=int, default=None)
    args = ap.parse_args(argv)
    if args.cmd == "commands":
        sys.stdout.write(commands_text())
        return 0
    if not os.path.isdir(args.evidence):
        print(f"recorder_forensics: --evidence {args.evidence!r} is not a directory", file=sys.stderr)
        return 2
    res = analyse(args.evidence, now_ms=args.now_ms, receipts=args.receipts, backup=args.backup, manifest=args.manifest)
    print(json.dumps(res, indent=2, sort_keys=True, default=str))
    return 1 if res["findings"] else (3 if res["verdict"] == "INCONCLUSIVE" else 0)


if __name__ == "__main__":
    sys.exit(main())
