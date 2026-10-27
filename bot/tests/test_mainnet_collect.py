"""The runner collects the REAL market's public numbers, and cannot be hurt by it.

`--mainnet-db` makes each monitor cycle append one observation of Bybit's public
mainnet market (tools/mainnet_public.py: unauthenticated GETs, its own evidence
file) next to the testnet evidence. It is OBSERVATION COLLECTION: reported beside
the cycle outcome, never folded into it, so a refused or broken mainnet read can
neither fail a good cycle nor make a bad one look good. It orders nothing. All
offline: the observer is injected.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import mainnet_public as MP  # noqa: E402
import monitor_summary as MS  # noqa: E402
import run_outcomes as RO  # noqa: E402
import runner_fixtures as RF  # noqa: E402
import runner_monitor as RM  # noqa: E402
from test_runner_monitor import ENV, FakeRunner  # noqa: E402


def obs(perp=100_000.0, spot=100_000.0):
    base = 1_700_000_000_000
    return {"origin": MP.ORIGIN, "venue": MP.VENUE, "source": "fake",
            "perp_mark": perp, "spot_mark": spot, "funding_bps": 1.0,
            "funding_print_bps": 1.0, "funding_print_ms": base,
            "perp_bid": None, "perp_ask": None, "venue_time_s": None,
            "observed_at_s": 1_700_000_000.0, "fetch_started_s": 1_700_000_000.0,
            "settled_prints": [[1.0, base + i * 28_800_000] for i in range(8)]}


class Observer:
    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.calls = result or obs(), exc, 0

    def __call__(self):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.result


def cycle(tmp_path, runner, *, db, observer, probe=200):
    code = RM.monitor_once(str(tmp_path / "run"), env=ENV, prober=lambda: probe,
                           runner=runner, sleep=lambda s: None,
                           history_root=str(tmp_path), mainnet_db=db,
                           mainnet_observer=observer)
    return code, json.load(open(tmp_path / "run" / "monitor.json"))


def history(tmp_path):
    return [json.loads(l) for l in open(tmp_path / "monitor.jsonl")]


# -- one observation ----------------------------------------------------------

class TestMainnetObservation:
    def test_no_db_means_no_observation_and_no_call(self):
        o = Observer()
        assert RM.mainnet_observation(None, o) is None and o.calls == 0

    def test_a_good_read_is_recorded_with_its_basis(self, tmp_path):
        db = str(tmp_path / "m" / "mainnet_public.jsonl")
        out = RM.mainnet_observation(db, Observer(obs(perp=100_150.0)))
        assert out["state"] == "RECORDED" and out["seq"] == 0
        assert out["basis_bps"] == pytest.approx(15.0)
        rows, issues = MP.read(db)
        assert len(rows) == 1 and not issues

    def test_a_refused_read_writes_nothing_and_says_why(self, tmp_path):
        db = str(tmp_path / "m.jsonl")
        out = RM.mainnet_observation(db, Observer(exc=MP.MainnetRefusal("geo-blocked")))
        assert out["state"] == "REFUSED" and "geo-blocked" in out["detail"]
        assert not os.path.exists(db)

    def test_any_other_failure_is_a_fault_and_never_raises(self, tmp_path):
        out = RM.mainnet_observation(str(tmp_path / "m.jsonl"),
                                     Observer(exc=RuntimeError("boom")))
        assert out["state"] == "FAULT" and "RuntimeError" in out["detail"]

    def test_a_broken_chain_is_refused_not_extended(self, tmp_path):
        db = str(tmp_path / "m.jsonl")
        RM.mainnet_observation(db, Observer())
        open(db, "a").write("garbage\n")
        out = RM.mainnet_observation(db, Observer())
        assert out["state"] == "REFUSED"

    def test_it_will_not_write_into_a_sqlite_book(self, tmp_path):
        db = tmp_path / "post_gate_v1.db"
        db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 64)
        assert RM.mainnet_observation(str(db), Observer())["state"] == "REFUSED"


# -- it cannot change the cycle -----------------------------------------------

class TestItNeverChangesTheOutcome:
    @pytest.mark.parametrize("exc", [MP.MainnetRefusal("x"), RuntimeError("y"), None])
    def test_the_cycle_outcome_and_exit_code_are_the_same_with_and_without_it(self, tmp_path, exc):
        for name in ("a", "b"):
            (tmp_path / name).mkdir()
        without_code, without = cycle(tmp_path / "a", FakeRunner(RF.preflight_report("PASS")),
                                      db=None, observer=None)
        code, with_ = cycle(tmp_path / "b", FakeRunner(RF.preflight_report("PASS")),
                            db=str(tmp_path / "b" / "m.jsonl"), observer=Observer(exc=exc))
        assert code == without_code == 0
        assert with_["outcome"] == without["outcome"]

    def test_a_failed_cycle_stays_failed_whatever_the_mainnet_says(self, tmp_path):
        runner = FakeRunner(RF.preflight_report("PASS", verdict="PREFLIGHT_BLOCKED",
                                                blockers=["x"]), rc=2)
        code, rec = cycle(tmp_path, runner, db=str(tmp_path / "m.jsonl"),
                          observer=Observer())
        assert code == RM.EXIT_FAILED and rec["outcome"] == RO.MONITOR_FAILED
        assert rec["mainnet"]["state"] == "RECORDED"      # still collected

    def test_the_observation_is_made_even_when_testnet_is_unreachable(self, tmp_path):
        o = Observer()
        cycle(tmp_path, FakeRunner(RF.preflight_report("PASS")),
              db=str(tmp_path / "m.jsonl"), observer=o, probe=403)
        assert o.calls == 1

    def test_the_preflight_child_never_sees_the_mainnet_path_or_a_mainnet_flag(self, tmp_path):
        runner = FakeRunner(RF.preflight_report("PASS"))
        cycle(tmp_path, runner, db=str(tmp_path / "m.jsonl"), observer=Observer())
        argv, env, _ = runner.calls[0]
        assert "mainnet" not in " ".join(argv).lower()
        assert env["BYBIT_VENUE"] == "testnet" and env["USE_TESTNET"] == "1"
        assert env["PAPER_TRADING"] == "1"


# -- the history says what happened -------------------------------------------

class TestHistoryAndSummary:
    def test_the_history_line_carries_the_mainnet_reading(self, tmp_path):
        cycle(tmp_path, FakeRunner(RF.preflight_report("PASS")),
              db=str(tmp_path / "m.jsonl"), observer=Observer(obs(perp=100_200.0)))
        line = history(tmp_path)[0]
        assert line["mainnet_state"] == "RECORDED"
        assert line["mainnet_basis_bps"] == pytest.approx(20.0)
        assert line["order_sent"] is False

    def test_a_failure_leaves_a_reason_in_the_history(self, tmp_path):
        cycle(tmp_path, FakeRunner(RF.preflight_report("PASS")),
              db=str(tmp_path / "m.jsonl"),
              observer=Observer(exc=MP.MainnetRefusal("HTTP 403 from the venue")))
        line = history(tmp_path)[0]
        assert line["mainnet_state"] == "REFUSED" and "403" in line["mainnet_detail"]

    def test_without_mainnet_the_history_line_is_unchanged(self, tmp_path):
        cycle(tmp_path, FakeRunner(RF.preflight_report("PASS")), db=None, observer=None)
        assert not [k for k in history(tmp_path)[0] if k.startswith("mainnet")]

    def line(self, minutes_ago, **kw):
        import calendar
        import time
        now = float(calendar.timegm((2026, 10, 27, 12, 0, 0)))
        r = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - minutes_ago * 60)),
             "outcome": "ENTRY_NOT_ELIGIBLE", "order_sent": False, "reasons": []}
        r.update(kw)
        return json.dumps(r) + "\n", now

    def recent_history(self):
        import time
        t = time.time()
        return "".join(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t - k * 600)),
                                   "outcome": "ENTRY_NOT_ELIGIBLE", "order_sent": False,
                                   "reasons": []}) + "\n" for k in (3, 2, 1))

    def summarise(self, *rows):
        import calendar
        now = float(calendar.timegm((2026, 10, 27, 12, 0, 0)))
        text = "".join(self.line(m, **kw)[0] for m, kw in rows)
        parsed, _ = MS.scan(text)
        return MS.summarise(parsed, now=now, raw_text=text)

    def test_the_summary_counts_mainnet_states_and_shows_the_latest_basis(self):
        s = self.summarise((120, {"mainnet_state": "RECORDED", "mainnet_basis_bps": 10.0}),
                           (60, {"mainnet_state": "REFUSED", "mainnet_detail": "x"}),
                           (5, {"mainnet_state": "RECORDED", "mainnet_basis_bps": 12.5}))
        assert s["mainnet_states"] == {"RECORDED": 2, "REFUSED": 1}
        assert s["mainnet_basis_bps"]["max"] == 12.5
        out = MS.render(s)
        assert "mainnet" in out and "RECORDED x2" in out and "REFUSED x1" in out
        assert "public" in out and "not profit" in out

    def test_a_summary_without_mainnet_prints_no_mainnet_line(self):
        s = self.summarise((5, {}))
        assert "mainnet" not in MS.render(s).lower()
        assert s["mainnet_states"] == {}

    def test_a_failing_mainnet_read_does_not_make_the_runner_unhealthy(self):
        s = self.summarise((60, {"mainnet_state": "FAULT"}), (5, {"mainnet_state": "FAULT"}))
        assert s["health"] == "OK" and s["exit"] == 0


# -- shipped, wired, and kept apart from testnet ------------------------------

class TestWiring:
    def read(self, rel):
        return open(os.path.join(BOT, rel), encoding="utf-8").read()

    def test_the_image_copies_the_reader_and_the_command_enables_it(self):
        docker = self.read("Dockerfile.drill")
        assert "tools/mainnet_public.py" in docker.replace("\\\n", " ")
        cmd = [l for l in docker.splitlines() if l.startswith("CMD ")][0]
        assert "--mainnet-db" in cmd and "/data/mainnet/mainnet_public.jsonl" in cmd

    def test_the_entrypoint_creates_the_directory(self):
        assert "/data/mainnet" in self.read("deploy/drill-entrypoint.sh")

    def test_main_refuses_a_mainnet_db_that_is_the_shadow_book_or_a_sqlite_file(self, tmp_path, monkeypatch):
        for name in UD_CREDENTIALS():
            monkeypatch.setenv(name, "x")
        started = []
        monkeypatch.setattr(RM, "loop", lambda *a, **k: started.append(k) or 0)
        shadow = str(tmp_path / "post_gate_v1.db")
        for bad in (shadow, str(tmp_path / "other.db"), str(tmp_path / "x.sqlite")):
            code = RM.main(["--loop", "--run-dir", str(tmp_path / "r"),
                            "--shadow-db", shadow, "--mainnet-db", bad])
            assert code == RM.EXIT_REFUSED, bad
        # the same path with no telltale suffix must still be refused: equality alone
        plain = str(tmp_path / "shadow_book")
        assert RM.main(["--loop", "--run-dir", str(tmp_path / "r"),
                        "--shadow-db", plain, "--mainnet-db", plain]) == RM.EXIT_REFUSED
        assert started == []
        assert RM.main(["--loop", "--run-dir", str(tmp_path / "r2"),
                        "--mainnet-db", str(tmp_path / "m.jsonl")]) == 0
        assert started[0]["mainnet_db"] == str(tmp_path / "m.jsonl")

    def test_the_status_script_reports_the_mainnet_evidence_and_survives_its_absence(self):
        sh = self.read("../scripts/runner_status.sh")
        assert "mainnet_public.py report" in sh and "not profit" in sh
        assert "unavailable" in sh.split("mainnet_public.py report", 1)[1]


class TestTheStatusScriptSection:
    """runner_status.sh against a fake `fly` that answers the mainnet report with
    a REAL `mainnet_public.report` (so the section's field names are checked
    against the code that writes them, not against a hand-written dict)."""

    def run(self, tmp_path, mainnet_text):
        import test_monitor_summary as TMS
        fly = TMS.fake_fly(tmp_path, history=TestHistoryAndSummary().recent_history(),
                           report=TMS.REPORT)
        (tmp_path / "mainnet.txt").write_text(mainnet_text)
        src = open(fly).read()
        src = src.replace("      *forward-report*)",
                          f"      *mainnet_public*) cat {tmp_path}/mainnet.txt ;;\n      *forward-report*)", 1)
        open(fly, "w").write(src)
        import subprocess
        return subprocess.run(["bash", os.path.join(os.path.dirname(BOT), "scripts", "runner_status.sh")],
                              capture_output=True, text=True, timeout=120,
                              env={**os.environ, "FLY": fly})

    def test_a_real_report_is_summarised(self, tmp_path):
        db = str(tmp_path / "m.jsonl")
        MP.append(db, obs(perp=100_000.0))
        MP.append(db, obs(perp=101_500.0))
        r = self.run(tmp_path, json.dumps(MP.report(db)))
        assert r.returncode == 0, r.stdout + r.stderr
        out = r.stdout
        assert "== mainnet public evidence" in out
        assert "observations 2" in out and "basis bps min +0 median +75 max +150" in out
        assert "entry gate open on 1 of 2 observations (50%)" in out
        assert "no fills, fees, slippage or liquidation are modelled" in out
        assert "observations, not profit" in out

    def test_an_absent_report_is_unavailable_not_a_failure(self, tmp_path):
        r = self.run(tmp_path, "")
        assert r.returncode == 0 and "mainnet report unavailable" in r.stdout

    def test_a_broken_chain_is_shown_as_a_problem_line(self, tmp_path):
        db = str(tmp_path / "m.jsonl")
        MP.append(db, obs())
        open(db, "a").write("garbage\n")
        r = self.run(tmp_path, json.dumps(MP.report(db)))
        assert "!! the evidence file chain is broken" in r.stdout

    def test_no_observations_yet(self, tmp_path):
        r = self.run(tmp_path, json.dumps(MP.report(str(tmp_path / "none.jsonl"))))
        assert "no observations yet" in r.stdout


def UD_CREDENTIALS():
    import unattended_drill as UD
    return UD.CREDENTIALS
