"""ms_accounting: unknown != zero, unreconciled != profit, duplicate fills, funding sign. Offline, pure."""
import os
import sys

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import carry_costs as CC  # noqa: E402
import ms_accounting as A  # noqa: E402
from ms_types import Fill  # noqa: E402


def fill(fid="f1", side="Sell", qty=1.0, price=100.0, fee=0.055, sym="BTCUSDT", leg="P", grp="G1", ts=1000, prov="MEASURED", liq="TAKER"):
    return Fill(fill_id=fid, group_id=grp, leg_id=leg, symbol=sym, side=side, qty=qty, price=price, fee_usd=fee, fee_ccy="USDT", liquidity=liq, ts_ms=ts, prov=prov)


def closed_pair():
    """short perp 100 -> 99 (+1), long spot 100 -> 101 (+1), fees 4 x 0.1, perp funding +0.5."""
    L = A.Ledger()
    L.record_fill(fill("a", "Sell", 1, 100, 0.1, leg="P"))
    L.record_fill(fill("b", "Buy", 1, 100, 0.1, leg="S"), category="spot")
    L.record_fill(fill("c", "Buy", 1, 99, 0.1, leg="P", ts=2000))
    L.record_fill(fill("d", "Sell", 1, 101, 0.1, leg="S", ts=2000), category="spot")
    L.record_funding("BTCUSDT", 1500, 0.5, "MEASURED")
    return L


def test_positive_control_closed_reconciled_pair_reports_a_claimable_profit():
    p = closed_pair().pnl()
    assert p["price_leg"] == pytest.approx(2.0) and p["funding"] == pytest.approx(0.5) and p["fees"] == pytest.approx(0.4)
    assert p["net"] == pytest.approx(2.1) and p["realised"] == pytest.approx(2.1) and p["unrealised"] == 0.0
    assert p["status"] == "RECONCILED" and p["claimable_profit"] == pytest.approx(2.1) and p["flags"] == []
    assert p["prov"] == "MEASURED"


def test_rollups_per_leg_per_symbol_and_portfolio_agree():
    L = closed_pair()
    L.record_fill(fill("e", "Sell", 2, 50, 0.1, sym="ETHUSDT", leg="P", grp="G2"))
    L.record_fill(fill("f", "Buy", 2, 49, 0.1, sym="ETHUSDT", leg="P", grp="G2", ts=3000))
    L.record_funding("ETHUSDT", 2500, -0.25, "MEASURED")
    p = L.pnl()
    assert p["per_symbol"]["BTCUSDT"]["net"] == pytest.approx(2.1)
    assert p["per_symbol"]["ETHUSDT"]["net"] == pytest.approx(2.0 - 0.25 - 0.2)
    assert p["net"] == pytest.approx(p["per_symbol"]["BTCUSDT"]["net"] + p["per_symbol"]["ETHUSDT"]["net"])
    assert set(p["per_leg"]) == {"G1/P", "G1/S", "G2/P"}
    assert p["per_leg"]["G1/P"]["price_leg"] == pytest.approx(1.0) and p["per_leg"]["G1/S"]["price_leg"] == pytest.approx(1.0)
    assert p["per_leg"]["G1/P"]["fees"] == pytest.approx(0.2)


def test_per_leg_never_allocates_funding_and_never_claims_a_profit():
    leg = closed_pair().pnl()["per_leg"]["G1/P"]
    assert leg["funding"] is None and leg["net"] is None and leg["claimable_profit"] is None and "FUNDING_NOT_ALLOCATED_TO_LEG" in leg["flags"]


# ---------------------------------------------------------------- funding sign
def test_funding_sign_convention_short_receives_positive_funding():
    assert A.funding_cashflow(-1000.0, 0.0001) == pytest.approx(0.1)          # short, longs pay shorts => received
    assert A.funding_cashflow(1000.0, 0.0001) == pytest.approx(-0.1)          # long pays
    assert A.funding_cashflow(-1000.0, -0.0001) == pytest.approx(-0.1)        # negative rate: short pays
    assert A.funding_cashflow(0.0, 0.01) == 0.0


def test_wrong_funding_sign_changes_the_net_and_is_detected():
    L = closed_pair()
    good = L.pnl()["net"]
    L2 = A.Ledger()
    for f, c in ((fill("a", "Sell", 1, 100, 0.1, leg="P"), "linear"), (fill("b", "Buy", 1, 100, 0.1, leg="S"), "spot"), (fill("c", "Buy", 1, 99, 0.1, leg="P", ts=2000), "linear"),
                 (fill("d", "Sell", 1, 101, 0.1, leg="S", ts=2000), "spot")):
        L2.record_fill(f, category=c)
    L2.record_funding("BTCUSDT", 1500, -0.5, "MEASURED")                       # the sign flipped
    assert L2.pnl()["net"] == pytest.approx(good - 1.0)
    assert L2.pnl()["funding"] == pytest.approx(-0.5)


def test_funding_interval_conversion_uses_carry_costs_not_an_8h_assumption():
    # 1 bps/print at 480m is 3 bps/day; the same claim at 60m is 0.125 bps/print. Booking cash is interval-free; the conversion is the carry_costs one.
    assert CC.funding_bps_per_day(1.0, 480) == pytest.approx(3.0)
    assert CC.funding_bps_per_day(0.125, 60) == pytest.approx(3.0)
    assert CC.funding_bps_per_day(1.0, 7) is None                                # does not divide 1440: unknown, never defaulted
    n = 1000.0
    per_print_480 = A.funding_cashflow(-n, 1.0 / 1e4)
    per_print_60 = A.funding_cashflow(-n, 0.125 / 1e4)
    assert per_print_480 * 3 == pytest.approx(per_print_60 * 24)


def test_funding_duplicate_print_is_ignored_and_conflict_flagged():
    L = closed_pair()
    assert L.record_funding("BTCUSDT", 1500, 0.5, "MEASURED") is False
    assert L.pnl()["funding"] == pytest.approx(0.5) and L.pnl()["status"] == "RECONCILED"
    L.record_funding("BTCUSDT", 1500, 0.9, "MEASURED")
    p = L.pnl()
    assert "FUNDING_CONFLICT:BTCUSDT:1500" in p["flags"] and p["claimable_profit"] is None


# ---------------------------------------------------------------- unknown != zero
def test_missing_funding_record_is_unknown_not_zero():
    L = A.Ledger()
    L.record_fill(fill("a", "Sell", 1, 100, 0.1))
    L.record_fill(fill("c", "Buy", 1, 99, 0.1, ts=2000))
    p = L.pnl()
    assert p["funding"] is None and p["net"] is None and p["claimable_profit"] is None and "FUNDING_NOT_RECORDED:BTCUSDT" in p["flags"]
    assert p["price_leg"] == pytest.approx(1.0)                                     # the known part is still reported


def test_explicit_declaration_is_the_only_way_funding_is_zero():
    L = A.Ledger()
    L.record_fill(fill("a", "Sell", 1, 100, 0.1))
    L.record_fill(fill("c", "Buy", 1, 99, 0.1, ts=2000))
    L.declare_no_funding("BTCUSDT", "held between two funding prints", "DERIVED")
    p = L.pnl()
    assert p["funding"] == 0.0 and p["net"] == pytest.approx(0.8) and p["status"] == "RECONCILED"
    with pytest.raises(A.LedgerError):
        L.declare_no_funding("BTCUSDT", "  ")


def test_funding_amount_unknown_makes_net_unknown():
    L = closed_pair()
    L.record_funding("BTCUSDT", 1700, None, "MEASURED")
    p = L.pnl()
    assert p["funding"] is None and p["net"] is None and p["claimable_profit"] is None


def test_spot_only_symbol_needs_no_funding():
    L = A.Ledger()
    L.record_fill(fill("b", "Buy", 1, 100, 0.1, leg="S"), category="spot")
    L.record_fill(fill("d", "Sell", 1, 101, 0.1, leg="S", ts=2000), category="spot")
    p = L.pnl()
    assert p["funding"] == 0.0 and p["net"] == pytest.approx(0.8) and p["status"] == "RECONCILED"


def test_missing_fee_is_flagged_not_zero():
    for bad in (None, float("nan")):
        L = closed_pair()
        L.record_fill(fill("z", "Sell", 1, 100, bad, sym="SOLUSDT", leg="X", grp="G9"), category="spot")
        p = L.pnl()
        assert p["fees"] is None and p["net"] is None and p["claimable_profit"] is None and "FEES_UNKNOWN" in p["flags"]
        assert any(f.startswith("FEE_UNKNOWN") for f in p["flags"])


def test_zero_cost_execution_unmeasured_is_flagged():
    L = A.Ledger()
    L.record_fill(fill("a", "Sell", 1, 100, 0.0, prov="SIMULATED"))
    L.record_fill(fill("c", "Buy", 1, 99, 0.0, ts=2000, prov="SIMULATED"))
    L.declare_no_funding("BTCUSDT", "test")
    p = L.pnl()
    assert p["fees"] is None and p["net"] is None and p["claimable_profit"] is None and any(f.startswith("ZERO_FEE_UNVERIFIED") for f in p["flags"])


def test_zero_fee_that_was_measured_is_accepted():
    L = A.Ledger()
    L.record_fill(fill("a", "Sell", 1, 100, 0.0, prov="MEASURED"))
    L.record_fill(fill("c", "Buy", 1, 99, 0.0, ts=2000, prov="MEASURED"))
    L.declare_no_funding("BTCUSDT", "test")
    assert L.pnl()["fees"] == 0.0


def test_rebate_is_never_assumed():
    L = A.Ledger()
    L.record_fill(fill("a", "Sell", 1, 100, -0.05))
    p = L.pnl()
    assert p["fees"] is None and any(f.startswith("FEE_REBATE_UNVERIFIED") for f in p["flags"])


# ---------------------------------------------------------------- unreconciled != profit
def test_fake_owner_profit_claim_open_leg_never_reports_profit():
    """One leg closed in profit, the hedge leg still open and unmarked: the ledger must not look like a clean profit."""
    L = A.Ledger()
    L.record_fill(fill("a", "Sell", 1, 100, 0.1, leg="P"))
    L.record_fill(fill("b", "Buy", 1, 100, 0.1, leg="S"), category="spot")
    L.record_fill(fill("c", "Buy", 1, 90, 0.1, leg="P", ts=2000))                    # perp closed +10
    L.record_funding("BTCUSDT", 1500, 0.5, "MEASURED")
    p = L.pnl()
    assert p["net"] is None and p["claimable_profit"] is None and p["status"] == "UNRECONCILED"
    assert p["unrealised"] is None and any(f.startswith("NO_MARK_FOR_OPEN_LEG") for f in p["flags"]) and any(f.startswith("OPEN_LEG") for f in p["flags"])
    assert p["price_leg"] is None                                                   # the known +10 is not offered as 'price leg'; realised is separate:
    assert p["per_leg"]["G1/P"]["realised_price"] == pytest.approx(10.0)


def test_open_leg_with_a_mark_has_a_net_but_still_no_claimable_profit():
    L = A.Ledger()
    L.record_fill(fill("a", "Sell", 1, 100, 0.1))
    L.mark("BTCUSDT", 1500, 90.0)
    L.record_funding("BTCUSDT", 1200, 0.2, "MEASURED")
    p = L.pnl()
    assert p["unrealised"] == pytest.approx(10.0) and p["net"] == pytest.approx(10.0 + 0.2 - 0.1)
    assert p["claimable_profit"] is None and p["status"] == "UNRECONCILED"
    assert p["prov"] in ("MODELLED", "ASSUMED", "UNKNOWN")                           # a mark-to-market is never MEASURED profit


def test_stale_mark_does_not_replace_a_newer_one():
    L = A.Ledger()
    L.record_fill(fill("a", "Buy", 1, 100, 0.1))
    assert L.mark("BTCUSDT", 2000, 110.0) is True
    assert L.mark("BTCUSDT", 1000, 50.0) is False
    L.declare_no_funding("BTCUSDT", "x")
    assert L.pnl()["unrealised"] == pytest.approx(10.0)


def test_spot_mark_is_not_the_perp_mark():
    L = A.Ledger()
    L.record_fill(fill("b", "Buy", 1, 100, 0.1, leg="S"), category="spot")
    L.mark("BTCUSDT", 1500, 200.0)                                                 # a perp mark only
    assert L.pnl()["unrealised"] is None


def test_provenance_is_the_weakest_input():
    L = closed_pair()
    L.record_fill(fill("s", "Buy", 1, 10, 0.1, sym="SOLUSDT", leg="Q", grp="G7", prov="SIMULATED"))
    L.record_fill(fill("t", "Sell", 1, 11, 0.1, sym="SOLUSDT", leg="Q", grp="G7", ts=2000, prov="SIMULATED"))
    L.declare_no_funding("SOLUSDT", "x", "ASSUMED")
    p = L.pnl()
    assert p["per_symbol"]["BTCUSDT"]["prov"] == "MEASURED" and p["per_symbol"]["SOLUSDT"]["prov"] == "ASSUMED" and p["prov"] == "ASSUMED"


# ---------------------------------------------------------------- duplicates and invalid records
def test_duplicate_fill_is_ignored():
    L = A.Ledger()
    assert L.record_fill(fill("a", "Sell", 1, 100, 0.1)) is True
    assert L.record_fill(fill("a", "Sell", 1, 100, 0.1)) is False
    L.record_fill(fill("c", "Buy", 1, 99, 0.1, ts=2000))
    L.declare_no_funding("BTCUSDT", "x")
    p = L.pnl()
    assert p["fees"] == pytest.approx(0.2) and p["price_leg"] == pytest.approx(1.0) and p["duplicates_ignored"] == 1 and p["status"] == "RECONCILED"


def test_conflicting_duplicate_is_flagged_and_unreconciles():
    L = closed_pair()
    L.record_fill(fill("a", "Sell", 9, 100, 0.1, leg="P"))
    p = L.pnl()
    assert "DUPLICATE_FILL_CONFLICT:a" in p["flags"] and p["claimable_profit"] is None and p["status"] == "UNRECONCILED"


@pytest.mark.parametrize("kw", [dict(qty=0), dict(qty=-1), dict(price=0), dict(price=float("nan")), dict(side="sell"), dict(fid=""), dict(ts=0), dict(qty=True)])
def test_invalid_fill_is_refused_loudly(kw):
    with pytest.raises(A.LedgerError):
        A.Ledger().record_fill(fill(**kw))


def test_invalid_mark_and_funding_refused():
    L = A.Ledger()
    for args in (("BTCUSDT", 1, 0.0), ("BTCUSDT", 1, float("inf")), ("BTCUSDT", 0, 1.0)):
        with pytest.raises(A.LedgerError):
            L.mark(*args)
    with pytest.raises(A.LedgerError):
        L.record_funding("BTCUSDT", 0, 1.0, "MEASURED")
    with pytest.raises(A.LedgerError):
        L.record_funding("BTCUSDT", 5, 1.0, "BOGUS")


# ---------------------------------------------------------------- position arithmetic
def test_partial_close_and_flip():
    L = A.Ledger()
    L.record_fill(fill("a", "Buy", 2, 100, 0.1))
    L.record_fill(fill("b", "Sell", 1, 110, 0.1, ts=2000))                          # +10 realised, 1 left @100
    L.record_fill(fill("c", "Sell", 3, 120, 0.1, ts=3000))                          # closes 1 (+20), flips short 2 @120
    L.mark("BTCUSDT", 4000, 115.0)
    L.declare_no_funding("BTCUSDT", "x")
    leg = L.pnl()["per_leg"]["G1/P"]
    assert leg["realised_price"] == pytest.approx(30.0) and leg["qty"] == pytest.approx(-2.0)
    assert leg["unrealised_price"] == pytest.approx((120 - 115) * 2)


def test_average_cost_on_adds():
    L = A.Ledger()
    L.record_fill(fill("a", "Sell", 1, 100, 0.1))
    L.record_fill(fill("b", "Sell", 3, 120, 0.1, ts=2000))                          # avg 115
    L.record_fill(fill("c", "Buy", 4, 105, 0.1, ts=3000))
    leg = L.pnl()["per_leg"]["G1/P"]
    assert leg["realised_price"] == pytest.approx((115 - 105) * 4) and leg["qty"] == 0.0


def test_slippage_is_attribution_inside_price_leg_and_unknown_without_reference():
    L = A.Ledger()
    L.record_fill(fill("a", "Sell", 1, 99.5, 0.1), ref_price=100.0)                 # sold 0.5 worse than planned
    L.record_fill(fill("b", "Buy", 1, 99.0, 0.1, ts=2000), ref_price=99.0)
    L.declare_no_funding("BTCUSDT", "x")
    p = L.pnl()
    assert p["slippage"] == pytest.approx(0.5) and p["price_leg"] == pytest.approx(0.5)
    assert p["net"] == pytest.approx(0.5 - 0.2)                                      # slippage NOT deducted a second time
    L2 = A.Ledger()
    L2.record_fill(fill("a", "Sell", 1, 99.5, 0.1))
    assert L2.pnl()["slippage"] is None
