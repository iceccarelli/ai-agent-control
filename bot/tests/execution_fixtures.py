"""Execution records for tests that need MANY of them, made by the real producer.

`execution_cost_estimate` no longer believes a record that merely says
"MEASURED": it re-derives the verdict and the figures from the stored book,
timestamps and fill. A fixture therefore has to be a record the producer could
really have written, so this builds one with `execution_record.assess_fill`
from a decision-time book and a fill price, and only then lets a test overwrite
a field to make it invalid.
"""
from __future__ import annotations

import math
import os
import sys
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import execution_record as xr                                # noqa: E402

_UNSET = object()


def _finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) \
        and math.isfinite(x)


def valid_record(side: str = "Sell", product: str = "linear", q: float = 0.01,
                 ref: float = 60000.0, slip_usd: float = 0.12,
                 spr_usd: float = 0.3, order_type: str = "market",
                 symbol: str = "BTCUSDT", req: Optional[float] = None,
                 ms: int = 1_700_000_000_000, link: str = "L",
                 **over: Any) -> Dict[str, Any]:
    """A MEASURED record: `ref` is the executable side of the book, the book's
    half-spread costs `spr_usd` for `q`, the fill is `slip_usd` worse than
    `ref` (negative = better). Anything in `over` overwrites the result."""
    qb = q if _finite(q) and q > 0 else (req if _finite(req) and req > 0 else 0.01)
    reqb = req if _finite(req) and req >= qb else qb
    slip = slip_usd if _finite(slip_usd) else 0.12
    spr = spr_usd if _finite(spr_usd) and spr_usd > 0 else 0.3
    half = spr / qb                       # half-spread per unit
    if side == "Sell":
        bid, ask = ref, ref + 2 * half
        fill = ref - slip / qb
    else:
        bid, ask = ref - 2 * half, ref
        fill = ref + slip / qb
    ctx = xr.DecisionContext(
        venue="bybit", symbol=symbol, product=product, side=side,
        decision_ms=ms - 50, qty=qb, mark=(bid + ask) / 2.0, bid=bid, ask=ask,
        observed_ms=ms - 60, book_source="tick_snapshot")
    r = xr.assess_fill(
        ctx, side=side, symbol=symbol, product=product, venue="bybit",
        order_type=order_type, requested_qty=reqb, filled_qty=qb, avg_price=fill,
        now_ms=ms, fee_usd=0.2, fee_confirmed=True, order_link_id=link,
        submit_ms=ms - 40, fill_ms=ms - 5)
    r = dict(r)
    r["filled_qty"] = q
    r["requested_qty"] = req or q
    if not _finite(slip_usd):
        r["slippage_usd"] = slip_usd
    if not (_finite(spr_usd) and spr_usd > 0):
        r["spread_cost_usd"] = spr_usd
    r.update(over)
    return r
