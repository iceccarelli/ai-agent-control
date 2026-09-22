"""Single source of truth for "closed forward bars" across Stage B readers.

The forward scorer (`slice76_forward_shadow.py`, same schema as
`artifacts/forward_shadow_current.json`) writes the closed-bar count nested,
under `ceiling.closed_forward_bars` and `forward_window.of_which_closed` —
never at the top level. `promote_forward_shadow.py` copies that scorer
payload through verbatim, so a freshly promoted shadow file carries no
top-level `closed_forward_bars` key at all. Older shadow files (and test
fixtures) predate the nested schema and only have the top-level key.

Every reader of "how many forward bars have closed" — the promotion
before/after summary, `stage_b_forward_accrual.sh`'s HUMAN_PROMOTE_HINT
compare, and `control_plane_tick.py`'s tick JSON / NEXT_MISSION line — must
resolve through `resolve_closed_forward_bars` instead of reading
`closed_forward_bars` directly, or a bare promote silently reports 0/180
even though the ceiling says otherwise.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def resolve_closed_forward_bars(doc: Optional[Dict[str, Any]]) -> int:
    """Resolve the closed-forward-bars count from a shadow/scratch document.

    Preference order: `ceiling.closed_forward_bars`, then
    `forward_window.of_which_closed`, then the legacy top-level
    `closed_forward_bars`. Missing or non-numeric values fall through to the
    next source; an empty/absent document resolves to 0.
    """
    if not doc:
        return 0

    ceiling = doc.get("ceiling") or {}
    value = ceiling.get("closed_forward_bars")
    if value is not None:
        return int(value)

    window = doc.get("forward_window") or {}
    value = window.get("of_which_closed")
    if value is not None:
        return int(value)

    return int(doc.get("closed_forward_bars") or 0)
