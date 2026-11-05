"""The live order book inside the REAL entry-benefit gate.

Own-fill history is the stronger evidence about what execution cost; the live book
is the only evidence about what it costs NOW. The gate uses the worse of the two
where both exist, the live book (plus the owner's latency allowance) where it is
the only one, and refuses where a required input is missing."""
from __future__ import annotations

import pytest

import entry_benefit_gate as G
import execution_cost_estimate as EST
import md_broker as MB
from test_md_integration import broker_for, complete_state, snapshot as take, frozen_time  # noqa: F401

RICH = [6.0, 5.8, 6.1, 5.9, 6.0, 6.2]


def check(gate, snap, *, history=RICH, mark=None, spot=None, entries=()):
    return gate.check(
        now_ms=int(snap.observed_at_s * 1000), entries=list(entries), execution_mode="overlay",
        perp_symbol="BTCUSDT", spot_symbol="BTCUSDT", mark=mark or snap.perp_mark, spot=spot or snap.spot_mark,
        funding_history=history, last_print_ms=snap.funding_print_ms, round_trip_bps=11.0,
        borrow_apr=0.0, opportunity_cost_apr=0.0, notional_usd=100.0, snapshot=snap)


@pytest.fixture
def snap(frozen_time):
    st, now, wall = complete_state()
    frozen_time(wall)
    b, _ = broker_for(st, now, wall)
    return take(b, wall)


def test_the_snapshot_carries_the_displayed_book(snap):
    d = snap.detail["depth"]
    assert set(d) == {"perp_bids", "perp_asks", "spot_bids", "spot_asks"}
    assert len(d["perp_bids"]) >= 20 and d["perp_bids"][0][0] < d["perp_asks"][0][0]


def test_without_a_latency_allowance_the_live_book_alone_is_not_a_cost(snap):
    r = check(G.EntryBenefitGate(mode=G.ENFORCE), snap)
    assert r.reason == G.BENEFIT_UNAVAILABLE and not r.allowed
    live = r.trace["inputs"]["execution_cost"]["live_depth"]
    assert live["status"] == "UNAVAILABLE" and "LATENCY_ALLOWANCE_NOT_SUPPLIED" in live["reasons"]
    assert live["used"] is False and live["spread_bps"] is not None        # shown, not used


def test_with_an_allowance_the_live_book_prices_execution_and_only_evidence_is_missing(snap):
    r = check(G.EntryBenefitGate(mode=G.ENFORCE, latency_allowance_bps=2.0), snap)
    ec = r.trace["inputs"]["execution_cost"]
    assert ec["cost_source"] == "BOOK_WALK_NO_LATENCY" and ec["live_depth"]["used"] is True
    assert ec["slippage_bps"] == pytest.approx(ec["live_depth"]["slippage_bps"] + 2.0)
    assert r.trace["layers"]["entry_benefit"]["passed"] is True
    assert r.reason == G.EVIDENCE_INSUFFICIENT                              # no strategy-evidence verdict exists


def test_thin_funding_is_still_refused_on_the_live_book_costs(snap):
    r = check(G.EntryBenefitGate(mode=G.ENFORCE, latency_allowance_bps=2.0), snap,
              history=[1.0, 0.9, 1.2, 0.8, 1.1, 1.0])
    assert r.reason == G.BENEFIT_NOT_SUPPORTED and not r.allowed


def test_where_both_exist_the_worse_cost_wins(snap, monkeypatch):
    def own(entries, **kw):
        return {"status": EST.MEASURED, "legs": [], "spread_bps": 5.0, "slippage_bps": 7.0, "reasons": [],
                "policy": {}, "emergency_exit": {}}
    monkeypatch.setattr(G._est, "estimate_round_trip", own)
    r = check(G.EntryBenefitGate(mode=G.ENFORCE, latency_allowance_bps=2.0), snap)
    ec = r.trace["inputs"]["execution_cost"]
    assert ec["cost_source"] == "WORSE_OF_OWN_FILLS_AND_LIVE_BOOK"
    assert ec["spread_bps"] == 5.0 and ec["slippage_bps"] == 7.0              # own fills were worse
    monkeypatch.setattr(G._est, "estimate_round_trip",
                        lambda e, **kw: {"status": EST.MEASURED, "legs": [], "spread_bps": 0.0,
                                         "slippage_bps": 0.0, "reasons": [], "policy": {}, "emergency_exit": {}})
    ec = check(G.EntryBenefitGate(mode=G.ENFORCE, latency_allowance_bps=3.0), snap).trace[
        "inputs"]["execution_cost"]
    assert ec["spread_bps"] == pytest.approx(ec["live_depth"]["spread_bps"])
    assert ec["slippage_bps"] == pytest.approx(ec["live_depth"]["slippage_bps"] + 3.0)  # the book was worse


def test_a_snapshot_without_depth_leaves_the_gate_exactly_as_it_was(snap):
    snap.detail.pop("depth")
    r = check(G.EntryBenefitGate(mode=G.ENFORCE, latency_allowance_bps=2.0), snap)
    assert r.reason == G.BENEFIT_UNAVAILABLE and r.trace["inputs"]["execution_cost"]["live_depth"] is None
    assert G.EntryBenefitGate().fingerprint() != G.EntryBenefitGate(latency_allowance_bps=2.0).fingerprint()


def test_a_book_too_thin_for_the_size_is_not_extrapolated(snap):
    gate = G.EntryBenefitGate(mode=G.ENFORCE, latency_allowance_bps=2.0)
    r = gate.check(now_ms=int(snap.observed_at_s * 1000), entries=[], execution_mode="overlay",
                   perp_symbol="BTCUSDT", spot_symbol="BTCUSDT", mark=snap.perp_mark, spot=snap.spot_mark,
                   funding_history=RICH, last_print_ms=snap.funding_print_ms, round_trip_bps=11.0,
                   borrow_apr=0.0, opportunity_cost_apr=0.0, notional_usd=5e8, snapshot=snap)
    live = r.trace["inputs"]["execution_cost"]["live_depth"]
    assert live["status"] == "UNAVAILABLE" and any("DEPTH_INSUFFICIENT" in x for x in live["reasons"])
    assert r.reason == G.BENEFIT_UNAVAILABLE


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf"), "2", True])
def test_an_invalid_latency_allowance_is_refused_at_construction(bad):
    with pytest.raises(ValueError):
        G.EntryBenefitGate(latency_allowance_bps=bad)
