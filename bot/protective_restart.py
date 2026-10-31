"""Protect-only restart: an existing position must never become unmanaged
because normal startup cannot safely resume.

Today `main.start()` simply refuses to run when the database's recorded
environment conflicts with the process, or when order recovery leaves orders
unresolved. Refusing is right for ENTRIES and wrong for a held position: the
short perp and its spot hedge are still at the venue with nobody watching them.

`assess_restart` is the pure decision. It reads plain data and returns a frozen
`RestartVerdict`; it performs no I/O, sends nothing and never mutates anything.

MODES
  NORMAL        nothing blocks a normal restart. Entries as configured.
  PROTECT_ONLY  something blocks entries (environment conflict, unreadable DB,
                unresolved orders, kill switch). The venue's own position is
                verified, so reduce-only exits stay possible and must be run.
  CANNOT_PROTECT the exposure cannot be verified (venue unreadable) or the
                exit path itself is refused. An unmistakable operator alert is
                required; no entry, no blind order.

INVARIANTS
  * Unknown exposure is UNKNOWN, never flat. A conflicted or unreadable
    database is never evidence of flatness and is never adopted: exposure is
    taken from the VENUE snapshot only.
  * Entries are allowed only in NORMAL (and only if entries are enabled).
  * An order in any unresolved state (pending, submitted, partial, timed_out,
    unknown, reconciliation_required, contradicted, cancel_requested, ...) -
    or an unrecognised status - is listed in `no_resend_links`: its link must
    never be re-sent and it needs reconciliation against the venue.
  * Exits are reduce-only legs sized from the venue, never from the database.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

import order_lifecycle as _ol

NORMAL = "NORMAL"
PROTECT_ONLY = "PROTECT_ONLY"
CANNOT_PROTECT = "CANNOT_PROTECT"

#: Quantities at or below this are treated as zero (dust).
FLAT_EPS = 1e-12


class Exposure(str, Enum):
    FLAT = "FLAT"        # every leg read from the venue and zero
    OPEN = "OPEN"        # at least one leg is read and non-zero
    UNKNOWN = "UNKNOWN"  # no leg is non-zero, but a leg could not be read


@dataclass(frozen=True)
class VenueSnapshot:
    """A read-only venue reading. `None` means the read FAILED (UNKNOWN), which
    is not 0. perp_qty is signed: negative = short, positive = long. spot_qty
    is the base inventory held (>= 0)."""
    perp_qty: Optional[float] = None
    spot_qty: Optional[float] = None


@dataclass(frozen=True)
class OrderFact:
    """One order row from the store, as plain data."""
    order_link_id: str
    status: str
    leg: str = ""
    qty: float = 0.0
    filled_qty: float = 0.0


@dataclass(frozen=True)
class ExitLeg:
    """A reduce-only action the venue snapshot supports. Never an entry."""
    leg: str            # "perp" | "spot"
    side: str           # "Buy" (cover a short) | "Sell" (close a long / sell spot)
    qty: float
    reduce_only: bool = True


@dataclass(frozen=True)
class OperatorAlert:
    required: bool
    severity: str       # "NONE" | "WARNING" | "CRITICAL"
    message: str


@dataclass(frozen=True)
class RestartVerdict:
    mode: str
    entries_allowed: bool
    exits_allowed: bool
    exposure: Exposure
    position_verified: bool          # exposure read from the venue, all legs
    position_unknown: bool
    reasons: Tuple[str, ...]
    alert: OperatorAlert
    no_resend_links: frozenset       # links that must never be re-sent
    reconciliation_required: bool
    reduce_now: bool                 # a position needing immediate reduction
    exit_legs: Tuple[ExitLeg, ...]
    perp_qty: Optional[float]
    spot_qty: Optional[float]

    @property
    def required_operator_alert(self) -> bool:
        return self.alert.required


def _finite(x: Any) -> Optional[float]:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    f = float(x)
    return f if math.isfinite(f) else None


def classify_exposure(venue: Optional[VenueSnapshot]) -> Exposure:
    """UNKNOWN is distinct from FLAT. A non-finite or missing reading is a
    failed read."""
    if venue is None:
        return Exposure.UNKNOWN
    perp, spot = _finite(venue.perp_qty), _finite(venue.spot_qty)
    legs = (perp, spot)
    if any(v is not None and abs(v) > FLAT_EPS for v in legs):
        return Exposure.OPEN
    if any(v is None for v in legs):
        return Exposure.UNKNOWN
    return Exposure.FLAT


def _exit_legs(venue: VenueSnapshot) -> Tuple[ExitLeg, ...]:
    out = []
    perp, spot = _finite(venue.perp_qty), _finite(venue.spot_qty)
    if perp is not None and abs(perp) > FLAT_EPS:
        out.append(ExitLeg("perp", "Buy" if perp < 0 else "Sell", abs(perp)))
    if spot is not None and spot > FLAT_EPS:
        out.append(ExitLeg("spot", "Sell", spot))
    return tuple(out)


def assess_restart(
    *,
    environment_conflict: Optional[str],
    store_readable: bool,
    orders: Optional[Iterable[OrderFact]],
    venue: Optional[VenueSnapshot],
    kill_switch_engaged: Optional[bool],
    entries_enabled: bool,
    exits_permitted: bool = True,
    exits_refused_reason: str = "",
    immediate_reduction_required: bool = False,
) -> RestartVerdict:
    """Decide how a restarting process may treat the book.

    environment_conflict  `main.environment_conflict(...)` result (None = ok).
    store_readable        False if the DB could not be opened/read at all.
    orders                unresolved rows from the store; None = could not be
                          read (treated as unresolved, never as "none").
    venue                 read-only venue snapshot; None legs = UNKNOWN.
    kill_switch_engaged   True / False / None (unreadable = engaged).
    entries_enabled       the operator's own entry flag.
    exits_permitted       `carry_orders_permitted_for(cfg, client, exit=True)[0]`.
    immediate_reduction_required
                          caller-detected urgency (margin/liquidation risk).
    """
    reasons = []
    degraded = False       # something forbids a normal restart
    must_alert = False

    if environment_conflict:
        degraded = must_alert = True
        reasons.append(f"ENVIRONMENT_CONFLICT: {environment_conflict}; the "
                       "database is not adopted and not treated as flat")
    if not store_readable:
        degraded = must_alert = True
        reasons.append("STORE_UNREADABLE: the database cannot be read; its "
                       "ledger and orders are unknown, not empty")

    # Orders: unresolved / uncertain / contradicted / partial never re-sent.
    links = set()
    contradicted = partial = 0
    orders_known = orders is not None
    if not orders_known:
        reasons.append("ORDERS_UNREADABLE: the order log could not be read; "
                       "outstanding orders are unknown, not absent")
        degraded = must_alert = True
    else:
        for o in orders:
            st = str(o.status)
            if st in _ol.TERMINAL:
                continue
            links.add(str(o.order_link_id))
            if st == _ol.CONTRADICTED:
                contradicted += 1
            if st == _ol.PARTIAL or (_finite(o.filled_qty) or 0.0) > 0.0:
                partial += 1
        if links:
            degraded = must_alert = True
            reasons.append(
                f"UNRESOLVED_ORDERS: {len(links)} order(s) not final "
                f"({contradicted} contradicted, {partial} with fills); their "
                "links are never re-sent and need reconciliation against the "
                "venue")

    if kill_switch_engaged is not False:
        degraded = True
        reasons.append("KILL_SWITCH_UNREADABLE: treated as engaged"
                       if kill_switch_engaged is None else
                       "KILL_SWITCH_ENGAGED: no new entries")
    if not entries_enabled:
        reasons.append("ENTRIES_DISABLED: the entry flag is off")

    exposure = classify_exposure(venue)
    verified = exposure in (Exposure.FLAT, Exposure.OPEN) and venue is not None \
        and _finite(venue.perp_qty) is not None and _finite(venue.spot_qty) is not None
    unknown = exposure is Exposure.UNKNOWN or not verified
    legs = _exit_legs(venue) if venue is not None else ()
    has_position = exposure is Exposure.OPEN

    if exposure is Exposure.UNKNOWN:
        reasons.append("EXPOSURE_UNKNOWN: the venue position could not be "
                       "read; it is NOT assumed flat")
    elif exposure is Exposure.OPEN and not verified:
        reasons.append("EXPOSURE_PARTIAL: one leg is open, the other could "
                       "not be read")

    cannot = False
    if exposure is Exposure.UNKNOWN:
        cannot = True
    elif has_position and not exits_permitted:
        cannot = True
        reasons.append("EXIT_PATH_REFUSED: reduce-only exits are not "
                       f"permitted ({exits_refused_reason or 'gate refused'})")
    elif has_position and not legs:
        cannot = True
        reasons.append("EXIT_UNSIZED: the open position yields no sizeable "
                       "reduce-only leg")

    if cannot:
        mode = CANNOT_PROTECT
    elif degraded:
        mode = PROTECT_ONLY
    else:
        mode = NORMAL

    exits_allowed = (mode == PROTECT_ONLY and has_position and exits_permitted) \
        or (mode == NORMAL and exits_permitted)
    entries_allowed = mode == NORMAL and bool(entries_enabled)
    reduce_now = bool(immediate_reduction_required) and has_position \
        and exits_allowed
    if immediate_reduction_required and not reduce_now:
        reasons.append("REDUCTION_REQUIRED_BUT_IMPOSSIBLE: immediate "
                       "reduction needed and the exit path is not available")
    elif reduce_now:
        reasons.append("REDUCE_NOW: immediate reduction required")

    if mode == CANNOT_PROTECT:
        alert = OperatorAlert(
            True, "CRITICAL",
            "CRITICAL - POSITION MAY BE UNMANAGED - HUMAN ACTION REQUIRED NOW: "
            "the bot cannot establish protection ("
            + "; ".join(r.split(":")[0] for r in reasons) + "). "
            "Check the venue and close or hedge the position manually. No "
            "orders will be sent by this process.")
    elif immediate_reduction_required and not reduce_now:
        alert = OperatorAlert(True, "CRITICAL",
                              "CRITICAL - reduction required but the exit "
                              "path is unavailable: act manually now.")
    elif must_alert or reduce_now or (degraded and has_position):
        alert = OperatorAlert(
            True, "CRITICAL" if reduce_now else "WARNING",
            "PROTECT-ONLY RESTART: entries are disabled; the open position is "
            "managed by reduce-only exits only. Reconcile: "
            + "; ".join(r.split(":")[0] for r in reasons if not
                        r.startswith("ENTRIES_DISABLED")) + ".")
    else:
        alert = OperatorAlert(False, "NONE", "")

    return RestartVerdict(
        mode=mode, entries_allowed=entries_allowed,
        exits_allowed=bool(exits_allowed), exposure=exposure,
        position_verified=bool(verified), position_unknown=bool(unknown),
        reasons=tuple(reasons), alert=alert,
        no_resend_links=frozenset(links),
        reconciliation_required=bool(links or not orders_known),
        reduce_now=reduce_now, exit_legs=legs if exits_allowed else (),
        perp_qty=_finite(venue.perp_qty) if venue else None,
        spot_qty=_finite(venue.spot_qty) if venue else None)


def may_send_order(verdict: RestartVerdict, *, order_link_id: str,
                   reduce_only: bool) -> Tuple[bool, str]:
    """Gate for any order this process considers after the verdict. An entry
    needs entries_allowed; a reduce-only exit needs exits_allowed; a link that
    is unresolved is never re-sent."""
    if order_link_id in verdict.no_resend_links:
        return (False, "link is unresolved: reconcile, never re-send")
    if verdict.mode == CANNOT_PROTECT:
        return (False, "CANNOT_PROTECT: no orders from this process")
    if reduce_only:
        return (verdict.exits_allowed,
                "reduce-only exit" if verdict.exits_allowed
                else "exits not allowed")
    return (verdict.entries_allowed,
            "entry" if verdict.entries_allowed else
            f"{verdict.mode}: entries are disabled")


def build_alert_payload(verdict: RestartVerdict) -> Dict[str, Any]:
    """JSON-serialisable record for the existing journal/log path."""
    return {
        "event": "protective_restart",
        "level": ("critical" if verdict.alert.severity == "CRITICAL"
                  else "warning" if verdict.alert.required else "info"),
        "mode": verdict.mode,
        "operator_alert_required": verdict.alert.required,
        "message": verdict.alert.message,
        "reasons": list(verdict.reasons),
        "exposure": verdict.exposure.value,
        "position_verified": verdict.position_verified,
        "position_unknown": verdict.position_unknown,
        "entries_allowed": verdict.entries_allowed,
        "exits_allowed": verdict.exits_allowed,
        "reduce_now": verdict.reduce_now,
        "no_resend_links": sorted(verdict.no_resend_links),
        "exit_legs": [{"leg": l.leg, "side": l.side, "qty": l.qty,
                       "reduce_only": l.reduce_only} for l in verdict.exit_legs],
        "perp_qty": verdict.perp_qty,
        "spot_qty": verdict.spot_qty,
    }
