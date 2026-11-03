"""A deterministic fake venue that fails the ways a real one does.

It models only what a close needs: a signed perp position, reduce-only Buy
orders keyed by order link id (one order per id, ever), partial fills, a lost
response, a late fill, a lagging position read and a lagging order lookup.
Nothing here touches a network."""
from __future__ import annotations

import random
from typing import Dict, List, Optional, Sequence

import close_plan as CP


class Timeout(Exception):
    """The response never arrived. The order MAY have executed."""


class Venue:
    def __init__(self, short: float, *, modes: Sequence[str] = (),
                 position_lag_reads: int = 0, order_lag_reads: int = 0,
                 unreadable_reads: int = 0):
        self.net = -float(short)
        self.initial_short = float(short)
        self.modes = list(modes)            # per NEW order: full | partial:<f> | lost | drop | reject | late
        self.orders: Dict[str, dict] = {}
        self.placed: List[tuple] = []       # (link_id, qty, short_at_time)
        self.bought = 0.0
        self._exec = 0
        self.position_lag_reads = position_lag_reads
        self._stale_net: Optional[float] = None
        self.order_lag_reads = order_lag_reads
        self.unreadable_reads = unreadable_reads
        self.duplicate_sends_seen = 0

    # -- reads (may lag or fail) --------------------------------------------------------
    def read_position(self) -> Optional[float]:
        if self.unreadable_reads > 0:
            self.unreadable_reads -= 1
            return None
        if self.position_lag_reads > 0 and self._stale_net is not None:
            self.position_lag_reads -= 1
            return self._stale_net
        return self.net

    def read_order(self, link_id: str) -> Optional[CP.OrderObs]:
        o = self.orders.get(link_id)
        if o is None:
            return None
        if self.order_lag_reads > 0:
            self.order_lag_reads -= 1
            return None
        return CP.OrderObs(o["status"], o["filled"], tuple(o["execs"]))

    def observe(self, attempts: Sequence[CP.Attempt], lag_waits: int = 0) -> CP.Observation:
        orders = {}
        for a in attempts:
            ob = self.read_order(a.link_id)
            if ob is not None:
                orders[a.link_id] = ob
        return CP.Observation(net_qty=self.read_position(), orders=orders, lag_waits=lag_waits)

    # -- writes ---------------------------------------------------------------------------
    def _fill(self, o: dict, qty: float) -> None:
        qty = min(qty, -self.net)
        if qty <= 0:
            return
        self._stale_net = self.net
        self.net += qty
        self.bought += qty
        o["filled"] += qty
        self._exec += 1
        o["execs"].append(f"x{self._exec}")

    def place(self, link_id: str, qty: float) -> None:
        short = -self.net
        self.placed.append((link_id, qty, short))
        if link_id in self.orders:                       # one order per link id, ever
            self.duplicate_sends_seen += 1
            raise ValueError("DUPLICATE_ORDER_LINK_ID")
        mode = self.modes.pop(0) if self.modes else "full"
        if mode == "drop":                               # the request never arrived
            raise Timeout()
        o = {"qty": qty, "status": "New", "filled": 0.0, "execs": [], "late": False}
        self.orders[link_id] = o
        if qty > short + 1e-12:                          # reduce-only: refused, never reverses
            o["status"] = "Rejected"
            return
        if mode == "reject":
            o["status"] = "Rejected"
            return
        if mode == "late":
            o["late"] = True
            return
        frac = 1.0
        if mode.startswith("partial:"):
            frac = float(mode.split(":")[1])
        self._fill(o, qty * frac)
        o["status"] = "Filled" if frac >= 1.0 else "Cancelled"
        if mode == "lost":
            raise Timeout()                              # executed, response lost

    def tick(self) -> None:
        """Time passes: late orders fill."""
        for o in self.orders.values():
            if o["late"]:
                o["late"] = False
                self._fill(o, o["qty"])
                o["status"] = "Filled"

    # -- invariants ---------------------------------------------------------------------------
    def live_orders(self) -> int:
        return sum(1 for o in self.orders.values() if o["status"] in ("New", "PartiallyFilled"))


def drive(venue: Venue, cycle_id: str = "cyc-1", *, policy: CP.Policy = CP.Policy(),
          restart_rng: Optional[random.Random] = None, restart_p: float = 0.0,
          max_steps: int = 80, book_short: Optional[float] = None):
    """Run the planner against the venue until it is flat or halts. Between any
    two steps the process may 'restart': all memory is dropped and only the
    durable attempt log survives. Returns (log, last_plan, steps)."""
    log: tuple = ()
    last = None
    lag_waits = 0
    for step in range(max_steps):
        if restart_rng is not None and restart_rng.random() < restart_p:
            lag_waits = 0                                 # memory lost; only `log` survives
        obs = venue.observe(log, lag_waits)
        plan = CP.plan_close(cycle_id, log, obs, policy,
                             book_short_qty=venue.initial_short if book_short is None else book_short)
        log = CP.apply_plan(log, plan)                    # persisted BEFORE any send
        last = plan
        lag_waits = lag_waits + 1 if plan.reason == CP.R_LAG else 0
        # invariants that must hold after EVERY step
        assert venue.net <= 1e-9, "the position reversed (net long)"
        assert venue.live_orders() <= 1, "two close orders live at once"
        assert venue.bought <= venue.initial_short + 1e-9, "bought more than the short"
        if plan.action in (CP.NOOP_FLAT, CP.HALT):
            return log, plan, step + 1
        if plan.action in (CP.SEND, CP.RESEND):
            crash = restart_rng is not None and restart_rng.random() < restart_p
            if not crash:                                 # a crash after INTENT, before the send
                try:
                    venue.place(plan.link_id, plan.qty)
                except (Timeout, ValueError):
                    pass
            log = tuple(a if a.n != plan.attempt or a.state != CP.INTENT
                        else CP.Attempt(a.n, a.link_id, a.qty, CP.SENT) for a in log)
        else:
            venue.tick()
    return log, last, max_steps
