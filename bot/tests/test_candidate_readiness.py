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
                     "spot_perp_basis", "spread_microstructure", "btcusdt_funding_carry_overlay"}
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
    assert r["readiness_verdict"] == "INSUFFICIENT_DATA" and r["edge_vs_costs"]["policies"]["single_print_harvest"] == "REJECTED"
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
    return CR.SymbolSeries(sym, iv, f, m, launch_ms=start, delivery_ms=delivery, spot_listed=spot)


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


def _hist(tmp_path, *, spot=True, tamper_funding=None, mark_scripts=None, syms=("BTCUSDT", "ETHUSDT", "FASTUSDT", "OLDUSDT")):
    """A real dataset root: universe capture (BTC/ETH 8h, FAST 240m, OLD Closed with a deliveryTime), funding + mark-kline-240 written by the real writer with a scripted
    fetch / clock / sleep, and (optionally) a spot-instruments snapshot. Returns (root, universe_dir, spot_dir)."""
    delivery = TMH.BASE - 60 * TMH.H8
    root = tmp_path / "hist"
    os.mkdir(root, 0o700)
    uni = TMH._universe_dir(tmp_path, syms=syms, intervals={"FASTUSDT": 240}, bases={"FASTUSDT": "1000FAST"}, statuses={"OLDUSDT": "Closed"}, deliveries={"OLDUSDT": delivery})
    recs, info = MH.load_universe_dir(str(uni), include_statuses=["Trading", "Closed"])
    fdata = {("funding", "BTCUSDT"): TMH.grid(TMH.FIRST, TMH.N, TMH.H8), ("funding", "ETHUSDT"): TMH.grid(TMH.FIRST, TMH.N, TMH.H8),
             ("funding", "FASTUSDT"): TMH.grid(TMH.FIRST, 2 * TMH.N - 1, H4), ("funding", "OLDUSDT"): TMH.grid(TMH.FIRST, TMH.N - 60, TMH.H8)}
    fdata = {k: v for k, v in fdata.items() if k[1] in syms}
    fspecs = MH.build_specs("funding", None, sorted(recs), TMH.START, TMH.END, recs)
    fv = TMH.HV(fdata)
    TMH.run(root, fspecs, fv, universe=info)
    mdata = {("mark-kline", s): TMH.grid(TMH.FIRST, 2 * TMH.N - 1, H4) for s in syms if s != "OLDUSDT"}
    if "OLDUSDT" in syms:
        mdata[("mark-kline", "OLDUSDT")] = TMH.grid(TMH.FIRST, (delivery - TMH.FIRST) // H4 + 1, H4)
    mspecs = MH.build_specs("mark-kline", MARK_IV, sorted(recs), TMH.START, TMH.BASE + 10, recs)
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
    assert by["BTCUSDT"].funding[0] == (TMH.FIRST, pytest.approx(1.0)) and by["BTCUSDT"].mark[0] == (TMH.FIRST, pytest.approx(100.5))      # 0.0001 fraction = 1 bp; mark = close
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
    assert CR.main(["--history-root", root, "--universe-dir", uni, "--spot-dir", spot, "--mark-interval", MARK_IV]) == 0
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
    m = _mutant("if t - formation_ms < ts <= t]", "if t - formation_ms < ts <= t + formation_ms]")
    leaky = mk("L", 480, [0.1] * 60 + [500.0] * 60)
    assert m.formation_score(leaky, 18 * DAY, 5 * DAY) > 100 and CR.formation_score(leaky, 18 * DAY, 5 * DAY) == pytest.approx(0.3)


def test_mutation_without_the_delivery_cap_a_closed_contract_keeps_earning():
    m = _mutant("end = min(t + h_ms, s.delivery_ms) if s.delivery_ms is not None else t + h_ms", "end = t + h_ms")
    s = mk("Z", 480, [2.0] * 90, delivery=12 * DAY)
    leg = m.run_variant([s], formation_days=3, hold_days=7, top_k=1, round_trip_bps=0.0, t_start=10 * DAY, t_end=17 * DAY)["rows"][0]["legs"][0]
    assert leg["prints"] == 21


def test_mutation_without_interval_normalisation_per_print_rates_would_be_compared():
    m = _mutant("return statistics.fmean(vals) * 1440.0 / s.interval_min", "return statistics.fmean(vals)")
    c = mk("C", 240, [1.2] * 60)
    assert m.formation_score(c, 5 * DAY, 3 * DAY) == pytest.approx(1.2) and CR.formation_score(c, 5 * DAY, 3 * DAY) == pytest.approx(7.2)


def test_mutation_a_perp_only_symbol_labelled_hedgeable_would_be_reported_as_carry():
    m = _mutant('return {True: "HEDGEABLE_ON_BYBIT", False: "PERP_ONLY"}.get(s.spot_listed, "HEDGE_UNKNOWN")', 'return "HEDGEABLE_ON_BYBIT"')
    p = mk("P", 480, [3.0] * 120, spot=False)
    leg = m.run_variant([p], formation_days=3, hold_days=7, top_k=1, round_trip_bps=11.0, t_start=10 * DAY, t_end=30 * DAY)["rows"][0]["legs"][0]
    assert "hedged_carry_funding_net_of_cost_bps" in leg
