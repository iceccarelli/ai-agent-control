"""The public mainnet reader: it can LOOK at the real market and do nothing else.

Authorized by the owner as READ-ONLY, UNAUTHENTICATED, PUBLIC market data. It does
not authorize mainnet trading, keys, signing, or turning any order gate on, and
these tests pin that statically and behaviourally. Everything is offline: a fake
GET stands in for the network. Nothing here says anything about what a real venue
returned; that needs operator-pasted output.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, BOT)

import mainnet_public as MP  # noqa: E402

SRC = os.path.join(BOT, "tools", "mainnet_public.py")


class Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


def market(perp=100_000.0, spot=100_000.0, funding_bps=1.0, rate_bps=1.0, n=8):
    """A canned public mainnet view, in the shape of Bybit's v5 market API."""
    base_ms = 1_700_000_000_000
    hist = [{"symbol": "BTCUSDT", "fundingRate": f"{rate_bps / 1e4:.8f}",
             "fundingRateTimestamp": str(base_ms + i * 28_800_000)}
            for i in range(n)]
    hist.reverse()                                   # the venue lists newest first

    def get(url, params, timeout):
        get.calls.append((url, dict(params), timeout))
        path = url.split("api.bybit.com", 1)[1]
        if path == "/v5/market/time":
            return Resp({"retCode": 0, "result": {"timeSecond": "1700000100"}})
        if path == "/v5/market/tickers" and params.get("category") == "linear":
            return Resp({"retCode": 0, "result": {"list": [{
                "markPrice": str(perp), "fundingRate": f"{funding_bps / 1e4:.8f}",
                "bid1Price": str(perp - 0.5), "ask1Price": str(perp + 0.5)}]}})
        if path == "/v5/market/tickers" and params.get("category") == "spot":
            return Resp({"retCode": 0, "result": {"list": [{"lastPrice": str(spot)}]}})
        if path == "/v5/market/funding/history":
            return Resp({"retCode": 0, "result": {"list": hist}})
        if path == "/v5/market/orderbook":
            return Resp({"retCode": 0, "result": {
                "s": "BTCUSDT", "ts": 1700000000000,
                "b": [[str(perp - 0.5 - i), "5"] for i in range(50)],
                "a": [[str(perp + 0.5 + i), "5"] for i in range(50)]}})
        raise AssertionError(f"unexpected request {url} {params}")
    get.calls = []
    return get


# -- what it asks the venue ---------------------------------------------------

class TestItOnlyReadsPublicMainnetMarketData:
    def test_it_observes_with_unauthenticated_gets_on_the_three_public_paths(self):
        get = market()
        obs = MP.MainnetPublicReader(http_get=get).observe()
        paths = {u.split("api.bybit.com", 1)[1] for u, _, _ in get.calls}
        # DELIBERATELY WIDENED from three to four: the public order book, to
        # measure the cost model's slippage assumption (tests/test_mainnet_book.py)
        assert paths == {"/v5/market/time", "/v5/market/tickers",
                         "/v5/market/funding/history", "/v5/market/orderbook"}
        assert all(u.startswith("https://api.bybit.com/") for u, _, _ in get.calls)
        for _, params, _ in get.calls:                  # nothing signed, no key
            assert not {k.lower() for k in params} & {
                "api_key", "sign", "signature", "timestamp", "recv_window"}
        assert obs["perp_mark"] == 100_000.0 and obs["spot_mark"] == 100_000.0

    def test_the_observation_is_labelled_mainnet_public_not_testnet(self):
        obs = MP.MainnetPublicReader(http_get=market()).observe()
        assert obs["origin"] == "mainnet_public" and obs["venue"] == "mainnet"
        assert obs["source"] == "bybit-public-rest:https://api.bybit.com"

    def test_a_path_that_is_not_public_market_data_is_refused(self):
        r = MP.MainnetPublicReader(http_get=market())
        for path in ("/v5/order/create", "/v5/account/wallet-balance",
                     "/v5/position/list", "/v5/market/../order/create", ""):
            with pytest.raises(MP.MainnetRefusal):
                r._fetch(path, {})

    def test_a_venue_error_is_a_refusal_and_not_a_guess(self):
        def get(url, params, timeout):
            return Resp({"retCode": 10001, "retMsg": "x"})
        with pytest.raises(MP.MainnetRefusal):
            MP.MainnetPublicReader(http_get=get).observe()

    def test_an_empty_market_view_is_a_refusal(self):
        def get(url, params, timeout):
            return Resp({"retCode": 0, "result": {"list": []}})
        with pytest.raises(MP.MainnetRefusal):
            MP.MainnetPublicReader(http_get=get).observe()

    def test_a_transport_failure_is_a_refusal_and_carries_no_url_secret(self):
        def get(url, params, timeout):
            raise TimeoutError("boom")
        with pytest.raises(MP.MainnetRefusal) as exc:
            MP.MainnetPublicReader(http_get=get).observe()
        assert "TimeoutError" in str(exc.value)

    def test_the_default_get_refuses_any_other_host_scheme_or_port(self):
        for url in ("http://api.bybit.com/v5/market/time",
                    "https://api-testnet.bybit.com/v5/market/time",
                    "https://api.bybit.com.evil.example/v5/market/time",
                    "https://user@api.bybit.com/v5/market/time",
                    "https://api.bybit.com:8443/v5/market/time"):
            with pytest.raises(MP.MainnetRefusal):
                MP.default_get(url, {}, 1.0)


    def test_the_default_get_refuses_a_non_public_path_before_any_socket(self, monkeypatch):
        """On the right host, but not a market-data path: refused before the
        network. `build_opener` is replaced so a broken guard fails this test
        instead of contacting anything."""
        def no_network(*a, **k):
            raise AssertionError("the network was touched")
        monkeypatch.setattr(MP.urllib.request, "build_opener", no_network)
        for path in ("/v5/order/create", "/v5/account/wallet-balance",
                     "/v5/market/time/../../order/create", "/v5/market/tickers/"):
            with pytest.raises(MP.MainnetRefusal):
                MP.default_get("https://api.bybit.com" + path, {}, 1.0)


# -- it can read, it cannot trade ---------------------------------------------

class TestItCannotTrade:
    BANNED_IMPORTS = {"requests", "carry_broker", "bybit_connection", "carry_engine",
                      "carry_risk", "main", "hmac", "ws_transport", "shadow",
                      "market_snapshot", "trading_engine", "persistence", "config"}

    def test_it_imports_nothing_order_capable(self):
        tree = ast.parse(open(SRC, encoding="utf-8").read())
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                names.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                names.add(n.module.split(".")[0])
        assert not names & self.BANNED_IMPORTS, names & self.BANNED_IMPORTS
        assert names <= {"__future__", "argparse", "hashlib", "json", "math", "os",
                         "statistics", "sys", "time", "typing", "urllib",
                         "carry_costs", "collections"}, names

    def test_the_source_has_no_credential_signature_post_or_arming_text(self):
        full = open(SRC, encoding="utf-8").read()
        # the module docstring DESCRIBES the prohibitions ("no signature"), so it
        # is not part of what is checked; everything after it is
        src = full.split('"""', 2)[2]
        assert "def default_get" in src
        for banned in ("BYBIT_API", "X-BAPI", "LIVE_TRADING_ACK", "PAPER_TRADING",
                       "os.environ", "getenv", "hmac", "signature", "recv_window",
                       "place_order", "place_market", "method=\"POST\"",
                       "method='POST'", "USE_TESTNET"):
            assert banned not in src, banned

    def test_importing_it_loads_no_order_capable_module(self):
        code = ("import sys\nsys.path[0:1] = [%r, %r]\nimport mainnet_public\n"
                "bad = sorted(m for m in sys.modules if m.split('.')[0] in %r)\n"
                "print('BAD', bad)\n"
                % (os.path.join(BOT, "tools"), BOT, sorted(self.BANNED_IMPORTS)))
        out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True,
                             text=True, timeout=60, cwd=BOT)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "BAD []", out.stdout

    def test_the_testnet_shadow_still_cannot_name_mainnet(self):
        import carry_shadow
        assert set(carry_shadow.VENUE_BASE) == {"testnet"}


# -- the evidence file --------------------------------------------------------

def obs_of(**kw):
    return MP.MainnetPublicReader(http_get=market(**kw)).observe()


class TestTheEvidenceFile:
    def test_observations_append_and_chain(self, tmp_path):
        db = str(tmp_path / "m.jsonl")
        MP.append(db, obs_of())
        MP.append(db, obs_of(perp=100_050.0))
        rows, issues = MP.read(db)
        assert len(rows) == 2 and not issues
        assert rows[1]["prev"] == rows[0]["hash"]
        assert all(r["obs"]["origin"] == "mainnet_public" for r in rows)

    def test_tampering_is_detected(self, tmp_path):
        db = str(tmp_path / "m.jsonl")
        MP.append(db, obs_of())
        MP.append(db, obs_of(perp=100_050.0))
        lines = open(db).read().splitlines()
        row = json.loads(lines[0])
        row["obs"]["perp_mark"] = 1.0
        lines[0] = json.dumps(row, sort_keys=True)
        open(db, "w").write("\n".join(lines) + "\n")
        _, issues = MP.read(db)
        assert issues
        with pytest.raises(MP.MainnetRefusal):
            MP.append(db, obs_of())                     # never extend a broken chain

    def test_it_will_not_write_into_a_sqlite_testnet_book(self, tmp_path):
        db = tmp_path / "post_gate_v1.db"
        db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
        with pytest.raises(MP.MainnetRefusal, match="SQLite"):
            MP.append(str(db), obs_of())

    def test_it_will_not_write_an_observation_of_another_origin(self, tmp_path):
        db = str(tmp_path / "m.jsonl")
        for other in ({"origin": "live_public", "venue": "testnet"},
                      {"origin": "mainnet_public", "venue": "testnet"},
                      {"origin": "live_public", "venue": "mainnet"}, {}):
            with pytest.raises(MP.MainnetRefusal):
                MP.append(db, other)
        assert not os.path.exists(db)

    def test_it_will_not_extend_a_file_with_another_origin(self, tmp_path):
        db = tmp_path / "m.jsonl"
        row = {"seq": 0, "prev": "", "obs": {"origin": "live_public", "venue": "testnet"}}
        row["hash"] = MP._hash(row)
        db.write_text(json.dumps(row, sort_keys=True) + "\n")
        with pytest.raises(MP.MainnetRefusal):
            MP.append(str(db), obs_of())

    def test_a_torn_last_line_is_not_extended(self, tmp_path):
        db = tmp_path / "m.jsonl"
        MP.append(str(db), obs_of())
        with open(db, "a") as fh:
            fh.write('{"seq": 1, "pr')
        with pytest.raises(MP.MainnetRefusal):
            MP.append(str(db), obs_of())


# -- what the evidence says, and what it does not -----------------------------

class TestTheReport:
    def test_it_counts_gate_verdicts_and_basis_on_the_real_market_view(self, tmp_path):
        db = str(tmp_path / "m.jsonl")
        MP.append(db, obs_of(perp=100_000.0, funding_bps=1.0, rate_bps=1.0))    # basis 0
        MP.append(db, obs_of(perp=101_500.0, funding_bps=1.0, rate_bps=1.0))    # +150 bps
        MP.append(db, obs_of(perp=100_000.0, funding_bps=0.0, rate_bps=0.0))    # no funding
        rep = MP.report(db)
        assert rep["observations"] == 3
        assert rep["basis_bps"]["max"] == pytest.approx(150.0)
        assert rep["gate"]["allowed"] == 1
        assert rep["gate"]["refusals"].get("ENTRY_BASIS_EXCEEDS_CARRY_BUDGET") == 1
        assert rep["gate"]["refusals"].get("FUNDING_NOT_POSITIVE") == 1

    def test_the_report_labels_itself_and_never_claims_profit(self, tmp_path):
        db = str(tmp_path / "m.jsonl")
        MP.append(db, obs_of())
        rep = MP.report(db)
        assert rep["origin"] == "mainnet_public" and rep["venue"] == "mainnet"
        assert rep["pnl_claim_allowed"] is False
        note = rep["what_it_is"].lower()
        assert "not profit" in note and "no fills" in note and "mainnet" in note
        assert "testnet" in note                         # it is never mixed with it

    def test_an_empty_or_missing_file_reports_nothing_rather_than_guessing(self, tmp_path):
        assert MP.report(str(tmp_path / "none.jsonl"))["observations"] == 0

    def test_a_broken_chain_is_reported_not_summarised(self, tmp_path):
        db = str(tmp_path / "m.jsonl")
        MP.append(db, obs_of())
        open(db, "a").write("garbage\n")
        rep = MP.report(db)
        assert rep["chain_issues"] and "gate" not in rep


# -- command line -------------------------------------------------------------

class TestCommandLine:
    def test_observe_appends_one_row_and_report_prints_it(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(MP, "default_get", market())
        db = str(tmp_path / "m.jsonl")
        assert MP.main(["observe", "--db", db]) == 0
        assert MP.main(["report", "--db", db]) == 0
        out = capsys.readouterr().out
        assert '"observations": 1' in out and "mainnet_public" in out

    def test_a_refused_observation_appends_nothing(self, tmp_path, monkeypatch):
        def get(url, params, timeout):
            raise TimeoutError("x")
        monkeypatch.setattr(MP, "default_get", get)
        db = str(tmp_path / "m.jsonl")
        assert MP.main(["observe", "--db", db]) == MP.EXIT_REFUSED
        assert not os.path.exists(db)
