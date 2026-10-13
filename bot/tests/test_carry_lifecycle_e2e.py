"""One carry lifecycle through the SHIPPED modules, then a restart.

CarryEngine -> LedgerBroker -> CarryBroker -> (scripted venue transport) ->
Journal -> StateStore -> restart -> carry_report. Nothing here is a model of
the production code: the engine, both brokers, the double-entry journal, the
funding reconciler, the store and the report CLI are the real ones. Only the
HTTP transport is scripted (offline; no credential, no network). Every figure
is a FIXTURE value, not an observation.
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import ledger as L  # noqa: E402
import funding_reconcile as R  # noqa: E402
from carry_broker import FundingHistory, SettlementEvidence  # noqa: E402
from market_snapshot import MarketSnapshot  # noqa: E402
from persistence import StateStore  # noqa: E402
from test_carry_fee_truth import (  # noqa: E402
    H8, MARK, build, kinds, modern, open_the_hedge)
from test_carry_funding_truth import pay, reconcile  # noqa: E402
from tools import carry_report  # noqa: E402


def _held_prints_then_invert(engine, journal):
    """Two held positive prints, then an inverted streak that unwinds."""
    decisions = []
    stamp = 4 * H8
    for bps in (3.0, 3.0, -4.0, -4.0, -4.0, -4.0, -4.0, -4.0):
        decisions.append(engine.on_candle(
            mark=MARK, funding_bps=bps, spot=MARK, funding_print_ms=stamp))
        stamp += H8
        if decisions[-1].action in ("closed", "unwound", "unwind"):
            break
    return decisions


def _reconcile(journal):
    now = journal.entries[-1].ms + 1_000
    lo = journal.entries[0].ms - 120_000
    R.reconcile_funding(
        journal, history=FundingHistory(start_ms=lo, end_ms=now),
        evidence=SettlementEvidence(start_ms=lo, end_ms=now, windows=1),
        now_ms=now)


class TestWholeLifecycle:
    def test_open_repeat_hold_close_reconcile_persist_restart(
            self, tmp_path, monkeypatch):
        # A controllable wall clock: the journal stamps real epoch time, and the
        # ledger refuses to attribute a settlement unless the position was
        # unchanged for a minute either side of it, so the hold must be real.
        skew = [0.0]
        real_time = time.time
        monkeypatch.setattr(time, "time", lambda: real_time() + skew[0])
        venue, journal, engine = build(modern)

        # 1-4. strategy intent -> sizing/risk -> order intents -> venue fills
        assert open_the_hedge(engine).action == "opened"
        assert kinds(journal) == ["spot_buy", "perp_open"]
        created_after_open = len(venue.created)
        links = [b["orderLinkId"] for b in venue.created]
        assert len(set(links)) == len(links) == 2      # unique client ids

        # repeated invocation with the SAME settled print must not add a pair
        again = engine.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                                 funding_print_ms=3 * H8)
        assert again.action != "opened"
        assert len(venue.created) == created_after_open
        assert kinds(journal) == ["spot_buy", "perp_open"]

        # held through, then the funding inverts and the pair is unwound
        skew[0] = 600.0        # ten minutes pass while the pair is held
        engine.snapshot = MarketSnapshot(
            perp_mark=MARK, spot_mark=MARK, funding_bps=3.0,
            margin_multiple=5.0, observed_at_s=time.time())
        decisions = _held_prints_then_invert(engine, journal)
        assert engine.position is None, [d.action for d in decisions]
        assert "perp_close" in kinds(journal) and kinds(journal).count("spot_sell")

        # the position can no longer exist without both legs having closed
        sides = [(b["category"], b["side"]) for b in venue.created]
        assert sides.count(("spot", "Buy")) == sides.count(("spot", "Sell")) == 1

        # 5-7. the venue pays one settlement while the pair was held; it is
        # booked from the venue's own row (never the engine estimate), booking
        # the same row again is a no-op, and only then is the net CONFIRMED
        opened = next(e for e in journal.entries if e.kind == "perp_open")
        closed = next(e for e in journal.entries if e.kind == "perp_close")
        perp_qty = abs(float(next(b for b in venue.created
                                  if b["category"] == "linear")["qty"]))
        t_pay = opened.ms + (closed.ms - opened.ms) // 2
        payment = pay(1, 4.25, ms=t_pay, size=-perp_qty)
        first = L.book_funding_payments(journal, [payment], ms=closed.ms + 1)
        second = L.book_funding_payments(journal, [payment], ms=closed.ms + 2)
        assert (first["booked"], second["booked"], second["duplicate"]) == (1, 0, 1), (first, second, t_pay, opened.ms, closed.ms, perp_qty)
        now = closed.ms + 30 * H8
        rec = reconcile(journal, [t_pay], now_ms=now, rows=[payment],
                        start=opened.ms - 60_000)
        assert rec is not None
        st = journal.statement()
        assert st["funding_usd"] == pytest.approx(4.25)
        assert st["funding_venue_usd"] == pytest.approx(4.25)
        assert st["funding_estimated_usd"] == 0.0
        assert st["net_basis"] == "confirmed", st["funding_lifecycles"]
        assert st["net_usd"] == pytest.approx(4.25 - st["fees_usd"])
        assert st["fees_confirmed"] is True
        assert st["fees_usd"] > 0
        assert st["net_usd"] == pytest.approx(
            st["funding_usd"] - st["fees_usd"] - st["borrow_usd"]
            + st["realised_usd"])
        assert st["open_perp_qty"] == pytest.approx(0.0, abs=1e-9)
        wallet = L.reconcile(journal, venue_btc=st["btc_qty"],
                             venue_usdt=st["usdt_qty"],
                             venue_perp_qty=st["open_perp_qty"])
        assert wallet["verdict"] == "RECONCILED"

        db = str(tmp_path / "carry.db")
        store = StateStore(db)
        store.save_ledger(journal.to_rows())
        store.close()

        # 8. a restart rebuilds the same economics from the durable rows only
        store = StateStore(db)
        reborn = L.Journal.from_rows(store.load_ledger())
        store.close()
        st2 = reborn.statement()
        for key in ("net_usd", "fees_usd", "funding_usd", "realised_usd",
                    "fees_confirmed", "net_basis"):
            assert st2[key] == st[key] or st2[key] == pytest.approx(st[key])
        # persisting twice writes the same book (no duplicated entries)
        store = StateStore(db)
        store.save_ledger(reborn.to_rows())
        assert len(store.load_ledger()) == len(journal.to_rows())
        store.close()

        # the operator surface reads the same truth, read-only
        report = carry_report.build_report(db)
        assert report["carry"]["status"] == "REALISED"
        assert report["carry"]["net_pnl_provenance"] is not None
        json.dumps(report, default=str)                 # serialisable

    def test_a_restart_in_the_middle_of_a_hold_does_not_reopen(self, tmp_path):
        venue, journal, engine = build(modern)
        assert open_the_hedge(engine).action == "opened"
        state = engine.to_state()
        _v2, _j2, engine2 = build(modern)
        engine2.restore(state)
        assert engine2.position is not None
        before = len(_v2.created)
        out = engine2.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                                funding_print_ms=3 * H8)
        assert out.action != "opened" and len(_v2.created) == before


class TestFailsSafelyWithANonzeroExit:
    def test_a_missing_database_is_exit_2(self, tmp_path):
        assert carry_report.main([str(tmp_path / "absent.db")]) == 2

    def test_a_rejected_leg_does_not_leave_an_unpaired_book(self):
        venue, journal, engine = build(modern)
        venue_post = venue._request

        def refuse_perp(method, endpoint, **kw):
            body = kw.get("body") or {}
            if endpoint == "/v5/order/create" and body.get("category") == "linear":
                raise RuntimeError("venue rejected the perp leg")
            return venue_post(method, endpoint, **kw)

        venue._request = refuse_perp
        out = open_the_hedge(engine)
        assert out.action != "opened"
        # whatever spot landed must have been flattened or flagged; never a
        # silently open one-legged book recorded as a hedge
        st = journal.statement()
        assert engine.position is None or not engine.position.is_paired
        assert abs(st["open_perp_qty"]) < 1e-9
