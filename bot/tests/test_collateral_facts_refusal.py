"""The overlay does not open a short against BTC the venue does not say is collateral.

Offline only: documented / captured wallet schemas and test doubles. No network,
no account, no credential."""
from __future__ import annotations

import ast
import dataclasses
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for p in (BOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import margin_facts as MF                                       # noqa: E402
from carry_broker import CarryBroker                            # noqa: E402
from test_carry_broker import FakeClient, envelope              # noqa: E402
from test_carry_cold_start import H8, MARK, Venue               # noqa: E402

#: The shape of a BTC row in the tracked, credential-redacted testnet capture.
REAL_ROW = {"accruedInterest": "0", "availableToBorrow": "", "bonus": "0",
            "borrowAmount": "0.000000000000000000", "coin": "BTC", "colRes": "-1",
            "collateralSwitch": True, "cumRealisedPnl": "0", "equity": "0.10472857",
            "locked": "0", "marginCollateral": True, "spotBorrow": "0",
            "spotHedgingQty": "0", "totalOrderIM": "0", "totalPositionIM": "0",
            "totalPositionMM": "0", "unrealisedPnl": "0", "usdValue": "8691.03",
            "walletBalance": "0.10472857"}


def wallet(row):
    return [{"accountMMRate": "0", "coin": [dict(row)]}]


# -- the pure reading ---------------------------------------------------------------

def test_the_tracked_testnet_capture_has_exactly_the_documented_fields():
    """Ties the parser to something the venue really sent, not to our own guess."""
    path = os.path.join(BOT, "artifacts", "testnet_conformance_evidence.jsonl")
    seen = 0
    for line in open(path):
        rec = json.loads(line)
        if "/v5/account/wallet-balance" not in str(rec.get("request_url")):
            continue
        for acct in rec["response"]["result"]["list"]:
            row = MF.find_coin_row([acct], "BTC")
            assert row is not None
            assert MF.collateral_status(row, "BTC")["status"] == MF.OK
            assert "collateralRatio" not in row and "collateral_ratio" not in row
            seen += 1
    assert seen >= 1


def test_a_healthy_row_is_ok_and_the_ratio_is_never_a_number():
    s = MF.collateral_status(REAL_ROW, "BTC")
    assert (s["status"], s["reason"]) == (MF.OK, "")
    assert s["collateral_ratio"] is None
    assert s["collateral_ratio_status"].startswith("UNAVAILABLE")
    assert s["col_res"] == "-1"            # reported, not interpreted


def test_a_switch_that_is_off_is_not_collateral():
    s = MF.collateral_status(dict(REAL_ROW, collateralSwitch=False), "BTC")
    assert (s["status"], s["reason"]) == (MF.NOT_COLLATERAL, MF.COLLATERAL_SWITCH_OFF)


def test_a_coin_the_platform_disallows_is_not_collateral_whatever_the_switch_says():
    s = MF.collateral_status(dict(REAL_ROW, marginCollateral=False), "BTC")
    assert (s["status"], s["reason"]) == (MF.NOT_COLLATERAL, MF.PLATFORM_DISALLOWS_COLLATERAL)


@pytest.mark.parametrize("field", ["marginCollateral", "collateralSwitch"])
def test_an_absent_field_is_insufficient_risk_data_never_ok(field):
    row = {k: v for k, v in REAL_ROW.items() if k != field}
    s = MF.collateral_status(row, "BTC")
    assert s["status"] == MF.INSUFFICIENT_RISK_DATA
    assert (s["reason"], s["field"]) == (MF.COLLATERAL_FIELD_ABSENT, field)


@pytest.mark.parametrize("bad", [None, "", "yes", 1, 0, "ON", [], {}])
@pytest.mark.parametrize("field", ["marginCollateral", "collateralSwitch"])
def test_a_malformed_field_is_insufficient_risk_data(field, bad):
    s = MF.collateral_status(dict(REAL_ROW, **{field: bad}), "BTC")
    assert s["status"] == MF.INSUFFICIENT_RISK_DATA
    assert s["reason"] == MF.COLLATERAL_FIELD_UNREADABLE


def test_stringified_booleans_are_read_and_nothing_else_is():
    ok = MF.collateral_status(dict(REAL_ROW, marginCollateral="true",
                                   collateralSwitch="TRUE"), "BTC")
    assert ok["status"] == MF.OK
    off = MF.collateral_status(dict(REAL_ROW, collateralSwitch="false"), "BTC")
    assert off["reason"] == MF.COLLATERAL_SWITCH_OFF


def test_the_switch_is_not_consulted_when_the_platform_disallows():
    """collateralSwitch is only meaningful when marginCollateral is true."""
    row = dict(REAL_ROW, marginCollateral=False)
    row.pop("collateralSwitch")
    assert MF.collateral_status(row, "BTC")["status"] == MF.NOT_COLLATERAL


@pytest.mark.parametrize("row", [None, [], "x", 5])
def test_a_missing_row_is_insufficient_risk_data(row):
    assert MF.collateral_status(row, "BTC")["reason"] == MF.COIN_ROW_MISSING


def test_find_coin_row_reads_the_named_coin_and_nothing_near_it():
    rows = [{"coin": [{"coin": "USDT", "marginCollateral": True},
                      dict(REAL_ROW)]}]
    assert MF.find_coin_row(rows, "btc")["coin"] == "BTC"
    assert MF.find_coin_row(rows, "ETH") is None
    assert MF.find_coin_row([], "BTC") is None
    assert MF.find_coin_row([{"coin": None}], "BTC") is None
    assert MF.find_coin_row(None, "BTC") is None


# -- the broker -----------------------------------------------------------------------

def broker_for(rows):
    client = FakeClient(mark=MARK)
    real = client._request

    def request(method, endpoint, **kw):
        if endpoint == "/v5/account/wallet-balance":
            return envelope({"list": rows})
        return real(method, endpoint, **kw)
    client._request = request
    return CarryBroker(client=client, order_gate=lambda: (True, "T"),
                       sequence_source=lambda *a: 1)


def test_the_broker_reports_the_venues_collateral_state_for_the_held_coin():
    assert broker_for(wallet(REAL_ROW)).get_collateral_facts("BTCUSDT")["status"] == MF.OK
    off = broker_for(wallet(dict(REAL_ROW, collateralSwitch=False)))
    assert off.get_collateral_facts("BTCUSDT")["reason"] == MF.COLLATERAL_SWITCH_OFF


def test_the_broker_says_insufficient_risk_data_for_a_row_it_cannot_find():
    b = broker_for([{"accountMMRate": "0", "coin": [{"coin": "USDT"}]}])
    assert b.get_collateral_facts("BTCUSDT")["reason"] == MF.COIN_ROW_MISSING


def test_an_unreadable_wallet_raises_rather_than_reporting_a_state():
    from carry_broker import PairIncident
    client = FakeClient(mark=MARK)
    client._request = lambda *a, **k: envelope({})            # no `list`
    b = CarryBroker(client=client, order_gate=lambda: (True, "T"),
                    sequence_source=lambda *a: 1)
    with pytest.raises(PairIncident):
        b.get_collateral_facts("BTCUSDT")


# -- the engine -------------------------------------------------------------------------

class V(Venue):
    """A venue double that can state collateral facts."""
    facts = {"status": "OK", "reason": ""}
    raises = None

    def get_collateral_facts(self, symbol):
        if self.raises:
            raise self.raises
        return self.facts


def engine(venue, *, mode="overlay", flag=True):
    import time
    from carry_engine import CarryEngine
    from carry_risk import CarryRisk
    from market_snapshot import MarketSnapshot
    e = CarryEngine(broker=venue, max_notional_usd=100.0, borrow_apr=0.0,
                    execution_mode=mode, persist=lambda s: None,
                    require_collateral_facts=flag)
    e.pair_risk = CarryRisk(max_notional_usd=100.0)
    e.snapshot = MarketSnapshot(perp_mark=MARK, spot_mark=MARK, funding_bps=3.0,
                                margin_multiple=5.0, observed_at_s=time.time())
    for k in (1, 2):
        e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK, funding_print_ms=k * H8)
    return e


def tick(e):
    return e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                       funding_print_ms=3 * H8, timestamp_ms=3 * H8)


def test_an_overlay_opens_when_the_venue_says_the_held_coin_is_collateral():
    v = V()
    d = tick(engine(v))
    assert d.action == "opened", d.reason
    assert v.orders and v.orders[0][0] == "Sell"


@pytest.mark.parametrize("facts,reason", [
    ({"status": "NOT_COLLATERAL", "reason": "COLLATERAL_SWITCH_OFF"}, "HELD_COIN_NOT_COLLATERAL"),
    ({"status": "NOT_COLLATERAL", "reason": "PLATFORM_DISALLOWS_COLLATERAL"}, "HELD_COIN_NOT_COLLATERAL"),
    ({"status": "INSUFFICIENT_RISK_DATA", "reason": "COLLATERAL_FIELD_ABSENT"}, "INSUFFICIENT_RISK_DATA"),
    ({"status": "SOMETHING_NEW", "reason": ""}, "HELD_COIN_NOT_COLLATERAL"),
])
def test_anything_but_ok_is_a_named_refusal_with_no_order(facts, reason):
    v = V()
    v.facts = facts
    d = tick(engine(v))
    assert d.action == "stand_aside" and d.reason == reason
    assert v.orders == []                           # nothing was sent
    assert d.detail["facts"] == facts


def test_an_unreadable_collateral_state_is_insufficient_risk_data_not_a_pass():
    v = V()
    v.raises = RuntimeError("wallet down")
    d = tick(engine(v))
    assert (d.action, d.reason) == ("stand_aside", "INSUFFICIENT_RISK_DATA")
    assert d.detail["why"] == "COLLATERAL_FACTS_UNREADABLE" and v.orders == []


def test_a_broker_that_cannot_state_it_is_a_refusal_when_the_check_is_on():
    v = Venue()                                      # no get_collateral_facts at all
    d = tick(engine(v))
    assert (d.action, d.reason) == ("stand_aside", "INSUFFICIENT_RISK_DATA")
    assert v.orders == []


def test_a_malformed_answer_is_a_refusal():
    v = V()
    v.facts = "OK"                                   # not a mapping
    assert tick(engine(v)).reason == "INSUFFICIENT_RISK_DATA"


def test_with_the_check_off_nothing_changes_for_existing_callers():
    v = Venue()
    assert tick(engine(v, flag=False)).action == "opened"


def test_the_check_is_for_the_overlay_only_acquire_margin_is_usdt_not_the_held_coin():
    v = V()
    v.facts = {"status": "NOT_COLLATERAL", "reason": "COLLATERAL_SWITCH_OFF"}
    e = engine(v, mode="acquire")
    assert e._collateral_refusal() is None


def test_a_passing_collateral_check_still_meets_the_other_gates():
    """It only ADDS a refusal: with the facts OK a stale market view is still
    refused by the pair gate, and with the facts NOT OK the collateral refusal
    is the one named (it comes first, before any gate that could reserve)."""
    ok = V()
    e = engine(ok)
    e.snapshot = dataclasses.replace(e.snapshot, observed_at_s=e.snapshot.observed_at_s - 3600)
    refused = tick(e)
    assert refused.action == "stand_aside"
    assert refused.reason != "HELD_COIN_NOT_COLLATERAL" and ok.orders == []
    bad = V()
    bad.facts = {"status": "NOT_COLLATERAL", "reason": "COLLATERAL_SWITCH_OFF"}
    e2 = engine(bad)
    e2.snapshot = dataclasses.replace(e2.snapshot, observed_at_s=e2.snapshot.observed_at_s - 3600)
    assert tick(e2).reason == "HELD_COIN_NOT_COLLATERAL"


def test_a_refusal_leaves_no_residue():
    from carry_engine import BookState
    v = V()
    v.facts = {"status": "INSUFFICIENT_RISK_DATA", "reason": "COLLATERAL_FIELD_ABSENT"}
    e = engine(v)
    before = (e._last_cycle_ms, e.state, e.position)
    d = tick(e)
    assert d.reason == "INSUFFICIENT_RISK_DATA"
    assert (e._last_cycle_ms, e.state, e.position) == before
    assert e.state == BookState.FLAT and v.orders == []


def test_an_acquire_entry_is_driven_end_to_end_unaffected():
    v = V()
    v.facts = {"status": "NOT_COLLATERAL", "reason": "COLLATERAL_SWITCH_OFF"}
    d = tick(engine(v, mode="acquire"))
    assert d.reason not in ("HELD_COIN_NOT_COLLATERAL", "INSUFFICIENT_RISK_DATA")


# -- the production wiring --------------------------------------------------------------

def test_the_real_ledger_wrapper_forwards_the_question_to_the_broker():
    """LedgerBroker delegates explicitly (no __getattr__). Without a forwarder
    every production overlay entry would be refused as 'cannot state'."""
    import ledger as L
    inner = broker_for(wallet(REAL_ROW))
    lb = L.LedgerBroker(inner, L.Journal(), ms=lambda: 1_700_000_000_000)
    assert lb.get_collateral_facts("BTCUSDT")["status"] == MF.OK
    off = L.LedgerBroker(broker_for(wallet(dict(REAL_ROW, collateralSwitch=False))),
                         L.Journal(), ms=lambda: 1_700_000_000_000)
    assert off.get_collateral_facts("BTCUSDT")["reason"] == MF.COLLATERAL_SWITCH_OFF


def test_production_enables_the_check():
    tree = ast.parse(open(os.path.join(BOT, "main.py"), encoding="utf-8").read())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", getattr(n.func, "attr", "")) == "CarryEngine"]
    assert calls, "main.py builds the CarryEngine"
    for c in calls:
        kw = {k.arg: k.value for k in c.keywords}
        assert isinstance(kw.get("require_collateral_facts"), ast.Constant) \
            and kw["require_collateral_facts"].value is True


def test_the_comment_no_longer_asserts_the_collateral_assumption_as_fact():
    src = open(os.path.join(BOT, "carry_engine.py"), encoding="utf-8").read()
    assert "has no reachable liquidation price at all, so this gate should never bind" not in src
    assert "_collateral_refusal" in src


def test_the_drill_exercises_the_same_gate_as_production():
    """The real-venue drill must meet the refusal production would, not skip it."""
    tree = ast.parse(open(os.path.join(BOT, "tools", "drill.py"), encoding="utf-8").read())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and any(k.arg == "require_liquidation_check" for k in n.keywords)]
    assert calls, "the drill builds its engine with the liquidation check"
    for c in calls:
        kw = {k.arg: k.value for k in c.keywords}
        assert isinstance(kw.get("require_collateral_facts"), ast.Constant) \
            and kw["require_collateral_facts"].value is True


def test_the_broker_asks_the_unfiltered_wallet_like_the_tracked_capture():
    seen = []
    client = FakeClient(mark=MARK)
    real = client._request

    def request(method, endpoint, **kw):
        if endpoint == "/v5/account/wallet-balance":
            seen.append(kw.get("params"))
            return envelope({"list": wallet(REAL_ROW)})
        return real(method, endpoint, **kw)
    client._request = request
    CarryBroker(client=client, order_gate=lambda: (True, "T"),
                sequence_source=lambda *a: 1).get_collateral_facts("BTCUSDT")
    assert seen == [{"accountType": "UNIFIED"}]
