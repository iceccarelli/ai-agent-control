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
    seal(world); M = world["manifest"]
    res = SL.backup(world["md"], dest, rec, manifest=M, now_ms=NOW)
    assert [d["segment"] for d in res["backed_up"]] == [SL.segment_names(world["md"])[0]]
    assert any("still being written" in s for s in res["skipped"])
    assert SL.restore_check(dest, rec, M)["ok"]
    again = SL.backup(world["md"], dest, rec, manifest=M, now_ms=NOW)             # idempotent: nothing new, nothing overwritten
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
    seal(world)
    with pytest.raises(SL.SealError, match="failed verification"):
        SL.backup(world["md"], dest, rec, manifest=world["manifest"], now_ms=NOW, copy_fn=flaky)
    assert not os.path.exists(rec) or not open(rec).read().strip()
    assert any(f.endswith(".REJECTED") for f in os.listdir(dest))


def test_restore_check_catches_a_backup_that_decayed_after_it_was_receipted(world):
    dest = str(world["tmp"] / "offvol"); rec = str(world["tmp"] / "off" / "receipts.jsonl")
    os.makedirs(os.path.dirname(rec), exist_ok=True)
    seal(world); M = world["manifest"]
    SL.backup(world["md"], dest, rec, manifest=M, now_ms=NOW)
    assert SL.restore_check(dest, rec, M)["ok"]
    name = SL.segment_names(dest)[0]
    _flip(os.path.join(dest, name))
    bad = SL.restore_check(dest, rec, M)
    assert not bad["ok"] and bad["problems"]
    os.remove(os.path.join(dest, name))
    assert any("missing from the backup" in p for p in SL.restore_check(dest, rec, M)["problems"])


def test_a_backup_destination_inside_the_recorders_directory_is_refused(world):
    with pytest.raises(SL.SealError, match="beside the original"):
        SL.backup(world["md"], os.path.join(world["md"], "bk"), str(world["tmp"] / "r.jsonl"), manifest=world["manifest"], now_ms=NOW)


def test_the_tool_never_writes_into_the_recorders_directory(world):
    side = ("-wal", "-shm")         # SQLite may create these when it opens a WAL database read-only; they are not data and not written by this tool

    def names():
        return sorted(f for f in os.listdir(world["md"]) if not f.endswith(side))
    before = names()
    seal(world); SL.check(world["md"], world["manifest"])
    SL.backup(world["md"], str(world["tmp"] / "offvol"), str(world["tmp"] / "off" / "r.jsonl"), manifest=world["manifest"], now_ms=NOW)
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
    M = str(tmp_path / "off" / "manifest.jsonl"); LATER = DAY2 + 2 * 3_600_000
    SL.seal(str(md), M, now_ms=LATER)
    res = SL.backup(str(md), dest, rec, manifest=M, now_ms=LATER)
    assert res["backed_up"][0]["records"] == 20
    assert SL.restore_check(dest, rec, M)["ok"]
    # and a copy function that silently drops records is now caught by the content comparison
    def lossy(src, dst):
        SL.online_copy(src, dst)
        c = sqlite3.connect(dst)
        for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
            c.execute(f'DROP TRIGGER "{n}"')
        c.execute("DELETE FROM log WHERE id > 10"); c.commit(); c.close()
    with pytest.raises(SL.SealError, match="records were lost"):
        SL.backup(str(md), str(tmp_path / "off2"), str(tmp_path / "off" / "r2.jsonl"), manifest=M, now_ms=LATER, copy_fn=lossy)
    st.close()


def test_a_day_that_ended_minutes_ago_is_not_yet_closed(world):
    seal(world)
    res = SL.backup(world["md"], str(world["tmp"] / "offvol"), str(world["tmp"] / "off_r.jsonl"), manifest=world["manifest"], now_ms=DAY2 - (DAY2 % 86_400_000) + 60_000)
    assert res["backed_up"] == [] and any("still being written" in x for x in res["skipped"])
    later = SL.backup(world["md"], str(world["tmp"] / "offvol"), str(world["tmp"] / "off_r.jsonl"), manifest=world["manifest"], now_ms=DAY2 + 3_600_000)     # an hour+ after midnight: now it is closed
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


def test_a_bit_flipped_segment_is_reported_by_the_deep_check_not_raised(world):
    """Reproduced by the lifecycle worker: check(deep=True) on a bit-flipped original raised sqlite3.DatabaseError instead of returning ok=False."""
    seal(world)
    _flip(seg(world, 1))
    res = SL.check(world["md"], world["manifest"], deep=True)
    assert res["ok"] is False and any("unreadable" in p or "ALTERED" in p or "chain mismatch" in p for p in res["problems"])


# ---------------------------------------------------------------- red-team round 2 (c8e6cbd)

def _rec(world):
    r = str(world["tmp"] / "off" / "receipts.jsonl")
    os.makedirs(os.path.dirname(r), exist_ok=True)
    return r


def test_backup_of_a_truncated_sealed_day_is_refused_and_restore_check_would_not_pass(world):
    """Reproduced: backup never consulted the manifest, so a truncated sealed closed day was copied, receipted and restore-checked OK."""
    seal(world)
    _truncate(seg(world, 1), keep=5)
    dest, rec = str(world["tmp"] / "bk"), _rec(world)
    with pytest.raises(SL.SealError, match="refusing to back up"):
        SL.backup(world["md"], dest, rec, manifest=world["manifest"], now_ms=NOW)
    assert not os.path.exists(rec) and (not os.path.isdir(dest) or os.listdir(dest) == [])
    # positive control: the same call on an untouched directory works
    ok = {"md": make_store(world["tmp"] / "md2"), "manifest": str(world["tmp"] / "off" / "m2.jsonl"), "tmp": world["tmp"]}
    SL.seal(ok["md"], ok["manifest"], now_ms=NOW)
    assert SL.backup(ok["md"], str(world["tmp"] / "bk2"), str(world["tmp"] / "off" / "r2.jsonl"), manifest=ok["manifest"], now_ms=NOW)["ok"]


def test_backup_needs_a_manifest_and_skips_days_not_sealed_as_closed(world):
    dest, rec = str(world["tmp"] / "bk"), _rec(world)
    with pytest.raises(SL.SealError, match="refusing to back up"):
        SL.backup(world["md"], dest, rec, manifest=world["manifest"], now_ms=NOW)              # no manifest at all
    os.makedirs(os.path.dirname(world["manifest"]), exist_ok=True)
    SL.seal(world["md"], world["manifest"], now_ms=DAY1 + 3_600_000)                            # sealed while day 1 was still open
    res = SL.backup(world["md"], dest, rec, manifest=world["manifest"], now_ms=NOW)
    assert res["ok"] is False and res["backed_up"] == [] and any("NOT SEALED" in x for x in res["skipped"])


def test_restore_check_requires_a_matching_receipt_for_every_sealed_closed_day(world):
    import evidence_chain as EC
    seal(world); M = world["manifest"]
    dest, rec = str(world["tmp"] / "bk"), _rec(world)
    os.makedirs(dest)
    # a valid-looking receipts file that covers nothing the manifest sealed
    EC.append(rec, {"at_ms": 1, "segment": "md-19990101.db", "sha256": "0" * 64, "records": 0, "last_id": None, "last_chain": ""})
    res = SL.restore_check(dest, rec, M)
    assert not res["ok"] and any("NO receipt" in p for p in res["problems"])
    # a receipt for the right day with the wrong count (a truncated copy that was receipted) is not enough either
    name = SL.segment_names(world["md"])[0]
    d = SL.describe(world["md"], name, closed=False)
    EC.append(rec, {"at_ms": 2, "segment": name, "sha256": "0" * 64, "records": d["records"] - 5, "last_id": d["last_id"] - 5, "last_chain": d["last_chain"]})
    res = SL.restore_check(dest, rec, M)
    assert any("records" in p and "differs from the sealed value" in p for p in res["problems"])
    # no manifest => not ok
    assert not SL.restore_check(dest, rec, str(world["tmp"] / "nope.jsonl"))["ok"]


def _fabricate(world, wall_ms):
    import md_store as ST2
    fd = str(world["tmp"] / "fab")
    s = ST2.MarketStore(fd, commit_every=1, environment="mainnet")
    for i in range(3):
        s.append("frame", wall_ms=wall_ms + i, mono_s=float(i), category="linear", conn_id="c", topic="tickers.BTCUSDT", payload='{"fake":%d}' % i)
    s.close()
    day = ST2._utc_day(wall_ms)
    shutil.copy(os.path.join(fd, f"md-{day}.db"), world["md"])
    return f"md-{day}.db"


def test_a_backdated_fabricated_segment_is_flagged_by_check_seal_and_backup(world):
    """Reproduced: a segment copied in with its own chain_start (older than the newest sealed day) passed check/backup/restore-check and could be sealed over."""
    seal(world)
    assert SL.check(world["md"], world["manifest"], deep=True)["ok"]                          # positive control: the honest directory is clean
    name = _fabricate(world, DAY1 - 5 * 86_400_000)
    chk = SL.check(world["md"], world["manifest"], deep=True)
    assert not chk["ok"] and any("UNSEALED" in p and name in p for p in chk["problems"]), chk
    assert any("CHAIN BREAK" in p for p in chk["problems"])
    with pytest.raises(SL.SealError):
        SL.backup(world["md"], str(world["tmp"] / "bk"), _rec(world), manifest=world["manifest"], now_ms=NOW)
    with pytest.raises(SL.SealError):
        SL.seal(world["md"], world["manifest"], now_ms=NOW)


def test_a_first_seal_refuses_segments_that_do_not_chain(world):
    _fabricate(world, DAY1 - 5 * 86_400_000)
    os.makedirs(os.path.dirname(world["manifest"]), exist_ok=True)
    with pytest.raises(SL.SealError, match="do not chain"):
        SL.seal(world["md"], world["manifest"], now_ms=NOW)


def test_a_segment_with_a_foreign_chain_start_after_the_sealed_history_is_flagged(world):
    seal(world)
    _fabricate(world, DAY2 + 3 * 86_400_000)                                                   # newer than everything sealed, but its chain_start is its own
    probs = SL.check(world["md"], world["manifest"])["problems"]
    assert any("CHAIN BREAK" in p for p in probs) and any("ID GAP" in p for p in probs), probs


def test_a_second_rejected_copy_does_not_overwrite_the_first(world):
    """Reproduced: os.rename(dst, dst + '.REJECTED') replaced an earlier .REJECTED (the evidence of the first failure)."""
    seal(world)
    dest, rec = str(world["tmp"] / "bk"), _rec(world)

    def bad(payload):
        def f(src, dst):
            with open(dst, "wb") as fh:
                fh.write(payload)
        return f
    for payload in (b"FIRST-REJECTED-EVIDENCE", b"SECOND"):
        with pytest.raises(SL.SealError):
            SL.backup(world["md"], dest, rec, manifest=world["manifest"], now_ms=NOW, copy_fn=bad(payload))
    name = SL.segment_names(world["md"])[0]
    assert open(os.path.join(dest, name + ".REJECTED"), "rb").read() == b"FIRST-REJECTED-EVIDENCE"
    assert open(os.path.join(dest, name + ".REJECTED.2"), "rb").read() == b"SECOND"


def test_seal_without_an_explicit_clock_never_closes_the_newest_day(world):
    """A wall clock stepped forward would otherwise hash-pin a day the recorder is still writing (all of 2023 is 'long ago' to the real clock)."""
    import evidence_chain as EC
    os.makedirs(os.path.dirname(world["manifest"]), exist_ok=True)
    SL.seal(world["md"], world["manifest"])                                                     # no now_ms: real clock, far in the future of the fixture data
    s1, s2 = EC.rows_of(EC.read_lines(world["manifest"])[0])[0]["segments"]
    assert s1["closed"] is True and "sha256" in s1 and s2["closed"] is False and "sha256" not in s2
    st = ST.MarketStore(world["md"], commit_every=1, environment="mainnet")
    st.append("frame", wall_ms=DAY2 + 99_000, mono_s=99.0, category="linear", conn_id="c1", topic="t", payload="{}"); st.close()
    assert SL.check(world["md"], world["manifest"])["ok"]                                       # the writer's growth is not 'ALTERED'
    # an explicit clock closes it (owner's decision after the recorder stopped)
    SL.seal(world["md"], world["manifest"], now_ms=NOW + 2 * 86_400_000)
    assert EC.rows_of(EC.read_lines(world["manifest"])[0])[-1]["segments"][-1]["closed"] is True


# ---------------------------------------------------------------- round 3: retention pruning, manifest location, future segments

def _prune_oldest(world):
    st = ST.MarketStore(world["md"], commit_every=1, environment="mainnet")
    try:
        return st.prune_segments_before(ST._utc_day(DAY2))
    finally:
        st.close()


def test_retention_pruning_after_a_verified_backup_is_not_a_deadlock(world):
    """Reproduced: the recorder's own --retention-days pruned a sealed day, check said MISSING, and seal and backup then refused forever."""
    import evidence_chain as EC
    seal(world); M = world["manifest"]
    dest, rec = str(world["tmp"] / "bk"), _rec(world)
    SL.backup(world["md"], dest, rec, manifest=M, now_ms=NOW)                                  # backup BEFORE retention
    assert _prune_oldest(world), "positive control: the recorder really pruned the oldest day"
    chk = SL.check(world["md"], M, receipts=rec)
    assert chk["ok"], chk
    SL.seal(world["md"], M, now_ms=NOW, receipts=rec)                                           # and sealing works again
    assert SL.check(world["md"], M, receipts=rec)["ok"]
    assert SL.restore_check(dest, rec, M)["ok"]                                                 # the copy still proves the pruned day
    # without the receipts the pruned day is still reported (the receipts are what excuse it)
    assert not SL.check(world["md"], M)["ok"]
    assert EC.verify(rec)["ok"]


def test_a_pruned_day_without_a_backup_receipt_stays_a_loud_failure(world):
    seal(world); M = world["manifest"]
    rec = _rec(world)
    assert _prune_oldest(world)
    for r in (None, rec):                                                                       # no receipts at all / an empty receipts file
        chk = SL.check(world["md"], M, receipts=r)
        assert not chk["ok"]
        msg = " ".join(chk["problems"])
        assert "MISSING" in msg and "NOT backed up" in msg and "retention" in msg, msg
    with pytest.raises(SL.SealError):
        SL.backup(world["md"], str(world["tmp"] / "bk"), rec, manifest=M, now_ms=NOW)


def test_a_deleted_day_is_not_excused_by_a_forged_pruned_jsonl_or_a_mismatched_receipt(world):
    import evidence_chain as EC
    seal(world); M = world["manifest"]
    dest, rec = str(world["tmp"] / "bk"), _rec(world)
    SL.backup(world["md"], dest, rec, manifest=M, now_ms=NOW)
    name = SL.segment_names(world["md"])[0]
    # delete the OLDEST day and forge a pruned.jsonl, but receipt from a DIFFERENT chain value
    os.remove(os.path.join(world["md"], name))
    rows = EC.rows_of(EC.read_lines(rec)[0])
    forged = str(world["tmp"] / "forged.jsonl")
    EC.append(forged, dict(rows[0], last_chain="ab" * 32))
    assert not SL.check(world["md"], M, receipts=forged)["ok"]
    # positive control: the genuine receipt excuses it, because the next day's chain_start equals its last_chain
    assert SL.check(world["md"], M, receipts=rec)["ok"]


def test_a_missing_day_that_is_not_the_oldest_is_never_excused(world):
    seal(world); M = world["manifest"]
    dest, rec = str(world["tmp"] / "bk"), _rec(world)
    SL.backup(world["md"], dest, rec, manifest=M, now_ms=NOW)
    names = SL.segment_names(world["md"])
    os.remove(os.path.join(world["md"], names[1]))                                              # the newer day vanished, the older remains
    chk = SL.check(world["md"], M, receipts=rec)
    assert not chk["ok"] and any("MISSING" in p and names[1] in p for p in chk["problems"])


def test_the_manifest_may_not_hide_behind_a_symlink_or_a_subdirectory_of_the_recorder(world):
    """Reproduced: abspath(dirname(manifest)) != recorder dir for an alias or a subdir, so seal wrote into the recorder's directory."""
    os.symlink(world["md"], str(world["tmp"] / "alias"))
    before = sorted(os.listdir(world["md"]))
    with pytest.raises(SL.SealError, match="outside memory"):
        SL.seal(world["md"], str(world["tmp"] / "alias" / "m2.jsonl"), now_ms=NOW)
    os.makedirs(os.path.join(world["md"], "sub"))
    with pytest.raises(SL.SealError, match="outside memory"):
        SL.seal(world["md"], os.path.join(world["md"], "sub", "m3.jsonl"), now_ms=NOW)
    os.makedirs(str(world["tmp"] / "off"), exist_ok=True)
    os.symlink(os.path.join(world["md"], "m4.jsonl"), str(world["tmp"] / "off" / "link.jsonl"))   # the manifest file itself is a link into the directory
    with pytest.raises(SL.SealError, match="outside memory"):
        SL.seal(world["md"], str(world["tmp"] / "off" / "link.jsonl"), now_ms=NOW)
    assert sorted(os.listdir(world["md"])) == sorted(before + ["sub"])
    seal(world)                                                                                 # positive control: a real outside manifest still seals
    with pytest.raises(SL.SealError, match="recorder"):
        SL.backup(world["md"], str(world["tmp"] / "bk"), os.path.join(world["md"], "sub", "r.jsonl"), manifest=world["manifest"], now_ms=NOW)


def test_a_fabricated_future_segment_is_flagged_and_does_not_change_the_newest(world):
    seal(world)
    assert SL.check(world["md"], world["manifest"], now_ms=NOW)["ok"]                           # positive control
    name = _fabricate(world, 2_900_000_000_000 * 10)                                            # year ~2920
    chk = SL.check(world["md"], world["manifest"], now_ms=NOW)
    assert not chk["ok"] and any("FUTURE" in p and name in p for p in chk["problems"]), chk
    assert not any("CHAIN BREAK" in p or "ID GAP" in p for p in chk["problems"]), chk                 # the fake day is reported once, not folded into the chain
    with pytest.raises(SL.SealError):
        SL.seal(world["md"], world["manifest"], now_ms=NOW)
    # a first seal over a directory with a future segment is refused too
    os.remove(os.path.join(world["md"], name))
    _fabricate(world, 2_900_000_000_000 * 10)
    os.makedirs(str(world["tmp"] / "off2"))
    with pytest.raises(SL.SealError, match="FUTURE"):
        SL.seal(world["md"], str(world["tmp"] / "off2" / "m.jsonl"), now_ms=NOW)


def test_pruning_is_not_excused_when_the_oldest_remaining_day_does_not_chain_from_the_receipted_one(world):
    seal(world); M = world["manifest"]
    dest, rec = str(world["tmp"] / "bk"), _rec(world)
    SL.backup(world["md"], dest, rec, manifest=M, now_ms=NOW)
    assert _prune_oldest(world) and SL.check(world["md"], M, receipts=rec)["ok"]               # positive control
    con = sqlite3.connect(os.path.join(world["md"], SL.segment_names(world["md"])[0]))
    con.execute("UPDATE meta SET value=? WHERE key='chain_start'", ("cd" * 32,)); con.commit(); con.close()
    chk = SL.check(world["md"], M, receipts=rec)
    assert not chk["ok"] and any("MISSING" in p for p in chk["problems"]) and any("CHAIN BREAK" in p for p in chk["problems"]), chk


def test_pruning_is_not_excused_by_receipts_whose_own_chain_does_not_verify(world):
    seal(world); M = world["manifest"]
    dest, rec = str(world["tmp"] / "bk"), _rec(world)
    SL.backup(world["md"], dest, rec, manifest=M, now_ms=NOW)
    assert _prune_oldest(world)
    raw = open(rec, "rb").read()
    open(rec, "wb").write(raw.replace(b'"at_ms":1700090000000', b'"at_ms":1700090000001'))  # edit a field that does not matter to the excuse: only the chain notices
    assert raw != open(rec, "rb").read()
    assert not SL.check(world["md"], M, receipts=rec)["ok"]
    open(rec, "wb").write(raw)
    assert SL.check(world["md"], M, receipts=rec)["ok"]


# ---------------------------------------------------------------- round 4: seal and check must agree; a receipt is not the copy

DAY3 = DAY2 + 86_400_000
NOW3 = DAY3 + 2 * 3_600_000


def _pruned_world_without_pruned_jsonl(world, backup_first=True):
    """The red-team scenario: days 1 and 2 are sealed closed (and backed up), day 3 starts AFTER the seal, retention prunes days 1-2, pruned.jsonl is absent. The oldest
    remaining day (3) was never sealed, so only the receipts can vouch for its chain_start."""
    M = world["manifest"]
    os.makedirs(os.path.dirname(M), exist_ok=True)
    SL.seal(world["md"], M, now_ms=NOW3)
    dest, rec = str(world["tmp"] / "bk"), _rec(world)
    if backup_first:
        SL.backup(world["md"], dest, rec, manifest=M, now_ms=NOW3)
    st = ST.MarketStore(world["md"], commit_every=1, environment="mainnet")
    for i in range(3):
        st.append("frame", wall_ms=DAY3 + i * 1000, mono_s=float(i), category="linear", conn_id="c1", topic="tickers.BTCUSDT", payload=f'{{"i":{i},"d":3}}')
    assert st.prune_segments_before(ST._utc_day(DAY3))
    st.close()
    pj = os.path.join(world["md"], "pruned.jsonl")
    if os.path.exists(pj):
        os.remove(pj)                                   # the recorder's own note is absent (restored dir / lost): only the receipts can vouch
    return M, dest, rec


def test_seal_agrees_with_check_after_retention_when_pruned_jsonl_is_absent(world):
    M, dest, rec = _pruned_world_without_pruned_jsonl(world)
    assert SL.check(world["md"], M, receipts=rec, now_ms=NOW3)["ok"]                           # positive control: check passes
    SL.seal(world["md"], M, now_ms=NOW3, receipts=rec)                                          # and so must seal (was a permanent refusal)
    assert SL.check(world["md"], M, receipts=rec, now_ms=NOW3)["ok"]


def test_seal_still_refuses_a_pruned_day_without_a_verified_receipt(world):
    M, dest, rec = _pruned_world_without_pruned_jsonl(world, backup_first=False)             # no backup ever made: no receipt
    assert not SL.check(world["md"], M, receipts=rec, now_ms=NOW3)["ok"]
    with pytest.raises(SL.SealError):
        SL.seal(world["md"], M, now_ms=NOW3, receipts=rec)


def test_a_receipt_alone_says_so_and_a_named_backup_must_hold_the_copy(world):
    M, dest, rec = _pruned_world_without_pruned_jsonl(world)
    plain = SL.check(world["md"], M, receipts=rec, now_ms=NOW3)
    assert plain["ok"] and "RECEIPTS ALONE" in " ".join(plain["notes"]) and "restore-check" in " ".join(plain["notes"])
    good = SL.check(world["md"], M, receipts=rec, backup=dest, now_ms=NOW3)                                  # positive control: the copy is there
    assert good["ok"] and "RECEIPTS ALONE" not in " ".join(good["notes"])
    name = SL.segment_names(dest)[0]
    with open(os.path.join(dest, name), "ab") as fh:
        fh.write(b"x")                                                                          # the copy no longer hashes as receipted
    assert not SL.check(world["md"], M, receipts=rec, backup=dest, now_ms=NOW3)["ok"]
    os.remove(os.path.join(dest, name))                                                         # the copy is gone altogether
    bad = SL.check(world["md"], M, receipts=rec, backup=dest, now_ms=NOW3)
    assert not bad["ok"] and "NOT excused" in " ".join(bad["problems"])
    with pytest.raises(SL.SealError):
        SL.seal(world["md"], M, receipts=rec, backup=dest, now_ms=NOW3)


def test_cli_check_accepts_backup(world, capsys):
    M, dest, rec = _pruned_world_without_pruned_jsonl(world)
    assert SL.main(["check", "--dir", world["md"], "--manifest", M, "--receipts", rec, "--backup", dest]) == 0
    os.remove(os.path.join(dest, SL.segment_names(dest)[0]))
    assert SL.main(["check", "--dir", world["md"], "--manifest", M, "--receipts", rec, "--backup", dest]) == 1
