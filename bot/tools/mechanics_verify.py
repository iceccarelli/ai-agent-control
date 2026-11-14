#!/usr/bin/env python3
"""READ-ONLY verifier of ONE Testnet mechanics session directory: did the machinery do what it is supposed to do, and can that be shown from evidence that was not produced by the thing being checked?

Round 22. The owner's Testnet approval buys MECHANICS evidence (orders, fills, fees, hedge, close, flat, reconciliation), never profit: a Testnet result is fake money and says nothing about Mainnet expectancy.
This tool turns the session's files into a closed verdict. It makes no network call, writes nothing, reads no secret and no clock; it reads the session directory (and, if given, an independent venue snapshot
taken AFTER the session by ``ms_testnet_autopilot.py venue-snapshot``).

Verdict per check: PASS / FAIL / UNKNOWN. Overall:

  MECHANICS_VERIFIED  every REQUIRED check passed. Needs the independent venue snapshot: a session's own ``flat_verified`` is a claim, not the proof.
  MECHANICS_FAILED    at least one check CONTRADICTS the claim (a wrong side, a duplicate order, a fee that does not add up, a venue that is not flat ...).
  INCONCLUSIVE        nothing contradicts it, but a required fact could not be established (no snapshot, no funding observation when one was required, a file missing or malformed).

``REPEATED_MECHANICS_VERIFIED`` (``verify_repeated``) needs at least two DISTINCT sessions (different group id, plan digest and decision) that are each MECHANICS_VERIFIED. Neither says anything about profit.

A session that sent orders but filled nothing verifies NOTHING (check C00): no unexercised mechanism is ever reported as working.

Usage:  python3 tools/mechanics_verify.py SESSION_DIR [--venue-snapshot FILE] [--require-funding | --no-funding] [--json]
Exit:   0 MECHANICS_VERIFIED, 1 MECHANICS_FAILED, 3 INCONCLUSIVE, 2 usage."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from decimal import Decimal as D
from typing import Any, Dict, Iterator, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [p for p in (HERE, os.path.dirname(HERE)) if p not in sys.path]
import economic_model as EM  # noqa: E402  (the ONE capital definition)
import ms_execution as EX  # noqa: E402  (verify_chain: the journal's own hash chain)
import ms_testnet_session as S  # noqa: E402  (record_refusals / plan_refusals / digest_of: the writers' own validators)
import venues as V  # noqa: E402

SCHEMA = "mechanics_verify/v1"
SNAPSHOT_SCHEMA = "mechanics_venue_snapshot/1"
VERIFIED, FAILED, INCONCLUSIVE = "MECHANICS_VERIFIED", "MECHANICS_FAILED", "INCONCLUSIVE"
ABS_TOL = D("0.000001")
FEE_BPS_MAX = D("25")           # a measured fee above 25 bps of leg notional is not a Bybit tier: it is a wrong number
CASH_TOL_ABS = D("0.02")        # the funding module's own wallet tolerance
HEDGE_QTY_TOL = D("1e-9")
#: a snapshot older than the last fill, or from before the session ended, proves nothing about the end state
SNAPSHOT_KEYS = ("schema", "read_ms", "complete", "linear_positions", "spot_balances", "open_orders_active", "open_orders_conditional")


def _json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _lines(path: str) -> Optional[List[str]]:
    try:
        with open(path, encoding="utf-8") as fh:
            return [ln.rstrip("\n") for ln in fh if ln.strip()]
    except OSError:
        return None


def _d(x: Any) -> Optional[D]:
    try:
        return None if x is None or isinstance(x, bool) else D(str(x))
    except Exception:  # noqa: BLE001
        return None


class _Rows:
    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []

    def add(self, cid: str, verdict: str, detail: str, required: bool = True, **facts: Any) -> None:
        self.rows.append({"id": cid, "verdict": verdict, "required": required, "detail": detail[:400], **({"facts": facts} if facts else {})})

    @contextlib.contextmanager
    def guard(self, cid: str, required: bool = True) -> Iterator[None]:
        """A check whose input is malformed or missing a field is UNKNOWN, never a pass and never a crash."""
        try:
            yield
        except Exception as exc:  # noqa: BLE001 - a verifier must not be taken down by a hostile or damaged file
            self.add(cid, "UNKNOWN", f"unreadable evidence: {type(exc).__name__}: {str(exc)[:120]}", required)

    def overall(self) -> str:
        if any(r["verdict"] == "FAIL" for r in self.rows):
            return FAILED
        if any(r["verdict"] == "UNKNOWN" and r["required"] for r in self.rows):
            return INCONCLUSIVE
        return VERIFIED


def snapshot_problems(snap: Any) -> List[str]:
    """Why a venue snapshot cannot be used (empty = usable): the keys the verifier reads must all be present and well-typed."""
    if not isinstance(snap, dict):
        return ["SNAPSHOT_NOT_AN_OBJECT"]
    why = [f"SNAPSHOT_KEY_MISSING:{k}" for k in SNAPSHOT_KEYS if k not in snap]
    if snap.get("schema") != SNAPSHOT_SCHEMA:
        why.append("SNAPSHOT_SCHEMA")
    if snap.get("complete") is not True:
        why.append("SNAPSHOT_INCOMPLETE")
    if not isinstance(snap.get("read_ms"), int) or isinstance(snap.get("read_ms"), bool):
        why.append("SNAPSHOT_READ_MS")
    for k in ("open_orders_active", "open_orders_conditional"):
        if k in snap and not isinstance(snap[k], list):
            why.append(f"SNAPSHOT_TYPE:{k}")
    return why


def verify(dirpath: str, snapshot: Optional[Dict[str, Any]] = None, *, require_funding: Optional[bool] = None) -> Dict[str, Any]:
    r = _Rows()
    rec, plan, pre = (_json(os.path.join(dirpath, n)) for n in (S.FILES["record"], S.FILES["plan"], S.FILES["preflight"]))
    q, meas, fund, recon = (_json(os.path.join(dirpath, n)) for n in ("execution_quality.json", "exec_measurements.json", "funding_settlement.json", "reconciliation.json"))
    lim, dec, auto = (_json(os.path.join(dirpath, n)) for n in ("run_limits.json", "decision.json", "autopilot.json"))
    jl = _lines(os.path.join(dirpath, S.FILES["journal"]))
    groups = _lines(os.path.join(dirpath, "ms_groups.jsonl"))
    if not all(isinstance(x, dict) for x in (rec, plan, pre, q, meas, recon, lim)) or jl is None:
        r.add("C01_FILES", "UNKNOWN", "a required session file is missing or unparsable (record, plan, preflight, execution_quality, exec_measurements, reconciliation, run_limits, journal)")
        return _result(dirpath, r)
    ctx: Dict[str, Any] = {}
    with r.guard("C01_INTEGRITY"):
        bad = S.record_refusals(rec) + S.plan_refusals(plan)
        chain_ok, chain_why = EX.verify_chain(jl)
        pre_ok = pre.get("preflight_digest") == S.digest_of(pre, ("preflight_digest",)) and pre.get("status") == "PASS"
        link_ok = rec["plan"]["digest"] == plan["plan_digest"] == rec["confirmation"]["plan_digest_confirmed"] and plan.get("preflight_digest") == pre.get("preflight_digest")
        ctx["chain_ok"] = chain_ok
        r.add("C01_INTEGRITY", "PASS" if not bad and chain_ok and pre_ok and link_ok else "FAIL",
              f"record/plan refusals={bad} journal_chain={chain_ok}:{chain_why} preflight_pass_digest={pre_ok} digest_links={link_ok}")
    with r.guard("C02_VENUE_IDENTITY"):
        host = V.get("testnet").host
        ident = rec["venue"] == "Testnet" and rec["endpoint_host"] == host == plan["endpoint_host"] and rec["identity"]["mode"] == "TESTNET" and any(c["name"] == "endpoint_identity" and c["ok"] for c in pre["checks"])
        r.add("C02_VENUE_IDENTITY", "PASS" if ident else "FAIL", f"venue={rec['venue']} host={rec['endpoint_host']} registry_host={host} mode={rec['identity']['mode']}")
    with r.guard("C03_ACCOUNT"):
        basis = pre.get("account_identity_basis")
        r.add("C03_ACCOUNT", "PASS" if basis == "USER_ID" and pre.get("account_ref") else "UNKNOWN", f"account_ref={pre.get('account_ref')} basis={basis}", account_ref=pre.get("account_ref"))
    with r.guard("C04_AUTHORITY"):
        if auto is not None:
            a_ok = auto.get("plan_digest") == plan["plan_digest"] and bool(auto.get("grant_sha256")) and isinstance(dec, dict) and bool(dec.get("decision_id")) and all(leg.get("decision_id") == dec.get("decision_id") for leg in q["legs"])
            r.add("C04_AUTHORITY", "PASS" if a_ok else "FAIL", f"autopilot.json plan_digest/grant_sha256 + decision.json decision_id carried by every execution_quality leg: {a_ok}")
        else:
            r.add("C04_AUTHORITY", "PASS", "typed session: the typed digest equals the plan digest (C01); no grant involved", required=False)
    with r.guard("C05_CAPITAL"):
        notional, usdt, lev = _d(plan["notional_usd"]), _d(pre.get("usdt")), int(plan["leverage"])
        need = D(str(EM.hedged_pair_capital_usd(D(repr(float(notional))), max(1, lev)).value)) if notional is not None else None
        ctx["usdt"] = usdt
        r.add("C05_CAPITAL", "PASS" if need is not None and usdt is not None and usdt >= need and notional <= D(str(plan["aggregate_ceiling_usd"])) else "FAIL",
              f"usdt={usdt} need={need} notional={notional} ceiling={plan['aggregate_ceiling_usd']}", capital_needed_usd=str(need))
    sym = plan.get("symbol")
    gid = (plan.get("group") or {}).get("group_id")
    qty: Optional[D] = None
    flt: Dict[str, Any] = {}
    with r.guard("C06_PLAN_LEGS"):
        legs = {leg["leg_id"]: leg for leg in plan["legs"]}
        flt = pre["filters"][sym]
        qty = _d(plan["group"]["legs"][0]["qty"])
        steps_ok = all(qty % D(str(flt[c]["step"])) == 0 for c in ("linear", "spot")) and qty >= D(str(flt["linear"]["min_qty"]))
        sides_ok = (legs.get("p", {}).get("side") == "Sell" and legs["p"]["category"] == "linear" and legs.get("s", {}).get("side") == "Buy" and legs["s"]["category"] == "spot"
                    and all(leg["symbol"] == sym for leg in legs.values()) and all(not leg["reduce_only"] for leg in legs.values()))
        r.add("C06_PLAN_LEGS", "PASS" if steps_ok and sides_ok else "FAIL", f"perp Sell + spot Buy of {qty} {sym}; steps_ok={steps_ok} sides_ok={sides_ok}", planned_qty=str(qty))
    ev: List[Dict[str, Any]] = []
    intents: List[Dict[str, Any]] = []
    exit_i: List[Dict[str, Any]] = []
    links: List[str] = []
    by_leg: Dict[str, List[Dict[str, Any]]] = {}
    qsum: Dict[str, D] = {}
    perp_in = spot_in = spot_base_fee_qty = D(0)
    exits_of: Dict[str, Any] = {}
    exercised = False
    with r.guard("C07_NO_DUPLICATE_ORDERS"):
        ev = [json.loads(line) for line in jl]
        intents = [e["body"] for e in ev if e["type"] == "INTENT"]
        acks: Dict[str, List[str]] = {}
        for e in ev:
            if e["type"] == "ACK":
                acks.setdefault(e["body"]["leg_id"], []).append(e["body"]["status"])
        links = [b["link"] for b in intents]
        entry_i = [b for b in intents if b["kind"] == "ENTRY"]
        exit_i = [b for b in intents if b["kind"] == "EXIT"]
        rec_links = [o["link_id"] for o in rec["orders"]]
        rec_oids = [o["order_id"] for o in rec["orders"]]
        dup_exit = {p: [b["leg_id"] for b in exit_i if b["parent"] == p and "ACCEPTED" in acks.get(b["leg_id"], [])] for p in ("p", "s")}
        ok = (len(links) == len(set(links)) and len(rec_links) == len(set(rec_links)) and len(rec_oids) == len(set(rec_oids)) and all(x.startswith(str(gid) + "-") for x in links + rec_links)
              and {b["leg_id"] for b in entry_i} == {"p", "s"} and len(entry_i) == 2 and all(len(v) <= 1 for v in dup_exit.values())
              and set(rec_links) <= set(links) and all(b["link"] in rec_links or acks.get(b["leg_id"], ["?"])[-1] == "REJECTED" for b in intents))
        r.add("C07_NO_DUPLICATE_ORDERS", "PASS" if ok else "FAIL", f"intents={len(links)} venue_orders={len(rec_links)} distinct_order_ids={len(set(rec_oids))} accepted_exits_per_parent={ {k: len(v) for k, v in dup_exit.items()} }")
    with r.guard("C08_EXEC_SIDE_SYMBOL"):
        for x in rec["executions"]:
            by_leg.setdefault(x["link_id"][len(str(gid)) + 1:], []).append(x)
        exits_of = {b["leg_id"]: (b["parent"], b["leg"]) for b in exit_i}
        exp_side = {"p": "Sell", "s": "Buy", **{k: v[1]["side"] for k, v in exits_of.items()}}
        side_ok = all(x["side"] == exp_side.get(k) and x["symbol"] == sym for k, xs in by_leg.items() for x in xs)
        r.add("C08_EXEC_SIDE_SYMBOL", "PASS" if side_ok and by_leg else ("UNKNOWN" if not by_leg else "FAIL"), f"legs with fills={sorted(by_leg)} every row symbol={sym} and side == the leg's planned side: {side_ok}")
    with r.guard("C09_FILL_QTY"):
        qsum = {k: sum((D(str(x["qty"])) for x in xs), D(0)) for k, xs in by_leg.items()}
        fee_ccy = {leg["leg_id"]: leg["fee"]["currencies"] for leg in q["legs"]}
        spot_base_fee_qty = sum((D(str(x["fee_usd"])) / D(str(x["price"])) for x in by_leg.get("s", [])), D(0)) if fee_ccy.get("s") and fee_ccy["s"] != ["USDT"] else D(0)
        perp_in, spot_in = qsum.get("p", D(0)), qsum.get("s", D(0))
        exercised = perp_in > 0 and spot_in > 0 and bool(exits_of) and all(qsum.get(k, D(0)) > 0 for k in exits_of)
        entry_ok = qty is not None and abs(perp_in - qty) <= HEDGE_QTY_TOL and abs(spot_in + spot_base_fee_qty - qty) <= HEDGE_QTY_TOL
        r.add("C09_FILL_QTY", "PASS" if entry_ok else ("UNKNOWN" if not exercised else "FAIL"), f"planned={qty} perp_filled={perp_in} spot_net={spot_in} spot_base_fee_qty={spot_base_fee_qty}; sum(fills)+fee-in-kind == planned")
    with r.guard("C10_HEDGE"):
        hedged_state = any(e["type"] == "STATE" and e["body"].get("to") == "HEDGED" for e in ev)
        hedge_ok = perp_in > 0 and abs(perp_in - (spot_in + spot_base_fee_qty)) <= HEDGE_QTY_TOL
        r.add("C10_HEDGE", "PASS" if hedge_ok and hedged_state else ("UNKNOWN" if not exercised else "FAIL"),
              f"hedge_ratio(net spot/perp)={None if perp_in == 0 else str((spot_in / perp_in).quantize(D('0.000001')))} reached_HEDGED={hedged_state} unhedged_window_ms={q['group'].get('unhedged_window_ms')}")
    rec_fee: Optional[D] = None
    net_total: Optional[D] = None
    with r.guard("C11_FEES"):
        fills_j = [e["body"]["fill"] for e in ev if e["type"] == "FILL"]
        prov_ok = all(f["prov"] == "MEASURED" and f["fee_ccy"] == "USDT" and D(str(f["fee_usd"])) >= 0 for f in fills_j) and len(fills_j) == len(rec["executions"])
        fee_rec = sum((D(str(x["fee_usd"])) for x in rec["executions"]), D(0))
        fee_q = sum((D(str(leg["fee"]["usd_signed"])) for leg in q["legs"] if leg["fee"]["usd_signed"] is not None), D(0))
        rec_fee = _d(rec["net"]["fees_usd"])
        bps: Dict[str, str] = {}
        for leg in q["legs"]:
            if leg["fills"]["notional"] and D(str(leg["fills"]["notional"])) > 0:
                bps[leg["link_id"][len(str(gid)) + 1:] + ":" + leg["instrument"]["category"] + ":" + leg["side"] + ":" + str(leg["liquidity"])] = str((D(str(leg["fee"]["usd_signed"])) / D(str(leg["fills"]["notional"])) * 10000).quantize(D("0.01")))
        plaus = all(D(v) <= FEE_BPS_MAX for v in bps.values())
        agree = rec_fee is not None and abs(fee_rec - rec_fee) <= ABS_TOL and abs(fee_q - rec_fee) <= ABS_TOL
        r.add("C11_FEES", "PASS" if prov_ok and plaus and agree else ("UNKNOWN" if rec_fee is None else "FAIL"),
              f"sum(executions.fee_usd)={fee_rec} quality={fee_q} record.net.fees_usd={rec_fee} all MEASURED/USDT-equivalent={prov_ok} plausible(<=25bps)={plaus}", measured_fee_bps=bps)
    with r.guard("C12_NET_IDENTITY"):
        px = sum(((D(1) if x["side"] == "Sell" else D(-1)) * D(str(x["qty"])) * D(str(x["price"])) for x in rec["executions"]), D(0))
        pnl = _d(rec["net"]["price_pnl_usd"])
        funding_usd = _d(rec["net"]["funding_usd"])
        net_total = None if (pnl is None or funding_usd is None or rec_fee is None) else pnl + funding_usd - rec_fee
        net_ok = pnl is not None and abs(px - pnl) <= ABS_TOL
        r.add("C12_NET_IDENTITY", "PASS" if net_ok and rec["flat_verified"] is True and net_total is not None else ("UNKNOWN" if (pnl is None or funding_usd is None) else "FAIL"),
              f"price_leg recomputed={px} record={pnl}; net=price_leg+funding-fees={net_total} (funding={funding_usd} fees={rec_fee})", net_usd=None if net_total is None else str(net_total))
    with r.guard("C13_FUNDING"):
        fr = (fund or {}).get("recognitions", [{}])[0] if fund else {}
        st = [p["status"] for p in fr.get("prints", [])]
        need_funding = bool((plan["timing"]["hold_s"] or 0) > 0) if require_funding is None else bool(require_funding)
        if not st:
            r.add("C13_FUNDING", "UNKNOWN" if need_funding else "PASS", f"no settlement inside the held window (flags={fr.get('flags')}); only a COMPLETE public history may say 'none'", required=need_funding)
        elif all(s.startswith("BOOKED") for s in st) and not (fund or {}).get("suppressed"):
            r.add("C13_FUNDING", "PASS", f"settlements booked: {st}; booked_usd={fr.get('booked_usd')}; flags={fr.get('flags')}", required=need_funding)
        else:
            r.add("C13_FUNDING", "FAIL" if any(s in ("CONFLICT", "WALLET_MISMATCH") for s in st) else "UNKNOWN",
                  f"statuses={st} suppressed={(fund or {}).get('suppressed')} (a QUARANTINED_SIGN row is the evidence that settles the sign convention, and stays UNKNOWN)", required=need_funding)
    with r.guard("C14_CLOSE"):
        pe = next((b for b in exit_i if b["parent"] == "p"), None)
        se = next((b for b in exit_i if b["parent"] == "s"), None)
        pe_ok = pe is not None and pe["leg"]["reduce_only"] is True and pe["leg"]["side"] == "Buy" and pe["leg"]["category"] == "linear" and qsum.get(pe["leg_id"], D(0)) == perp_in
        se_ok = se is not None and se["leg"]["side"] == "Sell" and se["leg"]["category"] == "spot" and qsum.get(se["leg_id"], D(0)) <= spot_in + HEDGE_QTY_TOL
        order_ok = bool(pe and se and by_leg.get(pe["leg_id"]) and by_leg.get(se["leg_id"])) and max(x["ts_ms"] for x in by_leg[pe["leg_id"]]) <= min(x["ts_ms"] for x in by_leg[se["leg_id"]])
        r.add("C14_CLOSE", "PASS" if pe_ok and se_ok and order_ok else ("UNKNOWN" if not exercised else "FAIL"),
              f"perp exit reduce-only Buy == entry qty: {pe_ok}; spot exit Sell <= spot held: {se_ok}; spot exit after perp exit fills: {order_ok}")
    with r.guard("C15_FINAL_VENUE_STATE"):
        last_fill = max((x["ts_ms"] for x in rec["executions"]), default=0)
        fp = rec["final_position"] or {}
        base = (lim.get("baseline") or {}).get(f"spot:{sym}", 0.0)
        dust = max(D(str(flt["spot"]["step"])), D("0.002") * (qty or D(0)))
        local_flat = (rec["flat_verified"] is True and abs(D(str(fp.get(sym, 1)))) == 0 and abs(D(str(fp.get("SPOT:" + str(sym), 1)))) <= dust
                      and recon.get("view_complete") is True and recon.get("venue_reconciled") is True and not recon.get("finding_codes"))
        if snapshot is None:
            r.add("C15_FINAL_VENUE_STATE", "UNKNOWN" if local_flat else "FAIL",
                  f"the session claims flat_verified={rec['flat_verified']} but carries no read timestamp and no raw open-order list: the read cannot be shown to follow the last fill ({last_fill}); take `ms_testnet_autopilot.py venue-snapshot` and pass --venue-snapshot")
        else:
            bad_snap = snapshot_problems(snapshot)
            if bad_snap:
                r.add("C15_FINAL_VENUE_STATE", "UNKNOWN", f"the venue snapshot cannot be used: {bad_snap}")
            else:
                snap_ok = (snapshot["read_ms"] > last_fill and all(abs(D(str(v))) == 0 for v in snapshot["linear_positions"].values())
                           and snapshot["open_orders_active"] == [] and snapshot["open_orders_conditional"] == []
                           and abs(D(str(snapshot["spot_balances"].get(flt["spot"]["base"], 0))) - D(str(base))) <= dust)
                r.add("C15_FINAL_VENUE_STATE", "PASS" if snap_ok and local_flat else "FAIL",
                      f"snapshot read_ms={snapshot['read_ms']} > last_fill={last_fill}; every linear position (ANY symbol) 0; active AND conditional open orders empty; spot base within dust of the baseline {base}")
                if net_total is not None and ctx.get("usdt") is not None and "usdt_balance" in snapshot:
                    dc = D(str(snapshot["usdt_balance"])) - ctx["usdt"]
                    r.add("C12b_CASH_IDENTITY", "PASS" if abs(dc - net_total) <= CASH_TOL_ABS else "FAIL",
                          f"venue USDT delta since preflight={dc} vs recomputed net={net_total} (tolerance {CASH_TOL_ABS}); valid only if nothing else touched the account", required=False)
    kill = os.path.exists(os.path.join(dirpath, S.FILES["kill"])) or os.path.exists(os.path.join(dirpath, S.FILES["owner_unwind"]))
    with r.guard("C16_LOCAL_STATE"):
        last_group = None
        for line in groups or []:
            e = json.loads(line)
            if e.get("group_id") == gid:
                last_group = e["event"]
        jl_end = ev[-1]["type"] == "STATE" and ev[-1]["body"]["to"] in ("CLOSED", "FAILED") and (any(e["type"] == "FLAT_VERIFIED" for e in ev) or not exercised)
        r.add("C16_LOCAL_STATE", "PASS" if last_group == "resolve" and jl_end and lim.get("gid") == gid and not kill else "FAIL",
              f"ms_groups last event={last_group}; journal ends CLOSED/FAILED with FLAT_VERIFIED={jl_end}; kill/unwind files present={kill}")
    with r.guard("C17_EVIDENCE"):
        q_links = {leg["link_id"] for leg in q["legs"]}
        r.add("C17_EVIDENCE", "PASS" if q_links == set(links) and q["group"]["group_id"] == gid and q["group"]["venue_flat_verified"] is True and ctx.get("chain_ok") else "FAIL",
              f"quality legs == journal intents: {q_links == set(links)}; quality.venue_flat_verified={q['group']['venue_flat_verified']}; chain={ctx.get('chain_ok')}")
    with r.guard("C18_TEARDOWN", required=False):
        r.add("C18_TEARDOWN", "PASS" if not kill and lim.get("gid") == gid else "FAIL", "no MS_KILL / OWNER_UNWIND in the session dir; the executor lock and the Supabase lease release are UNKNOWN offline (read the holder note / lease row)", required=False)
    with r.guard("C19_BORROW_MARGIN", required=False):
        samples = ((meas.get("__wallet_facts__") or {}).get("samples")) or []
        der = [x["facts"]["derived"] for x in samples if isinstance(x, dict) and isinstance(x.get("facts"), dict) and x["facts"].get("readable")]
        borrowed = [x for x in der if x.get("borrow_stated") not in (None, "0", 0)] + [x for x in der if x.get("accrued_interest_stated") not in (None, "0", 0)]
        stated = [x for x in der if x.get("borrow_stated") is not None and x.get("committed_initial_margin") is not None]
        hedge_sample = any(x.get("committed_initial_margin") not in (None, "0") for x in der)
        r.add("C19_BORROW_MARGIN", "FAIL" if borrowed else ("PASS" if stated and hedge_sample else "UNKNOWN"),
              f"samples={len(samples)} readable={len(der)} borrow/interest>0={len(borrowed)} venue stated borrow+IM in {len(stated)} samples; a sample WITH the hedge open and IM stated={hedge_sample}; None = not stated = UNKNOWN, never zero", required=False)
    r.add("C00_MECHANICS_EXERCISED", "PASS" if exercised else "UNKNOWN", f"both entry legs filled and both exits filled: {exercised} (a session that sent orders but filled nothing verifies NOTHING)")
    return _result(dirpath, r, group_id=gid, plan_digest=plan.get("plan_digest"), decision_id=(dec or {}).get("decision_id") if isinstance(dec, dict) else None,
                   net_usd=None if net_total is None else str(net_total))


def _result(dirpath: str, r: _Rows, **ident: Any) -> Dict[str, Any]:
    r.rows.sort(key=lambda x: x["id"])
    overall = r.overall()
    return {"schema": SCHEMA, "overall": overall, "session": os.path.basename(os.path.abspath(dirpath)), "checks": r.rows, **ident,
            "claim": "Testnet mechanics evidence only: fake money, not owner profit and not Mainnet expectancy" + ("" if overall == VERIFIED else "; NOT verified")}


def verify_repeated(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """REPEATED_MECHANICS_VERIFIED: at least two sessions, each MECHANICS_VERIFIED, with DISTINCT group id, plan digest and decision id (a copied directory is one session, not two)."""
    ok = [x for x in results if x.get("overall") == VERIFIED]
    keys = {(x.get("group_id"), x.get("plan_digest"), x.get("decision_id")) for x in ok}
    distinct = len(keys) == len(ok) and all(None not in k[:2] for k in keys)
    level = "REPEATED_MECHANICS_VERIFIED" if len(ok) >= 2 and distinct else ("MECHANICS_VERIFIED" if ok else "NOT_VERIFIED")
    return {"schema": "mechanics_repeated/v1", "level": level, "sessions": len(results), "verified": len(ok), "distinct": distinct,
            "claim": "mechanics only; no profit, no Mainnet expectancy"}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("session_dir", help="the session directory (ms_auto_<UTC> or the typed session's state dir)")
    ap.add_argument("--venue-snapshot", default=None, help="the JSON written by `ms_testnet_autopilot.py venue-snapshot` AFTER the session (the independent proof of the end state)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--require-funding", dest="require_funding", action="store_true", default=None, help="the session was meant to span a funding settlement: a missing observation is INCONCLUSIVE")
    g.add_argument("--no-funding", dest="require_funding", action="store_false", help="no funding observation is required (a mechanics session that closes before any settlement)")
    ap.add_argument("--json", action="store_true", help="print the full JSON result")
    args = ap.parse_args(argv)
    if not os.path.isdir(args.session_dir):
        print(f"not a directory: {args.session_dir}", file=sys.stderr)
        return 2
    snap = None
    if args.venue_snapshot:
        snap = _json(args.venue_snapshot)
        if snap is None:
            print(f"cannot read the venue snapshot: {args.venue_snapshot}", file=sys.stderr)
            return 2
    out = verify(args.session_dir, snap, require_funding=args.require_funding)
    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        for c in out["checks"]:
            print(f"{c['verdict']:8} {'req' if c['required'] else 'opt'} {c['id']:26} {c['detail'][:170]}")
        print("OVERALL:", out["overall"])
        print("NOTE:", out["claim"])
    return {VERIFIED: 0, FAILED: 1, INCONCLUSIVE: 3}[out["overall"]]


if __name__ == "__main__":
    raise SystemExit(main())
