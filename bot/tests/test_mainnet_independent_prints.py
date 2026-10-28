"""The evidence report says how many INDEPENDENT funding settlements it covers.

The runner observes hourly; Bybit settles funding every 8 hours. Up to eight
consecutive observations therefore carry the same funding print and give the
same gate answer, so "the gate was open on 17 of 26 observations" describes about
three settlements, not 26 independent draws. The protocol counts observations
and is frozen; this only adds the honest denominator beside it. It does not
touch the protocol, the verdict, or the gate.
"""
from __future__ import annotations

import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, BOT)

import mainnet_public as MP  # noqa: E402
from test_mainnet_public import market  # noqa: E402

EIGHT_H_MS = 8 * 3600 * 1000


def base_obs():
    return MP.MainnetPublicReader(http_get=market()).observe()


def shifted(o, settlements):
    """The same observation, as seen `settlements` funding periods later."""
    o = copy.deepcopy(o)
    d = settlements * EIGHT_H_MS
    o["settled_prints"] = [[bps, ms + d] for bps, ms in o["settled_prints"]]
    o["funding_print_ms"] += d
    o["observed_at_s"] += settlements * 8 * 3600
    return o


def report(tmp_path, *observations):
    db = str(tmp_path / "m.jsonl")
    for o in observations:
        MP.append(db, o)
    return MP.report(db)


class TestTheCount:
    def test_repeated_observations_of_one_settlement_count_once_per_print(self, tmp_path):
        o = base_obs()
        r = report(tmp_path, o, copy.deepcopy(o), copy.deepcopy(o))
        assert r["observations"] == 3
        assert r["distinct_funding_settlements"] == len({ms for _, ms in o["settled_prints"]})

    def test_each_new_settlement_adds_exactly_one(self, tmp_path):
        o = base_obs()
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        one = report(tmp_path / "a", o)["distinct_funding_settlements"]
        two = report(tmp_path / "b", o, shifted(o, 1))["distinct_funding_settlements"]
        assert two == one + 1

    def test_the_count_is_never_above_the_observations_times_prints(self, tmp_path):
        o = base_obs()
        r = report(tmp_path, o, shifted(o, 3))
        assert r["distinct_funding_settlements"] <= 2 * len(o["settled_prints"])

    def test_it_is_absent_when_the_file_cannot_be_read(self, tmp_path):
        db = str(tmp_path / "m.jsonl")
        MP.append(db, base_obs())
        open(db, "a").write("garbage\n")
        assert "distinct_funding_settlements" not in MP.report(db)

    def test_an_empty_file_does_not_crash_the_report(self, tmp_path):
        assert "distinct_funding_settlements" not in MP.report(str(tmp_path / "none.jsonl"))


class TestItChangesNothingElse:
    def test_the_protocol_hash_and_verdict_are_unchanged(self, tmp_path):
        r = report(tmp_path, base_obs())
        assert r["protocol"]["hash"] == MP.protocol_hash()
        assert r["protocol"]["verdict"] == "INSUFFICIENT_REAL_MARKET_SAMPLE"

    def test_the_verdict_does_not_read_the_new_field(self):
        rep = {"observations": 1000, "span_hours": 40 * 24.0, "chain_issues": [],
               "gate": {"allowed": 0, "refused": 1000, "open_fraction": 0.0, "refusals": {}},
               "distinct_funding_settlements": 10_000}
        assert MP.protocol_verdict(rep) == "GATE_RARELY_OPEN_ON_REAL_MARKET"

    def test_the_report_is_json_serialisable(self, tmp_path):
        json.dumps(report(tmp_path, base_obs()))


class TestTheStatusScriptShowsIt:
    def run(self, tmp_path, rep):
        from test_mainnet_collect import TestTheStatusScriptSection as T
        return T().run(tmp_path, json.dumps(rep))

    def test_the_denominator_and_the_caveat_are_printed(self, tmp_path):
        o = base_obs()
        rep = report(tmp_path, o, copy.deepcopy(o))
        r = self.run(tmp_path, rep)
        assert r.returncode == 0, r.stdout + r.stderr
        n = rep["distinct_funding_settlements"]
        assert f"covering {n} distinct funding settlements" in r.stdout
        assert "hourly observations repeat" in r.stdout

    def test_an_older_report_without_the_field_still_prints(self, tmp_path):
        o = base_obs()
        rep = report(tmp_path, o)
        rep.pop("distinct_funding_settlements")
        r = self.run(tmp_path, rep)
        assert r.returncode == 0 and "distinct funding settlements" not in r.stdout
        assert "protocol verdict:" in r.stdout
