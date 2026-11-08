"""ms_risk: closed policy schema, UNKNOWN != zero, exposure ceilings, attack cases. Offline, pure."""
import copy
import os
import sys

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import ms_risk as R  # noqa: E402
from ms_types import BookLevel, LegPlan, MarketSnapshot, OrderGroupPlan  # noqa: E402

NOW = 1_800_000_000_000


def policy(**kw):
    p = {"policy_version": "pv1", "candidate_id": "cand1", "spec_digest": "d" * 64, "venue": "testnet",
         "allowed_symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"], "allowed_sides": {"linear": ["Sell", "Buy"], "spot": ["Buy", "Sell"]},
         "allowed_order_types": ["Limit", "Market"], "max_notional_per_symbol_usd": 2000.0, "max_aggregate_notional_usd": 3000.0,
         "max_gross_exposure_usd": 6000.0, "max_net_exposure_usd": 500.0, "max_active_groups": 3, "max_unhedged_usd": 1200.0, "max_leverage": 3.0,
         "max_market_age_ms": 5000, "max_account_age_ms": 10000, "execution_styles": ["MAKER_FIRST", "TAKER"]}
    p.update(kw)
    return p


def snaps(*syms, mark=100.0, age=100):
    return {s: MarketSnapshot(symbol=s, decision_ms=NOW, mark=mark, spot_mark=mark, book_age_ms=age) for s in syms}


def leg(sym="BTCUSDT", cat="linear", side="Sell", qty="10", lid="L1", ot="Market", price=None, ro=False):
    return LegPlan(leg_id=lid, symbol=sym, category=cat, side=side, qty=qty, order_type=ot, price=price, reduce_only=ro)


def plan(*legs, style="TAKER", **kw):
    d = dict(group_id="G1", candidate_id="cand1", spec_digest="d" * 64, policy_version="pv1", created_ms=NOW, legs=tuple(legs), style=style,
             max_wait_ms=1000, notional_usd=0.0)
    d.update(kw)
    return OrderGroupPlan(**d)


def state(**kw):
    s = {"equity_usd": 10000.0, "positions": {}, "open_groups": 0, "market_ts_ms": NOW - 100, "account_ts_ms": NOW - 100}
    s.update(kw)
    return s


def pair(sym="BTCUSDT", qty="10"):
    return plan(leg(sym, "linear", "Sell", qty, "P"), leg(sym, "spot", "Buy", qty, "S"))


def ok_decision(p=None, st=None, pol=None, sn=None):
    return R.risk_assess(p or pair(), st or state(), pol or policy(), sn or snaps("BTCUSDT"))


def test_positive_control_hedged_pair_is_approved():
    d = ok_decision()
    assert d.approved and d.reasons == () and d.limits["gross"] == 2000.0 and d.limits["net"] == 0.0 and d.limits["aggregate"] == 1000.0


# ---------------------------------------------------------------- policy: closed schema
@pytest.mark.parametrize("key", R.POLICY_KEYS)
def test_policy_missing_key_rejects(key):
    p = policy()
    del p[key]
    d = ok_decision(pol=p)
    assert not d.approved and d.reasons[0].startswith("POLICY_MISSING_KEYS")


def test_policy_unknown_key_rejects():
    d = ok_decision(pol=policy(extra_limit=1))
    assert not d.approved and d.reasons[0].startswith("POLICY_UNKNOWN_KEYS")


@pytest.mark.parametrize("syms", [["*"], ["ALL"], [], "BTCUSDT", ["btcusdt"], ["BTC*"], ["BTCUSDT", "BTCUSDT"], [""], ["BTCUSDT", None]])
def test_wildcard_or_malformed_symbol_list_rejects(syms):
    d = ok_decision(pol=policy(allowed_symbols=syms))
    assert not d.approved and any(r.startswith("POLICY_ALLOWED_SYMBOLS") for r in d.reasons)


@pytest.mark.parametrize("venue", ["mainnet", "eu", "nonsense", "", None, "Testnet", "TESTNET", "testnet "])
def test_non_sandbox_venue_rejects(venue):
    d = ok_decision(pol=policy(venue=venue))
    assert not d.approved and any(r.startswith("POLICY_VENUE") or r == "POLICY_FIELD_INVALID:venue" for r in d.reasons)


@pytest.mark.parametrize("key,bad", [("max_notional_per_symbol_usd", None), ("max_gross_exposure_usd", 0), ("max_net_exposure_usd", float("nan")),
                                     ("max_unhedged_usd", True), ("max_leverage", 0.5), ("max_active_groups", 0), ("max_market_age_ms", 1.5),
                                     ("max_account_age_ms", None), ("max_aggregate_notional_usd", float("inf"))])
def test_unusable_limit_rejects(key, bad):
    d = ok_decision(pol=policy(**{key: bad}))
    assert not d.approved and any(r.startswith("POLICY_LIMIT_INVALID") for r in d.reasons)


def test_unknown_order_type_style_side_in_policy_reject():
    for kw in ({"allowed_order_types": ["Limit", "StopMarket"]}, {"execution_styles": ["YOLO"]}, {"allowed_sides": {"linear": ["Short"]}},
               {"allowed_sides": {"margin": ["Buy"]}}):
        assert not ok_decision(pol=policy(**kw)).approved


# ---------------------------------------------------------------- identity of the plan
@pytest.mark.parametrize("attr", ["candidate_id", "spec_digest", "policy_version"])
def test_plan_identity_must_match_policy(attr):
    d = ok_decision(p=pair().__class__(**{**pair().__dict__, attr: "other"}))
    assert not d.approved and f"PLAN_{attr.upper()}_MISMATCH" in d.reasons


def test_execution_style_not_in_policy_rejects():
    d = ok_decision(pol=policy(execution_styles=["MAKER_FIRST"]))
    assert "EXECUTION_STYLE_NOT_ALLOWED" in d.reasons


# ---------------------------------------------------------------- UNKNOWN is never zero
@pytest.mark.parametrize("key,code", [("equity_usd", "EQUITY_UNKNOWN"), ("positions", "POSITIONS_UNKNOWN"), ("open_groups", "OPEN_GROUPS_UNKNOWN"),
                                      ("market_ts_ms", "MARKET_TS_UNKNOWN"), ("account_ts_ms", "ACCOUNT_TS_UNKNOWN")])
@pytest.mark.parametrize("how", ["missing", "none"])
def test_missing_or_none_state_rejects_with_code(key, code, how):
    st = state()
    if how == "missing":
        del st[key]
    else:
        st[key] = None
    d = ok_decision(st=st)
    assert not d.approved and code in d.reasons


def test_nan_and_zero_equity_reject():
    for e in (float("nan"), 0, -5.0, True):
        assert "EQUITY_UNKNOWN" in ok_decision(st=state(equity_usd=e)).reasons


def test_position_with_unknown_notional_rejects_and_is_not_zero():
    st = state(positions={"ETHUSDT": {"category": "linear", "notional_usd": None}})
    d = ok_decision(st=st)
    assert not d.approved and "POSITION_UNKNOWN:ETHUSDT" in d.reasons


def test_missing_mark_rejects_not_zero_notional():
    sn = {"BTCUSDT": MarketSnapshot(symbol="BTCUSDT", decision_ms=NOW, mark=None, spot_mark=100.0, book_age_ms=1)}
    d = ok_decision(sn=sn)
    assert not d.approved and any(r.startswith("PRICE_UNKNOWN") for r in d.reasons)
    assert any(r.startswith("PRICE_UNKNOWN") for r in R.risk_assess(pair(), state(), policy(), {}).reasons)


def test_spot_leg_is_priced_at_spot_mark_not_perp_mark():
    sn = {"BTCUSDT": MarketSnapshot(symbol="BTCUSDT", decision_ms=NOW, mark=100.0, spot_mark=None, book_age_ms=1)}
    d = ok_decision(sn=sn)
    assert any(r.startswith("PRICE_UNKNOWN") and ":S" in r for r in d.reasons)


# ---------------------------------------------------------------- stale data / stale account (entry)
def test_stale_account_entry_rejected():
    d = ok_decision(st=state(account_ts_ms=NOW - 10_001))
    assert not d.approved and "ACCOUNT_DATA_STALE" in d.reasons
    assert ok_decision(st=state(account_ts_ms=NOW - 10_000)).approved          # boundary: exactly the limit is fresh


def test_stale_market_and_book_rejected():
    assert "MARKET_DATA_STALE" in ok_decision(st=state(market_ts_ms=NOW - 5001)).reasons
    assert "BOOK_STALE:BTCUSDT" in ok_decision(sn=snaps("BTCUSDT", age=5001)).reasons
    assert "BOOK_AGE_UNKNOWN:BTCUSDT" in ok_decision(sn=snaps("BTCUSDT", age=None)).reasons


def test_future_timestamp_rejected():
    assert "ACCOUNT_TS_IN_FUTURE" in ok_decision(st=state(account_ts_ms=NOW + 1)).reasons


def test_exit_is_not_blocked_by_stale_data():
    st = state(positions={"BTCUSDT": {"category": "linear", "notional_usd": -1000.0}}, account_ts_ms=NOW - 999_999, market_ts_ms=None)
    p = plan(leg("BTCUSDT", "linear", "Buy", "10", "X", ro=True))
    d = R.risk_assess(p, st, policy(), snaps("BTCUSDT", age=None))
    assert d.approved and d.limits["exit_only"] is True


def test_reduce_only_without_position_or_same_side_is_not_an_exit():
    p = plan(leg("BTCUSDT", "linear", "Buy", "10", "X", ro=True))
    assert not R.risk_assess(p, state(), policy(), snaps("BTCUSDT")).approved
    st = state(positions={"BTCUSDT": {"category": "linear", "notional_usd": 1000.0}})          # long: a Buy does not reduce it
    assert any(r.startswith("REDUCE_ONLY_NO_MATCHING") for r in R.risk_assess(p, st, policy(), snaps("BTCUSDT")).reasons)


def test_reduce_larger_than_position_rejected():
    st = state(positions={"BTCUSDT": {"category": "linear", "notional_usd": -500.0}})
    p = plan(leg("BTCUSDT", "linear", "Buy", "10", "X", ro=True))                       # 1000 > 500
    assert any(r.startswith("REDUCE_EXCEEDS_POSITION") for r in R.risk_assess(p, st, policy(), snaps("BTCUSDT")).reasons)


def test_exit_on_symbol_outside_allowlist_still_rejected():
    st = state(positions={"DOGEUSDT": {"category": "linear", "notional_usd": -100.0}})
    p = plan(leg("DOGEUSDT", "linear", "Buy", "1", "X", ro=True))
    d = R.risk_assess(p, st, policy(), snaps("DOGEUSDT"))
    assert not d.approved and any(r.startswith("SYMBOL_NOT_ALLOWED") for r in d.reasons)


def test_spot_sell_without_inventory_rejected():
    p = plan(leg("BTCUSDT", "spot", "Sell", "1", "S"))
    assert any(r.startswith("SPOT_SELL_WITHOUT_INVENTORY") for r in R.risk_assess(p, state(), policy(), snaps("BTCUSDT")).reasons)


# ---------------------------------------------------------------- wildcard / allow-list / sides / types
def test_symbol_outside_allowlist_rejected():
    d = R.risk_assess(pair("DOGEUSDT"), state(), policy(), snaps("DOGEUSDT"))
    assert not d.approved and "SYMBOL_NOT_ALLOWED:DOGEUSDT" in d.reasons


def test_side_and_order_type_restrictions():
    pol = policy(allowed_sides={"linear": ["Sell"], "spot": ["Buy"]}, allowed_order_types=["Limit"])
    p = plan(leg("BTCUSDT", "linear", "Buy", "1", "A", ot="Limit", price="100"))
    d = R.risk_assess(p, state(), pol, snaps("BTCUSDT"))
    assert any(r.startswith("SIDE_NOT_ALLOWED") for r in d.reasons)
    d = R.risk_assess(pair(), state(), pol, snaps("BTCUSDT"))
    assert any(r.startswith("ORDER_TYPE_NOT_ALLOWED") for r in d.reasons)


@pytest.mark.parametrize("qty", ["0", "-1", "1e3", "abc", "", "1_0"])
def test_bad_qty_rejected(qty):
    p = plan(leg("BTCUSDT", "linear", "Sell", qty, "A"))
    assert any(r.startswith("QTY_INVALID") for r in R.risk_assess(p, state(), policy(), snaps("BTCUSDT")).reasons)


def test_market_with_price_and_limit_without_price_rejected():
    assert any(r.startswith("MARKET_WITH_PRICE") for r in ok_decision(p=plan(leg(price="1"))).reasons)
    assert any(r.startswith("LIMIT_WITHOUT_PRICE") for r in ok_decision(p=plan(leg(ot="Limit", price=None))).reasons)


def test_limit_sell_priced_at_max_of_limit_and_mark_so_a_cheap_limit_cannot_hide_notional():
    p = plan(leg("BTCUSDT", "linear", "Sell", "100", "A", ot="Limit", price="1"))     # 100 qty @ mark 100 = 10000, not 100
    assert "PER_SYMBOL_NOTIONAL_ABOVE_CAP:BTCUSDT" in ok_decision(p=p).reasons


# ---------------------------------------------------------------- ceilings
def test_per_symbol_ceiling():
    d = ok_decision(p=pair(qty="11"))                                  # 1100 + 1100 = 2200 > 2000
    assert "PER_SYMBOL_NOTIONAL_ABOVE_CAP:BTCUSDT" in d.reasons


def test_aggregate_ceiling_with_every_symbol_under_its_own_cap():
    pol = policy(max_aggregate_notional_usd=1500.0, max_gross_exposure_usd=1e9, max_net_exposure_usd=1e9, max_unhedged_usd=1e9, max_leverage=100.0)
    st = state(positions={"ETHUSDT": {"category": "linear", "notional_usd": -600.0}, "SPOT:ETHUSDT": {"category": "spot", "notional_usd": 600.0}})
    d = R.risk_assess(pair("BTCUSDT", "10"), st, pol, {**snaps("BTCUSDT")})
    assert not d.approved and "AGGREGATE_NOTIONAL_ABOVE_CAP" in d.reasons
    assert not any(r.startswith("PER_SYMBOL") for r in d.reasons)         # the symbol cap alone would have let it through


def test_cross_symbol_exposure_bypass_is_counted():
    """Exposure parked in symbols outside the allow-list (or other symbols) still counts toward gross / net."""
    pol = policy(max_gross_exposure_usd=2500.0)
    st = state(positions={"DOGEUSDT": {"category": "linear", "notional_usd": -900.0}})
    d = ok_decision(pol=pol, st=st)
    assert not d.approved and "GROSS_EXPOSURE_ABOVE_CAP" in d.reasons
    pol = policy(max_net_exposure_usd=500.0)
    st = state(positions={"DOGEUSDT": {"category": "linear", "notional_usd": 600.0}})
    assert "NET_EXPOSURE_ABOVE_CAP" in ok_decision(pol=pol, st=st).reasons


def test_net_ceiling_for_an_unhedged_single_leg():
    p = plan(leg("BTCUSDT", "linear", "Sell", "6", "A"))               # net -600 > 500
    assert "NET_EXPOSURE_ABOVE_CAP" in ok_decision(p=p).reasons


def test_paired_order_failure_exposure_is_bounded_by_max_unhedged():
    """If only ONE leg of a pair fills the book sits at that leg's notional: a pair whose leg exceeds max_unhedged is refused although it nets to zero."""
    pol = policy(max_unhedged_usd=900.0)
    d = ok_decision(pol=pol)                                           # each leg 1000 > 900
    assert not d.approved and "UNHEDGED_EXPOSURE_ABOVE_CAP" in d.reasons and d.limits["net"] == 0.0
    assert ok_decision(pol=policy(max_unhedged_usd=1000.0)).approved


def test_partial_hedge_net_exposure_rejected():
    p = plan(leg("BTCUSDT", "linear", "Sell", "10", "P"), leg("BTCUSDT", "spot", "Buy", "4", "S"))        # hedge covers 40%
    d = ok_decision(p=p, pol=policy(max_net_exposure_usd=500.0))
    assert not d.approved and "NET_EXPOSURE_ABOVE_CAP" in d.reasons


def test_leverage_ceiling():
    d = ok_decision(st=state(equity_usd=500.0))                        # gross 2000 / 500 = 4 > 3
    assert "LEVERAGE_ABOVE_CAP" in d.reasons


def test_max_active_groups():
    assert "MAX_ACTIVE_GROUPS_REACHED" in ok_decision(st=state(open_groups=3)).reasons
    assert ok_decision(st=state(open_groups=2)).approved


def test_spot_and_perp_with_same_symbol_key_do_not_overwrite_each_other():
    st = state(positions={"BTCUSDT": {"category": "linear", "notional_usd": -400.0}, "SPOT:BTCUSDT": {"category": "spot", "notional_usd": 400.0}})
    d = ok_decision(st=st, pol=policy(max_notional_per_symbol_usd=1700.0))       # 400+400 existing + 1000+1000 = 2800 > 1700
    assert "PER_SYMBOL_NOTIONAL_ABOVE_CAP:BTCUSDT" in d.reasons
    assert d.limits["gross"] == pytest.approx(2800.0)


def test_all_reasons_are_reported_not_just_the_first():
    d = ok_decision(st=state(account_ts_ms=NOW - 99999, open_groups=9, equity_usd=100.0))
    assert {"ACCOUNT_DATA_STALE", "MAX_ACTIVE_GROUPS_REACHED", "LEVERAGE_ABOVE_CAP"} <= set(d.reasons)


def test_decision_is_immutable_and_does_not_mutate_inputs():
    st, pol = state(), policy()
    before = copy.deepcopy((st, pol))
    d = ok_decision(st=st, pol=pol)
    assert (st, pol) == before
    with pytest.raises(Exception):
        d.approved = False                                              # type: ignore[misc]
