"""Execution evidence round trip: the drill WRITES it, the samples report READS it.

    CarryBroker -> LedgerBroker -> Journal --drill.append_execution_records--> JSONL
        --tools/execution_samples_report--> execution_cost_estimate

for BOTH sides of the round trip (linear Sell = the entry, linear Buy = the
exit), a partial fill and an unfilled order. Orders that did not fill are kept
as execution-OUTCOME lines (kind ``execution_outcome``) and are never a
filled-order cost sample. Fill dedup and environment labelling stay strict.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
sys.path.insert(0, HERE)

import drill                                                 # noqa: E402
import execution_cost_estimate as est                        # noqa: E402
import execution_record as xr                                # noqa: E402
import execution_samples_report as esr                       # noqa: E402
import ledger as L                                           # noqa: E402
from carry_broker import CarryBroker                         # noqa: E402
from execution_fixtures import valid_record                  # noqa: E402
from test_execution_e2e import T0, Venue, env                # noqa: E402

QTY = 0.01
NOTIONAL = 1000.0          # QTY * ~100k


class DepthVenue(Venue):
    """The e2e venue plus a public order book."""

    def _request(self, method, endpoint, *, params=None, body=None,
                 signed=False, retries=3, timeout_s=None):
        if endpoint == "/v5/market/orderbook":
            assert signed is False
            return env({"s": params["symbol"],
                        "b": [[str(self.bid), "5"], [str(self.bid - 10), "5"]],
                        "a": [[str(self.ask), "5"], [str(self.ask + 10), "5"]],
                        "ts": self.clock[0], "u": 1})
        return super()._request(method, endpoint, params=params, body=body,
                                signed=signed, retries=retries)


def rig(venue, clock):
    seq = iter(range(1, 10_000))
    cb = CarryBroker(client=venue, sequence_source=lambda *a: next(seq),
                     order_gate=lambda: (True, "TEST"),
                     clock_ms=lambda: clock[0])
    j = L.Journal()
    return L.LedgerBroker(cb, j, ms=lambda: clock[0]), j


def trade(lb, clock, side, *, qty=QTY, product="linear", tolerate=False):
    lb.begin_decision(symbol="BTCUSDT", product=product, side=side, qty=qty,
                      mark=100_000.0, book_timeout_s=1.0,
                      # as the engine declares it
                      route=("normal_entry" if (product, side) in
                             (("linear", "Sell"), ("spot", "Buy"))
                             else "normal_exit"))
    clock[0] += 40
    try:
        res = lb.place_market(symbol="BTCUSDT", side=side, qty=qty,
                              product=product)
    except Exception:       # noqa: BLE001
        if not tolerate:
            raise
        res = None
    clock[0] += 1_000
    return res


def run_round_trips(n, *, fill_frac=1.0, base=None):
    clock = [base or T0]
    v = DepthVenue(clock, bid=99_995.0, ask=100_005.0, fill_px=100_000.0,
                   fill_frac=fill_frac)
    lb, j = rig(v, clock)
    for _ in range(n):
        trade(lb, clock, "Sell")             # the entry (perp open)
        trade(lb, clock, "Buy")              # the exit (perp close)
    return lb, j, clock


def report(path, *, environment="testnet", now_ms, mode="overlay"):
    return esr.build("", mode=mode, notional_usd=NOTIONAL, perp="BTCUSDT",
                     spot="BTCUSDT", now_ms=now_ms, records=[str(path)],
                     environment=environment)


def leg(rep, name):
    return next(l for l in rep["legs"] if l["leg"] == name)


def lines(path):
    return [json.loads(x) for x in open(path, encoding="utf-8") if x.strip()]


# -- both sides --------------------------------------------------------------------

def test_both_sides_round_trip_and_are_counted(tmp_path):
    lb, j, clock = run_round_trips(21)
    path = tmp_path / "records.jsonl"
    n = drill.append_execution_records(str(path), j, "testnet", set(),
                                       outcomes=lb.executions)
    assert n == 42                                   # 21 entries + 21 exits
    ls = lines(path)
    assert {l["kind"] for l in ls} == {"perp_open", "perp_close"}
    rep = report(path, now_ms=clock[0])
    sell, buy = leg(rep, "linear/Sell/market"), leg(rep, "linear/Buy/market")
    # the exit is sampled exactly like the entry
    assert sell["n_in_size_band"] == buy["n_in_size_band"] == 21
    assert sell["rejected"] == {} and buy["rejected"] == {}
    assert rep["status"] == est.MEASURED, rep["reasons"]
    assert rep["outcomes"]["total"] == 0
    # every record is book-measured, with depth stated and the environment kept
    for l in ls:
        r = l["fee_raw"]["execution"]
        assert r["status"] == xr.MEASURED and r["reference_basis"] == "executable_side"
        assert r["book_source"] == "orderbook"
        assert r["depth"]["status"] == "MEASURED" and r["depth"]["levels_used"] == 1
        assert r["mid_price"] == 100_000.0 and r["spread_bps"] == pytest.approx(1.0)
        assert r["environment"] == ""                # the e2e client states none
        assert r["book_age_ms"] == 0 and r["order_link_id"] == l["ref"]
        assert r["notional_usd"] == pytest.approx(QTY * 100_000.0)


def test_the_two_sides_have_the_sides_the_engine_assigned():
    """Attribution: a perp Sell is the OPEN (the entry), a perp Buy the CLOSE."""
    lb, j, _clock = run_round_trips(2)
    by_kind = {}
    for e in j.entries:
        by_kind.setdefault(e.kind, set()).add(e.fee_raw["execution"]["side"])
    assert by_kind == {"perp_open": {"Sell"}, "perp_close": {"Buy"}}


def test_re_writing_the_same_journal_appends_nothing(tmp_path):
    lb, j, _c = run_round_trips(3)
    path, written = str(tmp_path / "r.jsonl"), set()
    assert drill.append_execution_records(path, j, "testnet", written,
                                          outcomes=lb.executions) == 6
    assert drill.append_execution_records(path, j, "testnet", written,
                                          outcomes=lb.executions) == 0


# -- strict reader -----------------------------------------------------------------

def test_the_environment_label_is_strict(tmp_path):
    lb, j, clock = run_round_trips(21)
    path = tmp_path / "r.jsonl"
    drill.append_execution_records(str(path), j, "testnet", set(),
                                   outcomes=lb.executions)
    rep = report(path, environment="mainnet", now_ms=clock[0])
    assert rep["status"] == est.UNAVAILABLE
    assert all(l["n"] == 0 for l in rep["legs"])
    assert rep["records_skipped"] == 42
    assert leg(rep, "linear/Buy/market")["n_in_size_band"] == 0


def test_a_record_that_contradicts_its_lines_label_is_not_trusted(tmp_path):
    lb, j, clock = run_round_trips(21)
    path = tmp_path / "r.jsonl"
    drill.append_execution_records(str(path), j, "testnet", set(),
                                   outcomes=lb.executions)
    rows = lines(path)
    for r in rows:                                     # records say mainnet,
        r["fee_raw"]["execution"]["environment"] = "mainnet"   # lines say testnet
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    rep = report(path, environment="testnet", now_ms=clock[0])
    assert all(l["n"] == 0 for l in rep["legs"])
    assert rep["records_skipped"] == 42


def test_the_same_fill_twice_in_a_file_counts_once(tmp_path):
    lb, j, clock = run_round_trips(21)
    path = tmp_path / "r.jsonl"
    drill.append_execution_records(str(path), j, "testnet", set(),
                                   outcomes=lb.executions)
    text = path.read_text()
    path.write_text(text + text)                      # a file concatenated twice
    rep = report(path, now_ms=clock[0])
    for name in ("linear/Sell/market", "linear/Buy/market"):
        assert leg(rep, name)["n"] == 21              # counted once
    # identical lines are dropped by the reader and REPORTED (42 = the copies)
    assert rep["records_duplicate_lines"] == 42


def test_a_duplicate_that_is_not_byte_identical_is_still_caught_by_the_estimator(
        tmp_path):
    """Defence in depth: the reader drops identical lines; a copy re-serialised
    differently (other key order / spacing) is not identical text, and the
    estimator's own duplicate key must still count the fill once."""
    lb, j, clock = run_round_trips(21)
    path = tmp_path / "r.jsonl"
    drill.append_execution_records(str(path), j, "testnet", set(),
                                   outcomes=lb.executions)
    lines = path.read_text().splitlines()
    again = [json.dumps(json.loads(x), indent=None, separators=(",", ":"),
                        sort_keys=False) for x in lines]
    path.write_text("\n".join(lines + again) + "\n")
    rep = report(path, now_ms=clock[0])
    for name in ("linear/Sell/market", "linear/Buy/market"):
        assert leg(rep, name)["n"] == 21
        assert leg(rep, name)["rejected"] == {"DUPLICATE_ROW": 21}
    assert rep["records_duplicate_lines"] == 0


def test_a_second_process_appending_the_same_fills_is_not_double_counted(tmp_path):
    lb, j, clock = run_round_trips(21)
    path = str(tmp_path / "r.jsonl")
    drill.append_execution_records(path, j, "testnet", set(),
                                   outcomes=lb.executions)
    drill.append_execution_records(path, j, "testnet", set(),    # fresh `written`
                                   outcomes=lb.executions)
    rep = report(path, now_ms=clock[0])
    assert leg(rep, "linear/Buy/market")["n"] == 21


# -- partial fills ------------------------------------------------------------------

def test_a_partial_fill_is_one_sample_of_what_filled(tmp_path):
    lb, j, clock = run_round_trips(21, fill_frac=0.5)
    path = tmp_path / "r.jsonl"
    drill.append_execution_records(str(path), j, "testnet", set(),
                                   outcomes=lb.executions)
    rows = lines(path)
    for r in rows:
        rec = r["fee_raw"]["execution"]
        assert rec["filled_qty"] == pytest.approx(QTY / 2)
        assert rec["unfilled_qty"] == pytest.approx(QTY / 2)
        assert rec["fill_ratio"] == pytest.approx(0.5)
    # the sample is the filled half: $500, inside the size band of a $1000 plan
    rep = report(path, now_ms=clock[0])
    buy = leg(rep, "linear/Buy/market")
    assert buy["n_in_size_band"] == 21 and rep["outcomes"]["total"] == 0


def test_a_post_only_partial_and_its_cancel_increment_are_one_order():
    """The estimator merges `<link>` and `<link>+cancel` into one observation."""
    class E:
        def __init__(self, kind, ms, ref, rec):
            self.kind, self.ms, self.ref = kind, ms, ref
            self.fee_raw = {"execution": rec}

    def rec(link, q, ms):
        return valid_record(side="Sell", q=q, req=0.01, ref=100_000.0,
                            slip_usd=0.0, spr_usd=0.05, order_type="post_only",
                            ms=ms, link=link)
    entries = [E("perp_open", 10_000, "L1", rec("L1", 0.004, 10_000)),
               E("perp_open", 11_000, "L1+cancel", rec("L1+cancel", 0.002, 11_000))]
    obs, rejected = est.extract(entries, as_of_ms=20_000, symbol="BTCUSDT",
                                product="linear", side="Sell",
                                order_types=("post_only",))
    assert len(obs) == 1 and rejected == {}
    assert obs[0].notional_usd == pytest.approx(0.006 * 100_000.0)


# -- orders that did not fill -------------------------------------------------------

def test_an_unfilled_order_is_an_outcome_line_and_never_a_sample(tmp_path):
    lb, j, clock = run_round_trips(3, fill_frac=0.0)
    assert j.entries == []                           # nothing was booked
    nofill = [r for r in lb.executions if r["status"] == xr.NO_FILL]
    assert len(nofill) == 6
    path = tmp_path / "r.jsonl"
    n = drill.append_execution_records(str(path), j, "testnet", set(),
                                       outcomes=lb.executions)
    assert n == 6
    rows = lines(path)
    assert {r["kind"] for r in rows} == {drill.OUTCOME_KIND}
    assert all("fee_raw" not in r and r["execution"]["filled_qty"] in (0, 0.0)
               for r in rows)
    rep = report(path, now_ms=clock[0])
    for name in ("linear/Sell/market", "linear/Buy/market"):
        assert leg(rep, name)["n"] == 0 and leg(rep, name)["rejected"] == {}
    assert rep["outcomes"]["total"] == 6
    assert rep["outcomes"]["by_leg"] == {
        "linear/Sell/market": {"NO_FILL": 3}, "linear/Buy/market": {"NO_FILL": 3}}
    assert rep["outcomes"]["counted_as_samples"] == 0
    assert rep["status"] == est.UNAVAILABLE


def test_a_rejected_order_is_kept_as_an_outcome(tmp_path):
    clock = [T0]
    v = DepthVenue(clock, bid=99_995.0, ask=100_005.0, fill_px=100_000.0,
                   fail_create=True)
    lb, j = rig(v, clock)
    assert trade(lb, clock, "Sell", tolerate=True) is None
    assert j.entries == []
    rec, = lb.executions
    assert rec["status"] == xr.NO_FILL and rec["reasons"][0].startswith("NO_ORDER")
    path = tmp_path / "r.jsonl"
    assert drill.append_execution_records(str(path), j, "testnet", set(),
                                          outcomes=lb.executions) == 1
    row, = lines(path)
    assert row["kind"] == drill.OUTCOME_KIND and row["environment"] == "testnet"
    assert row["execution"]["requested_qty"] == QTY
    rep = report(path, now_ms=clock[0] + 1)
    assert rep["outcomes"]["reasons"] == {"NO_ORDER": 1}
    assert all(l["n"] == 0 for l in rep["legs"])


def test_a_submission_that_raised_is_kept_as_an_outcome(tmp_path):
    class Timeout(DepthVenue):
        def _request(self, method, endpoint, **kw):
            if endpoint == "/v5/order/create":
                raise RuntimeError("timed out")
            return super()._request(method, endpoint, **kw)

    clock = [T0]
    lb, j = rig(Timeout(clock, bid=99_995.0, ask=100_005.0,
                        fill_px=100_000.0), clock)
    trade(lb, clock, "Buy", tolerate=True)
    assert j.entries == []
    rec, = lb.executions
    assert rec["status"] == xr.NO_FILL
    assert rec["reasons"][0].startswith("SUBMISSION_FAILED")
    path = tmp_path / "r.jsonl"
    drill.append_execution_records(str(path), j, "testnet", set(),
                                   outcomes=lb.executions)
    rep = report(path, now_ms=clock[0] + 1)
    assert rep["outcomes"]["by_leg"] == {"linear/Buy/market": {"NO_FILL": 1}}
    assert all(l["n"] == 0 for l in rep["legs"])


def test_outcomes_are_environment_strict_and_deduplicated(tmp_path):
    lb, j, clock = run_round_trips(2, fill_frac=0.0)
    path = tmp_path / "r.jsonl"
    drill.append_execution_records(str(path), j, "testnet", set(),
                                   outcomes=lb.executions)
    text = path.read_text()
    path.write_text(text + text)
    first = report(path, now_ms=clock[0])
    assert first["outcomes"]["total"] == 4                           # not 8
    assert first["records_duplicate_lines"] == 4
    other = report(path, environment="mainnet", now_ms=clock[0])
    # the four distinct lines are skipped for the wrong environment; the four
    # copies are duplicates of them, dropped before the environment is looked at
    assert other["outcomes"]["total"] == 0 and other["records_skipped"] == 4
    assert other["records_duplicate_lines"] == 4


def test_outcome_writing_is_idempotent_within_a_process(tmp_path):
    lb, j, _c = run_round_trips(2, fill_frac=0.0)
    path, written = str(tmp_path / "r.jsonl"), set()
    assert drill.append_execution_records(path, j, "testnet", written,
                                          outcomes=lb.executions) == 4
    assert drill.append_execution_records(path, j, "testnet", written,
                                          outcomes=lb.executions) == 0


def test_a_filled_and_an_unfilled_order_are_kept_apart(tmp_path):
    clock = [T0]
    v = DepthVenue(clock, bid=99_995.0, ask=100_005.0, fill_px=100_000.0)
    lb, j = rig(v, clock)
    trade(lb, clock, "Sell")
    v.fill_frac = 0.0
    trade(lb, clock, "Buy")
    path = tmp_path / "r.jsonl"
    drill.append_execution_records(str(path), j, "testnet", set(),
                                   outcomes=lb.executions)
    kinds = sorted(r["kind"] for r in lines(path))
    assert kinds == [drill.OUTCOME_KIND, "perp_open"]


def test_an_outcome_can_never_reach_the_estimator_as_a_fill():
    """Belt and braces: even handed to the estimator, an outcome is no sample."""
    class E:
        def __init__(self, kind, rec):
            self.kind, self.ms, self.ref = kind, 10_000, "L1"
            self.fee_raw = {"execution": rec}

    nofill = xr.failed_submission(None, side="Buy", symbol="BTCUSDT",
                                  product="linear", venue="bybit",
                                  order_type="market", requested_qty=0.01,
                                  why="SUBMISSION_FAILED: X")
    for kind in (drill.OUTCOME_KIND, "perp_close"):
        obs, rejected = est.extract([E(kind, nofill)], as_of_ms=20_000,
                                    symbol="BTCUSDT", product="linear",
                                    side="Buy")
        assert obs == []
    assert drill.OUTCOME_KIND not in est.FILL_KINDS
    assert drill.OUTCOME_KIND == esr.OUTCOME_KIND            # the two stay pinned
    assert drill.EXECUTION_RECORDS_SCHEMA == esr.SCHEMA


def test_a_cancelled_unfilled_resting_order_is_an_outcome():
    class Inner:
        VENUE = "bybit"

        def place_post_only(self, **kw):
            return {"filled_qty": 0.0, "avg_price": 0.0, "order_link_id": "R1",
                    "submit_ms": T0, "venue": "bybit", "fee_detail": None}

        def cancel_order(self, **kw):
            return {"filled_qty": 0.0, "avg_price": 0.0, "updated_ms": T0 + 500}

    j = L.Journal()
    lb = L.LedgerBroker(Inner(), j, ms=lambda: T0 + 600)
    lb.place_post_only(symbol="BTCUSDT", side="Sell", qty=0.01, price=100_000.0,
                       product="linear")
    assert lb.executions == []                       # resting is not an outcome yet
    lb.cancel_order(symbol="BTCUSDT", order_link_id="R1", product="linear")
    rec, = lb.executions
    assert rec["status"] == xr.NO_FILL and rec["order_link_id"] == "R1"
    assert rec["reasons"][-1].startswith("CANCELLED_UNFILLED")
    assert rec["order_type"] == "post_only" and rec["requested_qty"] == 0.01
    assert j.entries == []
