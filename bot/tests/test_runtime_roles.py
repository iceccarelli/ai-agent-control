"""Role construction, CapabilityTransport enforcement, and the import surface of the capability module (AST)."""
import ast
import os
import sys

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)

import runtime_capability as RC  # noqa: E402
import venues  # noqa: E402

TN = venues.get("testnet").host
MN = venues.get("mainnet").host
SIGNED = {"X-BAPI-API-KEY": "k", "X-BAPI-SIGN": "s", "X-BAPI-TIMESTAMP": "1"}


class Spy:
    def __init__(self):
        self.calls = []
        self.marker = "inner-attr"

    def request(self, method, url, *, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        return 200, "{}"


def wrap(role, venue="testnet"):
    spy = Spy()
    cap = RC.Capability.for_role(role, venue=venue, credential_class=RC.TESTNET_CREDENTIAL_CLASS if role == RC.TESTNET_EXECUTOR else None)
    return RC.CapabilityTransport(spy, cap), spy


# ---- construction -------------------------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("role", [RC.PUBLIC_COLLECTOR, RC.EVIDENCE_BACKUP])
@pytest.mark.parametrize("kw", [{"api_key": "x"}, {"api_secret": "y"}, {"api_key": "x", "api_secret": "y"}, {"credential_class": "testnet_dedicated"},
                                {"env": {"BYBIT_API_KEY": "x"}}, {"env": {"BYBIT_API_SECRET": ""}}, {"env": {"bybit_api_key": "x"}},
                                {"env": {"BYBIT_TESTNET_API_KEY": "x", "PATH": "/bin"}}, {"env": {"BYBIT_TESTNET_SECRET": "z"}}])
def test_credential_free_roles_refuse_any_credential(role, kw):
    with pytest.raises(RC.CapabilityConstructionError):
        RC.Capability.for_role(role, **kw)


@pytest.mark.parametrize("role", [RC.PUBLIC_COLLECTOR, RC.EVIDENCE_BACKUP])
def test_credential_free_roles_accept_a_clean_env(role):
    assert RC.Capability.for_role(role, env={"PATH": "/bin", "BYBIT_VENUE": "testnet"}).role == role


@pytest.mark.parametrize("cc", [None, "", "generic", "mainnet", "demo", "TESTNET_DEDICATED", "testnet", "testnet_dedicated "])
def test_executor_needs_the_exact_testnet_credential_class(cc):
    with pytest.raises(RC.CapabilityConstructionError):
        RC.Capability.for_role(RC.TESTNET_EXECUTOR, venue="testnet", credential_class=cc)


def test_unknown_role_and_unqualified_venue_refused():
    for role in ("admin", "", None, "Public_Collector"):
        with pytest.raises(RC.CapabilityConstructionError):
            RC.Capability.for_role(role)
    with pytest.raises(RC.CapabilityConstructionError):
        RC.Capability.for_role(RC.PUBLIC_COLLECTOR, venue="eu")


def test_executor_positive_construction():
    c = RC.Capability.for_role(RC.TESTNET_EXECUTOR, venue="testnet", credential_class="testnet_dedicated")
    assert c.host == TN and c.venue == "testnet"


# ---- the transport wrapper ----------------------------------------------------------------------------------------------------------------------------------
def test_collector_transport_passes_a_public_get_and_delegates_attributes():
    t, spy = wrap(RC.PUBLIC_COLLECTOR)
    assert t.request("GET", f"https://{TN}/v5/market/tickers?category=linear&symbol=BTCUSDT", headers={"Content-Type": "application/json"}, body=None, timeout=5) == (200, "{}")
    assert len(spy.calls) == 1 and t.marker == "inner-attr"


HOSTILE = [
    ("collector signs", RC.PUBLIC_COLLECTOR, "GET", f"https://{TN}/v5/market/tickers", SIGNED, None),
    ("collector signed account read", RC.PUBLIC_COLLECTOR, "GET", f"https://{TN}/v5/position/list", SIGNED, None),
    ("collector posts order", RC.PUBLIC_COLLECTOR, "POST", f"https://{TN}/v5/order/create", SIGNED, "{}"),
    ("collector posts unsigned order", RC.PUBLIC_COLLECTOR, "POST", f"https://{TN}/v5/order/create", {}, "{}"),
    ("collector authorization header", RC.PUBLIC_COLLECTOR, "GET", f"https://{TN}/v5/market/time", {"Authorization": "Bearer x"}, None),
    ("collector lowercase sign header", RC.PUBLIC_COLLECTOR, "GET", f"https://{TN}/v5/market/time", {"x-bapi-sign": "s"}, None),
    ("collector wrong host", RC.PUBLIC_COLLECTOR, "GET", f"https://{MN}/v5/market/time", {}, None),
    ("collector host suffix trick", RC.PUBLIC_COLLECTOR, "GET", f"https://{TN}.evil.example/v5/market/time", {}, None),
    ("collector userinfo trick", RC.PUBLIC_COLLECTOR, "GET", f"https://{TN}@evil.example/v5/market/time", {}, None),
    ("collector port", RC.PUBLIC_COLLECTOR, "GET", f"https://{TN}:8443/v5/market/time", {}, None),
    ("collector http", RC.PUBLIC_COLLECTOR, "GET", f"http://{TN}/v5/market/time", {}, None),
    ("collector fragment", RC.PUBLIC_COLLECTOR, "GET", f"https://{TN}/v5/market/time#x", {}, None),
    ("collector upper host", RC.PUBLIC_COLLECTOR, "GET", f"https://{TN.upper()}/v5/market/time", {}, None),
    ("collector upper path", RC.PUBLIC_COLLECTOR, "GET", f"https://{TN}/V5/market/time", {}, None),
    ("collector slash path", RC.PUBLIC_COLLECTOR, "GET", f"https://{TN}/v5/market/time/", {}, None),
    ("collector double slash", RC.PUBLIC_COLLECTOR, "GET", f"https://{TN}//v5/market/time", {}, None),
    ("collector body on get", RC.PUBLIC_COLLECTOR, "GET", f"https://{TN}/v5/market/time", {}, "{}"),
    ("collector withdrawal", RC.PUBLIC_COLLECTOR, "POST", f"https://{TN}/v5/asset/withdraw/create", SIGNED, "{}"),
    ("monitor posts order", RC.READ_ONLY_MONITOR, "POST", f"https://{TN}/v5/order/create", SIGNED, "{}"),
    ("monitor posts cancel", RC.READ_ONLY_MONITOR, "POST", f"https://{TN}/v5/order/cancel", SIGNED, "{}"),
    ("monitor leverage", RC.READ_ONLY_MONITOR, "POST", f"https://{TN}/v5/position/set-leverage", SIGNED, "{}"),
    ("monitor delete", RC.READ_ONLY_MONITOR, "DELETE", f"https://{TN}/v5/order/cancel", SIGNED, None),
    ("monitor transfer", RC.READ_ONLY_MONITOR, "POST", f"https://{TN}/v5/asset/transfer/inter-transfer", SIGNED, "{}"),
    ("monitor signed GET of order create", RC.READ_ONLY_MONITOR, "GET", f"https://{TN}/v5/order/create", SIGNED, None),
    ("monitor withdrawal GET", RC.READ_ONLY_MONITOR, "GET", f"https://{TN}/v5/asset/withdraw/query-record", SIGNED, None),
    ("monitor unknown path", RC.READ_ONLY_MONITOR, "GET", f"https://{TN}/v5/account/whatever", SIGNED, None),
    ("monitor mainnet host from testnet role", RC.READ_ONLY_MONITOR, "GET", f"https://{MN}/v5/position/list", SIGNED, None),
    ("backup public read", RC.EVIDENCE_BACKUP, "GET", f"https://{TN}/v5/market/time", {}, None),
    ("backup signed read", RC.EVIDENCE_BACKUP, "GET", f"https://{TN}/v5/position/list", SIGNED, None),
    ("backup post", RC.EVIDENCE_BACKUP, "POST", f"https://{TN}/v5/order/create", SIGNED, "{}"),
    ("backup arbitrary host", RC.EVIDENCE_BACKUP, "GET", "https://example.com/", {}, None),
    ("executor mainnet host", RC.TESTNET_EXECUTOR, "POST", f"https://{MN}/v5/order/create", SIGNED, "{}"),
    ("executor demo host", RC.TESTNET_EXECUTOR, "POST", "https://api-demo.bybit.com/v5/order/create", SIGNED, "{}"),
    ("executor query on post", RC.TESTNET_EXECUTOR, "POST", f"https://{TN}/v5/order/create?x=1", SIGNED, "{}"),
    ("executor case variant", RC.TESTNET_EXECUTOR, "POST", f"https://{TN}/v5/order/Create", SIGNED, "{}"),
    ("executor amend", RC.TESTNET_EXECUTOR, "POST", f"https://{TN}/v5/order/amend", SIGNED, "{}"),
    ("executor batch create", RC.TESTNET_EXECUTOR, "POST", f"https://{TN}/v5/order/create-batch", SIGNED, "{}"),
    ("executor withdrawal", RC.TESTNET_EXECUTOR, "POST", f"https://{TN}/v5/asset/withdraw/create", SIGNED, "{}"),
    ("executor transfer", RC.TESTNET_EXECUTOR, "POST", f"https://{TN}/v5/asset/transfer/universal-transfer", SIGNED, "{}"),
    ("executor unsigned post", RC.TESTNET_EXECUTOR, "POST", f"https://{TN}/v5/order/create", {}, "{}"),
    ("executor sub-account key", RC.TESTNET_EXECUTOR, "POST", f"https://{TN}/v5/user/create-sub-api", SIGNED, "{}"),
]


@pytest.mark.parametrize("name,role,method,url,headers,body", HOSTILE, ids=[h[0] for h in HOSTILE])
def test_hostile_requests_never_reach_the_inner_transport(name, role, method, url, headers, body):
    t, spy = wrap(role)
    with pytest.raises(RC.CapabilityRefused):
        t.request(method, url, headers=headers, body=body, timeout=1)
    assert spy.calls == []


def test_executor_positive_controls_reach_the_inner_transport():
    t, spy = wrap(RC.TESTNET_EXECUTOR)
    t.request("POST", f"https://{TN}/v5/order/create", headers={**SIGNED, "Content-Type": "application/json"}, body='{"symbol":"BTCUSDT"}', timeout=5)
    t.request("GET", f"https://{TN}/v5/position/list?category=linear&symbol=BTCUSDT", headers=SIGNED, body=None, timeout=5)
    assert [c[0] for c in spy.calls] == ["POST", "GET"]


def test_monitor_positive_signed_get_and_unknown_kwargs_refused():
    t, spy = wrap(RC.READ_ONLY_MONITOR)
    t.request("GET", f"https://{TN}/v5/account/wallet-balance?accountType=UNIFIED", headers=SIGNED, body=None, timeout=5)
    assert len(spy.calls) == 1
    with pytest.raises(RC.CapabilityRefused):
        t.request("GET", f"https://{TN}/v5/market/time", headers={}, body=None, timeout=1, verify=False)
    with pytest.raises(RC.CapabilityRefused):
        t.request("GET", None, headers={}, body=None, timeout=1)


# ---- import surface -----------------------------------------------------------------------------------------------------------------------------------------
CANNOT_SIGN = {"hmac", "hashlib", "base64", "secrets", "ssl", "socket", "requests", "urllib3", "http", "httpx", "aiohttp", "websocket", "websockets",
               "bybit_connection", "config", "os", "subprocess", "entry_authority", "carry_broker", "md_transport", "ws_transport"}
ALLOWED_IMPORTS = {"__future__", "re", "dataclasses", "typing", "urllib.parse", "venues"}


def _imports(path):
    tree = ast.parse(open(path).read())
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            out.add(n.module or "")
    return out


def test_capability_module_imports_nothing_that_can_sign_or_send():
    imps = _imports(os.path.join(BOT, "runtime_capability.py"))
    assert imps <= ALLOWED_IMPORTS, imps - ALLOWED_IMPORTS
    assert not ({i.split(".")[0] for i in imps} & CANNOT_SIGN)


def test_the_ast_check_itself_detects_a_signing_import(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("import hmac\nfrom bybit_connection import BybitClient\n")
    assert {i.split(".")[0] for i in _imports(str(p))} & CANNOT_SIGN


def test_public_market_data_modules_do_not_import_signing_or_the_client():
    for mod in ("md_rest.py", "md_feed.py", "md_wire.py", "md_book.py"):
        path = os.path.join(BOT, mod)
        if not os.path.exists(path):
            continue
        assert not ({i.split(".")[0] for i in _imports(path)} & {"hmac", "bybit_connection", "carry_broker", "entry_authority"}), mod


def test_venue_registry_is_the_only_host_source():
    src = open(os.path.join(BOT, "runtime_capability.py")).read()
    assert "bybit.com" not in src and "bybit.eu" not in src


# ---- red-team bypasses: header forms, control characters, credential query params ----------------------------------------------------------------------------
URL_OK = f"https://{TN}/v5/market/time"

BAD_HEADERS = [
    ("trailing space", {"X-BAPI-SIGN ": "x"}),
    ("trailing tab", {"x-bapi-api-key\t": "k"}),
    ("list of tuples", [("X-BAPI-SIGN", "x"), ("X-BAPI-API-KEY", "k")]),
    ("tuple of tuples", (("x-bapi-sign", "x"),)),
    ("bytes key", {b"X-BAPI-SIGN": "x"}),
    ("bytes key trailing space", {b"x-bapi-sign ": "x"}),
    ("cookie", {"Cookie": "session=1"}),
    ("proxy-authorization", {"Proxy-Authorization": "Basic x"}),
    ("x-sign", {"X-Sign": "x"}),
    ("authorization upper", {"AUTHORIZATION": "Bearer x"}),
    ("authorization mixed+space", {" AuthoriZation ": "Bearer x"}),
    ("non-str key", {1: "x"}),
    ("non-ascii key", {"x-bapi-signé": "x"}),
    ("bytes undecodable", {b"\xff\xfe": "x"}),
    ("inner whitespace key", {"x bapi sign": "x"}),
    ("newline in key", {"Accept\nX-BAPI-SIGN": "x"}),
    ("unparseable: a string", "X-BAPI-SIGN: x"),
    ("unparseable: ints", 5),
    ("unparseable: odd pair", [("a", "b", "c")]),
    ("unparseable: list of str", ["X-BAPI-SIGN"]),
]


@pytest.mark.parametrize("name,headers", BAD_HEADERS, ids=[b[0] for b in BAD_HEADERS])
def test_every_header_form_that_signs_or_cannot_be_parsed_is_refused_for_the_collector(name, headers):
    t, spy = wrap(RC.PUBLIC_COLLECTOR)
    with pytest.raises(RC.CapabilityRefused):
        t.request("GET", URL_OK, headers=headers, body=None, timeout=1)
    assert spy.calls == []


def test_benign_header_forms_still_pass():
    t, spy = wrap(RC.PUBLIC_COLLECTOR)
    for h in (None, {}, {"Accept": "application/json", "User-Agent": "x/1"}, [("Accept", "application/json")], {b"Accept": b"x"}):
        t.request("GET", URL_OK, headers=h, body=None, timeout=1)
    assert len(spy.calls) == 5


@pytest.mark.parametrize("name,headers", [b for b in BAD_HEADERS if b[0] in ("trailing space", "list of tuples", "bytes key", "cookie")])
def test_signed_header_forms_are_seen_as_signed_by_the_monitor_too(name, headers):
    # signed monitor reads are legal: the forms must be DETECTED (a signed read of an unsigned-only path is refused, a public path stays public)
    t, spy = wrap(RC.READ_ONLY_MONITOR)
    try:
        t.request("GET", f"https://{TN}/v5/market/time", headers=headers, body=None, timeout=1)
    except RC.CapabilityRefused:
        pass
    assert spy.calls == []                        # signed (or unparseable) request to a PUBLIC path: refused


@pytest.mark.parametrize("url", [
    f"https://{TN}/v5/market/ti\nme", f"https://{TN}/v5/market/ti\tme", f"https://{TN}/v5/market/ti\rme", f"https://{TN[:3]}\n{TN[3:]}/v5/market/time",
    f"https://{TN}/v5/market/time\n", f"https://{TN}/v5/market/time ", f" https://{TN}/v5/market/time", f"https://{TN}/v5/market/time?a=b c",
    f"https://{TN}/v5/market/t\x00ime", f"https://{TN}/v5/market/t\x7fime", f"https://{TN}/v5/market/tiéme", f"https://{TN}\\@evil.example/v5/market/time",
    f"https://{TN}/v5/market/time?x= ", f"ht\ntps://{TN}/v5/market/time",
])
def test_urls_with_control_whitespace_or_non_ascii_characters_are_refused_before_parsing(url):
    for role in (RC.PUBLIC_COLLECTOR, RC.READ_ONLY_MONITOR, RC.TESTNET_EXECUTOR):
        t, spy = wrap(role)
        with pytest.raises(RC.CapabilityRefused):
            t.request("GET", url, headers={}, body=None, timeout=1)
        assert spy.calls == []


@pytest.mark.parametrize("q", ["api_key=K", "sign=S", "signature=S", "apikey=K", "token=T", "secret=S", "API_KEY=K", "Api-Key=K", "X-BAPI-SIGN=s", "access_token=t",
                               "category=linear&api%5Fkey=K", "category=linear&%73ign=S", "api_secret=s", "key=k", "password=p"])
def test_credential_looking_query_params_are_refused(q):
    for role in (RC.PUBLIC_COLLECTOR, RC.READ_ONLY_MONITOR):
        t, spy = wrap(role)
        with pytest.raises(RC.CapabilityRefused):
            t.request("GET", f"https://{TN}/v5/market/tickers?category=linear&{q}", headers={}, body=None, timeout=1)
        assert spy.calls == []


def test_ordinary_query_params_still_pass():
    t, spy = wrap(RC.PUBLIC_COLLECTOR)
    t.request("GET", f"https://{TN}/v5/market/kline?category=linear&symbol=BTCUSDT&interval=60&limit=200&cursor=a%3Db", headers={}, body=None, timeout=1)
    assert len(spy.calls) == 1


def test_monitor_cannot_read_the_api_key_endpoint_off_testnet():
    for venue in ("mainnet", "demo"):
        mon = RC.Capability.for_role(RC.READ_ONLY_MONITOR, venue=venue)
        assert mon.check("GET", "/v5/user/query-api", True, mon.host) == "ENDPOINT_FORBIDDEN"
    mon = RC.Capability.for_role(RC.READ_ONLY_MONITOR, venue="testnet")        # the owner's testnet preflight (testnet_action_manifest) names it
    assert mon.check("GET", "/v5/user/query-api", True, mon.host) is None
    assert "/v5/user/query-api" not in RC.ACCOUNT_READ_PATHS


def test_a_trailing_newline_endpoint_is_not_canonical():
    c = RC.Capability.for_role(RC.PUBLIC_COLLECTOR, venue="testnet")
    assert c.check("GET", "/v5/market/time\n", False, c.host) == "ENDPOINT_NOT_CANONICAL"
    assert c.check("GET", "/v5/market/time", False, c.host + "\n") == "HOST_REFUSED"


def test_recent_trade_is_a_public_market_path():
    assert "/v5/market/recent-trade" in RC.PUBLIC_MARKET_PATHS
