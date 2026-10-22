"""The direction of funding money is never assumed: a settlement is booked only
when its `funding` is reproduced by its own wallet `change`."""
from __future__ import annotations

import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import directional_funding as df  # noqa: E402
import ledger as L  # noqa: E402
from carry_broker import CarryBroker, funding_sign_check  # noqa: E402
from test_directional_funding import (  # noqa: E402
    H, NOW, _meta, _net, _row, _run, _setup, _trade)
from test_carry_funding_truth import hedged_journal, pay, T0  # noqa: E402


class TestTheCheckItself:
    @pytest.mark.parametrize("funding,change,expect", [
        (0.30, "0.30", "confirmed"),
        (-0.05, "-0.05", "confirmed"),
        (0.0, "0", "confirmed"),
        (0.30, "-0.30", "mismatch"),
        (-0.05, "0.05", "mismatch"),
        (0.30, None, "unverified"),
        (0.30, "", "unverified"),
        (0.30, "abc", "unverified"),
        (0.30, "nan", "unverified"),
        (0.30, "7.77", "unverified"),       # agrees with neither
    ])
    def test_verdicts(self, funding, change, expect):
        assert funding_sign_check(funding, change)[0] == expect

    def test_cash_flow_and_fee_are_part_of_the_mapping(self):
        # change = cashFlow + funding - fee
        assert funding_sign_check(0.30, "0.29", cash_flow="0", fee="0.01")[0] == "confirmed"
        assert funding_sign_check(0.30, "-0.31", cash_flow="0", fee="0.01")[0] == "mismatch"


class TestTheEvidenceCarriesTheVerdict:
    def test_each_payment_says_whether_its_sign_is_confirmed(self, tmp_path):
        fake, store, broker = _setup(tmp_path)
        good, bad, none = _row(1, 0.3, 12), _row(2, 0.3, 10), _row(3, 0.3, 8)
        bad["change"] = "-0.3"
        del none["change"]
        fake.settlements += [good, bad, none]
        ev = broker.get_funding_evidence("BTCUSDT", int(NOW - 40 * H), NOW)
        assert {p["id"]: p["sign"] for p in ev.payments} == {
            "S1": "confirmed", "S2": "mismatch", "S3": "unverified"}


class TestDirectionalBookingRefusesAnUnconfirmedSign:
    def test_a_mismatched_row_leaves_the_trade_unbooked_with_the_status(self, tmp_path):
        fake, store, broker = _setup(tmp_path)
        tid = _trade(store)
        base = _net(store, tid)
        row = _row(1, 0.3, 12)
        row["change"] = "-0.3"
        fake.settlements.append(row)
        r = _run(store, broker)
        assert r["booked"] == 0 and r["statuses"] == {"FUNDING_SIGN_MISMATCH": 1}
        assert _meta(store, tid)["funding_booked"] is False
        assert _net(store, tid) == pytest.approx(base)

    def test_a_row_without_a_change_field_is_not_booked(self, tmp_path):
        fake, store, broker = _setup(tmp_path)
        tid = _trade(store)
        row = _row(1, 0.3, 12)
        del row["change"]
        fake.settlements.append(row)
        r = _run(store, broker)
        assert r["statuses"] == {"FUNDING_SIGN_UNVERIFIED": 1}
        assert _meta(store, tid)["funding_status"] == "FUNDING_SIGN_UNVERIFIED"

    def test_one_bad_row_among_good_ones_blocks_the_whole_trade(self, tmp_path):
        fake, store, broker = _setup(tmp_path)
        tid = _trade(store)
        bad = _row(2, 0.1, 6)
        bad["change"] = "-0.1"
        fake.settlements += [_row(1, 0.3, 12), bad]
        _run(store, broker)
        assert _meta(store, tid)["funding_booked"] is False

    def test_a_matching_row_is_booked_and_says_so(self, tmp_path):
        fake, store, broker = _setup(tmp_path)
        tid = _trade(store)
        fake.settlements.append(_row(1, 0.3, 12))
        assert _run(store, broker)["booked"] == 1
        assert "confirmed against the wallet change" in _meta(store, tid)[
            "funding_evidence"]["sign"]


class TestCarryLedgerRefusesToAssumeTheSign:
    @pytest.mark.parametrize("verdict", ["mismatch", "unverified", None])
    def test_an_unconfirmed_sign_is_flagged_and_not_income(self, verdict):
        j = hedged_journal()
        p = pay(1, 4.25)
        if verdict is None:
            del p["sign"]
        else:
            p["sign"] = verdict
        out = L.book_funding_payments(j, [p], ms=T0 + 10)
        st = j.statement()
        assert out["booked"] == 0 and out["unattributed"] == 1
        assert st["funding_venue_usd"] == 0.0 and st["funding_confirmed"] is False

    def test_a_confirmed_sign_is_booked(self):
        j = hedged_journal()
        assert L.book_funding_payments(j, [pay(1, 4.25)], ms=T0 + 10)["booked"] == 1
