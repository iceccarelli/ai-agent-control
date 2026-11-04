"""Realised cash is not marked-to-market pair economics.

`ledger.revalue_inventory` marks the client's BTC from one spot price to
another. The coin is never sold, so that movement is an unrealised mark, not
cash. The report used to sum every REALISED line, marks included, into a
cycle's net and so into `realized_cash_net_usd`; the evidence adapter then
treated a booked inventory leg as what made an overlay cycle cash evidence.

These tests run the REAL report builder over real journals and feed its REAL
output to the carry evidence adapter. Every figure is a hand-built fixture,
never an observation.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, os.path.join(ROOT, "tools"), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import carry_evidence as cev                                   # noqa: E402
import carry_performance as perf                               # noqa: E402
import ledger as L                                             # noqa: E402
import strategy_evidence as se                                 # noqa: E402
import test_performance_evidence as T                          # noqa: E402
from ledger_fixtures import vraw as _vraw  # noqa: E402

NOW_S = T.NOW / 1000.0
WINDOW = (1.0e9, 1.6e9)
P0 = 100_000.0
# overlay_cycle defaults: funding +300, fees 55 + 55, short 1 BTC from 100k
BASE = 300.0 - 110.0


def mark(j, d1, *, ref="rv", qty=1.0, p1=98_000.0, observed_ms=None,
         valuation="MARK_TO_MARKET", legacy=False, fill_ms=None):
    """The client's BTC marked from the entry spot to the exit spot."""
    fill = T.T0 + d1 * T.DAY if fill_ms is None else fill_ms
    raw = None if legacy else {
        "valuation": valuation, "qty": qty, "entry_spot": P0, "exit_spot": p1,
        "exit_mark_source": "tick_spot_mark",
        "exit_mark_observed_ms": fill if observed_ms is None else observed_ms,
        "fill_ms": fill}
    if legacy:
        return L.revalue_inventory(j, ms=T.T0 + d1 * T.DAY + 1, ref=ref,
                                   qty=qty, from_price=P0, to_price=p1)
    return L.revalue_inventory(j, ms=T.T0 + d1 * T.DAY + 1, ref=ref, qty=qty,
                               from_price=P0, to_price=p1, raw=raw)


def one(tmp_path, *, close=98_000.0, p1=98_000.0, legacy=False, n=1, **kw):
    j = L.Journal()
    for i in range(n):
        T.overlay_cycle(j, 2 * i, 2 * i + 1, tag=f"c{i}", close_price=close,
                        **{k: v for k, v in kw.items()
                           if k in ("fee_status", "close_fee_status",
                                    "reconcile", "funding")})
        mark(j, 2 * i + 1, ref=f"c{i}-rv", p1=p1, legacy=legacy)
    db = T.write_db(tmp_path, j)
    return perf.build_report(db, environment="mainnet", now_ms=T.NOW)


def cash(rep):
    return rep["sections"]["realized_cash"]


def cyc(rep, i=0):
    return rep["sections"]["ledger_detail"]["cycles"][i]


# -- the separation -------------------------------------------------------------

def test_a_profitable_perp_close_with_an_inventory_loss(tmp_path):
    rep = one(tmp_path, close=98_000.0, p1=98_000.0)
    # cash: the short really paid +2000 on the venue; the coin was not sold
    assert cash(rep)["evidence_class"] == "ACTUAL_RECONCILED"
    assert cash(rep)["realized_cash_net_usd"] == pytest.approx(BASE + 2000.0)
    assert cash(rep)["realized_cash_price_leg_usd"] == pytest.approx(2000.0)
    c = cyc(rep)
    assert c["inventory_mark_usd"] == pytest.approx(-2000.0)
    assert c["pair_net_usd"] == pytest.approx(BASE)
    assert c["pair_economics_class"] == perf.MARKED_PAIR_ECONOMICS


def test_a_losing_perp_close_with_an_inventory_gain(tmp_path):
    rep = one(tmp_path, close=102_000.0, p1=102_000.0)
    assert cash(rep)["realized_cash_net_usd"] == pytest.approx(BASE - 2000.0)
    c = cyc(rep)
    assert c["inventory_mark_usd"] == pytest.approx(2000.0)
    # pair economics positive while realised cash is negative
    assert c["pair_net_usd"] == pytest.approx(BASE) and c["pair_net_usd"] > 0
    assert cash(rep)["realized_cash_net_usd"] < 0


def test_positive_realised_cash_with_a_negative_pair(tmp_path):
    rep = one(tmp_path, close=98_000.0, p1=97_700.0)         # coin -2300
    assert cash(rep)["realized_cash_net_usd"] == pytest.approx(BASE + 2000.0)
    c = cyc(rep)
    assert c["pair_net_usd"] == pytest.approx(BASE + 2000.0 - 2300.0)
    assert c["pair_net_usd"] < 0 < cash(rep)["realized_cash_net_usd"]


def test_cash_plus_the_mark_is_the_pair_and_nothing_else_moves(tmp_path):
    rep = one(tmp_path, n=3)
    total_cash = cash(rep)["realized_cash_net_usd"]
    marks = sum(cyc(rep, i)["inventory_mark_usd"] for i in range(3))
    pair = sum(cyc(rep, i)["pair_net_usd"] for i in range(3))
    assert total_cash + marks == pytest.approx(pair)
    assert rep["invariants_hold"] is True


def test_the_cash_section_names_no_mark_and_the_pair_has_its_own_section(tmp_path):
    rep = one(tmp_path)
    assert not [k for k in cash(rep) if "mark" in k or "pair" in k]
    pair = rep["sections"]["marked_pair_economics"]
    assert pair["evidence_class"] == perf.MARKED_PAIR_ECONOMICS
    assert pair["settled_on_venue"] is False
    assert pair["marked_pair_net_usd"] == pytest.approx(BASE)
    assert "valuation" in pair and "mark" in pair["valuation"].lower()


# -- fail closed ----------------------------------------------------------------

def test_confirmed_funding_but_unconfirmed_fees(tmp_path):
    rep = one(tmp_path, fee_status="unknown")
    assert cash(rep)["evidence_class"] == "UNAVAILABLE"
    assert "realized_cash_net_usd" not in cash(rep)
    c = cyc(rep)
    assert c["pair_net_usd"] is None


def test_confirmed_fees_but_unreconciled_funding(tmp_path):
    rep = one(tmp_path, reconcile=False)
    assert cash(rep)["evidence_class"] == "UNAVAILABLE"
    c = cyc(rep)
    assert c["pair_net_usd"] is None


def test_a_mark_with_no_provenance_gives_no_pair_economics(tmp_path):
    rep = one(tmp_path, legacy=True)                 # an entry with no source
    c = cyc(rep)
    assert c["pair_net_usd"] is None
    assert any("UNVERIFIED_MARK" in r for r in c["pair_unavailable_reasons"])
    # the cash figure is unaffected by an unusable mark
    assert cash(rep)["realized_cash_net_usd"] == pytest.approx(BASE + 2000.0)


def test_a_stale_mark_gives_no_pair_economics(tmp_path):
    j = L.Journal()
    T.overlay_cycle(j, 0, 1, tag="c0")
    mark(j, 1, ref="c0-rv", observed_ms=T.T0 + T.DAY - 10 * 60_000)  # 10 min old
    rep = perf.build_report(T.write_db(tmp_path, j), environment="mainnet",
                            now_ms=T.NOW)
    c = cyc(rep)
    assert c["pair_net_usd"] is None
    assert any("STALE_MARK" in r for r in c["pair_unavailable_reasons"])


def test_a_mark_dated_after_its_fill_is_not_a_mark(tmp_path):
    j = L.Journal()
    T.overlay_cycle(j, 0, 1, tag="c0")
    mark(j, 1, ref="c0-rv", observed_ms=T.T0 + T.DAY + 60_000)       # future
    rep = perf.build_report(T.write_db(tmp_path, j), environment="mainnet",
                            now_ms=T.NOW)
    assert cyc(rep)["pair_net_usd"] is None


def test_a_replayed_mark_is_not_counted_twice(tmp_path):
    j = L.Journal()
    T.overlay_cycle(j, 0, 1, tag="c0")
    mark(j, 1, ref="c0-rv")
    mark(j, 1, ref="c0-rv")                           # the same entry replayed
    rep = perf.build_report(T.write_db(tmp_path, j), environment="mainnet",
                            now_ms=T.NOW)
    c = cyc(rep)
    assert c["pair_net_usd"] is None
    assert any("DUPLICATE_MARK" in r for r in c["pair_unavailable_reasons"])


def test_conflicting_marks_for_one_close_are_not_averaged(tmp_path):
    j = L.Journal()
    T.overlay_cycle(j, 0, 1, tag="c0")
    mark(j, 1, ref="c0-rv", p1=98_000.0)
    mark(j, 1, ref="c0-rv", p1=99_000.0)              # same ref, other price
    rep = perf.build_report(T.write_db(tmp_path, j), environment="mainnet",
                            now_ms=T.NOW)
    c = cyc(rep)
    assert c["pair_net_usd"] is None
    assert any("CONFLICTING_MARK" in r for r in c["pair_unavailable_reasons"])


def test_marks_that_do_not_cover_the_closed_quantity(tmp_path):
    j = L.Journal()
    T.overlay_cycle(j, 0, 1, tag="c0")
    mark(j, 1, ref="c0-rv", qty=0.4)                  # 1.0 BTC was closed
    rep = perf.build_report(T.write_db(tmp_path, j), environment="mainnet",
                            now_ms=T.NOW)
    c = cyc(rep)
    assert c["pair_net_usd"] is None
    assert any("MARK_QTY_MISMATCH" in r for r in c["pair_unavailable_reasons"])


def test_a_mark_that_is_not_labelled_mark_to_market_is_refused(tmp_path):
    j = L.Journal()
    T.overlay_cycle(j, 0, 1, tag="c0")
    mark(j, 1, ref="c0-rv", valuation="REALISED")
    rep = perf.build_report(T.write_db(tmp_path, j), environment="mainnet",
                            now_ms=T.NOW)
    assert cyc(rep)["pair_net_usd"] is None


# -- the evidence and allocation decision ---------------------------------------

def evidence(rep):
    return cev.evidence_from_report(
        rep, strategy_id="btc_funding_carry", observed_at_s=NOW_S,
        discovery_window_s=WINDOW, drawdown_limit_usd=50.0)


def test_a_marked_inventory_gain_cannot_satisfy_the_cash_only_allocation(tmp_path):
    # 25 cycles whose pair economics are strongly positive on marks alone.
    rep = one(tmp_path, n=25, close=102_000.0, p1=102_000.0, funding=900.0)
    assert all(cyc(rep, i)["pair_net_usd"] > 0 for i in range(25))
    ev = evidence(rep)
    assert ev.trades == () and ev.unqualified_results == 25
    v = se.evaluate_strategy_evidence(ev, now_s=NOW_S)
    assert v.verdict == se.INSUFFICIENT


def test_a_booked_mark_does_not_turn_an_overlay_into_cash_evidence(tmp_path):
    rep = one(tmp_path, n=25)
    assert evidence(rep).trades == ()
    assert se.evaluate_strategy_evidence(
        evidence(rep), now_s=NOW_S).verdict == se.INSUFFICIENT


def acquire(j, d0, d1, *, tag, close=P0):
    L.spot_buy(j, ms=T.T0 + d0 * T.DAY, ref=f"{tag}-sb", qty=1.0, price=P0,
               fee_btc=0.0, fee_usdt=55.0, fee_status="confirmed")
    L.perp_open(j, ms=T.T0 + d0 * T.DAY + 1, ref=f"{tag}-o", qty=1.0,
                price=P0, fee_usd=55.0, fee_status="confirmed")
    L.funding(j, ms=T.T0 + (d0 + 1) * T.DAY, ref=f"{tag}-f", usd=300.0,
              basis="venue", raw=_vraw(f"{tag}-f", T.T0 + (d0 + 1) * T.DAY, 300.0))
    L.perp_close(j, ms=T.T0 + d1 * T.DAY, ref=f"{tag}-c", qty=1.0, price=close,
                 entry_price=P0, fee_usd=55.0, fee_status="confirmed")
    L.spot_sell(j, ms=T.T0 + d1 * T.DAY + 1, ref=f"{tag}-ss", qty=1.0,
                price=close, cost_basis=P0, fee_usd=55.0,
                fee_status="confirmed")
    T.reconcile_last(j, ms=T.T0 + d1 * T.DAY + 2)


def test_an_acquire_cycle_is_cash_because_both_legs_were_traded(tmp_path):
    j = L.Journal()
    for i in range(25):
        acquire(j, 3 * i, 3 * i + 2, tag=f"a{i}", close=98_000.0)
    rep = perf.build_report(T.write_db(tmp_path, j), environment="mainnet",
                            now_ms=T.NOW)
    assert cash(rep)["evidence_class"] == "ACTUAL_RECONCILED"
    # perp +2000, spot -2000 (really sold), funding 300, fees 4 x 55
    assert cash(rep)["realized_cash_net_usd"] == pytest.approx(25 * (300 - 220))
    ev = evidence(rep)
    assert len(ev.trades) == 25 and ev.unqualified_results == 0


# -- the ledger statement and the other readers ----------------------------------

def test_the_ledger_statement_keeps_the_mark_out_of_realised_and_net():
    j = L.Journal()
    T.overlay_cycle(j, 0, 1, tag="c0")
    mark(j, 1, ref="c0-rv")
    st = j.statement(now_ms=T.NOW)
    assert st["realised_usd"] == pytest.approx(2000.0)           # the perp, cash
    assert st["net_usd"] == pytest.approx(BASE + 2000.0)
    assert st["inventory_mark_usd"] == pytest.approx(-2000.0)    # the coin, mark
    assert st["pair_net_usd"] == pytest.approx(BASE)


def test_a_mark_written_by_the_broker_is_verified_by_the_report():
    from test_overlay_inventory_leg import PricedVenue, broker, buy, sell
    v, b = broker()
    sell(b, 0.001, 100_000.0, 100_000.0, v)
    buy(b, 0.001, 90_000.0, 90_000.0, v)
    (c,) = [c for c in perf.cycles_from_journal(b.journal) if c["closed"]]
    assert c["mark_status"] == "VERIFIED", c["mark_reasons"]
    (m,) = [e for e in b.journal.entries if e.kind == "revalue"]
    assert m.fee_raw["valuation"] == L.MARK_VALUATION
    assert m.fee_raw["exit_mark_source"] in ("tick_spot_mark",
                                             "public_spot_read")


def test_carry_report_does_not_present_the_mark_as_cash(tmp_path):
    import carry_report as creport
    j = L.Journal()
    T.overlay_cycle(j, 0, 1, tag="c0")
    mark(j, 1, ref="c0-rv")
    out = creport.build_report(T.write_db(tmp_path, j), environment="mainnet")
    c = out["carry"]
    assert c["net_usd"] == pytest.approx(BASE + 2000.0)
    assert c["inventory_mark_usd"] == pytest.approx(-2000.0)
    assert c["pair_net_usd"] == pytest.approx(BASE)
    assert c["pair_net_class"] == perf.MARKED_PAIR_ECONOMICS
