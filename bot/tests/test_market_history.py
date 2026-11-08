"""market_history: offline only. fetch, clock, sleep and the token-bucket clock are injected; a socket guard fails any real connection; every directory is a tmp dir."""
from __future__ import annotations

import ast
import json
import os
import socket
import stat
import sys
import threading

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import market_collector as MC  # noqa: E402
import market_history as MH  # noqa: E402
import universe_capture as UC  # noqa: E402
import venues  # noqa: E402

H8 = 8 * 3600 * 1000
BASE = (1_799_000_000_000 // H8) * H8                 # the newest funding settlement
N = 500
FIRST = BASE - (N - 1) * H8
START = FIRST - 100 * H8                              # the window opens long before the symbol's first row (it launched at FIRST - 1)
END = BASE + 1000
SERVER0 = END + 500


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("network touched")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "data"
    os.mkdir(r, 0o700)
    return r


class Time:
    """One fake clock for the bucket and the backoff: ``sleep`` advances it."""
    def __init__(self):
        self.t = 0.0
        self.sleeps = []
        self._l = threading.Lock()

    def mono(self):
        return self.t

    def sleep(self, s):
        with self._l:
            self.sleeps.append(s)
            self.t += s


class Clock:
    def __init__(self, t=SERVER0 + 50):
        self.t = t
        self._l = threading.Lock()

    def __call__(self):
        with self._l:
            self.t += 7
            return self.t


def row_for(dataset, sym, ts, **over):
    if dataset == MH.FUNDING:
        r = {"symbol": sym, "fundingRate": "0.0001", "fundingRateTimestamp": str(ts)}
        r.update(over)
        return r
    if dataset == MH.OI:
        r = {"openInterest": "10.5", "timestamp": str(ts)}
        r.update(over)
        return r
    if dataset == MH.KLINE:
        return over.get("row", [str(ts), "100", "101", "99", "100.5", "10", "1000"])
    return over.get("row", [str(ts), "100", "101", "99", "100.5"])


def envelope(dataset, sym, rows, server, **kw):
    d = {"retCode": 0, "retMsg": "OK", "result": {"category": "linear", "list": rows}, "time": server}
    if dataset != MH.FUNDING:
        d["result"]["symbol"] = sym
    d.update(kw)
    return json.dumps(d, indent=1).encode()


class HV:
    """Serves backward-paged history: ``data[(dataset, symbol)]`` = ascending timestamps. ``script[(dataset, symbol)]`` = items consumed first (bytes or an exception)."""
    PATHS = {MH.DATASETS[d]["path"]: d for d in MH.DATASETS}

    def __init__(self, data, server=SERVER0):
        self.data, self.server, self.script, self.urls = data, server, {}, []
        self.lock = threading.Lock()

    def __call__(self, url):
        base = venues.rest_for("mainnet")
        assert url.startswith(base)
        path, _, qs = url[len(base):].partition("?")
        q = dict(p.split("=", 1) for p in qs.split("&"))
        ds, sym = self.PATHS[path], q["symbol"]
        with self.lock:
            self.urls.append(url)
            sc = self.script.get((ds, sym))
            if sc:
                item = sc.pop(0)
                if isinstance(item, BaseException):
                    raise item
                if callable(item):
                    return item(q)
                return item
        spec = MH.DATASETS[ds]
        lo, hi, lim = int(q[spec["start"]]), int(q[spec["end"]]), int(q["limit"])
        assert lim <= spec["limit"] and spec["start"] in q and spec["end"] in q       # startTime is never sent without endTime
        ts = [t for t in self.data.get((ds, sym), []) if lo <= t <= hi]
        ts.sort(reverse=True)
        return envelope(ds, sym, [row_for(ds, sym, t) for t in ts[:lim]], self.server)


def grid(first, n, step):
    return [first + i * step for i in range(n)]


def funding_specs(syms=("BTCUSDT",), interval_min=480, launch=FIRST - 1, **kw):
    recs = {s: {"launchTime": launch, "fundingInterval_min": interval_min, "snapshot_ms": 1} for s in syms}
    return MH.build_specs(MH.FUNDING, None, list(syms), START, END, recs, **kw)


def run(root, specs, venue, dataset=MH.FUNDING, interval=None, **kw):
    t = kw.pop("time", None) or Time()
    kw.setdefault("clock", Clock())
    kw.setdefault("free_bytes_fn", lambda _p: 1 << 40)
    kw.setdefault("reserve_bytes", 1)
    return MH.run_dataset(str(root), dataset, interval, specs, fetch=venue, sleep=t.sleep, mono=t.mono, **kw)


def ddir(root, dataset="funding", interval=None):
    return os.path.join(str(root), MH.dataset_dirname(dataset, interval))


def sym_of(man, s="BTCUSDT"):
    return man["symbols"][s]


def funding_venue(syms=("BTCUSDT",), n=N, step=H8):
    return HV({(MH.FUNDING, s): grid(BASE - (n - 1) * step, n, step) for s in syms})


# ---------------------------------------------------------------- request building (exact official parameters)

def test_request_urls_use_the_documented_parameters_and_always_send_both_times():
    base = venues.rest_for("mainnet")
    assert MH.request_url("funding", "BTCUSDT", None, 10, 20) == f"{base}/v5/market/funding/history?category=linear&symbol=BTCUSDT&startTime=10&endTime=20&limit=200"
    assert MH.request_url("kline", "BTCUSDT", "60", 10, 20) == f"{base}/v5/market/kline?category=linear&symbol=BTCUSDT&interval=60&start=10&end=20&limit=1000"
    assert MH.request_url("mark-kline", "ETHUSDT", "D", 10, 20) == f"{base}/v5/market/mark-price-kline?category=linear&symbol=ETHUSDT&interval=D&start=10&end=20&limit=1000"
    assert MH.request_url("index-kline", "ETHUSDT", "5", 10, 20, 7) == f"{base}/v5/market/index-price-kline?category=linear&symbol=ETHUSDT&interval=5&start=10&end=20&limit=7"
    assert MH.request_url("open-interest", "BTCUSDT", "5min", 10, 20) == f"{base}/v5/market/open-interest?category=linear&symbol=BTCUSDT&intervalTime=5min&startTime=10&endTime=20&limit=200"


@pytest.mark.parametrize("args", [
    ("funding", "btcusdt", None, 10, 20), ("funding", "BTC&x=1", None, 10, 20), ("funding", "BTCUSDT", None, 30, 20), ("funding", "BTCUSDT", None, 0, 20),
    ("funding", "BTCUSDT", None, True, 20), ("nope", "BTCUSDT", None, 10, 20), ("kline", "BTCUSDT", "M", 10, 20), ("kline", "BTCUSDT", None, 10, 20),
    ("kline", "BTCUSDT", "61", 10, 20), ("open-interest", "BTCUSDT", "1min", 10, 20), ("open-interest", "BTCUSDT", None, 10, 20), ("funding", "BTCUSDT", None, 10, 10 ** 15),
])
def test_request_url_refuses_what_is_not_pinned(args):
    with pytest.raises(MH.HistoryError):
        MH.request_url(*args)


@pytest.mark.parametrize("ds,lim", [("funding", 201), ("kline", 1001), ("mark-kline", 1001), ("index-kline", 1001), ("open-interest", 201), ("funding", 0)])
def test_limits_are_the_documented_maxima(ds, lim):
    with pytest.raises(MH.HistoryError):
        MH.request_url(ds, "BTCUSDT", "60" if "kline" in ds or ds == "kline" else ("5min" if ds == "open-interest" else None), 10, 20, lim)


def test_the_network_primitive_refuses_a_path_outside_the_history_set_and_an_order_path():
    base = venues.rest_for("mainnet")
    for bad in (f"{base}/v5/market/tickers?category=linear", f"{base}/v5/order/create?x=1", f"{base}/v5/account/wallet-balance?x=1", f"{base}/v5/market/kline",
                "https://evil.example/v5/market/kline?category=linear"):
        with pytest.raises(MC.CollectorError):
            MH.http_fetch(bad)


# ---------------------------------------------------------------- happy paths, raw bytes, permissions, verify

def test_funding_backfill_is_complete_with_raw_pages_preserved_exclusively_at_0600(root):
    v = funding_venue()
    man = run(root, funding_specs(), v)
    s = sym_of(man)
    assert man["dataset_status"] == "COMPLETE" and s["status"] == "COMPLETE" and s["reasons"] == []
    cov = s["coverage"]
    assert (cov["rows"], cov["expected"], cov["missing"]) == (N, N, 0)
    assert cov["first_ts"] == FIRST and cov["last_ts"] == BASE and cov["interval_ms"] == H8 and cov["interval_source"].startswith("universe_record:fundingInterval_min")
    assert s["stop_reason"] == "exhausted" and s["pages"] == 4          # 200 + 200 + 100 rows, then the EMPTY page that proves nothing older exists
    sdir = os.path.join(ddir(root), "BTCUSDT")
    for n in range(1, 5):
        p = os.path.join(sdir, f"page-{n:06d}.json")
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    led = [json.loads(x) for x in open(os.path.join(sdir, "pages.jsonl"))]
    pages = [r for r in led if r["type"] == "page"]
    assert [r["raw_rows"] for r in pages] == [200, 200, 100, 0]
    for r in pages:                                                      # bytes exactly as served (indent=1 is non-canonical on purpose), params + times recorded
        raw = open(os.path.join(sdir, r["file"]), "rb").read()
        assert MH._sha(raw) == r["sha256"] and raw.startswith(b'{\n "retCode"')
        assert r["request"]["params"]["startTime"] == str(START) and r["request"]["params"]["limit"] == "200"
        assert r["server_time"] == SERVER0 and r["local_receive_time" if False else "recv_ms"] > SERVER0 and r["collection_ms"] >= r["recv_ms"]
    ends = [int(r["request"]["params"]["endTime"]) for r in pages]
    assert ends[0] == END and all(a > b for a, b in zip(ends, ends[1:]))   # strictly backwards
    assert ends[1] == pages[0]["min_ts"] - 1
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700 and stat.S_IMODE(os.stat(ddir(root)).st_mode) == 0o700 and stat.S_IMODE(os.stat(sdir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(os.path.join(ddir(root), "manifest.json")).st_mode) == 0o600
    assert MH.verify_dataset(ddir(root))["ok"]


def test_a_captured_page_is_never_overwritten(root):
    run(root, funding_specs(), funding_venue())
    sdir = os.path.join(ddir(root), "BTCUSDT")
    with pytest.raises(UC.CaptureError):
        UC._create_exclusive(os.path.join(sdir, "page-000001.json"), b"x")


@pytest.mark.parametrize("dataset,interval,step", [("kline", "60", 3_600_000), ("mark-kline", "15", 900_000), ("index-kline", "D", 86_400_000), ("open-interest", "1h", 3_600_000)])
def test_every_dataset_pages_backward_to_complete_and_verifies(root, dataset, interval, step):
    first, n = (BASE // step) * step - 1499 * step, 1500
    v = HV({(dataset, "BTCUSDT"): grid(first, n, step)})
    spec = MH.build_specs(dataset, interval, ["BTCUSDT"], first - 10 * step, first + (n - 1) * step + 10, {"BTCUSDT": {"launchTime": first - 1}})
    man = run(root, spec, v, dataset, interval)
    s = sym_of(man)
    assert s["status"] == "COMPLETE", s
    assert s["coverage"]["rows"] == n and s["coverage"]["missing"] == 0 and s["coverage"]["interval_ms"] == step
    assert s["coverage"]["interval_source"] == "declared_interval_grid"
    assert MH.verify_dataset(ddir(root, dataset, interval))["ok"]
    rows = [json.loads(x) for x in open(os.path.join(ddir(root, dataset, interval), "BTCUSDT", "rows.jsonl"))]
    assert [r["ts"] for r in rows] == sorted(r["ts"] for r in rows)
    assert rows[0]["observation_time"] == (rows[0]["ts"] + step if "kline" in dataset else rows[0]["ts"])


def test_a_kline_with_inconsistent_ohlc_or_a_bad_decimal_is_rejected_and_counted(root):
    first, step = BASE - 50 * 60_000, 60_000
    v = HV({("kline", "BTCUSDT"): grid(first, 50, step)})
    bad = [str(first + 5 * step), "100", "99", "101", "100", "1", "1"]            # high < low
    v.script[("kline", "BTCUSDT")] = [envelope("kline", "BTCUSDT", [bad, ["x", "1", "1", "1", "1", "1", "1"], [str(first), "NaN", "1", "1", "1", "1", "1"]], SERVER0)]
    spec = MH.build_specs("kline", "1", ["BTCUSDT"], first, first + 49 * step, {"BTCUSDT": {"launchTime": first - 1}})
    man = run(root, spec, v, "kline", "1")
    s = sym_of(man)
    assert s["status"] == "PARTIAL" and "rejects" in s["reasons"]
    assert s["rejects"]["ohlc_inconsistent"] == 1 and s["rejects"]["bad_row"] == 2


# ---------------------------------------------------------------- the seven timestamps

def _times(**over):
    t = {"exchange_event_time": 1000, "source_server_time": 5000, "local_receive_time": 5010, "collection_time": 5020, "normalization_time": 6000, "observation_time": 1000}
    t.update(over)
    return t


def test_time_invariants_accept_a_consistent_set_and_name_every_violation():
    assert MH.check_time_invariants(_times()) == []
    assert MH.check_time_invariants(_times(decision_time=6000)) == []
    assert MH.check_time_invariants(_times(observation_time=999))                                   # observation before the event
    assert MH.check_time_invariants(_times(collection_time=5000))                                   # collected before received
    assert MH.check_time_invariants(_times(normalization_time=5019))                                # normalized before collected
    assert MH.check_time_invariants(_times(source_server_time=1000 - MH.SKEW_WARN_MS - 1))          # a row from the future of the server's clock
    assert MH.check_time_invariants(_times(decision_time=999))                                      # look-ahead: before observation
    assert MH.check_time_invariants(_times(decision_time=5009))                                     # before local receipt
    assert MH.check_time_invariants({k: v for k, v in _times().items() if k != "collection_time"})
    assert MH.check_time_invariants(_times(local_receive_time=True))
    assert set(MH.TIME_FIELDS) == {"exchange_event_time", "source_server_time", "local_receive_time", "collection_time", "normalization_time", "observation_time", "decision_time"}


def test_usable_at_never_lets_a_decision_precede_observation_or_receipt():
    t = _times(observation_time=2000, local_receive_time=5010)
    assert not MH.usable_at(t, 1999) and not MH.usable_at(t, 2000) and not MH.usable_at(t, 5009)       # live: needs BOTH
    assert MH.usable_at(t, 5010) and MH.usable_at(t, 9999)
    assert MH.usable_at(t, 2000, "replay") and not MH.usable_at(t, 1999, "replay")                       # replay relaxes only the receipt rule
    with pytest.raises(MH.HistoryError):
        MH.usable_at(t, 9999, "anything")
    assert not MH.usable_at({"observation_time": 2000}, 9999)                                            # unknown receipt: refused in live mode


def test_the_stored_times_of_every_row_satisfy_the_ordering_invariants(root):
    step = 3_600_000
    first = (BASE // step) * step - 99 * step
    v = HV({("kline", "BTCUSDT"): grid(first, 100, step)})
    spec = MH.build_specs("kline", "60", ["BTCUSDT"], first, first + 99 * step + 10, {"BTCUSDT": {"launchTime": first - 1}})
    man = run(root, spec, v, "kline", "60")
    s = sym_of(man)
    sdir = os.path.join(ddir(root, "kline", "60"), "BTCUSDT")
    pages = {r["n"]: r for r in map(json.loads, open(os.path.join(sdir, "pages.jsonl"))) if r["type"] == "page"}
    for line in open(os.path.join(sdir, "rows.jsonl")):
        r = json.loads(line)
        p = pages[r["page"]]
        t = {"exchange_event_time": r["ts"], "source_server_time": p["server_time"], "local_receive_time": p["recv_ms"], "collection_time": p["collection_ms"],
             "normalization_time": s["normalized_ms"], "observation_time": r["observation_time"]}
        assert MH.check_time_invariants(t) == [], (t, r)
        assert r["observation_time"] == r["ts"] + step                                                  # a candle is final only at its close


def test_a_row_stamped_after_the_servers_own_clock_is_rejected_not_kept(root):
    v = funding_venue(n=10)
    future = BASE + 10 * H8
    v.script[("funding", "BTCUSDT")] = [envelope("funding", "BTCUSDT", [row_for("funding", "BTCUSDT", future)] + [row_for("funding", "BTCUSDT", t) for t in grid(BASE - 9 * H8, 10, H8)], SERVER0)]
    spec = funding_specs(launch=BASE - 9 * H8 - 1)
    man = run(root, spec, v)
    assert sym_of(man)["rejects"].get("out_of_window", 0) + sym_of(man)["rejects"].get("row_in_future_of_server_time", 0) >= 1
    assert sym_of(man)["status"] == "PARTIAL"


# ---------------------------------------------------------------- gaps are reported, never filled; the grid is per symbol and never defaulted

def test_a_missing_settlement_is_reported_as_a_gap_and_never_filled(root):
    ts = grid(FIRST, N, H8)
    del ts[250]
    man = run(root, funding_specs(), HV({("funding", "BTCUSDT"): ts}))
    s = sym_of(man)
    assert s["status"] == "PARTIAL" and "gaps" in s["reasons"]
    assert s["coverage"]["missing"] == 1 and s["coverage"]["rows"] == N - 1 and s["coverage"]["expected"] == N
    assert s["gaps"] == [{"after_ts": FIRST + 249 * H8, "next_ts": FIRST + 251 * H8, "missing": 1}]
    rows = [json.loads(x)["ts"] for x in open(os.path.join(ddir(root), "BTCUSDT", "rows.jsonl"))]
    assert FIRST + 250 * H8 not in rows and len(rows) == N - 1                  # no row invented


def test_the_funding_grid_is_each_symbols_own_interval_from_the_universe_record(root):
    ts4 = grid(BASE - 399 * 4 * 3_600_000, 400, 4 * 3_600_000)
    ts8 = grid(BASE - 399 * H8, 400, H8)
    v = HV({("funding", "AAAUSDT"): ts4, ("funding", "BBBUSDT"): ts8})
    recs = {"AAAUSDT": {"launchTime": ts4[0] - 1, "fundingInterval_min": 240, "snapshot_ms": 1}, "BBBUSDT": {"launchTime": ts8[0] - 1, "fundingInterval_min": 480, "snapshot_ms": 1}}
    man = run(root, MH.build_specs("funding", None, ["AAAUSDT", "BBBUSDT"], START, END, recs), v)
    assert sym_of(man, "AAAUSDT")["coverage"]["interval_ms"] == 4 * 3_600_000 and sym_of(man, "AAAUSDT")["status"] == "COMPLETE"
    assert sym_of(man, "BBBUSDT")["coverage"]["interval_ms"] == H8 and sym_of(man, "BBBUSDT")["status"] == "COMPLETE"
    # 8h data under a record that says 240 min: the data is NOT judged gap-by-gap on the declared grid; the disagreement itself is reported
    r2 = os.path.join(os.path.dirname(str(root)), "data_b")
    os.mkdir(r2, 0o700)
    man2 = run(r2, MH.build_specs("funding", None, ["BBBUSDT"], START, END, {"BBBUSDT": dict(recs["BBBUSDT"], fundingInterval_min=240)}), v)
    s2 = sym_of(man2, "BBBUSDT")
    assert s2["status"] == "PARTIAL" and s2["reasons"] == ["interval_disagrees_with_record"] and s2["gap_count"] == 0 and s2["coverage"]["observed_interval_ms"] == H8


def test_an_unknown_funding_interval_refuses_the_grid_check_and_is_never_defaulted_to_480(root):
    specs = MH.build_specs("funding", None, ["BTCUSDT"], START, END)                 # no universe record, no declaration
    assert specs[0]["interval_ms"] is None and specs[0]["interval_source"] == "UNKNOWN"
    man = run(root, specs, funding_venue())
    s = sym_of(man)
    assert s["status"] == "UNVERIFIED" and s["reasons"][0] == "grid_unknown_interval" and s["grid_check"] == "REFUSED_UNKNOWN_INTERVAL"
    assert s["coverage"]["interval_ms"] is None and s["coverage"]["expected"] is None and s["coverage"]["missing"] is None and s["gaps"] == []
    assert man["dataset_status"] == "UNVERIFIED"
    assert s["coverage"]["rows"] == N                                               # the rows are kept; only the completeness claim is refused


def test_an_operator_declared_interval_is_recorded_as_such(root):
    specs = MH.build_specs("funding", None, ["BTCUSDT"], START, END, {"BTCUSDT": {"launchTime": FIRST - 1}}, {"BTCUSDT": 480})
    assert specs[0]["interval_source"] == "operator_declared"
    assert sym_of(run(root, specs, funding_venue()))["status"] == "COMPLETE"


H4, H1 = 4 * 3_600_000, 3_600_000


def test_a_funding_interval_change_inside_the_history_is_a_regime_change_never_a_gap(root):
    ts = grid(FIRST, 200, H8)
    ts += grid(ts[-1] + H4, 200, H4)                         # 480 -> 240
    ts += grid(ts[-1] + H1, 150, H1)                         # 240 -> 60 (a dynamically shortened contract)
    last = ts[-1]
    specs = MH.build_specs("funding", None, ["BTCUSDT"], START, last + 1000, {"BTCUSDT": {"launchTime": FIRST - 1, "fundingInterval_min": 60, "snapshot_ms": 1}})
    man = run(root, specs, HV({("funding", "BTCUSDT"): ts}))
    s = sym_of(man)
    assert s["status"] == "COMPLETE" and s["reasons"] == [] and s["gap_count"] == 0 and s["off_grid_count"] == 0 and s["coverage"]["missing"] == 0
    assert [(g["fundingInterval_min"], g["settlements_per_day"], g["source"]) for g in s["segments"]] == [(480, 3.0, "inferred_from_print_spacing"),
                                                                                                       (240, 6.0, "inferred_from_print_spacing"), (60, 24.0, "inferred_from_print_spacing")]
    assert [(c["from_interval_ms"], c["to_interval_ms"]) for c in s["regime_changes"]] == [(H8, H4), (H4, H1)]
    assert s["regime_changes"][0]["at_ts"] == s["segments"][1]["first_ts"]
    assert s["coverage"]["rows"] == len(ts) and s["coverage"]["expected"] == len(ts)
    assert MH.verify_dataset(ddir(root))["ok"]


def test_a_longer_interval_after_a_shorter_one_is_a_regime_but_a_lone_long_step_is_a_gap(root):
    ts = grid(FIRST, 100, H4) + grid(FIRST + 99 * H4 + H8, 100, H8)                  # 240 -> 480 sustained
    specs = MH.build_specs("funding", None, ["BTCUSDT"], START, ts[-1] + 1000, {"BTCUSDT": {"launchTime": FIRST - 1, "fundingInterval_min": 480, "snapshot_ms": 1}})
    s = sym_of(run(root, specs, HV({("funding", "BTCUSDT"): ts})))
    assert s["status"] == "COMPLETE" and len(s["segments"]) == 2 and s["gap_count"] == 0
    ts2 = grid(FIRST, 100, H4)
    del ts2[50]                                                                       # ONE missing print inside a steady 240 regime
    r2 = os.path.join(os.path.dirname(str(root)), "data_c")
    os.mkdir(r2, 0o700)
    specs = MH.build_specs("funding", None, ["BTCUSDT"], START, ts2[-1] + 1000, {"BTCUSDT": {"launchTime": FIRST - 1, "fundingInterval_min": 240, "snapshot_ms": 1}})
    s2 = sym_of(run(r2, specs, HV({("funding", "BTCUSDT"): ts2})))
    assert s2["status"] == "PARTIAL" and s2["gap_count"] == 1 and s2["coverage"]["missing"] == 1 and len(s2["segments"]) == 1 and s2["regime_changes"] == []


def test_a_lone_off_grid_step_is_not_a_regime_and_is_never_filled(root):
    ts = grid(FIRST, 100, H8) + [FIRST + 99 * H8 + H4]
    s = sym_of(run(root, funding_specs(), HV({("funding", "BTCUSDT"): ts})))
    assert s["off_grid_count"] == 1 and "off_grid_step" in s["reasons"] and s["status"] == "PARTIAL" and s["regime_changes"] == []
    assert s["coverage"]["rows"] == 101


def test_the_current_record_interval_must_match_the_last_observed_regime(root):
    ts = grid(FIRST, 200, H8)
    s = sym_of(run(root, funding_specs(interval_min=240), HV({("funding", "BTCUSDT"): ts})))
    assert s["status"] == "PARTIAL" and "interval_disagrees_with_record" in s["reasons"]


def test_a_closed_contracts_history_ends_at_its_delivery_time_and_later_time_is_not_a_gap_or_stale(root):
    delivery = BASE - 60 * H8
    ts = grid(FIRST, N - 60, H8)                                                     # the last print is at the delisting
    assert ts[-1] == delivery
    rec = {"launchTime": FIRST - 1, "deliveryTime": delivery + 1, "fundingInterval_min": 480, "snapshot_ms": 1}
    specs = MH.build_specs("funding", None, ["CLOSEDUSDT"], START, END, {"CLOSEDUSDT": rec})
    s = sym_of(run(root, specs, HV({("funding", "CLOSEDUSDT"): ts})), "CLOSEDUSDT")
    assert s["status"] == "COMPLETE" and s["coverage"]["delivery_ms"] == delivery + 1 and s["coverage"]["tail_lag_ms"] < H8
    r2 = os.path.join(os.path.dirname(str(root)), "data_d")
    os.mkdir(r2, 0o700)
    live = MH.build_specs("funding", None, ["CLOSEDUSDT"], START, END, {"CLOSEDUSDT": dict(rec, deliveryTime=0)})            # the same data on a live contract IS stale
    assert sym_of(run(r2, live, HV({("funding", "CLOSEDUSDT"): ts})), "CLOSEDUSDT")["status"] == "STALE"


def test_a_contract_delisted_before_the_window_is_excluded_and_listed_not_counted_as_missing(root):
    rec = {"launchTime": FIRST - 1, "deliveryTime": START - 1000, "fundingInterval_min": 480, "snapshot_ms": 1}
    specs = MH.build_specs("funding", None, ["OLDUSDT", "BTCUSDT"], START, END, {"OLDUSDT": rec, "BTCUSDT": dict(rec, deliveryTime=0)})
    man = run(root, specs, funding_venue())
    assert man["excluded"] == {"OLDUSDT": "delisted_before_window"} and list(man["symbols"]) == ["BTCUSDT"] and man["dataset_status"] == "COMPLETE"


def test_a_percent_encoded_cursor_is_passed_through_unchanged_never_double_encoded():
    base = venues.rest_for("mainnet")
    c = "first%3D10000000AIDOGEUSDT%2Csecond%3D2"
    u = MH.request_url("open-interest", "BTCUSDT", "5min", 10, 20, cursor=c)
    assert u.endswith("&cursor=" + c) and "%253D" not in u and "%252C" not in u
    assert u.startswith(f"{base}/v5/market/open-interest?category=linear&symbol=BTCUSDT&intervalTime=5min&startTime=10&endTime=20&limit=200&cursor=")
    for bad in ("a&b", "a b", "x#y", "", "a\nb", "é"):
        if bad:
            with pytest.raises(MH.HistoryError):
                MH.request_url("open-interest", "BTCUSDT", "5min", 10, 20, cursor=bad)
    with pytest.raises(MH.HistoryError):
        MH.request_url("funding", "BTCUSDT", None, 10, 20, cursor=c)                       # only open-interest takes a cursor
    # the role and the primitive accept the verbatim URL (a '%' in a query is not a credential)
    seen = []

    class Spy:
        def request(self, method, url, **kw):
            seen.append(url)
            return b"{}"
    real = MC._UrllibTransport
    MC._UrllibTransport = Spy
    try:
        MH.http_fetch(u)
    finally:
        MC._UrllibTransport = real
    assert seen == [u]


def test_the_pages_next_cursor_is_recorded_verbatim_in_the_ledger(root):
    step = H1
    first = (BASE // step) * step - 9 * step
    v = HV({("open-interest", "BTCUSDT"): grid(first, 10, step)})
    c = "first%3DAB%2Csecond%3D1"
    real = v.__call__

    def with_cursor(url):
        d = json.loads(real(url))
        d["result"]["nextPageCursor"] = c
        return json.dumps(d).encode()
    spec = MH.build_specs("open-interest", "1h", ["BTCUSDT"], first, first + 9 * step + 5, {"BTCUSDT": {"launchTime": first - 1}})
    run(root, spec, with_cursor, "open-interest", "1h")
    led = [json.loads(x) for x in open(os.path.join(ddir(root, "open-interest", "1h"), "BTCUSDT", "pages.jsonl"))]
    assert {r["next_page_cursor"] for r in led if r["type"] == "page"} == {c}


def test_stale_data_is_stale_not_complete(root):
    last = BASE - 50 * H8
    v = HV({("funding", "BTCUSDT"): grid(FIRST, N - 50, H8)})
    man = run(root, funding_specs(), v)
    s = sym_of(man)
    assert s["coverage"]["last_ts"] == last and s["status"] == "STALE" and s["reasons"] == ["tail_older_than_two_intervals"]
    assert man["dataset_status"] == "STALE"


def test_a_head_that_cannot_be_proved_without_a_launch_time_is_partial(root):
    specs = MH.build_specs("funding", None, ["BTCUSDT"], START, END, {"BTCUSDT": {"fundingInterval_min": 480, "snapshot_ms": 1}})     # no launchTime in the record
    s = sym_of(run(root, specs, funding_venue()))
    assert s["status"] == "PARTIAL" and "head_unverified_no_launch_time" in s["reasons"]


def test_a_head_gap_after_the_launch_is_missing_data(root):
    ts = grid(FIRST + 5 * H8, N - 5, H8)                                            # five settlements after the launch are absent
    s = sym_of(run(root, funding_specs(), HV({("funding", "BTCUSDT"): ts})))
    assert s["status"] == "PARTIAL" and "head_gap" in s["reasons"] and s["coverage"]["head_missing"] == 5


def test_no_rows_at_all_is_partial_never_complete(root):
    s = sym_of(run(root, funding_specs(), HV({})))
    assert s["status"] == "PARTIAL" and "no_rows" in s["reasons"] and s["coverage"]["rows"] == 0


# ---------------------------------------------------------------- duplicates

def test_identical_duplicate_rows_are_counted_and_dropped_and_an_overlapping_page_row_is_out_of_window(root):
    v = funding_venue(n=30)
    dup = row_for("funding", "BTCUSDT", BASE - 3 * H8)
    rows = [row_for("funding", "BTCUSDT", t) for t in sorted(v.data[("funding", "BTCUSDT")], reverse=True)]
    v.script[("funding", "BTCUSDT")] = [envelope("funding", "BTCUSDT", rows + [dup], SERVER0)]
    s = sym_of(run(root, funding_specs(launch=BASE - 29 * H8 - 1), v))
    assert s["duplicates_identical"] == 1 and s["coverage"]["rows"] == 30 and s["conflicts"] == 0 and s["status"] == "COMPLETE"
    # a row a later page repeats from an EARLIER page lies above that page's endTime: counted as a reject (stricter than a dedup), never double counted
    v2 = funding_venue(n=300)
    first_page = lambda q: envelope("funding", "BTCUSDT", [row_for("funding", "BTCUSDT", t) for t in sorted(v2.data[("funding", "BTCUSDT")], reverse=True)[:200]], SERVER0)  # noqa: E731
    second = lambda q: envelope("funding", "BTCUSDT", [row_for("funding", "BTCUSDT", BASE)] + [row_for("funding", "BTCUSDT", t) for t in sorted(v2.data[("funding", "BTCUSDT")], reverse=True)[200:]], SERVER0)  # noqa: E731
    v2.script[("funding", "BTCUSDT")] = [first_page, second]
    r2 = os.path.join(os.path.dirname(str(root)), "data_e")
    os.mkdir(r2, 0o700)
    s2 = sym_of(run(r2, funding_specs(launch=BASE - 299 * H8 - 1), v2))
    assert s2["coverage"]["rows"] == 300 and s2["rejects"]["out_of_window"] == 1 and s2["status"] == "PARTIAL"


def test_a_conflicting_duplicate_makes_the_dataset_conflicted_and_nothing_is_merged(root):
    v = funding_venue(n=30)
    t = BASE - 3 * H8
    v.script[("funding", "BTCUSDT")] = [envelope("funding", "BTCUSDT", [row_for("funding", "BTCUSDT", BASE), row_for("funding", "BTCUSDT", t), row_for("funding", "BTCUSDT", t, fundingRate="0.0009")], SERVER0)]
    man = run(root, funding_specs(launch=BASE - 29 * H8 - 1), v)
    s = sym_of(man)
    assert s["status"] == "CONFLICTED" and man["dataset_status"] == "CONFLICTED" and s["conflicts"] == 1 and s["reasons"][0] == "conflicting_duplicate"
    rows = [json.loads(x) for x in open(os.path.join(ddir(root), "BTCUSDT", "rows.jsonl"))]
    assert [r for r in rows if r["ts"] == t][0]["v"]["fundingRate"] == "0.0001"          # the first row stays; the conflicting one is not averaged in


# ---------------------------------------------------------------- interrupted pagination, resume, quarantine

class Crash(RuntimeError):
    pass


def test_a_crash_mid_symbol_resumes_from_the_last_verified_page_without_refetching_or_double_counting(root):
    v = funding_venue()
    calls = {"n": 0}
    real = v.__call__

    def flaky(url):
        calls["n"] += 1
        if calls["n"] == 3:
            raise Crash("power cut")
        return real(url)
    with pytest.raises(Crash):
        run(root, funding_specs(), flaky)
    urls_before = list(v.urls)
    assert len(urls_before) == 2
    sdir = os.path.join(ddir(root), "BTCUSDT")
    assert sorted(f for f in os.listdir(sdir) if f.startswith("page-")) == ["page-000001.json", "page-000002.json"]
    man = run(root, funding_specs(), v)
    new_urls = v.urls[len(urls_before):]
    assert not set(new_urls) & set(urls_before)                                      # a verified page is never fetched again
    s = sym_of(man)
    assert s["status"] == "COMPLETE" and s["coverage"]["rows"] == N and s["duplicates_identical"] == 0 and s["run"]["pages_resumed"] == 2
    assert MH.verify_dataset(ddir(root))["ok"]


def test_a_torn_ledger_tail_and_an_orphan_page_are_quarantined_and_the_rows_counted_once(root):
    v = funding_venue()
    run(root, funding_specs(), v, max_pages=2)
    sdir = os.path.join(ddir(root), "BTCUSDT")
    with open(os.path.join(sdir, "pages.jsonl"), "ab") as fh:
        fh.write(b'{"type":"page","n":3,"fi')                                         # a crash mid-line
    open(os.path.join(sdir, "page-000003.json"), "wb").write(b"{}")                  # the page file written just before the ledger row
    os.chmod(os.path.join(sdir, "page-000003.json"), 0o600)
    man = run(root, funding_specs(), v)
    s = sym_of(man)
    assert s["status"] == "COMPLETE" and s["coverage"]["rows"] == N
    assert os.path.isdir(os.path.join(sdir, "quarantine", "1")) and s["run"]["quarantined"] == ["quarantine/1"]
    assert os.path.exists(os.path.join(sdir, "quarantine", "1", "page-000003.json"))
    r = MH.verify_dataset(ddir(root))
    assert r["ok"], r


def test_a_corrupt_page_is_quarantined_and_refetched_from_the_last_verified_one(root):
    v = funding_venue()
    run(root, funding_specs(), v)
    sdir = os.path.join(ddir(root), "BTCUSDT")
    p = os.path.join(sdir, "page-000002.json")
    data = bytearray(open(p, "rb").read())
    data[40] ^= 0x01
    open(p, "wb").write(bytes(data))
    bad = MH.verify_dataset(ddir(root))
    assert not bad["ok"] and any("PAGE_HASH_MISMATCH" in x for x in bad["problems"])
    before = len(v.urls)
    man = run(root, funding_specs(), v)
    assert sym_of(man)["status"] == "COMPLETE" and sym_of(man)["coverage"]["rows"] == N
    assert len(v.urls) - before == 3                                                  # pages 2..4 again (page 1 verified, kept); rows are not double counted
    assert os.path.exists(os.path.join(sdir, "quarantine", "1", "page-000002.json"))
    assert MH.verify_dataset(ddir(root))["ok"]


def test_a_missing_page_file_is_detected_by_verify_and_repaired_by_resume(root):
    v = funding_venue()
    run(root, funding_specs(), v)
    sdir = os.path.join(ddir(root), "BTCUSDT")
    os.remove(os.path.join(sdir, "page-000003.json"))
    assert any("PAGE_MISSING" in x for x in MH.verify_dataset(ddir(root))["problems"])
    man = run(root, funding_specs(), v)
    assert sym_of(man)["status"] == "COMPLETE" and MH.verify_dataset(ddir(root))["ok"]


def test_resume_with_a_different_window_is_refused_not_mixed(root):
    v = funding_venue()
    run(root, funding_specs(), v)
    other = MH.build_specs("funding", None, ["BTCUSDT"], START + 1, END, {"BTCUSDT": {"launchTime": FIRST - 1, "fundingInterval_min": 480}})
    man = run(root, other, v)
    s = sym_of(man)
    assert s["status"] == "UNVERIFIED" and "PARAMS_MISMATCH" in json.dumps(s["run"]["errors"])


def test_page_cap_stops_a_symbol_and_the_next_run_continues(root):
    v = funding_venue()
    man = run(root, funding_specs(), v, max_pages=1)
    s = sym_of(man)
    assert s["status"] == "PARTIAL" and "not_terminated" in s["reasons"] and s["pages"] == 1 and any("page_cap" in e for e in s["run"]["errors"])
    man2 = run(root, funding_specs(), v)
    assert sym_of(man2)["status"] == "COMPLETE" and len(set(v.urls)) == len(v.urls)


def test_a_cursor_that_does_not_move_backwards_stops_the_symbol(root):
    v = funding_venue()
    newest = [row_for("funding", "BTCUSDT", t) for t in (BASE, BASE - H8)]
    v.script[("funding", "BTCUSDT")] = [envelope("funding", "BTCUSDT", newest, SERVER0)] * 1
    man = run(root, funding_specs(), HV(v.data) if False else v)
    # the first page is honest (moves the cursor); now make every page repeat the same newest rows: the cursor cannot move below endTime
    v2 = funding_venue()
    stuck = lambda q: envelope("funding", "BTCUSDT", [row_for("funding", "BTCUSDT", BASE + 5 * H8)], SERVER0 + 10 * H8)  # noqa: E731
    v2.script[("funding", "BTCUSDT")] = [stuck]
    root2 = os.path.join(os.path.dirname(str(root)), "data2")
    os.mkdir(root2, 0o700)
    man2 = run(root2, funding_specs(), v2)
    s = sym_of(man2)
    assert s["status"] == "PARTIAL" and any("cursor_stalled" in e for e in s["run"]["errors"]) and len(v2.urls) == 1
    assert s["pages_failed"] == 1 and s["coverage"]["rows"] == 0


def test_an_envelope_error_page_is_kept_as_evidence_and_stops_the_symbol(root):
    v = funding_venue()
    v.script[("funding", "BTCUSDT")] = [json.dumps({"retCode": 10001, "retMsg": "params error", "result": {}, "time": SERVER0}).encode()]
    s = sym_of(run(root, funding_specs(), v))
    assert s["status"] == "PARTIAL" and s["pages_failed"] == 1 and any("retCode_10001" in e for e in s["run"]["errors"])
    s2 = sym_of(run(root, funding_specs(), v))                                       # the next run resumes and finishes; the failed page does not poison the status
    assert s2["status"] == "COMPLETE" and s2["rejects"] == {}


# ---------------------------------------------------------------- manifest / hash mismatch

def test_verify_detects_a_tampered_manifest_rows_file_ledger_and_strays(root):
    run(root, funding_specs(), funding_venue())
    d = ddir(root)
    mp = os.path.join(d, "manifest.json")
    orig = open(mp, "rb").read()
    assert MH.verify_dataset(d)["ok"]
    doc = json.loads(orig)
    doc["symbols"]["BTCUSDT"]["coverage"]["rows"] += 1
    open(mp, "w").write(json.dumps(doc))
    r = MH.verify_dataset(d)
    assert not r["ok"] and any("manifest_digest" in x for x in r["problems"])
    doc["manifest_digest"] = MH._sha(MH._canon({k: v for k, v in doc.items() if k != "manifest_digest"}))                     # an attacker re-seals the digest
    open(mp, "w").write(json.dumps(doc))
    r = MH.verify_dataset(d)
    assert not r["ok"] and any("coverage differs" in x for x in r["problems"])                                                   # ... but the re-derivation from raw pages disagrees
    open(mp, "wb").write(orig)
    rp = os.path.join(d, "BTCUSDT", "rows.jsonl")
    keep = open(rp, "rb").read()
    open(rp, "ab").write(b"\n")
    assert any("rows.jsonl hash differs" in x for x in MH.verify_dataset(d)["problems"])
    open(rp, "wb").write(keep)
    lp = os.path.join(d, "BTCUSDT", "pages.jsonl")
    lkeep = open(lp, "rb").read()
    open(lp, "ab").write(json.dumps({"type": "stop", "reason": "reached_start"}).encode() + b"\n")
    assert any("ledger_sha256" in x for x in MH.verify_dataset(d)["problems"])
    open(lp, "wb").write(lkeep)
    open(os.path.join(d, "BTCUSDT", "page-000099.json"), "wb").write(b"{}")
    assert any("UNLISTED_FILE" in x for x in MH.verify_dataset(d)["problems"])
    os.remove(os.path.join(d, "BTCUSDT", "page-000099.json"))
    os.chmod(rp, 0o644)
    assert any("group/world accessible" in x for x in MH.verify_dataset(d)["problems"])
    os.chmod(rp, 0o600)
    assert MH.verify_dataset(d)["ok"]
    os.remove(mp)
    assert not MH.verify_dataset(d)["ok"]


# ---------------------------------------------------------------- rate limits, backoff, bucket, concurrency, skew

def test_a_429_storm_backs_off_with_an_injected_sleep_then_gives_up_as_partial_and_resumes_later(root):
    v = funding_venue()
    t = Time()
    v.script[("funding", "BTCUSDT")] = [MC.RateLimited("HTTP 429")] * 4
    man = run(root, funding_specs(), v, time=t, max_retries=3, backoff_s=1.0)
    s = sym_of(man)
    assert s["status"] == "PARTIAL" and any("rate_limited" in e for e in s["run"]["errors"]) and s["run"]["rate_limit_events"] == 4
    assert [x for x in t.sleeps if x >= 1.0][:3] == [1.0, 2.0, 4.0]                  # exponential, bounded by max_retries (never an endless loop)
    assert len(v.urls) == 4                                                           # one try + 3 retries, no more
    man2 = run(root, funding_specs(), v, time=t)                                      # the storm passed: a rerun completes the symbol
    assert sym_of(man2)["status"] == "COMPLETE"


def test_retcode_10006_in_a_body_is_a_rate_limit_too(root):
    v = funding_venue()
    storm = json.dumps({"retCode": 10006, "retMsg": "Too many visits", "result": {}, "time": SERVER0}).encode()
    v.script[("funding", "BTCUSDT")] = [storm, storm, None]
    v.script[("funding", "BTCUSDT")][2] = lambda q: envelope("funding", "BTCUSDT", [row_for("funding", "BTCUSDT", t) for t in sorted(v.data[("funding", "BTCUSDT")], reverse=True)[:200]], SERVER0)
    t = Time()
    man = run(root, funding_specs(), v, time=t, backoff_s=1.0)
    assert sym_of(man)["status"] == "COMPLETE" and sym_of(man)["run"]["rate_limit_events"] == 2


def test_the_429_penalty_is_shared_so_the_other_worker_also_waits():
    t = Time()
    b = MH.TokenBucket(100.0, 100, t.mono, t.sleep)
    b.acquire()
    assert t.sleeps == []
    b.penalize(7.0)
    b.acquire()
    assert t.sleeps and abs(sum(t.sleeps) - 7.0) < 1e-9


def test_token_bucket_spaces_requests_at_the_declared_rate():
    t = Time()
    b = MH.TokenBucket(2.0, 1, t.mono, t.sleep)
    for _ in range(5):
        b.acquire()
    assert abs(t.t - 4 * 0.5) < 1e-9                                                  # 1 burst + 4 more at 2/s
    with pytest.raises(MH.HistoryError):
        MH.TokenBucket(0, 1, t.mono, t.sleep)


def test_run_total_fake_time_respects_the_shared_rate_with_two_workers(root):
    syms = ("AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT")
    v = funding_venue(syms)
    t = Time()
    man = run(root, funding_specs(syms), v, time=t, workers=2, rate=4.0)
    assert man["dataset_status"] == "COMPLETE"
    requests = len(v.urls)
    assert t.t >= (requests - 4) / 4.0 - 1e-6                                          # burst 4, then 4 requests/second in total, whatever the worker count


def test_two_workers_give_the_same_evidence_as_one(root, tmp_path):
    syms = ("AAAUSDT", "BBBUSDT", "CCCUSDT")
    m1 = run(root, funding_specs(syms), funding_venue(syms), workers=1)
    r2 = tmp_path / "data_b"
    os.mkdir(r2, 0o700)
    m2 = run(r2, funding_specs(syms), funding_venue(syms), workers=2)
    for s in syms:
        a, b = sym_of(m1, s), sym_of(m2, s)
        assert a["rows_sha256"] == b["rows_sha256"] and a["pages_digest"] == b["pages_digest"] and a["status"] == b["status"] == "COMPLETE"
    with pytest.raises(MH.HistoryError):
        run(root, funding_specs(syms), funding_venue(syms), workers=99)


def test_clock_skew_between_server_and_receive_is_flagged_and_the_symbol_is_partial(root):
    v = funding_venue(n=20)
    s = sym_of(run(root, funding_specs(launch=BASE - 19 * H8 - 1), v, clock=Clock(SERVER0 + 60_000)))        # we received it a minute after the server's own time
    assert s["status"] == "PARTIAL" and "clock_skew" in s["reasons"] and s["skew_flags"] >= 1
    s2 = sym_of(run(os.path.join(os.path.dirname(str(root)), "data3") if os.mkdir(os.path.join(os.path.dirname(str(root)), "data3"), 0o700) is None else root,
                    funding_specs(launch=BASE - 19 * H8 - 1), v, clock=Clock(SERVER0 - 60_000)))              # or before it
    assert s2["status"] == "PARTIAL" and "clock_skew" in s2["reasons"]


def test_a_page_without_a_server_time_is_a_skew_flag_not_a_pass(root):
    v = funding_venue(n=20)
    v.script[("funding", "BTCUSDT")] = [json.dumps({"retCode": 0, "result": {"category": "linear", "list": [row_for("funding", "BTCUSDT", BASE)]}}).encode()]
    s = sym_of(run(root, funding_specs(launch=BASE - 19 * H8 - 1), v))
    assert s["skew_flags"] >= 1 and s["status"] == "PARTIAL"


# ---------------------------------------------------------------- disk and root guards

def test_a_data_root_inside_a_git_work_tree_is_refused(tmp_path):
    repo = tmp_path / "repo"
    os.makedirs(repo / ".git")
    r = repo / "data"
    os.mkdir(r, 0o700)
    man = run(r, funding_specs(), funding_venue())
    assert man["status"] == "REFUSED_ROOT" and any("git work tree" in p for p in man["problems"])
    assert not os.path.exists(r / "funding")                                           # nothing was written
    nested = repo / "a" / "b" / "c"                                                    # a root that does not exist yet is judged by its nearest existing parent
    assert MC.root_guard(str(nested), 1, lambda _p: 1 << 40)


def test_a_group_readable_root_or_a_link_is_refused(tmp_path):
    r = tmp_path / "open"
    os.mkdir(r, 0o755)
    os.chmod(r, 0o755)
    assert any("0700" in p for p in MC.root_guard(str(r), 1, lambda _p: 1 << 40))
    link = tmp_path / "ln"
    os.mkdir(tmp_path / "real", 0o700)
    os.symlink(tmp_path / "real", link)
    assert any("symbolic link" in p for p in MC.root_guard(str(link), 1, lambda _p: 1 << 40))


def test_disk_full_guard_refuses_before_any_write_and_stops_a_run_whose_disk_fills(root):
    man = run(root, funding_specs(), funding_venue(), free_bytes_fn=lambda _p: 10, reserve_bytes=1000)
    assert man["status"] == "REFUSED_ROOT" and any("below the reserve" in p for p in man["problems"]) and os.listdir(root) == []
    free = {"v": 1 << 40}
    syms = ("AAAUSDT", "BBBUSDT")
    v = funding_venue(syms)

    def fetch(url):
        free["v"] = 10                                                                 # the disk fills during the first symbol
        return v(url)
    man = run(root, funding_specs(syms), fetch, free_bytes_fn=lambda _p: free["v"], reserve_bytes=1000, workers=1)
    b = sym_of(man, "BBBUSDT")
    assert b["status"] == "UNVERIFIED" and b["reasons"] == ["symbol_not_collected"] and any("disk_floor" in e for e in b["run"]["errors"])
    assert man["dataset_status"] != "COMPLETE" and not os.path.exists(os.path.join(ddir(root), "BBBUSDT"))


def test_a_concurrent_second_run_on_the_same_dataset_is_refused(root):
    d = ddir(root)
    os.makedirs(d, mode=0o700)
    with MC._locked(d):
        with pytest.raises(MC.Locked):
            run(root, funding_specs(), funding_venue())


# ---------------------------------------------------------------- universe selection

def _instrument(s, intervals=None, statuses=None, deliveries=None, bases=None, sid=None):
    return {"symbol": s, "symbolId": sid or "", "contractType": "LinearPerpetual", "status": (statuses or {}).get(s, "Trading"), "baseCoin": (bases or {}).get(s, s[:-4]),
            "quoteCoin": "USDT", "settleCoin": "USDT", "launchTime": str(FIRST - 1), "deliveryTime": str((deliveries or {}).get(s, 0)),
            "fundingInterval": (intervals or {}).get(s, 480), "upperFundingRate": "0.00375", "lowerFundingRate": "-0.00375",
            "priceFilter": {"minPrice": "0.1", "tickSize": "0.1"}, "lotSizeFilter": {"minOrderQty": "0.001", "maxOrderQty": "10", "qtyStep": "0.001", "minNotionalValue": "5"}}


def _ticker(s):
    return {"symbol": s, "markPrice": "1", "indexPrice": "1", "lastPrice": "1", "fundingRate": "0.0001", "nextFundingTime": "1", "fundingIntervalHour": "8", "fundingCap": "0.00375",
            "openInterest": "1", "openInterestValue": "1", "volume24h": "1", "turnover24h": "5"}


def _universe_dir(tmp_path, syms=("BTCUSDT", "ETHUSDT", "NEWUSDT"), intervals=None, statuses=None, deliveries=None, bases=None, v2=True, name="uni", refuse_closed=False):
    """A real schema-2 capture (``capture_universe``: every status partition asked for, a venue that serves status filters like Bybit) or, with v2=False, the legacy schema-1 one."""
    inst = {s: _instrument(s, intervals, statuses, deliveries, bases) for s in syms}

    def fetch(url):
        base = venues.rest_for("mainnet")
        path, _, qs = url[len(base):].partition("?")
        q = dict(x.split("=", 1) for x in qs.split("&"))
        if path == UC.PATH_TICKERS:
            rows = [_ticker(s) for s in syms]
        else:
            want = q.get("status", "Trading")
            if refuse_closed and want == "Closed":
                return json.dumps({"retCode": 10001, "retMsg": "params error: status", "result": {}, "time": SERVER0}).encode()
            rows = [r for r in inst.values() if r["status"] == want] if v2 else list(inst.values())
        return json.dumps({"retCode": 0, "retMsg": "OK", "result": {"category": q["category"], "list": rows, "nextPageCursor": ""}, "time": SERVER0}).encode()
    d = tmp_path / name
    if v2:
        UC.capture_universe(str(d), fetch=fetch, clock_ms=lambda: SERVER0, sleep=lambda _s: None, pause_s=0)
    else:
        UC.capture(str(d), fetch=fetch, clock_ms=lambda: SERVER0, sleep=lambda _s: None)
    return d


def test_symbols_and_intervals_come_from_a_verified_universe_directory(root, tmp_path):
    d = _universe_dir(tmp_path, intervals={"ETHUSDT": 240})
    recs, info = MH.load_universe_dir(str(d))
    assert sorted(recs) == ["BTCUSDT", "ETHUSDT", "NEWUSDT"] and info["symbols"] == 3 and len(info["manifest_sha256"]) == 64
    specs = MH.build_specs("funding", None, sorted(recs), START, END, recs)
    by = {s["symbol"]: s for s in specs}
    assert by["ETHUSDT"]["interval_ms"] == 240 * 60_000 and by["BTCUSDT"]["interval_ms"] == H8 and by["BTCUSDT"]["launch_ms"] == FIRST - 1
    assert by["BTCUSDT"]["interval_source"].startswith("universe_record:fundingInterval_min@")
    man = run(root, specs, funding_venue(("BTCUSDT", "NEWUSDT")), universe=info)
    assert man["universe"]["manifest_sha256"] == info["manifest_sha256"]
    assert sym_of(man, "ETHUSDT")["status"] == "PARTIAL"                               # no rows served for it: reported, not hidden
    assert sym_of(man, "BTCUSDT")["status"] == "COMPLETE"


def test_a_universe_directory_that_does_not_re_hash_is_refused(tmp_path):
    d = _universe_dir(tmp_path)
    f = next(p for p in os.listdir(d) if p.startswith("universe-"))
    with open(d / f, "ab") as fh:
        fh.write(b"\n")
    with pytest.raises(MH.HistoryError):
        MH.load_universe_dir(str(d))
    with pytest.raises(MH.HistoryError):
        MH.load_universe_dir(str(tmp_path / "nowhere"))


def test_only_trading_filters_the_universe(tmp_path):
    d = _universe_dir(tmp_path)
    recs, _ = MH.load_universe_dir(str(d), only_trading=True)
    assert len(recs) == 3


# ---------------------------------------------------------------- CLI

def test_cli_backfill_then_verify(root, tmp_path, capsys):
    v = funding_venue()
    t = Time()
    argv = ["funding", "--out-root", str(root), "--symbols", "BTCUSDT", "--start-ms", str(START), "--end-ms", str(END), "--funding-interval-min", "BTCUSDT=480", "--rate", "50"]
    rc = MH.main(argv, fetch=v, clock=Clock(), sleep=t.sleep, mono=t.mono, free_bytes_fn=lambda _p: 1 << 40)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["dataset_status"] == "PARTIAL"                              # no launch time declared: the head cannot be proved, so the exit is non-zero
    assert out["symbols"]["BTCUSDT"]["reasons"] == ["head_unverified_no_launch_time"] and out["symbols"]["BTCUSDT"]["rows"] == N
    rc = MH.main(["verify", "--dir", ddir(root)])
    assert rc == 0 and json.loads(capsys.readouterr().out)["ok"] is True
    os.remove(os.path.join(ddir(root), "BTCUSDT", "page-000001.json"))
    assert MH.main(["verify", "--dir", ddir(root)]) == 1


def test_cli_universe_dir_and_exit_codes(root, tmp_path, capsys):
    d = _universe_dir(tmp_path, syms=("BTCUSDT",))
    v = funding_venue()
    t = Time()
    kw = dict(fetch=v, clock=Clock(), sleep=t.sleep, mono=t.mono, free_bytes_fn=lambda _p: 1 << 40)
    rc = MH.main(["funding", "--out-root", str(root), "--universe-dir", str(d), "--start-ms", str(START), "--end-ms", str(END), "--rate", "50"], **kw)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["dataset_status"] == "COMPLETE"
    assert MH.main(["funding", "--out-root", str(root), "--symbols", "BTCUSDT"], **kw) == 2                    # no window
    assert MH.main(["funding", "--out-root", str(root), "--symbols", "btc", "--start-ms", "1"], **kw) == 2
    assert MH.main(["kline", "--out-root", str(root), "--symbols", "BTCUSDT", "--interval", "M", "--start-ms", "1", "--end-ms", "5"], **kw) == 2
    rc = MH.main(["funding", "--out-root", str(root), "--symbols", "BTCUSDT", "--days", "3"], fetch=v, clock=Clock(), sleep=t.sleep, mono=t.mono, free_bytes_fn=lambda _p: 1)
    assert rc == 2


# ---------------------------------------------------------------- the module cannot sign, send orders or open its own connection (AST)

def test_market_history_imports_no_http_signing_or_broker_module_and_names_only_the_five_paths():
    src = open(os.path.join(BOT, "tools", "market_history.py")).read()
    tree = ast.parse(src)
    imps = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imps |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            imps.add(n.module or "")
    assert imps == {"__future__", "argparse", "hashlib", "json", "os", "re", "shutil", "sys", "threading", "time", "urllib.parse", "concurrent.futures", "decimal", "typing",
                    "market_collector", "universe_capture", "venues"}
    assert not any(i.split(".")[0] in ("hmac", "bybit_connection", "carry_broker", "entry_authority", "requests", "http", "socket", "urllib3", "secrets") for i in imps)
    assert "urllib.request" not in imps
    strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert {s for s in strings if s.startswith("/v5/")} == {"/v5/market/funding/history", "/v5/market/kline", "/v5/market/mark-price-kline", "/v5/market/index-price-kline",
                                                           "/v5/market/open-interest"}
    assert set(MH.HISTORY_PATHS) <= set(__import__("runtime_capability").PUBLIC_MARKET_PATHS)
    doc_nodes = {id(n.body[0].value) for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
                 and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    blob = " ".join(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc_nodes).lower()
    for bad in ("x-bapi", "api_key", "apikey", "secret", "signature", "/order/", "/position/", "/account/", "/asset/", "private", "environ"):
        assert bad not in blob
    assert "POST" not in strings and "PUT" not in strings and "DELETE" not in strings
    calls = " ".join(ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert "environ" not in calls and "getenv" not in calls and "BybitClient" not in calls


def test_the_history_fetch_goes_through_the_public_collector_role_and_nothing_else():
    seen = []

    class Spy:
        def request(self, method, url, **kw):
            seen.append((method, url, kw))
            return b"{}"
    real = MC._UrllibTransport
    MC._UrllibTransport = Spy
    try:
        url = MH.request_url("open-interest", "BTCUSDT", "5min", 10, 20)
        assert MH.http_fetch(url) == b"{}"
    finally:
        MC._UrllibTransport = real
    assert [s[0] for s in seen] == ["GET"] and set(seen[0][2]["headers"]) <= {"Accept", "User-Agent"}


# ---------------------------------------------------------------- Closed contracts, include-statuses, served coverage

def test_include_statuses_selects_closed_contracts_and_asks_them_up_to_their_delivery_time(root, tmp_path):
    delivery = BASE - 60 * H8
    d = _universe_dir(tmp_path, syms=("BTCUSDT", "OLDUSDT", "GONEUSDT", "PRELUSDT"), statuses={"OLDUSDT": "Closed", "GONEUSDT": "Closed", "PRELUSDT": "PreLaunch"},
                      deliveries={"OLDUSDT": delivery, "GONEUSDT": START - 5000})
    recs, info = MH.load_universe_dir(str(d), include_statuses=["Trading", "Closed"])
    assert sorted(recs) == ["BTCUSDT", "GONEUSDT", "OLDUSDT"] and info["included_statuses"] == ["Closed", "Trading"]
    assert info["statuses_in_capture"] == {"Closed": 2, "PreLaunch": 1, "Trading": 1}
    specs = {x["symbol"]: x for x in MH.build_specs("funding", None, sorted(recs), START, END, recs)}
    assert specs["OLDUSDT"]["end_ms"] == delivery and specs["OLDUSDT"]["window_end_ms"] == END      # endTime = deliveryTime for a contract delisted inside the window
    assert specs["BTCUSDT"]["end_ms"] == END and specs["GONEUSDT"]["skip"] == "delisted_before_window"
    v = HV({("funding", "BTCUSDT"): grid(FIRST, N, H8), ("funding", "OLDUSDT"): grid(FIRST, N - 60, H8)})
    man = run(root, list(specs.values()), v, universe=info)
    assert man["excluded"] == {"GONEUSDT": "delisted_before_window"} and man["universe"]["included_statuses"] == ["Closed", "Trading"]
    old = sym_of(man, "OLDUSDT")
    assert old["status"] == "COMPLETE"
    assert max(int(u.split("endTime=")[1].split("&")[0]) for u in v.urls if "OLDUSDT" in u) == delivery           # never asked past the delisting
    assert MH.load_universe_dir(str(d))[1]["included_statuses"] == "ALL"
    with pytest.raises(MH.HistoryError):
        MH.load_universe_dir(str(d), only_trading=True, include_statuses=["Closed"])


def test_per_symbol_coverage_records_the_requested_window_and_what_the_venue_actually_served(root):
    served = grid(FIRST + 40 * H8, N - 40, H8)                                       # the venue serves LESS than the contract's life: recorded, never assumed
    man = run(root, funding_specs(launch=FIRST - 1), HV({("funding", "BTCUSDT"): served}))
    c = sym_of(man)["coverage"]
    assert c["requested_window"] == {"start_ms": START, "end_ms": END, "symbol_end_ms": END}
    assert (c["served_first_ts"], c["served_last_ts"], c["served_rows"]) == (served[0], served[-1], len(served))
    assert sym_of(man)["status"] == "PARTIAL" and "head_gap" in sym_of(man)["reasons"]           # visible as truncation, not as a clean pass


def test_cli_universe_capture_alias_and_include_statuses(root, tmp_path, capsys):
    d = _universe_dir(tmp_path, syms=("BTCUSDT", "OLDUSDT"), statuses={"OLDUSDT": "Closed"}, deliveries={"OLDUSDT": BASE - 60 * H8})
    v = HV({("funding", "BTCUSDT"): grid(FIRST, N, H8), ("funding", "OLDUSDT"): grid(FIRST, N - 60, H8)})
    t = Time()
    kw = dict(fetch=v, clock=Clock(), sleep=t.sleep, mono=t.mono, free_bytes_fn=lambda _p: 1 << 40)
    rc = MH.main(["funding", "--out-root", str(root), "--universe-capture", str(d), "--include-statuses", "Trading,Closed", "--start-ms", str(START), "--end-ms", str(END), "--rate", "50"], **kw)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and sorted(out["symbols"]) == ["BTCUSDT", "OLDUSDT"]


# ---------------------------------------------------------------- spot instruments and hedge availability

def spot_body(rows, server=SERVER0, cursor="", category="spot"):
    return json.dumps({"retCode": 0, "retMsg": "OK", "result": {"category": category, "list": rows, "nextPageCursor": cursor}, "time": server}, indent=1).encode()


def spot_row(base, quote="USDT", status="Trading", symbol=None):
    return {"symbol": symbol or base + quote, "baseCoin": base, "quoteCoin": quote, "status": status, "innovation": "0"}


def test_spot_instruments_is_one_request_with_no_pagination_and_verifies(root):
    urls = []

    def fetch(u):
        urls.append(u)
        return spot_body([spot_row("BTC"), spot_row("ETH"), spot_row("ETH", "USDC"), spot_row("DEAD", status="Closed")])
    res = MH.capture_spot_instruments(str(root), fetch=fetch, clock=Clock(), sleep=lambda _s: None, free_bytes_fn=lambda _p: 1 << 40, reserve_bytes=1)
    assert res["dataset_status"] == "COMPLETE" and res["rows"] == 4 and res["by_quote"] == {"USDC": 1, "USDT": 3} and res["by_status"] == {"Closed": 1, "Trading": 3}
    assert urls == [f"{venues.rest_for('mainnet')}/v5/market/instruments-info?category=spot"]
    assert stat.S_IMODE(os.stat(os.path.join(res["dir"], "page-000001.json")).st_mode) == 0o600
    assert res["page"]["local_receive_time"] <= res["page"]["collection_time"] <= res["normalization_time"] and res["page"]["source_server_time"] == SERVER0
    assert MH.verify_spot(res["dir"])["ok"]
    open(os.path.join(res["dir"], "rows.jsonl"), "ab").write(b"x")
    assert not MH.verify_spot(res["dir"])["ok"]


def test_spot_snapshot_with_a_cursor_conflict_bad_envelope_or_skew_is_not_complete(root):
    def snap(body, clock=None, sub="a"):
        r = root / sub
        os.mkdir(r, 0o700)
        return MH.capture_spot_instruments(str(r), fetch=lambda u: body, clock=clock or Clock(), sleep=lambda _s: None, free_bytes_fn=lambda _p: 1 << 40, reserve_bytes=1)
    assert snap(spot_body([spot_row("BTC")], cursor="page%3D2"), sub="c1")["dataset_status"] == "PARTIAL"
    assert "venue_named_a_cursor_not_followed" in snap(spot_body([spot_row("BTC")], cursor="page%3D2"), sub="c2")["reasons"]
    assert snap(spot_body([spot_row("BTC"), spot_row("BTC", status="Closed", symbol="BTCUSDT")]), sub="c3")["dataset_status"] == "CONFLICTED"
    assert snap(spot_body([spot_row("BTC")], category="linear"), sub="c4")["dataset_status"] == "PARTIAL"
    assert snap(spot_body([spot_row("BTC")]), clock=Clock(SERVER0 + 90_000), sub="c5")["dataset_status"] == "PARTIAL"
    assert snap(spot_body([{"symbol": "bad sym"}]), sub="c6")["rejects"] == {"bad_row": 1}
    assert snap(spot_body([]), sub="c7")["reasons"] == ["no_rows"]


def test_spot_capture_failure_and_rate_limit_are_reported_not_raised(root):
    def storm(_u):
        raise MC.RateLimited("HTTP 429")
    t = Time()
    res = MH.capture_spot_instruments(str(root), fetch=storm, clock=Clock(), sleep=t.sleep, free_bytes_fn=lambda _p: 1 << 40, reserve_bytes=1, max_retries=2, backoff_s=1.0)
    assert res["status"] == "FAILED" and res["rate_limit_events"] == 3 and t.sleeps == [1.0, 2.0]
    assert MH.capture_spot_instruments(str(root), fetch=storm, free_bytes_fn=lambda _p: 1, reserve_bytes=1000)["status"] == "REFUSED_ROOT"


def test_hedge_availability_is_answered_from_the_raw_snapshot_and_never_assumes_a_multiplier(root, tmp_path):
    d = _universe_dir(tmp_path, syms=("BTCUSDT", "ETHUSDT", "ZZZUSDT", "DEADUSDT", "1000PEPEUSDT", "NOSPOTUSDT"),
                      bases={"1000PEPEUSDT": "1000PEPE", "NOSPOTUSDT": "NOSPOT"}, statuses={"DEADUSDT": "Closed"})
    recs, _ = MH.load_universe_dir(str(d))
    res = MH.capture_spot_instruments(str(root), fetch=lambda u: spot_body([spot_row("BTC"), spot_row("ETH", "USDC"), spot_row("ZZZ", status="Delisted"), spot_row("DEAD"), spot_row("PEPE")]),
                                      clock=Clock(), sleep=lambda _s: None, free_bytes_fn=lambda _p: 1 << 40, reserve_bytes=1)
    h = MH.hedge_availability(recs, res["dir"])
    v = {k: x["verdict"] for k, x in h["by_symbol"].items()}
    assert v == {"BTCUSDT": "SPOT_TRADING", "ETHUSDT": "NO_SPOT_PAIR", "ZZZUSDT": "SPOT_NOT_TRADING", "DEADUSDT": "SPOT_TRADING", "1000PEPEUSDT": "UNRESOLVED_MULTIPLIER", "NOSPOTUSDT": "NO_SPOT_PAIR"}
    assert h["by_symbol"]["1000PEPEUSDT"]["spot_candidate"] == "PEPEUSDT" and h["by_symbol"]["DEADUSDT"]["perp_status"] == "Closed"
    assert h["counts"]["NO_SPOT_PAIR"] == 2 and h["perps"] == 6
    open(os.path.join(res["dir"], "page-000001.json"), "ab").write(b" ")
    with pytest.raises(MH.HistoryError):
        MH.hedge_availability(recs, res["dir"])


def test_cli_spot_and_hedge_commands(root, tmp_path, capsys):
    d = _universe_dir(tmp_path, syms=("BTCUSDT",))
    kw = dict(fetch=lambda u: spot_body([spot_row("BTC")]), clock=Clock(), sleep=lambda _s: None, free_bytes_fn=lambda _p: 1 << 40)
    assert MH.main(["spot-instruments", "--out-root", str(root), "--reserve-bytes", "1"], **kw) == 0
    snap = json.loads(capsys.readouterr().out)["dir"]
    assert MH.main(["hedge-availability", "--universe-capture", str(d), "--spot-dir", snap]) == 0
    assert json.loads(capsys.readouterr().out)["counts"] == {"SPOT_TRADING": 1}
    assert MH.main(["verify", "--dir", snap]) == 0
    capsys.readouterr()
    assert MH.main(["spot-instruments", "--out-root", str(root)], fetch=lambda u: b"{}", clock=Clock(SERVER0 + 10_000_000), sleep=lambda _s: None, free_bytes_fn=lambda _p: 1 << 40) == 1


# ---------------------------------------------------------------- universe_capture v2 acceptance rules

def test_a_schema_2_capture_is_accepted_only_when_it_verifies_and_its_rederived_status_is_complete_or_partial(tmp_path):
    d = _universe_dir(tmp_path, syms=("BTCUSDT", "OLDUSDT"), statuses={"OLDUSDT": "Closed"}, deliveries={"OLDUSDT": BASE})
    recs, info = MH.load_universe_dir(str(d), include_statuses=["Trading", "Closed"])
    assert info["capture_schema"] == 2 and info["dataset_status"] in ("COMPLETE", "PARTIAL") and info["status_coverage"] and sorted(recs) == ["BTCUSDT", "OLDUSDT"]
    assert recs["OLDUSDT"]["deliveryTime"] == BASE and recs["OLDUSDT"]["fundingInterval_min"] == 480
    f = next(p for p in os.listdir(d) if p.startswith("universe-"))
    os.chmod(d / f, 0o600)
    with open(d / f, "ab") as fh:
        fh.write(b"\n")
    with pytest.raises(MH.HistoryError, match="does not verify"):
        MH.load_universe_dir(str(d))


def test_a_schema_2_capture_whose_rederived_status_is_not_acceptable_is_refused(tmp_path, monkeypatch):
    d = _universe_dir(tmp_path)
    real = UC.verify_capture_dir
    for bad in ("UNVERIFIED", "CONFLICTED", "STALE"):
        monkeypatch.setattr(UC, "verify_capture_dir", lambda p, bad=bad: dict(real(p), coverage=dict(real(p)["coverage"], dataset_status=bad)))
        with pytest.raises(MH.HistoryError, match=bad):
            MH.load_universe_dir(str(d))
    monkeypatch.setattr(UC, "verify_capture_dir", lambda p: dict(real(p), ok=False, problems=["x"]))
    with pytest.raises(MH.HistoryError):
        MH.load_universe_dir(str(d))


def test_a_venue_that_refuses_the_closed_partition_leaves_a_recorded_answer_and_no_closed_symbols(tmp_path):
    d = _universe_dir(tmp_path, syms=("BTCUSDT", "OLDUSDT"), statuses={"OLDUSDT": "Closed"}, refuse_closed=True)
    recs, info = MH.load_universe_dir(str(d), include_statuses=["Trading", "Closed"])
    assert sorted(recs) == ["BTCUSDT"] and info["dataset_status"] in ("COMPLETE", "PARTIAL")
    assert "REFUSED" in json.dumps(info["status_coverage"])                                # the refusal is recorded, so the absence of Closed is visible, not assumed


def test_a_legacy_schema_1_capture_is_still_readable_and_labelled(tmp_path):
    d = _universe_dir(tmp_path, v2=False, name="uni1")
    recs, info = MH.load_universe_dir(str(d))
    assert info["capture_schema"] == 1 and sorted(recs) == ["BTCUSDT", "ETHUSDT", "NEWUSDT"] and info["status_coverage"] is None
