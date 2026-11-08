"""Economic model: exact hand-computed expectations and cross-checks against the repo's existing cost authorities.

Fixture world (hand-computable): BTCUSDT mid 50000; best bid 49990 / ask 50010 => half-spread 10/50000 = 2 bps.
Book depth: bids 0.5@49990, 1.5@49980; asks 0.5@50010, 1.5@50020.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from decimal import Decimal as D

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(BOT, "tools"), BOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)
if os.environ.get("ECON_MODEL_DIR"):                  # mutation runs: the disposable copy must win
    sys.path.insert(0, os.environ["ECON_MODEL_DIR"])

import pytest  # noqa: E402

import carry_costs as CC  # noqa: E402
import carry_prospective as CP  # noqa: E402
import carry_teardown as CT  # noqa: E402
import economic_model as EM  # noqa: E402
import strategy_verdict as SV  # noqa: E402

NOW = 1_700_000_000_000
H = 3_600_000
INPUT_SHA = hashlib.sha256(b"economic fixture input data").hexdigest()
with open(os.path.join(BOT, "research", "candidates", "cross_sectional_funding.json"), encoding="utf-8") as _f:
    SPEC = json.load(_f)
SPEC["fee_model"]["taker_bps"]["provenance"] = "measured"         # the verdict layer refuses a cost PASS when EVERY spec cost input is assumed
SPEC["capacity_model"]["min_viable_capacity_usd"] = 100.0           # a real, valid candidate spec (candidate_id + digest are stamped on evidence)


def perp(**kw):
    a = dict(symbol="BTCUSDT", category="linear", base_coin="BTC", quote_coin="USDT", tick_size=D("0.1"), qty_step=D("0.001"),
             min_qty=D("0.001"), min_notional=D("5"), status="Trading", funding_interval_hours=8,
             funding_cap=D("0.0075"), funding_floor=D("-0.0075"))
    a.update(kw)
    return EM.Instrument(**a).validate()


def spot(**kw):
    a = dict(symbol="BTCUSDT", category="spot", base_coin="BTC", quote_coin="USDT", tick_size=D("0.01"), qty_step=D("0.0001"),
             min_qty=D("0.0001"), min_notional=D("5"), status="Trading")
    a.update(kw)
    return EM.Instrument(**a).validate()


def book(ts=NOW - 1000):
    return EM.Book(bids=((D("49990"), D("0.5")), (D("49980"), D("1.5"))), asks=((D("50010"), D("0.5")), (D("50020"), D("1.5"))), ts_ms=ts)


def tier(taker="0.00055", maker="0.0002", source="venue_stated", observed=NOW - H):
    return EM.FeeTier(maker=D(maker), taker=D(taker), source=source, observed_ms=observed)


def q(v, p="modelled", u=""):
    return EM.Quantity(D(v), p, u)


def overlay(*, rate="0.0001", n=30, notional="50000", fee=None, bk=None, **over):
    """Short-perp overlay on owned BTC (no spot leg): 11 bps round trip. Every input known and non-assumed."""
    pi = perp()
    leg = EM.Leg("perp", pi, "sell", bk or book(), fee or tier())
    kw = dict(legs=(leg,), notional_usd=D(notional), gross_edge_bps=EM.carry_gross_edge(pi, q(rate, "modelled"), hold_days=D(n) * 8 / 24),
              hold_days=D(n) * 8 / 24, now_ms=NOW, max_book_age_ms=5000, max_fee_age_ms=24 * H,
              impact_bps=q("0.5"), basis_cost_bps=q("1"), borrow_apr=q("0", "measured"), opportunity_cost_apr=q("0.0365", "venue_stated"),
              hedge_ratio=q("1", "measured"), adverse_move_bps=q("100", "modelled"))
    kw.update(over)
    return EM.Scenario(**kw)


# ---------------------------------------------------------------- Quantity
def test_unknown_propagates_and_is_never_zero():
    u = EM.unknown("bps")
    assert (q("1") + u).value is None and (u * 0).value is None and (q("1") - u).provenance == "unknown"
    assert EM.qsum([q("1"), u, q("2")]).value is None
    assert (q("1") / q("0")).value is None                      # /0 is unknown, not inf and not 0
    assert EM.at_least(u, 0) is None
    with pytest.raises(EM.EconomicModelError):
        u.require()
    with pytest.raises(EM.EconomicModelError):
        EM.Quantity(None, "measured")                           # a missing value cannot claim to be measured
    with pytest.raises(EM.EconomicModelError):
        EM.Quantity(D(0), "unknown")                            # a zero cannot claim to be unknown


def test_provenance_is_weakest_floored_at_derived_and_estimate_is_not_observation():
    assert (q("1", "measured") + q("2", "measured")).provenance == "derived"
    assert (q("1", "measured") + q("2", "modelled")).provenance == "modelled"
    assert (q("1", "venue_stated") * q("2", "assumed")).provenance == "assumed"
    assert (q("3", "measured") * 2).value == D("6")
    assert q("1", "measured") != q("1", "modelled")
    assert q("1", "measured").observed and not q("1", "modelled").observed
    for bad in (1.5, True):
        with pytest.raises(EM.EconomicModelError):
            EM.to_decimal(bad)                                  # floats never enter exact arithmetic
    with pytest.raises(EM.EconomicModelError):
        q("1") + 1.5


# ---------------------------------------------------------------- Instrument
def test_instrument_rows_strict():
    row = {"category": "linear", "symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT", "status": "Trading", "fundingInterval": 480,
           "upperFundingRate": "0.0075", "lowerFundingRate": "-0.0075", "priceFilter": {"tickSize": "0.10"},
           "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "minNotionalValue": "5"}}
    i = EM.Instrument.from_venue_row(row, expect_symbol="BTCUSDT", expect_category="linear")
    assert i.funding_interval_hours == 8 and i.interval_ms == 28_800_000 and i.tick_size == D("0.1")
    assert EM.Instrument.from_venue_row(dict(row, fundingInterval=240), expect_symbol="BTCUSDT", expect_category="linear").settlements_per_day == D(6)
    assert i.round_qty(D("1.23456")) == D("1.234")
    assert i.order_problems(D("1.234"), D("50000.1")) == []
    assert i.order_problems(D("0.0005"), D("5000.15")) == ["BELOW_MIN_QTY", "QTY_OFF_STEP", "PRICE_OFF_TICK", "BELOW_MIN_NOTIONAL"]
    assert not perp(status="Settling").tradable


# ---------------------------------------------------------------- fees
def test_fee_units_and_rebate_and_base_coin():
    assert EM.fee_fraction("5.5", "bps") == D("0.00055") == EM.fee_fraction("0.055", "percent") == EM.fee_fraction("0.00055", "fraction")
    assert EM.fee_fraction("-0.5", "bps") == D("-0.00005")       # maker rebate accepted
    assert EM.fee_fraction("0", "bps") == 0
    # a base-coin fee of 0.1% on a 1.000 BTC buy leaves 0.999 held
    assert EM.held_qty_after_fee(D("1.000"), D("0.001"), True) == D("0.999")
    assert EM.held_qty_after_fee(D("1.000"), D("0.001"), False) == D("1.000")


def test_fee_age():
    t = tier()
    assert t.rate("taker", NOW, 24 * H) == EM.Quantity(D("0.00055"), "venue_stated", "fraction")
    assert t.rate("taker", NOW - H + 24 * H + 1, 24 * H).value is None      # one ms older than the limit -> unknown
    assert t.rate("taker", NOW - 2 * H, 24 * H).value is None               # observed in the future -> unknown
    assert EM.FeeTier(D("0.00055"), D("0.00055"), "assumed").rate("taker", NOW, 1).provenance == "assumed"


# ---------------------------------------------------------------- book walk
def test_walk_exact():
    w = EM.walk_book(book(), "buy", D("1.0"), now_ms=NOW, max_age_ms=5000)
    # 0.5*50010 + 0.5*50020 = 50015.0 ; touch 50010 -> 5/50000 = 1 bp ; half-spread 10/50000 = 2 bps
    assert (w.executable, w.avg_price, w.slippage_bps, w.half_spread_bps, w.total_bps) == (True, D("50015"), D("1"), D("2"), D("3"))
    s = EM.walk_book(book(), "sell", D("1.0"), now_ms=NOW, max_age_ms=5000)
    assert s.avg_price == D("49985") and s.slippage_bps == D("1")
    assert EM.walk_book(book(), "buy", D("2.001"), now_ms=NOW, max_age_ms=5000).reason == "BOOK_TOO_THIN"
    assert EM.walk_book(book(NOW - 5001), "buy", D("1"), now_ms=NOW, max_age_ms=5000).reason == "STALE_BOOK"
    crossed = EM.Book(((D("50020"), D(1)),), ((D("50010"), D(1)),), NOW)
    assert EM.walk_book(crossed, "buy", D("1"), now_ms=NOW, max_age_ms=5000).reason.startswith("BAD_BOOK")


# ---------------------------------------------------------------- funding
def test_funding_sign_convention():
    # rate +1 bp, SHORT 2 BTC at 50000: shorts receive 2*50000*0.0001 = 10
    assert EM.funding_cash(D("0.0001"), D("-2"), D("50000")) == D("10.0000")
    assert EM.funding_cash(D("0.0001"), D("2"), D("50000")) == D("-10.0000")
    assert EM.funding_cash(D("-0.0001"), D("-2"), D("50000")) == D("-10.0000")


def test_funding_recon_hand_numbers():
    p = perp()
    rows = [EM.Settlement(t, "BTCUSDT", D("0.0001"), D("-2"), D("50000"), D("10")) for t in (8 * H, 16 * H, 24 * H)]
    r = EM.reconcile_funding(p, rows, 0, 24 * H)
    assert r.total == EM.Quantity(D("30"), "measured", "usd") and r.flags == ()
    assert EM.expected_settlement_times(p, 0, 24 * H) == [8 * H, 16 * H, 24 * H]
    assert len(EM.expected_settlement_times(perp(funding_interval_hours=4), 0, 24 * H)) == 6


# ---------------------------------------------------------------- the chain
def test_overlay_chain_hand_computed():
    r = EM.evaluate_chain(overlay())
    c = r.components
    # gross: 0.0001*1e4*30 = 30 bps. fees 2*5.5 = 11. spread 2*2 = 4. slippage: entry 1 + exit 1 = 2.
    assert r.stages["THEORETICAL"].value == D("30")
    assert c["fees"].value == D("11.00000") and c["spread"].value == D("4") and c["slippage"].value == D("2")
    assert r.stages["EXECUTABLE"].value == D("13")                       # 30 - 11 - 4 - 2
    # borrow 0; opportunity 0.0365 * (30*8/24 = 10 days)/365*1e4 = 10 ; hedge err |1-1|*100 = 0 ; impact .5 ; basis 1
    assert c["opportunity_cost"].value == D("10")
    assert r.stages["CAPTURABLE"].value == D("1.5")                      # 13 - 0.5 - 1 - 10
    assert r.stages["REALISED"].value is None and r.executable and r.unknown_inputs == ()


def test_partial_hedge_costs_edge():
    r = EM.evaluate_chain(overlay(hedge_ratio=q("0.6", "measured")))
    assert r.components["hedge_error"].value == D("40.0")                # |1-0.6| * 100
    assert r.stages["CAPTURABLE"].value == D("1.5") - D("40")


def test_maker_rebate_leg():
    # maker rebate -1 bp: fees = 2*(-0.0001)*1e4 = -2 ; no spread/slippage ; adverse selection 3 per maker leg
    r = EM.evaluate_chain(overlay(fee=tier(maker="-0.0001"), adverse_selection_bps=q("3"), maker_fill_probability=q("1"),
                                  legs=(EM.Leg("perp", perp(), "sell", book(), tier(maker="-0.0001"), role="maker"),)))
    assert r.components["fees"].value == D("-2.0000") and r.components["spread"].value == 0 and r.components["adverse_selection"].value == D("3")
    assert r.stages["EXECUTABLE"].value == D("32")                       # 30 + 2


def test_two_leg_generic_spread():
    # long-spot / short-perp style spread, gross 25 bps: fees 2*(10+5.5) = 31
    legs = (EM.Leg("spot", spot(), "buy", book(), tier(taker="0.0010")), EM.Leg("perp", perp(), "sell", book(), tier()))
    r = EM.evaluate_chain(overlay(legs=legs, gross_edge_bps=q("25")))
    assert r.components["fees"].value == D("31.00000") and r.components["spread"].value == D("8") and r.components["slippage"].value == D("4")
    assert r.stages["EXECUTABLE"].value == D("-18")                      # 25 - 31 - 8 - 4


def test_funding_edge_screened_against_instrument_bounds():
    p = perp()
    d10 = D(10)                                                                          # 10 days on an 8h perp = 30 settlements
    assert EM.carry_gross_edge(p, q("0.0001"), hold_days=d10).value == D("30")
    assert EM.carry_gross_edge(p, q("0.0001"), hold_days=d10, short=False).value == D("-30")      # a long pays
    assert EM.carry_gross_edge(p, q("0.0076"), hold_days=d10).value is None                       # above the venue cap: not an edge
    assert EM.carry_gross_edge(p, EM.unknown(), hold_days=d10).value is None


# ---------------------------------------------------------------- capacity
def test_capacity_exact():
    base = dict(impact_bps=q("0"), basis_cost_bps=q("0"), opportunity_cost_apr=q("0", "measured"))
    # net(q) = 30 - 11 - 4 - slip ; slip(q) = 4*(q-0.5)/q for 0.5<q<=2  => net = 11 + 2/q. Floor 12.5 => q <= 1.3333; step .001 => q=1.333 max
    # q=1.334 starts at N = 1.334*50000 = 66700 => capacity is the last cent below it.
    assert EM.capacity_usd(overlay(**base), "12.5").value == D("66699.99")
    assert EM.capacity_usd(overlay(**base), "5").value == D("100000.00")             # depth-limited: 2.0 BTC * 50000
    assert EM.capacity_usd(overlay(**base), "20").value == D("0")                    # even the smallest order fails -> known zero
    assert EM.depth_limit_usd(overlay()) == D("100000")
    assert EM.capacity_usd(overlay(**dict(base, basis_cost_bps=EM.unknown("bps"))), "5").value is None


# ---------------------------------------------------------------- evidence for strategy_verdict
def test_cost_model_evidence_counts_only_when_whole():
    item = EM.cost_model_evidence(EM.evaluate_chain(overlay(opportunity_cost_apr=q("0.0", "measured"))), spec=SPEC, data_sha256=INPUT_SHA, recorded_ms=1)
    assert item.result == SV.PASS and item.metrics["unknown_cost_inputs"] == 0
    assert item.metrics["net_edge_bps_after_costs"] == 11.5                         # 13 - 0.5 - 1
    assert SV.counted(item, SPEC) == (True, None)
    unk = EM.cost_model_evidence(EM.evaluate_chain(overlay(basis_cost_bps=EM.unknown("bps"))), spec=SPEC, data_sha256=INPUT_SHA)
    assert unk.result == SV.INCONCLUSIVE and unk.metrics["unknown_cost_inputs"] == 1 and unk.metrics["net_edge_bps_after_costs"] is None
    assert SV.counted(unk, SPEC)[0] is False
    neg = EM.cost_model_evidence(EM.evaluate_chain(overlay(rate="0.00001")), spec=SPEC, data_sha256=INPUT_SHA)        # gross 3 bps -> deeply negative, fully known
    assert neg.result == SV.FAIL and SV.counted(neg, SPEC) == (True, None)
    assumed = EM.cost_model_evidence(EM.evaluate_chain(overlay(fee=tier(source="assumed", observed=None))), spec=SPEC, data_sha256=INPUT_SHA)
    assert assumed.result == SV.INCONCLUSIVE and assumed.metrics["unknown_cost_inputs"] == 1   # strict: ASSUMED is not evidence


def test_capacity_evidence():
    base = dict(impact_bps=q("0"), basis_cost_bps=q("0"), opportunity_cost_apr=q("0", "measured"))
    cap = EM.capacity_usd(overlay(**base), "12.5")
    sha = INPUT_SHA
    ok = EM.capacity_evidence(cap, floor_bps="12.5", min_viable_capacity_usd=100, spec=SPEC, data_sha256=sha)
    assert ok.result == SV.PASS and ok.metrics["capacity_usd"] == 66699.99 and SV.counted(ok, SPEC) == (True, None)
    low = EM.capacity_evidence(cap, floor_bps="12.5", min_viable_capacity_usd=70000, spec=SPEC, data_sha256=sha)
    assert low.result == SV.FAIL
    assert EM.capacity_evidence(EM.unknown("usd"), floor_bps=1, min_viable_capacity_usd=1, spec=SPEC, data_sha256=sha).result == SV.INCONCLUSIVE


# ---------------------------------------------------------------- realised
def test_realised_hand_computed():
    ts_close = 25 * H
    fills = [EM.Fill("a", "SPOT", "buy", D(1), D("50010"), D("50.01"), "USDT", 1), EM.Fill("b", "BTCUSDT", "sell", D(1), D("49990"), D("27.4945"), "USDT", 2),
             EM.Fill("c", "SPOT", "sell", D(1), D("50030"), D("50.03"), "USDT", ts_close), EM.Fill("d", "BTCUSDT", "buy", D(1), D("50020"), D("27.511"), "USDT", ts_close + 1)]
    rows = [EM.Settlement(t, "BTCUSDT", D("0.0002"), D("-1"), D("50000"), D("10")) for t in (8 * H, 16 * H, 24 * H)]
    fr = EM.reconcile_funding(perp(), rows, 0, 24 * H)
    r = EM.realised_result(fills=fills, expected_fill_ids="abcd", funding=fr, venue_net_qty={"SPOT": D(0), "BTCUSDT": D(0)}, marks={})
    # spot +20, perp -30 (sold 49990, bought back 50020) = -10 ; fees 50.01 + 27.4945 + 50.03 + 27.511 = 155.0455 ; funding +30
    assert r.pnl_usd.value == D("-135.0455") and r.pnl_usd.provenance == "derived" and r.flags == ()
    assert r.unrealised_usd.value == D("0") and r.open_inventory == {}


# ---------------------------------------------------------------- cross-checks against the repo's authorities
def test_agrees_with_carry_costs_fee_constants():
    spot_t, perp_t = D(str(CC.TAKER_BPS_SPOT)) / 10000, D(str(CC.TAKER_BPS_PERP)) / 10000
    assert EM.round_trip_fee_bps([EM.Quantity(spot_t, "venue_stated"), EM.Quantity(perp_t, "venue_stated")]).value == D("31")
    assert float(EM.round_trip_fee_bps([EM.Quantity(spot_t, "venue_stated"), EM.Quantity(perp_t, "venue_stated")]).value) == CC.ROUND_TRIP_BPS == 31.0
    # overlay: two perp legs, 2 * 5.5 = 11 ; the chain's fee component is the same number
    assert float(EM.round_trip_fee_bps([EM.Quantity(perp_t, "venue_stated")]).value) == 2 * CC.TAKER_BPS_PERP == 11.0
    assert float(EM.evaluate_chain(overlay()).components["fees"].value) == 11.0
    two = EM.evaluate_chain(overlay(legs=(EM.Leg("spot", spot(), "buy", book(), tier(taker="0.0010")), EM.Leg("perp", perp(), "sell", book(), tier()))))
    assert float(two.components["fees"].value) == CC.ROUND_TRIP_BPS


def test_agrees_with_required_funding_1_131():
    # hand: need_net = 10/100 * 1.10 * 30/365 * 1e4 = 90.41095890... ; (11.4 + 90.41095890)/(3*30) = 1.131232876...
    got = EM.required_funding_bps_per_settlement(10, 30, "11.4", "0.10", 3)
    assert got.quantize(D("0.000001")) == D("1.131233")
    assert float(got) == pytest.approx(CP.required_bps(10.0), abs=1e-12)
    assert float(got) == pytest.approx(CT.required_funding(10.0, 30.0, 11.4, 0.10), abs=1e-12)
    assert round(float(got), 3) == 1.131
    # DISAGREEMENT (documented): the 11 bps overlay round trip (2*5.5) gives 1.1268, not 1.131; 1.131 needs the repo's 11.4 round trip.
    assert EM.required_funding_bps_per_settlement(10, 30, "11", "0.10", 3).quantize(D("0.000001")) == D("1.126788")
    assert CP.ROUND_TRIP_BPS == 11.4 != 2 * CC.TAKER_BPS_PERP


def test_agrees_with_carry_costs_breakeven_and_basis():
    assert float(EM.breakeven_funding_bps(30, 11, 3)) == pytest.approx(CC.breakeven_funding_bps_per_settlement(30, round_trip_bps=11, borrow_bps_per_day=3), abs=1e-12)
    assert EM.breakeven_funding_bps(30, 11, 3) == D(11) / 30 + 1               # hand: 0.36666.. + 3/3
    assert float(EM.basis_bps(50100, 50000)) == pytest.approx(CC.basis_bps(50100.0, 50000.0), abs=1e-9)
    assert EM.basis_bps(50100, 50000) == D("20")
