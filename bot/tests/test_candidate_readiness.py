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
    series, _info = m.load_history_root(root, universe_dir=uni, mark_interval=MARK_IV)
    assert "OLDUSDT" in {x.symbol for x in series}


def test_mutation_without_the_closed_in_series_check_survivors_would_be_labelled_delistings(tmp_path, offline):
    m = _mutant("closed_missing = sorted(sy for sy, r in recs.items() if", "closed_missing = [] if True else sorted(sy for sy, r in recs.items() if")
    root, uni, _ = _hist(tmp_path, spot=False, skip_history=("OLDUSDT",))
    assert m.load_history_root(root, universe_dir=uni, mark_interval=MARK_IV)[1]["survivorship"] == "DELISTINGS_OBSERVED"
