"""Residual execution risk (round 10, priority four): exits must never be wedged by a cause the VENUE did not make, and an order the venue already has must never be called a refusal.

Reproduced first by the round-10 execution specialist (scratch probes A6, A9) against this repository's own fake Testnet:

  A6  an unreadable public ticker made the executor refuse a REDUCE-ONLY perp exit locally (``NOTIONAL_UNVERIFIABLE``: a linear contract has a minimum notional and the reference price was unknown). Nothing was sent,
      yet every refusal counted toward ``max_exit_attempts`` and five of them HALTED the group with the perp still short.
  A9  the spot duplicate-link answer 170141 (seen on a real testnet rejection, per the client's own comment) was a definitive REJECTION while the order existed and was Filled.

What is NOT changed here, on purpose: spot sales stay owner-only after an unanswered exit (a duplicate could sell the OWNER's own coin); no entry is ever opened on an unknown outcome; the same-link bound for a reduce-only perp
exit is documented in docs/MONEY_MISSION.md as SAFE-WITH-CONDITIONS and awaits the owner's four venue checks before any code relaxes ``max_resends``.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (HERE, BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ms_execution as EX  # noqa: E402
import ms_testnet_executor as TE  # noqa: E402
from ms_testnet_world import World, begin  # noqa: E402


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    world.ready("BTCUSDT", 180.0)                    # 0.003 BTC of each leg
    return world


def drive(w, run, n=6, dt=3):
    for _ in range(n):
        w.clock.sleep(dt)
        now = w.clock.now()
        run.engine.step(now, market_ts_ms=now, account_ts_ms=now)
        run.bookkeep()
    return run.state()


def creates(w):
    return [json.loads(r[3]) for r in w.fake.requests if r[0] == "POST" and r[1] == "/v5/order/create"]


def venue_perp(w):
    return {s: str(p["size"]) for s, p in w.fake.positions.items() if p["size"] != 0}


# ------------------------------------------------------------------------------------------------ A6: a risk-reducing exit is never blocked by a price the executor could not read
def test_an_unreadable_ticker_does_not_block_a_reduce_only_exit_and_the_venue_ends_flat(w):
    run, ctx, rec = begin(w)
    assert drive(w, run, 2) == "HEDGED"
    w.fake.inject("/v5/market/tickers", "timeout", times=500, match=lambda d: d.get("category") == "linear")
    run.engine.request_exit(run.gid, w.clock.now(), "T")
    drive(w, run, 6)
    g = run.engine.groups[run.gid]
    sent = [c for c in creates(w) if c.get("reduceOnly")]
    assert sent, "the reduce-only exit must reach the venue: it can only reduce exposure, and the venue enforces its own minimum"
    assert g.halted_reason != "EXIT_ORDERS_REJECTED"
    assert venue_perp(w) == {}, "the perp is closed"


def test_an_unreadable_ticker_still_blocks_an_ENTRY_it_cannot_price(w):
    """the relaxation is for reduce-only legs only: an order that would OPEN exposure with an unverifiable notional stays refused"""
    D = TE.D
    f = TE.Filters("linear", "BTCUSDT", "Trading", "BTC", D("0.1"), D("0.001"), D("0.001"), D("100"), D("5"), 480)
    assert "NOTIONAL_UNVERIFIABLE" in TE.filter_violations(f, side="Sell", order_type="Market", qty="0.002", price=None, ref_price=None)
    assert "NOTIONAL_UNVERIFIABLE" not in TE.filter_violations(f, side="Buy", order_type="Market", qty="0.002", price=None, ref_price=None, reduce_only=True)
    assert "NOTIONAL_BELOW_MIN" in TE.filter_violations(f, side="Buy", order_type="Market", qty="0.001", price=None, ref_price=D("1000"), reduce_only=True), "a KNOWN price below the minimum is still refused"


def test_a_failure_to_READ_the_instrument_rules_is_not_a_rejection_the_halt_counts(w):
    """FILTERS_UNAVAILABLE says the executor could not look something up; nothing was refused by the venue. It must not count toward the exit-attempt halt, and it must not be retried inside one step."""
    run, ctx, rec = begin(w)
    assert drive(w, run, 2) == "HEDGED"
    w.fake.inject("/v5/market/instruments-info", "timeout", times=500, match=lambda d: d.get("category") == "linear")
    run.ex._filters.clear()                                           # the executor must look the rules up again (cached rules would hide the outage)
    run.engine.request_exit(run.gid, w.clock.now(), "T")
    drive(w, run, 8)
    g = run.engine.groups[run.gid]
    refusals = [l for l in g.exit_legs() if l.status == EX.REJECTED and l.reject_reason.startswith("FILTERS_UNAVAILABLE")]
    assert g.halted_reason != "EXIT_ORDERS_REJECTED", [(l.plan.leg_id, l.reject_reason[:40]) for l in g.exit_legs()]
    w.fake.faults.clear()
    drive(w, run, 6)
    assert venue_perp(w) == {}, "once the read works again the exit goes through and the venue is flat"
    assert refusals, "the refusals did happen (the test is not vacuous)"


# ------------------------------------------------------------------------------------------------ A9: an order the venue already has is not a refusal
def test_a_spot_duplicate_link_answer_adopts_the_existing_order(w):
    run, ctx, rec = begin(w, submit=False)
    leg = run.group_plan.legs[1]                                      # the spot BUY
    req = EX.OrderRequest(run.gid, "c", "d", leg, f"{run.gid}-{leg.leg_id}")
    first = run.ex.send_order(req)
    assert first.status == "ACCEPTED"
    orig = w.fake._create

    def create(d):
        if d.get("category") == "spot" and d.get("orderLinkId") in w.fake.by_link:
            return w.fake._err(170141, "Duplicate clientOrderId")      # what a real testnet answered for a spot duplicate (bybit_connection.py)
        return orig(d)
    w.fake._create = create
    again = run.ex.send_order(req)                                     # the engine's idempotent resend of the SAME link
    assert again.status == "ACCEPTED" and again.order_id == first.order_id and again.reason == "ADOPTED_BY_LOOKUP", again


def test_170130_stays_a_definitive_refusal_because_it_is_also_an_invalid_parameter(w):
    """a code that can mean 'invalid parameter' must not be turned into 'the order exists': with no order under the link the lookup would say UNKNOWN and the resend would loop"""
    run, ctx, rec = begin(w, submit=False)
    leg = run.group_plan.legs[1]
    req = EX.OrderRequest(run.gid, "c", "d", leg, f"{run.gid}-{leg.leg_id}")
    w.fake._create = lambda d: w.fake._err(170130, "Data sent for parameter 'qty' is not valid.")
    ack = run.ex.send_order(req)
    assert ack.status == "REJECTED" and "170130" in ack.reason
