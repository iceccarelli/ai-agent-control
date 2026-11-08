"""Offline proof of the dedicated TESTNET transport: host identity, redirect/proxy/header refusals, dedicated-credential-only, least-privilege key check, canary secrecy."""
from __future__ import annotations

import ast
import copy
import io
import json
import os
import pickle
import socket
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import bybit_connection as bc  # noqa: E402
import runtime_capability as RC  # noqa: E402
import ms_testnet_transport as TT  # noqa: E402
from ms_fake_bybit_testnet import FAKE_KEY, FAKE_SECRET, FakeBybitTestnet  # noqa: E402

BOT = os.path.dirname(HERE)
ENV = {TT.KEY_ENV: FAKE_KEY, TT.SECRET_ENV: FAKE_SECRET}


@pytest.fixture(autouse=True)
def _nosleep(monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)


class Spy:
    def __init__(self):
        self.calls = []

    def request(self, method, url, *, headers=None, body=None, timeout=None):
        self.calls.append((method, url))
        return 200, "{}"


def tt(inner=None):
    return TT.TestnetTransport(inner if inner is not None else Spy())


def test_positive_control_signed_read_through_the_full_stack():
    fake = FakeBybitTestnet()
    client, transport = TT.build_client(TT.TestnetCredentials.from_mapping(ENV), fake)
    r = client._request("GET", "/v5/user/query-api", signed=True)
    assert r["retCode"] == 0 and client.venue == "testnet" and transport.requests_sent == 1
    assert fake.requests[0][1] == "/v5/user/query-api"


def test_bad_signature_is_what_the_fake_rejects_so_the_positive_control_is_not_vacuous():
    fake = FakeBybitTestnet(secret="another-secret")
    client, _ = TT.build_client(TT.TestnetCredentials.from_mapping(ENV), fake)
    with pytest.raises(bc.PermanentAPIError) as e:
        client._request("GET", "/v5/user/query-api", signed=True, retries=0)
    assert e.value.ret_code == 10004


@pytest.mark.parametrize("url", [
    "https://api.bybit.com/v5/market/time",                       # mainnet
    "https://api.bytick.com/v5/market/time",                      # regional mainnet
    "https://api.bybit.eu/v5/market/time",                        # eu (unqualified)
    "https://api-demo.bybit.com/v5/market/time",                  # demo
    "https://api-testnet.bybit.com.evil.example/v5/market/time",  # suffix lookalike
    "https://evil.example/api-testnet.bybit.com/v5/market/time",
    "https://api-testnet.bybit.com@evil.example/v5/market/time",  # userinfo trick
    "https://evil.example@api-testnet.bybit.com/v5/market/time",
    "https://api-testnet.bybit.com:8443/v5/market/time",          # port
    "https://api-testnet.bybit.com./v5/market/time",              # trailing dot
    "https://API-TESTNET.BYBIT.COM/v5/market/time",               # case
    "https://api-testnet.bybit.com\\@evil.example/v5/market/time",
    "http://api-testnet.bybit.com/v5/market/time",                # plain http
    "ftp://api-testnet.bybit.com/v5/market/time",
    "https://203.0.113.7/v5/market/time",                         # IP literal (DNS rebinding style)
    "https://[::1]/v5/market/time",
    "https://api-testnet.bybit.com/not-v5/market/time",
    "https://api-testnet.bybit.com/v5/../v5/market/time",
    "https://api-testnet.bybit.com/v5/market/time#frag",
    "//api-testnet.bybit.com/v5/market/time",
    "",
])
def test_every_host_other_than_the_exact_registry_testnet_host_is_refused_before_the_inner_transport(url):
    spy = Spy()
    with pytest.raises((TT.TestnetTransportRefused, RC.CapabilityRefused)):
        tt(spy).request("GET", url, headers={}, timeout=1)
    assert spy.calls == []


def test_the_allowed_host_is_the_registry_row_not_a_literal():
    import venues
    assert TT.testnet_host() == venues.get("testnet").host == "api-testnet.bybit.com"
    assert venues.get("testnet").kind == venues.SANDBOX


@pytest.mark.parametrize("hdr", ["Host", "X-Forwarded-Host", "X-HTTP-Method-Override", "Proxy-Authorization", "Cookie", "Authorization", "Forwarded"])
def test_headers_that_could_reroute_or_re_method_a_request_are_refused(hdr):
    spy = Spy()
    with pytest.raises((TT.TestnetTransportRefused, RC.CapabilityRefused)):
        tt(spy).request("GET", "https://api-testnet.bybit.com/v5/market/time", headers={hdr: "evil.example"}, timeout=1)
    assert spy.calls == []


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_answer_is_refused_not_followed(status):
    class Redirecting:
        def request(self, method, url, *, headers=None, body=None, timeout=None):
            return status, ""
    with pytest.raises(TT.TestnetTransportRefused) as e:
        tt(Redirecting()).request("GET", "https://api-testnet.bybit.com/v5/market/time", headers={}, timeout=1)
    assert e.value.code == "REDIRECT_REFUSED"


def test_the_real_inner_transport_is_configured_to_follow_nothing_and_trust_no_environment(monkeypatch):
    inner = TT.NoRedirectInner()                                   # builds a requests session; sends nothing (conftest blocks Session.request anyway)
    assert inner._session.trust_env is False and inner._session.proxies == {} and inner._session.max_redirects == 0
    src = open(os.path.join(BOT, "tools", "ms_testnet_transport.py")).read()
    assert "allow_redirects=False" in src


def test_only_the_exact_transport_class_is_the_testnet_executor_transport():
    t = tt()
    assert TT.is_testnet_executor_transport(t)

    class Sub(TT.TestnetTransport):
        pass
    cap = RC.Capability.for_role(RC.TESTNET_EXECUTOR, venue="testnet", credential_class=RC.TESTNET_CREDENTIAL_CLASS)
    assert not TT.is_testnet_executor_transport(Sub(Spy(), cap))
    assert not TT.is_testnet_executor_transport(RC.CapabilityTransport(Spy(), cap))
    assert not TT.is_testnet_executor_transport(Spy()) and not TT.is_testnet_executor_transport(None)


@pytest.mark.parametrize("venue", ["mainnet", "demo", "eu", "unknown"])
def test_the_transport_cannot_be_built_for_any_other_venue(venue):
    with pytest.raises((RC.CapabilityConstructionError, TT.TestnetTransportRefused)):
        TT.TestnetTransport(Spy(), RC.Capability.for_role(RC.TESTNET_EXECUTOR, venue=venue, credential_class=RC.TESTNET_CREDENTIAL_CLASS))
    monitor = RC.Capability.for_role(RC.READ_ONLY_MONITOR, venue="testnet")
    with pytest.raises(TT.TestnetTransportRefused):
        TT.TestnetTransport(Spy(), monitor)


def test_the_role_capability_still_refuses_withdraw_transfer_amend_and_batch_on_the_testnet_host():
    fake = FakeBybitTestnet()
    client, _ = TT.build_client(TT.TestnetCredentials.from_mapping(ENV), fake)
    for path in ("/v5/asset/transfer/inter-transfer", "/v5/asset/withdraw/create", "/v5/order/amend", "/v5/order/create-batch", "/v5/account/set-margin-mode"):
        with pytest.raises(bc.OrderNotSent):
            client._request("POST", path, body={"x": "1"}, signed=True)
    assert fake.requests == []


# ---------------------------------------------------------------- credentials
def test_missing_dedicated_credentials_is_a_typed_error_with_the_exact_setup_instructions():
    with pytest.raises(TT.TestnetCredentialsMissing) as e:
        TT.TestnetCredentials.from_mapping({})
    msg = str(e.value)
    assert TT.KEY_ENV in msg and TT.SECRET_ENV in msg and "Codespaces" in msg and "Secrets" in msg and e.value.missing == [TT.KEY_ENV, TT.SECRET_ENV]


@pytest.mark.parametrize("env", [
    {"BYBIT_API_KEY": "generic-key", "BYBIT_API_SECRET": "generic-secret"},                      # the generic pair is NEVER a fallback
    {"BYBIT_API_KEY": "g", "BYBIT_API_SECRET": "g", TT.KEY_ENV: FAKE_KEY},
    {TT.KEY_ENV: "", TT.SECRET_ENV: "  "},
    {"BYBIT_TESTNET_DEDICATED_API_KEY": "x", "BYBIT_TESTNET_DEDICATED_API_SECRET": "y"},        # near-miss names are not the dedicated variables
])
def test_generic_or_near_miss_credentials_never_substitute(env):
    with pytest.raises(TT.TestnetCredentialsMissing):
        TT.TestnetCredentials.from_mapping(env)
    p = TT.TestnetCredentials.presence(env)
    assert not (p["dedicated_key_present"] and p["dedicated_secret_present"])


def test_the_transport_and_executor_modules_never_read_the_environment():
    for rel in ("tools/ms_testnet_transport.py", "tools/ms_testnet_executor.py"):
        tree = ast.parse(open(os.path.join(BOT, rel)).read())
        for n in ast.walk(tree):
            assert not (isinstance(n, ast.Attribute) and n.attr in ("environ", "getenv", "environb")), (rel, n.lineno)
            assert not (isinstance(n, ast.Name) and n.id in ("environ", "getenv")), (rel, n.lineno)


def test_importing_the_modules_opens_no_socket_and_reads_no_credential():
    """Fresh interpreter (reloading would replace the classes other tests hold): any socket use or environment credential read at import fails it."""
    import subprocess
    code = ("import socket,sys,os\n"
            "def boom(*a,**k): raise AssertionError('socket used at import')\n"
            "socket.socket=boom; socket.create_connection=boom; socket.getaddrinfo=boom\n"
            f"sys.path[:0]=[{BOT!r}, {os.path.join(BOT, 'tools')!r}]\n"
            "import ms_testnet_transport, ms_testnet_executor, ms_testnet_session\n"
            "print('imported')\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("BYBIT")}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and "imported" in r.stdout, r.stderr[-500:]


def _sweep(obj_texts):
    for t in obj_texts:
        assert FAKE_KEY not in t and FAKE_SECRET not in t, "canary leaked"


def test_canary_credentials_never_appear_in_repr_str_pickle_exceptions_or_logs(caplog):
    creds = TT.TestnetCredentials.from_mapping(ENV)
    fake = FakeBybitTestnet()
    client, transport = TT.build_client(creds, fake)
    texts = [repr(creds), str(creds), repr(client), repr(client.cfg), repr(transport), str(client.describe()), repr(transport.capability)]
    for fn in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError) as e:
            fn(creds)
        texts.append(str(e.value))
    with pytest.raises(AttributeError):
        creds._key = "x"
    fake.secret = "different"                                          # a signature failure carries the venue's message, not our key
    with caplog.at_level("DEBUG"):
        with pytest.raises(bc.PermanentAPIError) as e:
            client._request("GET", "/v5/user/query-api", signed=True, retries=0)
    texts += [repr(e.value), str(e.value), repr(e.value.payload), caplog.text]
    with pytest.raises(TT.TestnetCredentialsMissing) as e2:
        TT.TestnetCredentials.from_mapping({TT.KEY_ENV: FAKE_KEY})     # the key IS present: the error must not echo it
    texts.append(str(e2.value))
    with pytest.raises(TT.TestnetTransportRefused) as e3:
        transport.request("GET", "https://api.bybit.com/v5/market/time", headers={"X-BAPI-API-KEY": FAKE_KEY}, timeout=1)
    texts.append(str(e3.value))
    _sweep(texts)


# ---------------------------------------------------------------- least privilege
def _perm(readonly=0, **perms):
    return {"readOnly": readonly, "permissions": perms, "ips": ["*"]}


def test_key_with_exactly_trade_permissions_passes():
    bad, summary = TT.key_permission_findings(_perm(ContractTrade=["Order", "Position"], Spot=["SpotTrade"]))
    assert bad == [] and summary["read_only"] == 0


@pytest.mark.parametrize("res,code", [
    (_perm(Wallet=["AccountTransfer"], ContractTrade=["Order"]), "KEY_HAS_TRANSFER_OR_WITHDRAW"),
    (_perm(Wallet=["Withdraw"]), "KEY_HAS_TRANSFER_OR_WITHDRAW"),
    (_perm(Wallet=["SubMemberTransfer"]), "KEY_HAS_TRANSFER_OR_WITHDRAW"),
    (_perm(ContractTrade=["Order"], Exchange=["ExchangeHistory"]), "KEY_HAS_EXTRA_PERMISSION"),
    (_perm(Options=["OptionsTrade"]), "KEY_HAS_EXTRA_PERMISSION"),
    (_perm(readonly=1, ContractTrade=["Order"]), "KEY_IS_READ_ONLY"),
    ({"permissions": {}}, "KEY_READONLY_UNREADABLE"),
    ({"readOnly": 0}, "KEY_PERMISSIONS_UNREADABLE"),
    ({"readOnly": 0, "permissions": {"Spot": "SpotTrade"}}, "KEY_PERMISSIONS_UNREADABLE"),
    (None, "KEY_INFO_UNREADABLE"),
    ("x", "KEY_INFO_UNREADABLE"),
])
def test_key_with_transfer_withdraw_extra_or_unreadable_permissions_is_refused(res, code):
    bad, _ = TT.key_permission_findings(res)
    assert any(b.startswith(code) for b in bad), bad


def test_check_api_key_reads_the_testnet_only_path_and_refuses_a_withdraw_key():
    fake = FakeBybitTestnet()
    client, _ = TT.build_client(TT.TestnetCredentials.from_mapping(ENV), fake)
    ok, detail, _ = TT.check_api_key(client)
    assert ok and detail == "ok"
    fake.api_perms["permissions"]["Wallet"] = ["Withdraw"]
    ok, detail, summary = TT.check_api_key(client)
    assert not ok and "KEY_HAS_TRANSFER_OR_WITHDRAW" in detail
    assert FAKE_KEY not in json.dumps(summary)
    assert all(r[1] == "/v5/user/query-api" for r in fake.requests)


def test_a_mainnet_style_key_is_not_a_testnet_key_the_testnet_host_rejects_it():
    fake = FakeBybitTestnet(key="some-other-key")
    client, _ = TT.build_client(TT.TestnetCredentials.from_mapping(ENV), fake)
    ok, detail, _ = TT.check_api_key(client)
    assert not ok and "retCode=10003" in detail


# ---- the transport's OWN checks, with the capability layer bypassed (a surviving mutant removed the /v5/ path-prefix check because the capability layer caught it first)
@pytest.mark.parametrize("url", [
    "https://api-testnet.bybit.com/other/market/time", "https://api-testnet.bybit.com/market/time", "https://api-testnet.bybit.com/", "https://api-testnet.bybit.com",
    "https://api.bybit.com/v5/market/time", "https://api-testnet.bybit.com:8443/v5/market/time", "https://user:pw@api-testnet.bybit.com/v5/market/time",
    "http://api-testnet.bybit.com/v5/market/time", "https://api-testnet.bybit.com.evil.example/v5/market/time", "https://API-TESTNET.BYBIT.COM/v5/market/time",
])
def test_the_transports_own_host_and_path_checks_hold_without_the_capability_layer(monkeypatch, url):
    spy = Spy()
    monkeypatch.setattr(RC.CapabilityTransport, "request", lambda self, method, url, **kw: self._inner.request(method, url, **{k: v for k, v in kw.items() if k != "timeout"}))
    with pytest.raises(TT.TestnetTransportRefused):
        tt(spy).request("GET", url, headers={}, timeout=1)
    assert spy.calls == []
    ok = tt(spy)
    ok.request("GET", "https://api-testnet.bybit.com/v5/market/time", headers={}, timeout=1)          # positive control: the same bypass lets the right URL through
    assert spy.calls
