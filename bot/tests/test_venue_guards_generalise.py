"""The venue guards are properties of the REGISTRY, not of the string "mainnet".

Before `venues.py`, "is this real money?" was `venue != "mainnet"` in five places, so
a venue added to the tuple would have been a sandbox: no acknowledgement, no
promotion gate, no transport refusal. These tests add venues that did not exist
when the guards were written (a synthetic LIVE one, a synthetic SANDBOX one, the
real-but-unqualified `eu`, and a typo) and prove each guard treats them by the
registry's word. Nothing here touches a network, a credential or a real database.
"""
import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import bybit_connection as bc  # noqa: E402
import config as C  # noqa: E402
import venues as V  # noqa: E402
from fake_bybit import FakeBybit  # noqa: E402
from persistence import StateStore  # noqa: E402

LIVE2 = V.Venue("live2", V.LIVE, "https://api.example-live.test", "wss://stream.example-live.test/v5/private",
                "mainnet", "mainnet", "synthetic live venue for the guard-generalisation proof")
SAND2 = V.Venue("sand2", V.SANDBOX, "https://api.example-sand.test", "wss://stream.example-sand.test/v5/private",
                "testnet", "testnet", "synthetic sandbox venue")


@pytest.fixture
def registry(monkeypatch):
    table = dict(V._QUALIFIED)
    table["live2"] = LIVE2
    table["sand2"] = SAND2
    monkeypatch.setattr(V, "_QUALIFIED", table)
    return table


def _client(tmp_path, venue, fake=None, **env):
    cfg = C.load(dict({"BYBIT_VENUE": venue, "PAPER_TRADING": "1"}, **env))
    store = StateStore(str(tmp_path / f"{venue}.db"))
    store.claim_writer()
    fake = fake or FakeBybit()
    return bc.BybitClient(config=cfg, store=store, transport=fake), fake, cfg


# -- registry semantics ----------------------------------------------------------------------------------

def test_only_registered_sandboxes_are_sandboxes(registry):
    assert V.is_sandbox("testnet") and V.is_sandbox("demo") and V.is_sandbox("sand2")
    for name in ("mainnet", "live2", "eu", "mainnet ", "MAINNET", "", None, 0, "typo", "api.bybit.com"):
        if isinstance(name, str) and name.strip().lower() in ("testnet", "demo"):
            continue
        assert V.requires_live_authorisation(name), name


def test_eu_is_named_refused_with_a_reason_and_never_a_sandbox():
    assert "eu" in V.unqualified() and "eu" not in V.qualified_names()
    with pytest.raises(V.VenueNotQualified, match="NOT established"):
        V.get("eu")
    assert V.requires_live_authorisation("eu")
    assert not V.is_sandbox("eu")


# -- config: the environment parser ----------------------------------------------------------------------

def test_config_marks_a_live_venue_as_not_a_sandbox(registry):
    cfg = C.load({"BYBIT_VENUE": "live2", "PAPER_TRADING": "1"})
    assert cfg.BYBIT_VENUE == "live2"
    assert cfg.USE_TESTNET is False                       # derived from the registry, not from "== mainnet"
    live, why = C.is_live_authorized(cfg)
    assert live is False and "not armed" in why


def test_config_treats_a_registered_sandbox_as_a_sandbox(registry):
    cfg = C.load({"BYBIT_VENUE": "sand2"})
    assert cfg.USE_TESTNET is True and C.is_live_authorized(cfg)[0] is False


@pytest.mark.parametrize("venue", ["eu", "EU", " eu "])
def test_config_refuses_eu_with_the_registered_reason(venue):
    with pytest.raises(C.ConfigError, match="NOT established"):
        C.load({"BYBIT_VENUE": venue})


def test_config_refuses_a_typo_instead_of_defaulting():
    with pytest.raises(C.ConfigError):
        C.load({"BYBIT_VENUE": "mainet"})


def test_use_testnet_cannot_contradict_a_live_venue(registry):
    with pytest.raises(C.ConfigError, match="disagree"):
        C.load({"BYBIT_VENUE": "live2", "USE_TESTNET": "1"})
    with pytest.raises(C.ConfigError, match="disagree"):
        C.load({"BYBIT_VENUE": "sand2", "USE_TESTNET": "0"})


# -- the client: construction and the transport backstop -------------------------------------------------

def test_the_client_reads_every_host_from_the_one_venue_row(registry, tmp_path):
    client, _, _ = _client(tmp_path, "live2")
    assert client.base_url == LIVE2.rest and client.ws_private_url == LIVE2.ws_private
    assert client.testnet is False
    assert client.private_ws_url() == LIVE2.ws_private


@pytest.mark.parametrize("venue", ["eu", "typo"])
def test_the_client_cannot_be_built_for_an_unqualified_or_unknown_venue(tmp_path, venue):
    cfg = types.SimpleNamespace(BYBIT_VENUE=venue, USE_TESTNET=True, PAPER_TRADING=True,
                                BYBIT_API_KEY="", BYBIT_API_SECRET="")
    with pytest.raises(ValueError):
        bc.resolve_client_venue(cfg)


@pytest.mark.parametrize("endpoint,body", [("/v5/order/create", {"symbol": "BTCUSDT"}),
                                           ("/v5/position/set-leverage", {"symbol": "BTCUSDT"})])
def test_a_live_venue_refuses_exposure_at_the_transport_without_authorisation(registry, tmp_path, endpoint, body):
    client, fake, _ = _client(tmp_path, "live2")
    with pytest.raises(bc.PermanentAPIError, match="LIVE_NOT_AUTHORIZED"):
        client._request("POST", endpoint, body=body, signed=True)
    assert fake.requests == []                              # nothing left the process


def test_a_live_venue_refuses_a_submit_without_authorisation(registry, tmp_path):
    client, fake, _ = _client(tmp_path, "live2")
    result = client._submit({"symbol": "BTCUSDT"}, "oid-1", "entry")
    assert result.reason == "LIVE_NOT_AUTHORIZED"
    assert not [r for r in fake.requests if "order/create" in r["url"]]


def test_the_real_mainnet_is_still_guarded(tmp_path):
    client, fake, _ = _client(tmp_path, "mainnet")
    with pytest.raises(bc.PermanentAPIError, match="LIVE_NOT_AUTHORIZED"):
        client._request("POST", "/v5/order/create", body={"symbol": "BTCUSDT"}, signed=True)
    assert fake.requests == []


def test_a_registered_sandbox_is_not_refused_by_the_live_backstop(registry, tmp_path):
    """Control: the guard is not 'refuse everything'. A sandbox reaches the transport."""
    client, fake, _ = _client(tmp_path, "sand2")
    try:
        client._request("POST", "/v5/position/set-leverage", body={"symbol": "BTCUSDT"}, signed=True)
    except bc.PermanentAPIError as exc:
        assert "LIVE_NOT_AUTHORIZED" not in str(exc)
    except Exception:       # a transient/fake-exchange answer is irrelevant here
        pass


# -- cross-wiring: a host that is not this venue's is refused before anything is sent ---------------------

def test_a_testnet_client_cannot_be_walked_onto_the_mainnet_host(tmp_path):
    client, fake, _ = _client(tmp_path, "testnet")
    client.base_url = V.rest_for("mainnet")                 # reassigned after construction
    with pytest.raises(bc.PermanentAPIError, match="is not the testnet host"):
        client._request("GET", "/v5/market/tickers", params={"category": "linear", "symbol": "BTCUSDT"})
    assert fake.requests == []


def test_a_private_stream_url_that_is_not_the_venues_is_refused_before_credentials_are_sent(tmp_path):
    client, _, _ = _client(tmp_path, "testnet")
    assert client.private_ws_url() == V.ws_private_for("testnet")
    client.ws_private_url = V.ws_private_for("mainnet")
    with pytest.raises(bc.PermanentAPIError, match="private stream host"):
        client.private_ws_url()
    client.ws_private_url = "ws://stream-testnet.bybit.com/v5/private"          # plaintext
    with pytest.raises(bc.PermanentAPIError):
        client.private_ws_url()


def test_demo_reads_mainnet_prices_but_is_a_sandbox_for_execution():
    assert V.feed_environment("demo") == "mainnet" and V.is_sandbox("demo")
    assert V.feed_environment("testnet") == "testnet" and V.feed_environment("mainnet") == "mainnet"
    for bad in ("eu", "typo", None):
        with pytest.raises(ValueError):
            V.feed_environment(bad)


# -- the repository lint ---------------------------------------------------------------------------------

def test_the_fly_lint_refuses_any_non_sandbox_venue_in_a_tracked_file(registry):
    from tools import fly_stack as FS

    def venue_problems(venue):
        cfg = FS.template()
        cfg["env"]["BYBIT_VENUE"] = venue
        return [p for p in FS.check(cfg) if "BYBIT_VENUE" in p]

    for venue in ("mainnet", "live2", "eu", "typo"):
        assert venue_problems(venue), venue
    assert not venue_problems("sand2")
    assert not venue_problems("testnet")


# -- main.py: the per-order gate and the environment label --------------------------------------------------

def _gate_cfg(registry, venue, **env):
    return C.load(dict({"BYBIT_VENUE": venue}, **env))


def test_the_per_order_gate_refuses_a_client_aimed_at_another_venues_host(registry):
    import main as app
    cfg = _gate_cfg(registry, "sand2", PAPER_TRADING="0")
    good = types.SimpleNamespace(base_url=SAND2.rest)
    assert app.carry_orders_permitted_for(cfg, good)[0] is True
    for other in (V.rest_for("testnet"), V.rest_for("mainnet"), LIVE2.rest):
        ok, why = app.carry_orders_permitted_for(cfg, types.SimpleNamespace(base_url=other))
        assert ok is False and "sand2" in why or "targets" in why, why
    ok, why = app.carry_orders_permitted_for(cfg, types.SimpleNamespace(base_url=""))
    assert ok is False


def test_a_sandbox_config_is_refused_a_client_on_any_live_venues_host(registry):
    import main as app
    cfg = _gate_cfg(registry, "testnet", PAPER_TRADING="0")
    for live in V.live_names():
        ok, why = app.carry_orders_permitted_for(cfg, types.SimpleNamespace(base_url=V.rest_for(live)))
        assert ok is False and live in why


def test_a_live_venue_config_without_authorisation_sends_no_carry_order(registry):
    import main as app
    cfg = _gate_cfg(registry, "live2", PAPER_TRADING="0")
    ok, why = app.carry_orders_permitted_for(cfg, types.SimpleNamespace(base_url=LIVE2.rest))
    # config.load already forces PAPER on an unarmed live venue; either refusal is correct
    assert ok is False and ("paper" in why.lower() or "live authorisation" in why)


@pytest.mark.parametrize("venue,use_testnet,paper,expected", [
    ("testnet", True, True, "testnet"), ("demo", True, False, "testnet"),
    ("mainnet", False, True, "paper"), ("mainnet", False, False, "mainnet"),
    ("live2", False, False, "mainnet"),
    # the label never reads safer than the more dangerous of flag and registry
    ("mainnet", True, False, "mainnet"), ("live2", True, True, "paper"),
    ("eu", True, False, "mainnet"), ("typo", True, False, "mainnet"),
    (None, True, False, "testnet"), (None, False, False, "mainnet"),
])
def test_the_execution_label_never_reads_safer_than_the_registry(registry, venue, use_testnet, paper, expected):
    import main as app
    cfg = types.SimpleNamespace(BYBIT_VENUE=venue, USE_TESTNET=use_testnet, PAPER_TRADING=paper)
    assert app.execution_environment_of(cfg) == expected
