"""0035 — connector_check can FAIL, so it can be the first command in the VPC.

It printed a verdict and exited 0 either way. From inside the intended VPC the
question is binary — can this host reach every public endpoint the carry book
reads on Bybit testnet? — and a one-off ECS task needs an exit code to say so.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import connector_check as cc  # noqa: E402


def _fetch(block=()):
    def fetch(url, timeout):
        return (403, "blocked") if any(b in url for b in block) else (200, "{}")
    return fetch


class TestTheCarryReadsAreChecked:
    def test_every_public_read_the_carry_book_makes_is_probed(self):
        urls = " ".join(u for _n, u in cc.ENDPOINTS)
        for path in ("/v5/market/time", "category=linear", "category=spot",
                     "/v5/market/funding/history",
                     "/v5/market/instruments-info"):
            assert path in urls, path
        assert all("api-testnet.bybit.com" in u for n, u in cc.ENDPOINTS
                   if n.startswith("bybit_testnet_"))

    def test_all_reachable_is_carry_reads_ok(self):
        r = cc.check(fetch=_fetch())
        assert r["bybit_testnet_verdict"] == "BYBIT_TESTNET_OK"
        assert r["carry_reads_verdict"] == "CARRY_READS_OK"

    def test_one_blocked_carry_read_is_a_failure(self):
        r = cc.check(fetch=_fetch(block=("funding/history",)))
        assert r["carry_reads_verdict"] == "CARRY_READS_BLOCKED"
        assert "bybit_testnet_funding_history" in r["carry_reads_blocked"]


class TestStrictModeExits:
    def test_strict_and_reachable_exits_zero(self, tmp_path):
        out = tmp_path / "cc.json"
        assert cc.main(["--require-bybit-testnet", "--out", str(out)],
                       fetch=_fetch()) == 0
        assert json.loads(out.read_text())["carry_reads_verdict"] == \
            "CARRY_READS_OK"

    def test_strict_and_blocked_exits_two(self, tmp_path):
        assert cc.main(["--require-bybit-testnet", "--out",
                        str(tmp_path / "cc.json")],
                       fetch=_fetch(block=("bybit",))) == 2

    def test_without_the_flag_it_still_reports_and_exits_zero(self, tmp_path):
        assert cc.main(["--out", str(tmp_path / "cc.json")],
                       fetch=_fetch(block=("bybit",))) == 0

    def test_it_still_sends_no_keys_and_places_nothing(self):
        r = cc.check(fetch=_fetch())
        assert r["keys_used"] is False and r["orders_placed"] is False
        for _n, url in cc.ENDPOINTS:
            assert "order" not in url and "position" not in url
            assert "wallet" not in url
