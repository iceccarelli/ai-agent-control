"""The carry book's seat at the portfolio table.

`portfolio_state` (read-only, one state with a source per field) and
`capital_reservations` (atomic, persisted) are strategy-neutral. This adapter
binds them to the carry book's broker, store and symbols, and gives the engine
the three things it needs:

  entry_blockers()    why NEW exposure is refused right now (empty = clear). A
                      fresh venue read and a fresh state build on every call;
                      nothing is cached, because a stale "clear" is the failure.
  reserve_entry(...)  hold the dollars an entry will use, atomically against
                      every other strategy, BEFORE its first leg.
  sync(...)           once per tick: expire stale holds, commit the entry's hold
                      once its orders exist, and give the dollars back when the
                      book is flat with nothing in flight.

Reducing risk never asks this class: `reduction_allowed()` is unconditional and
the engine's exit paths do not call the authority at all.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, List, Tuple

import capital_reservations as cr
import portfolio_state as ps

logger = logging.getLogger(__name__)

STRATEGY_ID = "carry"


class CarryCapitalAuthority:
    def __init__(self, *, store: Any, broker: Any, spot_symbol: str,
                 perp_symbol: str, reservations: Any,
                 limits: "ps.PortfolioLimits", account_scope: str,
                 ttl_s: float, quote_coin: str = "USDT",
                 clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.broker = broker
        self.spot_symbol = spot_symbol
        self.perp_symbol = perp_symbol
        self.reservations = reservations
        self.limits = limits
        self.account_scope = account_scope
        self.ttl_s = float(ttl_s)
        self.quote_coin = quote_coin
        self.clock = clock

    # -- reads -----------------------------------------------------------------

    def _state(self) -> "ps.PortfolioState":
        """A fresh venue read and a fresh state. Never cached."""
        venue = ps.read_venue(self.broker, spot_symbol=self.spot_symbol,
                              perp_symbol=self.perp_symbol,
                              quote_coin=self.quote_coin, clock=self.clock)
        return ps.build_portfolio_state(
            self.store, limits=self.limits, venue=venue,
            reservations=self.reservations, account_scope=self.account_scope,
            now_epoch=self.clock())

    def entry_blockers(self) -> List[str]:
        return self._state().entry_blockers()

    def entries_permitted(self) -> Tuple[bool, str]:
        return self._state().entries_permitted()

    def reduction_allowed(self, purpose: str = "exit") -> bool:
        """Protective exits and emergency risk reduction: always. Unconditional
        and read-free on purpose."""
        return True

    # -- capital -----------------------------------------------------------------

    def reserve_entry(self, cycle_id: str, amount_usd: float) -> Tuple[bool, str]:
        """Hold `amount_usd` for this cycle against the venue's CURRENT
        available balance. (ok, code)."""
        state = self._state()
        result = self.reservations.reserve(
            STRATEGY_ID, str(cycle_id), float(amount_usd), self.account_scope,
            "entry", available=state.available_capital, ttl_s=self.ttl_s,
            now=self.clock())
        return bool(result.ok), str(result.code)

    def sync(self, *, position_open: bool) -> None:
        """Called once per tick. Never raises into the engine's decision.

        Order matters. FIRST every hold that has an order behind it is bound to
        that order (committed), whether or not a pair is open: a first leg that
        timed out leaves an unresolved order and a flat book, and its hold must
        not expire on a timer and free dollars that order may be using. ONLY THEN
        are holds that never got an order expired, and a flat book with nothing
        in flight gives its dollars back. (Expiring first lost the hold of a
        pair whose tick came later than the TTL.)"""
        now = self.clock()
        for row in self._carry_live():
            if row.get("state") == "RESERVED":
                self._commit(row)
        self.reservations.expire_stale(now=now, account_scope=self.account_scope)
        live = self._carry_live()
        if not live or position_open:
            return
        # Flat. Give the dollars back only if nothing of ours is still in
        # flight: an unresolved order may be using them.
        if self.store.unresolved_carry_orders():
            return
        for row in live:
            res = self.reservations.release(
                row["intent_id"], "book flat with no order in flight", now=now)
            if not res.ok:
                logger.warning("capital: reservation %s kept (%s)",
                               row["intent_id"], res.code)

    def _carry_live(self) -> list:
        return [r for r in self.reservations.live(self.account_scope)
                if r.get("strategy_id") == STRATEGY_ID]

    def _commit(self, row: dict) -> None:
        """A held pair's hold must not expire on a timer: bind it to the cycle's
        first order so it stays held until released."""
        cycle = str(row["intent_id"])
        orders = [o for o in self.store.orders_for_symbol(self.perp_symbol)
                  if str(o.get("cycle_id")) == cycle]
        if not orders:
            orders = [o for o in self.store.orders_for_symbol(self.spot_symbol)
                      if str(o.get("cycle_id")) == cycle]
        if not orders:
            logger.warning("capital: no order recorded for %s; its hold is not "
                           "committed", cycle)
            return
        res = self.reservations.commit(
            cycle, str(orders[0]["order_link_id"]), now=self.clock())
        if not res.ok:
            logger.warning("capital: could not commit %s (%s)", cycle, res.code)
