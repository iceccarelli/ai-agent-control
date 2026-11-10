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
import signal
import stat
import sys
import threading
import time
from decimal import Decimal as D
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bybit_connection as bc  # noqa: E402
import ms_edge_gate as EG  # noqa: E402
import ms_testnet_adapter as AD  # noqa: E402
import ms_testnet_executor as TE  # noqa: E402
import ms_testnet_session as S  # noqa: E402
import ms_testnet_transport as TT  # noqa: E402

GRANT_SCHEMA = "ms_autopilot_grant/v1"
GRANT_KEYS = frozenset({"schema", "reference", "created_ms", "expires_ms", "manifest_plan_sha256", "symbols", "max_sessions_per_day", "max_notional_usd", "max_hold_s",
                        "min_funding_bps", "allow_mechanics_sessions", "grant_sha256"})
MAX_GRANT_DAYS = 30
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
            g = json.loads(fh.read().decode("utf-8"))
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


def run_once(env: Mapping[str, str], *, inner: Any = None, clock: Optional[Callable[[], int]] = None, sleep: Optional[Callable[[float], None]] = None,
             out: Callable[[str], None] = print, state_root: Optional[str] = None) -> Dict[str, Any]:
    clock = clock or (lambda: int(time.time() * 1000))
    sleep = sleep or time.sleep
    now = int(clock())
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
    try:                                                              # the ECONOMICS gate: expected NET edge on the one economic model, from the venue's own rows, books and fee tier
        inp = EG.collect(_executor(env, inner, clock, sym), sym, D(str(g["max_notional_usd"])), hold_s, D(str(rate)), nxt, int(clock()))
        verdict = EG.evaluate_edge(inp)
    except (TE.ExecutorError, bc.BybitAPIError, TT.TestnetTransportRefused, StopIteration, KeyError, IndexError, ValueError, TypeError, ArithmeticError) as exc:
        return _res("ERROR", [f"EDGE_INPUTS_UNAVAILABLE:{type(exc).__name__}"])
    session_class, why_not = classify_session(verdict, D(str(rate)), g)
    if session_class is None:
        return _res("SKIPPED", why_not, funding_bps=rate * 1e4, edge=verdict.as_dict())
    root = state_root or os.path.join(os.path.expanduser("~"), ".ai-agent-control")
    d = os.path.join(root, "ms_auto_" + time.strftime("%Y%m%dT%H%M%S", time.gmtime(now / 1000.0)))
    _ACTIVE["dir"] = d
    try:
        return _run_session(env, inner, clock, sleep, d, sym, g, hold_s, rate, session_class, verdict)
    finally:
        _ACTIVE["dir"] = None


def _run_session(env: Mapping[str, str], inner: Any, clock: Callable[[], int], sleep: Callable[[float], None], d: str, sym: str, g: Mapping[str, Any], hold_s: int,
                 rate: float, session_class: str, verdict: EG.EdgeVerdict) -> Dict[str, Any]:
    lines: List[str] = []

    def step(*argv: str) -> int:
        return S.main(["--state-dir", d, *argv], env=env, inner_transport=inner, clock_ms=clock, sleep=sleep, input_fn=lambda _p: "", out=lines.append)

    if _STOP.is_set():
        return _res("SKIPPED", ["STOP_REQUESTED"], state_dir=d)
    if step("preflight") != 0:
        return _res("ERROR", ["PREFLIGHT_FAILED"], state_dir=d, detail=lines[-6:])
    S._write_json(os.path.join(d, "edge_verdict.json"), {"session_class": session_class, "label": "TESTNET_MECHANICS_NOT_AN_OPPORTUNITY" if session_class == "TESTNET_MECHANICS" else "PROFIT_SEEKING_CANDIDATE",
                                                          "edge": verdict.as_dict()})
    if step("plan", "--symbol", sym, "--notional-usd", str(g["max_notional_usd"]), "--hold-seconds", str(hold_s)) != 0:
        return _res("SKIPPED", ["PLAN_REFUSED"], state_dir=d, detail=lines[-3:])
    try:
        with open(os.path.join(d, "plan.json"), encoding="utf-8") as fh:
            digest = json.load(fh)["plan_digest"]
    except (OSError, ValueError, KeyError):
        return _res("ERROR", ["PLAN_UNREADABLE"], state_dir=d)
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
    lines.clear()
    if _STOP.is_set():                                               # a stop arrived before any order: nothing was placed, no entry was counted
        return _res("SKIPPED", ["STOP_REQUESTED"], state_dir=d)
    rc = step("execute", "--confirm-plan-digest", digest, "--unattended")
    try:
        with open(os.path.join(d, "testnet_session.json"), encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        rec = {}
    summary = {"state_dir": d, "plan_digest": digest, "hold_s": hold_s, "funding_bps": rate * 1e4, "flat_verified": rec.get("flat_verified"), "orders": len(rec.get("orders", [])),
               "net": rec.get("net"), "exit_code": rc, "tail": lines[-4:], "session_class": session_class, "edge": verdict.as_dict()}
    if rc == S.EXIT_REFUSED:
        return _res("REFUSED", ["EXECUTE_REFUSED"], **summary)
    return _res("DONE" if rec.get("flat_verified") is True else "NOT_FLAT", [], **summary)


# ---------------------------------------------------------------------------------------------------- the supervised loop
def loop(env: Mapping[str, str], *, root: str = DEFAULT_ROOT, once: Callable[..., Dict[str, Any]] = run_once, clock: Optional[Callable[[], int]] = None,
         sleep: Optional[Callable[[float], None]] = None, out: Callable[[str], None] = print, max_iterations: Optional[int] = None, **kw: Any) -> int:
    """Repeat ``once`` until STOP exists, a NOT_FLAT / REFUSED result (a human must look: ALERT file, exit 3), or three errors in a row. One loop per root (lock file)."""
    clock = clock or (lambda: int(time.time() * 1000))
    sleep = sleep or (lambda s: _STOP.wait(s))                       # a stop signal wakes the wait at once
    os.makedirs(root, mode=0o700, exist_ok=True)
    if os.path.exists(os.path.join(root, "ALERT")):                  # a human must look first: after a restart (Fly restarts a failed machine) the loop must NOT carry on
        out(f"ALERT present in {root}: not starting. Read it, fix the cause, remove the ALERT file, then start again")
        return 0
    lock = open(os.path.join(root, "lock"), "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        out("another autopilot loop holds this root's lock")
        return 4
    errors, n = 0, 0
    while max_iterations is None or n < max_iterations:
        n += 1
        if _STOP.is_set() or os.path.exists(os.path.join(root, "STOP")):
            out("stop requested: stopping")
            return 0
        res = once(env, clock=clock, sleep=sleep, out=out, **kw)
        res["at_ms"] = int(clock())
        S._write_json(os.path.join(root, "last_result.json"), res)
        out(json.dumps({k: res.get(k) for k in ("result", "why", "state_dir", "flat_verified", "funding_bps")}, sort_keys=True, default=str))
        if res["result"] in ("NOT_FLAT", "REFUSED"):
            S._write_json(os.path.join(root, "ALERT"), res)
            return 3
        errors = errors + 1 if res["result"] == "ERROR" else 0
        if errors >= MAX_CONSECUTIVE_ERRORS:
            S._write_json(os.path.join(root, "ALERT"), res)
            return 3
        sleep(COOLDOWN_AFTER_SESSION_S if res["result"] == "DONE" else POLL_S)
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
    g.add_argument("--min-funding-bps", type=float, default=5.0, help="only trade when the funding rate pays at least this many bps per settlement")
    g.add_argument("--days", type=int, default=7, help=f"validity, <= {MAX_GRANT_DAYS}")
    g.add_argument("--allow-mechanics-sessions", action="store_true", help="ALSO allow bounded TESTNET_MECHANICS sessions (fake-money lifecycle/fee/fill measurement, expected net NEGATIVE, never "
                   "an opportunity) when the economics gate says the trade does not pay; without it only a PROFIT_CANDIDATE session can run")
    sub.add_parser("once", help="one cycle")
    lp = sub.add_parser("loop", help="repeat until STOP, a NOT_FLAT/REFUSED result, or repeated errors")
    lp.add_argument("--root", default=DEFAULT_ROOT)
    sub.add_parser("status", help="print the grant's bounds and the last result (no network)")
    return ap


def main(argv: Optional[Sequence[str]] = None, *, env: Mapping[str, str]) -> int:
    a = _parser().parse_args(list(argv) if argv is not None else None)
    e = dict(env)
    if a.cmd == "grant":
        if not sys.stdin.isatty():
            print("refused: the grant needs an interactive terminal (you type the reference yourself)")
            return 2
        return cmd_grant(a)
    if a.cmd == "status":
        manifest, _st = S._load_capability_manifest()
        g, why = load_grant(int(time.time() * 1000), manifest or {}, None) if manifest else (None, ["NO_CAPABILITY"])
        print(json.dumps({"grant": None if g is None else {k: v for k, v in g.items() if k != "grant_sha256"}, "why": why}, indent=2, sort_keys=True))
        return 0
    if a.cmd == "once":
        res = run_once(e)
        print(json.dumps(res, indent=2, sort_keys=True, default=str))
        return 0 if res["result"] in ("DONE", "SKIPPED") else 3
    install_signal_handlers()
    return loop(e, root=a.root)


if __name__ == "__main__":
    sys.exit(main(env=os.environ))
