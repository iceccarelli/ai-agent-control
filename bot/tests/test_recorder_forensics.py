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
    lines = ["total 4100000"]
    for day, size in sizes.items():
        lines.append(f"-rw-r--r-- 1 bot bot {size} 2026-{day[4:6]}-{day[6:8]} 23:59:59.000000000 +0000 md-{day}.db")
    lines.append("-rw-r--r-- 1 bot bot 1200000 2026-11-10 23:00:00.000000000 +0000 capacity-20261110.jsonl")
    lines.append("-rw-r--r-- 1 bot bot 2100 2026-11-10 23:00:00.000000000 +0000 pruned.jsonl")
    return "\n".join(lines) + "\n"


def _monitor(count, n=5, step_min=15):
    rows = []
    for i in range(n):
        rows.append({"at": f"2026-11-13T{(10 + (i * step_min) // 60):02d}:{(i * step_min) % 60:02d}:00Z", "recorder_state": "FAULT", "recorder_records": count, "recorder_free_mb": 509.0})
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
    assert df == {"size_bytes": 5251530752, "used_bytes": 4200000000, "available_bytes": 480000000, "usable_bytes": 4680000000}
    assert RF.parse_df("Filesystem 1B-blocks Used Available Use% Mounted on\n/dev/mapper/vg-data\n  5251530752 4200000000 480000000 90% /data\n")["available_bytes"] == 480000000
    assert RF.parse_df("garbage") is None and RF.parse_df(None) is None
    ls = RF.parse_ls(_ls({DAY8: 10, DAY9: 20}) + "-rw-r--r-- 1 bot bot 5 2026-11-09 01:00:00.000000000 +0000 md-20261109.db-wal\n")
    assert ls["segment_bytes_by_day"] == {DAY8: 10, DAY9: 25} and ls["other_bytes"] == 1200000 + 2100
    p = RF.parse_proc(PROC)
    assert p["processes_present"] == {"recorder": True, "supervisor": True, "monitor": True} and p["retention_days"] == "2" and p["min_free_mb"] == "512" and p["order_capable_words"] == []
    assert RF.parse_proc("a\x00b\x00tools/market_recorder.py\x00--recorder-retention-days=1\x00")["retention_days"] == "1"


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
    growing = "".join(json.dumps({"at": f"2026-11-14T10:{i * 15:02d}:00Z", "recorder_state": "RECORDING", "recorder_records": 100 + i * 50}) + "\n" for i in range(4))
    assert RF.analyse(evidence(tmp_path, monitor=growing, df=None), now_ms=NOW)["observed"]["writing"]["status"] == "WRITING"
    short_flat = "".join(json.dumps({"at": f"2026-11-14T10:0{i}:00Z", "recorder_state": "FAULT", "recorder_records": 7}) + "\n" for i in range(4))
    assert RF.analyse(evidence(tmp_path, monitor=short_flat), now_ms=NOW)["observed"]["writing"]["status"] == "UNKNOWN", "three equal counts within minutes prove nothing"
    empty = tmp_path / "empty"
    empty.mkdir()
    res = RF.analyse(str(empty), now_ms=NOW)
    assert res["findings"] == [] and res["observed"]["writing"]["status"] == "UNKNOWN" and len(res["unknown"]) >= 6, "silence is not health"


def test_a_healthy_volume_has_no_findings_and_the_tool_writes_nothing_into_the_evidence(tmp_path, capsys):
    healthy_df = "Filesystem 1B-blocks Used Available Use% Mounted on\n/dev/vdb 10737418240 3000000000 7000000000 30% /data\n"
    growing = "".join(json.dumps({"at": f"2026-11-14T10:{i * 15:02d}:00Z", "recorder_state": "RECORDING", "recorder_records": 100 + i * 50}) + "\n" for i in range(4))
    ls = _ls({"20261112": int(1.4 * G), "20261113": int(1.4 * G), "20261114": int(0.3 * G)})
    ev = evidence(tmp_path, df=healthy_df, monitor=growing, ls=ls)
    before = sorted((n, os.path.getsize(os.path.join(ev, n))) for n in os.listdir(ev))
    now = 1_794_672_000_000      # 2026-11-14T16:00:00Z
    res = RF.analyse(ev, now_ms=now)
    assert res["findings"] == [], res["findings"]
    assert sorted((n, os.path.getsize(os.path.join(ev, n))) for n in os.listdir(ev)) == before
    assert RF.main(["analyse", "--evidence", ev, "--now-ms", str(now)]) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == "recorder_forensics/v1"
    assert RF.main(["commands"]) == 0 and "fly ssh console" in capsys.readouterr().out
