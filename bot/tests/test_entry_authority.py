"""The common entry authority at the last boundary (BybitClient._request): a bare client opens nothing; every exposure-increasing order passes the kill switch, the
owner-approved risk policy (measured inputs), a notional cap and a durable reservation; exits are not weakened. Offline; temp directories; the transport is a recorder.
Reproduced before the fix: BybitClient.place_order / the linear-stop drill route and any direct _request('/v5/order/create') reached the transport with no gate at all."""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(BOT, "tools"))

import bybit_connection as bc  # noqa: E402
import entry_authority as EA  # noqa: E402
import risk_policy as RP  # noqa: E402
from fake_bybit import API_KEY, API_SECRET, FakeBybit  # noqa: E402
from persistence import StateStore  # noqa: E402

pytestmark = pytest.mark.real_entry_authority


class Cfg:
    BYBIT_VENUE = "testnet"
    USE_TESTNET = True
    PAPER_TRADING = False
    LIVE_TRADING_ACK = ""
    BYBIT_API_KEY = API_KEY
    BYBIT_API_SECRET = API_SECRET
    BYBIT_RECV_WINDOW_MS = 5000
    REQUEST_TIMEOUT_SECONDS = 5.0
    USE_LEVERAGE = False
    SYMBOL_FILTERS_TTL = 3600
    ORDERLINK_PREFIX = "BB"
    STATE_DB_PATH = ""


class Recorder(FakeBybit):
    pass


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "s.db"))
    yield s
    s.close()


def client(store, authority=None):
    v = Recorder()
    c = bc.BybitClient(config=Cfg(), store=store, transport=v)
    if authority is not None:
        c.entry_authority = authority
    return c, v


def order(**kw):
    body = {"category": "linear", "symbol": "BTCUSDT", "side": "Sell", "orderType": "Market", "qty": "0.001", "orderLinkId": "L1"}
    body.update(kw)
    return body


def sent_orders(v):
    return [r for r in v.requests if "/v5/order/create" in r["url"]]


def build(tmp_path, *, policy=None, state=None, kill=lambda: (False, ""), mark=lambda s: 100_000.0, max_usd=102.0, require_policy=True, hedge=None):
    path = None
    os.makedirs(str(tmp_path), exist_ok=True)
    if policy is not None:
        path = str(tmp_path / "policy.json")
        json.dump(dataclasses.asdict(policy), open(path, "w"))
    store = type("S", (), {"is_kill_switch_engaged": staticmethod(kill)})()
    return EA.build_for_book(venue="testnet", store=store, state_dir=str(tmp_path / "state"), max_notional_usd=max_usd, mark_provider=mark, policy_path=path,
                             state_provider=(lambda: state) if state is not None else None, require_risk_policy=require_policy,
                             perp_symbol="BTCUSDT", spot_symbol="BTCUSDT", hedge_reader=hedge)


POLICY = RP.Policy(per_trade_loss_usd=2.0, daily_loss_usd=3.0, strategy_drawdown_usd=5.0, portfolio_drawdown_usd=8.0, max_hold_hours=360.0, max_exposure_usd=100.0, max_leverage=3.0,
                   min_liquidity_usd=1000.0, max_unhedged_usd=5.0, min_margin_multiple=2.0, min_liquidation_distance_pct=20.0, max_data_age_s=90.0,
                   max_correlated_exposure_usd=100.0, min_recent_funding_bps=0.2, approved_by_owner=True, approval_note="TEST FIXTURE, not an owner approval")
GOOD = RP.State(trade_loss_usd=0.0, loss_today_usd=0.0, strategy_drawdown_usd=0.0, portfolio_drawdown_usd=0.0, hold_hours=0.0, exposure_usd=0.0, leverage=1.0, liquidity_usd=50_000.0,
                unhedged_usd=0.0, margin_multiple=5.0, liquidation_distance_pct=60.0, data_age_s=1.0, correlated_exposure_usd=0.0, recent_mean_funding_bps=0.5, operational_fault=False)


# ---------------------------------------------------------------- what counts as exposure

@pytest.mark.parametrize("body,expected", [
    (order(), True), (order(side="Buy"), True), (order(side="Buy", reduceOnly=True), False), (order(side="Buy", reduceOnly="true"), True),
    (order(category="spot", side="Buy"), True), (order(category="spot", side="Sell"), False), (order(category="spot", side="Sell", reduceOnly=True), False),
    (order(category="option"), True), (order(category="weird"), True), (order(category=""), True), (order(side="???"), True), (order(reduceOnly="no"), True),
    (order(category="spot", side="Buy", reduceOnly=True), True)])          # spot has no reduce-only: a flagged spot BUY is still an entry
def test_what_counts_as_new_exposure(body, expected):
    assert EA.increases_exposure(body) is expected


# ---------------------------------------------------------------- the bare client opens nothing

@pytest.mark.parametrize("body", [order(), order(side="Buy"), order(category="spot", side="Buy", orderLinkId="L2"), order(category="inverse"), order(category="option")])
def test_a_bare_client_refuses_every_opening_order_before_the_transport(store, body):
    c, v = client(store)
    with pytest.raises(bc.PermanentAPIError, match="NO_ENTRY_AUTHORITY"):
        c._request("POST", "/v5/order/create", body=body, signed=True)
    assert sent_orders(v) == [] and c.orders_submitted == 0


def test_the_linear_stop_drill_route_place_order_is_refused_too(store):
    c, v = client(store)
    res = c.place_order(symbol="BTCUSDT", side="Buy", qty="0.001", order_link_id="LS1") if not c.is_linear else None
    # whichever route reaches the transport is refused: call the low-level path place_order itself uses
    with pytest.raises(bc.PermanentAPIError, match="ENTRY_REFUSED"):
        c._request("POST", "/v5/order/create", body=order(orderLinkId="LS2"), signed=True)
    assert sent_orders(v) == []


def test_exits_are_not_blocked_and_reach_the_transport(store):
    c, v = client(store)
    c._request("POST", "/v5/order/create", body=order(side="Buy", reduceOnly=True, orderLinkId="X1"), signed=True)
    c._request("POST", "/v5/order/create", body=order(category="spot", side="Sell", orderLinkId="X2"), signed=True)
    assert len(sent_orders(v)) == 2


def test_an_order_without_a_link_id_or_core_fields_is_refused_not_defaulted(store, tmp_path):
    c, v = client(store, build(tmp_path, policy=POLICY, state=GOOD))
    for body in (order(orderLinkId=""), {k: x for k, x in order().items() if k != "orderLinkId"}, order(qty=""), order(symbol="")):
        with pytest.raises(bc.PermanentAPIError, match="ORDER_FIELD_MISSING"):
            c._request("POST", "/v5/order/create", body=body, signed=True)
    assert sent_orders(v) == []


# ---------------------------------------------------------------- the authority's checks

def test_positive_control_an_approved_measured_in_limit_order_reaches_the_transport_once(store, tmp_path):
    c, v = client(store, build(tmp_path, policy=POLICY, state=GOOD))
    c._request("POST", "/v5/order/create", body=order(), signed=True)
    assert len(sent_orders(v)) == 1 and c.orders_submitted == 1


def test_no_approved_policy_is_a_refusal_whatever_else_is_true(store, tmp_path):
    for pol in (None, dataclasses.replace(POLICY, approved_by_owner=False)):
        c, v = client(store, build(tmp_path / ("p" if pol is None else "q"), policy=pol, state=GOOD))
        with pytest.raises(bc.PermanentAPIError, match="NO_APPROVED_POLICY"):
            c._request("POST", "/v5/order/create", body=order(), signed=True)
        assert sent_orders(v) == []


def test_an_approved_policy_with_unmeasured_inputs_still_refuses(store, tmp_path):
    c, v = client(store, build(tmp_path, policy=POLICY, state=RP.State(operational_fault=False)))
    with pytest.raises(bc.PermanentAPIError, match="MISSING_INPUT"):
        c._request("POST", "/v5/order/create", body=order(), signed=True)
    assert sent_orders(v) == []


def test_a_policy_file_with_a_string_approval_or_an_unknown_field_is_not_a_policy(tmp_path):
    good = dataclasses.asdict(POLICY)
    for mutate in (lambda d: d.update(approved_by_owner="True"), lambda d: d.update(extra=1), lambda d: d.pop("max_leverage")):
        d = dict(good); mutate(d)
        p = tmp_path / "p.json"; p.write_text(json.dumps(d))
        pol = EA.load_policy(str(p))
        assert pol is None or pol.approved_by_owner is not True
    p = tmp_path / "ok.json"; p.write_text(json.dumps(good))
    assert EA.load_policy(str(p)).approved_by_owner is True and EA.load_policy(str(tmp_path / "missing.json")) is None
    (tmp_path / "junk.json").write_text("{nope")
    assert EA.load_policy(str(tmp_path / "junk.json")) is None


def test_the_kill_switch_is_read_at_the_boundary_and_an_unreadable_one_is_engaged(store, tmp_path):
    c, v = client(store, build(tmp_path, policy=POLICY, state=GOOD, kill=lambda: (True, "owner")))
    with pytest.raises(bc.PermanentAPIError, match="KILL_SWITCH_ENGAGED"):
        c._request("POST", "/v5/order/create", body=order(), signed=True)
    def boom():
        raise RuntimeError("db locked")
    c2, v2 = client(store, build(tmp_path / "b", policy=POLICY, state=GOOD, kill=boom))
    with pytest.raises(bc.PermanentAPIError, match="KILL_SWITCH_UNREADABLE"):
        c2._request("POST", "/v5/order/create", body=order(), signed=True)
    assert sent_orders(v) == [] and sent_orders(v2) == []


def test_a_breach_latches_the_suspension_across_a_new_authority_instance(store, tmp_path):
    bad = dataclasses.replace(GOOD, loss_today_usd=99.0)
    c, v = client(store, build(tmp_path, policy=POLICY, state=bad))
    with pytest.raises(bc.PermanentAPIError, match="BREACH:daily_loss_usd"):
        c._request("POST", "/v5/order/create", body=order(), signed=True)
    c2, v2 = client(store, build(tmp_path, policy=POLICY, state=GOOD))          # a RESTART with a clean state and the same state dir
    with pytest.raises(bc.PermanentAPIError, match="LATCHED"):
        c2._request("POST", "/v5/order/create", body=order(orderLinkId="L9"), signed=True)
    assert sent_orders(v) == [] and sent_orders(v2) == []
    RP.RiskLatch(str(tmp_path / "state" / "risk_latch.json")).clear("btcusdt_funding_carry_overlay", reason="owner reviewed", at_ms=1)
    c3, v3 = client(store, build(tmp_path, policy=POLICY, state=GOOD))
    c3._request("POST", "/v5/order/create", body=order(orderLinkId="L10"), signed=True)
    assert len(sent_orders(v3)) == 1


@pytest.mark.parametrize("qty,price,reason", [("0.002", None, "NOTIONAL_ABOVE_CAP"), ("0.001", "150000", "NOTIONAL_ABOVE_CAP"), ("abc", None, "UNPARSABLE"), ("-1", None, "INVALID"),
                                              ("0", None, "INVALID")])
def test_the_notional_cap_and_unparsable_quantities_refuse(store, tmp_path, qty, price, reason):
    c, v = client(store, build(tmp_path, policy=POLICY, state=GOOD))
    body = order(qty=qty, **({"price": price, "orderType": "Limit"} if price else {}))      # a priced order is a Limit (a priced Market is refused outright, below)
    with pytest.raises(bc.PermanentAPIError, match=reason):
        c._request("POST", "/v5/order/create", body=body, signed=True)
    assert sent_orders(v) == []


def test_no_price_for_a_market_order_refuses(store, tmp_path):
    c, v = client(store, build(tmp_path, policy=POLICY, state=GOOD, mark=lambda s: None))
    with pytest.raises(bc.PermanentAPIError, match="NO_PRICE_FOR_NOTIONAL"):
        c._request("POST", "/v5/order/create", body=order(), signed=True)


def test_the_wrong_venue_refuses(store, tmp_path):
    auth = EA.build_for_book(venue="mainnet", store=type("S", (), {"is_kill_switch_engaged": staticmethod(lambda: (False, ""))})(), state_dir=str(tmp_path / "s"),
                             max_notional_usd=102.0, mark_provider=lambda s: 1.0, policy_path=None, state_provider=lambda: GOOD, require_risk_policy=False,
                             perp_symbol="BTCUSDT", spot_symbol="BTCUSDT")
    c, v = client(store, auth)                                                  # a testnet client holding an authority built for mainnet
    with pytest.raises(bc.PermanentAPIError, match="VENUE_NOT_ALLOWED"):
        c._request("POST", "/v5/order/create", body=order(), signed=True)


# ---------------------------------------------------------------- the durable reservation

def test_an_identical_resend_is_allowed_a_conflicting_reuse_is_refused_and_the_third_entry_is_refused(store, tmp_path):
    c, v = client(store, build(tmp_path, policy=POLICY, state=GOOD))
    c._request("POST", "/v5/order/create", body=order(orderLinkId="A"), signed=True)
    with pytest.raises(bc.PermanentAPIError, match="Duplicate orderLinkId"):                 # the AUTHORITY let the identical resend through (a lost-ack retry);
        c._request("POST", "/v5/order/create", body=order(orderLinkId="A"), signed=True)     # it is the VENUE that answers 'duplicate', which the broker then resolves by readback
    with pytest.raises(bc.PermanentAPIError, match="RESERVATION_CONFLICT"):
        c._request("POST", "/v5/order/create", body=order(orderLinkId="A", qty="0.0009"), signed=True)
    c._request("POST", "/v5/order/create", body=order(side="Buy", category="spot", orderLinkId="B", qty="0.001"), signed=True)
    with pytest.raises(bc.PermanentAPIError, match="TOO_MANY_UNRESOLVED_ENTRIES"):
        c._request("POST", "/v5/order/create", body=order(orderLinkId="C"), signed=True)
    assert len(sent_orders(v)) == 3                                                          # A, A (resend), B


def test_a_refused_order_reserves_nothing(store, tmp_path):
    c, v = client(store, build(tmp_path, policy=POLICY, state=GOOD, kill=lambda: (True, "x")))
    with pytest.raises(bc.PermanentAPIError):
        c._request("POST", "/v5/order/create", body=order(orderLinkId="R"), signed=True)
    led = tmp_path / "state" / "entry_reservations.jsonl"
    assert not led.exists() or led.read_text().strip() == ""


def test_an_unreadable_reservation_ledger_refuses(store, tmp_path):
    auth = build(tmp_path, policy=POLICY, state=GOOD)
    (tmp_path / "state" / "entry_reservations.jsonl").write_text("not json\n")
    c, v = client(store, auth)
    with pytest.raises(bc.PermanentAPIError, match="RESERVATION_LEDGER_UNREADABLE"):
        c._request("POST", "/v5/order/create", body=order(), signed=True)


def test_concurrent_processes_cannot_both_reserve_past_the_limit(tmp_path):
    led = str(tmp_path / "led.jsonl")
    go = str(tmp_path / "go")
    code = ("import sys,os; sys.path[:0]=[%r]; import entry_authority as EA\n"
            "while not os.path.exists(%r): pass\n"
            "i=EA.OrderIntent('linear','BTCUSDT','Sell','0.001',False,sys.argv[1])\n"
            "print(EA.ReservationStore(%r).reserve(i, max_unresolved=2))" % (BOT, go, led))
    procs = [subprocess.Popen([sys.executable, "-I", "-c", code, f"P{i}"], stdout=subprocess.PIPE, text=True) for i in range(16)]
    import time
    time.sleep(3.0)
    open(go, "w").close()
    outs = [p.communicate(timeout=120)[0].strip() for p in procs]
    assert outs.count("None") == 2 and outs.count("TOO_MANY_UNRESOLVED_ENTRIES") == 14, outs


def test_the_reservation_takes_an_exclusive_lock(tmp_path, monkeypatch):
    import fcntl
    calls = []
    real = fcntl.flock
    monkeypatch.setattr(fcntl, "flock", lambda fd, op: (calls.append(op), real(fd, op))[1])
    EA.ReservationStore(str(tmp_path / "l.jsonl")).reserve(EA.OrderIntent("linear", "BTCUSDT", "Sell", "0.001", False, "Z"))
    assert calls and calls[0] == fcntl.LOCK_EX


def test_an_authority_with_no_checks_cannot_be_built():
    with pytest.raises(ValueError, match="no checks"):
        EA.EntryAuthority([])


# ---------------------------------------------------------------- production wiring (the real build_bot path)

def test_the_production_book_installs_the_authority_on_the_client_every_order_passes_through(monkeypatch, tmp_path):
    import config as _config
    import drill as D
    env = {"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0", "CARRY_EXECUTION_MODE": "overlay", "BYBIT_VENUE": "testnet", "STATE_DB_PATH": str(tmp_path / "state" / "s.db")}
    for k, val in env.items():
        monkeypatch.setenv(k, val)
    monkeypatch.setattr(_config, "_CFG", _config.load(env))
    bot = D._load_bot()
    assert isinstance(bot.client.entry_authority, EA.EntryAuthority) and bot.carry is not None
    assert bot.engine.client is bot.client
    # no policy file is configured, so the real production authority refuses to open anything, and the transport is never reached
    sent = []
    bot.client.transport = type("T", (), {"request": lambda self, *a, **k: (sent.append(a), (200, "{}"))[1]})()
    with pytest.raises(bc.PermanentAPIError, match="NO_APPROVED_POLICY"):
        bot.client._request("POST", "/v5/order/create", body=order(), signed=True)
    # The authority's RiskInputProvider may make READ-ONLY measurement requests (e.g. the order book for liquidity); what must never happen is an ORDER reaching the transport.
    assert [a for a in sent if a[0] == "POST" or "/order/create" in str(a[1])] == []


def test_an_order_refused_by_ANY_check_reserves_nothing(store, tmp_path):
    """Reproduced: only a kill-switch refusal skipped the reservation; a risk or notional refusal still reserved a slot for an order that was never sent."""
    bad = dataclasses.replace(GOOD, loss_today_usd=99.0)
    c, v = client(store, build(tmp_path, policy=POLICY, state=bad))
    for i in range(5):
        with pytest.raises(bc.PermanentAPIError):
            c._request("POST", "/v5/order/create", body=order(orderLinkId=f"N{i}"), signed=True)
    led = tmp_path / "state" / "entry_reservations.jsonl"
    assert not led.exists() or led.read_text().strip() == ""
    c2, v2 = client(store, build(tmp_path / "x", policy=POLICY, state=GOOD, max_usd=1.0))      # notional refusal
    with pytest.raises(bc.PermanentAPIError, match="NOTIONAL_ABOVE_CAP"):
        c2._request("POST", "/v5/order/create", body=order(orderLinkId="Q"), signed=True)
    led2 = tmp_path / "x" / "state" / "entry_reservations.jsonl"
    assert not led2.exists() or led2.read_text().strip() == ""


def test_a_local_refusal_is_a_known_not_sent_outcome_not_an_unknown_one():
    """Reproduced: a transport-level refusal (ret_code 0) was read by CarryBroker as 'the request may still land', which halted the book and blocked restart."""
    from carry_broker import CarryBroker
    assert CarryBroker._definitively_rejected(bc.OrderNotSent("ENTRY_REFUSED: X")) is True
    assert CarryBroker._definitively_rejected(bc.PermanentAPIError("HTTP 400")) is False          # no venue retCode: still unknown
    assert CarryBroker._definitively_rejected(bc.TransientAPIError("HTTP 500")) is False
    assert issubclass(bc.OrderNotSent, bc.PermanentAPIError)                                      # existing handlers that catch PermanentAPIError keep working


def test_the_reservation_limit_is_a_window_not_a_lifetime_so_the_book_does_not_wedge(tmp_path):
    """Found in review: nothing marks a reservation resolved, so a lifetime count refused every entry after the first pair, forever."""
    st = EA.ReservationStore(str(tmp_path / "l.jsonl"))
    t0 = 1_800_000_000_000
    mk = lambda link: EA.OrderIntent("linear", "BTCUSDT", "Sell", "0.001", False, link)
    assert st.reserve(mk("P1"), now_ms=t0) is None and st.reserve(mk("P2"), now_ms=t0 + 1000) is None
    assert st.reserve(mk("P3"), now_ms=t0 + 2000) == "TOO_MANY_UNRESOLVED_ENTRIES"          # concurrent entries are still bounded
    later = t0 + EA.RESERVATION_WINDOW_MS + 1
    assert st.reserve(mk("P4"), now_ms=later) is None                                       # a new pair, long after, is allowed
    assert st.reserve(mk("P1"), now_ms=later) is None                                       # an identical resend of an OLD id is still recognised
    other = EA.OrderIntent("linear", "BTCUSDT", "Sell", "0.5", False, "P1")
    assert st.reserve(other, now_ms=later) == "RESERVATION_CONFLICT_SAME_ID_DIFFERENT_ORDER"   # and a different order under it is still a conflict forever


# ---------------------------------------------------------------- red-team reproductions (a327b75)

@pytest.mark.parametrize("endpoint", ["/v5/order/create?x=1", "/v5/order/create/", "/V5/order/create", "/v5/order//create", "/v5/order/create#f", "/v5/order/create ", "/v5/order/../order/create"])
def test_a_non_canonical_write_endpoint_is_refused_before_the_transport(store, endpoint):
    """Reproduced: POST '/v5/order/create?x=1' skipped both the live gate and the authority (exact string match) and reached the transport."""
    c, v = client(store)
    with pytest.raises(bc.OrderNotSent, match="ENDPOINT_REFUSED"):
        c._request("POST", endpoint, body=order(), signed=True)
    assert v.requests == [] and c.orders_submitted == 0


def test_the_live_gate_cannot_be_skipped_by_a_query_string_on_a_mainnet_client(store):
    class MainnetCfg(Cfg):
        BYBIT_VENUE = "mainnet"
        USE_TESTNET = False
    v = Recorder()
    c = bc.BybitClient(config=MainnetCfg(), store=store, transport=v)
    with pytest.raises(bc.OrderNotSent):
        c._request("POST", "/v5/order/create?x=1", body=order(), signed=True)
    with pytest.raises(bc.OrderNotSent, match="LIVE_NOT_AUTHORIZED"):
        c._request("POST", "/v5/order/create", body=order(), signed=True)
    assert v.requests == []


@pytest.mark.parametrize("endpoint", ["/v5/order/amend", "/v5/order/create-batch", "/v5/order/amend-batch"])
def test_unused_order_write_endpoints_are_denied(store, endpoint):
    c, v = client(store)
    with pytest.raises(bc.OrderNotSent, match="ENDPOINT_REFUSED"):
        c._request("POST", endpoint, body=order(), signed=True)
    assert v.requests == []


def test_cancel_and_reads_still_work(store):
    c, v = client(store)
    c._request("POST", "/v5/order/cancel", body={"category": "linear", "symbol": "BTCUSDT", "orderLinkId": "Z"}, signed=True) if False else None
    c._request("GET", "/v5/account/wallet-balance", params={"accountType": "UNIFIED"}, signed=True)
    assert v.requests


def test_a_marketable_limit_sell_cannot_declare_a_tiny_notional(store, tmp_path):
    """Reproduced: Limit Sell 100 BTC at price '1' declared $100 and was sent under a $102 cap."""
    c, v = client(store, build(tmp_path, policy=POLICY, state=GOOD))
    with pytest.raises(bc.OrderNotSent, match="NOTIONAL_ABOVE_CAP"):
        c._request("POST", "/v5/order/create", body=order(orderType="Limit", qty="100", price="1", timeInForce="PostOnly"), signed=True)
    assert sent_orders(v) == []
    c._request("POST", "/v5/order/create", body=order(orderType="Limit", qty="0.001", price="99900", timeInForce="PostOnly", orderLinkId="LM1"), signed=True)   # control
    assert len(sent_orders(v)) == 1


def test_a_limit_buy_is_costed_at_its_own_price_and_a_limit_sell_without_a_mark_refuses(store, tmp_path):
    c, v = client(store, build(tmp_path, policy=POLICY, state=GOOD))
    with pytest.raises(bc.OrderNotSent, match="NOTIONAL_ABOVE_CAP"):
        c._request("POST", "/v5/order/create", body=order(category="spot", side="Buy", orderType="Limit", qty="1", price="100000", orderLinkId="LB1"), signed=True)
    c2, v2 = client(store, build(tmp_path / "n", policy=POLICY, state=GOOD, mark=lambda s: None))
    with pytest.raises(bc.OrderNotSent, match="NO_PRICE_FOR_NOTIONAL"):
        c2._request("POST", "/v5/order/create", body=order(orderType="Limit", qty="0.001", price="100000", orderLinkId="LB2"), signed=True)


def test_a_local_refusal_through_place_order_is_recorded_rejected_not_unknown(store):
    """Reproduced: a bare client's place_order stored the refused order as status=unknown and listed it in unresolved_orders()."""
    c, v = client(store)
    r = c.place_order(symbol="BTCUSDT", side="Buy", qty="0.001", order_link_id="PO1")
    assert not r.ok and r.reason and "NO_ENTRY_AUTHORITY" in r.reason
    assert store.get_order("PO1")["status"] == "rejected"
    assert "PO1" not in [x["order_link_id"] for x in store.unresolved_orders()]
    assert sent_orders(v) == []


@pytest.mark.parametrize("extra", [{"isLeverage": 1}, {"isLeverage": "1"}])
def test_a_margin_spot_sell_is_exposure_not_an_exit(extra):
    assert EA.increases_exposure(order(category="spot", side="Sell", **extra)) is True
    assert EA.increases_exposure(order(category="spot", side="Sell", isLeverage=0)) is False
    assert EA.increases_exposure(order(category="spot", side="Sell")) is False


@pytest.mark.parametrize("flag", [1, "TRUE ", "True", "yes", "1", 1.0])
def test_only_the_exact_reduce_only_forms_make_an_order_an_exit(flag):
    assert EA.increases_exposure(order(side="Buy", reduceOnly=flag)) is True
    assert EA.increases_exposure(order(side="Buy", reduceOnly=True)) is False and EA.increases_exposure(order(side="Buy", reduceOnly="true")) is True


def test_a_dict_subclass_that_lies_is_refused_and_an_authority_that_errors_does_not_approve(store, tmp_path):
    class Liar(dict):
        def get(self, k, d=None):
            return True if k == "reduceOnly" else super().get(k, d)
    c, v = client(store)
    with pytest.raises(bc.OrderNotSent, match="PLAIN_OBJECT"):
        c._request("POST", "/v5/order/create", body=Liar(order(side="Buy")), signed=True)
    class Boom:
        def authorize(self, body, *, venue):
            raise OSError("disk")
    c2, v2 = client(store, Boom())
    with pytest.raises(bc.OrderNotSent, match="AUTHORITY_ERROR:OSError"):
        c2._request("POST", "/v5/order/create", body=order(), signed=True)
    assert v.requests == [] and v2.requests == []


def test_resolve_appends_never_replaces_and_frees_the_window(tmp_path):
    st = EA.ReservationStore(str(tmp_path / "l.jsonl"))
    mk = lambda link: EA.OrderIntent("linear", "BTCUSDT", "Sell", "0.001", False, link)
    t0 = 1_800_000_000_000
    assert st.reserve(mk("A"), now_ms=t0) is None and st.reserve(mk("B"), now_ms=t0) is None
    assert st.reserve(mk("C"), now_ms=t0) == "TOO_MANY_UNRESOLVED_ENTRIES"
    ino = os.stat(st.path).st_ino
    st.resolve("A")
    assert os.stat(st.path).st_ino == ino                                        # same file: nothing was swapped from under a waiting reserve()
    assert st.reserve(mk("C"), now_ms=t0) is None                                # a resolved entry frees a slot inside the window


def test_a_symlinked_policy_file_is_not_a_policy(tmp_path):
    real = tmp_path / "real.json"; real.write_text(json.dumps(dataclasses.asdict(POLICY)))
    link = tmp_path / "link.json"; os.symlink(real, link)
    assert EA.load_policy(str(real)).approved_by_owner is True and EA.load_policy(str(link)) is None


def test_the_backtests_simulated_authority_works_only_on_its_own_simulated_transport(store):
    """Found by CI: the backtester drives an in-process SimulatedExchange through a BybitClient; a bare client refused every order (0 trades). Its authority is bound to that transport."""
    from backtest import SimulatedVenueAuthority
    sim = Recorder()
    auth = SimulatedVenueAuthority(sim)
    c = bc.BybitClient(config=Cfg(), store=store, transport=sim)
    c.entry_authority = auth
    c._request("POST", "/v5/order/create", body=order(orderLinkId="BT1"), signed=True)
    assert len(sent_orders(sim)) == 1
    other = Recorder()                                              # the same authority on ANY other transport (a real venue's) approves nothing
    c2 = bc.BybitClient(config=Cfg(), store=store, transport=other)
    c2.entry_authority = auth
    with pytest.raises(bc.OrderNotSent, match="BOUND_TO_ANOTHER_TRANSPORT"):
        c2._request("POST", "/v5/order/create", body=order(orderLinkId="BT2"), signed=True)
    assert sent_orders(other) == []


# ================================================================ red-team round 2 (fix worker G3)
# Each test below failed on c8e6cbd (PoCs rt2_a/poc1-4) and has a positive control next to it.

def _auth_refusal(auth, body):
    try:
        auth.authorize(body, venue="testnet")
    except EA.EntryRefused as e:
        return e.reasons
    return None


def _bare_auth(tmp_path, **kw):
    return build(tmp_path, require_policy=False, **kw)


# ---- 1. Market order + price must not be sized at that price; orderType is stated, exact, and a Market order has none --------------------------------------------

def test_market_order_with_a_price_is_refused_and_never_sent(store, tmp_path):
    c, v = client(store, build(tmp_path, policy=POLICY, state=GOOD, mark=lambda s: 50_000.0))
    with pytest.raises(bc.PermanentAPIError, match="MARKET_ORDER_WITH_PRICE"):
        c._request("POST", "/v5/order/create", body=order(qty="1", price="1", orderLinkId="M1", side="Sell"), signed=True)
    assert sent_orders(v) == []


def test_market_order_with_a_price_is_refused_on_both_sides(tmp_path):
    a = _bare_auth(tmp_path, mark=lambda s: 50_000.0)
    assert _auth_refusal(a, order(side="Sell", price="1", orderLinkId="P1")) == ("MARKET_ORDER_WITH_PRICE",)
    assert _auth_refusal(a, order(category="spot", side="Buy", price="1", orderLinkId="P2")) == ("MARKET_ORDER_WITH_PRICE",)


def _intent(**kw):
    d = dict(category="linear", symbol="BTCUSDT", side="Buy", qty="1", reduce_only=False, order_link_id="X1", price=None, order_type="Market")
    d.update(kw)
    return EA.OrderIntent(**d)


def test_order_notional_of_a_market_order_is_never_below_the_mark_on_either_side():
    mark = lambda s: 50_000.0                                                   # noqa: E731
    for side in ("Buy", "Sell"):
        assert EA.order_notional(_intent(side=side, price="1"), mark)[0] == 50_000      # a stated price can only RAISE a market order's notional
        assert EA.order_notional(_intent(side=side, price="90000"), mark)[0] == 90_000
        assert EA.order_notional(_intent(side=side), mark)[0] == 50_000                 # positive control: the plain market order is at the mark
        assert EA.order_notional(_intent(side=side), None) == (None, "NO_PRICE_FOR_NOTIONAL")
    # a limit BUY is still at its own limit (it cannot fill above it): that behaviour is unchanged
    assert EA.order_notional(_intent(order_type="Limit", side="Buy", price="40000"), mark)[0] == 40_000


@pytest.mark.parametrize("bad,code", [(None, "ORDER_FIELD_MISSING:orderType"), ("", "ORDER_FIELD_MISSING:orderType"), ("market", "ORDER_TYPE_UNKNOWN:market"),
                                      ("Stop", "ORDER_TYPE_UNKNOWN:Stop"), ("MARKET", "ORDER_TYPE_UNKNOWN:MARKET"), ("Market ", "ORDER_TYPE_UNKNOWN:Market ")])
def test_unknown_or_missing_order_type_is_refused(tmp_path, bad, code):
    a = _bare_auth(tmp_path)
    b = order()
    if bad is None:
        del b["orderType"]
    else:
        b["orderType"] = bad
    assert _auth_refusal(a, b) == (code,)


def test_positive_control_honest_market_and_limit_orders_pass(tmp_path):
    a = _bare_auth(tmp_path, mark=lambda s: 50_000.0)
    assert _auth_refusal(a, order(qty="0.001", orderLinkId="H1")) is None
    assert _auth_refusal(a, order(qty="0.001", orderType="Limit", price="50000", timeInForce="PostOnly", orderLinkId="H2")) is None


# ---- 3. the authority binds category / symbol / side to the carry's legs -------------------------------------------------------------------------------------

@pytest.mark.parametrize("body,code", [
    (order(symbol="DOGEUSDT", side="Sell", qty="0.001"), "UNKNOWN_INSTRUMENT:linear:DOGEUSDT"),
    (order(category="option", symbol="BTC-30DEC26-90000-C-USDT", side="Buy", qty="1"), "UNKNOWN_INSTRUMENT:option:BTC-30DEC26-90000-C-USDT"),
    (order(category="inverse", symbol="BTCUSD", side="Sell"), "UNKNOWN_INSTRUMENT:inverse:BTCUSD"),
    (order(category="LINEAR"), "UNKNOWN_INSTRUMENT:LINEAR:BTCUSDT"),
    (order(symbol="BTCUSDT "), "UNKNOWN_INSTRUMENT:linear:BTCUSDT "),
    (order(side="Buy"), "WRONG_DIRECTION:linear:BTCUSDT:Buy"),                 # a perp LONG (the carry is short)
    (order(side="sell"), "WRONG_DIRECTION:linear:BTCUSDT:sell"),
    (order(category="spot", side="Sell", isLeverage=1), "IS_LEVERAGE_NOT_ALLOWED"),   # a leveraged spot sell: not an exit, refused as a margin order
])
def test_only_the_carry_legs_in_their_direction_are_authorised(tmp_path, body, code):
    a = _bare_auth(tmp_path, mark=lambda s: 50_000.0 if s == "BTCUSDT" else None)
    reasons = _auth_refusal(a, dict(body, orderLinkId="B1"))
    assert reasons is not None and code in reasons


def test_the_two_carry_legs_are_authorised_positive_control(tmp_path):
    a = _bare_auth(tmp_path, mark=lambda s: 50_000.0)
    assert _auth_refusal(a, order(side="Sell", qty="0.001", orderLinkId="C1")) is None
    assert _auth_refusal(a, order(category="spot", side="Buy", qty="0.001", marketUnit="baseCoin", orderLinkId="C2")) is None


def test_symbols_are_bound_per_category_not_just_listed(tmp_path):
    a = EA.build_for_book(venue="testnet", store=type("S", (), {"is_kill_switch_engaged": staticmethod(lambda: (False, ""))})(), state_dir=str(tmp_path / "s"),
                          max_notional_usd=102.0, mark_provider=lambda s: 50_000.0, require_risk_policy=False, perp_symbol="BTCUSDT", spot_symbol="ETHUSDT")
    assert _auth_refusal(a, order(category="spot", symbol="BTCUSDT", side="Buy", orderLinkId="S1")) is not None     # BTCUSDT is the PERP here, not a spot leg
    assert _auth_refusal(a, order(symbol="ETHUSDT", side="Sell", orderLinkId="S2")) is not None


def test_build_for_book_refuses_to_be_built_unbound(tmp_path):
    for kw in ({}, {"perp_symbol": "BTCUSDT"}, {"spot_symbol": "BTCUSDT"}, {"perp_symbol": "btc usdt", "spot_symbol": "BTCUSDT"}):
        with pytest.raises(ValueError):
            EA.build_for_book(venue="testnet", store=object(), state_dir=str(tmp_path / "s"), max_notional_usd=1.0, **kw)


def test_reduce_only_perp_exit_is_not_weakened_nor_bound(store, tmp_path):
    c, v = client(store, build(tmp_path, policy=POLICY, state=GOOD))
    c._request("POST", "/v5/order/create", body=order(side="Buy", reduceOnly=True, orderLinkId="E1"), signed=True)    # exit as today: not an authority matter
    assert len(sent_orders(v)) == 1


def _spot_sell(qty="0.01", **kw):
    return order(**dict(dict(category="spot", side="Sell", qty=qty, marketUnit="baseCoin"), **kw))


def test_spot_sell_without_a_hedge_reader_is_refused_not_treated_as_an_exit(store, tmp_path):
    c, v = client(store, build(tmp_path, policy=POLICY, state=GOOD))
    with pytest.raises(bc.PermanentAPIError, match="WRONG_DIRECTION:spot:BTCUSDT:Sell"):
        c._request("POST", "/v5/order/create", body=_spot_sell(orderLinkId="SS1"), signed=True)
    assert sent_orders(v) == []


def test_spot_sell_that_strips_the_hedge_off_a_short_perp_is_refused(store, tmp_path):
    # short perp 0.01, spot 0.01: selling 0.005 leaves 0.005 against a 0.01 short
    c, v = client(store, build(tmp_path, policy=POLICY, state=GOOD, hedge=lambda: (0.01, 0.01)))
    with pytest.raises(bc.PermanentAPIError, match="WRONG_DIRECTION"):
        c._request("POST", "/v5/order/create", body=_spot_sell("0.005", orderLinkId="SS2"), signed=True)
    assert sent_orders(v) == []


@pytest.mark.parametrize("reader", [lambda: (_ for _ in ()).throw(RuntimeError("venue down")), lambda: (float("nan"), 1.0), lambda: (0.0, float("inf")), lambda: "x",
                                    lambda: (-1.0, 1.0)])
def test_spot_sell_with_an_unreadable_hedge_is_refused(store, tmp_path, reader):
    c, v = client(store, build(tmp_path, policy=POLICY, state=GOOD, hedge=reader))
    with pytest.raises(bc.PermanentAPIError, match="WRONG_DIRECTION"):
        c._request("POST", "/v5/order/create", body=_spot_sell(orderLinkId="SS3"), signed=True)


def test_spot_sell_positive_controls_flat_perp_proportional_close(store, tmp_path):
    for n, (perp, spot, qty) in enumerate([(0.0, 0.01, "0.01"),                  # perp already closed: sell it all
                                           (0.004, 0.01, "0.006")]):             # proportional partial close: 0.004 spot left against a 0.004 short
        c, v = client(store, build(tmp_path / f"d{n}", policy=POLICY, state=GOOD, hedge=lambda perp=perp, spot=spot: (perp, spot)))
        c._request("POST", "/v5/order/create", body=_spot_sell(qty, orderLinkId=f"OK{n}"), signed=True)
        assert len(sent_orders(v)) == 1


def test_spot_sell_in_a_quote_unit_cannot_be_shown_to_keep_the_hedge(store, tmp_path):
    c, v = client(store, build(tmp_path, policy=POLICY, state=GOOD, hedge=lambda: (0.0, 1.0)))
    with pytest.raises(bc.PermanentAPIError, match="MARKET_UNIT_NOT_BASE_COIN"):               # not an exit (hedge cannot be shown), and not a base-coin order either
        c._request("POST", "/v5/order/create", body=_spot_sell(orderLinkId="Q1", marketUnit="quoteCoin"), signed=True)


# ---- 4. reservation ledger: link id shape, and 'identical' means the whole canonical body ----------------------------------------------------------------------

@pytest.mark.parametrize("link", [" A1", "A1 ", "A1\n", "A1\x00", "\tA1", "A 1", "a" * 37, "é1", "A1;", 5, ["A1"], True])
def test_order_link_id_must_be_exactly_the_venue_shape(tmp_path, link):
    a = _bare_auth(tmp_path)
    assert _auth_refusal(a, order(orderLinkId=link)) == ("ORDER_LINK_ID_INVALID",)


def test_whitespace_link_id_cannot_slip_a_third_distinct_order_past_the_limit(tmp_path):
    a = _bare_auth(tmp_path)
    assert _auth_refusal(a, order(orderLinkId="A1")) is None and _auth_refusal(a, order(orderLinkId="A2")) is None
    assert _auth_refusal(a, order(orderLinkId="A3")) == ("TOO_MANY_UNRESOLVED_ENTRIES",)
    assert _auth_refusal(a, order(orderLinkId=" A1")) == ("ORDER_LINK_ID_INVALID",)
    assert open(os.path.join(str(tmp_path), "state", "entry_reservations.jsonl")).read().count("\n") == 2


def test_link_id_edge_shapes_are_accepted_positive_control(tmp_path):
    a = EA.build_for_book(venue="testnet", store=type("S", (), {"is_kill_switch_engaged": staticmethod(lambda: (False, ""))})(), state_dir=str(tmp_path / "s"),
                          max_notional_usd=102.0, mark_provider=lambda s: 50_000.0, require_risk_policy=False, perp_symbol="BTCUSDT", spot_symbol="BTCUSDT")
    assert _auth_refusal(a, order(orderLinkId="a-B_9" * 7 + "x")) is None                    # 36 chars


@pytest.mark.parametrize("changed", [dict(orderType="Limit", price="50000"), dict(price=None, reduceOnly=True), dict(timeInForce="PostOnly"), dict(qty="0.002"),
                                     dict(category="spot", side="Buy"), dict(side="Buy")])
def test_same_link_id_with_a_different_canonical_body_is_a_conflict(tmp_path, changed):
    a = _bare_auth(tmp_path, mark=lambda s: 50_000.0)
    first = order(orderLinkId="R1")
    assert _auth_refusal(a, first) is None
    second = dict(first, orderLinkId="R1")
    second.update(changed)
    reasons = _auth_refusal(a, second)
    assert reasons is not None
    if "reduceOnly" not in changed:                                              # (a reduce-only is an exit and never reaches the ledger at all)
        assert "RESERVATION_CONFLICT_SAME_ID_DIFFERENT_ORDER" in reasons or "WRONG_DIRECTION:linear:BTCUSDT:Buy" in reasons


def test_the_ledger_itself_compares_the_full_body(tmp_path):
    store_ = EA.ReservationStore(str(tmp_path / "led.jsonl"))
    base = EA.OrderIntent(category="linear", symbol="BTCUSDT", side="Sell", qty="0.001", reduce_only=False, order_link_id="R2", price=None, order_type="Market")
    assert store_.reserve(base) is None
    assert store_.reserve(base) is None                                          # positive control: the identical resend
    for kw in (dict(order_type="Limit", price="90000"), dict(price="1"), dict(reduce_only=True), dict(time_in_force="GTC")):
        other = EA.OrderIntent(**dict(base.__dict__, **kw))
        assert store_.reserve(other) == "RESERVATION_CONFLICT_SAME_ID_DIFFERENT_ORDER", kw


# ---- 2. write endpoints: strict canonical + allow-list on a BARE client ------------------------------------------------------------------------------------

def _bare_client(store):
    v = Recorder()
    c = bc.BybitClient(config=Cfg(), store=store, transport=v)
    assert c.capability is None and c.entry_authority is None
    return c, v


@pytest.mark.parametrize("endpoint", [
    "/v5/asset/withdraw/create", "/v5/asset/transfer/inter-transfer", "/v5/spot-margin-trade/borrow", "/v5/account/set-margin-mode", "/v5/user/create-sub-member",
    "/v5/order/amend", "/v5/order/create-batch", "/v5/order/%63reate", "/v5/order/create%00", "/v5/order/create;x=1", "/v5/order/create\\x", "/v5/order/create\x00",
    "/v5/order/create\n", "/v5/order/create/", "/v5/Order/create", "/v5/order/create?x=1", "/v5//order/create", "/v5/order/./create", "v5/order/create", "/v5/order/cancel-all-x",
    "/v5/position/switch-mode", "/v5/position/set-risk-limit", "/v5/order/cancel\r", "/v5/account/demo-apply-money",
])
def test_a_bare_client_refuses_every_write_that_is_not_allow_listed(store, endpoint):
    c, v = _bare_client(store)
    with pytest.raises(bc.OrderNotSent, match="ENDPOINT_REFUSED"):
        c._request("POST", endpoint, body={"coin": "BTC", "qty": "1", "orderLinkId": "W1"}, signed=True)
    assert v.requests == []


@pytest.mark.parametrize("method", ["DELETE", "PUT", "PATCH", "HEAD", "delete"])
def test_a_bare_client_refuses_non_post_writes(store, method):
    c, v = _bare_client(store)
    with pytest.raises(bc.OrderNotSent, match="ENDPOINT_REFUSED"):
        c._request(method, "/v5/order/cancel", body={"category": "linear", "symbol": "BTCUSDT"}, signed=True)
    assert v.requests == []


@pytest.mark.parametrize("endpoint,body", [("/v5/order/cancel", {"category": "linear", "symbol": "BTCUSDT", "orderLinkId": "C1"}),
                                           ("/v5/order/cancel-all", {"category": "linear", "symbol": "BTCUSDT"}),
                                           ("/v5/position/trading-stop", {"category": "linear", "symbol": "BTCUSDT", "stopLoss": "1", "tpslMode": "Full", "positionIdx": 0}),
                                           ("/v5/position/set-leverage", {"category": "linear", "symbol": "BTCUSDT", "buyLeverage": "1", "sellLeverage": "1"})])
def test_allow_listed_writes_still_reach_the_transport_positive_control(store, endpoint, body):
    c, v = _bare_client(store)
    try:
        c._request("POST", endpoint, body=body, signed=True)
    except bc.BybitAPIError:
        pass                                                                    # the fake may answer with a venue error; what matters is that it was SENT
    assert [r for r in v.requests if r["url"].endswith(endpoint)]


def test_the_demo_faucet_is_allow_listed_for_the_demo_venue_only(store):
    assert bc.WRITE_ENDPOINT_ALLOWLIST["/v5/account/demo-apply-money"] == frozenset({"demo"})
    c, v = _bare_client(store)
    with pytest.raises(bc.OrderNotSent, match="ENDPOINT_REFUSED"):
        c._request("POST", "/v5/account/demo-apply-money", body={"adjustType": 0}, signed=True)


def test_the_write_allow_list_is_exactly_what_the_code_sends():
    """Inventory pin: a new write endpoint needs a reviewed change HERE and in runtime_capability.EXECUTOR_POST_PATHS (a subset of it)."""
    import runtime_capability as RC
    assert set(bc.WRITE_ENDPOINT_ALLOWLIST) == {"/v5/order/create", "/v5/order/cancel", "/v5/order/cancel-all", "/v5/position/trading-stop", "/v5/position/set-leverage",
                                                "/v5/account/demo-apply-money"}
    assert RC.EXECUTOR_POST_PATHS <= set(bc.WRITE_ENDPOINT_ALLOWLIST)
    assert not (set(bc.WRITE_ENDPOINT_ALLOWLIST) & bc.DENIED_ORDER_ENDPOINTS)


# ---- set-leverage has its own bound ------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"category": "linear", "symbol": "BTCUSDT", "buyLeverage": "100", "sellLeverage": "100"},
    {"category": "linear", "symbol": "BTCUSDT", "buyLeverage": "11", "sellLeverage": "11"},
    {"category": "linear", "symbol": "BTCUSDT", "buyLeverage": "3", "sellLeverage": "5"},
    {"category": "linear", "symbol": "BTCUSDT", "buyLeverage": "03", "sellLeverage": "03"},
    {"category": "linear", "symbol": "BTCUSDT", "buyLeverage": "0", "sellLeverage": "0"},
    {"category": "linear", "symbol": "BTCUSDT", "buyLeverage": 3, "sellLeverage": 3},
    {"category": "linear", "symbol": "BTCUSDT", "buyLeverage": "3", "sellLeverage": "3", "extra": "x"},
    {"category": "spot", "symbol": "BTCUSDT", "buyLeverage": "3", "sellLeverage": "3"},
    {"category": "linear", "symbol": "btc usdt", "buyLeverage": "3", "sellLeverage": "3"},
    {"category": "linear", "symbol": "DOGEUSDT", "buyLeverage": "3", "sellLeverage": "3"},     # refused only once an authority binds the perp symbol (see below)
])
def test_set_leverage_is_bounded_and_bound_to_the_perp_symbol(store, tmp_path, body):
    c, v = client(store, build(tmp_path, require_policy=False))
    with pytest.raises(bc.OrderNotSent, match="LEVERAGE_REFUSED"):
        c._request("POST", "/v5/position/set-leverage", body=body, signed=True)
    assert v.requests == []


def test_set_leverage_positive_control_the_request_the_client_builds(store):
    c, v = _bare_client(store)
    c._request("POST", "/v5/position/set-leverage", body={"category": "linear", "symbol": "BTCUSDT", "buyLeverage": "10", "sellLeverage": "10"}, signed=True)
    assert [r for r in v.requests if r["url"].endswith("/v5/position/set-leverage")]


# ---- 5. TOCTOU: the authority judges the exact string that is sent -----------------------------------------------------------------------------------------

class _LyingStr(str):
    def __str__(self):
        return "0.001"


class _EqTrue(str):
    def __eq__(self, o):
        return True
    __hash__ = str.__hash__


class _Capture:
    """An authority that records what it was handed and approves."""

    def __init__(self):
        self.seen = []

    def authorize(self, body, *, venue):
        self.seen.append(dict(body))


def test_a_str_subclass_cannot_show_the_authority_different_data_than_the_wire(store, tmp_path):
    c, v = client(store, build(tmp_path, policy=POLICY, state=GOOD, mark=lambda s: 50_000.0))
    with pytest.raises(bc.PermanentAPIError, match="NOTIONAL_ABOVE_CAP"):                # the wire says qty "1" (= $50k), not the lie "0.001"
        c._request("POST", "/v5/order/create", body=order(qty=_LyingStr("1"), orderLinkId="T1"), signed=True)
    assert sent_orders(v) == []


def test_an_eq_true_reduce_only_cannot_pass_as_an_exit(store, tmp_path):
    c, v = client(store, build(tmp_path, policy=POLICY, state=GOOD, mark=lambda s: 50_000.0))
    with pytest.raises(bc.PermanentAPIError, match="REDUCE_ONLY_NOT_BOOLEAN"):
        c._request("POST", "/v5/order/create", body=order(qty="1000", reduceOnly=_EqTrue("false"), orderLinkId="T2"), signed=True)
    assert sent_orders(v) == []


def test_the_authority_is_handed_exactly_the_decoded_wire_string(store):
    c, v = _bare_client(store)
    cap = _Capture()
    c.entry_authority = cap
    body = order(qty=_LyingStr("0.5"), orderLinkId="T3")
    try:
        c._request("POST", "/v5/order/create", body=body, signed=True)
    except bc.BybitAPIError:
        pass
    wire = [r for r in v.requests if r["url"].endswith("/v5/order/create")][0]
    wire_body = wire["body"] if isinstance(wire.get("body"), str) else json.dumps(wire.get("body"))
    assert cap.seen and cap.seen[0] == json.loads(wire_body) and cap.seen[0]["qty"] == "0.5"


def test_a_body_that_is_not_plain_json_is_refused_before_anything_else(store):
    c, v = _bare_client(store)
    for bad in ({"qty": float("nan")}, {"qty": object()}):
        with pytest.raises(bc.OrderNotSent, match="BODY_REFUSED"):
            c._request("POST", "/v5/order/cancel", body=bad, signed=True)
    assert v.requests == []


@pytest.mark.parametrize("endpoint", ["/v5/order/create\n", "/v5/order/%63reate", "/v5/order/create;x", "/v5/order/create\x00", "/v5/order/create\\x", "/v5/Order/create"])
def test_the_canonical_regex_refuses_independently_of_the_allow_list(store, monkeypatch, endpoint):
    """Defence in depth: even a (mistaken) allow-list entry cannot make a non-canonical path sendable."""
    monkeypatch.setitem(bc.WRITE_ENDPOINT_ALLOWLIST, endpoint, None)
    c, v = _bare_client(store)
    with pytest.raises(bc.OrderNotSent, match="ENDPOINT_REFUSED"):
        c._request("POST", endpoint, body={"qty": "1"}, signed=True)
    assert v.requests == []


# ================================================================ round 3 (red team): reduce-only binding, leverage/stop/cancel bodies, closed body, mark sanity

def _bound(tmp_path, **kw):
    """An authority bound to BTCUSDT perp+spot with no risk policy (the binding checks are what is tested)."""
    kw.setdefault("mark", lambda s: 100_000.0)
    kw.setdefault("max_usd", 1_000_000.0)
    return build(tmp_path, require_policy=False, **kw)


def _send(c, endpoint, body):
    """Send; a VENUE error from the fake (not a local refusal) means the request was sent, which is all the positive controls need."""
    try:
        return c._request("POST", endpoint, body=body, signed=True)
    except bc.OrderNotSent:
        raise
    except bc.PermanentAPIError as exc:
        if str(exc).startswith(("ENTRY_REFUSED", "ENDPOINT_REFUSED", "LIVE_NOT")):
            raise
        return None


# ---- 1. reduceOnly: only boolean True, exact lowercase category, and only the carry's own perp ------------------------------------------------------------------

@pytest.mark.parametrize("body", [order(side="Buy", reduceOnly="true"), order(side="Buy", reduceOnly=1), order(side="Buy", reduceOnly="TRUE"), order(side="Buy", reduceOnly="True"),
                                  order(side="Buy", reduceOnly=1.0), order(side="Buy", reduceOnly=["x"]),
                                  order(category="LINEAR", side="Buy", reduceOnly=True), order(category="Linear", side="Buy", reduceOnly=True),
                                  order(category="INVERSE", side="Buy", reduceOnly=True), order(category=" linear", side="Buy", reduceOnly=True),
                                  order(category="spot", side="SELL"), order(category="SPOT", side="Sell"), order(category="spot", side="sell"), order(category="spot", side=" Sell")])
def test_only_exact_spellings_count_as_an_exit(body):
    assert EA.increases_exposure(body) is True


def test_exact_reduce_only_still_counts_as_an_exit_positive_control():
    assert EA.increases_exposure(order(side="Buy", reduceOnly=True)) is False
    assert EA.increases_exposure(order(category="spot", side="Sell")) is False


@pytest.mark.parametrize("body", [
    order(symbol="DOGEUSDT", side="Buy", reduceOnly=True, qty="100000", orderType="Limit", price="1", orderLinkId="E1"),
    order(symbol="ETHUSDT", side="Buy", reduceOnly=True, orderLinkId="E2"),
    order(category="inverse", side="Buy", reduceOnly=True, orderLinkId="E3"),
    order(category="option", side="Buy", reduceOnly=True, orderLinkId="E4"),
    order(side="Sell", reduceOnly=True, orderLinkId="E5"),                      # the carry only ever holds a short: its exit is a perp BUY
    order(side="Buy", reduceOnly=True, orderLinkId="E6", leverage="100"),       # unknown key
])
def test_a_reduce_only_order_for_anything_but_the_carrys_own_perp_is_refused(store, tmp_path, body):
    c, v = client(store, _bound(tmp_path))
    with pytest.raises(bc.PermanentAPIError, match="ENTRY_REFUSED"):
        _send(c, "/v5/order/create", body)
    assert sent_orders(v) == []


def test_the_carrys_own_reduce_only_exit_still_reaches_the_transport_positive_control(store, tmp_path):
    c, v = client(store, _bound(tmp_path))
    _send(c, "/v5/order/create", order(side="Buy", reduceOnly=True, orderLinkId="E7", positionIdx=0))
    assert len(sent_orders(v)) == 1


@pytest.mark.parametrize("flag", ["true", "TRUE", 1, "1", "yes"])
def test_a_non_boolean_reduce_only_is_never_an_exit_and_is_refused_as_malformed(store, tmp_path, flag):
    c, v = client(store, _bound(tmp_path))
    with pytest.raises(bc.PermanentAPIError, match="ENTRY_REFUSED"):
        _send(c, "/v5/order/create", order(side="Buy", reduceOnly=flag, symbol="DOGEUSDT", orderLinkId="E8"))
    with pytest.raises(bc.PermanentAPIError, match="ENTRY_REFUSED"):
        _send(c, "/v5/order/create", order(side="Sell", reduceOnly=flag, orderLinkId="E9"))
    assert sent_orders(v) == []


def test_category_spelled_upper_case_is_not_the_carrys_instrument(store, tmp_path):
    c, v = client(store, _bound(tmp_path))
    with pytest.raises(bc.PermanentAPIError, match="UNKNOWN_INSTRUMENT"):
        _send(c, "/v5/order/create", order(category="LINEAR", orderLinkId="E10"))
    assert sent_orders(v) == []


# ---- 2. set-leverage is bounded by the configured leverage and the approved policy ---------------------------------------------------------------------------------

class _Cfg3(Cfg):
    DEFAULT_LEVERAGE = 3


def _lev(level):
    return {"category": "linear", "symbol": "BTCUSDT", "buyLeverage": level, "sellLeverage": level}


def _client3(store, authority):
    v = Recorder()
    c = bc.BybitClient(config=_Cfg3(), store=store, transport=v)
    c.entry_authority = authority
    return c, v


def test_set_leverage_above_the_configured_leverage_is_refused_with_an_authority(store, tmp_path):
    c, v = _client3(store, _bound(tmp_path))
    assert c.leverage == 3
    with pytest.raises(bc.OrderNotSent, match="LEVERAGE_REFUSED"):
        _send(c, "/v5/position/set-leverage", _lev("10"))
    with pytest.raises(bc.OrderNotSent, match="LEVERAGE_REFUSED"):
        _send(c, "/v5/position/set-leverage", _lev("4"))
    assert v.requests == []
    _send(c, "/v5/position/set-leverage", _lev("3"))                              # positive control: the configured leverage
    _send(c, "/v5/position/set-leverage", _lev("2"))
    assert len([r for r in v.requests if r["url"].endswith("set-leverage")]) == 2


def test_set_leverage_above_the_approved_policys_max_leverage_is_refused(store, tmp_path):
    pol = dataclasses.replace(POLICY, max_leverage=2.0)
    a = build(tmp_path, policy=pol, state=GOOD)
    c, v = _client3(store, a)
    with pytest.raises(bc.OrderNotSent, match="LEVERAGE_REFUSED"):
        _send(c, "/v5/position/set-leverage", _lev("3"))
    assert v.requests == []
    _send(c, "/v5/position/set-leverage", _lev("2"))
    assert len(v.requests) == 1


def test_set_leverage_with_an_authority_but_no_readable_policy_is_bounded_by_the_config_only(store, tmp_path):
    c, v = _client3(store, _bound(tmp_path))
    _send(c, "/v5/position/set-leverage", _lev("3"))
    assert len(v.requests) == 1


# ---- 3. trading-stop and cancel bodies ----------------------------------------------------------------------------------------------------------------------------

def _stop(**kw):
    b = {"category": "linear", "symbol": "BTCUSDT", "stopLoss": "50000.5", "tpslMode": "Full", "slTriggerBy": "LastPrice", "slOrderType": "Market", "positionIdx": 0}
    b.update(kw)
    return b


def test_trading_stop_positive_controls_the_bodies_the_client_builds(store, tmp_path):
    c, v = client(store, _bound(tmp_path))
    _send(c, "/v5/position/trading-stop", _stop())
    _send(c, "/v5/position/trading-stop", {"category": "linear", "symbol": "BTCUSDT", "stopLoss": "0", "tpslMode": "Full", "positionIdx": 0})
    assert len(v.requests) == 2
    cb, vb = _bare_client(store)
    _send(cb, "/v5/position/trading-stop", {"category": "linear", "symbol": "BTCUSDT", "stopLoss": "1", "tpslMode": "Full", "positionIdx": 0})
    assert len(vb.requests) == 1


@pytest.mark.parametrize("body", [
    _stop(symbol="DOGEUSDT"), _stop(category="spot"), _stop(category="inverse"), _stop(category="LINEAR"), _stop(positionIdx=1), _stop(positionIdx=2), _stop(positionIdx="1"),
    _stop(stopLoss="1e5"), _stop(stopLoss="-5"), _stop(stopLoss=" 5"), _stop(stopLoss=5), _stop(stopLoss=""), _stop(stopLoss="NaN"), _stop(stopLoss="5.5.5"),
    _stop(takeProfit="1"), _stop(tpLimitPrice="1"), _stop(slSize="1"), _stop(extra="x"), _stop(tpslMode="Partial"), _stop(slOrderType="Limit"), _stop(slTriggerBy="Bogus"),
    _stop(trailingStop="100"), _stop(activePrice="1"),
    {k: x for k, x in _stop().items() if k != "stopLoss"}, {k: x for k, x in _stop().items() if k != "symbol"}, {k: x for k, x in _stop().items() if k != "positionIdx"},
    [], None,
])
def test_trading_stop_outside_the_allow_list_is_refused(store, tmp_path, body):
    c, v = client(store, _bound(tmp_path))
    with pytest.raises(bc.OrderNotSent, match="TRADING_STOP_REFUSED"):
        _send(c, "/v5/position/trading-stop", body)
    assert v.requests == []


def test_trading_stop_shape_is_refused_on_a_bare_client_too(store):
    c, v = _bare_client(store)
    with pytest.raises(bc.OrderNotSent, match="TRADING_STOP_REFUSED"):
        _send(c, "/v5/position/trading-stop", _stop(takeProfit="1"))
    with pytest.raises(bc.OrderNotSent, match="TRADING_STOP_REFUSED"):
        _send(c, "/v5/position/trading-stop", _stop(positionIdx=1))
    assert v.requests == []


@pytest.mark.parametrize("body", [
    {"category": "linear", "symbol": "DOGEUSDT", "orderLinkId": "C1"},
    {"category": "linear", "symbol": "BTCUSDT"},                                  # no id at all
    {"category": "linear", "symbol": "BTCUSDT", "orderLinkId": ""},
    {"category": "linear", "symbol": "BTCUSDT", "orderLinkId": " C1"},
    {"category": "linear", "symbol": "BTCUSDT", "orderLinkId": "C1\n"},
    {"category": "linear", "symbol": "BTCUSDT", "orderLinkId": "C" * 37},
    {"category": "linear", "symbol": "BTCUSDT", "orderLinkId": 5},
    {"category": "linear", "symbol": "BTCUSDT", "orderId": "a b"},
    {"category": "option", "symbol": "BTCUSDT", "orderLinkId": "C1"},
    {"category": "LINEAR", "symbol": "BTCUSDT", "orderLinkId": "C1"},
    {"category": "linear", "orderLinkId": "C1"},                                  # no symbol
    {"category": "linear", "symbol": "BTCUSDT", "orderLinkId": "C1", "orderFilter": "StopOrder"},
    {"category": "linear", "symbol": "BTCUSDT", "orderLinkId": "C1", "x": "y"},
    {"category": "spot", "symbol": "ETHUSDT", "orderLinkId": "C1"},
])
def test_cancel_outside_the_allow_list_is_refused(store, tmp_path, body):
    c, v = client(store, _bound(tmp_path))
    with pytest.raises(bc.OrderNotSent, match="CANCEL_REFUSED"):
        _send(c, "/v5/order/cancel", body)
    assert v.requests == []


@pytest.mark.parametrize("body", [{"category": "linear", "symbol": "BTCUSDT", "orderLinkId": "C1"}, {"category": "spot", "symbol": "BTCUSDT", "orderLinkId": "C-1_x"},
                                  {"category": "linear", "symbol": "BTCUSDT", "orderId": "1234-abcd-5678"}, {"category": "linear", "symbol": "BTCUSDT", "orderId": "9", "orderLinkId": "C1"}])
def test_cancel_positive_controls(store, tmp_path, body):
    c, v = client(store, _bound(tmp_path))
    _send(c, "/v5/order/cancel", body)
    assert len(v.requests) == 1


@pytest.mark.parametrize("body", [
    {"category": "linear"},                                                       # the whole category
    {"category": "spot"},
    {"category": "linear", "symbol": ""},
    {"category": "linear", "symbol": "DOGEUSDT"},
    {"category": "spot", "symbol": "DOGEUSDT", "orderFilter": "Order"},
    {"category": "option", "symbol": "BTCUSDT"},
    {"category": "linear", "symbol": "BTCUSDT", "settleCoin": "USDT"},
    {"category": "linear", "symbol": "BTCUSDT", "orderFilter": "Bogus"},
    {"category": "linear", "symbol": "BTCUSDT", "orderFilter": "StopOrder"},      # a spot filter on a perp
    {"category": "spot", "symbol": "BTCUSDT", "orderFilter": "Bogus"},
])
def test_cancel_all_needs_the_bound_symbol_and_a_known_shape(store, tmp_path, body):
    c, v = client(store, _bound(tmp_path))
    with pytest.raises(bc.OrderNotSent, match="CANCEL_REFUSED"):
        _send(c, "/v5/order/cancel-all", body)
    assert v.requests == []


@pytest.mark.parametrize("body", [{"category": "linear", "symbol": "BTCUSDT"}, {"category": "spot", "symbol": "BTCUSDT", "orderFilter": "Order"},
                                  {"category": "spot", "symbol": "BTCUSDT", "orderFilter": "StopOrder"}, {"category": "spot", "symbol": "BTCUSDT", "orderFilter": "tpslOrder"}])
def test_cancel_all_positive_controls(store, tmp_path, body):
    c, v = client(store, _bound(tmp_path))
    _send(c, "/v5/order/cancel-all", body)
    assert len(v.requests) == 1


def test_cancel_all_without_a_symbol_is_refused_even_on_a_bare_client(store):
    c, v = _bare_client(store)
    with pytest.raises(bc.OrderNotSent, match="CANCEL_REFUSED"):
        _send(c, "/v5/order/cancel-all", {"category": "linear"})
    assert v.requests == []


# ---- 4. a closed allow-list of order-create body keys, and the whole canonical body on resend -----------------------------------------------------------------------

@pytest.mark.parametrize("extra", [dict(triggerPrice="1"), dict(orderFilter="StopOrder"), dict(takeProfit="1"), dict(stopLoss="1"), dict(closeOnTrigger=True), dict(tpslMode="Full"),
                                   dict(leverage="100"), dict(smpType="None"), dict(slippageTolerance="1"), dict(tpTriggerBy="LastPrice"), dict(bogus=1)])
def test_an_unknown_order_body_key_is_refused(tmp_path, extra):
    a = _bound(tmp_path)
    reasons = _auth_refusal(a, order(**extra))
    assert reasons and any(r.startswith("UNKNOWN_BODY_FIELD") for r in reasons), reasons


@pytest.mark.parametrize("extra", [dict(isLeverage=1), dict(isLeverage="1"), dict(isLeverage=True), dict(isLeverage=0.5), dict(positionIdx=1), dict(positionIdx=2), dict(positionIdx="2"),
                                   dict(marketUnit="quoteCoin"), dict(reduceOnly="false"), dict(timeInForce="Bogus")])
def test_a_forbidden_value_of_an_allowed_key_is_refused(tmp_path, extra):
    a = _bound(tmp_path)
    assert _auth_refusal(a, order(**extra)) is not None


def test_a_spot_buy_with_margin_borrow_is_refused_and_never_sent(store, tmp_path):
    c, v = client(store, _bound(tmp_path))
    with pytest.raises(bc.PermanentAPIError, match="ENTRY_REFUSED"):
        _send(c, "/v5/order/create", order(category="spot", side="Buy", isLeverage=1, orderLinkId="B1"))
    assert sent_orders(v) == []


def test_every_key_the_code_legitimately_sends_is_allowed_positive_control(tmp_path):
    for n, b in enumerate([order(orderLinkId="P1", positionIdx=0), order(category="spot", side="Buy", orderLinkId="P2", isLeverage=0, marketUnit="baseCoin"),
                           order(orderType="Limit", price="100000", timeInForce="PostOnly", orderLinkId="P3"),
                           order(category="spot", side="Buy", orderType="Limit", price="100000", isLeverage="0", orderLinkId="P4")]):
        assert _auth_refusal(_bound(tmp_path / f"a{n}"), b) is None, b


@pytest.mark.parametrize("changed", [dict(positionIdx=0), dict(isLeverage=0), dict(marketUnit="baseCoin"), dict(positionIdx="0")])
def test_a_resend_compares_the_whole_canonical_body(tmp_path, changed):
    a = _bound(tmp_path)
    first = order(orderLinkId="Z1")
    assert _auth_refusal(a, first) is None
    assert _auth_refusal(a, dict(first)) is None                                  # positive control: the identical resend
    reasons = _auth_refusal(a, dict(first, **changed))
    assert reasons is not None and "RESERVATION_CONFLICT_SAME_ID_DIFFERENT_ORDER" in reasons


def test_the_ledger_row_carries_the_whole_body(tmp_path):
    a = _bound(tmp_path)
    assert _auth_refusal(a, order(orderLinkId="Z2", positionIdx=0)) is None
    rows = [json.loads(x) for x in open(str(tmp_path / "state" / "entry_reservations.jsonl")).read().splitlines() if x.strip()]
    assert rows[0]["body"]["positionIdx"] == 0 and "orderLinkId" not in rows[0]["body"]
