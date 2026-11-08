"""The REAL integrated path (config -> build_bot -> TradingBot.tick -> CarryEngine -> LedgerBroker -> CarryBroker -> BybitClient -> SyntheticVenue) under the PRODUCTION entry authority.
Proves, on the venue's own state: with no approved policy an entry places zero orders and leaves the book flat and restartable; with an approved policy and measured inputs the same
entry proceeds through the whole lifecycle. Modelled behaviour only (SyntheticVenue is not the exchange)."""
from __future__ import annotations

import dataclasses
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (BOT, HERE, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import entry_authority as EA  # noqa: E402
from main import _entry_hedge_read  # noqa: E402
import risk_policy as RP  # noqa: E402
from carry_engine import BookState  # noqa: E402
from test_testnet_lifecycle_offline import CREATE, B, H8, World, clock, no_live_ack, no_network, world  # noqa: E402,F401
from test_entry_authority import GOOD, POLICY  # noqa: E402

pytestmark = pytest.mark.real_entry_authority


def approve(w: World, tmp_path, *, state=GOOD):
    """Give the world's REAL production authority an approved policy file and a measured state (the seams the owner/measurement providers will fill)."""
    path = tmp_path / "approved_policy.json"
    path.write_text(json.dumps(dataclasses.asdict(POLICY)))
    w.bot.client.entry_authority = EA.build_for_book(
        venue="testnet", store=w.store, state_dir=os.path.dirname(w.path), max_notional_usd=102.0,
        mark_provider=lambda s: 50_000.0, policy_path=str(path), state_provider=lambda: state, require_risk_policy=True,
        perp_symbol=w.bot.carry.perp_symbol, spot_symbol=w.bot.carry.spot_symbol, hedge_reader=lambda: _entry_hedge_read(w.bot))


def test_production_wiring_refuses_every_entry_without_an_approved_policy_and_leaves_the_book_clean(world):
    w = world(mode="overlay")
    w.prime()
    assert isinstance(w.bot.client.entry_authority, EA.EntryAuthority)       # installed by build_bot, not by the test
    d = w.tick()
    v = w.venue
    assert v.requests_by_path[CREATE] == 0 and v.orders == {} and v.executions == [] and v.net_position() == 0.0     # NOTHING reached the venue
    assert v.wallet == {"USDT": 10_000.0, "BTC": 0.05}
    assert w.bot.carry.state is not BookState.HEDGED
    reasons = [r for _l, rs in w.bot.client.entry_authority.refusals for r in rs]
    assert any("NO_APPROVED_POLICY" in r for r in reasons), (d, reasons)


def test_a_restart_after_such_a_refusal_boots_clean_and_places_no_order(world):
    w = world(mode="overlay")
    w.prime(); w.tick()
    w.boot()                                                       # process restart on the same state
    assert w.started is True                                       # a refused entry leaves nothing unresolved: the restart is clean
    w.tick()
    assert w.venue.requests_by_path[CREATE] == 0 and w.venue.net_position() == 0.0


def test_with_an_approved_policy_and_measured_inputs_the_same_entry_runs_the_full_lifecycle(world, tmp_path):
    w = world(mode="overlay")
    w.prime()
    approve(w, tmp_path)
    d = w.tick()
    v = w.venue
    assert d["action"] in ("opened", "hedged", "open", "entered") or v.requests_by_path[CREATE] >= 1, d
    assert v.requests_by_path[CREATE] == 1 and v.net_position() < 0                     # overlay: ONE perp short, hedged by the BTC already held
    assert w.bot.carry.state is BookState.HEDGED
    # the reservation is durable: it is on disk beside the state database, naming the order
    led = os.path.join(os.path.dirname(w.path), "entry_reservations.jsonl")
    rows = [json.loads(x) for x in open(led).read().splitlines() if x.strip()]
    assert len(rows) == 1 and rows[0]["side"] == "Sell" and rows[0]["category"] == "linear"


def test_a_breach_suspends_new_exposure_and_latches_but_a_reduce_only_exit_still_reaches_the_venue(world, tmp_path):
    import bybit_connection as bc
    w = world(mode="overlay")
    w.prime(); approve(w, tmp_path)
    w.tick()
    assert w.bot.carry.state is BookState.HEDGED and w.venue.requests_by_path[CREATE] == 1
    held = abs(w.venue.net_position())
    approve(w, tmp_path, state=dataclasses.replace(GOOD, loss_today_usd=99.0))                  # a loss limit is now breached
    new_entry = {"category": "linear", "symbol": "BTCUSDT", "side": "Sell", "orderType": "Market", "qty": "0.001", "orderLinkId": "AFTERBREACH1"}
    with pytest.raises(bc.PermanentAPIError, match="BREACH:daily_loss_usd"):
        w.bot.client._request("POST", CREATE, body=new_entry, signed=True)
    latch = RP.RiskLatch(os.path.join(os.path.dirname(w.path), "risk_latch.json"))
    assert latch.is_suspended("btcusdt_funding_carry_overlay")[0] is True
    assert w.venue.requests_by_path[CREATE] == 1 and abs(w.venue.net_position()) == held           # the refused entry added nothing
    # a clean state does NOT lift the latch (a restart-equivalent check) ...
    approve(w, tmp_path)
    with pytest.raises(bc.PermanentAPIError, match="LATCHED"):
        w.bot.client._request("POST", CREATE, body={**new_entry, "orderLinkId": "AFTERBREACH2"}, signed=True)
    # ... but the exit is never gated: a reduce-only buy of the held short goes through and the venue is flat
    qty = f"{held:.3f}"
    w.bot.client._request("POST", CREATE, body={"category": "linear", "symbol": "BTCUSDT", "side": "Buy", "orderType": "Market", "qty": qty, "reduceOnly": True,
                                                "orderLinkId": "EXITAFTERBREACH"}, signed=True)
    assert w.venue.requests_by_path[CREATE] == 2 and abs(w.venue.net_position()) < 1e-9
