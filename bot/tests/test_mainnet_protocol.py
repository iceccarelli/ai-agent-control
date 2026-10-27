"""The real-market evidence is read under a protocol written BEFORE any of it was seen.

Moving a threshold after looking at the data is how a weak result becomes a
strong one. The thresholds and the meaning of each verdict are frozen in
`mainnet_public.MAINNET_PROTOCOL`; its hash is pinned here as a literal, so
changing any of it means editing this test on purpose, in a visible diff.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, BOT)

import mainnet_public as MP  # noqa: E402
from test_mainnet_public import market  # noqa: E402

#: The hash of the protocol AS DECLARED on 2026-10-27, before any mainnet number
#: was read. A change here is a change of protocol and must say why in the PR.
DECLARED_HASH = "e17aae5111e381359e6909d01a1ce5795b2a4551279fbcfe2ae9dc37fc0b8891"


def rep(n=600, days=40.0, open_fraction=0.30, issues=None):
    allowed = round(n * open_fraction)
    return {"observations": n, "span_hours": days * 24.0, "chain_issues": issues or [],
            "gate": {"allowed": allowed, "refused": n - allowed,
                     "open_fraction": allowed / n if n else 0.0, "refusals": {}}}


class TestTheProtocolIsFrozen:
    def test_the_declared_hash_is_the_one_in_the_code(self):
        assert MP.protocol_hash() == DECLARED_HASH

    def test_the_thresholds_are_the_declared_ones(self):
        p = MP.MAINNET_PROTOCOL
        assert p["adequacy"] == {"min_observations": 500, "min_span_days": 30}
        assert p["thresholds"] == {"rarely_open_below": 0.05}
        assert p["pnl_claim_allowed"] is False
        assert p["evidence_class"] == "mainnet_public" and p["venue"] == "mainnet"

    def test_it_forbids_moving_the_goalposts(self):
        never = " ".join(MP.MAINNET_PROTOCOL["never"]).lower()
        for phrase in ("profitability claim", "change to the gate",
                       "after looking at the data", "after the fact", "mixing"):
            assert phrase in never, phrase

    def test_no_verdict_can_be_read_as_profit(self):
        for v in MP.MAINNET_PROTOCOL["verdicts"]:
            assert "PROFIT" not in v.upper() and "EDGE" not in v.upper().replace("EDGE_", "x")
        for text in MP.MAINNET_PROTOCOL["meaning"].values():
            assert "not an edge" in text.lower() or "no conclusion" in text.lower() \
                or "nothing to tune" in text.lower()


class TestTheVerdict:
    @pytest.mark.parametrize("n", [0, 1, 499])
    def test_too_few_observations_is_insufficient(self, n):
        assert MP.protocol_verdict(rep(n=n)) == "INSUFFICIENT_REAL_MARKET_SAMPLE"

    def test_exactly_the_minimum_observations_is_enough(self):
        assert MP.protocol_verdict(rep(n=500)) != "INSUFFICIENT_REAL_MARKET_SAMPLE"

    @pytest.mark.parametrize("days", [0.0, 29.99])
    def test_too_short_a_span_is_insufficient(self, days):
        assert MP.protocol_verdict(rep(days=days)) == "INSUFFICIENT_REAL_MARKET_SAMPLE"

    def test_exactly_the_minimum_span_is_enough(self):
        assert MP.protocol_verdict(rep(days=30.0)) != "INSUFFICIENT_REAL_MARKET_SAMPLE"

    def test_a_gate_that_is_almost_never_open_is_named_as_such(self):
        r = rep(n=1000, open_fraction=0.0)
        assert MP.protocol_verdict(r) == "GATE_RARELY_OPEN_ON_REAL_MARKET"
        r = rep(n=1000, open_fraction=0.049)
        assert MP.protocol_verdict(r) == "GATE_RARELY_OPEN_ON_REAL_MARKET"

    def test_at_the_floor_it_is_open_often_enough_to_look_further(self):
        r = rep(n=1000, open_fraction=0.05)
        assert MP.protocol_verdict(r) == "GATE_OPEN_ON_REAL_MARKET_WORTH_A_FORWARD_BOOK"

    def test_an_unreadable_evidence_file_gets_no_verdict_about_the_market(self):
        r = rep(issues=["line 3: the hash chain is broken"])
        assert MP.protocol_verdict(r) == "EVIDENCE_UNREADABLE"

    def test_a_report_with_no_gate_block_is_insufficient_not_a_crash(self):
        assert MP.protocol_verdict({"observations": 900, "span_hours": 2000.0,
                                    "chain_issues": []}) == "INSUFFICIENT_REAL_MARKET_SAMPLE"
        assert MP.protocol_verdict({}) == "INSUFFICIENT_REAL_MARKET_SAMPLE"

    def test_every_verdict_is_a_declared_one(self):
        for r in (rep(n=3), rep(open_fraction=0.0), rep(open_fraction=0.5),
                  rep(issues=["x"]), {}):
            assert MP.protocol_verdict(r) in MP.MAINNET_PROTOCOL["verdicts"]

    def test_the_verdict_cannot_be_changed_by_another_field(self):
        r = rep(n=1000, open_fraction=0.0)
        r["pnl_claim_allowed"] = True
        r["verdict"] = "GATE_OPEN_ON_REAL_MARKET_WORTH_A_FORWARD_BOOK"
        assert MP.protocol_verdict(r) == "GATE_RARELY_OPEN_ON_REAL_MARKET"


class TestTheReportCarriesIt:
    def test_a_small_real_file_is_insufficient_and_says_what_that_means(self, tmp_path):
        db = str(tmp_path / "m.jsonl")
        MP.append(db, MP.MainnetPublicReader(http_get=market()).observe())
        r = MP.report(db)
        p = r["protocol"]
        assert p["verdict"] == "INSUFFICIENT_REAL_MARKET_SAMPLE"
        assert p["hash"] == MP.protocol_hash() == DECLARED_HASH
        assert p["adequacy"] == {"min_observations": 500, "min_span_days": 30}
        assert "no conclusion" in p["meaning"].lower()
        assert r["pnl_claim_allowed"] is False

    def test_an_empty_file_still_carries_the_protocol(self, tmp_path):
        r = MP.report(str(tmp_path / "none.jsonl"))
        assert r["protocol"]["verdict"] == "INSUFFICIENT_REAL_MARKET_SAMPLE"

    def test_a_broken_chain_carries_the_unreadable_verdict(self, tmp_path):
        db = str(tmp_path / "m.jsonl")
        MP.append(db, MP.MainnetPublicReader(http_get=market()).observe())
        open(db, "a").write("garbage\n")
        assert MP.report(db)["protocol"]["verdict"] == "EVIDENCE_UNREADABLE"

    def test_the_report_is_json_serialisable(self, tmp_path):
        db = str(tmp_path / "m.jsonl")
        MP.append(db, MP.MainnetPublicReader(http_get=market()).observe())
        json.dumps(MP.report(db))


class TestTheStatusScriptShowsIt:
    """Runs the real runner_status.sh against a fake `fly` that answers with a REAL
    mainnet_public.report (the harness is test_mainnet_collect's)."""

    def run(self, tmp_path, n_obs):
        from test_mainnet_collect import TestTheStatusScriptSection as T
        db = str(tmp_path / "m.jsonl")
        for _ in range(n_obs):
            MP.append(db, MP.MainnetPublicReader(http_get=market()).observe())
        return T().run(tmp_path, json.dumps(MP.report(db)))

    def test_the_verdict_its_meaning_and_the_threshold_are_printed(self, tmp_path):
        out = self.run(tmp_path, 2).stdout
        assert "protocol verdict: INSUFFICIENT_REAL_MARKET_SAMPLE" in out
        assert "no conclusion in either direction" in out
        assert "needs 500 observations over 30 days" in out

    def test_with_no_observations_the_verdict_is_still_printed(self, tmp_path):
        from test_mainnet_collect import TestTheStatusScriptSection as T
        r = T().run(tmp_path, json.dumps(MP.report(str(tmp_path / "none.jsonl"))))
        assert "no observations yet" in r.stdout
        assert "protocol verdict: INSUFFICIENT_REAL_MARKET_SAMPLE" in r.stdout
