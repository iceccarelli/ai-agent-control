"""Fee-literal allow-list. EVERY numeric literal in a fee context of production Python must be declared here with its category; a NEW literal, a CHANGED value or a STALE entry fails.

Categories (reason in the comment): AUTH = carry_costs (the published-fee vocabulary; test_cost_contracts pins it) | MIRROR = a second typed copy that must become an import of AUTH (pinned equal by a parity
test today; each removal shortens this list) | STRATEGY_COST_ASSUMPTION = directional strategies' own 25 bps round trip (frozen signal files are never edited) | POLICY = liquidation / slippage / reserve /
allowance constants that are not fee rates | TOLERANCE = reconciliation tolerances | FIXTURE_OR_REPORT, SCENARIO_OR_REPORT = demo data or output labels | DEAD = no importer | NOT_A_FEE.
This is a TRIPWIRE: it cannot prove a fee is right, only that nobody typed a new one without a reviewer seeing it. The RATE authority for an ACCOUNT is the signed /v5/account/fee-rate read
(account_measured); the published VIP-0 table is only ever 'venue_stated' and can never qualify a candidate.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, BOT]

import fee_literal_scan as F  # noqa: E402  (the scanner: tests/fee_literal_scan.py, a helper next to this test)

ALLOW = {  # (file, context, symbol): (values, category)
    ('backtest.py', 'assign', 'PERP_MAKER_FEE_BPS'): (('2.0',), 'MIRROR'),
    ('backtest.py', 'assign', 'maker_fee'): (('0.001',), 'MIRROR'),
    ('backtest.py', 'assign', 'round_trip_bps'): (('2.0',), 'MIRROR'),
    ('backtest.py', 'assign', 'taker_fee'): (('0.001',), 'MIRROR'),
    ('backtest.py', 'default', '__init__(maker_fee)'): (('0.001',), 'MIRROR'),
    ('backtest.py', 'default', '__init__(taker_fee)'): (('0.001',), 'MIRROR'),
    ('capital_survival.py', 'dict', 'liquidation_fee_bps'): (('50.0',), 'POLICY'),
    ('carry_costs.py', 'assign', 'ASSUMED_ROUND_TRIP_SPREAD_BPS'): (('0.4',), 'AUTH'),
    ('carry_costs.py', 'assign', 'MAKER_BPS_PERP_MODELLED'): (('2.0',), 'AUTH'),
    ('carry_costs.py', 'assign', 'TAKER_BPS_PERP'): (('5.5',), 'AUTH'),
    ('carry_costs.py', 'assign', 'TAKER_BPS_SPOT'): (('10.0',), 'AUTH'),
    ('carry_costs.py', 'assign', 'round_trip_per_day'): (('1E-9',), 'AUTH'),
    ('carry_shadow.py', 'dict', 'maker_bps'): (('2.0',), 'MIRROR'),
    ('config.py', 'assign', 'maker_bps'): (('2.0', '10.0', '500.0'), 'POLICY'),
    ('config.py', 'assign', 'round_trip_bps'): (('2.0',), 'POLICY'),
    ('config.py', 'assign', 'taker_bps'): (('5.5', '10.0', '500.0'), 'POLICY'),
    ('config.py', 'env_default', 'MAKER_FEE_BPS'): (('2.0', '10.0'), 'MIRROR'),
    ('config.py', 'env_default', 'MAKER_TIMEOUT_SECONDS'): (('20.0',), 'NOT_A_FEE'),
    ('config.py', 'env_default', 'TAKER_FEE_BPS'): (('5.5', '10.0'), 'MIRROR'),
    ('economic_model.py', 'assign', 'fee'): (('2',), 'MIRROR'),
    ('execution_record.py', 'default', 'reconcile(fee_tol)'): (('1E-9',), 'TOLERANCE'),
    ('risk_management.py', 'assign', 'round_trip_cost_bps'): (('25.0',), 'STRATEGY_COST_ASSUMPTION'),
    ('signals/btc_alt_spillover_v1.py', 'assign', 'ROUND_TRIP_BPS'): (('25.0',), 'STRATEGY_COST_ASSUMPTION'),
    ('signals/compression_breakout_v1.py', 'assign', 'ROUND_TRIP_BPS'): (('25.0',), 'STRATEGY_COST_ASSUMPTION'),
    ('signals/donchian_breakout_v1.py', 'assign', 'ROUND_TRIP_BPS'): (('25.0',), 'STRATEGY_COST_ASSUMPTION'),
    ('signals/funding_carry_fade_v1.py', 'assign', 'ROUND_TRIP_BPS'): (('25.0',), 'STRATEGY_COST_ASSUMPTION'),
    ('signals/open_gap_fade_v1.py', 'assign', 'ROUND_TRIP_BPS'): (('25.0',), 'STRATEGY_COST_ASSUMPTION'),
    ('signals/post_shock_fade_v1.py', 'assign', 'ROUND_TRIP_BPS'): (('25.0',), 'STRATEGY_COST_ASSUMPTION'),
    ('signals/range_location_fade_v1.py', 'assign', 'ROUND_TRIP_BPS'): (('25.0',), 'STRATEGY_COST_ASSUMPTION'),
    ('signals/sign_flip_momentum_v1.py', 'assign', 'ROUND_TRIP_BPS'): (('25.0',), 'STRATEGY_COST_ASSUMPTION'),
    ('signals/ts_momentum_v1.py', 'assign', 'ROUND_TRIP_BPS'): (('25.0',), 'STRATEGY_COST_ASSUMPTION'),
    ('tools/account_review.py', 'assign', 'FEE_TOL_BPS'): (('0.25',), 'TOLERANCE'),
    ('tools/bisection.py', 'kwarg', 'round_trip_bps'): (('25.0',), 'STRATEGY_COST_ASSUMPTION'),
    ('tools/capacity_summary.py', 'kwarg', 'p95_total_round_trip_bps'): (('0.95',), 'FIXTURE_OR_REPORT'),
    ('tools/carry_backtest.py', 'assign', 'TAKER_BPS_PERP'): (('5.5',), 'MIRROR'),
    ('tools/carry_backtest.py', 'assign', 'TAKER_BPS_SPOT'): (('10.0',), 'MIRROR'),
    ('tools/carry_core.py', 'default', 'params(round_trip_bps)'): (('31.0',), 'MIRROR'),
    ('tools/carry_core.py', 'default', 'params(taker_perp_bps)'): (('5.5',), 'MIRROR'),
    ('tools/carry_core.py', 'default', 'params(taker_spot_bps)'): (('10.0',), 'MIRROR'),
    ('tools/carry_core.py', 'kwarg', 'round_trip_bps'): (('11.0',), 'MIRROR'),
    ('tools/carry_performance.py', 'assign', 'SENSITIVITY_FEE_MULTIPLES'): (('1.0', '1.5', '2.0', '3.0'), 'SCENARIO_OR_REPORT'),
    ('tools/carry_replay.py', 'assign', 'maker_bps'): (('2.0',), 'MIRROR'),
    ('tools/carry_teardown.py', 'assign', 'fee'): (('2.0',), 'FIXTURE_OR_REPORT'),
    ('tools/drill.py', 'kwarg', 'round_trip_bps'): (('2.0',), 'MIRROR'),
    ('tools/evidence_reconcile.py', 'assign', 'CARRY_FEE_ABS_TOL'): (('1E-7',), 'TOLERANCE'),
    ('tools/evidence_reconcile.py', 'assign', 'FEE_ABS_TOL'): (('1E-9',), 'TOLERANCE'),
    ('tools/filter_diagnostic.py', 'default', 'collect_pools(round_trip_bps)'): (('25.0',), 'MIRROR'),
    ('tools/mainnet_public.py', 'assign', 'GATE_ROUND_TRIP_BPS'): (('11.0',), 'MIRROR'),
    ('tools/ms_economics.py', 'dict', 'fee'): (('1.5',), 'DEAD'),
    ('tools/ms_economics.py', 'kwarg', 'fee_tier_delta_bps_per_side'): (('5.0',), 'DEAD'),
    ('tools/ms_economics.py', 'kwarg', 'liquidation_fee_bps'): (('60.0',), 'DEAD'),
    ('tools/ms_edge_gate.py', 'assign', 'PUBLISHED_VIP0_MAKER'): (('0.0002', '0.001'), 'MIRROR'),
    ('tools/ms_edge_gate.py', 'assign', 'PUBLISHED_VIP0_TAKER'): (('0.00055', '0.001'), 'MIRROR'),
    ('tools/ms_execution.py', 'assign', 'fee'): (('2.0', '5.5'), 'MIRROR'),
    ('tools/ms_execution.py', 'assign', 'recon_fee_bps'): (('5.5',), 'MIRROR'),
    ('tools/ms_fillmodel.py', 'assign', 'taker_extra_slip_bps'): (('0.5',), 'POLICY'),
    ('tools/ms_portfolio.py', 'assign', 'fee_reserve_bps'): (('40.0',), 'POLICY'),
    ('tools/ms_replay.py', 'assign', 'liq_fee_bps'): (('50.0',), 'POLICY'),
    ('tools/ms_testnet_session.py', 'call_args', 'set_exit_fee_bps'): (('5.5', '10.0'), 'MIRROR'),
    ('tools/opportunity_map.py', 'assign', 'SPOT_MAKER_BPS'): (('10.0',), 'MIRROR'),
    ('tools/opportunity_map.py', 'dict', 'by_entry_basis_unconditional_taker'): (('5.0', '10.0'), 'SCENARIO_OR_REPORT'),
    ('tools/opportunity_map.py', 'dict', 'by_entry_ewma_unconditional_taker'): (('0.3', '0.65', '1.5'), 'SCENARIO_OR_REPORT'),
    ('tools/opportunity_map.py', 'dict', 'entries_by_ewma_threshold_maker_best_case'): (('0.65', '1.5'), 'SCENARIO_OR_REPORT'),
    ('tools/opportunity_map.py', 'dict', 'entries_by_ewma_threshold_taker'): (('0.65', '1.5'), 'SCENARIO_OR_REPORT'),
    ('tools/overlay_economics.py', 'assign', 'fees'): (('2.0',), 'FIXTURE_OR_REPORT'),
    ('tools/owner_import.py', 'assign', 'FEE_RATE_ABS_TOL'): (('0.0001',), 'TOLERANCE'),
    ('tools/owner_import.py', 'assign', 'FEE_RATE_REL_TOL'): (('0.02',), 'TOLERANCE'),
    ('tools/research_harness.py', 'dict', 'fee_bps'): (('5.5',), 'FIXTURE_OR_REPORT'),
    ('tools/residual_diagnostic.py', 'kwarg', 'round_trip_bps'): (('25.0',), 'STRATEGY_COST_ASSUMPTION'),
    ('tools/strategy_verdict.py', 'dict', 'fee_model'): (('2.0', '5.5'), 'FIXTURE_OR_REPORT'),
    ('tools/strategy_verdict.py', 'dict', 'maker_bps'): (('2.0',), 'FIXTURE_OR_REPORT'),
    ('tools/strategy_verdict.py', 'dict', 'taker_bps'): (('5.5',), 'FIXTURE_OR_REPORT'),
    ('tools/sweep_geometry.py', 'assign', 'DEFAULT_ROUND_TRIP_BPS'): (('25.0',), 'STRATEGY_COST_ASSUMPTION'),
    ('tools/testnet_session.py', 'default', 'build_cash_ledger(taker_fee)'): (('0.001',), 'MIRROR'),
    ('tools/testnet_session.py', 'kwarg', 'taker_fee'): (('0.001',), 'MIRROR'),
    ('tools/venue_study.py', 'assign', 'LEGS_PER_ROUND_TRIP'): (('4',), 'FIXTURE_OR_REPORT'),
    ('tools/venue_study.py', 'dict', 'round_trip_bps_at_that_size'): (('0.5',), 'FIXTURE_OR_REPORT'),
    ('trading_engine.py', 'assign', 'taker_fee_estimate'): (('0.001',), 'MIRROR'),
    ('venue_fees.py', 'assign', 'SPOT_UNSTATED_FEE_ALLOWANCE'): (('0.002',), 'POLICY'),
}

def test_every_fee_literal_in_production_code_is_declared():
    inv = F.inventory(BOT)
    new = {k: v for k, v in inv.items() if k not in ALLOW}
    changed = {k: (ALLOW[k][0], v) for k, v in inv.items() if k in ALLOW and ALLOW[k][0] != v}
    stale = sorted(k for k in ALLOW if k not in inv)
    assert not new, f"NEW fee-context literal(s) {new}: use carry_costs / the account's fee read, or declare it in ALLOW with a category"
    assert not changed, f"a declared fee literal CHANGED value (declared, now): {changed}"
    assert not stale, f"stale allow-list entries (the literal is gone: delete the entry so the list only shrinks): {stale}"


def test_mirrors_are_pinned_equal_to_the_authority_today():
    """While a MIRROR exists it must equal carry_costs (bps). ms_edge_gate's Decimal fractions are the one copy that no test pinned before."""
    import carry_costs as CC
    sys.path.insert(0, os.path.join(BOT, "tools"))
    import ms_edge_gate as EG
    from decimal import Decimal as D
    assert EG.PUBLISHED_VIP0_TAKER["linear"] * D(10000) == D(str(CC.TAKER_BPS_PERP))
    assert EG.PUBLISHED_VIP0_TAKER["spot"] * D(10000) == D(str(CC.TAKER_BPS_SPOT))
    assert EG.PUBLISHED_VIP0_MAKER["linear"] * D(10000) == D(str(CC.MAKER_BPS_PERP_MODELLED))


# ---------------------------------------------------------------- mutation tests for the scan itself
@pytest.mark.parametrize("body", [
    "TAKER_FEE = 0.0006",
    "PERP_FEE_BPS = 5.5",
    "x = dict(maker_fee=0.0001)",
    "def f(taker_fee=0.001):\n    return taker_fee",
    "ledger.set_exit_fee_bps(5.5, 10.0)",
    "fee = q * price * (2.0 if maker else 5.5) / 1e4",
    "SPEC = {'taker_bps': lab(5.5, 'assumed')}",
    "GATE_ROUND_TRIP_BPS = 11.0",
    "rate = parse_float(env, 'TAKER_FEE_BPS', 7.0)",
])
def test_the_scan_sees_a_planted_fee_literal(tmp_path, body):
    (tmp_path / "planted.py").write_text(body + "\n")
    assert F.scan(str(tmp_path)), body


@pytest.mark.parametrize("body", ["x = 0", "TAKER_FEE = 0.0", "n_legs = 2", "ROUND_TRIPS = 4", "rate = fee_rate_from_venue(row)", "FEE_COUNT = 3"])
def test_the_scan_ignores_zero_initialisers_counts_and_non_literals(tmp_path, body):
    (tmp_path / "planted.py").write_text(body + "\n")
    assert not F.scan(str(tmp_path)), body


def test_an_unparsable_file_fails_the_scan(tmp_path):
    (tmp_path / "bad.py").write_text("def broken(:\n")
    with pytest.raises(F.FeeScanError):
        F.scan(str(tmp_path))
