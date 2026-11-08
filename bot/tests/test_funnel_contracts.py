"""ONE FUNNEL, IDENTICAL EVERYWHERE. A real universe capture and real market_history datasets (the real writers, a stubbed venue, no network) are read by the planner, the diagnosis,
the history loader and the freshness audit; all four must report the SAME counts, the SAME exclusion reasons and the SAME symbol lists, because they all call ``backfill_plan.funnel``."""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, HERE)

import backfill_plan as BP  # noqa: E402
import candidate_readiness as CR  # noqa: E402
import data_freshness_audit as DFA  # noqa: E402
import market_history as MH  # noqa: E402
import test_backfill_plan as TB  # noqa: E402
import universe_capture as UC  # noqa: E402

DAY, H4, H8, T, W0, P = TB.DAY, TB.H4, TB.H8, TB.T, TB.W0, TB.P
no_network = TB.no_network
root = TB.root


def _instruments():
    return [TB.inst("AAAUSDT"), TB.inst("BBBUSDT"),
            TB.inst("OLDUSDT", "Closed", delivery=T - 2 * DAY),                      # a Closed perp inside the window
            TB.inst("GONEUSDT", "Closed", delivery=W0 - 5 * DAY),                    # delisted before the window
            TB.inst("LATEUSDT", "Trading", launch=T + 3 * DAY),                      # launched after it
            TB.inst("PREUSDT", "PreLaunch", launch=T + 10 * DAY),
            TB.inst("NEVERUSDT", "Closed", launch=T - DAY, delivery=T - 2 * DAY),
            TB.inst("NODELUSDT", "Closed"),                                          # Closed with no delivery time
            TB.inst("FUTUSDT", "Trading", ctype="LinearFutures"),                    # a dated future
            TB.inst("MISSUSDT"),                                                     # in funding, absent from mark
            TB.inst("PARTUSDT"),                                                     # mark with a hole -> PARTIAL (kept by the loader, not complete)
            TB.inst("HOLEUSDT"),                                                     # funding with a hole -> an unknown interval for the prints after it: excluded
            TB.inst("IVFOURUSDT", interval=240)]                                    # a 240-minute funding contract


def _collect(rootdir, uni, ds, interval, grids, symbols, universe_for_zzz=True):
    recs, info = MH.load_universe_dir(str(uni), include_statuses=["Trading", "Closed"])
    specs = MH.build_specs(ds, interval, symbols, W0, T, recs)
    v = TB.Venue(grids)
    clk = iter(range(TB.SERVER + 100, TB.SERVER + 10 ** 7, 3))
    return MH.run_dataset(str(rootdir), ds, interval, specs, fetch=v, clock=lambda: next(clk), sleep=lambda _s: None, mono=lambda: 0.0, free_bytes_fn=lambda _p: 1 << 40,
                          reserve_bytes=1, universe=info, rate=1000.0, selection={"mode": "EXPLICIT_SYMBOLS", "plan_digest": None})


@pytest.fixture
def world(tmp_path, root):
    uni = TB.make_universe(tmp_path, _instruments())
    recs, _ = MH.load_universe_dir(str(uni), include_statuses=["Trading", "Closed"])
    f, m = {}, {}
    full = ["AAAUSDT", "BBBUSDT", "OLDUSDT", "MISSUSDT", "PARTUSDT", "HOLEUSDT", "IVFOURUSDT", "ZZZUSDT"]
    for s in full:
        lo, hi = W0, (T - 2 * DAY if s == "OLDUSDT" else T)
        step = H4 if s == "IVFOURUSDT" else H8
        f[(MH.FUNDING, s)] = TB.grid(lo, hi, step)
        if s != "MISSUSDT":
            m[(MH.MARK, s)] = TB.grid(lo, hi - H4 if hi - H4 > lo else hi, H4)
    g = f[(MH.FUNDING, "HOLEUSDT")]
    f[(MH.FUNDING, "HOLEUSDT")] = g[:3] + g[6:]                                       # a hole of two prints
    g = m[(MH.MARK, "PARTUSDT")]
    m[(MH.MARK, "PARTUSDT")] = g[:3] + g[6:]
    _collect(root, uni, MH.FUNDING, None, f, full)
    _collect(root, uni, MH.MARK, "240", m, [s for s in full if s != "MISSUSDT"])
    return uni, root, tmp_path


def _bp(uni, root, **kw):
    a = dict(study_from_ms=None, study_to_ms=T, **P)
    a.update(kw)
    return a


def _four(world):
    uni, root, tmp = world
    doc = BP.plan(str(uni), str(root), **_bp(uni, root))
    plan_file = tmp / "plan.json"
    plan_file.write_text(json.dumps(doc))
    diag = BP.diagnose(str(uni), str(root), **_bp(uni, root))
    series, info = CR.load_history_root(str(root), universe_dir=str(uni), mark_interval="240", window=(doc["window"]["effective_from_ms"], doc["window"]["effective_to_ms"]))
    aud = DFA.audit(str(root), plan_file=str(plan_file), now_ms=T + DAY)
    direct = BP.funnel(str(uni), str(root), window=(W0, T), report_datasets=(MH.FUNDING, MH.MARK))
    return {"plan": doc["funnel"], "diagnose": diag["funnel"], "loader": info["funnel"], "audit": aud["readiness_vocabulary"]["research_ready"]["funnel"], "direct": direct.report()}, series, doc, diag, info


def test_the_four_consumers_report_the_identical_funnel(world):
    reps, series, doc, diag, info = _four(world)
    ref = reps["direct"]
    for who, rep in reps.items():
        core = {k: v for k, v in rep.items() if k not in ("datasets", "window_source")}
        assert core == {k: v for k, v in ref.items() if k not in ("datasets", "window_source")}, who
        assert rep["funnel_digest"] == ref["funnel_digest"], who
    assert {x.symbol for x in series} == set(ref["loadable"])
    assert doc["eligible_symbols"] == ref["eligible"] and [x["count"] for x in diag["steps"]] == [x["count"] for x in ref["stages"]]


def test_the_funnel_says_exactly_what_this_universe_and_these_datasets_imply(world):
    reps, _s, doc, _d, info = _four(world)
    f = reps["direct"]
    assert f["eligible"] == ["AAAUSDT", "BBBUSDT", "HOLEUSDT", "IVFOURUSDT", "MISSUSDT", "OLDUSDT", "PARTUSDT"]
    assert "FUTUSDT" not in f["excluded_lifecycle"] and "FUTUSDT" not in f["symbols_in_datasets_not_loaded"]       # the real capture writer already drops dated futures (the classifier would say not_perpetual)
    assert f["excluded_lifecycle"] == {"GONEUSDT": "delivered_before_window", "LATEUSDT": "launched_after_window", "NEVERUSDT": "never_trading",
                                       "NODELUSDT": "closed_without_delivery", "PREUSDT": "prelaunch_no_history"}
    assert f["excluded_stage"]["MISSUSDT"] == "not_in_mark_dataset" and f["excluded_stage"]["HOLEUSDT"].startswith("funding_interval_unknown_for_") and len(f["excluded_stage"]) == 2
    assert f["loadable"] == ["AAAUSDT", "BBBUSDT", "IVFOURUSDT", "OLDUSDT", "PARTUSDT"]
    assert f["complete"] == ["AAAUSDT", "BBBUSDT", "IVFOURUSDT", "OLDUSDT"]                  # PARTUSDT has a mark hole: loaded (warned) but not complete
    assert f["symbols_in_datasets_not_loaded"] == {**{k: v for k, v in f["excluded_stage"].items()}, "ZZZUSDT": "not_in_universe"}
    assert [x["count"] for x in f["stages"]] == [12, 7, 7, 6, 6, 6, 5, 5, 4]
    assert f["ended_unaccounted"] == {"NODELUSDT": "closed_without_delivery"}                # the only ended contract that is neither loaded nor PROVEN out of scope
    assert info["survivorship"] == "SURVIVORS_ONLY" and info["closed_in_universe_not_in_series"]["names"] == ["NODELUSDT"]
    assert {e["symbol"]: e["reason"] for e in info["excluded"]} == f["symbols_in_datasets_not_loaded"]
    assert info["not_complete_over_window"] == ["PARTUSDT"] and doc["readiness"]["research_ready"] is False


def test_the_240_minute_symbol_keeps_its_own_interval(world):
    _r, series, *_ = _four(world)
    iv = {x.symbol: x.interval_min for x in series}
    assert iv["IVFOURUSDT"] == 240 and iv["AAAUSDT"] == 480


def test_a_different_window_changes_the_funnel_for_every_consumer_alike(world):
    uni, root, _t = world
    late = BP.funnel(str(uni), str(root), window=(T - DAY, T + 20 * DAY), report_datasets=(MH.FUNDING, MH.MARK)).report()
    assert "LATEUSDT" in late["eligible"] and "OLDUSDT" not in late["eligible"]                 # delivered at T-2d, before this window
    assert late["funnel_digest"] != BP.funnel(str(uni), str(root), window=(W0, T)).report()["funnel_digest"]
    _s, info = CR.load_history_root(str(root), universe_dir=str(uni), mark_interval="240", window=(T - DAY, T + 20 * DAY))
    assert info["funnel"]["eligible"] == late["eligible"] and {x.symbol for x in _s} == set(late["loadable"])


def test_the_loader_without_a_window_uses_the_funding_manifest_request(world):
    uni, root, _t = world
    _s, info = CR.load_history_root(str(root), universe_dir=str(uni), mark_interval="240")
    assert info["funnel"]["window"] == {"from_ms": W0, "to_ms": T} and info["funnel"]["window_source"] == BP.WINDOW_FROM_FUNDING_MANIFEST
    assert info["history_window"]["days"] == 6.0


def test_a_classifier_class_is_reported_by_every_consumer_alike(world):
    uni, root, _t = world
    odd = lambda rec, a, b, st, q: (False, "TERMINAL_UNRESOLVED_PENDINGOPEN", []) if rec.get("symbol") == "OLDUSDT" else BP.classify_record(rec, a, b, st, q)      # noqa: E731
    f = BP.funnel(str(uni), str(root), window=(W0, T), classifier=odd).report()
    assert f["excluded_lifecycle"]["OLDUSDT"] == "TERMINAL_UNRESOLVED_PENDINGOPEN" and "OLDUSDT" not in f["loadable"]
    assert "OLDUSDT" in f["ended_unaccounted"]                                                      # an unknown class never silently counts as out of scope
    _s, info = CR.load_history_root(str(root), universe_dir=str(uni), mark_interval="240", window=(W0, T), classifier=odd)
    assert info["funnel"]["funnel_digest"] == f["funnel_digest"] and "OLDUSDT" not in {x.symbol for x in _s}
    assert BP.plan(str(uni), str(root), **_bp(uni, root), classifier=odd)["funnel"]["funnel_digest"] == f["funnel_digest"]


def test_excluded_symbol_statuses_are_the_same_constant_in_both_modules():
    assert tuple(BP.EXCLUDED_SYMBOL_STATUSES) == tuple(CR.EXCLUDED_SYMBOL_STATUSES)


def _owner_universe(tmp_path):
    """The owner's shape: perpetual / dated rows whose OWN status is PendingOpen but which the venue served inside the Closed partition (written by the real capture writer)."""
    rows = [r for r in _instruments() if r["symbol"] != "NODELUSDT"] + [TB.inst("CANCELUSDT", "PendingOpen", delivery=T - 3 * DAY), TB.inst("UNRESUSDT", "PendingOpen", delivery=T - 3 * DAY),
                             TB.inst("DATEDUSDT", "PendingOpen", delivery=T - 3 * DAY, ctype="LinearFutures", quote="USD")]

    def fetch(url):
        base = TB.venues.rest_for("mainnet")
        path, _, qs = url[len(base):].partition("?")
        q = dict(x.split("=", 1) for x in qs.split("&"))
        if path == UC.PATH_TICKERS:
            lst = [TB.tick(r["symbol"]) for r in rows if r["status"] != "PendingOpen"]
        else:
            want = q.get("status")
            lst = [r for r in rows if want is None or r["status"] == want or (want == "Closed" and r["status"] == "PendingOpen")]
        return json.dumps({"retCode": 0, "retMsg": "OK", "result": {"category": q["category"], "list": lst, "nextPageCursor": ""}, "time": TB.SERVER}).encode()
    d = tmp_path / "owner_uni"
    UC.capture_universe(str(d), fetch=fetch, clock_ms=lambda: TB.SERVER, sleep=lambda _s: None, pause_s=0)
    return d


def test_the_owners_pending_open_in_closed_rows_are_classified_and_agree_everywhere(tmp_path, root):
    uni = _owner_universe(tmp_path)
    sc = UC.eligible_instruments(str(uni))
    assert sc["usable"] is True, sc["research_blockers"]
    f = BP.funnel(str(uni), None, window=(W0, T)).report()
    # without history evidence the perpetual cancelled-before-opening cannot be told from an unresolved one; the dated future is out of scope; neither is eligible
    assert f["excluded_lifecycle"]["UNRESUSDT"] == "TERMINAL_UNRESOLVED_PENDINGOPEN" and f["excluded_lifecycle"]["CANCELUSDT"] == "TERMINAL_UNRESOLVED_PENDINGOPEN"
    assert f["lifecycle_class_counts"] == sc["lifecycle"]["class_counts"] and f["lifecycle_authority"] == "universe_capture.eligible_instruments"
    assert "UNRESUSDT" not in f["eligible"] and "CANCELUSDT" not in f["eligible"] and "DATEDUSDT" not in f["eligible"]
    assert f["scope_dataset_status"] == sc["dataset_status"]
    # the planner reports the very same classes
    doc = BP.plan(str(uni), None, **_bp(uni, None))
    assert doc["funnel"]["lifecycle_class_counts"] == f["lifecycle_class_counts"] and doc["funnel"]["excluded_lifecycle"] == f["excluded_lifecycle"]


def test_verified_empty_history_distinguishes_cancelled_before_opening_from_unresolved(tmp_path, root):
    uni = _owner_universe(tmp_path)
    syms = ["AAAUSDT", "CANCELUSDT"]
    g = {(MH.FUNDING, "AAAUSDT"): TB.grid(W0, T, H8), (MH.MARK, "AAAUSDT"): TB.grid(W0, T - H4, H4)}          # CANCELUSDT: the venue serves no row at all
    _collect(root, uni, MH.FUNDING, None, g, syms)
    _collect(root, uni, MH.MARK, "240", g, syms)
    man = json.loads((root / "funding" / "manifest.json").read_text())
    f = BP.funnel(str(uni), str(root), window=(W0, T), report_datasets=(MH.FUNDING, MH.MARK)).report()
    # the real collector never reports an EMPTY symbol COMPLETE, so real empty history is NOT evidence of a cancellation: the perpetual stays unresolved
    assert man["symbols"]["CANCELUSDT"]["status"] != "COMPLETE" and not BP.history_evidence_from_datasets(BP.funnel(str(uni), str(root), window=(W0, T), report_datasets=(MH.FUNDING, MH.MARK)).datasets).get("CANCELUSDT", {}).get("coverage_complete")
    assert f["excluded_lifecycle"]["CANCELUSDT"] == "TERMINAL_UNRESOLVED_PENDINGOPEN"


def test_positive_history_evidence_turns_unresolved_into_cancelled_before_opening(tmp_path, monkeypatch):
    uni = _owner_universe(tmp_path)
    cov = lambda rows: {"coverage": {"rows": rows}, "status": "COMPLETE"}          # noqa: E731
    view = lambda syms: {"rd": {"present": True, "verified": True, "manifest": {"symbols": syms}}}          # noqa: E731
    views = {MH.FUNDING: view({"CANCELUSDT": cov(0), "UNRESUSDT": cov(0)}), MH.MARK: view({"CANCELUSDT": cov(0)})}
    ev = BP.history_evidence_from_datasets(views)
    assert ev == {"CANCELUSDT": {"funding_rows": 0, "kline_rows": 0, "coverage_complete": True}}            # UNRESUSDT has no kline-type dataset: no evidence
    views[MH.MARK]["rd"]["verified"] = False
    assert BP.history_evidence_from_datasets(views) == {}                                                   # unverified datasets are never evidence
    monkeypatch.setattr(BP, "history_evidence_from_datasets", lambda _v: {"CANCELUSDT": {"funding_rows": 0, "kline_rows": 0, "coverage_complete": True}})
    f = BP.funnel(str(uni), None, window=(W0, T)).report()
    assert f["excluded_lifecycle"]["CANCELUSDT"] == "CONFIRMED_CANCELLED_BEFORE_OPENING" and f["excluded_lifecycle"]["UNRESUSDT"] == "TERMINAL_UNRESOLVED_PENDINGOPEN"
    assert f["lifecycle_class_counts"] == UC.eligible_instruments(str(uni), history_evidence={"CANCELUSDT": {"funding_rows": 0, "kline_rows": 0, "coverage_complete": True}})["lifecycle"]["class_counts"]
