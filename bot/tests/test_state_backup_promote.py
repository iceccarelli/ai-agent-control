"""state_backup.promote: the owner-confirmed, cold-only step that installs a restored database. Temp directories only; the tracked state directory is never touched."""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import state_backup as SB  # noqa: E402


def make_db(path, n=5):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, link TEXT)")
    con.executemany("INSERT INTO orders (link) VALUES (?)", [(f"L{i}",) for i in range(n)])
    con.execute("PRAGMA user_version = 7")
    con.commit()
    con.close()


@pytest.fixture
def world(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    db = str(src / "s.db")
    make_db(db)
    m = SB.backup(db, str(tmp_path / "bk"))
    bk = str(tmp_path / "bk" / m["backup_file"])
    rest = SB.restore(bk, str(tmp_path / "restored"))
    live_dir = tmp_path / "live"; live_dir.mkdir()
    live = str(live_dir / "live.db")
    make_db(live, n=2)                                         # the "damaged" live database to be replaced
    return {"restored": rest["restored_to"], "live": live, "backup": bk, "tmp": tmp_path}


def rows(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    finally:
        con.close()


def test_promote_installs_the_restored_database_and_keeps_the_old_one(world):
    res = SB.promote(world["restored"], world["live"], confirm_owner=True, now=1_700_000_000)
    assert res["ok"] and rows(world["live"]) == 5
    assert res["previous_kept_as"] and rows(res["previous_kept_as"]) == 2          # renamed, never deleted
    assert not [f for f in os.listdir(os.path.dirname(world["live"])) if f.startswith(".promote-")]


def test_promote_needs_the_owners_confirmation_and_changes_nothing_without_it(world):
    with pytest.raises(SystemExit, match="confirm-owner"):
        SB.promote(world["restored"], world["live"], confirm_owner=False)
    assert rows(world["live"]) == 2


@pytest.mark.parametrize("side", ["-wal", "-shm", "-journal"])
def test_promote_refuses_while_a_side_file_says_a_writer_is_or_was_there(world, side):
    open(world["live"] + side, "wb").close()
    with pytest.raises(SystemExit, match="stop the machine"):
        SB.promote(world["restored"], world["live"], confirm_owner=True)
    assert rows(world["live"]) == 2


def test_promote_refuses_while_another_connection_holds_the_live_database(world):
    holder = sqlite3.connect(world["live"])
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(SystemExit, match="in use"):
            SB.promote(world["restored"], world["live"], confirm_owner=True)
    finally:
        holder.rollback(); holder.close()
    assert rows(world["live"]) == 2


def test_promote_refuses_a_restored_file_that_changed_after_its_manifest(world):
    with open(world["restored"], "ab") as fh:
        fh.write(b"x")
    with pytest.raises(SystemExit, match="does not verify"):
        SB.promote(world["restored"], world["live"], confirm_owner=True)
    assert rows(world["live"]) == 2


def test_promote_refuses_a_live_path_that_is_a_link_or_a_missing_directory(world, tmp_path):
    link = tmp_path / "link.db"; os.symlink(world["live"], link)
    with pytest.raises(SystemExit):
        SB.promote(world["restored"], str(link), confirm_owner=True)
    with pytest.raises(SystemExit):
        SB.promote(world["restored"], str(tmp_path / "nope" / "x.db"), confirm_owner=True)


def test_promote_onto_an_absent_live_file_installs_without_a_kept_copy(world, tmp_path):
    fresh = str(tmp_path / "live" / "brand_new.db")
    res = SB.promote(world["restored"], fresh, confirm_owner=True)
    assert res["ok"] and res["previous_kept_as"] is None and rows(fresh) == 5


def test_verify_catches_a_swapped_newest_row_even_when_hash_and_counts_are_made_to_agree(world):
    bk = world["backup"]
    con = sqlite3.connect(bk); con.execute("DELETE FROM orders WHERE id = 5"); con.execute("INSERT INTO orders VALUES (99, 'x')"); con.commit(); con.close()
    mp = bk + SB.MANIFEST_SUFFIX
    m = json.load(open(mp)); m["sha256"] = SB._sha256(bk); json.dump(m, open(mp, "w"))      # an attacker/accident that fixes the hash
    res = SB.verify(bk)
    assert not res["ok"] and any("newest row ids" in p for p in res["problems"])


def test_verify_catches_a_schema_version_change_and_warns_on_an_old_manifest(world):
    bk = world["backup"]
    con = sqlite3.connect(bk); con.execute("PRAGMA user_version = 8"); con.commit(); con.close()
    mp = bk + SB.MANIFEST_SUFFIX
    m = json.load(open(mp)); m["sha256"] = SB._sha256(bk); json.dump(m, open(mp, "w"))
    assert any("schema version" in p for p in SB.verify(bk)["problems"])
    con = sqlite3.connect(bk); con.execute("PRAGMA user_version = 7"); con.commit(); con.close()
    m = json.load(open(mp)); m["sha256"] = SB._sha256(bk); m.pop("max_rowid"); m.pop("user_version"); json.dump(m, open(mp, "w"))
    old = SB.verify(bk)
    assert old["ok"] and old["warnings"]                         # accepted, but never silently
