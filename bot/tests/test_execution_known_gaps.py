"""Known gaps in the Testnet execution lifecycle, from an independent adversarial integration review, reproduced on the fake venue with the repo's own World harness.

Each test states the DESIRED behaviour. The ones that still FAIL are strict xfails: they document the open defect precisely, and the moment a fix lands the xfail goes red (XPASS strict) and
the mark must be deleted, so this table can only get shorter. They are low-probability on BTC at the sizes the autopilot uses (an IOC market order partially filling; two creates of one leg failing
in a row) but they decide whether an UNATTENDED session or the owner's `reconcile --unwind` ends flat, so they are stated, not forgotten:

  R2 / R2b  a partial perp ENTRY fill leaves a naked spot long that neither the session nor the owner's unwind closes
  R3        a partial perp EXIT fill halts the group with the hedge still open (the owner's unwind does succeed)
  R6        two failed creates of the second (spot) leg leave a naked perp short that only the owner's unwind closes
  R5, R1    (fixed here: a lost first ack, and an order the venue says filled with no execution rows)
"""
from __future__ import annotations

import json
import os
import sys
from decimal import Decimal as D

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (HERE, BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest

import ms_capital as CC
import ms_testnet_autopilot as AP
import ms_testnet_session as S
from ms_testnet_world import World
from test_ms_testnet_autopilot import _profit_world


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def cycle(w, eng="eng", sleep=None):
    got = []
    res = AP.run_once(w.env, inner=w.fake, clock=w.clock.now, sleep=sleep or w.clock.sleep, out=[].append, state_root=str(w.tmp / "auto"), engine_root=str(w.tmp / eng),
                      on_event=lambda k, s, p, n=0: got.append((k, s, dict(p), n)))
    return res, got


def held_funding(w, rate="0.0080"):
    start, st, real = w.clock.now(), {"d": False}, w.clock.sleep

    def sleep(s):
        real(s)
        if not st["d"] and w.clock.now() >= start + 600_000 and any(p["size"] != 0 for p in w.fake.positions.values()):
            w.fake.settle_funding("BTCUSDT", rate, ts_ms=w.clock.now())
            st["d"] = True
    return sleep


def limit_first(w, category, side, reduce, avail):
    """only the FIRST matching order meets thin liquidity (the venue fills ``avail`` and cancels the rest of an IOC)."""
    orig, n = w.fake._create, {"n": 0}

    def create(d):
        hit = d.get("category") == category and d.get("side") == side and bool(d.get("reduceOnly")) == reduce
        if hit and n["n"] == 0:
            n["n"] = 1
            w.fake.liquidity[d["symbol"]] = D(avail)
        else:
            w.fake.liquidity.pop(d["symbol"], None)
        return orig(d)
    w.fake._create = create


def venue_truth(w):
    perp = {s: str(p["size"]) for s, p in w.fake.positions.items() if p["size"] != 0}
    spot = {c: str(v) for c, v in w.fake.wallet.items() if c != "USDT" and v != 0}
    return perp, spot


# R1 ------------------------------------------------------------------------------------------------------------------------------------------------
def test_R1_a_filled_order_whose_executions_the_venue_list_omits_must_not_publish_a_known_net_or_cost_complete(w, monkeypatch):
    _profit_world(w)
    orig = w.fake._fill

    def hide(o, qty, px, *, taker, dup=False):
        orig(o, qty, px, taker=taker, dup=dup)
        if o["orderLinkId"].endswith("-p"):
            w.fake.hidden_execs.add(w.fake.execs[-1]["execId"])        # the perp ENTRY fill: order row says Filled, /v5/execution/list does not list it
    w.fake._fill = hide
    seen = {}
    orig_snap = S.Run.snapshot_record

    def snap(self, rec, final=False):
        out = orig_snap(self, rec, final)
        if final:
            seen["status"] = self.ledger.pnl()["status"]
        return out
    monkeypatch.setattr(S.Run, "snapshot_record", snap)
    res, _ = cycle(w)
    rec = json.load(open(os.path.join(res["state_dir"], "testnet_session.json")))
    q = json.load(open(os.path.join(res["state_dir"], "execution_quality.json")))["group"]
    assert seen["status"] == "UNRECONCILED"                              # the ledger itself says it cannot stand behind the numbers ...
    assert rec["net"]["fees_usd"] is None and rec["net"]["price_pnl_usd"] is None, f"... yet the session record publishes net {rec['net']} (true fees 0.396)"
    assert q["cost_complete"] is False and q["unknown"], f"... and the quality record says cost_complete={q['cost_complete']} unknown={q['unknown']} total_cost={q['total_execution_cost_usd']} (true 0.402)"


# R2 / R3 ------------------------------------------------------------------------------------------------------------------------------------------
@pytest.mark.xfail(strict=True, reason="known gap (see the module docstring)")
def test_R2_a_partial_perp_entry_fill_must_unwind_to_flat_in_the_session_and_in_ONE_owner_unwind(w):
    _profit_world(w)
    limit_first(w, "linear", "Sell", False, "0.0015")                   # the perp short fills 0.001 of 0.003 (IOC), the spot buy fills in full
    res, _ = cycle(w, sleep=held_funding(w))
    perp, spot = venue_truth(w)
    assert res["result"] == "DONE", f"session ended {res['result']}: the venue holds perp {perp} spot {spot} (naked spot long); journal: 5 spot exits REJECTED SPOT_EXIT_HELD_PERP_NOT_CLOSED then HALT EXIT_ORDERS_REJECTED"


@pytest.mark.xfail(strict=True, reason="known gap (see the module docstring)")
def test_R2b_the_owner_unwind_after_a_partial_perp_entry_sends_an_exit(w):
    _profit_world(w)
    limit_first(w, "linear", "Sell", False, "0.0015")
    res, _ = cycle(w, sleep=held_funding(w))
    d = res["state_dir"]
    plan = json.load(open(os.path.join(d, "plan.json")))
    posts0 = len([r for r in w.fake.requests if r[0] == "POST"])
    out = []
    rc = S.main(["--state-dir", d, "reconcile", "--unwind", "--confirm-plan-digest", plan["plan_digest"]], env=w.env, inner_transport=w.fake, clock_ms=w.clock.now, sleep=w.clock.sleep,
                input_fn=lambda _p: "", out=out.append)
    perp, spot = venue_truth(w)
    assert rc == 0, f"owner `reconcile --unwind` rc={rc}, {len([r for r in w.fake.requests if r[0] == 'POST']) - posts0} orders sent, venue still holds perp {perp} spot {spot}: {out[-1][:160]}"


@pytest.mark.xfail(strict=True, reason="known gap (see the module docstring)")
def test_R3_a_partial_perp_exit_fill_must_not_halt_the_group_with_an_open_hedge(w):
    _profit_world(w)
    limit_first(w, "linear", "Buy", True, "0.001")                      # the first reduce-only perp exit fills 0.001 of 0.003
    res, _ = cycle(w, sleep=held_funding(w))
    perp, spot = venue_truth(w)
    assert res["result"] == "DONE", f"session ended {res['result']}: perp {perp} spot {spot} remain (spot exit held 5x20 s, counted as 5 rejections -> HALT EXIT_ORDERS_REJECTED)"


# R4 ------------------------------------------------------------------------------------------------------------------------------------------------

def test_R5_a_timed_out_first_leg_must_not_leave_a_one_legged_group_HEDGED_and_held_across_the_settlement(w):
    """the perp create times out once BEFORE reaching the venue: submit_group stops its sequence (the spot buy is never sent), the idempotent resend later fills the perp alone and the engine
    derives HEDGED from the one leg it knows. The session then HOLDS A NAKED PERP SHORT across the settlement and books the funding as the carry's realised result."""
    _profit_world(w)
    w.fake.inject("/v5/order/create", "timeout", times=1, match=lambda d: d.get("category") == "linear" and d.get("side") == "Sell")
    res, got = cycle(w, sleep=held_funding(w))
    spot_orders = [r for r in w.fake.requests if r[0] == "POST" and r[1] == "/v5/order/create" and json.loads(r[3]).get("category") == "spot"]
    rec = json.load(open(os.path.join(res["state_dir"], "testnet_session.json")))
    assert spot_orders or (rec["net"]["funding_usd"] in (None, 0.0)), (f"a NAKED perp short was held across the settlement ({len(spot_orders)} spot orders were ever sent) and reported {res['result']} with funding "
                                                                       f"{rec['net']['funding_usd']} as the carry's result")


@pytest.mark.xfail(strict=True, reason="known gap (see the module docstring)")
def test_R6_two_failed_creates_of_the_second_leg_must_not_leave_a_naked_perp_short(w):
    _profit_world(w)
    w.fake.inject("/v5/order/create", "rate_limit", times=2, match=lambda d: d.get("category") == "spot" and d.get("side") == "Buy")
    res, _ = cycle(w, sleep=held_funding(w))
    perp, spot = venue_truth(w)
    assert res["result"] == "DONE", f"session ended {res['result']}: the venue holds a NAKED perp {perp} (spot {spot}); the unresolved spot entry leg wedges the group: no exit is ever sent"
