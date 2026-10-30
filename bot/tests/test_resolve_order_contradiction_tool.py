"""The operator tool that clears a contradicted order: dry run by default,
written evidence required, local database only."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools"))

import order_lifecycle as ol                          # noqa: E402
from persistence import StateStore                    # noqa: E402
import resolve_order_contradiction as tool            # noqa: E402

from test_order_lifecycle import intent, ev, overfilled, LINK   # noqa: E402


@pytest.fixture()
def db(tmp_path):
    p = str(tmp_path / "s.db")
    s = StateStore(p)
    overfilled(s)
    s.close()
    return p


def state(db):
    s = StateStore(db)
    try:
        return s.get_order(LINK)
    finally:
        s.close()


def run(db, *extra):
    return tool.main(["--db", db, "--order-link-id", LINK,
                      "--verified-qty", "1.0", *extra])


def test_dry_run_writes_nothing(db, capsys):
    assert run(db, "--evidence", "execution list") == 0
    assert state(db)["status"] == "contradicted"
    assert "DRY RUN" in capsys.readouterr().out


def test_apply_with_evidence_sets_the_verified_quantity(db):
    assert run(db, "--evidence", "execution list", "--apply") == 0
    row = state(db)
    assert row["status"] == "filled" and row["filled_qty"] == 1.0


def test_blank_evidence_is_refused(db):
    assert run(db, "--evidence", "  ", "--apply") == 2
    assert state(db)["status"] == "contradicted"


def test_only_a_contradicted_order_is_resolved(tmp_path):
    p = str(tmp_path / "s.db")
    s = StateStore(p)
    intent(s)
    s.close()
    assert run(p, "--evidence", "x", "--apply") == 2
    assert state(p)["status"] == "pending"


def test_a_missing_database_is_refused(tmp_path):
    assert run(str(tmp_path / "nope.db"), "--evidence", "x") == 2
