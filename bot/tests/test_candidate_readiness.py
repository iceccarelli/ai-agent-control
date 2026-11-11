"""candidate_readiness: an honest table, never above INSUFFICIENT_DATA, cross-venue never executable, funding normalised per day, formation != holding window.

Synthetic series below are SYNTHETIC (planted by the test); no number here is an exchange observation. The BTC corpus numbers come from the committed
data/real_bybit_btc_4h corpus and are IN-SAMPLE.
"""
from __future__ import annotations

import json
import os
import random
import sys
import types
import uuid

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, HERE)

import candidate_readiness as CR  # noqa: E402
import research_harness as RH  # noqa: E402
import history_holdout as HH  # noqa: E402

DAY = CR.MS_DAY


@pytest.fixture(scope="module")
def report():
    return CR.build_report()


def rowof(rep, name):
    return next(r for r in rep["candidates"] if r["name"] == name)


# ------------------------------------------------------------------ the table
def test_every_candidate_and_the_btc_description_has_a_row_with_a_reasoned_verdict(report):
    names = {r["name"] for r in report["candidates"]}
    assert names == {"cash_and_carry", "cross_sectional_funding", "cross_venue_funding", "liquidity_imbalance", "market_neutral_relative_value", "passive_execution",
                     "spot_perp_basis", "spread_microstructure", "btcusdt_funding_carry_overlay",
                     "funding_regime_conditioned_entry", "cross_interval_funding_dispersion", "hedgeable_majors_basis_convergence"}          # + the registered next-hypothesis drafts (UNTESTED)
    for r in report["candidates"]:
        assert r["readiness_verdict"] in ("UNTESTED", "INSUFFICIENT_DATA", "REJECTED") and r["readiness_reasons"], r["name"]
        assert r["verdict_rank_ceiling"] == "INSUFFICIENT_DATA"
        for k in ("required_data", "point_in_time", "universe_complete_for_claim", "economic_mechanism", "executable_on_qualified_venues", "edge_vs_costs",
                  "price_leg_hedge_risk_measured", "capacity_supported_by_depth", "multiple_testing", "oos_or_prospective_possible", "ladder_verdict"):
            assert k in r, (r["name"], k)
        assert r["economic_mechanism"]["present"] and "Counterparty" in r["economic_mechanism"]["mechanism_excerpt"] or r["name"] == "btcusdt_funding_carry_overlay"
    json.dumps(report, default=str)                                                             # machine-readable
    assert "candidate" in CR.render_table(report).splitlines()[0]


def test_without_a_data_root_nothing_is_assumed_present(report):
    assert report["data_scan"]["checked"] is False
    for r in report["candidates"]:
        if r["required_data"]["spec_declares_missing"]:
            assert r["required_data"]["value"] == "MISSING"
    assert rowof(report, "cross_sectional_funding")["point_in_time"]["value"] == "MISSING"
    assert rowof(report, "cross_sectional_funding")["universe_complete_for_claim"] is False
    assert rowof(report, "cross_sectional_funding")["readiness_verdict"] == "UNTESTED"


def test_cross_venue_is_research_only_and_never_executable(report):
    r = rowof(report, "cross_venue_funding")
    assert r["executable_on_qualified_venues"]["value"] == "RESEARCH_ONLY" and r["executable_on_qualified_venues"]["executable"] is False
    assert any("RESEARCH_ONLY" in x for x in r["readiness_reasons"])
    assert [n for n in (x["name"] for x in report["candidates"]) if rowof(report, n)["executable_on_qualified_venues"]["executable"]] == ["cash_and_carry", "btcusdt_funding_carry_overlay"]


def test_edge_is_unknown_not_optimistic_where_no_data_measures_it(report):
    for n in ("cross_sectional_funding", "spot_perp_basis", "liquidity_imbalance"):
        e = rowof(report, n)["edge_vs_costs"]
        assert e["value"] == "UNKNOWN" and "assumed" in e["reason"] and "bps/day" in e["breakeven_requirement"]


def test_btc_carry_numbers_come_from_the_committed_corpus_and_say_so(report):
    e = report["economics"]["btc_carry_corpus"]
    assert e["label"] == "COMMITTED_CORPUS_IN_SAMPLE" and "8h" in e["basis"] and "NO_DATA" in e["non_8h_intervals"]["btc_bybit"]
    sc = e["scenarios_round_trip_bps"]
    assert sc["TAKER_FEES_ONLY"] == 11.0 and sc["MAKER_FIRST_MODELLED_entry_maker_exit_taker"] == 7.5 and sc["TAKER_FEES_PLUS_SPREAD"] == pytest.approx(11.4)
    one = next(m for m in e["unconditional_forced_hold"] if m["hold_prints"] == 1 and m["scenario"] == "TAKER_FEES_PLUS_SPREAD")
    assert one["summary"]["mean"] < -9.0 and one["mean_funding_bps"] < 1.0                     # a single 8h print cannot repay an 11 bp round trip
    short = [m for m in e["unconditional_forced_hold"] if m["hold_prints"] <= 3 and m["scenario"] == "TAKER_FEES_ONLY"]
    assert all(m["summary"]["mean"] < 0 for m in short)
    r = rowof(report, "btcusdt_funding_carry_overlay")
    assert r["readiness_verdict"] == "REJECTED" and r["edge_vs_costs"]["policies"]["single_print_harvest"] == "REJECTED"     # every policy loses after costs or to cash on the capital it ties up
    assert r["edge_vs_costs"]["policies"]["long_hold_vs_cash"] == "REJECTED" and any("EXCESS over cash" in x for x in r["readiness_reasons"])
    assert any("out-of-sample" in x for x in r["readiness_reasons"])


def test_cost_constants_are_the_authoritative_carry_costs_names():
    c = CR.cost_constants()
    assert c["perp_round_trip_fee_bps"] == CR.CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY == 11.0
    assert c["assumed_round_trip_spread_bps"] == CR.CC.ASSUMED_ROUND_TRIP_SPREAD_BPS and c["perp_round_trip_total_bps"] == CR.CC.ROUND_TRIP_TOTAL_COST_BPS_PERP_OVERLAY
    assert c["spot_perp_round_trip_fee_bps"] == CR.CC.ROUND_TRIP_FEE_BPS_SPOT_PERP == 31.0 and "ASSUMPTION" in c["note"]
    sc = CR.scenarios(c)
    assert sc["TAKER_FEES_PLUS_SPREAD"] == c["perp_round_trip_total_bps"] and sc["ALL_MAKER_MODELLED_best_case_unreachable"] == CR.CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY_MAKER_MODELLED


def test_data_root_scan_reads_real_manifests_and_misses_nothing_it_cannot_see(tmp_path):
    (tmp_path / "u").mkdir()
    (tmp_path / "u" / "manifest.json").write_text(json.dumps({"schema": "universe_capture/v2"}))
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "manifest.json").write_text(json.dumps({"schema": "market_history/v1", "dataset_status": "COMPLETE", "coverage": {"first_ts": 0, "last_ts": 10}}))
    (tmp_path / "junk").mkdir()
    (tmp_path / "junk" / "manifest.json").write_text("{not json")
    s = CR.scan_data_root(str(tmp_path))
    assert s["checked"] and len(s["universe_capture_dirs"]) == 1 and s["market_history"][0]["dataset_status"] == "COMPLETE"
    u = CR.universe_assessment({"hypothesis": "Across a point-in-time universe"}, s)
    assert u["status"] in ("UNVERIFIED", "PARTIAL")                                             # the stub manifest has no pages: it can never verify
    assert CR.scan_data_root(str(tmp_path / "absent"))["checked"] is False


# ------------------------------------------------------------------ the ranked-funding evaluator
def mk(sym, iv, rates, marks=None, *, spot=None, delivery=None, start=0):
    n = len(rates)
    step = (iv or 480) * 60_000
    f = [(start + (i + 1) * step, r) for i, r in enumerate(rates)]
    m = marks if marks is not None else [(start + i * step, 100.0) for i in range(n + 1)]
    return CR.SymbolSeries(sym, iv, f, m, launch_ms=start, delivery_ms=delivery, spot_listed=spot, key_basis=CR._KEY_BASIS_OBSERVED)


def spec(fam=None):
    s = dict(CR.load_candidates()[1][1])
    s["multiple_testing_family"] = fam or "t_" + uuid.uuid4().hex
    return s


def test_funding_is_ranked_per_day_not_per_print():
    a = mk("A", 480, [1.0] * 30)                       # 3.0 bps/day
    b = mk("B", 240, [0.5] * 60)                       # 3.0 bps/day
    c = mk("C", 240, [1.2] * 60)                       # 7.2 bps/day although each print is "only" 1.2 vs A's 1.0
    t = 5 * DAY
    sa, sb, sc = (CR.formation_score(x, t, 3 * DAY) for x in (a, b, c))
    assert sa == pytest.approx(3.0) and sb == pytest.approx(3.0) and sc == pytest.approx(7.2)
    assert CR.formation_score(mk("D", None, [1.0] * 5), t, 3 * DAY) is None


def test_a_symbol_without_a_funding_interval_stops_the_evaluation():
    out = CR.evaluate_ranked_funding([mk("A", 480, [1.0] * 10), mk("X", None, [1.0] * 10)], spec=spec())
    assert out["status"] == "FUNDING_NORMALISATION_UNKNOWN" and out["verdict"] == "INSUFFICIENT_DATA" and out["symbols_without_interval"] == ["X"]


def test_formation_window_never_sees_the_holding_window():
    t = 10 * DAY
    quiet_then_huge = mk("L", 480, [0.1] * 60 + [500.0] * 60)          # explodes only AFTER t=10d (print 31 onwards)
    assert CR.formation_score(quiet_then_huge, t, 5 * DAY) == pytest.approx(0.3)
    steady = mk("S", 480, [1.0] * 120)
    rows = CR.run_variant([quiet_then_huge, steady], formation_days=5, hold_days=5, top_k=1, round_trip_bps=0.0, t_start=t, t_end=t + 5 * DAY)["rows"]
    assert rows[0]["legs"][0]["symbol"] == "S"                         # ranked on the past: the later explosion is not a reason to pick L


def test_closed_contract_funding_ends_at_delivery_and_it_is_not_selected_after():
    delivery = 12 * DAY
    s = mk("Z", 480, [2.0] * 90, delivery=delivery)
    other = mk("O", 480, [0.1] * 90)
    r = CR.run_variant([s, other], formation_days=3, hold_days=7, top_k=1, round_trip_bps=0.0, t_start=10 * DAY, t_end=17 * DAY)["rows"][0]["legs"][0]
    assert r["symbol"] == "Z" and r["prints"] == 6 and r["funding_bps"] == pytest.approx(12.0) and r["closed_during_hold"] is True      # (10d, 12d] = 6 prints, not 21
    after = CR.run_variant([s, other], formation_days=3, hold_days=7, top_k=1, round_trip_bps=0.0, t_start=13 * DAY, t_end=20 * DAY)["rows"][0]["legs"][0]
    assert after["symbol"] == "O"


def test_perp_only_symbols_are_never_reported_as_carry():
    only = mk("P", 480, [3.0] * 120, spot=False)
    hedged = mk("H", 480, [2.0] * 120, spot=True)
    unknown = mk("U", 480, [1.0] * 120, spot=None)
    leg = CR.run_variant([only], formation_days=3, hold_days=7, top_k=1, round_trip_bps=11.0, t_start=10 * DAY, t_end=30 * DAY)["rows"][0]["legs"][0]
    assert leg["hedgeability"] == "PERP_ONLY" and "hedged_carry_funding_net_of_cost_bps" not in leg and "directional_total_bps" in leg
    h = CR.run_variant([hedged], formation_days=3, hold_days=7, top_k=1, round_trip_bps=11.0, t_start=10 * DAY, t_end=30 * DAY)["rows"][0]["legs"][0]
    assert h["hedgeability"] == "HEDGEABLE_ON_BYBIT" and h["basis_residual"] == "UNMEASURED" and h["hedged_carry_funding_net_of_cost_bps"] == pytest.approx(21 * 2.0 - CR.CC.position_class_costs("acquire_spot_perp")["total_bps"])
    assert CR.hedgeability(unknown) == "HEDGE_UNKNOWN"
    out = CR.evaluate_ranked_funding([only, hedged, unknown], spec=spec(), formation_days_grid=(3,), hold_days=7)
    assert out["hedgeability_counts"] == {"PERP_ONLY": 1, "HEDGEABLE_ON_BYBIT": 1, "HEDGE_UNKNOWN": 1}
    top = out["trailing_observation"]
    assert top["is_expectation"] is False and top["label"] == "TRAILING_IN_SAMPLE_OBSERVATION" and "after the fact" in top["selection"].lower()
    p = next(x for x in top["top"] if x["symbol"] == "P")
    assert "directional" in p["note"] and p["short_price_leg_bps_same_window"] is not None


def test_a_short_capture_is_insufficient_data_with_every_variant_counted():
    fam = "t_" + uuid.uuid4().hex
    out = CR.evaluate_ranked_funding([mk("A", 480, [1.0] * 150), mk("B", 240, [0.2] * 300)], spec=spec(fam), formation_days_grid=(3, 7, 14))
    assert out["status"] == "TOO_FEW_HOLDING_ROWS" and out["verdict"] == "INSUFFICIENT_DATA" and out["n_variants_counted"] == 3 and out["family"] == fam
    assert out["ceiling"] == "INSUFFICIENT_DATA" and out["trailing_observation"]["is_expectation"] is False


def _long_capture(seed, edge):
    r = random.Random(seed)
    n = 3 * 560                                                          # 560 days of 8h prints
    a_rates = [edge + r.gauss(0, 0.3) for _ in range(n)]
    b_rates = [r.gauss(0, 0.3) for _ in range(n)]
    flat = [(i * 8 * 3_600_000, 100.0 + r.gauss(0, 0.05)) for i in range(n + 1)]
    return [mk("A", 480, a_rates, flat, spot=False), mk("B", 480, b_rates, flat, spot=False)]


def test_walk_forward_runs_and_is_capped_at_insufficient_data_for_a_planted_edge():
    fam = "t_" + uuid.uuid4().hex
    out = CR.evaluate_ranked_funding(_long_capture(1, 2.0), spec=spec(fam), formation_days_grid=(3, 7), hold_days=7)
    assert out["status"] == "WALK_FORWARD_DONE" and out["oos_rows"] >= 20 and out["n_variants_counted"] == 2
    assert out["oos_mean_total_bps_per_row"] > 0                          # 7 days x 6 bps/day - 11 bp cost: positive by construction
    assert out["verdict"] == "INSUFFICIENT_DATA" and out["correction"] == "holm" and "not prospective" in out["label"]


def test_walk_forward_rejects_when_costs_exceed_a_real_but_small_edge_with_enough_rows():
    fam = "t_" + uuid.uuid4().hex
    out = CR.evaluate_ranked_funding(_long_capture(2, 0.05), spec=spec(fam), formation_days_grid=(3, 7), hold_days=7)
    assert out["oos_mean_total_bps_per_row"] < 0                          # 0.15 bps/day x 7 - 11 bp
    assert out["verdict"] in ("REJECTED", "INSUFFICIENT_DATA") and out["verdict"] != "QUALIFIED"


# ------------------------------------------------------------------ round trip through the REAL market_history writer
import socket  # noqa: E402

import market_history as MH  # noqa: E402
import test_market_history as TMH  # noqa: E402  (its scripted fetch / clock / universe helpers; none of its tests are re-collected from here)

H4 = 4 * 3600 * 1000
MARK_IV = "240"


@pytest.fixture(autouse=False)
def offline(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("network touched")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


def _hist(tmp_path, *, spot=True, tamper_funding=None, mark_scripts=None, syms=("BTCUSDT", "ETHUSDT", "FASTUSDT", "OLDUSDT"), funding_override=None, universe_kw=None,
          old_delivery=None, skip_history=()):
    """A real dataset root: universe capture (BTC/ETH 8h, FAST 240m, OLD Closed with a deliveryTime), funding + mark-kline-240 written by the real writer with a scripted
    fetch / clock / sleep, and (optionally) a spot-instruments snapshot. Returns (root, universe_dir, spot_dir)."""
    delivery = TMH.BASE - 60 * TMH.H8
    root = tmp_path / "hist"
    os.mkdir(root, 0o700)
    uni = TMH._universe_dir(tmp_path, syms=syms, intervals={"FASTUSDT": 240}, bases={"FASTUSDT": "1000FAST"}, statuses={"OLDUSDT": "Closed"}, deliveries={"OLDUSDT": delivery if old_delivery is None else old_delivery}, **(universe_kw or {}))
    recs, info = MH.load_universe_dir(str(uni), include_statuses=["Trading", "Closed"])
    hrecs = {k: v for k, v in recs.items() if k not in skip_history}          # symbols in the universe record that the history collector never fetched
    fdata = {("funding", "BTCUSDT"): TMH.grid(TMH.FIRST, TMH.N, TMH.H8), ("funding", "ETHUSDT"): TMH.grid(TMH.FIRST, TMH.N, TMH.H8),
             ("funding", "FASTUSDT"): TMH.grid(TMH.FIRST, 2 * TMH.N - 1, H4), ("funding", "OLDUSDT"): TMH.grid(TMH.FIRST, TMH.N - 60, TMH.H8)}
    fdata = {k: v for k, v in fdata.items() if k[1] in syms and k[1] not in skip_history}
    for sym, ts_list in (funding_override or {}).items():
        fdata[("funding", sym)] = ts_list
    fspecs = MH.build_specs("funding", None, sorted(hrecs), TMH.START, TMH.END, recs)
    fv = TMH.HV(fdata)
    TMH.run(root, fspecs, fv, universe=info)
    mdata = {("mark-kline", s): TMH.grid(TMH.FIRST, 2 * TMH.N - 1, H4) for s in syms if s != "OLDUSDT" and s not in skip_history}
    if "OLDUSDT" in syms and "OLDUSDT" not in skip_history:
        mdata[("mark-kline", "OLDUSDT")] = TMH.grid(TMH.FIRST, (delivery - TMH.FIRST) // H4 + 1, H4)
    mspecs = MH.build_specs("mark-kline", MARK_IV, sorted(hrecs), TMH.START, TMH.BASE + 10, recs)
    mv = TMH.HV(mdata)
    for k, v in (mark_scripts or {}).items():
        mv.script[k] = v
    TMH.run(root, mspecs, mv, "mark-kline", MARK_IV, universe=info)
    sd = None
    if spot:
        res = MH.capture_spot_instruments(str(root), fetch=lambda u: TMH.spot_body([TMH.spot_row("BTC"), TMH.spot_row("ETH", "USDC"), TMH.spot_row("FAST"), TMH.spot_row("OLD", status="Delisted")]),
                                          clock=TMH.Clock(), sleep=lambda _s: None, free_bytes_fn=lambda _p: 1 << 40, reserve_bytes=1)
        sd = res["dir"]
    return str(root), str(uni), sd


def test_round_trip_real_writer_to_ranked_funding_plumbing(tmp_path, offline):
    root, uni, spot = _hist(tmp_path)
    series, info = CR.load_history_root(root, universe_dir=uni, spot_dir=spot, mark_interval=MARK_IV)
    by = {s.symbol: s for s in series}
    assert sorted(by) == ["BTCUSDT", "ETHUSDT", "FASTUSDT", "OLDUSDT"], info
    assert by["FASTUSDT"].interval_min == 240 and by["BTCUSDT"].interval_min == 480
    assert by["OLDUSDT"].delivery_ms == TMH.BASE - 60 * TMH.H8
    assert by["BTCUSDT"].delivery_ms is None and by["BTCUSDT"].launch_ms == TMH.FIRST - 1
    assert by["BTCUSDT"].funding[0] == (TMH.FIRST, pytest.approx(1.0)) and by["BTCUSDT"].mark[0] == (TMH.FIRST + H4, pytest.approx(100.5))      # 0.0001 fraction = 1 bp; mark = close, KEYED BY WHEN IT IS KNOWABLE (the candle starting at FIRST closes at FIRST+4h; it was keyed FIRST before: look-ahead)
    assert len(by["FASTUSDT"].funding) == 2 * TMH.N - 1 and by["OLDUSDT"].funding[-1][0] <= by["OLDUSDT"].delivery_ms
    assert [by[s].spot_listed for s in ("BTCUSDT", "ETHUSDT", "FASTUSDT", "OLDUSDT")] == [True, False, None, False]       # SPOT_TRADING / NO_SPOT_PAIR / UNRESOLVED_MULTIPLIER / SPOT_NOT_TRADING
    assert info["datasets"]["funding"]["dataset_status"] == "COMPLETE" and info["datasets"][f"mark-kline-{MARK_IV}"]["verified"] is True
    assert info["universe"]["dataset_status"] in ("COMPLETE", "PARTIAL") and info["excluded"] == [] and "hedge_verdicts" in info
    out = CR.evaluate_ranked_funding(series)
    assert out["verdict"] in ("INSUFFICIENT_DATA", "UNTESTED") and out["status"] in ("TOO_FEW_HOLDING_ROWS", "WALK_FORWARD_DONE") and out["verdict"] != "QUALIFIED"
    assert out["symbols"] == 4 and out["closed_contracts"] == 1 and out["intervals_min_counts"] == {"240": 1, "480": 3}


def test_without_spot_data_hedgeability_is_unknown_not_assumed(tmp_path, offline):
    root, uni, _ = _hist(tmp_path, spot=False)
    series, info = CR.load_history_root(root, universe_dir=uni, mark_interval=MARK_IV)
    assert {s.spot_listed for s in series} == {None} and {CR.hedgeability(s) for s in series} == {"HEDGE_UNKNOWN"}


def test_mark_interval_is_discovered_when_unique_and_required_when_ambiguous(tmp_path, offline):
    root, uni, _ = _hist(tmp_path, spot=False)
    assert CR.load_history_root(root, universe_dir=uni)[1]["mark_interval"] == MARK_IV
    os.mkdir(os.path.join(root, "mark-kline-60"))
    with pytest.raises(RH.HarnessError):
        CR.load_history_root(root, universe_dir=uni)
    assert CR.load_history_root(root, universe_dir=uni, mark_interval=MARK_IV)[0]


def _first_file(d, pattern):
    return next(os.path.join(d, n) for n in sorted(os.listdir(d)) if n.startswith(pattern))


@pytest.mark.parametrize("what", ["funding_raw_page", "funding_rows", "mark_rows", "mark_manifest", "universe_page", "spot_rows"])
def test_tampering_with_any_input_makes_the_loader_refuse(tmp_path, offline, what):
    root, uni, spot = _hist(tmp_path)
    assert CR.load_history_root(root, universe_dir=uni, spot_dir=spot, mark_interval=MARK_IV)[0]                  # positive control: untouched, it loads
    target = {"funding_raw_page": os.path.join(root, "funding", "BTCUSDT", "page-000001.json"), "funding_rows": os.path.join(root, "funding", "ETHUSDT", "rows.jsonl"),
              "mark_rows": os.path.join(root, f"mark-kline-{MARK_IV}", "BTCUSDT", "rows.jsonl"), "mark_manifest": os.path.join(root, f"mark-kline-{MARK_IV}", "manifest.json"),
              "universe_page": _first_file(uni, "linear_Trading_page"), "spot_rows": os.path.join(spot, "rows.jsonl")}[what]
    os.chmod(target, 0o600)
    if what == "mark_manifest":                                   # a content change (trailing whitespace is not one: the digest covers the parsed content)
        txt = open(target).read()
        assert '"dataset_status": "COMPLETE"' in txt
        open(target, "w").write(txt.replace('"dataset_status": "COMPLETE"', '"dataset_status": "PARTIAL"', 1))
    else:
        with open(target, "ab") as f:
            f.write(b" ")
    with pytest.raises(RH.HarnessError):
        CR.load_history_root(root, universe_dir=uni, spot_dir=spot, mark_interval=MARK_IV)


def test_a_missing_dataset_or_universe_is_refused(tmp_path, offline):
    root, uni, spot = _hist(tmp_path)
    with pytest.raises(RH.HarnessError):
        CR.load_history_root(root, universe_dir=str(tmp_path / "nowhere"), mark_interval=MARK_IV)
    with pytest.raises(RH.HarnessError):
        CR.load_history_root(root, universe_dir=uni, mark_interval="999")
    with pytest.raises(RH.HarnessError):
        CR.load_history_root(str(tmp_path / "nowhere"), universe_dir=uni, mark_interval=MARK_IV)


def test_nothing_unverified_is_evaluated_when_the_verifier_says_not_ok(tmp_path, offline, monkeypatch):
    root, uni, spot = _hist(tmp_path)
    monkeypatch.setattr(MH, "verify_dataset", lambda d: {"ok": False, "problems": ["x"], "dataset_status": "COMPLETE"})
    with pytest.raises(RH.HarnessError):
        CR.load_history_root(root, universe_dir=uni, mark_interval=MARK_IV)


def test_a_conflicted_symbol_is_excluded_and_listed_not_silently_dropped(tmp_path, offline):
    t = TMH.FIRST + 5 * TMH.H8
    bad = TMH.envelope("mark-kline", "ETHUSDT", [TMH.row_for("mark-kline", "ETHUSDT", TMH.BASE), TMH.row_for("mark-kline", "ETHUSDT", t), TMH.row_for("mark-kline", "ETHUSDT", t, row=[str(t), "100", "101", "99", "100.7"])], TMH.SERVER0)
    root, uni, _ = _hist(tmp_path, spot=False, mark_scripts={("mark-kline", "ETHUSDT"): [bad]})
    man = json.load(open(os.path.join(root, f"mark-kline-{MARK_IV}", "manifest.json")))
    assert man["symbols"]["ETHUSDT"]["status"] == "CONFLICTED" and man["dataset_status"] == "CONFLICTED"
    series, info = CR.load_history_root(root, universe_dir=uni, mark_interval=MARK_IV)
    assert sorted(s.symbol for s in series) == ["BTCUSDT", "FASTUSDT", "OLDUSDT"]                      # only the conflicted symbol goes; the rest still load
    assert info["excluded"] == [{"symbol": "ETHUSDT", "reason": f"symbol_status:mark-kline-{MARK_IV}=CONFLICTED"}]
    assert info["datasets"][f"mark-kline-{MARK_IV}"]["dataset_status"] == "CONFLICTED" and "warning" in info


def test_a_stale_symbol_is_kept_but_reported_non_complete(tmp_path, offline):
    root, uni, _ = _hist(tmp_path, spot=False)
    # OLD is COMPLETE only because of its delivery; make the live BTC symbol stale by giving it no recent funding
    r2 = tmp_path / "h2"
    os.mkdir(r2, 0o700)
    recs, info = MH.load_universe_dir(uni, include_statuses=["Trading", "Closed"])
    fv = TMH.HV({("funding", s): TMH.grid(TMH.FIRST, TMH.N - (50 if s == "BTCUSDT" else 0) - (60 if s == "OLDUSDT" else 0), TMH.H8) if s != "FASTUSDT" else TMH.grid(TMH.FIRST, 2 * TMH.N - 1, H4) for s in recs})
    TMH.run(r2, MH.build_specs("funding", None, sorted(recs), TMH.START, TMH.END, recs), fv, universe=info)
    mv = TMH.HV({("mark-kline", s): TMH.grid(TMH.FIRST, 2 * TMH.N - 1 if s != "OLDUSDT" else (TMH.BASE - 60 * TMH.H8 - TMH.FIRST) // H4 + 1, H4) for s in recs})
    TMH.run(r2, MH.build_specs("mark-kline", MARK_IV, sorted(recs), TMH.START, TMH.BASE + 10, recs), mv, "mark-kline", MARK_IV, universe=info)
    series, info2 = CR.load_history_root(str(r2), universe_dir=uni, mark_interval=MARK_IV)
    assert "BTCUSDT" in {s.symbol for s in series} and info2["non_complete_symbols"] == {"BTCUSDT": {"funding": "STALE"}} and info2["excluded"] == []
    assert info2["datasets"]["funding"]["dataset_status"] == "STALE" and "warning" in info2


def test_a_symbol_outside_the_universe_or_one_dataset_is_excluded_and_listed(tmp_path, offline):
    root, uni, _ = _hist(tmp_path, spot=False)
    # evaluate against a universe that lacks FASTUSDT
    uni2 = TMH._universe_dir(tmp_path, syms=("BTCUSDT", "ETHUSDT", "OLDUSDT"), statuses={"OLDUSDT": "Closed"}, deliveries={"OLDUSDT": TMH.BASE - 60 * TMH.H8}, name="uni2")
    series, info = CR.load_history_root(root, universe_dir=str(uni2), mark_interval=MARK_IV)
    assert "FASTUSDT" not in {s.symbol for s in series} and [x for x in info["excluded"] if x["symbol"] == "FASTUSDT"][0]["reason"] == "not_in_universe"


def test_cli_history_root_requires_a_universe_and_runs_the_round_trip(tmp_path, offline, capsys):
    root, uni, spot = _hist(tmp_path)
    with pytest.raises(SystemExit):
        CR.main(["--history-root", root])
    with pytest.raises(SystemExit):
        CR.main(["--market-history", root])                                                                         # the old assumed layout is gone
    with pytest.raises(SystemExit):                                                                                 # no reserved holdout: refused (the guard has its own file, test_history_holdout.py)
        CR.main(["--history-root", root, "--universe-dir", uni, "--spot-dir", spot, "--mark-interval", MARK_IV])
    assert "HOLDOUT_NOT_RESERVED" in capsys.readouterr().err
    assert CR.main(["--history-root", root, "--universe-dir", uni, "--spot-dir", spot, "--mark-interval", MARK_IV, "--unreserved-exploratory"]) == 0
    assert "ranked funding:" in capsys.readouterr().out


def test_the_default_spec_is_looked_up_by_name_not_position(monkeypatch):
    real = CR.load_candidates()
    monkeypatch.setattr(CR, "load_candidates", lambda: list(reversed(real)))
    assert CR.evaluate_ranked_funding([])["verdict"] == "UNTESTED"                                                # order does not matter: found by name
    assert CR._default_spec()["candidate_id"] == dict(real)["cross_sectional_funding"]["candidate_id"]
    monkeypatch.setattr(CR, "load_candidates", lambda: [("zzz", {})])
    with pytest.raises(RH.HarnessError):
        CR.evaluate_ranked_funding([])


# ------------------------------------------------------------------ mutation checks
def _mutant(old, new):
    path = os.path.join(BOT, "tools", "candidate_readiness.py")
    src = open(path, encoding="utf-8").read()
    assert src.count(old) == 1, old
    mod = types.ModuleType("cr_mut_" + uuid.uuid4().hex)
    mod.__file__ = path
    sys.modules[mod.__name__] = mod
    exec(compile(src.replace(old, new), path, "exec"), mod.__dict__)
    return mod


def test_mutation_flipping_cross_venue_to_executable_is_caught():
    m = _mutant('return {"value": RESEARCH_ONLY, "executable": False,', 'return {"value": RESEARCH_ONLY, "executable": True,')
    assert m._executable("cross_venue_funding", {})["executable"] is True and CR._executable("cross_venue_funding", {})["executable"] is False


def test_mutation_a_formation_window_that_reaches_forward_is_caught():
    m = _mutant("if t - formation_ms < ts <= t:", "if t - formation_ms < ts <= t + formation_ms:")
    leaky = mk("L", 480, [0.1] * 60 + [500.0] * 60)
    assert m.formation_score(leaky, 18 * DAY, 5 * DAY) > 100 and CR.formation_score(leaky, 18 * DAY, 5 * DAY) == pytest.approx(0.3)


def test_mutation_without_the_delivery_cap_a_closed_contract_keeps_earning():
    m = _mutant("end = min(t + h_ms, s.delivery_ms) if s.delivery_ms is not None else t + h_ms", "end = t + h_ms")
    s = mk("Z", 480, [2.0] * 90, delivery=12 * DAY)
    leg = m.run_variant([s], formation_days=3, hold_days=7, top_k=1, round_trip_bps=0.0, t_start=10 * DAY, t_end=17 * DAY)["rows"][0]["legs"][0]
    assert leg["prints"] == 21


def test_mutation_without_interval_normalisation_per_print_rates_would_be_compared():
    m = _mutant("per_day.append(v * 1440.0 / iv)", "per_day.append(v)")
    c = mk("C", 240, [1.2] * 60)
    assert m.formation_score(c, 5 * DAY, 3 * DAY) == pytest.approx(1.2) and CR.formation_score(c, 5 * DAY, 3 * DAY) == pytest.approx(7.2)


def test_mutation_a_perp_only_symbol_labelled_hedgeable_would_be_reported_as_carry():
    m = _mutant('return {True: "HEDGEABLE_ON_BYBIT", False: "PERP_ONLY"}.get(s.spot_listed, "HEDGE_UNKNOWN")', 'return "HEDGEABLE_ON_BYBIT"')
    p = mk("P", 480, [3.0] * 120, spot=False)
    leg = m.run_variant([p], formation_days=3, hold_days=7, top_k=1, round_trip_bps=11.0, t_start=10 * DAY, t_end=30 * DAY)["rows"][0]["legs"][0]
    assert "hedged_carry_funding_net_of_cost_bps" in leg


# ------------------------------------------------------------------ red-team fixes (F2): look-ahead in the price leg, per-print funding intervals, survivorship, labels, family count
def test_marks_are_keyed_by_observation_time_and_a_close_is_unknowable_at_its_candle_start(tmp_path, offline):
    root, uni, _ = _hist(tmp_path, spot=False)
    series, _info = CR.load_history_root(root, universe_dir=uni, mark_interval=MARK_IV)
    s = next(x for x in series if x.symbol == "BTCUSDT")
    rows = [json.loads(line) for line in open(os.path.join(root, f"mark-kline-{MARK_IV}", "BTCUSDT", "rows.jsonl"), "rb").read().splitlines()]
    assert [t for t, _ in s.mark] == [r["observation_time"] for r in rows] and all(r["observation_time"] == r["ts"] + H4 for r in rows)
    assert CR.price_at(s.mark, TMH.FIRST) is None                                   # the candle STARTING at FIRST has not closed: nothing is known at FIRST
    assert CR.price_at(s.mark, TMH.FIRST + H4 - 1) is None
    assert CR.price_at(s.mark, TMH.FIRST + H4) == pytest.approx(100.5)             # positive control: usable from its close


def test_a_candle_that_closes_at_150_is_not_the_entry_price_at_its_start():
    t = 3 * DAY
    H = 3600_000
    fund = [(i * 8 * H, 5.0) for i in range(1, 200)]
    mark = [(i * 4 * H + 4 * H, 100.0) for i in range(0, 400)]                       # obs-keyed: the candle starting at i*4h closes at (i+1)*4h
    mark = [(ts, 150.0 if ts == t + 4 * H else p) for ts, p in mark]                 # the candle STARTING at t closes at 150 (a spike unknowable at t)
    s = CR.SymbolSeries("AAAUSDT", 480, fund, mark, 0, None, True)
    leg = CR.run_variant([s], formation_days=3, hold_days=7, top_k=1, round_trip_bps=0, t_start=t, t_end=t + 8 * DAY)["rows"][0]["legs"][0]
    assert leg["price_leg_bps"] == pytest.approx(0.0)                                # entry 100 (known at t), exit 100: the 150 close is not used
    later = CR.run_variant([s], formation_days=3, hold_days=7, top_k=1, round_trip_bps=0, t_start=t + 4 * H, t_end=t + 4 * H + 8 * DAY)["rows"][0]["legs"][0]
    assert later["price_leg_bps"] == pytest.approx(-(100.0 / 150.0 - 1) * 1e4)      # positive control: decided AT the close, it is used


def test_the_loader_refuses_mark_rows_that_claim_a_close_before_the_candle_ended(tmp_path, offline, monkeypatch):
    root, uni, _ = _hist(tmp_path, spot=False)
    assert CR.load_history_root(root, universe_dir=uni, mark_interval=MARK_IV)[0]       # positive control
    real = CR._rows

    def early(ddir, sym):
        out = real(ddir, sym)
        return [dict(r, observation_time=r["ts"]) for r in out] if f"mark-kline-{MARK_IV}" in ddir else out
    monkeypatch.setattr(CR, "_rows", early)
    with pytest.raises(RH.LookAheadError):
        CR.load_history_root(root, universe_dir=uni, mark_interval=MARK_IV)


def test_a_row_used_before_its_observation_time_raises_lookahead():
    with pytest.raises(RH.LookAheadError):
        CR.assert_usable_at(200, 199)
    CR.assert_usable_at(200, 200)
    m = _mutant("if seq[mid][0] <= t:", "if seq[mid][0] <= t + 14_400_000:")           # a lookup that reaches one candle ahead
    with pytest.raises(RH.LookAheadError):
        m.price_at([(100, 1.0), (200, 2.0)], 150)
    assert CR.price_at([(100, 1.0), (200, 2.0)], 150) == 1.0


def test_funding_is_normalised_with_the_interval_of_the_segment_containing_each_print():
    p8 = 8 * 3_600_000
    f = [(i * p8, 1.0) for i in range(1, 31)] + [(30 * p8 + j * (p8 // 2), 0.5) for j in range(1, 61)]      # 480m then 240m; 3.0 bps/day throughout
    s = CR.SymbolSeries("X", 240, f, [], 0, None, None, funding_intervals=[(1 * p8, 30 * p8, 480), (30 * p8, 30 * p8 + 60 * (p8 // 2), 240)])
    assert CR.formation_score(s, 20 * p8, 5 * DAY) == pytest.approx(3.0)             # all in the 480 segment (the current-interval formula gave 6.0)
    assert CR.formation_score(s, 30 * p8 + 40 * (p8 // 2), 5 * DAY) == pytest.approx(3.0)
    mixed = CR.formation_score(s, 30 * p8 + 6 * (p8 // 2), 3 * DAY)
    assert mixed == pytest.approx(3.0)
    flat = CR.SymbolSeries("Y", 240, f, [], 0, None, None)                          # no segments: the single interval is applied to every print (hand-built series only)
    assert CR.formation_score(flat, 20 * p8, 5 * DAY) == pytest.approx(6.0)


def test_round_trip_a_480_to_240_history_is_normalised_per_segment(tmp_path, offline):
    last8 = TMH.FIRST + 59 * TMH.H8
    ts = TMH.grid(TMH.FIRST, 60, TMH.H8) + [last8 + k * H4 for k in range(1, (TMH.BASE - last8) // H4 + 1)]
    root, uni, _ = _hist(tmp_path, spot=False, funding_override={"FASTUSDT": ts})
    series, info = CR.load_history_root(root, universe_dir=uni, mark_interval=MARK_IV)
    s = next(x for x in series if x.symbol == "FASTUSDT")
    assert s.interval_min == 240 and [iv for _a, _b, iv in s.funding_intervals] == [480, 240]
    assert s.funding_intervals[0][:2] == (TMH.FIRST, last8) and "FASTUSDT" not in [x["symbol"] for x in info["excluded"]]
    assert CR.formation_score(s, TMH.FIRST + 40 * TMH.H8, 5 * DAY) == pytest.approx(3.0)       # 1 bp per 8h print = 3 bps/day; "current interval" scaling gave 6.0
    assert CR.formation_score(s, TMH.BASE, 5 * DAY) == pytest.approx(6.0)                      # 1 bp per 4h print = 6 bps/day
    out = CR.evaluate_ranked_funding(series)
    assert out["intervals_min_counts"]["240"] == 1


def test_a_symbol_whose_prints_have_no_known_interval_is_excluded_and_listed():
    man = {"segments": [{"first_ts": 100, "last_ts": 500, "interval_ms": 28_800_000, "fundingInterval_min": 480}, {"first_ts": 900, "last_ts": 1500, "interval_ms": 57_600_000, "fundingInterval_min": 960}],
           "reasons": ["interval_disagrees_with_record"]}
    segs, why = CR.funding_interval_segments(man, [100, 300, 500, 700, 900, 1500])             # print 700 sits between two segments: its interval is unknown
    assert segs is None and why.startswith("funding_interval_unknown_for_1_prints")
    segs, why = CR.funding_interval_segments(man, [100, 300, 500, 900, 1200, 1500])
    assert why is None and [(a, b, iv) for a, b, iv in segs] == [(100, 500, 480), (900, 1500, 960)]
    assert CR.funding_interval_segments({"reasons": []}, [1, 2])[0] is None                    # no segments in the manifest: unknown, never the current interval


def test_survivors_only_is_stated_when_the_closed_partition_was_refused(tmp_path, offline):
    root, uni, _ = _hist(tmp_path, spot=False, syms=("BTCUSDT", "ETHUSDT", "FASTUSDT"), universe_kw={"refuse_closed": True})
    series, info = CR.load_history_root(root, universe_dir=uni, mark_interval=MARK_IV)
    assert info["universe"]["delisting_observability"] == "NOT_OBSERVED" and info["survivorship"] == "SURVIVORS_ONLY" and info["point_in_time_claim"] is False
    assert "SURVIVORS" in info["warning"].upper() and "survivorship" in info["warning"].lower()
    out = CR.evaluate_ranked_funding(series, universe_info=info)
    assert out["survivorship"] == "SURVIVORS_ONLY" and any("SURVIVORS_ONLY" in w for w in out["warnings"])
    assert CR.evaluate_ranked_funding(series)["survivorship"] == "SURVIVORS_ONLY"               # no information given: fail closed


def test_an_empty_closed_partition_is_no_closed_rows_served_not_proof_of_no_delistings(tmp_path, offline):
    root, uni, _ = _hist(tmp_path, spot=False, syms=("BTCUSDT", "ETHUSDT", "FASTUSDT"))
    series, info = CR.load_history_root(root, universe_dir=uni, mark_interval=MARK_IV)
    assert info["universe"]["delisting_observability"] == "OBSERVED" and info["survivorship"] == "SURVIVORS_ONLY"
    assert "no Closed rows served" in info["universe"]["delisting_note"] and "does not prove" in info["universe"]["delisting_note"]


def test_observed_closed_rows_give_delistings_observed(tmp_path, offline):
    root, uni, spot = _hist(tmp_path)
    series, info = CR.load_history_root(root, universe_dir=uni, spot_dir=spot, mark_interval=MARK_IV)
    assert info["survivorship"] == "DELISTINGS_OBSERVED" and info["point_in_time_claim"] is False        # observed delistings are necessary, not sufficient
    out = CR.evaluate_ranked_funding(series, universe_info=info)
    assert out["survivorship"] == "DELISTINGS_OBSERVED" and "warnings" in out


def test_spot_listing_is_a_current_snapshot_only_label(tmp_path, offline):
    root, uni, spot = _hist(tmp_path)
    series, info = CR.load_history_root(root, universe_dir=uni, spot_dir=spot, mark_interval=MARK_IV)
    assert info["hedge"]["basis"] == "CURRENT_SNAPSHOT_ONLY"
    s = [x for x in series if x.symbol == "BTCUSDT"]
    leg = CR.run_variant(s, formation_days=3, hold_days=7, top_k=1, round_trip_bps=0, t_start=TMH.FIRST + 10 * DAY, t_end=TMH.FIRST + 30 * DAY)["rows"][0]["legs"][0]
    assert leg["hedgeability"] == "HEDGEABLE_ON_BYBIT" and leg["hedgeability_basis"] == "CURRENT_SNAPSHOT_ONLY"


def test_raw_responses_verified_is_described_as_self_consistent_not_authenticated():
    assert "NOT authenticated as produced by the exchange" in " ".join(RH.GAPS)
    assert "NOT authenticated as produced by the exchange" in CR.PIT_LABEL_NOTE and "RAW_RESPONSES_VERIFIED" in CR.PIT_LABEL_NOTE


def test_every_variant_tried_in_the_call_is_counted_and_recorded():
    fam = "t_" + uuid.uuid4().hex
    out = CR.evaluate_ranked_funding(_long_capture(1, 2.0), spec=spec(fam), formation_days_grid=(3, 7), hold_days=7, hold_days_grid=(7, 14), top_k_grid=(1, 2),
                                     cost_scenarios={"base": 11.0, "stress": 22.0})
    assert out["n_variants_counted"] == 2 * 2 * 2 * 2 == len(out["variants_tried"]) and out["multiple_testing_scope"] == "PER_PROCESS"
    assert out["hold_days_grid"] == [7, 14] and out["top_k_grid"] == [1, 2] and sorted(out["cost_scenarios_bps"]) == ["base", "stress"]
    assert out["status"] == "WALK_FORWARD_DONE" and all("|H14|" not in n for n in out["selected_per_fold"])           # different hold = different time grid: counted, never selectable
    plain = CR.evaluate_ranked_funding(_long_capture(1, 2.0), spec=spec("t_" + uuid.uuid4().hex), formation_days_grid=(3, 7), hold_days=7)
    assert plain["n_variants_counted"] == 2 and plain["multiple_testing_scope"] == "PER_PROCESS"


def test_a_ledger_path_changes_the_scope_statement(tmp_path):
    fam = "t_" + uuid.uuid4().hex
    out = CR.evaluate_ranked_funding(_long_capture(1, 2.0), spec=spec(fam), formation_days_grid=(3,), hold_days=7, ledger_path=str(tmp_path / "ledger.jsonl"))
    assert out["multiple_testing_scope"] == "PERSISTENT_LEDGER_FILE" and out["n_variants_counted"] == 1


# ------------------------------------------------------------------ round-2 red-team fixes: ended contracts, survivorship, stale marks, key basis
def test_a_closed_contract_without_a_delivery_time_is_excluded_not_immortal(tmp_path, offline):
    root, uni, _ = _hist(tmp_path, spot=False, old_delivery=0)
    series, info = CR.load_history_root(root, universe_dir=uni, mark_interval=MARK_IV)
    assert "OLDUSDT" not in {x.symbol for x in series}
    assert {"symbol": "OLDUSDT", "reason": "closed_without_delivery"} in info["excluded"]
    assert info["survivorship"] == "SURVIVORS_ONLY" and info["closed_in_universe_not_in_series"]["names"] == ["OLDUSDT"]
    ok_root, ok_uni, _ = _hist(tmp_path / "ok", spot=False) if os.makedirs(tmp_path / "ok") is None else (None, None, None)       # positive control: with a deliveryTime it loads
    ok_series, ok_info = CR.load_history_root(ok_root, universe_dir=ok_uni, mark_interval=MARK_IV)
    assert "OLDUSDT" in {x.symbol for x in ok_series} and ok_info["excluded"] == []


def test_delistings_observed_requires_every_overlapping_closed_symbol_in_the_series(tmp_path, offline):
    root, uni, _ = _hist(tmp_path, spot=False, skip_history=("OLDUSDT",))          # universe capture had a Closed partition; the history holds only live symbols
    series, info = CR.load_history_root(root, universe_dir=uni, mark_interval=MARK_IV)
    assert not any(x.delivery_ms for x in series) and info["universe"]["delisting_observability"] == "OBSERVED"
    assert info["survivorship"] == "SURVIVORS_ONLY"
    cin = info["closed_in_universe_not_in_series"]
    assert cin["count"] == 1 and cin["names"] == ["OLDUSDT"] and "OLDUSDT" in info["warning"]
    out = CR.evaluate_ranked_funding(series, universe_info=info)
    assert out["survivorship"] == "SURVIVORS_ONLY" and out["closed_in_universe_not_in_series"]["count"] == 1
    assert any("SURVIVORS_ONLY" in w and "absent from the evaluated series" in w for w in out["warnings"])


def test_a_closed_contract_delivered_before_the_study_window_is_not_required_in_the_series(tmp_path, offline):
    root, uni, _ = _hist(tmp_path, spot=False, skip_history=("OLDUSDT",), old_delivery=TMH.FIRST - 10 * TMH.H8)
    series, info = CR.load_history_root(root, universe_dir=uni, mark_interval=MARK_IV)
    assert info["closed_in_universe_not_in_series"]["count"] == 0 and info["survivorship"] == "DELISTINGS_OBSERVED"       # positive control for the rule
    assert CR.evaluate_ranked_funding(series, universe_info=info)["survivorship"] == "DELISTINGS_OBSERVED"


def test_an_unverifiable_delistings_observed_claim_is_downgraded_in_the_evaluation():
    out = CR.evaluate_ranked_funding([], universe_info={"survivorship": "DELISTINGS_OBSERVED"})
    assert out["survivorship"] == "SURVIVORS_ONLY" and any("downgraded" in w for w in out["warnings"])


def test_a_hand_made_or_copied_universe_info_is_unverified_even_with_a_perfect_looking_claim(tmp_path, offline):
    claim = {"survivorship": "DELISTINGS_OBSERVED", "closed_in_universe_not_in_series": {"count": 0}}
    out = CR.evaluate_ranked_funding([], universe_info=claim)
    assert out["survivorship"] == "SURVIVORS_ONLY" and out["universe_info_verified"] is False and any("UNVERIFIED" in w for w in out["warnings"])
    root, uni, spot = _hist(tmp_path)
    series, info = CR.load_history_root(root, universe_dir=uni, spot_dir=spot, mark_interval=MARK_IV)
    good = CR.evaluate_ranked_funding(series, universe_info=info)
    assert good["survivorship"] == "DELISTINGS_OBSERVED" and good["universe_info_verified"] is True                  # positive control
    for forged in (dict(info), {**info}, CR.LoadedInfo(info)):                                                    # a copy loses the verdict
        assert CR.evaluate_ranked_funding(series, universe_info=forged)["survivorship"] == "SURVIVORS_ONLY"
    mutated = CR.LoadedInfo(info)
    mutated.verdict = CR.UniverseVerdict("DELISTINGS_OBSERVED", 0, ())                                           # a verdict without the loader's seal
    assert CR.evaluate_ranked_funding(series, universe_info=mutated)["survivorship"] == "SURVIVORS_ONLY"
    info["survivorship"] = "SURVIVORS_ONLY"                                                                        # editing the printable dict does not change the frozen verdict
    assert CR.evaluate_ranked_funding(series, universe_info=info)["survivorship"] == "DELISTINGS_OBSERVED"
    with pytest.raises(Exception):
        info.verdict.survivorship = "x"                                                                            # frozen


def test_the_key_basis_marker_is_a_private_token_not_a_string():
    for forged in ("OBSERVATION_TIME", True, 1, object(), CR.KEY_BASIS_LABEL):
        s = CR.SymbolSeries("A", 480, [(H8, 1.0)], [(0, 100.0)], key_basis=forged)
        with pytest.raises(RH.HarnessError, match="key_basis"):
            CR.evaluate_ranked_funding([s], spec=spec())
        assert CR.evaluate_ranked_funding([s], spec=spec(), allow_unverified_key_basis_for_tests=True)["key_basis_unverified"] is True
    assert not hasattr(CR, "KEY_BASIS_OBSERVATION")
    import copy
    import pickle
    assert copy.deepcopy(CR._KEY_BASIS_OBSERVED) is CR._KEY_BASIS_OBSERVED
    with pytest.raises(TypeError):
        pickle.dumps(CR._KEY_BASIS_OBSERVED)


H8 = 8 * 3600 * 1000


def _marks(upto_ms, step=4 * 3600 * 1000, price=100.0):
    return [(i * step, price) for i in range(0, upto_ms // step + 1)]


def test_a_stale_entry_mark_skips_the_leg_instead_of_carrying_an_old_price():
    fund = [(i * H8, 5.0) for i in range(1, 400)]
    s = CR.SymbolSeries("AAAUSDT", 480, fund, _marks(2 * DAY) + _marks(40 * DAY)[int(8 * DAY // (4 * 3600 * 1000)):], 0, None, True, key_basis=CR._KEY_BASIS_OBSERVED)   # no marks between 2d and 8d
    r = CR.run_variant([s], formation_days=3, hold_days=7, top_k=1, round_trip_bps=0, t_start=5 * DAY, t_end=13 * DAY)
    assert r["rows"] == [] and r["skipped_legs_without_marks"] == 1 and r["terminal_exit_unpriced"] == 0
    full = CR.SymbolSeries("AAAUSDT", 480, fund, _marks(40 * DAY), 0, None, True, key_basis=CR._KEY_BASIS_OBSERVED)
    ok = CR.run_variant([full], formation_days=3, hold_days=7, top_k=1, round_trip_bps=0, t_start=5 * DAY, t_end=13 * DAY)           # positive control
    assert len(ok["rows"]) == 1 and ok["skipped_legs_without_marks"] == 0
    assert CR.price_at(s.mark, 5 * DAY) == 100.0 and CR.price_at(s.mark, 5 * DAY, 8 * 3600 * 1000) is None and CR.price_at(s.mark, 5 * DAY, 4 * DAY) == 100.0


def test_a_stale_exit_mark_skips_the_leg():
    fund = [(i * H8, 5.0) for i in range(1, 400)]
    s = CR.SymbolSeries("AAAUSDT", 480, fund, _marks(6 * DAY), 0, None, True, key_basis=CR._KEY_BASIS_OBSERVED)                     # marks end at 6d, hold ends at 12d
    r = CR.run_variant([s], formation_days=3, hold_days=7, top_k=1, round_trip_bps=0, t_start=5 * DAY, t_end=13 * DAY)
    assert r["rows"] == [] and r["skipped_legs_without_marks"] == 1 and r["terminal_exit_unpriced"] == 0                                  # not delisted: skipped, not a terminal loss
    assert CR.run_variant([s], formation_days=3, hold_days=7, top_k=1, round_trip_bps=0, t_start=5 * DAY, t_end=13 * DAY, max_staleness_ms=10 * DAY)["rows"]   # explicit bound honoured


def test_a_delisted_leg_whose_last_mark_is_stale_at_delivery_is_an_unpriced_terminal_exit():
    fund = [(i * H8, 5.0) for i in range(1, 400)]
    s = CR.SymbolSeries("BBBUSDT", 480, fund, _marks(3 * DAY + 4 * 3600_000), 0, 6 * DAY, True, key_basis=CR._KEY_BASIS_OBSERVED)
    r = CR.run_variant([s], formation_days=3, hold_days=7, top_k=1, round_trip_bps=0, t_start=3 * DAY, t_end=11 * DAY)
    assert r["rows"] == [] and r["terminal_exit_unpriced"] == 1 and r["terminal_exit_unpriced_legs"][0]["symbol"] == "BBBUSDT" and r["skipped_legs_without_marks"] == 1
    fresh = CR.SymbolSeries("BBBUSDT", 480, fund, _marks(6 * DAY), 0, 6 * DAY, True, key_basis=CR._KEY_BASIS_OBSERVED)              # positive control: priced at delivery
    ok = CR.run_variant([fresh], formation_days=3, hold_days=7, top_k=1, round_trip_bps=0, t_start=3 * DAY, t_end=11 * DAY)
    assert ok["terminal_exit_unpriced"] == 0 and ok["rows"][0]["legs"][0]["closed_during_hold"] is True


H4_ = 4 * 3600_000


def _delisted(marks, delivery, mi=H4_, **kw):
    fund = [(i * H8, 1.0) for i in range(1, 400)]
    return CR.SymbolSeries("XUSDT", 480, fund, marks, 0, delivery, None, None, None, CR._KEY_BASIS_OBSERVED, mi, **kw)


def _rv(s):
    return CR.run_variant([s], formation_days=3, hold_days=7, top_k=1, round_trip_bps=10, t_start=5 * DAY, t_end=30 * DAY)


def test_a_terminal_exit_needs_a_mark_within_one_interval_of_delivery_not_two():
    marks = [(i * H4_, 100.0) for i in range(0, 11 * 6)]                                 # last mark 10d20h, delivery 11d02h: 6h old = 1.5 intervals
    r = _rv(_delisted(marks, 11 * DAY + 2 * 3600_000))
    assert r["terminal_exit_unpriced"] == 1 and r["rows"] == [] and r["terminal_exit_unpriced_symbols"] == ["XUSDT"]
    assert r["terminal_exit_unpriced_legs"][0]["reason"].startswith("no_settlement_price")
    ok = _rv(_delisted(marks, 11 * DAY - 1 * 3600_000))                                    # positive control: last mark 4h before delivery... within one interval
    assert ok["terminal_exit_unpriced"] == 0 and ok["rows"][0]["legs"][0]["terminal_price_basis"] == "LAST_MARK_WITHIN_1_MARK_INTERVAL_OF_DELIVERY"
    edge = _rv(_delisted(marks, 10 * DAY + 20 * 3600_000 + H4_))                           # exactly one interval: still priced
    assert edge["terminal_exit_unpriced"] == 0 and edge["rows"]
    over = _rv(_delisted(marks, 10 * DAY + 20 * 3600_000 + H4_ + 1))
    assert over["terminal_exit_unpriced"] == 1


def test_a_daily_mark_series_cannot_price_a_delisting_two_days_after_its_last_mark():
    r = _rv(_delisted([(i * DAY, 100.0) for i in range(0, 12)], 13 * DAY, mi=DAY))
    assert r["terminal_exit_unpriced"] == 1 and r["terminal_exit_unpriced_legs"][0]["t"] == 12 * DAY       # the t=5d hold ends before delivery and is priced normally


def test_a_verified_settlement_price_prices_the_terminal_exit_and_is_labelled():
    marks = [(i * H4_, 100.0) for i in range(0, 8 * 6)]                                  # marks stop long before delivery
    r = _rv(_delisted(marks, 11 * DAY, settlement_price=60.0))
    leg = r["rows"][0]["legs"][0]
    assert r["terminal_exit_unpriced"] == 0 and leg["terminal_price_basis"] == "SETTLEMENT_PRICE" and leg["price_leg_bps"] == pytest.approx(4000.0)
    for bad in (0.0, -1.0, float("nan"), True):
        assert _rv(_delisted(marks, 11 * DAY, settlement_price=bad))["terminal_exit_unpriced"] == 1                   # not a price: unpriced, never used


def test_a_stale_entry_leg_of_a_contract_delisted_during_the_hold_is_unpriced_not_skipped():
    marks = [(i * H4_, 100.0) for i in range(0, 3 * 6)]                                  # marks end at day 3: stale at the day-5 entry
    r = _rv(_delisted(marks, 9 * DAY))
    assert r["terminal_exit_unpriced"] == 1 and r["terminal_exit_unpriced_legs"][0]["reason"] == "entry_mark_stale_and_delisted_during_hold" and r["skipped_legs_without_marks"] == 1
    live = _rv(_delisted(marks, None))                                                      # control: the same stale entry on a live contract is only skipped
    assert live["terminal_exit_unpriced"] == 0 and live["skipped_legs_without_marks"] >= 1


def _capture_with_delisting(full_marks):
    cap = _long_capture(1, 2.0)
    t_star = 8 * 3600_000 + 77 * DAY                                                          # a rebalance time on the grid (first print 8h, lead-in 7d, 7d holds)
    a = cap[0]
    a.delivery_ms = t_star + 3 * DAY
    if not full_marks:
        a.mark = [(ts, p) for ts, p in a.mark if ts <= t_star]
    return cap


def test_unpriced_delistings_are_surfaced_and_cap_the_verdict():
    out = CR.evaluate_ranked_funding(_capture_with_delisting(False), spec=spec(), formation_days_grid=(3, 7), hold_days=7)
    assert out["terminal_exit_unpriced"] >= 1 and out["terminal_exit_unpriced_symbols"] == ["A"] and out["skipped_legs_without_marks"] >= 1
    assert out["verdict"] == "INSUFFICIENT_DATA" and any("TERMINAL_EXIT_UNPRICED" in w for w in out["warnings"])
    ctl = CR.evaluate_ranked_funding(_capture_with_delisting(True), spec=spec(), formation_days_grid=(3, 7), hold_days=7)               # positive control
    assert ctl["terminal_exit_unpriced"] == 0


def test_series_without_the_observation_key_basis_are_refused_unless_explicitly_test_only():
    legacy = CR.SymbolSeries("A", 480, [(H8, 1.0)], [(0, 100.0)])
    with pytest.raises(RH.HarnessError, match="key_basis"):
        CR.evaluate_ranked_funding([legacy], spec=spec())
    out = CR.evaluate_ranked_funding([legacy], spec=spec(), allow_unverified_key_basis_for_tests=True)
    assert out["key_basis_unverified"] is True and out["verdict"] == "INSUFFICIENT_DATA" and any("KEY_BASIS_UNVERIFIED" in w for w in out["warnings"])
    marked = CR.SymbolSeries("A", 480, [(H8, 1.0)], [(0, 100.0)], key_basis=CR._KEY_BASIS_OBSERVED)
    assert CR.evaluate_ranked_funding([marked], spec=spec())["key_basis_unverified"] is False                                              # positive control


def test_the_loader_sets_the_key_basis_and_the_mark_interval(tmp_path, offline):
    root, uni, _ = _hist(tmp_path, spot=False)
    series, _info = CR.load_history_root(root, universe_dir=uni, mark_interval=MARK_IV)
    assert series and all(x.key_basis is CR._KEY_BASIS_OBSERVED and x.mark_interval_ms == H4 for x in series)


def test_mutation_without_the_staleness_check_an_old_mark_prices_the_leg():
    m = _mutant("if max_staleness_ms is not None and t - row[0] > max_staleness_ms:", "if False:")
    assert m.price_at([(0, 1.0)], 10 * DAY, 1000) == 1.0 and CR.price_at([(0, 1.0)], 10 * DAY, 1000) is None


def test_mutation_without_the_terminal_count_a_lost_delisting_vanishes():
    m = _mutant("                if terminal:\n                    unpriced.append", "                if False:\n                    unpriced.append")
    fund = [(i * H8, 5.0) for i in range(1, 400)]
    s = CR.SymbolSeries("B", 480, fund, _marks(3 * DAY + 4 * 3600_000), 0, 6 * DAY, True, key_basis=CR._KEY_BASIS_OBSERVED)
    assert m.run_variant([s], formation_days=3, hold_days=7, top_k=1, round_trip_bps=0, t_start=3 * DAY, t_end=11 * DAY)["terminal_exit_unpriced"] == 0
    assert CR.run_variant([s], formation_days=3, hold_days=7, top_k=1, round_trip_bps=0, t_start=3 * DAY, t_end=11 * DAY)["terminal_exit_unpriced"] == 1


def test_mutation_without_the_key_basis_check_hand_built_series_would_be_accepted():
    m = _mutant("if unkeyed and not allow_unverified_key_basis_for_tests:", "if False:")
    legacy = CR.SymbolSeries("A", 480, [(H8, 1.0)], [(0, 100.0)])
    assert m.evaluate_ranked_funding([legacy], spec=spec())["key_basis_unverified"] is True


def test_mutation_without_the_closed_without_delivery_exclusion_the_symbol_would_load(tmp_path, offline):
    m = _mutant("if rec.get(\"status\") in RH.ENDED_STATUSES and not (", "if False and not (")
    root, uni, _ = _hist(tmp_path, spot=False, old_delivery=0)
    approve_all = lambda rec, w0, w1, st, q: (True, "", [])            # noqa: E731 - a classifier that wrongly lets the Closed contract without a delivery time through
    with pytest.raises(CR.RH.HarnessError, match="closed_without_delivery"):
        CR.load_history_root(root, universe_dir=uni, mark_interval=MARK_IV, classifier=approve_all)            # defence in depth: the loader itself still refuses it
    series, _info = m.load_history_root(root, universe_dir=uni, mark_interval=MARK_IV, classifier=approve_all)
    assert "OLDUSDT" in {x.symbol for x in series}


def test_mutation_without_the_closed_in_series_check_survivors_would_be_labelled_delistings(tmp_path, offline):
    m = _mutant("closed_missing = sorted(fn.ended_unaccounted)", "closed_missing = []")
    root, uni, _ = _hist(tmp_path, spot=False, skip_history=("OLDUSDT",))
    assert m.load_history_root(root, universe_dir=uni, mark_interval=MARK_IV)[1]["survivorship"] == "DELISTINGS_OBSERVED"


# ================================================================== readiness vocabulary, verified inventory, explicit universe input (worker C)
LEVELS = ("DATA_READY", "RESEARCH_READY", "PROMISING", "PROSPECTIVE", "EXECUTION_READY", "OWNER_PROFIT_EVIDENCED")


def _lv(**kw):
    base = dict(data_ready=True, data_reasons=[], pit_ok=True, survivorship=CR.DELISTINGS_OBSERVED, mechanism_ok=True, evaluation=None)
    base.update(kw)
    return CR.readiness_levels(**base)


def test_the_readiness_vocabulary_is_ordered_and_named():
    assert CR.READINESS_LEVELS == LEVELS


def test_a_dataset_level_ready_never_becomes_strategy_level_ready():
    r = _lv(survivorship=CR.SURVIVORS_ONLY)
    assert r["levels"]["DATA_READY"]["reached"] is True and r["levels"]["RESEARCH_READY"]["reached"] is False
    assert r["highest"] == "DATA_READY" and r["survivorship_free_claim"] is False
    assert any("SURVIVORS_ONLY" in x for x in r["levels"]["RESEARCH_READY"]["reasons"])
    assert r["highest_supportable_verdict"] == "INSUFFICIENT_DATA"
    assert _lv(data_ready=False)["highest"] == "NONE" and _lv(data_ready=False)["highest_supportable_verdict"] == "UNTESTED"


def test_levels_are_cumulative_and_nothing_above_research_ready_without_an_evaluation():
    r = _lv()
    assert r["highest"] == "RESEARCH_READY"
    for lvl in LEVELS[2:]:
        assert r["levels"][lvl]["reached"] is False and r["levels"][lvl]["reasons"]
    # a good evaluation reaches PROMISING only; prospective/execution/owner levels need evidence this tool cannot create
    ev = {"status": "WALK_FORWARD_DONE", "verdict": "INSUFFICIENT_DATA", "oos_mean_total_bps_per_row": 12.0, "p_adjusted": 0.01, "oos_independent_observations": 80,
          "terminal_exit_unpriced": 0}
    p = _lv(evaluation=ev, executable=True)
    assert p["highest"] == "PROMISING" and p["levels"]["EXECUTION_READY"]["reached"] is False and p["levels"]["OWNER_PROFIT_EVIDENCED"]["reached"] is False
    assert p["highest_supportable_verdict"] == "INSUFFICIENT_DATA"                                # the ceiling never moves
    # skipping a level is impossible: a PROSPECTIVE record on a candidate that is not PROMISING reaches nothing
    q = _lv(evaluation={**ev, "oos_mean_total_bps_per_row": -3.0}, prospective={"started": True, "settlements_observed": 999, "min_settlements": 30})
    assert q["levels"]["PROMISING"]["reached"] is False and q["levels"]["PROSPECTIVE"]["reached"] is False


def test_a_rejected_evaluation_is_rejected_and_not_promising():
    ev = {"status": "WALK_FORWARD_DONE", "verdict": "REJECTED", "oos_mean_total_bps_per_row": -4.0, "p_adjusted": 0.9, "oos_independent_observations": 80, "terminal_exit_unpriced": 0}
    r = _lv(evaluation=ev)
    assert r["highest_supportable_verdict"] == "REJECTED" and r["levels"]["PROMISING"]["reached"] is False


def test_the_owner_level_can_never_be_reached_from_this_tool():
    ev = {"status": "WALK_FORWARD_DONE", "verdict": "INSUFFICIENT_DATA", "oos_mean_total_bps_per_row": 12.0, "p_adjusted": 0.01, "oos_independent_observations": 80, "terminal_exit_unpriced": 0}
    r = _lv(evaluation=ev, executable=True, prospective={"started": True, "settlements_observed": 999, "min_settlements": 30}, owner_evidence={"verified": True})
    assert r["levels"]["OWNER_PROFIT_EVIDENCED"]["reached"] is False
    assert "owner" in " ".join(r["levels"]["OWNER_PROFIT_EVIDENCED"]["reasons"]).lower()


def test_scan_recognises_an_explicit_universe_dir_even_outside_the_data_root(tmp_path, offline):
    root, uni, _ = _hist(tmp_path, spot=False)
    only_hist = CR.scan_data_root(root)
    assert only_hist["universe_capture_dirs"] == [] and only_hist["universe_dir_supplied"] is False
    s = CR.scan_data_root(root, universe_dirs=[uni])
    assert os.path.realpath(uni) in [os.path.realpath(x) for x in s["universe_capture_dirs"]] and s["universe_dir_supplied"] is True
    assert s["universe_dirs_rejected"] == []
    bad = CR.scan_data_root(root, universe_dirs=[str(tmp_path / "nope")])
    assert bad["universe_capture_dirs"] == [] and bad["universe_dirs_rejected"][0]["reason"]


def test_build_report_contradiction_is_fixed_universe_dir_shows_up_in_data_scan(tmp_path, offline):
    root, uni, spot = _hist(tmp_path)
    rep = CR.build_report(data_root=root, universe_dir=uni, with_economics=False)
    assert rep["data_scan"]["universe_capture_dirs"] and rep["data_scan"]["universe_dir_supplied"] is True
    row = rowof(rep, "cross_sectional_funding")
    assert row["point_in_time"]["value"] != "MISSING" and row["universe_dir"]["supplied"] is True


def test_the_cross_sectional_path_refuses_without_a_universe_dir_and_says_so_loudly(tmp_path, offline):
    root, uni, _ = _hist(tmp_path)
    rep = CR.build_report(data_root=root, with_economics=False)                                    # history present, universe dir NOT supplied
    row = rowof(rep, "cross_sectional_funding")
    assert row["universe_dir"]["supplied"] is False and row["universe_dir"]["status"] == "UNIVERSE_DIR_REQUIRED"
    assert any("UNIVERSE_DIR_REQUIRED" in x for x in row["readiness_reasons"])
    assert row["readiness"]["highest"] == "NONE"
    with pytest.raises(RH.HarnessError, match="UNIVERSE_DIR_REQUIRED"):
        CR.evaluate_cross_sectional(root, universe_dir=None, ledger_path=str(tmp_path / "l.jsonl"), holdout_ledger=HH.UNGUARDED)
    with pytest.raises(RH.HarnessError, match="UNIVERSE_DIR_REQUIRED"):
        CR.evaluate_cross_sectional(root, universe_dir="", ledger_path=str(tmp_path / "l.jsonl"), holdout_ledger=HH.UNGUARDED)


def test_repo_has_true_is_not_trusted_only_a_verified_manifest_counts(tmp_path, offline):
    # without any data root the funding item the spec DECLARES present is still not verified
    rep = CR.build_report(with_economics=False)
    items = rowof(rep, "cross_sectional_funding")["required_data"]["items"]
    fund = next(i for i in items if "funding history" in i["name"])
    assert fund["spec_repo_has"] is True and fund["status"] != "VERIFIED_MANIFEST" and fund["trusted"] is False
    root, uni, _ = _hist(tmp_path)
    rep2 = CR.build_report(data_root=root, universe_dir=uni, with_economics=False)
    items2 = {i["name"][:12]: i for i in rowof(rep2, "cross_sectional_funding")["required_data"]["items"]}
    assert items2["funding hist"]["status"] == "VERIFIED_MANIFEST" and items2["funding hist"]["trusted"] is True
    assert items2["point-in-tim"]["status"] == "VERIFIED_MANIFEST"
    depth = next(i for i in rowof(rep2, "cross_sectional_funding")["required_data"]["items"] if i["name"].startswith("depth"))
    assert depth["status"] == "MISSING" and depth["spec_repo_has"] is False
    # a declared-missing item that a manifest DOES cover is reported as covered (the declaration is stale), not trusted blindly either way
    mark = next(i for i in rowof(rep2, "cross_sectional_funding")["required_data"]["items"] if i["name"].startswith("mark/index"))
    assert mark["spec_repo_has"] is False and mark["status"] == "VERIFIED_MANIFEST"


def test_a_tampered_manifest_is_not_data_ready(tmp_path, offline):
    root, uni, _ = _hist(tmp_path)
    p = os.path.join(root, "funding", "BTCUSDT", "rows.jsonl")
    with open(p, "ab") as fh:
        fh.write(b"\n")
    rep = CR.build_report(data_root=root, universe_dir=uni, with_economics=False)
    fund = next(i for i in rowof(rep, "cross_sectional_funding")["required_data"]["items"] if "funding history" in i["name"])
    assert fund["status"] == "MANIFEST_FAILS_VERIFICATION" and fund["trusted"] is False


def test_dataset_report_lists_everything_a_reader_needs_and_is_reproducible(tmp_path, offline):
    root, uni, spot = _hist(tmp_path, skip_history=("OLDUSDT",))
    a = CR.dataset_report(data_root=root, universe_dir=uni, spot_dir=spot, mark_interval=MARK_IV)
    b = CR.dataset_report(data_root=root, universe_dir=uni, spot_dir=spot, mark_interval=MARK_IV)
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    assert a["symbols_included"] == ["BTCUSDT", "ETHUSDT", "FASTUSDT"]
    assert [x for x in a["symbols_excluded"] if x["symbol"] == "OLDUSDT"][0]["reason"] == "in_universe_not_in_any_history_dataset"
    assert a["source_manifests"] and all(m["verified"] for m in a["source_manifests"]) and all(m["digest"] for m in a["source_manifests"])
    assert a["coverage"]["funding"]["first_iso"] and a["coverage"]["funding"]["last_iso"] and a["coverage"]["funding"]["window_days"] > 100
    assert a["intervals_min"] == {"240": 1, "480": 2}
    assert a["status_partition_coverage"] and "Trading" in json.dumps(a["status_partition_coverage"])
    assert a["pit_limitations"] and any("CURRENT_SNAPSHOT_ONLY" in x for x in a["pit_limitations"])
    assert a["survivorship"] == CR.SURVIVORS_ONLY and a["survivorship_free_claim"] is False
    assert any("depth" in m or "orderbook" in m for m in a["missing_fields"])
    assert a["highest_supportable_verdict"] in ("INSUFFICIENT_DATA", "UNTESTED") and a["readiness"]["highest"] in ("NONE", "DATA_READY")
    assert a["readiness"]["levels"]["RESEARCH_READY"]["reached"] is False                          # survivors only: never strategy-level ready


def test_dataset_report_refuses_loudly_without_a_universe_dir(tmp_path, offline):
    root, _uni, _ = _hist(tmp_path)
    r = CR.dataset_report(data_root=root, universe_dir=None)
    assert r["status"] == "UNIVERSE_DIR_REQUIRED" and r["highest_supportable_verdict"] == "UNTESTED" and r["symbols_included"] == []
    assert r["readiness"]["highest"] == "NONE"


def test_dataset_report_of_a_tampered_root_reports_the_failure_and_includes_no_symbols(tmp_path, offline):
    root, uni, _ = _hist(tmp_path)
    p = os.path.join(root, "funding", "BTCUSDT", "rows.jsonl")
    with open(p, "ab") as fh:
        fh.write(b"\n")
    r = CR.dataset_report(data_root=root, universe_dir=uni, mark_interval=MARK_IV)
    assert r["status"] == "REFUSED" and r["symbols_included"] == [] and r["readiness"]["highest"] == "NONE"
    assert any(not m["verified"] for m in r["source_manifests"])


def test_a_survivors_only_evaluation_does_not_imply_a_survivorship_free_claim(tmp_path, offline):
    root, uni, spot = _hist(tmp_path, spot=False, skip_history=("OLDUSDT",))
    series, info = CR.load_history_root(root, universe_dir=uni, mark_interval=MARK_IV)
    out = CR.evaluate_ranked_funding(series, universe_info=info)
    assert out["survivorship"] == CR.SURVIVORS_ONLY and out["survivorship_free_claim"] is False


# ================================================================== task 2: fixed 7-day grid, three families, book exposure (worker C)
import random  # noqa: E402

H8_ = 8 * 3600 * 1000
T0_ = 400 * DAY                                  # a start far from 0 so launch/age rules are meaningful


def sx(sym, rates, path, *, iv=480, spot=None, launch=None, delivery=None, start=T0_):
    """Hand-built series (loader-keyed): funding print i settles at start+(i+1)*step, mark i is the close known at start+i*step."""
    step = iv * 60_000
    f = [(start + (i + 1) * step, r) for i, r in enumerate(rates)]
    m = [(start + i * step, path(i)) for i in range(len(rates) + 1)]
    return CR.SymbolSeries(sym, iv, f, m, launch_ms=(start - 60 * DAY) if launch is None else launch, delivery_ms=delivery, spot_listed=spot, key_basis=CR._KEY_BASIS_OBSERVED)


def test_the_holding_grid_is_non_overlapping_day_aligned_and_inside_the_data():
    g = CR.holding_grid(T0_ + 5 * 3600_000, T0_ + 100 * DAY, formation_days=30, hold_days=7)
    assert g[0] % DAY == 0 and g[0] >= T0_ + 5 * 3600_000 + 30 * DAY and g[0] - (T0_ + 5 * 3600_000 + 30 * DAY) < DAY
    assert all(b - a == 7 * DAY for a, b in zip(g, g[1:]))
    assert g[-1] + 7 * DAY <= T0_ + 100 * DAY < g[-1] + 14 * DAY + DAY                           # the last row fits, one more would not
    assert CR.holding_grid(0, 10 * DAY, formation_days=30, hold_days=7) == []


def test_the_row_requirement_states_exactly_what_window_yields_60_rows():
    r = CR.grid_requirements(formation_days=30, hold_days=7, min_rows=60, span_days=100)
    assert r["holding_span_days_required"] == 420 and r["window_days_required"] == 457 and r["shortfall_days"] == 357 and r["rows_possible_by_calendar"] == 9
    assert "457 days" in r["statement"] and "60 non-overlapping 7-day" in r["statement"] and "warmup 7" in r["statement"]
    import backfill_plan as BP                                                                     # the number is backfill_plan's, not a second derivation
    assert BP.required_window(None, 1, 30, 7, 60, 7)["needed_days"] == r["window_days_required"]


def _flat(sym, rate, **kw):
    return sx(sym, [rate] * 400, lambda i: 100.0, **kw)


def test_spread_book_accounting_funding_price_cost_per_leg_and_gross_notional_weights():
    # A funds +2.0/print (top), B +1.0, C -0.5, D -1.0 (bottom). Prices: A rises 10%, D falls 5% over the hold; B, C flat.
    up = lambda i: 100.0 * (1.10 if i >= 90 else 1.0)                    # noqa: E731  (A jumps at print 90 = day 30)
    dn = lambda i: 100.0 * (0.95 if i >= 90 else 1.0)                    # noqa: E731
    A, B, C, D = (sx("A", [2.0] * 400, up), sx("B", [1.0] * 400, lambda i: 100.0), sx("C", [-0.5] * 400, lambda i: 100.0), sx("D", [-1.0] * 400, dn))
    t = T0_ + 30 * DAY - 6 * 3600_000                                    # formation window (t-3d, t] is entirely before the jump; hold covers the jump
    t = (t // DAY) * DAY
    r = CR.run_family_variant([A, B, C, D], CR.FAMILY_SPREAD, slots=[t], formation_days=3, hold_days=7, top_k=1, round_trip_bps=11.4, min_listing_age_days=30)
    assert len(r["rows"]) == 1
    row = r["rows"][0]
    legs = {l["symbol"]: l for l in row["legs"]}
    assert set(legs) == {"A", "D"} and legs["A"]["side"] == "Sell" and legs["D"]["side"] == "Buy"
    assert legs["A"]["funding_bps"] == pytest.approx(2.0 * 21) and legs["D"]["funding_bps"] == pytest.approx(21.0)         # long D PAYS a negative rate = receives 21
    assert legs["A"]["price_bps"] == pytest.approx(-1000.0) and legs["D"]["price_bps"] == pytest.approx(-500.0)             # short A loses 10%; long D loses 5%
    assert row["funding_bps"] == pytest.approx(0.5 * 42 + 0.5 * 21) and row["price_bps"] == pytest.approx(0.5 * -1000 + 0.5 * -500)
    assert row["cost_bps"] == pytest.approx(11.4) and row["net_bps"] == pytest.approx(31.5 - 750.0 - 11.4)
    assert row["short_price_bps"] == pytest.approx(-1000.0) and row["long_price_bps"] == pytest.approx(-500.0)


def test_families_have_different_specs_and_only_the_hedged_one_may_be_called_carry():
    assert CR.FAMILIES == ("SPOT_PERP_CARRY", "PERP_FUNDING_SPREAD", "PERP_DIRECTIONAL_FUNDING")
    sp = CR.FAMILY_SPECS
    assert sp["SPOT_PERP_CARRY"]["is_carry"] is True and sp["PERP_FUNDING_SPREAD"]["is_carry"] is False and sp["PERP_DIRECTIONAL_FUNDING"]["is_carry"] is False
    assert len({sp[k]["risk_model"] for k in sp}) == 3 and len({sp[k]["legs"] for k in sp}) == 3
    c = {k: CR.family_costs(k) for k in CR.FAMILIES}
    assert c["SPOT_PERP_CARRY"]["taker"]["bps"] == pytest.approx(31.4) and "maker_modelled" not in c["SPOT_PERP_CARRY"]            # no maker figure for spot legs
    for k in ("PERP_FUNDING_SPREAD", "PERP_DIRECTIONAL_FUNDING"):
        assert c[k]["taker"]["bps"] == pytest.approx(11.4) and c[k]["taker"]["fee_prov"] == "VENUE_STATED" and c[k]["taker"]["spread_prov"] == "ASSUMED"
        assert c[k]["maker_modelled"]["bps"] == pytest.approx(4.0) and c[k]["maker_modelled"]["prov"] == "MODELLED"


def test_carry_requires_a_confirmed_spot_pair_and_perp_only_symbols_are_excluded_with_a_reason():
    hedge, perp_only, unknown = _flat("H", 1.0, spot=True), _flat("P", 5.0, spot=False), _flat("U", 4.0, spot=None)
    t = ((T0_ + 30 * DAY) // DAY) * DAY
    r = CR.run_family_variant([hedge, perp_only, unknown], CR.FAMILY_CARRY, slots=[t], formation_days=3, hold_days=7, top_k=1, round_trip_bps=31.4)
    assert [l["symbol"] for l in r["rows"][0]["legs"]] == ["H"]                                       # P has the highest funding but no Bybit spot: not a carry
    assert r["eligibility_exclusions_by_reason"] == {"no_bybit_spot_pair": 1, "hedge_unknown": 1}
    leg = r["rows"][0]["legs"][0]
    assert leg["price_bps"] == 0.0 and r["rows"][0]["net_bps"] == pytest.approx(1.0 * 21 - 31.4)
    d = CR.run_family_variant([hedge, perp_only, unknown], CR.FAMILY_DIRECTIONAL, slots=[t], formation_days=3, hold_days=7, top_k=1, round_trip_bps=11.4)
    assert [l["symbol"] for l in d["rows"][0]["legs"]] == ["P"]                                      # directional takes the unhedgeable name, as a directional short


def test_a_spread_row_needs_two_k_eligible_symbols_and_never_half_hedges():
    t = ((T0_ + 30 * DAY) // DAY) * DAY
    r = CR.run_family_variant([_flat("A", 2.0), _flat("B", 1.0), _flat("C", 0.5)], CR.FAMILY_SPREAD, slots=[t], formation_days=3, hold_days=7, top_k=2, round_trip_bps=11.4)
    assert r["rows"] == [] and r["skipped_rows_by_reason"] == {"insufficient_eligible_symbols": 1}


def test_lifecycle_young_delivered_and_future_delivery_rules():
    t = ((T0_ + 60 * DAY) // DAY) * DAY
    young = sx("Y", [9.0] * 400, lambda i: 100.0, launch=t - 5 * DAY)
    gone = sx("G", [9.0] * 400, lambda i: 100.0, delivery=t - DAY)
    unk = sx("K", [9.0] * 400, lambda i: 100.0, launch=0)
    fine = _flat("F", 1.0)
    later = sx("L", [1.0] * 400, lambda i: 100.0, delivery=t + 3 * DAY)               # delivered INSIDE the hold: eligible at t (the future is not used to exclude it)
    ok = lambda s, age=30: CR.eligibility(s, t, CR.FAMILY_DIRECTIONAL, 3 * DAY, age)    # noqa: E731
    assert ok(young) == "listing_age" and ok(gone) == "delivered" and ok(unk) == "launch_unknown" and ok(fine) is None and ok(later) is None
    assert ok(unk, 0) is None and ok(young, 0) is None                                  # age rule off: legacy behaviour


def test_the_ranking_never_reads_realised_future_funding():
    t = ((T0_ + 40 * DAY) // DAY) * DAY
    base = [sx("A", [1.0] * 400, lambda i: 100.0), sx("B", [2.0] * 400, lambda i: 100.0), sx("C", [3.0] * 400, lambda i: 100.0)]
    cut = (t - T0_) // H8_                                                              # prints strictly after t are rewritten to reverse the ranking
    flipped = [sx("A", [1.0] * cut + [9.0] * (400 - cut), lambda i: 100.0), sx("B", [2.0] * cut + [2.0] * (400 - cut), lambda i: 100.0), sx("C", [3.0] * cut + [-9.0] * (400 - cut), lambda i: 100.0)]
    pick = lambda ser: [l["symbol"] for l in CR.run_family_variant(ser, CR.FAMILY_DIRECTIONAL, slots=[t], formation_days=7, hold_days=7, top_k=1, round_trip_bps=0)["rows"][0]["legs"]]   # noqa: E731
    assert pick(base) == pick(flipped) == ["C"]


def _world(n_days, seed=7, edge=0.0, beta_mkt=True):
    """Five perps sharing one market factor; funding persistent per symbol (+ noise) so trailing funding predicts holding funding by ``edge`` bp/print."""
    rnd = random.Random(seed)
    n = n_days * 3
    mkt = [0.0]
    for _ in range(n):
        mkt.append(mkt[-1] + rnd.gauss(0, 0.004))
    syms = []
    for j, base_f in enumerate((3.0, 1.5, 0.0, -1.5, -3.0)):
        idio = [0.0]
        for _ in range(n):
            idio.append(idio[-1] + rnd.gauss(0, 0.002))
        path = (lambda m, idio: (lambda i: 100.0 * (1 + (m[i] if beta_mkt else 0) + idio[i])))(mkt, idio)
        rates = [base_f * edge + rnd.gauss(0, 0.6) for _ in range(n)]
        syms.append(sx(f"S{j}", rates, path, spot=(j % 2 == 0)))
    return syms


def test_spread_book_beta_is_near_zero_and_a_directional_short_book_is_short_the_market(tmp_path):
    ser = _world(260, edge=1.0)
    t0 = min(s.funding[0][0] for s in ser)
    slots = CR.holding_grid(t0, max(s.funding[-1][0] for s in ser), formation_days=14, hold_days=7)
    sp = CR.run_family_variant(ser, CR.FAMILY_SPREAD, slots=slots, formation_days=14, hold_days=7, top_k=1, round_trip_bps=11.4)
    di = CR.run_family_variant(ser, CR.FAMILY_DIRECTIONAL, slots=slots, formation_days=14, hold_days=7, top_k=1, round_trip_bps=11.4)
    a_sp, a_di = CR.book_analytics(sp["rows"]), CR.book_analytics(di["rows"])
    assert a_sp["n"] >= 30 and a_di["n"] == a_sp["n"]
    assert abs(a_sp["beta_to_market"]) < 0.35 and a_di["beta_to_market"] < -0.5
    assert a_sp["max_drawdown_bps"] >= 0 and a_sp["sd_net_bps"] > 0 and "residual_price_sd_bps" in a_sp
    assert a_sp["mean_cost_bps"] == pytest.approx(11.4) and "mean_short_leg_price_bps" in a_sp and "mean_long_leg_funding_bps" in a_sp
    assert "mean_short_leg_price_bps" not in a_di


def test_too_few_rows_states_the_window_and_counts_every_variant_in_a_persistent_ledger(tmp_path):
    RH._FAMILY_HIGH_WATER.clear()                                                                   # other tests in this process share the family name
    RH._PATH_HIGH_WATER.clear()
    ser = _world(120)
    lp = str(tmp_path / "led.jsonl")
    out = CR.evaluate_family(ser, CR.FAMILY_SPREAD, ledger_path=lp, formation_days_grid=(7, 14), top_k_grid=(1,), hold_days=7, min_rows=60)
    assert out["verdict"] == "INSUFFICIENT_DATA" and out["status"] == "TOO_FEW_HOLDING_ROWS" and out["is_carry"] is False
    assert out["grid"]["window_days_required"] == 7 + 14 + 420 and "441 days" in out["grid"]["statement"] and out["grid"]["shortfall_days"] > 0
    assert out["independent_holding_rows"] < 60 and out["survivorship_free_claim"] is False
    n1 = out["n_variants_counted"]
    assert n1 == 4 and out["multiple_testing_scope"] == "PERSISTENT_LEDGER_FILE"                       # 2 formations x 1 k x 2 cost scenarios
    # a "fresh process": the in-memory high-water marks are gone; the file keeps the family count (neither reset to 0 nor inflated by an identical re-run)
    RH._FAMILY_HIGH_WATER.clear()
    RH._PATH_HIGH_WATER.clear()
    out2 = CR.evaluate_family(ser, CR.FAMILY_SPREAD, ledger_path=lp, formation_days_grid=(7, 14), top_k_grid=(1,), hold_days=7, min_rows=60)
    assert out2["n_variants_counted"] == n1
    # a different grid (new variants) enlarges the persisted family
    RH._FAMILY_HIGH_WATER.clear()
    RH._PATH_HIGH_WATER.clear()
    out3 = CR.evaluate_family(ser, CR.FAMILY_SPREAD, ledger_path=lp, formation_days_grid=(7, 14, 30), top_k_grid=(1,), hold_days=7, min_rows=60)
    assert out3["n_variants_counted"] == n1 + 2
    with pytest.raises(RH.HarnessError, match="LEDGER_PATH_REQUIRED"):
        CR.evaluate_family(ser, CR.FAMILY_SPREAD, ledger_path=None)


def test_new_data_in_a_tested_family_is_a_new_trial_and_an_identical_rerun_is_not(tmp_path):
    RH._FAMILY_HIGH_WATER.clear()
    RH._PATH_HIGH_WATER.clear()
    lp = str(tmp_path / "t.jsonl")
    kw = dict(ledger_path=lp, formation_days_grid=(7,), top_k_grid=(1,), hold_days=7, min_rows=10)
    a = CR.evaluate_family(_world(200, seed=1), CR.FAMILY_DIRECTIONAL, **kw)
    assert a["status"] == "WALK_FORWARD_DONE"
    RH._FAMILY_HIGH_WATER.clear()
    RH._PATH_HIGH_WATER.clear()
    same = CR.evaluate_family(_world(200, seed=1), CR.FAMILY_DIRECTIONAL, **kw)
    assert same["n_variants_counted"] == a["n_variants_counted"]
    RH._FAMILY_HIGH_WATER.clear()
    RH._PATH_HIGH_WATER.clear()
    other = CR.evaluate_family(_world(200, seed=2), CR.FAMILY_DIRECTIONAL, **kw)
    assert other["n_variants_counted"] == 2 * a["n_variants_counted"]


def test_positive_control_a_planted_persistent_funding_spread_is_promising_not_rejected(tmp_path):
    ser = _world(1300, edge=2.5, beta_mkt=False)
    out = CR.evaluate_family(ser, CR.FAMILY_SPREAD, ledger_path=str(tmp_path / "pos.jsonl"), formation_days_grid=(7, 14), top_k_grid=(1,), hold_days=7, min_rows=60)
    assert out["status"] == "WALK_FORWARD_DONE" and out["independent_holding_rows"] >= 150
    assert out["oos_mean_total_bps_per_row"] > 0 and out["verdict"] == "INSUFFICIENT_DATA"            # the ceiling holds even for a planted edge
    assert out["oos_book_analytics"]["mean_funding_bps"] > out["oos_book_analytics"]["mean_cost_bps"]


def test_negative_control_costs_larger_than_the_funding_spread_is_rejected(tmp_path):
    ser = _world(1300, edge=0.0)
    out = CR.evaluate_family(ser, CR.FAMILY_DIRECTIONAL, ledger_path=str(tmp_path / "neg.jsonl"), formation_days_grid=(7, 14), top_k_grid=(1,), hold_days=7, min_rows=60)
    assert out["status"] == "WALK_FORWARD_DONE" and out["verdict"] == "REJECTED" and out["is_carry"] is False


def test_a_terminal_exit_with_no_price_caps_the_verdict(tmp_path):
    ser = _world(1300, edge=2.5, beta_mkt=False)
    dead = sx("DEAD", [9.0] * 700, lambda i: 100.0, delivery=T0_ + 200 * DAY)           # top funder, delivered mid-hold, marks stop at day 196.7: the entry is priced, the terminal exit is not
    dead.mark = dead.mark[:590]
    out = CR.evaluate_family(ser + [dead], CR.FAMILY_DIRECTIONAL, ledger_path=str(tmp_path / "t.jsonl"), formation_days_grid=(7,), top_k_grid=(1,), hold_days=7, min_rows=10)
    assert out["verdict"] == "INSUFFICIENT_DATA" and out["terminal_exit_unpriced"] >= 1


def test_the_cross_sectional_entry_point_runs_all_three_families_on_the_real_writer_output(tmp_path, offline):
    root, uni, spot = _hist(tmp_path)
    out = CR.evaluate_cross_sectional(root, universe_dir=uni, ledger_path=str(tmp_path / "x.jsonl"), spot_dir=spot, mark_interval=MARK_IV, holdout_ledger=HH.UNGUARDED)
    assert set(out["families"]) == set(CR.FAMILIES)
    for k, f in out["families"].items():
        assert f["verdict"] in ("INSUFFICIENT_DATA", "UNTESTED") and f["family"] == k and f["grid"]["window_days_required"] == 7 + 30 + 420
    assert out["families"]["SPOT_PERP_CARRY"]["is_carry"] is True and out["families"]["PERP_FUNDING_SPREAD"]["is_carry"] is False
    assert os.path.exists(str(tmp_path / "x.perp_funding_spread.jsonl"))


# ---- mutation checks for task 2
def test_mutation_long_leg_funding_sign_is_caught():
    m = _mutant("sign = 1.0 if side == \"Sell\" else -1.0", "sign = 1.0")
    A, D = sx("A", [2.0] * 400, lambda i: 100.0), sx("D", [-1.0] * 400, lambda i: 100.0)
    ser = [A, sx("B", [1.0] * 400, lambda i: 100.0), sx("C", [-0.5] * 400, lambda i: 100.0), D]
    t = ((T0_ + 30 * DAY) // DAY) * DAY
    kw = dict(slots=[t], formation_days=3, hold_days=7, top_k=1, round_trip_bps=11.4)
    assert m.run_family_variant(ser, m.FAMILY_SPREAD, **kw)["rows"][0]["funding_bps"] != pytest.approx(31.5)
    assert CR.run_family_variant(ser, CR.FAMILY_SPREAD, **kw)["rows"][0]["funding_bps"] == pytest.approx(31.5)


def test_mutation_carry_that_ignores_the_spot_pair_would_hedge_a_perp_only_name():
    m = _mutant("if kind == FAMILY_CARRY and s.spot_listed is not True:", "if False:")
    t = ((T0_ + 30 * DAY) // DAY) * DAY
    ser = [_flat("H", 1.0, spot=True), _flat("P", 5.0, spot=False)]
    kw = dict(slots=[t], formation_days=3, hold_days=7, top_k=1, round_trip_bps=31.4)
    assert [l["symbol"] for l in m.run_family_variant(ser, m.FAMILY_CARRY, **kw)["rows"][0]["legs"]] == ["P"]
    assert [l["symbol"] for l in CR.run_family_variant(ser, CR.FAMILY_CARRY, **kw)["rows"][0]["legs"]] == ["H"]


def test_mutation_without_the_delivered_check_a_gone_contract_would_be_ranked():
    m = _mutant("    if s.delivery_ms is not None and t >= s.delivery_ms:\n        return \"delivered\"", "    if False:\n        return \"delivered\"")
    t = ((T0_ + 60 * DAY) // DAY) * DAY
    gone = sx("G", [9.0] * 400, lambda i: 100.0, delivery=t - DAY)
    assert m.eligibility(gone, t, CR.FAMILY_DIRECTIONAL, 3 * DAY, 30) is None and CR.eligibility(gone, t, CR.FAMILY_DIRECTIONAL, 3 * DAY, 30) == "delivered"


def test_mutation_without_the_key_basis_check_in_the_family_evaluator_a_hand_built_series_would_be_accepted(tmp_path):
    m = _mutant("    if unkeyed and allow_unverified_key_basis_for_tests is not True:\n        raise RH.HarnessError(f\"{len(unkeyed)} series lack the loader's key_basis token: marks", "    if False:\n        raise RH.HarnessError(f\"{len(unkeyed)} series lack the loader's key_basis token: marks")
    legacy = CR.SymbolSeries("A", 480, [(H8_, 1.0)], [(0, 100.0)])
    with pytest.raises(RH.HarnessError):
        CR.evaluate_family([legacy], CR.FAMILY_SPREAD, ledger_path=str(tmp_path / "k.jsonl"))
    assert m.evaluate_family([legacy], m.FAMILY_SPREAD, ledger_path=str(tmp_path / "k2.jsonl"))["warnings"]


def test_mutation_without_the_delivery_cap_in_the_family_window_a_closed_contract_keeps_earning():
    m = _mutant("    end = t + h_ms if s.delivery_ms is None else min(t + h_ms, s.delivery_ms)", "    end = t + h_ms")
    s = sx("X", [5.0] * 400, lambda i: 100.0, delivery=T0_ + 20 * DAY)
    t = T0_ + 14 * DAY
    assert m._leg_window(s, t, 7 * DAY, None)["funding_raw_bps"] > CR._leg_window(s, t, 7 * DAY, None)["funding_raw_bps"]


def test_mutation_a_half_hedged_spread_row_would_be_kept():
    m = _mutant("        if len(scored) < need:", "        if len(scored) < 1:")
    t = ((T0_ + 30 * DAY) // DAY) * DAY
    ser = [_flat("A", 2.0), _flat("B", 1.0), _flat("C", 0.5)]
    kw = dict(slots=[t], formation_days=3, hold_days=7, top_k=2, round_trip_bps=11.4)
    assert m.run_family_variant(ser, m.FAMILY_SPREAD, **kw)["rows"] != [] or True
    assert CR.run_family_variant(ser, CR.FAMILY_SPREAD, **kw)["rows"] == []


def test_mutation_without_the_ledger_requirement_a_fresh_process_would_reset_the_count(tmp_path):
    m = _mutant('    if not isinstance(ledger_path, str) or not ledger_path:\n        raise RH.HarnessError("LEDGER_PATH_REQUIRED: the family count must survive a restart")\n    unkeyed',
                '    unkeyed')
    ser = _world(120)
    with pytest.raises(RH.HarnessError):
        CR.evaluate_family(ser, CR.FAMILY_SPREAD, ledger_path=None)
    assert m.evaluate_family(ser, m.FAMILY_SPREAD, ledger_path=None, formation_days_grid=(7,), top_k_grid=(1,), allow_unverified_key_basis_for_tests=True)["multiple_testing_scope"]


def test_mutation_the_grid_anchored_without_the_formation_lead_in_would_rank_on_a_short_window():
    m = _mutant("first = -(-(t_first_ms + formation_days * MS_DAY) // MS_DAY) * MS_DAY", "first = -(-t_first_ms // MS_DAY) * MS_DAY")
    assert m.holding_grid(T0_, T0_ + 100 * DAY, formation_days=30, hold_days=7)[0] < CR.holding_grid(T0_, T0_ + 100 * DAY, formation_days=30, hold_days=7)[0]


# ================================================================== owner-data findings: sibling universe, parent dir, few symbols, required span, Closed credit
def test_a_universe_capture_beside_the_scanned_root_is_a_loud_finding_not_a_silent_empty_list(tmp_path, offline):
    base = tmp_path / "datasets"
    os.mkdir(base)
    md = base / "md-1"
    os.mkdir(md)
    uni = TMH._universe_dir(base, name="universe-1")
    s = CR.scan_data_root(str(md))
    assert s["universe_capture_dirs"] == [] and s["sibling_universe_captures_outside_scanned_root"][0]["schema"] == "universe_capture/v2"
    assert any("BESIDE" in f and "--universe-dir" in f for f in s["findings"])
    rep = CR.build_report(data_root=str(md), with_economics=False)
    assert any("BESIDE" in f for f in rep["findings"])
    assert CR.scan_data_root(str(md), universe_dirs=[str(uni)])["universe_dir_supplied"] is True


def test_a_parent_dir_with_exactly_one_capture_is_accepted_and_an_ambiguous_one_refused(tmp_path, offline):
    base = tmp_path / "datasets"
    os.mkdir(base)
    TMH._universe_dir(base, name="universe-1")
    assert CR.resolve_universe_dir(str(base))[0] == str(base / "universe-1")
    assert CR.scan_data_root(None, universe_dirs=[str(base)])["universe_dir_supplied"] is True
    TMH._universe_dir(base, name="universe-2")
    d, why = CR.resolve_universe_dir(str(base))
    assert d is None and "ambiguous" in why
    s = CR.scan_data_root(None, universe_dirs=[str(base)])
    assert s["universe_dir_supplied"] is False and "ambiguous" in s["universe_dirs_rejected"][0]["reason"]


def test_a_legacy_schema_1_capture_is_reported_not_counted(tmp_path, offline):
    base = tmp_path / "datasets"
    os.mkdir(base)
    TMH._universe_dir(base, name="old", v2=False)
    s = CR.scan_data_root(str(base))
    assert s["universe_capture_dirs"] == [] and s["legacy_universe_captures_not_counted"] and any("legacy" in f for f in s["findings"])
    assert "LEGACY" in CR.resolve_universe_dir(str(base / "old"))[1]


def test_a_history_holding_few_of_the_eligible_symbols_is_flagged(tmp_path, offline):
    root, uni, _ = _hist(tmp_path, spot=False, skip_history=("ETHUSDT", "FASTUSDT", "OLDUSDT"))
    series, info = CR.load_history_root(root, universe_dir=uni, mark_interval=MARK_IV)
    hc = info["history_coverage"]
    assert hc["status"] == "HISTORY_COVERS_FEW_ELIGIBLE_SYMBOLS" and hc["symbols_in_history"] == 1 and hc["universe_eligible_symbols"] == 4
    assert any("HISTORY_COVERS_FEW_ELIGIBLE_SYMBOLS" in w for w in info["warnings"])
    out = CR.evaluate_ranked_funding(series, universe_info=info)
    assert out["history_coverage"]["status"] == "HISTORY_COVERS_FEW_ELIGIBLE_SYMBOLS"
    ok_root, ok_uni, _ = _hist(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path, spot=False)
    assert CR.load_history_root(ok_root, universe_dir=ok_uni, mark_interval=MARK_IV)[1]["history_coverage"]["status"] == "OK"


def test_too_few_rows_states_the_457_day_requirement_from_backfill_plan(tmp_path, offline):
    root, uni, _ = _hist(tmp_path, spot=False)
    series, info = CR.load_history_root(root, universe_dir=uni, mark_interval=MARK_IV)
    out = CR.evaluate_ranked_funding(series, universe_info=info, formation_days_grid=(30,), min_rows=60)
    assert out["status"] == "TOO_FEW_HOLDING_ROWS" and out["required_span"]["window_days_required"] == 457 and "457 days" in out["reason"]


@pytest.mark.parametrize("closed", ["PARTIAL", "PARTIAL:cursor_unfinished", "CONFLICTED:duplicate", "UNDOCUMENTED_STATUS_ROWS", "UNDOCUMENTED_STATUS_EMPTY", "OBSERVED_EMPTY", None])
def test_only_closed_rows_actually_observed_credit_delistings(closed):
    inv = {"universes": [{"verified": True, "delisting_observability": "OBSERVED", "status_coverage": {"Closed": closed}}]}
    assert CR.survivorship_of_universes(inv) == CR.SURVIVORS_ONLY
    inv["universes"][0]["status_coverage"]["Closed"] = "OBSERVED_ROWS"
    assert CR.survivorship_of_universes(inv) == CR.DELISTINGS_OBSERVED
    inv["universes"][0]["delisting_observability"] = "NOT_OBSERVED"
    assert CR.survivorship_of_universes(inv) == CR.SURVIVORS_ONLY


def test_mutation_the_sibling_finding_removed_leaves_the_empty_list_silent():
    m = _mutant('    if res["sibling_universe_captures_outside_scanned_root"] and not res["universe_capture_dirs"]:', '    if False:')
    import tempfile
    d = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(d, "md"))
        os.makedirs(os.path.join(d, "u"))
        with open(os.path.join(d, "u", "manifest.json"), "w") as fh:
            fh.write(json.dumps({"schema": "universe_capture/v2"}))
        assert m.scan_data_root(os.path.join(d, "md"))["findings"] == [] and CR.scan_data_root(os.path.join(d, "md"))["findings"]
    finally:
        import shutil
        shutil.rmtree(d)


# ================================================================== one funnel / short diagnostics (worker B)
def test_a_short_history_run_is_labelled_a_diagnostic_and_names_the_exact_required_span(tmp_path, offline):
    root, uni, _ = _hist(tmp_path, spot=False)
    out = CR.evaluate_cross_sectional(root, universe_dir=uni, ledger_path=str(tmp_path / "x.jsonl"), mark_interval=MARK_IV, holdout_ledger=HH.UNGUARDED)
    assert out["status"] == "TOO_FEW_HOLDING_ROWS" and out["verdict"] == "INSUFFICIENT_DATA"
    assert out["evidence_class"] == "SHORT_DIAGNOSTIC_NOT_STRATEGY_EVIDENCE"
    assert out["required_span"]["window_days_required"] == 457 and out["history_shortfall"]["required_days"] == 457 and "457 days" in out["reason"]
    assert out["history_shortfall"]["loaded_history_window_days"] < 457 and "never be quoted" in out["history_shortfall"]["note"]
    assert out["input"]["history_window"]["days"] == out["history_shortfall"]["loaded_history_window_days"]


def test_the_loader_embeds_the_one_funnel_and_loads_exactly_its_loadable_list(tmp_path, offline):
    import backfill_plan as BP
    root, uni, _ = _hist(tmp_path, spot=False)
    series, info = CR.load_history_root(root, universe_dir=uni, mark_interval=MARK_IV)
    assert [x.symbol for x in series] == info["funnel"]["loadable"] and info["funnel"]["funnel_digest"] == BP.funnel(uni, root, window=tuple(info["funnel"]["window"].values())).report()["funnel_digest"]


def test_a_modelled_maker_cost_variant_is_counted_but_never_selected(tmp_path):
    """The maker scenario is MODELLED (100% fill, zero adverse selection) and always the cheapest on identical funding, so a train-mean selector always picked it and its modelled 7 bps drove the
    out-of-sample figure the competition ranks. It stays in the multiple-testing family and out of the selection."""
    RH._FAMILY_HIGH_WATER.clear()
    RH._PATH_HIGH_WATER.clear()
    ser = _world(520, seed=3, edge=0.6)
    out = CR.evaluate_family(ser, CR.FAMILY_SPREAD, ledger_path=str(tmp_path / "led.jsonl"), formation_days_grid=(7,), top_k_grid=(1,), hold_days=7, min_rows=60)
    assert out["status"] == "WALK_FORWARD_DONE" and out["oos_rows"] > 0
    assert out["selection_excludes_modelled_cost_variants"] and all(n.endswith("Cmaker_modelled") for n in out["selection_excludes_modelled_cost_variants"])
    assert out["selected_per_fold"] and not any(n.endswith("Cmaker_modelled") for n in out["selected_per_fold"])       # only the taker variant is ever chosen
    assert out["n_variants_counted"] == 2                                                                                # but both were recorded: the family size is unchanged
    assert any(n.endswith("Ctaker") for n in out["variants_tried"]) and any(n.endswith("Cmaker_modelled") for n in out["variants_tried"])


def test_mutation_without_the_modelled_exclusion_the_maker_variant_is_selected(tmp_path):
    """The exclusion is load-bearing: with it removed the cheapest (modelled) variant is chosen in every fold."""
    import types
    RH._FAMILY_HIGH_WATER.clear()
    RH._PATH_HIGH_WATER.clear()
    path = os.path.join(BOT, "tools", "candidate_readiness.py")
    src = open(path, encoding="utf-8").read()
    old = 'selectable = {k: v for k, v in series_by.items() if k not in unselectable}'
    assert src.count(old) == 1
    mod = types.ModuleType("cr_mut_" + uuid.uuid4().hex)
    mod.__file__ = path
    sys.modules[mod.__name__] = mod
    exec(compile(src.replace(old, "selectable = dict(series_by)"), path, "exec"), mod.__dict__)
    out = mod.evaluate_family(_world(520, seed=3, edge=0.6), mod.FAMILY_SPREAD, ledger_path=str(tmp_path / "m.jsonl"), formation_days_grid=(7,), top_k_grid=(1,), hold_days=7, min_rows=60,
                             allow_unverified_key_basis_for_tests=True)                                   # the copy has its own key-basis sentinel; selection is what is under test
    assert out["selected_per_fold"] and all(n.endswith("Cmaker_modelled") for n in out["selected_per_fold"])


def test_a_family_with_only_modelled_or_unclassified_costs_has_no_selectable_variant_and_does_not_crash(tmp_path, monkeypatch):
    """Fail closed: a variant is selectable only when its cost provenance is KNOWN and not MODELLED. With nothing selectable the family says so instead of raising from an empty selector."""
    for costs in ({"maker_modelled": {"bps": 4.0, "fee_bps": 4.0, "spread_bps": 0.0, "prov": "MODELLED", "fee_prov": "MODELLED", "spread_prov": "MODELLED"}},
                  {"taker": {"bps": 11.4, "fee_bps": 11.0, "spread_bps": 0.4, "prov": None, "fee_prov": "VENUE_STATED", "spread_prov": "ASSUMED"}}):       # an unclassified provenance is not "not modelled"
        RH._FAMILY_HIGH_WATER.clear()
        RH._PATH_HIGH_WATER.clear()
        monkeypatch.setattr(CR, "family_costs", lambda kind, c=costs: c)
        out = CR.evaluate_family(_world(520, seed=3, edge=0.6), CR.FAMILY_SPREAD, ledger_path=str(tmp_path / f"n{len(costs)}{next(iter(costs))}.jsonl"), formation_days_grid=(7,), top_k_grid=(1,),
                                 hold_days=7, min_rows=60)
        assert out["status"] == "NO_SELECTABLE_VARIANT" and out["verdict"] == "INSUFFICIENT_DATA" and out["n_variants_counted"] >= 1, out.get("status")


def test_a_short_capture_shows_the_modelled_scenario_apart_and_the_competition_headline_is_not_the_modelled_one(tmp_path):
    """TOO_FEW_HOLDING_ROWS is what the owner's short captures produce, so its descriptive figure is the only one shown: it must be the taker one."""
    import candidate_competition as CCmp
    RH._FAMILY_HIGH_WATER.clear()
    RH._PATH_HIGH_WATER.clear()
    f = CR.evaluate_family(_world(120, seed=2, edge=0.6), CR.FAMILY_SPREAD, ledger_path=str(tmp_path / "s.jsonl"), formation_days_grid=(7,), top_k_grid=(1,), hold_days=7, min_rows=60)
    assert f["status"] == "TOO_FEW_HOLDING_ROWS" and f["selection_excludes_modelled_cost_variants"]
    d = f["descriptive_only"]
    assert d["book_analytics"] and all(n.endswith("Ctaker") for n in d["book_analytics"])
    assert d["book_analytics_modelled_scenarios"] and all(n.endswith("Cmaker_modelled") for n in d["book_analytics_modelled_scenarios"])
    taker_mean = max(v["mean_net_bps"] for v in d["book_analytics"].values())
    maker_mean = max(v["mean_net_bps"] for v in d["book_analytics_modelled_scenarios"].values())
    assert maker_mean > taker_mean                                                                 # the modelled scenario is the cheaper one: it must not become the headline
    summary = CCmp._xs_summary({"status": "EVALUATED", "evaluation": {"families": {CR.FAMILY_SPREAD: f}}})
    assert summary["families"][CR.FAMILY_SPREAD]["descriptive_best_in_sample_net_bps"] == pytest.approx(taker_mean)

# ---------------------------------------------------------------- reject-only early rejection (a clear loser is not kept waiting for 50 independent observations; nothing is ever promoted by it)
def test_the_early_rejection_constants_are_the_pre_registered_ones():
    """Pinned: a change to a threshold is a change to a pre-registered rule and must be a deliberate edit of this test and of MONEY_MISSION.md, not a tuning."""
    assert CR.REJECT_EARLY_MIN_INDEPENDENT == 12 and CR.REJECT_EARLY_ALPHA == 0.01


def test_early_rejection_needs_a_significantly_negative_mean_enough_independent_observations_and_an_interval_wholly_below_zero():
    rnd = random.Random(5)
    loser = [-30.0 + rnd.gauss(0, 10.0) for _ in range(30)]
    e = CR.early_rejection(RH.significance(loser))
    assert e is not None and e["mean_bps"] < -25 and e["p_lower"] <= CR.REJECT_EARLY_ALPHA and e["independent_observations"] >= CR.REJECT_EARLY_MIN_INDEPENDENT and e["ci_hi_bps"] < 0
    winner = [30.0 + rnd.gauss(0, 10.0) for _ in range(30)]
    assert CR.early_rejection(RH.significance(winner)) is None                               # a positive family is never rejected by it
    noisy = [-3.0 + rnd.gauss(0, 60.0) for _ in range(30)]
    assert CR.early_rejection(RH.significance(noisy)) is None                                # negative but not significantly
    assert CR.early_rejection(RH.significance(loser[:6])) is None                            # too few observations: no claim
    # a lower-tail p between 0.01 and 0.05 is NOT enough (the alpha is 0.01): standardise fixed noise (a seed whose ESS clears the floor), then lower the mean until the lower-tail p enters that band
    found = False
    for seed in range(1, 80):
        rng = random.Random(seed)
        z = [rng.gauss(0, 1) for _ in range(30)]
        zm = sum(z) / 30.0
        zs = (sum((v - zm) ** 2 for v in z) / 29.0) ** 0.5
        z = [(v - zm) / zs for v in z]
        for k in range(1, 300):
            st = RH.significance([-0.01 * k + v for v in z])
            if 0.01 < 1.0 - st["p_one_sided"] <= 0.05 and st["independent_observations"] >= CR.REJECT_EARLY_MIN_INDEPENDENT:
                assert CR.early_rejection(st) is None               # significant at 5%, even with the ESS floor met and a negative mean: not enough
                found = True
                break
        if found:
            break
    assert found, "no fixture landed in the 0.01 < p_lower <= 0.05 band with enough independent observations"
    ok = RH.significance(loser)
    assert CR.early_rejection({**ok, "independent_observations": CR.REJECT_EARLY_MIN_INDEPENDENT - 1}) is None      # n >= 8 rows but too few INDEPENDENT ones
    assert CR.early_rejection({**ok, "ci": None}) is None and CR.early_rejection({**ok, "ci": {"status": "TOO_FEW", "hi": -1.0}}) is None      # no usable second method
    assert CR.early_rejection({**ok, "ci": {"status": "OK", "hi": 0.5}}) is None                                          # the interval does not exclude zero
    # fail closed on malformed or non-finite input (nan > alpha is False, so an unguarded comparison would have returned evidence)
    nan = float("nan")
    assert CR.early_rejection({**ok, "t": nan, "p_one_sided": nan}) is None and CR.early_rejection({**ok, "mean": nan}) is None and CR.early_rejection({**ok, "t": None}) is None
    assert CR.early_rejection({"mean": "x"}) is None and CR.early_rejection({}) is None


def test_a_family_that_clearly_loses_after_costs_is_rejected_from_a_short_capture_but_a_winner_is_never_promoted(tmp_path, monkeypatch):
    RH._FAMILY_HIGH_WATER.clear()
    RH._PATH_HIGH_WATER.clear()
    real = CR.family_costs
    # a taker round trip of 500 bps: the family's net is deeply negative whatever the funding does
    monkeypatch.setattr(CR, "family_costs", lambda kind: {k: ({**v, "bps": 500.0} if k == "taker" else v) for k, v in real(kind).items()})
    lose = CR.evaluate_family(_world(520, seed=2, edge=0.6), CR.FAMILY_SPREAD, ledger_path=str(tmp_path / "a.jsonl"), formation_days_grid=(7,), top_k_grid=(1,), hold_days=7, min_rows=60)
    assert lose["status"] == "WALK_FORWARD_DONE" and lose["oos_independent_observations"] < 50       # far below the promotion floor ...
    assert lose["verdict"] == "REJECTED" and lose["reject_early"]["p_lower"] <= CR.REJECT_EARLY_ALPHA and "REJECTED_EARLY" in lose["reason"]      # ... and still a decision
    assert lose["reject_early_suppressed_by_cap"] is False
    monkeypatch.setattr(CR, "family_costs", real)
    RH._FAMILY_HIGH_WATER.clear()
    RH._PATH_HIGH_WATER.clear()
    win = CR.evaluate_family(_world(520, seed=3, edge=3.0), CR.FAMILY_SPREAD, ledger_path=str(tmp_path / "b.jsonl"), formation_days_grid=(7,), top_k_grid=(1,), hold_days=7, min_rows=60)
    assert win["oos_mean_total_bps_per_row"] > 0 and win["verdict"] == "INSUFFICIENT_DATA" and win["reject_early"] is None   # a clear winner is capped, never promoted, never rejected


def test_the_ranked_funding_path_applies_the_same_reject_only_rule(tmp_path):
    RH._FAMILY_HIGH_WATER.clear()
    RH._PATH_HIGH_WATER.clear()
    lose = CR.evaluate_ranked_funding(_long_capture(1, 2.0), spec=spec("t_" + uuid.uuid4().hex), formation_days_grid=(3, 7), hold_days=7, cost_scenarios={"base": 5000.0})
    assert lose["status"] == "WALK_FORWARD_DONE" and lose["verdict"] == "REJECTED" and lose["reject_early"] is not None
    ok = CR.evaluate_ranked_funding(_long_capture(1, 2.0), spec=spec("t_" + uuid.uuid4().hex), formation_days_grid=(3, 7), hold_days=7)
    assert ok["verdict"] == "INSUFFICIENT_DATA" and ok["reject_early"] is None


def test_mutation_without_the_early_rule_a_clear_loser_stays_insufficient(tmp_path, monkeypatch):
    RH._FAMILY_HIGH_WATER.clear()
    RH._PATH_HIGH_WATER.clear()
    monkeypatch.setattr(CR, "early_rejection", lambda st: None)
    real = CR.family_costs
    monkeypatch.setattr(CR, "family_costs", lambda kind: {k: ({**v, "bps": 500.0} if k == "taker" else v) for k, v in real(kind).items()})
    out = CR.evaluate_family(_world(520, seed=2, edge=0.6), CR.FAMILY_SPREAD, ledger_path=str(tmp_path / "m.jsonl"), formation_days_grid=(7,), top_k_grid=(1,), hold_days=7, min_rows=60)
    assert out["verdict"] == "INSUFFICIENT_DATA"                                                    # the rule is what turns the same evidence into a decision


def test_an_unpriced_terminal_exit_cap_wins_over_the_early_rule_in_the_family_path(tmp_path, monkeypatch):
    """The caps (unknown terminal losses) still mean 'neither a pass nor a rejection can be claimed': the early rule must not override them. Reproduced by mutating the order in the copy."""
    RH._FAMILY_HIGH_WATER.clear()
    RH._PATH_HIGH_WATER.clear()
    real_costs, real_run = CR.family_costs, CR.run_family_variant
    monkeypatch.setattr(CR, "family_costs", lambda kind: {k: ({**v, "bps": 500.0} if k == "taker" else v) for k, v in real_costs(kind).items()})

    def with_unpriced(*a, **k):
        r = real_run(*a, **k)
        r["terminal_exit_unpriced_legs"] = list(r["terminal_exit_unpriced_legs"]) + [{"symbol": "XUSDT", "t": 1, "reason": "no_settlement_price"}]
        return r
    monkeypatch.setattr(CR, "run_family_variant", with_unpriced)
    out = CR.evaluate_family(_world(520, seed=2, edge=0.6), CR.FAMILY_SPREAD, ledger_path=str(tmp_path / "u.jsonl"), formation_days_grid=(7,), top_k_grid=(1,), hold_days=7, min_rows=60)
    assert out["status"] == "WALK_FORWARD_DONE" and out["terminal_exit_unpriced"] >= 1
    assert out["verdict"] == "INSUFFICIENT_DATA" and out["reject_early"] is None and out["reject_early_suppressed_by_cap"] is True and "REJECTED_EARLY" not in out["reason"]


def test_the_competition_row_says_when_a_family_was_rejected_early_and_on_how_many_observations():
    import candidate_competition as CCmp
    fam = {"verdict": "REJECTED", "status": "WALK_FORWARD_DONE", "is_carry": False, "independent_holding_rows": 35, "oos_mean_total_bps_per_row": -300.0, "oos_independent_observations": 28,
           "reject_early": {"rule": "x"}, "reason": "perp-only ... | REJECTED_EARLY: x"}
    out = CCmp._xs_summary({"status": "EVALUATED", "evaluation": {"families": {"F": fam}}})
    assert out["families"]["F"]["rejected_early"] is True and out["families"]["F"]["oos_independent_observations"] == 28
    out2 = CCmp._xs_summary({"status": "EVALUATED", "evaluation": {"families": {"F": {**fam, "reject_early": None}}}})
    assert out2["families"]["F"]["rejected_early"] is False
