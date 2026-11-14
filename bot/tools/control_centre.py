"""Owner control centre: a READ-ONLY, localhost-only operator UI over data that already exists. It tells one operator, truthfully, what the system knows and what it does not.

    python3 tools/control_centre.py [--data-root DIR] [--universe-dir DIR] [--artifacts-dir DIR] [--testnet-dir DIR] [--port 8787] [--with-economics] [--verify]
    python3 tools/control_centre.py --print-state ...        # the JSON the UI renders, no server

What it is NOT: it has no controls. It cannot place, amend or cancel an order, arm anything, write a file, or make an outbound request. It never imports a venue client, a
transport or any signing code (``tests/test_control_centre.py`` pins that). The HTTP surface is GET-only on 127.0.0.1 (any other bind host is refused at start-up):

    GET /               the page            GET /static/{index.html,app.js,style.css}   (an allow-list of exactly these; nothing else is served)
    GET /api/state      the state dict (JSON, allow_nan=False)                           GET /api/health   {"ok": true} (liveness only)
    anything else => 404; any method other than GET => 405; a Host header that is not localhost/127.0.0.1 (DNS rebinding) => 403; no query string is ever used.
Headers on every response: Content-Security-Policy (default-src 'self'), X-Content-Type-Options nosniff, Cache-Control no-store. No env VALUE is logged or returned
(secrets are ``set``/``unset`` only); the access log is silenced.

Sources (all read-only, all existing modules): ``owner_console.build`` (environment flags, capital, eligibility, testnet qualification, backup, P&L stub),
``promotion_gate.evaluate_promotion_gate`` (the gate verdict), ``candidate_readiness`` (candidate specs, readiness, universe scan), ``data_freshness_audit.audit`` (three axes),
``universe_capture.verify_capture_dir`` (a capture's integrity) and the JSON ARTIFACTS below. Nothing is invented: a value with no source is ``{"v": null, "basis": "MISSING"}``
and a section with no source says so (``state: "NO_DATA"``); NO_DATA is never green.

BASIS vocabulary (closed; the UI shows the legend on every view; ``val()`` forces MISSING for a null value):
    MEASURED                observed in captured data or the owner account
    EXCHANGE_REPORTED       a venue response
    CALCULATED              derived from measured inputs
    ESTIMATED               a model or an assumption produced it
    SIMULATED               replay / paper; never realised cash
    MISSING                 no source; NOT zero, NOT fine
    UNVERIFIED              self-consistent but not authenticated (e.g. a capture whose raw responses re-hash: it proves what was read, not that the exchange produced it)
    OWNER_SUPPLIED_UNVERIFIED   an owner import that has not been reconciled against the venue

ARTIFACT CONTRACT (files in ``--artifacts-dir``; written by the multi-symbol layer's CLIs; this module consumes ONLY these files, never the ms_* modules). Every file is one JSON
object <= 1 MiB, UTF-8, no NaN/Infinity, a regular file (not a symlink or directory), with these common keys:
    schema        exact string below            generated_ms  int, UTC ms (not more than 5 min in the future)
    basis         one of the per-file allowed bases below (anything else => the file is rejected)
Unknown extra keys are ignored (never shown). ``num`` = finite JSON number (not bool); ``num|null`` = unknown (null means UNKNOWN, never zero). Money is USD, rates are basis points.
A missing file => section shows an empty state. A malformed/oversize/symlink/NaN/wrong-schema file => ``state: "ERROR"`` listing the file and the reason; no value from it is shown.

  replay_report.json    schema "replay_report/v1"   basis: SIMULATED
      universe_scope  "SURVIVORS_ONLY" | "DELISTINGS_OBSERVED"     (anything but a corroborated DELISTINGS_OBSERVED is shown as survivors-only)
      period_start_ms int|null, period_end_ms int|null, n_groups int
      groups_by_state {GroupState name: int}                        gross_pnl_usd num|null, fees_usd num|null, net_pnl_usd num|null   (gross and net are DIFFERENT fields)
      symbols [ {symbol str, kind OPP_KIND, spot_hedge HEDGEABLE_ON_BYBIT|PERP_ONLY|HEDGE_UNKNOWN, n_groups int, gross_pnl_usd num|null, fees_usd num|null, net_pnl_usd num|null} ]
  portfolio_state.json  schema "portfolio_state/v1" basis: SIMULATED | MEASURED | EXCHANGE_REPORTED | OWNER_SUPPLIED_UNVERIFIED
      equity_usd num|null, gross_exposure_usd num, net_exposure_usd num, unrealised_pnl_usd num|null
      positions [ {symbol str, kind OPP_KIND, spot_hedge (as above), side "Buy"|"Sell", notional_usd num, entry_price num|null, mark_price num|null, unrealised_pnl_usd num|null, group_id str|null} ]
  risk_state.json       schema "risk_state/v1"      basis: CALCULATED | MEASURED | SIMULATED
      halted bool, halt_reasons [str]
      limits [ {name str, unit str, limit num|null, value num|null, status "OK"|"BREACH"|"UNKNOWN"} ]
      alerts [ {severity "INFO"|"WARN"|"CRITICAL", code str, detail str} ]
  ledger_pnl.json       schema "ledger_pnl/v1"      basis: SIMULATED | MEASURED | EXCHANGE_REPORTED | OWNER_SUPPLIED_UNVERIFIED
      period_start_ms int|null, period_end_ms int|null
      realised_gross_usd num|null (the realised PRICE result at mid ALONE: funding is funding_usd), fees_usd num|null, funding_usd num|null, realised_net_usd num|null, unrealised_usd num|null
      (identity: realised_net = realised_gross + funding - fees - taker_slippage - adverse_selection)
      exit_adjustment_usd num|null (OPTIONAL: unrealised_usd is the LIQUIDATION value of what is open; portfolio unrealised is mark-to-market; unrealised_usd - exit_adjustment_usd = portfolio unrealised)
      taker_slippage_usd num|null, adverse_selection_usd num|null (OPTIONAL modelled costs, shown as slippage / adverse selection with their source field)
      net_usd num|null (OPTIONAL: the TOTAL net, = realised_net_usd + unrealised_usd, the same figure as replay_report net_pnl_usd; absent = not stated)
      (a SIMULATED ledger is shown as simulated P&L, never as realised; net is never inferred from gross)
  competition.json      schema "competition/v1"     basis: ESTIMATED | SIMULATED | CALCULATED
      candidates [ {candidate_id str, kind OPP_KIND, spot_hedge (as above), status "RESEARCHING"|"REJECTED"|"INSUFFICIENT_EVIDENCE"|"SUFFICIENT_EVIDENCE", reason str|null (REQUIRED non-empty for REJECTED and
                    INSUFFICIENT_EVIDENCE), gross_bps_horizon num|null, net_bps_horizon num|null, n_observations int|null, prov PROV} ]
  opportunities.json    schema "opportunities/v1"   basis: ESTIMATED | CALCULATED
      decision_ms int, est_net_usd_per_day num|null
      items [ {candidate_id str, kind OPP_KIND, symbols [str], spot_hedge (as above), hold_days num, gross_bps_horizon num|null, net_bps_horizon num|null, capacity_usd num|null,
               prov PROV, exclusions [str]} ]
  OPP_KIND = SPOT_PERP_CARRY | PERP_FUNDING_SPREAD | PERP_DIRECTIONAL_FUNDING;  PROV = MEASURED|VENUE_STATED|DERIVED|MODELLED|ASSUMED|UNKNOWN (ms_types.PROVS).
  Rule the UI enforces: only SPOT_PERP_CARRY with spot_hedge HEDGEABLE_ON_BYBIT is called carry; a PERP_ONLY / unknown-hedge symbol is "directional funding (not carry)".

RUN IDENTITY. Every artifact SHOULD carry ``identity`` = ms_types.RunIdentity.as_dict() {run_id, mode REPLAY|PAPER|TESTNET, candidate_id, spec_digest, data_digest, code_revision,
generated_ms}. A file without it is accepted as LEGACY_NO_IDENTITY and the whole state is flagged UNVERIFIED. The simulated "run family" (every file but testnet_session.json) must agree on
run_id, candidate_id, data_digest and mode; a disagreement is INCONSISTENT_RUN: a loud banner, and NO figure that needs two files (net, gross, fees, funding, realised, unrealised, the
plain-language answers) is combined or shown. testnet_session.json is a different family: its mode must be TESTNET and nothing in it is ever added to a simulated number.

More artifacts (all optional; missing = empty state; ``basis`` is not required for these):
  pipeline_summary.json  "pipeline_summary/v1"   outcome (or outcome_code) in OUTCOME_CODES, reason?, required_span{}, data_digest_parts{}, evidence_basis{}
  testnet_session.json   "testnet_session/v1"    (read from --testnet-dir when given, else --artifacts-dir; basis NOT_RUN may carry a preflight result and a plan, never orders/executions/a position/a confirmation) venue 'Testnet', endpoint_host (must be the registered testnet host), credential_state{dedicated_key_present, dedicated_secret_present,
                         generic_credentials_ignored}, preflight{status NOT_RUN|PASS|FAIL, checked_ms, checks[{name, ok, detail}]}, plan{digest, manifest_digest, legs[], notional_usd}|null,
                         confirmation{state NOT_REQUESTED|CONFIRMED, plan_digest_confirmed}, orders[], executions[], final_position{}|null, flat_verified bool|null,
                         basis NOT_RUN|EXCHANGE_REPORTED, net{fees_usd, funding_usd, price_pnl_usd, note}
  reconcile_report.json  "reconcile_report/v1"   findings[{code, detail, severity}], halt_new_exposure bool, views_compared[str]
  universe_scope.json    "universe_scope/v1"     scope verdict, eligible_perpetual_lifecycle_universe, lifecycle class counts (all optional)
  funnel.json            "funnel/v1"             steps [{name,count}] or counts{}, exclusion_reasons{} (all optional)
ONE COHERENT STATE: ``build_state`` reads every file once and derives every displayed money figure ONCE (``state["figures"]``: net = the replay's net_pnl_usd; each figure carries
``concept`` and ``src``). Every view reads those same values; the accounting identities (gross - fees = net, realised + unrealised = net, ...) are checked and shown, never assumed.
"""
from __future__ import annotations

import argparse
import errno
import json
import math
import os
import re
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import candidate_readiness as CR  # noqa: E402
import data_freshness_audit as DFA  # noqa: E402
import ms_types as MST  # noqa: E402
import owner_console as OC  # noqa: E402
import promotion_gate as PG  # noqa: E402
import universe_capture as UC  # noqa: E402
import venues as VEN  # noqa: E402                    # the registry of what a venue name means (pure; no client, no transport)

SCHEMA = "control_centre/v1"
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "control_centre_static")
STATIC_FILES = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8"}
DEFAULT_PORT = 8787
LOCAL_HOSTS = ("127.0.0.1", "localhost")
MAX_ARTIFACT_BYTES = 1_048_576
MAX_ITEMS = 2000
MAX_STR = 400
ARTIFACT_STALE_MS = 3 * 86_400_000
FUTURE_SKEW_MS = 5 * 60_000

BASIS_MEANING = {
    "MEASURED": "Observed in captured data or the owner account.",
    "EXCHANGE_REPORTED": "Stated by a venue response.",
    "CALCULATED": "Derived from measured inputs.",
    "ESTIMATED": "Produced by a model or an assumption; not observed.",
    "SIMULATED": "Replay or paper run; never realised cash.",
    "MISSING": "No source has this value. It is not zero and not fine.",
    "UNVERIFIED": "Self-consistent but not authenticated: bytes re-hash, but nothing proves the exchange produced them.",
    "OWNER_SUPPLIED_UNVERIFIED": "An owner import that has not been reconciled against the venue.",
}
BASES = tuple(BASIS_MEANING)
STATUS_MEANING = {"OK": "A checker returned a good result from real data.", "ATTENTION": "Something needs a look.", "FAIL": "Something is wrong.",
                  "NO_DATA": "Nothing has been captured or supplied. Not green.", "INFO": "A fact, not a verdict."}
PROV_TO_BASIS = {"MEASURED": "MEASURED", "VENUE_STATED": "EXCHANGE_REPORTED", "DERIVED": "CALCULATED", "MODELLED": "ESTIMATED", "ASSUMED": "ESTIMATED", "UNKNOWN": "MISSING"}
HEDGES = ("HEDGEABLE_ON_BYBIT", "PERP_ONLY", "HEDGE_UNKNOWN")
SEVERITIES = ("INFO", "WARN", "CRITICAL")
COMP_STATUSES = ("RESEARCHING", "REJECTED", "INSUFFICIENT_EVIDENCE", "SUFFICIENT_EVIDENCE")


def val(v: Any, basis: str, unit: Optional[str] = None, note: Optional[str] = None) -> Dict[str, Any]:
    """The one shape of a displayed value. A null value is MISSING whatever basis was asked for; an unknown basis is a bug."""
    if basis not in BASES:
        raise AssertionError(f"basis {basis!r} outside the vocabulary")
    return {"v": v, "basis": basis if v is not None else "MISSING", "unit": unit, "note": note}


# ======================================================================== artifacts
class ArtifactError(ValueError):
    pass


def _bad(msg: str) -> "ArtifactError":
    return ArtifactError(msg)


def _num(o: Mapping[str, Any], k: str, *, null: bool = True) -> Optional[float]:
    if k not in o:
        raise _bad(f"missing key {k!r}")
    x = o[k]
    if x is None:
        if null:
            return None
        raise _bad(f"{k!r} must be a number")
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or abs(x) > 1e15:
        raise _bad(f"{k!r} must be a finite number")
    return float(x)


def _int(o: Mapping[str, Any], k: str, *, null: bool = False) -> Optional[int]:
    if k not in o:
        raise _bad(f"missing key {k!r}")
    x = o[k]
    if x is None and null:
        return None
    if isinstance(x, bool) or not isinstance(x, int) or x < 0 or x > 10 ** 15:
        raise _bad(f"{k!r} must be a non-negative integer" + (" or null" if null else ""))
    return x


def _str(o: Mapping[str, Any], k: str, *, null: bool = False, nonempty: bool = True) -> Optional[str]:
    if k not in o:
        raise _bad(f"missing key {k!r}")
    x = o[k]
    if x is None and null:
        return None
    if not isinstance(x, str) or (nonempty and not x.strip()) or len(x) > MAX_STR or re.search(r"[\x00-\x08\x0b-\x1f\x7f]", x):
        raise _bad(f"{k!r} must be a short plain string" + (" or null" if null else ""))
    return x


def _enum(o: Mapping[str, Any], k: str, allowed: Sequence[str]) -> str:
    x = _str(o, k)
    if x not in allowed:
        raise _bad(f"{k!r} must be one of {list(allowed)}")
    return str(x)


def _list(o: Mapping[str, Any], k: str, *, required: bool = True) -> List[Any]:
    if k not in o:
        if required:
            raise _bad(f"missing key {k!r}")
        return []
    x = o[k]
    if not isinstance(x, list) or len(x) > MAX_ITEMS:
        raise _bad(f"{k!r} must be a list of at most {MAX_ITEMS}")
    return x


def _dicts(o: Mapping[str, Any], k: str) -> List[Mapping[str, Any]]:
    rows = _list(o, k)
    if not all(isinstance(r, dict) for r in rows):
        raise _bad(f"every entry of {k!r} must be an object")
    return rows


def _strs(o: Mapping[str, Any], k: str) -> List[str]:
    rows = _list(o, k)
    for r in rows:
        if not isinstance(r, str) or len(r) > MAX_STR or re.search(r"[\x00-\x08\x0b-\x1f\x7f]", r):
            raise _bad(f"every entry of {k!r} must be a short plain string")
    return list(rows)


def _bool(o: Mapping[str, Any], k: str) -> bool:
    if k not in o or not isinstance(o[k], bool):
        raise _bad(f"{k!r} must be true or false")
    return bool(o[k])


def _opp_label(kind: str, hedge: str) -> Dict[str, Any]:
    """Only a hedged spot-perp carry is called carry. A perp-only or unknown-hedge symbol is directional funding or a spread, never carry."""
    if kind == "SPOT_PERP_CARRY" and hedge == "HEDGEABLE_ON_BYBIT":
        return {"is_carry": True, "label": "Spot-perp carry (hedged)"}
    if kind == "SPOT_PERP_CARRY":
        return {"is_carry": False, "label": f"Not shown as carry: the spot hedge is not confirmed ({hedge})"}
    if kind == "PERP_FUNDING_SPREAD":
        return {"is_carry": False, "label": "Perp funding spread (not carry)"}
    return {"is_carry": False, "label": "Directional funding (not carry)"}


def _v_replay(o: Mapping[str, Any]) -> Dict[str, Any]:
    scope = _enum(o, "universe_scope", ("SURVIVORS_ONLY", "DELISTINGS_OBSERVED"))
    syms = []
    for r in _dicts(o, "symbols"):
        kind, hedge = _enum(r, "kind", MST.OPP_KINDS), _enum(r, "spot_hedge", HEDGES)
        syms.append({"symbol": _str(r, "symbol"), "kind": kind, "spot_hedge": hedge, **_opp_label(kind, hedge), "n_groups": _int(r, "n_groups"),
                     "gross_pnl_usd": _num(r, "gross_pnl_usd"), "fees_usd": _num(r, "fees_usd"), "net_pnl_usd": _num(r, "net_pnl_usd")})
    gbs = o.get("groups_by_state")
    if not isinstance(gbs, dict) or len(gbs) > 32 or not all(k in MST.GROUP_STATES and isinstance(v, int) and not isinstance(v, bool) and v >= 0 for k, v in gbs.items()):
        raise _bad("'groups_by_state' must map GroupState names to non-negative integers")
    return {"universe_scope": scope, "period_start_ms": _int(o, "period_start_ms", null=True), "period_end_ms": _int(o, "period_end_ms", null=True), "n_groups": _int(o, "n_groups"),
            "groups_by_state": dict(gbs), "gross_pnl_usd": _num(o, "gross_pnl_usd"), "fees_usd": _num(o, "fees_usd"), "net_pnl_usd": _num(o, "net_pnl_usd"), "symbols": syms,
            **{k: (_num(o, k) if k in o else None) for k in ("slippage_usd", "taker_slippage_usd", "adverse_selection_usd")}}      # optional modelled costs the producer states; absent = not stated


def _v_portfolio(o: Mapping[str, Any]) -> Dict[str, Any]:
    pos = []
    for r in _dicts(o, "positions"):
        kind, hedge = _enum(r, "kind", MST.OPP_KINDS), _enum(r, "spot_hedge", HEDGES)
        pos.append({"symbol": _str(r, "symbol"), "kind": kind, "spot_hedge": hedge, **_opp_label(kind, hedge), "side": _enum(r, "side", ("Buy", "Sell")),
                    "notional_usd": _num(r, "notional_usd", null=False), "entry_price": _num(r, "entry_price"), "mark_price": _num(r, "mark_price"),
                    "unrealised_pnl_usd": _num(r, "unrealised_pnl_usd"), "group_id": _str(r, "group_id", null=True)})
    return {"equity_usd": _num(o, "equity_usd"), "gross_exposure_usd": _num(o, "gross_exposure_usd", null=False), "net_exposure_usd": _num(o, "net_exposure_usd", null=False),
            "unrealised_pnl_usd": _num(o, "unrealised_pnl_usd"), "positions": pos}


def _v_risk(o: Mapping[str, Any]) -> Dict[str, Any]:
    limits = [{"name": _str(r, "name"), "unit": _str(r, "unit"), "limit": _num(r, "limit"), "value": _num(r, "value"), "status": _enum(r, "status", ("OK", "BREACH", "UNKNOWN"))}
              for r in _dicts(o, "limits")]
    alerts = [{"severity": _enum(r, "severity", SEVERITIES), "code": _str(r, "code"), "detail": _str(r, "detail", nonempty=False)} for r in _dicts(o, "alerts")]
    return {"halted": _bool(o, "halted"), "halt_reasons": _strs(o, "halt_reasons"), "limits": limits, "alerts": alerts}


def _v_ledger(o: Mapping[str, Any]) -> Dict[str, Any]:
    return {"period_start_ms": _int(o, "period_start_ms", null=True), "period_end_ms": _int(o, "period_end_ms", null=True), **{k: _num(o, k) for k in
            ("realised_gross_usd", "fees_usd", "funding_usd", "realised_net_usd", "unrealised_usd")},
            "net_usd": _num(o, "net_usd") if "net_usd" in o else None,          # optional: the TOTAL net as the producer states it (the replay's net_pnl_usd); never derived here
            **{k: (_num(o, k) if k in o else None) for k in ("taker_slippage_usd", "adverse_selection_usd", "exit_adjustment_usd")}}      # optional: modelled costs and the open-leg exit adjustment the producer states; absent / null = unknown (MISSING), never zero


def _v_competition(o: Mapping[str, Any]) -> Dict[str, Any]:
    out = []
    for r in _dicts(o, "candidates"):
        status = _enum(r, "status", COMP_STATUSES)
        reason = _str(r, "reason", null=True)
        if status in ("REJECTED", "INSUFFICIENT_EVIDENCE") and not (reason and reason.strip()):
            raise _bad(f"candidate {r.get('candidate_id')!r}: a {status} entry must carry a reason")
        kind, hedge = _enum(r, "kind", MST.OPP_KINDS), _enum(r, "spot_hedge", HEDGES)
        out.append({"candidate_id": _str(r, "candidate_id"), "kind": kind, "spot_hedge": hedge, **_opp_label(kind, hedge), "status": status, "reason": reason, "gross_bps_horizon": _num(r, "gross_bps_horizon"),
                    "net_bps_horizon": _num(r, "net_bps_horizon"), "n_observations": _int(r, "n_observations", null=True), "prov": _enum(r, "prov", MST.PROVS),
                    # Round 20: the producer's net is BEFORE the owner's cost of capital; the after-figure and the state of that input travel with it (absent in an older artifact: unknown, never zero)
                    "net_bps_after_cost_of_capital": _num(r, "net_bps_after_cost_of_capital") if "net_bps_after_cost_of_capital" in r else None,
                    "cost_of_capital_state": (r.get("cost_of_capital") or {}).get("state") if isinstance(r.get("cost_of_capital"), Mapping) and (r.get("cost_of_capital") or {}).get("state") in ("STATED", "UNSTATED") else None,
                    # Round 21: the CAPITAL the cost was charged on travels with it (an after-cost figure without its capital basis cannot be compared with the executor's); absent in an older artifact: not carried, never assumed
                    "capital_factor_used": _factor_range((r.get("cost_of_capital") or {}).get("capital_factor_used")) if isinstance(r.get("cost_of_capital"), Mapping) else None,
                    "capital_factor_basis": _short_text((r.get("cost_of_capital") or {}).get("capital_factor_basis")) if isinstance(r.get("cost_of_capital"), Mapping) else None})
    return {"candidates": out}


def _factor_range(x: Any) -> Optional[Dict[str, float]]:
    """A {min, max} pair of finite positive multiples of notional, or None (not carried / not a number: never coerced)."""
    if not isinstance(x, Mapping):
        return None
    lo, hi = x.get("min"), x.get("max")
    for v in (lo, hi):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            return None
    return {"min": float(lo), "max": float(hi)} if lo <= hi else None


def _short_text(x: Any, limit: int = 400) -> Optional[str]:
    return x[:limit] if isinstance(x, str) and x.strip() else None


def _coc_basis_note(c: Mapping[str, Any]) -> str:
    """What capital the cost of capital was charged on, stated beside every after-cost figure (Round 21)."""
    fu = c.get("capital_factor_used")
    if isinstance(fu, Mapping) and fu.get("min") is not None and fu.get("max") is not None:
        lo, hi = fu["min"], fu["max"]
        shown = f"{lo:.2f}x" if lo == hi else f"{lo:.2f}x to {hi:.2f}x"
        return f"charged on {shown} committed capital per unit of notional ({c.get('capital_factor_basis') or 'the basis text is not carried'})"
    return ("capital basis NOT carried by this artifact: an artifact written before Round 21 charged a 1.2x research basis, which is not comparable with the 2.04x the executor commits for the same carry")


def _v_opps(o: Mapping[str, Any]) -> Dict[str, Any]:
    items = []
    for r in _dicts(o, "items"):
        kind, hedge = _enum(r, "kind", MST.OPP_KINDS), _enum(r, "spot_hedge", HEDGES)
        items.append({"candidate_id": _str(r, "candidate_id"), "kind": kind, "symbols": _strs(r, "symbols"), "spot_hedge": hedge, **_opp_label(kind, hedge),
                      "hold_days": _num(r, "hold_days", null=False), "gross_bps_horizon": _num(r, "gross_bps_horizon"), "net_bps_horizon": _num(r, "net_bps_horizon"),
                      "capacity_usd": _num(r, "capacity_usd"), "prov": _enum(r, "prov", MST.PROVS), "exclusions": _strs(r, "exclusions")})
    return {"decision_ms": _int(o, "decision_ms"), "est_net_usd_per_day": _num(o, "est_net_usd_per_day"), "items": items}


OUTCOME_CODES = ("NO_OPPORTUNITY", "REJECTED_CANDIDATE", "INADEQUATE_HISTORY", "INADEQUATE_SAMPLE", "NO_ALLOCATION", "RISK_REFUSAL", "NO_FILL", "SIMULATED_EXECUTION", "UNKNOWN_OR_UNRECONCILED")
OUTCOME_TEXT = {
    "NO_OPPORTUNITY": ("No opportunity cleared the cost-aware screen.", "Nothing to do until a new scan finds a positive-net opportunity."),
    "REJECTED_CANDIDATE": ("The candidate was rejected on the evidence.", "Read the rejection reason; a rejected candidate is not retried without new evidence."),
    "INADEQUATE_HISTORY": ("The history does not cover the required span.", "Backfill the missing history (see the required span) and re-run; do not shorten the requirement."),
    "INADEQUATE_SAMPLE": ("There are too few observations to conclude anything.", "Collect more observations; a small sample is not evidence."),
    "NO_ALLOCATION": ("The allocator assigned no capital.", "Check why the allocator gave nothing (policy, risk, evidence)."),
    "RISK_REFUSAL": ("The risk gate refused.", "Read the refusal code in Risk; nothing proceeds until the cause is fixed."),
    "NO_FILL": ("Orders were sent but nothing filled.", "Inspect the order book and the order states in Execution before any retry."),
    "SIMULATED_EXECUTION": ("Execution happened in simulation only.", "A simulation is not cash: the next step is the owner-run testnet session, then prospective evidence."),
    "UNKNOWN_OR_UNRECONCILED": ("The outcome is unknown or unreconciled.", "Reconcile against the venue before reading any result."),
}
IDENT_KEYS = ("run_id", "mode", "candidate_id", "spec_digest", "data_digest", "code_revision")
RUN_FAMILY, TESTNET_FAMILY = "run", "testnet"


def _identity(o: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """None = the file carries no identity (LEGACY_NO_IDENTITY). A present but malformed identity is a refusal of the whole file."""
    if "identity" not in o:
        return None
    i = o["identity"]
    if not isinstance(i, dict):
        raise _bad("'identity' must be an object")
    out: Dict[str, Any] = {k: _str(i, k) for k in IDENT_KEYS}
    if out["mode"] not in MST.MODES:
        raise _bad(f"identity.mode must be one of {list(MST.MODES)}")
    out["generated_ms"] = _int(i, "generated_ms")
    return out


def _scalar_map(o: Mapping[str, Any], k: str, *, maxn: int = 40) -> Dict[str, Any]:
    """An optional {short key: scalar} object. Nested values are shown as compact text. Control characters refuse the file."""
    if k not in o or o[k] is None:
        return {}
    x = o[k]
    if not isinstance(x, dict) or len(x) > maxn:
        raise _bad(f"{k!r} must be an object of at most {maxn} entries")
    out: Dict[str, Any] = {}
    for kk, vv in x.items():
        if not isinstance(kk, str) or not kk or len(kk) > 64 or re.search(r"[\x00-\x08\x0b-\x1f\x7f]", kk):
            raise _bad(f"{k!r} has an invalid key")
        if vv is None or isinstance(vv, bool):
            out[kk] = vv
        elif isinstance(vv, (int, float)):
            if not math.isfinite(vv) or abs(vv) > 1e15:
                raise _bad(f"{k!r}.{kk[:20]} must be finite")
            out[kk] = vv
        elif isinstance(vv, str):
            if re.search(r"[\x00-\x08\x0b-\x1f\x7f]", vv):
                raise _bad(f"{k!r}.{kk[:20]} contains control characters")
            out[kk] = vv[:MAX_STR]
        else:
            out[kk] = json.dumps(vv, sort_keys=True, default=str)[:160]
    return out


def _v_pipeline(o: Mapping[str, Any]) -> Dict[str, Any]:
    st = o.get("status")
    code = o.get("outcome", o.get("outcome_code", st.get("primary") if isinstance(st, dict) else None))      # ms_pipeline writes status.primary
    if isinstance(code, dict):
        code = code.get("code")
    if code not in OUTCOME_CODES:
        raise _bad(f"'outcome' must be one of {list(OUTCOME_CODES)}")
    reason = o.get("reason")
    if reason is not None and (not isinstance(reason, str) or len(reason) > MAX_STR or re.search(r"[\x00-\x08\x0b-\x1f\x7f]", reason)):
        raise _bad("'reason' must be a short plain string or null")
    return {"outcome": code, "reason": reason, "required_span": _scalar_map(o, "required_span") or _scalar_map(o, "history"), "data_digest_parts": _scalar_map(o, "data_digest_parts") or _scalar_map(o, "data_parts"),
            "evidence_basis": _scalar_map(o, "evidence_basis")}


def _v_reconcile(o: Mapping[str, Any]) -> Dict[str, Any]:
    fnd = []
    for r in _dicts(o, "findings"):
        sev = _str(r, "severity") if "severity" in r else "ERROR"          # ms_reconcile findings carry no severity: any finding is a disagreement (active)
        fnd.append({"code": _str(r, "code"), "detail": _str(r, "detail", nonempty=False), "severity": str(sev).upper()[:24]})
    comp = o.get("compared")
    views = _strs(o, "views_compared") if "views_compared" in o else (sorted(str(k)[:64] for k in comp) if isinstance(comp, dict) else [])
    return {"findings": fnd, "halt_new_exposure": _bool(o, "halt_new_exposure"), "views_compared": views}


def _v_scope(o: Mapping[str, Any]) -> Dict[str, Any]:
    verdict = o.get("scope_verdict", o.get("verdict"))
    if verdict is not None and (not isinstance(verdict, str) or len(verdict) > MAX_STR or re.search(r"[\x00-\x08\x0b-\x1f\x7f]", verdict)):
        raise _bad("the scope verdict must be a short plain string")
    elig = o.get("eligible_perpetual_lifecycle_universe")
    if isinstance(elig, dict):
        elig_map = _scalar_map(o, "eligible_perpetual_lifecycle_universe")
    elif isinstance(elig, int) and not isinstance(elig, bool) and 0 <= elig <= 10 ** 9:
        elig_map = {"count": elig}
    elif elig is None:
        elig_map = {}
    else:
        raise _bad("'eligible_perpetual_lifecycle_universe' must be a count or an object")
    classes = _scalar_map(o, "lifecycle_class_counts") or _scalar_map(o, "lifecycle_classes")
    return {"scope_verdict": verdict, "eligible": elig_map, "lifecycle_classes": classes}


def _v_funnel(o: Mapping[str, Any]) -> Dict[str, Any]:
    steps: List[Dict[str, Any]] = []
    if isinstance(o.get("steps"), list):
        for r in o["steps"][:MAX_ITEMS]:
            if not isinstance(r, dict):
                raise _bad("every funnel step must be an object")
            name = r.get("name", r.get("step"))
            cnt = r.get("count")
            if not isinstance(name, str) or not name or len(name) > MAX_STR or isinstance(cnt, bool) or not isinstance(cnt, int) or cnt < 0:
                raise _bad("a funnel step needs a name and a non-negative integer count")
            steps.append({"name": name, "count": cnt})
    else:
        for kk, vv in _scalar_map(o, "counts").items():
            if isinstance(vv, bool) or not isinstance(vv, int) or vv < 0:
                raise _bad("funnel counts must be non-negative integers")
            steps.append({"name": kk, "count": vv})
    return {"steps": steps, "exclusion_reasons": _scalar_map(o, "exclusion_reasons")}


def _v_testnet(o: Mapping[str, Any]) -> Dict[str, Any]:
    if o.get("venue") != "Testnet":
        raise _bad("'venue' must be 'Testnet'")
    host = _str(o, "endpoint_host")
    if str(host).lower() != VEN.rest_hosts().get("testnet"):
        raise _bad("endpoint_host is not the registered testnet host (refused: this page never shows a non-testnet endpoint as a testnet session)")
    basis = _enum(o, "basis", ("NOT_RUN", "EXCHANGE_REPORTED"))
    cs = o.get("credential_state")
    if not isinstance(cs, dict):
        raise _bad("'credential_state' must be an object")
    cred = {k: _bool(cs, k) for k in ("dedicated_key_present", "dedicated_secret_present", "generic_credentials_ignored")}
    pf = o.get("preflight")
    if not isinstance(pf, dict):
        raise _bad("'preflight' must be an object")
    checks = [{"name": _str(c, "name"), "ok": _bool(c, "ok"), "detail": _str(c, "detail", nonempty=False)} for c in _dicts(pf, "checks")]
    preflight = {"status": _enum(pf, "status", ("NOT_RUN", "PASS", "FAIL")), "checked_ms": _int(pf, "checked_ms", null=True), "checks": checks}
    plan = o.get("plan")
    if plan is not None:
        if not isinstance(plan, dict):
            raise _bad("'plan' must be an object or null")
        legs = [{"symbol": _str(l, "symbol"), "side": _str(l, "side"), "order_type": _str(l, "order_type"), "qty": _num(l, "qty"), "price": _num(l, "price"), "notional_usd": _num(l, "notional_usd")}
                for l in _dicts(plan, "legs")]
        plan = {"digest": _str(plan, "digest"), "manifest_digest": _str(plan, "manifest_digest", null=True), "legs": legs, "notional_usd": _num(plan, "notional_usd")}
    cf = o.get("confirmation")
    if not isinstance(cf, dict):
        raise _bad("'confirmation' must be an object")
    conf = {"state": _enum(cf, "state", ("NOT_REQUESTED", "CONFIRMED")), "plan_digest_confirmed": _str(cf, "plan_digest_confirmed", null=True)}
    orders = [{"link_id": _str(r, "link_id"), "order_id": _str(r, "order_id", null=True), "symbol": _str(r, "symbol"), "side": _str(r, "side"), "qty": _num(r, "qty"),
               "status": _str(r, "status"), "filled_qty": _num(r, "filled_qty"), "avg_price": _num(r, "avg_price")} for r in _dicts(o, "orders")]
    execs = [{"exec_id": _str(r, "exec_id"), "link_id": _str(r, "link_id", null=True), "symbol": _str(r, "symbol"), "side": _str(r, "side"), "qty": _num(r, "qty"), "price": _num(r, "price"),
              "fee_usd": _num(r, "fee_usd"), "ts_ms": _int(r, "ts_ms", null=True)} for r in _dicts(o, "executions")]
    fp = o.get("final_position")
    if fp is not None:
        if not isinstance(fp, dict) or len(fp) > 200:
            raise _bad("'final_position' must be an object or null")
        fp = {_str({"k": k}, "k"): _num({"q": q}, "q", null=False) for k, q in fp.items()}
    fv = o.get("flat_verified")
    if fv is not None and not isinstance(fv, bool):
        raise _bad("'flat_verified' must be true, false or null")
    net = o.get("net")
    if not isinstance(net, dict):
        raise _bad("'net' must be an object")
    netv = {"fees_usd": _num(net, "fees_usd"), "funding_usd": _num(net, "funding_usd"), "price_pnl_usd": _num(net, "price_pnl_usd"), "note": _str(net, "note", null=True) if "note" in net else None}
    if basis == "NOT_RUN" and (orders or execs or fp or conf["state"] == "CONFIRMED"):         # a read-only preflight result and a plan may exist before any session: they are not orders
        raise _bad("basis NOT_RUN contradicts orders, executions, a position or a confirmation in the same file")
    if conf["state"] == "CONFIRMED" and plan is None:
        raise _bad("a confirmation without a plan")
    return {"venue": "Testnet", "endpoint_host": host, "basis": basis, "credential_state": cred, "preflight": preflight, "plan": plan, "confirmation": conf, "orders": orders,
            "executions": execs, "final_position": fp, "flat_verified": fv, "net": netv}


_ALL_OWNER = ("SIMULATED", "MEASURED", "EXCHANGE_REPORTED", "OWNER_SUPPLIED_UNVERIFIED")
#: file -> (schema, allowed bases or None when the file has no basis field, validator, family)
ARTIFACTS: Dict[str, Tuple[str, Optional[Tuple[str, ...]], Callable[[Mapping[str, Any]], Dict[str, Any]], str]] = {
    "replay_report.json": ("replay_report/v1", ("SIMULATED",), _v_replay, RUN_FAMILY),
    "portfolio_state.json": ("portfolio_state/v1", _ALL_OWNER, _v_portfolio, RUN_FAMILY),
    "risk_state.json": ("risk_state/v1", ("CALCULATED", "MEASURED", "SIMULATED"), _v_risk, RUN_FAMILY),
    "ledger_pnl.json": ("ledger_pnl/v1", _ALL_OWNER, _v_ledger, RUN_FAMILY),
    "competition.json": ("competition/v1", ("ESTIMATED", "SIMULATED", "CALCULATED"), _v_competition, RUN_FAMILY),
    "opportunities.json": ("opportunities/v1", ("ESTIMATED", "CALCULATED"), _v_opps, RUN_FAMILY),
    "pipeline_summary.json": ("pipeline_summary/v1", None, _v_pipeline, RUN_FAMILY),
    "reconcile_report.json": ("reconcile_report/v1", None, _v_reconcile, RUN_FAMILY),
    "universe_scope.json": ("universe_scope/v1", None, _v_scope, RUN_FAMILY),
    "funnel.json": ("funnel/v1", None, _v_funnel, RUN_FAMILY),
    "testnet_session.json": ("testnet_session/v1", ("NOT_RUN", "EXCHANGE_REPORTED"), _v_testnet, TESTNET_FAMILY),
}


def _reject_const(name: str) -> Any:
    raise ValueError(f"non-finite JSON constant {name}")


def _read_regular(path: str) -> bytes:
    """Open without following a link, confirm a regular file, read at most the cap (+1 to detect oversize)."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _bad("not a regular file")
        if st.st_size > MAX_ARTIFACT_BYTES:
            raise _bad(f"oversize ({st.st_size} bytes > {MAX_ARTIFACT_BYTES})")
        data = os.read(fd, MAX_ARTIFACT_BYTES + 1)
        if len(data) > MAX_ARTIFACT_BYTES:
            raise _bad("oversize")
        return data
    finally:
        os.close(fd)


def load_artifact(artifacts_dir: Optional[str], name: str, now_ms: int) -> Dict[str, Any]:
    """One artifact -> {file, state EMPTY|OK|ERROR, error, basis, generated_ms, age_ms, stale, identity, identity_status, family, data}. Never raises; never guesses a value."""
    if name not in ARTIFACTS:
        raise KeyError(name)
    schema, bases, validator, family = ARTIFACTS[name]
    res: Dict[str, Any] = {"file": name, "state": "EMPTY", "error": None, "basis": None, "generated_ms": None, "age_ms": None, "stale": False, "identity": None, "identity_status": None,
                           "family": family, "data": None}
    if not artifacts_dir or not os.path.isdir(artifacts_dir):
        return res
    path = os.path.join(artifacts_dir, name)
    try:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return res
        if stat.S_ISLNK(st.st_mode):
            raise _bad("a symbolic link is refused")
        if not stat.S_ISREG(st.st_mode):
            raise _bad("not a regular file")
        if os.path.dirname(os.path.realpath(path)) != os.path.realpath(artifacts_dir):
            raise _bad("resolves outside the artifacts directory")
        raw = _read_regular(path)
        obj = json.loads(raw.decode("utf-8", "strict"), parse_constant=_reject_const)
        if not isinstance(obj, dict):
            raise _bad("top level must be a JSON object")
        if obj.get("schema") != schema:
            raise _bad(f"schema must be {schema!r}")
        basis = obj.get("basis")
        if bases is not None and basis not in bases:
            raise _bad(f"basis must be one of {list(bases)} (got {str(basis)[:40]!r})")
        ident = _identity(obj)
        if "generated_ms" in obj:
            gen = _int(obj, "generated_ms")
        elif ident is not None:
            gen = ident["generated_ms"]
        else:
            raise _bad("missing key 'generated_ms'")
        for g in (gen, ident["generated_ms"] if ident else None):
            if g is not None and g > now_ms + FUTURE_SKEW_MS:
                raise _bad("generated_ms is in the future")
        data = validator(obj)
        res.update(state="OK", basis=basis if isinstance(basis, str) else None, generated_ms=gen, age_ms=now_ms - gen, stale=(now_ms - gen) > ARTIFACT_STALE_MS, data=data, identity=ident,
                   identity_status="IDENTIFIED" if ident else "LEGACY_NO_IDENTITY")
    except OSError as exc:
        res.update(state="ERROR", error=("a symbolic link is refused" if exc.errno == errno.ELOOP else f"unreadable ({type(exc).__name__})"))
    except (ArtifactError, ValueError, RecursionError, UnicodeDecodeError) as exc:
        res.update(state="ERROR", error=str(exc)[:240], data=None, basis=None)
    return res


# ======================================================================== sections
def _safe(fn: Callable[[], Dict[str, Any]], what: str) -> Dict[str, Any]:
    try:
        return fn()
    except Exception as exc:                                         # noqa: BLE001 - one broken source must show as an ERROR section, not take the page down
        return {"state": "ERROR", "error": f"{what}: {type(exc).__name__}: {str(exc)[:160]}"}


def _gate() -> Dict[str, Any]:
    try:
        v = PG.evaluate_promotion_gate()
        items = [{"key": i.key, "complete": bool(i.complete), "owner": i.owner} for i in v.items]
        return {"decision": "ALLOW" if v.allows_live else "REFUSE", "allows_live": bool(v.allows_live), "complete": sum(1 for i in items if i["complete"]), "total": len(items),
                "open_items": [i["key"] for i in items if not i["complete"]][:12], "reasons": [str(r)[:200] for r in (getattr(v, "reasons", None) or [])][:6]}
    except Exception as exc:                                         # noqa: BLE001 - an unreadable gate is a closed gate
        return {"decision": "REFUSE", "allows_live": False, "complete": 0, "total": 0, "open_items": [], "reasons": [f"gate unreadable ({type(exc).__name__}): refuses"]}


def _actions(oc: Mapping[str, Any], gate: Mapping[str, Any]) -> Dict[str, Any]:
    env, elig = oc["environment"], oc["eligibility"]
    armed = bool(env.get("live_authorized"))
    acts: List[Dict[str, Any]] = []
    acts.append({"id": "research", "label": "Read-only research on captured data", "permitted": bool(elig["research"]["met"]),
                 "reasons": [elig["research"]["meaning"]] if elig["research"]["met"] else ["eligibility.research is not met"]})
    paper_ok = bool(elig["research"]["met"]) and not armed
    acts.append({"id": "replay", "label": "Paper replay (offline simulation, SIMULATED results)", "permitted": paper_ok,
                 "reasons": ["no venue access; results are labelled SIMULATED and are never realised cash"] if paper_ok else
                            (["the environment reports real money ARMED: stop and check it is intended"] if armed else ["research eligibility is not met"])})
    tn = elig["testnet"]
    tn_reasons = [] if tn["met"] else [f"{tn['meaning']} (status {tn['status']})", f"offline testnet qualification: {oc['testnet'].get('offline_qualification')}"]
    acts.append({"id": "testnet", "label": "Testnet session", "permitted": bool(tn["met"]), "reasons": tn_reasons or ["owner-run testnet session recorded"]})
    live_reasons: List[str] = []
    if not armed:
        live_reasons.append(f"not armed: {env.get('reason')}")
    if not gate["allows_live"]:
        live_reasons.append(f"promotion gate {gate['decision']}: {gate['complete']} of {gate['total']} checklist items complete" + (f"; first open: {gate['open_items'][0]}" if gate["open_items"] else ""))
    live_ok = bool(elig["owner_authorised"]["met"]) and armed and bool(gate["allows_live"])
    acts.append({"id": "mainnet", "label": "Mainnet (real money)", "permitted": live_ok,
                 "reasons": live_reasons or ["armed by the owner AND the promotion gate allows (verify independently before relying on this display)"]})
    return {"items": acts, "note": "This page cannot start, change or stop anything. It lists what the repository's own checks permit right now."}


def _research(cand_rows: Sequence[Mapping[str, Any]], comp: Dict[str, Any], opps: Dict[str, Any], econ_loaded: bool, gate: Mapping[str, Any]) -> Dict[str, Any]:
    rows, rejected = [], []
    for r in cand_rows:
        ladder, ready = str(r.get("ladder_verdict")), str(r.get("readiness_verdict"))
        ex = (r.get("executable_on_qualified_venues") or {})
        pit = (r.get("point_in_time") or {}).get("value")
        data_present = (r.get("required_data") or {}).get("value", "MISSING")
        if ladder == "OWNER_PROFIT_EVIDENCED":
            stage = "OWNER_PROFIT_EVIDENCED"
        elif ladder == "QUALIFIED" and ex.get("executable"):
            stage = "EXECUTION_READY"
        elif ladder in ("PROSPECTIVE", "PROMISING"):
            stage = ladder
        elif str(data_present).startswith("PRESENT") and pit in ("VERIFIED_RAW_RESPONSES", "NOT_APPLICABLE_SINGLE_INSTRUMENT"):
            stage = "RESEARCH_READY"
        elif str(data_present).startswith("PRESENT"):
            stage = "DATA_READY"
        else:
            stage = "NOT_READY"
        is_rej = ladder == "REJECTED" or ready == "REJECTED"
        policies = ((r.get("edge_vs_costs") or {}).get("policies") or {}) if isinstance(r.get("edge_vs_costs"), dict) else {}
        why = [str(x)[:260] for x in (list(r.get("readiness_reasons") or []) + list(r.get("ladder_reasons") or []))][:8]
        blockers = [f"Verdict ladder: {ladder} (QUALIFIED needs prospective evidence; OWNER_PROFIT_EVIDENCED needs the owner's attestation)",
                    f"Executable on qualified venues: {ex.get('value')}", f"Promotion gate: {gate['decision']}"]
        row = {"candidate": str(r.get("candidate")), "name": str(r.get("name")), "stage": stage, "ladder_verdict": ladder, "readiness_verdict": ready, "rejected": bool(is_rej),
               "evidence_sufficient": ladder in ("QUALIFIED", "OWNER_PROFIT_EVIDENCED"), "authorised": False, "required_data": str(data_present),
               "point_in_time": str(pit), "executable": str(ex.get("value")), "edge_vs_costs": val(None, "MISSING", note=str((r.get("edge_vs_costs") or {}).get("reason") or "no measurement")[:260])
               if not policies else val(str(r["edge_vs_costs"].get("value"))[:120], "CALCULATED", note="in-sample on the committed corpus; not an out-of-sample result"),
               "why_not_authorised": why + blockers, "policies": {str(k): str(v) for k, v in policies.items()}}
        rows.append(row)
        for pk, pv in policies.items():
            if str(pv) == "REJECTED":
                rejected.append({"candidate": row["candidate"], "what": f"policy {pk}", "why": "rejected by the corpus economics (in-sample)", "basis": "CALCULATED"})
        if is_rej:
            rejected.append({"candidate": row["candidate"], "what": "candidate", "why": "; ".join(why[:2]) or ladder, "basis": "CALCULATED"})
    for c in (comp.get("data") or {}).get("candidates", []):
        if c["status"] == "REJECTED":
            rejected.append({"candidate": c["candidate_id"], "what": "competition entry", "why": c["reason"], "basis": comp["basis"]})
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r["stage"]] = counts.get(r["stage"], 0) + 1
    out: Dict[str, Any] = {"state": "OK" if rows else "NO_DATA", "candidates": rows, "stage_counts": counts, "rejected": rejected,
                           "sufficient_evidence": [r["candidate"] for r in rows if r["evidence_sufficient"]] +
                                                  [c["candidate_id"] for c in (comp.get("data") or {}).get("candidates", []) if c["status"] == "SUFFICIENT_EVIDENCE"],
                           "economics_loaded": econ_loaded,
                           "note": "Sufficient evidence is not authorisation: authorisation is the owner's act (config.is_live_authorized AND the signed promotion gate)."
                                   + ("" if econ_loaded else " In-sample corpus economics are not loaded (slow); start with --with-economics to include rejection reasons from them.")}
    out["competition"] = _artifact_view(comp, lambda d: {"candidates": [{**c, "gross": val(c["gross_bps_horizon"], PROV_TO_BASIS[c["prov"]], "bps"), "net": val(c["net_bps_horizon"], PROV_TO_BASIS[c["prov"]], "bps", "per closed group, BEFORE the owner's cost of capital (" + ("stated: see net_after_cost_of_capital" if c.get("cost_of_capital_state") == "STATED" else "UNSTATED or not carried by this artifact: no after-cost figure exists") + "); the multi-symbol pipeline uses the ledger's net (after fees, taker slippage and adverse selection)"),
                                                                       "net_after_cost_of_capital": val(c.get("net_bps_after_cost_of_capital"), PROV_TO_BASIS[c["prov"]], "bps", "per closed group, after the stated cost of capital, " + _coc_basis_note(c) + "; unknown when the rate is unstated"),
                                                                       }
                                                                      for c in d["candidates"]]},
                                      empty="No competition result yet. Run the candidate competition CLI to write competition.json into the artifacts directory.")
    out["opportunities"] = _artifact_view(opps, _opp_view, empty="No opportunity scan yet. Run the opportunity scanner to write opportunities.json into the artifacts directory.")
    return out


def _opp_view(d: Mapping[str, Any]) -> Dict[str, Any]:
    items = []
    for it in d["items"]:
        b = PROV_TO_BASIS[it["prov"]]
        items.append({**{k: it[k] for k in ("candidate_id", "kind", "symbols", "spot_hedge", "is_carry", "label", "exclusions")}, "hold_days": val(it["hold_days"], "ESTIMATED", "days"),
                      "gross": val(it["gross_bps_horizon"], b, "bps", "before costs"), "net": val(it["net_bps_horizon"], b, "bps", "after the costs the scanner knows; not computed from gross here"),
                      "capacity": val(it["capacity_usd"], b, "USD"), "eligible": (not it["exclusions"]) and it["net_bps_horizon"] is not None})
    elig = sorted([i for i in items if i["eligible"]], key=lambda i: -i["net"]["v"])
    for rank, i in enumerate(elig, 1):
        i["rank"] = rank                                           # 1 = best known net; an item with an unknown net has no rank (it is never ranked by its gross)
    for i in items:
        i.setdefault("rank", None)
    items = elig + [i for i in items if not i["eligible"]]
    pos = [i for i in elig if i["net"]["v"] > 0]
    return {"items": items, "n_items": len(items), "n_eligible": len(elig), "n_positive_net": len(pos),
            "best_net": val(max((i["net"]["v"] for i in elig), default=None), "ESTIMATED", "bps", "best eligible item; an upper bound, not a forecast"),
            "est_net_usd_per_day": val(d["est_net_usd_per_day"], "ESTIMATED", "USD/day", "as stated by the scanner; a model output; its financing is charged on the scanner's committed-capital factor (the registry's: 2.04x for a carry at leverage 1) when a rate was stated, and an opportunities.json written before Round 21 used a 1.2x research basis")}


def _artifact_view(a: Dict[str, Any], view: Callable[[Dict[str, Any]], Dict[str, Any]], *, empty: str) -> Dict[str, Any]:
    base = {"file": a["file"], "age_ms": a["age_ms"], "stale": a["stale"], "generated_ms": a["generated_ms"], "basis": a["basis"]}
    if a["state"] == "EMPTY":
        return {**base, "state": "NO_DATA", "empty": empty}
    if a["state"] == "ERROR":
        return {**base, "state": "ERROR", "error": f"{a['file']}: {a['error']}"}
    return {**base, "state": "OK", **view(a["data"])}


def _pnl_view(a: Dict[str, Any], F: Mapping[str, Any]) -> Dict[str, Any]:
    """The ledger's view. Every money value is the SAME object ``figures`` holds (one source per concept); this function only places them."""
    base = {"file": a["file"], "age_ms": a["age_ms"], "stale": a["stale"], "generated_ms": a["generated_ms"], "basis": a["basis"]}
    if a["state"] == "EMPTY":
        return {**base, "state": "NO_DATA", "empty": "No P&L ledger yet. Run the accounting CLI to write ledger_pnl.json, or import an owner export with tools/owner_ledger.py."}
    if a["state"] == "ERROR":
        return {**base, "state": "ERROR", "error": f"{a['file']}: {a['error']}"}
    b = a["basis"]
    simulated = b == "SIMULATED"
    # A simulated ledger fills the 'simulated_*' group and leaves 'realised_*' EMPTY: simulated is never realised. Gross and net are separate fields; net is never derived here.
    grp = "simulated" if simulated else "realised"
    other = "realised" if simulated else "simulated"
    return {**base, "state": "OK", "kind": "SIMULATED_NOT_REALISED" if simulated else ("OWNER_IMPORT_UNVERIFIED" if b == "OWNER_SUPPLIED_UNVERIFIED" else "ACCOUNT_REPORTED_UNRECONCILED"),
            f"{grp}_gross": F["closed_gross"], f"{grp}_fees": F["fees"], f"{grp}_funding": F["funding"], f"{grp}_net": F["closed_net"],
            f"{other}_gross": val(None, "MISSING"), f"{other}_net": val(None, "MISSING"), "unrealised": F["unrealised"], "total_net": F["net"],
            "period": [a["data"]["period_start_ms"], a["data"]["period_end_ms"]]}


def _portfolio(a: Dict[str, Any], ledger: Dict[str, Any], F: Mapping[str, Any]) -> Dict[str, Any]:
    def view(d: Mapping[str, Any]) -> Dict[str, Any]:
        bs = a["basis"]
        pos = [{**{k: p[k] for k in ("symbol", "kind", "spot_hedge", "is_carry", "label", "side", "group_id")}, "notional": val(p["notional_usd"], bs, "USD"),
                "entry_price": val(p["entry_price"], bs, "USD"), "mark_price": val(p["mark_price"], bs, "USD"), "unrealised": val(p["unrealised_pnl_usd"], bs, "USD")} for p in d["positions"]]
        gross = d["gross_exposure_usd"]
        big = max(d["positions"], key=lambda p: abs(p["notional_usd"]), default=None)
        lev = (gross / d["equity_usd"]) if (d["equity_usd"] and d["equity_usd"] > 0) else None
        conc = (abs(big["notional_usd"]) / gross) if (big is not None and gross and gross > 0) else None
        no_field = "portfolio_state/v1 has no such field"
        return {"equity": val(d["equity_usd"], bs, "USD"), "gross_exposure": val(d["gross_exposure_usd"], bs, "USD"), "net_exposure": val(d["net_exposure_usd"], bs, "USD"),
                "unrealised": F["unrealised"], "positions": pos, "n_positions": len(pos), "simulated": bs == "SIMULATED",
                "available_balance": val(None, "MISSING", note=no_field), "reserved_capital": val(None, "MISSING", note=no_field), "margin": val(None, "MISSING", note=no_field),
                "drawdown": val(None, "MISSING", note=no_field + "; a drawdown limit, if any, is in Risk"),
                "leverage": val(lev, "CALCULATED", "x", "gross exposure / equity, both from this file; not a margin figure"),
                "concentration": val(None if conc is None else round(conc * 100, 2), "CALCULATED", "% of gross", None if big is None else "largest position: " + big["symbol"])}
    out = _artifact_view(a, view, empty="No portfolio state yet. Run the portfolio CLI to write portfolio_state.json into the artifacts directory. Nothing is held that this page can see.")
    out["pnl"] = _pnl_view(ledger, F)
    return out


def _execution(replay: Dict[str, Any], oc: Mapping[str, Any], F: Mapping[str, Any]) -> Dict[str, Any]:
    def view(d: Mapping[str, Any]) -> Dict[str, Any]:
        sym = [{**{k: s[k] for k in ("symbol", "kind", "spot_hedge", "is_carry", "label", "n_groups")}, "gross": val(s["gross_pnl_usd"], "SIMULATED", "USD", "before fees; GROSS"),
                "fees": val(s["fees_usd"], "SIMULATED", "USD"), "net": val(s["net_pnl_usd"], "SIMULATED", "USD", "after fees; NET")} for s in d["symbols"]]
        unhedged = d["groups_by_state"].get("UNHEDGED", 0) + d["groups_by_state"].get("UNKNOWN_OUTCOME", 0)
        return {"universe_scope": d["universe_scope"], "n_groups": d["n_groups"], "groups_by_state": d["groups_by_state"], "unhedged_or_unknown": unhedged,
                "gross": F["gross"], "fees": F["fees"], "net": F["net"], "period": [d["period_start_ms"], d["period_end_ms"]], "symbols": sym,
                "order_level": val(None, "MISSING", note="replay_report/v1 carries order-group states only: no orders, acks, fills, cancels or rejections per order")}
    out = _artifact_view(replay, view, empty="No replay yet. Run the replay CLI to write replay_report.json into the artifacts directory.")
    out["mode"] = "REPLAY / PAPER ONLY"
    out["testnet_offline_qualification"] = val(oc["testnet"].get("offline_qualification"), "CALCULATED", note="guard probes only; it reads nothing from a venue")
    out["note"] = "No order can be sent from this page, and the replay layer is wired to simulated transports only."
    return out


def _risk(rs: Dict[str, Any], oc: Mapping[str, Any]) -> Dict[str, Any]:
    def view(d: Mapping[str, Any]) -> Dict[str, Any]:
        b = rs["basis"]
        return {"halted": d["halted"], "halt_reasons": d["halt_reasons"], "alerts": d["alerts"],
                "limits": [{**{k: x[k] for k in ("name", "unit", "status")}, "limit": val(x["limit"], b, x["unit"]), "value": val(x["value"], b, x["unit"])} for x in d["limits"]]}
    out = _artifact_view(rs, view, empty="No risk state yet. Run the risk CLI to write risk_state.json into the artifacts directory. Until then nothing says the book is within limits.")
    r = oc["risk"]
    out["policy"] = {"active": bool(r.get("policy_active")), "status": r.get("status"), "basis": "ESTIMATED",
                     "text": ("A risk policy is PROPOSED from data, not owner-approved: with no approved policy every entry is refused." if not r.get("policy_active") else "A risk policy is active."),
                     "refusals_without_policy": [str(x)[:200] for x in (r.get("evaluation_without_an_approved_policy") or [])][:4],
                     "proposed_max_hold_days": val(r.get("proposed_max_hold_days"), "ESTIMATED", "days", "proposal only"),
                     "proposed_drawdown_bps_of_notional": val(r.get("proposed_strategy_drawdown_bps_balanced"), "ESTIMATED", "bps", "proposal only"),
                     "unmeasured_limits": [str(x)[:160] for x in (r.get("unmeasured_limits") or [])][:8] if isinstance(r.get("unmeasured_limits"), list) else []}
    return out


def _freshness(data_root: Optional[str], universe_dir: Optional[str], now_ms: int, verify: bool) -> Dict[str, Any]:
    if not data_root:
        return {"state": "NO_DATA", "verdict": "NO_DATA", "empty": "No data root configured. Start with --data-root DIR (the private market-data root written by tools/market_collector.py)."}
    if not os.path.isdir(data_root):
        return {"state": "NO_DATA", "verdict": "NO_DATA", "empty": "The data root does not exist yet. Run tools/market_collector.py run-continuous --out-root DIR to capture data."}
    try:
        rep = DFA.audit(data_root, now_ms=now_ms, universe_capture=universe_dir if universe_dir and os.path.isdir(universe_dir) else None, verify=verify)
    except (ValueError, OSError) as exc:
        return {"state": "ERROR", "verdict": "ERROR", "error": f"data_freshness_audit refused: {str(exc)[:200]}"}
    ax = rep["axes"]
    # Timestamps are MEASURED only if this run re-hashed the datasets; otherwise they are what the unauthenticated manifest says (UNVERIFIED).
    tsb = "MEASURED" if verify else "UNVERIFIED"
    cyc = rep["cycles"].get("latest_complete_cycle")
    hist = []
    for name, h in rep["history"].items():
        newest = [s["latest_observed_ts"] for s in h["symbols"].values() if isinstance(s.get("latest_observed_ts"), int)]
        hist.append({"dataset": name, "status": str(h.get("dataset_status")), "verify": h.get("verify"), "n_symbols": len(h["symbols"]), "stale_symbols": len(h["stale_symbols"]),
                     "incomplete_symbols": len(h["incomplete_symbols"]), "newest_ms": val(max(newest, default=None), tsb, "ms"), "age_ms": val(None if not newest else now_ms - max(newest), tsb, "ms")})
    issues = [{"code": str(i.get("code")), "level": str(i.get("level")), "detail": str(i.get("detail"))[:240]} for i in ax["data_freshness"]["issues"]][:20]
    return {"state": "OK" if ax["data_freshness"]["verdict"] != "NO_DATA" else "NO_DATA", "verdict": ax["data_freshness"]["verdict"], "readiness": ax["research_readiness"]["verdict"],
            "code_correctness": ax["code_correctness"]["verdict"], "issues": issues, "readiness_issues": [str(i.get("detail") or i.get("code"))[:200] for i in ax["research_readiness"]["issues"]][:10],
            "latest_cycle": None if not cyc else {"cycle": str(cyc["cycle"]), "age_ms": val(cyc["age_ms"], tsb, "ms")}, "datasets": hist, "verified_this_run": bool(verify),
            "backups": {k: (None if rep["backups"].get(k) is None else True) for k in ("backup", "restore_check", "seal")},
            "collector": _collector(rep.get("collector_health")),
            "status_partitions": {"present": list(rep["status_partitions"].get("present") or []), "missing": list(rep["status_partitions"].get("missing") or [])},
            "note": "Three separate axes; they are never merged into one light. Code correctness is not assessed here."}


def _collector(h: Any) -> Dict[str, Any]:
    """health.json is unauthenticated: whatever it says is UNVERIFIED here (the audit's own verdict is what turns green)."""
    if not isinstance(h, dict):
        return {"state": "NO_DATA", "empty": "No health.json was found (the collector has not reported, or is not running)."}
    cf = h.get("consecutive_failures")
    return {"state": "OK", "status": val(str(h.get("status"))[:40], "UNVERIFIED", note="from health.json, an unauthenticated file"),
            "consecutive_failures": val(cf if isinstance(cf, int) and not isinstance(cf, bool) else None, "UNVERIFIED"),
            "interval_ms": val(h.get("interval_ms") if isinstance(h.get("interval_ms"), int) and not isinstance(h.get("interval_ms"), bool) else None, "UNVERIFIED", "ms")}


def _capture(universe_dir: Optional[str]) -> Dict[str, Any]:
    if not universe_dir:
        return {"state": "NO_DATA", "empty": "No universe capture configured. Start with --universe-dir DIR (a schema-v2 capture from tools/universe_capture.py)."}
    if not os.path.isdir(universe_dir):
        return {"state": "NO_DATA", "empty": "The universe capture directory does not exist. Run tools/universe_capture.py to take one."}
    rep = UC.verify_capture_dir(universe_dir)
    cov = rep.get("coverage") or {}
    try:
        surv, surv_note = CR._survivorship_info({"status_coverage": cov.get("status_coverage"), "delisting_observability": cov.get("delisting_observability")})
    except Exception:                                                # noqa: BLE001
        surv, surv_note = CR.SURVIVORS_ONLY, "survivorship could not be derived: treated as survivors only"
    ok = bool(rep.get("ok"))
    if ok:
        integrity = val("RAW_RESPONSES_VERIFIED", "UNVERIFIED", note=CR.PIT_LABEL_NOTE[:300])
    else:
        integrity = val(None, "MISSING", note="verification failed: " + "; ".join(str(p) for p in (rep.get("problems") or [])[:3])[:300])
    return {"state": "OK" if ok else "ERROR", "ok": ok, "integrity": integrity, "dataset_status": str(cov.get("dataset_status")),
            "manifest_sha256": val(rep.get("manifest_sha256") if ok else None, "CALCULATED", note="hash of the manifest bytes read now; identifies them, does not authenticate them"),
            "survivorship": surv if ok else CR.SURVIVORS_ONLY, "survivorship_note": surv_note[:300], "problems": [str(p)[:200] for p in (rep.get("problems") or [])[:8]],
            "error": None if ok else "capture verification failed; no value from it is trusted"}


def _stamp(a: Mapping[str, Any]) -> Dict[str, Any]:
    ident = a.get("identity") or {}
    return {"file": a["file"], "state": "NO_DATA" if a["state"] == "EMPTY" else a["state"], "error": a["error"], "basis": a["basis"], "age_ms": a["age_ms"], "stale": a["stale"],
            "identity_status": a.get("identity_status"), "run_id": ident.get("run_id"), "mode": ident.get("mode"), "family": a.get("family")}


# ======================================================================== coherence, figures, accounting
TOL = 0.0101
FAMILY_FIELDS = ("run_id", "candidate_id", "data_digest", "mode")


def _coherence(art: Mapping[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Do the loaded artifacts belong to ONE run? The simulated family must agree on run_id, candidate_id, data_digest and mode; testnet_session.json must say TESTNET and is never combined
    with the family. 'UNKNOWN' (the producer did not know) is unverified, not a match and not a mismatch."""
    runs: List[Dict[str, Any]] = []
    for name, a in art.items():
        if a["state"] != "OK":
            continue
        ident = a["identity"]
        runs.append({"file": name, "family": a["family"], "status": a["identity_status"], **({k: ident[k] for k in (*IDENT_KEYS, "generated_ms")} if ident else {})})
    fam = [r for r in runs if r["family"] == RUN_FAMILY]
    idf = [r for r in fam if r["status"] == "IDENTIFIED"]
    mism: List[Dict[str, Any]] = []
    for f in FAMILY_FIELDS:
        vals = {r[f] for r in idf if r[f] != "UNKNOWN"}
        if len(vals) > 1:
            mism.append({"field": f, "values": {r["file"]: r[f] for r in idf}})
    for r in idf:
        if r["mode"] == "TESTNET" and art[r["file"]]["basis"] == "SIMULATED":                    # a SIMULATED figure may never belong to a TESTNET run
            mism.append({"field": "mode", "values": {r["file"]: "TESTNET"}, "why": "a simulated-family artifact may not claim mode TESTNET"})
    for r in runs:
        if r["family"] == TESTNET_FAMILY and r["status"] == "IDENTIFIED" and r["mode"] != "TESTNET":
            mism.append({"field": "mode", "values": {r["file"]: r["mode"]}, "why": "testnet_session.json must carry identity.mode TESTNET"})
    legacy = [r["file"] for r in runs if r["status"] == "LEGACY_NO_IDENTITY"]
    unknown = sorted({f"{r['file']}:{k}" for r in runs if r["status"] == "IDENTIFIED" for k in ("run_id", "candidate_id", "data_digest", "code_revision", "spec_digest") if r[k] == "UNKNOWN"})
    if not runs:
        verdict, banner = "NO_ARTIFACTS", "No artifact is loaded, so there is no run to identify."
    elif mism:
        verdict = "INCONSISTENT_RUN"
        banner = ("INCONSISTENT_RUN: the artifacts do not belong to one run (" + "; ".join(sorted({m["field"] for m in mism})) + " differ). No figure that needs two files is combined or shown until they match.")
    elif legacy or unknown:
        verdict = "UNVERIFIED"
        banner = ("UNVERIFIED: " + (f"{len(legacy)} file(s) carry no run identity ({', '.join(legacy[:4])}); " if legacy else "") +
                  (f"unknown identity fields ({', '.join(unknown[:4])}); " if unknown else "") + "nothing proves these files belong together.")
    else:
        verdict, banner = "CONSISTENT", "Every loaded artifact carries a run identity and they agree."
    primary = None
    if verdict in ("CONSISTENT", "UNVERIFIED") and idf:
        primary = {k: idf[0][k] for k in (*IDENT_KEYS, "generated_ms")}
    revs = sorted({r["code_revision"] for r in runs if r["status"] == "IDENTIFIED"})
    return {"verdict": verdict, "banner": banner, "runs": runs, "mismatches": mism, "legacy_files": legacy, "unknown_fields": unknown, "primary": primary,
            "code_revisions": revs, "n_compared": len(idf), "combined_numbers_withheld": verdict == "INCONSISTENT_RUN",
            "note": "Identity is self-declared by unauthenticated local files: agreement shows files were written together, not that they are true."}


def _fig(concept: str, v: Optional[float], basis: str, src: Optional[str], note: Optional[str] = None) -> Dict[str, Any]:
    d = val(v, basis, "USD", note)
    d["concept"] = concept
    d["src"] = src if d["v"] is not None else None
    return d


def _figures(art: Mapping[str, Dict[str, Any]], coh: Mapping[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """THE money figures of the page: one value and one source field per concept. Every view (and every sentence) reads these; none recomputes."""
    withheld = bool(coh["combined_numbers_withheld"])
    rp, lg, pf = art["replay_report.json"], art["ledger_pnl.json"], art["portfolio_state.json"]
    R = rp["data"] if rp["state"] == "OK" else {}
    L = lg["data"] if lg["state"] == "OK" else {}
    P = pf["data"] if pf["state"] == "OK" else {}

    def pick(concept: str, cands: Sequence[Tuple[Any, Optional[str], str]], note: str) -> Dict[str, Any]:
        if withheld:
            return _fig(concept, None, "MISSING", None, "withheld: INCONSISTENT_RUN (the artifacts are not from one run)")
        for v, b, src in cands:
            if v is not None and b:
                return _fig(concept, v, b, src, note)
        return _fig(concept, None, "MISSING", None, note + "; no artifact states it")
    lb, pb = lg["basis"], pf["basis"]
    nobackend = "no backend artifact carries this yet"
    F = {
        "net": pick("net", [(R.get("net_pnl_usd"), "SIMULATED", "replay_report.json:net_pnl_usd"), (L.get("net_usd"), lb, "ledger_pnl.json:net_usd")], "NET: after fees; the one net of this page (total = realised + unrealised)"),
        "gross": pick("gross", [(R.get("gross_pnl_usd"), "SIMULATED", "replay_report.json:gross_pnl_usd")], "GROSS: before fees"),
        "fees": pick("fees", [(R.get("fees_usd"), "SIMULATED", "replay_report.json:fees_usd"), (L.get("fees_usd"), lb, "ledger_pnl.json:fees_usd")], "fees paid or modelled"),
        "funding": pick("funding", [(L.get("funding_usd"), lb, "ledger_pnl.json:funding_usd")], "funding received less paid, as the ledger states it"),
        "closed_gross": pick("closed_gross", [(L.get("realised_gross_usd"), lb, "ledger_pnl.json:realised_gross_usd")], "realised PRICE result at mid, before funding, fees and modelled slippage; GROSS (funding is its own line)"),
        "closed_net": pick("closed_net", [(L.get("realised_net_usd"), lb, "ledger_pnl.json:realised_net_usd")], "realised after fees and funding; NET of the realised part"),
        "unrealised": pick("unrealised", [(L.get("unrealised_usd"), lb, "ledger_pnl.json:unrealised_usd")] + ([] if lg["state"] == "OK" else [(P.get("unrealised_pnl_usd"), pb, "portfolio_state.json:unrealised_pnl_usd")]), "mark-to-market; not cash"),
        "slippage": pick("slippage", [(L.get("taker_slippage_usd"), lb, "ledger_pnl.json:taker_slippage_usd")], "modelled taker walk of the book (inside the fill prices, charged once)"),
        "hedge_cost": pick("hedge_cost", [], nobackend),
        "adverse_selection": pick("adverse_selection", [(L.get("adverse_selection_usd"), lb, "ledger_pnl.json:adverse_selection_usd")], "modelled post-fill markout"),
    }
    checks: List[Dict[str, Any]] = []

    def chk(cid: str, label: str, lhs: Optional[float], rhs: Optional[float], detail: str) -> None:
        if withheld:
            checks.append({"id": cid, "label": label, "state": "NOT_CHECKABLE", "detail": "withheld: INCONSISTENT_RUN"})
        elif lhs is None or rhs is None:
            checks.append({"id": cid, "label": label, "state": "NOT_CHECKABLE", "detail": "an input is missing (unknown, not zero)"})
        else:
            ok = abs(lhs - rhs) <= TOL
            checks.append({"id": cid, "label": label, "state": "PASS" if ok else "FAIL", "detail": f"{detail}: {lhs:,.2f} vs {rhs:,.2f}"})
    def modelled(d: Mapping[str, Any]) -> Tuple[bool, float, str]:
        """(checkable, modelled cost total, what was used). The identity is net = gross (+ funding) - fees - slippage (taker walk + adverse selection). A producer that states NO modelled cost
        is checked without one (its numbers either reconcile or they do not); a PARTIALLY stated pair is not checkable (an unknown part is never zero)."""
        tot, tk, ad = d.get("slippage_usd"), d.get("taker_slippage_usd"), d.get("adverse_selection_usd")
        if tot is not None:
            return True, tot, "slippage"
        if tk is not None and ad is not None:
            return True, tk + ad, "taker slippage + adverse selection"
        if tk is None and ad is None:
            return True, 0.0, "no modelled cost stated"
        return False, 0.0, "only one of taker slippage / adverse selection stated"
    g, f, n = R.get("gross_pnl_usd"), R.get("fees_usd"), R.get("net_pnl_usd")
    ok_r, slip_r, used_r = modelled(R)
    chk("gross_minus_fees", "gross - fees - modelled slippage = net (replay)", None if (g is None or f is None or not ok_r) else g - f - slip_r, n, f"replay gross - fees - {used_r} vs net")
    syms = R.get("symbols") or []
    sn = [s["net_pnl_usd"] for s in syms]
    chk("symbols_sum", "sum of symbol nets = net (replay)", None if (not syms or any(x is None for x in sn)) else sum(sn), n, "symbols vs total")
    rg, rf, fu, rn, un = (L.get(k) for k in ("realised_gross_usd", "fees_usd", "funding_usd", "realised_net_usd", "unrealised_usd"))
    ok_l, slip_l, used_l = modelled(L)
    chk("ledger_components", "realised gross + funding - fees - modelled slippage = realised net (ledger)", None if (None in (rg, rf, fu) or not ok_l) else rg + fu - rf - slip_l, rn,
        f"realised gross + funding - fees - {used_l} vs realised net")
    tot = F["net"]["v"]
    chk("realised_plus_unrealised", "realised net + unrealised = net", None if (rn is None or un is None) else rn + un, tot, "realised + unrealised vs the one net")
    if L.get("net_usd") is not None and n is not None:
        chk("ledger_net_matches_replay", "ledger net = replay net", L["net_usd"], n, "ledger vs replay")
    if L.get("fees_usd") is not None and f is not None:
        chk("fees_match", "ledger fees = replay fees", L["fees_usd"], f, "ledger vs replay")
    if L.get("unrealised_usd") is not None and P.get("unrealised_pnl_usd") is not None:
        # The ledger values what is open at its LIQUIDATION value (exit side of the book, less the taker exit fee: what the one net includes); the portfolio shows MARK-TO-MARKET. They differ by exactly
        # the exit adjustment the ledger states. A ledger that states none is compared directly (its own claim is that the two valuations coincide).
        adj = L.get("exit_adjustment_usd") or 0.0
        chk("unrealised_matches_portfolio", "ledger unrealised - exit adjustment = portfolio mark-to-market unrealised", L["unrealised_usd"] - adj, P["unrealised_pnl_usd"],
            "ledger (liquidation value) less its exit adjustment vs portfolio (mark-to-market)")
    pu = [p["unrealised_pnl_usd"] for p in (P.get("positions") or [])]
    if P and P.get("unrealised_pnl_usd") is not None and pu:
        chk("positions_sum", "sum of position unrealised = portfolio unrealised", None if any(x is None for x in pu) else sum(pu), P["unrealised_pnl_usd"], "positions vs total")
    used = {a["basis"] for a, k in ((rp, R), (lg, L), (pf, P)) if k}
    if len(used) > 1 and not withheld:
        checks.append({"id": "bases_agree", "label": "all contributing files declare one basis", "state": "FAIL", "detail": "mixed bases: " + ", ".join(sorted(str(u) for u in used))})
    return F, checks


def _venue_recon(art: Mapping[str, Dict[str, Any]], coh: Mapping[str, Any], integrity: str) -> Dict[str, Any]:
    """VENUE_RECONCILED needs: a ledger that is itself EXCHANGE_REPORTED, a reconcile report that compared a venue view with no active finding and no halt, one consistent run, and
    passing accounting identities. Anything less is INTERNAL_CONSISTENCY_ONLY. A simulated ledger can never be venue reconciled."""
    lg, rc = art["ledger_pnl.json"], art["reconcile_report.json"]
    why: List[str] = []
    if lg["state"] != "OK":
        why.append("no ledger_pnl.json")
    elif lg["basis"] != "EXCHANGE_REPORTED":
        why.append(f"the ledger basis is {lg['basis']}, not EXCHANGE_REPORTED")
    if rc["state"] != "OK":
        why.append("no reconcile_report.json")
    else:
        d = rc["data"]
        if not any(("venue" in v.lower() or "exchange" in v.lower()) for v in d["views_compared"]):
            why.append("the reconcile report compared no venue view (views: " + (", ".join(d["views_compared"]) or "none") + ")")
        if d["halt_new_exposure"]:
            why.append("the reconcile report halts new exposure")
        if [x for x in d["findings"] if x["severity"] != "INFO"]:
            why.append("the reconcile report has active findings")
    for name, a in (("ledger_pnl.json", lg), ("reconcile_report.json", rc)):                 # self-attestation is not enough: both files must NAME a TESTNET run (a REPLAY / PAPER / SimVenue view never counts)
        if a["state"] == "OK":
            ident = a.get("identity")
            if not ident or ident.get("mode") != "TESTNET":
                why.append(f"{name} does not carry a TESTNET run identity (mode {ident.get('mode') if ident else 'absent'})")
    if coh["verdict"] != "CONSISTENT":
        why.append(f"run identity is {coh['verdict']}")
    if integrity != "PASS":
        why.append(f"accounting identities: {integrity}")
    ok = not why
    return {"status": "VENUE_RECONCILED" if ok else "INTERNAL_CONSISTENCY_ONLY", "why_not": why,
            "meaning": ("INTERNAL_CONSISTENCY_ONLY: the files agree with each other; nothing here was compared with the venue." if not ok else
                        "VENUE_RECONCILED: the reconcile report compared the ledger with a venue view. This is not mainnet cash: the run mode names its environment.")}


def _accounting(art: Mapping[str, Dict[str, Any]], coh: Mapping[str, Any], F: Mapping[str, Dict[str, Any]], checks: List[Dict[str, Any]], pnl: Mapping[str, Any], testnet: Mapping[str, Any]) -> Dict[str, Any]:
    withheld = bool(coh["combined_numbers_withheld"])
    fails = [c for c in checks if c["state"] == "FAIL"]
    integrity = "WITHHELD" if withheld else "FAIL" if fails else ("PASS" if any(c["state"] == "PASS" for c in checks) else "NOT_CHECKABLE")
    recon = _venue_recon(art, coh, integrity)
    have = art["replay_report.json"]["state"] == "OK" or art["ledger_pnl.json"]["state"] == "OK"
    order = [("gross", "Gross (before fees)"), ("fees", "Fees"), ("funding", "Funding"), ("slippage", "Slippage"), ("hedge_cost", "Hedge cost"), ("adverse_selection", "Adverse selection"),
             ("closed_gross", "Realised gross"), ("closed_net", "Realised net"), ("unrealised", "Unrealised (mark-to-market)"), ("net", "Net (after fees; the one net)")]
    required = ("gross", "fees", "funding", "closed_net", "unrealised", "net")
    missing = [k for k in required if F[k]["v"] is None]
    nets = F["net"]
    simulated_basis = nets["basis"] == "SIMULATED" or art["ledger_pnl.json"]["basis"] == "SIMULATED"
    complete = (not withheld) and not missing and integrity == "PASS" and recon["status"] == "VENUE_RECONCILED" and not simulated_basis
    why_incomplete = ([] if complete else
                      (["INCONSISTENT_RUN: numbers withheld"] if withheld else []) + ([f"missing: {', '.join(missing)}"] if missing and not withheld else []) +
                      ([f"accounting identities {integrity}"] if integrity != "PASS" and not withheld else []) + (["the figures are simulated"] if simulated_basis else []) +
                      ([recon["meaning"]] if recon["status"] != "VENUE_RECONCILED" else []))
    def label(k: str, lbl: str) -> str:                           # a simulated figure is never labelled "realised"
        return {"closed_gross": "Simulated closed-leg gross", "closed_net": "Simulated closed-leg net"}[k] if (k in ("closed_gross", "closed_net") and F[k]["basis"] == "SIMULATED") else lbl
    out: Dict[str, Any] = {"state": "OK" if have else "NO_DATA", "withheld": withheld, "components": [{"key": k, "label": label(k, lbl), "value": F[k]} for k, lbl in order], "checks": checks,
                           "integrity": integrity, "venue_reconciliation": recon, "complete": complete, "why_incomplete": why_incomplete, "pnl_kind": pnl.get("kind"),
                           "simulated": {"label": "SIMULATED (replay / paper): never realised cash", "net": F["net"] if simulated_basis else val(None, "MISSING", "USD", "no simulated figure loaded"),
                                         "unrealised": F["unrealised"] if simulated_basis else val(None, "MISSING", "USD")},
                           "testnet": {"label": "TESTNET (venue sandbox, fake money): not owner profit", "state": testnet.get("state"), "fees": testnet.get("fees", val(None, "MISSING", "USD")),
                                       "funding": testnet.get("funding", val(None, "MISSING", "USD")), "price_pnl": testnet.get("price_pnl", val(None, "MISSING", "USD")),
                                       "note": "Components only. Nothing from the testnet session is ever added to a simulated number."},
                           "mainnet_cash": {"label": "REALISED MAINNET CASH", "status": "NOT_AVAILABLE", "realised": val(None, "MISSING", "USD"),
                                            "note": "No artifact in this repository can carry mainnet cash: the run identity has no MAINNET mode and live trading is not authorised."}}
    if not have:
        out["empty"] = "No replay_report.json or ledger_pnl.json yet. Run the replay and accounting CLIs to write them into the artifacts directory."
    return out


# ======================================================================== testnet, pipeline, reconcile
TESTNET_BANNER = "TESTNET results are not owner profit. A testnet fill proves a code path against a sandbox with fake money; it is not an edge and not cash."


def _ostat(status: str) -> str:
    s = status.lower()
    if "partial" in s:
        return "partial"
    if "cancel" in s:
        return "cancelled"
    if "reject" in s:
        return "rejected"
    if "fill" in s:
        return "filled"
    return "open"


def _testnet_view(a: Dict[str, Any], oc: Mapping[str, Any]) -> Dict[str, Any]:
    base = {"file": a["file"], "age_ms": a["age_ms"], "stale": a["stale"], "generated_ms": a["generated_ms"], "banner": TESTNET_BANNER,
            "offline_qualification": val(oc["testnet"].get("offline_qualification"), "CALCULATED", note="guard probes only; it reads nothing from a venue")}
    if a["state"] == "EMPTY":
        return {**base, "state": "NO_DATA", "session_state": "NOT_RUN", "pill": "NO_DATA",
                "empty": "NOT_RUN: no testnet_session.json. The owner runs the read-only testnet preflight and, separately, a confirmed session (RUNBOOKS: testnet); until then there is nothing to show. NOT_RUN is not a pass."}
    if a["state"] == "ERROR":
        return {**base, "state": "ERROR", "session_state": "ERROR", "pill": "FAIL", "error": f"{a['file']}: {a['error']}"}
    d, run = a["data"], a["data"]["basis"]
    flags: List[str] = []
    pf = d["preflight"]
    eff = pf["status"]
    if eff == "PASS" and (not pf["checks"] or any(not c["ok"] for c in pf["checks"])):
        eff = "FAIL"
        flags.append("PREFLIGHT_PASS_CONTRADICTS_CHECKS: the file says PASS but a check failed or none is listed; treated as FAIL")
    plan, conf = d["plan"], d["confirmation"]
    if conf["state"] == "CONFIRMED" and plan is not None and conf["plan_digest_confirmed"] != plan["digest"]:
        flags.append("CONFIRMATION_DIGEST_MISMATCH: the confirmed digest is not the plan's digest")
    if d["orders"] and conf["state"] != "CONFIRMED":
        flags.append("ORDERS_WITHOUT_CONFIRMATION: orders exist but no confirmation of a plan is recorded")
    fp, fv = d["final_position"], d["flat_verified"]
    nonzero = sorted(k for k, q in (fp or {}).items() if abs(q) > 1e-12)
    flat = fp is not None and fv is True and not nonzero
    if fv is True and (fp is None or nonzero):
        flags.append("FLAT_CLAIM_NOT_SUPPORTED: flat_verified is true but the final position is missing or not zero; not shown as flat")
    fees = [e["fee_usd"] for e in d["executions"]]
    if d["net"]["fees_usd"] is not None and fees and None not in fees and abs(sum(fees) - d["net"]["fees_usd"]) > TOL:
        flags.append("FEES_DO_NOT_MATCH_EXECUTIONS: net.fees_usd differs from the sum of the execution fees")
    if run == "NOT_RUN" and eff == "NOT_RUN" and plan is None:
        state = "NOT_RUN"
    elif eff == "FAIL":
        state = "PREFLIGHT_FAILED"
    elif eff == "NOT_RUN":
        state = "PREFLIGHT_NOT_RUN"
    elif plan is None:
        state = "PREFLIGHT_PASSED_NO_PLAN"
    elif d["orders"]:
        state = "EXECUTED_FLAT_VERIFIED" if flat else ("EXECUTED_NOT_FLAT" if nonzero else "EXECUTED_FLAT_UNVERIFIED")
    elif conf["state"] == "CONFIRMED":
        state = "CONFIRMED_NO_ORDERS"
    else:
        state = "PLANNED_UNCONFIRMED"
    pill = ("NO_DATA" if state in ("NOT_RUN", "PREFLIGHT_NOT_RUN") else "FAIL" if state in ("PREFLIGHT_FAILED", "EXECUTED_NOT_FLAT") or flags else
            "INFO" if state == "EXECUTED_FLAT_VERIFIED" else "ATTENTION")
    xb = "EXCHANGE_REPORTED" if run == "EXCHANGE_REPORTED" else "MISSING"           # venue-sourced fields carry a venue basis only when the file declares it
    st = [_ostat(o["status"]) for o in d["orders"]]
    cred = d["credential_state"]
    return {**base, "state": "OK", "session_state": state, "pill": pill, "flags": flags, "run_basis": run, "identity_status": a["identity_status"],
            "identity": a["identity"],
            "venue": val("Testnet", "CALCULATED", note="the registered testnet host; checked against venues.py"), "endpoint_host": d["endpoint_host"],
            "credentials": {"dedicated_key_present": val(cred["dedicated_key_present"], "MEASURED"), "dedicated_secret_present": val(cred["dedicated_secret_present"], "MEASURED"),
                            "generic_credentials_ignored": val(cred["generic_credentials_ignored"], "MEASURED"), "note": "Booleans only: no value is read or shown."},
            "preflight": {"status": eff, "declared_status": pf["status"], "checked_ms": pf["checked_ms"], "checks": pf["checks"]},
            "plan": None if plan is None else {"digest": val(plan["digest"], "CALCULATED"), "manifest_digest": val(plan["manifest_digest"], "CALCULATED"), "notional": val(plan["notional_usd"], "CALCULATED", "USD"),
                                               "legs": [{**{k: l[k] for k in ("symbol", "side", "order_type")}, "qty": val(l["qty"], "CALCULATED"), "price": val(l["price"], "CALCULATED", "USD"),
                                                         "notional": val(l["notional_usd"], "CALCULATED", "USD")} for l in plan["legs"]]},
            "confirmation": {"state": conf["state"], "plan_digest_confirmed": conf["plan_digest_confirmed"], "matches_plan": bool(plan and conf["plan_digest_confirmed"] == plan["digest"])},
            "orders": [{**{k: o[k] for k in ("link_id", "order_id", "symbol", "side", "status")}, "qty": val(o["qty"], xb), "filled_qty": val(o["filled_qty"], xb), "avg_price": val(o["avg_price"], xb, "USD")} for o in d["orders"]],
            "executions": [{**{k: e[k] for k in ("exec_id", "link_id", "symbol", "side", "ts_ms")}, "qty": val(e["qty"], xb), "price": val(e["price"], xb, "USD"), "fee": val(e["fee_usd"], xb, "USD")} for e in d["executions"]],
            "counts": {"orders": len(d["orders"]), "acked": sum(1 for o in d["orders"] if o["order_id"]), "filled": st.count("filled"), "partial": st.count("partial"), "cancelled": st.count("cancelled"),
                       "rejected": st.count("rejected"), "open": st.count("open"), "executions": len(d["executions"])},
            "final_position": None if fp is None else {k: val(q, xb) for k, q in fp.items()}, "flat": {"verified_flat": flat, "declared": fv, "nonzero": nonzero},
            "fees": val(d["net"]["fees_usd"], xb, "USD", "TESTNET; not owner profit"), "funding": val(d["net"]["funding_usd"], xb, "USD", "TESTNET; not owner profit"),
            "price_pnl": val(d["net"]["price_pnl_usd"], xb, "USD", "TESTNET; not owner profit"), "net_note": d["net"]["note"],
            "evidence": {"file": a["file"], "basis": run, "plan_digest": plan["digest"] if plan else None, "manifest_digest": plan["manifest_digest"] if plan else None}}


def _pipeline_view(a: Dict[str, Any]) -> Dict[str, Any]:
    def view(d: Mapping[str, Any]) -> Dict[str, Any]:
        mean, nxt = OUTCOME_TEXT[d["outcome"]]
        return {"outcome": d["outcome"], "meaning": mean, "next": nxt, "reason": d["reason"], "required_span": d["required_span"], "data_digest_parts": d["data_digest_parts"],
                "evidence_basis": d["evidence_basis"], "blocking": d["outcome"] != "SIMULATED_EXECUTION"}
    return _artifact_view(a, view, empty="No pipeline summary yet. Run the pipeline CLI to write pipeline_summary.json into the artifacts directory; until then nothing says what the engine last did.")


def _scope_view(a: Dict[str, Any]) -> Dict[str, Any]:
    return _artifact_view(a, lambda d: {"scope_verdict": d["scope_verdict"], "eligible": d["eligible"], "lifecycle_classes": d["lifecycle_classes"]},
                          empty="No universe scope yet. Run the universe scope check to write universe_scope.json into the artifacts directory.")


def _funnel_view(a: Dict[str, Any]) -> Dict[str, Any]:
    return _artifact_view(a, lambda d: {"steps": d["steps"], "exclusion_reasons": d["exclusion_reasons"]}, empty="No funnel yet. Run the funnel report to write funnel.json into the artifacts directory.")


def _reconcile_view(a: Dict[str, Any]) -> Dict[str, Any]:
    def view(d: Mapping[str, Any]) -> Dict[str, Any]:
        active = [x for x in d["findings"] if x["severity"] != "INFO"]
        return {"findings": d["findings"], "active": active, "halt_new_exposure": d["halt_new_exposure"], "views_compared": d["views_compared"],
                "has_venue_view": any(("venue" in v.lower() or "exchange" in v.lower()) for v in d["views_compared"])}
    return _artifact_view(a, view, empty="No reconcile report yet. Run the reconciliation CLI to write reconcile_report.json; until then nothing compares the internal views with each other or with the venue.")


ENV_FLAGS = ("LIVE_TRADING_ACK", "USE_TESTNET", "PAPER_TRADING", "BYBIT_API_KEY", "BYBIT_API_SECRET", "OWNER_ATTESTATION_KEY")


def _env_flags(env: Mapping[str, str]) -> List[Dict[str, str]]:
    """PRESENCE only: 'set' or 'unset'. No value of any variable is ever read into the state."""
    return [{"name": n, "presence": "set" if str(env.get(n, "") or "").strip() else "unset"} for n in ENV_FLAGS]


def _usd(x: Optional[Mapping[str, Any]]) -> str:
    return "not stated (MISSING)" if not x or x.get("v") is None else f"${x['v']:,.2f}"


def _next_steps(c: Mapping[str, Any]) -> List[str]:
    """What must happen next, computed from the blocking reasons actually present. The gate line is always last and is the owner's."""
    out: List[str] = []
    coh = c["coh"]
    if coh["verdict"] == "INCONSISTENT_RUN":
        out.append("Regenerate the artifacts from ONE run: " + ", ".join(sorted({m["field"] for m in coh["mismatches"]})) + " differ between files. Combined numbers stay withheld until they match.")
    rc = c["reconcile"]
    if rc.get("state") == "OK" and (rc["halt_new_exposure"] or rc["active"]):
        out.append("Resolve the reconciliation findings (" + ", ".join(sorted({x["code"] for x in rc["active"]})[:4]) + "); new exposure stays halted" + (" by the report" if rc["halt_new_exposure"] else "") + ".")
    if c["risk"].get("state") == "OK" and c["risk"]["halted"]:
        out.append("Trading is halted by the risk state: " + "; ".join(c["risk"]["halt_reasons"][:2]) + ". Clear the cause; the page cannot.")
    bad = [f for f, a in c["art"].items() if a["state"] == "ERROR"]
    if bad:
        out.append("Fix or regenerate the refused file(s): " + ", ".join(bad[:5]) + ".")
    fv = c["fresh"].get("verdict")
    if fv == "NO_DATA":
        out.append("Capture market data: start tools/market_collector.py run-continuous --out-root DIR, then check it with tools/data_freshness_audit.py.")
    elif fv in ("STALE", "PARTIAL") or c["fresh"].get("state") == "ERROR":
        out.append(f"Market data is {fv or 'unreadable'}: see Evidence and run tools/data_freshness_audit.py before trusting any result.")
    pl = c["pipeline"]
    if pl["state"] == "OK" and pl["blocking"]:
        out.append(f"Pipeline outcome {pl['outcome']}: {pl['next']}")
    elif pl["state"] == "NO_DATA":
        out.append("Run the pipeline so it writes pipeline_summary.json; until then the engine's last outcome is unknown.")
    absent = [f for f in ("replay_report.json", "portfolio_state.json", "ledger_pnl.json", "risk_state.json") if c["art"][f]["state"] == "EMPTY"]
    if absent:
        out.append("Produce the missing artifact(s): " + ", ".join(absent) + ".")
    if coh["verdict"] == "UNVERIFIED":
        out.append("Regenerate the artifacts with a run identity so they can be shown to belong together.")
    if c["accounting"]["state"] == "OK" and c["accounting"]["integrity"] == "FAIL":
        out.append("Fix the accounting identity failure(s): " + ", ".join(k["id"] for k in c["accounting"]["checks"] if k["state"] == "FAIL")[:200] + ".")
    tn = c["testnet"]
    if tn["session_state"] in ("NOT_RUN", "PREFLIGHT_NOT_RUN"):
        out.append("Owner: run the read-only testnet preflight (RUNBOOKS: testnet). It has not been run; TESTNET results are never owner profit.")
    elif tn.get("flags"):
        out.append("Investigate the testnet session flags: " + "; ".join(f.split(":")[0] for f in tn["flags"][:3]) + ".")
    elif tn["session_state"] == "PREFLIGHT_FAILED":
        out.append("The testnet preflight failed: fix the failing check(s) before any session.")
    if not c["gate"]["allows_live"]:
        out.append(f"The promotion gate is {c['gate']['decision']} ({c['gate']['complete']} of {c['gate']['total']} items); only the owner can sign it and nothing on this page can.")
    return out


def _answers(c: Mapping[str, Any]) -> List[Dict[str, str]]:
    """The seven plain-language questions, answered ONLY from the one state (figures, sections), never from constants. Money is not mentioned when the run is inconsistent."""
    F, coh, pl, pf, ex, tn, acc, rc = c["F"], c["coh"], c["pipeline"], c["portfolio"], c["execution"], c["testnet"], c["accounting"], c["reconcile"]
    wh = bool(coh["combined_numbers_withheld"])
    A: List[Dict[str, str]] = []
    eng = [f"Live trading is {'ARMED (stop and check it is intended)' if c['live'] else 'not armed'}; this is a paper and testnet system."]
    eng.append(f"Last pipeline outcome: {pl['outcome']}, {pl['meaning'].lower()}" if pl["state"] == "OK" else "No pipeline_summary.json, so nothing says what the engine last did.")
    eng.append(f"Market data: {c['fresh'].get('verdict', 'NO_DATA')}.")
    if c["risk"].get("state") == "OK" and c["risk"]["halted"]:
        eng.append("Trading is HALTED by the risk state.")
    A.append({"q": "What is the engine doing?", "a": " ".join(eng), "basis": "CALCULATED"})
    if wh:
        cap = "Withheld: the artifacts are not from one run (INCONSISTENT_RUN)."
    elif pf.get("state") == "OK":
        cap = (f"No real money: {'simulated ' if pf['simulated'] else ''}gross exposure {_usd(pf['gross_exposure'])}, net {_usd(pf['net_exposure'])} (basis {pf['gross_exposure']['basis']})."
               if pf["simulated"] else f"Gross exposure {_usd(pf['gross_exposure'])}, net {_usd(pf['net_exposure'])} per portfolio_state.json (basis {pf['gross_exposure']['basis']}).")
    else:
        cap = "Unknown (MISSING): there is no portfolio_state.json."
    cap += f" Capital allocated by the owner console: {_usd(c['capital'])}."
    A.append({"q": "What capital is at risk?", "a": cap, "basis": "MISSING" if pf.get("state") != "OK" or wh else pf["gross_exposure"]["basis"]})
    pos = (f"{pf['n_positions']} position(s) in portfolio_state.json" + (" (simulated)." if pf["simulated"] else ".")) if pf.get("state") == "OK" else "Unknown: no portfolio_state.json."
    if tn.get("state") == "OK" and tn["final_position"] is not None:
        pos += " Testnet final position: " + ("flat, verified." if tn["flat"]["verified_flat"] else "not verified flat.")
    A.append({"q": "Are there positions?", "a": pos, "basis": "SIMULATED" if pf.get("simulated") else "MISSING"})
    if acc["complete"]:
        pa = f"Yes: every required component is present, the identities pass and the ledger is venue reconciled. Net {_usd(F['net'])}."
    else:
        pa = ("No. " + "; ".join(acc["why_incomplete"][:3])) if acc["why_incomplete"] else "No."
        if not wh and F["net"]["v"] is not None:
            pa += f" The one net on file is {_usd(F['net'])} ({F['net']['basis']}); unrealised {_usd(F['unrealised'])}."
    A.append({"q": "Is P&L complete?", "a": pa, "basis": "CALCULATED"})
    done = []
    if ex.get("state") == "OK":
        done.append(f"Replay: {ex['n_groups']} order group(s) (SIMULATED, never cash).")
    if tn.get("state") == "OK" and tn["counts"]["orders"]:
        done.append(f"Testnet: {tn['counts']['orders']} order(s), {tn['counts']['executions']} execution(s) (not owner profit).")
    done.append("No mainnet order has been sent from here and none can be.")
    A.append({"q": "What has executed?", "a": " ".join(done), "basis": "SIMULATED" if ex.get("state") == "OK" else "CALCULATED"})
    blk = [b["label"] for b in c["actions"]["items"] if not b["permitted"]]
    restr = [r["text"] for r in c["restrictions"]][:3]
    A.append({"q": "What is blocked?", "a": ("Blocked: " + "; ".join(blk) + "." if blk else "Nothing the repository's checks block.") + (" Restrictions: " + " | ".join(restr) if restr else ""), "basis": "CALCULATED"})
    steps = c["next_steps"]
    A.append({"q": "What must happen next?", "a": steps[0] if steps else "Nothing is blocking and nothing is pending (verify independently).", "basis": "CALCULATED"})
    return A


def build_state(*, data_root: Optional[str], universe_dir: Optional[str], artifacts_dir: Optional[str], env: Mapping[str, str], now_ms: int,
                with_economics: bool = False, economics: Optional[Dict[str, Any]] = None, verify: bool = False, testnet_dir: Optional[str] = None) -> Dict[str, Any]:
    """Pure aggregation of real sources (no network, no write). Every number carries a basis; every missing source is an explicit NO_DATA/ERROR section.
    ONE state: every artifact is read once; ``coherence`` and ``figures`` are derived once; every section below reads them."""
    now_ms = int(now_ms)
    # testnet_session.json is written by the owner's session tool into ITS state dir (default ~/.ai-agent-control/ms_testnet), not into a pipeline run's artifacts dir: read it from there when given
    art = {name: load_artifact(testnet_dir if (name == "testnet_session.json" and testnet_dir) else artifacts_dir, name, now_ms) for name in ARTIFACTS}
    oc = OC.build(dict(env), now_ms=now_ms)
    gate = _gate()
    actions = _actions(oc, gate)
    coh = _coherence(art)
    F, fig_checks = _figures(art, coh)

    def cand() -> Dict[str, Any]:
        scan = CR.scan_data_root(data_root)
        econ = economics if economics is not None else (CR.btc_carry_economics() if with_economics else None)
        return {"state": "OK", "rows": [CR.candidate_row(n, s, scan, econ) for n, s in CR.load_candidates()], "econ": econ is not None}
    cr = _safe(cand, "candidate_readiness")
    research = (_research(cr["rows"], art["competition.json"], art["opportunities.json"], cr["econ"], gate) if cr.get("state") == "OK" else
                {"state": "ERROR", "error": cr.get("error"), "candidates": [], "rejected": [], "sufficient_evidence": [], "stage_counts": {}, "note": ""})
    pipeline = _pipeline_view(art["pipeline_summary.json"])
    research["pipeline"] = pipeline
    research["universe_scope"] = _scope_view(art["universe_scope.json"])
    research["funnel"] = _funnel_view(art["funnel.json"])
    fresh = _safe(lambda: _freshness(data_root, universe_dir, now_ms, verify), "data_freshness_audit")
    capture = _safe(lambda: _capture(universe_dir), "universe_capture")
    portfolio = _portfolio(art["portfolio_state.json"], art["ledger_pnl.json"], F)
    execution = _execution(art["replay_report.json"], oc, F)
    risk = _risk(art["risk_state.json"], oc)
    reconcile = _reconcile_view(art["reconcile_report.json"])
    testnet = _testnet_view(art["testnet_session.json"], oc)
    accounting = _accounting(art, coh, F, fig_checks, portfolio["pnl"], testnet)
    rc_ok = reconcile.get("state") == "OK"
    execution["reconciliation"] = reconcile
    execution["testnet"] = {k: testnet.get(k) for k in ("state", "session_state", "pill", "counts", "orders", "executions", "empty", "error", "banner", "run_basis")}
    risk["halt_flags"] = ([{"source": "risk_state.json", "halted": bool(risk["halted"]), "why": "; ".join(risk["halt_reasons"]) or "not halted"}] if risk.get("state") == "OK" else []) + \
                         ([{"source": "reconcile_report.json", "halted": bool(reconcile["halt_new_exposure"]), "why": "halt_new_exposure"}] if rc_ok else []) + \
                         [{"source": "promotion gate", "halted": not gate["allows_live"], "why": f"{gate['decision']}: live entry is closed"}]
    risk["kill_switch"] = val(None, "MISSING", note="no artifact carries a kill-switch state; risk_state/v1 has only halted and halt_reasons")
    risk["stress"] = val(None, "MISSING", note="no stress result artifact exists; see the unmeasured limits in the policy block")
    risk["recovery"] = ({"state": "RECOVERY_REQUIRED" if (reconcile["halt_new_exposure"] or reconcile["active"]) else "NO_RECOVERY_REPORTED", "basis": "CALCULATED", "why": "from reconcile_report.json"}
                        if rc_ok else {"state": "MISSING", "basis": "MISSING", "why": "no reconcile_report.json: recovery status is unknown, not fine"})
    risk["exposure"] = ({"gross": portfolio["gross_exposure"], "net": portfolio["net_exposure"]} if portfolio.get("state") == "OK" else {"gross": val(None, "MISSING"), "net": val(None, "MISSING")})
    risk["refusals"] = {"outcome": pipeline["outcome"] if pipeline.get("state") == "OK" and pipeline["outcome"] == "RISK_REFUSAL" else None, "without_policy": risk["policy"]["refusals_without_policy"],
                        "alerts": [a for a in risk.get("alerts", []) if a["severity"] != "INFO"]}

    # Survivorship: a result is survivorship-free only if the file CLAIMS delistings were observed AND this run's capture check corroborates it.
    rp = art["replay_report.json"]
    claimed = rp["data"]["universe_scope"] if rp["state"] == "OK" else None
    corroborated = capture.get("state") == "OK" and capture.get("survivorship") == CR.DELISTINGS_OBSERVED
    free = bool(claimed == "DELISTINGS_OBSERVED" and corroborated)
    if rp["state"] == "OK":
        execution["survivorship"] = {"claimed_by_file": claimed, "corroborated_here": corroborated, "survivorship_free": free,
                                     "text": ("Delistings observed and corroborated by a verified universe capture." if free else
                                              "SURVIVORS ONLY: contracts that were delisted are missing, so this result must not be read as survivorship-free.")}

    us = research["universe_scope"]
    if us.get("state") == "OK":                                   # a file's claim of more than survivors is shown only when a verified capture here corroborates it
        v = str(us["scope_verdict"] or "")
        claims_free = bool(re.search(r"SURVIVORSHIP_FREE|DELISTINGS_OBSERVED", v.upper()))
        us["scope_verdict_shown"] = v if (not claims_free or corroborated) else "SURVIVORS_ONLY (the file claims more; no verified capture here corroborates it)"
        us["corroborated_here"] = corroborated

    # ---- attention list (every red/amber, with the source)
    att: List[Dict[str, str]] = []

    def flag(sev: str, src: str, text: str) -> None:
        att.append({"severity": sev, "source": src, "text": text})
    for f in oc["environment"]["findings"]:
        flag("FAIL" if oc["environment"]["live_authorized"] else "ATTENTION", "environment", f)
    if gate["allows_live"] or oc["environment"]["live_authorized"]:
        flag("FAIL", "safety", "Live trading looks armed or the gate allows it. This repository must stay paper/testnet.")
    if coh["verdict"] == "INCONSISTENT_RUN":
        flag("FAIL", "coherence", coh["banner"])
    elif coh["verdict"] == "UNVERIFIED":
        flag("ATTENTION", "coherence", coh["banner"])
    fv = fresh.get("verdict")
    if fresh.get("state") == "ERROR":
        flag("FAIL", "data", str(fresh.get("error")))
    elif fv == "STALE":
        flag("FAIL", "data", "Market data is STALE: " + "; ".join(i["detail"] for i in fresh["issues"][:2]))
    elif fv == "PARTIAL":
        flag("ATTENTION", "data", "Market data is PARTIAL: " + "; ".join(i["code"] for i in fresh["issues"][:4]))
    if capture.get("state") == "ERROR":
        flag("ATTENTION", "universe", str(capture.get("error")) + ": " + "; ".join(capture.get("problems", [])[:2]))
    for name, a in art.items():
        if a["state"] == "ERROR":
            flag("FAIL", "artifacts", f"{name} refused: {a['error']}")
        elif a["state"] == "OK" and a["stale"]:
            flag("ATTENTION", "artifacts", f"{name} is older than {ARTIFACT_STALE_MS // 86_400_000} days")
    if risk.get("state") == "OK":
        if risk["halted"]:
            flag("FAIL", "risk", "Trading is HALTED: " + "; ".join(risk["halt_reasons"][:3]))
        for al in risk["alerts"]:
            if al["severity"] in ("CRITICAL", "WARN"):
                flag("FAIL" if al["severity"] == "CRITICAL" else "ATTENTION", "risk", f"{al['code']}: {al['detail']}")
        for lim in risk["limits"]:
            if lim["status"] == "BREACH":
                flag("FAIL", "risk", f"limit breached: {lim['name']}")
    if execution.get("state") == "OK" and execution["unhedged_or_unknown"]:
        flag("ATTENTION", "execution", f"{execution['unhedged_or_unknown']} order group(s) UNHEDGED or UNKNOWN_OUTCOME in the replay")
    if rc_ok:
        for x in reconcile["active"]:
            flag("FAIL" if x["severity"] in ("CRITICAL", "ERROR", "HALT", "HIGH") else "ATTENTION", "reconcile", f"{x['code']}: {x['detail']}")
        if reconcile["halt_new_exposure"]:
            flag("FAIL", "reconcile", "The reconcile report halts new exposure.")
    if accounting["state"] == "OK" and accounting["integrity"] == "FAIL":
        flag("FAIL", "accounting", "Accounting identity failure: " + ", ".join(k["id"] for k in accounting["checks"] if k["state"] == "FAIL"))
    for fl in testnet.get("flags") or []:
        flag("FAIL", "testnet", fl)
    if testnet.get("session_state") == "PREFLIGHT_FAILED":
        flag("ATTENTION", "testnet", "The testnet preflight failed.")

    # ---- restrictions and incidents
    restrictions: List[Dict[str, str]] = []
    if not gate["allows_live"]:
        restrictions.append({"source": "promotion gate", "severity": "INFO", "text": f"Live entry is closed: the promotion gate is {gate['decision']} ({gate['complete']} of {gate['total']} items)."})
    if not oc["environment"]["live_authorized"]:
        restrictions.append({"source": "environment", "severity": "INFO", "text": "Live trading is not authorised: " + str(oc["environment"]["reason"])})
    if not oc["risk"].get("policy_active"):
        restrictions.append({"source": "risk policy", "severity": "INFO", "text": "No owner-approved risk policy: every entry is refused."})
    if risk.get("state") == "OK" and risk["halted"]:
        restrictions.append({"source": "risk_state.json", "severity": "FAIL", "text": "Halted: " + "; ".join(risk["halt_reasons"][:3])})
    if rc_ok and reconcile["halt_new_exposure"]:
        restrictions.append({"source": "reconcile_report.json", "severity": "FAIL", "text": "New exposure is halted by the reconcile report."})
    if coh["verdict"] == "INCONSISTENT_RUN":
        restrictions.append({"source": "coherence", "severity": "FAIL", "text": "Combined figures are withheld: the artifacts are not from one run."})
    if fv in ("STALE", "PARTIAL", "NO_DATA") or fresh.get("state") == "ERROR":
        restrictions.append({"source": "market data", "severity": "ATTENTION", "text": f"Market data is {fv or 'unreadable'}: no result should be relied on."})
    incidents = [{"code": x["code"], "detail": x["detail"], "severity": x["severity"], "source": "reconcile_report.json"} for x in (reconcile["active"] if rc_ok else [])]

    # ---- overview cards: green only when a checker returned it from real data
    def card(cid: str, title: str, status: str, text: str, basis: str, link: str) -> Dict[str, Any]:
        return {"id": cid, "title": title, "status": status, "text": text, "basis": basis, "link": link}
    cards = []
    env_ok = oc["environment"]["status"] == "VERIFIED" and not oc["environment"]["live_authorized"] and not gate["allows_live"]
    cards.append(card("safety", "Safety posture", "INFO" if env_ok else "FAIL" if (oc["environment"]["live_authorized"] or gate["allows_live"]) else "ATTENTION",
                      f"Live trading: {'ARMED' if oc['environment']['live_authorized'] else 'not armed'}. Promotion gate: {gate['decision']} ({gate['complete']} of {gate['total']} items). Capital allocated: ${oc['capital']['capital_usd']:.0f}.",
                      "CALCULATED", "overview"))
    dm = {"HEALTHY": "OK", "PARTIAL": "ATTENTION", "STALE": "FAIL", "NO_DATA": "NO_DATA"}.get(str(fv), "FAIL" if fresh.get("state") == "ERROR" else "NO_DATA")
    cards.append(card("data", "Market data", dm, {"NO_DATA": "No market data captured yet. Run tools/market_collector.py to start capturing.", "OK": "Complete, fresh, backed up and verified in this run.",
                      "ATTENTION": "Data exists but is partial or unproved. See Evidence.", "FAIL": "Data is stale or the audit could not run. See Evidence."}[dm],
                      "UNVERIFIED" if dm != "NO_DATA" else "MISSING", "evidence"))
    if risk.get("state") == "OK":
        bad = [a for a in att if a["source"] in ("risk", "artifacts")]
        ops = "FAIL" if any(a["severity"] == "FAIL" for a in bad) else "ATTENTION" if bad else "OK"
        ops_text = "Risk state reports no halt, alert or breach." if ops == "OK" else f"{len(bad)} item(s) need attention. See Risk."
        ops_basis = risk["basis"]
    else:
        ops = "FAIL" if risk.get("state") == "ERROR" else "NO_DATA"
        ops_text = risk.get("error") or "No runtime risk state is available, so nothing can say the system is operating correctly."
        ops_basis = "MISSING"
    cards.append(card("operations", "System operations", ops, ops_text, ops_basis, "risk"))
    cards.append(card("research", "Research pipeline", "INFO", f"{len(research.get('candidates', []))} candidate(s) under study; {len(research.get('rejected', []))} rejection(s) on record; "
                      f"{len(research.get('sufficient_evidence', []))} with sufficient evidence. None is authorised.", "CALCULATED", "research"))
    if pipeline.get("state") == "OK":
        cards.append(card("pipeline", "Pipeline outcome", "INFO" if not pipeline["blocking"] else "ATTENTION", f"{pipeline['outcome']}: {pipeline['meaning']}", art["pipeline_summary.json"]["basis"] or "CALCULATED", "research"))
    else:
        cards.append(card("pipeline", "Pipeline outcome", "FAIL" if pipeline.get("state") == "ERROR" else "NO_DATA", pipeline.get("error") or pipeline.get("empty", ""), "MISSING", "research"))
    op = research.get("opportunities", {})
    if op.get("state") == "OK":
        cards.append(card("opportunity", "Estimated net opportunity", "INFO", f"{op['n_positive_net']} of {op['n_items']} scanned item(s) have positive NET after known costs. Estimates, not results.", "ESTIMATED", "research"))
    else:
        cards.append(card("opportunity", "Estimated net opportunity", "FAIL" if op.get("state") == "ERROR" else "NO_DATA", op.get("error") or op.get("empty", ""), "MISSING", "research"))
    if coh["combined_numbers_withheld"] and portfolio.get("state") == "OK":
        cards.append(card("exposure", "Portfolio exposure", "FAIL", "Withheld: the artifacts are not from one run (INCONSISTENT_RUN).", "MISSING", "portfolio"))
    elif portfolio.get("state") == "OK":
        pb = portfolio["gross_exposure"]["basis"]
        cards.append(card("exposure", "Portfolio exposure", "INFO", f"Gross ${portfolio['gross_exposure']['v']:,.0f}, net ${portfolio['net_exposure']['v']:,.0f} across {portfolio['n_positions']} position(s)." +
                          (" Simulated." if pb == "SIMULATED" else ""), pb, "portfolio"))
    else:
        cards.append(card("exposure", "Portfolio exposure", "FAIL" if portfolio.get("state") == "ERROR" else "NO_DATA", portfolio.get("error") or portfolio.get("empty", ""), "MISSING", "portfolio"))
    pnl = portfolio["pnl"]
    if pnl.get("state") == "OK" and coh["combined_numbers_withheld"]:
        cards.append(card("pnl", "Realised and unrealised P&L", "FAIL", "Withheld: the artifacts are not from one run (INCONSISTENT_RUN).", "MISSING", "accounting"))
    elif pnl.get("state") == "OK":
        sim = pnl["kind"] == "SIMULATED_NOT_REALISED"
        g = "simulated" if sim else "realised"
        if sim:                                                    # ONE net: the total simulated net (= the Execution tab / replay report net_pnl_usd), realised and unrealised beside it
            text = (f"Simulated net (total): {_usd(pnl['total_net'])}; of which realised {_usd(pnl[g + '_net'])} and unrealised {_usd(pnl['unrealised'])} (mark-to-market). "
                    "This is a simulation, not realised cash.")
        else:
            text = f"Realised net: {_usd(pnl[g + '_net'])}; unrealised {_usd(pnl['unrealised'])}. Not reconciled with the venue unless Accounting says so."
        cards.append(card("pnl", "Realised and unrealised P&L", "FAIL" if accounting["integrity"] == "FAIL" else "INFO", text, pnl["basis"], "accounting"))
    else:
        cards.append(card("pnl", "Realised and unrealised P&L", "FAIL" if pnl.get("state") == "ERROR" else "NO_DATA", pnl.get("error") or pnl.get("empty", ""), "MISSING", "accounting"))
    if rc_ok:
        cards.append(card("reconcile", "Reconciliation", "FAIL" if (reconcile["halt_new_exposure"] or reconcile["active"]) else ("OK" if reconcile["has_venue_view"] else "INFO"),
                          f"{len(reconcile['active'])} active finding(s) over {len(reconcile['views_compared'])} view(s)" + ("" if reconcile["has_venue_view"] else "; no venue view was compared, so this is internal only") +
                          ("; new exposure is HALTED" if reconcile["halt_new_exposure"] else "") + ".", "CALCULATED", "execution"))
    else:
        cards.append(card("reconcile", "Reconciliation", "FAIL" if reconcile.get("state") == "ERROR" else "NO_DATA", reconcile.get("error") or reconcile.get("empty", ""), "MISSING", "execution"))
    cards.append(card("testnet", "Testnet session", testnet["pill"], {"NOT_RUN": "NOT_RUN: no testnet session has run. This is not a pass."}.get(testnet["session_state"], f"Session state {testnet['session_state']}. TESTNET results are not owner profit."),
                      testnet.get("run_basis") or "MISSING", "testnet"))
    core = [c for c in cards if c["id"] in ("safety", "data", "operations")]
    if coh["verdict"] == "INCONSISTENT_RUN" or (rc_ok and reconcile["halt_new_exposure"]):
        head = ("FAIL", "INCONSISTENT_RUN: artifacts from different runs" if coh["verdict"] == "INCONSISTENT_RUN" else "New exposure is halted by the reconcile report")
    elif any(c["status"] == "FAIL" for c in core):
        head = ("FAIL", "Needs attention now")
    elif any(c["status"] == "ATTENTION" for c in core):
        head = ("ATTENTION", "Running, with items that need a look")
    elif any(c["status"] == "NO_DATA" for c in core):
        head = ("NO_DATA", "Not enough data to say the system is operating correctly")
    else:
        head = ("OK", "Checks that ran are passing")
    permitted = [a["label"] for a in actions["items"] if a["permitted"]]
    ctx = {"coh": coh, "F": F, "pipeline": pipeline, "portfolio": portfolio, "execution": execution, "testnet": testnet, "accounting": accounting, "reconcile": reconcile, "risk": risk, "fresh": fresh,
           "art": art, "gate": gate, "actions": actions, "restrictions": restrictions, "live": bool(oc["environment"]["live_authorized"]), "capital": val(oc["capital"]["capital_usd"], "CALCULATED", "USD")}
    ctx["next_steps"] = _next_steps(ctx)
    overview = {"headline": {"status": head[0], "text": head[1]}, "answers": _answers(ctx), "next_steps": ctx["next_steps"], "cards": cards, "attention": att, "permitted_now": permitted,
                "blocked_now": [{"label": a["label"], "reasons": a["reasons"]} for a in actions["items"] if not a["permitted"]], "restrictions": restrictions, "incidents": incidents,
                "engine": {"mode": "REPLAY / PAPER; testnet only by the owner; live not authorised" if not oc["environment"]["live_authorized"] else "LIVE LOOKS ARMED: stop",
                           "run": coh["primary"], "pipeline_outcome": pipeline.get("outcome"), "halted": bool(risk.get("halted")) if risk.get("state") == "OK" else None},
                "market_data": {"verdict": fv or "NO_DATA", "state": fresh.get("state"), "readiness": fresh.get("readiness"), "collector": fresh.get("collector"),
                                "latest_cycle": fresh.get("latest_cycle"), "empty": fresh.get("empty"), "error": fresh.get("error")},
                "environment": {"flags": _env_flags(env), "live_authorized": bool(oc["environment"]["live_authorized"]), "note": "Presence only: 'set' or 'unset'. No value is read or shown."}}

    evidence = {"sources": [_stamp(a) for a in art.values()], "freshness": fresh, "universe_capture": capture,
                "owner_console": {"headline": oc["headline"], "evidence": {"status": oc["evidence"]["status"], "why": oc["evidence"].get("why") or oc["evidence"].get("reading")},
                                  "backup": {"status": oc["backup"]["status"], "why": oc["backup"].get("why") or oc["backup"].get("note")},
                                  "owner_pnl": {"status": oc["pnl"]["actual_realised"]["status"], "why": oc["pnl"]["actual_realised"].get("why")}},
                "coverage": fresh.get("datasets", []) if fresh.get("state") == "OK" else [],
                "digests": {"data_digest_parts": pipeline.get("data_digest_parts", {}) if pipeline.get("state") == "OK" else {}, "run_data_digest": (coh["primary"] or {}).get("data_digest"),
                            "capture_manifest_sha256": capture.get("manifest_sha256") if capture.get("state") == "OK" else None,
                            "evidence_basis": pipeline.get("evidence_basis", {}) if pipeline.get("state") == "OK" else {}},
                "operations": fresh.get("backups") if fresh.get("state") == "OK" else None,
                "software": {"code_revisions": coh["code_revisions"], "note": "identity.code_revision as each artifact declares it; unauthenticated"},
                "ci": {"state": "NOT_AVAILABLE_OFFLINE", "text": "CI status is not available offline: this page fetches nothing, so it cannot say whether CI is green."},
                "caveats": ["RAW_RESPONSES_VERIFIED means captured bytes are self-consistent; it is NOT authentication that the exchange produced them.",
                            "health.json in a data root is unauthenticated; an ok record in it that was not re-checked is never counted as HEALTHY.",
                            "Artifact files are unauthenticated local files; their basis is what they declare and is restricted per file."]}
    state = {"schema": SCHEMA, "as_of_ms": now_ms, "legend": {"basis": [{"code": k, "meaning": v} for k, v in BASIS_MEANING.items()],
                                                              "status": [{"code": k, "meaning": v} for k, v in STATUS_MEANING.items()]},
             "coherence": coh, "figures": F, "overview": overview, "research": research, "portfolio": portfolio, "execution": execution, "accounting": accounting, "risk": risk,
             "testnet": testnet, "evidence": evidence, "actions": actions,
             "environment": {"live_authorized": bool(oc["environment"]["live_authorized"]), "reason": oc["environment"]["reason"], "flags": overview["environment"]["flags"], "gate": gate,
                             "capital_usd": val(oc["capital"]["capital_usd"], "CALCULATED", "USD", oc["capital"]["note"])}}
    return _scrub(_clean(state), env)


_HIDE_MIN = {"LIVE_TRADING_ACK": 3, "BYBIT_API_KEY": 3, "BYBIT_API_SECRET": 3, "BYBIT_TESTNET_API_KEY": 3, "BYBIT_TESTNET_API_SECRET": 3, "OWNER_ATTESTATION_KEY": 3, "USE_TESTNET": 4, "PAPER_TRADING": 4, "BYBIT_VENUE": 4}


def _scrub(o: Any, env: Mapping[str, str]) -> Any:
    """Presence only: a variable's VALUE never appears in the state, not even inside a message another module composed (e.g. 'PAPER_TRADING=...is not a recognised boolean')."""
    hide = sorted({str(env.get(n)).strip() for n, k in _HIDE_MIN.items() if env.get(n) and len(str(env.get(n)).strip()) >= k}, key=len, reverse=True)
    if not hide:
        return o

    def walk(x: Any) -> Any:
        if isinstance(x, str):
            for v in hide:
                x = x.replace(v, "[value hidden]")
            return x
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return x
    return walk(o)


def _clean(o: Any) -> Any:
    """JSON-safe: NaN/inf become null (unknown), tuples lists, keys strings, unknown types strings. Nothing is guessed."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_clean(v) for v in o]
    if o is None or isinstance(o, (str, int, bool)):
        return o
    return str(o)


# ======================================================================== HTTP (GET-only, loopback only)
CSP = "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


def host_ok(host_header: Optional[str], port: int) -> bool:
    """DNS-rebinding defence: the Host header must name this loopback listener exactly (127.0.0.1 or localhost, optional matching port)."""
    if not host_header or len(host_header) > 64:
        return False
    m = re.fullmatch(r"(127\.0\.0\.1|localhost)(?::(\d{1,5}))?", host_header.strip().lower())
    return bool(m) and (m.group(2) is None or int(m.group(2)) == port)


class _Config:
    def __init__(self, data_root: Optional[str], universe_dir: Optional[str], artifacts_dir: Optional[str], with_economics: bool, verify: bool,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000), ttl_ms: int = 5000, env: Optional[Mapping[str, str]] = None, testnet_dir: Optional[str] = None) -> None:
        self.testnet_dir = testnet_dir
        self.data_root, self.universe_dir, self.artifacts_dir, self.with_economics, self.verify, self.clock, self.ttl_ms = data_root, universe_dir, artifacts_dir, with_economics, verify, clock, ttl_ms
        self.env = os.environ if env is None else env
        self._lock = threading.Lock()
        self._econ: Optional[Dict[str, Any]] = None
        self._cache: Optional[Tuple[int, bytes]] = None

    def state_bytes(self) -> bytes:
        with self._lock:                                             # one build at a time; the build is read-only and a few seconds at most
            now = self.clock()
            if self._cache and now - self._cache[0] < self.ttl_ms:
                return self._cache[1]
            if self.with_economics and self._econ is None:
                self._econ = CR.btc_carry_economics()
            st = build_state(data_root=self.data_root, universe_dir=self.universe_dir, artifacts_dir=self.artifacts_dir, env=self.env, now_ms=now,
                             with_economics=False, economics=self._econ, verify=self.verify, testnet_dir=self.testnet_dir)
            body = json.dumps(st, allow_nan=False, sort_keys=True).encode("utf-8")
            self._cache = (now, body)
            return body


def make_handler(cfg: _Config) -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ControlCentre"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:       # noqa: A002 - silenced: no request line, header or env value is ever logged
            return

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            if code == 405:
                self.send_header("Allow", "GET")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def send_error(self, code: int, message: Optional[str] = None, explain: Optional[str] = None) -> None:
            code = 405 if code == 501 else code                        # an unknown method is a method-not-allowed, not "unimplemented"
            self._send(code, json.dumps({"error": http_status(code)}).encode(), "application/json")

        def do_GET(self) -> None:
            port = self.server.server_address[1]
            if not host_ok(self.headers.get("Host"), port):
                return self._send(403, b'{"error":"forbidden host"}', "application/json")
            path = self.path.split("?", 1)[0].split("#", 1)[0]          # a query string is never used
            if path == "/" or path == "/index.html":
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/health":
                return self._send(200, b'{"ok":true}', "application/json")
            if path == "/api/state":
                try:
                    return self._send(200, cfg.state_bytes(), "application/json")
                except Exception as exc:                              # noqa: BLE001 - the page shows an error state; the traceback is not served
                    return self._send(500, json.dumps({"error": "state could not be built", "kind": type(exc).__name__}).encode(), "application/json")
            return self._send(404, b'{"error":"not found"}', "application/json")

        def _static(self, name: str) -> None:
            if name not in STATIC_FILES:                               # exact allow-list: no decoding, no separators, no traversal
                return self._send(404, b'{"error":"not found"}', "application/json")
            try:
                with open(os.path.join(STATIC_DIR, name), "rb") as fh:
                    body = fh.read()
            except OSError:
                return self._send(404, b'{"error":"not found"}', "application/json")
            self._send(200, body, STATIC_FILES[name])

        def _no(self) -> None:
            self._send(405, b'{"error":"method not allowed"}', "application/json")
        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _no

        def do_HEAD(self) -> None:
            self._no()
    return Handler


def http_status(code: int) -> str:
    return {400: "bad request", 403: "forbidden", 404: "not found", 405: "method not allowed", 414: "uri too long", 431: "headers too large", 500: "error"}.get(code, "error")


def make_server(cfg: _Config, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    if host != "127.0.0.1":
        raise ValueError("the control centre binds 127.0.0.1 only (refusing host %r)" % host)
    srv = ThreadingHTTPServer((host, port), make_handler(cfg))
    srv.daemon_threads = True
    return srv


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only owner control centre (127.0.0.1, GET only). No controls, no outbound request.")
    ap.add_argument("--data-root", default=None, help="private market-data root (tools/market_collector.py output)")
    ap.add_argument("--universe-dir", default=None, help="a schema-v2 universe capture directory")
    ap.add_argument("--artifacts-dir", default=None, help="directory holding the multi-symbol JSON artifacts (see the module docstring)")
    ap.add_argument("--testnet-dir", default=None, help="directory holding testnet_session.json: the session tool's --state-dir (default: --artifacts-dir)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1", help="only 127.0.0.1 is accepted")
    ap.add_argument("--with-economics", action="store_true", help="also load the in-sample corpus economics once (about 15 s)")
    ap.add_argument("--verify", action="store_true", help="re-hash datasets in the freshness audit (slow on a large root)")
    ap.add_argument("--print-state", action="store_true", help="print the state JSON and exit")
    a = ap.parse_args(argv)
    cfg = _Config(a.data_root, a.universe_dir, a.artifacts_dir, a.with_economics, a.verify, ttl_ms=0 if a.print_state else 5000, testnet_dir=a.testnet_dir)
    if a.print_state:
        sys.stdout.write(cfg.state_bytes().decode() + "\n")
        return 0
    try:
        srv = make_server(cfg, a.host, a.port)
    except (ValueError, OSError) as exc:
        print(f"control_centre: {exc}", file=sys.stderr)
        return 2
    print(f"control centre on http://127.0.0.1:{srv.server_address[1]}/  (read-only; Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
