"""Execution records are durable at fill time, and cover the safety-net close.

They used to be appended once, at report time, from an in-memory journal: a
hard kill (SIGKILL after the 120 s grace, an OOM kill, a crash) lost every
fill of the run, and the reduce-only safety-net close ran in another process
that wrote nothing. The exits that the drill itself failed to complete - the
worst exits - were therefore the ones that never reached the sample set, which
biases the Buy leg towards good exits.

`LedgerBroker.record_sink` is called synchronously right after each fill or
outcome is booked; the drill's `RecordWriter` appends and fsyncs the line
before the next thing happens. The end-of-run append stays as a reconciliation
(it shares the writer's key set, so nothing is written twice).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for p in (BOT, os.path.join(BOT, "tools"), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import drill                                                  # noqa: E402
import ledger as L                                            # noqa: E402

QTY = 0.001


class Venue:
    """A venue that fills at a settable price and quotes a book."""
    VENUE = "testnet"
    px = 100_000.0

    def __init__(self, short=QTY):
        self.short, self.sent = short, []

    def get_mark(self, s): return self.px
    def get_spot_mark(self, s): return self.px
    def get_book_top(self, symbol, product): return {"bid": self.px - 5,
                                                      "ask": self.px + 5}
    def get_perp_position(self, s): return float(self.short)
    def get_open_carry_orders(self, a, b): return []
    def get_position_slots(self, s): return []

    def place_market(self, *, symbol, side, qty, product):
        self.sent.append((side, qty, product))
        if side == "Buy" and product == "linear":
            self.short = max(0.0, self.short - qty)
        return {"filled_qty": qty, "avg_price": self.px,
                "order_link_id": f"L{len(self.sent)}", "fee": 0.2,
                "fee_status": "confirmed"}


def broker(venue, sink, journal=None):
    return L.LedgerBroker(venue, journal or L.Journal(),
                          ms=lambda: 1_700_000_000_000,
                          record_sink=sink)


def fill(b, side, px=None):
    b.begin_decision(symbol="BTCUSDT", product="linear", side=side, qty=QTY,
                     mark=100_000.0, bid=99_995.0, ask=100_005.0,
                     observed_ms=1_700_000_000_000 - 20)
    return b.place_market(symbol="BTCUSDT", side=side, qty=QTY,
                          product="linear")


# -- the sink ---------------------------------------------------------------------

def test_each_fill_reaches_the_sink_as_it_is_booked():
    got = []
    b = broker(Venue(), got.append)
    fill(b, "Sell")
    assert [e["type"] for e in got] == ["fill"]
    assert got[0]["kind"] == "perp_open" and got[0]["ref"] == "L1"
    assert got[0]["fee_raw"]["execution"]["side"] == "Sell"
    fill(b, "Buy")
    assert [e["kind"] for e in got] == ["perp_open", "perp_close"]


def test_an_order_that_filled_nothing_reaches_the_sink_as_an_outcome():
    got = []

    class Rejects(Venue):
        def place_market(self, **kw):
            return None
    b = broker(Rejects(), got.append)
    b.place_market(symbol="BTCUSDT", side="Sell", qty=QTY, product="linear")
    assert [e["type"] for e in got] == ["outcome"]
    assert got[0]["execution"]["status"] == "NO_FILL"


def test_a_raising_sink_never_touches_the_booking_or_the_order():
    def boom(_e):
        raise OSError("disk full")
    b = broker(Venue(), boom)
    out = fill(b, "Sell")
    assert out["filled_qty"] == QTY                   # the order result stands
    assert any(e.kind == "perp_open" for e in b.journal.entries)
    assert b.sink_errors == 1 and "disk full" in b.sink_last_error


def test_no_sink_is_the_old_behaviour():
    b = L.LedgerBroker(Venue(), L.Journal(), ms=lambda: 1_700_000_000_000)
    fill(b, "Sell")
    assert b.sink_errors == 0


# -- the writer --------------------------------------------------------------------

def test_the_writer_appends_and_labels_each_line_at_once(tmp_path):
    path = str(tmp_path / "r.jsonl")
    w = drill.RecordWriter(path, "testnet", set())
    b = broker(Venue(), w)
    fill(b, "Sell")
    lines = [json.loads(x) for x in open(path).read().splitlines()]
    assert len(lines) == 1                        # on disk BEFORE any report
    assert lines[0]["environment"] == "testnet" and lines[0]["kind"] == "perp_open"
    assert lines[0]["schema"] == drill.EXECUTION_RECORDS_SCHEMA


def test_the_end_of_run_flush_does_not_duplicate_what_the_writer_wrote(tmp_path):
    path = str(tmp_path / "r.jsonl")
    written = set()
    w = drill.RecordWriter(path, "testnet", written)
    b = broker(Venue(), w)
    fill(b, "Sell")
    fill(b, "Buy")
    n = drill.append_execution_records(path, b.journal, "testnet", written,
                                       outcomes=b.executions)
    assert n == 0
    assert len(open(path).read().splitlines()) == 2


def test_a_failed_durable_write_is_picked_up_by_the_flush(tmp_path):
    path = str(tmp_path / "r.jsonl")
    written = set()
    w = drill.RecordWriter(str(tmp_path), "testnet", written)   # a directory
    b = broker(Venue(), w)
    fill(b, "Sell")
    assert w.errors == 1 and written == set()
    assert drill.append_execution_records(path, b.journal, "testnet", written,
                                          outcomes=b.executions) == 1


def test_a_hard_kill_loses_nothing_that_already_filled(tmp_path):
    path = str(tmp_path / "r.jsonl")
    script = textwrap.dedent(f"""
        import os, signal, sys
        sys.path[:0] = {[BOT, os.path.join(BOT, "tools"), HERE]!r}
        import drill, ledger as L
        from test_durable_execution_records import Venue, broker, fill
        w = drill.RecordWriter({path!r}, "testnet", set())
        b = broker(Venue(), w)
        fill(b, "Sell")
        fill(b, "Buy")
        os.kill(os.getpid(), signal.SIGKILL)       # no report, no cleanup
    """)
    r = subprocess.run([sys.executable, "-c", script])
    assert r.returncode == -signal.SIGKILL
    kinds = [json.loads(x)["kind"] for x in open(path).read().splitlines()]
    assert kinds == ["perp_open", "perp_close"]


# -- the safety-net close ----------------------------------------------------------

def test_the_safety_net_close_is_recorded_and_book_measured(tmp_path):
    import close_testnet_short as cts
    path = str(tmp_path / "r.jsonl")
    v = Venue(short=QTY)
    out = cts.close_short(v, "BTCUSDT", expected_qty=QTY, sleep=lambda s: None,
                          records_path=path, venue_label="testnet")
    assert out["ok"] and out["sent"]
    (line,) = [json.loads(x) for x in open(path).read().splitlines()]
    assert line["kind"] == "perp_close" and line["environment"] == "testnet"
    ex = line["fee_raw"]["execution"]
    assert ex["side"] == "Buy" and ex["reference_basis"] == "executable_side"
    assert ex["status"] == "MEASURED"


def test_a_booking_failure_after_the_order_is_sent_still_verifies_flat(tmp_path):
    import close_testnet_short as cts
    path = str(tmp_path / "r.jsonl")
    v = Venue(short=QTY)
    orig = L.perp_close

    def boom(*a, **k):
        raise RuntimeError("booking exploded")
    L.perp_close = boom
    try:
        out = cts.close_short(v, "BTCUSDT", expected_qty=QTY,
                              sleep=lambda s: None, records_path=path,
                              venue_label="testnet")
    finally:
        L.perp_close = orig
    assert out["sent"] is True and out["flat_verified"] is True
    assert "booking" in str(out.get("recording_error", "")).lower()
    assert v.sent == [("Buy", QTY, "linear")]          # one order, not two


def test_without_a_records_path_the_close_is_exactly_as_before():
    import close_testnet_short as cts
    v = Venue(short=QTY)
    out = cts.close_short(v, "BTCUSDT", expected_qty=QTY, sleep=lambda s: None)
    assert out["ok"] and v.sent == [("Buy", QTY, "linear")]
    assert "recording_error" not in out


def test_the_unattended_runner_passes_the_records_file_to_the_close():
    src = open(os.path.join(BOT, "tools", "unattended_drill.py")).read()
    i = src.index('os.path.join(TOOLS, "close_testnet_short.py")')
    assert "--execution-records" in src[i:i + 700]


# -- a line's time must agree with the record inside it ---------------------------

def test_a_line_whose_time_disagrees_with_its_record_is_not_a_fresh_sample():
    import time as _t
    import execution_cost_estimate as est
    from execution_fixtures import valid_record
    now = int(_t.time() * 1000)

    class E:
        def __init__(self, ms, rec, i):
            self.kind, self.ms, self.ref = "perp_open", ms, f"o{i}"
            self.fee_raw = {"execution": rec}

    def run(record_age_ms):
        entries = []
        for i in range(25):
            line_ms = now - 60_000 - 2000 * i                 # looks fresh
            rec = valid_record("Sell", "linear",
                               ms=line_ms - record_age_ms, link=f"o{i}")
            entries.append(E(line_ms, rec, i))
        return est.extract(entries, as_of_ms=now, symbol="BTCUSDT",
                           product="linear", side="Sell")

    obs, rej = run(0)                                         # control
    assert len(obs) == 25 and "LINE_TIME_MISMATCH" not in rej
    obs, rej = run(100 * 86_400_000)      # the record is 100 days older
    assert obs == [] and rej.get("LINE_TIME_MISMATCH") == 25


# -- a records problem is visible, and never an order-path result -----------------

def test_a_records_problem_is_reported_and_is_not_an_order_path_failure():
    import unattended_drill as ud
    assert ud.records_problem(None) is None
    assert ud.records_problem({"execution_records": {
        "durable_write_errors": 0, "appended": 0}}) is None
    # the durable write failed but the end-of-run flush recovered everything
    assert ud.records_problem({"execution_records": {
        "durable_write_errors": 2, "appended": 2}}) is None
    msg = ud.records_problem({"execution_records": {
        "durable_write_errors": 2, "appended": 0,
        "durable_write_last_error": "OSError: disk full"}})
    assert msg and "disk full" in msg
    assert "end-of-run records write failed" in ud.records_problem(
        {"execution_records_error": "OSError: x"})
    v = ud.verdict(preflight=0, drill=0, flat=0, recon=0, reconciled=True,
                   orders_placed=True)
    assert v["ok"] is True and "execution_records_problem" not in v["problems"]


# -- partial fills are reported, not just counted as ordinary fills ---------------

def test_the_report_shows_partial_fills_and_their_unfilled_remainder(tmp_path):
    import execution_samples_report as esr
    from execution_fixtures import valid_record
    j = L.Journal()
    base = 1_700_000_000_000
    for i, (q, req) in enumerate([(0.010, 0.010), (0.004, 0.010),
                                  (0.006, 0.010)]):
        rec = valid_record("Sell", "linear", q=q, req=req, ms=base + 10_000 * i,
                           link=f"o{i}")
        L.perp_open(j, ms=base + 10_000 * i, ref=f"o{i}", qty=q, price=60000.0,
                    fee_usd=0.2, fee_status="confirmed",
                    fee_raw={"execution": rec})
    path = str(tmp_path / "r.jsonl")
    drill.append_execution_records(path, j, "testnet", set())
    out = esr.build("", mode="overlay", notional_usd=600.0, perp="BTCUSDT",
                    spot="BTCUSDT", now_ms=base + 100_000, records=[path],
                    environment="testnet")
    leg = out["partial_fills"]["linear/Sell/market"]
    assert leg["orders"] == 2                       # the 0.004 and the 0.006
    assert abs(leg["unfilled_qty"] - 0.010) < 1e-9  # 0.006 + 0.004 left over
