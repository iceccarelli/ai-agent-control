"""The pre-registered funding-regime-conditioned carry evaluator (tools/regime_entry_eval.py). Offline.

What these tests protect: the regime state is PAST-ONLY (a print after the decision cannot change it), every print is normalised by the interval of its own segment, only hedgeable
symbols ever enter, the primary metric is exactly funding minus the authoritative round trip, a re-run is the SAME trial (the ledger never grows), the verdict can never exceed
INSUFFICIENT_DATA, a measured basis residual never changes the registered metric, and the registration cannot drift silently."""
import ast
import json
import os
import statistics
import sys

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools"), os.path.join(BOT, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import candidate_readiness as CR  # noqa: E402
import regime_entry_eval as RE  # noqa: E402
import research_harness as RH  # noqa: E402

DAY = CR.MS_DAY
RT = RE._round_trip()


def sym(name, days, bps_per_day, *, iv=480, spot=True, delivery=None, rates=None, marks=None):
    """A hand-built hedgeable series: one print every ``iv`` minutes with the given bps/day (constant unless ``rates`` is given), flat mark 100."""
    step = iv * 60_000
    n = int(days * DAY // step)
    per = bps_per_day * iv / 1440.0
    f = [(( i + 1) * step, (rates[i] if rates else per)) for i in range(n)]
    m = marks if marks is not None else [(i * step, 100.0) for i in range(n + 1)]
    return CR.SymbolSeries(name, iv, f, m, launch_ms=1, delivery_ms=delivery, spot_listed=spot, key_basis=CR._KEY_BASIS_OBSERVED)


def ev(series, tmp_path, **kw):
    return RE.evaluate(series, ledger_path=str(tmp_path / "led" / "f.jsonl"), **kw)


# ---------------------------------------------------------------- the registration
def test_the_registration_is_frozen_and_its_digest_moves_when_a_number_moves():
    reg = RE.registration()
    assert reg["regime_state"] == {"formation_days": 7, "min_mean_funding_bps_per_day": 2.0, "min_positive_print_share": 0.8, "min_prints": 3}
    assert reg["hold_days"] == 30 and reg["variants"] == 1 and reg["min_listing_age_days"] == 30 and reg["verdict_ceiling"] == "INSUFFICIENT_DATA"
    assert reg["cost_scenario"]["round_trip_bps"] == RT == pytest.approx(31.4)            # carry_costs' authoritative spot+perp taker round trip: 31.0 fees + 0.4 ASSUMED spread
    assert reg["min_rows_for_a_test"] == 50
    d0 = RE.registration_digest(reg)
    for path, val in ((("regime_state", "min_mean_funding_bps_per_day"), 2.5), (("hold_days",), 21), (("regime_state", "min_positive_print_share"), 0.7)):
        r2 = json.loads(json.dumps(reg))
        tgt = r2
        for k in path[:-1]:
            tgt = tgt[k]
        tgt[path[-1]] = val
        assert RE.registration_digest(r2) != d0
    assert RE.registration_digest() == d0                                              # deterministic


def test_print_registration_cli_prints_the_digest(capsys):
    assert RE.main(["--history-root", "x", "--universe-dir", "y", "--print-registration"]) == 0
    o = json.loads(capsys.readouterr().out)
    assert o["digest"] == RE.registration_digest() and o["registration"]["registration_id"].endswith("@reg-1")


def test_the_module_reads_no_clock_no_environment_and_no_network():
    src = open(os.path.join(BOT, "tools", "regime_entry_eval.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & {"socket", "requests", "urllib", "http", "time", "datetime", "subprocess", "ssl"}, imported
    for bad in ("os.environ", "getenv", "time.time", "datetime", "urlopen"):
        assert bad not in src.replace('"datetime"', ""), bad


# ---------------------------------------------------------------- the regime state is past-only and interval-normalised
def test_the_state_uses_only_prints_up_to_the_decision_time():
    p = RE.REGISTRATION["regime_state"]
    base = sym("A", 40, 3.0)
    t = 20 * DAY
    on0, d0 = RE.regime_state(base, t, p)
    # the same series but with a hugely different FUTURE: nothing at or before t changes, so nothing about the state may change
    fut = [(ts, v if ts <= t else -1e6) for ts, v in base.funding]
    s2 = CR.SymbolSeries("A", 480, fut, base.mark, launch_ms=1, spot_listed=True, key_basis=CR._KEY_BASIS_OBSERVED)
    assert RE.regime_state(s2, t, p) == (on0, d0) and on0 is True
    # positive control: the same future edit DOES change the state when the decision is after it
    assert RE.regime_state(s2, t + 10 * DAY, p)[0] is False


def test_every_print_is_normalised_by_the_interval_of_its_own_segment():
    p = {"formation_days": 7, "min_mean_funding_bps_per_day": 2.0, "min_positive_print_share": 0.8, "min_prints": 3}
    h480, h240 = 480 * 60_000, 240 * 60_000
    f = [(i * h480, 1.0) for i in range(1, 7)] + [(6 * h480 + i * h240, 1.0) for i in range(1, 13)]
    s = CR.SymbolSeries("X", 240, f, [], launch_ms=1, spot_listed=True, funding_intervals=[(0, 6 * h480, 480), (6 * h480 + 1, 10 ** 15, 240)], key_basis=CR._KEY_BASIS_OBSERVED)
    on, d = RE.regime_state(s, 6 * h480 + 12 * h240, p)
    expected = statistics.fmean([1.0 * 3] * 6 + [1.0 * 6] * 12)                         # 1 bp per 480m print = 3/day; 1 bp per 240m print = 6/day
    assert d["mean_bps_per_day"] == pytest.approx(expected) and on is (expected >= 2.0)
    # a print with no segment makes the state UNKNOWN (never ON)
    s_bad = CR.SymbolSeries("X", 240, f, [], launch_ms=1, spot_listed=True, funding_intervals=[(0, 3 * h480, 480)], key_basis=CR._KEY_BASIS_OBSERVED)
    assert RE.regime_state(s_bad, 6 * h480 + 12 * h240, p)[0] is None


def test_a_rich_mean_from_a_few_prints_is_not_a_persistent_regime():
    p = RE.REGISTRATION["regime_state"]
    rates = [-1.0] * 8 + [50.0] * 13 + [0.0] * 200                                      # 21 prints in the 7-day window ending at day 7: mean rich, only 13/21 positive
    s = sym("B", 20, 0.0, rates=rates)
    on, d = RE.regime_state(s, 7 * DAY, p)
    assert d["mean_bps_per_day"] > 2.0 and d["positive_share"] < 0.8 and on is False
    assert RE.regime_state(sym("C", 20, 3.0), 7 * DAY, p)[0] is True                   # positive control: steady 3 bps/day is ON


def test_too_few_prints_is_unknown_not_off_and_never_on():
    s = sym("D", 20, 3.0)
    s.funding[:] = [x for x in s.funding if x[0] > 6.9 * DAY][:2]
    assert RE.regime_state(s, 7 * DAY, RE.REGISTRATION["regime_state"])[0] is None


# ---------------------------------------------------------------- the evaluation
def test_the_primary_metric_is_exactly_funding_minus_the_round_trip(tmp_path):
    s = sym("AAA", 400, 4.0)
    out = ev([s], tmp_path)
    slots = CR.holding_grid(s.funding[0][0], s.funding[-1][0], formation_days=7, hold_days=30)
    assert out["rows"] == len(slots) - 1 and out["rows"] >= 10                          # the first slot is inside the 30-day listing-age window
    for r in out["rows_detail"]:
        assert r["funding_bps"] == pytest.approx(30 * 4.0) and r["net_bps"] == pytest.approx(30 * 4.0 - RT) and r["n_legs"] == 1
    assert out["status"] == "TOO_FEW_HOLDING_ROWS" and out["verdict"] == "INSUFFICIENT_DATA" and out["descriptive"]["label"] == "IN_SAMPLE_DESCRIPTIVE_NOT_A_TEST"


def test_only_hedgeable_symbols_enter_and_an_off_regime_enters_nothing(tmp_path):
    series = [sym("HEDGE", 300, 4.0, spot=True), sym("PERPONLY", 300, 4.0, spot=False), sym("UNKNOWN", 300, 4.0, spot=None), sym("POOR", 300, 0.5, spot=True)]
    out = ev(series, tmp_path)
    assert {l for r in out["rows_detail"] for l in [r["n_legs"]]} == {1}
    assert out["descriptive"]["symbols_ever_entered"] == 1 and out["hedgeable_symbols"] == 2
    ex = out["eligibility_exclusions_by_reason"]
    assert ex.get("no_bybit_spot_pair", 0) > 0 and ex.get("hedge_unknown", 0) > 0 and ex.get("regime_off", 0) > 0


def test_without_a_spot_snapshot_nothing_is_eligible_and_it_says_why(tmp_path):
    out = ev([sym("U", 300, 4.0, spot=None)], tmp_path)
    assert out["rows"] == 0 and any("NO_HEDGEABLE_SYMBOLS" in w for w in out["warnings"]) and out["verdict"] == "INSUFFICIENT_DATA"


def test_a_rerun_is_the_same_trial_and_the_ledger_never_shrinks(tmp_path):
    series = [sym("AAA", 400, 4.0)]
    a = ev(series, tmp_path)
    b = ev(series, tmp_path)
    assert a["n_variants_counted"] == 1 == b["n_variants_counted"]
    led = RH.VariantLedger(RE.FAMILY, path=str(tmp_path / "led" / "f.jsonl"))
    assert led.n_variants == 1
    with pytest.raises(RH.HarnessError):
        led.reset()


def test_the_verdict_is_capped_even_with_a_huge_sample_and_a_huge_edge(tmp_path):
    # 3000 days of strong, steady funding on many hedgeable symbols: >= 50 rows. The cap and the label must still hold.
    series = [sym(f"S{i}", 3000, 5.0 + 0.01 * i) for i in range(6)]
    out = ev(series, tmp_path)
    assert out["rows"] >= 50 and out["status"] == "TEST_DONE"
    assert out["verdict"] in ("INSUFFICIENT_DATA", "REJECTED") and out["verdict"] != "PROMISING" and out["ceiling"] == "INSUFFICIENT_DATA"
    assert out["label"].startswith("HISTORICAL_TEST") and out["not_a_clean_holdout"]


def test_a_losing_regime_with_enough_rows_is_rejected(tmp_path):
    # rich (3 bps/day) on days 1..7 of every 30-day block, nothing otherwise: every entry lands in a rich week and every 30-day hold earns 7 x 3 = 21 bps < the 31.4 round trip
    import random
    rng = random.Random(7)
    rates = []
    for i in range(3000 * 3):
        ts = (i + 1) * 480 * 60_000
        base = 1.0 if 1 <= ((ts - 1) // DAY) % 30 <= 7 else 0.0                            # 1.0 bp per 480m print = 3 bps/day
        rates.append(base + rng.gauss(0.0, 0.05))
    out = ev([sym("LOSER", 3000, 0.0, rates=rates)], tmp_path)
    assert out["rows"] >= 50 and out["status"] == "TEST_DONE"
    assert out["mean_net_bps"] < 0 and out["verdict"] == "REJECTED"


def test_a_symbol_delivered_mid_hold_is_closed_at_delivery_with_capped_funding(tmp_path):
    step = 480 * 60_000
    s = sym("DEL", 100, 4.0, delivery=50 * DAY)
    out = ev([s], tmp_path)
    rows = {r["t"]: r for r in out["rows_detail"]}
    t38 = 38 * DAY
    assert t38 in rows and rows[t38]["funding_bps"] == pytest.approx(12 * 4.0, rel=0.02) and rows[t38]["net_bps"] == pytest.approx(12 * 4.0 - RT, rel=0.05)
    assert 68 * DAY not in rows and step                                                  # no entry after delivery


def test_series_without_the_loaders_key_basis_token_are_refused(tmp_path):
    s = sym("AAA", 100, 4.0)
    s.key_basis = "forged"
    with pytest.raises(RH.HarnessError):
        ev([s], tmp_path)


# ---------------------------------------------------------------- the basis residual is measured separately and never changes the registered metric
def test_a_measured_basis_residual_never_changes_the_registered_series(tmp_path):
    s = sym("AAA", 400, 4.0)
    step = 480 * 60_000
    flat = {"AAA": [(i * step, 100.0) for i in range(int(400 * DAY // step) + 1)]}
    drift = {"AAA": [(i * step, 100.0 * (1.0 + 0.0001 * (i * step // DAY))) for i in range(int(400 * DAY // step) + 1)]}
    base = ev([s], tmp_path / "a")
    with_flat = ev([s], tmp_path / "b", index=flat)
    with_drift = ev([s], tmp_path / "c", index=drift)
    series_of = lambda o: [r["net_bps"] for r in o["rows_detail"]]                       # noqa: E731
    assert series_of(base) == series_of(with_flat) == series_of(with_drift)
    assert base["descriptive"]["basis_residual"]["status"] == "UNMEASURED"
    assert with_flat["descriptive"]["basis_residual"]["mean_bps"] == pytest.approx(0.0, abs=1e-9)
    assert with_drift["descriptive"]["basis_residual"]["mean_bps"] == pytest.approx(30 * 0.0001 * 1e4, rel=0.05)           # +1% per 100 days of index drift = ~30 bps over a 30-day hold


# ---------------------------------------------------------------- the real writers feed it end to end
def test_it_runs_on_a_real_history_root_and_refuses_a_missing_universe(tmp_path, monkeypatch):
    import test_candidate_readiness as TCR
    import socket

    def boom(*a, **k):
        raise AssertionError("network touched")
    monkeypatch.setattr(socket.socket, "connect", boom)
    root, uni, spot = TCR._hist(tmp_path)
    out = RE.run_on_root(root, universe_dir=uni, ledger_path=str(tmp_path / "led.jsonl"), spot_dir=spot, mark_interval=TCR.MARK_IV)
    assert out["verdict"] == "INSUFFICIENT_DATA" and out["registration_digest"] == RE.registration_digest() and out["input"]["symbols_loaded"] == 4
    assert out["basis_measurement"]["status"] == "UNMEASURED"
    with pytest.raises(SystemExit) as ei:
        RE.main(["--history-root", root])
    assert ei.value.code == 2
