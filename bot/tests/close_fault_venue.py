"""A deterministic fake venue that fails the ways a real one does.

It models only what a close needs: a signed perp position, reduce-only Buy
orders keyed by order link id (one order per id, ever), partial fills, a lost
response, a late fill, a lagging position read and a lagging order lookup.
Nothing here touches a network."""
from __future__ import annotations

import random
from typing import Dict, List, Optional, Sequence

import close_plan as CP

#: The OPT-IN behaviour (re-send an unseen attempt under the SAME link id). The
#: planner's default policy no longer does this, because whether a venue rejects
#: a repeated orderLinkId is unverified here; the older fault-injection tests
#: exercise this behaviour on purpose and so pass it explicitly. The default
#: (absence-proof) behaviour is exercised by test_close_plan_absence_proof.py.
LEGACY_RESEND = CP.Policy(resend_same_link_id=True)


class Timeout(Exception):
    """The response never arrived. The order MAY have executed."""


class Venue:
    def __init__(self, short: float, *, modes: Sequence[str] = (),
                 position_lag_reads: int = 0, order_lag_reads: int = 0,
                 unreadable_reads: int = 0, trusting: bool = False,
                 dup_accepts_after_final: bool = False, never_shows_orders: bool = False,
                 status_alias: Optional[dict] = None, order_unreadable_reads: int = 0,
                 absence_after_reads: int = 2):
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
        #: a venue that does NOT enforce reduce-only (an oversize Buy would reverse)
        self.trusting = trusting
        #: a venue that, contrary to the assumption, re-accepts a link id once the
        #: first order with that id is final
        self.dup_accepts_after_final = dup_accepts_after_final
        self.never_shows_orders = never_shows_orders
        self.status_alias = dict(status_alias or {})    # real status -> reported status
        self.order_unreadable_reads = order_unreadable_reads
        #: Models a SOUND absence proof: an attempt whose order truly does not
        #: exist at the venue is reported proven-absent after this many
        #: consecutive empty lookups. An order that exists but is hidden (lag,
        #: `never_shows_orders`) is NEVER reported absent: the proof's
        #: precondition (the lookup lag is shorter than the acceptance window)
        #: is the caller's to establish, and the executor tests cover it.
        self.absence_after_reads = absence_after_reads
        self._absent_reads: Dict[str, int] = {}
        self.sends_by_link: Dict[str, int] = {}
        self.min_net = self.net                          # the most negative..: tracks reversal
        self.max_net_seen = self.net

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
        if o is None or self.never_shows_orders:
            return None
        if self.order_lag_reads > 0:
            self.order_lag_reads -= 1
            return None
        status = self.status_alias.get(o["status"], o["status"])
        return CP.OrderObs(status, o["filled"], tuple(o["execs"]))

    def observe(self, attempts: Sequence[CP.Attempt], lag_waits: int = 0, waits: int = 0
                ) -> CP.Observation:
        orders, failed, proven = {}, [], []
        for a in attempts:
            if self.order_unreadable_reads > 0:
                self.order_unreadable_reads -= 1
                failed.append(a.link_id)
                continue
            ob = self.read_order(a.link_id)
            if ob is not None:
                orders[a.link_id] = ob
                self._absent_reads[a.link_id] = 0
            else:
                n = self._absent_reads.get(a.link_id, 0) + 1
                self._absent_reads[a.link_id] = n
                if a.link_id not in self.orders and n >= self.absence_after_reads:
                    proven.append(a.link_id)
        return CP.Observation(net_qty=self.read_position(), orders=orders,
                              order_read_failed=tuple(failed), lag_waits=lag_waits, waits=waits,
                              absence_proven=tuple(proven))

    # -- writes ---------------------------------------------------------------------------
    def _fill(self, o: dict, qty: float) -> None:
        if not self.trusting:
            qty = min(qty, -self.net)
        if qty <= 0:
            return
        self._stale_net = self.net
        self.net += qty
        self.bought += qty
        o["filled"] += qty
        self._exec += 1
        o["execs"].append(f"x{self._exec}")
        self.max_net_seen = max(self.max_net_seen, self.net)

    def place(self, link_id: str, qty: float) -> None:
        short = -self.net
        if not CP.valid_link_id(link_id):                # the venue refuses an illegal id
            raise ValueError(f"INVALID_ORDER_LINK_ID {link_id!r}")
        self.placed.append((link_id, qty, short))
        self.sends_by_link[link_id] = self.sends_by_link.get(link_id, 0) + 1
        if link_id in self.orders:                       # one order per link id, ever
            first = self.orders[link_id]
            if not (self.dup_accepts_after_final and first["status"] in ("Filled", "Cancelled", "Rejected")):
                self.duplicate_sends_seen += 1
                raise ValueError("DUPLICATE_ORDER_LINK_ID")
            del self.orders[link_id]                     # the (assumed-away) venue lets it through
        mode = self.modes.pop(0) if self.modes else "full"
        if mode == "drop":                               # the request never arrived
            raise Timeout()
        o = {"qty": qty, "status": "New", "filled": 0.0, "execs": [], "late": False}
        self.orders[link_id] = o
        if qty > short + 1e-12 and not self.trusting:    # reduce-only: refused, never reverses
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


def drive(venue: Venue, cycle_id: str = "cyc-1", *, policy: CP.Policy = LEGACY_RESEND,
          restart_rng: Optional[random.Random] = None, restart_p: float = 0.0,
          max_steps: int = 120, book_short: Optional[float] = None,
          durable_counters: bool = False):
    """Run the planner against the venue until it is flat or halts. Between any
    two steps the process may 'restart': memory (the wait counters) is dropped and
    only the durable attempt log survives, unless `durable_counters` (the caller
    persists them, which is what a real one must do for the wait bound to hold
    across restarts; losing them only DELAYS the halt, it never sends sooner).
    Returns (log, last_plan, steps).

    The invariants asserted here are PLANNER invariants, checked on what the
    planner PROPOSES against what it was shown, not on the fake's own clamping:
      * a proposal is reduce-only and never larger than the short it observed
      * no link id is sent more than policy.max_sends times
      * the run terminates (flat, or a named HALT) within a bound
    and, on a venue that does not enforce reduce-only (`trusting`), that the
    position really never reversed."""
    log: tuple = ()
    last = None
    waits = lag_waits = 0
    for step in range(max_steps):
        if restart_rng is not None and restart_rng.random() < restart_p:
            if not durable_counters:                      # memory lost; only `log` survives
                waits = lag_waits = 0
        obs = venue.observe(log, lag_waits, waits)
        seen_short = None if obs.net_qty is None else max(0.0, -obs.net_qty)
        plan = CP.plan_close(cycle_id, log, obs, policy)
        log = CP.apply_plan(log, plan)                    # persisted BEFORE any send
        last = plan
        waits = waits + 1 if plan.action == CP.WAIT else 0
        lag_waits = lag_waits + 1 if plan.reason == CP.R_LAG else 0
        if plan.action in (CP.SEND, CP.RESEND):
            assert plan.reduce_only, "a close proposal must be reduce-only"
            assert seen_short is not None and plan.qty <= seen_short + 1e-9, \
                f"proposed {plan.qty} against an observed short of {seen_short}"
            assert CP.valid_link_id(plan.link_id)
        if venue.trusting:
            assert venue.max_net_seen <= 1e-9, "the position reversed on a venue that would let it"
        if plan.action in (CP.NOOP_FLAT, CP.HALT):
            return log, plan, step + 1
        if plan.action in (CP.SEND, CP.RESEND):
            crash = restart_rng is not None and restart_rng.random() < restart_p
            if not crash:                                 # a crash after INTENT, before the send
                try:
                    venue.place(plan.link_id, plan.qty)
                except (Timeout, ValueError):
                    pass
            assert venue.sends_by_link.get(plan.link_id, 0) <= policy.max_sends, "a link id was sent too often"
            log = tuple(a if a.n != plan.attempt or a.state != CP.INTENT
                        else CP.Attempt(a.n, a.link_id, a.qty, CP.SENT, a.filled_qty, a.exec_ids,
                                        a.sends, a.short_before) for a in log)
        else:
            venue.tick()
        assert venue.live_orders() <= 1, "two close orders live at once"
    return log, last, max_steps
