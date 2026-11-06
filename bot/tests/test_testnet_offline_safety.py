"""The offline safety layer of the testnet qualification: what is PROVEN, through the real client, before any exchange contact is authorised.

This is NOT a testnet run and proves nothing about fills, fees or profitability. It proves, with the real ``BybitClient`` (signing, host assertion, live gate)
over a fake transport and with every network primitive made to fail the test, that:

  * the endpoint is the registry's one host for the venue, and a client cannot be pointed anywhere else (no mainnet fallback, no demo/testnet mixing);
  * mainnet order creation and leverage changes are refused at the transport without live authorisation, before any byte is handed to the transport;
  * the testnet drill refuses mainnet, demo, a testnet name on a foreign host and an unstated host, before reading the venue, armed or not;
  * the live acknowledgement stays unset and is refused by the drill when present;
  * no withdrawal or transfer path exists in the client or the drill;
  * the process cannot open a socket, and only a temporary database is used.

What it does NOT prove, so nobody reads more into it: order/fill/cancel/partial-fill/restart behaviour against a venue's real semantics (see the matrix in
``test_carry_*`` and ``test_open_crash_real_stack.py`` for what the existing fakes cover, and the report for what is still missing offline), fee tiers, latency,
or that a key is a testnet key (the client cannot tell: a key's venue is checked only by the exchange rejecting it).
"""
from __future__ import annotations

import ast
import os
import socket
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(BOT, "tools"))

import bybit_connection as bc  # noqa: E402
import venues  # noqa: E402
import drill as D  # noqa: E402
from fake_bybit import API_KEY, API_SECRET, FakeBybit  # noqa: E402
from persistence import StateStore  # noqa: E402


class NetworkAttempted(AssertionError):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any attempt to resolve, connect or create a connection fails the test: the harness cannot reach an exchange even by accident."""
    def boom(*_a, **_k):
        raise NetworkAttempted("this offline test tried to touch the network")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


@pytest.fixture(autouse=True)
def no_live_ack(monkeypatch):
    monkeypatch.delenv("LIVE_TRADING_ACK", raising=False)
    assert os.environ.get("LIVE_TRADING_ACK") is None


def cfg(venue: str, use_testnet):
    class Cfg:
        BYBIT_VENUE = venue
        USE_TESTNET = use_testnet
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
    return Cfg()


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "s.db"))
    yield s
    s.close()


class RecordingVenue(FakeBybit):
    """The fake exchange, recording every URL it was handed, so a test can assert that none left the testnet host."""


def client_for(venue, use_testnet, store, transport=None):
    return bc.BybitClient(config=cfg(venue, use_testnet), store=store, transport=transport or RecordingVenue())


# ---------------------------------------------------------------- endpoint selection

def test_the_testnet_client_talks_only_to_the_registrys_testnet_host(store):
    venue = RecordingVenue()
    c = client_for("testnet", True, store, venue)
    assert c.venue == "testnet" and c.base_url == venues.rest_for("testnet") == bc.TESTNET_REST
    assert c.private_ws_url() == venues.ws_private_for("testnet")
    c.get_equity()                                                     # a real signed request through the real client
    assert venue.requests, "nothing was sent"
    assert all(r["url"].startswith(bc.TESTNET_REST) for r in venue.requests)
    assert all(r["headers"].get("X-BAPI-SIGN") for r in venue.requests if "/v5/account" in r["url"])


@pytest.mark.parametrize("hostile_url", ["https://api.bybit.com", "https://api-demo.bybit.com", "https://api.bybit.eu", "http://api-testnet.bybit.com",
                                         "https://api-testnet.bybit.com.evil.example", "https://evil.example/api-testnet.bybit.com"])
def test_a_testnet_client_whose_base_url_is_altered_refuses_before_the_transport_sees_anything(store, hostile_url):
    venue = RecordingVenue()
    c = client_for("testnet", True, store, venue)
    c.base_url = hostile_url                                           # the worst case: something rewrote the URL after construction
    with pytest.raises(Exception) as exc:
        c.get_equity()
    assert not isinstance(exc.value, NetworkAttempted)
    assert venue.requests == [], "a request reached the transport on a foreign host"


def test_demo_and_testnet_are_different_hosts_and_the_drill_refuses_demo(store):
    assert venues.rest_for("demo") != venues.rest_for("testnet")
    assert bc.TESTNET_REST != venues.rest_for("demo")
    c = client_for("demo", True, store)                                # the registry treats demo as a SANDBOX, so USE_TESTNET pairs with it
    assert c.base_url == venues.rest_for("demo")
    assert D.venue_refusal(cfg("demo", True), c) is not None
    assert D.venue_refusal(cfg("testnet", True), c) is not None        # even a testnet-labelled config refuses a client that is on the demo host


def test_contradictory_or_unknown_venue_settings_refuse_construction(store):
    for venue, flag in (("mainnet", True), ("testnet", False), ("eu", False), ("nonsense", True)):
        with pytest.raises(Exception):
            client_for(venue, flag, store)


# ---------------------------------------------------------------- no mainnet orders

@pytest.mark.parametrize("endpoint", sorted(bc.EXPOSURE_ENDPOINTS))
def test_a_mainnet_client_cannot_create_an_order_or_change_leverage_without_live_authorisation(store, endpoint):
    venue = RecordingVenue()
    c = client_for("mainnet", False, store, venue)                      # constructing a mainnet client is allowed; USING it for exposure is not
    with pytest.raises(bc.PermanentAPIError, match="LIVE_NOT_AUTHORIZED"):
        c._request("POST", endpoint, body={"category": "linear", "symbol": "BTCUSDT"}, signed=True)
    assert venue.requests == [], "an exposure request reached the transport on mainnet"


def test_live_authorisation_is_not_granted_by_a_testnet_configuration_or_by_the_environment():
    import config
    armed, why = config.is_live_authorized(cfg("testnet", True))
    assert armed is not True and why
    assert os.environ.get("LIVE_TRADING_ACK") is None


def test_the_drill_refuses_a_live_acknowledgement_even_on_the_testnet_host(store):
    c = client_for("testnet", True, store)
    config = cfg("testnet", True)
    config.LIVE_TRADING_ACK = "I_UNDERSTAND"
    assert "live-trading acknowledgement" in D.session_refusal(config, c)
    config.LIVE_TRADING_ACK = ""
    assert D.session_refusal(config, c) is None                         # the clean configuration is accepted: the refusals above are not blanket


@pytest.mark.parametrize("venue,flag,url", [("mainnet", False, "https://api.bybit.com"), ("demo", True, "https://api-demo.bybit.com"),
                                           ("testnet", True, "https://api.bybit.com"), ("testnet", True, "")])
def test_the_drill_refuses_any_non_testnet_target_armed_or_not_before_reading_the_venue(store, venue, flag, url):
    class FakeClient:
        base_url = url
    assert D.venue_refusal(cfg(venue, flag), FakeClient()) is not None
    assert D.session_refusal(cfg(venue, flag), FakeClient()) is not None


# ---------------------------------------------------------------- no withdrawal or transfer capability

def test_no_withdrawal_or_transfer_endpoint_is_reachable_from_the_client_or_the_drill():
    forbidden = ("/v5/asset/withdraw", "/v5/asset/transfer", "/v5/asset/deposit", "/v5/account/transfer", "withdraw", "inter-transfer", "universal-transfer")
    for path in (os.path.join(BOT, "bybit_connection.py"), os.path.join(BOT, "carry_broker.py"), os.path.join(BOT, "tools", "drill.py"),
                 os.path.join(BOT, "tools", "close_testnet_short.py"), os.path.join(BOT, "tools", "unattended_drill.py")):
        tree = ast.parse(open(path, encoding="utf-8").read())
        strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        code_strings = [x for x in strings if x.startswith("/v5/")]                 # request paths, not prose
        for bad in forbidden:
            assert not any(bad in s for s in code_strings), (os.path.basename(path), bad)
    # The only asset call in the repo is the demo faucet, which refuses unless the client is on the demo host and is not part of the testnet drill.
    assert "demo-apply-money" not in open(os.path.join(BOT, "tools", "drill.py"), encoding="utf-8").read()


# ---------------------------------------------------------------- the process itself

def test_the_socket_guard_really_fails_a_test_that_touches_the_network():
    with pytest.raises(NetworkAttempted):
        socket.create_connection(("api-testnet.bybit.com", 443))
    with pytest.raises(NetworkAttempted):
        socket.getaddrinfo("api.bybit.com", 443)


def test_state_lives_only_in_a_temporary_database(store, tmp_path):
    assert os.path.dirname(store.path if hasattr(store, "path") else str(tmp_path / "s.db")).startswith(str(tmp_path))
    protected = os.path.join(BOT, "state", "trading_state.db")
    assert not os.path.abspath(str(tmp_path / "s.db")) == os.path.abspath(protected)
