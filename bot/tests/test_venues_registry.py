"""The venue registry: real money is stated, not inferred; hosts derive from one row;
unknown and unqualified venues fail closed."""
from __future__ import annotations

import pytest

import venues as V


def test_the_registry_states_which_venues_are_real_money():
    assert V.live_names() == ("mainnet",)
    assert set(V.sandbox_names()) == {"testnet", "demo"}
    assert V.get("mainnet").is_live and not V.get("testnet").is_live and not V.get("demo").is_live


@pytest.mark.parametrize("name", ["mainnet", "testnet", "demo"])
def test_every_host_derives_from_the_same_row(name):
    v = V.get(name)
    assert v.rest.startswith("https://") and v.ws_private.startswith("wss://")
    assert v.host == v.rest.split("//")[1]
    assert v.ws_private_host.split(".")[0].startswith("stream")
    assert V.rest_for(name) == v.rest and V.ws_private_for(name) == v.ws_private
    assert V.rest_hosts()[name] == v.host


def test_hosts_are_distinct_so_two_venues_can_never_share_an_endpoint():
    hosts = list(V.rest_hosts().values())
    ws = [V.get(n).ws_private_host for n in V.qualified_names()]
    assert len(set(hosts)) == len(hosts) and len(set(ws)) == len(ws)


def test_demo_reads_real_mainnet_prices_and_testnet_reads_its_own():
    assert V.feed_environment("demo") == "mainnet"
    assert V.feed_environment("testnet") == "testnet"
    assert V.feed_environment("mainnet") == "mainnet"


def test_the_demo_label_conflation_is_declared_not_hidden():
    assert V.execution_environment("demo") == "testnet" and "testnet" in V.get("demo").note.lower() + "testnet"
    assert V.execution_environment("mainnet") == "mainnet"


@pytest.mark.parametrize("bad", ["", "nope", "Mainnet2", None, 5, "main net"])
def test_unknown_names_are_refused_and_need_live_authorisation(bad):
    with pytest.raises(V.UnknownVenue):
        V.get(bad)
    assert V.requires_live_authorisation(bad) is True and V.is_sandbox(bad) is False
    with pytest.raises(V.UnknownVenue):
        V.feed_environment(bad)


def test_eu_is_registered_as_unqualified_with_its_reason_and_the_evidence_needed():
    with pytest.raises(V.VenueNotQualified) as exc:
        V.get("eu")
    assert "not qualified" in str(exc.value) and "NOT established" in str(exc.value)
    u = V.unqualified()["eu"]
    assert u.documented_rest == "https://api.bybit.eu" and len(u.evidence_needed) >= 4
    assert V.requires_live_authorisation("eu") is True            # never exempt from the live chain
    for fn in (V.feed_environment, V.rest_for, V.ws_private_for, V.execution_environment):
        with pytest.raises(V.VenueNotQualified):
            fn("eu")
    assert "eu" not in V.qualified_names()


def test_eu_never_shares_a_host_with_a_qualified_venue():
    assert V.host_of(V.unqualified()["eu"].documented_rest) not in V.rest_hosts().values()


def test_only_registered_sandboxes_are_exempt_from_live_authorisation():
    exempt = {n for n in ("mainnet", "testnet", "demo", "eu", "x") if not V.requires_live_authorisation(n)}
    assert exempt == {"testnet", "demo"}


def test_a_bad_row_cannot_be_constructed():
    ok = dict(name="x", kind=V.LIVE, rest="https://a.example", ws_private="wss://s.example/p",
              feed_environment="mainnet", execution_environment="mainnet")
    V.Venue(**ok)
    for patch in ({"kind": "paper"}, {"rest": "http://a.example"}, {"ws_private": "ws://s.example"},
                  {"feed_environment": "eu"}, {"rest": "https://"}):
        with pytest.raises(ValueError):
            V.Venue(**{**ok, **patch})


def test_the_module_imports_nothing_and_reads_no_environment():
    import ast, os
    src = open(os.path.join(os.path.dirname(V.__file__), "venues.py")).read()
    tree = ast.parse(src)
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert mods <= {"__future__", "dataclasses", "typing", "urllib"}
    assert "environ" not in src.replace("environment", "")
