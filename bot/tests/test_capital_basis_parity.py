"""Round 21: ONE committed-capital basis from the scanner to the executor.

`ms_scanner` charged the owner's stated cost of capital on a RESEARCH basis (`margin_frac` 0.2: 1.2x per unit of notional for a carry, 0.2x for a
perp-only book) while the executable acquire structure commits 2.04x at leverage 1 (`economic_model.structure_capital_usd`: the spot leg paid in full + the
perp margin + the plan's 2% buffer) and a perp-only book commits 1.02x. The scanner decides eligibility and rank; the executor re-prices on 2.04x. So a row could
be scanned ELIGIBLE, ranked first and opened by the replay although it loses money on the capital the executor would actually commit. These tests pin the registry
as the only basis and the decisions that flip when the basis is the real one. They would all have failed on the research basis.

What does NOT change: unstated cost of capital stays UNKNOWN (Round 20), the E1-registered engine files are untouched, and no risk limit or gate moves.
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
import economic_model as EM  # noqa: E402
import ms_capital as MC  # noqa: E402
import ms_scanner as S  # noqa: E402
from test_ms_scanner import DEEP, NOW, cost, inst, run, snap  # noqa: E402

CARRY, SPREAD, DIRECTIONAL = "SPOT_PERP_CARRY", "PERP_FUNDING_SPREAD", "PERP_DIRECTIONAL_FUNDING"


def _acq(lev):
    return float(EM.hedged_pair_capital_usd(1, lev).value)


def _perp(lev):
    return float(EM.perp_only_capital_usd(1, lev).value)


# ------------------------------------------------------------------------------------------------- one definition
@pytest.mark.parametrize("lev", [1, 2, 3, 5])
def test_the_scanner_factor_is_the_registry_factor_at_every_leverage(lev):
    assert S.committed_capital_factor(CARRY, lev) == pytest.approx(_acq(lev))
    assert S.committed_capital_factor(SPREAD, lev) == pytest.approx(_perp(lev))
    assert S.committed_capital_factor(DIRECTIONAL, lev) == pytest.approx(_perp(lev))


def test_one_definition_of_acquired_capital_across_the_modules_that_price_it():
    """At leverage 1 every module that states what a pair commits must say 2.04 (and 1.02 for a perp-only book)."""
    values = {"scanner": S.committed_capital_factor(CARRY, 1), "registry": _acq(1), "allocator MARGIN_PER_USD": float(MC.MARGIN_PER_USD), "cash_basis": CB.capital_per_gross_notional(True)}
    assert set(round(v, 9) for v in values.values()) == {2.04}, values
    perp = {"scanner": S.committed_capital_factor(SPREAD, 1), "registry": _perp(1), "cash_basis": CB.capital_per_gross_notional(False)}
    assert set(round(v, 9) for v in perp.values()) == {1.02}, perp


def test_the_research_margin_parameter_is_gone_and_a_stale_caller_fails_closed():
    assert "margin_frac" not in S.DEFAULT_PARAMS and S.DEFAULT_PARAMS["leverage"] == 1
    with pytest.raises(S.ScanError, match="unknown scanner parameter"):
        run(params={"margin_frac": 0.2})


@pytest.mark.parametrize("bad", [0, -1, 0.5, 1.5, True, None, "1", float("nan"), 101, 10**6, 10**31])
def test_leverage_must_be_a_whole_number_of_at_least_one(bad):
    with pytest.raises(S.ScanError):
        run(params={"leverage": bad})


def test_the_leverage_bound_keeps_a_perp_only_book_from_being_priced_at_almost_no_capital():
    """REVIEWED (Round 21): an unbounded leverage priced a perp-only book at 1.02e-6 and inflated its rank; 10**31 raised out of the scan. 100 is the highest venue leverage; above it the scan is refused, never crashed."""
    assert S.committed_capital_factor(SPREAD, 100) == pytest.approx(_perp(100))
    for bad in (101, 10**6, 10**31):
        with pytest.raises(S.ScanError):
            S.committed_capital_factor(SPREAD, bad)


def test_the_factor_function_refuses_a_kind_it_does_not_know():
    with pytest.raises(S.ScanError):
        S.committed_capital_factor("SPOT_PERP_CARRY_LIKE", 1)


# ------------------------------------------------------------------------------------------------- golden numbers
def test_golden_carry_financing_net_and_rank_on_the_registry_basis():
    o = run(params={"opportunity_cost_apr": 0.0365})[0]
    assert cost(o, "financing").bps == pytest.approx(0.0365 / 365.0 * 1e4 * 7 * 2.04)         # 14.28
    assert cost(o, "financing").bps == pytest.approx(14.28, abs=0.005)
    assert o.net_bps_horizon == pytest.approx(84.0 - 31.0 - 1.0 - 2.0 - 14.28, abs=0.005)      # 35.72
    assert o.rank_score == pytest.approx((o.net_bps_horizon / 2.04) / o.risk_score)
    assert "2.04x committed capital" in " ".join(o.reasons)
    assert "margin_frac" not in " ".join(o.reasons) and "research assumption" not in " ".join(o.reasons)


# ------------------------------------------------------------------------------------------------- decisions that flip
def _carry(fund, apr, **kw):
    return run(i=[inst("AAAUSDT")], s=[snap("AAAUSDT", fund=(fund,) * 9)], params={"opportunity_cost_apr": apr, **kw})[0]


@pytest.mark.parametrize("fund,apr", [(2.2, 0.04), (2.3, 0.04), (2.5, 0.08), (3.0, 0.08)])
def test_a_carry_that_loses_money_on_the_executed_capital_is_not_eligible(fund, apr):
    """On the old 1.2x basis each of these was ELIGIBLE with a positive net; on the 2.04x the executor commits it loses money."""
    o = _carry(fund, apr)
    hold_funding = fund * 3 * 7
    true_net = hold_funding - 34.0 - apr / 365.0 * 1e4 * 7 * 2.04
    assert true_net < 0
    assert o.exclusions, f"scanned ELIGIBLE although it loses {true_net:.2f} bps on the capital the executor commits: {o}"
    assert not (o.rank_score or 0) > 0 or o.exclusions


def test_a_carry_that_clears_the_executed_capital_is_still_eligible():
    o = _carry(5.0, 0.04)                                  # 105 - 34 - 15.65 = 55.3 bps
    assert o.exclusions == () and o.net_bps_horizon == pytest.approx(105.0 - 34.0 - 0.04 / 365.0 * 1e4 * 7 * 2.04)


def test_the_rank_order_follows_the_executed_capital():
    """Two symbols whose order inverts when the capital charge is 2.04x instead of 1.2x (planted world)."""
    insts = [inst("XXXUSDT"), inst("YYYUSDT")]
    snaps = [snap("XXXUSDT", fund=(5.4,) * 9, spot=99.95), snap("YYYUSDT", fund=(4.0,) * 9, spot=99.98)]
    rows = run(i=insts, s=snaps, params={"opportunity_cost_apr": 0.04})
    eligible = [o for o in rows if not o.exclusions]
    assert [o.symbol if hasattr(o, "symbol") else o.legs[-1][0] for o in eligible] == ["XXXUSDT", "YYYUSDT"], [(o.legs[-1][0], o.rank_score) for o in rows]


def test_a_perp_only_book_is_charged_the_registry_perp_margin_not_a_fifth_of_it():
    """The old 0.2x understated the capital a perp-only book commits 5.1x."""
    from test_ms_scanner import _spread_world
    ins, sn, vol = _spread_world()
    rows = run(ins, sn, kind=SPREAD, params={"price_vol_bps_per_day": vol, "top_k": 2, "opportunity_cost_apr": 0.0365})
    pairs = [o for o in rows if len(o.legs) == 2]
    assert pairs
    for o in pairs:
        assert cost(o, "financing").bps == pytest.approx(0.0365 / 365.0 * 1e4 * 7 * 1.02)     # 7.14, not 1.4
    harsh = run(ins, sn, kind=SPREAD, params={"price_vol_bps_per_day": vol, "top_k": 2, "opportunity_cost_apr": 0.5})
    assert all(cost(o, "financing").bps == pytest.approx(0.5 / 365.0 * 1e4 * 7 * 1.02) for o in harsh if len(o.legs) == 2)


def test_a_directional_perp_charges_the_registry_perp_margin_too():
    o = S.scan([inst("HIUSDT")], [snap("HIUSDT", fund=(2.0,) * 9)], spec={"kind": DIRECTIONAL, "style": "TAKER"}, candidate_id="c@1", spec_digest="d" * 8, decision_ms=NOW,
               params={"persistence": 1.0, "opportunity_cost_apr": 0.04})[0]
    assert cost(o, "financing").bps == pytest.approx(0.04 / 365.0 * 1e4 * 7 * 1.02, abs=0.01)  # 7.83, not 1.53


def test_a_higher_leverage_commits_less_capital_and_the_charge_follows():
    lo = cost(run(params={"opportunity_cost_apr": 0.04, "leverage": 1})[0], "financing").bps
    hi = cost(run(params={"opportunity_cost_apr": 0.04, "leverage": 3})[0], "financing").bps
    assert hi == pytest.approx(lo * _acq(3) / _acq(1)) and hi < lo


def test_an_unstated_cost_of_capital_is_still_unknown_not_zero_on_the_new_basis():
    o = run(params={"opportunity_cost_apr": None})[0]
    assert cost(o, "financing").bps is None and cost(o, "financing").prov == "UNKNOWN"
    assert "FINANCING_UNKNOWN" in o.exclusions and o.net_bps_horizon is None and o.rank_score is None


# ------------------------------------------------------------------------------------------------- the replay sizes on the executor's leverage
def _replay_config():
    import types

    import ms_pipeline as MP
    v = types.SimpleNamespace(execution_policy="TAKER", max_daily_loss_usd=None, max_drawdown_frac=None, start_ms=NOW - 10 * 86_400_000, end_ms=NOW, hold_days=7.0, decision_every_hours=8.0, spec_digest="d" * 8)
    return MP, MP.build_replay_config(v)


def test_the_pipeline_replay_sizes_on_the_executor_leverage_not_the_allocator_default():
    """REPRODUCED (Round 21): the replay used the allocator's default perp_leverage 3.0: a carry held 1 + 1/3 + fees = 1.34x cash per notional against the 2.04x the executor commits (leverage 1), so it could
    size ~1.5x the notional the executor could fund, and it modelled a 31% liquidation distance against ~98%."""
    MP, cfg = _replay_config()
    assert cfg.portfolio.perp_leverage == 1.0 == float(S.DEFAULT_PARAMS["leverage"])
    import ms_portfolio as PF
    assert PF.PortfolioPolicy().perp_leverage == 3.0, "the module default stays (research tests use it); the PIPELINE overrides it explicitly"
    cash_frac = 1.0 + 1.0 / cfg.portfolio.perp_leverage + cfg.portfolio.fee_reserve_bps / 1e4 * 2
    assert cash_frac == pytest.approx(_acq(1), abs=0.04), (cash_frac, _acq(1))        # 2.008 (the allocator's own fee reserve) vs 2.04 (the registry's 2% buffer): within the buffer, never 1.34


def test_the_maker_first_replay_policy_carries_the_same_leverage():
    import types

    import ms_pipeline as MP
    v = types.SimpleNamespace(execution_policy="MAKER_FIRST", max_daily_loss_usd=5.0, max_drawdown_frac=0.1, start_ms=NOW - 10 * 86_400_000, end_ms=NOW, hold_days=7.0, decision_every_hours=8.0, spec_digest="d" * 8)
    cfg = MP.build_replay_config(v)
    assert cfg.portfolio.perp_leverage == 1.0 and cfg.portfolio.max_daily_loss_usd == 5.0 and cfg.portfolio.max_drawdown_frac == 0.1


# ------------------------------------------------------------------------------------------------- research tables charge capital on the committed capital
def test_the_acquire_breakeven_funding_is_charged_on_the_registry_capital():
    """REPRODUCED (Round 21): breakeven_table charged the cash rate per unit of NOTIONAL for the acquire row (1.0x), 0.347 bps per print short of the 2.04x the structure commits at 3.65%."""
    import opportunity_map as OM
    tier = {"perp_taker_bps": 5.5, "spot_taker_bps": 10.0, "perp_maker_bps": 2.0, "spot_maker_bps": 10.0}
    rows = OM.breakeven_table(tier, slippage_bps_per_leg=1.0, holds=[7], capital_aprs=[0.0, 0.0365])
    acq = {r["capital_cost_apr"]: r for r in rows if r["mode"] == "acquire" and r["style"] == "taker"}
    ov = {r["capital_cost_apr"]: r for r in rows if r["mode"] == "overlay" and r["style"] == "taker"}
    assert acq[0.0365]["capital_per_notional"] == pytest.approx(2.04) and ov[0.0365]["capital_per_notional"] == pytest.approx(OM.CAPITAL_PER_NOTIONAL)
    extra_acq = acq[0.0365]["breakeven_funding_bps_per_8h"] - acq[0.0]["breakeven_funding_bps_per_8h"]
    assert extra_acq == pytest.approx(0.0365 / 365.0 * 1e4 / OM.PRINTS_PER_DAY * 2.04)            # 0.68 bps per print, was 0.333
    extra_ov = ov[0.0365]["breakeven_funding_bps_per_8h"] - ov[0.0]["breakeven_funding_bps_per_8h"]
    assert extra_ov == pytest.approx(0.0365 / 365.0 * 1e4 / OM.PRINTS_PER_DAY * OM.CAPITAL_PER_NOTIONAL)
    assert extra_acq > extra_ov


def test_the_competition_headline_derives_its_capital_multiple_from_the_constant_not_a_literal():
    import inspect

    import candidate_competition as CCmp
    src = inspect.getsource(CCmp)
    assert "EXCESS over cash on 1.16x capital" not in src and "{OM.CAPITAL_PER_NOTIONAL:.2f}x capital" in src
