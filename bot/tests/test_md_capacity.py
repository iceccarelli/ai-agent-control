"""The capacity curve: what each trade size costs on the displayed book, from real traffic."""
import math

import pytest

import md_capacity as MC
import md_economics as ME
from test_md_integration import complete_state


@pytest.fixture(scope="module")
def view():
    st, now, wall = complete_state()
    return st.view(now, wall)


def test_the_curve_is_measured_for_small_sizes_and_unavailable_past_the_displayed_depth(view):
    row = MC.capacity_row(view, now_ms=1)
    by = {p["notional_usd"]: p for p in row["curve"]}
    assert by[1_000.0]["status"] == ME.MEASURED and by[10_000.0]["status"] == ME.MEASURED
    unavailable = [p for p in row["curve"] if p["status"] == ME.UNAVAILABLE]
    assert unavailable, "a book of 50 levels cannot fill five million dollars; that is the limit, not a price"
    assert all("DEPTH_INSUFFICIENT" in " ".join(p["reasons"]) or p["reasons"] for p in unavailable)
    assert row["max_displayed_fillable_notional_usd"] == max(
        p["notional_usd"] for p in row["curve"] if p["status"] == ME.MEASURED)


def test_cost_never_falls_as_size_grows(view):
    pts = [p for p in MC.capacity_row(view, now_ms=1)["curve"] if p["status"] == ME.MEASURED]
    totals = [p["total_round_trip_bps"] for p in pts]
    assert totals == sorted(totals) and len(totals) >= 2


def test_the_row_matches_the_economics_module_it_is_built_on(view):
    row = MC.capacity_row(view, now_ms=1, notionals=(10_000.0,))
    p = row["curve"][0]
    dc = ME.depth_costs({"perp_bids": view.perp_bids, "perp_asks": view.perp_asks,
                         "spot_bids": view.spot_bids, "spot_asks": view.spot_asks},
                        mode=ME.OVERLAY, notional_usd=10_000.0, ref_price=view.perp_mid,
                        latency_allowance_bps=0.0)
    assert p["spread_bps"] == pytest.approx(dc.spread_bps) and p["slippage_bps"] == pytest.approx(dc.slippage_bps)
    fees = ME.FeeSchedule.published_default()
    assert p["fee_round_trip_bps"] == pytest.approx(2 * fees.perp_taker_bps)
    assert p["total_round_trip_bps"] == pytest.approx(p["book_round_trip_bps"] + p["fee_round_trip_bps"])


def test_unknowns_stay_unknown_and_the_fee_source_is_labelled(view):
    row = MC.capacity_row(view, now_ms=1)
    assert row["fee_source"] == ME.FEE_PUBLISHED_DEFAULT
    assert "UNKNOWN" in row["latency_bps"] and row["classes"]["latency_borrow_account_tier"] == ME.UNKNOWN
    assert row["note"].startswith("lower bound")


def test_breakeven_exists_only_for_positive_funding(view):
    row = MC.capacity_row(view, now_ms=1)
    f = row["funding_forecast_bps_per_settlement"]
    for p in row["curve"]:
        if p["status"] != ME.MEASURED:
            continue
        if f is not None and f > 0:
            assert p["breakeven_settlements"] == pytest.approx(p["total_round_trip_bps"] / f)
        else:
            assert p["breakeven_settlements"] is None


def test_a_view_with_no_book_yields_unavailable_points_not_an_exception():
    import md_state as S
    empty = S.MarketState("BTCUSDT").view(1.0, 1)
    row = MC.capacity_row(empty, now_ms=1)
    assert all(p["status"] == ME.UNAVAILABLE for p in row["curve"])
    assert row["max_displayed_fillable_notional_usd"] is None


def test_affordable_notional_picks_the_largest_size_within_the_budget(view):
    row = MC.capacity_row(view, now_ms=1)
    measured = [p for p in row["curve"] if p["status"] == ME.MEASURED]
    cheapest = min(p["total_round_trip_bps"] for p in measured)
    assert MC.affordable_notional(row, cheapest - 0.001) is None
    assert MC.affordable_notional(row, 1e9) == row["max_displayed_fillable_notional_usd"]
    assert math.isfinite(MC.affordable_notional(row, measured[0]["total_round_trip_bps"]))
