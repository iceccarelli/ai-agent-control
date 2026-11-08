"""OWNER-INVOKED Bybit TESTNET session tool for the multi-symbol hedged carry (testnet only; fake money; never mainnet).

    python3 tools/ms_testnet_session.py preflight  --state-dir DIR                          # READ-ONLY: env, endpoint, key permissions, clock, modes, balances, filters, book, flat/empty
    python3 tools/ms_testnet_session.py plan       --state-dir DIR --symbol BTCUSDT --notional-usd 40     # offline: builds plan.json from the manifest + the preflight capture
    python3 tools/ms_testnet_session.py execute    --state-dir DIR --confirm-plan-digest <digest>        # then TYPE the digest at the prompt
    python3 tools/ms_testnet_session.py reconcile  --state-dir DIR [--unwind --confirm-plan-digest D]   # re-read the venue, re-verify an interrupted session
    python3 tools/ms_testnet_session.py status     --state-dir DIR                                       # prints testnet_session.json (no network)

Nothing here runs on import. ``main`` takes everything it needs as arguments (environment mapping, inner transport, clock, sleep, prompt) so the offline proof drives it against the
in-process fake testnet; the ``__main__`` wrapper is the ONLY place that reads ``os.environ`` and builds the real (no-redirect, no-proxy) inner transport. ``execute`` refuses
(exit 2, no network) unless ALL of: the owner capability file (never created by code) is valid, both dedicated credentials are present, the plan on disk still hashes to its
``plan_digest``, ``--confirm-plan-digest`` equals it AND the digest typed at the prompt equals it, a PASS preflight younger than ``--max-preflight-age-min`` exists for the same
manifest, the kill-switch file is absent, no session was already executed from this state dir, and (under CI / pytest) the transport was injected explicitly.

Bounds (at or below the owner's manifest, which is itself <= the repository ceilings): aggregate notional <= $100 across ALL symbols, ONE active group, explicit symbol allow-list,
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
import ms_accounting as AC  # noqa: E402
import ms_authority as MA  # noqa: E402
import ms_execution as EX  # noqa: E402
import ms_reconcile as MR  # noqa: E402
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
HARD_CEILING_USD = 100.0
MAX_LIFECYCLE_H = 9.0
DEFAULT_MAX_PREFLIGHT_AGE_MIN = 15
MAX_PLAN_AGE_MIN = 120
EXIT_OK, EXIT_FAIL, EXIT_REFUSED = 0, 1, 2
FILES = {"record": "testnet_session.json", "plan": "plan.json", "preflight": "preflight.json", "journal": "engine_journal.jsonl", "kill": "MS_KILL"}


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


class Ctx:
    def __init__(self, args: argparse.Namespace, env: Mapping[str, str], inner: Any, clock: Callable[[], int], sleep: Callable[[float], None], input_fn: Callable[[str], str],
                 out: Callable[[str], None]) -> None:
        self.args, self.env, self.inner, self.clock, self.sleep, self.input_fn, self.out = args, env, inner, clock, sleep, input_fn, out
        self.dir = os.path.abspath(args.state_dir)
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
                          spec_digest, data_digest, "UNKNOWN", now)
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


def _orders_for_record(view_orders: Mapping[str, Mapping[str, Any]], gid: str) -> List[Dict[str, Any]]:
    rows = []
    for link, o in sorted(view_orders.items()):
        if link.startswith(gid + "-"):
            rows.append({"link_id": link, "order_id": o.get("order_id"), "symbol": o.get("symbol"), "side": o.get("side"), "qty": o.get("qty"), "status": o.get("status"),
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
    snap: Dict[str, Any] = {"market": {}, "filters": {}, "spot_baseline": {}}
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
    for cat, f, ref in (("linear", lin, mark), ("spot", sp, D(str(mk["spot_ask"])))):
        if D(f["min_notional"]) > 0 and D(qty) * ref < D(f["min_notional"]):
            raise ValueError(f"{cat.upper()}_NOTIONAL_BELOW_MIN")
    spot_px = T.round_to_tick(mk["spot_ask"], sp["tick"], side="Buy", passive=False)
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
                                                     generated_ms=int(ctx.clock())), "TESTNET", s0["candidate_id"], s0["spec_digest"], pre["preflight_digest"], "UNKNOWN",
                                    int(ctx.clock())).as_dict()
    rec["plan"] = {"digest": plan["plan_digest"], "manifest_digest": plan["manifest_plan_sha256"],
                   "legs": [{"symbol": l["symbol"], "side": l["side"], "order_type": l["order_type"], "qty": l["qty"], "price": l["price"], "notional_usd": l["notional_usd"]} for l in plan["legs"]],
                   "notional_usd": plan["notional_usd"]}
    save_record(ctx, rec)
    ctx.out(json.dumps(plan, indent=2, sort_keys=True))
    ctx.out(f"PLAN DIGEST: {plan['plan_digest']}")
    ctx.out("To execute (testnet, fake money): execute --confirm-plan-digest <that digest>, then type it at the prompt.")
    return EXIT_OK


# ---------------------------------------------------------------------------------------------------- execute / reconcile machinery
class _Cached:
    """A provider that re-reads when its value is older than ``refresh_ms`` and, when a read FAILS, serves the last good value with its ORIGINAL timestamp (so the authority's
    freshness check, not this class, decides it is stale)."""

    def __init__(self, read: Callable[[], Any], clock: Callable[[], int], refresh_ms: int = 500) -> None:
        self.read, self.clock, self.refresh_ms = read, clock, refresh_ms
        self.value: Any = None
        self.at: Optional[int] = None

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

    def _load_start(self) -> None:
        d = _read_json(os.path.join(self.ctx.dir, "run_limits.json"))
        if isinstance(d, dict) and d.get("gid") == self.gid_hint():
            self.start_ms, self.start_equity = d.get("start_ms"), d.get("start_equity_usd")

    def gid_hint(self) -> str:
        return str(self.plan["group"]["group_id"])

    def _save_start(self) -> None:
        _write_json(os.path.join(self.ctx.dir, "run_limits.json"), {"gid": self.gid_hint(), "start_ms": self.start_ms, "start_equity_usd": self.start_equity})

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
        return max(0.0, -pos.get(f"linear:{symbol}", 0.0)), max(0.0, spot - float(self.baseline.get(f"spot:{symbol}", 0.0)))

    def venue_state(self) -> Dict[str, Any]:
        v = self.ex.venue_state()
        self.last_view = v
        return v

    # the engine's send hook: release dead exit reservations before an exit is authorised (as ms_pipeline does), hold a spot exit until the perp is closed, then the executor
    def send(self, req: EX.OrderRequest) -> Any:
        if req.leg.leg_id.startswith("x"):
            self.release_dead_exits()
            if self.authority.is_resolved(self.gid):                    # exposure exists (the engine is exiting): a group resolved earlier may be EXITED again, never re-entered
                self.authority.reopen_group(self.gid)
            if req.leg.category == "spot" and req.leg.side == "Sell" and self.hold_spot_exit:
                ack = self._await_perp_closed(req)
                if ack is not None:
                    return ack
        return self.ex.send_order(req)

    def _await_perp_closed(self, req: EX.OrderRequest) -> Optional[EX.Ack]:
        """A spot sell is an exit only once the perp it hedges is closed (``ms_authority.spot_sell_keeps_hedge``), and on a real exchange the perp exit's FILL arrives after its
        ack: the authority would refuse the spot sell and five such rejections HALT the group. So the spot exit waits until a COMPLETE venue position read shows the perp flat.
        None = go ahead; an Ack = give up (REJECTED, one rejection) after ``hold_spot_exit_s``."""
        sym = req.leg.symbol
        deadline = int(self.ctx.clock()) + int(self.hold_spot_exit_s * 1000)
        while True:
            v = self.ex.venue_state()
            pos = v["positions"]
            if v["complete"] and pos is not None and abs(pos.get(f"linear:{sym}", 0.0)) < 1e-12:
                return None
            if int(self.ctx.clock()) >= deadline:
                return EX.Ack(req.link_id, "REJECTED", None, "SPOT_EXIT_HELD_PERP_NOT_CLOSED")
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
                                           "MEASURED"), category=x["category"], position_leg=pos_leg)
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
        venue_pos = None if pos is None else {sym: pos.get(f"linear:{sym}", 0.0), "SPOT:" + sym: pos.get(f"spot:{sym}", 0.0) - float(self.baseline.get(f"spot:{sym}", 0.0))}
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
            if abs(pos.get(f"linear:{sym}", 0.0)) >= 1e-12 or abs(pos.get(f"spot:{sym}", 0.0) - float(self.baseline.get(f"spot:{sym}", 0.0))) > dust:
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

    def snapshot_record(self, rec: Dict[str, Any], final: bool = False) -> Dict[str, Any]:
        view = self.ex.venue_state()
        rec["orders"] = _orders_for_record(view["orders"], self.gid)
        rec["executions"] = _execs_for_record(view["executions"], self.gid)
        rec["basis"] = "EXCHANGE_REPORTED" if rec["orders"] or rec["executions"] else "NOT_RUN"
        if final:
            pos = view["positions"]
            complete = bool(view["complete"]) and pos is not None
            # {symbol: qty}: perp positions by symbol; the spot leg as "SPOT:<symbol>" = balance MINUS the owner's pre-session baseline (0 when flat)
            rec["final_position"] = ({k.split(":", 1)[1]: v for k, v in pos.items() if k.startswith("linear:")}
                                     | {"SPOT:" + k.split(":", 1)[1]: v - float(self.baseline.get(k, 0.0)) for k, v in pos.items() if k.startswith("spot:")}) if pos is not None else None
            flat = self.venue_flat(view) if complete else False
            rec["flat_verified"] = bool(flat)                          # None only before the final read; False when the read was incomplete or anything remains
            self.last_view = view
            self.feed_ledger()
            self.reconcile_step(int(self.ctx.clock()))
            cn = self.ledger.canonical_net()                          # worker F's canonical net: None (never zero) wherever a component is unknown (funding is not read here)
            rec["net"] = {"fees_usd": cn["fees"], "funding_usd": cn["funding"], "price_pnl_usd": cn["price_leg"] if flat else None, "note": NOTE}
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
    if ctx.inner is None and (ctx.env.get("CI") or ctx.env.get("PYTEST_CURRENT_TEST")):
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
    if not why:
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
    rec = load_record(ctx)
    rec["confirmation"] = {"state": "CONFIRMED", "plan_digest_confirmed": plan["plan_digest"]}
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
    g = eng.groups.get(run.gid)
    unsent = [l.leg_id for l in run.group_plan.legs if g is not None and l.leg_id not in g.legs]
    if unsent and g is not None and g.legs:                             # the crash came between two legs: the engine only knows legs that were sent, so it can call a one-legged group HEDGED
        ctx.out(f"UNSENT PLAN LEG(S) {unsent}: they were never sent, so the group is NOT hedged whatever its state says; they are not sent now. Unwind with --unwind --confirm-plan-digest.")
    for link in stuck:
        ctx.out(f"UNRESOLVED ENTRY LEG {link}: no venue order is known for it; it is NOT re-sent (a new entry needs the full execute gates)")
    if run.start_ms is not None and int(ctx.clock()) - run.start_ms >= run.lifecycle_ms and run.state() not in ("CLOSED", "FAILED"):
        ctx.out("LIFECYCLE_EXCEEDED: the session is past its lifecycle bound; unwind required (reconcile --unwind --confirm-plan-digest)")
    if ctx.args.unwind and run.state() not in ("CLOSED", "FAILED"):
        view = run.last_view
        for link in stuck:                                              # a leg the venue (COMPLETE read) has no order for never existed there: close it out so the unwind can proceed
            if view and view.get("complete") and link not in view["orders"]:
                eng.on_order_done(run.gid, link.split("-", 1)[1], int(ctx.clock()))
        eng.request_exit(run.gid, int(ctx.clock()), "RECONCILE_UNWIND")
        _drive(ctx, run, rec, ("CLOSED", "FAILED"), plan["timing"]["exit_wait_s"])
    run.snapshot_record(rec, final=True)
    save_record(ctx, rec)
    ctx.out(f"group state {run.state()}; engine halts={sorted(eng.halts)}; flat_verified={rec['flat_verified']}")
    return EXIT_OK if rec["flat_verified"] is True and run.state() in ("CLOSED", "FAILED") else EXIT_FAIL


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
    for name in ("preflight", "plan", "execute", "reconcile", "status"):
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
        if name == "reconcile":
            s.add_argument("--unwind", action="store_true")
    return ap


def main(argv: Optional[Sequence[str]] = None, *, env: Optional[Mapping[str, str]] = None, inner_transport: Any = None, clock_ms: Optional[Callable[[], int]] = None,
         sleep: Optional[Callable[[float], None]] = None, input_fn: Optional[Callable[[str], str]] = None, out: Optional[Callable[[str], None]] = None) -> int:
    import time as _time
    a = _parser().parse_args(list(argv) if argv is not None else None)
    ctx = Ctx(a, dict(env or {}), inner_transport, clock_ms or (lambda: int(_time.time() * 1000)), sleep or _time.sleep, input_fn or input, out or print)
    ctx.plan_poll = 1                                                   # type: ignore[attr-defined]
    return {"preflight": cmd_preflight, "plan": cmd_plan, "execute": cmd_execute, "reconcile": cmd_reconcile, "status": cmd_status}[a.cmd](ctx)


if __name__ == "__main__":
    sys.exit(main(env=os.environ))
