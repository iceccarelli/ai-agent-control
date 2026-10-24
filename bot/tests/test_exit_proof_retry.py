"""An exit booked without its execution ids is proven later, and an order filled
in pieces across cycles is proven piece by piece.

Before: `_exit_execution_ids` ran once, at booking. An unreadable endpoint or a
fill list that had not caught up left the exit unproven FOREVER, and a venue
order that filled across several cycles could never be proven at all, because
each piece saw the other pieces' fills.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from test_exit_execution_ids import (closed, exec_row, meta, trades,  # noqa: E402
                                     venue)

T0 = 1_800_000_000.0


def book_unproven(tmp_path, executions=(), qty=0.01):
    fake, engine, store = venue(tmp_path)
    fake.closed_pnl.append(closed(qty))
    fake.executions += list(executions)
    assert engine.observe_exits()["closed"] == 1
    (t,) = trades(store)
    assert meta(t)["exit_exec_ids"] == []
    return fake, engine, store, t


class TestAMissedProofIsRetried:
    def test_executions_that_appear_later_prove_the_exit(self, tmp_path):
        fake, engine, store, t = book_unproven(tmp_path)
        before = {k: t[k] for k in ("exit_price", "gross_pnl", "exit_fee", "qty")}
        fake.executions += [exec_row(1, 0.004), exec_row(2, 0.006)]
        out = engine.retry_missed_exit_proofs(now=T0)
        assert out["proven"] == 1
        (t2,) = trades(store)
        assert meta(t2)["exit_exec_ids"] == ["EX-1", "EX-2"]
        assert meta(t2)["exit_proof"]["proven_after_booking"] is True
        assert {k: t2[k] for k in before} == before        # accounting untouched

    def test_nothing_is_owed_after_it_is_proven(self, tmp_path):
        fake, engine, store, _ = book_unproven(tmp_path)
        fake.executions += [exec_row(1, 0.01)]
        engine.retry_missed_exit_proofs(now=T0)
        assert engine.retry_missed_exit_proofs(now=T0 + 10_000) == {
            "checked": 0, "proven": 0, "pending": 0, "given_up": 0}

    def test_a_failed_attempt_is_counted_and_backed_off(self, tmp_path):
        fake, engine, store, _ = book_unproven(tmp_path)
        calls = []
        real = fake.request
        fake.request = lambda *a, **k: (calls.append(a), real(*a, **k))[1]
        engine.retry_missed_exit_proofs(now=T0)                  # attempt 1: none yet
        n = len(calls)
        engine.retry_missed_exit_proofs(now=T0 + 10)             # inside the spacing
        assert len(calls) == n
        assert meta(trades(store)[0])["exit_proof"]["attempts"] == 1
        engine.retry_missed_exit_proofs(now=T0 + 10 + 60)        # 60 s * 2**1 not yet
        assert meta(trades(store)[0])["exit_proof"]["attempts"] == 1
        engine.retry_missed_exit_proofs(now=T0 + 121)
        assert meta(trades(store)[0])["exit_proof"]["attempts"] == 2

    def test_it_gives_up_after_the_attempt_budget_and_stops_reading(self, tmp_path):
        fake, engine, store, _ = book_unproven(tmp_path)
        now = T0
        for _ in range(store.EXIT_PROOF_MAX_ATTEMPTS):
            engine.retry_missed_exit_proofs(now=now)
            now += 1e6
        t = trades(store)[0]
        assert meta(t)["exit_proof"]["attempts"] == store.EXIT_PROOF_MAX_ATTEMPTS
        assert meta(t)["exit_exec_ids"] == []                   # still unproven
        assert store.exit_proof_candidates() == []
        fake.executions += [exec_row(1, 0.01)]
        assert engine.retry_missed_exit_proofs(now=now + 1e6)["checked"] == 0
        assert meta(trades(store)[0])["exit_exec_ids"] == []

    def test_an_unreadable_endpoint_is_an_attempt_not_a_crash(self, tmp_path):
        fake, engine, store, _ = book_unproven(tmp_path)
        real = fake.request

        def boom(method, path, *a, **k):
            if "execution" in path:
                raise RuntimeError("down")
            return real(method, path, *a, **k)
        fake.request = boom
        out = engine.retry_missed_exit_proofs(now=T0)
        assert out["proven"] == 0
        assert "unreadable" in meta(trades(store)[0])["exit_proof"]["last_reason"]

    def test_executions_that_contradict_the_booked_price_do_not_prove_it(self, tmp_path):
        fake, engine, store, _ = book_unproven(tmp_path)
        fake.executions += [exec_row(1, 0.01, price="60500")]
        assert engine.retry_missed_exit_proofs(now=T0)["proven"] == 0
        assert meta(trades(store)[0])["exit_exec_ids"] == []
        assert "booked at" in meta(trades(store)[0])["exit_proof"]["last_reason"]

    def test_executions_that_do_not_add_up_do_not_prove_it(self, tmp_path):
        fake, engine, store, _ = book_unproven(tmp_path)
        fake.executions += [exec_row(1, 0.004)]
        assert engine.retry_missed_exit_proofs(now=T0)["proven"] == 0

    def test_an_exit_with_no_order_ids_is_not_a_candidate(self, tmp_path):
        fake, engine, store, t = book_unproven(tmp_path)
        m = meta(t)
        m["exit_order_ids"] = []
        store._conn.execute("UPDATE trades SET meta = ? WHERE id = ?",
                            (json.dumps(m), t["id"]))
        assert store.exit_proof_candidates() == []


class TestAnOrderFilledAcrossCyclesIsProvenPieceByPiece:
    def two_pieces(self, tmp_path, executions):
        fake, engine, store = venue(tmp_path, size=0.004)
        fake.closed_pnl.append(closed(0.006, dt=1000))
        fake.executions += executions
        assert engine.observe_exits()["reduced"] == 1
        # the rest of the same venue order fills a cycle later
        fake.positions.pop("BTCUSDT", None)
        fake.closed_pnl.append(closed(0.004, dt=5000))
        assert engine.observe_exits()["closed"] == 1
        rows = trades(store)
        assert len(rows) == 2 and all(meta(r)["exit_exec_ids"] == [] for r in rows)
        return fake, engine, store

    def test_each_piece_gets_its_own_whole_fills(self, tmp_path):
        fake, engine, store = self.two_pieces(
            tmp_path, [exec_row(1, 0.006), exec_row(2, 0.004)])
        out = engine.retry_missed_exit_proofs(now=T0)
        assert out["proven"] == 2
        a, b = trades(store)
        assert meta(a)["exit_exec_ids"] == ["EX-1"]
        assert meta(b)["exit_exec_ids"] == ["EX-2"]
        # one fill prices one exit row only
        assert store.claimed_exit_exec_ids("BTCUSDT") == {"ex-1", "ex-2"}

    def test_several_fills_per_piece_are_cut_on_whole_executions(self, tmp_path):
        fake, engine, store = self.two_pieces(tmp_path, [
            exec_row(1, 0.002), exec_row(2, 0.004), exec_row(3, 0.003),
            exec_row(4, 0.001)])
        assert engine.retry_missed_exit_proofs(now=T0)["proven"] == 2
        a, b = trades(store)
        assert meta(a)["exit_exec_ids"] == ["EX-1", "EX-2"]
        assert meta(b)["exit_exec_ids"] == ["EX-3", "EX-4"]

    def test_an_execution_that_straddles_the_boundary_is_never_split(self, tmp_path):
        fake, engine, store = self.two_pieces(tmp_path, [exec_row(1, 0.01)])
        out = engine.retry_missed_exit_proofs(now=T0)
        assert out["proven"] == 0
        assert all(meta(r)["exit_exec_ids"] == [] for r in trades(store))
        assert "whole fills" in meta(trades(store)[0])["exit_proof"]["last_reason"]

    def test_a_piece_already_proven_keeps_its_fills_and_leaves_the_rest(self, tmp_path):
        fake, engine, store = self.two_pieces(
            tmp_path, [exec_row(1, 0.006), exec_row(2, 0.004)])
        a, _b = trades(store)
        assert store.record_exit_exec_ids(a["id"], ["EX-1"], T0)
        assert engine.retry_missed_exit_proofs(now=T0)["proven"] == 1
        assert meta(trades(store)[1])["exit_exec_ids"] == ["EX-2"]

    def test_fills_that_belong_to_a_later_unbooked_piece_are_left_alone(self, tmp_path):
        fake, engine, store = venue(tmp_path)
        fake.closed_pnl.append(closed(0.006))
        fake.executions += [exec_row(1, 0.006), exec_row(2, 0.004)]
        # booked as a full exit of 0.01 would not happen; book 0.006 alone
        fake2, engine2, store2 = venue(tmp_path / "b", size=0.004)
        fake2.closed_pnl.append(closed(0.006))
        fake2.executions += [exec_row(1, 0.006), exec_row(2, 0.004)]
        engine2.observe_exits()
        assert engine2.retry_missed_exit_proofs(now=T0)["proven"] == 1
        (t,) = trades(store2)
        assert meta(t)["exit_exec_ids"] == ["EX-1"]


class TestRecordExitExecIds:
    def test_it_refuses_an_exit_that_already_names_executions(self, tmp_path):
        fake, engine, store, t = book_unproven(tmp_path)
        assert store.record_exit_exec_ids(t["id"], ["EX-9"], T0) is True
        assert store.record_exit_exec_ids(t["id"], ["EX-10"], T0) is False
        assert meta(trades(store)[0])["exit_exec_ids"] == ["EX-9"]

    def test_it_refuses_an_execution_another_exit_names(self, tmp_path):
        fake, engine, store, t = book_unproven(tmp_path)
        other = dict(t, id=None)
        cols = [k for k in other if k != "id"]
        store._conn.execute(
            f"INSERT INTO trades ({', '.join(cols)}) VALUES "
            f"({', '.join('?' for _ in cols)})",
            [json.dumps({"exit_exec_ids": ["EX-5"]}) if k == "meta" else
             ("other-exit" if k == "dedupe_key" else other[k]) for k in cols])
        assert store.record_exit_exec_ids(t["id"], ["ex-5"], T0) is False
        assert store.record_exit_exec_ids(t["id"], ["EX-6"], T0) is True

    @pytest.mark.parametrize("ids", [[], [""], ["  "], ["A", "a"]])
    def test_it_refuses_unusable_ids(self, tmp_path, ids):
        fake, engine, store, t = book_unproven(tmp_path)
        assert store.record_exit_exec_ids(t["id"], ids, T0) is False

    def test_a_missing_trade_is_false_not_an_error(self, tmp_path):
        fake, engine, store, t = book_unproven(tmp_path)
        assert store.record_exit_exec_ids(99999, ["EX-1"], T0) is False


class TestWiring:
    def test_the_main_cycle_calls_it(self):
        src = open(os.path.join(os.path.dirname(HERE), "main.py"),
                   encoding="utf-8").read()
        assert "retry_missed_exit_proofs" in src


# ---------------------------------------------------------------------------
# found by independent review (each reproduced before the fix)
# ---------------------------------------------------------------------------

def clone(store, t, n, **over):
    cols = [k for k in t.keys() if k != "id"]
    vals = [f"clone-{n}" if k == "dedupe_key" else over.get(k, t[k]) for k in cols]
    store._conn.execute(
        f"INSERT INTO trades ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        vals)
    store._conn.commit()


def realised(fake, engine, store, t):
    """Mark the booked exit fee as a realised venue fee of a known size."""
    store._conn.execute("UPDATE trades SET exit_fee_realized = 1, exit_fee = 0.61 "
                        "WHERE id = ?", (t["id"],))
    return fake, engine, store, trades(store)[0]


def two_unclosed(tmp_path, executions, size=0.005):
    fake, engine, store = venue(tmp_path, size=size)
    fake.closed_pnl.append(closed(0.005, dt=1000))
    fake.executions += executions
    assert engine.observe_exits()["reduced"] == 1
    fake.positions.pop("BTCUSDT", None)
    fake.closed_pnl.append(closed(0.005, dt=5000))
    assert engine.observe_exits()["closed"] == 1
    return fake, engine, store


class TestReviewFindings:
    def test_an_old_pile_of_proven_trades_does_not_hide_a_newer_unproven_exit(
            self, tmp_path):
        fake, engine, store, t = book_unproven(tmp_path)
        old = meta(t)
        for i in range(200):
            clone(store, t, i, closed_epoch=float(t["closed_epoch"]) - 10_000 + i,
                  meta=json.dumps(dict(old, exit_exec_ids=[f"OLD-{i}"])))
        fake.executions += [exec_row(1, 0.01)]
        assert len(store.exit_proof_candidates()) == 1
        assert engine.retry_missed_exit_proofs(now=T0)["proven"] == 1
        first = store._query("SELECT meta FROM trades ORDER BY id LIMIT 1")[0]
        assert json.loads(first["meta"])["exit_exec_ids"] == ["EX-1"]

    def test_given_up_rows_do_not_occupy_the_candidate_window(self, tmp_path):
        fake, engine, store, t = book_unproven(tmp_path)
        given_up = dict(meta(t), exit_proof={"attempts": store.EXIT_PROOF_MAX_ATTEMPTS})
        for i in range(30):
            clone(store, t, i, closed_epoch=float(t["closed_epoch"]) - 10_000 + i,
                  meta=json.dumps(given_up))
        assert len(store.exit_proof_candidates(limit=5)) == 1

    def test_the_limit_counts_candidates_not_rows(self, tmp_path):
        fake, engine, store, t = book_unproven(tmp_path)
        for i in range(10):
            clone(store, t, i, closed_epoch=float(t["closed_epoch"]) + 1 + i)
        assert len(store.exit_proof_candidates(limit=4)) == 4

    def test_a_given_up_earlier_piece_blocks_the_later_one_from_taking_its_fill(
            self, tmp_path):
        fake, engine, store = two_unclosed(tmp_path, [
            exec_row(1, 0.005, fee="0.0005"), exec_row(2, 0.005, fee="0.5")])
        a, _b = trades(store)
        m = meta(a)
        m["exit_proof"] = {"attempts": store.EXIT_PROOF_MAX_ATTEMPTS}
        store._conn.execute("UPDATE trades SET meta=? WHERE id=?",
                            (json.dumps(m), a["id"]))
        out = engine.retry_missed_exit_proofs(now=T0)
        assert out["proven"] == 0
        assert meta(trades(store)[1])["exit_exec_ids"] == []
        assert "earlier exit of the same order" in \
            meta(trades(store)[1])["exit_proof"]["last_reason"]

    def test_the_executions_fee_must_be_the_fee_the_exit_was_booked_with(
            self, tmp_path):
        fake, engine, store, t = realised(*book_unproven(tmp_path))
        fake.executions += [exec_row(1, 0.01, fee="123.0")]
        assert engine.retry_missed_exit_proofs(now=T0)["proven"] == 0
        assert "charged" in meta(trades(store)[0])["exit_proof"]["last_reason"]

    def test_the_matching_fee_proves_it(self, tmp_path):
        fake, engine, store, t = realised(*book_unproven(tmp_path))
        fake.executions += [exec_row(1, 0.01, fee=repr(float(t["exit_fee"])))]
        assert engine.retry_missed_exit_proofs(now=T0)["proven"] == 1

    def test_an_estimated_booked_fee_is_not_compared(self, tmp_path):
        """A fee that was only an estimate at booking has nothing to compare to
        (the reconciler flags EXIT_FEE_NOT_REALIZED on it separately)."""
        fake, engine, store, t = book_unproven(tmp_path)
        assert not t["exit_fee_realized"]
        fake.executions += [exec_row(1, 0.01, fee="123.0")]
        assert engine.retry_missed_exit_proofs(now=T0)["proven"] == 1

    def test_a_new_sibling_does_not_spend_the_older_pieces_attempts(self, tmp_path):
        fake, engine, store, t = book_unproven(tmp_path)
        engine.retry_missed_exit_proofs(now=T0)                 # attempt 1 for A
        clone(store, t, 99, closed_epoch=float(t["closed_epoch"]) + 5)
        engine.retry_missed_exit_proofs(now=T0 + 1)             # only the new one is due
        a, c = trades(store)[0], trades(store)[1]
        assert meta(a)["exit_proof"]["attempts"] == 1           # not bumped
        assert meta(c)["exit_proof"]["attempts"] == 1

    def test_nothing_is_read_while_everything_is_in_back_off(self, tmp_path):
        fake, engine, store, t = book_unproven(tmp_path)
        engine.retry_missed_exit_proofs(now=T0)
        sqls, real = [], store._query
        store._query = lambda sql, p=(): (sqls.append(sql), real(sql, p))[1]
        out = engine.retry_missed_exit_proofs(now=T0 + 1)
        assert out["pending"] == 1 and out["checked"] == 0
        assert len(sqls) == 1 and "ORDER BY closed_epoch ASC, id ASC" in sqls[0]
        assert "WHERE symbol" not in sqls[0]            # no sibling load

    @pytest.mark.parametrize("junk", ["garbage", 5, [1], None])
    def test_a_corrupt_exit_proof_value_does_not_escape(self, tmp_path, junk):
        fake, engine, store, t = book_unproven(tmp_path)
        m = meta(t)
        m["exit_proof"] = junk
        store._conn.execute("UPDATE trades SET meta=? WHERE id=?",
                            (json.dumps(m), t["id"]))
        fake.executions += [exec_row(1, 0.01, fee=repr(float(t["exit_fee"])))]
        assert engine.retry_missed_exit_proofs(now=T0)["proven"] == 1

    def test_a_proven_sibling_of_another_order_set_still_owns_its_fills(self, tmp_path):
        fake, engine, store, t = book_unproven(tmp_path)
        clone(store, t, 7, meta=json.dumps(dict(
            meta(t), exit_order_ids=["O1", "O2"], exit_exec_ids=["EX-1"])),
            closed_epoch=float(t["closed_epoch"]) - 100)
        fake.executions += [exec_row(1, 0.01, fee=repr(float(t["exit_fee"])))]
        assert engine.retry_missed_exit_proofs(now=T0)["proven"] == 0
