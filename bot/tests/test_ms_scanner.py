"""ms_scanner: pure opportunity scanner. Hand-computed economics, every exclusion code, interval normalisation, depth-walk capacity, determinism, no I/O, and mutation checks."""
from __future__ import annotations

import os
import random
import sys
import time
import types
import uuid

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ms_scanner as S  # noqa: E402
import ms_types as T  # noqa: E402
from ms_types import BookLevel, Instrument, MarketSnapshot  # noqa: E402

NOW = 1_800_000_000_000
DEEP = 1_000_000.0


def inst(sym="AAAUSDT", **kw):
    d = dict(symbol=sym, symbol_id=None, status="Trading", launch_ms=NOW - 400 * T.MS_PER_DAY, delivery_ms=None, funding_interval_min=480, base_coin=sym[:-4], tick_size="0.01",
             qty_step="0.001", min_order_qty="0.001", max_order_qty="1000000", max_mkt_order_qty="1000000", min_notional_usd="5", max_leverage="50", spot_symbol=sym[:-4] + "USDT",
             spot_hedge="HEDGEABLE_ON_BYBIT")
    d.update(kw)
    return Instrument(**d)


def snap(sym="AAAUSDT", *, mark=100.0, spot=99.98, bid=99.995, ask=100.005, bid_qty=DEEP, ask_qty=DEEP, fund=(4.0,) * 9, iv=480, age=100, decision_ms=NOW, oi=None, **kw):
    d = dict(symbol=sym, decision_ms=decision_ms, mark=mark, index=mark, bids=(BookLevel(bid, bid_qty),), asks=(BookLevel(ask, ask_qty),), book_age_ms=age,
             last_funding_bps_per_print=fund[-1] if fund else None, trailing_funding_bps_per_print=tuple(fund), funding_interval_min=iv, next_funding_ms=NOW + 3_600_000,
             open_interest_usd=oi, turnover24h_usd=1e9, spot_mark=spot)
    d.update(kw)
    return MarketSnapshot(**d)


def run(i=None, s=None, *, kind="SPOT_PERP_CARRY", style="TAKER", params=None, decision_ms=NOW):
    i = i if i is not None else [inst()]
    s = s if s is not None else [snap()]
    return S.scan(i, s, spec={"kind": kind, "style": style}, candidate_id="c@1", spec_digest="d" * 8, decision_ms=decision_ms, params=params)


def cost(o, name):
    return next(c for c in o.costs if c.name == name)


# ------------------------------------------------------------------ golden economics (hand computed)
def test_carry_golden_numbers_every_cost_has_a_provenance_and_net_is_hand_computed():
    o = run()[0]
    assert o.exclusions == () and o.kind == "SPOT_PERP_CARRY" and o.legs == (("SPOT:AAAUSDT", "Buy"), ("AAAUSDT", "Sell"))
    assert o.expected_funding_bps_horizon == pytest.approx(4.0 * 3 * 7)                       # 4 bps / 8h print = 12 bps/day x 7 days
    assert (cost(o, "entry_fee").bps, cost(o, "entry_fee").prov) == (pytest.approx(15.5), "VENUE_STATED")           # spot taker 10 + perp taker 5.5
    assert (cost(o, "exit_fee").bps, cost(o, "exit_fee").prov) == (pytest.approx(15.5), "VENUE_STATED")
    assert (cost(o, "spread").bps, cost(o, "spread").prov) == (pytest.approx(1.0), "MEASURED")                       # 0.01 / 100 = 1.0 bps > ASSUMED 0.4
    assert (cost(o, "hedge").bps, cost(o, "hedge").prov) == (pytest.approx(2.0), "ASSUMED")
    assert cost(o, "financing").bps == 0.0 and cost(o, "financing").prov == "ASSUMED"
    assert cost(o, "slippage").bps == pytest.approx(0.0) and cost(o, "slippage").prov == "DERIVED"
    assert o.net_bps_horizon == pytest.approx(84.0 - 31.0 - 1.0 - 2.0)
    basis = (100.0 - 99.98) / 99.98 * 1e4
    assert o.risk_score == pytest.approx(basis + 2.0)                                           # zero funding sigma (flat prints) + |basis| + ASSUMED floor
    assert o.rank_score == pytest.approx((o.net_bps_horizon / 1.2) / o.risk_score)
    assert o.prov == "ASSUMED"                                                                   # weakest input, never better than the assumed hedge slippage / spread


def test_the_assumed_carry_costs_spread_is_the_floor_when_the_book_is_tighter():
    o = run(s=[snap(bid=99.9990, ask=100.0010)])[0]                                              # 0.2 bps measured < 0.4 assumed
    assert (cost(o, "spread").bps, cost(o, "spread").prov) == (pytest.approx(0.4), "ASSUMED")


def test_fee_constants_are_carry_costs_names():
    import carry_costs as CC
    o = run()[0]
    assert cost(o, "entry_fee").bps + cost(o, "exit_fee").bps == pytest.approx(CC.ROUND_TRIP_FEE_BPS_SPOT_PERP) == 31.0
    d = run(kind="PERP_DIRECTIONAL_FUNDING", params={"price_vol_bps_per_day": {"AAAUSDT": 100.0}})[0]
    assert cost(d, "entry_fee").bps + cost(d, "exit_fee").bps == pytest.approx(CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY) == 11.0
    assert not any(c.name in ("hedge", "financing") for c in d.costs)                              # a perp-only book has no spot leg


def test_maker_first_is_modelled_and_the_spot_leg_stays_taker():
    o = run(style="MAKER_FIRST")[0]
    assert (cost(o, "entry_fee").bps, cost(o, "entry_fee").prov) == (pytest.approx(10.0 + 2.0), "MODELLED")          # spot taker + perp MODELLED maker
    assert cost(o, "exit_fee").bps == pytest.approx(15.5)
    assert (cost(o, "adverse_selection").bps, cost(o, "adverse_selection").prov) == (pytest.approx(0.5), "MODELLED")  # half the measured 1.0 bps spread
    assert cost(o, "missed_fill").bps == pytest.approx(0.25 * ((5.5 - 2.0) + 0.5)) and cost(o, "missed_fill").prov == "MODELLED"
    t = run(style="TAKER")[0]
    assert not any(c.name in ("adverse_selection", "missed_fill") for c in t.costs)
    assert any("MODELLED" in r for r in o.reasons)


def test_net_not_gross_decides_the_rank():
    # X has twice the gross funding but a thin book (slippage eats the edge); Y is modest and deep
    thin_x = MarketSnapshot("XXXUSDT", NOW, 100.0, 100.0, (BookLevel(99.995, 5.0), BookLevel(95.0, 3000.0)), (BookLevel(100.005, 5.0), BookLevel(105.0, 3000.0)), 100, 24.0, (24.0,) * 9, 480,
                            NOW + 3_600_000, None, 1e9, 99.98)
    rows = run([inst("XXXUSDT"), inst("YYYUSDT")], [thin_x, snap("YYYUSDT", fund=(6.0,) * 9)])
    by = {o.legs[1][0]: o for o in rows}
    assert by["XXXUSDT"].expected_funding_bps_horizon > by["YYYUSDT"].expected_funding_bps_horizon
    assert by["XXXUSDT"].capacity_usd < by["YYYUSDT"].capacity_usd
    assert rows[0].legs[1][0] == "YYYUSDT" and (by["XXXUSDT"].rank_score is None or by["XXXUSDT"].rank_score < by["YYYUSDT"].rank_score)


# ------------------------------------------------------------------ interval normalisation
def test_a_240_minute_print_is_normalised_to_the_same_daily_rate_as_a_480_minute_print():
    a = run(s=[snap(fund=(4.0,) * 9, iv=480)])[0]
    b = run(i=[inst(funding_interval_min=240)], s=[snap(fund=(2.0,) * 18, iv=240)])[0]
    assert a.expected_funding_bps_horizon == pytest.approx(b.expected_funding_bps_horizon) == pytest.approx(84.0)
    c = run(s=[snap(fund=(1.0,) * 9, iv=60)])[0]                                                 # 1 bp per hour = 24 bps/day
    assert c.expected_funding_bps_horizon == pytest.approx(24.0 * 7)


@pytest.mark.parametrize("iv", [None, 0, -480, 7, 481])
def test_an_unknown_or_unusable_interval_is_excluded_not_defaulted_to_480(iv):
    o = run(s=[snap(iv=iv)])[0]
    assert "FUNDING_INTERVAL_UNKNOWN" in o.exclusions and o.net_bps_horizon is None and o.rank_score is None and o.expected_funding_bps_horizon is None


def test_the_robust_level_is_min_of_mean_and_median_so_a_spike_cannot_lift_it():
    spike = (1.0,) * 8 + (100.0,)
    o = run(s=[snap(fund=spike)], params={"formation_days": 3.0})[0]
    assert o.expected_funding_bps_horizon == pytest.approx(1.0 * 3 * 7)                         # median 1.0 < mean 12.0


# ------------------------------------------------------------------ tradability
@pytest.mark.parametrize("status", ["PreLaunch", "PendingOpen", "Settling", "Delivering", "Closed", "Mystery"])
def test_only_trading_status_opens_new_entries(status):
    o = run(i=[inst(status=status)])[0]
    assert f"NOT_TRADING:{status}" in o.exclusions and o.rank_score is None


def test_future_launch_and_delivery_inside_the_hold_are_excluded():
    assert "NOT_LAUNCHED" in run(i=[inst(launch_ms=NOW + 1)])[0].exclusions
    assert "DELIVERY_WITHIN_HOLD" in run(i=[inst(delivery_ms=NOW + 3 * T.MS_PER_DAY)])[0].exclusions
    assert run(i=[inst(delivery_ms=NOW + 30 * T.MS_PER_DAY)])[0].exclusions == ()


def test_hedge_rules_for_carry_and_vol_for_unhedged_kinds():
    assert "NO_SPOT_HEDGE" in run(i=[inst(spot_hedge="PERP_ONLY")])[0].exclusions
    assert "HEDGE_UNKNOWN" in run(i=[inst(spot_hedge="HEDGE_UNKNOWN")])[0].exclusions
    d = run(i=[inst(spot_hedge="PERP_ONLY")], kind="PERP_DIRECTIONAL_FUNDING")[0]
    assert "VOL_UNKNOWN" in d.exclusions and d.rank_score is None and d.net_bps_horizon is None                    # a directional short needs a measured volatility
    d2 = run(i=[inst(spot_hedge="PERP_ONLY")], kind="PERP_DIRECTIONAL_FUNDING", params={"price_vol_bps_per_day": {"AAAUSDT": 150.0}})[0]
    assert d2.exclusions == () and d2.kind == "PERP_DIRECTIONAL_FUNDING" and d2.legs == (("AAAUSDT", "Sell"),)
    assert d2.risk_score == pytest.approx(150.0 * 7 ** 0.5)                                       # funding sigma 0 (flat prints) + price sigma only
    assert d2.expected_price_leg_bps == 0.0


# ------------------------------------------------------------------ freshness and the information set
def test_a_snapshot_from_after_the_decision_is_never_read():
    o = run(s=[snap(decision_ms=NOW + 1, fund=(500.0,) * 9)])[0]
    assert o.exclusions == ("SNAPSHOT_FROM_FUTURE",) and o.expected_funding_bps_horizon is None and o.net_bps_horizon is None and o.rank_score is None


def test_stale_snapshot_stale_book_unknown_age_crossed_and_empty_book_next_funding_in_past():
    assert "STALE_SNAPSHOT" in run(s=[snap(decision_ms=NOW - 3_600_000)])[0].exclusions
    assert "BOOK_STALE" in run(s=[snap(age=60_000)])[0].exclusions
    assert "BOOK_AGE_UNKNOWN" in run(s=[snap(age=None)])[0].exclusions
    assert "BOOK_CROSSED" in run(s=[snap(bid=100.5, ask=100.0)])[0].exclusions
    assert "NO_BOOK" in run(s=[snap(bids=(), asks=())])[0].exclusions
    assert "NEXT_FUNDING_IN_PAST" in run(s=[snap(next_funding_ms=NOW - 1)])[0].exclusions
    assert "NO_MARK" in run(s=[snap(mark=None)])[0].exclusions
    assert "SPREAD_TOO_WIDE" in run(s=[snap(bid=99.0, ask=101.0)])[0].exclusions
    assert "NO_SPOT_MARK" in run(s=[snap(spot=None)])[0].exclusions and run(s=[snap(spot=None)])[0].rank_score is None
    assert "BASIS_TOO_WIDE" in run(s=[snap(spot=90.0)])[0].exclusions


def test_missing_instrument_or_snapshot_and_thin_history_are_reported_not_dropped():
    rows = run(i=[inst("AAAUSDT")], s=[snap("AAAUSDT"), snap("BBBUSDT")])
    by = {o.legs[1][0]: o for o in rows}
    assert "NO_INSTRUMENT" in by["BBBUSDT"].exclusions
    rows2 = run(i=[inst("AAAUSDT"), inst("CCCUSDT")], s=[snap("AAAUSDT")])
    assert any("NO_SNAPSHOT" in o.exclusions for o in rows2)
    o = run(s=[snap(fund=(4.0, 4.0))])[0]
    assert "INSUFFICIENT_FUNDING_HISTORY" in o.exclusions and o.net_bps_horizon is None
    o = run(s=[snap(fund=(4.0,) * 8 + (float("nan"),))])[0]
    assert "INSUFFICIENT_FUNDING_HISTORY" in o.exclusions and o.net_bps_horizon is None


def test_unknown_input_is_never_zero():
    for o in (run(s=[snap(iv=None)])[0], run(s=[snap(fund=())])[0], run(s=[snap(spot=None)])[0], run(i=[inst(spot_hedge="HEDGE_UNKNOWN")])[0]):
        assert o.rank_score is None and (o.net_bps_horizon is None or o.exclusions)


# ------------------------------------------------------------------ instrument rules
def test_unknown_rules_and_minimums_exclude():
    for kw in ({"tick_size": None}, {"qty_step": None}, {"min_order_qty": None}):
        assert "RULES_UNKNOWN" in run(i=[inst(**kw)])[0].exclusions
    o = run(i=[inst(min_order_qty="1000000")])[0]
    assert "BELOW_MIN_ORDER_QTY" in o.exclusions and "CAPACITY_BELOW_MIN" in o.exclusions
    o = run(i=[inst(min_notional_usd="1000000")])[0]
    assert "BELOW_MIN_NOTIONAL" in o.exclusions
    o = run(i=[inst(qty_step="1000")])[0]                                                         # 10_000 / 100 = 100 units rounds DOWN to 0 at a step of 1000
    assert "BELOW_MIN_ORDER_QTY" in o.exclusions


# ------------------------------------------------------------------ depth walk and capacity
def test_depth_walk_matches_the_hand_computed_vwap():
    levels = (BookLevel(100.0, 1.0), BookLevel(99.0, 10.0))
    vwap = 150.0 / (1.0 + 50.0 / 99.0)
    assert S.walk_slippage_bps(levels, 150.0, 100.0) == pytest.approx((100.0 - vwap) / 100.0 * 1e4)
    assert S.walk_slippage_bps(levels, 50.0, 100.0) == pytest.approx(0.0)
    assert S.walk_slippage_bps(levels, 5000.0, 100.0) is None                                      # more than the visible depth: not executable, not free
    assert S.walk_slippage_bps(levels, 0.0, 100.0) == 0.0


def test_capacity_is_the_size_where_slippage_equals_the_remaining_edge():
    levels_b = (BookLevel(99.995, 20.0),) + tuple(BookLevel(99.995 - 0.05 * k, 20.0) for k in range(1, 60))
    levels_a = (BookLevel(100.005, 20.0),) + tuple(BookLevel(100.005 + 0.05 * k, 20.0) for k in range(1, 60))
    s_ = MarketSnapshot("AAAUSDT", NOW, 100.0, 100.0, levels_b, levels_a, 100, 4.0, (4.0,) * 9, 480, NOW + 3_600_000, None, 1e9, 99.98)
    o = run(s=[s_], params={"notional_usd": 1e9, "margin_frac": 0.2})[0]
    edge_pre = o.expected_funding_bps_horizon - sum(c.bps for c in o.costs if c.name != "slippage")
    assert o.capacity_usd > 0
    mid = 100.0
    rt = lambda n: S.walk_slippage_bps(levels_b, n, mid) + S.walk_slippage_bps(levels_a, n, mid)        # noqa: E731
    assert rt(o.capacity_usd) == pytest.approx(edge_pre, rel=1e-3)
    assert rt(o.capacity_usd * 1.05) > edge_pre
    big = run(s=[s_], params={"notional_usd": 1e9})[0]
    assert big.net_bps_horizon == pytest.approx(o.net_bps_horizon)                                  # the evaluated size is min(request, capacity): net is at that size
    small = run(s=[s_], params={"notional_usd": 200.0})[0]
    assert small.net_bps_horizon > o.net_bps_horizon                                                # smaller size, less slippage


def test_capacity_is_bounded_by_open_interest_participation_and_max_market_qty():
    o = run(s=[snap(oi=200_000.0)])[0]
    assert o.capacity_usd == pytest.approx(0.01 * 200_000.0)
    o2 = run(i=[inst(max_mkt_order_qty="5")])[0]
    assert o2.capacity_usd == pytest.approx(5 * 100.0)


def test_no_edge_means_zero_capacity_and_an_exclusion():
    o = run(s=[snap(fund=(0.5,) * 9)])[0]
    assert o.capacity_usd == 0.0 and "CAPACITY_BELOW_MIN" in o.exclusions and o.rank_score is None
    n = run(s=[snap(fund=(-1.0,) * 9)])[0]
    assert "FUNDING_NOT_POSITIVE" in n.exclusions


# ------------------------------------------------------------------ the spread family (perp only, NEVER a carry)
def _spread_world():
    syms = {"HI1USDT": 6.0, "HI2USDT": 4.0, "MIDUSDT": 1.0, "LO1USDT": -2.0, "LO2USDT": -4.0}
    ins = [inst(s, spot_hedge="PERP_ONLY") for s in syms]
    sn = [snap(s, fund=(f,) * 9) for s, f in syms.items()]
    vol = {s: 200.0 for s in syms}
    return ins, sn, vol


def test_spread_pairs_short_high_long_low_with_perp_only_costs():
    ins, sn, vol = _spread_world()
    rows = run(ins, sn, kind="PERP_FUNDING_SPREAD", params={"price_vol_bps_per_day": vol, "top_k": 2})
    pairs = [o for o in rows if len(o.legs) == 2]
    assert len(pairs) == 4 and all(o.kind == "PERP_FUNDING_SPREAD" for o in pairs)
    best = rows[0]
    assert best.legs == (("HI1USDT", "Sell"), ("LO2USDT", "Buy"))
    # funding per gross notional: 0.5 x short receives 18 bps/day + 0.5 x long receives 12 bps/day = 15 bps/day over 7 days
    assert best.expected_funding_bps_horizon == pytest.approx(0.5 * 18 * 7 + 0.5 * 12 * 7)
    assert cost(best, "entry_fee").bps + cost(best, "exit_fee").bps == pytest.approx(11.0)           # two perp legs, gross-notional weights
    assert not any(c.name in ("hedge", "financing") for c in best.costs) and best.expected_price_leg_bps == 0.0
    assert best.risk_score == pytest.approx((0.5 ** 2 * (200 * 7 ** 0.5) ** 2 * 2) ** 0.5)           # independent-legs price sigma (pair_corr 0)
    assert any("NOT" in r or "dollar-neutral" in r for r in best.reasons)
    mid = [o for o in rows if o.legs == (("MIDUSDT", "Sell"),)]
    assert mid and "NO_PAIR" in mid[0].exclusions


def test_spread_long_leg_is_charged_the_higher_of_mean_and_median():
    ins, sn, vol = _spread_world()
    sn = [snap("LO2USDT", fund=(-4.0,) * 8 + (-0.0,)) if s.symbol == "LO2USDT" else s for s in sn]
    rows = run(ins, sn, kind="PERP_FUNDING_SPREAD", params={"price_vol_bps_per_day": vol, "top_k": 1})
    pair = next(o for o in rows if len(o.legs) == 2)
    long_leg = pair.legs[1][0]
    assert long_leg == "LO1USDT" or long_leg == "LO2USDT"
    import statistics
    f = (-4.0,) * 8 + (0.0,)
    conservative = max(statistics.fmean(f), statistics.median(f)) * 3                                  # per day, the long PAYS this
    if long_leg == "LO2USDT":
        assert pair.expected_funding_bps_horizon == pytest.approx((0.5 * 18 * 7) + 0.5 * (-conservative) * 7)


def test_a_spread_needs_two_eligible_symbols():
    ins, sn, vol = _spread_world()
    rows = run(ins[:1], sn[:1], kind="PERP_FUNDING_SPREAD", params={"price_vol_bps_per_day": vol})
    assert all(o.rank_score is None for o in rows) and any("NO_PAIR" in o.exclusions for o in rows)


# ------------------------------------------------------------------ order, determinism, purity
def test_ranking_is_deterministic_under_input_order_and_ties_break_by_symbol():
    syms = [f"S{c}USDT" for c in "ABCDEF"]
    ins = [inst(s) for s in syms]
    sn = [snap(s, fund=(3.0 + (i % 3),) * 9) for i, s in enumerate(syms)]
    base = run(ins, sn)
    for seed in range(5):
        rnd = random.Random(seed)
        a, b = list(ins), list(sn)
        rnd.shuffle(a)
        rnd.shuffle(b)
        assert run(a, b) == base
    eligible = [o for o in base if o.rank_score is not None]
    assert [o.rank_score for o in eligible] == sorted((o.rank_score for o in eligible), reverse=True)
    ties = [o for o in eligible if o.rank_score == eligible[0].rank_score]
    assert [o.legs for o in ties] == sorted(o.legs for o in ties)
    assert base == run(dict((i.symbol, i) for i in ins), dict((x.symbol, x) for x in sn))                # mappings are accepted too
    assert all(o.exclusions for o in base[len(eligible):])                                                 # excluded rows come last


def test_the_scanner_does_no_io_and_reads_no_clock(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("I/O or clock touched")
    import builtins
    monkeypatch.setattr(time, "time", boom)
    monkeypatch.setattr(time, "monotonic", boom)
    monkeypatch.setattr(builtins, "open", boom)
    monkeypatch.setattr(os, "listdir", boom)
    import socket
    monkeypatch.setattr(socket.socket, "connect", boom)
    assert run()[0].rank_score is not None


def test_module_source_has_no_io_clock_or_network_imports():
    src = open(os.path.join(BOT, "tools", "ms_scanner.py"), encoding="utf-8").read()
    for bad in ("import time", "import socket", "import requests", "urllib", "datetime", "open(", "import random", "os.environ", "subprocess"):
        assert bad not in src, bad


def test_malformed_calls_are_errors_and_malformed_market_data_is_an_exclusion():
    with pytest.raises(S.ScanError):
        run(params={"hold_dayz": 7})
    with pytest.raises(S.ScanError):
        run(decision_ms=0)
    with pytest.raises(S.ScanError):
        S.scan([inst()], [snap()], spec={"kind": "CARRY_LIKE"}, candidate_id="c", spec_digest="d", decision_ms=NOW)
    with pytest.raises(S.ScanError):
        run(params={"hold_days": -1})
    with pytest.raises(S.ScanError):
        run(params={"p_missed_fill": 2.0})


def test_every_emitted_exclusion_code_is_in_the_documented_vocabulary():
    codes = set()
    for o in (run(i=[inst(status="Closed")])[0], run(s=[snap(age=None)])[0], run(i=[inst(spot_hedge="PERP_ONLY")])[0], run(s=[snap(iv=None)])[0], run(s=[snap(fund=(0.5,) * 9)])[0]):
        codes |= {e.split(":")[0] for e in o.exclusions}
    assert codes <= set(S.EXCLUSIONS)


# ------------------------------------------------------------------ mutation checks
def _mutant(old, new):
    path = os.path.join(BOT, "tools", "ms_scanner.py")
    src = open(path, encoding="utf-8").read()
    assert src.count(old) == 1, old
    mod = types.ModuleType("mss_mut_" + uuid.uuid4().hex)
    mod.__file__ = path
    sys.modules[mod.__name__] = mod
    exec(compile(src.replace(old, new), path, "exec"), mod.__dict__)
    return mod


def _mrun(m, i=None, s=None, **kw):
    return m.scan(i or [inst()], s or [snap()], spec={"kind": kw.pop("kind", "SPOT_PERP_CARRY"), "style": kw.pop("style", "TAKER")}, candidate_id="c", spec_digest="d", decision_ms=NOW, params=kw.pop("params", None))


def test_mutation_trading_status_check_removed():
    m = _mutant("    if inst.status != T.TRADABLE_STATUS:", "    if False:")
    assert _mrun(m, i=[inst(status="Closed")])[0].exclusions == () and run(i=[inst(status="Closed")])[0].exclusions


def test_mutation_future_snapshot_check_removed():
    m = _mutant("    if snap.decision_ms > decision_ms:", "    if False:")
    assert "SNAPSHOT_FROM_FUTURE" not in _mrun(m, s=[snap(decision_ms=NOW + 1)])[0].exclusions and "SNAPSHOT_FROM_FUTURE" in run(s=[snap(decision_ms=NOW + 1)])[0].exclusions


def test_mutation_interval_defaulting_to_480_is_caught():
    m = _mutant("    iv = snap.funding_interval_min\n    if funding_bps_per_day(1.0, iv) is None:", "    iv = snap.funding_interval_min or 480\n    if funding_bps_per_day(1.0, iv) is None:")
    assert _mrun(m, s=[snap(iv=None)])[0].net_bps_horizon is not None and run(s=[snap(iv=None)])[0].net_bps_horizon is None


def test_mutation_rank_on_gross_funding_instead_of_net_is_caught():
    m = _mutant("        rank = (net / capital_factor) / max(risk, float(p[\"risk_floor_bps\"]))", "        rank = (funding_h / capital_factor) / max(risk, float(p[\"risk_floor_bps\"]))")
    thin = MarketSnapshot("XXXUSDT", NOW, 100.0, 100.0, (BookLevel(99.995, 5.0), BookLevel(95.0, 3000.0)), (BookLevel(100.005, 5.0), BookLevel(105.0, 3000.0)), 100, 24.0, (24.0,) * 9, 480,
                          NOW + 3_600_000, None, 1e9, 99.98)
    ins, sn = [inst("XXXUSDT"), inst("YYYUSDT")], [thin, snap("YYYUSDT", fund=(6.0,) * 9)]
    assert _mrun(m, ins, sn)[0].legs[1][0] == "XXXUSDT" and run(ins, sn)[0].legs[1][0] == "YYYUSDT"


def test_mutation_unknown_net_as_zero_is_caught():
    m = _mutant("    net = None\n    unknown = [c.name for c in costs if c.bps is None]", "    net = 0.0\n    unknown = [c.name for c in costs if c.bps is None]")
    assert _mrun(m, s=[snap(spot=None)])[0].net_bps_horizon == 0.0 and run(s=[snap(spot=None)])[0].net_bps_horizon is None


def test_mutation_exit_fee_dropped_is_caught():
    m = _mutant("return [CostLine(\"entry_fee\", entry, prov), CostLine(\"exit_fee\", base_exit, \"VENUE_STATED\")], reasons", "return [CostLine(\"entry_fee\", entry, prov), CostLine(\"exit_fee\", 0.0, \"VENUE_STATED\")], reasons")
    assert _mrun(m)[0].net_bps_horizon > run()[0].net_bps_horizon


def test_mutation_long_leg_charged_the_favourable_statistic_is_caught():
    m = _mutant("    v = -max(f[\"mean_bps_per_day\"], f[\"median_bps_per_day\"])", "    v = -min(f[\"mean_bps_per_day\"], f[\"median_bps_per_day\"])")
    f = {"mean_bps_per_day": -1.0, "median_bps_per_day": -4.0}
    assert m.leg_funding_bps_per_day(f, "Buy", 1.0) != S.leg_funding_bps_per_day(f, "Buy", 1.0)
    assert S.leg_funding_bps_per_day(f, "Buy", 1.0) == pytest.approx(1.0)                              # the long pays max(-1,-4) = -1 -> receives 1 (the smaller receipt)


def test_mutation_slippage_ignored_is_caught():
    m = _mutant("            costs.append(CostLine(\"slippage\", slippage, \"DERIVED\"))", "            costs.append(CostLine(\"slippage\", 0.0, \"DERIVED\"))")
    thin = MarketSnapshot("AAAUSDT", NOW, 100.0, 100.0, (BookLevel(99.995, 5.0), BookLevel(95.0, 3000.0)), (BookLevel(100.005, 5.0), BookLevel(105.0, 3000.0)), 100, 4.0, (4.0,) * 9, 480,
                          NOW + 3_600_000, None, 1e9, 99.98)
    assert _mrun(m, s=[thin], params={"notional_usd": 100_000.0})[0].net_bps_horizon > run(s=[thin], params={"notional_usd": 100_000.0})[0].net_bps_horizon
