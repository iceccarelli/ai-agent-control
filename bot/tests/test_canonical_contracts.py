"""Cross-module CONTRACT tests: the REAL modules driven together (ms_portfolio.allocate -> ms_risk -> ms_authority -> ms_execution.ExecutionEngine + SimVenue -> ms_accounting.Ledger ->
ms_reconcile) must agree on positions and on net P&L at every stage, a rejected / failed group must leave no reservation, and an injected divergence must HALT new exposure and name the
disagreement. Offline: the venue is the in-process SimVenue, the authority's transport is the pipeline's in-process simulation, the state dir is a tmp dir."""
from __future__ import annotations

import os
import sys
from decimal import Decimal as D

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (BOT, os.path.join(BOT, "tools")):
    sys.path.insert(0, _p)

import entry_authority as EA  # noqa: E402
import ms_accounting as ACC  # noqa: E402
import ms_authority as MA  # noqa: E402
import ms_execution as EX  # noqa: E402
import ms_pipeline as PIPE  # noqa: E402
import ms_portfolio as PF  # noqa: E402
import ms_reconcile as RC  # noqa: E402
import ms_risk as RK  # noqa: E402
import ms_types as T  # noqa: E402
from ms_types import GroupState as G  # noqa: E402

NOW = 1_800_000_000_000
NF = NOW + 3_600_000
SYM = "AAAUSDT"
MARK = 100.0
BID, ASK = 99.95, 100.07


def inst(sym=SYM):
    return T.Instrument(symbol=sym, symbol_id=None, status="Trading", launch_ms=0, delivery_ms=None, funding_interval_min=480, base_coin=sym[:-4], tick_size="0.1", qty_step="0.1",
                        min_order_qty="0.1", max_order_qty="10000", max_mkt_order_qty="5000", min_notional_usd="5", max_leverage="10", spot_symbol=sym, spot_hedge="HEDGEABLE_ON_BYBIT")


def snap(sym=SYM):
    return T.MarketSnapshot(symbol=sym, decision_ms=NOW, mark=MARK, index=MARK, bids=(T.BookLevel(BID, 1000.0),), asks=(T.BookLevel(ASK, 1000.0),), book_age_ms=100,
                            funding_interval_min=480, next_funding_ms=NF, spot_mark=MARK)


def opp(cid="c1", sym=SYM, cap=2000.0):
    return T.Opportunity(candidate_id=cid, spec_digest="spec1", kind="SPOT_PERP_CARRY", decision_ms=NOW, legs=((sym, "Sell"), ("SPOT:" + sym, "Buy")), hold_days=3.0,
                         expected_funding_bps_horizon=30.0, expected_price_leg_bps=0.0, costs=(), net_bps_horizon=15.0, capacity_usd=cap, risk_score=1.0, rank_score=1.0, prov="MEASURED")


def pstate(**kw):
    d = dict(equity_usd=100_000.0, cash_usd=100_000.0, peak_equity_usd=100_000.0, positions={}, open_groups=0, margin_used_usd=0.0, market_ts_ms=NOW - 1000, account_ts_ms=NOW - 1000)
    d.update(kw)
    return PF.PortfolioState(**d)


class Stack:
    """allocate -> risk -> authority -> engine + SimVenue -> ledger (+ the pipeline's LedgerAdapter, the control-centre artifact writer's ledger) -> reconcile."""

    def __init__(self, tmp_path, *, pol=None, state=None):
        self.now = NOW
        self.pol = pol or PF.PortfolioPolicy(style="TAKER")
        self.pstate = state or pstate()
        self.ins, self.sn, self.sp, self.depth = {SYM: inst()}, {SYM: snap()}, {SYM: inst()}, {SYM: 1e9}
        self.rpol = PIPE.derive_risk_policy([SYM], candidate_id="c1", spec_digest="spec1", portfolio=self.pol, equity_usd=100_000.0)
        self.authority = MA.MultiSymbolAuthority(policy=self.rpol, transport=PIPE.SimulatedTransport(), state_dir=str(tmp_path / "st"), strategy="contract_test",
                                                 state_provider=lambda: self.pstate.to_risk_state(), snapshot_provider=lambda: self.sn, kill_switch_reader=lambda: (False, ""),
                                                 clock=lambda: self.now / 1000.0, hedge_reader=self._hedge)
        self.venue = EX.SimVenue()
        self.engine = EX.ExecutionEngine(EX.MemoryStore(), send_order=self._send, cancel_order=self.venue.cancel_order, venue_state=self.venue.venue_state,
                                         qty_steps={f"linear:{SYM}": "0.1", f"spot:{SYM}": "0.1"})
        self.ledger = ACC.Ledger(track_acks=True)
        self.ledger.set_exit_fee_bps(5.5, 0.0)
        self.adapter = PIPE.LedgerAdapter("MEASURED", 5.5, 0.0)
        self.hook = RC.HaltOnDisagreement.for_engine(self.engine)
        self.cat = {}
        self.queued = []

    def _hedge(self, symbol):
        pos = self.venue.positions()                                                  # the venue's OWN position read, as the pipeline wires it
        return max(0.0, -pos.get(f"linear:{symbol}", 0.0)), max(0.0, pos.get(f"spot:{symbol}", 0.0))

    # ---- the order path: every order the engine sends is authorised first, then sent to the simulated venue
    def _send(self, req):
        try:
            self.authority.authorize(PIPE.body_from_request(req), venue="testnet")
        except EA.EntryRefused as exc:
            return EX.Ack(req.link_id, "REJECTED", None, "AUTHORITY:" + ",".join(getattr(exc, "reasons", ()) or ("REFUSED",)))
        ack = self.venue.send_order(req)
        if ack.status == "ACCEPTED":
            self.ledger.acknowledge(req.group_id, req.leg.leg_id, ack.order_id)
        self.cat[req.link_id] = req.leg.category
        if ack.status == "ACCEPTED" and req.leg.reduce_only and req.leg.category == "linear":       # a taker perp exit fills on the venue AT ONCE (so the spot sell that follows is hedge-keeping)
            self.queued.append(self.venue.execute(req.link_id, float(req.leg.qty), 100.0, self.now + 3100, liquidity="TAKER"))
        return ack

    def allocate(self, o=None):
        return PF.allocate([o or opp()], state=self.pstate, policy=self.pol, instruments=self.ins, snapshots=self.sn, decision_ms=NOW, spot_instruments=self.sp,
                           spot_depth_usd=self.depth, risk_assess=RK.risk_assess, risk_policy=self.rpol)

    def enter(self):
        a = self.allocate()
        assert len(a.plans) == 1, [d.reasons for d in a.decisions]
        plan = a.plans[0]
        dec = self.authority.register_group(plan)
        assert dec.approved, dec.reasons
        self.engine.plan_group(plan, NOW)
        self.engine.approve_group(plan.group_id, dec, NOW)
        st = self.engine.submit_group(plan.group_id, NOW, market_ts_ms=NOW - 100, account_ts_ms=NOW - 100)
        assert st == G.SUBMITTED, (st, self.engine.groups[plan.group_id].reason)
        return plan

    def fill(self, link, qty, price, t, *, liq="TAKER", deliver_engine=True, deliver_ledger=True):
        f = self.venue.execute(link, qty, price, t, liquidity=liq)
        assert f is not None
        self.deliver(f, t, deliver_engine=deliver_engine, deliver_ledger=deliver_ledger)
        return f

    def deliver(self, f, t, *, deliver_engine=True, deliver_ledger=True):
        cat = self.venue.orders[f.fill_id.rsplit(":", 1)[0]]["category"]
        if deliver_engine:
            self.engine.on_fill(f, t)
        if deliver_ledger:
            self.ledger.record_fill(f, category=cat, position_leg=f"{cat}:{SYM}")        # exits carry new leg ids: book them into the POSITION leg
            self.adapter.record_fill(f, cat)
            slip = (f.price - MARK) * f.qty * (1 if f.side == "Buy" else -1)
            self.ledger.record_slippage(slip, "taker", SYM, key="slip:" + f.fill_id)
            self.adapter.record_slippage(slip, "taker", SYM)
            if cat == "linear":
                self.ledger.declare_no_funding(SYM, "no print crossed")

    def flush(self, t):
        q, self.queued = self.queued, []
        for f in q:
            self.deliver(f, t)

    def mark(self, price, t, bid=None, ask=None):
        for cat, key in (("linear", SYM), ("spot", "SPOT:" + SYM)):
            self.ledger.mark(SYM, t, price, category=cat, prov="MEASURED", bid=bid, ask=ask)
            self.adapter.mark(key, t, price, bid, ask)

    def book_funding(self, ts, amount):
        self.ledger.record_funding(SYM, ts, amount, "MEASURED")
        self.adapter.record_funding(SYM, ts, amount, "MEASURED")

    def _artifact_net(self):
        p = self.adapter.pnl()
        return p["net"] if p["net_complete"] else None                                  # the artifacts write null whenever net_complete is False

    def views(self, **over):
        kw = dict(engine=self.engine, ledger=self.ledger, authority=self.authority, venue_read=self.venue.venue_state(),
                  net_pnl={"ledger": self.ledger.canonical_net()["net"], "artifact_ledger": self._artifact_net()},
                  view_ts_ms={"account": self.now - 100, "market": self.now - 100}, now_ms=self.now)
        kw.update(over)
        return RC.collect_views(**kw)

    def check(self, **over):
        return RC.reconcile(self.views(**over))

    def fill_all(self, plan, t=NOW + 1000):
        for lg in plan.legs:
            link = MA.link_id(plan.group_id, lg.leg_id)
            self.fill(link, float(lg.qty), 100.0 + (0.07 if lg.side == "Buy" and lg.category == "linear" else 0.0) * 0 + (0.05 if lg.side == "Buy" else -0.05), t)
        return plan


@pytest.fixture()
def stack(tmp_path):
    return Stack(tmp_path)


# ================================================================================================ the happy path: the same truth at every stage
def test_same_positions_and_same_net_at_every_stage_of_a_full_cycle(stack):
    s = stack
    assert s.check().ok                                                              # nothing yet: every view empty
    plan = s.enter()
    rep = s.check()
    assert rep.ok, rep.reasons                                                        # submitted, nothing filled: engine / venue / ledger flat, one reservation per leg, group live
    rs = RC.authority_reservations(s.authority)
    assert set(rs["registered"]) == {plan.group_id} and len(rs["reserved_links"]) == 2

    perp, spot = (next(l for l in plan.legs if l.category == c) for c in ("linear", "spot"))
    plink, slink = MA.link_id(plan.group_id, perp.leg_id), MA.link_id(plan.group_id, spot.leg_id)
    q = float(perp.qty)
    half = round(q / 2, 1)
    s.fill(plink, half, 99.95, NOW + 1000)                                            # PARTIAL fill of the perp leg
    assert s.engine.positions() == {f"linear:{SYM}": format(D(str(-half)).normalize(), "f")}
    assert s.ledger.positions() == {SYM: D(str(-half))} and s.venue.positions() == {f"linear:{SYM}": -half}
    s.mark(MARK, NOW + 1500, BID, ASK)
    rep = s.check()
    assert rep.ok, rep.reasons                                                        # the partial fill is the same number in engine, venue, ledger
    assert s.ledger.canonical_net()["net"] == pytest.approx(s.adapter.pnl()["net"])

    s.fill(plink, q - half, 99.95, NOW + 2000)
    s.fill(slink, q, 100.05, NOW + 2000)
    assert s.engine.groups[plan.group_id].state == G.HEDGED
    s.mark(MARK, NOW + 2500, BID, ASK)
    s.book_funding(NOW + 2600, 0.5)
    rep = s.check()
    assert rep.ok, rep.reasons
    cn, ap = s.ledger.canonical_net(), s.adapter.pnl()
    assert cn["net"] == pytest.approx(ap["net"], abs=1e-9) and cn["fees"] == pytest.approx(ap["fees"]) and cn["funding"] == pytest.approx(ap["funding"])
    assert cn["taker_slippage"] == pytest.approx(ap["taker_slippage"]) and cn["valuation"] == "EXIT_SIDE" and ap["valuation"] == "EXIT_SIDE"

    s.engine.request_exit(plan.group_id, NOW + 3000)
    s.flush(NOW + 3100)
    for link, o in list(s.venue.orders.items()):
        if "-x" in link and o["status"] == "New":
            s.fill(link, o["qty"], 100.0, NOW + 3100)
    assert s.engine.groups[plan.group_id].state == G.CLOSED
    s.mark(MARK, NOW + 3200, BID, ASK)
    rep = s.check()
    assert rep.codes() == (RC.RESERVATION_LEAK,) and "CLOSED" in rep.findings[0].detail         # flat and closed: the ONLY disagreement is the reservation nobody released yet
    s.authority.resolve_group(plan.group_id)
    rep = s.check()
    assert rep.ok, rep.reasons
    assert s.venue.positions() == {f"linear:{SYM}": 0.0, f"spot:{SYM}": 0.0} and s.engine.positions() == {} and set(s.ledger.positions().values()) == {D(0)}
    assert s.ledger.pnl()["internally_consistent"] is True and s.ledger.canonical_net()["net"] == pytest.approx(s.adapter.pnl()["net"], abs=1e-9)
    # exact: the Decimal ledger's net is reproducible from its own inputs
    assert ACC.Ledger.from_inputs(s.ledger.export_inputs()).pnl_exact()["net"] == s.ledger.pnl_exact()["net"]


def test_ledger_reconciles_with_the_venue_executions_of_the_cycle(stack):
    s = stack
    plan = s.enter()
    s.fill_all(plan)
    ex = [{"execId": x["fill_id"], "orderLinkId": x["link_id"], "symbol": x["symbol"], "side": x["side"], "execQty": str(x["qty"]), "execPrice": str(x["price"]),
           "execFee": str(x["fee_usd"])} for x in s.venue.venue_state()["executions"]]
    pos = {k.replace("linear:", "").replace("spot:", "SPOT:"): v for k, v in s.venue.positions().items()}
    fees = sum(D(str(x["fee_usd"])) for x in s.venue.venue_state()["executions"])
    r = s.ledger.reconcile_with_venue(ex, pos, None, fees)
    assert r["venue_reconciled"] is True, r["findings"]
    s.ledger.record_fill(T.Fill("X1", plan.group_id, plan.legs[0].leg_id, SYM, "Buy", 1.0, 100.0, 0.01, "USDT", "TAKER", NOW + 9000, "SIMULATED"))
    assert s.ledger.pnl()["venue_reconciled"] is False                                # a booking the venue never confirmed makes it stale


# ================================================================================================ rejected / failed groups leave no reservation
def test_a_risk_rejected_allocation_registers_nothing_and_reserves_nothing(tmp_path):
    s = Stack(tmp_path, state=pstate(equity_usd=100_000.0, peak_equity_usd=130_000.0))        # drawdown 23% > 10%
    a = s.allocate()
    assert a.plans == () and any("DRAWDOWN" in r for r in a.decisions[0].reasons)
    rs = RC.authority_reservations(s.authority)
    assert rs["registered"] == {} and rs["reserved_links"] == set()
    assert s.check().ok


def test_a_plan_the_authority_refuses_leaves_no_group_and_no_reservation(stack):
    s = stack
    plan = s.allocate().plans[0]
    big = T.OrderGroupPlan(plan.group_id, plan.candidate_id, plan.spec_digest, plan.policy_version, plan.created_ms,
                           tuple(T.LegPlan(l.leg_id, l.symbol, l.category, l.side, "100000", l.order_type, l.price, l.reduce_only, l.post_only, l.time_in_force) for l in plan.legs),
                           plan.style, plan.max_wait_ms, plan.notional_usd)
    dec = s.authority.register_group(big)
    assert not dec.approved and any("ABOVE_CAP" in r for r in dec.reasons)
    rs = RC.authority_reservations(s.authority)
    assert rs["registered"] == {} and rs["reserved_links"] == set()
    s.engine.plan_group(big, NOW)
    s.engine.approve_group(big.group_id, dec, NOW)
    assert s.engine.groups[big.group_id].state == G.FAILED and s.venue.orders == {}
    assert s.check().ok                                                                # a failed, never-registered group is not a disagreement


def test_a_group_that_failed_after_registration_is_a_named_leak_until_released_and_release_is_narrow(stack):
    s = stack
    plan = s.allocate().plans[0]
    assert s.authority.register_group(plan).approved
    s.engine.plan_group(plan, NOW)
    s.engine.approve_group(plan.group_id, s.authority.register_group(plan), NOW)
    s.engine.halt_new_exposure("TEST_HALT", NOW)                                       # the submit is refused by the engine: the group FAILS having reserved capacity
    assert s.engine.submit_group(plan.group_id, NOW, market_ts_ms=NOW - 100, account_ts_ms=NOW - 100) == G.FAILED
    s.engine.resume_new_exposure("TEST_HALT", NOW)
    rep = s.check()
    assert rep.codes() == (RC.RESERVATION_LEAK,) and rep.findings[0].key == plan.group_id and rep.halt_new_exposure
    v = s.views()
    assert RC.release_candidates(v) == [plan.group_id]
    assert RC.release_leaked(s.authority, s.engine, ["nope"], s.venue.venue_state()) == {"nope": "REFUSED:NOT_REGISTERED"}
    assert RC.release_leaked(s.authority, s.engine, [plan.group_id], s.venue.venue_state()) == {plan.group_id: "RELEASED"}
    assert RC.release_leaked(s.authority, s.engine, [plan.group_id], s.venue.venue_state()) == {plan.group_id: "REFUSED:ALREADY_RESOLVED"}
    assert s.check().ok


def test_release_refuses_a_group_that_is_not_terminal_or_that_still_holds_a_position(stack):
    s = stack
    plan = s.enter()
    assert RC.release_leaked(s.authority, s.engine, [plan.group_id], s.venue.venue_state())[plan.group_id].startswith("REFUSED:ENGINE_STATE_")
    perp = next(l for l in plan.legs if l.category == "linear")
    s.fill(MA.link_id(plan.group_id, perp.leg_id), float(perp.qty), 100.0, NOW + 1000)
    s.engine._set_state(s.engine.groups[plan.group_id], G.FAILED, "FORCED_FOR_TEST", NOW + 1100)                 # terminal in name while the engine still holds the perp
    assert RC.release_leaked(s.authority, s.engine, [plan.group_id], s.venue.venue_state()) == {plan.group_id: "REFUSED:VENUE_HOLDS_A_POSITION_ON_THE_GROUPS_SYMBOL"}


# ================================================================================================ injected divergences HALT with the right finding
def halted(s, rep, now=NOW + 5000):
    s.hook(rep, now)
    return s.engine.new_exposure_block(now, now - 100, now - 100)


def second_plan_is_refused_by_the_halt(s, now=NOW + 5000):
    p2 = T.OrderGroupPlan("gSECOND00000001", "c1", "spec1", s.pol.policy_version, now, (
        T.LegPlan("lP2", SYM, "linear", "Sell", "1", "Market", None, False, False, "IOC"), T.LegPlan("lS2", SYM, "spot", "Buy", "1", "Market", None, False, False, "IOC")),
        "TAKER", 1000, 200.0)
    s.engine.plan_group(p2, now)
    s.engine.approve_group(p2.group_id, type("A", (), {"approved": True, "reasons": ()})(), now)
    return s.engine.submit_group(p2.group_id, now, market_ts_ms=now - 100, account_ts_ms=now - 100)


def test_ledger_drops_a_fill_halts_new_exposure_and_names_it(stack):
    s = stack
    plan = s.enter()
    spot = next(l for l in plan.legs if l.category == "spot")
    perp = next(l for l in plan.legs if l.category == "linear")
    s.fill(MA.link_id(plan.group_id, perp.leg_id), float(perp.qty), 99.95, NOW + 1000)
    s.fill(MA.link_id(plan.group_id, spot.leg_id), float(spot.qty), 100.05, NOW + 1000, deliver_ledger=False)       # the ledger never sees the spot fill
    rep = s.check()
    assert rep.halt_new_exposure and [(f.code, f.key) for f in rep.findings if f.code == RC.MISSING_IN_LEDGER] == [(RC.MISSING_IN_LEDGER, f"SPOT:{SYM}")]
    blocked = halted(s, rep)
    assert blocked is not None and blocked.startswith("NEW_EXPOSURE_HALTED:RECONCILE_DISAGREEMENT:") and f"MISSING_IN_LEDGER:SPOT:{SYM}" in blocked
    assert second_plan_is_refused_by_the_halt(s) == G.FAILED
    assert s.ledger.positions().get(f"SPOT:{SYM}") is None                             # the ledger was NOT overwritten from the venue / engine
    assert s.engine.positions()[f"spot:{SYM}"] == format(D(str(spot.qty)).normalize(), "f")      # nor was the engine


def test_venue_shows_extra_quantity_halts_and_is_never_adopted(stack):
    s = stack
    plan = s.enter()
    s.fill_all(plan)
    assert s.check().ok
    before = (dict(s.engine.positions()), dict(s.ledger.positions()))
    s.venue.position_override[f"linear:{SYM}"] = -float(next(l for l in plan.legs if l.category == "linear").qty) - 2.0          # someone else sold 2 more
    rep = s.check()
    assert {(f.code, f.key) for f in rep.findings} == {(RC.QTY_MISMATCH, SYM)} and {v for f in rep.findings for v in f.views} == {"venue", "engine", "ledger"}
    assert halted(s, rep) is not None
    assert (dict(s.engine.positions()), dict(s.ledger.positions())) == before


def test_venue_read_incomplete_or_failing_halts_new_exposure(stack):
    s = stack
    s.enter()
    s.venue.incomplete_view = True
    rep = s.check()
    assert RC.INCOMPLETE_VENUE_READ in rep.codes() and halted(s, rep) is not None
    s.venue.incomplete_view = False
    s.venue.positions_unknown = True
    assert RC.INCOMPLETE_VENUE_READ in s.check().codes()


def test_duplicate_funding_is_idempotent_and_a_conflicting_one_halts(stack):
    s = stack
    plan = s.enter()
    s.fill_all(plan)
    s.mark(MARK, NOW + 2000, BID, ASK)
    s.book_funding(NOW + 2600, 0.5)
    base = s.ledger.canonical_net()["net"]
    s.ledger.record_funding(SYM, NOW + 2600, 0.5, "MEASURED")                          # the venue re-sent the same settlement
    assert s.ledger.canonical_net()["net"] == base and s.check().ok
    s.ledger.record_funding(SYM, NOW + 2600, 0.7, "MEASURED")                          # ... and once with a different amount: two truths for one key
    rep = s.check()
    assert rep.halt_new_exposure and (RC.LEDGER_INTEGRITY, f"FUNDING_CONFLICT:{SYM}:{NOW + 2600}") in {(f.code, f.key) for f in rep.findings}
    assert s.ledger.canonical_net()["net"] == base                                     # the first record stays; nothing was overwritten


def test_a_writer_that_counts_a_funding_event_twice_is_a_net_pnl_mismatch(stack):
    s = stack
    plan = s.enter()
    s.fill_all(plan)
    s.mark(MARK, NOW + 2000, BID, ASK)
    s.book_funding(NOW + 2600, 0.5)
    assert s.check().ok
    s.adapter.record_funding(SYM, NOW + 2700, 0.5, "MEASURED")                        # the artifact ledger books a "duplicate" under another ts: its net drifts from the canonical ledger
    rep = s.check()
    assert rep.codes() == (RC.NET_PNL_MISMATCH,) and rep.findings[0].views == ("ledger", "artifact_ledger") and rep.halt_new_exposure


def test_stale_account_view_halts_and_a_fresh_one_does_not(stack):
    s = stack
    s.enter()
    rep = s.check(view_ts_ms={"account": NOW - 120_000, "market": NOW - 100})
    assert rep.codes() == (RC.STALE_VIEW,) and rep.findings[0].key == "account" and halted(s, rep) is not None
    assert s.check().ok


def test_orphan_fill_before_the_ack_is_flagged_by_the_ledger_and_halts_reconcile(stack):
    s = stack
    plan = s.enter()
    perp = next(l for l in plan.legs if l.category == "linear")
    fresh = ACC.Ledger(track_acks=True)                                                # a restarted process: its ledger saw the fill but not the acknowledgement
    f = T.Fill("e1", plan.group_id, perp.leg_id, SYM, "Sell", 1.0, 100.0, 0.05, "USDT", "TAKER", NOW + 1000, "MEASURED")
    fresh.record_fill(f)
    rep = RC.reconcile(RC.Views(ledger_flags=tuple(fresh.integrity_flags()), engine_positions={}, venue_positions={}, venue_complete=True))
    assert rep.codes() == (RC.LEDGER_INTEGRITY,) and rep.findings[0].key == f"ORPHAN_FILL:{plan.group_id}:{perp.leg_id}"
    fresh.acknowledge(plan.group_id, perp.leg_id)
    assert fresh.integrity_flags() == []


# ================================================================================================ the control-centre artifact writer's ledger equals the ledger
def test_ledger_adapter_net_and_components_equal_the_canonical_ledger_in_every_valuation_state(stack):
    s = stack
    plan = s.enter()
    s.fill_all(plan)
    for book in ((BID, ASK), (None, None)):
        s.mark(MARK, NOW + 2000 + (1 if book[0] is None else 0), *book)
        cn, ap = s.ledger.canonical_net(), s.adapter.pnl()
        assert cn["net"] == pytest.approx(ap["net"], abs=1e-9) and cn["valuation"] == ap["valuation"]
        assert cn["price_leg"] == pytest.approx(ap["price_leg"], abs=1e-9)


# ================================================================================================ portfolio-level hard limits at the allocation boundary
def test_allocation_blocks_on_a_daily_loss_breach_and_fails_closed_without_the_input(tmp_path):
    pol = PF.PortfolioPolicy(style="TAKER", max_daily_loss_usd=500.0)
    ok = Stack(tmp_path / "a", pol=pol, state=pstate(day_start_equity_usd=100_200.0))
    assert len(ok.allocate().plans) == 1
    breach = Stack(tmp_path / "b", pol=pol, state=pstate(day_start_equity_usd=100_500.0))
    d = breach.allocate().decisions[0]
    assert d.action == "SKIP" and "BLOCK_DAILY_LOSS_HALT" in d.reasons
    unknown = Stack(tmp_path / "c", pol=pol, state=pstate())
    d = unknown.allocate().decisions[0]
    assert d.action == "SKIP" and "BLOCK_DAILY_LOSS_INPUT_UNKNOWN" in d.reasons


def test_the_risk_gate_applies_the_same_hard_limits_to_the_authority_registration(tmp_path):
    s = Stack(tmp_path)
    plan = s.allocate().plans[0]
    hard = {**s.rpol, "max_daily_loss_usd": 500.0, "max_drawdown_frac": 0.05}
    for k, v in (("peak_equity_usd", 100_000.0),):
        assert RK.risk_assess(plan, {**s.pstate.to_risk_state(), "day_start_equity_usd": 100_200.0}, hard, s.sn).approved
    d = RK.risk_assess(plan, {**s.pstate.to_risk_state(), "day_start_equity_usd": 101_000.0}, hard, s.sn)
    assert not d.approved and "DAILY_LOSS_LIMIT_BREACHED" in d.reasons and d.limits["halt_new_exposure"] is True
    assert "DAILY_LOSS_INPUT_UNKNOWN" in RK.risk_assess(plan, s.pstate.to_risk_state(), hard, s.sn).reasons
    # the authority built on the hard policy refuses registration when its own state provider lacks the input
    s2 = Stack(tmp_path / "x")
    s2.rpol = hard
    auth = MA.MultiSymbolAuthority(policy=hard, transport=PIPE.SimulatedTransport(), state_dir=str(tmp_path / "x" / "st2"), strategy="contract_test",
                                   state_provider=lambda: s2.pstate.to_risk_state(), snapshot_provider=lambda: s2.sn, kill_switch_reader=lambda: (False, ""), clock=lambda: NOW / 1000.0)
    dec = auth.register_group(plan)
    assert not dec.approved and "DAILY_LOSS_INPUT_UNKNOWN" in dec.reasons
    assert RC.authority_reservations(auth)["registered"] == {}
