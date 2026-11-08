"""Cross-module reconciliation of the multi-symbol layer: ONE canonical financial / position truth, and a system that HALTS NEW EXPOSURE (never silently overwrites) when components
disagree. Pure: no clock, no I/O, no venue, no network. The inputs are views the caller already holds; nothing here reads ``bot/state``.

CANONICAL OWNERSHIP (the single source of each fact; every other holder is a DERIVED view that must agree with it, and a disagreement is a finding, never a silent overwrite)
  market snapshots ............ MicroDataset / the replay World
  instrument metadata ......... the universe capture rows
  candidate id + spec digest .. the candidate spec file / competition record
  positions ................... TESTNET: the venue's own position read (complete flag required).  REPLAY / PAPER: the SimVenue book.
                                engine (``ExecutionEngine.positions``), portfolio (``ms_portfolio.PortfolioState``), ledger (``ms_accounting.Ledger.positions``) and artifacts are DERIVED
  reservations ................ ``ms_authority``'s durable ledger (``ms_groups.jsonl`` / ``ms_reservations.jsonl``)
  risk policy ................. ``ms_risk``'s closed schema (``policy_refusals``)
  order-group state ........... the ``ExecutionEngine`` journal
  order ids ................... ``orderLinkId = group_id-leg_id`` (``ms_authority.link_id``)
  fills ....................... venue executions keyed by ``execId`` (``fill_id``)
  funding events .............. keyed ``(symbol, settlement ts_ms)``
  account state ............... the venue read, WITH its timestamp
  net P&L ..................... ``ms_accounting.Ledger`` via the one net definition (``Ledger.canonical_net``): gross at mid + funding - fees - taker slippage - adverse selection, each once;
                                open legs valued at the exit side of the book less the exit fee
  testnet session evidence .... ``testnet_session.json``
  incidents ................... the engine journal's anomalies

Position keys are canonical ``'SYM'`` (linear perp) and ``'SPOT:SYM'`` (spot). ``'linear:SYM'`` / ``'spot:SYM'`` (the engine and SimVenue spelling) and a trailing ``'#tag'`` are folded
into them (``canon_key``), so a position cannot hide behind a spelling.

Findings (typed; each names the key and the views that disagree): POSITION_MISMATCH, QTY_MISMATCH, MISSING_IN_VENUE, MISSING_IN_LEDGER, RESERVATION_LEAK, STALE_VIEW,
INCOMPLETE_VENUE_READ, NET_PNL_MISMATCH, plus LEDGER_INTEGRITY (a conflicting duplicate / orphan fill / conflicting funding inside the ledger) and RESERVATION_RELEASED_EARLY.
``ReconcileReport.halt_new_exposure`` is True when there is ANY finding; ``reasons`` name each. A missing view is not "agreement": the venue read is REQUIRED (``require_venue``).

``HaltOnDisagreement`` is the hook Worker D / E wire into ``ExecutionEngine.halt_new_exposure``; ``HaltOnDisagreement.for_engine(engine)`` is the tested adapter. It only HALTS (and, only on an
explicit call, resumes): it never edits a position, a ledger or a reservation. ``release_candidates`` lists leaked reservations; ``release_leaked`` is the explicit, narrow release of a
reservation whose group is terminal in the engine AND flat in the engine's positions (``MultiSymbolAuthority.resolve_group``); it refuses anything else.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ms_accounting import to_decimal  # noqa: E402

POSITION_MISMATCH = "POSITION_MISMATCH"
QTY_MISMATCH = "QTY_MISMATCH"
MISSING_IN_VENUE = "MISSING_IN_VENUE"
MISSING_IN_LEDGER = "MISSING_IN_LEDGER"
RESERVATION_LEAK = "RESERVATION_LEAK"
RESERVATION_RELEASED_EARLY = "RESERVATION_RELEASED_EARLY"
STALE_VIEW = "STALE_VIEW"
INCOMPLETE_VENUE_READ = "INCOMPLETE_VENUE_READ"
NET_PNL_MISMATCH = "NET_PNL_MISMATCH"
LEDGER_INTEGRITY = "LEDGER_INTEGRITY"
FINDING_CODES = (POSITION_MISMATCH, QTY_MISMATCH, MISSING_IN_VENUE, MISSING_IN_LEDGER, RESERVATION_LEAK, RESERVATION_RELEASED_EARLY, STALE_VIEW, INCOMPLETE_VENUE_READ,
                 NET_PNL_MISMATCH, LEDGER_INTEGRITY)
TERMINAL_STATES = ("CLOSED", "FAILED")
SPOT_PREFIX = "SPOT:"
QTY_TOL = Decimal("1e-9")
PNL_TOL = Decimal("1e-6")
PORTFOLIO_QTY_REL_TOL = Decimal("0.05")     # the portfolio holds USD notional at the sizing mark, not a quantity: compared by quantity only when a mark is given, within 5%
DEFAULT_MAX_AGE_MS = 60_000


def canon_key(k: Any) -> Optional[str]:
    """'linear:SYM' / 'SYM#tag' -> 'SYM'; 'spot:SYM' / 'SPOT:SYM#tag' -> 'SPOT:SYM'. None for a non-string / empty key."""
    if not isinstance(k, str) or not k:
        return None
    k = k.split("#", 1)[0]
    if k.startswith("linear:"):
        k = k[len("linear:"):]
    elif k.startswith("spot:"):
        k = SPOT_PREFIX + k[len("spot:"):]
    return k or None


def _fold(positions: Mapping[Any, Any]) -> Tuple[Dict[str, Decimal], List[str]]:
    """canonical key -> signed quantity (summed over spellings). The second value lists keys whose quantity is unknown (None / non-numeric): never read as zero."""
    out: Dict[str, Decimal] = {}
    unknown: List[str] = []
    for k, v in positions.items():
        ck = canon_key(k)
        q = to_decimal(v.get("qty") if isinstance(v, Mapping) else v)
        if ck is None or q is None:
            unknown.append(str(k))
            continue
        out[ck] = out.get(ck, Decimal(0)) + q
    return out, unknown


@dataclass(frozen=True)
class Finding:
    code: str
    key: str
    views: Tuple[str, ...]
    detail: str = ""

    def reason(self) -> str:
        return f"{self.code}:{self.key}:{'/'.join(self.views)}"


@dataclass(frozen=True)
class ReconcileReport:
    findings: Tuple[Finding, ...]
    halt_new_exposure: bool
    reasons: Tuple[str, ...]
    compared: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.findings

    def codes(self) -> Tuple[str, ...]:
        return tuple(sorted({f.code for f in self.findings}))

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "halt_new_exposure": self.halt_new_exposure, "reasons": list(self.reasons),
                "findings": [{"code": f.code, "key": f.key, "views": list(f.views), "detail": f.detail} for f in self.findings], "compared": dict(self.compared)}


@dataclass(frozen=True)
class Views:
    """Everything a reconciliation compares. Every field is optional; what is absent is NOT agreement (the venue read in particular is required, see ``reconcile``)."""
    engine_positions: Optional[Mapping[str, Any]] = None        # ExecutionEngine.positions(): {'linear:SYM': '-10', 'spot:SYM': '10'}
    engine_spot_baseline: Optional[Mapping[str, Any]] = None    # ExecutionEngine.spot_base: balance the client owned that the engine did not buy
    engine_group_states: Optional[Mapping[str, Any]] = None     # ExecutionEngine.states()
    portfolio: Optional[Any] = None                             # ms_portfolio.PortfolioState (positions are USD notional)
    portfolio_marks: Optional[Mapping[str, Any]] = None         # canonical key -> mark, to compare the portfolio by quantity as well as by sign
    ledger_positions: Optional[Mapping[str, Any]] = None        # ms_accounting.Ledger.positions()
    ledger_flags: Sequence[str] = ()                            # ms_accounting.Ledger.integrity_flags()
    reservations: Optional[Mapping[str, Any]] = None            # authority_reservations(authority)
    venue_positions: Optional[Mapping[str, Any]] = None         # THE position read (TESTNET: the venue; REPLAY / PAPER: SimVenue.positions())
    venue_complete: bool = False                                # the read says it is complete
    artifact_positions: Optional[Mapping[str, Any]] = None      # position block of an artifact, canonical or engine spelling
    net_pnl: Optional[Mapping[str, Any]] = None                 # name -> net (e.g. {'ledger': .., 'artifact': ..}); None is "unknown"; the first name is the reference
    view_ts_ms: Optional[Mapping[str, Optional[int]]] = None    # name -> timestamp of that view (e.g. {'account': .., 'market': ..})
    now_ms: Optional[int] = None


def authority_reservations(authority: Any) -> Dict[str, Any]:
    """The durable reservation view of a ``MultiSymbolAuthority`` (its group ledger + reservation links). Read-only; raises nothing the authority would not."""
    rows = authority._rows()
    registered = {r["group_id"]: [{"leg_id": l["leg_id"], "symbol": l["symbol"], "category": l["category"]} for l in r["plan"]["legs"]] for r in rows if r["event"] == "group"}
    return {"registered": registered, "resolved": authority._resolved_sets(rows)[1], "settled": {(r["group_id"], r["leg_id"]) for r in rows if r["event"] == "settle"},
            "reserved_links": set(l for l in authority._reserved_links() if isinstance(l, str))}


def collect_views(*, engine: Any = None, portfolio: Any = None, ledger: Any = None, authority: Any = None, venue_read: Optional[Mapping[str, Any]] = None,
                  artifact_positions: Optional[Mapping[str, Any]] = None, net_pnl: Optional[Mapping[str, Any]] = None, view_ts_ms: Optional[Mapping[str, Optional[int]]] = None,
                  now_ms: Optional[int] = None, portfolio_marks: Optional[Mapping[str, Any]] = None) -> Views:
    """Build ``Views`` from the live objects. ``venue_read`` is the engine's venue view shape ({"positions": .., "complete": bool}). If ``net_pnl`` is None and a ledger is given, the ledger's
    canonical net is the reference entry."""
    npnl = dict(net_pnl) if net_pnl is not None else {}
    if ledger is not None and "ledger" not in npnl:
        npnl = {"ledger": ledger.canonical_net()["net"], **npnl}
    vr = venue_read if isinstance(venue_read, Mapping) else None
    return Views(engine_positions=engine.positions() if engine is not None else None, engine_spot_baseline=dict(engine.spot_base) if engine is not None else None,
                 engine_group_states=engine.states() if engine is not None else None, portfolio=portfolio, portfolio_marks=portfolio_marks,
                 ledger_positions=ledger.positions() if ledger is not None else None, ledger_flags=tuple(ledger.integrity_flags()) if ledger is not None else (),
                 reservations=authority_reservations(authority) if authority is not None else None,
                 venue_positions=vr.get("positions") if vr else None, venue_complete=bool(vr.get("complete", False)) if vr else False, artifact_positions=artifact_positions,
                 net_pnl=npnl or None, view_ts_ms=view_ts_ms, now_ms=now_ms)


def _portfolio_qty(portfolio: Any, marks: Mapping[str, Decimal]) -> Tuple[Dict[str, Decimal], Dict[str, Decimal], List[str]]:
    """({key: signed notional}, {key: qty where a mark exists}, unknown keys)."""
    notional: Dict[str, Decimal] = {}
    qty: Dict[str, Decimal] = {}
    unknown: List[str] = []
    for k, p in (getattr(portfolio, "positions", None) or {}).items():
        ck = canon_key(k)
        n = to_decimal(getattr(p, "notional_usd", None))
        if ck is None or n is None:
            unknown.append(str(k))
            continue
        notional[ck] = notional.get(ck, Decimal(0)) + n
    for ck, n in notional.items():
        m = marks.get(ck)
        if m is not None and m > 0:
            qty[ck] = n / m
    return notional, qty, unknown


def reconcile(views: Views, *, require_venue: bool = True, require_engine_knows_reserved: bool = True, qty_tol: Decimal = QTY_TOL, pnl_tol: Decimal = PNL_TOL,
              max_age_ms: Any = DEFAULT_MAX_AGE_MS) -> ReconcileReport:
    """Compare every view with the canonical one and return typed findings. The venue position read is the reference when it is present and complete; when it is not, that is itself an
    ``INCOMPLETE_VENUE_READ`` finding (halt) and the engine becomes the reference for the remaining comparisons so the other disagreements are still named. Nothing is overwritten."""
    F: List[Finding] = []
    compared: Dict[str, Any] = {}

    def add(code: str, key: str, views_: Sequence[str], detail: str = "") -> None:
        F.append(Finding(code, key, tuple(views_), detail))

    # ---- venue read completeness
    venue: Optional[Dict[str, Decimal]] = None
    base: Dict[str, Decimal] = {}
    if views.engine_spot_baseline:
        base, _bu = _fold(views.engine_spot_baseline)
        base = {k if k.startswith(SPOT_PREFIX) else SPOT_PREFIX + k: v for k, v in base.items()}
    if views.venue_positions is None:
        if require_venue:
            add(INCOMPLETE_VENUE_READ, "venue", ("venue",), "NO_VENUE_POSITION_READ")
    elif not isinstance(views.venue_positions, Mapping):
        add(INCOMPLETE_VENUE_READ, "venue", ("venue",), "VENUE_READ_NOT_A_MAPPING")
    else:
        venue, unk = _fold(views.venue_positions)
        if unk:
            add(INCOMPLETE_VENUE_READ, "venue", ("venue",), "UNKNOWN_QTY:" + ",".join(sorted(unk))[:80])
            venue = None
        elif not views.venue_complete:
            add(INCOMPLETE_VENUE_READ, "venue", ("venue",), "READ_NOT_COMPLETE")
            venue = None
    if venue is not None:                                                  # the client's own pre-existing spot holding is not the engine's: compare on the engine's DELTA
        venue = {k: v - base.get(k, Decimal(0)) for k, v in venue.items()}
        for k in base:
            venue.setdefault(k, -base[k])

    # ---- collect the derived position views
    derived: Dict[str, Dict[str, Decimal]] = {}
    if views.engine_positions is not None:
        derived["engine"], u = _fold(views.engine_positions)
        for k in u:
            add(POSITION_MISMATCH, k, ("engine",), "UNKNOWN_QTY")
    if views.ledger_positions is not None:
        derived["ledger"], u = _fold(views.ledger_positions)
        for k in u:
            add(POSITION_MISMATCH, k, ("ledger",), "UNKNOWN_QTY")
    if views.artifact_positions is not None:
        derived["artifact"], u = _fold(views.artifact_positions)
        for k in u:
            add(POSITION_MISMATCH, k, ("artifact",), "UNKNOWN_QTY")
    port_notional: Optional[Dict[str, Decimal]] = None
    port_qty: Dict[str, Decimal] = {}
    if views.portfolio is not None:
        marks = {canon_key(k): to_decimal(v) for k, v in (views.portfolio_marks or {}).items()}
        port_notional, port_qty, u = _portfolio_qty(views.portfolio, {k: v for k, v in marks.items() if k and v is not None})
        for k in u:
            add(POSITION_MISMATCH, k, ("portfolio",), "UNKNOWN_NOTIONAL")
    ref_name = "venue" if venue is not None else ("engine" if "engine" in derived else None)
    compared["reference"] = ref_name
    compared["views"] = sorted(list(derived) + (["portfolio"] if port_notional is not None else []) + (["venue"] if venue is not None else []))
    ref = venue if venue is not None else derived.get("engine")

    def cmp_one(name: str, key: str, r: Decimal, n: Decimal) -> None:
        rz, nz = abs(r) <= qty_tol, abs(n) <= qty_tol
        if rz and nz:
            return
        if nz and not rz:
            add(MISSING_IN_LEDGER if name == "ledger" else POSITION_MISMATCH, key, (ref_name or "?", name), f"{ref_name} {r} but {name} flat/absent")
        elif rz and not nz:
            add(MISSING_IN_VENUE if ref_name == "venue" else POSITION_MISMATCH, key, (ref_name or "?", name), f"{name} {n} but {ref_name} flat/absent")
        elif (r > 0) != (n > 0):
            add(POSITION_MISMATCH, key, (ref_name or "?", name), f"{ref_name} {r} vs {name} {n}: opposite sides")
        elif abs(r - n) > qty_tol:
            add(QTY_MISMATCH, key, (ref_name or "?", name), f"{ref_name} {r} vs {name} {n}")

    if ref is not None:
        for name, d in sorted(derived.items()):
            if name == ref_name:
                continue
            for k in sorted(set(ref) | set(d)):
                cmp_one(name, k, ref.get(k, Decimal(0)), d.get(k, Decimal(0)))
        if port_notional is not None:
            for k in sorted(set(ref) | set(port_notional)):
                r, n = ref.get(k, Decimal(0)), port_notional.get(k, Decimal(0))
                rz, nz = abs(r) <= qty_tol, n == 0
                if rz and nz:
                    continue
                if nz:
                    add(POSITION_MISMATCH, k, (ref_name or "?", "portfolio"), f"{ref_name} {r} but portfolio holds no notional")
                elif rz:
                    add(MISSING_IN_VENUE if ref_name == "venue" else POSITION_MISMATCH, k, (ref_name or "?", "portfolio"), f"portfolio notional {n} but {ref_name} flat/absent")
                elif (r > 0) != (n > 0):
                    add(POSITION_MISMATCH, k, (ref_name or "?", "portfolio"), "opposite sides")
                elif k in port_qty and abs(port_qty[k] - r) > PORTFOLIO_QTY_REL_TOL * abs(r):
                    add(QTY_MISMATCH, k, (ref_name or "?", "portfolio"), f"{ref_name} {r} vs portfolio {port_qty[k]} (notional / mark)")

    # ---- the ledger's own integrity (conflicting duplicates, orphans, conflicting funding)
    for f in views.ledger_flags or ():
        add(LEDGER_INTEGRITY, str(f), ("ledger",), "")

    # ---- net P&L: one definition, every holder agrees
    if views.net_pnl:
        items = list(views.net_pnl.items())
        ref_n, ref_v = items[0][0], items[0][1]
        rd = to_decimal(ref_v)
        for name, v in items[1:]:
            vd = to_decimal(v)
            if (rd is None) != (vd is None) or (rd is not None and vd is not None and abs(rd - vd) > pnl_tol):
                add(NET_PNL_MISMATCH, "net", (ref_n, name), f"{ref_n}={ref_v} {name}={v}")

    # ---- reservations
    rs = views.reservations
    if rs is not None:
        states = {g: str(getattr(s, "value", s)) for g, s in (views.engine_group_states or {}).items()}
        registered, resolved, links = rs.get("registered", {}), set(rs.get("resolved", ())), set(rs.get("reserved_links", ()))
        for link in sorted(links):
            gid = link.split("-", 1)[0]
            if gid not in registered:
                add(RESERVATION_LEAK, link, ("authority",), "LINK_WITHOUT_REGISTERED_GROUP")
        for gid in sorted(registered):
            st = states.get(gid)
            if gid not in resolved:
                if st in TERMINAL_STATES:
                    add(RESERVATION_LEAK, gid, ("authority", "engine"), f"GROUP_{st}_BUT_RESERVATION_NOT_RELEASED")
                elif st is None and views.engine_group_states is not None and require_engine_knows_reserved:
                    add(RESERVATION_LEAK, gid, ("authority", "engine"), "RESERVED_GROUP_UNKNOWN_TO_ENGINE")
            elif st is not None and st not in TERMINAL_STATES:
                add(RESERVATION_RELEASED_EARLY, gid, ("authority", "engine"), f"RESOLVED_BUT_ENGINE_{st}")

    # ---- staleness
    if views.view_ts_ms:
        for name, ts in sorted(views.view_ts_ms.items()):
            lim = max_age_ms.get(name, DEFAULT_MAX_AGE_MS) if isinstance(max_age_ms, Mapping) else max_age_ms
            if type(ts) is not int or ts <= 0:
                add(STALE_VIEW, name, (name,), "TS_UNKNOWN")
            elif views.now_ms is None:
                add(STALE_VIEW, name, (name,), "NOW_UNKNOWN")
            elif views.now_ms - ts < 0:
                add(STALE_VIEW, name, (name,), "TS_IN_FUTURE")
            elif views.now_ms - ts > lim:
                add(STALE_VIEW, name, (name,), f"AGE_{views.now_ms - ts}_MS_ABOVE_{lim}")

    uniq: Dict[str, Finding] = {}
    for f in F:
        uniq.setdefault(f.reason(), f)
    findings = tuple(uniq.values())
    return ReconcileReport(findings, bool(findings), tuple(uniq), compared)


class HaltOnDisagreement:
    """The hook that turns a disagreement into a halt of new exposure. ``halt(reason, now_ms)`` is ``ExecutionEngine.halt_new_exposure`` (``resume`` its ``resume_new_exposure``).
    Called with a report it HALTS when ``report.halt_new_exposure`` (reason ``RECONCILE_DISAGREEMENT:<codes+keys>``; a changed finding set replaces the previous reason). It never resumes on
    its own unless ``auto_resume=True`` AND a later report is clean; ``clear(now_ms)`` is the explicit resume. It touches no position, ledger or reservation."""
    PREFIX = "RECONCILE_DISAGREEMENT:"

    def __init__(self, halt: Callable[[str, int], None], resume: Optional[Callable[[str, int], None]] = None, *, auto_resume: bool = False) -> None:
        self._halt, self._resume, self.auto_resume = halt, resume, bool(auto_resume)
        self.active: Optional[str] = None
        self.history: List[Tuple[int, Tuple[str, ...]]] = []

    @classmethod
    def for_engine(cls, engine: Any, *, auto_resume: bool = False) -> "HaltOnDisagreement":
        return cls(engine.halt_new_exposure, engine.resume_new_exposure, auto_resume=auto_resume)

    @staticmethod
    def reason_for(report: ReconcileReport) -> str:
        return HaltOnDisagreement.PREFIX + ",".join(sorted({f"{f.code}:{f.key}" for f in report.findings}))[:200]

    def __call__(self, report: ReconcileReport, now_ms: int) -> ReconcileReport:
        if report.halt_new_exposure:
            reason = self.reason_for(report)
            if reason != self.active:
                self._halt(reason, now_ms)
                old, self.active = self.active, reason
                if old is not None and self._resume is not None:
                    self._resume(old, now_ms)
            self.history.append((now_ms, report.reasons))
        elif self.active is not None and self.auto_resume:
            self.clear(now_ms)
        return report

    def clear(self, now_ms: int) -> bool:
        """Explicit resume of the reason this hook raised (a human / supervisor decision). False when nothing was active or there is no resume callable."""
        if self.active is None or self._resume is None:
            return False
        self._resume(self.active, now_ms)
        self.active = None
        return True


def release_candidates(views: Views) -> List[str]:
    """Group ids whose reservation LEAKED (terminal in the engine, reservation not released)."""
    rep = reconcile(Views(reservations=views.reservations, engine_group_states=views.engine_group_states), require_venue=False, require_engine_knows_reserved=False)
    return sorted(f.key for f in rep.findings if f.code == RESERVATION_LEAK and f.detail.startswith("GROUP_"))


def release_leaked(authority: Any, engine: Any, group_ids: Iterable[str], venue_read: Optional[Mapping[str, Any]] = None) -> Dict[str, str]:
    """Release (``authority.resolve_group``) a leaked reservation, ONLY when the group is terminal in the engine AND a COMPLETE VENUE position read (``venue_read``, the engine's venue view
    shape) shows no position on any of the group's symbols (FLAT is the venue's own read, never ``engine.positions()``). Without a complete venue read nothing is released. Everything
    else is refused with the reason; nothing else is touched. Returns {group_id: 'RELEASED' | 'REFUSED:<why>'}."""
    out: Dict[str, str] = {}
    rs = authority_reservations(authority)
    states = {g: str(getattr(s, "value", s)) for g, s in engine.states().items()}
    vp = venue_read.get("positions") if isinstance(venue_read, Mapping) else None
    venue_ok = isinstance(vp, Mapping) and bool(venue_read.get("complete", False))              # type: ignore[union-attr]
    pos, unk = _fold(vp) if venue_ok else ({}, [])
    if unk:
        venue_ok = False
    base: Dict[str, Decimal] = {}
    if venue_ok:
        for k, v in (getattr(engine, "spot_base", None) or {}).items():
            ck = canon_key(k if str(k).startswith(("spot:", "SPOT:")) else "spot:" + str(k))
            q = to_decimal(v)
            if ck and q is not None:
                base[ck] = q
    for gid in sorted(set(group_ids)):
        legs = rs["registered"].get(gid)
        if legs is None:
            out[gid] = "REFUSED:NOT_REGISTERED"
        elif gid in rs["resolved"]:
            out[gid] = "REFUSED:ALREADY_RESOLVED"
        elif states.get(gid) not in TERMINAL_STATES:
            out[gid] = f"REFUSED:ENGINE_STATE_{states.get(gid)}"
        elif not venue_ok:
            out[gid] = "REFUSED:NO_COMPLETE_VENUE_READ"
        elif any(abs(pos.get(k, Decimal(0)) - base.get(k, Decimal(0))) > QTY_TOL
                 for k in (canon_key(("SPOT:" if l["category"] == "spot" else "") + l["symbol"]) or "" for l in legs)):
            out[gid] = "REFUSED:VENUE_HOLDS_A_POSITION_ON_THE_GROUPS_SYMBOL"
        else:
            authority.resolve_group(gid)
            out[gid] = "RELEASED"
    return out
