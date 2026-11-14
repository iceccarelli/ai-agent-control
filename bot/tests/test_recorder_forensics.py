"""Round 21: the owner's read-only captures of the carry-drill machine become a verdict in which every statement is OBSERVED, INFERRED or UNKNOWN, and silence is never health.

The scenario is the one the owner reported on 2026-11-14: a 5 GB volume with about 4.2 GB used and less than the 512 MiB reserve free, a recorder count frozen at 21,837,668 in successive monitor entries,
pruned.jsonl rows for 5, 6 and 7 November, `ps` absent from the image. Offline; nothing connects to Fly; the evidence directory is never written."""
from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path[:0] = [BOT, os.path.join(BOT, "tools"), HERE]

import recorder_forensics as RF  # noqa: E402
import image_manifest as IM  # noqa: E402
import md_seal as SL  # noqa: E402
import test_md_seal as TMS  # noqa: E402

NOW = 1_794_700_000_000          # 2026-11-14T... UTC: the day after the last segment day below
DAY8, DAY9, DAY10 = "20261108", "20261109", "20261110"
G = 1_000_000_000


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("network touched")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


DF = "Filesystem        1B-blocks       Used  Available Use% Mounted on\n/dev/vdb         5251530752 4200000000  480000000  90% /data\n"


def _ls(sizes):
    """`ls -l` of /data/market. The fixture's 1.4 GB per day is the owner's reported 4.2 GB used divided by three retained days: an ASSUMPTION of this fixture (the owner's per-segment sizes are not known), not a measurement."""
    lines = ["total 4100000"]
    for day, size in sizes.items():
        lines.append(f"-rw-r--r-- 1 bot bot {size} 2026-{day[4:6]}-{day[6:8]} 23:59:59.000000000 +0000 md-{day}.db")
    lines.append("-rw-r--r-- 1 bot bot 1200000 2026-11-10 23:00:00.000000000 +0000 capacity-20261110.jsonl")
    lines.append("-rw-r--r-- 1 bot bot 2100 2026-11-10 23:00:00.000000000 +0000 pruned.jsonl")
    return "\n".join(lines) + "\n"


def _monitor(count, n=5, step_min=15):
    rows = []
    for i in range(n):
        rows.append({"at": f"2026-11-14T{(22 + (i * step_min) // 60):02d}:{(i * step_min) % 60:02d}:00Z", "recorder_state": "FAULT", "recorder_records": count, "recorder_free_mb": 509.0})
    return "".join(json.dumps(r) + "\n" for r in rows)


PROC = "\n".join(["python", "tools/runner_supervisor.py", "python", "tools/market_recorder.py", "--recorder-min-free-mb", "512", "--recorder-retention-days", "2", "python", "tools/runner_monitor.py"]) + "\n"


def evidence(tmp_path, *, name="evidence", df=DF, ls=None, monitor=None, proc=PROC, pruned=None, image=None, status=None):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    files = {"df.txt": df, "ls.txt": ls if ls is not None else _ls({DAY8: int(1.4 * G), DAY9: int(1.4 * G), DAY10: int(1.4 * G)}), "monitor_tail.jsonl": monitor if monitor is not None else _monitor(21_837_668),
             "proc.txt": proc, "pruned.jsonl": pruned if pruned is not None else "", "image_actual.txt": image, "recorder_status.json": status}
    for fname, text in files.items():
        if text is not None:
            (d / fname).write_text(text)
    return str(d)


def test_the_capture_commands_are_read_only_and_the_proc_one_survives_its_quoting(tmp_path):
    text = RF.commands_text()
    forbidden = ("rm ", "mv ", " cp ", "extend", "destroy", "deploy", "restart", "scale", "volumes", "secrets", "machine", "--arm", "kill ")
    for ln in (ln for ln in text.splitlines() if not ln.startswith("#")):
        assert not any(w in ln for w in forbidden), ln
        if ln.startswith("fly "):
            assert ln.startswith("fly ssh console -a carry-drill -C ") and any(v in ln for v in ("df ", "ls ", "cat ", "tail "))
    # the quoting of the /proc command, executed with a stand-in `fly` that parses the -C argument the way an exec does (shlex), not by trusting the string
    line = next(ln for ln in text.splitlines() if "proc.txt" in ln and ln.startswith("fly "))
    shim = tmp_path / "shim.sh"
    shim.write_text('fly() { python3 -c \'import shlex,subprocess,sys; sys.exit(subprocess.run(shlex.split(sys.argv[1])).returncode)\' "$6"; }\n' + line.replace("> proc.txt", "") + "\n")
    r = subprocess.run(["bash", str(shim)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "/proc/[0-9]*/cmdline" in r.stdout and "\x00" not in r.stdout and "tr" in r.stdout, r.stdout[:300]
    assert shlex.split(line.split("-C ", 1)[1].replace("> proc.txt", "").strip())[0].startswith("sh -c"), "the remote command is one argument"


def test_the_parsers_read_the_owners_output_shapes():
    df = RF.parse_df(DF)
    assert df == {"size_bytes": 5251530752, "used_bytes": 4200000000, "available_bytes": 480000000}
    assert RF.parse_df("Filesystem 1B-blocks Used Available Use% Mounted on\n/dev/mapper/vg-data\n  5251530752 4200000000 480000000 90% /data\n")["available_bytes"] == 480000000
    assert RF.parse_df("garbage") is None and RF.parse_df(None) is None
    # Round 21 (review): read from the right and only for the /data mount: a preamble, a numeric device name or the df of another mount is never the volume
    pre = "Connecting to fdaa:0:1234:a7b:5678:9012:3456:2... complete\n" + DF
    assert RF.parse_df(pre) == df
    assert RF.parse_df("Filesystem 1B-blocks Used Available Use% Mounted on\n/dev/disk/by-id/volume-12345 5251530752 4200000000 480000000 90% /data\n") == df
    assert RF.parse_df("Filesystem 1B-blocks Used Available Use% Mounted on\noverlay 5251530752 4200000000 480000000 90% /\n") is None
    assert RF.parse_df("Filesystem 1B-blocks Used Available Use% Mounted on\n/dev/vdb 100 4200000000 480000000 90% /data\n") is None, "used + available cannot exceed the size"
    ls = RF.parse_ls(_ls({DAY8: 10, DAY9: 20}) + "-rw-r--r-- 1 bot bot 5 2026-11-09 01:00:00.000000000 +0000 md-20261109.db-wal\n")
    assert ls["segment_bytes_by_day"] == {DAY8: 10, DAY9: 25} and ls["other_bytes"] == 1200000 + 2100
    p = RF.parse_proc(PROC)
    assert p["processes_present"] == {"recorder": True, "supervisor": True, "monitor": True} and p["retention_days"] == "2" and p["min_free_mb"] == "512" and p["order_capable_words"] == []
    assert RF.parse_proc("a\x00b\x00tools/market_recorder.py\x00--recorder-retention-days=1\x00")["retention_days"] == "1"
    assert RF.parse_ls("ls: cannot access '/data/market': No such file or directory\n") is None, "an error message is not an empty directory"
    assert RF.parse_ls("total 0\n")["segment_bytes_by_day"] == {}


def test_the_owners_situation_is_diagnosed_with_every_claim_labelled(tmp_path):
    res = RF.analyse(evidence(tmp_path), now_ms=NOW)
    ob, inf = res["observed"], {x["claim"]: x for x in res["inferred"]}
    assert ob["volume"]["free_below_reserve"] is True and ob["volume"]["free_mib"] == pytest.approx(457.8, abs=0.1) and ob["volume"]["reserve_source"].startswith("OBSERVED")
    assert ob["writing"]["status"] == "NOT_WRITING" and "21837668" in ob["writing"]["why"].replace(",", "")
    assert ob["processes"]["processes_present"]["recorder"] is True, "a recorder process is present ..."
    assert "recorder_process_present_but_not_writing" in inf, "... and that is not proof it writes"
    assert inf["measured_daily_rate_bytes"]["value"] == int(1.4 * G) and inf["measured_daily_rate_bytes"]["status"] == "INFERRED"
    # three days on disk, retention 2, the measured rate: usable (used + available) is below what the retention needs
    need = inf["volume_meets_minimum_for_retention"]
    assert need["value"] is False and need["needed_bytes"] > need["usable_bytes"] and need["alert_free_needed_bytes"] > need["needed_bytes"]
    assert inf["retention_overdue"]["oldest_day"] == DAY8 and inf["next_day_segment_never_opened"]["value"] is True
    codes = [f.split(":")[0] for f in res["findings"]]
    assert {"FREE_BELOW_RESERVE", "NOT_WRITING", "VOLUME_BELOW_MINIMUM_FOR_RETENTION", "RETENTION_OVERDUE"} <= set(codes)
    assert any("backup" in u for u in res["unknown"]) and any("image" in u for u in res["unknown"]), "what was not captured stays UNKNOWN"


def test_a_pruned_day_is_unknown_not_backed_up_when_no_receipts_are_supplied(tmp_path):
    pruned = "".join(json.dumps({"segment": f"md-202611{d:02d}.db", "records": 7_000_000 + d, "last_chain": f"{d:064x}", "pruned_at_ms": NOW - (10 - d) * 86_400_000}) + "\n" for d in (5, 6, 7))
    res = RF.analyse(evidence(tmp_path, pruned=pruned), now_ms=NOW)
    rows = res["observed"]["pruned_segment_backups"]["rows"]
    assert [r["segment"] for r in rows] == ["md-20261105.db", "md-20261106.db", "md-20261107.db"] and all(r["status"] == "UNKNOWN" for r in rows)
    assert not any(f.startswith("PRUNED_WITHOUT_VERIFIED_BACKUP") for f in res["findings"]), "no verdict without the owner's receipts"


@pytest.fixture
def sealed(tmp_path):
    """Two real segments (day 1 closed, day 2 open), sealed, day 1 backed up: the owner's outside memory."""
    md = TMS.make_store(tmp_path / "md")
    manifest = str(tmp_path / "off" / "manifest.jsonl")
    os.makedirs(os.path.dirname(manifest), exist_ok=True)
    SL.seal(md, manifest, now_ms=TMS.NOW)
    dest, receipts = str(tmp_path / "bk"), str(tmp_path / "off" / "receipts.jsonl")
    SL.backup(md, dest, receipts, manifest=manifest, now_ms=TMS.NOW)
    name = SL.segment_names(md)[0]
    d = SL.describe(md, name, closed=True)
    return {"md": md, "manifest": manifest, "dest": dest, "receipts": receipts, "name": name, "records": d["records"], "last_chain": d["last_chain"], "tmp": tmp_path}


def _pruned_line(name, records, last_chain):
    return json.dumps({"segment": name, "records": records, "last_chain": last_chain, "pruned_at_ms": NOW}) + "\n"


def test_a_pruned_day_is_backed_up_only_when_the_copy_hashes_and_chains_as_the_recorder_wrote(sealed):
    ev = evidence(sealed["tmp"], pruned=_pruned_line(sealed["name"], sealed["records"], sealed["last_chain"]))
    kw = dict(receipts=sealed["receipts"], backup=sealed["dest"], manifest=sealed["manifest"], now_ms=NOW)
    ok = RF.analyse(ev, **kw)
    row = ok["observed"]["pruned_segment_backups"]["rows"][0]
    assert row["status"] == "BACKED_UP_AND_VERIFIED" and ok["observed"]["pruned_segment_backups"]["restore_check"]["ok"] is True
    assert not any(f.startswith("PRUNED_WITHOUT") for f in ok["findings"])
    # the recorder's own pruned.jsonl disagrees with the receipted copy: a wrong chain or count is not a backup of THAT day
    bad_chain = RF.analyse(evidence(sealed["tmp"], name="e_chain", pruned=_pruned_line(sealed["name"], sealed["records"], "f" * 64)), **kw)
    assert bad_chain["observed"]["pruned_segment_backups"]["rows"][0]["status"] == "CHAIN_OR_COUNT_DIFFERS" and any(f.startswith("PRUNED_WITHOUT") for f in bad_chain["findings"])
    bad_count = RF.analyse(evidence(sealed["tmp"], name="e_count", pruned=_pruned_line(sealed["name"], sealed["records"] + 1, sealed["last_chain"])), **kw)
    assert bad_count["observed"]["pruned_segment_backups"]["rows"][0]["status"] == "CHAIN_OR_COUNT_DIFFERS"
    # a day the recorder pruned that was never backed up: unrecoverable
    never = RF.analyse(evidence(sealed["tmp"], name="e_never", pruned=_pruned_line("md-20201231.db", 5, "a" * 64)), **kw)
    assert never["observed"]["pruned_segment_backups"]["rows"][0]["status"] == "NO_RECEIPT"
    # the copy tampered with, or removed
    copy = os.path.join(sealed["dest"], sealed["name"])
    with open(copy, "ab") as fh:
        fh.write(b"x")
    tampered = RF.analyse(ev, **kw)
    assert tampered["observed"]["pruned_segment_backups"]["rows"][0]["status"] == "COPY_HASH_DIFFERS"
    os.remove(copy)
    gone = RF.analyse(ev, **kw)
    assert gone["observed"]["pruned_segment_backups"]["rows"][0]["status"] == "COPY_MISSING"


def test_the_running_image_is_compared_with_this_checkouts_source_and_fails_closed(tmp_path):
    expected = IM.expected_listing(BOT)
    ok = RF.analyse(evidence(tmp_path, image=expected.replace(IM.MARKER + "\n", "")), now_ms=NOW)
    assert ok["observed"]["image"]["verdict"] in ("IDENTICAL_TO_EXPECTED", "RECORDER_FILES_IDENTICAL_REPORT_TOOLS_DIFFER") and ok["observed"]["image"]["contains_this_recorder_implementation"] is True
    lines = expected.splitlines()
    stale = "\n".join(ln.replace(ln.split()[0], "0" * 64, 1) if ln.endswith("md_store.py") else ln for ln in lines) + "\n"       # an image from before the recorder's bounded recovery: one recorder file differs
    bad = RF.analyse(evidence(tmp_path, image=stale), now_ms=NOW)
    assert bad["observed"]["image"]["contains_this_recorder_implementation"] is False and any(f.startswith("IMAGE_DIFFERS_FROM_THIS_CHECKOUT") for f in bad["findings"])
    assert "md_store.py" in bad["observed"]["image"]["differing_files"]


def test_a_growing_count_is_writing_a_short_flat_window_is_unknown_and_missing_captures_are_unknown(tmp_path):
    growing = "".join(json.dumps({"at": f"2026-11-14T22:{i * 15:02d}:00Z", "recorder_state": "RECORDING", "recorder_records": 100 + i * 50}) + "\n" for i in range(4))
    assert RF.analyse(evidence(tmp_path, monitor=growing, df=None), now_ms=NOW)["observed"]["writing"]["status"] == "WRITING"
    short_flat = "".join(json.dumps({"at": f"2026-11-14T22:0{i}:00Z", "recorder_state": "FAULT", "recorder_records": 7}) + "\n" for i in range(4))
    assert RF.analyse(evidence(tmp_path, monitor=short_flat), now_ms=NOW)["observed"]["writing"]["status"] == "UNKNOWN", "three equal counts within minutes prove nothing"
    empty = tmp_path / "empty"
    empty.mkdir()
    res = RF.analyse(str(empty), now_ms=NOW)
    assert res["findings"] == [] and res["observed"]["writing"]["status"] == "UNKNOWN" and len(res["unknown"]) >= 6, "silence is not health"
    assert res["verdict"] == "INCONCLUSIVE", "no finding on no captures is not a clean bill"
    assert RF.main(["analyse", "--evidence", str(empty), "--now-ms", str(NOW)]) == 3
    assert RF.main(["analyse", "--evidence", str(tmp_path / "does-not-exist")]) == 2


def test_a_healthy_volume_has_no_findings_and_the_tool_writes_nothing_into_the_evidence(tmp_path, capsys):
    healthy_df = "Filesystem 1B-blocks Used Available Use% Mounted on\n/dev/vdb 10737418240 3000000000 7000000000 30% /data\n"
    growing = "".join(json.dumps({"at": f"2026-11-14T15:{i * 15:02d}:00Z", "recorder_state": "RECORDING", "recorder_records": 100 + i * 50}) + "\n" for i in range(4))
    ls = _ls({"20261112": int(1.4 * G), "20261113": int(1.4 * G), "20261114": int(0.3 * G)})
    ev = evidence(tmp_path, df=healthy_df, monitor=growing, ls=ls)
    before = sorted((n, os.path.getsize(os.path.join(ev, n))) for n in os.listdir(ev))
    now = 1_794_672_000_000      # 2026-11-14T16:00:00Z
    res = RF.analyse(ev, now_ms=now)
    assert res["findings"] == [], res["findings"]
    assert res["verdict"] == "NO_FINDING_ON_THE_CAPTURES_GIVEN"
    assert sorted((n, os.path.getsize(os.path.join(ev, n))) for n in os.listdir(ev)) == before
    assert RF.main(["analyse", "--evidence", ev, "--now-ms", str(now)]) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == "recorder_forensics/v1"
    assert RF.main(["commands"]) == 0 and "fly ssh console" in capsys.readouterr().out


# ------------------------------------------------------------------------------------------------ Round 21 review: a stale tail, a cache-length window, capacity, missing captures, restore, labels
def test_a_stale_monitor_tail_is_not_evidence_about_the_present(tmp_path):
    """REPRODUCED (review): four entries from 11-01 with growing counts, captured on 11-14, read as WRITING."""
    stale = "".join(json.dumps({"at": f"2026-11-01T10:{i * 15:02d}:00Z", "recorder_state": "RECORDING", "recorder_records": 150 + i * 50}) + "\n" for i in range(4))
    w = RF.analyse(evidence(tmp_path, monitor=stale), now_ms=NOW)["observed"]["writing"]
    assert w["status"] == "UNKNOWN" and "stale tail" in w["why"], w
    flat_stale = "".join(json.dumps({"at": f"2026-11-01T10:{i * 15:02d}:00Z", "recorder_state": "FAULT", "recorder_records": 9}) + "\n" for i in range(4))
    assert RF.analyse(evidence(tmp_path, name="e2", monitor=flat_stale), now_ms=NOW)["observed"]["writing"]["status"] == "UNKNOWN", "a stale frozen count is not NOT_WRITING either"


def test_a_flat_count_over_less_than_two_cache_periods_is_not_a_stall(tmp_path):
    """The recorder caches its record count for 600 s: three equal counts 12 minutes apart can be the cache."""
    flat12 = "".join(json.dumps({"at": f"2026-11-14T22:{i * 6:02d}:00Z", "recorder_state": "FAULT", "recorder_records": 7}) + "\n" for i in range(3))
    assert RF.analyse(evidence(tmp_path, monitor=flat12), now_ms=NOW)["observed"]["writing"]["status"] == "UNKNOWN"
    flat30 = "".join(json.dumps({"at": f"2026-11-14T22:{i * 15:02d}:00Z", "recorder_state": "FAULT", "recorder_records": 7}) + "\n" for i in range(3))
    assert RF.analyse(evidence(tmp_path, name="e2", monitor=flat30), now_ms=NOW)["observed"]["writing"]["status"] == "NOT_WRITING"


def test_capacity_counts_only_the_room_the_recorder_can_own_not_bytes_other_writers_hold(tmp_path):
    """REPRODUCED (review): 3 segments of 0.5 GB, df used 4.2 GB (the rest is other writers), 0.48 GB free, retention 2: the room is 0.48 + 1.5 = 1.98 GB < the 2.10 GB the retention needs, not (used + available) = 4.68 GB."""
    ev = evidence(tmp_path, ls=_ls({DAY8: int(0.5 * G), DAY9: int(0.5 * G), DAY10: int(0.5 * G)}))
    res = RF.analyse(ev, now_ms=NOW)
    inf = {x["claim"]: x for x in res["inferred"]}
    need = inf["volume_meets_minimum_for_retention"]
    assert need["value"] is False and need["usable_bytes"] == 480_000_000 + int(1.5 * G), need
    assert inf["bytes_used_outside_the_market_files"]["value"] > 2_000_000_000, "the 2.7 GB the market files do not account for is stated, not hidden"
    assert any(f.startswith("VOLUME_BELOW_MINIMUM_FOR_RETENTION") for f in res["findings"])


def test_a_missing_or_empty_capture_is_unknown_never_nothing_to_report(tmp_path):
    res = RF.analyse(evidence(tmp_path, ls="ls: cannot access '/data/market': No such file or directory\n", monitor=""), now_ms=NOW)
    assert "observed" in res and "segments" not in res["observed"], "an ls error is not an empty directory"
    unk = " | ".join(res["unknown"])
    assert "segments on disk" in unk and "pruned.jsonl is absent or empty" in unk and "monitor history" in unk and "recorder's own status" in unk, unk


def test_a_matching_copy_without_a_passing_restore_check_is_not_verified(sealed):
    """REPRODUCED (review): BACKED_UP_AND_VERIFIED was awarded without the restore check although the runbook requires a seal receipt AND a restore check."""
    ev = evidence(sealed["tmp"], pruned=_pruned_line(sealed["name"], sealed["records"], sealed["last_chain"]))
    no_manifest = RF.analyse(ev, receipts=sealed["receipts"], backup=sealed["dest"], now_ms=NOW)
    row = no_manifest["observed"]["pruned_segment_backups"]["rows"][0]
    assert row["status"] == "COPY_MATCHES_RECEIPT_RESTORE_NOT_VERIFIED" and "restore check was NOT run" in row["why"]
    assert any(f.startswith("PRUNED_WITHOUT_VERIFIED_BACKUP") for f in no_manifest["findings"]) and any("RESTORE" in u for u in no_manifest["unknown"])
    bad_manifest = os.path.join(os.path.dirname(sealed["manifest"]), "tampered.jsonl")
    with open(sealed["manifest"], "rb") as src, open(bad_manifest, "wb") as dst:
        dst.write(src.read() + b'{"not":"a chained row"}\n')
    failed = RF.analyse(evidence(sealed["tmp"], name="e_fail", pruned=_pruned_line(sealed["name"], sealed["records"], sealed["last_chain"])), receipts=sealed["receipts"], backup=sealed["dest"], manifest=bad_manifest, now_ms=NOW)
    assert failed["observed"]["pruned_segment_backups"]["rows"][0]["status"] == "COPY_MATCHES_RECEIPT_RESTORE_NOT_VERIFIED" and "FAILED" in failed["observed"]["pruned_segment_backups"]["rows"][0]["why"]
    assert any(f.startswith("PRUNED_WITHOUT_VERIFIED_BACKUP") for f in failed["findings"])


def test_an_assumed_reserve_is_labelled_assumed_and_a_nan_flag_is_not_believed(tmp_path):
    res = RF.analyse(evidence(tmp_path, proc=None), now_ms=NOW)
    f = next(x for x in res["findings"] if x.startswith("FREE_BELOW_RESERVE"))
    assert "ASSUMED" in f and "(OBSERVED) <" in f, "the free space is observed; the reserve is the documented value, and says so"
    nan = PROC.replace("--recorder-min-free-mb\n512", "--recorder-min-free-mb\nnan")
    r2 = RF.analyse(evidence(tmp_path, name="e2", proc=nan), now_ms=NOW)
    assert r2["observed"]["volume"]["reserve_mib"] == 512.0 and r2["observed"]["volume"]["reserve_source"].startswith("ASSUMED")
    assert any(x.startswith("FREE_BELOW_RESERVE") for x in r2["findings"]), "a NaN reserve flag must not silence the finding"


def test_uninitialised_segments_are_not_reported_as_unbacked(sealed):
    pr = json.dumps({"segment": "md-20260101.db", "records": 0, "last_chain": None, "uninitialised": True, "pruned_at_ms": NOW}) + "\n"
    res = RF.analyse(evidence(sealed["tmp"], pruned=pr), receipts=sealed["receipts"], backup=sealed["dest"], manifest=sealed["manifest"], now_ms=NOW)
    assert res["observed"]["pruned_segment_backups"]["rows"][0]["status"] == "NOT_APPLICABLE"
    assert not any(f.startswith("PRUNED_WITHOUT_VERIFIED_BACKUP") for f in res["findings"])


def test_a_crafted_prune_timestamp_is_unknown_not_a_crash(tmp_path):
    pr = json.dumps({"segment": "md-20261105.db", "records": 5, "last_chain": "a" * 64, "pruned_at_ms": 10 ** 30}) + "\n"
    res = RF.analyse(evidence(tmp_path, pruned=pr), now_ms=NOW)
    assert res["observed"]["pruned_segments"]["rows"][0]["pruned_at_utc"] is None
