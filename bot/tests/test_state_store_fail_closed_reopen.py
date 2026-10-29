"""A state DB that lost its history must not reopen with the kill switch clear.

``StateStore`` used to seed ``kill_switch`` with ``engaged=0`` whenever the row
was absent, so a deleted row (or a wiped volume, when the caller asked for a
guarded open) reopened as "trading permitted" with no signal that anything had
been lost. Offline: every database here lives in ``tmp_path``.
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import persistence  # noqa: E402
from persistence import StateStore  # noqa: E402


def _tripped_then_row_deleted(db):
    s = StateStore(str(db))
    s.trip_kill_switch("operator halt")
    s._conn.close()
    raw = sqlite3.connect(str(db))
    raw.execute("DELETE FROM kill_switch")
    raw.commit()
    raw.close()


def test_deleted_kill_switch_row_reseeds_engaged(tmp_path):
    db = tmp_path / "s.db"
    _tripped_then_row_deleted(db)
    engaged, reason = StateStore(str(db)).is_kill_switch_engaged()
    assert engaged is True
    assert "missing" in reason


def test_untouched_reopen_keeps_state_and_clear_stays_clear(tmp_path):
    db = tmp_path / "s.db"
    s = StateStore(str(db))
    s._conn.close()
    assert StateStore(str(db)).is_kill_switch_engaged() == (False, "")


def test_default_open_of_new_db_is_still_clear(tmp_path):
    # Paper/testnet first runs and every scratch database keep working.
    assert StateStore(str(tmp_path / "n.db")).is_kill_switch_engaged() == (False, "")


def test_engage_if_new_seeds_a_fresh_db_engaged(tmp_path):
    engaged, reason = StateStore(
        str(tmp_path / "n.db"), engage_if_new=True).is_kill_switch_engaged()
    assert engaged is True
    assert "newly created" in reason


def test_engage_if_new_treats_zero_byte_file_as_new(tmp_path):
    db = tmp_path / "z.db"
    db.write_bytes(b"")
    assert StateStore(str(db), engage_if_new=True).is_kill_switch_engaged()[0] is True


def test_engage_if_new_does_not_engage_an_initialized_db(tmp_path):
    db = tmp_path / "s.db"
    StateStore(str(db))._conn.close()
    assert StateStore(str(db), engage_if_new=True).is_kill_switch_engaged() == (False, "")


def test_engaged_state_survives_reopen_and_only_human_clears(tmp_path):
    db = tmp_path / "s.db"
    s = StateStore(str(db), engage_if_new=True)
    s._conn.close()
    again = StateStore(str(db), engage_if_new=True)
    assert again.is_kill_switch_engaged()[0] is True
    with pytest.raises(persistence.PersistenceError):
        again.clear_kill_switch_by_human("anything")
    again.clear_kill_switch_by_human("HUMAN_CLEARED_KILL_SWITCH")
    assert again.is_kill_switch_engaged() == (False, "")
