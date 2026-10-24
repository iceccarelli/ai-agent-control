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
