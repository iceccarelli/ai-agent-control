"""The replay must not skip the liquidation gate production runs.

`main.build_bot` builds the engine with `require_liquidation_check=True`; the
replay built it without, so the 4-year result was produced by an engine that
could never leave a short for being too close to liquidation - including the
192-day hold that supplied most of the profit. The corpus has no `liqPrice`,
leverage, collateral or `accountMMRate`, so the replay cannot evaluate that
gate on its own and must not look as if it did. These tests drive the real
`CarryEngine` through `replay()`.
"""
from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import carry_backtest as cb          # noqa: E402
import carry_engine                  # noqa: E402
import carry_replay as rp            # noqa: E402
from test_carry_replay import H8, START, rows  # noqa: E402

FLOOR = carry_engine.MIN_LIQUIDATION_DISTANCE_PCT


def entry_of(data):
    """Where the engine opens on this series, found by running it."""
    report = rp.replay(data, notional=100_000.0, borrow_apr=0.0)
    return report["trade_rows"][0]["entry_index"], \
        report["trade_rows"][0]["entry_perp"]


def with_spike(data, index, *, high_mult, entry):
    """Same close, a much higher high: the settlement close looks safe."""
    out = list(data)
    r = out[index]
    out[index] = cb.Settlement(ms=r.ms, perp=r.perp, spot=r.spot, rate=r.rate,
                               perp_high=entry * high_mult,
                               perp_low=r.perp_low)
    return out


# -- 1. no evidence: refuse to claim -------------------------------------------

def test_without_evidence_the_gate_is_not_evaluated_and_the_run_says_so():
    r = rp.replay(rows(60), notional=100_000.0, borrow_apr=0.0)
    liq = r["liquidation"]
    assert liq["gate"] == "NOT_EVALUATED"
    assert liq["production_equivalent"] is False
    assert liq["engine_require_liquidation_check"] is False
    assert any("not evaluated" in w for w in liq["why_not_production_equivalent"])
    assert any("liquidation" in n for n in r["not_modelled"])


def test_requiring_evidence_builds_the_engine_as_production_does_and_halts():
    """An absent input is an unreadable liquidation price: the engine halts on
    the first held tick, exactly as it does live. It is never a safe value."""
    r = rp.replay(rows(60), notional=100_000.0, borrow_apr=0.0,
                  require_liquidation_evidence=True)
    liq = r["liquidation"]
    assert liq["gate"] == "REQUIRED_EVIDENCE_MISSING"
    assert liq["engine_require_liquidation_check"] is True
    assert liq["production_equivalent"] is False
    assert r["halted_at_ms"] is not None
    assert r["reasons"].get("LIQUIDATION_PRICE_UNREADABLE", 0) >= 1
    assert r["trades"] == 1 and r["trade_rows"][0]["reason"] == "OPEN_AT_END"


def test_the_engine_flag_matches_production_when_evidence_is_given():
    import inspect
    import main
    assert "require_liquidation_check=True" in inspect.getsource(main.build_bot)
    ev = rp.LiquidationInputs.declared_isolated(1.0, "test scenario")
    r = rp.replay(rows(60), notional=100_000.0, borrow_apr=0.0, liquidation=ev)
    assert r["liquidation"]["engine_require_liquidation_check"] is True
    assert r["liquidation"]["floor_pct"] == FLOOR


# -- 2. evidence supplied: the production gate, on the adverse price ------------

def test_a_safe_close_cannot_hide_an_adverse_high_that_crosses_the_floor():
    base = rows(60)
    idx, entry = entry_of(base)
    spike_at = idx + 5
    data = with_spike(base, spike_at, high_mult=1.40, entry=entry)
    # 2x isolated: liquidation at 1.5 x entry. The close is ~entry (distance
    # ~50%); the high 1.40 x entry is 7.1% from liquidation: below the floor.
    ev = rp.LiquidationInputs.declared_isolated(2.0, "test scenario")

    closed_only = rp.replay(base, notional=100_000.0, borrow_apr=0.0,
                            liquidation=ev)
    hidden = rp.replay(data, notional=100_000.0, borrow_apr=0.0, liquidation=ev)

    assert closed_only["trade_rows"][0]["reason"] == "OPEN_AT_END"
    t = hidden["trade_rows"][0]
    assert t["reason"].startswith("LIQUIDATION_") and \
        t["reason"].endswith("PCT_AWAY_BELOW_FLOOR")
    assert t["closed_ms"] == data[spike_at].ms
    assert hidden["liquidation"]["intrabar_trips"] == 1
    assert hidden["liquidation"]["min_distance_pct"] < FLOOR
    # the close alone was safe: that is the whole point
    assert 100 * (1.5 * entry - data[spike_at].perp) / data[spike_at].perp > FLOOR


def test_the_exit_is_priced_where_the_floor_was_crossed_not_at_the_close():
    base = rows(60)
    idx, entry = entry_of(base)
    data = with_spike(base, idx + 5, high_mult=1.40, entry=entry)
    ev = rp.LiquidationInputs.declared_isolated(2.0, "test scenario")
    t = rp.replay(data, notional=100_000.0, borrow_apr=0.0,
                  liquidation=ev)["trade_rows"][0]
    crossing = 1.5 * entry / (1.0 + FLOOR / 100.0)
    assert t["exit_perp"] == pytest.approx(crossing, rel=1e-9)
    assert t["exit_perp"] > data[idx + 5].perp          # worse than the close
    assert t["exit_perp"] < data[idx + 5].perp_high


def test_a_gap_beyond_the_crossing_is_priced_at_the_worst_traded_price():
    base = rows(60)
    idx, entry = entry_of(base)
    out = list(base)
    k = idx + 5
    r = out[k]
    # the whole interval trades beyond the crossing: where inside it the floor
    # was crossed cannot be placed, so the high is taken
    out[k] = cb.Settlement(ms=r.ms, perp=entry * 1.35, spot=entry * 1.35,
                           rate=r.rate, perp_high=entry * 1.40,
                           perp_low=entry * 1.33)
    ev = rp.LiquidationInputs.declared_isolated(2.0, "test scenario")
    t = rp.replay(out, notional=100_000.0, borrow_apr=0.0,
                  liquidation=ev)["trade_rows"][0]
    assert t["reason"].startswith("LIQUIDATION_")
    assert t["exit_perp"] == pytest.approx(entry * 1.40, rel=1e-9)


def test_a_gate_that_never_binds_leaves_the_accounting_untouched():
    """Ordinary replay accounting is unchanged: with evidence the gate never
    trips on, every figure equals the run without it."""
    data = rows(120)
    plain = rp.replay(data, notional=100_000.0, borrow_apr=0.0)
    far = rp.LiquidationInputs.declared_isolated(1.0, "test scenario")
    gated = rp.replay(data, notional=100_000.0, borrow_apr=0.0, liquidation=far)
    assert gated["liquidation"]["intrabar_trips"] == 0
    for key in ("trades", "orders", "net_usd", "funding_usd", "fees_usd",
                "basis_usd"):
        assert gated[key] == plain[key], key
    assert plain["trades"] == 1
    assert plain["net_usd"] == pytest.approx(7046.866126, abs=1e-5)


def test_default_accounting_is_unchanged_on_a_churning_series():
    r = rp.replay(rows(200, cycle=6), notional=100_000.0, borrow_apr=0.0)
    assert r["trades"] == 20 and r["orders"] == 40
    # 0051: moved from +75.573143 on purpose. The entry is now `entry_gate`
    # (evaluate_entry + the measured-hold check), so this synthetic series
    # re-enters on different prints. A synthetic series; it is not evidence.
    assert r["net_usd"] == pytest.approx(-1064.341114, abs=1e-5)


# -- 3. unusable evidence is a refusal, never a safe value -----------------------

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -5.0, "x"])
def test_an_unusable_liquidation_price_halts_the_engine(bad):
    ev = rp.LiquidationInputs(kind="declared", source="test",
                              liq_price=lambda entry, bar: bad)
    r = rp.replay(rows(60), notional=100_000.0, borrow_apr=0.0, liquidation=ev)
    assert r["reasons"].get("LIQUIDATION_PRICE_UNREADABLE", 0) >= 1
    assert r["halted_at_ms"] is not None


def test_a_bar_whose_range_excludes_its_close_halts_the_engine():
    base = rows(60)
    idx, _entry = entry_of(base)
    bad = list(base)
    r = bad[idx + 3]
    bad[idx + 3] = cb.Settlement(ms=r.ms, perp=r.perp, spot=r.spot, rate=r.rate,
                                 perp_high=r.perp * 0.9, perp_low=r.perp * 0.8)
    ev = rp.LiquidationInputs.declared_isolated(2.0, "test scenario")
    out = rp.replay(bad, notional=100_000.0, borrow_apr=0.0, liquidation=ev)
    assert out["reasons"].get("LIQUIDATION_PRICE_UNREADABLE", 0) >= 1


def test_an_explicit_unreachable_price_is_counted_not_assumed():
    ev = rp.LiquidationInputs(kind="recorded", source="test recording",
                              liq_price=lambda entry, bar: None)
    r = rp.replay(rows(60), notional=100_000.0, borrow_apr=0.0, liquidation=ev)
    assert r["liquidation"]["unreachable_ticks"] >= 1
    assert r["trade_rows"][0]["reason"] == "OPEN_AT_END"


def test_the_account_margin_rate_floor_is_enforced_when_supplied():
    ev = rp.LiquidationInputs(kind="declared", source="test",
                              liq_price=lambda entry, bar: None,
                              account_mm_rate=lambda bar: 0.60)
    r = rp.replay(rows(60), notional=100_000.0, borrow_apr=0.0, liquidation=ev)
    t = r["trade_rows"][0]
    assert t["reason"].startswith("ACCOUNT_MM_RATE_") and \
        t["reason"].endswith("ABOVE_FLOOR")


# -- 4. only recorded, complete evidence can be called production-equivalent -------

def test_declared_inputs_are_a_scenario_never_production_equivalent():
    ev = rp.LiquidationInputs.declared_isolated(5.0, "test scenario")
    liq = rp.replay(rows(60), notional=100_000.0, borrow_apr=0.0,
                    liquidation=ev)["liquidation"]
    assert liq["gate"] == "EVALUATED" and liq["evidence_kind"] == "declared"
    assert liq["production_equivalent"] is False
    assert any("scenario" in w for w in liq["why_not_production_equivalent"])


def test_recorded_but_partial_evidence_is_still_not_production_equivalent():
    partial = rp.LiquidationInputs(kind="recorded", source="a recording",
                                   liq_price=lambda entry, bar: None)
    liq = rp.replay(rows(60), notional=100_000.0, borrow_apr=0.0,
                    liquidation=partial)["liquidation"]
    assert liq["production_equivalent"] is False
    joined = " ".join(liq["why_not_production_equivalent"])
    assert "accountMMRate" in joined and "margin_multiple" in joined


def test_complete_recorded_evidence_is_production_equivalent():
    full = rp.LiquidationInputs(kind="recorded", source="a recording",
                                liq_price=lambda entry, bar: None,
                                account_mm_rate=lambda bar: 0.05,
                                margin_multiple=lambda bar: 6.0)
    r = rp.replay(rows(60), notional=100_000.0, borrow_apr=0.0, liquidation=full)
    assert r["liquidation"]["production_equivalent"] is True
    assert r["liquidation"]["evidence_source"] == "a recording"


def test_evidence_must_name_its_source_and_kind():
    with pytest.raises(ValueError):
        rp.LiquidationInputs(kind="recorded", source=" ",
                             liq_price=lambda e, b: None)
    with pytest.raises(ValueError):
        rp.LiquidationInputs(kind="measured", source="x",
                             liq_price=lambda e, b: None)
    for lev in (0.5, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            rp.LiquidationInputs.declared_isolated(lev, "x")


# -- 5. no look-ahead ---------------------------------------------------------------

def test_a_decision_never_depends_on_a_later_bar_under_the_gate():
    base = rows(80)
    idx, entry = entry_of(base)
    ev = rp.LiquidationInputs.declared_isolated(2.0, "test scenario")
    cut = idx + 6
    later = with_spike(base, cut + 10, high_mult=1.9, entry=entry)
    a, b = [], []
    rp.replay(base, notional=100_000.0, borrow_apr=0.0, liquidation=ev, trace=a)
    rp.replay(later, notional=100_000.0, borrow_apr=0.0, liquidation=ev, trace=b)
    assert a[:cut + 1] == b[:cut + 1]
    assert a[cut + 10] != b[cut + 10]          # and the spike does bite, later
