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
