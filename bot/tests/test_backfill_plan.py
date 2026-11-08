"""backfill_plan: offline only. A real schema-2 universe capture and real market_history datasets are built in tmp dirs from a stubbed venue; nothing touches the network."""
from __future__ import annotations

import copy
import json
import os
import shutil
import socket
import sys
import threading

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import backfill_plan as BP  # noqa: E402
import market_history as MH  # noqa: E402
import universe_capture as UC  # noqa: E402
import venues  # noqa: E402

DAY = 86_400_000
H4, H8 = 4 * 3600_000, 8 * 3600_000
T = (1_799_000_000_000 // DAY) * DAY                  # study end, a UTC day boundary
P = dict(formation_days=2, hold_days=1, min_holding_rows=3, warmup_days=1)       # 1 + 2 + 3*1 = 6 days
NEED_DAYS = 6
W0 = T - NEED_DAYS * DAY
SERVER = T + 2 * DAY


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("network touched")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "hist"
    os.mkdir(r, 0o700)
    return r


def inst(sym, status="Trading", launch=None, delivery=0, interval=480, ctype="LinearPerpetual", quote="USDT"):
    return {"symbol": sym, "symbolId": "", "contractType": ctype, "status": status, "baseCoin": sym[:-4], "quoteCoin": quote, "settleCoin": quote,
            "launchTime": str(launch if launch is not None else W0 - 30 * DAY), "deliveryTime": str(delivery), "fundingInterval": interval, "upperFundingRate": "0.00375",
            "lowerFundingRate": "-0.00375", "priceFilter": {"minPrice": "0.1", "tickSize": "0.1"},
            "lotSizeFilter": {"minOrderQty": "0.001", "maxOrderQty": "10", "qtyStep": "0.001", "minNotionalValue": "5"}}


def tick(s):
    return {"symbol": s, "markPrice": "1", "indexPrice": "1", "lastPrice": "1", "fundingRate": "0.0001", "nextFundingTime": "1", "fundingIntervalHour": "8", "fundingCap": "0.00375",
            "openInterest": "1", "openInterestValue": "1", "volume24h": "1", "turnover24h": "5"}


def default_instruments():
    return [inst("AAAUSDT"), inst("BBBUSDT"),
            inst("OLDUSDT", "Closed", delivery=T - 2 * DAY),                         # delivered inside the window
            inst("GONEUSDT", "Closed", delivery=W0 - 5 * DAY),                       # delivered before it
            inst("PREUSDT", "PreLaunch", launch=T + 10 * DAY),
            inst("LATEUSDT", "Trading", launch=T + 3 * DAY),                         # launches after it
            inst("NEVERUSDT", "Closed", launch=T - DAY, delivery=T - 2 * DAY),       # delivered before it launched
            inst("NODELUSDT", "Closed")]                                             # Closed, no delivery time


def make_universe(tmp_path, instruments=None, name="uni"):
    rows = instruments if instruments is not None else default_instruments()

    def fetch(url):
        base = venues.rest_for("mainnet")
        path, _, qs = url[len(base):].partition("?")
        q = dict(x.split("=", 1) for x in qs.split("&"))
        if path == UC.PATH_TICKERS:
            lst = [tick(r["symbol"]) for r in rows]
        else:
            want = q.get("status", "Trading")
            lst = [r for r in rows if r["status"] == want]
        return json.dumps({"retCode": 0, "retMsg": "OK", "result": {"category": q["category"], "list": lst, "nextPageCursor": ""}, "time": SERVER}).encode()
    d = tmp_path / name
    UC.capture_universe(str(d), fetch=fetch, clock_ms=lambda: SERVER, sleep=lambda _s: None, pause_s=0)
    return d


class Venue:
    """Serves ascending-grid history for (dataset, symbol), backward-paged like the venue."""
    def __init__(self, grids):
        self.grids = grids
        self.lock = threading.Lock()
        self.urls = []

    def __call__(self, url):
        base = venues.rest_for("mainnet")
        path, _, qs = url[len(base):].partition("?")
        q = dict(p.split("=", 1) for p in qs.split("&"))
        ds = {MH.DATASETS[d]["path"]: d for d in MH.DATASETS}[path]
        sym = q["symbol"]
        self.urls.append(url)
        spec = MH.DATASETS[ds]
        lo, hi, lim = int(q[spec["start"]]), int(q[spec["end"]]), int(q["limit"])
        ts = sorted((t for t in self.grids.get((ds, sym), []) if lo <= t <= hi), reverse=True)[:lim]
        rows = []
        for t in ts:
            if ds == MH.FUNDING:
                rows.append({"symbol": sym, "fundingRate": "0.0001", "fundingRateTimestamp": str(t)})
            else:
                rows.append([str(t), "100", "101", "99", "100.5"])
        d = {"retCode": 0, "retMsg": "OK", "result": {"category": "linear", "list": rows}, "time": SERVER}
        if ds != MH.FUNDING:
            d["result"]["symbol"] = sym
        return json.dumps(d).encode()


def grid(a, b, step):
    return list(range((a // step) * step if (a // step) * step >= a else (a // step + 1) * step, b + 1, step))


def collect(root, uni, ds, interval, syms, start, end, venue, plan_doc=None):
    recs, info = MH.load_universe_dir(str(uni), include_statuses=["Trading", "Closed"])
    specs = MH.build_specs(ds, interval, syms, start, end, recs)
    clk = iter(range(SERVER + 100, SERVER + 10 ** 7, 3))
    return MH.run_dataset(str(root), ds, interval, specs, fetch=venue, clock=lambda: next(clk), sleep=lambda _s: None, mono=lambda: 0.0, free_bytes_fn=lambda _p: 1 << 40,
                          reserve_bytes=1, universe=info, rate=1000.0)


def venue_for(syms, recs, start, end):
    g = {}
    for s in syms:
        r = recs[s]
        lo = max(start, int(r["launchTime"]))
        hi = min(end, int(r["deliveryTime"])) if int(r["deliveryTime"]) else end
        g[(MH.FUNDING, s)] = grid(lo, hi, H8)
        g[(MH.MARK, s)] = grid(lo, hi - H4 if hi - H4 > lo else hi, H4)
        g[(MH.INDEX, s)] = g[(MH.MARK, s)]
    return Venue(g)


def full_history(root, uni, syms=("AAAUSDT", "BBBUSDT", "OLDUSDT"), datasets=(MH.FUNDING, MH.MARK), start=W0, end=T):
    recs, _ = MH.load_universe_dir(str(uni), include_statuses=["Trading", "Closed"])
    v = venue_for(syms, {s: {k: str(x) if k in ("launchTime", "deliveryTime") else x for k, x in recs[s].items()} for s in syms}, start, end)
    for ds in datasets:
        collect(root, uni, ds, None if ds == MH.FUNDING else "240", list(syms), start, end, v)
    return v


def do_plan(uni, root=None, **kw):
    args = dict(study_from_ms=None, study_to_ms=T, **P)
    args.update(kw)
    return BP.plan(str(uni), str(root) if root else None, **args)


# ---------------------------------------------------------------- window

def test_the_registered_window_is_warmup_plus_formation_plus_sixty_weekly_holds():
    w = BP.required_window(None, T, 30, 7, 60, 7)
    assert w["needed_days"] == 7 + 30 + 60 * 7 == 457 and w["needed_ms"] == 457 * DAY
    assert w["effective_from_ms"] == T - 457 * DAY and w["effective_to_ms"] == T and w["components"]["holding_days"] == 420


def test_a_longer_operator_window_wins_and_a_shorter_one_is_flagged():
    w = BP.required_window(T - 500 * DAY, T, 30, 7, 60, 7)
    assert w["effective_days"] == 500 and w["study_window_sufficient"] is True
    short = BP.required_window(T - 100 * DAY, T, 30, 7, 60, 7)
    assert short["effective_days"] == 457 and short["study_window_sufficient"] is False


@pytest.mark.parametrize("bad", [dict(formation_days=0), dict(hold_days=-1), dict(min_holding_rows=0), dict(warmup_days=-1)])
def test_nonsense_parameters_are_refused(bad):
    args = dict(study_from_ms=None, study_to_ms=T, formation_days=30, hold_days=7, min_holding_rows=60, warmup_days=7)
    args.update(bad)
    with pytest.raises(BP.PlanError):
        BP.required_window(**args)
    with pytest.raises(BP.PlanError):
        BP.required_window(T, T, 30, 7, 60, 7)


# ---------------------------------------------------------------- eligibility

def test_eligibility_lists_every_exclusion_with_its_reason(tmp_path):
    uni = make_universe(tmp_path)
    doc = do_plan(uni)
    assert doc["eligible_symbols"] == ["AAAUSDT", "BBBUSDT", "OLDUSDT"]
    assert doc["excluded_detail"] == {"GONEUSDT": "delivered_before_window", "LATEUSDT": "launched_after_window",
                                      "NEVERUSDT": "never_trading", "NODELUSDT": "closed_without_delivery", "PREUSDT": "prelaunch_no_history"}
    assert doc["eligible_count"] + doc["excluded_count"] == doc["universe"]["rows_linear"] == 8
    assert doc["eligible_by_status"] == {"Closed": 1, "Trading": 2}


def test_status_filter_quote_filter_and_unknown_interval(tmp_path):
    rows = default_instruments() + [inst("ODDUSDT", "Closed", delivery=T - DAY, interval=0)]
    uni = make_universe(tmp_path, rows)
    assert "OLDUSDT" not in do_plan(uni, include_statuses=("Trading",))["eligible_symbols"]
    assert do_plan(uni)["excluded_detail"]["ODDUSDT"] == "funding_interval_unknown"


def test_the_capture_already_drops_non_usdt_perps_but_the_classifier_does_not_rely_on_it():
    r = {"status": "Trading", "launchTime": W0 - DAY, "deliveryTime": 0, "fundingInterval_min": 480}
    ok = lambda **kw: BP.classify_record({**r, **kw}, W0, T, ["Trading", "Closed"], ["USDT"])[:2]          # noqa: E731
    assert ok() == (True, "")
    assert ok(contractType="LinearFutures") == (False, "not_perpetual:LinearFutures")
    assert ok(quoteCoin="USDC") == (False, "quote_not_selected:USDC")
    assert BP.classify_record({**r, "quoteCoin": "USDC"}, W0, T, ["Trading"], None)[0] is True
    assert ok(status="Delivering", deliveryTime=T - DAY) == (False, "status_not_included:Delivering")
    assert BP.effective_statuses(["Trading", "Closed"]) == ["Trading", "Closed", "Delivering"] and BP.effective_statuses(["Trading"]) == ["Trading"]
    assert BP.classify_record({**r, "status": "Delivering", "deliveryTime": T - DAY}, W0, T, BP.effective_statuses(["Closed"]), None)[0] is True
    assert BP.classify_record({**r, "status": "Delivering", "deliveryTime": 0}, W0, T, ["Delivering"], None)[:2] == (False, "closed_without_delivery")
    assert ok(launchTime=0)[0] is True and "launch_unknown" in BP.classify_record({**r, "launchTime": 0}, W0, T, ["Trading"], None)[2]


def test_the_overlap_rule_agrees_with_what_market_history_would_skip(tmp_path):
    uni = make_universe(tmp_path)
    doc = do_plan(uni)
    recs, _ = MH.load_universe_dir(str(uni), include_statuses=["Trading", "Closed"])
    specs = MH.build_specs(MH.FUNDING, None, sorted(recs), W0, T, recs)
    skipped = {x["symbol"] for x in specs if x.get("skip")}
    assert skipped == {"GONEUSDT", "LATEUSDT"} and skipped <= set(doc["excluded_detail"])
    assert not (skipped & set(doc["eligible_symbols"]))


def test_an_unverified_universe_is_refused(tmp_path):
    uni = make_universe(tmp_path)
    f = next(n for n in os.listdir(uni) if n.startswith("universe-"))
    with open(uni / f, "ab") as fh:
        fh.write(b" ")
    with pytest.raises(BP.PlanError, match="universe capture refused"):
        do_plan(uni)
    with pytest.raises(BP.PlanError):
        do_plan(tmp_path / "nowhere")


# ---------------------------------------------------------------- commands, estimates, history

def test_no_history_root_means_everything_is_missing_and_the_commands_are_ordered(tmp_path):
    uni = make_universe(tmp_path)
    doc = do_plan(uni, workers=3, rate=2.5, plan_path="/p/plan.json", out_root="/data/h")
    assert all(e["counts"]["MISSING"] == 3 for e in doc["datasets"].values())
    assert doc["readiness"]["research_ready"] is False
    cmds = doc["commands"]
    assert [c["dataset"] for c in cmds[:5]] == ["funding", "mark-kline", "index-kline", "kline", "open-interest"]
    for c in cmds[:5]:
        line = c["command"]
        for must in ("--symbols-from-plan /p/plan.json", f"--universe-capture {uni}", "--include-statuses Trading,Closed,Delivering", f"--days {NEED_DAYS}", f"--end-ms {T}", "--workers 3", "--rate 2.5",
                     "--out-root /data/h"):
            assert must in line
    assert "--interval 240" in cmds[1]["command"] and "--interval 4h" in cmds[4]["command"] and "--interval" not in cmds[0]["command"]
    assert {c["command"].split("--symbols-from-plan ")[1].split(" ")[0] for c in cmds[:5]} == {"/p/plan.json"}      # one symbol source for every dataset


def test_request_estimate_uses_the_real_page_limit_and_rate(tmp_path):
    uni = make_universe(tmp_path, [inst("AAAUSDT")])
    e = do_plan(uni, rate=4.0)["datasets"]["funding"]["estimate"]
    rows = (T - (T - NEED_DAYS * DAY)) // H8 + 1                          # 19
    assert e["page_limit"] == 200 and e["requests_total"] == -(-rows // 200) + 1 == 2
    assert e["wall_clock_lower_bound_s_total"] == 0.5 and e["requests_remaining"] == 2
    k = do_plan(uni, rate=4.0)["datasets"]["kline"]["estimate"]
    assert k["page_limit"] == 1000


def test_complete_history_for_every_eligible_symbol_is_research_ready(root, tmp_path):
    uni = make_universe(tmp_path)
    full_history(root, uni)
    doc = do_plan(uni, root)
    assert doc["datasets"]["funding"]["counts"]["COMPLETE"] == 3 and doc["datasets"]["mark-kline"]["counts"]["COMPLETE"] == 3, doc["datasets"]["funding"]["missing_ranges"]
    assert doc["datasets"]["funding"]["verified"] is True and doc["blockers"] == []
    assert doc["readiness"]["research_ready"] is True and doc["readiness"]["unmet"] == []
    assert doc["datasets"]["index-kline"]["counts"]["MISSING"] == 3 and doc["datasets"]["index-kline"]["required"] is False       # optional datasets do not block
    assert do_plan(uni, root, required_datasets=("funding", "mark-kline", "index-kline"))["readiness"]["research_ready"] is False


def test_a_missing_symbol_in_mark_names_its_exact_range_and_blocks_readiness(root, tmp_path):
    uni = make_universe(tmp_path)
    full_history(root, uni, datasets=(MH.FUNDING,))
    full_history(root, uni, syms=("AAAUSDT", "BBBUSDT"), datasets=(MH.MARK,))
    doc = do_plan(uni, root)
    m = doc["datasets"]["mark-kline"]
    assert m["counts"]["MISSING"] == 1 and m["symbols_by_status"]["MISSING"]["names"] == ["OLDUSDT"]
    assert m["missing_ranges"]["OLDUSDT"]["ranges_ms"] == [[W0, T - 2 * DAY]]                   # its lifecycle clamps the end to the delivery time
    assert doc["readiness"]["research_ready"] is False
    assert any(b["code"] == "SYMBOL_SET_MISMATCH" for b in doc["blockers"]) and "only_in_funding" in doc["symbol_set_mismatch"]["mark-kline"]


def test_a_dataset_collected_for_a_different_symbol_set_is_flagged(root, tmp_path):
    uni = make_universe(tmp_path)
    full_history(root, uni)
    full_history(root, uni, syms=("AAAUSDT", "BBBUSDT", "OLDUSDT", "GONEUSDT"), datasets=(MH.INDEX,)) if False else None
    rows = default_instruments() + [inst("EXTRAUSDT")]
    uni2 = make_universe(tmp_path, rows, name="uni2")
    full_history(root, uni2, syms=("AAAUSDT", "BBBUSDT", "OLDUSDT", "EXTRAUSDT"), datasets=(MH.MARK,))      # mark re-collected from a wider list
    doc = do_plan(uni, root)
    mm = doc["symbol_set_mismatch"]["mark-kline"]
    assert mm["only_in_this_dataset"]["names"] == ["EXTRAUSDT"] and mm["not_in_plan"]["names"] == ["EXTRAUSDT"]
    assert doc["readiness"]["research_ready"] is False


def test_a_window_collected_shorter_than_required_is_partial_with_the_missing_head(root, tmp_path):
    uni = make_universe(tmp_path)
    full_history(root, uni, start=W0 + 2 * DAY)
    doc = do_plan(uni, root)
    f = doc["datasets"]["funding"]
    assert f["counts"]["COMPLETE"] == 0 and f["counts"]["PARTIAL"] == 3
    assert f["missing_ranges"]["AAAUSDT"]["ranges_ms"] == [[W0, W0 + 2 * DAY - 1]] and "window_short_start" in f["missing_ranges"]["AAAUSDT"]["reasons"]
    assert doc["readiness"]["research_ready"] is False


def test_a_dataset_that_does_not_re_hash_is_never_ready(root, tmp_path):
    uni = make_universe(tmp_path)
    full_history(root, uni)
    page = root / "funding" / "AAAUSDT" / "page-000001.json"
    page.write_bytes(page.read_bytes() + b" ")
    doc = do_plan(uni, root)
    assert doc["datasets"]["funding"]["verified"] is False and doc["datasets"]["funding"]["counts"]["UNVERIFIED"] == 3
    assert doc["readiness"]["research_ready"] is False
    skipped = do_plan(uni, root, verify=False)
    assert skipped["readiness"]["research_ready"] is False                # not verified here = not ready, whatever the manifest claims


def test_a_short_study_window_blocks_readiness_even_with_full_data(root, tmp_path):
    uni = make_universe(tmp_path)
    full_history(root, uni)
    doc = do_plan(uni, root, study_from_ms=T - 2 * DAY)
    assert any(b["code"] == "STUDY_WINDOW_TOO_SHORT" for b in doc["blockers"]) and doc["readiness"]["research_ready"] is False


# ---------------------------------------------------------------- the plan seals the symbol set for market_history

def test_market_history_takes_its_symbols_from_the_plan_and_refuses_an_edited_one(root, tmp_path, capsys):
    uni = make_universe(tmp_path)
    doc = do_plan(uni, plan_path=str(tmp_path / "plan.json"))
    pf = tmp_path / "plan.json"
    pf.write_text(json.dumps(doc))
    recs, _ = MH.load_universe_dir(str(uni), include_statuses=["Trading", "Closed"])
    v = venue_for(doc["eligible_symbols"], {s: {k: str(x) if k in ("launchTime", "deliveryTime") else x for k, x in recs[s].items()} for s in doc["eligible_symbols"]}, W0, T)
    clk = iter(range(SERVER + 100, SERVER + 10 ** 7, 3))
    args = ["funding", "--out-root", str(root), "--symbols-from-plan", str(pf), "--universe-capture", str(uni), "--include-statuses", "Trading,Closed,Delivering", "--days", str(NEED_DAYS),
            "--end-ms", str(T), "--rate", "1000"]
    rc = MH.main(args, fetch=v, clock=lambda: next(clk), sleep=lambda _s: None, mono=lambda: 0.0, free_bytes_fn=lambda _p: 1 << 40)
    assert rc == 0, capsys.readouterr()
    man = json.loads((root / "funding" / "manifest.json").read_text())
    assert sorted(man["symbols"]) == doc["eligible_symbols"] and man["universe"]["symbols_from_plan"]["plan_digest"] == doc["plan_digest"]
    after = BP.plan(str(uni), str(root), study_from_ms=None, study_to_ms=T, **P)
    assert after["datasets"]["funding"]["counts"]["COMPLETE"] == 3
    # edited symbol list
    bad = copy.deepcopy(doc)
    bad["eligible_symbols"] = bad["eligible_symbols"] + ["EXTRAUSDT"]
    bad["eligible_count"] += 1
    (tmp_path / "bad.json").write_text(json.dumps(bad))
    assert MH.main(["funding", "--out-root", str(root), "--symbols-from-plan", str(tmp_path / "bad.json"), "--days", str(NEED_DAYS), "--end-ms", str(T)], fetch=v) == 2
    assert "plan_digest" in capsys.readouterr().err


def test_market_history_refuses_a_window_that_differs_from_the_plans_and_a_foreign_universe(root, tmp_path, capsys):
    uni = make_universe(tmp_path)
    doc = do_plan(uni)
    pf = tmp_path / "plan.json"
    pf.write_text(json.dumps(doc))
    other = make_universe(tmp_path, default_instruments() + [inst("ZZZUSDT")], name="other")
    v = Venue({})
    base = ["funding", "--out-root", str(root), "--symbols-from-plan", str(pf)]
    assert MH.main(base + ["--days", "5", "--end-ms", str(T)], fetch=v) == 2 and "differs from the plan's" in capsys.readouterr().err
    assert MH.main(base + ["--days", str(NEED_DAYS), "--end-ms", str(T), "--universe-capture", str(other), "--include-statuses", "Trading,Closed,Delivering"], fetch=v) == 2
    assert "manifest sha256 differs" in capsys.readouterr().err
    assert v.urls == []
    assert MH.main(["funding", "--out-root", str(root), "--days", "5"], fetch=v) == 2         # no symbol source at all


def test_a_resealed_but_inconsistent_plan_statuses_and_mixed_symbol_sources_are_refused(root, tmp_path, capsys):
    uni = make_universe(tmp_path)
    doc = do_plan(uni)
    v = Venue({})
    window = ["--days", str(NEED_DAYS), "--end-ms", str(T)]
    # a plan whose digest was recomputed after its count was edited
    lie = copy.deepcopy(doc)
    lie["eligible_count"] = 99
    lie["plan_digest"] = MH.plan_digest(lie)
    (tmp_path / "lie.json").write_text(json.dumps(lie))
    assert MH.main(["funding", "--out-root", str(root), "--symbols-from-plan", str(tmp_path / "lie.json")] + window, fetch=v) == 2
    assert "eligible_count" in capsys.readouterr().err
    (tmp_path / "ok.json").write_text(json.dumps(doc))
    ok = ["funding", "--out-root", str(root), "--symbols-from-plan", str(tmp_path / "ok.json")] + window
    assert MH.main(ok + ["--include-statuses", "Trading"], fetch=v) == 2 and "differs from the plan's" in capsys.readouterr().err
    assert MH.main(ok + ["--symbols", "AAAUSDT"], fetch=v) == 2
    assert v.urls == []


# ---------------------------------------------------------------- diagnose

def diag(uni, root, **kw):
    args = dict(study_from_ms=None, study_to_ms=T, **P)
    args.update(kw)
    return BP.diagnose(str(uni) if uni else None, str(root), **args)


def test_diagnose_flags_an_unsupplied_universe_dir_as_the_first_blocker_and_explains_the_scan(root, tmp_path):
    uni = make_universe(tmp_path)                               # sits NEXT TO the history root, as in the owner's datasets/ layout
    full_history(root, uni)
    res = diag(None, root)
    assert res["first_blocker"]["code"] == "UNIVERSE_DIR_NOT_SUPPLIED"
    assert res["data_scan"]["universe_capture_dirs"] == []
    sib = {s["dir"]: s for s in res["data_scan"]["sibling_manifests_outside_scanned_root"]}
    assert sib[str(uni)]["schema"] == "universe_capture/v2" and "sibling" in sib[str(uni)]["why_not_found"]
    # positive control: scanning the parent finds it
    assert diag(None, root, data_root=str(tmp_path))["data_scan"]["universe_capture_dirs"] == [str(uni)]


def test_diagnose_funnel_matches_the_real_loader_and_names_the_cause(root, tmp_path):
    uni = make_universe(tmp_path)
    full_history(root, uni, syms=("AAAUSDT", "OLDUSDT"))        # 3 are eligible; history was collected for 2
    res = diag(uni, root)
    steps = {s["step"]: s["count"] for s in res["steps"]}
    assert steps["1_universe_rows_linear"] == 8 and steps["2_eligible_lifecycle"] == 3
    assert steps["3_in_funding_dataset"] == 2 and steps["4_in_mark-kline_dataset"] == 2 and steps["8_loadable"] == 2 and steps["9_complete_over_the_required_window"] == 2
    assert res["predicted_symbols_loaded"] == 2 and res["loader_run"]["symbols_loaded"] == 2 and res["funnel_matches_loader"] is True
    codes = [b["code"] for b in res["blockers"]]
    assert "HISTORY_COVERS_FEW_ELIGIBLE_SYMBOLS" in codes and "UNIVERSE_DIR_NOT_SUPPLIED" not in codes
    b = next(b for b in res["blockers"] if b["code"] == "HISTORY_COVERS_FEW_ELIGIBLE_SYMBOLS")
    assert "1 of 3" in b["detail"] and "included_statuses" in b["detail"]
    assert steps and all(s["symbols"]["count"] == s["count"] for s in res["steps"])           # counts are complete even where the names are capped


def test_a_funnel_that_disagrees_with_the_real_loader_is_reported_not_trusted(root, tmp_path, monkeypatch):
    uni = make_universe(tmp_path)
    full_history(root, uni)
    monkeypatch.setattr(BP, "_run_loader", lambda *_a: {"symbols_loaded": 99})
    res = diag(uni, root)
    assert res["funnel_matches_loader"] is False and any(b["code"] == "FUNNEL_DISAGREES_WITH_LOADER" for b in res["blockers"])


def test_diagnose_counts_complete_when_the_names_are_capped(root, tmp_path):
    many = [inst(f"S{i:03d}USDT") for i in range(BP.NAMES_CAP + 5)]
    uni = make_universe(tmp_path, many)
    s = BP.diagnose(str(uni), str(root), study_from_ms=None, study_to_ms=T, **P)
    first = s["steps"] and next(x for x in s["steps"] if x["step"].startswith("1_"))
    assert first["count"] == BP.NAMES_CAP + 5 and len(first["symbols"]["names"]) == BP.NAMES_CAP and first["symbols"]["names_truncated"] is True


def test_diagnose_too_few_holding_rows(root, tmp_path):
    uni = make_universe(tmp_path)
    full_history(root, uni)                                     # 6 days of history
    res = diag(uni, root, min_holding_rows=60)                  # the real design needs 60
    assert res["holding_rows_estimate"] < 60 and any(b["code"] == "TOO_FEW_HOLDING_ROWS" for b in res["blockers"])
    ok = diag(uni, root, formation_days=2, min_holding_rows=3)
    assert not any(b["code"] == "TOO_FEW_HOLDING_ROWS" for b in ok["blockers"])


def test_diagnose_reports_missing_and_unverifying_datasets(root, tmp_path):
    uni = make_universe(tmp_path)
    full_history(root, uni, datasets=(MH.FUNDING,))
    res = diag(uni, root)
    assert any(b["code"] == "MARK_DATASET_AMBIGUOUS_OR_ABSENT" for b in res["blockers"])
    full_history(root, uni, datasets=(MH.MARK,))
    shutil.rmtree(root / "funding" / "BBBUSDT")
    assert any(b["code"] == "DATASET_DOES_NOT_VERIFY" for b in diag(uni, root)["blockers"])


# ---------------------------------------------------------------- CLI

def test_cli_plan_writes_a_sealed_plan_and_exits_nonzero_until_ready(root, tmp_path, capsys):
    uni = make_universe(tmp_path)
    out = tmp_path / "plan.json"
    argv = ["plan", "--universe-capture", str(uni), "--history-root", str(root), "--study-to-ms", str(T), "--formation-days", "2", "--hold-days", "1", "--min-holding-rows", "3",
            "--warmup-days", "1", "--out", str(out)]
    assert BP.main(argv) == 1
    doc = json.loads(out.read_text())
    assert MH.plan_digest(doc) == doc["plan_digest"] and oct(os.stat(out).st_mode & 0o777) == "0o600"
    full_history(root, uni)
    assert BP.main(argv) == 0
    assert BP.main(["plan", "--universe-capture", str(tmp_path / "nowhere"), "--study-to-ms", str(T)]) == 2
    assert BP.main(["diagnose", "--history-root", str(root), "--study-to-ms", str(T), "--formation-days", "2", "--hold-days", "1", "--min-holding-rows", "3", "--warmup-days", "1"]) == 1
    capsys.readouterr()


def test_the_module_reads_files_only():
    import ast
    src = open(os.path.join(BOT, "tools", "backfill_plan.py")).read()
    tree = ast.parse(src)
    imps = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    for bad in ("socket", "urllib", "requests", "http", "hmac", "bybit_connection", "carry_broker", "subprocess"):
        assert not any((i or "").split(".")[0] == bad for i in imps)
    calls = " ".join(ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert "BybitClient" not in calls and "environ" not in calls and "getenv" not in calls


# ---------------------------------------------------------------- run-plan: sealed plan -> dataset by dataset, resumable, into a NEW root

import itertools  # noqa: E402
import urllib.parse  # noqa: E402

import market_collector as MC  # noqa: E402


def _sealed(tmp_path, uni, **kw):
    doc = do_plan(uni, **kw)
    pf = tmp_path / "plan.json"
    pf.write_text(json.dumps(doc))
    return doc, str(pf)


def _clock():
    c = itertools.count(SERVER + 100, 3)
    return lambda: next(c)


def _venue_for_plan(uni, doc):
    recs, _ = MH.load_universe_dir(str(uni), include_statuses=["Trading", "Closed"])
    syms = doc["eligible_symbols"]
    return venue_for(syms, {s: {k: str(x) if k in ("launchTime", "deliveryTime") else x for k, x in recs[s].items()} for s in syms}, W0, T)


def _run(pf, out, uni, v, **kw):
    a = dict(universe_dir=str(uni), fetch=v, clock=_clock(), sleep=lambda _s: None, mono=lambda: 0.0, workers=1, rate=1000.0, free_bytes_fn=lambda _p: 1 << 40, reserve_bytes=1)
    a.update(kw)
    return MH.run_plan(pf, str(out), **a)


def test_run_order_is_the_planners_dataset_order():
    assert MH.RUN_ORDER == BP.DATASET_ORDER


def test_run_plan_collects_every_required_dataset_for_the_same_symbols_and_the_replan_is_ready(tmp_path):
    uni = make_universe(tmp_path)
    doc, pf = _sealed(tmp_path, uni)
    out = tmp_path / "new"
    prog = _run(pf, out, uni, _venue_for_plan(uni, doc))
    assert prog["status"] == "DONE" and [d for d in prog["datasets"]] == ["funding", "mark-kline"]
    f, m = (json.loads((out / d / "manifest.json").read_text()) for d in ("funding", "mark-kline-240"))
    assert sorted(f["symbols"]) == sorted(m["symbols"]) == doc["eligible_symbols"]
    assert f["selection"] == {"mode": "FROM_PLAN", "plan_digest": doc["plan_digest"], "symbols_n": 3}
    p = json.loads((out / MH.PROGRESS_NAME).read_text())
    assert p["datasets"]["funding"]["symbols_remaining_count"] == 0 and p["totals"]["requests_made"] > 0 and p["totals"]["eta_lower_bound_s"] == 0.0 and oct(os.stat(out / MH.PROGRESS_NAME).st_mode & 0o777) == "0o600"
    assert do_plan(uni, out)["readiness"]["research_ready"] is True
    assert BP.diagnose(str(uni), str(out), study_from_ms=None, study_to_ms=T, **P)["funnel_matches_loader"] is True


def test_an_interrupted_run_resumes_without_refetching_a_single_page(tmp_path):
    uni = make_universe(tmp_path)
    doc, pf = _sealed(tmp_path, uni)
    out = tmp_path / "new"
    v1 = _venue_for_plan(uni, doc)
    with pytest.raises(KeyboardInterrupt):
        _run(pf, out, uni, v1, stop_after_requests=2)
    p = json.loads((out / MH.PROGRESS_NAME).read_text())
    assert p["status"] == "INTERRUPTED" and p["last_error"]["error"].startswith("KeyboardInterrupt") and p["datasets"]["funding"]["status"] == "INTERRUPTED"
    assert 0 < len(v1.urls) <= 2 and p["datasets"]["funding"]["symbols_remaining_count"] > 0
    v2 = _venue_for_plan(uni, doc)
    prog = _run(pf, out, uni, v2)
    assert prog["status"] == "DONE" and prog["resumed"] == 1
    assert not (set(v1.urls) & set(v2.urls)), "a page fetched before the interruption was fetched again"
    assert do_plan(uni, out)["readiness"]["research_ready"] is True
    v3 = _venue_for_plan(uni, doc)
    assert _run(pf, out, uni, v3)["status"] == "DONE" and v3.urls == []                       # a finished run costs zero requests


def test_a_429_storm_backs_off_boundedly_stops_before_the_next_dataset_and_resumes(tmp_path):
    uni = make_universe(tmp_path)
    doc, pf = _sealed(tmp_path, uni)
    out = tmp_path / "new"
    sleeps, now = [], [0.0]

    def sleep(sec):                                              # a virtual clock: the token bucket's penalty only elapses when somebody sleeps
        sleeps.append(sec)
        now[0] += sec

    def storm(url):
        raise MC.RateLimited("429")
    prog = _run(pf, out, uni, storm, sleep=sleep, mono=lambda: now[0], max_retries=3, backoff_s=1.0)
    assert prog["status"] == "INCOMPLETE" and "rate_limited" in prog["last_error"]["error"] and not (out / "mark-kline-240").exists()
    assert {1.0, 2.0, 4.0} <= set(sleeps) and max(sleeps) <= MC.BACKOFF_CAP_S             # exponential, bounded, per symbol
    assert prog["datasets"]["funding"]["rate_limit_events"] > 0
    v = _venue_for_plan(uni, doc)
    assert _run(pf, out, uni, v)["status"] == "DONE"


def test_a_closed_contract_is_never_asked_past_its_delivery_time(tmp_path):
    uni = make_universe(tmp_path)
    doc, pf = _sealed(tmp_path, uni)
    v = _venue_for_plan(uni, doc)
    _run(pf, tmp_path / "new", uni, v)
    endtime = lambda u: int(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query))[MH.DATASETS[MH.FUNDING]["end"]])      # noqa: E731
    fund_urls = [u for u in v.urls if MH.DATASETS[MH.FUNDING]["path"] in u]
    ends = [endtime(u) for u in fund_urls if "symbol=OLDUSDT" in u]
    assert ends and max(ends) <= T - 2 * DAY and any(e == T - 2 * DAY for e in ends)
    other = [endtime(u) for u in fund_urls if "symbol=AAAUSDT" in u]
    assert max(other) == T


def test_run_plan_refuses_git_roots_existing_datasets_foreign_plans_and_forming_candles(tmp_path):
    uni = make_universe(tmp_path)
    doc, pf = _sealed(tmp_path, uni)
    v = Venue({})
    with pytest.raises(MH.HistoryError, match="git work tree"):
        _run(pf, os.path.join(BOT, "zz_never_created_runplan"), uni, v)
    assert not os.path.exists(os.path.join(BOT, "zz_never_created_runplan"))
    old = tmp_path / "old"
    os.mkdir(old, 0o700)
    os.mkdir(old / "funding", 0o700)
    (old / "funding" / "manifest.json").write_text("{}")
    with pytest.raises(MH.HistoryError, match="existing dataset"):
        _run(pf, old, uni, v)
    assert (old / "funding" / "manifest.json").read_text() == "{}" and v.urls == []
    other = do_plan(uni, formation_days=3)
    (tmp_path / "plan2.json").write_text(json.dumps(other))
    done = tmp_path / "new"
    _run(pf, done, uni, _venue_for_plan(uni, doc))
    with pytest.raises(MH.HistoryError, match="different plan"):
        _run(str(tmp_path / "plan2.json"), done, uni, v)
    with pytest.raises(MH.HistoryError, match="WINDOW_END_NOT_CLOSED"):
        _run(pf, tmp_path / "early", uni, v, clock=lambda: T + 1000)
    assert not (tmp_path / "early").exists() and v.urls == []
    assert sorted(os.listdir(tmp_path)) == sorted(["uni", "plan.json", "plan2.json", "old", "new"])          # nothing was written anywhere else


def test_a_dataset_whose_symbol_set_differs_from_the_plan_stops_the_run(tmp_path):
    uni = make_universe(tmp_path)
    inject = lambda rec, a, b, st, q: (True, "", []) if rec["symbol"] == "GONEUSDT" else BP.classify_record(rec, a, b, st, q)      # noqa: E731
    doc, pf = _sealed(tmp_path, uni, classifier=inject)
    assert "GONEUSDT" in doc["eligible_symbols"]
    v = Venue({(MH.FUNDING, s): grid(W0, T, H8) for s in doc["eligible_symbols"]})
    prog = _run(pf, tmp_path / "new", uni, v)
    assert prog["status"] == "FAILED" and "SYMBOL_SET_MISMATCH" in prog["last_error"]["error"] and not (tmp_path / "new" / "mark-kline-240").exists()


def test_an_unproven_funding_interval_change_still_blocks_the_dataset_through_run_plan(tmp_path):
    uni = make_universe(tmp_path, [inst("AAAUSDT"), inst("BBBUSDT")])
    doc, pf = _sealed(tmp_path, uni)
    mid = W0 + 3 * DAY
    g = {(MH.FUNDING, "AAAUSDT"): grid(W0, mid, H8) + grid(mid + H4, T, H4), (MH.FUNDING, "BBBUSDT"): grid(W0, T, H8)}
    prog = _run(pf, tmp_path / "new", uni, Venue(g))
    man = json.loads((tmp_path / "new" / "funding" / "manifest.json").read_text())
    assert man["symbols"]["AAAUSDT"]["status"] == "PARTIAL" and "unproven_regime_change" in man["symbols"]["AAAUSDT"]["reasons"]
    assert prog["status"] == "INCOMPLETE" and not (tmp_path / "new" / "mark-kline-240").exists()


def test_the_cli_run_plan_and_the_recorded_selection_modes(tmp_path, capsys):
    uni = make_universe(tmp_path)
    doc, pf = _sealed(tmp_path, uni)
    v = _venue_for_plan(uni, doc)
    rc = MH.main(["run-plan", "--plan", pf, "--out-root", str(tmp_path / "cli"), "--universe-capture", str(uni), "--rate", "1000"], fetch=v, clock=_clock(), sleep=lambda _s: None,
                 mono=lambda: 0.0, free_bytes_fn=lambda _p: 1 << 40)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["status"] == "DONE" and out["progress_file"].endswith(MH.PROGRESS_NAME)
    ex = tmp_path / "explicit"
    MH.main(["funding", "--out-root", str(ex), "--symbols", "AAAUSDT", "--start-ms", str(W0), "--end-ms", str(T), "--rate", "1000"], fetch=_venue_for_plan(uni, doc), clock=_clock(),
            sleep=lambda _s: None, mono=lambda: 0.0, free_bytes_fn=lambda _p: 1 << 40)
    assert json.loads((ex / "funding" / "manifest.json").read_text())["selection"] == {"mode": "EXPLICIT_SYMBOLS", "plan_digest": None, "symbols_n": 1}
    capsys.readouterr()


# ---------------------------------------------------------------- the nine-symbol diagnosis: WHY did the loader see so few symbols, from the manifests alone

def _explicit(root, uni, syms, **kw):
    recs, info = MH.load_universe_dir(str(uni), include_statuses=["Trading", "Closed"])
    v = venue_for(syms, {s: {k: str(x) if k in ("launchTime", "deliveryTime") else x for k, x in recs[s].items()} for s in syms}, W0, T)
    for ds, iv in ((MH.FUNDING, None), (MH.MARK, "240")):
        specs = MH.build_specs(ds, iv, syms, W0, T, None, {s: 480 for s in syms})                       # no record: nothing is skipped by lifecycle, as with a hand-typed --symbols list
        MH.run_dataset(str(root), ds, iv, specs, fetch=v, clock=_clock(), sleep=lambda _s: None, mono=lambda: 0.0, free_bytes_fn=lambda _p: 1 << 40, reserve_bytes=1, rate=1000.0,
                       selection={"mode": "EXPLICIT_SYMBOLS", "plan_digest": None}, **kw)


def _cause(res, name):
    return next(c for c in res["why_few_symbols"]["causes"] if c["cause"] == name)


def test_a_dataset_collected_from_an_explicit_symbols_list_is_named_as_the_cause(root, tmp_path):
    uni = make_universe(tmp_path)
    _explicit(root, uni, ["AAAUSDT", "OLDUSDT"])
    res = diag(uni, root)
    w = res["why_few_symbols"]
    assert w["primary_cause"] == "a_explicit_symbols_list" and w["loaded_symbols"] == 2 and w["eligible_symbols"] == 3
    assert _cause(res, "a_explicit_symbols_list")["verdict"] == "APPLIES" and _cause(res, "a_explicit_symbols_list")["evidence"]["selection"]["mode"] == "EXPLICIT_SYMBOLS"
    assert _cause(res, "b_universe_dir_not_supplied_or_recognised")["verdict"] == "DOES_NOT_APPLY"
    seq = [c["command"] for c in w["command_sequence"]]
    assert "backfill_plan.py diagnose" in seq[0] and "backfill_plan.py plan" in seq[1] and "market_history.py run-plan" in seq[2] and "backfill_plan.py plan" in seq[3] and "candidate_readiness.py" in seq[4]
    assert all(f"{root}-full-{T}" in c for c in seq[1:4]) and f"--study-to-ms {T}" in seq[0]            # a NEW root next to the old one: nothing is overwritten


def test_a_legacy_manifest_without_a_selection_block_is_unknown_legacy_and_suspected(root, tmp_path):
    uni = make_universe(tmp_path)
    _explicit(root, uni, ["AAAUSDT", "OLDUSDT"])
    for d in ("funding", "mark-kline-240"):                                          # emulate a manifest written before the block existed (and re-seal its digest)
        mp = root / d / "manifest.json"
        man = json.loads(mp.read_text())
        del man["selection"]
        man["universe"] = None
        man["manifest_digest"] = MH._sha(MH._canon({k: v for k, v in man.items() if k != "manifest_digest"}))
        mp.write_text(json.dumps(man, indent=1, sort_keys=True))
    res = diag(uni, root)
    assert res["selection"]["funding"]["mode"] == "UNKNOWN_LEGACY" and res["selection"]["funding"]["recorded"] is False and "explicit" in res["selection"]["funding"]["legacy_hint"]
    assert _cause(res, "d_dataset_status")["verdict"] == "DOES_NOT_APPLY"
    assert _cause(res, "a_explicit_symbols_list")["verdict"] == "SUSPECTED" and res["why_few_symbols"]["primary_cause"] == "a_explicit_symbols_list"
    assert BP.plan(str(uni), str(root), study_from_ms=None, study_to_ms=T, **P)["datasets"]["funding"]["selection"]["mode"] == "UNKNOWN_LEGACY"


def test_an_unsupplied_universe_dir_is_cause_b_and_the_fix_sequence_has_a_placeholder(root, tmp_path):
    uni = make_universe(tmp_path)
    full_history(root, uni)
    res = diag(None, root)
    assert _cause(res, "b_universe_dir_not_supplied_or_recognised")["verdict"] == "APPLIES" and res["why_few_symbols"]["primary_cause"] == "b_universe_dir_not_supplied_or_recognised"
    assert "<UNIVERSE_CAPTURE_DIR>" in res["why_few_symbols"]["command_sequence"][1]["command"]


def test_lifecycle_filtering_applies_only_when_collected_symbols_are_dropped_by_it(root, tmp_path):
    uni = make_universe(tmp_path)
    _explicit(root, uni, ["AAAUSDT", "GONEUSDT"])                                    # GONE was delivered before the window: collected anyway by hand
    res = diag(uni, root)
    c = _cause(res, "c_lifecycle_filtering")
    assert c["verdict"] == "APPLIES" and c["evidence"]["collected_but_lifecycle_excluded"]["names"] == ["GONEUSDT"]


def test_dataset_status_is_cause_d_when_eligible_collected_symbols_fail_the_status_checks(root, tmp_path):
    uni = make_universe(tmp_path)
    full_history(root, uni, datasets=(MH.FUNDING,))
    full_history(root, uni, syms=("AAAUSDT", "BBBUSDT"), datasets=(MH.MARK,))
    res = diag(uni, root)
    d = _cause(res, "d_dataset_status")
    assert d["verdict"] == "APPLIES" and d["evidence"]["stage_exclusions_by_reason"] == {"not_in_mark_dataset": 1}
    assert _cause(res, "c_lifecycle_filtering")["verdict"] == "DOES_NOT_APPLY"


# ---------------------------------------------------------------- required datasets per candidate spec; what remains when the span cannot be covered

def _spec(*names):
    return {"candidate_id": "synthetic@1", "required_data": [{"name": n} for n in names]}


def test_the_real_candidate_specs_map_to_their_datasets_and_name_the_unverifiable_items():
    import candidate_readiness as CR
    specs = dict(CR.load_candidates())
    a = BP.datasets_for_spec(specs["cross_sectional_funding"])
    assert a["datasets"] == ["funding", "mark-kline", "index-kline"] and [u["kinds"] for u in a["unverifiable"]] == [["depth"]]
    b = BP.datasets_for_spec(specs["spot_perp_basis"])
    assert "funding" in b["datasets"] and "index-kline" in b["datasets"] and b["spot_availability"] is True and b["unverifiable"][0]["kinds"] == ["depth", "spot_price"]
    c = BP.datasets_for_spec(_spec("open interest per symbol", "kline candles per symbol", "funding history", "mark price per symbol"))
    assert c["datasets"] == ["funding", "mark-kline", "kline", "open-interest"] and c["unverifiable"] == []


def test_a_spec_that_needs_depth_can_never_be_research_ready_however_complete_the_history(root, tmp_path):
    import candidate_readiness as CR
    uni = make_universe(tmp_path)
    full_history(root, uni, datasets=(MH.FUNDING, MH.MARK, MH.INDEX))
    doc = do_plan(uni, root, candidate_spec=dict(CR.load_candidates())["cross_sectional_funding"])
    assert doc["parameters"]["required_datasets"] == ["funding", "mark-kline", "index-kline"] and doc["datasets"]["index-kline"]["counts"]["COMPLETE"] == 3
    assert doc["readiness"]["research_ready"] is False and any("UNVERIFIABLE" in u and "depth" in u for u in doc["readiness"]["unmet"])
    assert doc["candidate"]["required"]["unverifiable"][0]["kinds"] == ["depth"]


def test_a_spec_whose_datasets_are_all_collectable_is_ready_only_when_each_is_complete(root, tmp_path):
    uni = make_universe(tmp_path)
    full_history(root, uni)
    spec = _spec("funding history per symbol", "mark price per symbol", "kline candles per symbol")
    doc = do_plan(uni, root, candidate_spec=spec)
    assert doc["parameters"]["required_datasets"] == ["funding", "mark-kline", "kline"] and doc["readiness"]["research_ready"] is False
    assert doc["datasets"]["kline"]["counts"]["MISSING"] == 3 and doc["datasets"]["funding"]["counts"]["COMPLETE"] == 3
    assert do_plan(uni, root, candidate_spec=_spec("funding history", "mark price"))["readiness"]["research_ready"] is True


def test_spot_availability_needs_a_verified_spot_snapshot(root, tmp_path):
    uni = make_universe(tmp_path)
    full_history(root, uni)
    doc = do_plan(uni, root, candidate_spec=_spec("funding history", "mark price", "spot hedge availability"))
    assert doc["candidate"]["spot_coverage"] == {"status": "NOT_SUPPLIED"} and any("spot availability" in u for u in doc["readiness"]["unmet"]) and doc["readiness"]["research_ready"] is False


def test_the_replan_of_a_candidate_plan_keeps_the_candidate(root, tmp_path):
    import candidate_readiness as CR
    uni = make_universe(tmp_path)
    full_history(root, uni)
    spec = dict(CR.load_candidates())["cross_sectional_funding"]
    doc = do_plan(uni, root, candidate_spec=spec)
    fresh = BP.replan(doc, str(root))
    assert fresh["candidate"]["required"]["unverifiable"] == doc["candidate"]["required"]["unverifiable"] and fresh["readiness"]["research_ready"] is False


def test_a_collection_shorter_than_the_required_span_states_what_remains_and_is_labelled_a_diagnostic(root, tmp_path):
    uni = make_universe(tmp_path)
    full_history(root, uni, start=W0 + 2 * DAY)
    doc = do_plan(uni, root)
    s = doc["history_shortfall"]
    assert doc["evidence_class"] == s["evidence_class"] == "SHORT_DIAGNOSTIC_NOT_STRATEGY_EVIDENCE" and s["required_days"] == NEED_DAYS and s["short_by_days_max"] == 2.0
    assert s["datasets"]["funding"]["collected_days"] == 4.0 and "window_short_start" in s["datasets"]["funding"]["causes"] and "NEW root" in s["remaining"]
    assert any("SHORT_DIAGNOSTIC" in u for u in doc["readiness"]["unmet"])
    full = tmp_path / "full"
    os.mkdir(full, 0o700)
    full_history(full, uni)
    ok = do_plan(uni, full)
    assert ok["evidence_class"] == "FULL_SPAN_REQUESTED" and ok["history_shortfall"]["covers_required_span"] is True and ok["readiness"]["research_ready"] is True


def test_no_history_at_all_is_a_total_shortfall(tmp_path):
    uni = make_universe(tmp_path)
    s = do_plan(uni)["history_shortfall"]
    assert s["short_by_days_max"] == NEED_DAYS and s["datasets"]["funding"]["cause"] == "DATASET_ABSENT"


def test_a_symbol_launched_inside_the_window_ends_on_an_empty_page_and_is_never_refetched(tmp_path):
    uni = make_universe(tmp_path, [inst("AAAUSDT"), inst("MIDUSDT", launch=W0 + 2 * DAY)])
    doc, pf = _sealed(tmp_path, uni)
    out = tmp_path / "new"
    assert _run(pf, out, uni, _venue_for_plan(uni, doc))["status"] == "DONE"
    ledger = (out / "funding" / "MIDUSDT" / "pages.jsonl").read_text()
    assert '"reason":"exhausted"' in ledger                                        # it stopped on an empty page, not at the window start
    v = _venue_for_plan(uni, doc)
    assert _run(pf, out, uni, v)["status"] == "DONE" and v.urls == []


def test_a_dataset_directory_this_plans_progress_file_does_not_own_is_refused_even_beside_its_own(tmp_path):
    uni = make_universe(tmp_path)
    doc, pf = _sealed(tmp_path, uni)
    out = tmp_path / "new"
    _run(pf, out, uni, _venue_for_plan(uni, doc))
    os.mkdir(out / "kline-240", 0o700)
    with pytest.raises(MH.HistoryError, match="does not own"):
        _run(pf, out, uni, Venue({}))


def test_resume_does_not_re_ask_a_symbol_that_already_ended_on_an_empty_page(tmp_path):
    uni = make_universe(tmp_path, [inst("AMIDUSDT", launch=W0 + 2 * DAY), inst("BBBUSDT")])
    doc, pf = _sealed(tmp_path, uni)
    out = tmp_path / "new"
    v1 = _venue_for_plan(uni, doc)
    with pytest.raises(KeyboardInterrupt):
        _run(pf, out, uni, v1, stop_after_requests=3)                               # AMID finished (a data page + the empty page), BBB interrupted after one request
    assert '"reason":"exhausted"' in (out / "funding" / "AMIDUSDT" / "pages.jsonl").read_text()
    v2 = _venue_for_plan(uni, doc)
    assert _run(pf, out, uni, v2)["status"] == "DONE"
    assert not any("symbol=AMIDUSDT" in u for u in v2.urls if MH.DATASETS[MH.FUNDING]["path"] in u) and not (set(v1.urls) & set(v2.urls))


# ---------------------------------------------------------------- red team: a subset resume must not forget the datasets already collected

def test_a_subset_rerun_keeps_the_other_datasets_in_the_progress_file(tmp_path):
    uni = make_universe(tmp_path)
    doc, pf = _sealed(tmp_path, uni)
    out = tmp_path / "new"
    assert _run(pf, out, uni, _venue_for_plan(uni, doc))["status"] == "DONE"                      # 1. full run
    v = _venue_for_plan(uni, doc)
    assert _run(pf, out, uni, v, datasets=["funding"])["status"] == "DONE" and v.urls == []        # 2. subset resume
    prog = json.loads((out / MH.PROGRESS_NAME).read_text())
    assert sorted(prog["datasets"]) == ["funding", "mark-kline"] and prog["datasets"]["mark-kline"]["status"] == "DONE"
    v3 = _venue_for_plan(uni, doc)
    assert _run(pf, out, uni, v3)["status"] == "DONE" and v3.urls == []                            # 3. full rerun: no "does not own" refusal, zero requests


def test_the_funnel_range_and_the_collection_range_use_one_delivery_rule_for_every_status():
    for status in ("Trading", "PendingOpen", "Closed", "Delivering"):
        rec = {"symbol": "XXXUSDT", "status": status, "launchTime": W0 - DAY, "deliveryTime": T - 2 * DAY}
        lo, hi = BP.symbol_range(rec, W0, T)
        spec = MH.build_specs(MH.FUNDING, None, ["XXXUSDT"], W0, T, {"XXXUSDT": rec}, {"XXXUSDT": 480})[0]
        assert hi == spec["end_ms"] == T - 2 * DAY, status                        # the same end whatever the status says
        assert lo == W0
    live = {"symbol": "XXXUSDT", "status": "Trading", "launchTime": W0 - DAY, "deliveryTime": 0}
    assert BP.symbol_range(live, W0, T)[1] == T == MH.build_specs(MH.FUNDING, None, ["XXXUSDT"], W0, T, {"XXXUSDT": live}, {"XXXUSDT": 480})[0]["end_ms"]
