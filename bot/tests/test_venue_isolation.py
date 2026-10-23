"""Which exchange a client talks to is decided once, centrally, verified on the
URL actually sent, and an unarmed mainnet client cannot open exposure.

Everything here is offline: a recording transport stands in for the network, and
the credentials are placeholders. No request leaves the process."""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import bybit_connection as bc  # noqa: E402
from persistence import StateStore  # noqa: E402

FAKE_KEY, FAKE_SECRET = "FAKEKEY-not-a-credential", "FAKESECRET-not-a-credential"


class Recorder(bc.Transport):
    def __init__(self):
        self.calls = []

    def request(self, method, url, *, headers, params=None, body=None,
                timeout=10.0):
        self.calls.append((method, url))
        return 200, json.dumps({"retCode": 0, "retMsg": "OK", "result": {
            "list": [], "timeSecond": "1700000000"}, "time": 1700000000000})


class Cfg:
    BYBIT_API_KEY = FAKE_KEY
    BYBIT_API_SECRET = FAKE_SECRET
    BYBIT_RECV_WINDOW_MS = 5000
    REQUEST_TIMEOUT_SECONDS = 5.0
    USE_LEVERAGE = False
    SYMBOL_FILTERS_TTL = 3600
    ORDERLINK_PREFIX = "BB"


def cfg(**kw):
    return type("C", (Cfg,), kw)()


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "s.db"))
    yield s
    s.close()


def make(store, **kw):
    t = Recorder()
    return bc.BybitClient(config=cfg(**kw), store=store, transport=t), t


class TestTheVenueIsNeverGuessed:
    def test_nothing_stated_is_refused_not_defaulted(self):
        with pytest.raises(ValueError):
            bc.resolve_client_venue(cfg())

    @pytest.mark.parametrize("blank", [None, "", "  "])
    def test_a_blank_testnet_flag_is_not_read_as_mainnet(self, blank):
        with pytest.raises(ValueError):
            bc.resolve_client_venue(cfg(USE_TESTNET=blank))

    @pytest.mark.parametrize("junk", ["maybe", "tru", "2", "nope"])
    def test_an_unreadable_flag_is_refused(self, junk):
        with pytest.raises(ValueError):
            bc.resolve_client_venue(cfg(USE_TESTNET=junk))

    @pytest.mark.parametrize("venue,flag", [
        ("mainnet", True), ("mainnet", "1"), ("testnet", False),
        ("demo", False), ("demo", "0")])
    def test_a_contradiction_is_refused(self, venue, flag):
        with pytest.raises(ValueError, match="disagree"):
            bc.resolve_client_venue(cfg(BYBIT_VENUE=venue, USE_TESTNET=flag))

    def test_an_unknown_venue_is_refused(self):
        with pytest.raises(ValueError):
            bc.resolve_client_venue(cfg(BYBIT_VENUE="bybit", USE_TESTNET=True))

    @pytest.mark.parametrize("kw,expected", [
        (dict(USE_TESTNET=True), "testnet"),
        (dict(USE_TESTNET=False), "mainnet"),     # an explicit, stated boolean
        (dict(BYBIT_VENUE="testnet"), "testnet"),
        (dict(BYBIT_VENUE=" DEMO "), "demo"),
        (dict(BYBIT_VENUE="mainnet", USE_TESTNET=False), "mainnet"),
        (dict(BYBIT_VENUE="testnet", USE_TESTNET="1"), "testnet")])
    def test_agreeing_configs_resolve(self, kw, expected):
        assert bc.resolve_client_venue(cfg(**kw)) == expected

    def test_the_constructor_refuses_a_contradictory_config(self, store):
        with pytest.raises(ValueError):
            bc.BybitClient(config=cfg(BYBIT_VENUE="mainnet", USE_TESTNET=True),
                           store=store, transport=Recorder())


class TestTheUrlActuallySentIsChecked:
    def test_a_tampered_base_url_sends_nothing(self, store):
        client, t = make(store, BYBIT_VENUE="testnet")
        client.base_url = bc.MAINNET_REST
        with pytest.raises(bc.PermanentAPIError):
            client._request("GET", "/v5/market/tickers",
                            params={"category": "spot", "symbol": "BTCUSDT"})
        assert t.calls == []

    @pytest.mark.parametrize("bad", [
        "http://api-testnet.bybit.com", "https://api-testnet.bybit.com.evil.io",
        "https://evil.io/api-testnet.bybit.com", "https://api.bybit.com"])
    def test_only_the_venues_own_https_host_passes(self, store, bad):
        client, t = make(store, BYBIT_VENUE="testnet")
        client.base_url = bad
        with pytest.raises(bc.PermanentAPIError):
            client._request("GET", "/v5/market/tickers", params={})
        with pytest.raises(bc.PermanentAPIError):
            client.sync_time()
        assert t.calls == []

    @pytest.mark.parametrize("venue", ["testnet", "demo", "mainnet"])
    def test_the_constructed_client_reaches_its_own_host(self, store, venue):
        client, t = make(store, BYBIT_VENUE=venue)
        client._request("GET", "/v5/market/time")
        assert t.calls and t.calls[0][1].startswith(
            f"https://{bc.VENUE_HOSTS[venue]}/")

    def test_the_hosts_are_distinct(self):
        assert len(set(bc.VENUE_HOSTS.values())) == 3


class TestAnUnarmedMainnetClientCannotOpenExposure:
    BODY = {"category": "spot", "symbol": "BTCUSDT", "side": "Buy",
            "orderType": "Market", "qty": "0.001", "orderLinkId": "x1"}

    def test_a_direct_order_create_is_refused_and_not_counted(self, store):
        # carry_broker calls _request directly, past _submit
        client, t = make(store, BYBIT_VENUE="mainnet", USE_TESTNET=False)
        with pytest.raises(bc.PermanentAPIError, match="LIVE_NOT_AUTHORIZED"):
            client._request("POST", "/v5/order/create", body=self.BODY,
                            signed=True)
        assert t.calls == [] and client.orders_submitted == 0

    def test_set_leverage_is_refused_too(self, store):
        client, t = make(store, BYBIT_VENUE="mainnet", USE_TESTNET=False)
        with pytest.raises(bc.PermanentAPIError, match="LIVE_NOT_AUTHORIZED"):
            client._request("POST", "/v5/position/set-leverage",
                            body={"symbol": "BTCUSDT"}, signed=True)
        assert t.calls == []

    def test_cancel_is_still_allowed_unarmed(self, store):
        client, t = make(store, BYBIT_VENUE="mainnet", USE_TESTNET=False)
        client._request("POST", "/v5/order/cancel-all", signed=True,
                        body={"category": "spot"})
        assert [c[0] for c in t.calls] == ["POST"]

    def test_the_placed_order_path_reports_not_authorized(self, store):
        client, t = make(store, BYBIT_VENUE="mainnet", USE_TESTNET=False)
        result = client._submit(dict(self.BODY), "x1", "test")
        assert result.ok is False and result.reason == "LIVE_NOT_AUTHORIZED"
        assert t.calls == [] and client.orders_submitted == 0

    def test_a_testnet_client_is_not_blocked_by_the_live_gate(self, store):
        client, t = make(store, BYBIT_VENUE="testnet", USE_TESTNET=True)
        client._request("POST", "/v5/order/create", body=self.BODY, signed=True)
        assert client.orders_submitted == 1 and len(t.calls) == 1


class TestDiagnosticsRevealNoSecret:
    def test_describe_and_repr_carry_no_credential(self, store):
        client, _ = make(store, BYBIT_VENUE="testnet", USE_TESTNET=True)
        text = json.dumps(client.describe()) + repr(client) + str(client.describe())
        assert FAKE_KEY not in text and FAKE_SECRET not in text
        d = client.describe()
        assert d["venue"] == "testnet" and d["host"] == "api-testnet.bybit.com"
        assert d["sandbox"] is True and d["credentials_present"] is True
        assert d["live_authorized"] is False

    def test_a_signed_request_error_names_no_secret(self, store):
        client, t = make(store, BYBIT_VENUE="testnet")
        client.base_url = "https://api.bybit.com"
        with pytest.raises(bc.PermanentAPIError) as err:
            client._request("GET", "/v5/account/wallet-balance", signed=True,
                            params={"accountType": "UNIFIED"})
        assert FAKE_KEY not in str(err.value) and FAKE_SECRET not in str(err.value)


class TestTheTestRunIsOffline:
    def test_no_exchange_credentials_or_venue_selection_are_inherited(self):
        if os.environ.get("BOT_ALLOW_NETWORK_TESTS") == "1":
            pytest.skip("integration opt-in")
        for name in ("BYBIT_API_KEY", "BYBIT_API_SECRET", "BYBIT_VENUE",
                     "USE_TESTNET", "PAPER_TRADING", "LIVE_TRADING_ACK"):
            assert name not in os.environ, name

    def test_the_http_layer_refuses_to_leave_the_machine(self):
        if os.environ.get("BOT_ALLOW_NETWORK_TESTS") == "1":
            pytest.skip("integration opt-in")
        import requests
        with pytest.raises(RuntimeError, match="offline test run"):
            requests.get("https://api-testnet.bybit.com/v5/market/time")
        with pytest.raises(RuntimeError, match="offline test run"):
            bc.RequestsTransport().request(
                "GET", "https://api.bybit.com/v5/market/time", headers={})

    def test_an_empty_environment_loads_as_a_paper_testnet(self):
        import config
        c = config.load({})
        assert c.USE_TESTNET is True and c.PAPER_TRADING is True
        assert c.BYBIT_VENUE == "testnet" and c.LIVE_AUTHORIZED is False
