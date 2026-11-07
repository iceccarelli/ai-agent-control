"""Mutation-style fault suite: for each fault a deliberately wrong input is fed in and the model must refuse/flag it;
the POSITIVE CONTROL (the correct input) must be accepted, so a model that refuses everything also fails.
Hand-computed expectations only. Fixtures come from test_economic_model."""
from __future__ import annotations

from decimal import Decimal as D

import pytest

from test_economic_model import H, NOW, SPEC, book, overlay, perp, q, spot, tier  # noqa: F401,E402  (first: it sets sys.path / the mutation override)
import economic_model as EM  # noqa: E402
import strategy_verdict as SV  # noqa: E402

P = perp()


def settle(ts, rate="0.0001", qty="-2", mark="50000", cash="10", symbol="BTCUSDT"):
    return EM.Settlement(ts, symbol, D(rate), D(qty), None if mark is None else D(mark), D(cash))


def three(**kw):
    return [settle(t, **kw) for t in (8 * H, 16 * H, 24 * H)]


def recon(rows, inst=P):
    return EM.reconcile_funding(inst, rows, 0, 24 * H)


def fill(i, side="buy", sym="S", qty="1", px="50000", fee="0", ccy="USDT"):
    return EM.Fill(i, sym, side, D(qty), D(px), D(fee), ccy, 1)


def realised(fills, ids, venue, marks=None, funding=None, export=None):
    f = funding or EM.FundingRecon(EM.Quantity(D(0), "measured", "usd"), (), (), 0)
    return EM.realised_result(fills=fills, expected_fill_ids=ids, funding=f, venue_net_qty=venue, marks=marks or {}, export=export)


# 1. wrong funding sign
def test_wrong_funding_sign():
    assert recon(three()).total.value == D("30")                                   # control: short + positive rate receives
    bad = recon(three(cash="-10"))                                                  # same rows booked inverted
    assert "FUNDING_SIGN_CONTRADICTION" in bad.flags and bad.total.value is None
    assert "FUNDING_SIGN_CONTRADICTION" in recon(three(qty="2")).flags              # a long booked as receiving


# 2. wrong funding interval
def test_wrong_funding_interval():
    assert recon(three()).flags == ()                                               # control: 8h rows, 8h instrument
    four = perp(funding_interval_hours=4)
    assert "MISSING_SETTLEMENT" in recon(three(), four).flags                       # 8h rows against a 4h instrument: half are missing
    rows4 = [settle(t) for t in range(4 * H, 24 * H + 1, 4 * H)]
    assert "SETTLEMENT_OFF_INTERVAL_GRID" in recon(rows4, P).flags                  # 4h rows against an 8h instrument
    assert recon(rows4, four).flags == ()                                           # control: 4h rows, 4h instrument
    base = {"category": "linear", "symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT", "status": "Trading", "upperFundingRate": "0.0075",
            "lowerFundingRate": "-0.0075", "priceFilter": {"tickSize": "0.1"}, "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"}}
    for minutes in (90, 30, 0, 600):
        with pytest.raises(EM.EconomicModelError):
            EM.Instrument.from_venue_row(dict(base, fundingInterval=minutes), expect_symbol="BTCUSDT", expect_category="linear")
    with pytest.raises(EM.EconomicModelError):
        EM.Instrument.from_venue_row(base, expect_symbol="BTCUSDT", expect_category="linear")     # interval absent: never assumed 8h


# 3. 10x funding
def test_ten_x_funding():
    assert recon(three()).flags == ()
    assert "FUNDING_MAGNITUDE_CONTRADICTION" in recon(three(cash="100")).flags       # rate*qty*mark = 10, booked 100
    assert "FUNDING_MAGNITUDE_CONTRADICTION" in recon(three(cash="1")).flags         # and a /10 slip
    assert "RATE_OUTSIDE_VENUE_BOUNDS" in recon(three(rate="0.075", cash="100")).flags   # 10x the 0.0075 cap
    assert EM.carry_gross_edge(P, q("0.0001"), 30).value == D("30")
    assert EM.carry_gross_edge(P, q("0.001"), 30).value == D("300")                  # still inside the cap: accepted (the cap IS the screen)
    assert EM.carry_gross_edge(P, q("0.01"), 30).value is None                       # a 10x slip above the cap is unknown, not 300 bps


# 4. wrong fee unit
def test_wrong_fee_unit():
    assert EM.fee_fraction("5.5", "bps") == D("0.00055")
    for v, u in (("5.5", "fraction"), ("0.055", "fraction"), ("5.5", "percent"), ("0.055", "bps"), ("0.00055", "bps")):
        with pytest.raises(EM.EconomicModelError):
            EM.fee_fraction(v, u)
    with pytest.raises(EM.EconomicModelError):
        EM.FeeTier(maker=D("5.5"), taker=D("5.5"), source="venue_stated", observed_ms=NOW)
    with pytest.raises(EM.EconomicModelError):
        EM.fee_fraction("5.5", "pct")


# 5. wrong symbol row
def test_wrong_symbol_row():
    row = {"category": "linear", "symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT", "status": "Trading", "fundingInterval": 480, "upperFundingRate": "0.0075",
           "lowerFundingRate": "-0.0075", "priceFilter": {"tickSize": "0.1"}, "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"}}
    assert EM.select_instrument([row], "BTCUSDT", "linear").symbol == "BTCUSDT"
    eth = dict(row, symbol="ETHUSDT", baseCoin="ETH")
    with pytest.raises(EM.EconomicModelError):
        EM.select_instrument([eth], "BTCUSDT", "linear")                             # no such row
    with pytest.raises(EM.EconomicModelError):
        EM.select_instrument([row, row], "BTCUSDT", "linear")                        # ambiguous
    with pytest.raises(EM.EconomicModelError):
        EM.Instrument.from_venue_row(eth, expect_symbol="BTCUSDT", expect_category="linear")
    with pytest.raises(EM.EconomicModelError):
        EM.select_instrument([row], "BTCUSDT", "spot")                               # a linear row is not the spot instrument
    with pytest.raises(EM.EconomicModelError):
        EM.select_instrument([{k: v for k, v in row.items() if k != "category"}], "BTCUSDT", "linear")   # a row that does not say what it is
    assert "WRONG_SYMBOL_ROW" in recon(three(symbol="ETHUSDT")).flags               # a funding row for another symbol


# 6. stale fee tier
def test_stale_fee_tier():
    fresh = EM.evaluate_chain(overlay())
    assert fresh.components["fees"].known and EM.cost_model_evidence(fresh).result == SV.PASS
    stale = EM.evaluate_chain(overlay(fee=tier(observed=NOW - 25 * H)))              # 25h old against a 24h limit
    assert stale.components["fees"].value is None and stale.net_edge_bps.value is None
    ev = EM.cost_model_evidence(stale)
    assert ev.result == SV.INCONCLUSIVE and SV.counted(ev, SPEC)[0] is False
    assert EM.evaluate_chain(overlay(fee=tier(observed=NOW + H))).components["fees"].value is None   # future-dated: also not current


# 7. duplicate fill
def test_duplicate_fill():
    ok = [fill("a"), fill("b", side="sell", px="50010")]
    r = realised(ok, "ab", {"S": D(0)})
    assert r.pnl_usd.value == D("10")                                                # -50000 + 50010
    bad = realised(ok + [fill("a")], "ab", {"S": D(1)})
    assert "DUPLICATE_FILL" in bad.flags and bad.pnl_usd.value is None


# 8. missing fill
def test_missing_fill():
    r = realised([fill("a")], "ab", {"S": D(1)}, marks={"S": D("50000")})
    assert "MISSING_FILL" in r.flags and r.pnl_usd.value is None
    assert EM.reconcile_fills([fill("a"), fill("b")], "ab").ok


# 9. partial hedge
def test_partial_hedge():
    assert EM.hedge_check(D("1"), D("1")).neutral
    h = EM.hedge_check(D("0.6"), D("1"))
    assert not h.neutral and h.ratio == D("0.6") and h.residual_qty == D("-0.4")
    held = EM.held_qty_after_fee(D("1.000"), D("0.001"), True)                       # base-coin fee: 0.999 held vs a 1.000 short
    assert not EM.hedge_check(held, D("1.000"), "0.0001").neutral and EM.hedge_check(held, D("1.000"), "0.01").neutral
    assert EM.evaluate_chain(overlay(hedge_ratio=q("0.6", "measured"))).net_edge_bps.value == D("-38.5")     # 1.5 - 0.4*100
    unk = EM.evaluate_chain(overlay(hedge_ratio=EM.unknown("ratio")))                # an unmeasured hedge is unknown, not perfect
    assert unk.net_edge_bps.value is None and EM.cost_model_evidence(unk).result == SV.INCONCLUSIVE


# 10. stale market data
def test_stale_market_data():
    assert EM.evaluate_chain(overlay()).executable
    st = EM.evaluate_chain(overlay(bk=book(NOW - 5001)))
    assert not st.executable and any("STALE_BOOK" in b for b in st.blockers) and st.stages["EXECUTABLE"].value is None
    assert EM.cost_model_evidence(st).result == SV.INCONCLUSIVE
    thin = EM.evaluate_chain(overlay(notional="200000"))                             # 4 BTC against 2 BTC of depth
    assert not thin.executable and any("BOOK_TOO_THIN" in b for b in thin.blockers)
    assert EM.capacity_usd(overlay(bk=book(NOW - 5001)), "5").value is None or EM.capacity_usd(overlay(bk=book(NOW - 5001)), "5").value == 0


# 11. missing settlement
def test_missing_settlement():
    rows = three()
    r = recon(rows[:2])
    assert "MISSING_SETTLEMENT" in r.flags and r.missing_ts == (24 * H,) and r.total.value is None
    fills = [fill("a"), fill("b", side="sell", px="50010")]
    assert realised(fills, "ab", {"S": D(0)}, funding=r).pnl_usd.value is None
    assert realised(fills, "ab", {"S": D(0)}, funding=recon(rows)).pnl_usd.value == D("40")      # control: 10 + 30


# 12. unpriced inventory
def test_unpriced_inventory():
    f = [fill("a")]
    r = realised(f, "a", {"S": D(1)})
    assert "UNPRICED_INVENTORY:S" in r.flags and r.pnl_usd.value is None
    assert realised(f, "a", {"S": D(1)}, marks={"S": D("50020")}).pnl_usd.value == D("20")       # control: 1*50020 - 50000
    assert "VENUE_POSITION_READ_MISSING:S" in realised(f, "a", {"S": None}, marks={"S": D("50020")}).flags
    assert "INVENTORY_MISMATCH:S" in realised(f, "a", {"S": D(0)}, marks={"S": D("50020")}).flags  # FLAT is the venue's read, not fill arithmetic


# 13. phantom profit
def test_phantom_profit():
    comps = {"trade": q("-10", "measured"), "fees": q("-155.0455", "measured"), "funding": q("30", "measured")}
    assert EM.audit_reported_profit("-135.0455", comps).status == "CONFIRMED"
    ph = EM.audit_reported_profit("500", dict(comps, funding=EM.unknown("usd")))
    assert ph.status == "PHANTOM_UNKNOWN_COMPONENTS" and ph.detail == ("funding",)
    assert EM.audit_reported_profit("-135.0455", dict(comps, funding=q("30", "modelled"))).status == "UNOBSERVED_COMPONENTS"
    assert EM.audit_reported_profit("-100", comps).status == "MISMATCH"
    # an unknown can never be summed into a number
    assert EM.qsum(list(dict(comps, funding=EM.unknown("usd")).values())).value is None


# 14. incomplete owner export
def test_incomplete_owner_export():
    good = {"fills": [1], "settlements": [1], "fills_declared_rows": 1, "truncated": False, "covers_from_ms": 0, "covers_to_ms": 24 * H, "sha256": "cd" * 32}
    assert EM.check_owner_export(good, 0, 24 * H).complete
    cases = {"SECTION_MISSING:settlements": dict(good, settlements=None), "ROW_COUNT_MISMATCH:fills": dict(good, fills_declared_rows=2),
             "TRUNCATION_NOT_RULED_OUT": {k: v for k, v in good.items() if k != "truncated"}, "WINDOW_NOT_COVERED": dict(good, covers_to_ms=23 * H),
             "NO_EXPORT_HASH": dict(good, sha256="xyz")}
    for flag, exp in cases.items():
        c = EM.check_owner_export(exp, 0, 24 * H)
        assert not c.complete and flag in c.problems, flag
    inc = EM.check_owner_export(cases["WINDOW_NOT_COVERED"], 0, 24 * H)
    assert realised([fill("a")], "a", {"S": D(0)}, export=inc).pnl_usd.value is None
    assert "OWNER_EXPORT_INCOMPLETE" in realised([fill("a")], "a", {"S": D(0)}, export=inc).flags


# cross-cutting: an estimate can never be promoted to an observation, and unknown can never PASS
def test_unknown_can_never_pass_and_estimates_are_not_observations():
    for name in ("impact_bps", "basis_cost_bps", "borrow_apr", "opportunity_cost_apr", "adverse_move_bps"):
        unit = "fraction" if name.endswith("apr") else "bps"
        ev = EM.cost_model_evidence(EM.evaluate_chain(overlay(**{name: EM.unknown(unit)})))
        assert ev.result == SV.INCONCLUSIVE and ev.metrics["unknown_cost_inputs"] >= 1, name
        assert SV.counted(ev, SPEC)[0] is False
    assert q("1", "modelled") != q("1", "measured")
    assert not EM.audit_reported_profit("1", {"x": q("1", "modelled")}).status == "CONFIRMED"
