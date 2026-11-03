"""What the venue states about whether the held BTC actually backs the short.

The overlay opens a short perp against BTC the owner already holds. Whether that
BTC counts as margin collateral is a venue-side fact, and so is the ratio it
counts at. The engine's liquidation checks read the venue's own derived numbers
(`liqPrice`, `accountMMRate`), which already include both, but nothing used to
check that the BTC is collateral at all, and the comment above
`MIN_LIQUIDATION_DISTANCE_PCT` said the gate "should never bind" on the strength
of an assumption nobody verified.

What the Bybit v5 wallet-balance coin row states (docs, and a tracked, redacted
testnet capture in `artifacts/testnet_conformance_evidence.jsonl`):

  marginCollateral  bool  the PLATFORM allows this coin as margin collateral
  collateralSwitch  bool  the USER turned it on; meaningful only when
                          marginCollateral is true
  colRes            str   platform collateral-restriction status ("-1" on a
                          healthy account); its meanings are not documented
                          well enough to gate on

What it does NOT state: a collateral ratio. The old `collateralRatio` field is
documented as no longer accurate since 2025-02-19, and the current tiered ratio
lives behind a different endpoint and depends on size. So the ratio is
UNAVAILABLE here, said so, and never filled with a number.

Pure functions: no I/O, no client, no clock.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

OK = "OK"
INSUFFICIENT_RISK_DATA = "INSUFFICIENT_RISK_DATA"
NOT_COLLATERAL = "NOT_COLLATERAL"

#: Why a coin does not back the position, or why we cannot say.
COLLATERAL_FIELD_ABSENT = "COLLATERAL_FIELD_ABSENT"
COLLATERAL_FIELD_UNREADABLE = "COLLATERAL_FIELD_UNREADABLE"
PLATFORM_DISALLOWS_COLLATERAL = "PLATFORM_DISALLOWS_COLLATERAL"
COLLATERAL_SWITCH_OFF = "COLLATERAL_SWITCH_OFF"
COIN_ROW_MISSING = "COIN_ROW_MISSING"

RATIO_UNAVAILABLE = ("UNAVAILABLE: the wallet row carries no collateral ratio; "
                     "the deprecated collateralRatio is not accurate since "
                     "2025-02-19 and the tiered ratio is a separate endpoint "
                     "that depends on size")


def _flag(row: Mapping[str, Any], name: str) -> Optional[bool]:
    """A venue boolean, or None when it is absent or not a boolean. Accepts the
    JSON booleans the venue sends and the strings 'true'/'false' (a proxy or a
    fixture may stringify); anything else, including 1/0, is unreadable."""
    if name not in row:
        return None
    v = row[name]
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    return None


def collateral_status(row: Optional[Mapping[str, Any]], coin: str) -> Dict[str, Any]:
    """Does this coin back a derivatives position, on the venue's own word?

    Returns a dict with `status` OK | NOT_COLLATERAL | INSUFFICIENT_RISK_DATA and
    a `reason`. OK only when the platform allows the coin AND the user switched it
    on. A missing, null or malformed field is INSUFFICIENT_RISK_DATA: the risk
    engine does not assume a fact the venue did not state."""
    base: Dict[str, Any] = {
        "coin": coin, "margin_collateral": None, "collateral_switch": None,
        "col_res": None, "collateral_ratio": None,
        "collateral_ratio_status": RATIO_UNAVAILABLE}
    if not isinstance(row, Mapping):
        return dict(base, status=INSUFFICIENT_RISK_DATA, reason=COIN_ROW_MISSING)
    base["col_res"] = row.get("colRes")
    platform = _flag(row, "marginCollateral")
    switch = _flag(row, "collateralSwitch")
    base["margin_collateral"], base["collateral_switch"] = platform, switch
    if platform is None:
        absent = "marginCollateral" not in row
        return dict(base, status=INSUFFICIENT_RISK_DATA,
                    reason=COLLATERAL_FIELD_ABSENT if absent
                    else COLLATERAL_FIELD_UNREADABLE, field="marginCollateral")
    if platform is False:
        return dict(base, status=NOT_COLLATERAL, reason=PLATFORM_DISALLOWS_COLLATERAL)
    if switch is None:
        absent = "collateralSwitch" not in row
        return dict(base, status=INSUFFICIENT_RISK_DATA,
                    reason=COLLATERAL_FIELD_ABSENT if absent
                    else COLLATERAL_FIELD_UNREADABLE, field="collateralSwitch")
    if switch is False:
        return dict(base, status=NOT_COLLATERAL, reason=COLLATERAL_SWITCH_OFF)
    return dict(base, status=OK, reason="")


def find_coin_row(wallet_rows: Any, coin: str) -> Optional[Mapping[str, Any]]:
    """The named coin's row from a `/v5/account/wallet-balance` `result.list`
    (an account list whose first element nests `coin: [...]`), or None."""
    try:
        coins = (wallet_rows[0].get("coin") or []) if wallet_rows else []
    except (AttributeError, IndexError, KeyError, TypeError):
        return None
    for entry in coins:
        if isinstance(entry, Mapping) and str(entry.get("coin", "")).upper() == coin.upper():
            return entry
    return None
