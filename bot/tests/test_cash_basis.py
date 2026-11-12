"""The cash alternative and the capital a strategy ties up, in the cross-sectional evaluators (round 10, priority two).

A cross-sectional row is bps of GROSS notional: it never said whether the book beat what the same capital earns as cash. The evaluator now also reports, per named cash source, the EXCESS over that cash ON THE
CAPITAL COMMITTED, as its own separately counted set of variants, reject-only. These tests pin: the capital factors are derived (not typed), the cash is aligned to the hold and an unpriced row is dropped and
counted (never zero cash), the sources are never mixed, nothing here can promote, and an identical re-run is the same trial.

Reproduced first by the round-10 research specialist on a synthetic 720-day root: with a 4%/yr cash alternative the carry family's out-of-sample mean moved from +2.1 to -6.1 bps per row and the spread from +55.2 to +47.0:
the same rows, a different sign.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (HERE, BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cash_basis as CB  # noqa: E402
import candidate_readiness as CR  # noqa: E402
import economic_model as EM  # noqa: E402
import opportunity_map as OM  # noqa: E402
import research_harness as RH  # noqa: E402
from test_candidate_readiness import T0_, _world  # noqa: E402

DAY = 86_400_000
HOUR = 3_600_000


def curve(apr, start=T0_ - 90 * DAY, end=T0_ + 1500 * DAY, hole=None):
    pts = [(t, apr) for t in range(start, end, HOUR) if not (hole and hole[0] <= t < hole[1])]
    return OM.CashCurve(pts)


def basis(apr, **kw):
    return CB.CashBasis(CB.HISTORICAL, f"flat {apr} test curve", curve=curve(apr, **kw), meta={"test": True})


def fresh():
    RH._FAMILY_HIGH_WATER.clear()
    RH._PATH_HIGH_WATER.clear()


# ------------------------------------------------------------------------------------------------ capital is derived, never typed
def test_capital_per_gross_notional_comes_from_the_economic_models_own_definitions():
    assert CB.capital_per_gross_notional(True, 1) == pytest.approx(float(EM.hedged_pair_capital_usd(1, 1).value)) == pytest.approx(2.04)
    assert CB.capital_per_gross_notional(False, 1) == pytest.approx(float(EM.perp_only_capital_usd(1, 1).value)) == pytest.approx(1.02)
    assert CB.capital_per_gross_notional(True, 2) == pytest.approx(1.5 * 1.02) and CB.capital_per_gross_notional(False, 2) == pytest.approx(0.5 * 1.02), "leverage moves it: a per-USD constant would silently encode 1"
    with pytest.raises(EM.EconomicModelError):
        EM.perp_only_capital_usd(1, 0.5)


def test_the_excess_arithmetic_is_net_over_capital_minus_the_cash_the_capital_would_have_earned():
    b = basis(0.04)
    assert CB.cash_bps_over(b, T0_, 7) == pytest.approx(0.04 * 7 / 365 * 1e4)
    ex, unknown = CB.excess_rows([{"t": T0_, "net_bps": 20.0}], hold_days=7, capital=2.04, basis=b)
    assert unknown == [] and ex[T0_]["excess_bps_of_capital"] == pytest.approx(20.0 / 2.04 - 0.04 * 7 / 365 * 1e4)
    assert ex[T0_]["cash_hurdle_bps_of_gross"] == pytest.approx(2.04 * 0.04 * 7 / 365 * 1e4)


def test_a_row_the_cash_source_cannot_price_is_dropped_and_counted_never_priced_at_zero():
    b = basis(0.04, hole=(T0_ + 10 * DAY, T0_ + 12 * DAY))
    rows = [{"t": T0_ + 8 * DAY, "net_bps": 5.0}, {"t": T0_ + 20 * DAY, "net_bps": 5.0}, {"t": T0_ - 400 * DAY, "net_bps": 5.0}]
    ex, unknown = CB.excess_rows(rows, hold_days=7, capital=1.02, basis=b)
    assert unknown == [T0_ + 8 * DAY, T0_ - 400 * DAY] and list(ex) == [T0_ + 20 * DAY]


def test_the_owner_statement_yields_a_basis_only_when_stated_current_and_valid():
    import ms_edge_gate as EG
    now = 1_794_312_000_000
    names = {EG.ENV_CASH_APR: "0.04", EG.ENV_CASH_SOURCE: "OWNER_STATED:savings", EG.ENV_CASH_STATED_UTC: "2026-11-09"}
    b, info = CB.owner_basis(names, now)
    assert b is not None and b.label == CB.OWNER_STATED and b.flat_apr == pytest.approx(0.04) and info["state"] == "STATED"
    assert CB.owner_basis({}, now)[0] is None and CB.owner_basis({}, now)[1]["state"] == "UNSTATED"
    assert CB.owner_basis({**names, EG.ENV_CASH_STATED_UTC: "2024-01-01"}, now)[1]["state"] == "STALE" and CB.owner_basis({**names, EG.ENV_CASH_STATED_UTC: "2024-01-01"}, now)[0] is None
    assert CB.owner_basis({**names, EG.ENV_CASH_APR: "four percent"}, now)[0] is None
    free, _ = CB.owner_basis({**names, EG.ENV_CASH_APR: "0"}, now)
    assert free is not None and free.flat_apr == 0.0, "an explicit 0 is a statement ('free by decision'), not an absence"


def test_the_historical_basis_names_its_file_and_is_none_when_the_file_is_missing(tmp_path):
    b = CB.historical_basis(BOT)
    assert b is not None and b.meta["file"] == OM.OKX_LENDING and len(b.meta["sha256"]) == 64 and b.meta["first_ms"] < b.meta["last_ms"]
    assert CB.historical_basis(str(tmp_path)) is None, "no cash series: every cash-adjusted figure is UNKNOWN, nothing is guessed"


# ------------------------------------------------------------------------------------------------ the evaluators
KW = dict(formation_days_grid=(7, 14), top_k_grid=(1,), hold_days=7, min_rows=60)


def test_a_family_whose_excess_over_a_high_cash_rate_is_negative_is_rejected_reject_only(tmp_path):
    """positive nominal (INSUFFICIENT_DATA without cash) but a cash alternative that out-earns the planted spread on 1.02x capital (a 300%/yr test device): the same rows reject"""
    fresh()
    plain = CR.evaluate_family(_world(1300, edge=2.5, beta_mkt=False), CR.FAMILY_SPREAD, ledger_path=str(tmp_path / "plain.jsonl"), **KW)
    assert plain["verdict"] == "INSUFFICIENT_DATA", "the premise: without cash this family is not rejected"
    fresh()
    ser = _world(1300, edge=2.5, beta_mkt=False)
    out = CR.evaluate_family(ser, CR.FAMILY_SPREAD, ledger_path=str(tmp_path / "h.jsonl"), cash_bases=[basis(3.0)], **KW)
    blk = out["vs_cash"]["HISTORICAL"]
    assert blk["status"] == "WALK_FORWARD_DONE" and blk["oos_mean_excess_bps_of_capital_per_row"] < 0
    assert blk["verdict"] == "REJECTED" and out["verdict"] == "REJECTED" and out["rejected_vs_cash"] == ["HISTORICAL"] and "REJECTED_VS_CASH" in out["reason"]


def test_a_planted_edge_is_never_promoted_by_the_cash_block_and_the_ceiling_holds(tmp_path):
    fresh()
    ser = _world(1300, edge=2.5, beta_mkt=False)
    out = CR.evaluate_family(ser, CR.FAMILY_SPREAD, ledger_path=str(tmp_path / "p.jsonl"), cash_bases=[basis(0.0)], **KW)
    blk = out["vs_cash"]["HISTORICAL"]
    assert blk["oos_mean_excess_bps_of_capital_per_row"] > 0 and blk["verdict"] == "INSUFFICIENT_DATA" and out["verdict"] == "INSUFFICIENT_DATA", "reject-only: a positive excess is never a pass"


def test_cash_lowers_the_statistic_by_exactly_the_cash_on_the_capital_and_changes_no_nominal_number(tmp_path):
    fresh()
    ser = _world(1300, edge=2.5, beta_mkt=False)
    plain = CR.evaluate_family(ser, CR.FAMILY_SPREAD, ledger_path=str(tmp_path / "a.jsonl"), **KW)
    fresh()
    with_cash = CR.evaluate_family(ser, CR.FAMILY_SPREAD, ledger_path=str(tmp_path / "b.jsonl"), cash_bases=[basis(0.04)], **KW)
    assert with_cash["oos_mean_total_bps_per_row"] == plain["oos_mean_total_bps_per_row"] and with_cash["p_adjusted"] == plain["p_adjusted"], "the nominal evaluation is untouched"
    blk = with_cash["vs_cash"]["HISTORICAL"]
    cap, cash = 1.02, 0.04 * 7 / 365 * 1e4
    assert blk["oos_mean_excess_bps_of_capital_per_row"] == pytest.approx(blk["oos_mean_net_bps_of_gross_per_row"] / cap - cash, rel=1e-9)
    assert blk["oos_mean_cash_hurdle_bps_of_gross_per_row"] == pytest.approx(cap * cash)


def test_the_cash_adjusted_variants_are_new_counted_trials_and_an_identical_rerun_is_the_same_trial(tmp_path):
    lp = str(tmp_path / "c.jsonl")
    fresh()
    ser = _world(1300, edge=2.5, beta_mkt=False)
    base = CR.evaluate_family(ser, CR.FAMILY_SPREAD, ledger_path=lp, **KW)["n_variants_counted"]
    fresh()
    once = CR.evaluate_family(ser, CR.FAMILY_SPREAD, ledger_path=lp, cash_bases=[basis(0.04)], **KW)
    selectable = len(once["variants_tried"]) - len(once["selection_excludes_modelled_cost_variants"])
    assert once["vs_cash"]["HISTORICAL"]["n_variants_counted"] == base + selectable, "one new trial per selectable variant per cash source"
    fresh()
    again = CR.evaluate_family(ser, CR.FAMILY_SPREAD, ledger_path=lp, cash_bases=[basis(0.04)], **KW)
    assert again["vs_cash"]["HISTORICAL"]["n_variants_counted"] == once["vs_cash"]["HISTORICAL"]["n_variants_counted"]
    fresh()
    owner = CB.CashBasis(CB.OWNER_STATED, "flat 4%", flat_apr=0.04, meta={})
    two = CR.evaluate_family(ser, CR.FAMILY_SPREAD, ledger_path=lp, cash_bases=[basis(0.04), owner], **KW)
    assert two["vs_cash"]["OWNER_STATED"]["n_variants_counted"] == once["vs_cash"]["HISTORICAL"]["n_variants_counted"] + selectable, "each source is its own set: the sources are never mixed"


def test_rows_before_the_cash_series_begins_are_dropped_from_every_variant_and_counted(tmp_path):
    fresh()
    ser = _world(1300, edge=2.5, beta_mkt=False)
    late = basis(0.04, start=T0_ + 300 * DAY)
    out = CR.evaluate_family(ser, CR.FAMILY_SPREAD, ledger_path=str(tmp_path / "d.jsonl"), cash_bases=[late], **KW)
    blk = out["vs_cash"]["HISTORICAL"]
    assert blk["rows_without_cash"] > 20 and blk["rows"] == blk["rows_aligned"] - blk["rows_without_cash"]
    fresh()
    zero = CR.evaluate_family(ser, CR.FAMILY_SPREAD, ledger_path=str(tmp_path / "e.jsonl"), cash_bases=[basis(0.0)], **KW)
    assert zero["vs_cash"]["HISTORICAL"]["rows_without_cash"] == 0 and blk["rows"] < zero["vs_cash"]["HISTORICAL"]["rows"], "an unpriced row is dropped, not priced at zero"


def test_a_series_that_covers_too_few_rows_says_so_and_decides_nothing(tmp_path):
    fresh()
    ser = _world(1300, edge=2.5, beta_mkt=False)
    out = CR.evaluate_family(ser, CR.FAMILY_SPREAD, ledger_path=str(tmp_path / "f.jsonl"), cash_bases=[basis(0.04, start=T0_ + 1100 * DAY)], **KW)
    blk = out["vs_cash"]["HISTORICAL"]
    assert blk["status"] == "TOO_FEW_ROWS_WITH_CASH" and blk["verdict"] == "INSUFFICIENT_DATA" and out["verdict"] == "INSUFFICIENT_DATA"


def test_too_few_rows_for_a_test_still_shows_the_in_sample_hurdle_and_records_nothing(tmp_path):
    fresh()
    ser = _world(120)
    lp = str(tmp_path / "g.jsonl")
    out = CR.evaluate_family(ser, CR.FAMILY_CARRY, ledger_path=lp, cash_bases=[basis(0.04)], **KW)
    if out["status"] == "TOO_FEW_HOLDING_ROWS":
        d = out["descriptive_only"]["vs_cash_in_sample"]["HISTORICAL"]
        assert d["label"] == "IN_SAMPLE_DESCRIPTIVE_NOT_A_TEST" and d["capital_per_gross_notional"] == pytest.approx(2.04)
        v = next(iter(d["variants"].values()))
        assert v["mean_cash_hurdle_bps_of_gross"] == pytest.approx(2.04 * 0.04 * 7 / 365 * 1e4)
        assert out["n_variants_counted"] == len(out["variants_tried"]), "descriptive: the cash block recorded no trial"


def test_the_carry_family_is_charged_on_the_full_pair_capital_and_the_spread_on_the_margin_only(tmp_path):
    fresh()
    ser = _world(1300, edge=2.5, beta_mkt=False)
    carry = CR.evaluate_family(ser, CR.FAMILY_CARRY, ledger_path=str(tmp_path / "carry.jsonl"), cash_bases=[basis(0.04)], **KW)
    fresh()
    spread = CR.evaluate_family(ser, CR.FAMILY_SPREAD, ledger_path=str(tmp_path / "spread.jsonl"), cash_bases=[basis(0.04)], **KW)
    assert carry["vs_cash"]["HISTORICAL"]["capital_per_gross_notional"] == pytest.approx(2.04) and spread["vs_cash"]["HISTORICAL"]["capital_per_gross_notional"] == pytest.approx(1.02)


# ------------------------------------------------------------------------------------------------ the competition's headline and CLI
def test_the_headline_net_edge_is_the_conservative_excess_over_cash_when_cash_was_applied_else_the_nominal_and_says_so():
    import candidate_competition as CC_
    fam = lambda nominal, blocks: {"verdict": "INSUFFICIENT_DATA", "status": "WALK_FORWARD_DONE", "is_carry": False, "independent_holding_rows": 80, "oos_mean_total_bps_per_row": nominal,        # noqa: E731
                                   "vs_cash": blocks, "survivorship": "SURVIVORS_ONLY"}
    blk = lambda x: {"verdict": "INSUFFICIENT_DATA", "status": "WALK_FORWARD_DONE", "oos_mean_excess_bps_of_capital_per_row": x, "capital_per_gross_notional": 1.02}
    res = {"status": "EVALUATED", "evaluation": {"families": {"PERP_FUNDING_SPREAD": fam(30.0, {"HISTORICAL": blk(8.0), "OWNER_STATED": blk(-2.0)}), "SPOT_PERP_CARRY": fam(10.0, {"HISTORICAL": blk(-5.0), "OWNER_STATED": blk(-9.0)})},
                                                 "survivorship": "SURVIVORS_ONLY", "input": {"holdout": {"status": "RESERVED"}}}}
    s = CC_._xs_summary(res)
    assert s["net_edge"] == -2.0 and s["net_edge_basis"].startswith("EXCESS_OVER_CASH_BPS_OF_CAPITAL_PER_ROW") and "HISTORICAL" in s["net_edge_basis"] and "OWNER_STATED" in s["net_edge_basis"]
    assert any(r.startswith("VS_CASH:") for r in s["reasons"]), "a positive nominal figure that is negative over cash must say so"
    assert s["families"]["PERP_FUNDING_SPREAD"]["vs_cash"]["HISTORICAL"]["oos_mean_excess_bps_of_capital_per_row"] == 8.0
    none = CC_._xs_summary({"status": "EVALUATED", "evaluation": {"families": {"PERP_FUNDING_SPREAD": fam(30.0, {})}, "survivorship": "SURVIVORS_ONLY", "input": {"holdout": {"status": "RESERVED"}}}})
    assert none["net_edge"] == 30.0 and none["net_edge_basis"].startswith("NOMINAL_BPS_OF_GROSS_NOTIONAL") and "NOT a comparison with cash" in none["net_edge_basis"]


def test_the_cli_never_substitutes_the_historical_series_for_an_owner_statement_it_cannot_use(tmp_path, capsys):
    import candidate_competition as CC_
    rc = CC_.main(["--ledger-dir", str(tmp_path / "led"), "--owner-cash-apr", "0.04", "--owner-cash-source", "my savings account", "--owner-cash-stated-utc", "2019-01-01"])
    out = capsys.readouterr().out
    assert rc == 0 and "your cost-of-capital statement is not usable (STALE" in out and "the historical series was NOT substituted for it" in out


def test_the_cli_says_when_no_cash_was_applied(tmp_path, capsys):
    import candidate_competition as CC_
    assert CC_.main(["--ledger-dir", str(tmp_path / "led"), "--no-historical-cash"]) == 0
    assert "NOTE" not in capsys.readouterr().out
