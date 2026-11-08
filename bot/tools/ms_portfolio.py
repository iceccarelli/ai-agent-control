"""ms_portfolio: portfolio state, constraints and a deterministic greedy allocator over ``list[Opportunity]`` (pure; no venue, no clock, no network).

Contract: opportunities are taken in order (-rank_score, candidate_id). Each is sized by the TIGHTEST of every constraint and every skip/allocation carries reason codes. A
MISSING input (state, price, depth, instrument filter, funding time, capacity, spot depth) BLOCKS the candidate with a ``MISSING_*`` code; it is never read as zero. Quantities
are rounded DOWN to the instrument step, limit prices to the tick on the passive side, and min notional / min qty / max qty are enforced. ``OrderGroupPlan`` ids are derived
from (candidate_id, spec_digest, decision_ms, symbols) and (group_id, symbol, category, side, index): the same inputs give the same ids (idempotent across a restart).

Constraints applied: capacity fraction, depth fraction (of the MEASURED book side the leg would consume), per-symbol notional and leverage, gross, net, aggregate leverage,
margin utilisation, cash/collateral buffer, liquidation distance (leverage is lowered to meet it), underlying / correlated-cluster / stablecoin concentration, funding exposure per
settlement time, max open groups, drawdown halt, and account/market staleness. An injected ``risk_assess`` (ms_risk contract) is consulted for every sized plan and may reject it.
"""
from __future__ import annotations

import hashlib
import math
import os
import sys
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ms_types as T  # noqa: E402

SPOT_PREFIX = "SPOT:"


@dataclass(frozen=True)
class Position:
    key: str                          # perp symbol, or "SPOT:<symbol>"
    category: str                     # linear | spot
    notional_usd: float               # SIGNED: long > 0
    next_funding_ms: Optional[int] = None
    underlying: Optional[str] = None
    quote: str = "USDT"


@dataclass(frozen=True)
class PortfolioState:
    """Every field Optional: None means UNKNOWN and blocks allocation."""
    equity_usd: Optional[float] = None
    cash_usd: Optional[float] = None
    peak_equity_usd: Optional[float] = None
    positions: Optional[Mapping[str, Position]] = None
    open_groups: Optional[int] = None
    margin_used_usd: Optional[float] = None
    market_ts_ms: Optional[int] = None
    account_ts_ms: Optional[int] = None

    def missing(self) -> List[str]:
        out = []
        for name in ("equity_usd", "cash_usd", "peak_equity_usd", "positions", "open_groups", "margin_used_usd", "market_ts_ms", "account_ts_ms"):
            v = getattr(self, name)
            if v is None or (isinstance(v, float) and not math.isfinite(v)):
                out.append(f"MISSING_{name.upper()}")
        if self.equity_usd is not None and not (self.equity_usd > 0):
            out.append("EQUITY_NOT_POSITIVE")
        return out

    def gross(self) -> float:
        return sum(abs(p.notional_usd) for p in (self.positions or {}).values())

    def net(self) -> float:
        return sum(p.notional_usd for p in (self.positions or {}).values())

    def to_risk_state(self) -> Dict[str, Any]:
        """The ms_risk contract shape."""
        return {"equity_usd": self.equity_usd, "positions": {k: {"category": p.category, "notional_usd": p.notional_usd} for k, p in (self.positions or {}).items()},
                "open_groups": self.open_groups, "market_ts_ms": self.market_ts_ms, "account_ts_ms": self.account_ts_ms}


@dataclass(frozen=True)
class PortfolioPolicy:
    policy_version: str = "ms-portfolio-v1"
    max_groups: int = 5
    one_group_per_symbol: bool = True
    min_net_bps: float = 0.0
    style: str = "MAKER_FIRST"
    max_wait_ms: int = 120_000
    capacity_fraction: float = 0.10
    depth_fraction: float = 0.10
    max_symbol_notional_usd: float = 50_000.0
    max_symbol_frac: float = 0.25                 # |notional| of one key / equity
    max_symbol_leverage: float = 0.50             # |notional| of one key / equity (exposure leverage)
    max_gross_frac: float = 1.0
    max_net_frac: float = 0.10
    max_aggregate_leverage: float = 1.0
    max_margin_util: float = 0.60
    cash_buffer_frac: float = 0.20
    fee_reserve_bps: float = 40.0
    perp_leverage: float = 3.0
    maint_margin_frac: float = 0.02
    min_liq_distance: float = 0.20
    max_underlying_frac: float = 0.40
    max_cluster_frac: float = 0.60
    max_stable_frac: float = 1.0
    max_funding_bucket_frac: float = 0.60
    max_drawdown_frac: float = 0.10
    max_book_age_ms: int = 5_000
    max_account_age_ms: int = 60_000
    max_market_age_ms: int = 60_000
    correlation_groups: Mapping[str, str] = field(default_factory=dict)       # base coin -> cluster name


@dataclass(frozen=True)
class AllocDecision:
    candidate_id: str
    action: str                                   # ALLOCATE | SKIP
    reasons: Tuple[str, ...]
    notional_usd: float = 0.0
    binding: str = ""
    group_id: str = ""
    constraints: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Allocation:
    plans: Tuple[T.OrderGroupPlan, ...]
    decisions: Tuple[AllocDecision, ...]
    state: PortfolioState
    metrics: Mapping[str, Any]


# ------------------------------------------------------------------ ids

def derive_group_id(candidate_id: str, spec_digest: str, decision_ms: int, symbols: Sequence[str]) -> str:
    h = hashlib.sha256("|".join([candidate_id, spec_digest, str(int(decision_ms)), ",".join(symbols)]).encode()).hexdigest()
    return "g" + h[:16]


def derive_leg_id(group_id: str, symbol: str, category: str, side: str, index: int) -> str:
    return "l" + hashlib.sha256("|".join([group_id, symbol, category, side, str(index)]).encode()).hexdigest()[:10]


# ------------------------------------------------------------------ metrics

def exposure_metrics(state: PortfolioState, policy: PortfolioPolicy, instruments: Mapping[str, T.Instrument]) -> Dict[str, Any]:
    pos = state.positions or {}
    eq = state.equity_usd if state.equity_usd and state.equity_usd > 0 else None
    per_symbol = {k: abs(p.notional_usd) for k, p in sorted(pos.items())}
    under: Dict[str, float] = {}
    cluster: Dict[str, float] = {}
    stable: Dict[str, float] = {}
    buckets: Dict[Any, float] = {}
    for k, p in pos.items():
        u = p.underlying or k
        under[u] = under.get(u, 0.0) + abs(p.notional_usd)
        c = policy.correlation_groups.get(u, u)
        cluster[c] = cluster.get(c, 0.0) + abs(p.notional_usd)
        stable[p.quote] = stable.get(p.quote, 0.0) + abs(p.notional_usd)
        if p.category == "linear":
            buckets[p.next_funding_ms] = buckets.get(p.next_funding_ms, 0.0) + abs(p.notional_usd)
    peak = state.peak_equity_usd
    dd = (peak - state.equity_usd) / peak if (peak and peak > 0 and state.equity_usd is not None) else None
    return {"gross_usd": state.gross(), "net_usd": state.net(), "per_symbol_usd": per_symbol, "underlying_usd": under, "cluster_usd": cluster, "stable_usd": stable,
            "funding_bucket_usd": {str(k): v for k, v in buckets.items()},
            "aggregate_leverage": state.gross() / eq if eq else None, "margin_util": (state.margin_used_usd / eq) if (eq and state.margin_used_usd is not None) else None,
            "max_symbol_leverage": (max(per_symbol.values()) / eq) if (eq and per_symbol) else (0.0 if eq else None), "drawdown_frac": dd,
            "cash_usd": state.cash_usd, "open_groups": state.open_groups}


# ------------------------------------------------------------------ helpers

def _book_notional(levels: Sequence[T.BookLevel]) -> float:
    return sum(l.price * l.qty for l in levels)


def _round_common_qty(q: float, steps: Sequence[str]) -> float:
    cur = Decimal(str(q))
    for _ in range(8):
        prev = cur
        for s in steps:
            cur = Decimal(T.round_down_to_step(cur, s))
        if cur == prev:
            break
    return float(cur)


def _fmt(x: float) -> str:
    return format(Decimal(str(x)).normalize(), "f") if x != 0 else "0"


def _blocked(cid: str, reasons: Sequence[str], binding: str = "") -> AllocDecision:
    return AllocDecision(cid, "SKIP", tuple(reasons), 0.0, binding)


def _fresh(ts: Optional[int], now: int, max_age: int, code: str) -> Optional[str]:
    if ts is None:
        return f"{code}_UNKNOWN"
    age = now - ts
    if age < 0:
        return f"{code}_FROM_THE_FUTURE"
    return f"{code}_STALE" if age > max_age else None


# ------------------------------------------------------------------ the allocator

def allocate(opps: Sequence[T.Opportunity], *, state: PortfolioState, policy: PortfolioPolicy, instruments: Mapping[str, T.Instrument],
             snapshots: Mapping[str, T.MarketSnapshot], decision_ms: int, spot_instruments: Optional[Mapping[str, T.Instrument]] = None,
             spot_depth_usd: Optional[Mapping[str, float]] = None, risk_assess: Optional[Callable[..., Any]] = None,
             risk_policy: Optional[Mapping[str, Any]] = None) -> Allocation:
    spot_instruments = spot_instruments or {}
    spot_depth_usd = spot_depth_usd or {}
    ordered = sorted(opps, key=lambda o: (-(o.rank_score if o.rank_score is not None else float("-inf")), o.candidate_id))
    global_block: List[str] = list(state.missing())
    if not global_block:
        for code, ts, age in (("ACCOUNT_TS", state.account_ts_ms, policy.max_account_age_ms), ("MARKET_TS", state.market_ts_ms, policy.max_market_age_ms)):
            r = _fresh(ts, decision_ms, age, code)
            if r:
                global_block.append(r)
        peak, eq = state.peak_equity_usd, state.equity_usd
        if peak and peak > 0 and (peak - eq) / peak >= policy.max_drawdown_frac:
            global_block.append("DRAWDOWN_HALT")
    cur = state
    plans: List[T.OrderGroupPlan] = []
    decisions: List[AllocDecision] = []
    held_keys = set((state.positions or {}).keys())
    for o in ordered:
        if global_block:
            decisions.append(_blocked(o.candidate_id, [f"BLOCK_{c}" for c in global_block]))
            continue
        d, plan, cur = _one(o, cur, policy, instruments, snapshots, decision_ms, spot_instruments, spot_depth_usd, risk_assess, risk_policy, held_keys)
        decisions.append(d)
        if plan is not None:
            plans.append(plan)
    return Allocation(tuple(plans), tuple(decisions), cur, exposure_metrics(cur, policy, instruments))


def _one(o: T.Opportunity, st: PortfolioState, pol: PortfolioPolicy, instruments, snapshots, decision_ms, spot_instruments, spot_depth, risk_assess, risk_policy,
         held_keys):
    cid = o.candidate_id
    skip = lambda reasons, binding="": (_blocked(cid, reasons, binding), None, st)  # noqa: E731
    if o.exclusions:
        return skip(["EXCLUDED:" + c for c in o.exclusions])
    if not o.legs:
        return skip(["NO_LEGS"])
    if o.rank_score is None or not math.isfinite(o.rank_score):
        return skip(["UNRANKABLE"])
    if o.net_bps_horizon is None:
        return skip(["NET_UNKNOWN"])
    if o.net_bps_horizon <= pol.min_net_bps:
        return skip(["NET_BELOW_MIN"])
    if o.capacity_usd is None or not o.capacity_usd > 0:
        return skip(["MISSING_CAPACITY"])
    if st.open_groups >= pol.max_groups:
        return skip(["MAX_GROUPS"])
    eq = float(st.equity_usd)

    # --- resolve every leg's market inputs; any hole blocks
    legs: List[Dict[str, Any]] = []
    blocks: List[str] = []
    for idx, (sym, side) in enumerate(o.legs):
        spot = sym.startswith(SPOT_PREFIX)
        base_sym = sym[len(SPOT_PREFIX):] if spot else sym
        key = sym
        inst_perp = instruments.get(base_sym)
        snap = snapshots.get(base_sym)
        if side not in ("Buy", "Sell"):
            blocks.append(f"BAD_SIDE:{sym}")
            continue
        if inst_perp is None:
            blocks.append(f"MISSING_INSTRUMENT:{base_sym}")
            continue
        if snap is None:
            blocks.append(f"MISSING_SNAPSHOT:{base_sym}")
            continue
        if inst_perp.status != T.TRADABLE_STATUS or not inst_perp.tradable_at(decision_ms):
            blocks.append(f"NOT_TRADABLE:{base_sym}")
            continue
        if spot:
            if inst_perp.spot_hedge != "HEDGEABLE_ON_BYBIT":
                blocks.append(f"NOT_HEDGEABLE:{base_sym}")
                continue
            inst = spot_instruments.get(base_sym)
            if inst is None:
                blocks.append(f"MISSING_SPOT_FILTERS:{base_sym}")
                continue
            if snap.spot_mark is None or not snap.spot_mark > 0:
                blocks.append(f"MISSING_SPOT_PRICE:{base_sym}")
                continue
            if base_sym not in spot_depth:
                blocks.append(f"MISSING_SPOT_DEPTH:{base_sym}")
                continue
            ref, depth = snap.spot_mark, float(spot_depth[base_sym])
            nf = None
        else:
            inst = inst_perp
            if snap.mark is None or not snap.mark > 0:
                blocks.append(f"MISSING_PRICE:{base_sym}")
                continue
            if snap.book_age_ms is None or snap.book_age_ms > pol.max_book_age_ms or not snap.bids or not snap.asks:
                blocks.append(f"MISSING_OR_STALE_BOOK:{base_sym}")
                continue
            if snap.funding_interval_min is None:
                blocks.append(f"MISSING_FUNDING_INTERVAL:{base_sym}")
                continue
            if snap.next_funding_ms is None:
                blocks.append(f"MISSING_FUNDING_TIME:{base_sym}")
                continue
            ref = snap.mark
            depth = _book_notional(snap.bids if side == "Sell" else snap.asks)
            nf = snap.next_funding_ms
        if None in (inst.tick_size, inst.qty_step, inst.min_order_qty, inst.max_order_qty, inst.min_notional_usd):
            blocks.append(f"MISSING_INSTRUMENT_FILTERS:{sym}")
            continue
        legs.append({"idx": idx, "key": key, "sym": base_sym, "side": side, "spot": spot, "inst": inst, "snap": snap, "ref": float(ref), "depth": depth, "nf": nf,
                     "underlying": inst_perp.base_coin or base_sym, "quote": inst_perp.quote_coin or "USDT"})
    if blocks:
        return skip(list(dict.fromkeys(blocks)))
    if pol.one_group_per_symbol and any(l["key"] in held_keys for l in legs):
        return skip(["SYMBOL_ALREADY_HELD"])

    n_legs = len(legs)
    perp_legs = [l for l in legs if not l["spot"]]
    lev_by = {}
    for l in perp_legs:
        ml = l["inst"].max_leverage
        cap = [pol.perp_leverage, 1.0 / (pol.min_liq_distance + pol.maint_margin_frac)]
        if ml is not None:
            cap.append(float(ml))
        lev_by[l["key"]] = max(1.0, min(cap))
    margin_frac = sum(1.0 / lev_by[l["key"]] for l in perp_legs)
    cash_frac = sum(1.0 if l["spot"] else 1.0 / lev_by[l["key"]] for l in legs) + pol.fee_reserve_bps / 1e4 * n_legs
    pos = st.positions or {}
    cons: Dict[str, float] = {}
    cons["CAPACITY"] = o.capacity_usd * pol.capacity_fraction
    for l in legs:
        cons[f"DEPTH:{l['key']}"] = l["depth"] * pol.depth_fraction
        cur_abs = abs(pos[l["key"]].notional_usd) if l["key"] in pos else 0.0
        cons[f"SYMBOL_NOTIONAL:{l['key']}"] = pol.max_symbol_notional_usd - cur_abs
        cons[f"SYMBOL_FRAC:{l['key']}"] = pol.max_symbol_frac * eq - cur_abs
        cons[f"SYMBOL_LEVERAGE:{l['key']}"] = pol.max_symbol_leverage * eq - cur_abs
    cons["GROSS"] = (pol.max_gross_frac * eq - st.gross()) / n_legs
    cons["AGG_LEVERAGE"] = (pol.max_aggregate_leverage * eq - st.gross()) / n_legs
    sgn = sum((1.0 if l["side"] == "Buy" else -1.0) for l in legs)
    net_now, net_cap = st.net(), pol.max_net_frac * eq
    if sgn != 0:
        s = 1.0 if sgn > 0 else -1.0
        room = (net_cap - abs(net_now)) if s * net_now >= 0 else (net_cap + abs(net_now))
        cons["NET"] = room / abs(sgn)
    if margin_frac > 0:
        cons["MARGIN_UTIL"] = (pol.max_margin_util * eq - st.margin_used_usd) / margin_frac
    cons["CASH_BUFFER"] = (st.cash_usd - pol.cash_buffer_frac * eq) / cash_frac
    for u in sorted({l["underlying"] for l in legs}):
        cur_u = sum(abs(p.notional_usd) for p in pos.values() if (p.underlying or p.key) == u)
        k = sum(1 for l in legs if l["underlying"] == u)
        cons[f"UNDERLYING:{u}"] = (pol.max_underlying_frac * eq - cur_u) / k
        cl = pol.correlation_groups.get(u, u)
        cur_c = sum(abs(p.notional_usd) for p in pos.values() if pol.correlation_groups.get(p.underlying or p.key, p.underlying or p.key) == cl)
        kc = sum(1 for l in legs if pol.correlation_groups.get(l["underlying"], l["underlying"]) == cl)
        cons[f"CLUSTER:{cl}"] = (pol.max_cluster_frac * eq - cur_c) / kc
    for q in sorted({l["quote"] for l in legs}):
        cur_q = sum(abs(p.notional_usd) for p in pos.values() if p.quote == q)
        kq = sum(1 for l in legs if l["quote"] == q)
        cons[f"STABLE:{q}"] = (pol.max_stable_frac * eq - cur_q) / kq
    for nf in sorted({l["nf"] for l in perp_legs}):
        cur_f = sum(abs(p.notional_usd) for p in pos.values() if p.category == "linear" and p.next_funding_ms in (nf, None))
        kf = sum(1 for l in perp_legs if l["nf"] == nf)
        cons[f"FUNDING_BUCKET:{nf}"] = (pol.max_funding_bucket_frac * eq - cur_f) / kf
    binding = min(sorted(cons), key=lambda k: (cons[k], k))
    notional = cons[binding]
    if not notional > 0:
        return skip([f"CONSTRAINT_EXHAUSTED:{binding}"], binding)

    # --- quantities: step-rounded, min/max enforced
    reasons = [f"BINDING:{binding}"]
    style_market = pol.style == "TAKER"
    qtys: Dict[str, float] = {}
    if o.kind == "SPOT_PERP_CARRY":
        steps = [l["inst"].qty_step for l in legs]
        q = _round_common_qty(notional / perp_legs[0]["ref"] if perp_legs else notional / legs[0]["ref"], steps)
        cap_q = min(float(l["inst"].max_mkt_order_qty if style_market and l["inst"].max_mkt_order_qty else l["inst"].max_order_qty) for l in legs)
        if q > cap_q:
            q = _round_common_qty(cap_q, steps)
            reasons.append("QTY_CLAMPED_MAX")
        for l in legs:
            qtys[l["key"]] = q
    else:
        n2 = notional
        for l in legs:
            ql = float(T.round_down_to_step(n2 / l["ref"], l["inst"].qty_step))
            capl = float(l["inst"].max_mkt_order_qty if style_market and l["inst"].max_mkt_order_qty else l["inst"].max_order_qty)
            if ql > capl:
                ql = float(T.round_down_to_step(capl, l["inst"].qty_step))
                reasons.append("QTY_CLAMPED_MAX")
            qtys[l["key"]] = ql
        n2 = min(qtys[l["key"]] * l["ref"] for l in legs)
        for l in legs:
            qtys[l["key"]] = min(qtys[l["key"]], float(T.round_down_to_step(n2 / l["ref"], l["inst"].qty_step)))
    for l in legs:
        q = qtys[l["key"]]
        if q <= 0 or q < float(l["inst"].min_order_qty):
            return skip([f"BELOW_MIN_QTY:{l['key']}", f"BINDING:{binding}"], binding)
        if q * l["ref"] < float(l["inst"].min_notional_usd):
            return skip([f"BELOW_MIN_NOTIONAL:{l['key']}", f"BINDING:{binding}"], binding)

    # --- build the plan
    gid = derive_group_id(cid, o.spec_digest, o.decision_ms, [l["key"] for l in legs])
    leg_plans: List[T.LegPlan] = []
    for l in legs:
        inst = l["inst"]
        cat = "spot" if l["spot"] else "linear"
        if pol.style == "TAKER":
            lp = T.LegPlan(derive_leg_id(gid, l["sym"], cat, l["side"], l["idx"]), l["sym"], cat, l["side"], _fmt(qtys[l["key"]]), "Market", None, False, False, "IOC")
        else:
            if l["spot"]:
                px = l["ref"]
            else:
                touch = l["snap"].asks[0].price if l["side"] == "Sell" else l["snap"].bids[0].price      # join the touch on our own side of the book
                px = touch
            lp = T.LegPlan(derive_leg_id(gid, l["sym"], cat, l["side"], l["idx"]), l["sym"], cat, l["side"], _fmt(qtys[l["key"]]), "Limit",
                           T.round_to_tick(px, inst.tick_size, side=l["side"], passive=True), True, False, "GTC")
        leg_plans.append(lp)
    gross = sum(qtys[l["key"]] * l["ref"] for l in legs)
    plan = T.OrderGroupPlan(gid, cid, o.spec_digest, pol.policy_version, decision_ms, tuple(leg_plans), pol.style, pol.max_wait_ms, gross)

    # --- the state this allocation would create (also what risk is asked about)
    new_pos = dict(pos)
    for l in legs:
        sgn_l = 1.0 if l["side"] == "Buy" else -1.0
        n_l = qtys[l["key"]] * l["ref"]
        new_pos[l["key"]] = Position(l["key"], "spot" if l["spot"] else "linear", sgn_l * n_l, l["nf"], l["underlying"], l["quote"])
    cap_req = sum((qtys[l["key"]] * l["ref"]) * (1.0 if l["spot"] else 1.0 / lev_by[l["key"]]) for l in legs)
    margin_add = sum(qtys[l["key"]] * l["ref"] / lev_by[l["key"]] for l in perp_legs)
    new_state = replace(st, positions=new_pos, open_groups=st.open_groups + 1, cash_usd=st.cash_usd - cap_req, margin_used_usd=st.margin_used_usd + margin_add)
    if risk_assess is not None:
        dec = risk_assess(plan, st.to_risk_state(), risk_policy if risk_policy is not None else {}, snapshots)
        if not getattr(dec, "approved", False):
            return skip(["RISK_REJECTED"] + [str(r) for r in getattr(dec, "reasons", ())], binding)
    liq = {k: 1.0 / v - pol.maint_margin_frac for k, v in lev_by.items()}
    detail = dict(cons)
    detail["CAPITAL_REQUIRED"] = cap_req
    for k, v in liq.items():
        detail[f"LIQ_DISTANCE:{k}"] = v
    held_keys.update(l["key"] for l in legs)
    return AllocDecision(cid, "ALLOCATE", tuple(reasons), gross, binding, gid, detail), plan, new_state
