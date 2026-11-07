"""md_seal: a lost tail, a lost day or an altered record in the market-data log is DETECTED, and a backup is only proven by a restore-check.
Reproduced first: deleting ids 7-10 of 10 made MarketStore.verify() return no problem and a reopened writer resumed at id 7. Temp directories only."""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import md_seal as SL  # noqa: E402
import md_store as ST  # noqa: E402

DAY1 = 1_700_000_000_000                # 2023-11-14
DAY2 = DAY1 + 86_400_000
NOW = DAY2 + 3_600_000                  # day 2 is "today"; day 1 is closed


def make_store(directory, per_day=(10, 4)):
    s = ST.MarketStore(str(directory), commit_every=1, environment="mainnet")
    for d, n in zip((DAY1, DAY2), per_day):
        for i in range(n):
            s.append("frame", wall_ms=d + i * 1000, mono_s=float(i), category="linear", conn_id="c1", topic="tickers.BTCUSDT", payload=f'{{"i":{i},"d":{d}}}')
    s.close()
    return str(directory)


@pytest.fixture
def world(tmp_path):
    md = make_store(tmp_path / "md")
    return {"md": md, "manifest": str(tmp_path / "off" / "manifest.jsonl"), "tmp": tmp_path}


def seg(world, day=1):
    names = SL.segment_names(world["md"])
    return os.path.join(world["md"], names[day - 1])


def seal(world):
    os.makedirs(os.path.dirname(world["manifest"]), exist_ok=True)
    return SL.seal(world["md"], world["manifest"], now_ms=NOW)


def test_a_clean_log_checks_ok_and_closed_days_are_hash_pinned(world):
    res = seal(world)
    assert res["segments"] == 2
    chk = SL.check(world["md"], world["manifest"])
    assert chk["ok"] and chk["problems"] == []
    import evidence_chain as EC
    rows = EC.rows_of(EC.read_lines(world["manifest"])[0])
    s1, s2 = rows[0]["segments"]
    assert s1["closed"] and "sha256" in s1 and (not s2["closed"]) and "sha256" not in s2


def _truncate(path, keep):
    con = sqlite3.connect(path)
    con.execute("DROP TRIGGER IF EXISTS no_delete")
    for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
        con.execute(f'DROP TRIGGER "{name}"')
    con.execute("DELETE FROM log WHERE id > ?", (keep,))
    con.commit(); con.close()


def test_a_lost_tail_is_detected_and_store_verify_alone_missed_it(world):
    seal(world)
    _truncate(seg(world, 2), keep=1)
    st = ST.MarketStore(world["md"]); assert st.verify() == [], "the store alone cannot see a lost tail (the reproduced hole)"
    chk = SL.check(world["md"], world["manifest"])
    assert not chk["ok"] and any("TRUNCATED" in p for p in chk["problems"])


def test_a_lost_day_is_detected(world):
    seal(world)
    os.remove(seg(world, 1))
    chk = SL.check(world["md"], world["manifest"])
    assert any("MISSING" in p for p in chk["problems"])


def test_an_altered_closed_day_is_detected_by_hash_and_an_altered_open_record_by_chain(world):
    seal(world)
    con = sqlite3.connect(seg(world, 1)); con.execute("UPDATE log SET topic='x' WHERE id=2") if False else None
    for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
        con.execute(f'DROP TRIGGER "{name}"')
    con.execute("UPDATE log SET topic='tampered' WHERE id=2"); con.commit(); con.close()
    chk = SL.check(world["md"], world["manifest"])
    assert any("closed day's file hash changed" in p for p in chk["problems"])
    assert SL.recompute_chain(seg(world, 1))                      # and the record-level chain notices the field change independently


def test_growth_after_sealing_is_fine_and_a_new_seal_follows(world):
    seal(world)
    s = ST.MarketStore(world["md"], commit_every=1, environment="mainnet")
    s.append("frame", wall_ms=DAY2 + 99_000, mono_s=99.0, category="linear", conn_id="c1", topic="t", payload="{}"); s.close()
    assert SL.check(world["md"], world["manifest"])["ok"]
    SL.seal(world["md"], world["manifest"], now_ms=NOW)
    assert SL.check(world["md"], world["manifest"])["ok"]


def test_a_missing_or_tampered_manifest_is_not_ok(world):
    assert not SL.check(world["md"], world["manifest"])["ok"]
    seal(world)
    text = open(world["manifest"], "rb").read()
    tampered = text.replace(b'"records":10', b'"records":9')
    assert tampered != text, "the test must really change the manifest"
    open(world["manifest"], "wb").write(tampered)
    assert not SL.check(world["md"], world["manifest"])["ok"]


def test_the_manifest_may_not_live_inside_the_recorders_directory(world):
    with pytest.raises(SL.SealError, match="outside memory"):
        SL.seal(world["md"], os.path.join(world["md"], "manifest.jsonl"), now_ms=NOW)


def test_backup_copies_only_closed_days_verifies_and_is_restore_checked(world):
    dest = str(world["tmp"] / "offvol"); rec = str(world["tmp"] / "off" / "receipts.jsonl")
    os.makedirs(os.path.dirname(rec), exist_ok=True)
    res = SL.backup(world["md"], dest, rec, now_ms=NOW)
    assert [d["segment"] for d in res["backed_up"]] == [SL.segment_names(world["md"])[0]]
    assert any("still being written" in s for s in res["skipped"])
    assert SL.restore_check(dest, rec)["ok"]
    again = SL.backup(world["md"], dest, rec, now_ms=NOW)             # idempotent: nothing new, nothing overwritten
    assert again["backed_up"] == [] and any("already backed up" in s for s in again["skipped"])


def _flip(path):
    """Invert 8 bytes in the middle of the file: always a real change, never a no-op write of bytes that were already there."""
    with open(path, "r+b") as fh:
        fh.seek(os.path.getsize(path) // 2)
        b = fh.read(8); fh.seek(-len(b), 1); fh.write(bytes(x ^ 0xFF for x in b))


def test_a_corrupt_copy_is_rejected_and_set_aside_not_receipted(world):
    dest = str(world["tmp"] / "offvol"); rec = str(world["tmp"] / "off" / "receipts.jsonl")
    os.makedirs(os.path.dirname(rec), exist_ok=True)

    def flaky(src, dst):
        shutil.copyfile(src, dst)
        _flip(dst)
    with pytest.raises(SL.SealError, match="failed verification"):
        SL.backup(world["md"], dest, rec, now_ms=NOW, copy_fn=flaky)
    assert not os.path.exists(rec) or not open(rec).read().strip()
    assert any(f.endswith(".REJECTED") for f in os.listdir(dest))


def test_restore_check_catches_a_backup_that_decayed_after_it_was_receipted(world):
    dest = str(world["tmp"] / "offvol"); rec = str(world["tmp"] / "off" / "receipts.jsonl")
    os.makedirs(os.path.dirname(rec), exist_ok=True)
    SL.backup(world["md"], dest, rec, now_ms=NOW)
    assert SL.restore_check(dest, rec)["ok"]
    name = SL.segment_names(dest)[0]
    _flip(os.path.join(dest, name))
    bad = SL.restore_check(dest, rec)
    assert not bad["ok"] and bad["problems"]
    os.remove(os.path.join(dest, name))
    assert any("missing from the backup" in p for p in SL.restore_check(dest, rec)["problems"])


def test_a_backup_destination_inside_the_recorders_directory_is_refused(world):
    with pytest.raises(SL.SealError, match="beside the original"):
        SL.backup(world["md"], os.path.join(world["md"], "bk"), str(world["tmp"] / "r.jsonl"), now_ms=NOW)


def test_the_tool_never_writes_into_the_recorders_directory(world):
    side = ("-wal", "-shm")         # SQLite may create these when it opens a WAL database read-only; they are not data and not written by this tool

    def names():
        return sorted(f for f in os.listdir(world["md"]) if not f.endswith(side))
    before = names()
    seal(world); SL.check(world["md"], world["manifest"])
    SL.backup(world["md"], str(world["tmp"] / "offvol"), str(world["tmp"] / "off" / "r.jsonl"), now_ms=NOW)
    assert names() == before


# ---------------------------------------------------------------- red-team reproductions (8c7a716)

def test_backup_of_a_segment_the_writer_still_has_open_keeps_every_record(tmp_path):
    """Reproduced: a plain copy of the main .db lost 10 of 20 records (the rest were in the WAL) and the receipt + restore-check still passed."""
    md = tmp_path / "md"
    st = ST.MarketStore(str(md), commit_every=1, environment="mainnet")
    for i in range(20):
        st.append("frame", wall_ms=DAY1 + i * 1000, mono_s=float(i), category="linear", conn_id="c", topic="t", payload=f'{{"i":{i}}}')
        if i == 9:
            c = sqlite3.connect(os.path.join(str(md), SL.segment_names(str(md))[0])); c.execute("PRAGMA wal_checkpoint(PASSIVE)"); c.close()
    # the writer is deliberately NOT closed: yesterday's segment is still open in WAL mode
    dest = str(tmp_path / "offvol"); rec = str(tmp_path / "off" / "r.jsonl"); os.makedirs(str(tmp_path / "off"))
    res = SL.backup(str(md), dest, rec, now_ms=DAY2 + 2 * 3_600_000)
    assert res["backed_up"][0]["records"] == 20
    assert SL.restore_check(dest, rec)["ok"]
    # and a copy function that silently drops records is now caught by the content comparison
    def lossy(src, dst):
        SL.online_copy(src, dst)
        c = sqlite3.connect(dst)
        for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
            c.execute(f'DROP TRIGGER "{n}"')
        c.execute("DELETE FROM log WHERE id > 10"); c.commit(); c.close()
    with pytest.raises(SL.SealError, match="records were lost"):
        SL.backup(str(md), str(tmp_path / "off2"), str(tmp_path / "off" / "r2.jsonl"), now_ms=DAY2 + 2 * 3_600_000, copy_fn=lossy)
    st.close()


def test_a_day_that_ended_minutes_ago_is_not_yet_closed(world):
    res = SL.backup(world["md"], str(world["tmp"] / "offvol"), str(world["tmp"] / "off_r.jsonl"), now_ms=DAY2 - (DAY2 % 86_400_000) + 60_000)
    assert res["backed_up"] == [] and any("still being written" in x for x in res["skipped"])
    later = SL.backup(world["md"], str(world["tmp"] / "offvol"), str(world["tmp"] / "off_r.jsonl"), now_ms=DAY2 + 3_600_000)     # an hour+ after midnight: now it is closed
    assert [d["segment"] for d in later["backed_up"]] == [SL.segment_names(world["md"])[0]]


def test_sealing_over_a_truncation_is_refused_so_it_cannot_become_the_new_baseline(world):
    seal(world)
    _truncate(seg(world, 1), keep=3)
    assert not SL.check(world["md"], world["manifest"])["ok"]
    with pytest.raises(SL.SealError, match="refusing to seal over existing problems"):
        SL.seal(world["md"], world["manifest"], now_ms=NOW)
    assert not SL.check(world["md"], world["manifest"])["ok"]                       # still detected afterwards
    SL.seal(world["md"], world["manifest"], now_ms=NOW, accept_problems=True)        # the owner's explicit re-baseline


def test_every_earlier_seal_is_checked_not_only_the_latest(world):
    seal(world)
    s = ST.MarketStore(world["md"], commit_every=1, environment="mainnet")
    s.append("frame", wall_ms=DAY2 + 50_000, mono_s=50.0, category="linear", conn_id="c1", topic="t", payload="{}"); s.close()
    SL.seal(world["md"], world["manifest"], now_ms=NOW)                              # a second seal with 5 records on day 2
    _truncate(seg(world, 2), keep=2)                                                 # cut below BOTH seals
    probs = SL.check(world["md"], world["manifest"])["problems"]
    assert any("TRUNCATED" in p for p in probs)


def test_an_edit_inside_an_open_segment_is_caught_by_the_deep_check_only(world):
    seal(world)
    con = sqlite3.connect(seg(world, 2))
    for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
        con.execute(f'DROP TRIGGER "{n}"')
    con.execute("UPDATE log SET topic='tampered' WHERE id=12"); con.commit(); con.close()      # ids are global across segments: day 2 holds 11-14
    assert SL.check(world["md"], world["manifest"])["ok"], "the cheap check cannot see a mid-segment edit (documented)"
    deep = SL.check(world["md"], world["manifest"], deep=True)
    assert not deep["ok"] and any("chain mismatch" in p for p in deep["problems"])


def test_a_closed_day_that_gained_a_record_is_altered(world):
    seal(world)
    con = sqlite3.connect(seg(world, 1))
    for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
        con.execute(f'DROP TRIGGER "{n}"')
    con.execute("INSERT INTO log (id, kind, wall_ms, mono_s, category, conn_id, topic, payload, chain) SELECT 999, kind, wall_ms, mono_s, category, conn_id, topic, payload, chain FROM log WHERE id=1")
    con.commit(); con.close()
    probs = SL.check(world["md"], world["manifest"])["problems"]
    assert any("ALTERED" in p and "records" in p for p in probs), probs
