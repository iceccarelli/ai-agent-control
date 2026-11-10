#!/usr/bin/env python3
"""UNATTENDED Bybit TESTNET carry sessions: fake money, a bounded, owner-signed, expiring grant, the same engine and every guard that is not the typed digest.

WHAT IT DOES (one cycle = `once`; `loop` repeats it):
  1. loads the OWNER'S GRANT (`ms_autopilot_grant.json`, 0600, next to the capability file): bounds on sessions per UTC day, notional, hold, minimum funding, symbols, an expiry, bound to
     the manifest's plan_sha256 and a digest. The owner creates it with `grant` and a reference they type; nothing here can create it unattended;
  2. reads the PUBLIC funding rate and the next settlement and goes only if the funding would PAY for the trade (rate >= min_funding_bps; a short perp collects positive funding) and the
     settlement is close enough that hold_s = seconds to it + a margin fits the grant (so the hedge is open ACROSS the settlement, then unwinds);
  3. `ms_testnet_session` preflight -> plan -> assess (every leg and the whole group must be approved) -> `execute --unattended` with the plan's own digest;
  4. reads the session record and reports FLAT / NOT_FLAT. NOT_FLAT stops the loop and writes an ALERT file: a human looks.

WHAT IT CANNOT DO: touch mainnet (the venue registry pins the Testnet host; this module never sets a live switch), exceed a manifest or ceiling bound, exceed the grant, run without a
valid unexpired grant, run past the manifest's entries-per-UTC-day cap (the shared entries file counts these executes too), run after `touch <root>/STOP`, or place an order while the
per-state-dir kill switch (MS_KILL) is engaged. The caps, loss limit, authority, hedge-keeping rule and flat verification of the engine are unchanged.

WHAT IT DOES NOT PROMISE: profit. Testnet money is fake. One round trip costs about 31 bps in taker fees; the funding gate exists so sessions only run when the funding can cover it.
It must run on a host that can reach Bybit: GitHub-hosted runners are blocked by Bybit's CDN (see .github/workflows/carry-drill-testnet.yml); a Codespace, a small server or a
self-hosted runner works.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import shutil
import signal
import stat
import sys
import threading
import time
import uuid
from decimal import Decimal as D
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bybit_connection as bc  # noqa: E402
import ms_capital as CC  # noqa: E402
import ms_edge_gate as EG  # noqa: E402
import ms_engine_state as ES  # noqa: E402
import ms_testnet_adapter as AD  # noqa: E402
import ms_testnet_executor as TE  # noqa: E402
import ms_testnet_session as S  # noqa: E402
import ms_testnet_transport as TT  # noqa: E402

GRANT_SCHEMA = "ms_autopilot_grant/v1"
GRANT_KEYS = frozenset({"schema", "reference", "created_ms", "expires_ms", "manifest_plan_sha256", "symbols", "max_sessions_per_day", "max_notional_usd", "max_hold_s",
                        "min_funding_bps", "allow_mechanics_sessions", "grant_sha256"})
MAX_GRANT_DAYS = 30
MAX_GRANT_BYTES = 65_536
MAX_HOLD_S = 3300                       # the session's lifecycle is one hour: hold + 60 s entry wait + 180 s exit wait must fit
EXIT_MARGIN_S = 120                     # the hedge is held this long PAST the settlement
MIN_SECONDS_TO_SETTLEMENT = 90          # entry (up to 60 s) must complete before the settlement
POLL_S = 120                            # a cheap public read between cycles
COOLDOWN_AFTER_SESSION_S = 1800
MAX_CONSECUTIVE_ERRORS = 3
DEFAULT_ROOT = os.path.join(os.path.expanduser("~"), ".ai-agent-control", "ms_autopilot")


# SIGTERM / SIGHUP / SIGINT (a Fly stop or deploy, a closed terminal, Ctrl-c): stop starting sessions and ask the ACTIVE session to unwind. The session already honours its per-state-dir
# kill switch (cancel entries, reduce-only unwind, verify flat), so the handler only engages that switch; the process then ends when the session does (Fly's kill_timeout is 300 s).
_STOP = threading.Event()
_ACTIVE: Dict[str, Optional[str]] = {"dir": None}


def _on_signal(signum: int, _frame: Any = None) -> None:
    _STOP.set()
    d = _ACTIVE.get("dir")
    if d:
        try:
            AD.engage_kill_switch(os.path.join(d, "MS_KILL"), f"autopilot received signal {signum}")
        except OSError:
            pass


def install_signal_handlers() -> None:
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, _on_signal)


def grant_path() -> str:
    return os.path.join(os.path.dirname(AD.DEFAULT_CAPABILITY_PATH), "ms_autopilot_grant.json")


def _sha(body: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps({k: v for k, v in body.items() if k != "grant_sha256"}, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def load_grant(now_ms: int, manifest: Mapping[str, Any], path: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """(grant, reasons). Reasons non-empty = no grant: the autopilot does nothing. Never raises."""
    p = path or grant_path()
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return None, ["GRANT_MISSING"]
    except OSError:
        return None, ["GRANT_UNREADABLE"]
    if not stat.S_ISREG(st.st_mode):
        return None, ["GRANT_NOT_A_REGULAR_FILE"]
    if st.st_mode & 0o077:
        return None, ["GRANT_FILE_NOT_PRIVATE"]
    try:
        with open(p, "rb") as fh:
            raw = fh.read(MAX_GRANT_BYTES + 1)
        if len(raw) > MAX_GRANT_BYTES:
            return None, ["GRANT_TOO_LARGE"]
        g = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None, ["GRANT_UNREADABLE"]
    why: List[str] = []
    if not isinstance(g, dict) or set(g) != GRANT_KEYS:
        return None, ["GRANT_SHAPE"]
    if g["schema"] != GRANT_SCHEMA:
        why.append("GRANT_SCHEMA")
    if g["grant_sha256"] != _sha(g):
        why.append("GRANT_DIGEST_MISMATCH")
    if not (isinstance(g["reference"], str) and 1 <= len(g["reference"].strip()) <= 120):
        why.append("GRANT_REFERENCE")
    if not (isinstance(g["created_ms"], int) and isinstance(g["expires_ms"], int) and not isinstance(g["created_ms"], bool) and not isinstance(g["expires_ms"], bool)):
        why.append("GRANT_TIMES")
    else:
        if g["expires_ms"] <= now_ms:
            why.append("GRANT_EXPIRED")
        if g["expires_ms"] - g["created_ms"] > MAX_GRANT_DAYS * 86_400_000 or g["expires_ms"] <= g["created_ms"]:
            why.append("GRANT_LIFETIME")
    if g["manifest_plan_sha256"] != manifest.get("plan_sha256"):
        why.append("GRANT_IS_FOR_ANOTHER_MANIFEST")
    syms = g["symbols"]
    if not (isinstance(syms, list) and syms and all(isinstance(x, str) for x in syms) and set(syms) <= set(manifest.get("symbol_allowlist", []))):
        why.append("GRANT_SYMBOLS")
    b = manifest.get("bounds", {})
    ms = g["max_sessions_per_day"]
    if not (isinstance(ms, int) and not isinstance(ms, bool) and 1 <= ms <= int(b.get("max_entries_per_utc_day", 0))):
        why.append("GRANT_MAX_SESSIONS")
    if not (_num(g["max_notional_usd"]) and 0 < float(g["max_notional_usd"]) <= min(AD.AGGREGATE_CEILING_USD, float(b.get("aggregate_max_notional_usd", 0)))):
        why.append("GRANT_MAX_NOTIONAL")
    if not (isinstance(g["max_hold_s"], int) and not isinstance(g["max_hold_s"], bool) and 0 <= g["max_hold_s"] <= MAX_HOLD_S):
        why.append("GRANT_MAX_HOLD")
    if not (_num(g["min_funding_bps"]) and float(g["min_funding_bps"]) >= 0):
        why.append("GRANT_MIN_FUNDING")
    if not isinstance(g["allow_mechanics_sessions"], bool):
        why.append("GRANT_ALLOW_MECHANICS")
    return (None, why) if why else (g, [])


def unattended_refusals(now_ms: int, plan: Mapping[str, Any], manifest: Mapping[str, Any]) -> List[str]:
    """Why an `execute --unattended` of THIS plan is refused (empty = allowed): a valid grant AND the plan inside its bounds AND a session left today."""
    g, why = load_grant(now_ms, manifest)
    if g is None:
        return why
    out: List[str] = []
    if plan.get("symbol") not in g["symbols"]:
        out.append("PLAN_SYMBOL_NOT_GRANTED")
    if not (_num(plan.get("notional_usd")) and float(plan["notional_usd"]) <= float(g["max_notional_usd"]) + 1e-9):
        out.append("PLAN_NOTIONAL_ABOVE_GRANT")
    hold = (plan.get("timing") or {}).get("hold_s")
    if not (isinstance(hold, int) and not isinstance(hold, bool) and 0 <= hold <= int(g["max_hold_s"])):
        out.append("PLAN_HOLD_ABOVE_GRANT")
    if S.entries_today(now_ms) >= int(g["max_sessions_per_day"]):
        out.append("GRANT_SESSIONS_PER_DAY_REACHED")
    return out


def write_provenance(state_dir: str, now_ms: int, plan: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    """`autopilot.json` in the state dir: that no human typed this digest, and under which grant. (The session record has a closed schema the control centre reads.)"""
    g, _ = load_grant(now_ms, manifest)
    S._write_json(os.path.join(state_dir, "autopilot.json"), {"mode": "UNATTENDED_STANDING_AUTHORISATION", "at_ms": now_ms, "plan_digest": plan.get("plan_digest"),
                                                              "grant_reference": (g or {}).get("reference"), "grant_sha256": (g or {}).get("grant_sha256")})


# ---------------------------------------------------------------------------------------------------- the funding gate (pure)
def funding_decision(rate: float, next_funding_ms: int, now_ms: int, grant: Mapping[str, Any]) -> Tuple[bool, int, str]:
    """(go, hold_s, reason). A short perp COLLECTS positive funding: go only if the rate pays at least ``min_funding_bps`` and the next settlement is near enough to hold ACROSS it."""
    bps = float(rate) * 1e4
    if not math.isfinite(bps) or bps < float(grant["min_funding_bps"]) or bps <= 0:
        return False, 0, f"FUNDING_BELOW_THRESHOLD:{bps:.2f}bps<{float(grant['min_funding_bps']):.2f}"
    secs = (int(next_funding_ms) - int(now_ms)) / 1000.0
    if secs < MIN_SECONDS_TO_SETTLEMENT:
        return False, 0, f"SETTLEMENT_TOO_CLOSE:{secs:.0f}s"
    hold = int(math.ceil(secs)) + EXIT_MARGIN_S
    if hold > int(grant["max_hold_s"]):
        return False, 0, f"SETTLEMENT_TOO_FAR:{secs:.0f}s (hold {hold}s > grant {int(grant['max_hold_s'])}s)"
    return True, hold, f"FUNDING_OK:{bps:.2f}bps settlement in {secs:.0f}s"


def _executor(env: Mapping[str, str], inner: Any, clock: Callable[[], int], symbol: str) -> Any:
    """A read-only TestnetExecutor view (the registry-pinned Testnet host, no redirects) for the public reads and the one signed fee-rate read."""
    client, _t = TT.build_client(TT.TestnetCredentials.from_mapping(env), inner if inner is not None else TT.NoRedirectInner())
    return TE.TestnetExecutor(client, symbols=[symbol], spot_symbols=[symbol], clock_ms=clock, kill_reader=lambda: (False, "read only"))


def fetch_funding(env: Mapping[str, str], inner: Any, clock: Callable[[], int], symbol: str) -> Tuple[float, int]:
    """PUBLIC read of the perp's funding rate and next settlement on the Testnet host."""
    row = _executor(env, inner, clock, symbol)._get("/v5/market/tickers", {"category": "linear", "symbol": symbol}, signed=False)["list"][0]
    return float(row["fundingRate"]), int(row["nextFundingTime"])


MARKET_QUALITY_BLOCKERS = ("BOOK", "NOT_TRADING", "INSTRUMENT_INVALID", "MODEL_REFUSED", "QTY", "NOTIONAL")


def classify_session(verdict: EG.EdgeVerdict, rate: D, grant: Mapping[str, Any]) -> Tuple[Optional[str], List[str]]:
    """(session_class, why_not). PROFIT_SEEKING only for an EG.PROFIT_CANDIDATE whose funding also clears the grant's own floor. TESTNET_MECHANICS (a bounded fake-money measurement
    of lifecycle, fees and fills, NEVER an opportunity) only when the OWNER allowed it in the grant, the market is usable and the funding is not negative. Anything else is no session."""
    floor_ok = rate * EG.BPS >= D(str(grant["min_funding_bps"]))
    if verdict.session_class == EG.PROFIT_CANDIDATE and floor_ok:
        return "PROFIT_SEEKING", []
    why = [f"EDGE_{verdict.session_class}"] + list(verdict.reasons)[:4]
    if grant["allow_mechanics_sessions"] is True and rate >= 0 and not any(b in r for r in verdict.reasons for b in MARKET_QUALITY_BLOCKERS):
        return "TESTNET_MECHANICS", why
    return None, why


# ---------------------------------------------------------------------------------------------------- one cycle
def _res(result: str, why: Sequence[str] = (), **kw: Any) -> Dict[str, Any]:
    return {"result": result, "why": list(why), **kw}


def _noop_state(_state: str, _source: str) -> None:
    return None


def run_once(env: Mapping[str, str], *, inner: Any = None, clock: Optional[Callable[[], int]] = None, sleep: Optional[Callable[[float], None]] = None,
             out: Callable[[str], None] = print, state_root: Optional[str] = None, engine_root: Optional[str] = None, lease: Any = None,
             on_state: Callable[[str, str], None] = _noop_state, dry_run: bool = False, **_ignored: Any) -> Dict[str, Any]:
    clock = clock or (lambda: int(time.time() * 1000))
    sleep = sleep or time.sleep
    now = int(clock())
    on_state("DATA_CHECK", "cycle_start")
    manifest, st = S._load_capability_manifest()
    if manifest is None:
        return _res("REFUSED", ["OWNER_CAPABILITY_FILE:" + ",".join(st.get("reasons", []))])
    g, why = load_grant(now, manifest)
    if g is None:
        return _res("REFUSED", why)
    p = TT.TestnetCredentials.presence(env)
    if not (p["dedicated_key_present"] and p["dedicated_secret_present"]):
        return _res("REFUSED", ["DEDICATED_TESTNET_CREDENTIALS_MISSING"])
    cap = min(int(g["max_sessions_per_day"]), int(manifest["bounds"]["max_entries_per_utc_day"]))
    if S.entries_today(now) >= cap:
        return _res("SKIPPED", ["DAILY_SESSIONS_REACHED"])
    sym = g["symbols"][0]
    try:
        rate, nxt = fetch_funding(env, inner, clock, sym)
    except (TE.ExecutorError, bc.BybitAPIError, TT.TestnetTransportRefused, KeyError, IndexError, ValueError, TypeError) as exc:
        return _res("ERROR", [f"FUNDING_READ_FAILED:{type(exc).__name__}"])
    go, hold_s, reason = funding_decision(rate, nxt, now, g)
    if not go:
        return _res("SKIPPED", [reason], funding_bps=rate * 1e4)
    on_state("RESEARCHING", "edge_gate")
    try:                                                              # the ECONOMICS gate: expected NET edge on the one economic model, from the venue's own rows, books and fee tier
        inp = EG.collect(_executor(env, inner, clock, sym), sym, D(str(g["max_notional_usd"])), hold_s, D(str(rate)), nxt, int(clock()))
        verdict = EG.evaluate_edge(inp)
    except (TE.ExecutorError, bc.BybitAPIError, TT.TestnetTransportRefused, StopIteration, KeyError, IndexError, ValueError, TypeError, ArithmeticError) as exc:
        return _res("ERROR", [f"EDGE_INPUTS_UNAVAILABLE:{type(exc).__name__}"])
    session_class, why_not = classify_session(verdict, D(str(rate)), g)
    if dry_run:                                                       # the owner's read-only `evaluate`: the verdict and the class, never a session
        return _res("EVALUATED", why_not, funding_bps=rate * 1e4, hold_s=hold_s, session_class=session_class, edge=verdict.as_dict())
    if session_class is None:
        return _res("SKIPPED", why_not, funding_bps=rate * 1e4, edge=verdict.as_dict())
    root = state_root or os.path.join(os.path.expanduser("~"), ".ai-agent-control")
    d = os.path.join(root, "ms_auto_" + time.strftime("%Y%m%dT%H%M%S", time.gmtime(now / 1000.0)))
    _ACTIVE["dir"] = d
    try:
        return _run_session(env, inner, clock, sleep, d, sym, g, hold_s, rate, session_class, verdict, manifest, engine_root or DEFAULT_ROOT, lease, on_state)
    finally:
        _ACTIVE["dir"] = None


def _run_session(env: Mapping[str, str], inner: Any, clock: Callable[[], int], sleep: Callable[[float], None], d: str, sym: str, g: Mapping[str, Any], hold_s: int,
                 rate: float, session_class: str, verdict: EG.EdgeVerdict, manifest: Mapping[str, Any], engine_root: str, lease: Any,
                 on_state: Callable[[str, str], None]) -> Dict[str, Any]:
    lines: List[str] = []

    def step(*argv: str, sl: Optional[Callable[[float], None]] = None, poc: Optional[Callable[[], Optional[str]]] = None) -> int:
        return S.main(["--state-dir", d, *argv], env=env, inner_transport=inner, clock_ms=clock, sleep=sl or sleep, input_fn=lambda _p: "", out=lines.append, pre_order_check=poc)

    if _STOP.is_set():
        return _res("SKIPPED", ["STOP_REQUESTED"], state_dir=d)
    if step("preflight") != 0:
        return _res("ERROR", ["PREFLIGHT_FAILED"], state_dir=d, detail=lines[-6:])
    pf = ES.read_json(os.path.join(d, "preflight.json")) or {}
    book = CC.CapitalBook(engine_root)
    usdt_now = pf.get("usdt")
    book.ensure_baseline(usdt_now)
    settled = book.settle(usdt_now)                                   # what the venue's cash REALLY did since the last verified-flat session (losses in full; gains only as far as explained)
    if settled["status"] == "UNEXPLAINED_CASH":                       # cash the ledger cannot explain: no new entries until a human has looked (the evidence stays in capital.json)
        ES.EngineState(engine_root, clock).set("ALERT", {"code": "ACCOUNT_CASH_UNEXPLAINED", "session_id": settled.get("session_id"), "delta_usd": settled.get("delta_usd")})
        return _res("REFUSED", ["ACCOUNT_CASH_UNEXPLAINED"], state_dir=d, capital=settled)
    cb = book.load()
    alloc = CC.allocate(grant_max_usd=g["max_notional_usd"], ceiling_usd=AD.AGGREGATE_CEILING_USD, open_gross_usd=0, available_usdt=usdt_now, basis=book.sizing_basis(cb), start_cash=CC._d(cb["start_cash_usd"]),
                        loss_streak=int(cb["loss_streak"]), drawdown_usd=book.drawdown(cb), profit_seeking=session_class == "PROFIT_SEEKING", halted=bool(cb["halted"]))
    S._write_json(os.path.join(d, "edge_verdict.json"), {"session_class": session_class, "label": "TESTNET_MECHANICS_NOT_AN_OPPORTUNITY" if session_class == "TESTNET_MECHANICS" else "PROFIT_SEEKING_CANDIDATE",
                                                          "edge": verdict.as_dict(), "allocation": alloc.as_dict(), "capital_settlement": settled})
    if alloc.notional <= 0:                                           # the allocator can only say LESS than the owner's limits; zero is a decision to stay idle
        return _res("SKIPPED", ["ALLOCATION_ZERO:" + alloc.binding], state_dir=d, allocation=alloc.as_dict())
    if step("plan", "--symbol", sym, "--notional-usd", str(alloc.notional), "--hold-seconds", str(hold_s)) != 0:
        return _res("SKIPPED", ["PLAN_REFUSED"], state_dir=d, detail=lines[-3:])
    try:
        with open(os.path.join(d, "plan.json"), encoding="utf-8") as fh:
            digest = json.load(fh)["plan_digest"]
    except (OSError, ValueError, KeyError):
        return _res("ERROR", ["PLAN_UNREADABLE"], state_dir=d)
    on_state("PLAN_PROPOSED", "plan_written")
    lines.clear()
    if step("assess") != 0:
        return _res("SKIPPED", ["ASSESS_FAILED"], state_dir=d, detail=lines[-3:])
    try:
        a = json.loads("\n".join(lines)[ "\n".join(lines).index("{"):])
        bad = [k for k, v in a["risk_per_leg"].items() if not v["approved"]] + ([] if a["risk_whole_group"]["approved"] else ["whole_group"])
    except (ValueError, KeyError):
        return _res("SKIPPED", ["ASSESS_UNREADABLE"], state_dir=d)
    if bad:
        return _res("SKIPPED", ["ASSESS_NOT_APPROVED:" + ",".join(bad)], state_dir=d)
    on_state("RISK_APPROVED", "assess_approved")
    lines.clear()
    if _STOP.is_set():                                               # a stop arrived before any order: nothing was placed, no entry was counted
        return _res("SKIPPED", ["STOP_REQUESTED"], state_dir=d)
    why_now = revocation_reason(int(clock()), manifest, engine_root, lease)
    if why_now is not None:                                          # authority vanished between the gates and the order: nothing is placed, no entry is counted
        return _res("SKIPPED", ["REVOKED_BEFORE_ORDER:" + why_now], state_dir=d)
    on_state("EXECUTING", "execute_started")
    watch_sleep, watch = _watching_sleep(sleep, d, clock, manifest, engine_root, lease)

    def pre_order() -> Optional[str]:
        if _STOP.is_set():
            return "STOP_SIGNAL"
        return revocation_reason(int(clock()), manifest, engine_root, lease)
    rc = step("execute", "--confirm-plan-digest", digest, "--unattended", sl=watch_sleep, poc=pre_order)
    try:
        with open(os.path.join(d, "testnet_session.json"), encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        rec = {}
    summary = {"state_dir": d, "plan_digest": digest, "hold_s": hold_s, "funding_bps": rate * 1e4, "flat_verified": rec.get("flat_verified"), "orders": len(rec.get("orders", [])),
               "net": rec.get("net"), "exit_code": rc, "tail": lines[-4:], "session_class": session_class, "edge": verdict.as_dict(), "revoked_reason": watch["reason"]}
    on_state("RECONCILING", "session_ended")
    net = rec.get("net") or {}
    fees, price, fund = (CC._d(net.get(k)) for k in ("fees_usd", "price_pnl_usd", "funding_usd"))
    known = None if (fees is None or price is None) else price - fees + (fund or D(0))
    book.park(os.path.basename(d), usdt_now, known, fund is not None, rec.get("flat_verified") is True)   # settled at the NEXT flat cash read, never from fill arithmetic alone
    summary["allocation"] = alloc.as_dict()
    if rc == S.EXIT_REFUSED:
        return _res("REFUSED", ["EXECUTE_REFUSED"], **summary)
    return _res("DONE" if rec.get("flat_verified") is True else "NOT_FLAT", [], **summary)


# ---------------------------------------------------------------------------------------------------- authority can be REVOKED while a session is running
def revocation_reason(now_ms: int, manifest: Mapping[str, Any], engine_root: str, lease: Any) -> Optional[str]:
    """Why new authority is gone RIGHT NOW (None = still authorised): the grant vanished / expired / was tampered, the owner set STOP or an ALERT appeared, or the executor lease is not
    held. A DRAIN is not revocation (the active session finishes). Revocation never abandons a hedge: it engages the session's kill switch (cancel entries, reduce-only unwind, flat check)."""
    g, why = load_grant(now_ms, manifest)
    if g is None:
        return "GRANT:" + (why[0] if why else "INVALID")
    st = ES.EngineState(engine_root, lambda: now_ms)
    for m in ("STOP", "ALERT"):
        if st.has(m):
            return m
    if lease is not None and not lease.held():
        return "LEASE_LOST"
    return None


def _watching_sleep(sleep: Callable[[float], None], state_dir: str, clock: Callable[[], int], manifest: Mapping[str, Any], engine_root: str, lease: Any
                    ) -> Tuple[Callable[[float], None], Dict[str, Any]]:
    """Wraps the session's own sleep: every poll of the running session first re-checks the authority, so a revocation is seen within one poll (about a second), not once per session.
    FAIL CLOSED: a watcher that cannot run is a revocation, an unengageable kill switch is retried on every poll and raises an ALERT, and nothing here ever raises into the session."""
    box: Dict[str, Any] = {"reason": None, "engaged": False}

    def engage() -> None:
        try:
            AD.engage_kill_switch(os.path.join(state_dir, "MS_KILL"), "revoked:" + str(box["reason"]))
            box["engaged"] = True
        except OSError:
            if not box.get("kill_alerted"):                                  # the session cannot be told to stop: a human must know NOW
                box["kill_alerted"] = True
                try:
                    ES.EngineState(engine_root, clock).set("ALERT", {"code": "KILL_SWITCH_UNENGAGEABLE", "state_dir": state_dir, "reason": str(box["reason"])})
                except OSError:
                    pass
        try:
            ES.write_json_atomic(os.path.join(state_dir, "revocation.json"), {"reason": box["reason"], "at_ms": int(clock()), "kill_engaged": box["engaged"]})
        except OSError:
            pass

    def watch(seconds: float) -> None:
        try:
            if box["reason"] is None:
                box["reason"] = revocation_reason(int(clock()), manifest, engine_root, lease)
            if box["reason"] is not None and not box["engaged"]:
                engage()
        except Exception as exc:                                             # noqa: BLE001 - any failure of the authority check is itself a revocation
            box["reason"] = box["reason"] or f"WATCHER_ERROR:{type(exc).__name__}"
            try:
                engage()
            except Exception:                                                # noqa: BLE001
                pass
        sleep(seconds)
    return watch, box


# ---------------------------------------------------------------------------------------------------- recovery: a restart is NOT permission to trade
def session_dirs(state_root: str) -> List[str]:
    try:
        return sorted(os.path.join(state_root, n) for n in os.listdir(state_root) if n.startswith("ms_auto_") and os.path.isdir(os.path.join(state_root, n)))
    except OSError:
        return []


def venue_flat_proof(ex: Any, symbol: str, baseline: Optional[float], entry_qty: Optional[float]) -> Tuple[bool, List[str], bool]:
    """(flat, reasons, venue_read_complete). FLAT is the venue's OWN complete position read: NO perp position is open on ANY symbol, the spot balance of ``symbol`` equals the session
    baseline within dust, and no order is open (ours or foreign). An incomplete read is not 'flat': it is unknown."""
    v = ex.venue_state()
    pos = v.get("positions")
    if not v.get("complete") or pos is None:
        return False, ["VENUE_READ_INCOMPLETE"], False
    why: List[str] = []
    for k, q in sorted(pos.items()):
        if k.startswith("linear:") and abs(q) >= 1e-12:
            why.append(f"PERP_POSITION_OPEN:{k[7:]}:{q}")
    if baseline is not None:
        dust = max(float(ex.filters("spot", symbol).qty_step), 0.002 * float(entry_qty or 0.0))
        delta = S._minus(pos.get(f"spot:{symbol}", 0.0), baseline)
        if abs(delta) > dust:
            why.append(f"SPOT_DELTA_OPEN:{symbol}:{delta}")
    if v.get("foreign_open_orders") or [o for o in v.get("orders", {}).values() if o.get("status") in ("New", "PartiallyFilled")]:
        why.append(f"OPEN_ORDERS:{symbol}")
    return (not why), why, True


def recover(env: Mapping[str, str], *, inner: Any = None, clock: Optional[Callable[[], int]] = None, state_root: Optional[str] = None, symbol: Optional[str] = None, **_k: Any
            ) -> Tuple[bool, List[str], Dict[str, Any]]:
    """(ok, reasons, info). ok = the venue PROVES the account flat for EVERY symbol the manifest allows (not just the one a grant names), relative to the last session's baseline, and no
    order is open. An unresolved prior session whose venue state is flat is recorded (recovery.json: accounting must be reconciled) and allowed; anything open, incomplete or unreadable
    is not ok (the caller pauses or raises an ALERT)."""
    clock = clock or (lambda: int(time.time() * 1000))
    root = state_root or os.path.join(os.path.expanduser("~"), ".ai-agent-control")
    manifest, st = S._load_capability_manifest()
    syms = [symbol] if symbol else list((manifest or {}).get("symbol_allowlist") or [])
    if manifest is None or not syms:
        return False, ["OWNER_CAPABILITY_FILE:" + ",".join(st.get("reasons", []))], {"venue_read_complete": False}
    last = (session_dirs(root) or [None])[-1]
    baselines: Dict[str, Optional[float]] = {}
    entry_qty = None
    unresolved = False
    if last:
        lim = ES.read_json(os.path.join(last, "run_limits.json")) or {}
        for sym in syms:
            b = (lim.get("baseline") or {}).get(f"spot:{sym}")
            baselines[sym] = float(b) if isinstance(b, (int, float)) and not isinstance(b, bool) else None
        plan = ES.read_json(os.path.join(last, "plan.json")) or {}
        try:
            entry_qty = float(plan["group"]["legs"][0]["qty"])
        except (KeyError, IndexError, TypeError, ValueError):
            entry_qty = None
        rec = ES.read_json(os.path.join(last, "testnet_session.json")) or {}
        jp = os.path.join(last, S.FILES["journal"])
        started = (os.path.exists(jp) and os.path.getsize(jp) > 0) or bool(rec.get("orders"))
        unresolved = bool(started and rec.get("flat_verified") is not True)
    all_why: List[str] = []
    complete_all = True
    for sym in syms:
        try:
            flat, why, complete = venue_flat_proof(_executor(env, inner, clock, sym), sym, baselines.get(sym), entry_qty)
        except (TE.ExecutorError, bc.BybitAPIError, TT.TestnetTransportRefused, TT.TestnetCredentialsMissing, KeyError, ValueError, TypeError) as exc:
            return False, [f"VENUE_UNREADABLE:{type(exc).__name__}"], {"venue_read_complete": False, "last_session": last, "unresolved": unresolved}
        complete_all = complete_all and complete
        for w in why:
            if w not in all_why:
                all_why.append(w)
    info = {"venue_read_complete": complete_all, "last_session": last, "unresolved": unresolved, "reconciled_ms": int(clock()), "symbols": syms}
    if all_why:
        return False, all_why, info
    if unresolved and last:
        ES.write_json_atomic(os.path.join(last, "recovery.json"), {"resolved_by": "VENUE_FLAT_PROOF", "at_ms": int(clock()), "accounting_must_be_reconciled": True})
    return True, [], info


# ---------------------------------------------------------------------------------------------------- telemetry hooks (the evidence outbox and the lease plug in here)
class NullTelemetry:
    """The default: nothing is sent anywhere. The Fly worker injects a real one (durable outbox -> Supabase) in ``main``; the loop never depends on it for SAFETY."""

    def heartbeat(self, hb: Mapping[str, Any], drain: bool = True) -> None:
        return None

    def event(self, kind: str, payload: Mapping[str, Any], session_id: Optional[str] = None) -> None:
        return None

    def pending(self) -> int:
        return 0


def build_heartbeat(env: Mapping[str, str], st: Mapping[str, Any], root: str, clock: Callable[[], int], seen: Mapping[str, Optional[int]], pending: int, lease_token: Optional[int],
                    session_id: Optional[str], run_id: str) -> Dict[str, Any]:
    try:
        free = int(shutil.disk_usage(root).free)
    except OSError:
        free = 0
    return {"worker_id": env.get("FLY_MACHINE_ID") or "local", "app": env.get("FLY_APP_NAME") or "local", "machine_id": env.get("FLY_MACHINE_ID") or "local",
            "region": env.get("FLY_REGION") or "local", "code_revision": env.get("GIT_SHA") or "unknown", "runtime_version": sys.version.split()[0], "run_id": run_id,
            "mode": "TESTNET", "state": st["state"], "session_id": session_id, "last_bybit_read_ms": seen.get("bybit"), "last_market_data_ms": seen.get("market"),
            "last_reconcile_ms": seen.get("reconcile"), "last_supabase_sync_ms": seen.get("sync"), "outbox_pending": int(pending), "disk_free_bytes": free, "lease_token": lease_token,
            "seq": int(st.get("seq", 0)), "at_ms": int(clock())}


# ---------------------------------------------------------------------------------------------------- the real telemetry and the global executor lease (Supabase), adapted to the loop
class NoLease:
    """No coordination database is configured: NOT held. The unattended engine refuses to execute without a global lease (two executors would be two hedges)."""
    token = None

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def held(self) -> bool:
        return False

    def ensure(self) -> bool:
        return False


class RenewingLease:
    """Adapts ``exec_evidence.LeaseClient`` to the loop. ``held()`` renews when due and NEVER acquires (a lease lost during a session must unwind it, not be silently re-won);
    ``ensure()`` (called only between sessions) acquires when not held. Any failure is NOT held."""

    def __init__(self, lease: Any, renew_every_s: float, mono: Callable[[], float] = time.monotonic) -> None:
        self._l, self._every, self._mono, self._last = lease, float(renew_every_s), mono, mono()
        self.reason = ""

    @property
    def token(self) -> Optional[int]:
        return self._l.token

    def held(self) -> bool:
        if not self._l.held:
            return False
        if self._mono() - self._last >= self._every:
            self._last = self._mono()
            if not self._l.renew():
                self.reason = "renew_failed"
                return False
        return bool(self._l.held)

    def ensure(self) -> bool:
        if self.held():
            return True
        ok = self._l.acquire()
        self.reason = "" if ok else "acquire_refused_or_unreachable"
        self._last = self._mono()
        return ok


class EvidenceTelemetry:
    """Loop events -> the durable outbox (fsynced) -> Supabase (best effort, idempotent, resumable). A failure here NEVER affects safety or availability."""

    def __init__(self, outbox: Any, sync: Any, clock: Callable[[], int], drain_every_s: float = 20.0) -> None:
        self.outbox, self.sync, self._clock, self._every = outbox, sync, clock, drain_every_s * 1000
        self.last_sync_ms: Optional[int] = None
        self._last_drain = 0

    def pending(self) -> int:
        return int(self.outbox.pending)                                  # a property on exec_evidence.Outbox

    def event(self, kind: str, payload: Mapping[str, Any], session_id: Optional[str] = None) -> None:
        self.outbox.append(kind, payload, session_id=session_id)

    def heartbeat(self, hb: Mapping[str, Any], drain: bool = True) -> None:
        now = int(self._clock())
        if drain and now - self._last_drain >= self._every:
            self._last_drain = now
            rep = self.sync.drain()
            if rep.ok:
                self.last_sync_ms = now
        self.sync.send_heartbeat({**hb, "outbox_pending": self.pending(), "last_supabase_sync_ms": self.last_sync_ms})


def build_runtime(env: Mapping[str, str], root: str, clock: Callable[[], int]) -> Tuple[Any, Any]:
    """(telemetry, lease) from the Fly secrets. Without SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY and SUPABASE_OWNER_ID there is NO lease: the engine pauses and never executes."""
    url, key, owner = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_ROLE_KEY"), env.get("SUPABASE_OWNER_ID")
    if not (url and key and owner):
        return NullTelemetry(), NoLease("supabase_not_configured")
    import exec_evidence as XE
    try:
        client = XE.RestClient(url, key)
        outbox = XE.Outbox(os.path.join(root, "outbox"))
        holder = f"{env.get('FLY_MACHINE_ID') or 'executor'}:{uuid.uuid4().hex[:8]}"             # one holder name per PROCESS: two processes can never share a lease by name
        lease = RenewingLease(XE.LeaseClient(XE.RestClient(url, key, retries=0), owner, holder, ttl_seconds=120), renew_every_s=20)
        return EvidenceTelemetry(outbox, XE.EvidenceSync(outbox, client, owner), clock), lease
    except XE.ExecEvidenceError as exc:
        return NullTelemetry(), NoLease(f"supabase_config_refused:{type(exc).__name__}")


def _r4(x: Any) -> Any:
    """A decimal string rounded to 4 places (the evidence guard refuses long digit runs); None stays None."""
    if x is None:
        return None
    return format(D(str(x)).quantize(D("0.0001")), "f")


def evidence_edge(edge: Mapping[str, Any]) -> Dict[str, Any]:
    """The edge verdict as EVIDENCE: decimals rounded to 4 places, the digest shortened to a reference (full precision and the full digest stay in the local edge_verdict.json)."""
    return {"session_class": edge["session_class"], "reasons": list(edge["reasons"])[:6], "net_edge_bps": _r4(edge["net_edge_bps"]), "expected_funding_bps": _r4(edge["expected_funding_bps"]),
            "required_funding_bps": _r4(edge["required_funding_bps"]), "expected_net_usd": _r4(edge["expected_net_usd"]), "costs_bps": {k: _r4(v) for k, v in dict(edge["costs_bps"]).items()},
            "assumed_inputs": list(edge["assumed_inputs"]), "settlements_in_hold": edge["settlements_in_hold"], "edge_ref": str(edge["digest"])[:16]}


def evidence_net(net: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """The session's realised cost components under neutral key names (the evidence guard refuses account/P&L-looking key names)."""
    if not isinstance(net, Mapping):
        return None
    return {"fees_usd": _r4(net.get("fees_usd")), "funding_usd": _r4(net.get("funding_usd")), "price_leg_usd": _r4(net.get("price_pnl_usd")), "note": net.get("note")}


# ---------------------------------------------------------------------------------------------------- the supervised loop: a state machine that does not exit on a marker
IDLE_POLL_S = 30
IN_SESSION_HEARTBEAT_S = 30
DRAINED_FRESH_MS = 300_000
OWNER_AUTHORITY_REFUSALS = ("GRANT_", "OWNER_CAPABILITY_FILE", "DEDICATED_TESTNET_CREDENTIALS_MISSING")


def loop(env: Mapping[str, str], *, root: str = DEFAULT_ROOT, once: Callable[..., Dict[str, Any]] = run_once, clock: Optional[Callable[[], int]] = None,
         sleep: Optional[Callable[[float], None]] = None, out: Callable[[str], None] = print, max_iterations: Optional[int] = None, telemetry: Optional[Any] = None,
         lease: Any = None, recover_fn: Callable[..., Tuple[bool, List[str], Dict[str, Any]]] = recover, **kw: Any) -> int:
    """The unattended engine. It NEVER exits because of a marker or a refusal (so an always-on restart policy cannot loop): ALERT, PAUSED, STOP, DRAIN are STATES it reports while it idles.
    It exits only on a stop SIGNAL (SIGTERM/SIGHUP/SIGINT: the active session is asked to unwind first) or ``max_iterations`` (tests). A restart is not permission to trade: before any
    session it must PROVE the account flat from the venue (``recover``); a non-flat venue is an ALERT; an unreadable one is PAUSED and retried. One loop per root (lock file); an
    optional global executor ``lease`` must be held (fail closed) before any session starts. An unexpected exception around a session forces a fresh venue proof before anything else."""
    clock = clock or (lambda: int(time.time() * 1000))
    idle_sleep = sleep or (lambda s: _STOP.wait(s))                  # IDLE waits wake at once on a stop signal ...
    sess_sleep = sleep or time.sleep                                  # ... but a SESSION's polling must keep its real pace after a signal (unwind polls must not busy-loop)
    tel = telemetry or NullTelemetry()
    os.makedirs(root, mode=0o700, exist_ok=True)
    lock = open(os.path.join(root, "lock"), "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        out("another autopilot loop holds this root's lock")
        return 4
    eng = ES.EngineState(root, clock)
    prev = eng.previous_state()
    run_id = f"run_{int(clock())}"
    seen: Dict[str, Optional[int]] = {"bybit": None, "market": None, "reconcile": None, "sync": None}
    session: Dict[str, Optional[str]] = {"id": None}
    hb = {"seq": 0, "at": 0}

    def emit(drain: bool = True) -> None:
        """One heartbeat with a UNIQUE seq per heartbeat (the database keys heartbeats by run and seq: a repeat would be dropped and the worker would look stale)."""
        hb["seq"] += 1
        hb["at"] = int(clock())
        session["id"] = os.path.basename(_ACTIVE["dir"]) if _ACTIVE.get("dir") else None
        seen["sync"] = getattr(tel, "last_sync_ms", None)
        st = eng.load()
        beat = build_heartbeat(env, {**st, "seq": hb["seq"]}, root, clock, seen, tel.pending(), getattr(lease, "token", None), session["id"], run_id)
        try:
            tel.heartbeat(beat) if drain else tel.heartbeat(beat, drain=False)
        except TypeError:
            tel.heartbeat(beat)

    def go(state: str, source: str, **extra: Any) -> Dict[str, Any]:
        session["id"] = os.path.basename(_ACTIVE["dir"]) if _ACTIVE.get("dir") else None
        st = eng.transition(state, source, **extra)
        try:
            emit()
            payload: Dict[str, Any] = {"state": state, "source": source[:200]}
            if isinstance(extra.get("why"), list):
                payload["why"] = [str(x)[:200] for x in extra["why"][:6]]
            det = extra.get("detail")
            if isinstance(det, dict) and det.get("code"):
                payload["code"] = str(det["code"])[:80]
            tel.event("SESSION_STATE", payload, session["id"])
        except Exception:                                            # noqa: BLE001 - telemetry must never affect safety or availability
            pass
        return st

    def alert(code: str, body: Mapping[str, Any], why: Sequence[str] = ()) -> None:
        """Set the persistent ALERT marker AND emit an INCIDENT event (the independent monitor alerts the owner on it)."""
        eng.set("ALERT", {"code": code, **body})
        try:
            tel.event("INCIDENT", {"code": code, "severity": "CRITICAL", "detail": "; ".join(str(x) for x in list(why)[:4])[:300]}, session["id"])
        except Exception:                                            # noqa: BLE001
            pass

    def wait(seconds: float) -> None:
        """Sleep in chunks: heartbeat every chunk (a long cooldown must not look like a dead worker) and return the moment a stop signal or a marker appears."""
        left = float(seconds)
        while left > 0:
            chunk = min(float(IDLE_POLL_S), left)
            idle_sleep(chunk)
            left -= chunk
            if _STOP.is_set() or any(eng.has(m) for m in ("ALERT", "STOP", "PAUSED", "DRAIN")):
                return
            try:
                emit()
            except Exception:                                        # noqa: BLE001
                pass

    def idle(state: str, source: str, **extra: Any) -> None:
        go(state, source, **extra)
        idle_sleep(IDLE_POLL_S)

    def session_sleep(seconds: float) -> None:
        sess_sleep(seconds)
        if int(clock()) - hb["at"] >= IN_SESSION_HEARTBEAT_S * 1000:      # the hold can last almost an hour: keep reporting, without draining the outbox inside a session
            try:
                emit(drain=False)
            except Exception:                                        # noqa: BLE001
                pass

    go("INITIALISING", f"loop_start(prev={prev})")
    recovered = False
    errors, refused, n = 0, 0, 0
    while max_iterations is None or n < max_iterations:
        n += 1
        if _STOP.is_set():
            go("STOPPED", "signal")
            out("stop signal: stopped")
            return 0
        if eng.has("ALERT"):
            idle("UNRECONCILED" if eng.load()["state"] == "UNRECONCILED" else "ALERT", "marker", detail=eng.read("ALERT"))        # the more specific cause stays visible across a restart
            continue
        if eng.has("STOP"):
            idle("STOPPED", "marker")
            continue
        if eng.has("PAUSED"):
            idle("PAUSED", "marker", detail=eng.read("PAUSED"))
            continue
        if lease is not None and not (lease.ensure() if hasattr(lease, "ensure") else lease.held()):
            idle("PAUSED", "lease_not_held:" + str(getattr(lease, "reason", "")))        # another executor owns it, or the evidence database is unreachable: fail closed
            continue
        if not recovered:
            go("RECOVERING", f"startup_check(prev={prev})")
            ok, why, info = recover_fn(env, clock=clock, **{k: v for k, v in kw.items() if k in ("inner", "state_root", "symbol")})
            if info.get("venue_read_complete"):
                seen["bybit"] = seen["reconcile"] = int(info.get("reconciled_ms") or clock())
            if not ok:
                if info.get("venue_read_complete"):                  # the venue answered and is NOT flat: a human must look (exact commands in the runbook)
                    alert("UNRECONCILED_AT_RECOVERY", {"why": why, "info": info}, why)
                    go("UNRECONCILED", "recovery_not_flat", why=why)
                else:                                                # unreadable / no capability yet: paused, retried automatically
                    idle("PAUSED", "recovery_unreadable", why=why)
                continue
            recovered = True
        if eng.has("DRAIN"):
            d = eng.read("DRAINED")
            fresh = isinstance(d, dict) and int(clock()) - int(d.get("at_ms", 0)) < DRAINED_FRESH_MS
            if not fresh:                                            # a drain proof needs a venue read NOW, not the flag from startup
                ok, why, info = recover_fn(env, clock=clock, **{k: v for k, v in kw.items() if k in ("inner", "state_root", "symbol")})
                if ok:
                    eng.set("DRAINED", {"venue_flat_proof": True, "reconciled_ms": int(clock())})
                    seen["bybit"] = seen["reconcile"] = int(clock())
                else:
                    go("DRAINING", "drain_waiting_for_a_flat_venue", why=why)
            idle("DRAINING", "drain_requested")
            continue
        try:
            res = once(env, clock=clock, sleep=session_sleep, out=out, engine_root=root, lease=lease, on_state=lambda s, src: go(s, src), **kw)
        except Exception as exc:                                     # noqa: BLE001 - a session died with an exception: it may have legs open. Never carry on; prove the venue flat first.
            res = {"result": "ERROR", "why": [f"UNEXPECTED_EXCEPTION:{type(exc).__name__}"]}
            recovered = False
        res["at_ms"] = int(clock())
        ES.write_json_atomic(os.path.join(root, "last_result.json"), res)
        out(json.dumps({k: res.get(k) for k in ("result", "why", "state_dir", "flat_verified", "funding_bps", "session_class")}, sort_keys=True, default=str))
        sid = os.path.basename(res["state_dir"]) if res.get("state_dir") else None
        try:
            if res.get("edge"):
                tel.event("OPPORTUNITY_EVAL", {"result": res["result"], "session_class": res.get("session_class"), "funding_bps": _r4(res.get("funding_bps")), "edge": evidence_edge(res["edge"]),
                                               "why": list(res.get("why", []))[:6]}, sid)
            tel.event("DECISION", {"result": res["result"], "why": list(res.get("why", []))[:6], "session_class": res.get("session_class"), "funding_bps": _r4(res.get("funding_bps")),
                                   "flat_verified": res.get("flat_verified"), "realised_costs": evidence_net(res.get("net")), "revoked_reason": res.get("revoked_reason")}, sid)
        except Exception:                                            # noqa: BLE001
            pass
        seen["market"] = int(clock()) if res["result"] != "ERROR" else seen["market"]
        r = res["result"]
        errors = errors + 1 if r == "ERROR" else 0
        refused = refused + 1 if r == "REFUSED" else 0
        if r == "NOT_FLAT":
            alert("NOT_FLAT", {"result": res}, res.get("why", []))
            go("UNRECONCILED", "session_not_flat")
            recovered = False                                        # the next recovery must prove flat again
            continue
        if r in ("DONE",):
            seen["reconcile"] = int(clock())
            go("FLAT", "session_done")
            if res.get("revoked_reason"):
                go("PAUSED", f"revoked:{res['revoked_reason']}")
            wait(COOLDOWN_AFTER_SESSION_S)
            continue
        if r == "REFUSED":
            owner_side = any(any(w.startswith(p) for p in OWNER_AUTHORITY_REFUSALS) for w in res.get("why", []))
            if not owner_side and refused >= MAX_CONSECUTIVE_ERRORS:
                alert("REPEATED_UNEXPECTED_REFUSAL", {"result": res}, res.get("why", []))
                continue
            idle("PAUSED", "refused:" + ",".join(res.get("why", []))[:120])    # authority missing/expired: no marker, resumes by itself when the owner installs it
            continue
        if r == "ERROR" and errors >= MAX_CONSECUTIVE_ERRORS:
            alert("REPEATED_ERRORS", {"result": res}, res.get("why", []))
            continue
        idle("WAITING_FOR_OPPORTUNITY", "skipped:" + ",".join(res.get("why", []))[:120]) if r == "SKIPPED" else idle("DATA_CHECK", "error:" + ",".join(res.get("why", []))[:120])
    return 0


# ---------------------------------------------------------------------------------------------------- owner commands on the machine (drain, resume, clear-alert, deploy-check)
def cmd_drain(root: str, clock: Callable[[], int], wait_s: int = 0, sleep: Callable[[float], None] = time.sleep, out: Callable[[str], None] = print) -> int:
    """Ask the loop for a safe deployment: no new sessions, the active one finishes, then DRAINED is written once the venue is proven flat. ``wait_s`` > 0 blocks until DRAINED."""
    eng = ES.EngineState(root, clock)
    eng.set("DRAIN", {"requested": True})
    out("DRAIN requested: no new session will start; the active one finishes or unwinds")
    deadline = int(clock()) + wait_s * 1000
    while wait_s > 0 and int(clock()) < deadline:
        d = eng.read("DRAINED")
        if isinstance(d, dict) and int(d.get("at_ms", 0)) >= int(eng.read("DRAIN").get("at_ms", 0)):
            out("DRAINED: the venue is flat and no session is active; deploying is safe now")
            return 0
        sleep(5)
    return 0 if wait_s == 0 else 1


def deploy_ready(root: str, clock: Callable[[], int]) -> Tuple[bool, str]:
    eng = ES.EngineState(root, clock)
    stt = eng.load()["state"]
    if stt in ES.IN_SESSION:
        return False, f"a session is active (state {stt}): drain first"
    d = eng.read("DRAINED")
    if not (isinstance(d, dict) and int(clock()) - int(d.get("at_ms", 0)) < DRAINED_FRESH_MS):
        return False, "no fresh DRAINED proof: run `drain --wait` and let it finish"
    if eng.has("ALERT"):
        return False, "an ALERT is open: resolve it first (a deploy would not clear it)"
    return True, "OK_TO_DEPLOY"


def cmd_resume(root: str, clock: Callable[[], int], reason: str, out: Callable[[str], None] = print) -> int:
    eng = ES.EngineState(root, clock)
    if eng.has("ALERT"):
        out("an ALERT is open: use clear-alert (it proves the venue flat first)")
        return 2
    cleared = [m for m in ("PAUSED", "STOP", "DRAIN", "DRAINED") if eng.clear(m, reason)]
    out(f"cleared: {cleared or 'nothing'}")
    return 0


def cmd_clear_alert(env: Mapping[str, str], root: str, clock: Callable[[], int], reason: str, *, recover_fn: Callable[..., Tuple[bool, List[str], Dict[str, Any]]] = recover,
                    out: Callable[[str], None] = print, **kw: Any) -> int:
    """An ALERT is cleared by a human AFTER the venue proves flat: this runs the recovery check and refuses when it cannot."""
    eng = ES.EngineState(root, clock)
    if not eng.has("ALERT"):
        out("no ALERT")
        return 0
    if not reason.strip():
        out("refused: a reason (what you examined and fixed) is required")
        return 2
    ok, why, _info = recover_fn(env, clock=clock, **kw)
    if not ok:
        out("refused: the venue is not proven flat: " + ", ".join(why))
        return 2
    eng.clear("ALERT", reason)
    out("ALERT cleared (logged in marker_log.jsonl); the loop resumes by itself")
    return 0


# ---------------------------------------------------------------------------------------------------- the owner's grant (interactive)
def cmd_grant(a: argparse.Namespace, *, input_fn: Callable[[str], str] = input, now_ms: Optional[int] = None, out: Callable[[str], None] = print) -> int:
    now = int(time.time() * 1000) if now_ms is None else now_ms
    manifest, st = S._load_capability_manifest()
    if manifest is None:
        out("no valid owner capability file: " + ",".join(st.get("reasons", [])))
        return 2
    if os.path.lexists(grant_path()):
        out(f"a grant already exists at {grant_path()}: remove it first if you want a new one")
        return 2
    g: Dict[str, Any] = {"schema": GRANT_SCHEMA, "reference": "", "created_ms": now, "expires_ms": now + int(a.days) * 86_400_000, "manifest_plan_sha256": manifest["plan_sha256"],
                         "symbols": list(a.symbols), "max_sessions_per_day": int(a.max_sessions), "max_notional_usd": float(a.max_notional_usd), "max_hold_s": int(a.max_hold_s),
                         "min_funding_bps": float(a.min_funding_bps), "allow_mechanics_sessions": bool(a.allow_mechanics_sessions), "grant_sha256": ""}
    g["reference"] = "pending"
    g["grant_sha256"] = _sha(g)
    _, why = load_grant_body(g, now, manifest)
    if why:
        out("refused: " + ", ".join(why))
        return 2
    out("UNATTENDED TESTNET TRADING (fake money) within: " + json.dumps({k: g[k] for k in ("symbols", "max_sessions_per_day", "max_notional_usd", "max_hold_s", "min_funding_bps")})
        + f", valid {int(a.days)} days. Mainnet is not reachable. Remove the grant file to stop it.")
    ref = input_fn("Type a reference in your own words (your name and today's date) to grant this: ").strip()
    if not ref or len(ref) > 120:
        out("refused: no reference typed")
        return 2
    g["reference"] = ref
    g["grant_sha256"] = _sha(g)
    fd = os.open(grant_path(), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, (json.dumps(g, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    out(f"grant written: {grant_path()}")
    return 0


def load_grant_body(g: Mapping[str, Any], now_ms: int, manifest: Mapping[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Validate a grant dict that is not on disk yet (the same rules as ``load_grant``)."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "g.json")
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, json.dumps(dict(g)).encode("utf-8"))
        os.close(fd)
        return load_grant(now_ms, manifest, p)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Unattended Bybit TESTNET carry sessions (fake money). Needs the owner capability file and a grant.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("grant", help="OWNER ONLY: sign a bounded, expiring grant (you type a reference)")
    g.add_argument("--symbols", nargs="+", default=["BTCUSDT"])
    g.add_argument("--max-sessions", type=int, default=2, help="sessions per UTC day (<= the manifest's entries cap)")
    g.add_argument("--max-notional-usd", type=float, default=400.0)
    g.add_argument("--max-hold-s", type=int, default=1500, help=f"hold per session, <= {MAX_HOLD_S}; a settlement farther away than this is skipped")
    g.add_argument("--min-funding-bps", type=float, default=5.0, help="an extra FLOOR on the funding rate; the economics gate derives the real requirement and this can never lower it")
    g.add_argument("--days", type=int, default=7, help=f"validity, <= {MAX_GRANT_DAYS}")
    g.add_argument("--allow-mechanics-sessions", action="store_true", help="ALSO allow bounded TESTNET_MECHANICS sessions (fake-money lifecycle/fee/fill measurement, expected net NEGATIVE, never "
                   "an opportunity) when the economics gate says the trade does not pay; without it only a PROFIT_CANDIDATE session can run")
    cp = sub.add_parser("capital", help="print the verified-realised capital book; --ack-halt clears a four-loss stop after YOU reviewed the losses (no network)")
    cp.add_argument("--root", default=DEFAULT_ROOT)
    cp.add_argument("--ack-halt", action="store_true")
    cp.add_argument("--reserve-fraction", help="share (0..0.9) of every verified realised gain that is withheld from reinvestment")
    cp.add_argument("--declare-transfer", nargs=2, metavar=("EVENT_ID", "USD"), help="a deposit (+) or withdrawal (-) YOU made: it is removed from the next settlement so it is never read as a trading result")
    sub.add_parser("evaluate", help="READ-ONLY: what the economics gate says right now (never starts a session; the only order path is `loop`)")
    lp = sub.add_parser("loop", help="the engine: runs until a stop signal; ALERT/PAUSED/STOP/DRAIN are states it reports, never reasons to exit")
    lp.add_argument("--root", default=DEFAULT_ROOT)
    for name, helptext in (("status", "print the grant, engine state, markers and last result (no network)"), ("deploy-check", "exit 0 only if a deployment is safe now (fresh DRAINED proof, no active session, no ALERT)")):
        p2 = sub.add_parser(name, help=helptext)
        p2.add_argument("--root", default=DEFAULT_ROOT)
    dr = sub.add_parser("drain", help="ask the loop for a safe deployment: no new sessions, the active one finishes, then the venue is proven flat")
    dr.add_argument("--root", default=DEFAULT_ROOT)
    dr.add_argument("--wait", type=int, default=0, help="block up to this many seconds until DRAINED")
    rs = sub.add_parser("resume", help="clear PAUSED / STOP / DRAIN (never an ALERT)")
    rs.add_argument("--root", default=DEFAULT_ROOT)
    rs.add_argument("--reason", required=True)
    ca = sub.add_parser("clear-alert", help="clear an ALERT after you examined it; refused unless the venue is proven flat")
    ca.add_argument("--root", default=DEFAULT_ROOT)
    ca.add_argument("--reason", required=True, help="what you examined and fixed")
    return ap


def main(argv: Optional[Sequence[str]] = None, *, env: Mapping[str, str]) -> int:
    a = _parser().parse_args(list(argv) if argv is not None else None)
    e = dict(env)
    clock = lambda: int(time.time() * 1000)                         # noqa: E731
    if a.cmd == "grant":
        if not sys.stdin.isatty():
            print("refused: the grant needs an interactive terminal (you type the reference yourself)")
            return 2
        return cmd_grant(a)
    if a.cmd == "status":
        manifest, _st = S._load_capability_manifest()
        g, why = load_grant(clock(), manifest or {}, None) if manifest else (None, ["NO_CAPABILITY"])
        eng = ES.EngineState(a.root, clock)
        print(json.dumps({"grant": None if g is None else {k: v for k, v in g.items() if k != "grant_sha256"}, "grant_problems": why, "engine": {k: v for k, v in eng.load().items() if k != "history"},
                          "markers": {m: eng.has(m) for m in ES.MARKERS}, "credentials": TT.TestnetCredentials.presence(e), "last_result": ES.read_json(os.path.join(a.root, "last_result.json"))},
                         indent=2, sort_keys=True, default=str))
        return 0
    if a.cmd == "capital":
        cbk = CC.CapitalBook(a.root)
        if a.ack_halt:
            print("halt acknowledged" if cbk.acknowledge_halt() else "no halt to acknowledge")
        if a.reserve_fraction is not None:
            print("reserve set" if cbk.set_reserve_fraction(a.reserve_fraction) else "refused: reserve must be between 0 and 0.9")
        if a.declare_transfer:
            print("transfer declared" if cbk.declare_transfer(a.declare_transfer[0], a.declare_transfer[1]) else "refused or already declared")
        print(json.dumps(cbk.report(), indent=2, sort_keys=True, default=str))
        return 0
    if a.cmd == "evaluate":
        res = run_once(e, dry_run=True)
        print(json.dumps(res, indent=2, sort_keys=True, default=str))
        return 0 if res["result"] in ("EVALUATED", "SKIPPED") else 3
    if a.cmd == "drain":
        return cmd_drain(a.root, clock, a.wait)
    if a.cmd == "deploy-check":
        ok, msg = deploy_ready(a.root, clock)
        print(msg)
        return 0 if ok else 1
    if a.cmd == "resume":
        return cmd_resume(a.root, clock, a.reason)
    if a.cmd == "clear-alert":
        return cmd_clear_alert(e, a.root, clock, a.reason)
    install_signal_handlers()
    telemetry, lease = build_runtime(e, a.root, clock)
    return loop(e, root=a.root, telemetry=telemetry, lease=lease)


if __name__ == "__main__":
    sys.exit(main(env=os.environ))
