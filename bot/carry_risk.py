"""The size gate the carry pair does not have. Refuses; never sizes up.

WHY THIS EXISTS
===============
`RiskManager.gate_order` guards every directional order in this tree: notional
cap, per-trade risk budget, leverage ceiling, daily loss limit, kill switch,
quote sanity. The carry pair passes through NONE of it.

That was a defensible choice and it was never written down as one. A hedged pair
has no stop distance, so per-trade risk sizing is meaningless for it, and daily
loss limits assume directional P&L. But the consequence is that TODAY the ONLY
size control on the carry book is `max_notional_usd`. One number. At $100 that
is harmless. At $1m it is the entire risk framework.

This module is the pair-shaped equivalent: the checks that DO mean something for
two hedged legs, and none of the ones that do not.

WHAT IT CHECKS
==============
  KILL SWITCH     tripped means tripped, for every book in the process.
  ONE PAIR        the engine holds one position. A second pair against the same
                  margin is two books sharing one liquidation price, which is
                  the failure `build_bot`'s carry XOR directional already
                  prevents at the strategy level and nothing prevents here.
  NOTIONAL CAP    from shadow.SHADOW_MAX_NOTIONAL_USD, the same constant
                  promotion_gate polices. Not a config key, not a default.
  MARGIN FLOOR    headroom on the short leg BEFORE committing more of it.
  ENTRIES PER DAY the backtest opened 18 pairs in four years. A book trying to
                  open twice in a day is not running this strategy, it is
                  thrashing, and thrashing pays the 31 bps round trip each time.
  VENUE FRESHNESS delegated to MarketSnapshot.assert_fresh.

WHAT IT WILL NOT DO
===================
Return a size. It answers yes or no to a size someone else chose. A gate that
can enlarge an order is not a gate, and the one field this book must never grow
by accident is notional.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: The backtest opened 18 pairs across 1,447 days. One entry per UTC day is
#: already an order of magnitude more permissive than the strategy needs, and it
#: is a hard ceiling on how much the round trip can cost you in a bad loop.
MAX_ENTRIES_PER_UTC_DAY = 1

#: Headroom on the short perp before opening. Mirrors CarryEngine's own floor:
#: the gate must not be more permissive than the engine it guards.
MIN_MARGIN_MULTIPLE = 2.0


@dataclass(frozen=True)
class PairDecision:
    ok: bool
    reason: str
    detail: Dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok


def _block(reason: str, **detail: Any) -> PairDecision:
    return PairDecision(False, reason, detail)


class CarryRisk:
    """Pair-level gate. Consulted before both legs, never after one."""

    def __init__(self, *, store: Any = None,
                 max_notional_usd: Optional[float] = None,
                 min_margin_multiple: float = MIN_MARGIN_MULTIPLE,
                 max_entries_per_day: int = MAX_ENTRIES_PER_UTC_DAY) -> None:
        if max_notional_usd is None:
            # Single authority. Not a config key and not a default: 0016 removed
            # exactly this kind of invented cap from build_bot, and the gate
            # that polices size must read the same constant promotion_gate does.
            import shadow
            max_notional_usd = float(shadow.SHADOW_MAX_NOTIONAL_USD)
        self.store = store
        self.max_notional_usd = float(max_notional_usd)
        self.min_margin_multiple = float(min_margin_multiple)
        self.max_entries_per_day = int(max_entries_per_day)
        self._entries: List[dt.date] = []

    def gate_open(self, *, notional_usd: float, snapshot: Any,
                  has_open_pair: bool, now: Optional[dt.datetime] = None
                  ) -> PairDecision:
        """May the book open a pair of this size, right now?

        Ordered by how much damage the failure does, not by cost to check. A
        tripped kill switch outranks a stale price, and both outrank a cap.
        """
        if self.store is not None:
            try:
                engaged, why = self.store.is_kill_switch_engaged()
            except Exception as exc:  # noqa: BLE001
                # An unreadable switch is an engaged switch. persistence.py has
                # taken this position since slice 12 and the carry book must not
                # be the one place that disagrees.
                return _block("KILL_SWITCH_UNREADABLE", error=str(exc))
            if engaged:
                return _block("KILL_SWITCH_ENGAGED", why=why)

        if has_open_pair:
            return _block(
                "PAIR_ALREADY_OPEN",
                note="a second pair shares one liquidation price with the "
                     "first and neither engine knows about the other's legs")

        try:
            snapshot.assert_fresh()
        except Exception as exc:  # noqa: BLE001
            return _block("MARKET_VIEW_STALE", error=str(exc))

        margin = float(getattr(snapshot, "margin_multiple", 0.0))
        if margin < self.min_margin_multiple:
            return _block("MARGIN_HEADROOM_TOO_THIN",
                          margin_multiple=margin,
                          floor=self.min_margin_multiple)

        if not (isinstance(notional_usd, (int, float))
                and math.isfinite(notional_usd) and notional_usd > 0):
            return _block("NOTIONAL_INVALID", notional_usd=notional_usd)

        if notional_usd > self.max_notional_usd:
            return _block("NOTIONAL_ABOVE_CAP", requested=float(notional_usd),
                          cap=self.max_notional_usd)

        today = (now or dt.datetime.now(dt.timezone.utc)).date()
        used = sum(1 for d in self._entries if d == today)
        if used >= self.max_entries_per_day:
            return _block("ENTRY_LIMIT_REACHED_TODAY", used=used,
                          limit=self.max_entries_per_day,
                          note="the backtest opened 18 pairs in four years; a "
                               "book opening twice in a day is thrashing, and "
                               "each round trip costs 31 bps")

        return PairDecision(True, "PAIR_APPROVED",
                            {"notional_usd": float(notional_usd),
                             "cap": self.max_notional_usd,
                             "margin_multiple": margin,
                             "basis_bps": getattr(snapshot, "basis_bps", None)})

    def record_entry(self, *, now: Optional[dt.datetime] = None) -> None:
        """Called only after BOTH legs land. A refused or half-filled pair did
        not consume the day's entry."""
        today = (now or dt.datetime.now(dt.timezone.utc)).date()
        self._entries.append(today)
        del self._entries[:-32]
