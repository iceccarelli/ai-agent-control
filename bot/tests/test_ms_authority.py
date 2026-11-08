"""ms_authority: the multi-symbol entry authority at the final venue-request boundary. Offline: the transport is an in-process recorder, the state dir is a tmp dir, the kill switch,
clock, account state and market snapshots are injected. Nothing here can send a request, and nothing constructs a real or testnet transport that is used."""
from __future__ import annotations

import copy
import json
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(BOT, "tools"))

import backtest  # noqa: E402
import bybit_connection as bc  # noqa: E402
import entry_authority as EA  # noqa: E402
import ms_authority as MA  # noqa: E402
import ms_risk  # noqa: E402
import ms_testnet_adapter as AD  # noqa: E402
import runtime_capability as RC  # noqa: E402
from fake_bybit import FakeBybit  # noqa: E402
from ms_types import LegPlan, MarketSnapshot, OrderGroupPlan  # noqa: E402
from persistence import StateStore  # noqa: E402

NOW = 1_800_000_000_000


@MA.register_simulated_transport
class SimT(bc.Transport):
    """An in-process simulation that records and answers nothing real. Accepted because its EXACT class is registered (the marker alone grants nothing)."""
    IN_PROCESS_SIMULATION = True

    def __init__(self):
        self.requests = []

    def request(self, method, url, *, headers, params=None, body=None, timeout=10.0):
        self.requests.append((method, url, body))
        return 200, json.dumps({"retCode": 0, "retMsg": "OK", "result": {"orderId": "1", "orderLinkId": "x"}})


def pol(**kw):
    p = {"policy_version": "pv1", "candidate_id": "cand1", "spec_digest": "d" * 64, "venue": "testnet", "allowed_symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
         "allowed_sides": {"linear": ["Sell", "Buy"], "spot": ["Buy", "Sell"]}, "allowed_order_types": ["Limit", "Market"], "max_notional_per_symbol_usd": 2000.0,
         "max_aggregate_notional_usd": 3000.0, "max_gross_exposure_usd": 6000.0, "max_net_exposure_usd": 500.0, "max_active_groups": 3, "max_unhedged_usd": 1200.0,
         "max_leverage": 3.0, "max_market_age_ms": 5000, "max_account_age_ms": 10000, "execution_styles": ["MAKER_FIRST", "TAKER"]}
    p.update(kw)
    return p


class Env:
    """Mutable world: account state, snapshots, kill switch, clock."""

    def __init__(self):
        self.now = NOW
        self.state = {"equity_usd": 10000.0, "positions": {}, "open_groups": 0, "market_ts_ms": NOW - 100, "account_ts_ms": NOW - 100}
        self.snaps = {s: MarketSnapshot(symbol=s, decision_ms=NOW, mark=100.0, spot_mark=100.0, book_age_ms=100) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT")}
        self.kill = (False, "")
        self.fail_state = False

    def state_provider(self):
        if self.fail_state:
            raise RuntimeError("down")
        return self.state

    def refresh(self):
        self.state["market_ts_ms"] = self.now - 100
        self.state["account_ts_ms"] = self.now - 100


@pytest.fixture()
def env():
    return Env()


def make(env, tmp_path, policy=None, transport=None, hedge=None):
    t = transport or SimT()
    a = MA.MultiSymbolAuthority(policy=policy or pol(), transport=t, state_dir=str(tmp_path / "st"), strategy="multi_symbol_funding_carry", state_provider=env.state_provider,
                                snapshot_provider=lambda: env.snaps, kill_switch_reader=lambda: env.kill, clock=lambda: env.now / 1000.0, hedge_reader=hedge)
    return a, t


def leg(sym="BTCUSDT", cat="linear", side="Sell", qty="10", lid="P", ot="Market", price=None, ro=False, tif="GTC", post=False):
    return LegPlan(leg_id=lid, symbol=sym, category=cat, side=side, qty=qty, order_type=ot, price=price, reduce_only=ro, time_in_force=tif, post_only=post)


def pair(gid="G1", sym="BTCUSDT", qty="10"):
    return OrderGroupPlan(group_id=gid, candidate_id="cand1", spec_digest="d" * 64, policy_version="pv1", created_ms=NOW, style="TAKER", max_wait_ms=1000, notional_usd=2000.0,
                          legs=(leg(sym, "linear", "Sell", qty, "P"), leg(sym, "spot", "Buy", qty, "S")))


def body(gid="G1", lid="P", sym="BTCUSDT", cat="linear", side="Sell", qty="10", **kw):
    b = {"category": cat, "symbol": sym, "side": side, "orderType": "Market", "qty": qty, "orderLinkId": f"{gid}-{lid}"}
    b.update(kw)
    return b


def refused(a, b, venue="testnet"):
    with pytest.raises(EA.EntryRefused) as ei:
        a.authorize(b, venue=venue)
    return ei.value.reasons


def reg(a, plan=None):
    d = a.register_group(plan or pair())
    assert d.approved, d.reasons
    return d


# ================================================================ construction: no policy, no authority
def test_no_policy_no_authority(env, tmp_path):
    with pytest.raises(MA.AuthorityConfigError, match="NO_APPROVED_POLICY"):
        MA.MultiSymbolAuthority(policy=None, transport=SimT(), state_dir=str(tmp_path), strategy="s", state_provider=env.state_provider,
                                snapshot_provider=lambda: env.snaps, kill_switch_reader=lambda: (False, ""))


@pytest.mark.parametrize("kw", [dict(allowed_symbols=["*"]), dict(allowed_symbols=["ALL"]), dict(venue="mainnet"), dict(venue="eu"), dict(venue="Testnet"), dict(extra=1),
                                dict(max_leverage=None), dict(execution_styles=[])])
def test_bad_policy_cannot_build_an_authority(env, tmp_path, kw):
    with pytest.raises(MA.AuthorityConfigError, match="POLICY_REFUSED"):
        make(env, tmp_path, policy=pol(**kw))


def test_missing_policy_key_cannot_build(env, tmp_path):
    p = pol()
    del p["max_unhedged_usd"]
    with pytest.raises(MA.AuthorityConfigError):
        make(env, tmp_path, policy=p)


def test_providers_strategy_and_transport_required(env, tmp_path):
    base = dict(policy=pol(), state_dir=str(tmp_path), state_provider=env.state_provider, snapshot_provider=lambda: env.snaps, kill_switch_reader=lambda: (False, ""))
    for kw in (dict(strategy=""), dict(strategy=" x"), dict(strategy="s", transport=None), dict(strategy="s", transport=SimT(), kill_switch_reader=None),
               dict(strategy="s", transport=SimT(), state_dir="")):
        a = {**base, "transport": SimT(), **kw}
        with pytest.raises(MA.AuthorityConfigError):
            MA.MultiSymbolAuthority(**a)


def test_the_callers_policy_dict_is_copied(env, tmp_path):
    p = pol()
    a, _ = make(env, tmp_path, policy=p)
    p["max_net_exposure_usd"] = 1e12
    p["allowed_symbols"].append("DOGEUSDT")
    assert a.policy["max_net_exposure_usd"] == 500.0 and "DOGEUSDT" not in a.policy["allowed_symbols"]


# ================================================================ the transport can only be a simulation (or the disabled testnet capability)
def test_authority_binds_to_simulated_transports(env, tmp_path):
    for t in (SimT(), backtest.SimulatedExchange({})):
        a, _ = make(env, tmp_path, transport=t)
        assert a.mode == "SIMULATED" and a.bound_transport is t


def test_real_http_transport_is_refused(env, tmp_path):
    with pytest.raises(MA.AuthorityConfigError, match="TRANSPORT_NOT_SIMULATED"):
        make(env, tmp_path, transport=bc.RequestsTransport())


def test_arbitrary_or_unmarked_transports_are_refused(env, tmp_path):
    class Unmarked(bc.Transport):
        pass

    class InstanceMarked(bc.Transport):
        def __init__(self):
            self.IN_PROCESS_SIMULATION = True               # instance-level claims do not count

    for t in (FakeBybit(), Unmarked(), InstanceMarked(), object()):
        with pytest.raises(MA.AuthorityConfigError, match="TRANSPORT_NOT_SIMULATED"):
            make(env, tmp_path, transport=t)


def test_a_simulation_subclass_that_holds_a_real_session_is_refused(env, tmp_path):
    class Sneaky(bc.RequestsTransport):
        IN_PROCESS_SIMULATION = True
    with pytest.raises(MA.AuthorityConfigError):
        make(env, tmp_path, transport=Sneaky())


def test_each_real_transport_guard_stands_on_its_own(env, tmp_path):
    class HoldsSession(bc.Transport):                       # not a RequestsTransport, but it carries a pooled HTTP session
        IN_PROCESS_SIMULATION = True

        def __init__(self):
            self._session = object()

    class StrippedRequests(bc.RequestsTransport):           # IS a RequestsTransport, session removed, marker claimed
        IN_PROCESS_SIMULATION = True

        def __init__(self):
            pass

    for t in (HoldsSession(), StrippedRequests()):
        with pytest.raises(MA.AuthorityConfigError, match="TRANSPORT_NOT_SIMULATED"):
            make(env, tmp_path, transport=t)


def test_capability_wrapped_simulation_is_not_a_simulation(env, tmp_path):
    cap = RC.Capability.for_role(RC.TESTNET_EXECUTOR, venue="testnet", credential_class=RC.TESTNET_CREDENTIAL_CLASS)
    with pytest.raises(MA.AuthorityConfigError, match="TESTNET_CAPABILITY_DISABLED"):
        make(env, tmp_path, transport=RC.CapabilityTransport(SimT(), cap))


def _wrapped(venue="testnet", role=RC.TESTNET_EXECUTOR):
    inner = SimT()
    cap = RC.Capability.for_role(role, venue=venue, credential_class=RC.TESTNET_CREDENTIAL_CLASS if role == RC.TESTNET_EXECUTOR else None)
    return RC.CapabilityTransport(inner, cap), inner


def _build_testnet(env, tmp_path, *, policy=None, transport=None, binding="default"):
    t = transport or _wrapped()[0]
    b = MA.TestnetBinding("BYBIT_TESTNET_DEDICATED_API_KEY") if binding == "default" else binding
    return MA.MultiSymbolAuthority(policy=policy or pol(), transport=t, state_dir=str(tmp_path / "st"), strategy="s", state_provider=env.state_provider,
                                   snapshot_provider=lambda: env.snaps, kill_switch_reader=lambda: env.kill, testnet=b)


def test_testnet_requires_the_disabled_by_default_capability(env, tmp_path, monkeypatch):
    assert AD.enabled() is False
    with pytest.raises(MA.AuthorityConfigError, match="TESTNET_CAPABILITY_DISABLED"):
        _build_testnet(env, tmp_path)


def test_testnet_all_three_conditions_still_need_the_rest(env, tmp_path, monkeypatch):
    monkeypatch.setattr(AD, "enabled", lambda path=None: True)
    # positive control: flag + sandbox testnet venue + dedicated name + capability transport -> constructs; and sends nothing
    t, inner = _wrapped()
    a = _build_testnet(env, tmp_path, transport=t)
    assert a.mode == "TESTNET" and a.bound_transport is t and inner.requests == []
    for kw, code in ((dict(binding=None), "TESTNET_BINDING_MISSING"), (dict(binding=MA.TestnetBinding("BYBIT_API_KEY")), "CREDENTIAL_NAME_GENERIC"),
                     (dict(binding=MA.TestnetBinding("BYBIT_API_SECRET")), "CREDENTIAL_NAME_GENERIC"), (dict(binding=MA.TestnetBinding("BYBIT_MAINNET_KEY")), "CREDENTIAL_NAME_NOT_TESTNET"),
                     (dict(binding=MA.TestnetBinding("BYBIT_TESTNET_LIVE_KEY")), "CREDENTIAL_NAME_NON_TESTNET_WORD"), (dict(binding=MA.TestnetBinding("x")), "CREDENTIAL_NAME_INVALID"),
                     (dict(policy=pol(venue="demo")), "VENUE_NOT_TESTNET"), (dict(transport=bc.RequestsTransport()), "TRANSPORT_NOT_TESTNET_EXECUTOR_CAPABILITY"),
                     (dict(transport=_wrapped(role=RC.READ_ONLY_MONITOR)[0]), "TRANSPORT_NOT_TESTNET_EXECUTOR_CAPABILITY"),
                     (dict(transport=FakeBybit()), "TRANSPORT_NOT_TESTNET_EXECUTOR_CAPABILITY")):
        with pytest.raises(MA.AuthorityConfigError, match=code):
            _build_testnet(env, tmp_path, **kw)


def test_mainnet_policy_can_never_reach_any_transport(env, tmp_path, monkeypatch):
    monkeypatch.setattr(AD, "enabled", lambda path=None: True)
    for t in (SimT(), _wrapped()[0]):
        with pytest.raises(MA.AuthorityConfigError, match="POLICY_VENUE"):
            MA.MultiSymbolAuthority(policy=pol(venue="mainnet"), transport=t, state_dir=str(tmp_path), strategy="s", state_provider=env.state_provider,
                                    snapshot_provider=lambda: env.snaps, kill_switch_reader=lambda: (False, ""), testnet=MA.TestnetBinding("BYBIT_TESTNET_DEDICATED_API_KEY"))


class _Cfg:
    BYBIT_VENUE = "testnet"
    USE_TESTNET = True
    PAPER_TRADING = False
    LIVE_TRADING_ACK = ""
    BYBIT_API_KEY = "test-key"
    BYBIT_API_SECRET = "test-secret"
    BYBIT_RECV_WINDOW_MS = 5000
    REQUEST_TIMEOUT_SECONDS = 5.0
    USE_LEVERAGE = False
    SYMBOL_FILTERS_TTL = 3600
    ORDERLINK_PREFIX = "BB"
    STATE_DB_PATH = ""


@pytest.fixture()
def client_factory(tmp_path):
    stores = []

    def f(transport):
        s = StateStore(str(tmp_path / f"s{len(stores)}.db"))
        stores.append(s)
        return bc.BybitClient(config=_Cfg(), store=s, transport=transport)
    yield f
    for s in stores:
        s.close()


def test_install_only_on_the_bound_transport_and_the_policy_venue(env, tmp_path, client_factory):
    a, t = make(env, tmp_path)
    c = client_factory(t)
    MA.install(c, a)
    assert c.entry_authority is a
    with pytest.raises(MA.AuthorityConfigError, match="BOUND_TRANSPORT"):
        MA.install(client_factory(SimT()), a)                                    # a different simulated transport
    with pytest.raises(MA.AuthorityConfigError, match="BOUND_TRANSPORT"):
        MA.install(client_factory(FakeBybit()), a)
    with pytest.raises(MA.AuthorityConfigError, match="NOT_A_MULTI_SYMBOL"):
        MA.install(c, object())
    a2, t2 = make(env, tmp_path, policy=pol(venue="demo"))
    with pytest.raises(MA.AuthorityConfigError, match="POLICY_VENUE"):
        MA.install(client_factory(t2), a2)


def test_authority_is_not_wired_anywhere_by_default():
    for name in ("main.py", "bybit_connection.py", "entry_authority.py", "carry_engine.py", "carry_broker.py", "config.py", "runtime_capability.py", "persistence.py"):
        p = os.path.join(BOT, name)
        if os.path.exists(p):
            assert not re.search(r"ms_authority|MultiSymbolAuthority|ms_testnet_adapter", open(p).read()), name
    assert bc.DEFAULT_ENTRY_AUTHORITY is None or type(bc.DEFAULT_ENTRY_AUTHORITY).__name__ == "_PermissiveForTests"


# ================================================================ registration and the entry decision
def test_positive_control_pair_is_registered_and_both_legs_authorised(env, tmp_path):
    a, _ = make(env, tmp_path)
    reg(a)
    i1 = a.authorize(body(lid="P"), venue="testnet")
    i2 = a.authorize(body(lid="S", cat="spot", side="Buy"), venue="testnet")
    assert i1.order_link_id == "G1-P" and i2.order_link_id == "G1-S" and a.refusals == []
    rows = [json.loads(l) for l in open(tmp_path / "st" / "ms_reservations.jsonl")]
    assert {r["link"] for r in rows} == {"G1-P", "G1-S"}


def test_identical_resend_is_allowed_a_changed_resend_is_a_conflict(env, tmp_path):
    a, _ = make(env, tmp_path)
    p = pair()
    p = OrderGroupPlan(**{**p.__dict__, "legs": (leg("BTCUSDT", "linear", "Sell", "10", "P", ot="Limit", price="100", tif="GTC"), p.legs[1])})
    reg(a, p)
    b = body(lid="P", orderType="Limit", price="100", timeInForce="GTC")
    a.authorize(b, venue="testnet")
    a.authorize(dict(b), venue="testnet")                                            # lost ack -> same order again
    b2 = {k: v for k, v in b.items() if k != "timeInForce"}                          # same plan leg, different wire body
    assert "RESERVATION_CONFLICT_SAME_ID_DIFFERENT_ORDER" in refused(a, b2)


def test_unregistered_group_and_leg_are_refused(env, tmp_path):
    a, _ = make(env, tmp_path)
    assert refused(a, body()) == ("GROUP_NOT_REGISTERED",)
    reg(a)
    assert refused(a, body(lid="Z")) == ("LEG_NOT_IN_GROUP",)
    assert refused(a, body(gid="G2")) == ("GROUP_NOT_REGISTERED",)


@pytest.mark.parametrize("link", ["G1P", "G1-P-X", "-P", "G1-", "G 1-P", "G1--P", "G1.P"])
def test_order_link_id_must_be_group_dash_leg(env, tmp_path, link):
    a, _ = make(env, tmp_path)
    reg(a)
    b = body()
    b["orderLinkId"] = link
    with pytest.raises(EA.EntryRefused):
        a.authorize(b, venue="testnet")


def test_malformed_bodies_refused_by_the_shared_grammar(env, tmp_path):
    a, _ = make(env, tmp_path)
    reg(a)
    for b in (body(qty="1e1"), body(qty=10), {k: v for k, v in body().items() if k != "orderLinkId"}, body(orderType="StopMarket"), body(extra=1)):
        with pytest.raises(EA.EntryRefused):
            a.authorize(b, venue="testnet")


@pytest.mark.parametrize("mut,code", [({"qty": "11"}, "BODY_FIELD_NOT_PLAN:qty"), ({"symbol": "ETHUSDT"}, "BODY_FIELD_NOT_PLAN:symbol"), ({"side": "Buy"}, "BODY_FIELD_NOT_PLAN:side"),
                                      ({"category": "spot"}, "BODY_FIELD_NOT_PLAN:category"), ({"orderType": "Limit", "price": "1"}, "BODY_FIELD_NOT_PLAN:orderType"),
                                      ({"price": "100"}, "BODY_FIELD_NOT_PLAN:price"), ({"reduceOnly": True}, "BODY_FIELD_NOT_PLAN:reduceOnly")])
def test_body_must_equal_the_registered_leg(env, tmp_path, mut, code):
    a, _ = make(env, tmp_path)
    reg(a)
    b = body()
    b.update(mut)
    reasons = refused(a, b)
    if code in ("BODY_FIELD_NOT_PLAN:qty", "BODY_FIELD_NOT_PLAN:symbol", "BODY_FIELD_NOT_PLAN:side"):
        assert code in reasons                                                      # the exact mismatch is named
    assert reasons


@pytest.mark.parametrize("extra", [{"isLeverage": 1}, {"isLeverage": 0}, {"positionIdx": 1}, {"positionIdx": 0}, {"marketUnit": "quoteCoin"}, {"triggerPrice": "1"}, {"orderFilter": "StopOrder"},
                                   {"takeProfit": "1"}, {"stopLoss": "1"}, {"closeOnTrigger": True}, {"tpslMode": "Full"}, {"smpType": "None"}])
def test_nothing_the_plan_does_not_state_may_ride_along(env, tmp_path, extra):
    a, _ = make(env, tmp_path)
    reg(a)
    b = body()
    b.update(extra)
    assert refused(a, b)


def test_cross_symbol_order_under_another_legs_link_is_refused(env, tmp_path):
    """Re-using an approved link id for a different symbol (the bypass the final boundary exists for)."""
    a, _ = make(env, tmp_path)
    reg(a)
    for sym in ("ETHUSDT", "DOGEUSDT"):
        assert "BODY_FIELD_NOT_PLAN:symbol" in refused(a, body(sym=sym))


def test_wildcard_or_unlisted_symbol_cannot_be_registered(env, tmp_path):
    a, _ = make(env, tmp_path)
    d = a.register_group(pair("G9", "DOGEUSDT"))
    assert not d.approved and "SYMBOL_NOT_ALLOWED:DOGEUSDT" in d.reasons
    assert refused(a, body(gid="G9", sym="DOGEUSDT")) == ("GROUP_NOT_REGISTERED",)


@pytest.mark.parametrize("venue", ["mainnet", "eu", "Testnet", "testnet ", "demo", "", None, "live"])
def test_only_the_exact_policy_sandbox_venue_is_accepted(env, tmp_path, venue):
    a, _ = make(env, tmp_path)
    reg(a)
    assert any(r.startswith("VENUE_NOT_ALLOWED") for r in refused(a, body(), venue=venue))


def test_mainnet_and_eu_are_refused_even_if_a_client_names_them(env, tmp_path, client_factory):
    a, t = make(env, tmp_path)
    reg(a)
    c = client_factory(t)
    MA.install(c, a)
    c.venue = "mainnet"
    with pytest.raises(bc.OrderNotSent, match="VENUE_NOT_ALLOWED"):
        c._authorize_new_exposure(body())
    c.venue = "eu"
    with pytest.raises(bc.OrderNotSent, match="VENUE_NOT_ALLOWED"):
        c._authorize_new_exposure(body())


def test_kill_switch_engaged_or_unreadable_refuses_entries(env, tmp_path):
    a, _ = make(env, tmp_path)
    reg(a)
    env.kill = (True, "operator")
    assert refused(a, body()) == ("KILL_SWITCH_ENGAGED",)
    a2, _ = make(env, tmp_path / "x")
    a2._kill = lambda: (_ for _ in ()).throw(OSError("boom"))
    assert refused(a2, body()) == ("KILL_SWITCH_UNREADABLE",)


def test_stale_account_entry_refused_at_the_boundary_even_if_registered_fresh(env, tmp_path):
    a, _ = make(env, tmp_path)
    reg(a)
    env.now += 11_000                                                               # no refresh: the account read is now older than max_account_age_ms
    env.state["market_ts_ms"] = env.now - 100
    reasons = refused(a, body())
    assert "RISK:ACCOUNT_DATA_STALE" in reasons
    env.refresh()
    a.authorize(body(), venue="testnet")                                            # fresh again


def test_stale_market_and_unknown_state_refused(env, tmp_path):
    a, _ = make(env, tmp_path)
    reg(a)
    env.state["market_ts_ms"] = NOW - 6000
    assert "RISK:MARKET_DATA_STALE" in refused(a, body())
    env.state["market_ts_ms"] = NOW - 100
    env.state["account_ts_ms"] = None
    assert "RISK:ACCOUNT_TS_UNKNOWN" in refused(a, body())
    env.refresh()
    env.fail_state = True
    assert refused(a, body()) == ("RISK:STATE_UNAVAILABLE",)


def test_stale_account_cannot_even_register(env, tmp_path):
    a, _ = make(env, tmp_path)
    env.state["account_ts_ms"] = NOW - 20_000
    d = a.register_group(pair())
    assert not d.approved and "ACCOUNT_DATA_STALE" in d.reasons
    assert not os.path.exists(tmp_path / "st" / "ms_groups.jsonl") or open(tmp_path / "st" / "ms_groups.jsonl").read() == ""


def test_policy_digest_binding_plan_identity(env, tmp_path):
    a, _ = make(env, tmp_path)
    p = pair()
    d = a.register_group(OrderGroupPlan(**{**p.__dict__, "candidate_id": "other"}))
    assert not d.approved and "PLAN_CANDIDATE_ID_MISMATCH" in d.reasons
    d = a.register_group(OrderGroupPlan(**{**p.__dict__, "spec_digest": "e" * 64}))
    assert not d.approved and "PLAN_SPEC_DIGEST_MISMATCH" in d.reasons
    d = a.register_group(OrderGroupPlan(**{**p.__dict__, "policy_version": "pv0"}))
    assert not d.approved and "PLAN_POLICY_VERSION_MISMATCH" in d.reasons
    assert a.policy_digest == MA.policy_digest(pol()) and len(a.policy_digest) == 64


def test_group_id_reuse_with_a_different_plan_is_refused_and_identical_is_idempotent(env, tmp_path):
    a, _ = make(env, tmp_path)
    reg(a)
    assert a.register_group(pair()).approved
    d = a.register_group(pair(qty="5"))
    assert not d.approved and d.reasons == ("GROUP_ID_REUSED_DIFFERENT_PLAN",)


def test_invalid_ids_and_exit_legs_cannot_be_registered(env, tmp_path):
    a, _ = make(env, tmp_path)
    assert "GROUP_ID_INVALID" in a.register_group(pair("G-1")).reasons
    long_leg = OrderGroupPlan(**{**pair("g" * 24).__dict__, "legs": (leg("BTCUSDT", "linear", "Sell", "10", "l" * 24),)})
    assert any(r.startswith("LINK_ID_TOO_LONG") for r in a.register_group(long_leg).reasons)
    p = pair()
    exit_leg = OrderGroupPlan(**{**p.__dict__, "legs": (leg("BTCUSDT", "linear", "Buy", "10", "X", ro=True),)})
    assert any(r.startswith("EXIT_LEG_IN_ENTRY_GROUP") for r in a.register_group(exit_leg).reasons)


# ================================================================ exposure ceilings across symbols and groups
def test_aggregate_ceiling_across_groups_with_every_symbol_under_its_cap(env, tmp_path):
    a, _ = make(env, tmp_path, policy=pol(max_aggregate_notional_usd=1500.0, max_gross_exposure_usd=1e9, max_net_exposure_usd=1e9, max_unhedged_usd=1e9, max_leverage=100.0))
    reg(a, pair("G1", "BTCUSDT"))
    a.authorize(body("G1", "P"), venue="testnet")
    a.authorize(body("G1", "S", cat="spot", side="Buy"), venue="testnet")
    d = a.register_group(pair("G2", "ETHUSDT"))                                    # 1000 + 1000 hedged = 2000 > 1500; ETH alone (2000 gross) is under its own cap
    assert not d.approved and "AGGREGATE_NOTIONAL_ABOVE_CAP" in d.reasons and not any(r.startswith("PER_SYMBOL") for r in d.reasons)


def test_pending_reservations_of_other_groups_count_until_settled_or_resolved(env, tmp_path):
    a, _ = make(env, tmp_path, policy=pol(max_aggregate_notional_usd=1500.0, max_gross_exposure_usd=1e9, max_net_exposure_usd=1e9, max_unhedged_usd=1e9, max_leverage=100.0))
    reg(a, pair("G1", "BTCUSDT"))
    for l, k in (("P", {}), ("S", dict(cat="spot", side="Buy"))):
        a.authorize(body("G1", l, **k), venue="testnet")
    assert not a.register_group(pair("G2", "ETHUSDT")).approved
    a.resolve_group("G1")                                                           # flat per the venue: G1 stops counting
    assert a.register_group(pair("G2", "ETHUSDT")).approved


def test_settled_legs_are_not_counted_twice(env, tmp_path):
    a, _ = make(env, tmp_path, policy=pol(max_notional_per_symbol_usd=2000.0, max_aggregate_notional_usd=1500.0, max_gross_exposure_usd=1e9, max_net_exposure_usd=1e9,
                                         max_unhedged_usd=1e9, max_leverage=100.0))
    reg(a, pair("G1", "BTCUSDT", "5"))                                              # 500 hedged
    a.authorize(body("G1", "P", qty="5"), venue="testnet")
    a.authorize(body("G1", "S", qty="5", cat="spot", side="Buy"), venue="testnet")
    # the account now shows G1's legs; settle them so they are not also counted as pending
    env.state["positions"] = {"BTCUSDT": {"category": "linear", "notional_usd": -500.0}, "SPOT:BTCUSDT": {"category": "spot", "notional_usd": 500.0}}
    env.state["open_groups"] = 1
    a.settle_leg("G1", "P")
    a.settle_leg("G1", "S")
    d = a.register_group(pair("G2", "ETHUSDT", "9"))                               # 500 (G1) + 900 = 1400 <= 1500 counted once; twice would be 1900
    assert d.approved, d.reasons


def test_cross_symbol_exposure_bypass_split_across_groups_is_stopped(env, tmp_path):
    """Three symbols, each pair well under its own per-symbol cap and each individually fine: together they exceed the portfolio ceilings."""
    a, _ = make(env, tmp_path, policy=pol(max_gross_exposure_usd=4500.0, max_aggregate_notional_usd=1e9, max_net_exposure_usd=1e9, max_unhedged_usd=1e9, max_leverage=100.0))
    for gid, sym in (("G1", "BTCUSDT"), ("G2", "ETHUSDT")):
        reg(a, pair(gid, sym))
        a.authorize(body(gid, "P", sym=sym), venue="testnet")
        a.authorize(body(gid, "S", sym=sym, cat="spot", side="Buy"), venue="testnet")
        env.refresh()
    d = a.register_group(pair("G3", "SOLUSDT"))
    assert not d.approved and "GROSS_EXPOSURE_ABOVE_CAP" in d.reasons


def test_max_active_groups_counts_registered_unresolved_groups(env, tmp_path):
    a, _ = make(env, tmp_path, policy=pol(max_active_groups=1))
    reg(a, pair("G1", "BTCUSDT", "2"))
    a.authorize(body("G1", "P", qty="2"), venue="testnet")
    d = a.register_group(pair("G2", "ETHUSDT", "2"))
    assert not d.approved and "MAX_ACTIVE_GROUPS_REACHED" in d.reasons


def test_paired_order_failure_one_leg_filled_other_refused_no_new_exposure_after_the_kill(env, tmp_path):
    a, _ = make(env, tmp_path)
    reg(a)
    a.authorize(body("G1", "P"), venue="testnet")                                  # perp leg goes out
    env.kill = (True, "unhedged: operator stop")                                   # the hedge leg fails -> operator / recovery engages the switch
    assert refused(a, body("G1", "S", cat="spot", side="Buy")) == ("KILL_SWITCH_ENGAGED",)


def test_registration_rejects_a_pair_whose_single_leg_breaches_max_unhedged(env, tmp_path):
    a, _ = make(env, tmp_path, policy=pol(max_unhedged_usd=900.0))
    d = a.register_group(pair())
    assert not d.approved and "UNHEDGED_EXPOSURE_ABOVE_CAP" in d.reasons


def test_partial_hedge_plan_refused(env, tmp_path):
    a, _ = make(env, tmp_path)
    p = pair()
    half = OrderGroupPlan(**{**p.__dict__, "legs": (p.legs[0], leg("BTCUSDT", "spot", "Buy", "3", "S"))})
    d = a.register_group(half)
    assert not d.approved and "NET_EXPOSURE_ABOVE_CAP" in d.reasons


# ================================================================ exits
def opened(a, env):
    reg(a)
    a.authorize(body("G1", "P"), venue="testnet")
    a.authorize(body("G1", "S", cat="spot", side="Buy"), venue="testnet")


def exit_body(**kw):
    b = {"category": "linear", "symbol": "BTCUSDT", "side": "Buy", "orderType": "Market", "qty": "10", "reduceOnly": True, "orderLinkId": "G1-X1"}
    b.update(kw)
    return b


def test_reduce_only_exit_on_an_opened_symbol_is_allowed_even_when_stale_and_killed(env, tmp_path):
    a, _ = make(env, tmp_path)
    opened(a, env)
    env.kill = (True, "stop")
    env.now += 999_999
    assert a.exit_refusal(exit_body()) is None
    a.authorize(exit_body(), venue="testnet")                                       # an exit through authorize is judged only by its binding


@pytest.mark.parametrize("mut,code", [({"symbol": "ETHUSDT"}, "EXIT_SYMBOL_NOT_OPENED_BY_GROUP"), ({"side": "Sell"}, "EXIT_SAME_SIDE_AS_ENTRY"), ({"qty": "11"}, "EXIT_QTY_EXCEEDS_ENTRY"),
                                      ({"orderLinkId": "G2-X1"}, "EXIT_GROUP_NOT_OPENED"), ({"orderLinkId": "G1-P"}, "EXIT_LINK_REUSES_ENTRY_LEG"),
                                      ({"orderLinkId": "G1X1"}, "EXIT_LINK_ID_INVALID"), ({"category": "inverse"}, "EXIT_SYMBOL_NOT_OPENED_BY_GROUP"),
                                      ({"qty": "0"}, "EXIT_QTY_INVALID"), ({"orderType": "StopMarket"}, "EXIT_ORDER_TYPE_NOT_ALLOWED"), ({"isLeverage": 1}, "EXIT_BODY_NOT_ALLOWED"),
                                      ({"positionIdx": 1}, "EXIT_BODY_NOT_ALLOWED"), ({"triggerPrice": "5"}, "EXIT_BODY_NOT_ALLOWED")])
def test_exit_is_bound_to_what_the_group_opened(env, tmp_path, mut, code):
    a, _ = make(env, tmp_path)
    opened(a, env)
    b = exit_body(**mut)
    assert a.exit_refusal(b) == code
    with pytest.raises(EA.EntryRefused):
        a.authorize(b, venue="testnet")


def test_exit_without_reduce_only_is_an_entry_and_goes_through_the_entry_path(env, tmp_path):
    a, _ = make(env, tmp_path)
    opened(a, env)
    b = exit_body()
    del b["reduceOnly"]
    assert refused(a, b)[0] in ("LEG_NOT_IN_GROUP",)


def test_exit_of_a_resolved_group_is_refused(env, tmp_path):
    a, _ = make(env, tmp_path)
    opened(a, env)
    a.resolve_group("G1")
    assert a.exit_refusal(exit_body()) == "EXIT_GROUP_RESOLVED"


def test_spot_sell_is_refused_unless_the_venue_reads_prove_the_hedge_stays(env, tmp_path, client_factory):
    a, t = make(env, tmp_path)
    opened(a, env)
    sell = {"category": "spot", "symbol": "BTCUSDT", "side": "Sell", "orderType": "Market", "qty": "10", "orderLinkId": "G1-X2"}
    assert a.spot_sell_keeps_hedge(sell) is False
    with pytest.raises(EA.EntryRefused) as ei:
        a.authorize(sell, venue="testnet")
    assert ei.value.reasons == ("SPOT_SELL_NOT_PROVEN_HEDGE_KEEPING",)
    c = client_factory(t)
    MA.install(c, a)
    with pytest.raises(bc.OrderNotSent, match="SPOT_SELL_NOT_PROVEN"):
        c._authorize_new_exposure(sell)
    # with a reader that shows the perp is already flat the sell is a bound exit
    a2, t2 = make(env, tmp_path / "h", hedge=lambda sym: (0.0, 10.0))
    reg(a2)
    a2.authorize(body("G1", "P"), venue="testnet")
    a2.authorize(body("G1", "S", cat="spot", side="Buy"), venue="testnet")
    assert a2.spot_sell_keeps_hedge(sell) is True
    a2.authorize(sell, venue="testnet")
    still_hedged = make(env, tmp_path / "h2", hedge=lambda sym: (10.0, 10.0))[0]
    reg(still_hedged)
    assert still_hedged.spot_sell_keeps_hedge(sell) is False                         # perp still short 10: selling spot would strip the hedge
    other = dict(sell, symbol="ETHUSDT")
    assert a2.spot_sell_keeps_hedge(other) is False


# ================================================================ fail closed on its own storage and through the real client protocol
def test_unreadable_group_ledger_refuses(env, tmp_path):
    a, _ = make(env, tmp_path)
    reg(a)
    with open(tmp_path / "st" / "ms_groups.jsonl", "a") as fh:
        fh.write("{not json\n")
    assert refused(a, body()) == ("GROUP_LEDGER_UNREADABLE",)
    assert a.register_group(pair("G7")).reasons == ("GROUP_LEDGER_UNREADABLE",)
    assert a.exit_refusal(exit_body()) == "GROUP_LEDGER_UNREADABLE"


def test_state_is_not_mutated_by_the_authority(env, tmp_path):
    a, _ = make(env, tmp_path)
    reg(a)
    before = copy.deepcopy(env.state)
    a.authorize(body(), venue="testnet")
    assert env.state == before


def test_client_protocol_end_to_end_nothing_unregistered_reaches_the_transport(env, tmp_path, client_factory):
    a, t = make(env, tmp_path)
    c = client_factory(t)
    MA.install(c, a)
    assert c.entry_authority is a and a.max_leverage() == 3
    with pytest.raises(bc.OrderNotSent, match="GROUP_NOT_REGISTERED"):
        c._request("POST", "/v5/order/create", body=body(), signed=True)
    assert t.requests == []
    reg(a)
    c._authorize_new_exposure(body())                                              # approved: no exception
    c._request("POST", "/v5/order/create", body=body("G1", "S", cat="spot", side="Buy"), signed=True)
    assert len(t.requests) == 1 and json.loads(t.requests[0][2])["orderLinkId"] == "G1-S"
    with pytest.raises(bc.OrderNotSent, match="ENTRY_REFUSED"):
        c._request("POST", "/v5/order/create", body=body("G1", "P", qty="99"), signed=True)
    assert len(t.requests) == 1


def test_a_client_on_another_transport_approves_nothing(env, tmp_path, client_factory):
    a, t = make(env, tmp_path)
    reg(a)
    c = client_factory(FakeBybit())
    c.entry_authority = a                                                           # bypassing install(): the client itself still refuses
    with pytest.raises(bc.OrderNotSent, match="AUTHORITY_BOUND_TO_ANOTHER_TRANSPORT"):
        c._authorize_new_exposure(body())


def test_the_btc_production_authority_is_unchanged_and_independent():
    assert not hasattr(EA, "MultiSymbolAuthority") and not hasattr(EA, "ms_risk")
    src = open(os.path.join(BOT, "entry_authority.py")).read()
    assert "ms_" not in src


# ================================================================ red team rt8: the simulated-transport check is a WHITELIST, not a marker
class _Inner:                       # stands in for a real session holder (never constructed from the repo's transports)
    def __init__(self):
        self.http = object()


class Sneaky:
    IN_PROCESS_SIMULATION = True    # self-declared; holds a "real" session under another attribute name
    def __init__(self):
        self.inner = _Inner()


def test_a_class_that_only_declares_the_marker_is_not_a_simulation(env, tmp_path):
    assert MA.is_simulated_transport(Sneaky()) is False
    with pytest.raises(MA.AuthorityConfigError, match="TRANSPORT_NOT_SIMULATED"):
        make(env, tmp_path, transport=Sneaky())


def test_even_a_registered_class_is_refused_when_an_instance_holds_a_network_object(env, tmp_path):
    class Holder:
        def __init__(self):
            self.conn = _Inner()
    Holder.__module__ = "tests_holder"
    MA.register_simulated_transport(Holder)                                       # class level is clean: registration passes
    assert MA.is_simulated_transport(Holder()) is True
    h = Holder()
    NetObj = type("Session", (), {})
    NetObj.__module__ = "requests.sessions"
    h.conn = NetObj()                                                              # a real-looking session under an arbitrary attribute name
    assert MA.is_simulated_transport(h) is False
    h2 = Holder()
    h2.whatever = type("PooledSession", (), {})()
    assert MA.is_simulated_transport(h2) is False


def test_registration_refuses_real_transports_and_network_touching_classes():
    class FromRequests(bc.RequestsTransport):
        pass

    class FromCapability(RC.CapabilityTransport):
        pass
    for cls in (bc.RequestsTransport, FromRequests, RC.CapabilityTransport, FromCapability, "notaclass", object()):
        with pytest.raises(MA.AuthorityConfigError, match="SIMULATED_TRANSPORT_REFUSED"):
            MA.register_simulated_transport(cls)
    import types
    import urllib.request
    mod = types.ModuleType("fake_net_mod")
    mod.urllib_request = urllib.request                                            # the module's own namespace touches urllib
    sys.modules["fake_net_mod"] = mod
    try:
        class Touches:
            pass
        Touches.__module__ = "fake_net_mod"
        mod.Touches = Touches
        assert any(r.startswith("MODULE_TOUCHES_NETWORK") for r in MA.simulated_transport_class_refusals(Touches))
        with pytest.raises(MA.AuthorityConfigError):
            MA.register_simulated_transport(Touches)
        mod2 = types.ModuleType("fake_sock_mod")
        import socket
        mod2.s = socket.socket
        sys.modules["fake_sock_mod"] = mod2

        class Sock:
            pass
        Sock.__module__ = "fake_sock_mod"
        with pytest.raises(MA.AuthorityConfigError):
            MA.register_simulated_transport(Sock)
    finally:
        sys.modules.pop("fake_net_mod", None)
        sys.modules.pop("fake_sock_mod", None)
    assert Touches not in MA._SIMULATED_REGISTRY


def test_registration_is_for_the_exact_class_and_a_marker_is_never_consulted(env, tmp_path):
    class Registered:
        pass
    Registered.__module__ = "tests_registered"
    MA.register_simulated_transport(Registered)

    class Child(Registered):
        IN_PROCESS_SIMULATION = True
    assert MA.is_simulated_transport(Registered()) is True
    assert MA.is_simulated_transport(Child()) is False                             # a subclass is another class
    assert MA.is_simulated_transport(SimT()) is True
    del SimT.IN_PROCESS_SIMULATION                                                  # the marker is not what grants it
    try:
        assert MA.is_simulated_transport(SimT()) is True
    finally:
        SimT.IN_PROCESS_SIMULATION = True


def test_the_pipelines_transport_and_the_backtester_are_accepted_but_a_lookalike_is_not():
    import ms_pipeline
    assert MA.is_simulated_transport(ms_pipeline.SimulatedTransport()) is True
    assert MA.is_simulated_transport(backtest.SimulatedExchange({})) is True
    look = type("SimulatedTransport", (), {"IN_PROCESS_SIMULATION": True})
    look.__module__ = "ms_pipeline"
    assert MA.is_simulated_transport(look()) is False                              # same name and module, not the module's class
    assert MA.is_simulated_transport(bc.RequestsTransport()) is False


# ================================================================ red team rt8: exits are bound by QUANTITY, cumulatively, with a price band
def exit_b(lid, qty="5", **kw):
    return exit_body(orderLinkId=f"G1-{lid}", qty=qty, **kw)


def test_exit_positive_control_and_idempotent_resend(env, tmp_path):
    a, _ = make(env, tmp_path)
    opened(a, env)
    assert a.exit_refusal(exit_b("X1", "10")) is None
    assert a.exit_refusal(exit_b("X1", "10")) is None                               # an identical resend is how a lost ack is resolved: not counted twice
    assert a.exit_refusal(exit_b("X1", "9")) == "EXIT_LINK_CONFLICT"                # the same link with a different order


def test_many_exits_with_fresh_link_ids_cannot_exceed_the_entry_cumulatively(env, tmp_path):
    a, _ = make(env, tmp_path)
    opened(a, env)                                                                  # entry leg qty 10 -> at most 10.2 may ever be exited
    for i in range(2):
        assert a.exit_refusal(exit_b(f"X{i}", "5")) is None                        # 10 total
    assert a.exit_refusal(exit_b("X2", "0.2")) is None                              # 10.2 total: the stated 2% slack
    assert a.exit_refusal(exit_b("X3", "0.1")) == "EXIT_CUMULATIVE_QTY_EXCEEDS_ENTRY"
    for i in range(20):
        assert a.exit_refusal(exit_b(f"Y{i}", "1")) == "EXIT_CUMULATIVE_QTY_EXCEEDS_ENTRY"
    with pytest.raises(EA.EntryRefused) as ei:
        a.authorize(exit_b("X9", "1"), venue="testnet")
    assert ei.value.reasons == ("EXIT_CUMULATIVE_QTY_EXCEEDS_ENTRY",)


def test_the_reservation_is_durable_across_authority_instances(env, tmp_path):
    a, _ = make(env, tmp_path)
    opened(a, env)
    assert a.exit_refusal(exit_b("X1", "10")) is None
    a2, _ = make(env, tmp_path)                                                     # same state dir: a restart
    assert a2.exit_refusal(exit_b("X2", "1")) == "EXIT_CUMULATIVE_QTY_EXCEEDS_ENTRY"
    assert a2.exit_refusal(exit_b("X1", "10")) is None


def test_released_quantity_can_be_exited_again_and_a_filled_part_stays_consumed(env, tmp_path):
    a, _ = make(env, tmp_path)
    opened(a, env)
    assert a.exit_refusal(exit_b("X1", "10")) is None
    assert a.exit_refusal(exit_b("X2", "4")) == "EXIT_CUMULATIVE_QTY_EXCEEDS_ENTRY"
    assert a.release_exit("G1-X1", "4") is True                                     # 6 filled, 4 never filled
    assert a.exit_refusal(exit_b("X2", "4")) is None                                # the 4 can be exited again, under a fresh link
    assert a.exit_refusal(exit_b("X3", "0.5")) == "EXIT_CUMULATIVE_QTY_EXCEEDS_ENTRY"      # the filled 6 stayed consumed
    assert a.release_exit("G1-X1", "7") is False and a.release_exit("G1-NOPE") is False     # cannot release more than is outstanding, or an unknown link
    assert a.release_exit("G1-X1") is True                                          # the rest (6 -> remaining outstanding is 6): whole remainder
    assert a.exit_refusal(exit_b("X1", "10")) == "EXIT_LINK_RELEASED"               # a released link is spent


def test_exit_reservations_are_per_group_and_per_symbol(env, tmp_path):
    a, _ = make(env, tmp_path)
    reg(a, pair("G1", "BTCUSDT"))
    a.authorize(body("G1", "P"), venue="testnet")
    a.authorize(body("G1", "S", cat="spot", side="Buy"), venue="testnet")
    reg(a, pair("G2", "ETHUSDT"))
    a.authorize(body("G2", "P", sym="ETHUSDT"), venue="testnet")
    assert a.exit_refusal(exit_b("X1", "10")) is None
    assert a.exit_refusal(exit_body(orderLinkId="G2-X1", symbol="ETHUSDT", qty="10")) is None       # G1's exits do not eat G2's allowance


def test_a_limit_exit_must_be_near_the_mark_and_an_unreadable_mark_refuses_it(env, tmp_path):
    a, _ = make(env, tmp_path)
    opened(a, env)
    lim = lambda lid, px: exit_b(lid, "1", orderType="Limit", price=px, timeInForce="GTC")          # noqa: E731
    assert a.exit_refusal(lim("L1", "100")) is None
    assert a.exit_refusal(lim("L2", "1.0")) == "EXIT_PRICE_OUT_OF_BAND"
    assert a.exit_refusal(lim("L3", "111")) == "EXIT_PRICE_OUT_OF_BAND"
    assert a.exit_refusal(lim("L4", "0")) in ("EXIT_PRICE_UNVERIFIABLE", "EXIT_BODY_NOT_ALLOWED")
    assert a.exit_refusal(exit_b("L5", "1", orderType="Limit")) in ("EXIT_PRICE_UNVERIFIABLE", "EXIT_BODY_NOT_ALLOWED")      # no price at all
    env.snaps["BTCUSDT"] = MarketSnapshot(symbol="BTCUSDT", decision_ms=NOW, mark=None, spot_mark=100.0, book_age_ms=1)
    assert a.exit_refusal(lim("L6", "100")) == "EXIT_PRICE_UNVERIFIABLE"
    assert a.exit_refusal(exit_b("M1", "1")) is None                                # a Market reduce-only exit needs no mark
    assert a.exit_refusal(exit_b("M2", "1", reduceOnly=False)) == "EXIT_NOT_REDUCE_ONLY"   # a Market exit that is not reduce-only is not an exit


# ================================================================ red team rt8: a registered group's legs are reserved whole at registration
def big_pair(gid, sym, qty="20"):
    return pair(gid, sym, qty)                                                      # 2000 aggregate each; the policy cap is 3000


def test_a_later_group_cannot_squeeze_an_in_flight_group(env, tmp_path):
    a, _ = make(env, tmp_path, policy=pol(max_notional_per_symbol_usd=5000.0, max_unhedged_usd=3000.0, max_gross_exposure_usd=12000.0))
    reg(a, big_pair("GA", "BTCUSDT"))                                                # registered, NOTHING sent yet
    d = a.register_group(big_pair("GB", "ETHUSDT"))                                  # 2000 + 2000 > 3000: refused at registration, not later
    assert not d.approved and "AGGREGATE_NOTIONAL_ABOVE_CAP" in d.reasons
    a.authorize(body("GA", "P", qty="20"), venue="testnet")
    a.authorize(body("GA", "S", cat="spot", side="Buy", qty="20"), venue="testnet")  # the second leg of the first group is still approved
    assert not a.register_group(big_pair("GB", "ETHUSDT")).approved                  # and after its legs are sent
    a.resolve_group("GA")
    assert a.register_group(big_pair("GB", "ETHUSDT")).approved                      # positive control: it fits once the first group is resolved


def test_unsent_legs_of_an_earlier_group_count_but_a_later_groups_never_count_against_an_earlier_one(env, tmp_path):
    a, _ = make(env, tmp_path, policy=pol(max_notional_per_symbol_usd=5000.0, max_unhedged_usd=3000.0, max_gross_exposure_usd=12000.0))
    reg(a, pair("GA", "BTCUSDT", "10"))
    reg(a, pair("GB", "ETHUSDT", "10"))                                              # 1000 + 1000 + ... fits
    assert not a.register_group(pair("GC", "SOLUSDT", "20")).approved                # 1000 + 1000 + 2000 > 3000
    # the earlier group's legs are authorised whatever the later group has reserved
    a.authorize(body("GB", "P", sym="ETHUSDT"), venue="testnet")
    a.authorize(body("GB", "S", sym="ETHUSDT", cat="spot", side="Buy"), venue="testnet")
    a.authorize(body("GA", "P"), venue="testnet")
    a.authorize(body("GA", "S", cat="spot", side="Buy"), venue="testnet")
