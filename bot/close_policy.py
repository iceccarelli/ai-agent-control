"""The close policy: every number and choice that is the OWNER's, stated as such.

The multi-step close (`close_executor`) changes what the book does with a short
it must get out of. How many attempts, how long to wait, whether an id may be
re-sent, which order type, what happens when patience runs out: none of these
has a value this repository can derive, and none is given a default here. Each
is read from configuration; an absent one is UNSET, an unusable one INVALID, and
either keeps the book on its original close (one reduce-only Buy, halt on a
partial fill), which is the behaviour already in production.

The path is on only when ALL of these hold: every key below is SET and valid,
AND `CARRY_CLOSE_PATH_APPROVED` is exactly `APPROVED_BY_OWNER`. The approval is a
separate, deliberately awkward string so that filling in the numbers can never,
by itself, change how the book closes.

Some choices exist only to be refused. The only order type the broker can place
for a close is a reduce-only market order, so that is the only value accepted;
a slippage ceiling is NOT accepted at all, because a market order carries no
price cap this code can verify (Bybit documents a slippage tolerance for market
orders but not what happens when it is exceeded, and nothing here has measured
it). Accepting a number and not enforcing it would be a control that is not one.

The bounds below are sanity ceilings that stop a typo from turning a bounded
loop into an unbounded one. They are not recommendations and are not defaults.

Pure: no I/O.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

SET, UNSET, INVALID = "SET", "UNSET", "INVALID"

APPROVAL_VALUE = "APPROVED_BY_OWNER"

#: Sanity ceilings on what an owner may type. They cap, never suggest.
CEILING_ATTEMPTS = 10
CEILING_WAITS = 240
CEILING_LAG_WAITS = 60
CEILING_SENDS = 5

RESEND_NEVER = "NEVER"
RESEND_SAME_LINK_ID = "SAME_LINK_ID"
ORDER_MARKET_REDUCE_ONLY = "MARKET_REDUCE_ONLY"
TIMEOUT_HALT = "HALT"
RESIDUAL_HALT_AND_RECORD = "HALT_AND_RECORD"
ESCALATION_HALT = "HALT_AND_LOG_CRITICAL"

K_ATTEMPTS = "CARRY_CLOSE_MAX_ATTEMPTS"
K_WAITS = "CARRY_CLOSE_MAX_WAITS"
K_LAG_WAITS = "CARRY_CLOSE_MAX_LAG_WAITS"
K_RESEND = "CARRY_CLOSE_RESEND"
K_SENDS = "CARRY_CLOSE_MAX_SENDS"
K_ORDER_TYPE = "CARRY_CLOSE_ORDER_TYPE"
K_TIMEOUT = "CARRY_CLOSE_ON_TIMEOUT"
K_RESIDUAL = "CARRY_CLOSE_RESIDUAL"
K_ESCALATION = "CARRY_CLOSE_ESCALATION"
K_SLIPPAGE = "CARRY_CLOSE_SLIPPAGE_CEILING_BPS"
K_APPROVED = "CARRY_CLOSE_PATH_APPROVED"

ALL_KEYS: Tuple[str, ...] = (
    K_ATTEMPTS, K_WAITS, K_LAG_WAITS, K_RESEND, K_SENDS, K_ORDER_TYPE,
    K_TIMEOUT, K_RESIDUAL, K_ESCALATION, K_SLIPPAGE, K_APPROVED)

#: What each decision is, for the status page and the owner. Not a default.
DECISIONS: Dict[str, str] = {
    K_ATTEMPTS: "most close orders (attempts) before a human is called",
    K_WAITS: "most consecutive polls of a close that is live or unconfirmed "
             "before a human is called",
    K_LAG_WAITS: "most polls of a position read that has not caught up with a "
                 "fill before the next reduce-only order is proposed anyway",
    K_RESEND: "NEVER (a new attempt only after the old one is proven absent) or "
              "SAME_LINK_ID (re-send the same id; relies on a venue behaviour "
              "that must first be measured on testnet)",
    K_SENDS: "with SAME_LINK_ID only: most sends of one id",
    K_ORDER_TYPE: "the order a close is made of; only MARKET_REDUCE_ONLY exists",
    K_TIMEOUT: "what happens when patience runs out; only HALT exists",
    K_RESIDUAL: "what is done with a short that remains; only HALT_AND_RECORD "
                "exists (it is never abandoned or booked as closed)",
    K_ESCALATION: "how a halt is raised; only HALT_AND_LOG_CRITICAL exists",
    K_SLIPPAGE: "NOT SUPPORTED: a market close has no verified price cap, so a "
                "ceiling cannot be enforced; leave unset",
    K_APPROVED: f"must be exactly {APPROVAL_VALUE} for the path to turn on",
}


@dataclass(frozen=True)
class ClosePolicy:
    max_attempts: int
    max_waits: int
    max_lag_waits: int
    resend: str
    max_sends: int
    order_type: str
    on_timeout: str
    residual: str
    escalation: str

    @property
    def resend_same_link_id(self) -> bool:
        return self.resend == RESEND_SAME_LINK_ID


@dataclass(frozen=True)
class CloseVerdict:
    state: str                          # SET / UNSET / INVALID (the values)
    policy: Optional[ClosePolicy]
    approved: bool
    rows: Tuple[Dict[str, Any], ...]    # one per key: key / state / value / reason

    @property
    def enabled(self) -> bool:
        """True only when every value is SET and valid AND the owner approved."""
        return self.state == SET and self.policy is not None and self.approved

    def blockers(self) -> List[str]:
        out = [f"{r['key']}: {r['state']} - {r['reason']}"
               for r in self.rows if r["state"] != SET and r["required"]]
        if self.state == SET and not self.approved:
            out.append(f"{K_APPROVED}: UNSET - the values are set but the owner "
                       "has not approved the path")
        return out


def _raw(cfg: Any, key: str) -> Optional[str]:
    v = getattr(cfg, key, None)
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    text = str(v).strip()
    return text or None


def _int(text: str, low: int, high: int) -> Tuple[Optional[int], str]:
    try:
        f = float(text)
    except (TypeError, ValueError):
        return None, "not a number"
    if not math.isfinite(f) or f != int(f):
        return None, "not a whole number"
    n = int(f)
    if n < low or n > high:
        return None, f"outside {low}..{high} (a sanity ceiling, not a recommendation)"
    return n, ""


def load_close_policy(cfg: Any) -> CloseVerdict:
    """Read the close policy from `cfg`. Never raises; never defaults a value."""
    rows: List[Dict[str, Any]] = []
    values: Dict[str, Any] = {}

    def row(key: str, state: str, value: Any, reason: str, required: bool = True) -> None:
        rows.append({"key": key, "state": state, "value": value,
                     "reason": reason, "required": required,
                     "decision": DECISIONS[key]})

    def need_int(key: str, low: int, high: int, required: bool = True) -> None:
        raw = _raw(cfg, key)
        if raw is None:
            row(key, UNSET, None, "the owner has not set it", required)
            return
        n, why = _int(raw, low, high)
        if n is None:
            row(key, INVALID, raw, why, required)
            return
        values[key] = n
        row(key, SET, n, "", required)

    def need_choice(key: str, allowed: Tuple[str, ...]) -> None:
        raw = _raw(cfg, key)
        if raw is None:
            row(key, UNSET, None, "the owner has not set it")
            return
        if raw not in allowed:
            row(key, INVALID, raw, f"must be one of {', '.join(allowed)}")
            return
        values[key] = raw
        row(key, SET, raw, "")

    need_int(K_ATTEMPTS, 1, CEILING_ATTEMPTS)
    need_int(K_WAITS, 1, CEILING_WAITS)
    need_int(K_LAG_WAITS, 0, CEILING_LAG_WAITS)
    need_choice(K_RESEND, (RESEND_NEVER, RESEND_SAME_LINK_ID))
    if values.get(K_RESEND) == RESEND_SAME_LINK_ID:
        need_int(K_SENDS, 1, CEILING_SENDS)
    else:
        raw = _raw(cfg, K_SENDS)
        row(K_SENDS, SET, raw, "not used: re-sending an id is off",
            required=False)
        values[K_SENDS] = 1
    need_choice(K_ORDER_TYPE, (ORDER_MARKET_REDUCE_ONLY,))
    need_choice(K_TIMEOUT, (TIMEOUT_HALT,))
    need_choice(K_RESIDUAL, (RESIDUAL_HALT_AND_RECORD,))
    need_choice(K_ESCALATION, (ESCALATION_HALT,))

    slip = _raw(cfg, K_SLIPPAGE)
    if slip is None:
        row(K_SLIPPAGE, SET, None, "unset, as it must be", required=False)
    else:
        row(K_SLIPPAGE, INVALID, slip,
            "a slippage ceiling cannot be enforced on a market close; refusing "
            "to accept a control that is not one", required=True)

    required_rows = [r for r in rows if r["required"]]
    if any(r["state"] == INVALID for r in required_rows):
        state = INVALID
    elif any(r["state"] == UNSET for r in required_rows):
        state = UNSET
    else:
        state = SET

    approved = _raw(cfg, K_APPROVED) == APPROVAL_VALUE
    arow_state = SET if approved else UNSET
    row(K_APPROVED, arow_state, APPROVAL_VALUE if approved else None,
        "" if approved else "the owner has not approved the path", required=False)

    policy: Optional[ClosePolicy] = None
    if state == SET:
        policy = ClosePolicy(
            max_attempts=values[K_ATTEMPTS], max_waits=values[K_WAITS],
            max_lag_waits=values[K_LAG_WAITS], resend=values[K_RESEND],
            max_sends=values[K_SENDS], order_type=values[K_ORDER_TYPE],
            on_timeout=values[K_TIMEOUT], residual=values[K_RESIDUAL],
            escalation=values[K_ESCALATION])
    return CloseVerdict(state=state, policy=policy, approved=approved,
                        rows=tuple(rows))


def planner_policy(policy: ClosePolicy, *, qty_step: float, min_qty: float):
    """The `close_plan.Policy` for an approved `ClosePolicy` and the venue's lot
    rules (read from the venue by the caller, never assumed)."""
    import close_plan as CP
    for name, v in (("qty_step", qty_step), ("min_qty", min_qty)):
        if (isinstance(v, bool) or not isinstance(v, (int, float))
                or not math.isfinite(v) or v <= 0):
            raise ValueError(f"{name} must be a positive finite number, got {v!r}")
    return CP.Policy(
        max_attempts=policy.max_attempts, qty_step=float(qty_step),
        min_qty=float(min_qty), max_lag_waits=policy.max_lag_waits,
        max_waits=policy.max_waits, max_sends=policy.max_sends,
        resend_same_link_id=policy.resend_same_link_id)


def status_lines(verdict: CloseVerdict) -> List[str]:
    """One line per decision, for the log and the status page."""
    head = ("CLOSE PATH ON" if verdict.enabled
            else "CLOSE PATH OFF (the original single-order close is in use)")
    lines = [head]
    for r in verdict.rows:
        if r["state"] == SET and not r["required"] and r["value"] is None:
            continue
        lines.append(f"  {r['key']}: {r['state']}"
                     + (f" = {r['value']}" if r["value"] is not None else "")
                     + (f" - {r['reason']}" if r["reason"] else ""))
    return lines
