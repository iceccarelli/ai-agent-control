import hashlib
import json
import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import state_backup as SB  # noqa: E402


def make_db(path, wal=False):
    con = sqlite3.connect(path)
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, link TEXT)")
    con.execute("CREATE TABLE kill_switch (id INTEGER PRIMARY KEY, engaged INTEGER)")
    con.executemany("INSERT INTO orders (link) VALUES (?)", [(f"L{i}",) for i in range(25)])
    con.execute("INSERT INTO kill_switch VALUES (1, 0)")
    con.commit()
    con.close()


def digest(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest(), os.stat(path).st_mtime_ns


@pytest.fixture
def live(tmp_path):
    d = tmp_path / "live"
    d.mkdir()
    p = str(d / "trading_state.db")
    make_db(p)
    return p


def test_backup_verify_and_restore_round_trip_and_never_touch_the_source(tmp_path, live):
    before = digest(live)
    m = SB.backup(live, str(tmp_path / "off_volume"), now=1_793_800_000)
    assert digest(live) == before                                           # the source was not written
    assert m["tables"] == {"kill_switch": 1, "orders": 25} and m["integrity_ok"] and len(m["sha256"]) == 64
    assert "contents" in m["note"] and "L1" not in json.dumps(m)           # counts only, never rows
    bf = os.path.join(tmp_path, "off_volume", m["backup_file"])
    assert SB.verify(bf)["ok"]
    r = SB.restore(bf, str(tmp_path / "rehearsal"))
    assert r["ok"] and r["tables"] == m["tables"]
    assert sqlite3.connect(r["restored_to"]).execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 25
    assert digest(live) == before


def test_a_live_wal_database_is_backed_up_consistently(tmp_path):
    p = str(tmp_path / "live" / "trading_state.db")
    os.makedirs(os.path.dirname(p))
    make_db(p, wal=True)
    writer = sqlite3.connect(p)
    writer.execute("INSERT INTO orders (link) VALUES ('late')")
    writer.commit()                                                          # the writer is still open
    m = SB.backup(p, str(tmp_path / "out"))
    assert m["tables"]["orders"] == 26
    writer.close()


def test_a_backup_beside_the_ledger_is_refused(tmp_path, live):
    for out in (os.path.dirname(live), os.path.join(os.path.dirname(live), "backups")):
        with pytest.raises(SystemExit, match="own directory"):
            SB.backup(live, out)


def test_tampering_with_the_backup_is_detected(tmp_path, live):
    m = SB.backup(live, str(tmp_path / "out"))
    bf = os.path.join(tmp_path, "out", m["backup_file"])
    con = sqlite3.connect(bf)
    con.execute("DELETE FROM orders WHERE id = 3")
    con.commit(); con.close()
    v = SB.verify(bf)
    assert not v["ok"] and any("SHA-256" in p for p in v["problems"])
    with pytest.raises(SystemExit, match="does not verify"):
        SB.restore(bf, str(tmp_path / "r"))


def test_a_missing_manifest_or_a_non_database_does_not_verify(tmp_path, live):
    m = SB.backup(live, str(tmp_path / "out"))
    bf = os.path.join(tmp_path, "out", m["backup_file"])
    os.remove(bf + SB.MANIFEST_SUFFIX)
    assert not SB.verify(bf)["ok"]
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not sqlite" * 100)
    json.dump({"sha256": hashlib.sha256(junk.read_bytes()).hexdigest(), "tables": {}}, open(str(junk) + SB.MANIFEST_SUFFIX, "w"))
    assert not SB.verify(str(junk))["ok"]


def test_restore_only_ever_goes_into_a_new_or_empty_disposable_directory(tmp_path, live):
    m = SB.backup(live, str(tmp_path / "out"))
    bf = os.path.join(tmp_path, "out", m["backup_file"])
    full = tmp_path / "full"
    full.mkdir()
    (full / "x").write_text("occupied")
    with pytest.raises(SystemExit, match="not an empty directory"):
        SB.restore(bf, str(full))
    with pytest.raises(SystemExit, match="live state directory"):
        SB.restore(bf, str(tmp_path / "state"))
    with pytest.raises(SystemExit, match="live state directory"):
        SB.restore(bf, "/app/state")
    empty = tmp_path / "empty"
    empty.mkdir()
    assert SB.restore(bf, str(empty))["ok"]


def test_the_cli_has_no_default_path_and_reports_failure_in_its_exit_code(tmp_path, live, capsys):
    with pytest.raises(SystemExit):
        SB.main(["backup", "--out-dir", str(tmp_path)])                      # --db is required
    assert SB.main(["backup", "--db", live, "--out-dir", str(tmp_path / "o")]) == 0
    bf = [f for f in os.listdir(tmp_path / "o") if f.endswith(".db")][0]
    assert SB.main(["verify", "--backup", str(tmp_path / "o" / bf)]) == 0
    open(tmp_path / "o" / bf, "ab").write(b"x")
    assert SB.main(["verify", "--backup", str(tmp_path / "o" / bf)]) == 1
