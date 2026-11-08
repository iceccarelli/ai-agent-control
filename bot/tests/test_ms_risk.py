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


# ---------------------------------------------------------------- exits are bound by QUANTITY, cumulatively, with a price band (red team rt8 rk1)
SHORT = {"BTCUSDT": {"category": "linear", "notional_usd": -100.0}}                         # 1 unit short at mark 100
SPOT_LONG = {"SPOT:BTCUSDT": {"category": "spot", "notional_usd": 100.0}}


def _exit(qty, ot="Market", price=None, lid="X", side="Buy", ro=True):
    return leg("BTCUSDT", "linear", side, qty, lid, ot, price, ro)


def test_exit_positive_control_one_unit_at_the_mark_is_approved():
    d = R.risk_assess(plan(_exit("1")), state(positions=dict(SHORT)), policy(), snaps("BTCUSDT"))
    assert d.approved and d.limits["exit_only"] is True
    assert R.risk_assess(plan(_exit("1", "Limit", "100")), state(positions=dict(SHORT)), policy(), snaps("BTCUSDT")).approved
    assert R.risk_assess(plan(_exit("0.5", lid="A"), _exit("0.5", lid="B")), state(positions=dict(SHORT)), policy(), snaps("BTCUSDT")).approved


def test_a_cheap_limit_price_cannot_hide_a_huge_quantity():
    # 50 units at a limit of 1.0 is a notional of 50 USD, below the 100 USD held: the old notional check approved it
    d = R.risk_assess(plan(_exit("50", "Limit", "1.0")), state(positions=dict(SHORT)), policy(), snaps("BTCUSDT"))
    assert not d.approved and "REDUCE_EXCEEDS_POSITION:BTCUSDT" in d.reasons and "EXIT_PRICE_OUT_OF_BAND:BTCUSDT" in d.reasons


def test_two_reduce_only_legs_are_judged_together_not_each_against_the_whole_position():
    d = R.risk_assess(plan(_exit("1", lid="A"), _exit("1", lid="B")), state(positions=dict(SHORT)), policy(), snaps("BTCUSDT"))
    assert not d.approved and "REDUCE_EXCEEDS_POSITION:BTCUSDT" in d.reasons                # the plan is refused AS A WHOLE
    d = R.risk_assess(plan(_exit("1.02"), ), state(positions=dict(SHORT)), policy(), snaps("BTCUSDT"))
    assert d.approved                                                                       # the stated 2% slack
    assert not R.risk_assess(plan(_exit("1.03")), state(positions=dict(SHORT)), policy(), snaps("BTCUSDT")).approved


def test_a_stated_position_quantity_is_what_binds_the_exit():
    st = state(positions={"BTCUSDT": {"category": "linear", "notional_usd": -100.0, "qty": 0.5}})        # the venue says 0.5 units
    assert "REDUCE_EXCEEDS_POSITION:BTCUSDT" in R.risk_assess(plan(_exit("1")), st, policy(), snaps("BTCUSDT")).reasons
    assert R.risk_assess(plan(_exit("0.5")), st, policy(), snaps("BTCUSDT")).approved
    for bad in (0.0, -1.0, float("nan"), True, "1"):
        st = state(positions={"BTCUSDT": {"category": "linear", "notional_usd": -100.0, "qty": bad}})
        assert "POSITION_UNKNOWN:BTCUSDT" in R.risk_assess(plan(_exit("0.5")), st, policy(), snaps("BTCUSDT")).reasons


def test_quantity_is_unknown_without_a_mark_never_zero():
    sn = {"BTCUSDT": MarketSnapshot(symbol="BTCUSDT", decision_ms=NOW, mark=None, spot_mark=100.0, book_age_ms=1)}
    assert not R.risk_assess(plan(_exit("1")), state(positions=dict(SHORT)), policy(), sn).approved


@pytest.mark.parametrize("px,ok", [("100", True), ("109", True), ("111", False), ("91", True), ("89", False), ("1.0", False), ("9999", False)])
def test_limit_exit_price_band(px, ok):
    d = R.risk_assess(plan(_exit("1", "Limit", px)), state(positions=dict(SHORT)), policy(), snaps("BTCUSDT"))
    assert d.approved is ok and (ok or "EXIT_PRICE_OUT_OF_BAND:BTCUSDT" in d.reasons)


def test_a_market_exit_without_reduce_only_is_not_an_exit():
    d = R.risk_assess(plan(_exit("1", ro=False)), state(positions=dict(SHORT)), policy(), snaps("BTCUSDT"))
    assert d.limits["exit_only"] is False                                                   # it is judged as an entry (and refused by the freshness/exposure rules or not, but never as an exit)


def test_spot_exit_is_bound_by_quantity_and_band_too():
    st = state(positions=dict(SPOT_LONG))
    sell = lambda q, ot="Market", px=None, lid="S": leg("BTCUSDT", "spot", "Sell", q, lid, ot, px)       # noqa: E731
    assert R.risk_assess(plan(sell("1")), st, policy(), snaps("BTCUSDT")).approved
    assert "REDUCE_EXCEEDS_POSITION:BTCUSDT" in R.risk_assess(plan(sell("1", lid="A"), sell("1", lid="B")), st, policy(), snaps("BTCUSDT")).reasons
    assert "EXIT_PRICE_OUT_OF_BAND:BTCUSDT" in R.risk_assess(plan(sell("1", "Limit", "50")), st, policy(), snaps("BTCUSDT")).reasons


# ---------------------------------------------------------------- red team: spot inventory and key/category consistency
def test_spot_sell_with_a_zero_or_missing_inventory_figure_is_refused():
    p = plan(leg("BTCUSDT", "spot", "Sell", "0.5", "S"))
    for held in (0.0, -50.0):
        st = state(positions={"SPOT:BTCUSDT": {"category": "spot", "notional_usd": held}})
        assert "SPOT_SELL_WITHOUT_INVENTORY:BTCUSDT" in R.risk_assess(p, st, policy(), snaps("BTCUSDT")).reasons
    assert "SPOT_SELL_WITHOUT_INVENTORY:BTCUSDT" in R.risk_assess(p, state(), policy(), snaps("BTCUSDT")).reasons


def test_spot_sell_larger_than_the_inventory_in_a_mixed_plan_is_refused():
    st = state(positions=dict(SPOT_LONG))
    mixed = plan(leg("BTCUSDT", "spot", "Sell", "5", "S"), leg("ETHUSDT", "linear", "Sell", "1", "E"))
    assert "SPOT_SELL_WITHOUT_INVENTORY:BTCUSDT" in R.risk_assess(mixed, st, policy(), snaps("BTCUSDT", "ETHUSDT")).reasons
    ok = plan(leg("BTCUSDT", "spot", "Sell", "0.5", "S"), leg("ETHUSDT", "linear", "Sell", "1", "E"))
    assert "SPOT_SELL_WITHOUT_INVENTORY:BTCUSDT" not in R.risk_assess(ok, st, policy(), snaps("BTCUSDT", "ETHUSDT")).reasons


@pytest.mark.parametrize("key,cat", [("SPOT:BTCUSDT", "linear"), ("BTCUSDT", "spot"), ("SPOT:BTCUSDT#pending:G-L", "linear"), ("BTCUSDT#t", "spot")])
def test_position_key_and_category_must_agree(key, cat):
    st = state(positions={key: {"category": cat, "notional_usd": -100.0}})
    d = ok_decision(st=st)
    assert not d.approved and any(r.startswith("POSITION_KEY_CATEGORY_MISMATCH") for r in d.reasons)
    good = state(positions={"SPOT:ETHUSDT": {"category": "spot", "notional_usd": 100.0}, "ETHUSDT": {"category": "linear", "notional_usd": -100.0}})
    assert ok_decision(st=good).approved                                                    # positive control


# =============================================================================================== portfolio-level bypass attempts (splitting) and HARD loss limits
HELD_PERP = {"category": "linear", "notional_usd": -1000.0}
HELD_SPOT = {"category": "spot", "notional_usd": 1000.0}


def test_splitting_one_symbol_over_two_groups_is_counted_by_the_book():
    pol = policy(max_notional_per_symbol_usd=2500.0, max_aggregate_notional_usd=1e9, max_gross_exposure_usd=1e9)
    first = pair("BTCUSDT", "10")                                                    # 1000 + 1000 = 2000 <= 2500
    assert ok_decision(p=first, pol=pol).approved
    st = state(positions={"BTCUSDT": HELD_PERP, "SPOT:BTCUSDT": HELD_SPOT}, open_groups=1)
    second = pair("BTCUSDT", "10")
    d = ok_decision(p=second, st=st, pol=pol)
    assert not d.approved and "PER_SYMBOL_NOTIONAL_ABOVE_CAP:BTCUSDT" in d.reasons


@pytest.mark.parametrize("held_key", ["BTCUSDT#g1", "BTCUSDT#pending:abc", "BTCUSDT#"])
def test_a_tag_suffix_does_not_hide_the_symbol(held_key):
    pol = policy(max_notional_per_symbol_usd=2500.0, max_aggregate_notional_usd=1e9, max_gross_exposure_usd=1e9)
    st = state(positions={held_key: HELD_PERP, "SPOT:BTCUSDT#t": HELD_SPOT})
    d = ok_decision(p=pair("BTCUSDT", "10"), st=st, pol=pol)
    assert not d.approved and "PER_SYMBOL_NOTIONAL_ABOVE_CAP:BTCUSDT" in d.reasons and d.limits["per_symbol"]["BTCUSDT"] == 4000.0


def test_spot_and_perp_keys_of_one_symbol_share_one_base_and_a_mislabelled_key_is_refused():
    st = state(positions={"SPOT:BTCUSDT": {"category": "linear", "notional_usd": 1000.0}})
    assert "POSITION_KEY_CATEGORY_MISMATCH:SPOT:BTCUSDT" in ok_decision(st=st).reasons
    st = state(positions={"BTCUSDT": {"category": "spot", "notional_usd": 1000.0}})
    assert "POSITION_KEY_CATEGORY_MISMATCH:BTCUSDT" in ok_decision(st=st).reasons


def test_many_symbols_each_under_its_cap_still_hit_the_aggregate_gross_and_net_limits():
    pol = policy(max_aggregate_notional_usd=2500.0, max_gross_exposure_usd=4500.0, max_net_exposure_usd=900.0, max_unhedged_usd=1e9, max_leverage=100.0)
    held = {}
    for s in ("ETHUSDT", "SOLUSDT"):
        held[s] = {"category": "linear", "notional_usd": -800.0}
        held["SPOT:" + s] = {"category": "spot", "notional_usd": 800.0}
    d = R.risk_assess(pair("BTCUSDT", "10"), state(positions=held), pol, snaps("BTCUSDT"))
    assert not d.approved and "AGGREGATE_NOTIONAL_ABOVE_CAP" in d.reasons and not any(r.startswith("PER_SYMBOL") for r in d.reasons)
    pol = policy(max_aggregate_notional_usd=1e9, max_gross_exposure_usd=3500.0, max_net_exposure_usd=1e9, max_unhedged_usd=1e9, max_leverage=100.0)
    assert "GROSS_EXPOSURE_ABOVE_CAP" in R.risk_assess(pair("BTCUSDT", "10"), state(positions=held), pol, snaps("BTCUSDT")).reasons


def test_the_unhedged_limit_cannot_be_split_across_symbols_it_is_the_book_net():
    pol = policy(max_unhedged_usd=1200.0, max_net_exposure_usd=1e9, max_aggregate_notional_usd=1e9, max_gross_exposure_usd=1e9, max_leverage=100.0)
    st = state(positions={"ETHUSDT": {"category": "linear", "notional_usd": -900.0}})
    one_leg = plan(leg("BTCUSDT", "linear", "Sell", "5", "A"))                         # -500 more -> book net -1400
    assert "UNHEDGED_EXPOSURE_ABOVE_CAP" in R.risk_assess(one_leg, st, pol, snaps("BTCUSDT")).reasons


@pytest.mark.parametrize("mutate,code", [
    (lambda s: s.pop("equity_usd"), "EQUITY_UNKNOWN"), (lambda s: s.update(equity_usd=None), "EQUITY_UNKNOWN"), (lambda s: s.update(equity_usd=float("nan")), "EQUITY_UNKNOWN"),
    (lambda s: s.pop("positions"), "POSITIONS_UNKNOWN"), (lambda s: s.update(positions=None), "POSITIONS_UNKNOWN"),
    (lambda s: s.update(positions={"BTCUSDT": {"category": "linear", "notional_usd": None}}), "POSITION_UNKNOWN:BTCUSDT"),
    (lambda s: s.update(positions={"BTCUSDT#x": {"category": "linear", "notional_usd": float("inf")}}), "POSITION_UNKNOWN:BTCUSDT#x"),
    (lambda s: s.pop("open_groups"), "OPEN_GROUPS_UNKNOWN"), (lambda s: s.update(open_groups=-1), "OPEN_GROUPS_UNKNOWN"),
])
def test_a_missing_exposure_or_risk_input_blocks_and_is_never_read_as_zero(mutate, code):
    st = state()
    mutate(st)
    d = ok_decision(st=st)
    assert not d.approved and code in d.reasons
    assert "gross" not in d.limits or code.startswith("OPEN_GROUPS") or code == "EQUITY_UNKNOWN"      # no exposure number is invented from a missing input


# ---- HARD daily-loss and drawdown limits
def hard(**kw):
    return policy(max_daily_loss_usd=200.0, max_drawdown_frac=0.05, **kw)


def hstate(**kw):
    return state(**{"equity_usd": 10000.0, "peak_equity_usd": 10100.0, "day_start_equity_usd": 10100.0, **kw})


def test_hard_limits_positive_control_inside_both_limits_is_approved():
    d = ok_decision(pol=hard(), st=hstate())
    assert d.approved and d.limits["daily_loss_usd"] == 100.0 and round(d.limits["drawdown_frac"], 6) == round(100 / 10100, 6)


def test_policy_schema_accepts_the_optional_keys_only_when_valid_and_stays_closed():
    assert R.policy_refusals(hard()) == [] and R.policy_refusals(policy()) == []
    for k, bad in (("max_daily_loss_usd", 0), ("max_daily_loss_usd", -5.0), ("max_daily_loss_usd", float("nan")), ("max_daily_loss_usd", True), ("max_daily_loss_usd", "200"),
                   ("max_drawdown_frac", 0), ("max_drawdown_frac", 1.5), ("max_drawdown_frac", -0.1), ("max_drawdown_frac", float("inf")), ("max_drawdown_frac", None)):
        assert f"POLICY_LIMIT_INVALID:{k}" in R.policy_refusals(policy(**{k: bad})), (k, bad)
    assert any(r.startswith("POLICY_UNKNOWN_KEYS") for r in R.policy_refusals(policy(max_weekly_loss_usd=1.0)))
    assert set(R.POLICY_KEYS).isdisjoint(R.OPTIONAL_POLICY_KEYS) and set(R.OPTIONAL_POLICY_KEYS) == {"max_daily_loss_usd", "max_drawdown_frac"}


@pytest.mark.parametrize("drop", ["equity_usd", "day_start_equity_usd"])
def test_daily_loss_inputs_missing_fail_closed_for_entries(drop):
    st = hstate()
    st.pop(drop)
    d = ok_decision(pol=policy(max_daily_loss_usd=200.0), st=st)
    assert not d.approved and ("DAILY_LOSS_INPUT_UNKNOWN" in d.reasons or "EQUITY_UNKNOWN" in d.reasons)
    if drop == "day_start_equity_usd":
        assert "DAILY_LOSS_INPUT_UNKNOWN" in d.reasons


@pytest.mark.parametrize("bad", [None, 0, -1.0, float("nan"), True, "10100"])
def test_daily_and_drawdown_inputs_must_be_positive_finite_numbers(bad):
    assert "DAILY_LOSS_INPUT_UNKNOWN" in ok_decision(pol=hard(), st=hstate(day_start_equity_usd=bad)).reasons
    assert "DRAWDOWN_INPUT_UNKNOWN" in ok_decision(pol=hard(), st=hstate(peak_equity_usd=bad)).reasons


def test_daily_loss_breach_blocks_entries_and_boundary_is_a_breach():
    d = ok_decision(pol=hard(), st=hstate(equity_usd=9900.0, peak_equity_usd=9900.0))             # loss exactly 200 = the limit
    assert not d.approved and "DAILY_LOSS_LIMIT_BREACHED" in d.reasons and d.limits["halt_new_exposure"] is True
    assert ok_decision(pol=hard(), st=hstate(equity_usd=9900.01, peak_equity_usd=9900.01)).approved


def test_drawdown_breach_blocks_entries_even_with_the_daily_limit_unset():
    pol = policy(max_drawdown_frac=0.05)
    d = ok_decision(pol=pol, st=state(equity_usd=9400.0, peak_equity_usd=10000.0))                # 6% > 5%
    assert not d.approved and "DRAWDOWN_LIMIT_BREACHED" in d.reasons and d.limits["halt_new_exposure"] is True
    assert ok_decision(pol=pol, st=state(equity_usd=9600.0, peak_equity_usd=10000.0)).approved
    assert "DAILY_LOSS_LIMIT_BREACHED" not in d.reasons and "DAILY_LOSS_INPUT_UNKNOWN" not in d.reasons       # an unset limit needs no input


def test_peak_below_equity_is_an_inconsistent_input_not_a_zero_drawdown():
    d = ok_decision(pol=policy(max_drawdown_frac=0.05), st=state(equity_usd=10000.0, peak_equity_usd=9000.0))
    assert not d.approved and "DRAWDOWN_INPUT_UNKNOWN" in d.reasons


def test_without_the_optional_keys_nothing_changes_and_no_inputs_are_needed():
    assert ok_decision(pol=policy(), st=state()).approved
    assert "halt_new_exposure" not in ok_decision(pol=policy(), st=state()).limits


def test_reduce_only_exits_are_allowed_during_a_breach_and_entries_are_not():
    st = hstate(equity_usd=9000.0, peak_equity_usd=10100.0, positions={"BTCUSDT": {"category": "linear", "notional_usd": -1000.0, "qty": 10.0}})
    exit_ = plan(leg("BTCUSDT", "linear", "Buy", "10", "X", ro=True))
    d = ok_decision(p=exit_, pol=hard(), st=st)
    assert d.approved and d.limits["exit_only"] is True
    entry = pair("ETHUSDT", "1")
    assert not ok_decision(p=entry, pol=hard(), st=st, sn=snaps("ETHUSDT")).approved
    mixed = plan(leg("BTCUSDT", "linear", "Buy", "10", "X", ro=True), leg("ETHUSDT", "linear", "Sell", "1", "Y"))
    dm = ok_decision(p=mixed, pol=hard(), st=st, sn=snaps("BTCUSDT", "ETHUSDT"))
    assert not dm.approved and "DAILY_LOSS_LIMIT_BREACHED" in dm.reasons


def test_missing_hard_limit_inputs_never_block_a_reduce_only_exit():
    st = state(positions={"BTCUSDT": {"category": "linear", "notional_usd": -1000.0, "qty": 10.0}})        # no peak, no day start
    exit_ = plan(leg("BTCUSDT", "linear", "Buy", "10", "X", ro=True))
    assert ok_decision(p=exit_, pol=hard(), st=st).approved
    assert "DAILY_LOSS_INPUT_UNKNOWN" in ok_decision(pol=hard(), st=st).reasons
