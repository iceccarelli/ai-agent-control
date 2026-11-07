"""universe_capture: offline only. The transport and the clock are injected; a socket guard fails any real connection."""
from __future__ import annotations

import json
import os
import socket
import stat
import sys
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
    r.update(over)
    return r


def env(rows, nxt="", time_ms=NOW, **kw):
    d = {"retCode": 0, "retMsg": "OK", "result": {"category": "linear", "list": rows, "nextPageCursor": nxt}, "time": time_ms}
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
    with pytest.raises(UC.CaptureError):
        run(tmp_path, Venue({"": env([inst("BTCUSDT")])}, [tick("BTCUSDT\n")]))


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
