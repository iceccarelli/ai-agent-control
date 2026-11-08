"""ms_portfolio: constrained deterministic allocation; missing inputs BLOCK; sizes never exceed measured depth/capacity; ids are idempotent. Synthetic inputs only."""
from __future__ import annotations

import os
import sys
from dataclasses import replace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (BOT, os.path.join(BOT, "tools")):
    sys.path.insert(0, _p)

import ms_portfolio as PF  # noqa: E402
import ms_types as T  # noqa: E402

NOW = 10_000_000
NF = NOW + 3_600_000


def inst(sym="AAAUSDT", base="AAA", **kw):
    d = dict(symbol=sym, symbol_id=None, status="Trading", launch_ms=0, delivery_ms=None, funding_interval_min=480, base_coin=base, tick_size="0.1", qty_step="0.1",
             min_order_qty="0.1", max_order_qty="10000", max_mkt_order_qty="5000", min_notional_usd="5", max_leverage="10", spot_symbol=sym, spot_hedge="HEDGEABLE_ON_BYBIT")
    d.update(kw)
    return T.Instrument(**d)


def snap(sym="AAAUSDT", mark=100.0, depth_qty=1000.0, **kw):
    b = 100.0 if mark is None else mark
    d = dict(symbol=sym, decision_ms=NOW, mark=mark, index=mark, bids=(T.BookLevel(b - 0.05, depth_qty),), asks=(T.BookLevel(b + 0.07, depth_qty),), book_age_ms=100,
             funding_interval_min=480, next_funding_ms=NF, spot_mark=mark)
    d.update(kw)
    return T.MarketSnapshot(**d)


def opp(cid="c1", sym="AAAUSDT", rank=1.0, cap=1e6, **kw):
    d = dict(candidate_id=cid, spec_digest="spec1", kind="SPOT_PERP_CARRY", decision_ms=NOW, legs=((sym, "Sell"), ("SPOT:" + sym, "Buy")), hold_days=3.0,
             expected_funding_bps_horizon=30.0, expected_price_leg_bps=0.0, costs=(), net_bps_horizon=15.0, capacity_usd=cap, risk_score=1.0, rank_score=rank, prov="MEASURED")
    d.update(kw)
    return T.Opportunity(**d)


def state(**kw):
    d = dict(equity_usd=100_000.0, cash_usd=100_000.0, peak_equity_usd=100_000.0, positions={}, open_groups=0, margin_used_usd=0.0, market_ts_ms=NOW - 1000, account_ts_ms=NOW - 1000)
    d.update(kw)
    return PF.PortfolioState(**d)


def world(syms=("AAAUSDT",), **snapkw):
    ins = {s: inst(s, s[:-4]) for s in syms}
    sn = {s: snap(s, **snapkw) for s in syms}
    sp = {s: inst(s, s[:-4]) for s in syms}
    depth = {s: 1e9 for s in syms}
    return ins, sn, sp, depth


def run(opps, st=None, pol=None, w=None, **kw):
    ins, sn, sp, depth = w or world()
    kw.setdefault("spot_instruments", sp)
    kw.setdefault("spot_depth_usd", depth)
    return PF.allocate(opps, state=st or state(), policy=pol or PF.PortfolioPolicy(), instruments=ins, snapshots=sn, decision_ms=NOW, **kw)


def test_positive_control_a_carry_is_allocated_with_a_complete_step_and_tick_rounded_plan():
    a = run([opp()])
    assert len(a.plans) == 1 and a.decisions[0].action == "ALLOCATE" and a.decisions[0].binding
    p = a.plans[0]
    perp = next(l for l in p.legs if l.category == "linear")
    spot = next(l for l in p.legs if l.category == "spot")
    assert perp.side == "Sell" and spot.side == "Buy" and perp.qty == spot.qty and p.style == "MAKER_FIRST"
    assert perp.post_only and perp.order_type == "Limit" and not perp.reduce_only
    assert perp.price == "100.1"                     # best ask 100.07 rounded UP to the 0.1 tick (a passive sell never rounds into the book)
    assert spot.price == "100"                       # spot mark rounded DOWN
    assert (float(perp.qty) * 10) == pytest.approx(round(float(perp.qty) * 10))      # multiple of the 0.1 step
    assert p.notional_usd == pytest.approx(2 * float(perp.qty) * 100.0)


def test_output_is_deterministic_independent_of_input_order_and_ranked_by_rank_score():
    ins, sn, sp, depth = world(("AAAUSDT", "BBBUSDT"))
    o1, o2 = opp("c1", "AAAUSDT", rank=1.0), opp("c2", "BBBUSDT", rank=5.0)
    a = run([o1, o2], w=(ins, sn, sp, depth))
    b = run([o2, o1], w=(ins, sn, sp, depth))
    assert a == b and [d.candidate_id for d in a.decisions] == ["c2", "c1"]
    assert run([o1, o2], w=(ins, sn, sp, depth)) == a


def test_ids_are_idempotent_and_change_with_any_input():
    g = PF.derive_group_id("c", "s", 5, ["A"])
    assert g == PF.derive_group_id("c", "s", 5, ["A"]) and len({g, PF.derive_group_id("c2", "s", 5, ["A"]), PF.derive_group_id("c", "s2", 5, ["A"]),
                                                                PF.derive_group_id("c", "s", 6, ["A"]), PF.derive_group_id("c", "s", 5, ["B"])}) == 5
    l = PF.derive_leg_id(g, "A", "linear", "Sell", 0)
    assert l == PF.derive_leg_id(g, "A", "linear", "Sell", 0) and l != PF.derive_leg_id(g, "A", "spot", "Sell", 0) and len(g) + len(l) + 1 <= 36
    p1, p2 = run([opp()]).plans[0], run([opp()]).plans[0]
    assert p1.group_id == p2.group_id and [x.leg_id for x in p1.legs] == [x.leg_id for x in p2.legs]


@pytest.mark.parametrize("what", ["equity", "cash", "peak", "positions", "open_groups", "margin", "market_ts", "account_ts"])
def test_a_missing_state_input_blocks_everything_never_zero(what):
    field = {"equity": "equity_usd", "cash": "cash_usd", "peak": "peak_equity_usd", "positions": "positions", "open_groups": "open_groups", "margin": "margin_used_usd",
             "market_ts": "market_ts_ms", "account_ts": "account_ts_ms"}[what]
    a = run([opp()], st=state(**{field: None}))
    assert a.plans == () and a.decisions[0].action == "SKIP" and any("MISSING_" in r for r in a.decisions[0].reasons)


@pytest.mark.parametrize("hole", ["mark", "book", "book_age", "interval", "next_funding", "spot_mark", "tick", "step", "min_qty", "max_qty", "min_notional"])
def test_a_missing_market_or_filter_input_blocks_the_candidate(hole):
    ins, sn, sp, depth = world()
    if hole == "mark":
        sn["AAAUSDT"] = snap(mark=None)
    elif hole == "book":
        sn["AAAUSDT"] = snap(bids=())
    elif hole == "book_age":
        sn["AAAUSDT"] = snap(book_age_ms=None)
    elif hole == "interval":
        sn["AAAUSDT"] = snap(funding_interval_min=None)
    elif hole == "next_funding":
        sn["AAAUSDT"] = snap(next_funding_ms=None)
    elif hole == "spot_mark":
        sn["AAAUSDT"] = snap(spot_mark=None)
    else:
        key = {"tick": "tick_size", "step": "qty_step", "min_qty": "min_order_qty", "max_qty": "max_order_qty", "min_notional": "min_notional_usd"}[hole]
        sp["AAAUSDT"] = inst(**{key: None})
    a = run([opp()], w=(ins, sn, sp, depth))
    assert a.plans == () and a.decisions[0].action == "SKIP" and a.decisions[0].reasons


def test_missing_spot_depth_filters_capacity_or_instrument_block():
    ins, sn, sp, depth = world()
    assert run([opp()], w=(ins, sn, sp, {})).plans == ()
    assert run([opp()], w=(ins, sn, {}, depth)).plans == ()
    assert run([opp(cap=None)]).decisions[0].reasons == ("MISSING_CAPACITY",)
    assert run([opp()], w=({}, sn, sp, depth)).plans == ()
    assert run([opp()], w=(ins, {}, sp, depth)).plans == ()


def test_size_never_exceeds_the_depth_fraction_of_the_measured_book_side():
    w = world(depth_qty=20.0)                                    # 20 x ~100 = ~2000 USD per side
    a = run([opp()], w=w)
    perp_notional = float(a.plans[0].legs[0].qty) * 100.0
    assert perp_notional <= 0.10 * 20.0 * 99.95 + 1e-6 and a.decisions[0].binding.startswith("DEPTH")


def test_size_never_exceeds_the_capacity_fraction():
    a = run([opp(cap=5_000.0)])
    assert float(a.plans[0].legs[0].qty) * 100.0 <= 500.0 + 1e-6 and a.decisions[0].binding == "CAPACITY"


def test_exposure_limits_bind_gross_net_symbol_and_leverage():
    pol = PF.PortfolioPolicy(max_symbol_frac=0.05, capacity_fraction=1.0, depth_fraction=1.0)
    a = run([opp()], pol=pol)
    assert float(a.plans[0].legs[0].qty) * 100.0 <= 0.05 * 100_000 + 1e-6 and "SYMBOL" in a.decisions[0].binding
    pol = PF.PortfolioPolicy(max_gross_frac=0.04, max_symbol_frac=1, max_symbol_leverage=1, max_symbol_notional_usd=1e9, capacity_fraction=1.0, depth_fraction=1.0)
    a = run([opp()], pol=pol)
    assert a.plans[0].notional_usd <= 0.04 * 100_000 + 1e-6 and a.decisions[0].binding == "GROSS"
    pol = PF.PortfolioPolicy(max_aggregate_leverage=0.02, max_gross_frac=9, max_symbol_frac=1, max_symbol_leverage=1, max_symbol_notional_usd=1e9, capacity_fraction=1.0, depth_fraction=1.0)
    assert run([opp()], pol=pol).decisions[0].binding == "AGG_LEVERAGE"
    # a one-legged directional position is bounded by the NET limit
    d = opp(kind="PERP_DIRECTIONAL_FUNDING", legs=(("AAAUSDT", "Sell"),))
    pol = PF.PortfolioPolicy(max_net_frac=0.01, capacity_fraction=1.0, depth_fraction=1.0, max_symbol_frac=1, max_symbol_leverage=1, max_symbol_notional_usd=1e9)
    a = run([d], pol=pol)
    assert a.decisions[0].binding == "NET" and float(a.plans[0].legs[0].qty) * 100 <= 1000 + 1e-6


def test_existing_exposure_consumes_room_and_max_groups_and_one_group_per_symbol():
    held = {"AAAUSDT": PF.Position("AAAUSDT", "linear", -10_000.0, NF, "AAA")}
    a = run([opp()], st=state(positions=held, open_groups=1))
    assert a.plans == () and a.decisions[0].reasons == ("SYMBOL_ALREADY_HELD",)
    assert run([opp()], st=state(open_groups=5)).decisions[0].reasons == ("MAX_GROUPS",)
    ins, sn, sp, depth = world(("AAAUSDT", "BBBUSDT"))
    pol = PF.PortfolioPolicy(max_gross_frac=0.10, capacity_fraction=1.0, depth_fraction=1.0, max_symbol_frac=1, max_symbol_leverage=1, max_symbol_notional_usd=1e9, max_underlying_frac=1, max_cluster_frac=1)
    heavy = {"BBBUSDT": PF.Position("BBBUSDT", "linear", -9_990.0, NF, "BBB"), "SPOT:BBBUSDT": PF.Position("SPOT:BBBUSDT", "spot", 0.0, None, "BBB")}
    a = run([opp()], st=state(positions=heavy, open_groups=1), pol=pol, w=(ins, sn, sp, depth))
    assert a.plans == () and a.decisions[0].reasons[0].startswith("BELOW_MIN") and a.decisions[0].binding == "GROSS"


def test_second_allocation_sees_the_state_the_first_created():
    ins, sn, sp, depth = world(("AAAUSDT", "BBBUSDT"))
    pol = PF.PortfolioPolicy(max_gross_frac=0.30, capacity_fraction=1.0, depth_fraction=1.0, max_symbol_frac=1, max_symbol_leverage=1, max_symbol_notional_usd=1e9,
                             max_underlying_frac=1, max_cluster_frac=1, max_funding_bucket_frac=9)
    a = run([opp("c1", "AAAUSDT", rank=2, cap=10_000.0), opp("c2", "BBBUSDT", rank=1)], pol=pol, w=(ins, sn, sp, depth))
    assert len(a.plans) == 2 and a.state.open_groups == 2 and a.metrics["gross_usd"] <= 0.30 * 100_000 + 1e-6
    assert a.state.cash_usd < 100_000 and a.state.margin_used_usd > 0
    assert a.decisions[1].constraints["GROSS"] < a.decisions[0].constraints["GROSS"]


def test_cash_buffer_and_margin_utilisation_bind():
    pol = PF.PortfolioPolicy(capacity_fraction=1.0, depth_fraction=1.0, max_symbol_frac=1, max_symbol_leverage=1, max_symbol_notional_usd=1e9, max_gross_frac=9, max_aggregate_leverage=9,
                             max_underlying_frac=9, max_cluster_frac=9, max_stable_frac=9, max_funding_bucket_frac=9, max_net_frac=1)
    a = run([opp()], st=state(cash_usd=21_000.0), pol=pol)            # 20k buffer => 1k deployable
    assert a.decisions[0].binding == "CASH_BUFFER" and a.decisions[0].constraints["CAPITAL_REQUIRED"] <= 1_000.0 + 1e-6
    assert run([opp()], st=state(cash_usd=20_000.0), pol=pol).plans == ()
    a = run([opp()], st=state(margin_used_usd=59_990.0), pol=replace(pol, max_margin_util=0.6))
    assert a.decisions[0].binding == "MARGIN_UTIL" or a.plans == ()


def test_liquidation_distance_lowers_leverage_and_is_reported():
    pol = PF.PortfolioPolicy(perp_leverage=20.0, min_liq_distance=0.20, maint_margin_frac=0.02)
    a = run([opp()], pol=pol)
    liq = [v for k, v in a.decisions[0].constraints.items() if k.startswith("LIQ_DISTANCE")]
    assert liq and min(liq) >= 0.20 - 1e-9


def test_concentration_underlying_cluster_stable_and_funding_bucket_limits():
    ins, sn, sp, depth = world(("AAAUSDT", "BBBUSDT"))
    base = dict(capacity_fraction=1.0, depth_fraction=1.0, max_symbol_frac=1, max_symbol_leverage=1, max_symbol_notional_usd=1e9, max_gross_frac=9, max_aggregate_leverage=9, max_net_frac=1)
    held = {"BBBUSDT": PF.Position("BBBUSDT", "linear", -30_000.0, NF, "BBB")}
    # same cluster: AAA and BBB in one correlation group capped at 35% of equity => only 5k left
    pol = PF.PortfolioPolicy(**base, max_cluster_frac=0.35, correlation_groups={"AAA": "ALTS", "BBB": "ALTS"}, max_underlying_frac=9, max_stable_frac=9, max_funding_bucket_frac=9)
    a = run([opp()], st=state(positions=held, open_groups=1), pol=pol, w=(ins, sn, sp, depth))
    assert a.decisions[0].binding == "CLUSTER:ALTS" and a.plans[0].notional_usd / 2 <= 5_000 + 1e-6
    pol = PF.PortfolioPolicy(**base, max_stable_frac=0.31, max_cluster_frac=9, max_underlying_frac=9, max_funding_bucket_frac=9)
    assert run([opp()], st=state(positions=held, open_groups=1), pol=pol, w=(ins, sn, sp, depth)).decisions[0].binding == "STABLE:USDT"
    pol = PF.PortfolioPolicy(**base, max_funding_bucket_frac=0.32, max_cluster_frac=9, max_underlying_frac=9, max_stable_frac=9)
    assert run([opp()], st=state(positions=held, open_groups=1), pol=pol, w=(ins, sn, sp, depth)).decisions[0].binding == f"FUNDING_BUCKET:{NF}"
    pol = PF.PortfolioPolicy(**base, max_underlying_frac=0.0001, max_cluster_frac=9, max_stable_frac=9, max_funding_bucket_frac=9)
    assert run([opp()], pol=pol).plans == ()


def test_position_with_unknown_funding_time_counts_against_every_bucket():
    held = {"BBBUSDT": PF.Position("BBBUSDT", "linear", -30_000.0, None, "BBB")}
    pol = PF.PortfolioPolicy(capacity_fraction=1.0, depth_fraction=1.0, max_symbol_frac=1, max_symbol_leverage=1, max_symbol_notional_usd=1e9, max_gross_frac=9,
                             max_aggregate_leverage=9, max_net_frac=1, max_funding_bucket_frac=0.32, max_cluster_frac=9, max_underlying_frac=9, max_stable_frac=9)
    assert run([opp()], st=state(positions=held, open_groups=1), pol=pol).decisions[0].binding.startswith("FUNDING_BUCKET")


def test_drawdown_stale_account_and_stale_market_halt_new_exposure():
    assert run([opp()], st=state(equity_usd=89_000.0)).decisions[0].reasons == ("BLOCK_DRAWDOWN_HALT",)
    assert run([opp()], st=state(account_ts_ms=NOW - 10 * 60_000)).decisions[0].reasons == ("BLOCK_ACCOUNT_TS_STALE",)
    assert run([opp()], st=state(market_ts_ms=NOW - 10 * 60_000)).decisions[0].reasons == ("BLOCK_MARKET_TS_STALE",)
    assert run([opp()], st=state(account_ts_ms=NOW + 5)).decisions[0].reasons == ("BLOCK_ACCOUNT_TS_FROM_THE_FUTURE",)
    assert run([opp()], st=state(equity_usd=0.0)).plans == ()


def test_min_notional_min_qty_and_max_qty_are_enforced():
    ins, sn, sp, depth = world()
    sp["AAAUSDT"] = inst(min_notional_usd="1000000")
    assert run([opp()], w=(ins, sn, sp, depth)).decisions[0].reasons[0].startswith("BELOW_MIN_NOTIONAL")
    sp["AAAUSDT"] = inst(min_order_qty="1000000")
    assert run([opp()], w=(ins, sn, sp, depth)).decisions[0].reasons[0].startswith("BELOW_MIN_QTY")
    ins2 = {"AAAUSDT": inst(max_order_qty="3", max_mkt_order_qty="3")}
    sp2 = {"AAAUSDT": inst(max_order_qty="3", max_mkt_order_qty="3")}
    a = run([opp()], w=(ins2, sn, sp2, depth))
    assert float(a.plans[0].legs[0].qty) == 3.0 and "QTY_CLAMPED_MAX" in a.decisions[0].reasons


def test_not_tradable_unhedgeable_excluded_unrankable_and_net_below_min_are_skipped_with_codes():
    ins, sn, sp, depth = world()
    ins["AAAUSDT"] = inst(status="Closed")
    assert run([opp()], w=(ins, sn, sp, depth)).decisions[0].reasons == ("NOT_TRADABLE:AAAUSDT",)
    ins["AAAUSDT"] = inst(spot_hedge="PERP_ONLY")
    assert run([opp()], w=(ins, sn, sp, depth)).decisions[0].reasons == ("NOT_HEDGEABLE:AAAUSDT",)
    assert run([opp(exclusions=("STALE_FUNDING",))]).decisions[0].reasons == ("EXCLUDED:STALE_FUNDING",)
    assert run([opp(rank=None)]).decisions[0].reasons == ("UNRANKABLE",)
    assert run([opp(net_bps_horizon=None)]).decisions[0].reasons == ("NET_UNKNOWN",)
    assert run([opp(net_bps_horizon=-1.0)]).decisions[0].reasons == ("NET_BELOW_MIN",)
    assert run([opp(legs=())]).decisions[0].reasons == ("NO_LEGS",)


def test_taker_style_builds_market_legs_without_prices():
    a = run([opp()], pol=PF.PortfolioPolicy(style="TAKER"))
    assert all(l.order_type == "Market" and l.price is None and not l.post_only for l in a.plans[0].legs) and a.plans[0].style == "TAKER"


def test_risk_is_consulted_with_the_contract_shape_and_a_rejection_blocks_without_changing_state():
    calls = []

    class D:
        def __init__(self, ok, why=()):
            self.approved, self.reasons, self.limits = ok, tuple(why), {}

    def risk(plan, st, pol, snaps):
        calls.append((plan, st, pol, snaps))
        return D(False, ["LIQ"])

    a = run([opp()], risk_assess=risk, risk_policy={"x": 1})
    assert a.plans == () and a.decisions[0].reasons[:2] == ("RISK_REJECTED", "LIQ") and a.state.open_groups == 0 and a.state.cash_usd == 100_000.0
    plan, st, pol, snaps = calls[0]
    assert set(st) == {"equity_usd", "positions", "open_groups", "market_ts_ms", "account_ts_ms", "peak_equity_usd"} and pol == {"x": 1} and "AAAUSDT" in snaps
    assert len(run([opp()], risk_assess=lambda *a: D(True)).plans) == 1


def test_metrics_report_gross_net_leverage_and_drawdown():
    held = {"AAAUSDT": PF.Position("AAAUSDT", "linear", -10_000.0, NF, "AAA"), "SPOT:AAAUSDT": PF.Position("SPOT:AAAUSDT", "spot", 10_000.0, None, "AAA")}
    m = PF.exposure_metrics(state(positions=held, equity_usd=95_000.0), PF.PortfolioPolicy(), {})
    assert m["gross_usd"] == 20_000 and m["net_usd"] == 0 and m["aggregate_leverage"] == pytest.approx(20_000 / 95_000) and m["drawdown_frac"] == pytest.approx(0.05)
    assert m["underlying_usd"]["AAA"] == 20_000 and m["funding_bucket_usd"][str(NF)] == 10_000
