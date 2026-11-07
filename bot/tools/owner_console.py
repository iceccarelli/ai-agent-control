"""One offline read-out for the owner: is anything armed, how stale is the evidence, what may capital be, and what is NOT known.

    python3 tools/owner_console.py [--funding prints.jsonl] [--backup FILE] [--now-ms N]

Offline and read-only. It makes no request, reads no credential VALUE (secrets are reported as set/unset only), opens no database except a backup file you
name (and only to verify it), and never writes. It re-uses the repository's own authorities instead of restating them: ``config.is_live_authorized`` for
whether real money is armed, ``carry_prospective.progress`` for the registered evaluation, ``promotion_gate`` for the live chain, ``capital_allocator`` for
what capital is, ``state_backup.verify`` for a backup. Every section carries ONE status word from a closed vocabulary, so a number is never read as more than
it is:

    VERIFIED     checked here against its authority and agrees
    ESTIMATED    a model or assumption produced it; not observed
    PROVISIONAL  observed but incomplete or not yet reconciled
    STALE        was available, is older than it should be
    UNAVAILABLE  nothing was supplied or it could not be read; NOT zero, NOT fine

Capital is the allocator's answer with no strategy evidence, which is ZERO; this tool cannot raise it. Eligibility is four SEPARATE questions (research,
simulated, testnet, owner-authorised) and only the last one can ever authorise anything, and only through ``config.is_live_authorized`` AND the promotion gate.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import account_review as AR  # noqa: E402
import capital_allocator as CA  # noqa: E402
import carry_prospective as CP  # noqa: E402
import config as C  # noqa: E402
import promotion_gate as PG  # noqa: E402
import venues  # noqa: E402

VERIFIED, ESTIMATED, PROVISIONAL, STALE, UNAVAILABLE = "VERIFIED", "ESTIMATED", "PROVISIONAL", "STALE", "UNAVAILABLE"
STATUSES = (VERIFIED, ESTIMATED, PROVISIONAL, STALE, UNAVAILABLE)
SETTLE_MS = CP.SETTLE_MS
STALE_AFTER_SETTLEMENTS = 2                  # the newest print should be no older than two settlement intervals while the window is open
_SECRET_NAMES = ("BYBIT_API_KEY", "BYBIT_API_SECRET", "LIVE_TRADING_ACK")
_SAFE_VALUE_NAMES = ("USE_TESTNET", "PAPER_TRADING", "BYBIT_VENUE", "CARRY_DRILL_ENABLED", "CARRY_ENTRY_BENEFIT_MODE")


def _now_ms() -> int:
    return int(time.time() * 1000)


def section_environment(env: Mapping[str, str]) -> Dict[str, Any]:
    """Safety flags by VALUE, secrets by presence only. Live authority is asked of ``config.is_live_authorized`` and nothing else."""
    values = {k: env.get(k) for k in _SAFE_VALUE_NAMES if env.get(k) not in (None, "")}
    secrets = {k: ("set" if str(env.get(k, "") or "").strip() else "unset") for k in _SECRET_NAMES}
    armed, why = C.is_live_authorized(dict(env))
    findings: List[str] = []
    if armed:
        findings.append("REAL MONEY IS ARMED by this environment: stop and check it is intended")
    if secrets["LIVE_TRADING_ACK"] == "set":
        findings.append("LIVE_TRADING_ACK is set; this repository's rule is that it stays unset")
    venue = env.get("BYBIT_VENUE")
    if venue not in (None, "") and str(venue).strip().lower() not in venues.sandbox_names() + venues.live_names():
        findings.append(f"BYBIT_VENUE {venue!r} is not a qualified venue (an unqualified or unknown name is refused everywhere)")
    return {"status": VERIFIED if not findings else PROVISIONAL, "live_authorized": bool(armed), "reason": why,
            "values": values, "secrets": secrets, "findings": findings}


def section_evidence(funding_path: Optional[str], now_ms: int) -> Dict[str, Any]:
    """The registered prospective evaluation's progress (it evaluates nothing) and how fresh the newest print is."""
    if not funding_path:
        return {"status": UNAVAILABLE, "why": "no prints file supplied (--funding); make one with tools/prospective_prints.py"}
    try:
        prints = CP._load(funding_path)
        progress = CP.progress(prints, now_ms)
    except (AR.RecordError, OSError, ValueError) as exc:
        return {"status": UNAVAILABLE, "why": f"prints file unreadable ({type(exc).__name__}: {exc})"}
    newest = max((t for t, _v in prints), default=None)
    age_h = None if newest is None else round((now_ms - newest) / 3_600_000, 2)
    window_open = progress["state"] == "WINDOW_OPEN"
    stale = window_open and (newest is None or now_ms - newest > STALE_AFTER_SETTLEMENTS * SETTLE_MS)
    status = STALE if stale else (PROVISIONAL if window_open or progress["state"] == "WINDOW_NOT_OPEN" else VERIFIED)
    return {"status": status, "window_state": progress["state"], "newest_print_age_hours": age_h, "coverage": progress["coverage"],
            "required_funding_bps": progress["required_funding_bps"], "needed_settlements": progress["needed_settlements_for_min_edge"],
            "needed_exceeds_window": progress["needed_exceeds_window"],
            "note": "progress only: no trial recorded, no verdict. A closed-window verdict comes from tools/trial_journal.py alone.",
            "reading": progress["reading"]}


def section_capital() -> Dict[str, Any]:
    """What the allocator says capital is when no strategy has evidence. This tool cannot supply evidence, so this is always zero."""
    limits = CA.AllocationLimits(free_capital_usd=CA.Sourced(0.0, "owner_console: no capital declared"), max_total_usd=0.0, per_strategy_max_usd={},
                                 max_total_loss_usd=0.0, max_total_drawdown_contribution=0.0, max_group_usd=0.0,
                                 book_tail_risk_usd=CA.Sourced(0.0, "owner_console: no capital declared"))
    plan = CA.allocate([], limits, risk_aversion=1.0, evidence=None)
    return {"status": VERIFIED, "capital_usd": float(plan.total_allocated_usd), "reasons": list(plan.reasons),
            "note": "capital is zero until a strategy has an ALLOCATE verdict from strategy_evidence; this tool does not and cannot grant it"}


def section_eligibility(env: Mapping[str, str]) -> Dict[str, Any]:
    """Four separate questions. Nothing here implies the next."""
    armed, _why = C.is_live_authorized(dict(env))
    try:
        gate = bool(PG.promotion_gate_allows_live())
    except Exception as exc:                                         # noqa: BLE001 - an unreadable gate is a closed gate
        gate = False
    return {
        "research": {"status": VERIFIED, "met": True, "meaning": "hypotheses may be studied offline; this authorises nothing"},
        "simulated": {"status": UNAVAILABLE, "met": False, "meaning": "needs a passing offline lifecycle run (the test suite); not assessed by this read-out"},
        "testnet": {"status": UNAVAILABLE, "met": False, "meaning": "needs an owner-run, owner-collected testnet session; none is recorded here"},
        "owner_authorised": {"status": VERIFIED, "met": bool(armed and gate), "live_authorized": bool(armed), "promotion_gate_allows_live": gate,
                             "meaning": "requires config.is_live_authorized AND the signed promotion gate; both are owner acts"},
    }


def section_backup(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {"status": UNAVAILABLE, "why": "no backup named (--backup); nothing here schedules or stores one (RUNBOOKS: Backups)"}
    import state_backup as SB
    rep = SB.verify(path)
    return {"status": VERIFIED if rep.get("ok") else PROVISIONAL, "ok": bool(rep.get("ok")), "problems": rep.get("problems", []),
            "note": "verify only compares the file with its manifest and runs integrity_check; it does not prove the backup is recent or restorable onto the live path"}


def section_testnet(env: Mapping[str, str]) -> Dict[str, Any]:
    """The offline testnet qualification (guard probes) and the count of venue facts still unread. Never 'certified': only an owner-run bounded preflight can read the venue."""
    import testnet_certificate as TCERT
    rep = TCERT.build(env)
    failing = [p["probe"] for p in rep["section_a_offline_probes"] if p["status"] != TCERT.PASS]
    return {"status": PROVISIONAL, "offline_qualification": rep["offline_qualification"], "failing_probes": failing,
            "venue_facts_not_read": len(rep["section_b_venue_facts"]), "testnet_certified": False,
            "note": "guard logic only; see tools/testnet_certificate.py for the bounded plan the owner would authorise"}


def section_risk() -> Dict[str, Any]:
    """The risk policy is PROPOSED, not active: no limit is owner-approved, so ``risk_policy.evaluate`` refuses everything. Shows the data-derived proposals."""
    import risk_policy as RP
    import risk_policy_proposal as RPP
    try:
        prop = RPP.propose(RPP.load_bps(RPP.DEFAULT_FUNDING), notional_usd=100.0)
    except (OSError, ValueError, KeyError) as exc:
        return {"status": UNAVAILABLE, "why": f"no proposal could be derived ({type(exc).__name__})", "policy_active": False}
    bal = next(o for o in prop["strategy_drawdown_funding_only"]["options"] if o["setting"] == "balanced")
    return {"status": ESTIMATED, "policy_active": False, "evaluation_without_an_approved_policy": RP.evaluate(None, RP.State()).reasons,
            "proposed_strategy_drawdown_bps_balanced": bal["strategy_drawdown_limit_bps_of_notional"],
            "proposed_max_hold_days": prop["max_hold"]["recommended_days"], "break_even_days_at_last_365d_mean": prop["max_hold"]["break_even_days"],
            "share_of_history_below_hurdle_requirement": prop["persistent_shortfall"]["share_of_history_below_required_trailing_90_prints"],
            "unmeasured_limits": prop["unmeasured_limits"], "decision": prop["decision"]}


def section_pnl() -> Dict[str, Any]:
    return {"actual_realised": {"status": UNAVAILABLE, "why": "no owner-account export is supplied to this read-out; run tools/owner_ledger.py on an export you hold"},
            "modelled": {"status": ESTIMATED, "why": "any carry figure from the research tools is a model, never a result; keep it apart from the actual line above"}}


def build(env: Mapping[str, str], *, funding: Optional[str] = None, backup: Optional[str] = None, now_ms: Optional[int] = None) -> Dict[str, Any]:
    now = _now_ms() if now_ms is None else min(int(now_ms), _now_ms())          # a caller can only move the clock earlier
    rep = {"environment": section_environment(env), "evidence": section_evidence(funding, now), "capital": section_capital(),
           "eligibility": section_eligibility(env), "testnet": section_testnet(env), "risk": section_risk(), "backup": section_backup(backup), "pnl": section_pnl(), "as_of_ms": now}
    for k, v in rep.items():
        if isinstance(v, dict) and "status" in v and v["status"] not in STATUSES:
            raise AssertionError(f"section {k} has a status outside the vocabulary")
    rep["headline"] = (f"live={'ARMED' if rep['environment']['live_authorized'] else 'off'}; capital=${rep['capital']['capital_usd']:.0f}; "
                       f"evidence={rep['evidence']['status']}; testnet_offline={rep['testnet']['offline_qualification']}; backup={rep['backup']['status']}; actual P&L={rep['pnl']['actual_realised']['status']}")
    return rep


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Offline owner read-out. No request, no credential value, no write.")
    ap.add_argument("--funding", default=None, help="a prints file made by tools/prospective_prints.py")
    ap.add_argument("--backup", default=None, help="a state backup file (verified against its manifest)")
    ap.add_argument("--now-ms", type=int, default=None)
    a = ap.parse_args(argv)
    try:
        rep = build(os.environ, funding=a.funding, backup=a.backup, now_ms=a.now_ms)
    except (AR.RecordError, OSError) as exc:
        print(f"owner_console: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(rep, indent=2, sort_keys=True, default=str, allow_nan=False))
    return 3 if rep["environment"]["live_authorized"] else 0


if __name__ == "__main__":
    sys.exit(main())
