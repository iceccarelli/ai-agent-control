"""The carry teardown: arithmetic reconciled against the engine's own ledger, no lookahead in the basis timing, gaps never bridged, and a
decision ladder whose default is ZERO capital."""
import copy
import json
import math
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(BOT, "tools"))

import carry_teardown as CT  # noqa: E402
import trade_economics as TE  # noqa: E402

S = CT.SETTLE_MS
B = CT.BAR_MS
T0 = 1_700_000_000_000 // S * S                 # a settlement-grid instant


def synthetic(n=200, funding=1.0, price=60000.0):
    ts = [T0 + i * S for i in range(n)]
    f = [funding] * n
    bars = {}
    for t in range(T0 - 6 * B, T0 + (n + 3) * S, B):
        bars[t] = (price, price)
    return ts, f, dict(bars), dict(bars)


# ============================== data validation ==============================

def rows(ts, f):
    return [{"symbol": "BTCUSDT", "fundingRate": str(x / 1e4), "fundingRateTimestamp": str(t)} for t, x in zip(ts, f)]


def test_the_funding_parser_accepts_a_clean_history_and_refuses_each_kind_of_bad_one():
    ts, f = CT.parse_funding(rows([T0, T0 + S, T0 + 2 * S], [1.0, 0.5, -0.2]))
    assert ts == [T0, T0 + S, T0 + 2 * S] and f == pytest.approx([1.0, 0.5, -0.2])
    for bad in ([], "x", None):
        with pytest.raises(CT.DataError):
            CT.parse_funding(bad)
    good = rows([T0, T0 + S], [1.0, 1.0])
    with pytest.raises(CT.DataError):
        CT.parse_funding(good + [good[0]])                                   # duplicate stamp
    with pytest.raises(CT.DataError):
        CT.parse_funding(rows([T0 + 1], [1.0]))                              # off the 8h grid
    with pytest.raises(CT.DataError):
        CT.parse_funding(rows([T0], [1000.0]))                               # 10 % per settlement is a unit error
    with pytest.raises(CT.DataError):
        CT.parse_funding([{"fundingRate": "nan", "fundingRateTimestamp": str(T0)}])
    with pytest.raises(CT.DataError):
        CT.parse_funding([{"fundingRate": "0.0001"}])


def test_the_kline_parser_refuses_non_positive_prices_off_grid_bars_and_duplicates():
    ok = [[T0 // B * B, "100", "101", "99", "100.5", "1"]]
    assert CT.parse_klines(ok) == {T0 // B * B: (100.0, 100.5)}
    for bad in ([[T0 // B * B, "-1", "1", "1", "1", "1"]], [[T0 // B * B + 1, "1", "1", "1", "1", "1"]], ok + ok, [], [["x"]]):
        with pytest.raises(CT.DataError):
            CT.parse_klines(bad)


# ============================== the committed history is what the report says it is ==============================

@pytest.fixture(scope="module")
def study():
    return CT.load_study()


def test_the_committed_public_history_has_the_shape_the_report_relies_on(study):
    ts, f, perp, spot = study
    assert len(ts) == 4600 and ts[0] == 1656489600000 and ts[-1] == 1788940800000
    assert all(b - a == S for a, b in zip(ts, ts[1:]))                       # no gap on the 8h grid
    assert len(perp) == len(spot) == 9000 and max(f) <= 12.0 and min(f) < -1.0


def test_the_year_statistics_match_an_independent_recomputation(study):
    import datetime
    ts, f, _, _ = study
    st = CT.settlement_stats(ts, f)
    by = {}
    for t, x in zip(ts, f):
        by.setdefault(datetime.datetime.fromtimestamp(t / 1000, datetime.timezone.utc).year, []).append(x)
    for y, xs in by.items():
        b = st["by_year"][y]
        assert b["n"] == len(xs) and b["mean_bps"] == pytest.approx(sum(xs) / len(xs))
        assert b["share_negative"] == pytest.approx(sum(1 for x in xs if x < 0) / len(xs))
        assert b["annualised_gross_pct_on_notional"] == pytest.approx(sum(xs) / len(xs) * 3 * 365 / 100)
    last = f[-1095:]
    assert st["last_365d"]["mean_bps"] == pytest.approx(sum(last) / 1095)
    # the recent regime is capped at the 1 bp baseline: not one settlement in the last year paid more
    assert st["last_365d"]["share_at_or_below_baseline"] == 1.0 and st["last_365d"]["max_bps"] <= 1.0 + 1e-9
    assert st["effective_sample_size"] < 0.02 * len(f)                        # 4,600 prints, a few dozen independent ones


# ============================== closed-form economics ==============================

def test_breakeven_is_the_cost_over_the_funding_and_never_for_non_positive_funding():
    b = CT.breakeven(11.4, 0.65)
    assert b["settlements"] == pytest.approx(11.4 / 0.65) and b["days"] == pytest.approx(11.4 / 0.65 / 3) and b["days"] == pytest.approx(5.846, abs=1e-3)
    for x in (0.0, -0.3):
        assert CT.breakeven(11.4, x)["settlements"] is None
    with pytest.raises(ValueError):
        CT.breakeven(-1, 1.0)


@pytest.mark.parametrize("hurdle", [0.0, 4.0, 10.0, 15.0, 20.0, 37.5])
@pytest.mark.parametrize("hold", [3, 7, 30, 90, 180])
@pytest.mark.parametrize("cost", [0.0, 6.0, 11.4, 17.0])
@pytest.mark.parametrize("reserve", [0.0, 0.1, 0.5])
def test_the_funding_required_for_a_hurdle_earns_exactly_that_hurdle_on_capital(hurdle, hold, cost, reserve):
    need = CT.required_funding(hurdle, hold, cost, reserve)
    got = CT.net_return(need, hold, cost, reserve)["annualised_pct_on_capital"]
    assert got == pytest.approx(hurdle, abs=1e-9)
    assert CT.net_return(need * 1.01 + 1e-9, hold, cost, reserve)["annualised_pct_on_capital"] > hurdle


def test_the_closed_form_reconciles_with_the_engines_own_ledger():
    """trade_economics.compute is what the engine and the report use; the teardown's arithmetic must not drift from it."""
    for f, hold, cost, reserve in ((0.65, 30, 11.4, 0.1), (1.0, 7, 6.0, 0.0), (-0.2, 14, 17.0, 0.25), (2.0, 90, 11.4, 0.1)):
        w = CT.waterfall(f, hold, cost, reserve, notional=250_000.0)
        want = CT.net_return(f, hold, cost, reserve)
        assert w["net_pnl_usd"] / 250_000.0 * 1e4 == pytest.approx(want["net_bps"])
        assert w["annualised_pct_on_notional"] == pytest.approx(want["annualised_pct_on_notional"])
        assert w["annualised_pct_on_economic_capital"] == pytest.approx(want["annualised_pct_on_capital"])


def test_the_ledger_names_what_it_does_not_price_instead_of_zeroing_it():
    w = CT.waterfall(0.65, 30, 11.4, 0.1)
    assert {"latency", "market_impact", "borrow", "operational"} <= set(w["unknown_inputs"]) and w["unknown_inputs_tag"] == "UNKNOWN"
    assert w["inputs"]["round_trip_bps"]["tag"] == "ASSUMED" and w["inputs"]["funding_bps_per_settlement"]["tag"] == "PUBLIC_OBSERVATION"


def test_at_the_one_bp_baseline_the_lowest_screen_is_not_reachable_on_a_thirty_day_hold():
    """The structural fact in the recent regime: funding never exceeds 1 bp, and 10 % on fully allocated capital needs more."""
    need = CT.required_funding(10.0, 30, 11.4, 0.10)
    assert need == pytest.approx(1.1311, abs=1e-3) and need > 1.0
    assert CT.net_return(1.0, 30, 11.4, 0.10)["annualised_pct_on_capital"] < 10.0


# ============================== rolling-start simulation ==============================

def test_constant_funding_and_flat_prices_give_exactly_the_closed_form_for_every_window():
    ts, f, perp, spot = synthetic(n=200, funding=0.5)
    r = CT.rolling_starts(ts, f, perp, spot, 7)
    m = 21
    assert r["windows"] == 200 - m + 1 and r["excluded"] == 0
    d = r["funding_plus_basis"]
    assert d["mean_bps"] == pytest.approx(0.5 * m) and d["min_bps"] == pytest.approx(0.5 * m) and d["p05_bps"] == pytest.approx(0.5 * m)
    assert d["share_net_positive_after_cost"]["6.0"] == 1.0 and d["share_net_positive_after_cost"]["11.4"] == 0.0     # 10.5 bps < 11.4
    assert r["non_overlapping_windows"] == math.ceil(r["windows"] / m)
    assert r["adverse_basis_widening_bps"]["max"] == pytest.approx(0.0)


def test_basis_timing_uses_the_bar_ending_at_entry_and_the_bar_opening_at_exit_and_nothing_later():
    ts, f, perp, spot = synthetic(n=60, funding=0.0)
    m = 21
    i = 10
    entry_bar, exit_bar = ts[i] - B, ts[i + m - 1]
    perp[entry_bar] = (60000.0, 60600.0)                       # perp rich by 100 bps at the close of the entry bar
    perp[exit_bar] = (60300.0, 60300.0)                        # perp rich by 50 bps at the OPEN of the exit bar
    base = CT.rolling_starts(ts, f, perp, spot, 7)
    # the ONE window starting at i earns basis_in - basis_out = 100 - 50 = 50 bps (zero funding)
    one = CT.rolling_starts(ts[i:i + m], f[i:i + m], perp, spot, 7)
    assert one["windows"] == 1 and one["funding_plus_basis"]["mean_bps"] == pytest.approx(50.0)
    # prices AFTER the exit open (the exit bar's close and every later bar) must not change THIS window
    p2 = copy.deepcopy(perp)
    p2[exit_bar] = (60300.0, 99999.0)
    p2[exit_bar + B] = (99999.0, 99999.0)
    p2[exit_bar + 2 * B] = (1.0, 1.0)
    p2[entry_bar - B] = (1.0, 1.0)                              # ... nor any bar BEFORE the entry bar
    again = CT.rolling_starts(ts[i:i + m], f[i:i + m], p2, spot, 7)
    assert again["funding_plus_basis"]["mean_bps"] == pytest.approx(one["funding_plus_basis"]["mean_bps"])
    # but the bars actually used DO change it
    p4 = copy.deepcopy(perp)
    p4[entry_bar] = (60000.0, 61200.0)                         # 200 bps rich at entry -> 200 - 50 = 150
    assert CT.rolling_starts(ts[i:i + m], f[i:i + m], p4, spot, 7)["funding_plus_basis"]["mean_bps"] == pytest.approx(150.0)
    # and the full-history run still contains that window unchanged
    assert base["windows"] == 60 - m + 1


def test_a_missing_settlement_excludes_the_windows_that_span_it_and_is_counted_not_bridged():
    ts, f, perp, spot = synthetic(n=80, funding=1.0)
    del ts[40], f[40]
    r = CT.rolling_starts(ts, f, perp, spot, 7)
    m = 21
    spanning = sum(1 for i in range(len(ts) - m + 1) if ts[i + m - 1] - ts[i] != (m - 1) * S)
    assert spanning == m - 1 and r["excluded"] == spanning
    assert r["windows"] == len(ts) - m + 1 - spanning


def test_missing_kline_bars_exclude_the_window_rather_than_borrowing_a_neighbour():
    ts, f, perp, spot = synthetic(n=60, funding=1.0)
    full = CT.rolling_starts(ts, f, perp, spot, 7)["windows"]
    del perp[ts[15] - B]
    r = CT.rolling_starts(ts, f, perp, spot, 7)
    assert r["windows"] == full - 1 and r["excluded"] >= 1


def test_the_recent_regime_is_materially_worse_than_the_history_on_the_committed_data(study):
    ts, f, perp, spot = study
    allh = CT.rolling_starts(ts, f, perp, spot, 30)
    rec = CT.rolling_starts(ts, f, perp, spot, 30, first_start_ms=ts[-1095])
    assert rec["funding_plus_basis"]["mean_bps"] < 0.5 * allh["funding_plus_basis"]["mean_bps"]
    assert rec["funding_plus_basis"]["share_net_positive_after_cost"]["17.0"] < allh["funding_plus_basis"]["share_net_positive_after_cost"]["17.0"]
    assert rec["non_overlapping_windows"] < 20                              # the honest sample size is a dozen, not a thousand


# ============================== criteria and the decision ladder ==============================

def test_unsupplied_owner_inputs_are_unknown_and_supplied_ones_are_labelled_as_the_owners():
    c = CT.kill_criteria(None, {10.0: 1.13})
    assert all(c[k]["tag"] == "UNKNOWN" and c[k]["value"] is None for k in ("min_acceptable_net_return_pct_on_capital", "alternative_return_pct",
                                                                           "max_tolerable_drawdown_pct", "max_capital_at_risk_usd", "max_round_trip_cost_bps"))
    assert c["scenario_hurdles_pct"]["provenance"] == "PROVISIONAL_SCREENING_HURDLE_NOT_OWNER_APPROVED"
    c2 = CT.kill_criteria({"max_tolerable_drawdown_pct": 8.0}, {10.0: 1.13})
    assert c2["max_tolerable_drawdown_pct"] == {"value": 8.0, "tag": "ASSUMED", "provenance": "OWNER_SUPPLIED", "meaning": c2["max_tolerable_drawdown_pct"]["meaning"]}
    assert c2["min_acceptable_net_return_pct_on_capital"]["tag"] == "UNKNOWN"


REQ = 1.13


def ev(**kw):
    base = dict(prospective_settlements=300, effective_sample_size=60, mean_bps=1.4, lower_bound_bps=1.2, upper_bound_bps=1.6,
                account_reconciled=False, owner_authorisation=False)
    base.update(kw)
    return base


def test_capital_is_zero_and_nothing_is_authorised_in_every_branch_of_the_ladder():
    cases = [ev(prospective_settlements=10), ev(effective_sample_size=5), ev(mean_bps=0.5, lower_bound_bps=0.2, upper_bound_bps=0.9), ev(lower_bound_bps=1.0),
             ev(prospective_settlements=120), ev(), ev(account_reconciled=True), ev(account_reconciled=True, owner_authorisation=True),
             {}, ev(lower_bound_bps=None), ev(upper_bound_bps=None)]
    steps = []
    for c in cases:
        d = CT.decide(c, REQ)
        steps.append(d["step"])
        assert d["capital_recommendation"] == "ZERO" and d["authorizes_trading"] is False and d["step"] in CT.LADDER
    assert steps == ["CONTINUE_RESEARCH", "CONTINUE_RESEARCH", "REJECT_OR_SUSPEND", "CONTINUE_RESEARCH", "RESEARCH_CANDIDATE_REVIEW",
                     "PROSPECTIVE_EDGE_REVIEW", "ECONOMIC_RECONCILIATION_REQUIRED", "EXECUTION_READINESS_REVIEW", "CONTINUE_RESEARCH",
                     "CONTINUE_RESEARCH", "CONTINUE_RESEARCH"]


@pytest.mark.parametrize("bad", [
    {"prospective_settlements": float("nan")}, {"effective_sample_size": float("nan")}, {"prospective_settlements": "300"}, {"effective_sample_size": True},
    {"prospective_settlements": -1}, {"mean_bps": float("inf")}, {"lower_bound_bps": float("nan")}, {"upper_bound_bps": "1.6"},
    {"lower_bound_bps": 1.6, "upper_bound_bps": 1.2}, {"prospective_settlements": 10 ** 400}, {"upper_bound_bps": 10 ** 400}])
def test_unusable_evidence_is_never_a_step_up_even_with_both_flags_true(bad):
    d = CT.decide(ev(account_reconciled=True, owner_authorisation=True, **bad), REQ)
    assert d["step"] == "CONTINUE_RESEARCH" and "not usable" in d["reasons"][0]
    assert d["capital_recommendation"] == "ZERO" and d["authorizes_trading"] is False


@pytest.mark.parametrize("flag", ["no", "false", "yes", "true", 1, 0, "False", [], None])
def test_the_two_gates_are_exactly_True_not_truthy(flag):
    assert CT.decide(ev(account_reconciled=flag), REQ)["step"] == "PROSPECTIVE_EDGE_REVIEW"
    assert CT.decide(ev(account_reconciled=True, owner_authorisation=flag), REQ)["step"] == "ECONOMIC_RECONCILIATION_REQUIRED"


@pytest.mark.parametrize("req", [float("nan"), float("inf"), 0.0, -1.0, "1.1", None, True, 10 ** 400])
def test_a_bad_requirement_is_refused_not_compared(req):
    with pytest.raises(ValueError):
        CT.decide(ev(), req)


def test_rejection_needs_the_evidence_to_rule_the_requirement_out_not_merely_a_low_mean():
    assert CT.decide(ev(mean_bps=0.2, lower_bound_bps=0.0, upper_bound_bps=1.2), REQ)["step"] == "CONTINUE_RESEARCH"      # interval still includes the requirement
    assert CT.decide(ev(mean_bps=0.2, lower_bound_bps=0.0, upper_bound_bps=1.0), REQ)["step"] == "REJECT_OR_SUSPEND"
    assert CT.decide(ev(prospective_settlements=89, upper_bound_bps=0.0), REQ)["step"] == "CONTINUE_RESEARCH"            # too little to reject either


# ============================== the report ==============================

@pytest.fixture(scope="module")
def report():
    return CT.build()


def test_every_section_carries_a_valid_provenance_tag_and_unknowns_are_listed(report):
    for key, sec in report.items():
        if isinstance(sec, dict) and key not in ("criteria", "capital_recommendation", "waterfalls"):
            assert sec.get("tag") in CT.TAGS, key
    for name, w in report["waterfalls"].items():
        assert w["tag"] in CT.TAGS and w["unknown_inputs_tag"] == "UNKNOWN" and w["unknown_inputs"], name
    assert not {"MEASURED", "ACCOUNT_VERIFIED"} & {sec.get("tag") for sec in report.values() if isinstance(sec, dict)}      # never produced here
    assert {"account fee tier", "market impact", "latency", "owner hurdle"} <= set(report["unknowns"])


def test_the_report_recommends_zero_capital_and_says_why(report):
    assert report["capital_recommendation"]["value"] == "ZERO" and report["capital_recommendation"]["authorizes_trading"] is False


def test_the_report_states_the_decisive_facts_for_the_recent_regime(report):
    req = {float(k): v for k, v in report["required_funding_bps_per_settlement_30d"]["by_hurdle_pct"].items()}
    assert req[10.0] > 1.0 and req[20.0] > 2.0
    last = report["funding_statistics"]["last_365d"]
    assert last["share_at_or_below_baseline"] == 1.0 and last["mean_bps"] < 0.5
    w = report["waterfalls"]["last_365d"]
    assert w["annualised_pct_on_economic_capital"] < 2.0
    sizing = report["future_study_sizing"]["by_edge_bps"]
    assert sizing["0.25"]["required_settlements"] < sizing["0.1"]["required_settlements"] < sizing["0.05"]["required_settlements"]


def test_a_cheaper_assumed_cost_improves_the_numbers_but_cannot_reach_the_hurdle_at_the_baseline_cap():
    cheap = CT.build(round_trip_bps=6.0)
    k10 = str(CT.HURDLES_PCT[0])
    need = cheap["required_funding_bps_per_settlement_30d"]["by_hurdle_pct"][k10]
    assert need < CT.build()["required_funding_bps_per_settlement_30d"]["by_hurdle_pct"][k10]
    assert need > 0.9                                                     # still most of the 1 bp ceiling


def test_render_prints_the_tags_the_unknowns_and_the_limits(report):
    text = CT.render(report)
    for must in ("PUBLIC_OBSERVATION", "ASSUMED", "SIMULATED", "UNKNOWN:", "LIMITS:", "capital recommendation: ZERO", "non-overlapping",
                 "PROVISIONAL, not the owner's", "in-sample"):
        assert must in text, must


def test_the_cli_runs_offline_prints_json_and_refuses_nonsense(capsys):
    assert CT.main(["--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["data"]["settlements"] == 4600 and out["capital_recommendation"]["value"] == "ZERO"
    for argv in (["--round-trip-bps", "-1"], ["--round-trip-bps", "nan"], ["--reserve", "9"]):
        with pytest.raises(SystemExit):
            CT.main(argv)
    assert CT.main(["--owner-hurdle-pct", "12", "--owner-max-drawdown-pct", "6"]) == 0
    capsys.readouterr()
    owned = CT.build(owner={"min_acceptable_net_return_pct_on_capital": 12.0})
    assert owned["criteria"]["min_acceptable_net_return_pct_on_capital"]["provenance"] == "OWNER_SUPPLIED"


def test_a_corrupt_history_is_refused_with_a_message_not_a_traceback(tmp_path, capsys):
    bad = tmp_path / "f.json"
    bad.write_text(json.dumps([{"fundingRate": "0.0001", "fundingRateTimestamp": "1"}]))
    assert CT.main(["--funding", str(bad)]) == 2
    assert "refusing" in capsys.readouterr().err


def test_the_tool_imports_only_the_pure_modules_and_reads_no_runtime_state():
    import ast
    src = open(os.path.join(BOT, "tools", "carry_teardown.py"), encoding="utf-8").read()
    got = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            got |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            got.add((n.module or "").split(".")[0])
    assert got <= {"__future__", "argparse", "datetime", "gzip", "json", "math", "os", "sys", "typing", "evidence_stats", "statistical_power", "trade_economics"}, got
    for forbidden in ("trading_state", "testnet_session_evidence", "runs/", ".env", "requests", "urllib", "socket"):
        assert forbidden not in src, forbidden


# ---- the rolling-start arithmetic is per ENTRY notional of a fixed BTC quantity (independent review, finding 10) ----

def _world(price_path, rate_bps=1.0, n=40):
    """n settlements at a flat funding rate with the spot and perp opens/closes following price_path (one price per 4h bar)."""
    base = 1_700_000_000_000 // CT.SETTLE_MS * CT.SETTLE_MS
    ts = [base + i * CT.SETTLE_MS for i in range(n)]
    perp, spot = {}, {}
    for j in range(2 * n + 4):
        t0 = base - CT.BAR_MS + j * CT.BAR_MS
        p = price_path(t0)
        perp[t0] = (p, p)                    # (open, close): perp == spot, so the basis is zero everywhere
        spot[t0] = (p, p)
    return ts, [rate_bps] * n, perp, spot


def test_funding_follows_a_fixed_quantity_so_a_price_rise_raises_it_per_entry_notional():
    flat = _world(lambda t: 60000.0)
    rise = _world(lambda t: 60000.0 * (1.0 + (t - 1_700_000_000_000) / (40 * CT.SETTLE_MS) * 0.5))      # +50 % across the sample
    rf = CT.rolling_starts(*flat, hold_days=3, costs_bps=(0.0,))
    rr = CT.rolling_starts(*rise, hold_days=3, costs_bps=(0.0,))
    assert rf["funding_only"]["mean_bps"] == pytest.approx(9.0)            # 9 settlements x 1.0 bps, price flat
    assert rr["funding_only"]["mean_bps"] > 9.0                            # the same quantity is worth more, so it earns more per entry notional


def test_the_exit_basis_is_normalised_by_the_entry_spot_not_the_exit_spot():
    # perp - spot = +60 at the exit bar open (0.1 % of 60000) while the price has doubled by then; per ENTRY notional that is 60 / 60000 = 10 bps
    # (dividing by the exit spot, 120000, would say 5 bps)
    base = 1_700_000_000_000 // CT.SETTLE_MS * CT.SETTLE_MS
    ts = [base + i * CT.SETTLE_MS for i in range(3)]
    perp, spot = {}, {}
    for j in range(12):
        t0 = base - CT.BAR_MS + j * CT.BAR_MS
        spot[t0] = (60000.0, 60000.0)
        perp[t0] = (60000.0, 60000.0)
    b_out = ts[2]
    spot[b_out] = (120000.0, 120000.0)
    perp[b_out] = (120060.0, 120060.0)
    r = CT.rolling_starts(ts, [0.0, 0.0, 0.0], perp, spot, hold_days=1, costs_bps=(0.0,))
    assert r["windows"] == 1
    assert r["funding_plus_basis"]["mean_bps"] == pytest.approx(0.0 - (60.0 / 60000.0 * 1e4))       # basis_in 0, basis_out +10 bps: -10


def test_a_missing_settlement_bar_excludes_the_window_instead_of_bridging_it():
    ts, f, perp, spot = _world(lambda t: 60000.0)
    del perp[ts[4]]
    r = CT.rolling_starts(ts, f, perp, spot, hold_days=1, costs_bps=(0.0,))
    assert r["excluded"] >= 1


def test_a_history_shorter_than_a_year_is_a_clean_error_not_an_index_error(tmp_path):
    import gzip
    rows = [{"fundingRate": "0.0001", "fundingRateTimestamp": str(1_700_000_000_000 // CT.SETTLE_MS * CT.SETTLE_MS + i * CT.SETTLE_MS)} for i in range(50)]
    fp = tmp_path / "f.json.gz"
    with gzip.open(fp, "wt") as fh:
        json.dump(rows, fh)
    with pytest.raises(CT.DataError, match="at least 365 days"):
        CT.build(str(fp))
    assert CT.main(["--funding", str(fp)]) == 2


@pytest.mark.parametrize("row", [{"fundingRate": 10 ** 400, "fundingRateTimestamp": "1700000000000"},
                                 {"fundingRate": "0.0001", "fundingRateTimestamp": 10 ** 400}])
def test_an_integer_beyond_float_range_in_a_history_row_is_a_data_error(row):
    with pytest.raises(CT.DataError):
        CT.parse_funding([row])
    with pytest.raises(CT.DataError):
        CT.parse_klines([[10 ** 400, "1", "1", "1", "1"]])


def test_the_ledger_section_labels_its_numbers_an_optimistic_bound_and_gives_the_hurdle_verdicts():
    rep = CT.build()
    for name, w in rep["waterfalls"].items():
        assert w["net_is_optimistic_bound"] is True and w["unknown_inputs"], name
        assert set(w["hurdle_verdicts"]) == {"10.0", "15.0", "20.0"}
    # at the last year's 0.259 bps even the optimistic bound fails every screen, but at the ASSUMED fee schedule: not account-verified,
    # so the failure is stated as a failure on the declared fees, never as proof
    assert set(rep["waterfalls"]["last_365d"]["hurdle_verdicts"].values()) == {"FAILS_ON_THE_DECLARED_FEES_NOT_ACCOUNT_VERIFIED"}
    text = CT.render(rep)
    assert "OPTIMISTIC BOUND" in text


# ---- second-round review: one window formula everywhere, a stricter ladder, owner inputs validated, no tracebacks ----

def _usd_window(ts, f, perp, spot, i, m):
    """The independent check: $1 of entry notional, a FIXED BTC quantity, everything in USD, then converted to bps."""
    b_in, b_out = ts[i] - CT.BAR_MS, ts[i + m - 1]
    p_in, s_in = perp[b_in][1], spot[b_in][1]            # entry: the close of the bar that ENDS at the first settlement
    p_out, s_out = perp[b_out][0], spot[b_out][0]        # exit: the open of the bar that STARTS at the last settlement
    q = 1.0 / s_in                                      # BTC held per $1 of entry notional
    funding = sum(f[k] / 1e4 * q * perp[ts[k]][0] for k in range(i, i + m))
    perp_pnl = q * (p_in - p_out)                        # short the perp
    spot_pnl = q * (s_out - s_in)                        # long the BTC
    return (funding + perp_pnl + spot_pnl) * 1e4


def test_the_window_arithmetic_equals_an_independent_usd_simulation_on_the_committed_history():
    ts, f, perp, spot = CT.load_study()
    m = 30 * CT.PER_DAY
    checked = 0
    for i in range(0, len(ts) - m, 211):
        if ts[i + m - 1] - ts[i] != (m - 1) * CT.SETTLE_MS:
            continue
        w = CT._window(ts, f, perp, spot, i, m)
        if w is None:
            continue
        assert w[0] + w[1] - w[2] == pytest.approx(_usd_window(ts, f, perp, spot, i, m), abs=1e-6)
        checked += 1
    assert checked >= 15


def test_the_yearly_thirty_day_table_is_the_same_arithmetic_as_rolling_starts():
    import datetime
    ts, f, perp, spot = CT.load_study()
    rep = CT.build()
    m = 30 * CT.PER_DAY
    for y in ("2023", "2024", "2025"):
        lo = int(datetime.datetime(int(y), 1, 1, tzinfo=datetime.timezone.utc).timestamp() * 1000)
        hi = int(datetime.datetime(int(y) + 1, 1, 1, tzinfo=datetime.timezone.utc).timestamp() * 1000)
        vals = [_usd_window(ts, f, perp, spot, i, m) - CT.DEFAULT_ROUND_TRIP_BPS for i in range(len(ts) - m + 1)
                if lo <= ts[i] < hi and ts[i + m - 1] - ts[i] == (m - 1) * CT.SETTLE_MS and CT._window(ts, f, perp, spot, i, m) is not None]
        row = rep["thirty_day_outcome_by_start_year"]["by_year"][y]
        assert row["windows"] == len(vals) and row["mean_net_bps"] == pytest.approx(sum(vals) / len(vals), abs=1e-6), y


def test_the_adverse_basis_widening_is_the_worst_open_basis_minus_the_entry_basis():
    ts, f, perp, spot, = _world(lambda t: 60000.0, n=6)
    # widen the perp over the spot at one settlement open in the middle of the hold
    mid = ts[2]
    perp[mid] = (60000.0 + 60.0, perp[mid][1])
    r = CT.rolling_starts(ts, f, perp, spot, hold_days=1, costs_bps=(0.0,))
    assert r["adverse_basis_widening_bps"]["max"] == pytest.approx(60.0 / 60000.0 * 1e4)


@pytest.mark.parametrize("bad", [
    {"effective_sample_size": 5000, "prospective_settlements": 100}, {"mean_bps": 99.0}, {"mean_bps": -50.0}, {"mean_bps": None},
    {"upper_bound_bps": None}, {"lower_bound_bps": None}])
def test_incoherent_or_incomplete_evidence_cannot_climb_the_ladder(bad):
    d = CT.decide(ev(account_reconciled=True, owner_authorisation=True, **bad), REQ)
    assert d["step"] in ("CONTINUE_RESEARCH",) and d["capital_recommendation"] == "ZERO"


def test_a_missing_key_or_a_non_mapping_evidence_is_the_lowest_step():
    for e in (None, [], "x", 5, {}):
        assert CT.decide(e, REQ)["step"] == "CONTINUE_RESEARCH"
    base = ev()
    base.pop("mean_bps")
    assert CT.decide(base, REQ)["step"] == "CONTINUE_RESEARCH"


@pytest.mark.parametrize("key,val", [("min_acceptable_net_return_pct_on_capital", float("nan")), ("max_capital_at_risk_usd", float("inf")),
                                     ("max_tolerable_drawdown_pct", -5.0), ("max_round_trip_cost_bps", "11"), ("alternative_return_pct", float("nan")),
                                     ("max_capital_at_risk_usd", 10 ** 400), ("max_round_trip_cost_bps", True)])
def test_owner_inputs_must_be_real_numbers(key, val):
    with pytest.raises(ValueError):
        CT.kill_criteria({key: val}, {10.0: 1.1})


def test_a_negative_alternative_return_is_a_legitimate_owner_input():
    assert CT.kill_criteria({"alternative_return_pct": -1.5}, {10.0: 1.1})["alternative_return_pct"]["value"] == -1.5


def test_the_command_line_never_prints_a_traceback(tmp_path, capsys):
    import gzip
    bad = {"notjson.json": b"this is not json", "bad.json.gz": b"not gzip", "dir.json": None}
    for name, content in bad.items():
        p = tmp_path / name
        if content is None:
            p.mkdir()
        else:
            p.write_bytes(content)
        assert CT.main(["--funding", str(p)]) == 2, name
        assert "refusing:" in capsys.readouterr().err
    assert CT.main(["--funding", str(tmp_path / "missing.json")]) == 2
    capsys.readouterr()
    dict_rows = tmp_path / "dictrows.json.gz"
    with gzip.open(dict_rows, "wt") as fh:
        json.dump([{"a": 1}], fh)
    assert CT.main(["--perp-4h", str(dict_rows)]) == 2
    capsys.readouterr()
    base = 1_700_000_000_000 // CT.SETTLE_MS * CT.SETTLE_MS
    const = tmp_path / "const.json.gz"
    with gzip.open(const, "wt") as fh:
        json.dump([{"fundingRate": "0.0001", "fundingRateTimestamp": str(base + i * CT.SETTLE_MS)} for i in range(1500)], fh)
    assert CT.main(["--funding", str(const)]) == 2                    # a constant history has no variance to size a study from
    capsys.readouterr()
    assert CT.main(["--owner-hurdle-pct", "nan", "--json"]) == 2
    assert "finite number" in capsys.readouterr().err
