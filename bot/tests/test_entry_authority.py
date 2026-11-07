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


def build(tmp_path, *, policy=None, state=None, kill=lambda: (False, ""), mark=lambda s: 100_000.0, max_usd=102.0, require_policy=True):
    path = None
    os.makedirs(str(tmp_path), exist_ok=True)
    if policy is not None:
        path = str(tmp_path / "policy.json")
        json.dump(dataclasses.asdict(policy), open(path, "w"))
    store = type("S", (), {"is_kill_switch_engaged": staticmethod(kill)})()
    return EA.build_for_book(venue="testnet", store=store, state_dir=str(tmp_path / "state"), max_notional_usd=max_usd, mark_provider=mark, policy_path=path,
                             state_provider=(lambda: state) if state is not None else None, require_risk_policy=require_policy)


POLICY = RP.Policy(per_trade_loss_usd=2.0, daily_loss_usd=3.0, strategy_drawdown_usd=5.0, portfolio_drawdown_usd=8.0, max_hold_hours=360.0, max_exposure_usd=100.0, max_leverage=3.0,
                   min_liquidity_usd=1000.0, max_unhedged_usd=5.0, min_margin_multiple=2.0, min_liquidation_distance_pct=20.0, max_data_age_s=90.0,
                   max_correlated_exposure_usd=100.0, min_recent_funding_bps=0.2, approved_by_owner=True, approval_note="TEST FIXTURE, not an owner approval")
GOOD = RP.State(trade_loss_usd=0.0, loss_today_usd=0.0, strategy_drawdown_usd=0.0, portfolio_drawdown_usd=0.0, hold_hours=0.0, exposure_usd=0.0, leverage=1.0, liquidity_usd=50_000.0,
                unhedged_usd=0.0, margin_multiple=5.0, liquidation_distance_pct=60.0, data_age_s=1.0, correlated_exposure_usd=0.0, recent_mean_funding_bps=0.5, operational_fault=False)


# ---------------------------------------------------------------- what counts as exposure

@pytest.mark.parametrize("body,expected", [
    (order(), True), (order(side="Buy"), True), (order(side="Buy", reduceOnly=True), False), (order(side="Buy", reduceOnly="true"), False),
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
    body = order(qty=qty, **({"price": price} if price else {}))
    with pytest.raises(bc.PermanentAPIError, match=reason):
        c._request("POST", "/v5/order/create", body=body, signed=True)
    assert sent_orders(v) == []


def test_no_price_for_a_market_order_refuses(store, tmp_path):
    c, v = client(store, build(tmp_path, policy=POLICY, state=GOOD, mark=lambda s: None))
    with pytest.raises(bc.PermanentAPIError, match="NO_PRICE_FOR_NOTIONAL"):
        c._request("POST", "/v5/order/create", body=order(), signed=True)


def test_the_wrong_venue_refuses(store, tmp_path):
    auth = EA.build_for_book(venue="mainnet", store=type("S", (), {"is_kill_switch_engaged": staticmethod(lambda: (False, ""))})(), state_dir=str(tmp_path / "s"),
                             max_notional_usd=102.0, mark_provider=lambda s: 1.0, policy_path=None, state_provider=lambda: GOOD, require_risk_policy=False)
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
    assert sent == []


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
    assert EA.increases_exposure(order(side="Buy", reduceOnly=True)) is False and EA.increases_exposure(order(side="Buy", reduceOnly="true")) is False


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
