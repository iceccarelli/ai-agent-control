"""universe_capture: offline only. The transport and the clock are injected; a socket guard fails any real connection."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import socket
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import universe_capture as UC  # noqa: E402
import venues  # noqa: E402

NOW = 1_800_000_000_000


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("network touched")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


def inst(sym, **over):
    r = {"symbol": sym, "contractType": "LinearPerpetual", "status": "Trading", "baseCoin": sym[:-4], "quoteCoin": "USDT", "settleCoin": "USDT",
         "launchTime": "1585526400000", "deliveryTime": "0", "fundingInterval": 480, "upperFundingRate": "0.00375", "lowerFundingRate": "-0.00375",
         "priceFilter": {"minPrice": "0.10", "tickSize": "0.10"},
         "lotSizeFilter": {"minOrderQty": "0.001", "maxOrderQty": "1190", "qtyStep": "0.001", "minNotionalValue": "5"}}
    r.update({"symbolId": f"id-{sym}"[:40], "symbolType": "", "isPreListing": False, "priceScale": "2", "unifiedMarginTrade": True, "copyTrading": "both",
              "deliveryFeeRate": "", "leverageFilter": {"minLeverage": "1", "maxLeverage": "100.00", "leverageStep": "0.01"},
              "riskParameters": {"priceLimitRatioX": "0.01", "priceLimitRatioY": "0.02"}})
    r["priceFilter"] = dict(r["priceFilter"], maxPrice="199999.80")
    r["lotSizeFilter"] = dict(r["lotSizeFilter"], maxMktOrderQty="500", postOnlyMaxOrderQty="1190")
    r.update(over)
    return r


def env(rows, nxt="", time_ms=NOW, category="linear", **kw):
    d = {"retCode": 0, "retMsg": "OK", "result": {"category": category, "list": rows, "nextPageCursor": nxt}, "time": time_ms}
    d.update(kw)
    return json.dumps(d, indent=1).encode()          # non-canonical bytes on purpose: they must be preserved unedited


def tick(sym, **over):
    t = {"symbol": sym, "markPrice": "100.5", "indexPrice": "100.4", "lastPrice": "100.5", "fundingRate": "0.0001", "nextFundingTime": str(NOW + 1000),
         "fundingIntervalHour": "8", "fundingCap": "0.00375", "openInterest": "10", "openInterestValue": "1005", "volume24h": "5", "turnover24h": "500"}
    t.update(over)
    return t


class Venue:
    """Serves scripted instrument pages by cursor, then the tickers. Records every URL."""
    def __init__(self, pages, tickers, tickers_time=NOW):
        self.pages = pages                      # {cursor: bytes}
        self.tickers = tickers
        self.tickers_time = tickers_time
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        path, _, qs = url.partition("?")
        q = dict(p.split("=", 1) for p in qs.split("&"))
        if path.endswith(UC.PATH_TICKERS):
            return env(self.tickers, time_ms=self.tickers_time)
        return self.pages[q.get("cursor", "")]


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        self.t += 7
        return self.t


def run(tmp_path, venue, name="run1", **kw):
    kw.setdefault("sleep", lambda _s: None)
    kw.setdefault("clock_ms", Clock())
    return UC.capture(str(tmp_path / name), fetch=venue, **kw)


def three_pages():
    return {"": env([inst("BTCUSDT"), inst("ETHUSDT")], "c1"), "c1": env([inst("SOLUSDT")], "c2"), "c2": env([inst("XRPUSDT")], "")}


def all_tickers():
    return [tick(s) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")]


def read_universe(m, d):
    return [json.loads(x) for x in open(os.path.join(d, m["universe"]["file"]))]


# ---------------------------------------------------------------- requests are constants on the registry host

def test_requests_are_constants_on_the_registry_host(tmp_path):
    v = Venue(three_pages(), all_tickers())
    run(tmp_path, v)
    base = venues.rest_for("mainnet")
    assert v.urls[0] == f"{base}/v5/market/instruments-info?category=linear&limit=1000"
    assert v.urls[1].endswith("&cursor=c1") and v.urls[2].endswith("&cursor=c2")
    assert v.urls[3] == f"{base}/v5/market/tickers?category=linear"
    assert all(u.startswith("https://api.bybit.com/") for u in v.urls)


def test_pagination_complete_manifest_and_universe(tmp_path):
    m = run(tmp_path, Venue(three_pages(), all_tickers()))
    d = str(tmp_path / "run1")
    assert m["stopped_because"] == "end_of_list" and [p["rows"] for p in m["pages"]] == [2, 1, 1]
    assert m["rows_seen"] == 4 and m["rows_accepted"] == 4 and m["rows_rejected"] == 0 and m["schema_version"] == 1
    assert [p["request_cursor"] for p in m["pages"]] == ["", "c1", "c2"]
    rows = read_universe(m, d)
    assert [r["symbol"] for r in rows] == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]          # sorted by symbol only, never by performance
    btc = rows[0]
    assert btc["status"] == "Trading" and btc["launchTime"] == 1585526400000 and btc["deliveryTime"] == 0 and btc["contractType"] == "LinearPerpetual"
    assert btc["fundingInterval_min"] == 480 and btc["upperFundingRate"] == "0.00375" and btc["lowerFundingRate"] == "-0.00375"
    assert (btc["tickSize"], btc["qtyStep"], btc["minOrderQty"], btc["minNotionalValue"]) == ("0.10", "0.001", "0.001", "5")
    assert btc["ticker"]["fundingCap"] == "0.00375" and btc["snapshot_ms"] == m["snapshot_ms"]
    assert set(os.listdir(d)) == {"instruments_page0001.json", "instruments_page0002.json", "instruments_page0003.json", "tickers.json",
                                  f"universe-{m['snapshot_ms']}.jsonl", "manifest.json"}


def test_raw_bytes_are_unedited_hashed_and_private(tmp_path):
    pages = three_pages()
    m = run(tmp_path, Venue(pages, all_tickers()))
    d = str(tmp_path / "run1")
    for p, cur in zip(m["pages"], ("", "c1", "c2")):
        got = open(os.path.join(d, p["file"]), "rb").read()
        assert got == pages[cur] and UC._sha(got) == p["sha256"] and p["bytes"] == len(got)
        assert stat.S_IMODE(os.stat(os.path.join(d, p["file"])).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.join(d, "manifest.json")).st_mode) == 0o600
    assert m["universe"]["sha256"] == UC._sha(open(os.path.join(d, m["universe"]["file"]), "rb").read())


def test_receive_time_comes_from_the_injected_clock(tmp_path):
    m = run(tmp_path, Venue(three_pages(), all_tickers()), clock_ms=Clock(NOW))
    assert m["started_ms"] == NOW + 7 and [p["recv_ms"] for p in m["pages"]] == [NOW + 14, NOW + 21, NOW + 28] and m["tickers"]["recv_ms"] == NOW + 35


# ---------------------------------------------------------------- cursor chain

def test_a_repeated_cursor_is_fatal(tmp_path):
    pages = {"": env([inst("BTCUSDT")], "c1"), "c1": env([inst("ETHUSDT")], "c2"), "c2": env([inst("SOLUSDT")], "c1")}
    with pytest.raises(UC.CaptureError, match="did not advance"):
        run(tmp_path, Venue(pages, all_tickers()))


def test_a_cursor_that_does_not_advance_is_fatal(tmp_path):
    pages = {"": env([inst("BTCUSDT")], "c1"), "c1": env([inst("ETHUSDT")], "c1")}
    with pytest.raises(UC.CaptureError, match="did not advance"):
        run(tmp_path, Venue(pages, all_tickers()))


def test_an_empty_page_that_names_a_cursor_is_fatal(tmp_path):
    pages = {"": env([inst("BTCUSDT")], "c1"), "c1": env([], "c2")}
    with pytest.raises(UC.CaptureError, match="empty page"):
        run(tmp_path, Venue(pages, all_tickers()))


def test_the_page_cap_with_a_cursor_pending_is_fatal_and_writes_no_manifest(tmp_path):
    pages = {"": env([inst("BTCUSDT")], "c1"), "c1": env([inst("ETHUSDT")], "c2"), "c2": env([inst("SOLUSDT")], "c3")}
    with pytest.raises(UC.CaptureError, match="INCOMPLETE"):
        run(tmp_path, Venue(pages, all_tickers()), max_pages=2)
    assert "manifest.json" not in os.listdir(tmp_path / "run1")


def test_an_oversize_page_is_fatal(tmp_path):
    rows = [inst(f"A{i}USDT") for i in range(UC.PAGE_LIMIT + 1)]
    with pytest.raises(UC.CaptureError, match="exceeds the endpoint's page limit"):
        run(tmp_path, Venue({"": env(rows)}, []))


def test_an_oversize_body_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(UC, "MAX_BODY_BYTES", 100)
    with pytest.raises(UC.CaptureError, match="larger"):
        run(tmp_path, Venue(three_pages(), all_tickers()))


@pytest.mark.parametrize("cursor", ["a b", "x" * 600, "ü", "a&limit=1"])
def test_a_hostile_cursor_is_refused_before_it_can_become_a_url(tmp_path, cursor):
    with pytest.raises(UC.CaptureError):
        run(tmp_path, Venue({"": env([inst("BTCUSDT")], cursor)}, []))


# ---------------------------------------------------------------- rows

def test_non_linear_perpetual_and_delivery_rows_are_counted_not_dropped(tmp_path):
    rows = [inst("BTCUSDT"), inst("BTC-27DEC26", contractType="LinearFutures", deliveryTime="1798329600000"),
            inst("BTCUSDZ26", contractType="LinearFutures", deliveryTime="1798329600000"),
            inst("ETHPERP", contractType="InversePerpetual", quoteCoin="USD", settleCoin="ETH"),
            inst("SOLPERP", quoteCoin="USDC", settleCoin="USDC"), inst("BADUSDT", fundingInterval=0), inst("lowerusdt"),
            inst("NOINTUSDT", fundingInterval=None), "junk"]
    m = run(tmp_path, Venue({"": env(rows)}, [tick("BTCUSDT")]))
    assert m["rows_seen"] == 9 and m["rows_accepted"] == 1 and m["rows_rejected"] == 8
    assert m["rejected_by_reason"] == {"bad_symbol": 2, "invalid_field": 2, "not_an_object": 1, "not_perpetual": 2, "not_usdt": 1}
    assert [r["symbol"] for r in m["rejected"]] == ["BTC-27DEC26", "BTCUSDZ26", "ETHPERP", "SOLPERP", "BADUSDT", "lowerusdt", "NOINTUSDT", ""]
    assert len(read_universe(m, str(tmp_path / "run1"))) == 1


@pytest.mark.parametrize("over", [dict(upperFundingRate="-0.1"), dict(lowerFundingRate="0.1"), dict(upperFundingRate="3"),
                                  dict(fundingInterval=100000), dict(fundingInterval="abc"), dict(launchTime="-1"),
                                  dict(priceFilter={"tickSize": "0"}), dict(lotSizeFilter={"qtyStep": "0.001", "minOrderQty": "0.001"}),
                                  dict(status="")])
def test_hostile_field_values_reject_the_row_with_a_count(tmp_path, over):
    m = run(tmp_path, Venue({"": env([inst("BTCUSDT", **over), inst("ETHUSDT")])}, [tick("ETHUSDT")]))
    assert m["rows_accepted"] == 1 and m["rejected_by_reason"] == {"invalid_field": 1} and m["rejected"][0]["symbol"] == "BTCUSDT"


def test_positive_control_a_clean_row_passes_every_check():
    norm, why, sym = UC.normalize_instrument(inst("BTCUSDT"))
    assert why is None and sym == "BTCUSDT" and norm["fundingInterval_min"] == 480


def test_conflicting_duplicate_symbol_is_fatal_identical_is_counted(tmp_path):
    with pytest.raises(UC.CaptureError, match="conflicting"):
        run(tmp_path, Venue({"": env([inst("BTCUSDT"), inst("BTCUSDT", status="Closed")])}, []))
    m = run(tmp_path, Venue({"": env([inst("BTCUSDT"), inst("BTCUSDT")])}, [tick("BTCUSDT")]), name="run2")
    assert m["identical_duplicates"] == 1 and m["rows_accepted"] == 1
    with pytest.raises(UC.CaptureError, match="twice with different"):
        run(tmp_path, Venue({"": env([inst("BTCUSDT")])}, [tick("BTCUSDT"), tick("BTCUSDT", markPrice="1")]), name="run3")


def test_gaps_and_interval_disagreement_are_reported_not_fixed(tmp_path):
    m = run(tmp_path, Venue({"": env([inst("BTCUSDT"), inst("ETHUSDT")])}, [tick("BTCUSDT", fundingIntervalHour="4"), tick("GHOSTUSDT")]))
    g = m["gaps"]
    assert g["instruments_without_ticker"] == ["ETHUSDT"] and g["tickers_without_usdt_perp_instrument"] == ["GHOSTUSDT"]
    assert g["funding_interval_instrument_vs_ticker_mismatch"] == ["BTCUSDT"]
    rows = read_universe(m, str(tmp_path / "run1"))
    assert rows[1]["ticker"] is None and rows[0]["fundingInterval_min"] == 480          # the instrument's own value is kept


def test_clock_skew_is_flagged_never_corrected(tmp_path):
    ok = run(tmp_path, Venue(three_pages(), all_tickers()), clock_ms=Clock(NOW))
    assert ok["clock"]["pages_flagged"] == [] and not ok["clock"]["tickers_flagged"]
    assert ok["pages"][0]["skew_ms"] == 14
    skewed = run(tmp_path, Venue(three_pages(), all_tickers(), tickers_time=NOW - 60_000), clock_ms=Clock(NOW + 60_000), name="run2")
    assert skewed["clock"]["pages_flagged"] == [1, 2, 3] and skewed["clock"]["tickers_flagged"] and skewed["pages"][0]["skew_flag"] == "SKEW"
    assert skewed["snapshot_ms"] == NOW + 60_007                    # the injected clock is recorded as is


def test_a_non_success_or_wrong_category_envelope_is_fatal(tmp_path):
    with pytest.raises(UC.CaptureError, match="retCode"):
        run(tmp_path, Venue({"": env([], retCode=10001)}, []))
    bad = json.dumps({"retCode": 0, "result": {"category": "inverse", "list": []}, "time": NOW}).encode()
    with pytest.raises(UC.CaptureError, match="category"):
        run(tmp_path, Venue({"": bad}, []), name="run2")
    with pytest.raises(UC.CaptureError, match="UTF-8 JSON"):
        run(tmp_path, Venue({"": b"\xff\xfe"}, []), name="run3")


# ---------------------------------------------------------------- never overwrites / hosts / redirects

def test_a_capture_never_overwrites_and_needs_a_new_directory(tmp_path):
    run(tmp_path, Venue(three_pages(), all_tickers()))
    before = {f: open(tmp_path / "run1" / f, "rb").read() for f in os.listdir(tmp_path / "run1")}
    with pytest.raises(UC.CaptureError, match="not empty"):
        run(tmp_path, Venue(three_pages(), all_tickers()))
    assert before == {f: open(tmp_path / "run1" / f, "rb").read() for f in os.listdir(tmp_path / "run1")}
    with pytest.raises(UC.CaptureError, match="never overwritten"):
        UC._create_exclusive(str(tmp_path / "run1" / "manifest.json"), b"x")


def test_an_output_directory_that_is_a_link_is_refused(tmp_path):
    os.mkdir(tmp_path / "real")
    os.symlink(tmp_path / "real", tmp_path / "lnk")
    with pytest.raises(UC.CaptureError, match="link"):
        run(tmp_path, Venue(three_pages(), all_tickers()), name="lnk")


def test_wrong_host_path_and_non_https_registry_are_refused(monkeypatch):
    for url in ("https://evil.example/v5/market/instruments-info?category=linear", "https://api.bybit.com.evil.example/v5/market/tickers?category=linear",
                "https://api.bybit.com/v5/account/wallet-balance?category=linear", "http://api.bybit.com/v5/market/tickers?category=linear"):
        with pytest.raises(UC.CaptureError, match="refusing"):
            UC.http_fetch(url)                                          # refused before any socket is opened
    with pytest.raises(UC.CaptureError, match="pinned"):
        UC.request_url("/v5/order/create")
    monkeypatch.setattr(venues, "rest_for", lambda _n: "http://api.bybit.com")
    with pytest.raises(UC.CaptureError, match="https"):
        UC.request_url(UC.PATH_TICKERS)


def test_a_redirect_is_never_followed():
    h = UC._NoRedirect()
    assert h.redirect_request(None, None, 302, "Found", {}, "https://evil.example/") is None
    opener = urllib.request.build_opener(UC._NoRedirect)
    assert any(isinstance(x, UC._NoRedirect) for x in opener.handlers)
    assert not any(type(x) is urllib.request.HTTPRedirectHandler for x in opener.handlers)


def test_no_credential_header_or_signing_in_the_module():
    src = open(UC.__file__).read()
    for word in ("X-BAPI", "api_key", "apiKey", "hmac", "environ", "getenv", "POST"):
        assert word not in src, word


# ---------------------------------------------------------------- diff (hand-computed)

def snap(tmp_path, name, pages, tickers, t):
    m = run(tmp_path, Venue(pages, tickers), name=name, clock_ms=Clock(t))
    return UC.load_universe(os.path.join(str(tmp_path / name), m["universe"]["file"]))


def test_diff_listings_delistings_status_interval_and_cap_changes(tmp_path):
    a = snap(tmp_path, "a", {"": env([inst("BTCUSDT"), inst("ETHUSDT"), inst("OLDUSDT"), inst("SOLUSDT"), inst("XRPUSDT")])},
             [tick(s) for s in ("BTCUSDT", "ETHUSDT", "OLDUSDT", "SOLUSDT", "XRPUSDT")], NOW)
    b = snap(tmp_path, "b", {"": env([inst("BTCUSDT"), inst("ETHUSDT", fundingInterval=240), inst("NEWUSDT"), inst("SOLUSDT", status="Settling"),
                                      inst("XRPUSDT", upperFundingRate="0.01", lowerFundingRate="-0.01")])},
             [tick(s) for s in ("BTCUSDT", "ETHUSDT", "NEWUSDT", "SOLUSDT", "XRPUSDT")], NOW + 86_400_000)
    d = UC.diff_snapshots(a, b)
    assert d["listed"] == ["NEWUSDT"] and d["delisted"] == ["OLDUSDT"]
    assert d["status_changes"] == [{"symbol": "SOLUSDT", "from": "Trading", "to": "Settling"}]
    assert d["funding_interval_changes"] == [{"symbol": "ETHUSDT", "from_min": 480, "to_min": 240}]
    assert d["funding_bound_changes"] == [{"symbol": "XRPUSDT", "from": ["-0.00375", "0.00375"], "to": ["-0.01", "0.01"]}]
    assert d["other_changes"] == [] and d["old_count"] == 5 and d["new_count"] == 5 and d["gap_ms"] == 86_400_000


def test_diff_of_identical_snapshots_is_empty_and_duplicate_rows_are_fatal(tmp_path):
    a = snap(tmp_path, "a", three_pages(), all_tickers(), NOW)
    d = UC.diff_snapshots(a, a)
    assert not any(d[k] for k in ("listed", "delisted", "status_changes", "funding_interval_changes", "funding_bound_changes", "other_changes"))
    p = tmp_path / "dup.jsonl"
    row = json.dumps(next(iter(a.values())))
    p.write_text(row + "\n" + row + "\n")
    with pytest.raises(UC.CaptureError, match="duplicate symbol"):
        UC.load_universe(str(p))
    q = tmp_path / "bad.jsonl"
    q.write_text('{"symbol": "X"}\n')
    with pytest.raises(UC.CaptureError, match="schema"):
        UC.load_universe(str(q))


def test_cli_diff_and_protected_inputs_are_refused(tmp_path, capsys):
    m = run(tmp_path, Venue(three_pages(), all_tickers()))
    u = os.path.join(str(tmp_path / "run1"), m["universe"]["file"])
    assert UC.main(["diff", u, u]) == 0 and '"listed": []' in capsys.readouterr().out
    assert UC.main(["diff", str(tmp_path / "trading_state.db"), u]) == 2
    assert UC.main(["capture", "--out-dir", str(tmp_path / "run1")]) == 2         # not empty: refused before any network use


# ---------------------------------------------------------------- red-team hardening

@pytest.mark.parametrize("name,sample", [("_SYMBOL", "BTCUSDT"), ("_DEC", "1.5"), ("_SDEC", "-1.5"), ("_UINT", "123"), ("_CURSOR", "abc123")])
def test_every_pattern_refuses_a_trailing_newline(name, sample):
    pat = getattr(UC, name)
    assert pat.match(sample) and pat.fullmatch(sample)
    assert pat.match(sample + "\n") is None and pat.fullmatch(sample + "\n") is None


def test_a_cursor_or_symbol_with_a_trailing_newline_is_refused(tmp_path):
    with pytest.raises(UC.CaptureError):
        UC.request_url(UC.PATH_INSTRUMENTS, "abc\n")
    norm, why, sym = UC.normalize_instrument(inst("BTCUSDT\n"))
    assert norm is None
    m = run(tmp_path, Venue({"": env([inst("BTCUSDT")])}, [tick("BTCUSDT"), tick("BTCUSDT\n")]))      # an odd ticker symbol is REJECTED and counted, never fatal
    assert m["rejected_tickers"]["by_reason"] == {"bad_symbol": 1} and m["tickers"]["rows"] == 2


@pytest.mark.parametrize("bad", ["9" * 40, "1" + "0" * 16, "NaN", "-1", "1e5", "1_0"])
def test_a_poisoned_ticker_turnover_is_refused(tmp_path, bad):
    with pytest.raises(UC.CaptureError):
        run(tmp_path, Venue({"": env([inst("BTCUSDT")])}, [tick("BTCUSDT", turnover24h=bad)]))


def test_universe_http_fetch_consults_the_public_collector_capability(monkeypatch):
    import urllib.request as ur
    hits = []

    def boom(*a, **k):
        hits.append(a)
        raise AssertionError("opened")
    monkeypatch.setattr(ur.OpenerDirector, "open", boom)
    monkeypatch.setattr(ur, "build_opener", boom)
    base = venues.rest_for("mainnet")
    for method, url, headers, body in (("POST", f"{base}/v5/market/tickers?category=linear", None, None),
                                       ("GET", f"{base}/v5/market/tickers?category=linear", {"X-BAPI-SIGN": "s"}, None),
                                       ("GET", f"{base}/v5/market/tick\ners?category=linear", None, None),
                                       ("GET", f"{base}/v5/market/tickers?category=linear&sign=S", None, None)):
        with pytest.raises(UC.CaptureError):
            UC.http_request(method, url, headers=headers, body=body)
    assert hits == []


# =====================================================================================================================
# schema v2: ticker tolerance, status partitions, per-page evidence, reconciliation, verification, CLI, diff
# =====================================================================================================================

def chain(row_pages, category="linear", time_ms=NOW):
    d = {}
    for i, rows in enumerate(row_pages):
        d["" if i == 0 else f"p{i}"] = env(rows, f"p{i + 1}" if i < len(row_pages) - 1 else "", time_ms=time_ms, category=category)
    return d


class V2Venue:
    """Scripted venue keyed by (category, status): value is {cursor: bytes | Exception | [sequence]}. Unknown partitions answer an empty list."""
    def __init__(self, parts=None, tickers=None, tickers_time=NOW, tickers_raw=None):
        self.parts = {("linear", None): chain([[inst("BTCUSDT"), inst("ETHUSDT")]]), ("linear", "Trading"): chain([[inst("BTCUSDT"), inst("ETHUSDT")]])}
        self.parts.update(parts or {})
        self.tickers = [tick("BTCUSDT"), tick("ETHUSDT")] if tickers is None else tickers
        self.tickers_time = tickers_time
        self.tickers_raw = tickers_raw
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        path, _, qs = url.partition("?")
        q = {k: v[0] for k, v in urllib.parse.parse_qs(qs).items()}
        if path.endswith(UC.PATH_TICKERS):
            return self.tickers_raw if self.tickers_raw is not None else env(self.tickers, time_ms=self.tickers_time)
        entry = self.parts.get((q["category"], q.get("status")))
        if entry is None:
            return env([], category=q["category"])
        resp = entry[q.get("cursor", "")]
        if isinstance(resp, list):
            resp = resp.pop(0)
        if isinstance(resp, BaseException):
            raise resp
        return resp


def run2(tmp_path, venue, name="v2", sleeps=None, **kw):
    sl = [] if sleeps is None else sleeps
    kw.setdefault("clock_ms", Clock())
    return UC.capture_universe(str(tmp_path / name), fetch=venue, sleep=sl.append, **kw)


def outcomes(m):
    return {(p["category"], p["status"]): p["outcome"] for p in m["partitions"]}


def uni2(m, d):
    return [json.loads(x) for x in open(os.path.join(d, m["universe_file"]["file"]))]


REAL_MIX = [tick("BTCUSDT"), tick("ETHUSDT"), tick("BTC-27NOV26"), tick("BTCUSDt"), tick("1000PEPEUSDT"), tick("BTCPERP"), "junk", {"symbol": 5}, {"nosym": 1}]


# ---------------------------------------------------------------- 1. the tickers crash

def test_real_ticker_mix_is_counted_not_fatal_in_both_capture_paths(tmp_path):
    m = run2(tmp_path, V2Venue(tickers=REAL_MIX))
    rt = m["rejected_tickers"]
    assert m["tickers"]["rows"] == 9 and m["tickers"]["accepted"] == 4 and m["tickers"]["rejected_count"] == 5
    assert rt["count"] == 5 and rt["by_reason"] == {"bad_symbol": 1, "not_an_object": 1, "symbol_has_hyphen": 1, "symbol_not_string": 2} and not rt["list_capped"]
    dated = next(e for e in rt["listed"] if e["symbol"] == "BTC-27NOV26")
    assert dated["reason"] == "symbol_has_hyphen" and dated["row_sha256"] == UC._row_sha(REAL_MIX[2])
    m1 = run(tmp_path, Venue({"": env([inst("BTCUSDT")])}, REAL_MIX), name="legacy")          # the schema-1 capture used by market_collector too
    assert m1["rejected_tickers"]["count"] == 5 and m1["rows_accepted"] == 1


def test_positive_control_a_clean_ticker_set_has_no_rejects(tmp_path):
    m = run2(tmp_path, V2Venue())
    assert m["rejected_tickers"]["count"] == 0 and m["tickers"]["accepted"] == 2


def test_ticker_reject_list_is_capped_but_the_count_is_complete(tmp_path):
    rows = [tick("BTCUSDT")] + [tick(f"X{i}-1") for i in range(UC.MAX_REJECT_LIST + 50)]
    m = run2(tmp_path, V2Venue(tickers=rows))
    rt = m["rejected_tickers"]
    assert rt["count"] == UC.MAX_REJECT_LIST + 50 and len(rt["listed"]) == UC.MAX_REJECT_LIST and rt["list_capped"]


@pytest.mark.parametrize("rows", [[tick("BTCUSDT", markPrice=1.5)], [tick("BTCUSDT"), tick("BTCUSDT", markPrice="1")], [tick("BTCUSDT", turnover24h="9" * 40)],
                                  [tick("BTCUSDT", turnover24h="-1")]])
def test_accepted_ticker_symbols_still_fail_closed(tmp_path, rows):
    with pytest.raises(UC.CaptureError):
        run2(tmp_path, V2Venue(tickers=rows))


@pytest.mark.parametrize("raw", [b"\xff\xfe", b"[]", env([], retCode=10001), env([], "cur"), env([], category="inverse")])
def test_a_malformed_or_unsuccessful_ticker_envelope_fails_closed(tmp_path, raw):
    with pytest.raises(UC.CaptureError):
        run2(tmp_path, V2Venue(tickers_raw=raw))


# ---------------------------------------------------------------- 2. status partitions

def test_every_status_partition_is_requested_with_its_own_cursor_chain(tmp_path):
    v = V2Venue({("linear", "Trading"): chain([[inst("BTCUSDT")], [inst("ETHUSDT")]])})
    sleeps = []
    m = run2(tmp_path, v, sleeps=sleeps)
    base = venues.rest_for("mainnet") + "/v5/market/instruments-info?category=linear&limit=1000"
    assert v.urls == [base, base + "&status=Trading", base + "&status=Trading&cursor=p1", base + "&status=PendingOpen", base + "&status=PreLaunch",
                      base + "&status=Delivering", base + "&status=Closed", venues.rest_for("mainnet") + "/v5/market/tickers?category=linear"]
    assert [(p["category"], p["status"]) for p in m["partitions"]] == [("linear", None)] + [("linear", s) for s in UC.STATUSES]
    assert len(sleeps) == len(v.urls) - 1 and all(0 < x <= UC.MAX_BACKOFF_S for x in sleeps)        # polite pacing between every request
    assert m["schema"] == "universe_capture/v2" and m["categories"] == ["linear"]


def test_closed_refused_by_the_venue_is_an_outcome_not_an_exception(tmp_path):
    refusal = env([], retCode=10001, retMsg="params error: status")
    m = run2(tmp_path, V2Venue({("linear", "Closed"): {"": refusal}}))
    o = outcomes(m)[("linear", "Closed")]
    assert o == "REFUSED_BY_VENUE:10001/params error: status"
    part = next(p for p in m["partitions"] if p["status"] == "Closed")
    assert part["complete"] is False and part["pages"][0]["rows"] == 0
    assert open(tmp_path / "v2" / part["pages"][0]["file"], "rb").read() == refusal            # the refusal is kept byte for byte
    assert m["delisting_observability"] == "NOT_OBSERVED" and m["status_coverage"]["Closed"] == o and m["dataset_status"] == "COMPLETE"
    assert "NOT point-in-time" in m["coverage_note"] and "may omit old delistings" in m["coverage_note"]


def test_closed_observed_empty_and_observed_rows(tmp_path):
    m = run2(tmp_path, V2Venue())                       # unknown partitions answer an empty list
    assert outcomes(m)[("linear", "Closed")] == "OBSERVED_EMPTY" and m["delisting_observability"] == "OBSERVED"
    old = inst("OLDUSDT", status="Closed", deliveryTime="1700000000000", upperFundingRate="", lowerFundingRate="", symbolId="sid-old")
    m = run2(tmp_path, V2Venue({("linear", "Closed"): chain([[old]])}), name="v2b")
    assert outcomes(m)[("linear", "Closed")] == "OBSERVED_ROWS" and m["delisting_observability"] == "OBSERVED"
    rows = {r["symbol"]: r for r in uni2(m, str(tmp_path / "v2b"))}
    assert rows["OLDUSDT"]["status"] == "Closed" and rows["OLDUSDT"]["seen_in"] == ["linear:Closed"] and rows["OLDUSDT"]["deliveryTime"] == 1700000000000
    assert rows["BTCUSDT"]["seen_in"] == ["linear:Trading", "linear:default"] and not rows["BTCUSDT"]["conflict"]
    assert rows["OLDUSDT"]["upperFundingRate"] is None                                         # a closed row keeps what the venue sent, nothing invented


def test_a_venue_that_ignores_the_status_parameter_is_not_credited_as_observing_it(tmp_path):
    m = run2(tmp_path, V2Venue({("linear", "Closed"): chain([[inst("BTCUSDT"), inst("ETHUSDT")]])}))      # Trading rows come back for status=Closed
    assert outcomes(m)[("linear", "Closed")].startswith("REFUSED_BY_VENUE:status_param_not_honored(2 rows")
    assert m["delisting_observability"] == "NOT_OBSERVED"
    assert all(r["seen_in"] == ["linear:Trading", "linear:default"] for r in uni2(m, str(tmp_path / "v2")))


def test_retry_on_http_429_and_on_retcode_10006_with_injected_backoff(tmp_path):
    ok = env([inst("BTCUSDT")])
    sleeps = []
    v = V2Venue({("linear", "Trading"): {"": [UC.HttpStatusError(429), UC.HttpStatusError(429), ok]},
                 ("linear", "PendingOpen"): {"": [env([], retCode=10006, retMsg="Too many visits"), env([], category="linear")]}})
    m = run2(tmp_path, v, sleeps=sleeps)
    tr = next(p for p in m["partitions"] if p["status"] == "Trading")
    assert tr["outcome"] == "OBSERVED_ROWS" and tr["pages"][0]["attempts"] == 3
    assert [x for x in sleeps if x >= 1.0] == [1.0, 2.0, 1.0]                                  # exponential per request, then the 10006 retry
    po = next(p for p in m["partitions"] if p["status"] == "PendingOpen")
    assert po["outcome"] == "OBSERVED_EMPTY" and po["pages"][0]["attempts"] == 2


def test_exhausted_retries_are_recorded_not_raised(tmp_path):
    m = run2(tmp_path, V2Venue({("linear", "Closed"): {"": [UC.HttpStatusError(429)] * 9}}), retries=2)
    assert outcomes(m)[("linear", "Closed")] == "TRANSPORT_ERROR:HTTP_429"
    rl = env([], retCode=10006, retMsg="Too many visits")
    m = run2(tmp_path, V2Venue({("linear", "Closed"): {"": [rl] * 9}}), retries=1, name="v2b")
    assert outcomes(m)[("linear", "Closed")] == "REFUSED_BY_VENUE:10006/Too many visits" and m["partitions"][-1]["pages"][0]["attempts"] == 2


def test_transport_errors_and_malformed_bodies_are_outcomes(tmp_path):
    v = V2Venue({("linear", "Closed"): {"": UC.TransportFailure("reset", "ConnectionResetError")}, ("linear", "PendingOpen"): {"": b"not json"},
                 ("linear", "PreLaunch"): {"": OSError("boom")}})
    m = run2(tmp_path, v)
    o = outcomes(m)
    assert o[("linear", "Closed")] == "TRANSPORT_ERROR:ConnectionResetError" and o[("linear", "PendingOpen")] == "TRANSPORT_ERROR:MalformedEnvelope"
    assert o[("linear", "PreLaunch")] == "TRANSPORT_ERROR:OSError"
    assert m["dataset_status"] == "PARTIAL" and m["delisting_observability"] == "NOT_OBSERVED"
    assert next(p for p in m["partitions"] if p["status"] == "Closed")["pages"] == []


@pytest.mark.parametrize("pages,why", [
    ({"": env([inst("BTCUSDT")], "p1"), "p1": env([inst("ETHUSDT")], "p2"), "p2": env([inst("SOLUSDT")], "p1")}, "did not advance"),
    ({"": env([inst("BTCUSDT")], "p1"), "p1": env([inst("ETHUSDT")], "p1")}, "did not advance"),
    ({"": env([inst("BTCUSDT")], "p1"), "p1": env([], "p2")}, "empty page"),
    ({"": env([inst("BTCUSDT")], "p1"), "p1": UC.TransportFailure("x", "TimeoutError")}, "not exhausted"),
    ({"": env([inst(f"A{i}USDT") for i in range(UC.PAGE_LIMIT + 1)])}, "page limit"),
    ({"": env([inst("BTCUSDT")], "p1"), "p1": env([], retCode=10001, retMsg="boom")}, "retCode 10001"),
])
def test_a_broken_cursor_chain_is_partial_not_complete(tmp_path, pages, why):
    m = run2(tmp_path, V2Venue({("linear", "Trading"): pages}))
    t = next(p for p in m["partitions"] if p["status"] == "Trading")
    assert t["outcome"] == "PARTIAL" and not t["complete"] and why in t["error"]
    assert m["dataset_status"] == "PARTIAL" and "manifest.json" in os.listdir(tmp_path / "v2")
    assert UC.verify_capture_dir(str(tmp_path / "v2"))["ok"]                                    # honest PARTIAL verifies as exactly that


def test_the_page_cap_with_a_cursor_pending_is_partial(tmp_path):
    v = V2Venue({("linear", "Trading"): chain([[inst("BTCUSDT")], [inst("ETHUSDT")], [inst("SOLUSDT")]])})
    m = run2(tmp_path, v, max_pages=2)
    t = next(p for p in m["partitions"] if p["status"] == "Trading")
    assert t["outcome"] == "PARTIAL" and len(t["pages"]) == 2 and m["dataset_status"] == "PARTIAL"
    assert sum(1 for u in v.urls if "status=Trading" in u) == 2


def test_a_hostile_cursor_never_becomes_a_url_and_an_oversize_body_is_fatal(tmp_path, monkeypatch):
    v = V2Venue({("linear", "Trading"): {"": env([inst("BTCUSDT")], "a b")}})
    m = run2(tmp_path, v)
    assert outcomes(m)[("linear", "Trading")] == "TRANSPORT_ERROR:MalformedEnvelope" and not any("a b" in u or "a%20b" in u for u in v.urls)
    monkeypatch.setattr(UC, "MAX_BODY_BYTES", 100)
    with pytest.raises(UC.CaptureError, match="larger"):
        run2(tmp_path, V2Venue(), name="big")


def test_arguments_are_validated(tmp_path):
    for kw in (dict(categories=["option"]), dict(statuses=["Settling"]), dict(categories=[])):
        with pytest.raises(UC.CaptureError):
            run2(tmp_path, V2Venue(), **kw)
    for bad in ({"status": "Closed;x"}, {"status": "Settling"}, {"category": "option"}):
        with pytest.raises(UC.CaptureError):
            UC.request_url(UC.PATH_INSTRUMENTS, **bad)
    with pytest.raises(UC.CaptureError):
        UC.request_url(UC.PATH_TICKERS, status="Closed")
    with pytest.raises(UC.CaptureError):
        UC.request_url(UC.PATH_INSTRUMENTS, category="spot", status="Trading")


def test_a_capability_refusal_is_fatal_not_an_outcome(tmp_path):
    def refuse(_url):
        raise UC.CapabilityRefusal("role refused")
    with pytest.raises(UC.CapabilityRefusal):
        run2(tmp_path, refuse)


def test_categories_inverse_and_spot_spot_is_one_unpaginated_request(tmp_path):
    inv = inst("BTCUSD", contractType="InversePerpetual", quoteCoin="USD", settleCoin="BTC", symbolId="inv-1")
    spot = {"symbol": "BTCUSDT", "symbolId": "spot-1", "status": "Trading", "baseCoin": "BTC", "quoteCoin": "USDT", "innovation": "0",
            "priceFilter": {"tickSize": "0.01"}, "lotSizeFilter": {"basePrecision": "0.000001", "minOrderQty": "0.000048"}}
    v = V2Venue({("inverse", None): chain([[inv]], "inverse"), ("inverse", "Trading"): chain([[inv]], "inverse"), ("spot", None): {"": env([spot], category="spot")}})
    m = run2(tmp_path, v, categories=["linear", "inverse", "spot"])
    base = venues.rest_for("mainnet") + "/v5/market/instruments-info"
    assert v.urls.count(base + "?category=spot") == 1 and not any("category=spot&" in u for u in v.urls)
    assert [(p["category"], p["status"]) for p in m["partitions"] if p["category"] == "spot"] == [("spot", None)]
    assert sum(1 for u in v.urls if "tickers" in u) == 1
    rows = {(r["category"], r["symbol"]): r for r in uni2(m, str(tmp_path / "v2"))}
    assert rows[("inverse", "BTCUSD")]["fundingInterval_min"] == 480 and rows[("spot", "BTCUSDT")]["lotSizeFilter"]["basePrecision"] == "0.000001"
    assert m["status_coverage_by_category"]["spot"] == {"default": "OBSERVED_ROWS"} and m["dataset_status"] == "COMPLETE"
    assert UC.verify_capture_dir(str(tmp_path / "v2"))["ok"]
    cursor_spot = V2Venue({("spot", None): {"": env([spot], "more", category="spot")}})
    m = run2(tmp_path, cursor_spot, name="v2c", categories=["spot"])
    assert outcomes(m)[("spot", None)] == "PARTIAL" and sum(1 for u in cursor_spot.urls if "spot" in u) == 1 and m["tickers"] is None


# ---------------------------------------------------------------- 3. evidence per page and every field

def test_every_page_is_preserved_byte_for_byte_with_its_request_facts(tmp_path):
    pages = chain([[inst("BTCUSDT")], [inst("ETHUSDT")]])
    m = run2(tmp_path, V2Venue({("linear", "Trading"): pages}))
    d = tmp_path / "v2"
    t = next(p for p in m["partitions"] if p["status"] == "Trading")
    assert t["params"] == {"category": "linear", "limit": 1000, "status": "Trading"} and t["rows_total"] == 2
    for pg, cur in zip(t["pages"], ("", "p1")):
        raw = open(d / pg["file"], "rb").read()
        assert raw == pages[cur] and UC._sha(raw) == pg["sha256"] and pg["bytes"] == len(raw) and stat.S_IMODE(os.stat(d / pg["file"]).st_mode) == 0o600
        assert {"n", "file", "sha256", "bytes", "cursor_in", "cursor_out", "rows", "server_time_ms", "recv_ms"} <= set(pg)
        assert pg["server_time_ms"] == NOW and pg["cursor_in"] == cur and pg["params"] == t["params"]
    assert [p["cursor_out"] for p in t["pages"]] == ["p1", ""] and [p["n"] for p in t["pages"]] == [1, 2]
    assert m["tickers"]["file"] == "tickers.json" and m["tickers"]["sha256"] == UC._sha(open(d / "tickers.json", "rb").read())
    assert m["universe_file"]["sha256"] == UC._sha(open(d / m["universe_file"]["file"], "rb").read())
    assert stat.S_IMODE(os.stat(d / "manifest.json").st_mode) == 0o600
    with pytest.raises(UC.CaptureError, match="not empty"):
        run2(tmp_path, V2Venue())


def test_every_documented_instrument_field_is_normalised(tmp_path):
    pre = inst("NEWUSDT", status="PreLaunch", isPreListing=True, preListingInfo={"curAuctionPhase": "CallAuction", "phases": []}, fundingInterval=240, symbolType="innovation")
    m = run2(tmp_path, V2Venue({("linear", "PreLaunch"): chain([[pre]])}))
    rows = {r["symbol"]: r for r in uni2(m, str(tmp_path / "v2"))}
    btc = rows["BTCUSDT"]
    want = {"schema": 2, "category": "linear", "symbolId": "id-BTCUSDT", "contractType": "LinearPerpetual", "status": "Trading", "baseCoin": "BTC", "quoteCoin": "USDT",
            "settleCoin": "USDT", "symbolType": "", "launchTime": 1585526400000, "deliveryTime": 0, "deliveryFeeRate": None, "fundingInterval_min": 480,
            "upperFundingRate": "0.00375", "lowerFundingRate": "-0.00375", "priceScale": 2, "minLeverage": "1", "maxLeverage": "100.00", "leverageStep": "0.01",
            "tickSize": "0.10", "minPrice": "0.10", "maxPrice": "199999.80", "minOrderQty": "0.001", "maxOrderQty": "1190", "maxMktOrderQty": "500",
            "postOnlyMaxOrderQty": "1190", "qtyStep": "0.001", "minNotionalValue": "5", "isPreListing": False, "preListingInfo": None, "priceLimitRatioX": "0.01",
            "priceLimitRatioY": "0.02", "unifiedMarginTrade": True, "copyTrading": "both", "ticker": btc["ticker"]}
    assert {k: btc[k] for k in want} == want and btc["ticker"]["fundingCap"] == "0.00375"
    n = rows["NEWUSDT"]
    assert n["status"] == "PreLaunch" and n["isPreListing"] is True and n["preListingInfo"]["curAuctionPhase"] == "CallAuction" and n["fundingInterval_min"] == 240
    assert n["symbolType"] == "innovation" and n["seen_in"] == ["linear:PreLaunch"]


@pytest.mark.parametrize("over", [dict(upperFundingRate="-0.1"), dict(fundingInterval=0), dict(fundingInterval=100000), dict(symbolId="a b"), dict(isPreListing="yes"),
                                  dict(leverageFilter={"maxLeverage": "abc"}), dict(priceFilter={"tickSize": "0"}), dict(launchTime="-1"), dict(symbolId=True),
                                  dict(preListingInfo="x"), dict(copyTrading="x" * 40), dict(priceScale="n")])
def test_v2_hostile_field_values_reject_the_row_with_a_reason(tmp_path, over):
    m = run2(tmp_path, V2Venue({("linear", "Trading"): chain([[inst("BTCUSDT", **over), inst("ETHUSDT")]])}))
    assert m["rejected_instruments"]["by_reason"] == {"invalid_field": 1} and m["rejected_instruments"]["listed"][0]["symbol"] == "BTCUSDT"
    assert [r["symbol"] for r in uni2(m, str(tmp_path / "v2"))] == ["ETHUSDT"] or all(r["symbol"] != "BTCUSDT" or r["seen_in"] == ["linear:default"] for r in uni2(m, str(tmp_path / "v2")))


def test_v2_positive_control_and_non_perpetual_rows_are_counted_once(tmp_path):
    rec, why, sym = UC.normalize_instrument_v2(inst("BTCUSDT"), "linear")
    assert why is None and sym == "BTCUSDT" and rec["fundingInterval_min"] == 480
    dated = inst("BTC-27NOV26", contractType="LinearFutures", deliveryTime="1798329600000")
    fut = inst("BTCUSDZ26", contractType="LinearFutures", deliveryTime="1798329600000")
    rows = [inst("BTCUSDT"), dated, fut, inst("SOLPERP", quoteCoin="USDC", settleCoin="USDC"), "junk", inst("ETHUSDT", fundingInterval=240)]
    pages = {("linear", None): chain([rows]), ("linear", "Trading"): chain([rows])}
    m = run2(tmp_path, V2Venue(pages))
    ri = m["rejected_instruments"]
    assert ri["count"] == 4 and ri["by_reason"] == {"bad_symbol": 1, "not_an_object": 1, "not_perpetual": 1, "not_usdt": 1} and not ri["list_capped"]   # a row in two partitions counts once
    assert m["excluded_contract_types"] == {"linear:LinearFutures": 1}                         # futures are kept out of the perpetual universe, counted, never silent
    assert part_of(m, "Trading")["by_contract_type"] == {"<not_an_object>": 1, "LinearFutures": 2, "LinearPerpetual": 3}
    assert next(e for e in ri["listed"] if e["symbol"] == "BTC-27NOV26")["partitions"] == ["linear:Trading", "linear:default"]


# ---------------------------------------------------------------- 4. reconciliation

def test_a_status_transition_between_partitions_is_a_conflict_with_exact_keys(tmp_path):
    v = V2Venue({("linear", "Delivering"): chain([[inst("BTCUSDT", status="Delivering")]])})
    m = run2(tmp_path, v)
    assert m["dataset_status"] == "CONFLICTED"
    assert m["conflicts"] == [{"kind": "symbol_variants", "category": "linear", "symbol": "BTCUSDT", "fields": ["status"], "symbolIds": ["id-BTCUSDT"],
                               "variants": [{"status": "Delivering", "seen_in": ["linear:Delivering"]}, {"status": "Trading", "seen_in": ["linear:Trading", "linear:default"]}]}]
    rows = [r for r in uni2(m, str(tmp_path / "v2")) if r["symbol"] == "BTCUSDT"]
    assert len(rows) == 2 and all(r["conflict"] for r in rows)                                  # both variants kept, none chosen
    with pytest.raises(UC.CaptureError, match="duplicate symbol"):
        UC.load_universe(os.path.join(str(tmp_path / "v2"), m["universe_file"]["file"]))
    assert UC.verify_capture_dir(str(tmp_path / "v2"))["ok"]


def test_one_symbolid_on_two_symbols_and_in_partition_conflicts(tmp_path):
    v = V2Venue({("linear", "Trading"): chain([[inst("BTCUSDT"), inst("XBTUSDT", symbolId="id-BTCUSDT")]])})
    m = run2(tmp_path, v)
    assert m["dataset_status"] == "CONFLICTED"
    assert {"kind": "symbolId_multiple_symbols", "category": "linear", "symbolId": "id-BTCUSDT", "symbols": ["BTCUSDT", "XBTUSDT"]} in m["conflicts"]
    v = V2Venue({("linear", "Trading"): chain([[inst("BTCUSDT"), inst("BTCUSDT", maxLeverage=1, leverageFilter={"minLeverage": "1", "maxLeverage": "50", "leverageStep": "0.01"})]])})
    m = run2(tmp_path, v, name="v2b")
    c = m["conflicts"][0]
    assert m["dataset_status"] == "CONFLICTED" and c["fields"] == ["maxLeverage"] and {"linear:Trading"} <= {x for v in c["variants"] for x in v["seen_in"]}
    assert run2(tmp_path, V2Venue({("linear", "Trading"): chain([[inst("BTCUSDT"), inst("BTCUSDT"), inst("ETHUSDT")]])}), name="v2c")["dataset_status"] == "COMPLETE"


def test_stale_and_unverified_datasets(tmp_path):
    m = run2(tmp_path, V2Venue(tickers_time=NOW - 60_000), clock_ms=Clock(NOW + 60_000))
    assert m["dataset_status"] == "STALE"
    slow = Clock()
    ticks = iter(range(NOW, NOW + 10 ** 9, UC.MAX_SPAN_MS))
    m = run2(tmp_path, V2Venue(), name="v2b", clock_ms=lambda: next(ticks))
    assert m["dataset_status"] == "STALE"                                                        # responses further apart than one point in time
    nov = V2Venue({("linear", None): chain([[inst("BTCUSDT")]], time_ms=None)})
    assert run2(tmp_path, nov, name="v2c", clock_ms=slow)["dataset_status"] == "UNVERIFIED"


def test_a_missing_core_partition_is_partial(tmp_path):
    m = run2(tmp_path, V2Venue(), statuses=["Closed"])
    assert m["dataset_status"] == "PARTIAL" and outcomes(m)[("linear", "Closed")] == "OBSERVED_EMPTY"


# ---------------------------------------------------------------- 5. verification

def good_capture(tmp_path, name="good"):
    old = inst("OLDUSDT", status="Closed", deliveryTime="1700000000000", symbolId="sid-old")
    v = V2Venue({("linear", "Trading"): chain([[inst("BTCUSDT")], [inst("ETHUSDT")]]), ("linear", None): chain([[inst("BTCUSDT")], [inst("ETHUSDT")]]),
                 ("linear", "Closed"): chain([[old]])}, tickers=[tick("BTCUSDT"), tick("ETHUSDT"), tick("BTC-27NOV26")])
    m = run2(tmp_path, v, name=name)
    return m, str(tmp_path / name)


def test_verify_accepts_an_honest_capture_and_returns_the_documented_schema(tmp_path):
    m, d = good_capture(tmp_path)
    r = UC.verify_capture_dir(d)
    assert r["ok"] is True and r["problems"] == [] and set(r) == {"ok", "problems", "coverage", "manifest_sha256"}
    assert r["manifest_sha256"] == UC._sha(open(os.path.join(d, "manifest.json"), "rb").read())
    c = r["coverage"]
    assert c["dataset_status"] == "COMPLETE" and c["dataset_status_claimed"] == "COMPLETE" and c["delisting_observability"] == "OBSERVED"
    assert c["status_coverage"]["Closed"] == "OBSERVED_ROWS" and c["status_coverage_by_category"]["linear"]["default"] == "OBSERVED_ROWS"
    assert c["universe_rows"] == 3 and c["rejected_tickers_count"] == 1 and c["rejected_instruments_count"] == 0 and c["conflicts"] == []
    assert {"category": "linear", "status": "Closed", "outcome": "OBSERVED_ROWS", "rows_total": 1, "complete": True, "pages": 1, "by_contract_type": {"LinearPerpetual": 1}} in c["partitions"]
    assert UC.verify_capture_dir(d) == r                                                          # pure: a second call changes nothing


def mutate_copy(tmp_path, src, fn, name):
    dst = str(tmp_path / name)
    shutil.copytree(src, dst)
    fn(dst)
    return UC.verify_capture_dir(dst)


def edit_manifest(fn):
    def go(d):
        p = os.path.join(d, "manifest.json")
        m = json.load(open(p))
        fn(m)
        open(p, "w").write(json.dumps(m, indent=2, sort_keys=True))
    return go


def refresh_sha(d, fname):
    p = os.path.join(d, "manifest.json")
    m = json.load(open(p))
    raw = open(os.path.join(d, fname), "rb").read()
    for part in m["partitions"]:
        for pg in part["pages"]:
            if pg["file"] == fname:
                pg["sha256"], pg["bytes"] = UC._sha(raw), len(raw)
    open(p, "w").write(json.dumps(m, indent=2, sort_keys=True))


def part_of(m, status):
    return next(p for p in m["partitions"] if p["status"] == status)


def test_verify_flags_every_kind_of_tampering(tmp_path):
    _m, d = good_capture(tmp_path)

    def flip(dd):
        f = os.path.join(dd, "linear_Trading_page0001.json")
        b = bytearray(open(f, "rb").read())
        b[-3] ^= 1
        open(f, "wb").write(bytes(b))

    def swap_closed_for_trading_rows(dd):                       # the attacker keeps every hash consistent and lies about coverage
        f = os.path.join(dd, "linear_Closed_page0001.json")
        open(f, "wb").write(env([inst("BTCUSDT")]))
        refresh_sha(dd, "linear_Closed_page0001.json")

    def empty_closed_but_claim_rows(dd):
        f = os.path.join(dd, "linear_Closed_page0001.json")
        open(f, "wb").write(env([]))
        refresh_sha(dd, "linear_Closed_page0001.json")

    def forge_universe(dd):                                      # universe edited AND its hash in the manifest fixed up: only re-derivation can catch it
        f = os.path.join(dd, _m["universe_file"]["file"])
        txt = open(f).read().replace('"status":"Closed"', '"status":"Trading"')
        open(f, "w").write(txt)
        edit_manifest(lambda m: m["universe_file"].update(sha256=UC._sha(txt.encode())))(dd)

    cases = {
        "forged_universe": (forge_universe, "differs from the universe re-derived"),
        "flip": (flip, "modified"),
        "missing": (lambda dd: os.remove(os.path.join(dd, "linear_Trading_page0002.json")), "missing"),
        "extra": (lambda dd: open(os.path.join(dd, "stray.json"), "w").write("{}"), "extra file"),
        "universe": (lambda dd: open(os.path.join(dd, _m["universe_file"]["file"]), "ab").write(b"x"), "modified"),
        "tickers": (lambda dd: open(os.path.join(dd, "tickers.json"), "ab").write(b" "), "modified"),
        "swap": (swap_closed_for_trading_rows, "status_param_not_honored"),
        "empty": (empty_closed_but_claim_rows, "raw pages give"),
        "outcome": (edit_manifest(lambda m: part_of(m, "Closed").update(outcome="OBSERVED_EMPTY")), "raw pages give"),
        "rows_total": (edit_manifest(lambda m: part_of(m, "Trading").update(rows_total=9)), "rows_total"),
        "page_rows": (edit_manifest(lambda m: part_of(m, "Trading")["pages"][0].update(rows=7)), "rows 7"),
        "cursor_in": (edit_manifest(lambda m: part_of(m, "Trading")["pages"][1].update(cursor_in="zzz")), "does not continue the chain"),
        "cursor_out": (edit_manifest(lambda m: part_of(m, "Trading")["pages"][0].update(cursor_out="")), "cursor_out"),
        "complete": (edit_manifest(lambda m: part_of(m, "Closed").update(complete=False)), "complete flag"),
        "coverage": (edit_manifest(lambda m: m["status_coverage"].update(PreLaunch="OBSERVED_ROWS")), "status_coverage claim"),
        "delisting": (edit_manifest(lambda m: m.update(delisting_observability="NOT_OBSERVED")), "delisting_observability claim"),
        "dataset": (edit_manifest(lambda m: m.update(dataset_status="PARTIAL")), "dataset_status claim"),
        "rejected": (edit_manifest(lambda m: m["rejected_tickers"].update(count=0)), "rejected_tickers"),
        "conflicts": (edit_manifest(lambda m: m.update(conflicts=[{"kind": "x"}])), "conflicts differ"),
        "traversal": (edit_manifest(lambda m: part_of(m, "Trading")["pages"][0].update(file="../x.json")), "pinned name"),
        "params": (edit_manifest(lambda m: part_of(m, "Closed").update(params={"category": "linear", "limit": 1000})), "params"),
        "note": (edit_manifest(lambda m: m.update(coverage_note="all good")), "coverage_note"),
        "transport_claim": (edit_manifest(lambda m: part_of(m, "Closed").update(outcome="TRANSPORT_ERROR:Timeout")), "raw pages give"),
        "no_pages": (edit_manifest(lambda m: part_of(m, "Closed").update(pages=[])), "raw pages give"),
        "schema": (edit_manifest(lambda m: m.update(schema="universe_capture/v1")), "not universe_capture/v2"),
        "key_gone": (edit_manifest(lambda m: m.pop("rejected_tickers")), "aborted"),
        "manifest_garbage": (lambda dd: open(os.path.join(dd, "manifest.json"), "w").write("{"), "aborted"),
        "manifest_gone": (lambda dd: os.remove(os.path.join(dd, "manifest.json")), "missing"),
    }
    for name, (fn, needle) in cases.items():
        r = mutate_copy(tmp_path, d, fn, f"t_{name}")
        assert not r["ok"] and any(needle in p for p in r["problems"]), (name, r["problems"])
        assert r["coverage"].get("dataset_status", "UNVERIFIED") == "UNVERIFIED" and r["coverage"].get("delisting_observability", "NOT_OBSERVED") == "NOT_OBSERVED", name


def test_verify_never_raises_and_refuses_protected_or_missing_paths(tmp_path):
    for bad in (str(tmp_path / "nope"), str(tmp_path / "trading_state.db"), str(tmp_path)):
        r = UC.verify_capture_dir(bad)
        assert r["ok"] is False and r["problems"] and r["manifest_sha256"] is None
    os.symlink(tmp_path, tmp_path / "lnk")
    assert UC.verify_capture_dir(str(tmp_path / "lnk"))["ok"] is False
    _m, d = good_capture(tmp_path)
    os.mkdir(os.path.join(d, "sub"))
    assert any("not a regular file" in p or "extra file" in p for p in UC.verify_capture_dir(d)["problems"])


def test_verify_supports_a_broken_chain_only_as_partial_and_a_transport_error_only_as_a_negative(tmp_path):
    v = V2Venue({("linear", "Closed"): {"": UC.TransportFailure("x", "TimeoutError")}})
    m = run2(tmp_path, v)
    r = UC.verify_capture_dir(str(tmp_path / "v2"))
    assert r["ok"] and r["coverage"]["status_coverage"]["Closed"] == "TRANSPORT_ERROR:TimeoutError" and r["coverage"]["delisting_observability"] == "NOT_OBSERVED"
    assert r["coverage"]["dataset_status"] == "PARTIAL" and m["dataset_status"] == "PARTIAL"


# ---------------------------------------------------------------- 7. CLI

def cli(tmp_path, args, venue=None, capsys=None, **kw):
    v = venue or V2Venue()
    rc = UC.main(args, fetch=v, clock_ms=Clock(), sleep=lambda _s: None, **kw)
    return rc, (capsys.readouterr().out if capsys else "")


def test_cli_capture_prints_a_machine_readable_summary_and_verify_wraps_the_verifier(tmp_path, capsys):
    out = str(tmp_path / "cap")
    rc, text = cli(tmp_path, ["capture", "--out-dir", out], capsys=capsys)
    s = json.loads(text)
    assert rc == 0 and s["dataset_status"] == "COMPLETE" and s["delisting_observability"] == "OBSERVED" and s["status_coverage"]["Closed"] == "OBSERVED_EMPTY"
    assert s["manifest"] == os.path.join(out, "manifest.json") and s["conflicts"] == [] and set(UC.STATUSES) | {"default"} == set(s["status_coverage"])
    rc, text = cli(tmp_path, ["verify", "--dir", out], capsys=capsys)
    assert rc == 0 and json.loads(text)["ok"] is True
    open(os.path.join(out, "stray"), "w").write("x")
    rc, text = cli(tmp_path, ["verify", "--dir", out], capsys=capsys)
    assert rc == 1 and json.loads(text)["ok"] is False


def test_cli_exit_codes_for_conflicted_and_partial_and_subsets(tmp_path, capsys):
    conflicted = V2Venue({("linear", "Delivering"): chain([[inst("BTCUSDT", status="Delivering")]])})
    rc, text = cli(tmp_path, ["capture", "--out-dir", str(tmp_path / "c1")], conflicted, capsys)
    assert rc == 3 and json.loads(text)["dataset_status"] == "CONFLICTED"
    partial = V2Venue({("linear", "Trading"): {"": UC.TransportFailure("x", "TimeoutError")}})
    rc, text = cli(tmp_path, ["capture", "--out-dir", str(tmp_path / "c2")], partial, capsys)
    assert rc == 3 and json.loads(text)["dataset_status"] == "PARTIAL"
    v = V2Venue({("inverse", None): chain([[]], "inverse"), ("inverse", "Trading"): chain([[]], "inverse")})
    rc, text = cli(tmp_path, ["capture", "--out-dir", str(tmp_path / "c3"), "--categories", "linear,inverse", "--statuses", "Trading,Closed"], v, capsys)
    assert rc == 0 and sorted(json.loads(text)["status_coverage_by_category"]) == ["inverse", "linear"] and not any("PendingOpen" in u for u in v.urls)
    for bad in (["--categories", "futures"], ["--statuses", "Open"]):
        rc, _t = cli(tmp_path, ["capture", "--out-dir", str(tmp_path / "c4")] + bad, capsys=capsys)
        assert rc == 2


def test_cli_refuses_an_out_dir_inside_the_repo_or_a_git_work_tree(tmp_path, capsys):
    def never(_u):
        raise AssertionError("a refused capture must not touch the network")
    inside_repo = os.path.join(os.path.dirname(BOT), "dataset_out")
    assert UC.main(["capture", "--out-dir", inside_repo], fetch=never) == 2 and not os.path.exists(inside_repo)
    wt = tmp_path / "work"
    os.makedirs(wt / ".git")
    assert UC.main(["capture", "--out-dir", str(wt / "sub" / "cap")], fetch=never) == 2
    assert UC.main(["capture", "--out-dir", str(wt / "cap")], fetch=never) == 2
    rc, _t = cli(tmp_path, ["capture", "--out-dir", str(wt / "cap"), "--allow-inside-repo-for-tests"], capsys=capsys)
    assert rc == 0 and os.path.isfile(wt / "cap" / "manifest.json")


def test_cli_private_root_must_be_private_and_contain_the_out_dir(tmp_path, capsys):
    def never(_u):
        raise AssertionError("refused before the network")
    root = tmp_path / "priv"
    os.mkdir(root, 0o700)
    os.chmod(root, 0o700)
    assert UC.main(["capture", "--out-dir", str(tmp_path / "elsewhere"), "--private-root", str(root)], fetch=never) == 2
    rc, _t = cli(tmp_path, ["capture", "--out-dir", str(root / "cap"), "--private-root", str(root)], capsys=capsys)
    assert rc == 0
    open_root = tmp_path / "open"
    os.mkdir(open_root)
    os.chmod(open_root, 0o755)
    assert UC.main(["capture", "--out-dir", str(open_root / "cap"), "--private-root", str(open_root)], fetch=never) == 2
    os.symlink(root, tmp_path / "rl")
    assert UC.main(["capture", "--out-dir", str(root / "c2"), "--private-root", str(tmp_path / "rl")], fetch=never) == 2


# ---------------------------------------------------------------- diff across snapshots

def snap2(tmp_path, name, venue, t):
    m = run2(tmp_path, venue, name=name, clock_ms=Clock(t))
    return UC.load_universe(os.path.join(str(tmp_path / name), m["universe_file"]["file"]))


def test_diff_status_transitions_renamed_symbols_and_changed_rules(tmp_path):
    def both(rows):
        return {("linear", None): chain([rows]), ("linear", "Trading"): chain([[r for r in rows if r["status"] == "Trading"]])}
    a = snap2(tmp_path, "a", V2Venue(both([inst("BTCUSDT"), inst("ETHUSDT"), inst("OLDUSDT", symbolId="sidX")]), tickers=[]), NOW)
    b = snap2(tmp_path, "b", V2Venue({("linear", None): chain([[
        inst("BTCUSDT", priceFilter={"tickSize": "0.5", "minPrice": "0.5"}, leverageFilter={"minLeverage": "1", "maxLeverage": "50", "leverageStep": "0.01"}, fundingInterval=240),
        inst("ETHUSDT", status="Delivering"), inst("NEWUSDT", symbolId="sidX")]]),
        ("linear", "Trading"): chain([[inst("BTCUSDT", priceFilter={"tickSize": "0.5", "minPrice": "0.5"}, leverageFilter={"minLeverage": "1", "maxLeverage": "50", "leverageStep": "0.01"},
                                           fundingInterval=240), inst("NEWUSDT", symbolId="sidX")]]),
        ("linear", "Delivering"): chain([[inst("ETHUSDT", status="Delivering")]])}, tickers=[]), NOW + 1000)
    d = UC.diff_snapshots(a, b)
    assert d["listed"] == ["NEWUSDT"] and d["delisted"] == ["OLDUSDT"]
    assert d["renames"] == [{"symbolId": "sidX", "from": "OLDUSDT", "to": "NEWUSDT"}]
    assert d["status_changes"] == [{"symbol": "ETHUSDT", "from": "Trading", "to": "Delivering"}] and d["status_transitions"] == {"Trading->Delivering": 1}
    assert d["funding_interval_changes"] == [{"symbol": "BTCUSDT", "from_min": 480, "to_min": 240}]
    assert d["other_changes"] == [{"symbol": "BTCUSDT", "field": "maxLeverage", "from": "100.00", "to": "50"},
                                  {"symbol": "BTCUSDT", "field": "maxPrice", "from": "199999.80", "to": None},
                                  {"symbol": "BTCUSDT", "field": "minPrice", "from": "0.10", "to": "0.5"}, {"symbol": "BTCUSDT", "field": "tickSize", "from": "0.10", "to": "0.5"}]
    assert d["symbolid_changes"] == [] and d["gap_ms"] == 1000
    c = snap2(tmp_path, "c", V2Venue({("linear", None): chain([[inst("BTCUSDT", priceFilter={"tickSize": "0.5", "minPrice": "0.5"}, leverageFilter={"minLeverage": "1", "maxLeverage": "50", "leverageStep": "0.01"},
                                                                  fundingInterval=240), inst("ETHUSDT", status="Closed"), inst("NEWUSDT", symbolId="sidX")]]),
                                     ("linear", "Closed"): chain([[inst("ETHUSDT", status="Closed")]]), ("linear", "Trading"): chain([[]])}, tickers=[]), NOW + 2000)
    assert UC.diff_snapshots(b, c)["status_transitions"] == {"Delivering->Closed": 1}


def test_diff_symbolid_change_and_v1_v2_snapshots_do_not_create_noise(tmp_path):
    a = snap2(tmp_path, "a", V2Venue(tickers=[]), NOW)
    b = copy.deepcopy(a)
    b["BTCUSDT"]["symbolId"] = "other"
    assert UC.diff_snapshots(a, b)["symbolid_changes"] == [{"symbol": "BTCUSDT", "from": "id-BTCUSDT", "to": "other"}]
    legacy = snap(tmp_path, "legacy", {"": env([inst("BTCUSDT"), inst("ETHUSDT")])}, [tick("BTCUSDT"), tick("ETHUSDT")], NOW)
    d = UC.diff_snapshots(legacy, a)
    assert d["other_changes"] == [] and d["listed"] == [] and d["delisted"] == [] and d["status_changes"] == []


# ---------------------------------------------------------------- 6. the collector role

def test_v2_requests_pass_the_public_collector_role_and_nothing_else_does(tmp_path, monkeypatch):
    seen = []

    def fake(self, method, url, *, headers, body, timeout):
        seen.append((method, url, dict(headers), body))
        return b"{}"
    monkeypatch.setattr(UC._UrllibTransport, "request", fake)
    base = venues.rest_for("mainnet")
    for st, cur in ((None, None), ("Closed", None), ("Closed", "abc123"), ("PreLaunch", None)):
        assert UC.http_request("GET", UC.request_url(UC.PATH_INSTRUMENTS, cur, status=st)) == b"{}"
    assert UC.http_request("GET", UC.request_url(UC.PATH_INSTRUMENTS, category="spot")) == b"{}"
    assert all(m == "GET" and u.startswith(base + "/v5/market/instruments-info?") and b is None and not any(k.lower().startswith(("x-bapi", "auth")) for k in h) for m, u, h, b in seen)
    for method, url in (("POST", seen[0][1]), ("GET", base + "/v5/market/instruments-info?category=linear&status=Closed&sign=x"), ("GET", base + "/v5/position/list?category=linear")):
        with pytest.raises(UC.CaptureError):
            UC.http_request(method, url)
    assert len(seen) == 5


def test_http_status_errors_keep_the_venue_body_for_the_caller(monkeypatch):
    import io

    class FakeOpener:
        def open(self, req, timeout):
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {}, io.BytesIO(b'{"retCode":10006}'))
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_a: FakeOpener())
    with pytest.raises(UC.HttpStatusError) as ei:
        UC._UrllibTransport().request("GET", "https://x", headers={}, body=None, timeout=1)
    assert ei.value.status == 429 and ei.value.cls == "HTTP_429" and ei.value.body == b'{"retCode":10006}'


# ---------------------------------------------------------------- owner-observed live shapes (fixtures built from the owner's probe; this repo cannot verify them)

PCT = "first%3D10000000AIDOGEUS"          # OWNER-OBSERVED: the venue's cursor arrives already percent-encoded


def test_a_percent_encoded_cursor_is_sent_unchanged_never_double_encoded(tmp_path):
    base = venues.rest_for("mainnet") + "/v5/market/instruments-info?category=linear&limit=1000&status=Closed"
    assert UC.request_url(UC.PATH_INSTRUMENTS, PCT, status="Closed") == base + "&cursor=" + PCT
    assert "%25" not in UC.request_url(UC.PATH_INSTRUMENTS, PCT, status="Closed")
    plain = "abc.DEF_1-2~3"
    assert UC.request_url(UC.PATH_INSTRUMENTS, plain, status="Closed").endswith("&cursor=" + plain)
    futs = [inst(f"X{i}USDT-13NOV26", contractType="LinearFutures", status="Closed", deliveryTime="1700000000000") for i in range(3)]
    perps = [inst(f"D{i}USDT", status="Closed", deliveryTime="1700000000000") for i in range(2)]
    pages = {"": env(perps, PCT), urllib.parse.unquote(PCT): env(futs, "")}       # the venue decodes the query ONCE: a double-encoded cursor would not be found
    m = run2(tmp_path, V2Venue({("linear", "Closed"): pages}))
    t = part_of(m, "Closed")
    assert t["outcome"] == "OBSERVED_ROWS" and [p["cursor_in"] for p in t["pages"]] == ["", PCT] and [p["cursor_out"] for p in t["pages"]] == [PCT, ""]
    assert t["by_contract_type"] == {"LinearFutures": 3, "LinearPerpetual": 2}
    assert UC.verify_capture_dir(str(tmp_path / "v2"))["ok"]


def test_a_page_whose_first_row_repeats_an_earlier_page_means_the_cursor_was_not_honoured(tmp_path):
    first = [inst("AUSDT"), inst("BUSDT")]
    pages = {"": env(first, "n1"), "n1": env(first, "")}              # the venue ignored the cursor and served page one again
    m = run2(tmp_path, V2Venue({("linear", "Closed"): pages}))
    t = part_of(m, "Closed")
    assert t["outcome"] == "PARTIAL" and "cursor was not honoured" in t["error"] and m["dataset_status"] == "PARTIAL"
    assert m["delisting_observability"] == "NOT_OBSERVED"


def test_owner_observed_shape_1058_closed_rows_over_two_pages_and_default_page_cap_suffices(tmp_path):
    perps = [inst(f"P{i}USDT", status="Closed", deliveryTime="1700000000000") for i in range(318)]
    futs = [inst(f"F{i}USDT-{i}", contractType="LinearFutures", status="Closed", deliveryTime="1700000000000") for i in range(740)]
    rows = perps + futs
    pages = {"": env(rows[:1000], PCT), urllib.parse.unquote(PCT): env(rows[1000:], "")}
    m = run2(tmp_path, V2Venue({("linear", "Closed"): pages}))
    t = part_of(m, "Closed")
    assert t["rows_total"] == 1058 and [p["rows"] for p in t["pages"]] == [1000, 58] and UC.MAX_PAGES >= 5
    assert t["by_contract_type"] == {"LinearFutures": 740, "LinearPerpetual": 318}
    assert m["excluded_contract_types"] == {} and m["rejected_instruments"]["by_reason"] == {"bad_symbol": 740}      # hyphenated dated futures: counted, never in the perp universe
    assert sum(1 for r in uni2(m, str(tmp_path / "v2")) if r["status"] == "Closed") == 318


def test_ticker_vs_instrument_reconciliation_reports_orphans_and_missing_tickers(tmp_path):
    pre = [inst(f"{s}USDT", status="PreLaunch") for s in ("OPENAI", "BP")]
    fut = inst("BTCUSDT-13NOV26", contractType="LinearFutures", symbolId="fut-1")
    v = V2Venue({("linear", "PreLaunch"): chain([pre]), ("linear", None): chain([[inst("BTCUSDT"), inst("ETHUSDT"), fut]]),
                 ("linear", "Trading"): chain([[inst("BTCUSDT"), inst("ETHUSDT"), fut]])},
                tickers=[tick("BTCUSDT"), tick("OPENAIUSDT"), tick("BPUSDT"), tick("BTCUSDT-13NOV26"), tick("GHOSTUSDT"), tick("ALIENUSDT-1")])
    m = run2(tmp_path, v)
    r = m["ticker_reconciliation"]
    assert r["tickers_accepted"] == 4 and r["tickers_rejected"] == 2 and r["tickers_rejected_by_reason"] == {"symbol_has_hyphen": 2}
    assert r["orphans"] == ["GHOSTUSDT"] and r["orphan_count"] == 1                                    # accepted ticker, no instrument in any partition
    assert r["instruments_without_ticker"] == ["ETHUSDT"]
    assert r["rejected_tickers_with_instrument"] == 1 and r["rejected_tickers_without_instrument"] == 1   # the dated future has an instrument row, ALIEN does not
    assert UC.verify_capture_dir(str(tmp_path / "v2"))["coverage"]["ticker_reconciliation"] == r
    clean = run2(tmp_path, V2Venue({("linear", "PreLaunch"): chain([pre])}, tickers=[tick("BTCUSDT"), tick("ETHUSDT"), tick("OPENAIUSDT"), tick("BPUSDT")]), name="clean")
    assert clean["ticker_reconciliation"]["orphan_count"] == 0 and clean["ticker_reconciliation"]["instruments_without_ticker"] == []


# ---------------------------------------------------------------- renames are lineage, not conflicts (F2)
def _rename_venue(old_status="Closed", old_over=None, new_over=None, new_status="Trading"):
    old = inst("OLDUSDT", **{"status": old_status, "symbolId": "sid-r", "launchTime": "1600000000000", "deliveryTime": "1700000000000", **(old_over or {})})
    new = inst("NEWUSDT", **{"status": new_status, "symbolId": "sid-r", "launchTime": "1700000000000", **(new_over or {})})
    btc = inst("BTCUSDT")
    parts = {("linear", None): chain([[old, new, btc]]), ("linear", "Trading"): chain([[r for r in (new, btc) if r["status"] == "Trading"]]),
             ("linear", "Closed"): chain([[old]] if old_status == "Closed" else [[]]), ("linear", "Delivering"): chain([[old]] if old_status == "Delivering" else [[]])}
    return V2Venue(parts, tickers=[tick("NEWUSDT"), tick("BTCUSDT")])


def test_a_closed_old_symbol_and_a_trading_new_one_sharing_a_symbolid_are_a_rename_not_a_conflict(tmp_path):
    m = run2(tmp_path, _rename_venue())
    d = str(tmp_path / "v2")
    assert m["conflicts"] == [] and m["dataset_status"] == "COMPLETE"
    assert m["renames"] == [{"category": "linear", "symbolId": "sid-r", "from": "OLDUSDT", "to": "NEWUSDT"}]
    v = UC.verify_capture_dir(d)
    assert v["ok"] is True, v["problems"]                                                   # verify re-derives the same renames
    assert v["coverage"]["renames"] == m["renames"] and v["coverage"]["dataset_status"] == "COMPLETE"


def test_a_delivering_old_symbol_is_a_rename_too(tmp_path):
    m = run2(tmp_path, _rename_venue(old_status="Delivering"), name="deliv")
    assert m["conflicts"] == [] and [r["from"] for r in m["renames"]] == ["OLDUSDT"]


def test_two_trading_rows_sharing_a_symbolid_stay_conflicted(tmp_path):
    m = run2(tmp_path, _rename_venue(old_status="Trading", old_over={"deliveryTime": "0"}), name="twot")
    assert m["dataset_status"] == "CONFLICTED" and m["renames"] == []
    assert any(c["kind"] == "symbolId_multiple_symbols" for c in m["conflicts"])


def test_two_closed_rows_with_one_symbolid_and_no_launch_after_delivery_stay_conflicted(tmp_path):
    m = run2(tmp_path, _rename_venue(new_status="Closed", new_over={"launchTime": "1650000000000", "deliveryTime": "1710000000000"}), name="twoc")
    assert m["dataset_status"] == "CONFLICTED" and m["renames"] == []                      # same status, and the new one launched BEFORE the old one was delivered


def test_a_tampered_renames_claim_fails_verification(tmp_path):
    run2(tmp_path, _rename_venue())
    d = str(tmp_path / "v2")
    mp = os.path.join(d, "manifest.json")
    man = json.load(open(mp))
    man["renames"] = []
    os.chmod(mp, 0o600)
    open(mp, "w").write(json.dumps(man, indent=2, sort_keys=True) + "\n")
    v = UC.verify_capture_dir(d)
    assert v["ok"] is False and any("renames" in p for p in v["problems"])


def test_mutation_without_the_rename_rule_a_closed_plus_trading_pair_is_a_conflict(tmp_path, monkeypatch):
    monkeypatch.setattr(UC, "_rename_chain", lambda by_sym: None)
    m = run2(tmp_path, _rename_venue(), name="mut")
    assert m["dataset_status"] == "CONFLICTED"                                             # the pre-fix behaviour: the control that proves the test above discriminates


# ---------------------------------------------------------------- survivors-only is stated, not implied (F2)
def test_a_refused_closed_partition_is_complete_at_capture_level_but_survivors_only_in_words(tmp_path):
    v = V2Venue({("linear", "Closed"): {"": env([], retCode=10001, retMsg="params error")}})
    m = run2(tmp_path, v, name="refused")
    assert m["dataset_status"] == "COMPLETE" and m["delisting_observability"] == "NOT_OBSERVED"       # capture-level semantics unchanged: two separate facts
    txt = UC.describe_delisting_observability(m["delisting_observability"], m["status_coverage"]["Closed"])
    assert "survivors only" in txt and "NOT observed" in txt
    assert "no Closed rows served" in UC.describe_delisting_observability("OBSERVED", "OBSERVED_EMPTY") and "does not prove" in UC.describe_delisting_observability("OBSERVED", "OBSERVED_EMPTY")
    assert "survivors only" not in UC.describe_delisting_observability("OBSERVED", "OBSERVED_ROWS")
