"""ms_execution: the order-group lifecycle under the fault matrix (lost/duplicate ack, duplicate/delayed fill, one-leg and partial fills, failed hedge, unknown position, stale
data, restart, crash between acceptance and persistence, risk breach, instrument halt, funding interval change, cancel race). Simulated venue only; no network, no clock."""
from __future__ import annotations

from decimal import Decimal
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (BOT, os.path.join(BOT, "tools")):
    sys.path.insert(0, _p)

import ms_execution as EX  # noqa: E402
import ms_types as T  # noqa: E402
from ms_types import GroupState as G  # noqa: E402

GID = "gTEST000000000001"
PERP, SPOT = "lPERP00001", "lSPOT00001"
PL, SL = f"{GID}-{PERP}", f"{GID}-{SPOT}"


def mkplan(gid=GID, perp=PERP, spot=SPOT, sym="XUSDT", max_wait=60_000):
    return T.OrderGroupPlan(gid, "cand1", "spec1", "pol1", 1_000, (
        T.LegPlan(perp, sym, "linear", "Sell", "10", "Limit", "100.1", True, False, "GTC"),
        T.LegPlan(spot, sym, "spot", "Buy", "10", "Limit", "100", True, False, "GTC")), "MAKER_FIRST", max_wait, 2000.0)


class Approve:
    approved, reasons, limits = True, (), {}


class Reject:
    approved, reasons, limits = False, ("LIQ_DISTANCE",), {}


class H:
    def __init__(self, venue=None, store=None, policy=None, plan=None):
        self.v = venue or EX.SimVenue()
        self.store = store or EX.MemoryStore()
        self.policy = policy or EX.ExecPolicy()
        self.e = self.make()
        self.plan = plan or mkplan()

    def make(self):
        return EX.ExecutionEngine(self.store, send_order=self.v.send_order, cancel_order=self.v.cancel_order, venue_state=self.v.venue_state, policy=self.policy,
                                  qty_steps={"linear:XUSDT": "0.1", "spot:XUSDT": "0.1"})

    def start(self, t=2_000, approve=True, plan=None):
        p = plan or self.plan
        self.e.plan_group(p, t)
        self.e.approve_group(p.group_id, Approve() if approve else Reject(), t)
        return self.e.submit_group(p.group_id, t, market_ts_ms=t - 100, account_ts_ms=t - 100)

    def fill(self, link, qty, price=100.0, t=3_000, liq="MAKER", deliver=True):
        f = self.v.execute(link, qty, price, t, liquidity=liq)
        if deliver:
            self.e.on_fill(f, t)
        return f

    def state(self, gid=GID):
        return self.e.groups[gid].state

    def types(self, typ):
        return [x for x in self.e.journal.entries if x["type"] == typ]

    def transitions(self, gid=GID):
        return [x["body"]["to"] for x in self.types("STATE") if x["group_id"] == gid]


def hedged(h):
    h.start()
    h.fill(PL, 10)
    h.fill(SL, 10)
    assert h.state() == G.HEDGED
    return h


# ---------------------------------------------------------------- positive control and the normal path

def test_positive_control_both_legs_fill_hedged_then_exit_closes_only_when_the_venue_reads_flat():
    h = H()
    assert h.start() == G.SUBMITTED
    h.fill(PL, 4)
    assert h.state() == G.PARTIAL
    h.fill(PL, 6)
    assert h.state() == G.PARTIAL
    h.fill(SL, 10)
    assert h.state() == G.HEDGED and h.e.positions() == {"linear:XUSDT": "-10", "spot:XUSDT": "10"}
    h.e.request_exit(GID, 5_000)
    exits = [o for k, o in h.v.orders.items() if "-x" in k]
    assert len(exits) == 2 and all(o["order_type"] == "Market" for o in exits)
    assert [o["reduce_only"] for o in exits if o["category"] == "linear"] == [True]
    assert h.state() == G.EXITING
    for k, o in list(h.v.orders.items()):
        if "-x" in k:
            h.fill(k, o["qty"], 100.0, 5_100, liq="TAKER")
    assert h.state() == G.CLOSED
    assert h.transitions() == ["RISK_APPROVED", "SUBMITTED", "PARTIAL", "HEDGED", "EXITING", "CLOSED"]
    ok, why = EX.verify_chain(h.store.read())
    assert ok, why


def test_nothing_is_sent_for_an_unapproved_or_risk_rejected_group():
    h = H()
    h.e.plan_group(h.plan, 1)
    assert h.e.submit_group(GID, 2, market_ts_ms=1, account_ts_ms=1) == G.PLANNED and h.v.send_calls == {}
    h.e.approve_group(GID, Reject(), 3)
    assert h.state() == G.FAILED and h.v.send_calls == {}
    assert h.e.submit_group(GID, 4, market_ts_ms=3, account_ts_ms=3) == G.FAILED and h.v.send_calls == {}


def test_plan_group_is_idempotent_and_every_leg_order_is_bound_to_group_candidate_and_spec():
    seen = []
    v = EX.SimVenue()
    h = H(venue=v)
    orig = v.send_order
    h.e._send = lambda req: (seen.append(req), orig(req))[1]
    h.start()
    h.e.plan_group(h.plan, 9)
    assert len(h.types("PLAN")) == 1
    assert {(r.group_id, r.candidate_id, r.spec_digest, r.link_id) for r in seen} == {(GID, "cand1", "spec1", PL), (GID, "cand1", "spec1", SL)}


# ---------------------------------------------------------------- acks

def test_lost_ack_makes_the_outcome_unknown_and_reconcile_adopts_the_order_with_no_resend():
    h = H()
    h.v.drop_ack.add(PL)
    assert h.start() == G.UNKNOWN_OUTCOME
    assert h.v.send_calls == {PL: 1}                                  # the second leg is not sent while the first is unknown
    h.e.step(2_100)                                                    # reconcile finds the order under the link id
    h.e.step(60_000)
    h.e.step(120_000)
    assert h.v.send_calls[PL] == 1 and h.e.groups[GID].legs[PERP].status != EX.UNKNOWN and h.e.groups[GID].legs[PERP].order_id == h.v.orders[PL]["order_id"]
    assert h.state() == G.FAILED and h.v.orders[PL]["status"] == "Cancelled"      # the unsent sibling can never complete the hedge: nothing was left working
    assert SL not in h.v.send_calls


def test_a_lost_request_is_resent_once_only_after_a_complete_read_shows_no_order_and_the_grace_elapsed():
    h = H()
    h.v.lose_request.add(PL)
    h.start()
    assert h.state() == G.UNKNOWN_OUTCOME and h.v.send_calls == {PL: 1}
    h.e.step(2_100)                                                    # grace (2000) not elapsed since the intent at 2000
    assert h.v.send_calls[PL] == 1 and h.state() == G.UNKNOWN_OUTCOME
    h.v.incomplete_view = True
    h.e.step(10_000)
    assert h.v.send_calls[PL] == 1                                     # an incomplete read proves nothing
    h.v.incomplete_view = False
    h.v.lose_request.clear()
    h.e.step(10_100)
    assert h.v.send_calls[PL] == 2 and len(h.v.orders) == 1            # exactly one resend, same link id (idempotency key)
    h.e.step(10_200)
    h.e.step(10_300)
    assert h.v.send_calls[PL] == 2


def test_never_blind_resend_the_engine_does_not_resend_on_its_own_even_when_steps_repeat():
    h = H()
    h.v.drop_ack.add(PL)
    h.start()
    h.v.fail_state_read = True                                         # reconcile impossible: the leg must stay unknown
    for t in range(3_000, 30_000, 3_000):
        h.e.step(t)
    assert h.v.send_calls[PL] == 1 and h.state() == G.UNKNOWN_OUTCOME and "UNKNOWN_VENUE_POSITION" in h.e.halts


def test_duplicate_ack_is_counted_and_ignored():
    h = H()
    h.start()
    before = h.e.groups[GID].state
    h.e.on_ack(EX.Ack(PL, "ACCEPTED", h.v.orders[PL]["order_id"]), GID, PERP, 2_050)
    assert h.e.groups[GID].ack_dups == 1 and h.state() == before and len(h.types("ACK_DUP")) == 1
    h.e.on_ack(EX.Ack(PL, "ACCEPTED", "OTHER"), GID, PERP, 2_060)       # a DIFFERENT order id under the same link is an anomaly: halt the group
    assert h.state() == G.HALTED and h.e.anomalies[-1]["code"] == "CONFLICTING_ORDER_ID"


# ---------------------------------------------------------------- fills

def test_duplicate_fills_are_deduplicated_by_fill_id():
    h = H()
    h.start()
    f = h.fill(PL, 4)
    assert h.e.on_fill(f, 3_100) is False and h.e.on_fill(f, 3_200) is False
    assert h.e.groups[GID].legs[PERP].filled == 4 and h.e.groups[GID].fill_dups == 2 and h.e.positions() == {"linear:XUSDT": "-4"}


def test_fills_from_an_unknown_group_halt_new_exposure():
    h = H()
    h.start()
    stray = T.Fill("zz", "gNOPE", "lX", "XUSDT", "Sell", 1, 100, 0.1, "USDT", "MAKER", 5, "SIMULATED")
    assert h.e.on_fill(stray, 5) is False and "UNATTRIBUTED_FILL" in h.e.halts


def test_one_leg_fills_the_other_never_does_the_maker_leg_is_cancelled_and_the_filled_leg_is_unwound_with_a_reduce_only_taker():
    h = H()
    h.start()
    h.fill(PL, 10)
    assert h.state() == G.PARTIAL
    h.e.step(2_000 + 60_000)                                           # max_wait elapsed: cancel the unfilled spot maker leg
    assert h.v.orders[SL]["status"] == "Cancelled"
    unwind = [o for k, o in h.v.orders.items() if "-x" in k]
    assert len(unwind) == 1 and unwind[0]["side"] == "Buy" and unwind[0]["order_type"] == "Market" and unwind[0]["reduce_only"] and unwind[0]["qty"] == 10
    assert h.state() == G.EXITING and h.e.groups[GID].exit_mode == "UNWIND"
    h.fill(next(k for k in h.v.orders if "-x" in k), 10, liq="TAKER", t=63_000)
    assert h.state() == G.CLOSED and h.v.positions().get("linear:XUSDT", 0) == 0
    assert "UNHEDGED" in h.transitions()


def test_partial_fills_on_both_legs_hedge_the_matched_part_and_unwind_the_excess_only():
    h = H()
    h.start()
    h.fill(PL, 6)
    h.fill(SL, 3)
    assert h.state() == G.PARTIAL
    h.e.step(70_000)
    # perp 6 vs spot 3 => 3 of the perp is excess; unwind exactly that
    unwind = [o for k, o in h.v.orders.items() if "-x" in k]
    assert [(o["category"], o["side"], o["qty"]) for o in unwind] == [("linear", "Buy", 3.0)]
    h.fill(next(k for k in h.v.orders if "-x" in k), 3, liq="TAKER", t=71_000)
    assert h.state() == G.HEDGED and h.e.groups[GID].exit_mode is None
    assert h.e.positions() == {"linear:XUSDT": "-3", "spot:XUSDT": "3"}


def test_failed_hedge_leg_rejected_cancels_the_sibling_and_nothing_is_left_working():
    h = H()
    h.v.reject[SL] = "PostOnly would cross"
    h.start()
    assert h.state() == G.FAILED and h.v.orders[PL]["status"] == "Cancelled" and h.e.open_groups() == 0


def test_failed_hedge_after_the_first_leg_filled_unwinds_taker_and_closes_flat():
    h = H()
    h.start()
    h.fill(PL, 10)
    h.e.step(70_000)
    assert h.state() == G.EXITING
    ex = next(k for k in h.v.orders if "-x" in k)
    h.fill(ex, 10, liq="TAKER", t=70_500)
    assert h.state() == G.CLOSED


def test_delayed_fill_after_cancel_reopens_the_group_as_unhedged_and_it_is_unwound():
    h = H()
    h.start()
    h.e.step(70_000)                                                   # nothing filled: both cancelled
    assert h.state() == G.FAILED
    f = h.v.execute(PL, 10, 100.0, 69_999)                             # the cancel raced a fill the venue had already matched? (the engine hears late)
    h.v.orders[PL]["status"] = "Filled"
    h.e.on_fill(f, 71_000)
    assert h.e.groups[GID].legs[PERP].filled == 10 and h.state() == G.EXITING
    assert any("-x" in k for k in h.v.orders) and "UNHEDGED" in h.transitions()


def test_cancel_race_the_order_filled_before_the_cancel_so_the_leg_is_unknown_until_reconcile_adopts_the_fill_once():
    h = H()
    h.start()
    h.fill(SL, 10)
    h.fill(PL, 10, deliver=False)                                       # filled at the venue, the fill message is still in flight
    h.e.step(70_000)                                                    # engine cancels perp: TOO_LATE
    assert h.e.groups[GID].legs[PERP].filled == 0 and h.state() == G.UNKNOWN_OUTCOME and h.v.orders[PL]["status"] == "Filled"
    h.e.step(70_100)
    h.e.step(70_200)
    assert h.e.groups[GID].legs[PERP].filled == 10                      # adopted exactly once
    assert len([x for x in h.types("FILL") if x["body"]["fill"]["leg_id"] == PERP]) == 1
    late = h.v.executions[-2]
    assert h.e.on_fill(T.Fill(late["fill_id"], GID, PERP, "XUSDT", "Sell", 10, 100.0, 0.02, "USDT", "MAKER", 70_000, "SIMULATED"), 70_300) is False
    assert h.state() in (G.HEDGED,)
    assert h.e.positions() == {"linear:XUSDT": "-10", "spot:XUSDT": "10"}


def test_a_lost_cancel_ack_leaves_the_leg_unknown_and_reconcile_resolves_it():
    h = H()
    h.start()
    h.v.cancel_lost.add(SL)
    h.fill(PL, 10)
    h.e.step(70_000)
    assert h.e.groups[GID].legs[SPOT].status == EX.UNKNOWN and h.state() == G.UNKNOWN_OUTCOME
    h.v.cancel_lost.clear()
    h.e.step(70_100)
    assert h.v.orders[SL]["status"] == "Cancelled" and h.e.groups[GID].legs[SPOT].status == EX.CANCELLED     # reconcile saw it resting; the timeout pass cancelled it for real
    assert h.v.send_calls[SL] == 1
    assert any("-x" in k for k in h.v.orders)


# ---------------------------------------------------------------- unknown position, stale data, halts

def test_unknown_venue_position_halts_new_exposure_and_a_complete_read_lifts_it():
    h = H()
    h.v.positions_unknown = True
    h.e.reconcile(1_000)
    assert "UNKNOWN_VENUE_POSITION" in h.e.halts
    h.e.plan_group(h.plan, 2_000)
    h.e.approve_group(GID, Approve(), 2_000)
    assert h.e.submit_group(GID, 2_000, market_ts_ms=1_900, account_ts_ms=1_900) == G.FAILED and h.v.send_calls == {}
    assert "NEW_EXPOSURE_HALTED" in h.e.groups[GID].reason
    h.v.positions_unknown = False
    h.e.reconcile(3_000)
    assert h.e.halts == {}


def test_closed_requires_the_venue_to_read_flat_not_fill_arithmetic():
    h = hedged(H())
    h.e.request_exit(GID, 5_000)
    h.v.positions_unknown = True
    for k, o in list(h.v.orders.items()):
        if "-x" in k:
            h.fill(k, o["qty"], 100.0, 5_100, liq="TAKER")
    assert h.state() != G.CLOSED and "UNKNOWN_VENUE_POSITION" in h.e.halts
    h.v.positions_unknown = False
    h.v.position_override["linear:XUSDT"] = -2.0                          # the venue still shows a short the fills say is gone
    h.e.step(6_000)
    assert h.state() == G.HALTED and any(a["code"] in ("NOT_FLAT_ON_VENUE", "POSITION_MISMATCH") for a in h.e.anomalies)
    assert any(r.startswith("POSITION_MISMATCH") for r in h.e.halts)


@pytest.mark.parametrize("market_ts,account_ts,why", [(None, 1_950, "MARKET_DATA_UNKNOWN"), (1_950, None, "ACCOUNT_DATA_UNKNOWN"), (2_000 - 90_000, 1_950, "MARKET_DATA_STALE"),
                                                      (1_950, 2_000 - 90_000, "ACCOUNT_DATA_STALE"), (2_500, 1_950, "MARKET_DATA_STALE")])
def test_stale_or_unknown_market_or_account_data_means_no_new_exposure(market_ts, account_ts, why):
    h = H()
    h.e.plan_group(h.plan, 2_000)
    h.e.approve_group(GID, Approve(), 2_000)
    assert h.e.submit_group(GID, 2_000, market_ts_ms=market_ts, account_ts_ms=account_ts) == G.FAILED
    assert why in h.e.groups[GID].reason and h.v.send_calls == {}


def test_stale_data_does_not_stop_risk_reducing_exits():
    h = hedged(H())
    h.e.request_exit(GID, 400_000)                                       # far later: any data would be stale
    assert any("-x" in k for k in h.v.orders)


def test_instrument_halt_blocks_new_and_recovery_orders_until_resume():
    h = H()
    h.start()
    h.fill(PL, 10)
    h.e.on_instrument_halt("XUSDT", 4_000)
    assert h.state() == G.HALTED
    h.e.step(80_000)
    assert not any("-x" in k for k in h.v.orders) and h.state() == G.HALTED
    p2 = mkplan("gTEST000000000002", "lP2", "lS2")
    h.e.plan_group(p2, 80_000)
    h.e.approve_group(p2.group_id, Approve(), 80_000)
    assert h.e.submit_group(p2.group_id, 80_000, market_ts_ms=79_990, account_ts_ms=79_990) == G.FAILED and "SYMBOL_HALTED" in h.e.groups[p2.group_id].reason
    h.e.on_instrument_resume("XUSDT", 90_000)
    h.e.step(90_100)
    assert any("-x" in k for k in h.v.orders) and h.state() in (G.EXITING, G.UNKNOWN_OUTCOME)


def test_funding_interval_change_fails_unsubmitted_groups_blocks_the_symbol_and_flags_live_ones():
    h = H()
    h.e.plan_group(h.plan, 1_000)
    h.e.approve_group(GID, Approve(), 1_000)
    h.e.on_funding_interval_change("XUSDT", 480, 60, 1_500)
    assert h.state() == G.FAILED and h.e.groups[GID].reason == "FUNDING_INTERVAL_CHANGED"
    p2 = mkplan("gTEST000000000002", "lP2", "lS2")
    h.e.plan_group(p2, 2_000)
    h.e.approve_group(p2.group_id, Approve(), 2_000)
    assert h.e.submit_group(p2.group_id, 2_000, market_ts_ms=1_990, account_ts_ms=1_990) == G.FAILED and "SYMBOL_BLOCKED" in h.e.groups[p2.group_id].reason
    h2 = hedged(H())
    h2.e.on_funding_interval_change("XUSDT", 480, 240, 6_000)
    assert h2.e.groups[GID].interval_changed and h2.state() == G.HEDGED
    h3 = H(policy=EX.ExecPolicy(exit_on_interval_change=True))
    hedged(h3)
    h3.e.on_funding_interval_change("XUSDT", 480, 240, 6_000)
    assert h3.state() == G.EXITING
    h2.e.clear_symbol_block("XUSDT", 7_000)
    assert h2.e.blocked_symbols == {}


def test_risk_breach_mid_group_stops_adding_cancels_the_working_leg_and_unwinds_the_filled_one():
    h = H()
    h.start()
    h.fill(PL, 10)
    h.e.on_risk_breach(["DRAWDOWN"], 4_000)
    assert h.v.orders[SL]["status"] == "Cancelled" and any(r.startswith("RISK_BREACH") for r in h.e.halts)
    unwind = [o for k, o in h.v.orders.items() if "-x" in k]
    assert len(unwind) == 1 and unwind[0]["reduce_only"] and unwind[0]["side"] == "Buy"
    p2 = mkplan("gTEST000000000002", "lP2", "lS2", sym="YUSDT")
    h.e.plan_group(p2, 4_100)
    h.e.approve_group(p2.group_id, Approve(), 4_100)
    assert h.e.submit_group(p2.group_id, 4_100, market_ts_ms=4_090, account_ts_ms=4_090) == G.FAILED


def test_liquidation_of_one_leg_makes_the_group_unhedged_and_the_other_leg_is_unwound():
    h = hedged(H())
    h.v.force_close("XUSDT", "linear", "Buy", 10, 130.0, 6_000)
    h.e.on_forced_close(GID, PERP, 10, 6_000)
    assert h.state() == G.EXITING and h.e.groups[GID].exit_mode == "UNWIND"
    unwind = [o for k, o in h.v.orders.items() if "-x" in k]
    assert [(o["category"], o["side"]) for o in unwind] == [("spot", "Sell")]
    h.fill(next(k for k in h.v.orders if "-x" in k), 10, liq="TAKER", t=6_100)
    assert h.state() == G.CLOSED


# ---------------------------------------------------------------- restart and crash

def test_restart_with_open_orders_rebuilds_from_the_journal_and_never_resubmits():
    h = H()
    h.start()
    h.fill(PL, 4)
    sends = dict(h.v.send_calls)
    h.v.execute(SL, 10, 100.0, 5_000)                                   # filled while the process was down
    h2 = EX.ExecutionEngine.recover(h.store, 6_000, send_order=h.v.send_order, cancel_order=h.v.cancel_order, venue_state=h.v.venue_state, policy=h.policy)
    assert h.v.send_calls == sends
    assert h2.groups[GID].legs[PERP].filled == 4 and h2.groups[GID].legs[SPOT].filled == 10
    assert h2.states()[GID] == "PARTIAL"
    h2.on_fill(h.v.execute(PL, 6, 100.0, 6_100), 6_100)
    assert h2.states()[GID] == "HEDGED" and h.v.send_calls == sends
    h3 = EX.ExecutionEngine(h.store, send_order=h.v.send_order, cancel_order=h.v.cancel_order, venue_state=h.v.venue_state)
    assert h3.states() == h2.states() and h3.positions() == h2.positions()


def test_crash_after_venue_acceptance_before_persistence_is_found_by_the_idempotency_key():
    h = H()
    h.v.crash_after_accept.add(PL)
    with pytest.raises(EX.SimulatedCrash):
        h.start()
    assert [x["type"] for x in h.e.journal.entries if x["group_id"] == GID][-1] == "INTENT"      # accepted at the venue, never acked locally
    h.v.crash_after_accept.clear()
    e2 = EX.ExecutionEngine.recover(h.store, 4_000, send_order=h.v.send_order, cancel_order=h.v.cancel_order, venue_state=h.v.venue_state)
    assert h.v.send_calls == {PL: 1} and e2.groups[GID].legs[PERP].order_id == h.v.orders[PL]["order_id"]
    assert e2.groups[GID].legs[PERP].status == EX.LIVE


def test_crash_before_the_request_reached_the_venue_resends_once_after_reconcile():
    h = H()
    h.v.lose_request.add(PL)
    h.start()
    h.v.lose_request.clear()
    e2 = EX.ExecutionEngine(h.store, send_order=h.v.send_order, cancel_order=h.v.cancel_order, venue_state=h.v.venue_state)
    assert e2.states()[GID] == "UNKNOWN_OUTCOME" and h.v.send_calls == {PL: 1}      # constructing the engine sends nothing
    e2.reconcile(2_100)
    assert h.v.send_calls == {PL: 1}
    e2.reconcile(5_000)
    e2.reconcile(5_100)
    assert h.v.send_calls == {PL: 2} and len(h.v.orders) == 1


# ---------------------------------------------------------------- journal integrity

def test_the_journal_is_hash_chained_and_a_mutated_or_removed_line_is_refused():
    h = hedged(H())
    lines = h.store.read()
    assert EX.verify_chain(lines)[0]
    bad = list(lines)
    e = json.loads(bad[3])
    e["body"] = {**e["body"], "tampered": 1}
    bad[3] = json.dumps(e, sort_keys=True, separators=(",", ":"))
    assert not EX.verify_chain(bad)[0]
    assert not EX.verify_chain(lines[:2] + lines[3:])[0]
    with pytest.raises(ValueError):
        EX.ExecutionEngine(EX.MemoryStore(bad), send_order=h.v.send_order, cancel_order=h.v.cancel_order, venue_state=h.v.venue_state)


def test_file_store_round_trips_and_survives_a_restart(tmp_path):
    store = EX.FileStore(str(tmp_path / "j.jsonl"))
    h = H(store=store)
    h.start()
    h.fill(PL, 10)
    e2 = EX.ExecutionEngine(EX.FileStore(str(tmp_path / "j.jsonl")), send_order=h.v.send_order, cancel_order=h.v.cancel_order, venue_state=h.v.venue_state)
    assert e2.states() == h.e.states() and e2.positions() == h.e.positions()


def test_position_mismatch_between_engine_and_venue_halts_new_exposure():
    h = hedged(H())
    h.v.position_override["linear:XUSDT"] = -7.0
    h.e.reconcile(9_000)
    assert any(r.startswith("POSITION_MISMATCH") for r in h.e.halts)
    h.v.position_override.clear()
    h.v.position_override["linear:ZUSDT"] = 1.0                           # exposure the engine knows nothing about
    h.e.reconcile(9_100)
    assert "POSITION_MISMATCH:linear:ZUSDT" in h.e.halts


def test_folding_a_journal_that_holds_the_same_fill_twice_counts_it_once():
    h = H()
    h.start()
    f = h.fill(PL, 4)
    last = [x for x in h.e.journal.entries if x["type"] == "FILL"][-1]
    h.e.journal.append("FILL", GID, 3_500, {"fill": last["body"]["fill"]})            # a duplicate that reached the journal (e.g. a double write)
    e2 = EX.ExecutionEngine(h.store, send_order=h.v.send_order, cancel_order=h.v.cancel_order, venue_state=h.v.venue_state)
    assert e2.groups[GID].legs[PERP].filled == 4 and e2.positions() == {"linear:XUSDT": "-4"}


# ---------------------------------------------------------------- reduce-only is enforced by the venue; flat is the venue's own read for EVERY leg key (spot included)

def _ro(venue, lid, side, qty, cat="linear", sym="XUSDT", ro=True):
    req = EX.OrderRequest("gRO", "cand1", "spec1", T.LegPlan(lid, sym, cat, side, str(qty), "Market", None, False, ro and cat == "linear", "IOC"), f"gRO-{lid}")
    ack = venue.send_order(req)
    return ack, req


def check_reduce_only_never_flips():
    v = EX.SimVenue()
    ack, _ = _ro(v, "e1", "Sell", 10, ro=False)                                       # open a short of 10 (not reduce-only)
    v.execute("gRO-e1", 10, 100.0, 1_000)
    assert v.positions()["linear:XUSDT"] == -10
    ack, _ = _ro(v, "x1", "Buy", 25)                                                  # an exit of 2.5x the position
    assert ack.status == "ACCEPTED"
    f = v.execute("gRO-x1", 25, 100.0, 2_000, liquidity="TAKER")
    assert f is not None and f.qty == pytest.approx(10.0)                              # clamped to what is held
    assert v.positions()["linear:XUSDT"] == pytest.approx(0.0)                        # it did not flip long
    ack2, _ = _ro(v, "x2", "Buy", 5)                                                   # nothing left to reduce: refused, never a new long
    assert ack2.status == "REJECTED" and "REDUCE_ONLY" in (ack2.reason or "")
    assert v.positions()["linear:XUSDT"] == pytest.approx(0.0)
    # cumulative across exits: two exits that together exceed the position
    ack, _ = _ro(v, "e2", "Sell", 10, ro=False)
    v.execute("gRO-e2", 10, 100.0, 3_000)
    _ro(v, "x3", "Buy", 6)
    _ro(v, "x4", "Buy", 6)
    a = v.execute("gRO-x3", 6, 100.0, 4_000, liquidity="TAKER")
    b = v.execute("gRO-x4", 6, 100.0, 4_001, liquidity="TAKER")
    assert a.qty == pytest.approx(6.0) and b is not None and b.qty == pytest.approx(4.0)
    assert v.positions()["linear:XUSDT"] == pytest.approx(0.0)
    # a reduce-only SELL cannot flip a long short either
    _ro(v, "e3", "Buy", 3, ro=False)
    v.execute("gRO-e3", 3, 100.0, 5_000)
    _ro(v, "x5", "Sell", 9)
    c = v.execute("gRO-x5", 9, 100.0, 5_100, liquidity="TAKER")
    assert c.qty == pytest.approx(3.0) and v.positions()["linear:XUSDT"] == pytest.approx(0.0)


def test_simvenue_clamps_and_refuses_reduce_only_orders_so_a_position_never_flips():
    check_reduce_only_never_flips()


def test_mutation_a_venue_that_ignores_reduce_only_is_caught(monkeypatch):
    monkeypatch.setattr(EX.SimVenue, "_reducible", lambda self, category, symbol, side: 1e18)
    with pytest.raises(AssertionError):
        check_reduce_only_never_flips()


def test_the_simvenue_spot_balance_is_its_own_ledger_updated_by_matching_and_a_spot_sell_cannot_go_short():
    v = EX.SimVenue()
    _ro(v, "b1", "Buy", 4, cat="spot")
    v.execute("gRO-b1", 4, 100.0, 1_000)
    v.hidden_executions.add("gRO-b1:0")                                                # the executions READ hides it; the balance does not depend on that read's rows
    assert v.venue_state()["executions"] == [] and v.positions()["spot:XUSDT"] == pytest.approx(4.0)
    _ro(v, "s1", "Sell", 9, cat="spot")
    f = v.execute("gRO-s1", 9, 100.0, 2_000, liquidity="TAKER")
    assert f.qty == pytest.approx(4.0) and v.positions().get("spot:XUSDT", 0.0) == pytest.approx(0.0)       # spot has no short: only the held balance can be sold


def check_closed_needs_the_spot_leg_flat_on_the_venue_too():
    h = hedged(H())
    h.e.request_exit(GID, 5_000)
    h.v.position_override["spot:XUSDT"] = 4.0                                          # the venue's spot balance still shows 4 the fills say are gone
    for k, o in list(h.v.orders.items()):
        if "-x" in k:
            h.fill(k, o["qty"], 100.0, 5_100, liq="TAKER")
    h.e.step(6_000)
    assert h.state() != G.CLOSED
    assert any(r.startswith("POSITION_MISMATCH:spot:XUSDT") for r in h.e.halts) or any(a.get("code") == "NOT_FLAT_ON_VENUE" for a in h.e.anomalies)


def test_closed_requires_the_venue_to_read_the_spot_leg_flat_not_only_the_perp():
    check_closed_needs_the_spot_leg_flat_on_the_venue_too()


def test_mutation_skipping_spot_keys_in_the_flat_verification_is_caught(monkeypatch):
    real = EX.ExecutionEngine._read_venue

    def no_spot(self):
        v = real(self)
        if v and v.get("positions") is not None:
            v = dict(v)
            v["positions"] = {k: x for k, x in v["positions"].items() if not k.startswith("spot:")}
        return v
    monkeypatch.setattr(EX.ExecutionEngine, "_read_venue", no_spot)
    with pytest.raises(AssertionError):
        check_closed_needs_the_spot_leg_flat_on_the_venue_too()


def check_closed_needs_a_complete_read():
    h = hedged(H())
    h.e.request_exit(GID, 5_000)
    h.v.incomplete_view = True
    for k, o in list(h.v.orders.items()):
        if "-x" in k:
            h.fill(k, o["qty"], 100.0, 5_100, liq="TAKER")
    h.e.step(6_000)
    assert h.state() != G.CLOSED and not h.e.groups[GID].flat_verified               # an incomplete read cannot show a leg's key as flat
    h.v.incomplete_view = False
    h.e.step(7_000)
    assert h.state() == G.CLOSED                                                        # positive control: the complete read closes it


def test_closed_requires_a_complete_venue_read_for_every_leg_key():
    check_closed_needs_a_complete_read()


def test_mutation_ignoring_the_completeness_of_the_read_is_caught(monkeypatch):
    real = EX.ExecutionEngine._read_venue

    def always_complete(self):
        v = real(self)
        return dict(v, complete=True) if v else v
    monkeypatch.setattr(EX.ExecutionEngine, "_read_venue", always_complete)
    with pytest.raises(AssertionError):
        check_closed_needs_a_complete_read()


# ---------------------------------------------------------------- round-2 fixes

def test_an_incomplete_venue_read_does_not_lift_the_unknown_position_halt():
    h = H()
    h.v.positions_unknown = True
    h.e.reconcile(1000)
    assert "UNKNOWN_VENUE_POSITION" in h.e.halts
    h.v.positions_unknown = False
    h.v.incomplete_view = True
    h.e.reconcile(2000)
    assert "UNKNOWN_VENUE_POSITION" in h.e.halts                       # an incomplete read may miss rows: it cannot lift the halt
    h.v.incomplete_view = False
    h.e.reconcile(3000)
    assert "UNKNOWN_VENUE_POSITION" not in h.e.halts                   # positive control: a complete read does


@pytest.mark.parametrize("link,key", [(PL, "linear:XUSDT"), (SL, "spot:XUSDT")])
def test_a_recon_adopted_fill_is_not_double_counted_when_the_real_execution_arrives_later(link, key):
    h = H()
    h.start()
    f = h.v.execute(link, 10, 100.0, 3000)
    h.v.hidden_executions.add(f.fill_id)                               # executed at the venue, the execution is not (yet) visible
    h.e.reconcile(4000)
    leg = next(l for l in h.e.groups[GID].legs.values() if l.link == link)
    assert leg.filled == 10                                            # adopted from the order's cumulative quantity
    h.v.hidden_executions.clear()
    h.e.reconcile(5000)                                                # the real execution is now visible
    assert leg.filled <= leg.plan_qty and leg.filled == 10
    assert Decimal(h.e.positions()[key]) == Decimal(str(h.v.book[key]))
    h.e.reconcile(6000)
    assert leg.filled == 10 and not any(k.startswith("POSITION_MISMATCH") for k in h.e.halts)


def test_a_partial_real_execution_replaces_only_its_part_of_the_recon_fill():
    h = H()
    h.start()
    f = h.v.execute(PL, 10, 100.0, 3000)
    h.v.hidden_executions.add(f.fill_id)
    h.e.reconcile(4000)
    leg = h.e.groups[GID].legs[PERP]
    h.e.on_fill(T.Fill("real-1", GID, PERP, "XUSDT", "Sell", 4.0, 100.0, 0.0, "USDT", "MAKER", 4100, "SIMULATED"), 4100)
    assert leg.filled == 10 and h.e.positions()["linear:XUSDT"] == "-10"


def _exit_all(h, t=5_100):
    for k, o in list(h.v.orders.items()):
        if "-x" in k:
            h.fill(k, o["qty"], 100.0, t, liq="TAKER")


def test_preexisting_spot_inventory_does_not_block_a_perfectly_hedged_and_exited_group():
    h = H()
    h.v.book["spot:XUSDT"] = 3.0                                       # BTC the client already owned
    h.start()
    h.fill(PL, 10)
    h.fill(SL, 10)
    assert h.state() == G.HEDGED
    h.e.request_exit(GID, 5_000)
    _exit_all(h)
    h.e.step(6_000)
    assert h.state() == G.CLOSED and not h.e.halts
    assert h.v.book["spot:XUSDT"] == pytest.approx(3.0)


def test_a_real_residual_spot_delta_still_blocks_closed_with_inventory_present():
    h = H()
    h.v.book["spot:XUSDT"] = 3.0
    h.start()
    h.fill(PL, 10)
    h.fill(SL, 10)
    h.e.request_exit(GID, 5_000)
    h.v.position_override["spot:XUSDT"] = 5.0                          # 2 more than the inventory the venue should show after the exit
    _exit_all(h)
    h.e.step(6_000)
    assert h.state() != G.CLOSED
    assert any(a.get("code") == "NOT_FLAT_ON_VENUE" for a in h.e.anomalies)


def test_without_a_baseline_the_group_cannot_be_closed_by_delta_and_says_why():
    h = H()
    h.v.book["spot:XUSDT"] = 3.0
    h.v.incomplete_view = True                                         # the pre-submit read is incomplete: no baseline can be captured
    h.start()
    h.fill(PL, 10)
    h.fill(SL, 10)
    h.e.request_exit(GID, 5_000)
    _exit_all(h)
    h.v.incomplete_view = False
    h.e.step(6_000)
    assert h.state() == G.EXITING
    assert any(a.get("code") == "SPOT_BASELINE_MISSING" for a in h.e.anomalies)
