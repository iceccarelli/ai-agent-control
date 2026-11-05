"""The suite cannot open the tracked state database, and cannot reach a venue."""
import os
import sqlite3

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_opening_the_tracked_state_database_is_refused_before_sqlite_sees_it():
    with pytest.raises(AssertionError, match="never a test database"):
        sqlite3.connect(os.path.join(BOT, "state", "trading_state.db"))
    with pytest.raises(AssertionError, match="never a test database"):
        sqlite3.connect(os.path.join(BOT, "state", "..", "state", "other.db"))


def test_a_database_under_tmp_path_opens_normally(tmp_path):
    con = sqlite3.connect(str(tmp_path / "ok.db"))
    con.execute("CREATE TABLE t (x)")
    con.close()


def test_the_default_state_path_resolves_into_the_guarded_directory():
    import config as C
    assert C.load({}).STATE_DB_PATH in ("state/trading_state.db", os.path.join("state", "trading_state.db"))


def test_the_requests_layer_refuses_to_leave_the_machine():
    import requests
    with pytest.raises(RuntimeError, match="offline test run"):
        requests.Session().request("GET", "https://api.bybit.com/v5/market/time")
