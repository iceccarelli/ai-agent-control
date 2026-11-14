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
             last_funding_bps_per_print=fund[-1] if fund else None, trailing_funding_bps_per_print=tuple(fund), trailing_funding_intervals_min=(iv,) * len(fund) if isinstance(iv, int) else (), funding_interval_min=iv, next_funding_ms=NOW + 3_600_000,
             open_interest_usd=oi, turnover24h_usd=1e9, spot_mark=spot)
    d.update(kw)
    return MarketSnapshot(**d)


def run(i=None, s=None, *, kind="SPOT_PERP_CARRY", style="TAKER", params=None, decision_ms=NOW):
    i = i if i is not None else [inst()]
    s = s if s is not None else [snap()]
    return S.scan(i, s, spec={"kind": kind, "style": style}, candidate_id="c@1", spec_digest="d" * 8, decision_ms=decision_ms,
                  params={"persistence": 1.0, "opportunity_cost_apr": 0.0, **(params or {})})        # the economics tests pin persistence 1.0 (the default 0.5 is tested on its own) and STATE a 0% alternative
                                                                                                   # return (Round 20: the DEFAULT is UNKNOWN, tested on its own below; a stated zero is a statement)


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
    assert cost(o, "financing").bps == 0.0 and cost(o, "financing").prov == "MODELLED"                  # a STATED 0% (the helper states it): a statement, labelled like the edge gate labels an owner's figure
    assert cost(o, "slippage").bps == pytest.approx(0.0) and cost(o, "slippage").prov == "DERIVED"
    assert o.net_bps_horizon == pytest.approx(84.0 - 31.0 - 1.0 - 2.0)
    basis = (100.0 - 99.98) / 99.98 * 1e4
    assert o.risk_score == pytest.approx(basis + 2.0)                                           # zero funding sigma (flat prints) + |basis| + ASSUMED floor
    assert o.rank_score == pytest.approx((o.net_bps_horizon / 2.04) / o.risk_score)                                  # Round 21: the registry's ACQUIRED_SPOT_PERP capital (2.04x at leverage 1), not the research 1.2x
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
    assert not any(c.name == "hedge" for c in d.costs)                                              # a perp-only book has no spot leg (its committed margin still has a financing line: Round 20)


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
                            NOW + 3_600_000, None, 1e9, 99.98, trailing_funding_intervals_min=(480,) * 9)
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
    s_ = MarketSnapshot("AAAUSDT", NOW, 100.0, 100.0, levels_b, levels_a, 100, 4.0, (4.0,) * 9, 480, NOW + 3_600_000, None, 1e9, 99.98, trailing_funding_intervals_min=(480,) * 9)
    o = run(s=[s_], params={"notional_usd": 1e9, "leverage": 1})[0]
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
    assert not any(c.name == "hedge" for c in best.costs) and best.expected_price_leg_bps == 0.0      # no spot leg; the committed margin still has an opportunity cost (Round 20: a financing line)
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
    return m.scan(i or [inst()], s or [snap()], spec={"kind": kw.pop("kind", "SPOT_PERP_CARRY"), "style": kw.pop("style", "TAKER")}, candidate_id="c", spec_digest="d", decision_ms=NOW, params={"persistence": 1.0, "opportunity_cost_apr": 0.0, **(kw.pop("params", None) or {})})


def test_mutation_trading_status_check_removed():
    m = _mutant("    if inst.status != T.TRADABLE_STATUS:", "    if False:")
    assert _mrun(m, i=[inst(status="Closed")])[0].exclusions == () and run(i=[inst(status="Closed")])[0].exclusions


def test_mutation_future_snapshot_check_removed():
    m = _mutant("    if snap.decision_ms > decision_ms:", "    if False:")
    assert "SNAPSHOT_FROM_FUTURE" not in _mrun(m, s=[snap(decision_ms=NOW + 1)])[0].exclusions and "SNAPSHOT_FROM_FUTURE" in run(s=[snap(decision_ms=NOW + 1)])[0].exclusions


def test_mutation_interval_defaulting_to_480_is_caught():
    m = _mutant("    iv = snap.funding_interval_min\n    if funding_bps_per_day(1.0, iv) is None:", "    iv = snap.funding_interval_min or 480\n    if funding_bps_per_day(1.0, iv) is None:")
    sn = snap(iv=None, trailing_funding_intervals_min=(480,) * 9)
    assert _mrun(m, s=[sn])[0].net_bps_horizon is not None and run(s=[sn])[0].net_bps_horizon is None


def test_mutation_rank_on_gross_funding_instead_of_net_is_caught():
    m = _mutant("        rank = (net / capital_factor) / max(risk, float(p[\"risk_floor_bps\"]))", "        rank = (funding_h / capital_factor) / max(risk, float(p[\"risk_floor_bps\"]))")
    thin = MarketSnapshot("XXXUSDT", NOW, 100.0, 100.0, (BookLevel(99.995, 5.0), BookLevel(95.0, 3000.0)), (BookLevel(100.005, 5.0), BookLevel(105.0, 3000.0)), 100, 24.0, (24.0,) * 9, 480,
                          NOW + 3_600_000, None, 1e9, 99.98, trailing_funding_intervals_min=(480,) * 9)
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
                          NOW + 3_600_000, None, 1e9, 99.98, trailing_funding_intervals_min=(480,) * 9)
    assert _mrun(m, s=[thin], params={"notional_usd": 100_000.0})[0].net_bps_horizon > run(s=[thin], params={"notional_usd": 100_000.0})[0].net_bps_horizon


# ------------------------------------------------------------------ per-print intervals (red-team sc1) and persistence
def _mixed_snap(**kw):
    prints = tuple([5.0] * 9 + [2.5] * 3)                                   # 9 x 480m at 5 bps + 3 x 240m at 2.5 bps: a flat 15 bps/day throughout
    ivs = tuple([480] * 9 + [240] * 3)
    return snap(fund=prints, iv=240, trailing_funding_intervals_min=ivs, **kw)


def test_a_480_to_240_change_does_not_inflate_the_old_prints_poc():
    f, ex = S._funding(_mixed_snap(), S._params({"formation_days": 3.0}))
    assert ex == [] and f["mean_bps_per_day"] == pytest.approx(15.0) and f["median_bps_per_day"] == pytest.approx(15.0) and f["sd_bps_per_day"] == pytest.approx(0.0)
    o = run(i=[inst(funding_interval_min=240)], s=[_mixed_snap()], params={"formation_days": 3.0})[0]
    assert o.expected_funding_bps_horizon == pytest.approx(15.0 * 7)         # interval-normalised per-day figure x hold days


@pytest.mark.parametrize("ivs", [(), (480,) * 11, (480,) * 13, (480,) * 8 + (0, 480, 480, 240), (480,) * 8 + (7, 480, 480, 240)])
def test_missing_or_mismatched_or_unusable_per_print_intervals_exclude(ivs):
    o = run(i=[inst(funding_interval_min=240)], s=[snap(fund=tuple([5.0] * 9 + [2.5] * 3), iv=240, trailing_funding_intervals_min=ivs)])[0]
    assert "FUNDING_INTERVAL_PER_PRINT_UNKNOWN" in o.exclusions and o.net_bps_horizon is None and o.rank_score is None and o.expected_funding_bps_horizon is None


def test_the_formation_window_covers_interval_time_not_a_print_count():
    prints = tuple([50.0] * 6 + [5.0] * 9)                                  # 6 old 480m prints (2 days, 150/day) then 9 x 480m = 3 days at 15/day
    f, _ = S._funding(snap(fund=prints, iv=480, trailing_funding_intervals_min=(480,) * 15), S._params({"formation_days": 3.0}))
    assert f["n_prints"] == 9.0 and f["mean_bps_per_day"] == pytest.approx(15.0)
    mixed = tuple([50.0] * 4 + [2.5] * 3)                                   # the newest 3 x 240m prints cover 0.5 day of a 0.5-day window
    f2, _ = S._funding(snap(fund=mixed, iv=240, trailing_funding_intervals_min=(480,) * 4 + (240,) * 3), S._params({"formation_days": 0.5, "min_prints": 3}))
    assert f2["n_prints"] == 3.0 and f2["mean_bps_per_day"] == pytest.approx(15.0)


def test_default_persistence_is_a_stated_modelled_decay_recorded_in_the_reasons():
    assert S.DEFAULT_PARAMS["persistence"] == 0.5
    o = S.scan([inst()], [snap()], spec={"kind": "SPOT_PERP_CARRY", "style": "TAKER"}, candidate_id="c", spec_digest="d", decision_ms=NOW, params=None)[0]
    assert o.expected_funding_bps_horizon == pytest.approx(12.0 * 7 * 0.5)
    assert any("persistence 0.5" in r and "MODELLED" in r for r in o.reasons)
    with pytest.raises(S.ScanError):
        S.scan([inst()], [snap()], spec={"kind": "SPOT_PERP_CARRY", "style": "TAKER"}, candidate_id="c", spec_digest="d", decision_ms=NOW, params={"persistence": 1.5})


def test_a_higher_persistence_assumption_cannot_rank_an_assumed_result_above_a_measured_one():
    # AAA: wide book -> MEASURED spread (weakest cost prov MODELLED). BBB: tight book -> ASSUMED spread. BBB has far more funding, so under persistence 1.0 it out-scores AAA on rank_score.
    ins = [inst("AAAUSDT", spot_hedge="PERP_ONLY"), inst("BBBUSDT", spot_hedge="PERP_ONLY")]
    sn = [snap("AAAUSDT", bid=99.95, ask=100.05, fund=(5.0,) * 9, iv=480), snap("BBBUSDT", bid=99.999, ask=100.001, fund=(20.0,) * 9, iv=480)]
    vol = {"AAAUSDT": 50.0, "BBBUSDT": 50.0}
    for pers in (0.5, 1.0):
        rows = S.scan(ins, sn, spec={"kind": "PERP_DIRECTIONAL_FUNDING", "style": "TAKER"}, candidate_id="c", spec_digest="d", decision_ms=NOW, params={"persistence": pers, "price_vol_bps_per_day": vol, "opportunity_cost_apr": 0.0})
        by = {o.legs[0][0]: o for o in rows}
        assert by["AAAUSDT"].prov == "MODELLED" and by["BBBUSDT"].prov == "ASSUMED" and by["BBBUSDT"].rank_score > by["AAAUSDT"].rank_score
        assert [o.legs[0][0] for o in rows[:2]] == ["AAAUSDT", "BBBUSDT"]


def test_mutation_per_print_interval_replaced_by_the_current_interval_is_caught():
    m = _mutant("day = [x * 1440.0 / v for x, v in zip(xs, ivs)]", "day = [x * 1440.0 / iv for x in xs]")
    assert m._funding(_mixed_snap(), m._params(None))[0]["mean_bps_per_day"] != pytest.approx(15.0)
    assert S._funding(_mixed_snap(), S._params(None))[0]["mean_bps_per_day"] == pytest.approx(15.0)


def test_mutation_per_print_unknown_guard_removed_is_caught():
    m = _mutant("if len(ivs) != len(xs) or (xs and", "if False and (xs and")
    sn = snap(fund=(5.0,) * 9, iv=480, trailing_funding_intervals_min=())
    assert S._funding(sn, S._params(None))[1] == ["FUNDING_INTERVAL_PER_PRINT_UNKNOWN"]
    assert m._funding(sn, m._params(None))[1] != ["FUNDING_INTERVAL_PER_PRINT_UNKNOWN"]


def test_mutation_provenance_ranking_removed_is_caught():
    m = _mutant("key=lambda o: (T.PROVS.index(o.prov) if o.prov in T.PROVS else len(T.PROVS), -o.rank_score, o.legs))", "key=lambda o: (0, -o.rank_score, o.legs))")
    ins = [inst("AAAUSDT", spot_hedge="PERP_ONLY"), inst("BBBUSDT", spot_hedge="PERP_ONLY")]
    sn = [snap("AAAUSDT", bid=99.95, ask=100.05, fund=(5.0,) * 9, iv=480), snap("BBBUSDT", bid=99.999, ask=100.001, fund=(20.0,) * 9, iv=480)]
    params = {"persistence": 1.0, "opportunity_cost_apr": 0.0, "price_vol_bps_per_day": {"AAAUSDT": 50.0, "BBBUSDT": 50.0}}
    sp = {"kind": "PERP_DIRECTIONAL_FUNDING", "style": "TAKER"}
    assert m.scan(ins, sn, spec=sp, candidate_id="c", spec_digest="d", decision_ms=NOW, params=params)[0].legs[0][0] == "BBBUSDT"
    assert S.scan(ins, sn, spec=sp, candidate_id="c", spec_digest="d", decision_ms=NOW, params=params)[0].legs[0][0] == "AAAUSDT"


# ------------------------------------------------------------------ Round 20: unknown financing is UNKNOWN, never a numeric 0.0
def _unscored(**kw):
    """A scan that does NOT state the owner's alternative return: the scanner's own default."""
    i, sn = kw.pop("i", [inst()]), kw.pop("s", [snap()])
    return S.scan(i, sn, spec={"kind": kw.pop("kind", "SPOT_PERP_CARRY"), "style": "TAKER"}, candidate_id="c@1", spec_digest="d" * 8, decision_ms=NOW,
                  params={"persistence": 1.0, **kw.pop("params", {})})


def test_an_unstated_financing_input_is_unknown_and_the_row_cannot_be_ranked_or_traded():
    """Round 20 (P0): ``opportunity_cost_apr`` defaulted to carry_costs.DEFAULT_OPPORTUNITY_COST_APR = 0.0, so EVERY unpriced carry candidate got `financing = 0.0 (ASSUMED)`, a computed net and a rank
    (the module's own docstring says 'Unknown input => net None and unrankable. Never zero, never defaulted'). An unpriced cost of capital is not zero."""
    o = _unscored()[0]
    f = cost(o, "financing")
    assert f.bps is None and f.prov == "UNKNOWN", f
    assert o.net_bps_horizon is None and o.rank_score is None and o.prov == "UNKNOWN"
    assert "FINANCING_UNKNOWN" in o.exclusions and "FINANCING_UNKNOWN" in S.EXCLUSIONS
    assert any("FINANCING_UNKNOWN" in r for r in o.reasons)
    # the same row with the rate STATED is priced and ranked: the ONLY difference is the owner's statement
    stated = run()[0]
    assert stated.exclusions == () and stated.net_bps_horizon is not None and stated.rank_score is not None


def test_a_stated_financing_rate_is_charged_on_the_capital_the_ranking_divides_by():
    # 3.65%/yr = 1 bp/day of capital; 7 days; the carry commits 2.04x per unit of notional (Round 21: economic_model's ACQUIRED_SPOT_PERP at leverage 1, the factor rank_score divides by; it was the research 1.2x)
    o = run(params={"opportunity_cost_apr": 0.0365})[0]
    f = cost(o, "financing")
    assert (f.bps, f.prov) == (pytest.approx(1.0 * 7 * 2.04), "MODELLED")
    base = run()[0]
    assert o.net_bps_horizon == pytest.approx(base.net_bps_horizon - 14.28) and o.exclusions == ()
    assert o.rank_score == pytest.approx((o.net_bps_horizon / 2.04) / max(o.risk_score, 1.0)) and o.rank_score < base.rank_score
    harsh = run(params={"opportunity_cost_apr": 0.365})[0]                                        # 10 bps/day x 7 x 2.04 = 142.8 bps: more than the whole 84 bps of funding, before any fee
    assert cost(harsh, "financing").bps == pytest.approx(142.8) and harsh.rank_score is None
    assert harsh.exclusions and "FINANCING_UNKNOWN" not in harsh.exclusions, "PRICED and refused (the capacity rule fires first: no edge is left to trade), not 'unknown'"


def test_a_stated_zero_is_a_statement_not_an_unknown():
    o = run(params={"opportunity_cost_apr": 0.0})[0]
    assert (cost(o, "financing").bps, cost(o, "financing").prov) == (0.0, "MODELLED") and "FINANCING_UNKNOWN" not in o.exclusions and o.net_bps_horizon is not None


@pytest.mark.parametrize("bad", [True, False, float("nan"), float("inf"), float("-inf"), -0.01, "0.05", [0.05], {"a": 1}])
def test_a_malformed_financing_input_is_refused_never_coerced_to_a_number(bad):
    with pytest.raises(S.ScanError, match="opportunity_cost_apr"):
        _unscored(params={"opportunity_cost_apr": bad})


def test_the_perp_only_spread_is_unpriced_without_the_rate_too():
    """The committed margin of a perp-only book has an opportunity cost as well (it had no financing line at all)."""
    ins, sn, vol = _spread_world()
    unpriced = _unscored(i=ins, s=sn, kind="PERP_FUNDING_SPREAD", params={"price_vol_bps_per_day": vol, "top_k": 2})
    pairs = [o for o in unpriced if len(o.legs) == 2]
    assert pairs and all("FINANCING_UNKNOWN" in o.exclusions and o.rank_score is None and o.net_bps_horizon is None for o in pairs)
    priced = run(ins, sn, kind="PERP_FUNDING_SPREAD", params={"price_vol_bps_per_day": vol, "top_k": 2, "opportunity_cost_apr": 0.0365})
    best = priced[0]
    assert (cost(best, "financing").bps, cost(best, "financing").prov) == (pytest.approx(1.0 * 7 * 1.02), "MODELLED")          # Round 21: economic_model's PERP_ONLY margin + buffer (1.02x at leverage 1), not a fifth of it
    assert best.exclusions == () and best.rank_score is not None


def test_an_unknown_cost_can_never_make_a_candidate_pass_the_profitability_gate_or_outrank_a_priced_one():
    """Two symbols; the richer one would win with a free cost of capital. With the rate UNSTATED neither is eligible (no ranking exists); with it STATED both are ranked by their priced net."""
    ins = [inst("AAAUSDT"), inst("BBBUSDT")]
    sn = [snap("AAAUSDT", fund=(6.0,) * 9), snap("BBBUSDT", fund=(4.0,) * 9)]
    unpriced = _unscored(i=ins, s=sn)
    assert all(o.rank_score is None and o.net_bps_horizon is None for o in unpriced) and not any(not o.exclusions for o in unpriced)
    priced = run(ins, sn, params={"opportunity_cost_apr": 0.0})
    assert [o.legs[1][0] for o in priced if not o.exclusions] == ["AAAUSDT", "BBBUSDT"]
    harsh = run(ins, sn, params={"opportunity_cost_apr": 5.0})                    # a stated 500%/yr: the same rows are PRICED and refused (they do not 'pass by being unknown', nor fail by it)
    assert all(o.exclusions and "FINANCING_UNKNOWN" not in o.exclusions and o.rank_score is None for o in harsh)
