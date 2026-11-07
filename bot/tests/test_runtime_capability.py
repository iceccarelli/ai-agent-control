"""Capability.check: per-role positive controls and a long hostile table. Offline, pure."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import runtime_capability as RC  # noqa: E402
import venues  # noqa: E402

TN = venues.get("testnet").host
MN = venues.get("mainnet").host
DM = venues.get("demo").host


def cap(role, venue="testnet"):
    return RC.Capability.for_role(role, venue=venue, credential_class=RC.TESTNET_CREDENTIAL_CLASS if role == RC.TESTNET_EXECUTOR else None)


COLLECTOR, MONITOR, BACKUP, EXECUTOR = (cap(r) for r in RC.ROLES)

ALLOWED = [
    (COLLECTOR, "GET", p, False, TN) for p in sorted(RC.PUBLIC_MARKET_PATHS)
] + [
    (cap(RC.PUBLIC_COLLECTOR, "mainnet"), "GET", "/v5/market/tickers", False, MN),
    (cap(RC.PUBLIC_COLLECTOR, "demo"), "GET", "/v5/market/kline", False, DM),
] + [
    (MONITOR, "GET", p, True, TN) for p in sorted(RC.ACCOUNT_READ_PATHS)
] + [
    (MONITOR, "GET", "/v5/market/time", False, TN),
    (cap(RC.READ_ONLY_MONITOR, "mainnet"), "GET", "/v5/position/list", True, MN),
] + [
    (EXECUTOR, "POST", p, True, TN) for p in sorted(RC.EXECUTOR_POST_PATHS)
] + [
    (EXECUTOR, "GET", "/v5/position/list", True, TN),
    (EXECUTOR, "GET", "/v5/market/orderbook", False, TN),
]


@pytest.mark.parametrize("c,m,e,s,h", ALLOWED)
def test_positive_controls(c, m, e, s, h):
    assert c.check(m, e, s, h) is None


FORBIDDEN_PATHS = [
    "/v5/asset/withdraw/create", "/v5/asset/transfer/inter-transfer", "/v5/asset/transfer/universal-transfer", "/v5/asset/deposit/query-record",
    "/v5/account/withdraw", "/v5/account/transfer", "/v5/user/create-sub-member", "/v5/user/create-sub-api", "/v5/user/update-api",
    "/v5/user/delete-api", "/v5/spot-margin-trade/switch-mode", "/v5/crypto-loan/borrow", "/v5/order/amend", "/v5/order/create-batch",
    "/v5/order/amend-batch", "/v5/position/switch-mode", "/v5/account/set-margin-mode", "/v5/account/upgrade-to-uta",
]
BAD_FORMS = [
    "/v5/order/create?x=1", "/v5/order/create?", "/V5/order/create", "/v5/Order/create", "/v5/order/Create", "/v5/order/create/", "//v5/order/create",
    "/v5//order/create", "/v5/order/../order/create", "/v5/order/./create", "/v5/order/create#f", "/v5/order/create ", " /v5/order/create",
    "/v5/order/create\n", "/v5/order/create\t", "v5/order/create", "/v5/order/create%00", "/v5/order/%63reate", "/v5/order/create;x", "/v5/order/create\\",
    "/v5/market/tickers?category=linear", "/v5/market/Tickers", "/v5/market/tickers/", "/v5/", "/v5", "/", "", "/v6/order/create",
    "/v5/order/create-", "/v5/-order/create", "/v5/order/creatе", "/v5/market/time/..",
]
UNKNOWN = ["/v5/market/recent-trade", "/v5/market/open-interest", "/v5/account/unknown", "/v5/order/unknown", "/v5/position/switch-isolated", "/other/path",
           "/v5/position/confirm-pending-mmr", "/v5/account/borrow"]
ROLE_CAPS = [COLLECTOR, MONITOR, BACKUP, EXECUTOR]


@pytest.mark.parametrize("c", ROLE_CAPS, ids=lambda c: c.role)
@pytest.mark.parametrize("path", FORBIDDEN_PATHS + BAD_FORMS + UNKNOWN)
@pytest.mark.parametrize("method,signed", [("GET", True), ("GET", False), ("POST", True), ("POST", False)])
def test_hostile_path_table_refused_for_every_role(c, path, method, signed):
    assert c.check(method, path, signed, c.host) is not None


@pytest.mark.parametrize("c", [COLLECTOR, MONITOR, EXECUTOR], ids=lambda c: c.role)
def test_withdrawal_transfer_asset_family_has_an_explicit_code(c):
    for p in ("/v5/asset/withdraw/create", "/v5/account/transfer", "/v5/asset/transfer/inter-transfer", "/v5/user/create-sub-api"):
        for method in ("GET", "POST"):
            for signed in (True, False):
                assert c.check(method, p, signed, c.host) == "ENDPOINT_FORBIDDEN"


def test_backup_refuses_everything_even_positive_controls_of_other_roles():
    for c, m, e, s, h in ALLOWED:
        assert BACKUP.check(m, e, s, h).startswith("NO_VENUE_ACCESS")
    assert BACKUP.check("GET", "/v5/market/time", False, TN).startswith("NO_VENUE_ACCESS")


def test_collector_cannot_post_or_sign():
    for p in sorted(RC.EXECUTOR_POST_PATHS | RC.ACCOUNT_READ_PATHS | RC.PUBLIC_MARKET_PATHS):
        assert COLLECTOR.check("POST", p, True, TN)
        assert COLLECTOR.check("POST", p, False, TN)
    for p in sorted(RC.ACCOUNT_READ_PATHS | RC.PUBLIC_MARKET_PATHS):
        assert COLLECTOR.check("GET", p, True, TN) in ("COLLECTOR_UNSIGNED_ONLY", "ENDPOINT_NOT_ALLOWED")
    assert COLLECTOR.check("GET", "/v5/market/tickers", True, TN) == "COLLECTOR_UNSIGNED_ONLY"
    assert COLLECTOR.check("GET", "/v5/position/list", False, TN) == "ENDPOINT_NOT_ALLOWED"


def test_monitor_cannot_post_delete_or_trade():
    for method in ("POST", "DELETE", "PUT", "PATCH", "get", "post", "", None, 1):
        for p in sorted(RC.EXECUTOR_POST_PATHS | RC.ACCOUNT_READ_PATHS):
            for s in (True, False):
                assert MONITOR.check(method, p, s, TN) is not None
    assert MONITOR.check("GET", "/v5/order/create", True, TN) is not None
    assert MONITOR.check("GET", "/v5/position/list", False, TN) == "ENDPOINT_NOT_ALLOWED"   # an account read must be signed, a market read unsigned


def test_executor_post_rules():
    assert EXECUTOR.check("POST", "/v5/order/create", False, TN) == "POST_MUST_BE_SIGNED"
    assert EXECUTOR.check("DELETE", "/v5/order/cancel", True, TN) == "METHOD_REFUSED"
    assert EXECUTOR.check("POST", "/v5/order/amend", True, TN) == "ENDPOINT_NOT_ALLOWED"
    assert EXECUTOR.check("POST", "/v5/order/create-batch", True, TN) == "ENDPOINT_NOT_ALLOWED"
    assert EXECUTOR.check("POST", "/v5/market/tickers", True, TN) == "ENDPOINT_NOT_ALLOWED"


@pytest.mark.parametrize("c", [COLLECTOR, MONITOR, EXECUTOR], ids=lambda c: c.role)
def test_host_must_be_the_registry_host_exactly(c):
    p, s = ("/v5/market/time", False)
    assert c.check("GET", p, s, c.host) is None
    for h in (MN if c.host != MN else TN, DM, c.host.upper(), c.host + ".", c.host + ":443", c.host + ".evil.example", "evil." + c.host, "", None, "api.bybit.eu",
              c.host + "/", " " + c.host, "localhost", "127.0.0.1"):
        assert c.check("GET", p, s, h) == "HOST_REFUSED"


def test_executor_never_mainnet_or_demo_even_if_the_host_matches_a_forged_capability():
    for v in ("mainnet", "demo", "eu", "nope", "", None):
        with pytest.raises(RC.CapabilityConstructionError):
            RC.Capability.for_role(RC.TESTNET_EXECUTOR, venue=v, credential_class=RC.TESTNET_CREDENTIAL_CLASS)
    for forged in (RC.Capability(RC.TESTNET_EXECUTOR, "mainnet", MN, RC.TESTNET_CREDENTIAL_CLASS), RC.Capability(RC.TESTNET_EXECUTOR, "demo", DM, RC.TESTNET_CREDENTIAL_CLASS),
                   RC.Capability(RC.TESTNET_EXECUTOR, "testnet", TN, "generic"), RC.Capability(RC.TESTNET_EXECUTOR, "testnet", TN, None)):
        assert forged.check("POST", "/v5/order/create", True, forged.host) == "EXECUTOR_NOT_TESTNET"


def test_non_boolean_signed_and_unknown_role_refused():
    for s in (1, 0, "true", None):
        assert MONITOR.check("GET", "/v5/position/list", s, TN) == "SIGNED_FLAG_NOT_BOOLEAN"
    assert RC.Capability("root", "testnet", TN, None).check("GET", "/v5/market/time", False, TN) == "ROLE_UNKNOWN"


def test_every_listed_path_is_canonical_and_sets_are_disjoint_from_forbidden():
    for p in RC.PUBLIC_MARKET_PATHS | RC.ACCOUNT_READ_PATHS | RC.EXECUTOR_POST_PATHS:
        assert RC._CANONICAL.match(p)
        assert not any(w in p for w in RC._FORBIDDEN_WORDS)
    assert not (RC.PUBLIC_MARKET_PATHS & RC.EXECUTOR_POST_PATHS) and not (RC.ACCOUNT_READ_PATHS & RC.EXECUTOR_POST_PATHS)
