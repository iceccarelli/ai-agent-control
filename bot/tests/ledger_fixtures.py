"""Fixtures for tests that need booked VENUE funding.

A funding entry labelled `venue` must carry the venue row it was booked from
(`venue_id`, `venue_ms`, `venue_usd`); the ledger refuses the label without it.
`vraw` builds that evidence for a synthetic entry. Legacy rows (written before
provenance existed) are built through `legacy_journal` so they load the way a
persisted one does."""
from __future__ import annotations

from typing import Any, Dict


def vraw(ref: str, ms: int, usd: float, symbol: str = "BTCUSDT",
         size: float = -0.001) -> Dict[str, Any]:
    return {"venue_id": str(ref), "venue_ms": int(ms), "venue_usd": float(usd),
            "currency": "USDT", "size": size, "symbol": symbol}


def legacy_journal(journal, *, drop=("basis",), basis=None):
    """What a journal written before provenance looks like once reloaded: the
    named keys are missing from every persisted row, `basis` is optionally set,
    and `from_rows` accepts it as written."""
    import ledger as L
    rows = journal.to_rows()
    for r in rows:
        for k in drop:
            r.pop(k, None)
        if basis is not None and r.get("kind") == "funding":
            r["basis"] = basis
    return L.Journal.from_rows(rows)
