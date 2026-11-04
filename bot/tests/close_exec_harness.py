"""A deterministic broker + clock + crash harness for the close executor.

The FakeBroker speaks the same four calls the executor uses (signed net
position, order read by link id, execution list, reduce-only market Buy with a
caller-supplied id) and fails the ways Bybit can: a response that never comes
(order executed or not), a position read that lags, a lookup that lags or
errors, an executions list that disagrees with the order row, and a process
that dies at an exact point. Everything is driven by explicit knobs and a
manual clock; nothing here is random unless a test seeds it.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (BOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import close_plan as CP                                           # noqa: E402
from carry_broker import CarryOrderRefused, OrderOutcomeUnknown   # noqa: E402
from close_executor import CloseExecutor                           # noqa: E402
from persistence import StateStore                                 # noqa: E402


class Crash(BaseException):
    """The process died here. A BaseException so no `except Exception` eats it."""


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class FakeBroker:
    """One venue, one symbol. `short` is the opening short in base units."""

    def __init__(self, short: float, *, modes: Sequence[str] = (), clock: Clock,
                 position_lag_reads: int = 0, position_unreadable_reads: int = 0,
                 order_lag_reads: int = 0, order_unreadable_reads: int = 0,
                 exec_mismatch: bool = False, hide_orders: bool = False,
                 trusting: bool = False, position_says_long: bool = False,
                 exit_allowed: bool = True, ack_claims_fill: bool = False):
        self.clock = clock
        self.net = -float(short)
        self.initial_short = float(short)
        self.modes = list(modes)
        self.orders: Dict[str, dict] = {}
        self.sends: Dict[str, int] = {}
        self.placed: List[tuple] = []
        self.position_lag_reads = position_lag_reads
        self._stale_net: Optional[float] = None
        self.position_unreadable_reads = position_unreadable_reads
        self.order_lag_reads = order_lag_reads
        self.order_unreadable_reads = order_unreadable_reads
        self.exec_mismatch = exec_mismatch
        self.hide_orders = hide_orders
        self.trusting = trusting
        self.position_says_long = position_says_long
        self.exit_allowed = exit_allowed
        self.ack_claims_fill = ack_claims_fill
        self.crash_after_accept: set = set()       # link ids: venue accepts, process dies
        self.crash_before_send: set = set()        # link ids: process dies before the request leaves
        self._x = 0
        self.max_net = self.net
        self.reads = {"position": 0, "order": 0, "exec": 0}

    # -- the four calls ------------------------------------------------------
    def get_perp_net_qty(self, symbol: str) -> float:
        self.reads["position"] += 1
        if self.position_unreadable_reads > 0:
            self.position_unreadable_reads -= 1
            raise RuntimeError("position endpoint down")
        if self.position_says_long:
            return abs(self.net) + 0.5
        if self.position_lag_reads > 0 and self._stale_net is not None:
            self.position_lag_reads -= 1
            return self._stale_net
        return self.net

    def read_order(self, symbol: str, link_id: str, product: str = "linear"):
        self.reads["order"] += 1
        if self.order_unreadable_reads > 0:
            self.order_unreadable_reads -= 1
            raise RuntimeError("order endpoint down")
        o = self.orders.get(link_id)
        if o is None or self.hide_orders:
            return None
        if self.order_lag_reads > 0:
            self.order_lag_reads -= 1
            return None
        return {"filled_qty": o["filled"], "status": o["status"],
                "order_link_id": link_id, "avg_price": 100.0 if o["filled"] else 0.0,
                "resting": o["status"] in CP.LIVE_STATUSES}

    def get_order_executions(self, symbol: str, *, order_link_id: str = "",
                             order_id: str = "", product: str = "linear"):
        self.reads["exec"] += 1
        o = self.orders.get(order_link_id)
        if not o:
            return []
        rows = [{"execId": e, "execQty": q} for e, q in o["execs"]]
        if self.exec_mismatch and rows:
            rows[0] = dict(rows[0], execQty=rows[0]["execQty"] + 0.001)
        return rows

    def place_market(self, *, symbol: str, side: str, qty: float, product: str,
                     order_link_id: Optional[str] = None):
        if side != "Buy" or product != "linear":
            raise AssertionError("a close sends only a linear Buy")
        if not self.exit_allowed:
            raise CarryOrderRefused("exit gate closed; no carry order sent")
        link = order_link_id
        if link in self.crash_before_send:
            raise Crash(f"died before sending {link}")
        self.sends[link] = self.sends.get(link, 0) + 1
        self.placed.append((link, float(qty), self.net))
        if link in self.orders:
            raise RuntimeError(f"duplicate orderLinkId {link}")      # a venue that refuses repeats
        mode = self.modes.pop(0) if self.modes else "full"
        if mode == "drop":
            raise TimeoutError("request never reached the venue")
        if mode == "reject":
            self.orders[link] = {"qty": qty, "filled": 0.0, "execs": [],
                                 "status": "Rejected", "visible_after": 0}
            return None
        short = max(0.0, -self.net)
        want = float(qty) if self.trusting else min(float(qty), short)
        frac = 1.0
        status = "Filled"
        if mode.startswith("partial:"):
            frac = float(mode.split(":")[1])
            status = "PartiallyFilledCanceled"
        elif mode == "live":
            frac, status = 0.0, "New"
        got = round(want * frac, 10)
        self._stale_net = self.net
        self.net = round(self.net + got, 10)
        self.max_net = max(self.max_net, self.net)
        self._x += 1
        execs = [(f"x{self._x}", got)] if got > 0 else []
        self.orders[link] = {"qty": qty, "filled": got, "execs": execs, "status": status}
        if link in self.crash_after_accept:
            raise Crash(f"died after the venue accepted {link}")
        if mode == "lost":
            raise TimeoutError("response lost after acceptance")
        if self.ack_claims_fill:
            return {"filled_qty": float(qty), "avg_price": 100.0, "order_link_id": link}
        return {"filled_qty": got, "avg_price": 100.0, "order_link_id": link}

    def live_orders(self) -> int:
        return sum(1 for o in self.orders.values() if o["status"] in CP.LIVE_STATUSES)

    def finish_live(self, link: str, filled: Optional[float] = None) -> None:
        """A resting order ends: filled in full, or cancelled with what it got."""
        o = self.orders[link]
        short = max(0.0, -self.net)
        add = min(short, (o["qty"] - o["filled"]) if filled is None else filled)
        if add > 0:
            self._stale_net = self.net
            self.net = round(self.net + add, 10)
            self._x += 1
            o["execs"].append((f"x{self._x}", add))
            o["filled"] = round(o["filled"] + add, 10)
        o["status"] = "Filled" if o["filled"] >= o["qty"] - 1e-12 else "PartiallyFilledCanceled"


class CrashStore:
    """A StateStore that can die just before or just after a named write
    commits. The wrapped store is the same file, so a 'restart' reopens it."""

    def __init__(self, store: StateStore):
        self.inner = store
        self.armed: Optional[tuple] = None        # (method, "before"|"after", nth)
        self.calls: Dict[str, int] = {}

    def arm(self, method: str, when: str, nth: int = 1) -> None:
        self.armed = (method, when, nth)
        self.calls[method] = 0

    def __getattr__(self, name):
        target = getattr(self.inner, name)
        if not callable(target) or name not in ("apply_close_step", "mark_close_sent"):
            return target

        def call(*a, **kw):
            self.calls[name] = self.calls.get(name, 0) + 1
            hit = (self.armed and self.armed[0] == name and self.calls[name] == self.armed[2])
            if hit and self.armed[1] == "before":
                self.armed = None
                raise Crash(f"died before {name}")
            out = target(*a, **kw)
            if hit and self.armed and self.armed[1] == "after":
                self.armed = None
                raise Crash(f"died after {name}")
            return out
        return call


class Rig:
    """store file + venue + clock; `executor()` builds a fresh executor over the
    same durable log, which is what a restart is."""

    def __init__(self, tmp_path, short: float = 0.0100, *, policy: Optional[CP.Policy] = None,
                 window: float = 60.0, **venue_kw):
        self.path = str(tmp_path / "state.db")
        self.clock = Clock()
        self.venue = FakeBroker(short, clock=self.clock, **venue_kw)
        self.policy = policy or CP.Policy(max_attempts=3, qty_step=0.001, min_qty=0.001,
                                          max_waits=12, max_lag_waits=3)
        self.window = window
        self.cid = "cyc-1"
        self.store = CrashStore(StateStore(self.path))
        self.restarts = 0

    def executor(self) -> CloseExecutor:
        return CloseExecutor(broker=self.venue, store=self.store, symbol="BTCUSDT",
                             policy=self.policy, absence_window_s=self.window,
                             clock=self.clock)

    def restart(self) -> CloseExecutor:
        """The process died: reopen the same file, build a new executor."""
        try:
            self.store.inner.close()
        except Exception:
            pass
        self.store = CrashStore(StateStore(self.path))
        self.restarts += 1
        return self.executor()

    def attempts(self) -> List[dict]:
        return self.store.inner.close_attempts(self.cid)

    def run(self, ex: Optional[CloseExecutor] = None, *, steps: int = 80, dt: float = 5.0,
            on_step=None):
        """Step until FLAT or HALT. A Crash restarts the executor and goes on."""
        ex = ex or self.executor()
        last = None
        for i in range(steps):
            try:
                last = ex.step(self.cid)
            except Crash:
                ex = self.restart()
                continue
            if on_step:
                on_step(i, last)
            if last.status in ("FLAT", "HALT"):
                return last, ex
            self.clock.advance(dt)
        return last, ex
