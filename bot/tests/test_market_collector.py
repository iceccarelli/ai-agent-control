"""market_collector: offline only. fetch, clock and sleep are injected; a socket guard fails any real connection; every directory is a tmp dir."""
from __future__ import annotations

import ast
import json
import os
import random
import socket
import stat
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import evidence_chain as EC  # noqa: E402
import market_collector as MC  # noqa: E402
import universe_capture as UC  # noqa: E402
import venues  # noqa: E402

T0 = 1_800_000_000_000
STEP = 60_000
SYMS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT")
TURNOVER = {"BTCUSDT": "1000", "ETHUSDT": "900", "SOLUSDT": "800", "XRPUSDT": "300", "DOGEUSDT": "100"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("network touched")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


# ---------------------------------------------------------------- fixtures

def inst(sym):
    return {"symbol": sym, "contractType": "LinearPerpetual", "status": "Trading", "baseCoin": sym[:-4], "quoteCoin": "USDT", "settleCoin": "USDT",
            "launchTime": "1585526400000", "deliveryTime": "0", "fundingInterval": 480, "upperFundingRate": "0.00375", "lowerFundingRate": "-0.00375",
            "priceFilter": {"minPrice": "0.10", "tickSize": "0.10"},
            "lotSizeFilter": {"minOrderQty": "0.001", "maxOrderQty": "1190", "qtyStep": "0.001", "minNotionalValue": "5"}}


def tick(sym, turnover=None, **over):
    t = {"symbol": sym, "markPrice": "100.5", "indexPrice": "100.4", "lastPrice": "100.5", "fundingRate": "0.0001", "nextFundingTime": "1800000999999",
         "fundingIntervalHour": "8", "fundingCap": "0.00375", "openInterest": "10", "openInterestValue": "1005", "volume24h": "5",
         "turnover24h": turnover or TURNOVER.get(sym, "50"), "bid1Price": "100.4", "ask1Price": "100.6", "price24hPcnt": "0.01"}
    t.update(over)
    return t


def env(result, t, **kw):
    d = {"retCode": 0, "retMsg": "OK", "result": result, "time": t}
    d.update(kw)
    return json.dumps(d, indent=1).encode()          # non-canonical bytes on purpose: they must be preserved unedited


def lst(rows, t, nxt=""):
    return env({"category": "linear", "list": rows, "nextPageCursor": nxt}, t)


def book_body(sym, t, bids=None, asks=None, **over):
    r = {"s": sym, "b": bids if bids is not None else [["100.0", "1"], ["99.9", "2"]], "a": asks if asks is not None else [["100.1", "1"], ["100.2", "3"]],
         "ts": t, "u": 1, "seq": 1, "cts": t}
    r.update(over)
    return env(r, t)


def trade(i, sym, **over):
    r = {"execId": f"e{i}", "symbol": sym, "price": "100.0", "size": "0.5", "side": "Buy", "time": "1799999999000", "isBlockTrade": False}
    r.update(over)
    return r


def trades_body(sym, t, rows=None):
    return lst(rows if rows is not None else [trade(1, sym), trade(2, sym, side="Sell")], t)


class Venue:
    """Serves scripted public pages by path. ``script[(path, symbol)]`` is a list consumed in order: bytes, or an exception instance to raise."""
    def __init__(self, pages=None, tickers=None, syms=SYMS):
        self.t = T0
        self.pages = pages if pages is not None else {"": [inst(s) for s in syms]}
        self.tickers = tickers if tickers is not None else [tick(s) for s in syms]
        self.script = {}
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        path, _, qs = url.partition("?")
        path = path[len(venues.rest_for("mainnet")):]
        q = dict(p.split("=", 1) for p in qs.split("&"))
        key = (path, q.get("symbol"))
        if self.script.get(key):
            item = self.script[key].pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        if path == UC.PATH_INSTRUMENTS:
            cursor = q.get("cursor", "")
            body = self.pages[cursor]
            rows, nxt = body if isinstance(body, tuple) else (body, "")
            return lst(rows, self.t, nxt)
        if path == UC.PATH_TICKERS:
            return lst(self.tickers, self.t)
        if path == MC.PATH_BOOK:
            return book_body(q["symbol"], self.t)
        if path == MC.PATH_TRADES:
            return trades_body(q["symbol"], self.t)
        raise AssertionError(f"unexpected path {path}")


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        self.t += 7
        return self.t


def cycle(root, venue, i=0, **kw):
    t = T0 + i * STEP
    venue.t = t
    kw.setdefault("sleep", lambda _s: None)
    kw.setdefault("max_symbols", 3)
    kw.setdefault("free_bytes_fn", lambda _p: 1 << 40)
    return MC.run_once(str(root), t, venue, Clock(t), **kw)


def manifest_of(root, res):
    return json.load(open(os.path.join(str(root), res["cycle"], "manifest.json")))


def rows(root):
    return EC.rows_of(EC.read_lines(os.path.join(str(root), MC.LOG_NAME))[0])


@pytest.fixture
def root(tmp_path):
    return tmp_path / "collect"


# ---------------------------------------------------------------- full cycle

def test_full_cycle_on_fixtures(root):
    v = Venue()
    res = cycle(root, v)
    assert res["status"] == "OK" and res["errors"] == [] and res["cycle"] == f"cycle-{T0}"
    cd = root / res["cycle"]
    m = manifest_of(root, res)
    assert m["schema_version"] == 1 and m["status"] == "OK" and m["counts"]["books_ok"] == 3 and m["counts"]["trades_ok"] == 3
    # raw bytes unedited: the saved book is exactly what the venue served, hash and mode as recorded
    books = [a for a in m["artifacts"] if a["kind"] == "book"]
    assert [a["symbol"] for a in books] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    for a in m["artifacts"]:
        if a["kind"] == "tickers":
            continue
        data = (cd / a["file"]).read_bytes()
        assert MC._sha(data) == a["sha256"] and a["bytes"] == len(data) and a["recv_ms"] > T0 and a["schema_version"] == 1
        assert a["validation"]["ok"] is True and stat.S_IMODE(os.stat(cd / a["file"]).st_mode) == 0o600
    assert (cd / "books" / "BTCUSDT.json").read_bytes() == book_body("BTCUSDT", T0)          # indent=1 bytes preserved, not re-serialized
    assert books[0]["request"]["params"] == {"category": "linear", "symbol": "BTCUSDT", "limit": "50"}
    # COMPLETE marker = manifest hash; the log row matches; the tickers file carries bid/ask
    assert (cd / "COMPLETE").read_text().strip() == MC._file_sha(str(cd / "manifest.json"))
    r = rows(root)
    assert len(r) == 1 and r[0]["cycle"] == res["cycle"] and r[0]["manifest_sha256"] == res["manifest_sha256"] and r[0]["gaps"] == []
    assert EC.verify(str(root / MC.LOG_NAME))["ok"]
    tk = [json.loads(x) for x in open(cd / "tickers.jsonl")]
    assert tk[0]["bid1Price"] == "100.4" and tk[0]["ask1Price"] == "100.6" and tk[0]["recv_ms"] > T0
    assert MC.verify_cycle_dir(str(cd)) == []
    # requests are constants on the registry host, only the four public GET paths
    base = venues.rest_for("mainnet")
    assert all(u.startswith(base + "/v5/market/") for u in v.urls)
    assert {u.split("?")[0][len(base):] for u in v.urls} == set(MC.PUBLIC_PATHS)
    assert f"{base}/v5/market/orderbook?category=linear&symbol=BTCUSDT&limit=50" in v.urls
    assert f"{base}/v5/market/recent-trade?category=linear&symbol=BTCUSDT&limit=100" in v.urls


def test_pagination_through_universe_capture(root):
    v = Venue(pages={"": ([inst("BTCUSDT"), inst("ETHUSDT")], "c1"), "c1": ([inst("SOLUSDT")], "c2"), "c2": ([inst("XRPUSDT")], "")},
              tickers=[tick(s) for s in SYMS[:4]])
    res = cycle(root, v)
    assert res["status"] == "OK" and res["counts"]["instruments"] == 4
    um = json.load(open(root / res["cycle"] / "universe" / "manifest.json"))
    assert [p["request_cursor"] for p in um["pages"]] == ["", "c1", "c2"]
    assert any(u.endswith("&cursor=c2") for u in v.urls)


def test_repeated_cursor_is_fatal_for_the_universe_and_recorded(root):
    v = Venue(pages={"": ([inst("BTCUSDT")], "c1"), "c1": ([inst("ETHUSDT")], "c1")})
    res = cycle(root, v)
    assert res["status"] == "PARTIAL" and any("repeated cursor" in e for e in res["errors"])
    m = manifest_of(root, res)
    assert m["counts"]["symbols_selected"] == 0 and m["counts"]["books_ok"] == 0                 # a partial universe never drives a selection
    assert not any(u.split("?")[0].endswith(MC.PATH_BOOK) for u in v.urls)
    assert (root / res["cycle"] / "COMPLETE").exists() and len(rows(root)) == 1 and rows(root)[0]["status"] == "PARTIAL"
    # positive control: the same venue with an advancing cursor is complete
    v2 = Venue(pages={"": ([inst("BTCUSDT")], "c1"), "c1": ([inst("ETHUSDT")], "")})
    assert cycle(root, v2, i=1)["status"] == "OK"


# ---------------------------------------------------------------- rate limits

def test_429_backoff_is_bounded_and_the_cycle_is_partial(root):
    v = Venue()
    v.script[(MC.PATH_BOOK, "BTCUSDT")] = [MC.RateLimited("429")] * 50
    sleeps = []
    res = cycle(root, v, sleep=sleeps.append)
    assert res["status"] == "PARTIAL"
    assert sleeps.count(1.0) >= 1 and [s for s in sleeps if s != MC.PAUSE_S] == [1.0, 2.0, 4.0]         # bounded exponential backoff, then it stops
    book_urls = [u for u in v.urls if "orderbook" in u]
    assert len(book_urls) == 1 + MC.MAX_RETRIES and len(v.urls) < 20                                      # no infinite loop, no further symbols fetched
    m = manifest_of(root, res)
    assert [e["action"] for e in m["rate_limit_events"]] == ["backoff"] * 3 + ["exhausted"]
    assert m["counts"]["books_failed"] == 3 and m["counts"]["trades_failed"] == 3 and (root / res["cycle"] / "COMPLETE").exists()
    assert not list((root / res["cycle"] / "books").glob("*"))


def test_retcode_10006_is_a_rate_limit_and_recovery_is_clean(root):
    limited = json.dumps({"retCode": 10006, "retMsg": "Too many visits", "result": {}, "time": T0}).encode()
    v = Venue()
    v.script[(MC.PATH_TRADES, "ETHUSDT")] = [limited]                 # positive control: one throttle, then success
    sleeps = []
    res = cycle(root, v, sleep=sleeps.append)
    assert res["status"] == "OK" and 1.0 in sleeps and res["counts"]["rate_limit_events"] == 1
    v2 = Venue()
    v2.script[(MC.PATH_TRADES, "ETHUSDT")] = [limited] * 10
    res2 = cycle(root, v2, i=1, sleep=lambda _s: None)
    assert res2["status"] == "PARTIAL" and any("rate limited" in e for e in res2["errors"])
    assert len([u for u in v2.urls if "recent-trade" in u and "ETHUSDT" in u]) == 1 + MC.MAX_RETRIES


def test_partial_cycle_is_recorded_with_counted_rejects(root):
    v = Venue()
    v.script[(MC.PATH_BOOK, "ETHUSDT")] = [book_body("ETHUSDT", T0, bids=[["101.0", "1"]], asks=[["100.5", "1"]])]       # crossed
    res = cycle(root, v)
    m = manifest_of(root, res)
    assert res["status"] == "PARTIAL" and m["counts"]["books_failed"] == 1 and m["counts"]["rejects_total"]["book:crossed_book"] == 1
    eth = next(a for a in m["artifacts"] if a["kind"] == "book" and a["symbol"] == "ETHUSDT")
    assert eth["validation"]["ok"] is False and (root / res["cycle"] / eth["file"]).exists()          # the raw bytes are kept, not dropped
    assert rows(root)[0]["status"] == "PARTIAL"


# ---------------------------------------------------------------- gaps, resume

def test_missed_cycle_is_a_recorded_gap_never_filled(root):
    v = Venue()
    cycle(root, v, i=0)
    cycle(root, v, i=1)
    res = cycle(root, v, i=4)                                       # cycles 2 and 3 never ran
    g = rows(root)[2]["gaps"]
    assert len(g) == 1 and g[0]["missed_expected_cycles"] == 2 and g[0]["gap_ms"] == 3 * STEP and g[0]["previous_cycle"] == f"cycle-{T0 + STEP}"
    assert manifest_of(root, res)["gaps"] == g
    assert sorted(n for n in os.listdir(root) if n.startswith("cycle-")) == [f"cycle-{T0 + i * STEP}" for i in (0, 1, 4)]   # nothing invented
    assert rows(root)[1]["gaps"] == []                                                                    # positive control: consecutive => no gap


def test_clock_that_does_not_advance_is_refused(root):
    v = Venue()
    cycle(root, v, i=1)
    res = cycle(root, v, i=1)
    assert res["status"] == "REFUSED_CLOCK" and len(rows(root)) == 1


def test_interrupted_cycle_is_set_aside_never_reused(root):
    v = Venue()
    cycle(root, v, i=0)
    dead = root / f"cycle-{T0 + STEP}"
    os.makedirs(dead / "universe")
    (dead / "universe" / "instruments_page0001.json").write_bytes(b"half a page")                     # crashed mid-cycle, no COMPLETE
    res = cycle(root, v, i=1)                                       # the SAME cycle id is asked for again: it must not reuse the dead directory
    assert res["status"] == "OK"
    assert res["set_aside"] == [{"cycle": dead.name, "moved_to": dead.name + ".INCOMPLETE", "reason": "no COMPLETE marker"}]
    assert (root / (dead.name + ".INCOMPLETE") / "universe" / "instruments_page0001.json").read_bytes() == b"half a page"
    assert (root / dead.name / "COMPLETE").exists() and MC.verify_cycle_dir(str(root / dead.name)) == []
    assert manifest_of(root, res)["set_aside"][0]["moved_to"].endswith(".INCOMPLETE")
    # a second interruption does not overwrite the first set-aside directory
    os.makedirs(root / f"cycle-{T0 + 2 * STEP}")
    os.makedirs(root / f"cycle-{T0 + 3 * STEP}")
    (root / f"cycle-{T0 + 2 * STEP}.INCOMPLETE").mkdir()
    res2 = cycle(root, v, i=4)
    assert {s["moved_to"] for s in res2["set_aside"]} == {f"cycle-{T0 + 2 * STEP}.INCOMPLETE.2", f"cycle-{T0 + 3 * STEP}.INCOMPLETE"}


def test_complete_but_unrecorded_cycle_is_recorded_late_and_a_corrupt_one_is_set_aside(root):
    v = Venue()
    cycle(root, v, i=0)
    for n in os.listdir(root):
        if n.startswith(MC.LOG_NAME):
            os.remove(root / n)                                       # crash between COMPLETE and the log append (or a lost log)
    res = cycle(root, v, i=1)
    r = rows(root)
    assert res["status"] == "OK" and [x["cycle"] for x in r] == [f"cycle-{T0}", f"cycle-{T0 + STEP}"] and r[0]["recovered"] is True
    # corrupt COMPLETE-but-unrecorded directory: set aside rather than trusted
    bad = root / f"cycle-{T0 + 2 * STEP}"
    res2 = cycle(root, v, i=2)
    for n in os.listdir(root):
        if n.startswith(MC.LOG_NAME):
            os.remove(root / n)
    (bad / "books" / "BTCUSDT.json").write_bytes(b"tampered")
    res3 = cycle(root, v, i=3)
    assert any(s["cycle"] == bad.name and s["reason"] == "COMPLETE but failed verification" for s in res3["set_aside"]) and res2["status"] == "OK"


def test_a_broken_log_refuses_the_cycle(root):
    v = Venue()
    cycle(root, v, i=0)
    with open(root / MC.LOG_NAME, "ab") as fh:
        fh.write(b'{"cycle":"cycle-1"}\n')                            # a row with no chain link
    res = cycle(root, v, i=1)
    assert res["status"] == "REFUSED_LOG" and not (root / f"cycle-{T0 + STEP}").exists()


# ---------------------------------------------------------------- duplicates, conflicts, skew

def test_identical_duplicates_counted_conflicts_fail_the_artifact(root):
    v = Venue()
    dup = trade(1, "BTCUSDT")
    v.script[(MC.PATH_TRADES, "BTCUSDT")] = [trades_body("BTCUSDT", T0, [dup, dict(dup), trade(2, "BTCUSDT")])]
    v.script[(MC.PATH_TRADES, "ETHUSDT")] = [trades_body("ETHUSDT", T0, [trade(1, "ETHUSDT"), trade(1, "ETHUSDT", price="999.0")])]
    v.script[(MC.PATH_BOOK, "SOLUSDT")] = [book_body("SOLUSDT", T0, bids=[["100.0", "1"], ["100.0", "2"]])]
    res = cycle(root, v)
    m = manifest_of(root, res)
    by = {(a["kind"], a["symbol"]): a["validation"] for a in m["artifacts"] if a.get("file")}
    assert by[("trades", "BTCUSDT")]["ok"] is True and by[("trades", "BTCUSDT")]["duplicates_identical"] == 1 and by[("trades", "BTCUSDT")]["rows"] == 2
    assert by[("trades", "ETHUSDT")]["ok"] is False and by[("trades", "ETHUSDT")]["conflicts"] == 1
    assert by[("book", "SOLUSDT")]["ok"] is False and by[("book", "SOLUSDT")]["rejects"] == {"conflicting_duplicate_level": 1}
    assert res["status"] == "PARTIAL" and m["counts"]["conflicts"] == 2 and m["counts"]["duplicates_identical"] >= 1


def test_conflicting_duplicate_ticker_is_fatal_for_the_universe_and_never_merged(root):
    tickers = [tick("BTCUSDT"), tick("BTCUSDT", markPrice="1.0"), tick("ETHUSDT"), tick("SOLUSDT"), tick("XRPUSDT"), tick("DOGEUSDT")]
    res = cycle(root, Venue(tickers=tickers))                           # universe_capture refuses to merge two different rows for one symbol
    assert res["status"] == "PARTIAL" and any("different values" in e for e in res["errors"]) and res["counts"]["symbols_selected"] == 0
    # the collector's own ticker validation also excludes such a symbol (defence in depth) and counts an identical duplicate
    body = lst([tick("BTCUSDT"), tick("BTCUSDT", markPrice="1.0"), tick("ETHUSDT"), tick("ETHUSDT"), tick("SOLUSDT", bid1Price="101", ask1Price="100"),
                tick("XRPUSDT", turnover24h="")], T0)
    ok, info = MC.validate_tickers(body)
    assert sorted(ok) == ["ETHUSDT"] and info["conflicts"] == 1 and info["duplicates_identical"] == 1
    assert info["rejected_symbols"] == {"BTCUSDT": "conflicting_duplicate_ticker", "SOLUSDT": "crossed_bid1_ask1", "XRPUSDT": "missing:turnover24h"}


def test_clock_skew_is_flagged_not_corrected(root):
    v = Venue()
    v.script[(MC.PATH_BOOK, "BTCUSDT")] = [book_body("BTCUSDT", T0 - 60_000)]        # the venue's own time is a minute behind our receive clock
    res = cycle(root, v)
    m = manifest_of(root, res)
    b = next(a for a in m["artifacts"] if a["kind"] == "book" and a["symbol"] == "BTCUSDT")
    assert b["skew_flag"] == "SKEW" and b["skew_ms"] > MC.SKEW_WARN_MS and m["counts"]["skew_flags"] >= 1 and m["clock"]["flagged"]
    assert res["status"] == "OK" and (root / res["cycle"] / "COMPLETE").exists()                 # flagged, still recorded, nothing adjusted
    ok = cycle(root, Venue(), i=1)                                                            # positive control: no skew, nothing flagged
    assert manifest_of(root, ok)["counts"]["skew_flags"] == 0


# ---------------------------------------------------------------- storage budget

def test_storage_budget_refuses_and_reports(root):
    v = Venue()
    res = cycle(root, v, reserve_bytes=1000, free_bytes_fn=lambda _p: 10)
    assert res["status"] == "REFUSED_STORAGE" and res["free_bytes"] == 10 and v.urls == [] and not list(root.glob("cycle-*"))
    cycle(root, v, i=1)
    res2 = cycle(root, v, i=2, max_bytes=100)                         # the root already holds more than max_bytes
    assert res2["status"] == "REFUSED_STORAGE" and "max_bytes" in res2["reason"] and not (root / f"cycle-{T0 + 2 * STEP}").exists()
    assert cycle(root, v, i=2)["status"] == "OK"                      # positive control: within budget


# ---------------------------------------------------------------- selection rule

def test_selection_is_same_cycle_turnover_not_returns(root):
    # LOWVOL has the lowest turnover and the best 24h return AND a later cycle's turnover rise: neither may select it
    tickers = [tick("BTCUSDT", "1000", price24hPcnt="-0.05"), tick("ETHUSDT", "900", price24hPcnt="-0.04"), tick("SOLUSDT", "800", price24hPcnt="0.0"),
               tick("LOWVOLUSDT", "1", price24hPcnt="9.99"), tick("XRPUSDT", "300")]
    pages = {"": [inst(s) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "LOWVOLUSDT", "XRPUSDT")]}
    v = Venue(pages=pages, tickers=tickers)
    res = cycle(root, v, max_symbols=3)
    m = manifest_of(root, res)
    assert [s["symbol"] for s in m["selection"]["selected"]] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    sel = m["selection"]
    assert sel["rule"] == MC.SELECTION_RULE and sel["as_of_ms"] == m["artifacts"][0]["recv_ms"] and sel["source"]["sha256"] == m["artifacts"][0]["sha256"]
    assert not any("LOWVOL" in u for u in v.urls if "orderbook" in u or "recent-trade" in u)
    # the next cycle's data decides the next cycle's selection only: LOWVOL now leads on turnover and is chosen, nothing carries over
    v2 = Venue(pages=pages, tickers=[tick("LOWVOLUSDT", "5000")] + [t for t in tickers if t["symbol"] != "LOWVOLUSDT"])
    m2 = manifest_of(root, cycle(root, v2, i=1, max_symbols=3))
    assert [s["symbol"] for s in m2["selection"]["selected"]] == ["LOWVOLUSDT", "BTCUSDT", "ETHUSDT"]


def test_selection_function_is_deterministic_and_order_independent():
    t = {s: {"turnover24h": TURNOVER[s]} for s in SYMS}
    t["AAAUSDT"] = {"turnover24h": "900"}                              # a tie with ETHUSDT: broken by symbol, ascending
    base = MC.select_symbols(t, set(t), 4)
    assert [s["symbol"] for s in base] == ["BTCUSDT", "AAAUSDT", "ETHUSDT", "SOLUSDT"]
    for seed in range(20):
        items = list(t.items())
        random.Random(seed).shuffle(items)
        assert MC.select_symbols(dict(items), set(t), 4) == base
    assert MC.select_symbols(t, {"SOLUSDT", "XRPUSDT"}, 5) == [{"symbol": "SOLUSDT", "turnover24h": "800"}, {"symbol": "XRPUSDT", "turnover24h": "300"}]
    assert MC.select_symbols(t, set(t), 0) == []
    assert MC.select_symbols({"ZZZUSDT": {"turnover24h": "0"}}, {"ZZZUSDT"}, 3) == []         # zero turnover is not selectable
    with pytest.raises(MC.CollectorError):
        MC.select_symbols(t, set(t), -1)


def test_selection_function_signature_takes_only_this_cycles_snapshot():
    src = open(os.path.join(BOT, "tools", "market_collector.py")).read()
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "select_symbols")
    assert [a.arg for a in fn.args.args] == ["tickers", "eligible", "max_symbols"]
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    assert not names & {"time", "clock", "open", "os", "previous", "prev", "history", "returns", "price24hPcnt", "fetch"}


# ---------------------------------------------------------------- no credentials, no order capability

def test_module_has_no_order_capability_or_credentials_by_ast():
    path = os.path.join(BOT, "tools", "market_collector.py")
    tree = ast.parse(open(path).read())
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            imported.add(n.module or "")
    assert imported == {"__future__", "argparse", "hashlib", "json", "os", "re", "shutil", "sys", "time", "urllib.error", "urllib.parse", "urllib.request", "decimal", "typing",
                        "evidence_chain", "universe_capture", "venues"}
    assert not any(any(f in i for f in ("bybit_connection", "carry_broker", "hmac", "broker", "signing", "secrets")) for i in imported)
    strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    v5 = {s for s in strings if s.startswith("/v5/")}
    assert v5 == {"/v5/market/orderbook", "/v5/market/recent-trade"}                               # the rest come from universe_capture's two pinned paths
    assert set(MC.PUBLIC_PATHS) == {UC.PATH_INSTRUMENTS, UC.PATH_TICKERS, MC.PATH_BOOK, MC.PATH_TRADES}
    assert all(p.startswith("/v5/market/") for p in MC.PUBLIC_PATHS)
    blob = " ".join(strings).lower()
    for bad in ("x-bapi", "api_key", "api-key", "apikey", "secret", "signature", "/order/", "/position/", "/account/", "/asset/", "private", "os.environ"):
        assert bad not in blob
    # every URL is built from the registry host and pinned paths
    calls = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert any(c == "venues.rest_for('mainnet')" for c in calls)
    assert not any("environ" in c or "getenv" in c for c in calls)
    assert "method='GET'" in " ".join(calls).replace('"', "'")
    assert "POST" not in strings and "PUT" not in strings and "DELETE" not in strings


def test_http_fetch_and_request_url_refuse_anything_not_pinned():
    base = venues.rest_for("mainnet")
    for bad in (f"{base}/v5/order/create?category=linear", f"{base}/v5/account/wallet-balance?x=1", "https://evil.example/v5/market/tickers?category=linear",
                f"{base}/v5/market/tickers", "http://api.bybit.com/v5/market/tickers?category=linear"):
        with pytest.raises(MC.CollectorError):
            MC.http_fetch(bad)                                       # refused before any socket (the guard would fail the test otherwise)
    for kw in ({"symbol": "btcusdt", "limit": 5}, {"symbol": "BTC&x=1", "limit": 5}, {"symbol": "BTCUSDT", "limit": 0}, {"symbol": "BTCUSDT", "limit": 10_000},
               {"symbol": "BTCUSDT", "limit": True}, {"symbol": None, "limit": 5}):
        with pytest.raises(MC.CollectorError):
            MC.request_url(MC.PATH_BOOK, **kw)
    with pytest.raises(MC.CollectorError):
        MC.request_url("/v5/order/create", symbol="BTCUSDT", limit=5)
    assert MC.request_url(MC.PATH_TRADES, symbol="BTCUSDT", limit=5) == f"{base}/v5/market/recent-trade?category=linear&symbol=BTCUSDT&limit=5"


def test_protected_root_is_refused_and_nothing_overwritten(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    with pytest.raises(UC.AR.RecordError):
        cycle(runs / "x", Venue())
    # a cycle directory that already exists is never reused or overwritten
    root = tmp_path / "ok"
    cycle(root, Venue(), i=0)
    before = (root / f"cycle-{T0}" / "manifest.json").read_bytes()
    with pytest.raises(MC.CollectorError):
        MC._mkdir_exclusive(str(root / f"cycle-{T0}"))
    with pytest.raises(UC.CaptureError):
        MC._write(str(root / f"cycle-{T0}" / "manifest.json"), b"{}")
    assert (root / f"cycle-{T0}" / "manifest.json").read_bytes() == before


# ---------------------------------------------------------------- seal / check

def filled(root, n=3, **kw):
    v = Venue()
    for i in range(n):
        assert cycle(root, v, i=i, **kw)["status"] == "OK"
    return v


def test_seal_and_check_clean_then_detect_missing_altered_truncated(tmp_path, root):
    filled(root)
    man = str(tmp_path / "seal.jsonl")
    s = MC.seal_cycles(str(root), man)
    assert s["sealed_new"] == 3 and MC.check_cycles(str(root), man) == {"ok": True, "problems": [], "rows_checked": 1}       # positive control
    cdir = lambda i: root / f"cycle-{T0 + i * STEP}"                                           # noqa: E731
    # TRUNCATED: a raw file loses its tail
    p = cdir(0) / "books" / "BTCUSDT.json"
    data = p.read_bytes()
    p.write_bytes(data[:-10])
    r = MC.check_cycles(str(root), man)
    assert not r["ok"] and any("TRUNCATED books/BTCUSDT.json" in x for x in r["problems"])
    p.write_bytes(data)
    assert MC.check_cycles(str(root), man)["ok"]
    # ALTERED: same length, one byte flipped
    p.write_bytes(data[:-3] + bytes([data[-3] ^ 1]) + data[-2:])
    r = MC.check_cycles(str(root), man)
    assert any("ALTERED books/BTCUSDT.json" in x for x in r["problems"])
    p.write_bytes(data)
    # ALTERED: manifest.json edited; extra file planted
    (cdir(1) / "extra.json").write_bytes(b"{}")
    assert any("unexpected file extra.json" in x for x in MC.check_cycles(str(root), man)["problems"])
    os.remove(cdir(1) / "extra.json")
    # MISSING: a whole cycle, and a single file
    os.remove(cdir(2) / "tickers.jsonl")
    assert any("MISSING file tickers.jsonl" in x for x in MC.check_cycles(str(root), man)["problems"])
    import shutil
    shutil.rmtree(cdir(2))
    r = MC.check_cycles(str(root), man)
    assert any(f"cycle-{T0 + 2 * STEP}: MISSING" in x for x in r["problems"]) and True


def test_check_compares_with_every_earlier_row_and_the_log(tmp_path, root):
    v = Venue()
    man = str(tmp_path / "seal.jsonl")
    cycle(root, v, i=0)
    MC.seal_cycles(str(root), man)
    cycle(root, v, i=1)
    MC.seal_cycles(str(root), man)
    assert MC.check_cycles(str(root), man)["rows_checked"] == 2 and MC.check_cycles(str(root), man)["ok"]
    # damage visible only against the FIRST row
    p = root / f"cycle-{T0}" / "trades" / "BTCUSDT.json"
    p.write_bytes(p.read_bytes()[:-5])
    assert any("TRUNCATED" in x for x in MC.check_cycles(str(root), man)["problems"])
    with pytest.raises(MC.SealError):                                  # sealing over a problem would launder it
        MC.seal_cycles(str(root), man)


def test_lost_log_row_and_log_file_are_detected(tmp_path, root):
    filled(root, 2)
    man = str(tmp_path / "seal.jsonl")
    MC.seal_cycles(str(root), man)
    lines = (root / MC.LOG_NAME).read_bytes().split(b"\n")
    (root / MC.LOG_NAME).write_bytes(lines[0] + b"\n")                 # the newest row is lost
    r = MC.check_cycles(str(root), man)
    assert not r["ok"] and any("TRUNCATED" in x and "cycle-" in x for x in r["problems"])
    for n in os.listdir(root):
        if n.startswith(MC.LOG_NAME):
            os.remove(root / n)
    assert any("cycles.jsonl: MISSING" in x for x in MC.check_cycles(str(root), man)["problems"])


def test_seal_manifest_must_live_outside_and_check_without_manifest_fails(tmp_path, root):
    filled(root, 1)
    with pytest.raises(MC.SealError):
        MC.seal_cycles(str(root), str(root / "seal.jsonl"))
    assert MC.check_cycles(str(root), str(tmp_path / "none.jsonl"))["ok"] is False
    os.makedirs(tmp_path / "emptyroot")
    with pytest.raises(MC.SealError):
        MC.seal_cycles(str(tmp_path / "emptyroot"), str(tmp_path / "s.jsonl"))


# ---------------------------------------------------------------- backup / restore-check

def test_backup_then_restore_check_passes_and_is_incremental(tmp_path, root):
    filled(root, 2)
    dest, rec = tmp_path / "bk", str(tmp_path / "receipts.jsonl")
    (root / f"cycle-{T0 + 9 * STEP}").mkdir()                            # an interrupted cycle is never copied
    b = MC.backup_cycles(str(root), str(dest), rec)
    assert b["ok"] and [d["cycle"] for d in b["backed_up"]] == [f"cycle-{T0}", f"cycle-{T0 + STEP}"] and any("no COMPLETE" in s for s in b["skipped"])
    assert sorted(os.listdir(dest)) == [f"cycle-{T0}", f"cycle-{T0 + STEP}"]
    assert MC.restore_check(str(dest), rec) == {"ok": True, "problems": [], "checked": 2}
    assert stat.S_IMODE(os.stat(dest / f"cycle-{T0}" / "manifest.json").st_mode) == 0o600
    assert MC.backup_cycles(str(root), str(dest), rec)["backed_up"] == []                  # second run: already receipted, nothing re-copied
    cycle(root, Venue(), i=2)
    assert [d["cycle"] for d in MC.backup_cycles(str(root), str(dest), rec)["backed_up"]] == [f"cycle-{T0 + 2 * STEP}"]
    assert EC.verify(rec)["ok"]


def test_corrupt_copy_is_set_aside_and_gets_no_receipt(tmp_path, root):
    filled(root, 2)
    dest, rec = tmp_path / "bk", str(tmp_path / "receipts.jsonl")

    def flaky(src, dst):
        MC.copy_cycle_dir(src, dst)
        if src.endswith(str(T0)):
            p = os.path.join(dst, "books", "BTCUSDT.json")
            with open(p, "r+b") as fh:
                fh.truncate(20)                                         # a short copy

    b = MC.backup_cycles(str(root), str(dest), rec, flaky)
    assert b["ok"] is False and len(b["rejected"]) == 1 and "TRUNCATED" in b["rejected"][0]
    assert sorted(os.listdir(dest)) == sorted([f"cycle-{T0 + STEP}", f"cycle-{T0}.REJECTED"])
    assert [r["cycle"] for r in EC.rows_of(EC.read_lines(rec)[0])] == [f"cycle-{T0 + STEP}"]
    assert MC.restore_check(str(dest), rec)["ok"]                      # the rejected copy is not in the proven set
    # a corrupt copy that silently changes bytes (same length) is caught too, and a copy that never arrives
    for tag, damage in (("flip", lambda d: open(os.path.join(d, "tickers.jsonl"), "r+b").write(b"X")), ("none", None)):
        d2, r2 = tmp_path / f"bk_{tag}", str(tmp_path / f"r_{tag}.jsonl")
        if damage:
            def cp(src, dst, damage=damage):
                MC.copy_cycle_dir(src, dst)
                damage(dst)
        else:
            def cp(src, dst):
                pass
        out = MC.backup_cycles(str(root), str(d2), r2, cp)
        assert out["ok"] is False and len(out["rejected"]) == 2 and not os.path.exists(r2)


def test_backup_never_overwrites_and_never_sits_beside_the_original(tmp_path, root):
    filled(root, 1)
    dest, rec = tmp_path / "bk", str(tmp_path / "receipts.jsonl")
    os.makedirs(dest / f"cycle-{T0}")
    (dest / f"cycle-{T0}" / "precious").write_bytes(b"do not touch")
    with pytest.raises(MC.SealError):
        MC.backup_cycles(str(root), str(dest), rec)
    assert (dest / f"cycle-{T0}" / "precious").read_bytes() == b"do not touch"
    with pytest.raises(MC.SealError):
        MC.backup_cycles(str(root), str(root / "inside"), rec)
    with pytest.raises(MC.CollectorError):                             # the copy primitive itself is exclusive
        MC.copy_cycle_dir(str(root / f"cycle-{T0}"), str(dest / f"cycle-{T0}"))
    assert (dest / f"cycle-{T0}" / "precious").read_bytes() == b"do not touch"


def test_a_corrupt_source_is_not_backed_up_and_restore_detects_later_damage(tmp_path, root):
    filled(root, 2)
    p = root / f"cycle-{T0}" / "trades" / "ETHUSDT.json"
    p.write_bytes(p.read_bytes()[:-4])
    dest, rec = tmp_path / "bk", str(tmp_path / "receipts.jsonl")
    b = MC.backup_cycles(str(root), str(dest), rec)
    assert [d["cycle"] for d in b["backed_up"]] == [f"cycle-{T0 + STEP}"] and any("fails its own manifest" in s for s in b["skipped"])
    bp = dest / f"cycle-{T0 + STEP}" / "universe" / "tickers.json"
    bp.write_bytes(bp.read_bytes() + b" ")
    r = MC.restore_check(str(dest), rec)
    assert not r["ok"] and any("ALTERED" in x for x in r["problems"])
    import shutil
    shutil.rmtree(dest / f"cycle-{T0 + STEP}")
    assert any("missing from the backup" in x for x in MC.restore_check(str(dest), rec)["problems"])
    assert MC.restore_check(str(dest), str(tmp_path / "nope.jsonl"))["ok"] is False


# ---------------------------------------------------------------- CLI (offline commands only)

def test_cli_seal_check_backup_restore(tmp_path, root, capsys):
    filled(root, 2)
    man, rec, dest = str(tmp_path / "s.jsonl"), str(tmp_path / "r.jsonl"), str(tmp_path / "bk")
    assert MC.main(["seal", "--root", str(root), "--manifest", man]) == 0
    assert MC.main(["check", "--root", str(root), "--manifest", man]) == 0
    assert MC.main(["backup", "--root", str(root), "--to", dest, "--receipts", rec]) == 0
    assert MC.main(["restore-check", "--backup", dest, "--receipts", rec]) == 0
    p = root / f"cycle-{T0}" / "manifest.json"
    p.write_bytes(p.read_bytes() + b" ")
    assert MC.main(["check", "--root", str(root), "--manifest", man]) == 1
    assert MC.main(["seal", "--root", str(root), "--manifest", str(root / "in.jsonl")]) == 2
    capsys.readouterr()
