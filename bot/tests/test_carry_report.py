"""The report calls a net REALISED only when every fee is confirmed."""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from persistence import StateStore  # noqa: E402
from test_carry_fee_truth import absent, build, modern, open_the_hedge  # noqa: E402
from tools import carry_report  # noqa: E402


@pytest.mark.parametrize("fees,status", [
    (modern, "REALISED"), (absent, "PROVISIONAL_FEES_INCOMPLETE")])
def test_status_follows_fee_confirmation_across_a_real_database(
        tmp_path, fees, status):
    _venue, journal, engine = build(fees)
    open_the_hedge(engine)
    db = str(tmp_path / "run.db")
    store = StateStore(db)
    store.save_ledger(journal.to_rows())
    store.close()
    report = carry_report.build_report(db)
    assert report["carry"]["status"] == status
    assert [f["kind"] for f in report["carry"]["fills"]] == [
        "spot_buy", "perp_open"]


def test_a_missing_database_is_an_error_not_an_empty_report(tmp_path, capsys):
    assert carry_report.main([str(tmp_path / "nope.db")]) == 2


def test_it_opens_the_database_read_only(tmp_path):
    db = str(tmp_path / "run.db")
    StateStore(db).close()
    before = os.path.getmtime(db)
    carry_report.build_report(db)
    assert os.path.getmtime(db) == before


def test_a_book_that_never_traded_is_NO_FILLS_not_REALISED(tmp_path):
    """The vacuous-truth trap: zero fills means zero unconfirmed fees, and that
    printed REALISED on a real Codespaces run in which nothing was ordered."""
    import ledger as L
    db = str(tmp_path / "idle.db")
    store = StateStore(db)
    store.save_ledger(L.Journal().to_rows())
    store.close()
    report = carry_report.build_report(db)
    assert report["carry"]["status"] == "NO_FILLS"
    assert L.Journal().statement()["net_basis"] == "no_fills"
