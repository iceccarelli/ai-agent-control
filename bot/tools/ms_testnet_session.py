"""OWNER-INVOKED Bybit TESTNET session tool for the multi-symbol hedged carry (testnet only; fake money; never mainnet).

    python3 tools/ms_testnet_session.py --state-dir DIR preflight                          # READ-ONLY: env, endpoint, key permissions, clock, modes, balances, filters, book, flat/empty
    python3 tools/ms_testnet_session.py --state-dir DIR plan        --symbol BTCUSDT --notional-usd 40     # offline: builds plan.json from the manifest + the preflight capture
    python3 tools/ms_testnet_session.py --state-dir DIR execute     --confirm-plan-digest <digest>        # then TYPE the digest at the prompt
    python3 tools/ms_testnet_session.py --state-dir DIR reconcile   [--unwind --confirm-plan-digest D]   # re-read the venue, re-verify an interrupted session
    python3 tools/ms_testnet_session.py --state-dir DIR status                                            # prints testnet_session.json (no network)

Nothing here runs on import. ``main`` takes everything it needs as arguments (environment mapping, inner transport, clock, sleep, prompt) so the offline proof drives it against the
in-process fake testnet; the ``__main__`` wrapper is the ONLY place that reads ``os.environ`` and builds the real (no-redirect, no-proxy) inner transport. ``execute`` refuses
(exit 2, no network) unless ALL of: the owner capability file (never created by code) is valid, both dedicated credentials are present, the plan on disk still hashes to its
``plan_digest``, ``--confirm-plan-digest`` equals it AND the digest typed at the prompt equals it, a PASS preflight younger than ``--max-preflight-age-min`` exists for the same
manifest, the kill-switch file is absent, no session was already executed from this state dir, and (under CI / pytest) the transport was injected explicitly.

Bounds (at or below the owner's manifest, which is itself <= the repository ceilings): aggregate notional <= $500 (Testnet) across ALL symbols, ONE active group, explicit symbol allow-list,
lifecycle <= 9 h, leverage <= the manifest's, persistent kill switch file (``MS_KILL`` in the state dir), fresh account and market data, reduce-only exits, FLAT = a COMPLETE venue
position read (never fill arithmetic). Fees, funding and price P&L are reported as the exchange reported them; they are TESTNET figures, not owner profit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
from dataclasses import asdict, replace
from decimal import Decimal as D
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bybit_connection as bc  # noqa: E402
import economic_model as EM  # noqa: E402
import exec_quality as EQ  # noqa: E402
import ms_executor_lock as XL  # noqa: E402
import funding_settlement as FS  # noqa: E402
import ms_accounting as AC  # noqa: E402
import ms_authority as MA  # noqa: E402
import ms_execution as EX  # noqa: E402
import ms_pipeline as MP  # noqa: E402          # only its pure helpers: the git-work-tree test and the git revision reader (the repo's one)
import ms_reconcile as MR  # noqa: E402
import ms_risk as MRISK  # noqa: E402          # assess only: the same pure risk function the authority calls
import ms_testnet_adapter as AD  # noqa: E402
import ms_testnet_executor as TE  # noqa: E402
import ms_testnet_transport as TT  # noqa: E402
import ms_types as T  # noqa: E402
import venues as _venues  # noqa: E402

SESSION_SCHEMA = "testnet_session/v1"
PLAN_SCHEMA = "testnet_plan/v1"
PREFLIGHT_SCHEMA = "testnet_preflight/v1"
VENUE_LABEL = "Testnet"                                  # display label in the session record; the registry key (policy venue) is the lower-case "testnet"
NOTE = "TESTNET, not owner profit"
SPOT_BUY_CUSHION_BPS = 30                                # the spot entry is a limit IOC this far ABOVE the plan-time ask: it still fills at the best ask, the cushion only bounds the worst price
HARD_CEILING_USD = AD.AGGREGATE_CEILING_USD           # one number: the adapter's Testnet ceiling (it was a second hard-coded 100 that would have silently clamped a 500 plan)
MAX_LIFECYCLE_H = 9.0
DEFAULT_MAX_PREFLIGHT_AGE_MIN = 15
MAX_PLAN_AGE_MIN = 120
EXIT_OK, EXIT_FAIL, EXIT_REFUSED = 0, 1, 2
FILES = {"record": "testnet_session.json", "plan": "plan.json", "preflight": "preflight.json", "journal": "engine_journal.jsonl", "kill": "MS_KILL", "owner_unwind": "OWNER_UNWIND",
         "meas": "exec_measurements.json", "quality": "execution_quality.json", "decision": "decision.json", "funding": "funding_settlement.json",
         "recon": "reconciliation.json"}


# ---------------------------------------------------------------------------------------------------- small helpers
def canon(o: Any) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest_of(o: Mapping[str, Any], skip: Sequence[str]) -> str:
    return hashlib.sha256(canon({k: v for k, v in o.items() if k not in skip}).encode("utf-8")).hexdigest()


def _write_json(path: str, doc: Any) -> None:
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, indent=2, sort_keys=True, allow_nan=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _read_json(path: str) -> Optional[Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.loads(fh.read())
    except (OSError, ValueError):
        return None


class StateDirRefused(ValueError):
    """The ``--state-dir`` argument cannot be used (raised before anything is created)."""


def resolve_state_dir(raw: str) -> str:
    """``~`` is expanded here because the runbook's ``S="... --state-dir ~/..."`` reaches us unexpanded (a tilde inside double quotes is literal), and a state dir inside a git
    work tree is refused: the session record, plan, journal and kill switch must never be committable, and a literal ``./~`` directory is exactly that."""
    d = os.path.abspath(os.path.expanduser(str(raw)))
    if MP._inside_git(d):
        raise StateDirRefused(f"STATE_DIR_INSIDE_GIT_WORK_TREE: {d} (use a directory outside the repository, e.g. $HOME/.ai-agent-control/ms_testnet)")
    return d


class Ctx:
    def __init__(self, args: argparse.Namespace, env: Mapping[str, str], inner: Any, clock: Callable[[], int], sleep: Callable[[float], None], input_fn: Callable[[str], str],
                 out: Callable[[str], None]) -> None:
        self.args, self.env, self.inner, self.clock, self.sleep, self.input_fn, self.out = args, env, inner, clock, sleep, input_fn, out
        self.pre_order_check: Optional[Callable[[], Optional[str]]] = None      # an ENTRY order is refused (nothing is sent) when this returns a reason or raises; exits are never gated
        self.dir = resolve_state_dir(args.state_dir)
        os.makedirs(self.dir, mode=0o700, exist_ok=True)

    def path(self, k: str) -> str:
        return os.path.join(self.dir, FILES[k])


# ---------------------------------------------------------------------------------------------------- the session record (contract with the control centre)
def credential_state(env: Mapping[str, str]) -> Dict[str, Any]:
    p = TT.TestnetCredentials.presence(env)
    return {"dedicated_key_present": p["dedicated_key_present"], "dedicated_secret_present": p["dedicated_secret_present"], "generic_credentials_ignored": True}


def new_record(ctx: Ctx, *, candidate_id: str = "UNKNOWN", spec_digest: str = "UNKNOWN", data_digest: str = "UNKNOWN") -> Dict[str, Any]:
    now = int(ctx.clock())
    ident = T.RunIdentity(T.derive_run_id(mode="TESTNET", candidate_id=candidate_id, spec_digest=spec_digest, data_digest=data_digest, generated_ms=now), "TESTNET", candidate_id,
                          spec_digest, data_digest, MP.git_revision(), now)
    return {"schema": SESSION_SCHEMA, "identity": ident.as_dict(), "venue": VENUE_LABEL, "endpoint_host": _venues.get("testnet").host, "credential_state": credential_state(ctx.env),
            "preflight": {"status": "NOT_RUN", "checked_ms": None, "checks": []}, "plan": None, "confirmation": {"state": "NOT_REQUESTED", "plan_digest_confirmed": None},
            "orders": [], "executions": [], "final_position": None, "flat_verified": None, "basis": "NOT_RUN",
            "net": {"fees_usd": None, "funding_usd": None, "price_pnl_usd": None, "note": NOTE}}


def load_record(ctx: Ctx) -> Dict[str, Any]:
    rec = _read_json(ctx.path("record"))
    if isinstance(rec, dict) and not record_refusals(rec):
        rec["credential_state"] = credential_state(ctx.env)
        return rec
    return new_record(ctx)


def save_record(ctx: Ctx, rec: Dict[str, Any]) -> None:
    bad = record_refusals(rec)
    if bad:
        raise ValueError("session record invalid: " + ",".join(bad))
    _write_json(ctx.path("record"), rec)


def record_refusals(rec: Any) -> List[str]:
    """Structural check of ``testnet_session/v1`` (what the control centre may rely on)."""
    out: List[str] = []
    keys = {"schema", "identity", "venue", "endpoint_host", "credential_state", "preflight", "plan", "confirmation", "orders", "executions", "final_position", "flat_verified", "basis", "net"}
    if not isinstance(rec, dict) or set(rec) != keys:
        return ["KEYS"]
    if rec["schema"] != SESSION_SCHEMA or rec["venue"] != VENUE_LABEL:
        out.append("SCHEMA_OR_VENUE")
    if not isinstance(rec["identity"], dict) or rec["identity"].get("mode") != "TESTNET":
        out.append("IDENTITY_MODE")
    if rec["endpoint_host"] != _venues.get("testnet").host:
        out.append("ENDPOINT_HOST")
    cs = rec["credential_state"]
    if not (isinstance(cs, dict) and set(cs) == {"dedicated_key_present", "dedicated_secret_present", "generic_credentials_ignored"} and cs["generic_credentials_ignored"] is True
            and type(cs["dedicated_key_present"]) is bool and type(cs["dedicated_secret_present"]) is bool):
        out.append("CREDENTIAL_STATE")
    pf = rec["preflight"]
    if not (isinstance(pf, dict) and set(pf) == {"status", "checked_ms", "checks"} and pf["status"] in ("NOT_RUN", "PASS", "FAIL") and isinstance(pf["checks"], list)):
        out.append("PREFLIGHT")
    if rec["confirmation"].get("state") not in ("NOT_REQUESTED", "CONFIRMED") if isinstance(rec["confirmation"], dict) else True:
        out.append("CONFIRMATION")
    if rec["basis"] not in ("NOT_RUN", "EXCHANGE_REPORTED"):
        out.append("BASIS")
    if rec["basis"] == "NOT_RUN" and (rec["orders"] or rec["executions"]):
        out.append("EVIDENCE_WITHOUT_BASIS")
    n = rec["net"]
    if not (isinstance(n, dict) and set(n) == {"fees_usd", "funding_usd", "price_pnl_usd", "note"} and n["note"] == NOTE):
        out.append("NET")
    return out


def _num_or_none(x: Any) -> Optional[float]:
    """The record is a JSON contract of NUMBERS (the control centre refuses a numeric string); the plan and the venue carry exact decimal strings, which stay in plan.json."""
    return None if x is None else float(x)


def _orders_for_record(view_orders: Mapping[str, Mapping[str, Any]], gid: str) -> List[Dict[str, Any]]:
    rows = []
    for link, o in sorted(view_orders.items()):
        if link.startswith(gid + "-"):
            rows.append({"link_id": link, "order_id": o.get("order_id"), "symbol": o.get("symbol"), "side": o.get("side"), "qty": _num_or_none(o.get("qty")), "status": o.get("status"),
                         "filled_qty": o.get("filled_qty"), "avg_price": o.get("avg_price")})
    return rows


def _execs_for_record(execs: Sequence[Mapping[str, Any]], gid: str) -> List[Dict[str, Any]]:
    seen, rows = set(), []
    for x in execs:
        if x["link_id"].startswith(gid + "-") and x["fill_id"] not in seen:
            seen.add(x["fill_id"])
            rows.append({"exec_id": x["fill_id"], "link_id": x["link_id"], "symbol": x["symbol"], "side": x["side"], "qty": x["qty"], "price": x["price"], "fee_usd": x["fee_usd"],
                         "ts_ms": x["ts_ms"]})
    return sorted(rows, key=lambda r: (r["ts_ms"], r["exec_id"]))


def net_from(execs: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Exchange-reported figures. price P&L = sum(sell value) - sum(buy value) per category-symbol, only meaningful once flat (the caller passes it only then)."""
    fees = sum(float(x["fee_usd"]) for x in execs)
    pnl = sum((1 if x["side"] == "Sell" else -1) * float(x["qty"]) * float(x["price"]) for x in execs)
    return {"fees_usd": round(fees, 8), "price_pnl_usd": round(pnl, 8)}


# ---------------------------------------------------------------------------------------------------- building blocks
def _kill(ctx: Ctx) -> Tuple[bool, str]:
    return AD.kill_switch_engaged(ctx.path("kill"))


def _load_capability_manifest() -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    st = AD.capability_status()
    if st.get("enabled") is not True:
        return None, st
    doc = _read_json(AD.DEFAULT_CAPABILITY_PATH)
    return (doc["manifest"] if isinstance(doc, dict) else None), st


def _client(ctx: Ctx) -> Tuple[Any, TT.TestnetTransport]:
    creds = TT.TestnetCredentials.from_mapping(ctx.env)
    inner = ctx.inner if ctx.inner is not None else TT.NoRedirectInner()
    return TT.build_client(creds, inner)


def _check(checks: List[Dict[str, Any]], name: str, ok: bool, detail: str) -> bool:
    checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:300]})
    return bool(ok)


# ---------------------------------------------------------------------------------------------------- preflight (READ-ONLY)
def cmd_preflight(ctx: Ctx) -> int:
    rec = load_record(ctx)
    checks: List[Dict[str, Any]] = []
    now = int(ctx.clock())
    cap_manifest, st = _load_capability_manifest()
    p = TT.TestnetCredentials.presence(ctx.env)
    _check(checks, "env_presence", p["dedicated_key_present"] and p["dedicated_secret_present"],
           f"{TT.KEY_ENV}={'present' if p['dedicated_key_present'] else 'MISSING'} {TT.SECRET_ENV}={'present' if p['dedicated_secret_present'] else 'MISSING'} (generic BYBIT_API_* ignored)")
    _check(checks, "owner_capability_file", cap_manifest is not None, "valid" if cap_manifest is not None else ",".join(st.get("reasons", [])))
    names_ok = cap_manifest is not None and cap_manifest["credential_env"] == {"api_key": TT.KEY_ENV, "api_secret": TT.SECRET_ENV}
    _check(checks, "manifest_credential_names", names_ok, "manifest names the dedicated variables" if names_ok else f"the manifest must name {TT.KEY_ENV}/{TT.SECRET_ENV} (generate --session-credentials)")
    eng, why = _kill(ctx)
    _check(checks, "kill_switch", not eng, f"MS_KILL {why}")
    snapshot: Dict[str, Any] = {}
    if all(c["ok"] for c in checks):
        try:
            snapshot = _preflight_reads(ctx, cap_manifest, checks)   # type: ignore[arg-type]
        except TT.TestnetTransportRefused as exc:
            _check(checks, "transport", False, exc.code)
        except (bc.BybitAPIError, TE.ExecutorError) as exc:
            _check(checks, "reads", False, f"{type(exc).__name__}: {str(exc)[:160]}")
    elif not p["dedicated_key_present"] or not p["dedicated_secret_present"]:
        ctx.out(TT.SETUP_INSTRUCTIONS)
    status = "PASS" if all(c["ok"] for c in checks) else "FAIL"
    doc = {"schema": PREFLIGHT_SCHEMA, "status": status, "checked_ms": now, "checks": checks, "manifest_plan_sha256": cap_manifest["plan_sha256"] if cap_manifest else None,
           "symbols": cap_manifest["symbol_allowlist"] if cap_manifest else [], **snapshot}
    doc["preflight_digest"] = digest_of(doc, ("preflight_digest",))
    _write_json(ctx.path("preflight"), doc)
    rec["preflight"] = {"status": status, "checked_ms": now, "checks": checks}
    save_record(ctx, rec)
    for c in checks:
        ctx.out(f"[{'ok' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}")
    if snapshot:                                                              # informational: it does not fail a typed session, but the unattended autopilot's capital book needs it
        ref = snapshot.get("account_ref")
        ctx.out("capital account identity: " + (f"{ref} (basis {snapshot.get('account_identity_basis')})" if ref else
                "UNAVAILABLE - the venue's key read states no userID, so the autopilot's capital book will refuse every session (owner decision: CAPITAL_ACCOUNT_IDENTITY_FALLBACK=KEY_ID, see RUNBOOKS)"))
    ctx.out(f"preflight {status}")
    return EXIT_OK if status == "PASS" else EXIT_FAIL


def _preflight_reads(ctx: Ctx, manifest: Mapping[str, Any], checks: List[Dict[str, Any]]) -> Dict[str, Any]:
    client, transport = _client(ctx)
    ex = TE.TestnetExecutor(client, symbols=list(manifest["symbol_allowlist"]), clock_ms=ctx.clock, read_retries=1)
    _check(checks, "endpoint_identity", client.venue == "testnet" and _venues.host_of(client.base_url) == _venues.get("testnet").host and TT.is_testnet_executor_transport(transport),
           f"host={_venues.host_of(client.base_url)} venue={client.venue} role=testnet_executor")
    max_skew = int(getattr(ctx.args, "max_skew_ms", 2000))
    try:
        skew = client.sync_time()
        _check(checks, "server_time_skew", abs(skew) <= max_skew, f"skew_ms={skew} (limit {max_skew})")
    except bc.BybitAPIError as exc:
        _check(checks, "server_time_skew", False, f"unreadable: {type(exc).__name__}")
    ok, detail, summary = TT.check_api_key(client)
    _check(checks, "key_permissions", ok, detail if not ok else f"least privilege: {summary.get('granted')}")
    snap: Dict[str, Any] = {"market": {}, "filters": {}, "spot_baseline": {}, "account_ref": summary.get("account_ref"), "account_identity_basis": "USER_ID" if summary.get("account_ref") else "UNAVAILABLE"}
    if snap["account_ref"] is None and str(ctx.env.get("CAPITAL_ACCOUNT_IDENTITY_FALLBACK", "")).strip().upper() == "KEY_ID" and summary.get("key_id_account_ref"):
        snap["account_ref"], snap["account_identity_basis"] = summary["key_id_account_ref"], "KEY_ID"          # the OWNER chose this because the venue states no userID; rotating the key is then a new account
    try:
        info = ex._get("/v5/account/info", {}, signed=True)
        _check(checks, "account_mode", str(info.get("unifiedMarginStatus", "")) in ("3", "4", "5", "6") or info.get("marginMode") in ("REGULAR_MARGIN", "ISOLATED_MARGIN", "PORTFOLIO_MARGIN"),
               f"marginMode={info.get('marginMode')} unifiedMarginStatus={info.get('unifiedMarginStatus')}")
    except TE.ExecutorError as exc:
        _check(checks, "account_mode", False, str(exc))
    flat_ok, open_orders, lev_notes, one_way = True, [], [], True
    try:
        rows = ex._paged("/v5/position/list", {"category": "linear", "settleCoin": TE.QUOTE}, 200)
        for r in rows:
            if r.get("positionIdx") != 0:
                one_way = False
            if D(str(r["size"])) != 0:
                flat_ok = False
                open_orders.append(f"position:{r['symbol']}")
            if r.get("symbol") in manifest["symbol_allowlist"]:
                lev_notes.append(f"{r['symbol']}={r.get('leverage')}")
        _check(checks, "position_mode_one_way", one_way, "one-way (positionIdx 0)" if one_way else "HEDGE MODE detected: switch the account to one-way on the testnet site")
    except (TE.ExecutorError, KeyError, ValueError) as exc:
        _check(checks, "position_mode_one_way", False, str(exc))
        flat_ok = False
    try:
        w = ex._get("/v5/account/wallet-balance", {"accountType": "UNIFIED"}, signed=True)["list"][0]
        equity = float(w["totalEquity"])
        usdt = next((float(c["walletBalance"]) for c in w["coin"] if c["coin"] == TE.QUOTE), 0.0)
        _check(checks, "wallet_balance", equity > 0 and usdt >= 2 * AD.AGGREGATE_CEILING_USD / 2, f"equity_usd={equity} usdt={usdt}")
        snap["equity_usd"], snap["usdt"] = equity, usdt
        coins = {c["coin"]: float(c["walletBalance"]) for c in w["coin"]}
    except (TE.ExecutorError, KeyError, IndexError, ValueError) as exc:
        _check(checks, "wallet_balance", False, str(exc))
        coins = {}
    inst_ok, book_ok = True, True
    for s in manifest["symbol_allowlist"]:
        try:
            f = ex.filters("linear", s, refresh=True)
            inst_ok &= f.status == "Trading"
            snap["filters"][s] = {"linear": {"tick": TE.fmt(f.tick), "step": TE.fmt(f.qty_step), "min_qty": TE.fmt(f.min_qty), "max_qty": TE.fmt(f.max_qty), "min_notional": TE.fmt(f.min_notional),
                                             "status": f.status, "base": f.base_coin, "funding_interval_min": f.funding_interval_min}}
            try:
                sf = ex.filters("spot", s, refresh=True)
                snap["filters"][s]["spot"] = {"tick": TE.fmt(sf.tick), "step": TE.fmt(sf.qty_step), "min_notional": TE.fmt(sf.min_notional), "status": sf.status, "base": sf.base_coin}
                snap["spot_baseline"][f"spot:{s}"] = coins.get(sf.base_coin, 0.0)
            except TE.ExecutorError:
                pass                                                   # perp-only symbol: the plan tool refuses it for a hedged pair
            lt = ex._get("/v5/market/tickers", {"category": "linear", "symbol": s}, signed=False)["list"][0]
            ob = ex._get("/v5/market/orderbook", {"category": "linear", "symbol": s, "limit": 5}, signed=False)
            bid, ask = float(ob["b"][0][0]), float(ob["a"][0][0])
            m: Dict[str, Any] = {"bid": ob["b"][0][0], "ask": ob["a"][0][0], "mark": lt.get("markPrice") or lt["lastPrice"], "book_ts_ms": ob.get("ts"), "read_ms": int(ctx.clock())}
            if "spot" in snap["filters"][s]:
                st_ = ex._get("/v5/market/tickers", {"category": "spot", "symbol": s}, signed=False)["list"][0]
                so = ex._get("/v5/market/orderbook", {"category": "spot", "symbol": s, "limit": 5}, signed=False)
                m.update({"spot_bid": so["b"][0][0], "spot_ask": so["a"][0][0], "spot_last": st_["lastPrice"]})
            book_ok &= 0 < bid <= ask and math.isfinite(bid + ask)
            snap["market"][s] = m
            for cat in ("linear", "spot"):
                if cat == "spot" and "spot" not in snap["filters"][s]:
                    continue
                for o in ex._paged("/v5/order/realtime", {"category": cat, "symbol": s}, 50):
                    open_orders.append(f"order:{cat}:{s}:{o.get('orderId')}")
        except (TE.ExecutorError, KeyError, IndexError, TypeError, ValueError) as exc:
            inst_ok = book_ok = False
            _check(checks, f"market_{s}", False, f"unreadable: {str(exc)[:120]}")
    _check(checks, "instrument_filters", inst_ok and bool(snap["filters"]), f"allow-listed symbols read: {sorted(snap['filters'])}")
    _check(checks, "orderbook_ticker", book_ok and bool(snap["market"]), f"books read: {sorted(snap['market'])}")
    _check(checks, "flat_and_no_open_orders", flat_ok and not open_orders, "flat, no open orders" if flat_ok and not open_orders else "NOT CLEAN: " + ",".join(open_orders)[:200])
    maxlev = manifest["bounds"]["max_leverage"]
    _check(checks, "leverage_known", True, f"current leverage {','.join(lev_notes) or 'n/a'}; execute sets the plan leverage (<= {maxlev}) on the allow-listed perp")
    return snap


# ---------------------------------------------------------------------------------------------------- plan (offline)
def _lcm_step(a: D, b: D) -> D:
    sa, sb = max(-a.as_tuple().exponent, 0), max(-b.as_tuple().exponent, 0)
    s = 10 ** max(sa, sb)
    ia, ib = int(a * s), int(b * s)
    return D(ia * ib // math.gcd(ia, ib)) / D(s)


def build_plan(manifest: Mapping[str, Any], pre: Mapping[str, Any], *, symbol: str, notional_usd: float, leverage: int, now_ms: int, kill_path: str,
               hold_s: int = 0, entry_wait_s: int = 60, exit_wait_s: int = 180, max_lifecycle_hours: float = 1.0) -> Dict[str, Any]:
    """The complete proposed plan (pure). Raises ``ValueError`` with a reason when a bound or an instrument rule would be broken."""
    b = manifest["bounds"]
    if symbol not in manifest["symbol_allowlist"]:
        raise ValueError("SYMBOL_NOT_IN_ALLOW_LIST")
    ceiling = min(float(b["aggregate_max_notional_usd"]), HARD_CEILING_USD)
    if not (0 < notional_usd <= ceiling):
        raise ValueError(f"NOTIONAL_ABOVE_AGGREGATE_CEILING:{notional_usd}>{ceiling}")
    if not (1 <= leverage <= float(b["max_leverage"])):
        raise ValueError("LEVERAGE_ABOVE_MANIFEST")
    if not (0 < max_lifecycle_hours <= min(MAX_LIFECYCLE_H, float(b["max_lifecycle_hours"]))):
        raise ValueError("LIFECYCLE_ABOVE_BOUND")
    if hold_s < 0 or hold_s + entry_wait_s + exit_wait_s > max_lifecycle_hours * 3600:
        raise ValueError("TIMING_EXCEEDS_LIFECYCLE")
    fl, mk = pre.get("filters", {}).get(symbol), pre.get("market", {}).get(symbol)
    if not fl or not mk or "spot" not in fl or "spot_ask" not in mk:
        raise ValueError("SYMBOL_NOT_HEDGEABLE_ON_SPOT_OR_NOT_READ")
    lin, sp = fl["linear"], fl["spot"]
    mark = D(str(mk["mark"]))
    step = _lcm_step(D(lin["step"]), D(sp["step"]))
    qty = T.round_down_to_step(D(str(notional_usd)) / mark, step)
    if D(qty) <= 0 or D(qty) < D(lin["min_qty"]):
        raise ValueError("QTY_BELOW_MIN_QTY")
    notional = float(D(qty) * mark)
    if notional > ceiling + 1e-9:
        raise ValueError("QTY_NOTIONAL_ABOVE_CEILING")
    usdt = pre.get("usdt")
    if not (isinstance(usdt, (int, float)) and not isinstance(usdt, bool) and math.isfinite(usdt)):
        # Round 20 (specialist C): this cash check is what justifies pricing the spot leg's borrow at a structural zero (ms_edge_gate: SPOT_LEG_PAID_FROM_USDT_CASH_NO_BORROW). It used to run only when the
        # read happened to be numeric, so a missing or malformed USDT read skipped the very check the zero stands on. A plan with no proven cash is refused, never assumed funded.
        raise ValueError("USDT_BALANCE_UNREAD: the preflight carried no numeric USDT balance, so the spot leg cannot be shown to be paid from cash (no borrow)")
    # the spot buy (notional) plus the perp initial margin (notional / leverage) plus a 2% buffer must fit in the USDT read
    need = float(EM.hedged_pair_capital_usd(D(repr(notional)), max(1, leverage)).value)      # ONE definition of the capital a pair commits (economic_model); the allocator's margin cap reads the same
    if need > float(usdt) + 1e-9:
        raise ValueError(f"INSUFFICIENT_USDT_FOR_PLAN:need {need:.2f} have {float(usdt):.2f} (spot buy + perp margin at leverage {leverage} + 2%)")
    for cat, f, ref in (("linear", lin, mark), ("spot", sp, D(str(mk["spot_ask"])))):
        if D(f["min_notional"]) > 0 and D(qty) * ref < D(f["min_notional"]):
            raise ValueError(f"{cat.upper()}_NOTIONAL_BELOW_MIN")
    spot_px = T.round_to_tick(D(str(mk["spot_ask"])) * (1 + D(SPOT_BUY_CUSHION_BPS) / D(10_000)), sp["tick"], side="Buy", passive=False)     # rounds UP: never below the cushioned limit
    if D(qty) * D(spot_px) > D(str(ceiling)) + D("1e-9"):                      # the spot leg is assessed at its LIMIT price: the cushioned leg must still fit the ceiling
        raise ValueError(f"SPOT_LIMIT_NOTIONAL_ABOVE_CEILING:{float(D(qty) * D(spot_px)):.2f}>{ceiling}")
    legs = (T.LegPlan("p", symbol, "linear", "Sell", qty, "Market", None, False, False, "IOC"),
            T.LegPlan("s", symbol, "spot", "Buy", qty, "Limit", spot_px, False, False, "IOC"))
    s0 = manifest["strategy_allowlist"][0]
    policy = AD.policy_from_manifest(manifest)
    gid = "tn" + hashlib.sha256(f"{manifest['plan_sha256']}|{symbol}|{now_ms}".encode()).hexdigest()[:10]
    group = T.OrderGroupPlan(gid, s0["candidate_id"], s0["spec_digest"], policy["policy_version"], int(now_ms), legs, "TAKER", entry_wait_s * 1000, notional)
    leg_rows = [{"leg_id": l.leg_id, "symbol": l.symbol, "category": l.category, "side": l.side, "order_type": l.order_type, "qty": l.qty, "price": l.price, "time_in_force": l.time_in_force,
                 "reduce_only": l.reduce_only, "notional_usd": float(D(l.qty) * (D(l.price) if l.price else mark))} for l in legs]
    plan = {"schema": PLAN_SCHEMA, "venue": VENUE_LABEL, "endpoint_host": _venues.get("testnet").host, "created_ms": int(now_ms), "manifest_plan_sha256": manifest["plan_sha256"],
            "symbol": symbol, "notional_usd": notional, "aggregate_ceiling_usd": ceiling, "leverage": leverage, "style": "TAKER", "legs": leg_rows, "group": asdict(group),
            "policy": policy, "policy_digest": MA.policy_digest(policy),
            "bounds": {"one_active_group": True, "symbol_allow_list": list(manifest["symbol_allowlist"]), "max_lifecycle_hours": max_lifecycle_hours, "max_leverage": b["max_leverage"],
                       "max_market_age_ms": b["max_market_age_ms"], "max_account_age_ms": b["max_account_age_ms"], "kill_switch_file": kill_path},
            "timing": {"entry_wait_s": entry_wait_s, "hold_s": hold_s, "exit_wait_s": exit_wait_s, "poll_s": 1},
            "exit_plan": ["on hedge complete, hold hold_s seconds", "reduce-only Market IOC on the perp first, then spot Market sell (spot sell is an exit only after the perp is closed)",
                          "FLAT only when a COMPLETE venue position read shows every leg key flat", "kill switch / UNHEDGED / timeout => cancel entries, reduce-only unwind"],
            "preflight_digest": pre.get("preflight_digest"), "preflight_checked_ms": pre.get("checked_ms"),
            "spot_baseline": dict(pre.get("spot_baseline", {}))}
    plan["plan_digest"] = digest_of(plan, ("plan_digest",))
    return plan


def plan_refusals(plan: Any) -> List[str]:
    if not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA:
        return ["PLAN_SCHEMA"]
    if plan.get("plan_digest") != digest_of(plan, ("plan_digest",)):
        return ["PLAN_DIGEST_MISMATCH"]
    return []


def cmd_plan(ctx: Ctx) -> int:
    a = ctx.args
    manifest, st = _load_capability_manifest()
    pre = _read_json(ctx.path("preflight"))
    why: List[str] = []
    if manifest is None:
        why.append("OWNER_CAPABILITY_FILE:" + ",".join(st.get("reasons", [])))
    if not (isinstance(pre, dict) and pre.get("schema") == PREFLIGHT_SCHEMA and pre.get("status") == "PASS" and pre.get("preflight_digest") == digest_of(pre, ("preflight_digest",))):
        why.append("NO_PASSING_PREFLIGHT")
    elif manifest is not None and pre.get("manifest_plan_sha256") != manifest["plan_sha256"]:
        why.append("PREFLIGHT_FOR_ANOTHER_MANIFEST")
    elif ctx.clock() - int(pre["checked_ms"]) > a.max_preflight_age_min * 60_000:
        why.append("PREFLIGHT_STALE")
    if why:
        ctx.out("plan refused: " + "; ".join(why))
        return EXIT_REFUSED
    assert manifest is not None and isinstance(pre, dict)
    try:
        plan = build_plan(manifest, pre, symbol=a.symbol, notional_usd=a.notional_usd, leverage=a.leverage, now_ms=int(ctx.clock()), kill_path=ctx.path("kill"), hold_s=a.hold_seconds,
                          entry_wait_s=a.entry_wait_s, exit_wait_s=a.exit_wait_s, max_lifecycle_hours=a.max_lifecycle_hours)
    except ValueError as exc:
        ctx.out(f"plan refused: {exc}")
        return EXIT_REFUSED
    _write_json(ctx.path("plan"), plan)
    rec = load_record(ctx)
    s0 = manifest["strategy_allowlist"][0]
    rec["identity"] = T.RunIdentity(T.derive_run_id(mode="TESTNET", candidate_id=s0["candidate_id"], spec_digest=s0["spec_digest"], data_digest=pre["preflight_digest"],
                                                     generated_ms=int(ctx.clock())), "TESTNET", s0["candidate_id"], s0["spec_digest"], pre["preflight_digest"], MP.git_revision(),
                                    int(ctx.clock())).as_dict()
    rec["plan"] = {"digest": plan["plan_digest"], "manifest_digest": plan["manifest_plan_sha256"],
                   "legs": [{"symbol": l["symbol"], "side": l["side"], "order_type": l["order_type"], "qty": _num_or_none(l["qty"]), "price": _num_or_none(l["price"]), "notional_usd": l["notional_usd"]} for l in plan["legs"]],
                   "notional_usd": plan["notional_usd"]}
    save_record(ctx, rec)
    ctx.out(json.dumps(plan, indent=2, sort_keys=True))
    ctx.out(f"PLAN DIGEST: {plan['plan_digest']}")
    ctx.out("To execute (testnet, fake money): execute --confirm-plan-digest <that digest>, then type it at the prompt.")
    return EXIT_OK


# ---------------------------------------------------------------------------------------------------- execute / reconcile machinery
def _minus(a: Any, b: Any) -> float:
    """a - b in decimal: float subtraction of a wallet balance and the pre-session baseline leaves noise (0.106 - 0.1 = 0.0060000000000000053) that makes an exact-lot sell look 1e-17 too big."""
    return float(D(str(a)) - D(str(b)))


class _Cached:
    """A provider that re-reads when its value is older than ``refresh_ms`` and, when a read FAILS, serves the last good value with its ORIGINAL timestamp (so the authority's
    freshness check, not this class, decides it is stale)."""

    def __init__(self, read: Callable[[], Any], clock: Callable[[], int], refresh_ms: int = 500) -> None:
        self.read, self.clock, self.refresh_ms = read, clock, refresh_ms
        self.value: Any = None
        self.at: Optional[int] = None

    def invalidate(self) -> None:
        """Forget the value AND its timestamp: the next ``get`` must read again, and if that read fails it RAISES (a value read before a fill must never be served after it settled)."""
        self.value, self.at = None, None

    def get(self) -> Any:
        now = int(self.clock())
        if self.at is None or now - self.at >= self.refresh_ms:
            try:
                self.value, self.at = self.read(), now
            except (TE.ExecutorError, bc.BybitAPIError):
                if self.value is None:
                    raise
        return self.value


class Run:
    """The wired stack: client + transport + executor + authority + engine + bookkeeping for ONE group."""

    def __init__(self, ctx: Ctx, plan: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
        self.ctx, self.plan = ctx, plan
        creds = TT.TestnetCredentials.from_mapping(ctx.env)
        inner = ctx.inner if ctx.inner is not None else TT.NoRedirectInner()
        self.client, self.transport = TT.build_client(creds, inner)
        syms = [plan["symbol"]]
        self.ex = TE.TestnetExecutor(self.client, symbols=syms, spot_symbols=syms, clock_ms=ctx.clock, kill_reader=lambda: _kill(ctx))
        self.baseline = dict(plan.get("spot_baseline", {}))
        self.engine: Optional[EX.ExecutionEngine] = None
        self.group_plan = T.OrderGroupPlan(**{**plan["group"], "legs": tuple(T.LegPlan(**l) for l in plan["group"]["legs"])})
        self.gid = self.group_plan.group_id
        policy = dict(plan["policy"])
        if MA.policy_digest(policy) != plan["policy_digest"] or policy != AD.policy_from_manifest(manifest):
            raise MA.AuthorityConfigError("PLAN_POLICY_NOT_THE_OWNER_MANIFEST_POLICY")
        b = manifest["bounds"]
        self.max_loss_usd = float(b["max_loss_usd"])
        self.lifecycle_ms = int(min(float(plan["bounds"]["max_lifecycle_hours"]), float(b["max_lifecycle_hours"])) * 3_600_000)
        self.start_ms: Optional[int] = None
        self.start_equity: Optional[float] = None
        self.stopped: Optional[str] = None
        self._load_start()
        self._state = _Cached(self._read_state, ctx.clock)
        self._snaps = _Cached(lambda: {s: self.ex.snapshot(s) for s in syms}, ctx.clock)
        self.authority = MA.MultiSymbolAuthority(policy=policy, transport=self.transport, state_dir=ctx.dir, strategy=manifest["strategy_allowlist"][0]["strategy"],
                                                 state_provider=self._state.get, snapshot_provider=self._snapshots, kill_switch_reader=lambda: _kill(ctx), clock=lambda: ctx.clock() / 1000.0,
                                                 hedge_reader=self._hedge, testnet=MA.TestnetBinding(TT.KEY_ENV))
        MA.install(self.client, self.authority)
        self._settled: set = set()
        self._released: set = set()
        self._resolved = False
        self.killed = False
        self.ledger = AC.Ledger(track_acks=True)                       # the canonical ledger (worker F): fills by venue execId, acks, exit-side valuation
        self.ledger.set_exit_fee_bps(5.5, 10.0)
        self.last_view: Optional[Mapping[str, Any]] = None             # the venue read the engine ACTED on in its last step
        self.halter: Optional[Any] = None
        self.recon_report: Optional[Any] = None
        self.venue_recon: Optional[Dict[str, Any]] = None
        self.hold_spot_exit = True                                     # sequence the spot exit AFTER the perp exit fill is confirmed on the venue read
        self.hold_spot_exit_s = 20.0
        self.spot_exits_held = 0                                       # HELD answers so far: they are not venue rejections and do not count toward the exit halt, so each one is printed

    def _load_start(self) -> None:
        d = _read_json(os.path.join(self.ctx.dir, "run_limits.json"))
        if isinstance(d, dict) and d.get("gid") == self.gid_hint():
            self.start_ms, self.start_equity = d.get("start_ms"), d.get("start_equity_usd")
            b = d.get("baseline")                                  # the baseline the session TRADED with (read from the venue at execute), not the plan's older preflight copy
            if isinstance(b, dict) and b and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in b.values()):
                self.baseline = {str(k): float(v) for k, v in b.items()}

    def gid_hint(self) -> str:
        return str(self.plan["group"]["group_id"])

    def _save_start(self) -> None:
        _write_json(os.path.join(self.ctx.dir, "run_limits.json"), {"gid": self.gid_hint(), "start_ms": self.start_ms, "start_equity_usd": self.start_equity, "baseline": self.baseline})

    def _read_state(self) -> Dict[str, Any]:
        # open_groups = the OTHER active groups: the authority adds the group being assessed itself (ms_risk counts og + 1), so counting it here would refuse its own legs at max_active_groups=1
        others = sum(1 for gid, g in self.engine.groups.items() if gid != self.gid and g.state not in (T.GroupState.CLOSED, T.GroupState.FAILED)) if self.engine else 0
        st = self.ex.account_state(self.baseline, others)
        if self.start_equity is None:                                  # the loss limit is measured from the FIRST fresh account read of the session (persisted across a restart)
            self.start_equity = float(st["equity_usd"])
            self.start_ms = self.start_ms or int(self.ctx.clock())
            self._save_start()
        st["day_start_equity_usd"] = self.start_equity                 # -> ms_risk hard daily-loss limit (policy max_daily_loss_usd = the manifest's max_loss_usd)
        return st

    def _snapshots(self) -> Dict[str, T.MarketSnapshot]:
        snaps = self._snaps.get()
        age = int(self.ctx.clock()) - (self._snaps.at or 0)
        return {k: replace(v, book_age_ms=(v.book_age_ms or 0) + age if v.book_age_ms is not None else None) for k, v in snaps.items()}

    def _hedge(self, symbol: str) -> Tuple[float, float]:
        pos = self.ex.venue_state()["positions"] or {}
        spot = pos.get(f"spot:{symbol}")
        if spot is None:
            raise TE.ExecutorError("hedge read incomplete")
        return max(0.0, -pos.get(f"linear:{symbol}", 0.0)), max(0.0, _minus(spot, self.baseline.get(f"spot:{symbol}", 0.0)))

    def venue_state(self) -> Dict[str, Any]:
        v = self.ex.venue_state()
        self.last_view = v
        return v

    # the engine's send hook: release dead exit reservations before an exit is authorised (as ms_pipeline does), hold a spot exit until the perp is closed, then the executor
    def settle_filled_entries(self, wait_s: float = 5.0, *, skip_leg: str = "") -> None:
        """An entry leg the VENUE confirms fully filled is reflected in the account's position read: settle it, so the authority stops counting it twice (as a venue position AND as a
        still-reserved leg) when it assesses the NEXT leg. The engine sends both entry legs in one loop, before ``bookkeep`` runs, and a real exchange's order list can trail its position
        read by a moment: both real Testnet attempts were refused (per-symbol/aggregate/gross/unhedged) with the first leg counted twice. So a leg that was SENT is waited for, at most
        ``wait_s``, until the venue's own reads show it fully filled (the order, or the sum of its executions). A leg that is never confirmed stays RESERVED (conservative: the
        next leg is refused and the group unwound, never under-counted). The state cache is dropped after a settle so the next assessment re-reads the account AFTER the fill."""
        g = self.engine.groups.get(self.gid) if self.engine else None
        if g is None:
            return
        deadline = int(self.ctx.clock()) + int(wait_s * 1000)
        while True:
            pending = [l for l in g.entry_legs() if l.plan.leg_id != skip_leg and l.plan.leg_id not in self._settled and l.status in (EX.SENT, EX.LIVE, EX.UNKNOWN, EX.FILLED)]
            if not pending:
                return
            try:
                v = self.ex.venue_state()
            except (TE.ExecutorError, bc.BybitAPIError):
                v = None
            if v and v.get("complete"):
                for l in pending:
                    o = v["orders"].get(l.link)
                    done_qty = sum(D(str(x["qty"])) for x in v["executions"] if x.get("link_id") == l.link)
                    if (o and o.get("status") == "Filled" and D(str(o.get("filled_qty", 0))) >= D(str(l.plan_qty)) - D("1e-12")) or done_qty >= D(str(l.plan_qty)) - D("1e-12"):
                        self._settled.add(l.plan.leg_id)
                        self.authority.settle_leg(self.gid, l.plan.leg_id)
                        self._state.invalidate()
                if all(l.plan.leg_id in self._settled for l in pending):
                    return
            if int(self.ctx.clock()) >= deadline:
                return
            self.ctx.sleep(0.25)

    def send(self, req: EX.OrderRequest) -> Any:
        if not req.leg.leg_id.startswith("x"):
            chk = getattr(self.ctx, "pre_order_check", None)
            if chk is not None:                                          # authority (grant, STOP/ALERT, lease, stop signal) is re-checked IMMEDIATELY before each entry order
                try:
                    why = chk()
                except Exception as exc:                                  # noqa: BLE001 - a check that cannot run is a refusal, never a pass
                    why = f"CHECK_ERROR:{type(exc).__name__}"
                if why:
                    return EX.Ack(req.link_id, "REJECTED", None, f"PRE_ORDER_CHECK:{why}")
            self.settle_filled_entries(skip_leg=req.leg.leg_id)              # the leg being sent is SENT already but not on the venue yet: never wait for it
        if req.leg.leg_id.startswith("x"):
            self.release_dead_exits()
            if self.authority.is_resolved(self.gid):                    # exposure exists (the engine is exiting): a group resolved earlier may be EXITED again, never re-entered
                self.authority.reopen_group(self.gid)
            if req.leg.category == "spot" and req.leg.side == "Sell" and self.hold_spot_exit:
                ack = self._await_perp_closed(req)
                if ack is not None:
                    return ack
        ack = self.ex.send_order(req)
        if getattr(ack, "status", "") == "REJECTED" and "RISK:" in str(getattr(ack, "reason", "")):
            self.diagnose_refusal(req, ack)
        return ack

    def diagnose_refusal(self, req: EX.OrderRequest, ack: Any) -> None:
        """Print what the authority was shown when it refused an ENTRY on risk grounds (no secrets: notionals, leg statuses, settle marks), so the next failure is explained from numbers."""
        try:
            st = self._state.value or {}
            g = self.engine.groups.get(self.gid) if self.engine else None
            v = self.last_view or {}
            self.ctx.out("REFUSAL-DIAGNOSTIC " + json.dumps({
                "leg": req.leg.leg_id, "reason": str(getattr(ack, "reason", ""))[:300],
                "state_positions": {k: (p or {}).get("notional_usd") for k, p in (st.get("positions") or {}).items()}, "state_equity_usd": st.get("equity_usd"),
                "engine_legs": {l.plan.leg_id: l.status for l in (g.entry_legs() if g else [])}, "settled": sorted(self._settled),
                "venue_orders": {k: [o.get("status"), o.get("filled_qty")] for k, o in (v.get("orders") or {}).items()},
                "venue_exec_qty": {x.get("link_id"): x.get("qty") for x in (v.get("executions") or [])}}, sort_keys=True, default=str))
        except Exception as exc:                                       # noqa: BLE001 - a diagnostic must never break the run
            self.ctx.out(f"REFUSAL-DIAGNOSTIC unavailable: {type(exc).__name__}")

    def _engine_spot(self, symbol: str) -> float:
        """What the engine's own fills say the group holds in spot (0.0 when it holds none or the figure is unreadable: the caller then holds the sale)."""
        try:
            return max(0.0, float((self.engine.positions() if self.engine else {}).get(f"spot:{symbol}", 0.0)))
        except (TypeError, ValueError):
            return 0.0

    def _await_perp_closed(self, req: EX.OrderRequest) -> Optional[EX.Ack]:
        """A spot sell is an exit only once the perp it hedges is closed (``ms_authority.spot_sell_keeps_hedge``), and on a real exchange the perp exit's FILL arrives after its
        ack: the authority would refuse the spot sell and five such rejections HALT the group. So the spot exit waits until a COMPLETE venue position read shows the perp flat.
        None = go ahead; an Ack = give up (REJECTED, one rejection) after ``hold_spot_exit_s``."""
        sym = req.leg.symbol
        deadline = int(self.ctx.clock()) + int(self.hold_spot_exit_s * 1000)
        while True:
            v = self.ex.venue_state()
            pos = v["positions"]
            if v["complete"] and pos is not None:
                perp = pos.get(f"linear:{sym}", 0.0)
                if abs(perp) < 1e-12:
                    return None                                             # the perp is closed
                if perp < 0:                                                # a SHORT perp: a sale that leaves the group's spot still covering that short is the excess of a partial hedge, not its exit.
                    # The group's spot is the SMALLER of two independent witnesses: the account's balance MINUS the owner's pre-session coin (the wallet: a deposit or faucet AFTER the baseline was
                    # read inflates it, which let the sale leave while the whole short was open) and the engine's own book (what the group's fills made: 0 when it holds none, fail closed).
                    spot_held = min(max(0.0, _minus(pos.get(f"spot:{sym}", 0.0), self.baseline.get(f"spot:{sym}", 0.0))), self._engine_spot(sym))
                    if spot_held - float(req.leg.qty) >= -perp - 1e-9:
                        return None                                         # (a perp LONG nobody planned is not a hedge: it still holds the sale until the perp is flat, as before)
            if int(self.ctx.clock()) >= deadline:
                self.spot_exits_held += 1
                self.ctx.out(f"SPOT EXIT HELD #{self.spot_exits_held} {req.link_id}: the perp is still short on the venue read and the sale would leave it uncovered; it is not a venue rejection and does "
                             f"not count toward the exit halt (the exit window bounds it)")
                return EX.Ack(req.link_id, "REJECTED", None, EX.HELD_REASON)                 # the engine's own constant: a HELD exit is not a venue rejection and never counts toward the halt
            self.ctx.sleep(0.5)

    # ---- canonical ledger + reconciliation, fed from the venue read the engine acted on
    def feed_ledger(self) -> None:
        v, g = self.last_view, (self.engine.groups.get(self.gid) if self.engine else None)
        if v is None or g is None:
            return
        for l in g.legs.values():
            vo = v["orders"].get(l.link)                                # the VENUE's own record of the order also acknowledges it (a crash between acceptance and the journalled ack)
            oid = l.order_id or (vo.get("order_id") if vo else None)
            if oid:
                self.ledger.acknowledge(self.gid, l.plan.leg_id, oid)
        for x in v["executions"]:
            if not x["link_id"].startswith(self.gid + "-"):
                continue
            leg_id = x["link_id"][len(self.gid) + 1:]
            leg = g.legs.get(leg_id)
            pos_leg = leg.parent if (leg is not None and leg.kind == "EXIT" and leg.parent) else None          # an exit closes the ENTRY leg's position, not a leg of its own
            self.ledger.record_fill(T.Fill(x["fill_id"], self.gid, leg_id, x["symbol"], x["side"], x["qty"], x["price"], x["fee_usd"], "USDT", x["liquidity"], int(x["ts_ms"]),
                                           "MEASURED" if x.get("fee_known", True) else "UNKNOWN"),      # a Trade row with no fee stated is an UNKNOWN fee: the ledger books fees as unknown, never a measured zero
                                  category=x["category"], position_leg=pos_leg)
        try:
            now = int(self.ctx.clock())
            for sym, snap in self._snaps.get().items():
                self.ledger.mark(sym, now, snap.mark, category="linear", bid=snap.bids[0].price if snap.bids else None, ask=snap.asks[0].price if snap.asks else None)
                if snap.spot_mark is not None:
                    self.ledger.mark(sym, now, snap.spot_mark, category="spot")
        except (TE.ExecutorError, bc.BybitAPIError, AC.LedgerError):
            pass                                                       # a mark that cannot be read leaves the valuation flagged, never guessed

    def reconcile_step(self, now: int) -> None:
        """The VENUE read is the reference position (never ``engine.positions()``): engine, ledger, reservations and the ledger-vs-venue executions are compared against it; a
        disagreement HALTS new exposure (the engine's and the executor's)."""
        v = self.last_view
        if v is None or self.engine is None:
            return
        if self.halter is None:
            self.halter = MR.HaltOnDisagreement.for_engine(self.engine, auto_resume=True)    # clears ONLY on a later report that is clean, and a clean report needs a COMPLETE venue read
        rep = MR.reconcile(MR.collect_views(engine=self.engine, ledger=self.ledger, authority=self.authority, venue_read=v, now_ms=now))
        self.recon_report = self.halter(rep, now)
        if rep.halt_new_exposure:
            self.ex.halt("RECONCILE_DISAGREEMENT")
        elif self.halter.active is None:
            self.ex.unhalt("RECONCILE_DISAGREEMENT")
        sym = self.plan["symbol"]
        mine = [x for x in v["executions"] if x["link_id"].startswith(self.gid + "-")]
        pos = v["positions"]
        venue_pos = None if pos is None else {sym: pos.get(f"linear:{sym}", 0.0), "SPOT:" + sym: _minus(pos.get(f"spot:{sym}", 0.0), self.baseline.get(f"spot:{sym}", 0.0))}
        rows = [{"execId": x["fill_id"], "execQty": x["qty"], "execPrice": x["price"], "execFee": x["fee_usd"], "side": x["side"], "symbol": x["symbol"], "orderLinkId": x["link_id"]}
                for x in mine]
        self.venue_recon = self.ledger.reconcile_with_venue(rows, venue_pos, None, sum(float(x["fee_usd"]) for x in mine) if v["complete"] else None, executions_complete=bool(v["complete"]))

    def release_dead_exits(self) -> None:
        g = self.engine.groups.get(self.gid) if self.engine else None
        for l in (g.exit_legs() if g else []):
            if l.status in (EX.REJECTED, EX.CANCELLED) and l.link not in self._released:
                self._released.add(l.link)
                left = l.plan_qty - l.filled
                if left > 0:
                    self.authority.release_exit(l.link, format(left.normalize(), "f"))

    def venue_flat(self, view: Optional[Mapping[str, Any]]) -> bool:
        """FLAT for this group's symbols from a COMPLETE venue read: perp size zero, spot balance equal to the pre-session baseline within dust (<= max(step, 0.2% of the entry qty)), no
        open order of the group and no foreign open order. Never from fill arithmetic."""
        if not view or not view.get("complete") or view.get("positions") is None:
            return False
        pos = view["positions"]
        for sym in sorted({l.symbol for l in self.group_plan.legs}):
            dust = max(float(self.ex.filters("spot", sym).qty_step), 0.002 * max(float(l.qty) for l in self.group_plan.legs if l.symbol == sym))
            if abs(pos.get(f"linear:{sym}", 0.0)) >= 1e-12 or abs(_minus(pos.get(f"spot:{sym}", 0.0), self.baseline.get(f"spot:{sym}", 0.0))) > dust:
                return False
        return not view.get("foreign_open_orders") and not [o for o in view["orders"].values() if o["status"] in ("New", "PartiallyFilled")]

    def bookkeep(self) -> None:
        g = self.engine.groups.get(self.gid) if self.engine else None
        if g is None:
            return
        for l in g.entry_legs():
            if l.status == EX.FILLED and l.plan.leg_id not in self._settled:
                self._settled.add(l.plan.leg_id)
                self.authority.settle_leg(self.gid, l.plan.leg_id)
                self._state.invalidate()                                # the account read must be taken AFTER the fill this settle reflects
        self.release_dead_exits()
        terminal = g.state in (T.GroupState.CLOSED, T.GroupState.FAILED)
        resolved = self.authority.is_resolved(self.gid)
        if terminal and not resolved and self.venue_flat(self.last_view):
            self.authority.resolve_group(self.gid)                      # released ONLY after a COMPLETE venue read shows the group's symbols flat
        elif not terminal and resolved:
            self.authority.reopen_group(self.gid)                       # a late fill: the engine is unwinding again, so its exits must be authorised

    def make_engine(self, *, recover: bool, allow_exits: bool = True) -> Any:
        store = EX.FileStore(self.ctx.path("journal"))
        kw = dict(send_order=self.send, cancel_order=self.ex.cancel_order, venue_state=self.venue_state, policy=EX.ExecPolicy(reconcile_every_step=True, max_account_age_ms=15_000,
                                                                                                                                  max_market_age_ms=15_000),
                  qty_steps=self.ex.qty_steps())
        if not recover:
            self.engine = EX.ExecutionEngine(store, **kw)
            return self.engine
        # RECOVERY never sends an ENTRY (a SENT/UNKNOWN entry leg with no venue order is REPORTED, not re-sent: that needs the full execute gates) and sends a reduce-only exit only
        # when the owner confirmed the unwind. Cancel / adopt (executions, order status) are untouched.
        eng = EX.ExecutionEngine(store, **kw)
        real = eng._send_leg
        self.suppressed: List[Tuple[str, str, str]] = []

        def guarded(g: Any, lp: Any, kind: str, parent: Any, ts: int) -> bool:
            if kind != "EXIT" or not allow_exits:
                self.suppressed.append((g.plan.group_id, lp.leg_id, kind))
                return False
            return real(g, lp, kind, parent, ts)
        eng._send_leg = guarded                                         # type: ignore[method-assign]
        self.engine = eng
        eng.reconcile(int(self.ctx.clock()))
        return eng

    def unresolved_entries(self) -> List[str]:
        g = self.engine.groups.get(self.gid) if self.engine else None
        return [l.link for l in (g.entry_legs() if g else []) if l.status in (EX.SENT, EX.UNKNOWN)]

    def unresolved_exits(self) -> List[str]:
        """EXIT legs still SENT/UNKNOWN: an exit whose create call failed (timeout, 5xx, malformed answer) twice (the original send and its one idempotent resend) is neither live nor rejected, and while
        any leg is in that state the engine sends NO new exit link: the group is wedged with whatever exposure it holds. Only the owner's confirmed unwind may close such a leg out."""
        g = self.engine.groups.get(self.gid) if self.engine else None
        return [l.link for l in (g.exit_legs() if g else []) if l.status in (EX.SENT, EX.UNKNOWN)]

    def state(self) -> str:
        g = self.engine.groups.get(self.gid) if self.engine else None
        return g.state.value if g else "NONE"

    def on_kill(self, reason: str = "KILL_SWITCH") -> None:
        if not self.killed:
            self.killed = True
            self.stopped = reason
            now = int(self.ctx.clock())
            self.ex.halt(reason)
            assert self.engine is not None
            self.engine.halt_new_exposure(reason, now)
            if self.gid in self.engine.groups:
                self.engine.request_exit(self.gid, now, reason)

    def enforce_limits(self, now: int) -> Optional[str]:
        """Lifecycle (<= the manifest / plan hours from the session start) and the manifest's loss limit (equity fall since the session's first account read) are enforced DURING the run:
        either one halts new exposure and unwinds reduce-only. Returns the reason that fired (once)."""
        if self.killed:
            return None
        if self.start_ms is not None and now - self.start_ms >= self.lifecycle_ms:
            self.on_kill("LIFECYCLE_EXCEEDED")
            return "LIFECYCLE_EXCEEDED"
        try:
            eq = float(self._state.get()["equity_usd"])
        except (TE.ExecutorError, bc.BybitAPIError, KeyError, TypeError):
            return None                                                 # an unreadable account is not a loss; entries are refused by the authority's freshness check anyway
        if self.start_equity is not None and self.start_equity - eq >= self.max_loss_usd:
            self.on_kill("LOSS_LIMIT")
            return "LOSS_LIMIT"
        return None

    # ---- MEASURED execution quality (observation only: a fault here is counted and NEVER reaches the trading path)
    def execution_quality(self, view: Mapping[str, Any], final: bool, flat: Optional[bool]) -> Optional[Dict[str, Any]]:
        """Write ``exec_measurements.json`` (what this process saw and when, merged with what an earlier process of the same session saw: a restart keeps its timestamps) and
        ``execution_quality.json`` (the canonical per-leg and group records). The session record itself has a closed schema and is untouched."""
        g = self.engine.groups.get(self.gid) if self.engine else None
        if g is None:
            return None
        try:
            prior = _read_json(self.ctx.path("meas"))
            meas: Dict[str, Dict[str, Any]] = {k: dict(v) for k, v in (prior or {}).items() if isinstance(v, dict)} if isinstance(prior, dict) else {}
            for link, v in self.ex.meas.items():
                meas.setdefault(link, {}).update({k: x for k, x in v.items() if x is not None})
            ws = {(int(t), str(v)) for t, v in (meas.get("__wallet__", {}).get("samples", []))} | {(int(t), str(v)) for t, v in self.ex.wallet_samples}
            meas["__wallet__"] = {"samples": [[t, v] for t, v in sorted(ws)][-5000:]}
            facts = {int(x["ts_ms"]): x for x in (meas.get("__wallet_facts__", {}).get("samples", []) or []) if isinstance(x, dict) and isinstance(x.get("ts_ms"), int)}
            facts.update({int(x["ts_ms"]): x for x in getattr(self.ex, "wallet_facts", [])})       # Round 19: committed margin / borrow / interest as the venue STATED them (None = not stated)
            ordered = [facts[k] for k in sorted(facts)]
            meas["__wallet_facts__"] = {"samples": ordered[:1] + ordered[1:][-399:] if ordered else []}
            _write_json(self.ctx.path("meas"), meas)
            dec = _read_json(self.ctx.path("decision"))
            decision_id = dec.get("decision_id") if isinstance(dec, dict) else None
            legs: List[Dict[str, Any]] = []
            for l in sorted(g.legs.values(), key=lambda x: x.link):
                vo = view["orders"].get(l.link)
                if vo is None and final:
                    try:
                        row = self.ex.lookup(l.plan.category, l.plan.symbol, l.link)       # a signed READ of one (possibly closed) order: its terminal status and venue timestamps
                    except TE.ExecutorError:
                        row = None
                    if row is not None:
                        vo = {"order_id": row.get("orderId"), "status": row.get("orderStatus"), "filled_qty": row.get("cumExecQty"), "avg_price": row.get("avgPrice"),
                              "created_ms": TE._ms(row.get("createdTime")), "updated_ms": TE._ms(row.get("updatedTime"))}
                order = None if vo is None else {"order_id": vo.get("order_id"), "status": vo.get("status"), "cum_qty": vo.get("filled_qty"), "avg_price": vo.get("avg_price"),
                                                "created_ms": vo.get("created_ms"), "updated_ms": vo.get("updated_ms")}
                m = meas.get(l.link, {})
                legs.append(EQ.leg_quality(link_id=l.link, group_id=self.gid, leg_id=l.plan.leg_id, kind=l.kind, candidate_id=self.group_plan.candidate_id, decision_id=decision_id,
                                           plan=asdict(l.plan), ref=m.get("reference"), timing={k: m.get(k) for k in ("sent_ms", "acked_ms", "cancel_sent_ms", "cancel_acked_ms", "cancel_result")},
                                           order=order, fills=[x for x in view["executions"] if x["link_id"] == l.link]))
            known = {l.plan.leg_id for l in g.legs.values()}
            for lp in self.group_plan.legs:                              # PLANNED entry legs the engine never sent (a crash between two legs): they exist in the record, unfilled, so a one-legged group is never "hedged"
                if lp.leg_id not in known:
                    legs.append(EQ.leg_quality(link_id=f"{self.gid}-{lp.leg_id}", group_id=self.gid, leg_id=lp.leg_id, kind="ENTRY", candidate_id=self.group_plan.candidate_id, decision_id=decision_id,
                                               plan=asdict(lp), ref=None, timing=None, order=None, fills=[]))
            doc = EQ.session_quality(legs, style=self.group_plan.style, notional_usd=self.group_plan.notional_usd, flat_verified=flat, decision_id=decision_id)
            _write_json(self.ctx.path("quality"), doc)
            return doc
        except Exception as exc:                                       # noqa: BLE001 - measurement must never touch trading; the fault is counted and visible
            self.quality_errors = getattr(self, "quality_errors", 0) + 1
            self.ctx.out(f"execution quality not written ({type(exc).__name__}); trading is unaffected")
            return None

    # ---- funding settlement recognition: what the held hedge was paid or charged, from the venue's own records (never from a model)
    def recognise_funding(self, view: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """For each perp symbol of the group: the public settlement history, the venue's Funding rows and the wallet samples -> ``funding_settlement.recognise`` -> the ONE ledger. Funding is booked only
        when the venue's rows agree with the physical expectation (and the wallet, when sampled); it is declared zero only when a complete public history shows no held settlement; otherwise it stays
        UNKNOWN (None). The recognition (with every quarantined row) is written to ``funding_settlement.json``. A fault leaves funding UNKNOWN; it never blocks a flat check or an unwind."""
        g = self.engine.groups.get(self.gid) if self.engine else None
        if g is None:
            return None
        try:
            now = int(self.ctx.clock())
            prior = _read_json(self.ctx.path("meas"))
            wallet = {(int(t), str(v)) for t, v in ((prior or {}).get("__wallet__", {}).get("samples", []) if isinstance(prior, dict) else [])} | {(int(t), str(v)) for t, v in self.ex.wallet_samples}
            samples = sorted(wallet)
            docs: List[Dict[str, Any]] = []
            for sym in sorted({l.plan.symbol for l in g.legs.values() if l.plan.category == "linear"}):
                mine = [x for x in view["executions"] if x["link_id"].startswith(self.gid + "-")]
                perp = [{"ts_ms": x["ts_ms"], "side": x["side"], "qty": x["qty"], "price": x["price"]} for x in mine if x["category"] == "linear" and x["symbol"] == sym]
                other = [x["ts_ms"] for x in mine if not (x["category"] == "linear" and x["symbol"] == sym)]
                prints = self.ex.funding_history(sym, min(p["ts_ms"] for p in perp) - 1000, now) if perp else None
                rows = [u for u in view.get("unattributed", []) if u.get("type") == "Funding" and u.get("symbol") == sym]
                try:
                    interval = self.ex.filters("linear", sym).funding_interval_min
                except Exception:                                      # noqa: BLE001 - an unknown interval claims nothing
                    interval = None
                doc = FS.recognise(symbol=sym, perp_fills=perp, other_fill_ts=other, prints=prints, rows=rows, wallet_samples=samples, now_ms=now, interval_min=interval)
                for a in doc["actions"]:
                    if a["action"] == "record":
                        self.ledger.record_funding(sym, int(a["ts_ms"]), a["amount_usd"], a["prov"])
                    elif a["action"] == "declare_none":
                        self.ledger.declare_no_funding(sym, a["reason"], a["prov"])
                docs.append(doc)
            out = {"schema": "funding_settlement_session.v1", "recognitions": docs, "at_ms": now}
            _write_json(self.ctx.path("funding"), out)
            return out
        except Exception as exc:                                       # noqa: BLE001 - funding stays UNKNOWN; trading and flat verification are untouched
            self.ctx.out(f"funding recognition failed ({type(exc).__name__}); funding stays UNKNOWN")
            return None

    def _suppress_funding_doc(self, reason: str) -> None:
        """The funding recognition already written for this session is withdrawn: marked ``suppressed`` and every recognition unresolved with nothing booked, so the evidence event and the research feedback row (which read
        this document) say UNKNOWN too. The ledger's own funding sum is not consulted again after this point."""
        try:
            path = self.ctx.path("funding")
            doc = _read_json(path)
            if not isinstance(doc, dict):
                return
            doc["suppressed"] = reason
            for r in doc.get("recognitions") or []:
                if isinstance(r, dict):
                    r["all_prints_resolved"], r["booked_usd"] = False, None
                    for pr in r.get("prints") or []:                           # every print was judged against the held window, which is what is now unknown: its verdict and figures are withdrawn too (the
                        if isinstance(pr, dict):                                # FUNDING_SETTLEMENT evidence events are built from these rows and used to publish BOOKED 0.48 beside a record that said UNKNOWN)
                            if pr.get("status") != "SUPPRESSED":                # a SECOND withdrawal must not overwrite what the first one kept (the venue's own figures)
                                pr["was"] = {"status": pr.get("status"), "amount_usd": pr.get("amount_usd"), "expected_usd": pr.get("expected_usd")}
                            pr["status"], pr["amount_usd"], pr["expected_usd"], pr["reason"] = "SUPPRESSED", None, None, f"SUPPRESSED:{reason}"
            _write_json(path, doc)
        except Exception as exc:                                       # noqa: BLE001 - the record's own net is already unknown; a document that cannot be rewritten is stated, never trusted
            self.ctx.out(f"funding document could not be withdrawn ({type(exc).__name__}); treat its figures as UNKNOWN")

    def write_reconciliation(self, view_complete: bool) -> None:
        """Round 18 (execution specialist): the ledger-vs-venue result of the FINAL reconcile was assigned to ``self.venue_recon`` and never read or persisted, so whether a session's executions, ledger
        fills, positions and fees agreed with the venue could not be audited afterwards. Written to ``reconciliation.json`` (finding CODES only, never a free-text detail) beside the other session
        documents; evidence only: it feeds no halt, flat or authority decision, and a write that fails changes nothing."""
        vr = self.venue_recon if isinstance(self.venue_recon, dict) else None
        rr = self.recon_report
        doc = {"schema": "ms_session_reconciliation/1", "view_complete": bool(view_complete),
               "venue_reconciled": None if vr is None else vr.get("venue_reconciled") is True,
               "finding_codes": [] if vr is None else sorted({str(f.get("code"))[:60] for f in (vr.get("findings") or []) if isinstance(f, dict) and f.get("code")})[:12],
               "checked": None if vr is None else {k: v for k, v in (vr.get("checked") or {}).items() if k in ("executions", "ledger_fills", "wallet")},
               "halt_new_exposure": None if rr is None else bool(getattr(rr, "halt_new_exposure", False)),
               "note": "evidence only: no halt, flat or authority decision reads this document"}
        try:
            _write_json(self.ctx.path("recon"), doc)
        except Exception:                                              # noqa: BLE001 - evidence must never affect safety or availability
            pass

    def snapshot_record(self, rec: Dict[str, Any], final: bool = False) -> Dict[str, Any]:
        view = self.ex.venue_state()
        if view.get("complete") or not (rec.get("orders") or rec.get("executions")):       # an INCOMPLETE read never erases rows an earlier complete read already recorded
            rec["orders"] = _orders_for_record(view["orders"], self.gid)
            rec["executions"] = _execs_for_record(view["executions"], self.gid)
        rec["basis"] = "EXCHANGE_REPORTED" if rec["orders"] or rec["executions"] else "NOT_RUN"
        if final:
            pos = view["positions"]
            complete = bool(view["complete"]) and pos is not None
            # {symbol: qty}: perp positions by symbol; the spot leg as "SPOT:<symbol>" = balance MINUS the owner's pre-session baseline (0 when flat)
            rec["final_position"] = ({k.split(":", 1)[1]: v for k, v in pos.items() if k.startswith("linear:")}
                                     | {"SPOT:" + k.split(":", 1)[1]: _minus(v, self.baseline.get(k, 0.0)) for k, v in pos.items() if k.startswith("spot:")}) if pos is not None else None
            flat = self.venue_flat(view) if complete else False
            rec["flat_verified"] = bool(flat)                          # None only before the final read; False when the read was incomplete or anything remains
            self.last_view = view
            self.feed_ledger()
            if flat:
                self.recognise_funding(view)                         # only once the position is closed: the held window is then complete
            self.reconcile_step(int(self.ctx.clock()))
            self.write_reconciliation(complete)
            cn = self.ledger.canonical_net()                          # worker F's canonical net: None (never zero) wherever a component is unknown (funding is not read here)
            rec["net"] = {"fees_usd": cn["fees"], "funding_usd": cn["funding"], "price_pnl_usd": cn["price_leg"] if flat else None, "note": NOTE}
            errors_before = getattr(self, "quality_errors", 0)
            doc = self.execution_quality(view, True, bool(flat))
            integrity = [f for f in cn.get("flags", []) if str(f).startswith(AC.INTEGRITY_FLAG_PREFIXES)]
            quality_failed = getattr(self, "quality_errors", 0) > errors_before                     # the final measurement RAISED: the incomplete-fills safeguard below never ran
            incomplete = bool(doc) and "fills_incomplete" in (doc.get("group") or {}).get("unknown", [])
            if not complete or integrity or quality_failed or incomplete:
                # (0) Round 18 (execution specialist): the FINAL venue read itself was incomplete. A symbol's order and execution reads share one try-block, so an order-read failure also drops that symbol's
                # executions: the ledger holds only what THIS read returned (a lower bound, or a measured 0.0 with both entry legs filled and fees paid) and no `fills_incomplete` flag can fire when the order
                # rows are missing too. Nothing from an incomplete final read is a measurement.
                # (1) An order the VENUE says filled has no (or too few) execution rows: the ledger, which is fed only from executions, then holds a LOWER BOUND of the fees and the price leg, not a
                # measurement. (2) The ledger itself flags a conflict in its inputs (a duplicate fill with different content, an orphan fill ...): its partial sums are not a net. (3) The measurement that
                # would have told us (1) failed. Publishing any of them would turn "we could not tell" into a measured number (the capital book's `known` is None whenever either is None, so such a
                # session can debit a loss but never credit a gain).
                # Funding goes with them in EVERY case (round-10 specialist, H1): the held window starts at the first VISIBLE perp fill, so when an execution the venue's list does not show (or a failed measurement) leaves
                # that start wrong, a settlement the pair was really held through falls BEFORE it, "no settlement while held" is declared (a DERIVED zero) and a held +0.48 USD was published as 0.0 into the record, the
                # evidence event and the research feedback row. An unknown window cannot be a zero.
                rec["net"] = {**rec["net"], "fees_usd": None, "price_pnl_usd": None, "funding_usd": None}
                self._suppress_funding_doc("INCOMPLETE_FINAL_READ" if not complete else ("FILLS_INCOMPLETE" if incomplete else ("LEDGER_INTEGRITY" if integrity else "MEASUREMENT_FAILED")))
        elif rec["orders"] or rec["executions"]:
            self.execution_quality(view, False, None)
        return view


def _step_once(ctx: Ctx, run: Run, rec: Dict[str, Any]) -> str:
    now = int(ctx.clock())
    if _kill(ctx)[0]:
        run.on_kill()
    else:
        why = run.enforce_limits(now)
        if why:
            ctx.out(f"STOP: {why} -- new exposure halted, unwinding reduce-only")
    assert run.engine is not None
    run.engine.step(now, market_ts_ms=now, account_ts_ms=run.ex.last_view_ms or now)
    run.bookkeep()
    run.feed_ledger()
    run.reconcile_step(now)
    run.snapshot_record(rec)
    save_record(ctx, rec)
    return run.state()


def _drive(ctx: Ctx, run: Run, rec: Dict[str, Any], done: Sequence[str], timeout_s: float) -> str:
    t0 = int(ctx.clock())
    while True:
        st = _step_once(ctx, run, rec)
        now = int(ctx.clock())
        if st in done:
            return st
        if now - t0 >= timeout_s * 1000:
            return "TIMEOUT"
        ctx.sleep(float(ctx.plan_poll))


def _entries_path() -> str:
    return os.path.join(os.path.dirname(AD.DEFAULT_CAPABILITY_PATH), "ms_testnet_entries.jsonl")


def _utc_day(ms: int) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ms / 1000.0, _dt.timezone.utc).strftime("%Y-%m-%d")


def entries_today(now_ms: int) -> int:
    """Executes already started today (UTC), across state dirs: an append-only file next to the owner capability file. Unreadable = the cautious answer (a large number)."""
    try:
        n = 0
        with open(_entries_path(), encoding="utf-8") as fh:
            for line in fh:
                if line.strip() and json.loads(line).get("day") == _utc_day(now_ms):
                    n += 1
        return n
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):
        return 10 ** 6


def record_entry(now_ms: int, gid: str) -> None:
    fd = os.open(_entries_path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, (json.dumps({"day": _utc_day(now_ms), "group_id": gid, "at_ms": now_ms}) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def _gate_execute(ctx: Ctx) -> Tuple[List[str], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    a = ctx.args
    why: List[str] = []
    unattended = bool(getattr(a, "unattended", False))
    if ctx.inner is None and (ctx.env.get("PYTEST_CURRENT_TEST") or (ctx.env.get("CI") and not unattended)):       # CI is allowed ONLY on the unattended path, and only with a valid grant (below)
        why.append("EXECUTE_REFUSED_IN_CI_OR_TEST_WITHOUT_AN_EXPLICITLY_INJECTED_TRANSPORT")
    manifest, st = _load_capability_manifest()
    if manifest is None:
        why.append("OWNER_CAPABILITY_FILE:" + ",".join(st.get("reasons", [])))
    p = TT.TestnetCredentials.presence(ctx.env)
    if not (p["dedicated_key_present"] and p["dedicated_secret_present"]):
        why.append("DEDICATED_TESTNET_CREDENTIALS_MISSING")
    plan = _read_json(ctx.path("plan"))
    why += plan_refusals(plan)
    digest = plan.get("plan_digest") if isinstance(plan, dict) else None
    if not plan_refusals(plan):
        if not a.confirm_plan_digest or a.confirm_plan_digest != digest:
            why.append("CONFIRM_FLAG_DOES_NOT_EMBED_THE_PLAN_DIGEST")
    if isinstance(plan, dict) and manifest is not None:
        if plan.get("manifest_plan_sha256") != manifest["plan_sha256"]:
            why.append("PLAN_IS_FOR_ANOTHER_MANIFEST")
        if ctx.clock() - int(plan.get("created_ms", 0)) > MAX_PLAN_AGE_MIN * 60_000:
            why.append("PLAN_TOO_OLD")
    if unattended and ctx.pre_order_check is None:                      # ONLY the autopilot loop supplies this hook, and the loop is what enforces the halts (ALERT, STOP, PAUSED, the capital book) and the single-writer lease
        why.append("UNATTENDED_ONLY_VALID_UNDER_THE_AUTOPILOT_LOOP: a grant replaces the typed digest, it does not replace the halts and the lease the loop enforces; run the loop, or use the typed `execute`")
    if unattended:                                                      # the owner's standing, bounded, expiring authorisation replaces the typed digest: nothing else is relaxed
        import ms_testnet_autopilot as AP
        if manifest is not None and isinstance(plan, dict) and not plan_refusals(plan):
            why += ["UNATTENDED:" + r for r in AP.unattended_refusals(int(ctx.clock()), plan, manifest)]
        else:
            why.append("UNATTENDED:NEEDS_A_VALID_PLAN_AND_MANIFEST")
    pre = _read_json(ctx.path("preflight"))
    if not (isinstance(pre, dict) and pre.get("status") == "PASS" and pre.get("preflight_digest") == digest_of(pre, ("preflight_digest",))):
        why.append("NO_PASSING_PREFLIGHT")
    elif ctx.clock() - int(pre["checked_ms"]) > a.max_preflight_age_min * 60_000:
        why.append("PREFLIGHT_STALE")
    elif isinstance(plan, dict) and plan.get("preflight_digest") != pre.get("preflight_digest"):
        why.append("PREFLIGHT_IS_NOT_THE_ONE_THE_PLAN_WAS_BUILT_FROM")
    if _kill(ctx)[0]:
        why.append("KILL_SWITCH_ENGAGED")
    if manifest is not None and entries_today(int(ctx.clock())) >= int(manifest["bounds"]["max_entries_per_utc_day"]):
        why.append("MAX_ENTRIES_PER_UTC_DAY_REACHED")
    if os.path.exists(ctx.path("journal")) and os.path.getsize(ctx.path("journal")) > 0:
        why.append("SESSION_ALREADY_EXECUTED_USE_RECONCILE")
    if not why and not unattended:
        typed = ctx.input_fn(f"Type the plan digest to confirm TESTNET execution ({digest}): ").strip()
        if typed != digest:
            why.append("TYPED_CONFIRMATION_DOES_NOT_MATCH")
    return why, (plan if isinstance(plan, dict) else None), manifest


def cmd_execute(ctx: Ctx) -> int:
    why, plan, manifest = _gate_execute(ctx)
    if why:
        ctx.out("execute REFUSED (no network call was made): " + "; ".join(why))
        return EXIT_REFUSED
    assert plan is not None and manifest is not None
    lock = _account_lock(ctx, "execute")
    if not lock.try_acquire():
        h = XL.read_holder(lock.path)
        ctx.out("execute REFUSED (no network call was made): another order-capable session holds the account lock" + (f" ({h['role']}, pid {h['pid']}, {h['state_dir']})" if h else "")
                + "; two writers on one account are never allowed. If an exit is needed use: reconcile --unwind --confirm-plan-digest <digest>")
        return EXIT_REFUSED
    try:
        again = _state_gates(ctx, manifest)                            # the gates were evaluated BEFORE the (possibly minutes-long) typed prompt and the lock: what another process could have changed since is checked again
        if again:
            ctx.out("execute REFUSED (no network call was made): " + "; ".join(again))
            return EXIT_REFUSED
        return _execute_locked(ctx, plan, manifest)
    finally:
        lock.release()


def _state_gates(ctx: Ctx, manifest: Mapping[str, Any]) -> List[str]:
    """The gates whose answer can change while a prompt waits: the kill switch, the entries-per-day counter and whether this state directory already ran a session."""
    why: List[str] = []
    if _kill(ctx)[0]:
        why.append("KILL_SWITCH_ENGAGED")
    if entries_today(int(ctx.clock())) >= int(manifest["bounds"]["max_entries_per_utc_day"]):
        why.append("MAX_ENTRIES_PER_UTC_DAY_REACHED")
    if os.path.exists(ctx.path("journal")) and os.path.getsize(ctx.path("journal")) > 0:
        why.append("SESSION_ALREADY_EXECUTED_USE_RECONCILE")
    return why


def _account_lock(ctx: Ctx, role: str) -> "XL.ExecutorLock":
    return XL.ExecutorLock(XL.lock_path(AD.DEFAULT_CAPABILITY_PATH), role=role, state_dir=ctx.dir, now_ms=int(ctx.clock()))


def close_out_unanswered_entries(ctx: Ctx, run: "Run") -> None:
    """The unattended twin of the owner's ``reconcile --unwind`` close-out, for ENTRY legs ONLY (an unanswered EXIT leg still needs the owner's unwind): a leg whose create got no answer twice, that the
    venue has no trace of and that was sent at least ``CLOSE_OUT_MIN_AGE_MS`` ago never existed there; marking it done lets the engine unwind the leg that did fill (reduce-only) instead of leaving a
    one-legged group behind. Every leg is judged on a venue read made FOR IT (an earlier close-out sends exits, so a view read before the loop can be tens of seconds stale), and 'no trace' means all three of:
    a COMPLETE read that lists no order under the link, no execution under it, and the venue's own by-link lookup (which also sees CLOSED orders) answering 'no such order'. Any doubt, any unreadable
    answer: the leg is left for the owner."""
    try:
        g = run.engine.groups.get(run.gid) if run.engine else None
        if g is None:
            return
        for link in run.unresolved_entries():
            leg_id = link[len(run.gid) + 1:]
            leg = g.legs.get(leg_id)
            if leg is None or getattr(leg, "status", None) not in (EX.SENT, EX.UNKNOWN):
                continue                                                # resolved meanwhile (a close-out lets the engine adopt another leg's fill)
            view = run.ex.venue_state()
            if not (view.get("complete") and view.get("positions") is not None):
                return                                                  # an incomplete read proves nothing about ANY leg
            sent, now_ms = getattr(leg, "sent_ms", None), int(ctx.clock())
            if sent is not None and now_ms - int(sent) < CLOSE_OUT_MIN_AGE_MS:
                continue                                                # a create that left a moment ago may still reach the venue
            if link in view["orders"] or any(e.get("link_id") == link for e in view.get("executions", [])):
                continue                                                # it exists at the venue (an order, or a fill the order list has rolled past): reconcile adopts it
            if run.ex.lookup(leg.plan.category, leg.plan.symbol, link) is not None:
                continue                                                # a CLOSED order the open + recent list no longer shows
            ctx.out(f"ENTRY LEG {link}: no answer to its create and no trace at the venue (complete read, no execution, by-link lookup empty): closed out so the filled leg can unwind")
            run.engine.on_order_done(run.gid, leg_id, now_ms)
    except (TE.ExecutorError, bc.BybitAPIError):
        return


def _execute_locked(ctx: Ctx, plan: Dict[str, Any], manifest: Dict[str, Any]) -> int:
    rec = load_record(ctx)
    rec["confirmation"] = {"state": "CONFIRMED", "plan_digest_confirmed": plan["plan_digest"]}
    if getattr(ctx.args, "unattended", False):                          # provenance that the record schema (and the control centre) do not carry: who authorised it, and that no human typed the digest
        import ms_testnet_autopilot as AP
        AP.write_provenance(ctx.dir, int(ctx.clock()), plan, manifest)
    ctx.plan_poll = plan["timing"]["poll_s"]
    save_record(ctx, rec)
    try:
        run = Run(ctx, plan, manifest)
    except (MA.AuthorityConfigError, TT.TestnetTransportRefused, TT.TestnetCredentialsMissing) as exc:
        ctx.out(f"execute REFUSED at admission: {str(exc)[:200]}")
        return EXIT_REFUSED
    eng = run.make_engine(recover=False)
    sym, now = plan["symbol"], int(ctx.clock())
    run.start_ms = run.start_ms or now
    run._save_start()
    record_entry(now, run.gid)                                          # counts against max_entries_per_utc_day from here on (even if the venue then refuses)
    # 1. fresh clean-slate re-check (reads only): flat, no open or foreign orders, clock in sync
    try:
        run.client.sync_time()
        run.client.assert_clock_within(2000)
        view = run.ex.venue_state()
    except (bc.BybitAPIError, TE.ExecutorError) as exc:
        ctx.out(f"execute aborted before any order: {type(exc).__name__}")
        return EXIT_FAIL
    pos = view["positions"] or {}
    if not view["complete"] or view["positions"] is None or abs(pos.get(f"linear:{sym}", 0.0)) > 0 or view["foreign_open_orders"] or any(o["status"] in ("New", "PartiallyFilled") for o in view["orders"].values()):
        ctx.out("execute aborted before any order: the venue is not flat/clean or could not be read completely")
        return EXIT_FAIL
    run.baseline = {f"spot:{sym}": float(pos.get(f"spot:{sym}", 0.0))}
    run._save_start()                                                   # persist the baseline the session trades with (reconcile / a restart must use it)
    if not run.ex.set_leverage(sym, int(plan["leverage"])):
        ctx.out("execute aborted before any order: leverage could not be set")
        return EXIT_FAIL
    # 2. register (risk-approve on FRESH state), plan, approve, submit
    decision = run.authority.register_group(run.group_plan)
    if not decision.approved:
        ctx.out("execute aborted before any order: risk refused the group: " + ",".join(decision.reasons))
        return EXIT_FAIL
    eng.plan_group(run.group_plan, now)
    eng.approve_group(run.gid, decision, now)
    now = int(ctx.clock())
    eng.submit_group(run.gid, now, market_ts_ms=now, account_ts_ms=run.ex.last_view_ms or now)
    run.bookkeep()
    # 3. entry -> hold -> exit, all against the venue
    tm = plan["timing"]
    state = _drive(ctx, run, rec, ("HEDGED", "CLOSED", "FAILED"), tm["entry_wait_s"])
    if state == "HEDGED" and not run.killed:
        t_hold = int(ctx.clock())
        while int(ctx.clock()) - t_hold < tm["hold_s"] * 1000 and not _kill(ctx)[0]:
            if _step_once(ctx, run, rec) in ("CLOSED", "FAILED"):
                break
            ctx.sleep(float(tm["poll_s"]))
    if run.state() not in ("CLOSED", "FAILED"):
        close_out_unanswered_entries(ctx, run)                          # an entry leg whose create never got an answer (and the venue's COMPLETE read has no order under it) must not wedge the unwind of the leg that DID fill
        run.engine.request_exit(run.gid, int(ctx.clock()), "SESSION_EXIT")
        _drive(ctx, run, rec, ("CLOSED", "FAILED"), tm["exit_wait_s"])
    # 4. the final word: a COMPLETE venue read, never local arithmetic
    run.snapshot_record(rec, final=True)
    save_record(ctx, rec)
    ctx.out(f"group state {run.state()}; flat_verified={rec['flat_verified']}")
    if rec["flat_verified"] is not True:
        ctx.out("NOT VERIFIED FLAT: HUMAN MUST ACT (cancel orders / close positions on the testnet site, then run reconcile)")
        return EXIT_FAIL
    return EXIT_OK


def cmd_reconcile(ctx: Ctx) -> int:
    plan = _read_json(ctx.path("plan"))
    rec = load_record(ctx)
    manifest, st = _load_capability_manifest()
    if not (isinstance(plan, dict) and not plan_refusals(plan)) or not os.path.exists(ctx.path("journal")):
        ctx.out("nothing to reconcile: no executed session in this state dir")
        save_record(ctx, rec)
        return EXIT_OK
    why: List[str] = []
    if manifest is None:
        why.append("OWNER_CAPABILITY_FILE:" + ",".join(st.get("reasons", [])))
    if ctx.inner is None and (ctx.env.get("CI") or ctx.env.get("PYTEST_CURRENT_TEST")):
        why.append("RECONCILE_REFUSED_IN_CI_OR_TEST_WITHOUT_AN_EXPLICITLY_INJECTED_TRANSPORT")
    p = TT.TestnetCredentials.presence(ctx.env)
    if not (p["dedicated_key_present"] and p["dedicated_secret_present"]):
        why.append("DEDICATED_TESTNET_CREDENTIALS_MISSING")
    if ctx.args.unwind and ctx.args.confirm_plan_digest != plan["plan_digest"]:
        why.append("UNWIND_NEEDS_--confirm-plan-digest")
    if why:
        ctx.out("reconcile REFUSED (no network call was made): " + "; ".join(why))
        return EXIT_REFUSED
    assert manifest is not None
    lock = _account_lock(ctx, "unwind" if ctx.args.unwind else "reconcile")
    if not lock.try_acquire():
        if lock.error is not None:                                     # the LOCK is broken (a directory at its path, EACCES, no flock): that is not a holder
            if not ctx.args.unwind:
                ctx.out(f"reconcile REFUSED (nothing was sent): the account lock cannot be used ({lock.error}); fix {lock.path!r}. An emergency unwind is still possible with --unwind --confirm-plan-digest.")
                return EXIT_REFUSED
            ctx.out(f"WARNING: the account lock cannot be used ({lock.error}), so no running session can be holding it: the emergency unwind proceeds WITHOUT the lock "
                    f"(reduce-only exits; a holder would have failed the same way). Fix {lock.path!r} afterwards.")
        else:
            h = XL.read_holder(lock.path)
            if not ctx.args.unwind:
                ctx.out("reconcile REFUSED (nothing was sent): an order-capable session holds the account lock" + (f" ({h['role']}, pid {h['pid']}, {h['state_dir']})" if h else "")
                        + "; a reconcile can cancel a live leg and must not interleave with the session that owns the journal. For an emergency exit use --unwind --confirm-plan-digest.")
                return EXIT_REFUSED
            if not _hand_over_unwind(ctx, plan, lock, h):              # the emergency exit: the holder (the ONLY journal writer) is told to unwind; we wait for the lock, never run a second writer
                return EXIT_FAIL
    try:
        return _reconcile_locked(ctx, plan, manifest, rec)
    finally:
        lock.release()


#: lock holders whose loop reads its state dir's kill switch (the session tool: `execute` is also what the autopilot runs in-process). A legacy tool never does, so engaging a kill file for one
#: would only create a stray file and a pointless wait.
KILL_HONOURING_ROLES = ("execute", "unwind", "reconcile")


#: written into the holder's kill file by the owner's emergency unwind; the autopilot recognises it and STOPS (an ALERT marker) instead of re-arming after its cooldown
OWNER_UNWIND_KILL_REASON = "reconcile --unwind handover"
#: a leg sent less than this long ago is not closed out by the unwind: its request may still land after the fresh exit (Bybit's receive window is 5 s; this is a wide margin over it)
CLOSE_OUT_MIN_AGE_MS = 15_000


def _hand_over_unwind(ctx: Ctx, plan: Mapping[str, Any], lock: "XL.ExecutorLock", holder: Optional[Mapping[str, Any]]) -> bool:
    """The account lock is held by a running session and the owner asked for an emergency unwind. Engage THAT session's kill switch (it cancels entries, unwinds reduce-only and verifies flat, itself),
    then wait a bounded time for the lock. True once this process holds it; False (and a message naming who to stop) when the holder does not let go or cannot be handed over to."""
    if holder is None:                                                 # the holder may have JUST released (its note is cleared before the descriptor closes): look again for a few seconds
        for _ in range(20):
            if lock.try_acquire():
                ctx.out("the holder had just finished: reconciling against the venue")
                return True
            holder = XL.read_holder(lock.path)
            if holder is not None:
                break
            ctx.sleep(0.25)
    if not holder:
        ctx.out("unwind NOT started: the account lock is held but its holder cannot be identified (no readable note). Stop the order-capable process (autopilot-ctl drain / stop the app), then run this again. "
                "Nothing was sent; no second writer was started.")
        return False
    sd = holder["state_dir"]
    if holder["role"] not in KILL_HONOURING_ROLES:
        ctx.out(f"unwind NOT started: the account lock is held by a {holder['role']} process (pid {holder['pid']}), which does not read a kill switch. Stop that process (kill {holder['pid']}), then run this again. "
                "Nothing was sent; no file was created.")
        return False
    if not (os.path.isabs(sd) and os.path.isdir(sd) and (os.path.exists(os.path.join(sd, FILES["journal"])) or os.path.exists(os.path.join(sd, FILES["plan"])))):
        ctx.out(f"unwind NOT started: the lock note names {sd!r}, which is not an absolute path to a session state directory. Stop pid {holder['pid']} by hand, then run this again. Nothing was sent.")
        return False
    kill = os.path.join(os.path.realpath(sd), FILES["kill"])
    try:
        created = AD.engage_kill_switch(kill, OWNER_UNWIND_KILL_REASON)
    except OSError as exc:
        ctx.out(f"unwind NOT started: the holder's kill switch could not be engaged ({type(exc).__name__}). Stop pid {holder['pid']} by hand, then run this again. Nothing was sent.")
        return False
    try:                                                                # the kill file is exclusive-create: when it already holds another reason this marker still tells the autopilot who asked
        fd = os.open(os.path.join(os.path.realpath(sd), FILES["owner_unwind"]), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.write(fd, OWNER_UNWIND_KILL_REASON.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass                                                            # advisory: the kill switch itself is what unwinds
    ctx.out(f"the account lock is held by {holder['role']} pid {holder['pid']} ({sd}): its kill switch was {'engaged' if created else 'already engaged'} so IT unwinds reduce-only (one writer). "
            "Waiting for it to finish; the kill file stays engaged until you remove it on purpose. An autopilot holder stops with an ALERT when its session ends: clear it only when you mean trading to resume.")
    deadline = int(ctx.clock()) + (int(plan["timing"]["exit_wait_s"]) + 60) * 1000
    while int(ctx.clock()) < deadline:
        if lock.try_acquire():
            ctx.out("the holder has finished: reconciling against the venue")
            return True
        ctx.sleep(2.0)
    ctx.out(f"unwind handover TIMED OUT: pid {holder['pid']} still holds the lock. Stop that process, then run this again. Nothing was sent by this command.")
    return False


def _reconcile_locked(ctx: Ctx, plan: Dict[str, Any], manifest: Dict[str, Any], rec: Dict[str, Any]) -> int:
    ctx.plan_poll = plan["timing"]["poll_s"]
    try:
        run = Run(ctx, plan, manifest)
    except (MA.AuthorityConfigError, TT.TestnetTransportRefused, TT.TestnetCredentialsMissing) as exc:
        ctx.out(f"reconcile REFUSED at admission: {str(exc)[:200]}")
        return EXIT_REFUSED
    run.client.sync_time()
    eng = run.make_engine(recover=True, allow_exits=bool(ctx.args.unwind))   # adopt / cancel only: NO entry is ever re-sent here; an exit only with the confirmed --unwind
    if eng.journal.entries and run.start_ms is None:
        run.start_ms = int(eng.journal.entries[0]["ts_ms"])
    run.feed_ledger()
    run.bookkeep()
    stuck = run.unresolved_entries()
    stuck_exits = run.unresolved_exits()
    g = eng.groups.get(run.gid)
    unsent = [l.leg_id for l in run.group_plan.legs if g is not None and l.leg_id not in g.legs]
    if unsent and g is not None and g.legs:                             # the crash came between two legs: the engine only knows legs that were sent, so it can call a one-legged group HEDGED
        ctx.out(f"UNSENT PLAN LEG(S) {unsent}: they were never sent, so the group is NOT hedged whatever its state says; they are not sent now. Unwind with --unwind --confirm-plan-digest.")
    for link in stuck:
        ctx.out(f"UNRESOLVED ENTRY LEG {link}: no venue order is known for it; it is NOT re-sent (a new entry needs the full execute gates)")
    for link in stuck_exits:
        ctx.out(f"UNRESOLVED EXIT LEG {link}: its create call never got an answer and no more resends are allowed, so the group sends no further exit; only reconcile --unwind --confirm-plan-digest "
                "closes it out (when the venue's COMPLETE read has no order under it) and sends a fresh reduce-only exit")
    if run.start_ms is not None and int(ctx.clock()) - run.start_ms >= run.lifecycle_ms and run.state() not in ("CLOSED", "FAILED"):
        ctx.out("LIFECYCLE_EXCEEDED: the session is past its lifecycle bound; unwind required (reconcile --unwind --confirm-plan-digest)")
    if ctx.args.unwind and run.state() not in ("CLOSED", "FAILED"):
        view = run.last_view
        now_ms = int(ctx.clock())
        for link in stuck + stuck_exits:                                # a leg the venue (COMPLETE read) has no order for never existed there: close it out so the unwind can proceed (an exit leg left
            if not (view and view.get("complete") and link not in view["orders"]):   # SENT/UNKNOWN would otherwise wedge the group for good: the engine sends no exit while any leg is unresolved)
                continue
            leg_id = link[len(run.gid) + 1:]
            leg = g.legs.get(leg_id) if g is not None else None
            sent = getattr(leg, "sent_ms", None)
            if sent is not None and now_ms - int(sent) < CLOSE_OUT_MIN_AGE_MS:      # a request that left a moment ago may still be on its way: closing it out and sending a fresh order could make TWO
                ctx.out(f"LEG {link} was sent only {(now_ms - int(sent)) / 1000.0:.0f}s ago and may still reach the venue; it is NOT closed out (a late landing would double the order). "
                        f"Wait {(CLOSE_OUT_MIN_AGE_MS - (now_ms - int(sent))) / 1000.0:.0f}s and run the unwind again.")
                continue
            eng.on_order_done(run.gid, leg_id, now_ms)
        cleared = eng.clear_exit_halt(run.gid, int(ctx.clock()))       # the owner's CONFIRMED unwind is the one thing allowed to lift an exit halt (journalled): a halted group would otherwise send no exit
        if cleared:
            ctx.out(f"group halt '{cleared}' lifted by the confirmed unwind (journalled); exits are reduce-only")
        eng.request_exit(run.gid, int(ctx.clock()), "RECONCILE_UNWIND")
        st = _drive(ctx, run, rec, ("CLOSED", "FAILED", "HEDGED"), plan["timing"]["exit_wait_s"])
        if st == "HEDGED":                                              # the imbalance exit left a BALANCED remainder (the engine's UNWIND mode ends there): the owner asked for flat, so exit all of it
            eng.request_exit(run.gid, int(ctx.clock()), "RECONCILE_UNWIND")
            _drive(ctx, run, rec, ("CLOSED", "FAILED"), plan["timing"]["exit_wait_s"])
    run.snapshot_record(rec, final=True)
    save_record(ctx, rec)
    ctx.out(f"group state {run.state()}; engine halts={sorted(eng.halts)}; flat_verified={rec['flat_verified']}")
    return EXIT_OK if rec["flat_verified"] is True and run.state() in ("CLOSED", "FAILED") else EXIT_FAIL


def cmd_assess(ctx: Ctx) -> int:
    """READ-ONLY diagnostic: what the entry authority would be shown right now. Only signed GETs on the Testnet host (no order, no leverage change, nothing written to the state dir).
    Prints the plan baseline, the baseline the execute step would take from the venue, and the account state (equity, positions) under each, so a risk refusal can be explained from numbers."""
    plan = _read_json(ctx.path("plan"))
    why = plan_refusals(plan)
    p = TT.TestnetCredentials.presence(ctx.env)
    if not (p["dedicated_key_present"] and p["dedicated_secret_present"]):
        why.append("DEDICATED_TESTNET_CREDENTIALS_MISSING")
    if why:
        ctx.out("assess REFUSED (no network call was made): " + "; ".join(why))
        return EXIT_REFUSED
    creds = TT.TestnetCredentials.from_mapping(ctx.env)
    inner = ctx.inner if ctx.inner is not None else TT.NoRedirectInner()
    client, _transport = TT.build_client(creds, inner)
    sym = plan["symbol"]
    ex = TE.TestnetExecutor(client, symbols=[sym], spot_symbols=[sym], clock_ms=ctx.clock, kill_reader=lambda: _kill(ctx))
    try:
        view = ex.venue_state()
        pos = view["positions"] or {}
        venue_baseline = {f"spot:{sym}": float(pos.get(f"spot:{sym}", 0.0))}
        plan_baseline = dict(plan.get("spot_baseline", {}))
        out: Dict[str, Any] = {"symbol": sym, "plan_notional_usd": plan.get("notional_usd"), "plan_baseline": plan_baseline, "venue_baseline": venue_baseline,
                               "venue_positions": pos, "venue_complete": bool(view["complete"]), "foreign_open_orders": len(view["foreign_open_orders"])}
        for name, base in (("state_with_plan_baseline", plan_baseline), ("state_with_venue_baseline", venue_baseline), ("state_with_no_baseline", {})):
            st = ex.account_state(base, 0)
            out[name] = {"equity_usd": st["equity_usd"], "positions": st["positions"]}
        # the authority's own pure risk check, per leg, on the venue-baseline state and the plan's policy (no order, no registration): the same reasons an order would be refused with
        gp = T.OrderGroupPlan(**{**plan["group"], "legs": tuple(T.LegPlan(**l) for l in plan["group"]["legs"])})
        st = ex.account_state(venue_baseline, 0)
        st["day_start_equity_usd"] = st["equity_usd"]
        snaps = {sym: ex.snapshot(sym)}
        out["policy_caps"] = {k: v for k, v in plan["policy"].items() if k.startswith("max_")}
        out["risk_per_leg"] = {}
        for lg in gp.legs:
            d = MRISK.risk_assess(replace(gp, legs=(lg,), created_ms=int(ctx.clock())), json.loads(json.dumps(st)), plan["policy"], snaps)
            out["risk_per_leg"][f"{lg.category}:{lg.side}:{lg.leg_id}"] = {"approved": d.approved, "reasons": list(d.reasons), "limits": d.limits}
        # registration assesses BOTH legs together (per-symbol = spot + perp, gross = the sum): a plan that passes each leg alone can still be refused there, after an entry was counted
        dg = MRISK.risk_assess(replace(gp, created_ms=int(ctx.clock())), json.loads(json.dumps(st)), plan["policy"], snaps)
        out["risk_whole_group"] = {"approved": dg.approved, "reasons": list(dg.reasons), "limits": dg.limits}
    except (TE.ExecutorError, bc.BybitAPIError) as exc:
        ctx.out(f"assess failed: {type(exc).__name__}: {str(exc)[:200]}")
        return EXIT_FAIL
    ctx.out(json.dumps(out, indent=2, sort_keys=True))
    return EXIT_OK


def cmd_status(ctx: Ctx) -> int:
    rec = load_record(ctx)
    if not os.path.exists(ctx.path("record")):
        save_record(ctx, rec)                                           # a valid NOT_RUN record, never invented evidence
    ctx.out(json.dumps(rec, indent=2, sort_keys=True))
    return EXIT_OK


# ---------------------------------------------------------------------------------------------------- entry points
def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Owner-invoked Bybit TESTNET session tool (fake money; refuses anything else).")
    ap.add_argument("--state-dir", default=os.path.join(os.path.expanduser("~"), ".ai-agent-control", "ms_testnet"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("preflight", "plan", "execute", "reconcile", "status", "assess"):
        s = sub.add_parser(name)
        s.add_argument("--max-preflight-age-min", type=int, default=DEFAULT_MAX_PREFLIGHT_AGE_MIN)
        if name == "preflight":
            s.add_argument("--max-skew-ms", type=int, default=2000)
        if name == "plan":
            s.add_argument("--symbol", required=True)
            s.add_argument("--notional-usd", type=float, required=True)
            s.add_argument("--leverage", type=int, default=1)
            s.add_argument("--hold-seconds", type=int, default=0)
            s.add_argument("--entry-wait-s", type=int, default=60)
            s.add_argument("--exit-wait-s", type=int, default=180)
            s.add_argument("--max-lifecycle-hours", type=float, default=1.0)
        if name in ("execute", "reconcile"):
            s.add_argument("--confirm-plan-digest", default="")
        if name == "execute":
            s.add_argument("--unattended", action="store_true",
                           help="no typed digest, and allowed inside CI: ONLY with a valid owner-signed bounded autopilot grant (tools/ms_testnet_autopilot.py grant); every other gate still applies")
        if name == "reconcile":
            s.add_argument("--unwind", action="store_true")
    return ap


def main(argv: Optional[Sequence[str]] = None, *, env: Optional[Mapping[str, str]] = None, inner_transport: Any = None, clock_ms: Optional[Callable[[], int]] = None,
         sleep: Optional[Callable[[float], None]] = None, input_fn: Optional[Callable[[str], str]] = None, out: Optional[Callable[[str], None]] = None,
         pre_order_check: Optional[Callable[[], Optional[str]]] = None) -> int:
    import time as _time
    a = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        ctx = Ctx(a, dict(env or {}), inner_transport, clock_ms or (lambda: int(_time.time() * 1000)), sleep or _time.sleep, input_fn or input, out or print)
    except StateDirRefused as exc:
        (out or print)(f"refused (nothing was created): {exc}")
        return EXIT_REFUSED
    ctx.plan_poll = 1                                                   # type: ignore[attr-defined]
    ctx.pre_order_check = pre_order_check
    return {"preflight": cmd_preflight, "plan": cmd_plan, "execute": cmd_execute, "reconcile": cmd_reconcile, "status": cmd_status, "assess": cmd_assess}[a.cmd](ctx)


LEASE_TTL_S = 120            # short on purpose: a crashed manual run frees the account in two minutes, and a live one renews far more often than that
LEASE_RENEW_EVERY_S = 30.0


def guarded_main(argv: Optional[Sequence[str]] = None, *, env: Optional[Mapping[str, str]] = None, acquire: Optional[Callable[..., Any]] = None,
                 run: Optional[Callable[..., int]] = None, monotonic: Optional[Callable[[], float]] = None, real_sleep: Optional[Callable[[float], None]] = None) -> int:
    """The OWNER'S command-line entry point. ``execute`` places Testnet orders, so it takes THE shared single-writer lease first (the autopilot holds the same one; a second order-capable
    process on the account would be a second hedge), RENEWS it from its own sleep hook for as long as the session runs (the server caps a lease at 3600 s and a plan may run for hours), and
    releases it in ``finally``. When a renewal cannot be proven the session's kill switch is engaged: entries stop, the hedge unwinds reduce-only and flat is verified (a lost lease never abandons a
    hedge). Refused (nothing sent) when the lease is not configured, is held by another executor, or does not answer. Every other command is read-only (or the lease-free protective unwind) and
    runs as before. The autopilot calls ``main`` directly while it already holds the lease, so it is not double-locked."""
    import time as _time
    argv_l = list(argv) if argv is not None else sys.argv[1:]
    environ = dict(env if env is not None else os.environ)
    run = run or main
    parsed = _parser().parse_args(argv_l)
    if parsed.cmd != "execute":
        return run(argv_l, env=environ)
    if getattr(parsed, "unattended", False):
        # `--unattended` replaces the typed digest with the owner's standing grant, and is only safe under the autopilot LOOP: the loop is what checks ALERT / STOP / PAUSED, the capital book (halts and a
        # review in force), grant revocation and the allocator, and it calls main() directly with its own pre-order hook. This command line supplies a hook that only renews the lease, so accepting the flag
        # here would run a full unattended session past every one of those stops (the guard in _gate_execute tests only that a hook EXISTS).
        print("execute --unattended REFUSED (no order was sent): an unattended session is valid only under the autopilot loop, which enforces the halts, the capital book and the revocation of the grant. "
              "Run the attended `execute --confirm-plan-digest <digest>` (you type the digest) or start the autopilot.")
        return EXIT_REFUSED
    import legacy_executor_guard as LG
    take = acquire or (lambda e: LG.acquire(e, ttl_seconds=LEASE_TTL_S, service="manual-session"))
    handle, why = take(environ)
    if handle is None:
        print("execute REFUSED (no order was sent): " + why)
        return EXIT_REFUSED
    mono, rsleep = monotonic or _time.monotonic, real_sleep or _time.sleep
    st = {"handle": handle, "t": mono(), "lost": False}
    kill_path = os.path.join(os.path.abspath(os.path.expanduser(parsed.state_dir)), FILES["kill"])

    def before_entry() -> Optional[str]:
        """The STRONG pre-order check (as the autopilot's): the server's own answer, asked before every ENTRY order. The typed-digest prompt can take minutes, so the lease taken at start may have
        lapsed: if nobody else holds it, take it again; if it is held elsewhere, the entry is refused (exits are never gated)."""
        if st["handle"].renew():
            return None
        fresh, why2 = take(environ)
        if fresh is None:
            return "EXECUTOR_LEASE_LOST:" + str(why2)[:80]
        st["handle"] = fresh
        return None

    def sleep(seconds: float) -> None:
        rsleep(seconds)
        if mono() - st["t"] >= LEASE_RENEW_EVERY_S:
            st["t"] = mono()
            if not st["handle"].renew() and not st["lost"]:
                st["lost"] = True
                AD.engage_kill_switch(kill_path, "executor lease could not be renewed")
                print("EXECUTOR LEASE LOST: the session's kill switch was engaged (entries stop, the hedge unwinds reduce-only).")

    try:
        return run(argv_l, env=environ, sleep=sleep, pre_order_check=before_entry)
    finally:
        st["handle"].release()


if __name__ == "__main__":
    sys.exit(guarded_main())
