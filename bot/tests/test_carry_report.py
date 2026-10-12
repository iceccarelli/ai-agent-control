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


def test_estimated_or_unread_funding_blocks_REALISED(tmp_path):
    """Fees can all be venue-stated while the income line is still the
    engine's own number; that is not a realised result."""
    import ledger as L
    from test_carry_fee_truth import MARK, build, modern, open_the_hedge
    _v, journal, engine = build(modern)
    open_the_hedge(engine)
    L.funding(journal, ms=journal.entries[-1].ms + 1, ref="est", usd=3.0)
    db = str(tmp_path / "est.db")
    store = StateStore(db)
    store.save_ledger(journal.to_rows())
    store.close()
    c = carry_report.build_report(db)["carry"]
    assert c["status"] == "PROVISIONAL_FUNDING_UNVERIFIED"
    assert c["funding_estimated_usd"] == 3.0 and c["funding_venue_usd"] == 0.0


@pytest.mark.parametrize("fees,expected", [
    ("absent", "PROVISIONAL_FEES_INCOMPLETE"),
    ("modern", "PROVISIONAL_FUNDING_UNVERIFIED")])
def test_provisional_state_is_the_same_after_every_restart(tmp_path, fees, expected):
    """Estimated funding plus (optionally) unstated fees, written to the real
    store and read back repeatedly the way successive restarts would."""
    import ledger as L
    from test_carry_fee_truth import absent, build, modern, open_the_hedge
    _v, journal, engine = build(absent if fees == "absent" else modern)
    open_the_hedge(engine)
    L.funding(journal, ms=journal.entries[-1].ms + 1, ref="est", usd=3.0)
    db = str(tmp_path / "restart.db")
    store = StateStore(db)
    store.save_ledger(journal.to_rows())
    store.close()
    seen = set()
    for _ in range(3):                                   # three "restarts"
        c = carry_report.build_report(db)["carry"]
        seen.add((c["status"], c["funding_estimated_usd"], c["fees_unknown"]))
    assert len(seen) == 1
    assert next(iter(seen))[0] == expected
