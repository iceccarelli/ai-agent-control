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

import collections
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
        #: Pairs where ONE leg landed and was unwound (0033, INVENTORY D4). Not
        #: entries — record_entry stays "both legs landed" — but they paid a
        #: round trip, and a venue that keeps rejecting the perp would
        #: otherwise make the book buy and sell spot every tick.
        self._broken: List[dt.date] = []

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

        # `None` while flat - a ratio on an OPEN position (INVENTORY F4) is
        # genuinely undefined before the first fill, not a floor this gate
        # can check yet. `CarryEngine`'s own pre-open margin check already
        # tolerates this exact case (`margin is not None and margin < floor`);
        # this gate must agree, not re-block (or, before this fix, crash on
        # `float(None)`) the one open that would establish the ratio.
        raw_margin = getattr(snapshot, "margin_multiple", None)
        margin = None if raw_margin is None else float(raw_margin)
        if margin is not None and margin < self.min_margin_multiple:
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
        landed = sum(1 for d in self._entries if d == today)
        broken = sum(1 for d in self._broken if d == today)
        used = landed + broken
        if used >= self.max_entries_per_day:
            return _block("ENTRY_LIMIT_REACHED_TODAY", used=used,
                          landed_pairs_today=landed,
                          broken_pairs_today=broken,
                          limit=self.max_entries_per_day,
                          note="the backtest opened 18 pairs in four years; a "
                               "book opening twice in a day is thrashing, and "
                               "each round trip costs 31 bps")

        return PairDecision(True, "PAIR_APPROVED",
                            {"notional_usd": float(notional_usd),
                             "cap": self.max_notional_usd,
                             "margin_multiple": margin,
                             "basis_bps": getattr(snapshot, "basis_bps", None)})

    def export_days(self) -> Dict[str, List[str]]:
        """The allowance as plain data, so it survives the process."""
        return {"entries": [d.isoformat() for d in self._entries],
                "broken": [d.isoformat() for d in self._broken]}

    @staticmethod
    def _parse_days(data: Any) -> Optional[Dict[str, List[dt.date]]]:
        """A persisted allowance, or None when ANY part of it is unusable.

        Both buckets must be present as lists of ISO date strings. A wrong
        type, a missing bucket or one bad date makes the whole record
        untrustworthy: the unreadable item may have been today's spent entry,
        so it cannot be skipped and the rest believed.
        """
        if not isinstance(data, dict):
            return None
        out: Dict[str, List[dt.date]] = {}
        for key in ("entries", "broken"):
            rows = data.get(key)
            if not isinstance(rows, list):
                return None
            days: List[dt.date] = []
            for raw in rows:
                if not isinstance(raw, str):
                    return None
                try:
                    days.append(dt.date.fromisoformat(raw))
                except ValueError:
                    return None
            out[key] = days
        return out

    def load_days(self, data: Any) -> bool:
        """Take back a persisted allowance. True when it was valid and loaded.

        Loading is idempotent: each date's count becomes the larger of what is
        held and what was persisted, so loading twice cannot double-count. A
        False return means the record could not be trusted and the caller must
        `exhaust_today`; nothing is loaded from a partly-readable record.
        """
        parsed = self._parse_days(data)
        if parsed is None:
            return False
        for key, bucket in (("entries", self._entries),
                            ("broken", self._broken)):
            have = collections.Counter(bucket)
            want = collections.Counter(parsed[key])
            merged = sorted((have | want).elements())
            bucket[:] = merged[-32:]
        return True

    def exhaust_today(self, *, now: Optional[dt.datetime] = None) -> int:
        """Spend whatever is left of today's allowance. Returns how many
        entries were added (0 when it was already spent).

        The fail-closed answer to an allowance that cannot be verified: no
        entry today, and the same ledger format records it so a restart does
        not forget it. Adds only the shortfall, so repeating it never
        inflates the count.
        """
        today = (now or dt.datetime.now(dt.timezone.utc)).date()
        used = (sum(1 for d in self._entries if d == today)
                + sum(1 for d in self._broken if d == today))
        missing = max(0, self.max_entries_per_day - used)
        self._entries.extend([today] * missing)
        self._entries.sort()
        del self._entries[:-32]
        return missing

    def record_entry(self, *, now: Optional[dt.datetime] = None) -> None:
        """Called only after BOTH legs land. A REFUSED pair (no leg sent) did
        not consume the day's entry. A half-filled one is recorded separately,
        by record_broken_pair."""
        today = (now or dt.datetime.now(dt.timezone.utc)).date()
        self._entries.append(today)
        del self._entries[:-32]

    def record_broken_pair(self, *, now: Optional[dt.datetime] = None) -> None:
        """One leg landed alone and was unwound. It is not an entry; it did
        cost a round trip, so it spends the day's allowance exactly as an
        entry would. No new threshold: the same MAX_ENTRIES_PER_UTC_DAY."""
        today = (now or dt.datetime.now(dt.timezone.utc)).date()
        self._broken.append(today)
        del self._broken[:-32]
