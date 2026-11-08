"""ms_accounting: unknown != zero, unreconciled != profit, duplicate fills, funding sign. Offline, pure."""
import json
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


# ---------------------------------------------------------------- red team rt8: RECONCILED is internal consistency, not a venue read
FLAT_VENUE = {"BTCUSDT": 0.0, "SPOT:BTCUSDT": 0.0}


def test_status_reconciled_is_internal_consistency_and_says_so():
    p = closed_pair().pnl()
    assert p["status"] == "RECONCILED" and p["claimable_profit"] == pytest.approx(2.1)         # the old keys keep their names and values (consumers depend on them)
    assert p["reconciliation_basis"] == "INTERNAL_CONSISTENCY_ONLY" and p["internally_consistent"] is True and p["internal_status"] == "INTERNALLY_CONSISTENT"
    assert p["venue_reconciled"] is False and p["venue_claimable_profit"] is None               # nothing here read a venue
    assert "NOT a venue position read" in p["status_meaning"]
    for roll in list(p["per_symbol"].values()) + list(p["per_leg"].values()):
        assert roll["venue_reconciled"] is False and roll["reconciliation_basis"] == "INTERNAL_CONSISTENCY_ONLY"


def test_internal_inconsistency_is_reported_in_the_new_keys_too():
    L = A.Ledger()
    L.record_fill(fill("a", "Sell", 1, 100, 0.1))
    p = L.pnl()
    assert p["status"] == "UNRECONCILED" and p["internally_consistent"] is False and p["internal_status"] == "NOT_INTERNALLY_CONSISTENT" and p["venue_reconciled"] is False


def test_venue_reconciled_needs_an_injected_venue_read_that_matches():
    p = closed_pair().pnl(venue_positions=FLAT_VENUE)
    assert p["venue_reconciled"] is True and p["reconciliation_basis"] == "VENUE_POSITION_READ_MATCHED" and p["venue_claimable_profit"] == pytest.approx(2.1)


@pytest.mark.parametrize("venue,flag", [({"BTCUSDT": 0.5, "SPOT:BTCUSDT": 0.0}, "VENUE_POSITION_MISMATCH:BTCUSDT"),
                                        ({"BTCUSDT": 0.0}, "VENUE_POSITION_UNKNOWN:SPOT:BTCUSDT"),
                                        ({"BTCUSDT": 0.0, "SPOT:BTCUSDT": None}, "VENUE_POSITION_UNKNOWN:SPOT:BTCUSDT"),
                                        ({"BTCUSDT": 0.0, "SPOT:BTCUSDT": float("nan")}, "VENUE_POSITION_UNKNOWN:SPOT:BTCUSDT"),
                                        ({**FLAT_VENUE, "ETHUSDT": -2.0}, "VENUE_POSITION_NOT_IN_LEDGER:ETHUSDT"),
                                        ({}, "VENUE_POSITION_UNKNOWN:BTCUSDT")])
def test_a_venue_read_that_disagrees_or_is_incomplete_is_not_a_reconciliation(venue, flag):
    p = closed_pair().pnl(venue_positions=venue)
    assert p["venue_reconciled"] is False and p["venue_claimable_profit"] is None and flag in p["flags"] and p["reconciliation_basis"] == "VENUE_POSITION_READ_MISMATCH"
    assert p["status"] == "RECONCILED" and p["claimable_profit"] == pytest.approx(2.1)         # the internal view is unchanged: the venue read adds, never rewrites


def test_a_matching_venue_read_does_not_make_an_inconsistent_ledger_claimable():
    L = A.Ledger()
    L.record_fill(fill("a", "Sell", 1, 100, 0.1))                                              # open perp, no funding record
    p = L.pnl(venue_positions={"BTCUSDT": -1.0})
    assert p["reconciliation_basis"] == "VENUE_POSITION_READ_MATCHED" and p["venue_reconciled"] is False and p["venue_claimable_profit"] is None
    for bad in ([], "x", 3):
        assert closed_pair().pnl(venue_positions=bad)["reconciliation_basis"] == "VENUE_READ_INVALID"


# =============================================================================================== exact arithmetic (Decimal), idempotency, order independence
import random  # noqa: E402
from decimal import Decimal as D  # noqa: E402


def _state(L):
    p = L.pnl_exact()
    return {k: p[k] for k in ("price_leg", "funding", "fees", "slippage", "realised", "unrealised", "realised_price", "qty", "net", "claimable_profit", "status", "flags", "n_legs")} | {
        "legs": {k: (v["qty"], v["realised_price"], v["fees"], v["unrealised_price"]) for k, v in p["per_leg"].items()}}


def test_float_inputs_are_converted_by_str_and_the_arithmetic_is_exact():
    L = A.Ledger()
    L.record_fill(fill("a", "Buy", 0.1, 100.0, 0.0, prov="MEASURED", leg="P"))
    L.record_fill(fill("b", "Buy", 0.2, 100.0, 0.0, prov="MEASURED", leg="P", ts=1001))
    L.record_fill(fill("c", "Sell", 0.3, 101.0, 0.0, prov="MEASURED", leg="P", ts=1002))
    p = L.pnl_exact()
    assert p["per_leg"]["G1/P"]["qty"] == D(0) and p["per_leg"]["G1/P"]["dust_qty"] == D(0)      # 0.1 + 0.2 - 0.3 is exactly 0
    assert p["realised_price"] == D("0.3") and isinstance(p["realised_price"], D)
    assert 0.1 + 0.2 - 0.3 != 0                                                      # the float arithmetic this replaces would leave 5.5e-17


def test_fee_sum_is_exact_and_floats_appear_only_at_the_boundary():
    L = A.Ledger()
    for i in range(10):
        L.record_fill(fill(f"f{i}", "Buy" if i % 2 == 0 else "Sell", 1, 100, 0.1, ts=1000 + i))
    assert L.pnl_exact()["fees"] == D("1.0")
    naive = 0.0
    for _ in range(10):
        naive += 0.1                                  # plain float accumulation drifts on every Python version (sum() is compensated on 3.12)
    assert naive != 1.0
    out = L.pnl()
    assert isinstance(out["fees"], float) and isinstance(out["per_leg"]["G1/P"]["qty"], float) and out["fees"] == 1.0


def test_partial_close_books_realised_on_the_closed_part_and_stays_unreconciled():
    L = A.Ledger()
    L.record_fill(fill("a", "Sell", 3, 100, 0.1))
    L.record_fill(fill("b", "Buy", 1, 99, 0.1, ts=2000))
    L.record_fill(fill("m", "Buy", 1, 1, 0.1, ts=2001, leg="X"))     # (unrelated leg: just to have two legs)
    L.record_funding("BTCUSDT", 1500, 0.0, "MEASURED")
    L.mark("BTCUSDT", 3000, 98.0)
    p = L.pnl_exact()["per_leg"]["G1/P"]
    assert p["realised_price"] == D(1) and p["qty"] == D(-2) and p["unrealised_price"] == D(4)
    assert L.pnl()["claimable_profit"] is None and L.pnl()["status"] == "UNRECONCILED"


def test_partial_close_cost_rounding_is_the_only_division_and_a_full_close_is_exact():
    L = A.Ledger()
    L.record_fill(fill("a", "Buy", 3, 100.0, 0.1, prov="MEASURED"))
    L.record_fill(fill("b", "Sell", 1, 101.0, 0.1, ts=2000))
    L.record_fill(fill("c", "Sell", 2, 101.0, 0.1, ts=3000))
    p = L.pnl_exact()["per_leg"]["G1/P"]
    assert p["qty"] == D(0) and p["realised_price"] == D(3)


def test_unknown_mark_is_explicit_none_never_zero():
    L = A.Ledger()
    L.record_fill(fill("a", "Sell", 1, 100, 0.1))
    L.declare_no_funding("BTCUSDT", "held between prints")
    p = L.pnl_exact()
    assert p["unrealised"] is None and p["net"] is None and p["price_leg"] is None and any(f.startswith("NO_MARK_FOR_OPEN_LEG") for f in p["flags"])
    assert L.pnl()["net"] is None and L.canonical_net()["net"] is None and L.account()["equity"] is None


@pytest.mark.parametrize("seed", range(40))
def test_random_orderings_and_duplicates_give_the_same_state(seed):
    rnd = random.Random(1000 + seed)
    base = []
    pos = 0
    for i in range(rnd.randint(4, 12)):
        side = rnd.choice(["Buy", "Sell"])
        q = rnd.choice([1, 2, 0.5, 0.1, 0.3, 1.25])
        base.append(fill(f"f{i:02d}", side, q, round(rnd.uniform(90, 110), 2), rnd.choice([0.05, 0.1, 0.0123]), leg=rnd.choice(["P", "S"]), ts=1000 + i))
    funding = [(1500 + 10 * i, rnd.choice([0.5, -0.25, 0.1])) for i in range(3)]
    ref = A.Ledger()
    for f in base:
        ref.record_fill(f, category="linear")
    for ts, a in funding:
        ref.record_funding("BTCUSDT", ts, a, "MEASURED")
    ref.mark("BTCUSDT", 9000, 100.5)
    want = _state(ref)
    for trial in range(6):
        seq = base + [rnd.choice(base) for _ in range(rnd.randint(0, 6))]            # duplicates
        rnd.shuffle(seq)
        L = A.Ledger()
        fseq = funding + [rnd.choice(funding) for _ in range(3)]
        rnd.shuffle(fseq)
        for f in seq:
            L.record_fill(f)
        for ts, a in fseq:
            L.record_funding("BTCUSDT", ts, a, "MEASURED")
        L.mark("BTCUSDT", 8000, 1.0)                                                # an older mark arrives late...
        L.mark("BTCUSDT", 9000, 100.5)
        L.mark("BTCUSDT", 8500, 2.0)                                                # ...and one in between: neither replaces the newest
        assert _state(L) == want
        assert L.duplicates_ignored == len(seq) - len(base)


def test_conflicting_duplicate_fill_is_flagged_in_either_arrival_order():
    a, b = fill("x", "Buy", 1, 100), fill("x", "Buy", 2, 100)
    for first, second in ((a, b), (b, a)):
        L = A.Ledger()
        L.record_fill(first)
        assert L.record_fill(second) is False
        p = L.pnl()
        assert "DUPLICATE_FILL_CONFLICT:x" in p["flags"] and p["status"] == "UNRECONCILED" and p["claimable_profit"] is None
        assert L.integrity_flags() == ["DUPLICATE_FILL_CONFLICT:x"]


def test_funding_is_idempotent_by_symbol_and_ts_and_conflict_is_an_integrity_flag():
    L = closed_pair()
    n0 = L.pnl()["net"]
    assert L.record_funding("BTCUSDT", 1500, 0.5, "MEASURED") is False and L.pnl()["net"] == n0 and L.integrity_flags() == []
    assert L.record_funding("BTCUSDT", 1500, 0.7, "MEASURED") is False
    assert L.integrity_flags() == ["FUNDING_CONFLICT:BTCUSDT:1500"] and L.pnl()["claimable_profit"] is None and L.pnl()["net"] == n0       # the first record stays


def test_orphan_fill_is_accepted_flagged_and_cleared_by_the_acknowledgement():
    L = A.Ledger(track_acks=True)
    L.record_fill(fill("a", "Sell", 1, 100, 0.1))
    L.record_fill(fill("c", "Buy", 1, 99, 0.1, ts=2000))
    L.declare_no_funding("BTCUSDT", "x")
    p = L.pnl()
    assert "ORPHAN_FILL:G1:P" in p["flags"] and p["status"] == "UNRECONCILED" and p["claimable_profit"] is None and p["net"] == pytest.approx(0.8)
    assert L.integrity_flags() == ["ORPHAN_FILL:G1:P"]
    L.acknowledge("G1", "P", "O1")
    p = L.pnl()
    assert "ORPHAN_FILL:G1:P" not in p["flags"] and p["status"] == "RECONCILED" and L.integrity_flags() == []
    plain = A.Ledger()                                                                  # tracking is opt-in: the old behaviour is unchanged
    plain.record_fill(fill("a", "Sell", 1, 100, 0.1))
    assert not any("ORPHAN" in f for f in plain.pnl()["flags"])


def test_inputs_roundtrip_rebuilds_the_same_ledger_in_any_order():
    L = closed_pair()
    d = L.export_inputs()
    d["fills"] = list(reversed(d["fills"]))
    M = A.Ledger.from_inputs(json.loads(json.dumps(d)))
    assert _state(M) == _state(L) and json.loads(M.to_json())["fills"] == L.export_inputs()["fills"]


def test_deposits_and_withdrawals_move_the_balance_never_the_net():
    L = closed_pair()
    net0 = L.pnl()["net"]
    assert L.account()["balance"] is None and "OPENING_BALANCE_UNKNOWN" in L.account()["flags"] and L.account()["equity"] is None
    L.set_opening_balance(1000.0)
    L.record_account_event("d1", 5000, "DEPOSIT", 250.0)
    L.record_account_event("w1", 5001, "WITHDRAWAL", 40.0)
    assert L.record_account_event("d1", 5000, "DEPOSIT", 250.0) is False
    a = L.account()
    assert L.pnl()["net"] == net0 == pytest.approx(2.1) and L.pnl_exact()["net"] == D("2.1")
    assert a["balance"] == pytest.approx(1000 + 250 - 40 + 2.1) and a["equity"] == a["balance"] and a["deposits"] == 250.0 and a["withdrawals"] == 40.0
    assert a["margin_known"] is False and a["margin_used_usd"] is None and a["net_pnl_excludes_deposits_and_withdrawals"] is True
    L.set_margin(100.0, 900.0, 7000)
    assert L.account()["margin_known"] is True and L.account()["margin_used_usd"] == 100.0
    L.record_account_event("d1", 5000, "DEPOSIT", 999.0)
    assert "ACCOUNT_EVENT_CONFLICT:d1" in L.integrity_flags()
    with pytest.raises(A.LedgerError):
        L.record_account_event("d2", 5000, "DEPOSIT", -1.0)


def test_equity_is_balance_plus_unrealised_with_explicit_components():
    L = A.Ledger()
    L.set_opening_balance(1000)
    L.record_fill(fill("a", "Buy", 1, 100, 0.1))
    L.declare_no_funding("BTCUSDT", "x")
    L.mark("BTCUSDT", 2000, 103.0)
    a = L.account()
    assert a["realised_net"] == pytest.approx(-0.1) and a["unrealised"] == pytest.approx(3.0) and a["balance"] == pytest.approx(999.9) and a["equity"] == pytest.approx(1002.9)


# ---------------------------------------------------------------- the single net definition
def test_canonical_net_values_open_legs_at_the_exit_side_and_charges_slippage_once():
    L = A.Ledger()
    L.set_exit_fee_bps(5.5, 0.0)
    L.record_fill(fill("a", "Buy", 2, 100.5, 0.1))                          # taker walk: the fill is 0.5 above the 100.0 mid
    L.declare_no_funding("BTCUSDT", "x")
    L.record_slippage(1.0, "taker", "BTCUSDT")                               # 2 * 0.5
    L.record_slippage(0.25, "adverse", "BTCUSDT")
    L.mark("BTCUSDT", 2000, 100.0, bid=99.9, ask=100.1)
    c = L.canonical_net()
    assert c["valuation"] == "EXIT_SIDE" and c["taker_slippage"] == 1.0 and c["adverse_selection"] == 0.25
    # price leg at mid: (100 - 100.5) * 2 + 1.0 taker add-back = 0; exit adj = (99.9 - 100) * 2 - 2 * 99.9 * 5.5e-4
    adj = (99.9 - 100.0) * 2 - 2 * 99.9 * 5.5 / 1e4
    assert c["exit_adjustment"] == pytest.approx(adj) and c["net"] == pytest.approx(0.0 + adj - 0.1 - 1.0 - 0.25)
    assert c["ledger_net"] == pytest.approx(-1.1)                              # ms_accounting's own net is a different (price-leg) figure
    assert L.record_slippage(9, "taker", "BTCUSDT", key="k") is True and L.record_slippage(9, "taker", "BTCUSDT", key="k") is False


def test_canonical_net_without_an_exit_book_is_flagged_and_strict_makes_it_none():
    L = A.Ledger()
    L.record_fill(fill("a", "Buy", 1, 100, 0.1))
    L.declare_no_funding("BTCUSDT", "x")
    L.mark("BTCUSDT", 2000, 101.0)
    c = L.canonical_net()
    assert c["valuation"] == "MID_FALLBACK" and "EXIT_PRICE_MISSING" in c["flags"] and c["net"] == pytest.approx(0.9)
    assert L.canonical_net(strict_exit=True)["net"] is None


# ---------------------------------------------------------------- venue reconciliation
def venue_read(L=None, **over):
    ex = [{"execId": "a", "orderLinkId": "G1-P", "symbol": "BTCUSDT", "side": "Sell", "execQty": "1", "execPrice": "100", "execFee": "0.1"},
          {"execId": "b", "orderLinkId": "G1-S", "symbol": "BTCUSDT", "side": "Buy", "execQty": "1", "execPrice": "100", "execFee": "0.1"}]
    kw = {"executions": ex, "positions": {"BTCUSDT": "-1", "SPOT:BTCUSDT": "1"}, "wallet": {"balance_change_usd": "-0.2"}, "fees": "0.2"}
    kw.update(over)
    return kw


def open_pair():
    L = A.Ledger()
    L.record_fill(fill("a", "Sell", 1, 100, 0.1, leg="P"))
    L.record_fill(fill("b", "Buy", 1, 100, 0.1, leg="S"), category="spot")
    L.declare_no_funding("BTCUSDT", "x")
    return L


def codes(r):
    return sorted({f["code"] for f in r["findings"]})


def test_reconcile_with_venue_exact_agreement_sets_venue_reconciled():
    L = closed_pair()
    ex = [{"execId": i, "orderLinkId": f"G1-{leg}", "symbol": "BTCUSDT", "side": s, "execQty": "1", "execPrice": px, "execFee": "0.1"}
          for i, leg, s, px in (("a", "P", "Sell", "100"), ("b", "S", "Buy", "100"), ("c", "P", "Buy", "99"), ("d", "S", "Sell", "101"))]
    assert L.pnl()["venue_reconciled"] is False
    r = L.reconcile_with_venue(ex, {"BTCUSDT": "0", "SPOT:BTCUSDT": "0"}, {"balance_change_usd": "2.1"}, "0.4")
    assert r["venue_reconciled"] is True and r["findings"] == []
    p = L.pnl()
    assert p["venue_reconciled"] is True and p["venue_claimable_profit"] == pytest.approx(2.1) and p["reconciliation_basis"] == "VENUE_EXECUTIONS_POSITIONS_FEES"
    L.record_fill(fill("z", "Buy", 1, 100, 0.1, ts=9000))                       # anything booked afterwards makes the reconciliation stale
    p = L.pnl()
    assert p["venue_reconciled"] is False and "VENUE_RECONCILIATION_STALE" in p["flags"] and p["venue_claimable_profit"] is None


def test_reconcile_with_venue_open_pair_matches_and_internal_status_is_not_venue_status():
    L = open_pair()
    r = L.reconcile_with_venue(**venue_read())
    assert r["venue_reconciled"] is True and L.pnl()["status"] == "UNRECONCILED"          # open legs: still not a claimable profit
    assert L.pnl()["venue_reconciled"] is False or L.pnl()["internally_consistent"] is True


@pytest.mark.parametrize("over,code", [
    ({"executions": venue_read()["executions"][:1]}, "EXEC_MISSING_IN_VENUE"),
    ({"executions": venue_read()["executions"] + [{"execId": "q", "symbol": "BTCUSDT", "side": "Buy", "execQty": "1", "execPrice": "100", "execFee": "0.1"}]}, "EXEC_MISSING_IN_LEDGER"),
    ({"executions": [dict(venue_read()["executions"][0], execQty="1.5"), venue_read()["executions"][1]]}, "EXEC_QTY_MISMATCH"),
    ({"executions": [dict(venue_read()["executions"][0], execPrice="100.5"), venue_read()["executions"][1]]}, "EXEC_PRICE_MISMATCH"),
    ({"executions": [dict(venue_read()["executions"][0], execFee="0.2"), venue_read()["executions"][1]]}, "EXEC_FEE_MISMATCH"),
    ({"executions": [dict(venue_read()["executions"][0], side="Buy"), venue_read()["executions"][1]]}, "EXEC_SIDE_MISMATCH"),
    ({"executions": [dict(venue_read()["executions"][0], orderLinkId="G1-X"), venue_read()["executions"][1]]}, "EXEC_LINK_MISMATCH"),
    ({"executions": None}, "VENUE_EXECUTIONS_UNKNOWN"),
    ({"fees": "0.3"}, "FEES_MISMATCH"),
    ({"fees": None}, "VENUE_FEES_UNKNOWN"),
    ({"positions": {"BTCUSDT": "-1.5", "SPOT:BTCUSDT": "1"}}, "POSITION_QTY_MISMATCH"),
    ({"positions": {"BTCUSDT": "-1"}}, "VENUE_POSITION_UNKNOWN"),
    ({"positions": {"BTCUSDT": "-1", "SPOT:BTCUSDT": "1", "ETHUSDT": "2"}}, "VENUE_POSITION_NOT_IN_LEDGER"),
    ({"positions": None}, "VENUE_POSITIONS_UNKNOWN"),
    ({"positions": {"BTCUSDT": {"qty": "-1", "avg_price": "101"}, "SPOT:BTCUSDT": "1"}}, "POSITION_AVG_PRICE_MISMATCH"),
    ({"wallet": {"balance_change_usd": "-0.5"}}, "WALLET_MISMATCH"),
    ({"wallet": {"balance_change_usd": None}}, "VENUE_WALLET_UNKNOWN"),
])
def test_reconcile_with_venue_every_mismatch_is_a_finding_and_not_a_reconciliation(over, code):
    L = open_pair()
    r = L.reconcile_with_venue(**venue_read(**over))
    assert r["venue_reconciled"] is False and code in codes(r)
    p = L.pnl()
    assert p["venue_reconciled"] is False and p["venue_claimable_profit"] is None and p["reconciliation_basis"] == "VENUE_RECONCILIATION_MISMATCH"


def test_reconcile_with_venue_incomplete_execution_read_cannot_reconcile_and_never_edits_the_ledger():
    L = open_pair()
    before = (L.positions(), L.fills(), L.export_inputs())
    r = L.reconcile_with_venue(**venue_read(executions=venue_read()["executions"][:1]), executions_complete=False)
    assert r["venue_reconciled"] is False and "VENUE_EXECUTIONS_INCOMPLETE" in codes(r) and "EXEC_MISSING_IN_VENUE" not in codes(r)
    assert (L.positions(), L.fills(), L.export_inputs()) == before


def test_reconcile_with_venue_tolerances_are_stated_and_bounded():
    L = open_pair()
    ok = L.reconcile_with_venue(**venue_read(positions={"BTCUSDT": "-1.0000000001", "SPOT:BTCUSDT": "1"}))
    assert ok["venue_reconciled"] is True and ok["checked"]["tolerances"]["qty"] == "1E-9"
    bad = L.reconcile_with_venue(**venue_read(positions={"BTCUSDT": "-1.001", "SPOT:BTCUSDT": "1"}))
    assert bad["venue_reconciled"] is False


def test_reconcile_with_venue_ledger_integrity_flags_block_it():
    L = open_pair()
    L.record_fill(fill("a", "Sell", 2, 100, 0.1, leg="P"))                         # conflicting duplicate
    r = L.reconcile_with_venue(**venue_read())
    assert r["venue_reconciled"] is False and "LEDGER_INTEGRITY" in codes(r)


def test_a_negative_slippage_amount_is_refused_because_it_would_book_a_gain():
    L = A.Ledger() if hasattr(A, "Ledger") else None
    for kind in ("taker", "adverse"):
        with pytest.raises(A.LedgerError):
            L.record_slippage(-0.01, kind, "BTCUSDT")
    assert L.record_slippage(0.0, "taker", "BTCUSDT") is True and L.record_slippage(0.5, "adverse", "BTCUSDT") is True
