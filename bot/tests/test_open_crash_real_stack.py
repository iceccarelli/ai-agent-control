"""The OVERLAY open, killed at every boundary of its order lifecycle, through the REAL stack.

CarryEngine -> LedgerBroker -> CarryBroker -> StateStore (order log, on disk) -> a
simulated Bybit HTTP surface. Only the venue's HTTP answers are simulated; the id,
the order row written before the request, the readback, the booking of the fill, the
restart recovery and the cold-start reconciliation all run for real.

The invariants, checked after every scenario:
  O1 at most ONE create ever reaches the venue for one open (never a second id)
  O2 the journal books the venue's fill exactly once, never twice, never zero times
     when the venue filled and the book believes it holds the pair
  O3 a restart NEVER starts from CLEAN while the venue holds a position the ledger does
     not know: it is HALT (kill switch engaged) or RESUME of the exact pair
  O4 a restart that finds the venue flat and no fill starts CLEAN, and a fresh open
     sends exactly one more order
"""
from __future__ import annotations

import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (os.path.dirname(HERE), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import ledger as L                                                   # noqa: E402
import order_recovery as OR                                          # noqa: E402
from carry_broker import CarryBroker                                 # noqa: E402
from carry_engine import OVERLAY, BookState, CarryEngine, ColdStart  # noqa: E402
from carry_risk import CarryRisk                                     # noqa: E402
from close_exec_harness import Clock, Crash                          # noqa: E402
from persistence import StateStore                                   # noqa: E402
import test_close_real_stack as T                                    # noqa: E402

QTY = 0.01
RULES = {"qty_step": 0.001}


class Transport(Exception):
    ret_code = None


class Venue(T.SimHttp):
    """Bybit for one linear symbol, with a one-shot fault on the first create."""

    def __init__(self, fault=None):
        super().__init__(0.0)
        self.net = 0.0
        self.fault = fault

    def _request(self, method, endpoint, *, params=None, body=None, signed=False, retries=3, **kw):
        if endpoint == "/v5/order/create":
            link = body["orderLinkId"]
            fault, self.fault = self.fault, None
            self.creates.append(link)
            if fault == "before_send":
                self.creates.pop()
                raise Crash("died before the request left")
            if fault == "drop":
                raise Transport("connection reset before the request left")
            q = float(body["qty"])
            self.net += (-q if body["side"] == "Sell" else q)
            self.orders[link] = {"orderLinkId": link, "orderStatus": "Filled", "cumExecQty": f"{q:.8f}",
                                 "avgPrice": str(T.MARK), "cumFeeDetail": {"USDT": "0.5"},
                                 "execs": [(f"x-{link}", q)]}
            if fault == "lost":
                raise Transport("response lost after the venue accepted")
            if fault == "after_accept":
                raise Crash("died after the venue accepted the order")
            return T.env({"orderId": "o1", "orderLinkId": link})
        return super()._request(method, endpoint, params=params, body=body, signed=signed, retries=retries)


class Stack:
    def __init__(self, tmp_path, venue, persist=None):
        self.path = str(tmp_path / "s.db")
        self.venue, self.clock = venue, Clock()
        self.killed = []
        self.persisted = []
        self._build(persist)

    def _build(self, persist=None):
        self.store = StateStore(self.path)
        self.cb = CarryBroker(client=self.venue, sequence_source=lambda *a: 1,
                              order_gate=lambda: (True, "T"), exit_gate=lambda: (True, "T"),
                              order_log=self.store, clock_ms=lambda: int(self.clock() * 1000))
        self.journal = L.Journal()
        self.lb = L.LedgerBroker(self.cb, self.journal, ms=lambda: int(self.clock() * 1000),
                                 spot_symbol="BTCUSDT", book_overlay_inventory=True)
        self.eng = CarryEngine(broker=self.lb, kill_switch=self.killed.append, borrow_apr=0.0,
                               execution_mode=OVERLAY,
                               persist=persist or (lambda s: self.persisted.append(dict(s))))
        self.eng.pair_risk = CarryRisk(max_notional_usd=1000.0)

    def open(self):
        return self.eng._open_overlay(QTY, QTY, T.MARK, T.MARK, 1.0, T.OPENED, RULES)

    def restart(self):
        """The process died: reopen the same file with a new stack, then run what
        `main.carry_cold_start` runs: recovery first, then the ledger-vs-venue plan."""
        try:
            self.store.close()
        except Exception:
            pass
        self.clock.advance(3600)                  # long enough for any absence to be provable
        self.killed = []
        self._build()
        report = OR.resolve_unresolved_orders(self.cb, self.store, now_epoch=time.time() + 3600)
        ledger = self.persisted[-1] if self.persisted else self.store.load_carry_position()
        decision = self.eng.cold_start(
            ledger=ledger, venue_perp_qty=self.cb.get_perp_position("BTCUSDT"),
            venue_spot_qty=QTY, open_orders=self.cb.get_open_carry_orders("BTCUSDT", "BTCUSDT"))
        return report, decision

    def perp_open_entries(self):
        return [e for e in self.journal.entries if e.kind == "perp_open"]


def test_a_clean_open_books_one_fill_and_sends_one_order(tmp_path):
    v = Venue()
    s = Stack(tmp_path, v)
    d = s.open()
    assert d.action == "opened" and s.eng.state is BookState.HEDGED
    assert len(v.creates) == 1 and v.net == pytest.approx(-QTY)               # O1
    assert len(s.perp_open_entries()) == 1                                    # O2


@pytest.mark.parametrize("fault", ["lost"])
def test_a_lost_acknowledgement_is_read_back_and_booked_once_without_a_second_order(tmp_path, fault):
    v = Venue(fault)
    s = Stack(tmp_path, v)
    s.open()
    assert len(v.creates) == 1                                                # O1
    assert v.net == pytest.approx(-QTY)
    booked = sum(e.kind == "perp_open" for e in s.journal.entries)
    assert booked <= 1                                                        # O2: never twice


def test_a_dropped_request_is_unknown_not_failed_and_a_restart_stays_halted(tmp_path):
    """A request whose fate the process cannot tell is UNKNOWN: the book halts, sends
    nothing on top of it, and a restart does not turn the doubt into a clean start."""
    v = Venue("drop")
    s = Stack(tmp_path, v)
    d = s.open()
    assert d.action != "opened" and s.eng.state is BookState.HALTED
    assert v.net == 0.0 and len(v.creates) == 1                               # O1
    report, decision = s.restart()
    assert decision.action is ColdStart.HALT and s.killed                      # never CLEAN on a doubt
    assert len(v.creates) == 1


def test_death_after_the_submit_marker_but_before_the_request_is_proven_never_sent_later(tmp_path):
    v = Venue("before_send")
    s = Stack(tmp_path, v)
    with pytest.raises(Crash):
        s.open()
    assert v.creates == [] and v.net == 0.0
    report, decision = s.restart()
    assert report.unresolved == []                       # absence is proven once the window has passed
    assert decision.action is ColdStart.CLEAN                                 # O4
    assert s.killed == []


def test_death_between_the_intent_and_the_submit_marker_proves_never_sent_at_once(tmp_path):
    """The marker is written before the request, so an intent with no marker proves no
    request was made."""
    v = Venue()
    s = Stack(tmp_path, v)
    real = s.store.apply_order_event

    def die_on_marker(link, kind, **kw):
        if kind == "submit_attempt":
            raise Crash("died after the intent, before the marker")
        return real(link, kind, **kw)
    s.store.apply_order_event = die_on_marker
    with pytest.raises(Crash):
        s.open()
    assert v.creates == [] and v.net == 0.0
    s.store.apply_order_event = real
    report = OR.resolve_unresolved_orders(s.cb, s.store, now_epoch=time.time())     # NOW, not an hour later
    assert report.unresolved == []


@pytest.mark.parametrize("fault", ["after_accept", "persist"])
def test_death_after_the_venue_filled_halts_the_restart_rather_than_starting_clean(tmp_path, fault):
    """The venue holds a short the ledger never recorded. The restart must say so,
    engage the kill switch and send nothing: not CLEAN (a second hedge), not a guess."""
    if fault == "persist":
        v = Venue()

        def die(_state):
            raise Crash("died before the position was persisted")
        s = Stack(tmp_path, v, persist=die)
    else:
        v = Venue(fault)
        s = Stack(tmp_path, v)
    with pytest.raises(Crash):
        s.open()
    assert len(v.creates) == 1 and v.net == pytest.approx(-QTY)
    sends_before = len(v.creates)
    report, decision = s.restart()
    assert report.unresolved == []
    assert decision.action is ColdStart.HALT                                  # O3
    assert s.killed and "never recorded" in s.killed[0]
    assert len(v.creates) == sends_before                                     # nothing sent by the restart
    assert s.eng.state is BookState.HALTED


def test_a_reused_order_id_with_a_different_payload_sends_nothing_and_is_not_resumed(tmp_path):
    """The id is on file for a Sell of QTY; a request that reuses it for a Buy, a
    different size or another symbol is a different order. It is refused, not resumed
    (which would adopt the old order's venue state) and not sent."""
    from carry_broker import OrderOutcomeUnknown
    v = Venue()
    s = Stack(tmp_path, v)
    s.open()
    link = v.creates[0]
    import carry_broker as CB
    t = CB._OrderTrail(s.cb, QTY)
    for symbol, side, qty in (("BTCUSDT", "Buy", QTY), ("BTCUSDT", "Sell", QTY * 2), ("ETHUSDT", "Sell", QTY)):
        # unknown, not refused: a refusal would tell the close executor the order never existed
        with pytest.raises(OrderOutcomeUnknown, match="different order"):
            t.intent(link, "linear", symbol, side, f"{qty:.8f}", T.MARK, qty)
    assert len(v.creates) == 1
    # a re-send of the same id for LESS (what is left of a partly filled order) is the same order
    t3 = CB._OrderTrail(s.cb, QTY / 2)
    try:
        t3.intent(link, "linear", "BTCUSDT", "Sell", f"{QTY / 2:.8f}", T.MARK, QTY / 2)
    except CB.OrderOutcomeUnknown as exc:
        assert "different order" not in str(exc)
    # the same payload is still an ordinary retry: asked of the venue, never re-sent
    t2 = CB._OrderTrail(s.cb, QTY)
    try:
        t2.intent(link, "linear", "BTCUSDT", "Sell", f"{QTY:.8f}", T.MARK, QTY)
    except CB.OrderOutcomeUnknown:
        pass
    assert len(v.creates) == 1
