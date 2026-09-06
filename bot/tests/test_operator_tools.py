"""The three Track C operator tools: read, hash, report, refuse. Nothing else.

Invariants pinned: one-shot; exit 2 on a live request before touching
anything; the open bar is never written; historical bytes are a byte-prefix
of the appended file; ETH/SOL never touched; a digest mismatch refuses;
connector check uses no keys and places no orders.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import os
import shutil
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import append_closed_corpus as acc  # noqa: E402
import connector_check as cc  # noqa: E402
import operator_paper_daily as opd  # noqa: E402

DAY = 86_400_000


# ---------------------------------------------------------------- operator ---

def test_operator_exits_2_on_live_request_and_reads_nothing(tmp_path):
    code, report = opd.run(str(tmp_path), {"USE_TESTNET": "0", "PAPER_TRADING": "0"})
    assert code == 2 and report["preflight"] == "LIVE_REQUESTED"
    assert "ladder" not in report and report["live_authorized"] is False


def test_operator_sandbox_ok_reports_gate_false_and_ladder_from_newest_artifact(tmp_path):
    code, report = opd.run(ROOT, {"USE_TESTNET": "1", "PAPER_TRADING": "1",
                                  "STATE_DB_PATH": str(tmp_path / "none.db")})
    assert code == 0, report["preflight_reasons"]
    assert report["promotion_gate"]["allows_live"] is False
    assert report["ladder"]["artifact"].endswith("slice76_forward_shadow.json")
    assert len(report["ladder"]["ladder"]) == 6
    assert report["closer_to_autonomous_profit_agent"] is False
    assert report["live_authorized"] is False


def test_operator_refuses_when_the_shipped_kill_switch_is_engaged():
    db = os.path.join(ROOT, "state", "trading_state.db")
    if not os.path.isfile(db):
        pytest.skip("fixture db not present")
    code, report = opd.run(ROOT, {"USE_TESTNET": "1", "PAPER_TRADING": "1",
                                  "STATE_DB_PATH": db})
    assert code == 3 and any("kill switch" in r for r in report["preflight_reasons"])


def test_operator_refuses_ack_in_paper_and_models_current(tmp_path):
    os.makedirs(tmp_path / "models" / "current")
    code, report = opd.run(str(tmp_path), {"LIVE_TRADING_ACK": "I_UNDERSTAND"})
    assert code == 3
    joined = " ".join(report["preflight_reasons"])
    assert "LIVE_TRADING_ACK" in joined and "models/current" in joined


# ----------------------------------------------------------- connector check ---

def test_connector_check_reports_blocked_and_never_changes_venue():
    def fetch(url, timeout):
        return (403, "blocked") if "bybit" in url else (200, "{}")
    r = cc.check(fetch=fetch)
    assert r["bybit_testnet_verdict"] == "BYBIT_TESTNET_BLOCKED"
    assert r["corpus_path_verdict"] == "CORPUS_PATH_OK"
    assert r["keys_used"] is False and r["orders_placed"] is False
    assert r["venue_changed"] is False and r["track_d_permitted_from_this_host"] is False


def test_connector_check_never_sends_auth_headers(monkeypatch):
    seen = []

    def fetch(url, timeout):
        seen.append(url)
        return 0, "offline"
    cc.check(fetch=fetch)
    assert seen and all("api_key" not in u.lower() and "sign" not in u.lower() for u in seen)


# --------------------------------------------------------------- append corpus ---

def _corpus(tmp_path, last_day="2026-08-24"):
    lin = tmp_path / "lin.csv.gz"
    fun = tmp_path / "fun.csv.gz"
    day0 = dt.datetime(2026, 8, 23, tzinfo=dt.timezone.utc)
    rows = ["time_period_start,time_period_end,time_open,time_close,price_open,"
            "price_high,price_low,price_close,volume_traded,trades_count"]
    for i in range(2):
        d = day0 + dt.timedelta(days=i)
        s = d.strftime("%Y-%m-%dT00:00:00+00:00")
        e = d.strftime("%Y-%m-%dT23:59:59+00:00")
        rows.append(f"{s},{e},{s},{e},77000,79000,76000,78000,1000,5")
    with gzip.open(lin, "wb") as fh:
        fh.write(("\r\n".join(rows) + "\r\n").encode())
    t = int(dt.datetime(2026, 8, 25, 16, tzinfo=dt.timezone.utc).timestamp() * 1000)
    with gzip.open(fun, "wb") as fh:
        fh.write(("funding_time,funding_time_ms,symbol,funding_rate,mark_price\r\n"
                  f"2026-08-25T16:00:00+00:00,{t + 5},BTCUSDT,0.00010000,79464.0\r\n").encode())
    return str(lin), str(fun)


def _fake_fetch(now_ms):
    def fetch(path, params):
        if path == "klines":
            start = int(params["startTime"])
            out = []
            o = start
            while o <= now_ms:  # includes the OPEN bar on purpose
                out.append([o, "77719.00000000", "79974.80", "76649", "78953.0",
                            "232502.35000000", o + DAY - 1, "0", 5914733])
                o += DAY
            return out
        if path == "fundingRate":
            start = int(params["startTime"])
            out, t = [], (start // 28_800_000 + 1) * 28_800_000
            while t <= now_ms + 28_800_000:  # includes one FUTURE print on purpose
                out.append({"symbol": "BTCUSDT", "fundingTime": t + 3,
                            "fundingRate": "0.00010000", "markPrice": "80000.1"})
                t += 28_800_000
            return out
        raise AssertionError(path)
    return fetch


def test_append_is_closed_only_prefix_preserving_and_hash_published(tmp_path):
    lin, fun = _corpus(tmp_path)
    before_lin, before_fun = acc.read_gz(lin), acc.read_gz(fun)
    now = "2026-09-01T18:19:11Z"
    now_ms = int(dt.datetime(2026, 9, 1, 18, 19, 11, tzinfo=dt.timezone.utc).timestamp() * 1000)
    rep = acc.run(observed_at_utc=now, fetch=_fake_fetch(now_ms), linear_path=lin,
                  funding_path=fun, write=True,
                  expect_linear_sha256=hashlib.sha256(before_lin).hexdigest())
    assert rep["new_closed_bars"] == ["2026-08-25", "2026-08-26", "2026-08-27",
                                      "2026-08-28", "2026-08-29", "2026-08-30",
                                      "2026-08-31"]
    assert rep["refused_open_bar"] == "2026-09-01"
    assert rep["bars_fabricated"] == 0 and rep["eth_sol_touched"] is False
    after_lin, after_fun = acc.read_gz(lin), acc.read_gz(fun)
    assert after_lin.startswith(before_lin) and after_fun.startswith(before_fun)
    assert rep["linear_sha256_after"] == hashlib.sha256(after_lin).hexdigest()
    assert b"2026-09-01T00:00:00" not in after_lin
    # no funding print after observed_at
    last_ms = int(after_fun.decode().strip().splitlines()[-1].split(",")[1])
    assert last_ms <= now_ms
    assert after_lin.count(b"\r\n") == len(after_lin.split(b"\n")) - 1  # CRLF everywhere


def test_append_dry_run_writes_nothing(tmp_path):
    lin, fun = _corpus(tmp_path)
    before = acc.read_gz(lin)
    now_ms = int(dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
    acc.run(observed_at_utc="2026-09-01T00:00:00Z", fetch=_fake_fetch(now_ms),
            linear_path=lin, funding_path=fun, write=False)
    assert acc.read_gz(lin) == before


def test_append_refuses_on_digest_mismatch(tmp_path):
    lin, fun = _corpus(tmp_path)
    with pytest.raises(acc.Refuse):
        acc.run(observed_at_utc="2026-09-01T00:00:00Z", fetch=_fake_fetch(0),
                linear_path=lin, funding_path=fun, expect_linear_sha256="deadbeef")


def test_append_refuses_a_gap(tmp_path):
    lin, fun = _corpus(tmp_path)
    now_ms = int(dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
    inner = _fake_fetch(now_ms)

    def gappy(path, params):
        rows = inner(path, params)
        return rows[::2] if path == "klines" else rows
    with pytest.raises(acc.Refuse):
        acc.run(observed_at_utc="2026-09-01T00:00:00Z", fetch=gappy,
                linear_path=lin, funding_path=fun)


def test_append_against_the_real_corpus_is_a_noop_at_its_own_last_close():
    """observed_at = 2026-08-25T00:00:00Z: nothing new can be closed."""
    def fetch(path, params):
        return []
    rep = acc.run(observed_at_utc="2026-08-25T00:00:00Z", fetch=fetch, write=False)
    assert rep["new_closed_bars"] == [] and rep["new_funding_prints"] == 0
    assert rep["linear_sha256_before"] == rep["linear_sha256_after"]
    assert rep["linear_sha256_before"] == \
        "293774ee35fcac24679bed81356ef081c3787e8acfc3877b2fe12650316d97c9"
    assert rep["funding_sha256_before"] == \
        "c652c9b0b6332b5b98bc11ec89097abd69cea1d0b6774cf4f79532243ba611f4"
