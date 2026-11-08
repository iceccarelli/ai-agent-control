"""Mutation-style fault suite: for each fault a deliberately wrong input is fed in and the model must refuse/flag it;
the POSITIVE CONTROL (the correct input) must be accepted, so a model that refuses everything also fails.
Hand-computed expectations only. Fixtures come from test_economic_model."""
from __future__ import annotations

from decimal import Decimal as D

import pytest

from test_economic_model import H, INPUT_SHA, NOW, SPEC, book, overlay, perp, q, spot, tier  # noqa: F401,E402  (first: it sets sys.path / the mutation override)
import economic_model as EM  # noqa: E402
import strategy_verdict as SV  # noqa: E402

P = perp()
SHA_IN = INPUT_SHA


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
    d10 = D(10)                                                                      # 10 days = 30 settlements
    assert EM.carry_gross_edge(P, q("0.0001"), hold_days=d10).value == D("30")
    assert EM.carry_gross_edge(P, q("0.001"), hold_days=d10).value == D("300")       # still inside the cap: accepted (the cap IS the screen)
    assert EM.carry_gross_edge(P, q("0.01"), hold_days=d10).value is None            # a 10x slip above the cap is unknown, not 300 bps


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
    assert fresh.components["fees"].known and EM.cost_model_evidence(fresh, spec=SPEC, data_sha256=SHA_IN).result == SV.PASS
    stale = EM.evaluate_chain(overlay(fee=tier(observed=NOW - 25 * H)))              # 25h old against a 24h limit
    assert stale.components["fees"].value is None and stale.net_edge_bps.value is None
    ev = EM.cost_model_evidence(stale, spec=SPEC, data_sha256=SHA_IN)
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
    assert unk.net_edge_bps.value is None and EM.cost_model_evidence(unk, spec=SPEC, data_sha256=SHA_IN).result == SV.INCONCLUSIVE


# 10. stale market data
def test_stale_market_data():
    assert EM.evaluate_chain(overlay()).executable
    st = EM.evaluate_chain(overlay(bk=book(NOW - 5001)))
    assert not st.executable and any("STALE_BOOK" in b for b in st.blockers) and st.stages["EXECUTABLE"].value is None
    assert EM.cost_model_evidence(st, spec=SPEC, data_sha256=SHA_IN).result == SV.INCONCLUSIVE
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
    full = EM.realised_result(fills=[EM.Fill("a", "BTCUSDT", "sell", D(2), D("50000"), D(0), "USDT", 1), EM.Fill("b", "BTCUSDT", "buy", D(2), D("49995"), D(0), "USDT", 25 * H)],
                              expected_fill_ids="ab", funding=recon(rows), venue_net_qty={"BTCUSDT": D(0)}, marks={})
    assert full.pnl_usd.value == D("40") and full.flags == ()                                    # control: realised 10 (short 2 sold 50000, bought 49995) + funding 30


# 12. unpriced inventory
def test_unpriced_inventory():
    f = [fill("a")]
    r = realised(f, "a", {"S": D(1)})
    assert "UNPRICED_INVENTORY:S" in r.flags and r.pnl_usd.value is None
    ok = realised(f, "a", {"S": D(1)}, marks={"S": D("50020")})                                   # control: 1*50020 - 50000, but UNREALISED
    assert ok.pnl_usd.value == D("0") and ok.unrealised_usd.value == D("20") and ok.flags == ()
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
        ev = EM.cost_model_evidence(EM.evaluate_chain(overlay(**{name: EM.unknown(unit)})), spec=SPEC, data_sha256=SHA_IN)
        assert ev.result == SV.INCONCLUSIVE and ev.metrics["unknown_cost_inputs"] >= 1, name
        assert SV.counted(ev, SPEC)[0] is False
    assert q("1", "modelled") != q("1", "measured")
    assert not EM.audit_reported_profit("1", {"x": q("1", "modelled")}).status == "CONFIRMED"


# ---------------------------------------------------------------- red-team round: audit, evidence binding, numeric entry, maker legs


def test_audit_with_no_components_is_never_confirmed():
    a = EM.audit_reported_profit("0", {})
    assert a.status == "INCOMPLETE_NO_COMPONENTS" and a.status != "CONFIRMED"
    assert EM.audit_reported_profit("1000000", {}).status != "CONFIRMED"


@pytest.mark.parametrize("tol", ["1e9", "1", "0.06", "-0.01", "nan", "Infinity", 1.0])
def test_audit_tolerance_is_bounded(tol):
    with pytest.raises(EM.EconomicModelError):
        EM.audit_reported_profit("1000000", {"x": q("1", "measured")}, tolerance=tol)


def test_audit_tolerance_inside_bound_still_works():
    assert EM.audit_reported_profit("1.04", {"x": q("1", "measured")}, tolerance=EM.MAX_AUDIT_TOLERANCE).status == "CONFIRMED"
    assert EM.audit_reported_profit("1.06", {"x": q("1", "measured")}, tolerance=EM.MAX_AUDIT_TOLERANCE).status == "MISMATCH"
    assert EM.audit_reported_profit("1", {"x": q("1", "measured")}, tolerance="0").status == "CONFIRMED"


def test_audit_refuses_non_quantity_components():
    with pytest.raises(EM.EconomicModelError):
        EM.audit_reported_profit("1", {"x": D("1")})


def test_assumed_inputs_never_pass_and_there_is_no_switch():
    ch = EM.evaluate_chain(overlay(fee=tier(source="assumed", observed=None)))
    ev = EM.cost_model_evidence(ch, spec=SPEC, data_sha256=SHA_IN)
    assert ev.result == SV.INCONCLUSIVE and ev.metrics["net_edge_bps_after_costs"] is not None       # the number is visible, the PASS is not
    with pytest.raises(TypeError):
        EM.cost_model_evidence(ch, spec=SPEC, data_sha256=SHA_IN, assumed_counts_as_unknown=False)
    with pytest.raises(TypeError):
        EM.capacity_evidence(q("100000", "assumed", "usd"), floor_bps=1, min_viable_capacity_usd=1, spec=SPEC, data_sha256=SHA_IN, assumed_counts_as_unknown=False)
    assert EM.capacity_evidence(q("100000", "assumed", "usd"), floor_bps=1, min_viable_capacity_usd=1, spec=SPEC, data_sha256=SHA_IN).result == SV.INCONCLUSIVE
    ok = EM.cost_model_evidence(EM.evaluate_chain(overlay()), spec=SPEC, data_sha256=SHA_IN)
    assert ok.result == SV.PASS                                                                   # positive control


@pytest.mark.parametrize("bad", [None, "", "xyz", "AB" * 32, "ab" * 31, 5])
def test_evidence_requires_an_explicit_real_input_hash(bad):
    ch = EM.evaluate_chain(overlay())
    with pytest.raises(EM.EconomicModelError):
        EM.cost_model_evidence(ch, spec=SPEC, data_sha256=bad)
    with pytest.raises(EM.EconomicModelError):
        EM.capacity_evidence(q("1000", "modelled", "usd"), floor_bps=1, min_viable_capacity_usd=1, spec=SPEC, data_sha256=bad)


def test_evidence_cannot_certify_itself_with_the_scenarios_own_digest():
    ch = EM.evaluate_chain(overlay())
    with pytest.raises(EM.EconomicModelError):
        EM.cost_model_evidence(ch, spec=SPEC)                                                                 # absent -> refused, not defaulted
    with pytest.raises(EM.EconomicModelError):
        EM.cost_model_evidence(ch, spec=SPEC, data_sha256=ch.digest)                                          # self-certification
    assert EM.cost_model_evidence(ch, spec=SPEC, data_sha256=SHA_IN).data_sha256 == SHA_IN


@pytest.mark.parametrize("bad", ["1_0", "1_000", " 1", "1 ", "1 0", "\t1", "١٢٣", "１２", "1e99", "1e-99", "1E+400", "0x10", "1,5", "", "+-1", ".", "1e", "1e1_0",
                                 "NaN", "Infinity", "-Infinity", "sNaN"])
def test_to_decimal_refuses_noncanonical_numbers(bad):
    with pytest.raises(EM.EconomicModelError):
        EM.to_decimal(bad)


@pytest.mark.parametrize("good,exp", [("10", D(10)), ("-0.5", D("-0.5")), ("+1.25", D("1.25")), ("1e-8", D("1e-8")), ("2.5E3", D("2500")), (".5", D("0.5")),
                                      ("5.", D(5)), (7, D(7)), (D("0.0001"), D("0.0001"))])
def test_to_decimal_accepts_plain_numbers(good, exp):
    assert EM.to_decimal(good) == exp


def test_to_decimal_refuses_oversized_decimal_and_other_types():
    for bad in (D("1e100"), D("1e-100"), b"1", [1], None, 10 ** 40):
        with pytest.raises(EM.EconomicModelError):
            EM.to_decimal(bad)


def test_maker_leg_has_no_derived_zero_cost():
    legs = (EM.Leg("perp", perp(), "sell", book(), tier(maker="-0.0001"), role="maker"),)
    # no fill probability stated -> spread/slippage are UNKNOWN, not a derived zero; the chain cannot produce a PASS
    r = EM.evaluate_chain(overlay(legs=legs, adverse_selection_bps=q("3")))
    for k in ("spread", "slippage", "maker_fill"):
        assert not r.components[k].known, k
    assert EM.cost_model_evidence(r, spec=SPEC, data_sha256=SHA_IN).result == SV.INCONCLUSIVE
    # with adverse selection unknown the chain is unknown too
    r2 = EM.evaluate_chain(overlay(legs=legs, maker_fill_probability=q("0.8")))
    assert r2.net_edge_bps.value is None
    # both stated: the fill-probability cost is (1 - p) of the gross edge, hand computed 30 * 0.2 = 6
    r3 = EM.evaluate_chain(overlay(legs=legs, adverse_selection_bps=q("3"), maker_fill_probability=q("0.8")))
    assert r3.components["maker_fill"].value == D("6.0") and r3.components["spread"].value == 0 and r3.components["spread"].provenance != "derived"
    assert r3.net_edge_bps.known and EM.cost_model_evidence(r3, spec=SPEC, data_sha256=SHA_IN).result in (SV.PASS, SV.FAIL)
    # a stated probability outside (0, 1] is refused
    for p in ("0", "1.01", "-0.1"):
        with pytest.raises(EM.EconomicModelError):
            EM.evaluate_chain(overlay(legs=legs, adverse_selection_bps=q("3"), maker_fill_probability=q(p)))


def test_taker_chain_is_unaffected_by_maker_fill_probability_default():
    r = EM.evaluate_chain(overlay())
    assert r.components["maker_fill"].value == 0 and r.net_edge_bps.known


# ================================================================== red-team round 2 (economic model)
# Every test pairs a POSITIVE CONTROL (the correct input is accepted) with the fault, so a model that refuses everything also fails.
def _gross(rate, prov, hold_days):
    return EM.carry_gross_edge(P, q(rate, prov), hold_days=D(hold_days))


# ---- A) assumed gross edge / hedge ratio can never PASS
def test_assumed_funding_rate_in_gross_edge_is_inconclusive_never_pass():
    pos = EM.cost_model_evidence(EM.evaluate_chain(overlay()), spec=SPEC, data_sha256=SHA_IN)
    assert pos.result == SV.PASS                                                      # control: a modelled rate may PASS
    ch = EM.evaluate_chain(overlay(gross_edge_bps=_gross("0.0001", "assumed", D(30) * 8 / 24)))
    assert ch.net_edge_bps.value > 0 and "gross_edge" in ch.assumed_inputs
    ev = EM.cost_model_evidence(ch, spec=SPEC, data_sha256=SHA_IN)
    assert ev.result == SV.INCONCLUSIVE and "gross_edge" in ev.metrics["assumed_names"] and ev.metrics["net_edge_bps_after_costs"] is not None


def test_assumed_hedge_ratio_is_inconclusive_never_pass():
    ch = EM.evaluate_chain(overlay(hedge_ratio=q("1", "assumed", "ratio"), adverse_move_bps=q("0", "measured", "bps")))
    assert "hedge_ratio" in ch.assumed_inputs
    assert EM.cost_model_evidence(ch, spec=SPEC, data_sha256=SHA_IN).result == SV.INCONCLUSIVE
    ok = EM.evaluate_chain(overlay(hedge_ratio=q("1", "measured", "ratio"), adverse_move_bps=q("0", "measured", "bps")))
    assert "hedge_ratio" not in ok.assumed_inputs and EM.cost_model_evidence(ok, spec=SPEC, data_sha256=SHA_IN).result == SV.PASS


# ---- B) every cost component known-or-unknown (never silently negative); hold_days > 0
@pytest.mark.parametrize("name,unit", [("impact_bps", "bps"), ("basis_cost_bps", "bps"), ("borrow_apr", "fraction"), ("opportunity_cost_apr", "fraction"),
                                       ("adverse_move_bps", "bps"), ("adverse_selection_bps", "bps")])
def test_negative_cost_inputs_are_refused(name, unit):
    assert EM.evaluate_chain(overlay(**{name: q("0", "measured", unit)})).net_edge_bps.known          # control: a measured zero is a real cost
    with pytest.raises(EM.EconomicModelError):
        EM.evaluate_chain(overlay(**{name: q("-300", "measured", unit)}))


@pytest.mark.parametrize("hd", ["0", "-3650", "-1"])
def test_hold_days_must_be_positive(hd):
    with pytest.raises(EM.EconomicModelError):
        EM.evaluate_chain(overlay(hold_days=D(hd), gross_edge_bps=q("30", "modelled", "bps"), borrow_apr=q("0.1", "measured", "fraction")))
    assert EM.evaluate_chain(overlay(hold_days=D("0.5"), gross_edge_bps=q("30", "modelled", "bps"))).net_edge_bps.known


def test_hedge_ratio_cannot_be_negative():
    with pytest.raises(EM.EconomicModelError):
        EM.evaluate_chain(overlay(hedge_ratio=q("-1", "measured", "ratio")))


# ---- C) settlements derive from hold_days * settlements_per_day
def test_funding_edge_settlements_are_derived_from_hold_days():
    g = _gross("0.0001", "modelled", "1")                       # 1 day on an 8h perp = 3 settlements = 3 bps
    assert g.value == D("3") and g.hold_days == D("1")
    assert _gross("0.0001", "modelled", "10").value == D("30")
    assert EM.carry_gross_edge(perp(funding_interval_hours=4), q("0.0001"), hold_days=D("1")).value == D("6")
    with pytest.raises(EM.EconomicModelError):
        _gross("0.0001", "modelled", "0")
    with pytest.raises(EM.EconomicModelError):
        _gross("0.0001", "modelled", "1.5")                     # 4.5 settlements: not a whole number of settlements held
    with pytest.raises(TypeError):
        EM.carry_gross_edge(P, q("0.0001"), 24)                 # the old free n_settlements is gone


def test_eightfold_settlement_overstatement_is_refused():
    ok = EM.evaluate_chain(overlay(hold_days=D(1), gross_edge_bps=_gross("0.0003", "modelled", "1")))
    assert ok.components["borrow"].known                                                  # control: consistent 1 day
    eight_x = _gross("0.0003", "modelled", "8")                 # 24 settlements of an 8h perp = 8 days of funding ...
    with pytest.raises(EM.EconomicModelError):
        EM.evaluate_chain(overlay(hold_days=D(1), gross_edge_bps=eight_x))               # ... claimed for a 1-day hold


# ---- D) zero-rate funding with cash
def test_zero_rate_funding_with_cash_is_flagged():
    assert recon(three(rate="0", cash="0")).flags == ()                                   # control
    assert recon(three(rate="0", cash="0.001")).flags == ()                               # dust
    bad = recon(three(rate="0", cash="-1000"))
    assert "FUNDING_CASH_AT_ZERO_RATE" in bad.flags and bad.total.value is None
    assert "FUNDING_CASH_AT_ZERO_RATE" in recon(three(rate="0", cash="1000")).flags


# ---- magnitude bracket: a systematic one-sided bias inside the per-row x2 bracket
def test_systematic_one_sided_funding_bias_is_flagged():
    assert recon(three(cash="10")).flags == ()                                            # control: exact
    assert recon(three(cash="10.2")).flags == ()                                          # 2% rounding-scale deviation is accepted
    for cash in ("15", "6"):                                                              # every row 50% high / 40% low: inside x2, but systematic
        r = recon(three(cash=cash))
        assert "FUNDING_SYSTEMATIC_BIAS" in r.flags and r.total.value is None, cash
    mixed = [settle(8 * H, cash="15"), settle(16 * H, cash="6"), settle(24 * H, cash="10")]    # deviations of both signs average out near 10
    assert "FUNDING_SYSTEMATIC_BIAS" not in recon(mixed).flags


# ---- G) fills
def test_fill_construction_is_validated():
    assert fill("a").qty == D(1)                                                         # control
    for kw in ({"side": "hold"}, {"side": "BUY"}, {"qty": "0"}, {"qty": "-1"}, {"px": "0"}, {"px": "-5"}):
        with pytest.raises(EM.EconomicModelError):
            fill("a", **kw)
    with pytest.raises(EM.EconomicModelError):
        EM.Fill("a", "S", "buy", 1.0, D("1"), D("0"), "USDT", 1)                         # float qty


def test_fee_currency_is_quote_or_base_only():
    f = [fill("a", fee="1", ccy="MNT")]
    r = realised(f, "a", {"S": D(1)}, marks={"S": D("50000")})
    assert "FEE_CCY_UNSUPPORTED:S:MNT" in r.flags and r.pnl_usd.value is None            # never multiply a foreign-currency fee by price
    assert realised([fill("a", fee="1", ccy="USDT")], "a", {"S": D(1)}, marks={"S": D("50000")}).pnl_usd.value == D("-1")     # control: quote coin
    base = EM.realised_result(fills=[fill("a", fee="0.001", ccy="BTC")], expected_fill_ids="a", funding=EM.reconcile_funding(P, [], 0, 0), venue_net_qty={"S": D(1)},
                              marks={"S": D("50000")}, base_coins={"S": "BTC"})
    assert base.flags == () and base.components["fees"].value == D("50")                   # 0.001 BTC at 50000
    nob = EM.realised_result(fills=[fill("a", fee="0.001", ccy="BTC")], expected_fill_ids="a", funding=EM.reconcile_funding(P, [], 0, 0), venue_net_qty={"S": D(1)},
                             marks={"S": D("50000")})
    assert "FEE_CCY_UNSUPPORTED:S:BTC" in nob.flags                                        # base coin unknown for the symbol: refused, not guessed


# ---- E) realised vs unrealised
def _flat_funding():
    return EM.reconcile_funding(P, [], 0, 0)


def test_unrealised_mark_to_market_is_not_realised_profit():
    f = [fill("a", sym="BTCUSDT")]
    r = EM.realised_result(fills=f, expected_fill_ids="a", funding=_flat_funding(), venue_net_qty={"BTCUSDT": D(1)}, marks={"BTCUSDT": D("60000")})
    assert r.pnl_usd.value == D("0")                                                    # nothing was realised
    assert r.unrealised_usd.value == D("10000") and r.open_inventory == {"BTCUSDT": D(1)}
    closed = EM.realised_result(fills=f + [fill("b", side="sell", sym="BTCUSDT", px="60000")], expected_fill_ids="ab", funding=_flat_funding(),
                                venue_net_qty={"BTCUSDT": D(0)}, marks={})
    assert closed.pnl_usd.value == D("10000") and closed.unrealised_usd.value == D("0")   # control: closed -> realised


def test_marks_carry_a_timestamp_and_provenance():
    f = [fill("a", sym="BTCUSDT")]
    kw = dict(fills=f, expected_fill_ids="a", funding=_flat_funding(), venue_net_qty={"BTCUSDT": D(1)})
    bare = EM.realised_result(marks={"BTCUSDT": D("60000")}, **kw)
    assert bare.unrealised_usd.provenance == "assumed"                                    # an unstamped mark is not 'measured'
    fresh = EM.realised_result(marks={"BTCUSDT": EM.Mark(D("60000"), 1_000_000, "venue_mark")}, as_of_ms=1_010_000, **kw)
    assert fresh.unrealised_usd.provenance == "measured" and fresh.unrealised_usd.value == D("10000")
    stale = EM.realised_result(marks={"BTCUSDT": EM.Mark(D("60000"), 1_000_000, "venue_mark")}, as_of_ms=1_000_000 + EM.MAX_MARK_AGE_MS + 1, **kw)
    assert "STALE_MARK:BTCUSDT" in stale.flags and stale.pnl_usd.value is None
    future = EM.realised_result(marks={"BTCUSDT": EM.Mark(D("60000"), 2_000_000, "venue_mark")}, as_of_ms=1_000_000, **kw)
    assert "STALE_MARK:BTCUSDT" in future.flags
    with pytest.raises(EM.EconomicModelError):
        EM.Mark(D("0"), 1, "venue_mark")
    with pytest.raises(EM.EconomicModelError):
        EM.Mark(D("1"), 1, "")


def test_hedged_claim_requires_a_neutral_hedge_check():
    f = [fill("a", sym="BTCUSDT")]
    kw = dict(fills=f, expected_fill_ids="a", funding=_flat_funding(), venue_net_qty={"BTCUSDT": D(1)}, marks={"BTCUSDT": D("50000")}, claim_hedged=True)
    assert "HEDGE_CHECK_MISSING" in EM.realised_result(**kw).flags and EM.realised_result(**kw).pnl_usd.value is None
    assert "HEDGE_NOT_NEUTRAL" in EM.realised_result(hedge=EM.hedge_check(D("1"), D("0.5")), **kw).flags
    ok = EM.realised_result(hedge=EM.hedge_check(D("1"), D("1")), **kw)
    assert ok.flags == () and ok.pnl_usd.known


# ---- F) funding rows must be backed by fills / a consistent position
def _perp_funding(qty="-1", n=3):
    rows = [settle(t, qty=qty, cash=str(-D(qty) * 5)) for t in (8 * H, 16 * H, 24 * H)][:n]
    return EM.reconcile_funding(P, rows, 0, n * 8 * H)


def test_funding_without_any_fills_is_not_a_profit():
    fr = _perp_funding()
    assert fr.total.value == D("15") and fr.flags == ()
    r = EM.realised_result(fills=[], expected_fill_ids=[], funding=fr, venue_net_qty={}, marks={})
    assert "FUNDING_WITHOUT_FILLS" in r.flags and r.pnl_usd.value is None


def test_funding_position_must_match_the_fill_derived_position():
    open_short = [EM.Fill("s", "BTCUSDT", "sell", D(1), D("50000"), D(0), "USDT", 1)]
    fr = _perp_funding("-1")
    good = EM.realised_result(fills=open_short, expected_fill_ids="s", funding=fr, venue_net_qty={"BTCUSDT": D(-1)}, marks={"BTCUSDT": D("50000")})
    assert good.flags == () and good.pnl_usd.value == D("15")                              # control
    big = EM.realised_result(fills=open_short, expected_fill_ids="s", funding=_perp_funding("-5"), venue_net_qty={"BTCUSDT": D(-1)}, marks={"BTCUSDT": D("50000")})
    assert "FUNDING_POSITION_MISMATCH:BTCUSDT" in big.flags and big.pnl_usd.value is None   # funding paid on 5 BTC, fills show 1
    late = [EM.Fill("s", "BTCUSDT", "sell", D(1), D("50000"), D(0), "USDT", 20 * H)]       # position opened after the first two settlements
    r = EM.realised_result(fills=late, expected_fill_ids="s", funding=fr, venue_net_qty={"BTCUSDT": D(-1)}, marks={"BTCUSDT": D("50000")})
    assert "FUNDING_POSITION_MISMATCH:BTCUSDT" in r.flags
    manual = EM.FundingRecon(EM.Quantity(D("15"), "measured", "usd"), (), (), 3)           # a hand-built recon carries no positions to check
    r2 = EM.realised_result(fills=open_short, expected_fill_ids="s", funding=manual, venue_net_qty={"BTCUSDT": D(-1)}, marks={"BTCUSDT": D("50000")})
    assert "FUNDING_POSITIONS_UNVERIFIED" in r2.flags and r2.pnl_usd.value is None
