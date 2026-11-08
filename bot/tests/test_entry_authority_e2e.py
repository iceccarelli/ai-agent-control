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
        mark_provider=lambda s, c=None: 50_000.0, policy_path=str(path), state_provider=lambda: state, require_risk_policy=True,
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


# ---- the authority's mark: fresh and plausible, or unknown (round 3) ---------------------------------------------------------------------------------------------

class _Snap:
    def __init__(self, perp=100_000.0, spot=100_010.0, at=1_000.0):
        self.perp_mark, self.spot_mark, self.observed_at_s = perp, spot, at


class _Carry:
    perp_symbol = "BTCUSDT"
    spot_symbol = "BTCUSDT"

    def __init__(self, snapshot):
        self.snapshot = snapshot


class _Bot:
    def __init__(self, snapshot):
        self.carry = _Carry(snapshot)


def _mark(snap, symbol="BTCUSDT", now=1_005.0):
    from main import _entry_mark
    return _entry_mark(_Bot(snap), symbol, now_s=lambda: now, category="linear")


def test_entry_mark_positive_control_a_fresh_plausible_snapshot_gives_the_mark():
    assert _mark(_Snap()) == 100_000.0


@pytest.mark.parametrize("snap", [
    _Snap(at=1_000.0 - 3600),                       # an hour old
    _Snap(at=1_000.0 - 31),                         # just past the limit
    _Snap(at=1_005.0 + 60),                         # stamped in the future: a broken clock
    _Snap(at=None), _Snap(at="x"), _Snap(at=float("nan")), _Snap(at=True),
    _Snap(perp=1.0), _Snap(spot=1.0), _Snap(perp=100_000.0, spot=120_000.0), _Snap(perp=100_000.0, spot=80_000.0),   # a leg far from the other
    _Snap(perp=0.0), _Snap(perp=-5.0), _Snap(perp=float("nan")), _Snap(perp=float("inf")), _Snap(perp=None), _Snap(spot=None), _Snap(perp=True), _Snap(spot="100000"),
])
def test_entry_mark_is_unknown_for_a_stale_future_or_implausible_snapshot(snap):
    assert _mark(snap) is None


def test_entry_mark_is_unknown_for_a_symbol_that_is_not_a_carry_leg_or_without_a_snapshot():
    assert _mark(_Snap(), symbol="DOGEUSDT") is None
    assert _mark(None) is None


def test_entry_mark_uses_the_injected_clock_not_the_wall_clock():
    assert _mark(_Snap(at=1_000.0), now=1_029.0) == 100_000.0
    assert _mark(_Snap(at=1_000.0), now=1_031.0) is None


def test_a_stale_snapshot_makes_a_market_entry_unsizeable_and_refused(tmp_path):
    from main import _entry_mark
    bot = _Bot(_Snap(at=1_000.0 - 3600))
    a = EA.build_for_book(venue="testnet", store=type("S", (), {"is_kill_switch_engaged": staticmethod(lambda: (False, ""))})(), state_dir=str(tmp_path / "s"),
                          max_notional_usd=102.0, mark_provider=lambda s, c=None: _entry_mark(bot, s, now_s=lambda: 1_005.0), require_risk_policy=False,
                          perp_symbol="BTCUSDT", spot_symbol="BTCUSDT")
    with pytest.raises(EA.EntryRefused) as e:
        a.authorize({"category": "linear", "symbol": "BTCUSDT", "side": "Sell", "orderType": "Market", "qty": "0.001", "orderLinkId": "S1"}, venue="testnet")
    assert "NO_PRICE_FOR_NOTIONAL" in e.value.reasons


# ---- round 4: both legs are named BTCUSDT in production; the order's category picks the mark

def _mark_cat(snap, category, symbol="BTCUSDT"):
    from main import _entry_mark
    return _entry_mark(_Bot(snap), symbol, now_s=lambda: 1_005.0, category=category)


def test_entry_mark_is_category_aware_when_both_legs_share_a_symbol():
    s = _Snap(perp=100_000.0, spot=104_000.0)
    assert _mark_cat(s, "linear") == 100_000.0                      # positive control: perp leg
    assert _mark_cat(s, "spot") == 104_000.0
    assert _mark_cat(s, None) == 104_000.0                          # unknown category: the conservative (larger) mark
    assert _mark_cat(s, "option") is None


def test_a_spot_buy_is_sized_at_the_spot_mark_not_the_perp_mark(tmp_path):
    from main import _entry_mark
    bot = _Bot(_Snap(perp=100_000.0, spot=104_000.0))
    a = EA.build_for_book(venue="testnet", store=type("S", (), {"is_kill_switch_engaged": staticmethod(lambda: (False, ""))})(), state_dir=str(tmp_path / "s"),
                          max_notional_usd=102.0, mark_provider=lambda s, c=None: _entry_mark(bot, s, now_s=lambda: 1_005.0, category=c), require_risk_policy=False,
                          perp_symbol="BTCUSDT", spot_symbol="BTCUSDT")
    qty = "0.00101"                                                 # 0.00101*100000 = 101 <= 102 < 0.00101*104000 = 105.04
    perp = {"category": "linear", "symbol": "BTCUSDT", "side": "Sell", "orderType": "Market", "qty": qty, "orderLinkId": "P1"}
    a.authorize(perp, venue="testnet")                              # positive control: within the cap at the perp mark
    with pytest.raises(EA.EntryRefused) as e:
        a.authorize({"category": "spot", "symbol": "BTCUSDT", "side": "Buy", "orderType": "Market", "qty": qty, "orderLinkId": "S1"}, venue="testnet")
    assert "NOTIONAL_ABOVE_CAP" in e.value.reasons


@pytest.mark.parametrize("field,val", [("qty", "1_0"), ("qty", "١٠"), ("qty", "1e1"), ("qty", " 1"), ("price", "1_0"), ("price", "١٠.5"), ("qty", 0.001)])
def test_opening_order_numbers_must_be_plain_ascii_decimal_strings(field, val):
    body = {"category": "linear", "symbol": "BTCUSDT", "side": "Sell", "orderType": "Limit", "qty": "0.001", "price": "100000", "orderLinkId": "N1"}
    EA.intent_from_body(body)                                       # positive control
    body[field] = val
    with pytest.raises(EA.EntryRefused) as e:
        EA.intent_from_body(body)
    assert f"ORDER_{field.upper()}_NOT_PLAIN_DECIMAL" in e.value.reasons


def test_entry_mark_category_must_match_the_leg_that_carries_the_symbol():
    from main import _entry_mark
    bot = _Bot(_Snap(perp=100_000.0, spot=104_000.0))
    bot.carry.perp_symbol, bot.carry.spot_symbol = "BTCUSDT", "ETHUSDT"      # instance override: distinct legs
    mk = lambda sym, cat: _entry_mark(bot, sym, now_s=lambda: 1_005.0, category=cat)   # noqa: E731
    assert mk("BTCUSDT", "linear") == 100_000.0 and mk("ETHUSDT", "spot") == 104_000.0   # positive controls
    assert mk("ETHUSDT", "linear") is None and mk("BTCUSDT", "spot") is None
