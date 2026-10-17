"""The report calls a net REALISED only when every fee is confirmed."""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from persistence import StateStore  # noqa: E402
from test_carry_fee_truth import (  # noqa: E402
    build, modern, open_the_hedge, spot_known_perp_unknown)
from tools import carry_report  # noqa: E402


def _reconciled(journal):
    """Complete synthetic reads (test data) so only the FEES decide the status."""
    from carry_broker import FundingHistory, SettlementEvidence
    import funding_reconcile as R
    now = journal.entries[-1].ms + 1_000
    lo = journal.entries[0].ms - 120_000
    R.reconcile_funding(
        journal, history=FundingHistory(start_ms=lo, end_ms=now),
        evidence=SettlementEvidence(start_ms=lo, end_ms=now, windows=1),
        now_ms=now)


@pytest.mark.parametrize("fees,status", [
    (modern, "REALISED"),
    (spot_known_perp_unknown, "PROVISIONAL_FEES_INCOMPLETE")])
def test_status_follows_fee_confirmation_across_a_real_database(
        tmp_path, fees, status):
    _venue, journal, engine = build(fees)
    open_the_hedge(engine)
    _reconciled(journal)
    db = str(tmp_path / "run.db")
    store = StateStore(db)
    store.save_ledger(journal.to_rows())
    store.close()
    report = carry_report.build_report(db)
    assert report["carry"]["status"] == status
    assert [f["kind"] for f in report["carry"]["fills"]] == [
        "spot_buy", "perp_open"]


def test_fees_confirmed_but_funding_never_reconciled_is_not_REALISED(tmp_path):
    """The gap this closes: nothing had flagged the funding, so nothing said it
    was unproven. A lifecycle with no reconciliation record is PROVISIONAL."""
    _venue, journal, engine = build(modern)
    open_the_hedge(engine)
    db = str(tmp_path / "run.db")
    store = StateStore(db)
    store.save_ledger(journal.to_rows())
    store.close()
    carry = carry_report.build_report(db)["carry"]
    assert carry["status"] == "PROVISIONAL_FUNDING_UNVERIFIED"


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
    from test_carry_fee_truth import (
        build, modern, open_the_hedge, spot_known_perp_unknown)
    _v, journal, engine = build(
        spot_known_perp_unknown if fees == "absent" else modern)
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


# -- the recorded liquidation evidence, as the operator sees it -------------------

H8 = 8 * 3600 * 1000


def _obs(ms, **kw):
    base = dict(ms=ms, symbol="BTCUSDT", side="Sell", size=0.001, mark=100_000.0,
                liq_price=130_000.0, distance_pct=30.0, account_mm_rate=0.05,
                margin_multiple=6.0, reason="")
    base.update(kw)
    return base


def test_a_book_that_recorded_nothing_says_so(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    store.close()
    ev = carry_report.build_report(str(tmp_path / "s.db"))["liquidation_evidence"]
    assert ev == {"status": "NONE", "observations": 0}


def test_recorded_evidence_is_summarised_with_its_gaps(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    t0 = 1_700_000_000_000
    store.record_risk_observation(_obs(t0, distance_pct=40.0))
    store.record_risk_observation(_obs(t0 + 1_000, distance_pct=22.5,
                                       account_mm_rate=0.31))
    store.record_risk_observation(_obs(t0 + 1_000 + 3 * H8, liq_price=None,
                                       distance_pct=None,
                                       reason="BEYOND_VENUE_PRICE_BOUNDS"))
    store.close()
    ev = carry_report.build_report(str(tmp_path / "s.db"))["liquidation_evidence"]
    assert ev["status"] == "RECORDING"
    assert ev["observations"] == 3
    assert ev["first_ms"] == t0 and ev["last_ms"] == t0 + 1_000 + 3 * H8
    assert ev["min_distance_pct"] == pytest.approx(22.5)
    assert ev["max_account_mm_rate"] == pytest.approx(0.31)
    assert ev["unreachable_observations"] == 1
    assert ev["largest_gap_ms"] == 3 * H8
    assert ev["gaps_over_one_funding_interval"] == 1       # a replay refuses it


def test_the_evidence_summary_leaves_the_database_untouched(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    store.record_risk_observation(_obs(1_700_000_000_000))
    store.close()
    path = str(tmp_path / "s.db")
    before = os.path.getmtime(path)
    carry_report.build_report(path)
    assert os.path.getmtime(path) == before
