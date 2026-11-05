"""EU qualification is decided by owner-collected evidence, mechanically, and can
never authorise an order."""
from __future__ import annotations

import copy

import pytest

import venue_qualification as Q


def good():
    return {
        "rest_host": "api.bybit.eu", "ws_public_host": "stream.bybit.eu", "ws_private_host": "stream.bybit.eu",
        "official_docs_urls": ["https://bybit-exchange.github.io/eu-docs/example"],
        "instruments_linear_btcusdt": {"retCode": 0, "status": "Trading"},
        "key_permissions": {"read_only": True, "ip_restricted": True, "ips": ["203.0.113.7"],
                            "permissions": {"ContractTrade": ["Order", "Position"], "Spot": ["SpotTrade"], "Wallet": []}},
        "fee_rates": {"source": "GET /v5/account/fee-rate", "category": "linear", "symbol": "BTCUSDT",
                      "taker_fee_rate": 0.00055, "maker_fee_rate": 0.0002},
        "collateral_rules": {"source": "GET /v5/account/collateral-info", "coin": "BTC", "collateral_ratio": 0.9,
                             "margin_collateral": True},
        "support_confirmation": {"reference": "TICKET-REDACTED", "statement": "derivatives are available to this account"},
    }


def test_complete_consistent_evidence_qualifies_but_never_authorises_orders():
    r = Q.evaluate_eu(good())
    assert r.verdict == Q.QUALIFIED and r.reasons == () and r.missing == ()
    assert r.authorises_orders is False


def test_nothing_collected_is_unverified_and_names_everything_missing():
    r = Q.evaluate_eu({})
    assert r.verdict == Q.UNVERIFIED and not r.reasons
    assert {"rest_host", "instruments_linear_btcusdt", "key_permissions", "support_confirmation", "fee_rates", "collateral_rules",
            "official_docs_urls", "ws_public_host", "ws_private_host"} <= set(r.missing)


@pytest.mark.parametrize("mutate,needle", [
    (lambda e: e.update(rest_host="api.bybit.com"), "WRONG_HOST"),
    (lambda e: e.update(ws_private_host="stream.bybit.com"), "WRONG_WS_PRIVATE_HOST"),
    (lambda e: e["instruments_linear_btcusdt"].update(retCode=10001), "LINEAR_INSTRUMENT_RETCODE_NOT_ZERO"),
    (lambda e: e["instruments_linear_btcusdt"].update(status="Closed"), "LINEAR_INSTRUMENT_NOT_TRADING"),
    (lambda e: e["key_permissions"].update(permissions={"Spot": ["SpotTrade"]}), "NO_DERIVATIVES_PERMISSION"),
    (lambda e: e["key_permissions"]["permissions"].update(Wallet=["Withdraw"]), "WITHDRAW_PERMISSION_PRESENT"),
    (lambda e: e["key_permissions"].update(ip_restricted=False), "KEY_NOT_IP_RESTRICTED"),
    (lambda e: e["key_permissions"].update(ips=[]), "KEY_NOT_IP_RESTRICTED"),
    (lambda e: e["fee_rates"].update(taker_fee_rate=float("nan")), "FEE_RATES_INVALID"),
    (lambda e: e["fee_rates"].update(taker_fee_rate=-0.1), "FEE_RATES_INVALID"),
    (lambda e: e["fee_rates"].update(symbol="ETHUSDT"), "FEE_RATES_FOR_WRONG_INSTRUMENT"),
    (lambda e: e["collateral_rules"].update(collateral_ratio=1.5), "COLLATERAL_RULES_INVALID"),
    (lambda e: e["collateral_rules"].update(coin="USDT"), "COLLATERAL_RULES_INVALID"),
])
def test_contradicting_evidence_is_not_qualified_with_the_reason(mutate, needle):
    e = copy.deepcopy(good())
    mutate(e)
    r = Q.evaluate_eu(e)
    assert r.verdict == Q.NOT_QUALIFIED and any(needle in x for x in r.reasons), r.reasons
    assert r.authorises_orders is False


def test_a_spot_only_eu_account_is_the_documented_adverse_case():
    e = good()
    e["instruments_linear_btcusdt"] = {"retCode": 10001, "status": ""}
    e["key_permissions"]["permissions"] = {"Spot": ["SpotTrade"]}
    r = Q.evaluate_eu(e)
    assert r.verdict == Q.NOT_QUALIFIED and len(r.reasons) >= 2


def test_a_missing_confirmation_alone_leaves_it_unverified_not_qualified():
    e = good()
    del e["support_confirmation"]
    r = Q.evaluate_eu(e)
    assert r.verdict == Q.UNVERIFIED and r.missing == ("support_confirmation",)


@pytest.mark.parametrize("path", [("api_key",), ("key_permissions", "secret"), ("x", "X-BAPI-SIGN"), ("token",)])
def test_secret_shaped_evidence_is_refused_outright(path):
    e = good()
    d = e
    for p in path[:-1]:
        d = d.setdefault(p, {})
    d[path[-1]] = "whatever"
    r = Q.evaluate_eu(e)
    assert r.verdict == Q.NOT_QUALIFIED and r.reasons[0].startswith("EVIDENCE_CONTAINS_SECRET_SHAPED_FIELD")


@pytest.mark.parametrize("bad", [None, [], "x", 5])
def test_non_objects_are_refused(bad):
    assert Q.evaluate_eu(bad).verdict == Q.NOT_QUALIFIED


def test_the_owner_commands_are_read_only_and_name_no_secret():
    for cmd in Q.OWNER_COMMANDS.values():
        low = cmd.lower()
        assert "post" not in low and "order/create" not in low and "withdraw" not in low
    assert all("api.bybit.eu" in c or "ticket" in c.lower() for c in Q.OWNER_COMMANDS.values())


@pytest.mark.parametrize("field", ["fee_rates", "collateral_rules"])
def test_missing_fee_or_collateral_evidence_alone_leaves_it_unverified(field):
    e = good()
    del e[field]
    r = Q.evaluate_eu(e)
    assert r.verdict == Q.UNVERIFIED and r.missing == (field,) and r.authorises_orders is False


@pytest.mark.parametrize("mutate", [
    lambda e: e["collateral_rules"].update(margin_collateral=False),
    lambda e: e["fee_rates"].update(taker_fee_rate=0),
    lambda e: e["fee_rates"].update(taker_fee_rate=0.9),
    lambda e: e["fee_rates"].update(taker_fee_rate=0.055),            # a percent pasted as a fraction
    lambda e: e["fee_rates"].update(maker_fee_rate=-0.9),
    lambda e: e["fee_rates"].update(maker_fee_rate=float("inf")),
    lambda e: e["collateral_rules"].update(collateral_ratio=1e-9),
    lambda e: e["fee_rates"].update(source="x"),
    lambda e: e["collateral_rules"].update(source="trust me"),
    lambda e: e["fee_rates"].update(taker_fee_rate=True),
])
def test_implausible_fee_or_collateral_evidence_is_refused_not_qualified(mutate):
    """Each input is one the independent review reproduced as QUALIFIED."""
    e = good()
    mutate(e)
    r = Q.evaluate_eu(e)
    assert r.verdict == Q.NOT_QUALIFIED and r.authorises_orders is False, r


def test_a_negative_maker_rebate_within_bounds_is_accepted():
    e = good()
    e["fee_rates"]["maker_fee_rate"] = -0.0001
    assert Q.evaluate_eu(e).verdict == Q.QUALIFIED
