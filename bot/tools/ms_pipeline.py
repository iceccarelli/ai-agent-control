"""ms_pipeline: the multi-symbol system as ONE working pipeline, REPLAY / PAPER ONLY. It owns the seams between the independently built modules and nothing else.

    run(series_or_history_root, *, spec, candidate_id, spec_digest, replay_cfg, artifacts_dir, risk_policy=None, mode="REPLAY", ...) -> PipelineResult

What runs, in the order it runs (every step is the REAL module; nothing is stubbed):
  history      ``candidate_readiness.load_history_root`` (verified datasets only) OR injected ``ReplaySeries`` / ``SymbolSeries`` (labelled ``INJECTED_TEST_DATA``, never MEASURED)
  scan         ``ms_scanner.scan`` behind an adapter: the instruments are viewed AT the decision time (status from the lifecycle bounds), the snapshots are the replay world's
               point-in-time snapshots, spec / candidate / digest are bound
  allocate     ``ms_portfolio.allocate`` (inside ``ms_replay.run_replay``) with ``ms_risk.risk_assess`` and a policy that satisfies ms_risk's CLOSED schema
  authority    ``ms_authority.MultiSymbolAuthority`` sits in the loop: the plan is registered (risk-assessed again on fresh state) BEFORE it is submitted and EVERY leg body that would
               be sent is passed through ``authorize(body, venue=...)``; a refusal means the order never reaches the venue. The authority is bound to an in-process SIMULATION transport
  execute      ``ms_execution.ExecutionEngine`` + ``SimVenue`` driven by ``ms_fillmodel`` (maker fills need trade-through; fees are charged; funding is booked at the print time by side)
  account      ``ms_accounting.Ledger`` behind ``LedgerAdapter`` (the replay's ledger contract: record_fill / record_funding / mark / pnl()['net'] / record_slippage)
  artifacts    the six control-centre JSON files in EXACTLY the control_centre schema, plus ``pipeline_summary.json`` (not read by the control centre) with the full report

Nothing real is ever sent: there is no client, no credential, no network and no wall clock in this module (``generated_ms`` and the PAPER clock are injected).

Honesty rules enforced here (each has a test and a mutation):
  * artifact bases: replay results SIMULATED; opportunities ESTIMATED unless EVERY item is derived from measured/stated inputs (then CALCULATED); the competition file SIMULATED when its
    numbers come from replay round trips, else ESTIMATED. An item's provenance is the WEAKEST of its inputs; injected data can never be MEASURED (capped at ASSUMED).
  * unknown stays unknown: a ledger component that is unknown makes the artifact's net ``null`` (the control centre shows MISSING), never zero.
  * the pipeline never reports SUFFICIENT_EVIDENCE: a replay is not evidence of execution.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import entry_authority as EA  # noqa: E402
import ms_accounting as MACC  # noqa: E402
import ms_authority as MAUTH  # noqa: E402
import ms_execution as EX  # noqa: E402
import ms_portfolio as PF  # noqa: E402
import ms_replay as RP  # noqa: E402
import ms_risk as MR  # noqa: E402
import ms_scanner as SC  # noqa: E402
import ms_types as T  # noqa: E402

SCHEMA = "ms_pipeline/v1"
DATA_INJECTED = "INJECTED_TEST_DATA"
DATA_HISTORY = "VERIFIED_HISTORY_ROOT"
MODES = ("REPLAY", "PAPER")
POLICY_VENUE = "testnet"                      # the policy's registered SANDBOX classification; this pipeline never connects anywhere
AUTHORITY_STRATEGY = "multi_symbol_funding_carry_replay"
ARTIFACT_FILES = ("replay_report.json", "portfolio_state.json", "risk_state.json", "ledger_pnl.json", "competition.json", "opportunities.json")
SUMMARY_FILE = "pipeline_summary.json"
MAX_ITEMS = 500
MIN_INDEPENDENT = RP.MIN_INDEPENDENT_FOR_ANY_CLAIM
DAY_MS = 86_400_000
_CC_BASIS_BY_PROV = {"MEASURED": "CALCULATED", "VENUE_STATED": "CALCULATED", "DERIVED": "CALCULATED", "MODELLED": "ESTIMATED", "ASSUMED": "ESTIMATED", "UNKNOWN": "ESTIMATED"}


class PipelineRefused(ValueError):
    """The pipeline refuses to run: a malformed spec / policy, an unverified history root, a venue that is not a sandbox, a missing injected clock. Nothing ran."""


# ======================================================================== ledger adapter (ms_replay's ledger contract over ms_accounting.Ledger)

class LedgerAdapter:
    """``ms_replay`` expects ``record_fill(fill, category=)`` / ``record_funding`` / ``mark(symbol, ts, price)`` (spot marks arrive as ``SPOT:<symbol>``) / ``pnl()['net']`` (a FLOAT) and an
    optional ``record_slippage``. ``ms_accounting.Ledger`` keys spot by category, has no slippage hook and returns ``net=None`` when any component is unknown. This adapter:
      * translates the ``SPOT:`` mark key into ``category='spot'`` and books every fill of a (group, symbol, category) position under ONE leg (exits have their own order leg ids);
      * declares ``declare_no_funding`` for a perp symbol at its FIRST fill: the replay books EVERY settlement print it crosses, so 'no record yet' is a stated fact (provenance DERIVED), not an
        unknown; a recorded print always takes precedence over the declaration;
      * returns ``net`` as a float for the replay's equity (the sum of the KNOWN components) together with ``net_complete`` / ``flags``. The artifacts write ``null`` whenever ``net_complete``
        is False: an unknown component is never presented as a number there;
      * states THE net definition (the same as ``ms_replay.LocalLedger`` and ``ms_replay.build_report``, which asserts it):
            price_leg = ms_accounting's price leg (actual fill prices) + taker slippage     (AT MID: the taker walk is inside the fill prices, it is added back here ...)
            net       = price_leg + funding - fees - taker slippage - adverse selection      (... and charged once as slippage; adverse selection is a post-fill markout that is in NO
                                                                                              fill price, so ms_accounting's own net omits it and it is deducted here, once)
        ``ledger`` (the raw ms_accounting dict) is passed through untouched: ITS net is not the displayed net.
    """

    def __init__(self, mark_prov: str = "MEASURED") -> None:
        if mark_prov not in T.PROVS:
            raise ValueError(f"unknown provenance {mark_prov!r}")
        self.ledger = MACC.Ledger()
        self.mark_prov = mark_prov
        self.slip = {"adverse": 0.0, "taker": 0.0}
        self.slip_by_symbol: Dict[str, Dict[str, float]] = {}
        self.marks: Dict[Tuple[str, str], float] = {}
        self._declared: set = set()

    def record_fill(self, fill: T.Fill, category: str = "linear") -> bool:
        # POSITION identity, not order identity: the engine's exit orders carry NEW leg ids (``x...``), ms_accounting keys a leg on (group, leg_id). Booked under the entry's own id an exit
        # would sit as a second OPEN leg, the pair would never be flat and nothing could ever reconcile. (group, symbol, category) names the position; the fill id stays unique.
        fill = dataclasses.replace(fill, leg_id=f"{category}:{fill.symbol}")
        ok = self.ledger.record_fill(fill, category=category)
        if category == "linear" and fill.symbol not in self._declared:
            self._declared.add(fill.symbol)
            self.ledger.declare_no_funding(fill.symbol, "NO_SETTLEMENT_PRINT_BOOKED_YET_BY_THE_REPLAY", "DERIVED")
        return ok

    def record_funding(self, symbol: str, ts_ms: int, amount_usd: Optional[float], prov: str) -> bool:
        return self.ledger.record_funding(symbol, int(ts_ms), amount_usd, prov)

    def record_slippage(self, usd: float, kind: str, symbol: Optional[str] = None) -> None:
        k = "adverse" if kind == "adverse" else "taker"
        self.slip[k] += float(usd)
        d = self.slip_by_symbol.setdefault(symbol or "", {"adverse": 0.0, "taker": 0.0})
        d[k] += float(usd)

    def mark(self, symbol: str, ts_ms: int, price: float) -> None:
        if symbol.startswith("SPOT:"):
            self.ledger.mark(symbol[5:], int(ts_ms), price, category="spot", prov=self.mark_prov)
            self.marks[(symbol[5:], "spot")] = float(price)
        else:
            self.ledger.mark(symbol, int(ts_ms), price, category="linear", prov=self.mark_prov)
            self.marks[(symbol, "linear")] = float(price)

    def symbol_pnl(self, symbol: str) -> Optional[Dict[str, Any]]:
        """One symbol's rollup on the SAME definition (gross at mid, minus fees, taker slippage and adverse selection); None when the symbol has no ledger leg."""
        r = self.ledger.pnl()["per_symbol"].get(symbol)
        if r is None:
            return None
        sl = self.slip_by_symbol.get(symbol, {"adverse": 0.0, "taker": 0.0})
        pl = None if r["price_leg"] is None else r["price_leg"] + sl["taker"]
        gross = None if pl is None or r["funding"] is None else pl + r["funding"]
        net = None if gross is None or r["fees"] is None else gross - r["fees"] - sl["taker"] - sl["adverse"]
        return {"price_leg": pl, "gross": gross, "fees": r["fees"], "taker_slippage": sl["taker"], "adverse_selection": sl["adverse"], "net": net}

    def pnl(self) -> Dict[str, Any]:
        p = self.ledger.pnl()
        known = lambda k: p[k] if p[k] is not None else 0.0                      # noqa: E731 - only for the replay's equity number; net_complete tells the truth
        taker, adverse = self.slip["taker"], self.slip["adverse"]
        price_actual = p["price_leg"] if p["price_leg"] is not None else (p["realised_price"] or 0.0)
        price_leg = price_actual + taker                                          # AT MID
        funding, fees = known("funding"), known("fees")
        net = price_leg + funding - fees - taker - adverse
        unreal = p["unrealised_price"]
        return {"price_leg": price_leg, "funding": funding, "fees": fees, "slippage": taker + adverse, "adverse_selection": adverse, "taker_slippage": taker,
                "realised": net - (unreal or 0.0), "unrealised": unreal, "net": net, "net_complete": p["net"] is not None, "flags": list(p["flags"]), "status": p["status"],
                "prov": p["prov"], "ledger": p}


# ======================================================================== risk policy (ms_risk's CLOSED schema, derived from the symbols actually in the series)

def derive_risk_policy(symbols: Sequence[str], *, candidate_id: str, spec_digest: str, portfolio: PF.PortfolioPolicy, equity_usd: float,
                       aggregate_notional_usd: Optional[float] = None, exec_policy: Optional[EX.ExecPolicy] = None, venue: str = POLICY_VENUE) -> Dict[str, Any]:
    """A conservative ms_risk policy: an EXPLICIT allow-list of the symbols in the series (never a wildcard), the portfolio policy's own limits with 5% headroom for the price drift between
    sizing (mark) and sending (touch), and a SMALL explicit aggregate ceiling (default half of equity: a hedged pair counts once). The venue names a registered SANDBOX; nothing connects."""
    if not symbols or len(set(symbols)) != len(symbols):
        raise PipelineRefused("RISK_POLICY_NEEDS_A_NON_EMPTY_UNIQUE_SYMBOL_LIST")
    eq = float(equity_usd)
    if not math.isfinite(eq) or eq <= 0:
        raise PipelineRefused("EQUITY_NOT_POSITIVE")
    agg = float(aggregate_notional_usd) if aggregate_notional_usd is not None else 0.5 * eq
    leg_cap = min(portfolio.max_symbol_notional_usd, portfolio.max_symbol_frac * eq, portfolio.max_symbol_leverage * eq, agg)
    ex = exec_policy or EX.ExecPolicy()
    head = 1.05
    return {"policy_version": portfolio.policy_version, "candidate_id": candidate_id, "spec_digest": spec_digest, "venue": venue, "allowed_symbols": sorted(symbols),
            "allowed_sides": {"linear": ["Buy", "Sell"], "spot": ["Buy", "Sell"]}, "allowed_order_types": ["Limit", "Market"],
            "max_notional_per_symbol_usd": 2.0 * leg_cap * head, "max_aggregate_notional_usd": agg, "max_gross_exposure_usd": 2.0 * agg * head,
            "max_net_exposure_usd": max(portfolio.max_net_frac * eq, 0.01 * eq) * head, "max_active_groups": int(portfolio.max_groups), "max_unhedged_usd": leg_cap * head,
            "max_leverage": max(1.0, portfolio.max_aggregate_leverage * head), "max_market_age_ms": int(min(portfolio.max_market_age_ms, ex.max_market_age_ms)),
            "max_account_age_ms": int(min(portfolio.max_account_age_ms, ex.max_account_age_ms)), "execution_styles": [portfolio.style]}


# ======================================================================== the authority in the replay loop

class SimulatedTransport:
    """The ONLY transport an authority is bound to here: an in-process simulation that carries no request. ``IN_PROCESS_SIMULATION`` is declared at CLASS level (ms_authority's test)."""
    IN_PROCESS_SIMULATION = True

    def request(self, *a: Any, **k: Any) -> Any:                            # pragma: no cover - never called: the replay's SimVenue is the order sink
        raise RuntimeError("the pipeline's simulated transport carries no request")


def body_from_request(req: EX.OrderRequest) -> Dict[str, Any]:
    """The venue-request body the leg WOULD be sent as (Bybit v5 field names), built only from the leg the engine journalled. The authority judges exactly this dict."""
    leg = req.leg
    b: Dict[str, Any] = {"category": leg.category, "symbol": leg.symbol, "side": leg.side, "orderType": leg.order_type, "qty": leg.qty, "orderLinkId": req.link_id}
    if leg.order_type == "Limit":
        b["price"] = leg.price
        b["timeInForce"] = "PostOnly" if leg.post_only else leg.time_in_force
    elif leg.time_in_force:
        b["timeInForce"] = leg.time_in_force
    if leg.reduce_only:
        b["reduceOnly"] = True
    return b


class AuthorityGates:
    """``ms_replay.run_replay(gates=...)``: registers every plan with the authority BEFORE it is submitted and authorises EVERY order the engine sends (entry, resend, exit). The authority
    reads the account state and market snapshots of the CURRENT replay step through the probe (never a stale copy), with the injected clock. A refusal is returned as a reason: the
    transport answers REJECTED and the order never reaches the venue."""

    def __init__(self, policy: Mapping[str, Any], *, state_dir: str, venue: EX.SimVenue, start_ms: int) -> None:
        self.policy = dict(policy)
        self.venue = venue
        self.start_ms = int(start_ms)
        self.probe: Optional[RP.WorldProbe] = None
        self._current_group: Optional[str] = None
        self._registered: set = set()
        self._settled: set = set()
        self._resolved: set = set()
        self._released: set = set()
        self.exit_releases: List[Tuple[str, str]] = []
        self.authorized: List[str] = []
        self.refusals: List[Tuple[str, Tuple[str, ...]]] = []
        self.plan_refusals: List[Tuple[str, Tuple[str, ...]]] = []
        self.transport = SimulatedTransport()
        try:
            self.authority = MAUTH.MultiSymbolAuthority(policy=self.policy, transport=self.transport, state_dir=state_dir, strategy=AUTHORITY_STRATEGY, state_provider=self._state,
                                                        snapshot_provider=self._snapshots, kill_switch_reader=lambda: (False, "replay: no kill switch engaged"), clock=self._clock,
                                                        hedge_reader=self._hedge)
        except MAUTH.AuthorityConfigError as exc:
            raise PipelineRefused(f"AUTHORITY_REFUSED_THE_POLICY: {exc}") from exc
        if self.authority.mode != "SIMULATED":                              # belt and braces: nothing but a simulation is ever wired here
            raise PipelineRefused("AUTHORITY_NOT_IN_SIMULATED_MODE")

    # -- providers (read the present replay step)
    def bind(self, probe: RP.WorldProbe) -> None:
        self.probe = probe

    def _now(self) -> int:
        return self.probe.now() if self.probe is not None else self.start_ms

    def _clock(self) -> float:
        return (self._now() + 0.5) / 1000.0                               # +0.5ms: int(seconds*1000) lands on the replay's millisecond, never one below it (a 'future' market stamp)

    def _state(self) -> Dict[str, Any]:
        if self.probe is None:
            raise RuntimeError("no probe bound")
        st = self.probe.risk_state()
        cur = self._current_group
        eng = self.probe.engine()
        g = eng.groups.get(cur) if cur else None
        if g is not None and g.state not in (T.GroupState.CLOSED, T.GroupState.FAILED) and isinstance(st.get("open_groups"), int):
            st["open_groups"] = max(0, st["open_groups"] - 1)           # the group being sent is not 'another' active group
        return st

    def _snapshots(self) -> Dict[str, T.MarketSnapshot]:
        if self.probe is None:
            raise RuntimeError("no probe bound")
        return self.probe.snapshots()

    def _hedge(self, symbol: str) -> Tuple[float, float]:
        pos = self.venue.positions()                                       # the venue's OWN position read, never fill arithmetic
        return max(0.0, -pos.get(f"linear:{symbol}", 0.0)), max(0.0, pos.get(f"spot:{symbol}", 0.0))

    # -- the gates ms_replay calls
    def on_plan(self, plan: T.OrderGroupPlan) -> Any:
        d = self.authority.register_group(plan)
        if d.approved:
            self._registered.add(plan.group_id)
        else:
            self.plan_refusals.append((plan.group_id, tuple(d.reasons)))
        return d

    def release_dead_exits(self, gid: str) -> None:
        """The authority reserves exit QUANTITY cumulatively per group / symbol / category (ms_authority ``release_exit``): an exit that was REJECTED, CANCELLED or expired unfilled gave
        nothing back until told. The engine retries exits under fresh link ids, so without this every retry past ~1.02x the entry quantity is refused and the position can never close.
        Called before each exit is authorised (the retry is sent in the same step as the rejection) and after every step. Only the UNFILLED remainder is released; the fill stays consumed."""
        if self.probe is None:
            return
        g = self.probe.engine().groups.get(gid)
        if g is None:
            return
        for l in g.exit_legs():
            if l.status in (EX.REJECTED, EX.CANCELLED) and l.link not in self._released:
                self._released.add(l.link)
                left = l.plan_qty - l.filled
                if left > 0 and self.authority.release_exit(l.link, format(left.normalize(), "f")):
                    self.exit_releases.append((l.link, format(left.normalize(), "f")))

    def on_order(self, req: EX.OrderRequest) -> Optional[str]:
        self._current_group = req.group_id
        if self.probe is not None and req.link_id.split("-", 1)[-1].startswith("x"):
            self.release_dead_exits(req.group_id)
        try:
            body = body_from_request(req)
            self.authority.authorize(body, venue=self.policy["venue"])
        except EA.EntryRefused as exc:
            self.refusals.append((req.link_id, tuple(exc.reasons)))
            if req.link_id.split("-", 1)[-1].startswith("x"):
                self.authority.release_exit(req.link_id)                    # an exit refused after its quantity was reserved gives it back (False when nothing was reserved)
            return ",".join(exc.reasons)[:300]
        except Exception as exc:                                           # noqa: BLE001 - an authority that cannot decide refuses
            self.refusals.append((req.link_id, (f"AUTHORITY_ERROR:{type(exc).__name__}",)))
            return f"AUTHORITY_ERROR:{type(exc).__name__}"
        finally:
            self._current_group = None
        self.authorized.append(req.link_id)
        return None

    def on_step(self, now: int) -> None:
        """Tell the authority what the account now shows: a FILLED entry leg stops being a pending reservation; a CLOSED / FAILED group stops counting as active."""
        if self.probe is None:
            return
        eng = self.probe.engine()
        for gid in sorted(self._registered):
            g = eng.groups.get(gid)
            if g is None:
                continue
            for l in g.entry_legs():
                if l.status == EX.FILLED and (gid, l.plan.leg_id) not in self._settled:
                    self._settled.add((gid, l.plan.leg_id))
                    self.authority.settle_leg(gid, l.plan.leg_id)
            self.release_dead_exits(gid)
            if g.state in (T.GroupState.CLOSED, T.GroupState.FAILED) and gid not in self._resolved:
                self._resolved.add(gid)
                self.authority.resolve_group(gid)

    def stats(self) -> Dict[str, Any]:
        refused: Dict[str, int] = {}
        for _link, reasons in self.refusals:
            for r in reasons:
                k = r.split(":", 1)[0]
                refused[k] = refused.get(k, 0) + 1
        plan_refused: Dict[str, int] = {}
        for _gid, reasons in self.plan_refusals:
            for r in reasons:
                k = r.split(":", 1)[0]
                plan_refused[k] = plan_refused.get(k, 0) + 1
        return {"mode": self.authority.mode, "policy_digest": self.authority.policy_digest, "venue": self.policy["venue"], "orders_authorised": len(self.authorized),
                "orders_refused": len(self.refusals), "exit_reservations_released": len(self.exit_releases), "order_refusal_codes": dict(sorted(refused.items())), "groups_registered": len(self._registered),
                "groups_refused_at_registration": len(self.plan_refusals), "registration_refusal_codes": dict(sorted(plan_refused.items())), "transport": "IN_PROCESS_SIMULATION"}


# ======================================================================== scanner adapter

class ScanRecorder:
    """The scan_fn handed to the replay: ms_scanner.scan with the spec / candidate / digest bound, instruments viewed at the decision time. Records every scan (for opportunities.json and
    the competition status) and which opportunity produced which group (the group id is derived exactly as ms_portfolio derives it)."""

    def __init__(self, spec: Mapping[str, Any], candidate_id: str, spec_digest: str, params: Optional[Mapping[str, Any]]) -> None:
        self.spec, self.candidate_id, self.spec_digest, self.params = dict(spec), candidate_id, spec_digest, dict(params or {})
        self.scans: List[Tuple[int, List[T.Opportunity]]] = []
        self.by_group: Dict[str, T.Opportunity] = {}

    @staticmethod
    def instrument_at(inst: T.Instrument, t_ms: int) -> T.Instrument:
        """Status AT t from the lifecycle bounds (ms_types: 'for history use lifecycle bounds'): a contract delivered by t is Closed, one not yet launched is PreLaunch; an explicitly
        non-Trading recorded status with no bound stays as recorded. The scanner then refuses to open anything that is not Trading."""
        if inst.delivery_ms is not None and t_ms >= inst.delivery_ms:
            return dataclasses.replace(inst, status="Closed")
        if inst.launch_ms is not None and t_ms < inst.launch_ms:
            return dataclasses.replace(inst, status="PreLaunch")
        return inst

    def __call__(self, now: int, snaps: Mapping[str, T.MarketSnapshot], instruments: Mapping[str, T.Instrument]) -> List[T.Opportunity]:
        insts = {s: self.instrument_at(i, now) for s, i in instruments.items()}
        opps = SC.scan(insts, snaps, spec=self.spec, candidate_id=self.candidate_id, spec_digest=self.spec_digest, decision_ms=now, params=self.params)
        self.scans.append((now, list(opps)))
        for o in opps:
            if o.legs and not o.exclusions:
                gid = PF.derive_group_id(o.candidate_id, o.spec_digest, o.decision_ms, [s for s, _ in o.legs])
                self.by_group[gid] = o
        return opps


# ======================================================================== loading

def _hedge_of(spot_listed: Optional[bool]) -> str:
    return {True: "HEDGEABLE_ON_BYBIT", False: "PERP_ONLY"}.get(spot_listed, "HEDGE_UNKNOWN")


def _instrument_for(x: RP.ReplaySeries, cfg: RP.ReplayConfig) -> T.Instrument:
    if x.instrument is not None:
        inst = x.instrument
        if inst.status == "Closed" and inst.delivery_ms is not None:         # a contract that is Closed NOW traded before its delivery: the lifecycle bound carries the closure
            inst = dataclasses.replace(inst, status="Trading")
        return inst
    return T.Instrument(x.symbol, None, "Trading", x.launch_ms, x.delivery_ms, x.interval_min, x.base_coin or x.symbol, "USDT", cfg.default_tick, cfg.default_qty_step,
                        cfg.default_min_qty, cfg.default_max_qty, cfg.default_max_qty, cfg.default_min_notional, "10", x.symbol, _hedge_of(x.spot_listed))


def load_series(source: Any, *, universe_dir: Optional[str], spot_dir: Optional[str], mark_interval: Optional[str]) -> Tuple[List[Any], str, Dict[str, Any]]:
    """(series, data_label, info). A string is a market_history root and goes through the VERIFIED loader; anything else is a sequence of injected series (INJECTED_TEST_DATA)."""
    if isinstance(source, str):
        if not universe_dir:
            raise PipelineRefused("UNIVERSE_DIR_REQUIRED_FOR_A_HISTORY_ROOT")
        import candidate_readiness as CR                                  # lazy: heavy, and only the verified path needs it
        import research_harness as RH
        try:
            series, info = CR.load_history_root(source, universe_dir=universe_dir, spot_dir=spot_dir, mark_interval=mark_interval)
        except RH.HarnessError as exc:
            raise PipelineRefused(f"HISTORY_REFUSED: {exc}") from exc
        if not series:
            raise PipelineRefused("HISTORY_ROOT_HAS_NO_USABLE_SYMBOL")
        return list(series), DATA_HISTORY, dict(info)
    series = list(source)
    if not series:
        raise PipelineRefused("NO_SERIES")
    return series, DATA_INJECTED, {"survivorship": "SURVIVORS_ONLY", "excluded": []}


def _stale_bound(orig: Any, r: RP.ReplaySeries) -> Optional[int]:
    iv = getattr(orig, "mark_interval_ms", None)
    if not (isinstance(iv, int) and iv > 0):
        gaps = sorted(b[0] - a[0] for a, b in zip(r.mark, r.mark[1:]) if b[0] > a[0])
        iv = gaps[len(gaps) // 2] if gaps else None                        # the median spacing: one missing print does not move it
    return 2 * iv if iv else None


def _to_replay_series(series: Sequence[Any], cfg: RP.ReplayConfig) -> List[RP.ReplaySeries]:
    out: List[RP.ReplaySeries] = []
    seen = set()
    for x in series:
        r = x if isinstance(x, RP.ReplaySeries) else RP.from_symbol_series(x)
        if r.symbol in seen:
            raise PipelineRefused(f"DUPLICATE_SYMBOL:{r.symbol}")
        seen.add(r.symbol)
        if r.mark_stale_ms is None:                                         # a mark older than two mark intervals is UNKNOWN at the decision (the loader's own staleness rule)
            r = dataclasses.replace(r, mark_stale_ms=_stale_bound(x, r))
        out.append(dataclasses.replace(r, instrument=_instrument_for(r, cfg)))
    return sorted(out, key=lambda s: s.symbol)


# ======================================================================== artifacts

def _num(x: Any) -> Optional[float]:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _sum(*xs: Optional[float]) -> Optional[float]:
    return None if any(v is None for v in xs) else float(sum(xs))                # type: ignore[arg-type]


def _write_json(path: str, obj: Mapping[str, Any]) -> None:
    raw = json.dumps(obj, sort_keys=True, indent=1, allow_nan=False, ensure_ascii=True) + "\n"
    data = raw.encode("utf-8")
    if len(data) > 1_048_576:
        raise PipelineRefused(f"ARTIFACT_TOO_LARGE:{os.path.basename(path)}")
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _short(s: Any, n: int = 300) -> str:
    return "".join(c if 32 <= ord(c) < 127 else "?" for c in str(s))[:n]


def _hedge_label(hedges: Sequence[str]) -> str:
    hs = set(hedges)
    if hs and hs == {"HEDGEABLE_ON_BYBIT"}:
        return "HEDGEABLE_ON_BYBIT"
    if "PERP_ONLY" in hs:
        return "PERP_ONLY"
    return "HEDGE_UNKNOWN"


def _prov_cap(prov: str, label: str) -> str:
    """Injected data is never MEASURED: the weakest of the item's provenance and ASSUMED."""
    return T.weakest(prov, "ASSUMED") if label == DATA_INJECTED else prov


def _basis_from_provs(provs: Sequence[str]) -> str:
    return _CC_BASIS_BY_PROV[T.weakest(*provs)] if provs else "ESTIMATED"


@dataclass
class PipelineResult:
    mode: str
    data_label: str
    result: RP.ReplayResult
    report: Dict[str, Any]
    summary: Dict[str, Any]
    files: Dict[str, str]
    ledger: LedgerAdapter
    recorder: ScanRecorder
    gates: AuthorityGates
    policy: Dict[str, Any]
    artifacts: Dict[str, Dict[str, Any]] = field(default_factory=dict)


def _group_kinds(res: RP.ReplayResult, rec: ScanRecorder) -> Dict[str, str]:
    return {gid: rec.by_group[gid].kind for gid in res.groups if gid in rec.by_group}


def _build_artifacts(res: RP.ReplayResult, led: LedgerAdapter, rec: ScanRecorder, series: Sequence[RP.ReplaySeries], policy: Mapping[str, Any], gates: AuthorityGates,
                     report: Mapping[str, Any], *, label: str, generated_ms: int, spec: Mapping[str, Any], candidate_id: str, universe_scope: str) -> Dict[str, Dict[str, Any]]:
    cfg = res.cfg
    p = led.pnl()
    L = p["ledger"]
    complete = bool(p["net_complete"])
    hedge_of = {s.symbol: (s.instrument.spot_hedge if s.instrument else "HEDGE_UNKNOWN") for s in series}
    kinds = _group_kinds(res, rec)
    kind_default = str(spec.get("kind", "SPOT_PERP_CARRY"))
    common = {"generated_ms": int(generated_ms), "data_label": label}

    # ---- replay_report
    gross = p["price_leg"] + p["funding"] if complete else None             # AT MID: net = gross - fees - slippage (taker walk + adverse selection)
    fees = L["fees"]
    net = p["net"] if complete else None
    sym_rows = []
    sym_groups: Dict[str, List[str]] = {}
    for gid, g in sorted(res.groups.items()):
        for s in g.symbols:
            sym_groups.setdefault(s, []).append(gid)
    for sym in sorted(sym_groups):
        r = led.symbol_pnl(sym)
        kind = kinds.get(sym_groups[sym][0], kind_default)
        sym_rows.append({"symbol": sym, "kind": kind, "spot_hedge": hedge_of.get(sym, "HEDGE_UNKNOWN"), "n_groups": len(sym_groups[sym]),
                         "gross_pnl_usd": r["gross"] if r else 0.0, "fees_usd": r["fees"] if r else 0.0, "net_pnl_usd": r["net"] if r else 0.0})     # no ledger leg = no fill: a stated zero
    states: Dict[str, int] = {}
    for st in res.states.values():
        k = st.value if hasattr(st, "value") else str(st)
        states[k] = states.get(k, 0) + 1
    replay_report = {"schema": "replay_report/v1", "basis": "SIMULATED", **common, "universe_scope": universe_scope, "period_start_ms": cfg.start_ms, "period_end_ms": cfg.end_ms,
                     "n_groups": len(res.states), "groups_by_state": dict(sorted(states.items())), "gross_pnl_usd": gross, "fees_usd": fees, "net_pnl_usd": net, "symbols": sym_rows,
                     "slippage_usd": p["slippage"] if complete else None, "taker_slippage_usd": p["taker_slippage"] if complete else None,
                     "adverse_selection_usd": p["adverse_selection"] if complete else None,
                     "net_definition": "net = gross (price leg at mid + funding) - fees - slippage (taker walk + adverse selection), each modelled cost once"}

    # ---- ledger_pnl (realised / unrealised split; a simulated ledger is never realised cash)
    rg = (p["realised"] + p["fees"] + p["slippage"]) if complete else None     # realised price leg AT MID + funding
    ledger_pnl = {"schema": "ledger_pnl/v1", "basis": "SIMULATED", **common, "period_start_ms": cfg.start_ms, "period_end_ms": cfg.end_ms, "realised_gross_usd": rg,
                  "fees_usd": fees, "funding_usd": L["funding"], "slippage_usd": p["slippage"] if complete else None, "realised_net_usd": p["realised"] if complete else None, "unrealised_usd": L["unrealised_price"],
                  "net_definition": "realised_net + unrealised = net = gross (price leg at mid + funding) - fees - slippage (taker walk + adverse selection); ms_accounting's own net omits adverse selection",
                  "ledger_status": L["status"], "ledger_flags": [_short(f, 120) for f in L["flags"][:20]]}

    # ---- portfolio_state (end of window)
    legs: Dict[Tuple[str, str, str], Dict[str, float]] = {}
    for f in res.fills:
        cat = res.fills_cat.get(f.fill_id, "linear")
        d = legs.setdefault((f.group_id, f.symbol, cat), {"qty": 0.0, "in_qty": 0.0, "in_cost": 0.0, "first_sign": 0.0})
        sgn = 1.0 if f.side == "Buy" else -1.0
        if d["first_sign"] == 0.0:
            d["first_sign"] = sgn
        d["qty"] += sgn * f.qty
        if sgn == d["first_sign"]:
            d["in_qty"] += f.qty
            d["in_cost"] += f.qty * f.price
    positions = []
    gross_exp = net_exp = 0.0
    unreal = 0.0
    unreal_known = True
    for (gid, sym, cat), d in sorted(legs.items()):
        if abs(d["qty"]) < 1e-9:
            continue
        mark = led.marks.get((sym, cat))
        entry = d["in_cost"] / d["in_qty"] if d["in_qty"] > 0 else None
        if mark is None or entry is None:
            unreal_known = False
        notional = abs(d["qty"]) * (mark if mark is not None else (entry or 0.0))
        gross_exp += notional
        net_exp += math.copysign(notional, d["qty"])
        u = (mark - entry) * d["qty"] if (mark is not None and entry is not None) else None
        unreal += u or 0.0
        positions.append({"symbol": sym if cat == "linear" else sym, "kind": kinds.get(gid, kind_default), "spot_hedge": hedge_of.get(sym, "HEDGE_UNKNOWN"),
                          "side": "Buy" if d["qty"] > 0 else "Sell", "notional_usd": notional, "entry_price": entry, "mark_price": mark, "unrealised_pnl_usd": u, "group_id": gid,
                          "category": cat})
    for row in positions:
        row.pop("category")
    equity = (cfg.equity0 + net) if (net is not None and complete) else None
    portfolio = {"schema": "portfolio_state/v1", "basis": "SIMULATED", **common, "equity_usd": equity, "gross_exposure_usd": gross_exp, "net_exposure_usd": net_exp,
                 "unrealised_pnl_usd": unreal if unreal_known else None, "positions": positions}

    # ---- risk_state
    eng = res.engine
    halts = sorted(eng.halts) if eng is not None else []
    st = res.stats
    agg = 0.0
    per_base: Dict[str, Dict[str, float]] = {}
    for (gid, sym, cat), d in legs.items():
        if abs(d["qty"]) < 1e-9:
            continue
        mark = led.marks.get((sym, cat))
        if mark is not None:
            per_base.setdefault(sym, {}).update({cat: abs(d["qty"]) * mark})
    agg_known = all(((sym, cat) in led.marks) for (gid, sym, cat), d in legs.items() if abs(d["qty"]) >= 1e-9)
    for b, v in per_base.items():
        agg += max(v.values())
    peak_gross = _num(st.get("max_gross_usd"))
    obs_dd = _num(report["risk"]["max_drawdown_frac"]) if isinstance(report.get("risk"), Mapping) else None

    def lim(name: str, unit: str, limit: Optional[float], value: Optional[float]) -> Dict[str, Any]:
        status = "UNKNOWN" if (limit is None or value is None) else ("BREACH" if value > limit + 1e-9 else "OK")
        return {"name": name, "unit": unit, "limit": limit, "value": value, "status": status}
    limits = [lim("peak_gross_exposure", "USD", _num(policy["max_gross_exposure_usd"]), peak_gross),
              lim("aggregate_notional_end", "USD", _num(policy["max_aggregate_notional_usd"]), agg if agg_known else None),
              lim("net_exposure_end", "USD", _num(policy["max_net_exposure_usd"]), abs(net_exp) if unreal_known else None),
              lim("peak_open_groups", "groups", float(policy["max_active_groups"]), _num(st.get("max_open_groups"))),
              lim("max_drawdown", "fraction", _num(cfg.portfolio.max_drawdown_frac), obs_dd)]
    alerts: List[Dict[str, str]] = []
    for f in sorted({(x["kind"], x.get("symbol", "")) for x in res.fault_log}):
        alerts.append({"severity": "WARN", "code": _short("FAULT_INJECTED:" + f[0], 80), "detail": _short(f"symbol={f[1] or '-'}", 120)})
    n_bad = sum(1 for s in res.states.values() if (s.value if hasattr(s, "value") else s) in ("UNHEDGED", "UNKNOWN_OUTCOME"))
    if n_bad:
        alerts.append({"severity": "WARN", "code": "GROUPS_UNHEDGED_OR_UNKNOWN", "detail": f"{n_bad} group(s) in UNHEDGED or UNKNOWN_OUTCOME at the end of the window"})
    ast = gates.stats()
    if ast["orders_refused"] or ast["groups_refused_at_registration"]:
        alerts.append({"severity": "WARN", "code": "AUTHORITY_REFUSALS", "detail": _short(f"orders={ast['order_refusal_codes']} groups={ast['registration_refusal_codes']}", 300)})
    if not complete:
        alerts.append({"severity": "WARN", "code": "LEDGER_INCOMPLETE", "detail": "a ledger component is unknown: net is not stated"})
    risk_state = {"schema": "risk_state/v1", "basis": "SIMULATED", **common, "halted": bool(halts), "halt_reasons": [_short(h, 200) for h in halts], "limits": limits, "alerts": alerts}

    # ---- opportunities (last scan)
    scans = rec.scans
    last_ms, last = scans[-1] if scans else (cfg.start_ms, [])
    items = []
    for o in last[:MAX_ITEMS]:
        syms = [s[5:] if s.startswith("SPOT:") else s for s, _ in o.legs]
        syms = list(dict.fromkeys(syms))
        gross_bps = o.expected_funding_bps_horizon
        if gross_bps is not None and o.expected_price_leg_bps is not None:
            gross_bps = gross_bps + o.expected_price_leg_bps
        items.append({"candidate_id": o.candidate_id, "kind": o.kind, "symbols": syms, "spot_hedge": _hedge_label([hedge_of.get(s, "HEDGE_UNKNOWN") for s in syms]),
                      "hold_days": float(o.hold_days), "gross_bps_horizon": _num(gross_bps), "net_bps_horizon": _num(o.net_bps_horizon), "capacity_usd": _num(o.capacity_usd),
                      "prov": _prov_cap(o.prov, label), "exclusions": [_short(e, 80) for e in o.exclusions]})
    elig = [o for o in last if not o.exclusions and o.net_bps_horizon is not None and o.net_bps_horizon > 0 and o.rank_score is not None]
    est_day = None
    if elig:
        best = max(elig, key=lambda o: (o.rank_score, o.legs))
        notional = min(float(rec.params.get("notional_usd", SC.DEFAULT_PARAMS["notional_usd"])), best.capacity_usd or 0.0)
        est_day = best.net_bps_horizon / 1e4 * notional / best.hold_days if best.hold_days > 0 else None
    opportunities = {"schema": "opportunities/v1", "basis": _basis_from_provs([i["prov"] for i in items]), **common, "decision_ms": int(last_ms), "est_net_usd_per_day": est_day,
                     "items": items, "n_items_total": len(last), "n_items_shown": len(items)}

    # ---- competition (one candidate; never SUFFICIENT_EVIDENCE)
    n_pos_any = sum(1 for _ms, ops in scans for o in ops if not o.exclusions and o.net_bps_horizon is not None and o.net_bps_horizon > 0)
    closed = [g for g in res.groups.values() if g.closed and g.notional_usd > 0]
    nets = [(g.price_usd + g.funding_usd - g.fees_usd) / g.notional_usd * 1e4 for g in closed]
    grosses = [(g.price_usd + g.funding_usd) / g.notional_usd * 1e4 for g in closed]
    traded = sorted({s for g in res.groups.values() for s in g.symbols})
    indep = int(report["observations"]["independent"]) if isinstance(report.get("observations"), Mapping) else 0
    rprov = _prov_cap(str(report["provenance"]["weakest"]) if isinstance(report.get("provenance"), Mapping) else "ASSUMED", label)
    if not scans:
        status, reason = "RESEARCHING", None
    elif n_pos_any == 0:
        status, reason = "REJECTED", f"NO_EDGE_IN_WINDOW: none of {len(scans)} scan(s) had an eligible opportunity with positive net after costs (this window only; not a general rejection)"
    elif indep < MIN_INDEPENDENT:
        status, reason = "INSUFFICIENT_EVIDENCE", f"{indep} independent closed round trip(s) (< {MIN_INDEPENDENT}); a replay with MODELLED fills is not execution evidence"
    else:
        status, reason = "INSUFFICIENT_EVIDENCE", "replay fills are MODELLED: not execution evidence (promotion_eligible is False)"
    comp_kind = kinds.get(next((g for g in sorted(res.groups) if g in kinds), ""), kind_default)
    cand = {"candidate_id": candidate_id, "kind": comp_kind, "spot_hedge": _hedge_label([hedge_of.get(s, "HEDGE_UNKNOWN") for s in traded]) if traded else "HEDGE_UNKNOWN",
            "status": status, "reason": reason, "gross_bps_horizon": (sum(grosses) / len(grosses)) if grosses else None, "net_bps_horizon": (sum(nets) / len(nets)) if nets else None,
            "n_observations": len(closed), "prov": rprov}
    competition = {"schema": "competition/v1", "basis": "SIMULATED" if closed else "ESTIMATED", **common, "candidates": [cand]}
    return {"replay_report.json": replay_report, "portfolio_state.json": portfolio, "risk_state.json": risk_state, "ledger_pnl.json": ledger_pnl, "competition.json": competition,
            "opportunities.json": opportunities}


# ======================================================================== the run

def run(source: Any, *, universe_dir: Optional[str] = None, spot_dir: Optional[str] = None, mark_interval: Optional[str] = None, spec: Mapping[str, Any], candidate_id: str,
        spec_digest: str, risk_policy: Optional[Mapping[str, Any]] = None, replay_cfg: RP.ReplayConfig, artifacts_dir: str, mode: str = "REPLAY",
        scan_params: Optional[Mapping[str, Any]] = None, generated_ms: Optional[int] = None, clock: Optional[Callable[[], int]] = None, state_dir: Optional[str] = None,
        aggregate_notional_usd: Optional[float] = None, venue: Optional[EX.SimVenue] = None, store: Optional[Any] = None) -> PipelineResult:
    """Run the whole stack on SIMULATED transport and write the control-centre artifacts. ``source`` is a history-root path (verified loader) or a list of injected series.
    ``generated_ms`` (default: the end of the window) and the PAPER ``clock`` are injected: the same inputs give the same artifact bytes."""
    if mode not in MODES:
        raise PipelineRefused(f"MODE_MUST_BE_ONE_OF:{MODES}")
    if mode == "PAPER" and clock is None:
        raise PipelineRefused("PAPER_MODE_NEEDS_AN_INJECTED_CLOCK")
    if mode == "REPLAY" and clock is not None:
        raise PipelineRefused("REPLAY_MODE_USES_THE_GRID_NOT_A_CLOCK")
    if not isinstance(spec, Mapping) or spec.get("kind", "SPOT_PERP_CARRY") not in SC.KINDS or spec.get("style", "TAKER") not in SC.STYLES:
        raise PipelineRefused("SPEC_KIND_OR_STYLE_INVALID")
    if spec.get("style", "TAKER") != replay_cfg.portfolio.style:
        raise PipelineRefused(f"SPEC_STYLE_{spec.get('style', 'TAKER')}_DOES_NOT_MATCH_PORTFOLIO_STYLE_{replay_cfg.portfolio.style}: the scanner's costs would not be the plan's")
    if not (isinstance(candidate_id, str) and candidate_id.strip() and isinstance(spec_digest, str) and spec_digest.strip()):
        raise PipelineRefused("CANDIDATE_ID_AND_SPEC_DIGEST_REQUIRED")
    if replay_cfg.spec_digest != spec_digest:
        raise PipelineRefused("REPLAY_CFG_SPEC_DIGEST_MISMATCH")
    if not isinstance(artifacts_dir, str) or not artifacts_dir:
        raise PipelineRefused("ARTIFACTS_DIR_REQUIRED")

    raw_series, label, info = load_series(source, universe_dir=universe_dir, spot_dir=spot_dir, mark_interval=mark_interval)
    series = _to_replay_series(raw_series, replay_cfg)
    symbols = [s.symbol for s in series]
    scope = str(info.get("survivorship", "SURVIVORS_ONLY"))
    universe_scope = scope if scope in ("SURVIVORS_ONLY", "DELISTINGS_OBSERVED") else "SURVIVORS_ONLY"

    policy = dict(risk_policy) if risk_policy is not None else derive_risk_policy(symbols, candidate_id=candidate_id, spec_digest=spec_digest, portfolio=replay_cfg.portfolio,
                                                                                    equity_usd=replay_cfg.equity0, aggregate_notional_usd=aggregate_notional_usd,
                                                                                    exec_policy=replay_cfg.exec_policy)
    bad = MR.policy_refusals(policy)
    if bad:
        raise PipelineRefused("RISK_POLICY_REFUSED:" + ",".join(bad))
    missing_syms = sorted(set(symbols) - set(policy["allowed_symbols"]))
    if missing_syms:
        raise PipelineRefused("RISK_POLICY_ALLOW_LIST_LACKS:" + ",".join(missing_syms))

    os.makedirs(artifacts_dir, exist_ok=True)
    auth_dir = state_dir or os.path.join(artifacts_dir, "_ms_authority_state")
    if mode == "REPLAY":                                                   # a replay is a fresh world: its authority ledgers start empty (PAPER keeps them)
        for name in ("ms_groups.jsonl", "ms_reservations.jsonl"):
            try:
                os.unlink(os.path.join(auth_dir, name))
            except FileNotFoundError:
                pass
    sim_venue = venue or EX.SimVenue()
    gates = AuthorityGates(policy, state_dir=auth_dir, venue=sim_venue, start_ms=replay_cfg.start_ms)
    params = dict(scan_params or {})
    params.setdefault("hold_days", replay_cfg.hold_ms / DAY_MS)
    recorder = ScanRecorder(spec, candidate_id, spec_digest, params)
    holder: List[LedgerAdapter] = []
    mark_prov = "ASSUMED" if label == DATA_INJECTED else "MEASURED"

    def ledger_factory() -> LedgerAdapter:
        holder.append(LedgerAdapter(mark_prov))
        return holder[-1]

    def risk_assess(plan: T.OrderGroupPlan, state: Mapping[str, Any], pol: Mapping[str, Any], snaps: Mapping[str, T.MarketSnapshot]) -> Any:
        return MR.risk_assess(plan, state, pol, snaps)                     # looked up at call time

    res = RP.run_replay(series, replay_cfg, scan_fn=recorder, risk_assess=risk_assess, risk_policy=policy, ledger_factory=ledger_factory, store=store, venue=sim_venue,
                        clock=clock, gates=gates)
    led = holder[0]
    report = RP.build_report([res])
    if label == DATA_INJECTED:
        report["provenance"]["weakest"] = T.weakest(str(report["provenance"]["weakest"]), "ASSUMED")
    gen = int(generated_ms if generated_ms is not None else replay_cfg.end_ms)
    arts = _build_artifacts(res, led, recorder, series, policy, gates, report, label=label, generated_ms=gen, spec=spec, candidate_id=candidate_id, universe_scope=universe_scope)
    files: Dict[str, str] = {}
    for name in ARTIFACT_FILES:
        files[name] = os.path.join(artifacts_dir, name)
        _write_json(files[name], arts[name])
    reasons: Dict[str, int] = {}
    for d in res.decisions:
        for r in d["reasons"][:2]:
            k = str(r).split(":", 1)[0]
            reasons[k] = reasons.get(k, 0) + 1
    assumed = sorted(set(res.assumed) | ({"INSTRUMENT_FILTERS_DEFAULTED"} if label == DATA_HISTORY else set()))
    summary = {"schema": SCHEMA, "mode": mode, "data_label": label, "generated_ms": gen, "symbols": symbols, "spec": dict(spec), "candidate_id": candidate_id, "spec_digest": spec_digest,
               "risk_policy": policy, "authority": gates.stats(), "decision_reason_counts": dict(sorted(reasons.items())), "assumed_inputs": assumed,
               "ledger": {"net": led.pnl()["net"], "net_complete": led.pnl()["net_complete"], "flags": led.pnl()["flags"][:20], "status": led.pnl()["status"]},
               "journal_head": res.journal_head, "promotion_eligible": False, "report": report,
               "note": "REPLAY/PAPER on a simulated transport. ONE net everywhere (artifacts, equity curve, observations, this summary): net = gross (price leg AT MID + funding) - fees - "
                       "taker slippage - adverse selection. The taker walk is inside the fill prices (added back to reach mid, then charged once); adverse selection is a modelled "
                       "post-fill markout that is in no fill price, so ms_accounting's own net (the 'ledger' dict) omits it and the pipeline deducts it once"}
    files[SUMMARY_FILE] = os.path.join(artifacts_dir, SUMMARY_FILE)
    _write_json(files[SUMMARY_FILE], summary)
    return PipelineResult(mode, label, res, report, summary, files, led, recorder, gates, policy, arts)


def file_digests(artifacts_dir: str) -> Dict[str, str]:
    """sha256 of the six artifacts (for the determinism check)."""
    out = {}
    for name in ARTIFACT_FILES:
        with open(os.path.join(artifacts_dir, name), "rb") as fh:
            out[name] = hashlib.sha256(fh.read()).hexdigest()
    return out
