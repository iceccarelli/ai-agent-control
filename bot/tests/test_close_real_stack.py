"""The staged close through the REAL stack: CarryEngine -> CloseExecutor ->
LedgerBroker -> CarryBroker -> a simulated Bybit HTTP surface (StateStore on
disk for both the order log and the close log).

Only the venue's HTTP answers are simulated. That is the point: the id handed
to the broker, the order row written before the request, the readback after a
failed send, the booking of the fill, and the executor's reads all run for real.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (os.path.dirname(HERE), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import close_plan as CP                                             # noqa: E402
import ledger as L                                                   # noqa: E402
from carry_broker import CarryBroker                                 # noqa: E402
from carry_engine import (OVERLAY, BookState, CarryEngine,           # noqa: E402
                          CarryPosition, Leg)
from close_exec_harness import Clock                                 # noqa: E402
from close_executor import CloseExecutor                             # noqa: E402
from persistence import StateStore                                   # noqa: E402

MARK = 100_000.0
OPENED = 1_700_000_000_000


def env(result):
    return {"retCode": 0, "retMsg": "OK", "result": result}


class Transport(Exception):
    ret_code = None


class SimHttp:
    """Bybit's order/position/execution endpoints for one linear symbol."""

    def __init__(self, short, mode="full"):
        self.net = -short
        self.mode = mode
        self.orders = {}
        self.creates = []

    def _request(self, method, endpoint, *, params=None, body=None, signed=False, retries=3):
        p = params or {}
        if endpoint == "/v5/order/create":
            self.creates.append(body["orderLinkId"])
            link = body["orderLinkId"]
            if self.mode == "drop":
                raise Transport("connection reset before the request left")
            qty = float(body["qty"])
            got = min(qty, max(0.0, -self.net))
            self.net += got
            self.orders[link] = {"orderLinkId": link, "orderStatus": "Filled",
                                 "cumExecQty": f"{got:.8f}", "avgPrice": str(MARK),
                                 "cumFeeDetail": {"USDT": "0.5"}, "execs": [(f"x-{link}", got)]}
            if self.mode == "lost":
                raise Transport("response lost after the venue accepted")
            return env({"orderId": "o1", "orderLinkId": link})
        if endpoint in ("/v5/order/realtime", "/v5/order/history"):
            o = self.orders.get(p.get("orderLinkId"))
            if endpoint == "/v5/order/realtime":
                return env({"list": []})              # a filled order has left the open list
            return env({"list": [o] if o else []})
        if endpoint == "/v5/execution/list":
            o = self.orders.get(p.get("orderLinkId"))
            rows = [{"execId": e, "execQty": f"{q:.8f}", "execPrice": str(MARK),
                     "execFee": "0.5"} for e, q in (o["execs"] if o else [])]
            return env({"list": rows})
        if endpoint == "/v5/position/list":
            if self.net == 0:
                return env({"list": [{"side": "", "size": "0", "positionIdx": 0}]})
            return env({"list": [{"side": "Sell" if self.net < 0 else "Buy",
                                  "size": f"{abs(self.net):.8f}", "positionIdx": 0}]})
        return env({})


def rig(tmp_path, short=0.01, mode="full"):
    store = StateStore(str(tmp_path / "s.db"))
    http = SimHttp(short, mode)
    clock = Clock()
    cb = CarryBroker(client=http, sequence_source=lambda *a: 1,
                     order_gate=lambda: (True, "T"), exit_gate=lambda: (True, "T"),
                     order_log=store, clock_ms=lambda: int(clock() * 1000))
    journal = L.Journal()
    lb = L.LedgerBroker(cb, journal, ms=lambda: int(clock() * 1000),
                        spot_symbol="BTCUSDT", book_overlay_inventory=True)
    pol = CP.Policy(max_attempts=3, qty_step=0.001, min_qty=0.001, max_waits=40, max_lag_waits=3)
    ex = CloseExecutor(broker=lb, reader=cb, store=store, symbol="BTCUSDT", policy=pol,
                       absence_window_s=60.0, clock=clock)
    tripped = []
    eng = CarryEngine(broker=lb, kill_switch=tripped.append, borrow_apr=0.0,
                      execution_mode=OVERLAY, persist=lambda s: None, close_executor=ex)
    qty = short
    eng.position = CarryPosition(
        spot=Leg(symbol="BTCUSDT", side="Buy", product="spot", requested_qty=qty,
                 filled_qty=qty, avg_price=MARK),
        perp=Leg(symbol="BTCUSDT", side="Sell", product="linear", requested_qty=qty,
                 filled_qty=qty, avg_price=MARK), opened_ms=OPENED)
    eng.state = BookState.HEDGED
    return eng, http, store, clock, tripped, journal


def drive(eng, clock, n=40):
    eng._unwind(MARK, "TEST")
    for _ in range(n):
        if eng.state in (BookState.FLAT, BookState.HALTED):
            break
        clock.advance(10)
        eng.on_candle(mark=MARK, funding_bps=1.0, spot=MARK, timestamp_ms=OPENED + 10_000)


def test_a_normal_close_goes_through_the_real_broker_is_booked_and_flat(tmp_path):
    eng, http, store, clock, tripped, journal = rig(tmp_path)
    drive(eng, clock)
    assert eng.state is BookState.FLAT and tripped == []
    assert http.net == 0 and len(http.creates) == 1
    link = CP.link_id_for(eng.position.cycle_id if eng.position else "cyc-%d" % OPENED, 1)
    assert http.creates == [link]
    row = store.get_order(link)
    assert row is not None                       # the broker wrote the order row first
    assert len(journal.entries) >= 1             # and the fill was booked once


def test_a_lost_response_is_resolved_by_the_brokers_readback_and_booked_once(tmp_path):
    eng, http, store, clock, tripped, journal = rig(tmp_path, mode="lost")
    drive(eng, clock)
    assert len(http.creates) == 1                # never sent twice
    assert http.net == 0
    assert eng.state is BookState.FLAT and tripped == []
    kinds = [e.kind for e in journal.entries]
    assert kinds.count("perp_close") == 1        # the readback result was booked, once (never twice)
    # This rig hands the engine a position the journal never saw opened, so there is no spot reference for the hedged coin: its inventory leg is
    # recorded as UNBOOKED (a $0 marker), not silently dropped. That marker is the only other entry.
    assert kinds == ["perp_close", "inventory_unbooked"]


def test_a_request_that_never_left_is_not_assumed_to_have_failed_and_not_resent_early(tmp_path):
    eng, http, store, clock, tripped, journal = rig(tmp_path, mode="drop")
    eng._unwind(MARK, "TEST")
    for _ in range(3):
        clock.advance(10)
        eng.on_candle(mark=MARK, funding_bps=1.0, spot=MARK, timestamp_ms=OPENED + 10_000)
    assert len(http.creates) == 1                # one attempt, waiting on its proof
    assert eng.state is BookState.UNWINDING and tripped == []
    assert http.net == pytest.approx(-0.01)
    http.mode = "full"
    for _ in range(12):
        clock.advance(10)
        eng.on_candle(mark=MARK, funding_bps=1.0, spot=MARK, timestamp_ms=OPENED + 10_000)
    assert len(http.creates) == 2 and len(set(http.creates)) == 2
    assert http.net == 0
