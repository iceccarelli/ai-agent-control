"""The hedged PAIR's net, break-evens and cost sensitivity.

An overlay hedges the client's own BTC. The live journal books the short's price
leg and never revalues the inventory it hedges, so a cycle's booked net is the
net of an UNHEDGED SHORT: a BTC rally reads as a large loss and a fall as a
large win, neither of which is carry. These tests pin that the report states the
hedged-pair net beside the booked CASH net, that the verdict, the returns, the
benchmark comparison and the risk statistics are taken on the PAIR for an
overlay (never on the cash net of an unhedged short, whose sign follows BTC),
that every derived number says what it excludes, and that a missing input is
UNAVAILABLE, never zero.

Figures are hand-computed FIXTURES, not observations.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import ledger as L                      # noqa: E402
import carry_performance as perf        # noqa: E402
from ledger_fixtures import vraw as _vraw  # noqa: E402

DAY = 86_400_000
T0 = 1_700_000_000_000
P0 = 100_000.0


def overlay(j, d0, d1, *, tag, funding=300.0, close=P0, revalue=False,
            borrow=0.0):
    L.perp_open(j, ms=T0 + d0 * DAY, ref=f"{tag}-o", qty=1.0, price=P0,
                fee_usd=55.0, fee_status="confirmed")
    L.funding(j, ms=T0 + ((d0 + d1) // 2) * DAY, ref=f"{tag}-f", usd=funding,
              basis="venue", raw=_vraw(f"{tag}-f", T0 + ((d0 + d1) // 2) * DAY, funding))
    if borrow:
        L.borrow(j, ms=T0 + ((d0 + d1) // 2) * DAY + 1, ref=f"{tag}-b",
                 usd=borrow)
    if revalue:
        L.revalue_inventory(
            j, ms=T0 + d1 * DAY - 1, ref=f"{tag}-rv", qty=1.0,
            from_price=P0, to_price=close,
            raw={"valuation": L.MARK_VALUATION, "qty": 1.0,
                 "entry_spot": P0, "exit_spot": close,
                 "exit_mark_source": "tick_spot_mark",
                 "exit_mark_observed_ms": T0 + d1 * DAY - 1,
                 "fill_ms": T0 + d1 * DAY})
    L.perp_close(j, ms=T0 + d1 * DAY, ref=f"{tag}-c", qty=1.0, price=close,
                 entry_price=P0, fee_usd=55.0, fee_status="confirmed")


def acquire(j, d0, d1, *, tag, funding=300.0, close=P0):
    L.spot_buy(j, ms=T0 + d0 * DAY, ref=f"{tag}-sb", qty=1.0, price=P0,
               fee_btc=0.0, fee_usdt=55.0, fee_status="confirmed")
    L.perp_open(j, ms=T0 + d0 * DAY + 1, ref=f"{tag}-o", qty=1.0, price=P0,
                fee_usd=55.0, fee_status="confirmed")
    L.funding(j, ms=T0 + ((d0 + d1) // 2) * DAY, ref=f"{tag}-f", usd=funding,
              basis="venue", raw=_vraw(f"{tag}-f", T0 + ((d0 + d1) // 2) * DAY, funding))
    L.perp_close(j, ms=T0 + d1 * DAY, ref=f"{tag}-c", qty=1.0, price=close,
                 entry_price=P0, fee_usd=55.0, fee_status="confirmed")
    L.spot_sell(j, ms=T0 + d1 * DAY + 1, ref=f"{tag}-ss", qty=1.0, price=close,
                cost_basis=P0, fee_usd=55.0, fee_status="confirmed")


def economics(j, *, obs=(), env="paper"):
    import performance_analytics as pa
    cycles = perf.cycles_from_journal(j)
    perf.attach_capital(cycles, list(obs))
    e = perf.classify_environment(env, {})
    recon = {c["cycle_id"]: {"reconciled": True, "lifecycles": [],
                             "reasons": []} for c in cycles}
    for c in cycles:
        perf._classify_cycle(c, e, recon, [])
    opp = {o["cycle_id"]: o for o in (
        pa.overlay_opportunity_cost(c, benchmark_apr_pct=None)
        for c in cycles if c["closed"] and c["outcome"] == "CLOSED")}
    summ = perf.summarise(cycles)
    return perf.net_economics(cycles, e, journal=j, opp_by_cycle=opp,
                              period_days=summ.get("period_days"),
                              now_ms=T0 + 400 * DAY), cycles


# -- the unbooked inventory leg ----------------------------------------------

def test_a_btc_rally_is_not_a_carry_loss_for_an_overlay():
    j = L.Journal()
    overlay(j, 0, 10, tag="a", close=105_000.0)       # short loses 5000
    ne, cycles = economics(j)
    # unchanged: the BOOKED net is the net of the unhedged short, kept as the
    # labelled CASH figure
    assert ne["headline"]["net_usd"] == pytest.approx(300 - 110 - 5000)
    assert "unhedged short" in ne["headline"]["net_usd_meaning"]
    # CHANGED (Round 16): the verdict used to read this as the carry LOSING
    # money (FUNDING_POSITIVE_BUT_NET_NEGATIVE) because BTC rallied. A rally is
    # not a carry loss for a hedged pair. With the inventory leg only DERIVED
    # from perp prices (basis not measured) the verdict claims neither a profit
    # nor a loss: it states the two figures and what each is.
    assert ne["verdict"]["code"] == "OVERLAY_PAIR_DERIVED_NOT_BOOKED"
    # a DERIVED pair is funding - fees by construction (basis unmeasured): it is
    # shown, labelled, as hedged_pair_net_usd and is NOT the economic net
    assert ne["headline"]["economic_net_usd"] is None
    assert ne["headline"]["economic_net_basis"] == "DERIVED_NOT_BOOKED"
    assert ne["verdict"]["economic_net_usd"] is None
    # new, additive: the pair, with the inventory leg derived and labelled
    m = ne["metrics"]["hedged_pair_net_usd"]
    assert m["value"] == pytest.approx(190.0)
    assert m["evidence_class"] == perf.HISTORICAL_SIMULATION
    assert m["status"] == "DERIVED_NOT_BOOKED"
    assert ne["headline"]["hedged_pair_net_usd"] == pytest.approx(190.0)
    pair = ne["cycles"][0]["components"]["hedged_pair_net"]
    assert pair["value"] == pytest.approx(190.0)
    assert pair["additive"] is False and pair["inventory_leg"] == "DERIVED_FROM_PERP_FILL_PRICES"
    assert pair["inventory_leg_usd"] == pytest.approx(5000.0)
    # the basis residual cannot be measured from perp prices alone
    basis = ne["cycles"][0]["components"]["basis_pnl"]
    assert basis["value"] is None and basis["evidence_class"] == perf.UNAVAILABLE
    assert "basis" in basis["why"].lower()
    # the verdict says the loss is the unbooked hedge leg, with the pair number
    st = ne["verdict"]["statement"]
    assert "never revalued" in st and "190.00" in st
    assert ne["verdict"]["hedged_pair_net_usd"] == pytest.approx(190.0)


def test_a_btc_fall_is_not_a_carry_win_for_an_overlay():
    j = L.Journal()
    overlay(j, 0, 10, tag="a", close=95_000.0)        # short wins 5000
    ne, _ = economics(j)
    assert ne["headline"]["net_usd"] == pytest.approx(300 - 110 + 5000)
    assert ne["metrics"]["hedged_pair_net_usd"]["value"] == pytest.approx(190.0)


def test_a_booked_inventory_mark_is_added_to_the_pair_once_and_never_to_cash():
    j = L.Journal()
    overlay(j, 0, 10, tag="a", close=105_000.0, revalue=True)
    ne, cycles = economics(j)
    assert cycles[0]["revalue_usd"] == pytest.approx(5000.0)
    # CASH: 300 - 110 - 5000 (the short really lost 5000 on the venue). The
    # +5000 on the client's coin is a mark, not money.
    assert ne["headline"]["net_usd"] == pytest.approx(-4810.0)
    # PAIR: cash + the mark, once: 300 - 110 - 5000 + 5000
    m = ne["metrics"]["hedged_pair_net_usd"]
    assert m["value"] == pytest.approx(190.0)
    assert m["status"] == "COMPUTED"
    assert m["evidence_class"] == perf.MARKED_PAIR_ECONOMICS
    pair = ne["cycles"][0]["components"]["hedged_pair_net"]
    assert pair["inventory_leg"] == "BOOKED_MARK"
    assert pair["evidence_class"] == perf.MARKED_PAIR_ECONOMICS
    # with the inventory marked at its own price the residual IS measured
    assert ne["cycles"][0]["components"]["basis_pnl"]["value"] == pytest.approx(0.0)
    assert "never revalued" not in ne["verdict"]["statement"]


def _im_obs(opened_d=0, im=10_000.0):
    return {"cycle_opened_ms": T0 + opened_d * DAY, "position_im_usd": im}


def test_the_overlay_verdict_and_returns_do_not_follow_btc():
    """THE invariant: identical funding, fees and basis with BTC +5% or -5%
    give the same economic net, the same return on committed capital and the
    same verdict. Only the CASH figure (the net of an unhedged short) moves."""
    out = {}
    for name, close in (("rally", 105_000.0), ("fall", 95_000.0)):
        j = L.Journal()
        overlay(j, 0, 10, tag="a", close=close, revalue=True)
        out[name], _ = economics(j, obs=[_im_obs()])
    r, f = out["rally"], out["fall"]
    assert r["headline"]["net_usd"] == pytest.approx(300 - 110 - 5000)
    assert f["headline"]["net_usd"] == pytest.approx(300 - 110 + 5000)
    for ne in (r, f):
        assert ne["headline"]["economic_net_usd"] == pytest.approx(190.0)
        assert ne["headline"]["economic_net_basis"] == "COMPUTED"
        m = ne["metrics"]["return_on_committed_capital_pct"]
        # capital = observed margin 10,000 + the client's BTC at entry 100,000
        assert m["value"] == pytest.approx(100.0 * 190.0 / 110_000.0)
        assert m["numerator_basis"] == "COMPUTED"
        assert ne["verdict"]["economic_net_usd"] == pytest.approx(190.0)
        assert ne["verdict"]["booked_net_usd"] == pytest.approx(
            ne["headline"]["net_usd"])
    assert r["verdict"]["code"] == f["verdict"]["code"]
    assert r["verdict"]["code"] not in (
        "FUNDING_POSITIVE_BUT_NET_NEGATIVE", "NET_NEGATIVE")
    assert r["metrics"]["max_drawdown_usd"]["value"] == pytest.approx(
        f["metrics"]["max_drawdown_usd"]["value"])
    # the unhedged comparison is on the pair too: pair - unhedged = the hedge's
    # own increment, which is the cash net
    assert r["metrics"]["vs_unhedged_btc_hold"][
        "hedged_pair_minus_unhedged_usd"] == pytest.approx(190.0 - 5000.0)
    assert f["metrics"]["vs_unhedged_btc_hold"][
        "hedged_pair_minus_unhedged_usd"] == pytest.approx(190.0 + 5000.0)


def test_a_phantom_overlay_win_is_not_reported_as_a_return():
    """BTC falls 10% and the unhedged short 'wins' 10,000 in cash. The book's
    economic result is the pair's -ve/+ve few hundred dollars, never +10%."""
    j = L.Journal()
    overlay(j, 0, 10, tag="a", close=90_000.0, revalue=True, funding=20.0)
    ne, _ = economics(j, obs=[_im_obs()])
    assert ne["headline"]["net_usd"] == pytest.approx(20 - 110 + 10_000)
    assert ne["headline"]["economic_net_usd"] == pytest.approx(20 - 110)
    ret = ne["metrics"]["return_on_committed_capital_pct"]["value"]
    assert ret == pytest.approx(100.0 * (20 - 110) / 110_000.0)
    assert ret < 0, "funding 20 does not pay 110 of fees: it is not a +9% win"
    assert ne["verdict"]["code"] != "NET_POSITIVE"
    assert ne["verdict"]["economic_net_usd"] == pytest.approx(-90.0)


def test_an_overlay_without_a_pair_has_no_return_not_the_cash_return():
    j = L.Journal()
    j.post(L.Entry(ms=T0, kind="perp_open", ref="o",
                   lines=[L.Line(L.PERP, 0.0, qty=-1.0), L.Line(L.FEES, 55.0),
                          L.Line(L.USDT, -55.0, qty=-55.0)],
                   memo="not a readable memo", fee_status="confirmed"))
    j.post(L.Entry(ms=T0 + 5 * DAY, kind="perp_close", ref="c",
                   lines=[L.Line(L.PERP, 0.0, qty=1.0), L.Line(L.FEES, 55.0),
                          L.Line(L.USDT, -55.0, qty=-55.0)],
                   memo="not a readable memo", fee_status="confirmed"))
    ne, cycles = economics(j, obs=[_im_obs()])
    assert cycles[0]["overlay"] is True      # a perp with no spot leg bought
    assert ne["headline"]["economic_net_usd"] is None
    assert ne["verdict"]["code"] == "OVERLAY_PAIR_NOT_COMPUTABLE"
    for k in ("return_on_committed_capital_pct",
              "annualised_return_on_committed_capital_pct",
              "max_drawdown_usd", "expectancy_after_costs_usd"):
        assert ne["metrics"][k]["value"] is None, k
        assert "OVERLAY_PAIR_NOT_COMPUTABLE" in str(ne["metrics"][k]["why"]), k


def test_a_derived_overlay_book_publishes_no_return_and_no_statistics():
    """REVIEW FINDING (Round 16): with the inventory leg only DERIVED from perp
    prices the pair is funding - fees by construction, so its basis is
    unmeasured (counted as zero). A return on it overstated (+5.39%/yr annualised
    and +$295 expectancy per cycle in the reviewer's reproduction while the
    unmeasured basis was worth about -$300 per cycle). It is shown, labelled,
    as the derived pair; it is never a return, benchmark or statistics base."""
    j = L.Journal()
    for i in range(6):
        overlay(j, 20 * i, 20 * i + 10, tag=f"d{i}", funding=400.0,
                close=P0 * 1.00)
    cycles = perf.cycles_from_journal(j)
    obs = [{"cycle_opened_ms": perf._cycle_ms(c["cycle_id"]),
            "position_im_usd": 10_000.0} for c in cycles]
    ne, _ = economics(j, obs=obs)
    m = ne["metrics"]
    assert m["hedged_pair_net_usd"]["status"] == "DERIVED_NOT_BOOKED"
    assert m["hedged_pair_net_usd"]["value"] == pytest.approx(6 * (400 - 110))
    assert ne["headline"]["economic_net_usd"] is None
    for k in ("return_on_committed_capital_pct",
              "annualised_return_on_committed_capital_pct",
              "net_return_per_capital_day_pct", "expectancy_after_costs_usd",
              "max_drawdown_usd", "net_excluding_largest_winner_usd"):
        assert m[k]["value"] is None, k
        assert "OVERLAY_PAIR_DERIVED_NOT_BOOKED" in str(m[k]["why"]), k
    assert ne["headline"]["annualised_return_on_committed_capital_pct"] is None
    assert ne["verdict"]["code"] == "OVERLAY_PAIR_DERIVED_NOT_BOOKED"


def test_the_per_cycle_pair_carries_the_basis_when_the_marks_are_verified():
    """REVIEW FINDING (Round 16): `unhedged + increment` took the BTC's move
    from the perp fill prices, which cancels the price legs by construction and
    leaves the basis out. With verified marks the per-cycle pair is the MEASURED
    one (cash net + the booked inventory move): here the spot rose 4,000 while
    the perp the short lost 5,000, a basis of -1,000."""
    import performance_analytics as pa
    j = L.Journal()
    L.perp_open(j, ms=T0, ref="b-o", qty=1.0, price=P0, fee_usd=55.0,
                fee_status="confirmed")
    L.funding(j, ms=T0 + 5 * DAY, ref="b-f", usd=300.0, basis="venue",
              raw=_vraw("b-f", T0 + 5 * DAY, 300.0))
    L.revalue_inventory(
        j, ms=T0 + 10 * DAY - 1, ref="b-rv", qty=1.0, from_price=P0,
        to_price=P0 + 4_000.0,
        raw={"valuation": L.MARK_VALUATION, "qty": 1.0, "entry_spot": P0,
             "exit_spot": P0 + 4_000.0, "exit_mark_source": "tick_spot_mark",
             "exit_mark_observed_ms": T0 + 10 * DAY - 1,
             "fill_ms": T0 + 10 * DAY})
    L.perp_close(j, ms=T0 + 10 * DAY, ref="b-c", qty=1.0, price=P0 + 5_000.0,
                 entry_price=P0, fee_usd=55.0, fee_status="confirmed")
    _, cycles = economics(j, obs=[_im_obs()])
    c = cycles[0]
    assert c["mark_status"] == "VERIFIED"
    true_pair = 300 - 110 - 5_000 + 4_000
    assert perf.hedged_pair(c)["net_usd"] == pytest.approx(true_pair)
    opp = pa.overlay_opportunity_cost(c, benchmark_apr_pct=None)
    assert opp["vs_unhedged_btc"]["hedged_pair_pnl_usd"] == pytest.approx(true_pair)
    # without verified marks it stays the derived figure (basis-blind, labelled)
    c2 = dict(c, mark_status="UNVERIFIED")
    derived = pa.overlay_opportunity_cost(c2, benchmark_apr_pct=None)
    assert derived["vs_unhedged_btc"]["hedged_pair_pnl_usd"] == pytest.approx(
        5_000.0 + (300 - 110 - 5_000 + 0.0))


def test_a_derived_book_withholds_every_edge_cushion_and_return():
    """REVIEW FINDING (Round 16, second pass): pair_economics still used the
    derived pair for four positive figures (+1.77% on posted margin, +2.95 bps
    per notional-day, +0.98 bps of cushion over break-even, 29.5 bps of extra
    round trip to zero) while the unmeasured basis was about -$300 per cycle.
    The cost-recovery LEVELS stay (what must be earned, labelled basis-unmeasured);
    an edge, a cushion or a return on a derived pair does not."""
    j = L.Journal()
    for i in range(6):
        overlay(j, 20 * i, 20 * i + 10, tag=f"d{i}", funding=400.0)
    cycles = perf.cycles_from_journal(j)
    obs = [{"cycle_opened_ms": perf._cycle_ms(c["cycle_id"]),
            "position_im_usd": 10_000.0} for c in cycles]
    ne, _ = economics(j, obs=obs)
    m = ne["metrics"]
    for k in ("funding_margin_over_break_even_bps", "net_bps_per_notional_day",
              "pair_return_on_posted_margin_pct"):
        assert m[k]["value"] is None, k
        assert "OVERLAY_PAIR_DERIVED_NOT_BOOKED" in m[k]["why"], k
    s = m["execution_cost_sensitivity"]
    assert s["extra_round_trip_bps_to_zero_net"] is None
    assert s["fee_multiple_to_zero_net"] is None
    assert s["basis_pnl_included"] is False and s["cushion_withheld"]
    # the cost-recovery levels are still reported, labelled basis-unmeasured
    be = m["break_even_funding_bps_per_settlement"]
    assert be["value"] is not None and be["basis_pnl_included"] is False
    assert m["min_holding_days_to_cover_costs"]["value"] is not None


def test_a_verified_flat_mark_is_a_measured_pair_per_cycle():
    """REVIEW FINDING (Round 16, second pass): `if reval and ...` read a
    VERIFIED mark of exactly 0.0 (spot flat while the perp moved) as 'no mark',
    so the per-cycle pair stayed the basis-blind derived one (+190) while the
    measured pair is -160: a $350 basis loss hidden."""
    import performance_analytics as pa
    j = L.Journal()
    L.perp_open(j, ms=T0, ref="f-o", qty=1.0, price=P0, fee_usd=55.0,
                fee_status="confirmed")
    L.funding(j, ms=T0 + 5 * DAY, ref="f-f", usd=300.0, basis="venue",
              raw=_vraw("f-f", T0 + 5 * DAY, 300.0))
    L.revalue_inventory(
        j, ms=T0 + 10 * DAY - 1, ref="f-rv", qty=1.0, from_price=P0,
        to_price=P0,
        raw={"valuation": L.MARK_VALUATION, "qty": 1.0, "entry_spot": P0,
             "exit_spot": P0, "exit_mark_source": "tick_spot_mark",
             "exit_mark_observed_ms": T0 + 10 * DAY - 1,
             "fill_ms": T0 + 10 * DAY})
    L.perp_close(j, ms=T0 + 10 * DAY, ref="f-c", qty=1.0, price=P0 + 350.0,
                 entry_price=P0, fee_usd=55.0, fee_status="confirmed")
    _, cycles = economics(j, obs=[_im_obs()])
    c = cycles[0]
    assert c["mark_status"] == "VERIFIED" and c["revalue_entries"] == 1
    assert c["revalue_usd"] == 0.0
    true_pair = 300 - 110 - 350
    assert perf.hedged_pair(c)["net_usd"] == pytest.approx(true_pair)
    opp = pa.overlay_opportunity_cost(c, benchmark_apr_pct=None)
    u = opp["vs_unhedged_btc"]
    assert u["hedged_pair_pnl_usd"] == pytest.approx(true_pair)
    assert u["pair_basis"] == "MEASURED_MARK"


def test_the_per_cycle_benchmark_needs_a_measured_pair():
    """REVIEW FINDING (Round 16, second pass): a derived pair read 'beats 5%'
    (+$21 per cycle in the repro) with its basis counted as zero. Measured
    pairs (booked, verified marks) are still compared; derived ones are not."""
    import performance_analytics as pa
    j = L.Journal()
    overlay(j, 0, 10, tag="m", close=105_000.0, revalue=True)        # measured
    overlay(j, 20, 30, tag="d", close=105_000.0)                      # derived
    _, cycles = economics(j, obs=[_im_obs(0), _im_obs(20)])
    measured, derived = cycles
    ok = pa.overlay_opportunity_cost(measured, benchmark_apr_pct=5.0)
    assert ok["vs_benchmark_rate"]["available"] is True
    assert ok["vs_unhedged_btc"]["pair_basis"] == "MEASURED_MARK"
    no = pa.overlay_opportunity_cost(derived, benchmark_apr_pct=5.0)
    assert no["vs_benchmark_rate"]["available"] is False
    assert "OVERLAY_PAIR_DERIVED_NOT_BOOKED" in no["vs_benchmark_rate"]["why"]
    assert no["vs_unhedged_btc"]["pair_basis"] == "DERIVED_BASIS_UNMEASURED"


def test_the_overlay_increment_does_not_subtract_the_inventory_mark_twice():
    """The cycle's cash net already EXCLUDES the revaluation entry, so the
    hedge's increment is the cash net and pair = unhedged + increment is the
    hedged pair (cash + mark). Subtracting the mark again read the pair `mark`
    too low."""
    import performance_analytics as pa
    j = L.Journal()
    overlay(j, 0, 10, tag="a", close=105_000.0, revalue=True)
    _, cycles = economics(j, obs=[_im_obs()])
    c = cycles[0]
    assert c["revalue_usd"] == pytest.approx(5000.0)
    opp = pa.overlay_opportunity_cost(c, benchmark_apr_pct=None)
    assert opp["overlay_increment_usd"] == pytest.approx(c["net_usd"])
    assert opp["overlay_increment_usd"] == pytest.approx(300 - 110 - 5000)
    pair = opp["vs_unhedged_btc"]["hedged_pair_pnl_usd"]
    assert pair == pytest.approx(5000.0 + (300 - 110 - 5000))
    assert pair == pytest.approx(perf.hedged_pair(c)["net_usd"])


def test_an_acquire_cycle_already_holds_both_legs():
    j = L.Journal()
    acquire(j, 0, 10, tag="a", close=105_000.0)
    ne, cycles = economics(j)
    assert cycles[0]["overlay"] is False
    # funding 300 - four fills at 55 + (perp -5000) + (spot +5000)
    assert ne["headline"]["net_usd"] == pytest.approx(80.0)
    assert ne["metrics"]["hedged_pair_net_usd"]["value"] == pytest.approx(80.0)
    assert ne["cycles"][0]["components"]["hedged_pair_net"]["inventory_leg"] \
        == "SPOT_LEG_BOOKED"


# -- an acquire cycle is a PAIR only if its spot leg is flat and hedge-sized ----
# Round 17 red team: an acquire cycle whose perp closed while the spot sale was
# refused, partial or never made has its booked cash net WITHOUT the BTC's move,
# so a 5% fall read as +$5,000 per BTC under the strongest verdict code.

def acquire_shaped(j, d0, d1, *, tag, close=P0, buy=1.0, perp=1.0, sell=1.0,
                   funding=300.0):
    """An ACQUIRE cycle with its own spot purchase, perp short and spot sale (BTC
    quantities); `sell=0` is the perp closed while the spot leg is still held."""
    L.spot_buy(j, ms=T0 + d0 * DAY, ref=f"{tag}-sb", qty=buy, price=P0,
               fee_btc=0.0, fee_usdt=55.0, fee_status="confirmed")
    L.perp_open(j, ms=T0 + d0 * DAY + 1, ref=f"{tag}-o", qty=perp, price=P0,
                fee_usd=55.0, fee_status="confirmed")
    L.funding(j, ms=T0 + ((d0 + d1) // 2) * DAY, ref=f"{tag}-f", usd=funding,
              basis="venue", raw=_vraw(f"{tag}-f", T0 + ((d0 + d1) // 2) * DAY, funding))
    L.perp_close(j, ms=T0 + d1 * DAY, ref=f"{tag}-c", qty=perp, price=close,
                 entry_price=P0, fee_usd=55.0, fee_status="confirmed")
    if sell:
        L.spot_sell(j, ms=T0 + d1 * DAY + 1, ref=f"{tag}-ss", qty=sell,
                    price=close, cost_basis=P0 * sell, fee_usd=55.0,
                    fee_status="confirmed")


UNFLAT_SHAPES = [
    pytest.param(dict(sell=0.0), id="spot_never_sold_back"),
    pytest.param(dict(sell=0.9), id="spot_partly_sold_back"),
    pytest.param(dict(buy=0.5, perp=1.0, sell=0.5), id="spot_leg_smaller_than_hedge"),
    pytest.param(dict(buy=1.0, perp=0.5, sell=1.0), id="spot_leg_larger_than_hedge"),
]


@pytest.mark.parametrize("shape", UNFLAT_SHAPES)
def test_an_acquire_cycle_whose_spot_leg_is_not_flat_has_no_pair_and_no_return(shape):
    j = L.Journal()
    acquire_shaped(j, 0, 10, tag="a", close=95_000.0, **shape)
    ne, cycles = economics(j, obs=[_im_obs()])
    c = cycles[0]
    assert c["overlay"] is False and c["outcome"] == "CLOSED"
    pair = perf.hedged_pair(c)
    assert pair["available"] is False
    assert "SPOT_LEG_NOT_FLAT" in pair["why"]
    assert c["economic_net_usd"] is None
    assert c["economic_net_basis"] == "PAIR_NOT_COMPUTABLE"
    # the CASH is still reported as cash (it is what the account booked) ...
    assert ne["headline"]["net_usd"] == pytest.approx(c["net_usd"])
    # ... but it is not a result, a return, a benchmark base or a verdict
    assert ne["headline"]["economic_net_usd"] is None
    v = ne["verdict"]
    assert v["code"] == "ACQUIRE_SPOT_LEG_NOT_FLAT"
    assert not v["code"].startswith("NET_POSITIVE")
    assert v["economic_net_usd"] is None
    assert "no net result is claimed" in v["statement"]
    for k in ("return_on_committed_capital_pct",
              "annualised_return_on_committed_capital_pct",
              "net_return_per_capital_day_pct", "expectancy_after_costs_usd",
              "max_drawdown_usd", "net_excluding_largest_winner_usd"):
        assert ne["metrics"][k]["value"] is None, k
        assert "ACQUIRE_SPOT_LEG_NOT_FLAT" in str(ne["metrics"][k]["why"]), k
    sm = perf.summarise(cycles)
    for k in ("return_on_peak_notional_pct", "win_rate", "max_drawdown_usd",
              "best_cycle_share_of_winning_net_pct",
              "annualised_on_notional_in_market_pct"):
        assert sm[k] is None, k
    assert sm["economic_net_usd"] is None
    assert "PAIR_NOT_COMPUTABLE" in sm["returns_withheld_why"]
    assert sm["net_usd_confirmed"] == pytest.approx(c["net_usd"])   # cash kept as cash


def test_the_cash_of_an_unsold_spot_leg_follows_btc_but_the_economic_net_is_unstated():
    """The reproduction: moving the exit +/-5% moves the booked cash 1:1 (the
    phantom), while the economic net stays unstated, never a number that moves
    with BTC."""
    cash = {}
    for close in (95_000.0, 100_000.0, 105_000.0):
        j = L.Journal()
        acquire_shaped(j, 0, 10, tag="a", close=close, sell=0.0)
        c = perf.cycles_from_journal(j)[0]
        cash[close] = c["net_usd"]
        assert c["economic_net_usd"] is None
        assert perf.net_verdict is not None
    assert cash[95_000.0] - cash[105_000.0] == pytest.approx(10_000.0)
    assert cash[95_000.0] == pytest.approx(300 - 3 * 55 + 5_000.0)   # the phantom +5,000


@pytest.mark.parametrize("close", [95_000.0, 100_000.0, 105_000.0])
def test_a_flat_acquire_pair_and_a_measured_overlay_do_not_follow_the_btc_price(close):
    ja, jo = L.Journal(), L.Journal()
    acquire(ja, 0, 10, tag="a", close=close)
    overlay(jo, 0, 10, tag="o", close=close, revalue=True)
    ca = perf.cycles_from_journal(ja)[0]
    co = perf.cycles_from_journal(jo)[0]
    assert ca["economic_net_usd"] == pytest.approx(300 - 4 * 55)      # funding - fees
    assert ca["economic_net_basis"] == "SPOT_LEG_BOOKED"
    assert co["economic_net_usd"] == pytest.approx(300 - 2 * 55)
    assert co["economic_net_basis"] == "BOOKED_MARK"


def test_a_hedge_within_the_engines_delta_band_is_still_a_pair():
    """The perp short is snapped to the lot, so it can sit a little under the BTC
    held: inside the engine's DELTA_BAND (|spot - perp| / both legs, as
    `CarryPosition.delta_fraction`) that is a pair, outside it is not. The boundary
    is the engine's own, not half of it."""
    for perp, is_pair in ((0.99, True), (0.961, True), (0.96, False), (0.95, False),
                          (1.04, True), (1.041, False)):
        j = L.Journal()
        acquire_shaped(j, 0, 10, tag="a", close=95_000.0, buy=1.0, perp=perp, sell=1.0)
        got = perf.cycles_from_journal(j)[0]["economic_net_usd"]
        assert (got is not None) is is_pair, (perp, got)


@pytest.mark.parametrize("spot,perp", [(1.0, 0.99), (1.0, 0.961), (1.0, 0.96), (1.0, 0.9), (0.9, 1.0), (1.0, 1.04), (1.0, 1.041),
                                      (0.5, 1.0), (0.0015, 0.001), (0.0016, 0.001), (0.002, 0.001)])
def test_the_pair_boundary_is_exactly_the_engines_own_delta_fraction(spot, perp):
    """The same quantities through `CarryPosition.delta_fraction` (the number that trips the engine's rebalance): the engine calls them a hedge iff its
    fraction is within the band, and so does the report. A pair the engine would accept is never withheld; one it would rebalance is never a pair."""
    import carry_engine as CE
    price = 100_000.0
    pos = CE.CarryPosition(
        spot=CE.Leg(symbol="BTCUSDT", side="Buy", product="spot", requested_qty=spot, filled_qty=spot, avg_price=price),
        perp=CE.Leg(symbol="BTCUSDT", side="Sell", product="linear", requested_qty=perp, filled_qty=perp, avg_price=price))
    engine_says_hedge = pos.delta_fraction(price) <= CE.DELTA_BAND
    j = L.Journal()
    acquire_shaped(j, 0, 10, tag="a", close=95_000.0, buy=spot, perp=perp, sell=spot)
    report_says_pair = perf.cycles_from_journal(j)[0]["economic_net_usd"] is not None
    assert report_says_pair is engine_says_hedge, (spot, perp, pos.delta_fraction(price))


def test_the_acquire_hedge_tolerance_is_the_engines_delta_band():
    import carry_engine
    assert perf.ACQUIRE_HEDGE_TOLERANCE == carry_engine.DELTA_BAND


def test_an_acquire_cycle_with_an_unreadable_quantity_has_no_pair():
    j = L.Journal()
    L.spot_buy(j, ms=T0, ref="sb", qty=1.0, price=P0, fee_btc=0.0,
               fee_usdt=55.0, fee_status="confirmed")
    j.post(L.Entry(ms=T0 + 1, kind="perp_open", ref="o",
                   lines=[L.Line(L.PERP, 0.0, qty=-1.0), L.Line(L.FEES, 55.0),
                          L.Line(L.USDT, -55.0, qty=-55.0)],
                   memo="not a readable memo", fee_status="confirmed"))
    j.post(L.Entry(ms=T0 + 5 * DAY, kind="perp_close", ref="c",
                   lines=[L.Line(L.PERP, 0.0, qty=1.0), L.Line(L.FEES, 55.0),
                          L.Line(L.USDT, -55.0, qty=-55.0)],
                   memo="not a readable memo", fee_status="confirmed"))
    L.spot_sell(j, ms=T0 + 5 * DAY + 1, ref="ss", qty=1.0, price=P0,
                cost_basis=P0, fee_usd=55.0, fee_status="confirmed")
    c = perf.cycles_from_journal(j)[0]
    assert c["economic_net_usd"] is None


# -- the default text report and the per-cycle rows follow the economic net ----

def test_a_derived_overlay_text_report_prints_no_return_and_no_win_rate():
    j = L.Journal()
    for i in range(3):
        overlay(j, 20 * i, 20 * i + 10, tag=f"d{i}", funding=400.0)   # no mark
    cycles = perf.cycles_from_journal(j)
    perf.attach_capital(cycles, [
        {"cycle_opened_ms": perf._cycle_ms(c["cycle_id"]),
         "position_im_usd": 10_000.0} for c in cycles])
    for c in cycles:
        assert c["economic_net_usd"] is None
        assert c["economic_net_basis"] == "DERIVED_NOT_BOOKED"
        assert c["return_on_committed_capital_pct"] is None   # never the cash return
    s = perf.summarise(cycles)
    assert s["net_usd_confirmed"] == pytest.approx(3 * (400 - 110))   # cash kept
    for k in ("return_on_peak_notional_pct", "period_return_on_peak_capital_pct",
              "annualised_on_notional_in_market_pct", "win_rate",
              "max_drawdown_usd", "best_cycle_share_of_winning_net_pct"):
        assert s[k] is None, k
    assert "DERIVED_NOT_BOOKED" in s["returns_withheld_why"]
    text = "\n".join(perf.report_lines(cycles, s))
    assert "economic net (the hedged pair): n/a" in text
    assert "return on it n/a" in text
    assert "win rate n/a" in text and "max drawdown (closed cycles) n/a" in text
    assert "returns, win rate and drawdown: n/a - " in text
    assert "$n/a" not in text and "n/a%" not in text
    assert "no return on capital is shown" in text \
        or "committed capital: not available" in text


def test_a_measured_book_still_prints_its_returns():
    j = L.Journal()
    for i in range(3):
        overlay(j, 20 * i, 20 * i + 10, tag=f"m{i}", funding=400.0,
                revalue=True)
    cycles = perf.cycles_from_journal(j)
    s = perf.summarise(cycles)
    assert s["returns_withheld_why"] is None
    assert s["economic_net_usd"] == pytest.approx(3 * (400 - 110))
    assert s["win_rate"] == 1.0
    assert s["return_on_peak_notional_pct"] == pytest.approx(
        100 * 3 * 290 / 100_000.0)
    text = "\n".join(perf.report_lines(cycles, s))
    assert "n/a" not in text.split("\n")[1]       # the net line has every figure


def test_a_cycle_without_readable_perp_prices_gets_no_pair_not_zero():
    j = L.Journal()
    j.post(L.Entry(ms=T0, kind="perp_open", ref="o",
                   lines=[L.Line(L.PERP, 0.0, qty=-1.0), L.Line(L.FEES, 55.0),
                          L.Line(L.USDT, -55.0, qty=-55.0)],
                   memo="not a readable memo", fee_status="confirmed"))
    j.post(L.Entry(ms=T0 + 5 * DAY, kind="perp_close", ref="c",
                   lines=[L.Line(L.PERP, 0.0, qty=1.0), L.Line(L.FEES, 55.0),
                          L.Line(L.USDT, -55.0, qty=-55.0)],
                   memo="not a readable memo", fee_status="confirmed"))
    ne, _ = economics(j)
    m = ne["metrics"]["hedged_pair_net_usd"]
    assert m["value"] is None and m["evidence_class"] == perf.UNAVAILABLE
    assert m["why"]
    for k in ("break_even_funding_bps_per_settlement",
              "min_holding_days_to_cover_costs",
              "execution_cost_sensitivity"):
        assert ne["metrics"][k]["value"] is None, k
        assert ne["metrics"][k]["why"], k


# -- break-evens, edge, efficiency -------------------------------------------

def test_break_even_funding_holding_period_and_required_edge():
    j = L.Journal()
    # a MEASURED pair (the BTC marked with the perp: pair net 190): the edge,
    # cushion and return figures below need a measured basis
    overlay(j, 0, 10, tag="a", close=105_000.0, revalue=True)
    ne, _ = economics(j)
    M = ne["metrics"]
    # 30 settlements on a 100k notional: funding 300 = 1.0 bps per settlement
    assert M["observed_funding_bps_per_settlement"]["value"] == pytest.approx(1.0)
    # fees 110 = 11 bps of notional; break-even = 11 / 30 settlements
    be = M["break_even_funding_bps_per_settlement"]
    assert be["value"] == pytest.approx(11.0 / 30.0)
    assert be["basis_pnl_included"] is True
    assert M["funding_margin_over_break_even_bps"]["value"] == pytest.approx(
        1.0 - 11.0 / 30.0)
    # 3 bps/day of funding pays back an 11 bps fixed cost in 11/3 days
    assert M["min_holding_days_to_cover_costs"]["value"] == pytest.approx(11.0 / 3.0)
    # at the observed median hold (10 d) the edge needed per settlement
    assert M["min_required_funding_edge_bps_per_settlement"]["value"] == \
        pytest.approx(11.0 / 30.0)
    # capital efficiency: 190 over 100k x 10 notional-days = 1.9 bps/day
    assert M["net_bps_per_notional_day"]["value"] == pytest.approx(1.9)


def test_funding_that_does_not_cover_financing_has_no_holding_period():
    j = L.Journal()
    overlay(j, 0, 10, tag="a", funding=-20.0)
    ne, _ = economics(j)
    h = ne["metrics"]["min_holding_days_to_cover_costs"]
    assert h["value"] is None and "NEVER" in h["why"]


def test_financing_enters_the_break_even_per_day_not_as_a_fixed_cost():
    j = L.Journal()
    overlay(j, 0, 10, tag="a")
    j2 = L.Journal()
    overlay(j2, 0, 10, tag="a", borrow=100.0)     # same cycle, 100 financing
    base, _ = economics(j)
    fin, _ = economics(j2)
    # break-even funding rises by the financing per settlement: 100/(100k*30)
    d = (fin["metrics"]["break_even_funding_bps_per_settlement"]["value"]
         - base["metrics"]["break_even_funding_bps_per_settlement"]["value"])
    assert d == pytest.approx(100.0 / (P0 * 30) * 1e4)
    # funding net of financing now earns 2 bps/day against the same 11 bps
    assert fin["metrics"]["min_holding_days_to_cover_costs"]["value"] == \
        pytest.approx(11.0 / 2.0)


# -- sensitivity ---------------------------------------------------------------

def test_execution_cost_sensitivity_is_arithmetic_on_stated_shocks():
    j = L.Journal()
    # measured pair net 190, fees 110
    overlay(j, 0, 10, tag="a", close=105_000.0, revalue=True)
    ne, _ = economics(j)
    s = ne["metrics"]["execution_cost_sensitivity"]
    assert s["evidence_class"] == perf.HISTORICAL_SIMULATION
    assert s["status"] == "SENSITIVITY_NOT_MEASUREMENT"
    assert s["base_net_usd"] == pytest.approx(190.0)
    by_bps = {r["extra_round_trip_bps"]: r["net_usd"] for r in s["extra_bps"]}
    assert by_bps[0.0] == pytest.approx(190.0)
    assert by_bps[10.0] == pytest.approx(90.0)
    by_mult = {r["fee_multiple"]: r["net_usd"] for r in s["fee_multiple"]}
    assert by_mult[2.0] == pytest.approx(80.0)
    assert s["extra_round_trip_bps_to_zero_net"] == pytest.approx(19.0)
    assert s["fee_multiple_to_zero_net"] == pytest.approx(1.0 + 190.0 / 110.0)


def test_a_losing_book_has_no_cushion_to_report():
    j = L.Journal()
    overlay(j, 0, 10, tag="a", funding=50.0)          # pair net -60
    ne, _ = economics(j)
    s = ne["metrics"]["execution_cost_sensitivity"]
    assert s["base_net_usd"] == pytest.approx(-60.0)
    assert s["extra_round_trip_bps_to_zero_net"] is None
    assert s["fee_multiple_to_zero_net"] is None


def test_no_closed_cycle_means_every_new_metric_is_unavailable():
    ne, _ = economics(L.Journal())
    for k in ("hedged_pair_net_usd", "break_even_funding_bps_per_settlement",
              "min_holding_days_to_cover_costs",
              "min_required_funding_edge_bps_per_settlement",
              "net_bps_per_notional_day", "execution_cost_sensitivity",
              "observed_funding_bps_per_settlement"):
        assert ne["metrics"][k]["value"] is None, k
        assert ne["metrics"][k]["evidence_class"] == perf.UNAVAILABLE, k
        assert ne["metrics"][k]["why"], k


# -- through the whole report --------------------------------------------------

def test_the_report_carries_the_pair_metrics_under_the_paper_class(tmp_path):
    import json
    from persistence import StateStore
    j = L.Journal()
    overlay(j, 0, 10, tag="a", close=105_000.0)
    db = str(tmp_path / "s.db")
    store = StateStore(db)
    store.save_ledger(j.to_rows())
    store.record_execution_environment("testnet")
    for c in perf.cycles_from_journal(j):
        store.record_risk_observation({
            "ms": perf._cycle_ms(c["cycle_id"]) + 1, "symbol": "BTCUSDT",
            "cycle_opened_ms": perf._cycle_ms(c["cycle_id"]),
            "position_im_usd": 10_000.0})
    store.close()
    r = perf.build_report(db, environment="testnet", now_ms=T0 + 60 * DAY)
    json.dumps(r, allow_nan=False)                       # JSON-safe
    ne = r["sections"]["net_economics"]
    assert ne["evidence_class"] == perf.PAPER_EXECUTION
    assert r["headline"]["hedged_pair_net_usd"] == pytest.approx(190.0)
    assert r["headline"]["net_usd"] == pytest.approx(-4810.0)
    m = ne["metrics"]
    # the inventory leg here is only DERIVED (basis unmeasured): no return on it
    assert m["pair_return_on_posted_margin_pct"]["value"] is None
    assert "OVERLAY_PAIR_DERIVED_NOT_BOOKED" in m["pair_return_on_posted_margin_pct"]["why"]
    # nothing in it claims cash
    assert r["sections"]["realized_cash"]["evidence_class"] == perf.UNAVAILABLE
    # the evidence feed reads the pair figure off the detail cycle
    detail = r["sections"]["ledger_detail"]["cycles"][0]
    assert detail["hedged_pair_net_usd"] == pytest.approx(190.0)
    assert detail["net_usd"] == pytest.approx(-4810.0)
    assert detail["hedged_pair_inventory_leg"] == "DERIVED_FROM_PERP_FILL_PRICES"
